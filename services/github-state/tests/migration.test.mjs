import {test} from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {DatabaseSync} from 'node:sqlite';
import {runtime} from './runtime.mjs';
import {Miniflare,convertV4MiniflareOptions} from 'miniflare';
import {fixture,fill,finish,bucket,load,migration,RepositoryState,worker,repo,generation,target,sha,REQUIRED_TABLES} from './migration_fixture.mjs';

test('full fenced export contains every table, exact integers, timestamps, leases and R2 manifest; restart every step',async()=>{
 let f=await fixture();await fill(f);
 const before=f.db.prepare('SELECT * FROM jobs').get();
 const frozen=await f.command('freeze');assert.equal(frozen.status,200);assert.equal(frozen.body.bookmark,'authoritative-bookmark');
 for(let n=0;n<500;n++){
  const result=await f.command('step',{limit:2});assert.equal(result.status,200,JSON.stringify(result.body));
  if(result.body.phase==='source_verified')break;
  f=await fixture(f.db,f.archive);
  if(n===499)assert.fail('did not finish');
 }
 const receipt=(await f.command('status')).body;
 assert.equal(receipt.phase,'source_verified');
 assert.deepEqual(Object.keys(receipt.tables).sort(),[...REQUIRED_TABLES].sort());
 assert.ok(Object.values(receipt.tables).every(t=>t.verified && t.count===t.rows));
 assert.equal(receipt.blobs.objects,4,'short R2 list pages are all inventoried');
 const pages=[...f.archive.objects.entries()].filter(([key])=>key.includes('/demand/')).map(([,o])=>JSON.parse(o.text));
 const rows=pages.flatMap(p=>p.rows);
 assert.deepEqual(rows.find(r=>r[0]==='-9223372036854775808'),['-9223372036854775808',['text','negative'],['integer','9223372036854775807']]);
 assert.deepEqual(rows.find(r=>r[0]==='9223372036854775807'),['9223372036854775807',['text','positive'],['integer','-9223372036854775808']]);
 assert.deepEqual(f.db.prepare('SELECT * FROM jobs').get(),before,'export never resets cooldowns or leases');
 assert.equal(receipt.write_owner,'sqlite');assert.equal(receipt.production_cutover_supported,false);
 assert.ok(f.archive.objects.has('migrations/100/'+generation+'/receipt.json'));
});

test('freeze fences all mutation routes and alarm dispatch, while stored reads/304/cursors retain timestamps',async()=>{
 const f=await fixture();
 f.db.prepare('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)').run('pr','1','',JSON.stringify({number:1,state:'OPEN'}),'',123456,7,0);
 f.db.prepare('INSERT INTO collections VALUES(?,?,?,?,?,?,?)').run('open_prs:{}','open_prs','{}',1,123456,7,'etag');
 f.db.prepare('INSERT INTO members VALUES(?,?)').run('open_prs:{}','1');
 (await f.state.setMeta('revision',7));
 const page={version:1,repository:repo.name,operation:'open_prs',status:'ok',revision:7,complete:true,data:[{number:1}]};
 const cursorId='0b6c2f0e-1f7a-4b4c-9a1e-5b8d3c2a1f00';
 await f.archive.put('cursors/100/'+cursorId+'/100.json',JSON.stringify({repository:'100',key:'open_prs:{}',allow_observed:false,expires:Date.now()+60000,result:page}));
 await f.command('freeze');
 const sqlBefore=f.db.prepare('SELECT total_changes() n').get().n;
 for(const path of ['/repair','/tick','/activity-refresh','/configure','/storage/cleanup','/storage/probe','/storage/ingress','/event'])assert.equal((await f.request(path,{})).status,503,path);
 f.state.nextJob=()=>assert.fail('GitHub repair dispatch during freeze');
 await f.state.alarm();
 const query={repository:repo.name,operation:'open_prs'};
 const data=await (await f.request('/query',query)).json();assert.equal(data.status,'ok');assert.equal(data.observed_at,123456);assert.equal(data.verified_at,123456);
 assert.deepEqual(await (await f.request('/query',{...query,cursor:cursorId+':100'})).json(),page);
 const conditional=await f.state.fetch(new Request('https://internal/query',{method:'POST',headers:{'if-none-match':'"7"'},body:JSON.stringify(query)}));assert.equal(conditional.status,304);
 assert.equal(f.db.prepare('SELECT total_changes() n').get().n,sqlBefore,'no SQL mutation from reads or alarms');
 await assert.rejects(f.state.setMeta('forbidden',true),/migration_frozen/);
 assert.throws(()=>f.state.sql.exec("SELECT 1; UPDATE meta SET value='0' WHERE key='revision'"),/migration_frozen/);
});

