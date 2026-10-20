// Read-only operator validation. Keeps the management key outside Workers and
// never connects to PostgreSQL or GitHub. No environment/resource mutations.
import {readFile,writeFile} from 'node:fs/promises';
import {build} from 'esbuild';
const [configFile,tokenFile,outputFile]=process.argv.slice(2);
if(!configFile || !tokenFile || !outputFile)throw Error('Usage: node scripts/observe-provider.mjs IDENTITY_JSON PRIVATE_NEON_KEY RECEIPT_JSON');
const config=JSON.parse(await readFile(configFile,'utf8'));
const code=(await build({entryPoints:['src/provider_monitoring.ts'],bundle:true,platform:'node',format:'esm',write:false})).outputFiles[0].text;
const {prepareProvider,providerObservation}=await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'));
const objects=new Map();
const env={NEON_MONITORING:JSON.stringify(config),NEON_MONITORING_API_KEY:(await readFile(tokenFile,'utf8')).trim(),ARCHIVE:{async get(key){const v=objects.get(key);return v?{json:async()=>JSON.parse(v)}:null;},async put(key,value){objects.set(key,value);return {};}}};
await prepareProvider(env);
const value=await providerObservation(env);
if(value.stale)throw Error('Provider validation unavailable; no resources or credentials changed');
await writeFile(outputFile,JSON.stringify({verified_at:new Date().toISOString(),read_only:true,github_requests:0,observation:value},null,2)+'\n',{mode:0o600,flag:'wx'});
console.log('Verified read-only provider observation; receipt saved. CPU requires the configured observability integration.');
