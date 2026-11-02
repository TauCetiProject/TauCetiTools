// On-demand advisory discovery. No job history, CI webhook mirror, or periodic poller.
export async function scanHostedCapacity(
  organization:string,
  read:(path:string)=>Promise<{status:number;body:any;link?:string;deferred?:boolean}>,
  namespaceTarget=20,
):Promise<{routing_version:number;busy:number;running_jobs:Record<string,number>;namespace_busy?:number;namespace_busy_lower_bound?:number;queued_fleet?:string[][];placement_unavailable?:string;requests:number;repositories:number;runs:number;observed_at:number}> {
  let requests=0;const deadline=Date.now()+15000;
  if(!Number.isSafeInteger(namespaceTarget) || namespaceTarget<1 || namespaceTarget>10000)throw new Error('capacity_invalid_namespace_target');
  let queuedPagination=false;
  async function pages(path:string,key?:string,visit?:(rows:any[])=>Promise<void>):Promise<any[]> {
    const rows:any[]=[];
    for(let page=1;page<=20;page++) {
      if(requests>=80 || Date.now()>deadline)throw new Error('capacity_scan_limit');
      requests++;
      const response=await read(path+(path.includes('?')?'&':'?')+'per_page=100&page='+page);
      const values=key?response.body?.[key]:response.body;
      if(response.deferred || response.status!==200 || !Array.isArray(values))throw new Error('capacity_inventory_unavailable');
      if(path.includes('/actions/runs?status=queued') && response.link?.includes('rel="next"'))queuedPagination=true;
      if(visit)await visit(values);else rows.push(...values);
      // GitHub's live Actions total_count can disagree with the returned rows
      // even on a single page. Follow Link, as the ordinary picker does; the
      // count is not a transactionally consistent pagination boundary.
      if(!response.link?.includes('rel="next"'))return rows;
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
    await Promise.all(Array.from({length:Math.min(3,values.length)},worker));
    if(failure)throw failure;
  }
  let busy=0,namespace_busy=0;
  const running_jobs:Record<string,number>={github:0,namespace:0};
  const queued_fleet:string[][]=[];
  const controls=new Set(['pick','recover','retry-status','publish','finalize-merge-group-build','finalize-batch']);
  const seenRuns=new Set<string>(),seenJobs=new Set<string>();
  const namespaceByRepo=new Map<string,number>();
  const namespaceWorkflows=new Set<string>();
  const activeRuns:{prefix:string;id:string;path?:string}[]=[];
  const repos=all.filter(r=>!r.archived);
  async function discover(status:string):Promise<void> {
    await each(repos,async repo=>{
      const prefix='/repos/'+encodeURIComponent(organization)+'/'+encodeURIComponent(repo.name);
      for(const run of await pages(prefix+'/actions/runs?status='+status,'workflow_runs')) {
        if(!/^\d+$/.test(String(run.id)))throw new Error('capacity_invalid_run');
        const key=prefix+'/'+run.id;
        if(!seenRuns.has(key)){seenRuns.add(key);activeRuns.push({prefix,id:String(run.id),path:run.path});}
      }
    });
  }
  async function jobs(runs:typeof activeRuns,queued=false):Promise<void> {
    await each(runs,async run=>{
      if(queued && namespace_busy>=namespaceTarget)throw new Error('capacity_namespace_target');
      await pages(run.prefix+'/actions/runs/'+run.id+'/jobs','jobs',async rows=>{
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
        }
        if(queued && namespace_busy>=namespaceTarget)throw new Error('capacity_namespace_target');
      });
    });
  }
  // The ordinary picker needs the complete running GitHub inventory. Queued
  // workflow discovery is extra placement data: old empty queued runs must not
  // discard that useful answer or force every caller to repeat the scan.
  await discover('in_progress');
  await jobs(activeRuns);
  const running=busy;
  const prefix=(name:string)=>'/repos/'+encodeURIComponent(organization)+'/'+encodeURIComponent(name);
  const queuedRepos=[...repos].sort((a,b)=>(namespaceByRepo.get(prefix(b.name)) || 0)-(namespaceByRepo.get(prefix(a.name)) || 0));
  try {
    if(namespace_busy>=namespaceTarget)throw new Error('capacity_namespace_target');
    // Inspect each queued page before fetching another. A large queue must help
    // establish a full pool instead of consuming every request just listing runs.
    for(const repo of queuedRepos) {
      const repoPrefix=prefix(repo.name);
      await pages(repoPrefix+'/actions/runs?status=queued','workflow_runs',async rows=>{
        const queued:typeof activeRuns=[];
        // Older builds on this page are more likely to have finished placement.
        // Inspect workflows already using Namespace before control-only runs.
        const ordered=[...rows].reverse().sort((a,b)=>Number(namespaceWorkflows.has(repoPrefix+'/'+b.path))-Number(namespaceWorkflows.has(repoPrefix+'/'+a.path)));
        for(const run of ordered) {
          if(!/^\d+$/.test(String(run.id)))throw new Error('capacity_invalid_run');
          const key=repoPrefix+'/'+run.id;
          if(!seenRuns.has(key)) {
            seenRuns.add(key);
            const entry={prefix:repoPrefix,id:String(run.id),path:run.path};
            queued.push(entry);activeRuns.push(entry);
          }
        }
        await jobs(queued,true);
      });
    }
    // Offset pages can skip jobs as queued workflows start between requests.
    if(queuedPagination)throw new Error('capacity_queue_pagination');
  } catch(error) {
    // Retain distinct jobs from every completed page, including a partial scan.
    return {routing_version:1,busy:running,running_jobs,namespace_busy_lower_bound:namespace_busy,queued_fleet,requests,repositories:all.length,runs:activeRuns.length,observed_at:Date.now(),placement_unavailable:error instanceof Error && error.message.startsWith('capacity_')?error.message:'capacity_inventory_unavailable'};
  }
  return {routing_version:2,busy:running,running_jobs,namespace_busy,queued_fleet,requests,repositories:all.length,runs:activeRuns.length,observed_at:Date.now()};
}
