import {test} from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
const key='__repositoryOutboundFixture';
const bundle=await build({entryPoints:['src/repository_store.ts'],bundle:true,platform:'node',format:'esm',write:false,plugins:[{name:'outbound-fixture',setup(b){
 b.onResolve({filter:/^@neondatabase\/serverless$/},()=>({path:'driver',namespace:'outbound-fixture'}));
 b.onResolve({filter:/^\.\/storage$/},()=>({path:'logging',namespace:'outbound-fixture'}));
 b.onLoad({filter:/.*/,namespace:'outbound-fixture'},({path})=>({contents:path==='logging'?`export const log=(event,fields)=>globalThis.${key}.logs.push({event,...fields});`:`
 export const types={setTypeParser(){}};
 // Neon query results are lazy thenables: nothing is sent until awaited.
 export const neon=()=>({query:(sql,bindings,options)=>({then:(resolve,reject)=>globalThis.${key}.http(sql,bindings,options).then(resolve,reject)}),transaction:(queries,options)=>globalThis.${key}.http('transaction',[],options).then(rows=>queries.map(()=>rows))});
 export class Client {constructor(){throw Error('no writer in this fixture');}}
 `,loader:'js'}));
}}]});
const {PostgresStore,OutboundGate,StoreUnavailable,HTTP_READ_CONCURRENCY}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
function fixture(){
 const pending=[],state={active:0,max:0,signals:[]};
 globalThis[key]={logs:[],http(sql,bindings,options){
  state.active++;state.max=Math.max(state.max,state.active);state.signals.push(options?.fetchOptions?.signal);
  return new Promise(resolve=>pending.push(()=>{state.active--;resolve([{ok:1}]);}));
 }};
 return {pending,state,store:new PostgresStore('postgresql://unused@ep-fixture.us-east-2.aws.neon.tech/fixture','100','test-v1')};
}
const tick=()=>new Promise(resolve=>setTimeout(resolve,0));
test('HTTP reads never exceed the outbound concurrency limit',async()=>{
 const f=fixture();
 const reads=Array.from({length:20},(_,i)=>f.store.exec('SELECT ? AS n',i));
 await tick();
 assert.equal(f.state.active,HTTP_READ_CONCURRENCY);
 assert.ok(HTTP_READ_CONCURRENCY<6,'leave connections for the writer WebSocket and R2');
 const snapshot=f.store.outbound().http_reads;assert.equal(snapshot.in_flight,HTTP_READ_CONCURRENCY);assert.equal(snapshot.waiting,20-HTTP_READ_CONCURRENCY);
 while(f.pending.length || f.state.active){f.pending.splice(0).forEach(release=>release());await tick();}
 assert.equal((await Promise.all(reads)).length,20);
 assert.equal(f.state.max,HTTP_READ_CONCURRENCY);
 const done=f.store.outbound().http_reads;assert.equal(done.in_flight,0);assert.equal(done.waiting,0);assert.equal(done.completed,20);
});
test('read batches and reads share one gate',async()=>{
 const f=fixture();
 const work=[...Array.from({length:3},()=>f.store.batch([{sql:'SELECT 1'},{sql:'SELECT 2'}])),...Array.from({length:3},()=>f.store.exec('SELECT 1'))];
 await tick();assert.equal(f.state.active,HTTP_READ_CONCURRENCY);
 while(f.pending.length || f.state.active){f.pending.splice(0).forEach(release=>release());await tick();}
 await Promise.all(work);assert.equal(f.state.max,HTTP_READ_CONCURRENCY);
});
test('the read timeout starts only after admission',async()=>{
 const f=fixture();
 const reads=Array.from({length:HTTP_READ_CONCURRENCY+1},()=>f.store.exec('SELECT 1'));
 await tick();assert.equal(f.state.signals.length,HTTP_READ_CONCURRENCY);
 f.pending.shift()();await tick();await tick();
 assert.equal(f.state.signals.length,HTTP_READ_CONCURRENCY+1);assert.ok(f.state.signals.every(signal=>signal && !signal.aborted));
 while(f.pending.length || f.state.active){f.pending.splice(0).forEach(release=>release());await tick();}
 await Promise.all(reads);
});
test('a full queue sheds immediately as database unavailable',async()=>{
 const gate=new OutboundGate(1,2,60000);const releases=[];
 const hold=()=>gate.run(()=>new Promise(resolve=>releases.push(resolve)));
 const first=hold(),second=hold(),third=hold();
 await assert.rejects(hold(),error=>error instanceof StoreUnavailable);
 assert.equal(gate.snapshot().shed_queue_full,1);assert.equal(gate.snapshot().waiting,2);
 while(releases.length || gate.inFlight){releases.splice(0).forEach(release=>release());await tick();}
 await Promise.all([first,second,third]);assert.equal(gate.snapshot().completed,3);
});
test('a read waiting past the admission bound is shed and leaves the queue',async()=>{
 const gate=new OutboundGate(1,10,20);let release;
 const first=gate.run(()=>new Promise(resolve=>{release=resolve;}));
 await assert.rejects(gate.run(async()=>'late'),error=>error instanceof StoreUnavailable);
 assert.equal(gate.waiting,0);assert.equal(gate.snapshot().shed_wait,1);
 release();await first;assert.equal(gate.inFlight,0);
 assert.equal(await gate.run(async()=>'next'),'next');
});
test('a failed read releases its slot',async()=>{
 const gate=new OutboundGate(1,10,1000);
 await assert.rejects(gate.run(async()=>{throw Error('boom');}),/boom/);
 assert.equal(gate.inFlight,0);assert.equal(gate.snapshot().failed,1);
 assert.equal(await gate.run(async()=>7),7);
});
test('an expired waiter is shed at handoff even before its timer fires',async()=>{
 const gate=new OutboundGate(1,10,5);let release;
 const first=gate.run(()=>new Promise(resolve=>{release=resolve;}));
 let started=false;const second=gate.run(async()=>{started=true;});
 const spin=Date.now();while(Date.now()-spin<15){}
 release();await first;
 await assert.rejects(second,error=>error instanceof StoreUnavailable);
 assert.equal(started,false);assert.equal(gate.inFlight,0);assert.equal(gate.snapshot().shed_wait,1);
});
test('priority waiters are admitted first and bypass the ordinary queue bound',async()=>{
 const gate=new OutboundGate(1,1,60000);const releases=[],order=[];
 const first=gate.run(()=>new Promise(resolve=>releases.push(resolve)));
 const ordinary=gate.run(async()=>{order.push('ordinary');});
 const cleanup=gate.run(async()=>{order.push('cleanup');},{priority:true});
 releases.shift()();await Promise.all([first,ordinary,cleanup]);
 assert.deepEqual(order,['cleanup','ordinary']);
});
test('an aborted waiter leaves the queue without consuming a slot',async()=>{
 const gate=new OutboundGate(1,10,60000);let release;
 const first=gate.run(()=>new Promise(resolve=>{release=resolve;}));
 const abort=new AbortController();let started=false;
 const waiter=gate.run(async()=>{started=true;},{signal:abort.signal});
 abort.abort();await assert.rejects(waiter,error=>error instanceof StoreUnavailable);
 assert.equal(gate.waiting,0);release();await first;assert.equal(gate.inFlight,0);assert.equal(started,false);
});
