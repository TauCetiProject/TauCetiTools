import {test} from 'node:test';
import assert from 'node:assert/strict';
import {postgresFixture,PostgresStore,postgresSQL,rawReceiptLookup} from './postgres_runtime.mjs';
const enabled=Boolean(process.env.TEST_POSTGRES_URL && process.env.TEST_POSTGRES_MIGRATION_URL);
const pg=(name,fn)=>test(name,{skip:!enabled},fn);
function requireDirectCleanup(){const host=new URL(process.env.TEST_POSTGRES_URL).hostname;assert.ok(host.endsWith('.neon.tech')&&!host.split('.')[0].endsWith('-pooler'),'Session termination fault tests require a direct, unpooled Neon test endpoint');}
const repo={id:'100',name:'org/repo',installation:'200'};
const complete=async(state,q)=>state.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET complete=1',q.operation+':'+JSON.stringify(q.args || {}),q.operation,JSON.stringify(q.args || {}),1,Date.now(),await state.revision(),null);
const send=(state,path,body)=>state.fetch(new Request('https://internal'+path,{method:'POST',body:JSON.stringify(body)}));
async function event(f,id,body,event='issue_comment',received=Date.now()) {
 const key='events/'+id;await f.env.ARCHIVE.put(key,JSON.stringify(body));
 return f.state.ingest({key,delivery:id,event,repository:repo.name,received});
}
function intercept(store,check) {
 return new Proxy(store,{get(target,key){
  if(key==='transaction')return apply=>target.transaction(tx=>apply(intercept(tx,check)));
  if(key==='exec')return async(sql,...args)=>{check(sql);return target.exec(sql,...args);};
  if(key==='batch')return async statements=>{for(const s of statements)check(s.sql);return target.batch(statements);};
  const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;
 }});
}

test('PostgreSQL configuration errors never echo connection credentials',async()=>{
 const store=new PostgresStore('postgresql://user:test-password@[invalid/database','100','test-v1');
 await assert.rejects(store.exec('SELECT 1'),error=>error.message==='Repository database unavailable');
});

