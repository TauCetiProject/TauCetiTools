import type {RepairGroup} from './repair_coordinator';
import {StoreUnavailable,markBackendFailure} from './repository_store';

// A conservative bound for one transport hop, matching the platform's maximum
// waitUntil eviction-protection interval. Never a deadline on an entire history,
// scheduling quantum, writer queue, or sequence of successful paginated reads.
export const REPAIR_HOP_MS=15*60*1000;
export const INSTALLATION_HOP_MS=REPAIR_HOP_MS-60000;
export class RepairTransportTimeout extends StoreUnavailable {
  constructor(){super();this.message='repair transport timeout';markBackendFailure(this);}
}
export async function repairHop<T>(group:RepairGroup,action:()=>Promise<T>,cancel?:()=>void,limit=REPAIR_HOP_MS):Promise<T> {
  const actual=Promise.resolve().then(action);
  const entry={wait:undefined as unknown as Promise<unknown>,expired:false};
  group.pendingIO.set(actual,entry);
  const observed=actual.then(value=>{group.pendingIO.delete(actual);return value;},error=>{group.pendingIO.delete(actual);throw error;});
  let timer:ReturnType<typeof setTimeout>|undefined;
  const bounded=Promise.race([observed,new Promise<never>((_,reject)=>{
    timer=setTimeout(()=>{entry.expired=true;try{cancel?.();}catch{}reject(new RepairTransportTimeout());},limit);
  })]).finally(()=>{if(timer!==undefined)clearTimeout(timer);});
  entry.wait=bounded;return bounded;
}
