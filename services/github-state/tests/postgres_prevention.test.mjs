import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {postgresFixture,RepositoryState,PostgresStore,columns} from './postgres_runtime.mjs';
import {bucket} from './r2_fixture.mjs';
import {worker,ingress,retention} from './prevention_runtime.mjs';
const enabled=Boolean(process.env.TEST_POSTGRES_URL && process.env.TEST_POSTGRES_MIGRATION_URL);
const pg=(name,run)=>test(name,{skip:!enabled},run),repo={id:'100',name:'org/repo',installation:'200'};
const send=(state,path,body={},id='prevention-drill')=>state.fetch(new Request('https://internal'+path,{method:'POST',headers:{'x-request-id':id},body:JSON.stringify(body)}));
const digest=text=>createHash('sha256').update(text).digest('hex');

pg('minute PostgreSQL observations measure without quota-write grants and hourly sizes stay throttled',async()=>{
 const f=await postgresFixture();try {
  await f.store.prepareMonitoring(true);
  const first=(await f.store.health()).capacity;
  assert.ok(first.database_bytes>0);assert.equal(first.storage_limit_bytes,null,'unknown allowance stays unknown');
  assert.ok(first.observation.connections.max>0);assert.ok(first.tables.resources.total_bytes>=0);
  await assert.rejects(f.store.exec('UPDATE capacity_statistics SET storage_limit_bytes=?',1));
  await f.admin.query(`UPDATE ${f.schema}.capacity_statistics SET prepared_at=prepared_at-61000`);
  await f.store.prepareMonitoring(true);
  const second=(await f.store.health()).capacity;
  assert.ok(second.prepared_at>=first.prepared_at);assert.equal(second.tables_attempt_at,first.tables_attempt_at);
  assert.equal(second.tables_prepared_at,first.tables_prepared_at);
  const wrong=new PostgresStore(process.env.TEST_POSTGRES_URL,f.schema.slice('github_state_'.length),'obsolete');
  await assert.rejects(wrong.prepareMonitoring(true),/generation fenced/);
 }finally {await f.close();}
});

pg('prepared PostgreSQL health stays fast with a hung store and keeps diagnostics after persistence failure',async()=>{
 const f=await postgresFixture();try {
  f.env.ARCHIVE=bucket();f.env.INSTALLATIONS={idFromName(){throw Error('unexpected GitHub quota access');}};
  await f.state.storageMaintenance();
  const observation=await f.state.preparedObservation();assert.equal(observation.backend,'postgres');
  let queries=0;
  f.state.store=new Proxy(f.store,{get(target,key){if(['exec','batch','health'].includes(key))return ()=>{queries++;return new Promise(()=>{});};const value=target[key];return typeof value==='function'?value.bind(target):value;}});
  const start=performance.now(),health=await (await send(f.state,'/health')).json();
  assert.ok(performance.now()-start<250);assert.equal(queries,0);assert.equal(health.monitoring.backend,'postgres');
  f.state.store=f.store;f.state.observation=undefined;
  f.env.ARCHIVE.failures.push((operation,key)=>operation==='put' && key==='control/100/observation.json');
  await f.state.storageMaintenance();assert.equal((await f.state.preparedObservation()).persistence.stored,false);
  f.state.observation.at=Date.now()-61000;
  await f.state.storageMaintenance();assert.equal((await f.state.preparedObservation()).persistence.stored,true,'next scheduled attempt recovers');
 }finally {await f.close();}
});

pg('provider and compute alarms join prepared PostgreSQL health without blocking semantic writes',async()=>{
 const f=await postgresFixture();try {
  f.env.ARCHIVE=bucket();
  const identity={project:'project-123',branch:'br-test',endpoint:new URL(f.env.REPOSITORY_DATABASE_URL).hostname.split('.')[0].replace(/-pooler$/,'')},now=Date.now();
  f.env.NEON_MONITORING=JSON.stringify(identity);
  await f.env.ARCHIVE.put('control/neon-provider.json',JSON.stringify({...identity,at:now,capacity:{level:'warning'},compute:{state:'active'}}));
  await f.env.ARCHIVE.put('control/neon-compute.json',JSON.stringify({...identity,at:now,cpu_utilization:.95}));
  await f.state.storageMaintenance();
  let v=await f.state.preparedObservation();assert.equal(v.level,'critical');assert.ok(v.reasons.includes('compute_saturation'));assert.ok(v.reasons.includes('branch_storage_capacity'));
  await f.state.put('pr','7',{number:7,title:'input applies while metrics are critical'});
  assert.ok((await f.store.exec("SELECT data FROM resources WHERE kind='pr' AND id='7'")).one().data.includes('input applies'));
  f.env.NEON_MONITORING='{';f.state.observation.at=Date.now()-61000;
  await f.state.storageMaintenance();v=await f.state.preparedObservation();
  assert.equal(v.level,'warning');assert.equal(v.provider.stale,true);assert.equal(v.persistence.stored,true);
  let sql=0;f.state.store=new Proxy(f.store,{get(target,key){if(['exec','health'].includes(key))return ()=>{sql++;throw Error('health must use saved data');};const v=target[key];return typeof v==='function'?v.bind(target):v;}});
  const health=await (await send(f.state,'/health')).json();assert.equal(health.monitoring.level,'warning');assert.equal(sql,0);
 }finally {await f.close();}
});

