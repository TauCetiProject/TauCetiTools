import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {build} from 'esbuild';

const bundle=async entry=>{
 const out=await build({entryPoints:[entry],bundle:true,platform:'node',format:'esm',write:false,plugins:[{
  name:'durable-object-context',setup(builder){
   builder.onResolve({filter:/^cloudflare:workers$/},()=>({path:'context',namespace:'fixture'}));
   builder.onLoad({filter:/.*/,namespace:'fixture'},()=>({contents:'export class DurableObject {constructor(ctx,env){this.ctx=ctx;this.env=env}}',loader:'js'}));
  }
 }]});
 return import('data:text/javascript;base64,'+Buffer.from(out.outputFiles[0].text).toString('base64'));
};
const {RepositoryState}=await bundle('src/repository.ts');
const {repositories,repository,retired}=await bundle('src/protocol.ts');
const roadmap={id:'300',name:'org/roadmap',installation:'200'},repo={id:'100',name:'org/repo',installation:'200'};

// One retained SQLite object whose state survives "deployments" (new RepositoryState instances).
function storage(){
 const db=new DatabaseSync(':memory:'),alarm={at:null,deleted:0};
 const ctx={blockConcurrencyWhile:fn=>fn(),waitUntil(){},storage:{sql:{exec(sql,...bindings){
  let rows=[];
  if(!bindings.length && sql.includes(';'))db.exec(sql);
  else {const statement=db.prepare(sql);if(statement.columns().length)rows=statement.all(...bindings);else statement.run(...bindings);}
  return {toArray:()=>rows,one:()=>rows[0]};
 }},async transaction(fn){db.exec('SAVEPOINT async_tx');try{const result=await fn();db.exec('RELEASE async_tx');return result;}catch(error){db.exec('ROLLBACK TO async_tx');db.exec('RELEASE async_tx');throw error;}},
 transactionSync(fn){db.exec('SAVEPOINT tx');try{const value=fn();db.exec('RELEASE tx');return value;}catch(error){db.exec('ROLLBACK TO tx');db.exec('RELEASE tx');throw error;}},
 getAlarm:async()=>alarm.at,setAlarm:async value=>{alarm.at=value;},deleteAlarm:async()=>{alarm.at=null;alarm.deleted++;}}};
 const objects=new Map();
 const archive={async head(key){return objects.has(key)?{key}:null;},async put(key,text){objects.set(key,text);},async get(key){const text=objects.get(key);return text===undefined?null:{text:async()=>text,json:async()=>JSON.parse(text)};},async list(){return {objects:[],truncated:false};},async delete(){}};
 const deploy=retiredNames=>new RepositoryState(ctx,{ALLOWED_REPOSITORIES:JSON.stringify([repo,roadmap]),ARCHIVE:archive,...(retiredNames?{RETIRED_REPOSITORIES:JSON.stringify(retiredNames)}:{})});
 return {alarm,deploy};
}

test('retired repositories leave every configured-repository lookup, by name or id',()=>{
 const env={ALLOWED_REPOSITORIES:JSON.stringify([repo,roadmap]),RETIRED_REPOSITORIES:JSON.stringify(['Org/Roadmap'])};
 assert.deepEqual(repositories(env).map(r=>r.name),['org/repo']);
 assert.equal(repository(env,'org/roadmap'),undefined);
 assert.equal(repository(env,'300'),undefined);
 assert.equal(retired({...env,RETIRED_REPOSITORIES:'["300"]'},roadmap),true);
 assert.deepEqual(repositories({ALLOWED_REPOSITORIES:env.ALLOWED_REPOSITORIES}).map(r=>r.name),['org/repo','org/roadmap'],'nothing is retired by default');
 assert.throws(()=>repositories({...env,RETIRED_REPOSITORIES:'"org/roadmap"'}),/invalid retired repository configuration/);
});

test('a retired object cancels its alarm, schedules none and refuses work, then resumes when re-added',async()=>{
 const {alarm,deploy}=storage();
 const live=deploy();await live.setMeta('repository',roadmap);
 const due=Date.now()+60000;await live.setAlarm(due);
 assert.equal(alarm.at,due,'a configured repository schedules alarms');

 const retiredObject=deploy(['org/roadmap']);
 assert.equal(retiredObject.retired(),true);
 await retiredObject.setAlarm(Date.now()+1000);
 assert.equal(alarm.at,due,'a retired repository arms nothing');
 let woke=false;retiredObject.runSingleAlarm=async()=>{woke=true;};
 await retiredObject.alarm();
 assert.equal(alarm.at,null,'the pending alarm is cancelled');
 assert.equal(alarm.deleted,1);
 assert.equal(woke,false,'no repair work runs');
 for(const path of ['/query','/repair','/configure','/event','/tick']){
  const response=await retiredObject.fetch(new Request('https://internal'+path,{method:'POST',body:'{}'}));
  assert.equal(response.status,410,path);assert.equal((await response.json()).error,'repository_retired');
 }

 const readded=deploy();
 assert.equal(readded.retired(),false);
 await readded.setAlarm(due);
 assert.equal(alarm.at,due,'removing the entry restores scheduling from the retained data');
 assert.equal((await readded.meta('repository')).name,'org/roadmap','the retained data is kept');
});

test('another repository is unaffected by a retirement',async()=>{
 const {alarm,deploy}=storage();
 const first=deploy();await first.setMeta('repository',repo);
 const object=deploy(['org/roadmap']);
 assert.equal(object.retired(),false);
 const due=Date.now()+60000;await object.setAlarm(due);
 assert.equal(alarm.at,due);
});

test('raw reconciliation skips a retired target without re-reading or re-recording its delivery',async()=>{
 const {retainRaw}=await bundle('src/ingress.ts');
 const calls=[];
 const archive={async get(key){calls.push('get:'+key);return null;},async head(key){calls.push('head:'+key);return null;},async put(key){calls.push('put:'+key);}};
 const env={ALLOWED_REPOSITORIES:JSON.stringify([repo,roadmap]),RETIRED_REPOSITORIES:'["org/roadmap"]',ARCHIVE:archive};
 const object={key:'webhooks/2026/11/27/delivery-1.json',customMetadata:{targets:JSON.stringify([roadmap.id]),sha256:'a'.repeat(64),received:'1'}};
 assert.equal(await retainRaw(env,object,()=>[],async()=>false),true);
 assert.deepEqual(calls,[],'nothing is read or written for a retired target');
});
