import {test} from 'node:test';
import assert from 'node:assert/strict';
import {markdown,lifecycle,graph,stepState,modelFor} from './task_presentation.js';

test('目标工单登记后自动规划，阻塞时引导核对证据',async()=>{
 const {nextAction}=await import('./task_presentation.js');
 const task={workflow:'goal',cards:[],status:'created'};
 assert.match(nextAction(task),/自动开始规划/);
 assert.match(nextAction({...task,status:'blocked'}),/阻塞与已有证据/);
 assert.match(nextAction({...task,status:'completed'}),/已通过最终验收/);
});
const task={version:1,model:'gpt-6-astra',phase:'execute',status:'executing',step:0,paused:false,decisions:[{version:1,kind:'plan',approved:true}],plan:{steps:[{title:'<img onerror=x>',state:'running',objective:'目的',verification:'检查'}]}};
test('已登记与已派发不误称正在规划',async()=>{
 const {nextAction}=await import('./task_presentation.js');
 assert.match(nextAction({...task,status:'created'}),/尚未派发/);
 assert.match(nextAction({...task,status:'queued'}),/已排队/);
});
test('成果预览转义HTML与代码，不产生可执行标签或外部加载',()=>{
 const result=markdown('# 标题\n<script>alert(1)</script>\n![外部](https://example.test/x)\n```html\n<img src=x onerror=alert(1)>\n```');
 assert.ok(result.includes('<h2>标题</h2>'));assert.ok(!result.includes('<script>'));assert.ok(!result.includes('<img '));assert.ok(result.includes('&lt;script&gt;'));
});
test('Markdown表格与长文结构可阅读',()=>{const result=markdown('| 项目 | 状态 |\n| --- | --- |\n| A | 完成 |\n\n- **证据**\n> 原始记录');assert.ok(result.includes('<table>'));assert.ok(result.includes('<strong>证据</strong>'));assert.ok(result.includes('<blockquote>'));});
test('断线或暂停不呈现运行动画，图形步骤不会注入HTML',()=>{assert.equal(stepState({...task,observation_stale:true},0),'unknown');assert.equal(stepState({...task,paused:true},0),'blocked');assert.ok(!lifecycle({...task,observation_stale:true}).some(s=>s.state==='running'));assert.ok(!graph(task).includes('<img '));});
test('新角色配置覆盖旧通用模型且不会提前点亮交付',()=>{assert.equal(modelFor({...task,role_models:{execute:'gpt-6.1-terra'}},'execute'),'gpt-6.1-terra');assert.equal(lifecycle(task).at(-1).state,'pending');assert.equal(lifecycle({...task,status:'completed'}).at(-1).state,'done');});
test('审批之前的实施步骤保持待开始，完成后执行入口指向存在的最后步骤',()=>{
 assert.equal(stepState({...task,status:'awaiting_approval',decisions:[]},0),'pending');
 const row=lifecycle({...task,phase:'review',step:1,status:'reviewing'})[3];assert.equal(row.anchor,'step-0');
});

test('审核阻塞不显示待开始，且提供审核续办提示',async()=>{
 const {nextAction}=await import('./task_presentation.js');
 const waiting={...task,phase:'review',status:'awaiting_approval',cards:[{state:'open'}]};
 assert.equal(lifecycle(waiting)[4].state,'blocked');
 assert.match(nextAction(waiting),/验收阻塞/);
});

test('DeepSeek角色显示引擎且部分用量不冒充完整',async()=>{
 const {roles,usageSummary}=await import('./task_presentation.js');
 const record={...task,calls:2,role_models:{plan:'gpt-6-astra',execute:'deepseek-dsh',review:'gpt-6-astra'},role_routes:{execute:{engine:'dsh',model:'deepseek-flash'}},usage_by_role:{execute:{input:20,output:10,reported_calls:[1,2],partial_calls:[2]}}};
 assert.match(roles(record),/DeepSeek · DSH/);assert.match(roles(record),/deepseek-flash/);
 assert.match(usageSummary(record),/已收到 2\/2/);assert.match(usageSummary(record),/1 次有缺项/);assert.match(usageSummary(record),/缺项未知/);assert.match(usageSummary(record),/费用未知/);
 assert.doesNotMatch(usageSummary({...record,status:'executing'}),/完整上报/);
 assert.match(usageSummary({...record,versions:[{version:1}]}),/^累计含历史版本/);
});

