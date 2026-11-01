import {test} from 'node:test';
import assert from 'node:assert/strict';
import {fixture} from './pr_refresh_fixture.mjs';
const old='a'.repeat(40),head='b'.repeat(40),latest='c'.repeat(40);
const q={repository:'org/repo',operation:'pr',args:{number:7}},key='pr:{"number":7}';
const record=(sha=head,updatedAt='2026-11-01T09:23:46Z')=>({number:7,headRefOid:sha,baseRefOid:'d'.repeat(40),state:'OPEN',isDraft:false,updatedAt,labels:[]});
async function seed(f){await f.state.put('pr','7',record(old,'2026-11-01T09:59:13Z'),Date.now()-1000);await f.state.enqueue(q);}
function upstream(f,read){f.state.env={...f.state.env,INSTALLATIONS:{idFromName:id=>id,get:()=>({fetch:async(_url,init)=>Response.json(await read(JSON.parse(init.body)))})}};}
async function event(f,action='labeled',sha=latest){const id=crypto.randomUUID(),body={action,label:{name:'new-label'},pull_request:{number:7,title:'title',state:'open',updated_at:'2026-11-01T10:00:00Z',head:{sha},base:{sha:'d'.repeat(40)},labels:[{name:'new-label'}]}};f.objects.set(id,JSON.stringify(body));await f.state.ingest({repository:'org/repo',key:id,delivery:id,event:'pull_request',received:Date.now()+1});}

test('background full read repairs poisoned timestamps and validators, and dirties dependent files',async()=>{
 const f=await fixture();try{
  await seed(f);await f.store.exec('UPDATE jobs SET etag=?,started=? WHERE key=?','poison',Date.now()-3600000,key);
  const files='files:{"number":7}';await f.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)',files,'files','{"number":7}',1,1,1,'old-files');
  let request;upstream(f,async input=>{request=input;return input.etag?{unchanged:true}:{data:record(),api:'core',etag:'correct'};});
  const job=(await f.store.exec('SELECT * FROM jobs WHERE key=?',key)).one();await f.state.refresh(job);
  assert.equal(request.etag,null);assert.equal((await f.state.rows(q)).headRefOid,head);
  assert.equal((await f.store.exec('SELECT complete FROM collections WHERE key=?',files)).one().complete,0);
  assert.equal((await f.store.exec('SELECT etag FROM jobs WHERE key=?',key)).one().etag,'correct');
  assert.ok((await f.store.exec('SELECT key FROM jobs WHERE key=?','statuses:{"sha":"'+head+'"}')).toArray().length);
  assert.equal((await f.store.exec("SELECT id FROM resources WHERE kind='pr' AND id=?",key)).toArray().length,0);
 }finally{await f.close();}
});

test('label and lifecycle snapshots never replace unrelated head fields',async()=>{
 const f=await fixture();try{await seed(f);await event(f);const row=JSON.parse((await f.store.exec("SELECT data FROM resources WHERE kind='pr' AND id='7'")).one().data);assert.equal(row.headRefOid,old);assert.deepEqual(row.labels,[{name:'new-label'}]);}
 finally{await f.close();}
});

test('one synchronous read coalesces callers, commits before success and skips obsolete reports',async()=>{
 const f=await fixture();try{
  await seed(f);let release,entered;const ready=new Promise(r=>entered=r),gate=new Promise(r=>release=r);let reads=0;
  upstream(f,async()=>{reads++;entered();await gate;return {data:record(),api:'core'};});
  const input={repository:'org/repo',number:7,expected_head:old};const calls=Array.from({length:20},()=>f.state.refreshPR(input));await ready;
  // A different write remains possible during the HTTP wait.
  await f.state.put('issue','123',{number:123,state:'OPEN'});
  release();const results=await Promise.all(calls);assert.equal(reads,1);assert.ok(results.every(r=>r.outcome==='refreshed'));
  assert.equal((await f.state.rows(q)).headRefOid,head);
  assert.equal((await f.state.refreshPR(input)).outcome,'already-changed');assert.equal(reads,1);
 }finally{await f.close();}
});

test('newer input supersedes a full read without verification, and ordinary retry converges',async()=>{
 const f=await fixture();try{
  await seed(f);let first=true;upstream(f,async()=>{if(first){first=false;await event(f,'synchronize',latest);}return {data:record(),api:'core',etag:'bad-validator'};});
  let job=(await f.store.exec('SELECT * FROM jobs WHERE key=?',key)).one();await f.state.refresh(job);
  assert.equal((await f.store.exec('SELECT verified FROM collections WHERE key=?',key)).toArray().length,0);
  job=(await f.store.exec('SELECT * FROM jobs WHERE key=?',key)).one();assert.equal(job.started,null);assert.equal(job.etag,null);assert.equal(job.dirty,1);
  await f.state.refresh(job);assert.equal((await f.state.rows(q)).headRefOid,head);
 }finally{await f.close();}
});

