import {test} from 'node:test';
import assert from 'node:assert/strict';
import {fixture as sqliteFixture} from './migration_fixture.mjs';
import {postgresFixture} from './postgres_runtime.mjs';
import {drainRepairs} from './repair_background.mjs';
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
const query=(number,operation='files')=>({repository:'org/repo',operation,args:{number}});
const key=(number,operation='files')=>operation+':'+JSON.stringify({number});
const flush=()=>new Promise(setImmediate);
async function bounded(promise,label,limit=process.env.TEST_POSTGRES_URL?180000:10000){
 let timer;try{return await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error(label)),limit);})]);}finally{clearTimeout(timer);}
}
async function fixture(){
 const f=process.env.TEST_POSTGRES_URL?await postgresFixture():await sqliteFixture();
 const store=f.store || f.state.store;
 f.state.expire=async()=>({});f.state.requestOpenCoverage=async()=>{};f.state.maintenance=async()=>{};f.state.startStorageMaintenance=()=>{};
 return {...f,store,close:f.close || (async()=>f.db.close())};
}
async function seed(f,count,operation='files'){
 const queries=Array.from({length:count},(_,i)=>query(100+i,operation));
 await f.state.enqueueMany(queries.map(q=>({q,priority:5})),true);return queries;
}
function observeProgress(state){
 const original=state.settleRepairJobs,seen=new Map(),listeners=[];
 state.settleRepairJobs=function(jobs,effect){
  original.call(this,jobs,effect);
  if(effect==='progress')for(const job of jobs)seen.set(job.key,(seen.get(job.key)||0)+1);
  for(const listener of listeners)listener();
 };
 return {seen,when:predicate=>new Promise(resolve=>{const check=()=>{if(predicate(seen))resolve();};listeners.push(check);check();})};
}

test('real repair slots refill beyond sixteen launches while one group remains pending',async()=>{
 const f=await fixture(),gate=deferred();try{
  const queries=await seed(f,21),progress=observeProgress(f.state);let held;
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async(_url,init)=>{
   const q=JSON.parse(init.body).query;if(!held){held=key(q.args.number);await gate.promise;}
   return Response.json({api:'core',data:[]});
  }})};
  // A completed transaction scope must still launch through its actual owner.
  const scope=await f.state.atomic(state=>state);
  await bounded(scope.runAlarm(),'alarm waited for a slow group');
  await bounded(progress.when(seen=>seen.size===20),'freed slots did not continue past the launch budget');
  await f.state.repairCoordinator.flight;
  assert.equal(f.state.repairCoordinator.groups.size,1);assert.ok(f.state.activeOperations>=1);
  const completed=(await f.store.exec("SELECT COUNT(*) n FROM collections WHERE operation='files' AND complete=1")).one().n;
  assert.equal(completed,20,'an independent reader sees fast work committed before the held response');
  assert.equal(await f.state.complete(queries.find(q=>key(q.args.number)===held)),false);
  gate.resolve();await drainRepairs(f.state);assert.equal(f.state.activeOperations,0);
  assert.equal((await f.store.exec("SELECT COUNT(*) n FROM collections WHERE operation='files' AND complete=1")).one().n,21);
 }finally{gate.resolve();await drainRepairs(f.state);await f.close();}
});

