import {test} from 'node:test';
import assert from 'node:assert/strict';
import {bucket} from './r2_fixture.mjs';
import {monitoring,queueMonitoring} from './prevention_runtime.mjs';

test('PostgreSQL capacity separates growable bytes, unknown quotas, forecasts and staleness',()=>{
 const now=Date.now(),sample=(bytes,limit=1000,at=now)=>({database_bytes:bytes,storage_limit_bytes:limit,prepared_at:at});
 assert.equal(monitoring.postgresCapacity(sample(700)).level,'warning');
 assert.equal(monitoring.postgresCapacity(sample(850)).level,'critical');
 const unknown=monitoring.postgresCapacity(sample(999999,null));assert.equal(unknown.capacity_unknown,true);assert.equal(unknown.allowance,null);assert.equal(unknown.growable,true);
 const baseline={at:now-3600000,bytes:100,allowance:1000};let prior;
 for(let i=0;i<3;i++)prior=monitoring.postgresCapacity(sample(200+i),baseline,now,prior);
 assert.equal(prior.forecast_confirmed,true);assert.equal(prior.level,'critical');
 const shrinking=monitoring.postgresCapacity(sample(199),baseline,now,prior);assert.equal(shrinking.forecast_breaches,0);
 assert.equal(monitoring.postgresCapacity(sample(100,1000,now-200000),undefined,now).stale,true);
 assert.equal(prior.usage_scope,'current_database_lower_bound');assert.equal(prior.project_usage_bytes,null);
});

test('scheduled queue metrics retain last good values as stale after failed delivery without consuming messages',async()=>{
 const archive=bucket();let failed=false,calls=0;
 const queue={async metrics(){calls++;if(failed)throw Error('outage');return {backlogCount:7,backlogBytes:900,oldestMessageTimestamp:new Date(1000)};},send(){throw Error('must not send');}};
 const env={ARCHIVE:archive,EVENTS:queue,EVENTS_DLQ:queue};
 await queueMonitoring.prepareQueueMetrics(env);let value=await queueMonitoring.queueMetrics(env);
 assert.equal(calls,2);assert.equal(value.queue.count,7);assert.equal(value.dlq.oldest_received,1000);assert.equal(value.dlq.stale,false);
 failed=true;await queueMonitoring.prepareQueueMetrics(env);value=await queueMonitoring.queueMetrics(env);
 assert.equal(value.queue.count,7);assert.equal(value.queue.stale,true);assert.equal(value.dlq.error,'metrics_unavailable');
 failed=false;await queueMonitoring.prepareQueueMetrics(env);assert.equal((await queueMonitoring.queueMetrics(env)).queue.stale,false);
 delete env.EVENTS_DLQ;await queueMonitoring.prepareQueueMetrics(env);assert.equal((await queueMonitoring.queueMetrics(env)).dlq.error,'binding_unavailable');
});

const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return{promise,resolve,reject};};
const flush=async()=>{for(let n=0;n<8;n++)await Promise.resolve();};

test('timed maintenance retains unfinished writes across callers and proxy instances',async t=>{
 t.mock.timers.enable({apis:['setTimeout']});
 const writes=[],archive={put(){const d=deferred();writes.push(d);return d.promise;}};
 try{
  const first=monitoring.timedBucket(archive);assert.equal(monitoring.timedBucket(archive),first);assert.equal(monitoring.timedBucket(first),first);
  const calls=Array.from({length:16},()=>assert.rejects(first.put('key','body'),/monitoring I\/O timeout/));
  await flush();assert.equal(writes.length,16);t.mock.timers.tick(1500);await Promise.all(calls);
  await assert.rejects(monitoring.timedBucket(archive).put('later','body'),/monitoring I\/O busy/);assert.equal(writes.length,16);
  writes[0].resolve(null);await flush();const next=assert.rejects(first.put('later','body'),/monitoring I\/O timeout/);await flush();assert.equal(writes.length,17);t.mock.timers.tick(1500);await next;
  for(const d of writes)d.resolve(null);await flush();
 }finally{t.mock.timers.reset();}
});

