import {test} from 'node:test';
import assert from 'node:assert/strict';
import {installationFixture,InstallationScheduler} from './repair_installation_fixture.mjs';

test('accounting estimates preserve exact results and use a recent covering range',async(t)=>{
 const f=installationFixture();t.after(()=>f.close());
 const now=Date.now();t.mock.method(Date,'now',()=>now);
 const insert=f.db.prepare('INSERT INTO accounting(at,operation,api,points,shape,status,latency) VALUES(?,?,?,?,?,200,10)');
 f.db.exec('BEGIN');
 for(let i=0;i<30000;i++)insert.run(now-(2+i%166)*3600000,'issue_comments','graphql',99,'older');
 for(const row of [
  [now-1,'issue_comments','graphql',3,'a'],
  [now-2,'measurement_issue_comments','graphql',7,'a'],
  [now-3,'issue_comments','graphql',9,'b'],
  [now-4,'issue_comments','graphql',null,'empty'],
  [now-3600000,'issue_comments','graphql',100,'boundary'],
  [now-1,'another_operation','graphql',200,'a'],
  [now-1,'issue_comments','core',300,'a'],
 ])insert.run(...row);
 f.db.exec('COMMIT');
 const queries=[],exec=f.ctx.storage.sql.exec;
 f.ctx.storage.sql.exec=(sql,...args)=>{queries.push({sql,args});return exec(sql,...args);};
 for(const [operation,api,shape,fallback] of [
  ['issue_comments','graphql','a',1],['issue_comments','graphql','b',1],
  ['issue_comments','graphql',undefined,1],['issue_comments','graphql','boundary',2],
  ['issue_comments','graphql','empty',4],['missing','graphql',undefined,5],
  ['issue_comments','graphql','a',12],
 ]) {
  const value=f.scheduler.estimate(operation,api,shape,fallback),{sql,args}=queries.at(-1);
  const reference=f.db.prepare(sql.replace('FROM accounting','FROM accounting NOT INDEXED')).get(...args).cost;
  assert.equal(value,Math.max(fallback,reference||0));
  const plan=f.db.prepare('EXPLAIN QUERY PLAN '+sql).all(...args).map(row=>row.detail).join('\n');
  assert.match(plan,/USING COVERING INDEX accounting_estimate.*api=\?.*operation=\?.*at>\?/);
 }
 const before=queries.length;
 assert.equal(f.scheduler.estimate('issue_comments','core','a',2),1);
 assert.equal(queries.length,before,'REST estimates retain their constant-cost path');
 // Exercise the production latency comparator without making an upstream read.
 f.scheduler.pressure=()=>1;
 f.scheduler.http=async()=>({status:200,body:[],elapsed_ms:1});
 await f.scheduler.read({repository:f.repos[0],query:{operation:'issue_comments',args:{number:7}},page:1});
 const latency=queries.filter(({sql})=>sql.includes('AVG(latency)'));
 assert.ok(latency.length,'the production API comparator queried latency');
 for(const {sql,args} of latency) {
  const plan=f.db.prepare('EXPLAIN QUERY PLAN '+sql).all(...args).map(row=>row.detail).join('\n');
  assert.match(plan,/USING INDEX accounting_estimate.*api=\?.*operation=\?.*at>\?/);
 }
});

test('legacy accounting columns upgrade before the index and restarts preserve ledger rows',async(t)=>{
 class Legacy extends InstallationScheduler {
  constructor(ctx,env) {
   ctx.storage.sql.exec('CREATE TABLE accounting(id INTEGER PRIMARY KEY AUTOINCREMENT,at INTEGER,operation TEXT,api TEXT,method TEXT,endpoint TEXT,status INTEGER,points INTEGER,latency INTEGER,bytes INTEGER)');
   ctx.storage.sql.exec('INSERT INTO accounting(at,operation,api,points) VALUES(?,?,?,?)',Date.now(),'issue_comments','graphql',4);
   super(ctx,env);
  }
 }
 const f=installationFixture(Legacy);t.after(()=>f.close());
 assert.equal(f.scheduler.estimate('issue_comments','graphql'),4);
 const writes=f.db.prepare('SELECT total_changes() n').get().n;
 const restarted=new InstallationScheduler(f.ctx,f.env);
 assert.equal(restarted.estimate('issue_comments','graphql'),4);
 assert.equal(f.db.prepare('SELECT total_changes() n').get().n,writes);
 assert.equal(f.db.prepare('SELECT COUNT(*) n FROM accounting').get().n,1);
});

test('optional index failure preserves estimates and the next startup can recover',async(t)=>{
 class IndexUnavailable extends InstallationScheduler {
  constructor(ctx,env) {
   const exec=ctx.storage.sql.exec;
   ctx.storage.sql.exec=(sql,...args)=>{
    if(sql.startsWith('CREATE INDEX IF NOT EXISTS accounting_estimate'))throw new Error('database is full');
    return exec(sql,...args);
   };
   super(ctx,env);
   ctx.storage.sql.exec=exec;
  }
 }
 const f=installationFixture(IndexUnavailable);t.after(()=>f.close());
 f.db.prepare('INSERT INTO accounting(at,operation,api,points) VALUES(?,?,?,?)').run(Date.now(),'issue_comments','graphql',6);
 assert.equal(f.scheduler.estimate('issue_comments','graphql'),6);
 assert.equal(f.db.prepare("SELECT COUNT(*) n FROM sqlite_master WHERE name='accounting_estimate'").get().n,0);
 const restarted=new InstallationScheduler(f.ctx,f.env);
 assert.equal(restarted.estimate('issue_comments','graphql'),6);
 assert.equal(f.db.prepare("SELECT COUNT(*) n FROM sqlite_master WHERE name='accounting_estimate'").get().n,1);
});