test('队列区分待我处理、等待系统与未知，核对优先于上次的完成态',async()=>{
 const {queueFor}=await import('./task_presentation.js');
 for(const status of ['created','blocked','paused','budget_exhausted'])assert.equal(queueFor({status}),'attention');
 for(const status of ['queued','planning','executing','reviewing','stopping'])assert.equal(queueFor({status}),'running');
 assert.equal(queueFor({status:'awaiting_approval',cards:[]}),'unknown');
 assert.equal(queueFor({status:'awaiting_approval',cards:[{state:'open'}]}),'attention');
 assert.equal(queueFor({status:'completed'}),'delivered');
 assert.equal(queueFor({status:'completed'},false),'unknown');
 assert.equal(queueFor({status:'completed',runs:[{state:'unknown'}]}),'unknown');
 assert.equal(queueFor({status:'paused',pending_resume:{}}),'running');
 assert.equal(queueFor({status:'completed',revision_pending:{state:'blocked_dispatch'}}),'attention');
});

test('原单只在有当前动作时进入等你拍板队列', async()=>{
 const {projectGoalRow}=await import('./goal_team.js');
 const row={id:'wb-same',workbench_name:'情感样例',status:'AUTHORIZED',journey:{station:'decision',label:'待人工签收',artifact_review:true}};
 const pending={assignment:'candidate-1',sequence:2,state:'pending'};
 const ticket={...row,execution:{status:'awaiting_artifact_review',goal_execution:{artifact_reviews:[pending]}}};
 assert.equal(projectGoalRow(ticket).anchor,'artifacts');
 assert.equal(projectGoalRow(ticket).label,'交付 v2 · 待签收');
 assert.equal(projectGoalRow({...ticket,execution:{...ticket.execution,observation_stale:true}}).station,'unknown');
 assert.equal(projectGoalRow({...row,journey:{station:'decision',label:'预算用完'}}).station,'unknown');
});

test('偏好、正在停止和交付的入口始终绑定同一工单', async()=>{
 const {projectGoalRow}=await import('./goal_team.js');
 const row={id:'wb-same',status:'AUTHORIZED'};
 const pref={id:'q-1',revision:1,prompt:'哪组样例更贴切？'};
 const pending=projectGoalRow({...row,execution_status:'awaiting_preference',pending_preference:pref,journey:{station:'decision'}});
 assert.deepEqual([pending.id,pending.station,pending.label,pending.reason],['wb-same','decision','待选样例',pref.prompt]);
 const stopping=projectGoalRow({...row,execution_status:'stopping',journey:{station:'dock'}});
 assert.deepEqual([stopping.id,stopping.station,stopping.label],['wb-same','flight','正在停止']);
 const approved=projectGoalRow({...row,status:'AWAITING_APPROVAL',journey:{station:'decision',approve:true}});
 assert.equal(approved.action,'查看本版授权');
 const delivery=projectGoalRow({...row,delivery_sequence:3,delivery_at:'2026-11-24T12:00:00Z',journey:{station:'delivery'}});
 assert.deepEqual([delivery.id,delivery.anchor,delivery.label,delivery.at],['wb-same','artifacts','v3 已交付','2026-11-24T12:00:00Z']);
});

test('第二步方向沿用同一张原单的待决定入口，旧样例卡文字保持兼容', async()=>{
 const {projectGoalRow}=await import('./goal_team.js');
 const row={id:'wb-handoff',status:'AUTHORIZED',journey:{station:'decision'}};
 const pending={id:'question',revision:1,step:1,prompt:'希望保留哪种表达？',draft_sha256:'draft',candidate_digest:'binding'};
 const view=projectGoalRow({...row,pending_preference:pending});
 assert.deepEqual([view.id,view.anchor,view.label,view.action,view.reason],
  [row.id,'decision','第 2 步待定方向','查看修改方向',pending.prompt]);
 assert.equal(projectGoalRow({...row,pending_preference:{...pending,step:0}}).label,'待选样例');
 assert.equal(projectGoalRow({...row,pending_preference:pending,observation_stale:true}).station,'unknown');
});