pg('PostgreSQL outage and coordinator restart park acknowledged input then scheduled held replay recovers without GitHub',async t=>{
 const f=await postgresFixture();let down=false,network=0;try {
  f.env.ARCHIVE=bucket();let state=f.state;
  const wrap=()=>new Proxy(f.store,{get(target,key){
   if(key==='health')return async()=>down?{backend:'postgres',connected:false}:target.health();
   const value=target[key];return typeof value==='function'?async(...args)=>{if(down)throw Error('Repository database unavailable (08006)');return value.apply(target,args);}:value;
  }});
  state.store=wrap();
  f.env.REPOSITORIES={idFromName:id=>({toString:()=>id}),get:()=>({fetch:(url,init)=>state.fetch(new Request(url,init))})};
  f.env.INSTALLATIONS={idFromName(){network++;throw Error('unexpected GitHub access');}};
  const q={repository:repo.name,operation:'issue_comments',args:{number:7}},key='issue_comments:{"number":7}';
  await state.put('issue_comments','1',{id:'1',body:'cached',created_at:'2026-10-19T00:00:00Z'},Date.now(),false,'7');
  await state.membership(key,'1',true);
  await f.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)',key,q.operation,JSON.stringify(q.args),1,Date.now(),await state.revision(),null);
  const before=await (await send(state,'/query',q)).json();assert.equal(before.data[0].body,'cached');
  down=true;
  const cached=await (await send(state,'/query',q)).json();assert.equal(cached.data[0].body,'cached');assert.equal(cached.verified_at,before.verified_at);
  state=new RepositoryState(f.state.ctx,f.env);state.store=wrap();
  const missing=await send(state,'/query',q, 'cold-outage');const detail=await missing.json();
  assert.equal(missing.status,503);assert.equal(detail.error,'database_unavailable');assert.equal(detail.request_id,'cold-outage');assert.equal(Object.hasOwn(detail,'data'),false);
  const body={issue:{number:7},comment:{id:'1',body:'recovered',created_at:'2026-10-19T00:00:00Z',updated_at:'2026-10-20T00:00:00Z'}};
  const raw='webhooks/drill/recovery.json';await f.env.ARCHIVE.put(raw,JSON.stringify(body));
  let acknowledged=false,retries=0;
  await worker.queue({messages:[{body:{key:raw,delivery:'recover',event:'issue_comment',repository:repo.name,received:Date.now()},attempts:1,ack(){acknowledged=true;},retry(){retries++;}}]},f.env);
  assert.equal(acknowledged,true);assert.equal(retries,0);
  t.mock.timers.enable({apis:['Date'],now:Date.now()});
  for(let attempt=0;attempt<5;attempt++){t.mock.timers.tick(700000);await ingress.maintainIngress(f.env,repo);}
  assert.equal((await ingress.backlog(f.env,repo)).parked.count,1,'dependency waits preserve the normal lane without spending retries');
  // Model a previously held event from a transient service/parser failure;
  // the scheduled owner must recover both lanes after PostgreSQL returns.
  const heldRaw='webhooks/drill/held.json';await f.env.ARCHIVE.put(heldRaw,JSON.stringify({...body,comment:{...body.comment,id:'2',body:'held recovered'}}));
  const heldPointer={key:heldRaw,delivery:'held-recover',event:'issue_comment',repository:repo.name,received:Date.now()};
  const held=await ingress.park(f.env,repo,heldPointer,'previous-service-failure');
  await f.env.ARCHIVE.put(ingress.pointerKey(repo.id,heldPointer,true),JSON.stringify({...held,replay_failures:5}));
  await f.env.ARCHIVE.delete(ingress.pointerKey(repo.id,heldPointer));
  await state.storageMaintenance();t.mock.timers.tick(70000);
  await state.storageMaintenance();assert.equal((await state.preparedObservation()).level,'critical');
  down=false;t.mock.timers.tick(700000);
  await ingress.maintainIngress(f.env,repo);
  const restored=await ingress.backlog(f.env,repo);assert.equal(restored.held.count,0);assert.equal(restored.parked.count,0);assert.ok(restored.next_retry>Date.now());
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM deliveries WHERE id=?','recover')).one().n,1);
  const revision=await state.revision();await ingress.maintainIngress(f.env,repo);assert.equal(await state.revision(),revision);
  const after=await (await send(state,'/query',q)).json();assert.equal(after.data[0].body,'recovered');assert.equal(network,0);
 }finally {t.mock.timers.reset();await f.close();}
});

