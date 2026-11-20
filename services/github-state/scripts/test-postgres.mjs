import {spawn} from 'node:child_process';
import {postgresFiles} from './postgres-suite.mjs';
for(const key of ['TEST_POSTGRES_URL','TEST_POSTGRES_MIGRATION_URL']) {
 let valid=false;try{valid=Boolean(process.env[key]) && decodeURIComponent(new URL(process.env[key]).username).includes('_test_');}catch{}
 if(!valid)throw Error('PostgreSQL contracts require isolated test application and migration credentials');
}
// Two files at a time suits the Neon test branch's 0.5 to 2 CU compute; a local database takes more.
const args=['--test','--test-concurrency='+(process.env.POSTGRES_TEST_CONCURRENCY || 2)];
if(process.env.POSTGRES_TEST_REPORT)args.push(
 '--test-reporter=spec','--test-reporter-destination=stdout',
 '--test-reporter=junit','--test-reporter-destination='+process.env.POSTGRES_TEST_REPORT,
);
const child=spawn(process.execPath,[...args,...postgresFiles],{stdio:'inherit'});
child.on('exit',code=>{process.exitCode=code ?? 1;});
