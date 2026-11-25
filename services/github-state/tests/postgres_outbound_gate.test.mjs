import {test} from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
const key='__repositoryOutboundFixture';
const bundle=await build({entryPoints:['src/repository_store.ts'],bundle:true,platform:'node',format:'esm',write:false,plugins:[{name:'outbound-fixture',setup(b){
 b.onResolve({filter:/^@neondatabase\/serverless$/},()=>({path:'driver',namespace:'outbound-fixture'}));
 b.onResolve({filter:/^\.\/storage$/},()=>({path:'logging',namespace:'outbound-fixture'}));
 b.onLoad({filter:/.*/,namespace:'outbound-fixture'},({path})=>({contents:path==='logging'?`export const log=(event,fields)=>globalThis.${key}.logs.push({event,...fields});`:`
 export const types={setTypeParser(){}};
 export const neonConfig={};
 // Neon query results are lazy thenables: nothing is sent until awaited.
 export const neon=()=>({query:(sql,bindings,options)=>({then:(resolve,reject)=>globalThis.${key}.http(sql,bindings,options).then(resolve,reject)}),transaction:(queries,options)=>globalThis.${key}.http('transaction',[],options).then(rows=>queries.map(()=>rows))});
 export class Client {constructor(){throw Error('no writer in this fixture');}}
 `,loader:'js'}));
}}]});
const {PostgresStore,OutboundGate,StoreUnavailable,HTTP_READ_CONCURRENCY,HEAVY_READ_CONCURRENCY,HEAVY_READ,isHeavyRead}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
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
test('heavy reads leave at least one slot for small reads',async()=>{
 const f=fixture();
 assert.ok(HEAVY_READ_CONCURRENCY<HTTP_READ_CONCURRENCY);
 const page='SELECT * FROM (SELECT stored_page.* FROM (SELECT 1) AS stored_page) AS bounded_page WHERE true';
 assert.equal(isHeavyRead(page),true);assert.equal(isHeavyRead('SELECT jobs.* FROM jobs LIMIT 100'+HEAVY_READ),true);assert.equal(isHeavyRead('SELECT data FROM resources WHERE id=?'),false);
 const heavy=[...Array.from({length:2},()=>f.store.exec(page)),...Array.from({length:2},()=>f.store.batch([{sql:'SELECT 1'+HEAVY_READ},{sql:'SELECT 2'+HEAVY_READ}]))];
 await tick();
 assert.equal(f.state.active,HEAVY_READ_CONCURRENCY);
 let snapshot=f.store.outbound().http_reads;assert.equal(snapshot.in_flight_heavy,HEAVY_READ_CONCURRENCY);assert.equal(snapshot.waiting_heavy,4-HEAVY_READ_CONCURRENCY);
 // A small read arriving behind waiting heavy reads takes the reserved slot.
 const small=f.store.exec('SELECT 1');await tick();
 assert.equal(f.state.active,HEAVY_READ_CONCURRENCY+1);
 f.pending.pop()();assert.deepEqual((await small).toArray(),[{ok:1}]);
 snapshot=f.store.outbound().http_reads;assert.equal(snapshot.by_class.small.admitted,1);assert.equal(snapshot.by_class.heavy.admitted,HEAVY_READ_CONCURRENCY);
 while(f.pending.length || f.state.active){f.pending.splice(0).forEach(release=>release());await tick();}
 await Promise.all(heavy);assert.equal(f.state.max,HEAVY_READ_CONCURRENCY+1);
 snapshot=f.store.outbound().http_reads;assert.equal(snapshot.by_class.heavy.admitted,4);assert.equal(snapshot.in_flight_heavy,0);
});
test('a heavy read waiting for the heavy cap does not block small reads queued behind it',async()=>{
 const gate=new OutboundGate(3,10,60000);const releases=new Map(),order=[];
 const hold=(heavy,name)=>gate.run(()=>new Promise(resolve=>releases.set(name,resolve)).then(()=>order.push(name)),{heavy});
 const held=[hold(true,'h1'),hold(true,'h2'),hold(false,'s1')];await tick();
 assert.equal(gate.inFlight,3);assert.equal(gate.snapshot().in_flight_heavy,2);
 held.push(hold(true,'h3'),hold(false,'s2'));await tick();
 // A small read frees its slot; the heavy cap is full, so the small read
 // queued behind the waiting heavy read is admitted first.
 releases.get('s1')();await tick();
 assert.ok(releases.has('s2'));assert.ok(!releases.has('h3'));assert.equal(gate.snapshot().waiting_heavy,1);
 // Once a heavy read finishes, the waiting heavy read takes its slot.
 releases.get('h1')();await tick();assert.ok(releases.has('h3'));assert.equal(gate.waiting,0);
 for(const name of ['h2','s2','h3'])releases.get(name)();
 await Promise.all(held);assert.equal(gate.snapshot().completed,5);assert.equal(gate.inFlight,0);
});
test('heavy waiters fill at most half the queue',async()=>{
 const gate=new OutboundGate(3,8,60000);const releases=[];
 const hold=heavy=>gate.run(()=>new Promise(resolve=>releases.push(resolve)),{heavy});
 const held=[hold(true),hold(true),hold(false),...Array.from({length:4},()=>hold(true))];await tick();
 assert.equal(gate.snapshot().waiting_heavy,4);
 await assert.rejects(hold(true),error=>error instanceof StoreUnavailable);
 assert.equal(gate.snapshot().by_class.heavy.shed_queue_full,1);
 held.push(...Array.from({length:4},()=>hold(false)));await tick();
 assert.equal(gate.waiting,8);assert.equal(gate.snapshot().by_class.small.shed_queue_full,0);
 while(releases.length || gate.inFlight){releases.splice(0).forEach(release=>release());await tick();}
 await Promise.all(held);assert.equal(gate.snapshot().completed,11);
});
test('an unmarked read batch stays small',async()=>{
 const f=fixture();
 const work=[f.store.batch([{sql:'SELECT 1'},{sql:'SELECT 2'}]),f.store.batch([{sql:'SELECT 3'},{sql:'SELECT 4'+HEAVY_READ}])];await tick();
 const snapshot=f.store.outbound().http_reads;assert.equal(snapshot.by_class.small.admitted,1);assert.equal(snapshot.by_class.heavy.admitted,1);
 while(f.pending.length || f.state.active){f.pending.splice(0).forEach(release=>release());await tick();}
 await Promise.all(work);
});
test('a shed heavy read is counted by class',async()=>{
 const gate=new OutboundGate(2,10,20);let release;
 const first=gate.run(()=>new Promise(resolve=>{release=resolve;}),{heavy:true});
 await assert.rejects(gate.run(async()=>'late',{heavy:true}),error=>error instanceof StoreUnavailable);
 assert.equal(gate.snapshot().by_class.heavy.shed_wait,1);assert.equal(gate.snapshot().by_class.small.shed_wait,0);
 assert.equal(await gate.run(async()=>'small'),'small');
 release();await first;assert.equal(gate.inFlight,0);
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
test('diagnostics name running and slow reads by SQL text only',async()=>{
 const gate=new OutboundGate(2,10,1000);let release;
 const held=gate.run(()=>new Promise(resolve=>{release=resolve;}),{label:'SELECT  data\n FROM resources WHERE id=?'});
 await tick();
 const running=gate.snapshot().running;assert.equal(running.length,1);assert.equal(running[0].label,'SELECT data FROM resources WHERE id=?');
 release();await held;
 await assert.rejects(gate.run(async()=>{throw Error('private value');},{label:'SELECT 1'}));
 const snapshot=gate.snapshot();assert.equal(snapshot.running.length,0);assert.equal(snapshot.recent_slow.at(-1).label,'SELECT 1');assert.equal(snapshot.recent_slow.at(-1).ok,false);
 assert.ok(!JSON.stringify(snapshot).includes('private'));
});
const {__timedFetchForTests:timedFetch,resendable,HEADER_TIMEOUT_MS,HEADER_ATTEMPTS}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
async function withFetch(plan,run){
 const original=globalThis.fetch,calls=[];
 globalThis.fetch=(input,init)=>{const behaviour=plan[calls.length] ?? 'ok';calls.push(init);
  if(behaviour==='hang')return new Promise((_,reject)=>init.signal.addEventListener('abort',()=>reject(init.signal.reason)));
  return Promise.resolve(new Response('{"rows":[]}',{status:200}));};
 try{return await run(calls);}finally{globalThis.fetch=original;}
}
const select=JSON.stringify({queries:[{query:'SELECT value FROM meta WHERE key=$1',params:['x']}]}),readOnly={'Neon-Batch-Read-Only':'true'};
test('a read whose headers stall is resent within its deadline',async()=>{
 await withFetch(['hang','ok'],async calls=>{
  const started=Date.now();const response=await timedFetch('https://api.example/sql',{method:'POST',body:select,headers:readOnly,signal:AbortSignal.timeout(15000)});
  assert.equal(await response.text(),'{"rows":[]}');assert.equal(calls.length,2);
  assert.ok(Date.now()-started>=HEADER_TIMEOUT_MS-50 && Date.now()-started<HEADER_TIMEOUT_MS+1000);
 });
});
test('the final attempt waits for the caller deadline and its abort propagates',async()=>{
 await withFetch(Array(HEADER_ATTEMPTS).fill('hang'),async calls=>{
  const outer=new AbortController();setTimeout(()=>outer.abort(new Error('caller deadline')),HEADER_TIMEOUT_MS*(HEADER_ATTEMPTS-1)+300);
  await assert.rejects(timedFetch('https://api.example/sql',{method:'POST',body:select,headers:readOnly,signal:outer.signal}),/caller deadline/);
  assert.equal(calls.length,HEADER_ATTEMPTS);
 });
});
test('only database-enforced read-only batches are resent',async()=>{
 assert.equal(resendable({body:select,headers:readOnly}),true);
 assert.equal(resendable({body:select,headers:new Headers(readOnly)}),true);
 assert.equal(resendable({body:select,headers:{}}),false,'a batch without the read-only header');
 assert.equal(resendable({body:JSON.stringify({query:'SELECT 1',params:[]}),headers:readOnly}),false,'a standalone query');
 assert.equal(resendable({body:JSON.stringify({queries:[{query:'UPDATE jobs SET due=$1',params:[1]}]}),headers:readOnly}),false);
 assert.equal(resendable({body:'not json',headers:readOnly}),false);
 await withFetch(['hang'],async calls=>{
  const outer=new AbortController();setTimeout(()=>outer.abort(new Error('caller deadline')),HEADER_TIMEOUT_MS+500);
  await assert.rejects(timedFetch('https://api.example/sql',{method:'POST',body:select,headers:{},signal:outer.signal}),/caller deadline/);
  assert.equal(calls.length,1);
 });
});
test('the caller abort still cancels a stalled body after headers',async()=>{
 const original=globalThis.fetch;let bodySignal;
 globalThis.fetch=(input,init)=>{bodySignal=init.signal;return Promise.resolve(new Response(new ReadableStream({start(c){init.signal.addEventListener('abort',()=>c.error(init.signal.reason));}})));};
 try{
  const outer=new AbortController();setTimeout(()=>outer.abort(new Error('caller deadline')),50);
  await assert.rejects(timedFetch('https://api.example/sql',{method:'POST',body:select,headers:readOnly,signal:outer.signal}),/caller deadline/);
  assert.equal(bodySignal.aborted,true);
 }finally{globalThis.fetch=original;}
});
test('HTTP queries go to the compute endpoint host',async()=>{
 const {neonFetchEndpoint}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
 assert.equal(neonFetchEndpoint('ep-fixture.c-7.us-east-2.aws.neon.tech'),'https://ep-fixture.c-7.us-east-2.aws.neon.tech/sql');
});
