import {test} from 'node:test';
import assert from 'node:assert/strict';
import {runtime,repository,response,send} from './runtime.mjs';

test('installation owns token, measures batch charge, preserves huge IDs',async()=>{
 let network=0;
 const mf=await runtime(async req=>{
  assert.equal(req.headers.get('authorization'),'Bearer installation-read-token');network++;
  const payload=await req.json();assert.match(payload.query,/rateLimit\{cost remaining resetAt\}/);
  return response({data:{rateLimit:{cost:1,remaining:4999,resetAt:new Date(Date.now()+300000).toISOString()},repository:{q0:{comments:{nodes:[{id:'node',fullDatabaseId:'18446744073709551600',body:'x',createdAt:'now',updatedAt:'now',authorAssociation:'CONTRIBUTOR',author:{login:'review-bot',__typename:'Bot'}}],pageInfo:{hasNextPage:false,endCursor:null}}}}}});
 });
 try{
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName('200'));
  const r=await send(stub,'/batch',{repository,jobs:[{key:'k',query:{operation:'issue_comments',args:{number:7}}}]});
  assert.equal(r.status,200);const data=await r.json();assert.equal(data.results[0].data[0].id,'18446744073709551600');assert.equal(data.results[0].data[0].user.login,'review-bot[bot]');
  const health=await (await stub.fetch('https://internal/health')).json();assert.equal(health.hour.find(x=>x.operation==='issue_comments_batch').points,1);assert.equal(network,1);
 }finally{await mf.dispose();}
});
test('hundred-node reaction batches isolate an unavailable alias',async()=>{
 const mf=await runtime(async req=>{
  const payload=await req.json();assert.equal([...payload.query.matchAll(/q\d+:node/g)].length,100);
  const data={rateLimit:{cost:1,remaining:4999,resetAt:new Date(Date.now()+300000).toISOString()}};
  for(let i=0;i<100;i++)data['q'+i]={reactions:{nodes:[],pageInfo:{hasNextPage:false,endCursor:null}}};
  data.q17=null;
  return response({data,errors:[{type:'NOT_FOUND',path:['q17'],message:'Node no longer available'}]});
 });
 try{
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName('200'));
  const jobs=Array.from({length:100},(_,i)=>({key:String(i),node_id:'node_'+i,query:{operation:'reactions',args:{id:String(i+1)}}}));
  const result=await (await send(stub,'/batch',{repository,jobs})).json();
  assert.equal(result.results.length,100);assert.equal(result.results[17].unavailable,true);
  assert.equal(result.results.filter(r=>r.data).length,99);
 }finally{await mf.dispose();}
});
test('REST numeric identifiers retain their original digits',async()=>{
 const mf=await runtime(async()=>new Response('[{"id":18446744073709551600,"node_id":"node","body":"large identifier","created_at":"2026-10-12T00:00:00Z","updated_at":"2026-10-12T00:00:00Z","user":{"login":"peer"}}]',{
  headers:{'content-type':'application/json','x-ratelimit-resource':'core','x-ratelimit-remaining':'4999','x-ratelimit-limit':'5000','x-ratelimit-reset':String(Math.floor(Date.now()/1000)+300)}
 }));
 try{
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName('200'));
  const result=await (await send(stub,'/read',{repository,query:{operation:'issue_comments',args:{number:7}},api:'core',page:1})).json();
  assert.equal(result.api,'core');assert.equal(result.data[0].id,'18446744073709551600');
 }finally{await mf.dispose();}
});
test('REST exhaustion swaps history to GraphQL and retains complete results',async()=>{
 let calls=[];
 const mf=await runtime(async(req,u)=>{
  calls.push(u.pathname);
  if(u.pathname!=='/graphql')return response({message:'API rate limit exceeded'},'core',0,403);
  return response({data:{rateLimit:{cost:1,remaining:4999,resetAt:new Date(Date.now()+300000).toISOString()},repository:{issueOrPullRequest:{comments:{nodes:[],pageInfo:{hasNextPage:false,endCursor:null}}}}}});
 });
 try{
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName('200'));
  const data=await (await send(stub,'/read',{repository,query:{operation:'issue_comments',args:{number:7}},page:1})).json();assert.equal(data.api,'graphql');assert.deepEqual(data.data,[]);
  const bad=await (await send(stub,'/read',{repository,query:{operation:'files',args:{number:7}},page:1})).json();assert.equal(bad.deferred,true);
  const next=await (await send(stub,'/read',{repository,query:{operation:'issue_comments',args:{number:7}},page:1})).json();assert.equal(next.api,'graphql');
  assert.equal(calls.filter(p=>p.endsWith('/files')).length,1);
 }finally{await mf.dispose();}
});
test('mid-pagination exhaustion restarts in the other pool without mixing cursors',async()=>{
 let calls=[];
 const mf=await runtime(async(req,u)=>{
  calls.push(u.pathname);
  if(u.pathname!=='/graphql')return response({message:'API rate limit exceeded'},'core',0,403);
  const payload=await req.json();assert.equal(payload.variables.cursor,undefined);
  return response({data:{rateLimit:{cost:1,remaining:4999,resetAt:new Date(Date.now()+300000).toISOString()},repository:{issueOrPullRequest:{comments:{nodes:[],pageInfo:{hasNextPage:false,endCursor:null}}}}}});
 });
 try{
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName('200'));
  const result=await (await send(stub,'/read',{repository,query:{operation:'issue_comments',args:{number:7}},api:'core',page:2,cursor:'https://api.github.com/repos/org/repo/issues/7/comments?page=2'})).json();
  assert.deepEqual(result,{restart:true,api:'graphql'});
  assert.equal(calls.length,1);
  const complete=await (await send(stub,'/read',{repository,query:{operation:'issue_comments',args:{number:7}},api:result.api,page:1})).json();
  assert.equal(complete.api,'graphql');assert.deepEqual(complete.data,[]);
 }finally{await mf.dispose();}
});
for(const quotaType of ['RATE_LIMITED','RATE_LIMIT']) test('GraphQL HTTP-200 '+quotaType+' defers its pool; 401 renews token once',async()=>{
 let attempts=0;
 const mf=await runtime(async(req,u)=>{
  attempts++;
  if(attempts===1)return response({message:'bad token'},'graphql',5000,401);
  if(u.pathname==='/graphql')return response({errors:[{type:quotaType,message:'API rate limit already exceeded'}]},'graphql',0,200);
  return response([], 'core',4999);
 });
 try{
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName('200'));
  const result=await (await send(stub,'/read',{repository,query:{operation:'issue_comments',args:{number:7}},page:1})).json();
  assert.equal(result.api,'core');assert.deepEqual(result.data,[]);assert.equal(attempts,3);
 }finally{await mf.dispose();}
});
for(const remaining of [100,0]) test('secondary limit pauses both pools even with '+remaining+' primary points',async()=>{
 let calls=0;
 const mf=await runtime(async()=>{calls++;return Response.json({message:'secondary rate limit: content creation'},{status:429,headers:{'retry-after':'120','x-ratelimit-resource':'graphql','x-ratelimit-remaining':String(remaining)}});});
 try{
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName('200'));
  const a=await (await send(stub,'/read',{repository,query:{operation:'issue_comments',args:{number:7}},page:1})).json();assert.equal(a.deferred,true);
  const b=await (await send(stub,'/read',{repository,query:{operation:'files',args:{number:7}},page:1})).json();assert.equal(b.deferred,true);assert.equal(calls,1);
  const health=await (await stub.fetch('https://internal/health')).json();assert.equal(health.quota_types[0].category,'secondary:content');
 }finally{await mf.dispose();}
});
for(const offset of [-10,60]) test('late primary rejection respects its original reset '+offset,async()=>{
 let entered,release,calls=0;
 const started=new Promise(resolve=>{entered=resolve;}),gate=new Promise(resolve=>{release=resolve;});
 const oldReset=Math.floor(Date.now()/1000)+offset,newReset=Math.floor(Date.now()/1000)+3600;
 const mf=await runtime(async(req,u)=>{
  calls++;
  if(u.pathname.includes('/pulls/7/')) {
   entered();await gate;
   return Response.json({message:'API rate limit exceeded'},{status:403,headers:{'x-ratelimit-resource':'core','x-ratelimit-remaining':'0','x-ratelimit-limit':'5000','x-ratelimit-reset':String(oldReset)}});
  }
  return Response.json([],{headers:{'x-ratelimit-resource':'core','x-ratelimit-remaining':'4999','x-ratelimit-limit':'5000','x-ratelimit-reset':String(newReset)}});
 });
 try{
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName('200'));
  const pending=send(stub,'/read',{repository,query:{operation:'files',args:{number:7}},page:1});await started;
  const fresh=await (await send(stub,'/read',{repository,query:{operation:'files',args:{number:8}},page:1})).json();assert.deepEqual(fresh.data,[]);
  release();const late=await (await pending).json();assert.equal(late.deferred,true);
  const health=await (await stub.fetch('https://internal/health')).json(),core=health.budgets.find(b=>b.resource==='core');
  assert.equal(core.remaining,4999,'an old rejection cannot exhaust the newer window');
  assert.equal(core.reset,newReset*1000);assert.ok(late.retry_at<=Math.max(Date.now()+2000,oldReset*1000));
  const next=await (await send(stub,'/read',{repository,query:{operation:'files',args:{number:9}},page:1})).json();
  if(offset<0) {assert.deepEqual(next.data,[]);assert.equal(calls,3);}
  else {assert.equal(next.deferred,true);assert.ok(next.retry_at<=oldReset*1000+1000);assert.equal(calls,2);}
 }finally{release();await mf.dispose();}
});
test('delivery audit follows busy-repository pages and redelivers failures beyond page one',async()=>{
 const paths=[];
 const mf=await runtime(async(req,u)=>{
  paths.push(u.pathname+u.search);
  if(u.pathname.endsWith('/attempts'))return response({},'app',4999,202);
  const row={delivered_at:new Date().toISOString(),status_code:202};
  if(!u.searchParams.has('cursor'))return new Response(JSON.stringify(Array.from({length:100},(_,i)=>({...row,id:i+1}))),{
   headers:{'content-type':'application/json',link:'<https://api.github.com/app/hook/deliveries?per_page=100&cursor=older>; rel="next"'}
  });
  return response([{...row,id:101,status_code:500}],'app');
 });
 try{
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName('200'));
  assert.equal((await send(stub,'/audit',repository)).status,200);
  assert.equal(paths.filter(p=>p.includes('cursor=older')).length,1);
  assert.equal(paths.filter(p=>p.endsWith('/101/attempts')).length,1);
  await send(stub,'/audit',repository);
  assert.equal(paths.length,3,'completed audit does not poll again before its interval');
 }finally{await mf.dispose();}
});
test('one rejected redelivery cannot hide subsequent failed deliveries',async()=>{
 const attempts=[];
 const mf=await runtime(async(req,u)=>{
  if(u.pathname.endsWith('/attempts')) {attempts.push(u.pathname);return response({},'app',4999,u.pathname.includes('/101/')?404:202);}
  return response([101,102].map(id=>({id,delivered_at:new Date().toISOString(),status_code:500})),'app');
 });
 try {
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName('200'));
  assert.equal((await send(stub,'/audit',repository)).status,200);
  assert.deepEqual(attempts,['/app/hook/deliveries/101/attempts','/app/hook/deliveries/102/attempts']);
  await send(stub,'/audit',repository);assert.equal(attempts.length,2);
 }finally {await mf.dispose();}
});