test('PostgreSQL dialect preserves literal question marks, parameter indexes and exact numeric ordering',()=>{
 const sql=postgresSQL("SELECT r.id FROM resources r WHERE kind=? AND data!='?' ORDER BY CAST(r.id AS INTEGER),r.id",'github_state_100');
 assert.match(sql,/kind=\$1 AND data!='\?'/);assert.match(sql,/\^\[0-9\]\+\$/);assert.match(sql,/r.id::numeric/);
 assert.match(postgresSQL('INSERT OR IGNORE INTO members VALUES(?,?)','github_state_100'),/ON CONFLICT DO NOTHING$/);
});
pg('PostgreSQL preserves text IDs, exact timestamps, numeric ordering and change revisions',async()=>{
 const f=await postgresFixture();try {
  const {state,store}=f,q={repository:repo.name,operation:'issue_comments',args:{number:7}};
  for(const id of ['90071992547409931234','9007199254740993','12','2']) {
   await state.put('issue_comments',id,{id,body:'first',updated_at:'2026-10-19T22:29:53.816Z'},Date.now(),false,'7',true);
   await state.membership('issue_comments:{"number":7}',id,true);
  }
  await complete(state,q);
  const rows=await state.rows(q);assert.deepEqual(rows.map(r=>r.id),['2','12','9007199254740993','90071992547409931234']);
  assert.equal((await store.exec('SELECT updated FROM resources WHERE id=?','90071992547409931234')).one().updated,'2026-10-19T22:29:53.816Z');
  assert.equal(await state.revision(),8);
  const changes=await (await state.fetch(new Request('https://internal/changes?after=4'))).json();
  assert.equal(changes.reset,false);assert.deepEqual(changes.changes.map(r=>r.revision),[5,6,7,8]);
  await state.storageMaintenance();
  const health=await (await state.fetch(new Request('https://internal/health'))).json();assert.equal(health.storage.backend,'postgres');assert.equal(health.storage.schema_version,1);
 }finally{await f.close();}
});
pg('interrupted event rolls back resources, memberships, repairs, revisions and receipt; replay is idempotent',async()=>{
 const f=await postgresFixture();try {
  let fail=true;f.state.store=intercept(f.store,sql=>{if(fail && sql.includes('INTO deliveries'))throw Error('interrupted before receipt');});
  const body={action:'created',issue:{number:7},comment:{created_at:'2026-10-19T21:00:00Z',id:'90071992547409931234',body:'durable',updated_at:'2026-10-19T22:29:53Z'}};
  await assert.rejects(event(f,'interrupted',body),/interrupted/);
  for(const table of ['resources','members','jobs','changes','deliveries'])assert.equal((await f.store.exec('SELECT COUNT(*) n FROM '+table)).one().n,0,table);
  assert.equal(await f.state.revision(),0);assert.ok(f.objects.has('events/interrupted'));
  fail=false;await event(f,'interrupted',body);const revision=await f.state.revision();
  await event(f,'interrupted',body);assert.equal(await f.state.revision(),revision);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM deliveries')).one().n,1);
  const latest=await f.state.meta('latest_applied_input');assert.equal(latest.delivery,'interrupted');assert.ok(latest.applied_at>=latest.received);
 }finally{await f.close();}
});
pg('reordered events and tombstones retain the newest input even for identical source timestamps',async()=>{
 const f=await postgresFixture();try {
  const body={issue:{number:7},comment:{created_at:'2026-10-19T21:00:00Z',id:'99',body:'newest',updated_at:'2026-10-19T22:29:53Z'}},received=Date.now();
  await event(f,'new',body,'issue_comment',received);
  await event(f,'old',{...body,comment:{...body.comment,body:'old'}},'issue_comment',received-1000);
  assert.equal(JSON.parse((await f.store.exec('SELECT data FROM resources WHERE id=?','99')).one().data).body,'newest');
  await event(f,'deleted',{...body,action:'deleted'},'issue_comment',received+1000);
  await event(f,'late',{...body,comment:{...body.comment,body:'resurrection'}},'issue_comment',received);
  assert.equal((await f.store.exec('SELECT tombstone FROM resources WHERE id=?','99')).one().tombstone,1);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM members')).one().n,0);
 }finally{await f.close();}
});
pg('webhook commits during a slow upstream read; old response preserves the event and its repair generation',async()=>{
 const f=await postgresFixture();try {
  const q={repository:repo.name,operation:'issue_comments',args:{number:7}};await f.state.enqueue(q);
  const job=await f.state.claim(await f.state.nextJob());
  let release,entered;const gate=new Promise(r=>release=r),ready=new Promise(r=>entered=r);
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>{entered();await gate;return Response.json({api:'core',data:[{id:'99',body:'old',updated_at:'2026-10-19T22:00:00Z'}]});}})};
  const slow=f.state.refresh(job);await ready;
  await event(f,'new-webhook',{issue:{number:7},comment:{created_at:'2026-10-19T21:00:00Z',id:'99',body:'webhook',updated_at:'2026-10-19T23:00:00Z'}});
  release();await slow;
  assert.equal(JSON.parse((await f.store.exec('SELECT data FROM resources WHERE id=?','99')).one().data).body,'webhook');
  const current=(await f.store.exec('SELECT * FROM jobs WHERE key=?',job.key)).one();assert.equal(current.dirty,1);assert.ok(current.generation>job.start_generation);
  assert.equal(await f.state.complete(q),true);
 }finally{await f.close();}
});
pg('collection publication failure cannot expose a partial complete history and can be replayed',async()=>{
 const f=await postgresFixture();try {
  const q={repository:repo.name,operation:'issue_comments',args:{number:7}};await f.state.enqueue(q);
  const job=await f.state.claim(await f.state.nextJob());let fail=true;
  f.state.store=intercept(f.store,sql=>{if(fail && sql.startsWith('INSERT INTO collections'))throw Error('publication interrupted');});
  const result={api:'core',data:[{id:'10',body:'a'},{id:'20',body:'b'}]};
  await assert.rejects(f.state.refresh(job,result),/publication interrupted/);
  assert.equal(await f.state.complete(q),false);assert.equal((await f.store.exec('SELECT COUNT(*) n FROM resources')).one().n,0);
  fail=false;await f.state.refresh(job,result);assert.equal(await f.state.complete(q),true);assert.equal((await f.state.rows(q)).length,2);
 }finally{await f.close();}
});
pg('HTTP outage returns sanitized health, reconnects with the same store, and never returns invented coverage',async()=>{
 const f=await postgresFixture(),original=globalThis.fetch;try {
  globalThis.fetch=async()=>{throw Error('test connection interrupted');};
  const health=await f.store.health();assert.equal(health.connected,false);assert.ok(health.connection_failures>=1);
  await assert.rejects(f.store.exec('SELECT value FROM meta WHERE key=?','repository'),/Repository database unavailable/);
  globalThis.fetch=original;assert.equal((await f.store.health()).connected,true);
  assert.equal((await f.state.query({repository:repo.name,operation:'issue_comments',args:{number:99}})).status,'unavailable');
 }finally{globalThis.fetch=original;await f.close();}
});
pg('application role cannot migrate, change ownership/capacity or access production; stale generations cannot write',async()=>{
 const f=await postgresFixture();try {
  for(const sql of ['CREATE TABLE probe(id TEXT)',"UPDATE backend_control SET generation='wrong'","UPDATE capacity_statistics SET storage_limit_bytes=1"])
   await assert.rejects(f.store.exec(sql),/Repository database unavailable/);
  const role=(await f.admin.query('SELECT rolcreatedb,rolcreaterole,rolsuper FROM pg_roles WHERE rolname=$1',[process.env.TEST_POSTGRES_ROLE || 'github_state_test_app'])).rows[0];
  assert.deepEqual(role,{rolcreatedb:false,rolcreaterole:false,rolsuper:false});
  await f.admin.query(`UPDATE ${f.schema}.backend_control SET generation='next'`);
  await assert.rejects(f.state.put('pr','7',{number:7}),/generation fenced/);
  await assert.rejects(f.store.exec('INSERT INTO demand VALUES(?,?)','x',Date.now()),/generation fenced/);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM resources')).one().n,0);
 }finally{await f.close();}
});
pg('empty provisioned PostgreSQL does not bootstrap on deployment, tick or query',async()=>{
 const f=await postgresFixture({configured:false});try {
  f.env.INSTALLATIONS={idFromName(){throw Error('unexpected GitHub access');}};
  const response=await send(f.state,'/tick',{repository:repo.name});assert.equal(response.status,409);
  assert.equal((await f.state.query({repository:repo.name,operation:'open_prs'})).reason,'repository_unconfigured');
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs')).one().n,0);
 }finally{await f.close();}
});
pg('failed resource blob upload commits no reference or receipt and the event remains replayable',async()=>{
 const f=await postgresFixture();try {
  const put=f.env.ARCHIVE.put;let fail=true;
  f.env.ARCHIVE.put=async(key,...args)=>{if(fail && key.startsWith('resources/'))throw Error('R2 upload interrupted');return put(key,...args);};
  const body={issue:{number:7},comment:{id:'42',body:'x'.repeat(1600000),created_at:'2026-10-19T21:00:00Z'}};
  await assert.rejects(event(f,'blob',body),/R2 upload interrupted/);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM resources')).one().n,0);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM deliveries')).one().n,0);
  fail=false;await event(f,'blob',body);
  const ref=JSON.parse((await f.store.exec('SELECT data FROM resources WHERE id=?','42')).one().data);assert.ok(ref.$r2);assert.ok(f.objects.has(ref.$r2));
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM deliveries')).one().n,1);
 }finally{await f.close();}
});
pg('legacy SQL cursors, complete versus observed coverage, timestamps and conditional reads keep the wire contract',async()=>{
 const f=await postgresFixture();try {
  const q={repository:repo.name,operation:'issue_comments',args:{number:7}};
  await f.state.put('issue_comments','1',{id:'1',body:'stored'},Date.now(),false,'7');await f.state.membership('issue_comments:{"number":7}','1',true);await complete(f.state,q);
  const initialResponse=await send(f.state,'/query',q),first=await initialResponse.json();
  assert.equal(first.complete,true);assert.equal(first.coverage,'complete');assert.ok(Number.isSafeInteger(first.observed_at));assert.ok(Number.isSafeInteger(first.verified_at));
  for(const validator of [initialResponse.headers.get('etag'),'W/'+initialResponse.headers.get('etag')]) {
   const conditional=await f.state.fetch(new Request('https://internal/query',{method:'POST',headers:{'if-none-match':validator},body:JSON.stringify(q)}));assert.equal(conditional.status,304);
  }
  const payload={key:'issue_comments:{"number":7}',result:{...first,data:[{id:'legacy'}]}};
  await f.store.exec('INSERT INTO cursors VALUES(?,?,?)','legacy:100',Date.now()+60000,JSON.stringify(payload));
  assert.deepEqual((await (await send(f.state,'/query',{...q,cursor:'legacy:100'})).json()).data,[{id:'legacy'}]);
  assert.equal((await (await send(f.state,'/query',{...q,args:{number:8},cursor:'legacy:100'})).json()).reason,'cursor_query_mismatch');
 }finally{await f.close();}
});
pg('stored survey hydration batches large histories without GitHub calls',async()=>{
 const f=await postgresFixture();try {
  await f.state.put('pr','7',{number:7,state:'OPEN'});
  const rows=Array.from({length:1100},(_,i)=>({id:String(i+1),body:i===0?'<!--tauceti-rubric:reuse -->':'contest',in_reply_to_id:i===0?null:'1'}));
  await f.admin.query(`INSERT INTO ${f.schema}.resources(kind,id,parent,data,updated,observed,revision,tombstone) VALUES `+rows.map((_,i)=>`('review_comments',$${i*2+1},'7',$${i*2+2},'',0,0,0)`).join(','),rows.flatMap(row=>[row.id,JSON.stringify(row)]));
  await f.admin.query(`INSERT INTO ${f.schema}.members(collection,id) SELECT 'review_comments:{"number":7}',id FROM ${f.schema}.resources WHERE kind='review_comments'`);
  let calls=0;f.state.store=intercept(f.store,()=>calls++);
  f.env.INSTALLATIONS=new Proxy({},{get(){throw Error('Hydration attempted GitHub access');}});
  await f.state.hydrateSurvey('7');assert.ok(calls<60,'SQL statements must be batched, not per comment: '+calls);
  const facts=JSON.parse(await f.state.projectedSurvey('7'));assert.equal(facts.contest.id,'1100');assert.equal(facts.contest.rubric,'reuse');
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM survey_fragments')).one().n,1100);
 }finally{await f.close();}
});
pg('long file paths retain exact identity, update and membership uniqueness beyond B-tree tuple limits',async()=>{
 const f=await postgresFixture();try{
  const {randomBytes}=await import('node:crypto');
  const filename=Array.from({length:30},()=>randomBytes(96).toString('base64url')).join('/'),id='7:'+filename,q={repository:repo.name,operation:'files',args:{number:7}},key='files:{"number":7}';
  await f.state.put('files',id,{filename,additions:1},Date.now(),false,'7');await f.state.membership(key,id,true);
  await f.state.put('files',id,{filename,additions:2},Date.now(),false,'7');await f.state.membership(key,id,true);await complete(f.state,q);
  assert.deepEqual(await f.state.rows(q),[{filename,additions:2}]);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM members WHERE collection=?',key)).one().n,1);
  await assert.rejects(f.store.exec('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)','files',id,'7','{}','',0,0,0),/23P01/);
 }finally{await f.close();}
});
pg('writers wait locally beyond the database lock timeout, without losing another event',async()=>{
 const f=await postgresFixture();let release;try{
  let entered;const ready=new Promise(r=>entered=r),gate=new Promise(r=>release=r);
  const first=f.state.atomic(async state=>{await state.put('pr','1',{number:1});entered();await gate;});await ready;
  const second=f.state.put('pr','2',{number:2});
  const standalone=f.store.exec('INSERT INTO demand VALUES(?,?)','waiting-writer',Date.now());
  await new Promise(r=>setTimeout(r,5200));release();await Promise.all([first,second,standalone]);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM resources')).one().n,2);
 }finally{release?.();await f.close();}
});
pg('large publication writes are set based and do not open a transaction per row',async()=>{
 const f=await postgresFixture();try{
  const q={repository:repo.name,operation:'open_prs'};await f.state.enqueue(q);const job=await f.state.claim(await f.state.nextJob());
  let statements=0;f.state.store=intercept(f.store,()=>statements++);
  const data=Array.from({length:1200},(_,i)=>({number:i+1,state:'OPEN',headRefOid:'a'.repeat(40),isDraft:true,author:{login:'peer'},labels:[]}));
  const start=performance.now();await f.state.refresh(job,{api:'graphql',data});const duration=performance.now()-start;
  assert.ok(statements<400,'bounded cache groups must retain set-based writes, observed '+statements);
  assert.equal(await f.state.complete(q),true);assert.equal((await f.store.exec("SELECT COUNT(*) n FROM members WHERE collection='open_prs:{}'")).one().n,1200);
  console.log(JSON.stringify({publication_rows:1200,sql_statements:statements,duration_ms:duration}));
  statements=0;await f.state.enqueue(q);await f.store.exec('UPDATE jobs SET lease_id=NULL,lease_until=0,due=0 WHERE key=?',job.key);const unchanged=await f.state.claim((await f.store.exec('SELECT * FROM jobs WHERE key=?',job.key)).one());await f.state.refresh(unchanged,{api:'graphql',data});
  assert.ok(statements<450,'unchanged observations must also use bounded set writes, observed '+statements);
 }finally{await f.close();}
});

