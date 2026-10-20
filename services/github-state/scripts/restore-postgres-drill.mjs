// Isolated test branch only. Does not restart/select/migrate the production service.
import {readFile,writeFile} from 'node:fs/promises';
import {randomBytes,createHash} from 'node:crypto';
import {Client} from '@neondatabase/serverless';
import {testCredentials} from '../tests/postgres_credentials.mjs';
import {initializeSchema} from './postgres-schema.mjs';
const [tokenFile,destination]=process.argv.slice(2);
if(!tokenFile || !destination)throw Error('Usage: restore-postgres-drill.mjs NEON_TOKEN_FILE RECEIPT_FILE; isolated test URLs required');
const provision=JSON.parse(await readFile(new URL('../validation/neon-provisioning-2026-10-20.json',import.meta.url),'utf8'));
const project=provision.project.id,parent=provision.test_compute.branch_id;
const token=(await readFile(tokenFile,'utf8')).trim(),credentials=testCredentials();
const api=async(path,method='GET',body)=>{
 const r=await fetch('https://console.neon.tech/api/v2'+path,{method,headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined,signal:AbortSignal.timeout(30000)});
 if(!r.ok){const value=await r.json().catch(()=>({}));throw Error('neon_api_'+r.status+'_'+String(value.code || value.message || 'unknown').replace(/[^a-zA-Z0-9_ -]/g,'').slice(0,100));}return r.status===204?{}:r.json();
};
const checksum=rows=>createHash('sha256').update(JSON.stringify(rows)).digest('hex');
const receipt={version:1,started:new Date().toISOString(),project,test_branch:parent,production_data:false,production_fault_injection:false,github_calls:0,verified:false,temporary_branch_deleted:false};
let source,restored,branch,schema;
try {
 if(credentials.migration.hostname!==provision.test_compute.id+'.'+provision.project.region_id.replace('aws-','')+'.aws.neon.tech' && !credentials.migration.hostname.startsWith(provision.test_compute.id+'.'))throw Error('test_endpoint_identity_mismatch');
 const info=await api(`/projects/${project}/branches/${parent}`);
 if(info.branch.name!=='test' || info.branch.id!==parent)throw Error('test_branch_identity_mismatch');
 source=new Client({connectionString:credentials.migration.toString(),connectionTimeoutMillis:5000,query_timeout:10000});source.on('error',()=>{});await source.connect();
 const id=BigInt('0x'+randomBytes(8).toString('hex')).toString();schema='github_state_'+id;
 await initializeSchema(source,id,'restore-drill','github_state_test_app');
 await source.query(`INSERT INTO ${schema}.resources(kind,id,parent,data,observed,revision,tombstone) VALUES('issue_comments',$1,'7',$2,$3,1,0)`,['18446744073709551600',JSON.stringify({body:'before'}),Date.now()]);
 await source.query(`INSERT INTO ${schema}.collections(key,operation,args,complete,verified,revision) VALUES('issue_comments:{"number":7}','issue_comments','{"number":7}',1,$1,1)`,[Date.now()]);
 await source.query(`INSERT INTO ${schema}.members(collection,id) VALUES('issue_comments:{"number":7}','18446744073709551600')`);
 const tables=(await source.query("SELECT table_name FROM information_schema.tables WHERE table_schema=$1 ORDER BY table_name",[schema])).rows.map(r=>r.table_name);
 const manifests={};for(const table of tables)manifests[table]=checksum((await source.query(`SELECT * FROM ${schema}.${table} ORDER BY 1`)).rows);
 await new Promise(r=>setTimeout(r,3000));
 const point=(await source.query("SELECT to_char(clock_timestamp() AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') point")).rows[0].point;
 await new Promise(r=>setTimeout(r,3000));
 await source.query(`UPDATE ${schema}.resources SET data='{"body":"after"}'`);
 const created=await api(`/projects/${project}/branches`,'POST',{branch:{name:'issue317-restore-'+Date.now(),parent_id:parent,parent_timestamp:point},endpoints:[{type:'read_write',autoscaling_limit_min_cu:.25,autoscaling_limit_max_cu:.25,suspend_timeout_seconds:300}]});
 branch=created.branch.id;receipt.restore_branch=branch;receipt.restore_point=point;
 const host=created.endpoints?.[0]?.host;if(!host)throw Error('restore_endpoint_missing');
 const uri=new URL(credentials.migration);uri.hostname=host;
 const reconnect=async()=>{
  for(let attempt=0;attempt<30;attempt++) {
   const client=new Client({connectionString:uri.toString(),connectionTimeoutMillis:5000,query_timeout:10000});
   client.on('error',()=>{});
   try {await client.connect();return client;}catch {await client.end().catch(()=>{});await new Promise(r=>setTimeout(r,2000));}
  }
  throw Error('restore_connection_unavailable');
 };
 restored=await reconnect();
 for(const table of tables)if(checksum((await restored.query(`SELECT * FROM ${schema}.${table} ORDER BY 1`)).rows)!==manifests[table])throw Error('restore_checksum_mismatch');
 const value=(await restored.query(`SELECT id,data FROM ${schema}.resources`)).rows[0];
 if(value.id!=='18446744073709551600' || JSON.parse(value.data).body!=='before')throw Error('restore_semantic_mismatch');
 // Only this just-created disposable endpoint is restarted, after closing
 // our own session. Never restart the shared test or production endpoint.
 const endpoint=created.endpoints[0];
 if(endpoint.branch_id!==branch || [provision.test_compute.id,provision.production_compute.id].includes(endpoint.id))throw Error('restart_endpoint_identity_mismatch');
 await restored.end();restored=undefined;
 await api(`/projects/${project}/endpoints/${endpoint.id}/restart`,'POST');
 restored=await reconnect();
 for(const table of tables)if(checksum((await restored.query(`SELECT * FROM ${schema}.${table} ORDER BY 1`)).rows)!==manifests[table])throw Error('restart_checksum_mismatch');
 receipt.temporary_endpoint_restarted=true;receipt.restart_recovered=true;receipt.temporary_endpoint=endpoint.id;
 receipt.verified=true;receipt.tables=tables.length;receipt.manifest_sha256=checksum(manifests);receipt.exact_id_preserved=true;receipt.before_restored_after_source_mutation=true;
}catch(error){receipt.error=/^[a-zA-Z0-9_ -]+$/.test(error.message)?error.message:'restore_validation_failed';process.exitCode=1;}
finally {
 await restored?.end().catch(()=>{});
 if(branch)try {await api(`/projects/${project}/branches/${branch}`,'DELETE');receipt.temporary_branch_deleted=true;}catch {receipt.cleanup_error='restore_branch_cleanup_failed';process.exitCode=1;}
 if(source && schema)await source.query(`DROP SCHEMA ${schema} CASCADE`).catch(()=>{receipt.source_cleanup_error=true;process.exitCode=1;});
 await source?.end().catch(()=>{});
 receipt.finished=new Date().toISOString();await writeFile(destination,JSON.stringify(receipt,null,2)+'\n');
 console.log(JSON.stringify(receipt));
}
