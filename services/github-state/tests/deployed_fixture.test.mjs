import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {build} from 'esbuild';
import {Miniflare,convertV4MiniflareOptions} from 'miniflare';

test('native frozen-source rehearsal preserves reads and fences writes without external requests',async()=>{
 const bundled=await build({entryPoints:['scripts/fixtures/deployed-outage.ts'],bundle:true,format:'esm',platform:'browser',target:'es2022',external:['cloudflare:workers'],write:false});
 let calls=0;
 const mf=new Miniflare(convertV4MiniflareOptions({modules:true,script:bundled.outputFiles[0].text,compatibilityDate:'2026-10-11',durableObjects:{REPOSITORIES:{className:'RepositoryState',useSQLite:true}},r2Buckets:['ARCHIVE'],bindings:{DRILL_RUN_ID:'issue317-drill-12345678',DRILL_DATABASE_HOST:'ep-fixture.c-7.us-east-2.aws.neon.tech',REPOSITORY_DATABASE_URL:'postgresql://github_state_test_app:synthetic@ep-fixture.c-7.us-east-2.aws.neon.tech/github_state_test',ALLOWED_REPOSITORIES:JSON.stringify([{id:'100',name:'issue317/drill',installation:'317'}]),FAULT_INJECTION:'true',ADMIN_TOKEN_HASH:createHash('sha256').update('admin').digest('hex')},outboundService:()=>{calls++;throw Error('unexpected external request');}}));
 try {
  for(let n=0;n<2;n++){
   const response=await mf.dispatchFetch('https://fixture/fixture/frozen-source',{method:'POST',headers:{Authorization:'Bearer admin'}});
   assert.equal(response.status,200);const result=await response.json();
   assert.deepEqual(result,{readable:true,result_status:'ok',header:'retained frozen header',blocked_write:true,activated_source_rejected:true,blocked_external_fetches:0});
  }
  assert.equal(calls,0);
 }finally{await mf.dispose();}
});