test('in-flight event finishes before bookmark; new input parks even beyond retry horizon and survives retention',async()=>{
 const f=await fixture();
 let finishEvent,entered;
 const waiting=new Promise(r=>entered=r),gate=new Promise(r=>finishEvent=r);
 f.state.ingest=async()=>{entered();await gate;(await f.state.setMeta('revision',987));};
 const inflight=f.request('/event',{repository:repo.name});await waiting;
 let bookmarkRevision;
 f.ctx.storage.getCurrentBookmark=async()=>{bookmarkRevision=(await f.state.revision());return 'after-event';};
 const freezing=f.command('freeze');
 // Wait until durable barrier exists, without depending on a fixed timer.
 while(!f.archive.objects.has('control/100/migration.json'))await new Promise(r=>setImmediate(r));
 const body='{"zen":"during-freeze"}',key='webhooks/2000-01-01/during-freeze.json';
 await f.archive.put(key,body,{customMetadata:{sha256:sha(body),expires:'1'}});
 for(let attempts=1;attempts<=30;attempts++){
  let acked=false;
  await worker.queue({messages:[{body:{key,delivery:'during-freeze',event:'ping',repository:repo.name,received:1},attempts,ack(){acked=true;},retry(){assert.fail('spent queue retries during freeze');}}]},f.env);
  assert.equal(acked,true);
 }
 finishEvent();await inflight;
 assert.equal((await freezing).status,200);assert.equal(bookmarkRevision,987);
 const tasks=[];
 await worker.scheduled({},f.env,{waitUntil(p){tasks.push(p);}});await Promise.all(tasks);
 assert.ok(f.archive.objects.has(key),'raw input retention is pinned');
 assert.ok(f.archive.objects.has('parked/100/bodies/during-freeze.json'));
 assert.equal([...f.archive.objects.keys()].filter(k=>k.includes('/pointers/')).length,1);
});

test('production and wrong identity are rejected; no caller assertion enables cutover',async()=>{
 const f=await fixture();
 assert.equal((await f.command('freeze',{environment:'production',s0_passed:true,s1_passed:true})).status,400);
 assert.equal(f.archive.objects.size,0);
 assert.equal((await f.command('freeze',{apply:false})).body.error,'migration_apply_required');
 assert.equal((await f.command('freeze',{target:'postgresql://user:secret@host/db'})).body.error,'invalid_target_identity');
 assert.equal((await f.command('freeze')).status,200);
 assert.equal((await f.command('step',{target:'wrong-target'})).body.error,'migration_identity_conflict');
 assert.equal((await f.command('switch')).body.error,'migration_target_backend_unavailable');
 const restarted=await fixture(f.db,f.archive,{CF_VERSION:{id:'old-deployment'}});
 assert.equal((await restarted.command('step')).body.error,'migration_source_identity_changed');
 assert.equal((await restarted.request('/repair',{})).status,503);
});

test('unknown tables and unavailable authoritative bookmark fail closed',async()=>{
 const f=await fixture();f.db.exec('CREATE TABLE forgotten_state(id TEXT)');
 assert.equal((await f.command('freeze')).body.error,'migration_table_manifest_mismatch');
 assert.equal(f.archive.objects.size,0);
 f.db.exec('DROP TABLE forgotten_state');f.ctx.storage.getCurrentBookmark=async()=>null;
 assert.equal((await f.command('freeze')).body.error,'migration_bookmark_unavailable');
 assert.equal((await f.request('/repair',{})).status,503);
 f.ctx.storage.getCurrentBookmark=async()=>'recovered-bookmark';
 assert.equal((await f.command('freeze')).body.bookmark,'recovered-bookmark');
});