test('沿用现稿时提醒清空意见，改选可修改方向后允许保留输入', async()=>{
 const {preferenceDirectionConflict}=await import('./goal_team.js');
 assert.match(preferenceDirectionConflict({mode:'keep'},' 保留这句之外再补一句 '),/清空意见.*选择可修改的方向/);
 assert.equal(preferenceDirectionConflict({mode:'keep'},'  '),'');
 assert.equal(preferenceDirectionConflict({mode:'edit'},'仍然用这条意见'),'');
 assert.equal(preferenceDirectionConflict({id:'legacy'},'旧卡允许意见'),'');
});

test('团队审稿记录显示真实初稿与三份摘要，锁票前隐藏工作记录', async()=>{
 const {projectTeamHandoffs}=await import('./goal_team.js');
 const observations=[{path:'invite.html',excerpt:'旧版说明',issue:'待确认事项难以定位'}];
 const handoff={comparison:{summary:'采用右栏方案'},draft_review:{summary:'核对 invite.html 的标题',observations},
  instruction:{summary:'保留标题，改末行',retain:['标题'],verify:['末行']},draft_sha256:'draft'};
 const checked={before_sha256:'draft',artifact_sha256:'revised',passed:true,changed_files:['invite.html']};
 const assignments=[{id:'early'},{id:'middle',step:1,run:5,report:{team_handoff:handoff}},
  {id:'accepted',step:1,report:{handoff_verification:{verification:checked}}},
  {id:'rejected',step:1,report:{handoff_verification:{verification:{...checked,artifact_sha256:'rejected'}}}}];
 const row={execution:{goal_execution:{assignments,decisions:[{kind:'checkpoint_saved',assignment:'accepted'}]}}};
 assert.deepEqual(projectTeamHandoffs(row),[{id:'middle',step:'第 2 步',run:5,comparison:handoff.comparison.summary,
  review:handoff.draft_review.summary,instruction:handoff.instruction.summary,retain:['标题'],verify:['末行'],draft:'draft',
  observations,checked}]);
 assert.deepEqual(projectTeamHandoffs({...row,acceptance_contract:{criteria:[{required:true,verifier_id:'blind-projection'}]}}),[]);
 assert.deepEqual(projectTeamHandoffs({execution:{}}),[]);
});

test('普通表单仅在启用目标模式时开放团队审稿，切回时保留已选项', async()=>{
 const {mountTeamReviewControl}=await import('./goal_team.js');
 const previous=globalThis.document;
 const createElement=tag=>({tag,children:[],append(...nodes){this.children.push(...nodes);}});
 globalThis.document={createElement};
 try {
  const anchor={after(node){this.next=node;}};
  const sync=mountTeamReviewControl(anchor,'review_and_decide');
  const control=anchor.next, select=control.children[0];
  assert.deepEqual(select.children.map(option=>option.value),['','review','review_and_decide']);
  assert.equal(select.name,'team_review_mode');
  assert.equal(control.hidden,true);
  assert.equal(select.disabled,true);
  sync(true);
  assert.equal(control.hidden,false);
  assert.equal(select.disabled,false);
  assert.equal(select.value,'review_and_decide');
  sync(false); sync(true);
  assert.equal(select.value,'review_and_decide');
 } finally { globalThis.document=previous; }
});

