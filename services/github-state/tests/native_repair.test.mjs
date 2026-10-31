import {test} from 'node:test';
import assert from 'node:assert/strict';
import {runtime,repository,send} from './runtime.mjs';

test('native Workers SQLite claims and releases a full 100-history batch',{skip:Boolean(process.env.TEST_POSTGRES_URL)},async()=>{
 const mf=await runtime(async()=>Response.json([]),{},{repositoryAlarms:false,repositoryExtension:`async fetch(req){
  if(new URL(req.url).pathname==='/fixture/full-batch'){
   await this.store.exec('DELETE FROM jobs');
   const input=Array.from({length:100},(_,i)=>({q:{repository:'org/repo',operation:'issue_comments',args:{number:i+1}},priority:5}));
   await this.enqueueMany(input,true);let requests=0;
   this.env={...this.env,INSTALLATIONS:{idFromName:id=>id,get:()=>({fetch:async(_url,init)=>{
    requests++;const jobs=JSON.parse(init.body).jobs;
    if(jobs.length!==100)throw Error('full batch was fragmented');
    return Response.json({results:jobs.map(()=>({api:'graphql',data:[]}))});
   }})}};
   await this.dispatch(await this.claim(await this.nextJob()));
   return Response.json({requests,complete:(await this.store.exec("SELECT COUNT(*) n FROM collections WHERE operation='issue_comments' AND complete=1")).one().n,leases:(await this.store.exec('SELECT COUNT(*) n FROM jobs WHERE lease_id IS NOT NULL')).one().n});
  }
  return super.fetch(req);
 }`});
 try{
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName(repository.id));
  assert.equal((await send(stub,'/configure',{repository:repository.name})).status,200);
  const response=await send(stub,'/fixture/full-batch',{});
  assert.equal(response.status,200);assert.deepEqual(await response.json(),{requests:1,complete:100,leases:0});
 }finally{await mf.dispose();}
});

test('native Workers SQLite event scheduling chunks bulk statements under the binding limit',{skip:Boolean(process.env.TEST_POSTGRES_URL)},async()=>{
 const mf=await runtime(async()=>Response.json([]),{},{repositoryAlarms:false,repositoryExtension:`async fetch(req){
  if(new URL(req.url).pathname==='/fixture/ingest-batch'){
   await this.store.exec('DELETE FROM jobs');
   await this.atomic(state=>state.enqueueIngest(Array.from({length:100},(_,i)=>({q:{repository:'org/repo',operation:'issue_comments',args:{number:i+1}},priority:2,dirtyIfMissing:true}))));
   return Response.json((await this.store.exec("SELECT COUNT(*) n,SUM(dirty) dirty,MIN(generation) generation FROM jobs WHERE lane='active'")).one());
  }
  return super.fetch(req);
 }`});
 try{
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName(repository.id));
  assert.equal((await send(stub,'/configure',{repository:repository.name})).status,200);
  const r=await send(stub,'/fixture/ingest-batch',{});assert.equal(r.status,200);assert.deepEqual(await r.json(),{n:100,dirty:100,generation:1});
 }finally{await mf.dispose();}
});
