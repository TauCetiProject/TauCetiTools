import {postgresContractFixture} from './postgres_runtime.mjs';
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {spawnSync} from 'node:child_process';
import {build} from 'esbuild';

const bundle=await build({entryPoints:['src/repository.ts'],bundle:true,platform:'node',format:'esm',write:false,plugins:[{
 name:'durable-object-context',setup(builder){
  builder.onResolve({filter:/^cloudflare:workers$/},()=>({path:'context',namespace:'fixture'}));
  builder.onLoad({filter:/.*/,namespace:'fixture'},()=>({contents:'export class DurableObject {constructor(ctx,env){this.ctx=ctx;this.env=env}}',loader:'js'}));
 }
}]});
const {RepositoryState}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
async function fixture(){
 if(process.env.TEST_POSTGRES_URL)return postgresContractFixture({initialized:true});
 const db=new DatabaseSync(':memory:');let alarm=null;
 const objects=new Map();
 const env={ALLOWED_REPOSITORIES:JSON.stringify([{id:'100',name:'org/repo',installation:'200'}]),ARCHIVE:{
  async head(key){return objects.has(key)?{key}:null;},async put(key,text){objects.set(key,text);},async get(key){const text=objects.get(key);return text===undefined?null:{text:async()=>text,json:async()=>JSON.parse(text)};}
 }};
 const ctx={blockConcurrencyWhile:fn=>fn(),storage:{sql:{exec(sql,...bindings){
  let rows=[];
  if(!bindings.length && sql.includes(';')) db.exec(sql);
  else {const statement=db.prepare(sql);if(statement.columns().length)rows=statement.all(...bindings);else statement.run(...bindings);}
  return {toArray:()=>rows,one:()=>{assert.equal(rows.length,1);return rows[0];}};
 }},async transaction(fn){db.exec('SAVEPOINT async_tx');try{const result=await fn();db.exec('RELEASE async_tx');return result;}catch(error){db.exec('ROLLBACK TO async_tx');db.exec('RELEASE async_tx');throw error;}},transactionSync(fn){db.exec('SAVEPOINT tx');try{const value=fn();db.exec('RELEASE tx');return value;}catch(error){db.exec('ROLLBACK TO tx');db.exec('RELEASE tx');throw error;}},getAlarm:async()=>alarm,setAlarm:async value=>{alarm=value;}}};
 const state=new RepositoryState(ctx,env);
 const insert=async (operation,number,priority,due,attempt=0)=>{
  const q={operation,args:{number}},key=operation+':'+JSON.stringify(q.args);
  db.prepare('INSERT INTO jobs(key,query,priority,due,attempt,lane) VALUES(?,?,?,?,?,?)').run(key,JSON.stringify(q),priority,due,attempt,(await state.lane(q,false,priority)));
  return q;
 };
 return {state,db,insert,env,objects,ctx};
}

