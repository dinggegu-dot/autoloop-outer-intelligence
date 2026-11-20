import {test} from 'node:test';
import assert from 'node:assert/strict';
import {taskJourney} from './journey.js';
const base={goal:'测试',phase:'plan',status:'queued',paused:false,plan:null,step:0,version:1,decisions:[],artifacts:[],review:null,approval:null};
const plan={summary:'计划',steps:[{title:'实施',objective:'目标',state:'completed'}]};
test('尚未开始的规划不会显示正在执行',()=>{assert.equal(taskJourney(base).stages[1].state,'pending');assert.equal(taskJourney({...base,status:'planning'}).stages[1].state,'active');});
test('规划暂停和失败显示阻塞',()=>{for(const status of ['paused','blocked','budget_exhausted','stopping'])assert.equal(taskJourney({...base,status}).stages[1].state,'blocked');});
test('验收暂停、审批、预算耗尽保持在验收关卡',()=>{for(const status of ['paused','blocked','awaiting_approval','budget_exhausted']){const j=taskJourney({...base,plan,phase:'review',status,decisions:[{kind:'plan',version:1,approved:true}]});const review=j.stages.find(s=>s.title==='独立验收');assert.equal(review.state,'blocked');assert.equal(j.selected,review.id);}});
test('追加多次修复仍保留验收交付且汇总完成数',()=>{const steps=Array.from({length:9},(_,i)=>({title:'步骤'+i,objective:'检查',state:i<8?'completed':'running'}));const j=taskJourney({...base,plan:{summary:'修复',steps},phase:'execute',step:8,status:'executing',decisions:[{kind:'plan',version:1,approved:true}]});assert.equal(j.stages.length,10);assert.equal(j.stages[7].state,'active');assert.match(j.stages[7].title,/4\/5/);assert.equal(j.stages[8].title,'独立验收');assert.equal(j.stages[9].title,'成果交付');});
test('只有完成验收才点亮交付',()=>{const task={...base,plan,phase:'review',status:'reviewing',decisions:[{kind:'plan',version:1,approved:true}]};assert.equal(taskJourney(task).stages.at(-1).state,'pending');assert.equal(taskJourney({...task,status:'completed'}).stages.at(-1).state,'verified');});

test('方案未批时实施步骤仍未开始',()=>{const j=taskJourney({...base,plan:{summary:'方案',steps:[{title:'实施',objective:'目标',state:'pending'}]},phase:'execute',status:'awaiting_approval',approval:{kind:'plan'}});assert.equal(j.stages[2].state,'blocked');assert.equal(j.stages[3].state,'pending');});
test('全部实施完成后预算不足会定位验收入口',()=>{const j=taskJourney({...base,plan,phase:'execute',step:1,status:'budget_exhausted',decisions:[{kind:'plan',version:1,approved:true}]});const review=j.stages.find(s=>s.title==='独立验收');assert.equal(review.state,'blocked');assert.equal(j.selected,review.id);});
test('每个系统阶段都绑定明确分区，长步骤汇总保留定位',()=>{
 const j=taskJourney({...base,plan:{summary:'方案',steps:Array.from({length:8},(_,i)=>({title:'步骤'+i,objective:'做事',state:'pending'}))}});
 assert.deepEqual(j.stages[0].target,{panel:'overview',anchor:'goal'});
 assert.deepEqual(j.stages[2].target,{panel:'plan',anchor:'approval'});
 assert.deepEqual(j.stages[7].target,{panel:'plan',anchor:'step-4'});
 assert.deepEqual(j.stages.at(-1).target,{panel:'results',anchor:'artifacts'});
 assert.ok(j.stages.every(s=>s.target?.panel&&s.target.anchor));
});
