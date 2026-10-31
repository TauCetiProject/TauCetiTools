import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {RepositoryState,postgresFixture} from './postgres_runtime.mjs';
async function fixture(){
 if(process.env.TEST_POSTGRES_URL)return postgresFixture();
 const db=new DatabaseSync(':memory:');let alarm=null;
 const sql={exec(query,...args){let rows=[];if(!args.length&&query.includes(';'))db.exec(query);else{const statement=db.prepare(query);if(statement.columns().length)rows=statement.all(...args);else statement.run(...args);}return {toArray:()=>rows,one:()=>{assert.equal(rows.length,1);return rows[0];}};}};
 const ctx={id:{toString:()=>'100'},waitUntil:()=>{},blockConcurrencyWhile:fn=>fn(),storage:{sql,getAlarm:async()=>alarm,setAlarm:async at=>{alarm=at;},transactionSync(fn){db.exec('SAVEPOINT tx');try{const r=fn();db.exec('RELEASE tx');return r;}catch(e){db.exec('ROLLBACK TO tx');db.exec('RELEASE tx');throw e;}},async transaction(fn){db.exec('SAVEPOINT atx');try{const r=await fn();db.exec('RELEASE atx');return r;}catch(e){db.exec('ROLLBACK TO atx');db.exec('RELEASE atx');throw e;}}}};
 const env={ALLOWED_REPOSITORIES:JSON.stringify([{id:'100',name:'org/repo',installation:'200'}]),ARCHIVE:{get:async()=>null}};
 const state=new RepositoryState(ctx,env);await state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
 return {state,store:state.store,close:async()=>db.close()};
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
     if(bits&32)await state.store.exec('INSERT INTO jobs(key,query,priority,due,dirty,generation,lane,requested_at,attempt,wait_reason,lease_id,lease_until,started,start_generation,page,cursor) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',key,JSON.stringify(q),20,fixed+3600000,1,9,'verification',fixed-100000,2,'primary:core','existing-owner',fixed+60000,fixed-5000,8,3,'retained-cursor');
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