test('compact roadmap snapshot updates on comment edit/delete without reading full histories',async()=>{
 const {state,db,env}=(await fixture());try {
  const repo='org/repo';await state.setMeta('repository',{id:'100',name:repo,installation:'200'});
  const complete=async (operation,args={})=>{const key=operation+':'+JSON.stringify(args);await db.prepare('INSERT OR REPLACE INTO collections VALUES(?,?,?,?,?,?,?)').run(key,operation,JSON.stringify(args),1,Date.now(),(await state.revision()),null);};
  await complete('open_prs');
  for(const [number,area] of [[7,'PDE'],[8,'Topology']]) {
   await state.put('pr',String(number),{number,state:'OPEN',headRefOid:'a'.repeat(40),labels:[{name:'roadmap/'+area}],body:'private description '.repeat(2000)});
   await state.membership('open_prs:{}',String(number),true);await complete('issue_comments',{number});await complete('review_comments',{number});
  }
  const board={id:'100',body:'<!--tauceti-scoreboard-->\n<!--tauceti-meta:v1 '+JSON.stringify({head_sha:'a'.repeat(40),runs:[{provider:'claude',verdict:'approve',tok:{in:200}}]})+'-->',updated_at:'2026-10-13T00:00:00Z'};
  await state.put('issue_comments','100',board,Date.now(),false,'7');await state.membership('issue_comments:{"number":7}','100',true);
  await state.put('review_comments','101',{id:'101',body:'<!--tauceti-rubric:reuse -->'},Date.now(),false,'7');await state.membership('review_comments:{"number":7}','101',true);
  await state.put('review_comments','102',{id:'102',in_reply_to_id:'101',body:'contest',created_at:'now'},Date.now(),false,'7');await state.membership('review_comments:{"number":7}','102',true);
  await state.hydrateSurvey('7');
  const q={repository:repo,operation:'survey',args:{roadmap:'PDE',skip:'',numbers:''}};
  let result=await state.query(q);assert.equal(result.data.length,1);assert.equal(result.data[0].pr.number,7);
  assert.equal(result.data[0].review.meta.head_sha,'a'.repeat(40));assert.equal(result.data[0].review.contest.id,'102');
  assert.equal(result.data[0].review.issue_complete,true);assert.equal(JSON.stringify(result).includes('private description'),false);
  assert.deepEqual(result.data[0].review.markers,[],'unchanged v1 clients keep their required empty compatibility array');
  const legacy=payload=>spawnSync('python3',['../../tests/fixtures/legacy_survey_client.py'],{input:JSON.stringify(payload),encoding:'utf8'});
  const accepted=legacy(result);assert.equal(accepted.status,0,accepted.stderr);assert.equal(JSON.parse(accepted.stdout).accepted,true);
  const incompatible=structuredClone(result);delete incompatible.data[0].review.markers;
  assert.notEqual(legacy(incompatible).status,0,'the frozen client must detect the original missing-field regression');
  const cached=JSON.parse((await db.prepare('SELECT data FROM survey_state WHERE number=?').get('7')).data);
  await db.prepare('UPDATE survey_state SET data=? WHERE number=?').run(JSON.stringify({...cached,markers:[{head:'old',expires_at:9999999999,providers:['obsolete']}]}),'7');
  assert.deepEqual((await state.surveyRows(q)).rows[0].review.markers,[],'cached claims cannot reappear through the compatibility field');
  const cursor='0b6c2f0e-1f7a-4b4c-9a1e-5b8d3c2a1f00',pageKey='cursors/100/'+cursor+'/100.json';
  const queryKey='survey:'+JSON.stringify(Object.fromEntries(Object.entries(q.args).sort(([a],[b])=>a.localeCompare(b))));
  const continueHttp=async cursor=>{
   const response=await state.fetch(new Request('https://internal/query',{method:'POST',body:JSON.stringify({...q,cursor})}));
   assert.equal(response.status,200);return response.json();
  };
  for(const markers of [undefined,[{head:'old',expires_at:9999999999,providers:['obsolete']}]]) {
   const page=structuredClone(result);page.data[0].review.markers=markers;
   const stored=JSON.stringify({repository:'100',key:queryKey,expires:Date.now()+600000,result:page});await env.ARCHIVE.put(pageKey,stored);
   const continued=await continueHttp(cursor+':100');
   assert.deepEqual(continued.data[0].review.markers,[]);assert.equal(legacy(continued).status,0);
   assert.equal(continued.revision,result.revision);assert.equal(continued.observed_at,result.observed_at);assert.equal(continued.verified_at,result.verified_at);
   assert.equal(await(await env.ARCHIVE.get(pageKey)).text(),stored,'compatible reads do not rewrite the retained page');
  }
  if(state.store.backend==='postgres') {
   const row=structuredClone(result.data[0]);delete row.review.markers;
   const header={...result};delete header.data;
   await db.prepare('INSERT INTO cursors VALUES(?,?,?)').run('legacy-survey',Date.now()+600000,JSON.stringify({key:queryKey,rows:[row,row],result:header}));
   const continued=await continueHttp('legacy-survey:1');
   assert.equal(legacy(continued).status,0);assert.deepEqual(continued.data[0].review.markers,[]);assert.equal(continued.revision,result.revision);
  }
  assert.equal(JSON.stringify(result).includes('"tok"'),false);
  assert.equal((await db.prepare("SELECT COUNT(*) n FROM demand WHERE key LIKE '%comments:%'").get()).n,0,'complete projections must not promote every history to frequent verification');
  assert.equal(result.author_counts[''],2,'automatic caps count all known roadmap PRs');
  const explicit=await state.query({...q,args:{roadmap:'PDE',skip:'Topology',numbers:'8'}});assert.equal(explicit.data[0].pr.number,8,'explicit PR overrides roadmap and skips');
  assert.equal(legacy(explicit).status,0,'an unhydrated survey row remains accepted by the unchanged client');
  const prior=result.revision;
  await state.put('issue_comments','100',{...board,updated_at:'2026-10-13T01:00:00Z',body:'edited; scoreboard removed'},Date.now(),false,'7');
  await state.put('review_comments','101',{id:'101',body:'<!--tauceti-rubric:reuse -->'},Date.now(),true,'7');
  result=await state.query(q);assert.ok(result.revision>prior);assert.equal(result.data[0].review.meta,null);assert.equal(result.data[0].review.contest,null);
  const rows=state.rows.bind(state);state.rows=async(query)=>{assert.ok(!['issue_comments','review_comments'].includes(query.operation),'full histories were read by a warm query');return rows(query);};
  await state.query(q);
 }finally{await db.close();}
});

test('large inventory filters before statuses and serves compact facts in one page',async()=>{
 const {state,db}=(await fixture());try {
  await state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
  await db.prepare('INSERT INTO collections VALUES(?,?,?,?,?,?,?)').run('open_prs:{}','open_prs','{}',1,Date.now(),0,null);
  const resource=db.prepare('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)'),inventory=db.prepare('INSERT INTO survey_inventory VALUES(?,?)'),member=db.prepare('INSERT INTO members VALUES(?,?)');
  const hydrated=db.prepare('INSERT INTO survey_hydrated VALUES(?,?)');
  for(let i=1;i<=5000;i++) {
   const row={number:i,state:'OPEN',headRefOid:'a'.repeat(40),labels:[{name:'roadmap/'+(i<=20?'PDE':'Topology')}],target_focuses:[]},data=JSON.stringify(row);
   await resource.run('pr',String(i),'',data,'',Date.now(),0,0);await inventory.run(String(i),data);await member.run('open_prs:{}',String(i));await hydrated.run(String(i),'tauceti-survey-facts/v1:completed-scoreboards');
  }
  let statusReads=0;const rows=state.rows.bind(state);state.rows=async q=>{if(q.operation==='statuses')statusReads++;return rows(q);};
  const result=await state.query({repository:'org/repo',operation:'survey',args:{roadmap:'PDE'}});
  assert.equal(result.data.length,20);assert.equal(statusReads,20);assert.equal(result.cursor,undefined);assert.ok(JSON.stringify(result).length<20000);
 }finally{await db.close();}
});

