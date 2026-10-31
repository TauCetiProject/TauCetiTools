import {test} from 'node:test';
import assert from 'node:assert/strict';
import {fixture as sqliteFixture} from './migration_fixture.mjs';
import {postgresFixture} from './postgres_runtime.mjs';
async function fixture(){
 if(process.env.TEST_POSTGRES_URL)return postgresFixture();
 const f=await sqliteFixture();return {...f,store:f.state.store,objects:f.archive.objects,close:async()=>f.db.close()};
}
const query=number=>({repository:'org/repo',operation:'issue_comments',args:{number}}),key=number=>'issue_comments:'+JSON.stringify({number});
async function seed(f,count=4){
 const qs=Array.from({length:count},(_,i)=>query(100+i));await f.state.enqueueMany(qs.map(q=>({q,priority:5})),true);
 return qs;
}
function upstream(f,make=async jobs=>jobs.map(j=>({api:'graphql',data:[{id:String(j.query.args.number),body:'fetched',created_at:'2026-10-31T00:00:00Z'}]}))){
 let calls=0;f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async(url,init)=>{
  calls++;assert.equal(url,'https://internal/batch');return Response.json({results:await make(JSON.parse(init.body).jobs)});
 }})};return ()=>calls;
}
function intercept(store,check){return new Proxy(store,{get(target,prop){
 if(prop==='transaction')return(apply,purpose)=>target.transaction(tx=>apply(intercept(tx,check)),purpose);
 if(prop==='exec')return async(sql,...args)=>{await check(target,sql,args);return target.exec(sql,...args);};
 if(prop==='batch')return async statements=>{for(const s of statements)await check(target,s.sql,s.bindings||[]);return target.batch(statements);};
 const value=Reflect.get(target,prop,target);return typeof value==='function'?value.bind(target):value;
 }});}
async function dispatch(f){await f.state.dispatch(await f.state.claim(await f.state.nextJob()));}

test('small histories share publication transactions and retain complete upstream batching',async()=>{
 const f=await fixture();try{
  const qs=await seed(f),calls=upstream(f);let publications=0;
  const atomic=f.state.atomic;f.state.atomic=function(apply,purpose){if(purpose==='publication')publications++;return atomic.call(this,apply,purpose);};
  await dispatch(f);assert.equal(calls(),1);assert.equal(publications,2);
  for(const q of qs){assert.equal(await f.state.complete(q),true);assert.equal((await f.state.rows(q))[0].body,'fetched');}
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs WHERE lease_id IS NOT NULL')).one().n,0);
  assert.equal(f.state.repairMetrics.summary().repair_hour.find(x=>x.event==='completion').n,4);
 }finally{await f.close();}
});

test('rolled-back member SQL failure replays intact input and lets healthy siblings finish once',async()=>{
 const f=await fixture();try{
  const qs=await seed(f),calls=upstream(f);
  if(f.store.backend==='sqlite')await f.store.exec('CREATE TABLE fixture_constraint(id INTEGER CHECK(id>0))');
  f.state.store=intercept(f.store,async(store,sql,args)=>{
   if(!/INTO collections/.test(sql) || !args.includes(key(101)))return;
   if(store.backend==='postgres')await store.exec('SELECT CAST(? AS BIGINT)','invalid-bigint');
   else await store.exec('INSERT INTO fixture_constraint VALUES(?)',-1);
  });
  await dispatch(f);assert.equal(calls(),1);
  for(const q of qs)assert.equal(await f.state.complete(q),q.args.number!==101);
  for(const n of [100,102,103])assert.equal((await f.state.rows(query(n)))[0].body,'fetched','fallback must not consume or lose prepared data');
  const bad=(await f.store.exec('SELECT * FROM jobs WHERE key=?',key(101))).one();assert.equal(bad.attempt,1);assert.equal(bad.wait_reason,'transport_or_parser');
  assert.equal((await f.store.exec("SELECT COUNT(*) n FROM resources WHERE kind='issue_comments' AND id='101'")).one().n,0);
  assert.equal(f.state.repairMetrics.summary().repair_hour.find(x=>x.event==='completion').n,3,'rolled-back attempts are not committed completions');
 }finally{await f.close();}
});