test('redelivery auth errors are visible and deferred without hiding other gaps',async()=>{
 const attempts=[];
 const mf=await runtime(async(req,u)=>{
  if(u.pathname.endsWith('/attempts')) {attempts.push(u.pathname);return response({},'app',4999,u.pathname.includes('/101/')?401:202);}
  return response([101,102].map(id=>({id,delivered_at:new Date().toISOString(),status_code:500})),'app');
 });
 try {
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName('200'));
  assert.equal((await send(stub,'/audit',repository)).status,200);
  assert.deepEqual(attempts,['/app/hook/deliveries/101/attempts','/app/hook/deliveries/102/attempts']);
  const health=await (await stub.fetch('https://internal/health')).json();assert.equal(health.redelivery_failure.status,401);
  await send(stub,'/audit',repository);assert.equal(attempts.length,2);
 }finally {await mf.dispose();}
});

test('scalar GraphQL recipes declare only variables they use',async()=>{
 const mf=await runtime(async(req,u)=>{
  assert.equal(u.pathname,'/graphql');
  const payload=await req.json();
  const declaration=payload.query.match(/^query\((.*?)\)\{/)[1];
  for(const [,name] of declaration.matchAll(/\$(\w+):/g)) assert.match(payload.query.slice(declaration.length+8),new RegExp('\\$'+name+'\\b'));
  assert.equal(Object.hasOwn(payload.variables,'cursor'),false);
  return response({data:{rateLimit:{cost:1,remaining:4999,resetAt:new Date(Date.now()+300000).toISOString()},repository:{pullRequest:{number:7,state:'OPEN',headRefOid:'a'.repeat(40),author:{login:'peer'},labels:{nodes:[]}}}}});
 });
 try{
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName('200'));
  const result=await (await send(stub,'/read',{repository,query:{operation:'pr',args:{number:7}},api:'graphql',page:1})).json();
  assert.equal(result.api,'graphql');assert.equal(result.data.number,7);
 }finally{await mf.dispose();}
});
test('REST commit statuses use the same state spelling as GraphQL',async()=>{
 const mf=await runtime(async()=>response({statuses:[{context:'build',state:'success',created_at:'2026-10-12T00:00:00Z'}]},'core'));
 try{
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName('200'));
  const result=await (await send(stub,'/read',{repository,query:{operation:'statuses',args:{sha:'a'.repeat(40)}},api:'core',page:1})).json();
  assert.deepEqual(result.data,[{context:'build',state:'SUCCESS',startedAt:'2026-10-12T00:00:00Z'}]);
 }finally{await mf.dispose();}
});

