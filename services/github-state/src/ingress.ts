import { Env, EventPointer, Repository, digest, parseGitHub, stub, CI_EVENTS, repositories } from './protocol';
import { blocksWrites, holdsRetention, readMigration } from './migration';
import { classify, log } from './storage';
import { policy, protectedObject } from './retention';

// Parked ingress keeps acknowledged webhooks durable while the repository
// object cannot apply them (storage full or a planned migration). Pointers and
// pinned bodies live in private R2 without expiry metadata, so normal
// retention never removes them. Keys are deterministic: parking is idempotent.
export interface Parked extends EventPointer { original_key: string; sha256: string; parked_at: number; reason: string; replay_failures?: number; last_error?: string; retry_at?: number }
export const PARK_AFTER_ATTEMPTS = 10;
const HOLD_AFTER_FAILURES = 5;
const REPLAY_GROUP = 10;
export const receiptKey = (repo: string, delivery: string) => `receipts/${repo}/${delivery}.json`;
export const recoveryKey = (repo: string) => `control/${repo}/recovery.json`;
const pad = (n: number) => String(Math.max(0, Math.floor(n))).padStart(15, '0');
export const pointerKey = (repo: string, p: EventPointer, held = false) => `parked/${repo}/${held ? 'held' : 'pointers'}/${pad(p.received)}-${p.delivery}.json`;
export const bodyKey = (repo: string, delivery: string) => `parked/${repo}/bodies/${delivery}.json`;
export const controlKey = (repo: string) => `control/${repo}/ingress.json`;

// Legacy raw objects lack target metadata, so targeting still reads and hashes
// each original body. A group flushes once every member is waiting or done,
// independent of body-read latency; unknown results still park durably.
export function rawReceiptLookup(env: Env) {
  type Waiting = { delivery: string; resolve: (known: boolean) => void };
  type Pending = { repo: Repository; waiting: Waiting[]; timer?: ReturnType<typeof setTimeout> };
  const pending = new Map<string, Pending>();
  let grouped = false, active = 0, waiting = 0, outstanding = 0;
  const stats = { lookups: 0, requests: 0, requested_ids: 0, known_lookups: 0, failures: 0, largest_batch: 0, request_ms: 0 };
  const flush = async (batch: Pending): Promise<void> => {
    if (pending.get(batch.repo.id) !== batch) return;
    pending.delete(batch.repo.id);
    if (batch.timer !== undefined) clearTimeout(batch.timer);
    const ids = [...new Set(batch.waiting.map(v => v.delivery))], requested = new Set(ids), known = new Set<string>(), began = Date.now();
    stats.requests++; stats.requested_ids += ids.length; stats.largest_batch = Math.max(stats.largest_batch, ids.length);
    try {
      const response = await stub(env.REPOSITORIES, batch.repo.id).fetch('https://internal/storage/deliveries', { method: 'POST', body: JSON.stringify({ ids, repository_id: batch.repo.id }) });
      if (!response.ok) { await response.body?.cancel(); throw new Error('raw_receipt_lookup_unavailable'); }
      const result: any = await response.json();
      if (!Array.isArray(result?.known)) throw new Error('raw_receipt_lookup_invalid');
      for (const id of result.known) if (typeof id === 'string' && requested.has(id)) known.add(id);
    } catch { stats.failures++; /* An unavailable read never becomes an application receipt. */ }
    stats.request_ms += Date.now() - began;
    for (const item of batch.waiting) { const found = known.has(item.delivery); if (found) stats.known_lookups++; item.resolve(found); }
  };
  const flushWaiting = (): void => {
    if (grouped && active > 0 && waiting === active) for (const batch of [...pending.values()]) void flush(batch);
  };
  return {
    known(repo: Repository, delivery: string): Promise<boolean> {
      stats.lookups++;
      outstanding++;
      return new Promise(resolve => {
        const inGroup = grouped;
        if (inGroup) waiting++;
        let batch = pending.get(repo.id);
        if (!batch) { batch = { repo, waiting: [] }; pending.set(repo.id, batch); if (!inGroup) { const scheduled = batch; batch.timer = setTimeout(() => { void flush(scheduled); }, 20); } }
        batch.waiting.push({ delivery, resolve: known => { outstanding--; if (inGroup) waiting--; resolve(known); } });
        if (batch.waiting.length >= 100) void flush(batch);
        else flushWaiting();
      });
    },
    async runGroup<T>(tasks: (() => Promise<T>)[]): Promise<T[]> {
      if (grouped || outstanding || tasks.length > 100) throw new Error('raw_receipt_group_invalid');
      grouped = true; active = tasks.length;
      try {
        const results = await Promise.allSettled(tasks.map(async task => {
          try { return await task(); } finally { active--; flushWaiting(); }
        }));
        const failed = results.find((result): result is PromiseRejectedResult => result.status === 'rejected');
        if (failed) throw failed.reason;
        return results.map(result => (result as PromiseFulfilledResult<T>).value);
      } finally { grouped = false; }
    },
    summary() { return { ...stats }; },
  };
}
// Durable storage state. At the size limit Cloudflare rejects the commit after
// the handler returns and resets the object, so the object cannot record that
// it is full; whoever observes the failure records it here instead.
export const storageKey = (repo: string) => `control/${repo}/storage.json`;
// `started` is when the failing request began: a probe verified after that
// supersedes the report, and the conditional write rejects a concurrent probe.
export async function noteRepositoryFull(env: Env, repo: string, source: string, started: number): Promise<void> {
  const object = await env.ARCHIVE.get(storageKey(repo)), marker: any = object ? await object.json() : null;
  if (marker?.backend==='postgres' && marker?.state==='superseded' || marker?.state === 'full' || marker?.state === 'writable' && Number(marker.verified_at) >= started) return;
  const cooldown = marker?.state === 'writable' && Date.now() - marker.verified_at < 3600000 ? {probe_attempts:marker.probe_attempts,last_probe_at:marker.last_probe_at,next_probe_at:marker.next_probe_at} : {};
  await env.ARCHIVE.put(storageKey(repo), JSON.stringify({ state: 'full', since: started, source, ...cooldown }), {onlyIf:object ? {etagMatches:object.etag} : {etagDoesNotMatch:'*'}});
}
export function backoff(attempts: number): number { return Math.min(600, 5 * 2 ** Math.max(0, Math.min(attempts - 1, 8))); }