test('late maintenance get cancels its abandoned body before releasing the I/O slot',async t=>{
 t.mock.timers.enable({apis:['setTimeout']});
 const gets=[],cancel=deferred();let cancelled=0,puts=0;
 const archive={get(){const d=deferred();gets.push(d);return d.promise;},put(){puts++;return Promise.resolve(null);}};
 try{
  const timed=monitoring.timedBucket(archive),calls=Array.from({length:16},()=>assert.rejects(timed.get('key'),/timeout/));
  await flush();t.mock.timers.tick(1500);await Promise.all(calls);
  gets[0].resolve({body:{cancel(){cancelled++;return cancel.promise;}}});await flush();assert.equal(cancelled,1);
  await assert.rejects(timed.put('key','body'),/busy/);assert.equal(puts,0);
  cancel.resolve();await flush();await timed.put('key','body');assert.equal(puts,1);
  for(const d of gets)d.resolve(null);await flush();
 }finally{t.mock.timers.reset();}
});

test('timed-out body reads remain bounded and late rejection frees their slots',async t=>{
 t.mock.timers.enable({apis:['setTimeout']});
 const reads=[],archive={get:async()=>({json(){const d=deferred();reads.push(d);return d.promise;}})};
 try{
  const timed=monitoring.timedBucket(archive),objects=await Promise.all(Array.from({length:16},()=>timed.get('key')));
  const calls=objects.map(o=>assert.rejects(o.json(),/timeout/));await flush();t.mock.timers.tick(1500);await Promise.all(calls);
  await assert.rejects(timed.get('other'),/busy/);
  reads[0].reject(Error('late body failure'));await flush();assert.ok(await timed.get('other'));
  for(const d of reads)d.resolve({});await flush();
 }finally{t.mock.timers.reset();}
});

test('maintenance capacity is isolated per bucket and synchronous failures release it',async()=>{
 const archive={put(){throw Error('synchronous failure');}},timed=monitoring.timedBucket(archive);
 for(let n=0;n<32;n++)await assert.rejects(timed.put('key','body'),/synchronous failure/);
 const other=monitoring.timedBucket({get:async()=>null});assert.equal(await other.get('key'),null);
});

test('late get cancellation failure is contained and does not exhaust future maintenance',async t=>{
 t.mock.timers.enable({apis:['setTimeout']});
 const reads=[];let cancelCalls=0;
 const timed=monitoring.timedBucket({get(){const d=deferred();reads.push(d);return d.promise;}});
 try{
  const calls=Array.from({length:16},()=>assert.rejects(timed.get('key'),/timeout/));await flush();t.mock.timers.tick(1500);await Promise.all(calls);
  for(const d of reads)d.resolve({body:{cancel:async()=>{cancelCalls++;throw Error('cancel unavailable');}}});
  await flush();await flush();assert.equal(cancelCalls,16);
  const next=timed.get('next');await flush();assert.equal(reads.length,17);reads.at(-1).resolve(null);assert.equal(await next,null);
 }finally{t.mock.timers.reset();}
});

test('ordinary maintenance reads retain metadata and body results while another bucket is saturated',async t=>{
 t.mock.timers.enable({apis:['setTimeout']});const stalled=[];
 const first=monitoring.timedBucket({put(){const d=deferred();stalled.push(d);return d.promise;}});
 try{
  const calls=Array.from({length:16},()=>assert.rejects(first.put('key','body'),/timeout/));await flush();t.mock.timers.tick(1500);await Promise.all(calls);
  const second=monitoring.timedBucket({get:async()=>({key:'stored',etag:'unchanged',json:async()=>({observed:123,verified:456})})});
  const result=await second.get('key');assert.equal(result.key,'stored');assert.equal(result.etag,'unchanged');assert.deepEqual(await result.json(),{observed:123,verified:456});
  for(const d of stalled)d.resolve(null);await flush();
 }finally{t.mock.timers.reset();}
});
