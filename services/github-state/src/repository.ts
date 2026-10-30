import { RepositoryStore, SQLiteStore, PostgresStore, ReadCacheStore, BufferedStore, SerialLane, columns, collectionJoin } from './repository_store';
import { repositoryHealth } from './repository_health';
import { DurableObject } from 'cloudflare:workers';
import { Env, EventPointer, matchesETag, Query, Result, digest, json, queryKey, repository, stub, validateQuery, parseGitHub, CI_EVENTS, NOT_MIRRORED, publicQuery, stagingStateKey, stagedPageKey } from './protocol';
import { comment, issue, pr, status } from './normalize';
import { batchLimit } from './catalog';
import { Lane, LANES, laneAt, settings } from './reconciliation';
import { SURVEY_CONTRACT, SURVEY_INDEX_VERSION, compactPR, issueFragment, reviewFragment, summarizeFragments, inScope, roadmapFocuses } from './survey';
import { readPromotionReceipts } from './activity_landings';
import { queueMetrics } from './queue_monitoring';
import { ageProvider, providerObservation, providerAlarms } from './provider_monitoring';
import { targetConfig, targetOwner } from './migration_target';
import { controllerStep, preflight as cutoverPreflight, FREEZE_LEASE_MS } from './migration_control';
import { Migration, terminalReceipt, compatibilityStep, blocksRepairs, blocksWrites, archiveKey, beginExport, expectedTables, exportStep, generation, encodeMigration, publicReceipt, readMigration, saveMigration, targetIdentity, verifyStep } from './migration';
import { backlog, backoff, receiptKey, controlKey, noteRepositoryFull, storageKey } from './ingress';
import { Checkpoint, DEFAULT_POLICY, Policy, RetentionClass, R2_CLASSES, RULES, policy, sweep, sweepStore } from './retention';
import { CapacitySample, RepairMetrics, RequestMetrics, SQLITE_ALLOWANCE, capacity, postgresCapacity, emitAlarm, timedBucket, within } from './monitoring';
import { Failure, FULL_MESSAGE, AdminError, CLEANUP, PARKABLE, classify, deleteClass, faultySql, log, missingSchema, requestId, scan, summary, userTables as summaryTables } from './storage';

class LeaseLost extends Error {}
const SCHEMA_VERSION=5;
// 10 GiB limit less a margin. With no recorded storage state, a database this
// large is treated as full: no startup write may risk a failed commit, which
// resets the object and would repeat on every start.
const FULL_DATABASE=10*1024**3-16*1024**2;
// Clients do not judge data age. While the store cannot apply changes, a
// mirror that has not changed for this long is reported unavailable so
// clients use their direct GitHub fallback instead of acting on frozen state.
const FROZEN_AFTER=10*60000;
const CURSOR_UPLOADS=4;
// Change when the survey wire representation changes without a data revision.
const SURVEY_ETAG_PREFIX='survey-v1-empty-markers:';
// Private to local read scopes; request bodies and RPC arguments cannot set it.
const ACTIVITY_CLOCK=Symbol('activity clock');
const decimalArgument=(value:string):number|string=>Number.isSafeInteger(Number(value))?Number(value):value;
const activityFingerprint=(p:any,revision:number|undefined)=>JSON.stringify([p.headRefOid,p.baseRefOid,p.changedFiles,p.additions,p.deletions,revision]);
const cursorKey=(repo:string,id:string,offset:number)=>`cursors/${repo}/${id}/${offset}.json`;
function coordinationWindows(history:any,now:number):any {
  if(!history)return null;
  const windows:any={};
  for(const [name,days] of [['24h',1],['7d',7]] as const) {
    const result:any={dual:0,legacy:0,unknown:0,git_held:0};
    for(const [at,b] of Object.entries(history.buckets) as [string,any][])if(Number(at)>=Math.floor((now-days*86400000)/3600000)*3600000)for(const key of Object.keys(result))result[key]+=b[key] || 0;
    result.upgraded_fraction=result.dual+result.legacy?result.dual/(result.dual+result.legacy):null;windows[name]=result;
  }
  return {observed_since:history.observed_since,as_of:now,source:'observed-created-marker-webhooks',bucket_seconds:3600,windows,
    legacy_lease_upper_bound:Math.max(0,...Object.values(history.buckets).map((b:any)=>b.legacy_until || 0))};
}

