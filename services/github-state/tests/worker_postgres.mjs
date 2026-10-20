import {randomBytes} from 'node:crypto';
import {Client,neonConfig} from '@neondatabase/serverless';
import {WebSocketPair,Response as WorkerResponse,coupleWebSocket} from 'miniflare';
import WebSocket from 'ws';
import {testCredentials} from './postgres_credentials.mjs';
import {initializeSchema} from '../scripts/postgres-schema.mjs';
const cleanupRetryMs=[250,500,1000,2000,4000,8000];
export async function dropWorkerDatabase(control,database,pause=ms=>new Promise(resolve=>setTimeout(resolve,ms))) {
 if(!/^contract_[0-9a-f]{24}$/.test(database))throw Error('Invalid isolated worker database');
 for(let attempt=0;;attempt++) {
  try{await control.query('DROP DATABASE IF EXISTS "'+database+'" WITH (FORCE)');return;}
  catch(error){
   // Worker/provider backends can still be closing when cleanup starts.
   const transient=error?.code==='55006' || error?.code==='42501' && error?.routine==='TerminateOtherDBBackends';
   if(!transient || attempt===cleanupRetryMs.length)throw error;
   await pause(cleanupRetryMs[attempt]);
  }
 }
}
export async function closeWorkerDatabase(control,database,sockets=new Set(),pause) {
 const errors=[];
 try{
  const closed=await Promise.allSettled([...sockets].map(socket=>new Promise((resolve,reject)=>{
   if(socket.readyState===WebSocket.CLOSED){resolve();return;}
   const finish=()=>{clearTimeout(timer);socket.off('close',finish);resolve();};
   const timer=setTimeout(finish,2000);
   socket.once('close',finish);socket.once('error',()=>{});
   try{socket.terminate();}catch(error){clearTimeout(timer);socket.off('close',finish);reject(error);}
  })));
  for(const result of closed)if(result.status==='rejected')errors.push(result.reason);
 }catch(error){errors.push(error);}
 try{await dropWorkerDatabase(control,database,pause);}catch(error){errors.push(error);}
 try{await control.end();}catch(error){errors.push(error);}
 if(errors.length===1)throw errors[0];
 if(errors.length>1)throw new AggregateError(errors,'Isolated worker database cleanup failed');
}
export async function workerDatabase(repositories) {
 const {migration,application}=testCredentials();
 const database='contract_'+randomBytes(12).toString('hex'),control=new Client(migration.toString()),sockets=new Set();let closing;
 await control.connect();await control.query('CREATE DATABASE '+database);
 migration.pathname='/'+database;application.pathname='/'+database;
 const admin=new Client(migration.toString());
 try {
  await admin.connect();await admin.query(`REVOKE CONNECT ON DATABASE ${database} FROM PUBLIC`);
  const role=process.env.TEST_POSTGRES_ROLE || 'github_state_test_app';
  if(!/^[a-z][a-z0-9_]+$/.test(role))throw Error('Invalid test role');
  await admin.query(`GRANT CONNECT ON DATABASE ${database} TO ${role}`);
  for(const repo of repositories)await initializeSchema(admin,repo.id,'test-v1',role);
 }finally{await admin.end();}
 return {bindings:{REPOSITORY_BACKEND:'postgres',REPOSITORY_GENERATION:'test-v1',REPOSITORY_DATABASE_URL:application.toString()},
  async close(){return closing ||=closeWorkerDatabase(control,database,sockets);},
  async outbound(req){
   const url=new URL(req.url);
   if(url.hostname!==application.hostname && url.hostname!==new URL(neonConfig.fetchEndpoint(application.hostname,443)).hostname)return null;
   if(closing)throw Error('Worker database fixture is closing');
   if(req.headers.get('upgrade')?.toLowerCase()==='websocket') {
    url.protocol='wss:';
    const socket=new WebSocket(url),pair=new WebSocketPair();sockets.add(socket);socket.once('close',()=>sockets.delete(socket));
    await coupleWebSocket(socket,pair[1]);return new WorkerResponse(null,{status:101,webSocket:pair[0]});
   }
   return fetch(req.url,{method:req.method,headers:Object.fromEntries(req.headers),body:['GET','HEAD'].includes(req.method)?undefined:await req.arrayBuffer()});
  }
 };
}
