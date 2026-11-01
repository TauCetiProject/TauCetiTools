// Read existing Bors receipts, never GitHub or the merge-control API.
export const BORS_RECEIPTS = 'https://bors.taucetiproject.org/repositories/1/candidates?base=main&promoted=true';
const sha = (value:any) => typeof value==='string' && /^[a-f0-9]{40}$/.test(value);

export function promotionReceipts(value:any, repository='TauCetiProject/TauCeti', branch='main') {
  if(value?.schema!=='tauceti-bors.candidates/v1' || value.repo!==repository || value.base!==branch || value.timestamp_unit!=='unix_microseconds' || !Array.isArray(value.candidates)) throw new Error('Invalid Bors receipt envelope');
  const receipts=[];
  for(const candidate of value.candidates) {
    const snapshot=candidate.snapshot, events=candidate.events;
    if(snapshot?.contract!=='tauceti-bors.candidate/v1' || snapshot.base!==branch || !sha(snapshot.base_sha) || !Array.isArray(snapshot.members) || !Array.isArray(events)) continue;
    for(const event of events) {
      if(event.kind!=='promoted' || !['github.base_push_response','github.base_recovery_read'].includes(event.source) || !Number.isSafeInteger(event.observed_at) || event.observed_at<=0 || event.details?.base!==branch || event.details.base_sha!==snapshot.base_sha || !sha(event.details.commit)) continue;
      // A green CI result or a staging push alone is never landing evidence.
      if(!events.some((e:any)=>e.kind==='constructed' && e.details?.commit===event.details.commit && e.details.base_sha===snapshot.base_sha)) continue;
      for(const member of snapshot.members) {
        if(Number.isSafeInteger(member.pr) && member.pr>0 && sha(member.head_sha)) receipts.push({number:member.pr,head:member.head_sha,at:event.observed_at,source:event.source==='github.base_push_response'?'bors-main-push-receipt':'bors-main-recovery-observation'});
      }
    }
  }
  return receipts;
}

export async function readPromotionReceipts(fetcher:typeof fetch=fetch, url=BORS_RECEIPTS, repository='TauCetiProject/TauCeti', branch='main') {
  const response=await fetcher(url,{method:'GET',redirect:'manual',signal:AbortSignal.timeout(5000),headers:{'User-Agent':'TauCetiGitHubState/1.0','Accept':'application/json'}});
  if(response.status!==200 || !response.body) throw new Error('Bors receipts HTTP '+response.status);
  return promotionReceipts(await response.json(),repository,branch);
}
