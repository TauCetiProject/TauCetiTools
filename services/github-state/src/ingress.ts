import { Env, EventPointer, Repository, digest, parseGitHub, stub, CI_EVENTS, repositories } from './protocol';
import { readMigration } from './migration';
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
// Durable storage state. At the size limit Cloudflare rejects the commit after
// the handler returns and resets the object, so the object cannot record that
// it is full; whoever observes the failure records it here instead.
export const storageKey = (repo: string) => `control/${repo}/storage.json`;
// `started` is when the failing request began: a probe verified after that
// supersedes the report, and the conditional write rejects a concurrent probe.
export async function noteRepositoryFull(env: Env, repo: string, source: string, started: number): Promise<void> {
  const object = await env.ARCHIVE.get(storageKey(repo)), marker: any = object ? await object.json() : null;
  if (marker?.state === 'full' || marker?.state === 'writable' && Number(marker.verified_at) >= started) return;
  const cooldown = marker?.state === 'writable' && Date.now() - marker.verified_at < 3600000 ? {probe_attempts:marker.probe_attempts,last_probe_at:marker.last_probe_at,next_probe_at:marker.next_probe_at} : {};
  await env.ARCHIVE.put(storageKey(repo), JSON.stringify({ state: 'full', since: started, source, ...cooldown }), {onlyIf:object ? {etagMatches:object.etag} : {etagDoesNotMatch:'*'}});
}
export function backoff(attempts: number): number { return Math.min(600, 5 * 2 ** Math.max(0, Math.min(attempts - 1, 8))); }

