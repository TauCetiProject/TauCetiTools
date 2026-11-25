import { Client, neon, neonConfig, types } from '@neondatabase/serverless';
import { log } from './storage';

export type Bind = string | number | null;
export interface Statement { sql:string; bindings?:Bind[] }
export class Rows<T=Record<string,unknown>> {
  constructor(private rows:T[]) {}
  toArray():T[] {return this.rows;}
  one():T {if(this.rows.length!==1) throw new Error('Expected one repository row');return this.rows[0];}
}
// Identifiers, cursors and JSON stay text. Numeric columns contain counters and
// Unix milliseconds/microseconds, and must never silently lose precision.
const integer=(value:string)=>{const n=Number(value);if(!Number.isSafeInteger(n))throw new Error('Repository integer exceeds safe range');return n;};
types.setTypeParser(20,integer);
types.setTypeParser(1700,value=>{const n=Number(value);if(!Number.isFinite(n) || Number.isInteger(n)&&!Number.isSafeInteger(n))throw new Error('Repository numeric exceeds safe range');return n;});
export type WriterPurpose='other'|'read_demand'|'ingest'|'repair'|'enqueue'|'publication';
// Diagnostics only. Admission/yield decisions continue to use WriterPurpose.
export type WriterOperation='pr_validators'|'pr_refresh_enqueue'|'pr_refresh_publish'|'pr_read_start'|'activity_landings_reserve'|'activity_landings_publish'|'configure'|'demand'|'job_select';
export interface RepositoryStore {
  readonly backend:'sqlite'|'postgres';
  readonly transactional?:boolean;
  readonly queryCount?:number;
  readonly queuedOtherWork?:number;
  exec<T=any>(sql:string,...bindings:Bind[]):Promise<Rows<T>>;
  batch(statements:Statement[]):Promise<Rows<any>[]>;
  transaction<T>(apply:(store:RepositoryStore)=>Promise<T>,purpose?:WriterPurpose,operation?:WriterOperation):Promise<T>;
  health():Promise<Record<string,unknown>>;
  prepareMonitoring?(analyzeTables:boolean,attemptToken?:number):Promise<any>;
  outbound?():Record<string,unknown>;
}
export class SerialLane {
  private tail:Promise<unknown>=Promise.resolve();
  private waitingOther=0;
  get queuedOtherWork():number{return this.waitingOther;}
  run<T>(work:()=>Promise<T>,purpose:WriterPurpose='other'):Promise<T> {
    if(purpose!=='publication')this.waitingOther++;
    const begin=()=>{if(purpose!=='publication')this.waitingOther--;return work();};
    const pending=this.tail.then(begin,begin);this.tail=pending.catch(()=>{});return pending;
  }
}
// SQLite otherwise prefers the kind prefix of resource_parent and walks
// every resource of that kind before checking collection membership.
// Its CROSS JOIN fixes loop order: narrow indexed members first, then PK
// resource lookups. Keep ordinary joins for PostgreSQL's statistics planner.
export function collectionJoin(store:RepositoryStore):string {return store.backend==='sqlite'?'CROSS JOIN':'JOIN';}
// A failed publication may reuse its fetched input only after the adapter
// confirms rollback of a healthy apply transaction. The symbol survives test
// bundles without exposing diagnostics or changing public error messages.
const MEMBER_ROLLBACK=Symbol.for('tauceti.repository.member_rollback');
const MEMBER_DATA=Symbol.for('tauceti.repository.member_data');
const BACKEND_FAILURE=Symbol.for('tauceti.repository.backend_failure');
export const canReplayPublication=(error:unknown):boolean=>Boolean(error && typeof error==='object' && (error as any)[MEMBER_ROLLBACK]===true);
export const isMemberDataFailure=(error:unknown):boolean=>canReplayPublication(error) && (error as any)[MEMBER_DATA]===true;
export const isBackendFailure=(error:unknown):boolean=>Boolean(error && typeof error==='object' && (error as any)[BACKEND_FAILURE]===true) && !canReplayPublication(error);
const markMemberRollback=(error:unknown):void=>{if(error && typeof error==='object')try{Object.defineProperty(error,MEMBER_ROLLBACK,{value:true,configurable:true});}catch{}};
const markMemberData=(error:unknown):void=>{if(error && typeof error==='object')try{Object.defineProperty(error,MEMBER_DATA,{value:true,configurable:true});}catch{}};
const clearMemberRollback=(error:unknown):void=>{if(error && typeof error==='object')try{delete (error as any)[MEMBER_ROLLBACK];delete (error as any)[MEMBER_DATA];}catch{}};
export const markBackendFailure=(error:unknown):void=>{if(error && typeof error==='object')try{Object.defineProperty(error,BACKEND_FAILURE,{value:true});}catch{}};
const memberApplyError=(error:unknown):boolean=>error instanceof Error && !(error instanceof StoreFenced) && (!(error instanceof StoreUnavailable) || Boolean(error.sqlstate && /^(22|23|54)/.test(error.sqlstate)));
const sqliteMemberError=(error:any):boolean=>{
  if(Number.isInteger(error?.errcode))return [18,19,20].includes(error.errcode&255);
  if(typeof error?.code==='string')return /^SQLITE_(TOOBIG|CONSTRAINT|MISMATCH)(_|$)/.test(error.code);
  // Workerd SqlStorage exposes only this terminal native result-code suffix.
  // This is used only for an exception caught from the SQL API, never for an
  // arbitrary application message. Unknown/control/storage codes do not replay.
  const code=typeof error?.message==='string'?/: (SQLITE_[A-Z0-9_]+)(?: \(extended: SQLITE_[A-Z0-9_]+\))?$/.exec(error.message)?.[1]:undefined;
  return code!==undefined && ['SQLITE_TOOBIG','SQLITE_CONSTRAINT','SQLITE_MISMATCH'].includes(code);
};
export class SQLiteStore implements RepositoryStore {
  readonly backend='sqlite';
  private queryFailure:unknown;
  constructor(private storage:DurableObjectStorage,private sql:SqlStorage,readonly transactional=false,private generation='sqlite-v1',private context?:DurableObjectState,private lane=new SerialLane()) {}
  get queuedOtherWork():number{return this.lane.queuedOtherWork;}
  async exec<T=any>(query:string,...bindings:Bind[]):Promise<Rows<T>> {
    if(!this.transactional)return this.lane.run(()=>new SQLiteStore(this.storage,this.sql,true,this.generation,this.context,this.lane).exec<T>(query,...bindings));
    if(!/^\s*SELECT\b/i.test(query))this.fence();
    try{return new Rows(this.sql.exec<any>(query,...bindings).toArray());}catch(error){this.queryFailure=error;if(!sqliteMemberError(error))markBackendFailure(error);throw error;}
  }
  private fence():void {
    try {
      const row=this.sql.exec<any>("SELECT value FROM meta WHERE key='backend_owner'").toArray()[0];
      if(row) {const owner=JSON.parse(row.value);if(owner.backend!=='sqlite' || owner.generation!==this.generation)throw new StoreFenced();}
    }catch(error){this.queryFailure=error;throw error;}
  }
  private batchSync(statements:Statement[]):Rows<any>[] {if(statements.some(s=>!/^\s*SELECT\b/i.test(s.sql)))this.fence();return statements.map(s=>{try{return new Rows(this.sql.exec(s.sql,...(s.bindings || [])).toArray());}catch(error){this.queryFailure=error;if(!sqliteMemberError(error))markBackendFailure(error);throw error;}});}
  async batch(statements:Statement[]):Promise<Rows<any>[]> {
    if(this.transactional)return this.batchSync(statements);
    return this.lane.run(async()=>statements.some(s=>!/^\s*SELECT\b/i.test(s.sql))?this.storage.transactionSync(()=>this.batchSync(statements)):this.batchSync(statements));
  }
  transaction<T>(apply:(store:RepositoryStore)=>Promise<T>,purpose:WriterPurpose='other',operation?:WriterOperation):Promise<T> {
    if(this.transactional) return apply(this);
    const queued=Date.now();
    return this.lane.run(async()=>{
      const started=Date.now();
      let result:T,error:unknown,failed=false;
      const scoped=new SQLiteStore(this.storage,this.sql,true,this.generation,this.context,this.lane);let applyError:unknown;
      const execute=async()=>{try{result=await this.storage.transaction(async()=>{this.fence();try{return await apply(scoped);}catch(cause){applyError=cause;throw cause;}});}catch(cause){
        failed=true;error=cause;clearMemberRollback(cause);
        if(cause===applyError && memberApplyError(cause) && (scoped.queryFailure===undefined || sqliteMemberError(scoped.queryFailure))){
          markMemberRollback(cause);
          if(cause===scoped.queryFailure && sqliteMemberError(cause))markMemberData(cause);
        }
        if(!canReplayPublication(cause))markBackendFailure(cause);
      }};
      // Input gating also protects native SQLite recovery/status operations
      // from observing an open async savepoint. Catch inside the gate: an
      // expected transaction rollback must not reset the Durable Object.
      try {if(this.context)await this.context.blockConcurrencyWhile(execute);else await execute();}
      finally {const finished=Date.now();if(finished-queued>=250)log('repository_transaction_slow',{purpose,operation:operation ?? null,wait_ms:started-queued,transaction_ms:finished-started,failed});}
      if(failed)throw error;return result!;
    },purpose);
  }
  async health():Promise<Record<string,unknown>> {return {backend:this.backend,schema_version:5,database_size:this.sql.databaseSize};}
}
// The column list also documents positional inserts retained by the SQLite
// adapter. PostgreSQL adds an archive sequence without changing these inserts.
export const columns:Record<string,string[]>={
  meta:['key','value'],resources:['kind','id','parent','data','updated','observed','revision','tombstone'],
  activity_loc_cache:['number','fingerprint','data'],activity_landings:['number','head','at','source'],
  collections:['key','operation','args','complete','verified','revision','etag'],members:['collection','id'],
  deliveries:['id','event','received'],changes:['revision','at','kind','id','tombstone'],
  jobs:['key','query','priority','due','started','cursor','page','api','etag','attempt','dirty','generation','start_generation','lane','lease_id','lease_until','requested_at','created_at','wait_reason','nested'],
  demand:['key','at'],cursors:['id','expires','payload'],retired_objects:['key','at'],head_prs:['sha','number'],
  survey_fragments:['kind','id','parent','data'],survey_state:['number','data','revision'],survey_inventory:['number','data'],
  survey_hydrated:['number','contract'],survey_dirty:['number'],survey_hydration_failures:['number','retry_at'],
  repair_metrics:['at','event','lane','operation','value'],query_metrics:['operation','coverage','requests','last_at']
};
// Shared import/read contract. The activity number columns intentionally stay
// TEXT in PostgreSQL while SQLite's source uses INTEGER there.
const integerColumns=new Set(['observed','revision','tombstone','complete','verified','received','at','priority','due','started','page','attempt','dirty','generation','start_generation','lease_until','requested_at','created_at','retry_at','requests','last_at']);
export function integerColumn(table:string,name:string):boolean {return integerColumns.has(name) || table==='repair_metrics' && name==='value';}
export function schemaName(repository:string):string {
  if(!/^[0-9]{1,50}$/.test(repository)) throw new Error('Invalid repository identity');
  return 'github_state_'+repository;
}
// Explicit translations of the service's SQLite dialect. No user-provided SQL
// or identifiers enter this boundary. Values always use driver parameters.
export function postgresSQL(input:string,schema:string):string {
  let sql=input;
  sql=sql.replace(/INSERT OR REPLACE INTO activity_loc_cache VALUES\s*\(([^)]+)\)/gi,'INSERT INTO activity_loc_cache VALUES($1) ON CONFLICT(number) DO UPDATE SET fingerprint=excluded.fingerprint,data=excluded.data');
  if(/INSERT OR IGNORE/i.test(sql)) sql=sql.replace(/INSERT OR IGNORE/i,'INSERT')+' ON CONFLICT DO NOTHING';
  sql=sql.replace(/\bINSERT INTO (\w+)(\s+)(VALUES|SELECT)/gi,(_,table,space,op)=>{
    if(!columns[table])throw new Error('Unknown repository table');
    return `INSERT INTO ${table}(${columns[table].join(',')}) ${op}`;
  });
  sql=sql.replace(/json_(extract|type)\((\w+(?:\.\w+)?),'\$\.([\w.]+)'\)/g,(_,operation,value,path)=>{
    // Only project the requested field. PostgreSQL otherwise rejects escaped NULs
    // and lone surrogates anywhere in an unrelated payload body. Substitute
    // those escapes in this temporary projection, preserving the stored text.
    if(path==='state' || path==='headRefOid')value=`regexp_replace(${value},'\\\\u(0000|[dD][89a-fA-F][0-9a-fA-F]{2})','\\\\ufffd','g')`;
    const expression=`(${value}::jsonb ${operation==='type'?'#>':'#>>'} '{${path.replaceAll('.',',')}}')`;
    return operation==='type'?`jsonb_typeof(${expression})`:expression;
  });
  sql=sql.replace(/COALESCE\((\([^)]*::jsonb[^)]*\)),1\)/g,'COALESCE(($1)::bigint,1)');
  // SQLite casts nonnumeric IDs to zero and clamps oversized integers. Decimal
  // ordering avoids both PostgreSQL cast errors and bigint overflow.
  sql=sql.replace(/CAST\((\w+\.id) AS INTEGER\)/g,(_,field)=>`CASE WHEN ${field} ~ '^[0-9]+$' THEN ${field}::numeric ELSE 0 END`);
  sql=sql.replace(/\b(MAX|MIN)\((dirty|due|generation|1),/g,(_,fn,value)=>`${fn==='MAX'?'GREATEST':'LEAST'}(${value},`);
  sql=sql.replace(/MIN\(MAX\(due,lease_until\)\)/g,'MIN(GREATEST(due,lease_until))');
  // SQLite allows unqualified target columns in conflict updates; PostgreSQL
  // requires the target name when an excluded column has the same name.
  sql=sql.replace(/(DO UPDATE SET[\s\S]*)/i,part=>part.replace(/(?<![.\w])(dirty|generation|due|wait_reason|attempt|requests)(?![\w])/g,(name,_column,offset)=>{
    const before=part.slice(0,offset).trimEnd(),after=part.slice(offset+name.length);
    if(/^\s*=/.test(after) && (before.endsWith(',') || /\bSET$/.test(before)))return name;
    const table=sql.match(/INSERT INTO (\w+)/i)?.[1];return table?`${table}.${name}`:name;
  }));
  // SQL boolean values need an explicit integer cast for SQLite-style SUMs.
  sql=sql.replace(/SUM\((h\.contract=\?|s\.number IS NOT NULL|due<=\?(?: AND lease_until<=\?)?|lease_until>\?)\)/g,'SUM(($1)::int)');
  sql=sql.replace(/\bFROM \(SELECT operation,value,/,'FROM (SELECT operation,value,').replace(/\) WHERE rank>=/,') AS ranked WHERE rank>=');
  // PostgreSQL sorts nulls last by default; match SQLite for due/ordering keys.
  // Production integer timestamps are non-null; archive rowids are explicit.
  for(const table of Object.keys(columns).concat(['backend_control','capacity_statistics'])) {
    sql=sql.replace(new RegExp('\\b(FROM|JOIN|INTO|UPDATE) '+table+'\\b','g'),`$1 "${schema}".${table}`);
  }
  let parameter=0;
  // Question marks in quoted literals are not parameters.
  sql=sql.replace(/'(?:''|[^'])*'|\?/g,part=>part==='?'?'$'+(++parameter):part);
  return sql;
}
export class StoreUnavailable extends Error {readonly sqlstate?:string;constructor(code?:string){const safe=code && /^[0-9A-Z]{5}$/.test(code)?code:undefined;super('Repository database unavailable'+(safe?' ('+safe+')':''));this.sqlstate=safe;markBackendFailure(this);}}
export class StoreFenced extends Error {constructor(){super('Repository backend generation fenced');markBackendFailure(this);}}
type TransactionPhase='connect'|'begin'|'session'|'owner'|'apply'|'commit';
interface SessionIdentity {pid:number;started_us:string;token:string}
type SessionCleanup='terminated'|'already_ended'|'unconfirmed'|'expired';
interface DiscardedSession extends SessionIdentity {discardedAt:number;transaction_id:string}
type ConnectionFailure='client_timeout'|'server_error'|'driver_error'|'connection_lost'|'connect_timeout'|'rollback_timeout'|'application'|'fenced'|'commit_rejected';
interface ConnectionState {closed:boolean;ending:boolean;error?:StoreUnavailable;dataError?:StoreUnavailable;phase:TransactionPhase;code?:string;reason?:ConnectionFailure;commitUncertain?:boolean;cleanup?:'rollback_timeout'|'rollback_error';session?:SessionIdentity;ownerAttempted?:boolean;abort?:(error:StoreUnavailable)=>void}
const sqlState=(code:unknown):string|undefined=>typeof code==='string' && /^[0-9A-Z]{5}$/.test(code)?code:undefined;
const serverCode=(error:any):string|undefined=>['ERROR','FATAL','PANIC'].includes(error?.severity)?sqlState(error?.code):undefined;
// A client-side timeout does not cancel the active PostgreSQL query. Queuing
// ROLLBACK behind it can consume another full query timeout. Close that
// transport and confirm its server session has ended before another writer;
// a healthy connection still rolls back ordinary errors.
const unusableConnection=(code:string|undefined)=>!code || code.startsWith('08') || ['57P01','57P02','57P03'].includes(code);
const storeLog=(event:string,fields:Record<string,unknown>)=>{try{log(event,fields);}catch{}};
// Workers allow six simultaneous open outbound connections per Durable Object
// invocation context; further fetches wait in an unbounded runtime queue while
// their abort timers run. Under load every read then times out before it
// starts, and writer transactions stall on R2 or HTTP calls queued behind
// those reads. This gate keeps HTTP reads below the limit (leaving room for the
// writer WebSocket and R2), starts each read timeout only once admitted, and
// sheds reads that cannot be admitted promptly instead of stacking them.
// Diagnostics only: times each Neon HTTP fetch to its response headers and to
// its fully read body, separating a pre-header transport stall from a slow
// response body. The driver reads the buffered body exactly as before.
const fetchPhases={started:0,headers:0,bodies:0,failed_before_headers:0,failed_reading_body:0,max_headers_ms:0,max_body_ms:0,header_timeouts:0,retries:0,retry_successes:0,pre:new Map<number,number>(),body:new Map<number,number>(),next:0};
export function fetchPhaseSnapshot():Record<string,unknown> {
  const now=Date.now(),oldest=(m:Map<number,number>)=>{const v=Math.min(...m.values());return Number.isFinite(v)?now-v:null;};
  const {pre,body,next,...counts}=fetchPhases;
  return {...counts,awaiting_headers:pre.size,oldest_awaiting_headers_ms:oldest(pre),reading_body:body.size,oldest_reading_body_ms:oldest(body)};
}
// Healthy Neon HTTP responses send headers within about 100 ms. Some fetches
// from the Durable Object instead receive no headers at all until the caller's
// 15 s abort, while Neon shows no matching query. A request that the database
// itself runs read-only (a Neon batch with the read-only header, which every
// HTTP read here uses) is abandoned and sent again when its headers do not
// arrive promptly, within the caller's own deadline. Nothing else is resent.
export const HEADER_TIMEOUT_MS=1500;
export const HEADER_ATTEMPTS=4;
export function resendable(init:any):boolean {
  const headers=init?.headers,readOnly=headers instanceof Headers?headers.get('Neon-Batch-Read-Only'):headers?.['Neon-Batch-Read-Only'];
  if(readOnly!=='true' || typeof init?.body!=='string')return false;
  let parsed:any;try{parsed=JSON.parse(init.body);}catch{return false;}
  return Array.isArray(parsed?.queries) && parsed.queries.length>0 && parsed.queries.every((q:any)=>typeof q?.query==='string' && /^\s*SELECT\b/i.test(q.query));
}
class HeaderTimeout extends Error {constructor(){super('Neon response headers timed out');}}
// The caller's abort stays attached until the body has been read; only the
// header timer ends when headers arrive.
async function attemptFetch(input:any,init:any,outer:AbortSignal|undefined,limit:number|undefined):Promise<{response:Response,release:()=>void}> {
  const controller=new AbortController();let timer:ReturnType<typeof setTimeout>|undefined,timedOut=false;
  const forward=()=>controller.abort(outer?.reason);
  const release=()=>outer?.removeEventListener('abort',forward);
  if(outer?.aborted)forward();else outer?.addEventListener('abort',forward);
  if(limit!==undefined)timer=setTimeout(()=>{timedOut=true;controller.abort(new HeaderTimeout());},limit);
  try{return {response:await fetch(input,{...init,signal:controller.signal}),release};}
  catch(error){release();if(timedOut && !outer?.aborted)throw new HeaderTimeout();throw error;}
  finally{if(timer)clearTimeout(timer);}
}
async function timedFetch(input:any,init?:any):Promise<Response> {
  const id=fetchPhases.next++,started=Date.now();fetchPhases.started++;fetchPhases.pre.set(id,started);
  const outer:AbortSignal|undefined=init?.signal,retry=resendable(init);
  let attempt:{response:Response,release:()=>void}|undefined;
  try{
    for(let n=1;;n++){
      const last=!retry || n>=HEADER_ATTEMPTS;
      try{attempt=await attemptFetch(input,init,outer,last?undefined:HEADER_TIMEOUT_MS);if(n>1)fetchPhases.retry_successes++;break;}
      catch(error){
        if(!(error instanceof HeaderTimeout) || last || outer?.aborted)throw error;
        fetchPhases.header_timeouts++;fetchPhases.retries++;
      }
    }
  }catch(error){fetchPhases.failed_before_headers++;throw error;}finally{fetchPhases.pre.delete(id);}
  const {response,release}=attempt!;
  const headersAt=Date.now();fetchPhases.headers++;fetchPhases.max_headers_ms=Math.max(fetchPhases.max_headers_ms,headersAt-started);fetchPhases.body.set(id,headersAt);
  try{
    const body=await response.arrayBuffer();fetchPhases.bodies++;fetchPhases.max_body_ms=Math.max(fetchPhases.max_body_ms,Date.now()-headersAt);
    return new Response(body,{status:response.status,statusText:response.statusText,headers:response.headers});
  }catch(error){fetchPhases.failed_reading_body++;throw error;}finally{fetchPhases.body.delete(id);release();}
}
export const __timedFetchForTests=timedFetch;
// Send HTTP queries to the compute endpoint's own hostname, which Neon also
// serves at /sql, rather than the driver default of the shared regional
// api.<cell> host. The writer WebSocket already uses this hostname and stays
// healthy while HTTP requests to the shared host stall before headers.
export const neonFetchEndpoint=(host:string):string=>'https://'+host+'/sql';
try{if(neonConfig){neonConfig.fetchFunction=timedFetch;neonConfig.fetchEndpoint=neonFetchEndpoint;}}catch{}
export const HTTP_READ_CONCURRENCY=3;
export const HTTP_READ_QUEUE=32;
export const HTTP_READ_ADMISSION_MS=5000;
interface GateEntry {deadline:number;admit:()=>void;expire:()=>void}
export class OutboundGate {
  private active=0;
  // Priority entries (session cleanup) are admitted before ordinary reads.
  private queue:GateEntry[]=[];private priority:GateEntry[]=[];
  private counters={admitted:0,queued:0,shed_queue_full:0,shed_wait:0,cancelled:0,completed:0,failed:0,wait_ms_total:0,max_wait_ms:0,busy_ms_total:0,max_busy_ms:0};
  private lastShedAt:number|null=null;private lastShedLog=0;
  // Diagnostics only: SQL text prefixes (never bindings) of running reads and
  // of recent reads that failed or took over a second.
  private running=new Map<number,{label:string,started:number}>();private nextRun=0;
  private slow:{label:string,busy_ms:number,ok:boolean,at:number}[]=[];
  private buckets={under_100ms:0,under_1s:0,under_5s:0,over_5s:0};
  constructor(readonly limit=HTTP_READ_CONCURRENCY,readonly maxQueue=HTTP_READ_QUEUE,readonly maxWaitMs=HTTP_READ_ADMISSION_MS) {}
  get inFlight():number{return this.active;}
  get waiting():number{return this.queue.length+this.priority.length;}
  private shedError(reason:'queue_full'|'wait'):StoreUnavailable {
    if(reason==='queue_full')this.counters.shed_queue_full++;else this.counters.shed_wait++;
    const now=Date.now();this.lastShedAt=now;
    if(now-this.lastShedLog>=10000){this.lastShedLog=now;storeLog('repository_http_shed',{reason,...this.snapshot()});}
    return new StoreUnavailable();
  }
  private remove(entry:GateEntry):boolean {
    for(const list of [this.priority,this.queue]){const index=list.indexOf(entry);if(index>=0){list.splice(index,1);return true;}}
    return false;
  }
  // Hand a released slot to the next unexpired waiter; expired waiters are
  // shed here even if their timer has not fired yet.
  private release():void {
    const now=Date.now();
    for(;;){
      const next=this.priority.shift() ?? this.queue.shift();
      if(!next){this.active--;return;}
      if(next.deadline<now){next.expire();continue;}
      next.admit();return;
    }
  }
  async run<T>(work:()=>Promise<T>,options:{priority?:boolean,signal?:AbortSignal,label?:string}={}):Promise<T> {
    const enqueued=Date.now();
    if(options.signal?.aborted)throw new StoreUnavailable();
    if(this.active>=this.limit) {
      if(!options.priority && this.queue.length>=this.maxQueue)throw this.shedError('queue_full');
      this.counters.queued++;
      await new Promise<void>((resolve,reject)=>{
        let timer:ReturnType<typeof setTimeout>|undefined,settled=false;
        const finish=(error?:unknown)=>{if(settled)return;settled=true;if(timer)clearTimeout(timer);options.signal?.removeEventListener('abort',cancel);if(error)reject(error);else resolve();};
        const entry:GateEntry={deadline:enqueued+this.maxWaitMs,admit:()=>finish(),expire:()=>finish(this.shedError('wait'))};
        const cancel=()=>{if(this.remove(entry)){this.counters.cancelled++;finish(new StoreUnavailable());}};
        timer=setTimeout(()=>{if(this.remove(entry))entry.expire();},this.maxWaitMs);
        options.signal?.addEventListener('abort',cancel);
        (options.priority?this.priority:this.queue).push(entry);
      });
      // release() hands its slot over without decrementing active.
    }else this.active++;
    const admittedAt=Date.now(),wait=admittedAt-enqueued;
    this.counters.admitted++;this.counters.wait_ms_total+=wait;this.counters.max_wait_ms=Math.max(this.counters.max_wait_ms,wait);
    let ok=false;const run=this.nextRun++,label=(options.label ?? 'unlabelled').replace(/\s+/g,' ').slice(0,120);this.running.set(run,{label,started:admittedAt});
    try{const result=await work();ok=true;return result;}
    finally{
      this.running.delete(run);
      const busy=Date.now()-admittedAt;this.counters.busy_ms_total+=busy;this.counters.max_busy_ms=Math.max(this.counters.max_busy_ms,busy);
      this.buckets[busy<100?'under_100ms':busy<1000?'under_1s':busy<5000?'under_5s':'over_5s']++;
      if(!ok || busy>=1000){this.slow.push({label,busy_ms:busy,ok,at:Date.now()});if(this.slow.length>20)this.slow.shift();}
      if(ok)this.counters.completed++;else this.counters.failed++;
      this.release();
    }
  }
  snapshot():Record<string,unknown> {
    const c=this.counters,oldest=Math.min(...this.priority.concat(this.queue).map(e=>e.deadline));
    return {limit:this.limit,max_queue:this.maxQueue,max_wait_ms_allowed:this.maxWaitMs,in_flight:this.active,waiting:this.waiting,oldest_wait_ms:Number.isFinite(oldest)?Math.max(0,Date.now()-(oldest-this.maxWaitMs)):null,...c,
      mean_wait_ms:c.admitted?Math.round(c.wait_ms_total/c.admitted):null,mean_busy_ms:c.completed+c.failed?Math.round(c.busy_ms_total/(c.completed+c.failed)):null,last_shed_at:this.lastShedAt,
      busy_buckets:{...this.buckets},running:[...this.running.values()].map(r=>({label:r.label,age_ms:Date.now()-r.started})),recent_slow:[...this.slow]};
  }
}
// Reads over persistent WebSocket sessions to the compute endpoint, the same
// transport as the writer, instead of HTTP. Each session is read-only at the
// database (default_transaction_read_only) and bounded by statement_timeout.
// The outbound gate bounds sessions in use; idle sessions are reused and
// replaced after READER_MAX_AGE_MS or on any transport failure.
export const READER_MAX_AGE_MS=60000;
export const READER_QUERY_MS=15000;
interface Reader {client:Client;broken:boolean;opened:number}
export class ReaderPool {
  private idle:Reader[]=[];
  private counters={opened:0,open:0,reused:0,discarded:0,connect_failures:0,timeouts:0};
  constructor(private url:string) {}
  snapshot():Record<string,unknown> {return {...this.counters,idle:this.idle.length};}
  private discard(reader:Reader):void {
    if(reader.broken && (reader as any).gone)return;
    (reader as any).gone=true;reader.broken=true;this.counters.open--;this.counters.discarded++;
    try{void reader.client.end().catch(()=>{});}catch{}
    try{reader.client.neonConfig.destroy();}catch{}
  }
  private async connect():Promise<Reader> {
    let client:Client;try{client=new Client({connectionString:this.url,connectionTimeoutMillis:10000,query_timeout:READER_QUERY_MS});}catch{throw new StoreUnavailable();}
    const reader:Reader={client,broken:false,opened:Date.now()};this.counters.opened++;this.counters.open++;
    client.on('error',()=>{reader.broken=true;});client.on('end',()=>{reader.broken=true;});
    let timer:ReturnType<typeof setTimeout>|undefined;
    try{
      await Promise.race([(async()=>{await client.connect();await client.query("SET default_transaction_read_only=on; SET statement_timeout='15s'; SET idle_in_transaction_session_timeout='30s'");})(),
        new Promise<never>((_,reject)=>{timer=setTimeout(()=>reject(new StoreUnavailable('08001')),10000);})]);
    }catch(error){this.counters.connect_failures++;this.discard(reader);throw error;}finally{if(timer)clearTimeout(timer);}
    return reader;
  }
  async use<T>(work:(client:Client)=>Promise<T>):Promise<T> {
    let reader:Reader|undefined;
    while(this.idle.length){const candidate=this.idle.pop()!;if(candidate.broken || Date.now()-candidate.opened>READER_MAX_AGE_MS)this.discard(candidate);else{reader=candidate;this.counters.reused++;break;}}
    reader ??= await this.connect();
    let timer:ReturnType<typeof setTimeout>|undefined,healthy=false;
    try{
      const result=await Promise.race([work(reader.client),new Promise<never>((_,reject)=>{timer=setTimeout(()=>{this.counters.timeouts++;reader!.broken=true;reject(new StoreUnavailable('57014'));},READER_QUERY_MS+1000);})]);
      healthy=true;return result;
    }catch(error){
      // A server ERROR leaves the session usable; anything else discards it.
      if((error as any)?.severity==='ERROR' && !reader.broken)healthy=true;
      throw error;
    }finally{
      if(timer)clearTimeout(timer);
      if(healthy && !reader.broken)this.idle.push(reader);else this.discard(reader);
    }
  }
}
export class PostgresStore implements RepositoryStore {
  readonly backend='postgres';
  readonly schema:string;

