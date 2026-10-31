import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {RepositoryState,postgresFixture} from './postgres_runtime.mjs';
async function fixture(){
 if(process.env.TEST_POSTGRES_URL)return postgresFixture();
 const db=new DatabaseSync(':memory:');let alarm=null;
 const sql={exec(query,...args){let rows=[];if(!args.length&&query.includes(';'))db.exec(query);else{const statement=db.prepare(query);if(statement.columns().length)rows=statement.all(...args);else statement.run(...args);}return {toArray:()=>rows,one:()=>{assert.equal(rows.length,1);return rows[0];}};}};
 const ctx={id:{toString:()=>'100'},waitUntil:()=>{},blockConcurrencyWhile:fn=>fn(),storage:{sql,getAlarm:async()=>alarm,setAlarm:async at=>{alarm=at;},transactionSync(fn){db.exec('SAVEPOINT tx');try{const r=fn();db.exec('RELEASE tx');return r;}catch(e){db.exec('ROLLBACK TO tx');db.exec('RELEASE tx');throw e;}},async transaction(fn){db.exec('SAVEPOINT atx');try{const r=await fn();db.exec('RELEASE atx');return r;}catch(e){db.exec('ROLLBACK TO atx');db.exec('RELEASE atx');throw e;}}}};
 const objects=new Map();
 const env={ALLOWED_REPOSITORIES:JSON.stringify([{id:'100',name:'org/repo',installation:'200'}]),ARCHIVE:{head:async key=>objects.has(key)?{key}:null,get:async key=>objects.has(key)?{text:async()=>objects.get(key),json:async()=>JSON.parse(objects.get(key))}:null}};
 const state=new RepositoryState(ctx,env);await state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
 return {state,store:state.store,objects,close:async()=>db.close()};
}

test('event bulk scheduling preserves individual enqueue semantics across coverage, demand, retries and leases',async()=>{
 const f=await fixture();try{
  const fixed=Date.now(),realNow=Date.now;Date.now=()=>fixed;
  try{
   const inputs=[];
   for(let bits=0;bits<64;bits++){
    const q={repository:'org/repo',operation:bits%2?'review_threads':'statuses',args:bits%2?{number:bits+1}:{sha:bits.toString(16).padStart(40,'0')}},key=q.operation+':'+JSON.stringify(q.args);
    inputs.push({q,priority:bits%3,dirty:Boolean(bits&1),dirtyIfMissing:Boolean(bits&2),missingOnly:Boolean(bits&4)});
   }
   await f.state.atomic(async state=>{
    for(let bits=0;bits<inputs.length;bits++){
     const {q}=inputs[bits],key=q.operation+':'+JSON.stringify(q.args);
     if(bits&8)await state.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)',key,q.operation,JSON.stringify(q.args),1,fixed,0,null);
     if(bits&16)await state.store.exec('INSERT INTO demand VALUES(?,?)',key,fixed);
     if(bits&32)await state.store.exec('INSERT INTO jobs(key,query,priority,due,dirty,generation,lane,requested_at,attempt,wait_reason,lease_id,lease_until,started,start_generation,page,cursor) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',key,JSON.stringify(q),20,bits&2?fixed-3600000:fixed+3600000,Number(Boolean(bits&4)),9,bits&2?'requested':'verification',bits&1?fixed-100000:null,bits&8?2:0,bits&16?'primary:core':bits&4?'':null,'existing-owner',fixed+60000,fixed-5000,8,3,'retained-cursor');
    }
   });
   const before=(await f.store.exec('SELECT * FROM jobs')).toArray();
   await f.state.atomic(async state=>{
    for(const {q,priority,dirty,dirtyIfMissing,missingOnly} of inputs){const done=await state.complete(q);if(missingOnly&&done)continue;await state.enqueue(q,priority,fixed,dirty||dirtyIfMissing&&!done);}
   });
   const expected=(await f.store.exec('SELECT * FROM jobs ORDER BY key')).toArray().map(({rowid,...row})=>row);
   await f.state.atomic(async state=>{await state.store.exec('DELETE FROM jobs');for(const row of before){const keys=Object.keys(row).filter(key=>key!=='rowid');await state.store.exec('INSERT INTO jobs('+keys.join(',')+') VALUES('+keys.map(()=>'?').join(',')+')',...keys.map(k=>row[k]));}await state.enqueueIngest(inputs);});
   const actual=(await f.store.exec('SELECT * FROM jobs ORDER BY key')).toArray().map(({rowid,...row})=>row);
   assert.deepEqual(actual,expected);
  }finally{Date.now=realNow;}
 }finally{await f.close();}
});

test('bulk event duplicates preserve monotonic dirty generations and last priority',async()=>{
 const f=await fixture();try{
  const q={repository:'org/repo',operation:'review_threads',args:{number:7}};
  await f.state.atomic(async state=>state.enqueueIngest([{q,priority:2,dirty:true},{q,priority:1,dirty:true},{q,priority:0}]));
  const row=(await f.store.exec('SELECT * FROM jobs')).one();assert.equal(row.generation,2);assert.equal(row.dirty,1);assert.equal(row.priority,0);assert.equal(row.lane,'active');
 }finally{await f.close();}
});

test('event scheduling preserves an earlier SQLite alarm for unrelated work',{skip:Boolean(process.env.TEST_POSTGRES_URL)},async()=>{
 const f=await fixture();try{
  const q={repository:'org/repo',operation:'review_threads',args:{number:7}},now=Date.now(),earlier=now+10;
  await f.state.enqueue(q,20,now+3600000);
  await f.state.ctx.storage.setAlarm(earlier);
  await f.state.atomic(state=>state.enqueueIngest([{q,priority:2}]));
  assert.equal(await f.state.ctx.storage.getAlarm(),earlier);
 }finally{await f.close();}
});


test('failed event commit exposes no partial state and replay commits exactly once',async()=>{
 const f=await fixture();try{
  const body={action:'created',issue:{number:7},comment:{id:'99',body:'retained',created_at:'2026-10-31T04:00:00Z'}},pointer={repository:'org/repo',key:'fixture/input',delivery:'delivery',event:'issue_comment',received:Date.now()};
  f.objects.set(pointer.key,JSON.stringify(body));
  const commit=f.state.commitReceipt;let fail=true;
  f.state.commitReceipt=async function(p){await commit.call(this,p);if(fail)throw Error('injected before commit');};
  await assert.rejects(f.state.ingest(pointer),/injected before commit/);
  for(const table of ['resources','members','jobs','deliveries','changes'])assert.equal((await f.store.exec('SELECT COUNT(*) n FROM '+table)).one().n,0,table);
  assert.equal(f.state.ingestStats.size,0,'a rolled-back delivery contributes no committed metrics');
  fail=false;assert.equal(await f.state.ingest(pointer),true);assert.equal(await f.state.ingest(pointer),false);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM deliveries')).one().n,1);
  assert.equal(JSON.parse((await f.store.exec("SELECT data FROM resources WHERE kind='issue_comments' AND id='99'")).one().data).body,'retained');
  const stats=f.state.ingestStats.get('issue_comment');assert.equal(stats.deliveries,1);if(f.store.backend==='postgres'){assert.equal(stats.sql_samples,1);assert.ok(stats.sql_round_trips>0);}
 }finally{await f.close();}
});
