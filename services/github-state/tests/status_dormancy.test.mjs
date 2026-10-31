import {test} from 'node:test';
import assert from 'node:assert/strict';
import {fixture as sqliteFixture} from './migration_fixture.mjs';
import {postgresFixture} from './postgres_runtime.mjs';
import {runtime,repository,send} from './runtime.mjs';
const sha=n=>n.toString(16).padStart(40,'0'),query=n=>({repository:'org/repo',operation:'statuses',args:{sha:sha(n)}}),key=q=>'statuses:'+JSON.stringify(q.args);
async function fixture(){const f=process.env.TEST_POSTGRES_URL?await postgresFixture():await sqliteFixture();return {...f,store:f.store||f.state.store,close:f.close||(()=>f.db.close())};}
async function parent(f,n,head,state='OPEN'){
 await f.state.put('pr',String(n),{number:n,state,headRefOid:sha(n)});
 if(head!==sha(n))await f.state.put('pr',String(n),{number:n,state,headRefOid:head});
}
async function complete(f,q){await f.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)',key(q),q.operation,JSON.stringify(q.args),1,Date.now(),0,null);}
async function job(f,q,dirty=false){await f.state.enqueue(q,20,Date.now()-1000,dirty);return(await f.store.exec('SELECT * FROM jobs WHERE key=?',key(q))).one();}

const cases=[['historical open',sha(99),'OPEN',true],['current open',sha(1),'OPEN',false],['closed unknown head',null,'CLOSED',true],['merged',sha(1),'MERGED',true],['missing head',null,'OPEN',false],['invalid head','unknown','OPEN',false],['numeric head',123,'OPEN',false],['unknown state',sha(99),'UNKNOWN',false]];
for(const [label,head,state,dormant]of cases)test('status dormancy is consistent for '+label,async()=>{
 const f=await fixture();try{
  await parent(f,1,head,state);const q=query(1);assert.equal(await f.state.dormant(q),dormant);
  await f.state.enqueueMany([{q,priority:20}],true);assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs WHERE key=?',key(q))).one().n,dormant?0:1,'backfill agrees with individual dormancy');
  await f.store.exec('DELETE FROM jobs');await f.state.enqueueMany([{q,priority:20}]);
  assert.equal((await f.store.exec('SELECT lane FROM jobs WHERE key=?',key(q))).one().lane,dormant?'archive':'verification');
  await complete(f,q);const row=(await f.store.exec('SELECT * FROM jobs WHERE key=?',key(q))).one();
  assert.equal(await f.state.retireUnneeded(row),dormant);
 }finally{await f.close();}
});

test('shared current heads and missing associated resources keep statuses active',async()=>{
 const f=await fixture();try{
  await parent(f,1,sha(99));await f.state.put('pr','2',{number:2,state:'OPEN',headRefOid:sha(1)});assert.equal(await f.state.dormant(query(1)),false);
  await f.state.enqueueMany([{q:query(1),priority:20}],true);await complete(f,query(1));
  const shared=(await f.store.exec('SELECT * FROM jobs WHERE key=?',key(query(1)))).one();
  assert.deepEqual(await f.state.atomic(state=>state.claimCompanions(shared,[shared],undefined,true),'repair'),[],'a shared current head is not bulk-retired');
  await f.store.exec('DELETE FROM jobs');await f.store.exec('DELETE FROM collections');
  await f.state.put('pr','2',{number:2,state:'OPEN',headRefOid:sha(99)});assert.equal(await f.state.dormant(query(1)),true);
  await f.store.exec('INSERT INTO head_prs VALUES(?,?)',sha(1),'missing');assert.equal(await f.state.dormant(query(1)),false);
  assert.equal(await f.state.dormant(query(50)),false);
  const q=query(1);await f.state.enqueueMany([{q,priority:20}],true);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs WHERE key=?',key(q))).one().n,1,'backfill retains a missing associated PR');
  await complete(f,q);const row=(await f.store.exec('SELECT * FROM jobs WHERE key=?',key(q))).one();
  assert.deepEqual(await f.state.atomic(state=>state.claimCompanions(row,[row],undefined,true),'repair'),[]);
 }finally{await f.close();}
});

test('head projection tolerates escaped body characters and does not treat JSON numbers as heads',async()=>{
 const f=await fixture();try{
  await parent(f,1,sha(99));const q=query(1);
  for(const [encoded,expected]of [[JSON.stringify(sha(99)),true],['1'.repeat(40),false],['null',false]]){
   const text='{"number":1,"state":"OPEN","headRefOid":'+encoded+',"body":"\\u0000\\ud800"}';
   await f.store.exec("UPDATE resources SET data=? WHERE kind='pr' AND id='1'",text);
   assert.equal(await f.state.dormant(q),expected);
   await f.store.exec('DELETE FROM jobs');await f.state.enqueueMany([{q,priority:20}],true);
   assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs')).one().n,expected?0:1);
  }
 }finally{await f.close();}
});

