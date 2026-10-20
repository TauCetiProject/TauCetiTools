import { Env } from './protocol';
import { log } from './storage';

export interface CapacitySample { at: number; bytes: number; allowance: number | null }
export const SQLITE_ALLOWANCE = 10 * 1024 ** 3;
export function capacity(current: CapacitySample, previous?: CapacitySample, now = Date.now()): any {
  const elapsed = previous ? current.at - previous.at : 0;
  const growth = previous && elapsed >= 30000 ? (current.bytes - previous.bytes) / (elapsed / 1000) : null;
  const allowance = current.allowance && current.allowance > 0 ? current.allowance : null;
  const seconds = allowance && growth !== null && growth > 0 ? Math.max(0, allowance - current.bytes) / growth : null;
  const ratio = allowance ? current.bytes / allowance : null;
  const level = ratio !== null && ratio >= .85 || seconds !== null && seconds <= 86400 ? 'critical'
    : ratio !== null && ratio >= .7 || seconds !== null && seconds <= 7 * 86400 ? 'warning' : 'ok';
  return { ...current, allowance, backend: 'sqlite', allowance_kind: 'durable_object_hard_limit', growable: false,
    reusable_bytes: null, growth_bytes_per_second: growth, projected_exhaustion_at: seconds === null ? null : current.at + seconds * 1000,
    level, stale: now - current.at > 180000, capacity_unknown: allowance === null };
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
    return { ...counts, bounds_ms: BOUNDS, p50_ms: percentile(.5), p95_ms: percentile(.95), overflow_bucket: '>60000ms', error_rate: counts.requests ? counts.errors / counts.requests : null, unavailable_rate: counts.requests ? counts.unavailable / counts.requests : null, intervals: rows.map(([at, r]) => ({ at, ...r })), scope: 'repository_instance_since_restart' };
  }
}

export class RepairMetrics {
  private intervals = new Map<string, { at: number; event: string; lane: string; operation: string; n: number; value: number; maximum: number; latency: number[] }>();
  record(event: string, lane: string, operation: string, value: number, now = Date.now()): void {
    const at = Math.floor(now / 60000) * 60000, key = JSON.stringify([at, event, lane, operation]);
    for (const [key, row] of this.intervals) if (row.at < at - 59 * 60000) this.intervals.delete(key);
    // Dimensions are service-owned, with a hard ceiling as a final safeguard.
    if (!this.intervals.has(key) && this.intervals.size >= 10000) return;
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
    return {repair_hour:[...groups.values()],requested_latency};
  }

}

// Uses the already configured Workers observability stream. This is an alarm
// event, never proof of delivery to a human or to an external destination.
export function emitAlarm(env: Env, repository: string, observation: any): { channel: string; emitted: boolean; notified: false; error?: string } {
  try {
    log('github_state_capacity_alarm', { repository, level: observation.level, at: observation.at, capacity: observation.capacity,
      reasons: observation.reasons, operator_action: 'Follow docs/github-state-retention.md', deployment_id: env.CF_VERSION?.id });
    return { channel: 'configured_workers_observability', emitted: true, notified: false };
  } catch { return { channel: 'configured_workers_observability', emitted: false, notified: false, error: 'observability_emit_failed' }; }
}

// The quota owner retains its raw diagnostic window independently. Serving
// health uses this bounded projection rather than grouping that raw table.
export class AccountingMetrics {
  private rows = new Map<string, any>();
  private prepared:any={hour:[],recipes:[],at:null,scope:'installation_instance_since_restart'};
  record(row:any, now=Date.now()):void {
    const at=Math.floor(now/60000)*60000,key=JSON.stringify([at,row.api,row.operation,row.shape,row.batch_size]);
    for(const [key,r] of this.rows)if(r.at<at-59*60000)this.rows.delete(key);
    if(!this.rows.has(key) && this.rows.size>=10000)return;
    const r=this.rows.get(key) || {...row,at,requests:0,points:0,measured:0,unmeasured:0,bytes:0,latency_sum:0,max_latency:0,max_points:0,predicted_sum:0};
    r.requests++;r.points+=row.points || 0;r.measured+=Number(row.points!==null);r.unmeasured+=Number(row.points===null);
    r.bytes+=row.bytes;r.latency_sum+=row.latency;r.max_latency=Math.max(r.max_latency,row.latency);r.max_points=Math.max(r.max_points,row.points || 0);r.predicted_sum+=row.predicted;
    this.rows.set(key,r);
    const grouped=(recipe:boolean)=>{
      const groups=new Map<string,any>();
      for(const r of this.rows.values()) {
        const key=JSON.stringify([r.api,r.operation,...(recipe?[r.shape,r.batch_size]:[])]);
        const g=groups.get(key) || {api:r.api,operation:r.operation,...(recipe?{shape:r.shape,batch_size:r.batch_size}:{}),requests:0,points:0,measured:0,unmeasured:0,bytes:0,latency_sum:0,predicted_sum:0,max_points:0,max_latency:0};
        for(const f of ['requests','points','measured','unmeasured','bytes','latency_sum','predicted_sum'])g[f]+=r[f];
        g.max_latency=Math.max(g.max_latency,r.max_latency);g.max_points=Math.max(g.max_points,r.max_points);groups.set(key,g);
      }
      return [...groups.values()].map(g=>({...g,latency:g.latency_sum/g.requests,mean_points:g.measured?g.points/g.measured:null,predicted:g.predicted_sum/g.requests}));
    };
    this.prepared={at:now,hour:grouped(false),recipes:grouped(true),scope:'installation_instance_since_restart'};
  }
  snapshot(now=Date.now()):any { return {...this.prepared,stale:!this.prepared.at || now-this.prepared.at>180000}; }
}
