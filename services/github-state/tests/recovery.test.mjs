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
function bucket(){
 const objects=new Map(),failures=[];
 const meta=(key,o)=>({key,size:o.bytes.length,customMetadata:o.customMetadata,uploaded:o.uploaded,etag:o.etag});
 const check=(op,key)=>{const i=failures.findIndex(f=>f(op,key));if(i>=0){failures.splice(i,1);throw new Error('R2 '+op+' failed');}};
 return {objects,failures,
  async head(key){check('head',key);const o=objects.get(key);return o?meta(key,o):null;},
  async get(key){check('get',key);const o=objects.get(key);return o?{...meta(key,o),text:async()=>o.bytes.toString(),json:async()=>JSON.parse(o.bytes.toString()),arrayBuffer:async()=>new Uint8Array(o.bytes).buffer}:null;},
  async put(key,value,options={}){check('put',key);const bytes=Buffer.from(typeof value==='string'?value:new Uint8Array(value));
   if(options.sha256 && sha(bytes)!==options.sha256) throw new Error('digest mismatch');
   if(options.onlyIf?.etagMatches && objects.get(key)?.etag!==options.onlyIf.etagMatches) return null;
   objects.set(key,{bytes,customMetadata:options.customMetadata || {},uploaded:new Date(),etag:sha(bytes)});},
  async delete(keys){for(const key of [].concat(keys)){check('delete',key);objects.delete(key);}},
  async list({prefix='',limit=1000,cursor}={}){const keys=[...objects.keys()].filter(k=>k.startsWith(prefix)).sort(),start=Number(cursor || 0);
   return {objects:keys.slice(start,start+limit).map(k=>meta(k,objects.get(k))),truncated:start+limit<keys.length,cursor:String(start+limit)};},
 };
}
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
 await assert.rejects(drain(h.env,repo));
 assert.equal(h.state.applied.get('r1'),1);assert.equal(parked(h.archive,'r1').length,1);
 assert.equal(h.state.applied.get('r2'),1);assert.equal(parked(h.archive,'r2').length,1);
 const result=await drain(h.env,repo);
 assert.equal(result.replayed,2);
 for(const d of ['r1','r2','r3']) {assert.equal(h.state.applied.get(d),1,d+' applied once');assert.equal(parked(h.archive,d).length,0);}
 assert.equal([...h.archive.objects.keys()].filter(k=>k.startsWith('parked/')).length,0);
 // A redelivered message after parking applies directly; its pointer then deduplicates.
 h.state.full=true;await deliver(h.env,'r4');h.state.full=false;
 assert.equal((await deliver(h.env,'r4')).acked,true);
 assert.equal((await drain(h.env,repo)).replayed,1);assert.equal(h.state.applied.get('r4'),1);
});

test('a persistently failing parked event is held aside without blocking newer events',async()=>{
 const h=harness();h.state.full=true;
 await deliver(h.env,'bad');
 h.state.full=false;
 const {drain,backlog}=await load('src/ingress.ts');
 h.archive.objects.delete(JSON.parse(h.archive.objects.get(parked(h.archive,'bad')[0]).bytes).key);
 for(let i=0;i<5;i++) await drain(h.env,repo);
 const report=await backlog(h.env,repo);
 assert.equal(report.parked.count,0);assert.equal(report.held.count,1);
 h.state.full=true;await deliver(h.env,'good');h.state.full=false;
 assert.equal((await drain(h.env,repo)).replayed,1);assert.equal(h.state.applied.get('good'),1);
});

