import {mkdtempSync,writeFileSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';import {join} from 'node:path';
import {createHash} from 'node:crypto';import assert from 'node:assert/strict';
import {fixture} from './migration_fixture.mjs';
// Disk-backed R2 avoids counting an in-process copy of the entire archive as
// publication memory. Run in a fresh process with a 96 MiB old-space limit;
// GC is never forced, and samples describe Node heap rather than a DO limit.
const dir=mkdtempSync(join(tmpdir(),'github-state-publication-')),metadata=new Map(),samples=[];
const archive={async put(key,text,options={}){const path=join(dir,createHash('sha256').update(key).digest('hex'));writeFileSync(path,text,{mode:0o600});const value={key,path,size:Buffer.byteLength(text),etag:createHash('sha256').update(text).digest('hex'),customMetadata:options.customMetadata||{}};metadata.set(key,value);return value;},async get(key){const value=metadata.get(key);return value?{...value,text:async()=>readFileSync(value.path,'utf8'),json:async()=>JSON.parse(readFileSync(value.path,'utf8'))}:null;},async head(key){return metadata.get(key)||null;},async list({prefix='' }={}){return {objects:[...metadata.values()].filter(value=>value.key.startsWith(prefix)),truncated:false};}};
let f;try{
 f=await fixture(undefined,archive);let github=0;f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>{github++;throw Error('unexpected GitHub request');}})};
 const started=Date.now()-1000,key='recent_prs:{}',q={repository:'org/repo',operation:'recent_prs'},body='x'.repeat(3900);
 const rows=page=>Array.from({length:100},(_,i)=>{const number=(page-1)*100+i+1;return {number,state:'CLOSED',body,headRefOid:'a'.repeat(40),updatedAt:new Date(number>11015?started-31*86400000:started).toISOString()};});
 await f.state.enqueue(q);f.db.prepare('UPDATE jobs SET started=?,page=111 WHERE key=?').run(started,key);
 for(let page=1;page<111;page++)await archive.put(`staging/100/${encodeURIComponent(key)}/${started}/${page}`,JSON.stringify(rows(page)));
 f.state.traceRepair=(stage)=>{if(['publication_rows','publication_repairs','publication_applied'].includes(stage)){samples.push({stage,heap_used:process.memoryUsage().heapUsed});}};
 await f.state.refresh(f.db.prepare('SELECT * FROM jobs WHERE key=?').get(key),{api:'core',data:rows(111)});
 assert.equal(f.db.prepare("SELECT COUNT(*) n FROM resources WHERE kind='pr'").get().n,11015);
 assert.equal(f.db.prepare('SELECT COUNT(*) n FROM jobs').get().n,44061);assert.equal(github,0);
 const result={at:new Date().toISOString(),fixture:{staged_pages:111,decoded_rows:11100,published_rows:11015,dependent_jobs:44060,body_characters:3900,r2_fixture:'disk backed; no in-process retained archive text'},node_old_space_limit_mib:96,samples,github_calls:github,complete:await f.state.complete(q)};
 console.log(JSON.stringify(result));
}finally{f?.db.close();rmSync(dir,{recursive:true,force:true});}
