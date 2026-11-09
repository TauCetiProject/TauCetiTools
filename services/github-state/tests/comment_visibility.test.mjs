import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHmac} from 'node:crypto';
import {runtime,repository,send} from './runtime.mjs';
import {fixture} from './pr_refresh_fixture.mjs';

const comment=(body,updated='2026-11-08T23:36:00Z')=>({id:'4508278248',body,created_at:'2026-11-08T23:35:00Z',updated_at:updated,user:{login:'reviewer'}});
const query=operation=>({repository:repository.name,operation,args:{number:7}});
const workerExtension=`{...worker,async fetch(req,env,ctx){const pending=[];const response=await worker.fetch(req,env,{waitUntil:p=>pending.push(p)});await Promise.all(pending);return response;}}`;
const seed=`async fetch(req){
 if(new URL(req.url).pathname==='/test-seed'){
  await this.atomic(async state=>{for(const operation of ['issue_comments','review_comments'])await state.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)',operation+':{"number":7}',operation,'{"number":7}',1,Date.now(),0,null);});
  return Response.json({ok:true});
 }
 return super.fetch(req);
}`;
async function webhook(mf,event,delivery,action,row){
 const body=JSON.stringify({repository:{id:100},installation:{id:200},issue:{number:7},pull_request:{number:7},action,comment:row});
 const response=await mf.dispatchFetch('https://test/webhook/github',{method:'POST',body,headers:{'x-github-delivery':delivery,'x-github-event':event,'x-hub-signature-256':'sha256='+createHmac('sha256','secret').update(body).digest('hex')}});
 assert.equal(response.status,202);
}

for(const [event,operation] of [['issue_comment','issue_comments'],['pull_request_review_comment','review_comments']])test(`${event}: signed ingress updates ordinary shared queries while the queue is unconsumed`,async()=>{
 let upstream=0;
 const mf=await runtime(async()=>{upstream++;throw Error('unexpected upstream read');},{},{repositoryAlarms:false,repositoryExtension:seed,workerExtension});
 try{
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName(repository.id)),archive=await mf.getR2Bucket('ARCHIVE');
  await stub.fetch('https://internal/test-seed');
  const read=async()=>{
   const result=await (await mf.dispatchFetch('https://test/v1/query?repository=org/repo&operation='+operation+'&args=%7B%22number%22%3A7%7D',{headers:{Authorization:'Bearer client'}})).json();
   assert.equal(result.status,'ok');assert.equal(result.complete,true);return result;
  };
  await webhook(mf,event,'created-'+event.replaceAll('_','-'),'created',comment('created'));
  const first=await read();assert.deepEqual(first.data.map(c=>c.body),['created']);
  await webhook(mf,event,'edited-'+event.replaceAll('_','-'),'edited',comment('edited','2026-11-08T23:37:00Z'));
  const edited=await read();assert.deepEqual(edited.data.map(c=>c.body),['edited']);assert.ok(edited.revision>first.revision);
  // Queue redelivery after immediate application must use the committed receipt.
  const raw=(await archive.list({prefix:'webhooks/'})).objects.find(o=>o.key.endsWith('/edited-'+event.replaceAll('_','-')+'.json'));
  assert.equal((await send(stub,'/event',{repository:repository.name,key:raw.key,delivery:'edited-'+event.replaceAll('_','-'),event,received:Date.now()})).status,200);
  assert.equal((await read()).revision,edited.revision);
  await webhook(mf,event,'deleted-'+event.replaceAll('_','-'),'deleted',comment('edited','2026-11-08T23:37:00Z'));
  const deleted=await read();assert.deepEqual(deleted.data,[]);assert.ok(deleted.revision>edited.revision);
  assert.ok(await archive.head('receipts/100/deleted-'+event.replaceAll('_','-')+'.json'));
  assert.equal((await archive.list({prefix:'parked/100/pointers/'})).objects.length,0);
  assert.equal(upstream,0);
 }finally{await mf.dispose();}
});

