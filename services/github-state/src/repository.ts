import { DurableObject } from 'cloudflare:workers';
import { Env, EventPointer, Query, Result, digest, json, queryKey, repository, stub, validateQuery, parseGitHub, CI_EVENTS, NOT_MIRRORED } from './protocol';
import { comment, issue, pr, status } from './normalize';
import { batchLimit } from './catalog';
import { Lane, LANES, laneAt, settings } from './reconciliation';
import { SURVEY_CONTRACT, compactPR, issueFragment, reviewFragment, summarizeFragments, inScope, roadmapFocuses } from './survey';
import { readPromotionReceipts } from './activity_landings';
import { Migration, archiveKey, beginExport, expectedTables, exportStep, generation, encodeMigration, publicReceipt, readMigration, saveMigration, targetIdentity, verifyStep } from './migration';
import { backlog, controlKey, noteRepositoryFull, storageKey } from './ingress';
import { Checkpoint, DEFAULT_POLICY, Policy, RetentionClass, R2_CLASSES, RULES, policy, sweep } from './retention';
import { CapacitySample, RepairMetrics, RequestMetrics, SQLITE_ALLOWANCE, capacity, emitAlarm, timedBucket, within } from './monitoring';
import { Failure, FULL_MESSAGE, AdminError, CLEANUP, PARKABLE, classify, createRecovery, deleteClass, exportPage, faultySql, log, missingSchema, requestId, readManifest, scan, summary, userTables as summaryTables, verifyExport } from './storage';

const SCHEMA_VERSION=4;
// 10 GiB limit less a margin. With no recorded storage state, a database this
// large is treated as full: no startup write may risk a failed commit, which
// resets the object and would repeat on every start.
const FULL_DATABASE=10*1024**3-16*1024**2;
// Clients do not judge data age. While the store cannot apply changes, a
// mirror that has not changed for this long is reported unavailable so
// clients use their direct GitHub fallback instead of acting on frozen state.
const FROZEN_AFTER=10*60000;
const CURSOR_UPLOADS=4;
const cursorKey=(repo:string,id:string,offset:number)=>`cursors/${repo}/${id}/${offset}.json`;

