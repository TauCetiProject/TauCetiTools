import {drainRepairs} from './repair_background.mjs';
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {postgresFixture} from './postgres_runtime.mjs';
const enabled=Boolean(process.env.TEST_POSTGRES_URL && process.env.TEST_POSTGRES_MIGRATION_URL);
const pg=(name,fn)=>test(name,{skip:!enabled},fn);
const q=number=>({repository:'org/repo',operation:'review_comments',args:{number}});

pg('missing histories return before writer admission and still hydrate',async()=>{
 const f=await postgresFixture();let release;try {
  let entered;const ready=new Promise(r=>entered=r),gate=new Promise(r=>release=r);
  const holding=f.store.transaction(async()=>{entered();await gate;});await ready;
  let timer;
  try {
   const result=await Promise.race([f.state.query(q(23580)),new Promise((_,reject)=>timer=setTimeout(()=>reject(Error('missing history waited for writer admission')),5000))]);
   assert.equal(result.status,'unavailable');assert.equal(result.complete,false);
   assert.equal((await f.store.exec('SELECT COUNT(*) n FROM demand')).one().n,0);
  }finally{clearTimeout(timer);release();}
  await holding;await f.state.readDemands.flight;
  const job=(await f.store.exec('SELECT * FROM jobs WHERE key=?','review_comments:{"number":23580}')).one();
  assert.equal(job.lane,'requested');assert.ok(job.requested_at);
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>Response.json({api:'core',data:[]})})};
  await f.state.dispatch(await f.state.claim(job));
  const result=await f.state.query(q(23580));assert.equal(result.status,'ok');assert.equal(result.complete,true);assert.deepEqual(result.data,[]);
 }finally{release?.();await f.close();}
});

pg('one demand owner batches distinct misses despite optional-work saturation',async()=>{
 const f=await postgresFixture();let release;try {
  f.state.optionalWork.active=2;
  let entered;const ready=new Promise(r=>entered=r),gate=new Promise(r=>release=r);
  const holding=f.store.transaction(async()=>{entered();await gate;});await ready;
  const atomic=f.state.atomic;let writes=0;
  f.state.atomic=function(apply,purpose){if(purpose==='read_demand')writes++;return atomic.call(this,apply,purpose);};
  for(let repeat=0;repeat<6;repeat++)for(let i=0;i<8;i++)f.state.backgroundDemand(q(100+i),true);
  await Promise.resolve(); // The first group is now active behind the writer.
  for(let repeat=0;repeat<6;repeat++)for(let i=0;i<8;i++)f.state.backgroundDemand(q(100+i),true);
  release();await holding;await f.state.readDemands.flight;
  assert.equal(writes,2,'eight unique keys use two transactions, not one per reader');
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs WHERE lane=?','requested')).one().n,8);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM demand')).one().n,8);
  assert.equal(f.state.readDemands.pending.size,0);assert.equal(f.state.readDemands.active.size,0);
  f.state.optionalWork.active=0;
 }finally{release?.();await f.close();}
});

pg('failed demand keeps every key and resumes without a cooldown',async()=>{
 const f=await postgresFixture();try {
  const atomic=f.state.atomic;let failing=true;
  f.state.atomic=function(apply,purpose){if(purpose==='read_demand' && failing)return Promise.reject(Error('injected unavailable writer'));return atomic.call(this,apply,purpose);};
  for(const number of [11,12,13])f.state.backgroundDemand(q(number),true);
  await f.state.readDemands.flight;
  assert.equal(f.state.readDemands.pending.size,3);assert.equal(f.state.demandMemo.size,0);
  failing=false;f.state.startReadDemands();await f.state.readDemands.flight;
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM demand')).one().n,3);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs')).one().n,3);
  assert.equal(f.state.readDemands.pending.size,0);
 }finally{await f.close();}
});

pg('demand preserves parent retry deadlines and promotes existing dependencies',async()=>{
 const f=await postgresFixture();try {
  const parentQuery=q(7),source={...parentQuery,operation:'review_threads'};
  await f.state.enqueueMany([{q:parentQuery,priority:20},{q:source,priority:20}],true);
  const key='review_comments:{"number":7}',due=Date.now()+3600000;
  await f.store.exec("UPDATE jobs SET due=?,attempt=2,wait_reason='primary:core',generation=9 WHERE key=?",due,key);
  f.state.backgroundDemand(parentQuery,true);await f.state.readDemands.flight;
  const parent=(await f.store.exec('SELECT * FROM jobs WHERE key=?',key)).one(),child=(await f.store.exec('SELECT * FROM jobs WHERE key=?','review_threads:{"number":7}')).one();
  assert.equal(parent.due,due);assert.equal(parent.attempt,2);assert.equal(parent.wait_reason,'primary:core');assert.equal(parent.generation,9);assert.equal(parent.lane,'requested');
  assert.equal(child.lane,'requested');assert.ok(child.requested_at);assert.equal(await f.state.complete(parentQuery),false);
 }finally{await f.close();}
});

