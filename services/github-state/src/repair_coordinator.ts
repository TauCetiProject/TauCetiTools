// One repository owner, four complete group lifetimes, finite scheduling turns.
// Eligibility and quota decisions remain in the repository and installation owner.
export interface RepairIdentity {key:string;lease_id:string;lease_until:number;api?:string|null;lane?:string}
export interface RepairGroup {
  id:number; started:number;lane:string;jobs:RepairIdentity[]; remaining:Map<string,RepairIdentity>;
  pendingIO:Map<Promise<unknown>,{wait:Promise<unknown>,expired:boolean,cancellable?:boolean,graceUsed?:boolean}>; wakeAtStart:number; task?:Promise<void>;
  capacityRetryAt?:number;
  preparationError?:unknown;
}
export interface RepairQuantum {
  launches:number; retired:number; progress:number;dataProgress:number;memberRetries:number;
  failed:Set<string>; paths:Map<string,Set<string>>; capacityPaths:Map<string,Map<string,number>>;routed:Set<string>;
}
export interface RepairSelection {jobs:RepairIdentity[];preparationError?:unknown}
interface Hooks {
  concurrency:number;dispatches:number;
  held:()=>boolean;
  select:(q:RepairQuantum,owned:Map<string,RepairIdentity>)=>Promise<RepairSelection|{retired:true}|{skipped:true}|undefined>;
  eligible:(q:RepairQuantum,owned:Map<string,RepairIdentity>)=>Promise<boolean>;
  dispatch:(group:RepairGroup)=>Promise<void>;
  maintenance:(external:boolean,phase:'before'|'after')=>Promise<void>;
  arm:()=>Promise<void>;
  lifetime:(work:Promise<void>)=>void;
  operations:(delta:number)=>void;
  failure:(error:unknown)=>void;
  stalled:(group:RepairGroup)=>void;
}
const quantum=():RepairQuantum=>({launches:0,retired:0,progress:0,dataProgress:0,memberRetries:0,failed:new Set(),paths:new Map(),capacityPaths:new Map(),routed:new Set()});
export type RepairEffect='progress'|'member-retry'|'defer'|'capacity'|'restart'|'route'|'none';
export const repairPath=(job:Pick<RepairIdentity,'api'>):string=>job.api || 'auto';

