import { Env, constantEqual, digest } from './protocol';
import { capacity, timedBucket } from './monitoring';

const key='control/neon-provider.json';
const computeKey='control/neon-compute.json';
const FUTURE_SKEW=60000;
const ERRORS=new Set(['credential_unavailable','provider_authentication_failed','provider_rate_limited','provider_http_unavailable','provider_response_malformed','provider_identity_mismatch','provider_usage_unavailable','monitoring_payload_too_large']);
export function providerIdentity(env:Env):{project:string,branch:string,endpoint:string,cpu_required:boolean}|null {
  if(!env.NEON_MONITORING)return null;
  const value=JSON.parse(env.NEON_MONITORING);
  if(![['project',''],['branch','br-'],['endpoint','ep-']].every(([name,prefix])=>typeof value[name]==='string' && /^[a-z0-9-]{1,100}$/.test(value[name]) && value[name].startsWith(prefix)))throw Error('invalid_neon_monitoring_identity');
  if(value.cpu_required!==undefined && typeof value.cpu_required!=='boolean')throw Error('invalid_neon_monitoring_identity');
  return {project:value.project,branch:value.branch,endpoint:value.endpoint,cpu_required:value.cpu_required===true};
}
const natural=(value:any)=>Number.isSafeInteger(value) && value>=0;
const same=(a:any,b:any)=>a?.project===b.project && a?.branch===b.branch && a?.endpoint===b.endpoint;
export function providerCapacity(sample:{at:number,bytes:number,allowance:number|null},baseline:any,now:number,prior?:any):any {
  const unchanged=prior?.bytes===sample.bytes && prior?.allowance===sample.allowance;
  const measured=capacity(sample,baseline,now,unchanged?{...prior,bytes:undefined}:prior);
  if(unchanged) {
    // A cached estimate neither confirms growth again nor breaks a positive
    // forecast's consecutive confirmations. A genuinely flat hour still clears it.
    measured.forecast_breaches=measured.forecast_level==='ok' || measured.forecast_level!==prior.forecast_level?0:prior.forecast_breaches || 0;
    measured.forecast_confirmed=measured.forecast_breaches>=3;
    const ratio=sample.allowance?sample.bytes/sample.allowance:0;
    measured.level=ratio>=.85 || measured.forecast_confirmed && measured.forecast_level==='critical'?'critical':ratio>=.7 || measured.forecast_confirmed && measured.forecast_level==='warning'?'warning':'ok';
  }
  return measured;
}
export async function authorizeCompute(req:Request,env:Env):Promise<boolean> {
  const token=req.headers.get('Authorization')?.match(/^Bearer (.+)$/)?.[1];
  return Boolean(token && env.MONITORING_INGEST_TOKEN_HASH && env.MONITORING_INGEST_TOKEN_HASH!==env.ADMIN_TOKEN_HASH && constantEqual(await digest(token),env.MONITORING_INGEST_TOKEN_HASH));
}
export async function boundedText(body:ReadableStream<Uint8Array>|null,limit:number):Promise<string> {
  if(!body)return '';
  const reader=body.getReader(),chunks:Uint8Array[]=[];let size=0;
  try {for(;;){const {done,value}=await reader.read();if(done)break;size+=value.byteLength;if(size>limit){await reader.cancel();throw Error('monitoring_payload_too_large');}chunks.push(value);}}
  finally {reader.releaseLock();}
  const bytes=new Uint8Array(size);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.byteLength;}
  return new TextDecoder().decode(bytes);
}

