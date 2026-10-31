import {test} from 'node:test';
import assert from 'node:assert/strict';
import {runtime,repository,send} from './runtime.mjs';
import {nativeDrainRepairs} from './repair_background.mjs';
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const quiet=`this.expire=async()=>({});this.requestOpenCoverage=async()=>{};this.maintenance=async()=>{};this.startStorageMaintenance=()=>{};`;
async function configured(mf){
 const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName(repository.id));
 assert.equal((await send(stub,'/configure',{repository:repository.name})).status,200);return stub;
}

test('native Durable Object fetch acknowledges cancellation of a slow recipient',{skip:Boolean(process.env.TEST_POSTGRES_URL)},async()=>{
 const mf=await runtime(async()=>Response.json([]),{},{repositoryAlarms:false,repositoryExtension:`async fetch(req){
  const path=new URL(req.url).pathname;
  if(path==='/fixture/slow-recipient'){
   await new Promise(resolve=>setTimeout(resolve,60000));return Response.json({late:true});
  }
  if(path==='/fixture/cancel-recipient'){
   const peer=this.env.REPOSITORIES.get(this.env.REPOSITORIES.idFromName('slow-recipient'));
   const start=Date.now();try{await peer.fetch('https://internal/fixture/slow-recipient',{signal:AbortSignal.timeout(50)});return Response.json({canceled:false});}
   catch(error){return Response.json({canceled:true,name:error.name,ms:Date.now()-start});}
  }
  return super.fetch(req);
 }`});
 let timer;try{
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName(repository.id));
  const response=await Promise.race([send(stub,'/fixture/cancel-recipient',{}),new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('native DO fetch ignored cancellation')),5000);})]);
  const body=await response.json();assert.equal(body.canceled,true);assert.ok(['TimeoutError','AbortError'].includes(body.name));assert.ok(body.ms>=40&&body.ms<5000);
 }finally{clearTimeout(timer);await mf.dispose();}
});

test('native owner recovery releases four uncancellable archive slots after durable backoff',{skip:Boolean(process.env.TEST_POSTGRES_URL)},async()=>{
 const mf=await runtime(async()=>Response.json([]),{},{repositoryAlarms:false,repositoryExtension:`
 fixtureOwner=crypto.randomUUID();get repairHopLimit(){return 150;}
 async fetch(req){const path=new URL(req.url).pathname;
  if(path==='/fixture/archive-stall'){
   ${quiet}await this.store.exec('DELETE FROM jobs');
   await this.enqueueMany(Array.from({length:4},(_,number)=>({q:{repository:'org/repo',operation:'files',args:{number}},priority:5})),true);
   this.env={...this.env,ARCHIVE:new Proxy(this.env.ARCHIVE,{get(target,key){const v=Reflect.get(target,key,target);if(key==='put')return()=>new Promise(()=>{});return typeof v==='function'?v.bind(target):v;}}),INSTALLATIONS:{idFromName:id=>id,get:()=>({fetch:async()=>Response.json({api:'core',data:[]})})}};
   await this.runAlarm();return Response.json({owner:this.fixtureOwner,groups:this.repairCoordinator.groups.size});
  }
  if(path==='/fixture/recovery'){
   ${quiet}const before=(await this.store.exec('SELECT COUNT(*) n,SUM(attempt) attempts,COUNT(lease_id) leases,MIN(due) due FROM jobs')).one();
   await this.runAlarm();
   return Response.json({owner:this.fixtureOwner,before,groups:this.repairCoordinator.groups.size});
  }
  if(path==='/fixture/finish-recovered'){
   ${quiet}const clock=Date.now;
   this.env={...this.env,INSTALLATIONS:{idFromName:id=>id,get:()=>({fetch:async()=>Response.json({api:'core',data:[]})})}};
   try{Date.now=()=>clock()+65000;await this.runAlarm();${nativeDrainRepairs}}finally{Date.now=clock;}
   return Response.json({owner:this.fixtureOwner,complete:(await this.store.exec("SELECT COUNT(*) n FROM collections WHERE operation='files' AND complete=1")).one().n});
  }return super.fetch(req);
 }`});
 try{
  let stub=await configured(mf);const started=await(await send(stub,'/fixture/archive-stall',{})).json();assert.equal(started.groups,4);
  let recovered;for(let i=0;i<30;i++){
   await pause(50);let r;
   try{r=await send(stub,'/fixture/recovery',{});}catch(error){
    if(!String(error).includes('repair_transport_stalled'))throw error;
    const ns=await mf.getDurableObjectNamespace('REPOSITORIES');stub=ns.get(ns.idFromName(repository.id));continue;
   }
   assert.equal(r.status,200);recovered=await r.json();
   if(recovered.owner!==started.owner)break;
  }
  assert.notEqual(recovered.owner,started.owner,'uncancellable outgoing work requires bounded automatic owner recovery');
  assert.equal(recovered.before.n,4);assert.ok(recovered.before.attempts>=1&&recovered.before.attempts<=4);
  assert.equal(recovered.before.leases,4-recovered.before.attempts,'siblings interrupted before their bound retain durable lease recovery');
  assert.equal(recovered.groups,0,'ordinary ticks cannot immediately reclaim active leases or durable retries');
  const result=await(await send(stub,'/fixture/finish-recovered',{})).json();assert.equal(result.owner,recovered.owner);assert.equal(result.complete,4);
 }finally{await mf.dispose();}
});

