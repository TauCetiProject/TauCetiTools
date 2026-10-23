import {bucket} from './r2_fixture.mjs';
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {DatabaseSync} from 'node:sqlite';
import {build} from 'esbuild';

const shim={name:'fixture',setup(b){b.onResolve({filter:/^cloudflare:workers$/},()=>({path:'context',namespace:'fixture'}));b.onLoad({filter:/.*/,namespace:'fixture'},()=>({contents:'export class DurableObject {constructor(ctx,env){this.ctx=ctx;this.env=env}}',loader:'js'}));}};
const load=async entry=>import('data:text/javascript;base64,'+Buffer.from((await build({entryPoints:[entry],bundle:true,platform:'node',format:'esm',write:false,plugins:[shim]})).outputFiles[0].text).toString('base64'));
const worker=(await load('src/index.ts')).default;
const {RepositoryState}=await load('src/repository.ts');
const storage=await load('src/storage.ts');
const repo={id:'100',name:'org/repo',installation:'200'};
const sha=bytes=>createHash('sha256').update(bytes).digest('hex');

// R2 double with digest checks, prefix listing and injectable failures.
// Repository double: deduplicates by delivery ID like the real receipt table.
function harness(){
 const archive=bucket(),state={full:false,applied:new Map(),error:null,throws:false};
 const repository={async fetch(url,init){
  if(state.throws) throw new Error('network connection lost');
  const pointer=JSON.parse(init.body);
  if(state.applied.has(pointer.delivery)) return Response.json({ok:true});
  if(state.full) return Response.json({error:'storage_full',parkable:true},{status:503});
  if(state.error) return Response.json({error:state.error,parkable:false},{status:500});
  const body=await archive.get(pointer.key);
  if(!body) return Response.json({error:'internal',message:'event body unavailable'},{status:500});
  state.applied.set(pointer.delivery,(state.applied.get(pointer.delivery) || 0)+1);
  return Response.json({ok:true});
 }};
 const env={ALLOWED_REPOSITORIES:JSON.stringify([repo]),ARCHIVE:archive,REPOSITORIES:{idFromName:x=>x,get:()=>repository}};
 return {archive,state,env};
}
async function deliver(env,delivery,attempts=1){
 const key='webhooks/2026-10-20/'+delivery+'.json',text=JSON.stringify({delivery});
 if(!env.ARCHIVE.objects.has(key)) await env.ARCHIVE.put(key,text,{customMetadata:{sha256:sha(Buffer.from(text)),event:'issue_comment'}});
 const message={body:{key,delivery,event:'issue_comment',repository:repo.name,received:1792000000000},attempts,acked:false,retried:null,ack(){this.acked=true;},retry(options){this.retried=options;}};
 await worker.queue({messages:[message]},env);
 return message;
}
const parked=(archive,delivery)=>[...archive.objects.keys()].filter(k=>k.startsWith('parked/100/pointers/') && k.endsWith('-'+delivery+'.json'));
// Every acknowledged event has a committed receipt or a durable pointer and pinned body.
function durable({archive,state},delivery){
 if(state.applied.has(delivery)) return true;
 const [pointer]=parked(archive,delivery);
 return Boolean(pointer && archive.objects.has(JSON.parse(archive.objects.get(pointer).bytes).key));
}

test('a full repository parks deliveries instead of spending queue retries',async()=>{
 const h=harness();h.state.full=true;
 const message=await deliver(h.env,'d1');
 assert.equal(message.acked,true);assert.equal(message.retried,null);
 const pointer=JSON.parse(h.archive.objects.get(parked(h.archive,'d1')[0]).bytes);
 assert.equal(pointer.original_key,'webhooks/2026-10-20/d1.json');assert.equal(pointer.received,1792000000000);assert.equal(pointer.event,'issue_comment');
 assert.equal(sha(h.archive.objects.get(pointer.key).bytes),pointer.sha256,'pinned body verified');
 assert.equal(h.archive.objects.get(pointer.key).customMetadata.expires,undefined,'normal retention never expires parked bodies');
 // Duplicate delivery and redelivery after a crash before ack are idempotent.
 assert.equal((await deliver(h.env,'d1')).acked,true);
 assert.equal(parked(h.archive,'d1').length,1);
});

test('R2 failure while parking retries with bounded backoff and never acknowledges',async()=>{
 const h=harness();h.state.full=true;
 h.archive.failures.push((op,key)=>op==='put' && key.includes('/pointers/'));
 const first=await deliver(h.env,'d2');
 assert.equal(first.acked,false);assert.deepEqual(first.retried,{delaySeconds:5});
 assert.equal(parked(h.archive,'d2').length,0,'crash before pointer: nothing claims durability');
 const second=await deliver(h.env,'d2',2);
 assert.equal(second.acked,true);assert.ok(durable(h,'d2'));
 h.archive.failures.push((op,key)=>op==='put' && key.includes('/bodies/'));
 const third=await deliver(h.env,'d3',30);
 assert.equal(third.acked,false);assert.deepEqual(third.retried,{delaySeconds:600});
});

test('ordinary failures back off, then park before the queue retry budget is spent',async()=>{
 const h=harness();h.state.error='internal';
 const early=await deliver(h.env,'d4',1);
 assert.equal(early.acked,false);assert.deepEqual(early.retried,{delaySeconds:5});
 const later=await deliver(h.env,'d4',10);
 assert.equal(later.acked,true);assert.ok(durable(h,'d4'));
 h.state.error=null;h.state.throws=true;
 assert.deepEqual((await deliver(h.env,'d5',3)).retried,{delaySeconds:20});
 assert.equal((await deliver(h.env,'d5',12)).acked,true);assert.ok(durable(h,'d5'));
});

test('replay removes pointers only after the receipt commits; crashes repeat harmlessly',async()=>{
 const h=harness();h.state.full=true;
 for(const d of ['r1','r2','r3']) await deliver(h.env,d);
 h.state.full=false;
 // Crash after application, before cleanup (r1), and between body and
 // pointer removal (r2): both leave receipt-backed pointers that deduplicate.
 h.archive.failures.push((op,key)=>op==='delete' && key.includes('/bodies/r1'));
 h.archive.failures.push((op,key)=>op==='delete' && key.includes('/pointers/') && key.endsWith('-r2.json'));
 const {drain}=await load('src/ingress.ts');
 assert.equal((await drain(h.env,repo)).failed,2);
 assert.equal(h.state.applied.get('r1'),1);assert.equal(parked(h.archive,'r1').length,1);
 assert.equal(h.state.applied.get('r2'),1);assert.equal(parked(h.archive,'r2').length,1);
 for(const key of parked(h.archive,'r1').concat(parked(h.archive,'r2'))){const p=JSON.parse(h.archive.objects.get(key).bytes);p.retry_at=0;await h.archive.put(key,JSON.stringify(p));}
 const result=await drain(h.env,repo);
 assert.equal(result.replayed,2);
 for(const d of ['r1','r2','r3']) {assert.equal(h.state.applied.get(d),1,d+' applied once');assert.equal(parked(h.archive,d).length,0);}
 assert.equal([...h.archive.objects.keys()].filter(k=>k.startsWith('parked/')).length,0);
 // A redelivered message after parking applies directly; its pointer then deduplicates.
 h.state.full=true;await deliver(h.env,'r4');h.state.full=false;
 await h.archive.put('control/100/storage.json',JSON.stringify({state:'writable',verified_at:Date.now()}));
 assert.equal((await deliver(h.env,'r4')).acked,true);
 assert.equal((await drain(h.env,repo)).replayed,0);assert.equal(h.state.applied.get('r4'),1);
});

test('a persistently failing parked event is held aside without blocking newer events',async()=>{
 const h=harness();h.state.full=true;
 await deliver(h.env,'bad');
 h.state.full=false;
 const {drain,backlog,maintainIngress}=await load('src/ingress.ts');
 h.archive.objects.delete(JSON.parse(h.archive.objects.get(parked(h.archive,'bad')[0]).bytes).key);
 for(let i=0;i<5;i++){for(const key of parked(h.archive,'bad')){const p=JSON.parse(h.archive.objects.get(key).bytes);p.retry_at=0;await h.archive.put(key,JSON.stringify(p));}await drain(h.env,repo);}
 await maintainIngress(h.env,repo);
 const report=await backlog(h.env,repo);
 assert.equal(report.parked.count,0);assert.equal(report.held.count,1);
 h.state.full=true;await deliver(h.env,'good');h.state.full=false;
 assert.equal((await drain(h.env,repo)).replayed,1);assert.equal(h.state.applied.get('good'),1);
});

// Repository harness over node:sqlite with an R2 double.
function repositoryFixture(env={},size=0,db=new DatabaseSync(':memory:'),archive=bucket()){
 let alarm=null,ready;
 const ctx={blockConcurrencyWhile:fn=>(ready=fn()),id:{toString:()=>'fixture'},storage:{sql:{exec(sql,...bindings){let rows=[],written=0;if(!bindings.length && sql.includes(';'))db.exec(sql);else{const st=db.prepare(sql);if(st.columns().length)rows=st.all(...bindings);else written=Number(st.run(...bindings).changes);}return {rowsWritten:written,toArray:()=>rows,one:()=>rows[0]};},get databaseSize(){return size;}},
  async transaction(fn){db.exec('SAVEPOINT async_tx');try{const result=await fn();db.exec('RELEASE async_tx');return result;}catch(error){db.exec('ROLLBACK TO async_tx');db.exec('RELEASE async_tx');throw error;}},transactionSync(fn){db.exec('SAVEPOINT tx');try{const v=fn();db.exec('RELEASE tx');return v;}catch(e){db.exec('ROLLBACK TO tx');db.exec('RELEASE tx');throw e;}},sync:async()=>{},getAlarm:async()=>alarm,setAlarm:async v=>{alarm=v;}}};
 const state=new RepositoryState(ctx,{ALLOWED_REPOSITORIES:JSON.stringify([repo]),ARCHIVE:archive,...env});
 if(!size) state.localSetMeta('repository',repo);
 state.fixtureReady=ready;
 return {db,state,archive,ready};
}
test('receipt lookups stay outside the writer and do not block unrelated mirror reads',async()=>{
 const f=repositoryFixture();await f.ready;
 const key='fixture/slow-receipt';await f.archive.put(key,JSON.stringify({issue:{number:7},comment:{id:42,body:'hello'}}));
 let release,entered;const gate=new Promise(r=>release=r),ready=new Promise(r=>entered=r);
 const head=f.archive.head;let heads=0;
 let writing=false;const atomic=f.state.atomic.bind(f.state);
 f.state.atomic=apply=>atomic(async state=>{writing=true;try{return await apply(state);}finally{writing=false;}});
 f.archive.head=async key=>{if(key.startsWith('receipts/')){assert.equal(writing,false,'receipt I/O must not hold the writer');heads++;entered();await gate;}return head(key);};
 const event=f.state.ingest({repository:repo.name,event:'issue_comment',delivery:'slow-receipt',key,received:Date.now()});
 await ready;let timer;
 try {
  const read=f.state.store.exec("SELECT value FROM meta WHERE key='repository'");
  const result=await Promise.race([read,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('receipt I/O blocked a local read')),1000);})]);
  assert.equal(result.toArray().length,1);
 }finally{clearTimeout(timer);release();}
 await event;assert.equal(heads,1,'only one receipt lookup is needed, before acquiring the writer');
});

test('SQL-first receipt lookup deduplicates while archival moves a receipt to R2',async()=>{
 const f=repositoryFixture();await f.ready;
 const delivery='archive-race',key='fixture/archive-race';
 await f.archive.put(key,JSON.stringify({issue:{number:7},comment:{id:42,body:'must not replay'}}));
 f.db.prepare('INSERT INTO deliveries VALUES(?,?,?)').run(delivery,'issue_comment',Date.now()-30*86400000);
 let heads=0;const head=f.archive.head;
 f.archive.head=async key=>{heads++;return head(key);};
 const pointer={repository:repo.name,event:'issue_comment',delivery,key,received:Date.now()};
 await f.state.ingest(pointer);
 assert.equal(heads,0,'a SQL receipt needs no remote lookup');
 assert.equal(f.db.prepare('SELECT COUNT(*) n FROM resources').get().n,0);
 // Archival publishes R2 before deleting SQL. After deletion the same event
 // is still rejected by the one pre-transaction remote check.
 await f.archive.put('receipts/100/'+delivery+'.json','{}');
 f.db.prepare('DELETE FROM deliveries WHERE id=?').run(delivery);
 await f.state.ingest(pointer);assert.equal(heads,1);
 assert.equal(f.db.prepare('SELECT COUNT(*) n FROM resources').get().n,0);
});

test('a concurrent ingest receipt remains retained even for an old parked event',async()=>{
 const f=repositoryFixture();await f.ready;
 const pointer={repository:repo.name,event:'issue_comment',delivery:'concurrent-old',key:'fixture/concurrent-old',received:Date.now()-30*86400000};
 await f.archive.put(pointer.key,JSON.stringify({issue:{number:7},comment:{id:42,body:'must not apply twice'}}));
 f.state.storageLoaded=true;f.state.retentionPolicy={mode:'enforce',disabled:[],pinned:[]};
 const head=f.archive.head;let raced=false;
 f.archive.head=async key=>{
  const value=await head(key);
  if(key.startsWith('receipts/') && !raced){
   raced=true;
   await f.state.atomic(state=>state.commitReceipt(pointer));
   await f.state.archiveDeliveryReceipts();
  }
  return value;
 };
 await f.state.ingest(pointer);
 assert.equal(f.db.prepare('SELECT COUNT(*) n FROM deliveries').get().n,1);
 assert.equal(f.db.prepare('SELECT COUNT(*) n FROM resources').get().n,0);
 assert.equal(await f.state.meta('review_coordination'),undefined);
});
const queryFixture=(state,q)=>state.fetch(new Request('https://internal/query',{method:'POST',body:JSON.stringify({repository:'org/repo',...q})})).then(r=>r.json());

test('R2 cursors validate repository, query and expiry',async()=>{
 const {state,archive}=repositoryFixture();
 const page={version:1,repository:'org/repo',operation:'open_prs',status:'ok',revision:3,complete:true,data:[{number:1}]};
 const key='open_prs:{}',now=Date.now(),id='0b6c2f0e-1f7a-4b4c-9a1e-5b8d3c2a1f00';
 await archive.put('cursors/100/'+id+'/100.json',JSON.stringify({repository:'100',key,allow_observed:false,expires:now+60000,result:page}));
 await archive.put('cursors/100/'+id+'/200.json',JSON.stringify({repository:'100',key,allow_observed:false,expires:now-1,result:page}));
 await archive.put('cursors/100/'+id+'/300.json',JSON.stringify({repository:'999',key,allow_observed:false,expires:now+60000,result:page}));
 assert.deepEqual(await queryFixture(state,{operation:'open_prs',cursor:id+':100'}),page);
 assert.equal((await queryFixture(state,{operation:'open_issues',cursor:id+':100'})).reason,'cursor_query_mismatch');
 assert.equal((await queryFixture(state,{operation:'open_prs',cursor:id+':200'})).reason,'cursor_expired');
 assert.equal((await queryFixture(state,{operation:'open_prs',cursor:id+':300'})).reason,'cursor_expired','another repository cannot use the cursor');
 for(const cursor of ['../../etc:1','legacy:100',id+':100:extra']) assert.equal((await queryFixture(state,{operation:'open_prs',cursor})).reason,'cursor_expired',cursor);
 // CI operations are not mirrored: even a stored continuation is not served.
 assert.equal((await queryFixture(state,{operation:'workflow_jobs',args:{id:'9'},allow_observed:true,cursor:id+':100'})).reason,'not_mirrored');
});

test('expiry runs before repair and hydration, survives their failure, and keeps semantic data',async()=>{
 const {db,state}=repositoryFixture();
 const now=Date.now();
 db.prepare('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)').run('pr','1','1',JSON.stringify({number:1,state:'OPEN'}),'',now-90*86400000,1,0);
 db.prepare('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)').run('issue_comments','5','1',JSON.stringify({id:5}),'',now-90*86400000,2,1);
 db.prepare('INSERT INTO collections VALUES(?,?,?,?,?,?,?)').run('open_prs:{}','open_prs','{}',1,now-90*86400000,1,null);
 db.prepare('INSERT INTO members VALUES(?,?)').run('open_prs:{}','1');
 db.prepare('INSERT INTO deliveries VALUES(?,?,?)').run('recent','push',now-86400000);
 for(let i=0;i<60;i++) db.prepare('INSERT INTO retired_objects VALUES(?,?)').run('c'+i,now-31*86400000);
 db.prepare('INSERT INTO retired_objects VALUES(?,?)').run('live',now);
 db.prepare('INSERT INTO repair_metrics VALUES(?,?,?,?,?)').run(now-8*86400000,'page','active','pr',1);
 db.prepare('INSERT INTO jobs(key,query,priority,due,lane) VALUES(?,?,?,?,?)').run('pr:{"number":1}',JSON.stringify({repository:'org/repo',operation:'pr',args:{number:1}}),5,0,'active');
 state.nextJob=()=>{throw new Error('repair dispatch failed');};
 state.hydrateSurvey=async()=>{throw new Error('hydration failed');};
 state.invalidateSurvey=()=>{throw new Error('invalidation failed');};
 await state.fixtureReady;
 await state.alarm();
 assert.equal(db.prepare('SELECT COUNT(*) n FROM retired_objects').get().n,1,'the bounded expiry batch ran');
 assert.equal(db.prepare('SELECT COUNT(*) n FROM repair_metrics').get().n,0);
 await state.fixtureReady;
 await state.alarm();
 assert.deepEqual(db.prepare('SELECT key id FROM retired_objects').all().map(r=>r.id),['live']);
 assert.equal(db.prepare('SELECT COUNT(*) n FROM resources').get().n,2,'resources and tombstones remain');
 assert.equal(db.prepare('SELECT complete FROM collections').get().complete,1,'coverage is not reset');
 assert.equal(db.prepare('SELECT COUNT(*) n FROM members').get().n,1);
 assert.equal(db.prepare('SELECT COUNT(*) n FROM deliveries').get().n,1,'delivery receipts within retention remain');
});

test('cold start on a current schema performs no writes',async()=>{
 const {db}=repositoryFixture();
 const writes=[];
 const ctx={blockConcurrencyWhile:fn=>fn(),id:{toString:()=>'fixture'},storage:{sql:{exec(sql,...bindings){if(/^\s*(INSERT|UPDATE|REPLACE|CREATE|ALTER|DELETE)/i.test(sql))writes.push(sql.slice(0,40));let rows=[];if(!bindings.length && sql.includes(';'))db.exec(sql);else{const st=db.prepare(sql);if(st.columns().length)rows=st.all(...bindings);else st.run(...bindings);}return {toArray:()=>rows,one:()=>rows[0]};}},transactionSync:fn=>fn()}};
 const state=new RepositoryState(ctx,{ALLOWED_REPOSITORIES:JSON.stringify([repo])});
 assert.deepEqual(writes,[]);assert.equal(state.schema,'current');
});