test('migration is outside requests and one missing resource cannot block other PRs or maintenance',async()=>{
 const {state,db}=(await fixture());try {
  await state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});await state.setMeta('last_snapshot',Date.now());
  await db.prepare('INSERT INTO collections VALUES(?,?,?,?,?,?,?)').run('open_prs:{}','open_prs','{}',1,Date.now(),0,null);
  for(const number of [7,8]) {await state.put('pr',String(number),{number,labels:[{name:'roadmap/PDE'}]});await state.membership('open_prs:{}',String(number),true);}
  await state.put('issue_comments','100',{id:'100'},Date.now(),false,'7');
  await db.prepare("UPDATE resources SET data=? WHERE kind='issue_comments' AND id='100'").run(JSON.stringify({$r2:'missing'}));
  const q={repository:'org/repo',operation:'survey',args:{roadmap:'PDE'}};
  const result=await state.query(q);assert.equal(result.data.length,2);assert.ok(result.data.every(r=>!r.review.issue_complete));
  assert.equal((await db.prepare('SELECT COUNT(*) n FROM survey_hydrated').get()).n,0,'query must not scan existing histories to hydrate');
  await state.maintenance();
  assert.deepEqual((await db.prepare('SELECT number FROM survey_hydrated').all()).map(r=>r.number),['8']);
  assert.equal((await db.prepare('SELECT COUNT(*) n FROM survey_hydration_failures').get()).n,1);
  await state.maintenance();assert.equal((await state.query(q)).status,'ok');
 }finally{await db.close();}
});

test('comment bursts coalesce projection rebuilds and malformed reply IDs do not block ingestion',async()=>{
 const {state,db}=(await fixture());try {
  await state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
  await state.put('pr','7',{number:7,labels:[{name:'roadmap/PDE'}]});await state.membership('open_prs:{}','7',true);
  await db.prepare('INSERT INTO collections VALUES(?,?,?,?,?,?,?)').run('open_prs:{}','open_prs','{}',1,Date.now(),0,null);
  await state.put('issue_comments','50',{id:'50',body:'plain comment'},Date.now(),false,'7');await state.membership('issue_comments:{"number":7}','50',true);
  await state.hydrateSurvey('7');let rebuilds=0;const rebuild=state.rebuildSurvey.bind(state);state.rebuildSurvey=function(n){rebuilds++;return rebuild(n);};
  await state.put('review_comments','100',{id:'100',body:'<!--tauceti-rubric:reuse -->'},Date.now(),false,'7');await state.membership('review_comments:{"number":7}','100',true);
  for(let i=101;i<121;i++) {await state.put('review_comments',String(i),{id:String(i),in_reply_to_id:'100',body:'contest'},Date.now(),false,'7');await state.membership('review_comments:{"number":7}',String(i),true);}
  await state.put('review_comments','null',{id:null,in_reply_to_id:'100',body:'contest'},Date.now(),false,'7');await state.membership('review_comments:{"number":7}','null',true);
  assert.equal(rebuilds,0);const q={repository:'org/repo',operation:'survey',args:{roadmap:'PDE'}};
  const result=await state.query(q);assert.equal(rebuilds,1);assert.equal(result.data[0].review.contest.id,'120');
  await state.membership('review_comments:{"number":7}','100',false);assert.equal((await state.query(q)).data[0].review.contest,null);
 }finally{await db.close();}
});

test('partial PR payloads retain mergeability only for the same head and base',async()=>{
 const {state,db}=(await fixture());try {
  await state.put('pr','7',{number:7,headRefOid:'a',baseRefOid:'b',mergeable:'CONFLICTING',updatedAt:'2026-10-12T00:00:00Z'});
  await state.put('pr','7',{number:7,headRefOid:'a',baseRefOid:'b',title:'list row',updatedAt:'2026-10-12T00:00:00Z'});
  assert.equal(JSON.parse((await db.prepare("SELECT data FROM resources WHERE kind='pr' AND id='7'").get()).data).mergeable,'CONFLICTING');
  await state.put('pr','7',{number:7,headRefOid:'new',baseRefOid:'b',updatedAt:'2026-10-12T00:01:00Z'});
  assert.equal(JSON.parse((await db.prepare("SELECT data FROM resources WHERE kind='pr' AND id='7'").get()).data).mergeable,'UNKNOWN');
 }finally{await db.close();}
});

test('REST offset inventory gaps retain previously observed open PRs',async()=>{
 const {state,db}=(await fixture());try {
  await state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
  const q={repository:'org/repo',operation:'open_prs'},key='open_prs:{}';
  for(const number of [7,8]) {await state.put('pr',String(number),{number,state:'OPEN'});await state.membership(key,String(number),true);}
  await state.enqueue(q);const job=(await state.claim((await state.nextJob())));
  await state.refresh(job,{data:[{number:8,state:'OPEN'}],api:'core'});
  assert.deepEqual((await state.rows(q)).map(r=>r.number),[7,8]);
 }finally{await db.close();}
});

test('a newer close cannot be re-added by an older inventory response',async()=>{
 const {state,db}=(await fixture());try {
  await state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
  const q={repository:'org/repo',operation:'open_issues'},key='open_issues:{}';
  await state.enqueue(q);const job=(await state.claim((await state.nextJob())));job.started=Date.now()-10000;
  await state.put('issue','7',{number:7,state:'CLOSED'},Date.now(),false,'',true);
  await state.refresh(job,{data:[{number:7,state:'OPEN'}],api:'graphql'});
  assert.deepEqual(await state.rows(q),[]);
  assert.equal((await db.prepare('SELECT COUNT(*) n FROM members WHERE collection=? AND id=?').get(key,'7')).n,0);
 }finally{await db.close();}
});

test('expired staging restarts from page one instead of retrying a missing page forever',async()=>{
 const {state,db,env}=(await fixture());try {
  await state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
  env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>Response.json({data:[],api:'core'})})};
  const q={repository:'org/repo',operation:'files',args:{number:7}};await state.enqueue(q);
  const key='files:{"number":7}';await db.prepare('UPDATE jobs SET started=?,page=2,cursor=? WHERE key=?').run(Date.now()-86400001,'old',key);
  await state.dispatch((await state.claim((await state.nextJob()))));
  const row=(await db.prepare('SELECT * FROM jobs WHERE key=?').get(key));
  assert.equal(row.started,null);assert.equal(row.page,1);assert.equal(row.cursor,null);assert.equal(row.wait_reason,'staging_expired');
 }finally{await db.close();}
});