test('native stalled installation recovers before its caller and leaves repository reads and writes live',{skip:Boolean(process.env.TEST_POSTGRES_URL)},async()=>{
 const mf=await runtime(async()=>Response.json([]),{},{repositoryAlarms:false,
 installationExtension:`get repairHopLimit(){return 60;}
 async handleFetch(req){if(new URL(req.url).pathname==='/read')await new Promise(()=>{});return super.handleFetch(req);}`,
 repositoryExtension:`fixtureOwner=crypto.randomUUID();get repairHopLimit(){return 500;}
 async fetch(req){const path=new URL(req.url).pathname;
  if(path==='/fixture/installation-stall'){
   ${quiet}await this.store.exec('DELETE FROM jobs');
   await this.enqueueMany(Array.from({length:4},(_,number)=>({q:{repository:'org/repo',operation:'files',args:{number}},priority:5})),true);
   await this.runAlarm();return Response.json({owner:this.fixtureOwner});
  }
  if(path==='/fixture/stall-progress'){
   ${quiet}await this.runAlarm();await this.atomic(state=>state.put('pr','999',{number:999,state:'OPEN'}),'ingest');
   return Response.json({owner:this.fixtureOwner,groups:this.repairCoordinator?.groups.size || 0,rows:(await this.store.exec('SELECT SUM(attempt) attempts,COUNT(lease_id) leases FROM jobs')).one(),resource:Boolean((await this.store.exec("SELECT id FROM resources WHERE kind='pr' AND id='999'")).toArray().length)});
  }return super.fetch(req);
 }`});
 try{
  const stub=await configured(mf),started=await(await send(stub,'/fixture/installation-stall',{})).json();
  let result;for(let i=0;i<30;i++){
   await pause(40);const r=await send(stub,'/fixture/stall-progress',{});assert.equal(r.status,200);result=await r.json();
   assert.equal(result.owner,started.owner,'dependency recovery must not reset the repository');assert.equal(result.resource,true);
   if(result.groups===0)break;
  }
  assert.equal(result.groups,0);assert.equal(result.rows.attempts,4);assert.equal(result.rows.leases,0);
  // Continuing ordinary ticks cannot turn a persistent dependency fault into
  // repository reset/reclaim loops before the durable backoff expires.
  for(let i=0;i<3;i++){
   result=await(await send(stub,'/fixture/stall-progress',{})).json();assert.equal(result.owner,started.owner);assert.equal(result.rows.attempts,4);
  }
 }finally{await mf.dispose();}
});

