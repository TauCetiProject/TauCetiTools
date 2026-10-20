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
  return Response.json(url.endsWith('/branches?limit=100')?{branches:[{id:'br-test',logical_size:bytes},{id:'br-other',logical_size:100}]}:url.endsWith('/branches/br-test')?{branch:{id:'br-test',logical_size:bytes}}:url.endsWith('/endpoints/ep-test')?{endpoint:{id:'ep-test',branch_id:'br-test',current_state:'active',autoscaling_limit_min_cu:.5,autoscaling_limit_max_cu:2}}:{project:{id:'project-123',settings:{quota:{logical_size_bytes:1000}}}});
 };
 const age=()=>{const o=env.ARCHIVE.objects.get('control/neon-provider.json');const v=JSON.parse(o.bytes);v.last_attempt-=60000;o.bytes=Buffer.from(JSON.stringify(v));};
 try {
  await m.prepareProvider(env);let v=await m.providerObservation(env);assert.equal(calls,4);assert.equal(v.capacity.level,'critical');assert.equal(v.capacity.allowance_scope,'branch');assert.equal(v.project_logical_branch_sum_bytes,950);assert.equal(v.account_storage_allowance_bytes,null);assert.equal(v.compute.cpu_utilization,null);
  await m.prepareProvider(env);assert.equal(calls,4);
  age();fail=true;await m.prepareProvider(env);v=await m.providerObservation(env);assert.equal(v.capacity.bytes,850);assert.equal(v.stale,false);assert.equal(m.providerAlarms(v).level,'critical');assert.equal(v.capacity.stale,false);assert.equal((await m.providerObservation(env,Date.now()+200000)).stale,true);
  age();fail=false;bytes=600;await m.prepareProvider(env);v=await m.providerObservation(env);assert.equal(v.stale,false);assert.equal(v.capacity.level,'ok');assert.equal(JSON.stringify(v).includes('private-test-token'),false);
  const before=calls;await m.providerObservation(env);assert.equal(calls,before,'prepared reads never call provider/GitHub/SQL');
 }finally{globalThis.fetch=original;}
});

test('compute adapter rejects wrong identity, invalid or duplicate clocks and reports staleness and saturation separately',async()=>{
 const env=setup(),now=Date.now();
 for(const bad of [{at:now-180001},{at:now+60001},{cpu_utilization:NaN},{cpu_utilization:1.01},{branch:'br-wrong'}])await assert.rejects(m.saveCompute(env,{...identity,at:now,cpu_utilization:.8,...bad},now));
 await m.saveCompute(env,{...identity,at:now,cpu_utilization:.95,password:'discard'},now);
 await assert.rejects(m.saveCompute(env,{...identity,at:now,cpu_utilization:.9},now));
 let v=await m.providerObservation(env,now);assert.equal(v.compute.cpu_utilization,.95);assert.equal(m.providerAlarms(v).level,'critical');assert.equal(JSON.stringify(v).includes('discard'),false);
 v=await m.providerObservation(env,now+180001);assert.equal(v.compute.cpu_utilization,null);assert.equal(v.compute.stale,true);assert.equal(m.providerAlarms(v).level,'warning');
 env.ARCHIVE.failures.push((op)=>op==='put');await assert.rejects(m.saveCompute(env,{...identity,at:now+1,cpu_utilization:.5},now+1));
 await m.saveCompute(env,{...identity,at:now+1,cpu_utilization:.5},now+1);assert.equal((await m.providerObservation(env,now+1)).compute.cpu_utilization,.5);
});