test('one bad batched history does not turn a completed sibling into a retry',async()=>{
 const {state,db,env}=(await fixture());try {
  await state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
  env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>Response.json({results:[{data:[],api:'graphql'},{data:[],api:'graphql'}]})})};
  for(const number of [7,8]) await state.enqueue({repository:'org/repo',operation:'issue_comments',args:{number}});
  const original=state.refresh.bind(state);state.refresh=async(job,result)=>{if(JSON.parse(job.query).args.number===8)throw new Error('staging page missing');return original(job,result);};
  await state.dispatch((await state.claim((await state.nextJob()))));
  const done=(await db.prepare('SELECT * FROM jobs WHERE key=?').get('issue_comments:{"number":7}'));
  assert.equal(done.attempt,0);assert.equal(done.wait_reason,null);assert.ok(done.due>Date.now()+10000);
  assert.equal((await db.prepare('SELECT wait_reason FROM jobs WHERE key=?').get('issue_comments:{"number":8}')).wait_reason,'staging_expired');
 }finally{await db.close();}
});

test('persistent parser errors keep their pagination and back off without rapid full scans',async()=>{
 const {state,db}=(await fixture());try {
  const q={repository:'org/repo',operation:'files',args:{number:7}};await state.enqueue(q);
  const key='files:{"number":7}';await db.prepare('UPDATE jobs SET started=?,page=2,cursor=? WHERE key=?').run(Date.now()-1000,'cursor',key);
  let job=(await state.claim((await state.nextJob())));const firstStart=Date.now();await state.retryJob(job,new Error('repeated upstream cursor'));
  let row=(await db.prepare('SELECT * FROM jobs WHERE key=?').get(key));
  assert.equal(row.page,2);assert.equal(row.cursor,'cursor');assert.ok(row.started);assert.ok(row.due>=firstStart+60000);
  await db.prepare('UPDATE jobs SET due=0,lease_until=0 WHERE key=?').run(key);
  job=(await state.claim((await state.nextJob())));const secondStart=Date.now();await state.retryJob(job,new Error('repeated upstream cursor'));
  row=(await db.prepare('SELECT * FROM jobs WHERE key=?').get(key));
  assert.ok(row.due>=secondStart+120000);assert.equal(row.attempt,2);
 }finally{await db.close();}
});

test('active R2 references are retained',async()=>{
 const {state,db}=await fixture();try {
  await state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
  const insert=db.prepare('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)');
  const key='resources/100/retained';await insert.run('pr','7','',JSON.stringify({$r2:key}),'',Date.now(),1,0);
  assert.equal((await (await state.fetch(new Request('https://internal/retained-object?key='+encodeURIComponent(key)))).json()).retained,true);
  assert.equal((await (await state.fetch(new Request('https://internal/retained-object?key=unused'))).json()).retained,true,'unreferenced objects retain an archive grace period');
  await db.prepare('UPDATE retired_objects SET at=? WHERE key=?').run(Date.now()-9*86400000,'unused');
  assert.equal((await (await state.fetch(new Request('https://internal/retained-object?key=unused'))).json()).retained,true,'pinned recovery archives outlive the snapshot grace period');
 }finally{await db.close();}
});

test('large old repair backlog cannot starve fresh deltas and still makes progress',async ()=>{
 const {state,db,insert}=(await fixture());
 try{
  const now=Date.now();
  for(let i=0;i<5000;i++)await insert('files',i,20,now-2*3600000+i);
  await insert('pr_delta',1,0,now-1000);
  const choices=[];for(let i=0;i<15;i++)choices.push(await state.nextJob());
  assert.equal(choices.filter(j=>j.key.startsWith('pr_delta:')).length,13);
  assert.equal(choices.filter(j=>j.key.startsWith('files:')).length,2);
 }finally{await db.close();}
});

test('demand advances quiet reconciliation without revoking actual quota deferrals',async()=>{
 const {state,db,insert}=(await fixture());
 try{
  const now=Date.now(),hot=await insert('reactions',1,20,now+86400000),blocked=await insert('reactions',2,20,now+3600000,1);
  await state.noteDemand(hot);await state.noteDemand(blocked);
  const rows=(await db.prepare('SELECT priority,due,attempt FROM jobs ORDER BY key').all());
  assert.equal(rows[0].priority,5);
  assert.ok(rows[0].due<=Date.now(),'missing coverage is promoted immediately');
  assert.equal(rows[1].due,now+3600000);
  assert.equal((await state.priority({operation:'pr_delta'})),0);
  assert.equal((await state.priority(hot)),5);
  assert.equal((await state.priority({operation:'review_threads',args:{number:99}})),20);
 }finally{await db.close();}
});
test('closed PR history and CI polling retire, then client demand reactivates them',async()=>{
 const {state,db}=(await fixture());
 try{
  const sha='a'.repeat(40);
  await state.put('pr','7',{number:7,state:'MERGED',headRefOid:sha,updatedAt:new Date().toISOString()});
  await state.put('review_comments','12',{id:'12',node_id:'node'},Date.now(),false,'7');
  for(const q of [{operation:'review_threads',args:{number:7}},{operation:'statuses',args:{sha}},{operation:'reactions',args:{id:'12'}}])assert.equal((await state.dormant(q)),true);
  const q={repository:'org/repo',operation:'review_threads',args:{number:7}},key='review_threads:{"number":7}';
  await db.prepare('INSERT INTO collections VALUES(?,?,?,?,?,?,?)').run(key,q.operation,JSON.stringify(q.args),1,Date.now(),1,null);
  await state.noteDemand(q);
  assert.equal((await state.dormant(q)),false);
  assert.equal((await db.prepare('SELECT COUNT(*) n FROM jobs WHERE key=?').get(key)).n,1);
  assert.equal((await state.dormant({operation:'statuses',args:{sha:'b'.repeat(40)}})),false,'unknown heads are conservative');
 }finally{await db.close();}
});
test('a webhook arriving during a staged read keeps its repair pending',async()=>{
 const {state,db}=(await fixture());
 try{
  const q={operation:'review_threads',args:{number:7}},key='review_threads:{"number":7}';
  await state.put('pr','7',{number:7,state:'MERGED'});
  await state.enqueue(q,0,Date.now(),true);
  const job=(await db.prepare('SELECT * FROM jobs WHERE key=?').get(key));
  job.start_generation=job.generation;
  await state.enqueue(q,0,Date.now(),true);
  await state.finishJob(job,q);
  const next=(await db.prepare('SELECT * FROM jobs WHERE key=?').get(key));
  assert.equal(next.dirty,1);assert.ok(next.due<=Date.now());
  await state.finishJob({...next,start_generation:next.generation},q);
  assert.equal((await db.prepare('SELECT COUNT(*) n FROM jobs WHERE key=?').get(key)).n,0,'quiet closed history retires after the latest repair');
 }finally{await db.close();}
});