test('four requests overlap without blocking health; excess requests defer without spending',async()=>{
 let active=0,maximum=0,calls=0,release;
 const gate=new Promise(resolve=>release=resolve);
 const mf=await runtime(async()=>{calls++;active++;maximum=Math.max(maximum,active);await gate;active--;return response([],'core',4996);});
 try {
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName('200'));
  const pending=Array.from({length:4},(_,i)=>send(stub,'/read',{repository,query:{operation:'files',args:{number:i+1}},page:1}));
  const deadline=Date.now()+5000;while(calls<4 && Date.now()<deadline) await new Promise(r=>setTimeout(r,5));
  assert.equal(calls,4);
  const health=await (await stub.fetch('https://internal/health')).json();
  assert.equal(health.inflight.reduce((n,r)=>n+r.requests,0),4);
  const extra=await (await send(stub,'/read',{repository,query:{operation:'files',args:{number:5}},page:1})).json();
  assert.equal(extra.category,'capacity');assert.equal(calls,4);
  release();await Promise.all(pending);assert.equal(maximum,4);
  const after=await (await stub.fetch('https://internal/health')).json();assert.equal(after.inflight.length,0);
  assert.equal(after.budgets.find(b=>b.resource==='core').remaining,4996);
 } finally {release();await mf.dispose();}
});