test('four active full batches remain excluded after lease expiry and renew only matching tokens',async()=>{
 const f=await fixture(),gate=deferred(),ready=deferred(),clock=Date.now;let offset=0;try{
  await seed(f,400,'issue_comments');
  await f.store.exec("UPDATE jobs SET lane=CASE WHEN CAST(json_extract(query,'$.args.number') AS INTEGER)<200 THEN 'requested' WHEN CAST(json_extract(query,'$.args.number') AS INTEGER)<300 THEN 'active' WHEN CAST(json_extract(query,'$.args.number') AS INTEGER)<400 THEN 'verification' ELSE 'archive' END");
  let calls=0;f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async(_url,init)=>{
   const input=JSON.parse(init.body);assert.equal(input.jobs.length,100);if(++calls===4)ready.resolve();await gate.promise;
   return Response.json({results:input.jobs.map(()=>({api:'graphql',data:[]}))});
  }})};
  await bounded(f.state.runAlarm(),'full batch claiming did not finish');await bounded(ready.promise,'four full upstream groups were not launched');
  assert.equal(f.state.repairCoordinator.owned.size,400);
  Date.now=()=>clock()+offset;offset=70000;
  await f.state.enqueue(query(999),5);
  assert.equal(JSON.parse((await f.state.nextJob()).query).args.number,999,'exclusion applies before the candidate limit');
  const replacementUntil=Date.now()+300000;
  await f.store.exec('UPDATE jobs SET lease_id=?,lease_until=? WHERE key=?','replacement',replacementUntil,key(100,'issue_comments'));
  await f.state.atomic(async()=>{},'repair');
  const renewed=(await f.store.exec("SELECT COUNT(*) n FROM jobs WHERE lease_id IS NOT NULL AND lease_id!='replacement' AND lease_until>?",Date.now()+50000)).one().n;
  assert.equal(renewed,399);assert.equal(f.state.renewedLeases,399);
  assert.equal((await f.store.exec('SELECT lease_until FROM jobs WHERE key=?',key(100,'issue_comments'))).one().lease_until,replacementUntil);
  const before=calls;await f.state.runAlarm();assert.equal(calls,before,'full slots do not duplicate an expired local fetch');
  // Keep this case focused on the held groups; its unrelated work was only peeked.
  await f.store.exec('DELETE FROM jobs WHERE key=?',key(999));offset=0;gate.resolve();await drainRepairs(f.state);
  assert.equal(calls,4);assert.equal((await f.store.exec("SELECT COUNT(*) n FROM collections WHERE operation='issue_comments' AND complete=1")).one().n,399);
  assert.equal((await f.store.exec('SELECT lease_id FROM jobs WHERE key=?',key(100,'issue_comments'))).one().lease_id,'replacement');
 }finally{offset=0;Date.now=clock;gate.resolve();await drainRepairs(f.state);await f.close();}
});

test('a committed member can run its new generation while siblings remain active, without old cleanup clearing its token',async()=>{
 const f=await fixture(),archiveGate=deferred(),newGate=deferred(),newReady=deferred();try{
  await seed(f,9,'issue_comments');const base=f.env.ARCHIVE,put=base.put.bind(base);
  base.put=async(name,...args)=>{if(name.includes(encodeURIComponent(key(108,'issue_comments'))))await archiveGate.promise;return put(name,...args);};
  const tx=f.store.transaction.bind(f.store);let changed=false;
  f.store.transaction=async(apply,purpose)=>{const result=await tx(apply,purpose);
   if(purpose==='publication'&&!changed){changed=true;await f.store.exec('UPDATE jobs SET dirty=1,generation=generation+1,due=? WHERE key=?',Date.now(),key(100,'issue_comments'));}
   return result;
  };
  let calls=0;f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async(_url,init)=>{
   const jobs=JSON.parse(init.body).jobs;if(++calls===2){assert.equal(jobs.length,1);assert.equal(jobs[0].key,key(100,'issue_comments'));newReady.resolve();await newGate.promise;}
   return Response.json({results:jobs.map(()=>({api:'graphql',data:[]}))});
  }})};
  await f.state.runAlarm();await bounded(newReady.promise,'a settled new generation waited for its siblings');
  const c=f.state.repairCoordinator,original=[...c.groups.values()].find(g=>g.jobs.length===9),replacement=c.owned.get(key(100,'issue_comments')).lease_id;
  archiveGate.resolve();await bounded(original.task,'the original group did not finish');
  assert.equal(c.owned.get(key(100,'issue_comments')).lease_id,replacement);
  assert.equal((await f.store.exec('SELECT lease_id FROM jobs WHERE key=?',key(100,'issue_comments'))).one().lease_id,replacement);
  newGate.resolve();await drainRepairs(f.state);assert.equal(calls,2);
  assert.equal((await f.store.exec('SELECT dirty FROM jobs WHERE key=?',key(100,'issue_comments'))).one().dirty,0);
 }finally{archiveGate.resolve();newGate.resolve();await drainRepairs(f.state);await f.close();}
});