test('原单区分执行者返修、负责人复验与外层独立核验', async()=>{
 const {projectGoalRow}=await import('./goal_team.js');
 const row={id:'wb-same',journey:{station:'flight',label:'正在执行'}};
 const ticket=(repair,state='dispatched',status='executing')=>({...row,execution:{status,
  goal_execution:{assignments:[{state,inner_repairs:[{status:repair}]}]}}});
 const repairing=projectGoalRow(ticket('repairing'));
 assert.deepEqual([repairing.id,repairing.station,repairing.anchor,repairing.label],['wb-same','flight','progress','执行者正在返修']);
 assert.equal(projectGoalRow(ticket('rechecking')).label,'负责人正在复验新版');
 assert.equal(projectGoalRow(ticket('rechecking','cancelled')).label,'正在执行');
 assert.equal(projectGoalRow(ticket('passed','settled','reviewing')).label,'正在独立核验');
 const blind={criteria:[{required:true,verifier_id:'blind-projection'}]};
 assert.equal(projectGoalRow({...ticket('rechecking'),acceptance_contract:blind}).label,'正在执行');
});

test('待办先按已确认截止时间排序，再按等待起点和工单号排序', async()=>{
 const {sortQueue}=await import('./goal_team.js');
 const queue=[
  {id:'d',due:'',waitingSince:'2026-11-24T09:00:00Z'},
  {id:'b',due:'2026-11-25T10:00:00Z',waitingSince:'2026-11-24T10:00:00Z'},
  {id:'a',due:'2026-11-25T10:00:00Z',waitingSince:'2026-11-24T10:00:00Z'},
  {id:'c',due:'2026-11-26T10:00:00Z',waitingSince:'2026-11-24T08:00:00Z'},
 ];
 assert.deepEqual(sortQueue(queue,'decision').map(row=>row.id),['a','b','c','d']);
 assert.deepEqual(queue.map(row=>row.id),['d','b','a','c']);
});

class GoalElement {
  constructor(tag) {
   this.tag=tag; this.children=[]; this.dataset={}; this.value='';
   this.classList={add(){},toggle(){}};
  }
  get firstElementChild(){return this.children[0];}
  get childElementCount(){return this.children.length;}
  get isConnected(){return this.parent ? this.parent.isConnected : this.tag==='root';}
  append(...children){for(const child of children){child.parent=this;this.children.push(child);}}
  replaceChildren(...children){for(const child of this.children)child.parent=null;this.children=[];this.append(...children);}
  contains(node){return node===this || Boolean(node?.parent && this.contains(node.parent));}
  closest(){return null;}
  setAttribute(){}
  addEventListener(){}
  focus(){}
  showModal(){this.open=true;}
  close(){this.open=false;}
  querySelectorAll(selector){
   const matches=selector==='details[data-disclosure]' ? node=>node.tag==='details' && node.dataset.disclosure :
    selector==='[data-goal-write]' ? node=>Object.hasOwn(node.dataset,'goalWrite') :
    selector.startsWith('#') ? node=>node.id===selector.slice(1) :
    selector.startsWith('.') ? node=>node.className?.split(' ').includes(selector.slice(1)) : node=>node.tag===selector;
   return this.children.flatMap(node=>[...(matches(node)?[node]:[]),...node.querySelectorAll(selector)]);
  }
  querySelector(selector){return this.querySelectorAll(selector)[0];}
}
const flushGoalUI=()=>new Promise(resolve=>setImmediate(resolve));

