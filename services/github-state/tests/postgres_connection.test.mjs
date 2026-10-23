import {test} from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
const key='__repositoryConnectionFixture';
const bundle=await build({entryPoints:['src/repository_store.ts'],bundle:true,platform:'node',format:'esm',write:false,plugins:[{name:'connection-fixture',setup(b){
 b.onResolve({filter:/^@neondatabase\/serverless$/},()=>({path:'driver',namespace:'connection-fixture'}));
 b.onResolve({filter:/^\.\/storage$/},()=>({path:'logging',namespace:'connection-fixture'}));
 b.onLoad({filter:/.*/,namespace:'connection-fixture'},({path})=>({contents:path==='logging'?`export const log=(event,fields)=>globalThis.${key}.log(event,fields);`:`
 export const types={setTypeParser(){}};
 export const neon=()=>({query:(...args)=>globalThis.${key}.http(...args),transaction:queries=>Promise.all(queries)});
 export class Client {constructor(){return globalThis.${key}.client();}}
 `,loader:'js'}));
}}]});
const {PostgresStore,StoreFenced,StoreUnavailable}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
function fixture(scenarios=[]){
 const clients=[],committed=[],logs=[],cleanupLogs=[],cleanupCalls=[],cleanupResponses=[];
 const driver={log:(event,fields)=>(event==='repository_postgres_cleanup'?cleanupLogs:logs).push({event,...fields}),async http(sql,bindings){
  if(!sql.includes('AS terminated FROM pg_stat_activity'))throw Error('private HTTP transport diagnostic');
  cleanupCalls.push({sql,bindings});
  if(cleanupResponses.length)return cleanupResponses.shift()();
  const client=clients.find(c=>c.identity?.pid===bindings[0] && c.identity.started_us===bindings[1] && c.identity.token===bindings[2] && c.sessionActive);
  if(!client)return [];
  if(sql.startsWith('SELECT false'))return [{terminated:false}];
  client.sessionActive=false;client.serverTerminated=true;client.rollbackPending();return [{terminated:true}];
 },client(){
  const scenario=scenarios.shift() ?? {},handlers={},pending=[];
  const fail=()=>{throw Object.assign(new Error(scenario.message ?? 'private driver text postgresql://secret and payload'),scenario.code?{code:scenario.code,severity:scenario.severity}:{});};
  const client={commands:[],destroyed:false,ended:false,on(event,fn){handlers[event]=fn;},
   neonConfig:{destroy(){client.destroyed=true;handlers.end?.();}},
   rollbackPending(){pending.length=0;},
   async connect(){if(scenario.connectPending)return new Promise(()=>{});if(scenario.phase==='connect')fail();},
   async query(sql,bindings){
    const phase=sql.startsWith('BEGIN')?'begin':sql.includes("set_config('application_name'")?'session':sql.includes('backend_control')?'owner':sql==='COMMIT'?'commit':sql==='ROLLBACK'?'rollback':'apply';client.commands.push(phase);
    if(phase===scenario.phase){if(scenario.acceptCommit){committed.push(...pending.splice(0));client.sessionActive=false;}fail();}
    // A client timeout leaves an active query; a rollback queued behind it
    // cannot finish. Cleanup must discard the socket without awaiting it.
    if(phase==='rollback'){if(scenario.stalled)return new Promise(()=>{});pending.length=0;client.sessionActive=false;return {rows:[]};}
    if(phase==='session'){client.identity={pid:clients.indexOf(client)+100,started_us:'1234567890123456',token:bindings[0]};client.sessionActive=true;return {rows:[scenario.identity?{...client.identity,...scenario.identity}:client.identity]};}
    if(phase==='owner')return {rows:[{backend:scenario.fenced?'sqlite':'postgres',generation:'test-v1',schema_version:1}]};
    if(phase==='commit'){client.sessionActive=false;if(scenario.commitTag==='ROLLBACK')pending.length=0;else committed.push(...pending.splice(0));return {command:scenario.commitTag ?? 'COMMIT',rows:[]};}
    if(phase==='apply')pending.push('write');return {rows:[]};
   },async end(){client.ended=true;handlers.end?.();}
  };clients.push(client);return client;
 }};
 globalThis[key]=driver;
 return {clients,committed,logs,cleanupLogs,cleanupCalls,cleanupResponses,store:new PostgresStore('postgresql://unused@ep-fixture.us-east-2.aws.neon.tech/fixture','100','test-v1')};
}
const write=tx=>tx.exec('INSERT INTO demand VALUES(?,?)','private-input',1);
async function promptly(promise){let timer;try{return await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('writer blocked behind stalled cleanup')),1000);})]);}finally{clearTimeout(timer);}}
for(const phase of ['connect','begin','session','owner','apply','commit'])test(`uncoded ${phase} failure releases the writer without retry`,async()=>{
 const f=fixture([{phase,stalled:true}]);let callbacks=0;
 const first=assert.rejects(f.store.transaction(async tx=>{callbacks++;await write(tx);}),/^Error: Repository database unavailable$/);
 const next=f.store.transaction(write);await promptly(Promise.all([first,next]));
 assert.equal(callbacks,['apply','commit'].includes(phase)?1:0);assert.equal(f.clients.length,2);
 assert.equal(f.clients[0].destroyed,true);assert.equal(f.clients[0].ended,false);assert.ok(!f.clients[0].commands.includes('rollback'));
 assert.deepEqual(f.committed,['write']);
 const failure=f.logs.find(v=>v.failed);assert.equal(failure.phase,phase);assert.equal(failure.code,null);assert.equal(failure.connection_discarded,true);assert.ok(failure.wait_ms>=0);assert.ok(failure.transaction_ms>=0);
 assert.ok(!JSON.stringify(f.logs).includes('private'));
});
for(const code of ['08006','57P01','57P02','57P03','ECONNRESET','INVALID','EPIPE','EPERM','EBADF'])test(`${code} transport failure discards its connection`,async()=>{
 const severity=/^(08|57)/.test(code)?'FATAL':undefined;const f=fixture([{phase:'apply',code,severity,stalled:true}]);await promptly(assert.rejects(f.store.transaction(write),/Repository database unavailable/));
 assert.equal(f.clients[0].destroyed,true);assert.ok(!f.clients[0].commands.includes('rollback'));
 assert.equal(f.logs[0].code,severity?code:null);
});
for(const code of ['23505','57014','55P03'])test(`${code} server error retains normal rollback`,async()=>{
 const f=fixture([{phase:'apply',code,severity:'ERROR'}]);await assert.rejects(f.store.transaction(write),new RegExp(code));
 assert.deepEqual(f.clients[0].commands,['begin','session','owner','apply','rollback']);assert.equal(f.clients[0].ended,true);assert.equal(f.clients[0].destroyed,false);assert.deepEqual(f.committed,[]);
});
test('application exceptions and fencing retain their meaning and roll back',async()=>{
 const f=fixture();const error=new Error('private application error');await assert.rejects(f.store.transaction(async tx=>{await write(tx);throw error;}),e=>e===error);
 assert.equal(f.clients[0].commands.at(-1),'rollback');assert.deepEqual(f.committed,[]);assert.ok(!JSON.stringify(f.logs).includes('private'));
 const fenced=fixture([{fenced:true}]);await assert.rejects(fenced.store.transaction(write),StoreFenced);assert.equal(fenced.clients[0].commands.at(-1),'rollback');assert.equal(fenced.logs[0].reason,'fenced');
});
test('swallowed timeouts cannot commit; an uncertain commit never retries the callback',async()=>{
 const f=fixture([{phase:'apply',stalled:true}]);await promptly(assert.rejects(f.store.transaction(async tx=>{try{await write(tx);}catch{}return 'swallowed';}),/Repository database unavailable/));
 assert.ok(!f.clients[0].commands.includes('commit'));assert.deepEqual(f.committed,[]);
 const unknown=fixture([{phase:'commit',stalled:true,acceptCommit:true}]);let callbacks=0;
 await promptly(assert.rejects(unknown.store.transaction(async tx=>{callbacks++;await write(tx);}),/Repository database unavailable/));
 assert.equal(callbacks,1);assert.equal(unknown.clients.length,1);assert.deepEqual(unknown.committed,['write']);assert.equal(unknown.clients[0].destroyed,true);assert.equal(unknown.logs[0].outcome,'unknown');
});
test('logging failure cannot change a committed result; HTTP diagnostics withhold driver details',async()=>{
 const f=fixture();let logged=0;globalThis[key].log=()=>{logged++;throw Error('logging unavailable');};
 const now=Date.now;let clock=now();Date.now=()=>clock+=300;try{assert.equal(await f.store.transaction(async()=>42),42);}finally{Date.now=now;}assert.equal(logged,1);
 const http=fixture();await assert.rejects(http.store.exec('SELECT 1'),/^Error: Repository database unavailable$/);assert.deepEqual(http.logs,[{event:'repository_database_failure',phase:'http',code:null,reason:'driver_error'}]);
});

