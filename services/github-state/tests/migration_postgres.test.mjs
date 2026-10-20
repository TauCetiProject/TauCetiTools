import {test} from 'node:test';
import assert from 'node:assert/strict';
import {randomBytes} from 'node:crypto';
import {Client} from '@neondatabase/serverless';
import {fixture,fill,load,migration,generation} from './migration_fixture.mjs';
import {initializeSchema} from '../scripts/postgres-schema.mjs';
import {testCredentials} from './postgres_credentials.mjs';
const target=await load('src/migration_target.ts');
const control=await load('src/migration_control.ts');
const enabled=Boolean(process.env.TEST_POSTGRES_URL);
async function testFixture(){
 const credentials=testCredentials(),admin=new Client(credentials.migration.toString());await admin.connect();
 const id=BigInt('0x'+randomBytes(8).toString('hex')).toString(),schema='github_state_'+id;
 await admin.query('BEGIN');await initializeSchema(admin,id,generation,decodeURIComponent(credentials.application.username),{standby:true});
 await admin.query(`INSERT INTO ${schema}.capacity_statistics VALUES(1,$1,0,1099511627776,$1)`,[Date.now()]);await admin.query('COMMIT');
 const f=await fixture();await fill(f);
 const state={version:1,generation,repository:id,target:'neon-test',source:{object:'source-object',deployment:'test-deployment',schema:5},phase:'freezing',created:Date.now(),updated:Date.now(),tables:{},failure:null,
  staging:{after:null,page:'1',checked:0,complete:false,expired_jobs:0,expired_hash:'0'.repeat(64)},blobs:{prefix:0,pages:0,objects:0,hash:'0'.repeat(64),complete:false},
  cutover:{environment:'test',lease_until:Date.now()+900000,epoch:0,source_fenced:false,target_active:false,import:{},verify:{},acceptance:''}};
 f.env.REPOSITORY_DATABASE_URL=credentials.application.toString();f.env.MIGRATION_DATABASE_URL=credentials.migration.toString();
 f.env.MIGRATION_TARGET=JSON.stringify({identity:state.target,hostname:credentials.migration.hostname,database:decodeURIComponent(credentials.migration.pathname.slice(1)),application_role:decodeURIComponent(credentials.application.username),migration_role:decodeURIComponent(credentials.migration.username)});
 const checks=Object.fromEntries(['s0_compatibility','parked_ingress','queue_dlq_ownership','r2_retention_drained','s1_schema_roles','capacity','restore','unchanged_clients','stored_shadow','no_github_refetch','warm_read_p95'].map(x=>[x,true]));
 await f.archive.put(`control/${id}/migration-acceptance.json`,JSON.stringify({version:1,environment:'test',repository:id,target:state.target,deployment:'test-deployment',source_object:'source-object',valid_until:Date.now()+86400000,checks,queries:[{repository:'org/repo',operation:'open_prs'}],latency:{baseline_p95_ms:100,target_p95_ms:100}}));
 const retention=(await load('src/retention.ts')).R2_CLASSES;
 await f.archive.put(`control/${id}/retention-policy.json`,JSON.stringify({mode:'dry_run',disabled:[],pinned:Object.keys(retention)}));
 // This focused protocol fixture uses SQL belonging to a random isolated target;
 // R2 references remain exact source addresses, with no public/GitHub requests.
 let fenced=false,selected=false,released=false;
 const hooks={storage:f.ctx.storage,sql:f.ctx.storage.sql,async verifyStored(state){state.cutover.shadow={queries:1,hash:"fixture",verified_at:Date.now(),complete:true};},async drain(){},async fence(){fenced=true;},assertFence(){assert.ok(fenced);},async release(){released=true;fenced=false;},async select(){assert.ok(fenced);selected=true;}};
 return {...f,source:f.state,state,id,schema,admin,hooks,get selected(){return selected;},get released(){return released;},async close(){f.db.close();await admin.query(`DROP SCHEMA ${schema} CASCADE`);await admin.end();}};
}
test('S2 imports exact archives into a standby, restarts every step and recovers a lost switch commit', {skip:!enabled},async()=>{
 const f=await testFixture();
 try {
  await control.preflight(f.env,f.state);await migration.saveMigration(f.archive,f.state);
  let importedCrash=false,validationCrash=false;
  for(let n=0;n<600;n++){
   const state=await migration.readMigration(f.archive,f.id);
   if(state.phase==='switching')break;
   const op=state.phase==='importing'?'import':state.phase==='target_verifying'?'verify':null;
   if(op && !(op==='import'?importedCrash:validationCrash)) {
    // Fail after the target transaction commits, before its R2 checkpoint.
    await migration.saveMigration(f.archive,state);
    if(op==='import') {await target.importStep(f.env,state);importedCrash=true;}
    else {await target.targetVerifyStep(f.env,state);validationCrash=true;}
   }else await control.controllerStep(f.env,state,f.hooks);
   if(n===599)assert.fail('supervisor stalled');
  }
  assert.ok(importedCrash && validationCrash);
  let state=await migration.readMigration(f.archive,f.id);assert.equal(state.phase,'switching');
  const rows=(await f.admin.query(`SELECT rowid::text,at::text FROM ${f.schema}.demand ORDER BY rowid`)).rows;
  assert.deepEqual(rows[0],{rowid:'-9223372036854775808',at:'9223372036854775807'});
  assert.deepEqual(rows.at(-1),{rowid:'9223372036854775807',at:'-9223372036854775808'});
  await target.activateTarget(f.env,state); // crash before writing R2 selection
  state.cutover.lease_until=1;await migration.saveMigration(f.archive,state);
  await control.controllerStep(f.env,await migration.readMigration(f.archive,f.id),f.hooks);
  state=await migration.readMigration(f.archive,f.id);
  assert.equal(state.phase,'replaying');assert.equal(state.cutover.target_active,true);assert.equal(f.selected,true);assert.equal(f.released,false);
  const job=(await f.admin.query(`SELECT due::text,attempt::text,generation::text,lease_id,lease_until::text FROM ${f.schema}.jobs`)).rows[0];
  assert.deepEqual(job,{due:'9999999999999',attempt:'4',generation:'17',lease_id:null,lease_until:'0'});
  await assert.rejects(target.abandonTarget(f.env,state),/migration_forward_recovery_required/);
 }finally{await f.close();}
});
test('expired freeze automatically abandons before writes, retaining archives and fencing stale importers',{skip:!enabled},async()=>{
 const f=await testFixture();try {
  await control.preflight(f.env,f.state);await migration.saveMigration(f.archive,f.state);
  await control.controllerStep(f.env,f.state,f.hooks);
  const stale=await migration.readMigration(f.archive,f.id);
  stale.cutover.lease_until=1;await migration.saveMigration(f.archive,stale);
  await control.controllerStep(f.env,await migration.readMigration(f.archive,f.id),f.hooks);
  assert.equal((await migration.readMigration(f.archive,f.id)).phase,'abandoned');assert.equal(f.released,true);
  assert.equal(await target.targetOwner(f.env,stale),'abandoned');
  await assert.rejects(target.activateTarget(f.env,stale),/migration_target_owner_mismatch/);
  assert.ok(f.archive.objects.has(migration.archiveKey(f.id,generation)+'/receipt.json'));
 }finally{await f.close();}
});
test('target preflight rejects identity, nonempty state, missing restore and unsafe app roles',{skip:!enabled},async()=>{
 const f=await testFixture();try {
  const state=f.state;
  await target.targetPreflight(f.env,state);
  await assert.rejects(target.targetPreflight(f.env,{...state,target:'wrong-target'}),/migration_target_identity_mismatch/);
  await f.admin.query(`INSERT INTO ${f.schema}.demand(key,at) VALUES('unexpected',1)`);
  await assert.rejects(target.targetPreflight(f.env,state),/migration_target_not_empty/);
  await f.admin.query(`DELETE FROM ${f.schema}.demand; UPDATE ${f.schema}.capacity_statistics SET restore_verified_at=NULL`);
  await assert.rejects(target.targetPreflight(f.env,state),/migration_capacity_restore_unverified/);
 }finally{await f.close();}
});
test('ordinary supervisor restarts a fenced repository, preserves cursors, and replays an input arriving during freeze',{skip:!enabled},async()=>{
 let f=await testFixture();const cleanup=f.close;
 try {
  const custom={id:f.id,name:'org/repo',installation:'200'};
  await f.source.setMeta('repository',custom);f.env.ALLOWED_REPOSITORIES=JSON.stringify([custom]);
  f.db.prepare('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)').run('pr','7','',JSON.stringify({number:7,state:'OPEN',title:'Stored title'}),'',123456,123,0);
  f.db.prepare('INSERT INTO collections VALUES(?,?,?,?,?,?,?)').run('open_prs:{}','open_prs','{}',1,123456,123,'original-etag');
  f.db.prepare('INSERT INTO members VALUES(?,?)').run('open_prs:{}','7');
  const query={repository:custom.name,operation:'open_prs'};
  const before=await (await f.request('/query',query)).json();
  const cursor='0b6c2f0e-1f7a-4b4c-9a1e-5b8d3c2a1f00',page={version:1,repository:custom.name,operation:'open_prs',status:'ok',revision:123,complete:true,data:[{number:7}]};
  await f.archive.put(`cursors/${f.id}/${cursor}/100.json`,JSON.stringify({repository:f.id,key:'open_prs:{}',allow_observed:false,expires:Date.now()+86400000,result:page}));
  assert.equal((await f.command('start',{target:'neon-test'})).status,200);
  f.archive.fail((op,key)=>op==='get' && key===`control/${custom.id}/migration.json`);
  const outage=await fixture(f.db,f.archive,{...f.env});
  const fallback=await outage.request('/query',query);
  assert.deepEqual(await fallback.json(),before);
  assert.equal(fallback.headers.get('X-TauCeti-Read-Fallback'),'last-known-source');
  assert.ok(Number(fallback.headers.get('X-TauCeti-Source-Frozen-At'))>0);
  await assert.rejects(outage.state.setMeta('forbidden',true),/migration_frozen|backend generation fenced/);
  assert.throws(()=>f.db.prepare("INSERT INTO demand VALUES('old-worker',1)").run(),/generation fenced/);
  const key='webhooks/freeze/event.json',body=JSON.stringify({issue:{number:7},comment:{id:'during-freeze',body:'retained comment',created_at:'2026-10-19T00:00:00Z'}});
  await f.archive.put(key,body);
  const {worker}=await import('./migration_fixture.mjs');
  await worker.queue({messages:[{body:{key,delivery:'freeze-event',event:'issue_comment',repository:custom.name,received:Date.now()},attempts:30,ack(){},retry(){assert.fail('queue retry during freeze');}}]},f.env);
  const original={...f};
  for(let n=0;n<50;n++) {
    const cold=await fixture(f.db,f.archive,{...f.env});f={...original,...cold};
    const response=await f.request('/migration-supervise',{}),receipt=await response.json();
    assert.equal(response.status,200,JSON.stringify(receipt));
    if(receipt.phase==='replaying')break;
    if(n===49)assert.fail('ordinary supervisor did not finish');
  }
  assert.equal(f.state.store.backend,'postgres');
  assert.deepEqual(await (await f.request('/query',query)).json(),before);
  assert.deepEqual(await (await f.request('/query',{...query,cursor:cursor+':100'})).json(),page);
  const conditional=await f.state.fetch(new Request('https://internal/query',{method:'POST',headers:{'if-none-match':'"123"'},body:JSON.stringify(query)}));assert.equal(conditional.status,304);
  const ingress=await load('src/ingress.ts');
  const pointer=(await f.archive.list({prefix:`parked/${custom.id}/pointers/`})).objects[0];
  const parked=await f.archive.get(pointer.key).then(o=>o.json());
  await f.state.ingest({key:parked.key,delivery:parked.delivery,event:parked.event,repository:custom.name,received:parked.received});
  const committedRevision=(await f.admin.query(`SELECT value FROM ${f.schema}.meta WHERE key='revision'`)).rows[0].value;
  f.archive.fail((op,key,at)=>op==='delete' && key.includes('/pointers/') && at==='after');
  await ingress.drain(f.env,custom);
  const pending=await f.archive.get(pointer.key).then(o=>o.json());
  assert.ok(pending.retry_at>Date.now());
  // Simulate the ordinary owner's next pass after its real retry deadline.
  await f.archive.put(pointer.key,JSON.stringify({...pending,retry_at:0}));
  await ingress.drain(f.env,custom);
  const revision=(await f.admin.query(`SELECT value FROM ${f.schema}.meta WHERE key='revision'`)).rows[0].value;
  assert.equal(revision,committedRevision,'replaying a committed event cannot publish another change');
  await ingress.drain(f.env,custom);
  assert.equal((await f.admin.query(`SELECT value FROM ${f.schema}.meta WHERE key='revision'`)).rows[0].value,revision);
  assert.equal((await f.admin.query(`SELECT COUNT(*)::text n FROM ${f.schema}.deliveries WHERE id='freeze-event'`)).rows[0].n,'1');
  assert.ok(f.archive.objects.has(`receipts/${custom.id}/freeze-event.json`));
  assert.ok(!(await f.archive.list({prefix:`parked/${custom.id}/pointers/`})).objects.length);
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>Response.json({hour:[]})})};
  await f.archive.put('control/raw-recovery.json',JSON.stringify({last_completed:Date.now(),gaps:{count:0,truncated:false}}));
  const done=await (await f.request('/migration-supervise',{})).json();
  assert.equal(done.phase,'complete');assert.equal(done.cutover.release.backend_write_owner,'postgres');
  const receiptKey=migration.archiveKey(custom.id,generation)+'/receipt.json';
  await f.archive.delete(receiptKey); // crash at release receipt publication
  assert.equal((await f.request('/migration-supervise',{})).status,200);
  assert.equal((await f.archive.get(receiptKey).then(o=>o.json())).phase,'complete');
 }finally{await cleanup();}
});
test('a still-full source stays fenced after lease expiry and ordinary supervision retries release',{skip:!enabled},async()=>{
 const f=await testFixture();try {
  await control.preflight(f.env,f.state);await migration.saveMigration(f.archive,f.state);await control.controllerStep(f.env,f.state,f.hooks);
  let state=await migration.readMigration(f.archive,f.id);state.cutover.lease_until=1;await migration.saveMigration(f.archive,state);
  await assert.rejects(control.controllerStep(f.env,await migration.readMigration(f.archive,f.id),{...f.hooks,async release(){throw Error('full source commit');}}),/full source commit/);
  state=await migration.readMigration(f.archive,f.id);assert.equal(state.phase,'aborting');assert.equal(f.released,false);
  assert.equal(await target.targetOwner(f.env,state),'abandoned');
  await control.controllerStep(f.env,state,f.hooks);
  assert.equal((await migration.readMigration(f.archive,f.id)).phase,'abandoned');assert.equal(f.released,true);
 }finally{await f.close();}
});
