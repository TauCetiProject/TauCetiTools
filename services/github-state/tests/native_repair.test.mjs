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

test('native Workers SQLite records a 100-history capacity deferral in bulk',{skip:Boolean(process.env.TEST_POSTGRES_URL)},async()=>{
 const mf=await runtime(async()=>Response.json([]),{},{repositoryAlarms:false,repositoryExtension:`async fetch(req){
  if(new URL(req.url).pathname==='/fixture/deferred-batch'){
   await this.store.exec('DELETE FROM jobs');const until=Date.now()+300000;
   await this.enqueueMany(Array.from({length:100},(_,i)=>({q:{repository:'org/repo',operation:'issue_comments',args:{number:i+1}},priority:5})),true);
   this.env={...this.env,INSTALLATIONS:{idFromName:id=>id,get:()=>({fetch:async()=>Response.json({deferred:true,category:'capacity',retry_at:until})})}};
   await this.dispatch(await this.claim(await this.nextJob()));
   return Response.json((await this.store.exec("SELECT COUNT(*) n,SUM(attempt) attempts,COUNT(lease_id) leases FROM jobs WHERE wait_reason='capacity' AND due=?",until)).one());
  }
  return super.fetch(req);
 }`});
 try{
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName(repository.id));
  await send(stub,'/configure',{repository:repository.name});const r=await send(stub,'/fixture/deferred-batch',{});assert.equal(r.status,200);assert.deepEqual(await r.json(),{n:100,attempts:0,leases:0});
 }finally{await mf.dispose();}
});

test('native Workers SQLite isolates a CHECK failure and replays the healthy publication',{skip:Boolean(process.env.TEST_POSTGRES_URL)},async()=>{
 const mf=await runtime(async()=>Response.json([]),{},{repositoryAlarms:false,repositoryExtension:`async fetch(req){
  if(new URL(req.url).pathname==='/fixture/member-check'){
   await this.store.exec('DELETE FROM jobs');await this.store.exec('CREATE TABLE fixture_member_check(id INTEGER CHECK(id>0))');
   await this.enqueueMany([100,101].map(number=>({q:{repository:'org/repo',operation:'issue_comments',args:{number}},priority:5})),true);
   const bad='issue_comments:{"number":101}',wrap=target=>new Proxy(target,{get(target,prop){
    if(prop==='transaction')return(apply,purpose)=>target.transaction(tx=>apply(wrap(tx)),purpose);
    if(prop==='exec')return async(sql,...args)=>{if(sql.includes('INTO collections')&&args.includes(bad))await target.exec('INSERT INTO fixture_member_check VALUES(?)',-1);return target.exec(sql,...args);};
    if(prop==='batch')return async statements=>{for(const s of statements)if(s.sql.includes('INTO collections')&&(s.bindings||[]).includes(bad))await target.exec('INSERT INTO fixture_member_check VALUES(?)',-1);return target.batch(statements);};
    const value=Reflect.get(target,prop,target);return typeof value==='function'?value.bind(target):value;
   }});this.store=wrap(this.store);
   this.env={...this.env,INSTALLATIONS:{idFromName:id=>id,get:()=>({fetch:async()=>Response.json({results:[{api:'graphql',data:[]},{api:'graphql',data:[]}]})})}};
   await this.dispatch(await this.claim(await this.nextJob()));
   return Response.json({complete:(await this.store.exec("SELECT COUNT(*) n FROM collections WHERE operation='issue_comments' AND complete=1")).one().n,bad:(await this.store.exec('SELECT attempt,wait_reason FROM jobs WHERE key=?',bad)).one(),completions:this.repairMetrics.summary().repair_hour.filter(x=>x.event==='completion').reduce((n,x)=>n+x.n,0)});
  }
  return super.fetch(req);
 }`});
 try{
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName(repository.id));await send(stub,'/configure',{repository:repository.name});
  const response=await send(stub,'/fixture/member-check',{});assert.equal(response.status,200);assert.deepEqual(await response.json(),{complete:1,bad:{attempt:1,wait_reason:'transport_or_parser'},completions:1});
 }finally{await mf.dispose();}
});
