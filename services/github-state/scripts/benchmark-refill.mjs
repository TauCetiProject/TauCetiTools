// Same real PostgreSQL cohort, one65s transport and ordinary60s cron.
// A fixed observation horizon retains incomplete baseline work in the results.
import assert from 'node:assert/strict';
import {writeFileSync} from 'node:fs';
import {postgresFixture,RepositoryState,PostgresStore} from '../tests/postgres_runtime.mjs';
import {drainRepairs} from '../tests/repair_background.mjs';
import {baseline,implementationFingerprint,fingerprintFiles} from './benchmark-baseline.mjs';
const before=await baseline(),trials=Number(process.env.BENCHMARK_TRIALS || 3),holdMs=Number(process.env.BENCHMARK_HOLD_MS || 65000);
assert.ok(Number.isInteger(trials)&&trials>0);assert.ok(Number.isInteger(holdMs)&&holdMs>60000,'exercise a real wait beyond lease expiry');
const queries=Array.from({length:21},(_,i)=>({repository:'org/repo',operation:'files',args:{number:100+i}}));
const keys=new Set(queries.map(q=>'files:'+JSON.stringify(q.args)));
async function measure(RepositoryClass,StoreClass){
 const f=await postgresFixture({RepositoryClass,StoreClass}),calls=new Map(),completed=new Map(),writers=[],cronWork=[],maintenance={expiry:0,coverage:0,maintenance:0};
 let release,heldTimer,cron,active,started,heldAt,initial;const gate=new Promise(resolve=>release=resolve);
 try{
  await f.state.enqueueMany(queries.map(q=>({q,priority:5})),true);
  // Count real automatic maintenance, without adding scans or writer work.
  for(const [method,key] of [['expire','expiry'],['requestOpenCoverage','coverage'],['maintenance','maintenance']]){
   const run=f.state[method];f.state[method]=function(...args){maintenance[key]++;return run.apply(this,args);};
  }
  f.state.startStorageMaintenance=()=>{}; // Independent provider/health diagnostics.
  const finish=f.state.finishJob;
  f.state.finishJob=async function(job,...args){const value=await finish.call(this,job,...args);if(keys.has(job.key))active?.add(job.key);return value;};
  const transact=f.store.transact.bind(f.store);
  f.store.transact=async(apply,wait,purpose='other')=>{
   const began=performance.now(),settled=new Set();let failed=true,sql=0;
   try{
    const result=await transact(async store=>{
     active=settled;const run=store.run.bind(store);store.run=(...args)=>{sql++;return run(...args);};
     try{return await apply(store);}finally{active=undefined;}
    },wait,purpose);
    failed=false;for(const key of settled)if(!completed.has(key))completed.set(key,performance.now()-started);return result;
   }finally{writers.push({purpose,wait_ms:wait,hold_ms:performance.now()-began,sql,failed});}
  };
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async(_url,init)=>{
   const q=JSON.parse(init.body).query,n=q.args.number;calls.set(n,(calls.get(n)||0)+1);
   if(n===100){if(!heldTimer)heldTimer=setTimeout(()=>{heldAt=performance.now()-started;release();},holdMs);await gate;}
   return Response.json({api:'core',data:[]});
  }})};
  started=performance.now();initial=f.state.alarm();initial.catch(()=>{});
  cron=setInterval(()=>{const work=f.state.alarm();work.catch(()=>{});cronWork.push(work);},60000);
  // Both revisions observe the same held hop and automatic cron cadence.
  await Promise.race([gate,initial.then(()=>new Promise(()=>{}),error=>Promise.reject(error))]);await initial;await drainRepairs(f.state);clearInterval(cron);
  await Promise.all(cronWork);const elapsed=performance.now()-started;
  const complete=(await f.store.exec("SELECT key FROM collections WHERE operation='files' AND complete=1")).toArray();
  assert.equal(complete.length,completed.size);assert.equal(calls.get(100),1,'locally owned expired work is not fetched twice');
  assert.ok([...calls.values()].every(n=>n===1));
  const missing=(await f.store.exec("SELECT COUNT(*) n FROM jobs j LEFT JOIN collections c ON c.key=j.key AND c.complete=1 WHERE c.key IS NULL")).one().n;
  assert.equal(missing,21-complete.length,'incomplete cohort remains durably recoverable');
  return {elapsed_ms:elapsed,held_ms:heldAt,cohort:21,complete:complete.length,missing,fast_complete_before_held:[...completed].filter(([key,at])=>key!=='files:{"number":100}'&&at<heldAt).length,completed_ms:Object.fromEntries(completed),upstream_calls:Object.fromEntries(calls),maintenance,repair_scheduler:f.state.repairCoordinator?.snapshot() || null,lease_renewal_row_updates:f.state.renewedLeases || 0,writer_admissions:writers.length,application_sql_round_trips:writers.reduce((n,r)=>n+r.sql,0),writers};
 }finally{clearInterval(cron);clearTimeout(heldTimer);release();await Promise.allSettled([initial,...cronWork].filter(Boolean));await drainRepairs(f.state);await f.close();}
}
const result={baseline:before.commit,implementation_sha256:implementationFingerprint(),implementation_files:fingerprintFiles(),scope:'isolated real PostgreSQL;21independent missing file histories, one65s transport, real60s cron; fixed held-hop horizon includes incomplete baseline work; no GitHub requests; no fleet throughput claim',trials,before:[],after:[]};
for(let trial=0;trial<trials;trial++)for(const label of trial%2?['after','before']:['before','after']){
 const row=await measure(label==='before'?before.RepositoryState:RepositoryState,label==='before'?before.PostgresStore:PostgresStore);
 if(label==='after')assert.equal(row.complete,21,'refill reaches every short group beyond the16-launch boundary');
 result[label].push(row);console.log(JSON.stringify({trial,label,complete:row.complete,fast_before_held:row.fast_complete_before_held,elapsed_ms:row.elapsed_ms}));
 // Preserve each finished trial, including a later benchmark failure.
 writeFileSync(process.env.BENCHMARK_OUTPUT || '/tmp/refill-benchmark.json',JSON.stringify(result,null,2)+'\n');
}
assert.equal(result.implementation_sha256,implementationFingerprint(),'source changed during the trials');
