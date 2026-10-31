// Isolated real PostgreSQL: leased scheduler, complete history publication and
// concurrent ordinary ingestion; fixture upstream makes zero GitHub requests.
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {writeFileSync,readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {resolve,relative} from 'node:path';
import {build} from 'esbuild';
import {postgresFixture,RepositoryState,PostgresStore} from '../tests/postgres_runtime.mjs';
import {drainRepairs} from '../tests/repair_background.mjs';
const baseline=process.env.BASELINE_REF || '2732eb5';
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
const source=readFileSync('src/repository.ts','utf8'),configured=Number(/PUBLICATION_ITEMS=(\d+),/.exec(source)?.[1]);
const limits=(process.env.BENCHMARK_LIMITS || '2,4,8').split(',').map(Number);
assert.ok(limits.includes(configured)&&limits.every(n=>[2,4,8].includes(n))&&new Set(limits).size===limits.length,'limits must include the configured cap and use2/4/8');
const candidates=new Map([[configured,{RepositoryState,PostgresStore}]]);
for(const items of limits.filter(n=>n!==configured)){
 assert.equal(source.split('PUBLICATION_ITEMS='+configured+',').length,2);
 const variant=await build({stdin:{contents:"export {RepositoryState} from './repository';export {PostgresStore} from './repository_store';",loader:'ts',resolveDir:resolve('src')},bundle:true,platform:'node',format:'esm',write:false,plugins:[
  {name:'publication-limit',setup(b){b.onLoad({filter:/\/repository\.ts$/},()=>({contents:source.replace('PUBLICATION_ITEMS='+configured+',','PUBLICATION_ITEMS='+items+','),loader:'ts'}));}},
  {name:'context',setup(b){b.onResolve({filter:/^cloudflare:workers$/},()=>({path:'context',namespace:'fixture'}));b.onLoad({filter:/.*/,namespace:'fixture'},()=>({contents:'export class DurableObject {constructor(ctx,env){this.ctx=ctx;this.env=env}}',loader:'js'}));}}
 ]});
 candidates.set(items,await import('data:text/javascript;base64,'+Buffer.from(variant.outputFiles[0].text).toString('base64')));
}
const repo='org/repo',trials=Number(process.env.BENCHMARK_TRIALS || 3),mixedGroups=Number(process.env.BENCHMARK_GROUPS || 1),fullTrials=Number(process.env.BENCHMARK_FULL_TRIALS || trials);
const mixedCount=Number(process.env.BENCHMARK_HISTORY_COUNT || 8),ingressCount=Number(process.env.BENCHMARK_INGRESS_COUNT || 8);
assert.ok(Number.isInteger(trials)&&trials>0&&[1,4].includes(mixedGroups)&&Number.isInteger(fullTrials)&&fullTrials>0);
assert.ok(Number.isInteger(mixedCount)&&mixedCount>0&&mixedCount<=100&&Number.isInteger(ingressCount)&&ingressCount>=0);
const q=number=>({repository:repo,operation:'issue_comments',args:{number}});
async function measure(Class,StoreClass,count,ingressCount,intervalMs=0,groups=1){
 const f=await postgresFixture({RepositoryClass:Class,StoreClass});let release;
 try{
  const now=Date.now();await f.state.put('pr','9000',{number:9000,state:'CLOSED',body:'warm complete parent'});
  const warm=q(9000),warmKey='issue_comments:{"number":9000}';
  await f.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)',warmKey,warm.operation,JSON.stringify(warm.args),1,now,0,null);
  await f.state.enqueue(warm,20,now+3600000);
  const cohort=Array.from({length:count*groups},(_,i)=>q(100+i));await f.state.enqueueMany(cohort.map(q=>({q,priority:5})),true);
  if(groups>1)for(let group=0;group<groups;group++){
   const keys=cohort.slice(group*count,(group+1)*count).map(query=>'issue_comments:'+JSON.stringify(query.args));
   await f.store.exec('UPDATE jobs SET lane=? WHERE key IN ('+keys.map(()=>'?').join(',')+')',['requested','active','verification','archive'][group],...keys);
  }
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
   upstream++;assert.equal(url,'https://internal/batch');if(upstream===groups)entered();if(ingressCount)await gate;
   const jobs=JSON.parse(init.body).jobs;assert.equal(jobs.length,count);
   return Response.json({results:jobs.map(job=>({api:'graphql',data:count===100?[]:[{id:String(job.query.args.number),body:'complete fixture history',created_at:'2026-10-31T00:00:00Z'}]}))});
  }})};
  const start=performance.now(),alarm=f.state.runAlarm();await beganUpstream;
  const ingests=Array.from({length:ingressCount},(_,i)=>(async()=>{if(intervalMs&&i)await new Promise(resolve=>setTimeout(resolve,i*intervalMs));const t=performance.now();await f.state.ingest({repository:repo,key:'fixture/ingress-'+i,delivery:'ingress-'+i,event:'issue_comment',received:Date.now()});reads.push(performance.now()-t);})());
  release();await Promise.all([alarm,...ingests]);await drainRepairs(f.state);const completeMs=performance.now()-start;
  assert.equal(upstream,groups);
  for(const query of cohort)assert.equal(await f.state.complete(query),true);
  assert.equal((await raw.exec('SELECT COUNT(*) n FROM deliveries')).one().n,ingressCount);
  assert.equal((await raw.exec('SELECT COUNT(*) n FROM jobs WHERE lease_id IS NOT NULL')).one().n,0);
  const publications=writers.filter(x=>x.purpose==='publication');
  const p95=values=>[...values].sort((a,b)=>a-b)[Math.max(0,Math.ceil(values.length*.95)-1)]??null;
  return {histories_completed:cohort.length,committed_ingress:ingressCount,groups,ingress_interval_ms:intervalMs,elapsed_ms:completeMs,transactions,application_sql_round_trips:roundTrips,upstream_batches:upstream,github_requests:0,repair_scheduler:f.state.repairCoordinator?.snapshot() || null,lease_renewal_row_updates:f.state.renewedLeases || 0,grouped_publication:f.state.groupedPublication?{...f.state.groupedPublication}:null,ingress_ms:reads,ingress_p95_ms:p95(reads),ingress_admission_p95_ms:p95(writers.filter(x=>x.purpose==='ingest').map(x=>x.queue_wait_ms)),publication_transactions:publications.length,publication_max_hold_ms:Math.max(...publications.map(x=>x.transaction_ms)),writers};
 }finally{release?.();await f.close();}
}
const result={baseline:baselineCommit,implementation_sha256:createHash('sha256').update(['repository.ts','repository_store.ts','monitoring.ts','repair_coordinator.ts','repair_io.ts','installation.ts'].map(name=>readFileSync('src/'+name)).join('')).digest('hex'),scope:'isolated real PostgreSQL; ordinary runAlarm leased scheduler and tracked background completion; configured histories per group and issue-comment deliveries to a warm complete collection, burst and steady500ms spacing; requested limits interleaved with benchmark-only cap substitution; separate100memberemptyfullbatch alternating trials; fixture upstream only, zero GitHub requests; not a production event-mix or fleet throughput claim',trials,mixed_groups:mixedGroups,histories_per_group:mixedCount,ingress_deliveries:ingressCount,full_trials:fullTrials,configured_items:configured,candidate_limits:limits,before:[],after:[],steady:{before:[],after:[]},limits:Object.fromEntries(limits.filter(n=>n!==configured).map(n=>[n,{burst:[],steady:[]}])),full_batch:{},full_batch_trials:{before:[],after:[]}};
for(let trial=0;trial<trials;trial++)for(const label of trial%2?[...limits].reverse().concat(['before']):['before',...limits]){
 const classes=label==='before'?{RepositoryState:Baseline,PostgresStore:BaselineStore}:candidates.get(label);
 for(const [mode,interval] of [['burst',0],['steady',500]]){
  const row=await measure(classes.RepositoryState,classes.PostgresStore,mixedCount,ingressCount,interval,mixedGroups);
  (label==='before'?(mode==='burst'?result.before:result.steady.before):label===configured?(mode==='burst'?result.after:result.steady.after):result.limits[label][mode]).push(row);
 }
}
if(process.env.BENCHMARK_FULL!=='0')for(let trial=0;trial<fullTrials;trial++)for(const label of trial%2?['after','before']:['before','after']){
 const row=await measure(label==='before'?Baseline:RepositoryState,label==='before'?BaselineStore:PostgresStore,100,0);
 result.full_batch_trials[label].push(row);result.full_batch[label]??=row;
}
const median=a=>[...a].sort((x,y)=>x-y)[Math.floor(a.length/2)];result.summary={};
const summarize=xs=>Object.fromEntries(['elapsed_ms','transactions','application_sql_round_trips','publication_transactions','publication_max_hold_ms','ingress_p95_ms','ingress_admission_p95_ms'].map(k=>[k,median(xs.map(x=>x[k]))]));
for(const set of [result,result.steady]){
 set.summary={};for(const label of ['before','after'])set.summary[label]=summarize(set[label]);
}
for(const v of Object.values(result.limits))v.summary={burst:summarize(v.burst),steady:summarize(v.steady)};
result.full_batch_summary=Object.fromEntries(Object.entries(result.full_batch_trials).filter(([,v])=>v.length).map(([k,v])=>[k,summarize(v)]));
writeFileSync(process.env.BENCHMARK_OUTPUT || '/tmp/publication-benchmark.json',JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify({burst:result.summary,steady:result.steady.summary,limits:Object.fromEntries(Object.entries(result.limits).map(([k,v])=>[k,v.summary])),full_batch:result.full_batch_summary},null,2));
