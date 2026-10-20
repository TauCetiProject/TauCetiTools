// S2 source protocol. This is deliberately separate from observational recovery
// exports: only a drained, fenced repository may produce a migration archive.
import { columns, integerColumn } from './repository_store';
import { AdminError, REQUIRED_TABLES } from './storage';
import { digest, stagingStateKey, stagedPageKey } from './protocol';

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
export interface TargetCheckpoint {page:number;rows:number;hash:string;complete:boolean;after?:string}
export function blocksWrites(state:Migration|null):boolean {return Boolean(state && !['preflighting','abandoned','replaying','complete'].includes(state.phase));}
export function blocksRepairs(state:Migration|null):boolean {return blocksWrites(state) || Boolean(state?.phase==='replaying' && !state.cutover?.replay_complete);}
export interface Migration {
  checkpoint_etag?: string;
  version: 1; generation: string; repository: string; target: string;
  source: { object: string; deployment: string; schema: number };
  phase: 'preflighting' | 'freezing' | 'exporting' | 'verifying' | 'source_verified' | 'importing' | 'target_verifying' | 'preparing' | 'switching' | 'replaying' | 'complete' | 'aborting' | 'abandoned';
  cutover?: {target_identity?:Record<string,string>; environment:'test'|'production'; lease_until:number; epoch:number; source_fenced:boolean; target_active:boolean;
    compatibility?:Record<string,{after:string|null;complete:boolean;rows?:number}>; import:Record<string,TargetCheckpoint>; verify:Record<string,TargetCheckpoint>; acceptance:string;
    staging_after?:string; staging_rows?:number; staging_hash?:string; staging_complete?:boolean;
    observation_release?:{at:number;policy:unknown}; observation_finished_at?:number; abort_reason?:string; last_failure?:string; activation_intent?:boolean; target_quarantined?:{reason:string;at:number}; shadow?:{queries:number;hash:string;verified_at:number;complete:boolean;activity_checked?:boolean;cursor_checks?:number;expired_cursor_checks?:number;max_subrequests?:number;max_step_ms?:number}; replay_complete?:number; selected_at?:number; completed_at?:number; release?:Record<string,unknown>};
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
  if(state.version!==1 || state.repository!==repo || !['preflighting','freezing','exporting','verifying','source_verified','importing','target_verifying','preparing','switching','replaying','complete','aborting','abandoned'].includes(state.phase) || !state.source?.object || !state.source.deployment || !state.tables || !state.blobs || !state.staging) throw new AdminError('migration_control_corrupt');
  generation(state.generation); targetIdentity(state.target);
  if(state.cutover && (!['test','production'].includes(state.cutover.environment) || !Number.isSafeInteger(state.cutover.epoch) || !Number.isSafeInteger(state.cutover.lease_until) || !state.cutover.import || !state.cutover.verify || typeof state.cutover.source_fenced!=='boolean' || typeof state.cutover.target_active!=='boolean' || ['replaying','complete'].includes(state.phase) && !state.cutover.target_active))throw new AdminError('migration_control_corrupt');
  state.checkpoint_etag=object.etag;
  return state;
}
const PRIMARY_KEYS:Record<string,string[]>={
  meta:['key'],resources:['kind','id'],activity_loc_cache:['number'],activity_landings:['number','head'],collections:['key'],members:['collection','id'],deliveries:['id'],changes:['revision'],jobs:['key'],demand:['key'],retired_objects:['key'],head_prs:['sha','number'],survey_fragments:['kind','id'],survey_state:['number'],survey_inventory:['number'],survey_hydrated:['number'],survey_dirty:['number'],survey_hydration_failures:['number'],repair_metrics:[],query_metrics:['operation','coverage']
};
export function expectedTables(sql: SqlStorage): string[] {
  // Use literal prefixes: SQL LIKE's underscore wildcard can accidentally
  // hide application tables such as scf_extra or sqliteXstate.
  const actual = sql.exec<any>("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").toArray()
    .map(row=>row.name as string).filter(name=>!name.startsWith('sqlite_') && !name.startsWith('_cf_'));
  const expected = [...REQUIRED_TABLES].sort();
  if (JSON.stringify(actual) !== JSON.stringify(expected)) throw new AdminError('migration_table_manifest_mismatch');
  for(const name of actual) {
    const fields=schema(sql,name);
    if(JSON.stringify(fields.map(c=>c.name).sort())!==JSON.stringify([...columns[name]].sort()))throw new AdminError('migration_column_manifest_mismatch');
    if(JSON.stringify(fields.filter(c=>c.pk).sort((a,b)=>a.pk-b.pk).map(c=>c.name))!==JSON.stringify(PRIMARY_KEYS[name]))throw new AdminError('migration_constraint_incompatible');
  }
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
// Stay below PostgreSQL's 65,535 bind parameters, with bounded SQL/JSON memory.
// Blob verification has its own smaller reference bound below.
export function pageRowLimit(name:string):number {
  if(!Object.hasOwn(columns,name))throw new AdminError('migration_table_manifest_mismatch');
  return Math.min(5000,Math.floor(60000/(columns[name].length+1)));
}
export async function chain(hash: string, rows: Row[]): Promise<string> {
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
export function compatibleRows(name:string,fields:TableState['columns'],rows:Row[]):void {
  if(JSON.stringify(fields.map(c=>c.name).sort())!==JSON.stringify([...columns[name]].sort()))throw new AdminError('migration_column_manifest_mismatch');
  for(const row of rows) {
    // Other identity indexes are ordinary B-trees. Reserve tuple overhead;
    // resources/members use exact hash exclusion and permit long full keys.
    if(!['resources','members'].includes(name) && fields.reduce((bytes,c,i)=>bytes+(c.pk && !integerColumn(name,c.name)?new TextEncoder().encode((row[i+1] as Cell)[1] || '').byteLength:0),0)>2000)throw new AdminError('migration_constraint_incompatible');
    for(let i=0;i<fields.length;i++) {
      const [type,value]=row[i+1] as Cell,field=fields[i];
      if(type==='null') {if(field.pk || field.notnull)throw new AdminError('migration_cell_incompatible');continue;}
      if(!['integer','text'].includes(type) || value===null || value.includes('\0') || integerColumn(name,field.name) && type!=='integer')throw new AdminError('migration_cell_incompatible');
    }
  }
}
// This bounded scan runs while SQLite still accepts ordinary input. Export
// rechecks every frozen page to cover changes made after a row was scanned.
export function compatibilityStep(sql:SqlStorage,state:Migration):void {
  const names=expectedTables(sql),progress=state.cutover!.compatibility ||= Object.fromEntries(names.map(n=>[n,{after:null,complete:false,rows:0}]));
  const name=names.find(n=>!progress[n]?.complete);
  if(!name) {state.phase='freezing';return;}
  const check=progress[name],fields=schema(sql,name),rows=sourceRows(sql,name,fields,check.after,pageRowLimit(name));
  compatibleRows(name,fields,rows);
  if(rows.length){check.after=rows.at(-1)![0];check.rows=(check.rows || 0)+rows.length;}else check.complete=true;
}
export async function exportStep(bucket: R2Bucket, sql: SqlStorage, state: Migration, requestedLimit: unknown): Promise<void> {
  if (state.phase !== 'exporting') return;
  const name = Object.keys(state.tables).find(n => !state.tables[n].complete);
  if (!name) { state.phase = 'verifying'; await saveMigration(bucket, state); return; }
  const table = state.tables[name];
  const limit = Number.isSafeInteger(requestedLimit) && Number(requestedLimit) > 0 ? Math.min(Number(requestedLimit), state.cutover?pageRowLimit(name):250) : 100;
  let rows = sourceRows(sql, name, table.columns, table.after, limit);
  // A wide SQL page must not turn into unbounded R2 HEAD requests. Truncate
  // before the first row exceeding the cap; the rowid checkpoint resumes it.
  const keys=new Set<string>();
  for(let index=0;index<rows.length;index++) {
    const refs=references([rows[index]]),added=refs.filter(key=>!keys.has(key));
    if(keys.size+added.length>250){rows=rows.slice(0,index);break;}
    for(const key of added)keys.add(key);
  }
  if(state.cutover)compatibleRows(name,table.columns,rows);
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
    if (page.table !== name || page.index !== check.pages || page.before !== check.after || !page.rows.length || page.rows.length > (state.cutover?pageRowLimit(name):250) || !Array.isArray(page.refs) || page.refs.length>250) throw new AdminError('migration_page_corrupt');
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
      const legacy=`staging/${state.repository}/${encodeURIComponent(value('key')!)}/${started}/${page}`;
      const checkpoint=sql.exec<any>('SELECT value FROM meta WHERE key=?',stagingStateKey(value('key')!)).toArray()[0];
      let key:string | undefined;
      try {key=stagedPageKey(checkpoint?JSON.parse(checkpoint.value):undefined,legacy,started,String(page));}catch {}
      if(!key || !await bucket.head(key)) {
        // S0 already resets pagination when a partial staging page expires.
        // Preserve the exact SQL export and cooldown/generation, but record
        // that this execution checkpoint cannot resume from its old page.
        const text=JSON.stringify({rowid:row[0],key:value('key'),started,next_page:next,first_missing_page:String(page),
          reason:'staging_expired',required_import_action:'reset_pagination_preserve_cooldown_and_generation'});
        const sha256=await digest(text);
        await bucket.put(`${archiveKey(state.repository,state.generation)}/expired-staging/${(BigInt(row[0])+9223372036854775808n).toString().padStart(20,'0')}.json`,text,{sha256,customMetadata:{sha256}});
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
  const prefixes = ['resources/', 'cursors/', 'staging/', 'staging-spills/'].map(p => p + state.repository + '/'), check = state.blobs;
  if (check.prefix === prefixes.length) {
    check.complete = true; state.phase = 'source_verified';
    await saveMigration(bucket, state);
    // Retained receipt, separate from the mutable current-generation control.
    await bucket.put(archiveKey(state.repository, state.generation) + (state.cutover?'/source-receipt.json':'/receipt.json'), encodeMigration(state));
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
// A terminal receipt is immutable. HEAD avoids repeated conditional PUTs once
// publication is known durable; create-only PUT covers concurrent recovery.
export async function terminalReceipt(bucket:R2Bucket,state:Migration,filename='receipt.json'):Promise<void> {
  const key=archiveKey(state.repository,state.generation)+'/'+filename;
  const matches=(o:R2Object|null)=>o?.customMetadata?.generation===state.generation && o.customMetadata.phase===state.phase;
  const prior=await bucket.head(key);
  if(prior) {if(!matches(prior))throw new AdminError('migration_receipt_conflict');return;}
  const written=await bucket.put(key,encodeMigration(state),{onlyIf:{etagDoesNotMatch:'*'},customMetadata:{generation:state.generation,phase:state.phase}});
  if(!written && !matches(await bucket.head(key)))throw new AdminError('migration_receipt_conflict');
}
export function holdsRetention(state:Migration|null):boolean {return Boolean(state && state.phase!=='abandoned' && !state.cutover?.observation_finished_at);}
export function publicReceipt(state: Migration | null): unknown {
  if (!state) return { migration: null };
  // Do not return SQL cells, R2 reference keys, query arguments, or secrets.
  return { generation: state.generation, repository: state.repository, source: state.source, target: state.target,
    phase: state.phase, bookmark: state.bookmark, revision: state.revision, created: state.created, updated: state.updated, failure: state.failure,
    tables: Object.fromEntries(Object.entries(state.tables).map(([name, t]) => [name, { count: t.count, rows: t.rows, after: t.after, pages: t.pages, hash: t.hash, complete: t.complete, verified: t.verified, verified_pages: t.check.pages }])),
    blobs: state.blobs, staging:state.staging, cutover:state.cutover, write_owner: state.cutover?.target_active?'postgres':'sqlite', production_cutover_supported: Boolean(state.cutover) };
}
