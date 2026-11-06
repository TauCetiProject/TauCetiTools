// Two repositories plus the actual runner-capacity owner; fixture upstream only.
import assert from 'node:assert/strict';
import {writeFileSync} from 'node:fs';
import {fixture as sqliteFixture,RepositoryState as SQLiteRepository} from '../tests/migration_fixture.mjs';
import {postgresFixture,RepositoryState,PostgresStore} from '../tests/postgres_runtime.mjs';
import {installationFixture,InstallationScheduler} from '../tests/repair_installation_fixture.mjs';
import {drainRepairs} from '../tests/repair_background.mjs';
import {baseline,implementationFingerprint,fingerprintFiles} from './benchmark-baseline.mjs';
const previous=await baseline(),pg=Boolean(process.env.TEST_POSTGRES_URL),trials=Number(process.env.BENCHMARK_TRIALS || 3);
const windowMs=Number(process.env.BENCHMARK_WINDOW_MS || (pg?90000:4000)),historyCount=Number(process.env.BENCHMARK_SHARED_HISTORIES || 16);
const runnerRepos=Number(process.env.BENCHMARK_RUNNER_REPOSITORIES || 2),runnerRuns=process.env.BENCHMARK_RUNNER_RUNS==='1',runnerDelay=Number(process.env.BENCHMARK_RUNNER_DELAY_MS || 0);
assert.ok(Number.isInteger(runnerRepos)&&runnerRepos>0&&runnerRepos<=26&&Number.isInteger(runnerDelay)&&runnerDelay>=0);
assert.ok(Number.isInteger(trials)&&trials>0&&Number.isInteger(windowMs)&&windowMs>2000&&Number.isInteger(historyCount)&&historyCount>0);
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
const response=body=>Response.json(body,{headers:{'x-ratelimit-resource':'core','x-ratelimit-remaining':'4999','x-ratelimit-limit':'5000','x-ratelimit-reset':String(Math.floor(Date.now()/1000)+3600)}});
const github=url=>new URL(url instanceof Request?url.url:String(url)).hostname==='api.github.com';
async function measure(label){
 const classes=label==='before'?previous:{RepositoryState:pg?RepositoryState:SQLiteRepository,PostgresStore,InstallationScheduler};
 const installation=installationFixture(classes.InstallationScheduler),owners=[],held=[],original=globalThis.fetch,alarms=new Map(),flights=[],completion=[new Map(),new Map()],writers=[[],[]],alarmCounts=[0,0],requests=[0,0];
 const ready=deferred(),all=deferred();let releaseTimer,windowTimer,cron,stopped=false,started,peak=0,runnerResult=null,runnerAt=null;
 try{
  globalThis.fetch=async(url,init)=>{
   if(!github(url))return original(url,init);
   if(!started){const gate=deferred();held.push(gate);if(held.length===4)ready.resolve();await gate.promise;return response({});}
   peak=Math.max(peak,installation.db.prepare('SELECT COUNT(*) n FROM reservations').get().n);const path=new URL(url).pathname;
   const scanResponse=async body=>{if(runnerDelay)await new Promise(resolve=>setTimeout(resolve,runnerDelay));return response(body);};
   if(path.startsWith('/orgs/'))return scanResponse(Array.from({length:runnerRepos},(_,i)=>({name:'repo'+i,archived:false})));
   if(path.endsWith('/jobs'))return scanResponse({jobs:[],total_count:0});
   if(path.includes('/actions/runs')){const runs=runnerRuns&&new URL(url).searchParams.get('status')==='in_progress'?[{id:String(Number(/repo(\d+)/.exec(path)[1])+1)}]:[];return scanResponse({workflow_runs:runs,total_count:runs.length});}
   assert.ok(path.includes('/pulls/')&&path.endsWith('/files'));requests[path.includes('/repo0/')?0:1]++;
   await new Promise(resolve=>setTimeout(resolve,100));return response([]);
  };
  for(let i=0;i<4;i++)installation.requests.push(installation.scheduler.http('/held/'+i,'fixture-token','GET',undefined,'fixture','core'));
  await ready.promise;
  const states=new Map();installation.notify=async(id,input)=>{
   const state=states.get(id);if(!state)return;
   await state.fetch(new Request('https://internal/capacity-ready',{method:'POST',body:JSON.stringify(input)}));
  };
  for(let index=0;index<2;index++){
   const repo=installation.repos[index],f=pg?await postgresFixture({RepositoryClass:classes.RepositoryState,StoreClass:classes.PostgresStore}):await sqliteFixture(undefined,undefined,{},classes.RepositoryState);
   owners.push(f);states.set(repo.id,f.state);await f.state.setMeta('repository',repo);f.env.ALLOWED_REPOSITORIES=JSON.stringify(installation.repos);
   f.state.startStorageMaintenance=()=>{};
   f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:(url,init)=>installation.scheduler.fetch(new Request(url,init))})};
   await f.state.enqueueMany(Array.from({length:historyCount},(_,number)=>({q:{repository:repo.name,operation:'files',args:{number:number+100}},priority:5})),true);
   let active;const finish=f.state.finishJob,store=f.state.store,transaction=store.transaction.bind(store);
   f.state.finishJob=async function(job,...args){const value=await finish.call(this,job,...args);active?.add(job.key);return value;};
   store.transaction=async(apply,purpose='other')=>{
    const queued=performance.now(),keys=new Set();let entered,failed=true;
    try{
     const result=await transaction(async tx=>{entered=performance.now();active=keys;try{return await apply(tx);}finally{active=undefined;}},purpose);
     failed=false;for(const key of keys)if(!completion[index].has(key))completion[index].set(key,performance.now()-started);
     if(completion.every(c=>c.size===historyCount)&&runnerResult)all.resolve();return result;
    }finally{writers[index].push({purpose,wait_ms:entered===undefined?null:entered-queued,hold_ms:entered===undefined?null:performance.now()-entered,failed});}
   };
   if(!pg){
    const set=f.ctx.storage.setAlarm.bind(f.ctx.storage);await set(null);
    f.ctx.storage.setAlarm=async at=>{await set(at);clearTimeout(alarms.get(f));if(stopped)return;
     alarms.set(f,setTimeout(()=>{alarms.delete(f);alarmCounts[index]++;const work=(async()=>{await set(null);await f.state.alarm();})();work.catch(()=>{});flights.push(work);},Math.min(2147483647,Math.max(1,at-Date.now()))));
    };
   }
  }
  started=performance.now();peak=4;
  const launch=()=>{for(const f of owners){const work=f.state.alarm();work.catch(()=>{});flights.push(work);}};
  releaseTimer=setTimeout(()=>{for(const gate of held)gate.resolve();},2000);cron=setInterval(launch,60000);launch();
  const runner=installation.scheduler.runnerCapacity(installation.repos[0]).then(async r=>{runnerResult=await r.json();runnerAt=performance.now()-started;if(completion.every(c=>c.size===historyCount))all.resolve();});runner.catch(()=>{});flights.push(runner);
  await Promise.race([all.promise,new Promise(resolve=>{windowTimer=setTimeout(resolve,windowMs);})]);
  const elapsed=performance.now()-started;stopped=true;clearInterval(cron);for(const timer of alarms.values())clearTimeout(timer);
  const counts=completion.map(c=>c.size),times=completion.map(c=>[...c.values()]);
  const result={elapsed_ms:elapsed,completions:counts,completion_ms:times,runner_complete:Boolean(runnerResult?.complete),runner_ms:runnerAt,runner_requests:runnerResult?.requests ?? null,upstream_repairs:requests,peak_reservations:peak,notifications:installation.repos.slice(0,2).map(r=>installation.received.filter(id=>id===r.id).length),alarm_counts:[...alarmCounts],transaction_admissions:writers.map(v=>v.length),writers:writers.map(v=>[...v]),repair_scheduler:owners.map(f=>f.state.repairCoordinator?.snapshot() || null),capacity_wakes:installation.scheduler.capacityWakeStats || null};
  assert.ok(peak<=4);result.acceptance={histories_complete:counts.every(n=>n===historyCount),runner_complete:Boolean(runnerResult?.complete)};
  return result;
 }finally{
  stopped=true;clearTimeout(releaseTimer);clearTimeout(windowTimer);clearInterval(cron);for(const timer of alarms.values())clearTimeout(timer);for(const gate of held)gate.resolve();
  await Promise.allSettled(flights);await installation.close();for(const f of owners){await drainRepairs(f.state);if(f.close)await f.close();else f.db.close();}globalThis.fetch=original;
 }
}
const result={baseline:previous.commit,implementation_sha256:implementationFingerprint(),implementation_files:fingerprintFiles(),backend:pg?'real isolated PostgreSQL':'node:sqlite fixture',scope:'two configured-history repositories and actual configurable runner-capacity inventory sharing four reservations; fixed2s capacity release,100ms repair reads, ordinary60s cron and SQLite durable alarm emulation; fixed observation horizon retains incomplete baseline work; zero GitHub requests',trials,histories_per_repository:historyCount,runner_repositories:runnerRepos,runner_runs:runnerRuns,runner_delay_ms:runnerDelay,window_ms:windowMs,before:[],after:[]};
for(let trial=0;trial<trials;trial++)for(const label of trial%2?['after','before']:['before','after']){
 const row=await measure(label);result[label].push(row);console.log(JSON.stringify({trial,label,completions:row.completions,elapsed_ms:row.elapsed_ms,admissions:row.transaction_admissions,runner_ms:row.runner_ms}));
 writeFileSync(process.env.BENCHMARK_OUTPUT || '/tmp/shared-installation-benchmark.json',JSON.stringify(result,null,2)+'\n');
 if(label==='after'){assert.equal(row.acceptance.histories_complete,true);assert.equal(row.acceptance.runner_complete,true,'runner-capacity progress regressed under sustained reservations');}
}
assert.equal(result.implementation_sha256,implementationFingerprint(),'source changed during trials');
