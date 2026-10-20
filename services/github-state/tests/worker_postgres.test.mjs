import {test} from 'node:test';
import assert from 'node:assert/strict';
import {dropWorkerDatabase} from './worker_postgres.mjs';
const database='contract_0123456789abcdef01234567';
const managed=()=>Object.assign(Error('permission denied to terminate process'),{code:'42501',routine:'TerminateOtherDBBackends'});

test('isolated database cleanup retries managed connections without changing privileges',async()=>{
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