pg('PostgreSQL expiry bounds each pass and plateaus across retention windows without deleting referenced resources',async()=>{
 const f=await postgresFixture();try {
  let checkpoint;const day=86400000,start=Date.now()-30*day;
  await f.state.put('pr','7',{number:7,body:'quiet closed history',state:'CLOSED'},start);
  for(let interval=0;interval<12;interval++) {
   const now=start+interval*3*day;
   await f.store.batch(Array.from({length:100},(_,i)=>({sql:'INSERT INTO changes VALUES(?,?,?,?,?)',bindings:[10000+interval*100+i,now,'pr','7',0]})));
   const budget={rows:1000,bytes:4*1024**2,deadline:Date.now()+15000};
   checkpoint=await retention.sweepStore(f.store,'expired_changes',checkpoint,budget,true,now);
   assert.ok(1000-budget.rows<=1000);
   if(interval>5)assert.ok((await f.store.exec('SELECT COUNT(*) n FROM changes')).one().n<=400);
  }
  assert.ok((await f.store.exec("SELECT data FROM resources WHERE kind='pr' AND id='7'")).one().data.includes('quiet closed history'));
 }finally {await f.close();}
});

pg('verified migration archive restores every repository table, exact IDs and referenced spills; corrupt restore retries safely',async()=>{
 const source=await postgresFixture(),target=await postgresFixture();try {
  const huge='18446744073709551600',now=Date.now(),blob='resources/100/old-retained';
  await source.env.ARCHIVE.put(blob,JSON.stringify({id:huge,body:'retained archive contents'}));
  await source.store.exec('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)','issue_comments',huge,'7',JSON.stringify({$r2:blob}),'',now,1,0);
  await source.store.exec('INSERT INTO members VALUES(?,?)','issue_comments:{"number":7}',huge);
  await source.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)','issue_comments:{"number":7}','issue_comments','{"number":7}',1,now,1,null);
  await source.store.exec('INSERT INTO activity_landings VALUES(?,?,?,?)','7','head',1792000000123456,'receipt');
  await source.store.exec('INSERT INTO cursors VALUES(?,?,?)','retained-cursor',now+600000,JSON.stringify({resource:{$r2:blob}}));
  const pages=[];
  for(const table of Object.keys(columns)) {
   let after=0;
   for(;;){const rows=(await source.admin.query(`SELECT * FROM ${source.schema}.${table} WHERE rowid>$1 ORDER BY rowid LIMIT 100`,[after])).rows;
    if(!rows.length)break;const text=JSON.stringify(rows);pages.push({table,text,sha256:digest(text)});after=rows.at(-1).rowid;
   }
  }
  async function restore(corrupt=false) {
   await target.admin.query('BEGIN');
   try {
    await target.admin.query('TRUNCATE '+Object.keys(columns).map(t=>target.schema+'.'+t).join(','));
    for(const [index,page] of pages.entries()) {
     const text=corrupt && index===1?page.text+' ':page.text;
     if(digest(text)!==page.sha256)throw Error('archive_digest_mismatch');
     for(const row of JSON.parse(text)){const fields=Object.keys(row);await target.admin.query(`INSERT INTO ${target.schema}.${page.table} (${fields.join(',')}) OVERRIDING SYSTEM VALUE VALUES (${fields.map((_,i)=>'$'+(i+1)).join(',')})`,Object.values(row));}
    }
    for(const table of Object.keys(columns))await target.admin.query(`SELECT setval(pg_get_serial_sequence($1,'rowid'),GREATEST(COALESCE((SELECT MAX(rowid) FROM ${target.schema}.${table}),0),1),EXISTS(SELECT 1 FROM ${target.schema}.${table}))`,[target.schema+'.'+table]);
    await target.admin.query('COMMIT');
   }catch(error){await target.admin.query('ROLLBACK');throw error;}
  }
  await assert.rejects(restore(true),/archive_digest_mismatch/);await restore();
  for(const page of pages){const rows=(await target.admin.query(`SELECT * FROM ${target.schema}.${page.table} ORDER BY rowid`)).rows;assert.equal(digest(JSON.stringify(rows)),page.sha256);}
  await target.env.ARCHIVE.put(blob,source.objects.get(blob));
  const restored=(await target.store.exec('SELECT data FROM resources WHERE id=?',huge)).one();assert.equal((await target.state.decode(restored.data)).body,'retained archive contents');
  assert.equal((await target.store.exec('SELECT at FROM activity_landings')).one().at,1792000000123456);
  assert.ok((await target.store.exec('SELECT payload FROM cursors')).one().payload.includes(blob));
  await target.state.put('pr','8',{number:8,body:'post-restore progress',state:'OPEN'},Date.now());
  assert.ok((await target.store.exec("SELECT data FROM resources WHERE kind='pr' AND id='8'")).one().data.includes('post-restore progress'));
 }finally {await source.close();await target.close();}
});