test('unknown publication outcome does not label a committed dirty generation as a deferred attempt',async()=>{
 const f=await fixture();try{
  await seed(f,9,'issue_comments');const tx=f.store.transaction.bind(f.store);let changed=false,calls=0;
  f.store.transaction=async(apply,purpose)=>{const result=await tx(apply,purpose);
   if(purpose==='publication'&&!changed){changed=true;await f.store.exec('UPDATE jobs SET dirty=1,generation=generation+1,due=? WHERE key=?',Date.now(),key(100,'issue_comments'));throw Error('unknown commit');}
   return result;
  };
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async(_url,init)=>{
   calls++;const jobs=JSON.parse(init.body).jobs;return Response.json({results:jobs.map(()=>({api:'graphql',data:[]}))});
  }})};
  await f.state.runAlarm();await drainRepairs(f.state);assert.equal(calls,2,'the committed new generation remains eligible but the failed tail does not refetch');
  const fresh=(await f.store.exec('SELECT * FROM jobs WHERE key=?',key(100,'issue_comments'))).one();assert.equal(fresh.dirty,0);assert.equal(fresh.attempt,0);
  const tail=(await f.store.exec('SELECT * FROM jobs WHERE key=?',key(108,'issue_comments'))).one();assert.equal(tail.attempt,0);assert.equal(tail.lease_id,null);
 }finally{await drainRepairs(f.state);await f.close();}
});

test('authoritative global capacity ends futile claim sweeps without a recurring deadline poll',async()=>{
 const f=await fixture();try{
  await seed(f,40);let calls=0;
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>{calls++;return Response.json({deferred:true,category:'capacity',retry_at:Date.now()+250,scope:{kind:'installation'}});}})};
  await f.state.runAlarm();await drainRepairs(f.state);const initial=calls;assert.ok(initial>=1&&initial<=4);
  await new Promise(resolve=>setTimeout(resolve,350));await f.state.repairCoordinator.wake();await flush();
  assert.equal(calls,initial,'an elapsed capacity deadline alone does not start a fresh full-group sweep');
  await f.state.runAlarm();await drainRepairs(f.state);assert.ok(calls>initial&&calls<=initial+4,'ordinary automatic turns still recover');
  assert.equal((await f.store.exec('SELECT SUM(attempt) n FROM jobs')).one().n,0);
 }finally{await drainRepairs(f.state);await f.close();}
});

test('a missing staged page drains parallel archive bodies without resetting its owner',async()=>{
 const f=await fixture(),gate=deferred(),ready=deferred();let aborts=0;try{
  await seed(f,1);
  await f.store.exec('UPDATE jobs SET page=3,started=?,start_generation=generation',Date.now()-1000);
  const base=f.env.ARCHIVE,get=base.get.bind(base);
  base.get=async name=>{
   if(name.startsWith('staging/')&&name.endsWith('/1'))return null;
   if(name.startsWith('staging/')&&name.endsWith('/2'))return {text:async()=>{ready.resolve();await gate.promise;return '[]';}};
   return get(name);
  };
  f.state.ctx.abort=()=>{aborts++;};
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>Response.json({api:'core',data:[]})})};
  await f.state.runAlarm();await bounded(ready.promise,'the healthy archive sibling was not reached');
  await flush();assert.equal(aborts,0);assert.equal(f.state.repairCoordinator.groups.size,1);
  gate.resolve();await drainRepairs(f.state);assert.equal(aborts,0);
  const job=(await f.store.exec('SELECT attempt,wait_reason,lease_id FROM jobs WHERE key=?',key(100))).one();
  assert.equal(job.attempt,1);assert.equal(job.wait_reason,'staging_expired');assert.equal(job.lease_id,null);
  assert.equal(await f.state.complete(query(100)),false);
 }finally{gate.resolve();await drainRepairs(f.state);await f.close();}
});

