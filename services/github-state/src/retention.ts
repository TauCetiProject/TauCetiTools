// A rowid sweep bounds rows examined as well as rows deleted. An expiry WHERE
// followed by LIMIT alone can scan an entire unindexed table on a quiet store.
export const DAY = 86400000;
export const RULES = {
  expired_cursors: { table: 'cursors', clock: 'expires', age: 0 },
  expired_repair_metrics: { table: 'repair_metrics', clock: 'at', age: 7 * DAY },
  expired_changes: { table: 'changes', clock: 'at', age: 7 * DAY },
  expired_retired_objects: { table: 'retired_objects', clock: 'at', age: 30 * DAY },
  expired_deliveries: { table: 'deliveries', clock: 'received', age: 14 * DAY },
  expired_demand: { table: 'demand', clock: 'at', age: DAY },
} as const;
export type RetentionClass = keyof typeof RULES;
export const R2_CLASSES = { r2_cursors: 'cursors/', raw_webhooks: 'webhooks/', daily_archives: 'snapshots/', staging_pages: 'staging/', telemetry_intervals: 'telemetry/' } as const;
export interface Checkpoint { after: number; upper: number; started: number; completed?: number; examined: number; eligible: number; deleted: number; bytes: number; oldest_expired?: number; error?: string }
export interface Policy { mode: 'dry_run' | 'enforce'; disabled: string[]; pinned: string[]; archive_ci?: boolean }
export const DEFAULT_POLICY: Policy = { mode: 'dry_run', disabled: [], pinned: [] };
export function policy(input: any): Policy {
  if (!input || !['dry_run', 'enforce'].includes(input.mode) || !['disabled', 'pinned'].every(k => Array.isArray(input[k]) && input[k].every((n: any) => Object.hasOwn(RULES, n) || Object.hasOwn(R2_CLASSES, n)))) throw new Error('invalid_retention_policy');
  if (input.archive_ci !== undefined && typeof input.archive_ci !== 'boolean') throw new Error('invalid_retention_policy');
  return { mode: input.mode, disabled: [...new Set<string>(input.disabled)], pinned: [...new Set<string>(input.pinned)], archive_ci: input.archive_ci === true };
}
export interface Budget { rows: number; bytes: number; deadline: number }
// Names are fixed constants. No user-supplied SQL is interpolated here.
export function sweep(sql: SqlStorage, name: RetentionClass, previous: Checkpoint | undefined, budget: Budget, enforce: boolean, now = Date.now()): Checkpoint {
  const rule = RULES[name];
  let state: Checkpoint = previous ? { ...previous } : { after: 0, upper: 0, started: now, examined: 0, eligible: 0, deleted: 0, bytes: 0 };
  if (!previous || state.after >= state.upper) {
    state = { after: 0, upper: sql.exec<any>('SELECT COALESCE(MAX(rowid),0) n FROM ' + rule.table).one().n, started: now, completed: previous?.completed, examined: 0, eligible: 0, deleted: 0, bytes: 0 };
  }
  delete state.error;
  const cols = sql.exec<any>('PRAGMA table_info(' + rule.table + ')').toArray().map(c => c.name);
  const size = cols.map(c => 'COALESCE(length(CAST("' + c + '" AS BLOB)),0)').join('+');
  const rows = sql.exec<any>('SELECT rowid id,' + rule.clock + ' at,' + size + ' bytes FROM ' + rule.table + ' WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT ?', state.after, state.upper, Math.min(name === 'expired_cursors' ? 50 : 100, budget.rows)).toArray();
  for (const row of rows) {
    if (Date.now() >= budget.deadline || budget.rows <= 0 || row.bytes > budget.bytes) break;
    budget.rows--; budget.bytes -= row.bytes;
    // Recheck expiry at deletion. Replaying an old checkpoint cannot delete a
    // reused rowid that now belongs to a new receipt, demand or cursor.
    if (row.at < now - rule.age) {
      if (enforce) sql.exec('DELETE FROM ' + rule.table + ' WHERE rowid=? AND ' + rule.clock + '<?', row.id, now - rule.age);
      state.eligible++; state.bytes += row.bytes;
      if (enforce) state.deleted++;
      state.oldest_expired = Math.min(state.oldest_expired ?? row.at, row.at);
    }
    state.examined++; state.after = row.id;
  }
  if (!rows.length || state.after >= state.upper) { state.after = state.upper; state.completed = now; }
  return state;
}

// Explicit protection wins even if a mistakenly copied expiry is present.
export function protectedObject(key: string, metadata?: Record<string, string>): boolean {
  return /^(parked|recovery|migration|control|resources)\//.test(key) || metadata?.pinned === 'true';
}

export async function retirePrefix(bucket: R2Bucket, name: keyof typeof R2_CLASSES, now = Date.now()): Promise<any> {
  const prefix = R2_CLASSES[name], checkpointKey = 'control/retention/' + name + '.json';
  const prior = await bucket.get(checkpointKey).then(o => o?.json<any>());
  const page = await bucket.list({ prefix, cursor: prior?.cursor || undefined, limit: name === 'r2_cursors' ? 500 : 100, include: ['customMetadata'] });
  let bytes = 0, deleted = 0, examined = 0, paused = false;
  const deadline = Date.now() + 1000;
  for (const object of page.objects) {
    if (Date.now() >= deadline) { paused = true; break; }
    if (!protectedObject(object.key, object.customMetadata) && Number(object.customMetadata?.expires) < now) {
      if (bytes + object.size > 100 * 1024 ** 2) { paused = true; break; }
      await bucket.delete(object.key); bytes += object.size; deleted++;
    }
    examined++;
  }
  // If budget ran out, repeat the page. Advancing would skip unprocessed keys.
  // Deleting a row/object twice after a checkpoint failure is harmless.
  const state = { at: Date.now(), cursor: paused ? prior?.cursor || null : page.truncated ? page.cursor : null, examined, deleted, bytes, paused, completed: !paused && !page.truncated ? now : prior?.completed || null };
  await bucket.put(checkpointKey, JSON.stringify(state));
  return state;
}
