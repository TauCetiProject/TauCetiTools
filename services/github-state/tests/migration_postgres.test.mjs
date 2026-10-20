import {test} from 'node:test';
import assert from 'node:assert/strict';
import {fixture,load,migration,generation} from './migration_fixture.mjs';
import {testFixture,target,control} from './migration_postgres_fixture.mjs';
const enabled=Boolean(process.env.TEST_POSTGRES_URL);
test('S2 imports exact archives into a standby, restarts every step and recovers a lost switch commit', {skip:!enabled},async()=>{
 const f=await testFixture(1000);
 try {
  f.db.prepare('INSERT INTO demand(rowid,key,at) VALUES(9,?,1),(10,?,2),(99,?,3),(100,?,4)').run('numeric-nine','numeric-ten','numeric-ninety-nine','numeric-hundred');
  const templateJob=f.db.prepare('SELECT * FROM jobs').get(),fields=Object.keys(templateJob);
  const clone=f.db.prepare('INSERT INTO jobs('+fields.join(',')+') VALUES('+fields.map(()=>'?').join(',')+')');
  for(let i=1;i<30;i++)clone.run(...fields.map(key=>key==='key'?'expired-'+i:templateJob[key]));
  let connections=0,widePreparation=false;f.env.migrationConnection=()=>{connections++;};
  const insert=f.db.prepare('INSERT INTO demand(rowid,key,at) VALUES(?,?,?)');
  f.db.exec('BEGIN');for(let i=500;i<5500;i++)insert.run(i,'wide-'+i,i);f.db.exec('COMMIT');
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
   }else {
    const before=connections;await control.controllerStep(f.env,state,f.hooks);
    if(state.phase==='preparing' && connections-before===25)widePreparation=true;
   }
   if(n===599)assert.fail('supervisor stalled');
  }
  assert.ok(importedCrash && validationCrash);assert.equal(widePreparation,true,'all 25 staging-repair connections are metered');
  let state=await migration.readMigration(f.archive,f.id);assert.equal(state.phase,'switching');
  const rows=(await f.admin.query(`SELECT rowid::text,at::text FROM ${f.schema}.demand t ORDER BY t.rowid`)).rows;
  assert.equal(rows.length,5007);
  assert.ok(state.tables.demand.pages<5,'large SQL batches stay below the parameter limit');
  assert.deepEqual(rows[0],{rowid:'-9223372036854775808',at:'9223372036854775807'});
  assert.deepEqual(rows.at(-1),{rowid:'9223372036854775807',at:'-9223372036854775808'});
  await target.activateTarget(f.env,state); // crash before writing R2 selection
  state.cutover.lease_until=1;await migration.saveMigration(f.archive,state);
  await control.controllerStep(f.env,await migration.readMigration(f.archive,f.id),f.hooks);
  state=await migration.readMigration(f.archive,f.id);
  assert.equal(state.phase,'replaying');assert.equal(state.cutover.target_active,true);assert.equal(f.selected,true);assert.equal(f.released,false);
  const job=(await f.admin.query(`SELECT due::text,attempt::text,generation::text,lease_id,lease_until::text,started::text,page::text,cursor,api,etag,nested FROM ${f.schema}.jobs WHERE key='fixture'`)).rows[0];
  assert.deepEqual(job,{due:'9999999999999',attempt:'4',generation:'17',lease_id:null,lease_until:'0',started:null,page:'1',cursor:null,api:null,etag:null,nested:null});
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
  await f.archive.delete('control/queue-metrics.json');
  await assert.rejects(control.preflight(f.env,state),/migration_queue_dlq_unavailable/);
  await f.archive.put('control/queue-metrics.json',JSON.stringify({queue:{at:Date.now(),count:0,error:null},dlq:{at:Date.now(),count:0,error:null}}));
  await f.archive.put(`parked/${f.id}/held/existing.json`,'{}');
  await assert.rejects(control.preflight(f.env,state),/migration_held_input_pending/);await f.archive.delete(`parked/${f.id}/held/existing.json`);
  await control.preflight(f.env,state);
  const config=target.targetConfig(f.env),quote=value=>'"'+value.replaceAll('"','""')+'"';
  await f.admin.query(`GRANT CREATE ON SCHEMA ${f.schema} TO ${quote(config.application_role)}; ALTER FUNCTION ${f.schema}.assert_writer(TEXT) OWNER TO ${quote(config.application_role)}; REVOKE CREATE ON SCHEMA ${f.schema} FROM ${quote(config.application_role)}`);
  await assert.rejects(target.targetPreflight(f.env,state),/migration_application_role_unsafe/);
  await f.admin.query(`ALTER FUNCTION ${f.schema}.assert_writer(TEXT) OWNER TO ${quote(config.migration_role)}`);
  const original={...f.env},changed=JSON.parse(f.env.MIGRATION_TARGET);changed.hostname='wrong-endpoint.neon.tech';
  f.env.MIGRATION_TARGET=JSON.stringify(changed);
  for(const key of ['MIGRATION_DATABASE_URL','REPOSITORY_DATABASE_URL']) {const url=new URL(f.env[key]);url.hostname=changed.hostname;f.env[key]=url.toString();}
  await assert.rejects(target.targetOwner(f.env,state),/migration_target_identity_mismatch/);
  Object.assign(f.env,original);
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
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>Response.json({deployment_id:'test-deployment',repository_inflight:[{repository:custom.id,requests:1}]})})};
  let waiting;for(let n=0;n<100;n++){waiting=await f.command('start',{target:'neon-test'});if(waiting.body.error==='migration_drain_pending')break;assert.equal(waiting.status,200,JSON.stringify(waiting.body));}
  assert.equal(waiting.body.error,'migration_drain_pending');
  assert.equal((await f.archive.get(migration.migrationKey(custom.id)).then(o=>o.json())).bookmark,undefined);
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>Response.json({deployment_id:'test-deployment',repository_inflight:[],hour:[]})})};
  assert.equal((await f.command('start',{target:'neon-test'})).status,200);
  f.archive.fail((op,key)=>op==='get' && key===`control/${custom.id}/migration.json`);
  const outage=await fixture(f.db,f.archive,{...f.env});
  const fallback=await outage.request('/query',query);
  assert.deepEqual(await fallback.json(),before);
  assert.equal(fallback.headers.get('X-TauCeti-Read-Fallback'),'last-known-source');
  assert.ok(Number(fallback.headers.get('X-TauCeti-Source-Frozen-At'))>0);
  // Real source fence/triggers from the controller survive fresh constructors,
  // including the fault-injection startup ordering used by the deployed drill.
  for(const fault of [false,true])for(const unavailable of [false,true]) {
    if(unavailable)f.archive.fail((op,key)=>op==='get' && key===`control/${custom.id}/migration.json`);
    const reset=await fixture(f.db,f.archive,{...f.env,FAULT_INJECTION:fault?'true':undefined});
    const response=await reset.request('/query',query);assert.equal(response.status,200);
    assert.deepEqual(await response.json(),before);
    await assert.rejects(reset.state.setMeta('forbidden',true),/migration_frozen|backend generation fenced/);
    assert.throws(()=>f.db.prepare("INSERT INTO demand VALUES('trigger-check',1)").run(),/backend generation fenced/);
  }
  const checkpoint=await f.archive.get(`control/${custom.id}/migration.json`).then(o=>o.text());
  const activated=JSON.parse(checkpoint);activated.cutover.target_active=true;
  await f.archive.put(`control/${custom.id}/migration.json`,JSON.stringify(activated));
  try {
    const configured=JSON.parse(f.env.MIGRATION_TARGET);
    const reset=await fixture(f.db,f.archive,{...f.env,MIGRATION_TARGET:JSON.stringify({...configured,identity:'mismatched-target'})});
    const rejected=await reset.request('/query',query);assert.equal(rejected.status,503);
    assert.equal((await rejected.json()).error,'backend_fenced','known target activation never falls back after configuration failure');
  }finally{await f.archive.put(`control/${custom.id}/migration.json`,checkpoint);}

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
  await f.archive.put('control/'+custom.id+'/storage.json',JSON.stringify({state:'full',since:1}));
  await f.request('/migration-supervise',{});
  assert.equal((await f.archive.get('control/'+custom.id+'/storage.json').then(o=>o.json())).state,'superseded');
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
  f.archive.fail((op,key)=>op==='get' && key===`control/${custom.id}/migration.json`);
  const cold=await fixture(f.db,f.archive,{...f.env});
  assert.equal(cold.state.store.backend,'postgres');
  const current=await cold.request('/changes',{});assert.equal((await current.json()).revision,Number(revision));
  assert.equal(current.headers.get('X-TauCeti-Read-Fallback'),null);
  const steady=await fixture(f.db,f.archive,{...f.env,MIGRATION_ENVIRONMENT:undefined,MIGRATION_DATABASE_URL:undefined,MIGRATION_TARGET:undefined});
  assert.equal(steady.state.store.backend,'postgres');assert.equal((await (await steady.request('/changes',{})).json()).revision,Number(revision));
  assert.equal(await ingress.storageState(steady.env,custom),null,'retired migration bindings never resurrect the SQLite full marker');
  await ingress.noteRepositoryFull(steady.env,custom.id,'late-old-worker',Date.now());assert.equal(await ingress.storageState(steady.env,custom),null);
  await steady.state.preparePostgresObservation(custom.id,{durable:true,backlog:{held:{count:0}}},Date.now());
  assert.equal(steady.state.observation.generation,generation);
  assert.ok(!steady.state.observation.reasons.includes('backend_generation_fenced'));
  steady.env.REPOSITORIES.idFromName=()=>({toString:()=>steady.ctx.id.toString()});
  steady.state.observation=undefined;assert.equal((await steady.state.preparedObservation()).generation,generation);
  await steady.state.prepareStorage(false);
  assert.equal((await f.archive.get('control/'+custom.id+'/retention-checkpoint.json').then(o=>o.json())).generation,generation);

  assert.equal(revision,committedRevision,'replaying a committed event cannot publish another change');
  await ingress.drain(f.env,custom);
  assert.equal((await f.admin.query(`SELECT value FROM ${f.schema}.meta WHERE key='revision'`)).rows[0].value,revision);
  assert.equal((await f.admin.query(`SELECT COUNT(*)::text n FROM ${f.schema}.deliveries WHERE id='freeze-event'`)).rows[0].n,'1');
  assert.ok(f.archive.objects.has(`receipts/${custom.id}/freeze-event.json`));
  assert.ok(!(await f.archive.list({prefix:`parked/${custom.id}/pointers/`})).objects.length);
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>Response.json({deployment_id:'test-deployment',repository_inflight:[],hour:[]})})};
  // An unrelated historical raw gap does not own this migration's queues.
  await f.archive.put('control/raw-recovery.json',JSON.stringify({last_completed:1,gaps:{count:1,truncated:false}}));
  const queueSample=async(at,count)=>f.archive.put('control/queue-metrics.json',JSON.stringify({queue:{at,count:0,error:null},dlq:{at,count,error:null}}));
  await queueSample(1,0);assert.equal((await (await f.request('/migration-supervise',{})).json()).phase,'replaying','pre-selection queue observations cannot release');
  await queueSample(Date.now(),1);assert.equal((await (await f.request('/migration-supervise',{})).json()).phase,'replaying','DLQ must drain before reconciliation resumes');
  assert.ok(!(await migration.readMigration(f.archive,custom.id)).cutover.replay_complete);
  await queueSample(Date.now(),0);
  const poison={...parked,key,delivery:'held-poison'};
  const pinned=await ingress.park(f.env,custom,poison,'fixture_failure');
  const normalKey=ingress.pointerKey(custom.id,poison),heldKey=ingress.pointerKey(custom.id,poison,true);
  await f.archive.put(heldKey,JSON.stringify({...pinned,retry_at:Date.now()+86400000}));await f.archive.delete(normalKey);
  await ingress.maintainIngress(f.env,custom);
  const visited=await f.archive.get(ingress.recoveryKey(custom.id)).then(o=>o.json());
  assert.equal(typeof visited.held_last_pass,'object');assert.ok(visited.held_pass_completed_at>=(await migration.readMigration(f.archive,custom.id)).cutover.selected_at);

  assert.equal((await (await f.request('/migration-supervise',{})).json()).phase,'replaying');
  assert.equal(migration.blocksRepairs(await migration.readMigration(f.archive,custom.id)),false,'a deferred poison item cannot indefinitely stop bounded reconciliation');
  assert.ok(await f.archive.head(heldKey),'failed input remains durable and blocks final completion');
  assert.equal((await f.request('/event',poison)).status,200); // target receipt commits first
  await ingress.release(f.env,custom,poison);assert.ok(!await f.archive.head(heldKey));
  const done=await (await f.request('/migration-supervise',{})).json();
  assert.equal(done.phase,'complete');assert.equal(done.cutover.release.backend_write_owner,'postgres');
  const receiptKey=migration.archiveKey(custom.id,generation)+'/receipt.json';
  const immutable=f.archive.objects.get(receiptKey).text;await f.request('/migration-supervise',{});assert.equal(f.archive.objects.get(receiptKey).text,immutable);
  await f.archive.delete(receiptKey); // crash at release receipt publication
  assert.equal((await f.request('/migration-supervise',{})).status,200);
  assert.equal((await f.archive.get(receiptKey).then(o=>o.json())).phase,'complete');
  const invalid=await f.command('finish-observation',{target:'neon-test',retention_policy:{mode:'invalid'}});assert.equal(invalid.body.error,'migration_retention_policy_invalid');
  f.archive.fail((op,key)=>op==='put' && key.endsWith('/retention-policy.json'));
  assert.equal((await f.command('finish-observation',{target:'neon-test',retention_policy:{mode:'dry_run',disabled:[],pinned:[]}})).status,503);
  f.archive.fail((op,key)=>op==='put' && key.endsWith('/retention-policy.json'));
  assert.equal((await f.request('/migration-supervise',{})).status,503);
  assert.equal((await migration.readMigration(f.archive,custom.id)).failure,'migration_storage_failure');
  const release=await f.command('finish-observation',{target:'neon-test',retention_policy:{mode:'dry_run',disabled:[],pinned:[]}});
  assert.equal(release.status,200,JSON.stringify(release.body));
  assert.equal(migration.holdsRetention(await migration.readMigration(f.archive,custom.id)),false);
  assert.deepEqual((await f.archive.get(`control/${custom.id}/retention-policy.json`).then(o=>o.json())).pinned,[]);
  const observationKey=migration.archiveKey(custom.id,generation)+'/observation-receipt.json';
  assert.ok(await f.archive.head(observationKey));await f.archive.delete(observationKey);
  await f.request('/migration-supervise',{});assert.ok(await f.archive.head(observationKey));
  assert.equal(f.archive.objects.get(receiptKey).text,immutable);
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

