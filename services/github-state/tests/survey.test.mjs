import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {build} from 'esbuild';
import {runtime,repository,response,send} from './runtime.mjs';

const bundle=await build({entryPoints:['src/survey.ts'],bundle:true,platform:'node',format:'esm',write:false});
const facts=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
test('Python fallback and TypeScript projection agree on 240 changing histories and scopes',()=>{
 const process=spawnSync('python3',['../../tests/fixtures/survey_facts_parity.py'],{encoding:'utf8',maxBuffer:5000000});
 assert.equal(process.status,0,process.stderr);
 for(const c of JSON.parse(process.stdout)) {
  assert.deepEqual(facts.summarize(c.issues,c.reviews),c.expected);
  assert.deepEqual(facts.targetFocuses(c.row.body),c.focuses);
  assert.equal(facts.inScope(c.row,c.scope,c.skip),c.included);
 }
});

test('compact survey is authenticated, roadmap scoped and does not issue GitHub requests',async()=>{
 let network=0;
 const mf=await runtime(async()=>{network++;return response([],'core');},{},{repositoryAlarms:false});
 try {
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName('100'));
  // Seed repository identity without scheduling the unrelated bootstrap.
  // Configure deliberately starts background GitHub work, which can overlap
  // this assertion on a busy runner even though the survey itself is local.
  const key='webhooks/survey-fixture.json';
  await (await mf.getR2Bucket('ARCHIVE')).put(key,'{}');
  await send(stub,'/event',{key,delivery:'survey-fixture',event:'ping',repository:repository.name,received:Date.now()});
  const before=network;
  const r=await mf.dispatchFetch('https://test/v1/query?repository=org/repo&operation=survey&args='+encodeURIComponent(JSON.stringify({roadmap:'PDE',skip:'',numbers:''})),{headers:{Authorization:'Bearer client'}});
  assert.equal(r.status,200);const payload=await r.json();
  assert.equal(payload.contract,facts.SURVEY_CONTRACT);assert.equal(payload.status,'unavailable');assert.equal(network,before);
  assert.equal((await mf.dispatchFetch('https://test/v1/query?repository=org/repo&operation=survey',{headers:{Authorization:'Bearer invalid'}})).status,401);
 }finally{await mf.dispose();}
});

test('obsolete pause markers are not projected, including previously stored fragments',()=>{
 const head='a'.repeat(40), marker='<!--tauceti-blocked:v1 '+head+'-->';
 assert.equal('blocked' in facts.summarize([{body:marker+'\nEvidence',user:{login:'Author'}}],[]),false);
 assert.equal('blocked' in facts.summarizeFragments([{meta:null,rebase_heads:[],marker:null,blocked:[{head,author:'author'}]}],[]),false);
});
