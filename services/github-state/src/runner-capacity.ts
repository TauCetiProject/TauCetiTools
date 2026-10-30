// On-demand advisory discovery. No job history, CI webhook mirror, or periodic poller.
export async function scanHostedCapacity(
  organization:string,
  read:(path:string)=>Promise<{status:number;body:any;link?:string;deferred?:boolean}>,
):Promise<{routing_version:number;busy:number;namespace_busy?:number;namespace_busy_lower_bound?:number;queued_fleet?:string[][];placement_unavailable?:string;requests:number;repositories:number;runs:number;observed_at:number}> {
  let requests=0;const deadline=Date.now()+15000;
  async function pages(path:string,key?:string):Promise<any[]> {
    const rows:any[]=[];
    for(let page=1;page<=20;page++) {
      if(requests>=80 || Date.now()>deadline)throw new Error('capacity_scan_limit');
      requests++;
      const response=await read(path+(path.includes('?')?'&':'?')+'per_page=100&page='+page);
      const values=key?response.body?.[key]:response.body;
      if(response.deferred || response.status!==200 || !Array.isArray(values))throw new Error('capacity_inventory_unavailable');
      rows.push(...values);
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
  const queued_fleet:string[][]=[];
  const controls=new Set(['pick','recover','retry-status','publish','finalize-merge-group-build','finalize-batch']);
  const seenRuns=new Set<string>();
  const activeRuns:{prefix:string;id:string}[]=[];
  const repos=all.filter(r=>!r.archived);
  async function discover(status:string):Promise<void> {
    await each(repos,async repo=>{
      const prefix='/repos/'+encodeURIComponent(organization)+'/'+encodeURIComponent(repo.name);
      for(const run of await pages(prefix+'/actions/runs?status='+status,'workflow_runs')) {
        if(!/^\d+$/.test(String(run.id)))throw new Error('capacity_invalid_run');
        const key=prefix+'/'+run.id;
        if(!seenRuns.has(key)){seenRuns.add(key);activeRuns.push({prefix,id:String(run.id)});}
      }
    });
  }
  async function jobs(runs:typeof activeRuns):Promise<void> {
    await each(runs,async run=>{
      for(const job of await pages(run.prefix+'/actions/runs/'+run.id+'/jobs','jobs')) {
        if(typeof job.status!=='string' || !Array.isArray(job.labels) || job.labels.some((l:unknown)=>typeof l!=='string'))throw new Error('capacity_invalid_job');
        const namespace=job.labels.some((l:string)=>l.startsWith('nscloud-') || l.startsWith('namespace-profile-'));
        if(['queued','in_progress'].includes(job.status) && namespace && !controls.has(String(job.name || '').split(' / ').at(-1)!) && !job.labels.some((l:string)=>l.includes('-2x4-')))namespace_busy++;
        if(job.status==='queued' && job.labels.includes('self-hosted'))queued_fleet.push(job.labels);
        if(job.status==='in_progress' && !job.labels.some((l:string)=>l==='self-hosted' || l.startsWith('nscloud-') || l.startsWith('namespace-profile-')))busy++;
      }
    });
  }
  // The ordinary picker needs the complete running GitHub inventory. Queued
  // workflow discovery is extra placement data: old empty queued runs must not
  // discard that useful answer or force every caller to repeat the scan.
  await discover('in_progress');
  await jobs(activeRuns);
  const running=busy,namespaceLowerBound=namespace_busy,runningRuns=activeRuns.length;
  try {
    await discover('queued');
    const queued=activeRuns.slice(runningRuns);
    // Every additional run needs at least one jobs request. Avoid spending the
    // remaining allowance on an extension which already cannot be completed.
    if(queued.length>80-requests)throw new Error('capacity_scan_limit');
    await jobs(queued);
  } catch(error) {
    return {routing_version:1,busy:running,namespace_busy_lower_bound:namespaceLowerBound,requests,repositories:all.length,runs:runningRuns,observed_at:Date.now(),placement_unavailable:error instanceof Error && error.message.startsWith('capacity_')?error.message:'capacity_inventory_unavailable'};
  }
  return {routing_version:2,busy,namespace_busy,queued_fleet,requests,repositories:all.length,runs:activeRuns.length,observed_at:Date.now()};
}
