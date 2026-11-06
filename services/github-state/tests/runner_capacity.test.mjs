import test from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {runtime,response} from './runtime.mjs';
const code=await build({entryPoints:['src/runner-capacity.ts'],bundle:true,format:'esm',platform:'node',write:false});
const {scanHostedCapacity}=await import('data:text/javascript;base64,'+Buffer.from(code.outputFiles[0].text).toString('base64'));
const hosted={status:'in_progress',labels:['ubuntu-latest']};
const envelope=(key,rows)=>({status:200,body:key?{[key]:key==='jobs'?rows.map((job,i)=>({id:i+1,...job})):rows,total_count:rows.length}:rows});
function readFixture(options={}) {
 const calls=[];
 const read=async path=>{
  calls.push(path);
  if(path.startsWith('/orgs/'))return envelope(null,[{name:'code',archived:false},{name:'old',archived:true},{name:'other',archived:false}]);
  if(path.includes('/other/'))return envelope('workflow_runs',[]);
  if(path.includes('/actions/runs?'))return envelope('workflow_runs',[{id:'1'}]);
  if(options.failed || options.failedLater && path.endsWith('page=2'))return {status:403,body:{message:'rate limited'}};
  const jobs=[hosted,{...hosted,status:'queued'},{...hosted,labels:['nscloud-ubuntu-test']},{...hosted,labels:['namespace-profile-foo']},{...hosted,labels:['self-hosted']}];
  if(options.paginate && !path.endsWith('page=2'))return {...envelope('jobs',jobs),body:{jobs:jobs.map((job,i)=>({id:i+1,...job})),total_count:6},link:'<https://api.github.com/next>; rel="next"'};
  if(options.paginate)return envelope('jobs',[{...hosted,id:100}]);
  return envelope('jobs',jobs);
 };
 return {read,calls};
}
test('counts running hosted jobs across repositories, excluding archived repos and non-hosted labels',async()=>{
 const {read,calls}=readFixture();const s=await scanHostedCapacity('org',read);
 assert.equal(s.busy,1);assert.equal(s.requests,6);assert.equal(calls.length,6);
 assert.ok(calls.some(p=>p.includes('status=in_progress&')));
 assert.ok(!calls.some(p=>p.includes('/old/')));
});
test('running pool reporting reuses assigned jobs and excludes queued destinations',async()=>{
 const {read,calls}=readFixture();
 const machine='fleet-'+ 'a'.repeat(32);
 const value=await scanHostedCapacity('org',async path=>{
  if(!path.includes('/jobs?'))return read(path);
  calls.push(path);
  return envelope('jobs',[
   {status:'in_progress',labels:['ubuntu-latest'],runner_id:1,runner_name:'hosted'},
   {status:'in_progress',labels:['nscloud-ubuntu-test'],runner_id:2,runner_name:'nsc-runner'},
   {status:'in_progress',labels:['self-hosted',machine],runner_id:3,runner_name:'fleet-runner'},
   {status:'queued',labels:['self-hosted',machine],runner_id:4,runner_name:'not-started'},
   {status:'in_progress',labels:['ubuntu-latest'],runner_id:0,runner_name:''},
   {status:'in_progress',labels:['tauceti-fleet'],runner_id:5,runner_name:'fleet-'+ 'b'.repeat(12)+'-instance'},
  ]);
 });
 assert.deepEqual(value.running_jobs,{github:1,namespace:1,[machine]:1,['fleet-'+ 'b'.repeat(12)]:1});
 assert.equal(calls.length,6,'reporting adds no discovery requests');
});
test('cached-only HTTP reads never initiate discovery, including an empty cache',async()=>{
 let reads=0;
 const mf=await runtime(async()=>{reads++;throw new Error('status must not scan GitHub');});
 try {
  const url='https://state/v1/runner-capacity?repository=org/repo&cached_only=true';
  for(let i=0;i<3;i++){
   const response=await mf.dispatchFetch(url,{headers:{Authorization:'Bearer client'}});
   assert.equal(response.status,200);
   const value=await response.json();assert.equal(value.read_only,true);assert.equal(value.reason,'capacity_not_prepared');
  }
  assert.equal(reads,0);
  const dedicated=await mf.dispatchFetch(url.replace('/runner-capacity?','/runner-capacity/cached?'),{headers:{Authorization:'Bearer client'}});
  assert.equal((await dedicated.json()).read_only,true);assert.equal(reads,0);
  assert.equal((await mf.dispatchFetch(url)).status,401);
  assert.equal((await mf.dispatchFetch(url.replace('cached_only=true','cached_only=other'),{headers:{Authorization:'Bearer client'}})).status,400);
 }finally{await mf.dispose();}
});
test('routing snapshot counts queued/running Namespace builds and fleet targets, excluding controls',async()=>{
 const {read}=readFixture();
 const snapshot=await scanHostedCapacity('org',async path=>{
  if(!path.includes('/jobs?'))return read(path);
  return envelope('jobs',[
   {name:'sandboxed-build',status:'queued',labels:['nscloud-ubuntu-test']},
   {name:'sandboxed-build',status:'in_progress',labels:['nscloud-ubuntu-test']},
   {name:'warm (1)',status:'in_progress',labels:['nscloud-ubuntu-test']},
   {name:'pick-runner / pick',status:'in_progress',labels:['nscloud-ubuntu-test']},
   {name:'sandboxed-build',status:'queued',labels:['self-hosted','fleet-'+ 'a'.repeat(32),'fleet-instance-'+ 'b'.repeat(16)]},
  ]);
 });
 assert.equal(snapshot.routing_version,2);assert.equal(snapshot.namespace_busy,3);
 assert.equal(snapshot.busy,0);assert.equal(snapshot.queued_fleet.length,1);
});
test('pagination is complete and a missing later page is unavailable, never an undercount',async()=>{
 const {read}=readFixture({paginate:true});assert.equal((await scanHostedCapacity('org',read)).busy,2);
 for(const opts of [{failed:true},{paginate:true,failedLater:true}])await assert.rejects(()=>scanHostedCapacity('org',readFixture(opts).read),/capacity_/);
});
test('live Actions count drift does not invent another page or reject a complete Link traversal',async()=>{
 const {read,calls}=readFixture();
 const value=await scanHostedCapacity('org',async path=>{
  const r=await read(path);
  if(!Array.isArray(r.body))r.body.total_count+=2;
  return r;
 });
 assert.equal(value.busy,1);assert.equal(calls.length,6);
});
test('one authenticated refresh is shared by concurrent callers and the prepared cache costs no reads',async()=>{
 let reads=0;
 const {read}=readFixture();
 const mf=await runtime(async(req,u)=>{reads++;const r=await read(u.pathname+u.search);return response(r.body,'core',4999,r.status);});
 try {
  const get=()=>mf.dispatchFetch('https://state/v1/runner-capacity?repository=org/repo',{headers:{Authorization:'Bearer client'}});
  const values=await Promise.all(Array.from({length:8},async()=>{const r=await get();assert.equal(r.status,200);return r.json();}));
  assert.equal(reads,6);assert.ok(values.every(v=>v.complete && v.busy===1 && v.organization==='org'));
  assert.equal((await (await get()).json()).cached,true);assert.equal(reads,6);
  assert.equal((await mf.dispatchFetch('https://state/v1/runner-capacity?repository=other/repo',{headers:{Authorization:'Bearer client'}})).status,401);
  assert.equal((await mf.dispatchFetch('https://state/v1/runner-capacity?repository=org/repo')).status,401);
  assert.equal(reads,6);
 }finally{await mf.dispose();}
});
test('a failed refresh is briefly cached and cannot publish partial capacity or hammer quota',async()=>{
 let reads=0;
 const mf=await runtime(async(req,u)=>{reads++;return response({message:'API rate limit exceeded'},'core',0,403);});
 try {
  const get=()=>mf.dispatchFetch('https://state/v1/runner-capacity?repository=org/repo',{headers:{Authorization:'Bearer client'}});
  const first=await get();assert.equal(first.status,503);const v=await first.json();assert.equal(v.complete,false);assert.equal(v.busy,undefined);
  const second=await get();assert.equal(second.status,503);assert.equal(reads,1);
 }finally{await mf.dispose();}
});

