// Stored reads only. Alarms are captured, and every upstream access fails.
import {Worker,isMainThread,parentPort,workerData} from 'node:worker_threads';
import {createHash} from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {DatabaseSync} from 'node:sqlite';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {postgresContractFixture} from '../tests/postgres_runtime.mjs';
async function sqliteFixture(){
const baseline=workerData.baseline;

const source=execFileSync('git',['-C',workerData.baselineRepository,'show',baseline+':services/github-state/src/repository.ts'],{encoding:'utf8'});
const bundled=await build({stdin:{contents:source,loader:'ts',resolveDir:process.cwd()+'/src'},bundle:true,platform:'node',format:'esm',write:false,plugins:[{name:'context',setup(b){b.onResolve({filter:/^cloudflare:workers$/},()=>({path:'context',namespace:'fixture'}));b.onLoad({filter:/.*/,namespace:'fixture'},()=>({contents:'export class DurableObject {constructor(ctx,env){this.ctx=ctx;this.env=env}}',loader:'js'}));}}]});
const {RepositoryState}=await import('data:text/javascript;base64,'+Buffer.from(bundled.outputFiles[0].text).toString('base64'));

 return sqlite(RepositoryState);
}
function sqlite(RepositoryState){
 const db=new DatabaseSync(':memory:'),objects=new Map(),background=[];let alarm=null;
 const storage={sql:{exec(query,...bindings){let rows=[];if(!bindings.length && query.includes(';'))db.exec(query);else{const s=db.prepare(query);if(s.columns().length)rows=s.all(...bindings);else s.run(...bindings);}return {toArray:()=>rows,one:()=>rows[0]};}},transactionSync:fn=>fn(),getAlarm:async()=>alarm,setAlarm:async at=>{alarm=at;}};
 const ctx={storage,blockConcurrencyWhile:fn=>background.push(fn()),waitUntil:work=>background.push(work)};
 const env={ALLOWED_REPOSITORIES:'[{"id":"100","name":"org/repo","installation":"200"}]',ARCHIVE:{put:async(key,value)=>{objects.set(key,value);},get:async key=>objects.has(key)?{json:async()=>JSON.parse(objects.get(key))}:null},INSTALLATIONS:new Proxy({},{get(){throw Error('Benchmark attempted GitHub access');}})};
 const state=new RepositoryState(ctx,env);
 return {state,db,async close(){await Promise.allSettled(background);db.close();}};
}
const count=1200,comments=5000,head='a'.repeat(40),contract='tauceti-survey-facts/v1';
async function seed({state,db}){
 await state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});await state.setMeta('last_snapshot',Date.now());
 const resource=db.prepare('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)'),member=db.prepare('INSERT INTO members VALUES(?,?)'),collection=db.prepare('INSERT INTO collections VALUES(?,?,?,?,?,?,?)');
 await collection.run('open_prs:{}','open_prs','{}',1,Date.now(),0,null);
 for(let number=1;number<=count;number++){
  const data=JSON.stringify({number,title:'PR '+number,state:'OPEN',isDraft:true,headRefOid:head,author:{login:'peer'},labels:[{name:'roadmap/'+(number%20===0?'PDE':'Topology')}]}),id=String(number);
  await resource.run('pr',id,'',data,'',Date.now(),0,0);await member.run('open_prs:{}',id);
  await db.prepare('INSERT INTO survey_inventory VALUES(?,?)').run(id,data);await db.prepare('INSERT INTO survey_hydrated VALUES(?,?)').run(id,contract);
  await db.prepare('INSERT INTO survey_state VALUES(?,?,?)').run(id,'{}',0);
  for(const op of ['issue_comments','review_comments'])await collection.run(op+':'+JSON.stringify({number}),op,JSON.stringify({number}),1,Date.now(),0,null);
 }
 for(let i=1;i<=comments;i++){const id=String(i);await resource.run('issue_comments',id,'7',JSON.stringify({id,body:'stored body '.repeat(20)}),'',Date.now(),0,0);await member.run('issue_comments:{"number":7}',id);}
}
const queries=[{repository:'org/repo',operation:'survey',args:{roadmap:'PDE'}},{repository:'org/repo',operation:'issue_comments',args:{number:7}}];
const fetchQuery=(state,q)=>state.fetch(new Request('https://internal/query',{method:'POST',body:JSON.stringify(q)})).then(r=>r.json());
const percentile=(values,p)=>[...values].sort((a,b)=>a-b)[Math.ceil(values.length*p)-1];
if(isMainThread){
 const baseline=process.env.BASELINE_REF;
 if(!baseline || !process.env.TEST_POSTGRES_URL)throw Error('Set BASELINE_REF and isolated test credentials');
 const workers={},ready={};
 const receive=(worker,type)=>new Promise((resolve,reject)=>{
  const message=value=>{if(value.type===type){worker.off('message',message);worker.off('error',reject);resolve(value);}};
  worker.on('message',message);worker.once('error',reject);
 });
 try{
  for(const backend of ['sqlite','postgres']){
   const worker=new Worker(new URL(import.meta.url),{workerData:{backend,baseline,baselineRepository:process.env.BASELINE_REPOSITORY || process.cwd()}});workers[backend]=worker;ready[backend]=receive(worker,'ready');
  }
  const [sqlite,postgres]=await Promise.all([ready.sqlite,ready.postgres]);assert.deepEqual(sqlite.hashes,postgres.hashes);
  const result={baseline,prs:count,comments,concurrency_per_backend:8,trials:5,samples_per_backend_per_trial:1200,measurement:'separate V8 workers; synchronized workloads on equivalent stored data',github_calls:0,results:{sqlite:{cold_ms:sqlite.cold_ms},postgres:{cold_ms:postgres.cold_ms}}};
  for(let workload=0;workload<3;workload++){
   const timings={sqlite:[],postgres:[]},trials={sqlite:[],postgres:[]};
   for(let trial=0;trial<result.trials;trial++){
    const pending=Object.entries(workers).map(async([backend,worker])=>{const value=receive(worker,'measurement');worker.postMessage({type:'run',workload});return [backend,await value];});
    for(const [backend,value] of await Promise.all(pending)){timings[backend].push(...value.timings);trials[backend].push(value.stats);}
   }
   for(const backend of Object.keys(workers))result.results[backend][['survey','history_first','history_continuation'][workload]]={p50_ms:percentile(timings[backend],.5),p95_ms:percentile(timings[backend],.95),trials:trials[backend]};
  }
  const diagnostics=Object.entries(workers).map(async([backend,worker])=>{const value=receive(worker,'database');worker.postMessage({type:'database'});return [backend,await value];});
  for(const [backend,value] of await Promise.all(diagnostics)){result.results[backend].uncached=value.stats;if(value.sql)result.results[backend].sql_latency=value.sql;}
  result.uncached_reads_succeeded=Object.values(result.results).every(value=>Object.values(value.uncached).every(stats=>stats.errors===0));
  result.warm_p95_ratios=Object.fromEntries(['survey','history_first','history_continuation'].map(workload=>[workload,result.results.postgres[workload].p95_ms/result.results.sqlite[workload].p95_ms]));
  result.measurement+='; warm gate includes the public DTO cache; uncached diagnostic bypasses it';
  result.passed=result.uncached_reads_succeeded && Object.values(result.warm_p95_ratios).every(ratio=>ratio<=1.2);
  console.log(JSON.stringify(result,null,2));if(!result.passed)process.exitCode=1;
 }finally{
  await Promise.all(Object.values(workers).map(async worker=>{if(worker.threadId===-1)return;const closed=receive(worker,'closed');worker.postMessage({type:'close'});let timer;try{await Promise.race([closed,new Promise(resolve=>{timer=setTimeout(resolve,10000);})]);}finally{clearTimeout(timer);await worker.terminate();}}));
 }
}else{
 const f=workerData.backend==='sqlite'?await sqliteFixture():await postgresContractFixture();
 await seed(f);if(f.state.store)await f.state.store.exec('SELECT COUNT(*) n FROM resources');
 f.state.env.INSTALLATIONS=new Proxy({},{get(){throw Error('Benchmark attempted GitHub access');}});
 const sqlTimings=new Map();
 if(f.state.store){const source=f.state.store;f.state.store=new Proxy(source,{get(target,key){const method=Reflect.get(target,key,target);if(key==='exec' || key==='batch')return async(...args)=>{const tag=key==='batch'?'batch':args[0].includes('MAX(r.observed)')?'freshness':args[0].includes('AS bounded_page')?'bounded_scan':args[0].includes('FROM meta')?'metadata':'point_read',start=performance.now();try{return await method.apply(target,args);}finally{const values=sqlTimings.get(tag) || [];values.push(performance.now()-start);sqlTimings.set(tag,values);}};return typeof method==='function'?method.bind(target):method;}});}
 const cold=performance.now(),initial=await Promise.all(queries.map(q=>fetchQuery(f.state,q)));
 if(initial.some(r=>r.status!=='ok'))throw Error('Missing benchmark coverage');
 const workloads=[queries[0],queries[1],{...queries[1],cursor:initial[1].cursor}];
 parentPort.postMessage({type:'ready',cold_ms:performance.now()-cold,hashes:initial.map(value=>createHash('sha256').update(JSON.stringify(value.data)).digest('hex'))});
 parentPort.on('message',async message=>{
  if(message.type==='close'){await f.close();parentPort.postMessage({type:'closed'});parentPort.close();return;}
  if(message.type==='database'){
   const stats={};
   for(let workload=0;workload<3;workload++){
    const q=workloads[workload],read=()=>f.state.stableQuery?f.state.stableQuery(q):f.state.query(q);
    await read();const timings=[];let errors=0;
    for(let offset=0;offset<16;offset+=8)await Promise.all(Array.from({length:8},async()=>{const start=performance.now();try{const value=await read();if(value.status!=='ok')errors++;}catch{errors++;}timings.push(performance.now()-start);}));
    stats[['survey','history_first','history_continuation'][workload]]={p50_ms:percentile(timings,.5),p95_ms:percentile(timings,.95),samples:timings.length,errors};
   }
   parentPort.postMessage({type:'database',stats,sql:Object.fromEntries([...sqlTimings].map(([name,values])=>[name,{samples:values.length,p50_ms:percentile(values,.5),p95_ms:percentile(values,.95),max_ms:Math.max(...values)}]))});return;
  }
  const q=workloads[message.workload];for(let i=0;i<40;i++)await fetchQuery(f.state,q);
  const timings=[];
  for(let offset=0;offset<1200;offset+=8)await Promise.all(Array.from({length:8},async()=>{
   const start=performance.now(),value=await fetchQuery(f.state,q);if(value.status!=='ok')throw Error('Warm read unavailable');timings.push(performance.now()-start);
  }));
  parentPort.postMessage({type:'measurement',timings,stats:{p50_ms:percentile(timings,.5),p95_ms:percentile(timings,.95)}});
 });
}
