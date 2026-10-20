// Native component diagnostic; this does not measure full migration throughput.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {Miniflare,convertV4MiniflareOptions} from 'miniflare';

const packageVersion=name=>JSON.parse(readFileSync(new URL('../node_modules/'+name+'/package.json',import.meta.url),'utf8')).version;
const queries={cast:'SELECT SUM(length(CAST(value AS BLOB))) bytes FROM values_table',octets:'SELECT SUM(octet_length(value)) bytes FROM values_table'};
const script=`
export class ByteSizes {
 constructor(ctx){this.ctx=ctx;}
 async fetch(req){
  const sql=this.ctx.storage.sql,path=new URL(req.url).pathname;
  if(path==='/init'){
   sql.exec('CREATE TABLE values_table(value TEXT)');
   const text='x'.repeat(65536);
   for(let n=0;n<1000;n++){
    sql.exec('INSERT INTO values_table VALUES(?)',text);
    if(n%100===99)await this.ctx.storage.sync();
   }
   return Response.json({ready:true});
  }
  if(path==='/capability')return Response.json(sql.exec("SELECT octet_length('a😀') utf8").one());
  const queries=${JSON.stringify(queries)},query=queries[path.slice(1)];
  return query?Response.json(sql.exec(query).one()):new Response(null,{status:404});
 }
}
export default {fetch(req,env){return env.SIZES.get(env.SIZES.idFromName('fixture')).fetch(req);}};
`;
const mf=new Miniflare(convertV4MiniflareOptions({modules:true,script,compatibilityDate:'2026-10-11',durableObjects:{SIZES:{className:'ByteSizes',useSQLite:true}},outboundService:()=>{throw Error('unexpected_network_request');}}));
try{
 const read=async path=>{
  const started=performance.now(),response=await mf.dispatchFetch('https://fixture'+path),milliseconds=performance.now()-started;
  if(!response.ok)throw Error('native_byte_sizing_benchmark_failed');
  return {milliseconds,data:await response.json()};
 };
 await read('/init');assert.equal((await read('/capability')).data.utf8,5);
 for(let n=0;n<3;n++){await read('/cast');await read('/octets');}
 const samples={cast:[],octets:[]};
 for(let n=0;n<10;n++)for(const kind of(n%2?['cast','octets']:['octets','cast'])){
  const sample=await read('/'+kind);assert.equal(sample.data.bytes,65536000);samples[kind].push(sample.milliseconds);
 }
 const summarize=values=>{const sorted=[...values].sort((a,b)=>a-b);return {samples:values.length,p50_ms:sorted[4],p95_ms:sorted[9]};};
 console.log(JSON.stringify({version:1,verified_at:new Date().toISOString(),scope:'component query diagnostic in native workerd SQLite; not end-to-end migration throughput or the public-read latency gate',runtime:{workerd:packageVersion('workerd'),miniflare:packageVersion('miniflare'),node:process.versions.node,compatibility_date:'2026-10-11'},sqlite_octet_length_supported:true,fixture:{rows:1000,text_bytes_per_row:65536,expected_equal_total_bytes:65536000},method:{warmup_per_query:3,samples_per_query:10,order:'alternating',measurement:'Node wall time for native dispatch through response headers; returned byte totals checked for every sample'},queries,results:{cast:summarize(samples.cast),octets:summarize(samples.octets)},samples_ms:samples,github_reference_calls:0,generator:'node scripts/benchmark-migration-bytes.mjs',native_correctness_coverage:'tests/migration.test.mjs: UTF-8 page boundary, oversized row, NULL, BLOB and exact scalar encoding'}));
}finally{await mf.dispose();}
