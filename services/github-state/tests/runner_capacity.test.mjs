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
