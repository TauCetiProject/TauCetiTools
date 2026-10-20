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
  await state.storageMaintenance();state.postgresUnavailableSince=Date.now()-61000;state.observation.at=Date.now()-61000;
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
    await target.admin.query('COMMIT');
   }catch(error){await target.admin.query('ROLLBACK');throw error;}
  }
  await assert.rejects(restore(true),/archive_digest_mismatch/);await restore();
  for(const page of pages){const rows=(await target.admin.query(`SELECT * FROM ${target.schema}.${page.table} ORDER BY rowid`)).rows;assert.equal(digest(JSON.stringify(rows)),page.sha256);}
  await target.env.ARCHIVE.put(blob,source.objects.get(blob));
  const restored=(await target.store.exec('SELECT data FROM resources WHERE id=?',huge)).one();assert.equal((await target.state.decode(restored.data)).body,'retained archive contents');
  assert.equal((await target.store.exec('SELECT at FROM activity_landings')).one().at,1792000000123456);
  assert.ok((await target.store.exec('SELECT payload FROM cursors')).one().payload.includes(blob));
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
  f.state.store=new Proxy(f.store,{get(target,key){if(key==='prepareMonitoring')return async()=>{};if(key==='health')return async()=>({backend:'postgres',generation:'test-v1',connected:true,capacity:stats});const value=target[key];return typeof value==='function'?value.bind(target):value;}});
  console.log=()=>{throw Error('observability unavailable');};
  await f.state.storageMaintenance();let value=await f.state.preparedObservation();
  assert.equal(value.level,'warning');assert.equal(value.alarm.emitted,false);assert.equal(value.alarm.notified,false);assert.equal(value.capacity.growable,true);
  f.state.observation.at=now-61000;stats={...stats,prepared_at:now+1,database_bytes:850,observation:{connections:{used:9,max:10,reserved:0}}};
  await f.state.storageMaintenance();value=await f.state.preparedObservation();assert.equal(value.level,'critical');assert.ok(value.reasons.includes('connection_saturation'));
  f.state.observation.at=now-61000;stats={...stats,prepared_at:now+2,database_bytes:1,storage_limit_bytes:null,observation:{connections:{used:1,max:10,reserved:0}}};
  await f.state.storageMaintenance();value=await f.state.preparedObservation();assert.equal(value.capacity.capacity_unknown,true);assert.equal(value.level,'ok');
 }finally {console.log=output;await f.close();}
});