for(const when of ['before','after'])test(`restart around ${when}-commit export and validation checkpoint failures`,async()=>{
 let f=await fixture();await fill(f);await f.command('freeze');
 f.archive.fail((op,key,at)=>op==='put' && key.includes('/activity_landings/') && at===when);
 const failed=await f.command('step');assert.equal(failed.body.error,'migration_storage_failure');
 assert.ok(!JSON.stringify(failed).includes('sensitive'));
 f=await fixture(f.db,f.archive);
 // Interrupt after a page has been saved but while its control checkpoint is written.
 f.archive.fail((op,key,at)=>op==='put' && key==='control/100/migration.json' && at===when);
 assert.equal((await f.command('step')).status,503);
 f=await fixture(f.db,f.archive);
 while((await f.command('status')).body.phase==='exporting')assert.equal((await f.command('step',{limit:2})).status,200);
 f.archive.fail((op,key,at)=>op==='put' && key==='control/100/migration.json' && at===when);
 assert.equal((await f.command('step')).status,503);
 f=await fixture(f.db,f.archive);
 assert.equal((await finish(f)).phase,'source_verified');
});

test('verification rejects missing R2 references and changed/corrupt source pages; abandon retains archives',async()=>{
 const f=await fixture();await fill(f);await f.command('freeze');
 while((await f.command('status')).body.phase==='exporting')assert.equal((await f.command('step')).status,200);
 const key=[...f.archive.objects.keys()].find(k=>k.includes('/activity_landings/'));
 const saved=f.archive.objects.get(key);f.archive.objects.set(key,{...saved,text:'[]'});
 assert.equal((await f.command('step')).body.error,'migration_page_corrupt');
 f.archive.objects.set(key,saved);
 f.archive.objects.delete('resources/100/spill');
 let failed;
 for(let n=0;n<100;n++){failed=await f.command('step');if(failed.status!==200)break;}
 assert.equal(failed.body.error,'migration_blob_missing');
 await f.archive.put('resources/100/spill','{}');
 await finish(f);
 assert.equal((await f.command('abandon')).body.phase,'abandoned');
 assert.ok(f.archive.objects.has(key),'abandon never removes archives');
 assert.equal((await f.command('freeze')).body.error,'migration_generation_abandoned');
 (await f.state.setMeta('unfrozen',true));
});


test('public migration endpoint requires admin auth and never exposes stored rows',async()=>{
 const f=await fixture(undefined,undefined,{ADMIN_TOKEN_HASH:sha('admin'),CLIENT_TOKENS:JSON.stringify([{hash:sha('client'),repositories:[repo.name]}])});
 const call=token=>worker.fetch(new Request('https://service/admin/migration',{method:'POST',headers:token?{authorization:'Bearer '+token}:{},body:JSON.stringify({repository:repo.name,operation:'preflight'})}),f.env,{});
 assert.equal((await call()).status,401);
 assert.equal((await call('client')).status,401);
 const response=await call('admin');assert.equal(response.status,200);
 const data=await response.json();assert.equal(data.production_cutover_supported,false);assert.equal(data.source_export_environment,'test');
 assert.ok(data.blockers.includes('migration_target_configuration'));
 assert.equal(Object.hasOwn(data,'rows'),false);
});


test('final receipt interruption resumes without repeating export; a new generation requires explicit abandonment',async()=>{
 let f=await fixture();await f.command('freeze');
 while((await f.command('status')).body.phase!=='source_verified'){
  const status=(await f.command('status')).body;
  if(status.phase==='verifying' && Object.values(status.tables).every(t=>t.verified)){
   f.archive.fail((op,key,at)=>op==='put' && key.endsWith('/receipt.json') && at==='before');
   let failed;
   for(let n=0;n<10;n++){failed=await f.command('step');if(failed.status!==200)break;}
   assert.equal(failed.body.error,'migration_storage_failure');break;
  }
  assert.equal((await f.command('step')).status,200);
 }
 f=await fixture(f.db,f.archive);
 const complete=await f.command('step');assert.equal(complete.body.phase,'source_verified');
 assert.ok(f.archive.objects.has('migrations/100/'+generation+'/receipt.json'));
 assert.equal(JSON.parse(f.archive.objects.get('migrations/100/'+generation+'/receipt.json').text).failure,null);
 const next='c707ddba-076d-4a63-8696-96533031ce89';
 assert.equal((await f.command('freeze',{generation:next})).body.error,'migration_identity_conflict');
 await f.command('abandon');
 assert.equal((await f.command('freeze',{generation:next})).body.phase,'exporting');
 assert.ok(f.archive.objects.has('migrations/100/'+generation+'/abandoned.json'));
});