test('all four lanes get their configured share and leased work is excluded',async ()=>{
 const {state,db,insert}=(await fixture());
 try {
  for(const [i,lane] of ['requested','active','verification','archive'].entries()) {
   const q=await insert('files',i+1,20,Date.now()-10000);
   await db.prepare('UPDATE jobs SET lane=? WHERE key=?').run(lane,'files:'+JSON.stringify(q.args));
  }
  const counts={};for(let i=0;i<15;i++){const j=(await state.nextJob());counts[j.lane]=(counts[j.lane] || 0)+1;}
  assert.deepEqual(counts,{requested:8,active:4,verification:2,archive:1});
  const next=(await state.nextJob()),claimed=(await state.claim(next));
  assert.ok(claimed.lease_id);assert.equal((await state.claim(next)),undefined);
  assert.notEqual((await state.nextJob()).key,claimed.key);
  await db.prepare('UPDATE jobs SET lease_until=0 WHERE key=?').run(claimed.key);
  const replacement=(await state.claim(claimed));assert.notEqual(replacement.lease_id,claimed.lease_id);
  assert.equal(replacement.started,claimed.started,'expired leases preserve staged pagination');
 } finally {await db.close();}
});

test('late R2 encoding cannot overwrite a webhook and replaced leases cannot publish coverage',async()=>{
 const {state,db,env}=(await fixture());
 try {
  await state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
  let release,entered;const ready=new Promise(r=>entered=r),gate=new Promise(r=>release=r);
  const original=env.ARCHIVE.put;env.ARCHIVE.put=async(...args)=>{entered();await gate;return original(...args);};
  const pending=state.put('issue_comments','1',{id:1,body:'x'.repeat(1500000),updated_at:'2026-10-12T00:00:00Z'},Date.now()-10000);
  await ready;await state.put('issue_comments','1',{id:1,body:'new webhook',updated_at:'2026-10-12T00:01:00Z'},Date.now(),false,'7',true);
  release();assert.equal(await pending,false);
  assert.equal(JSON.parse((await db.prepare("SELECT data FROM resources WHERE kind='issue_comments' AND id='1'").get()).data).body,'new webhook');
  const q={repository:'org/repo',operation:'files',args:{number:7}};await state.enqueue(q);
  const claimed=(await state.claim((await state.nextJob())));await db.prepare('UPDATE jobs SET lease_id=NULL WHERE key=?').run(claimed.key);
  await state.refresh(claimed,{data:[],api:'core'});assert.equal((await state.complete(q)),false);
 } finally {await db.close();}
});

test('head-change webhooks refresh immutable file lists without resetting a quota cooldown',async()=>{
 const {state,db,objects}=(await fixture());
 try {
  await state.put('pr','7',{number:7,state:'OPEN',headRefOid:'a'.repeat(40),updatedAt:'2026-10-12T00:00:00Z'});
  const q={repository:'org/repo',operation:'files',args:{number:7}},key='files:{"number":7}';
  await state.enqueue(q,20,Date.now()+86400000);
  await db.prepare("UPDATE jobs SET due=?,wait_reason='primary:core',attempt=1 WHERE key=?").run(Date.now()+3600000,key);
  const before=(await db.prepare('SELECT * FROM jobs WHERE key=?').get(key));
  objects.set('head',JSON.stringify({pull_request:{number:7,state:'open',updated_at:'2026-10-12T01:00:00Z',head:{sha:'b'.repeat(40)},user:{login:'peer'}}}));
  await state.ingest({key:'head',delivery:'head',event:'pull_request',repository:'org/repo',received:Date.now()});
  const after=(await db.prepare('SELECT * FROM jobs WHERE key=?').get(key));
  assert.equal(after.dirty,1);assert.equal(after.lane,'active');assert.equal(after.due,before.due);assert.ok(after.generation>before.generation);
 } finally {await db.close();}
});

test('REST exhaustion repairs a flat review history through its existing GraphQL thread dependency',async()=>{
 const {state,db}=(await fixture());
 try {
  await state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
  const q={repository:'org/repo',operation:'review_comments',args:{number:7}},key='review_comments:{"number":7}';
  await state.noteDemand(q);await state.enqueue(q,5);
  const parent=(await state.claim((await state.nextJob()))),reset=Date.now()+3600000;
  await state.refresh(parent,{deferred:true,category:'primary:core',retry_at:reset});
  assert.equal((await db.prepare('SELECT due FROM jobs WHERE key=?').get(key)).due,reset);
  const sourceKey='review_threads:{"number":7}',source=(await db.prepare('SELECT * FROM jobs WHERE key=?').get(sourceKey));
  assert.equal(source.lane,'requested');
  await state.refresh(source,{api:'graphql',data:[{id:'thread',isResolved:false,comments:{nodes:[{id:'node',fullDatabaseId:'99',body:'retained review',createdAt:'2026-10-12T00:00:00Z',updatedAt:'2026-10-12T00:00:00Z',author:{login:'peer'}}],pageInfo:{hasNextPage:false}}}]});
  const result=await state.query(q);assert.equal(result.complete,true);assert.equal(result.data[0].body,'retained review');
  assert.equal((await db.prepare('SELECT wait_reason FROM jobs WHERE key=?').get(key)).wait_reason,null);
 } finally {await db.close();}
});