test('concurrent reads reserve the last primary point only once',async()=>{
 let calls=0,release;const gate=new Promise(r=>release=r);
 const mf=await runtime(async()=>{calls++;if(calls>1) await gate;return response([],'core',calls===1?1:0);});
 try {
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName('200'));
  await send(stub,'/read',{repository,query:{operation:'files',args:{number:1}},page:1});
  const pending=send(stub,'/read',{repository,query:{operation:'files',args:{number:2}},page:1});
  const deadline=Date.now()+5000;while(calls<2 && Date.now()<deadline) await new Promise(r=>setTimeout(r,5));
  const last=await (await send(stub,'/read',{repository,query:{operation:'files',args:{number:3}},page:1})).json();
  assert.equal(last.deferred,true);assert.equal(last.category,'capacity');assert.ok(last.retry_at<=Date.now()+1000,'in-flight reservations never create an hour-long reset wait');assert.equal(calls,2);release();await pending;
  const exhausted=await (await send(stub,'/read',{repository,query:{operation:'files',args:{number:3}},page:1})).json();
  assert.equal(exhausted.deferred,true);assert.ok(exhausted.retry_at>Date.now()+60000,'observed exhaustion still honors the real reset');
 } finally {release();await mf.dispose();}
});

test('nested review histories checkpoint each page instead of monopolizing an installation turn',async()=>{
 let calls=0;
 const mf=await runtime(async req=>{
  calls++;const {query,variables}=await req.json();
  const rateLimit={cost:1,remaining:4999,resetAt:new Date(Date.now()+300000).toISOString()};
  if(query.includes('reviewThreads(')) return response({data:{rateLimit,repository:{pullRequest:{reviewThreads:{nodes:[{id:'thread',comments:{nodes:[{id:'c1',fullDatabaseId:'1',body:'first'}],pageInfo:{hasNextPage:true,endCursor:'a'}}}],pageInfo:{hasNextPage:false,endCursor:null}}}}}});
  assert.equal(variables.id,'thread');
  return response({data:{rateLimit,node:{comments:{nodes:[{id:variables.cursor==='a'?'c2':'c3',fullDatabaseId:variables.cursor==='a'?'2':'3',body:'overflow comment'}],pageInfo:{hasNextPage:variables.cursor==='a',endCursor:'b'}}}}});
 });
 try {
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName('200'));
  const input={repository,query:{operation:'review_threads',args:{number:7}},page:1};
  const first=await (await send(stub,'/read',input)).json();assert.ok(first.continuation);assert.equal(calls,1);
  const second=await (await send(stub,'/read',{...input,nested:first.continuation})).json();assert.ok(second.continuation);assert.equal(calls,2);
  const third=await (await send(stub,'/read',{...input,nested:second.continuation})).json();
  assert.deepEqual(third.data[0].comments.nodes.map(n=>n.id),['c1','c2','c3']);assert.equal(calls,3);
 } finally {await mf.dispose();}
});

