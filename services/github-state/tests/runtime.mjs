import {build} from 'esbuild';
import {readFileSync} from 'node:fs';
import {generateKeyPairSync,createHash} from 'node:crypto';
import {workerDatabase} from './worker_postgres.mjs';
import {Miniflare,convertV4MiniflareOptions} from 'miniflare';
const key=generateKeyPairSync('rsa',{modulusLength:2048}).privateKey.export({format:'pem',type:'pkcs8'});
let storedReadBundle;
const hash=s=>createHash('sha256').update(s).digest('hex');
export const repository={id:'100',name:'org/repo',installation:'200'};
export async function runtime(handler,bindings={},options={}){
 const {repositoryAlarms,repositoryExtension,workerExtension,...runtimeOptions}=options;
 let script=readFileSync('dist/index.js','utf8');
 if(repositoryAlarms===false || repositoryExtension || workerExtension){
  const bundle=()=>build({stdin:{contents:"import worker,{RepositoryState as Base,InstallationScheduler} from './src/index';class RepositoryState extends Base{"+(repositoryAlarms===false?"async setAlarm(){}":"")+(repositoryExtension || "")+"};export{RepositoryState,InstallationScheduler};export default "+(workerExtension || "worker")+";",resolveDir:process.cwd(),loader:'ts'},bundle:true,format:'esm',platform:'browser',target:'es2022',external:['cloudflare:workers'],write:false}).then(result=>result.outputFiles[0].text);
  script=await ((repositoryExtension || workerExtension)?bundle():(storedReadBundle ??=bundle()));
 }
 const postgres=process.env.TEST_POSTGRES_URL?await workerDatabase(JSON.parse(bindings.ALLOWED_REPOSITORIES || JSON.stringify([repository]))):null;
 const mf=new Miniflare(convertV4MiniflareOptions({ratelimits:{PUBLIC_QUERY_LIMIT:{namespace_id:"1001",simple:{limit:1200,period:60}}},...runtimeOptions,modules:true,script,compatibilityDate:'2026-10-11',durableObjects:{REPOSITORIES:{className:'RepositoryState',useSQLite:true},INSTALLATIONS:{className:'InstallationScheduler',useSQLite:true}},r2Buckets:['ARCHIVE'],queueProducers:{EVENTS:'events'},bindings:{ALLOWED_REPOSITORIES:JSON.stringify([repository]),GITHUB_APP_ID:'42',GITHUB_INSTALLATION_ID:'200',GITHUB_PRIVATE_KEY:String(key),WEBHOOK_SECRET:'secret',CLIENT_TOKENS:JSON.stringify([{hash:hash('client'),repositories:['org/repo']}]),ADMIN_TOKEN_HASH:hash('admin'),...bindings,...postgres?.bindings},outboundService:async req=>{
  const database=await postgres?.outbound(req);if(database)return database;
  const u=new URL(req.url);
  if(u.pathname.endsWith('/access_tokens'))return Response.json({token:'installation-read-token',expires_at:new Date(Date.now()+3600000).toISOString()},{status:201});
  return handler(req,u);
 }}));
 if(postgres){const dispose=mf.dispose.bind(mf);mf.dispose=async()=>{try{await dispose();}finally{await postgres.close();}};}
 return mf;
}
export const response=(body,api='graphql',remaining=4999,status=200)=>Response.json(body,{status,headers:{'x-ratelimit-resource':api,'x-ratelimit-remaining':String(remaining),'x-ratelimit-limit':'5000','x-ratelimit-reset':String(Math.floor(Date.now()/1000)+300)}});
export const send=(stub,path,body)=>stub.fetch('https://internal'+path,{method:'POST',body:JSON.stringify(body)});
