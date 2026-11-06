import {test} from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
const output=await build({stdin:{contents:"export * from './src/repair_io';export {isBackendFailure} from './src/repository_store';",resolveDir:process.cwd()},bundle:true,platform:'node',format:'esm',write:false});
const {repairHop,RepairTransportTimeout,isBackendFailure}=await import('data:text/javascript;base64,'+Buffer.from(output.outputFiles[0].text+'\n//# sourceURL=repair-io-fixture.mjs').toString('base64'));
const group=()=>({pendingIO:new Map()});
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));

test('an expired unconfirmed transport remains tracked and carries backend failure classification',async()=>{
 const g=group();let release,cancels=0;
 const actual=new Promise(resolve=>release=resolve);
 await assert.rejects(repairHop(g,()=>actual,()=>{cancels++;},10),error=>error instanceof RepairTransportTimeout && isBackendFailure(error));
 assert.equal(cancels,1);assert.equal(g.pendingIO.size,1);assert.equal([...g.pendingIO.values()][0].expired,true);
 release();await pause(0);assert.equal(g.pendingIO.size,0);
});

test('a healthy multi-hop pipeline can exceed a single-hop budget without timing out',async()=>{
 const g=group(),start=Date.now();
 for(let page=0;page<8;page++)await repairHop(g,()=>pause(20),undefined,100);
 assert.ok(Date.now()-start>100);assert.equal(g.pendingIO.size,0);
});

test('a confirmed transport cancellation releases tracking without abandoning its operation',async()=>{
 const g=group();let reject;
 const actual=new Promise((_,fail)=>reject=fail);
 await assert.rejects(repairHop(g,()=>actual,()=>reject(Error('canceled by transport')),10));
 await pause(0);assert.equal(g.pendingIO.size,0);
});
