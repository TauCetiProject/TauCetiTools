// Operator-only faults/restores of an owned disposable child. Never production.
import {readFile,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash,createHmac,randomUUID} from 'node:crypto';
import {Client} from '@neondatabase/serverless';
import {sourceFingerprint} from './source-fingerprint.mjs';
const [directory,tokenFile,mode]=process.argv.slice(2);
if(!directory || !tokenFile || !['restart','restore'].includes(mode))throw Error('Usage: deployed-provider-drill.mjs PRIVATE_STATE_DIR NEON_TOKEN_FILE restart|restore');
const dir=resolve(directory),read=name=>readFile(resolve(dir,name),'utf8'),state=JSON.parse(await read('state.json'));
const provision=JSON.parse(await readFile(new URL('../validation/neon-provisioning-2026-10-20.json',import.meta.url),'utf8'));
const uri=new URL((await read('migration-url')).trim()),service=new URL(state.service_url),project=provision.project.id;
if(!/^issue317-drill-[a-f0-9]{8}$/.test(state.name) || state.production_actions!==false || state.project!==project || !/^[0-9]{1,50}$/.test(state.repository.id) || service.protocol!=='https:' || service.port && service.port!=='443' || service.hostname!==state.worker_hostname || !service.hostname.startsWith(state.name+'.') || !service.hostname.endsWith('.workers.dev') || service.username || service.password || service.search || service.hash || service.pathname!=='/' || !['postgres:','postgresql:'].includes(uri.protocol) || uri.hostname!==state.hostname || !state.hostname.startsWith(state.endpoint+'.') || decodeURIComponent(uri.username)!=='github_state_test_migration' || uri.pathname!=='/github_state_test')throw Error('disposable_identity_required');
const token=(await readFile(tokenFile,'utf8')).trim(),schema='github_state_'+state.repository.id;
const receipt={version:1,mode,run:state.name,...sourceFingerprint(fileURLToPath(new URL('..',import.meta.url))),started_at:new Date().toISOString(),project,source_branch:state.branch,source_endpoint:state.endpoint,schema,production_actions:false,verified:false};
const save=()=>writeFile(resolve(dir,'receipt-provider-'+mode+'.json'),JSON.stringify(receipt,null,2)+'\n',{mode:0o600});
const pause=ms=>new Promise(r=>setTimeout(r,ms));
const api=async(path,method='GET',body)=>{
 const response=await fetch('https://console.neon.tech/api/v2'+path,{method,redirect:'error',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined,signal:AbortSignal.timeout(30000)});
 if(!response.ok)throw Error('provider_api_'+response.status);return response.status===204?{}:response.json();
};
const worker=async(path,body,identity='admin',headers={})=>{
 const request_id=state.name+'-'+randomUUID(),response=await fetch(new URL(path,service),{method:body===undefined?'GET':'POST',redirect:'error',headers:{'Content-Type':'application/json','X-Request-ID':request_id,...(identity?{Authorization:'Bearer '+(await read(identity+'-token')).trim()}:{}),...headers},body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(30000)});
 const value=await response.json();if(!response.ok){receipt.worker_error={status:response.status,error:value.error,request_id:value.request_id || request_id};throw Error('worker_request_failed');}return {status:response.status,value};
};
const connect=async(url=uri)=>{for(let n=0;n<30;n++){const c=new Client({connectionString:url.toString(),connectionTimeoutMillis:5000,query_timeout:15000});c.on('error',()=>{});try{await c.connect();return c;}catch{await c.end().catch(()=>{});await pause(2000);}}throw Error('test_database_unavailable');};
const checksum=rows=>createHash('sha256').update(JSON.stringify(rows.map(row=>Object.fromEntries(Object.keys(row).sort().map(k=>[k,row[k]]))).sort((a,b)=>JSON.stringify(a).localeCompare(JSON.stringify(b))))).digest('hex');
let source,restored,restoreBranch,sentinel,paused=false;
try {
 const parent=(await api(`/projects/${project}/branches/${state.parent}`)).branch,branch=(await api(`/projects/${project}/branches/${state.branch}`)).branch,endpoint=(await api(`/projects/${project}/endpoints/${state.endpoint}`)).endpoint;
 if(!['test','test-s2'].includes(parent.name) || branch.parent_id!==parent.id || branch.name!==state.name || branch.id===parent.id || endpoint.branch_id!==branch.id || endpoint.id!==state.endpoint || [provision.production_compute.id,provision.test_compute.id].includes(endpoint.id))throw Error('unowned_disposable_endpoint');
 source=await connect();const identity=(await source.query('SELECT current_database() database,current_user role,pg_postmaster_start_time()::text started')).rows[0];
 if(identity.database!=='github_state_test' || identity.role!=='github_state_test_migration')throw Error('test_sql_identity_mismatch');
 receipt.before=identity;
 if(mode==='restart') {
  const params=new URLSearchParams({repository:state.repository.name,operation:'issue_comments',args:'{"number":1}'});
  const warm=(await worker('/v1/query?'+params,undefined,'client')).value;if(warm.status!=='ok')throw Error('warm_read_unavailable');
  await source.end();source=undefined;
  const restarting=await api(`/projects/${project}/endpoints/${state.endpoint}/restart`,'POST');receipt.operations=(restarting.operations || []).map(op=>op.id);if(!receipt.operations.length)throw Error('restart_operation_missing');await save();
  const cached=(await worker('/v1/query?'+params,undefined,'client')).value;
  if(cached.status!=='ok' || JSON.stringify(cached.data)!==JSON.stringify(warm.data) || ['revision','observed_at','verified_at'].some(k=>cached[k]!==warm[k]))throw Error('cached_read_changed');receipt.safe_cached_read=true;
  const number=Date.now()*1000,delivery=state.name+'-restart-'+randomUUID().slice(0,8),payload={repository:{id:Number(state.repository.id)},installation:{id:Number(state.repository.installation)},action:'created',issue:{number:1,pull_request:{url:'https://example.invalid/pulls/1'}},comment:{id:number,body:'provider restart drill',user:{login:'fixture',type:'User'},created_at:'2026-10-20T00:00:00Z',updated_at:'2026-10-20T00:00:00Z'}};
  const signed=createHmac('sha256',(await read('webhook-token')).trim()).update(JSON.stringify(payload)).digest('hex');
  const accepted=await worker('/webhook/github',payload,null,{'X-Hub-Signature-256':'sha256='+signed,'X-GitHub-Delivery':delivery,'X-GitHub-Event':'issue_comment'});if(accepted.status!==202)throw Error('ingress_not_acknowledged');receipt.ingress={delivery,status:accepted.status,comment_id:String(number),at:Date.now()};
  for(const operation of receipt.operations){let done=false;for(let n=0;n<40;n++){const status=(await api(`/projects/${project}/operations/${operation}`)).operation.status;if(['finished','skipped'].includes(status)){done=true;break;}if(['failed','error','cancelled'].includes(status))throw Error('restart_operation_failed');await pause(2000);}if(!done)throw Error('restart_operation_timeout');}
  source=await connect();const restarted=(await source.query('SELECT pg_postmaster_start_time()::text started,pg_postmaster_start_time()>$1::timestamptz advanced',[identity.started])).rows[0];if(restarted.advanced!==true)throw Error('restart_not_observed');receipt.after_restart=restarted;
  let committed=false;for(let n=0;n<36;n++){const rows=(await source.query(`SELECT id FROM ${schema}.deliveries WHERE id=$1`,[delivery])).rows;if(rows.length===1){committed=true;break;}await pause(5000);}if(!committed)throw Error('ordinary_recovery_timeout');receipt.ordinary_recovery=true;receipt.delivery_receipt_count=1;
 } else {
  await worker('/fixture/faults',{database:true});paused=true;
  const tables=(await source.query('SELECT table_name FROM information_schema.tables WHERE table_schema=$1 ORDER BY table_name',[schema])).rows.map(r=>r.table_name);
  if(tables.length!==24 || tables.some(t=>!/^[a-z_]+$/.test(t)))throw Error('unexpected_fixture_schema');
  await source.query('BEGIN');await source.query("SET LOCAL lock_timeout='5s'");await source.query('LOCK TABLE '+tables.map(t=>schema+'.'+t).join(',')+' IN SHARE MODE');
  const manifests={},counts={};for(const table of tables){const rows=(await source.query(`SELECT * FROM ${schema}.${table}`)).rows;manifests[table]=checksum(rows);counts[table]=rows.length;}
  const point=(await source.query("SELECT to_char(clock_timestamp() AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') point")).rows[0].point;
  await pause(3000);sentinel='restore_drill_'+randomUUID();await source.query(`INSERT INTO ${schema}.meta(key,value) VALUES($1,'true')`,[sentinel]);await source.query('COMMIT');
  const created=await api(`/projects/${project}/branches`,'POST',{branch:{name:state.name+'-restore-'+randomUUID().slice(0,8),parent_id:state.branch,parent_timestamp:point},endpoints:[{type:'read_write',autoscaling_limit_min_cu:.25,autoscaling_limit_max_cu:.25,suspend_timeout_seconds:300}]});
  restoreBranch=created.branch.id;receipt.restore_branch=restoreBranch;receipt.restore_point=point;await save();const host=created.endpoints?.[0]?.host;if(!host)throw Error('restore_endpoint_missing');const copy=new URL(uri);copy.hostname=host;restored=await connect(copy);
  for(const table of tables)if(checksum((await restored.query(`SELECT * FROM ${schema}.${table}`)).rows)!==manifests[table])throw Error('restore_checksum_mismatch');
  if((await restored.query(`SELECT value FROM ${schema}.meta WHERE key=$1`,[sentinel])).rows.length)throw Error('restore_point_boundary_mismatch');
  const rows=(await restored.query(`SELECT id,data FROM ${schema}.resources WHERE kind='issue_comments'`)).rows;if(!rows.some(row=>row.id==='9223372036854775807'))throw Error('exact_identifier_missing');
  const keys=[...new Set(rows.map(row=>JSON.parse(row.data).$r2).filter(Boolean))];if(!keys.length)throw Error('restore_spill_missing');
  receipt.spills=[];for(const key of keys){const result=(await worker('/fixture/spill',{key})).value;if(!result.exists || !result.valid)throw Error('restore_spill_unreadable');receipt.spills.push(result);}
  receipt.tables=counts;receipt.manifest_sha256=checksum([manifests]);receipt.exact_large_id_preserved=true;receipt.after_point_mutation_absent=true;receipt.spill_references_readable=true;
 }
 receipt.network_audit=(await worker('/fixture/network-audit')).value;if(receipt.network_audit.recorded_attempts || receipt.network_audit.truncated || receipt.network_audit.isolate_log_failures)throw Error('unexpected_network_attempts');receipt.verified=true;
} catch(error){receipt.error=/^[a-z_0-9]+$/.test(error.message)?error.message:'provider_drill_failed';process.exitCode=1;}
finally {
 await restored?.end().catch(()=>{});
 if(restoreBranch && receipt.verified)try{await api(`/projects/${project}/branches/${restoreBranch}`,'DELETE');receipt.restore_branch_deleted=true;}catch{receipt.restore_cleanup_error=true;process.exitCode=1;}
 if(source){await source.query('ROLLBACK').catch(()=>{});if(sentinel)await source.query(`DELETE FROM ${schema}.meta WHERE key=$1`,[sentinel]).catch(()=>{receipt.sentinel_cleanup_error=true;process.exitCode=1;});await source.end().catch(()=>{});}
 if(paused)await worker('/fixture/faults',{}).catch(()=>{receipt.fault_clear_error=true;process.exitCode=1;});
 receipt.finished_at=new Date().toISOString();await save();console.log(JSON.stringify(receipt));
}