  private http;
  private queriesExecuted=0;
  get queryCount():number{return this.queriesExecuted;}
  private writer=new SerialLane();
  get queuedOtherWork():number{return this.writer.queuedOtherWork;}
  private discarded:DiscardedSession[]=[];
  private nextCleanupAt=0;
  private cleanupFailures=0;
  private directEndpoint=false;
  // Shared by every scoped store of this repository object.
  private gate=new OutboundGate();
  // Set by the repository object; absent means HTTP reads.
  readers?:ReaderPool;
  private writerSocket:{open:number,since:number|null,opened:number}={open:0,since:null,opened:0};
  outbound():Record<string,unknown> {return {read_transport:this.readers?'websocket':'http',http_reads:this.gate.snapshot(),http_fetch:fetchPhaseSnapshot(),readers:this.readers?.snapshot() ?? null,writer_socket:{...this.writerSocket,open_ms:this.writerSocket.since===null?null:Date.now()-this.writerSocket.since},writer_queued:this.writer.queuedOtherWork};}
  private gated<T>(work:()=>Promise<T>,options?:{priority?:boolean,signal?:AbortSignal,label?:string}):Promise<T> {return this.gate.run(work,options);}
  constructor(private url:string,repository:string,private generation:string,private client?:Client,private diagnostics:{connections:number,queries:number,lastConnection?:number}={connections:0,queries:0},private connectionState?:ConnectionState,private onQuery?:()=>void) {
    this.schema=schemaName(repository);
    // Match the driver host parser. Endpoint overrides or ambiguous URL
    // separators never permit session termination, even on a Neon hostname.
    try{const parsed=new URL(url),host=new URL('http:'+url.slice(parsed.protocol.length)).hostname.toLowerCase().replace(/\.$/,'');this.directEndpoint=!parsed.searchParams.has('host')&&!url.includes('\\')&&host.endsWith('.neon.tech')&&!host.split('.')[0].endsWith('-pooler');}catch{}
    try{this.http=neon(url);}catch{throw new StoreUnavailable();}
  }
  get transactional():boolean {return Boolean(this.client);}
  private async run<T>(action:()=>Promise<T>,connection=this.connectionState):Promise<T> {
    if(connection?.error)throw connection.error;
    this.queriesExecuted++;
    this.onQuery?.();
    try{return await action();}catch(error){
      if(connection?.error)throw connection.error;
      if(error instanceof StoreFenced || error instanceof StoreUnavailable)throw error;
      const code=serverCode(error);
      const serverError=(error as any)?.severity==='ERROR';
      const reason=(error as any)?.message==='Query read timeout'?'client_timeout':['ERROR','FATAL','PANIC'].includes((error as any)?.severity)?'server_error':'driver_error';
      if(connection){
        connection.code=code;connection.reason=reason;
        if(connection.phase==='connect' || !serverError || unusableConnection(code)){
          connection.error=new StoreUnavailable(code);connection.abort?.(connection.error);
        }
      }else if(code!=='P0308')storeLog('repository_database_failure',{phase:'http',code:code ?? null,reason});
      if(code!=='P0308'){this.diagnostics.queries++;if(unusableConnection(code) || code?.startsWith('28') || !serverError){this.diagnostics.connections++;this.diagnostics.lastConnection=Date.now();}}
      // Driver errors can contain connection strings or parameter data.
      if(code==='P0308')throw new StoreFenced();
      const unavailable=new StoreUnavailable(code);
      if(connection && serverError && code && /^(22|23|54)/.test(code))connection.dataError=unavailable;
      throw unavailable;
    }
  }
  async exec<T=any>(sql:string,...bindings:Bind[]):Promise<Rows<T>> {
    const query=postgresSQL(sql,this.schema);
    // Standalone writes are still fenced and atomic. The callback path uses a
    // request-scoped interactive connection, closed before the request returns.
    if(!this.client && !/^\s*SELECT\b/i.test(sql))return (await this.batch([{sql,bindings}]))[0] as Rows<T>;
    if(!this.client && this.readers){const readers=this.readers;return this.run(async()=>new Rows<T>(await this.gated(()=>readers.use(async client=>(await client.query(query,bindings)).rows),{label:sql})));}
    return this.run(async()=>new Rows<T>(this.client?(await this.client.query(query,bindings)).rows:(await this.gated(()=>this.http.transaction([this.http.query(query,bindings)],{readOnly:true,fetchOptions:{signal:AbortSignal.timeout(15000)}}),{label:sql}))[0] as T[]));
  }
  async batch(statements:Statement[]):Promise<Rows<any>[]> {
    if(!this.client && statements.some(s=>!/^\s*SELECT\b/i.test(s.sql)))return this.transaction(tx=>tx.batch(statements));
    return this.executeBatch(statements);
  }
  private async executeBatch(statements:Statement[]):Promise<Rows<any>[]> {
    if(!statements.length)return [];
    if(!this.client && this.readers){
      // HTTP batches here are stored reads only (mutations entered transaction() above).
      const readers=this.readers;
      return this.selectBatch(statements,query=>this.run(()=>this.gated(()=>readers.use(client=>query(client)),{label:'batch['+statements.length+'] '+statements.map(s=>s.sql.slice(0,40)).join(' | ')})));
    }
    if(this.client) {
      if(statements.every(s=>/^\s*SELECT\b/i.test(s.sql)))return this.selectBatch(statements,query=>this.run(()=>query(this.client!)));
      const tables=statements.map(s=>s.sql.match(/^\s*(?:INSERT(?: OR IGNORE)? INTO|UPDATE|DELETE FROM) (\w+)/i)?.[1]);
      if(tables.every(Boolean) && new Set(tables).size===tables.length && statements.every(s=>!/\b(RETURNING|SELECT)\b/i.test(s.sql))) {
        const bindings:Bind[]=[],parts=statements.map((s,i)=>{
          const offset=bindings.length;
          const query=postgresSQL(s.sql,this.schema).replace(/'(?:''|[^'])*'|\$(\d+)/g,(part,n)=>n?'$'+(Number(n)+offset):part);
          bindings.push(...(s.bindings || []));return `s${i} AS (${query} RETURNING 1)`;
        });
        await this.run(()=>this.client!.query('WITH '+parts.join(',')+' SELECT 1',bindings));
        return statements.map(()=>new Rows([]));
      }
      return Promise.all(statements.map(s=>this.exec(s.sql,...(s.bindings || []))));
    }
    // Standalone mutations enter transaction() above. HTTP batches are stored
    // reads only; assert_writer remains part of the migration schema contract.
    return this.run(async()=>{
      const results=await this.gated(()=>{
        const queries=statements.map(s=>this.http.query(postgresSQL(s.sql,this.schema),s.bindings || []));
        return this.http.transaction(queries,{readOnly:true,isolationLevel:'RepeatableRead',fetchOptions:{signal:AbortSignal.timeout(15000)}});
      },{label:'batch['+statements.length+'] '+statements.map(s=>s.sql.slice(0,40)).join(' | ')});
      return results.map(rows=>new Rows(rows));
    });
  }
  // One statement, so one snapshot, matching the read-only HTTP batch.
  private async selectBatch(statements:Statement[],execute:(query:(client:Client)=>Promise<any[][]>)=>Promise<any[][]>):Promise<Rows<any>[]> {
    const bindings:Bind[]=[],queries=statements.map((s,i)=>{
      const offset=bindings.length,query=postgresSQL(s.sql,this.schema).replace(/'(?:''|[^'])*'|\$(\d+)/g,(part,n)=>n?'$'+(Number(n)+offset):part);
      bindings.push(...(s.bindings || []));return `(SELECT COALESCE(jsonb_agg(to_jsonb(q${i})),'[]'::jsonb) FROM (${query}) AS q${i})`;
    });
    const rows=await execute(async client=>(await client.query('SELECT jsonb_build_array('+queries.join(',')+') AS batches',bindings)).rows[0].batches as any[][]);
    const check=(value:any):void=>{if(typeof value==='number' && (!Number.isFinite(value) || Number.isInteger(value)&&!Number.isSafeInteger(value)))throw new StoreUnavailable('22003');if(value && typeof value==='object')for(const child of Object.values(value))check(child);};
    check(rows);return rows.map(row=>new Rows(row));
  }
  async transaction<T>(apply:(store:RepositoryStore)=>Promise<T>,purpose:WriterPurpose='other',operation?:WriterOperation):Promise<T> {
    if(this.client)return apply(this);
    const queued=Date.now();
    return this.writer.run(()=>this.transact(apply,Date.now()-queued,purpose,operation),purpose);
  }
  // WebSocket close is asynchronous: the server may retain the transaction
  // after the client times out. Prefer confirmed session cleanup before the
  // next writer, but never make HTTP recovery a permanent write dependency.
  // The database ownership lock protects every writer, including after this
  // bounded local gate expires or the isolate restarts. LOCAL application_name
  // distinguishes a completed transaction from an uncertain COMMIT; it never
  // establishes commit outcome.
  private async cleanupDiscarded(transaction_id:string):Promise<SessionCleanup|undefined> {
    const session=this.discarded[0];if(!session)return;
    // 45s is a local availability heuristic, not a bound on COMMIT/socket I/O.
    // Retain expired identities and retry them fairly; only positive evidence
    // removes one. The database owner lock still protects admitted writers.
    if(Date.now()<this.nextCleanupAt)return this.cleanupBlocked()?'unconfirmed':'expired';
    let timer:ReturnType<typeof setTimeout>|undefined;
    const abort=new AbortController();let result:SessionCleanup='unconfirmed';
    try {
      // Only a direct Neon connection has exclusive ownership of its physical
      // backend. Never signal a pooler backend which could have been reused.
      const direct=this.directEndpoint;
      const rows=await Promise.race([
        this.gated(()=>this.http.query(`SELECT ${direct?'pg_terminate_backend(pid,1000)':'false'} AS terminated FROM pg_stat_activity WHERE pid=$1 AND (EXTRACT(EPOCH FROM backend_start)*1000000)::bigint::text=$2 AND application_name=$3 AND usename=current_user AND datname=current_database()`,[session.pid,session.started_us,session.token],{fetchOptions:{signal:abort.signal}}),{priority:true,signal:abort.signal,label:'session_cleanup'}),
        new Promise<never>((_,reject)=>{timer=setTimeout(()=>{abort.abort();reject(new StoreUnavailable());},3000);})
      ]);
      if(rows.length===0)result='already_ended';
      else if(direct && rows.length===1 && rows[0].terminated===true)result='terminated';
    }catch{this.cleanupFailures++;}finally{if(timer)clearTimeout(timer);}
    // Exactly one attempt per admission, with a shared cooldown. Rotate a
    // failed identity so an older incident cannot starve a newer session.
    this.discarded.shift();if(result==='unconfirmed')this.discarded.push(session);
    this.nextCleanupAt=Date.now()+1000;
    if(result==='unconfirmed'&&!this.cleanupBlocked())result='expired';
    storeLog('repository_postgres_cleanup',{result,transaction_id,discarded_transaction_id:session.transaction_id});return result;
  }
  private cleanupBlocked():boolean {return this.discarded.some(s=>Date.now()-s.discardedAt<45000);}
  private async transact<T>(apply:(store:RepositoryStore)=>Promise<T>,wait_ms:number,purpose:WriterPurpose,operation?:WriterOperation):Promise<T> {
    const transaction_id=crypto.randomUUID(),admission_started_at=Date.now();
    const pendingCleanup=await this.cleanupDiscarded(transaction_id),admission_cleanup_ms=Date.now()-admission_started_at;
    if(this.cleanupBlocked()){
      storeLog('repository_postgres_transaction',{purpose,operation:operation ?? null,phase:'cleanup',code:null,reason:'cleanup_pending',admission_cleanup:pendingCleanup ?? null,session_cleanup:pendingCleanup,outcome:'failed',transaction_id,admission_started_at,admission_cleanup_ms,wait_ms,transaction_ms:0,failed:true,connection_discarded:false});
      throw new StoreUnavailable();
    }
    // Leave time for the server's 15s statement timeout to return its SQLSTATE.
    let client:Client;try{client=new Client({connectionString:this.url,connectionTimeoutMillis:10000,query_timeout:20000});}catch{throw new StoreUnavailable();}
    const started=Date.now();let failed=true,session_cleanup:SessionCleanup|undefined=pendingCleanup;
    this.writerSocket.open++;this.writerSocket.opened++;this.writerSocket.since??=started;
    const phase_ms:Record<TransactionPhase,number>={connect:0,begin:0,session:0,owner:0,apply:0,commit:0};
    let phaseBegan=started,phaseOpen=true,rollback_ms=0,close_ms=0,cleanup_ms=0,application_queries=0;
    const connection:ConnectionState={closed:false,ending:false,phase:'connect'};let disconnected!:(error:StoreUnavailable)=>void;
    const finishPhase=()=>{if(phaseOpen){phase_ms[connection.phase]+=Math.max(0,Date.now()-phaseBegan);phaseOpen=false;}};
    const enterPhase=(phase:TransactionPhase)=>{finishPhase();connection.phase=phase;phaseBegan=Date.now();phaseOpen=true;};
    const lost=new Promise<never>((_,reject)=>{disconnected=reject;});void lost.catch(()=>{});
    connection.abort=disconnected;
    const fail=(code?:string,reason:ConnectionFailure='connection_lost')=>{if(connection.ending || connection.error)return;const safeCode=sqlState(code) || '08006';if(!connection.reason){connection.code=safeCode;connection.reason=reason;}connection.error=new StoreUnavailable(safeCode);this.diagnostics.connections++;this.diagnostics.queries++;this.diagnostics.lastConnection=Date.now();disconnected(connection.error);};
    client.on('error',error=>fail(serverCode(error)));client.on('end',()=>{connection.closed=true;fail();});
    try {
      await this.run(async()=>{
        // Bound WebSocket opening independently of a missing close event.
        // Pipelined authentication finishes with BEGIN's first response.
        const timer=setTimeout(()=>fail('08001','connect_timeout'),10000);
        try{await Promise.race([client.connect(),lost]);}finally{clearTimeout(timer);}
        enterPhase('begin');await client.query("BEGIN; SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='15s'; SET LOCAL idle_in_transaction_session_timeout='30s'");
        enterPhase('session');
        const token='tauceti-ghstate-'+crypto.randomUUID();
        const identity=(await client.query("SELECT pg_backend_pid() AS pid,(EXTRACT(EPOCH FROM backend_start)*1000000)::bigint::text AS started_us,set_config('application_name',$1,true) AS token FROM pg_stat_activity WHERE pid=pg_backend_pid()",[token])).rows[0];
        if(!identity || !Number.isSafeInteger(identity.pid) || identity.pid<=0 || typeof identity.started_us!=='string' || !/^[0-9]+$/.test(identity.started_us) || identity.token!==token)throw new StoreUnavailable();
        connection.session={pid:identity.pid,started_us:identity.started_us,token};
        enterPhase('owner');connection.ownerAttempted=true;
        const control=await client.query(`SELECT generation,backend,schema_version FROM "${this.schema}".backend_control WHERE singleton=1 FOR UPDATE`);
        const row=control.rows[0];
        if(!row || row.backend!=='postgres' || row.generation!==this.generation || row.schema_version!==1)throw new StoreFenced();
      },connection);
      const scoped=new PostgresStore(this.url,this.schema.slice('github_state_'.length),this.generation,client,this.diagnostics,connection);
      scoped.writer=this.writer;scoped.gate=this.gate;scoped.writerSocket=this.writerSocket;
      // Application exceptions must retain their meaning (for example an
      // expired staging page); only driver operations are sanitized.
      enterPhase('apply');
      let result:T;
      try{result=await Promise.race([apply(scoped),lost]);}finally{application_queries=scoped.queryCount;}
      enterPhase('commit');
      const committed=await this.run(async()=>{
        connection.commitUncertain=true;
        try{const result=await client.query('COMMIT');connection.commitUncertain=false;return result;}
        catch(error){if((error as any)?.severity==='ERROR')connection.commitUncertain=false;throw error;}
      },connection);
      if(committed.command!=='COMMIT'){connection.reason='commit_rejected';connection.code='25P02';throw new StoreUnavailable('25P02');}failed=false;return result;
    }catch(error){
      finishPhase();clearMemberRollback(error);
      if(error instanceof StoreFenced)connection.reason='fenced';else connection.reason??='application';
      let rolledBack=false;
      if(!connection.closed && !connection.error){const rollbackStarted=Date.now();let timer:ReturnType<typeof setTimeout>|undefined;try{
        const rollback=await Promise.race([client.query('ROLLBACK'),new Promise<void>(resolve=>{timer=setTimeout(()=>{connection.cleanup='rollback_timeout';fail('08006','rollback_timeout');resolve();},2000);})]);rolledBack=rollback?.command==='ROLLBACK';
      }catch{connection.cleanup='rollback_error';fail();}finally{if(timer)clearTimeout(timer);rollback_ms=Date.now()-rollbackStarted;}}
      if(rolledBack && connection.phase==='apply' && !connection.error && !connection.commitUncertain && memberApplyError(error)){
        markMemberRollback(error);
        if(error===connection.dataError)markMemberData(error);
      }
      if(connection.error)markBackendFailure(error);
      throw error;
    }finally{
      finishPhase();
      connection.ending=true;const closing=Date.now();
      if(!connection.closed && connection.error){try{client.neonConfig.destroy();}catch{}}
      else if(!connection.closed){let timer:ReturnType<typeof setTimeout>|undefined;try{
        await Promise.race([client.end(),new Promise<void>(resolve=>{timer=setTimeout(()=>{try{client.neonConfig.destroy();}catch{}finally{resolve();}},2000);})]);
      }catch{}finally{if(timer)clearTimeout(timer);}}
      close_ms=Date.now()-closing;
      this.writerSocket.open--;if(!this.writerSocket.open)this.writerSocket.since=null;
      const cleaning=Date.now();
      if(connection.error && connection.ownerAttempted && connection.session){
        this.discarded.push({...connection.session,discardedAt:Date.now(),transaction_id});session_cleanup=await this.cleanupDiscarded(transaction_id);
      }
      cleanup_ms=Date.now()-cleaning;
      const transaction_ms=Date.now()-started;
      if(failed || session_cleanup || wait_ms+transaction_ms>=250)storeLog('repository_postgres_transaction',{purpose,operation:operation ?? null,application_queries,phase:connection.phase,code:connection.code ?? null,reason:connection.reason ?? null,cleanup:connection.cleanup ?? null,admission_cleanup:pendingCleanup ?? null,session_cleanup:session_cleanup ?? null,outcome:failed?(connection.commitUncertain?'unknown':'failed'):'committed',transaction_id,admission_started_at,admission_cleanup_ms,transaction_started_at:started,phase_ms,rollback_ms,close_ms,cleanup_ms,wait_ms,transaction_ms,failed,connection_discarded:Boolean(connection.error)});
    }
  }
  async prepareMonitoring(analyzeTables:boolean,attemptToken=Date.now()):Promise<any> {
    return this.run(async()=>{
      const result=await this.gated(()=>this.http.transaction([
        this.http.query("SELECT set_config('statement_timeout','5s',true)"),
        this.http.query(`SELECT "${this.schema}".prepare_monitoring($1,$2,$3) AS result`,[this.generation,analyzeTables,attemptToken])
      ],{fetchOptions:{signal:AbortSignal.timeout(6000)}}),{label:'prepare_monitoring'});
      return result[1][0].result;
    });
  }
  async health():Promise<Record<string,unknown>> {
    let metadata:Record<string,unknown>;
    try {
      const [control,capacity]=await this.batch([
        {sql:'SELECT backend,generation,schema_version FROM backend_control WHERE singleton=1'},
        {sql:'SELECT * FROM capacity_statistics WHERE singleton=1'}
      ]);
      metadata={...control.toArray()[0],connected:true,capacity:capacity.toArray()[0] ?? null};
    }catch {metadata={connected:false,capacity:null};}
    return {backend:this.backend,expected_generation:this.generation,...metadata,session_cleanup_pending:this.discarded.length>0,session_cleanup_state:this.cleanupBlocked()?'blocking':this.discarded.length?'expired':'clear',session_cleanup_count:this.discarded.length,session_cleanup_age_ms:this.discarded.length?Math.max(0,Date.now()-this.discarded.reduce((at,s)=>Math.min(at,s.discardedAt),Date.now())):null,session_cleanup_failures:this.cleanupFailures,connection_failures:this.diagnostics.connections,query_failures:this.diagnostics.queries,last_connection_failure:this.diagnostics.lastConnection ?? null};
  }
}