test('a webhook during a stored read cannot cache old rows under the newer revision',async()=>{
 const {state,db}=await fixture();try {
  await state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
  const q={repository:'org/repo',operation:'issue_comments',args:{number:7}},key='issue_comments:{"number":7}';
  await state.put('issue_comments','1',{id:'1',body:'old'},Date.now(),false,'7');await state.membership(key,'1',true);
  await db.prepare('INSERT INTO collections VALUES(?,?,?,?,?,?,?)').run(key,q.operation,JSON.stringify(q.args),1,Date.now(),await state.revision(),null);
  let release,entered,paused=false;const ready=new Promise(r=>entered=r),gate=new Promise(r=>release=r),rows=state.rows;
  state.rows=async function(query){const result=await rows.call(this,query);if(!paused && query.operation==='issue_comments'){paused=true;entered();await gate;}return result;};
  const request=()=>state.fetch(new Request('https://internal/query',{method:'POST',body:JSON.stringify(q)})).then(r=>r.json());
  const pending=request();await ready;
  await state.put('issue_comments','1',{id:'1',body:'new'},Date.now(),false,'7',true);
  release();const first=await pending,next=await request();
  assert.equal(first.data[0].body,'new');assert.equal(next.data[0].body,'new');assert.equal(next.revision,await state.revision());
 }finally{await db.close();}
});

test('a stale selection cannot claim or retire a newer repair generation',async()=>{
 const {state,db}=await fixture();try {
  await state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
  await state.put('pr','7',{number:7,state:'CLOSED'});
  const q={repository:'org/repo',operation:'issue_comments',args:{number:7}},key='issue_comments:{"number":7}';
  await db.prepare('INSERT INTO collections VALUES(?,?,?,?,?,?,?)').run(key,q.operation,JSON.stringify(q.args),1,Date.now(),await state.revision(),null);
  await state.enqueue(q);const stale=await state.nextJob();
  await state.enqueue(q,1,Date.now(),true);
  assert.equal(await state.claim(stale),undefined);
  assert.equal(await state.retireUnneeded(stale),false);
  const current=await state.claim(await state.nextJob());assert.equal(current.dirty,1);assert.ok(current.generation>stale.generation);
 }finally{await db.close();}
});

test('delta surveys keep fetching until the stored watermark and publish both pages',async()=>{
 const {state,db}=await fixture();try{
  await state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
  const q={repository:'org/repo',operation:'pr_delta'};await state.setMeta('pr_delta_watermark',Date.parse('2026-10-19T12:00:00Z'));await state.enqueue(q);
  const job=await state.claim(await state.nextJob());
  await state.refresh(job,{api:'graphql',cursor:'second',data:[{number:7,state:'OPEN',updatedAt:'2026-10-19T13:00:00Z'}]});
  assert.equal(await state.complete(q),false);
  const next=await db.prepare('SELECT * FROM jobs WHERE key=?').get(job.key);assert.equal(next.cursor,'second');assert.equal(next.page,2);
  await state.refresh(next,{api:'graphql',data:[{number:8,state:'CLOSED',updatedAt:'2026-10-19T12:30:00Z'}]});
  assert.equal(await state.complete(q),true);
  assert.equal((await db.prepare("SELECT COUNT(*) n FROM resources WHERE kind='pr' AND id IN ('7','8')").get()).n,2);
 }finally{await db.close();}
});

test('webhook freshness evidence stays on its resource without permanent per-input metadata',async()=>{
 const {state,db}=await fixture();try{
  await state.put('issue_comments','1',{id:'1',body:'retained'},100,false,'7',true);
  assert.equal(await state.put('issue_comments','1',{id:'1',body:'obsolete'},99,false,'7',true),false);
  assert.equal((await db.prepare("SELECT COUNT(*) n FROM meta WHERE key LIKE 'input:%'").get()).n,0);
  const stored=await db.prepare("SELECT data FROM resources WHERE kind='issue_comments' AND id='1'").get();
  assert.equal(JSON.parse(stored.data).__tauceti_received_at,100);assert.deepEqual(await state.decode(stored.data),{id:'1',body:'retained'});
 }finally{await db.close();}
});


test('malformed upstream state retains coverage and backs off with a traceable request ID',async()=>{
 const {state,db}=await fixture();try {
  await state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
  const q={repository:'org/repo',operation:'issue_comments',args:{number:7}};await state.enqueue(q);
  const key='issue_comments:{"number":7}',failure={unavailable:true,reason:'malformed_upstream_response',request_id:'upstream-trace'};
  const firstStart=Date.now();await state.refresh(await db.prepare('SELECT * FROM jobs WHERE key=?').get(key),failure);
  let row=await db.prepare('SELECT * FROM jobs WHERE key=?').get(key);
  assert.equal(row.wait_reason,failure.reason);assert.equal(row.attempt,1);assert.ok(row.due>=firstStart+60000);
  const secondStart=Date.now();await state.refresh(row,failure);row=await db.prepare('SELECT * FROM jobs WHERE key=?').get(key);
  assert.equal(row.attempt,2);assert.ok(row.due>=secondStart+120000);
  assert.equal((await state.meta('last_upstream_failure')).request_id,'upstream-trace');
  assert.equal((await db.prepare('SELECT COUNT(*) n FROM collections WHERE complete=1').get()).n,0);
 }finally {await db.close();}
});

