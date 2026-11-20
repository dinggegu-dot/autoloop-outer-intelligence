// 纯展示函数：内容全部转义，流程状态来自任务记录。
export const escapeText = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const e=escapeText;
export const modelName=id=>({'gpt-6-astra':'Astra','deepseek-dsh':'DeepSeek · DSH','gpt-6.1-terra':'Terra','gpt-6.1-sol':'Sol'}[id]||id||'未配置');
export const modelFor=(task,role)=>task.role_models?.[role]||task.model;
export const roleNames={plan:'方案设计',execute:'实施执行',review:'独立验收'};
export const panels={overview:'总览',plan:'方案与执行',results:'成果',changes:'调整与记录'};
export const stepState=(task,index)=>{
 if(task.plan?.steps[index]?.state==='completed')return 'done';
 const approved=task.decisions.some(d=>d.version===task.version&&d.kind==='plan'&&d.approved);
 if(!approved)return 'pending';
 if(task.phase!=='execute'||task.step!==index)return 'pending';
 if(task.observation_stale)return 'unknown';
 if(task.status==='executing'&&!task.paused)return 'running';
 return ['paused','stopping','blocked','budget_exhausted','awaiting_approval'].includes(task.status)||task.paused?'blocked':'pending';
};
const stateLabels={done:'已完成',running:'进行中',pending:'待开始',blocked:'待处理',unknown:'状态待核对'};
export const queueLabels = {all:'全部工单', attention:'待我处理', running:'进行中', delivered:'已交付', unknown:'待核对'};
export function queueFor(task,fresh=true){
  if(!fresh||task.observation_stale||task.runs?.some(r=>r.state==='unknown'))return 'unknown';
  const revision=task.revision_pending;
  if(revision)return ['blocked_dispatch','suspended'].includes(revision.state)||revision.error?'attention':'running';
  if(task.status==='completed')return 'delivered';
  if(task.status==='rejected')return 'all';
  if(task.pending_resume||task.status==='stopping')return 'running';
  if(task.workflow==='goal'&&task.status==='created'&&!task.paused)return 'running';
  if(task.paused||['created','blocked','paused','budget_exhausted'].includes(task.status))return 'attention';
  if(task.cards?.some(card=>card.state==='open'))return 'attention';
  if(task.status==='awaiting_approval')return 'unknown';
  return ['queued','planning','executing','reviewing'].includes(task.status)?'running':'unknown';
}
export function nextAction(task){
  if(queueFor(task)==='unknown')return '当前状态待核对，请刷新进度';
  if(task.status==='rejected')return '本版已结束，可在调整记录中查看决定';
  if(task.status==='stopping')return '正在等待执行轮次停止';
  if(task.revision_pending)return task.revision_pending.error||'已收到修改，正在准备新版方案';
  if(task.workflow==='goal'){
    if(task.status==='created')return '工单已登记，后台将自动开始规划';
    if(task.status==='paused')return '后台已停止，选择继续任务后续办';
    if(task.status==='blocked')return '后台已停止新增派工，请查看阻塞与已有证据';
    if(task.status==='budget_exhausted')return '工单授权的执行时间已用尽，请查看已有证据';
    if(task.status==='completed')return '已通过最终验收，可以阅读并下载交付成果';
  }
  if(task.status==='completed')return '阅读成果，或提出下一版改进';
  if(task.status==='created')return '工单已保存，尚未派发；可先补充资料，再从看板开始规划';
  if(task.status==='queued')return '任务已排队，尚未确认模型开始工作';
  if(task.cards?.some(c=>c.state==='open'))return task.phase==='review'?'查看验收阻塞，处理后继续审核':'核对方案并处理待办';
  if(task.status==='budget_exhausted')return '追加执行时间后继续';
  if(task.status==='blocked'&&task.blocker?.version===task.version&&task.blocker?.phase===task.phase)return '查看阻塞与已有证据，补齐条件后继续';
  if(task.paused||['blocked','stopping'].includes(task.status))return '查看阻塞原因，继续或调整要求';
  if(task.phase==='plan')return '方案设计中，完成后请你确认';
  if(task.phase==='review')return '独立验收中，检查结果将持续更新';
  return task.plan?.steps[task.step]?.title||'等待中台派发';
}
export function lifecycle(task){
  const approved=task.decisions.some(d=>d.version===task.version&&d.kind==='plan'&&d.approved);
  const complete=task.status==='completed';
  const execDone=task.plan?.steps.length&&task.plan.steps.every(s=>s.state==='completed');
  const running=!task.paused&&!task.observation_stale;
  return [
    {label:'目标',panel:'overview',anchor:'goal',state:'done'},
    {label:'方案',panel:'plan',anchor:'plan',state:task.plan?'done':task.status==='planning'&&running?'running':'pending'},
    {label:'确认',panel:'plan',anchor:'approval',state:approved?'done':task.plan?'blocked':'pending'},
    {label:'执行',panel:'plan',anchor:task.plan?.steps.length?'step-'+Math.min(task.step||0,task.plan.steps.length-1):'plan',state:execDone?'done':task.status==='executing'&&running?'running':'pending'},
    {label:'验收',panel:'plan',anchor:'review',state:complete?'done':task.phase==='review'?(task.observation_stale?'unknown':task.status==='reviewing'&&running?'running':'blocked'):'pending'},
    {label:'交付',panel:'results',anchor:'artifacts',state:complete?'done':'pending'}
  ];
}
export function pipeline(task){
 return `<nav class="mission-pipeline" aria-label="任务流程">${lifecycle(task).map((s,i)=>`<button class="pipeline-node ${s.state}" data-panel="${s.panel}" data-anchor="${s.anchor}" aria-label="${s.label}：${stateLabels[s.state]}"><span class="pipeline-dot">${s.state==='done'?'✓':String(i+1).padStart(2,'0')}</span><strong>${s.label}</strong><small>${stateLabels[s.state]}</small></button>`).join('')}</nav>`;
}
export function roles(task){
 return `<div class="role-flow" aria-label="模型角色分工">${Object.entries(roleNames).map(([role,label],i)=>`<div class="role-card ${task.active_role===role&&['planning','executing','reviewing'].includes(task.status)&&!task.paused&&!task.observation_stale?'running':''}"><span class="role-icon">${['✦','⌘','✓'][i]}</span><div><small>${label}</small><strong>${e(modelName(modelFor(task,role)))}</strong><span>${role==='plan'?'理解目标与设计步骤':role==='execute'?'按已批准方案实施':'依据产物与证据核验'}</span><small>${e(task.role_routes?.[role]?.engine || (modelFor(task,role)==='deepseek-dsh'?'dsh':'codex'))}${task.role_routes?.[role]?.engine==='dsh'?' · '+e(task.role_routes[role].model):''}</small></div></div>`).join('<span class="role-arrow" aria-hidden="true">→</span>')}</div>`;
}
function packagePlan(step){
 const packages=step.packages||[];
 if(!packages.length)return '';
 return `<details><summary>工作包与依赖（${packages.length}）</summary><ul>${packages.map(pkg=>
   `<li><strong>${e(pkg.id)}</strong> · ${e(pkg.capability)}<p>${e(pkg.objective)}</p><p>依赖 · ${e(pkg.depends_on.join('、')||'可独立开始')}</p><p>产物 · ${e(pkg.outputs.join('、'))}</p><p>核验 · ${e(pkg.checks.join('、'))}</p></li>`
 ).join('')}</ul></details>`;
}
export function graph(task){
 const steps=task.plan?.steps||[];
 if(!steps.length)return '<div class="mission-empty">方案生成后，每个步骤的目标、执行伙伴、验证条件都会在这里展开。</div>';
 return `<div class="step-graph" aria-label="阶段与工作包依赖">${steps.map((step,i)=>{const state=stepState(task,i);return `<article class="step-card ${state}" id="section-step-${i}" tabindex="-1"><div class="step-rail"><span>${state==='done'?'✓':String(i+1).padStart(2,'0')}</span></div><div class="step-content"><header><span class="step-label">${i===0?'方案起点':`依赖步骤 ${i}`} · ${e(modelName(modelFor(task,'execute')))}</span><span class="state-chip ${state}">${stateLabels[state]}</span></header><h4>${e(step.title)}</h4><p>${e(step.summary||step.objective)}</p><details><summary>目标与验收条件</summary><p>${e(step.objective)}</p><div class="verification"><strong>怎样算完成</strong><p>${e(step.verification)}</p></div></details>${packagePlan(step)}</div></article>`;}).join('')}</div>`;
}
// 限定 Markdown 子集。原始 HTML/脚本/图片语法均按文本显示，不加载外部内容。
export function markdown(source){
 const inline=s=>e(s).replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
 const lines=String(source).replace(/\r/g,'').split('\n');let result='',code=false,buffer=[],list=null;
 const closeList=()=>{if(list){result+=`</${list}>`;list=null;}};
 for(let i=0;i<lines.length;i++){
  const line=lines[i];
  if(/^\s*```/.test(line)){closeList();if(code){result+='<pre><code>'+e(buffer.join('\n'))+'</code></pre>';buffer=[];}code=!code;continue;}
  if(code){buffer.push(line);continue;}
  if(i+1<lines.length&&line.includes('|')&&/^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i+1])){
   closeList();const cells=x=>x.trim().replace(/^\||\|$/g,'').split('|').map(v=>v.trim());
   result+='<div class="read-table"><table><thead><tr>'+cells(line).map(v=>'<th>'+inline(v)+'</th>').join('')+'</tr></thead><tbody>';i++;
   while(i+1<lines.length&&lines[i+1].includes('|')&&lines[i+1].trim()){result+='<tr>'+cells(lines[++i]).map(v=>'<td>'+inline(v)+'</td>').join('')+'</tr>';}
   result+='</tbody></table></div>';continue;
  }
  const item=line.match(/^\s*(?:([-*+])|\d+[.)])\s+(.+)$/);
  if(item){const type=item[1]?'ul':'ol';if(list!==type){closeList();list=type;result+=`<${type}>`;}result+='<li>'+inline(item[2])+'</li>';continue;}
  closeList();const heading=line.match(/^(#{1,6})\s+(.+)$/);
  if(heading){const level=Math.min(heading[1].length+1,6);result+=`<h${level}>${inline(heading[2])}</h${level}>`;}
  else if(/^\s*>/.test(line))result+='<blockquote>'+inline(line.replace(/^\s*>\s?/,''))+'</blockquote>';
  else if(line.trim())result+='<p>'+inline(line)+'</p>';
 }
 closeList();if(code)result+='<pre><code>'+e(buffer.join('\n'))+'</code></pre>';return result;
}

export function usageSummary(task){
 const entries=Object.entries(task.usage_by_role||{});
 if(!entries.length)return 'Token 用量尚未上报 · 费用未知';
 const partial=new Set(entries.flatMap(([,v])=>v.partial_calls||[]));
 const reported=new Set(entries.flatMap(([,v])=>v.reported_calls||[])).size;
 return (task.versions?.length?'累计含历史版本 · ':'')+entries.map(([role,v])=>`${roleNames[role]||role}：输入 ${v.input.toLocaleString()} / 输出 ${v.output.toLocaleString()}`).join(' · ')+` · 已收到 ${reported}/${task.calls} 次调用的用量${partial.size?`，其中 ${partial.size} 次有缺项`:""}；仅统计已上报部分，执行中及缺项未知 · 费用未知`;
}