// One cron owner, independently of repository SQL and ingestion. The management
// credential remains separate from database credentials and is never returned.
export async function prepareProvider(env:Env):Promise<void> {
  const identity=providerIdentity(env);if(!identity)return;
  // Unknown checkpoint state must not overwrite last-good diagnostics/history.
  const archive=timedBucket(env.ARCHIVE),old:any=await archive.get(key).then(o=>o?.json()),now=Date.now();
  if(same(old,identity) && natural(old.last_attempt) && now>=old.last_attempt && now-old.last_attempt<50000)return;
  try {
    if(!env.NEON_MONITORING_API_KEY)throw Error('credential_unavailable');
    const read=async(path:string)=>{
      const r=await fetch('https://console.neon.tech/api/v2/projects/'+identity.project+path,{headers:{Authorization:'Bearer '+env.NEON_MONITORING_API_KEY},redirect:'manual',signal:AbortSignal.timeout(2000)});
      if(!r.ok)throw Error([401,403].includes(r.status)?'provider_authentication_failed':r.status===429?'provider_rate_limited':'provider_http_unavailable');
      const text=await boundedText(r.body,1024*1024);
      try {return JSON.parse(text);}catch {throw Error('provider_response_malformed');}
    };
    const [projectResponse,branchResponse,endpointResponse,inventory]=await Promise.all([read(''),read('/branches/'+identity.branch),read('/endpoints/'+identity.endpoint),read('/branches?limit=100').catch(()=>null)]);
    const project=projectResponse?.project,branch=branchResponse?.branch,endpoint=endpointResponse?.endpoint;
    if(project?.id!==identity.project || branch?.id!==identity.branch || endpoint?.id!==identity.endpoint || endpoint.branch_id!==identity.branch)throw Error('provider_identity_mismatch');
    if(!natural(branch.logical_size))throw Error('provider_usage_unavailable');
    const limits=[project.settings?.quota?.logical_size_bytes,project.branch_logical_size_limit_bytes];
    const limitsValid=limits.every(v=>v===undefined || v===null || natural(v));
    const positive=limits.filter(v=>natural(v) && v>0),limit=limitsValid && positive.length?Math.min(...positive):null;
    const history=(same(old,identity) && Array.isArray(old.history)?old.history:[]).filter((s:any)=>natural(s.at) && natural(s.bytes) && s.at<=now && s.at>=now-2*3600000).slice(-120);
    const baseline=history.filter((s:any)=>s.at<=now-3600000).at(-1);
    const measured=providerCapacity({at:now,bytes:branch.logical_size,allowance:limit},baseline,now,same(old,identity)?old.capacity:undefined);
    if(limit===null && same(old,identity) && ['critical','warning'].includes(old.capacity?.level)) {measured.level=old.capacity.level;measured.last_known_allowance=old.capacity.allowance ?? old.capacity.last_known_allowance ?? null;measured.last_known_pressure=true;}
    history.push({at:now,bytes:branch.logical_size,allowance:limit || null});
    // logical_size_bytes is a PER-BRANCH quota, not an account/project ceiling.
    // The branch sum counts shared logical data repeatedly and is not billed size.
    const inventoryAvailable=Array.isArray(inventory?.branches),previous=same(old,identity)?old:null;
    const raw=inventoryAvailable?inventory.branches:[];
    const branches=raw.slice(0,100).filter((b:any)=>typeof b?.id==='string' && natural(b.logical_size));
    const complete=Boolean(inventory) && Array.isArray(inventory.branches) && raw.length<100 && branches.length===raw.length && new Set(branches.map((b:any)=>b.id)).size===branches.length && !inventory.pagination?.cursor && !inventory.pagination?.next;
    const sum=branches.reduce((n:number,b:any)=>n+b.logical_size,0);
    const value={version:1,...identity,at:now,last_attempt:now,error:null,history,
      capacity:{...measured,backend:'postgres',growable:true,allowance_kind:'neon_branch_logical_size_quota',usage_scope:'selected_branch_all_databases',allowance_scope:'branch',measurement:'provider_logical_size_estimate',retrieved_at:now,provider_sample_at:null,provider_refresh_lag_unknown:true},
      branch_limits:{configured_quota_bytes:natural(limits[0])?limits[0] || null:null,provider_limit_bytes:natural(limits[1])?limits[1] || null:null,effective_bytes:limit,error:limitsValid?null:'provider_quota_unavailable'},
      branches:inventoryAvailable?branches.map((b:any)=>({id:b.id,logical_bytes:b.logical_size})):previous?.branches ?? null,
      branch_inventory_complete:inventoryAvailable?complete:previous?.branch_inventory_complete ?? false,
      branch_inventory_at:inventoryAvailable?now:previous?.branch_inventory_at ?? null,branch_inventory_stale:!inventoryAvailable,
      project_logical_branch_sum_bytes:inventoryAvailable?complete && natural(sum)?sum:null:previous?.project_logical_branch_sum_bytes ?? null,project_sum_includes_shared_copies:true,
      project_physical_bytes:null,account_storage_allowance_bytes:null,account_usage_bytes:null,
      compute:{state:endpoint.current_state,min_cu:endpoint.autoscaling_limit_min_cu,max_cu:endpoint.autoscaling_limit_max_cu},
      source:'neon_management_api'};
    await archive.put(key,JSON.stringify(value));
  }catch(error) {
    const reason=error instanceof Error && ERRORS.has(error.message)?error.message:error instanceof Error && ['AbortError','TimeoutError'].includes(error.name)?'provider_timeout':'provider_unavailable';
    await archive.put(key,JSON.stringify({...(same(old,identity)?old:identity),last_attempt:now,error:reason}));
  }
}