test('an initial barrier upload failure is retryable in the same process',async()=>{
 const f=await fixture();
 f.archive.fail((op,key,when)=>op==='put' && key==='control/100/migration.json' && when==='before');
 assert.equal((await f.command('freeze')).body.error,'migration_storage_failure');
 assert.equal((await f.request('/repair',{})).status,503);
 assert.equal((await f.command('freeze')).body.phase,'exporting');
 assert.equal((await finish(f)).phase,'source_verified');
});


test('a cold migration-control outage fences writes but keeps complete stored SQL reads available',async()=>{
 let f=await fixture();
 f.db.prepare('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)').run('pr','1','',JSON.stringify({number:1,state:'OPEN'}),'',123456,7,0);
 f.db.prepare('INSERT INTO collections VALUES(?,?,?,?,?,?,?)').run('open_prs:{}','open_prs','{}',1,123456,7,'etag');
 f.db.prepare('INSERT INTO members VALUES(?,?)').run('open_prs:{}','1');
 f.archive.fail((op,key)=>op==='get' && key==='control/100/migration.json');
 f=await fixture(f.db,f.archive);
 const query=await (await f.request('/query',{repository:repo.name,operation:'open_prs'})).json();
 assert.equal(query.status,'ok');assert.equal(query.observed_at,123456);
 assert.equal((await f.request('/repair',{})).status,503);
 assert.equal((await (await f.request('/health',{})).json()).storage.mutation_fence,'control_unavailable');
 await assert.rejects(f.state.setMeta('blocked',true),/migration_frozen/);
 assert.equal((await f.request('/tick',{})).status,200,'cron automatically retries the recovered control store');
 (await f.state.setMeta('recovered',true));
});


test('an abandoned generation cannot overwrite its retained archive after another generation',async()=>{
 const f=await fixture();await f.command('freeze');await finish(f);await f.command('abandon');
 const key='migrations/100/'+generation+'/receipt.json',receipt=f.archive.objects.get(key).text;
 const next='c707ddba-076d-4a63-8696-96533031ce89';
 await f.command('freeze',{generation:next});await f.command('abandon',{generation:next});
 assert.equal((await f.command('freeze')).body.error,'migration_generation_used');
 assert.equal(f.archive.objects.get(key).text,receipt);
});

test('stale checkpoint writers cannot overwrite a newer control record',async()=>{
 const f=await fixture();await f.command('freeze');
 const a=await migration.readMigration(f.archive,'100'),b=await migration.readMigration(f.archive,'100');
 a.failure='first-writer';await migration.saveMigration(f.archive,a);
 b.failure='stale-writer';
 await assert.rejects(migration.saveMigration(f.archive,b),/migration_checkpoint_conflict/);
 assert.equal((await migration.readMigration(f.archive,'100')).failure,'first-writer');
});

test('noninitial export statements seek directly into the rowid index',async()=>{
 const f=await fixture();await fill(f);
 const queries=[];
 const sql={exec(query,...bindings){queries.push({query,bindings});return f.ctx.storage.sql.exec(query,...bindings);}};
 const columns=migration.schema(f.ctx.storage.sql,'demand');
 const rows=migration.sourceRows(sql,'demand',columns,'-1',2);assert.equal(rows.length,2);
 for(const {query,bindings} of queries){
  const plan=f.db.prepare('EXPLAIN QUERY PLAN '+query).all(...bindings).map(r=>r.detail).join(' ');
  assert.match(plan,/SEARCH .* USING INTEGER PRIMARY KEY/);
  assert.doesNotMatch(plan,/SCAN/);
 }
});

test('expired staging records an importer recovery requirement without mutating source jobs',async()=>{
 const f=await fixture();await fill(f);await f.command('freeze');
 f.archive.objects.delete('staging/100/fixture/12345/2');
 const before=f.db.prepare('SELECT * FROM jobs').get();
 const result=await finish(f);assert.equal(result.staging.checked,1);assert.equal(result.staging.expired_jobs,1);
 assert.deepEqual(f.db.prepare('SELECT * FROM jobs').get(),before);
 const key=[...f.archive.objects.keys()].find(k=>k.includes('/expired-staging/'));
 const receipt=JSON.parse(f.archive.objects.get(key).text);
 assert.equal(receipt.reason,'staging_expired');
 assert.equal(receipt.required_import_action,'reset_pagination_preserve_cooldown_and_generation');
 assert.equal(receipt.first_missing_page,'2');
});