for(const [phase,code,severity] of [['connect','53300','FATAL'],['connect','28P01','FATAL'],['connect','3D000','FATAL'],['begin','53300','FATAL'],['begin','28P01','FATAL'],['begin','3D000','FATAL'],['apply','25P03','FATAL'],['apply','08006','ERROR']])test(`${phase} ${severity} ${code} cannot queue rollback`,async()=>{const f=fixture([{phase,code,severity,stalled:true}]);await promptly(assert.rejects(f.store.transaction(write),/Repository database unavailable/));assert.equal(f.clients[0].destroyed,true);assert.ok(!f.clients[0].commands.includes('rollback'));});

test('a callback swallowing a timeout cannot hold the local queue with non-database work',async()=>{const f=fixture([{phase:'apply',stalled:true}]);let release;const gate=new Promise(r=>release=r);try{const first=assert.rejects(f.store.transaction(async tx=>{try{await write(tx);}catch{}await gate;await write(tx);}),/Repository database unavailable/);await promptly(Promise.all([first,f.store.transaction(write)]));assert.equal(f.clients.length,2);assert.deepEqual(f.committed,['write']);}finally{release();}});

test('a stalled rollback preserves the application error and closes the connection within its cleanup deadline',async()=>{
 const f=fixture([{stalled:true}]),error=Error('private application error'),start=performance.now();
 await assert.rejects(f.store.transaction(async tx=>{await write(tx);throw error;}),e=>e===error);
 assert.ok(performance.now()-start<3500);assert.equal(f.clients[0].destroyed,true);assert.equal(f.logs[0].reason,'application');assert.equal(f.logs[0].cleanup,'rollback_timeout');
 assert.deepEqual(f.committed,[]);await f.store.transaction(write);assert.deepEqual(f.committed,['write']);
});