export async function storageState(env: Env, repo: Repository): Promise<any> {
  // This record describes the old SQLite file, not the selected remote store.
  if(env.REPOSITORY_BACKEND==='postgres')return null;
  if(['test','production'].includes(env.MIGRATION_ENVIRONMENT || '') && (await readMigration(env.ARCHIVE,repo.id))?.cutover?.target_active)return null;
  const marker:any=await env.ARCHIVE.get(storageKey(repo.id)).then(o=>o?o.json():null);
  return marker?.backend==='postgres' && marker?.state==='superseded'?null:marker;
}
export async function ingressMode(env: Env, repo: Repository): Promise<{ mode: 'apply' | 'parked'; reason?: string; at?: number }> {
  const migration=['test','production'].includes(env.MIGRATION_ENVIRONMENT || '')?await readMigration(env.ARCHIVE,repo.id):null;
  if(blocksWrites(migration)) return {mode:'parked',reason:'migration'};
  const object = await env.ARCHIVE.get(controlKey(repo.id));
  return object ? object.json() : { mode: 'apply' };
}
export async function setIngressMode(env: Env, repo: Repository, mode: unknown, reason: unknown): Promise<any> {
  if (mode !== 'apply' && mode !== 'parked') throw new Error('invalid ingress mode');
  const value = { mode, reason: typeof reason === 'string' ? reason.slice(0, 200) : undefined, at: Date.now() };
  if (mode === 'apply') await env.ARCHIVE.delete(controlKey(repo.id));
  else await env.ARCHIVE.put(controlKey(repo.id), JSON.stringify(value));
  return value;
}

// Pin and verify the body, then store its pointer. Only after both writes
// succeed may the caller acknowledge the queue message.
export async function park(env: Env, repo: Repository, pointer: EventPointer, reason: string): Promise<Parked> {
  const pinned = bodyKey(repo.id, pointer.delivery);
  const normalKey = pointerKey(repo.id,pointer), heldKey = pointerKey(repo.id,pointer,true);
  const [normal,held] = await Promise.all([env.ARCHIVE.get(normalKey),env.ARCHIVE.get(heldKey)]);
  const previous = held || normal;
  const retryState = previous ? await previous.json<Parked>() : undefined;
  // Re-pinning identical bytes is harmless, so the common path reads the
  // original once; an already pinned copy matters only once it has expired.
  const source = await env.ARCHIVE.get(pointer.key);
  let sha256 = source ? undefined : (await env.ARCHIVE.head(pinned))?.customMetadata?.sha256;
  if (!sha256) {
    if (!source) throw new Error('event body unavailable');
    const bytes = await source.arrayBuffer();
    sha256 = await digest(bytes);
    if (source.customMetadata?.sha256 && source.customMetadata.sha256 !== sha256) throw new Error('event body digest mismatch');
    // R2 rejects the write unless the stored bytes match this digest, so a
    // successful put is the verification.
    await env.ARCHIVE.put(pinned, bytes, { sha256, customMetadata: { kind: 'parked-body', sha256, delivery: pointer.delivery } });
  }
  const parked: Parked = { ...retryState, key: pinned, delivery: pointer.delivery, event: pointer.event, repository: repo.name, received: pointer.received, original_key: pointer.key, sha256, parked_at: Date.now(), reason };
  const text = JSON.stringify(parked), key = held ? heldKey : normalKey;
  await env.ARCHIVE.put(key, text, { sha256: await digest(text), customMetadata: { kind: 'parked-pointer', delivery: pointer.delivery }, onlyIf:previous ? {etagMatches:previous.etag} : {etagDoesNotMatch:'*'} });
  return parked;
}

// Queue success cleans input immediately; cron is the crash/R2-error fallback.
// The caller has already observed the repository's committed receipt.
export async function release(env: Env, repo: Repository, pointer: EventPointer): Promise<void> {
  await env.ARCHIVE.delete(bodyKey(repo.id,pointer.delivery));
  await env.ARCHIVE.delete([pointerKey(repo.id,pointer),pointerKey(repo.id,pointer,true)]);
}

