// Webhook snapshots describe more fields than the action actually changed.
// Keep this mapping local to PR ingestion, rather than versioning every field.
export function webhookPR(prior:any,row:any,body:any):any|null {
  // Lifecycle snapshots also carry the final/reopened head needed by existing
  // terminal collection and reporting rules. Preserve that behavior.
  if(['opened','synchronize','closed','reopened'].includes(body.action))return row;
  if(!prior)return null;
  // A contradictory metadata snapshot must not timestamp-fence the actual
  // synchronize/close event that may be delivered after it.
  const patch:any={updatedAt:row.headRefOid!==prior.headRefOid || row.state!==prior.state?prior.updatedAt:row.updatedAt};
  switch(body.action){
    case 'labeled': case 'unlabeled': {
      if(!body.label?.name)return null;
      patch.labels=(prior.labels || []).filter((l:any)=>l.name!==body.label.name);
      if(body.action==='labeled')patch.labels.push(body.label);
      break;
    }
    case 'ready_for_review': case 'converted_to_draft':patch.isDraft=row.isDraft;break;
    case 'edited':
      for(const field of ['title','body'])if(body.changes?.[field])patch[field]=row[field];
      if(body.changes?.base)Object.assign(patch,{baseRefName:row.baseRefName,baseRefOid:row.baseRefOid});
      break;
    default:return null;
  }
  return {...prior,...patch};
}

export function equivalentPR(a:any,b:any):boolean {
  const normalize=(value:any):any=>Array.isArray(value)?value.map(normalize):value && typeof value==='object'
    ?Object.fromEntries(Object.keys(value).filter(k=>!k.startsWith('__tauceti_') && value[k]!==undefined).sort().map(k=>[k,normalize(value[k])])):value;
  return JSON.stringify(normalize(a))===JSON.stringify(normalize(b));
}