test('local obsolete work retires while an installation-wide restriction blocks upstream selection',async()=>{
 const f=await fixture();try{
  const q=query(100);await seed(f,1);
  await f.state.put('pr','100',{number:100,state:'CLOSED'});
  await f.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)',key(100),'files',JSON.stringify(q.args),1,Date.now(),1,null);
  await f.store.exec('UPDATE jobs SET dirty=0');
  const c=f.state.getRepairCoordinator();
  f.state.quotaRestriction={category:'secondary',retry_at:Date.now()+60000,installation:'200',quantum:c.current,dataProgress:c.current.dataProgress};
  let calls=0;f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>{calls++;throw Error('local retirement attempted upstream');}})};
  await f.state.runAlarm();await drainRepairs(f.state);
  assert.equal(calls,0);assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs')).one().n,0);
  assert.equal(await f.state.complete(q),true,'retirement retains canonical completed coverage');
 }finally{await drainRepairs(f.state);await f.close();}
});

test('PostgreSQL coalesced release deadlines preserve both early and late wakes',{skip:!process.env.TEST_POSTGRES_URL},async()=>{
 const f=await fixture();try{
  const c=f.state.getRepairCoordinator(),wakes=[],start=Date.now();
  c.resumeCapacity=async()=>{wakes.push(Date.now()-start);};
  f.state.scheduleCapacityDeadline(start+150);const first=f.state.capacityWake;let superseded=0;
  const resolve=first.resolve;first.resolve=()=>{superseded++;resolve();};
  f.state.scheduleCapacityDeadline(start+350);
  f.state.scheduleCapacityDeadline(start+70);assert.equal(superseded,1,'superseded lifetime work resolves');
  assert.equal(f.state.capacityWake.at,start+70);assert.equal(f.state.capacityWake.latest,start+350);
  await bounded(new Promise(resolve=>{const check=()=>{if(wakes.length===2)resolve();else setTimeout(check,10);};check();}),'coalescing lost the later existing deadline');
  assert.ok(wakes[0]>=70&&wakes[0]<300);assert.ok(wakes[1]>=350);assert.equal(f.state.capacityWake,undefined);
 }finally{await drainRepairs(f.state);await f.close();}
});

test('PostgreSQL capacity released before retry_at resumes at the exact existing deadline',{skip:!process.env.TEST_POSTGRES_URL},async()=>{
 const f=await fixture();try{
  await seed(f,1);let calls=0,until;const events=[];
  const note=f.state.noteQuotaRestriction;
  f.state.noteQuotaRestriction=async function(result){await note.call(this,result);events.push({event:'note',at:Date.now(),retry:result.retry_at,group:this.repairGroup && {wake:this.repairGroup.wakeAtStart,at:this.repairGroup.capacityRetryAt},wake:this.owner.repairCoordinator.wakeSequence});};
  const settle=f.state.settleRepairJobs;
  f.state.settleRepairJobs=function(jobs,effect){events.push({event:'settle',effect,at:Date.now(),group:this.repairGroup && {wake:this.repairGroup.wakeAtStart,at:this.repairGroup.capacityRetryAt},wake:this.owner.repairCoordinator.wakeSequence});return settle.call(this,jobs,effect);};
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>{
   if(++calls===1){until=Date.now()+250;
    await f.state.fetch(new Request('https://internal/capacity-ready',{method:'POST',body:JSON.stringify({installation:'200'})}));
    return Response.json({deferred:true,category:'capacity',retry_at:until,scope:{kind:'installation'}});
   }
   assert.ok(Date.now()>=until,'notification must not advance a real job deadline');return Response.json({api:'core',data:[]});
  }})};
  const progress=observeProgress(f.state);await f.state.runAlarm();
  try{await bounded(progress.when(seen=>seen.has(key(100))),'an early release lost its only prompt wake',30000);}catch(error){
   const c=f.state.repairCoordinator;
   error.message+=' '+JSON.stringify({calls,events,progress:[...progress.seen],owned:[...c.owned.keys()],groups:c.groups.size,closed:c.unproductiveStop,q:{launches:c.current.launches,progress:c.current.progress,capacityPaths:[...c.current.capacityPaths],paths:[...c.current.paths],failed:[...c.current.failed]},restriction:f.state.quotaRestriction,wake:c.wakeSequence,job:(await f.store.exec('SELECT due,lease_id,wait_reason FROM jobs WHERE key=?',key(100))).one()});throw error;
  }
  await drainRepairs(f.state);assert.equal(calls,2);assert.equal(await f.state.complete(query(100)),true);
 }finally{await drainRepairs(f.state);await f.close();}
});
