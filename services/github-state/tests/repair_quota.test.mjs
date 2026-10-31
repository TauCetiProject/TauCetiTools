import {test} from 'node:test';
import assert from 'node:assert/strict';
import {installationFixture as fixture} from './repair_installation_fixture.mjs';
const flush=()=>new Promise(setImmediate);
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};

async function holdReservations(f,count=4){
 const ready=deferred();globalThis.fetch=async()=>{const gate=deferred();f.gates.push(gate);if(f.gates.length===count)ready.resolve();await gate.promise;
  return Response.json({}, {headers:{'x-ratelimit-resource':'core','x-ratelimit-remaining':'4000','x-ratelimit-limit':'5000','x-ratelimit-reset':String(Math.floor(Date.now()/1000)+3600)}});
 };
 for(let i=0;i<count;i++){const work=f.scheduler.http('/fixture/'+i,'fixture-token','GET',undefined,'fixture','core');work.catch(()=>{});f.requests.push(work);}
 await ready.promise;
}
async function rejectBatch(f,index){
 const repo=f.repos[index];return f.scheduler.fetch(new Request('https://internal/batch',{method:'POST',body:JSON.stringify({repository:repo,jobs:[{key:'issue_comments:{"number":1}',query:{repository:repo.name,operation:'issue_comments',args:{number:1}}}]})}));
}

test('quota scope distinguishes global reservations, pool exhaustion and an expensive query',async()=>{
 const f=fixture(),original=globalThis.fetch;try{
  const now=Date.now();f.db.prepare('INSERT INTO budgets VALUES(?,?,?,?,?)').run('graphql',100,5000,now+3600000,0);
  assert.equal(f.scheduler.deferral('graphql',101).scope.kind,'query');
  f.db.prepare('INSERT INTO reservations VALUES(?,?,?,?)').run('pending','graphql',80,now+60000);
  const costly=f.scheduler.deferral('graphql',50);assert.equal(costly.category,'capacity');assert.deepEqual(costly.scope,{kind:'query',api:'graphql'});
  assert.equal(f.scheduler.deferral('graphql',1),null,'cheaper queries remain usable');assert.equal(f.scheduler.deferral('core',1),null,'another pool remains usable');
  f.db.prepare('UPDATE reservations SET cost=100').run();assert.equal(f.scheduler.deferral('graphql',1).scope.kind,'pool');
  f.db.prepare('DELETE FROM reservations').run();f.db.prepare("UPDATE budgets SET remaining=5000 WHERE resource='graphql'").run();await holdReservations(f);
  const response=await rejectBatch(f,0),result=await response.json();assert.equal(result.category,'capacity');assert.deepEqual(result.scope,{kind:'installation'});
  assert.ok(result.retry_at>=Date.now()&&result.retry_at<=Date.now()+250);
 }finally{await f.close();globalThis.fetch=original;}
});

test('one released reservation wakes one registered repository, retaining peers in registration order',async()=>{
 const f=fixture(),original=globalThis.fetch;try{
  await holdReservations(f);
  for(const index of [0,0,1,2])assert.equal((await(await rejectBatch(f,index)).json()).category,'capacity');
  assert.equal(f.scheduler.capacityWaiters.size,3);
  f.gates[0].resolve();await f.requests[0];await flush();assert.deepEqual(f.received,['100']);assert.equal(f.scheduler.capacityWaiters.size,2);
  f.gates[1].resolve();await f.requests[1];await flush();assert.deepEqual(f.received,['100','101']);
  f.gates[2].resolve();await f.requests[2];await flush();assert.deepEqual(f.received,['100','101','102']);
  assert.equal(f.scheduler.capacityWaiters.size,0);assert.equal(f.scheduler.capacityNotifying.size,0);
 }finally{await f.close();globalThis.fetch=original;}
});

test('a reservation released between rejection and waiter registration does not lose the prompt wake',async()=>{
 const f=fixture(),original=globalThis.fetch;try{
  await holdReservations(f);const http=f.scheduler.http.bind(f.scheduler);let raced=false;
  f.scheduler.http=async(...args)=>{try{return await http(...args);}catch(error){
   if(error.result?.scope?.kind==='installation'&&!raced){raced=true;f.gates[0].resolve();await f.requests[0];}
   throw error;
  }};
  const result=await(await rejectBatch(f,0)).json();assert.equal(result.category,'capacity');await flush();assert.equal(raced,true);
  assert.deepEqual(f.received,['100']);
  const available=await(await f.scheduler.fetch(new Request('https://internal/availability',{method:'POST'}))).json();assert.equal(available.available,true);
  assert.equal(f.db.prepare('SELECT COUNT(*) n FROM reservations').get().n,3,'an advisory check does not reserve capacity');
 }finally{await f.close();globalThis.fetch=original;}
});

test('failed notifications and unknown optional configuration never replace the upstream result',async()=>{
 const f=fixture(),original=globalThis.fetch;try{
  await holdReservations(f);await rejectBatch(f,0);f.notify=async()=>{throw Error('simulated wake transport');};
  f.gates[0].resolve();const result=await f.requests[0];await flush();assert.equal(result.status,200);
  assert.equal(f.scheduler.capacityNotifying.size,0);
  f.env.ALLOWED_REPOSITORIES='{invalid optional recipient configuration';
  f.scheduler.registerCapacityWaiter(f.repos[1],{category:'capacity',scope:{kind:'installation'}});
  f.gates[1].resolve();assert.equal((await f.requests[1]).status,200);
 }finally{await f.close();globalThis.fetch=original;}
});

test('notification coalescing bounds outstanding calls while a recipient is slow',async()=>{
 const f=fixture(),original=globalThis.fetch,notification=deferred();try{
  await holdReservations(f);f.notify=async()=>notification.promise;
  await rejectBatch(f,0);f.gates[0].resolve();await f.requests[0];await flush();
  f.scheduler.registerCapacityWaiter(f.repos[0],{category:'capacity',scope:{kind:'installation'}});
  f.gates[1].resolve();await f.requests[1];await flush();assert.deepEqual(f.received,['100']);
  notification.resolve();await flush();assert.ok(f.received.length<=2);assert.ok(f.scheduler.capacityNotifying.size<=1);
 }finally{notification.resolve();await f.close();globalThis.fetch=original;}
});