pg('the first large event can prepare its blob before repository metadata exists',async()=>{
 const f=await postgresFixture({configured:false});try{
  await event(f,'first-large',{issue:{number:7},comment:{id:'42',body:'x'.repeat(1600000),created_at:'2026-10-19T00:00:00Z'}});
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM deliveries')).one().n,1);assert.equal((await f.state.meta('repository')).id,repo.id);
  const pointer=JSON.parse((await f.store.exec("SELECT data FROM resources WHERE kind='issue_comments' AND id='42'")).one().data);assert.ok(pointer.__tauceti_received_at);
  await f.store.exec('INSERT INTO retired_objects VALUES(?,?)',pointer.$r2,Date.now()-40*86400000);
  assert.equal((await (await f.state.fetch(new Request('https://internal/retained-object?key='+encodeURIComponent(pointer.$r2)))).json()).retained,true,'archived webhook data and pinned exports cannot be retired');
 }finally{await f.close();}
});

pg('PostgreSQL retention honors operator pins and parked-input receipt protection',async()=>{
 const f=await postgresFixture();try{
  const old=Date.now()-40*86400000;
  await f.store.exec('INSERT INTO changes VALUES(?,?,?,?,?)',100,old,'pr','7',0);await f.store.exec('INSERT INTO deliveries VALUES(?,?,?)','old','push',old);await f.store.exec('INSERT INTO demand VALUES(?,?)','old',old);
  Object.assign(f.state,{storageLoaded:true,retentionPolicy:{mode:'enforce',disabled:[],pinned:['expired_changes','expired_deliveries']},receiptsPinned:true});
  await f.state.expire();await f.state.archiveDeliveryReceipts();
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM changes')).one().n,1);assert.equal((await f.store.exec('SELECT COUNT(*) n FROM deliveries')).one().n,1);assert.equal((await f.store.exec('SELECT COUNT(*) n FROM demand')).one().n,0);
  f.state.retentionPolicy.pinned=[];f.state.receiptsPinned=false;await f.state.expire();await f.state.archiveDeliveryReceipts();
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM changes')).one().n,0);assert.equal((await f.store.exec('SELECT COUNT(*) n FROM deliveries')).one().n,0);assert.ok(f.objects.has('receipts/'+repo.id+'/old.json'),'durable R2 receipt protects replay after SQL expiry');
 }finally{await f.close();}
});
pg('an aborted PostgreSQL transaction cannot report a successful commit after a swallowed SQL error',async()=>{
 const f=await postgresFixture();try{
  await assert.rejects(f.store.transaction(async tx=>{await tx.exec('INSERT INTO demand VALUES(?,?)','must-roll-back',Date.now());try{await tx.exec('SELECT 1/0');}catch{}return true;}),/25P02/);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM demand')).one().n,0);
 }finally{await f.close();}
});
pg('explicit schema initialization upgrades pre-release key constraints and collation',async()=>{
 const f=await postgresFixture();try{
  await f.admin.query(`ALTER TABLE ${f.schema}.resources DROP CONSTRAINT resource_identity; ALTER TABLE ${f.schema}.resources ALTER COLUMN kind DROP NOT NULL,ALTER COLUMN id DROP NOT NULL,ALTER COLUMN id TYPE TEXT COLLATE "default"; ALTER TABLE ${f.schema}.members DROP CONSTRAINT member_identity; ALTER TABLE ${f.schema}.members ALTER COLUMN collection DROP NOT NULL,ALTER COLUMN id DROP NOT NULL;`);
  const {initializeSchema}=await import('../scripts/postgres-schema.mjs');await initializeSchema(f.admin,f.schema.slice('github_state_'.length),'test-v1','github_state_test_app');
  const row=(await f.admin.query("SELECT a.attnotnull,c.collname FROM pg_attribute a JOIN pg_class t ON t.oid=a.attrelid JOIN pg_namespace n ON n.oid=t.relnamespace JOIN pg_collation c ON c.oid=a.attcollation WHERE n.nspname=$1 AND t.relname='resources' AND a.attname='id'",[f.schema])).rows[0];
  assert.deepEqual(row,{attnotnull:true,collname:'C'});
  await assert.rejects(f.store.exec('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)','files',null,'7','{}','',0,0,0),/23502/);
 }finally{await f.close();}
});
pg('dependent batch statements retain input order within a transaction',async()=>{
 const f=await postgresFixture();try{
  await f.store.transaction(tx=>tx.batch([{sql:'INSERT INTO demand VALUES(?,?)',bindings:['batch-source',123]},{sql:'INSERT INTO meta(key,value) SELECT ?,CAST(at AS TEXT) FROM demand WHERE key=?',bindings:['batch-derived','batch-source']}]));
  assert.equal((await f.store.exec('SELECT value FROM meta WHERE key=?','batch-derived')).one().value,'123');
 }finally{await f.close();}
});
pg('late staging uploads from an old lease cannot replace a newer checkpoint',async()=>{
 const f=await postgresFixture();let release;try{
  const q={repository:repo.name,operation:'files',args:{number:7}},key='files:{"number":7}';await f.state.enqueue(q);const old=await f.state.claim(await f.state.nextJob());
  let entered,first=true;const ready=new Promise(r=>entered=r),gate=new Promise(r=>release=r),put=f.env.ARCHIVE.put;
  f.env.ARCHIVE.put=async(name,...args)=>{if(first && name.startsWith('staging/')){first=false;entered();await gate;}return put(name,...args);};
  const stale=f.state.refresh(old,{api:'core',data:[{filename:'obsolete'}],cursor:'old-page'});await ready;
  await f.store.exec('UPDATE jobs SET lease_until=0 WHERE key=?',key);const replacement=await f.state.claim((await f.store.exec('SELECT * FROM jobs WHERE key=?',key)).one());
  await f.state.refresh(replacement,{api:'core',data:[{filename:'fresh'}],cursor:'next-page'});release();await stale;
  const next=(await f.store.exec('SELECT * FROM jobs WHERE key=?',key)).one();await f.state.refresh(next,{api:'core',data:[{filename:'second'}]});
  assert.deepEqual((await f.state.rows(q)).map(row=>row.filename),['fresh','second']);assert.equal(await f.state.complete(q),true);
  assert.equal(await f.state.meta('staging_pages:'+key),undefined,'complete publication retires its SQL staging reference');
 }finally{release?.();await f.close();}
});
pg('history reads batch small rows while respecting the transferred-payload byte bound',async()=>{
 const f=await postgresFixture();try{
  const q={repository:repo.name,operation:'issue_comments',args:{number:7}},count=5001,base=90071992547409930000n;
  for(let offset=0;offset<count;offset+=1000){const ids=Array.from({length:Math.min(1000,count-offset)},(_,i)=>(base+BigInt(offset+i)).toString());await f.admin.query(`INSERT INTO ${f.schema}.resources(kind,id,parent,data,updated,observed,revision,tombstone) VALUES `+ids.map((_,i)=>`('issue_comments',$${i*2+1},'7',$${i*2+2},'',0,0,0)`).join(','),ids.flatMap(id=>[id,JSON.stringify({id,body:'x'.repeat(700)})]));}
  await f.admin.query(`INSERT INTO ${f.schema}.members(collection,id) SELECT 'issue_comments:{"number":7}',id FROM ${f.schema}.resources`);await complete(f.state,q);
  let reads=0,maxBytes=0;const source=f.store;
  f.state.store=new Proxy(source,{get(target,key){if(key==='exec')return async(sql,...args)=>{if(sql.startsWith('SELECT'))reads++;const result=await target.exec(sql,...args);maxBytes=Math.max(maxBytes,result.toArray().reduce((n,row)=>n+(typeof row.data==='string'?Buffer.byteLength(row.data):0),0));return result;};const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;}});
  const rows=await f.state.rows(q);assert.equal(rows.length,count);assert.equal(rows[0].id,String(base));assert.equal(rows.at(-1).id,String(base+BigInt(count-1)));
  assert.ok(reads<=5,'remote latency must scale by byte batches, observed '+reads);assert.ok(maxBytes<=2*1024*1024);assert.ok(maxBytes>1024*1024);
 }finally{await f.close();}
});
pg('a disconnected interactive transaction rolls back and reconnects on the next operation',async()=>{
 const f=await postgresFixture();try{
  await assert.rejects(f.store.transaction(async tx=>{await tx.exec('INSERT INTO demand VALUES(?,?)','disconnected',Date.now());await tx.client.end();}),/Repository database unavailable/);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM demand')).one().n,0);await f.state.put('pr','7',{number:7,state:'OPEN'});
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM resources')).one().n,1);assert.equal((await f.store.health()).connected,true);
 }finally{await f.close();}
});