test('an in-flight 304 cannot verify a webhook-modified record',async()=>{
 const f=await fixture();try{
  await seed(f);await f.state.setMeta('pr_validator_version',1);
  await f.store.exec('UPDATE jobs SET etag=? WHERE key=?','old-validator',key);
  await f.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)',key,'pr','{"number":7}',1,10,1,'old-validator');
  upstream(f,async input=>{assert.equal(input.etag,'old-validator');await event(f);return {unchanged:true,api:'core'};});
  await f.state.refresh((await f.store.exec('SELECT * FROM jobs WHERE key=?',key)).one());
  assert.equal((await f.store.exec('SELECT verified FROM collections WHERE key=?',key)).one().verified,10);
  const job=(await f.store.exec('SELECT * FROM jobs WHERE key=?',key)).one();assert.equal(job.etag,null);assert.equal(job.started,null);assert.equal(job.dirty,1);
 }finally{await f.close();}
});

test('refresh quota deferral is local response data and a failed flight can recover',async()=>{
 const f=await fixture();try{
  await seed(f);const retry=Date.now()+3600000;upstream(f,async()=>({deferred:true,category:'primary:core',retry_at:retry}));
  const input={repository:'org/repo',number:7,expected_head:old};const failed=await f.state.refreshPR(input);assert.equal(failed.status,'unavailable');assert.equal(failed.retry_at,retry);
  assert.equal((await f.store.exec('SELECT dirty FROM jobs WHERE key=?',key)).one().dirty,1);
  upstream(f,async()=>({data:record(),api:'core'}));assert.equal((await f.state.refreshPR(input)).status,'ok');
 }finally{await f.close();}
});

test('failed correction rolls back publication, retains recovery, and retries without acknowledgement',async()=>{
 const f=await fixture();try{
  await seed(f);upstream(f,async()=>({data:record(),api:'core'}));
  const publish=f.state.publishPR;
  f.state.publishPR=async function(...args){await publish.apply(this,args);throw Error('publication failed before commit');};
  const input={repository:'org/repo',number:7,expected_head:old};
  assert.equal((await f.state.refreshPR(input)).status,'unavailable');
  assert.equal(JSON.parse((await f.store.exec("SELECT data FROM resources WHERE kind='pr' AND id='7'")).one().data).headRefOid,old);
  assert.equal((await f.store.exec('SELECT dirty FROM jobs WHERE key=?',key)).one().dirty,1);
  f.state.publishPR=publish;
  assert.equal((await f.state.refreshPR(input)).status,'ok');assert.equal((await f.state.rows(q)).headRefOid,head);
 }finally{await f.close();}
});

test('successful repair invalidates the aggregate query grace cache',async()=>{
 const f=await fixture();try{
  await seed(f);await f.state.membership('open_prs:{}','7',true);
  await f.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)','open_prs:{}','open_prs','{}',1,1,1,null);
  const request=()=>f.state.fetch(new Request('https://internal/query',{method:'POST',body:JSON.stringify({repository:'org/repo',operation:'open_prs'})}));
  assert.equal((await (await request()).json()).data[0].headRefOid,old);
  upstream(f,async()=>({data:record(),api:'core'}));
  assert.equal((await f.state.refreshPR({repository:'org/repo',number:7,expected_head:old})).status,'ok');
  assert.equal((await (await request()).json()).data[0].headRefOid,head);
 }finally{await f.close();}
});

test('a response timeout does not abandon publication or require a client acknowledgement',async()=>{
 const f=await fixture();const timeout=globalThis.setTimeout;try{
  await seed(f);let release,entered;const ready=new Promise(r=>entered=r),gate=new Promise(r=>release=r);
  upstream(f,async()=>{entered();await gate;return {data:record(),api:'core'};});
  globalThis.setTimeout=(fn,delay,...args)=>timeout(fn,delay===15000 && String(fn).includes('refresh_timeout')?10:delay,...args);
  const input={repository:'org/repo',number:7,expected_head:old},request=f.state.refreshPR(input);await ready;
  assert.equal((await request).reason,'refresh_timeout');assert.equal((await f.store.exec('SELECT dirty FROM jobs WHERE key=?',key)).one().dirty,1);
  const pending=f.state.prRefreshes.get(7);release();await pending;
  assert.equal((await f.state.rows(q)).headRefOid,head);assert.equal((await f.state.refreshPR(input)).outcome,'already-changed');
 }finally{globalThis.setTimeout=timeout;await f.close();}
});