// Adapter for the already configured observability integration. Do not infer
// CPU utilization from billed CU-seconds or an endpoint's configured CU limit.
export async function saveCompute(env:Env,input:any,now=Date.now()):Promise<void> {
  let identity:ReturnType<typeof providerIdentity>;
  try {identity=providerIdentity(env);}catch {throw Error('monitoring_configuration_unavailable');}
  if(!identity)throw Error('monitoring_configuration_unavailable');
  if(!same(input,identity) || !natural(input.at) || input.at>now+FUTURE_SKEW || now-input.at>180000 || !Number.isFinite(input.cpu_utilization) || input.cpu_utilization<0 || input.cpu_utilization>1)throw Error('invalid_compute_observation');
  const archive=timedBucket(env.ARCHIVE),prior=await archive.get(computeKey),old:any=await prior?.json();
  if(same(old,identity) && old.at>=input.at)throw Error('stale_compute_observation');
  const written=await archive.put(computeKey,JSON.stringify({...identity,at:input.at,cpu_utilization:input.cpu_utilization,source:'configured_observability_integration'}),{onlyIf:prior?{etagMatches:prior.etag}:{etagDoesNotMatch:'*'}});
  if(!written)throw Error('compute_observation_conflict');
}

export async function providerObservation(env:Env,now=Date.now(),verifyDatabase=false):Promise<any> {
  let identity:ReturnType<typeof providerIdentity>;
  try {identity=providerIdentity(env);}catch {return {configured:true,stale:true,error:'invalid_neon_monitoring_identity',compute:{cpu_utilization:null,stale:true,reason:'invalid_neon_monitoring_identity'}};}
  if(!identity)return {configured:false,stale:true,compute:{cpu_utilization:null,stale:true,reason:'provider_monitoring_not_configured'}};
  if(verifyDatabase) {
    let matches=false;
    try {matches=new URL(env.REPOSITORY_DATABASE_URL || '').hostname.split('.')[0].replace(/-pooler$/,'')===identity.endpoint;}catch {}
    if(!matches)return {...identity,configured:true,stale:true,error:'provider_database_identity_mismatch',compute:{cpu_utilization:null,stale:true,reason:'provider_database_identity_mismatch'}};
  }
  const archive=timedBucket(env.ARCHIVE);
  const [value,compute]=await Promise.all([archive.get(key).then(o=>o?.json<any>()).catch(()=>null),archive.get(computeKey).then(o=>o?.json<any>()).catch(()=>null)]);
  now=Math.max(now,Date.now());
  const valid=same(value,identity),fresh=valid && natural(value.at) && value.at<=now+1000 && now-value.at<=180000;
  const cpuValid=same(compute,identity) && natural(compute.at) && compute.at<=now+FUTURE_SKEW && now-compute.at<=180000 && Number.isFinite(compute.cpu_utilization) && compute.cpu_utilization>=0 && compute.cpu_utilization<=1;
  const {history,...publicValue}=valid?value:{};
  return {...publicValue,...identity,configured:true,stale:!fresh,
    ...(publicValue.capacity?{capacity:{...publicValue.capacity,stale:!fresh}}:{}),
    compute:{...(valid?value.compute:{}),at:cpuValid?compute.at:null,cpu_utilization:cpuValid?compute.cpu_utilization:null,stale:!cpuValid,reason:cpuValid?null:'compute_observation_unavailable'}};
}

export function ageProvider(value:any,now=Date.now()):any {
  if(!value?.configured)return value;
  const stale=Boolean(value.stale) || !natural(value.at) || value.at>now+1000 || now-value.at>180000;
  const cpuStale=Boolean(value.compute?.stale) || !natural(value.compute?.at) || value.compute.at>now+FUTURE_SKEW || now-value.compute.at>180000;
  return {...value,stale,branch_inventory_stale:Boolean(value.branch_inventory_stale) || !natural(value.branch_inventory_at) || now-value.branch_inventory_at>180000,...(value.capacity?{capacity:{...value.capacity,stale}}:{}),compute:{...value.compute,stale:cpuStale,cpu_utilization:cpuStale?null:value.compute.cpu_utilization,reason:cpuStale?'compute_observation_unavailable':value.compute.reason}};
}

export function providerAlarms(value:any):{level:string,reasons:string[]} {
  if(!value.configured)return {level:'ok',reasons:[]};
  const reasons:string[]=[];
  if(value.stale)reasons.push('provider_monitoring_unavailable');
  if(value.capacity?.level && value.capacity.level!=='ok')reasons.push('branch_storage_capacity');
  if(value.cpu_required && value.compute?.stale)reasons.push('compute_monitoring_unavailable');
  const cpu=value.compute?.stale?null:value.compute?.cpu_utilization;
  if(cpu!==null && cpu>=.8)reasons.push('compute_saturation');
  return {level:value.capacity?.level==='critical' || cpu!==null && cpu>=.9?'critical':reasons.length?'warning':'ok',reasons};
}