test('unknown publication commit does not replay or retry already committed members',async()=>{
 const f=await fixture();try{
  await seed(f);const calls=upstream(f),transaction=f.store.transaction.bind(f.store);let injected=false;
  f.store.transaction=async(apply,purpose)=>{const result=await transaction(apply,purpose);if(purpose==='publication'&&!injected){injected=true;throw Error('commit outcome unknown');}return result;};
  await dispatch(f);assert.equal(calls(),1);assert.equal(injected,true);
  for(const n of [100,101]){assert.equal(await f.state.complete(query(n)),true);const j=(await f.store.exec('SELECT * FROM jobs WHERE key=?',key(n))).one();assert.equal(j.attempt,0);assert.equal(j.wait_reason,null);assert.ok(j.due>Date.now()+10000);}
  for(const n of [102,103]){assert.equal(await f.state.complete(query(n)),false);const j=(await f.store.exec('SELECT * FROM jobs WHERE key=?',key(n))).one();assert.equal(j.attempt,0);assert.equal(j.wait_reason,null);assert.equal(j.lease_id,null);}
 }finally{await f.close();}
});

test('a replacement lease is excluded while a newer webhook generation still publishes safely',async()=>{
 const f=await fixture();try{
  await seed(f,2);const calls=upstream(f,async jobs=>{
   await f.store.exec('UPDATE jobs SET lease_id=?,lease_until=? WHERE key=?','replacement',Date.now()+60000,key(101));
   const body={action:'created',issue:{number:100},comment:{id:'100',body:'new webhook',created_at:'2026-10-31T00:00:00Z',updated_at:'2026-10-31T01:00:00Z'}};
   await f.env.ARCHIVE.put('fixture/newer',JSON.stringify(body));
   await f.state.ingest({repository:'org/repo',key:'fixture/newer',delivery:'newer',event:'issue_comment',received:Date.now()+10});
   return jobs.map(()=>({api:'graphql',data:[{id:'100',body:'older fetched',created_at:'2026-10-31T00:00:00Z',updated_at:'2026-10-31T00:00:00Z'}]}));
  });
  await dispatch(f);assert.equal(calls(),1);assert.equal(await f.state.complete(query(100)),true);assert.equal((await f.state.rows(query(100)))[0].body,'new webhook');
  const changed=(await f.store.exec('SELECT * FROM jobs WHERE key=?',key(100))).one();assert.equal(changed.dirty,1);assert.ok(changed.generation>changed.start_generation);assert.ok(changed.due<=Date.now());
  const replacement=(await f.store.exec('SELECT * FROM jobs WHERE key=?',key(101))).one();assert.equal(replacement.lease_id,'replacement');assert.equal(replacement.attempt,0);assert.equal(await f.state.complete(query(101)),false);
 }finally{await f.close();}
});

for(const mode of ['primary','secondary','capacity','routing','transport'])test(`100-member ${mode} bookkeeping uses bounded transactions and retains wait semantics`,async()=>{
 const f=await fixture();try{
  await seed(f,100);const until=Date.now()+300000;let calls=0,transactions=0;
  const tx=f.store.transaction.bind(f.store);f.store.transaction=(...args)=>{transactions++;return tx(...args);};
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>{
   calls++;if(mode==='transport')throw Error('upstream transport');
   return Response.json({deferred:true,category:mode==='primary'?'primary:graphql':mode==='routing'?'routing:core':mode,retry_at:until});
  }})};
  await dispatch(f);assert.equal(calls,1);assert.ok(transactions<=8,'wait/routing/retry must not open one transaction per member');
  const jobs=(await f.store.exec('SELECT * FROM jobs')).toArray();assert.equal(jobs.length,100);
  for(const j of jobs){assert.equal(j.lease_id,null);assert.equal(j.dirty,0);
   if(mode==='routing'||mode==='primary'){assert.equal(j.api,'core');assert.equal(j.attempt,0);assert.ok(j.due<=Date.now());}
   else if(mode==='capacity'){assert.equal(j.attempt,0);assert.equal(j.wait_reason,'capacity');assert.equal(j.due,until);}
   else if(mode==='secondary'){assert.equal(j.attempt,1);assert.equal(j.wait_reason,'secondary');assert.equal(j.due,until);}
   else{assert.equal(j.attempt,1);assert.equal(j.wait_reason,'transport_or_parser');assert.ok(j.due>Date.now());}
  }
 }finally{await f.close();}
});