export class RepairCoordinator {
  readonly groups=new Map<number,RepairGroup>();
  readonly owned=new Map<string,RepairIdentity>();
  current=quantum();
  flight?:Promise<void>;
  wakeSequence=0;
  private nextId=0;
  private requested=false;
  private external=false;
  private stopped=false;
  private boundary=false;
  private closed=false;
  private newQuantumAllowed=false;
  private capacityPending?:number;
  private capacityEpoch=0;
  private readonly stats={since:Date.now(),turns:0,external_turns:0,launches:0,selections:0,maintenance_continuations:0,max_active_groups:0};
  private readonly slotMilliseconds:Record<string,number>={};
  constructor(private hooks:Hooks) {}
  get full():boolean{return this.groups.size>=this.hooks.concurrency;}
  get unproductiveStop():boolean{return this.closed&&!this.newQuantumAllowed;}
  snapshot():Record<string,unknown> {
    const now=Date.now(),slot_ms_by_lane={...this.slotMilliseconds};
    const groups=[...this.groups.values()].map(group=>{
      const age_ms=Math.max(0,now-group.started);
      slot_ms_by_lane[group.lane]=(slot_ms_by_lane[group.lane] || 0)+age_ms;
      return {lane:group.lane,age_ms,remaining_members:group.remaining.size,pending_hops:group.pendingIO.size,expired_hops:[...group.pendingIO.values()].filter(hop=>hop.expired).length};
    });
    return {scope:'repository_instance_since_restart',...this.stats,active_groups:groups.length,owned_members:this.owned.size,groups,slot_ms_by_lane,
      quantum:{launches:this.current.launches,progress:this.current.progress,data_progress:this.current.dataProgress,member_retries:this.current.memberRetries,retired:this.current.retired},stopped:this.stopped};
  }
  wake(external=false):Promise<void> {
    if(this.closed && !external && !this.external && !this.newQuantumAllowed && this.capacityPending===undefined)return Promise.resolve();
    this.requested=true;this.external ||= external;
    if(this.flight)return this.flight;
    if(this.stopped || this.hooks.held())return Promise.resolve();
    if(this.full && !this.external)return Promise.resolve();
    this.hooks.operations(1);
    const work=Promise.resolve().then(()=>this.fill()).catch(error=>this.hooks.failure(error));
    const flight=work.finally(()=>{
      this.hooks.operations(-1);
      if(this.flight===flight)this.flight=undefined;
      // Completion between the last selection and finalization cannot lose a wake.
      if(this.requested && !this.stopped && !this.hooks.held() && (!this.full || this.external))void this.wake();
    });
    this.flight=flight;this.hooks.lifetime(flight);return flight;
  }
  capacityWake():Promise<void>{this.wakeSequence++;return this.resumeCapacity();}
  resumeCapacity():Promise<void>{this.capacityPending=this.capacityEpoch;return this.wake();}
  settled(jobs:RepairIdentity[],effect:RepairEffect='none'):void {
    for(const job of jobs) {
      const actual=this.owned.get(job.key);
      if(actual?.lease_id===job.lease_id) {
        this.owned.delete(job.key);
        for(const group of this.groups.values())if(group.remaining.get(job.key)?.lease_id===job.lease_id)group.remaining.delete(job.key);
      }
      if(effect==='defer' || effect==='restart' || effect==='member-retry')this.excludePath(job);
      if(effect==='capacity'){
        const paths=this.current.capacityPaths.get(job.key)||new Map<string,number>();paths.set(repairPath(job),++this.capacityEpoch);this.current.capacityPaths.set(job.key,paths);
      }
      if(effect==='route') {
        // Routing to a path already known unsuccessful is not forward progress.
        if(!this.current.routed.has(job.key) && !this.current.paths.get(job.key)?.has('core') && !this.current.capacityPaths.get(job.key)?.has('core'))this.current.progress++;
        this.current.routed.add(job.key);this.excludePath(job);
      }
    }
    if(effect==='progress'){this.current.progress+=jobs.length;this.current.dataProgress+=jobs.length;}
    if(effect==='member-retry')this.current.memberRetries+=jobs.length;
    if(effect==='progress' || effect==='route' || effect==='member-retry'){if(this.closed)this.newQuantumAllowed=true;void this.wake();}
  }
  excludePath(job:RepairIdentity):void {
    const paths=this.current.paths.get(job.key) || new Set<string>();
    paths.add(repairPath(job));this.current.paths.set(job.key,paths);
  }
  failed(jobs:RepairIdentity[]):void {for(const job of jobs)this.current.failed.add(job.key);}
  private launch(jobs:RepairIdentity[],preparationError?:unknown):void {
    const lane=['requested','active','verification','archive'].includes(jobs[0].lane || '')?jobs[0].lane!:'unknown';
    const group:RepairGroup={id:++this.nextId,started:Date.now(),lane,jobs,remaining:new Map(jobs.map(j=>[j.key,j])),pendingIO:new Map(),wakeAtStart:this.wakeSequence,preparationError};
    // Registration precedes the next claim, and covers fetch through cleanup.
    this.groups.set(group.id,group);for(const job of jobs)this.owned.set(job.key,job);
    this.stats.launches++;this.stats.max_active_groups=Math.max(this.stats.max_active_groups,this.groups.size);
    this.current.launches++;this.hooks.operations(1);
    const work=Promise.resolve().then(()=>this.hooks.dispatch(group)).catch(error=>{this.failed([...group.remaining.values()]);this.hooks.failure(error);});
    const task=work.finally(async()=>{
      while(group.pendingIO.size) {
        const expired=[...group.pendingIO].filter(([,hop])=>hop.expired);
        if(expired.length){
          if(expired.every(([,hop])=>hop.cancellable && !hop.graceUsed)){
            // Cancellation acknowledgement can arrive after durable cleanup.
            // Keep the failed slot, rather than resetting healthy owner work
            // before the actual transport has a short chance to acknowledge.
            for(const [,hop] of expired)hop.graceUsed=true;
            let timer:ReturnType<typeof setTimeout>|undefined;
            try{await Promise.race([Promise.allSettled(expired.map(([actual])=>actual)),new Promise(resolve=>{timer=setTimeout(resolve,2000);})]);}
            finally{if(timer!==undefined)clearTimeout(timer);}
            continue;
          }
          // Only a genuinely expired, still-running hop can require a reset.
          this.stopped=true;this.hooks.stalled(group);return;
        }
        await Promise.allSettled([...group.pendingIO.values()].map(hop=>hop.wait));
      }
      for(const job of group.remaining.values())if(this.owned.get(job.key)?.lease_id===job.lease_id)this.owned.delete(job.key);
      this.slotMilliseconds[group.lane]=(this.slotMilliseconds[group.lane] || 0)+Math.max(0,Date.now()-group.started);
      this.groups.delete(group.id);this.hooks.operations(-1);
      void this.wake();
    });
    group.task=task;this.hooks.lifetime(task);
  }
  private async fill():Promise<void> {
    this.requested=false;
    const external=this.external;this.external=false;
    this.stats.turns++;if(external)this.stats.external_turns++;
    if(external || this.closed && this.newQuantumAllowed) {
      // Ordinary wakes cannot inherit an exclusion past a real retry deadline.
      this.current=quantum();this.boundary=false;this.closed=false;this.newQuantumAllowed=false;
    }
    if(this.capacityPending!==undefined){
      // A release may arrive during an in-flight selection that then closes
      // from its stale snapshot. Consume that wake in the same finite record;
      // do not lose it or grant an extra launch budget to an open quantum.
      const cutoff=this.capacityPending;this.capacityPending=undefined;let cleared=0;
      for(const [key,paths] of this.current.capacityPaths){
        for(const [path,epoch] of paths)if(epoch<=cutoff){paths.delete(path);cleared++;}
        if(!paths.size)this.current.capacityPaths.delete(key);
      }
      if(cleared && this.current.launches>=this.hooks.dispatches){
        // A capacity signal can renew its finite attempt budget, but says
        // nothing about backend failures, primary waits or API restart paths.
        const prior=this.current;
        this.current={...quantum(),failed:prior.failed,paths:prior.paths,capacityPaths:prior.capacityPaths,routed:prior.routed};
        this.boundary=false;
      }
      this.closed=false;
    }
    try {
      if(external)await this.hooks.maintenance(true,'before');
      while(!this.stopped && !this.hooks.held() && !this.full) {
        const q=this.current;
        if(q.launches>=this.hooks.dispatches || q.retired>=200) {
          if(!this.boundary){this.boundary=true;this.stats.maintenance_continuations++;await this.hooks.maintenance(false,'after');}
          // Confirmed member-data failures already moved those members to
          // durable future retry. They must not strand unrelated ready work.
          // Quota/backend failures and uncertain outcomes earn no such credit.
          const productive=q.progress>0 || q.memberRetries>0 || q.retired>=200;
          this.closed=true;this.newQuantumAllowed=false;
          // Evaluate with the old exclusions. A failed tail or API ping-pong
          // alone cannot authorize immediate chaining into a fresh record.
          if(productive && await this.hooks.eligible(q,this.owned)){this.newQuantumAllowed=true;this.requested=true;}
          break;
        }
        this.stats.selections++;
        const before=q.progress+q.memberRetries,selected=await this.hooks.select(q,this.owned);
        if(!selected){
          // In-flight work can commit an alternative API or a new generation.
          // Pause this record until completion instead of ending it prematurely.
          this.closed=this.groups.size===0;
          if(this.closed)this.newQuantumAllowed=false;
          if(this.closed && q.progress+q.memberRetries>before && await this.hooks.eligible(q,this.owned)){this.newQuantumAllowed=true;this.requested=true;}
          break;
        }
        if('retired' in selected){q.retired++;continue;}
        if('skipped' in selected){q.launches++;continue;}
        if(!selected.jobs.length){this.closed=true;this.newQuantumAllowed=false;break;}
        this.launch(selected.jobs,selected.preparationError);
      }
    } finally {
      if(external)try{await this.hooks.maintenance(true,'after');}catch(error){this.hooks.failure(error);}
      try{await this.hooks.arm();}catch(error){this.hooks.failure(error);}
    }
  }
}
