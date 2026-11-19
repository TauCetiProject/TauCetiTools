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


test('in-progress scoreboards cannot replace completed verdicts in compact or direct facts',()=>{
 const head='a'.repeat(40), previous='b'.repeat(40);
 const board=(id,updated,meta)=>({id,updated_at:updated,body:'<!--tauceti-scoreboard--> <!--tauceti-meta:v1 '+JSON.stringify(meta)+'-->'});
 const done=board('1','1',{head_sha:previous,states:{reuse:'green'},runs:[]});
 const init=board('2','2',{head_sha:head,mode:'init',states:{reuse:'blocking_request',scope:'stale'},runs:[]});
 assert.equal(facts.summarize([done,init],[]).meta.head_sha,previous);
 assert.equal(facts.summarize([init],[]).meta,null);
 assert.equal(facts.issueFragment(init).meta,null);
 assert.notEqual(facts.SURVEY_INDEX_VERSION,facts.SURVEY_CONTRACT);
});


test('old coordination comments and cached fragments do not project claims',()=>{
 const body='<!--tauceti-review-in-progress '+JSON.stringify({head:'a'.repeat(40),expires_at:9999999999,providers:['claude']})+'-->';
 assert.equal('markers' in facts.summarize([{body}],[]),false);
 assert.equal('markers' in facts.summarizeFragments([{meta:null,rebase_heads:[],marker:{head:'a'.repeat(40),expires_at:9999999999}}],[]),false);
});

test('scoreboard selection is independent of comment order (the parity fixture checks it against Python)',()=>{
 const board=(id,updated,state)=>({id,updated_at:updated,body:'<!--tauceti-scoreboard--> <!--tauceti-meta:v1 '+JSON.stringify({head_sha:'a'.repeat(40),states:{reuse:state},submitted_by:'r'+id})+'-->'});
 const tied=[board('7','2026-11-16T18:00:00Z','green'),board('12','2026-11-16T18:00:00Z','blocking_request'),board('9','2026-11-16T18:00:00Z','green')];
 for(const order of [[0,1,2],[2,1,0],[1,0,2],[0,2,1]]) assert.equal(facts.summarize(order.map(i=>tied[i]),[]).meta.submitted_by,'r12');
 const edited=board('6859938902','2026-11-16T18:00:40Z','blocking_request'),posted=board('6924490807','2026-11-16T17:53:30Z','green');
 assert.equal(facts.summarize([edited,posted],[]).meta.submitted_by,'r6859938902');
 assert.equal(facts.summarize([posted,edited],[]).meta.submitted_by,'r6859938902');
 const created=(id,at)=>({...board(id,'', 'green'),created_at:at});
 assert.equal(facts.summarize([created('2','2026-11-16T11:00:00Z'),created('1','2026-11-16T10:00:00Z')],[]).meta.submitted_by,'r2');
 assert.equal(facts.summarize([created('1','2026-11-16T12:00:00Z'),created('2','2026-11-16T11:00:00Z')],[]).meta.submitted_by,'r1');
 const unnumbered=(name,state)=>({...board('x','2026-11-16T18:00:00Z',state),id:undefined,body:board(name,'',state).body});
 assert.equal(facts.summarize([unnumbered('a','green'),unnumbered('b','blocking_request')],[]).meta.submitted_by,'rb');
});
