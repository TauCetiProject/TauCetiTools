#!/usr/bin/env node
// Local fault fixtures only. Never deploys or touches production infrastructure.
import {spawnSync} from 'node:child_process';
import {writeFileSync,mkdirSync,readFileSync,readdirSync} from 'node:fs';
import {resolve,dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
const cwd=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const destination=resolve(process.argv[2] || '/tmp/github-state-outage-drill-'+Date.now()+'.json');
const started=new Date().toISOString();
const hash=createHash('sha256');
for(const folder of ['src','tests','scripts'])for(const file of readdirSync(resolve(cwd,folder),{recursive:true}).sort()) {
 if(!/\.(ts|mjs|py)$/.test(file))continue;
 hash.update(folder+'/'+file+'\0');hash.update(readFileSync(resolve(cwd,folder,file)));
}
const workspace_sha256=hash.digest('hex');
const revision=spawnSync('git',['rev-parse','HEAD'],{cwd,encoding:'utf8'}).stdout.trim();
const build=spawnSync('npm',['run','build'],{cwd,encoding:'utf8'});
if(build.status!==0){process.stderr.write(build.stdout+build.stderr);process.exit(build.status || 1);}
const result=spawnSync(process.execPath,['--test','--test-reporter=tap','tests/recovery.test.mjs','tests/storage.test.mjs','tests/upstream.test.mjs','tests/activity_landings.test.mjs'],{cwd,encoding:'utf8',maxBuffer:32*1024**2});
const transcript=(result.stdout || '')+(result.stderr || '');
const evidence={version:1,started,finished:new Date().toISOString(),revision,workspace_sha256,environment:'local_fault_fixtures',exit_status:result.status,
  passed:(result.stdout.match(/^ok \d+ /gm) || []).length,failed:(result.stdout.match(/^not ok \d+ /gm) || []).length,
  transcript_sha256:createHash('sha256').update(transcript).digest('hex'),
  scenarios:['full SQLite and cold restart','durable parked ingress and idempotent recovery','R2 write/checkpoint/archive failures','GitHub quota exhaustion and recovery','malformed upstream/service responses','bounded retention over 50 simulated days','recovery export digest verification and SQLite restore'],
  postgresql:{run:false,blocked_by:'S1 issue #308 repository-store interface and isolated PostgreSQL test infrastructure'},
  production_fault_injection:false};
mkdirSync(dirname(destination),{recursive:true});
writeFileSync(destination+'.tap',transcript);
writeFileSync(destination,JSON.stringify(evidence,null,2)+'\n');
process.stdout.write(JSON.stringify({...evidence,evidence_file:destination,transcript_file:destination+'.tap'},null,2)+'\n');
if(result.status!==0)process.stderr.write(transcript);
process.exit(result.status || (result.error?1:0));
