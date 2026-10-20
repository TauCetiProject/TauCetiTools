import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { createHmac, createHash } from 'node:crypto';
import {runtime} from './runtime.mjs';
let mf, namespace, archive;
const token='test-client-token', admin='test-admin-token', secret='test-webhook-secret';
const sha=x=>createHash('sha256').update(x).digest('hex');
const repo={id:'10',name:'org/repo',installation:'20'};
before(async()=>{
  mf=await runtime(async()=>Response.json([]),{ALLOWED_REPOSITORIES:JSON.stringify([repo]),MIGRATION_ENVIRONMENT:'test',GITHUB_APP_ID:'',GITHUB_INSTALLATION_ID:'20',GITHUB_PRIVATE_KEY:'',WEBHOOK_SECRET:secret,CLIENT_TOKENS:JSON.stringify([{hash:sha(token),repositories:['org/repo']}]),ADMIN_TOKEN_HASH:sha(admin)});
  namespace=await mf.getDurableObjectNamespace('REPOSITORIES'); archive=await mf.getR2Bucket('ARCHIVE');
});
after(async()=>{await mf?.dispose();});
const request=(path,options={})=>mf.dispatchFetch('https://test'+path,options);
const headers={Authorization:'Bearer '+token};
function body(pr={}) { return {installation:{id:20},repository:{id:10,full_name:'org/repo'},action:'opened',pull_request:{id:123,number:7,title:'new',body:'text',state:'open',updated_at:'2026-10-11T00:00:00Z',head:{sha:'a'.repeat(40),ref:'feature',repo:{name:'repo',owner:{login:'org'}}},user:{login:'author'},...pr}}; }
async function ingest(id,event,payload,received=Date.now()) {
  const key='fixture/'+id; await archive.put(key,JSON.stringify(payload));
  return namespace.get(namespace.idFromName('10')).fetch('https://internal/event',{method:'POST',body:JSON.stringify({key,delivery:id,event,repository:'org/repo',received})});
}
test('public health does not expose internal quota or secrets',async()=>{
  const r=await request('/healthz'); assert.equal(r.status,200); assert.deepEqual(await r.json(),{service:'tauceti-github-state',version:1});
  assert.equal((await request('/v1/health?repository=org/repo')).status,401);
});
test('webhook rejects bad signatures and conflicting duplicate bodies',async()=>{
  const text=JSON.stringify(body()); const base={'x-github-delivery':'delivery-one','x-github-event':'pull_request'};
  assert.equal((await request('/webhook/github',{method:'POST',headers:base,body:text})).status,401);
  const signature='sha256='+createHmac('sha256',secret).update(text).digest('hex'); const good={...base,'x-hub-signature-256':signature};
  assert.equal((await request('/webhook/github',{method:'POST',headers:good,body:text})).status,202);
  const pinned=await archive.head('parked/10/bodies/delivery-one.json');
  assert.ok(pinned,'HTTP acknowledgement already has a pinned body');
  assert.equal(pinned.customMetadata.expires,undefined);
  assert.ok((await archive.list({prefix:'parked/10/pointers/'})).objects.some(o=>o.key.endsWith('-delivery-one.json')),'pointer is durable before queue delivery');
  assert.equal((await request('/webhook/github',{method:'POST',headers:good,body:text})).status,202);
  const other=JSON.stringify(body({title:'different'}));
  assert.equal((await request('/webhook/github',{method:'POST',headers:{...base,'x-hub-signature-256':'sha256='+createHmac('sha256',secret).update(other).digest('hex')},body:other})).status,409);
});
test('webhooks do not invent complete inventory; duplicate and old delivery are idempotent',async()=>{
  assert.equal((await ingest('new','pull_request',body())).status,200);
  const r=await request('/v1/query?repository=org/repo&operation=open_prs',{headers}); assert.equal(r.status,200);
  const p=await r.json(); assert.equal(p.status,'unavailable'); assert.equal(p.complete,false);
  const doStub=namespace.get(namespace.idFromName('10')); const first=await (await doStub.fetch('https://internal/health')).json();
  assert.equal((await ingest('new','pull_request',body())).status,200);
  await ingest('old','pull_request',body({title:'obsolete',state:'closed',updated_at:'2026-10-10T00:00:00Z'}),Date.now()-10000);
  // Background survey hydration may advance the global revision independently of these deliveries.
  const changes=await (await request('/v1/changes?repository=org/repo&after='+first.revision,{headers})).json();
  assert.equal(changes.reset,false);
  assert.deepEqual(changes.changes.filter(c=>c.kind==='pr' || c.kind==='collection'),[]);
});
test('resource tombstones advance journal without marking missing histories empty',async()=>{
  const c={id:999,body:'hello',created_at:'2026-10-11T00:00:00Z',updated_at:'2026-10-11T00:00:00Z',user:{login:'peer'}};
  await ingest('comment','issue_comment',{...body(),issue:{number:7},comment:c});
  await ingest('delete','issue_comment',{...body(),action:'deleted',issue:{number:7},comment:c});
  const changes=await (await request('/v1/changes?repository=org/repo',{headers})).json(); assert.equal(changes.changes.filter(c=>c.kind==='issue_comments' && c.id==='999').at(-1).tombstone,1);
  const q=await (await request('/v1/query?repository=org/repo&operation=issue_comments&args=%7B%22number%22%3A7%7D',{headers})).json(); assert.equal(q.status,'unavailable');
});
test('query validation, repo scoping, admin token independent',async()=>{
  assert.equal((await request('/v1/query?repository=org/repo&operation=unsafe',{headers})).status,400);
  const outside=await request('/v1/query?repository=org/private&operation=open_prs',{headers});
  assert.equal(outside.status,200);const unavailable=await outside.json();assert.equal(unavailable.status,'unavailable');assert.equal(unavailable.complete,false);assert.equal(Object.hasOwn(unavailable,'data'),false);
  assert.equal((await request('/v1/query?repository=org/private&operation=open_prs')).status,401);
  assert.equal((await request('/admin/bootstrap',{method:'POST',headers,body:JSON.stringify({repository:'org/repo'})})).status,401);
  assert.equal((await request('/admin/bootstrap',{method:'POST',headers:{Authorization:'Bearer '+admin},body:JSON.stringify({repository:'org/repo'})})).status,200);
});

