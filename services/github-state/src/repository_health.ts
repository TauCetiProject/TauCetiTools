import { RepositoryStore, Statement, collectionJoin } from './repository_store';
import { queryKey } from './protocol';
import { SURVEY_CONTRACT, SURVEY_INDEX_VERSION } from './survey';

// Capacity values are operator-prepared. Status reads never calculate table
// sizes, run ANALYZE, or call GitHub. Related counters share one HTTP batch.
export async function repositoryHealth(store:RepositoryStore):Promise<Record<string,any>> {
  const now=Date.now();
  const statements:Record<string,Statement>={
    meta:{sql:"SELECT key,value FROM meta WHERE key IN ('repository','revision','bootstrap_inventory','last_webhook','webhook_apply_ms','last_repair','last_upstream_failure','latest_applied_input')"},
    survey:{sql:`SELECT COUNT(*) open_prs,SUM(h.contract=?) hydrated,SUM(s.number IS NOT NULL) materialized FROM members m ${collectionJoin(store)} resources r ON r.kind='pr' AND r.id=m.id LEFT JOIN survey_hydrated h ON h.number=r.id LEFT JOIN survey_state s ON s.number=r.id WHERE m.collection=? AND r.tombstone=0`,bindings:[SURVEY_INDEX_VERSION,queryKey({operation:'open_prs'})]},
    failures:{sql:'SELECT COUNT(*) n FROM survey_hydration_failures'},
    coverage:{sql:'SELECT operation,COUNT(*) resources,SUM(complete) complete,MIN(verified) oldest_verified FROM collections GROUP BY operation'},
    jobs:{sql:'SELECT COUNT(*) total,SUM(due<=?) due,MIN(started) oldest_started FROM jobs',bindings:[now]},
    lanes:{sql:'SELECT lane,COUNT(*) jobs,SUM(due<=? AND lease_until<=?) eligible,SUM(lease_until>?) inflight,MIN(CASE WHEN due<=? AND lease_until<=? THEN due END) oldest_eligible,MIN(requested_at) oldest_requested FROM jobs GROUP BY lane',bindings:Array(5).fill(now)},
    waits:{sql:'SELECT lane,wait_reason,COUNT(*) jobs FROM jobs WHERE wait_reason IS NOT NULL GROUP BY lane,wait_reason'},
    repair_operations:{sql:"SELECT json_extract(query,'$.operation') operation,COUNT(*) jobs,SUM(due<=?) due,MIN(due) next_due,MAX(attempt) attempts FROM jobs GROUP BY operation",bindings:[now]}
  };
  const entries=Object.entries(statements),results=await store.batch(entries.map(([,s])=>s));
  const rows=Object.fromEntries(entries.map(([name],i)=>[name,results[i].toArray()]));
  const meta=Object.fromEntries(rows.meta.map(r=>[r.key,JSON.parse(r.value)]));
  return {...meta,revision:meta.revision || 0,survey:{contract:SURVEY_CONTRACT,...rows.survey[0],failures:rows.failures[0].n},...Object.fromEntries(Object.entries(rows).filter(([name])=>!['meta','survey','failures'].includes(name)).map(([name,data])=>[name,name==='jobs'?data[0]:data]))};
}