export async function storageState(env: Env, repo: Repository): Promise<any> {
  // This record describes the old SQLite file, not the selected remote store.
  if(env.REPOSITORY_BACKEND==='postgres')return null;
  return env.ARCHIVE.get(storageKey(repo.id)).then(o => o ? o.json() : null);
}
export async function ingressMode(env: Env, repo: Repository): Promise<{ mode: 'apply' | 'parked'; reason?: string; at?: number }> {
  const migration=env.MIGRATION_ENVIRONMENT==='test'?await readMigration(env.ARCHIVE,repo.id):null;
  if(migration && migration.phase!=='abandoned') return {mode:'parked',reason:'migration'};
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
  if ((await ingressMode(env, repo)).mode === 'parked') return { replayed: 0, stopped: 'parked' };
  if (Date.now() >= (options.deadline || Infinity)) return {replayed:0,remaining:true,cursor:options.cursor};
  const prefix = `parked/${repo.id}/${options.held ? 'held' : 'pointers'}/`;
  const page = await env.ARCHIVE.list({ prefix, limit: Math.min(Math.max(1, options.limit || 25), 100), startAfter: options.cursor });
  let replayed = 0, failed = 0, stopped: string | undefined, last_error: string | undefined, next_retry: number | undefined;
  const replay = async (object: R2Object): Promise<void> => {
    const parked = await env.ARCHIVE.get(object.key).then(o => o?.json<Parked>());
    if (!parked) return;
    last_error ||= parked.last_error;
    if (parked.retry_at) next_retry = Math.min(next_retry || Infinity, parked.retry_at);
    if ((parked.retry_at || 0) > Date.now()) return;
    const started = Date.now();
    let reason = 'internal';
    try {
      // A durable receipt also covers SQL receipt expiry and crashes during cleanup.
      const committed = await env.ARCHIVE.head(receiptKey(repo.id, parked.delivery));
      if (!committed) {
        const response = await stub(env.REPOSITORIES, repo.id).fetch('https://internal/event', { method: 'POST', body: JSON.stringify({ key: parked.key, delivery: parked.delivery, event: parked.event, repository: parked.repository, received: parked.received }) });
        if (!response.ok) {
          const error: any = await response.json().catch(() => ({}));
          reason = String(error.message === 'event body unavailable' ? 'event_body_unavailable' : error.error || response.status);
          if (reason === 'storage_full') await noteRepositoryFull(env, repo.id, 'replay', started);
          if (error.parkable) {stopped = reason; return;}
          throw new Error(reason);
        }
        await env.ARCHIVE.put(receiptKey(repo.id, parked.delivery), JSON.stringify({ delivery: parked.delivery, committed_at: Date.now() }));
      }
      await env.ARCHIVE.delete(parked.key);
      await env.ARCHIVE.delete(object.key);
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
    failed++; last_error = reason;
    const failures = (parked.replay_failures || 0) + 1;
    const updated = { ...parked, replay_failures: failures, last_error: reason, last_attempt: started, retry_at: Date.now() + backoff(failures) * 1000 };
    next_retry = Math.min(next_retry || Infinity, updated.retry_at);
    const hold = !options.held && failures >= HOLD_AFTER_FAILURES, target = hold ? pointerKey(repo.id, parked, true) : object.key;
    await env.ARCHIVE.put(target, JSON.stringify(updated), { customMetadata: { kind: 'parked-pointer', delivery: parked.delivery } });
    if (hold) await env.ARCHIVE.delete(object.key);
  };
  let visited = 0;
  for (let i = 0; i < page.objects.length && Date.now() < (options.deadline || Infinity); i += REPLAY_GROUP) {
    if (stopped) break;
    const group = page.objects.slice(i,i+REPLAY_GROUP);
    await Promise.all(group.map(object => replay(object).catch(error => { failed++; last_error = classify(error); log('pointer_maintenance_failed', { key: object.key, reason: last_error }); })));
    visited += group.length;
  }
  return { replayed, failed, last_error, next_retry, cursor: visited < page.objects.length || page.truncated ? (page.objects[visited-1]?.key || options.cursor) : undefined, remaining: Boolean(stopped) || page.truncated || replayed < page.objects.length, ...(stopped ? { stopped } : {}) };
}

// Status is a fixed number of R2 reads. Counts are bounded observations, not
// a synchronous inventory scan; the scheduled owner refreshes them.
export async function backlog(env: Env, repo: Repository, publicStatus = false): Promise<any> {
  const object = await env.ARCHIVE.get(recoveryKey(repo.id));
  const state:any = object ? await object.json() : {};
  const raw:any = await env.ARCHIVE.get('control/raw-recovery.json').then(o=>o?.json() ?? null);
  return { ...await ingressMode(env, repo), prepared_at:null, parked:{count:null}, held:{count:null}, ...state, storage: await storageState(env,repo), raw: publicStatus && raw ? {prepared_at:raw.prepared_at,last_attempt:raw.last_attempt,next_retry:raw.next_retry} : raw };
}

// Reconcile one retained raw object before allowing its expiry. This also
// recovers deliveries whose queue message reached the DLQ or was never sent.
// Unavailable data is a gap, never an application receipt.
export async function retainRaw(env: Env, object: R2Object, targets: (payload: any, event: string) => Repository[]): Promise<boolean> {
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
    try {
      const response = await stub(env.REPOSITORIES,repo.id).fetch('https://internal/storage/deliveries',{method:'POST',body:JSON.stringify({ids:[delivery],repository_id:repo.id})});
      if(response.ok) known = ((await response.json() as any).known || []).includes(delivery);
    } catch {}
    if(known) {await env.ARCHIVE.put(receiptKey(repo.id,delivery),JSON.stringify({delivery,committed_at:Date.now(),source:'sql_receipt'}));continue;}
    const pointer = { key: object.key, delivery, event, repository: repo.name, received: Number(body.customMetadata?.received) || object.uploaded.getTime() };
    // Do not overwrite retry state or resurrect an already held item.
    if (!await env.ARCHIVE.head(bodyKey(repo.id, delivery)) || !await env.ARCHIVE.head(pointerKey(repo.id, pointer)) && !await env.ARCHIVE.head(pointerKey(repo.id, pointer, true))) await park(env, repo, pointer, 'raw_reconciliation');
  }
  return true;
}

