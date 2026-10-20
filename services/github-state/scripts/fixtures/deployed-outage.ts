// Isolated synthetic rehearsal only. Never deploy with production bindings.
import worker,{RepositoryState as BaseState,InstallationScheduler as BaseInstallation} from '../../src/index';
import {authorize,digest,json,queryKey} from '../../src/protocol';
import {neonConfig} from '@neondatabase/serverless';
let faults:any={},databaseHost='',blocked=0,logFailures=0;
let nativeArchive:any,waitUntil:((task:Promise<any>)=>void)|undefined;
const pending=new Set<Promise<any>>();
const originalFetch=globalThis.fetch,NativeSocket=globalThis.WebSocket;
function rejected(kind:string,hostname:string):void {
 blocked++;
 if(!nativeArchive){logFailures++;return;}
 const task=nativeArchive.put('control/fixture-network-blocks/'+crypto.randomUUID(),JSON.stringify({at:Date.now(),kind,hostname})).catch(()=>{logFailures++;});
 pending.add(task);task.finally(()=>pending.delete(task));waitUntil?.(task);
}
globalThis.fetch=async(input:any,init?:any)=>{
 const url=new URL(typeof input==='string'?input:input instanceof URL?input.href:input.url),regional='api.'+databaseHost.split('.').slice(1).join('.');
 let allowed=url.protocol==='https:' && url.hostname===databaseHost && (!url.port || url.port==='443');
 if(url.protocol==='https:' && (!url.port || url.port==='443') && url.hostname===regional && url.pathname==='/sql'){
  try {const connection=new URL(new Headers(init?.headers || {}).get('Neon-Connection-String') || '');allowed=['postgres:','postgresql:'].includes(connection.protocol) && connection.hostname===databaseHost && ['github_state_test_app','github_state_test_migration'].includes(decodeURIComponent(connection.username)) && connection.pathname==='/github_state_test';}catch {}
 }
 if(!allowed){rejected('http',url.hostname);await Promise.allSettled([...pending]);throw Error('fixture forbids external network');}
 if(faults.database)throw Object.assign(Error('fixture database unavailable'),{code:'08006'});
 // The slow drill delays HTTP SQL/upgrade requests. WebSocket stream latency
 // is not simulated; real endpoint restart is exercised separately.
 if(faults.slow_ms)await new Promise(resolve=>setTimeout(resolve,faults.slow_ms));
 return originalFetch(input,{...init,redirect:'manual'});
};
class FixtureSocket extends NativeSocket {
 constructor(url:any,protocols?:any){
  const target=new URL(String(url));
  if(target.protocol!=='wss:' || target.hostname!==databaseHost || target.port && target.port!=='443'){rejected('websocket',target.hostname);throw Error('fixture forbids external network');}
  if(faults.database)throw Object.assign(Error('fixture database unavailable'),{code:'08006'});
  super(url,protocols);
 }
}
neonConfig.webSocketConstructor=FixtureSocket;
function validate(env:any):void {
 if(env.GITHUB_PRIVATE_KEY || env.GITHUB_APP_ID && env.GITHUB_APP_ID!=='0')throw Error('GitHub credentials forbidden in fixture');
 if(!/^issue317-drill-[a-f0-9]{8}$/.test(env.DRILL_RUN_ID || ''))throw Error('isolated fixture identity required');
 const url=new URL(env.REPOSITORY_DATABASE_URL || '');
 if(!['postgres:','postgresql:'].includes(url.protocol) || decodeURIComponent(url.username)!=='github_state_test_app' || url.pathname!=='/github_state_test' || url.hostname!==env.DRILL_DATABASE_HOST)throw Error('disposable test database required');
 if(env.MIGRATION_DATABASE_URL){const migration=new URL(env.MIGRATION_DATABASE_URL);if(migration.hostname!==url.hostname || decodeURIComponent(migration.username)!=='github_state_test_migration' || migration.pathname!==url.pathname || !['postgres:','postgresql:'].includes(migration.protocol))throw Error('disposable test migration role required');}
 const repos=JSON.parse(env.ALLOWED_REPOSITORIES || '[]');
 if(!repos.length || repos.some((r:any)=>!/^issue317\/[a-z]+$/.test(r.name)))throw Error('synthetic repositories required');
}
function wrap(env:any,ctx?:any):any {
 validate(env);
 const native=env.DRILL_NATIVE_ARCHIVE || env.ARCHIVE;
 databaseHost=new URL(env.REPOSITORY_DATABASE_URL).hostname;nativeArchive=native;
 if(ctx?.waitUntil)waitUntil=task=>ctx.waitUntil(task);
 const archive=new Proxy(native,{get(target,key){const member=Reflect.get(target,key,target);return typeof member!=='function'?member:async(...args:any[])=>{if(faults.r2)throw Error('fixture R2 unavailable');return member.apply(target,args);};}});
 return {...env,DRILL_NATIVE_ARCHIVE:native,ARCHIVE:archive};
}
async function prepare(env:any,ctx?:any):Promise<any> {
 const wrapped=wrap(env,ctx),native=wrapped.DRILL_NATIVE_ARCHIVE;
 faults=await native.get('control/fixture-faults.json').then((o:any)=>o?.json()).catch(()=>({})) || {};
 return wrapped;
}
export class RepositoryState extends BaseState {
 constructor(ctx:any,env:any){super(ctx,wrap(env,ctx));}
 // Stored-state rehearsal excludes upstream repair, with a hard network guard
 // as a second line of defence. Cron's migration/ingress owners stay unchanged.
 async setAlarm(){}
 async alarm(){const s:any=this;await prepare(s.env);await s.expire();s.startStorageMaintenance();}
 async fetch(req:Request):Promise<Response>{
  const s:any=this;await prepare(s.env);const path=new URL(req.url).pathname;
 if(path==='/fixture/frozen-source'){
  await s.setMeta('repository',{id:'317'+crypto.getRandomValues(new Uint32Array(1))[0],name:'issue317/frozen',installation:'317'});
  await s.put('pr','7',{number:7,title:'retained frozen header'});
  await s.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)','pr:{"number":7}','pr','{"number":7}',1,Date.now(),await s.revision(),null);
  const generation=crypto.randomUUID(),repo=await s.meta('repository');
  const state:any={version:1,generation,repository:repo.id,target:'fixture-target',source:{object:s.ctx.id.toString(),deployment:s.env.CF_VERSION?.id || 'native-fixture',schema:5},phase:'switching',created:Date.now(),updated:Date.now(),tables:{},failure:null,staging:{after:null,page:'1',checked:0,complete:false,expired_jobs:0,expired_hash:'0'.repeat(64)},blobs:{prefix:0,pages:0,objects:0,hash:'0'.repeat(64),complete:false},cutover:{environment:'test',lease_until:Date.now()+900000,epoch:0,source_fenced:false,target_active:false,import:{},verify:{},acceptance:''}};
  await s.fenceMigration(state);state.cutover.source_fenced=true;s.migrationState=state;
  s.startup();const readable=s.schema==='current';
  const result=await (await super.fetch(new Request('https://internal/query',{method:'POST',body:JSON.stringify({repository:repo.name,operation:'pr',args:{number:7}})}))).json() as any;
  let blockedWrite=false;try{await s.store.exec("INSERT INTO demand VALUES('illegal',1)");}catch(error){blockedWrite=error instanceof Error && /generation fenced|migration_frozen/.test(error.message);}
  s.migrationState.cutover.target_active=true;s.startup();const activatedRejected=s.schema.error==='backend_fenced';
  return json({readable,result_status:result.status,header:result.data?.title,blocked_write:blockedWrite,activated_source_rejected:activatedRejected,blocked_external_fetches:blocked});
 }
 if(path==='/fixture/state')return json({object:s.ctx.id.toString(),deployment:s.env.CF_VERSION?.id,backend:s.store.backend,revision:await s.revision(),blocked_external_fetches:blocked,monitoring_repository:s.monitoringRepository(),migration_held:s.migrationHeld(),mutation_blocked:s.mutationState.blocked,storage_loaded:s.storageLoaded,maintenance_flight:Boolean(s.storageFlight)});
  if(path==='/fixture/seed'){
   if(s.store.backend==='sqlite' && s.localMeta('backend_owner')?.backend==='migration')return json({error:'fixture_frozen'},409);
   const after=(await req.json() as any).after || 0,repo=JSON.parse(s.env.ALLOWED_REPOSITORIES)[0];
   await s.atomic(async(state:any)=>{
    await state.setMeta('repository',repo);
    for(let n=after+1;n<=Math.min(after+8,128);n++){
     await state.put('pr',String(n),{number:n,title:'fixture '+n,body:'stored fixture '.repeat(1400),state:'OPEN',isDraft:false,headRefOid:'a'.repeat(40),baseRefOid:'b'.repeat(40),author:{login:'fixture'},labels:{nodes:[],totalCount:0},updatedAt:'2026-10-20T00:00:00Z'});
     await state.membership('open_prs:{}',String(n),true);
    }
    if(after+8>=128){
     await state.put('issue_comments','9223372036854775807',{id:'9223372036854775807',body:'large fixture '.repeat(120000),created_at:'2026-10-20T00:00:00Z',author:{login:'fixture'}},Date.now(),false,'1');
     await state.membership('issue_comments:{"number":1}','9223372036854775807',true);
     for(const [operation,args] of [['open_prs',{}],['issue_comments',{number:1}]] as any[])await state.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET complete=1,verified=excluded.verified,revision=excluded.revision',queryKey({operation,args}),operation,JSON.stringify(args),1,Date.now(),await state.revision(),null);
     await state.setMeta('bootstrap_inventory',Date.now());await state.setMeta('last_repair',Date.now());
    }
   });
   return json({after:Math.min(after+8,128),complete:after+8>=128});
  }
  return super.fetch(req);
 }
}
export class CanaryState extends RepositoryState {
 constructor(ctx:any,env:any){super(ctx,{...env,REPOSITORIES:env.CANARIES,ALLOWED_REPOSITORIES:env.DRILL_CANARY_REPOSITORY,REPOSITORY_BACKEND:'postgres',REPOSITORY_GENERATION:'canary-v1',MIGRATION_ENVIRONMENT:undefined});}
}
export class InstallationScheduler extends BaseInstallation {
 constructor(ctx:any,env:any){super(ctx,wrap(env,ctx));}
 async fetch(req:Request):Promise<Response>{
  if(new URL(req.url).pathname==='/audit')return json({fixture_stored_only:true});
  return super.fetch(req);
 }
}
export default {
 async fetch(req:Request,env:any,ctx:any):Promise<Response>{
  const path=new URL(req.url).pathname;
  if(path.startsWith('/fixture/')){
   if(!await authorize(req,env,undefined,true))return json({error:'unauthorized'},401);
   try{wrap(env,ctx);}catch{return json({error:'fixture_configuration_rejected'},400);}
   if(path==='/fixture/probe-network'){
    const mode=(await req.json() as any).mode;try{if(mode==='websocket')new FixtureSocket('wss://api.github.com');else await fetch('https://api.github.com');}catch{}
    await Promise.allSettled([...pending]);return json({blocked,log_failures:logFailures});
   }
   if(path==='/fixture/network-audit'){
    const page=await env.ARCHIVE.list({prefix:'control/fixture-network-blocks/',limit:100});
    return json({recorded_attempts:page.objects.length,truncated:page.truncated,isolate_log_failures:logFailures,scope:'R2 receipts across fixture isolates; persistence failures may undercount attempts',physical_non_database_egress:'denied_by_guard'});
   }
   if(path==='/fixture/queue-metrics')return json(await env.ARCHIVE.get('control/queue-metrics.json').then((o:any)=>o?.json()) || {unavailable:true});
   if(path==='/fixture/spill'){
    const input=await req.json() as any,repo=JSON.parse(env.ALLOWED_REPOSITORIES)[0];
    if(typeof input.key!=='string' || !input.key.startsWith('resources/'+repo.id+'/'))return json({error:'fixture_spill_identity'},400);
    const object=await env.ARCHIVE.get(input.key);if(!object)return json({key:input.key,exists:false});
    const bytes=await object.arrayBuffer(),sha256=await digest(bytes),expected=input.key.split('/').at(-1);
    return json({key:input.key,exists:true,bytes:bytes.byteLength,sha256,valid:!expected?.startsWith('sha256-') || expected==='sha256-'+sha256});
   }
   if(path==='/fixture/objects'){
    const input=req.method==='POST'?await req.json() as any:{},page=await env.ARCHIVE.list({limit:100,...(input.cursor?{cursor:input.cursor}:{})});
    return json({objects:page.objects.map((o:any)=>({key:o.key,size:o.size})),truncated:page.truncated,cursor:page.truncated?page.cursor:undefined});
   }
   if(path==='/fixture/delete-objects'){
    const input=await req.json() as any;
    if(input.run!==env.DRILL_RUN_ID || env.DRILL_DISPOSABLE_BUCKET!==env.DRILL_RUN_ID)return json({error:'disposable_confirmation_required'},400);
    const page=await env.ARCHIVE.list({limit:100});
    if(page.objects.length)await env.ARCHIVE.delete(page.objects.map((o:any)=>o.key));
    return json({deleted:page.objects.length,remaining:page.truncated});
   }
   if(path==='/fixture/frozen-source'){
    return env.REPOSITORIES.get(env.REPOSITORIES.idFromName('frozen-source-'+env.DRILL_RUN_ID+'-'+crypto.randomUUID())).fetch('https://internal/fixture/frozen-source',{method:'POST'});
   }
   if(path==='/fixture/faults'){
    const value=await req.json();await env.ARCHIVE.put('control/fixture-faults.json',JSON.stringify(value));return json({stored:true});
   }
   if(path==='/fixture/acceptance'){
    const value:any=await req.json(),repo=JSON.parse(env.ALLOWED_REPOSITORIES)[0];
    if(value.repository!==repo.id || value.environment!=='test' || value.deployment!==env.CF_VERSION.id)return json({error:'fixture_acceptance_identity'},400);
    await env.ARCHIVE.put('control/'+repo.id+'/migration-acceptance.json',JSON.stringify(value));return json({stored:true});
   }
   if(path==='/fixture/pin-resource'){
    const repo=JSON.parse(env.ALLOWED_REPOSITORIES)[0],page=await env.ARCHIVE.list({prefix:'resources/'+repo.id+'/',limit:100});
    const key=page.objects.find((o:any)=>/\/sha256-[a-f0-9]{64}$/.test(o.key))?.key;if(!key)return json({error:'fixture_resource_missing'},404);
    const object=await env.ARCHIVE.get(key);if(!object)return json({error:'fixture_resource_missing'},404);
    const saved=await env.ARCHIVE.put(key,await object.arrayBuffer(),{customMetadata:{...object.customMetadata,pinned:'true'},onlyIf:{etagMatches:object.etag}});
    return json({pinned:Boolean(saved),key});
   }
   if(path==='/fixture/resources'){
    const repo=JSON.parse(env.ALLOWED_REPOSITORIES)[0],page=await env.ARCHIVE.list({prefix:'resources/'+repo.id+'/',limit:100,include:['customMetadata']});
    return json({objects:page.objects.map((o:any)=>({key:o.key,size:o.size,etag:o.etag,uploaded:o.uploaded,metadata:o.customMetadata})),truncated:page.truncated});
   }
   const canary=path.startsWith('/fixture/canary/'),repo=JSON.parse(canary?env.DRILL_CANARY_REPOSITORY:env.ALLOWED_REPOSITORIES)[0],ns=canary?env.CANARIES:env.REPOSITORIES;
   const target=ns.get(ns.idFromName(repo.id));
   if(path.endsWith('/query'))return target.fetch('https://internal/query',{method:'POST',headers:{'X-Request-ID':req.headers.get('X-Request-ID') || crypto.randomUUID()},body:await req.text()});
   return target.fetch('https://internal'+(path.endsWith('/seed')?'/fixture/seed':path.endsWith('/event')?'/event':'/fixture/state'),req.method==='POST'?{method:'POST',body:await req.text()}:undefined);
  }
  return worker.fetch(req,await prepare(env,ctx),ctx);
 },
 async scheduled(event:any,env:any,ctx:any){return worker.scheduled(event,await prepare(env,ctx),ctx);},
 async queue(batch:any,env:any,ctx:any){
  ctx.waitUntil(Promise.all(batch.messages.map((message:any)=>{
   const delivery=message.body?.delivery;
   return /^[a-zA-Z0-9-]{1,100}$/.test(delivery || '')?env.ARCHIVE.put('control/fixture-attempts/'+delivery+'/'+message.attempts,JSON.stringify({attempts:message.attempts,at:Date.now()})):Promise.resolve();
  })).catch(()=>{}));
  return worker.queue(batch,await prepare(env,ctx));
 }
};
