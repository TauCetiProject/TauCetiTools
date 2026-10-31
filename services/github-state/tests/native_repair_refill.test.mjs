import {test} from 'node:test';
import assert from 'node:assert/strict';
import {runtime,repository,send} from './runtime.mjs';
import {nativeDrainRepairs} from './repair_background.mjs';
const skip=Boolean(process.env.TEST_POSTGRES_URL);
const quiet=`this.expire=async()=>({});this.requestOpenCoverage=async()=>{};this.maintenance=async()=>{};this.startStorageMaintenance=()=>{};`;
async function configured(mf){
 const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName(repository.id));
 assert.equal((await send(stub,'/configure',{repository:repository.name})).status,200);return stub;
}

test('native SQLite excludes 400 active keys and renews only 399 still-owned leases',{skip},async()=>{
 const mf=await runtime(async()=>Response.json([]),{},{repositoryAlarms:false,repositoryExtension:`async fetch(req){
  if(new URL(req.url).pathname==='/fixture/owned-400'){
   ${quiet}await this.store.exec('DELETE FROM jobs');
   await this.enqueueMany(Array.from({length:400},(_,i)=>({q:{repository:'org/repo',operation:'issue_comments',args:{number:i+100}},priority:5})),true);
   await this.store.exec("UPDATE jobs SET lane=CASE WHEN CAST(json_extract(query,'$.args.number') AS INTEGER)<200 THEN 'requested' WHEN CAST(json_extract(query,'$.args.number') AS INTEGER)<300 THEN 'active' WHEN CAST(json_extract(query,'$.args.number') AS INTEGER)<400 THEN 'verification' ELSE 'archive' END");
   let release,ready,calls=0;const gate=new Promise(resolve=>release=resolve),started=new Promise(resolve=>ready=resolve);
   this.env={...this.env,INSTALLATIONS:{idFromName:id=>id,get:()=>({fetch:async(_url,init)=>{
    const jobs=JSON.parse(init.body).jobs;if(jobs.length!==100)throw Error('fragmented full group');if(++calls===4)ready();await gate;
    return Response.json({results:jobs.map(()=>({api:'graphql',data:[]}))});
   }})}};
   await this.runAlarm();await started;
   const c=this.repairCoordinator,owned=c.owned.size,clock=Date.now;let selection,blocked,renewed;
   try{
    Date.now=()=>clock()+70000;
    const q={repository:'org/repo',operation:'files',args:{number:999}},key='files:{"number":999}';
    await this.enqueue(q,5);c.current.paths.set(key,new Set(['auto']));blocked=!(await this.nextJob());c.current.paths.delete(key);
    selection=JSON.parse((await this.nextJob()).query).args.number;
    await this.store.exec('UPDATE jobs SET lease_id=?,lease_until=? WHERE key=?','replacement',Date.now()+300000,'issue_comments:{"number":100}');
    await this.atomic(async()=>{},'repair');renewed=this.renewedLeases;
    await this.runAlarm();await this.store.exec('DELETE FROM jobs WHERE key=?',key);
   }finally{Date.now=clock;release();}
   ${nativeDrainRepairs}
   return Response.json({owned,calls,selection,blocked,renewed,complete:(await this.store.exec("SELECT COUNT(*) n FROM collections WHERE operation='issue_comments' AND complete=1")).one().n,replacement:(await this.store.exec('SELECT lease_id FROM jobs WHERE key=?','issue_comments:{"number":100}')).one().lease_id});
  }return super.fetch(req);
 }`});
 try{
  const r=await send(await configured(mf),'/fixture/owned-400',{});assert.equal(r.status,200);
  assert.deepEqual(await r.json(),{owned:400,calls:4,selection:999,blocked:true,renewed:399,complete:399,replacement:'replacement'});
 }finally{await mf.dispose();}
});

test('native SQLite stops unproductive sweeps without a near-immediate backlog alarm',{skip},async()=>{
 const mf=await runtime(async()=>Response.json([]),{},{repositoryAlarms:false,repositoryExtension:`async fetch(req){
  if(new URL(req.url).pathname==='/fixture/deferred-budget'){
   ${quiet}await this.store.exec('DELETE FROM jobs');
   await this.enqueueMany(Array.from({length:40},(_,number)=>({q:{repository:'org/repo',operation:'files',args:{number}},priority:5})),true);
   let calls=0,at=0;const now=Date.now(),until=now+3600000;this.setAlarm=async value=>{at=value;};
   this.env={...this.env,INSTALLATIONS:{idFromName:id=>id,get:()=>({fetch:async()=>{calls++;return Response.json({deferred:true,category:'primary:core',retry_at:until,scope:{kind:'pool',api:'core'}});}})}};
   await this.runAlarm();${nativeDrainRepairs}
   // An already-armed future lease fallback may precede the pool deadline.
   // Neither that fallback nor the final retry may become a now+100ms sweep.
   return Response.json({calls,future:at>=now+50000,stopped:this.repairCoordinator.unproductiveStop,untouched:(await this.store.exec('SELECT COUNT(*) n FROM jobs WHERE wait_reason IS NULL')).one().n});
  }return super.fetch(req);
 }`});
 try{
  const r=await send(await configured(mf),'/fixture/deferred-budget',{});assert.equal(r.status,200);
  assert.deepEqual(await r.json(),{calls:16,future:true,stopped:true,untouched:24});
 }finally{await mf.dispose();}
});