test('production startup and ingress do not depend on test migration control reads',async()=>{
 let f=await fixture();
 // A permanent outage of the test-only control key must have no production effect.
 const get=f.archive.get.bind(f.archive);
 f.archive.get=async key=>{if(key==='control/100/migration.json')assert.fail('production touched the test migration control');return get(key);};
 f=await fixture(f.db,f.archive,{MIGRATION_ENVIRONMENT:undefined});
 assert.equal((await f.request('/tick',{})).status,200);
 assert.equal((await f.state.storageState()).mutation_fence,null);
 const {ingressMode}=await load('src/ingress.ts');
 assert.equal((await ingressMode(f.env,repo)).mode,'apply');
});


test('typed cells round-trip REAL extremes, NULL and BLOB values',async()=>{
 const f=await fixture();f.db.exec('CREATE TABLE scalar_values(value)');
 const values=[0.1,Number.MIN_VALUE,Infinity,-Infinity,-0,1,Math.PI,Number.MAX_VALUE];
 for(const value of values)f.db.prepare('INSERT INTO scalar_values VALUES(?)').run(value);
 f.db.prepare('INSERT INTO scalar_values VALUES(?)').run(null);
 f.db.prepare('INSERT INTO scalar_values VALUES(?)').run(Buffer.from([0,255,128]));
 const rows=migration.sourceRows(f.ctx.storage.sql,'scalar_values',migration.schema(f.ctx.storage.sql,'scalar_values'),null,100);
 const source=f.db.prepare('SELECT value,typeof(value) type FROM scalar_values ORDER BY rowid').all();
 for(let i=0;i<values.length;i++){
  assert.equal(rows[i][1][0],source[i].type);
  const encoded=rows[i][1][1],decoded=encoded==='Inf'?Infinity:encoded==='-Inf'?-Infinity:Number(encoded);
  assert.ok(decoded===source[i].value,`${source[i].value} failed round-trip through ${encoded}`);
 }
 assert.deepEqual(rows.at(-2)[1],['null',null]);
 assert.deepEqual(rows.at(-1)[1],['blob','00FF80']);
});


test('real R2 conditional writes enforce checkpoint and generation ownership',async()=>{
 const f=await fixture();await f.command('freeze');
 const state=await migration.readMigration(f.archive,'100');delete state.checkpoint_etag;
 const mf=await runtime(()=>assert.fail('migration called GitHub'));
 try{
  const archive=await mf.getR2Bucket('ARCHIVE');
  await migration.saveMigration(archive,state);
  const first=await migration.readMigration(archive,'100'),stale=await migration.readMigration(archive,'100');
  first.failure='newer';await migration.saveMigration(archive,first);
  stale.failure='stale';await assert.rejects(migration.saveMigration(archive,stale),/migration_checkpoint_conflict/);
  const key=migration.archiveKey('100',generation)+'/generation.json';
  assert.ok(await archive.put(key,'{}',{onlyIf:{etagDoesNotMatch:'*'}}));
  assert.equal(await archive.put(key,'replace',{onlyIf:{etagDoesNotMatch:'*'}}),null);
 }finally{await mf.dispose();}
});


test('a planned source freeze retains degraded stored reads with their original timestamps',async()=>{
 const f=await fixture();
 f.db.prepare('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)').run('pr','1','',JSON.stringify({number:1,state:'OPEN'}),'',123456,7,0);
 f.db.prepare('INSERT INTO collections VALUES(?,?,?,?,?,?,?)').run('open_prs:{}','open_prs','{}',1,123456,7,'etag');
 f.db.prepare('INSERT INTO members VALUES(?,?)').run('open_prs:{}','1');
 f.state.degraded={since:1,reason:'storage_full',operation:'test'};
 const query={repository:repo.name,operation:'open_prs'};
 assert.equal((await (await f.request('/query',query)).json()).reason,'mirror_frozen');
 await f.command('freeze');
 const result=await (await f.request('/query',query)).json();
 assert.equal(result.status,'ok');assert.equal(result.observed_at,123456);assert.equal(result.verified_at,123456);
});