test('初稿读取被明确拒绝后先撤下旧决定，普通故障保留重试',async t=>{
 const {mountGoalTeam}=await import('./goal_team.js');
 for(const status of [409,403,503]) await t.test(String(status),async t=>{
  const root=new GoalElement('root');
  const previous={window:globalThis.window,document:globalThis.document,location:globalThis.location,
   requestAnimationFrame:globalThis.requestAnimationFrame,fetch:globalThis.fetch};
  t.after(()=>Object.assign(globalThis,previous));
  Object.assign(globalThis,{
   window:{addEventListener(){}},
   document:{createElement:tag=>new GoalElement(tag),addEventListener(){}},
   location:{search:'?goal=wb-draft',hash:''},
   requestAnimationFrame:()=>{},
  });
  t.mock.method(globalThis,'setInterval',()=>0);
  const question={id:'first',revision:1,step:1,candidate_digest:'candidate-first',draft_sha256:'initial',
   options:[{id:'edit',label:'调整初稿'}]};
  let row={id:'wb-draft',revision:1,status:'AUTHORIZED',pending_preference:question,journey:{station:'decision'}};
  let reads=0, sessionRenewals=0, fileReads=0, readRejectedBody=false;
  let finishRefresh;
  const refreshed=new Promise(resolve=>{finishRefresh=resolve;});
  globalThis.fetch=async input=>{
   const params=new URL(input,'http://localhost').searchParams;
   if(!params.has('path')) return Response.json({files:[{path:'a.txt',kind:'text'},{path:'b.txt',kind:'text'}]});
   if(++fileReads===1) return Response.json({path:'a.txt',kind:'text',content:'封存原文'});
   return {
    status,ok:false,json:async()=>{readRejectedBody=true;return {error:'模拟读取错误'};},
   };
  };
  const team=mountGoalTeam(root,{
   initial:{drafts:[]},
   request:async path=>path==='/api/outer'?{drafts:[]}:(++reads===2?refreshed:row),
   renewSession:async()=>{sessionRenewals++;},
  });
  await flushGoalUI();
  const disclosure=root.querySelector('.goal-draft-disclosure');
  disclosure.open=true; disclosure.ontoggle();
  await flushGoalUI();
  assert.equal(root.querySelector('pre').textContent,'封存原文');
  const submit=root.querySelector('#goal-submit-preference');
  assert.ok(submit && !submit.disabled);
  const picker=root.querySelector('#goal-draft-file');
  picker.value='b.txt'; picker.onchange();
  await flushGoalUI();
  if(status===503){
   assert.equal(reads,1);
   assert.equal(root.querySelector('#goal-submit-preference'),submit);
   assert.ok(root.querySelectorAll('button').some(node=>node.textContent==='重试读取文件' && !node.hidden));
   assert.equal(readRejectedBody,true);
   return;
  }
  assert.equal(reads,2);
  assert.equal(sessionRenewals,status===403?1:0);
  assert.equal(readRejectedBody,false);
  assert.equal(root.querySelector('.goal-draft-preview'),undefined);
  assert.equal(root.querySelector('#goal-submit-preference'),undefined);
  assert.equal(root.querySelector('pre'),undefined);
  // 原单仍返回同一个已拒绝的绑定时也不能把旧决定放回来。
  row={...row,revision:2};
  finishRefresh(row);
  await flushGoalUI();
  assert.equal(root.querySelector('#goal-submit-preference'),undefined);
  row={...row,pending_preference:{...question,id:'second',candidate_digest:'candidate-second'}};
  await team.refresh();
  assert.ok(root.querySelector('#goal-submit-preference'));
 });
});

