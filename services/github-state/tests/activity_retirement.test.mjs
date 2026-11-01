import {test} from 'node:test';
import assert from 'node:assert/strict';
import {fixture as sqliteFixture} from './migration_fixture.mjs';
import {postgresFixture} from './postgres_runtime.mjs';
const sha=n=>n.toString(16).padStart(40,'0');
const query=(operation,number)=>({repository:'org/repo',operation,args:{number}});
const key=q=>q.operation+':'+JSON.stringify(q.args);
async function fixture(){const f=process.env.TEST_POSTGRES_URL?await postgresFixture():await sqliteFixture();return {...f,store:f.store||f.state.store,close:f.close||(()=>f.db.close())};}
const header=n=>({number:n,state:'CLOSED',headRefOid:sha(n),baseRefOid:sha(999),changedFiles:1,additions:1,deletions:0});
async function enqueue(f,q,dirty=true){await f.state.enqueue(q,1,Date.now(),dirty);return(await f.store.exec('SELECT * FROM jobs WHERE key=?',key(q))).one();}
async function files(f,n,current=true){
 const q=query('files',n);
 await f.state.put('files',n+':Example.lean',{filename:'Example.lean',status:'added',additions:1,deletions:0,raw_url:'https://raw.githubusercontent.com/org/repo/'+sha(current?n:998)+'/Example.lean'});
 await f.state.membership(key(q),n+':Example.lean',true);
 await f.store.exec('INSERT INTO collections VALUES(?,?,?,?,?,?,?)',key(q),'files',JSON.stringify(q.args),1,Date.now(),0,null);
}

test('ordinary retirement advances past retained final files and removes obsolete dirty histories',async()=>{
 const f=await fixture();try{
  // The first sweep batch consists entirely of final-file obligations to keep.
  for(let n=1;n<=102;n++){await f.state.put('pr',String(n),header(n));await enqueue(f,query('files',n));}
  await f.state.put('pr','200',header(200));
  const review=query('reviews',200),detail=query('pr',200),stale=query('files',201),hot=query('review_threads',200),owned=query('issue_comments',200);
  for(const q of [review,detail,hot,owned])await enqueue(f,q);
  await f.state.put('pr','201',header(201));await files(f,201,false);await enqueue(f,stale);
  await f.state.put('pr','202',header(202));await files(f,202);const final=query('files',202);await enqueue(f,final);
  const missing=query('pr',203);await f.state.put('pr','203',{number:203,state:'CLOSED',headRefOid:sha(203)});await enqueue(f,missing);
  const reaction={repository:'org/repo',operation:'reactions',args:{id:'comment'}};
  await f.state.put('review_comments','comment',{id:'comment',body:'old'},Date.now(),false,'200');await enqueue(f,reaction);
  const unmapped={repository:'org/repo',operation:'statuses',args:{sha:sha(777)}};await enqueue(f,unmapped);
  await f.store.exec('INSERT INTO demand VALUES(?,?)',key(hot),Date.now());
  await f.store.exec('UPDATE jobs SET lease_id=?,lease_until=? WHERE key=?','owner',Date.now()+60000,key(owned));
  const resources=(await f.store.exec('SELECT COUNT(*) n FROM resources')).one().n;
  assert.equal(f.state.quotaRestriction,undefined);
  await f.state.retireQuotaObsolete();assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs WHERE key=?',key(review))).one().n,1);
  for(let i=0;i<3;i++)await f.state.retireQuotaObsolete();
  for(const q of [review,detail,final,reaction,unmapped])assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs WHERE key=?',key(q))).one().n,0,key(q));
  for(const q of [stale,missing,hot,owned])assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs WHERE key=?',key(q))).one().n,1,key(q));
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM resources')).one().n,resources,'retirement preserves stored facts');
  assert.equal(await f.state.complete(final),true);
  await f.state.put('pr','200',{...header(200),state:'OPEN'});await enqueue(f,review);
  assert.equal(await f.state.retireUnneeded((await f.store.exec('SELECT * FROM jobs WHERE key=?',key(review))).one()),false,'reopening reactivates collection');
 }finally{await f.close();}
});