pg('slow PostgreSQL transactions and terminated connections recover through the same store without partial writes',async()=>{
 const f=await postgresFixture();try {
  const start=performance.now();
  await assert.rejects(f.store.transaction(async tx=>{
   await tx.exec('INSERT INTO demand VALUES(?,?)','interrupted',Date.now());
   await tx.exec("SELECT set_config('statement_timeout','100ms',true)");await tx.exec('SELECT pg_sleep(5)');
  }),/Repository database unavailable/);
  assert.ok(performance.now()-start<10000);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM demand')).one().n,0);
  await assert.rejects(f.store.transaction(async tx=>{await tx.exec('SELECT pg_terminate_backend(pg_backend_pid())');}),/Repository database unavailable/);
  await f.store.exec('INSERT INTO demand VALUES(?,?)','automatic-next-attempt',Date.now());
  assert.equal((await f.store.exec('SELECT key FROM demand')).one().key,'automatic-next-attempt');
 }finally {await f.close();}
});

pg('a lost commit response replays idempotently and imported legacy cursors expire with a bounded checkpoint',async()=>{
 const f=await postgresFixture();try {
  let lost=true;
  f.state.store=new Proxy(f.store,{get(target,key){
   if(key==='transaction')return async apply=>{const result=await target.transaction(apply);if(lost){lost=false;throw Error('Repository database unavailable (08006)');}return result;};
   const value=target[key];return typeof value==='function'?value.bind(target):value;
  }});
  const pointer={key:'fixture/lost',event:'issue_comment',delivery:'lost-response',repository:repo.name,received:Date.now()};
  await f.env.ARCHIVE.put(pointer.key,JSON.stringify({issue:{number:7},comment:{id:'9',body:'exactly once',created_at:'2026-10-19T00:00:00Z'}}));
  assert.equal((await send(f.state,'/event',pointer)).status,503);
  assert.equal((await send(f.state,'/event',pointer)).status,200);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM deliveries')).one().n,1);
  await f.store.batch([{sql:'INSERT INTO cursors VALUES(?,?,?)',bindings:['old',Date.now()-1,'{}']},{sql:'INSERT INTO cursors VALUES(?,?,?)',bindings:['live',Date.now()+600000,'{}']}]);
  const progress=await retention.sweepStore(f.store,'expired_legacy_cursors',undefined,{rows:10,bytes:10000,deadline:Date.now()+5000},true);
  assert.equal(progress.deleted,1);assert.equal((await f.store.exec('SELECT id FROM cursors')).one().id,'live');
 }finally {await f.close();}
});

