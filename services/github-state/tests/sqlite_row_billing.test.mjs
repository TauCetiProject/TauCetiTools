import {test} from 'node:test';
import assert from 'node:assert/strict';
import {runtime,repository,send} from './runtime.mjs';

// Cloudflare bills SQLite rows read and written per statement. These tests use
// workerd's own cursor counters to check that unchanged writes are skipped and
// that hot reads use indexes, while the stored state stays exactly the same.
const counted=`
 constructor(ctx,env){
  super(ctx,env);
  const exec=ctx.storage.sql.exec.bind(ctx.storage.sql);
  this.statements=[];
  ctx.storage.sql.exec=(sql,...args)=>{const cursor=exec(sql,...args);this.statements.push({sql,cursor});return cursor;};
 }
 rows(match){
  let read=0,written=0;
  for(const {sql,cursor} of this.statements)if(match(sql)){read+=cursor.rowsRead;written+=cursor.rowsWritten;}
  this.statements=[];return {read,written};
 }`;
const jobs=state=>state.sql.exec('SELECT * FROM jobs ORDER BY key').toArray();
const extension=counted+`
 async fetch(req){
  const path=new URL(req.url).pathname,input=req.method==='POST'?await req.json():{};
  if(path==='/t/enqueue'){
   this.rows(()=>true);
   await this.enqueueMany(input.items,input.backfill);
   return Response.json({...this.rows(sql=>sql.startsWith('INSERT INTO jobs')),jobs:(${jobs})(this)});
  }
  if(path==='/t/ingest'){
   this.rows(()=>true);
   await this.enqueueIngest(input.items);
   return Response.json({...this.rows(sql=>sql.startsWith('INSERT INTO jobs')),jobs:(${jobs})(this)});
  }
  if(path==='/t/meta'){
   this.rows(()=>true);
   await this.atomic(state=>state.setMeta(input.key,input.value));
   return Response.json({...this.rows(sql=>sql.startsWith('INSERT INTO meta')),value:await this.meta(input.key)});
  }
  if(path==='/t/clear-validators'){
   this.rows(()=>true);
   await this.atomic(async state=>{await state.store.exec('UPDATE jobs SET etag=? WHERE key=?',input.etag,input.key);});
   this.rows(()=>true);
   await this.atomic(async state=>{await state.put('pr',String(input.number),{number:input.number,title:input.title,headRefOid:'a'.repeat(40),baseRefOid:'b'.repeat(40),state:'OPEN'});});
   return Response.json({...this.rows(sql=>/^UPDATE (jobs|collections) SET etag=NULL/.test(sql)),jobs:(${jobs})(this)});
  }
  if(path==='/t/wake'){
   const results=[];
   for(const {where,bindings} of input.cases){
    this.rows(()=>true);
    const actual=await this.earliestWake(where,bindings);
    const cost=this.rows(sql=>sql.includes('FROM jobs'));
    const expected=this.sql.exec('SELECT MIN(MAX(due,lease_until)) AS due FROM jobs'+where,...bindings).one().due;
    results.push({actual,expected,read:cost.read});
   }
   return Response.json(results);
  }
  if(path==='/t/jobs'){
   this.sql.exec('DELETE FROM jobs');
   for(const [key,due,lease] of input.rows)this.sql.exec('INSERT INTO jobs(key,query,priority,due,lease_until,lane) VALUES(?,?,20,?,?,?)',key,'{}',due,lease,'verification');
   return Response.json({ok:true});
  }
  return super.fetch(req);
 }`;
const q=(operation,args)=>({repository:repository.name,operation,args});

