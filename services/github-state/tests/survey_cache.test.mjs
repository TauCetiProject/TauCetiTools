import {test} from 'node:test';
import assert from 'node:assert/strict';
import {runtime,repository,send} from './runtime.mjs';

const extension=`
 builds=0;
 async query(q){
  const revision=await this.revision();this.builds++;
  await this.nextRevision('fixture','changing-input');
  return {version:1,repository:q.repository,operation:q.operation,status:'ok',revision,complete:true,coverage:'complete',data:[{build:this.builds}]};
 }
 async fetch(req){
  if(new URL(req.url).pathname==='/fixture/expire'){
   for(const entry of this.completedQueries.values())entry.built=Date.now()-10001;
   return Response.json({ok:true});
  }
  return super.fetch(req);
 }
`;

test('survey discovery tolerates revision churn with bounded reuse; PR reads stay fresh',async()=>{
 let network=0;
 const mf=await runtime(async()=>{network++;throw Error('unexpected GitHub request');},{},{repositoryAlarms:false,repositoryExtension:extension});
 try{
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName(repository.id));
  await send(stub,'/configure',{repository:repository.name});
  const q={repository:repository.name,operation:'survey',args:{roadmap:'auto',stages:'review,fix'}};
  const first=await (await send(stub,'/query',q)).json();
  assert.equal(first.status,'ok');assert.equal(first.data[0].build,1);
  const second=await (await send(stub,'/query',q)).json();
  assert.deepEqual(second,first,'a repository revision must not force discovery to rebuild immediately');
  await send(stub,'/fixture/expire',{});
  const refreshed=await (await send(stub,'/query',q)).json();
  assert.equal(refreshed.status,'ok');assert.equal(refreshed.data[0].build,2,'reuse expires after ten seconds');
  const pr={repository:repository.name,operation:'pr',args:{number:1}};
  const direct=await (await send(stub,'/query',pr)).json();
  // Production's ordinary reads return the query result; main's stableQuery
  // rejects a deliberately changing input after three attempts. Both must
  // avoid the stale survey cache lane.
  const next=await (await send(stub,'/query',pr)).json();
  if(direct.status==='ok')assert.notDeepEqual(next,direct);
  else assert.equal(next.status,'unavailable');
  assert.equal(network,0);
 }finally{await mf.dispose();}
});