test('a replayed long-parked event keeps its receipt through maintenance',async()=>{
 const {db,state,archive}=repositoryFixture();
 await archive.put('parked/100/bodies/old.json',JSON.stringify({zen:'parked'}));
 await state.ingest({key:'parked/100/bodies/old.json',delivery:'old',event:'ping',repository:'org/repo',received:Date.now()-20*86400000});
 await state.maintenance();
 assert.equal(db.prepare("SELECT COUNT(*) n FROM deliveries WHERE id='old'").get().n,1);
});

test('a database at the size limit starts read-only: no migration, degraded until a verified probe',async()=>{
 const {db,archive}=repositoryFixture();
 db.exec("DELETE FROM meta WHERE key='schema_version'");
 const full=10*1024**3;
 const {state}=repositoryFixture({},full,db,archive);
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(state.schema,'migration_pending');assert.equal(state.degraded.operation,'database_size');
 assert.equal(db.prepare("SELECT COUNT(*) n FROM meta WHERE key='schema_version'").get().n,0,'no migration write was attempted');
 // After cleanup the file stays large; a probe-verified state lifts the size rule.
 await archive.put('control/100/storage.json',JSON.stringify({state:'writable',verified_at:Date.now()}));
 const {state:recovered}=repositoryFixture({},full,db,archive);
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(recovered.degraded,undefined);assert.equal(recovered.schema,'current');
 // A recorded failure degrades even a small database.
 await archive.put('control/100/storage.json',JSON.stringify({state:'full',since:1,source:'queue'}));
 const {state:marked}=repositoryFixture({},1,db,archive);
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(marked.degraded.operation,'recorded:queue');
});

test('a storage-full exception from the reset object parks immediately and records the state',async()=>{
 const h=harness();
 h.env.REPOSITORIES.get=()=>({fetch:async()=>{throw new Error('Exceeded the maximum database size.');}});
 const message=await deliver(h.env,'thrown',1);
 assert.equal(message.acked,true);assert.equal(message.retried,null);
 assert.ok(durable(h,'thrown'));
 assert.equal(JSON.parse(h.archive.objects.get('control/100/storage.json').bytes).state,'full');
 // A recorded full state parks without calling the repository object.
 h.env.REPOSITORIES.get=()=>({fetch:async()=>{throw new Error('unexpected repository call');}});
 assert.equal((await deliver(h.env,'recorded',1)).acked,true);assert.ok(durable(h,'recorded'));
 h.archive.objects.delete('control/100/storage.json');
 h.env.REPOSITORIES.get=()=>({fetch:async()=>{throw new Error('out of memory: SQLITE_NOMEM');}});
 assert.deepEqual((await deliver(h.env,'nomem',1)).retried,{delaySeconds:5},'other storage errors retry');
});

test('a recorded full state stops legacy migration even below the size threshold',async()=>{
 const {db,archive}=repositoryFixture();
 db.exec("DELETE FROM meta WHERE key='schema_version'");
 await archive.put('control/100/storage.json',JSON.stringify({state:'full',since:1,source:'queue'}));
 const {state}=repositoryFixture({},1,db,archive);
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(state.schema,'migration_pending');assert.equal(state.degraded.operation,'recorded:queue');
 assert.equal(db.prepare("SELECT COUNT(*) n FROM meta WHERE key='schema_version'").get().n,0);
});

test('degraded workflow reads never write: CI is not mirrored',async()=>{
 const {db,state}=repositoryFixture();
 db.prepare('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)').run('workflow_runs','77','',JSON.stringify({id:77,run_attempt:2}),'',1,1,0);
 (await state.setMeta('last_webhook',Date.now()));
 state.degraded={since:1,reason:'storage_full',operation:'test'};
 const before=db.prepare('SELECT COUNT(*) n FROM changes').get().n;
 const result=await queryFixture(state,{operation:'workflow_jobs',args:{id:'77'}});
 assert.equal(result.status,'unavailable');assert.equal(result.reason,'not_mirrored');
 assert.equal(db.prepare("SELECT COUNT(*) n FROM meta WHERE key='workflow_attempt:77'").get().n,0);
 assert.equal(db.prepare('SELECT COUNT(*) n FROM changes').get().n,before);
});

test('a failure report older than a verified probe cannot undo recovery',async()=>{
 const {noteRepositoryFull}=await load('src/ingress.ts');
 const archive=bucket(),env={ARCHIVE:archive};
 const started=Date.now()-1000;
 await archive.put('control/100/storage.json',JSON.stringify({state:'writable',verified_at:Date.now()}));
 await noteRepositoryFull(env,'100','queue',started);
 assert.equal(JSON.parse(archive.objects.get('control/100/storage.json').bytes).state,'writable');
 await noteRepositoryFull(env,'100','queue',Date.now()+1000);
 assert.equal(JSON.parse(archive.objects.get('control/100/storage.json').bytes).state,'full');
});

test('a batch parks its messages concurrently',async()=>{
 const h=harness();
 await h.archive.put('control/100/storage.json',JSON.stringify({state:'full',since:1,source:'queue'}));
 const put=h.archive.put.bind(h.archive);
 h.archive.put=async(...args)=>{await new Promise(resolve=>setTimeout(resolve,50));return put(...args);};
 const messages=[];
 for(let i=0;i<20;i++){
  const key='webhooks/2026-10-20/c'+i+'.json',text=JSON.stringify({i});
  await put(key,text,{customMetadata:{sha256:sha(Buffer.from(text))}});
  messages.push({body:{key,delivery:'c'+i,event:'push',repository:repo.name,received:1792000000000+i},attempts:1,acked:false,ack(){this.acked=true;},retry(){}});
 }
 const began=Date.now();
 await worker.queue({messages},h.env);
 assert.ok(messages.every(m=>m.acked));
 assert.ok(Date.now()-began<20*2*50,'not one message at a time');
 for(let i=0;i<20;i++) assert.ok(durable(h,'c'+i));
});


test('explicit ownership fence blocks adapter writes and raw SQL from old deployments',async()=>{
 const {db,state}=repositoryFixture({MIGRATION_ENVIRONMENT:'test'});try {
  const post=(path,body)=>state.fetch(new Request('https://internal/storage/'+path,{method:'POST',body:JSON.stringify(body)}));
  assert.equal((await post('ownership',{backend:'postgres',generation:'migration-2',expected_generation:'sqlite-v1'})).status,400,'park and drain first');
  db.prepare('INSERT INTO jobs(key,query,due) VALUES(?,?,?)').run('old','{}',0);
  await post('ingress',{mode:'parked'});
  const response=await post('ownership',{backend:'postgres',generation:'migration-2',expected_generation:'sqlite-v1'});
  assert.equal(response.status,200,JSON.stringify(await response.json()));
  await assert.rejects(state.put('pr','7',{number:7}),/generation fenced/);
  assert.throws(()=>db.prepare('INSERT INTO demand VALUES(?,?)').run('old-deployment',Date.now()),/generation fenced/);
  assert.throws(()=>db.prepare('UPDATE jobs SET dirty=1').run(),/generation fenced/);
  assert.equal((await post('probe',{})).status,400);
  assert.equal(db.prepare('SELECT COUNT(*) n FROM resources').get().n,0);
 }finally{db.close();}
});

test('a degraded mirror that stopped changing is reported unavailable, not served stale',async()=>{
 const {state}=repositoryFixture();
 const q={operation:'open_prs'};
 (await state.setMeta('last_webhook',Date.now()-60000));
 state.degraded={since:Date.now(),reason:'storage_full',operation:'test'};
 assert.notEqual((await queryFixture(state,q)).reason,'mirror_frozen','recently changed data is still served');
 (await state.setMeta('last_webhook',Date.now()-11*60000));
 const frozen=await queryFixture(state,q);
 assert.equal(frozen.status,'unavailable');assert.equal(frozen.reason,'mirror_frozen');assert.ok(frozen.frozen_since>0);
 assert.equal(Object.hasOwn(frozen,'data'),false);
 state.degraded=undefined;
 assert.notEqual((await queryFixture(state,q)).reason,'mirror_frozen','a quiet but writable mirror is current');
});

test('CI cleanup classes delete only CI rows, without an archive',async()=>{
 const {db,state}=repositoryFixture();
 const now=Date.now(),put=(kind,id)=>db.prepare('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)').run(kind,id,'',JSON.stringify({id}),'',now,1,0);
 put('workflow_runs','1');put('pr','7');put('checks','2');put('workflow_jobs','3');
 db.prepare('INSERT INTO members VALUES(?,?)').run('checks:{"sha":"a"}','2');
 db.prepare('INSERT INTO members VALUES(?,?)').run('open_prs:{}','7');
 db.prepare('INSERT INTO collections VALUES(?,?,?,?,?,?,?)').run('checks:{"sha":"a"}','checks','{}',1,now,1,null);
 db.prepare('INSERT INTO collections VALUES(?,?,?,?,?,?,?)').run('open_prs:{}','open_prs','{}',1,now,1,null);
 await state.setMeta('workflow_attempt:9',1);await state.setMeta('important_active:9',true);
 const call=(path,body)=>state.fetch(new Request('https://internal'+path,{method:'POST',body:JSON.stringify({repository_id:'100',...body})})).then(r=>r.json());
 for(const name of ['ci_resources','ci_members','ci_collections','ci_meta']) assert.equal((await call('/storage/cleanup',{class:name,limit:100})).class,name);
 assert.deepEqual(db.prepare('SELECT kind,id FROM resources').all().map(r=>r.kind+':'+r.id),['pr:7']);
 assert.deepEqual(db.prepare('SELECT collection FROM members').all().map(r=>r.collection),['open_prs:{}']);
 assert.deepEqual(db.prepare('SELECT key FROM collections').all().map(r=>r.key),['open_prs:{}']);
 assert.equal(db.prepare("SELECT COUNT(*) n FROM meta WHERE key LIKE 'workflow%' OR key LIKE 'important%'").get().n,0);
 assert.ok(await state.meta('repository'),'other meta survives');
});

const retention=await load('src/retention.ts');
const monitoring=await load('src/monitoring.ts');
const budget=(rows=100,bytes=4*1024**2)=>({rows,bytes,deadline:Date.now()+10000});
const scalar=(db,q)=>Object.values(db.prepare(q).get())[0];

test('sustained retention volume plateaus across windows with bounded rows, bytes and time',async()=>{
 const {db,state}=repositoryFixture(),day=86400000,checkpoints={};
 const start=Date.now()-60*day;
 for(let d=0;d<50;d++) {
  const now=start+d*day;
  for(let i=0;i<20;i++) {
   db.prepare('INSERT INTO deliveries VALUES(?,?,?)').run(d+'-'+i,'push',now);
   db.prepare('INSERT INTO changes VALUES(?,?,?,?,?)').run(d*20+i+1,now,'pr',String(i),0);
   db.prepare('INSERT INTO repair_metrics VALUES(?,?,?,?,?)').run(now,'page','active','pr',1);
   db.prepare('INSERT INTO retired_objects VALUES(?,?)').run(d+'-'+i,now);
  }
  for(const name of ['expired_deliveries','expired_changes','expired_repair_metrics','expired_retired_objects']) {
   for(let pass=0;pass<1000;pass++) {
    const b=budget(17,5000),before=b.rows;
    const next=retention.sweep(state.sql,name,checkpoints[name],b,true,now);
    assert.ok(before-b.rows<=17);assert.ok(b.bytes>=0);
    checkpoints[name]=next;
    if(next.after>=next.upper)break;
    assert.ok(pass<999,'bounded sweeps make progress');
   }
  }
  if(d>=20) {
   assert.equal(scalar(db,'SELECT COUNT(*) FROM deliveries'),15*20);
   assert.equal(scalar(db,'SELECT COUNT(*) FROM changes'),8*20);
   assert.equal(scalar(db,'SELECT COUNT(*) FROM repair_metrics'),8*20);
   assert.equal(scalar(db,'SELECT COUNT(*) FROM retired_objects'),(Math.min(d,30)+1)*20);
  }
 }
 const b={rows:100,bytes:1000,deadline:0};
 const before=scalar(db,'SELECT COUNT(*) FROM deliveries');
 retention.sweep(state.sql,'expired_deliveries',undefined,b,true,Date.now());
 assert.equal(scalar(db,'SELECT COUNT(*) FROM deliveries'),before,'expired time budget performs no deletion');
});

test('duplicate/stale retention checkpoints, dry runs and rowid reuse are safe',async()=>{
 const {db,state}=repositoryFixture(),now=Date.now();
 for(let i=0;i<20;i++)db.prepare('INSERT INTO retired_objects VALUES(?,?)').run('c'+i,now-31*86400000);
 const dry=retention.sweep(state.sql,'expired_retired_objects',undefined,budget(5),false,now);
 assert.equal(dry.eligible,5);assert.equal(dry.deleted,0);assert.equal(scalar(db,'SELECT COUNT(*) FROM retired_objects'),20);
 const first=retention.sweep(state.sql,'expired_retired_objects',undefined,budget(5),true,now);
 assert.equal(first.deleted,5);
 const old={...first,after:0};
 // Simulate a crash after SQL deletes but before the external checkpoint write.
 retention.sweep(state.sql,'expired_retired_objects',old,budget(5),true,now);
 db.prepare('INSERT INTO retired_objects VALUES(?,?)').run('fresh',now);
 for(let i=0;i<10;i++)retention.sweep(state.sql,'expired_retired_objects',old,budget(),true,now);
 assert.deepEqual(db.prepare('SELECT key id FROM retired_objects').all().map(r=>r.id),['fresh']);
 const b=budget(100,100);retention.sweep(state.sql,'expired_retired_objects',undefined,b,true,now);
 assert.ok(b.bytes>=0);
});

test('retention policy survives restart, preserves durable receipts, and isolates failed checkpoints',async()=>{
 const {db,state,archive}=repositoryFixture();
 const now=Date.now(),post=body=>state.fetch(new Request('https://internal/storage/retention',{method:'POST',body:JSON.stringify(body)}));
 const old=now-30*86400000;
 db.prepare('INSERT INTO deliveries VALUES(?,?,?)').run('pending','push',old);
 db.prepare('INSERT INTO demand VALUES(?,?)').run('quiet',old);
 await archive.put('parked/100/pointers/pending.json','{}');
 await post({policy:{mode:'enforce',disabled:[],pinned:['expired_demand']}});
 await state.storageMaintenance();
 assert.equal(scalar(db,'SELECT COUNT(*) FROM deliveries'),0,'durable R2 receipts permit SQL expiry while replay stays pending');
 assert.ok(await archive.head('receipts/100/pending.json'),'pending input retains its durable receipt');
 assert.equal(scalar(db,'SELECT COUNT(*) FROM demand'),1,'operator pin wins');
 archive.objects.delete('parked/100/pointers/pending.json');
 const restarted=repositoryFixture({},0,db,archive).state;
 archive.failures.push((op,key)=>op==='put' && key.endsWith('retention-checkpoint.json'));
 await restarted.storageMaintenance();
 assert.equal(scalar(db,'SELECT COUNT(*) FROM deliveries'),0);
 assert.equal(scalar(db,'SELECT COUNT(*) FROM demand'),1,'persisted override survived restart');
 await restarted.storageMaintenance();
 assert.ok(await archive.get('control/100/retention-checkpoint.json'),'later checkpoint succeeds after failed job');
 assert.equal((await post({policy:{mode:'enforce',disabled:['resources'],pinned:[]}})).status,400,'no arbitrary cleanup classes');
});

test('repair telemetry never writes authoritative SQL and its dimensions stay bounded',async()=>{
 const {db,state,archive}=repositoryFixture();
 for(let i=0;i<100000;i++)state.metric('page',{operation:'pr',repository:'org/repo'},'active');
 state.metric('requested_single_latency_ms',{operation:'pr',repository:'org/repo'},'requested',25);
 assert.equal(scalar(db,'SELECT COUNT(*) FROM repair_metrics'),0);
 assert.equal(state.repairMetrics.summary().repair_hour.find(r=>r.event==='page').value,100000);
 assert.equal(state.repairMetrics.summary().requested_latency[0].p95_ms,25);
 await state.storageMaintenance();
 const observation=await state.preparedObservation();
 const pages=observation.repair_summary.repair_hour.filter(r=>r.event==='page');
 assert.ok(pages.length<=2);assert.equal(pages.reduce((n,r)=>n+r.value,0),100000);
 const telemetry=[...archive.objects].filter(([key])=>key.startsWith('telemetry/'));
 assert.equal(telemetry.length,1);assert.ok(Number(telemetry[0][1].customMetadata.expires)>Date.now());
 const metrics=new monitoring.RequestMetrics();
 for(let minute=0;minute<120;minute++)for(let i=0;i<10;i++)metrics.record(20,i===0,i<2,minute*60000);
 const summary=metrics.snapshot(119*60000);
 assert.equal(summary.intervals.length,60);assert.equal(summary.requests,600);assert.equal(summary.p50_ms,25);assert.equal(summary.p95_ms,25);
 assert.equal(summary.error_rate,.1);assert.equal(summary.unavailable_rate,.2);
});

test('capacity thresholds handle growth, shrinkage, unknown allowance and stale samples',async()=>{
 const C=monitoring.capacity,at=1000000000,allowance=1000000;
 assert.equal(C({at,bytes:700000,allowance}).level,'warning');
 assert.equal(C({at,bytes:850000,allowance}).level,'critical');
 assert.equal(C({at,bytes:1000,allowance:null},{at:at-60000,bytes:0,allowance:null}).capacity_unknown,true);
 const previous={at:at-3600000,bytes:100000,allowance};
 assert.equal(C({at,bytes:200000,allowance},previous,at,{forecast_level:'critical',forecast_breaches:2,bytes:190000}).level,'critical','positive slope projects exhaustion within 24 hours');
 assert.equal(C({at,bytes:110000,allowance},previous,at,{forecast_level:'warning',forecast_breaches:2,bytes:109000}).level,'warning','positive slope projects exhaustion within seven days');
 assert.equal(C({at,bytes:99999,allowance},previous,at).projected_exhaustion_at,null);
 assert.equal(C({at,bytes:1000,allowance},undefined,at+180001).stale,true);
 assert.equal(C({at,bytes:200000,allowance},previous,at).level,'ok','one burst does not escalate a forecast');
 assert.equal(C({at,bytes:200000,allowance},previous,at,{forecast_level:'critical',forecast_breaches:2,bytes:200000}).level,'ok','flat follow-up clears confirmation');
 const original=console.log;console.log=()=>{throw new Error('destination failed');};
 try {const alarm=monitoring.emitAlarm({},'100',{level:'critical',at});assert.equal(alarm.emitted,false);assert.equal(alarm.notified,false);}
 finally {console.log=original;}
});