// Exercise the real scheduler with in-memory durable storage; no production-only test hooks.
const shim={name:'capacity-fixture',setup(b){b.onResolve({filter:/^cloudflare:workers$/},()=>({path:'fixture',namespace:'capacity-fixture'}));b.onLoad({filter:/.*/,namespace:'capacity-fixture'},()=>({contents:'export class DurableObject{constructor(ctx,env){this.ctx=ctx;this.env=env}}',loader:'js'}));}};
const bundle=await build({entryPoints:['src/installation.ts'],bundle:true,format:'esm',platform:'node',write:false,plugins:[shim]});
const {InstallationScheduler}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
function schedulerFixture(){
 const values=new Map(),background=[];let slots=0;
 const storage={get:async k=>values.get(k),put:async(k,v)=>values.set(k,structuredClone(v)),sql:{exec(q){return {toArray:()=>[],one:()=>({n:q.includes('COUNT(*)')?slots:0,cost:0})};}}};
 const scheduler=new InstallationScheduler({storage,waitUntil(promise){background.push(promise);}},{});scheduler.token=async()=> 'token';scheduler.usable=()=>5000;
 return {scheduler,values,background,setSlots:n=>{slots=n}};
}
test('cached-only reads retain expired snapshots without token or provider work',async()=>{
 const {scheduler,values}=schedulerFixture();
 scheduler.token=async()=>{throw new Error('must not mint a token');};
 scheduler.http=async()=>{throw new Error('must not contact GitHub');};
 const saved={complete:true,organization:'org',observed_at:1000,retry_at:2000,running_jobs:{github:3,namespace:8}};
 values.set('runner-capacity:org',saved);
 const response=await scheduler.cachedRunnerCapacity({name:'org/repo'});
 const value=await response.json();assert.equal(value.read_only,true);
 assert.equal(value.observed_at,1000);assert.deepEqual(value.running_jobs,saved.running_jobs);
 assert.deepEqual(values.get('runner-capacity:org'),saved);
 assert.equal(values.has('runner-capacity-attempts'),false);
 values.set('runner-capacity:org',{complete:false,reason:'capacity_deferred'});
 values.set('runner-capacity-last-good:org',saved);
 const retained=await(await scheduler.cachedRunnerCapacity({name:'org/repo'})).json();
 assert.deepEqual(retained.running_jobs,saved.running_jobs);
 assert.equal(retained.observed_at,1000);
 assert.equal(values.get('runner-capacity:org').complete,false,'reporting must not change routing failure state');
});
test('hourly allowance and lost responses survive refresh and cannot be bypassed by another caller',async()=>{
 const {scheduler,values}=schedulerFixture();let calls=0;
 scheduler.http=async()=>{calls++;throw new Error('connection lost')};
 values.set('runner-capacity-attempts',Array(2400).fill(Date.now()));
 let result=await (await scheduler.runnerCapacity({name:'org/repo',installation:'200'})).json();
 assert.equal(result.reason,'capacity_hourly_budget');assert.equal(calls,0);
 values.delete('runner-capacity:org');values.delete('runner-capacity-attempts');
 result=await (await scheduler.runnerCapacity({name:'org/repo',installation:'200'})).json();
 assert.equal(result.complete,false);assert.equal(calls,1);
 assert.equal(values.get('runner-capacity-attempts').length,1);
 await scheduler.runnerCapacity({name:'org/repo',installation:'200'});assert.equal(calls,1);
});
test('continuous callers fit up to 96 requests per scan within the unchanged hourly allowance',async()=>{
 for(const repositories of [17,26,31]){
 const {scheduler,values}=schedulerFixture();let calls=0,clock=Date.now();const original=Date.now;
 Date.now=()=>clock;
 scheduler.http=async path=>{
  calls++;
  if(path.startsWith('/orgs/'))return envelope(null,Array.from({length:repositories},(_,i)=>({name:'repo'+i,archived:false})));
  if(path.includes('/actions/runs?'))return envelope('workflow_runs',[{id:'1'}]);
  return envelope('jobs',[hosted]);
 };
 try {
  let scans=0;
  for(let elapsed=0;elapsed<3600000;elapsed+=5000){
   const fresh=values.get('runner-capacity:org')?.retry_at>clock;
   const result=await (await scheduler.runnerCapacity({name:'org/repo',installation:'200'})).json();
   assert.equal(result.complete,true);assert.equal(result.busy,repositories);
   assert.equal(result.cached===true,fresh);
   assert.ok(result.retry_at>clock);assert.ok(result.retry_at-result.observed_at<150000);
   if(!fresh)scans++;
   assert.ok(calls<=2400);
   clock+=5000;
  }
  assert.equal(scans,25);assert.equal(calls,scans*(1+3*repositories));
  assert.equal(values.get('runner-capacity-attempts').length,calls);
 }finally{Date.now=original;}
 }
});
test('brief contention for existing request slots yields and completes instead of caching an internal failure',async()=>{
 const {scheduler,setSlots}=schedulerFixture();const {read}=readFixture();const original=globalThis.fetch;
 globalThis.fetch=async url=>{const u=new URL(url);const v=await read(u.pathname+u.search);return Response.json(v.body);};
 setSlots(4);const timer=setTimeout(()=>setSlots(0),100);
 try {
  const result=await scheduler.runnerCapacity({name:'org/repo',installation:'200'});
  const v=await result.json();assert.equal(result.status,200);assert.equal(v.busy,1);
 }finally{clearTimeout(timer);globalThis.fetch=original;}
});
test('a busy organization inventory uses at most three reads at once and drains in-flight reads on failure',async()=>{
 let active=0,peak=0;
 const reader=(fail=false)=>async path=>{
  active++;peak=Math.max(peak,active);
  try {
   await new Promise(resolve=>setTimeout(resolve,2));
   if(path.startsWith('/orgs/'))return envelope(null,Array.from({length:6},(_,i)=>({name:'repo'+i,archived:false})));
   if(path.includes('/actions/runs?'))return envelope('workflow_runs',Array.from({length:4},(_,i)=>({id:String(i+1)})));
   if(fail && path.includes('/runs/1/'))throw Error('fixture request rejected');
   return envelope('jobs',[{...hosted,id:Number(path.match(/runs\/(\d+)/)[1])*100}]);
  }finally{active--;}
 };
 const value=await scanHostedCapacity('org',reader());assert.equal(value.busy,24);assert.equal(value.requests,37);
 assert.equal(peak,3);assert.equal(active,0);
 await assert.rejects(()=>scanHostedCapacity('org',reader(true)),/fixture request rejected/);
 assert.equal(active,0,'refresh must settle all started requests before releasing the single flight');
});

