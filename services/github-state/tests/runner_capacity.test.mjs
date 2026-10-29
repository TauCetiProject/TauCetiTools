import test from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {runtime,response} from './runtime.mjs';
const code=await build({entryPoints:['src/runner-capacity.ts'],bundle:true,format:'esm',platform:'node',write:false});
const {scanHostedCapacity}=await import('data:text/javascript;base64,'+Buffer.from(code.outputFiles[0].text).toString('base64'));
const hosted={status:'in_progress',labels:['ubuntu-latest']};
const envelope=(key,rows)=>({status:200,body:key?{[key]:rows,total_count:rows.length}:rows});
function readFixture(options={}) {
 const calls=[];
 const read=async path=>{
  calls.push(path);
  if(path.startsWith('/orgs/'))return envelope(null,[{name:'code',archived:false},{name:'old',archived:true},{name:'other',archived:false}]);
  if(path.includes('/other/'))return envelope('workflow_runs',[]);
  if(path.includes('/actions/runs?'))return envelope('workflow_runs',[{id:'1'}]);
  if(options.failed || options.failedLater && path.endsWith('page=2'))return {status:403,body:{message:'rate limited'}};
  const jobs=[hosted,{...hosted,status:'queued'},{...hosted,labels:['nscloud-ubuntu-test']},{...hosted,labels:['namespace-profile-foo']},{...hosted,labels:['self-hosted']}];
  if(options.paginate && !path.endsWith('page=2'))return {...envelope('jobs',jobs),body:{jobs,total_count:6},link:'<https://api.github.com/next>; rel="next"'};
  if(options.paginate)return envelope('jobs',[hosted]);
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
 const values=new Map();let slots=0;
 const storage={get:async k=>values.get(k),put:async(k,v)=>values.set(k,structuredClone(v)),sql:{exec(q){return {toArray:()=>[],one:()=>({n:q.includes('COUNT(*)')?slots:0,cost:0})};}}};
 const scheduler=new InstallationScheduler({storage},{});scheduler.token=async()=> 'token';scheduler.usable=()=>5000;
 return {scheduler,values,setSlots:n=>{slots=n}};
}
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
test('continuous callers reuse two-minute snapshots within the rolling hourly allowance',async()=>{
 for(const repositories of [17,26]){
 const {scheduler,values}=schedulerFixture();let calls=0,clock=Date.now();const original=Date.now;
 Date.now=()=>clock;
 scheduler.http=async path=>{
  calls++;
  if(path.startsWith('/orgs/'))return envelope(null,Array.from({length:repositories},(_,i)=>({name:'repo'+i,archived:false})));
  if(path.includes('/actions/runs?'))return envelope('workflow_runs',[{id:'1'}]);
  return envelope('jobs',[hosted]);
 };
 try {
  // 52 requests per scan matches production; 79 exercises the 80-call bound.
  for(let minute=0;minute<60;minute++){
   const result=await (await scheduler.runnerCapacity({name:'org/repo',installation:'200'})).json();
   assert.equal(result.complete,true);assert.equal(result.busy,repositories);
   assert.equal(result.cached===true,minute%2===1);
   assert.ok(result.retry_at>clock);
   clock+=60000;
  }
  assert.equal(calls,30*(1+3*repositories));assert.equal(values.get('runner-capacity-attempts').length,calls);
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
   return envelope('jobs',[hosted]);
  }finally{active--;}
 };
 const value=await scanHostedCapacity('org',reader());assert.equal(value.busy,24);assert.equal(value.requests,37);
 assert.equal(peak,3);assert.equal(active,0);
 await assert.rejects(()=>scanHostedCapacity('org',reader(true)),/fixture request rejected/);
 assert.equal(active,0,'refresh must settle all started requests before releasing the single flight');
});

test('many empty queued workflows cannot discard complete running GitHub capacity',async()=>{
 const calls=[];
 const value=await scanHostedCapacity('org',async path=>{
  calls.push(path);
  if(path.startsWith('/orgs/'))return envelope(null,[{name:'code',archived:false}]);
  if(path.includes('status=in_progress'))return envelope('workflow_runs',[{id:1}]);
  if(path.includes('status=queued'))return envelope('workflow_runs',Array.from({length:100},(_,i)=>({id:i+2})));
  assert.ok(path.includes('/runs/1/jobs'),'must not begin a queued-job extension that cannot fit');
  return envelope('jobs',[hosted,{name:'sandboxed-build',status:'in_progress',labels:['nscloud-ubuntu-test']}]);
 });
 assert.equal(value.routing_version,1);assert.equal(value.busy,1);assert.equal(value.requests,4);
 assert.equal(value.namespace_busy,undefined);assert.equal(value.queued_fleet,undefined);
 assert.equal(value.namespace_busy_lower_bound,1);
 assert.equal(value.placement_unavailable,'capacity_scan_limit');assert.equal(calls.length,4);
});
test('missing queued placement data preserves running capacity; missing running data still fails',async()=>{
 const {read}=readFixture();
 const value=await scanHostedCapacity('org',path=>path.includes('status=queued')?Promise.resolve({status:403,body:{}}):read(path));
 assert.equal(value.routing_version,1);assert.equal(value.busy,1);assert.equal(value.namespace_busy,undefined);
 await assert.rejects(()=>scanHostedCapacity('org',path=>path.includes('/jobs?')?Promise.resolve({status:403,body:{}}):read(path)),/capacity_inventory_unavailable/);
});