test('re-enqueueing unchanged jobs writes no SQLite rows and leaves every job unchanged',{skip:Boolean(process.env.TEST_POSTGRES_URL)},async()=>{
 const mf=await runtime(async()=>{throw Error('unexpected GitHub request');},{},{repositoryAlarms:false,repositoryExtension:extension});
 try{
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName(repository.id));
  const items=[];
  for(let n=1;n<=24;n++)for(const operation of ['issue_comments','review_threads','reviews','files'])items.push({q:q(operation,{number:n}),priority:20});
  items.push({q:q('statuses',{sha:'c'.repeat(40)}),priority:2});
  const first=await (await send(stub,'/t/enqueue',{items})).json();
  assert.equal(first.jobs.length,97);assert.ok(first.written>=97,'new jobs are inserted');
  const again=await (await send(stub,'/t/enqueue',{items})).json();
  assert.equal(again.written,0,'an unchanged enqueue rewrites no job or index rows');
  assert.deepEqual(again.jobs,first.jobs);
  // Every real change still lands: a dirty generation, a new priority and lane.
  const dirty=await (await send(stub,'/t/enqueue',{items:[{q:q('files',{number:3}),priority:20,dirty:true},{q:q('reviews',{number:4}),priority:5}]})).json();
  assert.ok(dirty.written>0);
  const before=new Map(first.jobs.map(job=>[job.key,job])),after=new Map(dirty.jobs.map(job=>[job.key,job]));
  assert.equal(after.get('files:{"number":3}').generation,before.get('files:{"number":3}').generation+1);
  assert.equal(after.get('files:{"number":3}').dirty,1);
  assert.equal(after.get('files:{"number":3}').lane,'active');
  assert.equal(after.get('reviews:{"number":4}').priority,5);
  for(const [key,job] of after)if(!['files:{"number":3}','reviews:{"number":4}'].includes(key))assert.deepEqual(job,before.get(key),key);
  // Webhook repairs use their own upsert; unchanged repeats are also skipped.
  // A missing collection with dirtyIfMissing is a real change on every repeat.
  const ingest=[{q:q('pr',{number:9}),priority:0},{q:q('open_prs'),priority:1}];
  const created=await (await send(stub,'/t/ingest',{items:ingest})).json();assert.ok(created.written>0);
  const repeated=await (await send(stub,'/t/ingest',{items:ingest})).json();
  assert.equal(repeated.written,0);assert.deepEqual(repeated.jobs,created.jobs);
  const changed=await (await send(stub,'/t/ingest',{items:[{q:q('pr',{number:9}),priority:0,dirty:true}]})).json();
  const prior=created.jobs.find(job=>job.key==='pr:{"number":9}'),current=changed.jobs.find(job=>job.key==='pr:{"number":9}');
  assert.ok(changed.written>0);assert.equal(current.generation,prior.generation+1);
 }finally{await mf.dispose();}
});

test('unchanged metadata and cleared validators are not rewritten',{skip:Boolean(process.env.TEST_POSTGRES_URL)},async()=>{
 const mf=await runtime(async()=>{throw Error('unexpected GitHub request');},{},{repositoryAlarms:false,repositoryExtension:extension});
 try{
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName(repository.id));
  const first=await (await send(stub,'/t/meta',{key:'pr_inventory_api',value:'graphql'})).json();
  assert.ok(first.written>=1);
  const same=await (await send(stub,'/t/meta',{key:'pr_inventory_api',value:'graphql'})).json();
  assert.equal(same.written,0);assert.equal(same.value,'graphql');
  const changed=await (await send(stub,'/t/meta',{key:'pr_inventory_api',value:'core'})).json();
  assert.ok(changed.written>=1);assert.equal(changed.value,'core');
  // A changed PR clears its validators; an already clear validator costs nothing.
  await send(stub,'/t/enqueue',{items:[{q:q('pr',{number:5}),priority:20}]});
  const cleared=await (await send(stub,'/t/clear-validators',{key:'pr:{"number":5}',etag:'"v1"',number:5,title:'one'})).json();
  assert.equal(cleared.written,1);assert.equal(cleared.jobs.find(job=>job.key==='pr:{"number":5}').etag,null);
  const clear=await (await send(stub,'/t/clear-validators',{key:'pr:{"number":6}',etag:null,number:5,title:'two'})).json();
  assert.equal(clear.written,0);assert.equal(clear.jobs.find(job=>job.key==='pr:{"number":5}').etag,null);
 }finally{await mf.dispose();}
});