test('a new deployment can abandon the same source object while export remains fenced',async()=>{
 let f=await fixture();await fill(f);await f.command('freeze');await f.command('step');
 f=await fixture(f.db,f.archive,{CF_VERSION:{id:'new-deployment'}});
 assert.equal((await f.command('step')).body.error,'migration_source_identity_changed');
 assert.equal((await f.command('abandon')).body.phase,'abandoned');
 assert.notEqual((await f.request('/repair',{})).status,503);
 assert.ok([...f.archive.objects.keys()].some(k=>k.includes('/activity_landings/')));
 assert.ok(f.archive.objects.has('migrations/100/'+generation+'/abandoned.json'));
});

test('a failed new intent cannot abandon an older generation loaded from R2',async()=>{
 const f=await fixture(),old='c707ddba-076d-4a63-8696-96533031ce89';
 await f.command('freeze',{generation:old});await f.command('abandon',{generation:old});
 let failed=false;
 const get=f.archive.get.bind(f.archive);
 f.archive.get=async key=>{if(failed && key==='control/100/migration.json'){failed=false;throw new Error('R2 unavailable');}return get(key);};
 f.archive.fail((op,key,when)=>{if(op==='put' && key==='control/100/migration.json' && when==='before'){failed=true;return true;}return false;});
 assert.equal((await f.command('freeze')).status,503);
 assert.equal((await f.command('abandon')).body.error,'migration_identity_conflict');
 assert.equal(f.archive.objects.has('migrations/100/'+generation+'/abandoned.json'),false);
});

test('legacy initialization resumes automatically after control reads recover',async()=>{
 let f=await fixture();f.db.exec("DELETE FROM meta WHERE key='schema_version'");
 f.archive.fail((op,key)=>op==='get' && key==='control/100/migration.json');
 f=await fixture(f.db,f.archive);
 assert.equal(f.state.schema,'migration_pending');
 assert.equal((await f.request('/tick',{})).status,200);
 assert.equal(f.state.schema,'current');assert.equal((await f.state.meta('schema_version')),5);
});


test('workerd SQLite REAL encoding matches the archive round-trip contract',async()=>{
 const source=`
 import {DurableObject} from 'cloudflare:workers';
 import {sourceRows,schema} from './src/migration';
 export class Scalars extends DurableObject {
  async fetch(){
   const sql=this.ctx.storage.sql;
   sql.exec('CREATE TABLE scalar_values(value)');
   for(const literal of ['0.1','5e-324','9e999','-9e999','-0.0','1.0','3.141592653589793','1.7976931348623157e308'])sql.exec('INSERT INTO scalar_values VALUES('+literal+')');
   const rows=sourceRows(sql,'scalar_values',schema(sql,'scalar_values'),null,100);
   const original=sql.exec('SELECT value FROM scalar_values ORDER BY rowid').toArray();
   for(let i=0;i<rows.length;i++){
    const text=rows[i][1][1],decoded=text==='Inf'?Infinity:text==='-Inf'?-Infinity:Number(text);
    if(decoded!==original[i].value)throw new Error('REAL did not round-trip');
   }
   return Response.json({rows});
  }
 }
 export default {fetch(req,env){return env.SCALARS.get(env.SCALARS.idFromName('values')).fetch(req);}};
 `;
 const built=await build({stdin:{contents:source,resolveDir:process.cwd(),loader:'ts'},bundle:true,format:'esm',write:false,external:['cloudflare:workers']});
 const mf=new Miniflare(convertV4MiniflareOptions({modules:true,script:built.outputFiles[0].text,compatibilityDate:'2026-10-11',durableObjects:{SCALARS:{className:'Scalars',useSQLite:true}}}));
 try{
  const response=await mf.dispatchFetch('https://fixture/');assert.equal(response.status,200);
  const {rows}=await response.json();
  assert.deepEqual(rows[2][1],['real','Inf']);assert.deepEqual(rows[3][1],['real','-Inf']);
  assert.deepEqual(rows[4][1],['real','0.0']);assert.deepEqual(rows[5][1],['real','1.0']);
 }finally{await mf.dispose();}
});


test('table discovery only excludes literal engine prefixes',async()=>{
 const f=await fixture();
 for(const name of ['scf_extra','sqliteXstate']){
  f.db.exec('CREATE TABLE '+name+'(id TEXT)');
  assert.equal((await f.command('freeze')).body.error,'migration_table_manifest_mismatch');
  assert.equal(f.archive.objects.size,0);
  f.db.exec('DROP TABLE '+name);
 }
});


