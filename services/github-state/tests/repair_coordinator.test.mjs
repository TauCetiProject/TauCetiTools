import {test} from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
const output=await build({entryPoints:['src/repair_coordinator.ts'],bundle:true,platform:'node',format:'esm',write:false});
const {RepairCoordinator}=await import('data:text/javascript;base64,'+Buffer.from(output.outputFiles[0].text).toString('base64'));
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const flush=()=>new Promise(setImmediate);
const job=(n,api=null)=>({key:'job'+n,lease_id:'token'+n,lease_until:Date.now()+60000,api});
function harness(count=1,options={}){
 const queue=Array.from({length:count},(_,i)=>job(i)),background=[],failures=[],maintenance=[];
 let selections=0,operations=0,peak=0,arms=0,held=false;
 const eligible=(q,owned)=>queue.find(j=>!owned.has(j.key)&&!q.failed.has(j.key)&&!q.paths.get(j.key)?.has(j.api||'auto')&&!q.capacityPaths.get(j.key)?.has(j.api||'auto'));
 const c=new RepairCoordinator({concurrency:4,dispatches:16,...options,
  held:()=>held,operations:n=>{operations+=n;},lifetime:p=>{background.push(p);p.catch(()=>{});},failure:e=>failures.push(e),
  select:async(q,owned)=>{selections++;const selected=eligible(q,owned);if(!selected){await h.emptySelection?.();return;}queue.splice(queue.indexOf(selected),1);return {jobs:[selected]};},
  eligible:async(q,owned)=>Boolean(eligible(q,owned)),
  maintenance:async(external,phase)=>maintenance.push({external,phase}),arm:async()=>{arms++;await h.arm?.();},
  dispatch:async group=>{peak=Math.max(peak,c.groups.size);await h.dispatch(group);},stalled:group=>h.stalled(group),
 });
 const h={c,queue,background,failures,maintenance,dispatch:async group=>c.settled(group.jobs,'progress'),stalled:()=>{},
  stats:()=>({selections,operations,peak,arms}),hold:()=>{held=true;}};
 return h;
}

test('short groups refill beside a held group across the sixteen-launch boundary',async()=>{
 const h=harness(49),gate=deferred();let completed=0;
 h.dispatch=async group=>{if(group.jobs[0].key==='job0')await gate.promise;h.c.settled(group.jobs,'progress');completed++;};
 await h.c.wake(true);await flush();
 assert.equal(completed,48);assert.equal(h.c.groups.size,1);assert.ok(h.stats().peak<=4);
 assert.ok(h.maintenance.filter(x=>!x.external).length>=2,'productive quantum boundaries advance maintenance');
 assert.equal(h.c.owned.size,1);gate.resolve();await flush();
 assert.equal(completed,49);assert.equal(h.c.groups.size,0);assert.equal(h.stats().operations,0);assert.deepEqual(h.failures,[]);
});

test('four full slots plus due backlog do not select or self-arm repeatedly',async()=>{
 const h=harness(8),gate=deferred();h.dispatch=async group=>{await gate.promise;h.c.settled(group.jobs,'progress');};
 await h.c.wake(true);await flush();assert.equal(h.stats().selections,4);assert.equal(h.c.groups.size,4);
 const before=h.stats();for(let i=0;i<20;i++)await h.c.wake();
 assert.deepEqual(h.stats(),before,'full capacity relies on completion instead of empty internal turns');
 await h.c.wake(true);assert.equal(h.stats().selections,4,'a genuine external wake does maintenance but no claim');
 assert.equal(h.maintenance.filter(x=>x.external&&x.phase==='after').length,2);
 gate.resolve();await flush();assert.equal(h.c.groups.size,0);assert.equal(h.stats().operations,0);
});

