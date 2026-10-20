import assert from 'node:assert/strict';
import {randomBytes} from 'node:crypto';
import {Client} from '@neondatabase/serverless';
import {fixture,fill,load,migration,generation} from './migration_fixture.mjs';
import {initializeSchema} from '../scripts/postgres-schema.mjs';
import {testCredentials} from './postgres_credentials.mjs';
const target=await load('src/migration_target.ts');
const control=await load('src/migration_control.ts');
const enabled=Boolean(process.env.TEST_POSTGRES_URL);
async function testFixture(){
 const credentials=testCredentials(),admin=new Client(credentials.migration.toString());await admin.connect();
 const id=BigInt('0x'+randomBytes(8).toString('hex')).toString(),schema='github_state_'+id;
 await admin.query('BEGIN');await initializeSchema(admin,id,generation,decodeURIComponent(credentials.application.username),{standby:true});
 await admin.query(`INSERT INTO ${schema}.capacity_statistics VALUES(1,$1,0,1099511627776,$1)`,[Date.now()]);await admin.query('COMMIT');
 const f=await fixture();await fill(f);
 const state={version:1,generation,repository:id,target:'neon-test',source:{object:'source-object',deployment:'test-deployment',schema:5},phase:'freezing',created:Date.now(),updated:Date.now(),tables:{},failure:null,
  staging:{after:null,page:'1',checked:0,complete:false,expired_jobs:0,expired_hash:'0'.repeat(64)},blobs:{prefix:0,pages:0,objects:0,hash:'0'.repeat(64),complete:false},
  cutover:{environment:'test',lease_until:Date.now()+900000,epoch:0,source_fenced:false,target_active:false,import:{},verify:{},acceptance:''}};
 f.env.REPOSITORY_DATABASE_URL=credentials.application.toString();f.env.MIGRATION_DATABASE_URL=credentials.migration.toString();
 f.env.MIGRATION_TARGET=JSON.stringify({identity:state.target,hostname:credentials.migration.hostname,database:decodeURIComponent(credentials.migration.pathname.slice(1)),application_role:decodeURIComponent(credentials.application.username),migration_role:decodeURIComponent(credentials.migration.username)});
 await f.archive.put('control/queue-metrics.json',JSON.stringify({queue:{at:Date.now(),count:0,error:null},dlq:{at:Date.now(),count:0,error:null}}));
 const checks=Object.fromEntries(['s0_compatibility','parked_ingress','queue_dlq_ownership','r2_retention_drained','s1_schema_roles','capacity','restore','unchanged_clients','stored_shadow','no_github_refetch','warm_read_p95'].map(x=>[x,true]));
 await f.archive.put(`control/${id}/migration-acceptance.json`,JSON.stringify({version:1,environment:'test',repository:id,target:state.target,deployment:'test-deployment',source_object:'source-object',valid_until:Date.now()+86400000,checks,queries:[{repository:'org/repo',operation:'open_prs'}],latency:{baseline_p95_ms:100,target_p95_ms:100}}));
 const retention=(await load('src/retention.ts')).R2_CLASSES;
 await f.archive.put(`control/${id}/retention-policy.json`,JSON.stringify({mode:'dry_run',disabled:[],pinned:Object.keys(retention)}));
 // This focused protocol fixture uses SQL belonging to a random isolated target;
 // R2 references remain exact source addresses, with no public/GitHub requests.
 let fenced=false,selected=false,released=false;
 const hooks={async compatibility(){},storage:f.ctx.storage,sql:f.ctx.storage.sql,async verifyStored(state){state.cutover.shadow={queries:1,hash:"fixture",verified_at:Date.now(),complete:true};},async drain(){},async fence(){fenced=true;},assertFence(){assert.ok(fenced);},async release(){released=true;fenced=false;},async select(){assert.ok(fenced);selected=true;}};
 return {...f,source:f.state,state,id,schema,admin,hooks,get selected(){return selected;},get released(){return released;},async close(){f.db.close();await admin.query(`DROP SCHEMA IF EXISTS ${schema} CASCADE`);await admin.end();}};
}

export {testFixture,target,control};
