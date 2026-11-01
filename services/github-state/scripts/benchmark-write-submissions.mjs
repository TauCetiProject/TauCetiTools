// Fixed-arrival mixed workloads, isolated PostgreSQL, mocked upstreams only.
import assert from 'node:assert/strict';
import {writeFileSync,readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {baseline,implementationFingerprint} from './benchmark-baseline.mjs';
const implementation_sha256=implementationFingerprint(),source_commit=execFileSync('git',['rev-parse','HEAD'],{encoding:'utf8'}).trim(),source_status=execFileSync('git',['status','--porcelain'],{encoding:'utf8'});
const {postgresFixture,RepositoryState,PostgresStore}=await import('../tests/postgres_runtime.mjs');
assert.equal(implementationFingerprint(),implementation_sha256,'source changed while bundling');
const before=await baseline(process.env.BASELINE_REF || '229fab98865e03632b369b08f745beecb31a1054');
const trials=Number(process.env.BENCHMARK_TRIALS || 3),modes=(process.env.BENCHMARK_MODES || 'quiet,burst,sustained').split(',');
assert.ok(Number.isSafeInteger(trials)&&trials>0&&modes.every(m=>['quiet','burst','sustained'].includes(m)));
const old='a'.repeat(40),head='b'.repeat(40),repo='org/repo',key=n=>'pr:'+JSON.stringify({number:n});
const query=n=>({repository:repo,operation:'pr',args:{number:n}});
const record=(number,sha)=>({number,state:'OPEN',headRefOid:sha,baseRefOid:'d'.repeat(40),mergeable:'MERGEABLE',labels:[],updatedAt:'2026-11-01T09:00:00Z',changedFiles:0,additions:0,deletions:0});
const percentile=(values,p)=>{const a=[...values].sort((a,b)=>a-b);return a.length?a[Math.ceil(a.length*p)-1]:null;};
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function arrivals(mode){
 const trace=[];const spacing=mode==='burst'?0:mode==='quiet'?2200:700;
 for(let i=0;i<3;i++)trace.push({at:i*spacing,kind:'repair',number:7+i},{at:i*spacing,kind:'poll',number:i});
 for(let i=0;i<4;i++)trace.push({at:i*spacing,kind:'ingest',number:100+i});
 for(let i=0;i<2;i++)trace.push({at:i*spacing,kind:'history',number:200+i});
 for(const at of [250,1200,2600,4700])trace.push({at,kind:'reader',number:7});
 return trace.sort((a,b)=>a.at-b.at);
}
async function measure(label,mode){
 const C=label==='before'?before:{RepositoryState,PostgresStore},f=await postgresFixture({RepositoryClass:C.RepositoryState,StoreClass:C.PostgresStore});
 try{
  await f.state.setMeta('repository',{id:'100',name:repo,installation:'200',bors_receipts_url:'https://bors.test/receipts'});
  await f.state.setMeta('activity_landings_transport','service-binding-v2');await f.state.setMeta('activity_landings_checked',Date.now()-300001);await f.state.setMeta('pr_validator_version',1);
  for(let number=7;number<10;number++){await f.state.put('pr',String(number),record(number,old),Date.now()-1000);await f.state.enqueue(query(number));}
  for(let number=200;number<202;number++)await f.state.enqueue({repository:repo,operation:'issue_comments',args:{number}});
  let reads=0,bors=0;const requests=[];
  f.state.env={...f.state.env,ALLOWED_REPOSITORIES:JSON.stringify([{id:'100',name:repo,installation:'200',bors_receipts_url:'https://bors.test/receipts'}]),BORS_ACTIVITY:{fetch:async()=>{bors++;await pause(50);return Response.json({schema:'tauceti-bors.candidates/v1',repo,base:'main',timestamp_unit:'unix_microseconds',candidates:[]});}},INSTALLATIONS:{idFromName:id=>id,get:()=>({fetch:async(url,init)=>{
   assert.equal(new URL(url).pathname,'/read');const input=JSON.parse(init.body);reads++;requests.push({operation:input.query.operation,number:input.query.args.number});
   await pause(50);return Response.json({data:input.query.operation==='pr'?record(input.query.args.number,head):input.query.args.number===201?[{id:'1001',body:'completed history',author:{login:'fixture'},createdAt:'2026-11-01T09:00:00Z',updatedAt:'2026-11-01T09:00:00Z'}]:[],api:'core'});
  }})}};
  const writers=[],transaction=f.store.transact.bind(f.store);let start;
  f.store.transact=async(apply,wait,purpose,operation)=>{const began=performance.now();let trips=0,failed=true;try{
   const result=await transaction(async tx=>{const run=tx.run.bind(tx);tx.run=(...args)=>{trips++;return run(...args);};return apply(tx);},wait,purpose,operation);failed=false;return result;
  }finally{writers.push({purpose,operation:operation||null,at_ms:began-start,wait_ms:wait,hold_ms:performance.now()-began,sql_round_trips:trips,failed});}};
  const outer_queries_start=f.store.queryCount;let http_read_round_trips=0;const outerRun=f.store.run.bind(f.store);f.store.run=(action,connection)=>{if(!connection)http_read_round_trips++;return outerRun(action,connection);};
  const times={repair:[],poll:[],ingest:[],history:[],reader:[]},trace=arrivals(mode);start=performance.now();
  const work=trace.map(async event=>{
   await pause(Math.max(0,event.at-(performance.now()-start)));const began=performance.now();
   if(event.kind==='poll')await f.state.refreshActivityLandings();
   if(event.kind==='repair'){
    const result=await f.state.refreshPR({repository:repo,number:event.number,expected_head:old});assert.equal(result.outcome,'refreshed');
   }
   if(event.kind==='ingest'){
    const id='delivery-'+event.number,body={action:'created',issue:{number:event.number},comment:{id:String(event.number),body:event.number===103?'x'.repeat(80000):'comment',created_at:'2026-11-01T09:00:00Z',updated_at:'2026-11-01T09:00:00Z'}};
    f.objects.set(id,JSON.stringify(body));await f.state.ingest({repository:repo,key:id,delivery:id,event:'issue_comment',received:Date.now()});
   }
   if(event.kind==='history'){
    const k='issue_comments:'+JSON.stringify({number:event.number}),row=(await f.store.exec('SELECT * FROM jobs WHERE key=?',k)).one();const claimed=await f.state.claim(row);assert.ok(claimed);await f.state.refresh(claimed);
   }
   if(event.kind==='reader')await f.state.rows(query(event.number));
   times[event.kind].push({arrival_ms:event.at,dispatch_lag_ms:began-start-event.at,elapsed_ms:performance.now()-began});
  });
  await Promise.all(work);const foreground_ms=performance.now()-start;
  // Account for the same PR obligations in both variants, including the baseline's
  // duplicate background read. New files/status/history obligations are enumerated.
  for(let number=7;number<10;number++){
   const job=(await f.store.exec('SELECT * FROM jobs WHERE key=?',key(number))).one();
   if(job.dirty && job.due<=Date.now()){const claimed=await f.state.claim(job);assert.ok(claimed);await f.state.refresh(claimed);}
   assert.equal((await f.store.exec('SELECT dirty FROM jobs WHERE key=?',key(number))).one().dirty,0);
   assert.equal((await f.state.rows(query(number))).headRefOid,head);
  }
  const total_ms=performance.now()-start,deliveries=(await f.store.exec('SELECT id FROM deliveries ORDER BY id')).toArray();assert.equal(deliveries.length,4);assert.equal(bors,1);assert.ok(writers.every(w=>!w.failed));
  for(let number=200;number<202;number++)assert.ok(await f.state.complete({operation:'issue_comments',args:{number}}));
  assert.equal(f.state.readDemands.flight,undefined,'direct rows must not start detached demand');assert.equal(f.state.readDemands.pending.size,0);assert.ok(writers.every(w=>w.purpose!=='read_demand' && w.operation!=='demand'));
  const pending=(await f.store.exec('SELECT key,dirty,generation,attempt,wait_reason FROM jobs WHERE dirty=1 OR attempt>0 OR wait_reason IS NOT NULL ORDER BY key')).toArray();
  return {trace,total_ms,foreground_ms,times,queue_p50_ms:percentile(writers.map(w=>w.wait_ms),.5),queue_p95_ms:percentile(writers.map(w=>w.wait_ms),.95),writers:writers.map(w=>({...w})),admissions:writers.length,writer_ms:writers.reduce((n,w)=>n+w.hold_ms,0),writer_application_round_trips:writers.reduce((n,w)=>n+w.sql_round_trips,0),http_read_round_trips,setup_commit_sql_calls:4*writers.length,sql_round_trips:writers.reduce((n,w)=>n+w.sql_round_trips,0)+http_read_round_trips+4*writers.length,outer_driver_operations:f.store.queryCount-outer_queries_start,upstream_reads:reads,bors_reads:bors,requests,committed_deliveries:deliveries.length,completed_pr_obligations:3,completed_histories:2,pending};
 }finally{await f.close();}
}
const started_at=new Date().toISOString(),harness_sha256=createHash('sha256').update(readFileSync(new URL(import.meta.url))).digest('hex');
const results={started_at,harness_sha256,node_version:process.version,source_commit,source_status,baseline:before.commit,implementation_sha256,latency_statistic:'median of per-trial maxima (3 repairs, 4 ingests, 4 readers per trial); not a production p95 estimate',sql_count_scope:'writer apply calls + HTTP read calls + 4 sequential BEGIN/session/owner/COMMIT calls per confirmed transaction; connection handshake excluded; outer driver wrappers reported separately; includes harness claim/verification reads, not a standalone product-cost estimate',reader_path:'RepositoryState.rows(pr); HTTP/edge and demand overhead excluded',scope:'isolated PostgreSQL; identical fixed arrival trace per mode; mocked 50ms upstream; no real GitHub or production writes; includes completion of the same PR obligations; pending fanout enumerated; empty and nonempty histories; latency includes dispatch lag from fixed arrivals; one due poll then fresh checks; not fleet throughput',trials,modes:{}};
for(const mode of modes){
 const r=results.modes[mode]={before:[],after:[]};
 for(let i=0;i<trials;i++)for(const label of i%2?['after','before']:['before','after'])r[label].push(await measure(label,mode));
 for(let i=0;i<trials;i++){assert.deepEqual(r.before[i].trace,r.after[i].trace);assert.deepEqual(r.before[i].pending,r.after[i].pending);}
 const median=a=>percentile(a,.5);r.summary={};
 for(const label of ['before','after'])r.summary[label]={total_ms:median(r[label].map(x=>x.total_ms)),foreground_ms:median(r[label].map(x=>x.foreground_ms)),admissions:median(r[label].map(x=>x.admissions)),writer_ms:median(r[label].map(x=>x.writer_ms)),queue_p95_ms:median(r[label].map(x=>x.queue_p95_ms)),writer_application_round_trips:median(r[label].map(x=>x.writer_application_round_trips)),http_read_round_trips:median(r[label].map(x=>x.http_read_round_trips)),sql_round_trips:median(r[label].map(x=>x.sql_round_trips)),upstream_reads:median(r[label].map(x=>x.upstream_reads)),...Object.fromEntries(['ingest','repair','reader'].map(kind=>[kind+'_max_ms',median(r[label].map(x=>Math.max(...x.times[kind].map(t=>t.elapsed_ms+t.dispatch_lag_ms))))]))};
 const a=r.summary.before,b=r.summary.after;
 r.acceptance={completion:b.total_ms<=a.total_ms*.95,input:b.ingest_max_ms<=a.ingest_max_ms*1.1+250,repair:b.repair_max_ms<=a.repair_max_ms*1.1+250,reader:b.reader_max_ms<=a.reader_max_ms*1.1+100};
 console.log(JSON.stringify({mode,summary:r.summary,acceptance:r.acceptance}));
 if(Object.values(r.acceptance).some(passed=>!passed))process.exitCode=1;
 assert.equal(implementationFingerprint(),results.implementation_sha256,'source must remain fixed during comparisons');
 results.updated_at=new Date().toISOString();
 writeFileSync(process.env.BENCHMARK_OUTPUT || '/tmp/write-submissions-benchmark.json',JSON.stringify(results,null,2)+'\n');
}
