// On-demand advisory discovery. No job history, CI webhook mirror, or periodic poller.
type CapacityJob={id:number|string;name?:string;status:string;labels:string[];runner_id?:number;runner_name?:string;inventory_size?:number};
export type JobLabelCache={
  get:(key:string,revision:string)=>CapacityJob[]|undefined;
  put:(key:string,revision:string,jobs:CapacityJob[])=>void;
  getRanges?:(key:string)=>number[]|undefined;
  putRanges?:(key:string,boundaries:number[])=>void;
};
export async function scanHostedCapacity(
  organization:string,
  read:(path:string,body?:unknown)=>Promise<{status:number;body:any;link?:string;deferred?:boolean}>,
  namespaceTarget=20,
  cache?:JobLabelCache,
):Promise<{routing_version:number;busy:number;running_jobs:Record<string,number>;namespace_busy?:number;namespace_busy_lower_bound?:number;queued_fleet?:string[][];placement_unavailable?:string;requests:number;repositories:number;runs:number;observed_at:number;job_cache_hits:number;job_cache_writes:number;elapsed_ms:number;running_requests:number}> {
  // Warm production scans exceed fifteen seconds during ordinary queue churn.
  // Keep five seconds of drain headroom inside the existing 25-second client.
  let requests=0;const started=Date.now(),deadline=started+20000;
  let job_cache_hits=0,job_cache_writes=0;
  if(!Number.isSafeInteger(namespaceTarget) || namespaceTarget<1 || namespaceTarget>10000)throw new Error('capacity_invalid_namespace_target');
  let queuedPagination=false;
  const concurrency=4;
  let active=0;const waiters:(()=>void)[]=[];
  async function request(path:string,body?:unknown) {
    if(active<concurrency)active++;else await new Promise<void>(resolve=>waiters.push(resolve));
    try {
      if(requests>=80 || Date.now()>deadline)throw new Error('capacity_scan_limit');
      requests++;return await read(path,body);
    }finally {
      const next=waiters.shift();if(next)next();else active--;
    }
  }
  type Range={lower?:number;upper?:number};
  function ranged(path:string,range:Range):string {
    const iso=(at:number)=>new Date(at*1000).toISOString().replace('.000Z','Z');
    const url=new URL(path,'https://api.github.com');
    url.searchParams.set('created',range.lower===undefined?'<='+iso(range.upper!):
      range.upper===undefined?'>='+iso(range.lower):iso(range.lower)+'..'+iso(range.upper));
    return url.pathname+url.search;
  }
  async function settle(work:Promise<unknown>[]):Promise<void> {
    const results=await Promise.allSettled(work);
    for(const result of results)if(result.status==='rejected')throw result.reason;
  }
  async function pages(path:string,key?:string,visit?:(rows:any[])=>Promise<void>,range?:Range,onSplit?:(at:number)=>void,onLeaf?:(range:Range,count:number)=>void):Promise<any[]> {
    if(key==='workflow_runs') {
      const url=new URL(path,'https://api.github.com');url.searchParams.set('exclude_pull_requests','true');
      path=url.pathname+url.search;
    }
    const rows:any[]=[];
    for(let page=1;page<=20;page++) {
      const response=await request(path+(path.includes('?')?'&':'?')+'per_page=100&page='+page);
      const values=key?response.body?.[key]:response.body;
      if(response.deferred || response.status!==200 || !Array.isArray(values))throw new Error('capacity_inventory_unavailable');
      if(range && (range.lower!==undefined || range.upper!==undefined) && values.some((run:any)=>{
        const at=typeof run.created_at==='string'?Date.parse(run.created_at)/1000:NaN;
        return !Number.isSafeInteger(at) || range.lower!==undefined && at<range.lower || range.upper!==undefined && at>range.upper;
      }))queuedPagination=true;
      const more=Boolean(response.link?.includes('rel="next"'));
      let children:Range[]|undefined,unsplittable=false;
      if(more && range!==undefined) {
        const times=values.map((run:any)=>typeof run.created_at==='string'?Date.parse(run.created_at)/1000:NaN);
        if(times.length && times.every((at:number)=>Number.isSafeInteger(at) &&
          (range.lower===undefined || at>=range.lower) && (range.upper===undefined || at<=range.upper))) {
          let split=Math.min(...times);
          if(range.upper!==undefined && split>=range.upper)split=range.upper-1;
          if(range.lower!==undefined && split<=range.lower)split=range.lower+1;
          unsplittable=range.lower!==undefined && split<=range.lower || range.upper!==undefined && split>=range.upper;
          if(!unsplittable){children=[{lower:range.lower,upper:split},{lower:split,upper:range.upper}];onSplit?.(split);}
        }else queuedPagination=true;
      }
      // Parent visitation synchronously records its run IDs before awaiting
      // witnesses. Child listings can overlap that latency without recounting
      // boundary jobs; the one request gate still limits all work to four.
      if(children && visit && cache && namespaceTarget-namespace_busy>5) {
        await settle([visit(values),...children.map(child=>pages(ranged(path,child),key,visit,child,onSplit,onLeaf))]);
        return rows;
      }
      if(visit)await visit(values);else rows.push(...values);
      // Follow Link rather than drifting total_count. Single-page range leaves
      // establish coverage; overlapping split instants retain fractional dates.
      if(!more){if(range)onLeaf?.(range,values.length);return rows;}
      if(unsplittable)throw new Error('capacity_queue_pagination');
      if(children){await settle(children.map(child=>pages(ranged(path,child),key,visit,child,onSplit,onLeaf)));return rows;}
    }
    throw new Error('capacity_scan_limit');
  }
  const all=await pages('/orgs/'+encodeURIComponent(organization)+'/repos');
  if(all.some(r=>typeof r.name!=='string' || typeof r.archived!=='boolean'))throw new Error('capacity_invalid_repository');
  async function each<T>(values:T[],visit:(value:T)=>Promise<void>):Promise<void> {
    let next=0,failure:unknown;
    const worker=async()=>{
      while(!failure && next<values.length) {
        const value=values[next++];
        try {await visit(value);}catch(error) {failure=error;}
      }
    };
    // Finish already-started requests before returning an unavailable snapshot.
    await Promise.all(Array.from({length:Math.min(concurrency,values.length)},worker));
    if(failure)throw failure;
  }
  let busy=0,namespace_busy=0;
  const running_jobs:Record<string,number>={github:0,namespace:0};
  const queued_fleet:string[][]=[];
  const controls=new Set(['pick','recover','retry-status','publish','finalize-merge-group-build','finalize-batch']);
  const seenRuns=new Set<string>(),seenJobs=new Set<string>();
  const namespaceByRepo=new Map<string,number>();
  const namespaceWorkflows=new Set<string>();
  type Run={prefix:string;id:string;path?:string;suite?:string;known?:CapacityJob[];proof?:CapacityJob[];inventorySize?:number};
  const activeRuns:Run[]=[];
  function entry(prefix:string,run:any):Run {
    return {prefix,id:String(run.id),path:run.path,
      suite:typeof run.check_suite_node_id==='string' && run.check_suite_node_id.length<=256?run.check_suite_node_id:undefined};
  }
  let witnessUnavailable=false;
  async function witnesses(runs:Run[]):Promise<void> {
    if(!cache || witnessUnavailable)return;
    const known=runs.filter(run=>run.suite);
    for(const run of known)run.known=cache.get(run.prefix+'/'+run.id,'job-labels-v2');
    let at=0;const batches:{batch:Run[];limits:number[]}[]=[];
    while(at<known.length) {
      const batch:Run[]=[],limits:number[]=[];let slots=0;
      while(at<known.length && batch.length<50) {
        const run=known[at],hint=run.known?.[0]?.inventory_size;
        const limit=Math.max(2,Math.min(100,Math.max(run.known?.length || 2,Number.isSafeInteger(hint)?hint!:0)));
        if(batch.length && slots+limit>200)break;
        batch.push(run);limits.push(limit);slots+=limit;at++;
      }
      batches.push({batch,limits});
    }
    await each(batches,async({batch,limits})=>{
      if(witnessUnavailable)return;
      const variables=Object.fromEntries(batch.map((run,i)=>['id'+i,run.suite]));
      const fields=batch.map((_,i)=>`q${i}:node(id:$id${i}){... on CheckSuite{checkRuns(first:${limits[i]},filterBy:{checkType:ALL}){totalCount pageInfo{hasNextPage} nodes{databaseId name status}}}}`).join(' ');
      const query='query('+batch.map((_,i)=>'$id'+i+':ID!').join(',')+'){'+fields+' rateLimit{cost remaining resetAt}}';
      let response;
      try {response=await request('/graphql',{query,variables});}
      catch(error) {
        if(!(error instanceof Error) || error.message!=='capacity_graphql_reserve')throw error;
        witnessUnavailable=true;return;
      }
      if(response.status!==200 || response.deferred || !response.body?.data){witnessUnavailable=true;return;}
      batch.forEach((run,i)=>{
        const connection=response.body.data['q'+i]?.checkRuns,nodes=connection?.nodes;
        if(Number.isSafeInteger(connection?.totalCount) && connection.totalCount>=0)run.inventorySize=connection.totalCount;
        if(connection?.pageInfo?.hasNextPage!==false || !Array.isArray(nodes) || nodes.length!==connection.totalCount ||
          !nodes.every(node=>node && /^\d+$/.test(String(node.databaseId)) && typeof node.name==='string' &&
            ['QUEUED','IN_PROGRESS','COMPLETED'].includes(node.status)))return;
        if(new Set(nodes.map(node=>String(node.databaseId))).size!==nodes.length)return;
        run.proof=nodes.map(node=>({id:node.databaseId,name:node.name,status:node.status.toLowerCase(),labels:[]}));
      });
    });
  }
  const repos=all.filter(r=>!r.archived);
  async function discover(status:string):Promise<void> {
    await each(repos,async repo=>{
      const prefix='/repos/'+encodeURIComponent(organization)+'/'+encodeURIComponent(repo.name);
      for(const run of await pages(prefix+'/actions/runs?status='+status,'workflow_runs')) {
        if(!/^\d+$/.test(String(run.id)))throw new Error('capacity_invalid_run');
        const key=prefix+'/'+run.id;
        if(!seenRuns.has(key)){seenRuns.add(key);activeRuns.push(entry(prefix,run));}
      }
    });
  }
  async function jobs(runs:typeof activeRuns,queued=false):Promise<void> {
    await witnesses(runs);
    await each(runs,async run=>{
      if(queued && namespace_busy>=namespaceTarget)throw new Error('capacity_namespace_target');
      const key=run.prefix+'/'+run.id;
      // GraphQL supplies a fresh, complete job-ID/status inventory. Only labels
      // and an already-assigned runner are reused: those belong to an immutable
      // job ID, not to a run timestamp or an assumed finished matrix expansion.
      const metadata=new Map((run.known || []).map(job=>[String(job.id),job]));
      const active=run.proof?.filter(job=>job.status!=='completed');
      const reusable=active?.every(job=>{
        const saved=metadata.get(String(job.id));
        return saved && (saved.status==='completed' || job.status!=='in_progress' || Boolean(saved.runner_id && saved.runner_name));
      });
      const observed:CapacityJob[]=[];
      const visit=async(rows:CapacityJob[])=>{
        for(const job of rows) {
          if(!/^\d+$/.test(String(job.id)) || typeof job.status!=='string' || !Array.isArray(job.labels) || job.labels.some((l:unknown)=>typeof l!=='string'))throw new Error('capacity_invalid_job');
          const key=run.prefix+'/'+job.id;
          if(seenJobs.has(key))continue;
          seenJobs.add(key);
          const namespace=job.labels.some((l:string)=>l.startsWith('nscloud-') || l.startsWith('namespace-profile-'));
          // Reporting reuses this already-required running inventory. An assigned
          // runner is evidence of execution; queued labels are only a destination.
          if(job.status==='in_progress' && job.runner_id && job.runner_name) {
            const machine=job.labels.find((label:string)=>/^fleet-[a-f0-9]{32}$/.test(label))
              || /^fleet-[a-f0-9]{12}-/.exec(job.runner_name)?.[0].slice(0,-1);
            const pool=namespace?'namespace':machine || (job.labels.includes('self-hosted')?'self-hosted':'github');
            running_jobs[pool]=(running_jobs[pool] || 0)+1;
          }
          if(['queued','in_progress'].includes(job.status) && namespace && !controls.has(String(job.name || '').split(' / ').at(-1)!) && !job.labels.some((l:string)=>l.includes('-2x4-'))) {
            namespace_busy++;
            namespaceByRepo.set(run.prefix,(namespaceByRepo.get(run.prefix) || 0)+1);
            if(run.path)namespaceWorkflows.add(run.prefix+'/'+run.path);
          }
          if(job.status==='queued' && job.labels.includes('self-hosted'))queued_fleet.push(job.labels);
          if(job.status==='in_progress' && !job.labels.some((l:string)=>l==='self-hosted' || l.startsWith('nscloud-') || l.startsWith('namespace-profile-')))busy++;
          observed.push({id:job.id,name:job.name,status:job.status,labels:job.labels,
            runner_id:job.runner_id,runner_name:job.runner_name});
        }
        if(queued && namespace_busy>=namespaceTarget)throw new Error('capacity_namespace_target');
      };
      if(reusable){
        job_cache_hits++;
        await visit(active!.map(job=>{const saved=metadata.get(String(job.id))!;
          return {...saved,status:saved.status==='completed'?'completed':job.status};}));
      }
      else {
        await pages(run.prefix+'/actions/runs/'+run.id+'/jobs','jobs',visit);
        // This is metadata, never a cached activity count. Incomplete GraphQL
        // pages still use the ordinary live REST traversal without certifying
        // that a previously observed job list was the whole current inventory.
        if(cache && observed.length &&
          JSON.stringify(observed).length<=65536){
          if(run.inventorySize!==undefined)observed[0].inventory_size=Math.min(100,run.inventorySize);
          cache.put(key,'job-labels-v2',observed);job_cache_writes++;
        }
      }
    });
  }
  // The ordinary picker needs the complete running GitHub inventory. Queued
  // workflow discovery is extra placement data: old empty queued runs must not
  // discard that useful answer or force every caller to repeat the scan.
  await discover('in_progress');
  await jobs(activeRuns);
  const running=busy;
  const running_requests=requests;
  const prefix=(name:string)=>'/repos/'+encodeURIComponent(organization)+'/'+encodeURIComponent(name);
  const queuedRepos=[...repos].sort((a,b)=>(namespaceByRepo.get(prefix(b.name)) || 0)-(namespaceByRepo.get(prefix(a.name)) || 0));
  try {
    if(namespace_busy>=namespaceTarget)throw new Error('capacity_namespace_target');
    // Inspect each queued page before fetching another. A large queue must help
    // establish a full pool instead of consuming every request just listing runs.
    const inspect=async(repo:{name:string})=>{
      const repoPrefix=prefix(repo.name);
      const path=repoPrefix+'/actions/runs?status=queued';
      const saved=cache?.getRanges?.(repoPrefix) || [];
      const boundaries=[...new Set(saved.filter(at=>Number.isSafeInteger(at) && at>=1199145600 && at<=Math.floor(Date.now()/1000)+86400))].sort((a,b)=>a-b).slice(0,16);
      const learned=new Set(boundaries);
      const leaves:{range:Range;count:number}[]=[];let complete=false;
      const ranges:Range[]=boundaries.length?[
        {upper:boundaries[0]},
        ...boundaries.slice(1).map((at,i)=>({lower:boundaries[i],upper:at})),
        {lower:boundaries.at(-1)},
      ]:[{}];
      const visit=async(rows:any[])=>{
        const queued:typeof activeRuns=[];
        // Older builds on this page are more likely to have finished placement.
        // Inspect workflows already using Namespace before control-only runs.
        const ordered=[...rows].reverse().sort((a,b)=>Number(namespaceWorkflows.has(repoPrefix+'/'+b.path))-Number(namespaceWorkflows.has(repoPrefix+'/'+a.path)));
        for(const run of ordered) {
          if(!/^\d+$/.test(String(run.id)))throw new Error('capacity_invalid_run');
          const key=repoPrefix+'/'+run.id;
          if(!seenRuns.has(key)) {
            seenRuns.add(key);
            const item=entry(repoPrefix,run);
            queued.push(item);activeRuns.push(item);
          }
        }
        await jobs(queued,true);
      };
      try {
        const scan=(range:Range)=>pages(boundaries.length?ranged(path,range):path,'workflow_runs',visit,range,at=>learned.add(at),(range,count)=>leaves.push({range,count})).then(()=>{});
        if(namespaceTarget-namespace_busy>5)await each(ranges,scan);
        else for(const range of ranges)await scan(range);
        complete=true;
      }finally {
        // Only search boundaries survive a scan, never activity or coverage.
        // Every next scan freshly reads the entire overlapping partition of
        // (-infinity,+infinity), including empty or newly populated ranges.
        let compact:number[];
        if(complete) {
          // Fresh leaf counts only optimize next scan's search. Merge sparse
          // neighbors (including drained queues), leaving arrival headroom.
          leaves.sort((a,b)=>(a.range.lower ?? -Infinity)-(b.range.lower ?? -Infinity));
          while(leaves.length>1) {
            let at=0;
            for(let i=1;i<leaves.length-1;i++)if(leaves[i].count+leaves[i+1].count<leaves[at].count+leaves[at+1].count)at=i;
            const count=leaves[at].count+leaves[at+1].count;
            if(count>80 && leaves.length<=17)break;
            leaves.splice(at,2,{range:{lower:leaves[at].range.lower,upper:leaves[at+1].range.upper},count});
          }
          compact=leaves.slice(1).map(leaf=>leaf.range.lower!);
        }else compact=[...learned].sort((a,b)=>a-b).slice(-16);
        if(complete || compact.length!==saved.length || compact.some((at,i)=>at!==saved[i]))cache?.putRanges?.(repoPrefix,compact);
      }
    };
    // Prove saturation in known Namespace workflows before spending requests
    // on quiet repositories. Independent quiet listings may then run together.
    const preferred=queuedRepos.filter(repo=>(namespaceByRepo.get(prefix(repo.name)) || 0)>0);
    await each(preferred,inspect);
    await each(queuedRepos.filter(repo=>!preferred.includes(repo)),inspect);
    // Offset pages can skip jobs as queued workflows start between requests.
    if(queuedPagination)throw new Error('capacity_queue_pagination');
  } catch(error) {
    // Retain distinct jobs from every completed page, including a partial scan.
    return {routing_version:1,busy:running,running_jobs,namespace_busy_lower_bound:namespace_busy,queued_fleet,requests,repositories:all.length,runs:activeRuns.length,observed_at:Date.now(),job_cache_hits,job_cache_writes,elapsed_ms:Date.now()-started,running_requests,placement_unavailable:error instanceof Error && error.message.startsWith('capacity_')?error.message:'capacity_inventory_unavailable'};
  }
  return {routing_version:2,busy:running,running_jobs,namespace_busy,queued_fleet,requests,repositories:all.length,runs:activeRuns.length,observed_at:Date.now(),job_cache_hits,job_cache_writes,elapsed_ms:Date.now()-started,running_requests};
}