test('preflight rejects duplicate privileged URLs and authenticates the application credential',{skip:!enabled},async()=>{
 const f=await testFixture();try {
  const original=f.env.REPOSITORY_DATABASE_URL;
  f.env.REPOSITORY_DATABASE_URL=f.env.MIGRATION_DATABASE_URL;
  assert.throws(()=>target.targetConfig(f.env),/migration_target_configuration/);
  f.env.REPOSITORY_DATABASE_URL=original;
  const wrong=new URL(original);wrong.password='invalid-fixture-password';f.env.REPOSITORY_DATABASE_URL=wrong.toString();
  await assert.rejects(target.targetPreflight(f.env,f.state),error=>error.message==='migration_target_unavailable');
  f.env.REPOSITORY_DATABASE_URL=original;await target.targetPreflight(f.env,f.state);
 }finally{await f.close();}
});
test('lease expiry during partial import releases SQLite and quarantines an inactive removed target',{skip:!enabled},async()=>{
 const f=await testFixture();try {
  await control.preflight(f.env,f.state);await migration.saveMigration(f.archive,f.state);
  let state=f.state;
  for(let n=0;n<500;n++){state=await migration.readMigration(f.archive,f.id);if(state.phase==='importing')break;await control.controllerStep(f.env,state,f.hooks);if(n===499)assert.fail('did not export');}
  await control.controllerStep(f.env,state,f.hooks); // one committed import page
  state=await migration.readMigration(f.archive,f.id);state.cutover.lease_until=1;await migration.saveMigration(f.archive,state);
  await f.admin.query(`DROP SCHEMA ${f.schema} CASCADE`);
  await control.controllerStep(f.env,await migration.readMigration(f.archive,f.id),f.hooks);
  const done=await migration.readMigration(f.archive,f.id);assert.equal(done.phase,'abandoned');assert.equal(f.released,true);
  assert.equal(done.cutover.abort_reason,'migration_freeze_lease_expired');assert.ok(done.cutover.target_quarantined);
 }finally{await f.close();}
});
test('a missing target after activation intent always requires forward recovery',{skip:!enabled},async()=>{
 const f=await testFixture();try {
  await control.preflight(f.env,f.state);await migration.saveMigration(f.archive,f.state);await control.controllerStep(f.env,f.state,f.hooks);
  const state=await migration.readMigration(f.archive,f.id);state.phase='switching';state.cutover.activation_intent=true;state.cutover.lease_until=1;await migration.saveMigration(f.archive,state);
  await f.admin.query(`DROP SCHEMA ${f.schema} CASCADE`);
  await assert.rejects(control.controllerStep(f.env,await migration.readMigration(f.archive,f.id),f.hooks),/migration_forward_recovery_required/);
  assert.equal(f.released,false);
 }finally{await f.close();}
});