test('admin reference reads bypass the mirror through the same quota owner and catalogue',async()=>{
 let calls=0;
 const mf=await runtime(async()=>{calls++;return response([],'core');},{REFERENCE_READS:'true'});
 try {
  const body=JSON.stringify({query:{repository:'org/repo',operation:'files',args:{number:7}}});
  assert.equal((await mf.dispatchFetch('https://test/admin/reference',{method:'POST',headers:{Authorization:'Bearer client'},body})).status,401);
  const result=await (await mf.dispatchFetch('https://test/admin/reference',{method:'POST',headers:{Authorization:'Bearer admin'},body})).json();
  assert.deepEqual(result.data,[]);assert.equal(calls,1);
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName('200'));
  const health=await (await stub.fetch('https://internal/health')).json();assert.ok(health.hour.some(row=>row.operation==='reference_files'));
  assert.equal((await mf.dispatchFetch('https://test/admin/reference',{method:'POST',headers:{Authorization:'Bearer admin'},body:JSON.stringify({query:{repository:'org/repo',operation:'files',args:{number:'../private'}}})})).status,400);
 } finally {await mf.dispose();}
});

test('measurement batches use the owner and return costs without response bodies',async()=>{
 const mf=await runtime(async req=>{
  const payload=await req.json();assert.match(payload.query,/reviewThreads\(first:20\)/);
  return response({data:{rateLimit:{cost:2,remaining:4998,resetAt:new Date(Date.now()+300000).toISOString()},repository:{q0:{reviewThreads:{nodes:[{id:'thread',comments:{nodes:[{body:'private response'}],pageInfo:{hasNextPage:true}}}],pageInfo:{hasNextPage:false}}}}}});
 },{REFERENCE_READS:'true'});
 try {
  const query={repository:'org/repo',operation:'review_threads',args:{number:7}};
  const r=await mf.dispatchFetch('https://test/admin/reference',{method:'POST',headers:{Authorization:'Bearer admin'},body:JSON.stringify({query,jobs:[{query}],profile:{threadPage:20}})});
  const result=await r.json();assert.equal(result.points,2);assert.equal(result.nested_overflows,1);assert.equal(result.batch_items,1);
  assert.equal(JSON.stringify(result).includes('private response'),false);assert.equal(Object.hasOwn(result,'data'),false);
 } finally {await mf.dispose();}
});

