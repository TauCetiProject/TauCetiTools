// Test-only builds change chunk size/yield policy; no runtime switch is shipped.
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {readFileSync,writeFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {postgresFixture} from '../tests/postgres_runtime.mjs';
import {baseline,implementationFingerprint,fingerprintFiles} from './benchmark-baseline.mjs';
import {drainRepairs} from '../tests/repair_background.mjs';
execFileSync('git',['diff','--exit-code','cf29ced','--','src','package-lock.json','../../tauceti_tools/github_catalog.json','scripts/postgres-schema.mjs']);
const harnessFiles=['scripts/benchmark-publication-sustained.mjs','scripts/benchmark-baseline.mjs','scripts/postgres-schema.mjs','tests/postgres_runtime.mjs','tests/postgres_credentials.mjs','tests/repair_background.mjs'];
const harnessHashes=()=>Object.fromEntries(harnessFiles.map(file=>[file,createHash('sha256').update(readFileSync(file)).digest('hex')]));
const measuredHarness=harnessHashes();
const previous=await baseline('cf29ced'),source=readFileSync('src/repository.ts','utf8');
const marker='if(grouped && committed.length>0 && (base.queuedOtherWork || 0)>0)break;';assert.equal(source.split(marker).length,2);assert.equal(source.split('PUBLICATION_ITEMS=8,').length,2);
const caps=(process.env.BENCHMARK_CAPS||'2,4,8').split(',').map(Number),trials=Number(process.env.BENCHMARK_TRIALS||1),count=Number(process.env.BENCHMARK_HISTORY_COUNT||16),mode=process.env.BENCHMARK_MODE||'steady';
assert.ok(caps.every(n=>[2,4,8].includes(n))&&trials>=1&&count>=1&&count<=100&&['quiet','burst','steady'].includes(mode));
const classes=new Map([['before',previous]]),variantFingerprints={};
for(const cap of caps){
 const variant=source.replace('PUBLICATION_ITEMS=8,','PUBLICATION_ITEMS='+cap+',').replace(marker,'/* bounded fixed chunk: benchmark build only */');
 const result=await build({stdin:{contents:"export {RepositoryState} from './repository';export {PostgresStore} from './repository_store';",loader:'ts',resolveDir:resolve('src')},bundle:true,platform:'node',format:'esm',write:false,plugins:[
  {name:'fixed-chunk',setup(b){b.onLoad({filter:/\/repository\.ts$/},()=>({contents:variant,loader:'ts'}));}},
  {name:'context',setup(b){b.onResolve({filter:/^cloudflare:workers$/},()=>({path:'context',namespace:'fixture'}));b.onLoad({filter:/.*/,namespace:'fixture'},()=>({contents:'export class DurableObject {constructor(ctx,env){this.ctx=ctx;this.env=env}}',loader:'js'}));}}
 ]});
 variantFingerprints[cap]=createHash('sha256').update(variant).digest('hex');classes.set(String(cap),await import('data:text/javascript;base64,'+Buffer.from(result.outputFiles[0].text+'\n//# sourceURL=sustained-variant-'+cap+'.mjs').toString('base64')));
}
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const q=number=>({repository:'org/repo',operation:'issue_comments',args:{number}}),key=n=>'issue_comments:'+JSON.stringify({number:n});
const percentile=(values,p)=>{const a=[...values].sort((a,b)=>a-b);return a.length?a[Math.max(0,Math.ceil(a.length*p)-1)]:null;};
async function measure(label){
 const C=classes.get(label),f=await postgresFixture({RepositoryClass:C.RepositoryState,StoreClass:C.PostgresStore});
 let release,done=false,repairAt,started,cron,produce;const inflight=[];const gate=new Promise(resolve=>release=resolve),writers=[],ingests=[],requests=[],demands=[],maintenance=[],readerTimes=[],cohortKeys=new Set(Array.from({length:4*count},(_,i)=>key(100+i))),complete=new Set(),queued={},yieldPurposes={};
 try{
  const now=Date.now(),sha='a'.repeat(40),warm={number:9000,state:'OPEN',headRefOid:sha,baseRefOid:'b'.repeat(40),mergeable:'MERGEABLE',labels:[],body:'warm fixture'};
  await f.state.put('pr','9000',warm);await f.state.membership('open_prs:{}','9000',true);
  for(const query of [q(9000),{repository:'org/repo',operation:'pr',args:{number:9000}},{repository:'org/repo',operation:'open_prs'},{repository:'org/repo',operation:'statuses',args:{sha}}]){
   const k=query.operation+':'+JSON.stringify(query.args||{});await f.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)',k,query.operation,JSON.stringify(query.args||{}),1,now,0,null);await f.state.enqueue(query,20,now+3600000);
  }
  await f.state.enqueueMany(Array.from({length:4*count},(_,i)=>({q:q(100+i),priority:5})),true);
  for(let group=0;group<4;group++)await f.store.exec('UPDATE jobs SET lane=? WHERE key IN ('+Array(count).fill('?').join(',')+')',['requested','active','verification','archive'][group],...Array.from({length:count},(_,i)=>key(100+group*count+i)));
  const originalTransaction=f.store.transact.bind(f.store),finish=f.state.finishJob;let settled;
  f.state.finishJob=async function(job,...args){const value=await finish.call(this,job,...args);if(cohortKeys.has(job.key))settled?.add(job.key);return value;};
  f.store.transact=async(apply,wait,purpose='other')=>{const began=performance.now(),keys=new Set();let sql=0,failed=true;try{
   const result=await originalTransaction(async tx=>{settled=keys;const run=tx.run.bind(tx);tx.run=(...args)=>{sql++;return run(...args);};try{return await apply(tx);}finally{settled=undefined;}},wait,purpose);failed=false;
   for(const k of keys)complete.add(k);if(complete.size===4*count&&!done){repairAt=performance.now()-started;done=true;clearInterval(cron);}return result;
  }finally{writers.push({purpose,began_ms:began-started,wait_ms:wait,hold_ms:performance.now()-began,round_trips:sql,failed});}};
  // Diagnostic hooks use existing purpose labels and change no queue decisions.
  const lane=f.store.writer,run=lane.run.bind(lane),getter=Object.getOwnPropertyDescriptor(Object.getPrototypeOf(lane),'queuedOtherWork').get;
  lane.run=(work,purpose='other')=>{queued[purpose]=(queued[purpose]||0)+1;return run(()=>{queued[purpose]--;return work();},purpose);};
  Object.defineProperty(lane,'queuedOtherWork',{get(){const n=getter.call(this);if(n)for(const [purpose,count]of Object.entries(queued))if(count&&purpose!=='publication')yieldPurposes[purpose]=(yieldPurposes[purpose]||0)+1;return n;}});
  const execute=f.store.exec.bind(f.store);let standaloneReads=0;f.store.exec=(sql,...args)=>{if(/^\s*SELECT\b/i.test(sql))standaloneReads++;return execute(sql,...args);};
  let groups=0;let ready;const prepared=new Promise(resolve=>ready=resolve);
  f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async(url,init)=>{
   assert.ok(['/batch','/read'].includes(new URL(url).pathname),'unexpected installation path');const input=JSON.parse(init.body);requests.push({path:new URL(url).pathname,operation:input.query?.operation,members:input.jobs?.length||1});
   if(url.endsWith('/batch')){if(++groups===4)ready();await gate;return Response.json({results:input.jobs.map(()=>({api:'graphql',data:[]}))});}
   return Response.json({api:'core',data:input.query?.operation==='pr'?warm:[]});
  }})};
  f.state.startStorageMaintenance=()=>{};
  started=performance.now();const alarm=f.state.runAlarm();await prepared;
  let deliveries=0;
  const emit=async i=>{
   const event=['issue_comment','status','pull_request'][i%3],path='fixture/ingress-'+i;
   const body=event==='issue_comment'?{action:'created',issue:{number:9000},comment:{id:String(90000+i),body:'sustained input',created_at:'2026-10-31T00:00:00Z'}}:event==='status'?{sha,context:'fixture',state:'success',created_at:'2026-10-31T00:00:00Z'}:{action:'edited',pull_request:warm};
   f.objects.set(path,JSON.stringify(body));const began=performance.now();deliveries++;
   const work=f.state.ingest({repository:'org/repo',key:path,delivery:'sustained-'+i,event,received:Date.now()}).then(ok=>{assert.equal(ok,true);ingests.push({event,arrival_ms:began-started,latency_ms:performance.now()-began});});inflight.push(work);work.catch(()=>{});
   if(i%10===0){const d=f.state.noteDemand(q(9000));inflight.push(d);d.catch(()=>{});demands.push(i);const m=f.state.maintenance();inflight.push(m);m.catch(()=>{});maintenance.push(i);}
   const readStart=performance.now();const r=f.state.query(q(9000),true).then(result=>{assert.equal(result.complete,true);readerTimes.push(performance.now()-readStart);});inflight.push(r);r.catch(()=>{});
  };
  produce=(async()=>{
   if(mode==='burst'){for(let i=0;i<8;i++)await emit(i);return;}
   if(mode==='quiet')return;
   for(let i=0;i<Math.max(480,count*16)&&!done;i++){await emit(i);await pause(1000);}
   assert.ok(done,'sustained fixture exceeded its fixed arrival horizon');
  })();produce.catch(()=>{});
  release();cron=setInterval(()=>{const task=f.state.runAlarm();inflight.push(task);task.catch(()=>{});},60000);
  await alarm;await drainRepairs(f.state);assert.equal(complete.size,4*count);done=true;await produce;await Promise.all(inflight);clearInterval(cron);
  const allAt=performance.now()-started,readTrips=standaloneReads;assert.equal((await f.store.exec('SELECT COUNT(*) n FROM deliveries')).one().n,deliveries);assert.ok(!writers.some(w=>w.failed));assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs WHERE lease_id IS NOT NULL')).one().n,0);
  return {mode,label,input_outstanding_at_repair_complete:ingests.filter(x=>x.arrival_ms<=repairAt&&x.arrival_ms+x.latency_ms>repairAt).length,writer_busy_ms_within_repair_window:Object.fromEntries([...new Set(writers.map(w=>w.purpose))].map(p=>[p,writers.filter(w=>w.purpose===p).reduce((n,w)=>n+Math.max(0,Math.min(w.began_ms+w.hold_ms,repairAt)-Math.max(w.began_ms,0)),0)])),histories_complete:complete.size,repair_complete_ms:repairAt,all_committed_ms:allAt,deliveries,deliveries_by_type:Object.fromEntries(['issue_comment','status','pull_request'].map(e=>[e,ingests.filter(v=>v.event===e).length])),input_latency_p50_ms:percentile(ingests.map(x=>x.latency_ms),.5),input_latency_p95_ms:percentile(ingests.map(x=>x.latency_ms),.95),input_latency_p99_ms:percentile(ingests.map(x=>x.latency_ms),.99),reader_p95_ms:percentile(readerTimes,.95),publication_admissions:writers.filter(w=>w.purpose==='publication').length,writer_admissions:writers.length,application_round_trips:writers.reduce((n,w)=>n+w.round_trips,0),standalone_read_http_calls:readTrips,max_writer_hold_ms:Math.max(...writers.map(w=>w.hold_ms)),max_publication_hold_ms:Math.max(...writers.filter(w=>w.purpose==='publication').map(w=>w.hold_ms)),yield_purposes:yieldPurposes,grouped_publication:f.state.groupedPublication,lease_renewal_updates:f.state.renewedLeases,maintenance_calls:maintenance.length,demand_calls:demands.length,reader_samples:readerTimes.length,ingests,writers,upstream_requests:requests,repair_scheduler:f.state.repairCoordinator.snapshot()};
 }finally{done=true;release?.();clearInterval(cron);await Promise.allSettled([produce,...inflight].filter(Boolean));try{await drainRepairs(f.state);}finally{await f.close();}}
}
const output={baseline:previous.commit,implementation_sha256:implementationFingerprint(),implementation_files:fingerprintFiles(),harness_sha256:measuredHarness,variant_sha256:variantFingerprints,trials,group_count:4,histories_per_group:count,mode,scope:'Isolated realPostgreSQL, fixtureupstream; threeeventmixat1delivery/secuntilcohortcommit; actualingestwriterscompete, noteDemandandmaintenanceinvokedperiodicallybutmayonlyread, read-onlyclientprobeeacharrival; real60scronmayfireonlyinslowerruns; storagepreparationdisabled; noGitHubrequests; deliverycounts/horizonsdiffer, notanaggregatecombinedthroughputcomparison; no runtimeconfiguration changes',rows:[]};
for(let trial=0;trial<trials;trial++)for(const label of trial%2?[...caps].reverse().map(String).concat('before'):['before',...caps.map(String)]){
 const row=await measure(label);output.rows.push({...row,trial});writeFileSync(process.env.BENCHMARK_OUTPUT||'/tmp/sustained-publication.json',JSON.stringify(output,null,2)+'\n');console.log(JSON.stringify({trial,label,mode,complete:row.histories_complete,elapsed_ms:row.all_committed_ms,deliveries:row.deliveries,input_p95_ms:row.input_latency_p95_ms,publications:row.publication_admissions}));
}
assert.equal(output.implementation_sha256,implementationFingerprint(),'source changed during experiment');

assert.deepEqual(harnessHashes(),measuredHarness,'harness changed during experiment');
