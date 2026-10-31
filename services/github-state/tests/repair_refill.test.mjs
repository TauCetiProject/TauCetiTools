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

test('durably backed-off malformed members do not strand a healthy item behind the dispatch cap',async()=>{
 const f=await fixture();try{
  await seed(f,18);const healthy=117;let calls=0;
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async(_url,init)=>{
   calls++;const input=JSON.parse(init.body);
   return Response.json(input.query.args.number===healthy?{api:'core',data:[]}:{unavailable:true,reason:'malformed_upstream_response',failure_kind:'member'});
  }})};
  await f.state.runAlarm();await drainRepairs(f.state);
  assert.equal(await f.state.complete(query(healthy)),true,'ordinary completion-driven refill reaches independent valid data');
  const rows=(await f.store.exec('SELECT key,attempt,due,lease_id FROM jobs')).toArray();
  assert.equal(calls,18);assert.equal(rows.filter(j=>j.attempt===1&&j.due>Date.now()).length,17);
  assert.ok(rows.every(j=>j.lease_id===null));
  assert.equal((await f.store.exec("SELECT COUNT(*) n FROM collections WHERE operation='files' AND complete=1")).one().n,1,'retry settlement is not completed coverage');
 }finally{await drainRepairs(f.state);await f.close();}
});

test('a generic apply TypeError backs off without turning a broken backlog into productive quanta',async()=>{
 const f=await fixture();try{
  await seed(f,40);let calls=0;const transaction=f.store.transaction.bind(f.store);
  f.store.transaction=(apply,purpose)=>transaction(tx=>apply(new Proxy(tx,{get(target,name){
   if(name==='exec')return async(sql,...args)=>{if(sql.includes('INTO collections'))throw new TypeError('fixture normalizer defect');return target.exec(sql,...args);};
   if(name==='batch')return async statements=>{if(statements.some(s=>s.sql.includes('INTO collections')))throw new TypeError('fixture normalizer defect');return target.batch(statements);};
   const value=Reflect.get(target,name,target);return typeof value==='function'?value.bind(target):value;
  }})),purpose);
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>{calls++;return Response.json({api:'core',data:[]});}})};
  await f.state.runAlarm();await drainRepairs(f.state);
  assert.equal(calls,16);assert.equal(f.state.repairCoordinator.current.memberRetries,0);assert.equal(f.state.repairCoordinator.current.dataProgress,0);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs WHERE attempt=1 AND due>?',Date.now())).one().n,16);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM collections WHERE complete=1')).one().n,0);
 }finally{await drainRepairs(f.state);await f.close();}
});

test('a truncated-body response receives no member-data continuation credit',async()=>{
 const f=await fixture();try{
  await seed(f,40);let calls=0;
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>{calls++;return Response.json({unavailable:true,reason:'malformed_upstream_response',failure_kind:'transport'});}})};
  await f.state.runAlarm();await drainRepairs(f.state);assert.equal(calls,16);assert.equal(f.state.repairCoordinator.current.memberRetries,0);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs WHERE attempt=1')).one().n,16);
 }finally{await drainRepairs(f.state);await f.close();}
});

test('a malformed internal service envelope backs off without member-data continuation credit',async()=>{
 const f=await fixture();try{
  await seed(f,40);let calls=0;
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>{calls++;return Response.json({});}})};
  await f.state.runAlarm();await drainRepairs(f.state);assert.equal(calls,16);assert.equal(f.state.repairCoordinator.current.memberRetries,0);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs WHERE attempt=1')).one().n,16);
 }finally{await drainRepairs(f.state);await f.close();}
});

test('a failed publication tail is retained across member-retry-only internal chains',async()=>{
 const f=await fixture();try{
  await seed(f,24,'issue_comments');await f.store.exec('UPDATE jobs SET due=?',Date.now()-2000);await seed(f,40);
  const publish=f.state.publishPrepared;let batches=0,files=0;
  f.state.publishPrepared=async function(input,...args){
   if(input.some(p=>p.job.key===key(108,'issue_comments')))throw Error('unclassified publication interruption');
   return publish.call(this,input,...args);
  };
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async(url,init)=>{
   const input=JSON.parse(init.body);
   if(url.endsWith('/batch')){batches++;return Response.json({results:input.jobs.map(()=>({api:'graphql',data:[]}))});}
   files++;return Response.json({unavailable:true,reason:'malformed_upstream_response',failure_kind:'member'});
  }})};
  await f.state.runAlarm();await drainRepairs(f.state);assert.equal(files,40);assert.equal(batches,1,'failure-heavy independent work must not refetch an unchanged tail every16launches');
  assert.ok(f.state.repairCoordinator.current.failed.has(key(123,'issue_comments')));
  await f.state.runAlarm();await drainRepairs(f.state);assert.equal(batches,2,'a genuine external wake retains ordinary automatic recovery');
 }finally{await drainRepairs(f.state);await f.close();}
});

