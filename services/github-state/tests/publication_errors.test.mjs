import {test} from 'node:test';
import assert from 'node:assert/strict';
import {load} from './prevention_runtime.mjs';
const {SQLiteStore,canReplayPublication}=await load('src/repository_store.ts');
for(const [message,fields,expected] of [
 ['CHECK failed: SQLITE_CONSTRAINT (extended: SQLITE_CONSTRAINT_CHECK)',{},true],
 ['too big: SQLITE_TOOBIG',{},true],
 ['wrong data: SQLITE_MISMATCH',{},true],
 ['disk full: SQLITE_FULL',{},false],
 ['io failure: SQLITE_IOERR',{},false],
 ['SQLITE_CONSTRAINT in ordinary prose',{},false],
 ['user SQLITE_CONSTRAINT: SQLITE_IOERR',{},false],
 ['CHECK failed: SQLITE_CONSTRAINT',{code:'SQLITE_FULL'},false],
 ['CHECK failed: SQLITE_CONSTRAINT',{errcode:13},false]
])test('SQLite publication error code classification: '+message,async()=>{
 const error=Object.assign(Error(message),fields),sql={exec(query){if(query.startsWith('SELECT'))return {toArray:()=>[]};throw error;}},storage={transaction:async fn=>fn()};
 const store=new SQLiteStore(storage,sql);await assert.rejects(store.transaction(tx=>tx.exec('INSERT INTO resources VALUES(?)','fixture')),e=>e===error);
 assert.equal(canReplayPublication(error),expected);
});
