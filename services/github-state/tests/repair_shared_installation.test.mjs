import {test} from 'node:test';
import assert from 'node:assert/strict';
import {installationFixture} from './repair_installation_fixture.mjs';
import {fixture as sqliteFixture} from './migration_fixture.mjs';
import {postgresFixture} from './postgres_runtime.mjs';
import {drainRepairs} from './repair_background.mjs';
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};

test('two repair owners and a runner-capacity scan progress through shared real installation reservations',async()=>{
 const installation=installationFixture(),owners=[],original=globalThis.fetch,held=[],alarmTimers=new Map(),alarmWork=[],alarmCounts=[0,0];let timer,closing=false;
 try{
  const ready=deferred();
  globalThis.fetch=async(url,init)=>{if(!github(url))return original(url,init);const gate=deferred();held.push(gate);if(held.length===4)ready.resolve();await gate.promise;return response({});};
  for(let i=0;i<4;i++)installation.requests.push(installation.scheduler.http('/held/'+i,'fixture-token','GET',undefined,'fixture','core'));
  await ready.promise;
  const all=deferred(),complete=[0,0],started=Date.now(),completionMs=[[],[]],repos=installation.repos.slice(0,2);
  const map=new Map();let peak=4,repairRequests=0;
  globalThis.fetch=async(url,init)=>{
   if(!github(url))return original(url,init);
   peak=Math.max(peak,installation.db.prepare('SELECT COUNT(*) n FROM reservations').get().n);
   const path=new URL(url).pathname;
   if(path.startsWith('/orgs/'))return response(repos.map(r=>({name:r.name.split('/')[1],archived:false})));
   if(path.includes('/actions/runs'))return response({workflow_runs:[],total_count:0});
   assert.ok(path.includes('/pulls/')&&path.endsWith('/files'),path);repairRequests++;
   await pause(100);return response([]);
  };
  installation.notify=async(id,input)=>{
   const state=map.get(id);assert.ok(state,'only configured waiting owners receive wakes');
   const r=await state.fetch(new Request('https://internal/capacity-ready',{method:'POST',body:JSON.stringify(input)}));assert.equal(r.status,200);
  };
  for(const [index,repo] of repos.entries()){
   const f=process.env.TEST_POSTGRES_URL?await postgresFixture():await sqliteFixture();owners.push(f);map.set(repo.id,f.state);
   await f.state.setMeta('repository',repo);f.env.ALLOWED_REPOSITORIES=JSON.stringify(installation.repos);
   f.state.expire=async()=>({});f.state.requestOpenCoverage=async()=>{};f.state.maintenance=async()=>{};f.state.startStorageMaintenance=()=>{};
   if(!process.env.TEST_POSTGRES_URL){
    // The SQL fixture normally records alarms without delivering them. This
    // end-to-end case includes the ordinary durable-alarm continuation.
    const set=f.ctx.storage.setAlarm.bind(f.ctx.storage);
    f.ctx.storage.setAlarm=async at=>{
     await set(at);clearTimeout(alarmTimers.get(f));if(closing)return;
     alarmTimers.set(f,setTimeout(()=>{
      alarmTimers.delete(f);
      alarmCounts[index]++;
      const work=(async()=>{await set(null);await f.state.alarm();})();work.catch(()=>{});alarmWork.push(work);
     },Math.min(2147483647,Math.max(1,at-Date.now()))));
    };
   }
   f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:(url,init)=>installation.scheduler.fetch(new Request(url,init))})};
   const settle=f.state.settleRepairJobs;
   f.state.settleRepairJobs=function(jobs,effect){settle.call(this,jobs,effect);if(effect==='progress'){
    complete[index]+=jobs.length;completionMs[index].push(Date.now()-started);if(complete.every(n=>n===16))all.resolve();
   }};
   await f.state.enqueueMany(Array.from({length:16},(_,number)=>({q:{repository:repo.name,operation:'files',args:{number:number+100}},priority:5})),true);
  }
  await Promise.all(owners.map(f=>f.state.runAlarm()));
  assert.ok(installation.scheduler.capacityWaiters.size>=1,'actual capacity rejection registers repository wakes');
  const runner=installation.scheduler.runnerCapacity(repos[0]);
  for(const gate of held){gate.resolve();await pause(20);}
  const deadline=new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('shared installation stranded '+JSON.stringify({complete,completionMs,received:installation.received}))),process.env.TEST_POSTGRES_URL?90000:10000);});
  const [,capacity]=await Promise.race([Promise.all([all.promise,runner.then(r=>r.json())]),deadline]);
  assert.equal(capacity.complete,true);assert.equal(capacity.requests,5);assert.equal(repairRequests,32);assert.equal(peak,4);
  assert.deepEqual(complete,[16,16]);
  for(const [index,f] of owners.entries()){
   await drainRepairs(f.state);const store=f.store || f.state.store;
   assert.equal((await store.exec('SELECT COUNT(*) n FROM jobs WHERE lease_id IS NOT NULL')).one().n,0);
   assert.equal((await store.exec("SELECT COUNT(*) n FROM collections WHERE operation='files' AND complete=1")).one().n,16);
   const launches=f.state.repairCoordinator.snapshot().launches,wakes=installation.received.filter(id=>id===repos[index].id).length;
   // Each independent completion permits refill; each initial/notification/
   // durable-alarm wake can race at most four requests before learning a new
   // shared-capacity rejection. Compare measured amplification with the prior
   // revision in the performance benchmark, rather than a timing-sensitive cap.
   assert.ok(launches<=complete[index]+4*(1+wakes+alarmCounts[index]),'claims exceed completion/release/alarm continuation bounds '+JSON.stringify({launches,wakes,alarms:alarmCounts[index]}));
  }
  console.log(JSON.stringify({fixture:'shared_installation',backend:process.env.TEST_POSTGRES_URL?'postgres':'sqlite',elapsed_ms:Date.now()-started,completions:complete,completion_ms:completionMs,launches:owners.map(f=>f.state.repairCoordinator.snapshot().launches),notifications:repos.map(repo=>installation.received.filter(id=>id===repo.id).length),alarm_counts:alarmCounts,runner_requests:capacity.requests}));
  assert.equal(installation.db.prepare('SELECT COUNT(*) n FROM reservations').get().n,0);
  assert.ok(installation.received.length<=41,'one release cannot broadcast to multiple recipients:32repairs+5runner requests+4held reservations');
 }finally{
  closing=true;clearTimeout(timer);for(const value of alarmTimers.values())clearTimeout(value);for(const gate of held)gate.resolve();
  await Promise.allSettled(alarmWork);
  await installation.close();for(const f of owners){await drainRepairs(f.state);if(f.close)await f.close();else f.db.close();}
  globalThis.fetch=original;
 }
});
function response(body){return Response.json(body,{headers:{'x-ratelimit-resource':'core','x-ratelimit-remaining':'4999','x-ratelimit-limit':'5000','x-ratelimit-reset':String(Math.floor(Date.now()/1000)+3600)}});}
function github(url){return new URL(url instanceof Request?url.url:String(url)).hostname==='api.github.com';}