test('oversized flat histories continue individually without losing smaller siblings',async()=>{
 const f=await fixture();try{
  const qs=await seed(f,3),body='x'.repeat(300000);const calls=upstream(f,async jobs=>jobs.map(j=>({api:'graphql',data:[{id:String(j.query.args.number),body:j.query.args.number===101?body:'small',created_at:'2026-10-31T00:00:00Z'}]})));
  await dispatch(f);assert.equal(calls(),1);for(const q of qs)assert.equal(await f.state.complete(q),true);
  assert.equal((await f.state.rows(query(101)))[0].body,body);
 }finally{await f.close();}
});


test('100 preparation failures retain jobs with one grouped retry transaction',async()=>{
 const f=await fixture();try{
  await seed(f,100);const calls=upstream(f),tx=f.store.transaction.bind(f.store);let transactions=0;
  f.store.transaction=(...args)=>{transactions++;return tx(...args);};
  f.env.ARCHIVE.put=async()=>{throw Error('R2 preparation unavailable');};
  await dispatch(f);assert.equal(calls(),1);assert.ok(transactions<=8);
  const rows=(await f.store.exec('SELECT attempt,wait_reason,lease_id FROM jobs')).toArray();assert.equal(rows.length,100);
  for(const row of rows){assert.equal(row.attempt,1);assert.equal(row.wait_reason,'transport_or_parser');assert.equal(row.lease_id,null);}
 }finally{await f.close();}
});

test('thread publication preserves an independently owned flat-history repair',async()=>{
 const f=await fixture();try{
  const flat={repository:'org/repo',operation:'review_comments',args:{number:7}},thread={...flat,operation:'review_threads'},flatKey='review_comments:{"number":7}';
  await f.state.enqueueMany([{q:flat,priority:5},{q:thread,priority:5}],true);
  const due=Date.now()+3600000;await f.store.exec('UPDATE jobs SET lease_id=?,lease_until=?,attempt=2,wait_reason=?,due=? WHERE key=?','independent-owner',Date.now()+60000,'primary:core',due,flatKey);
  const source=(await f.store.exec('SELECT * FROM jobs WHERE key=?','review_threads:{"number":7}')).one();
  await f.state.refresh(await f.state.claim(source),{api:'graphql',data:[]});
  assert.equal(await f.state.complete(flat),true);
  const parent=(await f.store.exec('SELECT * FROM jobs WHERE key=?',flatKey)).one();assert.equal(parent.lease_id,'independent-owner');assert.equal(parent.wait_reason,'primary:core');assert.equal(parent.attempt,2);assert.equal(parent.due,due);
 }finally{await f.close();}
});


test('a later uncertain commit preserves an individually published member and an exact capacity wait',async()=>{
 const f=await fixture();try{
  await seed(f,6);const until=Date.now()+300000,large='x'.repeat(300000);
  const calls=upstream(f,async jobs=>jobs.map(j=>j.query.args.number===101?{deferred:true,category:'capacity',retry_at:until}:{api:'graphql',data:[{id:String(j.query.args.number),body:j.query.args.number===100?large:'small',created_at:'2026-10-31T00:00:00Z'}]}));
  const tx=f.store.transaction.bind(f.store);let publications=0;
  f.store.transaction=async(apply,purpose)=>{const result=await tx(apply,purpose);if(purpose==='publication'&&++publications===2)throw Error('uncertain commit after individual and deferred members');return result;};
  await dispatch(f);assert.equal(calls(),1);
  for(const n of [100,102,103]){assert.equal(await f.state.complete(query(n)),true);const j=(await f.store.exec('SELECT * FROM jobs WHERE key=?',key(n))).one();assert.equal(j.attempt,0);assert.equal(j.wait_reason,null);}
  const deferred=(await f.store.exec('SELECT * FROM jobs WHERE key=?',key(101))).one();assert.equal(deferred.due,until);assert.equal(deferred.attempt,0);assert.equal(deferred.wait_reason,'capacity');
  for(const n of [104,105])assert.equal((await f.store.exec('SELECT attempt FROM jobs WHERE key=?',key(n))).one().attempt,0);
 }finally{await f.close();}
});

test('non-replayable publication failure stops individual members without one connection attempt per result',async()=>{
 const f=await fixture();try{
  const qs=Array.from({length:8},(_,i)=>({...query(100+i),operation:'review_threads'}));await f.state.enqueueMany(qs.map(q=>({q,priority:5})),true);
  const calls=upstream(f,async jobs=>jobs.map(()=>({api:'graphql',data:[]}))),tx=f.store.transaction.bind(f.store);let publications=0;
  f.store.transaction=async(apply,purpose)=>{if(purpose==='publication'){publications++;throw Error('non-replayable connection failure');}return tx(apply,purpose);};
  await dispatch(f);assert.equal(calls(),1);assert.equal(publications,1);
  for(const q of qs)assert.equal(await f.state.complete(q),false);
 }finally{await f.close();}
});