test('compute endpoint requires an ingest-only credential and never enters repository or quota owner',async()=>{
 const env=setup();env.ADMIN_TOKEN_HASH=await crypto.subtle.digest('SHA-256',new TextEncoder().encode('admin')).then(b=>Buffer.from(b).toString('hex'));
 env.MONITORING_INGEST_TOKEN_HASH=await crypto.subtle.digest('SHA-256',new TextEncoder().encode('ingest')).then(b=>Buffer.from(b).toString('hex'));
 env.REPOSITORIES=env.INSTALLATIONS={get(){throw Error('must not call DO');}};
 const call=(token,input)=>worker.fetch(new Request('https://test/admin/monitoring/compute',{method:'POST',headers:{Authorization:'Bearer '+token},body:JSON.stringify(input)}),env,{});
 assert.equal((await call('wrong',{})).status,401);assert.equal((await call('admin',{})).status,401);
 const forbidden=await worker.fetch(new Request('https://test/admin/migration',{method:'POST',headers:{Authorization:'Bearer ingest'},body:'{}'}),env,{});assert.equal(forbidden.status,401);
 assert.equal((await call('ingest',{...identity,at:Date.now(),cpu_utilization:.5})).status,200);
 assert.equal((await call('ingest',{...identity,at:Date.now(),cpu_utilization:2})).status,400);
 env.ARCHIVE.failures.push(op=>op==='put');
 assert.equal((await call('ingest',{...identity,at:Date.now()+1,cpu_utilization:.5})).status,503);
 env.NEON_MONITORING='{';assert.equal((await call('ingest',{...identity,at:Date.now(),cpu_utilization:.5})).status,503);
 assert.equal((await call('ingest',{extra:'x'.repeat(5000)})).status,413);
});

test('provider inventory truncation, unknown quotas, malformed identities and failed persistence never fabricate headroom',async()=>{
 const env=setup(),original=globalThis.fetch;let malformed=false;
 globalThis.fetch=async url=>Response.json(url.endsWith('/branches?limit=100')?{branches:Array.from({length:100},(_,i)=>({id:i===0?'br-test':'br-'+i,logical_size:100}))}:url.endsWith('/branches/br-test')?{branch:{id:'br-test',logical_size:100}}:url.endsWith('/endpoints/ep-test')?{endpoint:{id:'ep-test',branch_id:malformed?'br-wrong':'br-test'}}:{project:{id:'project-123',settings:{quota:{logical_size_bytes:0}}}});
 try {
  await m.prepareProvider(env);let v=await m.providerObservation(env);assert.equal(v.branch_inventory_complete,false);assert.equal(v.project_logical_branch_sum_bytes,null);assert.equal(v.capacity.capacity_unknown,true);assert.equal(m.providerAlarms(v).reasons.includes('branch_storage_limit_unknown'),false);
  env.ARCHIVE.objects.clear();malformed=true;await m.prepareProvider(env);v=await m.providerObservation(env);assert.equal(v.stale,true);assert.equal(v.capacity,undefined);
  env.ARCHIVE.objects.clear();malformed=false;env.ARCHIVE.failures.push(op=>op==='put');await m.prepareProvider(env);assert.equal((await m.providerObservation(env)).stale,true);
  env.ARCHIVE.objects.clear();await m.prepareProvider(env);assert.equal((await m.providerObservation(env)).stale,false);
 }finally{globalThis.fetch=original;}
});

test('a malformed optional provider configuration cannot prevent saved SQL diagnostics',async()=>{
 const env=setup();env.NEON_MONITORING='{';
 const v=await m.providerObservation(env);assert.equal(v.stale,true);assert.equal(m.providerAlarms(v).level,'warning');
});

test('cached provider observations age without I/O and preserve SQL pressure fields',()=>{
 const now=Date.now(),v=m.ageProvider({configured:true,at:now-180001,capacity:{level:'critical',stale:false},compute:{at:now-180001,cpu_utilization:.95,stale:false,active_queries:7}},now);
 assert.equal(v.stale,true);assert.equal(v.capacity.stale,true);assert.equal(v.compute.stale,true);assert.equal(v.compute.cpu_utilization,null);assert.equal(v.compute.active_queries,7);assert.equal(m.providerAlarms(v).level,'critical');
});

test('a configured provider cannot describe another database endpoint as the selected PostgreSQL store',async()=>{
 const env=setup(),now=Date.now();
 await env.ARCHIVE.put('control/neon-provider.json',JSON.stringify({...identity,at:now}));
 for(const url of [undefined,'postgres://u:p@ep-wrong.neon.tech/db','invalid']) {
  env.REPOSITORY_DATABASE_URL=url;const v=await m.providerObservation(env,now,true);
  assert.equal(v.error,'provider_database_identity_mismatch');assert.equal(v.stale,true);assert.equal(v.compute.cpu_utilization,null);
 }
 env.REPOSITORY_DATABASE_URL='postgres://u:p@ep-test-pooler.neon.tech/db';assert.equal((await m.providerObservation(env,now,true)).stale,false);
});