test('a missing connect close event cannot hold the writer after its own deadline',async t=>{const f=fixture([{connectPending:true}]);t.mock.timers.enable({apis:['setTimeout']});try{const failed=assert.rejects(f.store.transaction(write),/08001/);await new Promise(setImmediate);t.mock.timers.tick(10000);await failed;assert.equal(f.clients[0].destroyed,true);assert.ok(!f.clients[0].commands.includes('rollback'));assert.equal(f.logs[0].reason,'connect_timeout');await f.store.transaction(write);assert.deepEqual(f.committed,['write']);}finally{t.mock.timers.reset();}});

test('client timeout diagnostics use a fixed reason without logging driver text',async()=>{const f=fixture([{phase:'apply',message:'Query read timeout',stalled:true}]);await promptly(assert.rejects(f.store.transaction(write),/Repository database unavailable/));assert.equal(f.logs[0].reason,'client_timeout');assert.ok(!JSON.stringify(f.logs).includes('Query read timeout'));});

test('a confirmed server COMMIT error retains its cause when rollback cleanup times out',async()=>{const f=fixture([{phase:'commit',code:'57014',severity:'ERROR',stalled:true}]);await assert.rejects(f.store.transaction(write),/57014/);assert.equal(f.logs[0].outcome,'failed');assert.equal(f.logs[0].code,'57014');assert.equal(f.logs[0].reason,'server_error');assert.equal(f.logs[0].cleanup,'rollback_timeout');assert.equal(f.clients[0].destroyed,true);assert.deepEqual(f.committed,[]);});