// Repository harness over node:sqlite with an R2 double.
function repositoryFixture(env={},size=0,db=new DatabaseSync(':memory:'),archive=bucket()){
 let alarm=null,ready;
 const ctx={blockConcurrencyWhile:fn=>(ready=fn()),id:{toString:()=>'fixture'},storage:{sql:{exec(sql,...bindings){let rows=[],written=0;if(!bindings.length && sql.includes(';'))db.exec(sql);else{const st=db.prepare(sql);if(st.columns().length)rows=st.all(...bindings);else written=Number(st.run(...bindings).changes);}return {rowsWritten:written,toArray:()=>rows,one:()=>rows[0]};},get databaseSize(){return size;}},
  transactionSync(fn){db.exec('SAVEPOINT tx');try{const v=fn();db.exec('RELEASE tx');return v;}catch(e){db.exec('ROLLBACK TO tx');db.exec('RELEASE tx');throw e;}},sync:async()=>{},getAlarm:async()=>alarm,setAlarm:async v=>{alarm=v;}}};
 const state=new RepositoryState(ctx,{ALLOWED_REPOSITORIES:JSON.stringify([repo]),ARCHIVE:archive,...env});
 if(!size) state.setMeta('repository',repo);
 state.fixtureReady=ready;
 return {db,state,archive,ready};
}
const queryFixture=(state,q)=>state.fetch(new Request('https://internal/query',{method:'POST',body:JSON.stringify({repository:'org/repo',...q})})).then(r=>r.json());

test('legacy SQL cursors and R2 cursors validate query, coverage and expiry',async()=>{
 const {db,state,archive}=repositoryFixture();
 const page={version:1,repository:'org/repo',operation:'open_prs',status:'ok',revision:3,complete:true,data:[{number:1}]};
 const key='open_prs:{}',now=Date.now();
 db.prepare('INSERT INTO cursors VALUES(?,?,?)').run('legacy:100',now+60000,JSON.stringify({key,allow_observed:undefined,result:page}));
 db.prepare('INSERT INTO cursors VALUES(?,?,?)').run('old:100',now-1,JSON.stringify({key,result:page}));
 assert.deepEqual(await queryFixture(state,{operation:'open_prs',cursor:'legacy:100'}),page);
 assert.equal((await queryFixture(state,{operation:'open_prs',cursor:'old:100'})).reason,'cursor_expired');
 assert.equal((await queryFixture(state,{operation:'open_issues',cursor:'legacy:100'})).reason,'cursor_query_mismatch');
 const id='0b6c2f0e-1f7a-4b4c-9a1e-5b8d3c2a1f00';
 await archive.put('cursors/100/'+id+'/100.json',JSON.stringify({repository:'100',key:'workflow_jobs:{"id":"9"}',allow_observed:true,expires:now+60000,result:{...page,operation:'workflow_jobs'}}));
 await archive.put('cursors/100/'+id+'/200.json',JSON.stringify({repository:'100',key,allow_observed:false,expires:now-1,result:page}));
 await archive.put('cursors/100/'+id+'/300.json',JSON.stringify({repository:'999',key,allow_observed:false,expires:now+60000,result:page}));
 // CI operations are not mirrored: even a stored continuation is not served.
 assert.equal((await queryFixture(state,{operation:'workflow_jobs',args:{id:'9'},allow_observed:true,cursor:id+':100'})).reason,'not_mirrored');
 assert.equal((await queryFixture(state,{operation:'open_prs',cursor:id+':200'})).reason,'cursor_expired');
 assert.equal((await queryFixture(state,{operation:'open_prs',cursor:id+':300'})).reason,'cursor_expired','another repository cannot use the cursor');
 assert.equal((await queryFixture(state,{operation:'open_prs',cursor:'../../etc:1'})).reason,'cursor_expired');
 // Very old whole-snapshot cursors continue only inside their snapshot.
 db.prepare('INSERT INTO cursors VALUES(?,?,?)').run('whole',now+60000,JSON.stringify({key,result:{...page,data:undefined},rows:[{number:1},{number:2}]}));
 assert.deepEqual((await queryFixture(state,{operation:'open_prs',cursor:'whole:1'})).data,[{number:2}]);
 for(const cursor of ['whole:2','whole:900000','whole:1:extra']) assert.equal((await queryFixture(state,{operation:'open_prs',cursor})).reason,'cursor_invalid',cursor);
});

