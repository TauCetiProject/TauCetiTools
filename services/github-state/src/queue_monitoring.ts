import { Env } from './protocol';
import { timedBucket, within } from './monitoring';
const key='control/queue-metrics.json';
// One scheduled owner samples the existing bindings. Never consumes or sends a message.
export async function prepareQueueMetrics(env:Env):Promise<void> {
  const archive=timedBucket(env.ARCHIVE),old:any=await archive.get(key).then(o=>o?.json()).catch(()=>null);
  const attempt=Date.now();
  const entries=await Promise.all(([['queue',env.EVENTS],['dlq',env.EVENTS_DLQ]] as const).map(async([name,binding])=>{
    try {
      if(!binding?.metrics)throw Error('binding_unavailable');
      const value=await within(binding.metrics(),1500);
      if(![value.backlogCount,value.backlogBytes].every(n=>Number.isSafeInteger(n) && n>=0))throw Error('malformed_metrics');
      const oldest=value.oldestMessageTimestamp?.getTime();
      if(oldest!==undefined && (!Number.isSafeInteger(oldest) || oldest<0))throw Error('malformed_metrics');
      return [name,{at:attempt,count:value.backlogCount,bytes:value.backlogBytes,oldest_received:oldest || null,approximate:true,error:null}];
    }catch {return [name,{...old?.[name],count:old?.[name]?.count ?? null,error:binding?.metrics?'metrics_unavailable':'binding_unavailable',last_attempt:attempt}];}
  }));
  await archive.put(key,JSON.stringify({at:attempt,...Object.fromEntries(entries)}));
}
export async function queueMetrics(env:Env,now=Date.now()):Promise<any> {
  const value:any=await timedBucket(env.ARCHIVE).get(key).then(o=>o?.json()).catch(()=>null);
  return Object.fromEntries(['queue','dlq'].map(name=>[name,{...value?.[name],count:value?.[name]?.count ?? null,stale:!value?.[name]?.at || now-value[name].at>180000 || Boolean(value[name].error)}]));
}