test('a ROLLBACK commit tag is a definite failure even if subsequent cleanup stalls',async()=>{const f=fixture([{commitTag:'ROLLBACK',stalled:true}]);await assert.rejects(f.store.transaction(write),/25P02/);assert.equal(f.logs[0].outcome,'failed');assert.equal(f.logs[0].reason,'commit_rejected');assert.equal(f.logs[0].cleanup,'rollback_timeout');assert.deepEqual(f.committed,[]);});

test('connection loss before COMMIT is sent is a definite failure',async()=>{const f=fixture();await assert.rejects(f.store.transaction(async tx=>{await write(tx);tx.connectionState.error=new StoreUnavailable('08006');tx.connectionState.reason='connection_lost';}),/08006/);assert.ok(!f.clients[0].commands.includes('commit'));assert.equal(f.logs[0].outcome,'failed');assert.deepEqual(f.committed,[]);assert.ok(!JSON.stringify(f.logs).includes('private'));});


test('lost owner response terminates its identified session before the next writer',async()=>{
 const f=fixture([{phase:'owner',stalled:true}]);let callbacks=0;
 await promptly(Promise.all([assert.rejects(f.store.transaction(async tx=>{callbacks++;await write(tx);}),StoreUnavailable),f.store.transaction(write)]));
 assert.equal(callbacks,0);assert.equal(f.clients[0].serverTerminated,true);assert.equal(f.cleanupCalls.length,1);
 assert.deepEqual(f.cleanupCalls[0].bindings,Object.values(f.clients[0].identity));
 assert.deepEqual(f.committed,['write']);assert.equal(f.cleanupLogs[0].result,'terminated');
 assert.ok(!JSON.stringify(f.cleanupLogs).includes(f.clients[0].identity.token));
});

test('unconfirmed session cleanup blocks every writer but permits stored reads',async()=>{
 const f=fixture([{phase:'apply',stalled:true}]);
 f.cleanupResponses.push(async()=>{throw Error('private cleanup error');},async()=>[{terminated:false}]);
 await assert.rejects(f.store.transaction(write),StoreUnavailable);
 let callbacks=0;await assert.rejects(f.store.transaction(async()=>callbacks++),StoreUnavailable);
 assert.equal(callbacks,0);assert.equal(f.clients.length,1);assert.equal(f.clients[0].sessionActive,true);
 f.cleanupResponses.push(async()=>{throw Error('private cleanup error');});
 await assert.rejects(f.store.exec('INSERT INTO demand VALUES(?,?)','input',1),StoreUnavailable);assert.equal(f.clients.length,1);
 globalThis[key].http=async sql=>sql.includes('capacity_statistics')?[]:sql.includes('backend_control')?[{backend:'postgres',generation:'test-v1',schema_version:1}]:[{value:42}];assert.equal((await f.store.exec('SELECT 42 AS value')).one().value,42);
 const health=await f.store.health();assert.equal(health.connected,true);assert.equal(health.session_cleanup_pending,true);assert.ok(health.session_cleanup_age_ms>=0);assert.equal(f.cleanupCalls.length,1,'queued writers share the cleanup cooldown');
 assert.ok(!JSON.stringify([...f.logs,...f.cleanupLogs]).includes('private cleanup'));
});

test('cleanup has an independent deadline even when HTTP ignores abort',async t=>{
 const f=fixture([{phase:'apply',stalled:true}]);f.cleanupResponses.push(()=>new Promise(()=>{}));
 t.mock.timers.enable({apis:['setTimeout','Date'],now:Date.now()});
 try{const failed=assert.rejects(f.store.transaction(write),StoreUnavailable);await new Promise(setImmediate);t.mock.timers.tick(3000);await failed;
 assert.equal(f.cleanupLogs[0].result,'unconfirmed');await assert.rejects(f.store.transaction(write),StoreUnavailable);t.mock.timers.tick(1000);await f.store.transaction(write);assert.equal(f.clients[0].serverTerminated,true);assert.deepEqual(f.committed,['write']);assert.equal(f.logs.find(v=>v.outcome==='committed').session_cleanup,'terminated');}finally{t.mock.timers.reset();}
});

