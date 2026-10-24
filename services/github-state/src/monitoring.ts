import { Env } from './protocol';
import { log } from './storage';

export interface CapacitySample { at: number; bytes: number; allowance: number | null }
export const SQLITE_ALLOWANCE = 10 * 1024 ** 3;
export function capacity(current:CapacitySample,previous?:CapacitySample,now=Date.now(),prior?:{forecast_level?:string,forecast_breaches?:number,bytes?:number}):any {
  const elapsed=previous?current.at-previous.at:0;
  const growth=previous && elapsed>=3600000?(current.bytes-previous.bytes)/(elapsed/1000):null;
  const allowance=current.allowance && current.allowance>0?current.allowance:null;
  const seconds=allowance && growth!==null && growth>0?Math.max(0,allowance-current.bytes)/growth:null;
  const ratio=allowance?current.bytes/allowance:null;
  const forecast_level=seconds!==null && seconds<=86400?'critical':seconds!==null && seconds<=7*86400?'warning':'ok';
  const forecast_breaches=forecast_level==='ok' || prior?.bytes!==undefined && current.bytes<=prior.bytes?0:prior?.forecast_level===forecast_level?(prior.forecast_breaches || 0)+1:1;
  const confirmed=forecast_breaches>=3;
  const level=ratio!==null && ratio>=.85 || confirmed && forecast_level==='critical'?'critical'
    :ratio!==null && ratio>=.7 || confirmed && forecast_level==='warning'?'warning':'ok';
  return {...current,allowance,backend:'sqlite',allowance_kind:'durable_object_hard_limit',growable:false,
    reusable_bytes:null,growth_bytes_per_second:growth,projected_exhaustion_at:seconds===null?null:current.at+seconds*1000,
    baseline_at:previous?.at ?? null,forecast_level,forecast_breaches,forecast_confirmed:confirmed,
    level,stale:now-current.at>180000,capacity_unknown:allowance===null};
}

// PostgreSQL storage grows automatically; only an operator-verified effective
// database/branch allowance is a capacity threshold. Never infer a 10 GiB cap.
export function postgresCapacity(statistics:any,previous?:CapacitySample,now=Date.now(),prior?:any):any {
  const valid=(n:any)=>Number.isSafeInteger(n) && n>=0;
  if(!valid(statistics?.database_bytes) || !valid(statistics?.prepared_at))return {...prior,backend:'postgres',growable:true,allowance:null,allowance_kind:'operator_verified_database_or_branch_limit',usage_scope:'current_database_lower_bound',project_usage_bytes:null,stale:true,capacity_unknown:true};
  const limit=valid(statistics.storage_limit_bytes) && statistics.storage_limit_bytes>0?statistics.storage_limit_bytes:null;
  return {...capacity({at:statistics.prepared_at,bytes:statistics.database_bytes,allowance:limit},previous,now,prior),
    backend:'postgres',growable:true,allowance_kind:'operator_verified_database_or_branch_limit',
    reusable_bytes:null,reusable_space_reason:'dead_tuple_estimates_are_not_measured_free_space',
    usage_scope:'current_database_lower_bound',allowance_scope:'database_or_branch',project_usage_bytes:null,
    restore_verified_at:statistics.restore_verified_at ?? null};
}

