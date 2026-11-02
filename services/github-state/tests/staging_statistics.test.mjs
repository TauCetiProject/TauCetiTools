import {test} from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {DatabaseSync} from 'node:sqlite';
const bundle=await build({entryPoints:['src/staging_statistics.ts'],bundle:true,platform:'node',format:'esm',write:false});
const {stagingDurations,STAGING_DURATION_SQL}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
const now=Date.parse('2026-11-02T22:00:00Z');
const job=(id,minutesAgo,duration)=>({job_id:id,title:`bors branch=main batch=2 head=${'a'.repeat(40)} base=${'b'.repeat(40)}`,started_at:new Date(now-minutesAgo*60000-duration*1000).toISOString(),completed_at:new Date(now-minutesAgo*60000).toISOString()});
test('job runtime excludes queue time, duplicates, PR jobs and invalid clocks',()=>{
 const one=job(1,10,600),two=job(2,120,1200);
 const report=stagingDurations([one,one,two,{...job(3,5,99),title:'pr-build pr=1'},job(4,-10,100),job(5,5,-10)],now);
 assert.deepEqual(report.windows.map(x=>[x.samples,x.mean_seconds]),[[1,600],[2,900],[2,900],[2,900]]);
 assert.equal(report.read_only,true);assert.equal(report.truncated,false);
 assert.match(STAGING_DURATION_SQL,/LIMIT 10001/);
 assert.match(STAGING_DURATION_SQL,/j\.parent/);
});
test('empty mirror history is unknown and over-limit observations are marked',()=>{
 assert.equal(stagingDurations([],now).windows[0].mean_seconds,null);
 assert.equal(stagingDurations(Array(10001).fill(job(1,1,60)),now).truncated,true);
});
test('cached SQL binds jobs to their observed Bors workflow and reads no other repository',()=>{
 const db=new DatabaseSync(':memory:');
 try {
  db.exec('CREATE TABLE resources(kind TEXT,id TEXT,parent TEXT,data TEXT,tombstone INTEGER DEFAULT 0,PRIMARY KEY(kind,id))');
  const put=db.prepare('INSERT INTO resources(kind,id,parent,data) VALUES(?,?,?,?)');
  const row=job(1,10,600);
  put.run('workflow_runs','20','',JSON.stringify({display_title:row.title}));
  put.run('workflow_runs','21','',JSON.stringify({display_title:'pr-build pr=3'}));
  for(const [id,parent] of [['1','20'],['2','21'],['3','missing']])put.run('workflow_jobs',id,parent,JSON.stringify({...row,name:'sandboxed-build',status:'completed'}));
  const rows=db.prepare(STAGING_DURATION_SQL).all(new Date(now-7*86400000).toISOString());
  assert.equal(rows.length,1);assert.equal(rows[0].job_id,'1');
  assert.equal(stagingDurations(rows,now).windows[0].mean_seconds,600);
 } finally {db.close();}
});
