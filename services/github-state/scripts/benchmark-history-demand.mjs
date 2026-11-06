// Real isolated PostgreSQL, no GitHub requests. Compare the public handler and
// ordinary repair loop on identical incomplete histories and writer pressure.
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {writeFileSync,readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {resolve,relative} from 'node:path';
import {build} from 'esbuild';
import {postgresFixture,RepositoryState,PostgresStore} from '../tests/postgres_runtime.mjs';
const baseline=process.env.BASELINE_REF || '7051ff8';
const baselineCommit=execFileSync('git',['rev-parse',baseline],{encoding:'utf8'}).trim();
const bundle=await build({stdin:{contents:"export {RepositoryState} from './repository';export {PostgresStore} from './repository_store';",loader:'ts',resolveDir:process.cwd()+'/src'},bundle:true,platform:'node',format:'esm',write:false,plugins:[{
 name:'baseline-source',setup(b){b.onLoad({filter:/\.ts$/},args=>{
  if(!args.path.startsWith(resolve('src')+'/'))return;
  const path='services/github-state/src/'+relative(resolve('src'),args.path);
  return {contents:execFileSync('git',['show',baselineCommit+':'+path],{encoding:'utf8'}),loader:'ts'};
 });}},
 {name:'context',setup(b){b.onResolve({filter:/^cloudflare:workers$/},()=>({path:'context',namespace:'fixture'}));b.onLoad({filter:/.*/,namespace:'fixture'},()=>({contents:'export class DurableObject {constructor(ctx,env){this.ctx=ctx;this.env=env}}',loader:'js'}));}}
]});
const {RepositoryState:Baseline,PostgresStore:BaselineStore}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
const numbers=[23580,23597,23622,23657],head='a'.repeat(40),trials=Number(process.env.BENCHMARK_TRIALS || 3);
const query=(operation,number)=>({repository:'org/repo',operation,args:{number}});
const percentile=(a,p)=>[...a].sort((x,y)=>x-y)[Math.ceil(a.length*p)-1];
async function measure(Class,StoreClass){
 const f=await postgresFixture({RepositoryClass:Class,StoreClass});let release;
 try{
  for(const number of numbers){
   await f.state.put('pr',String(number),{number,state:'OPEN',headRefOid:head,body:'fixture',labels:[]});
   for(const operation of ['pr','issue_comments'])await f.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)',operation+':'+JSON.stringify({number}),operation,JSON.stringify({number}),1,Date.now(),0,null);
  }
  const old=Date.now()-4*3600000;
  await f.store.exec('INSERT INTO demand(key,at) VALUES '+numbers.map(()=>'(?,?)').join(','),...numbers.flatMap(n=>['review_comments:'+JSON.stringify({number:n}),old]));
  await f.state.enqueueMany(numbers.map(number=>({q:query('review_comments',number),priority:5})),true);
  const raw=f.state.store;let transactions=0,upstream=0;const waits=[],timings=[],purposes={};
  const transaction=raw.transaction.bind(raw);
  raw.transaction=async(apply,purpose='other')=>{
    const queued=performance.now();transactions++;purposes[purpose]=(purposes[purpose] || 0)+1;
    return transaction(tx=>{waits.push(performance.now()-queued);return apply(tx);},purpose);
  };
  f.state.startStorageMaintenance=()=>{}; // Exclude unrelated provider diagnostics.
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>{upstream++;return Response.json({api:'core',data:[]});}})};
  let entered;const ready=new Promise(r=>entered=r),gate=new Promise(r=>release=r);
  const holding=transaction(async()=>{entered();await gate;});await ready;
  const started=performance.now(),timer=setTimeout(()=>release(),500);
  const queries=[...numbers.map(n=>query('review_comments',n)),query('progress',numbers[0])];
  try{
   await Promise.all(Array.from({length:6},()=>queries).flat().map(async q=>{
    const began=performance.now();const response=await f.state.fetch(new Request('https://internal/query',{method:'POST',body:JSON.stringify(q)}));
    const result=await response.json();timings.push(performance.now()-began);
    assert.equal(response.status,200);assert.equal(result.status,'unavailable');assert.equal(result.complete,false);
   }));
  }finally{clearTimeout(timer);release();await holding;}
  const repliesComplete=performance.now()-started;
  if(f.state.readDemands)await f.state.readDemands.flight;
  const demandTransactions=transactions,demandsDurable=performance.now()-started;
  // Use the same quota-aware leased scheduler and publication code as normal.
  await f.state.alarm();
  const coverage=[];
  for(const number of numbers){const q=query('review_comments',number);assert.equal(await f.state.complete(q),true);coverage.push((await f.state.rows(q)).length);}
  const allHydrated=performance.now()-started;
  assert.deepEqual(coverage,[0,0,0,0]);assert.equal(upstream,4,'one fixture upstream read per incomplete history');
  const oldest=(await raw.exec('SELECT MIN(requested_at) oldest FROM jobs WHERE key LIKE ? AND lane=?','review_comments:%','requested')).one().oldest;
  assert.equal(oldest,null,'the fixed cohort leaves the requested backlog');
  return {reads:timings.length,p50_ms:percentile(timings,.5),p95_ms:percentile(timings,.95),replies_complete_ms:repliesComplete,demand_durable_ms:demandsDurable,demand_transactions:demandTransactions,total_transactions:transactions,transaction_admission_p95_ms:percentile(waits,.95),all_histories_complete_ms:allHydrated,histories_completed:4,oldest_requested_after:null,fixture_upstream_reads:upstream,github_requests:0,purposes};
 }finally{release?.();await f.close();}
}
async function measureCompanions(Class,StoreClass){
 const f=await postgresFixture({RepositoryClass:Class,StoreClass});try{
  const queries=Array.from({length:8},(_,i)=>query('issue_comments',100+i));
  await f.state.enqueueMany(queries.map(q=>({q,priority:5})),true);
  const raw=f.state.store;let transactions=0,calls=0;
  const purposes={};
  const transaction=raw.transaction.bind(raw);
  raw.transaction=(apply,purpose='other')=>{transactions++;purposes[purpose]=(purposes[purpose] || 0)+1;return transaction(apply,purpose);};
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async(url,init)=>{
   calls++;assert.equal(url,'https://internal/batch');const input=JSON.parse(init.body);assert.ok(input.jobs.length>=1 && input.jobs.length<=8);
   return Response.json({results:input.jobs.map(()=>({api:'graphql',data:[]}))});
  }})};
  f.state.maintenance=async()=>{};f.state.startStorageMaintenance=()=>{};
  const start=performance.now();await f.state.runAlarm();const duration=performance.now()-start;
  for(const q of queries)assert.equal(await f.state.complete(q),true);
  if(Class===RepositoryState)assert.equal(calls,1);
  return {histories_completed:8,upstream_batch_requests:calls,github_requests:0,total_transactions:transactions,duration_ms:duration,purposes};
 }finally{await f.close();}
}
const result={baseline:baselineCommit,implementation_sha256:createHash('sha256').update(readFileSync('src/repository.ts')).update(readFileSync('src/repository_store.ts')).digest('hex'),scope:'full baseline source snapshot with identical installed dependencies; isolated real PostgreSQL; fixed four-PR cohort; 30 authenticated repository-handler reads per trial; deliberate 500ms writer hold; fixture upstream only; excludes unrelated storage diagnostics; not a production fleet throughput measurement',trials,before:[],after:[],companions:{before:[],after:[]}};
for(let trial=0;trial<trials;trial++)for(const label of trial%2?['after','before']:['before','after']){const Class=label==='before'?Baseline:RepositoryState,StoreClass=label==='before'?BaselineStore:PostgresStore;result[label].push(await measure(Class,StoreClass));result.companions[label].push(await measureCompanions(Class,StoreClass));}
result.summary={};
for(const label of ['before','after'])result.summary[label]=Object.fromEntries(['p50_ms','p95_ms','replies_complete_ms','demand_durable_ms','demand_transactions','total_transactions','all_histories_complete_ms'].map(k=>[k,percentile(result[label].map(t=>t[k]),.5)]));
const output=process.env.BENCHMARK_OUTPUT || '/tmp/history-demand-benchmark.json';writeFileSync(output,JSON.stringify(result,null,2));console.log(JSON.stringify(result,null,2));