test('pooler connections are observed without signaling a reusable backend',async t=>{
 t.mock.timers.enable({apis:['Date'],now:Date.now()});try{
 const f=fixture([{phase:'apply',stalled:true}]);f.store.url='postgresql://unused@ep-fixture-pooler.us-east-2.aws.neon.tech/fixture';
 await assert.rejects(f.store.transaction(write),StoreUnavailable);assert.equal(f.clients[0].serverTerminated,undefined);
 await assert.rejects(f.store.transaction(write),StoreUnavailable);assert.equal(f.clients.length,1);assert.equal(f.cleanupLogs[0].result,'unconfirmed');
 f.clients[0].sessionActive=false;t.mock.timers.tick(1000);await f.store.transaction(write);assert.equal(f.clients.length,2);assert.equal(f.cleanupLogs.at(-1).result,'already_ended');
 }finally{t.mock.timers.reset();}
});

test('uncertain committed transaction ends without retry or termination of a completed session',async()=>{
 const f=fixture([{phase:'commit',stalled:true,acceptCommit:true}]);let callbacks=0;
 await assert.rejects(f.store.transaction(async tx=>{callbacks++;await write(tx);}),StoreUnavailable);
 assert.equal(f.cleanupLogs[0].result,'already_ended');assert.equal(f.logs[0].outcome,'unknown');assert.equal(f.clients[0].serverTerminated,undefined);
 await f.store.transaction(write);assert.equal(callbacks,1);assert.deepEqual(f.committed,['write','write']);
});

test('standalone writes use the same session ownership and cleanup boundary',async()=>{
 const f=fixture([{phase:'apply',stalled:true}]);await assert.rejects(write(f.store),StoreUnavailable);
 assert.equal(f.clients[0].serverTerminated,true);await write(f.store);assert.deepEqual(f.committed,['write']);assert.ok(f.clients.every(c=>c.commands.includes('session')));
});


test('invalid session identity refuses ownership and never calls application code',async()=>{
 for(const identity of [{pid:0},{started_us:123},{started_us:'not-a-timestamp'},{token:'different-token'}]){
  const f=fixture([{identity}]);let callbacks=0;await assert.rejects(f.store.transaction(async()=>callbacks++),StoreUnavailable);
  assert.equal(callbacks,0);assert.ok(!f.clients[0].commands.includes('owner'));assert.equal(f.cleanupCalls.length,0);assert.equal(f.clients[0].commands.at(-1),'rollback');
 }
});


test('HTTP cleanup outage cannot fence healthy WebSocket writes indefinitely',async t=>{
 const f=fixture([{phase:'apply',stalled:true}]);globalThis[key].http=async()=>{throw Error('private HTTP-only outage');};
 t.mock.timers.enable({apis:['Date'],now:Date.now()});
 try{
  await assert.rejects(f.store.transaction(write),StoreUnavailable);assert.equal(f.clients.length,1);
  let callbacks=0;await assert.rejects(f.store.transaction(async()=>callbacks++),StoreUnavailable);
  assert.equal(callbacks,0);assert.equal(f.clients.length,1);assert.equal(f.cleanupLogs.length,1);
  // The real server has expired its old transaction, while HTTP stays down.
  f.clients[0].sessionActive=false;t.mock.timers.tick(45000);
  await f.store.transaction(async tx=>{callbacks++;await write(tx);});assert.equal(callbacks,1);
  assert.deepEqual(f.committed,['write']);assert.equal(f.cleanupLogs.at(-1).result,'expired');assert.equal(f.store.discarded,undefined);
  assert.equal(f.clients[0].commands.filter(v=>v==='apply').length,1,'the failed callback was never retried');
  assert.ok(!JSON.stringify([...f.logs,...f.cleanupLogs]).includes('private HTTP-only'));
 }finally{t.mock.timers.reset();}
});
