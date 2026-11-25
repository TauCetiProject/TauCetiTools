import {test} from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
const key='__repositoryReaderFixture';
const bundle=await build({entryPoints:['src/repository_store.ts'],bundle:true,platform:'node',format:'esm',write:false,plugins:[{name:'reader-fixture',setup(b){
 b.onResolve({filter:/^@neondatabase\/serverless$/},()=>({path:'driver',namespace:'reader-fixture'}));
 b.onResolve({filter:/^\.\/storage$/},()=>({path:'logging',namespace:'reader-fixture'}));
 b.onLoad({filter:/.*/,namespace:'reader-fixture'},({path})=>({contents:path==='logging'?`export const log=()=>{};`:`
 export const types={setTypeParser(){}};
 export const neonConfig={};
 export const neon=()=>({query:()=>{throw Error('HTTP must not be used');},transaction:()=>{throw Error('HTTP must not be used');}});
 export class Client {constructor(){return globalThis.${key}.client();}}
 `,loader:'js'}));
}}]});
const {PostgresStore,ReaderPool,StoreUnavailable,READER_MAX_AGE_MS}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
function fixture(behaviour=()=>({rows:[{n:1}]})){
 const clients=[];
 globalThis[key]={client(){
  const handlers={},client={queries:[],ended:false,destroyed:false,on(event,fn){handlers[event]=fn;},emit(event){handlers[event]?.();},
   neonConfig:{destroy(){client.destroyed=true;}},async connect(){},async end(){client.ended=true;},
   async query(sql,bindings){client.queries.push(sql);if(sql.startsWith('SET '))return {rows:[]};return behaviour(sql,bindings,client);}};
  clients.push(client);return client;
 }};
 const store=new PostgresStore('postgresql://unused@ep-fixture.us-east-2.aws.neon.tech/fixture','100','test-v1');store.readers=new ReaderPool('unused');
 return {clients,store};
}
test('reads reuse one read-only session',async()=>{
 const f=fixture();
 assert.deepEqual((await f.store.exec('SELECT 1 AS n')).toArray(),[{n:1}]);
 await f.store.exec('SELECT 1 AS n');
 assert.equal(f.clients.length,1);assert.match(f.clients[0].queries[0],/default_transaction_read_only=on/);assert.match(f.clients[0].queries[0],/statement_timeout/);
 assert.equal(f.store.outbound().read_transport,'websocket');assert.equal(f.store.outbound().readers.reused,1);
});
test('a batch is one statement on a session',async()=>{
 const f=fixture(sql=>sql.startsWith('SELECT jsonb_build_array')?{rows:[{batches:[[{a:1}],[{b:2}]]}]}:{rows:[]});
 const rows=await f.store.batch([{sql:'SELECT 1 AS a'},{sql:'SELECT 2 AS b',bindings:[]}]);
 assert.deepEqual(rows.map(r=>r.toArray()),[[{a:1}],[{b:2}]]);
 assert.equal(f.clients[0].queries.filter(q=>!q.startsWith('SET ')).length,1);
});
test('a transport failure discards the session and the next read reconnects',async()=>{
 let fail=true;const f=fixture(()=>{if(fail){fail=false;throw Object.assign(new Error('private socket text'),{});}return {rows:[{n:2}]};});
 await assert.rejects(f.store.exec('SELECT 1'),error=>error instanceof StoreUnavailable && !String(error.message).includes('private'));
 assert.equal(f.clients[0].destroyed,true);
 assert.deepEqual((await f.store.exec('SELECT 1')).toArray(),[{n:2}]);assert.equal(f.clients.length,2);
});
test('a server error keeps the session',async()=>{
 let fail=true;const f=fixture(()=>{if(fail){fail=false;throw Object.assign(new Error('bad'),{severity:'ERROR',code:'22003'});}return {rows:[]};});
 await assert.rejects(f.store.exec('SELECT 1'),/22003/);await f.store.exec('SELECT 1');
 assert.equal(f.clients.length,1);assert.equal(f.clients[0].destroyed,false);
});
test('an ended or aged session is not reused',async()=>{
 const f=fixture();await f.store.exec('SELECT 1');f.clients[0].emit('end');await f.store.exec('SELECT 1');assert.equal(f.clients.length,2);
 const realNow=Date.now;try{Date.now=()=>realNow()+READER_MAX_AGE_MS+1;await f.store.exec('SELECT 1');}finally{Date.now=realNow;}
 assert.equal(f.clients.length,3);
});
test('standalone writes never use a read session',async()=>{
 const f=fixture();f.store.transaction=async()=>'writer';
 assert.equal(await f.store.batch([{sql:'UPDATE jobs SET due=?',bindings:[1]}]),'writer');assert.equal(f.clients.length,0);
});
