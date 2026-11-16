import {test} from 'node:test';
import assert from 'node:assert/strict';
import {runtime,repository,response,send} from './runtime.mjs';

test('selected main reference repair uses a narrow existing refs recipe',async()=>{
 let calls=0;
 const mf=await runtime(async(req,url)=>{
  calls++;
  assert.equal(url.pathname,'/graphql');
  assert.match((await req.json()).query,/ref\(qualifiedName:"refs\/heads\/main"\)/);
  return response({data:{rateLimit:{cost:1,remaining:4999,resetAt:new Date(Date.now()+300000).toISOString()},
                        repository:{ref:{target:{oid:'a'.repeat(40)}}}}});
 });
 try{
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName('200'));
  const result=await (await send(stub,'/read',{repository,query:{operation:'refs',args:{name:'main'}},page:1})).json();
  assert.equal(result.data[0].ref,'refs/heads/main');
  assert.equal(calls,1);
 }finally{await mf.dispose();}
});

test('selected main reads the existing exact webhook resource without a branch inventory',async()=>{
 const mf=await runtime(async()=>{throw Error('unexpected upstream request');},{},{repositoryAlarms:false,repositoryExtension:`
 async fetch(req){
  if(new URL(req.url).pathname==='/fixture/main'){
   await this.atomic(async state=>{
    await state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
    await state.put('refs','refs/heads/main',{ref:'refs/heads/main',object:{sha:'a'.repeat(40)}},Date.now());
    await state.put('refs','refs/heads/other',{ref:'refs/heads/other',object:{sha:'b'.repeat(40)}},Date.now());
   });
   return Response.json({ok:true});
  }
  return super.fetch(req);
 }`});
 try{
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName(repository.id));
  await send(stub,'/fixture/main',{});
  const query={repository:repository.name,operation:'refs',args:{name:'main'}};
  const result=await (await send(stub,'/query',query)).json();
  assert.equal(result.status,'ok');assert.equal(result.complete,true);
  assert.deepEqual(result.data,[{ref:'refs/heads/main',object:{sha:'a'.repeat(40)}}]);
  assert.ok(result.observed_at>0);
 }finally{await mf.dispose();}
});

test('unchanged cached evidence reports the revision through which it was revalidated',async()=>{
 const mf=await runtime(async()=>{throw Error('unexpected upstream request');},{},{repositoryAlarms:false,repositoryExtension:`
 async fetch(req){
  const path=new URL(req.url).pathname;
  if(path==='/fixture/header'){
   await this.atomic(async state=>{
    await state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
    await state.put('pr','7',{number:7,state:'OPEN',headRefOid:'a'.repeat(40)},Date.now());
    await state.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)','pr:{"number":7}','pr','{"number":7}',1,Date.now(),await state.revision(),null);
   });return Response.json({ok:true});
  }
  if(path==='/fixture/unrelated'){
   await this.atomic(state=>state.put('pr','8',{number:8,state:'OPEN',headRefOid:'b'.repeat(40)},Date.now()));
   return Response.json({ok:true});
  }
  return super.fetch(req);
 }`});
 try{
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName(repository.id));
  await send(stub,'/fixture/header',{});
  const query={repository:repository.name,operation:'pr',args:{number:7}};
  const first=await (await send(stub,'/query',query)).json();
  await send(stub,'/fixture/unrelated',{});
  const response=await send(stub,'/query',query),next=await response.json();
  assert.equal(next.revision,first.revision);
  assert.ok(Number(response.headers.get('X-TauCeti-Checked-Revision'))>first.revision);
  assert.deepEqual(next.data,first.data);
 }finally{await mf.dispose();}
});

test('authenticated observation reads never revive verification or request cold repairs',async()=>{
 const mf=await runtime(async()=>{throw Error('unexpected upstream request');},{},{repositoryAlarms:false,repositoryExtension:`
 async fetch(req){
  const path=new URL(req.url).pathname;
  if(path==='/fixture/observe'){
   await this.atomic(async state=>{
    await state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
    await state.put('pr','7',{number:7,state:'OPEN',headRefOid:'a'.repeat(40)},Date.now());
    await state.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)','pr:{"number":7}','pr','{"number":7}',1,Date.now(),await state.revision(),null);
   });return Response.json({ok:true});
  }
  if(path==='/fixture/demands')return Response.json({jobs:(await this.store.exec('SELECT COUNT(*) n FROM jobs')).one().n,demand:(await this.store.exec('SELECT COUNT(*) n FROM demand')).one().n});
  return super.fetch(req);
 }`});
 try{
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName(repository.id));
  await send(stub,'/fixture/observe',{});
  for(const operation of ['pr','issue_comments']){
   const url='https://test/v1/query?'+new URLSearchParams({repository:repository.name,operation,args:'{"number":7}'});
   const result=await (await mf.dispatchFetch(url,{headers:{Authorization:'Bearer client','X-TauCeti-Observe-Only':'true'}})).json();
   assert.equal(result.status,operation==='pr'?'ok':'unavailable');
  }
  const metrics=await (await send(stub,'/fixture/demands',{})).json();
  assert.deepEqual(metrics,{jobs:0,demand:0});
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