test('final file publication keeps ordinary cadence and retires after header correction without refetching',async()=>{
 const f=await fixture();try{
  const q=query('files',7),incomplete={...header(7),changedFiles:2};await f.state.put('pr','7',incomplete);await files(f,7);
  const job=await f.state.claim(await enqueue(f,q));
  await f.store.exec('INSERT INTO demand VALUES(?,?)',key(q),Date.now());
  await f.state.finishJob(job,q);
  const retained=(await f.store.exec('SELECT * FROM jobs WHERE key=?',key(q))).one();
  assert.equal(retained.dirty,0);assert.ok(retained.due>=Date.now()+await f.state.interval(q)-10000);
  assert.equal(await f.state.activityLOC(7,incomplete),null);
  await f.store.exec('UPDATE jobs SET lease_id=NULL,lease_until=0 WHERE key=?',key(q));
  await f.state.put('pr','7',header(7));
  assert.equal(await f.state.retireUnneeded((await f.store.exec('SELECT * FROM jobs WHERE key=?',key(q))).one()),false,'recent demand protects the final snapshot job');
  await f.store.exec('DELETE FROM demand WHERE key=?',key(q));
  await f.state.retireQuotaObsolete();
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs WHERE key=?',key(q))).one().n,0);
  assert.deepEqual(await f.state.activityLOC(7,header(7)),{added:1,deleted:0,head_sha:sha(7)});
 }finally{await f.close();}
});

test('aged explicit demand survives retirement until its reconciliation finishes',async()=>{
 const f=await fixture();try{
  const q=query('reviews',8);await f.state.put('pr','8',header(8));await enqueue(f,q);
  await f.store.exec('UPDATE jobs SET requested_at=? WHERE key=?',Date.now()-600000,key(q));
  await f.state.retireQuotaObsolete();
  let job=(await f.store.exec('SELECT * FROM jobs WHERE key=?',key(q))).one();
  assert.ok(job);assert.equal(await f.state.retireUnneeded(job),false);
  job=await f.state.claim(job);await f.state.finishJob(job,q);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs WHERE key=?',key(q))).one().n,0);
 }finally{await f.close();}
});

test('missing file blobs retain a claimable job and cannot stall cleanup or decode under the writer',async()=>{
 const f=await fixture();try{
  const q=query('files',9);await f.state.put('pr','9',header(9));await files(f,9);await enqueue(f,q);
  await f.store.exec("UPDATE resources SET data=? WHERE kind='files' AND id=?",JSON.stringify({$r2:'missing-file'}),'9:Example.lean');
  const review=query('reviews',9);await enqueue(f,review);
  const decode=f.state.decode;let reads=0;
  f.state.decode=async function(...args){assert.equal(Boolean(this.store.transactional),false,'file scans stay outside the writer');reads++;return decode.apply(this,args);};
  await f.state.retireQuotaObsolete();
  assert.ok(reads);assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs WHERE key=?',key(review))).one().n,0);
  const job=(await f.store.exec('SELECT * FROM jobs WHERE key=?',key(q))).one();
  assert.ok(job);assert.equal(await f.state.retireUnneeded(job),false);assert.ok(await f.state.claim(job));
 }finally{await f.close();}
});

test('retirement skips failed groups, advances past them and retries after wrapping',async()=>{
 const f=await fixture();try{
  await f.state.put('pr','10',header(10));
  const failed=query('issue_comments',10),later=query('reviews',10);await enqueue(f,failed);await enqueue(f,later);
  const predicate=f.state.obsoleteJobs;let fail=true;
  f.state.obsoleteJobs=async function(group){if(fail && group[0].key===key(failed))throw new Error('temporary read failure');return predicate.call(this,group);};
  await f.state.retireQuotaObsolete();
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs WHERE key=?',key(later))).one().n,0);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs WHERE key=?',key(failed))).one().n,1);
  fail=false;await f.state.retireQuotaObsolete();await f.state.retireQuotaObsolete();
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs WHERE key=?',key(failed))).one().n,0);
 }finally{await f.close();}
});

test('a retained-only sweep never takes a writer and valid files warm their proof before deleting',async()=>{
 const f=await fixture();try{
  const q=query('files',11);await f.state.put('pr','11',header(11));await enqueue(f,q);
  const atomic=f.state.atomic;let writes=0;
  f.state.atomic=async function(...args){writes++;return atomic.apply(this,args);};
  await f.state.retireQuotaObsolete();assert.equal(writes,0);
  await files(f,11);writes=0;await f.state.retireQuotaObsolete();
  const decode=f.state.decode;
  f.state.decode=async function(...args){assert.equal(Boolean(this.store.transactional),false);return decode.apply(this,args);};
  await f.state.retireQuotaObsolete();assert.ok(writes);
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs WHERE key=?',key(q))).one().n,0);
 }finally{await f.close();}
});

