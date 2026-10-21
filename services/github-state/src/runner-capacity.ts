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
  let busy=0,runs=0;
  for(const repo of all.filter(r=>!r.archived)) {
    const prefix='/repos/'+encodeURIComponent(organization)+'/'+encodeURIComponent(repo.name);
    const active=await pages(prefix+'/actions/runs?status=in_progress','workflow_runs');
    for(const run of active) {
      if(!/^\d+$/.test(String(run.id)))throw new Error('capacity_invalid_run');
      runs++;
      for(const job of await pages(prefix+'/actions/runs/'+run.id+'/jobs','jobs')) {
        if(typeof job.status!=='string' || !Array.isArray(job.labels) || job.labels.some((l:unknown)=>typeof l!=='string'))throw new Error('capacity_invalid_job');
        if(job.status==='in_progress' && !job.labels.some((l:string)=>l==='self-hosted' || l.startsWith('nscloud-') || l.startsWith('namespace-profile-')))busy++;
      }
    }
  }
  return {busy,requests,repositories:all.length,runs,observed_at:Date.now()};
}
