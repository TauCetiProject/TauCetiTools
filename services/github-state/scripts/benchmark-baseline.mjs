import {execFileSync} from 'node:child_process';
import {readFileSync,readdirSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {resolve,relative} from 'node:path';
import {build} from 'esbuild';
export const fingerprintFiles=()=>['../../tauceti_tools/github_catalog.json','package-lock.json',...readdirSync('src',{recursive:true}).filter(name=>name.endsWith('.ts')).map(name=>'src/'+name)].sort();
export const implementationFingerprint=()=>{const hash=createHash('sha256');for(const name of fingerprintFiles())hash.update(name+'\0').update(readFileSync(name)).update('\0');return hash.digest('hex');};
export async function baseline(ref=process.env.BASELINE_REF || '2732eb5'){
 const commit=execFileSync('git',['rev-parse',ref],{encoding:'utf8'}).trim();
 const output=await build({stdin:{contents:"export {RepositoryState} from './repository';export {PostgresStore} from './repository_store';export {InstallationScheduler} from './installation';",resolveDir:resolve('src'),loader:'ts'},bundle:true,platform:'node',format:'esm',write:false,plugins:[
  {name:'baseline-source',setup(b){b.onLoad({filter:/\.ts$/},args=>{
   if(!args.path.startsWith(resolve('src')+'/'))return;
   return {contents:execFileSync('git',['show',commit+':services/github-state/src/'+relative(resolve('src'),args.path)],{encoding:'utf8'}),loader:'ts'};
  });b.onLoad({filter:/github_catalog\.json$/},args=>args.path===resolve('../../tauceti_tools/github_catalog.json')?{contents:execFileSync('git',['show',commit+':tauceti_tools/github_catalog.json'],{encoding:'utf8'}),loader:'json'}:undefined);}},
  {name:'context',setup(b){b.onResolve({filter:/^cloudflare:workers$/},()=>({path:'context',namespace:'fixture'}));b.onLoad({filter:/.*/,namespace:'fixture'},()=>({contents:'export class DurableObject{constructor(ctx,env){this.ctx=ctx;this.env=env}}',loader:'js'}));}},
 ]});
 return {commit,...await import('data:text/javascript;base64,'+Buffer.from(output.outputFiles[0].text+'\n//# sourceURL=baseline-benchmark.mjs').toString('base64'))};
}