// Replay bounded concurrent groups, advancing only past visited keys. A
// pointer is removed only after a committed receipt, including across resets.
export async function drain(env: Env, repo: Repository, options: { limit?: number; held?: boolean; cursor?: string; deadline?: number } = {}): Promise<any> {
  if ((await ingressMode(env, repo)).mode === 'parked') return { replayed: 0, failed:0, remaining:true,cursor:options.cursor,stopped: 'parked' };
  if (Date.now() >= (options.deadline || Infinity)) return {replayed:0,failed:0,remaining:true,cursor:options.cursor,stopped:'deadline'};
  const prefix = `parked/${repo.id}/${options.held ? 'held' : 'pointers'}/`;
  const listingStarted=Date.now();
  const page = await env.ARCHIVE.list({ prefix, limit: Math.min(Math.max(1, options.limit || 25), 100), startAfter: options.cursor });
  const listingMs=Date.now()-listingStarted,preparingStarted=Date.now();
  let replayed = 0, failed = 0, stopped: string | undefined, last_error: string | undefined, next_retry: number | undefined;
  let superseded=0;const failedKeys=new Set<string>();const delivery_errors:{delivery:string;event:string;received:number;reason:string;outcome:'unconfirmed'|'superseded'|'retry_scheduled'|'held'}[]=[];
  const prepared=new Map<string,Parked|null>(),snapshotEtags=new Map<string,string>(),ciCommitted=new Set<string>();let batchStatus:number|string|null=null;
  // Compact pointers only: never load their webhook bodies into this batch.
  // A crash after SQL commits leaves the pointers replayable and deduplicated.
  for(let i=0;i<page.objects.length && Date.now()<(options.deadline || Infinity);i+=REPLAY_GROUP)await Promise.all(page.objects.slice(i,i+REPLAY_GROUP).map(async object=>{
    try{const snapshot=await env.ARCHIVE.get(object.key);if(snapshot)snapshotEtags.set(object.key,snapshot.etag);prepared.set(object.key,snapshot?await snapshot.json<Parked>():null);}catch{/* The ordinary per-pointer path records/retries this failure. */}
  }));
  const preparingMs=Date.now()-preparingStarted;
  const ciByDelivery=new Map<string,Parked>(),conflicts=new Set<string>();
  for(const p of prepared.values())if(p && p.repository===repo.name && CI_EVENTS.has(p.event) && /^[a-zA-Z0-9-]{1,100}$/.test(p.delivery) && Number.isSafeInteger(p.received) && p.received>=0 && (p.retry_at || 0)<=Date.now()){
    if(ciByDelivery.has(p.delivery) && ciByDelivery.get(p.delivery)!.event!==p.event)conflicts.add(p.delivery);
    else ciByDelivery.set(p.delivery,p);
  }
  const ci=[...ciByDelivery.values()].filter(p=>!conflicts.has(p.delivery));
  // Legacy SQL receipts already prove application. Resolve a bounded page
  // with one read-only request instead of entering one writer per duplicate.
  // Unknown deliveries still take the ordinary event path; this never creates
  // a SQL receipt or seeds upstream work. Preserve real retry deadlines.
  const receiptCandidates=new Set<string>(),receiptEligible=new Set<Parked>();
  for(const p of prepared.values())if(p && p.repository===repo.name && !CI_EVENTS.has(p.event) && /^[a-z_]{1,100}$/.test(p.event) && /^[a-zA-Z0-9-]{1,100}$/.test(p.delivery) && Number.isSafeInteger(p.received) && p.received>=0 && (p.retry_at || 0)<=Date.now()){receiptCandidates.add(p.delivery);receiptEligible.add(p);}
  const sqlCommitted=new Set<string>(),receiptLookupStarted=Date.now();let receiptLookupStatus:number|string|null=null;
  if(receiptCandidates.size && Date.now()<(options.deadline || Infinity))try{
    const response=await stub(env.REPOSITORIES,repo.id).fetch('https://internal/storage/deliveries',{method:'POST',body:JSON.stringify({ids:[...receiptCandidates],repository_id:repo.id})});
    receiptLookupStatus=response.status;
    if(!response.ok)await response.body?.cancel();
    const result:any=response.ok?await response.json():null;
    if(Array.isArray(result?.known))for(const id of result.known)if(typeof id==='string'&&receiptCandidates.has(id))sqlCommitted.add(id);
  }catch{receiptLookupStatus='transport_error';/* Existing per-event deduplication remains the safe fallback. */}
  const receiptLookupMs=Date.now()-receiptLookupStarted;
  const readySize=[...prepared.values()].filter(p=>p && (p.retry_at || 0)<=Date.now()).length;
  const batchStarted=Date.now();let batchLookupMs:number|undefined,batchWriteMs:number|undefined;
  if(ci.length && Date.now()<(options.deadline || Infinity))try{
    const response=await stub(env.REPOSITORIES,repo.id).fetch('https://internal/ci-receipts',{method:'POST',body:JSON.stringify({repository:repo.name,events:ci.map(({delivery,event,received})=>({delivery,event,received}))})});
    batchStatus=response.status;
    const result:any=response.ok?await response.json():null;
    if(Number.isFinite(result?.timing?.lookup_ms)&&result.timing.lookup_ms>=0)batchLookupMs=result.timing.lookup_ms;
    if(Number.isFinite(result?.timing?.write_ms)&&result.timing.write_ms>=0)batchWriteMs=result.timing.write_ms;
    if(Array.isArray(result?.committed) && ci.every(p=>result.committed.includes(p.delivery)))for(const p of ci)ciCommitted.add(p.delivery);
  }catch{batchStatus='transport_error';/* A lost batch response safely falls back to ordinary delivery deduplication. */}
  const batchMs=Date.now()-batchStarted,cleanupStarted=Date.now();
  const replay = async (object: R2Object): Promise<void> => {
    if(!prepared.has(object.key)){const snapshot=await env.ARCHIVE.get(object.key);if(snapshot)snapshotEtags.set(object.key,snapshot.etag);prepared.set(object.key,snapshot?await snapshot.json<Parked>():null);}
    const parked=prepared.get(object.key);
    if (!parked) return;
    if ((parked.retry_at || 0) > Date.now()){last_error ||= parked.last_error;next_retry=Math.min(next_retry || Infinity,parked.retry_at!);return;}
    const started = Date.now();
    let reason = 'internal';
    try {
      const knownSQL=receiptEligible.has(parked)&&sqlCommitted.has(parked.delivery);
      // A durable receipt also covers SQL receipt expiry and crashes during cleanup.
      const committed = !ciCommitted.has(parked.delivery) && !knownSQL && await env.ARCHIVE.head(receiptKey(repo.id, parked.delivery));
      if (!committed) {
        if(!ciCommitted.has(parked.delivery)&&!knownSQL) {
          const response = await stub(env.REPOSITORIES, repo.id).fetch('https://internal/event', { method: 'POST', body: JSON.stringify({ key: parked.key, delivery: parked.delivery, event: parked.event, repository: parked.repository, received: parked.received }) });
          if (!response.ok) {
            const error: any = await response.json().catch(() => ({}));
            reason = String(error.message === 'event body unavailable' ? 'event_body_unavailable' : error.error || response.status);
            if (reason === 'storage_full') await noteRepositoryFull(env, repo.id, 'replay', started);
            if (error.parkable) {stopped = reason; return;}
            throw new Error(reason);
          }
        }
        await env.ARCHIVE.put(receiptKey(repo.id, parked.delivery), JSON.stringify({ delivery: parked.delivery, committed_at: Date.now(),...(CI_EVENTS.has(parked.event)?{disposition:'not_mirrored'}:knownSQL?{source:'sql_receipt'}:{}) }),{onlyIf:{etagDoesNotMatch:'*'}});
      }
      await env.ARCHIVE.delete([parked.key,object.key]);
      replayed++;
      return;
    } catch (error) {
      if (classify(error) === 'storage_full') {
        reason = 'storage_full'; stopped = reason;
        await noteRepositoryFull(env, repo.id, 'replay', started);
        return;
      }
      log('parked_replay_failed', { repository: repo.name, delivery: parked.delivery, error: reason });
    }
    failed++;failedKeys.add(object.key);
    const detail:typeof delivery_errors[number]={delivery:parked.delivery,event:parked.event,received:parked.received,reason,outcome:'unconfirmed'};
    if(/^[a-zA-Z0-9-]{1,100}$/.test(parked.delivery) && /^[a-z_]{1,100}$/.test(parked.event) && Number.isSafeInteger(parked.received))delivery_errors.push(detail);
    const failures = (parked.replay_failures || 0) + 1;
    const updated = { ...parked, replay_failures: failures, last_error: reason, last_attempt: started, retry_at: Date.now() + backoff(failures) * 1000 };
    const hold = !options.held && failures >= HOLD_AFTER_FAILURES, target = hold ? pointerKey(repo.id, parked, true) : object.key;
    // A competing replay may already have removed this pointer or recorded a
    // newer cooldown. Never recreate it from this request's stale snapshot.
    const saved=await env.ARCHIVE.put(object.key, JSON.stringify(updated), { customMetadata: { kind: 'parked-pointer', delivery: parked.delivery },onlyIf:{etagMatches:snapshotEtags.get(object.key)!} });
    if(!saved){failed--;failedKeys.delete(object.key);superseded++;detail.outcome='superseded';return;}
    last_error=reason;next_retry=Math.min(next_retry || Infinity,updated.retry_at);detail.outcome='retry_scheduled';
    if (hold) {
      detail.outcome='unconfirmed';
      // Publish the held copy before removing its normal-lane predecessor.
      // An existing held copy retains its newer retry state.
      await env.ARCHIVE.put(target,JSON.stringify(updated),{customMetadata:{kind:'parked-pointer',delivery:parked.delivery},onlyIf:{etagDoesNotMatch:'*'}});
      await env.ARCHIVE.delete(object.key);detail.outcome='held';
    }
  };
  let visited = 0;
  for (let i = 0; i < page.objects.length && Date.now() < (options.deadline || Infinity); i += REPLAY_GROUP) {
    if (stopped) break;
    const group = page.objects.slice(i,i+REPLAY_GROUP);
    await Promise.all(group.map(object => replay(object).catch(error => { if(!failedKeys.has(object.key)){failed++;failedKeys.add(object.key);} last_error = classify(error); log('pointer_maintenance_failed', { key: object.key, reason: last_error }); })));
    // A parkable refusal did not attempt this group against a writable store.
    // Retry it from the preceding checkpoint; committed peers deduplicate.
    if(stopped)break;
    visited += group.length;
  }
  if(!stopped && visited<page.objects.length)stopped='deadline';
  return { replayed, failed, superseded, delivery_errors, timing:{listing_ms:listingMs,preparing_ms:preparingMs,receipt_lookup_ms:receiptLookupMs,batch_ms:batchMs,batch_lookup_ms:batchLookupMs,batch_write_ms:batchWriteMs,cleanup_ms:Date.now()-cleanupStarted}, page_size:page.objects.length, ready_size:readySize, batched:ciCommitted.size, batch_status:batchStatus, known_receipts:sqlCommitted.size, receipt_lookup_status:receiptLookupStatus, last_error, next_retry, cursor: visited < page.objects.length || page.truncated ? (page.objects[visited-1]?.key || options.cursor) : undefined, remaining: Boolean(stopped) || page.truncated || replayed < page.objects.length, ...(stopped ? { stopped } : {}) };
}