export class RepositoryState extends DurableObject<Env> {
  private inflightQueries=new Map<string,Promise<Result>>();
  private completedQueries=new Map<string,{result:Result,expires:number}>();
  private snapshotFlight?:Promise<void>;
  sql:SqlStorage;
  // In-memory only: a full database cannot record that it is full. A restart
  // retries writes once and re-enters this mode on the first failure.
  degraded?:{since:number,reason:Failure,operation:string};
  schema:'current'|'legacy'|{error:Failure,missing:string[],cause:Failure}='current';
  private faultFull=false;
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
  private ciAfter=0;
  private obsoleteAfter=0;
  private ciProgress:any;
  private receiptsPinned=true;
  private receiptsChecked=0;
  private observation:any;
  private observationReadAt=0;
  private diagnostics:any={coverage:[],lanes:[],waits:[],repair_hour:[],requested_latency:[],queries:[],repair_operations:[]};
  private diagnosticsAt=0;
  private lastCapacity:any;
  private capacityHistory:CapacitySample[]=[];
  private tableEstimates:any;
  private tableAttemptAt=0;
  private requestMetrics=new RequestMetrics();
  private queryCounts=new Map<string,{operation:string,coverage:string,requests:number,last_at:number}>();
  private repairMetrics=new RepairMetrics();
  // Planned-migration barrier: loaded from R2 once per instance and updated
  // by the admin endpoint, which waits for in-flight applications to finish.
  private ingress?:string;
  private applying=0;
  private migrationState: Migration | null = null;
  private migrationUnavailable=false;
  private activeOperations=0;
  private migrationFlight: Promise<unknown> = Promise.resolve();
  migrationHeld(): boolean { return this.migrationUnavailable || Boolean(this.migrationState && this.migrationState.phase!=='abandoned'); }
  private logged=new Map<string,number>();
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx,env);
    this.sql=env.FAULT_INJECTION==='true'?faultySql(ctx.storage.sql,()=>this.faultFull):ctx.storage.sql;
    const sourceSql=this.sql;
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
      try {repo=this.meta('repository')?.id;} catch { /* Fresh test objects have no schema yet. */ }
      if(repo && this.env.ARCHIVE && this.env.MIGRATION_ENVIRONMENT==='test') {
        try {this.migrationState=await readMigration(this.env.ARCHIVE,repo);}
        catch {this.migrationUnavailable=true;}
      }
      await this.storageMarker();
      if (env.FAULT_INJECTION==='true' || this.schema!=='current') this.startup();
      await this.loadRetentionState();
    });
  }
  // A recorded full state degrades the object; a probe-verified writable state
  // overrides the size rule after cleanup, since freed pages do not shrink the file.
  async storageMarker(): Promise<void> {
    let marker:any;
    try {
      const repo=this.meta('repository')?.id;
      marker=repo?await this.env.ARCHIVE.get(storageKey(repo)).then(o=>o?.json<any>()):undefined;
    } catch {return;}
    if (marker?.state==='full') this.degraded={since:Number(marker.since) || Date.now(),reason:'storage_full',operation:'recorded:'+marker.source};
    else if (marker?.state==='writable' && this.degraded?.operation==='database_size') this.degraded=undefined;
  }
  markFull(operation:string): void {
    const repo=(()=>{try {return this.meta('repository')?.id;} catch {return undefined;}})();
    if (repo) noteRepositoryFull(this.env,repo,operation,this.degraded?.since ?? Date.now()).catch(()=>{});
  }
  faultKey(): string { return 'faults/'+this.ctx.id.toString()+'.json'; }
  // Existing current schemas start without any write. Migration is one
  // transaction; if it cannot commit, compatible stored data stays readable.
  startup(defer=false): void {
    let version:unknown;
    try {version=this.meta('schema_version');} catch {version=undefined;}
    if(version===SCHEMA_VERSION) {this.schema='current';return;}
    if(this.migrationHeld()) {this.schema='legacy';return;}
    if(this.degraded || defer && this.sql.exec<any>("SELECT COUNT(*) n FROM sqlite_master WHERE type='table' AND name='meta'").one().n) {
      // Never attempt a migration that cannot commit; existing data stays readable.
      let missing:string[];
      try {missing=missingSchema(this.sql);} catch {missing=['sqlite_master'];}
      this.schema=missing.length?{error:'schema_unavailable',missing,cause:'storage_full'}:'legacy';
      return;
    }
    try {this.ctx.storage.transactionSync(()=>this.migrate());this.schema='current';}
    catch(error) {
      let missing:string[];
      try {missing=missingSchema(this.sql);} catch {missing=['sqlite_master'];}
      const cause=this.noteFailure('migration',error);
      this.schema=missing.length?{error:'schema_unavailable',missing,cause}:'legacy';
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
      CREATE TABLE IF NOT EXISTS jobs(key TEXT PRIMARY KEY,query TEXT,priority INTEGER,due INTEGER,started INTEGER,cursor TEXT,page INTEGER DEFAULT 1,api TEXT,etag TEXT,attempt INTEGER DEFAULT 0,dirty INTEGER DEFAULT 0,generation INTEGER DEFAULT 0,start_generation INTEGER DEFAULT 0);
      CREATE INDEX IF NOT EXISTS jobs_due ON jobs(due,priority);
      CREATE TABLE IF NOT EXISTS demand(key TEXT PRIMARY KEY,at INTEGER);
      CREATE TABLE IF NOT EXISTS cursors(id TEXT PRIMARY KEY,expires INTEGER,payload TEXT);
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
    const columns=new Set(this.sql.exec<any>('PRAGMA table_info(jobs)').toArray().map(c=>c.name));
    for (const name of ['dirty','generation','start_generation']) if (!columns.has(name)) {
      this.sql.exec('ALTER TABLE jobs ADD COLUMN '+name+' INTEGER DEFAULT 0');
      if (name==='dirty') this.sql.exec('UPDATE jobs SET dirty=1 WHERE priority<=5 AND due<=?',Date.now());
    }
    for (const [name,type] of Object.entries({lane:"TEXT DEFAULT 'verification'",lease_id:'TEXT',lease_until:'INTEGER DEFAULT 0',requested_at:'INTEGER',created_at:'INTEGER',wait_reason:'TEXT',nested:'TEXT'})) if (!columns.has(name)) this.sql.exec('ALTER TABLE jobs ADD COLUMN '+name+' '+type);
    this.sql.exec('CREATE INDEX IF NOT EXISTS jobs_lane_due ON jobs(lane,due,lease_until)');
    if (!this.meta('reconciliation_version')) {
      this.sql.exec("UPDATE jobs SET created_at=?,lane=CASE WHEN EXISTS(SELECT 1 FROM demand d WHERE d.key=jobs.key AND d.at>?) AND NOT EXISTS(SELECT 1 FROM collections c WHERE c.key=jobs.key AND c.complete=1) THEN 'requested' WHEN dirty=1 OR priority<=5 THEN 'active' WHEN key LIKE 'recent_%' THEN 'archive' ELSE 'verification' END",Date.now(),Date.now()-300000);
      this.setMeta('reconciliation_version',2);
    }
    if (!this.meta('head_index_version')) {
      this.sql.exec("INSERT OR IGNORE INTO head_prs SELECT json_extract(data,'$.headRefOid'),id FROM resources WHERE kind='pr' AND id NOT LIKE 'pr:%' AND json_extract(data,'$.headRefOid') IS NOT NULL");
      this.setMeta('head_index_version',1);
    }
    if (!this.meta('workflow_scope_version')) {
      this.setMeta('workflow_since',new Date(Date.now()-3600000).toISOString());
      this.setMeta('workflow_scope_version',1);
      this.sql.exec('UPDATE jobs SET started=NULL,cursor=NULL,page=1,api=NULL,etag=NULL,due=? WHERE key=?',Date.now(),queryKey({operation:'workflow_runs'}));
    }
    // Delta surveys must retain their place ahead of a large bootstrap backlog.
    this.sql.exec("UPDATE jobs SET priority=0 WHERE (key LIKE 'pr_delta:%' OR key LIKE 'issue_delta:%') AND priority!=0");
    this.setMeta('schema_version',SCHEMA_VERSION);
  }
  noteFailure(operation:string,error:unknown): Failure {
    const reason=classify(error);
    if(reason==='storage_full' && !this.degraded) {this.degraded={since:Date.now(),reason,operation};this.markFull(operation);}
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
  optional<T>(operation:string,write:()=>T): T | undefined {
    if(this.degraded || this.migrationHeld()) return undefined;
    try {return write();} catch(error) {this.noteFailure(operation,error);return undefined;}
  }
  async optionalAsync(operation:string,write:()=>Promise<unknown>): Promise<void> {
    if(this.degraded || this.migrationHeld()) return;
    try {await write();} catch(error) {this.noteFailure(operation,error);}
  }
  async setAlarm(at:number): Promise<void> {
    if(this.faultFull) throw new Error(FULL_MESSAGE);
    await this.ctx.storage.setAlarm(at);
  }
  meta(key: string): any { const row = this.sql.exec<any>('SELECT value FROM meta WHERE key=?',key).toArray()[0]; return row ? JSON.parse(row.value) : undefined; }
  setMeta(key:string,value:unknown): void { this.sql.exec('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',key,JSON.stringify(value)); }
  revision(): number { return this.meta('revision') || 0; }
  nextRevision(kind:string,id:string,tombstone=false): number {
    const revision = this.revision()+1; this.setMeta('revision',revision);
    this.sql.exec('INSERT INTO changes VALUES(?,?,?,?,?)',revision,Date.now(),kind,id,Number(tombstone)); return revision;
  }
  async encode(data: unknown, expires?:number): Promise<string> {
    const text = JSON.stringify(data);
    if (new TextEncoder().encode(text).length < 1500000) return text;
    const key = `${expires?'staging-spills':'resources'}/${this.meta('repository').id}/${crypto.randomUUID()}`;
    // Active resources must remain readable for their entire SQL row lifetime.
    // Raw deliveries, staging pages, and snapshots have separate expiry rules.
    await this.env.ARCHIVE.put(key,text,{customMetadata:{kind:expires?'cursor':'resource',...(expires?{expires:String(expires)}:{})}});
    return JSON.stringify({$r2:key});
  }
  async decode(text: string): Promise<any> {
    const data = JSON.parse(text);
    if (data?.$r2) { const object = await this.env.ARCHIVE.get(data.$r2); if (!object) throw new Error('resource unavailable'); return object.json(); }
    return data;
  }
  async *decodeRows(rows:{data:string}[]):AsyncGenerator<any> {
    // Archived collection reads preserve order with a bounded R2 fanout.
    for(let i=0;i<rows.length;i+=8)for(const data of await Promise.all(rows.slice(i,i+8).map(r=>this.decode(r.data))))yield data;
  }
  async put(kind:string,id:string,data:any,started=Date.now(),deleted=false,parent='',fromEvent=false): Promise<boolean> {
    let old = this.sql.exec<any>('SELECT * FROM resources WHERE kind=? AND id=?',kind,id).toArray()[0];
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
    const encoded = await this.encode(data);
    // R2 encoding yields: repeat the guard against events received during that await.
    old=this.sql.exec<any>('SELECT * FROM resources WHERE kind=? AND id=?',kind,id).toArray()[0];
    if (!fromEvent && old?.observed>started || old?.updated && updated && old.updated>updated || old?.updated && updated===old.updated && old.tombstone && !deleted) return false;
    if (kind==='pr' && /^[1-9][0-9]*$/.test(id) && data?.headRefOid) this.sql.exec('INSERT OR IGNORE INTO head_prs VALUES(?,?)',data.headRefOid,id);
    if (old?.data===encoded && Boolean(old.tombstone)===deleted) {
      this.sql.exec('UPDATE resources SET observed=? WHERE kind=? AND id=?',Date.now(),kind,id);
      return true;
    }
    const rev = this.nextRevision(kind,id,deleted);
    this.sql.exec('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET parent=excluded.parent,data=excluded.data,updated=excluded.updated,observed=excluded.observed,revision=excluded.revision,tombstone=excluded.tombstone',kind,id,parent,encoded,updated,Date.now(),rev,Number(deleted));
    if(kind==='pr' && /^[1-9][0-9]*$/.test(id)) this.sql.exec('INSERT INTO survey_inventory VALUES(?,?) ON CONFLICT(number) DO UPDATE SET data=excluded.data',id,JSON.stringify(compactPR(data)));
    if(['issue_comments','review_comments'].includes(kind)) {
      try {this.projectSurvey(kind,id,parent,data,deleted);this.dirtySurvey(parent);}
      catch {this.invalidateSurvey(parent);}
    }
    return true;
  }
  membership(key:string,id:string,included:boolean): void {
    const exists=this.sql.exec('SELECT id FROM members WHERE collection=? AND id=?',key,id).toArray().length>0;
    if (exists===included) return;
    if (included) this.sql.exec('INSERT INTO members VALUES(?,?)',key,id);
    else this.sql.exec('DELETE FROM members WHERE collection=? AND id=?',key,id);
    this.nextRevision('collection',key);
    if(key.startsWith('issue_comments:') || key.startsWith('review_comments:')) {
      const args=JSON.parse(key.slice(key.indexOf(':')+1));
      if(args.number) this.dirtySurvey(String(args.number));
    }
  }
  projectSurvey(kind:string,id:string,parent:string,data:any,deleted=false): void {
    if(!parent || !this.sql.exec("SELECT id FROM resources WHERE kind='pr' AND id=?",parent).toArray().length) return;
    const fragment=kind==='issue_comments'?issueFragment(data):reviewFragment(data);
    const useful=kind==='issue_comments'?fragment.meta!==null || fragment.marker || fragment.rebase_heads.length || fragment.blocked.length:fragment.root || fragment.parent && fragment.reply;
    if(deleted || !useful) this.sql.exec('DELETE FROM survey_fragments WHERE kind=? AND id=?',kind,id);
    else this.sql.exec('INSERT INTO survey_fragments VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET parent=excluded.parent,data=excluded.data',kind,id,parent,JSON.stringify(fragment));
  }
  projectedSurvey(number:string): string {
    const read=(kind:string)=>this.sql.exec<any>('SELECT f.data FROM survey_fragments f JOIN members m ON m.id=f.id AND m.collection=? WHERE f.kind=? AND f.parent=? ORDER BY CAST(f.id AS INTEGER),f.id',queryKey({operation:kind,args:{number:Number(number)}}),kind,number).toArray().map(row=>JSON.parse(row.data));
    return JSON.stringify(summarizeFragments(read('issue_comments'),read('review_comments')));
  }
  rebuildSurvey(number:string,data=this.projectedSurvey(number)): void {
    this.sql.exec('INSERT INTO survey_state VALUES(?,?,?) ON CONFLICT(number) DO UPDATE SET data=excluded.data,revision=excluded.revision',number,data,this.revision());
    this.sql.exec('DELETE FROM survey_dirty WHERE number=?',number);
  }
  dirtySurvey(number:string): void {
    if(number && this.sql.exec("SELECT id FROM resources WHERE kind='pr' AND id=?",number).toArray().length)
      this.sql.exec('INSERT OR IGNORE INTO survey_dirty VALUES(?)',number);
  }
  invalidateSurvey(number:string): void {
    this.sql.exec('DELETE FROM survey_hydrated WHERE number=?',number);
    this.sql.exec('INSERT INTO survey_hydration_failures VALUES(?,?) ON CONFLICT(number) DO UPDATE SET retry_at=excluded.retry_at',number,Date.now()+300000);
    this.nextRevision('survey_failure',number);
  }
  async hydrateSurvey(number:string): Promise<void> {
    // Existing deployments already have complete histories. Project those
    // locally once, in bounded batches, without any new GitHub requests.
    const prRow=this.sql.exec<any>("SELECT data,revision FROM resources WHERE kind='pr' AND id=?",number).toArray()[0];
    if(prRow) {
      const data=await this.decode(prRow.data);
      if(this.sql.exec<any>("SELECT revision FROM resources WHERE kind='pr' AND id=?",number).toArray()[0]?.revision===prRow.revision)
        this.sql.exec('INSERT INTO survey_inventory VALUES(?,?) ON CONFLICT(number) DO UPDATE SET data=excluded.data',number,JSON.stringify(compactPR(data)));
    }
    const rows=this.sql.exec<any>("SELECT kind,id,parent,data,revision FROM resources WHERE kind IN ('issue_comments','review_comments') AND parent=? AND tombstone=0",number).toArray();
    for(const row of rows) {
      const data=await this.decode(row.data);
      const current=this.sql.exec<any>('SELECT revision,tombstone FROM resources WHERE kind=? AND id=?',row.kind,row.id).toArray()[0];
      if(current?.revision===row.revision && !current.tombstone) this.projectSurvey(row.kind,row.id,number,data);
    }
    this.rebuildSurvey(number);
    this.nextRevision('survey',number);
    this.sql.exec('INSERT INTO survey_hydrated VALUES(?,?) ON CONFLICT(number) DO UPDATE SET contract=excluded.contract',number,SURVEY_CONTRACT);
    this.sql.exec('DELETE FROM survey_hydration_failures WHERE number=?',number);
  }
  async surveyRows(q:Query): Promise<{rows:any[],author_counts:Record<string,number>} | null> {
    const inventory={repository:q.repository,operation:'open_prs'};
    await this.optionalAsync('demand',()=>this.noteDemand(inventory));
    if(!this.complete(inventory)) {await this.optionalAsync('enqueue',()=>this.enqueue(inventory,5));return null;}
    // Scan compact local metadata, then add statuses only for the selected
    // roadmap. Descriptions and unrelated comment histories stay in storage.
    const rows=this.sql.exec<any>("SELECT COALESCE(s.data,r.data) data FROM members m JOIN resources r ON r.kind='pr' AND r.id=m.id LEFT JOIN survey_inventory s ON s.number=r.id WHERE m.collection=? AND r.tombstone=0 ORDER BY CAST(r.id AS INTEGER),r.id",queryKey(inventory)).toArray();
    const scope=String(q.args?.roadmap || 'any'),skip=String(q.args?.skip || '').split(',').filter(Boolean),out=[];
    const author_counts:Record<string,number>={},stages=new Set(String(q.args?.stages ?? 'review,fix,rebase').split(','));
    for(const storedRow of rows) {
      const row=compactPR(await this.decode(storedRow.data));
      if(row.state && row.state!=='OPEN') continue;
      const areas=roadmapFocuses((row.labels || []).map((label:any)=>label.name),row.target_focuses,row.headRefName);
      if(!row.isDraft && (areas===null || areas.some(area=>!skip.includes(area)))) {
        const author=row.author?.login || '';author_counts[author]=(author_counts[author] || 0)+1;
      }
      if(q.args?.numbers ? !String(q.args.numbers).split(',').includes(String(row.number)) : !inScope(row,scope,skip)) continue;
      const number=String(row.number);let hydrated=this.sql.exec<any>('SELECT contract FROM survey_hydrated WHERE number=?',number).toArray()[0]?.contract===SURVEY_CONTRACT;
      let projected=this.sql.exec<any>('SELECT data FROM survey_state WHERE number=?',number).toArray()[0]?.data;
      if(hydrated && this.sql.exec('SELECT number FROM survey_dirty WHERE number=?',number).toArray().length) {
        // The projection is derived: serve it from fragments even when the
        // cache cannot be written, and persist it only when storage allows.
        try {projected=this.projectedSurvey(number);}catch {hydrated=false;this.optional('survey_invalidate',()=>this.invalidateSurvey(number));}
        if(hydrated) {const data=projected;this.optional('survey_projection',()=>this.rebuildSurvey(number,data));}
      }
      const review={...(hydrated && projected?JSON.parse(projected):summarizeFragments([],[])),issue_complete:false,review_complete:false};
      const statuses=await this.rows({repository:q.repository,operation:'statuses',args:{sha:row.headRefOid}});
      const green=Array.isArray(statuses) && statuses.some(c=>c.context==='build' && c.state==='SUCCESS') && !statuses.some(c=>c.context==='build' && c.state!=='SUCCESS');
      for(const [operation,field] of [['issue_comments','issue_complete'],['review_comments','review_complete']]) {
        const child={repository:q.repository,operation,args:{number:row.number}};
        review[field]=hydrated && this.complete(child);
        // Demand is deduplicated in the installation scheduler; serving a
        // snapshot never performs synchronous upstream validation.
        const needs=operation==='issue_comments'?(green && (stages.has('review') || stages.has('fix'))) || stages.has('rebase') && (row.labels || []).some((l:any)=>l.name==='needs-rebase'):green && (stages.has('review') || stages.has('fix'));
        if(!row.isDraft && needs && !this.complete(child)) {await this.optionalAsync('demand',()=>this.noteDemand(child));await this.optionalAsync('enqueue',()=>this.enqueue(child,5));}
      }
      out.push({contract:SURVEY_CONTRACT,pr:{...compactPR(row),statusCheckRollup:Array.isArray(statuses)?statuses:[]},review});
    }
    return {rows:out,author_counts};
  }
  surveyHealth(): any {
    const counts=this.sql.exec<any>("SELECT COUNT(*) open_prs,SUM(h.contract=?) hydrated,SUM(s.number IS NOT NULL) materialized FROM members m JOIN resources r ON r.kind='pr' AND r.id=m.id LEFT JOIN survey_hydrated h ON h.number=r.id LEFT JOIN survey_state s ON s.number=r.id WHERE m.collection=? AND r.tombstone=0",SURVEY_CONTRACT,queryKey({operation:'open_prs'})).one();
    const failures=this.sql.exec<any>('SELECT COUNT(*) n FROM survey_hydration_failures').one().n;
    return {contract:SURVEY_CONTRACT,...counts,failures};
  }
  id(operation:string,row:any): string { return String(row.number || row.id || row.node_id || row.ref || row.context || row.sha || row.headRefOid || 'value'); }
  async enqueue(q:Query,priority=20,due=Date.now(),dirty=false): Promise<void> {
    if (q.operation==='progress' || NOT_MIRRORED.has(q.operation)) return;
    const key=queryKey(q);
    const prior=this.sql.exec<any>('SELECT * FROM jobs WHERE key=?',key).toArray()[0];
    const lane=this.lane(q,dirty || Boolean(prior?.dirty),priority),requested=lane==='requested'?(prior?.requested_at || Date.now()):prior?.requested_at || null;
    this.sql.exec('INSERT INTO jobs(key,query,priority,due,dirty,generation,lane,requested_at,created_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET priority=excluded.priority,lane=excluded.lane,requested_at=excluded.requested_at,dirty=MAX(dirty,excluded.dirty),generation=generation+excluded.generation' + (dirty ? ",due=CASE WHEN wait_reason IS NOT NULL OR attempt>0 THEN due ELSE MIN(due,excluded.due) END" : ''),key,JSON.stringify({...q,allow_observed:undefined}),priority,due,Number(dirty),Number(dirty),lane,requested,Date.now());
    if (!prior) this.metric('arrival',q,lane);
    const alarm=await this.ctx.storage.getAlarm();
    const scheduled=this.sql.exec<any>('SELECT due FROM jobs WHERE key=?',key).one().due;
    if (!alarm || alarm>scheduled) await this.setAlarm(Math.max(Date.now()+100,scheduled));
  }
  complete(q:Pick<Query,'operation'|'args'>): boolean { return this.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',queryKey(q)).toArray().length>0; }
  metric(event:string,q:Query,lane:Lane,value=1): void {
    // Telemetry cannot turn a successfully applied page/job into a retry.
    try {this.repairMetrics.record(event,lane,q.operation,value);}catch(error) {try {this.noteFailure('repair_metrics',error);}catch {}}
  }
  lane(q:Query,dirty=false,priority=20): Lane {
    const demanded=(this.sql.exec<any>('SELECT at FROM demand WHERE key=?',queryKey(q)).toArray()[0]?.at || 0)>Date.now()-300000;
    if (demanded && !this.complete(q)) return 'requested';
    if (dirty || priority<=5 || q.operation.endsWith('_delta')) return 'active';
    return q.operation.startsWith('recent_') || this.dormant(q)?'archive':'verification';
  }
  importantWorkflow(id:string): boolean {
    const row=this.sql.exec<any>('SELECT data FROM resources WHERE kind=? AND id=?','workflow_runs',id).toArray()[0];
    return Boolean(row && ['staging','trying'].includes(JSON.parse(row.data).head_branch));
  }
  importantPending(id:string): boolean {
    return this.importantWorkflow(id) && (!this.meta('workflow_finished:'+id) || Boolean(this.meta('important_final_pending:'+id)));
  }
  noteWorkflow(id:string,row:any): void {
    this.setMeta('workflow_finished:'+id,row.status==='completed');
    this.setMeta('workflow_seen_at:'+id,Date.now());
    if(this.importantWorkflow(id)) {
      if(row.status!=='completed') this.setMeta('important_active:'+id,true);
      else if(this.meta('important_active:'+id)) this.setMeta('important_final_pending:'+id,true);
    }
  }
  runAttempt(id:string): number {
    const known=this.meta('workflow_attempt:'+id);
    if(known) return known;
    const cached=this.sql.exec<any>('SELECT data FROM resources WHERE kind=? AND id=?','workflow_runs',id).toArray()[0];
    if(!cached) return 1;
    const attempt=Number(JSON.parse(cached.data).run_attempt || 1);
    if(attempt===1) {this.optional('workflow_attempt',()=>this.setMeta('workflow_attempt:'+id,attempt));return attempt;}
    // Legacy mirrors did not scope membership by attempt. Revalidate once;
    // retain current webhook rows and all historical resource records. This
    // must commit before the legacy membership can be served.
    if(this.migrationHeld()) throw new AdminError('migration_frozen');
    if(this.degraded) throw new Error('Exceeded the maximum database size; workflow attempt revalidation needs a write (degraded)');
    this.ctx.storage.transactionSync(()=>{
      this.setMeta('workflow_attempt:'+id,attempt);
      const key=queryKey({operation:'workflow_jobs',args:{id}});
      this.sql.exec('UPDATE collections SET complete=0,etag=NULL WHERE key=?',key);
      this.sql.exec("DELETE FROM members WHERE collection=? AND id IN (SELECT id FROM resources WHERE kind='workflow_jobs' AND COALESCE(json_extract(data,'$.run_attempt'),1)!=?)",key,attempt);
      this.sql.exec('UPDATE jobs SET started=NULL,cursor=NULL,page=1,api=NULL,etag=NULL,nested=NULL,lease_id=NULL,lease_until=0,dirty=1,generation=generation+1 WHERE key=?',key);
      this.nextRevision('collection',key);
    });
    return attempt;
  }
  workflowAttempt(id:string,attempt:number): boolean {
    const prior=this.runAttempt(id);
    if (attempt<prior) return false;
    if (attempt>prior) {
      const key=queryKey({operation:'workflow_jobs',args:{id}});
      this.sql.exec('DELETE FROM members WHERE collection=?',key);
      this.sql.exec('UPDATE collections SET complete=0,etag=NULL WHERE key=?',key);
      this.sql.exec('UPDATE jobs SET started=NULL,cursor=NULL,page=1,api=NULL,etag=NULL,nested=NULL,lease_id=NULL,lease_until=0,generation=generation+1 WHERE key=?',key);
      this.nextRevision('collection',key);
      this.setMeta('workflow_finished:'+id,false);
      this.setMeta('important_final_pending:'+id,false);
      this.setMeta('important_active:'+id,false);
    }
    this.setMeta('workflow_attempt:'+id,attempt);
    return true;
  }
  async configure(name:string): Promise<void> {
    const repo=repository(this.env,name); if (!repo) throw new Error('repository not allowed');
    this.setMeta('repository',repo);
    for (const operation of ['repository','refs','open_prs','open_issues','recent_prs','recent_issues','pr_delta','issue_delta']) await this.enqueue({repository:repo.name,operation},operation==='open_prs' || operation.endsWith('_delta')?0:10);
    await this.setAlarm(Date.now()+100);
  }
  async ingest(pointer:EventPointer): Promise<void> {
    // Queued or parked CI events from before mirroring stopped need no receipt.
    if (CI_EVENTS.has(pointer.event)) return;
    // Receipt retention counts from application, not original receipt: a
    // replay of a long-parked event must keep deduplicating its cleanup.
    if (this.sql.exec('SELECT id FROM deliveries WHERE id=?',pointer.delivery).toArray().length) return;
    if (this.degraded) throw new Error('Exceeded the maximum database size; SQLITE_FULL recorded at '+new Date(this.degraded.since).toISOString());
    const object=await this.env.ARCHIVE.get(pointer.key); if (!object) throw new Error('event body unavailable');
    const body:any=parseGitHub(await object.text()); const repo=repository(this.env,pointer.repository); if (!repo) throw new Error('repository not allowed');
    this.setMeta('repository',repo);
    const event=pointer.event, deleted=body.action==='deleted';
    // All updates are idempotent; receipt is committed only after their durable rows and jobs.
    if (event==='pull_request' && body.pull_request) {
      const row=pr(body.pull_request), number=String(row.number);
      const before=this.sql.exec<any>('SELECT data FROM resources WHERE kind=? AND id=?','pr',number).toArray()[0];
      const prior=before?await this.decode(before.data):null;
      const accepted=await this.put('pr',number,row,pointer.received,false,number,true);
      // A webhook describes one PR, not a complete inventory. Never create fake coverage.
      const key=queryKey({operation:'open_prs'});
      if (accepted) this.membership(key,number,body.pull_request.state==='open');
      const detail={repository:repo.name,operation:'pr',args:{number:row.number}};
      const detailKnown=this.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',queryKey(detail)).toArray().length>0;
      await this.enqueue(detail,0,Date.now(),!detailKnown || row.mergeable==='UNKNOWN');
      const inventoryKnown=this.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',key).toArray().length>0;
      await this.enqueue({repository:repo.name,operation:'open_prs'},1,Date.now(),!inventoryKnown);
      if(accepted && row.headRefOid && prior?.headRefOid!==row.headRefOid) await this.enqueue({repository:repo.name,operation:'files',args:{number:row.number}},1,Date.now(),true);
      if (accepted && row.headRefOid) for (const operation of ['statuses','checks']) {
        const q={repository:repo.name,operation,args:{sha:row.headRefOid}};
        const known=this.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',queryKey(q)).toArray().length>0;
        await this.enqueue(q,1,Date.now(),!known || prior?.headRefOid!==row.headRefOid);
      }
    }
    if (event==='issues' && body.issue) {
      const row=issue(body.issue); const accepted=await this.put('issue',String(row.number),row,pointer.received,deleted,String(row.number),true);
      if (accepted) this.membership(queryKey({operation:'open_issues'}),String(row.number),!deleted && row.state==='OPEN');
      const q={repository:repo.name,operation:'open_issues'};
      const known=this.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',queryKey(q)).toArray().length>0;
      await this.enqueue(q,1,Date.now(),!known);
    }
    if (['issue_comment','pull_request_review_comment'].includes(event) && body.comment) {
      const operation=event==='issue_comment'?'issue_comments':'review_comments';
      const number=body.issue?.number || body.pull_request?.number;
      const row=comment(body.comment); const accepted=await this.put(operation,String(row.id),row,pointer.received,deleted,String(number),true);
      const key=queryKey({operation,args:{number}});
      if (accepted) this.membership(key,String(row.id),!deleted);
      const known=this.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',key).toArray().length>0;
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
      const known=this.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',queryKey(q)).toArray().length>0;
      await this.enqueue(q,0,Date.now(),!known);
    }
    if (event==='check_run' || event==='check_suite') {
      const sha=body.check_run?.head_sha || body.check_suite?.head_sha;
      if (sha) {
        const q={repository:repo.name,operation:'checks',args:{sha}},key=queryKey(q);
        if (body.check_run?.id) {
          const row={...body.check_run,updated_at:body.check_run.completed_at || body.check_run.started_at || new Date(pointer.received).toISOString()};
          if (await this.put('checks',String(row.id),row,pointer.received,false,sha,true)) this.membership(key,String(row.id),true);
        }
        const known=this.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',key).toArray().length>0;
        await this.enqueue(q,0,Date.now(),!known);
      }
    }
    if (event==='workflow_run' || event==='workflow_job') {
      const id=body.workflow_run?.id || body.workflow_job?.run_id;
      const attempt=Number(body.workflow_run?.run_attempt || body.workflow_job?.run_attempt || this.meta('workflow_attempt:'+id) || 1);
      if (id && !this.workflowAttempt(String(id),attempt)) {
        this.sql.exec('INSERT OR IGNORE INTO deliveries VALUES(?,?,?)',pointer.delivery,event,Date.now());
        return;
      }
      if (body.workflow_run) {
        const row=body.workflow_run;
        if (await this.put('workflow_runs',String(row.id),row,pointer.received,false,'',true)) {
          this.membership(queryKey({operation:'workflow_runs'}),String(row.id),true);
          this.noteWorkflow(String(row.id),row);
        }
      }
      if (id) {
        const q={repository:repo.name,operation:'workflow_jobs',args:{id:String(id)}},key=queryKey(q);
        if (body.workflow_job) {
          const row={...body.workflow_job,updated_at:body.workflow_job.completed_at || body.workflow_job.started_at || new Date(pointer.received).toISOString()};
          if (await this.put('workflow_jobs',String(row.id),row,pointer.received,false,String(id),true)) {
            this.membership(key,String(row.id),true);
            this.setMeta('workflow_seen_at:'+id,Date.now());
            if (row.status!=='completed') this.setMeta('workflow_finished:'+id,false);
          }
        }
        const demanded=(this.sql.exec<any>('SELECT at FROM demand WHERE key=?',key).toArray()[0]?.at || 0)>Date.now()-300000;
        // Observing a job does not establish exhaustive membership or create a baseline obligation.
        if (demanded || this.importantPending(String(id))) await this.enqueue(q,1,Date.now(),Boolean(body.workflow_run) || !this.complete(q));
      }
      const q={repository:repo.name,operation:'workflow_runs'};
      const known=this.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',queryKey(q)).toArray().length>0;
      await this.enqueue(q,1,Date.now(),!known);
    }
    if (['push','create','delete','repository','label','milestone','merge_group'].includes(event)) {
      if (event==='push' && String(body.ref).startsWith('refs/heads/')) {
        const row={ref:body.ref,object:{sha:body.after,type:'commit',url:'https://api.github.com/repos/'+repo.name+'/git/commits/'+body.after},updated_at:new Date(pointer.received).toISOString()};
        const removed=Boolean(body.deleted) || /^0+$/.test(body.after || '');
        if (await this.put('refs',body.ref,row,pointer.received,removed,'',true)) this.membership(queryKey({operation:'refs'}),body.ref,!removed);
      }
      // Header and CI webhooks already update the indexed records. A single
      // changed PR must not trigger a repository-wide survey at fleet scale.
      for (const operation of ['open_prs','open_issues','refs','repository']) {
        const q={repository:repo.name,operation};
        const known=this.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',queryKey(q)).toArray().length>0;
        await this.enqueue(q,2,Date.now(),!known || operation==='repository' && event==='repository' || operation==='refs' && ['create','delete'].includes(event));
      }
    }
    this.sql.exec('INSERT OR IGNORE INTO deliveries VALUES(?,?,?)',pointer.delivery,event,Date.now());
    this.setMeta('last_webhook',Math.max(pointer.received,this.meta('last_webhook') || 0));
    this.setMeta('webhook_apply_ms',Math.max(0,Date.now()-pointer.received));
  }
  kind(q:Query): string { return ['open_prs','recent_prs','pr','pr_delta'].includes(q.operation)?'pr':['open_issues','recent_issues','issue_delta'].includes(q.operation)?'issue':q.operation; }
  async rows(q:Query): Promise<any[] | any | null> {
    const key=queryKey(q), collection=this.sql.exec<any>('SELECT * FROM collections WHERE key=? AND complete=1',key).toArray()[0];
    if (!collection && !(q.operation==='workflow_jobs' && q.allow_observed && this.meta('workflow_attempt:'+q.args?.id))) return null;
    if (q.operation==='pr') {
      const row=this.sql.exec<any>('SELECT data FROM resources WHERE kind=? AND id=? AND tombstone=0','pr',String(q.args?.number)).toArray()[0];
      if (row) return this.decode(row.data);
    }
    if (['pr','repository','statuses'].includes(q.operation)) {
      const row=this.sql.exec<any>('SELECT data FROM resources WHERE kind=? AND id=? AND tombstone=0',q.operation,key).toArray()[0];
      if (!row) return null;
      const data=await this.decode(row.data);
      if (q.operation!=='statuses' || !Array.isArray(data)) return data;
      const contexts=new Map(data.map(n=>{const value=status(n);return [value.context,value];}));
      for (const row of this.sql.exec<any>('SELECT data FROM resources WHERE kind=? AND parent=? AND tombstone=0','status_context',String(q.args?.sha)).toArray()) {
        const value=status(await this.decode(row.data)),prior=contexts.get(value.context);
        if (!prior || Date.parse(value.startedAt)>=Date.parse(prior.startedAt)) contexts.set(value.context,value);
      }
      return [...contexts.values()];
    }
    const members=this.sql.exec<any>('SELECT r.data FROM members m JOIN resources r ON r.kind=? AND r.id=m.id WHERE m.collection=? AND r.tombstone=0 ORDER BY CAST(r.id AS INTEGER),r.id',this.kind(q),key).toArray();
    const rows=[];
    for await(const data of this.decodeRows(members)) {
      if (q.operation==='workflow_jobs' && Number(data.run_attempt || 1)!==(this.meta('workflow_attempt:'+q.args?.id) || 1)) continue;
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
  unavailable(q:Query,reason:string): Result {
    return {version:1,repository:q.repository,operation:q.operation,status:'unavailable',revision:this.revision(),complete:false,reason};
  }
  async query(q:Query): Promise<Result> {
    validateQuery(q);
    if (NOT_MIRRORED.has(q.operation)) return this.unavailable(q,'not_mirrored');
    if (!this.meta('repository')) {
      try {await this.configure(q.repository);}
      catch(error) {return this.unavailable(q,this.noteFailure('configure',error));}
    }
    const key=queryKey(q);
    if(q.operation==='workflow_jobs') {
      try {this.runAttempt(String(q.args?.id));}
      catch(error) {return this.unavailable(q,this.noteFailure('workflow_attempt',error));}
    }
    if (!q.allow_observed && q.operation!=='survey') await this.optionalAsync('demand',()=>this.noteDemand(q));
    if (q.cursor) return this.continuation(q,key);
    let rows:any;
    let author_counts:Record<string,number> | undefined;
    if(q.operation==='survey') {
      const snapshot=await this.surveyRows(q);rows=snapshot?.rows ?? null;author_counts=snapshot?.author_counts;
    } else if (q.operation==='progress') {
      const detail={...q,operation:'pr'};
      const p=await this.rows(detail);
      await this.optionalAsync('demand',()=>this.noteDemand(detail));
      if(!this.complete(detail)) await this.optionalAsync('enqueue',()=>this.enqueue(detail,5));
      const cs=await this.rows({...q,operation:'issue_comments'}), rcs=await this.rows({...q,operation:'review_comments'});
      for (const operation of ['issue_comments','review_comments']) {
        const child={...q,operation};
        await this.optionalAsync('demand',()=>this.noteDemand(child));
        if (!this.complete(child)) await this.optionalAsync('enqueue',()=>this.enqueue(child,5));
      }
      rows=p && cs && rcs ? {head:p.headRefOid,state:p.state,ncomments:cs.length+rcs.length,revision:this.revision()}:null;
    } else {
      if (!q.allow_observed && !this.complete(q)) await this.optionalAsync('enqueue',()=>this.enqueue(q,5));
      rows=await this.rows(q);
    }
    const complete=rows!==null && (q.operation==='survey' || q.operation==='progress' || this.complete(q));
    const {observed,verified}=this.freshness(q);
    const result:Result={version:1,repository:q.repository,operation:q.operation,status:rows===null?'unavailable':'ok',revision:this.revision(),observed_at:observed || verified,verified_at:verified,complete,coverage:complete?'complete':q.allow_observed?'webhook_observed':undefined};
    if(q.operation==='survey') {result.contract=SURVEY_CONTRACT;result.author_counts=author_counts;}
    if (rows===null) return {...result,reason:'coverage_unavailable'};
    if (Array.isArray(rows) && rows.length>100) {
      const id=crypto.randomUUID(),expires=Date.now()+600000,repo=this.meta('repository').id;
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
  freshness(q:Query): {observed?:number,verified?:number} {
    const args=q.args,number=q.args?.number;
    const sources=q.operation==='survey'?[{key:queryKey({operation:'open_prs'}),kind:'pr'}]
      :q.operation==='progress'?[{key:queryKey({operation:'pr',args}),kind:'pr',id:String(number)},{key:queryKey({operation:'issue_comments',args}),kind:'issue_comments'},{key:queryKey({operation:'review_comments',args}),kind:'review_comments'}]
      :[{key:queryKey(q),kind:this.kind(q)}];
    let observed:number | undefined,verified:number | undefined;
    for (const source of sources as {key:string,kind:string,id?:string}[]) {
      const at=source.id?this.sql.exec<any>('SELECT observed at FROM resources WHERE kind=? AND id=?',source.kind,source.id).toArray()[0]?.at
        :this.sql.exec<any>('SELECT MAX(r.observed) at FROM members m JOIN resources r ON r.kind=? AND r.id=m.id WHERE m.collection=?',source.kind,source.key).one().at;
      const checked=this.sql.exec<any>('SELECT verified FROM collections WHERE key=?',source.key).toArray()[0]?.verified;
      if (at) observed=Math.max(observed ?? 0,at);
      if (checked) verified=verified===undefined?checked:Math.min(verified,checked);
    }
    return {observed,verified};
  }
  async continuation(q:Query,key:string): Promise<Result> {
    const cursor=q.cursor!,now=Date.now();
    // Legacy SQL cursors remain readable until they expire.
    const row=this.sql.exec<any>('SELECT payload FROM cursors WHERE (id=? OR id=?) AND expires>? ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END LIMIT 1',cursor,cursor.split(':')[0],now,cursor).toArray()[0];
    let payload:any;
    if (row) payload=await this.decode(row.payload);
    else {
      const match=cursor.match(/^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):([1-9][0-9]{0,8})$/);
      const object=match?await this.env.ARCHIVE.get(cursorKey(this.meta('repository')?.id,match[1],Number(match[2]))):null;
      payload=object?await object.json():null;
      if (!payload || payload.repository!==this.meta('repository')?.id || !(payload.expires>now)) return this.unavailable(q,'cursor_expired');
    }
    if (payload.key!==key) return this.unavailable(q,'cursor_query_mismatch');
    if (Boolean(payload.allow_observed)!==Boolean(q.allow_observed)) return this.unavailable(q,'cursor_coverage_mismatch');
    if (payload.result?.data!==undefined) return payload.result;
    // Existing ten-minute cursors survive a rolling deployment. Only an offset
    // inside the stored snapshot can continue it; never fabricate an empty page.
    const legacy=cursor.match(/^([^:]+):([1-9][0-9]{0,8})$/),offset=Number(legacy?.[2]),id=legacy?.[1] || '';
    if (!legacy || !Array.isArray(payload.rows) || offset>=payload.rows.length) return this.unavailable(q,'cursor_invalid');
    return this.page(payload.result,payload.rows,id,offset);
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
  interval(q:Query): number {
    if (q.operation==='pr_delta' || q.operation==='issue_delta') return 60000;
    if (q.operation==='open_prs' || q.operation==='open_issues') return 3600000;
    if (q.operation==='files') return 365*86400000; // head-change webhook dirties this immutable-by-head read
    if (q.operation==='reactions') {
      const active=this.sql.exec<any>('SELECT at FROM demand WHERE key=?',queryKey(q)).toArray()[0];
      return active?.at>Date.now()-300000?300000:86400000;
    }
    if (q.operation==='reviews' || q.operation==='checks') return 6*3600000;
    const demand=this.sql.exec<any>('SELECT at FROM demand WHERE key=?',queryKey(q)).toArray()[0];
    if (q.operation==='statuses') return demand?.at>Date.now()-300000?300000:3600000;
    if (q.operation==='workflow_jobs') return 60000;
    if (q.operation==='workflow_runs') return demand?.at>Date.now()-300000?60000:3600000;
    if (['issue_comments','review_comments','review_threads','reviews','reactions'].includes(q.operation)) return demand?.at>Date.now()-300000?300000:3600000;
    return 3600000;
  }
  priority(q:Query): number {
    if (q.operation.endsWith('_delta')) return 0;
    const demand=this.sql.exec<any>('SELECT at FROM demand WHERE key=?',queryKey(q)).toArray()[0];
    if (demand?.at>Date.now()-300000) return 5;
    if (['open_prs','open_issues','repository','refs','workflow_runs'].includes(q.operation)) return 10;
    return 20;
  }
  async noteDemand(q:Query): Promise<void> {
    if (q.allow_observed || q.operation==='survey') return;
    const key=queryKey(q),now=Date.now();
    const prior=this.sql.exec<any>('SELECT at FROM demand WHERE key=?',key).toArray()[0];
    this.sql.exec('INSERT INTO demand VALUES(?,?) ON CONFLICT(key) DO UPDATE SET at=excluded.at',key,now);
    const complete=this.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',key).toArray().length>0;
    if (!complete && q.operation!=='progress') {
      this.sql.exec("UPDATE jobs SET lane='requested',requested_at=COALESCE(requested_at,?),due=CASE WHEN wait_reason IS NULL AND attempt=0 THEN MIN(due,?) ELSE due END WHERE key=?",now,now,key);
    }
    if (prior?.at>now-300000) return;
    if (complete && q.operation!=='progress' && !(q.operation==='workflow_jobs' && this.meta('workflow_finished:'+q.args?.id)) && !this.sql.exec('SELECT key FROM jobs WHERE key=?',key).toArray().length) await this.enqueue(q,20,now+this.interval(q));
    // Becoming active advances a quiet audit to its hot interval. A client
    // still receives existing data immediately; actual quota waits stay intact.
    this.sql.exec("UPDATE jobs SET due=MIN(due,?),priority=5,lane=CASE WHEN ? THEN 'verification' ELSE 'requested' END WHERE key=? AND attempt=0 AND started IS NULL AND wait_reason IS NULL",complete?now+this.interval(q):now,Number(complete),key);
    const due=this.sql.exec<any>('SELECT due FROM jobs WHERE key=?',key).toArray()[0]?.due;
    const alarm=await this.ctx.storage.getAlarm();
    if (due && (!alarm || alarm>due)) await this.setAlarm(Math.max(now+100,due));
  }
  nextJob(): any {
    const now=Date.now(),turn=this.meta('repair_turn') || 0;
    this.setMeta('repair_turn',turn+1);
    const preferred=laneAt(turn,settings(this.env).weights);
    return this.sql.exec<any>('SELECT * FROM jobs WHERE due<=? AND lease_until<=? ORDER BY CASE WHEN lane=? THEN 0 WHEN lane=\'requested\' THEN 1 WHEN lane=\'active\' THEN 2 WHEN lane=\'verification\' THEN 3 ELSE 4 END,due,key LIMIT 1',now,now,preferred).toArray()[0];
  }
  dormant(q:Query): boolean {
    if (q.operation==='workflow_jobs' && this.meta('workflow_finished:'+q.args?.id)) return true;
    const demand=this.sql.exec<any>('SELECT at FROM demand WHERE key=?',queryKey(q)).toArray()[0];
    if (demand?.at>Date.now()-300000) return false;
    if (q.operation==='workflow_jobs') {
      const row=this.sql.exec<any>('SELECT data FROM resources WHERE kind=? AND id=?','workflow_runs',String(q.args?.id)).toArray()[0];
      return Boolean(this.meta('workflow_finished:'+q.args?.id) || row && JSON.parse(row.data).status==='completed');
    }
    let numbers:string[]=[];
    if (q.args?.number) numbers=[String(q.args.number)];
    else if (q.operation==='reactions') {
      const row=this.sql.exec<any>('SELECT parent,tombstone FROM resources WHERE kind=? AND id=?','review_comments',String(q.args?.id)).toArray()[0];
      if (row?.tombstone) return true;
      if (row?.parent) numbers=[row.parent];
    } else if (['statuses','checks'].includes(q.operation)) numbers=this.sql.exec<any>('SELECT number FROM head_prs WHERE sha=?',String(q.args?.sha)).toArray().map(r=>r.number);
    if (!numbers.length) return false;
    return numbers.every(number=>{
      const row=this.sql.exec<any>("SELECT data FROM resources WHERE kind IN ('pr','issue') AND id=? ORDER BY kind DESC LIMIT 1",number).toArray()[0];
      const data=row?JSON.parse(row.data):null;
      return data?.state && data.state!=='OPEN';
    });
  }
  finishJob(job:any,q:Query,etag?:string): void {
    const current=this.sql.exec<any>('SELECT generation,priority,requested_at,lane FROM jobs WHERE key=?',job.key).toArray()[0];
    const again=current?.generation>(job.start_generation || 0);
    // A newer webhook can arrive while pages are being fetched. Completing the
    // old collection must not erase that event's pending repair.
    this.metric('completion',q,current?.lane || job.lane || 'verification');
    const requested=job.requested_at || current?.requested_at;
    if (requested) {
      this.metric('requested_latency_ms',q,'requested',Date.now()-requested);
      if(job.page===1 && !job.nested) this.metric('requested_single_latency_ms',q,'requested',Date.now()-requested);
    }
    this.sql.exec('UPDATE jobs SET due=?,priority=?,lane=?,dirty=?,started=NULL,cursor=NULL,page=1,api=NULL,etag=?,attempt=0,wait_reason=NULL,nested=NULL,requested_at=NULL WHERE key=?',again?Date.now():Date.now()+this.interval(q),again?current.priority:this.priority(q),this.lane(q,again),Number(again),etag || null,job.key);
    if(!again && q.operation==='workflow_jobs' && this.meta('workflow_finished:'+q.args?.id)) {
      this.setMeta('important_final_pending:'+q.args?.id,false);this.setMeta('important_active:'+q.args?.id,false);
    }
    if (!again && this.dormant(q)) this.sql.exec('DELETE FROM jobs WHERE key=?',job.key);
  }
  async refresh(job:any,supplied?:any): Promise<void> {
    const repo=this.meta('repository'), q:Query=JSON.parse(job.query);
    const started=job.started || Date.now();
    if (!job.started) {
      job.start_generation=job.generation || 0;
      this.sql.exec('UPDATE jobs SET started=?,start_generation=? WHERE key=?',started,job.start_generation,job.key);
    }
    const runAttempt=q.operation==='workflow_jobs'?this.runAttempt(String(q.args?.id)):undefined;
    const upstream=q.operation==='workflow_runs'?{...q,args:{after:new Date((this.meta('workflow_watermark') || Date.parse(this.meta('workflow_since')))-300000).toISOString()}}:q.operation==='workflow_jobs'?{...q,args:{...q.args,attempt:runAttempt}}:q;
    let nested:any;
    if(job.nested)try {nested=await this.decode(job.nested);}catch(error) {
      if(error instanceof Error && error.message==='resource unavailable')throw new Error('staging page missing');
      throw error;
    }
    const response=supplied?null:await stub(this.env.INSTALLATIONS,repo.installation).fetch('https://internal/read',{method:'POST',headers:{'x-request-id':crypto.randomUUID()},body:JSON.stringify({repository:repo,query:upstream,cursor:job.cursor,page:job.page,api:job.api,nested,etag:job.page===1 && !job.nested && q.operation!=='workflow_runs'?job.etag:undefined})});
    const result:any=supplied || await response!.json();
    if (job.lease_id && this.sql.exec<any>('SELECT lease_id FROM jobs WHERE key=?',job.key).toArray()[0]?.lease_id!==job.lease_id) return;
    if (result.continuation) {
      const nested=await this.encode(result.continuation,Date.now()+86400000);
      if (job.lease_id && this.sql.exec<any>('SELECT lease_id FROM jobs WHERE key=?',job.key).toArray()[0]?.lease_id!==job.lease_id) return;
      this.sql.exec('UPDATE jobs SET nested=?,api=?,due=?,wait_reason=NULL WHERE key=?',nested,result.api,Date.now(),job.key);
      this.metric('page',q,job.lane || 'verification');return;
    }
    if (result.restart) {
      this.sql.exec('UPDATE jobs SET started=NULL,cursor=NULL,page=1,api=?,etag=NULL,due=? WHERE key=?',result.api,Date.now(),job.key);
      return;
    }
    if (response && !response.ok || result.deferred || result.unavailable) {
      const capacity=result.category==='capacity';
      if(q.operation==='review_comments' && result.deferred && String(result.category).startsWith('primary')) {
        // The existing thread reconciler supplies the same flat history. Preserve
        // the REST cooldown while promoting that dependency in the other pool.
        const source={...q,operation:'review_threads'};
        const existing=this.sql.exec<any>('SELECT started,dirty FROM jobs WHERE key=?',queryKey(source)).toArray()[0];
        await this.noteDemand(source);await this.enqueue(source,1,Date.now(),!existing || !existing.started && !existing.dirty);
        if(!this.complete(q)) this.sql.exec("UPDATE jobs SET lane='requested',requested_at=COALESCE(requested_at,?) WHERE key=?",job.requested_at || Date.now(),queryKey(source));
      }
      this.sql.exec('UPDATE jobs SET due=?,attempt=attempt+?,wait_reason=? WHERE key=?',result.retry_at || Date.now()+Math.min(3600000,60000*2**Math.min(6,job.attempt)),capacity?0:1,result.category || result.reason || 'upstream',job.key);
      this.metric(capacity?'capacity_wait':'retry',q,job.lane || 'verification');
      if(!capacity) this.setMeta('last_upstream_failure',{at:Date.now(),operation:q.operation,category:result.category || result.reason || 'upstream',request_id:result.request_id}); return;
    }
    if (result.unchanged) {
      this.sql.exec('UPDATE collections SET verified=? WHERE key=?',Date.now(),job.key);
      this.finishJob(job,q,job.etag); return;
    }
    if (result.api!==job.api && job.page>1 && job.api) {
      // REST pages and GraphQL cursors cannot be mixed; restart this staged collection.
      this.sql.exec('UPDATE jobs SET started=NULL,cursor=NULL,page=1,api=NULL WHERE key=?',job.key); return;
    }
    const key=`staging/${repo.id}/${encodeURIComponent(job.key)}/${started}/${job.page}`;
    await this.env.ARCHIVE.put(key,JSON.stringify(result.data),{customMetadata:{expires:String(Date.now()+86400000)}});
    if (job.lease_id && this.sql.exec<any>('SELECT lease_id FROM jobs WHERE key=?',job.key).toArray()[0]?.lease_id!==job.lease_id) return;
    this.sql.exec('UPDATE jobs SET nested=NULL,wait_reason=NULL WHERE key=?',job.key);
    this.metric('page',q,job.lane || 'verification');
    let next=result.cursor;
    if (['pr_delta','issue_delta'].includes(q.operation) && Array.isArray(result.data) && result.data.some((r:any)=>Date.parse(r.updatedAt)<(this.meta(q.operation+'_watermark') || started)-300000)) next=undefined;
    if (q.operation.startsWith('recent_') && Array.isArray(result.data) && result.data.some((r:any)=>Date.parse(r.updatedAt)<Date.now()-30*86400000)) next=undefined;
    if (next) {
      if (next===job.cursor) throw new Error('repeated upstream cursor');
      this.sql.exec('UPDATE jobs SET cursor=?,page=page+1,api=?,due=?,etag=? WHERE key=?',next,result.api,Date.now(),result.etag || job.etag,job.key); return;
    }
    const rows:any[]=[]; let scalar:any;
    for (let page=1;page<=job.page;page++) {
      const object=await this.env.ARCHIVE.get(`staging/${repo.id}/${encodeURIComponent(job.key)}/${started}/${page}`);
      if (!object) throw new Error('staging page missing');
      const data:any=await object.json(); if (Array.isArray(data)) rows.push(...data); else scalar=data;
    }
    const kind=this.kind(q), memberIDs=new Set<string>(), acceptedIDs=new Set<string>();
    const changedNumbers=new Set<number>(), changedHeads=new Set<number>();
    if (scalar!==undefined || ['pr','repository','statuses'].includes(q.operation)) {
      await this.put(q.operation,job.key,scalar===undefined?rows:scalar,started);
      if (q.operation==='pr' && scalar?.number) await this.put('pr',String(scalar.number),scalar,started,false,String(scalar.number));
    } else {
      for (const row of rows) {
        if (q.operation.startsWith('recent_') && Date.parse(row.updatedAt)<Date.now()-30*86400000) continue;
        const id=q.operation==='files'?String(q.args?.number)+':'+String(row.filename):this.id(q.operation,row); memberIDs.add(id);
        if (kind==='pr') {
          const prior=this.sql.exec<any>('SELECT data,updated,observed FROM resources WHERE kind=? AND id=?','pr',id).toArray()[0];
          const before=prior?await this.decode(prior.data):null;
          if ((!prior || prior.observed<=started) && (!before || before.updatedAt!==row.updatedAt)) changedNumbers.add(row.number);
          if ((!prior || prior.observed<=started) && (!before || before.headRefOid!==row.headRefOid)) changedHeads.add(row.number);
        }
        if (q.operation==='workflow_jobs') row.run_attempt ||= runAttempt;
        if (q.operation==='workflow_runs' && !this.workflowAttempt(id,Number(row.run_attempt || 1))) continue;
        if (await this.put(kind,id,row,started,false,String(q.args?.number || q.args?.id || q.args?.sha || ''))) acceptedIDs.add(id);
        if (q.operation==='workflow_runs' && acceptedIDs.has(id)) {
          this.noteWorkflow(id,row);
          const key=queryKey({operation:'workflow_jobs',args:{id}});
          if (this.importantPending(id)) await this.enqueue({repository:repo.name,operation:'workflow_jobs',args:{id}},1);
        }
      }
      // Keep newer webhook membership when the reconciliation began before that event.
      const prior=this.sql.exec<any>('SELECT m.id,r.observed FROM members m LEFT JOIN resources r ON r.kind=? AND r.id=m.id WHERE m.collection=?',kind,job.key).toArray();
      // A REST offset can shift when another PR closes between pages. Absence
      // from that traversal cannot delete a previously observed open resource;
      // webhooks and delta surveys supply actual state changes instead.
      const openInventory=['open_prs','open_issues'].includes(q.operation);
      for (const row of prior) if (q.operation!=='workflow_runs' && !(openInventory && result.api==='core') && !memberIDs.has(row.id) && row.observed<=started && this.sql.exec<any>('SELECT observed FROM resources WHERE kind=? AND id=?',kind,row.id).toArray()[0]?.observed<=started) this.membership(job.key,row.id,false);
      for (const id of memberIDs) {
        const current=openInventory?this.sql.exec<any>('SELECT data,tombstone FROM resources WHERE kind=? AND id=?',kind,id).toArray()[0]:null;
        if(current && (current.tombstone || (await this.decode(current.data)).state!=='OPEN')) continue;
        this.membership(job.key,id,true);
      }
    }
    if (job.lease_id && this.sql.exec<any>('SELECT lease_id FROM jobs WHERE key=?',job.key).toArray()[0]?.lease_id!==job.lease_id) return;
    if (!this.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',job.key).toArray().length) this.nextRevision('collection',job.key);
    this.sql.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET complete=1,verified=excluded.verified,revision=excluded.revision,etag=excluded.etag',job.key,q.operation,JSON.stringify(q.args || {}),1,Date.now(),this.revision(),job.page===1?result.etag || null:null);
    this.finishJob(job,q,job.page===1?result.etag || null:null);
    if (q.operation==='workflow_runs') this.setMeta('workflow_watermark',started);
    if (['pr_delta','issue_delta'].includes(q.operation)) {
      const operation=q.operation==='pr_delta'?'open_prs':'open_issues', inventory=queryKey({operation});
      for (const row of rows) {
        const id=String(row.number);
        if (acceptedIDs.has(id)) this.membership(inventory,id,row.state==='OPEN');
        if (changedNumbers.has(row.number)) {
          await this.enqueue({repository:repo.name,operation:'issue_comments',args:{number:row.number}},2,Date.now(),true);
          if (operation==='open_prs') for (const op of ['review_threads','pr']) await this.enqueue({repository:repo.name,operation:op,args:{number:row.number}},3,Date.now(),true);
          if (changedHeads.has(row.number)) await this.enqueue({repository:repo.name,operation:'files',args:{number:row.number}},3,Date.now(),true);
        }
      }
      this.setMeta(q.operation+'_watermark',started);
    }
    if (q.operation==='open_prs' || q.operation==='recent_prs') {
      for (const row of rows) {
        if (q.operation==='recent_prs' && Date.parse(row.updatedAt)<Date.now()-30*86400000) continue;
        for (const operation of ['issue_comments','review_threads','reviews','files']) await this.enqueue({repository:repo.name,operation,args:{number:row.number}},20);
        if (q.operation==='open_prs') {
          const itemKey=queryKey({operation:'pr',args:{number:row.number}});
          await this.put('pr',itemKey,row,started);
          this.sql.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET complete=1,verified=excluded.verified,revision=excluded.revision',itemKey,'pr',JSON.stringify({number:row.number}),1,Date.now(),this.revision(),null);
        }
        if (q.operation==='open_prs' && row.headRefOid) for (const operation of ['statuses','checks']) await this.enqueue({repository:repo.name,operation,args:{sha:row.headRefOid}},2);
      }
      if(q.operation==='open_prs') this.setMeta('bootstrap_inventory',Date.now());
    }
    if (q.operation==='open_issues') for (const row of rows) await this.enqueue({repository:repo.name,operation:'issue_comments',args:{number:row.number}},20);
    if (q.operation==='review_comments') for (const row of rows) await this.enqueue({repository:repo.name,operation:'reactions',args:{id:String(row.id)}},25);
    if (q.operation==='review_threads') {
      // This complete thread reconciliation also supplies the flat REST-shaped history.
      const comments=rows.flatMap(t=>t.comments.nodes.map(comment));
      const flatKey=queryKey({operation:'review_comments',args:q.args}), ids=new Set<string>();
      for (const c of comments) {
        ids.add(String(c.id)); await this.put('review_comments',String(c.id),c,started,false,String(q.args?.number));
        await this.enqueue({repository:repo.name,operation:'reactions',args:{id:String(c.id)}},25);
      }
      const prior=this.sql.exec<any>('SELECT m.id,r.observed FROM members m LEFT JOIN resources r ON r.kind=? AND r.id=m.id WHERE m.collection=?','review_comments',flatKey).toArray();
      for (const row of prior) if (!ids.has(row.id) && row.observed<=started) this.membership(flatKey,row.id,false);
      for (const id of ids) this.membership(flatKey,id,true);
      if (!this.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',flatKey).toArray().length) this.nextRevision('collection',flatKey);
      this.sql.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET complete=1,verified=excluded.verified,revision=excluded.revision',flatKey,'review_comments',JSON.stringify(q.args),1,Date.now(),this.revision(),null);
      const dependent=this.sql.exec<any>('SELECT * FROM jobs WHERE key=? AND wait_reason LIKE ?',flatKey,'primary%').toArray()[0];
      if(dependent) {
        this.finishJob({...dependent,start_generation:dependent.generation},{...q,operation:'review_comments'});
        this.sql.exec('UPDATE jobs SET lease_id=NULL,lease_until=0 WHERE key=?',flatKey);
      }
    }
    this.setMeta('last_repair',Date.now());
  }
  // Expiry only deletes, which still succeeds at the storage limit. It runs
  // before repair, hydration or snapshots, any of which can fail first.
  expire(): Record<string,number> {
    const out:Record<string,number>={},now=Date.now();
    const budget={rows:6000,bytes:4*1024**2,deadline:now+50};
    if(!this.storageLoaded || this.migrationHeld())return out; // Unknown pins and source fences fail closed.
    const names=Object.keys(RULES) as RetentionClass[];
    for(let i=0;i<names.length;i++) {
      const name=names[(i+this.storageTurn)%names.length];
      if(name==='expired_deliveries' && this.receiptsPinned)continue;
      if(this.retentionPolicy.disabled.includes(name) || this.retentionPolicy.pinned.includes(name))continue;
      if(budget.rows<=0 || budget.bytes<=0 || Date.now()>=budget.deadline)break;
      const prior=this.retentionState[name];
      if(prior && prior.after>=prior.upper && prior.completed && now-prior.completed<60000)continue;
      // Preserve S0's emergency expiry; new classes start as a dry run.
      const enforce=this.retentionPolicy.mode==='enforce' || name!=='expired_demand';
      try {
        const next=sweep(this.sql,name,prior,budget,enforce,now);
        this.retentionState[name]=next;out[name]=next.deleted-(next.started===prior?.started?prior.deleted:0);
      } catch(error) {
        this.noteFailure('expire:'+name,error);
        this.retentionState[name]={...(prior || {after:0,upper:0,started:now,examined:0,eligible:0,deleted:0,bytes:0}),error:classify(error)};
      }
    }
    this.storageTurn=(this.storageTurn+1)%names.length;
    return out;
  }
  // Cron calls this independently of hydration, dispatch and archive creation.
  // Checkpoints and observations live outside the potentially full SQLite DB.
  async storageMaintenance(runExpiry=true): Promise<void> {
    if(this.migrationHeld())return;
    if(this.storageFlight)return this.storageFlight;
    this.activeOperations++;
    const flight=this.prepareStorage(runExpiry);this.storageFlight=flight;
    try {await flight;}finally {this.activeOperations--;if(this.storageFlight===flight)this.storageFlight=undefined;}
  }
  get maintenanceArchive():R2Bucket {return timedBucket(this.env.ARCHIVE);}
  startStorageMaintenance():void {
    if(this.migrationHeld())return;
    const task=this.storageMaintenance(false).catch(error=>this.noteFailure('storage_maintenance',error));
    if(this.ctx.waitUntil)this.ctx.waitUntil(task);
  }
  async loadRetentionState():Promise<void> {
    if(this.storageLoaded && this.retentionProgressLoaded)return;
    if(this.retentionLoadFlight)return this.retentionLoadFlight;
    const task=this.readRetentionState();this.retentionLoadFlight=task;
    try {await task;}finally {if(this.retentionLoadFlight===task)this.retentionLoadFlight=undefined;}
  }
  async readRetentionState():Promise<void> {
    let repo:string|undefined;try {repo=this.meta('repository')?.id;}catch {return;}if(!repo)return;
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
      if(checkpoint?.version===1 && checkpoint.classes && typeof checkpoint.classes==='object') {
        for(const name of Object.keys(RULES) as RetentionClass[]) {
          const row=checkpoint.classes[name];
          if(!this.retentionState[name] && row && ['after','upper','started','examined','eligible','deleted','bytes'].every(k=>Number.isSafeInteger(row[k]) && row[k]>=0) && row.after<=row.upper) {
            this.retentionState[name]={...row,completed:Number.isSafeInteger(row.completed) && row.completed<=Date.now()?row.completed:undefined};
          }
        }
        if(!this.ciAfter)this.ciAfter=Number.isSafeInteger(checkpoint.ci_after) && checkpoint.ci_after>=0?checkpoint.ci_after:0;
      }
      this.retentionProgressLoaded=true;
    }catch(error) {this.noteFailure('retention_checkpoint_load',error);}
  }

  async prepareStorage(runExpiry=true): Promise<void> {
    const repo=this.meta('repository')?.id;if(!repo)return;
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
    if(runExpiry && this.storageLoaded)this.expire();
    if(this.storageLoaded && this.retentionPolicy.archive_ci && !this.degraded) {
      try {await this.archiveCI(this.retentionPolicy.mode==='enforce');}catch(error) {this.noteFailure('archive_ci',error);}
    }
    if(this.migrationHeld())return;
    try {
      const text=JSON.stringify({version:1,classes:this.retentionState,ci_after:this.ciAfter});
      if(this.storageLoaded && this.retentionProgressLoaded && text!==this.checkpointText && Date.now()-this.checkpointAt>=30000) {
        await this.ctx.storage.sync();
        await this.maintenanceArchive.put('control/'+repo+'/retention-checkpoint.json',text);
        this.checkpointAt=Date.now();this.checkpointText=text;
      }
    }catch(error) {this.noteFailure('retention_checkpoint',error);}
    if(this.migrationHeld())return;
    this.prepareDiagnostics();
    const now=Date.now();
    if(this.observation && now-this.observation.at<60000)return;
    let ingressHealth:any;
    try {
      // A real tiny write/read tests the parked-ingress storage destination.
      const key='control/'+repo+'/ingress-durability-probe.json',nonce=crypto.randomUUID();
      await this.maintenanceArchive.put(key,nonce);
      if(await this.maintenanceArchive.get(key).then(o=>o?.text())!==nonce)throw new Error('durability_probe_mismatch');
      const configured=repository(this.env,repo);
      ingressHealth={at:now,durable:true,backlog:configured?await backlog({...this.env,ARCHIVE:this.maintenanceArchive},configured,2):null,
        queue:{count:null,reason:'management_metrics_not_configured'},dlq:{count:null,reason:'management_metrics_not_configured'}};
    }catch(error) {ingressHealth={at:now,durable:false,error:classify(error)};}
    if(this.migrationHeld())return;
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
      const value={version:1,at:now,level,reasons,capacity:measured,capacity_history:this.capacityHistory,tables:this.tableEstimates,table_attempt_at:this.tableAttemptAt,ingress:ingressHealth,refresh_lag_ms:this.meta('last_repair')?now-this.meta('last_repair'):null,requests:requestSummary,repair_summary:this.repairMetrics.summary(now),
        retention:{policy:this.retentionPolicy,policy_loaded:this.storageLoaded,classes:this.retentionState,ci:this.ciProgress,receipts_pinned:this.receiptsPinned,r2:r2Retention},
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
  async archiveCI(enforce=true): Promise<void> {
    if(this.migrationHeld())return;
    const began=Date.now(),cutoff=began-7*86400000,observedGeneration=this.policyGeneration;
    const candidates=this.sql.exec<any>('SELECT rowid rid,kind,id,observed,length(CAST(data AS BLOB)) bytes FROM resources WHERE rowid>? ORDER BY rowid LIMIT 100',this.ciAfter).toArray();
    let bytes=0,archived=0,eligible=0;
    for(const row of candidates) {
      if(Date.now()-began>=50)break;
      if(row.bytes>4*1024**2) {this.ciAfter=row.rid;continue;}
      if(bytes+row.bytes>4*1024**2)break;
      bytes+=row.bytes;
      // Workflow-run headers remain inline: scheduling uses their JSON fields.
      if(['workflow_jobs','checks','statuses'].includes(row.kind) && row.observed<cutoff && row.bytes>4096) {
        const before=this.sql.exec<any>('SELECT data FROM resources WHERE rowid=?',row.rid).one().data;
        const data=JSON.parse(before);
        if(!data.$r2) {
          eligible++;
          if(!enforce) {this.ciAfter=row.rid;continue;}
          const hash=await digest(before),key='resources/'+this.meta('repository').id+'/ci/'+hash;
          await this.maintenanceArchive.put(key,before,{sha256:hash,customMetadata:{kind:'resource',sha256:hash}});
          // Verify stored contents before publishing the transparent SQL index.
          const stored=await this.maintenanceArchive.get(key);
          if(!stored || await digest(await stored.text())!==hash)throw new Error('archive_verification_failed');
          // Preserve the inline field used by legacy attempt membership guards.
          if(this.migrationHeld() || observedGeneration!==this.policyGeneration)return;
          const pointer=JSON.stringify({$r2:key,...(row.kind==='workflow_jobs'?{run_attempt:data.run_attempt || 1}:{})});
          const written=this.sql.exec('UPDATE resources SET data=? WHERE rowid=? AND kind=? AND id=? AND data=?',pointer,row.rid,row.kind,row.id,before).rowsWritten;
          if(written)archived++;
        }
      }
      this.ciAfter=row.rid;
    }
    if(!candidates.length)this.ciAfter=0;
    this.ciProgress={at:Date.now(),after:this.ciAfter,examined:candidates.length,eligible,archived,bytes,enforce};
  }
  async preparedObservation(): Promise<any> {
    if(!this.observation && Date.now()-this.observationReadAt>=60000) {
      this.observationReadAt=Date.now();
      let repo:string|undefined;try {repo=this.meta('repository')?.id;}catch {}
      if(repo) {
        const task=this.maintenanceArchive.get('control/'+repo+'/observation.json').then(o=>o?.json<any>()).then(value=>{
          if(value && (!this.observation || value.at>this.observation.at))this.observation=value;
        }).catch(()=>{});
        if(this.ctx.waitUntil)this.ctx.waitUntil(task);
        // A cold status response stays fast even when the observation store is slow.
        await within(task,100).catch(()=>{});
      }
    }
    const {capacity_history,...publicObservation}=this.observation || {};
    return {...publicObservation,stale:!this.observation || Date.now()-this.observation.at>180000,age_ms:this.observation?Date.now()-this.observation.at:null};
  }
  async maintenance(): Promise<void> {
    if(!this.degraded) {
      try {
        const pending=this.sql.exec<any>('SELECT m.id FROM members m LEFT JOIN survey_hydrated h ON h.number=m.id LEFT JOIN survey_hydration_failures f ON f.number=m.id WHERE m.collection=? AND (h.contract IS NULL OR h.contract!=?) AND (f.retry_at IS NULL OR f.retry_at<=?) LIMIT 20',queryKey({operation:'open_prs'}),SURVEY_CONTRACT,Date.now()).toArray();
        for(const row of pending) {
          try {await this.hydrateSurvey(row.id);}catch {this.invalidateSurvey(row.id);}
        }
        for(const row of this.sql.exec<any>('SELECT number FROM survey_dirty LIMIT 20').toArray()) {
          try {this.rebuildSurvey(row.number);}catch {this.invalidateSurvey(row.number);this.sql.exec('DELETE FROM survey_dirty WHERE number=?',row.number);}
        }
      } catch(error) {this.noteFailure('survey_hydration',error);}
    }
    const now=Date.now();
    // Retire queued historical CI even if an old retry placed it far in the future.
    const obsolete="key LIKE 'workflow_jobs:%' AND lease_until<=? AND NOT EXISTS(SELECT 1 FROM demand d WHERE d.key=jobs.key AND d.at>?) AND NOT EXISTS(SELECT 1 FROM resources r WHERE r.kind='workflow_runs' AND r.id=CAST(json_extract(jobs.query,'$.args.id') AS TEXT) AND json_extract(r.data,'$.head_branch') IN ('staging','trying') AND (json_extract(r.data,'$.status')!='completed' OR EXISTS(SELECT 1 FROM meta m WHERE m.key='important_final_pending:' || r.id AND m.value='true')))";
    const scanned=this.sql.exec<any>('SELECT MAX(rowid) last FROM (SELECT rowid FROM jobs WHERE rowid>? ORDER BY rowid LIMIT 100)',this.obsoleteAfter).one().last;
    const retired=this.sql.exec<any>('SELECT key,lane FROM (SELECT * FROM jobs WHERE rowid>? ORDER BY rowid LIMIT 100) jobs WHERE '+obsolete,this.obsoleteAfter,now,now-300000).toArray();
    this.obsoleteAfter=scanned ?? 0;
    for(const row of retired) {this.sql.exec('DELETE FROM jobs WHERE key=?',row.key);this.metric('cancellation',{repository:this.meta('repository').name,operation:'workflow_jobs'},row.lane);}
    if(!this.snapshotFlight && !this.degraded) {
      const flight=this.snapshotPages(now);this.snapshotFlight=flight;
      try {await flight;} catch(error) {this.noteFailure('snapshot',error);} finally {if(this.snapshotFlight===flight)this.snapshotFlight=undefined;}
    }
  }
  async snapshotPages(now:number): Promise<void> {
    let state=this.meta('snapshot_pending');
    if(!state) {
      if(now-(this.meta('last_snapshot') || 0)<=86400000) return;
      state={version:2,observational:true,started:now,revision:this.revision(),key:`snapshots/${this.meta('repository').id}/${new Date(now).toISOString().slice(0,10)}-${crypto.randomUUID()}`,index:0,tables:['meta','resources','collections','members','activity_landings'].map(table=>({table,after:0,pages:0,upper:this.sql.exec<any>('SELECT COALESCE(MAX(rowid),0) last FROM '+table).one().last}))};
      this.setMeta('snapshot_pending',state);
    }
    // Archive bounded pages and checkpoint each one. Repository mutations can
    // interleave, so this is an observational archive; DO point-in-time recovery
    // remains the authoritative database backup.
    for(let turn=0;turn<5 && state.index<state.tables.length;turn++) {
      const table=state.tables[state.index];
      const columns=this.sql.exec<any>('PRAGMA table_info('+table.table+')').toArray().map(c=>c.name);
      const size=columns.map(c=>'COALESCE(length(CAST('+c+' AS BLOB)),0)').join('+');
      const candidates=this.sql.exec<any>('SELECT rowid AS id,'+size+' AS bytes FROM '+table.table+' WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT 1000',table.after,table.upper).toArray();
      if(!candidates.length) {state.index++;this.setMeta('snapshot_pending',state);continue;}
      let bytes=0,last=table.after;
      for(const row of candidates) {if(last!==table.after && bytes+row.bytes>1000000)break;bytes+=row.bytes;last=row.id;}
      const rows=this.sql.exec('SELECT * FROM '+table.table+' WHERE rowid>? AND rowid<=? ORDER BY rowid',table.after,last).toArray();
      await this.env.ARCHIVE.put(state.key+'/'+table.table+'/'+table.pages+'.json',JSON.stringify(rows),{customMetadata:{expires:String(now+7*86400000)}});
      table.after=last;table.pages++;this.setMeta('snapshot_pending',state);
    }
    if(state.index===state.tables.length) {
      await this.env.ARCHIVE.put(state.key+'.json',JSON.stringify({...state,finished:Date.now(),final_revision:this.revision()}),{customMetadata:{expires:String(now+7*86400000)}});
      this.setMeta('last_snapshot',Date.now());this.setMeta('snapshot_pending',null);
    }
  }
  retireUnneeded(job:any): boolean {
    const q:Query=JSON.parse(job.query);
    if (NOT_MIRRORED.has(q.operation)) {this.sql.exec('DELETE FROM jobs WHERE key=?',job.key);this.optional('repair_metrics',()=>this.metric('cancellation',q,job.lane));return true;}
    const demand=(this.sql.exec<any>('SELECT at FROM demand WHERE key=?',job.key).toArray()[0]?.at || 0)>Date.now()-300000;
    const unneeded=q.operation==='workflow_jobs' && !demand && !this.importantPending(String(q.args?.id));
    if (!unneeded && (job.dirty || !this.dormant(q) || !this.complete(q))) return false;
    this.sql.exec('DELETE FROM jobs WHERE key=?',job.key);
    this.metric('cancellation',q,job.lane);return true;
  }
  claim(job:any): any {
    const now=Date.now(),lease=crypto.randomUUID(),started=job.started || now,generation=job.started?job.start_generation:job.generation;
    this.sql.exec('UPDATE jobs SET lease_id=?,lease_until=?,started=?,start_generation=? WHERE key=? AND lease_until<=?',lease,now+settings(this.env).lease_ms,started,generation,job.key,now);
    return this.sql.exec<any>('SELECT * FROM jobs WHERE key=? AND lease_id=?',job.key,lease).toArray()[0];
  }
  retryJob(job:any,error:unknown): void {
    const expired=error instanceof Error && /staging page missing/.test(error.message);
    const category=expired?'staging_expired':'transport_or_parser';
    const delay=Math.min(3600000,60000*2**Math.min(job.attempt || 0,6));
    this.sql.exec('UPDATE jobs SET due=?,attempt=attempt+1,wait_reason=?'+(expired?',started=NULL,cursor=NULL,page=1,api=NULL,etag=NULL,nested=NULL':'')+' WHERE key=? AND lease_id=?',Date.now()+delay,category,job.key,job.lease_id);
    this.metric('retry',JSON.parse(job.query),job.lane);
    this.setMeta('last_upstream_failure',{at:Date.now(),operation:JSON.parse(job.query).operation,category});
  }
  async dispatch(first:any): Promise<void> {
    const q:Query=JSON.parse(first.query),operation=q.operation,repo=this.meta('repository');
    let jobs=[first];
    try {
      if (['issue_comments','review_threads','statuses','reactions'].includes(operation) && first.page===1 && !first.api && !first.nested) {
        const candidates=this.sql.exec<any>('SELECT * FROM jobs WHERE due<=? AND lease_until<=? AND lane=? AND page=1 AND api IS NULL AND nested IS NULL AND key LIKE ? ORDER BY due,key LIMIT ?',Date.now(),Date.now(),first.lane,operation+':%',Math.max(0,batchLimit(operation)-1)).toArray();
        for (const item of candidates) {
          if (this.retireUnneeded(item)) continue;
          const claimed=this.claim(item);if (claimed) jobs.push(claimed);
        }
        if (operation==='reactions') {
          const supported=[];
          for (const item of jobs) {
            const query=JSON.parse(item.query),row=this.sql.exec<any>('SELECT data FROM resources WHERE kind=? AND id=? AND tombstone=0','review_comments',String(query.args.id)).toArray()[0];
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
            catch(error) {this.retryJob(jobs[i],error);}
          }
        } else if (result.deferred && operation!=='review_threads' && result.category!=='capacity' && !String(result.category).startsWith('secondary')) {
          // Retry REST-capable operations in their other pool on the next quantum.
          for(const item of jobs) this.sql.exec('UPDATE jobs SET api=?,due=? WHERE key=? AND lease_id=?','core',Date.now(),item.key,item.lease_id);
        } else for(const item of jobs) await this.refresh(item,result.deferred || result.unavailable?result:{unavailable:true,reason:'malformed_service_response'});
      } else await this.refresh(first);
    } catch(error) {
      for(const item of jobs) this.retryJob(item,error);
    } finally {
      for(const item of jobs) this.sql.exec('UPDATE jobs SET lease_id=NULL,lease_until=0 WHERE key=? AND lease_id=?',item.key,item.lease_id);
    }
  }
  async alarm(): Promise<void> {
    if(this.migrationUnavailable) await this.retryMigrationControl();
    if(this.migrationHeld()) return;
    this.activeOperations++;
    try {await this.runAlarm();} finally {this.activeOperations--;}
  }
  async runAlarm(): Promise<void> {
    const options=settings(this.env),began=Date.now();let dispatched=0,retired=0;
    try {
      this.expire();
      if(this.schema!=='current' && !this.degraded) this.startup();
      // Every dispatch writes its lease before any GitHub request, so a full
      // store stops here without spending upstream quota.
      if(!this.degraded && typeof this.schema!=='object') try {
        while(dispatched<options.dispatches && Date.now()-began<options.launch_ms && !this.degraded && !this.migrationHeld()) {
          const wave:Promise<void>[]=[];
          while(wave.length<options.concurrency && dispatched<options.dispatches) {
            const next=this.nextJob();if (!next) break;
            if (this.retireUnneeded(next)) {if (++retired>=200) break;continue;}
            const job=this.claim(next);if (!job) continue;
            dispatched++;wave.push(this.dispatch(job));
          }
          if (!wave.length) break;
          await Promise.allSettled(wave);
        }
      } catch(error) {this.noteFailure('repair_dispatch',error);}
      try {if(!this.migrationHeld()) await this.maintenance();} catch(error) {this.noteFailure('maintenance',error);}
    } finally {
      try {
        const row=this.sql.exec<any>('SELECT MIN(MAX(due,lease_until)) AS due FROM jobs').one();
        // Degraded mode writes nothing, not even the next alarm; the cron tick
        // keeps expiring rows until a probe restores writes.
        if (row.due && !this.degraded && !this.migrationHeld()) await this.setAlarm(Math.max(Date.now()+100,row.due));
      } catch(error) {this.noteFailure('alarm',error);}
      this.startStorageMaintenance();
    }
  }
  async refreshActivityLandings(): Promise<void> {
    const configured=this.meta('repository');
    const receiptURL=configured?.bors_receipts_url;
    if(!receiptURL && !(configured?.name==='TauCetiProject/TauCeti' && this.env.BORS_ACTIVITY)) return;
    if(receiptURL && (!receiptURL.startsWith('https://') || new URL(receiptURL).username || new URL(receiptURL).password)) throw new Error('Bors receipts require an approved HTTPS URL');
    const now=Date.now();
    if(this.meta('activity_landings_transport')==='service-binding-v2' && now-(this.meta('activity_landings_checked') || 0)<300000) return;
    // Reserve the interval before yielding so overlapping cron requests cannot
    // multiply reads. This path never calls configure, demand or the scheduler.
    this.setMeta('activity_landings_checked',now);
    this.setMeta('activity_landings_transport','service-binding-v2');
    try {
      const receipts=await readPromotionReceipts((url,options)=>this.env.BORS_ACTIVITY ? this.env.BORS_ACTIVITY.fetch(url,options) : fetch(url,options),receiptURL,configured.name,configured.branch || 'main');
      for(const r of receipts) this.sql.exec('INSERT INTO activity_landings VALUES(?,?,?,?) ON CONFLICT(number,head) DO UPDATE SET at=excluded.at,source=excluded.source WHERE excluded.at<activity_landings.at',r.number,r.head,r.at,r.source);
      this.setMeta('activity_landings_success',now);
      this.setMeta('activity_landings_error',null);
    } catch(error) {this.setMeta('activity_landings_error','Stored Bors promotion receipts unavailable: '+(error instanceof Error?error.name+': '+error.message.slice(0,160):'unknown error'));}
  }
  async activityLOC(number:number,p:any,budget?:{remaining:number}): Promise<any> {
    const key=queryKey({operation:'files',args:{number}});
    const complete=this.sql.exec<any>('SELECT complete,revision FROM collections WHERE key=?',key).toArray()[0];
    if(!complete?.complete || !p.headRefOid || !Number.isSafeInteger(p.changedFiles)) return null;
    const fingerprint=JSON.stringify([p.headRefOid,p.baseRefOid,p.changedFiles,p.additions,p.deletions,complete.revision]);
    const cached=this.sql.exec<any>('SELECT data FROM activity_loc_cache WHERE number=? AND fingerprint=?',number,fingerprint).toArray()[0];
    if(cached) return JSON.parse(cached.data);
    if(budget && budget.remaining--<=0) return null;
    const remember=(result:any)=>{
      this.optional('activity_loc_cache',()=>this.sql.exec('INSERT OR REPLACE INTO activity_loc_cache VALUES (?,?,?)',number,fingerprint,JSON.stringify(result)));
      return result;
    };
    const stored=this.sql.exec<any>("SELECT r.data FROM members m JOIN resources r ON r.kind='files' AND r.id=m.id WHERE m.collection=? AND r.tombstone=0",key).toArray();
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
  frozenSince(): number | null {
    if (!this.degraded) return null;
    let changed:number;try {changed=Math.max(this.meta('last_webhook') || 0,this.meta('last_repair') || 0);}catch {return null;}
    return Date.now()-changed>FROZEN_AFTER?changed:null;
  }
  prepareDiagnostics(): void {
    if(Date.now()-this.diagnosticsAt<60000)return;
    try {this.diagnostics={storage:this.storageState(),storage_bytes:this.sql.databaseSize,snapshot_pending:this.meta('snapshot_pending'),survey:this.surveyHealth(),reconciliation_version:2,deployment_id:this.env.CF_VERSION?.id,settings:settings(this.env),repository:this.meta('repository'),revision:this.revision(),bootstrap_inventory:this.meta('bootstrap_inventory'),last_webhook:this.meta('last_webhook'),webhook_apply_ms:this.meta('webhook_apply_ms'),last_repair:this.meta('last_repair'),last_upstream_failure:this.meta('last_upstream_failure'),coverage:this.sql.exec('SELECT operation,COUNT(*) resources,SUM(complete) complete,MIN(verified) oldest_verified FROM collections GROUP BY operation').toArray(),jobs:this.sql.exec('SELECT COUNT(*) total,SUM(due<=?) due,MIN(started) oldest_started FROM jobs',Date.now()).one(),lanes:this.sql.exec('SELECT lane,COUNT(*) jobs,SUM(due<=? AND lease_until<=?) eligible,SUM(lease_until>?) inflight,MIN(CASE WHEN due<=? AND lease_until<=? THEN due END) oldest_eligible,MIN(requested_at) oldest_requested FROM jobs GROUP BY lane',...Array(5).fill(Date.now())).toArray(),waits:this.sql.exec('SELECT lane,wait_reason,COUNT(*) jobs FROM jobs WHERE wait_reason IS NOT NULL GROUP BY lane,wait_reason').toArray(),...this.repairMetrics.summary(),queries:[...this.queryCounts.values()],repair_operations:this.sql.exec("SELECT json_extract(query,'$.operation') operation,COUNT(*) jobs,SUM(due<=?) due,MIN(due) next_due,MAX(attempt) attempts FROM jobs GROUP BY operation",Date.now()).toArray()};this.diagnosticsAt=Date.now();}
    catch(error) {this.noteFailure('health_prepare',error);}
  }
  storageState(): any {
    return {mode:this.degraded?'degraded':'writable',mutation_fence:this.migrationUnavailable?'control_unavailable':this.migrationHeld()?'migration':null,degraded:this.degraded ?? null,frozen_since:this.frozenSince(),schema:this.schema,database_size:this.sql.databaseSize,fault_injection:this.env.FAULT_INJECTION==='true'?{full:this.faultFull}:undefined};
  }
  async storage(path:string,req:Request): Promise<Response> {
    const input:any=req.method==='POST'?await req.json():{};
    const repo=this.meta('repository')?.id ?? input.repository_id;
    if (typeof repo!=='string' || !/^[0-9]+$/.test(repo)) throw new AdminError('repository_unconfigured');
    if (path==='/storage/summary') return json({repository:repo,storage:this.storageState(),retention:{policy:this.retentionPolicy,policy_loaded:this.storageLoaded,classes:this.retentionState,ci:this.ciProgress,receipts_pinned:this.receiptsPinned},...summary(this.sql)});
    if (path==='/storage/retention') {
      if(input.policy!==undefined) {
        let next:Policy;try {next=policy(input.policy);}catch {throw new AdminError('invalid_retention_policy');}
        const update=this.policyUpdateFlight.then(async()=>{
          if(this.storageFlight)await this.storageFlight.catch(()=>{});
          if(this.retentionLoadFlight)await this.retentionLoadFlight;
          await this.env.ARCHIVE.put('control/'+repo+'/retention-policy.json',JSON.stringify(next));
          this.policyGeneration++;this.retentionPolicy=next;this.storageLoaded=true;
        });
        this.policyUpdateFlight=update.catch(()=>{});
        await update;
      }
      return json({policy:this.retentionPolicy,policy_loaded:this.storageLoaded,classes:this.retentionState,ci:this.ciProgress,receipts_pinned:this.receiptsPinned,budget:{rows:6000,bytes:4*1024**2,cooperative_io_budget_ms:50,cpu_bound:'rows_and_bytes'},observation:await this.preparedObservation()});
    }
    if (path==='/storage/scan') return json(scan(this.sql,input));
    if (path==='/storage/recovery') return json(await createRecovery(this.env.ARCHIVE,this.ctx.storage,this.sql,repo,this.revision()));
    if (path==='/storage/export') return json(await exportPage(this.env.ARCHIVE,this.sql,repo,input));
    if (path==='/storage/verify') return json(await verifyExport(this.env.ARCHIVE,repo,input));
    if (path==='/storage/cleanup') {
      const rule=CLEANUP[input.class];
      if (!rule) throw new AdminError('unknown_cleanup_class');
      let bound:{upper:number,started:number} | null=null,recovery:string | null=null;
      if (rule.recovery) {
        // Non-ephemeral rows are deleted only within a verified export.
        const manifest=await readManifest(this.env.ARCHIVE,repo,input.recovery),exported=manifest.tables['class:'+input.class] ?? (rule.bound?manifest.tables[rule.table]:undefined);
        if (!exported?.verified || !Number.isSafeInteger(exported.started)) throw new AdminError('recovery_not_verified');
        bound={upper:exported.upper,started:exported.started};recovery=manifest.id;
      }
      const before=this.sql.databaseSize,result=deleteClass(this.sql,input.class,bound,input);
      return json({class:input.class,table:rule.table,...result,recovery,database_size_before:before,database_size_after:this.sql.databaseSize,
        note:'Deleted rows become reusable free pages; the physical database size need not shrink. Prove headroom with /admin/storage/probe.'});
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
        return json({writable:false,bytes,reason:this.noteFailure('probe',error),database_size:this.sql.databaseSize,storage:this.storageState()});
      }
      this.sql.exec("DELETE FROM meta WHERE key LIKE 'storage_probe:%'");
      await this.ctx.storage.sync();
      await this.env.ARCHIVE.put(storageKey(repo),JSON.stringify({state:'writable',verified_at:Date.now(),bytes,database_size:this.sql.databaseSize}));
      this.degraded=undefined;
      if (this.schema!=='current') this.startup();
      await this.optionalAsync('alarm',()=>this.setAlarm(Date.now()+100));
      return json({writable:true,bytes,database_size_before:before,database_size_after:this.sql.databaseSize,storage:this.storageState()});
    }
    if (path==='/storage/fault') {
      if (this.env.FAULT_INJECTION!=='true') throw new AdminError('fault_injection_disabled');
      if (!['full','none'].includes(input.mode)) throw new AdminError('invalid_fault');
      await this.env.ARCHIVE.put(this.faultKey(),JSON.stringify({mode:input.mode,at:Date.now()}));
      this.faultFull=input.mode==='full';
      return json({storage:this.storageState()});
    }
    if (path==='/storage/ingress') {
      if (!['apply','parked'].includes(input.mode)) throw new AdminError('invalid_ingress_mode');
      this.ingress=input.mode;
      for (const deadline=Date.now()+25000;input.mode==='parked' && this.applying>0 && Date.now()<deadline;) await new Promise(resolve=>setTimeout(resolve,50));
      return json({mode:this.ingress,in_flight:this.applying,drained:this.applying===0});
    }
    if (path==='/storage/deliveries') {
      // Read-only receipt check for queue and DLQ reconciliation.
      if (!Array.isArray(input.ids) || input.ids.length>100 || input.ids.some((id:any)=>typeof id!=='string')) throw new AdminError('invalid_ids');
      return json({known:input.ids.filter((id:string)=>this.sql.exec('SELECT 1 FROM deliveries WHERE id=?',id).toArray().length)});
    }
    return json({error:'not_found'},404);
  }
  async retryMigrationControl(): Promise<void> {
    const result=this.migrationFlight.then(async()=>{
      if(!this.migrationUnavailable) return;
      try {
        this.migrationState=await readMigration(this.env.ARCHIVE,this.meta('repository').id);
        this.migrationUnavailable=false;
        if(this.schema!=='current' && !this.migrationHeld() && !this.degraded) this.startup();
      } catch { /* Keep stored reads and the write fence until the next tick. */ }
    });
    this.migrationFlight=result.catch(()=>{});
    await result;
  }
  async migration(req:Request): Promise<Response> {
    let state=this.migrationState;
    try {
      const input:any=await req.json();
      const repo=this.meta('repository')?.id;
      if(!repo) throw new AdminError('repository_unconfigured');
      // A control-store outage must fence mutations without taking complete
      // SQL reads down. An admin retry can reload the barrier after recovery.
      if(this.migrationUnavailable) {
        this.migrationState=await readMigration(this.env.ARCHIVE,repo);
        this.migrationUnavailable=false;state=this.migrationState;
      }
      if(input.operation==='status') return json(publicReceipt(state));
      if(!input.operation || input.operation==='preflight') {
        const tables=expectedTables(this.sql);
        return json({ ...publicReceipt(state) as object, repository:repo,
          source:state?.source ?? {object:this.ctx.id.toString(),deployment:this.env.CF_VERSION?.id ?? null,schema:this.meta('schema_version')}, expected_tables:tables,
          source_export_environment:this.env.MIGRATION_ENVIRONMENT ?? null,
          blockers:['s1_backend_unavailable','production_prerequisite_acceptance_unavailable'],
          production_cutover_supported:false });
      }
      const id=generation(input.generation),target=targetIdentity(input.target);
      // S1 is not implemented. Never treat caller-supplied claims as deployed
      // acceptance evidence or permit a production freeze with this protocol.
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
        if(this.meta('schema_version')!==SCHEMA_VERSION) throw new AdminError('migration_schema_incompatible');
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
        if(this.activeOperations) throw new AdminError('migration_drain_pending');
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
        for(const deadline=Date.now()+25000;this.activeOperations>0 && Date.now()<deadline;) await new Promise(resolve=>setTimeout(resolve,25));
        if(this.activeOperations) throw new AdminError('migration_drain_pending');
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
      const repo=this.meta('repository')?.id;
      if(repo && this.migrationHeld()) {
        const saved=await readMigration(this.env.ARCHIVE,repo).catch(()=>null);
        if(saved) {saved.failure=reason;this.migrationState=saved;await saveMigration(this.env.ARCHIVE,saved).catch(()=>{});}
      }
      return json({error:reason},reason==='migration_storage_failure' || reason==='migration_checkpoint_conflict'?503:400);
    }
  }
  async fetch(req:Request): Promise<Response> {
    const path=new URL(req.url).pathname;
    if(path==='/tick' && this.migrationUnavailable) await this.retryMigrationControl();
    if(path==='/migration') {
      const result=this.migrationFlight.then(()=>this.migration(req));
      this.migrationFlight=result.catch(()=>{});
      return result;
    }
    if(this.migrationHeld()) {
      if(path==='/event') return json({error:'migration_frozen',parkable:true},503);
      if(!['/query','/activity','/health','/changes','/retained-object','/storage/summary','/storage/scan','/storage/deliveries'].includes(path)) return json({error:'migration_frozen',parkable:true},503);
      if(path==='/retained-object') return json({retained:true});
    }
    const tracked=!this.migrationHeld();
    if(tracked) this.activeOperations++;
    try {return await this.repositoryFetch(req);} finally {if(tracked) this.activeOperations--;}
  }
  async repositoryFetch(req:Request): Promise<Response> {
    const url=new URL(req.url),path=url.pathname,request_id=requestId(req),requestStarted=Date.now();
    let requestError=false,requestUnavailable=false,requestOperation='',requestCoverage='complete';
    try {
      if (path.startsWith('/storage/')) return await this.storage(path,req);
      // A genuinely missing schema is a precise error, not empty data.
      if (typeof this.schema==='object' && path!=='/health') return json({error:this.schema.error,missing:this.schema.missing,cause:this.schema.cause,parkable:true,request_id},503);
      if (path==='/configure') { await this.configure((await req.json() as any).repository); return json({ok:true}); }
      if (path==='/event') {
        const pointer=await req.json() as EventPointer,repo=repository(this.env,pointer.repository);
        if (this.ingress===undefined && repo) this.ingress=(await this.env.ARCHIVE.get(controlKey(repo.id)).then(o=>o?.json<any>()))?.mode ?? 'apply';
        if (this.ingress==='parked') return json({error:'ingress_parked',parkable:true,request_id},503);
        this.applying++;
        try {await this.ingest(pointer);} finally {this.applying--;}
        return json({ok:true});
      }
      if(path==='/retained-object') {
        // Pinned recovery exports may reference spills indefinitely. Retire
        // only once a complete archive reference index/release protocol exists.
        return json({retained:true,reason:'recovery_reference_index_required'});
      }
      if (path==='/activity') {
        const input:any=await req.json();
        const prs=[],missing=[],locBudget={remaining:32};
        // Deliberately no configure, demand, collection verification, or repair.
        for (const number of input.numbers) {
          const row=this.sql.exec<any>("SELECT data,observed FROM resources WHERE kind='pr' AND id=? AND tombstone=0",String(number)).toArray()[0];
          if(!row){missing.push(number);continue;}
          const p=await this.decode(row.data);
          const landing=this.sql.exec<any>('SELECT at,source FROM activity_landings WHERE number=? AND head=?',number,p.headRefOid || '').toArray()[0];
          prs.push({number,state:p.state,created_at:p.createdAt || null,landed_at:p.mergedAt || (landing?new Date(landing.at/1000).toISOString():null),
            head_sha:p.headRefOid || null,observed_at:row.observed,
            landing_source:p.mergedAt?'github-merged':landing?.source || null,lean_loc:await this.activityLOC(number,p,locBudget)});
        }
        return json({version:1,repository:input.repository,revision:this.revision(),as_of:Date.now(),coverage:'observed',prs,missing,
          landing_observations:{checked_at:this.meta('activity_landings_checked'),last_success_at:this.meta('activity_landings_success'),error:this.meta('activity_landings_error')}});
      }
      if (path==='/query') {
        const q=await req.json() as Query;
        validateQuery(q);
        requestOperation=q.operation;
        if (NOT_MIRRORED.has(q.operation)) {requestUnavailable=true;return json(this.unavailable(q,'not_mirrored'));}
        const changed=this.frozenSince();
        const planned=this.migrationState && this.migrationState.phase!=='abandoned';
        if (changed!==null && !planned) {requestUnavailable=true;return json({...this.unavailable(q,'mirror_frozen'),frozen_since:changed},200,{'X-TauCeti-Storage':'degraded'});}
        if(q.operation==='workflow_jobs') {
          try {this.runAttempt(String(q.args?.id));}
          catch(error) {requestUnavailable=true;return json(this.unavailable(q,this.noteFailure('workflow_attempt',error)));}
        }
        const etag='"'+this.revision()+'"',headers:Record<string,string>=this.degraded?{'X-TauCeti-Storage':'degraded'}:{};
        if (!q.cursor && req.headers.get('if-none-match')===etag && q.operation!=='progress' && this.sql.exec('SELECT key FROM collections WHERE key=? AND complete=1',queryKey(q)).toArray().length) {
          await this.optionalAsync('demand',()=>this.noteDemand(q));
          return new Response(null,{status:304,headers:{ETag:etag,...headers}});
        }
        const key=queryKey(q)+'|observed:'+Boolean(q.allow_observed)+'|'+(q.cursor || '');
        const cached=this.completedQueries.get(key);
        if (!q.cursor && cached?.result.revision===this.revision() && cached.expires>Date.now()) {
          requestCoverage=cached.result.coverage || 'unknown';
          await this.optionalAsync('demand',()=>this.noteDemand(q));
          return json(cached.result,200,{ETag:'"'+cached.result.revision+'"',...headers});
        }
        let pending=this.inflightQueries.get(key);
        if (!pending) {
          pending=this.query(q);
          this.inflightQueries.set(key,pending);
        }
        let result:Result;
        try { result=await pending; }
        finally { if (this.inflightQueries.get(key)===pending) this.inflightQueries.delete(key); }
        if (!q.cursor && result.status==='ok') {
          this.completedQueries.delete(key);
          this.completedQueries.set(key,{result,expires:Date.now()+540000});
          while (this.completedQueries.size>16) this.completedQueries.delete(this.completedQueries.keys().next().value!);
        }
        requestUnavailable=result.status==='unavailable';
        requestCoverage=result.coverage || 'unknown';
        return json(result,200,{ETag:'"'+result.revision+'"',...(this.degraded?{'X-TauCeti-Storage':'degraded'}:{})});
      }
      if (path==='/health') {
        if (typeof this.schema==='object') return json({storage:this.storageState(),deployment_id:this.env.CF_VERSION?.id,monitoring:await this.preparedObservation(),request_id});
        return json({...this.diagnostics,reconciliation_version:2,settings:settings(this.env),...this.repairMetrics.summary(),queries:[...this.queryCounts.values()],query_metrics_scope:'repository_instance_since_restart',repair_metrics_scope:'repository_instance_since_restart',storage:this.storageState(),storage_bytes:this.sql.databaseSize,
          repository:this.meta('repository'),revision:this.revision(),bootstrap_inventory:this.meta('bootstrap_inventory'),
          deployment_id:this.env.CF_VERSION?.id,monitoring:await this.preparedObservation(),diagnostics_at:this.diagnosticsAt || null,
          diagnostics_stale:!this.diagnosticsAt || Date.now()-this.diagnosticsAt>180000});
      }
      if (path==='/changes') { const after=Number(new URL(req.url).searchParams.get('after') || 0); const first=this.sql.exec<any>('SELECT MIN(revision) AS revision FROM changes').one(); return json({revision:this.revision(),reset:after>0 && first.revision && after<first.revision-1,changes:this.sql.exec('SELECT * FROM changes WHERE revision>? ORDER BY revision LIMIT 1000',after).toArray()}); }
      if (path==='/activity-refresh' && req.method==='POST') {if(!this.degraded) await this.refreshActivityLandings();return json({ok:true,degraded:Boolean(this.degraded)});}
      if (path==='/tick') {
        // Cleanup/monitoring precede alarm scheduling, hydration and upstream work.
        this.expire();
        if (!this.meta('repository')) await this.configure((await req.json() as any).repository);
        await this.optionalAsync('alarm',()=>this.setAlarm(Date.now()+100));
        this.startStorageMaintenance();
        return json({ok:true,degraded:Boolean(this.degraded)});
      }
      if (path==='/repair') { this.sql.exec('UPDATE jobs SET due=CASE WHEN wait_reason IS NULL AND attempt=0 THEN ? ELSE due END,dirty=1,generation=generation+1',Date.now()); await this.setAlarm(Date.now()+100); return json({ok:true}); }
      return json({error:'not_found'},404);
    } catch(error) {
      requestError=true;
      if (error instanceof AdminError) return json({error:error.message,request_id},400);
      const reason=this.noteFailure('request:'+path,error);
      log('repository_request_failed',{request_id,path,reason});
      return json({error:reason,parkable:PARKABLE.has(reason),message:(error instanceof Error?error.message:'operation failed').slice(0,200),request_id},reason==='internal'?500:503);
    }finally {
      if(path==='/query')try {
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
