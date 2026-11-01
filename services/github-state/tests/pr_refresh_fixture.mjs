import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {RepositoryState,postgresFixture} from './postgres_runtime.mjs';
export async function fixture(){
 if(process.env.TEST_POSTGRES_URL)return postgresFixture();
 const db=new DatabaseSync(':memory:');let alarm=null;
 const sql={exec(query,...args){let rows=[];if(!args.length&&query.includes(';'))db.exec(query);else{const statement=db.prepare(query);if(statement.columns().length)rows=statement.all(...args);else statement.run(...args);}return {toArray:()=>rows,one:()=>{assert.equal(rows.length,1);return rows[0];}};}};
 const ctx={id:{toString:()=>'100'},waitUntil:()=>{},blockConcurrencyWhile:fn=>fn(),storage:{sql,getAlarm:async()=>alarm,setAlarm:async at=>{alarm=at;},transactionSync(fn){db.exec('SAVEPOINT tx');try{const r=fn();db.exec('RELEASE tx');return r;}catch(e){db.exec('ROLLBACK TO tx');db.exec('RELEASE tx');throw e;}},async transaction(fn){db.exec('SAVEPOINT atx');try{const r=await fn();db.exec('RELEASE atx');return r;}catch(e){db.exec('ROLLBACK TO atx');db.exec('RELEASE atx');throw e;}}}};
 const objects=new Map();
 const env={ALLOWED_REPOSITORIES:JSON.stringify([{id:'100',name:'org/repo',installation:'200'}]),ARCHIVE:{put:async(key,text)=>{objects.set(key,text);},head:async key=>objects.has(key)?{key}:null,get:async key=>objects.has(key)?{text:async()=>objects.get(key),json:async()=>JSON.parse(objects.get(key))}:null}};
 const state=new RepositoryState(ctx,env);await state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
 return {state,env,store:state.store,objects,close:async()=>db.close()};
}