pg('a missing collection bypasses a completed-demand memo immediately',async()=>{
 const f=await postgresFixture();try {
  const query={...q(7),operation:'issue_comments'},key='issue_comments:{"number":7}';
  await f.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)',key,query.operation,JSON.stringify(query.args),1,Date.now(),0,null);
  await f.state.noteReadDemand(query);await f.state.readDemands.flight;assert.ok(f.state.demandMemo.has(key));
  await f.store.exec('UPDATE collections SET complete=0 WHERE key=?',key);await f.store.exec('DELETE FROM jobs WHERE key=?',key);
  await f.state.noteReadDemand(query);await f.state.readDemands.flight;
  assert.equal((await f.store.exec('SELECT lane FROM jobs WHERE key=?',key)).one().lane,'requested');
  assert.equal(await f.state.complete(query),false);
 }finally{await f.close();}
});

pg('failed companion preparation retries its leader while unrelated work proceeds',async()=>{
 const f=await postgresFixture();try {
  const bad={...q(31),operation:'issue_comments'},good=q(32);
  await f.state.enqueueMany([bad,good].map(q=>({q,priority:5})),true);
  await f.store.exec("UPDATE jobs SET due=due-10000 WHERE key LIKE 'issue_comments:%'");
  const prepare=f.state.claimCompanions;f.state.claimCompanions=async function(first,...args){if(JSON.parse(first.query).operation==='issue_comments')throw Error('injected companion failure');return prepare.call(this,first,...args);};
  f.state.maintenance=async()=>{};f.state.startStorageMaintenance=()=>{};let reads=0;
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>{reads++;return Response.json({api:'core',data:[]});}})};
  await f.state.runAlarm();await drainRepairs(f.state);assert.equal(reads,1);assert.equal(await f.state.complete(good),true);
  const failed=(await f.store.exec('SELECT * FROM jobs WHERE key=?','issue_comments:{"number":31}')).one();
  assert.equal(failed.attempt,1);assert.ok(failed.due>Date.now());assert.equal(failed.lease_id,null);assert.equal(failed.lease_until,0);
 }finally{await f.close();}
});

pg('repair-wave batching preserves one upstream batch for compatible histories',async()=>{
 const f=await postgresFixture();try {
  const queries=[41,42,43,44].map(number=>({...q(number),operation:'issue_comments'}));
  await f.state.enqueueMany(queries.map(q=>({q,priority:5})),true);
  f.state.maintenance=async()=>{};f.state.startStorageMaintenance=()=>{};let calls=0;
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async(url,init)=>{
   calls++;assert.equal(url,'https://internal/batch');const input=JSON.parse(init.body);
   assert.equal(input.jobs.length,4,'one compatible group must not become four upstream requests');
   return Response.json({results:input.jobs.map(()=>({api:'graphql',data:[]}))});
  }})};
  await f.state.runAlarm();await drainRepairs(f.state);assert.equal(calls,1);
  for(const query of queries)assert.equal(await f.state.complete(query),true);
 }finally{await f.close();}
});

pg('cached progress hits do not add demand transactions',async()=>{
 const f=await postgresFixture();try {
  await f.state.put('pr','7',{number:7,state:'OPEN',headRefOid:'a'.repeat(40)});
  for(const operation of ['pr','issue_comments','review_comments'])await f.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)',operation+':{"number":7}',operation,'{"number":7}',1,Date.now(),0,null);
  const request=()=>f.state.fetch(new Request('https://internal/query',{method:'POST',body:JSON.stringify({...q(7),operation:'progress'})})).then(r=>r.json());
  assert.equal((await request()).status,'ok');await f.state.readDemands.flight;
  let writes=0;f.state.atomic=async()=>{writes++;throw Error('cached progress must not enter the writer');};
  for(let n=0;n<4;n++)assert.equal((await request()).status,'ok');
  await f.state.readDemands.flight;assert.equal(writes,0);assert.equal(f.state.readDemands.pending.size,0);
 }finally{await f.close();}
});

