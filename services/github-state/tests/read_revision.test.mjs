import {test} from 'node:test';
import assert from 'node:assert/strict';
import {runtime,repository,send} from './runtime.mjs';

const sha='a'.repeat(40);
const extension=`
 builds=0;mode='unrelated';
 async query(q){
  const revision=await this.revision();this.builds++;
  const number=this.mode==='unrelated'?8:7;
  if(this.mode==='other_child'){
   const kind=['issue_comments','review_comments','files'].includes(q.operation)?q.operation:'issue_comments';
   const id=kind==='files'?'8:outside.lean':'other-'+this.builds;
   await this.put(kind,id,{id,body:String(this.builds)},Date.now(),false,'8');
  }
  else if(this.mode==='other_sha')await this.put('status_context','other-sha-'+this.builds,{state:'SUCCESS'},Date.now(),false,'${'b'.repeat(40)}');
  else if(this.mode==='fallback')await this.put('pr','pr:{"number":7}',{headRefOid:String(this.builds).padStart(40,'0')},Date.now(),false,'7');
  else if(this.mode==='membership')await this.membership('issue_comments:{"number":7}','new-'+this.builds,true);
  else if(this.mode==='tombstone')await this.put('review_comments','deleted-'+this.builds,{id:'deleted-'+this.builds},Date.now(),true,'7');
  else if(q.operation==='statuses' && this.mode==='related')await this.put('status_context','status-'+this.builds,{state:'SUCCESS'},Date.now(),false,'${sha}');
  else if(this.mode==='related' && ['issue_comments','review_comments','files','progress'].includes(q.operation)){
   const kind=q.operation==='progress'?'issue_comments':q.operation;
   await this.put(kind,'row-'+this.builds,{id:'row-'+this.builds},Date.now(),false,'7');
  }else await this.put('pr',String(number),{number,headRefOid:String(this.builds).padStart(40,'0')},Date.now(),false,String(number));
  return {version:1,repository:q.repository,operation:q.operation,status:'ok',revision,complete:true,coverage:'complete',data:[{build:this.builds}]};
 }
 async fetch(req){
  if(new URL(req.url).pathname==='/fixture/mode'){this.mode=(await req.json()).mode;this.builds=0;this.completedQueries.clear();return Response.json({ok:true});}
  return super.fetch(req);
 }
`;

for(const operation of ['pr','progress','issue_comments','review_comments','files','statuses'])test(operation+' retries only changes to its own input',async()=>{
 let network=0;
 const mf=await runtime(async()=>{network++;throw Error('unexpected GitHub read');},{},{repositoryAlarms:false,repositoryExtension:extension});
 try{
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName(repository.id));
  await send(stub,'/configure',{repository:repository.name});
  const q={repository:repository.name,operation,args:operation==='statuses'?{sha}:{number:7}};
  const first=await (await send(stub,'/query',q)).json();
  assert.equal(first.status,'ok');assert.equal(first.data[0].build,1,'unrelated input must not repeat a complete read');
  assert.deepEqual(await (await send(stub,'/query',q)).json(),first,'unrelated input must not force history decoding on a repeated read');
  for(const mode of ['other_child','other_sha']){
   await send(stub,'/fixture/mode',{mode});
   const unrelated=await (await send(stub,'/query',q)).json();
   assert.equal(unrelated.status,'ok');assert.equal(unrelated.data[0].build,1);
  }
  await send(stub,'/fixture/mode',{mode:'related'});
  const changed=await (await send(stub,'/query',q)).json();
  assert.equal(changed.reason,'concurrent_input','continuing edits to this input still fail closed');
  if(operation==='issue_comments'){
   for(const mode of ['membership']){
    await send(stub,'/fixture/mode',{mode});
    assert.equal((await (await send(stub,'/query',q)).json()).reason,'concurrent_input');
   }
   await send(stub,'/fixture/mode',{mode:'head'});
   assert.equal((await (await send(stub,'/query',q)).json()).status,'ok','a PR label/head update cannot change its stored comment history');
  }
  if(['pr','progress'].includes(operation)){
   for(const mode of ['head','fallback']){
    await send(stub,'/fixture/mode',{mode});
    assert.equal((await (await send(stub,'/query',q)).json()).reason,'concurrent_input');
   }
  }
  if(operation==='review_comments'){
   await send(stub,'/fixture/mode',{mode:'tombstone'});
   assert.equal((await (await send(stub,'/query',q)).json()).reason,'concurrent_input');
  }
  assert.equal(network,0);
 }finally{await mf.dispose();}
});

test('real comment reads remain coherent and reusable across unrelated comment updates',async()=>{
 const mf=await runtime(async()=>{throw Error('unexpected GitHub read');},{},{repositoryAlarms:false,repositoryExtension:`
  decodes=0;mode='unrelated';
  async decode(text){
   this.owner.decodes++;
   const parent=this.mode==='related'?'7':'8';
   const id=this.mode==='related'?42:'outside-'+this.owner.decodes;
   await this.put('issue_comments',String(id),{id,body:'changed-'+this.owner.decodes},Date.now(),false,parent);
   return super.decode(text);
  }
  async fetch(req){
   const path=new URL(req.url).pathname;
   if(path==='/fixture/seed'){
    await this.put('issue_comments','42',{id:42,body:'stored'},Date.now(),false,'7');
    await this.membership('issue_comments:{"number":7}','42',true);
    await this.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)','issue_comments:{"number":7}','issue_comments','{"number":7}',1,Date.now(),await this.revision(),null);
    return Response.json({ok:true});
   }
   if(path==='/fixture/related'){
    this.mode='related';this.completedQueries.clear();return Response.json({ok:true});
   }
   if(path==='/fixture/edit'){
    await this.put('issue_comments','42',{id:42,body:'edited'},Date.now(),false,'7');
    return Response.json({ok:true});
   }
   return super.fetch(req);
  }
 `});
 try{
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName(repository.id));
  await send(stub,'/configure',{repository:repository.name});await send(stub,'/fixture/seed',{});
  const q={repository:repository.name,operation:'issue_comments',args:{number:7}};
  const first=await (await send(stub,'/query',q)).json();
  assert.equal(first.status,'ok');assert.deepEqual(first.data,[{id:42,body:'stored'}]);
  assert.deepEqual(await (await send(stub,'/query',q)).json(),first);
  const url='https://state/v1/query?'+new URLSearchParams({repository:repository.name,operation:q.operation,args:JSON.stringify(q.args)});
  const headers={Authorization:'Bearer client','If-None-Match':'"'+first.revision+'"'};
  assert.equal((await mf.dispatchFetch(url,{headers})).status,304,'the public endpoint preserves conditional reads despite unrelated revisions');
  await send(stub,'/fixture/edit',{}); // Keep the old cache entry in place.
  const updated=await mf.dispatchFetch(url,{headers});assert.equal(updated.status,200);
  const fresh=await updated.json();assert.deepEqual(fresh.data,[{id:42,body:'edited'}]);
  assert.ok(fresh.revision>first.revision,'a real edit invalidates the cached result and its ETag');
  await send(stub,'/fixture/related',{});
  assert.equal((await (await send(stub,'/query',q)).json()).reason,'concurrent_input');
 }finally{await mf.dispose();}
});
