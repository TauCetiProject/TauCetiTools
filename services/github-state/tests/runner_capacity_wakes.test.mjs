import {test} from 'node:test';
import assert from 'node:assert/strict';
import {installationFixture} from './repair_installation_fixture.mjs';
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
const response=body=>Response.json(body,{headers:{'x-ratelimit-resource':'core','x-ratelimit-remaining':'4999','x-ratelimit-limit':'5000','x-ratelimit-reset':String(Math.floor(Date.now()/1000)+3600)}});
async function hold(f){
 const ready=deferred();globalThis.fetch=async()=>{const gate=deferred();f.gates.push(gate);if(f.gates.length===4)ready.resolve();await gate.promise;return response({});};
 for(let i=0;i<4;i++)f.requests.push(f.scheduler.http('/held/'+i,'fixture-token','GET',undefined,'fixture','core'));
 await ready.promise;globalThis.fetch=async()=>response([]);
}

test('an early release never advances the runner request past its existing retry deadline',async()=>{
 const f=installationFixture(),original=globalThis.fetch;try{
  await hold(f);const readyAt=Date.now()+100;let resolved=false;
  const wait=f.scheduler.waitRunnerCapacity(readyAt,Date.now()+1000).then(value=>{resolved=true;return value;});
  f.gates[0].resolve();await f.requests[0];await pause(40);assert.equal(resolved,false);
  assert.equal(await wait,true);assert.ok(Date.now()>=readyAt);
  assert.equal(f.db.prepare('SELECT COUNT(*) n FROM reservations').get().n,3,'a wake hint does not reserve a slot');
  assert.equal(f.scheduler.capacityWaiters.size,0);
 }finally{await f.close();globalThis.fetch=original;}
});

test('a ready runner receives a release while an optional repository notification is slow',async()=>{
 const f=installationFixture(),original=globalThis.fetch,notification=deferred();try{
  await hold(f);f.notify=()=>notification.promise;
  f.scheduler.registerCapacityWaiter(f.repos[0],{category:'capacity',scope:{kind:'installation'}});
  const runner=f.scheduler.runnerCapacity(f.repos[0]);
  await pause(300);f.gates[0].resolve();await f.requests[0];await pause(10);
  assert.equal(f.scheduler.capacityNotifying.size,1);assert.deepEqual(f.received,['100']);
  f.gates[1].resolve();await f.requests[1];
  const result=await(await runner).json();assert.equal(result.complete,true);assert.equal(result.requests,1);
  assert.ok(f.scheduler.capacityWakeStats.runner>=1);assert.equal(f.scheduler.capacityNotifying.size,1,'runner progress does not await the repository notification');
  assert.equal(f.scheduler.capacityWaiters.size,0);
 }finally{notification.resolve();await f.close();globalThis.fetch=original;}
});

test('lost local wake notifications retain the existing timed retry fallback',async()=>{
 const f=installationFixture(),original=globalThis.fetch;try{
  await hold(f);f.scheduler.releaseCapacityHints=()=>{};
  const runner=f.scheduler.runnerCapacity(f.repos[0]),start=Date.now();
  await pause(300);f.gates[0].resolve();await f.requests[0];
  const result=await(await runner).json();assert.equal(result.complete,true);assert.equal(result.requests,1);
  assert.ok(Date.now()-start>=400&&Date.now()-start<2000);assert.equal(f.scheduler.capacityWaiters.size,0);
 }finally{await f.close();globalThis.fetch=original;}
});
