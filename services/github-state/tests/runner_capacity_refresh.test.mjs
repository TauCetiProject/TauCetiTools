import test from 'node:test';
import assert from 'node:assert/strict';
import {runtime,response} from './runtime.mjs';

function deferred(){let resolve;const promise=new Promise(r=>{resolve=r;});return {promise,resolve};}
async function promptly(promise){let timer;try{return await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('fresh capacity waited for background discovery')),2000);})]);}finally{clearTimeout(timer);}}

test('native warm routing serves honest fresh observations during one held refresh and publishes its replacement',async()=>{
 let gate=deferred(),entered=deferred(),orgReads=0;
 const mf=await runtime(async(req,url)=>{
  if(url.pathname.startsWith('/orgs/')){orgReads++;entered.resolve();await gate.promise;return response([{name:'repo',archived:false}],'core');}
  if(url.pathname.endsWith('/actions/runs'))return response({workflow_runs:[]},'core');
  throw Error('unexpected provider path');
 },{},{installationExtension:`
  async fetch(req){
   const path=new URL(req.url).pathname;
   if(path==='/fixture/capacity'){
    const value=await req.json();await this.ctx.storage.put('runner-capacity:org',value);await this.ctx.storage.put('runner-capacity-last-good:org',value);return Response.json({ok:true});
   }
   if(path==='/fixture/await-capacity'){
    if(this.capacityFlight)await this.capacityFlight;return Response.json({done:true});
   }
   return super.fetch(req);
  }
 `});
 const get=()=>mf.dispatchFetch('https://state/v1/runner-capacity?repository=org/repo',{headers:{Authorization:'Bearer client'}}).then(r=>r.json());
 const cached=()=>mf.dispatchFetch('https://state/v1/runner-capacity/cached?repository=org/repo',{headers:{Authorization:'Bearer client'}}).then(r=>r.json());
 try{
  const ns=await mf.getDurableObjectNamespace('INSTALLATIONS'),stub=ns.get(ns.idFromName('200'));
  const seed=async age=>{
   const observed=Date.now()-age;
   const value={complete:true,refresh_version:2,organization:'org',namespace_discovery_target:20,routing_version:2,busy:3,namespace_busy:6,queued_fleet:[],running_jobs:{github:3,namespace:6},observed_at:observed,retry_at:observed+145000,elapsed_ms:42000};
   await stub.fetch('https://internal/fixture/capacity',{method:'POST',body:JSON.stringify(value)});return value;
  };
  // The previous42s duration starts a new refresh before the145s cadence.
  let old=await seed(110000);
  const replies=await promptly(Promise.all([get(),get(),get()]));
  await promptly(entered.promise);
  assert.equal(orgReads,1,'concurrent warm callers must share one scan');
  for(const value of replies){assert.equal(value.cached,true);assert.equal(value.observed_at,old.observed_at);assert.equal(value.namespace_busy,6);}
  assert.equal((await cached()).observed_at,old.observed_at,'a background scan cannot manufacture freshness');
  gate.resolve();await stub.fetch('https://internal/fixture/await-capacity');
  let replacement=await cached();assert.ok(replacement.observed_at>old.observed_at);assert.equal(replacement.namespace_busy,0);

  // The refresh schedule has expired, but the client's150s freshness has not.
  gate=deferred();entered=deferred();old=await seed(146000);
  const nearExpiry=await promptly(get());await promptly(entered.promise);
  assert.equal(nearExpiry.observed_at,old.observed_at);assert.equal(nearExpiry.retry_at,old.observed_at+150000);
  assert.ok(nearExpiry.retry_at>Date.now());assert.equal(nearExpiry.namespace_busy,6);
  gate.resolve();await stub.fetch('https://internal/fixture/await-capacity');

  // Once the observation is genuinely expired, a caller must await new data.
  gate=deferred();entered=deferred();old=await seed(151000);let settled=false;
  const expired=get().then(value=>{settled=true;return value;});await promptly(entered.promise);
  assert.equal(settled,false,'expired data must never be returned as fresh');
  gate.resolve();replacement=await expired;
  assert.ok(replacement.observed_at>old.observed_at);assert.equal(replacement.namespace_busy,0);
 }finally{gate.resolve();await mf.dispose();}
});