pg('a retention pin is acknowledged only after an in-flight sweep completes',async()=>{
 const f=await postgresFixture();let release;try{
  const old=Date.now()-40*86400000;
  await f.store.exec('INSERT INTO changes VALUES(?,?,?,?,?)',101,old,'pr','7',0);
  Object.assign(f.state,{storageLoaded:true,retentionPolicy:{mode:'enforce',disabled:['expired_repair_metrics','expired_retired_objects','expired_deliveries','expired_demand'],pinned:[]}});
  let entered,paused=false;const ready=new Promise(r=>entered=r),gate=new Promise(r=>release=r),source=f.store;
  f.state.store=new Proxy(source,{get(target,key){if(key==='exec')return async(sql,...args)=>{const rows=await target.exec(sql,...args);if(!paused && sql.startsWith('SELECT') && sql.includes('FROM changes') && sql.includes('ORDER BY')){paused=true;entered();await gate;}return rows;};const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;}});
  const sweep=f.state.expire();await ready;let accepted=false;
  const pin=f.state.storage('/storage/retention',new Request('https://internal/storage/retention',{method:'POST',body:JSON.stringify({policy:{...f.state.retentionPolicy,pinned:['expired_changes']}})})).then(()=>{accepted=true;});
  await new Promise(r=>setTimeout(r,30));assert.equal(accepted,false);release();await sweep;await pin;
  await f.store.exec('INSERT INTO changes VALUES(?,?,?,?,?)',102,old,'pr','8',0);await f.state.expire();
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM changes WHERE revision=?',102)).one().n,1);
 }finally{release?.();await f.close();}
});

