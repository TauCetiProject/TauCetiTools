import {test} from 'node:test';
import assert from 'node:assert/strict';
import {fixture} from './pr_refresh_fixture.mjs';

const sha='a'.repeat(40);
const pointer=(f,delivery,context)=>{
 const key='webhooks/2026-11-11/'+delivery+'.json';
 f.objects.set(key,JSON.stringify({sha,context,state:'success',created_at:'2026-11-11T03:50:33Z'}));
 return {repository:'org/repo',key,delivery,event:'status',received:Date.now()};
};
const contexts=async f=>(await f.store.exec("SELECT id FROM resources WHERE kind='status_context' ORDER BY id")).toArray().map(r=>r.id.slice(41));
const receipts=async f=>(await f.store.exec('SELECT id FROM deliveries ORDER BY id')).toArray().map(r=>r.id);
// Hold the serialized writer so deliveries accumulate behind it.
function holdWriter(f){let release;const gate=new Promise(r=>release=r);const held=f.state.atomic(async()=>{await gate;});return async()=>{release();await held;};}
function countIngestTransactions(f){
 const atomic=f.state.atomic.bind(f.state),counts={ingest:0};
 f.state.atomic=(apply,purpose,operation)=>{if(purpose==='ingest')counts.ingest++;return atomic(apply,purpose,operation);};
 return counts;
}

test('deliveries waiting for the writer share its transactions',async()=>{
 const f=await fixture();try{
  if(f.store.backend!=='sqlite')return;
  const counts=countIngestTransactions(f),release=holdWriter(f);
  const names=['build','bump-guard','scope','lint','audit','docs'];
  const applied=Promise.all(names.map((context,i)=>f.state.ingest(pointer(f,'delivery-'+i,context))));
  await new Promise(r=>setTimeout(r,20));await release();
  assert.deepEqual(await applied,names.map(()=>true));
  assert.deepEqual(await contexts(f),[...names].sort());
  assert.deepEqual(await receipts(f),names.map((_,i)=>'delivery-'+i));
  assert.ok(counts.ingest<names.length,'grouped '+names.length+' deliveries into '+counts.ingest+' transactions');
  // Redelivery stops at the committed receipt without another transaction.
  const before=counts.ingest;
  assert.equal(await f.state.ingest(pointer(f,'delivery-0','build')),false);
  assert.equal(counts.ingest,before);
 }finally{await f.close();}
});

test('a failing delivery rolls back only its group and fails only itself',async()=>{
 const f=await fixture();try{
  if(f.store.backend!=='sqlite')return;
  const counts=countIngestTransactions(f),put=f.state.put;
  f.state.put=function(kind,id,...rest){if(kind==='status_context' && id.endsWith(':poison'))throw Error('poisoned delivery');return put.call(this,kind,id,...rest);};
  const release=holdWriter(f);
  const names=['build','poison','scope'];
  const applied=Promise.allSettled(names.map((context,i)=>f.state.ingest(pointer(f,'delivery-'+i,context))));
  await new Promise(r=>setTimeout(r,20));await release();
  const results=await applied;
  assert.deepEqual(results.map(r=>r.status),['fulfilled','rejected','fulfilled']);
  assert.equal(results[1].reason.message,'poisoned delivery');
  assert.deepEqual(await contexts(f),['build','scope']);
  assert.deepEqual(await receipts(f),['delivery-0','delivery-2']);
  assert.ok(counts.ingest>=names.length,'individual retries after a failed group');
 }finally{await f.close();}
});

test('a single delivery is applied without waiting for a group to fill',async()=>{
 const f=await fixture();try{
  const counts=countIngestTransactions(f);
  assert.equal(await f.state.ingest(pointer(f,'single','build')),true);
  assert.equal(counts.ingest,1);
  assert.deepEqual(await contexts(f),['build']);
 }finally{await f.close();}
});
