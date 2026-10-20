import {test} from 'node:test';
import assert from 'node:assert/strict';
import {postgresFixture,PostgresStore,postgresSQL} from './postgres_runtime.mjs';
const enabled=Boolean(process.env.TEST_POSTGRES_URL && process.env.TEST_POSTGRES_MIGRATION_URL);
const pg=(name,fn)=>test(name,{skip:!enabled},fn);
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
  const conditional=await f.state.fetch(new Request('https://internal/query',{method:'POST',headers:{'if-none-match':initialResponse.headers.get('etag')},body:JSON.stringify(q)}));assert.equal(conditional.status,304);
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
  assert.ok(statements<300,'SQL statement count must scale by batches, observed '+statements);
  assert.equal(await f.state.complete(q),true);assert.equal((await f.store.exec("SELECT COUNT(*) n FROM members WHERE collection='open_prs:{}'")).one().n,1200);
  console.log(JSON.stringify({publication_rows:1200,sql_statements:statements,duration_ms:duration}));
  statements=0;await f.state.enqueue(q);await f.store.exec('UPDATE jobs SET lease_id=NULL,lease_until=0,due=0 WHERE key=?',job.key);const unchanged=await f.state.claim((await f.store.exec('SELECT * FROM jobs WHERE key=?',job.key)).one());await f.state.refresh(unchanged,{api:'graphql',data});
  assert.ok(statements<330,'unchanged observations must also use set writes, observed '+statements);
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