pg('PostgreSQL storage and connection alarms retain prepared health when alarm delivery fails',async()=>{
 const f=await postgresFixture();const output=console.log;try {
  f.env.ARCHIVE=bucket();let now=Date.now(),stats={prepared_at:now,database_bytes:700,storage_limit_bytes:1000,observation:{connections:{used:7,max:10,reserved:0}},tables_prepared_at:now,tables:{}};
  f.state.store=new Proxy(f.store,{get(target,key){if(key==='prepareMonitoring')return async()=>{};if(key==='health')return async()=>({backend:'postgres',generation:'test-v1',schema_version:1,connected:true,capacity:stats});const value=target[key];return typeof value==='function'?value.bind(target):value;}});
  console.log=()=>{throw Error('observability unavailable');};
  await f.state.storageMaintenance();let value=await f.state.preparedObservation();
  assert.equal(value.level,'warning');assert.equal(value.alarm.emitted,false);assert.equal(value.alarm.notified,false);assert.equal(value.capacity.growable,true);
  f.state.observation.at=now-61000;stats={...stats,prepared_at:now+1,database_bytes:850,observation:{connections:{used:9,max:10,reserved:0}}};
  await f.state.storageMaintenance();value=await f.state.preparedObservation();assert.equal(value.level,'critical');assert.ok(value.reasons.includes('connection_saturation'));
  f.state.observation.at=now-61000;stats={...stats,prepared_at:now+2,database_bytes:1,storage_limit_bytes:null,observation:{connections:{used:1,max:10,reserved:0}}};
  await f.state.storageMaintenance();value=await f.state.preparedObservation();assert.equal(value.capacity.capacity_unknown,true);assert.equal(value.level,'ok');
 }finally {console.log=output;await f.close();}
});

pg('missing control and missing monitoring function alarm precisely without blocking prepared health',async()=>{
 const f=await postgresFixture();try {
  f.env.ARCHIVE=bucket();
  await f.admin.query(`DELETE FROM ${f.schema}.backend_control`);
  await f.state.storageMaintenance();let value=await f.state.preparedObservation();
  assert.equal(value.level,'critical');assert.ok(value.reasons.includes('backend_generation_fenced'));assert.equal(value.storage.stale,true);
  await f.admin.query(`INSERT INTO ${f.schema}.backend_control VALUES(1,'postgres','test-v1',1)`);
  await f.admin.query(`UPDATE ${f.schema}.backend_control SET schema_version=2`);f.state.observation.at=Date.now()-61000;
  await f.state.storageMaintenance();assert.ok((await f.state.preparedObservation()).reasons.includes('backend_generation_fenced'));
  await f.admin.query(`UPDATE ${f.schema}.backend_control SET schema_version=1`);
  await f.admin.query(`DROP FUNCTION ${f.schema}.prepare_monitoring(TEXT,BOOLEAN,BIGINT)`);
  f.state.observation.at=Date.now()-61000;
  await f.state.storageMaintenance();value=await f.state.preparedObservation();
  assert.equal(value.level,'warning');assert.ok(value.reasons.includes('monitoring_unavailable'));assert.equal(value.stale,true);
  f.state.store=new Proxy(f.store,{get(target,key){if(['exec','batch','health'].includes(key))return ()=>{throw Error('unexpected status SQL');};const v=target[key];return typeof v==='function'?v.bind(target):v;}});
  const health=await (await send(f.state,'/health')).json();assert.equal(health.storage.stale,true);assert.ok(health.storage.observed_at);
 }finally {await f.close();}
});

pg('SQL sampling uses one hourly token across clocks, retries known busy work, and ignores temporary catalog shadows',async()=>{
 const f=await postgresFixture();try {
  const firstToken=Date.now();const first=await f.store.prepareMonitoring(true,firstToken);assert.equal(first.analyzed,true);
  await f.admin.query(`UPDATE ${f.schema}.capacity_statistics SET tables_attempt_at=$1`,[firstToken-3600000]);
  // A recently prepared minute sample must not suppress an hourly analysis.
  const next=await f.store.prepareMonitoring(true,firstToken);assert.equal(next.analyzed,true);
  const third=await f.store.prepareMonitoring(true,firstToken+1);assert.equal(third.analysis_attempted,false);
  await f.admin.query('BEGIN');await f.admin.query("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",[f.schema+':monitoring']);
  const busy=await f.store.prepareMonitoring(true);assert.equal(busy.reason,'busy');assert.equal(busy.analysis_attempted,false);await f.admin.query('ROLLBACK');
  await f.store.transaction(async tx=>{
   await tx.exec('CREATE TEMP TABLE pg_class(oid oid,relname name,relnamespace oid,relkind char,reltuples real)');
   await tx.exec("INSERT INTO pg_class(oid,relname,relnamespace,relkind,reltuples) VALUES(0,'forged',0,'r',999999)");
   await tx.exec('CREATE TEMP TABLE pg_stat_activity(backend_type text,state text,wait_event_type text,datname name)');
   await tx.exec("INSERT INTO pg_stat_activity(backend_type,state,wait_event_type,datname) VALUES('client backend','active',NULL,current_database())");
   await tx.exec(`SELECT "${f.schema}".prepare_monitoring(?,true,?)`,'test-v1',Date.now());
  });
  const actual=(await f.store.health()).capacity;assert.equal(Object.hasOwn(actual.tables,'forged'),false);assert.ok(actual.observation.connections.used>=1);
 }finally {await f.admin.query('ROLLBACK').catch(()=>{});await f.close();}
});

