import {spawn} from 'node:child_process';
import {postgresFiles} from './postgres-suite.mjs';
for(const key of ['TEST_POSTGRES_URL','TEST_POSTGRES_MIGRATION_URL']) {
 let valid=false;try{valid=Boolean(process.env[key]) && decodeURIComponent(new URL(process.env[key]).username).includes('_test_');}catch{}
 if(!valid)throw Error('PostgreSQL contracts require isolated test application and migration credentials');
}
const child=spawn(process.execPath,['--test','--test-concurrency=2',...postgresFiles],{stdio:'inherit'});
child.on('exit',code=>{process.exitCode=code ?? 1;});
