import { Env } from './protocol';
import { capacity, timedBucket } from './monitoring';

const key='control/neon-provider.json';
const computeKey='control/neon-compute.json';
export function providerIdentity(env:Env):{project:string,branch:string,endpoint:string}|null {
  if(!env.NEON_MONITORING)return null;
  const value=JSON.parse(env.NEON_MONITORING);
  if(![['project',''],['branch','br-'],['endpoint','ep-']].every(([name,prefix])=>typeof value[name]==='string' && /^[a-z0-9-]{1,100}$/.test(value[name]) && value[name].startsWith(prefix)))throw Error('invalid_neon_monitoring_identity');
  return {project:value.project,branch:value.branch,endpoint:value.endpoint};
}
const natural=(value:any)=>Number.isSafeInteger(value) && value>=0;
const same=(a:any,b:any)=>a?.project===b.project && a?.branch===b.branch && a?.endpoint===b.endpoint;
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
  const archive=timedBucket(env.ARCHIVE),old:any=await archive.get(key).then(o=>o?.json()).catch(()=>null),now=Date.now();
  if(same(old,identity) && natural(old.last_attempt) && now>=old.last_attempt && now-old.last_attempt<50000)return;
  try {
    if(!env.NEON_MONITORING_API_KEY)throw Error('credential_unavailable');
    const read=async(path:string)=>{
      const r=await fetch('https://console.neon.tech/api/v2/projects/'+identity.project+path,{headers:{Authorization:'Bearer '+env.NEON_MONITORING_API_KEY},signal:AbortSignal.timeout(2000)});
      if(!r.ok)throw Error('provider_unavailable');
      const text=await boundedText(r.body,1024*1024);
      return JSON.parse(text);
    };
    const [projectResponse,branchResponse,endpointResponse]=await Promise.all([read(''),read('/branches?limit=100'),read('/endpoints/'+identity.endpoint)]);
    const project=projectResponse?.project,branches=branchResponse?.branches,endpoint=endpointResponse?.endpoint;
    if(project?.id!==identity.project || !Array.isArray(branches) || branches.length>100 || !branches.every((b:any)=>typeof b.id==='string' && natural(b.logical_size)) || new Set(branches.map((b:any)=>b.id)).size!==branches.length || endpoint?.id!==identity.endpoint || endpoint.branch_id!==identity.branch)throw Error('provider_identity_or_shape');
    const branch=branches.find((b:any)=>b.id===identity.branch);if(!branch)throw Error('provider_branch_missing');
    const limit=project.settings?.quota?.logical_size_bytes;
    if(limit!==undefined && !natural(limit))throw Error('provider_quota_malformed');
    const history=(same(old,identity) && Array.isArray(old.history)?old.history:[]).filter((s:any)=>natural(s.at) && natural(s.bytes) && s.at<=now && s.at>=now-2*3600000).slice(-120);
    const baseline=history.filter((s:any)=>s.at<=now-3600000).at(-1);
    const measured=capacity({at:now,bytes:branch.logical_size,allowance:limit || null},baseline,now,same(old,identity)?old.capacity:undefined);
    history.push({at:now,bytes:branch.logical_size,allowance:limit || null});
    // logical_size_bytes is a PER-BRANCH quota, not an account/project ceiling.
    // The branch sum counts shared logical data repeatedly and is not billed size.
    const complete=branches.length<100 && !branchResponse.pagination?.cursor && !branchResponse.pagination?.next;
    const sum=branches.reduce((n:number,b:any)=>n+b.logical_size,0);
    const value={version:1,...identity,at:now,last_attempt:now,error:null,history,
      capacity:{...measured,backend:'postgres',growable:true,allowance_kind:'neon_branch_logical_size_quota',usage_scope:'selected_branch_all_databases',allowance_scope:'branch',measurement:'provider_logical_size_estimate',retrieved_at:now,provider_sample_at:null,provider_refresh_lag_unknown:true},
      branches:branches.map((b:any)=>({id:b.id,logical_bytes:b.logical_size})),branch_inventory_complete:complete,
      project_logical_branch_sum_bytes:complete && natural(sum)?sum:null,project_sum_includes_shared_copies:true,
      project_physical_bytes:null,account_storage_allowance_bytes:null,account_usage_bytes:null,
      compute:{state:endpoint.current_state,min_cu:endpoint.autoscaling_limit_min_cu,max_cu:endpoint.autoscaling_limit_max_cu},
      source:'neon_management_api'};
    await archive.put(key,JSON.stringify(value));
  }catch {
    await archive.put(key,JSON.stringify({...(same(old,identity)?old:identity),last_attempt:now,error:'provider_unavailable'}));
  }
}