pg('monitoring has a server timeout, a failed expensive pass is not retried blindly, and later attempts recover',async()=>{
 const f=await postgresFixture();try {
  await f.admin.query(`CREATE FUNCTION ${f.schema}.slow_monitor() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(8); RETURN NEW; END $$; CREATE TRIGGER slow_monitor BEFORE UPDATE ON ${f.schema}.capacity_statistics FOR EACH ROW EXECUTE FUNCTION ${f.schema}.slow_monitor()`);
  const started=performance.now();await assert.rejects(f.store.prepareMonitoring(true),/57014/);assert.ok(performance.now()-started<7000);
  await f.admin.query(`DROP TRIGGER slow_monitor ON ${f.schema}.capacity_statistics`);
  assert.equal((await f.store.prepareMonitoring(true)).sampled,true);
  f.env.ARCHIVE=bucket();const attempts=[];
  f.state.store=new Proxy(f.store,{get(target,key){if(key==='prepareMonitoring')return async analyze=>{attempts.push(analyze);throw Error('Repository database unavailable');};const v=target[key];return typeof v==='function'?v.bind(target):v;}});
  await f.state.storageMaintenance();f.state.observation.at=Date.now()-61000;
  await f.state.storageMaintenance();assert.deepEqual(attempts,[true,false]);
  f.env.ARCHIVE=bucket();f.state.observation=undefined;const known=[];
  f.state.store=new Proxy(f.store,{get(target,key){if(key==='prepareMonitoring')return async analyze=>{known.push(analyze);return {analysis_attempted:false,reason:'busy'};};const v=target[key];return typeof v==='function'?v.bind(target):v;}});
  await f.state.storageMaintenance();
  const key='control/100/postgres-table-attempt-test-v1.json',pending=await f.env.ARCHIVE.get(key).then(o=>o.json());
  assert.equal(pending.not_started,true);await f.env.ARCHIVE.put(key,JSON.stringify({...pending,retry_at:Date.now()-1}));
  f.state.observation.at=Date.now()-61000;await f.state.storageMaintenance();assert.deepEqual(known,[true,true]);
 }finally {await f.close();}
});

pg('restricted connection observations include open application sessions',async()=>{
 const f=await postgresFixture(),clients=[];try {
  const {Client}=await import('@neondatabase/serverless');
  for(let i=0;i<4;i++){const client=new Client({connectionString:process.env.TEST_POSTGRES_URL,connectionTimeoutMillis:5000});await client.connect();await client.query('SELECT 1');clients.push(client);}
  await f.store.prepareMonitoring(true);
  const value=(await f.store.health()).capacity.observation.connections;
  assert.ok(value.used>=clients.length,'app sessions cannot disappear behind restricted backend_type visibility');
  assert.match(value.measurement,/unclassified_authenticated_upper_bound/);
  const member=(await f.admin.query("SELECT pg_has_role(current_user,'github_state_test_app','USAGE') member,pg_has_role(current_user,'pg_read_all_stats','USAGE') AS all_stats")).rows[0];
  if(!member.member && !member.all_stats)assert.ok(value.unclassified_authenticated>=clients.length);
 }finally {await Promise.allSettled(clients.map(client=>client.end()));await f.close();}
});

