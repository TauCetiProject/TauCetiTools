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
  for(let offset=1;offset<=601;offset+=12){
   const ids=Array.from({length:Math.min(12,602-offset)},(_,i)=>String(offset+i));
   await this.store.exec('INSERT INTO resources VALUES '+ids.map(()=>'(?,?,?,?,?,?,?,?)').join(','),...ids.flatMap(id=>['issue_comments',id,'7',JSON.stringify({id:Number(id),body:'mirrored'+'x'.repeat(8192)}),'',now,0,0]));
   await this.store.exec('INSERT INTO members VALUES '+ids.map(()=>'(?,?)').join(','),...ids.flatMap(id=>['issue_comments:{"number":7}',id]));
  }
  await this.store.exec('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)','pr','7','',JSON.stringify({number:7,state:'OPEN',headRefOid:'a'.repeat(40),labels:[{name:'needs-rebase'}],author:{login:'peer'}}),'',now,0,0);
  await this.store.exec('INSERT INTO members VALUES(?,?)','open_prs:{}','7');
  return Response.json({ok:true});
 }
 if(path==='/fixture/evict'){this.publicCompletedQueries.clear();return Response.json({ok:true});}
 if(path==='/fixture/jobs')return Response.json((await this.store.exec('SELECT query FROM jobs')).toArray().map(r=>JSON.parse(r.query)));
 if(path==='/fixture/counts'){
  const counts={};for(const table of ['jobs','demand','survey_dirty','survey_state','collections','resources'])counts[table]=(await this.store.exec('SELECT COUNT(*) n FROM '+table)).one().n;
  return Response.json(counts);
 }
 return super.fetch(req);
}`;
test('large public snapshots paginate without hydration, SQL mutation or upstream requests',async()=>{
 let upstream=0;
 const mf=await runtime(async()=>{upstream++;throw Error('unexpected upstream request');},{PUBLIC_READ_REPOSITORIES:'["org/repo"]'},{repositoryAlarms:false,repositoryExtension:extension});
 try {
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName(repository.id)),archive=await mf.getR2Bucket('ARCHIVE');
  await send(stub,'/fixture/seed',{});
  const counts=async()=>await (await send(stub,'/fixture/counts',{})).json();
  const before=await counts();
  const query=(operation,args={},headers={})=>mf.dispatchFetch('https://test/v1/query?repository=org/repo&operation='+operation+'&args='+encodeURIComponent(JSON.stringify(args)),{headers});
  const first=await query('issue_comments',{number:7}),etag=first.headers.get('etag'),payload=await first.json();
  assert.equal(payload.status,'ok');assert.ok(payload.cursor,'a snapshot over 4 MiB remains available');
  assert.ok(payload.data.length<500,'continuation pages are byte bounded');
  assert.deepEqual(await (await query('issue_comments',{number:7})).json(),payload,'identical reads reuse one snapshot');
  assert.equal((await query('issue_comments',{number:7},{'If-None-Match':etag})).status,304);
  const cursors=(await archive.list({prefix:'cursors/'})).objects;
  assert.ok(cursors.length>1,'large responses use temporary continuation pages');
  assert.ok(cursors.every(o=>o.size<1500000),'each stored page is byte bounded');
  assert.deepEqual(await counts(),before,'initial snapshots never mutate SQL');
  const rows=[...payload.data];let next=payload.cursor;
  // A later webhook edit must not change the already published snapshot.
  await archive.put('fixture/changed',JSON.stringify({action:'edited',issue:{number:7},comment:{id:601,body:'edited later',updated_at:'2030-01-01T00:00:00Z',user:{login:'peer'}}}));
  await send(stub,'/event',{key:'fixture/changed',delivery:'changed',event:'issue_comment',repository:repository.name,received:Date.now()});
  const afterWebhook=await counts(),blobsAfterWebhook=(await archive.list()).objects;
  await send(stub,'/fixture/evict',{});
  while(next){
   const r=await mf.dispatchFetch('https://test/v1/query?repository=org/repo&operation=issue_comments&args=%7B%22number%22%3A7%7D&cursor='+encodeURIComponent(next));
   assert.equal(r.status,200);const page=await r.json();assert.equal(page.status,'ok');assert.equal(page.revision,payload.revision);
   rows.push(...page.data);next=page.cursor;
  }
  assert.equal(rows.length,601);assert.equal(new Set(rows.map(r=>r.id)).size,601);
  assert.ok(rows.at(-1).body.startsWith('mirrored'),'continuation retains its original data after webhook edits');
  assert.deepEqual(await counts(),afterWebhook,'continuations never renew demand or schedule hydration');
  for(const [operation,args] of [['issue_comments',{number:999}],['review_threads',{number:999}],['statuses',{sha:'b'.repeat(40)}],['progress',{number:7}],['survey',{stages:'rebase'}]]){
   const response=await query(operation,args,{'x-query-readonly':'0'});assert.equal(response.status,200);
   assert.notEqual((await response.json()).reason,'public_read_busy');
  }
  const cursor=await mf.dispatchFetch('https://test/v1/query?repository=org/repo&operation=issue_comments&args=%7B%22number%22%3A7%7D&cursor='+encodeURIComponent(crypto.randomUUID()+':500'));
  assert.equal((await cursor.json()).reason,'cursor_expired');
  const batch=await mf.dispatchFetch('https://test/v1/query_batch',{method:'POST',body:JSON.stringify({queries:[{repository:'org/repo',operation:'issue_comments',args:{number:7}},{repository:'org/repo',operation:'review_threads',args:{number:999}}]})});
  assert.deepEqual((await batch.json()).results.map(r=>r.result.status),['ok','unavailable']);
  assert.deepEqual(await counts(),afterWebhook,'anonymous reads cannot persist demand, jobs or projections');
  const additions=(await archive.list()).objects.filter(o=>!blobsAfterWebhook.some(b=>b.key===o.key));
  assert.ok(additions.every(o=>o.key.startsWith('cursors/') || o.key.startsWith('fixture/')),'public reads write only temporary cursors');
  assert.equal(upstream,0);
  const authenticated=await (await query('issue_comments',{number:7},{Authorization:'Bearer client'})).json();
  assert.ok(authenticated.cursor,'authenticated reads retain independent paginated snapshots');
  const deadline=Date.now()+5000;
  while((await counts()).demand<=afterWebhook.demand && Date.now()<deadline)await new Promise(resolve=>setTimeout(resolve,100));
  assert.ok((await counts()).demand>afterWebhook.demand,'authenticated clients retain background demand');
  await archive.put('fixture/opened',JSON.stringify({action:'opened',pull_request:{id:8,number:8,state:'open',head:{sha:'c'.repeat(40)},user:{login:'peer'}}}));
  await send(stub,'/event',{key:'fixture/opened',delivery:'opened',event:'pull_request',repository:repository.name,received:Date.now()});
  const jobs=await (await send(stub,'/fixture/jobs',{})).json();
  for(const operation of ['issue_comments','review_threads'])assert.ok(jobs.some(q=>q.operation===operation && q.args?.number===8),'trusted new PR webhooks schedule histories without anonymous demand');
 } finally {await mf.dispose();}
});

test('a fleet burst coalesces matching reads without refusing distinct public queries',async()=>{
 let builds=0;
 const mf=await runtime(async(_req,url)=>{
  assert.equal(url.hostname,'snapshot.test');builds++;
  await new Promise(resolve=>setTimeout(resolve,50));return new Response('ready');
 },{PUBLIC_READ_REPOSITORIES:'["org/repo"]'},{repositoryAlarms:false,repositoryExtension:`
 async query(q){
  await fetch('https://snapshot.test/build');
  return {version:1,repository:q.repository,operation:q.operation,status:'ok',complete:true,revision:await this.revision(),data:[]};
 }`});
 try {
  const read=number=>mf.dispatchFetch('https://test/v1/query?repository=org/repo&operation=issue_comments&args='+encodeURIComponent(JSON.stringify({number}))).then(r=>r.json());
  const results=await Promise.all(Array.from({length:14},(_,i)=>read(i%7+1)));
  assert.ok(results.every(r=>r.status==='ok'),'seven distinct worker queries need no public build-slot refusal');
  assert.equal(builds,7,'matching in-flight reads share one preparation');
  await read(1);assert.equal(builds,7,'normal per-PR traffic does not evict the common fleet snapshot after two reads');
 } finally {await mf.dispose();}
});