test('legacy column order imports fields by archived names and preserves raw checksums',{skip:!enabled},async()=>{
 const f=await testFixture();try {
  f.db.exec("UPDATE jobs SET api='graphql',etag='original-etag',requested_at=1001,created_at=2002");
  const old=f.db.prepare('SELECT * FROM jobs').get(),ddl=f.db.prepare("SELECT sql FROM sqlite_master WHERE name='jobs'").get().sql;
  f.db.exec('ALTER TABLE jobs RENAME TO previous_jobs');
  f.db.exec(ddl.replace('api TEXT,etag TEXT','etag TEXT,api TEXT').replace('requested_at INTEGER,created_at INTEGER','created_at INTEGER,requested_at INTEGER'));
  const names=f.db.prepare('PRAGMA table_info(previous_jobs)').all().map(c=>c.name).join(',');
  f.db.exec(`INSERT INTO jobs(${names}) SELECT ${names} FROM previous_jobs`);f.db.exec('DROP TABLE previous_jobs');
  await control.preflight(f.env,f.state);await migration.saveMigration(f.archive,f.state);
  let state=f.state;
  for(let n=0;n<500;n++){state=await migration.readMigration(f.archive,f.id);if(state.phase==='preparing')break;await control.controllerStep(f.env,state,f.hooks);if(n===499)assert.fail('did not verify');}
  const job=(await f.admin.query(`SELECT api,etag,requested_at::text,created_at::text FROM ${f.schema}.jobs`)).rows[0];
  assert.deepEqual(job,{api:old.api,etag:old.etag,requested_at:String(old.requested_at),created_at:String(old.created_at)});
  assert.equal(state.cutover.verify.jobs.hash,state.tables.jobs.hash);
 }finally{await f.close();}
});