test('a late member retry cannot clear an installation restriction by creating a fresh quantum',async()=>{
 const f=await fixture();try{
  const c=f.state.getRepairCoordinator();await c.wake(true);const q=c.current;
  await seed(f,1);f.state.quotaRestriction={category:'secondary',retry_at:Date.now()-1,installation:'200',quantum:q,dataProgress:q.dataProgress};
  c.settled([{key:'previous-member',lease_id:'previous-token',lease_until:0}],'member-retry');
  await c.flight;await flush();assert.equal(c.current,q);assert.equal(c.current.launches,0);
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>Response.json({api:'core',data:[]})})};
  await f.state.runAlarm();await drainRepairs(f.state);assert.equal(await f.state.complete(query(100)),true);
 }finally{await drainRepairs(f.state);await f.close();}
});

test('an uncertain SQLite alarm write loses its hint and retains ordinary durable recovery',{skip:Boolean(process.env.TEST_POSTGRES_URL)},async()=>{
 const f=await fixture();try{
  const c=f.state.getRepairCoordinator();c.current.failed.add('failed-tail');
  const until=Date.now()+10000;await f.state.setAlarm(until,false,true);
  const set=f.ctx.storage.setAlarm;f.ctx.storage.setAlarm=async()=>{throw Error('fixture alarm storage failure');};
  await assert.rejects(f.state.setAlarm(until+10000,false,true));assert.equal(f.state.repairAlarm,undefined);
  assert.equal(await f.ctx.storage.getAlarm(),until,'the previously persisted alarm is retained');
  f.ctx.storage.setAlarm=set;await f.state.alarm();assert.equal(c.current.failed.size,0,'missing optional metadata makes the durable wake external');
 }finally{await drainRepairs(f.state);await f.close();}
});

test('an ordinary demand wake racing a stale continuation alarm read retains external recovery',{skip:Boolean(process.env.TEST_POSTGRES_URL)},async()=>{
 const f=await fixture(),reading=deferred(),release=deferred(),clock=Date.now;let offset=0;try{
  await seed(f,2);const c=f.state.getRepairCoordinator();c.current.failed.add(key(100));
  f.state.ordinaryAlarmAt=undefined;
  const future=Date.now()+60000;await f.state.setAlarm(future,false,false,true);
  const get=f.ctx.storage.getAlarm;let first=true;
  f.ctx.storage.getAlarm=async()=>{if(first){first=false;reading.resolve();await release.promise;return future;}return get();};
  const pending=f.state.armRepairAlarm();await reading.promise;
  await f.state.setAlarm(Date.now()+100);release.resolve();await pending;
  assert.equal(f.state.repairAlarm.continuation,false,'an earlier self-wake cannot downgrade a newly accepted ordinary wake');
  Date.now=()=>clock()+offset;offset=1000;
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>Response.json({api:'core',data:[]})})};
  await f.state.alarm();await drainRepairs(f.state);
  assert.equal(await f.state.complete(query(100)),true,'fresh demand is not stranded behind the old failed-path record');
  assert.equal(await f.state.complete(query(101)),true);
 }finally{release.resolve();offset=0;Date.now=clock;await drainRepairs(f.state);await f.close();}
});

test('a capacity release after closure does not refetch an unclassified publication tail',async()=>{
 const f=await fixture();try{
  await seed(f,24,'issue_comments');
  await f.store.exec('UPDATE jobs SET due=?',Date.now()-2000);
  const good=query(500);await f.state.enqueue(good,5,Date.now()-1000);
  const publish=f.state.publishPrepared;let batches=0,files=0;
  f.state.publishPrepared=async function(input,...args){
   // Target the second history chunk, independently of where the other
   // group's deferral bookkeeping enters the writer.
   if(input.some(p=>p.job.key===key(108,'issue_comments')))throw Error('unclassified publication interruption');
   return publish.call(this,input,...args);
  };
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async(url,init)=>{
   const input=JSON.parse(init.body);
   if(url.endsWith('/batch')){batches++;return Response.json({results:input.jobs.map(()=>({api:'graphql',data:[]}))});}
   if(++files===1)return Response.json({deferred:true,category:'capacity',retry_at:Date.now()+250,scope:{kind:'installation'}});
   return Response.json({api:'core',data:[]});
  }})};
  await f.state.runAlarm();await drainRepairs(f.state);assert.equal(batches,1);assert.equal(files,1);
  const c=f.state.repairCoordinator,tail=key(123,'issue_comments');assert.ok(c.current.failed.has(tail));
  assert.equal((await f.store.exec('SELECT attempt FROM jobs WHERE key=?',tail)).one().attempt,0);
  const due=(await f.store.exec('SELECT due FROM jobs WHERE key=?',key(500))).one().due;
  await new Promise(resolve=>setTimeout(resolve,Math.max(0,due-Date.now()+5)));
  await f.state.fetch(new Request('https://internal/capacity-ready',{method:'POST',body:JSON.stringify({installation:'200'})}));
  await drainRepairs(f.state);
  assert.equal(await f.state.complete(good),true);assert.equal(files,2);assert.equal(batches,1,'a capacity release does not authorize refetch of an unrelated failed tail');
  assert.equal((await f.store.exec('SELECT attempt FROM jobs WHERE key=?',tail)).one().attempt,0);
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
