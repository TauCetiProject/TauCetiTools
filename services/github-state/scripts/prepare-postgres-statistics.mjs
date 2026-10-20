import {Client} from '@neondatabase/serverless';
const [id]=process.argv.slice(2),limit=Number(process.env.STORAGE_LIMIT_BYTES);
if(!/^[0-9]{1,50}$/.test(id || '') || !process.env.MIGRATION_DATABASE_URL || !Number.isSafeInteger(limit) || limit<1e12)throw Error('Supply a repository ID, migration URL and verified STORAGE_LIMIT_BYTES of at least 1 TB');
let client;try{client=new Client(process.env.MIGRATION_DATABASE_URL);}catch{throw Error('Invalid PostgreSQL migration configuration');}
try {
 await client.connect();
 await client.query(`INSERT INTO github_state_${id}.capacity_statistics(singleton,prepared_at,database_bytes,storage_limit_bytes) VALUES(1,$1,pg_database_size(current_database()),$2) ON CONFLICT(singleton) DO UPDATE SET prepared_at=excluded.prepared_at,database_bytes=excluded.database_bytes,storage_limit_bytes=excluded.storage_limit_bytes`,[Date.now(),limit]);
 console.log(JSON.stringify({repository:id,prepared:true}));
}catch{throw Error('Capacity preparation failed');}finally{await client.end();}
