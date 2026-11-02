// This report reads retained mirror rows only. It never creates provider demand.
export const STAGING_DURATION_SQL = `SELECT j.id AS job_id,r.id AS run_id,
 json_extract(r.data,'$.display_title') AS title,
 json_extract(j.data,'$.started_at') AS started_at,
 json_extract(j.data,'$.completed_at') AS completed_at
 FROM resources j JOIN resources r ON r.kind='workflow_runs' AND r.id=j.parent AND r.tombstone=0
 WHERE j.kind='workflow_jobs' AND j.tombstone=0
 AND json_extract(j.data,'$.name')='sandboxed-build'
 AND json_extract(j.data,'$.status')='completed'
 AND json_extract(r.data,'$.display_title') LIKE 'bors branch=main batch=%'
 AND json_extract(j.data,'$.completed_at')>=?
 ORDER BY json_extract(j.data,'$.completed_at') DESC LIMIT 10001`;

export function stagingDurations(rows:any[], now=Date.now()) {
  const truncated=rows.length>10000, seen=new Set<string>(), samples:{finished:number,seconds:number}[]=[];
  for(const row of rows.slice(0,10000)) {
    // Bind to the exact Bors main-candidate workflow title, not a PR mentioning Bors.
    if(!/^bors branch=main batch=[1-9]\d* head=[a-f0-9]{40} base=[a-f0-9]{40}$/.test(row.title || ''))continue;
    const started=Date.parse(row.started_at),finished=Date.parse(row.completed_at);
    if(!Number.isFinite(started)||!Number.isFinite(finished)||finished<started||finished>now||seen.has(String(row.job_id)))continue;
    seen.add(String(row.job_id));samples.push({finished,seconds:(finished-started)/1000});
  }
  return {schema:'tauceti-ci.staging-durations/v1',observed_at:now/1000,read_only:true,truncated,
    coverage:'retained completed sandboxed-build jobs; missing mirror history is not backfilled by this report',
    windows:[['1h',3600],['3h',10800],['24h',86400],['7d',604800]].map(([window,seconds])=>{
      const selected=samples.filter(s=>s.finished>now-Number(seconds)*1000);
      return {window,seconds,samples:selected.length,mean_seconds:selected.length?selected.reduce((sum,s)=>sum+s.seconds,0)/selected.length:null};
    })};
}