// A dedicated bounded raw prefix pass shares the cron, so a large resource
// archive cannot delay DLQ reconciliation until the general expiry sweep.
export async function maintainRaw(env: Env, targets: (payload: any, event: string) => Repository[]): Promise<void> {
  const key = 'control/raw-recovery.json', object = await env.ARCHIVE.get(key);
  const state: any = object ? await object.json() : {};
  const began = Date.now();
  if (state.lease_until > began || state.next_retry > began) return;
  const claim = await env.ARCHIVE.put(key, JSON.stringify({ ...state, last_attempt: began, lease_until: began + 60000 }), { onlyIf: object ? { etagMatches: object.etag } : { etagDoesNotMatch: '*' } });
  if (!claim) return;
  let examined=0,deleted=0,bytes=0;
  try {
    for(let pages=0;pages<20 && Date.now()-began<20000;pages++) {
      const page = await env.ARCHIVE.list({ prefix: 'webhooks/', startAfter: state.after, limit: 50, include: ['customMetadata'] });
      let processed = 0;
      const expired:R2Object[]=[];
      for (const raw of page.objects) {
        if (Date.now() - began >= 20000) break;
        const gapKey = 'control/raw-gaps/' + raw.key.slice(raw.key.lastIndexOf('/') + 1);
        const gap: any = await env.ARCHIVE.get(gapKey).then(o=>o?.json());
        if (!gap || gap.retry_at <= Date.now()) {
          try {
            await retainRaw(env, raw, targets);
            if(!protectedObject(raw.key,raw.customMetadata) && Number(raw.customMetadata?.expires)<Date.now())expired.push(raw);
            if (gap) await env.ARCHIVE.delete(gapKey);
          } catch (error) {
            const failures = (gap?.failures || 0) + 1;
            await env.ARCHIVE.put(gapKey, JSON.stringify({ key: raw.key, failures, last_attempt: Date.now(), retry_at: Date.now() + backoff(failures) * 1000, reason: error instanceof Error ? error.message.slice(0, 200) : classify(error) }));
          }
        }
        processed++; examined++; state.after = raw.key;
      }
      // Read policies/fences once per page immediately before its batch
      // deletion. Recovery input has already been pinned or dispositioned.
      if(expired.length) {
        let frozen=false;
        for(const repo of repositories(env)) {
          if(env.MIGRATION_ENVIRONMENT==='test') {
            const migration=await readMigration(env.ARCHIVE,repo.id);
            if(migration && migration.phase!=='abandoned') {frozen=true;break;}
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
  const completed=await env.ARCHIVE.put(key, JSON.stringify({ ...state, last_attempt: began, lease_until: 0 }), { onlyIf: { etagMatches: claim.etag } });
  if(completed && !state.last_error) await env.ARCHIVE.put('control/retention/raw_webhooks.json',JSON.stringify({at:Date.now(),after:state.after,examined,deleted,bytes,completed:state.last_completed ?? null,recovery_owned:true}));
}

// One durable, renewable-per-pass owner on the existing cron. A crash leaves a
// short lease and cursor, not a new poller or an abandoned recovery obligation.
export async function maintainIngress(env: Env, repo: Repository): Promise<void> {
  const key = recoveryKey(repo.id), object = await env.ARCHIVE.get(key);
  const state: any = object ? await object.json() : {};
  if ((state.lease_until || 0) > Date.now() || (state.next_retry || 0) > Date.now()) return;
  const began = Date.now(), deadline = began + 20000;
  const claimed = await env.ARCHIVE.put(key, JSON.stringify({ ...state, last_attempt: began, lease_until: began + 60000 }), { onlyIf: object ? { etagMatches: object.etag } : { etagDoesNotMatch: '*' } });
  if (!claimed) return;
  try {
    const lanes = state.next_lane === 'held' ? ['held','pointers'] : ['pointers','held'];
    state.next_lane = lanes[1];
    const full = (await storageState(env,repo))?.state === 'full';
    for (const lane of full ? [] : lanes) {
      for(let pages=0;pages<20 && Date.now()<deadline;pages++) {
        const pass = await drain(env, repo, { limit: 50, held: lane === 'held', cursor: state[lane + '_cursor'], deadline });
        state[lane + '_cursor'] = pass.cursor;
        state[lane + '_last_pass'] = pass;
        if(pass.stopped || !pass.cursor) break;
      }
    }
    // Prepare bounded counts separately from the traversal cursors so newer
    // valid events remain reachable even when many old items are backing off.
    const counts:any = {};
    for (const lane of ['pointers', 'held']) {
      const prefix = `parked/${repo.id}/${lane}/`, page = await env.ARCHIVE.list({ prefix, limit: 1000 });
      counts[lane === 'pointers' ? 'parked' : 'held'] = { count: page.objects.length, truncated: page.truncated, oldest_received: page.objects.length ? Number(page.objects[0].key.slice(prefix.length, prefix.length + 15)) : null };
    }
    Object.assign(state,counts);state.prepared_at = Date.now();
    state.failures = 0; state.last_error = null; state.next_retry = began + 30000;
  } catch (error) {
    state.failures = (state.failures || 0) + 1; state.last_error = error instanceof Error ? error.message.slice(0, 200) : classify(error);
    state.next_retry = Date.now() + Math.max(60, backoff(state.failures)) * 1000;
  }
  // Conditional completion cannot overwrite a replacement owner after expiry.
  await env.ARCHIVE.put(key, JSON.stringify({ ...state, last_attempt: began, lease_until: 0 }), { onlyIf: { etagMatches: claimed.etag } });
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
