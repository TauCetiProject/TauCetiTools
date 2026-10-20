import {test} from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {dropWorkerDatabase,closeWorkerDatabase} from './worker_postgres.mjs';
const database='contract_0123456789abcdef01234567';
const managed=()=>Object.assign(Error('permission denied to terminate process'),{code:'42501',routine:'TerminateOtherDBBackends'});

test('isolated database cleanup retries backend conflicts in the same fixture database',async()=>{
 const queries=[],delays=[],errors=[managed(),Object.assign(Error('database is busy'),{code:'55006'})];
 await dropWorkerDatabase({async query(sql){queries.push(sql);if(errors.length)throw errors.shift();}},database,async ms=>{delays.push(ms);});
 assert.equal(queries.length,3);assert.deepEqual(delays,[250,500]);
 assert.ok(queries.every(sql=>sql==='DROP DATABASE IF EXISTS "'+database+'" WITH (FORCE)'));
});

test('isolated database cleanup does not retry unrelated permission failures',async()=>{
 const error=Object.assign(Error('not the database owner'),{code:'42501',routine:'dropdb'});let queries=0;
 await assert.rejects(dropWorkerDatabase({async query(){queries++;throw error;}},database,()=>assert.fail('unexpected retry')),value=>value===error);
 assert.equal(queries,1);
});

test('isolated database cleanup reports persistent failure after a bounded retry budget',async()=>{
 const error=managed(),delays=[];let queries=0;
 await assert.rejects(dropWorkerDatabase({async query(){queries++;throw error;}},database,async ms=>{delays.push(ms);}),value=>value===error);
 assert.equal(queries,7);assert.equal(delays.length,6);assert.ok(delays.reduce((n,ms)=>n+ms,0)<20000);
});

test('isolated database cleanup refuses names outside its generated fixture namespace',async()=>{
 for(const name of ['github_state','github_state_test','contract_other','contract_0123456789abcdef01234567;DROP DATABASE github_state']){
  await assert.rejects(dropWorkerDatabase({query(){assert.fail('unexpected database operation');}},name),/Invalid isolated worker database/);
 }
});

test('isolated database cleanup ends control after a drop failure',async()=>{
 const error=Object.assign(Error('not the database owner'),{code:'42501',routine:'dropdb'});let ended=0;
 await assert.rejects(closeWorkerDatabase({async query(){throw error;},async end(){ended++;}},database),value=>value===error);
 assert.equal(ended,1);
});

test('isolated database cleanup retains both drop and control-close failures',async()=>{
 const drop=Error('drop failed'),end=Error('close failed');
 await assert.rejects(closeWorkerDatabase({async query(){throw drop;},async end(){throw end;}},database),value=>value instanceof AggregateError && value.errors[0]===drop && value.errors[1]===end);
});

test('isolated database cleanup closes its owned sockets before dropping',async()=>{
 const events=[];
 class Socket extends EventEmitter {
  readyState=1;
  terminate(){events.push('terminate');queueMicrotask(()=>{this.readyState=3;events.push('socket closed');this.emit('close');});}
 }
 const socket=new Socket();
 await closeWorkerDatabase({async query(){assert.equal(socket.readyState,3);events.push('drop');},async end(){events.push('control ended');}},database,new Set([socket]));
 assert.deepEqual(events,['terminate','socket closed','drop','control ended']);
});

test('isolated database cleanup waits for other sockets after a termination failure',async()=>{
 const error=Error('socket termination failed');let closed=false;
 class Socket extends EventEmitter {
  readyState=1;
  constructor(fails){super();this.fails=fails;}
  terminate(){if(this.fails)throw error;setImmediate(()=>{this.readyState=3;closed=true;this.emit('close');});}
 }
 await assert.rejects(closeWorkerDatabase({async query(){assert.equal(closed,true);},async end(){}},database,new Set([new Socket(true),new Socket(false)])),value=>value===error);
});