test('正式原单逐次确认自由文本，预算独立确认',async t=>{
 const {mountGoalTeam}=await import('./goal_team.js');
 const ticket={id:'wb-test',status:'AUTHORIZED',revision:5,goal_revision:3,acceptance_revision:2,
  allow_model_input:true,journey:{station:'decision'}};
 const fixed={...ticket,allow_model_input:undefined};
 const question={id:'first',revision:1,step:0,candidate_digest:'sample-digest',options:[{id:'edit',label:'修改'}]};
 async function mount(test,row,{panel='',formalModelInputConsent=true,failFirst=false}={}) {
  const root=new GoalElement('root'), events=[];
  const previous={window:globalThis.window,document:globalThis.document,location:globalThis.location,
   history:globalThis.history,requestAnimationFrame:globalThis.requestAnimationFrame};
  test.after(()=>Object.assign(globalThis,previous));
  const location=new URL('http://localhost/?goal='+row.id);
  if(panel) location.searchParams.set('goal_panel',panel);
  Object.assign(globalThis,{
   window:{addEventListener(){}},
   document:{createElement:tag=>new GoalElement(tag),addEventListener(){}},
   location,history:{replaceState(){}},requestAnimationFrame:()=>{},
  });
  test.mock.method(globalThis,'setInterval',()=>0);
  const state={drafts:[row]};
  const team=mountGoalTeam(root,{initial:state,formalModelInputConsent,request:async(path,body)=>{
   if(path==='/api/outer') return state;
   if(path==='/api/outer/get') return row;
   events.push({path,body:JSON.parse(JSON.stringify(body))});
   if(failFirst && events.length===1) throw new Error('暂不可用');
   return {};
  }});
  await team.refresh();
  const dialog=root.querySelector('dialog');
  const [cancel,confirm]=dialog.querySelectorAll('button');
  const control=id=>root.querySelector('#goal-'+id);
  const fill=(id,value)=>{const field=control(id);field.value=value;field.oninput();};
  return {root,team,events,dialog,cancel,confirm,control,fill};
 }
 await t.test('只选方向直接提交，空白不作为外发意见',async t=>{
  const ui=await mount(t,{...ticket,pending_preference:question});
  ui.control('option-edit').onchange();
  ui.fill('preference-text','  ');
  ui.control('submit-preference').onclick();
  await flushGoalUI();
  assert.equal(ui.dialog.open,undefined);
  assert.equal(ui.events.length,1);
  const [{path,body}]=ui.events;
  assert.equal(path,'/api/outer/answer_preference');
  assert.deepEqual(body.answer,{option_id:'edit',text:''});
  assert.equal(Object.hasOwn(body,'allow_model_input'),false);
 });
 await t.test('正式固定试单显示预设样例，提交只带选项',async t=>{
  const ui=await mount(t,{...fixed,pending_preference:question});
  assert.equal(ui.control('preference-text'),undefined);
  assert.ok(ui.root.querySelector('.goal-now').querySelectorAll('p').some(node=>node.textContent==='选择一组样例。'));
  ui.control('option-edit').onchange();
  ui.control('submit-preference').onclick();
  await flushGoalUI();
  assert.equal(ui.events.length,1);
  assert.deepEqual(ui.events[0].body.answer,{option_id:'edit',text:''});
  assert.equal(Object.hasOwn(ui.events[0].body,'allow_model_input'),false);
 });
 await t.test('自由意见取消保留，失败重试仍需确认且复用原请求编号',async t=>{
  const ui=await mount(t,{...ticket,pending_preference:question},{failFirst:true});
  ui.fill('preference-text','保留标题，调整示例');
  ui.control('submit-preference').onclick();
  assert.match(ui.dialog.querySelector('p').textContent,/发送给 Codex 模型/);
  ui.cancel.onclick();
  await ui.team.refresh();
  assert.equal(ui.events.length,0);
  assert.equal(ui.control('preference-text').value,'保留标题，调整示例');
  ui.control('submit-preference').onclick();
  ui.confirm.onclick();
  await flushGoalUI();
  assert.equal(ui.control('preference-text').value,'保留标题，调整示例');
  assert.match(ui.root.querySelector('.goal-feedback').textContent,/已保留填写内容/);
  ui.control('submit-preference').onclick();
  assert.equal(ui.events.length,1);
  ui.confirm.onclick();
  await flushGoalUI();
  assert.equal(ui.events.length,2);
  assert.deepEqual(ui.events[0],ui.events[1]);
  const {body}=ui.events[0];
  assert.deepEqual(body.answer,{text:'保留标题，调整示例'});
  assert.deepEqual([body.ticket_id,body.revision,body.question_id,body.question_revision,body.candidate_digest,body.allow_model_input],
   [ticket.id,ticket.revision,question.id,question.revision,question.candidate_digest,true]);
 });
 await t.test('补充或替换条件只发当前工单版本',async t=>{
  const goal_contract={constraints:[{source:'USER_EXPLICIT',requirement_id:'previous',description:'原要求'}]};
  const ui=await mount(t,{...ticket,goal_contract},{panel:'revisions'});
  const replacement=ui.control('section-revisions').querySelector('input');
  replacement.checked=true; replacement.onchange();
  ui.fill('revision-text','本次改用新的验收样例');
  ui.control('submit-revision').onclick();
  assert.match(ui.dialog.querySelector('p').textContent,/替换选中的 1 项要求/);
  assert.equal(ui.events.length,0);
  ui.confirm.onclick();
  await flushGoalUI();
  const [{path,body:{request_id,...payload}}]=ui.events;
  assert.equal(path,'/api/outer/submit');
  assert.ok(request_id);
  assert.deepEqual(payload,{ticket_id:ticket.id,revision:ticket.revision,text:'本次改用新的验收样例',
   allow_model_input:true,replace_requirement_ids:['previous']});
 });
 await t.test('退回理由绑定当前候选并单独确认',async t=>{
  const candidate={assignment:'a'.repeat(32),sequence:2,state:'pending',artifact_sha256:'b'.repeat(64)};
  const row={...ticket,journey:{...ticket.journey,artifact_review:true},execution:{status:'awaiting_artifact_review',
   goal_execution:{artifact_reviews:[candidate]}}};
  const ui=await mount(t,row,{panel:'artifacts'});
  ui.fill('return-reason','补齐附录的来源');
  ui.control('return-artifact').onclick();
  assert.equal(ui.confirm.textContent,'同意并退回');
  assert.equal(ui.events.length,0);
  ui.confirm.onclick();
  await flushGoalUI();
  const [{path,body:{request_id,...payload}}]=ui.events;
  assert.equal(path,'/api/outer/return_artifact');
  assert.ok(request_id);
  assert.deepEqual(payload,{ticket_id:ticket.id,revision:ticket.revision,goal_revision:ticket.goal_revision,
   acceptance_revision:ticket.acceptance_revision,candidate:candidate.assignment,artifact_sha256:candidate.artifact_sha256,
   reason:'补齐附录的来源',allow_model_input:true});
 });
 await t.test('正式固定试单的候选仍可签收',async t=>{
  const candidate={assignment:'a'.repeat(32),sequence:2,state:'pending',artifact_sha256:'b'.repeat(64)};
  const row={...fixed,journey:{...ticket.journey,artifact_review:true},execution:{status:'awaiting_artifact_review',
   goal_execution:{artifact_reviews:[candidate]}}};
  const ui=await mount(t,row,{panel:'artifacts'});
  assert.equal(ui.control('return-reason'),undefined);
  assert.equal(ui.control('return-artifact'),undefined);
  ui.control('accept-artifact').onclick();
  ui.confirm.onclick();
  await flushGoalUI();
  assert.equal(ui.events[0].path,'/api/outer/accept_artifact');
  assert.equal(ui.events[0].body.candidate,candidate.assignment);
 });
 await t.test('正式固定试单的条件页只展示记录',async t=>{
  const ui=await mount(t,{...fixed,status:'VERIFICATION_GAP'},{panel:'revisions'});
  assert.equal(ui.control('section-revisions'),undefined);
  assert.equal(ui.root.querySelectorAll('button').some(node=>node.textContent==='补充条件'),false);
 });
 await t.test('预算先核对总额，确认后按原授权版本续办',async t=>{
  const mandate={budget_seconds:3600,team_role_limit:16,execution_profile:{}};
  const ui=await mount(t,{...ticket,mandate,mandate_revision:2,execution:{status:'budget_exhausted'}});
  const time=ui.control('budget-minutes');
  assert.equal(ui.control('extend-budget').disabled,true);
  time.value='15'; time.onchange();
  ui.control('extend-budget').onclick();
  assert.match(ui.dialog.querySelector('p').textContent,/75 分钟、16 个角色轮次/);
  assert.equal(ui.events.length,0);
  ui.confirm.onclick();
  await flushGoalUI();
  const [{path,body:{request_id,...payload}}]=ui.events;
  assert.equal(path,'/api/outer/extend_team_budget');
  assert.ok(request_id);
  assert.deepEqual(payload,{ticket_id:ticket.id,revision:ticket.revision,mandate_revision:2,additional_minutes:15,additional_roles:0});
 });
 await t.test('实验路径仍按原方式提交文字',async t=>{
  const ui=await mount(t,{...fixed,pending_preference:question},{formalModelInputConsent:false});
  ui.fill('preference-text','沿用实验流程');
  ui.control('submit-preference').onclick();
  await flushGoalUI();
  assert.equal(ui.dialog.open,undefined);
  assert.equal(ui.events.length,1);
  assert.deepEqual(ui.events[0].body.answer,{text:'沿用实验流程'});
  assert.equal(Object.hasOwn(ui.events[0].body,'allow_model_input'),false);
 });
});
