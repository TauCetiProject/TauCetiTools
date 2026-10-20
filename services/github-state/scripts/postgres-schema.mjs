import {readFile} from 'node:fs/promises';

export async function initializeSchema(client,repository,generation,role,{standby=false}={}) {
 if(!/^[0-9]{1,50}$/.test(repository || '') || !/^[A-Za-z0-9_-]{1,80}$/.test(generation || ''))throw Error('Invalid repository or generation');
 if(role && !/^[a-z][a-z0-9_]{0,62}$/.test(role))throw Error('Invalid application role');
 const schema='github_state_'+repository;
 const existing=(await client.query('SELECT to_regclass($1) relation',[schema+'.backend_control'])).rows[0].relation;
 if(existing){const owner=(await client.query(`SELECT * FROM ${schema}.backend_control WHERE singleton=1`)).rows[0];if(owner && (owner.backend!==(standby?'migration':'postgres') || owner.generation!==generation || owner.schema_version!==1))throw Error('Existing backend ownership differs; migration cannot select a new generation');}
 const sql=(await readFile(new URL('../migrations/001_postgres.sql',import.meta.url),'utf8')).replaceAll('__SCHEMA__',schema);
 const quote=value=>'"'+value.replaceAll('"','""')+'"';
 const previous=(await client.query("SELECT c.relname,a.attname FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=$1 AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped AND a.atttypid='text'::regtype AND a.attcollation<>'\"C\"'::regcollation",[schema])).rows;
 for(const column of previous)await client.query(`ALTER TABLE ${schema}.${quote(column.relname)} ALTER COLUMN ${quote(column.attname)} TYPE TEXT COLLATE "C"`);
 await client.query(sql);
 await client.query((await readFile(new URL('../migrations/002_monitoring.sql',import.meta.url),'utf8')).replaceAll('__SCHEMA__',schema));
 await client.query(`ALTER TABLE ${schema}.resources ALTER COLUMN kind SET NOT NULL,ALTER COLUMN id SET NOT NULL; ALTER TABLE ${schema}.members ALTER COLUMN collection SET NOT NULL,ALTER COLUMN id SET NOT NULL`);
 await client.query(`INSERT INTO ${schema}.backend_control VALUES(1,$2,$1,1) ON CONFLICT DO NOTHING`,[generation,standby?'migration':'postgres']);
 const owner=(await client.query(`SELECT * FROM ${schema}.backend_control WHERE singleton=1`)).rows[0];
 if(owner.backend!==(standby?'migration':'postgres') || owner.generation!==generation || owner.schema_version!==1)throw Error('Existing backend ownership differs; migration cannot select a new generation');
 if(!standby)await client.query(`INSERT INTO ${schema}.meta(key,value) VALUES('schema_version','4'),('reconciliation_version','2'),('head_index_version','1'),('workflow_scope_version','1') ON CONFLICT DO NOTHING`);
 if(role){
  await client.query(`GRANT USAGE ON SCHEMA ${schema} TO ${role}`);
  const tables=['meta','resources','activity_loc_cache','activity_landings','collections','members','deliveries','changes','jobs','demand','cursors','retired_objects','head_prs','survey_fragments','survey_state','survey_inventory','survey_hydrated','survey_dirty','survey_hydration_failures','repair_metrics','query_metrics'];
  await client.query(`GRANT SELECT,INSERT,UPDATE,DELETE ON ${tables.map(t=>schema+'.'+t).join(',')} TO ${role}`);
  await client.query(`GRANT USAGE ON ALL SEQUENCES IN SCHEMA ${schema} TO ${role}`);
  await client.query(`GRANT SELECT ON ${schema}.backend_control,${schema}.capacity_statistics TO ${role}`);
  // FOR UPDATE requires UPDATE privilege on at least one column. The key's
  // CHECK prevents changes; backend, generation and version remain read-only.
  await client.query(`GRANT UPDATE(singleton) ON ${schema}.backend_control TO ${role}`);
  await client.query(`GRANT EXECUTE ON FUNCTION ${schema}.assert_writer(TEXT) TO ${role}`);
  await client.query(`GRANT EXECUTE ON FUNCTION ${schema}.prepare_monitoring(TEXT,BOOLEAN,BIGINT) TO ${role}`);
 }
 return schema;
}