for(const hint of ['blocked','unknown'])test('native SQLite quota retry skips repeated maintenance and handles an '+hint+' hint',{skip},async()=>{
 const mf=await runtime(async()=>Response.json([]),{},{repositoryAlarms:false,repositoryExtension:`async fetch(req){
  if(new URL(req.url).pathname==='/fixture/quota-retry'){
   let expiry=0,maintenance=0,coverage=0,calls=0,hints=0;this.expire=async()=>{expiry++;};this.maintenance=async()=>{maintenance++;};this.requestOpenCoverage=async()=>{coverage++;};this.startStorageMaintenance=()=>{};
   await this.store.exec('DELETE FROM jobs');await this.enqueueMany([{q:{repository:'org/repo',operation:'files',args:{number:100}},priority:5}],true);
   this.setAlarm=async(at,quota=false)=>{this.repairAlarm={at,quota};};
   this.env={...this.env,INSTALLATIONS:{idFromName:id=>id,get:()=>({fetch:async url=>{
    if(url.endsWith('/availability')){hints++;if('${hint}'==='unknown')return Response.json({}, {status:503});}
    else calls++;
    return Response.json({deferred:true,category:'capacity',retry_at:Date.now()+250,scope:{kind:'installation'}});
   }})}};
   await this.runAlarm();${nativeDrainRepairs}const initial=calls,clock=Date.now;let offset=0;
   try{Date.now=()=>clock()+offset;
    for(let i=0;i<3;i++){offset=this.repairAlarm.at-clock()+1;await this.alarm();${nativeDrainRepairs}}
   }finally{Date.now=clock;}
   const before={expiry,maintenance,coverage};await this.runAlarm();${nativeDrainRepairs}
   return Response.json({initial,calls,hints,before,after:{expiry,maintenance,coverage}});
  }return super.fetch(req);
 }`});
 try{
  const r=await send(await configured(mf),'/fixture/quota-retry',{});assert.equal(r.status,200);const body=await r.json();
  assert.equal(body.initial,1);assert.equal(body.hints,3);assert.deepEqual(body.before,{expiry:1,maintenance:1,coverage:1});
  assert.deepEqual(body.after,{expiry:2,maintenance:2,coverage:2});
  assert.equal(body.calls,hint==='blocked'?1:4,'unknown optional hints fall back to actual requests');
 }finally{await mf.dispose();}
});

for(const category of ['capacity','secondary'])test('native SQLite restriction with three occupied slots retains its actual '+category+' wake',{skip},async()=>{
 const mf=await runtime(async()=>Response.json([]),{},{repositoryAlarms:false,repositoryExtension:`async fetch(req){
  if(new URL(req.url).pathname==='/fixture/partial-restriction'){
   let expiry=0,maintenance=0,calls=0,hints=0,release;const gate=new Promise(resolve=>release=resolve),alarms=[];
   this.expire=async()=>{expiry++;};this.maintenance=async()=>{maintenance++;};this.requestOpenCoverage=async()=>{};this.startStorageMaintenance=()=>{};
   this.setAlarm=async(at,quota=false)=>{this.repairAlarm={at,quota};alarms.push({at,quota});};
   await this.store.exec('DELETE FROM jobs');await this.enqueueMany(Array.from({length:8},(_,number)=>({q:{repository:'org/repo',operation:'files',args:{number}},priority:5})),true);
   let until;this.env={...this.env,INSTALLATIONS:{idFromName:id=>id,get:()=>({fetch:async url=>{
    if(url.endsWith('/availability')){hints++;return Response.json({deferred:true,category:'capacity',retry_at:Date.now()+250,scope:{kind:'installation'}});}
    calls++;if(calls<=3)await gate;
    else if(calls===4){until=Date.now()+('${category}'==='capacity'?250:30000);return Response.json({deferred:true,category:'${category}',retry_at:until,scope:{kind:'installation'}});}
    return Response.json({api:'core',data:[]});
   }})}};
   await this.runAlarm();
   while(this.repairCoordinator.groups.size===4)await new Promise(resolve=>setTimeout(resolve,1));
   await this.repairCoordinator.flight;
   const at=this.repairAlarm.at,quota=this.repairAlarm.quota,occupied=this.repairCoordinator.groups.size;
   const clock=Date.now;let offset=0;
   try{Date.now=()=>clock()+offset;
    if('${category}'==='capacity')for(let i=0;i<3;i++){offset=this.repairAlarm.at-clock()+1;await this.alarm();}
   }finally{Date.now=clock;}
   const before={expiry,maintenance,calls,hints},alarmCount=alarms.length;
   this.quotaRestriction=undefined;release();${nativeDrainRepairs}
   return Response.json({occupied,actualDeadline:at>=until,quota,before,alarmCount});
  }return super.fetch(req);
 }`});
 try{
  const r=await send(await configured(mf),'/fixture/partial-restriction',{});assert.equal(r.status,200);const body=await r.json();
  assert.equal(body.occupied,3);assert.equal(body.actualDeadline,true);assert.equal(body.quota,category==='capacity');
  assert.deepEqual(body.before,{expiry:1,maintenance:1,calls:4,hints:category==='capacity'?3:0});
  assert.ok(body.alarmCount<=12,'a partial registry does not create a self-generated maintenance loop');
 }finally{await mf.dispose();}
});