pg('shadow target reads cannot queue demand on their SQLite source owner',async()=>{
 const f=await postgresFixture();try {
  const target=Object.create(f.state);let writes=0;
  target.owner={store:{backend:'sqlite'},startReadDemands(){writes++;}};
  target.backgroundDemand(q(7),true);
  assert.equal(writes,0);assert.equal(f.state.readDemands.pending.size,0);
  f.state.migrationState={phase:'freezing'};f.state.backgroundDemand(q(8),true);
  assert.equal(f.state.readDemands.pending.size,0);f.state.migrationState=null;
 }finally{await f.close();}
});

pg('a miss during active demand is retained after an older completed decision',async()=>{
 const f=await postgresFixture();let release;try {
  const query={...q(7),operation:'issue_comments'},key='issue_comments:{"number":7}';
  await f.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)',key,query.operation,JSON.stringify(query.args),1,Date.now(),0,null);
  const atomic=f.state.atomic;let entered,first=true;
  const ready=new Promise(r=>entered=r),gate=new Promise(r=>release=r);
  f.state.atomic=async function(apply,purpose){
   const result=await atomic.call(this,apply,purpose);
   if(purpose==='read_demand' && first){first=false;entered();await gate;}
   return result;
  };
  f.state.backgroundDemand(query);await ready;
  await f.store.exec('UPDATE collections SET complete=0 WHERE key=?',key);
  await f.state.noteReadDemand(query);assert.equal(f.state.readDemands.pending.has(key),true);
  release();await f.state.readDemands.flight;
  assert.equal((await f.store.exec('SELECT lane FROM jobs WHERE key=?',key)).one().lane,'requested');
  assert.equal(await f.state.complete(query),false);
 }finally{release?.();await f.close();}
});

pg('retiring obsolete jobs yields the writer and continues to useful work',async()=>{
 const f=await postgresFixture();try {
  await f.state.atomic(async state=>{
   for(const number of [1,2,3,4]){
    await state.put('pr',String(number),{number,state:'CLOSED'});
    await state.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)','review_comments:'+JSON.stringify({number}),'review_comments',JSON.stringify({number}),1,Date.now(),0,null);
    await state.enqueue(q(number),20,Date.now()-3600000);
   }
   for(const number of [5,6])await state.enqueue(q(number),20);
   await state.store.exec("UPDATE jobs SET lane='active'");
  });
  const atomic=f.state.atomic,next=f.state.nextJob;
  f.state.atomic=function(apply,purpose){return atomic.call(this,async state=>{
   let calls=0;
   if(purpose==='repair')state.nextJob=async()=>{assert.ok(++calls<=8,'four retirements and four leaders must yield between transactions');return next.call(state);};
   return apply(state);
  },purpose);};
  f.state.maintenance=async()=>{};f.state.startStorageMaintenance=()=>{};
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>Response.json({api:'core',data:[]})})};
  await f.state.runAlarm();await drainRepairs(f.state);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs')).one().n,2);
  for(const number of [5,6])assert.equal(await f.state.complete(q(number)),true);
 }finally{await f.close();}
});

pg('a mixed backlog runs full upstream groups and unrelated work concurrently',async()=>{
 const f=await postgresFixture();let release;try {
  const threads=Array.from({length:25},(_,i)=>({...q(100+i),operation:'review_threads'}));
  await f.state.enqueueMany([...threads,q(999)].map(q=>({q,priority:5})),true);
  await f.store.exec("UPDATE jobs SET due=due-10000 WHERE key LIKE 'review_threads:%'");
  f.state.maintenance=async()=>{};f.state.startStorageMaintenance=()=>{};
  let started;const ready=new Promise(r=>started=r);
  const gate=new Promise(r=>release=r),sizes=[];let calls=0,reads=0;
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async(url,init)=>{
   calls++;if(calls===4){started();release();}await gate;
   const input=JSON.parse(init.body);
   if(url==='https://internal/batch'){
    sizes.push(input.jobs.length);return Response.json({results:input.jobs.map(()=>({api:'graphql',data:[]}))});
   }
   reads++;return Response.json({api:'core',data:[]});
  }})};
  let timer;const alarm=f.state.runAlarm();
  try{await Promise.race([ready,new Promise((_,reject)=>timer=setTimeout(()=>reject(Error('mixed work lost upstream parallelism')),30000))]);await alarm;}
  finally{clearTimeout(timer);release();await alarm;}
  await drainRepairs(f.state);
  assert.equal(calls,4);assert.equal(reads,1);assert.deepEqual(sizes.sort((a,b)=>a-b),[5,10,10]);
  for(const query of threads)assert.equal(await f.state.complete(query),true);
  assert.equal(await f.state.complete(q(999)),true);
 }finally{release?.();await f.close();}
});

