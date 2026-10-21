// On-demand advisory discovery. No job history, CI webhook mirror, or periodic poller.
export async function scanHostedCapacity(
  organization:string,
  read:(path:string)=>Promise<{status:number;body:any;link?:string;deferred?:boolean}>,
):Promise<{busy:number;requests:number;repositories:number;runs:number;observed_at:number}> {
  let requests=0;const deadline=Date.now()+15000;
  async function pages(path:string,key?:string):Promise<any[]> {
    const rows:any[]=[];
    for(let page=1;page<=20;page++) {
      if(++requests>80 || Date.now()>deadline)throw new Error('capacity_scan_limit');
      const response=await read(path+(path.includes('?')?'&':'?')+'per_page=100&page='+page);
      const values=key?response.body?.[key]:response.body;
      if(response.deferred || response.status!==200 || !Array.isArray(values))throw new Error('capacity_inventory_unavailable');
      rows.push(...values);
      if(!response.link?.includes('rel="next"')) {
        if(key && (!Number.isSafeInteger(response.body.total_count) || response.body.total_count>rows.length))throw new Error('capacity_incomplete_inventory');
        return rows;
      }
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
  let busy=0;
  const activeRuns:{prefix:string;id:string}[]=[];
  await each(all.filter(r=>!r.archived),async repo=>{
    const prefix='/repos/'+encodeURIComponent(organization)+'/'+encodeURIComponent(repo.name);
    for(const run of await pages(prefix+'/actions/runs?status=in_progress','workflow_runs')) {
      if(!/^\d+$/.test(String(run.id)))throw new Error('capacity_invalid_run');
      activeRuns.push({prefix,id:String(run.id)});
    }
  });
  await each(activeRuns,async run=>{
    for(const job of await pages(run.prefix+'/actions/runs/'+run.id+'/jobs','jobs')) {
      if(typeof job.status!=='string' || !Array.isArray(job.labels) || job.labels.some((l:unknown)=>typeof l!=='string'))throw new Error('capacity_invalid_job');
      if(job.status==='in_progress' && !job.labels.some((l:string)=>l==='self-hosted' || l.startsWith('nscloud-') || l.startsWith('namespace-profile-')))busy++;
    }
  });
  return {busy,requests,repositories:all.length,runs:activeRuns.length,observed_at:Date.now()};
}
