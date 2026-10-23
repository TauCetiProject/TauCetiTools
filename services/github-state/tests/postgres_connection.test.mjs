import {test} from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
const key='__repositoryConnectionFixture';
const bundle=await build({entryPoints:['src/repository_store.ts'],bundle:true,platform:'node',format:'esm',write:false,plugins:[{name:'connection-fixture',setup(b){
 b.onResolve({filter:/^@neondatabase\/serverless$/},()=>({path:'driver',namespace:'connection-fixture'}));
 b.onResolve({filter:/^\.\/storage$/},()=>({path:'logging',namespace:'connection-fixture'}));
 b.onLoad({filter:/.*/,namespace:'connection-fixture'},({path})=>({contents:path==='logging'?`export const log=(event,fields)=>globalThis.${key}.log(event,fields);`:`
 export const types={setTypeParser(){}};
 export const neon=()=>({query:async()=>{throw new Error('private HTTP transport diagnostic');}});
 export class Client {constructor(){return globalThis.${key}.client();}}
 `,loader:'js'}));
}}]});
const {PostgresStore,StoreFenced}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
function fixture(scenarios=[]){
 const clients=[],committed=[],logs=[];
 const driver={log:(event,fields)=>logs.push({event,...fields}),client(){
  const scenario=scenarios.shift() ?? {},handlers={},pending=[];
  const fail=()=>{throw Object.assign(new Error(scenario.message ?? 'private driver text postgresql://secret and payload'),scenario.code?{code:scenario.code,severity:scenario.severity}:{});};
  const client={commands:[],destroyed:false,ended:false,on(event,fn){handlers[event]=fn;},
   neonConfig:{destroy(){client.destroyed=true;pending.length=0;handlers.end?.();}},
   async connect(){if(scenario.connectPending)return new Promise(()=>{});if(scenario.phase==='connect')fail();},
   async query(sql){
    const phase=sql.startsWith('BEGIN')?'begin':sql.includes('backend_control')?'owner':sql==='COMMIT'?'commit':sql==='ROLLBACK'?'rollback':'apply';client.commands.push(phase);
    if(phase===scenario.phase){if(scenario.acceptCommit)committed.push(...pending.splice(0));fail();}
    // A client timeout leaves an active query; a rollback queued behind it
    // cannot finish. Cleanup must discard the socket without awaiting it.
    if(phase==='rollback'){if(scenario.stalled)return new Promise(()=>{});pending.length=0;return {rows:[]};}
    if(phase==='owner')return {rows:[{backend:scenario.fenced?'sqlite':'postgres',generation:'test-v1',schema_version:1}]};
    if(phase==='commit'){committed.push(...pending.splice(0));return {command:'COMMIT',rows:[]};}
    if(phase==='apply')pending.push('write');return {rows:[]};
   },async end(){client.ended=true;handlers.end?.();}
  };clients.push(client);return client;
 }};
 globalThis[key]=driver;
 return {clients,committed,logs,store:new PostgresStore('postgresql://unused','100','test-v1')};
}
const write=tx=>tx.exec('INSERT INTO demand VALUES(?,?)','private-input',1);
async function promptly(promise){let timer;try{return await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('writer blocked behind stalled cleanup')),1000);})]);}finally{clearTimeout(timer);}}
for(const phase of ['connect','begin','owner','apply','commit'])test(`uncoded ${phase} failure releases the writer without retry`,async()=>{
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
 const f=fixture([{phase:'apply',code,stalled:true}]);await promptly(assert.rejects(f.store.transaction(write),/Repository database unavailable/));
 assert.equal(f.clients[0].destroyed,true);assert.ok(!f.clients[0].commands.includes('rollback'));
 assert.equal(f.logs[0].code,/^[0-9A-Z]{5}$/.test(code)?code:null);
});
for(const code of ['23505','57014','55P03'])test(`${code} server error retains normal rollback`,async()=>{
 const f=fixture([{phase:'apply',code,severity:'ERROR'}]);await assert.rejects(f.store.transaction(write),new RegExp(code));
 assert.deepEqual(f.clients[0].commands,['begin','owner','apply','rollback']);assert.equal(f.clients[0].ended,true);assert.equal(f.clients[0].destroyed,false);assert.deepEqual(f.committed,[]);
});
test('application exceptions and fencing retain their meaning and roll back',async()=>{
 const f=fixture();const error=new Error('private application error');await assert.rejects(f.store.transaction(async tx=>{await write(tx);throw error;}),e=>e===error);
 assert.equal(f.clients[0].commands.at(-1),'rollback');assert.deepEqual(f.committed,[]);assert.ok(!JSON.stringify(f.logs).includes('private'));
 const fenced=fixture([{fenced:true}]);await assert.rejects(fenced.store.transaction(write),StoreFenced);assert.equal(fenced.clients[0].commands.at(-1),'rollback');
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

for(const [phase,code,severity] of [['connect','53300','FATAL'],['connect','28P01','FATAL'],['connect','3D000','FATAL'],['apply','25P03','FATAL'],['apply','08006','ERROR']])test(`${phase} ${severity} ${code} cannot queue rollback`,async()=>{const f=fixture([{phase,code,severity,stalled:true}]);await promptly(assert.rejects(f.store.transaction(write),/Repository database unavailable/));assert.equal(f.clients[0].destroyed,true);assert.ok(!f.clients[0].commands.includes('rollback'));});

test('a callback swallowing a timeout cannot hold the local queue with non-database work',async()=>{const f=fixture([{phase:'apply',stalled:true}]);let release;const gate=new Promise(r=>release=r);try{const first=assert.rejects(f.store.transaction(async tx=>{try{await write(tx);}catch{}await gate;await write(tx);}),/Repository database unavailable/);await promptly(Promise.all([first,f.store.transaction(write)]));assert.equal(f.clients.length,2);assert.deepEqual(f.committed,['write']);}finally{release();}});

test('a stalled rollback preserves the application error and closes the connection within its cleanup deadline',async()=>{
 const f=fixture([{stalled:true}]),error=Error('private application error'),start=performance.now();
 await assert.rejects(f.store.transaction(async tx=>{await write(tx);throw error;}),e=>e===error);
 assert.ok(performance.now()-start<3500);assert.equal(f.clients[0].destroyed,true);assert.equal(f.logs[0].reason,'rollback_timeout');
 assert.deepEqual(f.committed,[]);await f.store.transaction(write);assert.deepEqual(f.committed,['write']);
});

test('a missing connect close event cannot hold the writer after its own deadline',async t=>{const f=fixture([{connectPending:true}]);t.mock.timers.enable({apis:['setTimeout']});try{const failed=assert.rejects(f.store.transaction(write),/08001/);await new Promise(setImmediate);t.mock.timers.tick(10000);await failed;assert.equal(f.clients[0].destroyed,true);assert.ok(!f.clients[0].commands.includes('rollback'));assert.equal(f.logs[0].reason,'connect_timeout');await f.store.transaction(write);assert.deepEqual(f.committed,['write']);}finally{t.mock.timers.reset();}});

test('client timeout diagnostics use a fixed reason without logging driver text',async()=>{const f=fixture([{phase:'apply',message:'Query read timeout',stalled:true}]);await promptly(assert.rejects(f.store.transaction(write),/Repository database unavailable/));assert.equal(f.logs[0].reason,'client_timeout');assert.ok(!JSON.stringify(f.logs).includes('Query read timeout'));});