for(const operation of ['issue_comments','review_comments','review_threads'])test(`${operation}: an older in-flight refresh cannot erase comment creates, edits or deletes`,async()=>{
 const f=await fixture(),now=Date.now;try{
  const flat=operation==='review_threads'?'review_comments':operation,q=query(operation),key=operation+':{"number":7}';
  const initial=comment('old');
  await f.state.put(flat,initial.id,initial,Date.now()-1000,false,'7');
  await f.state.membership(flat+':{"number":7}',initial.id,true);
  await f.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)',flat+':{"number":7}',flat,'{"number":7}',1,1,1,null);
  const removed={...initial,id:'4508278250'};
  await f.state.put(flat,removed.id,removed,Date.now()-1000,false,'7');
  await f.state.membership(flat+':{"number":7}',removed.id,true);
  await f.state.enqueue(q);
  // Exercise the ambiguous same-millisecond boundary deterministically.
  const fixed=Date.now()+10;Date.now=()=>fixed;
  let enter,release;const ready=new Promise(r=>enter=r),gate=new Promise(r=>release=r);
  const stale=operation==='review_threads'?[{id:'thread',comments:{nodes:[initial,removed]}}]:[initial,removed];
  f.state.env={...f.state.env,INSTALLATIONS:{idFromName:id=>id,get:()=>({fetch:async()=>{enter();await gate;return Response.json({api:'core',data:stale});}})}};
  const refresh=f.state.refresh((await f.store.exec('SELECT * FROM jobs WHERE key=?',key)).one());await ready;
  for(const [id,action,row] of [['edit','edited',comment('new')],['create','created',{...comment('second'),id:'4508278249'}],['delete','deleted',{...comment('deleted'),id:'4508278250'}]]){
   const event=flat==='issue_comments'?'issue_comment':'pull_request_review_comment';
   f.objects.set(id,JSON.stringify({action,issue:{number:7},pull_request:{number:7},comment:row}));
   await f.state.ingest({repository:repository.name,key:id,delivery:id,event,received:Date.now()+1});
  }
  release();await refresh;
  const rows=await f.state.rows(query(flat));assert.deepEqual(rows.map(c=>[c.id,c.body]),[['4508278248','new'],['4508278249','second']]);
 }finally{Date.now=now;await f.close();}
});

test('failed immediate application leaves durable input for the existing queue',async()=>{
 const mf=await runtime(async()=>{throw Error('unexpected upstream');},{},{repositoryAlarms:false,workerExtension,repositoryExtension:`async fetch(req){if(new URL(req.url).pathname==='/event')return Response.json({error:'temporarily_unavailable'},{status:503});return super.fetch(req);}`});
 try{
  await webhook(mf,'issue_comment','deferred-comment','created',comment('retained'));
  const archive=await mf.getR2Bucket('ARCHIVE');assert.ok(await archive.head('parked/100/bodies/deferred-comment.json'));
  assert.equal((await archive.list({prefix:'parked/100/pointers/'})).objects.length,1);
  assert.equal(await archive.head('receipts/100/deferred-comment.json'),null);
 }finally{await mf.dispose();}
});

test('a delayed immediate application does not delay the webhook acknowledgement',async()=>{
 let release,entered;const ready=new Promise(r=>entered=r),gate=new Promise(r=>release=r);
 const mf=await runtime(async(req,url)=>{assert.equal(url.hostname,'comment.test');entered();await gate;return new Response('ready');},{},{repositoryAlarms:false,repositoryExtension:`async fetch(req){if(new URL(req.url).pathname==='/event')await fetch('https://comment.test/gate');return super.fetch(req);}`});
 let timer;
 try{
  await Promise.race([webhook(mf,'issue_comment','slow-comment','created',comment('retained')),
   new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('webhook acknowledgement waited for repository application')),5000);})]);
  clearTimeout(timer);await ready;
  const archive=await mf.getR2Bucket('ARCHIVE');assert.ok(await archive.head('parked/100/bodies/slow-comment.json'));
  assert.equal(await archive.head('receipts/100/slow-comment.json'),null);
 }finally{clearTimeout(timer);release();await mf.dispose();}
});
