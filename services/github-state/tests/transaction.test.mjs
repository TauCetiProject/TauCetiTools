import {test} from 'node:test';
import assert from 'node:assert/strict';
import {runtime,repository,send} from './runtime.mjs';

test('an interrupted publication cannot acknowledge or expose an overlapping SQLite/PG event',async()=>{
 let release,entered;const gate=new Promise(r=>release=r),ready=new Promise(r=>entered=r);
 const mf=await runtime(async(req,url)=>{if(url.hostname==='publication.test'){entered();await gate;return new Response('ready');}return Response.json([]);},{},{repositoryAlarms:false,repositoryExtension:`
 async fetch(req){
  if(new URL(req.url).pathname==='/test-publication'){
   try{await this.atomic(async state=>{
    await state.put('issue_comments','partial',{id:'partial',body:'must roll back'},Date.now(),false,'7');
    await state.membership('issue_comments:{"number":7}','partial',true);
    await fetch('https://publication.test/hold');
    throw Error('publication interrupted');
   });}catch{return Response.json({interrupted:true},{status:409});}
  }
  return super.fetch(req);
 }`});
 try{
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName(repository.id)),archive=await mf.getR2Bucket('ARCHIVE');
  await archive.put('fixture/concurrent-event',JSON.stringify({issue:{number:7},comment:{id:'retained',body:'must commit',created_at:'2026-10-19T00:00:00Z'}}));
  const pending=stub.fetch('https://internal/test-publication');await ready;
  let acknowledged=false;
  const event=send(stub,'/event',{repository:repository.name,event:'issue_comment',delivery:'concurrent-event',key:'fixture/concurrent-event',received:Date.now()}).then(response=>{acknowledged=true;return response;});
  const read=stub.fetch('https://internal/changes');
  await new Promise(r=>setTimeout(r,30));assert.equal(acknowledged,false,'event must wait for the active writer');
  release();assert.equal((await pending).status,409);assert.equal((await event).status,200);
  const during=await (await read).json();assert.equal(during.changes.some(row=>row.id==='partial'),false,'read cannot see an uncommitted revision');
  const after=await (await stub.fetch('https://internal/changes')).json();
  assert.equal(after.changes.some(row=>row.id==='partial'),false);assert.ok(after.changes.some(row=>row.id==='retained'));
  const before=after.revision;
  assert.equal((await send(stub,'/event',{repository:repository.name,event:'issue_comment',delivery:'concurrent-event',key:'fixture/concurrent-event',received:Date.now()})).status,200);
  assert.equal((await (await stub.fetch('https://internal/changes')).json()).revision,before,'acknowledged receipt survives rollback and deduplicates');
 }finally{release();await mf.dispose();}
});

test('slow Bors receipt reads do not hold the repository transaction or input gate',async()=>{
 let release,entered;const gate=new Promise(r=>release=r),ready=new Promise(r=>entered=r);
 const mf=await runtime(async(req,url)=>{assert.equal(url.hostname,'bors.test');entered();await gate;return Response.json({schema:'tauceti-bors.candidates/v1',repo:repository.name,base:'main',timestamp_unit:'unix_microseconds',candidates:[]});},{ALLOWED_REPOSITORIES:JSON.stringify([{...repository,bors_receipts_url:'https://bors.test/receipts'}])},{repositoryAlarms:false});
 let timer;
 try{
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName(repository.id)),archive=await mf.getR2Bucket('ARCHIVE');
  await send(stub,'/configure',{repository:repository.name});await archive.put('fixture/during-bors',JSON.stringify({issue:{number:7},comment:{id:42,body:'applied while Bors waits',created_at:'2026-10-19T00:00:00Z'}}));
  const refreshing=send(stub,'/activity-refresh',{});await ready;
  const event=send(stub,'/event',{repository:repository.name,event:'issue_comment',delivery:'during-bors',key:'fixture/during-bors',received:Date.now()});
  const applied=await Promise.race([event,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('Bors I/O held the repository writer')),2000);})]);
  clearTimeout(timer);assert.equal(applied.status,200);release();assert.equal((await refreshing).status,200);
 }finally{clearTimeout(timer);release();await mf.dispose();}
});

test('a standalone store batch rolls back earlier statements when a later statement fails',async()=>{
 const mf=await runtime(async()=>{throw Error('Unexpected upstream access');},{},{repositoryAlarms:false,repositoryExtension:`
 async fetch(req){
  const path=new URL(req.url).pathname;
  if(path==='/test-failed-batch'){try{await this.store.batch([{sql:'INSERT INTO meta VALUES(?,?)',bindings:['batch-must-roll-back','true']},{sql:'INSERT INTO missing_repository_table VALUES(?)',bindings:['failure']}]);}catch{return Response.json({interrupted:true},{status:409});}}
  if(path==='/test-batch-result')return Response.json((await this.store.exec('SELECT value FROM meta WHERE key=?','batch-must-roll-back')).toArray());
  return super.fetch(req);
 }`});
 try{const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName(repository.id));assert.equal((await stub.fetch('https://internal/test-failed-batch')).status,409);assert.deepEqual(await (await stub.fetch('https://internal/test-batch-result')).json(),[]);}finally{await mf.dispose();}
});
