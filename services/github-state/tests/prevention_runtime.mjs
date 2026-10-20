import {build} from 'esbuild';
const shim={name:'context',setup(b){b.onResolve({filter:/^cloudflare:workers$/},()=>({path:'context',namespace:'fixture'}));b.onLoad({filter:/.*/,namespace:'fixture'},()=>({contents:'export class DurableObject {constructor(ctx,env){this.ctx=ctx;this.env=env}}',loader:'js'}));}};
export const load=async entry=>import('data:text/javascript;base64,'+Buffer.from((await build({entryPoints:[entry],bundle:true,platform:'node',format:'esm',write:false,plugins:[shim]})).outputFiles[0].text).toString('base64'));
export const worker=(await load('src/index.ts')).default;
export const ingress=await load('src/ingress.ts');
export const monitoring=await load('src/monitoring.ts');
export const queueMonitoring=await load('src/queue_monitoring.ts');
export const providerMonitoring=await load('src/provider_monitoring.ts');
export const retention=await load('src/retention.ts');