test('an elapsed deferral is tried once per path and ordinary external wakes clear the record',async()=>{
 const h=harness(),original=h.queue[0];let attempts=0;
 h.dispatch=async group=>{attempts++;h.c.settled(group.jobs,'defer');h.queue.push(original);};
 await h.c.wake(true);await flush();assert.equal(attempts,1);
 for(let i=0;i<5;i++)await h.c.wake();await flush();assert.equal(attempts,1,'no automatic no-progress sweep');
 await h.c.wake(true);await flush();assert.equal(attempts,2,'the next ordinary wake retries without an operator');
});

test('API restart ping-pong does not count as productive continuation',async()=>{
 const h=harness();let attempts=0;
 h.dispatch=async group=>{attempts++;h.c.settled(group.jobs,'restart');h.queue.push({...group.jobs[0],api:group.jobs[0].api==='core'?'graphql':'core'});};
 await h.c.wake(true);await flush();assert.equal(attempts,3);assert.equal(h.c.current.progress,0);
});

test('one-way batch routing at the launch budget continues immediately in its usable alternative',async()=>{
 const h=harness(1,{dispatches:1}),paths=[];
 h.dispatch=async group=>{const j=group.jobs[0];paths.push(j.api||'auto');
  if(!j.api){h.c.settled(group.jobs,'route');h.queue.push({...j,api:'core'});}else h.c.settled(group.jobs,'progress');
 };
 await h.c.wake(true);await flush();assert.deepEqual(paths,['auto','core']);assert.equal(h.stats().operations,0);
});

test('completion racing flight finalization leaves a refill wake',async()=>{
 const h=harness(9),gate=deferred(),armed=deferred(),releaseArm=deferred();let first=true,completed=0;
 h.dispatch=async group=>{await gate.promise;h.c.settled(group.jobs,'progress');completed++;};
 h.arm=async()=>{if(first){first=false;armed.resolve();await releaseArm.promise;}};
 const wake=h.c.wake(true);await armed.promise;gate.resolve();await flush();
 assert.equal(completed,4);releaseArm.resolve();await wake;await flush();
 assert.equal(completed,9);assert.equal(h.queue.length,0);assert.equal(h.stats().operations,0);
});

test('external wakes clear paused path exclusions while retaining active ownership',async()=>{
 const h=harness(5),gates=new Map();h.dispatch=async group=>{const gate=deferred();gates.set(group.jobs[0].key,gate);await gate.promise;h.c.settled(group.jobs,'progress');};
 await h.c.wake(true);await flush();h.c.current.paths.set('job4',new Set(['auto']));
 const owned=[...h.c.owned].map(([key,j])=>[key,j.lease_id]);await h.c.wake(true);
 assert.deepEqual([...h.c.owned].map(([key,j])=>[key,j.lease_id]),owned);assert.equal(h.c.current.paths.size,0);
 gates.get('job1').resolve();await flush();assert.ok(gates.has('job4'),'due real demand is considered on the first free slot');
 for(const gate of gates.values())gate.resolve();await flush();assert.equal(h.stats().operations,0);
});

test('migration holds new launches while already owned groups settle and drain accounting',async()=>{
 const h=harness(8),gate=deferred();h.dispatch=async group=>{await gate.promise;h.c.settled(group.jobs,'progress');};
 await h.c.wake(true);h.hold();gate.resolve();await flush();
 assert.equal(h.queue.length,4);assert.equal(h.c.groups.size,0);assert.equal(h.c.owned.size,0);assert.equal(h.stats().operations,0);
});

test('an unconfirmed canceled hop retains its slot until owner recovery',async()=>{
 const h=harness(4,{concurrency:1});let recovered=0;
 h.dispatch=async group=>{group.pendingIO.set(new Promise(()=>{}),{wait:Promise.resolve(),expired:true});};h.stalled=()=>{recovered++;};
 await h.c.wake(true);await flush();assert.equal(recovered,1);assert.equal(h.c.groups.size,1);
 await h.c.wake(true);await flush();assert.equal(h.stats().selections,1,'a timed-out outgoing operation is not silently abandoned');
});