pg('a missing close handshake cannot hold the PostgreSQL writer lane',async()=>{
 const f=await postgresFixture();try{
  const start=performance.now();
  await f.store.transaction(async tx=>{await tx.exec('INSERT INTO demand VALUES(?,?)','close-handshake',1);const close=tx.client.end.bind(tx.client);tx.client.end=async()=>{await close();return new Promise(()=>{});};});
  assert.ok(performance.now()-start<6000);await f.store.exec('INSERT INTO demand VALUES(?,?)','next-writer',2);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM demand')).one().n,2);
 }finally{await f.close();}
});
pg('PostgreSQL mutation and scheduling never write the obsolete SQLite alarm file',async()=>{
 const f=await postgresFixture();try{
  f.state.ctx.storage.setAlarm=async()=>{throw Error('obsolete SQLite file is full');};
  f.state.ctx.storage.getAlarm=async()=>{throw Error('obsolete alarm metadata must not be read');};
  await f.state.enqueue({repository:repo.name,operation:'pr',args:{number:7}},1,Date.now(),true);
  assert.ok(await f.state.nextJob());await f.state.setAlarm(Date.now());
 }finally{await f.close();}
});
pg('byte-cut history scans reduce subsequent SQL candidate work for large rows',async()=>{
 const f=await postgresFixture();try{
  const count=1000,q={repository:repo.name,operation:'issue_comments',args:{number:7}};
  for(let offset=0;offset<count;offset+=200){const ids=Array.from({length:200},(_,i)=>String(offset+i+1));await f.admin.query(`INSERT INTO ${f.schema}.resources(kind,id,parent,data,updated,observed,revision,tombstone) VALUES `+ids.map((_,i)=>`('issue_comments',$${i*2+1},'7',$${i*2+2},'',0,0,0)`).join(','),ids.flatMap(id=>[id,JSON.stringify({id,body:'x'.repeat(8000)})]));}
  await f.admin.query(`INSERT INTO ${f.schema}.members(collection,id) SELECT 'issue_comments:{"number":7}',id FROM ${f.schema}.resources`);await complete(f.state,q);
  const limits=[],source=f.store;f.state.store=new Proxy(source,{get(target,key){if(key==='exec')return async(sql,...args)=>{if(sql.includes('AS bounded_page'))limits.push(Number(sql.match(/LIMIT (\d+)/)[1]));return target.exec(sql,...args);};const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;}});
  const rows=await f.state.rows(q);assert.equal(rows.length,count);assert.equal(rows.at(-1).id,String(count));assert.ok(limits.length>=4);assert.equal(limits[0],4096);assert.ok(limits.slice(1).every(n=>n<600),'large rows must not repeatedly process the full candidate window: '+limits);
 }finally{await f.close();}
});

pg('publication prefetch preserves a large newer resource',async()=>{
 const f=await postgresFixture();try{
  const q={repository:repo.name,operation:'open_prs'},started=Date.now()-1000;
  const current={number:7,state:'OPEN',updatedAt:'2026-10-20T00:00:00Z',body:'retained webhook body '.repeat(1000)+'\u0000tail'};
  await f.state.put('pr','7',current,Date.now(),false,'7');
  await f.state.enqueue(q);const job=await f.state.claim(await f.state.nextJob());
  await f.store.exec('UPDATE jobs SET started=? WHERE key=?',started,job.key);job.started=started;
  await f.state.refresh(job,{api:'graphql',data:[{number:7,state:'CLOSED',updatedAt:current.updatedAt,body:'stale repair'}]});
  const stored=(await f.store.exec("SELECT data FROM resources WHERE kind='pr' AND id='7'")).one();
  assert.deepEqual(JSON.parse(stored.data),current);
  assert.equal((await f.store.exec("SELECT COUNT(*) n FROM members WHERE collection='open_prs:{}' AND id='7'")).one().n,1);
 }finally{await f.close();}
});

