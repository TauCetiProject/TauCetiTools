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