test('a failed checkpoint read cannot erase retained diagnostics or the forecast baseline',async()=>{
 const env=setup(),prior={...identity,at:Date.now()-10000,last_attempt:Date.now()-61000,capacity:{level:'critical',bytes:950},history:[{at:1,bytes:100}]};
 await env.ARCHIVE.put('control/neon-provider.json',JSON.stringify(prior));
 const original=env.ARCHIVE.objects.get('control/neon-provider.json').bytes.toString();
 env.ARCHIVE.failures.push((op,key)=>op==='get' && key==='control/neon-provider.json');
 await assert.rejects(m.prepareProvider(env));assert.equal(env.ARCHIVE.objects.get('control/neon-provider.json').bytes.toString(),original);
 const v=await m.providerObservation(env,Date.now()+200000);assert.equal(v.capacity.stale,true);assert.equal(m.providerAlarms(v).level,'critical');
});

test('an absent selected branch in a large or malformed inventory does not disable its independent capacity sample',async()=>{
 const env=setup(),original=globalThis.fetch;
 globalThis.fetch=async url=>Response.json(url.endsWith('/branches?limit=100')?{branches:[{id:'br-new',logical_size:null}],pagination:{next:'page2'}}:url.endsWith('/branches/br-test')?{branch:{id:'br-test',logical_size:850}}:url.endsWith('/endpoints/ep-test')?{endpoint:{id:'ep-test',branch_id:'br-test'}}:{project:{id:'project-123',branch_logical_size_limit_bytes:1000,settings:{quota:{logical_size_bytes:2000}}}});
 try {await m.prepareProvider(env);const v=await m.providerObservation(env);assert.equal(v.stale,false);assert.equal(v.capacity.level,'critical');assert.equal(v.branch_limits.effective_bytes,1000);assert.equal(v.branch_inventory_complete,false);assert.equal(v.project_logical_branch_sum_bytes,null);}
 finally {globalThis.fetch=original;}
});

test('repeated cached estimates preserve but do not increment forecast confirmation',()=>{
 const now=Date.now(),baseline={at:now-3600000,bytes:100,allowance:1000};
 let prior=m.providerCapacity({at:now,bytes:200,allowance:1000},baseline,now);
 assert.equal(prior.forecast_breaches,1);
 prior=m.providerCapacity({at:now+60000,bytes:200,allowance:1000},baseline,now+60000,prior);assert.equal(prior.forecast_breaches,1);
 prior=m.providerCapacity({at:now+120000,bytes:201,allowance:1000},baseline,now+120000,prior);assert.equal(prior.forecast_breaches,2);
 prior=m.providerCapacity({at:now+180000,bytes:201,allowance:1000},baseline,now+180000,prior);assert.equal(prior.forecast_breaches,2);
 prior=m.providerCapacity({at:now+240000,bytes:202,allowance:1000},baseline,now+240000,prior);assert.equal(prior.forecast_confirmed,true);
 prior=m.providerCapacity({at:now+300000,bytes:199,allowance:1000},baseline,now+300000,prior);assert.equal(prior.forecast_breaches,0);
 const changedLevel=m.providerCapacity({at:now,bytes:200,allowance:1000},baseline,now,{bytes:200,allowance:1000,forecast_level:'warning',forecast_breaches:2});assert.equal(changedLevel.forecast_breaches,0);
});

test('storage monitoring can be enrolled before CPU routing; required CPU absence then warns',async()=>{
 const env=setup(),now=Date.now();
 await env.ARCHIVE.put('control/neon-provider.json',JSON.stringify({...identity,at:now,capacity:{level:'ok'}}));
 let v=await m.providerObservation(env,now);assert.equal(m.providerAlarms(v).level,'ok');assert.equal(v.compute.stale,true);
 env.NEON_MONITORING=JSON.stringify({...identity,cpu_required:true});
 v=await m.providerObservation(env,now);assert.ok(m.providerAlarms(v).reasons.includes('compute_monitoring_unavailable'));
 await m.saveCompute(env,{...identity,at:now,cpu_utilization:.5},now);v=await m.providerObservation(env,now);assert.equal(m.providerAlarms(v).level,'ok');
});