test('a complete cold inventory can exceed 96 requests without losing useful capacity',async()=>{
 const calls=[];
 const value=await scanHostedCapacity('org',async path=>{
  calls.push(path);
  if(path.startsWith('/orgs/'))return envelope(null,[{name:'code',archived:false}]);
  if(path.includes('status=in_progress'))return envelope('workflow_runs',[{id:1}]);
  if(path.includes('status=queued'))return envelope('workflow_runs',Array.from({length:100},(_,i)=>({id:i+2})));
  return envelope('jobs',path.includes('/runs/1/jobs')?[hosted,{name:'sandboxed-build',status:'in_progress',labels:['nscloud-ubuntu-test']}]:[]);
 });
 assert.equal(value.routing_version,2);assert.equal(value.busy,1);assert.equal(value.requests,104);
 assert.equal(value.namespace_busy,1);assert.deepEqual(value.queued_fleet,[]);
 assert.equal(value.placement_unavailable,undefined);assert.equal(calls.length,104);
});
const ns=(id,status='queued')=>({id,name:'sandboxed-build',status,labels:['nscloud-ubuntu-test']});
test('a thousand queued workflows establish saturation from their first page, busiest repository first',async()=>{
 const calls=[];let active=0,peak=0;
 const value=await scanHostedCapacity('org',async path=>{
  calls.push(path);active++;peak=Math.max(peak,active);
  try {
   await new Promise(resolve=>setTimeout(resolve,1));
   if(path.startsWith('/orgs/'))return envelope(null,[{name:'quiet',archived:false},{name:'busy',archived:false}]);
   if(path.includes('status=in_progress'))return envelope('workflow_runs',path.includes('/busy/')?[{id:1}]:[]);
   if(path.includes('/runs/1/jobs'))return envelope('jobs',[hosted,...Array.from({length:16},(_,i)=>ns(i+2,'in_progress'))]);
   assert.ok(path.includes('/busy/'),'inspect the repository with observed Namespace load first');
   if(path.includes('status=queued')) {
    assert.ok(path.endsWith('page=1'),'saturation must not wait for all ten queued-run pages');
    return {...envelope('workflow_runs',Array.from({length:100},(_,i)=>({id:i+2}))),link:'<https://api.github.com/next>; rel="next"'};
   }
   return envelope('jobs',[ns(Number(path.match(/runs\/(\d+)/)[1])+100)]);
  }finally{active--;}
 });
 assert.equal(value.busy,1);assert.equal(value.routing_version,1);
 assert.ok(value.namespace_busy_lower_bound>=20 && value.namespace_busy_lower_bound<=23);
 assert.equal(value.placement_unavailable,'capacity_namespace_target');
 assert.ok(calls.length<=12);assert.ok(peak<=4);assert.equal(active,0,'all started reads settle before the snapshot returns');
});
test('production-sized running inventory leaves budget to prove saturation behind queued controls',async()=>{
 const calls=[];
 const value=await scanHostedCapacity('org',async path=>{
  calls.push(path);
  if(path.startsWith('/orgs/'))return envelope(null,Array.from({length:14},(_,i)=>({name:i===13?'busy':'quiet'+i,archived:false})));
  if(path.includes('status=in_progress'))return envelope('workflow_runs',path.includes('/busy/')?Array.from({length:53},(_,i)=>({id:i+1,path:'build.yml'})):[]);
  if(path.includes('status=queued')) {
   assert.ok(path.includes('/busy/'));assert.ok(path.endsWith('page=1'));
   return {...envelope('workflow_runs',Array.from({length:100},(_,i)=>({id:i+100,path:i<90?'control.yml':'build.yml'}))),link:'<https://api.github.com/next>; rel="next"'};
  }
  const id=Number(path.match(/runs\/(\d+)/)[1]);
  if(id<=16)return envelope('jobs',[ns(id,'in_progress')]);
  if(id<=36)return envelope('jobs',[{...hosted,id}]);
  if(id<=53)return envelope('jobs',[]);
  assert.ok(id>=190,'known Namespace workflows must precede the ninety queued controls');
  return envelope('jobs',[ns(id)]);
 });
 assert.equal(value.busy,20);assert.ok(value.namespace_busy_lower_bound>=20);
 assert.equal(value.placement_unavailable,'capacity_namespace_target');assert.ok(calls.length<=75);
});
test('queued partial pages retain distinct observed jobs when a later jobs page fails',async()=>{
 const value=await scanHostedCapacity('org',async path=>{
  if(path.startsWith('/orgs/'))return envelope(null,[{name:'code',archived:false}]);
  if(path.includes('status=in_progress'))return envelope('workflow_runs',[{id:1}]);
  if(path.includes('status=queued'))return envelope('workflow_runs',[{id:2}]);
  if(path.includes('/runs/1/jobs'))return envelope('jobs',[hosted,ns(2,'in_progress')]);
  if(path.endsWith('page=2'))return {status:503,body:{}};
  return {...envelope('jobs',[ns(3),ns(3),ns(4),{...hosted,id:5}]),link:'<https://api.github.com/next>; rel="next"'};
 });
 assert.equal(value.routing_version,1);assert.equal(value.namespace_busy_lower_bound,3);
 assert.equal(value.busy,1,'queued extension cannot alter complete running GitHub capacity');
 assert.equal(value.placement_unavailable,'capacity_inventory_unavailable');
});
test('complete queued inventories count duplicate runs and jobs only once',async()=>{
 const value=await scanHostedCapacity('org',async path=>{
  if(path.startsWith('/orgs/'))return envelope(null,[{name:'code',archived:false}]);
  if(path.includes('status=in_progress'))return envelope('workflow_runs',[{id:1},{id:1}]);
  if(path.includes('status=queued'))return envelope('workflow_runs',[{id:1},{id:2},{id:2}]);
  if(path.includes('/runs/1/jobs'))return envelope('jobs',[ns(1,'in_progress')]);
  if(path.endsWith('page=2'))return envelope('jobs',[ns(2),ns(3)]);
  return {...envelope('jobs',[ns(1),ns(2)]),link:'<https://api.github.com/next>; rel="next"'};
 });
 assert.equal(value.routing_version,2);assert.equal(value.namespace_busy,3);
});
test('saturated running inventory needs no queued extension',async()=>{
 const value=await scanHostedCapacity('org',async path=>{
  if(path.startsWith('/orgs/'))return envelope(null,[{name:'code',archived:false}]);
  if(path.includes('status=in_progress'))return envelope('workflow_runs',[{id:1}]);
  assert.ok(path.includes('/runs/1/jobs'));
  return envelope('jobs',Array.from({length:20},(_,i)=>ns(i+1,'in_progress')));
 });
 assert.equal(value.namespace_busy_lower_bound,20);assert.equal(value.requests,3);
 assert.equal(value.placement_unavailable,'capacity_namespace_target');
});
test('missing queued placement data preserves running capacity; missing running data still fails',async()=>{
 const {read}=readFixture();
 const value=await scanHostedCapacity('org',path=>path.includes('status=queued')?Promise.resolve({status:403,body:{}}):read(path));
 assert.equal(value.routing_version,1);assert.equal(value.busy,1);assert.equal(value.namespace_busy,undefined);
 await assert.rejects(()=>scanHostedCapacity('org',path=>path.includes('/jobs?')?Promise.resolve({status:403,body:{}}):read(path)),/capacity_inventory_unavailable/);
});

