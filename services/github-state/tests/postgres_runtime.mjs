import {randomBytes} from 'node:crypto';
import {build} from 'esbuild';
import {testCredentials} from './postgres_credentials.mjs';
import {initializeSchema} from '../scripts/postgres-schema.mjs';
import {neon,Client,types} from '@neondatabase/serverless';
types.setTypeParser(20,value=>Number(value));
types.setTypeParser(1700,value=>Number(value));
const bundle=await build({entryPoints:['src/repository.ts','src/repository_store.ts'],bundle:true,platform:'node',format:'esm',write:false,outdir:'virtual',plugins:[{
 name:'context',setup(b){b.onResolve({filter:/^cloudflare:workers$/},()=>({path:'context',namespace:'fixture'}));b.onLoad({filter:/.*/,namespace:'fixture'},()=>({contents:'export class DurableObject {constructor(ctx,env){this.ctx=ctx;this.env=env}}',loader:'js'}));}
}]});
const imported=await Promise.all(bundle.outputFiles.map(file=>import('data:text/javascript;base64,'+Buffer.from(file.text+'\n//# sourceURL=postgres-repository-fixture.mjs').toString('base64'))));
export const {RepositoryState}=imported.find(m=>m.RepositoryState);
export const {PostgresStore,SQLiteStore,postgresSQL,Rows}=imported.find(m=>m.PostgresStore);
export async function postgresFixture({configured=true}={}) {
 const credentials=testCredentials();
 const admin=new Client(credentials.migration.toString());await admin.connect();
 const id=BigInt('0x'+randomBytes(8).toString('hex')).toString(),schema='github_state_'+id;
 await initializeSchema(admin,id,'test-v1',process.env.TEST_POSTGRES_ROLE || 'github_state_test_app');
 const objects=new Map(),background=[];let alarm=null;
 const env={BORS_ACTIVITY:{fetch:(...args)=>globalThis.fetch(...args)},ALLOWED_REPOSITORIES:JSON.stringify([{id:'100',name:'org/repo',installation:'200'}]),ARCHIVE:{async head(key){return objects.has(key)?{key}:null;},async put(key,text){objects.set(key,text);},async get(key){const text=objects.get(key);return text===undefined?null:{text:async()=>text,json:async()=>JSON.parse(text)};}}};
 const context={id:{toString:()=>'100'},waitUntil:work=>background.push(work),storage:{sql:{},getAlarm:async()=>alarm,setAlarm:async at=>{alarm=at;}}};
 Object.assign(env,{REPOSITORY_BACKEND:'postgres',REPOSITORY_DATABASE_URL:process.env.TEST_POSTGRES_URL,REPOSITORY_GENERATION:'test-v1',REPOSITORIES:{idFromName:id=>({toString:()=>id})}});
 const state=new RepositoryState(context,env);
 state.store=new PostgresStore(process.env.TEST_POSTGRES_URL,id,'test-v1');
 if(configured)await state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
 state.owner=state;
 await state.setMeta('last_snapshot',Date.now());
 return {state,env,objects,admin,schema,store:state.store,async close(){await Promise.allSettled(background);await admin.query(`DROP SCHEMA ${schema} CASCADE`);await admin.end();}};
}
// Contract fixtures seed SQL in noninteractive HTTP batches. Production code
// still uses the least-privileged role and the ordinary repository boundary.
export async function postgresContractFixture({initialized=true}={}) {
 const f=await postgresFixture({configured:initialized});
 const http=neon(process.env.TEST_POSTGRES_MIGRATION_URL),pending=[];
 const dialect=sql=>{
  const match=sql.match(/INSERT OR REPLACE INTO (\w+)/i);
  if(match){
   const table=match[1],fields={resources:['kind','id','parent','data','updated','observed','revision','tombstone'],collections:['key','operation','args','complete','verified','revision','etag']}[table];
   const keys=table==='resources'?['kind','id']:['key'];
   sql=sql.replace('INSERT OR REPLACE','INSERT')+` ON CONFLICT(${keys.join(',')}) DO UPDATE SET `+fields.filter(c=>!keys.includes(c)).map(c=>c+'=excluded.'+c).join(',');
  }
  return postgresSQL(sql,f.schema);
 };
 let flushing;
 async function flush(){
  if(flushing)return flushing;
  flushing=(async()=>{while(pending.length) {
   const statements=pending.splice(0,250);
   await http.transaction(statements.map(s=>http.query(dialect(s.sql),s.args)));
  }})();
  try{await flushing;}finally{flushing=undefined;}
 }
 const raw=f.state.store;
 f.state.store={backend:'postgres',exec:async(...args)=>{await flush();return raw.exec(...args);},batch:async(...args)=>{await flush();return raw.batch(...args);},transaction:async apply=>{await flush();return raw.transaction(apply);},health:()=>raw.health()};
 const db={prepare(sql){return {
  async run(...args){if(/^INSERT OR REPLACE INTO resources/.test(sql)){pending.push({sql:'DELETE FROM resources WHERE kind=? AND id=?',args:args.slice(0,2)},{sql:sql.replace('INSERT OR REPLACE','INSERT'),args});}else pending.push({sql,args});},
  async all(...args){await flush();return http.query(dialect(sql),args);},
  async get(...args){return (await this.all(...args))[0];}
 };},async close(){await f.close();}};
 await f.admin.query(`DELETE FROM ${f.schema}.meta WHERE key='last_snapshot'${initialized?'':" OR key='repository'"}`);
 const insert=async(operation,number,priority,due,attempt=0)=>{
  const q={operation,args:{number}},key=operation+':'+JSON.stringify(q.args);
  await db.prepare('INSERT INTO jobs(key,query,priority,due,attempt,lane) VALUES(?,?,?,?,?,?)').run(key,JSON.stringify(q),priority,due,attempt,priority<=5?'active':'verification');return q;
 };
 return {...f,db,insert,ctx:f.state.ctx};
}
