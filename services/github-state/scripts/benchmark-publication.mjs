// Isolated real PostgreSQL: leased scheduler, complete history publication and
// concurrent ordinary ingestion; fixture upstream makes zero GitHub requests.
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {writeFileSync,readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {resolve,relative} from 'node:path';
import {build} from 'esbuild';
import {postgresFixture,RepositoryState,PostgresStore} from '../tests/postgres_runtime.mjs';
const baseline=process.env.BASELINE_REF || '38ec297';
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
const limits=(process.env.BENCHMARK_LIMITS || '2,4,8').split(',').map(Number);
assert.ok(limits.includes(2)&&limits.every(n=>[2,4,8].includes(n))&&new Set(limits).size===limits.length,'limits must include2 and may include4/8');
const candidates=new Map([[2,{RepositoryState,PostgresStore}]]);
for(const items of limits.filter(n=>n!==2)){
 const source=readFileSync('src/repository.ts','utf8');assert.equal(source.split('PUBLICATION_ITEMS=2,').length,2);
 const variant=await build({stdin:{contents:"export {RepositoryState} from './repository';export {PostgresStore} from './repository_store';",loader:'ts',resolveDir:resolve('src')},bundle:true,platform:'node',format:'esm',write:false,plugins:[
  {name:'publication-limit',setup(b){b.onLoad({filter:/\/repository\.ts$/},()=>({contents:source.replace('PUBLICATION_ITEMS=2,','PUBLICATION_ITEMS='+items+','),loader:'ts'}));}},
  {name:'context',setup(b){b.onResolve({filter:/^cloudflare:workers$/},()=>({path:'context',namespace:'fixture'}));b.onLoad({filter:/.*/,namespace:'fixture'},()=>({contents:'export class DurableObject {constructor(ctx,env){this.ctx=ctx;this.env=env}}',loader:'js'}));}}
 ]});
 candidates.set(items,await import('data:text/javascript;base64,'+Buffer.from(variant.outputFiles[0].text).toString('base64')));
}
const repo='org/repo',trials=Number(process.env.BENCHMARK_TRIALS || 3);
const q=number=>({repository:repo,operation:'issue_comments',args:{number}});
async function measure(Class,StoreClass,count,ingressCount,intervalMs=0){
 const f=await postgresFixture({RepositoryClass:Class,StoreClass});let release;
 try{
  const now=Date.now();await f.state.put('pr','9000',{number:9000,state:'CLOSED',body:'warm complete parent'});
  const warm=q(9000),warmKey='issue_comments:{"number":9000}';
  await f.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)',warmKey,warm.operation,JSON.stringify(warm.args),1,now,0,null);
  await f.state.enqueue(warm,20,now+3600000);
  const cohort=Array.from({length:count},(_,i)=>q(100+i));await f.state.enqueueMany(cohort.map(q=>({q,priority:5})),true);
  for(let i=0;i<ingressCount;i++)f.objects.set('fixture/ingress-'+i,JSON.stringify({action:'created',issue:{number:9000},comment:{id:String(90000+i),body:'concurrent webhook',created_at:'2026-10-31T00:00:00Z'}}));
  const raw=f.store,transact=raw.transact.bind(raw);let transactions=0,roundTrips=0,upstream=0;const writers=[],reads=[];
  raw.transact=async(apply,wait,purpose='other')=>{
   transactions++;const start=performance.now();let calls=0,failed=true;
   try{return await transact(async tx=>{
    const run=tx.run.bind(tx);tx.run=(...args)=>{calls++;roundTrips++;return run(...args);};
    const value=await apply(tx);failed=false;return value;
   },wait,purpose);}finally{writers.push({purpose,queue_wait_ms:wait,transaction_ms:performance.now()-start,application_sql_round_trips:calls,failed});}
  };
  f.state.maintenance=async()=>{};f.state.startStorageMaintenance=()=>{};
  let entered;const beganUpstream=new Promise(r=>entered=r),gate=new Promise(r=>release=r);
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async(url,init)=>{
   upstream++;assert.equal(url,'https://internal/batch');entered();if(ingressCount)await gate;
   const jobs=JSON.parse(init.body).jobs;assert.equal(jobs.length,count);
   return Response.json({results:jobs.map(job=>({api:'graphql',data:count===100?[]:[{id:String(job.query.args.number),body:'complete fixture history',created_at:'2026-10-31T00:00:00Z'}]}))});
  }})};
  const start=performance.now(),alarm=f.state.runAlarm();await beganUpstream;
  const ingests=Array.from({length:ingressCount},(_,i)=>(async()=>{if(intervalMs&&i)await new Promise(resolve=>setTimeout(resolve,i*intervalMs));const t=performance.now();await f.state.ingest({repository:repo,key:'fixture/ingress-'+i,delivery:'ingress-'+i,event:'issue_comment',received:Date.now()});reads.push(performance.now()-t);})());
  release();await Promise.all([alarm,...ingests]);const completeMs=performance.now()-start;
  assert.equal(upstream,1);
  for(const query of cohort)assert.equal(await f.state.complete(query),true);
  assert.equal((await raw.exec('SELECT COUNT(*) n FROM deliveries')).one().n,ingressCount);
  assert.equal((await raw.exec('SELECT COUNT(*) n FROM jobs WHERE lease_id IS NOT NULL')).one().n,0);
  const publications=writers.filter(x=>x.purpose==='publication');
  const p95=values=>[...values].sort((a,b)=>a-b)[Math.max(0,Math.ceil(values.length*.95)-1)]??null;
  return {histories_completed:count,committed_ingress:ingressCount,ingress_interval_ms:intervalMs,elapsed_ms:completeMs,transactions,application_sql_round_trips:roundTrips,upstream_batches:upstream,github_requests:0,ingress_ms:reads,ingress_p95_ms:p95(reads),ingress_admission_p95_ms:p95(writers.filter(x=>x.purpose==='ingest').map(x=>x.queue_wait_ms)),publication_transactions:publications.length,publication_max_hold_ms:Math.max(...publications.map(x=>x.transaction_ms)),writers};
 }finally{release?.();await f.close();}
}
const result={baseline:baselineCommit,implementation_sha256:createHash('sha256').update(readFileSync('src/repository.ts')).update(readFileSync('src/repository_store.ts')).update(readFileSync('src/monitoring.ts')).digest('hex'),scope:'isolated real PostgreSQL; ordinary runAlarm leased scheduler; eight histories plus eight issue-comment deliveries to a warm complete collection, burst and steady500ms spacing, three alternating trials each; requested limits interleaved (benchmark-only constant substitution for4/8); separate100memberemptyfullbatch once/version; fixture upstream only, zero GitHub requests; not a production fleet throughput claim',trials,candidate_limits:limits,before:[],after:[],steady:{before:[],after:[]},limits:Object.fromEntries(limits.filter(n=>n!==2).map(n=>[n,{burst:[],steady:[]}])),full_batch:{}};
for(let trial=0;trial<trials;trial++)for(const label of trial%2?[...limits].reverse().concat(['before']):['before',...limits]){
 const classes=label==='before'?{RepositoryState:Baseline,PostgresStore:BaselineStore}:candidates.get(label);
 for(const [mode,interval] of [['burst',0],['steady',500]]){
  const row=await measure(classes.RepositoryState,classes.PostgresStore,8,8,interval);
  (label==='before'?(mode==='burst'?result.before:result.steady.before):label===2?(mode==='burst'?result.after:result.steady.after):result.limits[label][mode]).push(row);
 }
}
if(process.env.BENCHMARK_FULL!=='0')for(const label of ['before','after'])result.full_batch[label]=await measure(label==='before'?Baseline:RepositoryState,label==='before'?BaselineStore:PostgresStore,100,0);
const median=a=>[...a].sort((x,y)=>x-y)[Math.floor(a.length/2)];result.summary={};
const summarize=xs=>Object.fromEntries(['elapsed_ms','transactions','application_sql_round_trips','publication_transactions','publication_max_hold_ms','ingress_p95_ms','ingress_admission_p95_ms'].map(k=>[k,median(xs.map(x=>x[k]))]));
for(const set of [result,result.steady]){
 set.summary={};for(const label of ['before','after'])set.summary[label]=summarize(set[label]);
}
for(const v of Object.values(result.limits))v.summary={burst:summarize(v.burst),steady:summarize(v.steady)};
writeFileSync(process.env.BENCHMARK_OUTPUT || '/tmp/publication-benchmark.json',JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify({burst:result.summary,steady:result.steady.summary,limits:Object.fromEntries(Object.entries(result.limits).map(([k,v])=>[k,v.summary])),full_batch:Object.fromEntries(Object.entries(result.full_batch).map(([k,v])=>[k,{...v,writers:undefined}]))},null,2));