test('mixed configured targets share the most informative organization snapshot without overlapping scans',async()=>{
 const {scheduler,values}=schedulerFixture();let calls=0,active=0,peak=0;
 scheduler.http=async path=>{
  calls++;active++;peak=Math.max(peak,active);
  try {
   await new Promise(resolve=>setTimeout(resolve,1));
   if(path.startsWith('/orgs/'))return envelope(null,[{name:'repo',archived:false}]);
   if(path.includes('status=in_progress'))return envelope('workflow_runs',[{id:1}]);
   if(path.includes('status=queued'))return envelope('workflow_runs',[]);
   return envelope('jobs',Array.from({length:20},(_,i)=>ns(i+1,'in_progress')));
  }finally{active--;}
 };
 const get=async namespace_cap=>(await scheduler.runnerCapacity({name:'org/repo',installation:'200',namespace_cap})).json();
 const [small,large,coalesced]=await Promise.all([get(8),get(30),get(30)]);
 assert.equal(small.namespace_discovery_target,8);assert.equal(small.namespace_busy_lower_bound,20);
 assert.equal(large.namespace_discovery_target,30);assert.equal(large.routing_version,2);assert.equal(large.namespace_busy,20);
 assert.deepEqual(coalesced,large);
 assert.equal(peak,1,'different target scans serialize through the shared installation budget');assert.equal(calls,7);
 assert.equal((await get(8)).cached,true);assert.equal((await get(30)).cached,true);assert.equal((await get(10000)).cached,true);assert.equal(calls,7);
 const saved=values.get('runner-capacity:org');values.set('runner-capacity:org',{...saved,retry_at:0});
 const refreshed=await get(8);assert.equal(refreshed.namespace_discovery_target,30);assert.equal(refreshed.namespace_busy,20);
 assert.equal((await get(30)).cached,true);assert.equal(calls,11,'mixed caps add no scans after the first target upgrade');
 for(const namespace_cap of [0,-1,true,1.5,10001,'20'])assert.equal((await scheduler.runnerCapacity({name:'org/repo',installation:'200',namespace_cap})).status,400);
});
test('endpoint validates requested caps before starting discovery',async()=>{
 let calls=0;const mf=await runtime(async()=>{calls++;throw Error('must not read');});
 try {
  for(const cap of ['0','-1','1.5','true','10001','020','']) {
   const r=await mf.dispatchFetch('https://state/v1/runner-capacity?repository=org/repo&namespace_cap='+cap,{headers:{Authorization:'Bearer client'}});
   assert.equal(r.status,400,cap);
  }
  assert.equal(calls,0);
 }finally{await mf.dispose();}
});
test('partial scan retains observed fleet reservations and never certifies offset queued pagination',async()=>{
 const labels=['self-hosted','fleet-'+ 'a'.repeat(32),'fleet-instance-'+ 'b'.repeat(16)];
 const value=await scanHostedCapacity('org',async path=>{
  if(path.startsWith('/orgs/'))return envelope(null,[{name:'code',archived:false}]);
  if(path.includes('status=in_progress'))return envelope('workflow_runs',[{id:1}]);
  if(path.includes('status=queued'))return path.endsWith('page=1')?
   {...envelope('workflow_runs',[{id:2}]),link:'<https://api.github.com/next>; rel="next"'}:envelope('workflow_runs',[]);
  if(path.includes('/runs/1/jobs'))return envelope('jobs',[hosted]);
  return envelope('jobs',[ns(2),{id:3,name:'sandboxed-build',status:'queued',labels}]);
 });
 assert.equal(value.routing_version,1);assert.equal(value.namespace_busy_lower_bound,1);
 assert.deepEqual(value.queued_fleet,[labels]);assert.equal(value.placement_unavailable,'capacity_queue_pagination');
});

