// Isolated PostgreSQL: background refresh cost, not new missing coverage.
import assert from 'node:assert/strict';
import {writeFileSync} from 'node:fs';
import {postgresFixture,RepositoryState,PostgresStore} from '../tests/postgres_runtime.mjs';
import {baseline,implementationFingerprint,fingerprintFiles} from './benchmark-baseline.mjs';
import {drainRepairs} from '../tests/repair_background.mjs';
const prior=await baseline('cf29ced'),trials=3;
async function measure(RepositoryClass,StoreClass){
 const f=await postgresFixture({RepositoryClass,StoreClass}),writers=[];let calls=0,members=0;
 try{
  const s='"'+f.schema+'"',now=Date.now();
  // 5,000 associations make the bounded100-key retirement query exercise a
  // realistic relation. Exactly98of the100eligible histories are historical.
  await f.admin.query(`INSERT INTO ${s}.resources(kind,id,parent,data,updated,observed,revision,tombstone) SELECT 'pr',i::text,'',json_build_object('number',i,'state','OPEN','headRefOid',CASE WHEN i<=98 THEN repeat('b',40) ELSE lpad(to_hex(i),40,'0')END)::text,'',$1,0,0 FROM generate_series(1,5000)i`,[now]);
  await f.admin.query(`INSERT INTO ${s}.head_prs(sha,number) SELECT lpad(to_hex(i),40,'0'),i::text FROM generate_series(1,5000)i`);
  await f.admin.query(`INSERT INTO ${s}.collections(key,operation,args,complete,verified,revision,etag) SELECT 'statuses:{"sha":"'||lpad(to_hex(i),40,'0')||'"}','statuses',json_build_object('sha',lpad(to_hex(i),40,'0'))::text,1,$1,0,NULL FROM generate_series(1,100)i`,[now]);
  await f.admin.query(`INSERT INTO ${s}.jobs(key,query,priority,due,lane) SELECT 'statuses:{"sha":"'||lpad(to_hex(i),40,'0')||'"}',json_build_object('repository','org/repo','operation','statuses','args',json_build_object('sha',lpad(to_hex(i),40,'0')))::text,20,CASE WHEN i=99 THEN CAST($1 AS BIGINT)-10000 ELSE CAST($1 AS BIGINT)-1000 END,'verification' FROM generate_series(1,100)i`,[now]);
  f.state.expire=async()=>{};f.state.requestOpenCoverage=async()=>{};f.state.maintenance=async()=>{};f.state.startStorageMaintenance=()=>{};
  const transact=f.store.transact.bind(f.store);
  f.store.transact=async(apply,wait,purpose)=>{const start=performance.now();let sql=0,failed=true;try{return await transact(async tx=>{const run=tx.run.bind(tx);tx.run=(...args)=>{sql++;return run(...args);};const value=await apply(tx);failed=false;return value;},wait,purpose);}finally{writers.push({purpose,wait_ms:wait,hold_ms:performance.now()-start,application_round_trips:sql,failed});}};
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async(url,init)=>{assert.ok(url.endsWith('/batch'));const jobs=JSON.parse(init.body).jobs;calls++;members+=jobs.length;return Response.json({results:jobs.map(()=>({api:'graphql',data:[]}))});}})};
  const started=performance.now();await f.state.runAlarm();await drainRepairs(f.state);const elapsed=performance.now()-started;
  assert.equal((await f.store.exec('SELECT COUNT(*)n FROM collections WHERE complete=1')).one().n,100,'retirement retains coverage');
  const jobs=(await f.store.exec('SELECT key,lease_id FROM jobs ORDER BY key')).toArray();assert.ok(jobs.every(job=>job.lease_id===null));
  assert.ok(!writers.some(row=>row.failed));
  return {elapsed_ms:elapsed,eligible_histories:100,historical_histories:98,current_histories:2,upstream_batches:calls,upstream_histories:members,jobs_remaining:jobs.length,new_missing_completions:0,writer_admissions:writers.length,application_round_trips:writers.reduce((n,r)=>n+r.application_round_trips,0),writers,metrics:f.state.repairMetrics.summary()};
 }finally{await f.close();}
}
const output={baseline:prior.commit,implementation_sha256:implementationFingerprint(),implementation_files:fingerprintFiles(),trials,scope:'Isolated realPostgreSQL;5000knownPRheadassociations,100completecleanbackgroundstatuses;98supersededheads,2current; noGitHubrequests; countsrefreshesnotnewhydration',before:[],after:[]};
for(let trial=0;trial<trials;trial++)for(const label of trial%2?['after','before']:['before','after']){
 const row=await measure(label==='before'?prior.RepositoryState:RepositoryState,label==='before'?prior.PostgresStore:PostgresStore);output[label].push(row);
 assert.equal(row.upstream_histories,label==='before'?100:2);assert.equal(row.jobs_remaining,label==='before'?100:2);
 writeFileSync(process.env.BENCHMARK_OUTPUT||'/tmp/status-dormancy-benchmark.json',JSON.stringify(output,null,2)+'\n');console.log(JSON.stringify({trial,label,elapsed_ms:row.elapsed_ms,admissions:row.writer_admissions,upstream_histories:row.upstream_histories}));
}
assert.equal(output.implementation_sha256,implementationFingerprint(),'source changed during measurement');