for(const outcome of ['rolled_back','uncertain_commit','cleanup_unavailable'])test(`a middle chunk ${outcome} preserves untouched members of a full upstream batch`,async()=>{
 const f=await fixture(),now=Date.now;try{
  await seed(f,100);const calls=upstream(f,async jobs=>jobs.map(()=>({api:'graphql',data:[]})));
  const before=new Map((await f.store.exec('SELECT key,due,attempt,requested_at,generation FROM jobs')).toArray().map(j=>[j.key,j]));
  const tx=f.store.transaction.bind(f.store);let publications=0,blocked=false;
  f.state.store=new Proxy(f.store,{get(target,prop){
   if(prop==='transaction')return async(apply,purpose)=>{
    if(blocked)throw Error('cleanup_pending');
    if(purpose!=='publication'||++publications!==3)return tx(apply,purpose);
    if(outcome==='uncertain_commit'){await tx(apply,purpose);throw Error('commit outcome unknown');}
    try{return await tx(async store=>{
     await apply(store);
     if(store.backend==='postgres'){
      await store.exec("SELECT set_config('statement_timeout','20ms',true)");await store.exec('SELECT pg_sleep(0.1)');
     }else await store.exec('SELECT * FROM fixture_missing_backend_table');
    },purpose);}catch(error){blocked=outcome==='cleanup_unavailable';throw error;}
   };
   if(prop==='exec')return(sql,...args)=>{if(blocked&&!/^\s*SELECT\b/i.test(sql))throw Error('cleanup_pending');return target.exec(sql,...args);};
   const value=Reflect.get(target,prop,target);return typeof value==='function'?value.bind(target):value;
  }});
  if(outcome==='cleanup_unavailable')await assert.rejects(dispatch(f),/cleanup_pending/);else await dispatch(f);
  assert.equal(calls(),1);assert.equal(publications,3);
  const rows=(await f.store.exec('SELECT * FROM jobs')).toArray(),complete=(await f.store.exec("SELECT COUNT(*) n FROM collections WHERE operation='issue_comments' AND complete=1")).one().n;
  assert.equal(complete,outcome==='uncertain_commit'?6:4);
  for(const j of rows){
   const n=JSON.parse(j.query).args.number,prior=before.get(j.key);
   assert.equal(j.generation,prior.generation);assert.equal(j.requested_at,prior.requested_at);
   if(n>=106){assert.equal(j.attempt,prior.attempt);assert.equal(j.due,prior.due);assert.equal(j.wait_reason,null);}
   if(n===104||n===105)assert.equal(j.attempt,outcome==='rolled_back'?1:0);
   if(outcome==='cleanup_unavailable'&&n>=104)assert.ok(j.lease_id,'failed cleanup leaves durable ownership for expiry recovery');
   else assert.equal(j.lease_id,null);
  }
  if(outcome==='cleanup_unavailable'){
   blocked=false;Date.now=()=>now()+65000;
   await dispatch(f);assert.equal(calls(),2);
   assert.equal((await f.store.exec("SELECT COUNT(*) n FROM collections WHERE operation='issue_comments' AND complete=1")).one().n,100,'ordinary selection recovers after lease expiry');
  }
 }finally{Date.now=now;await f.close();}
});

test('a separate PostgreSQL reader cannot see partial collection publication',{skip:!process.env.TEST_POSTGRES_URL},async()=>{
 const f=await fixture();try{
  await seed(f,2);upstream(f);let observed=false;
  f.state.store=intercept(f.store,async(store,sql,args)=>{
   if(observed||!sql.includes('INTO collections')||!args.includes(key(101)))return;
   observed=true;const r=await f.admin.query(`SELECT (SELECT COUNT(*) FROM ${f.schema}.collections WHERE complete=1)::int complete,(SELECT COUNT(*) FROM ${f.schema}.resources WHERE kind='issue_comments')::int resources`);
   assert.deepEqual(r.rows[0],{complete:0,resources:0});
  });
  await dispatch(f);assert.equal(observed,true);assert.equal((await f.admin.query(`SELECT COUNT(*)::int n FROM ${f.schema}.collections WHERE complete=1`)).rows[0].n,2);
 }finally{await f.close();}
});
