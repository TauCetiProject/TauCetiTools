// S2 source protocol. This is deliberately separate from observational recovery
// exports: only a drained, fenced repository may produce a migration archive.
import { AdminError, REQUIRED_TABLES, userTables } from './storage';
import { digest } from './protocol';

export const migrationKey = (repo: string) => `control/${repo}/migration.json`;
export const archiveKey = (repo: string, generation: string) => `migrations/${repo}/${generation}`;
const quote = (name: string) => '"' + name.replace(/"/g, '""') + '"';
const ZERO = '0'.repeat(64);
export type Cell = [string, string | null];
export type Row = [string, ...Cell[]];
interface Checkpoint { after: string | null; rows: number; hash: string; pages: number }
interface TableState extends Checkpoint {
  columns: { name: string; type: string; pk: number; notnull: number; dflt_value: string | null }[];
  count: number; complete: boolean; verified: boolean; check: Checkpoint;
}
export interface Migration {
  checkpoint_etag?: string;
  version: 1; generation: string; repository: string; target: string;
  source: { object: string; deployment: string; schema: number };
  phase: 'freezing' | 'exporting' | 'verifying' | 'source_verified' | 'abandoned';
  created: number; updated: number; bookmark?: string; revision?: string;
  tables: Record<string, TableState>; failure: string | null;
  staging: { after: string | null; page: string; checked: number; complete: boolean; expired_jobs: number; expired_hash: string };
  blobs: { prefix: number; cursor?: string; pages: number; objects: number; hash: string; complete: boolean };
}
const fresh = (): Checkpoint => ({ after: null, rows: 0, hash: ZERO, pages: 0 });
export function generation(value: unknown): string {
  if (typeof value !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value)) throw new AdminError('invalid_generation');
  return value;
}
export function targetIdentity(value: unknown): string {
  // An opaque project/branch/database identity, never a connection string.
  if (typeof value !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,119}$/.test(value)) throw new AdminError('invalid_target_identity');
  return value;
}
export async function readMigration(bucket: R2Bucket, repo: string): Promise<Migration | null> {
  const object = await bucket.get(migrationKey(repo));
  if(!object) return null;
  const state=await object.json<Migration>();
  if(state.version!==1 || state.repository!==repo || !['freezing','exporting','verifying','source_verified','abandoned'].includes(state.phase) || !state.source?.object || !state.source.deployment || !state.tables || !state.blobs || !state.staging) throw new AdminError('migration_control_corrupt');
  generation(state.generation); targetIdentity(state.target);
  state.checkpoint_etag=object.etag;
  return state;
}
export function expectedTables(sql: SqlStorage): string[] {
  const actual = userTables(sql), expected = [...REQUIRED_TABLES].sort();
  if (JSON.stringify(actual) !== JSON.stringify(expected)) throw new AdminError('migration_table_manifest_mismatch');
  return actual;
}
export function schema(sql: SqlStorage, name: string): TableState['columns'] {
  return sql.exec<any>('PRAGMA table_info(' + quote(name) + ')').toArray().map(c => ({ name: c.name, type: c.type, pk: c.pk, notnull: c.notnull, dflt_value: c.dflt_value }));
}
export function encodeMigration(state: Migration): string {
  const {checkpoint_etag,...record}=state;
  return JSON.stringify(record);
}
export async function saveMigration(bucket: R2Bucket, state: Migration): Promise<void> {
  state.updated = Date.now();
  // The control record is the checkpoint. A failed write leaves the previous
  // checkpoint valid; deterministic page keys can safely be written again.
  const result=await bucket.put(migrationKey(state.repository), encodeMigration(state), {
    onlyIf:state.checkpoint_etag?{etagMatches:state.checkpoint_etag}:{etagDoesNotMatch:'*'},
  });
  if(!result) throw new AdminError('migration_checkpoint_conflict');
  state.checkpoint_etag=result.etag;
}
export function sourceRevision(sql: SqlStorage): string {
  const row = sql.exec<any>("SELECT value FROM meta WHERE key='revision'").toArray()[0];
  return row?.value ?? '0';
}
export async function beginExport(bucket: R2Bucket, storage: DurableObjectStorage, sql: SqlStorage, state: Migration): Promise<void> {
  const tables = expectedTables(sql);
  await storage.sync();
  const bookmark = await storage.getCurrentBookmark();
  if (!bookmark) throw new AdminError('migration_bookmark_unavailable');
  state.bookmark = bookmark;
  state.revision = sourceRevision(sql);
  for (const name of tables) state.tables[name] = {
    ...fresh(), columns: schema(sql, name), count: sql.exec<any>('SELECT COUNT(*) n FROM ' + quote(name)).one().n,
    complete: false, verified: false, check: fresh(),
  };
  state.phase = 'exporting';
  await saveMigration(bucket, state);
}
// Every value is typed. SQLite INTEGERs and rowids cross JavaScript as decimal
// strings, preserving all signed 64-bit values. JSON text is never reserialized.
export function sourceRows(sql: SqlStorage, name: string, columns: TableState['columns'], after: string | null, limit: number): Row[] {
  const selects = columns.flatMap((c, i) => {
    const col = quote(c.name);
    return [`typeof(${col}) AS t${i}`, `CASE typeof(${col}) WHEN 'blob' THEN hex(${col}) WHEN 'real' THEN printf('%!.17g',${col}) ELSE CAST(${col} AS TEXT) END AS v${i}`];
  });
  // Read lengths first so neither many large rows nor maliciously large input
  // limits allocate an unbounded result. Include one large row for progress.
  const sizes = columns.map(c => `COALESCE(length(CAST(${quote(c.name)} AS BLOB)),0)`).join('+');
  const lower=after===null?'':'rowid>CAST(? AS INTEGER)';
  const bindings=after===null?[]:[after];
  const candidates = sql.exec<any>(`SELECT CAST(rowid AS TEXT) id,${sizes} bytes FROM ${quote(name)} ${lower?'WHERE '+lower:''} ORDER BY rowid LIMIT ?`, ...bindings, limit).toArray();
  if (!candidates.length) return [];
  let bytes = 0, last = candidates[0].id;
  for (const row of candidates) {
    if (bytes && bytes + row.bytes > 512000) break;
    bytes += row.bytes; last = row.id;
  }
  return sql.exec<any>(`SELECT CAST(rowid AS TEXT) id,${selects.join(',')} FROM ${quote(name)} WHERE ${lower?lower+' AND ':''}rowid<=CAST(? AS INTEGER) ORDER BY rowid`, ...bindings, last).toArray().map(row => [row.id, ...columns.map((_, i): Cell => [row['t' + i], row['v' + i]])]);
}
async function chain(hash: string, rows: Row[]): Promise<string> {
  for (const row of rows) hash = await digest(hash + '\n' + JSON.stringify(row));
  return hash;
}
function references(rows: Row[]): string[] {
  const keys = new Set<string>();
  for (const row of rows) for (const cell of row.slice(1) as Cell[]) {
    if (cell[0] !== 'text' || !cell[1]?.startsWith('{')) continue;
    try { const value = JSON.parse(cell[1]); if (typeof value?.$r2 === 'string') keys.add(value.$r2); } catch { /* Not JSON. */ }
  }
  return [...keys].sort();
}
export async function exportStep(bucket: R2Bucket, sql: SqlStorage, state: Migration, requestedLimit: unknown): Promise<void> {
  if (state.phase !== 'exporting') return;
  const name = Object.keys(state.tables).find(n => !state.tables[n].complete);
  if (!name) { state.phase = 'verifying'; await saveMigration(bucket, state); return; }
  const table = state.tables[name];
  const limit = Number.isSafeInteger(requestedLimit) && Number(requestedLimit) > 0 ? Math.min(Number(requestedLimit), 250) : 100;
  const rows = sourceRows(sql, name, table.columns, table.after, limit);
  if (!rows.length) {
    if (table.rows !== table.count) throw new AdminError('migration_source_count_changed');
    table.complete = true;
  } else {
    const hash = await chain(table.hash, rows), refs = references(rows);
    const page = JSON.stringify({ version: 1, table: name, index: table.pages, before: table.after, rows, refs, hash });
    const sha256 = await digest(page);
    await bucket.put(`${archiveKey(state.repository, state.generation)}/${name}/${table.pages}.json`, page, { sha256, customMetadata: { kind: 'migration-page', sha256 } });
    table.after = rows.at(-1)![0]; table.rows += rows.length; table.hash = hash; table.pages++;
  }
  await saveMigration(bucket, state);
}
export async function verifyStep(bucket: R2Bucket, sql: SqlStorage, state: Migration): Promise<void> {
  if (state.phase !== 'verifying') return;
  if (sourceRevision(sql) !== state.revision) throw new AdminError('migration_revision_changed');
  expectedTables(sql);
  const name = Object.keys(state.tables).find(n => !state.tables[n].verified);
  if (!name) {
    if(!state.staging.complete) await stagingStep(bucket,sql,state);
    else await blobStep(bucket, state);
    return;
  }
  const table = state.tables[name], check = table.check;
  if (JSON.stringify(schema(sql, name)) !== JSON.stringify(table.columns)) throw new AdminError('migration_schema_changed');
  if (check.pages === table.pages) {
    if (check.rows !== table.count || check.hash !== table.hash || sql.exec<any>('SELECT COUNT(*) n FROM ' + quote(name)).one().n !== table.count) throw new AdminError('migration_checksum_mismatch');
    table.verified = true;
  } else {
    const object = await bucket.get(`${archiveKey(state.repository, state.generation)}/${name}/${check.pages}.json`);
    if (!object) throw new AdminError('migration_page_missing');
    const text = await object.text();
    if (await digest(text) !== object.customMetadata?.sha256) throw new AdminError('migration_page_corrupt');
    const page: { table: string; index: number; before: string | null; rows: Row[]; refs: string[]; hash: string } = JSON.parse(text);
    if (page.table !== name || page.index !== check.pages || page.before !== check.after || !page.rows.length || page.rows.length > 250) throw new AdminError('migration_page_corrupt');
    const source = sourceRows(sql, name, table.columns, check.after, page.rows.length);
    if (JSON.stringify(source) !== JSON.stringify(page.rows)) throw new AdminError('migration_source_mismatch');
    if (JSON.stringify(references(page.rows)) !== JSON.stringify(page.refs)) throw new AdminError('migration_reference_mismatch');
    for (const key of page.refs) if (!await bucket.head(key)) throw new AdminError('migration_blob_missing');
    check.hash = await chain(check.hash, page.rows);
    if (check.hash !== page.hash) throw new AdminError('migration_checksum_mismatch');
    check.after = page.rows.at(-1)![0]; check.rows += page.rows.length; check.pages++;
  }
  await saveMigration(bucket, state);
}
// Check implicitly addressed partial-job pages in bounded groups. The SQL
// source remains frozen, and the checkpoint advances only after every HEAD.
async function stagingStep(bucket: R2Bucket, sql: SqlStorage, state: Migration): Promise<void> {
  const check=state.staging,columns=state.tables.jobs.columns;
  const row=sourceRows(sql,'jobs',columns,check.after,1)[0];
  if(!row) {check.complete=true;await saveMigration(bucket,state);return;}
  const value=(name:string)=>(row[columns.findIndex(c=>c.name===name)+1] as Cell)[1];
  const started=value('started'),next=value('page');
  if(started!==null && next!==null) {
    if(!/^[0-9]+$/.test(started) || !/^[1-9][0-9]*$/.test(next)) throw new AdminError('migration_job_checkpoint_invalid');
    let page=BigInt(check.page);
    for(let n=0;page<BigInt(next) && n<100;n++,page++) {
      const key=`staging/${state.repository}/${encodeURIComponent(value('key')!)}/${started}/${page}`;
      if(!await bucket.head(key)) {
        // S0 already resets pagination when a partial staging page expires.
        // Preserve the exact SQL export and cooldown/generation, but record
        // that this execution checkpoint cannot resume from its old page.
        const text=JSON.stringify({rowid:row[0],key:value('key'),started,next_page:next,first_missing_page:String(page),
          reason:'staging_expired',required_import_action:'reset_pagination_preserve_cooldown_and_generation'});
        const sha256=await digest(text);
        await bucket.put(`${archiveKey(state.repository,state.generation)}/expired-staging/${row[0]}.json`,text,{sha256,customMetadata:{sha256}});
        check.expired_hash=await digest(check.expired_hash+'\n'+sha256);check.expired_jobs++;
        check.after=row[0];check.page='1';
        await saveMigration(bucket,state);return;
      }
      check.checked++;
    }
    check.page=String(page);
    if(page<BigInt(next)) {await saveMigration(bucket,state);return;}
  }
  check.after=row[0];check.page='1';
  await saveMigration(bucket,state);
}
// Inventory cursor and staging blobs as well as resources. Staging is addressed
// implicitly by (job key, started, page), not only by explicit $r2 references.
async function blobStep(bucket: R2Bucket, state: Migration): Promise<void> {
  const prefixes = ['resources/', 'cursors/', 'staging/'].map(p => p + state.repository + '/'), check = state.blobs;
  if (check.prefix === prefixes.length) {
    check.complete = true; state.phase = 'source_verified';
    await saveMigration(bucket, state);
    // Retained receipt, separate from the mutable current-generation control.
    await bucket.put(archiveKey(state.repository, state.generation) + '/receipt.json', encodeMigration(state));
    return;
  }
  const page = await bucket.list({ prefix: prefixes[check.prefix], cursor: check.cursor, limit: 100, include: ['customMetadata'] });
  const objects = page.objects.map(o => ({ key: o.key, etag: o.etag, size: o.size, expires: o.customMetadata?.expires ?? null }));
  const text = JSON.stringify(objects), sha256 = await digest(text);
  await bucket.put(`${archiveKey(state.repository, state.generation)}/blobs/${check.pages}.json`, text, { sha256, customMetadata: { sha256 } });
  check.hash = await digest(check.hash + '\n' + sha256); check.pages++; check.objects += objects.length;
  if (page.truncated) check.cursor = page.cursor;
  else { check.prefix++; delete check.cursor; }
  await saveMigration(bucket, state);
}
export function publicReceipt(state: Migration | null): unknown {
  if (!state) return { migration: null };
  // Do not return SQL cells, R2 reference keys, query arguments, or secrets.
  return { generation: state.generation, repository: state.repository, source: state.source, target: state.target,
    phase: state.phase, bookmark: state.bookmark, revision: state.revision, created: state.created, updated: state.updated, failure: state.failure,
    tables: Object.fromEntries(Object.entries(state.tables).map(([name, t]) => [name, { count: t.count, rows: t.rows, after: t.after, pages: t.pages, hash: t.hash, complete: t.complete, verified: t.verified, verified_pages: t.check.pages }])),
    blobs: state.blobs, staging:state.staging, write_owner: 'sqlite', production_cutover_supported: false };
}