export class RepositoryState extends DurableObject<Env> {
  private [ACTIVITY_CLOCK]?:number;
  private inflightQueries=new Map<string,Promise<Result>>();
  private publicCompletedQueries=new Map<string,{result:Result,expires:number,built:number,bytes?:number}>();
  private completedQueries=new Map<string,{result:Result,expires:number,built:number,bytes?:number}>();
  private alarmFlight?:Promise<void>;
  private revisionState:{value?:number}={};
  private readRevision?:number;
  private owner:RepositoryState=this;
  private maintenanceIOScope={};
  private transactionIOScope={};
  private mutationState={active:0,blocked:false};
  private pendingChanges?:{revision:number,at:number,kind:string,id:string,tombstone:number}[];
  private repositoryMemo?:any;
  private readOnlyQuery=false;
  private readScope=false;
  private preparedEncodings?:Map<string,string>;
  private optionalWork={active:0};
  private demandMemo=new Map<string,number>();
  sql:SqlStorage;
  store:RepositoryStore;
  localMeta(key:string):any {const row=this.sql.exec<any>('SELECT value FROM meta WHERE key=?',key).toArray()[0];return row?JSON.parse(row.value):undefined;}
  localSetMeta(key:string,value:unknown):void {this.sql.exec('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',key,JSON.stringify(value));}
  // R2 owns durable full state; this is its local read-only guard.
  degraded?:{since:number,reason:Failure,operation:string};
  schema:'current'|'migration_pending'|{error:Failure,missing:string[],cause:Failure}='current';
  private faultFull=false;
  private retentionLane=new SerialLane();
  private retentionPolicy: Policy = DEFAULT_POLICY;
  private retentionState: Partial<Record<RetentionClass, Checkpoint>> = {};
  private storageFlight?: Promise<void>;
  private storageLoaded=false;
  private retentionProgressLoaded=false;
  private retentionLoadFlight?:Promise<void>;
  private policyGeneration=0;
  private policyUpdateFlight:Promise<void>=Promise.resolve();
  private checkpointAt=0;
  private checkpointText='';
  private storageTurn=0;
  private receiptsPinned=true;
  private receiptsChecked=0;
  private observation:any;
  private observationReadAt=0;
  private diagnostics:any={coverage:[],lanes:[],waits:[],repair_hour:[],requested_latency:[],queries:[],repair_operations:[]};
  private diagnosticsAt=0;
  private lastCapacity:any;
  private postgresUnavailableSince?:number;
  private monitoringFailureSince?:number;
  private inputFailure?:{since:number,at:number,reason:string};
  private capacityHistory:CapacitySample[]=[];
  private tableEstimates:any;
  private tableAttemptAt=0;
  private requestMetrics=new RequestMetrics();
  private queryCounts=new Map<string,{operation:string,coverage:string,requests:number,last_at:number}>();
  private repairMetrics=new RepairMetrics();
  private probeFlight?: Promise<void>;
  private storageMarkerUnavailable=false;
  private deliveryFlights = new Map<string,Promise<void>>();
  // Planned-migration barrier: loaded from R2 once per instance and updated
  // by the admin endpoint, which waits for in-flight applications to finish.
  private ingress?:string;
  private applying=0;
  private migrationState: Migration | null = null;
  private nativeSql:SqlStorage;
  private selectedMigrationGeneration?:string;
  private migrationUnavailable=false;
  private activeOperations=0;
  private repairTraces=new WeakMap<object,string>();
  private migrationFlight: Promise<unknown> = Promise.resolve();
  migrationHeld(): boolean { return this.migrationUnavailable || blocksWrites(this.migrationState); }
  private repairsHeld():boolean {return this.migrationUnavailable || blocksRepairs(this.migrationState);}
  private traceRepair(stage:string,job?:any,counts?:{rows?:number,repairs?:number,staged_chars?:number}):void {
    if(this.env.RECONCILIATION_TRACE!=='true')return;
    let trace:string|undefined;
    if(job){trace=this.repairTraces.get(job);if(!trace){trace=crypto.randomUUID();this.repairTraces.set(job,trace);}}
    // Whitelist metadata: never log queries, keys, cursor values, response
    // bodies, exceptions or credentials when investigating an isolate reset.
    log('repository_repair_trace',{stage,backend:this.store.backend,...(job?{trace,operation:JSON.parse(job.query).operation,page:job.page,started:job.started ?? null}:{}),...counts});
  }
  private backendGeneration():string {return this.selectedMigrationGeneration ?? this.env.REPOSITORY_GENERATION ?? 'sqlite-v1';}
  private logged=new Map<string,number>();
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx,env);
    this.sql=env.FAULT_INJECTION==='true'?faultySql(ctx.storage.sql,()=>this.faultFull):ctx.storage.sql;
    const sourceSql=this.sql;this.nativeSql=sourceSql;
    this.sql=new Proxy(sourceSql,{get:(target,property)=>{
      if(property==='exec') return (query:string,...bindings:any[])=>{
        if(this.migrationHeld() && (this.migrationUnavailable || this.migrationState?.phase!=='freezing')) {
          // Frozen repository SQL uses single read statements only. Reject
          // multi-statement strings even if their first statement is a read.
          const readOnly=/^\s*(SELECT\b|PRAGMA\s+(table_info|index_list|index_info|page_size|page_count|freelist_count)\b)/i.test(query);
          if(query.includes(';') || !readOnly) throw new AdminError('migration_frozen');
        }
        return target.exec(query,...bindings);
      };
      const value=Reflect.get(target,property,target);
      return typeof value==='function'?value.bind(target):value;
    }});
    this.store=new SQLiteStore(ctx.storage,this.sql,false,env.REPOSITORY_GENERATION || 'sqlite-v1',ctx);
    // A committed local selection survives R2 outages and migration-binding
    // retirement. It is written durably before accepting any target input.
    let selected:any;try{selected=this.localMeta('backend_owner');}catch{}
    if(selected?.backend==='postgres' && selected.protocol==='s2-selection-v1') {
      this.selectCommittedBackend(selected);
      const repo=this.localMeta('repository')?.id;
      if(repo && ['test','production'].includes(env.MIGRATION_ENVIRONMENT || ''))ctx.blockConcurrencyWhile(async()=>{
        try {this.migrationState=await readMigration(env.ARCHIVE,repo);}catch{this.migrationUnavailable=true;}
      });
      return;
    }
    if(env.REPOSITORY_BACKEND==='postgres') {
      const repo=JSON.parse(env.ALLOWED_REPOSITORIES || '[]').find((r:any)=>env.REPOSITORIES.idFromName(r.id).toString()===ctx.id.toString());
      if(!repo || !env.REPOSITORY_DATABASE_URL || !env.REPOSITORY_GENERATION) throw new Error('PostgreSQL backend configuration missing');
      this.store=new PostgresStore(env.REPOSITORY_DATABASE_URL,repo.id,env.REPOSITORY_GENERATION);
      return; // No schema creation, migration, bootstrap or GitHub reads.
    }
    if(env.REPOSITORY_BACKEND && env.REPOSITORY_BACKEND!=='sqlite') throw new Error('Unknown repository backend');
    if (this.sql.databaseSize>=FULL_DATABASE) this.degraded={since:Date.now(),reason:'storage_full',operation:'database_size'};
    // A current or fresh schema starts synchronously; migrating an existing
    // legacy schema waits until the durable storage state is known.
    if (env.FAULT_INJECTION!=='true') this.startup(true);
    ctx.blockConcurrencyWhile(async()=>{
      if (env.FAULT_INJECTION==='true') {
        this.faultFull=(await env.ARCHIVE.get(this.faultKey()).then(o=>o?.json<any>()))?.mode==='full';
        if (this.faultFull) this.degraded ||= {since:Date.now(),reason:'storage_full',operation:'fault_injection'};
      }
      let repo:string | undefined;
      try {repo=this.localMeta('repository')?.id;} catch { /* Fresh test objects have no schema yet. */ }
      if(repo && this.env.ARCHIVE && ['test','production'].includes(this.env.MIGRATION_ENVIRONMENT || '')) {
        try {this.migrationState=await readMigration(this.env.ARCHIVE,repo);await this.selectMigrationBackend();
          if(this.store.backend==='sqlite')this.startup(true);}
        catch {this.migrationUnavailable=true;
          if(this.store.backend==='sqlite')this.startup(true);}
      }
      await this.storageMarker();
      if (env.FAULT_INJECTION==='true' || this.schema!=='current') this.startup();
      await this.loadRetentionState();
    });
  }
  // A recorded full state degrades the object; a probe-verified writable state
  // overrides the size rule after cleanup, since freed pages do not shrink the file.
  async storageMarker(): Promise<void> {
    if(this.store.backend==='postgres')return;
    let marker:any,repo:string|undefined;
    try {repo=(await this.meta('repository'))?.id;} catch {return;} // Fresh objects have no schema yet.
    try {
      marker=repo?await this.env.ARCHIVE.get(storageKey(repo)).then(o=>o?.json<any>()):undefined;
    } catch {this.storageMarkerUnavailable=true;this.degraded ||= {since:Date.now(),reason:'storage_error',operation:'storage_marker_unavailable'};return;}
    this.storageMarkerUnavailable=false;
    const unknown=this.degraded?.operation==='storage_marker_unavailable';
    if (marker?.state==='full') this.degraded={since:Number(marker.since) || Date.now(),reason:'storage_full',operation:'recorded:'+marker.source};
    else if (unknown || marker?.state==='writable' && (this.degraded?.operation==='database_size' || this.degraded && Number(marker.verified_at)>=this.degraded.since)) this.degraded=undefined;
    if(unknown && !this.degraded && this.schema!=='current')this.startup();
  }
  markFull(operation:string): void {
    const repo=(()=>{try {return this.localMeta('repository')?.id;} catch {return undefined;}})();
    if (repo) noteRepositoryFull(this.env,repo,operation,this.degraded?.since ?? Date.now()).catch(()=>{});
  }
  faultKey(): string { return 'faults/'+this.ctx.id.toString()+'.json'; }
  // Existing current schemas start without any write. Migration is one
  // transaction; if it cannot commit, compatible stored data stays readable.
  private frozenSourceReadable(owner:any,version:unknown):boolean {
    if(version!==SCHEMA_VERSION || this.store.backend!=='sqlite' || owner?.backend!=='migration' || owner.protocol!=='s2-selection-v1')return false;
    const state=this.migrationState;
    // A known activation prohibits source fallback even if selecting its
    // configuration fails. Unknown control can expose only the locally fenced
    // source: accepting target input requires a durable local postgres selection.
    if(state?.cutover?.target_active || state && state.generation!==owner.generation)return false;
    return this.migrationUnavailable || Boolean(this.migrationHeld() && state?.cutover?.source_fenced);
  }
  startup(defer=false): void {
    let version:unknown;
    try {version=this.localMeta('schema_version');} catch {version=undefined;}
    let owner:any;try{owner=this.localMeta('backend_owner');}catch{if(version!==undefined)throw new Error('Invalid backend ownership metadata');}
    if(owner && (owner.backend!=='sqlite' || owner.generation!==(this.env.REPOSITORY_GENERATION || 'sqlite-v1'))) {
      // A known frozen source remains readable after a constructor or storage
      // marker retry. The adapters and migration gate still fence every write.
      const frozenSource=this.frozenSourceReadable(owner,version);
      this.schema=frozenSource?'current':{error:'backend_fenced',missing:[],cause:'backend_fenced'};return;
    }
    if(version===SCHEMA_VERSION) {this.schema='current';return;}
    if(this.migrationHeld()) {this.schema='migration_pending';return;}
    if(this.degraded || defer && this.sql.exec<any>("SELECT COUNT(*) n FROM sqlite_master WHERE type='table' AND name='meta'").one().n) {
      // Never attempt a migration that cannot commit; existing data stays readable.
      let missing:string[];
      try {missing=missingSchema(this.sql);} catch {missing=['sqlite_master'];}
      this.schema=missing.length?{error:'schema_unavailable',missing,cause:'storage_full'}:'migration_pending';
      return;
    }
    try {this.ctx.storage.transactionSync(()=>this.migrate());this.schema='current';}
    catch(error) {
      let missing:string[];
      try {missing=missingSchema(this.sql);} catch {missing=['sqlite_master'];}
      const cause=this.noteFailure('migration',error);
      this.schema=missing.length?{error:'schema_unavailable',missing,cause}:'migration_pending';
    }
  }
  migrate(): void {
    this.sql.exec(`CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
      CREATE TABLE IF NOT EXISTS resources(kind TEXT,id TEXT,parent TEXT,data TEXT,updated TEXT,observed INTEGER,revision INTEGER,tombstone INTEGER DEFAULT 0,PRIMARY KEY(kind,id));
      CREATE TABLE IF NOT EXISTS activity_loc_cache(number INTEGER PRIMARY KEY,fingerprint TEXT,data TEXT);
      CREATE TABLE IF NOT EXISTS activity_landings(number INTEGER,head TEXT,at INTEGER,source TEXT,PRIMARY KEY(number,head));
      CREATE INDEX IF NOT EXISTS resource_parent ON resources(kind,parent,tombstone);
      CREATE TABLE IF NOT EXISTS collections(key TEXT PRIMARY KEY,operation TEXT,args TEXT,complete INTEGER,verified INTEGER,revision INTEGER,etag TEXT);
      CREATE TABLE IF NOT EXISTS members(collection TEXT,id TEXT,PRIMARY KEY(collection,id));
      CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,event TEXT,received INTEGER);
      CREATE TABLE IF NOT EXISTS changes(revision INTEGER PRIMARY KEY,at INTEGER,kind TEXT,id TEXT,tombstone INTEGER);
      CREATE TABLE IF NOT EXISTS jobs(key TEXT PRIMARY KEY,query TEXT,priority INTEGER,due INTEGER,started INTEGER,cursor TEXT,page INTEGER DEFAULT 1,api TEXT,etag TEXT,attempt INTEGER DEFAULT 0,dirty INTEGER DEFAULT 0,generation INTEGER DEFAULT 0,start_generation INTEGER DEFAULT 0,lane TEXT DEFAULT 'verification',lease_id TEXT,lease_until INTEGER DEFAULT 0,requested_at INTEGER,created_at INTEGER,wait_reason TEXT,nested TEXT);
      CREATE INDEX IF NOT EXISTS jobs_due ON jobs(due,priority);
      CREATE INDEX IF NOT EXISTS jobs_lane_due ON jobs(lane,due,lease_until);
      CREATE TABLE IF NOT EXISTS demand(key TEXT PRIMARY KEY,at INTEGER);
      CREATE TABLE IF NOT EXISTS retired_objects(key TEXT PRIMARY KEY,at INTEGER);
      CREATE TABLE IF NOT EXISTS head_prs(sha TEXT,number TEXT,PRIMARY KEY(sha,number));`);
    this.sql.exec(`CREATE TABLE IF NOT EXISTS survey_fragments(kind TEXT,id TEXT,parent TEXT,data TEXT,PRIMARY KEY(kind,id));
      CREATE INDEX IF NOT EXISTS survey_fragment_parent ON survey_fragments(parent,kind);
      CREATE TABLE IF NOT EXISTS survey_state(number TEXT PRIMARY KEY,data TEXT,revision INTEGER);
      CREATE TABLE IF NOT EXISTS survey_inventory(number TEXT PRIMARY KEY,data TEXT);
      CREATE TABLE IF NOT EXISTS survey_hydrated(number TEXT PRIMARY KEY,contract TEXT);
      CREATE TABLE IF NOT EXISTS survey_dirty(number TEXT PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS survey_hydration_failures(number TEXT PRIMARY KEY,retry_at INTEGER);`);
    this.sql.exec(`CREATE TABLE IF NOT EXISTS repair_metrics(at INTEGER,event TEXT,lane TEXT,operation TEXT,value INTEGER DEFAULT 1);
      CREATE INDEX IF NOT EXISTS repair_metrics_at ON repair_metrics(at);
      CREATE TABLE IF NOT EXISTS query_metrics(operation TEXT,coverage TEXT,requests INTEGER,last_at INTEGER,PRIMARY KEY(operation,coverage));`);
    // Continuation pages live in R2; the SQL cursor table is gone.
    this.sql.exec('DROP TABLE IF EXISTS cursors');
    this.localSetMeta('schema_version',SCHEMA_VERSION);
  }
  noteFailure(operation:string,error:unknown): Failure {
    const reason=classify(error);
    const owner=this.owner || this;
    if(reason==='storage_full' && !owner.degraded) {owner.degraded={since:Date.now(),reason,operation};owner.markFull(operation);}
    // At most one structured line per operation and minute; no payloads.
    const now=Date.now();
    if((this.logged.get(operation) || 0)<now-60000) {
      this.logged.set(operation,now);
      log('repository_storage_failure',{operation,reason,degraded:Boolean(this.degraded),message:(error instanceof Error?error.message:String(error)).slice(0,200)});
    }
    return reason;
  }
  // Demand, metrics, refresh scheduling and derived caches never decide
  // whether stored data can be served. Degraded mode suppresses them.
  async optional<T>(operation:string,write:()=>T|Promise<T>): Promise<T | undefined> {
    if(this.degraded || this.repairsHeld()) return undefined;
    try {return await write();} catch(error) {this.noteFailure(operation,error);return undefined;}
  }
  async optionalAsync(operation:string,write:()=>Promise<unknown>): Promise<void> {
    if(this.degraded || this.repairsHeld()) return;
    try {await write();} catch(error) {this.noteFailure(operation,error);}
  }
  async setAlarm(at:number): Promise<void> {
    // PostgreSQL uses the existing cron tick, never the obsolete SQLite file.
    if(this.store.backend==='postgres')return;
    if(this.faultFull) throw new Error(FULL_MESSAGE);
    await this.ctx.storage.setAlarm(at);
  }
  async atomic<T>(apply:(state:RepositoryState)=>Promise<T>):Promise<T> {
    if(this.store.transactional)return apply(this);
    if(this.migrationHeld())throw new AdminError('migration_frozen');
    if(this.mutationState.blocked)throw new Error('Repository backend generation fenced');
    this.mutationState.active++;
    let scoped:RepositoryState | undefined;
    try {
      const result=await this.store.transaction(async store=>{
        scoped=Object.create(this) as RepositoryState;
        const buffered=store.backend==='postgres'?new BufferedStore(store):undefined;
        scoped!.store=buffered || store;scoped!.revisionState={};scoped!.readScope=false;scoped!.readRevision=undefined;scoped!.repositoryMemo=undefined;scoped!.pendingChanges=store.backend==='postgres'?[]:undefined;
        const result=await apply(scoped!);await scoped!.flushChanges();await buffered?.flush();return result;
      });
      if(scoped?.revisionState.value!==undefined)this.revisionState.value=Math.max(this.revisionState.value ?? 0,scoped.revisionState.value);
      if(scoped?.repositoryMemo!==undefined)this.repositoryMemo=scoped.repositoryMemo;
      return result;
    }catch(error){this.revisionState.value=undefined;throw error;}finally{this.mutationState.active--;}
  }
  backgroundDemand(q:Query):void {
    if(this.readOnlyQuery)return;
    const key=queryKey(q),now=Date.now();
    if(this.optionalWork.active>=2 || (this.demandMemo.get(key) || 0)>now-60000)return;
    this.demandMemo.set(key,now);
    while(this.demandMemo.size>2000)this.demandMemo.delete(this.demandMemo.keys().next().value!);
    this.backgroundWrite('demand',()=>this.noteDemand(q));
  }
  backgroundWrite(operation:string,write:()=>Promise<unknown>):void {
    if(this.optionalWork.active>=2)return;
    this.optionalWork.active++;
    const work=this.optionalAsync(operation,write).finally(()=>{this.optionalWork.active--;});
    if(this.ctx.waitUntil)this.ctx.waitUntil(work);
  }
  async noteReadDemand(q:Query,known?:boolean):Promise<void> {
    if(this.readOnlyQuery)return;
    if(this.store.backend==='postgres' && (known ?? await this.complete(q))){this.backgroundDemand(q);return;}
    await this.optionalAsync('demand',()=>this.noteDemand(q));
  }
  async optionalRead(operation:string,write:()=>Promise<unknown>):Promise<void> {
    if(this.readOnlyQuery)return;
    if(this.store.backend==='postgres' && this.readScope){this.backgroundWrite(operation,write);return;}
    await this.optionalAsync(operation,write);
  }
  async scanRows(sql:string,...bindings:(string|number|null)[]):Promise<any[]> {
    const cached=this.store instanceof ReadCacheStore?this.store.peek(sql,bindings):undefined;if(cached!==undefined)return cached;
    const remote=this.store.backend==='postgres';let limit=remote?4096:256;
    // Bound transferred bytes as well as row count, without multiplying remote
    // round trips for small rows. One oversized row is allowed for progress.
    const bounded=(query:string,order:string)=>remote?'SELECT * FROM (SELECT stored_page.*,SUM(octet_length(to_jsonb(stored_page)::text)) OVER (ORDER BY '+order+' ROWS UNBOUNDED PRECEDING) AS __bytes,ROW_NUMBER() OVER (ORDER BY '+order+') AS __position,COUNT(*) OVER () AS __candidate_count FROM ('+query+') AS stored_page) AS bounded_page WHERE __bytes<=2097152 OR __position=1 ORDER BY '+order:query;
    const finished=(page:any[])=>!page.length || (remote?page.length===page[0].__candidate_count && page[0].__candidate_count<limit:page.length<limit);
    // A byte cut must also reduce candidate work on subsequent pages. Otherwise
    // large rows would be converted repeatedly behind the payload filter.
    const adapt=(page:any[])=>{if(remote)limit=Math.min(4096,page.length<page[0].__candidate_count?Math.max(1,page.length*2):limit*2);};
    const rows:any[]=[],ordered=sql.match(/ ORDER BY CAST\((r|f)\.id AS INTEGER\),\1\.id$/);
    if(ordered){
      const alias=ordered[1],inner=sql.slice(0,ordered.index).replace(/^SELECT /,`SELECT CAST(CAST(${alias}.id AS INTEGER) AS TEXT) AS __sort,${alias}.id AS __id,`);let after:{sort:string,id:string}|undefined;
      for(;;){
        const order='CAST(__sort AS NUMERIC),__id',query='SELECT * FROM ('+inner+') AS source_page'+(after?' WHERE (CAST(__sort AS NUMERIC),__id)>(CAST(? AS NUMERIC),?)':'')+' ORDER BY '+order+' LIMIT '+limit;
        const page=(await this.store.exec<any>(bounded(query,order),...bindings,...(after?[after.sort,after.id]:[]))).toArray();rows.push(...page);if(finished(page))return rows;adapt(page);after={sort:String(page.at(-1).__sort),id:page.at(-1).__id};
      }
    }
    if(/\bORDER BY\b/.test(sql))throw Error('Unsupported repository scan ordering');
    const identity=/\bFROM members m\b/.test(sql)?'m.rowid':/\bJOIN resources r\b/.test(sql)?'r.rowid':'rowid',where=sql.indexOf(' WHERE ');let after=0;
    const selected=(where<0?sql:sql.slice(0,where)).replace(/^SELECT /,`SELECT ${identity} AS __after,`),predicate=where<0?'': '('+sql.slice(where+7)+') AND ';
    for(;;){const query=selected+' WHERE '+predicate+identity+'>? ORDER BY '+identity+' LIMIT '+limit,page=(await this.store.exec<any>(bounded(query,'__after'),...bindings,after)).toArray();rows.push(...page);if(finished(page))return rows;adapt(page);after=page.at(-1).__after;}
  }
  async primePublication(q:Query,rows:any[]):Promise<void> {
    const cache=new ReadCacheStore(this.store);this.store=cache;
    const kind=this.kind(q),ids=rows.flatMap(row=>{const id=q.operation==='files'?String(q.args?.number)+':'+String(row.filename):this.id(q.operation,row);return kind==='pr'?[id,queryKey({operation:'pr',args:{number:row.number}})]:[id];});
    if(q.args?.number){const parent=String(q.args.number),found=(await cache.exec("SELECT id FROM resources WHERE kind='pr' AND id=?",parent)).toArray();cache.seed("SELECT id FROM resources WHERE kind='pr' AND id=?",[parent],found);}
    for(let offset=0;offset<ids.length;offset+=256) {
      const chunk=ids.slice(offset,offset+256);
      const stored=(await cache.batch([{sql:'SELECT * FROM resources WHERE kind=? AND id IN ('+chunk.map(()=>'?').join(',')+')',bindings:[kind,...chunk]},
        {sql:'SELECT collection,id FROM members WHERE collection IN ('+(['pr_delta','issue_delta'].includes(q.operation)?'?,?':'?')+') AND id IN ('+chunk.map(()=>'?').join(',')+')',bindings:[queryKey(q),...(['pr_delta','issue_delta'].includes(q.operation)?[queryKey({operation:q.operation==='pr_delta'?'open_prs':'open_issues'})]:[]),...chunk]}]));
      for(const id of chunk){const found=stored[0].toArray().filter(r=>r.id===id);cache.seed('SELECT * FROM resources WHERE kind=? AND id=?',[kind,id],found);cache.seed('SELECT data,updated,observed FROM resources WHERE kind=? AND id=?',[kind,id],found);cache.seed('SELECT data,tombstone FROM resources WHERE kind=? AND id=?',[kind,id],found);for(const key of [queryKey(q),...(['pr_delta','issue_delta'].includes(q.operation)?[queryKey({operation:q.operation==='pr_delta'?'open_prs':'open_issues'})]:[])])cache.seed('SELECT id FROM members WHERE collection=? AND id=?',[key,id],stored[1].toArray().filter(r=>r.id===id && r.collection===key));}
    }
  }
  async *publicationRows(q:Query,rows:any[],source:RepositoryStore,metadataOnly=false):AsyncGenerator<any> {
    if(source.backend!=='postgres' || metadataOnly){yield* rows;return;}
    const kind=this.kind(q),identities=(row:any)=>{
      const id=q.operation==='files'?String(q.args?.number)+':'+String(row.filename):this.id(q.operation,row);
      return kind==='pr'?[id,queryKey({operation:'pr',args:{number:row.number}})]:[id];
    };
    for(let offset=0;offset<rows.length;){
      const candidates=rows.slice(offset,offset+128),ids=[...new Set(candidates.flatMap(identities))];
      // Size old bodies before loading them. A compact incoming listing can
      // refer to large stored detail rows; both sides constrain the group.
      const stored=(await source.exec<any>('SELECT id,octet_length(data) bytes FROM resources WHERE kind=? AND id IN ('+ids.map(()=>'?').join(',')+')',kind,...ids)).toArray();
      const sizes=new Map(stored.map(row=>[row.id,Number(row.bytes)]));
      let end=offset,newBytes=0,oldBytes=0;const seen=new Set<string>();
      for(const row of candidates){
        const fresh=identities(row).filter(id=>!seen.has(id)),old=fresh.reduce((n,id)=>n+(sizes.get(id) || 0),0),next=JSON.stringify(row).length*2;
        if(end>offset && (newBytes+next>2097152 || oldBytes+old>2097152))break;
        for(const id of fresh)seen.add(id);newBytes+=next;oldBytes+=old;end++;
      }
      this.store=source;
      const chunk=rows.slice(offset,end);
      await this.primePublication(q,chunk);
      yield* chunk;
      offset=end;
    }
  }
  async primeRelated(numbers:string[],heads:string[]):Promise<void> {
    if(!(this.store instanceof ReadCacheStore))return;
    const cache=this.store;
    // At most 256 parents per batch. Bodies remain in their normal resources;
    // survey reads load compact projections and status contexts only.
    for(let offset=0;offset<Math.max(numbers.length,heads.length);offset+=256) {
      const ns=numbers.slice(offset,offset+256),hs=[...new Set(heads.slice(offset,offset+256))];
      const keys=[...ns.flatMap(number=>['issue_comments','review_comments'].map(operation=>queryKey({operation,args:{number:decimalArgument(number)}}))),...hs.map(sha=>queryKey({operation:'statuses',args:{sha}}))];
      const tasks:{sql:string,bindings:(string|number)[]}[]=[];
      if(ns.length)for(const table of ['survey_hydrated','survey_state','survey_dirty'])tasks.push({sql:'SELECT * FROM '+table+' WHERE number IN ('+ns.map(()=>'?').join(',')+')',bindings:ns});
      if(keys.length)tasks.push({sql:'SELECT * FROM collections WHERE key IN ('+keys.map(()=>'?').join(',')+')',bindings:keys});
      const statusKeys=hs.map(sha=>queryKey({operation:'statuses',args:{sha}}));
      if(hs.length) {
        tasks.push({sql:"SELECT * FROM resources WHERE kind='statuses' AND id IN ("+statusKeys.map(()=>'?').join(',')+')',bindings:statusKeys});
        // Cache seeding must preserve the rowid order used by scanRows on
        // SQLite. Heap/index order changes would otherwise reorder rollups.
        tasks.push({sql:"SELECT * FROM resources WHERE kind='status_context' AND parent IN ("+hs.map(()=>'?').join(',')+') AND tombstone=0 ORDER BY rowid',bindings:hs});
      }
      const results=await cache.batch(tasks.map(t=>({...t,sql:t.sql+' LIMIT 50001'})));
      if(results.some(r=>r.toArray().length>50000))throw new Error('Related repository read exceeds batch bound');
      let at=0;
      if(ns.length)for(const [table,sql] of [['survey_hydrated','SELECT contract FROM survey_hydrated WHERE number=?'],['survey_state','SELECT data FROM survey_state WHERE number=?'],['survey_dirty','SELECT number FROM survey_dirty WHERE number=?']]) {
        const rows=results[at++].toArray();for(const number of ns)cache.seed(sql,[number],rows.filter(r=>r.number===number));
      }
      if(keys.length){const rows=results[at++].toArray();for(const key of keys){const found=rows.filter(r=>r.key===key && r.complete===1);cache.seed('SELECT key FROM collections WHERE key=? AND complete=1',[key],found);cache.seed('SELECT * FROM collections WHERE key=? AND complete=1',[key],found);}}
      if(hs.length) {
        const rows=results[at++].toArray(),contexts=results[at++].toArray();
        for(const sha of hs){const key=queryKey({operation:'statuses',args:{sha}});cache.seed('SELECT data FROM resources WHERE kind=? AND id=? AND tombstone=0',['statuses',key],rows.filter(r=>r.id===key && !r.tombstone));cache.seed('SELECT data FROM resources WHERE kind=? AND parent=? AND tombstone=0',['status_context',sha],contexts.filter(r=>r.parent===sha));}
      }
      if(ns.length) {
        const hydrated=new Set(results[0].toArray().filter(row=>row.contract===SURVEY_INDEX_VERSION).map(row=>row.number));
        const dirty=results[2].toArray().map(row=>row.number).filter(number=>hydrated.has(number));
        await this.primeSurveyFragments(dirty);
      }
    }
  }
  async primeSurveyFragments(numbers:string[]):Promise<void> {
    if(!(this.store instanceof ReadCacheStore) || !numbers.length)return;
    // A changed projection still needs current fragments, but one remote round
    // trip per history per PR makes repository-wide discovery miss its deadline.
    // Prefetch only dirty, hydrated parents in the existing bounded parent batch.
    // Match exact collection membership; orphaned/removed comments stay excluded.
    for(const kind of ['issue_comments','review_comments']) {
      const rows=await this.scanRows(`SELECT f.parent,f.data FROM survey_fragments f JOIN members m ON m.id=f.id AND m.collection=(? || '{"number":' || f.parent || '}') WHERE f.kind=? AND f.parent IN (${numbers.map(()=>'?').join(',')}) ORDER BY CAST(f.id AS INTEGER),f.id`,kind+':',kind,...numbers);
      const grouped=new Map<string,any[]>();
      for(const row of rows){let group=grouped.get(row.parent);if(!group){group=[];grouped.set(row.parent,group);}group.push({data:row.data});}
      for(const number of numbers)this.store.seed('SELECT f.data FROM survey_fragments f JOIN members m ON m.id=f.id AND m.collection=? WHERE f.kind=? AND f.parent=? ORDER BY CAST(f.id AS INTEGER),f.id',[queryKey({operation:kind,args:{number:decimalArgument(number)}}),kind,number],grouped.get(number) || []);
    }
  }
  async meta(key:string):Promise<any> {
    if(key==='repository' && this.repositoryMemo!==undefined)return this.repositoryMemo;
    const row=(await this.store.exec<any>('SELECT value FROM meta WHERE key=?',key)).toArray()[0],value=row?JSON.parse(row.value):undefined;
    if(key==='repository')this.repositoryMemo=value;return value;
  }
  async setMeta(key:string,value:unknown): Promise<void> { await this.store.exec('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',key,JSON.stringify(value));if(key==='revision')this.revisionState.value=Number(value);if(key==='repository')this.repositoryMemo=value; }
  async revision():Promise<number> {
    if(this.readRevision!==undefined)return this.readRevision;
    if(this.revisionState.value!==undefined)return this.revisionState.value;
    const value=(await this.meta('revision')) || 0;
    return this.revisionState.value=Math.max(this.revisionState.value ?? 0,value);
  }
  async flushChanges():Promise<void> {
    if(!this.pendingChanges?.length)return;
    const changes=this.pendingChanges;
    for(let offset=0;offset<changes.length;offset+=200) {
      const batch=changes.slice(offset,offset+200),writes=[{sql:'INSERT INTO changes VALUES '+batch.map(()=>'(?,?,?,?,?)').join(','),bindings:batch.flatMap(row=>[row.revision,row.at,row.kind,row.id,row.tombstone])}];
      if(offset+200>=changes.length)writes.push({sql:'INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',bindings:['revision',JSON.stringify(this.revisionState.value)]});
      await this.store.batch(writes);
    }
    this.pendingChanges=[];
  }
  async nextRevision(kind:string,id:string,tombstone=false): Promise<number> {
    if(!this.store.transactional)return this.atomic(state=>RepositoryState.prototype.nextRevision.call(state,kind,id,tombstone));
    const revision=(await this.revision())+1;
    if(!Number.isSafeInteger(revision))throw new Error('Repository revision exceeds safe range');
    if(this.pendingChanges){this.pendingChanges.push({revision,at:Date.now(),kind,id,tombstone:Number(tombstone)});this.revisionState.value=revision;return revision;}
    await this.store.batch([
      {sql:'INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',bindings:['revision',JSON.stringify(revision)]},
      {sql:'INSERT INTO changes VALUES(?,?,?,?,?)',bindings:[revision,Date.now(),kind,id,Number(tombstone)]}
    ]);
    this.revisionState.value=revision;return revision;
  }
  async encode(data: unknown, expires?:number,repositoryId?:string): Promise<string> {
    const text = JSON.stringify(data);
    const memoKey=(expires?'staging:':'resource:')+text;
    if(this.preparedEncodings?.has(memoKey))return this.preparedEncodings.get(memoKey)!;
    const bytes=new TextEncoder().encode(text);
    if (bytes.length < 1500000) return text;
    const hash=expires?undefined:await digest(bytes.buffer as ArrayBuffer);
    const key = `${expires?'staging-spills':'resources'}/${repositoryId || (await this.meta('repository')).id}/${hash?'sha256-'+hash:crypto.randomUUID()}`;
    // Active resources must remain readable for their entire SQL row lifetime.
    // Raw deliveries, staging pages, and snapshots have separate expiry rules.
    // Immutable resource contents reuse one object across identical refreshes.
    // Conditional creation also preserves any existing operator/migration pin.
    // Staging uploads retain distinct keys because their expiry can differ.
    if(!hash || !await this.env.ARCHIVE.head(key))await this.env.ARCHIVE.put(key,bytes,{customMetadata:{kind:expires?'cursor':'resource',...(expires?{expires:String(expires)}:{})},...(hash?{sha256:hash,onlyIf:{etagDoesNotMatch:'*'}}:{})});
    return JSON.stringify({$r2:key});
  }
  async decode(text: string): Promise<any> {
    const data = JSON.parse(text);
    if (data?.$r2) { const object = await (this.store.transactional && this.store.backend==='postgres'?this.transactionArchive:this.env.ARCHIVE).get(data.$r2); if (!object) throw new Error('resource unavailable'); return object.json(); }
    if(data && typeof data==='object' && !Array.isArray(data))delete data.__tauceti_received_at;
    return data;
  }
  async *decodeRows(rows:{data:string}[]):AsyncGenerator<any> {
    // Archived collection reads preserve order with a bounded R2 fanout.
    for(let i=0;i<rows.length;i+=8)for(const data of await Promise.all(rows.slice(i,i+8).map(r=>this.decode(r.data))))yield data;
  }
  async put(kind:string,id:string,data:any,started=Date.now(),deleted=false,parent='',fromEvent=false,prepared?:string): Promise<boolean> {
    if(!this.store.transactional) {
      const encoded=await this.encode(data);
      return this.atomic(state=>RepositoryState.prototype.put.call(state,kind,id,data,started,deleted,parent,fromEvent,encoded));
    }
    let old = (await this.store.exec<any>('SELECT * FROM resources WHERE kind=? AND id=?',kind,id)).toArray()[0];
    const oldStored=old?JSON.parse(old.data):null,received=oldStored?.__tauceti_received_at || 0;
    if(fromEvent && received>started)return false;
    const updated = data?.updatedAt || data?.updated_at || data?.created_at || data?.startedAt || '';
    // A response begun before a webhook cannot overwrite that event, even if HTTP finishes later.
    if (!fromEvent && old?.observed > started) return false;
    if (old?.updated && updated && old.updated > updated) return false;
    if (old?.updated && updated===old.updated && old.tombstone && !deleted) return false;
    if(kind==='pr' && old && data?.headRefOid) {
      const prior=await this.decode(old.data);
      if(prior.headRefOid===data.headRefOid && prior.baseRefOid===data.baseRefOid)
        data={...prior,...Object.fromEntries(Object.entries(data).filter(([,v])=>v!==undefined))};
      else if(data.mergeable===undefined) data={...data,mergeable:'UNKNOWN'};
    }
    const plain = prepared && kind!=='pr'?prepared:await this.encode(data);
    const stored=JSON.parse(plain),inputReceived=fromEvent?started:received;
    if(inputReceived && stored && typeof stored==='object' && !Array.isArray(stored))stored.__tauceti_received_at=inputReceived;
    const encoded=JSON.stringify(stored);
    if(oldStored && typeof oldStored==='object')delete oldStored.__tauceti_received_at;
    // R2 encoding yields: repeat the guard against events received during that await.
    if(!this.store.transactional)old=(await this.store.exec<any>('SELECT * FROM resources WHERE kind=? AND id=?',kind,id)).toArray()[0];
    if (!fromEvent && old?.observed>started || old?.updated && updated && old.updated>updated || old?.updated && updated===old.updated && old.tombstone && !deleted) return false;
    if (kind==='pr' && /^[1-9][0-9]*$/.test(id) && data?.headRefOid) await this.store.exec('INSERT OR IGNORE INTO head_prs VALUES(?,?)',data.headRefOid,id);
    if (old && JSON.stringify(oldStored)===plain && Boolean(old.tombstone)===deleted) {
      if(encoded===old.data)await this.store.exec('UPDATE resources SET observed=? WHERE kind=? AND id=?',Date.now(),kind,id);
      else await this.store.exec('UPDATE resources SET data=?,observed=? WHERE kind=? AND id=?',encoded,Date.now(),kind,id);
      if(this.store instanceof ReadCacheStore)this.store.seed('SELECT data,tombstone FROM resources WHERE kind=? AND id=?',[kind,id],[{data:encoded,tombstone:Number(deleted)}]);
      return true;
    }
    const rev = (await this.nextRevision(kind,id,deleted));
    const resourceWrite=this.store.backend==='postgres'
      ?old?{sql:'UPDATE resources SET parent=?,data=?,updated=?,observed=?,revision=?,tombstone=? WHERE kind=? AND id=?',bindings:[parent,encoded,updated,Date.now(),rev,Number(deleted),kind,id]}
        :{sql:'INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)',bindings:[kind,id,parent,encoded,updated,Date.now(),rev,Number(deleted)]}
      :{sql:'INSERT INTO resources VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET parent=excluded.parent,data=excluded.data,updated=excluded.updated,observed=excluded.observed,revision=excluded.revision,tombstone=excluded.tombstone',bindings:[kind,id,parent,encoded,updated,Date.now(),rev,Number(deleted)]};
    const writes:{sql:string,bindings:(string|number)[]}[]=[resourceWrite];
    if(kind==='pr' && /^[1-9][0-9]*$/.test(id))writes.push({sql:'INSERT INTO survey_inventory VALUES(?,?) ON CONFLICT(number) DO UPDATE SET data=excluded.data',bindings:[id,JSON.stringify(compactPR(data))]});
    await this.store.batch(writes);
    if(this.store instanceof ReadCacheStore)this.store.seed('SELECT data,tombstone FROM resources WHERE kind=? AND id=?',[kind,id],[{data:encoded,tombstone:Number(deleted)}]);
    if(['issue_comments','review_comments'].includes(kind)) {
      await this.projectSurvey(kind,id,parent,data,deleted);await this.dirtySurvey(parent);
    }
    return true;
  }
  async membership(key:string,id:string,included:boolean): Promise<void> {
    if(!this.store.transactional)return this.atomic(state=>RepositoryState.prototype.membership.call(state,key,id,included));
    const exists=(await this.store.exec('SELECT id FROM members WHERE collection=? AND id=?',key,id)).toArray().length>0;
    if (exists===included) return;
    if (included) await this.store.exec('INSERT INTO members VALUES(?,?)',key,id);
    else await this.store.exec('DELETE FROM members WHERE collection=? AND id=?',key,id);
    await this.nextRevision('collection',key);
    if(key.startsWith('issue_comments:') || key.startsWith('review_comments:')) {
      const args=JSON.parse(key.slice(key.indexOf(':')+1));
      if(args.number) await this.dirtySurvey(String(args.number));
    }
  }
  async projectSurvey(kind:string,id:string,parent:string,data:any,deleted=false): Promise<void> {
    if(!this.store.transactional)return this.atomic(state=>RepositoryState.prototype.projectSurvey.call(state,kind,id,parent,data,deleted));
    if(!parent || !(await this.store.exec("SELECT id FROM resources WHERE kind='pr' AND id=?",parent)).toArray().length) return;
    const fragment=kind==='issue_comments'?issueFragment(data):reviewFragment(data);
    const useful=kind==='issue_comments'?fragment.meta!==null || fragment.rebase_heads.length:fragment.root || fragment.parent && fragment.reply;
    if(deleted || !useful) await this.store.exec('DELETE FROM survey_fragments WHERE kind=? AND id=?',kind,id);
    else await this.store.exec('INSERT INTO survey_fragments VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET parent=excluded.parent,data=excluded.data',kind,id,parent,JSON.stringify(fragment));
  }
  async projectedSurvey(number:string): Promise<string> {
    const read=async (kind:string)=>(await this.scanRows('SELECT f.data FROM survey_fragments f JOIN members m ON m.id=f.id AND m.collection=? WHERE f.kind=? AND f.parent=? ORDER BY CAST(f.id AS INTEGER),f.id',queryKey({operation:kind,args:{number:decimalArgument(number)}}),kind,number)).map(row=>JSON.parse(row.data));
    return JSON.stringify(summarizeFragments(await read('issue_comments'),await read('review_comments')));
  }
  async rebuildSurvey(number:string,data?:string): Promise<void> {
    if(!this.store.transactional)return this.atomic(state=>RepositoryState.prototype.rebuildSurvey.call(state,number,data));
    await this.store.batch([
      {sql:'INSERT INTO survey_state VALUES(?,?,?) ON CONFLICT(number) DO UPDATE SET data=excluded.data,revision=excluded.revision',bindings:[number,data ?? await this.projectedSurvey(number),await this.revision()]},
      {sql:'DELETE FROM survey_dirty WHERE number=?',bindings:[number]}
    ]);
  }
  async dirtySurvey(number:string): Promise<void> {
    if(this.store instanceof ReadCacheStore && this.store.dirtyParents.has(number))return;
    if(!this.store.transactional)return this.atomic(state=>RepositoryState.prototype.dirtySurvey.call(state,number));
    if(number && (await this.store.exec("SELECT id FROM resources WHERE kind='pr' AND id=?",number)).toArray().length){
      await this.store.exec('INSERT OR IGNORE INTO survey_dirty VALUES(?)',number);
      if(this.store instanceof ReadCacheStore)this.store.dirtyParents.add(number);
    }
  }
  async invalidateSurvey(number:string): Promise<void> {
    if(!this.store.transactional)return this.atomic(state=>RepositoryState.prototype.invalidateSurvey.call(state,number));
    await this.store.exec('DELETE FROM survey_hydrated WHERE number=?',number);
    await this.store.exec('INSERT INTO survey_hydration_failures VALUES(?,?) ON CONFLICT(number) DO UPDATE SET retry_at=excluded.retry_at',number,Date.now()+300000);
    await this.nextRevision('survey_failure',number);
  }
  async hydrateSurvey(number:string): Promise<void> {
    if(!this.store.transactional)return this.atomic(state=>RepositoryState.prototype.hydrateSurvey.call(state,number));
    if(this.store.backend==='postgres')return this.hydratePostgresSurvey(number);
    // Existing deployments already have complete histories. Project those
    // locally once, in bounded batches, without any new GitHub requests.
    const prRow=(await this.store.exec<any>("SELECT data,revision FROM resources WHERE kind='pr' AND id=?",number)).toArray()[0];
    if(prRow) {
      const data=await this.decode(prRow.data);
      if((await this.store.exec<any>("SELECT revision FROM resources WHERE kind='pr' AND id=?",number)).toArray()[0]?.revision===prRow.revision)
        await this.store.exec('INSERT INTO survey_inventory VALUES(?,?) ON CONFLICT(number) DO UPDATE SET data=excluded.data',number,JSON.stringify(compactPR(data)));
    }
    const rows=(await this.store.exec<any>("SELECT kind,id,parent,data,revision FROM resources WHERE kind IN ('issue_comments','review_comments') AND parent=? AND tombstone=0",number)).toArray();
    for(const row of rows) {
      const data=await this.decode(row.data);
      const current=(await this.store.exec<any>('SELECT revision,tombstone FROM resources WHERE kind=? AND id=?',row.kind,row.id)).toArray()[0];
      if(current?.revision===row.revision && !current.tombstone) await this.projectSurvey(row.kind,row.id,number,data);
    }
    await this.rebuildSurvey(number);
    await this.nextRevision('survey',number);
    await this.store.exec('INSERT INTO survey_hydrated VALUES(?,?) ON CONFLICT(number) DO UPDATE SET contract=excluded.contract',number,SURVEY_INDEX_VERSION);
    await this.store.exec('DELETE FROM survey_hydration_failures WHERE number=?',number);
  }
  async hydratePostgresSurvey(number:string):Promise<void> {
    const prRow=(await this.store.exec<any>("SELECT data FROM resources WHERE kind='pr' AND id=?",number)).toArray()[0];
    if(prRow)await this.store.exec('INSERT INTO survey_inventory VALUES(?,?) ON CONFLICT(number) DO UPDATE SET data=excluded.data',number,JSON.stringify(compactPR(await this.decode(prRow.data))));
    const rows=await this.scanRows("SELECT kind,id,data FROM resources WHERE kind IN ('issue_comments','review_comments') AND parent=? AND tombstone=0",number);
    await this.store.exec('DELETE FROM survey_fragments WHERE parent=?',number);
    const fragments:(string|number)[][]=[];
    const flush=async()=>{if(!fragments.length)return;const batch=fragments.splice(0);await this.store.exec('INSERT INTO survey_fragments VALUES '+batch.map(()=>'(?,?,?,?)').join(',')+' ON CONFLICT(kind,id) DO UPDATE SET parent=excluded.parent,data=excluded.data',...batch.flat());};
    if(prRow)for(let offset=0;offset<rows.length;offset+=8) {
      const decoded=await Promise.all(rows.slice(offset,offset+8).map(async row=>({row,data:await this.decode(row.data)})));
      for(const {row,data} of decoded) {
        const fragment=row.kind==='issue_comments'?issueFragment(data):reviewFragment(data);
        const useful=row.kind==='issue_comments'?fragment.meta!==null || fragment.rebase_heads.length:fragment.root || fragment.parent && fragment.reply;
        if(useful)fragments.push([row.kind,row.id,number,JSON.stringify(fragment)]);
      }
      if(fragments.length>=200)await flush();
    }
    await flush();await this.rebuildSurvey(number);await this.nextRevision('survey',number);
    await this.store.batch([
      {sql:'INSERT INTO survey_hydrated VALUES(?,?) ON CONFLICT(number) DO UPDATE SET contract=excluded.contract',bindings:[number,SURVEY_INDEX_VERSION]},
      {sql:'DELETE FROM survey_hydration_failures WHERE number=?',bindings:[number]}
    ]);
  }
  async surveyRows(q:Query): Promise<{rows:any[],author_counts:Record<string,number>} | null> {
    const inventory={repository:q.repository,operation:'open_prs'};
    const inventoryComplete=await this.complete(inventory);
    await this.noteReadDemand(inventory,inventoryComplete);
    if(!inventoryComplete) {await this.optionalAsync('enqueue',async ()=>(await this.enqueue(inventory,5)));return null;}
    // Scan compact local metadata, then add statuses only for the selected
    // roadmap. Descriptions and unrelated comment histories stay in storage.
    const rows=await this.scanRows(`SELECT COALESCE(s.data,r.data) data FROM members m ${collectionJoin(this.store)} resources r ON r.kind='pr' AND r.id=m.id LEFT JOIN survey_inventory s ON s.number=r.id WHERE m.collection=? AND r.tombstone=0 ORDER BY CAST(r.id AS INTEGER),r.id`,queryKey(inventory));
    const scope=String(q.args?.roadmap || 'any'),skip=String(q.args?.skip || '').split(',').filter(Boolean),out=[];
    const author_counts:Record<string,number>={},stages=new Set(String(q.args?.stages ?? 'review,fix,rebase').split(','));
    const decoded=[];for(const storedRow of rows)decoded.push(compactPR(await this.decode(storedRow.data)));
    const selected=decoded.filter(row=>q.args?.numbers?String(q.args.numbers).split(',').includes(String(row.number)):inScope(row,scope,skip));
    await this.primeRelated(selected.map(r=>String(r.number)),selected.map(r=>r.headRefOid).filter(Boolean));
    for(const row of decoded) {
      if(row.state && row.state!=='OPEN') continue;
      const areas=roadmapFocuses((row.labels || []).map((label:any)=>label.name),row.target_focuses,row.headRefName);
      if(!row.isDraft && (areas===null || areas.some(area=>!skip.includes(area)))) {
        const author=row.author?.login || '';author_counts[author]=(author_counts[author] || 0)+1;
      }
      if(q.args?.numbers ? !String(q.args.numbers).split(',').includes(String(row.number)) : !inScope(row,scope,skip)) continue;
      const number=String(row.number);let hydrated=(await this.store.exec<any>('SELECT contract FROM survey_hydrated WHERE number=?',number)).toArray()[0]?.contract===SURVEY_INDEX_VERSION;
      let projected=(await this.store.exec<any>('SELECT data FROM survey_state WHERE number=?',number)).toArray()[0]?.data;
      if(hydrated && (await this.store.exec('SELECT number FROM survey_dirty WHERE number=?',number)).toArray().length) {
        // The projection is derived: serve it from fragments even when the
        // cache cannot be written, and persist it only when storage allows.
        try {projected=(await this.projectedSurvey(number));}catch {hydrated=false;await this.optionalRead('survey_invalidate',async ()=>(await this.invalidateSurvey(number)));}
        if(hydrated) {const data=projected;await this.optionalRead('survey_projection',async ()=>(await this.rebuildSurvey(number,data)));}
      }
      // Existing v1 clients require this array. The survey projection ignores
      // coordination comments, including claims in older cached projections.
      const review={...(hydrated && projected?JSON.parse(projected):summarizeFragments([],[])),markers:[],issue_complete:false,review_complete:false};
      const statuses=await this.rows({repository:q.repository,operation:'statuses',args:{sha:row.headRefOid}});
      const green=Array.isArray(statuses) && statuses.some(c=>c.context==='build' && c.state==='SUCCESS') && !statuses.some(c=>c.context==='build' && c.state!=='SUCCESS');
      for(const [operation,field] of [['issue_comments','issue_complete'],['review_comments','review_complete']]) {
        const child={repository:q.repository,operation,args:{number:row.number}};
        review[field]=hydrated && (await this.complete(child));
        // Demand is deduplicated in the installation scheduler; serving a
        // snapshot never performs synchronous upstream validation.
        const needs=operation==='issue_comments'?(green && (stages.has('review') || stages.has('fix'))) || stages.has('rebase') && (row.labels || []).some((l:any)=>l.name==='needs-rebase'):green && (stages.has('review') || stages.has('fix'));
        if(!row.isDraft && needs && !(await this.complete(child))) {await this.noteReadDemand(child);await this.optionalAsync('enqueue',async ()=>(await this.enqueue(child,5)));}
      }
      out.push({contract:SURVEY_CONTRACT,pr:{...compactPR(row),statusCheckRollup:Array.isArray(statuses)?statuses:[]},review});
    }
    return {rows:out,author_counts};
  }
  async surveyHealth(): Promise<any> {
    const counts=(await this.store.exec<any>(`SELECT COUNT(*) open_prs,SUM(h.contract=?) hydrated,SUM(s.number IS NOT NULL) materialized FROM members m ${collectionJoin(this.store)} resources r ON r.kind='pr' AND r.id=m.id LEFT JOIN survey_hydrated h ON h.number=r.id LEFT JOIN survey_state s ON s.number=r.id WHERE m.collection=? AND r.tombstone=0`,SURVEY_INDEX_VERSION,queryKey({operation:'open_prs'}))).one();
    const failures=(await this.store.exec<any>('SELECT COUNT(*) n FROM survey_hydration_failures')).one().n;
    return {contract:SURVEY_CONTRACT,...counts,failures};
  }
  id(operation:string,row:any): string { return String(row.number || row.id || row.node_id || row.ref || row.context || row.sha || row.headRefOid || 'value'); }
  async enqueue(q:Query,priority=20,due=Date.now(),dirty=false): Promise<void> {
    if(this.readOnlyQuery)return;
    if (q.operation==='progress' || NOT_MIRRORED.has(q.operation)) return;
    if(!this.store.transactional)return this.atomic(state=>RepositoryState.prototype.enqueue.call(state,q,priority,due,dirty));
    const key=queryKey(q);
    const prior=(await this.store.exec<any>('SELECT * FROM jobs WHERE key=?',key)).toArray()[0];
    const lane=(await this.lane(q,dirty || Boolean(prior?.dirty),priority)),requested=lane==='requested'?(prior?.requested_at || Date.now()):prior?.requested_at || null;
    await this.store.exec('INSERT INTO jobs(key,query,priority,due,dirty,generation,lane,requested_at,created_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET priority=excluded.priority,lane=excluded.lane,requested_at=excluded.requested_at,dirty=MAX(dirty,excluded.dirty),generation=generation+excluded.generation' + (dirty ? ",due=CASE WHEN wait_reason IS NOT NULL OR attempt>0 THEN due ELSE MIN(due,excluded.due) END" : ''),key,JSON.stringify({...q,allow_observed:undefined}),priority,due,Number(dirty),Number(dirty),lane,requested,Date.now());
    if (!prior) await this.metric('arrival',q,lane);
    const alarm=this.store.backend==='postgres'?null:await this.ctx.storage.getAlarm();
    const scheduled=(await this.store.exec<any>('SELECT due FROM jobs WHERE key=?',key)).one().due;
    if (!alarm || alarm>scheduled) await this.setAlarm(Math.max(Date.now()+100,scheduled));
  }
  async enqueueMany(input:{q:Query,priority:number,dirty?:boolean}[]):Promise<void> {
    input=input.filter(({q})=>q.operation!=='progress' && !NOT_MIRRORED.has(q.operation));
    if(!input.length)return;
    if(!this.store.transactional)return this.atomic(state=>RepositoryState.prototype.enqueueMany.call(state,input));
    const readBatch=this.store.backend==='sqlite'?80:256,writeBatch=this.store.backend==='sqlite'?10:200,metricBatch=this.store.backend==='sqlite'?20:200;
    const entries=new Map<string,{q:Query,priority:number,dirty?:boolean}>();
    for(const item of input){const key=queryKey(item.q),prior=entries.get(key);entries.set(key,{...item,dirty:prior?.dirty || item.dirty});}
    if(entries.size>512){
      const unique=[...entries.values()];
      for(let offset=0;offset<unique.length;offset+=512)await this.enqueueMany(unique.slice(offset,offset+512));
      return;
    }
    const jobs=new Map<string,any>(),demands=new Map<string,any>(),collections=new Set<string>();
    const keys=[...entries.keys()],numbers=new Set<string>(),heads=new Map<string,string[]>(),comments=new Map<string,any>();
    const select=async(table:string,column:string,values:string[],filter='',projection='*')=>{
      const rows:any[]=[];
      for(let offset=0;offset<values.length;offset+=readBatch) {
        const batch=values.slice(offset,offset+readBatch);
        rows.push(...await this.scanRows('SELECT '+projection+' FROM '+table+' WHERE '+column+' IN ('+batch.map(()=>'?').join(',')+')'+filter,...batch));
      }
      return rows;
    };
    for(let offset=0;offset<keys.length;offset+=readBatch) {
      const chunk=keys.slice(offset,offset+readBatch),where=' WHERE key IN ('+chunk.map(()=>'?').join(',')+')';
      const [js,ds,cs]=await this.store.batch([['jobs','key,dirty,requested_at,due,wait_reason,attempt'],['demand','key,at'],['collections','key,complete']].map(([table,projection])=>({sql:'SELECT '+projection+' FROM '+table+where,bindings:chunk})));
      for(const row of js.toArray())jobs.set(row.key,row);
      for(const row of ds.toArray())demands.set(row.key,row);
      for(const row of cs.toArray())if(row.complete)collections.add(row.key);
    }
    for(const {q} of entries.values())if(q.args?.number)numbers.add(String(q.args.number));
    const shas=[...new Set([...entries.values()].filter(e=>e.q.args?.sha).map(e=>String(e.q.args!.sha)))];
    for(const row of await select('head_prs','sha',shas)){const group=heads.get(row.sha)||[];group.push(row.number);heads.set(row.sha,group);numbers.add(row.number);}
    const ids=[...entries.values()].filter(e=>e.q.operation==='reactions').map(e=>String(e.q.args?.id));
    if(ids.length)for(const row of await select('resources','id',ids," AND kind='review_comments'",'id,kind,parent,tombstone'))if(row.kind==='review_comments'){comments.set(row.id,row);if(row.parent)numbers.add(row.parent);}
    const states=new Map<string,any>();
    for(const row of await select('resources','id',[...numbers]," AND kind IN ('pr','issue')","id,kind,json_extract(data,'$.state') state"))if(['pr','issue'].includes(row.kind) && (!states.has(row.id)||row.kind==='pr'))states.set(row.id,row);
    const now=Date.now();let scheduled=Infinity;
    const values:any[][]=[],metrics:any[][]=[];
    for(const [key,{q,priority,dirty=false}] of entries) {
      const prior=jobs.get(key),demanded=(demands.get(key)?.at || 0)>now-300000;
      const comment=q.operation==='reactions'?comments.get(String(q.args?.id)):null;
      const ns=q.args?.number?[String(q.args.number)]:q.args?.sha?heads.get(String(q.args.sha)) || []:comment?.parent?[comment.parent]:[];
      const dormant=!demanded && (comment?.tombstone || ns.length>0 && ns.every(n=>states.get(n)?.state && states.get(n).state!=='OPEN'));
      const lane=demanded && !collections.has(key)?'requested':dirty || prior?.dirty || priority<=5 || q.operation.endsWith('_delta')?'active':q.operation.startsWith('recent_') || dormant?'archive':'verification';
      values.push([key,JSON.stringify({...q,allow_observed:undefined}),priority,now,Number(dirty),Number(dirty),lane,lane==='requested'?(prior?.requested_at || now):prior?.requested_at || null,now]);
      if(!prior)metrics.push([now,'arrival',lane,q.operation,1]);
      scheduled=Math.min(scheduled,!prior?now:dirty && !prior.wait_reason && !prior.attempt?Math.min(prior.due,now):prior.due);
    }
    // Independent rows share a parameterized VALUES statement. This is the
    // inventory/history fan-out path; remote latency is bounded per batch.
    for(let offset=0;offset<values.length;offset+=writeBatch) {
      const batch=values.slice(offset,offset+writeBatch);
      await this.store.exec('INSERT INTO jobs(key,query,priority,due,dirty,generation,lane,requested_at,created_at) VALUES '+batch.map(()=>'(?,?,?,?,?,?,?,?,?)').join(',')+' ON CONFLICT(key) DO UPDATE SET priority=excluded.priority,lane=excluded.lane,requested_at=excluded.requested_at,dirty=MAX(dirty,excluded.dirty),generation=generation+excluded.generation,due=CASE WHEN excluded.generation>0 AND wait_reason IS NULL AND attempt=0 THEN MIN(due,excluded.due) ELSE due END',...batch.flat());
    }
    for(const [at,event,lane,operation,value] of metrics)this.repairMetrics.record(event,lane,operation,value,at);
    const alarm=this.store.backend==='postgres'?null:await this.ctx.storage.getAlarm();
    if(!alarm || alarm>scheduled)await this.setAlarm(Math.max(now+100,scheduled));
  }
  async complete(q:Pick<Query,'operation'|'args'>): Promise<boolean> { return (await this.store.exec('SELECT key FROM collections WHERE key=? AND complete=1',queryKey(q))).toArray().length>0; }
  async metric(event:string,q:Query,lane:Lane,value=1): Promise<void> {try{this.repairMetrics.record(event,lane,q.operation,value);}catch(error){try{this.noteFailure('repair_metrics',error);}catch{}}}
  async lane(q:Query,dirty=false,priority=20): Promise<Lane> {
    const demanded=((await this.store.exec<any>('SELECT at FROM demand WHERE key=?',queryKey(q))).toArray()[0]?.at || 0)>Date.now()-300000;
    if (demanded && !(await this.complete(q))) return 'requested';
    if (dirty || priority<=5 || q.operation.endsWith('_delta')) return 'active';
    return q.operation.startsWith('recent_') || (await this.dormant(q))?'archive':'verification';
  }
  async configure(name:string): Promise<void> {
    if(!this.store.transactional)return this.atomic(state=>RepositoryState.prototype.configure.call(state,name));
    const repo=repository(this.env,name); if (!repo) throw new Error('repository not allowed');
    await this.setMeta('repository',repo);
    for (const operation of ['repository','refs','open_prs','open_issues','recent_prs','recent_issues','pr_delta','issue_delta']) await this.enqueue({repository:repo.name,operation},operation==='open_prs' || operation.endsWith('_delta')?0:10);
    await this.setAlarm(Date.now()+100);
  }
  async recordReviewStart(comment:any,received:number):Promise<void> {
    const marker=String(comment.body || '').match(/<!--tauceti-review-in-progress (.*?)-->/s);
    if(!marker || !comment.id)return;
    // ingest() already deduplicates GitHub delivery GUIDs, including redeliveries.
    // A cached/deleted comment may precede its create webhook; it must not hide a start.
    let data:any;try{data=JSON.parse(marker[1]);}catch{data={};}
    const now=Date.now(),hour=3600000,history=await this.meta('review_coordination') || {observed_since:now,buckets:{}};
    const parsed=Date.parse(comment.created_at || '');const at=Number.isFinite(parsed)?parsed:received;
    if(at<now-7*86400000 || at>now+300000)return;
    const key=String(Math.floor(at/hour)*hour),bucket=history.buckets[key] || {dual:0,legacy:0,unknown:0,git_held:0};
    const kind=data.coordination_protocol==='git+comment/v1'?'dual':data.coordination_protocol===undefined && data.schema==='tauceti-review-in-progress/v1'?'legacy':'unknown';
    bucket[kind]++;if(kind==='dual' && data.git_claim===true)bucket.git_held++;
    if(kind==='legacy' && Number.isFinite(data.expires_at))bucket.legacy_until=Math.max(bucket.legacy_until || 0,Math.min(data.expires_at*1000,at+3600000));
    history.buckets[key]=bucket;
    for(const k of Object.keys(history.buckets))if(Number(k)<now-7*86400000-hour)delete history.buckets[k];
    await this.setMeta('review_coordination',history);
  }
  async reviewCoordination():Promise<any> {
    const history=await this.meta('review_coordination');
    return coordinationWindows(history,this[ACTIVITY_CLOCK] ?? Date.now());
  }
  async ingest(pointer:EventPointer,preparedBody?:any): Promise<void> {
    const configured=repository(this.env,pointer.repository);if(!configured)throw Error('repository not allowed');
    if(!this.store.transactional){
      // Archival publishes R2 before deleting SQL. Read SQL first: either its
      // receipt is still present, or the following R2 check can see the archive.
      // Reversing these reads needs another remote lookup under the writer.
      if((await this.store.exec('SELECT id FROM deliveries WHERE id=?',pointer.delivery)).toArray().length)return;
      const receiptStarted=Date.now(),archived=await this.env.ARCHIVE.head(receiptKey(configured.id,pointer.delivery));
      if(Date.now()-receiptStarted>=100)log('repository_receipt_slow',{elapsed_ms:Date.now()-receiptStarted,transactional:false});
      if(archived)return;
      let body:any,encodings=new Map<string,string>();
      if(!CI_EVENTS.has(pointer.event)){
        const object=await this.env.ARCHIVE.get(pointer.key);if(!object)throw Error('event body unavailable');body=parseGitHub(await object.text());
        if(body.comment){const value=comment(body.comment),text=JSON.stringify(value);if(new TextEncoder().encode(text).length>=1500000)encodings.set('resource:'+text,await this.encode(value,undefined,configured.id));}
      }
      return this.atomic(async state=>{state.preparedEncodings=encodings;await RepositoryState.prototype.ingest.call(state,pointer,body);});
    }
    // Another ingest can commit while we preload the body. Its fresh SQL
    // receipt remains retained; no external I/O is needed under the writer.
    if((await this.store.exec('SELECT id FROM deliveries WHERE id=?',pointer.delivery)).toArray().length)return;
    if(this.degraded){if(this.degraded.reason!=='storage_full')throw Error('repository storage control unavailable');throw Error('Exceeded the maximum database size; SQLITE_FULL recorded at '+new Date(this.degraded.since).toISOString());}
    if(CI_EVENTS.has(pointer.event)){await this.store.exec('INSERT OR IGNORE INTO deliveries VALUES(?,?,?)',pointer.delivery,'ignored:'+pointer.event,Date.now());return;}
    const body:any=preparedBody,repo=configured;await this.setMeta('repository',repo);
    const event=pointer.event, deleted=body.action==='deleted';
    // All updates are idempotent; receipt is committed only after their durable rows and jobs.
    if (event==='pull_request' && body.pull_request) {
      const row=pr(body.pull_request), number=String(row.number);
      const before=(await this.store.exec<any>('SELECT data FROM resources WHERE kind=? AND id=?','pr',number)).toArray()[0];
      const prior=before?await this.decode(before.data):null;
      const accepted=await this.put('pr',number,row,pointer.received,false,number,true);
      // A webhook describes one PR, not a complete inventory. Never create fake coverage.
      const key=queryKey({operation:'open_prs'});
      if (accepted) await this.membership(key,number,body.pull_request.state==='open');
      const detail={repository:repo.name,operation:'pr',args:{number:row.number}};
      const detailKnown=(await this.store.exec('SELECT key FROM collections WHERE key=? AND complete=1',queryKey(detail))).toArray().length>0;
      await this.enqueue(detail,0,Date.now(),!detailKnown || row.mergeable==='UNKNOWN');
      const inventoryKnown=(await this.store.exec('SELECT key FROM collections WHERE key=? AND complete=1',key)).toArray().length>0;
      await this.enqueue({repository:repo.name,operation:'open_prs'},1,Date.now(),!inventoryKnown);
      if(accepted && row.headRefOid && prior?.headRefOid!==row.headRefOid) await this.enqueue({repository:repo.name,operation:'files',args:{number:row.number}},1,Date.now(),true);
      // Published mirrors hydrate new open PR histories from trusted webhooks,
      // independently of anonymous query demand.
      if(row.state==='OPEN' && publicQuery(this.env,repo))for(const operation of ['issue_comments','review_threads']) {
        const child={repository:repo.name,operation,args:{number:row.number}};
        if(!await this.complete(child))await this.enqueue(child,2);
      }
      if (accepted && row.headRefOid) for (const operation of ['statuses']) {
        const q={repository:repo.name,operation,args:{sha:row.headRefOid}};
        const known=(await this.store.exec('SELECT key FROM collections WHERE key=? AND complete=1',queryKey(q))).toArray().length>0;
        await this.enqueue(q,1,Date.now(),!known || prior?.headRefOid!==row.headRefOid);
      }
    }
    if (event==='issues' && body.issue) {
      const row=issue(body.issue); const accepted=await this.put('issue',String(row.number),row,pointer.received,deleted,String(row.number),true);
      if (accepted) await this.membership(queryKey({operation:'open_issues'}),String(row.number),!deleted && row.state==='OPEN');
      const q={repository:repo.name,operation:'open_issues'};
      const known=(await this.store.exec('SELECT key FROM collections WHERE key=? AND complete=1',queryKey(q))).toArray().length>0;
      await this.enqueue(q,1,Date.now(),!known);
    }
    if (event==='issue_comment' && body.action==='created' && body.comment) await this.recordReviewStart(body.comment,pointer.received);
    if (['issue_comment','pull_request_review_comment'].includes(event) && body.comment) {
      const operation=event==='issue_comment'?'issue_comments':'review_comments';
      const number=body.issue?.number || body.pull_request?.number;
      const row=comment(body.comment); const accepted=await this.put(operation,String(row.id),row,pointer.received,deleted,String(number),true);
      const key=queryKey({operation,args:{number}});
      if (accepted) await this.membership(key,String(row.id),!deleted);
      const known=(await this.store.exec('SELECT key FROM collections WHERE key=? AND complete=1',key)).toArray().length>0;
      await this.enqueue({repository:repo.name,operation,args:{number}},0,Date.now(),!known);
      if (operation==='review_comments') await this.enqueue({repository:repo.name,operation:'review_threads',args:{number}},2,Date.now(),true);
    }
    if (event==='pull_request_review' || event==='pull_request_review_thread') {
      const number=body.pull_request?.number;
      if (number) for (const operation of ['review_threads','reviews','review_comments']) await this.enqueue({repository:repo.name,operation,args:{number}},1,Date.now(),true);
    }
    if (event==='status' && body.sha && body.context) {
      await this.put('status_context',body.sha+':'+body.context,status(body),pointer.received,false,body.sha,true);
      const q={repository:repo.name,operation:'statuses',args:{sha:body.sha}};
      const known=(await this.store.exec('SELECT key FROM collections WHERE key=? AND complete=1',queryKey(q))).toArray().length>0;
      await this.enqueue(q,0,Date.now(),!known);
    }
    if (['push','create','delete','repository','label','milestone','merge_group'].includes(event)) {
      if (event==='push' && String(body.ref).startsWith('refs/heads/')) {
        const row={ref:body.ref,object:{sha:body.after,type:'commit',url:'https://api.github.com/repos/'+repo.name+'/git/commits/'+body.after},updated_at:new Date(pointer.received).toISOString()};
        const removed=Boolean(body.deleted) || /^0+$/.test(body.after || '');
        if (await this.put('refs',body.ref,row,pointer.received,removed,'',true)) await this.membership(queryKey({operation:'refs'}),body.ref,!removed);
      }
      // Header and CI webhooks already update the indexed records. A single
      // changed PR must not trigger a repository-wide survey at fleet scale.
      for (const operation of ['open_prs','open_issues','refs','repository']) {
        const q={repository:repo.name,operation};
        const known=(await this.store.exec('SELECT key FROM collections WHERE key=? AND complete=1',queryKey(q))).toArray().length>0;
        await this.enqueue(q,2,Date.now(),!known || operation==='repository' && event==='repository' || operation==='refs' && ['create','delete'].includes(event));
      }
    }
    await this.commitReceipt(pointer);
  }
  async commitReceipt(pointer:EventPointer):Promise<void> {
    // Use commit time, not the event's age: writer-side ingest deduplication
    // relies on archival retaining newly committed SQL receipts for 14 days.
    const now=Date.now();
    await this.store.batch([
      {sql:'INSERT OR IGNORE INTO deliveries VALUES(?,?,?)',bindings:[pointer.delivery,pointer.event,now]},
      {sql:'INSERT INTO meta VALUES(?,?),(?,?),(?,?),(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',bindings:[
        'latest_applied_input',JSON.stringify({delivery:pointer.delivery,event:pointer.event,received:pointer.received,applied_at:now}),
        'last_webhook',JSON.stringify(Math.max(pointer.received,(await this.meta('last_webhook')) || 0)),
        'webhook_apply_ms',JSON.stringify(Math.max(0,now-pointer.received)),
        'latest_input_applied_at',JSON.stringify(now)
      ]}
    ]);
  }

  kind(q:Query): string { return ['open_prs','recent_prs','pr','pr_delta'].includes(q.operation)?'pr':['open_issues','recent_issues','issue_delta'].includes(q.operation)?'issue':q.operation; }
  async rows(q:Query): Promise<any[] | any | null> {
    const key=queryKey(q), collection=(await this.store.exec<any>('SELECT * FROM collections WHERE key=? AND complete=1',key)).toArray()[0];
    if (!collection) return null;
    if (q.operation==='pr') {
      const row=(await this.store.exec<any>('SELECT data FROM resources WHERE kind=? AND id=? AND tombstone=0','pr',String(q.args?.number))).toArray()[0];
      if (row) return (await this.decode(row.data));
    }
    if (['pr','repository','statuses'].includes(q.operation)) {
      const row=(await this.store.exec<any>('SELECT data FROM resources WHERE kind=? AND id=? AND tombstone=0',q.operation,key)).toArray()[0];
      if (!row) return null;
      const data=await this.decode(row.data);
      if (q.operation!=='statuses' || !Array.isArray(data)) return data;
      const contexts=new Map(data.map(n=>{const value=status(n);return [value.context,value];}));
      for (const row of await this.scanRows('SELECT data FROM resources WHERE kind=? AND parent=? AND tombstone=0','status_context',String(q.args?.sha))) {
        const value=status(await this.decode(row.data)),prior=contexts.get(value.context);
        if (!prior || Date.parse(value.startedAt)>=Date.parse(prior.startedAt)) contexts.set(value.context,value);
      }
      return [...contexts.values()];
    }
    const members=await this.scanRows(`SELECT r.data FROM members m ${collectionJoin(this.store)} resources r ON r.kind=? AND r.id=m.id WHERE m.collection=? AND r.tombstone=0 ORDER BY CAST(r.id AS INTEGER),r.id`,this.kind(q),key);
    const rows=[];
    if(q.operation==='open_prs')await this.primeRelated([],members.map(r=>JSON.parse(r.data).headRefOid).filter(Boolean));
    for await(const data of this.decodeRows(members)) {
      if (q.operation==='open_prs' || q.operation==='open_issues') {
        if (data.state && data.state!=='OPEN') continue;
      }
      if (q.operation==='open_prs') {
        const statuses=await this.rows({repository:q.repository,operation:'statuses',args:{sha:data.headRefOid}});
        data.statusCheckRollup=Array.isArray(statuses)?statuses:[];
      }
      rows.push(data);
    }
    return rows;
  }
  async unavailable(q:Query,reason:string): Promise<Result> {
    return {version:1,repository:q.repository,operation:q.operation,status:'unavailable',revision:(await this.revision()),complete:false,reason};
  }
  async changedQuery(q:Query,after:number):Promise<boolean> {
    const number=q.args?.number,sha=q.args?.sha;
    let relevant:string,keys=[queryKey(q)],bindings:(string|number)[];
    if(number!==undefined && ['pr','progress','issue_comments','review_comments','files'].includes(q.operation)) {
      const parent=String(number);
      const children=q.operation==='progress'?['issue_comments','review_comments']
        :q.operation==='pr'?[]:[q.operation];
      if(q.operation==='progress')keys=['pr','issue_comments','review_comments'].map(operation=>queryKey({...q,operation}));
      const parts:string[]=[];bindings=[];
      if(['pr','progress'].includes(q.operation)){
        parts.push("(c.kind='pr' AND c.id IN (?,?))");bindings.push(parent,queryKey({...q,operation:'pr'}));
      }
      if(children.length){parts.push('(c.kind IN ('+children.map(()=>'?').join(',')+') AND r.parent=?)');bindings.push(...children,parent);}
      relevant=parts.join(' OR ');
    } else if(q.operation==='statuses' && typeof sha==='string') {
      relevant="(c.kind='statuses' AND c.id=?) OR (c.kind='status_context' AND r.parent=?)";bindings=[queryKey(q),sha];
    } else return true; // Other query shapes retain the repository-wide retry.
    // The retained change log identifies the rows/collection read by this PR.
    // Unrelated webhooks cannot invalidate its snapshot or force full history
    // decoding again. Real edits, membership changes and tombstones still retry.
    return (await this.store.exec(
      "SELECT 1 FROM changes c LEFT JOIN resources r ON r.kind=c.kind AND r.id=c.id WHERE c.revision>? AND ((c.kind='collection' AND c.id IN ("+keys.map(()=>'?').join(',')+')) OR '+relevant+') LIMIT 1',
      after,...keys,...bindings,
    )).toArray().length>0;
  }
  async stableQuery(q:Query,readOnly=false):Promise<Result> {
    if(q.cursor)return this.query(q,readOnly);
    // Inventories discover candidate work. Repeating a full inventory whenever any
    // webhook advances the repository revision makes a busy repository appear
    // unavailable. Selected-PR reads check the revisions of their own inputs.
    if(['survey','open_prs','open_issues'].includes(q.operation))return this.query(q,readOnly);
    for(let attempt=0;attempt<3;attempt++) {
      const result=await this.query(q,readOnly);
      const row=(await this.store.exec<any>("SELECT value FROM meta WHERE key='revision'")).toArray()[0];
      const revision=row?JSON.parse(row.value):0;
      this.revisionState.value=Math.max(this.revisionState.value ?? 0,revision);
      if(result.revision===revision || !await this.changedQuery(q,result.revision))return result;
    }
    return this.unavailable(q,'concurrent_input');
  }
  async query(q:Query,readOnly=false): Promise<Result> {
    if(!this.readScope) {
      const scoped=Object.create(this) as RepositoryState;if(this.store.backend==='postgres')scoped.store=new ReadCacheStore(this.store);scoped.readScope=true;scoped.readOnlyQuery=readOnly;scoped.readRevision=await this.revision();
      return scoped.query(q,readOnly);
    }
    validateQuery(q);
    if (NOT_MIRRORED.has(q.operation)) return this.unavailable(q,'not_mirrored');
    if (!(await this.meta('repository'))) {
      if(this.readOnlyQuery || this.store.backend==='postgres')return this.unavailable(q,'repository_unconfigured');
      try {await this.configure(q.repository);}
      catch(error) {return (await this.unavailable(q,this.noteFailure('configure',error)));}
    }
    const key=queryKey(q);
    if (q.cursor) return this.continuation(q,key);
    if (!q.allow_observed && !['survey','progress'].includes(q.operation))await this.noteReadDemand(q);
    let rows:any;
    let author_counts:Record<string,number> | undefined;
    if(q.operation==='survey') {
      const snapshot=await this.surveyRows(q);rows=snapshot?.rows ?? null;author_counts=snapshot?.author_counts;
    } else if (q.operation==='progress') {
      const detail={...q,operation:'pr'};
      const p=await this.rows(detail);
      await this.noteReadDemand(detail,p!==null);
      if(!(await this.complete(detail))) await this.optionalAsync('enqueue',async ()=>(await this.enqueue(detail,5)));
      const cs=await this.rows({...q,operation:'issue_comments'}), rcs=await this.rows({...q,operation:'review_comments'});
      for (const operation of ['issue_comments','review_comments']) {
        const child={...q,operation};
        await this.noteReadDemand(child);
        if (!(await this.complete(child))) await this.optionalAsync('enqueue',async ()=>(await this.enqueue(child,5)));
      }
      rows=p && cs && rcs ? {head:p.headRefOid,state:p.state,ncomments:cs.length+rcs.length,revision:(await this.revision())}:null;
    } else {
      if (!q.allow_observed && !(await this.complete(q))) await this.optionalAsync('enqueue',async ()=>(await this.enqueue(q,5)));
      rows=await this.rows(q);
    }
    const complete=rows!==null && (q.operation==='survey' || q.operation==='progress' || (await this.complete(q)));
    const {observed,verified}=(await this.freshness(q));
    const result:Result={version:1,repository:q.repository,operation:q.operation,status:rows===null?'unavailable':'ok',revision:(await this.revision()),observed_at:observed || verified,verified_at:verified,complete,coverage:complete?'complete':q.allow_observed?'webhook_observed':undefined};
    if(q.operation==='survey') {result.contract=SURVEY_CONTRACT;result.author_counts=author_counts;}
    if (rows===null) return {...result,reason:'coverage_unavailable'};
    // Public reads use the same stable pagination as authenticated reads.
    // Only temporary cursor objects may be written; repository hydration and
    // demand remain disabled in this read scope.
    if (Array.isArray(rows) && rows.length>100) {
      const id=crypto.randomUUID(),expires=Date.now()+600000,repo=(await this.meta('repository')).id;
      // Continuation pages go directly to private R2, never into SQLite. Each
      // page is separate, so continuation never decodes the whole inventory,
      // and every page retains the initial snapshot revision.
      const pages:{offset:number,page:Result}[]=[];
      for (let offset=0;offset<rows.length;) {
        const page=this.page(result,rows,id,offset);
        pages.push({offset,page});
        offset+=Array.isArray(page.data)?page.data.length:0;
      }
      const pending=pages.slice(1);
      const upload=async()=>{
        for (let next=pending.shift();next;next=pending.shift()) {
          const body=JSON.stringify({repository:repo,key,allow_observed:Boolean(q.allow_observed),expires,result:next.page});
          await this.env.ARCHIVE.put(cursorKey(repo,id,next.offset),body,{customMetadata:{kind:'cursor',expires:String(expires+60000)}});
        }
      };
      await Promise.all(Array.from({length:Math.min(CURSOR_UPLOADS,pending.length)},upload));
      return pages[0].page;
    }
    return {...result,data:rows};
  }
  // Derived results report the stored times of the collections they read:
  // the newest observation and the oldest complete verification.
  async freshness(q:Query): Promise<{observed?:number,verified?:number}> {
    const args=q.args,number=q.args?.number;
    const sources=q.operation==='survey'?[{key:queryKey({operation:'open_prs'}),kind:'pr'}]
      :q.operation==='progress'?[{key:queryKey({operation:'pr',args}),kind:'pr',id:String(number)},{key:queryKey({operation:'issue_comments',args}),kind:'issue_comments'},{key:queryKey({operation:'review_comments',args}),kind:'review_comments'}]
      :[{key:queryKey(q),kind:this.kind(q)}];
    let observed:number | undefined,verified:number | undefined;
    for (const source of sources as {key:string,kind:string,id?:string}[]) {
      const at=source.id?(await this.store.exec<any>('SELECT observed at FROM resources WHERE kind=? AND id=?',source.kind,source.id)).toArray()[0]?.at
        :(await this.store.exec<any>(`SELECT MAX(r.observed) at FROM members m ${collectionJoin(this.store)} resources r ON r.kind=? AND r.id=m.id WHERE m.collection=?`,source.kind,source.key)).one().at;
      const checked=(await this.store.exec<any>('SELECT verified FROM collections WHERE key=?',source.key)).toArray()[0]?.verified;
      if (at) observed=Math.max(observed ?? 0,at);
      if (checked) verified=verified===undefined?checked:Math.min(verified,checked);
    }
    return {observed,verified};
  }
  async continuation(q:Query,key:string): Promise<Result> {
    const cursor=q.cursor!,now=Date.now();
    const repo=(await this.meta('repository'))?.id;
    const match=cursor.match(/^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):([1-9][0-9]{0,8})$/);
    const object=match?await this.env.ARCHIVE.get(cursorKey(repo,match[1],Number(match[2]))):null;
    let payload:any;
    if(object) {
      payload=await object.json();
      if(payload.repository!==repo || !(payload.expires>now))return this.unavailable(q,'cursor_expired');
    }else {
      if(this.store.backend!=='postgres')return this.unavailable(q,'cursor_expired');
      // SQL fallback retains pre-recovery cursors, including UUID-shaped IDs.
      const row=(await this.store.exec<any>('SELECT payload FROM cursors WHERE (id=? OR id=?) AND expires>? ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END LIMIT 1',cursor,cursor.split(':')[0],now,cursor)).toArray()[0];
      if(!row)return this.unavailable(q,'cursor_expired');
      payload=await this.decode(row.payload);
    }
    if (payload.key!==key) return (await this.unavailable(q,'cursor_query_mismatch'));
    if (Boolean(payload.allow_observed)!==Boolean(q.allow_observed)) return (await this.unavailable(q,'cursor_coverage_mismatch'));
    // HTTP cursors and migration shadow reads share this path. Retained pages
    // keep their original revision/timestamps; only the wire DTO is normalized.
    const compatible=(result:Result):Result=>q.operation==='survey' && Array.isArray(result.data)
      ? {...result,data:result.data.map(row=>({...row,review:{...row.review,markers:[]}}))}:result;
    if (payload.result?.data!==undefined) return compatible(payload.result);
    // Existing ten-minute cursors survive a rolling deployment. Only an offset
    // inside the stored snapshot can continue it; never fabricate an empty page.
    const legacy=cursor.match(/^([^:]+):([1-9][0-9]{0,8})$/),offset=Number(legacy?.[2]),id=legacy?.[1] || '';
    if (!legacy || !Array.isArray(payload.rows) || offset>=payload.rows.length) return (await this.unavailable(q,'cursor_invalid'));
    return compatible(this.page(payload.result,payload.rows,id,offset));
  }
  page(result:Result,rows:any[],id:string,offset:number): Result {
    // Keep the number of client round trips bounded as inventories grow.
    // A byte bound avoids making comment-heavy pages arbitrarily large.
    let end=offset, bytes=0;
    while (end<rows.length && end<offset+500) {
      const size=new TextEncoder().encode(JSON.stringify(rows[end])).length;
      if (end>offset && bytes+size>1490000) break;
      bytes+=size; end++;
    }
    return {...result,data:rows.slice(offset,end),...(end<rows.length?{cursor:id+':'+end}:{})};
  }
  async interval(q:Query): Promise<number> {
    if (q.operation==='pr_delta' || q.operation==='issue_delta') return 60000;
    if (q.operation==='open_prs' || q.operation==='open_issues') return 3600000;
    if (q.operation==='files') return 365*86400000; // head-change webhook dirties this immutable-by-head read
    if (q.operation==='reactions') {
      const active=(await this.store.exec<any>('SELECT at FROM demand WHERE key=?',queryKey(q))).toArray()[0];
      return active?.at>Date.now()-300000?300000:86400000;
    }
    if (q.operation==='reviews') return 6*3600000;
    const demand=(await this.store.exec<any>('SELECT at FROM demand WHERE key=?',queryKey(q))).toArray()[0];
    if (q.operation==='statuses') return demand?.at>Date.now()-300000?300000:3600000;
    if (['issue_comments','review_comments','review_threads','reviews','reactions'].includes(q.operation)) return demand?.at>Date.now()-300000?300000:3600000;
    return 3600000;
  }
  async priority(q:Query): Promise<number> {
    if (q.operation.endsWith('_delta')) return 0;
    const demand=(await this.store.exec<any>('SELECT at FROM demand WHERE key=?',queryKey(q))).toArray()[0];
    if (demand?.at>Date.now()-300000) return 5;
    if (['open_prs','open_issues','repository','refs'].includes(q.operation)) return 10;
    return 20;
  }
  async noteDemand(q:Query): Promise<void> {
    if (q.allow_observed || q.operation==='survey') return;
    const key=queryKey(q),now=Date.now();
    const prior=(await this.store.exec<any>('SELECT at FROM demand WHERE key=?',key)).toArray()[0];
    // Demand stays hot for five minutes. Repeated cached reads need at most
    // one refresh per minute, not a write transaction (and SQLite input gate)
    // per reader. Check again inside the transaction to coalesce racing reads.
    if(prior?.at>now-60000 && await this.complete(q))return;
    if(!this.store.transactional)return this.atomic(state=>RepositoryState.prototype.noteDemand.call(state,q));
    await this.store.exec('INSERT INTO demand VALUES(?,?) ON CONFLICT(key) DO UPDATE SET at=excluded.at',key,now);
    const complete=(await this.store.exec('SELECT key FROM collections WHERE key=? AND complete=1',key)).toArray().length>0;
    if (!complete && q.operation!=='progress') {
      await this.store.exec("UPDATE jobs SET lane='requested',requested_at=COALESCE(requested_at,?),due=CASE WHEN wait_reason IS NULL AND attempt=0 THEN MIN(due,?) ELSE due END WHERE key=?",now,now,key);
    }
    if (prior?.at>now-300000) return;
    if (complete && q.operation!=='progress' && !(await this.store.exec('SELECT key FROM jobs WHERE key=?',key)).toArray().length) await this.enqueue(q,20,now+(await this.interval(q)));
    // Becoming active advances a quiet audit to its hot interval. A client
    // still receives existing data immediately; actual quota waits stay intact.
    await this.store.exec("UPDATE jobs SET due=MIN(due,?),priority=5,lane=CASE WHEN ? THEN 'verification' ELSE 'requested' END WHERE key=? AND attempt=0 AND started IS NULL AND wait_reason IS NULL",complete?now+(await this.interval(q)):now,Number(complete),key);
    const due=(await this.store.exec<any>('SELECT due FROM jobs WHERE key=?',key)).toArray()[0]?.due;
    const alarm=this.store.backend==='postgres'?null:await this.ctx.storage.getAlarm();
    if (due && (!alarm || alarm>due)) await this.setAlarm(Math.max(now+100,due));
  }
  async nextJob(): Promise<any> {
    if(!this.store.transactional)return this.atomic(state=>RepositoryState.prototype.nextJob.call(state));
    const now=Date.now(),turn=(await this.meta('repair_turn')) || 0;
    await this.setMeta('repair_turn',turn+1);
    const preferred=laneAt(turn,settings(this.env).weights);
    return (await this.store.exec<any>('SELECT * FROM jobs WHERE due<=? AND lease_until<=? ORDER BY CASE WHEN lane=? THEN 0 WHEN lane=\'requested\' THEN 1 WHEN lane=\'active\' THEN 2 WHEN lane=\'verification\' THEN 3 ELSE 4 END,due,key LIMIT 1',now,now,preferred)).toArray()[0];
  }
  async dormant(q:Query): Promise<boolean> {
    const demand=(await this.store.exec<any>('SELECT at FROM demand WHERE key=?',queryKey(q))).toArray()[0];
    if (demand?.at>Date.now()-300000) return false;
    let numbers:string[]=[];
    if (q.args?.number) numbers=[String(q.args.number)];
    else if (q.operation==='reactions') {
      const row=(await this.store.exec<any>('SELECT parent,tombstone FROM resources WHERE kind=? AND id=?','review_comments',String(q.args?.id))).toArray()[0];
      if (row?.tombstone) return true;
      if (row?.parent) numbers=[row.parent];
    } else if (q.operation==='statuses') {
      const counts=(await this.store.exec<any>("SELECT COUNT(*) total,SUM(CASE WHEN json_extract(r.data,'$.state') IN ('CLOSED','MERGED') THEN 1 ELSE 0 END) closed FROM head_prs h LEFT JOIN resources r ON r.kind='pr' AND r.id=h.number WHERE h.sha=?",String(q.args?.sha))).one();
      return counts.total>0 && counts.closed===counts.total;
    }
    if (!numbers.length) return false;
    return (await Promise.all(numbers.map(async number=>{
      const row=(await this.store.exec<any>("SELECT data FROM resources WHERE kind IN ('pr','issue') AND id=? ORDER BY kind DESC LIMIT 1",number)).toArray()[0];
      const data=row?JSON.parse(row.data):null;
      return Boolean(data?.state && data.state!=='OPEN');
    }))).every(Boolean);
  }
  async finishJob(job:any,q:Query,etag?:string): Promise<void> {
    if(!this.store.transactional)return this.atomic(state=>RepositoryState.prototype.finishJob.call(state,job,q,etag));
    const current=(await this.store.exec<any>('SELECT generation,priority,requested_at,lane FROM jobs WHERE key=?',job.key)).toArray()[0];
    const again=current?.generation>(job.start_generation || 0);
    // A newer webhook can arrive while pages are being fetched. Completing the
    // old collection must not erase that event's pending repair.
    await this.metric('completion',q,current?.lane || job.lane || 'verification');
    const requested=job.requested_at || current?.requested_at;
    if (requested) {
      await this.metric('requested_latency_ms',q,'requested',Date.now()-requested);
      if(job.page===1 && !job.nested) await this.metric('requested_single_latency_ms',q,'requested',Date.now()-requested);
    }
    await this.store.exec('UPDATE jobs SET due=?,priority=?,lane=?,dirty=?,started=NULL,cursor=NULL,page=1,api=NULL,etag=?,attempt=0,wait_reason=NULL,nested=NULL,requested_at=? WHERE key=?',again?Date.now():Date.now()+(await this.interval(q)),again?current.priority:(await this.priority(q)),(await this.lane(q,again)),Number(again),etag || null,again?current?.requested_at || null:null,job.key);
    if (!again && (await this.dormant(q))) await this.store.exec('DELETE FROM jobs WHERE key=?',job.key);
  }
  async nextRefreshCursor(q:Query,result:any,started:number):Promise<string|undefined>{
    let next=result.cursor;
    if(['pr_delta','issue_delta'].includes(q.operation) && Array.isArray(result.data)){
      const watermark=(await this.meta(q.operation+'_watermark')) || started;
      if(result.data.some((r:any)=>Date.parse(r.updatedAt)<watermark-300000))next=undefined;
    }
    if(q.operation.startsWith('recent_') && Array.isArray(result.data) && result.data.some((r:any)=>Date.parse(r.updatedAt)<Date.now()-30*86400000))next=undefined;
    return next;
  }
  async refresh(job:any,supplied?:any): Promise<void> {
    const repo=(await this.meta('repository')), q:Query=JSON.parse(job.query);
    const started=job.started || Date.now();
    if (!job.started) {
      job.start_generation=job.generation || 0;
      await this.store.exec('UPDATE jobs SET started=?,start_generation=? WHERE key=?',started,job.start_generation,job.key);
    }
    const upstream=q;
    let nested:any;
    if(job.nested)try {nested=await this.decode(job.nested);}catch(error) {
      if(error instanceof Error && error.message==='resource unavailable')throw new Error('staging page missing');
      throw error;
    }
    const response=supplied?null:await stub(this.env.INSTALLATIONS,repo.installation).fetch('https://internal/read',{method:'POST',headers:{'x-request-id':crypto.randomUUID()},body:JSON.stringify({repository:repo,query:upstream,cursor:job.cursor,page:job.page,api:job.api,nested,etag:job.page===1 && !job.nested && q.operation!=='workflow_runs'?job.etag:undefined})});
    const result:any=supplied?{...supplied}:await response!.json();
    this.traceRepair('refresh_response',job);
    const objects=new Map<string,string>(),encodings=new Map<string,string>();
    const prefix=`staging/${repo.id}/${encodeURIComponent(job.key)}/${started}/`;
    const publish=(!response || response.ok) && !result.continuation && !result.restart && !result.deferred && !result.unavailable && !result.error && !result.unchanged && result.data!==undefined;
    const preparedNext=publish?await this.nextRefreshCursor(q,result,started):undefined;
    const checkpoint=publish?await this.meta(stagingStateKey(job.key)):undefined;
    // Checkpointed pages are immutable. Load them before taking the publication
    // lock; the lease is checked again inside the transaction before use.
    if(publish && !preparedNext){
      this.traceRepair('publication_load_pages',job);
      const pages=Array.from({length:Math.max(0,(job.page || 1)-1)},(_,i)=>i+1);
      for(let offset=0;offset<pages.length;offset+=8)await Promise.all(pages.slice(offset,offset+8).map(async page=>{const object=await this.env.ARCHIVE.get(stagedPageKey(checkpoint,prefix+page,started,page));if(!object)throw Error('staging page missing');objects.set(prefix+page,await object.text());}));
    }
    let staged:{key:string,expires:number}|undefined;
    if(publish){const text=JSON.stringify(result.data);staged={key:prefix+job.page+'-'+crypto.randomUUID(),expires:Date.now()+86400000};await this.env.ARCHIVE.put(staged.key,text,{customMetadata:{expires:String(staged.expires)}});objects.set(prefix+job.page,text);}
    const sizes=this.env.RECONCILIATION_TRACE==='true'?{staged_chars:[...objects.values()].reduce((n,text)=>n+text.length,0)}:undefined;
    this.traceRepair('publication_encode_pages',job,sizes);
    for(const text of objects.values()){
      const data=JSON.parse(text),values=Array.isArray(data)?data:[data];
      for(const value of values){const body=JSON.stringify(value);if(new TextEncoder().encode(body).length>=1500000)encodings.set('resource:'+body,await this.encode(value));}
    }
    if(result.continuation){const text=JSON.stringify(result.continuation);if(new TextEncoder().encode(text).length>=1500000)encodings.set('staging:'+text,await this.encode(result.continuation,Date.now()+86400000));}
    this.traceRepair('refresh_transaction',job,sizes);
    try {await this.atomic(state=>{
      state.preparedEncodings=encodings;
      const archive=state.env.ARCHIVE;
      state.env={...state.env,ARCHIVE:new Proxy(archive,{get(target,key){if(key==='get')return async(name:string,...args:any[])=>{
        if(!objects.has(name))return (target.get as any)(name,...args);
        // Publication consumes each prepared page once; the immutable R2 key remains durable for retry.
        const consume=()=>{const text=objects.get(name)!;objects.delete(name);return text;};
        return {text:async()=>consume(),json:async()=>JSON.parse(consume())};
      };const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;}})};
      return RepositoryState.prototype.applyRefresh.call(state,job,q,repo,started,response,result,preparedNext,staged);
    });this.traceRepair('refresh_committed',job);}
    catch(error){if(!(error instanceof LeaseLost))throw error;}
  }
  async applyRefresh(job:any,q:Query,repo:any,started:number,response:Response|null,result:any,preparedNext:string|undefined,staged:{key:string,expires:number}|undefined):Promise<void> {
    if (job.lease_id && (await this.store.exec<any>('SELECT lease_id FROM jobs WHERE key=?',job.key)).toArray()[0]?.lease_id!==job.lease_id) throw new LeaseLost();
    if (result.continuation) {
      const nested=await this.encode(result.continuation,Date.now()+86400000);
      if (job.lease_id && (await this.store.exec<any>('SELECT lease_id FROM jobs WHERE key=?',job.key)).toArray()[0]?.lease_id!==job.lease_id) throw new LeaseLost();
      await this.store.exec('UPDATE jobs SET nested=?,api=?,due=?,wait_reason=NULL WHERE key=?',nested,result.api,Date.now(),job.key);
      await this.metric('page',q,job.lane || 'verification');return;
    }
    if (result.restart) {
      await this.store.exec('UPDATE jobs SET started=NULL,cursor=NULL,page=1,api=?,etag=NULL,due=? WHERE key=?',result.api,Date.now(),job.key);
      return;
    }
    if (response && !response.ok || result.deferred || result.unavailable) {
      const capacity=result.category==='capacity';
      if(q.operation==='review_comments' && result.deferred && String(result.category).startsWith('primary')) {
        // The existing thread reconciler supplies the same flat history. Preserve
        // the REST cooldown while promoting that dependency in the other pool.
        const source={...q,operation:'review_threads'};
        const existing=(await this.store.exec<any>('SELECT started,dirty FROM jobs WHERE key=?',queryKey(source))).toArray()[0];
        await this.noteDemand(source);await this.enqueue(source,1,Date.now(),!existing || !existing.started && !existing.dirty);
        if(!(await this.complete(q))) await this.store.exec("UPDATE jobs SET lane='requested',requested_at=COALESCE(requested_at,?) WHERE key=?",job.requested_at || Date.now(),queryKey(source));
      }
      await this.store.exec('UPDATE jobs SET due=?,attempt=attempt+?,wait_reason=? WHERE key=?',result.retry_at || Date.now()+Math.min(3600000,60000*2**Math.min(6,job.attempt)),capacity?0:1,result.category || result.reason || 'upstream',job.key);
      await this.metric(capacity?'capacity_wait':'retry',q,job.lane || 'verification');
      if(!capacity) await this.setMeta('last_upstream_failure',{at:Date.now(),operation:q.operation,category:result.category || result.reason || 'upstream',request_id:result.request_id}); return;
    }
    if (result.unchanged) {
      await this.store.exec('DELETE FROM meta WHERE key=?',stagingStateKey(job.key));
      await this.store.exec('UPDATE collections SET verified=? WHERE key=?',Date.now(),job.key);
      await this.finishJob(job,q,job.etag); return;
    }
    if (result.api!==job.api && job.page>1 && job.api) {
      // REST pages and GraphQL cursors cannot be mixed; restart this staged collection.
      await this.store.exec('UPDATE jobs SET started=NULL,cursor=NULL,page=1,api=NULL WHERE key=?',job.key); return;
    }
    if(!staged)throw Error('malformed service response');
    const previous=await this.meta(stagingStateKey(job.key));
    const pages=previous?.version===1 && previous.started===started?{...previous.pages}:{};
    pages[String(job.page)]={key:staged.key,expires:staged.expires};
    await this.setMeta(stagingStateKey(job.key),{version:1,started,pages});
    if (job.lease_id && (await this.store.exec<any>('SELECT lease_id FROM jobs WHERE key=?',job.key)).toArray()[0]?.lease_id!==job.lease_id) throw new LeaseLost();
    await this.store.exec('UPDATE jobs SET nested=NULL,wait_reason=NULL WHERE key=?',job.key);
    await this.metric('page',q,job.lane || 'verification');
    const next=preparedNext;
    if (next) {
      if (next===job.cursor) throw new Error('repeated upstream cursor');
      await this.store.exec('UPDATE jobs SET cursor=?,page=page+1,api=?,due=?,etag=? WHERE key=?',next,result.api,Date.now(),result.etag || job.etag,job.key); return;
    }
    const rows:any[]=[]; let scalar:any;
    this.traceRepair('publication_decode_pages',job);
    for (let page=1;page<=job.page;page++) {
      const object=await this.env.ARCHIVE.get(`staging/${repo.id}/${encodeURIComponent(job.key)}/${started}/${page}`);
      if (!object) throw new Error('staging page missing');
      const text=await object.text();
      const data:any=JSON.parse(text); if (Array.isArray(data)) rows.push(...data); else scalar=data;
    }
    this.traceRepair('publication_rows',job,{rows:rows.length});
    const publicationStore=this.store;
    if(this.store.backend==='postgres')this.store=new ReadCacheStore(publicationStore);
    const kind=this.kind(q), memberIDs=new Set<string>(), acceptedIDs=new Set<string>();
    const changedNumbers=new Set<number>(), changedHeads=new Set<number>();
    if (scalar!==undefined || ['pr','repository','statuses'].includes(q.operation)) {
      await this.put(q.operation,job.key,scalar===undefined?rows:scalar,started);
      if (q.operation==='pr' && scalar?.number) await this.put('pr',String(scalar.number),scalar,started,false,String(scalar.number));
    } else {
      for await (const row of this.publicationRows(q,rows,publicationStore)) {
        if (q.operation.startsWith('recent_') && Date.parse(row.updatedAt)<Date.now()-30*86400000) continue;
        const id=q.operation==='files'?String(q.args?.number)+':'+String(row.filename):this.id(q.operation,row); memberIDs.add(id);
        if (kind==='pr') {
          const prior=(await this.store.exec<any>('SELECT data,updated,observed FROM resources WHERE kind=? AND id=?','pr',id)).toArray()[0];
          const before=prior?await this.decode(prior.data):null;
          if ((!prior || prior.observed<=started) && (!before || before.updatedAt!==row.updatedAt)) changedNumbers.add(row.number);
          if ((!prior || prior.observed<=started) && (!before || before.headRefOid!==row.headRefOid)) changedHeads.add(row.number);
        }
        if (await this.put(kind,id,row,started,false,String(q.args?.number || q.args?.id || q.args?.sha || ''))) acceptedIDs.add(id);
      }
      // Keep newer webhook membership when the reconciliation began before that event.
      const prior=await this.scanRows('SELECT m.id,r.observed FROM members m LEFT JOIN resources r ON r.kind=? AND r.id=m.id WHERE m.collection=?',kind,job.key);
      // A REST offset can shift when another PR closes between pages. Absence
      // from that traversal cannot delete a previously observed open resource;
      // webhooks and delta surveys supply actual state changes instead.
      if(this.store instanceof ReadCacheStore)for(const row of prior)this.store.seed('SELECT id FROM members WHERE collection=? AND id=?',[job.key,row.id],[{id:row.id}]);
      const openInventory=['open_prs','open_issues'].includes(q.operation);
      for (const row of prior) if (!(openInventory && result.api==='core') && !memberIDs.has(row.id) && row.observed<=started) await this.membership(job.key,row.id,false);
      if(openInventory){
        const decided=new Set<string>();
        // Decide once per ID from the final stored resource, after every put.
        for await(const row of this.publicationRows(q,rows,publicationStore)){
          const id=this.id(q.operation,row);if(decided.has(id))continue;decided.add(id);
          const current=(await this.store.exec<any>('SELECT data,tombstone FROM resources WHERE kind=? AND id=?',kind,id)).toArray()[0];
          if(current && (current.tombstone || (await this.decode(current.data)).state!=='OPEN'))continue;
          await this.membership(job.key,id,true);
        }
      }else{
        if(this.store instanceof ReadCacheStore){const known=new Set(prior.map(row=>row.id));for(const id of memberIDs)this.store.seed('SELECT id FROM members WHERE collection=? AND id=?',[job.key,id],known.has(id)?[{id}]:[]);}
        for(const id of memberIDs)await this.membership(job.key,id,true);
      }
    }
    if (job.lease_id && (await this.store.exec<any>('SELECT lease_id FROM jobs WHERE key=?',job.key)).toArray()[0]?.lease_id!==job.lease_id) throw new LeaseLost();
    if (!(await this.store.exec('SELECT key FROM collections WHERE key=? AND complete=1',job.key)).toArray().length) await this.nextRevision('collection',job.key);
    await this.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET complete=1,verified=excluded.verified,revision=excluded.revision,etag=excluded.etag',job.key,q.operation,JSON.stringify(q.args || {}),1,Date.now(),(await this.revision()),job.page===1?result.etag || null:null);
    await this.store.exec('DELETE FROM meta WHERE key=?',stagingStateKey(job.key));
    await this.finishJob(job,q,job.page===1?result.etag || null:null);
    // These paths now need only repair identities. Release payload strings
    // before building a large fan-out, while keeping detail/thread rows until
    // their final publication pass has consumed them.
    const identity=(row:any)=>({number:row.number,id:row.id,state:row.state,updatedAt:row.updatedAt,headRefOid:row.headRefOid});
    if(!['open_prs','review_threads'].includes(q.operation)){
      for(let i=0;i<rows.length;i++)rows[i]=identity(rows[i]);
      result.data=undefined;scalar=undefined;this.preparedEncodings?.clear();
    }
    const deltaRepairs:{q:Query,priority:number,dirty?:boolean}[]=[];
    if (['pr_delta','issue_delta'].includes(q.operation)) {
      const operation=q.operation==='pr_delta'?'open_prs':'open_issues', inventory=queryKey({operation});
      for await (const row of this.publicationRows(q,rows,publicationStore)) {
        const id=String(row.number);
        if (acceptedIDs.has(id)) await this.membership(inventory,id,row.state==='OPEN');
        if (changedNumbers.has(row.number)) {
          deltaRepairs.push({q:{repository:repo.name,operation:'issue_comments',args:{number:row.number}},priority:2,dirty:true});
          if (operation==='open_prs') for (const op of ['review_threads','pr']) deltaRepairs.push({q:{repository:repo.name,operation:op,args:{number:row.number}},priority:3,dirty:true});
          if (changedHeads.has(row.number)) deltaRepairs.push({q:{repository:repo.name,operation:'files',args:{number:row.number}},priority:3,dirty:true});
        }
      }
      await this.setMeta(q.operation+'_watermark',started);
    }
    const repairs:{q:Query,priority:number,dirty?:boolean}[]=deltaRepairs;
    if (q.operation==='open_prs' || q.operation==='recent_prs') {
      let published=0;
      for await (const row of this.publicationRows(q,rows,publicationStore,q.operation==='recent_prs')) {
        if (q.operation==='recent_prs' && Date.parse(row.updatedAt)<Date.now()-30*86400000) continue;
        for (const operation of ['issue_comments','review_threads','reviews','files']) repairs.push({q:{repository:repo.name,operation,args:{number:row.number}},priority:20});
        if (q.operation==='open_prs') {
          const itemKey=queryKey({operation:'pr',args:{number:row.number}});
          await this.put('pr',itemKey,row,started);
          await this.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET complete=1,verified=excluded.verified,revision=excluded.revision',itemKey,'pr',JSON.stringify({number:row.number}),1,Date.now(),(await this.revision()),null);
        }
        if (q.operation==='open_prs' && row.headRefOid) for (const operation of ['statuses']) repairs.push({q:{repository:repo.name,operation,args:{sha:row.headRefOid}},priority:2});
        if(q.operation==='open_prs')rows[published++]=identity(row);
      }
      if(q.operation==='open_prs') await this.setMeta('bootstrap_inventory',Date.now());
    }
    if (q.operation==='open_issues') for (const row of rows) repairs.push({q:{repository:repo.name,operation:'issue_comments',args:{number:row.number}},priority:20});
    if (q.operation==='review_comments') for (const row of rows) repairs.push({q:{repository:repo.name,operation:'reactions',args:{id:String(row.id)}},priority:25});
    if (q.operation==='review_threads') {
      // This complete thread reconciliation also supplies the flat REST-shaped history.
      const comments=rows.flatMap(t=>t.comments.nodes.map(comment));
      rows.length=0;result.data=undefined;this.preparedEncodings?.clear();
      const flatKey=queryKey({operation:'review_comments',args:q.args}), ids=new Set<string>();
      const flatQuery={...q,operation:'review_comments'};
      let published=0;
      for await (const c of this.publicationRows(flatQuery,comments,publicationStore)) {
        ids.add(String(c.id)); await this.put('review_comments',String(c.id),c,started,false,String(q.args?.number));
        repairs.push({q:{repository:repo.name,operation:'reactions',args:{id:String(c.id)}},priority:25});
        comments[published++]=undefined;
      }
      const prior=await this.scanRows('SELECT m.id,r.observed FROM members m LEFT JOIN resources r ON r.kind=? AND r.id=m.id WHERE m.collection=?','review_comments',flatKey);
      if(this.store instanceof ReadCacheStore)for(const row of prior)this.store.seed('SELECT id FROM members WHERE collection=? AND id=?',[flatKey,row.id],[{id:row.id}]);
      for (const row of prior) if (!ids.has(row.id) && row.observed<=started) await this.membership(flatKey,row.id,false);
      if(this.store instanceof ReadCacheStore){const known=new Set(prior.map(row=>row.id));for(const id of ids)this.store.seed('SELECT id FROM members WHERE collection=? AND id=?',[flatKey,id],known.has(id)?[{id}]:[]);}
      for (const id of ids) await this.membership(flatKey,id,true);
      if (!(await this.store.exec('SELECT key FROM collections WHERE key=? AND complete=1',flatKey)).toArray().length) await this.nextRevision('collection',flatKey);
      await this.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET complete=1,verified=excluded.verified,revision=excluded.revision',flatKey,'review_comments',JSON.stringify(q.args),1,Date.now(),(await this.revision()),null);
      const dependent=(await this.store.exec<any>('SELECT * FROM jobs WHERE key=? AND wait_reason LIKE ?',flatKey,'primary%')).toArray()[0];
      if(dependent) {
        await this.finishJob({...dependent,start_generation:dependent.generation},{...q,operation:'review_comments'});
        await this.store.exec('UPDATE jobs SET lease_id=NULL,lease_until=0 WHERE key=?',flatKey);
      }
    }
    // Fan-out retains only compact query identities, never the decoded inventory.
    rows.length=0;result.data=undefined;scalar=undefined;this.preparedEncodings?.clear();
    this.traceRepair('publication_repairs',job,{repairs:repairs.length});
    await this.enqueueMany(repairs);
    await this.setMeta('last_repair',Date.now());
    this.traceRepair('publication_applied',job);
  }
  // Expiry only deletes, which still succeeds at the storage limit. It runs
  // before repair, hydration or snapshots, any of which can fail first.
  async expire(): Promise<Record<string,number>> {return this.owner.retentionLane.run(()=>this.expireTurn());}
  private async expireTurn(): Promise<Record<string,number>> {
    const out:Record<string,number>={},now=Date.now();
    const budget={rows:6000,bytes:4*1024**2,deadline:now+(this.store.backend==='postgres'?5000:50)};
    if(!this.storageLoaded || this.migrationHeld())return out; // Unknown pins and source fences fail closed.
    const names=Object.keys(RULES) as RetentionClass[];
    for(let i=0;i<names.length;i++) {
      const name=names[(i+this.storageTurn)%names.length];
      if(name==='expired_legacy_cursors' && this.store.backend==='sqlite')continue;
      if(name==='expired_deliveries')continue; // Async receipt archival owns this class.
      if(this.retentionPolicy.disabled.includes(name) || this.retentionPolicy.pinned.includes(name))continue;
      if(budget.rows<=0 || budget.bytes<=0 || Date.now()>=budget.deadline)break;
      const prior=this.retentionState[name];
      if(prior && prior.after>=prior.upper && prior.completed && now-prior.completed<60000)continue;
      // Preserve S0's emergency expiry; new classes start as a dry run.
      const enforce=this.retentionPolicy.mode==='enforce' || name!=='expired_demand';
      try {
        const next=this.store.backend==='postgres'?await sweepStore(this.store,name,prior,budget,enforce,now):sweep(this.sql,name,prior,budget,enforce,now);
        this.retentionState[name]=next;out[name]=next.deleted-(next.started===prior?.started?(prior?.deleted || 0):0);
      } catch(error) {
        this.noteFailure('expire:'+name,error);
        this.retentionState[name]={...(prior || {after:0,upper:0,started:now,examined:0,eligible:0,deleted:0,bytes:0}),error:classify(error)};
      }
    }
    this.storageTurn=(this.storageTurn+1)%names.length;
    return out;
  }
  // Same retention class/pins and bounded rowid checkpoint as ordinary
  // expiry, but SQL receipt deletion must follow durable R2 archival.
  async archiveDeliveryReceipts():Promise<void> {return this.owner.retentionLane.run(()=>this.archiveDeliveryReceiptTurn());}
  private async archiveDeliveryReceiptTurn():Promise<void> {
    const name='expired_deliveries',now=Date.now(),repo=(await this.meta('repository'))?.id;
    if(!repo || !this.storageLoaded || this.migrationHeld() || this.mutationState.blocked || this.retentionPolicy.disabled.includes(name) || this.retentionPolicy.pinned.includes(name))return;
    const previous=this.retentionState[name];if(previous?.completed && previous.after>=previous.upper && now-previous.completed<60000)return;
    const state:Checkpoint=previous && previous.after<previous.upper?{...previous}:{after:0,upper:(await this.store.exec<any>('SELECT COALESCE(MAX(rowid),0) n FROM deliveries')).one().n,started:now,examined:0,eligible:0,deleted:0,bytes:0};delete state.error;
    try{
      const size=this.store.backend==='postgres'?"octet_length(id)+octet_length(event)+8":"length(CAST(id AS BLOB))+length(CAST(event AS BLOB))+8";
      const rows=(await this.store.exec<any>('SELECT rowid rid,id,event,received,'+size+' bytes FROM deliveries WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT 100',state.after,state.upper)).toArray();let bytes=0;
      for(let offset=0;offset<rows.length && Date.now()-now<2000;offset+=10){
        const group:any[]=[];for(const row of rows.slice(offset,offset+10)){if(bytes+row.bytes>4*1024**2)break;bytes+=row.bytes;group.push(row);}if(!group.length)break;
        const results=await Promise.allSettled(group.map(async row=>{if(row.received>=now-14*86400000)return false;await this.maintenanceArchive.put(receiptKey(repo,row.id),JSON.stringify({delivery:row.id,committed_at:row.received,disposition:String(row.event).startsWith('ignored:')?'not_mirrored':'applied'}),{onlyIf:{etagDoesNotMatch:'*'}});return true;}));
        if(this.migrationHeld() || this.mutationState.blocked || this.retentionPolicy.disabled.includes(name) || this.retentionPolicy.pinned.includes(name))break;
        const expired=group.filter((row,i)=>results[i].status==='fulfilled' && (results[i] as PromiseFulfilledResult<boolean>).value);
        const removed=expired.length?(await this.store.exec<any>('DELETE FROM deliveries WHERE (rowid,id) IN ('+expired.map(()=>'(?,?)').join(',')+') AND received<? RETURNING rowid',...expired.flatMap(row=>[row.rid,row.id]),now-14*86400000)).toArray():[];
        state.deleted+=removed.length;
        for(let i=0;i<group.length;i++){
          const row=group[i],result=results[i];if(result.status==='rejected'){state.error=classify(result.reason);this.noteFailure('receipt_archive',result.reason);}
          if(row.received<now-14*86400000){state.eligible++;state.bytes+=row.bytes;state.oldest_expired=Math.min(state.oldest_expired ?? row.received,row.received);}state.after=row.rid;state.examined++;
        }
      }
      if(!rows.length || state.after>=state.upper)state.completed=now;
    }catch(error){state.error=classify(error);this.noteFailure('receipt_archive',error);}
    this.retentionState[name]=state;
  }
  // Cron calls this independently of hydration, dispatch and archive creation.
  // Checkpoints and observations live outside the potentially full SQLite DB.
  async storageMaintenance(runExpiry=true): Promise<void> {
    if(this.migrationHeld() || this.mutationState.blocked)return;
    if(this.storageFlight)return this.storageFlight;
    this.activeOperations++;
    this.traceRepair('storage_prepare');
    const flight=this.prepareStorage(runExpiry);this.storageFlight=flight;
    try {await flight;this.traceRepair('storage_prepared');}finally {this.activeOperations--;if(this.storageFlight===flight)this.storageFlight=undefined;}
  }
  get maintenanceArchive():R2Bucket {return timedBucket(this.env.ARCHIVE,this.owner.maintenanceIOScope);}
  get transactionArchive():R2Bucket {return timedBucket(this.env.ARCHIVE,this.owner.transactionIOScope);}
  startStorageMaintenance():void {
    if(this.migrationHeld() || this.mutationState.blocked)return;
    const task=this.storageMaintenance(false).catch(error=>this.noteFailure('storage_maintenance',error));
    if(this.ctx.waitUntil)this.ctx.waitUntil(task);
  }
  async loadRetentionState():Promise<void> {
    if(this.storageLoaded && this.retentionProgressLoaded)return;
    if(this.retentionLoadFlight)return this.retentionLoadFlight;
    const task=this.readRetentionState();this.retentionLoadFlight=task;
    try {await task;}finally {if(this.retentionLoadFlight===task)this.retentionLoadFlight=undefined;}
  }
  private monitoringRepository():string|undefined {
    try {return JSON.parse(this.env.ALLOWED_REPOSITORIES).find((r:any)=>this.env.REPOSITORIES.idFromName(r.id).toString()===this.ctx.id.toString())?.id;}catch {return undefined;}
  }
  async readRetentionState():Promise<void> {
    let repo:string|undefined;try {repo=this.store.backend==='postgres'?this.monitoringRepository():(await this.meta('repository'))?.id;}catch {return;}if(!repo)return;
    const observedGeneration=this.policyGeneration;
    if(!this.storageLoaded)try {
      const control=await this.maintenanceArchive.get('control/'+repo+'/retention-policy.json').then(o=>o?.json<any>());
      if(!this.storageLoaded && observedGeneration===this.policyGeneration) {
        this.retentionPolicy=control?policy(control):DEFAULT_POLICY;this.storageLoaded=true;
      }
    }catch(error) {this.noteFailure('retention_load',error);}
    // Policy publication can race the initial load. Progress is independent:
    // retain the acknowledged policy while restoring the scan's old checkpoint.
    if(!this.retentionProgressLoaded)try {
      const checkpoint=await this.maintenanceArchive.get('control/'+repo+'/retention-checkpoint.json').then(o=>o?.json<any>());
      if(checkpoint?.version===1 && (this.store.backend==='sqlite'?(!checkpoint.backend || checkpoint.backend==='sqlite'):checkpoint.backend==='postgres' && checkpoint.generation===this.backendGeneration()) && checkpoint.classes && typeof checkpoint.classes==='object') {
        for(const name of Object.keys(RULES) as RetentionClass[]) {
          const row=checkpoint.classes[name];
          if(!this.retentionState[name] && row && ['after','upper','started','examined','eligible','deleted','bytes'].every(k=>Number.isSafeInteger(row[k]) && row[k]>=0) && row.after<=row.upper) {
            this.retentionState[name]={...row,completed:Number.isSafeInteger(row.completed) && row.completed<=Date.now()?row.completed:undefined};
          }
        }
      }
      this.retentionProgressLoaded=true;
    }catch(error) {this.noteFailure('retention_checkpoint_load',error);}
  }

  async prepareStorage(runExpiry=true): Promise<void> {
    const repo=this.store.backend==='postgres'?this.monitoringRepository():(await this.meta('repository'))?.id;if(!repo)return;
    await this.loadRetentionState();
    if(this.migrationHeld())return;
    if(Date.now()-this.receiptsChecked>=60000) {
      try {
        const [parked,held]=await Promise.all([
          this.maintenanceArchive.list({prefix:'parked/'+repo+'/pointers/',limit:1}),
          this.maintenanceArchive.list({prefix:'parked/'+repo+'/held/',limit:1})
        ]);
        this.receiptsPinned=Boolean(parked.objects.length || held.objects.length);
        this.receiptsChecked=Date.now();
      }catch {this.receiptsPinned=true;}
    }
    if(this.migrationHeld())return;
    if(this.storageLoaded){if(runExpiry)await this.expire();await this.archiveDeliveryReceipts().catch(error=>this.noteFailure('receipt_archive',error));}
    if(this.migrationHeld())return;
    try {
      const text=JSON.stringify({version:1,backend:this.store.backend,generation:this.backendGeneration(),classes:this.retentionState});
      if(!this.migrationHeld() && this.storageLoaded && this.retentionProgressLoaded && text!==this.checkpointText && Date.now()-this.checkpointAt>=30000) {
        if(this.store.backend==='sqlite')await this.ctx.storage.sync();
        await this.maintenanceArchive.put('control/'+repo+'/retention-checkpoint.json',text);
        this.checkpointAt=Date.now();this.checkpointText=text;
      }
    }catch(error) {this.noteFailure('retention_checkpoint',error);}
    if(this.migrationHeld())return;
    await this.prepareDiagnostics();
    const now=Date.now();
    if(this.observation && now-this.observation.at<60000)return;
    let ingressHealth:any;
    try {
      // A real tiny write/read tests the parked-ingress storage destination.
      const key='control/'+repo+'/ingress-durability-probe.json',nonce=crypto.randomUUID();
      await this.maintenanceArchive.put(key,nonce);
      if(await this.maintenanceArchive.get(key).then(o=>o?.text())!==nonce)throw new Error('durability_probe_mismatch');
      const configured=repository(this.env,repo);
      ingressHealth={at:now,durable:true,backlog:configured?await backlog({...this.env,ARCHIVE:this.maintenanceArchive},configured,true):null,
        ...await queueMetrics(this.env,now)};
    }catch(error) {ingressHealth={at:now,durable:false,error:classify(error)};}
    if(this.migrationHeld())return;
    if(this.store.backend==='postgres') {
      await this.preparePostgresObservation(repo,ingressHealth,now);return;
    }
    try {
      const sample={at:now,bytes:this.sql.databaseSize,allowance:SQLITE_ALLOWANCE};
      if(!this.capacityHistory.length) {
        const old=await this.maintenanceArchive.get('control/'+repo+'/observation.json').then(o=>o?.json<any>()).catch(()=>undefined);
        if(old?.capacity)this.lastCapacity=old.capacity;
        if(Array.isArray(old?.capacity_history))this.capacityHistory=old.capacity_history;
        else if(old?.capacity)this.capacityHistory=[old.capacity];
        if(old?.tables)this.tableEstimates=old.tables;
        if(old?.table_attempt_at)this.tableAttemptAt=old.table_attempt_at;
      }
      // Logical size sampling/counting is hourly, never a status-read side effect.
      if(now-this.tableAttemptAt>=3600000)try {
        // Persist the attempt before SQL analysis, so an isolate reset cannot
        // repeat the same expensive job on every cold start.
        const attemptKey='control/'+repo+'/table-estimate-attempt.json';
        const last=await this.maintenanceArchive.get(attemptKey).then(o=>o?.json<any>());
        if(last?.at>this.tableAttemptAt)this.tableAttemptAt=last.at;
        if(now-this.tableAttemptAt<3600000)throw new Error('table_estimate_throttled');
        this.tableAttemptAt=now;
        await this.maintenanceArchive.put(attemptKey,JSON.stringify({at:now}));
        if(this.migrationHeld())return;
        const previous=this.tableEstimates;
        const tables=summaryTables(this.sql).map(name=>{
          const low=this.sql.exec<any>('SELECT rowid rid FROM "'+name+'" ORDER BY rowid LIMIT 1').toArray()[0]?.rid;
          const high=this.sql.exec<any>('SELECT rowid rid FROM "'+name+'" ORDER BY rowid DESC LIMIT 1').toArray()[0]?.rid;
          const span=low===undefined?0:high-low+1;
          const page=scan(this.sql,{table:name,limit:100});
          const count=page.rows?Math.round(span*page.rows/(page.last_rowid-page.first_rowid+1)):0;
          const estimate=page.rows?Math.round(page.bytes/page.rows*count):0;
          const old=previous?.values?.find((r:any)=>r.name===name);
          return {name,rows_estimate:count,rowid_span:span,sampled_rows:page.rows,logical_bytes_estimate:estimate,growth_bytes_per_second:old && now>previous.at?(estimate-old.logical_bytes_estimate)/((now-previous.at)/1000):null};
        });
        this.tableEstimates={at:now,method:'rowid_span_times_first_100_row_density_and_logical_mean; excludes internal metadata/free pages',values:tables};
      }catch(error) {if(!(error instanceof Error && error.message==='table_estimate_throttled'))this.noteFailure('table_estimates',error);}
      const r2Retention=Object.fromEntries(await Promise.all(Object.keys(R2_CLASSES).map(async name=>{
        const progress=await this.maintenanceArchive.get('control/retention/'+name+'.json').then(o=>o?.json<any>()).catch(()=>undefined);
        return [name,{...(progress || {}),stale:!progress || now-progress.at>180000}];
      })));
      if(this.migrationHeld())return;
      this.capacityHistory=this.capacityHistory.filter(r=>r.at>=now-2*3600000 && r.at<now).slice(-120);
      const baseline=this.capacityHistory.filter(r=>r.at<=now-3600000).at(-1);
      const measured=capacity(sample,baseline,now,this.lastCapacity);this.lastCapacity=measured;
      this.capacityHistory.push(sample);
      const reasons:string[]=[];
      if(measured.level!=='ok')reasons.push('storage_capacity');
      if(this.degraded && now-this.degraded.since>=60000)reasons.push('input_application_unavailable');
      if(!ingressHealth.durable)reasons.push('parked_ingress_durability_unavailable');
      if(ingressHealth.backlog?.held?.count>0 || ingressHealth.backlog?.parked?.oldest_received && now-ingressHealth.backlog.parked.oldest_received>=86400000)reasons.push('input_application_backlog');
      const level=reasons.some(r=>r!=='storage_capacity')?'critical':measured.level;
      const requests=this.requestMetrics.snapshot(now),repairMinutes=this.repairMetrics.snapshot(now).filter((r:any)=>r.at>=now-60000);
      const {intervals,...requestSummary}=requests;
      const value={version:1,at:now,level,reasons,capacity:measured,capacity_history:this.capacityHistory,tables:this.tableEstimates,table_attempt_at:this.tableAttemptAt,ingress:ingressHealth,refresh_lag_ms:(await this.meta('last_repair'))?now-(await this.meta('last_repair')):null,requests:requestSummary,repair_summary:this.repairMetrics.summary(now),provider:await providerObservation(this.env,now),
        retention:{policy:this.retentionPolicy,policy_loaded:this.storageLoaded,classes:this.retentionState,receipts_pinned:this.receiptsPinned,receipt_pin_backend:'r2',r2:r2Retention},
        compute:{status:'unavailable',reason:'not_exposed_by_sqlite_runtime'},connections:{status:'not_applicable'}};
      const alarm=level!=='ok'?emitAlarm(this.env,repo,value):{channel:'configured_workers_observability',emitted:false,notified:false};
      this.observation={...value,alarm};
      this.observation.persistence={stored:true,at:now};
      try {await this.maintenanceArchive.put('control/'+repo+'/observation.json',JSON.stringify(this.observation));}
      catch(error) {this.observation.persistence={stored:false,at:now,error:classify(error)};this.noteFailure('observation_persistence',error);}
      if(this.migrationHeld())return;
      if(requests.requests || repairMinutes.length)await this.maintenanceArchive.put('telemetry/'+repo+'/'+Math.floor(now/60000)+'.json',JSON.stringify({at:now,requests:intervals.filter((r:any)=>r.at>=now-60000),repair_intervals:repairMinutes}),{customMetadata:{expires:String(now+7*86400000)}});
    }catch(error) {this.noteFailure('monitoring',error);}
  }
  private async preparePostgresObservation(repo:string,ingress:any,now:number):Promise<void> {
    const generation=this.backendGeneration();
    if(!this.capacityHistory.length) {
      const old=await this.maintenanceArchive.get('control/'+repo+'/observation.json').then(o=>o?.json<any>()).catch(()=>null);
      if(old?.backend==='postgres' && old.generation===generation) {
        this.lastCapacity=old.capacity;
        if(Number.isSafeInteger(old.at) && now-old.at>=0 && now-old.at<=180000) {
          this.postgresUnavailableSince=old.database_unavailable_since;this.monitoringFailureSince=old.monitoring_failure_since;
        }
        if(Array.isArray(old.capacity_history))this.capacityHistory=old.capacity_history.filter((r:any)=>Number.isSafeInteger(r.at) && Number.isSafeInteger(r.bytes) && r.at<=now && r.at>now-2*3600000).slice(-121);
      }
    }
    let preparationError:string|undefined;
    try {
      // Record the expensive attempt before calling SQL: a cancelled SQL
      // transaction must not repeat table/index analysis every minute.
      const key='control/'+repo+'/postgres-table-attempt-'+generation+'.json';
      let analyze=false,token=Date.now();
      try {
        const prior=await this.maintenanceArchive.get(key).then(o=>o?.json<any>());
        if(!prior || !Number.isSafeInteger(prior.at) || prior.at>token+60000 || prior.at<=token-3600000 || prior.not_started===true && prior.retry_at<=token) {
          token=Date.now();await this.maintenanceArchive.put(key,JSON.stringify({at:token}));analyze=true;
        }
      }catch { /* Unknown attempt state skips expensive work, not minute gauges. */ }
      const prepared=await this.store.prepareMonitoring?.(analyze,token);
      if(analyze && prepared?.analysis_attempted===false)await this.maintenanceArchive.put(key,JSON.stringify({at:token,not_started:true,retry_at:Math.max(Date.now()+60000,prepared.table_retry_at || 0)})).catch(error=>this.noteFailure('postgres_monitoring_checkpoint',error));
    }catch(error) {preparationError=classify(error);this.noteFailure('postgres_monitoring_prepare',error);}
    if(this.migrationHeld())return;
    const storage=await this.store.health(),stats:any=storage.capacity;
    if(storage.connected===false)this.postgresUnavailableSince ??= now;else this.postgresUnavailableSince=undefined;
    this.capacityHistory=this.capacityHistory.filter(r=>r.at>=now-2*3600000 && r.at<now).slice(-120);
    const baseline=this.capacityHistory.filter(r=>r.at<=now-3600000).at(-1);
    const sampleValid=Number.isSafeInteger(stats?.prepared_at) && stats.prepared_at<=Date.now()+60000;
    const fresh=sampleValid && (!this.lastCapacity || stats.prepared_at>this.lastCapacity.at);
    const measured=fresh?postgresCapacity(stats,baseline,now,this.lastCapacity):{...(this.lastCapacity || postgresCapacity(stats,baseline,now)),stale:!sampleValid || now-stats.prepared_at>180000};
    if(fresh){this.lastCapacity=measured;this.capacityHistory.push({at:measured.at,bytes:measured.bytes,allowance:measured.allowance});}
    if(!storage.connected)measured.stale=true;
    if(preparationError)this.monitoringFailureSince ??= now;else this.monitoringFailureSince=undefined;
    const reasons:string[]=[];
    if(preparationError==='backend_fenced' || storage.connected && (storage.generation!==generation || storage.backend!=='postgres' || storage.schema_version!==1))reasons.push('backend_generation_fenced');
    if(measured.stale || this.monitoringFailureSince!==undefined && now-this.monitoringFailureSince>=180000)reasons.push('monitoring_unavailable');
    if(this.inputFailure && now-this.inputFailure.at>180000)this.inputFailure=undefined;
    if(this.inputFailure && this.inputFailure.at-this.inputFailure.since>=60000)reasons.push('input_application_unavailable');
    if(measured.level && measured.level!=='ok')reasons.push('storage_capacity');
    if(this.postgresUnavailableSince!==undefined && now-this.postgresUnavailableSince>=60000)reasons.push('input_application_unavailable');
    if(!ingress.durable)reasons.push('parked_ingress_durability_unavailable');
    const recovery=ingress.backlog;
    if(recovery?.held?.count>0 || recovery?.parked?.oldest_received && now-recovery.parked.oldest_received>=86400000)reasons.push('input_application_backlog');
    if(this.migrationHeld())return;
    const connections=stats?.observation?.connections;
    const usable=connections?connections.max-connections.reserved:0;
    const pressure=usable>0?connections.used/usable:null;
    if(pressure!==null && pressure>=.7)reasons.push('connection_saturation');
    const provider=await providerObservation(this.env,Date.now(),true),providerAlarm=providerAlarms(provider);
    const critical=reasons.some(r=>!['storage_capacity','connection_saturation','monitoring_unavailable'].includes(r)) || measured.level==='critical' || pressure!==null && pressure>=.85 || providerAlarm.level==='critical';
    const uniqueReasons=[...new Set([...reasons,...providerAlarm.reasons])];
    const level=critical?'critical':uniqueReasons.length?'warning':'ok';
    const requests=this.requestMetrics.snapshot(now),{intervals,...requestSummary}=requests;
    const value={version:1,at:now,backend:'postgres',generation,level,reasons:uniqueReasons,storage:{...storage,observed_at:now,stale:measured.stale},database_unavailable_since:this.postgresUnavailableSince,monitoring_failure_since:this.monitoringFailureSince,capacity:measured,capacity_history:this.capacityHistory,
      preparation_error:preparationError,monitoring_stale:measured.stale,table_row_estimates:stats?.observation?.table_row_estimates ?? null,tables:{at:stats?.tables_prepared_at ?? null,attempt_at:stats?.tables_attempt_at ?? null,values:stats?.tables ?? null,stale:!stats?.tables_prepared_at || now-stats.tables_prepared_at>2*3600000},
      connections:{...connections,ratio:pressure,connected:storage.connected,failures:storage.connection_failures,stale:!stats?.prepared_at || now-stats.prepared_at>180000},
      provider,
      compute:provider.configured?{...stats?.observation?.compute,...provider.compute,sql_at:stats?.prepared_at ?? null,sql_stale:!stats?.prepared_at || now-stats.prepared_at>180000}:{...(stats?.observation?.compute || {cpu_utilization:null,reason:'use_neon_compute_metrics'}),stale:!stats?.prepared_at || now-stats.prepared_at>180000},
      ingress,requests:requestSummary,repair_summary:this.repairMetrics.summary(now),
      refresh_lag_ms:this.diagnostics.last_repair?now-this.diagnostics.last_repair:null,
      latest_applied_input:this.diagnostics.latest_applied_input ?? null,
      input_failure:this.inputFailure ?? null,
      recovery:{prepared_at:recovery?.prepared_at ?? null,last_attempt:recovery?.last_attempt ?? null,next_retry:recovery?.next_retry ?? null,last_error:recovery?.last_error ?? null},
      retention:{policy:this.retentionPolicy,policy_loaded:this.storageLoaded,classes:this.retentionState,receipts_pinned:this.receiptsPinned,receipt_pin_backend:'r2'}};
    const alarm=level!=='ok'?emitAlarm(this.env,repo,value):{channel:'configured_workers_observability',emitted:false,notified:false};
    this.observation={...value,alarm,persistence:{stored:true,at:now}};
    try {await this.maintenanceArchive.put('control/'+repo+'/observation.json',JSON.stringify(this.observation));}
    catch(error){this.observation.persistence={stored:false,at:now,error:classify(error)};this.noteFailure('observation_persistence',error);}
    if(this.migrationHeld())return;
    const repair=this.repairMetrics.snapshot(now).filter((r:any)=>r.at>=now-60000);
    if(requests.requests || repair.length)await this.maintenanceArchive.put('telemetry/'+repo+'/'+Math.floor(now/60000)+'.json',JSON.stringify({at:now,requests:intervals.filter((r:any)=>r.at>=now-60000),repair_intervals:repair}),{customMetadata:{expires:String(now+7*86400000)}}).catch(error=>this.noteFailure('telemetry_persistence',error));
  }
  async preparedObservation(): Promise<any> {
    if(!this.observation && Date.now()-this.observationReadAt>=60000) {
      this.observationReadAt=Date.now();
      let repo:string|undefined;try {repo=this.store.backend==='postgres'?this.monitoringRepository():(await this.meta('repository'))?.id;}catch {}
      if(repo) {
        const task=this.maintenanceArchive.get('control/'+repo+'/observation.json').then(o=>o?.json<any>()).then(value=>{
          if(value && (this.store.backend!=='postgres' || value.backend==='postgres' && value.generation===this.backendGeneration()) && (!this.observation || value.at>this.observation.at))this.observation=value;
        }).catch(()=>{});
        if(this.ctx.waitUntil)this.ctx.waitUntil(task);
        // A cold status response stays fast even when the observation store is slow.
        await within(task,100).catch(()=>{});
      }
    }
    const {capacity_history,...publicObservation}=this.observation || {};
    if(publicObservation.provider) {
      publicObservation.provider=ageProvider(publicObservation.provider);
      if(this.store.backend==='postgres' && publicObservation.provider.configured)publicObservation.compute={...publicObservation.compute,...publicObservation.provider.compute};
    }
    if(publicObservation.compute && 'sql_at' in publicObservation.compute)publicObservation.compute={...publicObservation.compute,sql_stale:!Number.isSafeInteger(publicObservation.compute.sql_at) || Date.now()-publicObservation.compute.sql_at>180000};
    const stale=!this.observation || Date.now()-this.observation.at>180000 || Boolean(this.observation.monitoring_stale);
    return {...publicObservation,...(publicObservation.storage?{storage:{...publicObservation.storage,observed_at:this.observation.at,stale}}:{}),stale,age_ms:this.observation?Date.now()-this.observation.at:null};
  }
  async maintenance(): Promise<void> {
    if(!this.degraded) {
      try {
        const pending=(await this.store.exec<any>('SELECT m.id FROM members m LEFT JOIN survey_hydrated h ON h.number=m.id LEFT JOIN survey_hydration_failures f ON f.number=m.id WHERE m.collection=? AND (h.contract IS NULL OR h.contract!=?) AND (f.retry_at IS NULL OR f.retry_at<=?) LIMIT 20',queryKey({operation:'open_prs'}),SURVEY_INDEX_VERSION,Date.now())).toArray();
        for(const row of pending) {
          try {await this.hydrateSurvey(row.id);}catch {await this.invalidateSurvey(row.id);}
        }
        for(const row of (await this.store.exec<any>('SELECT number FROM survey_dirty LIMIT 20')).toArray()) {
          try {await this.rebuildSurvey(row.number);}catch {await this.invalidateSurvey(row.number);await this.store.exec('DELETE FROM survey_dirty WHERE number=?',row.number);}
        }
      } catch(error) {this.noteFailure('survey_hydration',error);}
    }
  }
  async retireUnneeded(job:any): Promise<boolean> {
    if(!this.store.transactional)return this.atomic(state=>RepositoryState.prototype.retireUnneeded.call(state,job));
    const q:Query=JSON.parse(job.query);
    if (!NOT_MIRRORED.has(q.operation) && (job.dirty || !(await this.dormant(q)) || !(await this.complete(q)))) return false;
    const removed=(await this.store.exec('DELETE FROM jobs WHERE key=? AND COALESCE(generation,0)=? AND lease_until<=? AND COALESCE(started,0)=? AND dirty=? RETURNING key',job.key,job.generation || 0,Date.now(),job.started || 0,job.dirty || 0)).toArray();
    if(!removed.length)return false;
    await this.store.exec('DELETE FROM meta WHERE key=?',stagingStateKey(job.key));
    await this.metric('cancellation',q,job.lane);return true;
  }
  async claim(job:any): Promise<any> {
    if(!this.store.transactional)return this.atomic(state=>RepositoryState.prototype.claim.call(state,job));
    const now=Date.now(),lease=crypto.randomUUID(),started=job.started || now,generation=job.started?job.start_generation:job.generation;
    await this.store.exec('UPDATE jobs SET lease_id=?,lease_until=?,started=?,start_generation=? WHERE key=? AND lease_until<=? AND COALESCE(generation,0)=? AND COALESCE(started,0)=?',lease,now+settings(this.env).lease_ms,started,generation,job.key,now,job.generation || 0,job.started || 0);
    return (await this.store.exec<any>('SELECT * FROM jobs WHERE key=? AND lease_id=?',job.key,lease)).toArray()[0];
  }
  async retryJob(job:any,error:unknown): Promise<void> {
    if(!this.store.transactional)return this.atomic(state=>RepositoryState.prototype.retryJob.call(state,job,error));
    const expired=error instanceof Error && /staging page missing/.test(error.message);
    const category=expired?'staging_expired':'transport_or_parser';
    const delay=Math.min(3600000,60000*2**Math.min(job.attempt || 0,6));
    await this.store.exec('UPDATE jobs SET due=?,attempt=attempt+1,wait_reason=?'+(expired?',started=NULL,cursor=NULL,page=1,api=NULL,etag=NULL,nested=NULL':'')+' WHERE key=? AND lease_id=?',Date.now()+delay,category,job.key,job.lease_id);
    await this.metric('retry',JSON.parse(job.query),job.lane);
    await this.setMeta('last_upstream_failure',{at:Date.now(),operation:JSON.parse(job.query).operation,category});
  }
  async dispatch(first:any): Promise<void> {
    this.traceRepair('dispatch',first);
    const q:Query=JSON.parse(first.query),operation=q.operation,repo=(await this.meta('repository'));
    let jobs=[first];
    try {
      if (['issue_comments','review_threads','statuses','reactions'].includes(operation) && first.page===1 && !first.api && !first.nested) {
        const candidates=(await this.store.exec<any>('SELECT * FROM jobs WHERE due<=? AND lease_until<=? AND lane=? AND page=1 AND api IS NULL AND nested IS NULL AND key LIKE ? ORDER BY due,key LIMIT ?',Date.now(),Date.now(),first.lane,operation+':%',Math.max(0,batchLimit(operation)-1))).toArray();
        for (const item of candidates) {
          if ((await this.retireUnneeded(item))) continue;
          const claimed=(await this.claim(item));if (claimed) jobs.push(claimed);
        }
        if (operation==='reactions') {
          const supported=[];
          for (const item of jobs) {
            const query=JSON.parse(item.query),row=(await this.store.exec<any>('SELECT data FROM resources WHERE kind=? AND id=? AND tombstone=0','review_comments',String(query.args.id))).toArray()[0];
            const data=row?await this.decode(row.data):null;
            if (data?.node_id) supported.push({...item,node_id:data.node_id});
          }
          if (supported.length!==jobs.length) { await this.refresh(first);return; }
          jobs=supported;
        }
        const response=await stub(this.env.INSTALLATIONS,repo.installation).fetch('https://internal/batch',{method:'POST',headers:{'x-request-id':crypto.randomUUID()},body:JSON.stringify({repository:repo,jobs:jobs.map(j=>({key:j.key,query:JSON.parse(j.query),node_id:j.node_id}))})});
        const result:any=await response.json();
        if (result.results) {
          for(let i=0;i<jobs.length;i++) {
            try {await this.refresh(jobs[i],result.results[i] || {unavailable:true});}
            catch(error) {await this.retryJob(jobs[i],error);}
          }
        } else if (result.deferred && operation!=='review_threads' && result.category!=='capacity' && !String(result.category).startsWith('secondary')) {
          // Retry REST-capable operations in their other pool on the next quantum.
          for(const item of jobs) await this.store.exec('UPDATE jobs SET api=?,due=? WHERE key=? AND lease_id=?','core',Date.now(),item.key,item.lease_id);
        } else for(const item of jobs) await this.refresh(item,result.deferred || result.unavailable?result:{unavailable:true,reason:'malformed_service_response'});
      } else await this.refresh(first);
    } catch(error) {
      for(const item of jobs) await this.retryJob(item,error);
    } finally {
      for(const item of jobs) await this.store.exec('UPDATE jobs SET lease_id=NULL,lease_until=0 WHERE key=? AND lease_id=?',item.key,item.lease_id);
      this.traceRepair('dispatch_complete',first);
    }
  }
  async alarm(): Promise<void> {
    if(this.owner.alarmFlight)return this.owner.alarmFlight;
    const flight=this.runSingleAlarm();this.owner.alarmFlight=flight;
    try{await flight;}finally{if(this.owner.alarmFlight===flight)this.owner.alarmFlight=undefined;}
  }
  async runSingleAlarm(): Promise<void> {
    if(this.migrationUnavailable) await this.retryMigrationControl();
    if(this.repairsHeld()) return;
    this.activeOperations++;
    try {await this.runAlarm();} finally {this.activeOperations--;}
  }
  async runAlarm(): Promise<void> {
    const options=settings(this.env),began=Date.now();let dispatched=0,retired=0;
    try {
      this.traceRepair('expire');
      await this.expire();
      this.traceRepair('select_jobs');
      if(this.schema!=='current' && !this.degraded) this.startup();
      // Every dispatch writes its lease before any GitHub request, so a full
      // store stops here without spending upstream quota.
      if(!this.degraded && typeof this.schema!=='object') try {
        while(dispatched<options.dispatches && Date.now()-began<options.launch_ms && !this.degraded && !this.migrationHeld()) {
          const wave:Promise<void>[]=[];
          while(wave.length<options.concurrency && dispatched<options.dispatches) {
            const selected=await this.atomic(async state=>{const next=await state.nextJob();if(!next)return undefined;if(await state.retireUnneeded(next))return {retired:true,job:undefined};return {retired:false,job:await state.claim(next)};});
            if(!selected)break;
            if(selected.retired){if(++retired>=200)break;continue;}
            const job=selected.job;if(!job)continue;
            dispatched++;wave.push(this.dispatch(job));
          }
          if (!wave.length) break;
          await Promise.allSettled(wave);
        }
      } catch(error) {this.noteFailure('repair_dispatch',error);}
      this.traceRepair('maintenance');
      try {if(!this.migrationHeld()) await this.maintenance();} catch(error) {this.noteFailure('maintenance',error);}
    } finally {
      try {
        const row=(await this.store.exec<any>('SELECT MIN(MAX(due,lease_until)) AS due FROM jobs')).one();
        // Degraded mode writes nothing, not even the next alarm; the cron tick
        // keeps expiring rows until a probe restores writes.
        if (row.due && !this.degraded && !this.migrationHeld()) await this.setAlarm(Math.max(Date.now()+100,row.due));
      } catch(error) {this.noteFailure('alarm',error);}
      this.traceRepair('storage_maintenance');
      this.startStorageMaintenance();
      this.traceRepair('alarm_complete');
    }
  }
  async refreshActivityLandings(): Promise<void> {
    const configured=await this.meta('repository'),receiptURL=configured?.bors_receipts_url;
    if(!receiptURL && !(configured?.name==='TauCetiProject/TauCeti' && this.env.BORS_ACTIVITY))return;
    if(receiptURL && (!receiptURL.startsWith('https://') || new URL(receiptURL).username || new URL(receiptURL).password))throw Error('Bors receipts require an approved HTTPS URL');
    const now=Date.now(),token=crypto.randomUUID();
    const reserved=await this.atomic(async state=>{
      if((await state.meta('activity_landings_transport'))==='service-binding-v2' && now-((await state.meta('activity_landings_checked')) || 0)<300000)return false;
      await state.setMeta('activity_landings_checked',now);await state.setMeta('activity_landings_transport','service-binding-v2');await state.setMeta('activity_landings_token',token);return true;
    });
    if(!reserved)return;
    let receipts:Awaited<ReturnType<typeof readPromotionReceipts>>=[],failure:string|null=null;
    try{receipts=await readPromotionReceipts((url,options)=>this.env.BORS_ACTIVITY?this.env.BORS_ACTIVITY.fetch(url,options):fetch(url,options),receiptURL,configured.name,configured.branch || 'main');}
    catch(error){failure='Stored Bors promotion receipts unavailable: '+(error instanceof Error?error.name+': '+error.message.slice(0,160):'unknown error');}
    await this.atomic(async state=>{
      if(await state.meta('activity_landings_token')!==token)return;
      if(!failure){
        for(const r of receipts)await state.store.exec('INSERT INTO activity_landings VALUES(?,?,?,?) ON CONFLICT(number,head) DO UPDATE SET at=excluded.at,source=excluded.source WHERE excluded.at<activity_landings.at',r.number,r.head,r.at,r.source);
        await state.setMeta('activity_landings_success',now);
      }
      await state.setMeta('activity_landings_error',failure);
    });
  }
  async primeActivity(numbers:number[]):Promise<RepositoryState> {
    if(this.store.backend!=='postgres')return this;
    const state=Object.create(this) as RepositoryState,cache=new ReadCacheStore(this.store);state.store=cache;state.readScope=true;
    for(let offset=0;offset<numbers.length;offset+=128) {
      const ns=numbers.slice(offset,offset+128),ids=ns.map(String),keys=ns.map(number=>queryKey({operation:'files',args:{number}}));
      const [resources,collections,loc]=await cache.batch([
        {sql:"SELECT id,data,observed FROM resources WHERE kind='pr' AND tombstone=0 AND id IN ("+ids.map(()=>'?').join(',')+')',bindings:ids},
        {sql:'SELECT key,complete,revision FROM collections WHERE key IN ('+keys.map(()=>'?').join(',')+')',bindings:keys},
        {sql:'SELECT number,fingerprint,data FROM activity_loc_cache WHERE number IN ('+ids.map(()=>'?').join(',')+')',bindings:ids}
      ]);
      const decoded=new Map<string,any>();
      for(const row of resources.toArray())decoded.set(row.id,await this.decode(row.data));
      const heads=ns.flatMap(number=>decoded.has(String(number))?[String(number),decoded.get(String(number)).headRefOid || '']:[]);
      const landings=heads.length?(await cache.exec('SELECT number,head,at,source FROM activity_landings WHERE (number,head) IN ('+Array.from({length:heads.length/2},()=>'(?,?)').join(',')+')',...heads)).toArray():[];
      for(const number of ns) {
        const id=String(number),found=resources.toArray().filter(r=>r.id===id),key=queryKey({operation:'files',args:{number}}),collection=collections.toArray().filter(r=>r.key===key),p=decoded.get(id);
        cache.seed("SELECT data,observed FROM resources WHERE kind='pr' AND id=? AND tombstone=0",[id],found);
        cache.seed('SELECT complete,revision FROM collections WHERE key=?',[key],collection);
        if(p){
          cache.seed('SELECT at,source FROM activity_landings WHERE number=? AND head=?',[number,p.headRefOid || ''],landings.filter(r=>r.number===id && r.head===p.headRefOid));
          const fingerprint=activityFingerprint(p,collection[0]?.revision);
          cache.seed('SELECT data FROM activity_loc_cache WHERE number=? AND fingerprint=?',[number,fingerprint],loc.toArray().filter(r=>r.number===id && r.fingerprint===fingerprint));
        }
      }
      // Reuse already decoded PR metadata, including occasional R2-backed rows.
      const decode=state.decode.bind(state),byText=new Map(resources.toArray().map(r=>[r.data,decoded.get(r.id)]));
      state.decode=async text=>byText.has(text)?byText.get(text):decode(text);
    }
    return state;
  }
  async activityLOC(number:number,p:any,budget?:{remaining:number}): Promise<any> {
    const key=queryKey({operation:'files',args:{number}});
    const complete=(await this.store.exec<any>('SELECT complete,revision FROM collections WHERE key=?',key)).toArray()[0];
    if(!complete?.complete || !p.headRefOid || !Number.isSafeInteger(p.changedFiles)) return null;
    const fingerprint=activityFingerprint(p,complete.revision);
    const cached=(await this.store.exec<any>('SELECT data FROM activity_loc_cache WHERE number=? AND fingerprint=?',number,fingerprint)).toArray()[0];
    if(cached) return JSON.parse(cached.data);
    if(budget && budget.remaining--<=0) return null;
    const remember=async (result:any)=>{
      await this.optionalRead('activity_loc_cache',async ()=>(await this.store.exec('INSERT OR REPLACE INTO activity_loc_cache VALUES (?,?,?)',number,fingerprint,JSON.stringify(result))));
      return result;
    };
    const stored=await this.scanRows(`SELECT r.data FROM members m ${collectionJoin(this.store)} resources r ON r.kind='files' AND r.id=m.id WHERE m.collection=? AND r.tombstone=0`,key);
    if(stored.length!==p.changedFiles) return remember(null);
    let added=0,deleted=0,totalAdded=0,totalDeleted=0;
    for(const row of stored) {
      const file=await this.decode(row.data);
      if(typeof file.filename!=='string') return remember(null);
      if(!Number.isSafeInteger(file.additions) || file.additions<0 || !Number.isSafeInteger(file.deletions) || file.deletions<0) return remember(null);
      // Cached file URLs carry their immutable commit. A mixed/old-head file
      // collection cannot be credited to the PR's current output.
      const expected=file.status==='removed'?p.baseRefOid:p.headRefOid;
      const matches=(raw:any)=>{
        try {
          const url=new URL(raw),parts=url.pathname.split('/').filter(Boolean);
          if(url.hostname==='github.com') return ['blob','raw'].includes(parts[2]) && parts[3]===expected;
          if(url.hostname==='raw.githubusercontent.com') return parts[2]===expected;
          return url.hostname==='api.github.com' && url.searchParams.get('ref')===expected;
        } catch {return false;}
      };
      if(!expected || ![file.blob_url,file.raw_url,file.contents_url].some(matches)) return remember(null);
      totalAdded+=file.additions;totalDeleted+=file.deletions;
      if(typeof file.filename==='string' && file.filename.endsWith('.lean')) {added+=file.additions;deleted+=file.deletions;}
    }
    if(Number.isSafeInteger(p.additions) && p.additions!==totalAdded || Number.isSafeInteger(p.deletions) && p.deletions!==totalDeleted) return remember(null);
    return remember({added,deleted,head_sha:p.headRefOid});
  }
  // The last time the mirror changed: an applied webhook or a stored repair.
  async frozenSince(): Promise<number | null> {
    if (!this.degraded) return null;
    let changed:number;try{changed=Math.max((await this.meta('last_webhook')) || 0,(await this.meta('last_repair')) || 0);}catch{return null;}
    return Date.now()-changed>FROZEN_AFTER?changed:null;
  }
  async prepareDiagnostics(): Promise<void> {
    if(Date.now()-this.diagnosticsAt<60000)return;
    try {this.diagnostics={reconciliation_version:2,...await repositoryHealth(this.store),...this.repairMetrics.summary(),queries:[...this.queryCounts.values()]};this.diagnosticsAt=Date.now();}
    catch(error){this.noteFailure('health_prepare',error);}
  }
  async storageState(): Promise<any> {
    return {mode:this.degraded?'degraded':'writable',mutation_fence:this.migrationUnavailable?'control_unavailable':this.migrationHeld()?'migration':null,degraded:this.degraded ?? null,frozen_since:(await this.frozenSince()),schema:this.schema,database_size:this.sql.databaseSize,fault_injection:this.env.FAULT_INJECTION==='true'?{full:this.faultFull}:undefined};
  }
  async storage(path:string,req:Request): Promise<Response> {
    if(this.store.backend==='postgres' && !['/storage/summary','/storage/ingress','/storage/deliveries','/storage/retention'].includes(path)) throw new AdminError('sqlite_recovery_only');
    if(this.store.backend==='postgres' && path==='/storage/summary') {const monitoring=await this.preparedObservation();return json({storage:monitoring.storage || {backend:'postgres',stale:true},monitoring,retention:{policy:this.retentionPolicy,policy_loaded:this.storageLoaded,classes:this.retentionState,receipts_pinned:this.receiptsPinned,receipt_pin_backend:'r2'}});}
    const input:any=req.method==='POST'?await req.json():{};
    const repo=(await this.meta('repository'))?.id ?? input.repository_id;
    if (typeof repo!=='string' || !/^[0-9]+$/.test(repo)) throw new AdminError('repository_unconfigured');
    if(this.store.backend==='sqlite' && ['/storage/cleanup','/storage/probe'].includes(path)) {
      const owner=this.localMeta('backend_owner');
      if(owner && (owner.backend!=='sqlite' || owner.generation!==(this.env.REPOSITORY_GENERATION || 'sqlite-v1')))throw new AdminError('backend_fenced');
    }
    if (path==='/storage/summary') return json({repository:repo,storage:await this.storageState(),retention:{policy:this.retentionPolicy,policy_loaded:this.storageLoaded,classes:this.retentionState,receipts_pinned:this.receiptsPinned,receipt_pin_backend:'r2'},...summary(this.sql)});
    if (path==='/storage/retention') {
      if(input.policy!==undefined) {
        let next:Policy;try {next=policy(input.policy);}catch {throw new AdminError('invalid_retention_policy');}
        const update=this.policyUpdateFlight.then(async()=>{
          if(this.storageFlight)await this.storageFlight.catch(()=>{});
          if(this.retentionLoadFlight)await this.retentionLoadFlight;
          await this.owner.retentionLane.run(async()=>{
            await this.env.ARCHIVE.put('control/'+repo+'/retention-policy.json',JSON.stringify(next));
            this.policyGeneration++;this.retentionPolicy=next;this.storageLoaded=true;
          });
        });
        this.policyUpdateFlight=update.catch(()=>{});
        await update;
      }
      return json({policy:this.retentionPolicy,policy_loaded:this.storageLoaded,classes:this.retentionState,receipts_pinned:this.receiptsPinned,receipt_pin_backend:'r2',budget:{rows:6000,bytes:4*1024**2,cooperative_io_budget_ms:50,cpu_bound:'rows_and_bytes',receipt_archive_io_budget_ms:2000,receipt_archive_rows:100},observation:await this.preparedObservation()});
    }
    if (path==='/storage/scan') return json(scan(this.sql,input));
    if (path==='/storage/cleanup') {
      const rule=CLEANUP[input.class];
      if (!rule) throw new AdminError('unknown_cleanup_class');
      const before=this.sql.databaseSize,result=deleteClass(this.sql,input.class,input);
      return json({class:input.class,table:rule.table,...result,database_size_before:before,database_size_after:this.sql.databaseSize,
        note:'Deleted rows become reusable free pages; the physical database size need not shrink. Prove headroom with /admin/storage/probe.'});
    }
    if(path==='/storage/ownership') {
      if(this.migrationState?.cutover || this.localMeta('backend_owner')?.backend==='postgres')throw new AdminError('migration_forward_recovery_required');
      if(this.env.MIGRATION_ENVIRONMENT!=='test')throw new AdminError('migration_production_prerequisites_unavailable');
      if(this.ingress!=='parked')throw new AdminError('ingress_must_be_drained');
      if(!['sqlite','postgres'].includes(input.backend) || !/^[A-Za-z0-9_-]{1,80}$/.test(input.generation || ''))throw new AdminError('invalid_backend_owner');
      const prior=this.localMeta('backend_owner') || {backend:'sqlite',generation:'sqlite-v1'};
      if(input.expected_generation!==prior.generation)throw new AdminError('backend_generation_mismatch');
      this.mutationState.blocked=true;
      try {
      if(this.storageFlight)await this.storageFlight;
      await this.policyUpdateFlight;
      for(const deadline=Date.now()+25000;this.mutationState.active>0 && Date.now()<deadline;)await new Promise(resolve=>setTimeout(resolve,50));
      if(this.mutationState.active || this.applying)throw new AdminError('repository_mutations_not_drained');
      this.ctx.storage.transactionSync(()=>{
        // Persistent SQLite triggers fence even deployments predating the
        // asynchronous adapter. Install only during the explicit S2 operation.
        const existing=new Set(this.sql.exec<any>("SELECT name FROM sqlite_master WHERE type='table'").toArray().map(row=>row.name));
        for(const table of Object.keys(columns).filter(table=>existing.has(table)))for(const operation of ['INSERT','UPDATE','DELETE']) {
          const record=operation==='DELETE'?'OLD':'NEW',exclude=table==='meta'?` AND ${record}.key!='backend_owner'`:'';
          this.sql.exec(`CREATE TRIGGER IF NOT EXISTS backend_fence_${table}_${operation.toLowerCase()} BEFORE ${operation} ON ${table} WHEN COALESCE((SELECT json_extract(value,'$.backend') FROM meta WHERE key='backend_owner'),'sqlite')!='sqlite'${exclude} BEGIN SELECT RAISE(ABORT,'Repository backend generation fenced'); END`);
        }
        this.localSetMeta('backend_owner',{backend:input.backend,generation:input.generation});
      });
      await this.ctx.storage.sync();this.revisionState.value=undefined;
      return json({backend:input.backend,generation:input.generation});
      }finally{this.mutationState.blocked=false;}
    }
    if (path==='/storage/probe') {
      const bytes=Number.isSafeInteger(input.bytes) && input.bytes>0?Math.min(input.bytes,64*1048576):65536,before=this.sql.databaseSize;
      // Commit a real row, then delete it: proves usable headroom of that size.
      // The size-limit failure arrives at commit, so wait for durability.
      try {
        // Rows stay below Cloudflare's per-row limit; together they prove the requested headroom.
        this.ctx.storage.transactionSync(()=>{
          for (let offset=0,i=0;offset<bytes;offset+=1048576,i++) this.sql.exec("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",'storage_probe:'+i,JSON.stringify('x'.repeat(Math.min(1048576,bytes-offset))));
        });
        await this.ctx.storage.sync();
      } catch(error) {
        try {this.sql.exec("DELETE FROM meta WHERE key LIKE 'storage_probe:%'");} catch {}
        return json({writable:false,bytes,reason:this.noteFailure('probe',error),database_size:this.sql.databaseSize,storage:(await this.storageState())});
      }
      this.sql.exec("DELETE FROM meta WHERE key LIKE 'storage_probe:%'");
      await this.ctx.storage.sync();
      const marker=await this.env.ARCHIVE.put(storageKey(repo),JSON.stringify({state:'writable',verified_at:Date.now(),bytes,database_size:this.sql.databaseSize,...(input.automatic?{probe_attempts:input.probe_attempts,last_probe_at:input.last_probe_at,next_probe_at:input.next_probe_at}:{})}),input.automatic?{onlyIf:{etagMatches:input.marker_etag}}:undefined);
      if (input.automatic && !marker) return json({writable:false,reason:'storage_marker_conflict'});
      this.degraded=undefined;
      if (this.schema!=='current') this.startup();
      await this.optionalAsync('alarm',async ()=>(await this.setAlarm(Date.now()+100)));
      return json({writable:true,bytes,database_size_before:before,database_size_after:this.sql.databaseSize,storage:(await this.storageState())});
    }
    if (path==='/storage/fault') {
      if (this.env.FAULT_INJECTION!=='true') throw new AdminError('fault_injection_disabled');
      if (!['full','none'].includes(input.mode)) throw new AdminError('invalid_fault');
      await this.env.ARCHIVE.put(this.faultKey(),JSON.stringify({mode:input.mode,at:Date.now()}));
      this.faultFull=input.mode==='full';
      return json({storage:(await this.storageState())});
    }
    if (path==='/storage/ingress') {
      if (!['apply','parked'].includes(input.mode)) throw new AdminError('invalid_ingress_mode');
      this.ingress=input.mode;
      if(input.mode==='apply')this.mutationState.blocked=false;
      for (const deadline=Date.now()+25000;input.mode==='parked' && this.applying>0 && Date.now()<deadline;) await new Promise(resolve=>setTimeout(resolve,50));
      return json({mode:this.ingress,in_flight:this.applying,drained:this.applying===0});
    }
    if (path==='/storage/deliveries') {
      // Read-only receipt check for queue and DLQ reconciliation.
      if (!Array.isArray(input.ids) || input.ids.length>100 || input.ids.some((id:any)=>typeof id!=='string')) throw new AdminError('invalid_ids');
      const rows=await this.store.batch(input.ids.map((id:string)=>({sql:'SELECT id FROM deliveries WHERE id=?',bindings:[id]})));
      return json({known:rows.flatMap(row=>row.toArray().map(r=>r.id))});
    }
    return json({error:'not_found'},404);
  }
  // Persist cooldown BEFORE allocating so commit-time resets cannot cause
  // constructor retries or repeated writes on every cron/request.
  async recoverHeadroom(): Promise<void> {
    if(this.store.backend!=='sqlite')return;
    if (this.probeFlight) return this.probeFlight;
    const run = async () => {
      if (this.storageMarkerUnavailable || this.migrationHeld() || this.ingress === 'parked') return;
      const repo = (await this.meta('repository'))?.id;
      if (!repo) return;
      const control = await this.env.ARCHIVE.get(controlKey(repo));
      if (control && (await control.json<any>()).mode === 'parked') return;
      await noteRepositoryFull(this.env, repo, 'tick', this.degraded?.since || Date.now());
      const object = await this.env.ARCHIVE.get(storageKey(repo));
      const state: any = object ? await object.json() : {};
      if ((state.next_probe_at || 0) > Date.now()) return;
      const attempts = (state.probe_attempts || 0) + 1;
      const claim = await this.env.ARCHIVE.put(storageKey(repo), JSON.stringify({ ...state, state: 'full', probe_attempts: attempts, last_probe_at: Date.now(), next_probe_at: Date.now() + Math.max(60, backoff(attempts)) * 1000 }), { onlyIf: object ? { etagMatches: object.etag } : { etagDoesNotMatch: '*' } });
      if (!claim) return;
      try {
        await this.storage('/storage/probe', new Request('https://internal/storage/probe', { method: 'POST', body: JSON.stringify({ repository_id: repo, automatic: true, marker_etag: claim.etag, bytes:4*1048576, probe_attempts:attempts, last_probe_at:Date.now(), next_probe_at:Date.now()+Math.max(60,backoff(attempts))*1000 }) }));
      } catch (error) { this.noteFailure('automatic_probe', error); }
    };
    const flight = run().catch(error => {log('automatic_probe_failed',{reason:classify(error)});});
    this.probeFlight = flight;
    try {await flight;} finally {if (this.probeFlight === flight) this.probeFlight = undefined;}
  }
  async retryMigrationControl(): Promise<void> {
    const result=this.migrationFlight.then(async()=>{
      if(!this.migrationUnavailable) return;
      try {
        this.migrationState=await readMigration(this.env.ARCHIVE,(await this.meta('repository')).id);
        this.migrationUnavailable=false;await this.selectMigrationBackend();
        if(this.store.backend==='sqlite' && this.localMeta('backend_owner')?.backend==='migration')this.startup(true);
        else if(this.schema!=='current' && !this.migrationHeld() && !this.degraded) this.startup();
      } catch {this.migrationUnavailable=true;if(this.store.backend==='sqlite')this.startup(true); /* Retry without enabling source writes. */ }
    });
    this.migrationFlight=result.catch(()=>{});
    await result;
  }
  private selectCommittedBackend(owner:any):void {
    const repo=this.localMeta('repository')?.id,identity=owner.target_identity;
    if(!repo || !identity || !this.env.REPOSITORY_DATABASE_URL || !owner.selected_at)throw new AdminError('migration_target_configuration');
    try {
      const url=new URL(this.env.REPOSITORY_DATABASE_URL);
      if(!['postgres:','postgresql:'].includes(url.protocol) || url.hostname!==identity.hostname || decodeURIComponent(url.pathname.slice(1))!==identity.database || decodeURIComponent(url.username)!==identity.application_role)throw 0;
    }catch {throw new AdminError('migration_target_configuration');}
    this.store=new PostgresStore(this.env.REPOSITORY_DATABASE_URL,repo,owner.generation);
    this.selectedMigrationGeneration=owner.generation;this.schema='current';this.degraded=undefined;this.storageMarkerUnavailable=false;
    this.repositoryMemo=undefined;this.revisionState.value=undefined;this.completedQueries.clear();this.publicCompletedQueries.clear();
  }
  private async selectMigrationBackend():Promise<void> {
    const state=this.migrationState;
    if(!state?.cutover?.target_active)return;
    if(this.selectedMigrationGeneration!==state.generation) {
      const identity=state.cutover.target_identity;
      if(!identity || Object.entries(identity).some(([key,value])=>(targetConfig(this.env) as any)[key]!==value))throw new AdminError('migration_target_identity_mismatch');
      const previous=this.localMeta('backend_owner');
      if(previous?.generation!==state.generation || !['migration','postgres'].includes(previous.backend))throw new AdminError('migration_source_fence_lost');
      const selected={...previous,backend:'postgres',selected_at:state.cutover.selected_at || Date.now(),target:state.target,target_identity:identity};
      this.nativeSql.exec("UPDATE meta SET value=? WHERE key='backend_owner'",JSON.stringify(selected));
      await this.ctx.storage.sync();this.selectCommittedBackend(selected);
    }
    // Queue owners must keep ignoring the obsolete file after migration-only
    // bindings are retired. Publication is retried after a selection crash.
    const key=storageKey(state.repository),marker:any=await this.env.ARCHIVE.get(key).then(o=>o?.json());
    if(marker?.state!=='superseded' || marker?.backend!=='postgres' || marker?.generation!==state.generation)await this.env.ARCHIVE.put(key,JSON.stringify({state:'superseded',backend:'postgres',generation:state.generation,selected_at:state.cutover.selected_at}));
  }

  private assertMigrationFence(state:Migration):void {
    const row=this.nativeSql.exec<any>("SELECT value FROM meta WHERE key='backend_owner'").toArray()[0];
    const owner=row && JSON.parse(row.value);
    if(!owner || owner.backend!=='migration' || owner.generation!==state.generation)throw new AdminError('migration_source_fence_lost');
  }
  private async drainMigration(dispatches:boolean):Promise<void> {
    if(this.storageFlight)await this.storageFlight;
    await this.policyUpdateFlight;await this.retentionLane.run(async()=>{});
    if(dispatches) {
    const repo=this.localMeta('repository');
    const upstream=await this.env.INSTALLATIONS.get(this.env.INSTALLATIONS.idFromName(repo.installation)).fetch('https://internal/health');
    if(!upstream.ok)throw new AdminError('migration_dispatch_drain_unavailable');
    const health:any=await upstream.json();
    if(health.deployment_id!==this.env.CF_VERSION?.id || !Array.isArray(health.repository_inflight))throw new AdminError('migration_dispatch_drain_unavailable');
    if(health.repository_inflight.some((row:any)=>row.repository===repo.id && row.requests>0))throw new AdminError('migration_drain_pending');
    }
    if(this.activeOperations || this.mutationState.active || this.optionalWork.active || this.applying)throw new AdminError('migration_drain_pending');
  }
  private async fenceMigration(state:Migration):Promise<void> {
    const sql=this.nativeSql;
    this.ctx.storage.transactionSync(()=>{
      const row=sql.exec<any>("SELECT value FROM meta WHERE key='backend_owner'").toArray()[0];
      const prior=row?JSON.parse(row.value):{backend:'sqlite',generation:this.env.REPOSITORY_GENERATION || 'sqlite-v1'};
      if(prior.backend==='migration' && prior.generation===state.generation)return;
      if(prior.backend!=='sqlite' || prior.generation!==(this.env.REPOSITORY_GENERATION || 'sqlite-v1'))throw new AdminError('migration_source_fence_lost');
      for(const table of expectedTables(sql))for(const op of ['INSERT','UPDATE','DELETE']) {
        const exclude=table==='meta'?` AND ${op==='DELETE'?'OLD':'NEW'}.key!='backend_owner'`:'';
        sql.exec(`CREATE TRIGGER IF NOT EXISTS backend_fence_${table}_${op.toLowerCase()} BEFORE ${op} ON ${table} WHEN COALESCE((SELECT json_extract(value,'$.backend') FROM meta WHERE key='backend_owner'),'sqlite')!='sqlite'${exclude} BEGIN SELECT RAISE(ABORT,'Repository backend generation fenced'); END`);
      }
      sql.exec("INSERT INTO meta VALUES('backend_owner',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",JSON.stringify({protocol:'s2-selection-v1',backend:'migration',generation:state.generation,frozen_at:Date.now(),selected_at:Number.MAX_SAFE_INTEGER,target:state.target,target_identity:state.cutover!.target_identity}));
    });
    await this.ctx.storage.sync();this.assertMigrationFence(state);
  }
  private async releaseMigration(state:Migration):Promise<void> {
    // A still-full file retains stored reads and parked ingress. Cron retries.
    const row=this.nativeSql.exec<any>("SELECT value FROM meta WHERE key='backend_owner'").toArray()[0];
    const owner=row && JSON.parse(row.value);
    if(!owner || owner.backend==='sqlite' && owner.generation===(this.env.REPOSITORY_GENERATION || 'sqlite-v1')){await this.ctx.storage.sync();return;}
    this.assertMigrationFence(state);
    this.nativeSql.exec("UPDATE meta SET value=? WHERE key='backend_owner'",JSON.stringify({backend:'sqlite',generation:this.env.REPOSITORY_GENERATION || 'sqlite-v1'}));
    await this.ctx.storage.sync();
  }
  private async cutover(input:any,prior:Migration|null,repo:string):Promise<Response> {
    const batchSteps=input.batch_steps ?? 1;
    if(!Number.isSafeInteger(batchSteps) || batchSteps<1 || batchSteps>100)throw new AdminError('migration_step_limit_invalid');
    if(input.environment!==this.env.MIGRATION_ENVIRONMENT || !['test','production'].includes(input.environment))throw new AdminError('migration_environment_mismatch');
    const id=generation(input.generation),target=targetIdentity(input.target);
    if(input.apply!==true)throw new AdminError('migration_apply_required');
    let state=prior;
    if(!state || state.phase==='abandoned' && state.generation!==id) {
      if(input.operation!=='start')throw new AdminError('migration_not_started');
      if((await this.meta('schema_version'))!==SCHEMA_VERSION || !this.env.CF_VERSION?.id)throw new AdminError('migration_schema_incompatible');
      expectedTables(this.nativeSql);
      if((await this.env.ARCHIVE.list({prefix:archiveKey(repo,id)+'/',limit:1})).objects.length)throw new AdminError('migration_generation_used');
      state={version:1,generation:id,repository:repo,target,checkpoint_etag:prior?.checkpoint_etag,
        source:{object:this.ctx.id.toString(),deployment:this.env.CF_VERSION.id,schema:SCHEMA_VERSION},phase:'preflighting',created:Date.now(),updated:Date.now(),tables:{},failure:null,
        blobs:{prefix:0,pages:0,objects:0,hash:'0'.repeat(64),complete:false},staging:{after:null,page:'1',checked:0,complete:false,expired_jobs:0,expired_hash:'0'.repeat(64)},
        cutover:{environment:input.environment,lease_until:Date.now()+FREEZE_LEASE_MS,epoch:0,source_fenced:false,target_active:false,import:{},verify:{},acceptance:''}};
      await cutoverPreflight(this.env,state);
      this.migrationState=state;await saveMigration(this.env.ARCHIVE,state);
    }
    if(state.generation!==id || state.target!==target)throw new AdminError('migration_identity_conflict');
    if(state.source.object!==this.ctx.id.toString())throw new AdminError('migration_source_identity_changed');
    if(input.operation==='finish-observation') {
      if(state.phase!=='complete')throw new AdminError('migration_observation_not_ready');
      let approved:Policy;try{approved=policy(input.retention_policy);}catch{throw new AdminError('migration_retention_policy_invalid');}
      if(!state.cutover!.observation_release){state.cutover!.observation_release={at:Date.now(),policy:approved};await saveMigration(this.env.ARCHIVE,state);}
      await this.finishObservation(state);return json(publicReceipt(state));
    }
    if(input.operation==='abandon') {
      if(state.cutover!.target_active)throw new AdminError('migration_forward_recovery_required');
      state.phase='aborting';state.cutover!.abort_reason='operator_abandoned';await saveMigration(this.env.ARCHIVE,state);
    } else if(!['start','step','preflight'].includes(input.operation))throw new AdminError('migration_operation_invalid');
    return this.superviseMigration(batchSteps);
  }
  private async shadowMigration(state:Migration,onQuery:()=>void=()=>{}):Promise<void> {
    const record=await this.env.ARCHIVE.get(`control/${state.repository}/migration-acceptance.json`);
    const text=record?await record.text():null;
    if(!text || await digest(text)!==state.cutover!.acceptance)throw new AdminError('migration_acceptance_changed');
    const evidence=JSON.parse(text),queries=evidence.queries;
    if(!Array.isArray(queries) || !queries.length || queries.length>20)throw new AdminError('migration_shadow_queries_missing');
    const target=Object.create(this) as RepositoryState;
    target.store=new PostgresStore(this.env.REPOSITORY_DATABASE_URL!,state.repository,state.generation,undefined,undefined,undefined,onQuery);
    target.revisionState={};target.repositoryMemo=undefined;
    const canonical=(value:any):string=>JSON.stringify(value,(_key,v)=>v && typeof v==='object' && !Array.isArray(v)?Object.fromEntries(Object.keys(v).sort().map(k=>[k,v[k]])):v);
    const check=state.cutover!.shadow ||= {queries:0,hash:'0'.repeat(64),verified_at:0,complete:false};
    if(check.queries<queries.length) {
      const query=queries[check.queries];
      if(query.repository!==(await this.meta('repository')).name)throw new AdminError('migration_shadow_query_invalid');
      validateQuery(query);
      const source=await this.query(query),destination=await target.query(query);
      if(!query.cursor && (source.status!=='ok' || destination.status!=='ok' || !source.complete || !destination.complete))throw new AdminError('migration_shadow_unavailable');
      if(canonical({...source,cursor:undefined})!==canonical({...destination,cursor:undefined}))throw new AdminError('migration_shadow_mismatch');
      if(source.cursor) {
        const continuation={...query,cursor:source.cursor},a=await this.query(continuation),b=await target.query(continuation);
        if(a.status!=='ok' || b.status!=='ok' || canonical(a)!==canonical(b))throw new AdminError('migration_shadow_mismatch');
        check.cursor_checks=(check.cursor_checks || 0)+1;
      }
      if(!query.cursor && Array.isArray(source.data) && source.data.length>1 && !check.expired_cursor_checks) {
        // Private acceptance snapshots use only the frozen stored result. They
        // exercise the existing cursor protocol without publishing SQL changes.
        const repo=(await this.meta('repository')).id,now=Date.now();
        for(const expired of [false,true]) {
          const id=crypto.randomUUID(),expires=expired?now-1000:now+600000;
          const result={...source,data:source.data.slice(1),cursor:undefined};
          await this.env.ARCHIVE.put(cursorKey(repo,id,1),JSON.stringify({repository:repo,key:queryKey(query),allow_observed:Boolean(query.allow_observed),expires,result}),{customMetadata:{kind:'cursor',expires:String(expires+60000)}});
          const continuation={...query,cursor:id+':1'},a=await this.query(continuation),b=await target.query(continuation);
          if(canonical(a)!==canonical(b) || (expired?a.reason!=='cursor_expired' || a.status!=='unavailable':a.status!=='ok'))throw new AdminError('migration_shadow_mismatch');
          if(expired)check.expired_cursor_checks=(check.expired_cursor_checks || 0)+1;else check.cursor_checks=(check.cursor_checks || 0)+1;
        }
      }
      check.hash=await digest(check.hash+'\n'+canonical({...source,cursor:undefined}));check.queries++;check.verified_at=Date.now();return;
    }
    if(Array.isArray(evidence.activity_numbers) && evidence.activity_numbers.length && !check.activity_checked) {
      const numbers=evidence.activity_numbers;
      if(numbers.length>20 || numbers.some((n:any)=>!Number.isSafeInteger(n) || n<=0))throw new AdminError('migration_shadow_query_invalid');
      const input={repository:(await this.meta('repository')).name,numbers};
      const request=()=>new Request('https://internal/activity',{method:'POST',body:JSON.stringify(input)});
      // Clock-derived windows and nested response timestamps must describe
      // the same instant while comparing otherwise frozen stored data.
      const source=Object.create(this) as RepositoryState;
      source[ACTIVITY_CLOCK]=target[ACTIVITY_CLOCK]=Date.now();
      const [a,b]=await Promise.all([source.repositoryFetch(request()),target.repositoryFetch(request())]);
      if(!a.ok || !b.ok)throw new AdminError('migration_shadow_unavailable');
      const left=await a.json<any>(),right=await b.json<any>();
      if(canonical(left)!==canonical(right))throw new AdminError('migration_shadow_mismatch');
      check.activity_checked=true;check.hash=await digest(check.hash+'\n'+canonical({...left,as_of:undefined}));return;
    }
    const [sourceHealth,targetHealth]=await Promise.all([repositoryHealth(this.store),repositoryHealth(target.store)]);
    // Runtime health uses the same SQL boundary. Only stable stored fields
    // participate; current clock-based eligibility is captured separately.
    if(canonical(sourceHealth.survey)!==canonical(targetHealth.survey))throw new AdminError('migration_health_mismatch');
    if(canonical(sourceHealth.coverage.sort((a:any,b:any)=>a.operation.localeCompare(b.operation)))!==canonical(targetHealth.coverage.sort((a:any,b:any)=>a.operation.localeCompare(b.operation))))throw new AdminError('migration_health_mismatch');
    if(this.env.MIGRATION_ENVIRONMENT==='production' && (!(check.cursor_checks!>0) || !(check.expired_cursor_checks!>0)))throw new AdminError('migration_acceptance_incomplete');
    check.complete=true;check.verified_at=Date.now();
  }
  private async finishMigrationReceipt(state:Migration):Promise<void> {
    const recovery=await backlog(this.env,{id:state.repository,name:(await this.meta('repository')).name,installation:''});
    const empty=(prefix:string)=>this.env.ARCHIVE.list({prefix,limit:1}).then(p=>!p.objects.length && !p.truncated);
    if(!await empty(`parked/${state.repository}/pointers/`))return;
    const heldEmpty=await empty(`parked/${state.repository}/held/`);
    const queues=await queueMetrics(this.env);
    // Both queues have this deployment as their consumer. The same durable
    // parking/receipt handler owns normal and dead-letter inputs. Historical
    // bucket scans remain visible, but are independent recovery work.
    if(['queue','dlq'].some(name=>queues[name].stale || queues[name].count!==0 || queues[name].at<state.cutover!.selected_at!))return;
    if(!state.cutover!.replay_complete && (heldEmpty || recovery.held_pass_started_at>=state.cutover!.selected_at! && recovery.held_pass_completed_at>=recovery.held_pass_started_at)){state.cutover!.replay_complete=Date.now();await saveMigration(this.env.ARCHIVE,state);}
    if(!heldEmpty){log('migration_held_input_pending',{repository:state.repository,held:recovery.held?.count ?? null,next_retry:recovery.next_retry ?? null,reconciliation_resumed:Boolean(state.cutover!.replay_complete)});return;}
    await this.prepareDiagnostics();
    const evidence=await this.env.ARCHIVE.get(`control/${state.repository}/migration-acceptance.json`);
    const evidenceText=evidence?await evidence.text():null,acceptance=evidenceText?JSON.parse(evidenceText):null;
    if(!acceptance || await digest(evidenceText!)!==state.cutover!.acceptance)throw new AdminError('migration_acceptance_changed');
    const health=await this.store.health();
    const rate=await this.env.INSTALLATIONS.get(this.env.INSTALLATIONS.idFromName((await this.meta('repository')).installation)).fetch('https://internal/health').then(r=>r.json<any>());
    state.cutover!.release={schema_versions:{source:state.source.schema,target:1},deployment:this.env.CF_VERSION?.id,source:state.source,target:state.target,
      revision:await this.meta('revision'),latest_applied_input:await this.meta('latest_applied_input'),latest_webhook:await this.meta('last_webhook'),latest_repair:await this.meta('last_repair'),
      backlog:{parked:0,held:0,queue:0,dlq:0,queue_observed_at:queues.queue.at,dlq_observed_at:queues.dlq.at,raw_completed:recovery.raw?.last_completed ?? null,raw_gaps:recovery.raw?.gaps ?? null},backend_write_owner:'postgres',
      latency:{baseline_p95_ms:acceptance.latency.baseline_p95_ms,target_p95_ms:acceptance.latency.target_p95_ms},cursor_coverage:{versions:['r2-v1'],active_checked:state.cutover!.shadow?.cursor_checks || 0,expired_checked:state.cutover!.shadow?.expired_cursor_checks || 0},github_hour:rate.hour ?? null,
      github_hour_observation:{at:rate.at ?? null,window_start:Number.isSafeInteger(rate.at)?rate.at-3600000:null,prepared_deployment:rate.prepared_deployment ?? null,scope:rate.scope ?? null,stale:rate.stale ?? true,window_complete:rate.window_complete ?? null,telemetry_failure:rate.telemetry_failure ?? null},health};
    state.cutover!.completed_at=Date.now();state.phase='complete';await saveMigration(this.env.ARCHIVE,state);
    await terminalReceipt(this.env.ARCHIVE,state);
  }
  private async finishObservation(state:Migration):Promise<void> {
    const release=state.cutover!.observation_release;if(!release)return;
    if(state.cutover!.observation_finished_at){await terminalReceipt(this.env.ARCHIVE,state,'observation-receipt.json');return;}
    if(await targetOwner(this.env,state)!=='postgres')throw new AdminError('migration_forward_recovery_required');
    await this.storage('/storage/retention',new Request('https://internal/storage/retention',{method:'POST',body:JSON.stringify({policy:policy(release.policy)})}));
    state.cutover!.observation_finished_at=release.at;state.failure=null;await saveMigration(this.env.ARCHIVE,state);
    await terminalReceipt(this.env.ARCHIVE,state,'observation-receipt.json');
  }
  private async superviseMigration(steps=5):Promise<Response> {
    // Bound cumulative request work alongside the per-page limits, leaving
    // memory and CPU headroom for overlapping stored reads and ingress.
    steps=Math.min(steps,5);
    const repo=JSON.parse(this.env.ALLOWED_REPOSITORIES || '[]').find((r:any)=>this.env.REPOSITORIES.idFromName(r.id).toString()===this.ctx.id.toString())?.id || this.localMeta('repository')?.id;
    if(!repo)return json({migration:null});
    let io=0;
    const bucket=new Proxy(this.env.ARCHIVE,{get(target,key){const value=Reflect.get(target,key,target);return typeof value==='function'?(...args:any[])=>{if(['get','head','put','list','delete'].includes(String(key)) && ++io>900)throw new AdminError('migration_shadow_request_budget');return value.apply(target,args);}:value;}});
    const controlEnv={...this.env,ARCHIVE:bucket,migrationConnection:()=>{io+=2;if(io>900)throw new AdminError('migration_shadow_request_budget');}};
    // Reserve room for the next bounded page (up to 250 blob HEADs), target
    // connections and terminal bookkeeping below the platform request limit.
    // Leave 280 operations: the widest verification step needs 250 HEADs.
    let advanced=false;
    for(const deadline=Date.now()+20000;steps-->0 && Date.now()<deadline && io<620;) {
      const state=await readMigration(bucket,repo);this.migrationState=state;
      if(!state?.cutover)break;
      if(state.phase==='switching' && advanced)break;
      if(state.source.object!==this.ctx.id.toString())throw new AdminError('migration_source_identity_changed');
      try {
        if(['complete','abandoned'].includes(state.phase)) {
          await this.selectMigrationBackend();
          if(state.phase==='complete')await this.finishObservation(state);
          await terminalReceipt(this.env.ARCHIVE,state);
          if(state.phase==='complete' && state.failure){state.failure=null;await saveMigration(bucket,state);}
          break;
        }
        io+=2;await controllerStep(controlEnv,state,{storage:this.ctx.storage,sql:this.nativeSql,compatibility:async s=>{compatibilityStep(this.nativeSql,s);if(s.phase==='freezing'){s.phase='preflighting';await cutoverPreflight(controlEnv,s);s.phase='freezing';}},
          fence:s=>this.fenceMigration(s),assertFence:s=>this.assertMigrationFence(s),verifyStored:async s=>{
            const shadow=Object.create(this) as RepositoryState;shadow.env=controlEnv;
            const before=io,began=Date.now();
            await shadow.shadowMigration(s,()=>{if(++io>900)throw new AdminError('migration_shadow_request_budget');});
            const check=s.cutover!.shadow;
            if(check){check.max_subrequests=Math.max(check.max_subrequests || 0,io-before);check.max_step_ms=Math.max(check.max_step_ms || 0,Date.now()-began);}
          },drain:s=>this.drainMigration(s.phase==='freezing'),release:s=>this.releaseMigration(s),
          select:async()=>{this.migrationState=state;await this.selectMigrationBackend();}});
        advanced=true;
        state.failure=state.phase==='abandoned'?(state.cutover!.abort_reason || null):null;await saveMigration(bucket,state);
        if(state.phase==='replaying') {await this.finishMigrationReceipt(state);break;}
        if(state.phase==='switching')break;
      } catch(error) {
        const reason=error instanceof AdminError?error.message:'migration_storage_failure';
        const saved=await readMigration(this.env.ARCHIVE,repo);
        if(saved && saved.generation===state.generation) {saved.failure=reason;if(saved.cutover)saved.cutover.last_failure=reason;if(reason==='migration_source_fence_lost' && !saved.cutover?.target_active)saved.phase='aborting';await saveMigration(this.env.ARCHIVE,saved);this.migrationState=saved;}
        return json({error:reason,...publicReceipt(this.migrationState) as object},503);
      }
    }
    return json(publicReceipt(this.migrationState));
  }
  async migration(req:Request): Promise<Response> {
    if(this.store.backend!=='sqlite' && !this.migrationState?.cutover)return json({error:'sqlite_source_migration_only'},400);
    let state=this.migrationState;
    try {
      const input:any=await req.json();
      const repo=this.localMeta('repository')?.id;
      if(!repo) throw new AdminError('repository_unconfigured');
      // A control-store outage must fence mutations without taking complete
      // SQL reads down. An admin retry can reload the barrier after recovery.
      if(this.migrationUnavailable) {
        this.migrationState=await readMigration(this.env.ARCHIVE,repo);
        this.migrationUnavailable=false;
        if(this.store.backend==='sqlite' && this.localMeta('backend_owner')?.backend==='migration')this.startup(true);
        try{await this.selectMigrationBackend();}catch(error){this.migrationUnavailable=true;throw error;}
        state=this.migrationState;
      }
      if(input.operation==='status') {state=await readMigration(this.env.ARCHIVE,repo);return json(publicReceipt(state));}
      if(input.operation==='start' || state?.cutover && !['preflight'].includes(input.operation)) return await this.cutover(input,state,repo);
      if(!input.operation || input.operation==='preflight') {
        const tables=expectedTables(this.sql);
        let verified=false;
        if(input.generation && input.target && !state?.cutover) {
          const candidate={generation:generation(input.generation),repository:repo,target:targetIdentity(input.target),source:{object:this.ctx.id.toString(),deployment:this.env.CF_VERSION?.id,schema:SCHEMA_VERSION},cutover:{environment:input.environment}} as Migration;
          await cutoverPreflight(this.env,candidate);verified=true;
        }
        return json({ ...publicReceipt(state) as object, repository:repo,
          source:state?.source ?? {object:this.ctx.id.toString(),deployment:this.env.CF_VERSION?.id ?? null,schema:(await this.meta('schema_version'))}, expected_tables:tables,
          source_export_environment:this.env.MIGRATION_ENVIRONMENT ?? null,
          backend_supported:['sqlite','postgres'],supervised_cutover_available:true,
          blockers:!this.env.MIGRATION_DATABASE_URL || !this.env.REPOSITORY_DATABASE_URL || !this.env.MIGRATION_TARGET?['migration_target_configuration']:[],
          acceptance_required:true,target_preflight_verified:verified,source_type_scan:'bounded_before_freeze',production_cutover_supported:this.env.MIGRATION_ENVIRONMENT==='production' });
      }
      const id=generation(input.generation),target=targetIdentity(input.target);
      // Source export does not authorize target import or production selection.
      // Caller-supplied claims cannot stand in for deployed acceptance evidence.
      if(input.environment!=='test' || this.env.MIGRATION_ENVIRONMENT!=='test') throw new AdminError('migration_production_prerequisites_unavailable');
      if(input.apply!==true) throw new AdminError('migration_apply_required');
      const previousEtag=state?.checkpoint_etag;
      if(state?.phase==='abandoned' && state.generation!==id && input.operation==='freeze') state=null;
      if(state && (state.generation!==id || state.target!==target)) throw new AdminError('migration_identity_conflict');
      if(!['freeze','step','abandon'].includes(input.operation)) throw new AdminError('migration_target_backend_unavailable');
      if(!state) {
        if(input.operation!=='freeze') throw new AdminError('migration_not_started');
        if((await this.env.ARCHIVE.list({prefix:archiveKey(repo,id)+'/',limit:1})).objects.length) throw new AdminError('migration_generation_used');
        expectedTables(this.sql);
        if((await this.meta('schema_version'))!==SCHEMA_VERSION) throw new AdminError('migration_schema_incompatible');
        if(!this.env.CF_VERSION?.id) throw new AdminError('migration_deployment_identity_required');
        state={version:1,generation:id,repository:repo,target,checkpoint_etag:previousEtag,
          source:{object:this.ctx.id.toString(),deployment:this.env.CF_VERSION.id,schema:SCHEMA_VERSION},
          phase:'freezing',created:Date.now(),updated:Date.now(),tables:{},failure:null,
          blobs:{prefix:0,pages:0,objects:0,hash:'0'.repeat(64),complete:false},
          staging:{after:null,page:'1',checked:0,complete:false,expired_jobs:0,expired_hash:'0'.repeat(64)}};
        // Install the in-memory barrier before yielding. Persist the barrier
        // before waiting so a restart cannot accidentally unfreeze the source.
        this.migrationState=state;
        await saveMigration(this.env.ARCHIVE,state);
      }
      if(state.source.object!==this.ctx.id.toString() || input.operation!=='abandon' && state.source.deployment!==this.env.CF_VERSION?.id) throw new AdminError('migration_source_identity_changed');
      // Always reload the durable checkpoint: an interrupted operation may
      // have changed its local object without committing that checkpoint.
      // If the very first barrier upload failed before commit, the in-memory
      // intent is still fenced and may safely retry that same initial upload.
      let checkpoint=await readMigration(this.env.ARCHIVE,repo);
      if(!checkpoint && state.phase==='freezing' && !Object.keys(state.tables).length) {
        await saveMigration(this.env.ARCHIVE,state);
        checkpoint=await readMigration(this.env.ARCHIVE,repo);
      }
      if(!checkpoint) throw new AdminError('migration_checkpoint_unavailable');
      if(checkpoint.generation!==id || checkpoint.target!==target) throw new AdminError('migration_identity_conflict');
      if(checkpoint.source.object!==this.ctx.id.toString() || input.operation!=='abandon' && checkpoint.source.deployment!==this.env.CF_VERSION?.id) throw new AdminError('migration_source_identity_changed');
      state=checkpoint;
      this.migrationState=state;
      state.failure=null;
      if(input.operation==='abandon') {
        // There is no PostgreSQL write switch in this protocol. Preserve all
        // archives; explicit abandonment only releases this source fence.
        if(this.activeOperations || this.mutationState.active || this.optionalWork.active) throw new AdminError('migration_drain_pending');
        state={...state,phase:'abandoned',failure:null};
        await this.env.ARCHIVE.put(archiveKey(repo,id)+'/abandoned.json',encodeMigration(state));
        await saveMigration(this.env.ARCHIVE,state);
        this.migrationState=state;
        return json(publicReceipt(state));
      }
      if(state.phase==='abandoned') throw new AdminError('migration_generation_abandoned');
      if(state.phase==='freezing') {
        const claimKey=archiveKey(repo,id)+'/generation.json';
        const claimed=await this.env.ARCHIVE.get(claimKey);
        if(claimed) {
          const owner:any=await claimed.json();
          if(owner.generation!==id || owner.target!==target || owner.source.object!==state.source.object || owner.source.deployment!==state.source.deployment) throw new AdminError('migration_generation_used');
        } else if(!await this.env.ARCHIVE.put(claimKey,JSON.stringify({generation:id,target,source:state.source}),{onlyIf:{etagDoesNotMatch:'*'}})) throw new AdminError('migration_generation_used');
        for(const deadline=Date.now()+25000;(this.activeOperations>0 || this.mutationState.active>0 || this.optionalWork.active>0) && Date.now()<deadline;) await new Promise(resolve=>setTimeout(resolve,25));
        if(this.activeOperations || this.mutationState.active || this.optionalWork.active) throw new AdminError('migration_drain_pending');
        await beginExport(this.env.ARCHIVE,this.ctx.storage,this.sql,state);
      } else if(input.operation==='step') {
        if(state.phase==='exporting') await exportStep(this.env.ARCHIVE,this.sql,state,input.limit);
        else if(state.phase==='verifying') await verifyStep(this.env.ARCHIVE,this.sql,state);
        else if(state.phase==='source_verified') await this.env.ARCHIVE.put(archiveKey(repo,id)+'/receipt.json',encodeMigration(state));
      }
      state.failure=null;
      await saveMigration(this.env.ARCHIVE,state);
      return json(publicReceipt(state));
    } catch(error) {
      const reason=error instanceof AdminError?error.message:'migration_storage_failure';
      // Error details can contain driver credentials or payloads. Persist only
      // controlled reason codes and keep the last committed checkpoint.
      const repo=this.localMeta('repository')?.id;
      if(repo && this.migrationHeld()) {
        const saved=await readMigration(this.env.ARCHIVE,repo).catch(()=>null);
        if(saved) {saved.failure=reason;this.migrationState=saved;await saveMigration(this.env.ARCHIVE,saved).catch(()=>{});}
      }
      return json({error:reason},reason==='migration_storage_failure' || reason==='migration_checkpoint_conflict'?503:400);
    }
  }
  async fetch(req:Request): Promise<Response> {
    // A separate route fails closed if the edge reaches an older object version
    // that does not implement read-only public snapshots yet.
    if(new URL(req.url).pathname==='/public-query') {
      req=new Request(new URL('/query',req.url),req);
      req.headers.set('x-query-readonly','1');
    }
    const path=new URL(req.url).pathname;
    if((path==='/tick' || path==='/migration-supervise') && this.migrationUnavailable) await this.retryMigrationControl();
    if(path==='/migration-supervise') {
      const result=this.migrationFlight.then(()=>this.superviseMigration());this.migrationFlight=result.catch(()=>{});return result;
    }
    if(path==='/migration') {
      const result=this.migrationFlight.then(()=>this.migration(req));
      this.migrationFlight=result.catch(()=>{});
      return result;
    }
    if(this.migrationState?.phase==='replaying' && !this.migrationState.cutover?.replay_complete && ['/repair','/configure','/activity-refresh'].includes(path))return json({error:'migration_replaying',parkable:true},503);
    if(this.migrationHeld()) {
      if(path==='/event') return json({error:'migration_frozen',parkable:true},503);
      if(!['/query','/activity','/health','/changes','/retained-object','/storage/summary','/storage/scan','/storage/deliveries'].includes(path)) return json({error:'migration_frozen',parkable:true},503);
      if(path==='/retained-object') return json({retained:true});
    }
    const tracked=!this.migrationHeld();
    if(tracked) this.activeOperations++;
    const started=Date.now();
    try {
      const response=await this.repositoryFetch(req);
      if(path==='/query') {
        const dispatched=Number(req.headers.get('x-query-dispatched-at'));
        response.headers.set('X-TauCeti-Query-Ms',String(Date.now()-started));
        if(dispatched>0)response.headers.set('X-TauCeti-Input-Wait-Ms',String(Math.max(0,started-dispatched)));
      }
      return response;
    } finally {if(tracked) this.activeOperations--;}
  }
  async repositoryFetch(req:Request): Promise<Response> {
    const url=new URL(req.url),path=url.pathname,request_id=requestId(req),requestStarted=Date.now();
    let requestError=false,requestUnavailable=false,requestOperation='',requestCoverage='complete';
    try {
      if (path.startsWith('/storage/')) return await this.storage(path,req);
      // A genuinely missing schema is a precise error, not empty data.
      if (typeof this.schema==='object' && path!=='/health') return json({error:this.schema.error,missing:this.schema.missing,cause:this.schema.cause,parkable:true,request_id},503);
      if (path==='/configure') { await this.configure((await req.json() as any).repository); return json({ok:true}); }
      if (path==='/ci-receipts') {
        // Old parked CI input predates the decision not to mirror these events.
        // Commit its existing ignored-delivery disposition in one fenced batch;
        // the ingress owner still publishes each R2 receipt before cleanup.
        const input:any=await req.json(),repo=repository(this.env,input?.repository);
        if(!repo || this.env.REPOSITORIES.idFromName(repo.id).toString()!==this.ctx.id.toString())return json({error:'invalid_ci_receipts'},400);
        if(!Array.isArray(input.events) || !input.events.length || input.events.length>100 || input.events.some((p:any)=>!p || typeof p.delivery!=='string' || !/^[a-zA-Z0-9-]{1,100}$/.test(p.delivery) || !CI_EVENTS.has(p.event) || !Number.isSafeInteger(p.received) || p.received<0))return json({error:'invalid_ci_receipts'},400);
        const events=new Map<string,any>();
        for(const p of input.events){if(events.has(p.delivery) && events.get(p.delivery).event!==p.event)return json({error:'invalid_ci_receipts'},400);events.set(p.delivery,p);}
        if(this.storageMarkerUnavailable)return json({error:'storage_control_unavailable',parkable:true,request_id},503);
        if(this.ingress===undefined){const mode=(await this.env.ARCHIVE.get(controlKey(repo.id)).then(o=>o?.json<any>()))?.mode ?? 'apply';this.ingress ??= mode;}
        if(this.ingress==='parked')return json({error:'ingress_parked',parkable:true,request_id},503);
        if(this.degraded)return json({error:this.degraded.reason==='storage_full'?'storage_full':'storage_control_unavailable',parkable:true,request_id},503);
        this.applying++;let timing:{lookup_ms?:number;write_ms:number}|undefined;
        try {
          const now=Date.now(),rows=[...events.values()];let pending=rows;
          // Lost batch replies and cleanup failures leave durable SQL receipts.
          // Discover those with a read before joining the serialized writer.
          // New deliveries still commit under the ordinary generation fence.
          if(this.store.backend==='postgres') {
            const known=new Set((await this.store.exec<any>('SELECT id FROM deliveries WHERE id IN ('+rows.map(()=>'?').join(',')+')',...rows.map(p=>p.delivery))).toArray().map(r=>r.id));
            pending=rows.filter(p=>!known.has(p.delivery));
          }
          const lookupFinished=Date.now();
          if(pending.length) {
            if((this.ingress as string|undefined)==='parked')return json({error:'ingress_parked',parkable:true,request_id},503);
            if(this.storageMarkerUnavailable)return json({error:'storage_control_unavailable',parkable:true,request_id},503);
            const degraded=this.degraded as {reason:string}|undefined;
            if(degraded)return json({error:degraded.reason==='storage_full'?'storage_full':'storage_control_unavailable',parkable:true,request_id},503);
            if(this.mutationState.blocked)return json({error:'backend_fenced',parkable:true,request_id},503);
          }
          if(pending.length)await this.atomic(async state=>{
            const size=state.store.backend==='sqlite'?33:100;
            for(let start=0;start<pending.length;start+=size){const batch=pending.slice(start,start+size);await state.store.exec('INSERT OR IGNORE INTO deliveries VALUES '+batch.map(()=>'(?,?,?)').join(','),...batch.flatMap(p=>[p.delivery,'ignored:'+p.event,now]));}
          });
          if(this.store.backend==='sqlite')await this.ctx.storage.sync();
          timing={...(this.store.backend==='postgres'?{lookup_ms:lookupFinished-now}:{}),write_ms:Date.now()-lookupFinished};
          this.inputFailure=undefined;
        }finally{this.applying--;}
        return json({committed:[...events.keys()],timing});
      }
      if (path==='/event') {
        const pointer=await req.json() as EventPointer,repo=repository(this.env,pointer.repository);
        if(this.storageMarkerUnavailable)return json({error:'storage_control_unavailable',parkable:true,request_id},503);
        if (this.ingress===undefined && repo) {const mode=(await this.env.ARCHIVE.get(controlKey(repo.id)).then(o=>o?.json<any>()))?.mode ?? 'apply';this.ingress ??= mode;}
        if (this.ingress==='parked') return json({error:'ingress_parked',parkable:true,request_id},503);
        if(!repo) throw new Error('repository not allowed');
        let flight=this.deliveryFlights.get(pointer.delivery);
        if(!flight) {
          this.applying++;
          flight=(async()=>{
            try {
              await this.ingest(pointer);
              if(this.store.backend==='sqlite')await this.ctx.storage.sync();
              await this.env.ARCHIVE.put(receiptKey(repo.id,pointer.delivery),JSON.stringify({delivery:pointer.delivery,committed_at:Date.now(),disposition:CI_EVENTS.has(pointer.event)?'not_mirrored':'applied'}),{onlyIf:{etagDoesNotMatch:'*'}});
            } finally {this.applying--;}
          })();
          this.deliveryFlights.set(pointer.delivery,flight);
        }
        try {await flight;} finally {if(this.deliveryFlights.get(pointer.delivery)===flight)this.deliveryFlights.delete(pointer.delivery);}
        this.inputFailure=undefined;
        return json({ok:true});
      }
      if(path==='/retained-object') {
        // Pinned recovery exports may reference spills indefinitely. Retire
        // only once a complete archive reference index/release protocol exists.
        return json({retained:true,reason:'recovery_reference_index_required'});
      }
      if (path==='/activity') {
        const input:any=await req.json();
        const prs=[],missing=[],locBudget={remaining:32},state=await this.primeActivity(input.numbers);
        // Deliberately no configure, demand, collection verification, or repair.
        for (const number of input.numbers) {
          const row=(await state.store.exec<any>("SELECT data,observed FROM resources WHERE kind='pr' AND id=? AND tombstone=0",String(number))).toArray()[0];
          if(!row){missing.push(number);continue;}
          const p=await state.decode(row.data);
          const landing=(await state.store.exec<any>('SELECT at,source FROM activity_landings WHERE number=? AND head=?',number,p.headRefOid || '')).toArray()[0];
          prs.push({number,state:p.state,created_at:p.createdAt || null,landed_at:p.mergedAt || (landing?new Date(landing.at/1000).toISOString():null),
            head_sha:p.headRefOid || null,observed_at:row.observed,
            landing_source:p.mergedAt?'github-merged':landing?.source || null,lean_loc:await state.activityLOC(number,p,locBudget)});
        }
        // Normal responses retain their completion-time clock. A migration
        // shadow supplies the same private clock to both local read scopes.
        const revision=await this.revision(),history=await this.meta('review_coordination');
        const landing_observations={checked_at:await this.meta('activity_landings_checked'),last_success_at:await this.meta('activity_landings_success'),error:await this.meta('activity_landings_error')};
        const asOf=this[ACTIVITY_CLOCK] ?? Date.now();
        return json({version:1,repository:input.repository,revision,as_of:asOf,coverage:'observed',prs,missing,
          review_coordination:coordinationWindows(history,asOf),landing_observations});
      }
      if (path==='/query') {
        const q=parseGitHub(await req.text()) as Query;
        const readOnly=req.headers.get('x-query-readonly')==='1';
        validateQuery(q);
        requestOperation=q.operation;
        if (NOT_MIRRORED.has(q.operation)){requestUnavailable=true;return json(await this.unavailable(q,'not_mirrored'));}
        await this.meta('repository');
        if(q.cursor && !readOnly)this.backgroundDemand(q);
        const changed=await this.frozenSince();
        const planned=this.migrationHeld() || this.migrationState?.phase==='replaying';
        if(changed!==null && !planned){requestUnavailable=true;return json({...await this.unavailable(q,'mirror_frozen'),frozen_since:changed},200,{'X-TauCeti-Storage':'degraded'});}
        // Replace a cached markerless survey even when stored data has not
        // changed. Clients treat validators as opaque; revisions stay numeric.
        const tag=(revision:number)=>'"'+(q.operation==='survey'?SURVEY_ETAG_PREFIX:'')+revision+'"';
        const etag=tag(await this.revision()),headers:Record<string,string>=this.degraded?{'X-TauCeti-Storage':'degraded'}:{};
        if(this.migrationUnavailable && this.store.backend==='sqlite') {headers['X-TauCeti-Read-Fallback']='last-known-source';const frozen=this.localMeta('backend_owner')?.frozen_at;if(frozen)headers['X-TauCeti-Source-Frozen-At']=String(frozen);}
        if (!q.cursor && matchesETag(req.headers.get('if-none-match'),etag) && q.operation!=='progress' && (await this.store.exec('SELECT key FROM collections WHERE key=? AND complete=1',queryKey(q))).toArray().length) {
          if(!readOnly)this.backgroundDemand(q);
          return new Response(null,{status:304,headers:{ETag:etag,...headers}});
        }
        const key=(readOnly?'public|':'authenticated|')+queryKey(q)+'|observed:'+Boolean(q.allow_observed)+'|'+(q.cursor || '');
        const completed=readOnly?this.publicCompletedQueries:this.completedQueries;
        const cached=completed.get(key);
        if (!q.cursor && cached && cached.expires>Date.now() &&
          (cached.result.revision===(await this.revision()) || ['survey','open_prs','open_issues'].includes(q.operation) && Date.now()-cached.built<10000 || !await this.changedQuery(q,cached.result.revision))) {
          requestCoverage=cached.result.coverage || 'unknown';
          if(!readOnly)this.backgroundDemand(q);
          return json(cached.result,200,{ETag:tag(cached.result.revision),...headers});
        }
        let pending=this.inflightQueries.get(key);
        if (!pending) {
          pending=q.cursor?this.continuation(q,queryKey(q)):this.stableQuery(q,readOnly);
          this.inflightQueries.set(key,pending);
        }
        let result:Result;
        try { result=await pending; }
        finally { if (this.inflightQueries.get(key)===pending) this.inflightQueries.delete(key); }
        if (!q.cursor && result.status==='ok') {
          completed.delete(key);
          completed.set(key,{result,expires:Date.now()+540000,built:Date.now(),...(readOnly?{bytes:new TextEncoder().encode(JSON.stringify(result)).length}:{})});
          while (completed.size>16 || readOnly && [...completed.values()].reduce((n,e)=>n+(e.bytes || 0),0)>12*1024*1024) completed.delete(completed.keys().next().value!);
        }
        requestUnavailable=result.status==='unavailable';
        requestCoverage=result.coverage || 'unknown';
        return json(result,200,{ETag:tag(result.revision),...headers});
      }
      if (path==='/health' && this.store.backend==='postgres') {
        const monitoring=await this.preparedObservation(),storage=monitoring.storage || {backend:'postgres',schema_version:null,expected_schema_version:1,expected_generation:this.backendGeneration(),connected:null,stale:true};
        return json({...this.diagnostics,reconciliation_version:2,revision:this.revisionState.value ?? this.diagnostics.revision ?? null,repository:this.repositoryMemo ?? this.diagnostics.repository ?? null,storage,monitoring,request_id,deployment_id:this.env.CF_VERSION?.id,diagnostics_at:this.diagnosticsAt || null,diagnostics_stale:!this.diagnosticsAt || Date.now()-this.diagnosticsAt>180000});
      }
      if (path==='/health') {
        const storage={...await this.storageState(),backend:'sqlite',schema_version:SCHEMA_VERSION};
        if(typeof this.schema==='object' || storage.connected===false)return json({storage,deployment_id:this.env.CF_VERSION?.id,monitoring:await this.preparedObservation(),request_id});
        return json({...this.diagnostics,reconciliation_version:2,settings:settings(this.env),...this.repairMetrics.summary(),queries:[...this.queryCounts.values()],query_metrics_scope:'repository_instance_since_restart',repair_metrics_scope:'repository_instance_since_restart',storage,storage_bytes:storage.database_size ?? (storage.capacity as any)?.database_bytes,
          repository:await this.meta('repository'),revision:await this.revision(),bootstrap_inventory:await this.meta('bootstrap_inventory'),latest_applied_input:await this.meta('latest_applied_input'),
          deployment_id:this.env.CF_VERSION?.id,monitoring:await this.preparedObservation(),diagnostics_at:this.diagnosticsAt || null,diagnostics_stale:!this.diagnosticsAt || Date.now()-this.diagnosticsAt>180000});
      }
      if(path==='/changes') {
        const after=Number(url.searchParams.get('after') || 0);
        if(!Number.isSafeInteger(after) || after<0)return json({error:'invalid_revision'},400);
        const [meta,first,changes]=await this.store.batch([
          {sql:"SELECT value FROM meta WHERE key='revision'"},
          {sql:'SELECT MIN(revision) AS revision FROM changes'},
          {sql:'SELECT revision,at,kind,id,tombstone FROM changes WHERE revision>? ORDER BY revision LIMIT 1000',bindings:[after]}
        ]);
        const revision=JSON.parse(meta.toArray()[0]?.value || '0'),oldest=first.one().revision;
        this.revisionState.value=Math.max(this.revisionState.value ?? 0,revision);
        return json({revision,reset:after>0 && oldest && after<oldest-1,changes:changes.toArray()});
      }
      if (path==='/activity-refresh' && req.method==='POST') {if(!this.degraded) await this.refreshActivityLandings();return json({ok:true,degraded:Boolean(this.degraded)});}
      if (path==='/tick') {
        // Cron reaches a full object even if its alarm cannot be rescheduled.
        if(this.store.backend==='postgres')this.startStorageMaintenance();
        if(this.store.backend==='sqlite')await this.storageMarker();
        await this.expire();
        if(this.degraded && this.store.backend==='sqlite')await this.recoverHeadroom();
        if (!(await this.meta('repository'))) {
          if(this.store.backend==='postgres')return json({ok:false,reason:'repository_unconfigured'},409);
          await this.configure((await req.json() as any).repository);
        }
        await this.optionalAsync('alarm',async ()=>(await this.setAlarm(Date.now()+100)));
        this.startStorageMaintenance();
        if(this.store.backend==='postgres')this.ctx.waitUntil(this.alarm());
        return json({ok:true,degraded:Boolean(this.degraded)});
      }
      if (path==='/repair') { await this.store.exec('UPDATE jobs SET due=CASE WHEN wait_reason IS NULL AND attempt=0 THEN ? ELSE due END,dirty=1,generation=generation+1',Date.now()); await this.setAlarm(Date.now()+100); return json({ok:true}); }
      return json({error:'not_found'},404);
    } catch(error) {
      requestError=true;
      if (error instanceof AdminError) return json({error:error.message,request_id},400);
      const reason=this.noteFailure('request:'+path,error);
      if(path==='/event' && PARKABLE.has(reason))this.inputFailure={since:this.inputFailure && Date.now()-this.inputFailure.at<=180000?this.inputFailure.since:Date.now(),at:Date.now(),reason};
      log('repository_request_failed',{request_id,path,reason});
      return json({error:reason,parkable:PARKABLE.has(reason),message:(error instanceof Error?error.message:'operation failed').slice(0,200),request_id},reason==='internal'?500:503);
    }finally {
      if(path==='/query')try {
        const finished=Date.now(),dispatched=Number(req.headers.get('x-query-dispatched-at'));
        if(finished-requestStarted>=250 || dispatched>0 && requestStarted-dispatched>=250)
          log('repository_query_slow',{request_id,operation:requestOperation,handler_ms:finished-requestStarted,input_wait_ms:dispatched>0?Math.max(0,requestStarted-dispatched):null,error:requestError,unavailable:requestUnavailable});
        this.requestMetrics.record(Date.now()-requestStarted,requestError,requestUnavailable);
        if(requestOperation) {
          const coverage=requestError?'error':requestUnavailable?'unavailable':requestCoverage,key=JSON.stringify([requestOperation,coverage]);
          const old=this.queryCounts.get(key);
          this.queryCounts.set(key,{operation:requestOperation,coverage,requests:(old?.requests || 0)+1,last_at:Date.now()});
        }
      }catch {}
    }
  }
}
