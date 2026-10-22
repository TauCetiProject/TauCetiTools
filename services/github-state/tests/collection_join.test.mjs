import {test} from 'node:test';
import assert from 'node:assert/strict';
import {runtime,repository,send} from './runtime.mjs';

test('a selected collection reads indexed members before resource payloads and freshness',async()=>{
 const mf=await runtime(async()=>{throw Error('unexpected GitHub request');},{},{repositoryAlarms:false,repositoryExtension:`
 async fetch(req){
  const path=new URL(req.url).pathname;
  if(path==='/fixture/seed'){
   await this.put('issue_comments','42',{id:42,body:'selected comment'},Date.now(),false,'7');
   await this.membership('issue_comments:{"number":7}','42',true);
   await this.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)','issue_comments:{"number":7}','issue_comments','{"number":7}',1,Date.now(),await this.revision(),null);
   return Response.json({ok:true});
  }
  if(path==='/fixture/plans'){
   const plans=[],exec=this.store.exec.bind(this.store);
   this.store.exec=async(sql,...bindings)=>{
    if(sql.includes('FROM members m') && sql.includes('JOIN resources r'))plans.push({sql,steps:this.sql.exec('EXPLAIN QUERY PLAN '+sql,...bindings).toArray().map(row=>row.detail)});
    return exec(sql,...bindings);
   };
   try{return Response.json({result:await this.query({repository:'org/repo',operation:'issue_comments',args:{number:7}}),plans});}
   finally{this.store.exec=exec;}
  }
  return super.fetch(req);
 }`});
 try{
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName(repository.id));
  await send(stub,'/fixture/seed',{});
  const {result,plans}=await (await send(stub,'/fixture/plans',{})).json();
  assert.equal(result.status,'ok');assert.equal(result.data.length,1);assert.equal(result.data[0].body,'selected comment');
  assert.ok(plans.some(p=>p.sql.includes('MAX(r.observed)')),'actual freshness query must be measured');
  assert.ok(plans.some(p=>p.sql.includes('r.data')),'actual payload query must be measured');
  for(const plan of plans){
   const searches=plan.steps.filter(s=>/SEARCH [mr] /.test(s));
   assert.match(searches[0],/SEARCH m .*collection=/,JSON.stringify(plan));
   assert.ok(searches.some(s=>/SEARCH r .*kind=.*id=/.test(s)),JSON.stringify(plan));
  }
 }finally{await mf.dispose();}
});