test('reference reads require an explicitly enabled environment and admin authentication',async()=>{
 const body=JSON.stringify({query:{repository:'org/repo',operation:'files',args:{number:7}}});
 assert.equal((await request('/admin/reference',{method:'POST',headers:{Authorization:'Bearer '+admin},body})).status,401,'production default disables reference reads');
});


test('activity observations are authenticated, repository-scoped, bounded, and tolerate missing coverage',async()=>{
 const body=JSON.stringify({repository:'org/repo',numbers:[999999]});
 assert.equal((await request('/v1/activity',{method:'POST',body})).status,401);
 assert.equal((await request('/v1/activity',{method:'POST',headers,body:JSON.stringify({repository:'org/private',numbers:[7]})})).status,403);
 for(const input of [null,{repository:'org/repo',numbers:[-1]},{repository:'org/repo',numbers:Array(501).fill(1)}])
  assert.equal((await request('/v1/activity',{method:'POST',headers,body:JSON.stringify(input)})).status,400);
 const r=await request('/v1/activity',{method:'POST',headers,body});
 assert.equal(r.status,200);const data=await r.json();assert.equal(data.coverage,'observed');assert.deepEqual(data.missing,[999999]);
});

test('CI records are not mirrored: webhooks are acknowledged unstored and queries fall back',async()=>{
 const text=JSON.stringify({repository:{id:10},installation:{id:20},action:'completed',workflow_run:{id:77,run_attempt:1,status:'completed'}});
 const signed={'x-github-delivery':'ci-delivery','x-github-event':'workflow_run','x-hub-signature-256':'sha256='+createHmac('sha256',secret).update(text).digest('hex')};
 const r=await request('/webhook/github',{method:'POST',headers:signed,body:text});
 assert.equal(r.status,202);assert.deepEqual(await r.json(),{ignored:true,reason:'not_mirrored'});
 assert.equal((await archive.list({prefix:'webhooks/'})).objects.some(o=>o.key.includes('ci-delivery')),false,'no raw body stored');
 for(const [operation,args] of [['workflow_jobs',{id:'77'}],['workflow_runs',{}],['checks',{sha:'a'.repeat(40)}]]) {
  const q=await (await request('/v1/query?repository=org/repo&operation='+operation+'&args='+encodeURIComponent(JSON.stringify(args)),{headers})).json();
  assert.equal(q.status,'unavailable');assert.equal(q.reason,'not_mirrored');assert.equal(Object.hasOwn(q,'data'),false);
 }
 // A CI event queued before mirroring stopped is acknowledged without writes.
 assert.equal((await ingest('queued-ci','check_run',{check_run:{id:5,head_sha:'a'.repeat(40)}})).status,200);
 const changes=await (await request('/v1/changes?repository=org/repo',{headers})).json();
 assert.equal(changes.changes.some(c=>c.kind==='checks'),false);
});

test('GET and batch queries retain unquoted IDs beyond the JavaScript safe range',async()=>{
 const doStub=namespace.get(namespace.idFromName(repo.id)),number='90071992547409931234';
 const key='fixture/exact-pr';await archive.put(key,'{"pull_request":{"number":'+number+',"state":"open","title":"exact ID","head":{"sha":"'+'b'.repeat(40)+'"},"user":{"login":"peer"}}}');
 assert.equal((await doStub.fetch('https://internal/event',{method:'POST',body:JSON.stringify({key,delivery:'exact-pr',event:'pull_request',repository:repo.name,received:Date.now()})})).status,200);
 const response=await request('/v1/query?repository=org/repo&operation=pr&args='+encodeURIComponent('{"number":'+number+'}'),{headers});
 assert.equal((await response.json()).reason,'coverage_unavailable');
 const changes=await (await request('/v1/changes?repository=org/repo',{headers})).json();
 assert.ok(changes.changes.some(row=>row.kind==='pr' && row.id===number));
 const exact='{"repository":"org/repo","operation":"pr","args":{"number":'+number+'}}';
 const batch=await request('/v1/query_batch',{method:'POST',headers,body:'{"queries":['+exact+',{"repository":"org/private","operation":"open_prs"},'+exact+']}'});
 const result=await batch.json();assert.equal(result.results.length,3);
 assert.equal(result.results[0].result.reason,'coverage_unavailable');assert.equal(result.results[1].result.reason,'repository_unavailable');assert.equal(result.results[2].result.reason,'coverage_unavailable');
});

test('explicit SQLite ownership fencing works in the Worker SQL runtime',{skip:Boolean(process.env.TEST_POSTGRES_URL)},async()=>{
 const post=(path,body)=>request('/admin/'+path,{method:'POST',headers:{Authorization:'Bearer '+admin},body:JSON.stringify({repository:repo.name,...body})});
 assert.equal((await post('ingress',{mode:'parked'})).status,200);
 const fence=await post('storage/ownership',{backend:'postgres',generation:'migration-2',expected_generation:'sqlite-v1'});
 assert.equal(fence.status,200,JSON.stringify(await fence.json()));
 const response=await post('storage/probe',{});assert.equal(response.status,400);assert.equal((await response.json()).error,'backend_fenced');
});
