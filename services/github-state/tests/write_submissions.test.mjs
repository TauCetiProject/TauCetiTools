import {test} from 'node:test';
import assert from 'node:assert/strict';
import {fixture} from './pr_refresh_fixture.mjs';
const transport='service-binding-v2',url='https://bors.test/receipts';
const receipts=()=>Response.json({schema:'tauceti-bors.candidates/v1',repo:'org/repo',base:'main',timestamp_unit:'unix_microseconds',candidates:[]});
async function configured(){
 const f=await fixture();await f.state.setMeta('repository',{id:'100',name:'org/repo',installation:'200',bors_receipts_url:url});
 let reads=0;
 f.state.env={...f.state.env,BORS_ACTIVITY:{fetch:async()=>{reads++;return receipts();}}};
 const admissions=[],transaction=f.store.transaction.bind(f.store);
 f.store.transaction=(apply,purpose,operation)=>{admissions.push({purpose,operation});return transaction(apply,purpose,operation);};
 return {...f,admissions,reads:()=>reads};
}
async function fresh(f){await f.state.setMeta('activity_landings_transport',transport);await f.state.setMeta('activity_landings_checked',Date.now());f.admissions.length=0;}

test('fresh committed landing metadata avoids writer admissions, including after restart',async()=>{
 const f=await configured();try{
  await fresh(f);
  await Promise.all(Array.from({length:5},()=>f.state.refreshActivityLandings()));
  const restarted=new f.state.constructor(f.state.ctx,f.state.env);restarted.store=f.store;
  await restarted.refreshActivityLandings();
  assert.equal(f.admissions.length,0);assert.equal(f.reads(),0);
 }finally{await f.close();}
});

test('concurrent due polls reserve once and commit their result',async()=>{
 const f=await configured();try{
  f.admissions.length=0;
  await Promise.all(Array.from({length:5},()=>f.state.refreshActivityLandings()));
  assert.equal(f.reads(),1);assert.ok(await f.state.meta('activity_landings_success'));
  assert.equal(await f.state.meta('activity_landings_error'),null);
  assert.equal(f.admissions.filter(x=>x.operation==='activity_landings_publish').length,1);
  assert.ok(f.admissions.every(x=>x.purpose==='other'));
 }finally{await f.close();}
});

test('an older waiting caller recognizes a newer committed poll reservation',async()=>{
 const f=await configured(),clock=Date.now;let release;try{
  let now=clock(),first=true,entered;Date.now=()=>now;
  const ready=new Promise(r=>entered=r),gate=new Promise(r=>release=r),atomic=f.state.atomic;
  f.state.atomic=async function(apply,purpose,operation){if(first && operation==='activity_landings_reserve'){first=false;entered();await gate;}return atomic.call(this,apply,purpose,operation);};
  const older=f.state.refreshActivityLandings();await ready;now+=10;
  await f.state.refreshActivityLandings();release();await older;
  assert.equal(f.reads(),1);
 }finally{release?.();Date.now=clock;await f.close();}
});

test('a reservation committed during the speculative read does not cause a no-op admission',async()=>{
 const f=await configured(),clock=Date.now;try{
  let now=clock();Date.now=()=>now;await fresh(f);
  await f.state.setMeta('activity_landings_checked',now+10);f.admissions.length=0;
  const execute=f.store.exec.bind(f.store);
  f.store.exec=async(sql,...args)=>{const result=await execute(sql,...args);if(sql.startsWith('SELECT key,value FROM meta'))now+=10;return result;};
  await f.state.refreshActivityLandings();assert.equal(f.admissions.length,0);assert.equal(f.reads(),0);
 }finally{Date.now=clock;await f.close();}
});

for(const invalid of [null,'yesterday',0,-1,Date.now()+3600000,{at:Date.now()},'malformed-json'])test(`invalid landing metadata recovers: ${JSON.stringify(invalid)}`,async()=>{
 const f=await configured();try{
  await fresh(f);
  if(invalid===null)await f.store.exec('DELETE FROM meta WHERE key=?','activity_landings_checked');
  else await f.store.exec('UPDATE meta SET value=? WHERE key=?',invalid==='malformed-json'?'broken':JSON.stringify(invalid),'activity_landings_checked');
  await f.state.refreshActivityLandings();
  assert.equal(f.reads(),1);assert.ok(await f.state.meta('activity_landings_success'));
 }finally{await f.close();}
});

test('old transport, a failed speculative read and a failed poll retain ordinary retry',async()=>{
 const f=await configured();try{
  await fresh(f);await f.state.setMeta('activity_landings_transport','old');
  const execute=f.store.exec.bind(f.store);let fail=true;
  f.store.exec=(sql,...args)=>{if(fail && sql.startsWith('SELECT key,value FROM meta')){fail=false;throw Error('speculative read failed');}return execute(sql,...args);};
  f.state.env.BORS_ACTIVITY.fetch=async()=>new Response('',{status:503});
  await f.state.refreshActivityLandings();assert.match(await f.state.meta('activity_landings_error'),/503/);
  await f.state.setMeta('activity_landings_checked',Date.now()-300001);
  f.state.env.BORS_ACTIVITY.fetch=async()=>receipts();
  await f.state.refreshActivityLandings();assert.equal(await f.state.meta('activity_landings_error'),null);assert.ok(await f.state.meta('activity_landings_success'));
 }finally{await f.close();}
});

test('a failed reservation rolls back its freshness metadata and retries without a reset',async()=>{
 const f=await configured();try{
  const set=f.state.setMeta;let fail=true;
  f.state.setMeta=async function(key,value){await set.call(this,key,value);if(fail && key==='activity_landings_token'){fail=false;throw Error('reservation rollback');}};
  await assert.rejects(f.state.refreshActivityLandings(),/reservation rollback/);
  assert.equal(await f.state.meta('activity_landings_checked'),undefined);assert.equal(f.reads(),0);
  await f.state.refreshActivityLandings();assert.equal(f.reads(),1);assert.ok(await f.state.meta('activity_landings_success'));
 }finally{await f.close();}
});