// Delivery diagnostics belong only to the immediate administrative reply.
function passSummary(pass:any):any {
  if(!pass || typeof pass!=='object')return pass;
  const {delivery_errors,failed_deliveries,...summary}=pass;return summary;
}

// Status is a fixed number of R2 reads. Counts are bounded observations, not
// a synchronous inventory scan; the scheduled owner refreshes them.
export async function backlog(env: Env, repo: Repository, publicStatus = false): Promise<any> {
  const object = await env.ARCHIVE.get(recoveryKey(repo.id));
  const state:any = object ? await object.json() : {};
  for(const lane of ['pointers','held'])if(state[lane+'_last_pass'])state[lane+'_last_pass']=passSummary(state[lane+'_last_pass']);
  const raw:any = await env.ARCHIVE.get('control/raw-recovery.json').then(o=>o?.json() ?? null);
  return { ...await ingressMode(env, repo), prepared_at:null, parked:{count:null}, held:{count:null}, ...state, storage: await storageState(env,repo), raw: publicStatus && raw ? {prepared_at:raw.prepared_at,last_attempt:raw.last_attempt,next_retry:raw.next_retry} : raw };
}

// Reconcile one retained raw object before allowing its expiry. This also
// recovers deliveries whose queue message reached the DLQ or was never sent.
// Unavailable data is a gap, never an application receipt.
export async function retainRaw(env: Env, object: R2Object, targets: (payload: any, event: string) => Repository[], receiptLookup: (repo: Repository, delivery: string) => Promise<boolean>): Promise<boolean> {
  const delivery = object.key.slice(object.key.lastIndexOf('/') + 1).replace(/\.json$/, '');
  if (object.customMetadata?.targets && object.customMetadata.sha256) {
    const ids:unknown = JSON.parse(object.customMetadata.targets);
    if (!Array.isArray(ids) || ids.some(id=>typeof id!=='string')) throw new Error('raw_target_metadata_invalid:' + delivery);
    let durable = ids.length > 0;
    for (const id of ids) {
      if (!repositories(env).some(repo=>repo.id===id)) {durable=false;break;}
      if (await env.ARCHIVE.head(receiptKey(id,delivery))) continue;
      const pointer = {delivery,received:Number(object.customMetadata.received)} as EventPointer;
      const pin = await env.ARCHIVE.head(bodyKey(id,delivery));
      if (pin?.customMetadata?.sha256 !== object.customMetadata.sha256 || !await env.ARCHIVE.head(pointerKey(id,pointer)) && !await env.ARCHIVE.head(pointerKey(id,pointer,true))) {durable=false;break;}
    }
    if (durable) return true;
  }
  const body = await env.ARCHIVE.get(object.key);
  if (!body) throw new Error('raw_body_unavailable:' + delivery);
  const bytes = await body.arrayBuffer(), sha256 = await digest(bytes);
  if (body.customMetadata?.sha256 && body.customMetadata.sha256 !== sha256) throw new Error('raw_body_digest_mismatch:' + delivery);
  let payload: any;
  try { payload = parseGitHub(new TextDecoder().decode(bytes)); }
  catch {
    await env.ARCHIVE.put('invalid/' + delivery + '.json', JSON.stringify({ delivery, key: object.key, sha256, reason: 'invalid_json', at: Date.now() }));
    return true;
  }
  const event = body.customMetadata?.event;
  if (!event) throw new Error('raw_event_metadata_unavailable:' + delivery);
  const repos = targets(payload, event);
  if (CI_EVENTS.has(event) || !repos.length) {
    await env.ARCHIVE.put('invalid/' + delivery + '.json', JSON.stringify({ delivery, key: object.key, sha256, reason: CI_EVENTS.has(event) ? 'not_mirrored' : 'no_target', at: Date.now() }));
    return true;
  }
  for (const repo of repos) {
    if (await env.ARCHIVE.head(receiptKey(repo.id, delivery))) continue;
    // Promote a legacy committed SQL receipt without re-parking or replay.
    // A store outage cannot prevent pinning the still-unapplied input.
    let known = false;
    try { known = await receiptLookup(repo, delivery); } catch {}
    if(known) {await env.ARCHIVE.put(receiptKey(repo.id,delivery),JSON.stringify({delivery,committed_at:Date.now(),source:'sql_receipt'}),{onlyIf:{etagDoesNotMatch:'*'}});continue;}
    const pointer = { key: object.key, delivery, event, repository: repo.name, received: Number(body.customMetadata?.received) || object.uploaded.getTime() };
    // Do not overwrite retry state or resurrect an already held item.
    if (!await env.ARCHIVE.head(bodyKey(repo.id, delivery)) || !await env.ARCHIVE.head(pointerKey(repo.id, pointer)) && !await env.ARCHIVE.head(pointerKey(repo.id, pointer, true))) await park(env, repo, pointer, 'raw_reconciliation');
  }
  return true;
}