pg('grouped publication packs mixed old bodies and preserves duplicate, tombstone, webhook and delta state',async()=>{
 const f=await postgresFixture(),originalNow=Date.now;try{
  const fixed=Date.now(),updated=new Date(fixed).toISOString(),head='b'.repeat(40),records=[];
  for(let number=1;number<=270;number++)for(const id of [String(number),'pr:'+JSON.stringify({number})]){
   const value={number,state:'OPEN',updatedAt:updated,headRefOid:head,body:number%2?'small':'large '.repeat(4000)+(number===260?'\u0000tail':'')};
   records.push(['pr',id,'',JSON.stringify(value),updated,number===260?fixed+1:fixed-1,1,Number(number===261)]);
  }
  await f.admin.query('INSERT INTO '+f.schema+'.resources(kind,id,parent,data,updated,observed,revision,tombstone) VALUES '+records.map((_,i)=>'('+Array.from({length:8},(_,j)=>'$'+(i*8+j+1)).join(',')+')').join(','),records.flat());
  await f.state.setMeta('revision',1);
  const q={repository:repo.name,operation:'open_prs'};await f.state.enqueue(q);const job=await f.state.claim((await f.store.exec('SELECT * FROM jobs WHERE key=?','open_prs:{}')).one());
  job.started=fixed;await f.store.exec('UPDATE jobs SET started=? WHERE key=?',fixed,job.key);
  let pointReads=0,groups=0,maxOld=0;
  f.state.store=intercept(f.store,sql=>{if(/^SELECT (\*|data,updated,observed|data,tombstone) FROM resources WHERE kind=\? AND id=\?$/.test(sql))pointReads++;});
  const prime=f.state.primePublication;
  f.state.primePublication=async function(query,rows){
   const ids=rows.flatMap(row=>[String(row.number),'pr:'+JSON.stringify({number:row.number})]);
   const bytes=Number((await this.store.exec('SELECT COALESCE(SUM(octet_length(data)),0) bytes FROM resources WHERE kind=? AND id IN ('+ids.map(()=>'?').join(',')+')','pr',...ids)).one().bytes);
   groups++;maxOld=Math.max(maxOld,bytes);assert.ok(rows.length<=128);assert.ok(bytes<=2097152);
   return prime.call(this,query,rows);
  };
  Date.now=()=>fixed;
  const rows=Array.from({length:270},(_,i)=>({number:i+1,state:'OPEN',updatedAt:updated,headRefOid:head,body:'incoming'}));
  rows.push({...rows[0],state:'CLOSED',updatedAt:new Date(fixed+1).toISOString(),body:'duplicate final state'});
  await f.state.refresh(job,{api:'graphql',data:rows});
  assert.equal((await f.store.exec("SELECT COUNT(*) n FROM members WHERE collection='open_prs:{}'")).one().n,268);
  for(const id of ['1','261'])assert.equal((await f.store.exec("SELECT COUNT(*) n FROM members WHERE collection='open_prs:{}' AND id=?",id)).one().n,0);
  const newer=JSON.parse((await f.store.exec("SELECT data FROM resources WHERE kind='pr' AND id='260'")).one().data);
  assert.equal(newer.body.endsWith('\u0000tail'),true);
  assert.equal((await f.store.exec("SELECT tombstone FROM resources WHERE kind='pr' AND id='261'")).one().tombstone,1);
  assert.equal(JSON.parse((await f.store.exec("SELECT data FROM resources WHERE kind='pr' AND id=?",'pr:{"number":1}')).one().data).state,'CLOSED');
  const delta={repository:repo.name,operation:'pr_delta'};await f.state.enqueue(delta,0);
  const deltaJob=await f.state.claim((await f.store.exec('SELECT * FROM jobs WHERE key=?','pr_delta:{}')).one());
  const changed=rows.slice(0,180).map(row=>({...row,state:row.number===1?'OPEN':'CLOSED',updatedAt:new Date(fixed+2).toISOString()}));
  await f.state.refresh(deltaJob,{api:'graphql',data:changed});
  assert.equal((await f.store.exec("SELECT COUNT(*) n FROM members WHERE collection='open_prs:{}'")).one().n,90);
  const revision=await f.state.revision();await f.state.refresh(deltaJob,{api:'graphql',data:changed});
  assert.equal(await f.state.revision(),revision);
  assert.ok(groups>3);assert.ok(maxOld>1048576);assert.equal(pointReads,0,'large old bodies must remain batched');
 }finally{Date.now=originalNow;await f.close();}
});

pg('state projections tolerate payload Unicode escapes without changing the stored JSON text',async()=>{
 const f=await postgresFixture();try{
  const bodies=['null\u0000body','high\ud800body','low\udfffbody',String.raw`literal\u0000`,String.raw`literal\ud800`,'emoji😀','quotes"state":"CLOSED"'];
  for(const body of bodies){
   const text=JSON.stringify({body,state:'OPEN'});
   const row=(await f.store.exec("SELECT data original,json_extract(data,'$.state') state FROM (SELECT CAST(? AS TEXT) data) AS payload",text)).one();
   assert.equal(row.state,'OPEN');assert.ok(row.original===text,'stored payload text must remain exact');
  }
 }finally{await f.close();}
});

pg('committed CI batches finish without entering the PostgreSQL writer again',async()=>{
 const f=await postgresFixture();try{
  const events=Array.from({length:100},(_,i)=>({delivery:'ci-known-'+i,event:'workflow_run',received:Date.now()}));
  const request=()=>send(f.state,'/ci-receipts',{repository:repo.name,events});
  assert.equal((await request()).status,200);
  const atomic=f.state.atomic;let writes=0;
  f.state.atomic=async()=>{writes++;throw Error('writer is unavailable');};
  const response=await request();assert.equal(response.status,200);
  const report=await response.json();assert.deepEqual(report.committed,events.map(p=>p.delivery));assert.ok(report.timing.write_ms>=0);assert.equal(writes,0);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM deliveries')).one().n,100);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM changes')).one().n,0);
  f.state.ingress='parked';assert.equal((await request()).status,503);
  f.state.ingress='apply';f.state.migrationState={phase:'exporting'};assert.equal((await request()).status,503);
  f.state.migrationState=null;f.state.atomic=atomic;
 }finally{await f.close();}
});

pg('partially committed CI batches retain the ordinary PostgreSQL writer fence',async()=>{
 const f=await postgresFixture();try{
  const event={delivery:'ci-prior',event:'check_run',received:Date.now()},fresh={...event,delivery:'ci-fresh'};
  const request=events=>send(f.state,'/ci-receipts',{repository:repo.name,events});
  assert.equal((await request([event])).status,200);
  const prior=(await f.store.exec('SELECT received FROM deliveries WHERE id=?',event.delivery)).one().received;
  assert.equal((await request([event,fresh])).status,200);
  assert.equal((await f.store.exec('SELECT received FROM deliveries WHERE id=?',event.delivery)).one().received,prior,'prior commit timestamp is preserved');
  const missing={...event,delivery:'ci-missing'};
  await f.admin.query(`UPDATE ${f.schema}.backend_control SET generation='another-generation' WHERE singleton=1`);
  assert.equal((await request([event])).status,200,'acknowledging a prior receipt adds no write');
  assert.equal((await request([event,fresh,missing])).status,503,'new receipts still require the writer generation');
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM deliveries')).one().n,2);
 }finally{await f.close();}
});

