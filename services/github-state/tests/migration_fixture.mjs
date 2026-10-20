import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {DatabaseSync} from 'node:sqlite';
import {build} from 'esbuild';
import {runtime} from './runtime.mjs';
import {Miniflare,convertV4MiniflareOptions} from 'miniflare';

const shim={name:'fixture',setup(b){b.onResolve({filter:/^cloudflare:workers$/},()=>({path:'context',namespace:'fixture'}));b.onLoad({filter:/.*/,namespace:'fixture'},()=>({contents:'export class DurableObject {constructor(ctx,env){this.ctx=ctx;this.env=env}}',loader:'js'}));}};
const load=async entry=>import('data:text/javascript;base64,'+Buffer.from((await build({entryPoints:[entry],bundle:true,platform:'node',format:'esm',write:false,plugins:[shim]})).outputFiles[0].text).toString('base64'));
const {RepositoryState}=await load('src/repository.ts');
const migration=await load('src/migration.ts');
const {REQUIRED_TABLES}=await load('src/storage.ts');
const worker=(await load('src/index.ts')).default;
const repo={id:'100',name:'org/repo',installation:'200'};
const generation='c707ddba-076d-4a63-8696-96533031ce88';
const target='neon-test-branch-database';
const sha=text=>createHash('sha256').update(text).digest('hex');

function bucket(){
 const objects=new Map();let failure=null;
 const check=(op,key,when)=>{if(failure?.(op,key,when)){failure=null;throw new Error('sensitive payload/credential detail');}};
 const meta=(key,o)=>({key,size:Buffer.byteLength(o.text),etag:sha(o.text),uploaded:new Date(0),customMetadata:o.customMetadata});
 return {objects,fail(fn){failure=fn;},
  async put(key,text,options={}){check('put',key,'before');
   const prior=objects.get(key);
   if(options.onlyIf?.etagDoesNotMatch==='*' && prior)return null;
   if(options.onlyIf?.etagMatches && (!prior || sha(prior.text)!==options.onlyIf.etagMatches))return null;
   text=typeof text==='string'?text:Buffer.from(text).toString();if(options.sha256)assert.equal(sha(text),options.sha256);objects.set(key,{text,customMetadata:options.customMetadata || {}});check('put',key,'after');return meta(key,objects.get(key));},
  async get(key){check('get',key,'before');const o=objects.get(key);return o?{...meta(key,o),text:async()=>o.text,json:async()=>JSON.parse(o.text),arrayBuffer:async()=>new Uint8Array(Buffer.from(o.text)).buffer}:null;},
  async head(key){const o=objects.get(key);return o?meta(key,o):null;},
  async delete(keys){for(const key of [].concat(keys)){check('delete',key,'before');objects.delete(key);check('delete',key,'after');}},
  async list({prefix='',cursor,limit=1000}={}){const keys=[...objects.keys()].filter(k=>k.startsWith(prefix)).sort();const start=Number(cursor || 0),end=start+Math.min(limit,2);return {objects:keys.slice(start,end).map(k=>meta(k,objects.get(k))),truncated:end<keys.length,cursor:String(end)};},
 };
}
async function fixture(db=new DatabaseSync(':memory:'),archive=bucket(),overrides={}){
 let alarm=null;const ready=[];
 const ctx={waitUntil(work){ready.push(Promise.resolve(work));},id:{toString:()=>'source-object'},blockConcurrencyWhile(fn){const pending=fn();ready.push(pending);return pending;},storage:{sql:{databaseSize:1024,exec(sql,...bindings){let rows=[];if(!bindings.length && sql.includes(';'))db.exec(sql);else{const st=db.prepare(sql);if(st.columns().length)rows=st.all(...bindings);else st.run(...bindings);}return {toArray:()=>rows,one:()=>rows[0]};}},async transaction(fn){db.exec('SAVEPOINT async_tx');try{const result=await fn();db.exec('RELEASE async_tx');return result;}catch(error){db.exec('ROLLBACK TO async_tx');db.exec('RELEASE async_tx');throw error;}},transactionSync(fn){db.exec('SAVEPOINT tx');try{const v=fn();db.exec('RELEASE tx');return v;}catch(e){db.exec('ROLLBACK TO tx');db.exec('RELEASE tx');throw e;}},sync:async()=>{},getCurrentBookmark:async()=>'authoritative-bookmark',setAlarm:async v=>{alarm=v;},getAlarm:async()=>alarm}};
 const env={ARCHIVE:archive,ALLOWED_REPOSITORIES:JSON.stringify([repo]),CF_VERSION:{id:'test-deployment'},MIGRATION_ENVIRONMENT:'test',...overrides};
 const state=new RepositoryState(ctx,env);await Promise.all(ready);
 if(!(await state.meta('repository')))(await state.setMeta('repository',repo));
 env.REPOSITORIES={idFromName:x=>x,get:()=>({fetch:(url,options)=>state.fetch(new Request(url,options))})};
 const request=(path,body)=>state.fetch(new Request('https://internal'+path,{method:'POST',body:JSON.stringify(body)}));
 const command=async(operation,extra={})=>{const response=await request('/migration',{repository:repo.name,environment:'test',operation,generation,target,apply:true,...extra});return {status:response.status,body:await response.json()};};
 return {db,archive,state,env,ctx,request,command};
}
async function fill(f){
 const now=Date.now();
 (await f.state.setMeta('revision',123));
 for(const table of REQUIRED_TABLES){
  if(table==='meta')continue;
  const columns=f.db.prepare('PRAGMA table_info('+table+')').all();
  const values=columns.map(c=>c.type==='INTEGER'?1:'fixture');
  f.db.prepare('INSERT INTO '+table+' VALUES('+values.map(()=>'?').join(',')+')').run(...values);
 }
 f.db.prepare('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)').run('pr','9223372036854775807','',JSON.stringify({$r2:'resources/100/spill'}),'original-timestamp',now,123,1);
 f.db.prepare('INSERT INTO collections VALUES(?,?,?,?,?,?,?)').run('incomplete','open_prs','{}',0,now,123,'original-etag');
 f.db.exec("UPDATE jobs SET query='{\"operation\":\"pr\"}',due=9999999999999,lease_until=9999999999998,attempt=4,started=12345,page=3,lease_id='old-worker',wait_reason='quota',generation=17,start_generation=16");
 f.db.exec('UPDATE activity_landings SET at=1792360000123456');
 // Exercise signed rowid extremes without JavaScript integer conversion.
 f.db.exec("INSERT INTO demand(rowid,key,at) VALUES(-9223372036854775808,'negative',9223372036854775807)");
 f.db.exec("INSERT INTO demand(rowid,key,at) VALUES(9223372036854775807,'positive',-9223372036854775808)");
 return Promise.all(['resources/100/spill','cursors/100/old/100.json','staging/100/fixture/12345/1','staging/100/fixture/12345/2'].map(key=>f.archive.put(key,'{"archived":true}',{customMetadata:{expires:'1'}})));
}
async function finish(f){
 let result;
 for(let n=0;n<500;n++){
  result=await f.command('step',{limit:2});
  assert.equal(result.status,200,JSON.stringify(result.body));
  if(result.body.phase==='source_verified')return result.body;
 }
 assert.fail('migration did not finish');
}


export {fixture,fill,finish,bucket,load,migration,RepositoryState,worker,repo,generation,target,sha,REQUIRED_TABLES};