test('whole-batch malformed responses retain the upstream trace and scheduler backoff',async()=>{
 const {state,db,env}=await fixture();try {
  await state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
  env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>Response.json({unavailable:true,reason:'malformed_upstream_response',request_id:'batch-trace'},{status:503})})};
  for(const number of [7,8])await state.enqueue({repository:'org/repo',operation:'issue_comments',args:{number}});
  const firstStart=Date.now();await state.dispatch(await state.claim(await state.nextJob()));
  for(const row of await db.prepare('SELECT attempt,wait_reason,due FROM jobs').all()) {
   assert.equal(row.wait_reason,'malformed_upstream_response');assert.equal(row.attempt,1);assert.ok(row.due>=firstStart+60000);
  }
  assert.equal((await state.meta('last_upstream_failure')).request_id,'batch-trace');
 }finally {await db.close();}
});

test('opt-in alarm traces identify publication stages without exposing input',async()=>{
 const {state,db,env}=await fixture(),messages=[],original=console.log;
 console.log=(...args)=>messages.push(args.join(' '));
 try {
  await state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
  const q={repository:'org/repo',operation:'files',args:{number:7}},key='files:{"number":7}';
  await state.enqueue(q);
  const job=await db.prepare('SELECT * FROM jobs WHERE key=?').get(key);
  const result={api:'core',data:[{filename:'private-file-name',patch:'private-payload-marker'}]};
  state.expire=async()=>({});state.nextJob=async()=>undefined;
  state.maintenance=async()=>{};state.startStorageMaintenance=()=>{};
  await state.refresh(job,result);
  await state.runAlarm();
  assert.equal(messages.length,0,'ordinary operation has no trace logging');
  env.RECONCILIATION_TRACE='true';
  await state.refresh(job,result);
  env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>Response.json(result)})};
  await state.dispatch(await state.claim(await db.prepare('SELECT * FROM jobs WHERE key=?').get(key)));
  await state.runAlarm();
  state.prepareStorage=async()=>{};await state.storageMaintenance(false);
  const trace=messages.map(message=>JSON.parse(message));
  for(const stage of ['refresh_response','publication_load_pages','publication_encode_pages','refresh_transaction','publication_decode_pages','publication_rows','publication_repairs','publication_applied','refresh_committed','dispatch','dispatch_complete','expire','select_jobs','maintenance','storage_maintenance','storage_prepare','storage_prepared','alarm_complete'])
   assert.ok(trace.some(entry=>entry.stage===stage),stage);
  for(const entry of trace){
   assert.equal(entry.event,'repository_repair_trace');
   assert.ok(Object.keys(entry).every(key=>['event','stage','backend','trace','operation','page','started','rows','repairs','staged_chars'].includes(key)));
   if(entry.operation)assert.match(entry.trace,/^[a-f0-9-]{36}$/);
   if(entry.page!==undefined)assert.equal(typeof entry.page,'number');
   if(entry.started!==undefined && entry.started!==null)assert.equal(typeof entry.started,'number');
   for(const count of ['rows','repairs','staged_chars'])if(entry[count]!==undefined)assert.equal(typeof entry[count],'number');
  }
  assert.equal(messages.join('').includes('private-'),false);
  assert.equal(messages.join('').includes('number'),false);
  assert.equal(trace.find(entry=>entry.stage==='publication_rows').rows,1);
  assert.ok(trace.find(entry=>entry.stage==='refresh_transaction').staged_chars>0);
  const dispatchTrace=trace.find(entry=>entry.stage==='dispatch').trace;
  assert.ok(trace.some(entry=>entry.stage==='refresh_committed' && entry.trace===dispatchTrace));
  assert.ok(trace.some(entry=>entry.stage==='dispatch_complete' && entry.trace===dispatchTrace));
  const stages=trace.filter(entry=>entry.trace===dispatchTrace).map(entry=>entry.stage);
  assert.ok(stages.indexOf('publication_encode_pages')<stages.indexOf('refresh_transaction'));
  assert.ok(stages.indexOf('publication_applied')<stages.indexOf('refresh_committed'));
  const atomic=state.atomic.bind(state);messages.length=0;
  state.atomic=apply=>atomic(async scoped=>{await apply(scoped);throw Error('commit interrupted');});
  await assert.rejects(state.refresh(job,result),/commit interrupted/);
  assert.ok(messages.some(line=>JSON.parse(line).stage==='publication_applied'));
  assert.equal(messages.some(line=>JSON.parse(line).stage==='refresh_committed'),false);
 }finally{console.log=original;await db.close();}
});