pg('an ingress pause during CI receipt lookup drains without starting a write',async()=>{
 const f=await postgresFixture();let release;try{
  let entered;const gate=new Promise(r=>release=r),reading=new Promise(r=>entered=r),raw=f.state.store;
  f.state.store=new Proxy(raw,{get(target,key){
   if(key==='exec')return async(sql,...args)=>{const rows=await target.exec(sql,...args);if(sql.startsWith('SELECT id FROM deliveries WHERE id IN')){entered();await gate;}return rows;};
   const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;
  }});
  let writes=0;f.state.atomic=async()=>{writes++;throw Error('unexpected writer');};
  const batch=send(f.state,'/ci-receipts',{repository:repo.name,events:[{delivery:'ci-paused-lookup',event:'check_run',received:Date.now()}]});
  await reading;assert.equal(f.state.applying,1);
  const pause=send(f.state,'/storage/ingress',{mode:'parked'});for(let n=0;n<100&&f.state.ingress!=='parked';n++)await new Promise(r=>setTimeout(r,20));
  assert.equal(f.state.ingress,'parked');release();
  assert.equal((await batch).status,503);assert.equal(writes,0);assert.equal((await(await pause).json()).drained,true);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM deliveries')).one().n,0);
 }finally{release?.();await f.close();}
});

pg('CI acknowledgement survives receipt archival between its lookup and reply',async()=>{
 const f=await postgresFixture();try{
  const delivery='ci-archived-during-lookup';await f.store.exec('INSERT INTO deliveries VALUES(?,?,?)',delivery,'ignored:check_run',Date.now()-15*86400000);
  f.state.storageLoaded=true;const raw=f.state.store;
  f.state.store=new Proxy(raw,{get(target,key){
   if(key==='exec')return async(sql,...args)=>{const rows=await target.exec(sql,...args);if(sql.startsWith('SELECT id FROM deliveries WHERE id IN'))await f.state.archiveDeliveryReceipts();return rows;};
   const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;
  }});
  f.state.atomic=async()=>{throw Error('acknowledgement must not acquire the writer');};
  const response=await send(f.state,'/ci-receipts',{repository:repo.name,events:[{delivery,event:'check_run',received:Date.now()}]});
  assert.equal(response.status,200);assert.deepEqual((await response.json()).committed,[delivery]);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM deliveries')).one().n,0);
  const receipt=await(await f.env.ARCHIVE.get('receipts/100/'+delivery+'.json')).json();assert.equal(receipt.delivery,delivery);assert.equal(receipt.disposition,'not_mirrored');
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM changes')).one().n,0);
 }finally{await f.close();}
});

pg('a blackholed close cannot retain the server lock after a client timeout',async()=>{
 requireDirectCleanup();
 const f=await postgresFixture(),NativeSocket=globalThis.WebSocket,sockets=[];let calls=0,rollbacks=0,abandoned;
 try{
  globalThis.WebSocket=class extends NativeSocket {
   drop=false;closeCalled=false;
   constructor(...args){super(...args);sockets.push(this);}
   addEventListener(type,listener,...args){return super.addEventListener(type,type==='message'?event=>{if(!this.drop)listener.call(this,event);}:listener,...args);}
   close(...args){this.closeCalled=true;if(!this.drop)return super.close(...args);}
  };
  const start=performance.now();
  await assert.rejects(f.store.transaction(async tx=>{
   calls++;const query=tx.client.query.bind(tx.client);tx.client.query=(sql,...args)=>{if(sql==='ROLLBACK')rollbacks++;return query(sql,...args);};await tx.exec('INSERT INTO demand VALUES(?,?)','timed-out-write',Date.now());
   assert.equal(sockets.length,1);abandoned=tx.connectionState.session;tx.client.connectionParameters.query_timeout=100;sockets[0].drop=true;
   await tx.exec('SELECT pg_sleep(0.05)');
  }),/Repository database unavailable/);
  assert.ok(performance.now()-start<8000,'cleanup must confirm termination without waiting for the idle timeout');
  assert.equal((await f.admin.query('SELECT COUNT(*)::int AS n FROM pg_stat_activity WHERE pid=$1',[abandoned.pid])).rows[0].n,0);
  assert.equal(calls,1);assert.equal(rollbacks,0,'a client timeout must not submit ROLLBACK');assert.equal(sockets[0].closeCalled,true);
  await f.store.transaction(write=>write.exec('INSERT INTO demand VALUES(?,?)','next-writer',Date.now()));
  assert.deepEqual((await f.store.exec('SELECT key FROM demand ORDER BY key')).toArray(),[{key:'next-writer'}]);
 }finally{globalThis.WebSocket=NativeSocket;for(const socket of sockets)try{NativeSocket.prototype.close.call(socket);}catch{}await f.close();}
});

pg('a client timeout during a running server statement preserves the database writer fence and rolls back',async()=>{
 requireDirectCleanup();
 const f=await postgresFixture(),NativeSocket=globalThis.WebSocket;let socket,calls=0,rollbacks=0;
 try{
  globalThis.WebSocket=class extends NativeSocket {
   drop=false;
   constructor(...args){super(...args);socket=this;}
   addEventListener(type,listener,...args){return super.addEventListener(type,type==='message'?event=>{if(!this.drop)listener.call(this,event);}:listener,...args);}
  };
  await assert.rejects(f.store.transaction(async tx=>{
   calls++;const query=tx.client.query.bind(tx.client);tx.client.query=(sql,...args)=>{if(sql==='ROLLBACK')rollbacks++;return query(sql,...args);};await tx.exec('INSERT INTO demand VALUES(?,?)','mid-statement',Date.now());
   // Test-only shorter client timeout: the real server is still sleeping
   // when the client loses its response. Keep the production server settings.
   tx.client.connectionParameters.query_timeout=100;socket.drop=true;
   await tx.exec('SELECT pg_sleep(2)');
  }),/Repository database unavailable/);
  assert.equal(calls,1);assert.equal(rollbacks,0,'a client timeout must not submit ROLLBACK');
  // Socket closure can end the backend between the identity lookup and
  // termination, returning false. Respect the cleanup retry cooldown before
  // checking that the next writer acquires the same database ownership lock.
  await new Promise(resolve=>setTimeout(resolve,1100));
  await f.store.transaction(tx=>tx.exec('INSERT INTO demand VALUES(?,?)','after-mid-statement',Date.now()));
  assert.deepEqual((await f.store.exec('SELECT key FROM demand ORDER BY key')).toArray(),[{key:'after-mid-statement'}]);
 }finally{globalThis.WebSocket=NativeSocket;try{socket?.close();}catch{}await f.close();}
});