test('a capacity release clears only installation-capacity paths in a paused quantum',async()=>{
 const h=harness(5),gate=deferred();let attempts=0;
 h.dispatch=async group=>{attempts++;await gate.promise;h.c.settled(group.jobs,'defer');};
 await h.c.wake(true);await flush();const q=h.c.current;
 q.failed.add('failed-tail');q.paths.set('primary',new Set(['core']));q.paths.set('restart',new Set(['graphql']));q.routed.add('routed');
 q.capacityPaths.set('job4',new Map([['auto',0]]));await h.c.capacityWake();
 assert.equal(h.c.current,q,'full slots retain the paused quantum');
 gate.resolve();await flush();
 assert.equal(h.c.current,q);assert.equal(q.capacityPaths.size,0);assert.ok(q.failed.has('failed-tail'));
 assert.deepEqual([...q.paths.get('primary')],['core']);assert.deepEqual([...q.paths.get('restart')],['graphql']);assert.ok(q.routed.has('routed'));
 assert.equal(attempts,5);assert.equal(h.stats().operations,0);
});

test('a capacity wake while full cannot later authorize an unproductive budget sweep',async()=>{
 const h=harness(12,{dispatches:4}),gate=deferred();let attempts=0;
 h.dispatch=async group=>{attempts++;await gate.promise;h.c.settled(group.jobs,'defer');};
 await h.c.wake(true);await h.c.capacityWake();gate.resolve();await flush();
 assert.equal(attempts,4);assert.equal(h.c.unproductiveStop,true);
 await h.c.wake();await flush();assert.equal(attempts,4,'the stale availability signal does not grant another quantum');
 await h.c.wake(true);await flush();assert.equal(attempts,8,'ordinary automatic scheduling remains live');
});

test('a capacity release during a stale empty selection resumes without losing its wake or resetting the quantum',async()=>{
 const h=harness(),selected=deferred(),selection=deferred(),dispatch=deferred();let attempts=0,paused=false;
 h.emptySelection=async()=>{if(!paused){paused=true;selected.resolve();await selection.promise;}};
 h.dispatch=async group=>{
  if(++attempts===1){await dispatch.promise;h.c.settled(group.jobs,'capacity');h.queue.push(group.jobs[0]);void h.c.resumeCapacity();}
  else h.c.settled(group.jobs,'progress');
 };
 const wake=h.c.wake(true);await selected.promise;const q=h.c.current;q.failed.add('unrelated-backend-tail');
 dispatch.resolve();await flush();assert.equal(h.c.groups.size,0);selection.resolve();await wake;await flush();
 assert.equal(attempts,2);assert.equal(h.c.current,q);assert.ok(q.failed.has('unrelated-backend-tail'));assert.equal(h.stats().operations,0);
});

for(const dispatches of [1,16])test('a closed capacity continuation retains failed tails and unrelated paths with budget '+dispatches,async()=>{
 const h=harness(1,{dispatches});let attempts=0;
 h.dispatch=async group=>{
  if(++attempts===1){h.c.settled(group.jobs,'capacity');h.queue.push(group.jobs[0]);}
  else h.c.settled(group.jobs,'progress');
 };
 await h.c.wake(true);await flush();const q=h.c.current;
 q.failed.add('failed-tail');q.paths.set('primary',new Set(['graphql']));q.routed.add('routed');
 await h.c.capacityWake();await flush();
 assert.equal(attempts,2);assert.ok(h.c.current.failed.has('failed-tail'));
 assert.deepEqual([...h.c.current.paths.get('primary')],['graphql']);assert.ok(h.c.current.routed.has('routed'));
 assert.equal(h.stats().operations,0);
});

