#!/usr/bin/env node
// SQLite fixtures or isolated Neon test contracts. Never deploys or faults production.
import {spawnSync} from 'node:child_process';
import {writeFileSync,mkdirSync,readFileSync,readdirSync} from 'node:fs';
import {resolve,dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import {postgresFiles} from './postgres-suite.mjs';
import {testCredentials} from '../tests/postgres_credentials.mjs';
const cwd=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const postgres=process.argv[2]==='--postgres';
if(postgres)testCredentials();else if(process.env.TEST_POSTGRES_URL || process.env.TEST_POSTGRES_MIGRATION_URL)throw Error('Use --postgres when test credentials are supplied');
const destination=resolve(process.argv[postgres?3:2] || '/tmp/github-state-outage-drill-'+Date.now()+'.json');
const started=new Date().toISOString();
const hash=createHash('sha256');
for(const folder of ['src','tests','scripts','migrations'])for(const file of readdirSync(resolve(cwd,folder),{recursive:true}).sort()) {
 if(!/\.(ts|mjs|py|sql)$/.test(file))continue;
 hash.update(folder+'/'+file+'\0');hash.update(readFileSync(resolve(cwd,folder,file)));
}
for(const file of ['package.json','package-lock.json','wrangler.jsonc','tsconfig.json']) {hash.update(file+'\0');hash.update(readFileSync(resolve(cwd,file)));}
const workspace_sha256=hash.digest('hex');
const revision=spawnSync('git',['rev-parse','HEAD'],{cwd,encoding:'utf8'}).stdout.trim();
const source_dirty=Boolean(spawnSync('git',['status','--porcelain','--','src','tests','scripts','migrations','package.json','package-lock.json','wrangler.jsonc','tsconfig.json'],{cwd,encoding:'utf8'}).stdout.trim());
for(const command of ['typecheck','build']) {
 const check=spawnSync('npm',['run',command],{cwd,encoding:'utf8'});
 if(check.status!==0){process.stderr.write(check.stdout+check.stderr);process.exit(check.status || 1);}
}
const files=postgres?postgresFiles:readdirSync(resolve(cwd,'tests')).filter(f=>f.endsWith('.test.mjs')).sort().map(f=>'tests/'+f);
const result=spawnSync(process.execPath,['--test','--test-concurrency=2','--test-reporter=tap',...files],{cwd,encoding:'utf8',maxBuffer:32*1024**2});
const transcript=(result.stdout || '')+(result.stderr || '');
const passed=[...result.stdout.matchAll(/^ok \d+(?: -)? (.+)$/gm)].map(m=>m[1]).filter(name=>!/# SKIP/.test(name));
const skipped=[...result.stdout.matchAll(/^ok \d+(?: -)? (.+) # SKIP.*$/gm)].map(m=>m[1]);
const failed=[...result.stdout.matchAll(/^not ok \d+(?: -)? (.+)$/gm)].map(m=>m[1]);
const evidence={version:2,started,finished:new Date().toISOString(),implementation_revision:revision,workspace_sha256,source_dirty,
  environment:postgres?'isolated_neon_test_branch':'local_sqlite_fault_fixtures',typecheck_exit_status:0,build_exit_status:0,exit_status:result.status,
  passed:passed.length,failed:failed.length,skipped:skipped.length,test_files:files,scenarios:passed,failed_tests:failed,
  transcript_sha256:createHash('sha256').update(transcript).digest('hex'),
  postgresql:{run:postgres,prerequisite:'TauCetiTools PR #5 / Worker issue #308',production_data:false},
  production_fault_injection:false};
mkdirSync(dirname(destination),{recursive:true});
writeFileSync(destination+'.tap',transcript);
writeFileSync(destination,JSON.stringify(evidence,null,2)+'\n');
process.stdout.write(JSON.stringify({...evidence,evidence_file:destination,transcript_file:destination+'.tap'},null,2)+'\n');
if(result.status!==0)process.stderr.write(transcript);
process.exit(result.status || (result.error || !passed.length?1:0));