test('prepared health does no aggregate SQL or GitHub calls and survives monitoring persistence failure',async()=>{
 const {state,archive}=repositoryFixture({INSTALLATIONS:{get(){assert.fail('monitoring must not call quota owner');}}});
 archive.failures.push((op,key)=>op==='put' && key.endsWith('observation.json'));
 await state.storageMaintenance();
 assert.equal((await state.preparedObservation()).stale,false,'in-memory diagnostics remain available');
 await state.prepareDiagnostics();assert.ok(state.diagnosticsAt>0);
 const original=state.sql;state.sql=new Proxy(original,{get(target,key){if(key==='exec')return (q,...b)=>{assert.ok(!/COUNT\(|GROUP BY|SUM\(|AVG\(/i.test(q),'health must only read prepared observations');return target.exec(q,...b);};return Reflect.get(target,key,target);}});
 const response=await state.fetch(new Request('https://internal/health'));
 assert.equal(response.status,200);assert.equal((await response.json()).monitoring.stale,false);
 state.observation.at=Date.now()-180001;
 assert.equal((await state.preparedObservation()).stale,true);
});

test('R2 outage alarms remain available and a later monitoring attempt recovers',async()=>{
 const {state,archive}=repositoryFixture();
 archive.failures.push((op,key)=>op==='put' && key.endsWith('ingress-durability-probe.json'));
 await state.storageMaintenance();
 let observation=await state.preparedObservation();
 assert.equal(observation.level,'critical');assert.ok(observation.reasons.includes('parked_ingress_durability_unavailable'));
 assert.equal(observation.alarm.notified,false);
 state.observation.at=Date.now()-60001;
 await state.storageMaintenance();observation=await state.preparedObservation();
 assert.equal(observation.ingress.durable,true);assert.equal(observation.level,'ok');
});



test('R2 prefix expiry preserves pinned inputs and retries after deletion/checkpoint failures',async()=>{
 const archive=bucket(),now=Date.now();
 for(const [key,metadata] of [['webhooks/expired',{expires:String(now-1)}],['webhooks/pinned',{expires:'1',pinned:'true'}],['webhooks/fresh',{expires:String(now+100000)}],['parked/100/bodies/input',{expires:'1'}]])await archive.put(key,'{}',{customMetadata:metadata});
 archive.failures.push((op,key)=>op==='put' && key==='control/retention/raw_webhooks.json');
 await assert.rejects(retention.retirePrefix(archive,'raw_webhooks',now));
 assert.equal(await archive.get('webhooks/expired'),null,'deletion can commit before checkpoint');
 const progress=await retention.retirePrefix(archive,'raw_webhooks',now);
 assert.ok(progress.completed);assert.ok(await archive.get('webhooks/pinned'));assert.ok(await archive.get('webhooks/fresh'));assert.ok(await archive.get('parked/100/bodies/input'));
 await archive.put('staging/expired','{}',{customMetadata:{expires:'1'}});
 archive.failures.push((op,key)=>op==='delete' && key==='staging/expired');
 await assert.rejects(retention.retirePrefix(archive,'staging_pages',now));
 assert.equal((await retention.retirePrefix(archive,'staging_pages',now)).deleted,1,'failed class does not disable future attempts');
});


test('malformed retention progress restarts safely without dropping persisted pins',async()=>{
 const {state,archive,db}=repositoryFixture();
 await archive.put('control/100/retention-policy.json',JSON.stringify({mode:'enforce',disabled:[],pinned:['expired_retired_objects']}));
 await archive.put('control/100/retention-checkpoint.json',JSON.stringify({version:1,classes:{expired_retired_objects:{after:'bad'}}}));
 db.prepare('INSERT INTO retired_objects VALUES(?,?)').run('pinned',Date.now()-31*86400000);
 await state.storageMaintenance();
 assert.equal(scalar(db,'SELECT COUNT(*) FROM retired_objects'),1);
 assert.equal((await state.preparedObservation()).retention.policy_loaded,true);
});


test('a single bounded sweep per interval keeps up with sustained append volume',async()=>{
 const {state,db}=repositoryFixture(),day=86400000,start=Date.now()-60*day;
 let checkpoint;
 for(let interval=0;interval<50;interval++) {
  const now=start+interval*day;
  for(let i=0;i<200;i++)db.prepare('INSERT INTO changes VALUES(?,?,?,?,?)').run(interval*200+i+1,now,'pr',String(i),0);
  const b=budget(6000);
  checkpoint=retention.sweep(state.sql,'expired_changes',checkpoint,b,true,now);
  assert.ok(6000-b.rows<=1000);
  if(interval>20)assert.ok(scalar(db,'SELECT COUNT(*) FROM changes')<=2400,'expiry reaches a plateau without unbounded catch-up loops');
 }
});



test('default policy retains the S0 fourteen-day receipt expiry',async()=>{
 const {db,state}=repositoryFixture();
 db.prepare('INSERT INTO deliveries VALUES(?,?,?)').run('old','ping',Date.now()-15*86400000);
 db.prepare('INSERT INTO deliveries VALUES(?,?,?)').run('live','ping',Date.now()-13*86400000);
 await state.storageMaintenance();
 assert.deepEqual(db.prepare('SELECT id FROM deliveries').all().map(r=>r.id),['live']);
});

test('tick re-arms before a slow monitoring job and expires from cached policy',async()=>{
 const {db,state}=repositoryFixture();await state.fixtureReady;
 db.prepare('INSERT INTO retired_objects VALUES(?,?)').run('old',Date.now()-31*86400000);
 let armed=false;state.setAlarm=async()=>{armed=true;};
 state.storageMaintenance=()=>new Promise(()=>{});
 const response=await state.fetch(new Request('https://internal/tick',{method:'POST',body:JSON.stringify({repository:'org/repo'})}));
 assert.equal(response.status,200);assert.equal(armed,true);assert.equal(scalar(db,'SELECT COUNT(*) FROM retired_objects'),0);
});

test('unknown durable pins fail closed while explicit emergency SQL cleanup remains available',async()=>{
 const {db,state,archive}=repositoryFixture();await state.fixtureReady;
 db.prepare('INSERT INTO retired_objects VALUES(?,?)').run('pinned-unknown',Date.now()-31*86400000);
 const original=archive.get.bind(archive);archive.get=async key=>{if(key.endsWith('retention-policy.json'))throw new Error('R2 unavailable');return original(key);};
 state.storageLoaded=false;
 await state.storageMaintenance();
 assert.equal(scalar(db,'SELECT COUNT(*) FROM retired_objects'),1,'never guess that an unavailable pin policy is empty');
 const response=await state.fetch(new Request('https://internal/storage/cleanup',{method:'POST',body:JSON.stringify({class:'expired_retired_objects'})}));
 assert.equal(response.status,200);assert.equal((await response.json()).rows,1,'operator-selected emergency class is independent of R2');
});

test('retention inspection does not launch expiry or archival',async()=>{
 const {db,state}=repositoryFixture();await state.fixtureReady;
 db.prepare('INSERT INTO retired_objects VALUES(?,?)').run('old',Date.now()-31*86400000);
 state.storageMaintenance=async()=>assert.fail('inspection must not run maintenance');
 const response=await state.fetch(new Request('https://internal/storage/retention',{method:'POST',body:'{}'}));
 assert.equal(response.status,200);assert.equal(scalar(db,'SELECT COUNT(*) FROM retired_objects'),1);
});

test('R2 expiry batches a full cursor page and reports oversize objects without starvation',async()=>{
 const archive=bucket(),now=Date.now();let deletes=0;
 for(let i=0;i<500;i++)await archive.put('cursors/'+String(i).padStart(4,'0'),'{}',{customMetadata:{expires:'1'}});
 const remove=archive.delete.bind(archive);archive.delete=async keys=>{deletes++;return remove(keys);};
 const result=await retention.retirePrefix(archive,'r2_cursors',now);
 assert.equal(result.deleted,500);assert.equal(deletes,1);
 await archive.put('staging/a-huge','{}',{customMetadata:{expires:'1'}});
 await archive.put('staging/z-small','{}',{customMetadata:{expires:'1'}});
 const list=archive.list.bind(archive);archive.list=async opts=>{const p=await list(opts);for(const o of p.objects)if(o.key.endsWith('a-huge'))o.size=101*1024**2;return p;};
 const progress=await retention.retirePrefix(archive,'staging_pages',now);
 assert.equal(progress.oversized.length,1);assert.equal(progress.deleted,1);assert.equal(progress.paused,false);
});

test('checkpoints are coalesced and prepared diagnostics advance from tick maintenance',async()=>{
 const {db,state,archive}=repositoryFixture();let writes=0;
 const put=archive.put.bind(archive);archive.put=async(key,...args)=>{if(key.endsWith('retention-checkpoint.json'))writes++;return put(key,...args);};
 await state.storageMaintenance();
 db.prepare('INSERT INTO retired_objects VALUES(?,?)').run('old',Date.now()-31*86400000);
 await state.storageMaintenance();await state.storageMaintenance();
 assert.equal(writes,1,'multiple alarms do not add checkpoint PUTs per second');
 assert.ok(state.diagnosticsAt>0);assert.equal(state.diagnostics.reconciliation_version,2);
});

test('hourly size estimates never count a whole table and checkpoint their attempt first',async()=>{
 const {state,archive}=repositoryFixture();await state.fixtureReady;
 const original=state.sql;let sawSample=false;
 state.sql=new Proxy(original,{get(target,key){if(key!=='exec')return Reflect.get(target,key,target);return (q,...b)=>{
  if(/FROM \"resources\"/.test(q) && /SUM\(/.test(q)) {assert.ok(archive.objects.has('control/100/table-estimate-attempt.json'));sawSample=true;}
  assert.ok(!/^SELECT COUNT\(\*\) n FROM \"/.test(q),'whole-table row counts are forbidden');
  return target.exec(q,...b);
 };}});
 await state.storageMaintenance();assert.equal(sawSample,true);
});

test('expired staged spills retire, then traversal restarts without an empty-result substitution',async()=>{
 const {db,state,archive}=repositoryFixture();await state.fixtureReady;
 const encoded=await state.encode({rows:['x'.repeat(1600000)]},Date.now()-1),key=JSON.parse(encoded).$r2;
 assert.ok(key.startsWith('staging-spills/'));assert.equal((await retention.retirePrefix(archive,'staging_spills')).deleted,1);
 const job={key:'review_threads:{"number":7}',query:JSON.stringify({operation:'review_threads',args:{number:7}}),page:1,attempt:0,lease_id:'l',nested:encoded};
 db.prepare('INSERT INTO jobs(key,query,due,lease_id,nested) VALUES(?,?,?,?,?)').run(job.key,job.query,0,job.lease_id,encoded);
 await assert.rejects(state.refresh(job),/staging page missing/);
 await state.retryJob(job,new Error('staging page missing'));
 const row=db.prepare('SELECT nested,wait_reason FROM jobs WHERE key=?').get(job.key);assert.equal(row.nested,null);assert.equal(row.wait_reason,'staging_expired');
});

test('archived collection reads bound parallel R2 fetches and preserve membership order',async()=>{
 const {db,state,archive}=repositoryFixture();await state.fixtureReady;
 const key='workflow_jobs:{"id":"9"}';await state.setMeta('workflow_attempt:9',1);
 db.prepare('INSERT INTO collections VALUES(?,?,?,?,?,?,?)').run(key,'workflow_jobs','{"id":"9"}',1,Date.now(),1,null);
 for(let i=1;i<=20;i++) {
  const blob='resources/100/job-'+i;await archive.put(blob,JSON.stringify({id:i,run_attempt:1}));
  db.prepare('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)').run('workflow_jobs',String(i),'9',JSON.stringify({$r2:blob,run_attempt:1}),'',1,1,0);
  db.prepare('INSERT INTO members VALUES(?,?)').run(key,String(i));
 }
 let active=0,peak=0;const get=archive.get.bind(archive);
 archive.get=async key=>{active++;peak=Math.max(peak,active);await new Promise(r=>setTimeout(r,1));try{return await get(key);}finally{active--;}};
 const rows=await state.rows({operation:'workflow_jobs',args:{id:'9'}});
 assert.equal(peak,8);assert.deepEqual(rows.map(r=>r.id),Array.from({length:20},(_,i)=>i+1));
});

test('installation accounting prepares retained hourly projections with a bounded scan and expires from its oldest prefix',async()=>{
 const {InstallationScheduler}=await load('src/installation.ts'),{state,db}=repositoryFixture();
 const owner=new InstallationScheduler(state.ctx,{}),now=Date.now();
 const insert=db.prepare('INSERT INTO accounting(at,operation,api,method,endpoint,status,points,latency,bytes,shape,batch_size,predicted) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)');
 for(let i=0;i<10;i++)insert.run(now-8*86400000,'old','core','GET','/x',200,1,10,10,'x',1,1);
 for(let i=0;i<10001;i++)insert.run(now,'live','core','GET','/x',200,1,10,10,'x',1,1);
 owner.expireAccounting();assert.equal(scalar(db,"SELECT COUNT(*) FROM accounting WHERE operation='old'"),0);
 owner.prepareAccounting();
 assert.equal(owner.preparedAccounting.hour[0].requests,10000);
 assert.equal(owner.preparedAccounting.scope,'retained_accounting_hour');assert.equal(owner.preparedAccounting.window_complete,false,'bound reached must not masquerade as a complete hour');
 const restarted=new InstallationScheduler(state.ctx,{});
 assert.deepEqual(restarted.preparedAccounting,JSON.parse(JSON.stringify(owner.preparedAccounting)),'restart preserves the scan-completeness limit');
});

test('cold installation health restores retained accounting without refreshing its timestamp or scanning the log',async()=>{
 const {InstallationScheduler}=await load('src/installation.ts'),{state,db}=repositoryFixture();await state.fixtureReady;
 state.ctx.storage.get=async()=>undefined;
 const originalNow=Date.now,originalFetch=globalThis.fetch;let now=originalNow(),externalCalls=0;
 Date.now=()=>now;globalThis.fetch=async()=>{externalCalls++;assert.fail('accounting health must not query GitHub');};
 try {
  const owner=new InstallationScheduler(state.ctx,{});
  db.prepare('INSERT INTO accounting(at,operation,api,status,points,latency,bytes) VALUES(?,?,?,?,?,?,?)').run(now,'pr','core',200,1,10,20);
  owner.prepareAccounting();
  const first=owner.preparedAccounting;
  assert.equal(first.window_complete,true);assert.equal(first.hour[0].requests,1);
  const queries=[],exec=state.ctx.storage.sql.exec.bind(state.ctx.storage.sql);
  state.ctx.storage.sql.exec=(sql,...bindings)=>{queries.push(sql);return exec(sql,...bindings);};
  now+=90000;
  const restarted=new InstallationScheduler(state.ctx,{}),health=await(await restarted.fetch(new Request('https://internal/health'))).json();
  assert.equal(health.scope,'retained_accounting_hour');assert.equal(health.at,first.at);assert.equal(health.stale,false);assert.deepEqual(health.hour,JSON.parse(JSON.stringify(first.hour)));
  now+=100000;
  const stale=await(await new InstallationScheduler(state.ctx,{}).fetch(new Request('https://internal/health'))).json();
  assert.equal(stale.at,first.at);assert.equal(stale.stale,true,'eviction must not make an old report fresh');
  assert.ok(!queries.some(sql=>/FROM accounting\b/.test(sql)),'cold reads use the single prepared row');
  assert.equal(externalCalls,0);
  restarted.prepareAccounting();
  const refreshed=new InstallationScheduler(state.ctx,{}).preparedAccounting;
  assert.equal(refreshed.at,now);assert.equal(scalar(db,'SELECT COUNT(*) FROM prepared_accounting'),1);
 }finally {Date.now=originalNow;globalThis.fetch=originalFetch;}
});

test('a failed accounting snapshot write preserves the last durable report',async()=>{
 const {InstallationScheduler}=await load('src/installation.ts'),{state,db}=repositoryFixture();await state.fixtureReady;
 state.ctx.storage.get=async()=>undefined;
 const originalNow=Date.now;let now=originalNow();Date.now=()=>now;
 try {
  const owner=new InstallationScheduler(state.ctx,{}),insert=db.prepare('INSERT INTO accounting(at,operation,api,status,points,latency,bytes) VALUES(?,?,?,?,?,?,?)');
  insert.run(now,'pr','core',200,1,10,20);owner.prepareAccounting();const first=JSON.parse(JSON.stringify(owner.preparedAccounting));
  now+=190000;
  insert.run(now,'pr','core',200,1,10,20);
  const exec=state.ctx.storage.sql.exec.bind(state.ctx.storage.sql);
  state.ctx.storage.sql.exec=(sql,...bindings)=>{if(sql.startsWith('INSERT INTO prepared_accounting'))throw new Error('storage unavailable');return exec(sql,...bindings);};
  owner.prepareAccounting();
  assert.deepEqual(JSON.parse(JSON.stringify(owner.preparedAccounting)),first);
  assert.deepEqual(new InstallationScheduler(state.ctx,{}).preparedAccounting,first,'restart cannot publish the failed update');
  const failed=await(await owner.fetch(new Request('https://internal/health'))).json();
  assert.equal(failed.stale,true);assert.equal(failed.accounting_preparation_failure.at,now);
  state.ctx.storage.sql.exec=exec;owner.prepareAccounting();
  const refreshed=new InstallationScheduler(state.ctx,{}).preparedAccounting;
  assert.equal(refreshed.at,now);assert.equal(refreshed.hour[0].requests,2);
  assert.equal((await(await owner.fetch(new Request('https://internal/health'))).json()).accounting_preparation_failure,null);
 }finally {Date.now=originalNow;}
});

test('invalid saved accounting falls back to provisional health and the next preparation repairs it',async()=>{
 const {InstallationScheduler}=await load('src/installation.ts'),{state,db}=repositoryFixture();await state.fixtureReady;
 state.ctx.storage.get=async()=>undefined;
 const initial=new InstallationScheduler(state.ctx,{});initial.prepareAccounting();const report=initial.preparedAccounting;
 for(const data of ['{','{}',JSON.stringify({...report,version:2}),JSON.stringify({...report,at:Date.now()+86400000}),JSON.stringify({...report,window_complete:undefined})]) {
  db.prepare('UPDATE prepared_accounting SET data=? WHERE singleton=1').run(data);
  const restarted=new InstallationScheduler(state.ctx,{}),health=await(await restarted.fetch(new Request('https://internal/health'))).json();
  assert.equal(health.scope,'installation_instance_since_restart');assert.equal(health.stale,true);assert.ok(health.accounting_preparation_failure);
  restarted.prepareAccounting();
  assert.equal(new InstallationScheduler(state.ctx,{}).preparedAccounting.version,1);
  assert.equal((await(await restarted.fetch(new Request('https://internal/health'))).json()).accounting_preparation_failure,null);
 }
});

test('saved accounting cannot replace live scheduler identity or quota settings',async()=>{
 const {InstallationScheduler}=await load('src/installation.ts'),{state,db}=repositoryFixture();await state.fixtureReady;
 state.ctx.storage.get=async()=>undefined;
 const owner=new InstallationScheduler(state.ctx,{CF_VERSION:{id:'preparing-version'}});owner.prepareAccounting();
 db.prepare('UPDATE prepared_accounting SET data=? WHERE singleton=1').run(JSON.stringify({...owner.preparedAccounting,deployment_id:'wrong-version',settings:{concurrency:1000},stale:false,window_start:0}));
 const restarted=new InstallationScheduler(state.ctx,{CF_VERSION:{id:'current-version'}}),health=await(await restarted.fetch(new Request('https://internal/health'))).json();
 assert.equal(health.deployment_id,'current-version');assert.equal(health.prepared_deployment,'preparing-version');
 assert.equal(health.window_start,owner.preparedAccounting.at-3600000);assert.notEqual(health.settings.concurrency,1000);
});

test('optional accounting snapshot creation failure does not disable the scheduler and can recover',async()=>{
 const {InstallationScheduler}=await load('src/installation.ts'),{state}=repositoryFixture();await state.fixtureReady;
 state.ctx.storage.get=async()=>undefined;
 const exec=state.ctx.storage.sql.exec.bind(state.ctx.storage.sql);
 state.ctx.storage.sql.exec=(sql,...bindings)=>{if(sql.startsWith('CREATE TABLE IF NOT EXISTS prepared_accounting'))throw new Error('storage unavailable');return exec(sql,...bindings);};
 const owner=new InstallationScheduler(state.ctx,{}),health=await owner.fetch(new Request('https://internal/health'));
 assert.equal(health.status,200);assert.equal((await health.json()).scope,'installation_instance_since_restart');
 state.ctx.storage.sql.exec=exec;
 const maintenance=await(await owner.fetch(new Request('https://internal/maintenance',{method:'POST'}))).json();
 assert.equal(maintenance.accounting_preparation_failure,null);
 assert.equal(new InstallationScheduler(state.ctx,{}).preparedAccounting.scope,'retained_accounting_hour');
});


test('cold health does not wait indefinitely for the observation destination',async()=>{
 const {state,archive}=repositoryFixture();await state.fixtureReady;
 let release;const original=archive.get.bind(archive);
 archive.get=key=>key.endsWith('observation.json')?new Promise(r=>{release=()=>r(null);}):original(key);
 const began=performance.now();const response=await state.fetch(new Request('https://internal/health'));
 assert.ok(performance.now()-began<500);assert.equal((await response.json()).monitoring.stale,true);release();
});

test('accounting instrumentation failure cannot fail an upstream semantic response',async()=>{
 const {InstallationScheduler}=await load('src/installation.ts'),{state}=repositoryFixture();await state.fixtureReady;
 state.ctx.storage.get=async()=>undefined;
 const owner=new InstallationScheduler(state.ctx,{}),original=globalThis.fetch;
 owner.accountingMetrics.record=()=>{throw new Error('telemetry unavailable');};
 globalThis.fetch=async()=>Response.json({id:'exact-result'},{headers:{'x-ratelimit-resource':'core','x-ratelimit-remaining':'99','x-ratelimit-limit':'100','x-ratelimit-reset':String(Math.floor(Date.now()/1000)+300)}});
 try {
  const result=await owner.http('/repos/org/repo','test-token','GET',undefined,'repository','core');
  assert.equal(result.status,200);assert.equal(result.body.id,'exact-result');assert.ok(owner.accountingFailure);
 }finally {globalThis.fetch=original;}
});


test('health retains schema diagnostics when repository metadata is missing',async()=>{
 const {state,db}=repositoryFixture();await state.fixtureReady;
 state.degraded={since:1,reason:'storage_full',operation:'fixture'};
 db.exec('DROP TABLE meta');state.schema={error:'schema_unavailable',missing:['meta'],cause:'storage_full'};
 const response=await state.fetch(new Request('https://internal/health'));
 assert.equal(response.status,200);const result=await response.json();assert.deepEqual(result.storage.schema.missing,['meta']);assert.equal(result.monitoring.stale,true);
});

test('a delayed cold policy load cannot overwrite an acknowledged operator pin',async()=>{
 const {state,archive}=repositoryFixture();await state.fixtureReady;state.storageLoaded=false;state.retentionProgressLoaded=false;
 const checkpoint={after:20,upper:100,started:Date.now()-60000,examined:20,eligible:0,deleted:0,bytes:1000};
 await archive.put('control/100/retention-checkpoint.json',JSON.stringify({version:1,classes:{expired_changes:checkpoint}}));
 const previous={mode:'enforce',disabled:[],pinned:[]},next={...previous,pinned:['expired_changes']};
 const key='control/100/retention-policy.json';await archive.put(key,JSON.stringify(previous));
 const put=archive.put.bind(archive),get=archive.get.bind(archive);
 let releasePut,enteredPut,releaseGet,enteredGet;
 const putGate=new Promise(r=>releasePut=r),putStarted=new Promise(r=>enteredPut=r),getGate=new Promise(r=>releaseGet=r),getStarted=new Promise(r=>enteredGet=r);
 archive.put=async(name,...args)=>{if(name===key) {enteredPut();await putGate;}return put(name,...args);};
 const updating=state.fetch(new Request('https://internal/storage/retention',{method:'POST',body:JSON.stringify({policy:next})}));await putStarted;
 archive.get=async name=>{if(name===key) {const old=await get(name);enteredGet();await getGate;return old;}return get(name);};
 const loading=state.loadRetentionState();await getStarted;
 releasePut();assert.equal((await updating).status,200);releaseGet();await loading;
 assert.deepEqual(state.retentionPolicy.pinned,['expired_changes']);
 assert.equal(state.retentionState.expired_changes.after,20);
});

test('a completed retention epoch waits for the next minute instead of rescanning on every alarm',async()=>{
 const {state,db}=repositoryFixture();await state.fixtureReady;
 db.prepare('INSERT INTO changes VALUES(?,?,?,?,?)').run(1,Date.now(),'pr','1',0);
 await state.expire();const first={...state.retentionState.expired_changes};
 for(let i=0;i<20;i++)await state.expire();
 assert.deepEqual(state.retentionState.expired_changes,first);
});

test('incremental accounting projections roll the hourly window without regrouping on health reads',async()=>{
 const metrics=new monitoring.AccountingMetrics(),all=[];
 for(let minute=0;minute<120;minute++)for(let i=0;i<10;i++) {
  const row={api:i%2?'core':'graphql',operation:'pr',shape:'shape-'+i%3,batch_size:1,points:i%3===0?null:1,latency:i,bytes:100,predicted:1,at:minute*60000};
  all.push(row);metrics.record(row,row.at);
 }
 const now=119*60000,current=all.filter(r=>r.at>=60*60000),snapshot=metrics.snapshot(now);
 assert.equal(snapshot.hour.reduce((n,r)=>n+r.requests,0),600);
 for(const row of snapshot.hour) {
  const expected=current.filter(r=>r.api===row.api),measured=expected.filter(r=>r.points!==null);
  assert.equal(row.requests,expected.length);assert.equal(row.points,measured.reduce((n,r)=>n+r.points,0));assert.equal(row.unmeasured,expected.length-measured.length);
  assert.equal(row.latency,expected.reduce((n,r)=>n+r.latency,0)/expected.length);
 }
 assert.equal(metrics.snapshot(now).hour,snapshot.hour,'health reads serve the prepared projection');
});


test('a future completion timestamp cannot indefinitely suppress expiry',async()=>{
 const {state,archive,db}=repositoryFixture();await state.fixtureReady;
 state.storageLoaded=false;state.retentionProgressLoaded=false;
 const now=Date.now(),row={after:1,upper:1,started:now,completed:now+365*86400000,examined:1,eligible:0,deleted:0,bytes:1};
 await archive.put('control/100/retention-policy.json',JSON.stringify({mode:'enforce',pinned:[],disabled:[]}));
 await archive.put('control/100/retention-checkpoint.json',JSON.stringify({version:1,classes:{expired_changes:row}}));
 db.prepare('INSERT INTO changes VALUES(?,?,?,?,?)').run(1,now-8*86400000,'pr','1',0);
 await state.loadRetentionState();await state.expire();
 assert.equal(scalar(db,'SELECT COUNT(*) FROM changes'),0);
});

test('a retention pin acquired while listing prevents the pending R2 delete',async()=>{
 const archive=bucket(),now=Date.now();let pinned=false;
 await archive.put('webhooks/100/old.json','retained',{customMetadata:{expires:String(now-1)}});
 const list=archive.list.bind(archive);archive.list=async options=>{const page=await list(options);pinned=true;return page;};
 const result=await retention.retirePrefix(archive,'raw_webhooks',now,async()=>!pinned);
 assert.ok(await archive.get('webhooks/100/old.json'));assert.equal(result.deleted,0);
});

test('checkpoint read failures preserve durable progress while cached-policy expiry still runs',async()=>{
 const {state,archive,db}=repositoryFixture();await state.fixtureReady;
 state.storageLoaded=false;state.retentionProgressLoaded=false;
 const key='control/100/retention-checkpoint.json',checkpoint=JSON.stringify({version:1,classes:{}});
 await archive.put(key,checkpoint);
 for(let i=0;i<2;i++)archive.failures.push((op,name)=>op==='get' && name===key);
 db.prepare('INSERT INTO changes VALUES(?,?,?,?,?)').run(1,Date.now()-8*86400000,'pr','1',0);
 await state.storageMaintenance();await state.storageMaintenance();
 assert.equal(scalar(db,'SELECT COUNT(*) FROM changes'),0,'expiry does not wait for R2 checkpoint recovery');
 assert.equal(state.retentionProgressLoaded,false);assert.equal(await archive.get(key).then(o=>o.text()),checkpoint);
 await state.storageMaintenance();assert.equal(state.retentionProgressLoaded,true);
});


const ingress=await load('src/ingress.ts');
const tick=state=>state.fetch(new Request('https://internal/tick',{method:'POST',body:JSON.stringify({repository:repo.name})}));
async function scheduled(env){const tasks=[];await worker.scheduled({},env,{waitUntil:p=>tasks.push(p)});await Promise.all(tasks);}
const clock=async fn=>{const original=Date.now;let now=original();Date.now=()=>now;try{await fn(ms=>{now+=ms;});}finally{Date.now=original;}};
function cronHarness(){
 const h=harness();let githubReads=0;
 const fetch=h.env.REPOSITORIES.get().fetch;
 h.env.REPOSITORIES.get=()=>({fetch:async(url,init)=>{
  if(new URL(url).pathname==='/tick'){if(!h.state.full)await h.archive.put('control/100/storage.json',JSON.stringify({state:'writable',verified_at:Date.now()}));return Response.json({ok:true});}
  if(new URL(url).pathname==='/storage/deliveries')return Response.json({known:[]});
  return fetch(url,init);
 }});
 h.env.INSTALLATIONS={idFromName:x=>x,get:()=>({fetch:async url=>{assert.ok(['/audit','/maintenance'].includes(new URL(url).pathname),'only existing installation maintenance and audit are allowed');return Response.json({ok:true});}})};
 h.githubReads=()=>githubReads;
 return h;
}

test('only ordinary cron passes recover held and legacy DLQ input; retry dates expire and poison is isolated',async()=>clock(async advance=>{
 const h=cronHarness();h.state.full=true;
 await deliver(h.env,'held-valid');h.state.full=false;h.state.error='dependency_down';
 for(let i=0;i<5;i++){await scheduled(h.env);advance(61000);}
 const held=[...h.archive.objects.keys()].find(k=>k.includes('/held/'));
 assert.ok(held);assert.equal(JSON.parse(h.archive.objects.get(held).bytes).replay_failures,5);
 // A DLQ entry is recovered from raw data; no DLQ queue poll or GitHub read.
 await h.archive.put('webhooks/2026-10-20/dlq-valid.json',JSON.stringify({repository:{id:100},installation:{id:200}}),{customMetadata:{event:'ping',expires:String(Date.now()+1)}});
 h.state.error=null;
 await h.archive.put('parked/100/held/000000000000001-poison.json',JSON.stringify({key:'missing-body',delivery:'poison',event:'ping',repository:repo.name,received:1,replay_failures:5}));
 for(let i=0;i<6;i++){await scheduled(h.env);advance(61000);}
 assert.equal(h.state.applied.get('held-valid'),1);assert.equal(h.state.applied.get('dlq-valid'),1);
 assert.ok(h.archive.objects.has('parked/100/held/000000000000001-poison.json'));
 assert.equal(h.githubReads(),0);
 const status=await ingress.backlog(h.env,repo);assert.equal(status.held.count,1);assert.ok(status.last_attempt);assert.ok(status.next_retry);
 assert.equal(status.held_last_pass.last_error,'event_body_unavailable');
}));

test('fair continuation reaches valid input beyond a page of backed off poison pointers',async()=>clock(async advance=>{
 const h=cronHarness();h.state.full=true;
 for(let i=0;i<60;i++)await deliver(h.env,'old-'+String(i).padStart(2,'0'));
 h.state.full=false;
 for(const [key,obj] of h.archive.objects){if(key.includes('/pointers/')){const p=JSON.parse(obj.bytes);p.retry_at=Date.now()+600000;await h.archive.put(key,JSON.stringify(p));}}
 await deliver(h.env,'z-valid',10); // Direct application already has a receipt.
 h.state.full=true;await deliver(h.env,'zz-valid');h.state.full=false;
 await scheduled(h.env);advance(61000);await scheduled(h.env);
 assert.equal(h.state.applied.get('zz-valid'),1,'new event behind 60 deferred items is reached');
 assert.equal(h.state.applied.has('old-00'),false,'future retries were honored');
 advance(600000);await scheduled(h.env);advance(61000);await scheduled(h.env);
 assert.equal(h.state.applied.get('old-00'),1,'expired retry became eligible');
}));

test('raw expiry cannot delete input when pinning fails; gaps and invalid dispositions are durable',async()=>clock(async advance=>{
 const h=cronHarness();h.state.full=true;
 const key='webhooks/2026-10-20/near-expiry.json';
 await h.archive.put(key,JSON.stringify({repository:{id:100}}),{customMetadata:{event:'ping',expires:String(Date.now()-1)}});
 h.archive.failures.push((op,k)=>op==='put' && k.includes('/bodies/'));
 await ingress.maintainRaw(h.env,p=>[repo]);
 assert.ok(h.archive.objects.has(key));
 const gap='control/raw-gaps/near-expiry.json';assert.match(JSON.parse(h.archive.objects.get(gap).bytes).reason,/R2 put failed/);
 advance(61000);await scheduled(h.env);
 assert.ok(h.archive.objects.has(ingress.bodyKey(repo.id,'near-expiry')));
 assert.equal(h.archive.objects.has(gap),false);
 await h.archive.put('webhooks/2026-10-20/bad-json.json','{',{customMetadata:{event:'ping',expires:String(Date.now()-1)}});
 advance(61000);await scheduled(h.env);
 assert.equal(h.archive.objects.has('webhooks/2026-10-20/bad-json.json'),false);
 assert.equal(JSON.parse(h.archive.objects.get('invalid/bad-json.json').bytes).reason,'invalid_json');
 // Missing event metadata is an explicit recoverable gap, never an applied event.
 await h.archive.put('webhooks/2026-10-20/no-metadata.json','{}',{customMetadata:{expires:String(Date.now()-1)}});
 advance(61000);await scheduled(h.env);
 assert.ok(h.archive.objects.has('webhooks/2026-10-20/no-metadata.json'));
 assert.match(JSON.parse(h.archive.objects.get('control/raw-gaps/no-metadata.json').bytes).reason,/raw_event_metadata_unavailable/);
}));

test('status performs no listing or repository/GitHub request, even after maintenance errors',async()=>{
 const h=cronHarness();await ingress.maintainIngress(h.env,repo);
 h.archive.list=()=>{throw new Error('status must not scan');};
 h.env.REPOSITORIES.get=()=>{throw new Error('status must not call repository');};
 assert.equal((await ingress.backlog(h.env,repo)).parked.count,0);
});

test('durable leases coordinate overlapping cron passes and recover after owner death',async()=>clock(async advance=>{
 const h=cronHarness();
 await h.archive.put(ingress.recoveryKey(repo.id),JSON.stringify({lease_until:Date.now()+60000}));
 await ingress.maintainIngress(h.env,repo);assert.equal((await ingress.backlog(h.env,repo)).prepared_at,null);
 advance(61000);
 const lists=[];const list=h.archive.list;h.archive.list=async options=>{lists.push(options.limit);return list(options);};
 await Promise.all([ingress.maintainIngress(h.env,repo),ingress.maintainIngress(h.env,repo)]);
 assert.equal(lists.filter(n=>n===50).length,2,'only the lease owner scans both replay lanes');
 assert.equal(lists.filter(n=>n===1000).length,2,'only the lease owner prepares both counts');
 assert.ok(lists.filter(n=>n===1).length<=4,'contenders only perform bounded lane-selection reads');
 assert.equal((await ingress.backlog(h.env,repo)).lease_until,0);
}));

test('commit-time full probe survives reset, preserves cooldown, isolates cleanup failures, then restarts refresh',async()=>clock(async advance=>{
 const f=repositoryFixture();await new Promise(resolve=>setImmediate(resolve));
 const marker='control/100/storage.json';
 f.state.degraded={since:Date.now(),reason:'storage_full',operation:'commit_failure'};
 await ingress.noteRepositoryFull({ARCHIVE:f.archive},repo.id,'event',Date.now());
 let commits=0;
 f.state.ctx.storage.sync=async()=>{commits++;throw new Error('Exceeded the maximum database size.');};
 await tick(f.state);
 assert.ok(f.state.degraded);assert.equal(commits,1);
 const first=JSON.parse(f.archive.objects.get(marker).bytes);assert.ok(first.next_probe_at>Date.now());
 const reset=repositoryFixture({},0,f.db,f.archive).state;
 await new Promise(resolve=>setImmediate(resolve));
 let probes=0;reset.ctx.storage.sync=async()=>{probes++;};
 // Every bounded expiry fails; recovery must still probe at the durable due time.
 const originalSql=reset.sql;reset.sql=new Proxy(originalSql,{get(target,property){if(property==='exec')return (sql,...args)=>{if(/^DELETE FROM (cursors|repair_metrics|changes|retired_objects)/.test(sql))throw new Error('cleanup unavailable');return target.exec(sql,...args);};return Reflect.get(target,property);}});
 await tick(reset);assert.equal(probes,0,'reset does not bypass persisted cooldown');
 // Cleanup remains broken during successful headroom verification.
 advance(61000);await tick(reset);
 assert.equal(JSON.parse(f.archive.objects.get(marker).bytes).state,'writable');assert.equal(reset.degraded,undefined);
 assert.ok(await reset.ctx.storage.getAlarm(),'ordinary refresh alarm restarted');
}));

test('repeated full probes back off durably and frozen stores never probe or apply',async()=>clock(async advance=>{
 const f=repositoryFixture();await new Promise(resolve=>setImmediate(resolve));
 f.state.degraded={since:Date.now(),reason:'storage_full',operation:'test'};
 let probes=0;f.state.ctx.storage.sync=async()=>{if(f.db.prepare("SELECT COUNT(*) n FROM meta WHERE key LIKE 'storage_probe:%'").get().n){probes++;throw new Error('SQLITE_FULL');}};
 for(let i=0;i<8;i++) {await tick(f.state);const s=JSON.parse(f.archive.objects.get('control/100/storage.json').bytes);assert.ok(s.next_probe_at>Date.now());advance(s.next_probe_at-Date.now());}
 const s=JSON.parse(f.archive.objects.get('control/100/storage.json').bytes);assert.equal(s.probe_attempts,8);assert.equal(probes,8);
 await f.archive.put('control/100/ingress.json',JSON.stringify({mode:'parked'}));await tick(f.state);assert.equal(probes,8);
 f.state.migrationState={phase:'source_verified'};
 const result=await tick(f.state);assert.equal(result.status,503);assert.equal(probes,8);
 const response=await f.state.fetch(new Request('https://internal/event',{method:'POST',body:'{}'}));assert.equal((await response.json()).error,'migration_frozen');
}));

test('a committed receipt survives repeated cleanup failures and SQL receipt expiry',async()=>clock(async advance=>{
 const f=repositoryFixture();await new Promise(resolve=>setImmediate(resolve));
 await f.archive.put('body','{}');
 const p={key:'body',delivery:'forever',event:'ping',repository:repo.name,received:Date.now()};
 const request=()=>new Request('https://internal/event',{method:'POST',body:JSON.stringify(p)});
 assert.equal((await f.state.fetch(request())).status,200);
 assert.ok(f.archive.objects.has(ingress.receiptKey(repo.id,'forever')));
 advance(15*86400000);await f.state.storageMaintenance();
 assert.equal(f.db.prepare('SELECT COUNT(*) n FROM deliveries').get().n,0);
 await f.archive.delete('body');
 const revision=(await f.state.revision());assert.equal((await f.state.fetch(request())).status,200);assert.equal((await f.state.revision()),revision);
}));

test('queue success releases pinned input immediately and R2 cleanup errors remain cron-owned',async()=>{
 const h=harness();h.state.full=true;await deliver(h.env,'queue-clean');h.state.full=false;
 assert.ok(durable(h,'queue-clean'));
 await h.archive.put('control/100/storage.json',JSON.stringify({state:'writable',verified_at:Date.now()}));
 assert.equal((await deliver(h.env,'queue-clean')).acked,true);
 assert.equal(parked(h.archive,'queue-clean').length,0);assert.equal(h.archive.objects.has(ingress.bodyKey(repo.id,'queue-clean')),false);
 h.state.full=true;await deliver(h.env,'queue-clean-fail');h.state.full=false;
 await h.archive.put('control/100/storage.json',JSON.stringify({state:'writable',verified_at:Date.now()}));
 h.archive.failures.push((op,k)=>op==='delete' && k.includes('/bodies/queue-clean-fail'));
 assert.equal((await deliver(h.env,'queue-clean-fail')).acked,true);
 assert.equal(parked(h.archive,'queue-clean-fail').length,1);
 assert.equal((await ingress.drain(h.env,repo)).replayed,1);
});

test('overlapping queue and cron event calls coalesce before the first R2 await',async()=>{
 const f=repositoryFixture();await new Promise(resolve=>setImmediate(resolve));await f.archive.put('body','{}');
 let applications=0;const ingest=f.state.ingest.bind(f.state);f.state.ingest=async p=>{applications++;await new Promise(r=>setImmediate(r));return ingest(p);};
 const p={key:'body',delivery:'overlap',event:'ping',repository:repo.name,received:Date.now()};
 const responses=await Promise.all([1,2,3].map(()=>f.state.fetch(new Request('https://internal/event',{method:'POST',body:JSON.stringify(p)}))));
 assert.ok(responses.every(r=>r.ok));assert.equal(applications,1);
});

test('systemic parking stops preserve poison budgets, and durable full mode avoids repository dispatch',async()=>{
 const h=harness();h.state.full=true;await deliver(h.env,'full-budget');const key=parked(h.archive,'full-budget')[0];
 await ingress.drain(h.env,repo);assert.equal(JSON.parse(h.archive.objects.get(key).bytes).replay_failures,undefined);
 h.env.REPOSITORIES.get=()=>{throw new Error('full maintenance must not dispatch');};
 await ingress.maintainIngress(h.env,repo);assert.equal(JSON.parse(h.archive.objects.get(key).bytes).replay_failures,undefined);
});

test('successful maintenance runs on consecutive cron minutes despite variable R2 latency',async()=>clock(async advance=>{
 const h=cronHarness();await ingress.maintainIngress(h.env,repo);const first=(await ingress.backlog(h.env,repo)).last_attempt;
 advance(59950);await ingress.maintainIngress(h.env,repo);assert.ok((await ingress.backlog(h.env,repo)).last_attempt>first);
}));

test('deadline exhaustion never advances an unvisited lane or skips the rest of a partial page',async()=>clock(async advance=>{
 const h=harness();h.state.full=true;for(let i=0;i<60;i++)await deliver(h.env,'deadline-'+String(i).padStart(2,'0'));h.state.full=false;
 const original=h.env.REPOSITORIES.get().fetch;
 h.env.REPOSITORIES.get=()=>({fetch:async(...args)=>{advance(1000);return original(...args);}});
 const pass=await ingress.drain(h.env,repo,{limit:50,deadline:Date.now()+1});
 assert.equal(pass.replayed,10);assert.ok(pass.cursor.endsWith('-deadline-09.json'));
 const unvisited=await ingress.drain(h.env,repo,{limit:50,cursor:pass.cursor,deadline:Date.now()-1});assert.equal(unvisited.cursor,pass.cursor);assert.equal(unvisited.replayed,0);
 const resumed=await ingress.drain(h.env,repo,{limit:50,cursor:pass.cursor});assert.equal(resumed.replayed,50);assert.equal(h.state.applied.size,60);
}));

test('failed count preparation keeps the last observation timestamp and defaults survive first-owner crash',async()=>clock(async advance=>{
 const h=cronHarness();await ingress.maintainIngress(h.env,repo);const before=await ingress.backlog(h.env,repo);
 advance(61000);const list=h.archive.list;let fail=true;h.archive.list=options=>{if(fail&&options.limit===1000&&options.prefix.includes('/pointers/')){fail=false;throw Error('count list failed');}return list(options);};await ingress.maintainIngress(h.env,repo);
 const after=await ingress.backlog(h.env,repo);assert.equal(after.prepared_at,before.prepared_at);assert.ok(after.last_attempt>before.last_attempt);assert.ok(after.last_error);
 await h.archive.put(ingress.recoveryKey(repo.id),JSON.stringify({lease_until:Date.now()+60000,last_attempt:Date.now()}));
 const fresh=await ingress.backlog(h.env,repo);assert.equal(fresh.parked.count,null);assert.equal(fresh.held.count,null);
 await h.archive.put('control/raw-recovery.json',JSON.stringify({last_error:'other-repository-delivery-secret',prepared_at:1}));
 assert.equal(JSON.stringify(await ingress.backlog(h.env,repo,true)).includes('other-repository-delivery-secret'),false);
}));

test('re-parking preserves held retries and successful probe hysteresis survives an immediate new full report',async()=>clock(async advance=>{
 const h=harness();h.state.full=true;const message=await deliver(h.env,'held-again');
 const normal=parked(h.archive,'held-again')[0],p=JSON.parse(h.archive.objects.get(normal).bytes),held=ingress.pointerKey(repo.id,p,true);p.replay_failures=7;p.retry_at=Date.now()+600000;
 await h.archive.put(held,JSON.stringify(p));await h.archive.delete(normal);await ingress.park(h.env,repo,message.body,'again');
 assert.equal(parked(h.archive,'held-again').length,0);assert.equal(JSON.parse(h.archive.objects.get(held).bytes).replay_failures,7);
 const f=repositoryFixture();await new Promise(resolve=>setImmediate(resolve));f.state.degraded={since:Date.now(),reason:'storage_full',operation:'test'};await tick(f.state);
 const writable=JSON.parse(f.archive.objects.get('control/100/storage.json').bytes);assert.equal(writable.bytes,4*1048576);assert.equal(writable.probe_attempts,1);
 advance(1);await ingress.noteRepositoryFull({ARCHIVE:f.archive},repo.id,'new-event',Date.now());
 const full=JSON.parse(f.archive.objects.get('control/100/storage.json').bytes);assert.equal(full.probe_attempts,1);assert.equal(full.next_probe_at,writable.next_probe_at);
}));

test('raw maintenance uses metadata without downloading committed bodies, promotes SQL receipts, and passes one page',async()=>clock(async advance=>{
 const h=cronHarness();for(let i=0;i<60;i++){
  const d='raw-'+i,bytes=JSON.stringify({repository:{id:100}}),hash=sha(Buffer.from(bytes));
  await h.archive.put('webhooks/2026-10-20/'+d+'.json',bytes,{customMetadata:{event:'ping',sha256:hash,targets:'["100"]',received:'1'}});await h.archive.put(ingress.receiptKey(repo.id,d),'{}');
 }
 const get=h.archive.get;h.archive.get=async key=>{assert.ok(!key.startsWith('webhooks/'));return get(key);};await ingress.maintainRaw(h.env,()=>[repo]);
 assert.ok(JSON.parse(h.archive.objects.get('control/raw-recovery.json').bytes).last_completed);h.archive.get=get;
 await h.archive.put('webhooks/2026-10-20/legacy-known.json','{"repository":{"id":100}}',{customMetadata:{event:'ping'}});
 h.env.REPOSITORIES.get=()=>({fetch:async(url,init)=>{assert.equal(new URL(url).pathname,'/storage/deliveries');return Response.json({known:JSON.parse(init.body).ids});}});
 advance(60000);await ingress.maintainRaw(h.env,()=>[repo]);assert.ok(h.archive.objects.has(ingress.receiptKey(repo.id,'legacy-known')));assert.equal(parked(h.archive,'legacy-known').length,0);
}));

test('raw maintenance dispositions CI deliveries from listed metadata alone and expires them in large pages',async()=>clock(async()=>{
 const h=cronHarness();for(let i=0;i<1500;i++)await h.archive.put('webhooks/2026-10-12/ci-'+String(i).padStart(4,'0')+'.json','{}',{customMetadata:{event:i%2?'workflow_job':'check_run',sha256:'x',targets:'["100"]',received:'1',expires:i<1200?'1':String(Date.now()+86400000)}});
 await h.archive.put('control/raw-gaps/ci-0001.json',JSON.stringify({failures:1,retry_at:0}));
 const get=h.archive.get,head=h.archive.head;
 h.archive.get=async key=>{assert.ok(!key.startsWith('webhooks/') && !key.startsWith('control/raw-gaps/'),key);return get(key);};
 h.archive.head=async key=>{assert.fail('unexpected head '+key);};
 await ingress.maintainRaw(h.env,()=>[repo]);h.archive.get=get;h.archive.head=head;
 const state=JSON.parse(h.archive.objects.get('control/raw-recovery.json').bytes);assert.ok(state.last_completed);
 assert.equal([...h.archive.objects.keys()].filter(k=>k.startsWith('webhooks/')).length,300);
 assert.equal([...h.archive.objects.keys()].filter(k=>k.startsWith('invalid/') || k.startsWith('parked/')).length,0);
 assert.equal(JSON.parse(h.archive.objects.get('control/retention/raw_webhooks.json').bytes).deleted,1200);
 assert.equal(h.archive.objects.has('control/raw-gaps/ci-0001.json'),false);assert.deepEqual(state.gaps,{count:0,truncated:false});
}));

test('raw maintenance recovers independent deliveries concurrently and parks every one',async()=>clock(async()=>{
 const h=cronHarness();for(let i=0;i<25;i++)await h.archive.put('webhooks/2026-10-12/lost-'+String(i).padStart(2,'0')+'.json','{"repository":{"id":100}}',{customMetadata:{event:'issue_comment'}});
 let inflight=0,peak=0;h.env.REPOSITORIES.get=()=>({fetch:async()=>{inflight++;peak=Math.max(peak,inflight);await new Promise(r=>setImmediate(r));inflight--;return Response.json({known:[]});}});
 await ingress.maintainRaw(h.env,()=>[repo]);
 assert.ok(peak>1,'peak '+peak);assert.ok(JSON.parse(h.archive.objects.get('control/raw-recovery.json').bytes).last_completed);
 for(let i=0;i<25;i++)assert.equal(parked(h.archive,'lost-'+String(i).padStart(2,'0')).length,1);
}));

test('permanent receipts cannot delay expiry sweeps for snapshots and staging',async()=>{
 const h=cronHarness();for(let i=0;i<150;i++)await h.archive.put('receipts/100/'+i+'.json','{}');
 await h.archive.put('snapshots/old','{}',{customMetadata:{expires:'1'}});await h.archive.put('staging/old','{}',{customMetadata:{expires:'1'}});
 for(let i=0;i<3;i++)await scheduled(h.env);assert.equal(h.archive.objects.has('snapshots/old'),false);assert.equal(h.archive.objects.has('staging/old'),false);
});

test('scheduled raw reconciliation honors retention policy pins before expiry, while still pinning input',async()=>clock(async advance=>{
 const h=cronHarness();h.state.full=true;
 const key='webhooks/2026-10-20/policy-raw.json';
 await h.archive.put(key,'{"repository":{"id":100}}',{customMetadata:{event:'ping',expires:'1'}});
 const control='control/100/retention-policy.json';await h.archive.put(control,JSON.stringify({mode:'enforce',disabled:[],pinned:['raw_webhooks']}));
 await scheduled(h.env);assert.ok(h.archive.objects.has(key));assert.ok(h.archive.objects.has(ingress.bodyKey(repo.id,'policy-raw')));
 await h.archive.put(control,JSON.stringify({mode:'enforce',disabled:[],pinned:[]}));advance(61000);await scheduled(h.env);
 assert.equal(h.archive.objects.has(key),false);assert.ok(h.archive.objects.has(ingress.bodyKey(repo.id,'policy-raw')));
 assert.equal(JSON.parse(h.archive.objects.get('control/retention/raw_webhooks.json').bytes).deleted,1);
}));

test('SQL receipt archival failure retains the receipt and ordinary storage maintenance retries safely',async()=>clock(async advance=>{
 const f=repositoryFixture();await f.ready;
 f.db.prepare('INSERT INTO deliveries VALUES(?,?,?)').run('archive-failure','push',Date.now()-30*86400000);
 f.archive.failures.push((op,key)=>op==='put' && key===ingress.receiptKey(repo.id,'archive-failure'));
 advance(61000);await f.state.storageMaintenance();
 assert.equal(f.db.prepare('SELECT COUNT(*) n FROM deliveries').get().n,1);
 advance(61000);await f.state.storageMaintenance();
 assert.equal(f.db.prepare('SELECT COUNT(*) n FROM deliveries').get().n,0);assert.ok(f.archive.objects.has(ingress.receiptKey(repo.id,'archive-failure')));
}));

test('receipt archival advances bounded keyset pages through live rows before deleting expired receipts',async()=>clock(async advance=>{
 const f=repositoryFixture();await f.ready;
 for(let i=0;i<200;i++)f.db.prepare('INSERT INTO deliveries VALUES(?,?,?)').run('bounded-'+i,'ping',Date.now()-(i<100?0:30*86400000));
 advance(61000);await f.state.storageMaintenance();
 assert.equal(f.state.retentionState.expired_deliveries.examined,100);assert.equal(f.db.prepare('SELECT COUNT(*) n FROM deliveries').get().n,200);
 advance(61000);await f.state.storageMaintenance();
 assert.equal(f.db.prepare('SELECT COUNT(*) n FROM deliveries').get().n,100);assert.ok(f.archive.objects.has(ingress.receiptKey(repo.id,'bounded-199')));
}));

test('ordinary tick owns SQL receipt archival even when synchronous expiry already ran',async()=>clock(async advance=>{
 const f=repositoryFixture();await f.ready;
 f.db.prepare('INSERT INTO deliveries VALUES(?,?,?)').run('tick-receipt','ping',Date.now()-30*86400000);
 advance(61000);await tick(f.state);await f.state.storageFlight;
 assert.equal(f.db.prepare('SELECT COUNT(*) n FROM deliveries').get().n,0);assert.ok(f.archive.objects.has(ingress.receiptKey(repo.id,'tick-receipt')));
}));

test('ordinary ticks recover a full marker published after construction and retry failed marker reads',async()=>clock(async advance=>{
 const f=repositoryFixture();await f.ready;
 assert.equal(f.state.degraded,undefined);
 await f.archive.put('control/100/storage.json',JSON.stringify({state:'full',since:Date.now(),source:'late_commit_failure'}));
 await tick(f.state);assert.equal(f.state.degraded,undefined);assert.equal(JSON.parse(f.archive.objects.get('control/100/storage.json').bytes).state,'writable');
 advance(61000);
 // Control dependency outage must fence a legacy constructor, then ordinary
 // ticks retry the marker and verify capacity once the dependency recovers.
 f.db.exec("DELETE FROM meta WHERE key='schema_version'");
 for(let i=0;i<2;i++)f.archive.failures.push((op,key)=>op==='get' && key==='control/100/storage.json');
 const reset=repositoryFixture({},0,f.db,f.archive);await reset.ready;
 assert.equal(reset.state.schema,'migration_pending');assert.ok(reset.state.degraded);
 await tick(reset.state);advance(61000);await tick(reset.state);
 assert.equal(reset.state.degraded,undefined);assert.equal(reset.state.schema,'current');
}));

test('a poison pointer cannot hold all SQL receipts indefinitely; explicit class pins still win',async()=>clock(async advance=>{
 const f=repositoryFixture();await f.ready;
 await f.archive.put('parked/100/held/000000000000001-poison.json','{}');
 f.db.prepare('INSERT INTO deliveries VALUES(?,?,?)').run('unrelated-old','ping',Date.now()-30*86400000);
 advance(61000);await f.state.storageMaintenance();
 assert.equal(f.state.receiptsPinned,true,'pending replay remains visible');assert.equal(f.db.prepare('SELECT COUNT(*) n FROM deliveries').get().n,0);
 assert.ok(f.archive.objects.has(ingress.receiptKey(repo.id,'unrelated-old')));
 f.state.retentionPolicy={mode:'enforce',pinned:['expired_deliveries'],disabled:[]};
 f.db.prepare('INSERT INTO deliveries VALUES(?,?,?)').run('explicit-pin','ping',Date.now()-30*86400000);
 advance(61000);await f.state.storageMaintenance();assert.equal(f.db.prepare('SELECT COUNT(*) n FROM deliveries').get().n,1);
}));

test('receipt archive uses a separate I/O budget and bounded concurrency as R2 advances time',async()=>clock(async advance=>{
 const f=repositoryFixture();await f.ready;let active=0,peak=0;
 for(let i=0;i<100;i++)f.db.prepare('INSERT INTO deliveries VALUES(?,?,?)').run('io-'+i,'ping',Date.now()-30*86400000);
 const put=f.archive.put;f.archive.put=async(key,...args)=>{
  if(!key.startsWith('receipts/'))return put(key,...args);
  active++;peak=Math.max(peak,active);advance(5);await new Promise(r=>setImmediate(r));
  try{return await put(key,...args);}finally{active--;}
 };
 advance(61000);await f.state.storageMaintenance();
 assert.equal(f.db.prepare('SELECT COUNT(*) n FROM deliveries').get().n,0);assert.ok(peak>1 && peak<=10);assert.equal(f.state.retentionState.expired_deliveries.examined,100);
}));

test('per-object raw pins survive recovery-owned expiry and page policy reads remain bounded',async()=>clock(async advance=>{
 const h=cronHarness();
 const key='webhooks/2026-10-20/object-pin.json';await h.archive.put(key,'{"repository":{"id":100}}',{customMetadata:{event:'ping',expires:'1',pinned:'true'}});
 for(let i=0;i<20;i++)await h.archive.put('webhooks/2026-10-20/expired-'+i+'.json','{"repository":{"id":100}}',{customMetadata:{event:'ping',expires:'1'}});
 let policyReads=0;const get=h.archive.get;h.archive.get=async key=>{if(key.endsWith('retention-policy.json'))policyReads++;return get(key);};
 await ingress.maintainRaw(h.env,()=>[repo]);
 assert.ok(h.archive.objects.has(key));assert.ok(h.archive.objects.has(ingress.bodyKey(repo.id,'object-pin')));assert.equal(policyReads,1);
 assert.equal([...h.archive.objects.keys()].filter(k=>k.startsWith('webhooks/')).length,1);
}));

test('a transient tick marker-read failure clears on the next read without publishing full or probing',async()=>clock(async advance=>{
 const f=repositoryFixture();await f.ready;
 const key='control/100/storage.json';
 const writable={state:'writable',verified_at:Date.now(),probe_attempts:8,next_probe_at:Date.now()+600000};await f.archive.put(key,JSON.stringify(writable));
 let writes=0,probes=0;const put=f.archive.put;f.archive.put=async(k,...args)=>{if(k===key)writes++;return put(k,...args);};
 f.state.ctx.storage.sync=async()=>{if(f.db.prepare("SELECT COUNT(*) n FROM meta WHERE key LIKE 'storage_probe:%'").get().n)probes++;};
 advance(1000);f.archive.failures.push((op,k)=>op==='get' && k===key);
 await tick(f.state);assert.equal(f.state.degraded.reason,'storage_error');assert.equal(writes,0);assert.equal(probes,0);
 const event=await f.state.fetch(new Request('https://internal/event',{method:'POST',body:JSON.stringify({key:'missing',delivery:'control-wait',event:'ping',repository:repo.name,received:Date.now()})}));
 assert.equal((await event.json()).error,'storage_control_unavailable');
 advance(61000);await tick(f.state);assert.equal(f.state.degraded,undefined);assert.equal(writes,0);assert.equal(probes,0);assert.deepEqual(JSON.parse(f.archive.objects.get(key).bytes),writable);
}));


test('PostgreSQL queue and replay ignore an obsolete SQLite full marker',async()=>{
 const h=harness();h.env.REPOSITORY_BACKEND='postgres';
 await h.archive.put('control/100/storage.json',JSON.stringify({state:'full',since:1}));
 assert.equal((await deliver(h.env,'postgres-after-sqlite-full')).acked,true);
 assert.equal(h.state.applied.has('postgres-after-sqlite-full'),true);
 h.state.full=true;await deliver(h.env,'postgres-replay-after-sqlite-full');h.state.full=false;
 const {maintainIngress,storageState}=await load('src/ingress.ts');
 assert.equal(await storageState(h.env,repo),null);
 await maintainIngress(h.env,repo);
 assert.equal(h.state.applied.has('postgres-replay-after-sqlite-full'),true);
 assert.equal(JSON.parse(h.archive.objects.get('control/100/storage.json').bytes).state,'full','SQLite recovery evidence remains intact');
});

test('identical large resource refreshes reuse immutable spills and do not manufacture revisions',async()=>{
 const {state,db,archive}=repositoryFixture();await state.fixtureReady;
 let uploads=0;const originalPut=archive.put.bind(archive);archive.put=async(key,...args)=>{if(key.startsWith('resources/'))uploads++;return originalPut(key,...args);};
 const data={id:'9223372036854775807',body:'x'.repeat(1600000),created_at:'2026-10-20T00:00:00Z'};
 await state.put('issue_comments',data.id,data,Date.now(),false,'7');
 const original=db.prepare('SELECT data FROM resources WHERE id=?').get(data.id).data,key=JSON.parse(original).$r2,revision=await state.revision();
 assert.match(key,/^resources\/100\/sha256-[a-f0-9]{64}$/);
 archive.objects.get(key).customMetadata.pinned='true';
 for(let n=0;n<16;n++)await state.put('issue_comments',data.id,data,Date.now(),false,'7');
 assert.equal(await state.revision(),revision);
 assert.equal([...archive.objects.keys()].filter(k=>k.startsWith('resources/')).length,1);
 assert.equal(archive.objects.get(key).customMetadata.pinned,'true','conditional creation must preserve an existing pin');
 assert.equal(uploads,1,'identical contents must not repeatedly upload the full payload');
 archive.failures.push((op,k)=>op==='put' && k.startsWith('resources/'));
 await assert.rejects(state.put('issue_comments',data.id,{...data,changed:true},Date.now(),false,'7'));
 assert.equal(await state.revision(),revision,'a failed upload cannot publish a new pointer');
 await state.put('issue_comments',data.id,{...data,changed:true},Date.now(),false,'7');
 assert.equal([...archive.objects.keys()].filter(k=>k.startsWith('resources/')).length,2);
 assert.equal((await state.decode(original)).changed,undefined,'old retained references remain readable');
 assert.equal((await state.decode(db.prepare('SELECT data FROM resources WHERE id=?').get(data.id).data)).changed,true);
});

test('spill reuse preserves legacy references and staging expiry stays independent',async()=>{
 const {state,archive}=repositoryFixture();await state.fixtureReady;
 const data={rows:['x'.repeat(1600000)]},old='resources/100/old-reference';
 await archive.put(old,JSON.stringify(data),{customMetadata:{kind:'resource',expires:'1'}});
 const encoded=await state.encode(data),key=JSON.parse(encoded).$r2;
 // Even incorrectly copied cursor metadata cannot reclassify the new resource key.
 archive.objects.get(key).customMetadata={kind:'cursor',expires:'1'};
 assert.equal((await retention.retirePrefix(archive,'legacy_staging_spills')).deleted,0);
 assert.deepEqual(await state.decode(JSON.stringify({$r2:old})),data);
 assert.deepEqual(await state.decode(encoded),data);
 const expires=Date.now()-1,a=await state.encode(data,expires),b=await state.encode(data,expires);
 assert.notEqual(a,b,'expiring staging uploads cannot share a resource lifetime');
 assert.equal((await retention.retirePrefix(archive,'staging_spills')).deleted,2);
 assert.deepEqual(await state.decode(encoded),data);
});

test('a known frozen source survives startup retries without relaxing ownership fences',async()=>{
 const {db,state}=repositoryFixture();try {
  await state.fixtureReady;
  await state.put('pr','7',{number:7,title:'retained header'});
  await state.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)','pr:{"number":7}','pr','{"number":7}',1,Date.now(),await state.revision(),null);
  const generation='frozen-source';
  state.localSetMeta('backend_owner',{protocol:'s2-selection-v1',backend:'migration',generation});
  state.migrationState={phase:'switching',generation,cutover:{source_fenced:true,target_active:false}};
  state.startup();assert.equal(state.schema,'current');
  const result=await queryFixture(state,{operation:'pr',args:{number:7}});
  assert.equal(result.status,'ok');assert.equal(result.data.title,'retained header');
  await assert.rejects(state.store.exec("INSERT INTO demand VALUES('illegal',1)"),/migration_frozen|generation fenced/);
  await assert.rejects(state.put('pr','8',{number:8}),/migration_frozen|generation fenced/);
  state.migrationState={...state.migrationState,generation:'wrong-generation'};
  state.startup();assert.equal(state.schema.error,'backend_fenced');
  state.migrationState={phase:'complete',generation,cutover:{source_fenced:true,target_active:true}};
  state.startup();assert.equal(state.schema.error,'backend_fenced','activated target cannot fall back to the old source');
 }finally {db.close();}
});

test('native source fences survive fresh constructors and recovered control revalidates fallback reads',async()=>{
 const first=repositoryFixture();await first.fixtureReady;const {db,archive,state}=first;
 try{
  await state.put('pr','7',{number:7,title:'frozen history'});
  await state.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)','pr:{"number":7}','pr','{"number":7}',1,Date.now(),await state.revision(),null);
  const generation='c707ddba-076d-4a63-8696-96533031ce88',checkpoint={version:1,generation,repository:'100',target:'test-target',source:{object:'fixture',deployment:'test-deployment',schema:5},phase:'switching',created:Date.now(),updated:Date.now(),tables:{},failure:null,staging:{after:null,page:'1',checked:0,complete:false,expired_jobs:0,expired_hash:'0'.repeat(64)},blobs:{prefix:0,pages:0,objects:0,hash:'0'.repeat(64),complete:false},cutover:{environment:'test',lease_until:Date.now()+900000,epoch:0,source_fenced:false,target_active:false,import:{},verify:{},acceptance:''}};
  await state.fenceMigration(checkpoint);checkpoint.cutover.source_fenced=true;
  for(const fault of [false,true])for(const unavailable of [false,true])for(const adminRetry of [false,true])for(const activated of [false,true]){
   await archive.put('control/100/migration.json',JSON.stringify(checkpoint));
   if(unavailable)archive.failures.push((op,key)=>op==='get' && key==='control/100/migration.json');
   const reset=repositoryFixture({MIGRATION_ENVIRONMENT:'test',FAULT_INJECTION:fault?'true':undefined},1,db,archive);await reset.ready;
   assert.equal((await queryFixture(reset.state,{operation:'pr',args:{number:7}})).data.title,'frozen history');
   await assert.rejects(reset.state.setMeta('forbidden',true),/migration_frozen|generation fenced/);
   assert.throws(()=>db.prepare("INSERT INTO demand VALUES('native-trigger',1)").run(),/generation fenced/);
   if(unavailable){
    await archive.put('control/100/migration.json',JSON.stringify({...checkpoint,generation:'c707ddba-076d-4a63-8696-96533031ce89',cutover:{...checkpoint.cutover,target_active:activated}}));
    if(adminRetry)await reset.state.fetch(new Request('https://internal/migration',{method:'POST',body:JSON.stringify({operation:'status'})}));
    else await reset.state.retryMigrationControl();
    assert.equal((await queryFixture(reset.state,{operation:'pr',args:{number:7}})).error,'backend_fenced','recovered control rechecks previously current fallback');
   }
  }
 }finally{db.close();}
});

test('test-migration constructor waits for a full marker before attempting a legacy schema upgrade',async()=>{
 const first=repositoryFixture();await first.fixtureReady;const {db,archive}=first;
 try{
  db.prepare("DELETE FROM meta WHERE key='schema_version'").run();
  await archive.put('control/100/storage.json',JSON.stringify({state:'full',since:Date.now(),source:'queue'}));
  const reset=repositoryFixture({MIGRATION_ENVIRONMENT:'test'},1,db,archive);await reset.ready;
  assert.equal(db.prepare("SELECT COUNT(*) n FROM meta WHERE key='schema_version'").get().n,0);
  assert.equal(reset.state.schema,'migration_pending');assert.ok(reset.state.degraded);
 }finally{db.close();}
});

test('parked CI receipts batch without semantic changes and survive response/cleanup crashes',async()=>{
 const f=repositoryFixture();await f.ready;
 let batches=0,individual=0,lost=true;
 const namespace={idFromName:()=>({toString:()=>f.state.ctx.id.toString()}),get:()=>({fetch:async(url,init)=>{
  const path=new URL(url).pathname;
  if(path==='/ci-receipts')batches++;else if(path==='/event')individual++;
  const response=await f.state.fetch(new Request(url,init));
  if(path==='/ci-receipts'&&response.ok&&lost){lost=false;throw Error('response lost after committed batch');}
  return response;
 }})};
 f.state.env.REPOSITORIES=namespace;
 const env={ALLOWED_REPOSITORIES:JSON.stringify([repo]),ARCHIVE:f.archive,REPOSITORIES:namespace},now=Date.now(),items=[];
 for(let i=0;i<25;i++){
  const p={repository:repo.name,delivery:'old-ci-'+i,event:i%2?'check_run':'workflow_job',received:now-10000+i,key:'webhooks/ci/'+i};
  await f.archive.put(p.key,'{}');await ingress.park(env,repo,p,'migration');items.push(p);
 }
 const waiting=items.at(-1),waitingKey=ingress.pointerKey(repo.id,waiting),value=await(await f.archive.get(waitingKey)).json();
 await f.archive.put(waitingKey,JSON.stringify({...value,retry_at:now+600000}));
 f.archive.failures.push((op,key)=>op==='put'&&key===ingress.receiptKey(repo.id,'old-ci-0'));
 f.archive.failures.push((op,key)=>op==='delete'&&key===ingress.bodyKey(repo.id,'old-ci-1'));
 const revision=await f.state.revision(),first=await ingress.drain(env,repo,{limit:100});
 assert.equal(batches,1);assert.ok(individual>0,'response loss uses existing deduplication');
 assert.equal(first.failed,2);assert.equal(f.db.prepare('SELECT COUNT(*) n FROM deliveries').get().n,24);
 assert.equal(f.db.prepare('SELECT COUNT(*) n FROM changes').get().n,0);assert.equal(await f.state.revision(),revision);
 for(const p of items.slice(0,-1))assert.equal(f.db.prepare('SELECT event FROM deliveries WHERE id=?').get(p.delivery).event,'ignored:'+p.event);
 assert.ok(await f.archive.get(ingress.bodyKey(repo.id,'old-ci-0')),'SQL commit alone cannot release the body before R2 receipt publication');
 assert.ok(await f.archive.get(ingress.pointerKey(repo.id,items[1])),'cleanup interruption retains the pointer');
 assert.equal(await f.archive.get(ingress.receiptKey(repo.id,waiting.delivery)),null);
 for(const p of items.slice(0,-1)){const key=ingress.pointerKey(repo.id,p),o=await f.archive.get(key);if(o){const v=await o.json();await f.archive.put(key,JSON.stringify({...v,retry_at:0}));}}
 const second=await ingress.drain(env,repo,{limit:100});assert.equal(second.failed,0);assert.equal(f.db.prepare('SELECT COUNT(*) n FROM deliveries').get().n,24);
 for(const p of items.slice(0,-1)){assert.ok(await f.archive.get(ingress.receiptKey(repo.id,p.delivery)));assert.equal(await f.archive.get(ingress.pointerKey(repo.id,p)),null);assert.equal(await f.archive.get(ingress.bodyKey(repo.id,p.delivery)),null);}
 assert.ok(await f.archive.get(waitingKey));assert.equal(await f.state.revision(),revision);
});

test('CI receipt endpoint rejects semantic input, wrong object, paused ingress and source fence',async()=>{
 const f=repositoryFixture();await f.ready;f.state.env.REPOSITORIES={idFromName:()=>({toString:()=>f.state.ctx.id.toString()})};
 const send=body=>f.state.fetch(new Request('https://internal/ci-receipts',{method:'POST',body:JSON.stringify(body)})),event={delivery:'bounded-ci',event:'workflow_run',received:Date.now()},input={repository:repo.name,events:[event]};
 for(const events of[[],Array(101).fill(event),[{...event,event:'issue_comment'}],[{...event,received:1.5}],[{...event,delivery:'bad/id'}],[event,{...event,event:'check_run'}]])assert.equal((await send({...input,events})).status,400);
 f.state.env.REPOSITORIES={idFromName:()=>({toString:()=>'other-object'})};assert.equal((await send(input)).status,400);
 f.state.env.REPOSITORIES={idFromName:()=>({toString:()=>f.state.ctx.id.toString()})};
 f.state.ingress='parked';assert.equal((await send(input)).status,503);assert.equal(f.db.prepare('SELECT COUNT(*) n FROM deliveries').get().n,0);
 f.state.ingress='apply';f.state.migrationState={phase:'exporting'};assert.equal((await send(input)).status,503);assert.equal(f.db.prepare('SELECT COUNT(*) n FROM deliveries').get().n,0);
 f.state.migrationState=null;assert.equal((await send(input)).status,200);assert.equal(f.db.prepare('SELECT COUNT(*) n FROM deliveries').get().n,1);
 assert.equal((await send(input)).status,200);assert.equal(f.db.prepare('SELECT COUNT(*) n FROM deliveries').get().n,1);assert.equal(f.db.prepare('SELECT COUNT(*) n FROM changes').get().n,0);
});

test('CI batches respect SQLite bindings, storage refusal and an in-flight ingress pause',async()=>{
 const f=repositoryFixture();await f.ready;
 f.state.env.REPOSITORIES={idFromName:()=>({toString:()=>f.state.ctx.id.toString()})};
 const events=Array.from({length:100},(_,i)=>({delivery:'bounded-'+i,event:'workflow_run',received:Date.now()}));
 const send=()=>f.state.fetch(new Request('https://internal/ci-receipts',{method:'POST',body:JSON.stringify({repository:repo.name,events})}));
 for(const reason of ['storage_full','storage_error']){
  f.state.degraded={since:Date.now(),reason};const response=await send();
  assert.equal(response.status,503);assert.equal((await response.json()).parkable,true);
  assert.equal(f.db.prepare('SELECT COUNT(*) n FROM deliveries').get().n,0);
 }
 f.state.degraded=undefined;f.state.inputFailure={at:Date.now(),reason:'old_failure'};
 const exec=f.state.ctx.storage.sql.exec;let largest=0;
 f.state.ctx.storage.sql.exec=(sql,...bindings)=>{largest=Math.max(largest,bindings.length);assert.ok(bindings.length<=100);return exec(sql,...bindings);};
 let release,entered;const gate=new Promise(r=>release=r),syncing=new Promise(r=>entered=r);
 f.state.ctx.storage.sync=async()=>{entered();await gate;};
 const batch=send();await syncing;
 assert.equal(f.state.applying,1);let paused=false;
 const pause=f.state.fetch(new Request('https://internal/storage/ingress',{method:'POST',body:JSON.stringify({mode:'parked'})})).then(async r=>{paused=true;return r.json();});
 await new Promise(r=>setTimeout(r,75));assert.equal(paused,false,'pause must wait through durable sync');
 release();assert.equal((await batch).status,200);
 assert.deepEqual(await pause,{mode:'parked',in_flight:0,drained:true});
 assert.equal(f.state.inputFailure,undefined);assert.equal(f.db.prepare('SELECT COUNT(*) n FROM deliveries').get().n,100);
 assert.equal(largest,99);assert.equal(f.db.prepare('SELECT COUNT(*) n FROM changes').get().n,0);
});

test('a late ingress-control read cannot undo a completed pause',async()=>{
 for(const path of ['/ci-receipts','/event']){
  const f=repositoryFixture();await f.ready;f.state.ingress=undefined;
  f.state.env.REPOSITORIES={idFromName:()=>({toString:()=>f.state.ctx.id.toString()})};
  let release,entered;const gate=new Promise(r=>release=r),reading=new Promise(r=>entered=r),get=f.archive.get;
  f.archive.get=async key=>{if(key==='control/100/ingress.json'){entered();await gate;return{json:async()=>({mode:'apply'})};}return get(key);};
  const event={delivery:'late-control',event:'check_run',received:Date.now()},body=path==='/event'?{...event,repository:repo.name,key:'unused'}:{repository:repo.name,events:[event]};
  const pending=f.state.fetch(new Request('https://internal'+path,{method:'POST',body:JSON.stringify(body)}));
  await reading;
  const pause=await f.state.fetch(new Request('https://internal/storage/ingress',{method:'POST',body:JSON.stringify({mode:'parked'})}));
  assert.equal((await pause.json()).drained,true);release();
  assert.equal((await pending).status,503);assert.equal(f.state.ingress,'parked');
  assert.equal(f.db.prepare('SELECT COUNT(*) n FROM deliveries').get().n,0);
 }
});

test('CI replay survives an old actor and a deadline immediately after batch commit',async()=>clock(async advance=>{
 const f=repositoryFixture();await f.ready;let oldActor=true,expire=false,batches=0,individual=0;
 const namespace={idFromName:()=>({toString:()=>f.state.ctx.id.toString()}),get:()=>({fetch:async(url,init)=>{
  const path=new URL(url).pathname;
  if(path==='/ci-receipts'){batches++;if(oldActor)return new Response(null,{status:404});}else if(path==='/event')individual++;
  const response=await f.state.fetch(new Request(url,init));
  if(path==='/ci-receipts' && expire){expire=false;advance(2000);}
  return response;
 }})};f.state.env.REPOSITORIES=namespace;
 const env={ALLOWED_REPOSITORIES:JSON.stringify([repo]),ARCHIVE:f.archive,REPOSITORIES:namespace};
 const park=async delivery=>{const p={repository:repo.name,delivery,event:'check_run',received:Date.now(),key:'webhooks/'+delivery};await f.archive.put(p.key,'{}');await ingress.park(env,repo,p,'migration');return p;};
 const old=await park('old-actor');assert.equal((await ingress.drain(env,repo)).replayed,1);
 assert.equal(individual,1);assert.equal((await(await f.archive.get(ingress.receiptKey(repo.id,old.delivery))).json()).disposition,'not_mirrored');
 oldActor=false;f.state.ctx.storage.sync=async()=>advance(7);const pending=await park('deadline-commit');expire=true;
 const first=await ingress.drain(env,repo,{deadline:Date.now()+1000});assert.equal(first.stopped,'deadline');assert.equal(first.replayed,0);
 assert.equal(first.timing.batch_ms,2007);assert.equal(first.timing.batch_lookup_ms,undefined);assert.equal(first.timing.batch_write_ms,7);assert.equal(first.timing.cleanup_ms,0);
 assert.equal(f.db.prepare('SELECT COUNT(*) n FROM deliveries WHERE id=?').get(pending.delivery).n,1);
 assert.ok(await f.archive.get(ingress.pointerKey(repo.id,pending)));assert.ok(await f.archive.get(ingress.bodyKey(repo.id,pending.delivery)));
 assert.equal(await f.archive.get(ingress.receiptKey(repo.id,pending.delivery)),null);
 assert.equal((await ingress.drain(env,repo)).replayed,1);assert.equal(batches,3);assert.equal(individual,1);
 assert.equal(f.db.prepare('SELECT COUNT(*) n FROM deliveries').get().n,2);assert.equal(f.db.prepare('SELECT COUNT(*) n FROM changes').get().n,0);
 assert.equal(await f.archive.get(ingress.pointerKey(repo.id,pending)),null);
}));

test('ordinary replay learns a smaller durable page after repeated zero-progress deadlines',async()=>clock(async advance=>{
 const h=harness(),committed=new Map(),sizes=[],cleanup=[];let slow=true;
 h.env.REPOSITORIES.get=()=>({fetch:async(url,init)=>{
  assert.equal(new URL(url).pathname,'/ci-receipts');
  const rows=JSON.parse(init.body).events;sizes.push(rows.length);
  for(const p of rows)if(!committed.has(p.delivery))committed.set(p.delivery,1);
  if(slow && rows.length>2)advance(21000);else advance(1000);
  return Response.json({committed:rows.map(p=>p.delivery)});
 }});
 for(let i=0;i<12;i++){
  const p={repository:repo.name,delivery:'adaptive-'+String(i).padStart(2,'0'),event:'check_run',received:Date.now(),key:'webhooks/adaptive-'+i};
  await h.archive.put(p.key,'{}');await ingress.park(h.env,repo,p,'migration');
 }
 const remove=h.archive.delete;
 h.archive.delete=async keys=>{for(const key of Array.isArray(keys)?keys:[keys])if(key.includes('/bodies/')){
  const delivery=key.slice(key.lastIndexOf('/')+1).replace(/\.json$/,'');
  assert.ok(committed.has(delivery));assert.ok(await h.archive.get(ingress.receiptKey(repo.id,delivery)));cleanup.push(delivery);
 }return remove(keys);};
 // A held cooldown is independent of the normal lane's learned page size.
 const delayed={repository:repo.name,delivery:'adaptive-held',event:'check_run',received:Date.now(),key:'webhooks/adaptive-held'};
 await h.archive.put(delayed.key,'{}');await ingress.park(h.env,repo,delayed,'migration');
 const original=await(await h.archive.get(ingress.pointerKey(repo.id,delayed))).json(),retry=Date.now()+3600000;
 await h.archive.put(ingress.pointerKey(repo.id,delayed,true),JSON.stringify({...original,retry_at:retry,replay_failures:5}));
 await remove(ingress.pointerKey(repo.id,delayed));
 await ingress.maintainIngress(h.env,repo);
 let state=await(await h.archive.get(ingress.recoveryKey(repo.id))).json();
 assert.equal(state.pointers_last_pass.replayed,0);assert.equal(state.pointers_last_pass.stopped,'deadline');
 assert.equal(state.pointers_last_pass.page_size,12);assert.equal(cleanup.length,0);
 // Each call loads a fresh module/process, and only the ordinary supervisor
 // resumes work. No explicit admin replay, cursor reset or cooldown bypass.
 for(let n=0;n<20 && [...h.archive.objects.keys()].some(k=>k.startsWith('parked/100/pointers/'));n++){
  advance(31000);await(await load('src/ingress.ts')).maintainIngress(h.env,repo);
 }
 state=await(await h.archive.get(ingress.recoveryKey(repo.id))).json();
 assert.equal([...h.archive.objects.keys()].filter(k=>k.startsWith('parked/100/pointers/')).length,0);
 assert.equal(committed.size,12);assert.ok([...committed.values()].every(n=>n===1));
 assert.equal(cleanup.length,12);assert.deepEqual(sizes.slice(0,3),[12,6,3]);assert.ok(sizes.slice(3).every(n=>n===1));
 assert.equal(state.pointers_page_limit,2);assert.equal(state.pointers_page_ceiling,2);assert.ok(state.pointers_page_ceiling_until>Date.now());
 assert.equal(state.held_page_limit,undefined);assert.equal(state.held_page_ceiling,undefined);
 assert.equal((await(await h.archive.get(ingress.pointerKey(repo.id,delayed,true))).json()).retry_at,retry);
 assert.ok(await h.archive.get(ingress.bodyKey(repo.id,delayed.delivery)));assert.equal(committed.has(delayed.delivery),false);
 assert.equal(state.lease_until,0);
 // A temporary slowdown must not impose a permanent throughput ceiling.
 slow=false;advance(15*60000+1);
 for(let i=0;i<24;i++){const p={repository:repo.name,delivery:'healthy-'+String(i).padStart(2,'0'),event:'check_run',received:Date.now(),key:'webhooks/healthy-'+i};await h.archive.put(p.key,'{}');await ingress.park(h.env,repo,p,'migration');}
 for(let n=0;n<5 && [...h.archive.objects.keys()].some(k=>k.startsWith('parked/100/pointers/'));n++){advance(31000);await ingress.maintainIngress(h.env,repo);}
 state=await(await h.archive.get(ingress.recoveryKey(repo.id))).json();assert.ok((state.pointers_page_limit ?? 100)>2);assert.equal(state.pointers_page_ceiling,undefined);
 assert.equal(cleanup.length,36);assert.equal(committed.size,36);
}));

test('a reset after the learned lease claim preserves its limit and cursor',async()=>clock(async advance=>{
 const h=harness();h.state.full=true;for(const d of ['learned-a','learned-b','learned-c','learned-d'])await deliver(h.env,d);h.state.full=false;
 await h.archive.put('control/100/storage.json',JSON.stringify({state:'writable',verified_at:Date.now()}));
 const key=ingress.recoveryKey(repo.id),cursor='parked/100/pointers/000000000000000-before.json';
 await h.archive.put(key,JSON.stringify({pointers_cursor:cursor,pointers_last_pass:{replayed:0,failed:0,stopped:'deadline',page_size:4,page_limit:50,budget_ms:20000}}));
 const put=h.archive.put;let lost=true;
 h.archive.put=async(k,...args)=>{const result=await put(k,...args);if(k===key&&lost){lost=false;throw Error('reset after learned lease');}return result;};
 await assert.rejects(ingress.maintainIngress(h.env,repo),/reset after learned lease/);
 const claim=await(await h.archive.get(key)).json();assert.equal(claim.pointers_page_limit,2);assert.equal(claim.pointers_cursor,cursor);assert.equal(h.state.applied.size,0);
 advance(61000);await(await load('src/ingress.ts')).maintainIngress(h.env,repo);
 assert.equal(h.state.applied.size,4);assert.ok([...h.state.applied.values()].every(n=>n===1));
 assert.equal((await(await h.archive.get(key)).json()).lease_until,0);
}));

test('expired owners that lose every slow completion still converge without an admin retry',async()=>clock(async advance=>{
 const h=harness(),key=ingress.recoveryKey(repo.id),committed=new Set(),sizes=[];
 h.env.REPOSITORIES.get=()=>({fetch:async(url,init)=>{
  assert.equal(new URL(url).pathname,'/ci-receipts');const rows=JSON.parse(init.body).events;sizes.push(rows.length);
  for(const p of rows)committed.add(p.delivery);
  if(rows.length>2){
   advance(61000);const owner=await(await h.archive.get(key)).json();
   // Another controller has fenced this completion. No slow pass gets to
   // publish its observed size/deadline, only its original lease claim survives.
   await h.archive.put(key,JSON.stringify({...owner,replaced:true,lease_until:Date.now()}));
  }else advance(1000);
  return Response.json({committed:rows.map(p=>p.delivery)});
 }});
 for(let i=0;i<16;i++){const p={repository:repo.name,delivery:'expired-page-'+String(i).padStart(2,'0'),event:'check_run',received:Date.now(),key:'webhooks/expired-page-'+i};await h.archive.put(p.key,'{}');await ingress.park(h.env,repo,p,'migration');}
 await ingress.maintainIngress(h.env,repo);
 const first=await(await h.archive.get(key)).json();assert.equal(first.pointers_last_pass,undefined);assert.equal(first.replay_inflight.limit,50);assert.equal(first.replay_inflight.lane,'pointers');
 for(let n=0;n<20 && [...h.archive.objects.keys()].some(k=>k.startsWith('parked/100/pointers/'));n++){
  advance(31000);await(await load('src/ingress.ts')).maintainIngress(h.env,repo);
 }
 assert.equal([...h.archive.objects.keys()].filter(k=>k.startsWith('parked/100/pointers/')).length,0);assert.equal(committed.size,16);
 const state=await(await h.archive.get(key)).json();assert.equal(state.pointers_page_limit,2);assert.equal(state.pointers_page_ceiling,undefined);
 assert.deepEqual(sizes.slice(0,6),[16,16,12,6,3,1]);assert.ok(sizes.slice(6).every(n=>n===1));assert.equal(state.lease_until,0);
 assert.equal(state.held_page_limit,undefined);assert.equal(state.held_page_ceiling,undefined);
 for(let i=0;i<16;i++){const delivery='expired-page-'+String(i).padStart(2,'0');assert.ok(await h.archive.get(ingress.receiptKey(repo.id,delivery)));assert.equal(await h.archive.get(ingress.bodyKey(repo.id,delivery)),null);}
}));

test('learned limits grow only with fast full pages at the cap and respect caller limits',async()=>clock(async advance=>{
 const h=harness(),key=ingress.recoveryKey(repo.id),sizes=[];h.state.full=true;
 for(let i=0;i<80;i++)await deliver(h.env,'growth-'+String(i).padStart(2,'0'));h.state.full=false;
 await h.archive.put('control/100/storage.json',JSON.stringify({state:'writable',verified_at:Date.now()}));
 await h.archive.put(key,JSON.stringify({pointers_page_limit:2,held_page_limit:1}));
 const list=h.archive.list;h.archive.list=async options=>{if(options.prefix==='parked/100/pointers/'&&options.startAfter!==undefined)sizes.push(options.limit);return list(options);};
 const original=h.env.REPOSITORIES.get();h.env.REPOSITORIES.get=()=>({fetch:async(...args)=>{advance(500);return original.fetch(...args);}});
 for(let n=0;n<3;n++){assert.equal((await ingress.replayIngress(h.env,repo,{limit:1})).replayed,1);}
 assert.equal((await(await h.archive.get(key)).json()).pointers_page_limit,2,'smaller explicit requests cannot grow the cap');
 await ingress.maintainIngress(h.env,repo);advance(31000);await ingress.maintainIngress(h.env,repo);
 const state=await(await h.archive.get(key)).json();assert.ok((state.pointers_page_limit ?? 100)>2);assert.equal(state.held_page_limit,1);
 assert.ok(sizes.some(n=>n>2));assert.ok(sizes.every(n=>n<=50));assert.equal(h.state.applied.size,80);
}));

test('legacy CI deadline evidence is usable without penalizing a starved shared-budget lane',async()=>{
 const h=harness(),key=ingress.recoveryKey(repo.id);
 await h.archive.put(key,JSON.stringify({
  pointers_last_pass:{replayed:0,failed:0,stopped:'deadline',batched:8},
  held_last_pass:{replayed:0,failed:0,stopped:'deadline',page_size:10,page_limit:50,budget_ms:1000},
 }));
 const result=await ingress.replayIngress(h.env,repo,{limit:1});assert.equal(result.replayed,0);assert.equal(result.remaining,false);
 const state=await(await h.archive.get(key)).json();assert.equal(state.pointers_page_limit,25);assert.equal(state.pointers_page_ceiling,undefined);
 assert.equal(state.held_page_limit,undefined);assert.equal(state.held_page_ceiling,undefined);
 assert.equal(result.page_limit,1);assert.equal(state.replay_inflight,undefined);
});

test('an expired one-pointer admin probe does not reduce automatic replay or its other lane',async()=>clock(async advance=>{
 const h=harness(),key=ingress.recoveryKey(repo.id);h.state.full=true;
 for(let i=0;i<80;i++)await deliver(h.env,'cancelled-probe-'+i);h.state.full=false;
 await h.archive.put('control/100/storage.json',JSON.stringify({state:'writable',verified_at:Date.now()}));
 const put=h.archive.put;let lost=true;h.archive.put=async(k,...args)=>{const value=await put(k,...args);if(k===key&&lost){lost=false;throw Error('cancelled admin claim');}return value;};
 await assert.rejects(ingress.replayIngress(h.env,repo,{limit:1}),/cancelled admin claim/);assert.equal(h.state.applied.size,0);
 const list=h.archive.list,sizes=[];h.archive.list=async options=>{if(options.prefix==='parked/100/pointers/'&&options.limit>1&&options.limit<=100)sizes.push(options.limit);return list(options);};
 advance(61000);await ingress.maintainIngress(h.env,repo);
 assert.deepEqual(sizes,[50,50]);assert.equal(h.state.applied.size,80);
 const state=await(await h.archive.get(key)).json();for(const lane of ['pointers','held']){assert.equal(state[lane+'_page_limit'],undefined);assert.equal(state[lane+'_page_ceiling'],undefined);}
}));

test('an expired completion after progress recovers throughput without a ceiling or duplicate input',async()=>clock(async advance=>{
 const h=harness(),key=ingress.recoveryKey(repo.id);h.state.full=true;for(let i=0;i<4;i++)await deliver(h.env,'completed-before-reset-'+i);h.state.full=false;
 await h.archive.put('control/100/storage.json',JSON.stringify({state:'writable',verified_at:Date.now()}));
 const put=h.archive.put;let lost=true;h.archive.put=async(k,body,...args)=>{if(k===key&&lost&&JSON.parse(body).lease_until===0){lost=false;throw Error('lost final checkpoint');}return put(k,body,...args);};
 await assert.rejects(ingress.maintainIngress(h.env,repo),/lost final checkpoint/);assert.equal(h.state.applied.size,4);
 h.state.full=true;for(let i=0;i<200;i++)await deliver(h.env,'after-progress-'+i);h.state.full=false;
 await h.archive.put('control/100/storage.json',JSON.stringify({state:'writable',verified_at:Date.now()}));
 advance(61000);await ingress.maintainIngress(h.env,repo);
 const state=await(await h.archive.get(key)).json();assert.equal(h.state.applied.size,204);assert.ok([...h.state.applied.values()].every(n=>n===1));
 assert.ok((state.pointers_page_limit ?? 100)>25);assert.equal(state.pointers_page_ceiling,undefined);assert.equal(state.held_page_limit,undefined);
}));

test('a full source does not attribute an expired claim to either unattempted lane',async()=>clock(async advance=>{
 const h=harness(),key=ingress.recoveryKey(repo.id);h.state.full=true;await deliver(h.env,'still-full-adaptive');
 await h.archive.put(key,JSON.stringify({lease_until:Date.now()-1,replay_inflight:{lane:'pointers',limit:50,kind:'cron'},pointers_page_limit:50,held_page_limit:50}));
 advance(1000);await ingress.maintainIngress(h.env,repo);
 const state=await(await h.archive.get(key)).json();assert.equal(state.pointers_page_limit,50);assert.equal(state.held_page_limit,50);assert.equal(state.replay_inflight,undefined);assert.equal(h.state.applied.size,0);
}));

test('both lanes retain their cadence and a lost handoff response preserves the completed lane',async()=>clock(async advance=>{
 const h=harness(),key=ingress.recoveryKey(repo.id);h.state.full=true;
 for(const d of ['handoff-normal','handoff-held'])await deliver(h.env,d);h.state.full=false;
 await h.archive.put('control/100/storage.json',JSON.stringify({state:'writable',verified_at:Date.now()}));
 const held=(await(await h.archive.get(parked(h.archive,'handoff-held')[0])).json());
 await h.archive.put(ingress.pointerKey(repo.id,held,true),JSON.stringify(held));await h.archive.delete(ingress.pointerKey(repo.id,held));
 const put=h.archive.put;let lost=true;
 h.archive.put=async(k,body,...args)=>{const value=await put(k,body,...args);if(k===key&&lost&&JSON.parse(body).replay_inflight?.lane==='held'){lost=false;throw Error('lost lane handoff acknowledgement');}return value;};
 await ingress.maintainIngress(h.env,repo);
 const pending=await(await h.archive.get(key)).json();assert.equal(pending.replay_inflight.lane,'held');assert.ok(pending.pointers_pass_completed_at);
 assert.equal(h.state.applied.get('handoff-normal'),1);assert.equal(h.state.applied.has('handoff-held'),false);assert.ok(await h.archive.get(ingress.receiptKey(repo.id,'handoff-normal')));
 advance(61000);await ingress.maintainIngress(h.env,repo);
 assert.equal(h.state.applied.size,2);assert.ok([...h.state.applied.values()].every(n=>n===1));
 const state=await(await h.archive.get(key)).json();assert.equal(state.pointers_page_limit,undefined);assert.equal(state.pointers_page_ceiling,undefined);assert.ok(state.held_pass_completed_at);
 assert.equal(state.held_page_limit,25,'a lost handoff acknowledgement is uncertain evidence for only its named lane');
 // An all-cooldown held lane still leaves time for normal input in this tick.
 h.state.full=true;await deliver(h.env,'next-normal');await deliver(h.env,'cooldown-held');h.state.full=false;
 await h.archive.put('control/100/storage.json',JSON.stringify({state:'writable',verified_at:Date.now()}));
 const p=await(await h.archive.get(parked(h.archive,'cooldown-held')[0])).json(),retry=Date.now()+3600000;
 await h.archive.put(ingress.pointerKey(repo.id,p,true),JSON.stringify({...p,retry_at:retry}));await h.archive.delete(ingress.pointerKey(repo.id,p));
 advance(31000);await ingress.maintainIngress(h.env,repo);
 assert.equal(h.state.applied.get('next-normal'),1);assert.equal(h.state.applied.has('cooldown-held'),false);
 assert.equal((await(await h.archive.get(ingress.pointerKey(repo.id,p,true))).json()).retry_at,retry);
}));

test('sustained fast backlog cannot extend a learned limit beyond its original expiry',async()=>clock(async advance=>{
 const h=harness(),key=ingress.recoveryKey(repo.id),until=Date.now()+15*60000;
 h.state.full=true;for(let i=0;i<160;i++)await deliver(h.env,'busy-expiry-'+i);h.state.full=false;
 await h.archive.put(key,JSON.stringify({pointers_page_limit:2,pointers_page_limit_until:until,pointers_page_ceiling:2,pointers_page_ceiling_until:until}));
 for(let i=0;i<8;i++)await ingress.replayIngress(h.env,repo,{limit:100});
 let state=await(await h.archive.get(key)).json();assert.equal(state.pointers_page_limit,2);assert.equal(state.pointers_page_limit_until,until);
 advance(15*60000+1);const pass=await ingress.replayIngress(h.env,repo,{limit:100});assert.equal(pass.page_limit,100);assert.equal(pass.replayed,100);
 state=await(await h.archive.get(key)).json();assert.equal(state.pointers_page_ceiling,undefined);assert.equal(state.pointers_page_limit,undefined);
}));

test('quiet held limits expire without successful full pages and optional selection failures do not delay input',async()=>clock(async advance=>{
 const h=harness(),key=ingress.recoveryKey(repo.id);h.state.full=true;await deliver(h.env,'quiet-held');await deliver(h.env,'selection-fallback');h.state.full=false;
 await h.archive.put('control/100/storage.json',JSON.stringify({state:'writable',verified_at:Date.now()}));
 const p=await(await h.archive.get(parked(h.archive,'quiet-held')[0])).json(),retry=Date.now()+3600000;
 await h.archive.put(ingress.pointerKey(repo.id,p,true),JSON.stringify({...p,retry_at:retry}));await h.archive.delete(ingress.pointerKey(repo.id,p));
 await h.archive.put(key,JSON.stringify({next_lane:'held',held_page_limit:1,held_page_limit_until:Date.now()+15*60000,held_page_ceiling:1,held_page_ceiling_until:Date.now()+15*60000}));
 advance(16*60000);const list=h.archive.list;let failed=false;
 h.archive.list=options=>{if(!failed&&options.limit===1){failed=true;throw Error('optional lane availability unavailable');}return list(options);};
 await ingress.maintainIngress(h.env,repo);
 const state=await(await h.archive.get(key)).json();assert.equal(failed,true);assert.equal(state.last_error,null);assert.equal(state.held_page_limit,undefined);assert.equal(state.held_page_ceiling,undefined);
 assert.equal(h.state.applied.get('selection-fallback'),1);assert.equal(h.state.applied.has('quiet-held'),false);assert.equal((await(await h.archive.get(ingress.pointerKey(repo.id,p,true))).json()).retry_at,retry);
}));

test('lost cleanup acknowledgements cannot recreate committed CI pointers or replace receipts',async()=>clock(async advance=>{
 const f=repositoryFixture();await f.ready;
 const ns={idFromName:()=>({toString:()=>f.state.ctx.id.toString()}),get:()=>({fetch:(url,init)=>f.state.fetch(new Request(url,init))})};f.state.env.REPOSITORIES=ns;
 const env={ALLOWED_REPOSITORIES:JSON.stringify([repo]),ARCHIVE:f.archive,REPOSITORIES:ns};
 const p={repository:repo.name,delivery:'cleanup-ack-lost',event:'check_run',received:Date.now(),key:'webhooks/cleanup-ack-lost'};
 await f.archive.put(p.key,'{}');await ingress.park(env,repo,p,'migration');
 const key=ingress.pointerKey(repo.id,p),remove=f.archive.delete;let lost=true;
 f.archive.delete=async keys=>{await remove(keys);if(lost&&[].concat(keys).includes(key)){lost=false;throw Error('lost cleanup acknowledgement');}};
 const pass=await ingress.drain(env,repo);
 assert.equal(pass.failed,0);assert.equal(pass.superseded,1);assert.equal(pass.last_error,undefined);assert.equal(pass.next_retry,undefined);assert.deepEqual(pass.delivery_errors,[{delivery:p.delivery,event:p.event,received:p.received,reason:'internal',outcome:'superseded'}]);
 assert.equal(await f.archive.get(key),null);assert.equal(await f.archive.get(ingress.bodyKey(repo.id,p.delivery)),null);
 const receipt=f.archive.objects.get(ingress.receiptKey(repo.id,p.delivery)).bytes.toString();
 assert.equal(f.db.prepare('SELECT COUNT(*) n FROM deliveries').get().n,1);assert.equal(f.db.prepare('SELECT COUNT(*) n FROM changes').get().n,0);
 advance(10000);
 assert.equal((await f.state.fetch(new Request('https://internal/event',{method:'POST',body:JSON.stringify(p)}))).status,200);
 assert.equal(f.archive.objects.get(ingress.receiptKey(repo.id,p.delivery)).bytes.toString(),receipt,'duplicate application must preserve the first durable receipt');
 assert.equal((await ingress.drain(env,repo)).replayed,0);
}));

test('stale replay failures cannot shorten a newer retry deadline',async()=>{
 const h=harness();h.state.full=true;await deliver(h.env,'newer-cooldown');h.state.full=false;
 const key=parked(h.archive,'newer-cooldown')[0],p=await(await h.archive.get(key)).json(),future=Date.now()+600000,put=h.archive.put;let once=true;
 h.archive.put=async(k,...args)=>{
  if(once&&k===ingress.receiptKey(repo.id,p.delivery)){once=false;await put(key,JSON.stringify({...p,replay_failures:4,retry_at:future}));throw Error('R2 put failed');}
  return put(k,...args);
 };
 const result=await ingress.drain(h.env,repo);assert.equal(result.failed,0);assert.equal(result.superseded,1);
 const saved=await(await h.archive.get(key)).json();assert.equal(saved.replay_failures,4);assert.equal(saved.retry_at,future);
 assert.equal((await ingress.drain(h.env,repo)).replayed,0);assert.equal(h.state.applied.get(p.delivery),1);
});

test('manual and cron replay share one conditional lease in either acquisition order',async()=>{
 for(const first of ['manual','cron']){
  const h=harness();h.state.full=true;for(const d of ['lease-a','lease-b'])await deliver(h.env,d);h.state.full=false;
  await h.archive.put('control/100/storage.json',JSON.stringify({state:'writable',verified_at:Date.now()}));
  const original=h.env.REPOSITORIES.get();let release,entered,blocked=true,calls=0;
  const gate=new Promise(r=>release=r),waiting=new Promise(r=>entered=r);
  h.env.REPOSITORIES.get=()=>({fetch:async(...args)=>{calls++;if(blocked){blocked=false;entered();await gate;}return original.fetch(...args);}});
  const pending=first==='manual'?ingress.replayIngress(h.env,repo,{limit:1}):ingress.maintainIngress(h.env,repo);
  await waiting;const before=calls;
  assert.equal((await ingress.replayIngress(h.env,repo,{limit:1})).stopped,'leased');
  await ingress.maintainIngress(h.env,repo);assert.equal(calls,before,'a competing owner must not dispatch another application');
  release();await pending;
  if(first==='manual')assert.equal((await ingress.replayIngress(h.env,repo,{limit:1})).replayed,1,'manual work can advance a healthy owner between cron ticks');
  assert.equal(h.state.applied.size,2);assert.ok([...h.state.applied.values()].every(n=>n===1));
  assert.equal((await(await h.archive.get(ingress.recoveryKey(repo.id))).json()).lease_until,0);
 }
});

test('manual replay recovers an unacknowledged lease claim after expiry',async()=>clock(async advance=>{
 const h=harness();h.state.full=true;await deliver(h.env,'lease-reset');h.state.full=false;
 const key=ingress.recoveryKey(repo.id),put=h.archive.put;let lost=true;
 h.archive.put=async(k,...args)=>{const r=await put(k,...args);if(k===key&&lost){lost=false;throw Error('reset after lease persistence');}return r;};
 await assert.rejects(ingress.replayIngress(h.env,repo),/reset after lease persistence/);
 assert.equal(h.state.applied.size,0);assert.equal((await ingress.replayIngress(h.env,repo)).stopped,'leased');
 advance(61000);assert.equal((await ingress.replayIngress(h.env,repo)).replayed,1);assert.equal(h.state.applied.get('lease-reset'),1);
}));

test('manual replay checkpoints bounded keyset progress and respects recovery backoff',async()=>clock(async advance=>{
 const h=harness();h.state.full=true;for(let i=0;i<20;i++)await deliver(h.env,'deadline-'+String(i).padStart(2,'0'));h.state.full=false;
 const original=h.env.REPOSITORIES.get();h.env.REPOSITORIES.get=()=>({fetch:async(...args)=>{advance(3000);return original.fetch(...args);}});
 const first=await ingress.replayIngress(h.env,repo,{limit:25});assert.equal(first.replayed,10);assert.equal(first.stopped,'deadline');
 const key=ingress.recoveryKey(repo.id),state=await(await h.archive.get(key)).json();assert.equal(state.pointers_cursor,first.cursor);assert.equal(state.lease_until,0);
 const second=await ingress.replayIngress(h.env,repo,{limit:25});assert.equal(second.replayed,10);assert.equal(second.remaining,false);assert.equal(h.state.applied.size,20);
 await h.archive.put(key,JSON.stringify({...state,lease_until:0,last_error:'transport',next_retry:Date.now()+60000}));
 const deferred=await ingress.replayIngress(h.env,repo);assert.equal(deferred.stopped,'recovery_backoff');assert.equal(deferred.replayed,0);
}));

test('a replaced manual lease keeps its newer checkpoint after the old pass commits',async()=>{
 const h=harness();h.state.full=true;await deliver(h.env,'replaced-lease');h.state.full=false;
 const key=ingress.recoveryKey(repo.id),remove=h.archive.delete;let replaced=false;
 h.archive.delete=async keys=>{await remove(keys);if(!replaced){replaced=true;const state=await(await h.archive.get(key)).json();await h.archive.put(key,JSON.stringify({...state,replacement:true,lease_until:Date.now()+60000}));}};
 const pass=await ingress.replayIngress(h.env,repo);assert.equal(pass.replayed,1);assert.equal(pass.stopped,'lease_replaced');
 assert.equal((await(await h.archive.get(key)).json()).replacement,true);assert.equal(h.state.applied.get('replaced-lease'),1);
});

test('invalid manual replay limits cannot acquire or delay the owner',async()=>{
 const h=harness();for(const limit of[0,-1,1.5,101])await assert.rejects(ingress.replayIngress(h.env,repo,{limit}),/invalid_replay_limit/);
 assert.equal(await h.archive.get(ingress.recoveryKey(repo.id)),null);
});

test('manual replay preserves cron cadence and keeps delivery diagnostics administrative',async()=>clock(async advance=>{
 const h=harness();h.state.full=true;for(const d of ['cadence-a','cadence-b'])await deliver(h.env,d);h.state.full=false;
 await h.archive.put('control/100/storage.json',JSON.stringify({state:'writable',verified_at:Date.now()}));
 const key=ingress.recoveryKey(repo.id),due=Date.now()+1000;
 await h.archive.put(key,JSON.stringify({next_retry:due,last_error:null}));
 assert.equal((await ingress.replayIngress(h.env,repo,{limit:1})).replayed,1);
 assert.equal((await(await h.archive.get(key)).json()).next_retry,due);
 advance(1001);await ingress.maintainIngress(h.env,repo);assert.equal(h.state.applied.size,2);
 h.state.error='internal';await deliver(h.env,'private-error',10);
 const reply=await ingress.replayIngress(h.env,repo);assert.equal(reply.failed,1);assert.equal(reply.delivery_errors[0].delivery,'private-error');
 const state=await(await h.archive.get(key)).json();assert.equal(state.pointers_last_pass.delivery_errors,undefined);
 for(const lane of ['pointers','held'])state[lane+'_last_pass']={replayed:2,failed:1,delivery_errors:reply.delivery_errors,failed_deliveries:reply.delivery_errors};
 await h.archive.put(key,JSON.stringify(state));
 for(const publicStatus of [false,true]){
  const view=await ingress.backlog(h.env,repo,publicStatus);
  for(const lane of ['pointers','held'])assert.deepEqual(view[lane+'_last_pass'],{replayed:2,failed:1});
 }
}));

test('retry fencing uses the same GET snapshot as the replayed pointer',async()=>{
 const h=harness();h.state.full=true;await deliver(h.env,'between-list-get');h.state.full=false;
 const key=parked(h.archive,'between-list-get')[0],list=h.archive.list;let rewrite=true;
 h.archive.list=async options=>{const page=await list(options);if(rewrite&&options.prefix==='parked/100/pointers/'){rewrite=false;const p=await(await h.archive.get(key)).json();await h.archive.put(key,JSON.stringify({...p,replay_failures:2}));}return page;};
 h.archive.failures.push((op,k)=>op==='put'&&k===ingress.receiptKey(repo.id,'between-list-get'));
 const pass=await ingress.drain(h.env,repo);assert.equal(pass.failed,1);assert.equal(pass.superseded,0);
 const p=await(await h.archive.get(key)).json();assert.equal(p.replay_failures,3);assert.ok(p.retry_at>Date.now());assert.equal(pass.delivery_errors[0].outcome,'retry_scheduled');
});

test('moving a failed normal pointer preserves an existing held cooldown',async()=>{
 const h=harness();h.state.full=true;await deliver(h.env,'held-race');h.state.full=false;h.state.error='internal';
 const key=parked(h.archive,'held-race')[0],p=await(await h.archive.get(key)).json(),held=ingress.pointerKey(repo.id,p,true),future=Date.now()+600000;
 await h.archive.put(key,JSON.stringify({...p,replay_failures:4}));await h.archive.put(held,JSON.stringify({...p,replay_failures:9,retry_at:future}));
 const pass=await ingress.drain(h.env,repo);assert.equal(pass.failed,1);assert.equal(pass.delivery_errors[0].outcome,'held');
 assert.equal(await h.archive.get(key),null);const retained=await(await h.archive.get(held)).json();assert.equal(retained.replay_failures,9);assert.equal(retained.retry_at,future);assert.ok(await h.archive.get(p.key));
});

test('manual held replay resumes its own cursor and retains it across a pause',async()=>{
 const h=harness();h.state.full=true;for(const d of ['held-a','held-b'])await deliver(h.env,d);h.state.full=false;
 const keys=[];for(const d of ['held-a','held-b']){const key=parked(h.archive,d)[0],p=await(await h.archive.get(key)).json(),held=ingress.pointerKey(repo.id,p,true);await h.archive.put(held,JSON.stringify(p));await h.archive.delete(key);keys.push(held);}
 const first=await ingress.replayIngress(h.env,repo,{held:true,limit:1});assert.equal(first.replayed,1);assert.equal(first.cursor,keys[0]);
 await ingress.setIngressMode(h.env,repo,'parked','test pause');
 const paused=await ingress.replayIngress(h.env,repo,{held:true,limit:1});assert.equal(paused.stopped,'parked');assert.equal(paused.cursor,keys[0]);
 assert.equal((await(await h.archive.get(ingress.recoveryKey(repo.id))).json()).held_cursor,keys[0]);
 await ingress.setIngressMode(h.env,repo,'apply');assert.equal((await ingress.replayIngress(h.env,repo,{held:true,limit:1})).replayed,1);assert.equal(h.state.applied.size,2);
 const state=await(await h.archive.get(ingress.recoveryKey(repo.id))).json();assert.equal(state.held_cursor,undefined);assert.ok(state.held_pass_completed_at);
});

test('retry maintenance errors count each pointer once and retain uncertain move outcomes',async()=>{
 for(const failure of ['retry','held','delete']){
  const h=harness();h.state.full=true;await deliver(h.env,'maintenance-'+failure);h.state.full=false;h.state.error='internal';
  const key=parked(h.archive,'maintenance-'+failure)[0],p=await(await h.archive.get(key)).json();await h.archive.put(key,JSON.stringify({...p,replay_failures:4}));
  const held=ingress.pointerKey(repo.id,p,true);
  h.archive.failures.push((op,k)=>failure==='retry'?op==='put'&&k===key:failure==='held'?op==='put'&&k===held:op==='delete'&&k===key);
  const result=await ingress.drain(h.env,repo);assert.equal(result.failed,1);assert.equal(result.delivery_errors.length,1);assert.equal(result.delivery_errors[0].outcome,'unconfirmed');
  assert.ok(await h.archive.get(p.key));assert.ok(await h.archive.get(key));
 }
});

test('a parkable refusal rewinds its group so a resumed sweep cannot skip due input',async()=>{
 const h=harness();h.state.full=true;for(let i=0;i<20;i++)await deliver(h.env,'rewind-'+String(i).padStart(2,'0'));h.state.full=false;
 const original=h.env.REPOSITORIES.get();let refuse=true;
 h.env.REPOSITORIES.get=()=>({fetch:async(url,init)=>{const p=JSON.parse(init.body);if(refuse&&p.delivery==='rewind-10'){refuse=false;return Response.json({error:'database_unavailable',parkable:true},{status:503});}return original.fetch(url,init);}});
 const first=await ingress.replayIngress(h.env,repo,{limit:25});assert.equal(first.stopped,'database_unavailable');assert.equal(first.replayed,19);assert.equal(first.failed,0);
 assert.ok(first.cursor.endsWith('-rewind-09.json'),'checkpoint must precede the refused group');
 let state=await(await h.archive.get(ingress.recoveryKey(repo.id))).json();assert.equal(state.pointers_pass_completed_at,undefined);
 const next=await ingress.replayIngress(h.env,repo,{limit:25});assert.equal(next.replayed,1);assert.equal(next.remaining,false);
 state=await(await h.archive.get(ingress.recoveryKey(repo.id))).json();assert.ok(state.pointers_pass_completed_at>=state.pointers_pass_started_at);
 assert.equal(h.state.applied.size,20);assert.ok([...h.state.applied.values()].every(n=>n===1));
});