// A dedicated bounded raw prefix pass shares the cron, so a large resource
// archive cannot delay DLQ reconciliation until the general expiry sweep.
const RAW_GROUP = 25;
const RAW_GAP_PAGE = 5;
export async function maintainRaw(env: Env, targets: (payload: any, event: string) => Repository[]): Promise<void> {
  const key = 'control/raw-recovery.json', object = await env.ARCHIVE.get(key);
  const state: any = object ? await object.json() : {};
  const began = Date.now();
  if (state.lease_until > began || state.next_retry > began) return;
  const claim = await env.ARCHIVE.put(key, JSON.stringify({ ...state, last_attempt: began, lease_until: began + 60000 }), { onlyIf: object ? { etagMatches: object.etag } : { etagDoesNotMatch: '*' } });
  if (!claim) return;
  const receipts = rawReceiptLookup(env);
  let examined=0,deleted=0,bytes=0;
  // A failed raw object lies behind the main cursor. Retry a small independent
  // keyset page before scanning more history, under the same durable lease.
  // Success establishes the existing receipt/parking proof; it never applies
  // input directly or expires the original raw body.
  const recover = async (raw: R2Object|string, gapKey: string, expired: R2Object[], snapshot?:{object:R2ObjectBody|null;gap:any}): Promise<boolean> => {
    const previous=snapshot?snapshot.object:await env.ARCHIVE.get(gapKey);
    const gap:any=snapshot?snapshot.gap:previous?await previous.json():null;
    if(gap && gap.retry_at>Date.now())return false;
    const rawKey=typeof raw==='string'?raw:raw.key;
    try {
      const source=typeof raw==='string'?await env.ARCHIVE.head(raw):raw;
      if(!source)throw new Error('raw_body_unavailable:'+rawKey);
      if(!CI_EVENTS.has(source.customMetadata?.event ?? ''))await retainRaw(env,source,targets,receipts.known);
      if(gap)await env.ARCHIVE.delete(gapKey);
      if(typeof raw!=='string')expired.push(raw);
      return true;
    }catch(error) {
      const failures=(gap?.failures || 0)+1;
      // A late failure must not resurrect a resolved gap or shorten a newer
      // retry deadline after this lease was replaced.
      await env.ARCHIVE.put(gapKey,JSON.stringify({key:rawKey,failures,last_attempt:Date.now(),retry_at:Date.now()+backoff(failures)*1000,reason:error instanceof Error?error.message.slice(0,200):classify(error)}),{onlyIf:previous?{etagMatches:previous.etag}:{etagDoesNotMatch:'*'}});
      return false;
    }
  };
  try {
    let visited=0,resolved=0,errors=0;
    const gapError=(stage:'entry'|'listing',error:unknown)=>{errors++;try{log('raw_gap_retry_failed',{stage,reason:classify(error)});}catch{}};
    try {
      const gapPage=await env.ARCHIVE.list({prefix:'control/raw-gaps/',startAfter:state.gap_cursor,limit:RAW_GAP_PAGE});
      for(const entry of gapPage.objects){
        if(Date.now()-began>=5000)break;
        try {
          const snapshot=await env.ARCHIVE.get(entry.key),gap:any=snapshot?await snapshot.json():null;
          if(gap && typeof gap.key==='string' && /^webhooks\/\d{4}-\d{2}-\d{2}\/[a-zA-Z0-9-]{1,100}\.json$/.test(gap.key) && entry.key==='control/raw-gaps/'+gap.key.slice(gap.key.lastIndexOf('/')+1) && !(gap.retry_at>Date.now())) {
            if((await receipts.runGroup([()=>recover(gap.key,entry.key,[],{object:snapshot,gap})]))[0])resolved++;
          }
        }catch(error){gapError('entry',error);}
        // Preserve an unreadable gap but do not let it starve later retries
        // or the main raw-prefix scan. It is revisited after this cursor wraps.
        visited++;state.gap_cursor=entry.key;
      }
      if(visited===gapPage.objects.length&&!gapPage.truncated)delete state.gap_cursor;
    }catch(error){gapError('listing',error);}
    state.gap_last_pass={at:Date.now(),examined:visited,resolved,errors};
    for(let pages=0;pages<20 && Date.now()-began<20000;pages++) {
      const page = await env.ARCHIVE.list({ prefix: 'webhooks/', startAfter: state.after, limit: 1000, include: ['customMetadata'] });
      let processed = 0;
      const expired:R2Object[]=[],ciGaps:string[]=[];
      // Independent deliveries are recovered in concurrent groups; the cursor
      // advances only past a group once every member has finished.
      for (let i = 0; i < page.objects.length;) {
        if (Date.now() - began >= 20000) break;
        const group: (() => Promise<void>)[] = [];
        let last = state.after;
        for (; i < page.objects.length && group.length < RAW_GROUP; i++) {
          const raw = page.objects[i];
          const gapKey = 'control/raw-gaps/' + raw.key.slice(raw.key.lastIndexOf('/') + 1);
          const expiredRaw = !protectedObject(raw.key,raw.customMetadata) && Number(raw.customMetadata?.expires)<Date.now();
          // Unmirrored CI deliveries need no recovery, so the listed metadata
          // decides without gap, receipt or body reads; any earlier gap is resolved.
          if (CI_EVENTS.has(raw.customMetadata?.event ?? '')) {
            ciGaps.push(gapKey);
            if (expiredRaw) expired.push(raw);
          } else group.push(()=>recover(raw,gapKey,expiredRaw?expired:[]).then(()=>{}));
          processed++; examined++; last = raw.key;
        }
        await receipts.runGroup(group);
        state.after = last;
      }
      if(ciGaps.length)await env.ARCHIVE.delete(ciGaps);
      // Read policies/fences once per page immediately before its batch
      // deletion. Recovery input has already been pinned or dispositioned.
      if(expired.length) {
        let frozen=false;
        for(const repo of repositories(env)) {
          if(['test','production'].includes(env.MIGRATION_ENVIRONMENT || '')) {
            const migration=await readMigration(env.ARCHIVE,repo.id);
            if(holdsRetention(migration)) {frozen=true;break;}
          }
          const control=await env.ARCHIVE.get('control/'+repo.id+'/retention-policy.json').then(o=>o?.json<any>());
          if(control) {const p=policy(control);if(p.disabled.includes('raw_webhooks') || p.pinned.includes('raw_webhooks')) {frozen=true;break;}}
        }
        if(!frozen) {await env.ARCHIVE.delete(expired.map(o=>o.key));deleted+=expired.length;bytes+=expired.reduce((n,o)=>n+o.size,0);}
      }
      if (!page.truncated && processed === page.objects.length) {state.after = undefined; state.last_completed = Date.now();break;}
    }
    state.last_error = null; state.next_retry = began + 30000;
    const gaps = await env.ARCHIVE.list({prefix:'control/raw-gaps/',limit:1000});
    state.gaps = {count:gaps.objects.length,truncated:gaps.truncated};
    state.prepared_at = Date.now();
  } catch (error) {
    state.last_error = error instanceof Error ? error.message.slice(0, 200) : classify(error);
    state.next_retry = began + 60000;
  }
  state.receipt_lookup = { at: Date.now(), ...receipts.summary() };
  const completed=await env.ARCHIVE.put(key, JSON.stringify({ ...state, last_attempt: began, lease_until: 0 }), { onlyIf: { etagMatches: claim.etag } });
  if(completed && !state.last_error) await env.ARCHIVE.put('control/retention/raw_webhooks.json',JSON.stringify({at:Date.now(),after:state.after,examined,deleted,bytes,completed:state.last_completed ?? null,recovery_owned:true}));
}

