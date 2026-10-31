import {DatabaseSync} from 'node:sqlite';
import {build} from 'esbuild';
const bundle=await build({entryPoints:['src/installation.ts'],bundle:true,platform:'node',format:'esm',write:false,plugins:[{
 name:'context',setup(b){b.onResolve({filter:/^cloudflare:workers$/},()=>({path:'context',namespace:'fixture'}));b.onLoad({filter:/.*/,namespace:'fixture'},()=>({contents:'export class DurableObject {constructor(ctx,env){this.ctx=ctx;this.env=env}}',loader:'js'}));}
}]});
export const {InstallationScheduler}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text+'\n//# sourceURL=repair-quota-fixture.mjs').toString('base64'));
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
export function installationFixture(Scheduler=InstallationScheduler){
 const db=new DatabaseSync(':memory:'),values=new Map(),background=[],received=[];
 const repos=Array.from({length:8},(_,i)=>({id:String(100+i),name:'org/repo'+i,installation:'200'}));
 values.set('token',{token:'fixture-token',expires:Date.now()+3600000});
 const ctx={waitUntil:work=>{background.push(work);work.catch(()=>{});},storage:{
  async get(key){return values.get(key);},async put(key,value){values.set(key,value);},
  sql:{exec(sql,...args){let rows=[];if(!args.length&&sql.includes(';'))db.exec(sql);else{const statement=db.prepare(sql);rows=statement.columns().length?statement.all(...args):(statement.run(...args),[]);}return {toArray:()=>rows,one:()=>rows[0]};}}
 }};
 const env={ALLOWED_REPOSITORIES:JSON.stringify(repos),ARCHIVE:{get:async()=>null},REPOSITORIES:{idFromName:id=>id,get:id=>({fetch:async(_url,init)=>{
  received.push(id);if(f.notify)await f.notify(id,JSON.parse(init.body));return Response.json({ok:true});
 }})}};
 const scheduler=new Scheduler(ctx,env);
 const f={db,ctx,env,scheduler,repos,received,background,gates:[],requests:[],notify:undefined,
  async close(){for(const g of f.gates)g.resolve();await Promise.allSettled(f.requests);for(let i=0;i<background.length;i++)await background[i].catch(()=>{});db.close();}};
 return f;
}
