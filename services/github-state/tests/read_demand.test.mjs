import {test} from 'node:test';
import assert from 'node:assert/strict';
import {runtime,repository,send} from './runtime.mjs';

const extension=`
 demandTransactions=0;
 async atomic(apply){this.demandTransactions++;return super.atomic(apply);}
 async fetch(req){
  if(new URL(req.url).pathname!=='/fixture/demand')return super.fetch(req);
  await this.setMeta('repository',${JSON.stringify(repository)});
  const q={repository:'org/repo',operation:'issue_comments',args:{number:7}};
  // Use the exact key established by enqueue rather than duplicating its format.
  await this.enqueue(q,20,Date.now()+3600000);
  const job=(await this.store.exec('SELECT key FROM jobs')).toArray()[0];
  await this.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)',job.key,q.operation,JSON.stringify(q.args),1,Date.now(),0,null);
  this.demandTransactions=0;
  await this.noteDemand(q);
  const first=(await this.store.exec('SELECT at FROM demand WHERE key=?',job.key)).one().at;
  const initial=this.demandTransactions;
  const before=(await this.store.exec('SELECT total_changes() n')).one().n;
  await this.noteDemand(q);await this.noteDemand(q);
  const repeated={transactions:this.demandTransactions-initial,writes:(await this.store.exec('SELECT total_changes() n')).one().n-before};
  await this.store.exec('UPDATE demand SET at=? WHERE key=?',Date.now()-61000,job.key);
  await this.noteDemand(q);
  const refreshed=(await this.store.exec('SELECT at FROM demand WHERE key=?',job.key)).one().at;
  // Missing coverage must still promote an existing job immediately, even
  // when this query was recently hot while its history was complete.
  await this.store.exec('UPDATE collections SET complete=0 WHERE key=?',job.key);
  await this.noteDemand(q);
  const requested=(await this.store.exec('SELECT lane FROM jobs WHERE key=?',job.key)).one().lane;
  return Response.json({initial,repeated,first,refreshed,requested});
 }
`;

test('repeated complete reads avoid SQLite write transactions; demand refresh and missing-history repair continue',async()=>{
 let upstream=0;
 const mf=await runtime(async()=>{upstream++;throw Error('unexpected GitHub request');},{},
                        {repositoryAlarms:false,repositoryExtension:extension});
 try{
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES');
  const result=await (await send(ns.get(ns.idFromName(repository.id)),'/fixture/demand',{})).json();
  assert.equal(result.initial,1);
  assert.deepEqual(result.repeated,{transactions:0,writes:0});
  assert.ok(result.refreshed>=result.first);
  assert.equal(result.requested,'requested');
  assert.equal(upstream,0);
 }finally{await mf.dispose();}
});
