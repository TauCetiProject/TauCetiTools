import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHmac} from 'node:crypto';
import {runtime,repository,send} from './runtime.mjs';

const head='a0bbb5deb583efbe98c55ea48ac3c71e4cb10b59';
const workerExtension=`{...worker,async fetch(req,env,ctx){const pending=[];const response=await worker.fetch(req,env,{waitUntil:p=>pending.push(p)});await Promise.all(pending);return response;}}`;
// Webhooks never establish inventory coverage; seed a complete open PR inventory.
const seed=`async fetch(req){
 if(new URL(req.url).pathname==='/test-seed'){
  await this.atomic(async state=>{await state.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)','open_prs:{}','open_prs','{}',1,Date.now(),0,null);});
  return Response.json({ok:true});
 }
 return super.fetch(req);
}`;
const pull={number:7,title:'feat: add a lemma',body:'',state:'open',draft:false,updated_at:'2026-11-11T03:38:57Z',created_at:'2026-11-11T03:38:57Z',
 head:{sha:head,ref:'lemma',repo:{name:'repo',owner:{login:'contributor'}}},base:{ref:'main',sha:'b'.repeat(40)},user:{login:'contributor',type:'User'},labels:[],html_url:'https://github.com/org/repo/pull/7'};
async function webhook(mf,event,delivery,payload){
 const body=JSON.stringify({repository:{id:100},installation:{id:200},...payload});
 const response=await mf.dispatchFetch('https://test/webhook/github',{method:'POST',body,headers:{'x-github-delivery':delivery,'x-github-event':event,'x-hub-signature-256':'sha256='+createHmac('sha256','secret').update(body).digest('hex')}});
 assert.equal(response.status,202);
}
// Surveys share a ten-second snapshot; a distinct unused skip area reads current facts.
let reads=0;
const survey=async mf=>{
 const args=encodeURIComponent(JSON.stringify({numbers:'7',skip:'unused-'+(reads++)}));
 const result=await (await mf.dispatchFetch('https://test/v1/query?repository=org/repo&operation=survey&args='+args,{headers:{Authorization:'Bearer client'}})).json();
 assert.equal(result.status,'ok');return result;
};

test('pull_request and status webhooks reach the survey while the queue is unconsumed',async()=>{
 let upstream=0;
 const mf=await runtime(async()=>{upstream++;throw Error('unexpected upstream read');},{},{repositoryAlarms:false,repositoryExtension:seed,workerExtension});
 try{
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName(repository.id)),archive=await mf.getR2Bucket('ARCHIVE');
  await stub.fetch('https://internal/test-seed');
  await webhook(mf,'pull_request','opened-pr',{action:'opened',number:7,pull_request:pull});
  const opened=await survey(mf);
  assert.deepEqual(opened.data.map(row=>[row.pr.number,row.pr.headRefOid,row.pr.statusCheckRollupCoverage]),[[7,head,'unavailable']]);
  await webhook(mf,'status','build-status',{sha:head,context:'build',state:'success',description:'passed sandboxed build and audits',created_at:'2026-11-11T03:50:33Z'});
  const built=await survey(mf);
  assert.equal(built.data[0].pr.statusCheckRollupCoverage,'webhook_observed');
  assert.deepEqual(built.data[0].pr.statusCheckRollup,[{context:'build',state:'SUCCESS',startedAt:'2026-11-11T03:50:33Z'}]);
  // Queue redelivery after immediate application stops at the committed receipt.
  const raw=(await archive.list({prefix:'webhooks/'})).objects.find(o=>o.key.endsWith('/build-status.json'));
  assert.equal((await send(stub,'/event',{repository:repository.name,key:raw.key,delivery:'build-status',event:'status',received:Date.now()})).status,200);
  const replayed=await survey(mf);
  assert.equal(replayed.revision,built.revision);assert.deepEqual(replayed.data,built.data);
  for(const delivery of ['opened-pr','build-status'])assert.ok(await archive.head('receipts/100/'+delivery+'.json'));
  assert.equal((await archive.list({prefix:'parked/100/pointers/'})).objects.length,0);
  assert.equal(upstream,0);
 }finally{await mf.dispose();}
});

test('CI and unlisted webhooks still wait for the queue',async()=>{
 const mf=await runtime(async()=>{throw Error('unexpected upstream read');},{},{repositoryAlarms:false,workerExtension});
 try{
  const archive=await mf.getR2Bucket('ARCHIVE');
  await webhook(mf,'issues','queued-issue',{action:'opened',issue:{number:8,title:'An issue',body:'',state:'open',updated_at:'2026-11-11T03:38:57Z',created_at:'2026-11-11T03:38:57Z',user:{login:'contributor'},labels:[]}});
  assert.equal((await archive.list({prefix:'parked/100/pointers/'})).objects.length,1);
  assert.equal(await archive.head('receipts/100/queued-issue.json'),null);
 }finally{await mf.dispose();}
});