// Fixed logarithmic buckets: memory and persisted cardinality do not grow with
// traffic. Quantiles are bucket upper bounds, rather than false exact values.
const BOUNDS = [1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000];
export class RequestMetrics {
  private intervals = new Map<number, { requests: number; errors: number; unavailable: number; latency: number[] }>();
  record(ms: number, error: boolean, unavailable: boolean, now = Date.now()): void {
    const minute = Math.floor(now / 60000) * 60000;
    const row = this.intervals.get(minute) ?? { requests: 0, errors: 0, unavailable: 0, latency: Array(BOUNDS.length + 1).fill(0) };
    row.requests++; row.errors += Number(error); row.unavailable += Number(unavailable);
    const bucket = BOUNDS.findIndex(b => ms <= b); row.latency[bucket < 0 ? BOUNDS.length : bucket]++;
    this.intervals.set(minute, row);
    for (const at of this.intervals.keys()) if (at < minute - 59 * 60000) this.intervals.delete(at);
  }
  snapshot(now = Date.now()): any {
    const rows = [...this.intervals].filter(([at]) => at >= now - 3600000);
    const counts = rows.reduce((a, [, r]) => ({ requests: a.requests + r.requests, errors: a.errors + r.errors, unavailable: a.unavailable + r.unavailable, latency: a.latency.map((v, i) => v + r.latency[i]) }), { requests: 0, errors: 0, unavailable: 0, latency: Array(BOUNDS.length + 1).fill(0) });
    const percentile = (p: number) => { if (!counts.requests) return null; let n = 0; for (let i = 0; i < counts.latency.length; i++) { n += counts.latency[i]; if (n >= Math.ceil(counts.requests * p)) return BOUNDS[i] ?? null; } return null; };
    return { ...counts, bounds_ms: BOUNDS, p50_ms: percentile(.5), p95_ms: percentile(.95), overflow_bucket: '>60000ms', error_rate: counts.requests ? counts.errors / counts.requests : null, unavailable_rate: counts.requests ? counts.unavailable / counts.requests : null, intervals: rows.map(([at, r]) => ({ at, ...r })), scope: 'repository_instance_since_restart', latency_measurement: 'worker_io_clock_elapsed; synchronous_CPU_time_not_measured' };
  }
}

export class RepairMetrics {
  private dropped=0;
  private intervals = new Map<string, { at: number; event: string; lane: string; operation: string; n: number; value: number; maximum: number; latency: number[] }>();
  record(event: string, lane: string, operation: string, value: number, now = Date.now()): void {
    const at = Math.floor(now / 60000) * 60000, key = JSON.stringify([at, event, lane, operation]);
    for (const [key, row] of this.intervals) if (row.at < at - 59 * 60000) this.intervals.delete(key);
    // Dimensions are service-owned, with a hard ceiling as a final safeguard.
    if (!this.intervals.has(key) && this.intervals.size >= 10000) {
      let oldest:string|undefined,at=Infinity;
      for(const [k,r] of this.intervals)if(r.at<at) {oldest=k;at=r.at;}
      if(oldest!==undefined) {this.dropped+=this.intervals.get(oldest)!.n;this.intervals.delete(oldest);}
    }
    const row = this.intervals.get(key) ?? { at, event, lane, operation, n: 0, value: 0, maximum: 0, latency: Array(BOUNDS.length + 1).fill(0) };
    row.n++; row.value += value; row.maximum = Math.max(row.maximum, value);
    if (event.endsWith('_ms')) { const b = BOUNDS.findIndex(b => value <= b); row.latency[b < 0 ? BOUNDS.length : b]++; }
    this.intervals.set(key, row);
  }
  snapshot(now = Date.now()): any[] { return [...this.intervals.values()].filter(r => r.at >= now - 3600000); }
  summary(now = Date.now()): any {
    const groups=new Map<string,any>(),latency=new Map<string,{samples:number,buckets:number[]}>();
    for(const row of this.snapshot(now)) {
      const key=JSON.stringify([row.event,row.lane,row.operation]);
      const g=groups.get(key) || {event:row.event,lane:row.lane,operation:row.operation,n:0,value:0,maximum:0};
      g.n+=row.n;g.value+=row.value;g.maximum=Math.max(g.maximum,row.maximum);groups.set(key,g);
      if(row.event==='requested_single_latency_ms') {
        const l=latency.get(row.operation) || {samples:0,buckets:Array(BOUNDS.length+1).fill(0)};
        l.samples+=row.n;l.buckets=l.buckets.map((v,i)=>v+row.latency[i]);latency.set(row.operation,l);
      }
    }
    const requested_latency=[...latency].map(([operation,l])=>{
      let seen=0,p95_ms:number|null=null;
      for(let i=0;i<l.buckets.length;i++) {seen+=l.buckets[i];if(seen>=Math.ceil(l.samples*.95)) {p95_ms=BOUNDS[i] ?? null;break;}}
      return {operation,samples:l.samples,p95_ms,method:'bucket_upper_bound',scope:'repository_instance_since_restart'};
    });
    return {repair_hour:[...groups.values()],requested_latency,dropped_samples:this.dropped};
  }

}