pg('lost ownership response is cleaned up before another transaction acquires the fence',async()=>{
 requireDirectCleanup();
 const f=await postgresFixture(),NativeSocket=globalThis.WebSocket,sockets=[];let callbacks=0;
 try{
  globalThis.WebSocket=class extends NativeSocket {
   drop=false;
   constructor(...args){super(...args);sockets.push(this);}
   send(data){if(Buffer.from(data).includes(Buffer.from('backend_control')))this.drop=true;return super.send(data);}
   addEventListener(type,listener,...args){return super.addEventListener(type,type==='message'?event=>{if(!this.drop)listener.call(this,event);}:listener,...args);}
   close(...args){if(!this.drop)return super.close(...args);}
  };
  const start=performance.now();await assert.rejects(f.store.transaction(async()=>callbacks++),/Repository database unavailable/);
  assert.ok(performance.now()-start<28000);assert.equal(callbacks,0);
  globalThis.WebSocket=NativeSocket;
  await f.store.transaction(tx=>tx.exec('INSERT INTO demand VALUES(?,?)','owner-response-recovered',Date.now()));
  assert.deepEqual((await f.store.exec('SELECT key FROM demand')).toArray(),[{key:'owner-response-recovered'}]);
 }finally{globalThis.WebSocket=NativeSocket;for(const socket of sockets)try{NativeSocket.prototype.close.call(socket);}catch{}await f.close();}
});

pg('cleanup identity guards cannot terminate a different session or a reused transaction',async()=>{
 const f=await postgresFixture();const {Client}=await import('@neondatabase/serverless');const other=new Client(process.env.TEST_POSTGRES_URL);await other.connect();
 try{
  await other.query('BEGIN');
  const token='unrelated-fixture-'+crypto.randomUUID();
  const identity=(await other.query("SELECT pg_backend_pid() AS pid,(EXTRACT(EPOCH FROM backend_start)*1000000)::bigint::text AS started_us,set_config('application_name',$1,true) AS token FROM pg_stat_activity WHERE pid=pg_backend_pid()",[token])).rows[0];
  for(const wrong of [{...identity,pid:0},{...identity,started_us:'1'},{...identity,token:'different-transaction'}]){
   f.store.discarded=[{...wrong,discardedAt:Date.now()}];f.store.nextCleanupAt=0;assert.equal(await f.store.cleanupDiscarded(),'already_ended');assert.equal((await other.query('SELECT 1 AS alive')).rows[0].alive,1);
  }
  // COMMIT resets the LOCAL token, even if the caller missed its response.
  await other.query('COMMIT');f.store.discarded=[{...identity,discardedAt:Date.now()}];f.store.nextCleanupAt=0;
  assert.equal(await f.store.cleanupDiscarded(),'already_ended');assert.equal((await other.query('SELECT 1 AS alive')).rows[0].alive,1);
  // The privileged fixture session is a different role and must never match.
  const adminToken='fixture-admin-'+crypto.randomUUID();
  const adminIdentity=(await f.admin.query("SELECT pg_backend_pid() AS pid,(EXTRACT(EPOCH FROM backend_start)*1000000)::bigint::text AS started_us,set_config('application_name',$1,false) AS token FROM pg_stat_activity WHERE pid=pg_backend_pid()",[adminToken])).rows[0];
  f.store.discarded=[{...adminIdentity,discardedAt:Date.now()}];f.store.nextCleanupAt=0;assert.equal(await f.store.cleanupDiscarded(),'already_ended');assert.equal((await f.admin.query('SELECT 1 AS alive')).rows[0].alive,1);
 }finally{await other.end();await f.close();}
});


pg('expired cleanup retains its session handle and cannot bypass the database ownership lock',async()=>{
 requireDirectCleanup();
 const f=await postgresFixture();const {Client}=await import('@neondatabase/serverless');const holder=new Client(process.env.TEST_POSTGRES_URL);holder.on('error',()=>{});await holder.connect();let released=false;
 try{
  await holder.query('BEGIN');
  const token='fixture-holder-'+crypto.randomUUID();
  const identity=(await holder.query("SELECT pg_backend_pid() AS pid,(EXTRACT(EPOCH FROM backend_start)*1000000)::bigint::text AS started_us,set_config('application_name',$1,true) AS token FROM pg_stat_activity WHERE pid=pg_backend_pid()",[token])).rows[0];
  await holder.query(`SELECT generation FROM ${f.schema}.backend_control WHERE singleton=1 FOR UPDATE`);
  f.store.discarded=[{...identity,discardedAt:Date.now()-45001}];let callbacks=0;
  const query=f.store.http.query.bind(f.store.http);f.store.http.query=async(...args)=>{if(args[0].includes('AS terminated FROM pg_stat_activity'))throw Error('HTTP-only cleanup outage');return query(...args);};
  await assert.rejects(f.store.transaction(async()=>callbacks++),/55P03/);assert.equal(callbacks,0);
  f.store.http.query=query;
  await f.store.transaction(tx=>tx.exec('INSERT INTO demand VALUES(?,?)','after-cleanup-grace',Date.now()));released=true;
  assert.equal(f.store.discarded.length,0);assert.equal((await f.admin.query('SELECT COUNT(*)::int AS n FROM pg_stat_activity WHERE pid=$1',[identity.pid])).rows[0].n,0);
  assert.deepEqual((await f.store.exec('SELECT key FROM demand')).toArray(),[{key:'after-cleanup-grace'}]);
 }finally{if(!released)try{await holder.query('ROLLBACK');}catch{}holder.neonConfig.destroy();await f.close();}
});

pg('concurrent raw receipt checks use one PostgreSQL read batch without entering a writer',async()=>{
 const f=await postgresFixture();try{
  const ids=Array.from({length:25},(_,i)=>'raw-known-'+i),known=ids.filter((_,i)=>i%2===0);
  await f.store.batch(known.map(id=>({sql:'INSERT INTO deliveries(id,event,received) VALUES(?,?,?)',bindings:[id,'issue_comment',Date.now()]})));
  f.store.transaction=async()=>assert.fail('receipt lookup must not enter a writer transaction');
  const transaction=f.store.http.transaction.bind(f.store.http);let readBatches=0;
  f.store.http.transaction=async(queries,options)=>{assert.equal(options.readOnly,true);assert.equal(queries.length,ids.length);readBatches++;return transaction(queries,options);};
  f.state.store=intercept(f.store,sql=>assert.match(sql,/^SELECT\b/,'raw lookup is stored reads only'));
  let requests=0;const env={...f.env,REPOSITORIES:{idFromName:id=>id,get:id=>({fetch:async(url,init)=>{assert.equal(id,repo.id);assert.equal(new URL(url).pathname,'/storage/deliveries');requests++;return f.state.fetch(new Request(url,init));}})}};
  const lookup=rawReceiptLookup(env),results=await Promise.all(ids.map(id=>lookup.known(repo,id)));assert.deepEqual(results,ids.map(id=>known.includes(id)));assert.equal(requests,1);assert.equal(readBatches,1);assert.equal(lookup.summary().known_lookups,known.length);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM deliveries')).one().n,known.length);for(const table of['changes','jobs','resources'])assert.equal((await f.store.exec('SELECT COUNT(*) n FROM '+table)).one().n,0);
 }finally{await f.close();}
});