test('bulk retirement protects dirty incomplete requested demanded and replacement-owned statuses',async()=>{
 const f=await fixture();try{
  const qs=Array.from({length:7},(_,i)=>query(i+1));
  for(let n=1;n<=7;n++){await parent(f,n,sha(99));if(n!==3)await complete(f,query(n));await job(f,query(n),n===2);}
  await f.store.exec('UPDATE jobs SET requested_at=? WHERE key=?',Date.now()-3600000,key(query(4)));
  await f.store.exec('INSERT INTO demand VALUES(?,?)',key(query(5)),Date.now());
  const rows=(await f.store.exec('SELECT * FROM jobs ORDER BY key')).toArray();
  await f.store.exec('UPDATE jobs SET lease_id=?,lease_until=? WHERE key=?','replacement',Date.now()+60000,key(query(6)));
  await f.store.exec('UPDATE jobs SET generation=generation+1 WHERE key=?',key(query(7)));
  // The actual coordinator never supplies an owned selection; the explicit
  // selection here proves retirement still fences a stale snapshot itself.
  const retired=await f.state.atomic(state=>state.claimCompanions(rows[0],rows,undefined,true),'repair');
  assert.deepEqual(retired.map(j=>j.key).sort(),[key(query(1))]);
  assert.equal((await f.store.exec('SELECT lease_id FROM jobs WHERE key=?',key(query(6)))).one().lease_id,'replacement');
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs')).one().n,6);
 }finally{await f.close();}
});

test('historical dirty status reconciles once; newer generation and replacement leases survive',async()=>{
 const f=await fixture();try{
  for(let n=1;n<=3;n++){await parent(f,n,sha(99));await complete(f,query(n));await job(f,query(n),true);}
  const first=await f.state.claim((await f.store.exec('SELECT * FROM jobs WHERE key=?',key(query(1)))).one());
  await f.state.refresh(first,{api:'core',data:[]});assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs WHERE key=?',key(query(1)))).one().n,0);
  assert.equal(await f.state.complete(query(1)),true,'retirement preserves complete data');
  const newer=await f.state.claim((await f.store.exec('SELECT * FROM jobs WHERE key=?',key(query(2)))).one());
  await f.state.enqueue(query(2),5,Date.now(),true);await f.state.refresh(newer,{api:'core',data:[]});
  const retained=(await f.store.exec('SELECT * FROM jobs WHERE key=?',key(query(2)))).one();assert.equal(retained.dirty,1);assert.ok(retained.generation>newer.start_generation);
  const replaced=await f.state.claim((await f.store.exec('SELECT * FROM jobs WHERE key=?',key(query(3)))).one());
  await f.store.exec('UPDATE jobs SET lease_id=?,lease_until=? WHERE key=?','replacement',Date.now()+60000,key(query(3)));
  await f.state.refresh(replaced,{api:'core',data:[]});assert.equal((await f.store.exec('SELECT lease_id FROM jobs WHERE key=?',key(query(3)))).one().lease_id,'replacement');
 }finally{await f.close();}
});

test('retired historical status is recreated by demand and by a force-push webhook',async()=>{
 const f=await fixture();try{
  const q=query(1);await parent(f,1,sha(99));await complete(f,q);assert.equal(await f.state.retireUnneeded(await job(f,q)),true);
  await f.state.noteDemand(q);assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs WHERE key=?',key(q))).one().n,1);
  await f.store.exec('DELETE FROM jobs');await f.store.exec('DELETE FROM demand');
  const pointer={repository:'org/repo',event:'pull_request',delivery:'force-push-back',key:'fixture/input',received:Date.now()};
  await f.env.ARCHIVE.put(pointer.key,JSON.stringify({action:'synchronize',pull_request:{number:1,state:'open',head:{sha:sha(1)},base:{sha:sha(90)},updated_at:new Date().toISOString()}}));
  assert.equal(await f.state.ingest(pointer),true);const recreated=(await f.store.exec('SELECT * FROM jobs WHERE key=?',key(q))).one();assert.equal(recreated.dirty,1);assert.equal(await f.state.dormant(q),false);
 }finally{await f.close();}
});