// A query-scoped snapshot of related read rows. Set-oriented prefetches seed
// the exact existing lookups, so network latency never scales per PR/comment.
export class ReadCacheStore implements RepositoryStore {
  readonly backend;
  readonly dirtyParents=new Set<string>();
  private cache=new Map<string,{rows:any[],sql:string,bindings:Bind[],tables:string[]}>();
  private index=new Map<string,Map<string,Set<string>>>();
  constructor(private source:RepositoryStore){this.backend=source.backend;}
  get transactional():boolean|undefined{return this.source.transactional;}
  get queryCount():number|undefined{return this.source.queryCount;}
  get queuedOtherWork():number|undefined{return this.source.queuedOtherWork;}
  private key(sql:string,bindings:Bind[]):string{return JSON.stringify([sql,bindings]);}
  peek(sql:string,bindings:Bind[]):any[]|undefined{return this.cache.get(this.key(sql,bindings))?.rows;}
  private remove(key:string):void {
    const entry=this.cache.get(key);if(!entry)return;
    this.cache.delete(key);
    const identity=JSON.stringify(entry.bindings.at(-1) ?? null);
    for(const table of entry.tables){const bucket=this.index.get(table)?.get(identity);bucket?.delete(key);if(!bucket?.size)this.index.get(table)?.delete(identity);}
  }
  seed(sql:string,bindings:Bind[],rows:any[]):void {
    const key=this.key(sql,bindings);this.remove(key);
    const tables=[...sql.matchAll(/\b(?:FROM|JOIN) (\w+)/g)].map(match=>match[1]);
    this.cache.set(key,{sql,bindings,rows,tables});
    const identity=JSON.stringify(bindings.at(-1) ?? null);
    for(const table of tables){let tableIndex=this.index.get(table);if(!tableIndex){tableIndex=new Map();this.index.set(table,tableIndex);}let bucket=tableIndex.get(identity);if(!bucket){bucket=new Set();tableIndex.set(identity,bucket);}bucket.add(key);}
  }
  async exec<T=any>(sql:string,...bindings:Bind[]):Promise<Rows<T>> {
    const entry=this.cache.get(this.key(sql,bindings));
    if(entry)return new Rows<T>(entry.rows);
    this.invalidate(sql,bindings);return this.source.exec<T>(sql,...bindings);
  }
  private invalidate(sql:string,bindings:Bind[]):void {
    const table=sql.match(/^\s*(?:INSERT(?: OR IGNORE)? INTO|UPDATE|DELETE FROM) (\w+)/i)?.[1];
    if(!table)return;
    if(/^DELETE/.test(sql) && (!bindings.length || /WHERE (collection|parent)=\?$/.test(sql)))for(const [key,entry] of this.cache)if(entry.tables.includes(table) && (!bindings.length || entry.bindings.includes(bindings[0])))this.remove(key);
    const tableIndex=this.index.get(table),candidates=new Set<string>();
    for(const value of bindings)for(const key of tableIndex?.get(JSON.stringify(value)) || [])candidates.add(key);
    // Seeded point lookups use their final binding as the identity. Matching
    // only those buckets keeps publication linear in the number of resources.
    for(const key of candidates){const entry=this.cache.get(key);if(!entry)continue;
      const kind=/INSERT INTO resources/i.test(sql)?bindings[0]:/^UPDATE resources SET (parent|data|observed)=/.test(sql)?bindings.at(-2):undefined;
      if(table==='resources' && entry.sql.includes("kind='pr'") && kind!==undefined && kind!=='pr')continue;
      if(entry.bindings.every(value=>bindings.includes(value)))this.remove(key);
    }
    if(table==='survey_dirty' && /^DELETE/.test(sql)){if(!bindings.length)this.dirtyParents.clear();else for(const value of bindings)this.dirtyParents.delete(String(value));}
  }
  batch(statements:Statement[]):Promise<Rows<any>[]> {for(const s of statements)this.invalidate(s.sql,s.bindings || []);return this.source.batch(statements);}
  transaction<T>(apply:(store:RepositoryStore)=>Promise<T>,purpose?:WriterPurpose,operation?:WriterOperation):Promise<T>{return this.source.transaction(apply,purpose,operation);}
  health():Promise<Record<string,unknown>> {return this.source.health();}
}