test('expiry runs before repair and hydration, survives their failure, and keeps semantic data',async()=>{
 const {db,state}=repositoryFixture();
 const now=Date.now();
 db.prepare('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)').run('pr','1','1',JSON.stringify({number:1,state:'OPEN'}),'',now-90*86400000,1,0);
 db.prepare('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)').run('issue_comments','5','1',JSON.stringify({id:5}),'',now-90*86400000,2,1);
 db.prepare('INSERT INTO collections VALUES(?,?,?,?,?,?,?)').run('open_prs:{}','open_prs','{}',1,now-90*86400000,1,null);
 db.prepare('INSERT INTO members VALUES(?,?)').run('open_prs:{}','1');
 db.prepare('INSERT INTO deliveries VALUES(?,?,?)').run('recent','push',now-86400000);
 for(let i=0;i<60;i++) db.prepare('INSERT INTO cursors VALUES(?,?,?)').run('c'+i,now-1000,'x'.repeat(1000));
 db.prepare('INSERT INTO cursors VALUES(?,?,?)').run('live',now+60000,'{}');
 db.prepare('INSERT INTO repair_metrics VALUES(?,?,?,?,?)').run(now-8*86400000,'page','active','pr',1);
 db.prepare('INSERT INTO jobs(key,query,priority,due,lane) VALUES(?,?,?,?,?)').run('pr:{"number":1}',JSON.stringify({repository:'org/repo',operation:'pr',args:{number:1}}),5,0,'active');
 state.nextJob=()=>{throw new Error('repair dispatch failed');};
 state.hydrateSurvey=async()=>{throw new Error('hydration failed');};
 state.invalidateSurvey=()=>{throw new Error('invalidation failed');};
 await state.fixtureReady;
 await state.alarm();
 assert.equal(db.prepare('SELECT COUNT(*) n FROM cursors').get().n,11,'first bounded expiry batch ran');
 assert.equal(db.prepare('SELECT COUNT(*) n FROM repair_metrics').get().n,0);
 await state.fixtureReady;
 await state.alarm();
 assert.deepEqual(db.prepare('SELECT id FROM cursors').all().map(r=>r.id),['live']);
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

test('verified cleanup never deletes a reused rowid written after the export began',()=>{
 const db=new DatabaseSync(':memory:'),now=Date.now(),day=86400000;
 const sql={exec(q,...b){let rows=[];const st=db.prepare(q);if(st.columns().length)rows=st.all(...b);else st.run(...b);return {toArray:()=>rows,one:()=>rows[0]};}};
 db.exec('CREATE TABLE demand(key TEXT PRIMARY KEY,at INTEGER)');
 db.prepare('INSERT INTO demand VALUES(?,?)').run('a',now-5*day);
 db.prepare('INSERT INTO demand VALUES(?,?)').run('b',now-5*day);
 const exported={upper:2,started:now-3*day};
 db.prepare("DELETE FROM demand WHERE key='b'").run();
 db.prepare('INSERT INTO demand VALUES(?,?)').run('reused',now-2*day);
 assert.equal(db.prepare("SELECT rowid FROM demand WHERE key='reused'").get().rowid,2);
 db.prepare("UPDATE demand SET at=? WHERE key='a'").run(now-2*day);
 assert.equal(storage.deleteClass(sql,'stale_demand',exported,{}).rows,0,'rows written after export start survive');
 db.prepare('INSERT INTO demand VALUES(?,?)').run('later',now-4*day);
 assert.equal(storage.deleteClass(sql,'stale_demand',exported,{}).rows,0,'rows beyond the exported rowid survive');
 db.prepare("UPDATE demand SET at=? WHERE key='a'").run(now-4*day);
 assert.equal(storage.deleteClass(sql,'stale_demand',exported,{}).rows,1);
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
 assert.equal(state.schema,'legacy');assert.equal(state.degraded.operation,'database_size');
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
 assert.equal(state.schema,'legacy');assert.equal(state.degraded.operation,'recorded:queue');
 assert.equal(db.prepare("SELECT COUNT(*) n FROM meta WHERE key='schema_version'").get().n,0);
});

test('degraded workflow reads never write: CI is not mirrored',async()=>{
 const {db,state}=repositoryFixture();
 db.prepare('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)').run('workflow_runs','77','',JSON.stringify({id:77,run_attempt:2}),'',1,1,0);
 state.setMeta('last_webhook',Date.now());
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

test('a degraded mirror that stopped changing is reported unavailable, not served stale',async()=>{
 const {state}=repositoryFixture();
 const q={operation:'open_prs'};
 state.setMeta('last_webhook',Date.now()-60000);
 state.degraded={since:Date.now(),reason:'storage_full',operation:'test'};
 assert.notEqual((await queryFixture(state,q)).reason,'mirror_frozen','recently changed data is still served');
 state.setMeta('last_webhook',Date.now()-11*60000);
 const frozen=await queryFixture(state,q);
 assert.equal(frozen.status,'unavailable');assert.equal(frozen.reason,'mirror_frozen');assert.ok(frozen.frozen_since>0);
 assert.equal(Object.hasOwn(frozen,'data'),false);
 state.degraded=undefined;
 assert.notEqual((await queryFixture(state,q)).reason,'mirror_frozen','a quiet but writable mirror is current');
});

test('a CI class export archives only CI rows and cleanup deletes exactly those',async()=>{
 const {db,state,archive}=repositoryFixture();
 const now=Date.now(),put=(kind,id,observed)=>db.prepare('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)').run(kind,id,'',JSON.stringify({id,blob:'x'.repeat(200)}),'',observed,1,0);
 put('workflow_runs','1',now-5000);put('pr','7',now-5000);put('checks','2',now-5000);put('workflow_jobs','3',now-5000);
 db.prepare('INSERT INTO members VALUES(?,?)').run('checks:{"sha":"a"}','2');
 db.prepare('INSERT INTO members VALUES(?,?)').run('open_prs:{}','7');
 db.prepare('INSERT INTO collections VALUES(?,?,?,?,?,?,?)').run('checks:{"sha":"a"}','checks','{}',1,now,1,null);
 db.prepare('INSERT INTO collections VALUES(?,?,?,?,?,?,?)').run('open_prs:{}','open_prs','{}',1,now,1,null);
 state.setMeta('workflow_attempt:9',1);state.setMeta('important_active:9',true);
 const call=(path,body)=>state.fetch(new Request('https://internal'+path,{method:'POST',body:JSON.stringify({repository_id:'100',...body})})).then(r=>r.json());
 const recovery=await call('/storage/recovery',{});
 const classes=['ci_resources','ci_members','ci_collections','ci_meta'];
 assert.equal((await call('/storage/cleanup',{class:'ci_resources',recovery:recovery.id})).error,'recovery_not_verified');
 for(const name of classes){
  let page;do page=await call('/storage/export',{recovery:recovery.id,class:name,page_bytes:8000000}); while(!page.complete);
  assert.equal((await call('/storage/verify',{recovery:recovery.id,class:name})).verified,true);
 }
 const manifest=JSON.parse(archive.objects.get('recovery/100/'+recovery.id+'.json').bytes);
 assert.equal(manifest.tables['class:ci_resources'].rows,3,'only CI resources were archived');
 // Written after the export began, or beyond its rowid bound: not covered, not deleted.
 db.prepare('UPDATE resources SET observed=? WHERE kind=? AND id=?').run(Date.now()+1000,'workflow_jobs','3');
 put('checks','4',now-5000);
 for(const name of classes) await call('/storage/cleanup',{class:name,recovery:recovery.id,limit:100});
 assert.deepEqual(db.prepare('SELECT kind,id FROM resources ORDER BY rowid').all().map(r=>r.kind+':'+r.id),['pr:7','workflow_jobs:3','checks:4']);
 assert.deepEqual(db.prepare('SELECT collection FROM members').all().map(r=>r.collection),['open_prs:{}']);
 assert.deepEqual(db.prepare('SELECT key FROM collections').all().map(r=>r.key),['open_prs:{}']);
 assert.equal(db.prepare("SELECT COUNT(*) n FROM meta WHERE key LIKE 'workflow%' OR key LIKE 'important%'").get().n,0);
 assert.ok(state.meta('repository'),'other meta survives');
 assert.equal((await call('/storage/export',{recovery:recovery.id,class:'repair_metrics'})).error,'class_export_unsupported');
});

const retention=await load('src/retention.ts');
const monitoring=await load('src/monitoring.ts');
const budget=(rows=100,bytes=4*1024**2)=>({rows,bytes,deadline:Date.now()+10000});
const scalar=(db,q)=>Object.values(db.prepare(q).get())[0];

test('sustained retention volume plateaus across windows with bounded rows, bytes and time',()=>{
 const {db,state}=repositoryFixture(),day=86400000,checkpoints={};
 const start=Date.now()-60*day;
 for(let d=0;d<50;d++) {
  const now=start+d*day;
  for(let i=0;i<20;i++) {
   db.prepare('INSERT INTO deliveries VALUES(?,?,?)').run(d+'-'+i,'push',now);
   db.prepare('INSERT INTO changes VALUES(?,?,?,?,?)').run(d*20+i+1,now,'pr',String(i),0);
   db.prepare('INSERT INTO repair_metrics VALUES(?,?,?,?,?)').run(now,'page','active','pr',1);
   db.prepare('INSERT INTO cursors VALUES(?,?,?)').run(d+'-'+i,now+600000,'x'.repeat(200));
  }
  for(const name of ['expired_deliveries','expired_changes','expired_repair_metrics','expired_cursors']) {
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
   assert.equal(scalar(db,'SELECT COUNT(*) FROM cursors'),20);
  }
 }
 const b={rows:100,bytes:1000,deadline:0};
 const before=scalar(db,'SELECT COUNT(*) FROM deliveries');
 retention.sweep(state.sql,'expired_deliveries',undefined,b,true,Date.now());
 assert.equal(scalar(db,'SELECT COUNT(*) FROM deliveries'),before,'expired time budget performs no deletion');
});

test('duplicate/stale retention checkpoints, dry runs and rowid reuse are safe',()=>{
 const {db,state}=repositoryFixture(),now=Date.now();
 for(let i=0;i<20;i++)db.prepare('INSERT INTO cursors VALUES(?,?,?)').run('c'+i,now-1,'x'.repeat(200));
 const dry=retention.sweep(state.sql,'expired_cursors',undefined,budget(5),false,now);
 assert.equal(dry.eligible,5);assert.equal(dry.deleted,0);assert.equal(scalar(db,'SELECT COUNT(*) FROM cursors'),20);
 const first=retention.sweep(state.sql,'expired_cursors',undefined,budget(5),true,now);
 assert.equal(first.deleted,5);
 const old={...first,after:0};
 // Simulate a crash after SQL deletes but before the external checkpoint write.
 retention.sweep(state.sql,'expired_cursors',old,budget(5),true,now);
 db.prepare('INSERT INTO cursors VALUES(?,?,?)').run('fresh',now+600000,'{}');
 for(let i=0;i<10;i++)retention.sweep(state.sql,'expired_cursors',old,budget(),true,now);
 assert.deepEqual(db.prepare('SELECT id FROM cursors').all().map(r=>r.id),['fresh']);
 const b=budget(100,100);retention.sweep(state.sql,'expired_cursors',undefined,b,true,now);
 assert.ok(b.bytes>=0);
});

test('retention policy survives restart, pins receipts while parked, and isolates failed checkpoints',async()=>{
 const {db,state,archive}=repositoryFixture();
 const now=Date.now(),post=body=>state.fetch(new Request('https://internal/storage/retention',{method:'POST',body:JSON.stringify(body)}));
 const old=now-30*86400000;
 db.prepare('INSERT INTO deliveries VALUES(?,?,?)').run('pending','push',old);
 db.prepare('INSERT INTO demand VALUES(?,?)').run('quiet',old);
 await archive.put('parked/100/pointers/pending.json','{}');
 await post({policy:{mode:'enforce',disabled:[],pinned:['expired_demand']}});
 assert.equal(scalar(db,'SELECT COUNT(*) FROM deliveries'),1,'parked input pins receipt expiry');
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

test('capacity thresholds handle growth, shrinkage, unknown allowance and stale samples',()=>{
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
 state.prepareDiagnostics();assert.ok(state.diagnosticsAt>0);
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

test('CI archival verifies bytes, preserves attempt guards, snapshots and cursors, and retries failed writes',async()=>{
 const {db,state,archive}=repositoryFixture();
 const payload={id:'77',run_attempt:2,steps:[{name:'x'.repeat(8000)}]},old=Date.now()-8*86400000;
 db.prepare('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)').run('workflow_jobs','77','9',JSON.stringify(payload),'',old,5,0);
 archive.failures.push((op,key)=>op==='put' && key.includes('/ci/'));
 await assert.rejects(state.archiveCI());
 assert.deepEqual(JSON.parse(db.prepare('SELECT data FROM resources').get().data),payload);
 await state.archiveCI();
 const encoded=db.prepare('SELECT data FROM resources').get().data,pointer=JSON.parse(encoded);
 assert.ok(pointer.$r2);assert.equal(pointer.run_attempt,2);assert.deepEqual(await state.decode(encoded),payload);
 assert.equal(scalar(db,"SELECT json_extract(data,'$.run_attempt') FROM resources"),2,'legacy membership guard works');
 assert.equal(scalar(db,'SELECT revision FROM resources'),5,'archival does not publish a semantic revision');
 await archive.put('recovery/100/pinned.json',JSON.stringify({data:encoded}));
 db.prepare('INSERT INTO cursors VALUES(?,?,?)').run('spill',Date.now()+600000,encoded);
 db.exec('DELETE FROM resources');
 const retained=await (await state.fetch(new Request('https://internal/retained-object?key='+encodeURIComponent(pointer.$r2)))).json();
 assert.equal(retained.retained,true);assert.deepEqual(await state.decode(encoded),payload);
 for(const key of ['parked/100/bodies/x','recovery/100/x','migration/100/x','resources/100/x'])assert.equal(retention.protectedObject(key,{expires:'1'}),true);
 assert.equal(retention.protectedObject('webhooks/x',{pinned:'true',expires:'1'}),true);
 assert.equal(retention.protectedObject('webhooks/x',{expires:'1'}),false);
});

test('verified recovery archives restore exact IDs, tombstones, memberships, coverage and spill contents',async()=>{
 const {db,state,archive}=repositoryFixture();
 const now=Date.now(),id='9223372036854775806';
 await state.put('issue_comments',id,{id,body:'retained history',updated_at:'2026-10-01'},now,false,'7');
 await state.put('issue_comments','deleted',{id:'deleted',body:'tombstone'},now,true,'7');
 state.membership('issue_comments:{"number":7}',id,true);
 db.prepare('INSERT INTO collections VALUES(?,?,?,?,?,?,?)').run('issue_comments:{"number":7}','issue_comments','{"number":7}',1,now,state.revision(),null);
 const key='resources/100/pinned-spill';await archive.put(key,JSON.stringify({body:'archived raw contents'}));
 db.prepare('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)').run('checks','spill','',JSON.stringify({$r2:key}),'',now,10,0);
 const storageContext={getCurrentBookmark:async()=>'test-bookmark'};
 const manifest=await storage.createRecovery(archive,storageContext,state.sql,'100',state.revision());
 for(const table of storage.userTables(state.sql)) {
  while(!(await storage.exportPage(archive,state.sql,'100',{recovery:manifest.id,table,limit:2})).complete){}
  while(!(await storage.verifyExport(archive,'100',{recovery:manifest.id,table})).verified){}
 }
 const copy=repositoryFixture({},0,undefined,archive),restored=await storage.readManifest(archive,'100',manifest.id);
 for(const [table,info] of Object.entries(restored.tables)) {
  copy.db.exec('DELETE FROM "'+table+'"');
  for(const page of info.pages) {
   const object=await archive.get(page.key),text=await object.text();assert.equal(sha(Buffer.from(text)),page.sha256);
   for(const {__rowid,...row} of JSON.parse(text)) {
    const cols=Object.keys(row);copy.db.prepare('INSERT INTO "'+table+'" ('+cols.map(c=>'"'+c+'"').join(',')+') VALUES('+cols.map(()=>'?').join(',')+')').run(...Object.values(row));
   }
  }
 }
 assert.equal(copy.db.prepare("SELECT id FROM resources WHERE id=?").get(id).id,id);
 assert.equal(copy.db.prepare("SELECT tombstone FROM resources WHERE id='deleted'").get().tombstone,1);
 assert.equal(copy.db.prepare('SELECT complete FROM collections').get().complete,1);
 assert.equal(copy.db.prepare('SELECT id FROM members').get().id,id);
 assert.deepEqual(await copy.state.decode(copy.db.prepare("SELECT data FROM resources WHERE id='spill'").get().data),{body:'archived raw contents'});
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
 await archive.put('control/100/retention-policy.json',JSON.stringify({mode:'enforce',disabled:[],pinned:['expired_cursors']}));
 await archive.put('control/100/retention-checkpoint.json',JSON.stringify({version:1,classes:{expired_cursors:{after:'bad'}},ci_after:-10}));
 db.prepare('INSERT INTO cursors VALUES(?,?,?)').run('pinned',Date.now()-1,'{}');
 await state.storageMaintenance();
 assert.equal(scalar(db,'SELECT COUNT(*) FROM cursors'),1);
 assert.equal((await state.preparedObservation()).retention.policy_loaded,true);
});


test('a single bounded sweep per interval keeps up with sustained append volume',()=>{
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


test('a concurrent resource update wins over slow CI archival',async()=>{
 const {state,db,archive}=repositoryFixture(),old=Date.now()-8*86400000;
 const before={id:'7',run_attempt:1,steps:['x'.repeat(8000)]},after={id:'7',run_attempt:2,steps:['new attempt']};
 db.prepare('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)').run('workflow_jobs','7','9',JSON.stringify(before),'',old,5,0);
 const put=archive.put.bind(archive);
 archive.put=async(key,...args)=>{
  await put(key,...args);
  if(key.includes('/ci/'))db.prepare('UPDATE resources SET data=?,revision=6,observed=? WHERE id=?').run(JSON.stringify(after),Date.now(),'7');
 };
 await state.archiveCI();
 const row=db.prepare('SELECT data,revision FROM resources').get();
 assert.deepEqual(JSON.parse(row.data),after);assert.equal(row.revision,6);assert.equal(state.ciProgress.archived,0);
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
 db.prepare('INSERT INTO cursors VALUES(?,?,?)').run('old',Date.now()-1,'{}');
 let armed=false;state.setAlarm=async()=>{armed=true;};
 state.storageMaintenance=()=>new Promise(()=>{});
 const response=await state.fetch(new Request('https://internal/tick',{method:'POST',body:JSON.stringify({repository:'org/repo'})}));
 assert.equal(response.status,200);assert.equal(armed,true);assert.equal(scalar(db,'SELECT COUNT(*) FROM cursors'),0);
});

test('unknown durable pins fail closed while explicit emergency SQL cleanup remains available',async()=>{
 const {db,state,archive}=repositoryFixture();await state.fixtureReady;
 db.prepare('INSERT INTO cursors VALUES(?,?,?)').run('pinned-unknown',Date.now()-1,'{}');
 const original=archive.get.bind(archive);archive.get=async key=>{if(key.endsWith('retention-policy.json'))throw new Error('R2 unavailable');return original(key);};
 state.storageLoaded=false;
 await state.storageMaintenance();
 assert.equal(scalar(db,'SELECT COUNT(*) FROM cursors'),1,'never guess that an unavailable pin policy is empty');
 const response=await state.fetch(new Request('https://internal/storage/cleanup',{method:'POST',body:JSON.stringify({class:'expired_cursors'})}));
 assert.equal(response.status,200);assert.equal((await response.json()).rows,1,'operator-selected emergency class is independent of R2');
});

test('retention inspection does not launch expiry or archival',async()=>{
 const {db,state}=repositoryFixture();await state.fixtureReady;
 db.prepare('INSERT INTO cursors VALUES(?,?,?)').run('old',Date.now()-1,'{}');
 state.storageMaintenance=async()=>assert.fail('inspection must not run maintenance');
 const response=await state.fetch(new Request('https://internal/storage/retention',{method:'POST',body:'{}'}));
 assert.equal(response.status,200);assert.equal(scalar(db,'SELECT COUNT(*) FROM cursors'),1);
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
 db.prepare('INSERT INTO cursors VALUES(?,?,?)').run('old',Date.now()-1,'{}');
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
 state.retryJob(job,new Error('staging page missing'));
 const row=db.prepare('SELECT nested,wait_reason FROM jobs WHERE key=?').get(job.key);assert.equal(row.nested,null);assert.equal(row.wait_reason,'staging_expired');
});

test('archived collection reads bound parallel R2 fetches and preserve membership order',async()=>{
 const {db,state,archive}=repositoryFixture();await state.fixtureReady;
 const key='workflow_jobs:{"id":"9"}';state.setMeta('workflow_attempt:9',1);
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
 await archive.put('control/100/retention-checkpoint.json',JSON.stringify({version:1,classes:{expired_changes:checkpoint},ci_after:80}));
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
 assert.equal(state.retentionState.expired_changes.after,20);assert.equal(state.ciAfter,80);
});

test('a completed retention epoch waits for the next minute instead of rescanning on every alarm',async()=>{
 const {state,db}=repositoryFixture();await state.fixtureReady;
 db.prepare('INSERT INTO changes VALUES(?,?,?,?,?)').run(1,Date.now(),'pr','1',0);
 state.expire();const first={...state.retentionState.expired_changes};
 for(let i=0;i<20;i++)state.expire();
 assert.deepEqual(state.retentionState.expired_changes,first);
});

test('incremental accounting projections roll the hourly window without regrouping on health reads',()=>{
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
 await state.loadRetentionState();state.expire();
 assert.equal(scalar(db,'SELECT COUNT(*) FROM changes'),0);
});

test('a retention pin acquired while listing prevents the pending R2 delete',async()=>{
 const archive=bucket(),now=Date.now();let pinned=false;
 await archive.put('webhooks/100/old.json','retained',{customMetadata:{expires:String(now-1)}});
 const list=archive.list.bind(archive);archive.list=async options=>{const page=await list(options);pinned=true;return page;};
 const result=await retention.retirePrefix(archive,'raw_webhooks',now,async()=>!pinned);
 assert.ok(await archive.get('webhooks/100/old.json'));assert.equal(result.deleted,0);
});
