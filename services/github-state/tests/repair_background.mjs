// Tests that assert final durable coverage explicitly await the tracked work.
// Alarm/tick handlers themselves return after finite slot filling.
export async function drainRepairs(state){
 const c=state.owner?.repairCoordinator || state.repairCoordinator;
 while(c && (c.flight || c.groups.size)){
  const groups=[...c.groups.values()],work=[c.flight,...groups.map(g=>g.task)].filter(Boolean);
  await Promise.all(work);
  if(!c.flight && c.groups.size && [...c.groups.values()].every(g=>groups.includes(g)))throw Error('settled repair tasks retained their slots without owner recovery');
 }
}
export const nativeDrainRepairs=`
 while(this.repairCoordinator?.flight || this.repairCoordinator?.groups.size){
  const c=this.repairCoordinator,groups=[...c.groups.values()];
  await Promise.all([c.flight,...groups.map(g=>g.task)].filter(Boolean));
  if(!c.flight && c.groups.size && [...c.groups.values()].every(g=>groups.includes(g)))throw Error('repair slot did not recover');
 }
`;