test('terminal detail uses sufficient typed output facts and tolerates unrelated escaped prose',async()=>{
 const f=await fixture();try{
  const cases=[
   [{body:'unrelated\u0000\ud800'},true],
   [{changedFiles:'1'},false],
   [{headRefOid:123},false],
   [{baseRefOid:123},false],
   [{additions:-1},false],
   [{state:'MERGED',mergedAt:null},false],
   [{state:'MERGED',mergedAt:'2026-10-31T19:00:00Z'},true],
  ];
  for(const [i,[change,expected]] of cases.entries()){
   const n=300+i,q=query('pr',n);await f.state.put('pr',String(n),{...header(n),...change});
   assert.equal(await f.state.retireUnneeded(await enqueue(f,q)),expected,JSON.stringify(change));
  }
 }finally{await f.close();}
});

test('review histories publish cached comments without eager reaction jobs; explicit reactions still queue',async()=>{
 const f=await fixture();try{
  await f.state.put('pr','12',{...header(12),state:'OPEN'});
  for(const operation of ['review_comments','review_threads']) {
   const q=query(operation,12),job=await f.state.claim(await enqueue(f,q));
   const comment={id:operation==='review_comments'?'121':'122',node_id:'node',body:'review',created_at:'2026-10-31T19:00:00Z'};
   const data=operation==='review_comments'?[comment]:[{id:'thread',isResolved:false,comments:{nodes:[{id:'node',fullDatabaseId:'122',body:'review',createdAt:comment.created_at,updatedAt:comment.created_at,author:{login:'peer'}}],pageInfo:{hasNextPage:false}}}];
   await f.state.refresh(job,{api:operation==='review_comments'?'core':'graphql',data});
   assert.equal((await f.store.exec("SELECT COUNT(*) n FROM jobs WHERE key LIKE 'reactions:%'")).one().n,0);
  }
  assert.equal((await f.state.rows(query('review_comments',12))).length,1);
  const explicit={repository:'org/repo',operation:'reactions',args:{id:'122'}};
  await f.state.enqueue(explicit);
  await f.state.noteDemand(explicit);
  assert.ok((await f.store.exec('SELECT * FROM jobs WHERE key=?',key(explicit))).one());
 }finally{await f.close();}
});

test('background retirement validates real R2 files outside the writer and preserves a racing lease',async()=>{
 const f=await fixture();try{
  for(const n of [13,14,15]){await f.state.put('pr',String(n),header(n));await enqueue(f,query('files',n));}
  for(const n of [14,15]){
   await files(f,n);
   const blob='retirement-file-'+n,data=JSON.stringify({filename:'Example.lean',status:'added',additions:1,deletions:0,raw_url:'https://raw.githubusercontent.com/org/repo/'+sha(n)+'/Example.lean'});
   await f.env.ARCHIVE.put(blob,data);
   await f.store.exec("UPDATE resources SET data=? WHERE kind='files' AND id=?",JSON.stringify({$r2:blob}),n+':Example.lean');
  }
  const decode=f.state.decode,archive=f.env.ARCHIVE,predicate=f.state.obsoleteJobs;let decoded=0,raced=false;
  f.state.decode=async function(...args){assert.equal(Boolean(this.store.transactional),false);assert.notEqual(this.env.ARCHIVE,archive,'background validation uses its timed R2 scope');decoded++;return decode.apply(this,args);};
  f.state.obsoleteJobs=async function(group){
   const result=await predicate.call(this,group);
   if(!this.store.transactional && !raced){raced=true;await f.store.exec('UPDATE jobs SET lease_id=?,lease_until=? WHERE key=?','new-owner',Date.now()+60000,key(query('files',14)));}
   return result;
  };
  f.state.startRetirementMaintenance();const flight=f.state.retirementFlight;assert.ok(flight);await flight;
  assert.equal(decoded,2);assert.equal(f.state.retirementFlight,undefined);assert.equal(f.state.activeOperations,0);
  assert.ok(f.state.retirementAfter,'the background scope advances the owner cursor');
  assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs WHERE key=?',key(query('files',15)))).one().n,0);
  for(const n of [13,14])assert.equal((await f.store.exec('SELECT COUNT(*) n FROM jobs WHERE key=?',key(query('files',n)))).one().n,1);
 }finally{await f.state.retirementFlight;await f.close();}
});
