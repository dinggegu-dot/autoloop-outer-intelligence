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