test('valid endpoint targets reach discovery and complete snapshots serve every cap',async()=>{
 const {read}=readFixture();let reads=0;
 const mf=await runtime(async(req,u)=>{
  reads++;const path=u.pathname+u.search;
  const r=path.includes('/jobs?')?envelope('jobs',Array.from({length:20},(_,i)=>ns(i+1,'in_progress'))):await read(path);
  return response(r.body,'core',4999,r.status);
 });
 try {
  const get=async cap=>{
   const r=await mf.dispatchFetch('https://state/v1/runner-capacity?repository=org/repo'+(cap===undefined?'':'&namespace_cap='+cap),{headers:{Authorization:'Bearer client'}});
   assert.equal(r.status,200);return r.json();
  };
  const first=await get(8);assert.equal(first.namespace_discovery_target,8);assert.equal(first.namespace_busy_lower_bound,20);
  const larger=await get(30);assert.equal(larger.namespace_discovery_target,30);assert.equal(larger.routing_version,2);assert.equal(larger.namespace_busy,20);
  const count=reads;assert.equal((await get()).cached,true);assert.equal((await get(10000)).cached,true);assert.equal(reads,count);
 }finally{await mf.dispose();}
});
test('partial discovery failures share the same cooldown across different caps',async()=>{
 const {scheduler}=schedulerFixture();let calls=0;
 scheduler.http=async()=>{calls++;throw Error('lost response');};
 const get=async namespace_cap=>(await scheduler.runnerCapacity({name:'org/repo',installation:'200',namespace_cap})).json();
 const [one,two,three]=await Promise.all([get(8),get(30),get(30)]);
 assert.equal(one.complete,false);assert.equal(two.complete,false);assert.equal(three.complete,false);assert.equal(calls,1);
 assert.equal((await get(10000)).cached,true);assert.equal(calls,1);
});

