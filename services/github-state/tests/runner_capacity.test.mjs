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
  if(options.failed)return {status:403,body:{message:'rate limited'}};
  const jobs=[hosted,{...hosted,status:'queued'},{...hosted,labels:['nscloud-ubuntu-test']},{...hosted,labels:['namespace-profile-foo']},{...hosted,labels:['self-hosted']}];
  if(options.paginate && !path.endsWith('page=2'))return {...envelope('jobs',jobs),body:{jobs,total_count:6},link:'<https://api.github.com/next>; rel="next"'};
  if(options.paginate)return envelope('jobs',[hosted]);
  if(options.incomplete)return {status:200,body:{jobs,total_count:10}};
  return envelope('jobs',jobs);
 };
 return {read,calls};
}
test('counts running hosted jobs across repositories, excluding archived repos and non-hosted labels',async()=>{
 const {read,calls}=readFixture();const s=await scanHostedCapacity('org',read);
 assert.equal(s.busy,1);assert.equal(s.requests,4);assert.equal(calls.length,4);
 assert.ok(calls.some(p=>p.includes('status=in_progress&')));
 assert.ok(!calls.some(p=>p.includes('/old/')));
});
test('pagination is complete and a missing later page is unavailable, never an undercount',async()=>{
 const {read}=readFixture({paginate:true});assert.equal((await scanHostedCapacity('org',read)).busy,2);
 for(const opts of [{failed:true},{incomplete:true}])await assert.rejects(()=>scanHostedCapacity('org',readFixture(opts).read),/capacity_/);
});
test('one authenticated refresh is shared by concurrent callers and the prepared cache costs no reads',async()=>{
 let reads=0;
 const {read}=readFixture();
 const mf=await runtime(async(req,u)=>{reads++;const r=await read(u.pathname+u.search);return response(r.body,'core',4999,r.status);});
 try {
  const get=()=>mf.dispatchFetch('https://state/v1/runner-capacity?repository=org/repo',{headers:{Authorization:'Bearer client'}});
  const values=await Promise.all(Array.from({length:8},async()=>{const r=await get();assert.equal(r.status,200);return r.json();}));
  assert.equal(reads,4);assert.ok(values.every(v=>v.complete && v.busy===1 && v.organization==='org'));
  assert.equal((await (await get()).json()).cached,true);assert.equal(reads,4);
  assert.equal((await mf.dispatchFetch('https://state/v1/runner-capacity?repository=other/repo',{headers:{Authorization:'Bearer client'}})).status,401);
  assert.equal((await mf.dispatchFetch('https://state/v1/runner-capacity?repository=org/repo')).status,401);
  assert.equal(reads,4);
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
 values.set('runner-capacity-attempts',Array(500).fill(Date.now()));
 let result=await (await scheduler.runnerCapacity({name:'org/repo',installation:'200'})).json();
 assert.equal(result.reason,'capacity_hourly_budget');assert.equal(calls,0);
 values.delete('runner-capacity:org');values.delete('runner-capacity-attempts');
 result=await (await scheduler.runnerCapacity({name:'org/repo',installation:'200'})).json();
 assert.equal(result.complete,false);assert.equal(calls,1);
 assert.equal(values.get('runner-capacity-attempts').length,1);
 await scheduler.runnerCapacity({name:'org/repo',installation:'200'});assert.equal(calls,1);
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
 const value=await scanHostedCapacity('org',reader());assert.equal(value.busy,24);assert.equal(value.requests,31);
 assert.equal(peak,3);assert.equal(active,0);
 await assert.rejects(()=>scanHostedCapacity('org',reader(true)),/fixture request rejected/);
 assert.equal(active,0,'refresh must settle all started requests before releasing the single flight');
});