test('stored shadows reject real target differences and matching unavailable results',{skip:!enabled},async()=>{
 const f=await testFixture();try {
  const custom={id:f.id,name:'org/repo',installation:'200'};await f.source.setMeta('repository',custom);f.env.ALLOWED_REPOSITORIES=JSON.stringify([custom]);
  f.db.prepare('INSERT INTO resources(kind,id,data,observed,revision,tombstone) VALUES(?,?,?,?,?,0)').run('pr','7',JSON.stringify({number:7,state:'OPEN',title:'source title'}),123456,123);
  f.db.prepare('INSERT INTO collections VALUES(?,?,?,?,?,?,?)').run('open_prs:{}','open_prs','{}',1,123456,123,'etag');
  f.db.prepare('INSERT INTO members VALUES(?,?)').run('open_prs:{}','7');
  await control.preflight(f.env,f.state);await migration.saveMigration(f.archive,f.state);
  let state=f.state;
  for(let n=0;n<500;n++){state=await migration.readMigration(f.archive,f.id);if(state.phase==='switching')break;await control.controllerStep(f.env,state,f.hooks);if(n===499)assert.fail('did not verify');}
  f.source.migrationState=state;await f.source.shadowMigration(state);assert.equal(state.cutover.shadow.queries,1);
  await f.admin.query(`UPDATE ${f.schema}.resources SET data=$1 WHERE kind='pr' AND id='7'`,[JSON.stringify({number:7,state:'OPEN',title:'different target title'})]);
  state.cutover.shadow=undefined;await assert.rejects(f.source.shadowMigration(state),/migration_shadow_mismatch/);
  f.db.prepare('UPDATE collections SET complete=0 WHERE key=?').run('open_prs:{}');
  await f.admin.query(`UPDATE ${f.schema}.collections SET complete=0 WHERE key=$1`,['open_prs:{}']);
  state.cutover.shadow=undefined;await assert.rejects(f.source.shadowMigration(state),/migration_shadow_unavailable/);
  state.cutover.shadow=undefined;await migration.saveMigration(f.archive,state);
  f.source.assertMigrationFence=()=>{};
  f.source.shadowMigration=async function(){for(let n=0;n<901;n++)await this.env.ARCHIVE.head('budget-test');};
  const exhausted=await f.source.superviseMigration(1);assert.equal(exhausted.status,503);
  assert.equal((await migration.readMigration(f.archive,f.id)).failure,'migration_shadow_request_budget');

 }finally{await f.close();}
});