test('the repair wake matches the full aggregate while reading a few indexed rows',{skip:Boolean(process.env.TEST_POSTGRES_URL)},async()=>{
 const mf=await runtime(async()=>{throw Error('unexpected GitHub request');},{},{repositoryAlarms:false,repositoryExtension:extension});
 try{
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName(repository.id));
  let seed=7;const random=()=>(seed=(seed*1103515245+12345)%2147483648)/2147483648;
  const now=1_000_000;
  const forms=keys=>[{where:'',bindings:[]},{where:' WHERE (due>? OR lease_until>?)',bindings:[now,now]},
   {where:' WHERE (due>? OR lease_until>? OR (1=1 AND key NOT IN (SELECT value FROM json_each(?))))',bindings:[now,now,JSON.stringify(keys)]}];
  const layouts={
   // Mostly unleased future work, as in production: the walk stops early.
   ordinary:n=>Array.from({length:n},(_,i)=>['k'+i,now+Math.floor(random()*3600000),random()<0.02?now+60000:0]),
   // Long leases on the earliest due rows force the complete aggregate.
   leased:n=>Array.from({length:n},(_,i)=>['k'+i,now-1000+i,i<100?now+10_000_000:0]),
   // Equal due values, NULL clocks and past leases.
   mixed:n=>Array.from({length:n},(_,i)=>['k'+i,random()<0.1?null:now+Math.floor(random()*5)*1000,random()<0.1?null:random()<0.5?now-5000:now+Math.floor(random()*8000)]),
   empty:()=>[],small:()=>[['a',now+5,now+9],['b',null,now+1],['c',now+7,null]],
   // An overdue unleased backlog that a full coordinator must look past.
   backlog:n=>[...Array.from({length:n},(_,i)=>['b'+i,now-n+i,0]),...Array.from({length:30},(_,i)=>['f'+i,now+1000+i,now+90_000_000])],
  };
  for(const [name,layout] of Object.entries(layouts))for(const n of [3,64,65,500]){
   const rows=layout(n);await send(stub,'/t/jobs',{rows});
   const results=await (await send(stub,'/t/wake',{cases:forms(rows.slice(0,3).map(row=>row[0]))})).json();
   for(const result of results)assert.equal(result.actual,result.expected,name+' '+n+' '+JSON.stringify(result));
   if(name==='ordinary' && n===500)for(const result of results)assert.ok(result.read<=40,'reads '+result.read+' rows instead of every job');
   // The examined prefix is bounded even when few early rows match.
   if(n===500)for(const result of results)assert.ok(result.read<=2*(rows.length+30)+2*(8+256),name+' reads '+result.read);
  }
 }finally{await mf.dispose();}
});

test('accounting expiry reads only the expired prefix and deletes exactly the same rows',{skip:Boolean(process.env.TEST_POSTGRES_URL)},async()=>{
 const mf=await runtime(async()=>{throw Error('unexpected GitHub request');},{},{installationExtension:counted+`
  async fetch(req){
   const path=new URL(req.url).pathname,input=req.method==='POST'?await req.json():{};
   if(path==='/t/accounting'){
    this.ctx.storage.sql.exec('DELETE FROM accounting');
    const old=Date.now()-8*86400000;
    for(let i=0;i<input.old;i++)this.ctx.storage.sql.exec('INSERT INTO accounting(at,operation,api,status,points) VALUES(?,?,?,?,?)',old+i,'pr','graphql',200,1);
    for(let i=0;i<input.fresh;i++)this.ctx.storage.sql.exec('INSERT INTO accounting(at,operation,api,status,points) VALUES(?,?,?,?,?)',Date.now(),'pr','graphql',200,1);
    if(input.late)this.ctx.storage.sql.exec('INSERT INTO accounting(at,operation,api,status,points) VALUES(?,?,?,?,?)',old,'pr','graphql',200,1);
    this.rows(()=>true);this.expireAccounting();
    const cost=this.rows(sql=>sql.includes('FROM accounting'));
    return Response.json({...cost,remaining:this.ctx.storage.sql.exec('SELECT at FROM accounting ORDER BY id').toArray().map(row=>row.at<Date.now()-7*86400000?'old':'fresh')});
   }
   return super.fetch(req);
  }`});
 try{
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName(repository.installation));
  const quiet=await (await send(stub,'/t/accounting',{old:0,fresh:300})).json();
  assert.equal(quiet.remaining.length,300);assert.ok(quiet.read<=5,'a live first row stops the scan: '+quiet.read);
  // A later expired row behind a live one is retained, as before.
  const mixed=await (await send(stub,'/t/accounting',{old:250,fresh:20,late:true})).json();
  assert.deepEqual(mixed.remaining,[...Array(20).fill('fresh'),'old']);
  const large=await (await send(stub,'/t/accounting',{old:5200,fresh:3})).json();
  assert.deepEqual(large.remaining,[...Array(200).fill('old'),'fresh','fresh','fresh'],'one pass still deletes at most the 5,000 oldest rows');
 }finally{await mf.dispose();}
});
