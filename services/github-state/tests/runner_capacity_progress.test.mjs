import test from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
const code=await build({entryPoints:['src/runner-capacity.ts'],bundle:true,format:'esm',platform:'node',write:false});
const {scanHostedCapacity}=await import('data:text/javascript;base64,'+Buffer.from(code.outputFiles[0].text).toString('base64'));
const base=Date.parse('2026-11-03T00:00:00Z');
const iso=i=>new Date(base+i*1000).toISOString();
function fixture(count=400,{sameSecond=false,repositories=1,runningCount=1,activeCount=1,perSecond=1,fractional=false}={}) {
 const rows=Array.from({length:count},(_,i)=>({id:i+100,run_attempt:1,head_sha:'a'.repeat(40),
  check_suite_node_id:'CS_'+(i+100),updated_at:iso(i),created_at:iso(sameSecond?0:Math.floor(i/perSecond)),
  ...(fractional?{actualCreated:base+Math.floor(i/perSecond)*1000+(i%perSecond)*120}:{}),
  path:'.github/workflows/build.yml'})).reverse();
 const saved=new Map(),reads=new Map(),extraNamespace=new Set(),extraJobs=new Map(),completed=new Set(),promoted=new Set(),generation=new Map(),historical=new Map();
 const ranges=new Map();
 const cache={get:(key,revision)=>saved.get(key)?.revision===revision?structuredClone(saved.get(key).jobs):undefined,
  put:(key,revision,jobs)=>saved.set(key,{revision,jobs:structuredClone(jobs)}),
  getRanges:key=>structuredClone(ranges.get(key)),putRanges:(key,boundaries)=>boundaries.length?ranges.set(key,structuredClone(boundaries)):ranges.delete(key)};
 const envelope=(key,values)=>({status:200,body:key?{[key]:values}:values});
 function jobRows(id) {
  let jobs;
  if(id<100)jobs=id>activeCount?[]:id===1?[
   {id:1,status:'in_progress',name:'sandboxed-build',labels:['ubuntu-24.04'],runner_id:1,runner_name:'hosted'},
   {id:2,status:'in_progress',name:'sandboxed-build',labels:['nscloud-ubuntu-test'],runner_id:2,runner_name:'namespace'},
  ]:[{id:id*10,status:'in_progress',name:'sandboxed-build',labels:['ubuntu-24.04'],runner_id:id,runner_name:'hosted'}];
  else jobs=[{id:id*10+(generation.get(id)||0)*100000,status:promoted.has(id)?'in_progress':'queued',name:'sandboxed-build',
   labels:id<103 || extraNamespace.has(id)?['nscloud-ubuntu-test']:['ubuntu-24.04'],
   ...(promoted.has(id)?{runner_id:id,runner_name:'assigned'}:{})},...(extraJobs.get(id)||[])];
  if(completed.has(id))jobs=jobs.map(job=>({...job,status:'completed'}));
  return jobs;
 }
 const read=async(path,body)=>{
  if(path==='/graphql') {
   const data={};
   for(const [key,suite] of Object.entries(body.variables)) {
    const i=Number(key.slice(2)),id=Number(suite.slice(3));
    const limit=Number(new RegExp('q'+i+':node.*?checkRuns\\(first:(\\d+)').exec(body.query)?.[1]);
    const all=[...jobRows(id),...(historical.get(id)||[])],jobs=all.slice(0,limit);
    data['q'+i]={checkRuns:{totalCount:all.length,pageInfo:{hasNextPage:all.length>limit},
     nodes:jobs.map(job=>({databaseId:job.id,name:job.name,status:job.status.toUpperCase()}))}};
   }
   return {status:200,body:{data}};
  }
  if(path.startsWith('/orgs/'))return envelope(null,Array.from({length:repositories},(_,i)=>({name:i?'other'+i:'repo',archived:false})));
  if(path.includes('/other'))return envelope('workflow_runs',[]);
  if(path.includes('status=in_progress'))return envelope('workflow_runs',Array.from({length:runningCount},(_,i)=>({id:i+1,check_suite_node_id:'CS_'+(i+1)})));
  if(path.includes('status=queued')) {
   const range=new URL(path,'https://api.github.com').searchParams.get('created');
   const selected=rows.filter(run=>{
    if(!range)return true;
    const at=run.actualCreated ?? Date.parse(run.created_at);
    if(range.startsWith('<='))return at<=Date.parse(range.slice(2));
    if(range.startsWith('>='))return at>=Date.parse(range.slice(2));
    const [lower,upper]=range.split('..').map(Date.parse);return at>=lower && at<=upper;
   });
   const r=envelope('workflow_runs',selected.slice(0,100));
   if(selected.length>100)r.link='<https://api.github.com/next>; rel="next"';
   return r;
  }
  const id=Number(/\/runs\/(\d+)\/jobs/.exec(path)?.[1]);reads.set(id,(reads.get(id)||0)+1);
  return envelope('jobs',jobRows(id));
 };
 return {read,cache,rows,saved,ranges,reads,extraNamespace,extraJobs,completed,promoted,generation,historical};
}
test('a 400-run mostly hosted queue converges under the unchanged 80-request bound',async()=>{
 const f=fixture();let value,total=0,passes=0;
 do {
  value=await scanHostedCapacity('org',f.read,20,f.cache);total+=value.requests;passes++;
  assert.ok(value.requests<=80);
  if(value.routing_version!==2){assert.equal(value.placement_unavailable,'capacity_scan_limit');assert.ok(value.job_cache_writes>0);}
 }while(value.routing_version!==2 && passes<12);
 assert.equal(value.routing_version,2);assert.equal(value.namespace_busy,4);assert.equal(value.busy,1);
 assert.equal(f.saved.size,401);assert.ok(passes>1);assert.ok(total<700);
 f.reads.clear();const warm=await scanHostedCapacity('org',f.read,20,f.cache);
 assert.equal(warm.routing_version,2);assert.equal(warm.namespace_busy,4);assert.equal(warm.job_cache_hits,401);
 assert.equal(warm.job_cache_writes,0);assert.ok(warm.requests<25);
 assert.equal(f.reads.size,0,'fresh GraphQL statuses reuse immutable labels and assigned runner metadata');
 console.log(JSON.stringify({queue:400,cold_passes:passes,cold_requests:total,warm_requests:warm.requests}));
});
test('new job IDs and matrix growth invalidate metadata reuse without any run timestamp change',async()=>{
 const f=fixture(4);await scanHostedCapacity('org',f.read,20,f.cache);f.reads.clear();
 const revision=f.rows.find(r=>r.id===103).updated_at;
 f.extraJobs.set(103,[{id:1031,status:'queued',name:'extra-build',labels:['nscloud-ubuntu-test']}]);
 let v=await scanHostedCapacity('org',f.read,20,f.cache);
 assert.equal(v.namespace_busy,5);assert.equal(f.reads.get(103),1);
 f.reads.clear();f.generation.set(103,1);f.extraNamespace.add(103);
 v=await scanHostedCapacity('org',f.read,20,f.cache);assert.equal(v.namespace_busy,6);assert.equal(f.reads.get(103),1);
 assert.equal(f.rows.find(r=>r.id===103).updated_at,revision);
});
test('persisted search boundaries freshly cover new head/tail arrivals and departed queued jobs',async()=>{
 const f=fixture();let value;
 for(let i=0;i<15;i++){value=await scanHostedCapacity('org',f.read,20,f.cache);if(value.routing_version===2)break;}
 assert.equal(value.routing_version,2);assert.ok(f.ranges.get('/repos/org/repo').length>0);
 f.rows.splice(f.rows.findIndex(run=>run.id===100),1);
 for(const [id,when] of [[1000,10000],[1001,-3600]]){
  f.rows.push({...f.rows[0],id,check_suite_node_id:'CS_'+id,created_at:iso(when),updated_at:iso(when)});
  f.extraNamespace.add(id);
 }
 f.rows.sort((a,b)=>Date.parse(b.created_at)-Date.parse(a.created_at));
 value=await scanHostedCapacity('org',f.read,20,f.cache);
 assert.equal(value.routing_version,2);assert.equal(value.namespace_busy,5);
 assert.ok(f.ranges.get('/repos/org/repo').length<=16);
});
test('invalid saved search boundaries cannot replace fresh queue discovery',async()=>{
 const f=fixture(4);f.ranges.set('/repos/org/repo',['invalid',null,-1,Infinity,NaN,8640000000000,1e12]);
 const read=async(path,body)=>{
  assert.ok(!/\+\d{6}-/.test(decodeURIComponent(path)),'expanded-year hints must never reach the provider');
  return f.read(path,body);
 };
 const value=await scanHostedCapacity('org',read,20,f.cache);
 assert.equal(value.routing_version,2);assert.equal(value.namespace_busy,4);
});
test('several queued repositories discard obsolete partitions as their queues drain',async()=>{
 const fixtures=Array.from({length:3},()=>fixture(160)),cache=fixtures[0].cache;let queuedReads=0,value;
 const read=async(path,body)=>{
  if(path.startsWith('/orgs/'))return {status:200,body:fixtures.map((_,i)=>({name:'r'+i,archived:false}))};
  if(path==='/graphql')return fixtures[0].read(path,body);
  if(path.includes('status=queued'))queuedReads++;
  const i=Number(/\/r(\d+)\//.exec(path)[1]);
  return fixtures[i].read(path.replace('/r'+i+'/','/repo/'),body);
 };
 for(let i=0;i<20;i++){value=await scanHostedCapacity('org',read,20,cache);if(value.routing_version===2)break;}
 assert.equal(value.routing_version,2);assert.equal(value.namespace_busy,12);assert.equal(fixtures[0].ranges.size,3);
 for(const f of fixtures)f.rows.splice(0);
 value=await scanHostedCapacity('org',read,20,cache);
 assert.equal(value.routing_version,2);assert.equal(value.namespace_busy,3);assert.equal(fixtures[0].ranges.size,0);
 queuedReads=0;value=await scanHostedCapacity('org',read,20,cache);
 assert.equal(value.routing_version,2);assert.equal(queuedReads,3,'drained repositories need one fresh listing each');
});
test('fresh execution/completion statuses reuse metadata only after runner assignment is known',async()=>{
 const f=fixture(4);await scanHostedCapacity('org',f.read,20,f.cache);f.reads.clear();
 f.promoted.add(100);let v=await scanHostedCapacity('org',f.read,20,f.cache);
 assert.equal(f.reads.get(100),1);assert.equal(v.namespace_busy,4);
 f.reads.clear();await scanHostedCapacity('org',f.read,20,f.cache);assert.equal(f.reads.size,0);
 f.completed.add(100);v=await scanHostedCapacity('org',f.read,20,f.cache);
 assert.equal(v.namespace_busy,3);assert.equal(f.reads.size,0);
});
test('missing, incomplete or quota-blocked live witnesses retain full REST discovery',async()=>{
 for(const mode of ['missing','truncated','quota']) {
  const f=fixture(1);
  const reader=async(path,body)=>{
   if(path==='/graphql' && mode==='quota')throw new Error('capacity_graphql_reserve');
   const v=await f.read(path,body);
   if(path==='/graphql') {
    if(mode==='missing')v.body={};
    else for(const node of Object.values(v.body.data))node.checkRuns.pageInfo.hasNextPage=true;
   }
   return v;
  };
  await scanHostedCapacity('org',reader,20,f.cache);await scanHostedCapacity('org',reader,20,f.cache);
  assert.equal(f.reads.get(100),2,mode);assert.equal(f.reads.get(1),2,mode);
 }
});
test('overlapping ranges retain hidden fractional-second boundary jobs',async()=>{
 const f=fixture(400,{perSecond:7,fractional:true});
 for(let id=395;id<=399;id++)f.extraNamespace.add(id);
 let v;
 for(let i=0;i<15;i++){v=await scanHostedCapacity('org',f.read,20,f.cache);if(v.routing_version===2)break;}
 assert.equal(v.routing_version,2);assert.equal(v.namespace_busy,9);
});
test('an overfull single second stays unknown instead of falsely certifying paginated coverage',async()=>{
 const f=fixture(101,{sameSecond:true});let v;
 for(let i=0;i<3;i++)v=await scanHostedCapacity('org',f.read,20,f.cache);
 assert.equal(v.routing_version,1);assert.equal(v.placement_unavailable,'capacity_queue_pagination');assert.ok(v.requests<=80);
});
test('only a complete job-page traversal can populate durable queued observations',async()=>{
 const f=fixture(1);
 const reader=async(path,body)=>{
  const v=await f.read(path,body);
  if(path.includes('/runs/100/jobs'))return path.endsWith('page=1')?{...v,link:'<https://api.github.com/next>; rel="next"'}:{status:503,body:{}};
  return v;
 };
 const v=await scanHostedCapacity('org',reader,20,f.cache);
 assert.equal(v.routing_version,1);assert.equal(f.saved.has('/repos/org/repo/100'),false);
});

// Real SQLite/KV ownership survives scheduler replacement, as in a DO eviction.
const {DatabaseSync}=await import('node:sqlite');
const ownerBundle=await build({entryPoints:['src/installation.ts'],bundle:true,format:'esm',platform:'node',write:false,plugins:[{
 name:'owner-context',setup(b){b.onResolve({filter:/^cloudflare:workers$/},()=>({path:'context',namespace:'owner-context'}));b.onLoad({filter:/.*/,namespace:'owner-context'},()=>({contents:'export class DurableObject{constructor(ctx,env){this.ctx=ctx;this.env=env}}',loader:'js'}));}
}]});
const {InstallationScheduler}=await import('data:text/javascript;base64,'+Buffer.from(ownerBundle.outputFiles[0].text).toString('base64'));
test('productive passes survive owner replacement, coalesce callers, respect cooldown and retain hourly quota',async()=>{
 const f=fixture(400,{repositories:14,runningCount:45,activeCount:30}),db=new DatabaseSync(':memory:'),values=new Map();let calls=0;
 const storage={get:async key=>values.get(key),put:async(key,value)=>values.set(key,structuredClone(value)),sql:{exec(q,...args){
  let rows=[];
  if(!args.length && q.includes(';'))db.exec(q);
  else {const statement=db.prepare(q);if(statement.columns().length)rows=statement.all(...args);else statement.run(...args);}
  return {toArray:()=>rows,one:()=>{assert.equal(rows.length,1);return rows[0];}};
 }}};
 const make=()=>{const owner=new InstallationScheduler({storage},{});owner.token=async()=> 'token';owner.usable=()=>5000;
  owner.http=async(path,token,method,body)=>{calls++;return f.read(path,body);};return owner;};
 const original=Date.now;let clock=original();const started=clock;Date.now=()=>clock;
 try {
  let last,passes=0;
  do {
   const owner=make();const get=async()=> (await owner.runnerCapacity({name:'org/repo',installation:'200'})).json();
   const [first,second]=await Promise.all([get(),get()]);last=first;passes++;
   assert.equal(first.requests,second.requests);assert.equal(first.job_cache_writes,second.job_cache_writes);
   const count=calls;assert.equal((await get()).cached,true);assert.equal(calls,count);
   if(first.routing_version!==2){assert.ok(first.retry_at>=clock+30000 && first.retry_at<=clock+31000);clock+=31000;}
  }while(last.routing_version!==2 && passes<20);
  assert.equal(last.routing_version,2);assert.equal(last.namespace_busy,4);
  assert.equal(db.prepare('SELECT COUNT(*) n FROM runner_capacity_jobs').get().n,430);
  assert.equal(values.get('runner-capacity-attempts').length,calls);
  const coldCalls=calls;
  while(clock<started+3600000) {
   clock+=31000;
   const current=await(await make().runnerCapacity({name:'org/repo',installation:'200'})).json();
   assert.equal(current.complete,true);assert.equal(current.routing_version,2);assert.equal(current.namespace_busy,4);
   assert.ok(current.requests<=80);assert.ok(calls<2400);
  }
  console.log(JSON.stringify({repos:14,running_runs:45,queued_runs:400,cold_passes:passes,cold_requests:coldCalls,hour_requests:calls}));
  let count=calls;assert.equal((await(await make().runnerCapacity({name:'org/repo',installation:'200'})).json()).cached,true);assert.equal(calls,count);
  const key=db.prepare('SELECT key FROM runner_capacity_jobs LIMIT 1').get().key;
  db.prepare('UPDATE runner_capacity_jobs SET jobs=? WHERE key=?').run('invalid json',key);clock+=121000;
  const recovered=await(await make().runnerCapacity({name:'org/repo',installation:'200'})).json();
  assert.equal(recovered.routing_version,2);assert.equal(recovered.namespace_busy,4);assert.equal(recovered.job_cache_writes,1);
  assert.doesNotThrow(()=>JSON.parse(db.prepare('SELECT jobs FROM runner_capacity_jobs WHERE key=?').get(key).jobs));count=calls;
  values.set('runner-capacity-attempts',Array(2400).fill(clock));clock+=121000;
  const blocked=await(await make().runnerCapacity({name:'org/repo',installation:'200'})).json();
  assert.equal(blocked.complete,false);assert.equal(blocked.reason,'capacity_hourly_budget');assert.equal(calls,count);
 }finally{Date.now=original;db.close();}
});

test('warm production-sized discovery completes with measured provider-like latency and at most four reads',async()=>{
 const f=fixture(700,{repositories:14,runningCount:45,activeCount:30});let initial;
 for(let i=0;i<20;i++){initial=await scanHostedCapacity('org',f.read,20,f.cache);if(initial.routing_version===2)break;}
 assert.equal(initial.routing_version,2);
 // Live scans also refresh newly assigned runners and reruns; a completely
 // static, fully cached queue understates the production latency.
 for(let id=104;id<112;id++)f.promoted.add(id);
 for(let id=112;id<120;id++)f.generation.set(id,1);
 let active=0,peak=0;
 const read=async(path,body)=>{
  active++;peak=Math.max(peak,active);
  try {
   const delay=path==='/graphql'?350+Object.keys(body.variables).length*18:path.startsWith('/orgs/')?550:path.includes('/repo/actions/runs?')?1750:350;
   await new Promise(resolve=>setTimeout(resolve,delay));return await f.read(path,body);
  }finally{active--;}
 };
 const v=await scanHostedCapacity('org',read,20,f.cache);
 assert.equal(v.routing_version,2);assert.equal(v.namespace_busy,4);assert.ok(v.requests<=80);
 assert.ok(v.elapsed_ms<20000);assert.ok(peak<=4);assert.equal(active,0);
 console.log(JSON.stringify({provider_latency_test_ms:v.elapsed_ms,requests:v.requests,peak_reads:peak}));
});

test('initial recovery can progress behind historical scan spending without changing the hourly ceiling',async()=>{
 const f=fixture(400,{repositories:14,runningCount:45,activeCount:30}),db=new DatabaseSync(':memory:'),values=new Map();let calls=0;
 const original=Date.now;let clock=original();const started=clock;Date.now=()=>clock;
 values.set('runner-capacity-attempts',Array.from({length:1840},(_,i)=>started-3600000+i*3600000/1840));
 const storage={get:async key=>values.get(key),put:async(key,value)=>values.set(key,structuredClone(value)),sql:{exec(q,...args){
  let rows=[];if(!args.length && q.includes(';'))db.exec(q);else {const s=db.prepare(q);if(s.columns().length)rows=s.all(...args);else s.run(...args);}
  return {toArray:()=>rows,one:()=>{assert.equal(rows.length,1);return rows[0];}};
 }}};
 try {
  let result,sawQuick=false;
  for(let i=0;i<25;i++) {
   const owner=new InstallationScheduler({storage},{});owner.token=async()=> 'token';owner.usable=()=>5000;
   owner.http=async(path,token,method,body)=>{calls++;return f.read(path,body);};
   result=await(await owner.runnerCapacity({name:'org/repo',installation:'200'})).json();
   assert.ok(values.get('runner-capacity-attempts').length<=2400);
   if(result.routing_version===2)break;
   if(result.retry_at-clock<=31000)sawQuick=true;
   clock=result.retry_at+1;
  }
  assert.equal(result.routing_version,2);assert.equal(result.namespace_busy,4);assert.equal(sawQuick,true);
  const bootstrap=values.get('runner-capacity-label-bootstrap-v2:org');assert.equal(bootstrap.done,true);assert.ok(bootstrap.spent<=800);
  assert.ok(clock-started<15*60000);
  console.log(JSON.stringify({old_hour_requests:1840,new_requests:calls,bootstrap_ms:clock-started}));
 }finally{Date.now=original;db.close();}
});

test('one inaccessible check suite does not discard valid sibling witnesses',async()=>{
 const f=fixture(4);
 const read=async(path,body)=>{
  const v=await f.read(path,body);
  if(path==='/graphql')for(const [key,id] of Object.entries(body.variables))if(id==='CS_103'){
   v.body.data['q'+key.slice(2)]=null;v.body.errors=[{type:'NOT_FOUND',message:'one suite unavailable'}];
  }
  return v;
 };
 await scanHostedCapacity('org',read,20,f.cache);
 const v=await scanHostedCapacity('org',read,20,f.cache);
 assert.equal(v.routing_version,2);assert.equal(v.namespace_busy,4);
 assert.equal(f.reads.get(103),2);assert.equal(f.reads.get(100),1);assert.equal(f.reads.get(1),1);
});


test('a rerun learns its full check-suite size without repeatedly reading REST job details',async()=>{
 const f=fixture(1);f.historical.set(100,[{id:901,status:'completed',name:'old-build',labels:[]},{id:902,status:'completed',name:'old-pick',labels:[]}]);
 await scanHostedCapacity('org',f.read,20,f.cache);f.reads.clear();
 const value=await scanHostedCapacity('org',f.read,20,f.cache);
 assert.equal(value.routing_version,2);assert.equal(value.namespace_busy,2);assert.equal(f.reads.size,0);
});