test('case-only current heads stay active and a reopen recreates retired current-head work',async()=>{
 const f=await fixture();try{
  const q={repository:'org/repo',operation:'statuses',args:{sha:'a'.repeat(40)}};
  await f.state.put('pr','1',{number:1,state:'OPEN',headRefOid:q.args.sha});await f.state.put('pr','1',{number:1,state:'OPEN',headRefOid:q.args.sha.toUpperCase()});
  assert.equal(await f.state.dormant(q),false);await f.state.enqueueMany([{q,priority:20}],true);
  await complete(f,q);const row=(await f.store.exec('SELECT * FROM jobs WHERE key=?',key(q))).one();
  assert.deepEqual(await f.state.atomic(state=>state.claimCompanions(row,[row],undefined,true),'repair'),[]);
  await f.state.put('pr','1',{number:1,state:'CLOSED',headRefOid:q.args.sha});assert.equal(await f.state.retireUnneeded(row),true);
  const pointer={repository:'org/repo',event:'pull_request',delivery:'reopen',key:'fixture/reopen',received:Date.now()};
  await f.env.ARCHIVE.put(pointer.key,JSON.stringify({action:'reopened',pull_request:{number:1,state:'open',head:{sha:q.args.sha},base:{sha:sha(90)},updated_at:new Date().toISOString()}}));
  assert.equal(await f.state.ingest(pointer),true);assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs WHERE key=?',key(q))).one().n,1);assert.equal(await f.state.dormant(q),false);
 }finally{await f.close();}
});

test('native status dormancy, bulk retirement and enqueue retain identical conservative behavior',{skip:Boolean(process.env.TEST_POSTGRES_URL)},async()=>{
 const mf=await runtime(async()=>Response.json([]),{},{repositoryAlarms:false,repositoryExtension:`async fetch(req){
  if(new URL(req.url).pathname==='/fixture/status-many'){
   await this.store.exec('DELETE FROM jobs');const queries=Array.from({length:130},(_,i)=>({repository:'org/repo',operation:'statuses',args:{sha:(i+100).toString(16).padStart(40,'0')}}));
   for(const [i,q]of queries.entries()){
    await this.put('pr',String(i+100),{number:i+100,state:'OPEN',headRefOid:q.args.sha});
    await this.put('pr',String(i+100),{number:i+100,state:'OPEN',headRefOid:'f'.repeat(40)});
    await this.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)','statuses:'+JSON.stringify(q.args),'statuses',JSON.stringify(q.args),1,Date.now(),0,null);
   }
   await this.enqueueMany(queries.map(q=>({q,priority:20})));const jobs=(await this.store.exec('SELECT * FROM jobs')).toArray();
   const retired=await this.atomic(state=>state.claimCompanions(jobs[0],jobs,undefined,true),'repair');
   return Response.json({retired:retired.length,remaining:(await this.store.exec('SELECT COUNT(*) n FROM jobs')).one().n});
  }
  if(new URL(req.url).pathname==='/fixture/status-dormancy'){
   await this.store.exec('DELETE FROM jobs');const old='a'.repeat(40),fresh='b'.repeat(40),q={repository:'org/repo',operation:'statuses',args:{sha:old}},key='statuses:'+JSON.stringify(q.args);
   await this.put('pr','1',{number:1,state:'OPEN',headRefOid:old});await this.put('pr','1',{number:1,state:'OPEN',headRefOid:fresh});
   const historical=await this.dormant(q);await this.enqueueMany([{q,priority:20}],true);const skipped=(await this.store.exec('SELECT COUNT(*) n FROM jobs')).one().n===0;
   await this.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)',key,'statuses',JSON.stringify(q.args),1,Date.now(),0,null);await this.enqueue(q,20);
   const row=(await this.store.exec('SELECT * FROM jobs')).one();const retired=await this.atomic(state=>state.claimCompanions(row,[row],undefined,true),'repair');
   await this.put('pr','2',{number:2,state:'OPEN',headRefOid:old});const shared=await this.dormant(q);
   await this.put('pr','2',{number:2,state:'OPEN',headRefOid:null});const unknown=await this.dormant(q);
   return Response.json({historical,skipped,retired:retired.length,shared,unknown});
  }return super.fetch(req);
 }`});
 try{const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName(repository.id));await send(stub,'/configure',{repository:repository.name});const r=await send(stub,'/fixture/status-dormancy',{});assert.equal(r.status,200);assert.deepEqual(await r.json(),{historical:true,skipped:true,retired:1,shared:false,unknown:false});const many=await send(stub,'/fixture/status-many',{});assert.equal(many.status,200);assert.deepEqual(await many.json(),{retired:130,remaining:0});}finally{await mf.dispose();}
});
