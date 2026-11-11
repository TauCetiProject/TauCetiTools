import {test} from 'node:test';
import assert from 'node:assert/strict';
import {runtime} from './runtime.mjs';

const extension=`async refreshPR(input){return {version:1,repository:input.repository,operation:'pr',status:'unavailable',reason:'primary:core',retry_at:Date.now()+3600000};}`;
const body={repository:'org/repo',number:7,expected_head:'a'.repeat(40)};
const caller=mf=>(input=body,token='client')=>mf.dispatchFetch('https://test/v1/refresh-pr',{method:'POST',headers:token?{Authorization:'Bearer '+token}:{},body:JSON.stringify(input)});

test('refresh is repository-scoped and keeps retry hints out of headers',async()=>{
 const mf=await runtime(async()=>{throw Error('route must delegate to repository owner');},{},{repositoryAlarms:false,repositoryExtension:extension});
 try{
  const call=caller(mf);
  assert.equal((await call(body,'')).status,403,'a private repository still requires its client token');
  assert.equal((await call({...body,repository:'other/repo'})).status,403);
  for(const input of [{...body,number:-1},{...body,expected_head:'bad'},{...body,observed_head:'bad'}])assert.equal((await call(input)).status,400);
  const response=await call();assert.equal(response.status,200);assert.equal(response.headers.get('Retry-After'),null);assert.ok((await response.json()).retry_at>Date.now());
 }finally{await mf.dispose();}
});

test('a public repository accepts anonymous refreshes',async()=>{
 const mf=await runtime(async()=>{throw Error('route must delegate to repository owner');},{PUBLIC_READ_REPOSITORIES:'["org/repo"]'},{repositoryAlarms:false,repositoryExtension:extension});
 try{
  const call=caller(mf);
  const response=await call(body,'');assert.equal(response.status,200);assert.ok((await response.json()).retry_at>Date.now());
  assert.equal((await call({...body,expected_head:'bad'},'')).status,400);
  assert.equal((await call({...body,repository:'other/repo'},'')).status,403);
 }finally{await mf.dispose();}
});