test('batch routing preserves scarce GraphQL quota when REST can carry the whole group',async()=>{
 let calls=0;
 const mf=await runtime(async(req,u)=>{
  calls++;
  if(u.pathname==='/graphql') return response({data:{rateLimit:{cost:1,remaining:1,resetAt:new Date(Date.now()+300000).toISOString()},repository:{pullRequest:{number:7,author:{login:'peer'},labels:{nodes:[]}}}}},'graphql',1);
  return response([],'core');
 });
 try {
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName('200'));
  await send(stub,'/read',{repository,query:{operation:'pr',args:{number:7}},api:'graphql',page:1});
  const jobs=Array.from({length:10},(_,i)=>({key:String(i),query:{operation:'issue_comments',args:{number:i+1}}}));
  const result=await (await send(stub,'/batch',{repository,jobs})).json();
  assert.equal(result.category,'routing:core');assert.equal(calls,1,'routing decisions consume no requests');
  const read=await (await send(stub,'/read',{repository,query:jobs[0].query,api:'core',page:1})).json();
  assert.deepEqual(read.data,[]);assert.equal(read.api,'core');assert.equal(calls,2);
 } finally {await mf.dispose();}
});

test('out-of-scope query fallback reveals no data and spends no installation quota',async()=>{
 let calls=0;
 const mf=await runtime(async()=>{calls++;return response([],'core');},{ALLOWED_REPOSITORIES:JSON.stringify([repository,{id:'101',name:'org/private',installation:'200'}])});
 try {
  for(const name of ['org/private','org/unknown']) {
   const r=await mf.dispatchFetch('https://test/v1/query?repository='+name+'&operation=open_prs',{headers:{Authorization:'Bearer client'}});
   assert.equal(r.status,200);const result=await r.json();assert.equal(result.status,'unavailable');assert.equal(result.reason,'repository_unavailable');assert.equal(Object.hasOwn(result,'data'),false);
  }
  assert.equal(calls,0);
  assert.equal((await mf.dispatchFetch('https://test/v1/query?repository=org/repo&operation=open_prs',{headers:{Authorization:'Bearer invalid'}})).status,401);
 } finally {await mf.dispose();}
});

test('malformed upstream JSON stays unavailable, preserves request ID, and retries through the same quota owner',async()=>{
 let malformed=true,network=0;
 const mf=await runtime(async()=>{network++;return malformed?new Response('not json',{status:200}):response({data:{rateLimit:{cost:1,remaining:4999,resetAt:new Date(Date.now()+300000).toISOString()},repository:{pullRequests:{nodes:[],pageInfo:{hasNextPage:false,endCursor:null}}}}});});
 try {
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName(repository.installation));
  const query={repository,query:{repository:repository.name,operation:'open_prs'},api:'graphql'};
  const first=await stub.fetch('https://internal/read',{method:'POST',headers:{'x-request-id':'malformed-drill'},body:JSON.stringify(query)});
  assert.equal(first.status,503);const error=await first.json();
  assert.equal(error.unavailable,true);assert.equal(error.reason,'malformed_upstream_response');assert.equal(error.request_id,'malformed-drill');assert.equal(Object.hasOwn(error,'data'),false);
  assert.equal(network,1,'no fallback fanout for a malformed response');
  malformed=false;
  const recovered=await (await send(stub,'/read',query)).json();
  assert.deepEqual(recovered.data,[]);assert.equal(network,2,'one successful retry by the existing owner');
 }finally{await mf.dispose();}
});

