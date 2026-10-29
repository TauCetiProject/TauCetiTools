import {test} from 'node:test';
import assert from 'node:assert/strict';
import {runtime,repository,send} from './runtime.mjs';
const extension=`async fetch(req){
 const path=new URL(req.url).pathname;
 if(path==='/fixture/seed'){
  await this.setMeta('repository',${JSON.stringify(repository)});
  const now=Date.now();
  for(const [operation,args] of [['issue_comments',{number:7}],['open_prs',{}]]){
   const key=operation+':'+JSON.stringify(args);
   await this.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)',key,operation,JSON.stringify(args),1,now,0,null);
  }
  for(let id=1;id<=601;id++){
   await this.store.exec('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)','issue_comments',String(id),'7',JSON.stringify({id,body:'mirrored'}),'',now,0,0);
   await this.store.exec('INSERT INTO members VALUES(?,?)','issue_comments:{"number":7}',String(id));
  }
  await this.store.exec('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)','pr','7','',JSON.stringify({number:7,state:'OPEN',headRefOid:'a'.repeat(40),labels:[{name:'needs-rebase'}],author:{login:'peer'}}),'',now,0,0);
  await this.store.exec('INSERT INTO members VALUES(?,?)','open_prs:{}','7');
  return Response.json({ok:true});
 }
 if(path==='/fixture/counts'){
  const counts={};for(const table of ['jobs','demand','survey_dirty','survey_state','collections','resources'])counts[table]=(await this.store.exec('SELECT COUNT(*) n FROM '+table)).one().n;
  return Response.json(counts);
 }
 return super.fetch(req);
}`;
test('anonymous real repository queries never schedule hydration or create cursor blobs',async()=>{
 let upstream=0;
 const mf=await runtime(async()=>{upstream++;throw Error('unexpected upstream request');},{PUBLIC_READ_REPOSITORIES:'["org/repo"]'},{repositoryAlarms:false,repositoryExtension:extension});
 try {
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName(repository.id)),archive=await mf.getR2Bucket('ARCHIVE');
  await send(stub,'/fixture/seed',{});
  const counts=async()=>await (await send(stub,'/fixture/counts',{})).json();
  const before=await counts(),blobsBefore=(await archive.list()).objects;
  const query=(operation,args={},headers={})=>mf.dispatchFetch('https://test/v1/query?repository=org/repo&operation='+operation+'&args='+encodeURIComponent(JSON.stringify(args)),{headers});
  const first=await query('issue_comments',{number:7}),etag=first.headers.get('etag'),payload=await first.json();
  assert.equal(payload.status,'ok');assert.equal(payload.data.length,601);assert.equal(payload.cursor,undefined);
  assert.equal((await (await query('issue_comments',{number:7})).json()).data.length,601);
  assert.equal((await query('issue_comments',{number:7},{'If-None-Match':etag})).status,304);
  for(const [operation,args] of [['issue_comments',{number:999}],['review_threads',{number:999}],['statuses',{sha:'b'.repeat(40)}],['progress',{number:7}],['survey',{stages:'rebase'}]]){
   assert.equal((await query(operation,args,{'x-query-readonly':'0'})).status,200);
  }
  const cursor=await mf.dispatchFetch('https://test/v1/query?repository=org/repo&operation=issue_comments&args=%7B%22number%22%3A7%7D&cursor='+encodeURIComponent(crypto.randomUUID()+':500'));
  assert.equal((await cursor.json()).reason,'public_cursor_unsupported');
  assert.deepEqual(await counts(),before,'anonymous reads cannot persist demand, jobs or projections');
  assert.deepEqual((await archive.list()).objects,blobsBefore,'anonymous reads cannot create cursor objects');
  assert.equal(upstream,0);
  const authenticated=await (await query('issue_comments',{number:7},{Authorization:'Bearer client'})).json();
  assert.ok(authenticated.cursor,'authenticated reads retain independent paginated snapshots');
  assert.ok((await counts()).demand>before.demand,'authenticated clients retain background demand');
 } finally {await mf.dispose();}
});
