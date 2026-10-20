import {spawnSync} from 'node:child_process';
import {readFileSync,readdirSync} from 'node:fs';
import {resolve} from 'node:path';
import {createHash} from 'node:crypto';
export function sourceFingerprint(cwd) {
 const hash=createHash('sha256');
 const folders=['src','tests','scripts','migrations'],files=['package.json','package-lock.json','wrangler.jsonc','tsconfig.json'];
 for(const folder of folders)for(const file of readdirSync(resolve(cwd,folder),{recursive:true}).sort()) {
  if(!/\.(ts|mjs|py|sql)$/.test(file))continue;
  hash.update(folder+'/'+file+'\0');hash.update(readFileSync(resolve(cwd,folder,file)));
 }
 for(const file of files){hash.update(file+'\0');hash.update(readFileSync(resolve(cwd,file)));}
 const revision=spawnSync('git',['rev-parse','HEAD'],{cwd,encoding:'utf8'});
 const status=spawnSync('git',['status','--porcelain','--',...folders,...files],{cwd,encoding:'utf8'});
 if(revision.status!==0 || status.status!==0)throw Error('Source identity unavailable');
 return {implementation_revision:revision.stdout.trim(),workspace_sha256:hash.digest('hex'),source_dirty:Boolean(status.stdout.trim())};
}