pg('large verification groups use bulk dormancy checks and retain hot closed work',async()=>{
 const f=await postgresFixture();try {
  const queries=Array.from({length:100},(_,i)=>({...q(i+1),operation:'issue_comments'})),now=Date.now();
  await f.store.exec('INSERT INTO resources(kind,id,parent,data,updated,observed,revision,tombstone) VALUES '+queries.map(()=>'(?,?,?,?,?,?,?,?)').join(','),...queries.flatMap((q,i)=>['pr',String(i+1),null,JSON.stringify({number:i+1,state:i>=97?'CLOSED':'OPEN'}),null,now,0,0]));
  await f.store.exec('INSERT INTO collections(key,operation,args,complete,verified,revision,etag) VALUES '+queries.map(()=>'(?,?,?,?,?,?,?)').join(','),...queries.flatMap(q=>['issue_comments:'+JSON.stringify(q.args),q.operation,JSON.stringify(q.args),1,now,0,null]));
  await f.state.enqueueMany(queries.map(q=>({q,priority:20})));
  await f.store.exec("UPDATE jobs SET lane='verification'");
  await f.store.exec('INSERT INTO demand(key,at) VALUES(?,?)','issue_comments:{"number":100}',now);
  const first=await f.state.claim(await f.state.nextJob());let calls=0;
  const counted=target=>new Proxy(target,{get(target,key){
   if(key==='transaction')return(apply,purpose)=>target.transaction(tx=>apply(counted(tx)),purpose);
   if(key==='exec' || key==='batch')return(...args)=>{calls++;return target[key](...args);};
   const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;
  }});
  f.state.store=counted(f.store);const companions=await f.state.claimCompanions(first);
  assert.ok(calls<=15,'dormancy must not issue one remote lookup per complete companion');
  assert.equal(companions.length,97);assert.ok([first,...companions].some(j=>j.key==='issue_comments:{"number":100}'));
  for(const number of [98,99])assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs WHERE key=?','issue_comments:'+JSON.stringify({number}))).one().n,0);
 }finally{await f.close();}
});

for(const operation of ['statuses','reactions'])pg('bulk '+operation+' dormancy matches ordinary retirement decisions',async()=>{
 const f=await postgresFixture();try {
  await f.state.atomic(async state=>{
   for(const [id,status] of [[1,'OPEN'],[2,'CLOSED'],[3,'CLOSED'],[4,null]])await state.put('pr',String(id),{number:id,state:status});
   await state.put('issue','4',{number:4,state:'CLOSED'});
   for(const [id,parent,deleted] of [[101,1,false],[102,2,true],[103,3,true],[104,3,false],[105,4,false]])await state.put('review_comments',String(id),{id:String(id)},Date.now(),deleted,String(parent));
  });
  const queries=operation==='statuses'?['a','b','c','d','e'].map(c=>({repository:'org/repo',operation,args:{sha:c.repeat(40)}})):[101,102,103,104,105,106].map(id=>({repository:'org/repo',operation,args:{id:String(id)}}));
  if(operation==='statuses')for(const [head,number] of [['a',1],['b',2],['c',3],['c',99],['d',3]])await f.store.exec('INSERT INTO head_prs VALUES(?,?)',head.repeat(40),String(number));
  for(const query of queries)await f.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)',operation+':'+JSON.stringify(query.args),operation,JSON.stringify(query.args),1,Date.now(),0,null);
  await f.state.enqueueMany(queries.map(q=>({q,priority:20})));await f.store.exec("UPDATE jobs SET lane='verification'");
  const hot=queries[operation==='statuses'?3:2];await f.store.exec('INSERT INTO demand(key,at) VALUES(?,?)',operation+':'+JSON.stringify(hot.args),Date.now());
  const firstQuery=queries[0],first=await f.state.claim((await f.store.exec('SELECT * FROM jobs WHERE key=?',operation+':'+JSON.stringify(firstQuery.args))).one());
  const original=(await f.store.exec('SELECT * FROM jobs WHERE lease_id IS NULL')).toArray(),expected=[];
  for(const job of original)if(job.dirty || !(await f.state.dormant(JSON.parse(job.query))) || !(await f.state.complete(JSON.parse(job.query))))expected.push(job.key);
  const claimed=await f.state.claimCompanions(first);
  assert.deepEqual(claimed.map(j=>j.key).sort(),expected.sort());
  assert.ok(claimed.some(j=>j.key===operation+':'+JSON.stringify(hot.args)),'hot demand must survive even for a closed or tombstoned parent');
 }finally{await f.close();}
});
