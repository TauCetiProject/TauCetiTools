import {mkdtempSync,writeFileSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';import {join} from 'node:path';
import {createHash} from 'node:crypto';import assert from 'node:assert/strict';
import {getHeapStatistics} from 'node:v8';import {execFileSync} from 'node:child_process';
import {fixture} from './migration_fixture.mjs';
// Disk-backed R2 avoids counting an in-process copy of the entire archive as
// publication memory. Run in a fresh process with a 96 MiB old-space limit;
// GC is never forced, and samples describe Node heap rather than a DO limit.
const heapFlag=/^--(?:max|min)[-_](?:(?:old|semi)[-_]space[-_]size(?:[-_]percentage)?|heap[-_]size)(?:=|$)/;
const heapFlags=process.execArgv.filter(arg=>heapFlag.test(arg));
assert.deepEqual(heapFlags,['--max-old-space-size=96'],'Use --max-old-space-size=96 as the only heap-sizing flag');
assert.ok(!(process.env.NODE_OPTIONS||'').split(/\s+/).some(arg=>heapFlag.test(arg.replace(/^['"]/,''))),'Remove heap-sizing flags from NODE_OPTIONS for this fixture');
const oldSpaceLimit=Number(heapFlags[0].split('=')[1]),githubSha=/^[a-f0-9]{40}$/.test(process.env.GITHUB_SHA||'')?process.env.GITHUB_SHA:null;
let codeCommit=null,workingTreeDirty=null;
try{const options={cwd:new URL('..',import.meta.url),encoding:'utf8',stdio:['ignore','pipe','ignore']},head=execFileSync('git',['rev-parse','HEAD'],options).trim();if(/^[a-f0-9]{40}$/.test(head))codeCommit=head;workingTreeDirty=Boolean(execFileSync('git',['status','--porcelain'],options).trim());}catch{}
const dir=mkdtempSync(join(tmpdir(),'github-state-publication-')),metadata=new Map(),samples=[];
const sample=(stage,details={})=>{const memory=process.memoryUsage(),heap=getHeapStatistics();samples.push({stage,...Object.fromEntries(['rows','repairs'].filter(key=>Number.isFinite(details[key])).map(key=>[key,details[key]])),heap_used:memory.heapUsed,rss:memory.rss,external:memory.external,array_buffers:memory.arrayBuffers,total_heap_size:heap.total_heap_size,heap_size_limit:heap.heap_size_limit});};
const archive={async put(key,text,options={}){const path=join(dir,createHash('sha256').update(key).digest('hex'));writeFileSync(path,text,{mode:0o600});const value={key,path,size:Buffer.byteLength(text),etag:createHash('sha256').update(text).digest('hex'),customMetadata:options.customMetadata||{}};metadata.set(key,value);return value;},async get(key){const value=metadata.get(key);return value?{...value,text:async()=>readFileSync(value.path,'utf8'),json:async()=>JSON.parse(readFileSync(value.path,'utf8'))}:null;},async head(key){return metadata.get(key)||null;},async list({prefix='' }={}){return {objects:[...metadata.values()].filter(value=>value.key.startsWith(prefix)),truncated:false};}};
let f;try{
 f=await fixture(undefined,archive);let github=0;f.env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>{github++;throw Error('unexpected GitHub request');}})};
 const started=Date.now()-1000,key='recent_prs:{}',q={repository:'org/repo',operation:'recent_prs'},body='x'.repeat(3900);let inputPages=0,inputRows=0;
 const rows=page=>{const values=Array.from({length:100},(_,i)=>{const number=(page-1)*100+i+1;return {number,state:'CLOSED',body,headRefOid:'a'.repeat(40),updatedAt:new Date(number>11015?started-31*86400000:started).toISOString()};});inputPages++;inputRows+=values.length;return values;};
 await f.state.enqueue(q);f.db.prepare('UPDATE jobs SET started=?,page=111 WHERE key=?').run(started,key);
 for(let page=1;page<111;page++)await archive.put(`staging/100/${encodeURIComponent(key)}/${started}/${page}`,JSON.stringify(rows(page)));
 f.state.traceRepair=(stage,_job,details)=>{if(['publication_rows','publication_repairs','publication_applied'].includes(stage))sample(stage,details);};
 const finalPage=rows(111),archivedPages=[...metadata.keys()].filter(name=>name.startsWith(`staging/100/${encodeURIComponent(key)}/${started}/`)).length;sample('before_refresh');
 assert.equal(archivedPages,110);
 await f.state.refresh(f.db.prepare('SELECT * FROM jobs WHERE key=?').get(key),{api:'core',data:finalPage});
 sample('after_refresh');
 const publishedRows=f.db.prepare("SELECT COUNT(*) n FROM resources WHERE kind='pr'").get().n,dependentJobs=f.db.prepare('SELECT COUNT(*) n FROM jobs WHERE key<>?').get(key).n,complete=await f.state.complete(q);
 assert.equal(publishedRows,11015);assert.equal(dependentJobs,44060);assert.equal(github,0);
 assert.equal(complete,true);
 const result={at:new Date().toISOString(),backend:'sqlite (node:sqlite in-memory)',code_commit:codeCommit,code_commit_source:codeCommit?'git_head':null,github_sha:githubSha,github_sha_matches_head:githubSha&&codeCommit?githubSha===codeCommit:null,github_event_name:process.env.GITHUB_EVENT_NAME||null,working_tree_dirty:workingTreeDirty,node_version:process.version,v8_version:process.versions.v8,fixture:{input_pages:inputPages,archived_pages:archivedPages,input_rows:inputRows,published_rows:publishedRows,dependent_jobs:dependentJobs,body_characters:body.length,r2_fixture:'disk backed; no in-process retained archive text'},node_old_space_limit_mib:oldSpaceLimit,heap_size_flags:heapFlags,forced_gc:false,samples,github_calls:github,complete};
 console.log(JSON.stringify(result));
}finally{f?.db.close();rmSync(dir,{recursive:true,force:true});}