// Publication writes are invisible until commit. Buffer independent row writes,
// coalesce them into set operations, and flush before any uncached read. Repeated
// identities and non-point updates force a flush to retain SQL statement order.
export class BufferedStore implements RepositoryStore {
  readonly backend='postgres';readonly transactional=true;
  private pending:Statement[]=[];private identities=new Set<string>();private payloadWrites=0;
  constructor(private source:RepositoryStore){}
  get queryCount():number|undefined{return this.source.queryCount;}
  get queuedOtherWork():number|undefined{return this.source.queuedOtherWork;}
  private identity(s:Statement):string|undefined {
    const insert=s.sql.match(/^INSERT(?: OR IGNORE)? INTO (\w+)(?:\(([^)]+)\))? VALUES\s*\(([^)]+)\)/);
    if(insert){
      const table=insert[1],fields=insert[2]?.split(',') || columns[table],keys=({resources:['kind','id'],members:['collection','id'],head_prs:['sha','number'],survey_fragments:['kind','id'],activity_landings:['number','head'],query_metrics:['operation','coverage']} as Record<string,string[]>)[table] || (table==='repair_metrics'?[]:[fields[0]]);
      if(!/^\?(?:,\?)*$/.test(insert[3]) || insert[3].split(',').length!==(s.bindings || []).length)return undefined;
      return keys.length?JSON.stringify([table,...keys.map(key=>s.bindings?.[fields.indexOf(key)])]):'metric:'+this.pending.length;
    }
    const validator=s.sql.match(/^UPDATE (jobs|collections) SET etag=NULL WHERE key IN \(\?,\?\)$/);
    if(validator)return JSON.stringify([validator[1],...(s.bindings || [])]);
    const pointDelete=s.sql.match(/^DELETE FROM (\w+) WHERE (kind=\? AND id=\?|collection=\? AND id=\?|number=\?)$/);
    if(pointDelete)return JSON.stringify([pointDelete[1],...(s.bindings || [])]);
    if(s.sql==='UPDATE resources SET observed=? WHERE kind=? AND id=?' || s.sql==='UPDATE resources SET data=?,observed=? WHERE kind=? AND id=?' || s.sql==='UPDATE resources SET parent=?,data=?,updated=?,observed=?,revision=?,tombstone=? WHERE kind=? AND id=?')return JSON.stringify(['resources',...s.bindings!.slice(-2)]);
    return undefined;
  }
  async exec<T=any>(sql:string,...bindings:Bind[]):Promise<Rows<T>> {
    const statement={sql,bindings},identity=this.identity(statement);
    if(!identity || /\bRETURNING\b/.test(sql)){await this.flush();return this.source.exec<T>(sql,...bindings);}
    const validator=sql.match(/^UPDATE (jobs|collections) SET etag=NULL WHERE key IN \(\?,\?\)$/);
    const identities=validator?bindings.map(key=>JSON.stringify([validator[1],key])):[identity];
    // Validator clears carry no row bodies; retain the original payload bound
    // without shrinking every set-based group when they accompany PR writes.
    if(identities.some(key=>this.identities.has(key)) || this.payloadWrites>=200 || this.pending.length>=600)await this.flush();
    if(!validator)this.payloadWrites++;
    for(const key of identities)this.identities.add(key);this.pending.push(statement);return new Rows<T>([]);
  }
  async batch(statements:Statement[]):Promise<Rows<any>[]> {
    if(statements.some(s=>!this.identity(s) || /\bRETURNING\b/.test(s.sql))){await this.flush();return this.source.batch(statements);}
    const results:Rows<any>[]=[];for(const s of statements)results.push(await this.exec(s.sql,...(s.bindings || [])));return results;
  }
  async flush():Promise<void> {
    if(!this.pending.length)return;
    const pending=this.pending;this.pending=[];this.identities.clear();this.payloadWrites=0;
    const groups=new Map<string,Statement[]>();
    for(const s of pending){const group=groups.get(s.sql) || [];group.push(s);groups.set(s.sql,group);}
    const writes:Statement[]=[];
    for(const [sql,group] of groups){
      if(group.length===1){writes.push(group[0]);continue;}
      const insert=sql.match(/^(INSERT(?: OR IGNORE)? INTO \w+(?:\([^)]+\))? VALUES\s*)(\([^)]+\))([\s\S]*)$/);
      if(insert){writes.push({sql:insert[1]+group.map(()=>insert[2]).join(',')+insert[3],bindings:group.flatMap(s=>s.bindings || [])});continue;}
      const validator=sql.match(/^UPDATE (jobs|collections) SET etag=NULL WHERE key IN \(\?,\?\)$/);
      if(validator){writes.push({sql:'UPDATE '+validator[1]+' SET etag=NULL WHERE key IN ('+group.flatMap(s=>s.bindings || []).map(()=>'?').join(',')+')',bindings:group.flatMap(s=>s.bindings || [])});continue;}
      const deletion=sql.match(/^(DELETE FROM \w+ WHERE )(.+)$/);
      if(deletion){writes.push({sql:deletion[1]+group.map(()=>'('+deletion[2]+')').join(' OR '),bindings:group.flatMap(s=>s.bindings || [])});continue;}
      if(sql==='UPDATE resources SET observed=? WHERE kind=? AND id=?'){
        writes.push({sql:'UPDATE resources AS r SET observed=v.observed FROM (VALUES '+group.map(()=>'(CAST(? AS BIGINT),CAST(? AS TEXT),CAST(? AS TEXT))').join(',')+') AS v(observed,kind,id) WHERE r.kind=v.kind AND r.id=v.id',bindings:group.flatMap(s=>s.bindings || [])});continue;
      }
      if(sql==='UPDATE resources SET data=?,observed=? WHERE kind=? AND id=?'){
        writes.push({sql:'UPDATE resources AS r SET data=v.data,observed=v.observed FROM (VALUES '+group.map(()=>'(CAST(? AS TEXT),CAST(? AS BIGINT),CAST(? AS TEXT),CAST(? AS TEXT))').join(',')+') AS v(data,observed,kind,id) WHERE r.kind=v.kind AND r.id=v.id',bindings:group.flatMap(s=>s.bindings || [])});continue;
      }
      if(sql.startsWith('UPDATE resources SET parent=')){
        const fields=['parent','data','updated','observed','revision','tombstone','kind','id'];
        writes.push({sql:'UPDATE resources AS r SET '+fields.slice(0,6).map(field=>field+'=v.'+field).join(',')+' FROM (VALUES '+group.map(()=>'(CAST(? AS TEXT),CAST(? AS TEXT),CAST(? AS TEXT),CAST(? AS BIGINT),CAST(? AS BIGINT),CAST(? AS BIGINT),CAST(? AS TEXT),CAST(? AS TEXT))').join(',')+') AS v('+fields.join(',')+') WHERE r.kind=v.kind AND r.id=v.id',bindings:group.flatMap(s=>s.bindings || [])});continue;
      }
      writes.push(...group);
    }
    await this.source.batch(writes);
  }
  transaction<T>(apply:(store:RepositoryStore)=>Promise<T>):Promise<T>{return apply(this);}
  async health():Promise<Record<string,unknown>>{await this.flush();return this.source.health();}
}