test('a pending release cannot clear a rejection settled after that wake',async()=>{
 const h=harness(5),gate=deferred();
 h.dispatch=async group=>{await gate.promise;h.c.settled(group.jobs,'capacity');};
 await h.c.wake(true);await h.c.capacityWake();
 gate.resolve();await flush();assert.equal(h.c.current.capacityPaths.size,5,'new rejection epochs survive the older pending wake');
 assert.equal(h.stats().operations,0);
});

test('an ordinary error drains a nonexpired neighboring hop without resetting the owner',async()=>{
 const h=harness(2,{concurrency:1}),gate=deferred(),wait=deferred();let resets=0,attempts=0;
 h.stalled=()=>resets++;
 h.dispatch=async group=>{
  if(++attempts===1){
   const actual=gate.promise.finally(()=>{group.pendingIO.delete(actual);wait.resolve();});
   group.pendingIO.set(actual,{wait:wait.promise,expired:false});throw Error('ordinary missing archive page');
  }
  h.c.settled(group.jobs,'progress');
 };
 await h.c.wake(true);await flush();assert.equal(resets,0);assert.equal(attempts,1);assert.equal(h.c.groups.size,1);
 gate.resolve();await flush();assert.equal(resets,0);assert.equal(attempts,2);assert.equal(h.stats().operations,0);
 assert.equal(h.failures.length,1);
});

test('durable member-data retries advance independent ready work without crediting data progress',async()=>{
 const h=harness(21);let attempts=0;const records=[];
 h.dispatch=async group=>{
  attempts++;if(!records.includes(h.c.current))records.push(h.c.current);
  h.c.settled(group.jobs,attempts<=20?'member-retry':'progress');
 };
 await h.c.wake(true);await flush();assert.equal(attempts,21);assert.equal(h.queue.length,0);
 assert.equal(records[0].memberRetries,16);assert.equal(records[0].progress,0);assert.equal(records[0].dataProgress,0);
 assert.equal(records[1].memberRetries,4);assert.equal(records[1].dataProgress,1);assert.equal(h.stats().operations,0);
});

test('delayed cancellation acknowledgement releases its slot without resetting unrelated work',async()=>{
 const h=harness(2,{concurrency:1}),cancel=deferred();let resets=0,attempts=0;
 h.stalled=()=>resets++;
 h.dispatch=async group=>{
  if(++attempts===1){
   const actual=cancel.promise.finally(()=>group.pendingIO.delete(actual));
   group.pendingIO.set(actual,{wait:Promise.resolve(),expired:true,cancellable:true});
  }else h.c.settled(group.jobs,'progress');
 };
 await h.c.wake(true);await flush();assert.equal(attempts,1);assert.equal(resets,0);assert.equal(h.c.groups.size,1);
 cancel.resolve();await flush();assert.equal(attempts,2);assert.equal(resets,0);assert.equal(h.stats().operations,0);
});

test('a canceled transport that never acknowledges still has bounded owner recovery',async()=>{
 const h=harness(1),reset=deferred(),start=Date.now();
 h.stalled=()=>reset.resolve();h.dispatch=async group=>{group.pendingIO.set(new Promise(()=>{}),{wait:Promise.resolve(),expired:true,cancellable:true});};
 await h.c.wake(true);await reset.promise;const elapsed=Date.now()-start;
 assert.ok(elapsed>=1900&&elapsed<10000);assert.equal(h.c.groups.size,1,'an unacknowledged operation is never silently abandoned');
});

test('a real cleared restriction can resume an exhausted closed record even without an old capacity path',async()=>{
 const h=harness(2,{dispatches:1});let attempts=0;
 h.dispatch=async group=>{h.c.settled(group.jobs,++attempts===1?'defer':'progress');};
 await h.c.wake(true);await flush();assert.equal(attempts,1);h.c.current.failed.add('old-failed-tail');
 await h.c.capacityWake(true);await flush();assert.equal(attempts,2);assert.ok(h.c.current.failed.has('old-failed-tail'));
});