test('valid cold capacity discovery can finish after twenty seconds within the unchanged request budget',async t=>{
 let clock=Date.parse('2026-11-06T21:00:00Z');t.mock.method(Date,'now',()=>clock);
 const {read,calls}=readFixture();
 const snapshot=await scanHostedCapacity('org',async(path,body)=>{clock+=5000;return read(path,body);});
 assert.equal(snapshot.routing_version,2);
 assert.ok(snapshot.elapsed_ms>20000);
 assert.ok(snapshot.observed_at<Date.now(),'queued discovery cannot refresh the age of the running observation');
 assert.equal(snapshot.requests,calls.length);
 assert.ok(snapshot.requests<=96);
 assert.equal(snapshot.namespace_busy,2);
});

test('a shared scan retains its lifetime and publishes for later callers without a waiting HTTP client',async()=>{
 const {scheduler,values,background}=schedulerFixture();
 const {read}=readFixture();let release;
 const held=new Promise(resolve=>{release=resolve;});
 scheduler.http=async(path,token,method,body)=>{await held;return read(path,body);};
 const abandoned=scheduler.runnerCapacity({name:'org/repo',installation:'200'});
 for(let i=0;i<20 && background.length===0;i++)await new Promise(resolve=>setImmediate(resolve));
 assert.equal(background.length,1,'the owner retains the scan independently of the HTTP response');
 release();await Promise.all(background);
 const saved=values.get('runner-capacity:org');assert.equal(saved.routing_version,2);
 assert.equal(saved.namespace_busy,2);
 assert.equal((await(await scheduler.runnerCapacity({name:'org/repo',installation:'200'})).json()).cached,true);
 await abandoned;
});