test('large staged repair fan-out rolls back, resumes, and reads scheduling metadata without payload copies',async()=>{
 const {state,db,env}=await fixture();try {
  await state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
  let upstream=0;env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>{upstream++;throw Error('unexpected GitHub request');}})};
  const q={repository:'org/repo',operation:'recent_prs'},key='recent_prs:{}',started=Date.now()-10000,count=2200;
  const body='private-history-payload '.repeat(250);
  const rows=Array.from({length:count},(_,i)=>({number:i+1,state:'CLOSED',updatedAt:new Date(started-1000).toISOString(),body,headRefOid:'a'.repeat(40)}));
  await state.enqueue(q);await db.prepare('UPDATE jobs SET started=? WHERE key=?').run(started,key);
  const child={repository:'org/repo',operation:'issue_comments',args:{number:1}},childKey='issue_comments:{"number":1}',cooldown=Date.now()+3600000;
  await state.enqueue(child);await db.prepare("UPDATE jobs SET due=?,attempt=4,wait_reason='quota',generation=17 WHERE key=?").run(cooldown,childKey);
  const job=await db.prepare('SELECT * FROM jobs WHERE key=?').get(key),before=await state.revision();
  const enqueue=state.enqueueMany;let interrupt=true,checked=0;
  state.enqueueMany=async function(input){
   const store=this.store;
   const check=sql=>{
    if(/^SELECT .* FROM (resources|jobs|collections)\b/.test(sql)){
     assert.equal(/\bSELECT \* FROM (resources|jobs|collections)\b/.test(sql),false,'fan-out must not copy resource bodies or nested job cursors');
     if(/ FROM resources\b/.test(sql))assert.equal(/\bdata\b/.test(sql.replace(/json_extract\(data,'\$\.state'\)/g,'')),false);
     checked++;
    }
   };
   this.store=new Proxy(store,{get(target,name){
    if(name==='exec')return (sql,...args)=>{check(sql);return target.exec(sql,...args);};
    if(name==='batch')return statements=>{for(const statement of statements)check(statement.sql);return target.batch(statements);};
    const value=Reflect.get(target,name,target);return typeof value==='function'?value.bind(target):value;
   }});
   try {await enqueue.call(this,input);if(interrupt)throw Error('fanout interrupted');}finally{this.store=store;}
  };
  await assert.rejects(state.refresh(job,{api:'core',data:rows}),/fanout interrupted/);
  assert.equal(await state.revision(),before);assert.equal(await state.complete(q),false);
  assert.equal((await db.prepare('SELECT COUNT(*) n FROM resources').get()).n,0);
  assert.equal((await db.prepare('SELECT COUNT(*) n FROM members').get()).n,0);
  assert.equal((await db.prepare('SELECT COUNT(*) n FROM jobs').get()).n,2);
  interrupt=false;await state.refresh(job,{api:'core',data:rows});
  assert.equal(await state.complete(q),true);
  assert.equal((await db.prepare("SELECT COUNT(*) n FROM resources WHERE kind='pr'").get()).n,count);
  assert.equal((await db.prepare('SELECT COUNT(*) n FROM members WHERE collection=?').get(key)).n,count);
  const retained=await db.prepare('SELECT due,attempt,wait_reason,generation FROM jobs WHERE key=?').get(childKey);
  assert.deepEqual({...retained},{due:cooldown,attempt:4,wait_reason:'quota',generation:17});
  assert.equal((await db.prepare("SELECT COUNT(*) n FROM jobs WHERE key!=? AND key!=? AND lane!='archive'").get(key,childKey)).n,0);
  const revision=await state.revision();await state.refresh(job,{api:'core',data:rows});
  assert.equal(await state.revision(),revision,'repeated completion publishes no duplicate semantic changes');
  assert.ok(checked>0);assert.equal(upstream,0);
 }finally{await db.close();}
});

test('bounded fan-out deduplicates dirty generations across batch boundaries and retains quota cooldowns',async()=>{
 const {state,db}=await fixture();try{
  await state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
  const first={repository:'org/repo',operation:'issue_comments',args:{number:1}},key='issue_comments:{"number":1}',cooldown=Date.now()+3600000;
  await state.enqueue(first);await db.prepare("UPDATE jobs SET generation=17,due=?,attempt=4,wait_reason='quota' WHERE key=?").run(cooldown,key);
  const input=Array.from({length:513},(_,i)=>({q:{...first,args:{number:i+1}},priority:20,dirty:true}));
  input.push({q:first,priority:2,dirty:true},{q:first,priority:3});
  await state.enqueueMany(input);
  const row=await db.prepare('SELECT generation,due,attempt,wait_reason,dirty,priority FROM jobs WHERE key=?').get(key);
  assert.deepEqual({...row},{generation:18,due:cooldown,attempt:4,wait_reason:'quota',dirty:1,priority:3});
  assert.equal((await db.prepare('SELECT COUNT(*) n FROM jobs').get()).n,513);
 }finally{await db.close();}
});

test('existing init fragments are rebuilt from cached comments without GitHub reads',async()=>{
 const {state,db}=await fixture(),oldFetch=globalThis.fetch;
 globalThis.fetch=async(...args)=>{
  const url=String(args[0]?.url ?? args[0]);
  if(new URL(url).hostname==='api.github.com')throw Error('projection migration must not read GitHub');
  return oldFetch(...args); // PostgreSQL fixtures use fetch for their database transport.
 };
 try {
  await state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
  await state.put('pr','7',{number:7,state:'OPEN',headRefOid:'new',labels:[]});
  await state.membership('open_prs:{}','7',true);
  for(const [id,meta] of [['100',{head_sha:'old',states:{reuse:'green'},runs:[]}],['101',{head_sha:'new',mode:'init',states:{reuse:'blocking_request'},runs:[]}]]) {
   await state.put('issue_comments',id,{id,updated_at:id,body:'<!--tauceti-scoreboard--> <!--tauceti-meta:v1 '+JSON.stringify(meta)+'-->'},Date.now(),false,'7');
   await state.membership('issue_comments:{"number":7}',id,true);
  }
  // Reproduce the old projection, which discarded mode and treated init as completed.
  await db.prepare('INSERT INTO survey_fragments VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET parent=excluded.parent,data=excluded.data').run('issue_comments','101','7',JSON.stringify({id:'101',updated:'101',meta:{head_sha:'new',states:{reuse:'blocking_request'},runs:[]},marker:null,rebase_heads:[]}));
  await db.prepare('INSERT INTO survey_hydrated VALUES(?,?) ON CONFLICT(number) DO UPDATE SET contract=excluded.contract').run('7','tauceti-survey-facts/v1');
  assert.equal(JSON.parse(await state.projectedSurvey('7')).meta.head_sha,'new');
  await state.maintenance();
  assert.equal(JSON.parse(await state.projectedSurvey('7')).meta.head_sha,'old');
  const marker=(await db.prepare('SELECT contract FROM survey_hydrated WHERE number=?').get('7')).contract;
  assert.equal(marker,'tauceti-survey-facts/v1:completed-scoreboards');
  assert.equal((await state.surveyHealth()).contract,'tauceti-survey-facts/v1','public contract remains compatible');
  await state.prepareDiagnostics();
  assert.equal(state.diagnostics.survey.hydrated,1,'prepared health uses the internal index marker');
  assert.equal(state.diagnostics.survey.contract,'tauceti-survey-facts/v1');
 }finally{globalThis.fetch=oldFetch;await db.close();}
});
