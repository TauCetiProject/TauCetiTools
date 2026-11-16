import {test} from 'node:test';
import assert from 'node:assert/strict';
import {runtime,repository,response,send} from './runtime.mjs';

test('selected main reference repair uses a narrow existing refs recipe',async()=>{
 let calls=0;
 const mf=await runtime(async(req,url)=>{
  calls++;
  assert.equal(url.pathname,'/repos/org/repo/git/matching-refs/heads/main');
  return response([{ref:'refs/heads/main',object:{sha:'a'.repeat(40),type:'commit'}}],'core');
 });
 try{
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName('200'));
  const result=await (await send(stub,'/read',{repository,query:{operation:'refs',args:{name:'main'}},page:1})).json();
  assert.equal(result.data[0].ref,'refs/heads/main');
  assert.equal(calls,1);
 }finally{await mf.dispose();}
});

test('the retained change journal identifies deleted comment parents for incremental readers',async()=>{
 const mf=await runtime(async()=>{throw Error('unexpected upstream request');},{},{repositoryAlarms:false,repositoryExtension:`
 async fetch(req){
  if(new URL(req.url).pathname==='/fixture/changes'){
   await this.atomic(async state=>{
    await state.put('issue_comments','42',{id:'42',body:'scoreboard'},Date.now(),false,'7');
    await state.put('issue_comments','42',{id:'42',body:'scoreboard'},Date.now()+1,true,'7');
   });
   return Response.json({ok:true});
  }
  return super.fetch(req);
 }`});
 try{
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName(repository.id));
  await send(stub,'/fixture/changes',{});
  const result=await (await stub.fetch('https://internal/changes?after=0')).json();
  const comments=result.changes.filter(row=>row.kind==='issue_comments');
  assert.equal(comments.length,2);
  assert.ok(comments.every(row=>row.parent==='7'));
  assert.equal(comments[1].tombstone,1);
  assert.ok(comments[1].revision>comments[0].revision);
 }finally{await mf.dispose();}
});