test('malformed responses without quota headers debit the existing owner budget once',async()=>{
 let malformed=false;
 const mf=await runtime(async()=>malformed?new Response('not JSON',{status:200}):response({data:{rateLimit:{cost:1,remaining:100,resetAt:new Date(Date.now()+300000).toISOString()},repository:{pullRequests:{nodes:[],pageInfo:{hasNextPage:false,endCursor:null}}}}},'graphql',100));
 try {
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName(repository.installation));
  const input={repository,query:{repository:repository.name,operation:'open_prs'},api:'graphql'};
  assert.equal((await send(stub,'/read',input)).status,200);
  const before=(await (await stub.fetch('https://internal/health')).json()).budgets.find(b=>b.resource==='graphql').remaining;
  malformed=true;assert.equal((await send(stub,'/read',input)).status,503);
  const after=(await (await stub.fetch('https://internal/health')).json()).budgets.find(b=>b.resource==='graphql').remaining;
  assert.equal(before-after,1);
 }finally{await mf.dispose();}
});

test('malformed status/header objects stay unavailable rather than becoming empty complete state',async()=>{
 let network=0;
 const mf=await runtime(async()=>{network++;return response({},'core');});
 try {
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName(repository.installation));
  for(const query of [{operation:'statuses',args:{sha:'a'.repeat(40)}},{operation:'pr',args:{number:7}},{operation:'repository'}]) {
   const before=network,result=await (await stub.fetch('https://internal/read',{method:'POST',headers:{'x-request-id':'shape-drill'},body:JSON.stringify({repository,query:{repository:repository.name,...query},api:'core'})})).json();
   assert.equal(result.unavailable,true);assert.equal(result.reason,'malformed_upstream_response');assert.equal(result.request_id,'shape-drill');assert.equal(Object.hasOwn(result,'data'),false);assert.equal(network-before,1);
  }
 }finally{await mf.dispose();}
});

test('malformed collection rows and pagination never publish an empty complete collection',async()=>{
 let network=0,graphql=false;
 const mf=await runtime(async()=>{network++;return graphql?response({data:{repository:{pullRequests:{nodes:[],pageInfo:{}}}}}):response([{}],'core');});
 try {
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName(repository.installation));
  const read=(operation,api,args={})=>send(stub,'/read',{repository,query:{repository:repository.name,operation,args},api});
  for(const [operation,args] of [['issue_comments',{number:7}],['workflow_jobs',{id:'9'}]]) {
   const before=network,value=await (await read(operation,'core',args)).json();assert.equal(value.unavailable,true);assert.equal(Object.hasOwn(value,'data'),false);assert.equal(network-before,1);
  }
  graphql=true;const value=await (await read('open_prs','graphql')).json();assert.equal(value.unavailable,true);assert.equal(Object.hasOwn(value,'data'),false);
 }finally{await mf.dispose();}
});

