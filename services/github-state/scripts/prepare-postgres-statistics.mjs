import {Client} from '@neondatabase/serverless';
const [id]=process.argv.slice(2),limit=Number(process.env.STORAGE_LIMIT_BYTES);
if(!/^[0-9]{1,50}$/.test(id || '') || !process.env.MIGRATION_DATABASE_URL || !Number.isSafeInteger(limit) || limit<1e12)throw Error('Supply a repository ID, migration URL and verified STORAGE_LIMIT_BYTES of at least 1 TB');
let client;try{client=new Client({connectionString:process.env.MIGRATION_DATABASE_URL,connectionTimeoutMillis:5000,query_timeout:10000});}catch{throw Error('Invalid PostgreSQL migration configuration');}
try {
 await client.connect();
 await client.query(`INSERT INTO github_state_${id}.capacity_statistics(singleton,storage_limit_bytes) VALUES(1,$1) ON CONFLICT(singleton) DO UPDATE SET storage_limit_bytes=excluded.storage_limit_bytes`,[limit]);
 const generation=(await client.query(`SELECT generation FROM github_state_${id}.backend_control WHERE singleton=1`)).rows[0]?.generation;
 await client.query(`SELECT github_state_${id}.prepare_monitoring($1,true)`,[generation]);
 console.log(JSON.stringify({repository:id,prepared:true,allowance_source:'operator_verified_effective_project_account_limit'}));
}catch{throw Error('Capacity preparation failed');}finally{await client.end().catch(()=>{});}
