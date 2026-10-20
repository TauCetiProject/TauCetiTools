import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {build} from 'esbuild';
import {Miniflare,convertV4MiniflareOptions} from 'miniflare';
const bundled=await build({entryPoints:['scripts/fixtures/deployed-outage.ts'],bundle:true,format:'esm',platform:'browser',target:'es2022',external:['cloudflare:workers'],write:false});
function fixture(extra={}){
 let calls=0;
 const mf=new Miniflare(convertV4MiniflareOptions({modules:true,script:bundled.outputFiles[0].text,compatibilityDate:'2026-10-11',durableObjects:{REPOSITORIES:{className:'RepositoryState',useSQLite:true},CANARIES:{className:'CanaryState',useSQLite:true}},r2Buckets:['ARCHIVE'],bindings:{DRILL_RUN_ID:'issue317-drill-12345678',DRILL_DISPOSABLE_BUCKET:'issue317-drill-12345678',DRILL_DATABASE_HOST:'ep-fixture.c-7.us-east-2.aws.neon.tech',REPOSITORY_DATABASE_URL:'postgresql://github_state_test_app:synthetic@ep-fixture.c-7.us-east-2.aws.neon.tech/github_state_test',ALLOWED_REPOSITORIES:JSON.stringify([{id:'100',name:'issue317/drill',installation:'317'}]),DRILL_CANARY_REPOSITORY:JSON.stringify([{id:'101',name:'issue317/canary',installation:'318'}]),FAULT_INJECTION:'true',ADMIN_TOKEN_HASH:createHash('sha256').update('admin').digest('hex'),...extra},outboundService:()=>{calls++;throw Error('unexpected external request');}}));
 return {mf,get calls(){return calls;},send:(path,body={},headers={})=>mf.dispatchFetch('https://fixture/fixture/'+path,{method:'POST',headers:{Authorization:'Bearer admin',...headers},body:JSON.stringify(body)})};
}

test('native real source fences preserve reads, block writes, and reject cold target reads precisely',async()=>{
 const f=fixture();try {
  for(let n=0;n<2;n++){
   const response=await f.send('frozen-source');assert.equal(response.status,200);
   assert.deepEqual(await response.json(),{readable:true,result_status:'ok',header:'retained frozen header',blocked_write:true,activated_source_rejected:true,blocked_external_fetches:0});
  }
  await f.send('faults',{database:true});
  const cold=await f.send('canary/query',{repository:'issue317/canary',operation:'files',args:{number:99}},{'X-Request-ID':'native-cold-outage'});
  assert.equal(cold.status,503);const missing=await cold.json();
  assert.equal(missing.error,'database_unavailable');assert.equal(missing.request_id,'native-cold-outage');assert.equal(Object.hasOwn(missing,'data'),false);
  assert.equal(f.calls,0);
 }finally{await f.mf.dispose();}
});

test('disposable cleanup requires confirmation and repeated empty cleanup succeeds',async()=>{
 const f=fixture();try{
  const bucket=await f.mf.getR2Bucket('ARCHIVE');await bucket.put('resources/100/fixture-retained','synthetic');
  assert.equal((await f.send('delete-objects',{run:'wrong'})).status,400);assert.ok(await bucket.head('resources/100/fixture-retained'));
  for(let n=0;n<2;n++){
   const response=await f.send('delete-objects',{run:'issue317-drill-12345678'});assert.equal(response.status,200);assert.equal((await response.json()).remaining,false);
  }
  assert.equal((await bucket.list()).objects.length,0);assert.equal(f.calls,0);
 }finally{await f.mf.dispose();}
 const unconfirmed=fixture({DRILL_DISPOSABLE_BUCKET:'other-bucket'});try{assert.equal((await unconfirmed.send('delete-objects',{run:'issue317-drill-12345678'})).status,400);}finally{await unconfirmed.mf.dispose();}
});

test('fixture rejects production credentials, repositories, and invalid run identities before I/O',async()=>{
 for(const values of [
  {DRILL_RUN_ID:'production'},
  {REPOSITORY_DATABASE_URL:'postgresql://github_state_prod_app:synthetic@ep-fixture.c-7.us-east-2.aws.neon.tech/github_state_production'},
  {MIGRATION_DATABASE_URL:'postgresql://github_state_prod_migration:synthetic@ep-fixture.c-7.us-east-2.aws.neon.tech/github_state_test'},
  {ALLOWED_REPOSITORIES:JSON.stringify([{id:'100',name:'TauCetiProject/TauCeti',installation:'317'}])},
  {GITHUB_PRIVATE_KEY:'forbidden-fixture-value'},
 ]){
  const f=fixture(values);try{const response=await f.send('objects');assert.equal(response.status,400);assert.equal((await response.json()).error,'fixture_configuration_rejected');assert.equal(f.calls,0);}finally{await f.mf.dispose();}
 }
});

test('HTTP and WebSocket GitHub attempts are blocked and recorded across fixture isolates',async()=>{
 const f=fixture();try{
  for(const mode of ['http','websocket']){const response=await f.send('probe-network',{mode});assert.equal(response.status,200);assert.equal((await response.json()).log_failures,0);}
  const audit=await (await f.send('network-audit')).json();assert.equal(audit.recorded_attempts,2);assert.equal(audit.truncated,false);assert.equal(audit.isolate_log_failures,0);assert.equal(f.calls,0);
 }finally{await f.mf.dispose();}
});


test('public cold PostgreSQL errors retain the service envelope, reason and request ID',async()=>{
 const f=fixture({REPOSITORY_BACKEND:'postgres',REPOSITORY_GENERATION:'native-cutover',CLIENT_TOKENS:JSON.stringify([{hash:createHash('sha256').update('client').digest('hex'),repositories:['issue317/drill']}])});
 try{
  await f.send('faults',{database:true});
  const params=new URLSearchParams({repository:'issue317/drill',operation:'files',args:'{"number":99}'});
  const response=await f.mf.dispatchFetch('https://fixture/v1/query?'+params,{headers:{Authorization:'Bearer client','X-Request-ID':'public-cold-outage'}});
  assert.equal(response.status,503);const result=await response.json();
  assert.deepEqual(result,{error:'storage_unavailable',reason:'database_unavailable',request_id:'public-cold-outage'});
  assert.equal(Object.hasOwn(result,'data'),false);assert.equal(f.calls,0);
 }finally{await f.mf.dispose();}
});
