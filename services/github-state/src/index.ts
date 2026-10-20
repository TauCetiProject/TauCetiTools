import { Env, EventPointer, Query, Repository, authorize, digest, json, repositories, repository, stub, validSignature, validateQuery, parseGitHub, CI_EVENTS } from './protocol';
import { readMigration } from './migration';
import { classify, log, requestId } from './storage';
import { PARK_AFTER_ATTEMPTS, backlog, backoff, drain, maintainIngress, maintainRaw, release, ingressMode, noteRepositoryFull, park, reconcile, setIngressMode, storageState } from './ingress';
import { R2_CLASSES, policy, retirePrefix } from './retention';
export { RepositoryState } from './repository';
export { InstallationScheduler } from './installation';

// The repositories that should receive one signed delivery.
function targets(env:Env,payload:any,event:string): Repository[] {
  const configured=repositories(env);
  const target=payload?.repository ? configured.filter(r=>r.id===String(payload.repository.id)) : ['installation','installation_repositories','ping'].includes(event)?configured:[];
  return target.filter(repo=>!payload?.installation?.id || String(payload.installation.id)===repo.installation);
}
const STORAGE_OPERATIONS=new Set(['scan','cleanup','probe','fault','retention']);
async function query(req:Request,env:Env,q:Query,request_id:string): Promise<Response> {
  try { validateQuery(q); } catch { return json({error:'invalid_query'},400); }
  const repo=repository(env,q.repository);
  if (!await authorize(req,env)) return json({error:'unauthorized'},401);
  if (!repo || !await authorize(req,env,repo.name)) return json({version:1,repository:q.repository,operation:q.operation,status:'unavailable',revision:0,complete:false,reason:'repository_unavailable'});
  let response:Response;const started=Date.now();
  try {response=await stub(env.REPOSITORIES,repo.id).fetch('https://internal/query',{method:'POST',headers:{'If-None-Match':req.headers.get('If-None-Match') || '','x-request-id':request_id},body:JSON.stringify({...q,repository:repo.name})});}
  catch(error) {
    const reason=classify(error);
    if (reason==='storage_full') await noteRepositoryFull(env,repo.id,'query',started).catch(()=>{});
    log('query_failed',{request_id,operation:q.operation,reason});
    return json({error:'storage_unavailable',reason,request_id},503);
  }
  if (response.status===304) return response;
  if (response.status!==200) {
    // Keep the established error code; add the precise cause and request ID.
    const detail:any=await response.json().catch(()=>({}));
    if (detail.error==='storage_full') await noteRepositoryFull(env,repo.id,'query',started).catch(()=>{});
    log('query_failed',{request_id,operation:q.operation,status:response.status,reason:detail.error});
    return json({error:'storage_unavailable',reason:typeof detail.error==='string'?detail.error:'status_'+response.status,request_id},503);
  }
  const etag=response.headers.get('etag');
  if (etag && req.headers.get('if-none-match')===etag && !q.cursor) {
    const copy:any=await response.clone().json();
    if (copy.status==='ok') return new Response(null,{status:304,headers:{ETag:etag,'Cache-Control':'private, no-cache'}});
  }
  return response;
}
export default {
  async fetch(req:Request,env:Env,ctx:ExecutionContext):Promise<Response> {
    const url=new URL(req.url),request_id=requestId(req);
    try {
      if (url.pathname==='/healthz' && req.method==='GET') return json({service:'tauceti-github-state',version:1});
      if (url.pathname==='/setup/register' && req.method==='GET') {
        const suffix=url.hostname.includes('github-state-test.')?'-test':'';
        const manifest={name:'tauceti-github-state'+suffix,url:url.origin,hook_attributes:{url:url.origin+'/webhook/github',active:true},redirect_url:url.origin+'/registration-complete',public:false,description:'Exclusive read-only webhook mirror for TauCeti fleet queries.',default_permissions:{metadata:'read',contents:'read',pull_requests:'read',issues:'read',checks:'read',statuses:'read',actions:'read',merge_queues:'read'},default_events:['pull_request','pull_request_review','pull_request_review_comment','pull_request_review_thread','issues','issue_comment','label','milestone','push','create','delete','repository','status','merge_group']};
        const escaped=JSON.stringify(manifest).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');
        return new Response(`<!doctype html><meta name="referrer" content="no-referrer"><title>Register TauCeti GitHub state App</title><h1>Register the dedicated read-only GitHub App</h1><p>This creates ${manifest.name}, owned by TauCetiProject. Install it only on TauCeti. It cannot write to GitHub.</p><form method="post" action="https://github.com/organizations/TauCetiProject/settings/apps/new"><input type="hidden" name="manifest" value="${escaped}"><button>Register ${manifest.name}</button></form>`,{headers:{'Content-Type':'text/html; charset=utf-8','Referrer-Policy':'no-referrer','Cache-Control':'no-store'}});
      }
      if (url.pathname==='/registration-complete' && req.method==='GET') return new Response('GitHub App registration completed. Give the one-time code from this URL to the setup helper, then install the App only on TauCeti.',{headers:{'Content-Type':'text/plain','Referrer-Policy':'no-referrer','Cache-Control':'no-store'}});
      if (url.pathname==='/webhook/github' && req.method==='POST') {
        if (Number(req.headers.get('content-length') || 0)>26*1024*1024) return json({error:'payload_too_large'},413);
        const body=await req.arrayBuffer();
        if (body.byteLength>26*1024*1024) return json({error:'payload_too_large'},413);
        if (!await validSignature(body,req.headers.get('x-hub-signature-256'),env.WEBHOOK_SECRET)) return json({error:'invalid_signature'},401);
        const delivery=req.headers.get('x-github-delivery'),event=req.headers.get('x-github-event');
        if (!delivery || !/^[a-zA-Z0-9-]{1,100}$/.test(delivery) || !event || !/^[a-z_]{1,100}$/.test(event)) return json({error:'invalid_delivery'},400);
        // Existing installations still send CI events; acknowledge without storing.
        if (CI_EVENTS.has(event)) return json({ignored:true,reason:'not_mirrored'},202);
        let payload:any; try { payload=parseGitHub(new TextDecoder().decode(body)); } catch { return json({error:'invalid_json'},400); }
        const target=targets(env,payload,event);
        if (!target.length) return json({ignored:true},202);
        const received=Date.now(),key=`webhooks/${new Date(received).toISOString().slice(0,10)}/${delivery}.json`;
        // R2 overwrite on duplicate is harmless only for the same signed bytes.
        const hash=await digest(body),prior=await env.ARCHIVE.head(key);
        if (prior?.customMetadata?.sha256 && prior.customMetadata.sha256!==hash) return json({error:'delivery_conflict'},409);
        await env.ARCHIVE.put(key,body,{customMetadata:{sha256:hash,event,targets:JSON.stringify(target.map(r=>r.id)),received:String(received),expires:String(received+14*86400000)}});
        for (const repo of target) {
          const pointer={key,delivery,event,repository:repo.name,received};
          await park(env,repo,pointer,'accepted');
          await env.EVENTS.send(pointer);
        }
        return json({accepted:true},202);
      }
      // Reporting never enters query(), noteDemand(), configure(), or the scheduler.
      if (url.pathname==='/v1/activity' && req.method==='POST') {
        if (!await authorize(req,env)) return json({error:'unauthorized'},401);
        if (Number(req.headers.get('content-length') || 0)>16384) return json({error:'payload_too_large'},413);
        const text=await req.text();if(text.length>16384)return json({error:'payload_too_large'},413);
        let input:any;try {input=JSON.parse(text);}catch{return json({error:'invalid_json'},400);}
        if(!input || typeof input!=='object' || Array.isArray(input))return json({error:'invalid_request'},400);
        const repo=repository(env,input.repository);
        if (!repo || !await authorize(req,env,repo.name)) return json({error:'repository_unavailable'},403);
        if (!Array.isArray(input.numbers) || input.numbers.length>500 || input.numbers.some((n:any)=>!Number.isSafeInteger(n) || n<=0)) return json({error:'invalid_numbers'},400);
        return stub(env.REPOSITORIES,repo.id).fetch('https://internal/activity',{method:'POST',body:JSON.stringify({repository:repo.name,numbers:[...new Set(input.numbers)]})});
      }
      if (url.pathname==='/v1/query' && req.method==='GET') {
        let args; try { args=JSON.parse(url.searchParams.get('args') || '{}'); } catch { return json({error:'invalid_args'},400); }
        if (url.searchParams.has('allow_observed') && !['true','false'].includes(url.searchParams.get('allow_observed')!)) return json({error:'invalid_query'},400);
        return await query(req,env,{repository:url.searchParams.get('repository') || '',operation:url.searchParams.get('operation') || '',args,cursor:url.searchParams.get('cursor') || undefined,allow_observed:url.searchParams.has('allow_observed')?url.searchParams.get('allow_observed')==='true':undefined},request_id);
      }
      if (url.pathname==='/v1/query_batch' && req.method==='POST') {
        const input:any=await req.json();
        if (!Array.isArray(input.queries) || input.queries.length>50) return json({error:'invalid_batch'},400);
        const results=[];
        for (const q of input.queries) { const response=await query(req,env,q,request_id); results.push({status:response.status,result:response.status===304?null:await response.json()}); }
        return json({version:1,results});
      }
      if (['/v1/health','/v1/changes'].includes(url.pathname) && req.method==='GET') {
        const repo=repository(env,url.searchParams.get('repository') || '');
        if (!repo || !await authorize(req,env,repo.name)) return json({error:'unauthorized'},401);
        const data=await stub(env.REPOSITORIES,repo.id).fetch('https://internal/' + (url.pathname.endsWith('health')?'health':'changes?after=' + encodeURIComponent(url.searchParams.get('after') || '0')));
        if (!url.pathname.endsWith('health')) return data;
        const quota=await stub(env.INSTALLATIONS,repo.installation).fetch('https://internal/health');
        return json({state:await data.json(),quota:await quota.json(),recovery:await backlog(env,repo,true).catch(()=>({error:'recovery_status_unavailable'}))});
      }
      if (url.pathname==='/admin/bootstrap' && req.method==='POST') {
        if (!await authorize(req,env,undefined,true)) return json({error:'unauthorized'},401);
        const input:any=await req.json(),repo=repository(env,input.repository);
        if (!repo) return json({error:'repository_not_allowed'},400);
        return stub(env.REPOSITORIES,repo.id).fetch('https://internal/configure',{method:'POST',body:JSON.stringify({repository:repo.name})});
      }
      if (url.pathname==='/admin/migration' && req.method==='POST') {
        if (!await authorize(req,env,undefined,true)) return json({error:'unauthorized'},401);
        const input:any=await req.json(),repo=repository(env,input?.repository || '');
        if(!repo) return json({error:'repository_not_allowed'},400);
        return stub(env.REPOSITORIES,repo.id).fetch('https://internal/migration',{method:'POST',body:JSON.stringify(input)});
      }
      // Bounded storage diagnostics and recovery. No arbitrary SQL or proxying.
      if (url.pathname==='/admin/storage' || url.pathname.startsWith('/admin/storage/') || url.pathname.startsWith('/admin/ingress')) {
        if (!await authorize(req,env,undefined,true)) return json({error:'unauthorized'},401);
        const input:any=req.method==='POST'?await req.json():{repository:url.searchParams.get('repository')};
        const repo=repository(env,input?.repository || '');
        if (!repo) return json({error:'repository_not_allowed'},400);
        const forward=async(path:string,body?:unknown)=>{
          const started=Date.now();
          try {return await stub(env.REPOSITORIES,repo.id).fetch('https://internal'+path,body===undefined?{headers:{'x-request-id':request_id}}:{method:'POST',headers:{'x-request-id':request_id},body:JSON.stringify(body)});}
          catch(error) {
            const reason=classify(error);
            if (reason==='storage_full') await noteRepositoryFull(env,repo.id,'admin',started).catch(()=>{});
            return json({error:'storage_unavailable',reason,request_id},503);
          }
        };
        if (url.pathname==='/admin/storage' && req.method==='GET') return forward('/storage/summary');
        const operation=url.pathname.slice('/admin/storage/'.length);
        if (url.pathname.startsWith('/admin/storage/') && req.method==='POST' && STORAGE_OPERATIONS.has(operation)) return forward('/storage/'+operation,{...input,repository_id:repo.id});
        try {
          if (url.pathname==='/admin/ingress' && req.method==='GET') return json(await backlog(env,repo));
          if (url.pathname==='/admin/ingress' && req.method==='POST') {
            // Consumers see the R2 mode; the repository object then refuses new
            // applications and reports once in-flight ones have finished.
            const mode=await setIngressMode(env,repo,input.mode,input.reason);
            const barrier=await forward('/storage/ingress',{mode:input.mode,repository_id:repo.id});
            return json({...mode,repository:await barrier.json()},barrier.status);
          }
          if (url.pathname==='/admin/ingress/replay' && req.method==='POST') return json(await drain(env,repo,{limit:input.limit,held:input.held===true}));
          if (url.pathname==='/admin/ingress/reconcile' && req.method==='POST') return json(await reconcile(env,repo,input,(payload,event)=>targets(env,payload,event)));
        } catch(error) {return json({error:error instanceof Error?error.message.slice(0,200):'invalid_request',request_id},400);}
        return json({error:'not_found'},404);
      }
      if (url.pathname==='/admin/reference' && req.method==='POST') {
        if (env.REFERENCE_READS!=='true' || !await authorize(req,env,undefined,true)) return json({error:'unauthorized'},401);
        const input:any=await req.json();
        try {validateQuery(['pr_delta','issue_delta'].includes(input.query?.operation)?{...input.query,operation:'open_prs'}:input.query);} catch {return json({error:'invalid_query'},400);}
        const repo=repository(env,input.query.repository);
        if (!repo || input.api && !['core','graphql'].includes(input.api) || input.page && (!Number.isInteger(input.page) || input.page<1)) return json({error:'invalid_query'},400);
        if (input.since && (input.query.operation!=='workflow_runs' || !Number.isFinite(Date.parse(input.since)))) return json({error:'invalid_query'},400);
        const referenceQuery=input.since?{...input.query,args:{...input.query.args,after:input.since}}:input.query;
        if(input.jobs) {
          if(!Array.isArray(input.jobs) || !input.jobs.length || input.jobs.length>100) return json({error:'invalid_batch'},400);
          try {for(const job of input.jobs) {validateQuery({...job.query,repository:repo.name});if(job.query.operation!==input.query.operation) throw new Error('mixed batch');}} catch {return json({error:'invalid_batch'},400);}
          return stub(env.INSTALLATIONS,repo.installation).fetch('https://internal/measure',{method:'POST',headers:{'x-request-id':request_id},body:JSON.stringify({repository:repo,jobs:input.jobs,profile:input.profile})});
        }
        // The quota owner performs an uncached, catalog-only reference read. No arbitrary proxy.
        return stub(env.INSTALLATIONS,repo.installation).fetch('https://internal/read',{method:'POST',headers:{'x-request-id':request_id},body:JSON.stringify({repository:repo,query:referenceQuery,page:input.page || 1,cursor:input.cursor,api:input.api,nested:input.nested,reference:true})});
      }
      return json({error:'not_found'},404);
    } catch(error) {
      const reason=classify(error);
      log('request_failed',{request_id,path:url.pathname,reason});
      return json({error:'service_unavailable',reason,request_id},503);
    }
  },
  async queue(batch:MessageBatch<EventPointer>,env:Env):Promise<void> {
    const modes=new Map<string,Promise<{mode:string}>>(),stored=new Map<string,Promise<any>>();
    // Messages are independent: each is acknowledged only after its own
    // receipt or durable parking, so a batch is handled concurrently.
    await Promise.all(batch.messages.map(async message=>{
      const pointer=message.body,repo=repository(env,pointer.repository);
      if (!repo) { message.ack(); return; }
      let reason:string,started=Date.now();
      try {
        if (!modes.has(repo.id)) modes.set(repo.id,ingressMode(env,repo));
        if (!stored.has(repo.id)) stored.set(repo.id,storageState(env,repo).catch(()=>null));
        if ((await modes.get(repo.id)!).mode==='parked') reason='planned_migration';
        // A recorded full repository cannot apply events; park without a round trip.
        else if ((await stored.get(repo.id))?.state==='full') reason='storage_full';
        else {
          started=Date.now();
          const result=await stub(env.REPOSITORIES,repo.id).fetch('https://internal/event',{method:'POST',body:JSON.stringify(pointer)});
          if (result.ok) {
            await release(env,repo,pointer).catch(error=>log('event_cleanup_deferred',{delivery:pointer.delivery,reason:classify(error)}));
            message.ack(); return;
          }
          const detail:any=await result.json().catch(()=>({}));
          // A full or unavailable repository is parked immediately rather
          // than spending the queue's retry budget.
          if (detail.error==='storage_full') await noteRepositoryFull(env,repo.id,'queue',started);
          if (detail.parkable) reason=String(detail.error);
          else if (message.attempts>=PARK_AFTER_ATTEMPTS) reason='retry_budget:'+String(detail.error || result.status);
          else {
            log('event_retry',{delivery:pointer.delivery,attempts:message.attempts,reason:detail.error || result.status,request_id:detail.request_id});
            message.retry({delaySeconds:backoff(message.attempts)});return;
          }
        }
      } catch(error) {
        // At the size limit the commit fails after the handler returns, so the
        // failure arrives as an exception from the reset object.
        const failure=classify(error);
        if (failure==='storage_full') {
          await noteRepositoryFull(env,repo.id,'queue',started).catch(()=>{});
          reason=failure;
        } else if (message.attempts<PARK_AFTER_ATTEMPTS) {
          log('event_retry',{delivery:pointer.delivery,attempts:message.attempts,reason:failure});
          message.retry({delaySeconds:backoff(message.attempts)});return;
        } else reason='retry_budget:'+failure;
      }
      try {
        await park(env,repo,pointer,reason);
        message.ack();
        log('event_parked',{delivery:pointer.delivery,repository:repo.name,reason});
      } catch(error) {
        log('event_park_failed',{delivery:pointer.delivery,attempts:message.attempts,reason:classify(error)});
        message.retry({delaySeconds:backoff(message.attempts)});
      }
    }));
  },
  async scheduled(_event:ScheduledController,env:Env,ctx:ExecutionContext):Promise<void> {
    const installations=new Set(repositories(env).map(repo=>repo.installation));
    for(const installation of installations)ctx.waitUntil((async()=>{await stub(env.INSTALLATIONS,installation).fetch('https://internal/maintenance',{method:'POST'});})().catch(error=>log('installation_maintenance_failed',{reason:classify(error)})));
    ctx.waitUntil(maintainRaw(env,(payload,event)=>targets(env,payload,event)).catch(error=>log('raw_reconciliation_failed',{reason:classify(error)})));
    for (const repo of repositories(env)) {
      if(repo.bors_receipts_url || (repo.name==='TauCetiProject/TauCeti' && env.BORS_ACTIVITY)) ctx.waitUntil(stub(env.REPOSITORIES,repo.id).fetch('https://internal/activity-refresh',{method:'POST'}));
      ctx.waitUntil((async()=>{
        let tick:Response;const started=Date.now();
        try {tick=await stub(env.REPOSITORIES,repo.id).fetch('https://internal/tick',{method:'POST',body:JSON.stringify({repository:repo.name})});}
        catch(error) {
          if (classify(error)==='storage_full') await noteRepositoryFull(env,repo.id,'tick',started);
          throw error;
        }
        const state:any=await tick.json().catch(()=>({}));
        // The delivery audit spends GitHub quota; skip it while the repository
        // cannot store results or ingestion is deliberately parked.
        if (!tick.ok || state.degraded || (await ingressMode(env,repo)).mode==='parked') {log('delivery_audit_skipped',{repository:repo.name,degraded:Boolean(state.degraded),status:tick.status});return;}
        await stub(env.INSTALLATIONS,repo.installation).fetch('https://internal/audit',{method:'POST',body:JSON.stringify(repo)});
      })().catch(error=>log('scheduled_failed',{repository:repo.name,reason:classify(error)})));
      ctx.waitUntil(maintainIngress(env,repo).catch(error=>log('parked_replay_failed',{repository:repo.name,reason:classify(error)})));
    }
    // Retention is conservatively paused for this bucket while a migration
    // retains its source. Do not expire active cursors, raw input, staging, or
    // old resource versions underneath an export/observation period.
    const retentionHeld=async()=>{
      if(env.MIGRATION_ENVIRONMENT!=='test') return false;
      for(const repo of repositories(env)) {
        const state=await readMigration(env.ARCHIVE,repo.id);
        if(state && state.phase!=='abandoned') return true;
      }
      return false;
    };
    // Separate prefixes prevent a large pinned resource/recovery inventory
    // from starving expiry of short-lived inputs and continuation pages.
    ctx.waitUntil((async()=>{
      if(await retentionHeld())return;
      const held=new Set<string>();
      for(const repo of repositories(env)) {
        const value=await env.ARCHIVE.get('control/'+repo.id+'/retention-policy.json').then(o=>o?.json<any>());
        if(value) {const p=policy(value);for(const name of [...p.disabled,...p.pinned])held.add(name);}
      }
      await Promise.allSettled(Object.keys(R2_CLASSES).map(async name=>{
        if(name==='raw_webhooks' || held.has(name))return; // Recovery owns proof before raw expiry.
        try {await retirePrefix(env.ARCHIVE,name as keyof typeof R2_CLASSES,Date.now(),async()=>{
          if(await retentionHeld())return false;
          // Recheck pins after listing and immediately before issuing deletion.
          // A batch already dispatched to R2 cannot be recalled.
          for(const repo of repositories(env)) {
            const value=await env.ARCHIVE.get('control/'+repo.id+'/retention-policy.json').then(o=>o?.json<any>());
            if(value) {const p=policy(value);if(p.disabled.includes(name) || p.pinned.includes(name))return false;}
          }
          return true;
        });}
        catch(error) {log('archive_retention_failed',{class:name,reason:classify(error)});}
      }));
    })().catch(error=>log('archive_retention_policy_failed',{reason:classify(error)})));

  }
} satisfies ExportedHandler<Env,EventPointer>;
