import {test} from 'node:test';
import assert from 'node:assert/strict';
import {runtime} from './runtime.mjs';

test('refresh is authenticated, repository-scoped, and keeps retry hints out of headers',async()=>{
 const mf=await runtime(async()=>{throw Error('route must delegate to repository owner');},{},{repositoryAlarms:false,repositoryExtension:`async refreshPR(input){return {version:1,repository:input.repository,operation:'pr',status:'unavailable',reason:'primary:core',retry_at:Date.now()+3600000};}`});
 try{
  const body={repository:'org/repo',number:7,expected_head:'a'.repeat(40)};
  const call=(input=body,token='client')=>mf.dispatchFetch('https://test/v1/refresh-pr',{method:'POST',headers:token?{Authorization:'Bearer '+token}:{},body:JSON.stringify(input)});
  assert.equal((await call(body,'')).status,401);
  assert.equal((await call({...body,repository:'other/repo'})).status,403);
  for(const input of [{...body,number:-1},{...body,expected_head:'bad'},{...body,observed_head:'bad'}])assert.equal((await call(input)).status,400);
  const response=await call();assert.equal(response.status,200);assert.equal(response.headers.get('Retry-After'),null);assert.ok((await response.json()).retry_at>Date.now());
 }finally{await mf.dispose();}
});
