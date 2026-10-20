import {test} from 'node:test';
import assert from 'node:assert/strict';
import {bucket} from './r2_fixture.mjs';
import {providerMonitoring as m,worker} from './prevention_runtime.mjs';
const identity={project:'project-123',branch:'br-test',endpoint:'ep-test'};
const setup=()=>({ARCHIVE:bucket(),NEON_MONITORING:JSON.stringify(identity),NEON_MONITORING_API_KEY:'private-test-token'});

test('provider branch quota includes all databases, labels project sum, preserves last good on failure and recovers',async()=>{
 const env=setup(),original=globalThis.fetch;let fail=false,calls=0,bytes=850;
 globalThis.fetch=async(url,options)=>{
  calls++;assert.equal(options.headers.Authorization,'Bearer private-test-token');assert.ok(url.startsWith('https://console.neon.tech/api/v2/projects/project-123'));
  if(fail)return new Response('{}',{status:503});
  return Response.json(url.endsWith('/branches?limit=100')?{branches:[{id:'br-test',logical_size:bytes},{id:'br-other',logical_size:100}]}:url.endsWith('/endpoints/ep-test')?{endpoint:{id:'ep-test',branch_id:'br-test',current_state:'active',autoscaling_limit_min_cu:.5,autoscaling_limit_max_cu:2}}:{project:{id:'project-123',settings:{quota:{logical_size_bytes:1000}}}});
 };
 const age=()=>{const o=env.ARCHIVE.objects.get('control/neon-provider.json');const v=JSON.parse(o.bytes);v.last_attempt-=60000;o.bytes=Buffer.from(JSON.stringify(v));};
 try {
  await m.prepareProvider(env);let v=await m.providerObservation(env);assert.equal(calls,3);assert.equal(v.capacity.level,'critical');assert.equal(v.capacity.allowance_scope,'branch');assert.equal(v.project_logical_branch_sum_bytes,950);assert.equal(v.account_storage_allowance_bytes,null);assert.equal(v.compute.cpu_utilization,null);
  await m.prepareProvider(env);assert.equal(calls,3);
  age();fail=true;await m.prepareProvider(env);v=await m.providerObservation(env);assert.equal(v.capacity.bytes,850);assert.equal(v.stale,true);assert.equal(m.providerAlarms(v).level,'warning');
  age();fail=false;bytes=600;await m.prepareProvider(env);v=await m.providerObservation(env);assert.equal(v.stale,false);assert.equal(v.capacity.level,'ok');assert.equal(JSON.stringify(v).includes('private-test-token'),false);
  const before=calls;await m.providerObservation(env);assert.equal(calls,before,'prepared reads never call provider/GitHub/SQL');
 }finally{globalThis.fetch=original;}
});

test('compute adapter rejects wrong identity, invalid or duplicate clocks and reports staleness and saturation separately',async()=>{
 const env=setup(),now=Date.now();
 for(const bad of [{at:now-180001},{at:now+1001},{cpu_utilization:NaN},{cpu_utilization:1.01},{branch:'br-wrong'}])await assert.rejects(m.saveCompute(env,{...identity,at:now,cpu_utilization:.8,...bad},now));
 await m.saveCompute(env,{...identity,at:now,cpu_utilization:.95,password:'discard'},now);
 await assert.rejects(m.saveCompute(env,{...identity,at:now,cpu_utilization:.9},now));
 let v=await m.providerObservation(env,now);assert.equal(v.compute.cpu_utilization,.95);assert.equal(m.providerAlarms(v).level,'critical');assert.equal(JSON.stringify(v).includes('discard'),false);
 v=await m.providerObservation(env,now+180001);assert.equal(v.compute.cpu_utilization,null);assert.equal(v.compute.stale,true);assert.equal(m.providerAlarms(v).level,'warning');
 env.ARCHIVE.failures.push((op)=>op==='put');await assert.rejects(m.saveCompute(env,{...identity,at:now+1,cpu_utilization:.5},now+1));
 await m.saveCompute(env,{...identity,at:now+1,cpu_utilization:.5},now+1);assert.equal((await m.providerObservation(env,now+1)).compute.cpu_utilization,.5);
});

test('compute endpoint requires existing admin authentication and never enters repository or quota owner',async()=>{
 const env=setup();env.ADMIN_TOKEN_HASH=await crypto.subtle.digest('SHA-256',new TextEncoder().encode('admin')).then(b=>Buffer.from(b).toString('hex'));
 env.REPOSITORIES=env.INSTALLATIONS={get(){throw Error('must not call DO');}};
 const call=(token,input)=>worker.fetch(new Request('https://test/admin/monitoring/compute',{method:'POST',headers:{Authorization:'Bearer '+token},body:JSON.stringify(input)}),env,{});
 assert.equal((await call('wrong',{})).status,401);
 assert.equal((await call('admin',{...identity,at:Date.now(),cpu_utilization:.5})).status,200);
 assert.equal((await call('admin',{...identity,at:Date.now(),cpu_utilization:2})).status,400);
 env.ARCHIVE.failures.push(op=>op==='put');
 assert.equal((await call('admin',{...identity,at:Date.now()+1,cpu_utilization:.5})).status,503);
});

test('provider inventory truncation, unknown quotas, malformed identities and failed persistence never fabricate headroom',async()=>{
 const env=setup(),original=globalThis.fetch;let malformed=false;
 globalThis.fetch=async url=>Response.json(url.endsWith('/branches?limit=100')?{branches:Array.from({length:100},(_,i)=>({id:i===0?'br-test':'br-'+i,logical_size:100}))}:url.endsWith('/endpoints/ep-test')?{endpoint:{id:'ep-test',branch_id:malformed?'br-wrong':'br-test'}}:{project:{id:'project-123',settings:{quota:{logical_size_bytes:0}}}});
 try {
  await m.prepareProvider(env);let v=await m.providerObservation(env);assert.equal(v.branch_inventory_complete,false);assert.equal(v.project_logical_branch_sum_bytes,null);assert.equal(v.capacity.capacity_unknown,true);assert.ok(m.providerAlarms(v).reasons.includes('branch_storage_limit_unknown'));
  env.ARCHIVE.objects.clear();malformed=true;await m.prepareProvider(env);v=await m.providerObservation(env);assert.equal(v.stale,true);assert.equal(v.capacity,undefined);
  env.ARCHIVE.objects.clear();malformed=false;env.ARCHIVE.failures.push(op=>op==='put');await m.prepareProvider(env);assert.equal((await m.providerObservation(env)).stale,true);
  env.ARCHIVE.objects.clear();await m.prepareProvider(env);assert.equal((await m.providerObservation(env)).stale,false);
 }finally{globalThis.fetch=original;}
});

test('a malformed optional provider configuration cannot prevent saved SQL diagnostics',async()=>{
 const env=setup();env.NEON_MONITORING='{';
 const v=await m.providerObservation(env);assert.equal(v.stale,true);assert.equal(m.providerAlarms(v).level,'warning');
});