for(const when of ['before','after'])test(`restart around ${when} local PostgreSQL selection commit preserves exclusive ownership`,{skip:!enabled},async()=>{
 const f=await testFixture();try {
  const custom={id:f.id,name:'org/repo',installation:'200'};await f.source.setMeta('repository',custom);f.env.ALLOWED_REPOSITORIES=JSON.stringify([custom]);
  f.db.prepare('INSERT INTO resources(kind,id,data,observed,revision,tombstone) VALUES(?,?,?,?,?,0)').run('pr','7',JSON.stringify({number:7,state:'OPEN'}),123456,123);
  f.db.prepare('INSERT INTO collections VALUES(?,?,?,?,?,?,?)').run('open_prs:{}','open_prs','{}',1,123456,123,'etag');f.db.prepare('INSERT INTO members VALUES(?,?)').run('open_prs:{}','7');
  assert.equal((await f.command('start',{target:'neon-test'})).status,200);
  let interrupted=false;const exec=f.ctx.storage.sql.exec;
  f.ctx.storage.sql.exec=(sql,...bindings)=>{
    const marker=sql.startsWith("UPDATE meta SET value=? WHERE key='backend_owner'") && JSON.parse(bindings[0]).backend==='postgres';
    if(marker && !interrupted){interrupted=true;if(when==='after')exec(sql,...bindings);throw Error('selection interrupted');}
    return exec(sql,...bindings);
  };
  let failed;
  for(let n=0;n<50;n++){const response=await f.request('/migration-supervise',{}),body=await response.json();if(response.status===503){failed=body;break;}assert.equal(response.status,200,JSON.stringify(body));}
  assert.ok(interrupted);assert.equal(failed.phase,'switching');assert.equal(failed.cutover.target_active,true);
  assert.throws(()=>f.db.prepare("INSERT INTO demand VALUES('stale-worker',1)").run(),/generation fenced/);
  const cold=await fixture(f.db,f.archive,{...f.env});assert.equal(cold.state.store.backend,'postgres');
  const resumed=await (await cold.request('/migration-supervise',{})).json();assert.equal(resumed.phase,'replaying');
  assert.equal(cold.state.localMeta('backend_owner').backend,'postgres');
  const sourceRevision=f.db.prepare("SELECT value FROM meta WHERE key='revision'").get().value;
  assert.equal((await cold.state.meta('revision')),JSON.parse(sourceRevision));
 }finally{await f.close();}
});