type ReplayLane = 'pointers' | 'held';
const REPLAY_CEILING_MS=15*60000;
const validPageLimit=(value:unknown):value is number=>Number.isSafeInteger(value) && Number(value)>=1 && Number(value)<=100;
function pageCeiling(state:any,lane:ReplayLane):number {
  return state[lane+'_page_ceiling_until']>Date.now() && validPageLimit(state[lane+'_page_ceiling'])?state[lane+'_page_ceiling']:100;
}
function pageLimit(state:any,lane:ReplayLane):number {
  return Math.min(validPageLimit(state[lane+'_page_limit'])?state[lane+'_page_limit']:100,pageCeiling(state,lane));
}
function reduceReplayLimit(state:any,lane:ReplayLane,size:number,observed=false):void {
  const cap=pageLimit(state,lane);
  if(observed){state[lane+'_page_ceiling']=Math.min(pageCeiling(state,lane),Math.max(1,size-1));state[lane+'_page_ceiling_until']=Date.now()+REPLAY_CEILING_MS;}
  state[lane+'_page_limit']=Math.max(1,Math.floor(Math.min(cap,size)/2));
  state[lane+'_page_limit_until']=Date.now()+REPLAY_CEILING_MS;
  state[lane+'_fast_passes']=0;
}
function learnReplayLimit(state:any,lane:ReplayLane):void {
  const pass=state[lane+'_last_pass'];
  if(!pass || pass.page_limit_adjusted || pass.stopped!=='deadline' || pass.replayed!==0 || pass.failed!==0 || pass.superseded>0 || pass.ready_size===0)return;
  // Do not penalize a lane that only received the end of a shared cron budget.
  // Legacy passes lack timing/size metadata; a committed CI batch proves that
  // they did attempt this many pointers before exhausting the budget.
  const size=validPageLimit(pass.page_size)?pass.page_size:validPageLimit(pass.batched)?50:0;
  if(size<2 || pass.budget_ms!==undefined && pass.budget_ms<10000)return;
  // Legacy CI counts are lower bounds on possibly mixed pages, so they may
  // reduce the working limit but cannot establish a sizing ceiling.
  reduceReplayLimit(state,lane,size,validPageLimit(pass.page_size) && pass.budget_ms>=10000);pass.page_limit_adjusted=true;
}
function recordReplayPass(state:any,lane:ReplayLane,pass:any,limit:number,budget:number,started:number):void {
  const duration=Date.now()-started;
  Object.assign(pass,{page_limit:limit,budget_ms:budget,duration_ms:duration});
  state[lane+'_last_pass']=passSummary(pass);
  const cap=pageLimit(state,lane);
  // Recover throughput cautiously, and only after full pages at the learned
  // cap. An explicitly smaller admin request provides no evidence for growth.
  const fast=limit===cap && pass.replayed===limit && !pass.failed && !pass.stopped && duration<10000 && budget>=10000;
  const previous=Number.isSafeInteger(state[lane+'_fast_passes'])?Math.max(0,Math.min(3,state[lane+'_fast_passes'])):0;
  const successes=budget<10000 || limit<cap?previous:fast?Math.min(4,previous+1):0;
  state[lane+'_fast_passes']=successes;
  if(successes===4 && cap<100){state[lane+'_page_limit']=Math.min(pageCeiling(state,lane),cap+Math.max(1,Math.floor(cap/4)));state[lane+'_page_limit_until'] ??= Date.now()+REPLAY_CEILING_MS;state[lane+'_fast_passes']=0;}
}
async function claimReplay(env:Env,repo:Repository,manual?:{lane:ReplayLane;limit:number},canReplay=true):Promise<{key:string;state:any;began:number;lane:ReplayLane;etag?:string;stopped?:string;next_retry?:number}> {
  const key = recoveryKey(repo.id), object = await env.ARCHIVE.get(key);
  const state: any = object ? await object.json() : {};
  const began=Date.now();
  let lane:ReplayLane=manual?.lane ?? (state.next_lane==='held'?'held':'pointers');
  if ((state.lease_until || 0)>began)return {key,state,began,lane,stopped:'leased',next_retry:state.lease_until};
  // Explicit admin work may advance a healthy owner between cron ticks. Real
  // recovery failures and each pointer's own retry deadline remain binding.
  if((state.next_retry || 0)>began && (!manual || state.last_error))return {key,state,began,lane,stopped:'recovery_backoff',next_retry:state.next_retry};
  // Persist the learned bound in the lease claim, before any potentially slow
  // I/O. A reset or replaced completion cannot discard the recovery decision.
  const availability=new Map<ReplayLane,boolean>();
  const hasInput=async(l:ReplayLane)=>{
    if(!availability.has(l)){const page=await env.ARCHIVE.list({prefix:`parked/${repo.id}/${l}/`,limit:1});availability.set(l,Boolean(page.objects.length || page.truncated));}
    return availability.get(l)!;
  };
  for(const l of ['pointers','held'] as const){
    if(!(state[l+'_page_ceiling_until']>began)){delete state[l+'_page_ceiling'];delete state[l+'_page_ceiling_until'];}
    if(state[l+'_page_limit_until']!==undefined && !(state[l+'_page_limit_until']>began)){delete state[l+'_page_limit'];delete state[l+'_page_limit_until'];state[l+'_fast_passes']=0;}
    if(canReplay)learnReplayLimit(state,l);
  }
  const old=state.replay_inflight;
  let work=false;
  try {
    if(canReplay && state.lease_until>0 && state.lease_until<=began && old && ['pointers','held'].includes(old.lane) && validPageLimit(old.limit)){
      // Only the attempted lane can provide expiry evidence. A cancelled small
      // admin probe says nothing about the larger automatic limit. Expiry is
      // uncertain evidence, so it never installs or extends a sizing ceiling.
      if((old.kind==='cron' || old.kind==='manual' && old.limit>=Math.min(50,pageLimit(state,old.lane))) && await hasInput(old.lane))reduceReplayLimit(state,old.lane,old.limit);
    }
    if(canReplay && !manual && !await hasInput(lane)){const other:ReplayLane=lane==='pointers'?'held':'pointers';if(await hasInput(other))lane=other;}
    work=canReplay && Boolean(manual || await hasInput(lane));
  } catch {work=canReplay;/* Optional lane-selection reads never postpone real replay; drain will surface an actual R2 outage. */}
  delete state.replay_inflight;delete state.lease_limits;
  const claimedAt=Date.now(),limit=Math.min(manual?.limit ?? 50,pageLimit(state,lane));
  if(work)state.replay_inflight={lane,limit,kind:manual?'manual':'cron',started_at:claimedAt};
  if(!manual && canReplay)state.next_lane=lane==='pointers'?'held':'pointers';
  const claimed = await env.ARCHIVE.put(key, JSON.stringify({ ...state, last_attempt: claimedAt, lease_until: claimedAt + 60000 }), { onlyIf: object ? { etagMatches: object.etag } : { etagDoesNotMatch: '*' } });
  return {key,state,began:claimedAt,lane,...(claimed?{etag:claimed.etag}:{stopped:'leased',next_retry:claimedAt+5000})};
}
async function prepareBacklog(env:Env,repo:Repository,state:any):Promise<void> {
  const counts:any = {};
  for (const lane of ['pointers', 'held']) {
    const prefix = `parked/${repo.id}/${lane}/`, page = await env.ARCHIVE.list({ prefix, limit: 1000 });
    counts[lane === 'pointers' ? 'parked' : 'held'] = { count: page.objects.length, truncated: page.truncated, oldest_received: page.objects.length ? Number(page.objects[0].key.slice(prefix.length, prefix.length + 15)) : null };
  }
  Object.assign(state,counts);state.prepared_at=Date.now();
}