test('large passes finish beyond 96 calls but cannot exceed remaining hourly allowance',async()=>{
 for(const [repositories,spent] of [[40,0],[51,2300]]){
  const {scheduler,values}=schedulerFixture();let calls=0;
  values.set('runner-capacity-attempts',Array(spent).fill(Date.now()));
  scheduler.http=async path=>{
   calls++;
   if(path.startsWith('/orgs/'))return envelope(null,Array.from({length:repositories},(_,i)=>({name:'repo'+i,archived:false})));
   if(path.includes('/actions/runs?'))return envelope('workflow_runs',[{id:1}]);
   return envelope('jobs',[hosted]);
  };
  const result=await(await scheduler.runnerCapacity({name:'org/repo',installation:'200'})).json();
  assert.equal(values.get('runner-capacity-attempts').length,spent+calls);
  assert.ok(spent+calls<=2400);
  if(!spent){
   assert.equal(result.routing_version,2);assert.equal(result.busy,repositories);assert.ok(calls>96);
   assert.ok(result.retry_at-result.observed_at>=145000);
   assert.ok(result.retry_at-result.observed_at<150000,'headroom must not create a stale-routing interval');
  }else{
   assert.equal(result.complete,false);assert.equal(result.reason,'capacity_hourly_budget');assert.equal(calls,100);
  }
  const previous=calls;
  assert.equal((await(await scheduler.runnerCapacity({name:'org/repo',installation:'200'})).json()).cached,true);
  assert.equal(calls,previous);
 }
});