pg('a single transient parkable event failure does not become a persistent critical alarm',async t=>{
 const f=await postgresFixture();try {
  f.env.ARCHIVE=bucket();let fail=true;
  f.state.store=new Proxy(f.store,{get(target,key){
   if(key==='transaction')return async apply=>{if(fail)throw Error('Repository database unavailable');return target.transaction(apply);};
   if(key==='prepareMonitoring')return async()=>({analysis_attempted:false,reason:'recent_sample'});
   if(key==='health')return async()=>({backend:'postgres',schema_version:1,generation:'test-v1',connected:true,capacity:{prepared_at:Date.now(),database_bytes:1,storage_limit_bytes:null}});
   const value=target[key];return typeof value==='function'?value.bind(target):value;
  }});
  await f.env.ARCHIVE.put('fixture/transient',JSON.stringify({issue:{number:7},comment:{id:'22',body:'durable',created_at:'2026-10-19T00:00:00Z'}}));
  const pointer={key:'fixture/transient',delivery:'transient',event:'issue_comment',repository:repo.name,received:Date.now()};
  assert.equal((await send(f.state,'/event',pointer)).status,503);
  t.mock.timers.enable({apis:['Date'],now:Date.now()});t.mock.timers.tick(70000);
  await f.state.storageMaintenance();assert.equal((await f.state.preparedObservation()).reasons.includes('input_application_unavailable'),false);
  assert.equal((await send(f.state,'/event',pointer)).status,503);f.state.observation.at=Date.now()-61000;
  await f.state.storageMaintenance();assert.ok((await f.state.preparedObservation()).reasons.includes('input_application_unavailable'));
  fail=false;assert.equal((await send(f.state,'/event',pointer)).status,200);f.state.observation.at=Date.now()-61000;
  await f.state.storageMaintenance();assert.equal((await f.state.preparedObservation()).reasons.includes('input_application_unavailable'),false);
 }finally {t.mock.timers.reset();await f.close();}
});

pg('repeated PostgreSQL resource refreshes reuse immutable spill contents across retained snapshots',async()=>{
 const f=await postgresFixture();try {
  f.env.ARCHIVE=bucket();
  const data={id:'9223372036854775807',body:'retained '.repeat(200000)};
  await f.state.put('issue_comments',data.id,data,Date.now(),false,'7');
  const old=(await f.store.exec('SELECT data FROM resources WHERE kind=? AND id=?','issue_comments',data.id)).one().data;
  const revision=await f.state.revision();
  await f.store.exec('INSERT INTO cursors VALUES(?,?,?)','snapshot',Date.now()+600000,JSON.stringify({data:JSON.parse(old)}));
  for(let n=0;n<12;n++)await f.state.put('issue_comments',data.id,data,Date.now(),false,'7');
  assert.equal(await f.state.revision(),revision);
  assert.equal([...f.env.ARCHIVE.objects.keys()].filter(k=>k.startsWith('resources/')).length,1);
  await f.state.put('issue_comments',data.id,{...data,body:data.body+' changed'},Date.now(),false,'7');
  const snapshot=JSON.parse((await f.store.exec('SELECT payload FROM cursors WHERE id=?','snapshot')).one().payload);
  assert.deepEqual(await f.state.decode(JSON.stringify(snapshot.data)),data);
  assert.equal([...f.env.ARCHIVE.objects.keys()].filter(k=>k.startsWith('resources/')).length,2);
 }finally {await f.close();}
});