// Admin replay participates in the same owner and keyset cursor as cron.
// Crashes leave at most a short lease; SQL/R2 receipts cover an unrecorded pass.
export async function replayIngress(env:Env,repo:Repository,options:{limit?:number;held?:boolean}={}):Promise<any> {
  if(options.limit!==undefined && (!Number.isSafeInteger(options.limit) || options.limit<1 || options.limit>100))throw new Error('invalid_replay_limit');
  const lane:ReplayLane=options.held?'held':'pointers';
  const owner=await claimReplay(env,repo,{lane,limit:options.limit ?? 25}),{key,state,began}=owner;
  if(!owner.etag)return {replayed:0,failed:0,remaining:true,stopped:owner.stopped,next_retry:owner.next_retry};
  let pass:any;
  try {
    if(!state[lane+'_cursor'])state[lane+'_pass_started_at']=Date.now();
    const limit=Math.min(options.limit ?? 25,pageLimit(state,lane)),started=Date.now(),budget=Math.max(0,began+20000-started);
    pass=await drain(env,repo,{...options,limit,cursor:state[lane+'_cursor'],deadline:began+20000});
    state[lane+'_cursor']=pass.cursor;recordReplayPass(state,lane,pass,limit,budget,started);
    if(!pass.cursor && !pass.stopped)state[lane+'_pass_completed_at']=Date.now();
    delete state.replay_inflight;
    await prepareBacklog(env,repo,state);
    state.failures=0;state.last_error=null; // Manual work must not postpone the next scheduled pass.
  }catch(error){
    state.failures=(state.failures || 0)+1;state.last_error=classify(error);
    state.next_retry=Date.now()+Math.max(60,backoff(state.failures))*1000;
    pass={replayed:0,failed:0,...pass,remaining:true,stopped:'recovery_error',last_error:state.last_error,next_retry:state.next_retry};
  }
  delete state.replay_inflight;
  const saved=await env.ARCHIVE.put(key,JSON.stringify({...state,last_attempt:began,lease_until:0}),{onlyIf:{etagMatches:owner.etag}});
  return saved?pass:{...pass,remaining:true,stopped:'lease_replaced'};
}

