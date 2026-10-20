import {createHash} from 'node:crypto';
const sha=bytes=>createHash('sha256').update(bytes).digest('hex');
export function bucket(){
 const objects=new Map(),failures=[];
 const meta=(key,o)=>({key,size:o.bytes.length,customMetadata:o.customMetadata,uploaded:o.uploaded,etag:o.etag});
 const check=(op,key)=>{const i=failures.findIndex(f=>f(op,key));if(i>=0){failures.splice(i,1);throw new Error('R2 '+op+' failed');}};
 return {objects,failures,
  async head(key){check('head',key);const o=objects.get(key);return o?meta(key,o):null;},
  async get(key){check('get',key);const o=objects.get(key);return o?{...meta(key,o),text:async()=>o.bytes.toString(),json:async()=>JSON.parse(o.bytes.toString()),arrayBuffer:async()=>new Uint8Array(o.bytes).buffer}:null;},
  async put(key,value,options={}){check('put',key);const bytes=Buffer.from(typeof value==='string'?value:new Uint8Array(value));
   if(options.sha256 && sha(bytes)!==options.sha256) throw new Error('digest mismatch');
   if(options.onlyIf?.etagDoesNotMatch==='*' && objects.has(key)) return null;
   if(options.onlyIf?.etagMatches && objects.get(key)?.etag!==options.onlyIf.etagMatches) return null;
   objects.set(key,{bytes,customMetadata:options.customMetadata || {},uploaded:new Date(),etag:sha(bytes)});return meta(key,objects.get(key));},
  async delete(keys){for(const key of [].concat(keys)){check('delete',key);objects.delete(key);}},
  async list({prefix='',limit=1000,cursor,startAfter}={}){check('list',prefix);const keys=[...objects.keys()].filter(k=>k.startsWith(prefix) && k>(cursor || startAfter || '')).sort();
   return {objects:keys.slice(0,limit).map(k=>meta(k,objects.get(k))),truncated:limit<keys.length,cursor:keys[Math.min(keys.length,limit)-1]};},
 };
}