// Adapter for the already configured observability integration. Do not infer
// CPU utilization from billed CU-seconds or an endpoint's configured CU limit.
export async function saveCompute(env:Env,input:any,now=Date.now()):Promise<void> {
  const identity=providerIdentity(env);
  if(!identity || !same(input,identity) || !natural(input.at) || input.at>now+1000 || now-input.at>180000 || !Number.isFinite(input.cpu_utilization) || input.cpu_utilization<0 || input.cpu_utilization>1)throw Error('invalid_compute_observation');
  const archive=timedBucket(env.ARCHIVE),prior=await archive.get(computeKey),old:any=await prior?.json();
  if(same(old,identity) && old.at>=input.at)throw Error('stale_compute_observation');
  const written=await archive.put(computeKey,JSON.stringify({...identity,at:input.at,cpu_utilization:input.cpu_utilization,source:'configured_observability_integration'}),{onlyIf:prior?{etagMatches:prior.etag}:{etagDoesNotMatch:'*'}});
  if(!written)throw Error('stale_compute_observation');
}

export async function providerObservation(env:Env,now=Date.now()):Promise<any> {
  let identity:ReturnType<typeof providerIdentity>;
  try {identity=providerIdentity(env);}catch {return {configured:true,stale:true,error:'invalid_neon_monitoring_identity',compute:{cpu_utilization:null,stale:true,reason:'invalid_neon_monitoring_identity'}};}
  if(!identity)return {configured:false,stale:true,compute:{cpu_utilization:null,stale:true,reason:'provider_monitoring_not_configured'}};
  const archive=timedBucket(env.ARCHIVE);
  const [value,compute]=await Promise.all([archive.get(key).then(o=>o?.json<any>()).catch(()=>null),archive.get(computeKey).then(o=>o?.json<any>()).catch(()=>null)]);
  const valid=same(value,identity),fresh=valid && natural(value.at) && value.at<=now+1000 && now-value.at<=180000 && !value.error;
  const cpuValid=same(compute,identity) && natural(compute.at) && compute.at<=now+1000 && now-compute.at<=180000 && Number.isFinite(compute.cpu_utilization) && compute.cpu_utilization>=0 && compute.cpu_utilization<=1;
  const {history,...publicValue}=valid?value:{};
  return {...publicValue,...identity,configured:true,stale:!fresh,
    compute:{...(valid?value.compute:{}),at:cpuValid?compute.at:null,cpu_utilization:cpuValid?compute.cpu_utilization:null,stale:!cpuValid,reason:cpuValid?null:'compute_observation_unavailable'}};
}

export function providerAlarms(value:any):{level:string,reasons:string[]} {
  if(!value.configured)return {level:'ok',reasons:[]};
  const reasons:string[]=[];
  if(value.stale)reasons.push('provider_monitoring_unavailable');
  if(!value.stale && value.capacity?.capacity_unknown)reasons.push('branch_storage_limit_unknown');
  if(!value.stale && value.capacity?.level && value.capacity.level!=='ok')reasons.push('branch_storage_capacity');
  if(value.compute?.stale)reasons.push('compute_monitoring_unavailable');
  const cpu=value.compute?.stale?null:value.compute?.cpu_utilization;
  if(cpu!==null && cpu>=.8)reasons.push('compute_saturation');
  return {level:!value.stale && value.capacity?.level==='critical' || cpu!==null && cpu>=.9?'critical':reasons.length?'warning':'ok',reasons};
}