// Uses the already configured Workers observability stream. This is an alarm
// event, never proof of delivery to a human or to an external destination.
export function emitAlarm(env: Env, repository: string, observation: any): { channel: string; emitted: boolean; notified: false; error?: string } {
  try {
    const {branches,history,...provider}=observation.provider || {};
    log('github_state_capacity_alarm', { repository, level: observation.level, at: observation.at, capacity: observation.capacity,
      provider:observation.provider?provider:undefined,compute:observation.compute,connections:observation.connections,
      reasons: observation.reasons, operator_action: 'Follow docs/github-state-retention.md', deployment_id: env.CF_VERSION?.id });
    return { channel: 'configured_workers_observability', emitted: true, notified: false };
  } catch { return { channel: 'configured_workers_observability', emitted: false, notified: false, error: 'observability_emit_failed' }; }
}

// The quota owner retains its raw diagnostic window independently. Serving
// health uses this bounded projection rather than grouping that raw table.
export class AccountingMetrics {
  private rows=new Map<string,any>();
  private hours=new Map<string,any>();
  private recipes=new Map<string,any>();
  private minute=-1;
  private dropped=0;
  private prepared:any={hour:[],recipes:[],at:null,scope:'installation_instance_since_restart'};
  private project(row:any,aggregate=false):void {
    const delta=aggregate?row:{...row,requests:1,points:row.points || 0,measured:Number(row.points!==null),unmeasured:Number(row.points===null),latency_sum:row.latency,predicted_sum:row.predicted,max_points:row.points || 0,max_latency:row.latency};
    for(const recipe of [false,true]) {
      const groups=recipe?this.recipes:this.hours,key=JSON.stringify([row.api,row.operation,...(recipe?[row.shape,row.batch_size]:[])]);
      let g=groups.get(key);
      if(!g) {
        g={api:row.api,operation:row.operation,...(recipe?{shape:row.shape,batch_size:row.batch_size}:{}),requests:0,points_sum:0,measured:0,unmeasured:0,bytes:0,latency_sum:0,predicted_sum:0,max_points:0,max_latency:0};
        groups.set(key,g);this.prepared[recipe?'recipes':'hour'].push(g);
      }
      for(const field of ['requests','measured','unmeasured','bytes','latency_sum','predicted_sum'])g[field]+=delta[field];
      g.points_sum+=delta.points;g.points=g.measured?g.points_sum:null;
      g.max_points=Math.max(g.max_points,delta.max_points);g.max_latency=Math.max(g.max_latency,delta.max_latency);
      g.latency=g.latency_sum/g.requests;g.mean_points=g.measured?g.points_sum/g.measured:null;g.predicted=g.predicted_sum/g.requests;
    }
  }
  record(row:any,now=Date.now()):void {
    const at=Math.floor(now/60000)*60000,key=JSON.stringify([at,row.api,row.operation,row.shape,row.batch_size]);
    let rebuild=at!==this.minute;
    if(rebuild) {this.minute=at;for(const [key,r] of this.rows)if(r.at<at-59*60000)this.rows.delete(key);}
    if(!this.rows.has(key) && this.rows.size>=10000) {
      let oldest:string|undefined,oldestAt=Infinity;
      for(const [k,r] of this.rows)if(r.at<oldestAt) {oldest=k;oldestAt=r.at;}
      if(oldest!==undefined) {this.dropped+=this.rows.get(oldest).requests;this.rows.delete(oldest);rebuild=true;}
    }
    const r=this.rows.get(key) || {...row,at,requests:0,points:0,measured:0,unmeasured:0,bytes:0,latency_sum:0,max_latency:0,max_points:0,predicted_sum:0};
    r.requests++;r.points+=row.points || 0;r.measured+=Number(row.points!==null);r.unmeasured+=Number(row.points===null);
    r.bytes+=row.bytes;r.latency_sum+=row.latency;r.max_latency=Math.max(r.max_latency,row.latency);r.max_points=Math.max(r.max_points,row.points || 0);r.predicted_sum+=row.predicted;
    this.rows.set(key,r);
    if(rebuild) {
      this.hours.clear();this.recipes.clear();this.prepared={at:now,hour:[],recipes:[],scope:'installation_instance_since_restart'};
      for(const row of this.rows.values())this.project(row,true);
    }else {this.project(row);this.prepared.at=now;}
  }
  snapshot(now=Date.now()):any {return {...this.prepared,dropped_requests:this.dropped,stale:!this.prepared.at || now-this.prepared.at>180000};}
}

