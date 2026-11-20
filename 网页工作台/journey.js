// 只从任务事实派生展示；失败、等待和修复不能被动画解释为完成。
const ids=['C1','T0','T1','T2','T3','T4','T5','T6','T7','T8'];
const halted=new Set(['paused','blocked','budget_exhausted','awaiting_approval','awaiting_artifact_review','stopping','rejected']);
export function taskJourney(task, preferred=null){
  const blocked=task.paused||halted.has(task.status);
  const effectivePhase=task.phase==='execute'&&task.plan?.steps.length&&task.step>=task.plan.steps.length&&task.plan.steps.every(s=>s.state==='completed')?'review':task.phase;
  const phaseState=(phase,running)=>effectivePhase!==phase?'pending':blocked?'blocked':task.status===running?'active':'pending';
  const approval=task.decisions.some(d=>d.kind==='plan'&&d.version===task.version&&d.approved);
  const completed=task.status==='completed';
  const rows=[
    ['目标登记',task.goal,'verified',{panel:'overview',anchor:'goal'}],
    ['制定执行方案',task.plan?.summary||'等待规划角色分析完整目标',task.plan?'verified':phaseState('plan','planning'),{panel:'plan',anchor:'plan'}],
    ['确认执行方案',task.status==='rejected'?'方案未获批准，任务已停止':'查看完整方案后决定是否执行',approval?'verified':task.plan&&(task.approval?.kind==='plan'||task.status==='rejected')?'blocked':'pending',{panel:'plan',anchor:'approval'}]
  ];
  const steps=(task.plan?.steps||[]).map((step,index)=>[
    step.title,step.summary||step.objective,
    step.state==='completed'?'verified':approval&&index===task.step&&task.phase==='execute'?phaseState('execute','executing'):'pending',{panel:'plan',anchor:'step-'+index}
  ]);
  // 修复可能追加任意多步。保留全部状态，通过最后一个实施站点汇总后续步骤。
  if(steps.length>5){
    const tail=steps.slice(4),done=tail.filter(s=>s[2]==='verified').length;
    rows.push(...steps.slice(0,4),[`后续 ${tail.length} 个步骤（完成 ${done}/${tail.length}）`,tail.map(s=>s[0]).join('；'),done===tail.length?'verified':tail.some(s=>s[2]==='blocked')?'blocked':tail.some(s=>s[2]==='active')?'active':'pending',{panel:'plan',anchor:'step-4'}]);
  }else rows.push(...steps);
  rows.push(['独立验收',task.review?.summary||'核对真实产物与完成条件',completed?'verified':phaseState('review','reviewing'),{panel:'plan',anchor:'review'}]);
  rows.push(['成果交付',task.artifacts.length?`${task.artifacts.length} 项产物，打开任务查看验收与下载`:'验收通过后交付成果',completed?'verified':'pending',{panel:'results',anchor:'artifacts'}]);
  const stages=rows.map((row,i)=>({id:ids[i],title:row[0],description:row[1],state:row[2],target:row[3]}));
  const focus=stages.find(s=>s.state==='blocked')||stages.find(s=>s.state==='active')||stages.find(s=>s.state==='pending')||stages.at(-1);
  return {stages,selected:preferred&&stages.some(s=>s.id===preferred)?preferred:focus.id,project:{paused:task.paused},task};
}