for(const loseResponse of[false,true])pg('historical CI replay batches fenced PostgreSQL receipts before R2 cleanup without GitHub; lost response='+loseResponse,async()=>{
 const f=await postgresFixture();try{
  f.env.ARCHIVE=bucket();let batches=0,individual=0,lost=loseResponse;
  f.env.INSTALLATIONS={idFromName(){throw Error('CI receipt replay must not fetch GitHub');}};
  f.env.REPOSITORIES={idFromName:id=>({toString:()=>id}),get:()=>({fetch:async(url,init)=>{
   const path=new URL(url).pathname;if(path==='/ci-receipts')batches++;else if(path==='/event')individual++;
   const r=await f.state.fetch(new Request(url,init));if(path==='/ci-receipts'&&r.ok&&lost){lost=false;throw Error('lost commit response');}return r;
  }})};
  const originalRevision=await f.state.revision(),items=[];
  for(let i=0;i<30;i++){const p={key:'webhooks/ci/'+i,repository:repo.name,delivery:'pg-ci-'+i,event:i%2?'check_run':'workflow_run',received:Date.now()-10000+i};await f.env.ARCHIVE.put(p.key,'{}');await ingress.park(f.env,repo,p,'migration');items.push(p);}
  const semantic={key:'webhooks/ci/semantic',repository:repo.name,delivery:'mixed-semantic',event:'issue_comment',received:Date.now()};
  await f.env.ARCHIVE.put(semantic.key,JSON.stringify({issue:{number:7},comment:{id:'mixed-comment',body:'must apply normally',created_at:'2026-10-19T00:00:00Z'}}));
  await ingress.park(f.env,repo,semantic,'migration');
  f.env.ARCHIVE.failures.push((op,key)=>op==='put'&&key===ingress.receiptKey(repo.id,'pg-ci-0'));
  const first=await ingress.drain(f.env,repo,{limit:100});assert.equal(batches,1);assert.equal(first.batched,loseResponse?0:30);assert.equal(first.batch_status,loseResponse?'transport_error':200);assert.equal(individual,loseResponse?31:1);assert.equal(first.failed,1);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM deliveries')).one().n,31);
  assert.deepEqual((await f.store.exec('SELECT kind,id FROM changes ORDER BY revision')).toArray(),[{kind:'issue_comments',id:'mixed-comment'},{kind:'collection',id:'issue_comments:{"number":7}'}],'only the semantic comment and its membership publish changes');
  assert.equal(await f.state.revision(),originalRevision+2);
  assert.ok((await f.store.exec("SELECT data FROM resources WHERE kind='issue_comments' AND id='mixed-comment'")).one().data.includes('must apply normally'));
  const key=ingress.pointerKey(repo.id,items[0]),p=await(await f.env.ARCHIVE.get(key)).json();await f.env.ARCHIVE.put(key,JSON.stringify({...p,retry_at:0}));
  assert.equal((await ingress.drain(f.env,repo,{limit:100})).failed,0);assert.equal((await f.env.ARCHIVE.list({prefix:'parked/100/'})).objects.length,0);
  assert.equal(await f.state.revision(),originalRevision+2);
  const r=await send(f.state,'/ci-receipts',{repository:repo.name,events:[{delivery:'semantic-not-allowed',event:'issue_comment',received:Date.now()}]});assert.equal(r.status,400);
  await f.admin.query(`UPDATE ${f.schema}.backend_control SET backend='migration' WHERE singleton=1`);
  assert.equal((await send(f.state,'/ci-receipts',{repository:repo.name,events:[{delivery:'wrong-owner',event:'workflow_job',received:Date.now()}]})).status,503);
  assert.equal((await f.store.exec("SELECT COUNT(*) n FROM deliveries WHERE id='wrong-owner'")).one().n,0);
 }finally{await f.close();}
});

pg('leased admin replay preserves PostgreSQL commit receipts after a lost cleanup acknowledgement',async()=>{
 const f=await postgresFixture();try{
  f.env.ARCHIVE=bucket();f.env.INSTALLATIONS={idFromName(){throw Error('replay must not fetch GitHub');}};
  f.env.REPOSITORIES={idFromName:id=>({toString:()=>id}),get:()=>({fetch:(url,init)=>f.state.fetch(new Request(url,init))})};
  const pointer={repository:repo.name,delivery:'pg-cleanup-ack',event:'workflow_job',received:Date.now(),key:'webhooks/pg-cleanup-ack'};
  await f.env.ARCHIVE.put(pointer.key,'{}');await ingress.park(f.env,repo,pointer,'migration');
  const key=ingress.pointerKey(repo.id,pointer),remove=f.env.ARCHIVE.delete;let lost=true;
  f.env.ARCHIVE.delete=async keys=>{await remove(keys);if(lost&&[].concat(keys).includes(key)){lost=false;throw Error('lost cleanup acknowledgement');}};
  const revision=await f.state.revision(),first=await ingress.replayIngress(f.env,repo);
  assert.equal(first.failed,0);assert.equal(first.superseded,1);assert.equal(first.delivery_errors[0].delivery,pointer.delivery);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM deliveries')).one().n,1);assert.equal(await f.state.revision(),revision);
  assert.equal(await f.env.ARCHIVE.get(key),null);assert.equal(await f.env.ARCHIVE.get(ingress.bodyKey(repo.id,pointer.delivery)),null);
  const receipt=f.env.ARCHIVE.objects.get(ingress.receiptKey(repo.id,pointer.delivery)).bytes.toString();
  assert.equal((await send(f.state,'/event',pointer)).status,200);
  assert.equal(f.env.ARCHIVE.objects.get(ingress.receiptKey(repo.id,pointer.delivery)).bytes.toString(),receipt);
  assert.equal((await ingress.replayIngress(f.env,repo)).replayed,0);assert.equal((await f.store.exec('SELECT COUNT(*) n FROM changes')).one().n,0);
 }finally{await f.close();}
});