export async function within<T>(work:Promise<T>,ms:number):Promise<T> {
  let timer:ReturnType<typeof setTimeout>|undefined;
  try {return await Promise.race([work,new Promise<T>((_,reject)=>{timer=setTimeout(()=>reject(new Error('monitoring I/O timeout')),ms);})]);}
  finally {if(timer!==undefined)clearTimeout(timer);}
}
// A timeout bounds the caller's wait, not the R2 operation. Keep timed-out
// work in its owner's budget until it actually settles. Repository maintenance
// and transactional reads have separate owners. Stateless callers omit an owner
// to keep the budget invocation-local instead of retaining canceled request I/O.
const timedBuckets=new WeakMap<object,{buckets:WeakMap<R2Bucket,R2Bucket>,pending:number}>();
const MAX_TIMED_BUCKET_IO=16;
const BODY_READS=new Set<PropertyKey>(['text','json','arrayBuffer','blob','bytes']);
export function timedBucket(bucket:R2Bucket,owner?:object):R2Bucket {
  let budget=owner?timedBuckets.get(owner):undefined;
  if(!budget){budget={buckets:new WeakMap(),pending:0};if(owner)timedBuckets.set(owner,budget);}
  const capacity=budget,cache=budget.buckets;
  const cached=cache.get(bucket);if(cached)return cached;
  const cancelBody=async(object:any):Promise<void>=>{try{await object?.body?.cancel();}catch{/* A reader can own the stream; its pending operation retains its slot. */}};
  const tracked=async<T>(work:()=>Promise<T>,late?:(value:T)=>Promise<void>,refused?:()=>void):Promise<T>=>{
    if(capacity.pending>=MAX_TIMED_BUCKET_IO){refused?.();throw new Error('monitoring I/O busy');}
    capacity.pending++;
    let expired=false;
    const completion=Promise.resolve().then(work).then(async result=>{if(expired&&late)await late(result);return result;}).finally(()=>{capacity.pending--;});
    try{return await within(completion,1500);}catch(error){expired=true;throw error;}
  };
  const proxy=new Proxy(bucket,{get(target,key){
    const value=Reflect.get(target,key,target);
    if(typeof value!=='function')return value;
    return async(...args:any[])=>{
      const result=await tracked(()=>Promise.resolve(value.apply(target,args)),key==='get'?cancelBody:undefined);
      if(key!=='get' || !result)return result;
      return new Proxy(result,{get(object,property){
        const member=Reflect.get(object,property,object);
        if(typeof member!=='function')return member;
        if(!BODY_READS.has(property))return member.bind(object);
        return (...params:any[])=>tracked(()=>Promise.resolve(member.apply(object,params)),undefined,()=>{void cancelBody(object);});
      }});
    };
  }});
  cache.set(bucket,proxy);cache.set(proxy,proxy);return proxy;
}
