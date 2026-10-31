// Real isolated PostgreSQL, no GitHub requests. Compare the public handler and
// ordinary repair loop on identical incomplete histories and writer pressure.
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {writeFileSync,readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {resolve,relative} from 'node:path';
import {build} from 'esbuild';
import {postgresFixture,RepositoryState,PostgresStore} from '../tests/postgres_runtime.mjs';
const baseline=process.env.BASELINE_REF || '6dc9cd3';
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
const trials=Number(process.env.BENCHMARK_TRIALS || 3),repo='org/repo',head='a'.repeat(40);
const cases=[
 ['pull_request',{action:'synchronize',pull_request:{number:7,title:'new head',state:'open',updated_at:'2026-10-31T04:00:00Z',head:{sha:head},mergeable:true,labels:[],user:{login:'fixture'}}}],
 ['status',{sha:head,context:'build',state:'success',created_at:'2026-10-31T04:00:00Z'}],
 ['issue_comment',{action:'created',issue:{number:7},comment:{id:'101',body:'comment',created_at:'2026-10-31T04:00:00Z',updated_at:'2026-10-31T04:00:00Z'}}],
 ['pull_request_review_comment',{action:'edited',pull_request:{number:7},comment:{id:'102',body:'edit',created_at:'2026-10-31T04:00:00Z',updated_at:'2026-10-31T04:00:01Z'}}],
 ['pull_request_review',{action:'submitted',pull_request:{number:7}}],
 ['push',{ref:'refs/heads/main',after:head}]
];
async function measure(RepositoryClass,StoreClass){
 const f=await postgresFixture({RepositoryClass,StoreClass});try{
  f.env.PUBLIC_READ_REPOSITORIES=JSON.stringify([repo]);
  await f.state.put('pr','7',{number:7,state:'OPEN',headRefOid:'b'.repeat(40),body:'old'});
  const original=f.state.atomic;let roundTrips=0,transactions=0;
  f.state.atomic=function(apply,purpose){return original.call(this,async state=>{
   transactions++;let source=state.store;while(source.source)source=source.source;
   const run=source.run.bind(source);source.run=(...args)=>{roundTrips++;return run(...args);};
   return apply(state);
  },purpose);};
  const output=[];
  for(let i=0;i<cases.length;i++){
   const [event,body]=cases[i],key='fixture/event-'+i;f.objects.set(key,JSON.stringify(body));
   const first=roundTrips,tx=transactions,start=performance.now();
   await f.state.ingest({repository:repo,key,delivery:'delivery-'+i,event,received:Date.now()});
   output.push({event,elapsed_ms:performance.now()-start,sql_round_trips:roundTrips-first,transactions:transactions-tx});
  }
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM deliveries')).one().n,cases.length);
  assert.equal((await f.state.meta('latest_applied_input')).delivery,'delivery-5');
  const jobs=(await f.store.exec('SELECT key,priority,dirty,generation,lane,attempt,wait_reason FROM jobs ORDER BY key')).toArray();
  return {events:output,total_ms:output.reduce((n,x)=>n+x.elapsed_ms,0),sql_round_trips:roundTrips,transactions,committed_deliveries:cases.length,jobs,github_requests:0};
 }finally{await f.close();}
}
const result={baseline:baselineCommit,implementation_sha256:createHash('sha256').update(readFileSync('src/repository.ts')).update(readFileSync('src/repository_store.ts')).digest('hex'),trials,scope:'isolated real PostgreSQL; six synthetic webhook deliveries per trial; full baseline source and store; identical installed dependencies; actual application SQL round trips instrumented below buffering; preparation/read and admission/commit latency included; no GitHub requests; not production throughput',before:[],after:[]};
for(let trial=0;trial<trials;trial++)for(const label of trial%2?['after','before']:['before','after'])result[label].push(await measure(label==='before'?Baseline:RepositoryState,label==='before'?BaselineStore:PostgresStore));
for(let i=0;i<trials;i++)assert.deepEqual(result.before[i].jobs,result.after[i].jobs,'same durable scheduling state');
const median=a=>[...a].sort((x,y)=>x-y)[Math.floor(a.length/2)];result.summary={};
for(const label of ['before','after'])result.summary[label]={total_ms:median(result[label].map(x=>x.total_ms)),sql_round_trips:median(result[label].map(x=>x.sql_round_trips)),transactions:median(result[label].map(x=>x.transactions)),events:cases.map(([event],i)=>({event,elapsed_ms:median(result[label].map(x=>x.events[i].elapsed_ms)),sql_round_trips:median(result[label].map(x=>x.events[i].sql_round_trips))}))};
writeFileSync(process.env.BENCHMARK_OUTPUT || '/tmp/ingest-benchmark.json',JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify(result.summary,null,2));
