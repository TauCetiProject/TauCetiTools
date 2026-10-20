// Build a committed fixture once, then deploy these exact bytes with no_bundle.
import {build} from 'esbuild';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import {sourceFingerprint} from './source-fingerprint.mjs';
const cwd=fileURLToPath(new URL('..',import.meta.url)),destination=process.argv[2];
if(!destination)throw Error('Usage: node scripts/build-deployed-drill.mjs PRIVATE_STATE_DIRECTORY');
const identity=sourceFingerprint(cwd);if(identity.source_dirty)throw Error('Commit source before building deployed acceptance evidence');
const output=await build({absWorkingDir:cwd,entryPoints:['scripts/fixtures/deployed-outage.ts'],bundle:true,format:'esm',platform:'browser',target:'es2022',external:['cloudflare:workers'],write:false});
if(output.outputFiles.length!==1)throw Error('Unexpected fixture bundle modules');
const dir=resolve(destination),bundle=output.outputFiles[0].contents;
await mkdir(dir,{recursive:true,mode:0o700});
await writeFile(resolve(dir,'deployed-outage.mjs'),bundle,{mode:0o600,flag:'wx'});
const manifest={...identity,bundle_file:'deployed-outage.mjs',bundle_bytes:bundle.length,bundle_sha256:createHash('sha256').update(bundle).digest('hex'),built_at:new Date().toISOString(),deployment_mode:'wrangler no_bundle=true; immutable emitted module'};
await writeFile(resolve(dir,'build-manifest.json'),JSON.stringify(manifest,null,2)+'\n',{mode:0o600,flag:'wx'});
console.log(JSON.stringify(manifest));
