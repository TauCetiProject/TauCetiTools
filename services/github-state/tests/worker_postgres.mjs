import {randomBytes} from 'node:crypto';
import {Client,neonConfig} from '@neondatabase/serverless';
import {WebSocketPair,Response as WorkerResponse,coupleWebSocket} from 'miniflare';
import WebSocket from 'ws';
import {testCredentials} from './postgres_credentials.mjs';
import {initializeSchema} from '../scripts/postgres-schema.mjs';
export async function workerDatabase(repositories) {
 const {migration,application}=testCredentials();
 const database='contract_'+randomBytes(12).toString('hex'),control=new Client(migration.toString());
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
  async close(){try{await control.query('DROP DATABASE '+database+' WITH (FORCE)');}finally{await control.end();}},
  async outbound(req){
   const url=new URL(req.url);
   if(url.hostname!==application.hostname && url.hostname!==new URL(neonConfig.fetchEndpoint(application.hostname,443)).hostname)return null;
   if(req.headers.get('upgrade')?.toLowerCase()==='websocket') {
    url.protocol='wss:';
    const socket=new WebSocket(url),pair=new WebSocketPair();
    await coupleWebSocket(socket,pair[1]);return new WorkerResponse(null,{status:101,webSocket:pair[0]});
   }
   return fetch(req.url,{method:req.method,headers:Object.fromEntries(req.headers),body:['GET','HEAD'].includes(req.method)?undefined:await req.arrayBuffer()});
  }
 };
}