// One durable, renewable-per-pass owner on the existing cron. A crash leaves a
// short lease and cursor, not a new poller or an abandoned recovery obligation.
export async function maintainIngress(env: Env, repo: Repository): Promise<void> {
  const full = (await storageState(env,repo))?.state === 'full';
  const owner=await claimReplay(env,repo,undefined,!full);
  if(!owner.etag)return;
  const {key,state,began,lane}=owner,deadline=began+20000;
  try {
    // Checkpoint once between lanes, not before every page. The second claim
    // retains the first lane's progress and identifies the only active lane.
    if(!full){
      const lanes:ReplayLane[]=[lane,lane==='pointers'?'held':'pointers'];
      for(const [index,current] of lanes.entries()){
        if(Date.now()>=deadline)break;
        const limit=Math.min(50,pageLimit(state,current));
        if(index){
          state.replay_inflight={lane:current,limit,kind:'cron',started_at:Date.now()};
          const next:R2Object|null=await env.ARCHIVE.put(key,JSON.stringify({...state,last_attempt:began,lease_until:began+60000}),{onlyIf:{etagMatches:owner.etag}});
          if(!next)return;
          owner.etag=next.etag;
        }
        for(let pages=0;pages<20 && Date.now()<deadline;pages++) {
          if(!state[current+'_cursor'])state[current+'_pass_started_at']=Date.now();
          const started=Date.now(),budget=Math.max(0,deadline-started);
          const pass = await drain(env, repo, { limit, held: current === 'held', cursor: state[current + '_cursor'], deadline });
          state[current + '_cursor'] = pass.cursor;
          recordReplayPass(state,current,pass,limit,budget,started);
          if(!pass.cursor && !pass.stopped)state[current+'_pass_completed_at']=Date.now();
          if(pass.stopped || !pass.cursor) break;
        }
      }
    }
    delete state.replay_inflight;
    // Prepare bounded counts separately from the traversal cursors so newer
    // valid events remain reachable even when many old items are backing off.
    await prepareBacklog(env,repo,state);
    state.failures = 0; state.last_error = null; state.next_retry = began + 30000;
  } catch (error) {
    state.failures = (state.failures || 0) + 1; state.last_error = error instanceof Error ? error.message.slice(0, 200) : classify(error);
    state.next_retry = Date.now() + Math.max(60, backoff(state.failures)) * 1000;
  }
  delete state.replay_inflight;
  // Conditional completion cannot overwrite a replacement owner after expiry.
  await env.ARCHIVE.put(key, JSON.stringify({ ...state, last_attempt: began, lease_until: 0 }), { onlyIf: { etagMatches: owner.etag } });
}

// Queue/DLQ reconciliation: retained raw deliveries without a committed
// receipt are parked for normal replay. Dry run unless apply is set.
export async function reconcile(env: Env, repo: Repository, input: any, targets: (payload: any, event: string) => Repository[]): Promise<any> {
  if (typeof input.date !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(input.date)) throw new Error('invalid reconciliation date');
  const page = await env.ARCHIVE.list({ prefix: `webhooks/${input.date}/`, cursor: typeof input.cursor === 'string' ? input.cursor : undefined, limit: 100, include: ['customMetadata'] });
  const objects = page.objects.map(o => ({ object: o, delivery: o.key.slice(o.key.lastIndexOf('/') + 1).replace(/\.json$/, '') })).filter(o => /^[a-zA-Z0-9-]{1,100}$/.test(o.delivery));
  const response = await stub(env.REPOSITORIES, repo.id).fetch('https://internal/storage/deliveries', { method: 'POST', body: JSON.stringify({ ids: objects.map(o => o.delivery), repository_id: repo.id }) });
  if (!response.ok) throw new Error('receipt check failed');
  const known = new Set((await response.json() as any).known as string[]);
  const missing: string[] = [], ignored: string[] = [], parked: string[] = [];
  for (const { object, delivery } of objects) {
    if (known.has(delivery)) continue;
    const event = object.customMetadata?.event || '';
    const body = await env.ARCHIVE.get(object.key);
    if (!body || !targets(parseGitHub(await body.text()), event).some(r => r.id === repo.id)) { ignored.push(delivery); continue; }
    missing.push(delivery);
    if (input.apply === true) { await park(env, repo, { key: object.key, delivery, event, repository: repo.name, received: object.uploaded.getTime() }, 'reconcile'); parked.push(delivery); }
  }
  return { date: input.date, checked: objects.length, known: known.size, missing, parked, ignored: ignored.length, cursor: page.truncated ? page.cursor : null };
}