test('Workers collector uses bounded manual redirects and never follows upstream to GitHub',async()=>{
 const {Miniflare,convertV4MiniflareOptions}=await import('miniflare'),{build}=await import('esbuild');
 const script=(await build({stdin:{contents:"import {prepareProvider,providerObservation} from './src/provider_monitoring';export default {async fetch(req,env){await prepareProvider(env);return Response.json(await providerObservation(env));}}",resolveDir:process.cwd(),loader:'ts'},bundle:true,format:'esm',platform:'browser',write:false})).outputFiles[0].text;
 let redirect=false,badLimit=false,calls=0;
 const mf=new Miniflare(convertV4MiniflareOptions({modules:true,script,compatibilityDate:'2026-10-11',r2Buckets:['ARCHIVE'],bindings:{NEON_MONITORING:JSON.stringify(identity),NEON_MONITORING_API_KEY:'private-test-token'},outboundService:async req=>{
  calls++;const url=new URL(req.url);assert.equal(url.hostname,'console.neon.tech');assert.equal(req.method,'GET');
  if(redirect)return new Response(null,{status:302,headers:{Location:'https://api.github.com/must-not-be-called'}});
  return Response.json(url.pathname.endsWith('/branches/br-test')?{branch:{id:'br-test',logical_size:100}}:url.pathname.endsWith('/branches')?{branches:[{id:'br-test',logical_size:100}]}:url.pathname.endsWith('/endpoints/ep-test')?{endpoint:{id:'ep-test',branch_id:'br-test'}}:{project:{id:'project-123',branch_logical_size_limit_bytes:badLimit?-1:1000}});
 }}));
 try {
  let v=await (await mf.dispatchFetch('https://test/sample')).json();assert.equal(v.stale,false);assert.equal(calls,4);assert.equal(v.branch_inventory_complete,true);
  const archive=await mf.getR2Bucket('ARCHIVE');
  const next=async()=>{const prior=await (await archive.get('control/neon-provider.json')).json();prior.last_attempt-=60000;await archive.put('control/neon-provider.json',JSON.stringify(prior));};
  await next();redirect=true;v=await (await mf.dispatchFetch('https://test/sample')).json();assert.equal(v.error,'provider_http_unavailable');assert.equal(calls,8);assert.equal(v.capacity.bytes,100);assert.equal(v.stale,false,'one failed collection retains a recent usable sample');
  await next();redirect=false;badLimit=true;v=await (await mf.dispatchFetch('https://test/sample')).json();assert.equal(v.capacity.bytes,100);assert.equal(v.capacity.capacity_unknown,true);assert.equal(v.branch_limits.error,'provider_quota_unavailable');
 }finally {await mf.dispose();}
});

test('real Workers R2 conditional creation and replacement fence concurrent compute updates',async()=>{
 const {Miniflare,convertV4MiniflareOptions}=await import('miniflare');
 const mf=new Miniflare(convertV4MiniflareOptions({modules:true,script:'export default {fetch(){return new Response("unused")}}',compatibilityDate:'2026-10-11',r2Buckets:['ARCHIVE']}));
 try {
  const native=await mf.getR2Bucket('ARCHIVE'),env=setup(),now=Date.now();
  const race=async(offset)=>{
   let reads=0,release;const ready=new Promise(resolve=>{release=resolve;});
   env.ARCHIVE={async get(key){const value=await native.get(key);if(++reads===2)release();await ready;return value;},put:(...args)=>native.put(...args)};
   const results=await Promise.allSettled([offset,offset+1].map(n=>m.saveCompute(env,{...identity,at:now+n,cpu_utilization:.5},now+n)));
   assert.equal(results.filter(r=>r.status==='fulfilled').length,1);assert.equal(results.find(r=>r.status==='rejected').reason.message,'compute_observation_conflict');
   env.ARCHIVE=native;const last=await (await native.get('control/neon-compute.json')).json();
   if(last.at<now+offset+1)await m.saveCompute(env,{...identity,at:now+offset+1,cpu_utilization:.5},now+offset+1);
   assert.equal((await (await native.get('control/neon-compute.json')).json()).at,now+offset+1);
  };
  await race(0);await race(2);
 }finally {await mf.dispose();}
});