test('source migration inventories and verifies the expiring staging-spill prefix',async()=>{
 const f=await fixture(),key='staging-spills/100/nested';
 await f.archive.put(key,'{"rows":[]}',{customMetadata:{kind:'cursor',expires:'1'}});
 f.db.prepare('INSERT INTO jobs(key,query,due,nested) VALUES(?,?,?,?)').run('review_threads:{"number":7}',JSON.stringify({operation:'review_threads',args:{number:7}}),0,JSON.stringify({$r2:key}));
 assert.equal((await f.command('freeze')).status,200);
 const result=await finish(f);assert.equal(result.blobs.objects,1);
 assert.ok([...f.archive.objects.entries()].some(([name,object])=>name.includes('/blobs/') && object.text.includes(key)));
});

test('source export verifies immutable staging references as well as legacy page addresses',async()=>{
 const f=await fixture();
 const q={repository:repo.name,operation:'files',args:{number:7}};await f.state.enqueue(q);const job=await f.state.claim(await f.state.nextJob());
 await f.state.refresh(job,{api:'core',data:[{filename:'first'}],cursor:'second'});
 const checkpoint=await f.state.meta('staging_pages:'+job.key);assert.equal(checkpoint.version,1);
 assert.ok(f.archive.objects.has(checkpoint.pages['1'].key));assert.equal(f.archive.objects.has(`staging/${repo.id}/${encodeURIComponent(job.key)}/${job.started}/1`),false);
 const frozen=await f.command('freeze');assert.equal(frozen.status,200);const receipt=await finish(f);
 assert.equal(receipt.staging.expired_jobs,0);assert.equal(receipt.staging.checked,1);assert.equal(receipt.phase,'source_verified');
});

test('supervised start requires deployed evidence before source writes or GitHub dispatch',async()=>{
 const f=await fixture();
 const before=f.db.prepare('SELECT total_changes() n').get().n;
 const result=await f.command('start');assert.equal(result.body.error,'migration_acceptance_missing');
 assert.equal(f.db.prepare('SELECT total_changes() n').get().n,before);
 assert.equal(f.archive.objects.size,0);
 assert.equal((await f.command('start',{environment:'production'})).body.error,'migration_environment_mismatch');
 const receipt={version:1,environment:'test',repository:'100',target,deployment:'old-deployment',source_object:'source-object',valid_until:Date.now()+100000};
 await f.archive.put('control/100/migration-acceptance.json',JSON.stringify(receipt));
 assert.equal((await f.command('start')).body.error,'migration_acceptance_stale');
 assert.equal(f.db.prepare('SELECT total_changes() n').get().n,before);
});

test('installation dispatch tracks whole requests across the freeze and rejects newly arriving reference reads',async()=>{
 const f=await fixture(),{InstallationScheduler}=await load('src/installation.ts');
 const kv=new Map();f.ctx.storage.get=async key=>kv.get(key);f.ctx.storage.put=async(key,value)=>kv.set(key,value);
 const installation=new InstallationScheduler(f.ctx,f.env);
 let entered,finish;const ready=new Promise(resolve=>entered=resolve),gate=new Promise(resolve=>finish=resolve);
 installation.read=async()=>{entered();await gate;return Response.json({ok:true});};
 const request=()=>installation.fetch(new Request('https://internal/read',{method:'POST',body:JSON.stringify({repository:repo,query:{operation:'open_prs'}})}));
 const old=request();await ready;
 const health=()=>installation.fetch(new Request('https://internal/health')).then(r=>r.json());
 assert.deepEqual((await health()).repository_inflight,[{repository:repo.id,requests:1}]);
 const frozen={version:1,generation,repository:repo.id,target,source:{object:'source-object',deployment:'test-deployment',schema:5},phase:'freezing',created:Date.now(),updated:Date.now(),tables:{},failure:null,blobs:{},staging:{}};
 await f.archive.put(migration.migrationKey(repo.id),JSON.stringify(frozen));
 const refused=await request();assert.equal(refused.status,503);assert.equal((await refused.json()).reason,'migration_frozen');
 assert.equal((await health()).repository_inflight[0].requests,1);
 finish();await old;assert.deepEqual((await health()).repository_inflight,[]);
});
