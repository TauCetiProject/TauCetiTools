import {Client} from '@neondatabase/serverless';
import {initializeSchema} from './postgres-schema.mjs';
const [repository,generation,mode]=process.argv.slice(2);
if(mode && mode!=='--standby')throw Error('Unknown initialization mode');
if(!process.env.MIGRATION_DATABASE_URL)throw Error('Usage: MIGRATION_DATABASE_URL=... APPLICATION_ROLE=... node scripts/migrate-postgres.mjs REPOSITORY_ID GENERATION');
let client;try{client=new Client(process.env.MIGRATION_DATABASE_URL);}catch{throw Error('Invalid PostgreSQL migration configuration');}
try {
 await client.connect();await client.query('BEGIN');
 const schema=await initializeSchema(client,repository,generation,process.env.APPLICATION_ROLE,{standby:mode==='--standby'});
 await client.query('COMMIT');console.log(JSON.stringify({schema,generation,schema_version:1}));
}catch{try{await client.query('ROLLBACK');}catch{}throw Error('PostgreSQL schema initialization failed; no credentials or SQL parameters are logged');}
finally{await client.end();}