test('a shared installation reset preserves both repository owners and ordinary runner recovery',{skip:Boolean(process.env.TEST_POSTGRES_URL)},async()=>{
 const repos=[repository,{...repository,id:'101',name:'org/peer'}];let slowRunner=true,runnerEntered;
 const runnerStarted=new Promise(resolve=>runnerEntered=resolve);
 const mf=await runtime(async(_req,url)=>{
  if(url.pathname.startsWith('/orgs/')){if(slowRunner){runnerEntered();await pause(1000);}return Response.json(repos.map(repo=>({name:repo.name.split('/')[1],archived:false})));}
  return Response.json({workflow_runs:[],total_count:0});
 },{ALLOWED_REPOSITORIES:JSON.stringify(repos)},{repositoryAlarms:false,
 installationExtension:`get repairHopLimit(){return 60;}
 async handleFetch(req){if(new URL(req.url).pathname==='/read')await new Promise(()=>{});return super.handleFetch(req);}`,
 repositoryExtension:`fixtureOwner=crypto.randomUUID();get repairHopLimit(){return 500;}
 async fetch(req){const path=new URL(req.url).pathname;
  if(path==='/fixture/shared-stall'){
   ${quiet}await this.store.exec('DELETE FROM jobs');const repo=await this.meta('repository');
   await this.enqueueMany(Array.from({length:2},(_,number)=>({q:{repository:repo.name,operation:'files',args:{number}},priority:5})),true);
   await this.runAlarm();return Response.json({owner:this.fixtureOwner});
  }
  if(path==='/fixture/shared-live'){
   ${quiet}await this.runAlarm();await this.atomic(state=>state.put('pr','999',{number:999,state:'OPEN'}),'ingest');
   return Response.json({owner:this.fixtureOwner,groups:this.repairCoordinator?.groups.size || 0,rows:(await this.store.exec('SELECT SUM(attempt) attempts,COUNT(lease_id) leases FROM jobs')).one(),resource:Boolean((await this.store.exec("SELECT id FROM resources WHERE kind='pr' AND id='999'")).toArray().length)});
  }return super.fetch(req);
 }`});
 try{
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stubs=repos.map(repo=>ns.get(ns.idFromName(repo.id)));
  for(let i=0;i<repos.length;i++)assert.equal((await send(stubs[i],'/configure',{repository:repos[i].name})).status,200);
  const installationNS=await mf.getDurableObjectNamespace('INSTALLATIONS');let installation=installationNS.get(installationNS.idFromName(repository.installation));
  const firstScan=send(installation,'/runner-capacity',repository).then(r=>r.json(),()=>({interrupted:true}));
  await runnerStarted;
  const started=await Promise.all(stubs.map(async stub=>(await send(stub,'/fixture/shared-stall',{})).json()));
  let states=[];const readWriteMs=[];
  for(let i=0;i<30;i++){
   await pause(40);const start=performance.now();
   states=await Promise.all(stubs.map(async(stub,index)=>{
    const response=await send(stub,'/fixture/shared-live',{});assert.equal(response.status,200);const value=await response.json();
    assert.equal(value.owner,started[index].owner);assert.equal(value.resource,true);return value;
   }));readWriteMs.push(performance.now()-start);
   if(states.every(state=>state.groups===0))break;
  }
  assert.ok(states.every(state=>state.groups===0&&state.rows.attempts===2&&state.rows.leases===0),'all callers must reach durable retry without resetting either repository');
  slowRunner=false;assert.equal((await firstScan).interrupted,true,'the first scan must actually cross the installation reset');
  installation=installationNS.get(installationNS.idFromName(repository.installation));
  const scan=await(await send(installation,'/runner-capacity',repository)).json();assert.equal(scan.complete,true);assert.equal(scan.requests,5);assert.equal(scan.cached,false,'recovery must perform a fresh scan');
  for(let i=0;i<3;i++)for(let index=0;index<stubs.length;index++){
   const value=await(await send(stubs[index],'/fixture/shared-live',{})).json();assert.equal(value.owner,started[index].owner);assert.equal(value.rows.attempts,2);
  }
  console.log(JSON.stringify({fixture:'shared_installation_reset',repositories:2,repair_retries:states.map(state=>state.rows.attempts),runner_recovered:scan.complete,independent_read_write_ms:readWriteMs}));
 }finally{slowRunner=false;await mf.dispose();}
});
