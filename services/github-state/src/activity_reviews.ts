// Cumulative published counters are an estimate of review passes. Reuse compact
// comment projections; reporting must not read prose, hydrate, or fetch GitHub.
export function scoreboardRounds(fragments:any[],head:string): {count:number,boards:number,basis:string}|null {
  let count=0,boards=0,current=false;
  for(const fragment of fragments) {
    const meta=fragment?.meta;
    if(!meta)continue;
    if(!Number.isSafeInteger(meta.round) || meta.round<0)return null;
    count+=meta.round;boards++;
    current ||= Boolean(head && meta.head_sha===head);
    if(!Number.isSafeInteger(count))return null;
  }
  return boards && current?{count,boards,basis:'scoreboard-round-counters'}:null;
}