test('a valid slow scan can acquire a temporarily occupied slot after its first twenty seconds',async t=>{
 const {scheduler,setSlots}=schedulerFixture(),{read}=readFixture();let clock=Date.now(),first=true,timer;
 t.mock.method(Date,'now',()=>clock);const original=globalThis.fetch;
 globalThis.fetch=async url=>{
  if(first){first=false;clock+=21000;setSlots(4);timer=setTimeout(()=>{clock+=300;setSlots(0);},100);}
  const u=new URL(url),value=await read(u.pathname+u.search);return Response.json(value.body);
 };
 try{
  const result=await(await scheduler.runnerCapacity({name:'org/repo',installation:'200'})).json();
  assert.equal(result.complete,true);assert.equal(result.routing_version,2);assert.equal(result.busy,1);
 }finally{clearTimeout(timer);globalThis.fetch=original;}
});

test('queued discovery time does not push refresh beyond the client freshness window',async t=>{
 const {scheduler}=schedulerFixture(),{read}=readFixture();let clock=Date.now();
 t.mock.method(Date,'now',()=>clock);
 scheduler.http=async(path,token,method,body)=>{clock+=5000;return read(path,body);};
 const result=await(await scheduler.runnerCapacity({name:'org/repo',installation:'200'})).json();
 assert.equal(result.routing_version,2);assert.ok(result.observed_at<clock);
 assert.equal(result.retry_at-result.observed_at,145000);
 assert.ok(result.retry_at<result.observed_at+150000);
});
