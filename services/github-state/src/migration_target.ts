// Privileged S2 operations. Runtime application queries never use these credentials.
import { Client } from '@neondatabase/serverless';
import { Env, digest } from './protocol';
import { AdminError, REQUIRED_TABLES } from './storage';
import { columns, integerColumn, schemaName } from './repository_store';
import { Migration, Row, Cell, pageRowLimit, compatibleRows, archiveKey, chain } from './migration';

export interface TargetIdentity { identity:string; hostname:string; database:string; application_role:string; migration_role:string }
export function targetConfig(env:Env):TargetIdentity {
  let value:TargetIdentity;
  try {value=JSON.parse(env.MIGRATION_TARGET || 'null');}catch {throw new AdminError('migration_target_configuration');}
  if(!value || ['identity','hostname','database','application_role','migration_role'].some(key=>typeof (value as any)[key]!=='string') || value.application_role===value.migration_role || env.MIGRATION_DATABASE_URL===env.REPOSITORY_DATABASE_URL || !/^[A-Za-z0-9_-]{1,120}$/.test(value.identity) || !/^[a-z0-9.-]+\.neon\.tech$/.test(value.hostname) || !value.database || !value.application_role || !value.migration_role)throw new AdminError('migration_target_configuration');
  for(const [url,role] of [[env.MIGRATION_DATABASE_URL,value.migration_role],[env.REPOSITORY_DATABASE_URL,value.application_role]]) {
    try {const parsed=new URL(url!);if(!['postgres:','postgresql:'].includes(parsed.protocol) || parsed.hostname!==value.hostname || decodeURIComponent(parsed.pathname.slice(1))!==value.database || decodeURIComponent(parsed.username)!==role)throw 0;}catch {throw new AdminError('migration_target_configuration');}
  }
  return {identity:value.identity,hostname:value.hostname,database:value.database,application_role:value.application_role,migration_role:value.migration_role};
}
async function transaction<T>(env:Env,repo:string,apply:(client:Client,schema:string)=>Promise<T>,url=env.MIGRATION_DATABASE_URL):Promise<T> {
  const schema=schemaName(repo);targetConfig(env);
  const client=new Client({connectionString:url,connectionTimeoutMillis:10000,query_timeout:20000});
  client.on('error',()=>{}); // The awaited operation reports a sanitized failure.
  try {await client.connect();await client.query('BEGIN');await client.query("SET LOCAL statement_timeout='15s'; SET LOCAL lock_timeout='5s'");const value=await apply(client,schema);await client.query('COMMIT');return value;}
  catch(error) {try{await client.query('ROLLBACK');}catch{}if(error instanceof AdminError)throw error;
    const code=typeof (error as any)?.code==='string' && /^[0-9A-Z]{5}$/.test((error as any).code)?(error as any).code:'transport';
    console.log(JSON.stringify({event:'migration_target_unavailable',code}));throw new AdminError(['23502','23505','23P01','22001','54000'].includes(code)?'migration_constraint_incompatible':'migration_target_unavailable');}
  finally {let timer:ReturnType<typeof setTimeout>|undefined;try{await Promise.race([client.end().catch(()=>{}),new Promise<void>(resolve=>{timer=setTimeout(()=>{try{client.neonConfig.destroy();}catch{}resolve();},2000);})]);}finally{if(timer)clearTimeout(timer);}}
}
async function owner(client:Client,schema:string,state:Migration,allowed:string[]):Promise<string> {
  const row=(await client.query(`SELECT backend,generation,schema_version FROM ${schema}.backend_control WHERE singleton=1 FOR UPDATE`)).rows[0];
  if(!row || row.generation!==state.generation || row.schema_version!==1 || !allowed.includes(row.backend))throw new AdminError('migration_target_owner_mismatch');
  return row.backend;
}
function enrolled(env:Env,state:Migration):void {
  const actual=targetConfig(env),expected=state.cutover?.target_identity;
  if(actual.identity!==state.target || expected && Object.entries(expected).some(([key,value])=>(actual as any)[key]!==value))throw new AdminError('migration_target_identity_mismatch');
}
export async function targetOwner(env:Env,state:Migration):Promise<string> {
  enrolled(env,state);
  return transaction(env,state.repository,async(c,s)=>{
    if(!(await c.query('SELECT to_regclass($1) relation',[s+'.backend_control'])).rows[0].relation)return 'missing';
    return owner(c,s,state,['migration','postgres','abandoned']);
  });
}
export async function targetPreflight(env:Env,state:Migration):Promise<void> {
  enrolled(env,state);
  if(targetConfig(env).identity!==state.target)throw new AdminError('migration_target_identity_mismatch');
  await transaction(env,state.repository,async(c,s)=>{
    await owner(c,s,state,['migration']);
    const identity=(await c.query("SELECT current_database() database,current_user role,current_setting('server_version_num') version,pg_size_bytes(current_setting('neon.max_cluster_size',true))::text storage_limit")).rows[0],config=targetConfig(env);
    if(Math.floor(Number(identity.version)/10000)!==18 || !identity.storage_limit || BigInt(identity.storage_limit)<1099511627776n)throw new AdminError('migration_capacity_restore_unverified');
    if(identity.database!==config.database || identity.role!==config.migration_role)throw new AdminError('migration_target_identity_mismatch');
    const capacity=(await c.query(`SELECT storage_limit_bytes::text bytes,restore_verified_at::text restored FROM ${s}.capacity_statistics WHERE singleton=1`)).rows[0];
    if(!capacity || BigInt(capacity.bytes || '0')<1099511627776n || !capacity.restored || Number(capacity.restored)<Date.now()-7*86400000 || Number(capacity.restored)>Date.now()+60000)throw new AdminError('migration_capacity_restore_unverified');
    const actual=(await c.query('SELECT table_name,column_name,data_type FROM information_schema.columns WHERE table_schema=$1 ORDER BY table_name,ordinal_position',[s])).rows;
    const names=[...new Set(actual.map(r=>r.table_name))].sort();
    if(JSON.stringify(names)!==JSON.stringify([...Object.keys(columns),'backend_control','capacity_statistics','migration_imports'].sort()))throw new AdminError('migration_target_schema_mismatch');
    for(const name of REQUIRED_TABLES) {
      const fields=actual.filter(r=>r.table_name===name);
      if(JSON.stringify(fields.map(r=>r.column_name))!==JSON.stringify(['rowid',...columns[name]]) || fields.some(r=>r.data_type!==((r.column_name==='rowid' || integerColumn(name,r.column_name))?'bigint':'text')))throw new AdminError('migration_target_schema_mismatch');
      if((await c.query(`SELECT EXISTS(SELECT 1 FROM ${s}.${name}) present`)).rows[0].present)throw new AdminError('migration_target_not_empty');
    }
    if((await c.query(`SELECT EXISTS(SELECT 1 FROM ${s}.cursors) OR EXISTS(SELECT 1 FROM ${s}.migration_imports) present`)).rows[0].present)throw new AdminError('migration_target_not_empty');
    const role=(await c.query('SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=$1',[config.application_role])).rows[0];
    if(!role || role.rolsuper || role.rolcreatedb || role.rolcreaterole)throw new AdminError('migration_application_role_unsafe');
    if((await c.query("SELECT has_schema_privilege($1,$2,'CREATE') allowed",[config.application_role,s])).rows[0].allowed || (await c.query("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE (rolsuper OR rolcreatedb OR rolcreaterole OR rolname='neon_superuser') AND pg_has_role($1,oid,'MEMBER')) allowed",[config.application_role])).rows[0].allowed)throw new AdminError('migration_application_role_unsafe');
    const ownership=(await c.query("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=$2 AND pg_has_role($1,nspowner,'MEMBER')) OR EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=$2 AND pg_has_role($1,c.relowner,'MEMBER')) OR EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=$2 AND pg_has_role($1,p.proowner,'MEMBER')) allowed",[config.application_role,s])).rows[0].allowed;
    if(ownership)throw new AdminError('migration_application_role_unsafe');
    for(const table of REQUIRED_TABLES)if(!(await c.query("SELECT has_table_privilege($1,$2,'SELECT') AND has_table_privilege($1,$2,'INSERT') AND has_table_privilege($1,$2,'UPDATE') AND has_table_privilege($1,$2,'DELETE') allowed",[config.application_role,s+'.'+table])).rows[0].allowed)throw new AdminError('migration_application_grants_missing');
    if(!(await c.query("SELECT has_function_privilege($1,$2,'EXECUTE') allowed",[config.application_role,s+'.assert_writer(text)'])).rows[0].allowed)throw new AdminError('migration_application_grants_missing');
    for(const privilege of ['INSERT','DELETE','TRUNCATE'])if((await c.query('SELECT has_table_privilege($1,$2,$3) allowed',[config.application_role,s+'.backend_control',privilege])).rows[0].allowed)throw new AdminError('migration_application_role_unsafe');
    for(const column of ['backend','generation','schema_version'])if((await c.query("SELECT has_column_privilege($1,$2,$3,'UPDATE') allowed",[config.application_role,s+'.backend_control',column])).rows[0].allowed)throw new AdminError('migration_application_role_unsafe');
    if((await c.query("SELECT pg_has_role($1,$2,'MEMBER') allowed",[config.application_role,config.migration_role])).rows[0].allowed)throw new AdminError('migration_application_role_unsafe');
  });
  await transaction(env,state.repository,async(c,s)=>{
    const identity=(await c.query('SELECT current_database() database,current_user role')).rows[0],config=targetConfig(env);
    if(identity.database!==config.database || identity.role!==config.application_role)throw new AdminError('migration_application_role_unsafe');
    await c.query('SAVEPOINT role_probe');
    let denied=false;
    try {await c.query(`UPDATE ${s}.backend_control SET backend=backend WHERE singleton=1`);}
    catch(error) {if((error as any)?.code!=='42501')throw error;denied=true;}
    await c.query('ROLLBACK TO SAVEPOINT role_probe');
    if(!denied)throw new AdminError('migration_application_role_unsafe');
  },env.REPOSITORY_DATABASE_URL);
}
interface Page {version:number;table:string;index:number;before:string|null;rows:Row[];refs:string[];hash:string}
export async function archivePage(env:Env,state:Migration,name:string,index:number):Promise<Page> {
  const object=await env.ARCHIVE.get(`${archiveKey(state.repository,state.generation)}/${name}/${index}.json`);
  if(!object)throw new AdminError('migration_page_missing');
  const text=await object.text();if(await digest(text)!==object.customMetadata?.sha256)throw new AdminError('migration_page_corrupt');
  const page:Page=JSON.parse(text);
  if(page.version!==1 || page.table!==name || page.index!==index || !page.rows.length || page.rows.length>pageRowLimit(name) || !Array.isArray(page.refs) || page.refs.length>250)throw new AdminError('migration_page_corrupt');
  compatibleRows(name,state.tables[name].columns,page.rows);
  return page;
}
function manifest(state:Migration,name:string):void {
  if(!Object.hasOwn(columns,name) || JSON.stringify(state.tables[name].columns.map(c=>c.name).sort())!==JSON.stringify([...columns[name]].sort()))throw new AdminError('migration_column_manifest_mismatch');
}
function values(row:Row,name:string):Array<string|null> {
  if(row.length!==columns[name].length+1 || !/^-?\d+$/.test(row[0]))throw new AdminError('migration_cell_incompatible');
  return [row[0],...(row.slice(1) as Cell[]).map(([type,value])=>{
    if(type==='null' && value===null)return null;
    // The application schema has only TEXT and INTEGER columns. Never silently
    // coerce SQLite blobs/reals or lose an integer through JavaScript Number.
    if(type==='integer' && value!==null && /^-?\d+$/.test(value) || type==='text' && typeof value==='string')return value;
    throw new AdminError('migration_cell_incompatible');
  })];
}
export async function importStep(env:Env,state:Migration):Promise<void> {
  enrolled(env,state);
  const progress=state.cutover!.import;
  const name=Object.keys(state.tables).find(n=>!progress[n]?.complete);
  if(!name){state.phase='target_verifying';return;}
  manifest(state,name);
  const table=state.tables[name],check=progress[name] ||= {page:0,rows:0,hash:'0'.repeat(64),complete:false};
  if(check.page===table.pages) {
    if(check.rows!==table.count || check.hash!==table.hash)throw new AdminError('migration_checksum_mismatch');
    check.complete=true;return;
  }
  const page=await archivePage(env,state,name,check.page);
  if(page.before!==(check.after ?? null) || await chain(check.hash,page.rows)!==page.hash)throw new AdminError('migration_checksum_mismatch');
  await transaction(env,state.repository,async(c,s)=>{
    await owner(c,s,state,['migration']);
    const ledger=(await c.query(`SELECT hash FROM ${s}.migration_imports WHERE generation=$1 AND name=$2 AND page=$3`,[state.generation,name,check.page])).rows[0];
    if(ledger) {if(ledger.hash!==page.hash)throw new AdminError('migration_checksum_mismatch');return;}
    const params:Array<string|null>=[],tuples=page.rows.map(row=>'('+values(row,name).map(value=>{params.push(value);return '$'+params.length;}).join(',')+')');
    await c.query(`INSERT INTO ${s}.${name}(rowid,${table.columns.map(c=>c.name).join(',')}) OVERRIDING SYSTEM VALUE VALUES ${tuples.join(',')}`,params);
    await c.query(`INSERT INTO ${s}.migration_imports(generation,name,page,hash) VALUES($1,$2,$3,$4)`,[state.generation,name,check.page,page.hash]);
  });
  check.page++;check.rows+=page.rows.length;check.hash=page.hash;check.after=page.rows.at(-1)![0];
}
export async function targetVerifyStep(env:Env,state:Migration):Promise<void> {
  enrolled(env,state);
  const progress=state.cutover!.verify;
  const name=Object.keys(state.tables).find(n=>!progress[n]?.complete);
  if(!name){state.phase='preparing';return;}
  manifest(state,name);
  const table=state.tables[name],check=progress[name] ||= {page:0,rows:0,hash:'0'.repeat(64),complete:false};
  await transaction(env,state.repository,async(c,s)=>{
    await owner(c,s,state,['migration']);
    if(check.page===table.pages) {
      const count=(await c.query(`SELECT COUNT(*)::text n FROM ${s}.${name}`)).rows[0].n;
      if(count!==String(table.count) || check.hash!==table.hash || check.rows!==table.count)throw new AdminError('migration_checksum_mismatch');
      check.complete=true;return;
    }
    const page=await archivePage(env,state,name,check.page);
    if(page.before!==(check.after ?? null) || await chain(check.hash,page.rows)!==page.hash)throw new AdminError('migration_checksum_mismatch');
    const rows=(await c.query(`SELECT rowid::text,${table.columns.map(c=>c.name+'::text').join(',')} FROM ${s}.${name} t WHERE ${check.after===undefined?'TRUE':'rowid>$1::bigint'} ORDER BY t.rowid LIMIT ${page.rows.length}`,check.after===undefined?[]:[check.after])).rows;
    if(rows.length!==page.rows.length || rows.some((row,i)=>JSON.stringify([row.rowid,...table.columns.map(c=>row[c.name])])!==JSON.stringify(values(page.rows[i],name))))throw new AdminError('migration_target_data_mismatch');
    for(const key of page.refs)if(!await env.ARCHIVE.head(key))throw new AdminError('migration_blob_missing');
    check.page++;check.rows+=page.rows.length;check.hash=page.hash;check.after=page.rows.at(-1)![0];
  });
}
// Apply explicitly recorded execution-checkpoint repairs only after exact raw
// import validation. Cooldowns, attempts and job generations remain untouched.
export async function prepareTargetStep(env:Env,state:Migration):Promise<void> {
  enrolled(env,state);
  const control=state.cutover!,prefix=archiveKey(state.repository,state.generation)+'/expired-staging/';
  const listed=await env.ARCHIVE.list({prefix,startAfter:control.staging_after,limit:25});
  for(const object of listed.objects) {
    const archived=await env.ARCHIVE.get(object.key);if(!archived)throw new AdminError('migration_page_missing');
    const text=await archived.text(),hash=await digest(text),record=JSON.parse(text);
    if(hash!==archived.customMetadata?.sha256 || record.required_import_action!=='reset_pagination_preserve_cooldown_and_generation')throw new AdminError('migration_page_corrupt');
    await transaction(env,state.repository,async(c,s)=>{
      await owner(c,s,state,['migration']);
      const result=await c.query(`UPDATE ${s}.jobs SET started=NULL,cursor=NULL,page=1,nested=NULL,api=NULL,etag=NULL WHERE rowid=$1::bigint AND key=$2 AND (started=$3::bigint OR started IS NULL) RETURNING started::text,page::text,cursor,nested,api,etag`,[record.rowid,record.key,record.started]);
      if(result.rowCount!==1 || result.rows[0].page!=='1' || ['started','cursor','nested','api','etag'].some(key=>result.rows[0][key]!==null))throw new AdminError('migration_staging_reset_mismatch');
    });
    control.staging_rows=(control.staging_rows || 0)+1;control.staging_hash=await digest((control.staging_hash || '0'.repeat(64))+'\n'+hash);control.staging_after=object.key;
  }
  if(!listed.truncated) {
    if((control.staging_rows || 0)!==state.staging.expired_jobs || (control.staging_hash || '0'.repeat(64))!==state.staging.expired_hash)throw new AdminError('migration_checksum_mismatch');
    control.staging_complete=true;state.phase='switching';
  }
}
export async function activateTarget(env:Env,state:Migration):Promise<void> {
  enrolled(env,state);
  await transaction(env,state.repository,async(c,s)=>{
    if(await owner(c,s,state,['migration','postgres'])==='postgres')return;
    // Change only execution ownership, never the quota/backoff due, attempts,
    // generations or collected evidence. Expired staging is reset separately.
    await c.query(`UPDATE ${s}.jobs SET lease_id=NULL,lease_until=0`);
    for(const table of Object.keys(state.tables)) {
      // Imported rowids include signed extrema; a fresh sequence need only
      // allocate unused IDs. Negative archive IDs do not constrain its start.
      const max=(await c.query(`SELECT COALESCE(MAX(rowid) FILTER(WHERE rowid<9223372036854775807),0)::text n FROM ${s}.${table}`)).rows[0].n;
      if(BigInt(max)>=9223372036854775806n)throw new AdminError('migration_rowid_sequence_exhausted');
      // Reserve the signed maximum as an archival ID when present.
      await c.query(`ALTER SEQUENCE ${s}.${table}_rowid_seq MAXVALUE 9223372036854775806`);
      await c.query(`SELECT setval(pg_get_serial_sequence($1,'rowid'),GREATEST($2::bigint,1),$2::bigint>0)::text`,[s+'.'+table,max]);
    }
    await c.query(`UPDATE ${s}.meta SET value=$1 WHERE key='backend_owner'`,[JSON.stringify({backend:'postgres',generation:state.generation})]);
    await c.query(`UPDATE ${s}.backend_control SET backend='postgres' WHERE singleton=1`);
  });
}
export async function abandonTarget(env:Env,state:Migration):Promise<void> {
  enrolled(env,state);
  await transaction(env,state.repository,async(c,s)=>{
    const backend=await owner(c,s,state,['migration','abandoned','postgres']);
    if(backend==='postgres')throw new AdminError('migration_forward_recovery_required');
    await c.query(`UPDATE ${s}.backend_control SET backend='abandoned' WHERE singleton=1`);
  });
}
