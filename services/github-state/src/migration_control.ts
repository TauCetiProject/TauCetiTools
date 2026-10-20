import { Env, digest, repositories, validateQuery } from './protocol';
import { R2_CLASSES, policy } from './retention';
import { AdminError } from './storage';
import { Migration, terminalReceipt, archiveKey, beginExport, exportStep, verifyStep, saveMigration, encodeMigration } from './migration';
import { abandonTarget, activateTarget, importStep, targetConfig, targetOwner, targetPreflight, targetVerifyStep, prepareTargetStep } from './migration_target';

// Expiration invalidates a pre-switch export. Ordinary cron reconciles it;
// recovering an expired controller never silently resumes that export.
export const FREEZE_LEASE_MS=15*60000;
export interface SourceHooks {
  storage:DurableObjectStorage; sql:SqlStorage; compatibility(state:Migration):Promise<void>;
  fence(state:Migration):Promise<void>; assertFence(state:Migration):void;
  verifyStored(state:Migration):Promise<void>; drain(state:Migration):Promise<void>; release(state:Migration):Promise<void>; select(state:Migration):Promise<void>;
}
export async function acceptance(env:Env,repo:string,target:string,object:string):Promise<string> {
  const evidence=await env.ARCHIVE.get(`control/${repo}/migration-acceptance.json`);
  if(!evidence)throw new AdminError('migration_acceptance_missing');
  const text=await evidence.text(),receipt=JSON.parse(text);
  if(receipt.version!==1 || receipt.environment!==env.MIGRATION_ENVIRONMENT || receipt.repository!==repo || receipt.target!==target || receipt.deployment!==env.CF_VERSION?.id || receipt.source_object!==object || !Number.isSafeInteger(receipt.valid_until) || receipt.valid_until<Date.now())throw new AdminError('migration_acceptance_stale');
  if(!Array.isArray(receipt.queries) || !receipt.queries.length || receipt.queries.length>20)throw new AdminError('migration_acceptance_incomplete');
  const configured=repositories(env).find(r=>r.id===repo);
  for(const query of receipt.queries){try{validateQuery(query);}catch{throw new AdminError('migration_shadow_query_invalid');}if(configured && query.repository!==configured.name)throw new AdminError('migration_shadow_query_invalid');}
  if(receipt.activity_numbers!==undefined && (!Array.isArray(receipt.activity_numbers) || receipt.activity_numbers.length>20 || receipt.activity_numbers.some((n:any)=>!Number.isSafeInteger(n) || n<=0)))throw new AdminError('migration_shadow_query_invalid');
  for(const key of ['s0_compatibility','parked_ingress','queue_dlq_ownership','r2_retention_drained','s1_schema_roles','capacity','restore','unchanged_clients','stored_shadow','no_github_refetch','warm_read_p95'])if(receipt.checks?.[key]!==true)throw new AdminError('migration_acceptance_incomplete');
  if(typeof receipt.latency?.baseline_p95_ms!=='number' || typeof receipt.latency?.target_p95_ms!=='number' || !Number.isFinite(receipt.latency.baseline_p95_ms) || !Number.isFinite(receipt.latency.target_p95_ms) || !(receipt.latency.baseline_p95_ms>0) || !(receipt.latency.target_p95_ms>0) || receipt.latency.target_p95_ms>receipt.latency.baseline_p95_ms*1.2)throw new AdminError('migration_latency_regression');
  if(env.MIGRATION_ENVIRONMENT==='production') {
    if(!Array.isArray(receipt.queries) || !receipt.queries.some((q:any)=>q.operation==='survey') || !receipt.queries.some((q:any)=>['open_prs','open_issues'].includes(q.operation)) || !receipt.queries.some((q:any)=>['issue_comments','review_comments','review_threads','reviews','files'].includes(q.operation)) || !Array.isArray(receipt.activity_numbers) || !receipt.activity_numbers.length)throw new AdminError('migration_acceptance_incomplete');
  }
  return digest(text);
}
export async function preflight(env:Env,state:Migration):Promise<void> {
  if(!['test','production'].includes(env.MIGRATION_ENVIRONMENT || '') || env.MIGRATION_ENVIRONMENT!==state.cutover?.environment)throw new AdminError('migration_environment_mismatch');
  state.cutover!.acceptance=await acceptance(env,state.repository,state.target,state.source.object);
  state.cutover!.target_identity={...targetConfig(env)};
  const pinned=await env.ARCHIVE.get(`control/${state.repository}/retention-policy.json`).then(o=>o?.json());
  const retention=pinned ? policy(pinned) : null;
  if(!retention || Object.keys(R2_CLASSES).some(name=>!retention.pinned.includes(name) && !retention.disabled.includes(name)))throw new AdminError('migration_retention_not_pinned');
  await targetPreflight(env,state);
}
export async function controllerStep(env:Env,state:Migration,hooks:SourceHooks):Promise<void> {
  const control=state.cutover!;
  if(state.phase==='abandoned' || state.phase==='complete')return;
  if(!control.target_active && (control.lease_until<=Date.now() || state.source.deployment!==env.CF_VERSION?.id)) {
    state.failure=control.lease_until<=Date.now()?'migration_freeze_lease_expired':'migration_source_identity_changed';control.abort_reason=state.failure;state.phase='aborting';
  }
  // Response loss after target activation must be discovered before aborting.
  if(state.phase==='switching' || state.phase==='aborting') {
    try {
      const backend=await targetOwner(env,state);
      if(backend==='postgres') {control.target_active=true;delete control.abort_reason;control.selected_at ||= Date.now();await saveMigration(env.ARCHIVE,state);}
      else if(backend==='missing') {
        if(control.activation_intent)throw new AdminError('migration_forward_recovery_required');
        state.phase='aborting';control.abort_reason ||= 'migration_target_missing';
      }
    }catch(error) {
      if(state.phase!=='aborting' || control.activation_intent || !(error instanceof AdminError) || !['migration_target_unavailable','migration_target_owner_mismatch','migration_target_configuration','migration_target_identity_mismatch'].includes(error.message))throw error;
    }
  }
  control.epoch++; // Checkpoint sequence; the R2 etag supplies exclusion, not this counter.
  await saveMigration(env.ARCHIVE,state);
  if(state.phase==='aborting' && !control.target_active) {
    try {await abandonTarget(env,state);}catch(error) {
      // The durable abort checkpoint wins before any old controller can save
      // activation intent. With no intent, S2 has never enabled target writes;
      // quarantine an unavailable/removed inactive target and release SQLite.
      if(control.activation_intent || !(error instanceof AdminError) || !['migration_target_unavailable','migration_target_owner_mismatch','migration_target_configuration','migration_target_identity_mismatch'].includes(error.message))throw error;
      control.target_quarantined={reason:error.message,at:Date.now()};
    }
    await hooks.drain(state);await hooks.release(state);state.phase='abandoned';state.failure=control.abort_reason || state.failure;
  } else if(!control.target_active && state.phase==='preflighting') {
    await hooks.compatibility(state);
  } else if(!control.target_active && state.phase==='freezing') {
    await hooks.drain(state);await hooks.fence(state);control.source_fenced=true;
    await beginExport(env.ARCHIVE,hooks.storage,hooks.sql,state);
  } else if(!control.target_active && ['exporting','verifying','source_verified','importing','target_verifying','preparing','switching'].includes(state.phase)) {
    hooks.assertFence(state);
    if(state.phase==='exporting')await exportStep(env.ARCHIVE,hooks.sql,state,250);
    else if(state.phase==='verifying')await verifyStep(env.ARCHIVE,hooks.sql,state);
    else if(state.phase==='source_verified')state.phase='importing';
    else if(state.phase==='importing')await importStep(env,state);
    else if(state.phase==='target_verifying')await targetVerifyStep(env,state);
    else if(state.phase==='preparing')await prepareTargetStep(env,state);
    else if(state.phase==='switching') {
      if(!control.shadow?.complete)await hooks.verifyStored(state);
      await saveMigration(env.ARCHIVE,state);
      if(!control.shadow?.complete) {control.lease_until=Date.now()+FREEZE_LEASE_MS;await saveMigration(env.ARCHIVE,state);return;}
      control.activation_intent=true;await saveMigration(env.ARCHIVE,state);
      await activateTarget(env,state);
      control.target_active=true;control.selected_at=Date.now();
      await saveMigration(env.ARCHIVE,state);
    }
  }
  if(control.target_active) {await hooks.select(state);state.phase='replaying';}
  control.lease_until=Date.now()+FREEZE_LEASE_MS;
  await saveMigration(env.ARCHIVE,state);
  if(['abandoned','complete'].includes(state.phase))await terminalReceipt(env.ARCHIVE,state);
  else if(state.phase==='replaying')await env.ARCHIVE.put(archiveKey(state.repository,state.generation)+'/cutover-receipt.json',encodeMigration(state));
}