test('batch validation isolates malformed aliases without empty substitution or extra upstream requests',async()=>{
 let payload,network=0;
 const mf=await runtime(async()=>{network++;return response({data:payload});});
 try {
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName(repository.installation));
  const connection=nodes=>({nodes,pageInfo:{hasNextPage:false,endCursor:null}});
  const comment={id:'node',fullDatabaseId:'18446744073709551600',body:'retained'};
  const cases=[
   ['statuses',{status:{}},{status:null},'malformed_upstream_response'],
   ['issue_comments',{comments:{nodes:[],pageInfo:{}}},{comments:connection([comment])},'malformed_upstream_response'],
   ['issue_comments',{comments:connection([{id:'node',fullDatabaseId:null,body:'cannot address by node id'}])},{comments:connection([comment])},'upstream_identity_unavailable'],
   ['issue_comments',{comments:connection([{...comment,replyTo:{fullDatabaseId:null}}])},{comments:connection([comment])},'upstream_identity_unavailable'],
   ['reactions',{reactions:connection([{content:'HEART'}])},{reactions:connection([{databaseId:'18446744073709551600',content:'HEART'}])},'upstream_identity_unavailable'],
   ['review_threads',{reviewThreads:connection([{id:'thread',comments:connection([{...comment,body:null}])}])},{reviewThreads:connection([{id:'thread',comments:connection([comment])}])},'malformed_upstream_response']
  ];
  for(const [operation,bad,good,reason] of cases) {
   payload=operation==='reactions'?{q0:bad,q1:good}:{repository:{q0:bad,q1:good}};
   const jobs=[0,1].map(i=>({key:String(i),node_id:'node'+i,query:{operation,args:{number:7,sha:'a'.repeat(40),id:String(i+1)}}}));
   const before=network,result=await (await stub.fetch('https://internal/batch',{method:'POST',headers:{'x-request-id':'batch-validation'},body:JSON.stringify({repository,jobs})})).json();
   assert.equal(network-before,1);assert.equal(result.results[0].unavailable,true);assert.equal(result.results[0].reason,reason);
   assert.equal(result.results[0].request_id,'batch-validation');assert.equal(Object.hasOwn(result.results[0],'data'),false);
   assert.equal(Object.hasOwn(result.results[0],'retry_at'),false,'scheduler owns exponential retry');
   assert.ok(Array.isArray(result.results[1].data),'valid sibling retained');
   if(['issue_comments','reactions'].includes(operation))assert.equal(result.results[1].data[0].id,comment.fullDatabaseId);
  }
 }finally {await mf.dispose();}
});


test('label overflow restarts a pinned GraphQL traversal through the same quota owner',async()=>{
 let network=0;
 const labels=Array.from({length:101},(_,i)=>({name:'label-'+i}));
 const mf=await runtime(async req=>{
  network++;
  if(new URL(req.url).pathname!=='/graphql')return response({number:7,state:'open',labels},'core');
  const {query}=await req.json(),pr={number:7,state:'OPEN',headRefOid:'head',labels:{totalCount:101,nodes:labels.slice(0,100)}};
  return response({data:{repository:query.includes('pullRequests(')?{pullRequests:{nodes:[pr],pageInfo:{hasNextPage:false,endCursor:null}}}:{pullRequest:pr}}});
 });
 try {
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName(repository.installation));
  for(const operation of ['open_prs','pr']) {
   const before=network,result=await (await send(stub,'/read',{repository,query:{operation,args:{number:7}},api:'graphql',page:2,cursor:'page-two'})).json();
   assert.deepEqual(result,{restart:true,api:'core'});assert.equal(network-before,1,'restart does not fan out');
  }
  const result=await (await send(stub,'/read',{repository,query:{operation:'pr',args:{number:7}},api:'core',page:1})).json();
  assert.equal(result.data.labels.length,101);
 }finally {await mf.dispose();}
});

test('malformed thread overflow cannot publish a partial complete history',async()=>{
 let network=0;
 const mf=await runtime(async()=>{network++;return response({data:{node:{comments:{nodes:[{id:'node',fullDatabaseId:null,body:'unaddressable'}],pageInfo:{hasNextPage:false,endCursor:null}}}}});});
 try {
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName(repository.installation));
  const nested={rows:[{id:'thread',comments:{nodes:[{id:'old',fullDatabaseId:'1',body:'retained'}],pageInfo:{hasNextPage:true,endCursor:'next'}}}]};
  const result=await (await send(stub,'/read',{repository,query:{operation:'review_threads',args:{number:7}},nested})).json();
  assert.equal(result.unavailable,true);assert.equal(result.reason,'upstream_identity_unavailable');assert.equal(Object.hasOwn(result,'data'),false);assert.equal(network,1);
 }finally {await mf.dispose();}
});
