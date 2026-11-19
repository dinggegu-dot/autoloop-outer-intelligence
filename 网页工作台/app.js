'use strict';
const byId=id=>document.getElementById(id);
const form=byId('new'), selector=byId('tasks'), detail=byId('detail');
let selected=new URLSearchParams(location.search).get('task')||'', csrf='', creation='', busy=false;
const node=(tag,text)=>Object.assign(document.createElement(tag),{textContent:text});
const notice=text=>{byId('notice').textContent=text;};
async function request(path,body){
  const options=body?{method:'POST',headers:{'Content-Type':'application/json','X-Odyssey-CSRF':csrf},body:JSON.stringify(body)}:{cache:'no-store'};
  const response=await fetch(path,options), data=await response.json();
  if(!response.ok)throw new Error(data.error||'请求结果未确认');
  return data;
}
function button(label,handler){
  const element=node('button',label);
  element.onclick=async()=>{if(busy)return;busy=true;element.disabled=true;try{await handler();await refresh();}catch(error){notice(error.message);}finally{busy=false;element.disabled=false;}};
  return element;
}
function render(task,tasks){
  detail.replaceChildren();
  if(!task){detail.append(node('p',selected?'指定工单不可用，不会改开其他工单。':'还没有工单。'));return;}
  const base='/api/tasks/'+encodeURIComponent(task.id);
  detail.append(node('h3',task.name),node('p',task.id+' · v'+task.version+' · '+task.status_label),
    node('p',task.goal),node('p','完成标准：'+(task.acceptance||'待方案定义')));
  if(task.role_routes)detail.append(node('p','本版实际模型：'+['plan','execute','review'].map(role=>task.role_routes[role]?.model||'未固定').join(' → ')));
  if(task.plan){
    detail.append(node('h3','当前方案'),node('p',task.plan.summary),node('p','范围：'+task.plan.scope));
    const steps=node('ol','');
    for(const step of task.plan.steps)steps.append(node('li',step.title+'：'+step.objective+'；验收：'+step.verification));
    detail.append(steps);
  }
  for(const card of task.cards.filter(item=>item.state==='open')){
    detail.append(node('h3',card.title),node('p',card.text));
    if(task.observation_stale||task.paused||task.revision_pending)continue;
    card.options.forEach((label,choice)=>detail.append(button(label,async()=>{
      if(!confirm('对当前方案选择：'+label+'？'))return;
      await request(base+'/answer',{card:card.id,version:card.version,choice});notice('决定已送达，等待续办。');
    })));
  }
  detail.append(node('h3','跨端接续'),node('p','来源：'+(task.source==='feishu'?'本人飞书私聊':'看板')));
  for(const note of task.inbox_notes)detail.append(node('p',note.at+' · '+(note.source==='board'?'看板':'本人飞书')+' · '+note.body));
  detail.append(button('补充资料或纠正归属',async()=>{
    const {editIntake}=await import('./intake_dialog.js');
    editIntake(task,tasks,request,refresh);
  }));
  detail.append(node('p','投递状态：'+({sent:'已送达',pending:'待投递',unknown:'结果未知'}[task.handoff_state]||'尚未请求')),
    button('转到本人飞书',async()=>{if(confirm('向已连接的本人私聊发送接续卡？不含文件。')){await request(base+'/handoff',{request_id:crypto.randomUUID()});notice('已登记切换请求，需要飞书桥在线。');}}));
  if(['created','paused','blocked'].includes(task.status))detail.append(button('开始或继续',async()=>{
    if(!confirm('按现有执行预算派发此任务？'))return;
    const result=await request(base+'/resume',{request_id:crypto.randomUUID()});notice(result.dispatch_error||'已请求继续。');
  }));
  if(['queued','planning','executing','reviewing','awaiting_approval'].includes(task.status))detail.append(button('暂停此任务',async()=>{
    if(confirm('暂停这张工单？')){await request(base+'/pause',{});notice('已请求暂停，请核对实际状态。');}
  }));
  detail.append(node('h3','成果与验收'),node('p',task.review?.summary||'尚无验收结论'));
  for(const path of task.artifacts){const link=node('a',path);link.href=base+'/artifact?path='+encodeURIComponent(path)+'&version='+task.version;detail.append(link,node('br',''));}
}
async function refresh(){
  const current=selected;
  try{
    const state=await request('/api/tasks'+(current?'?task='+encodeURIComponent(current):''));
    if(current!==selected)return;
    byId('connection').textContent=state.capability.message;
    if(!selected&&state.tasks.length)selected=state.tasks[0].id;
    selector.replaceChildren(...state.tasks.map(task=>new Option(task.name+' · '+task.status_label,task.id)));
    selector.value=selected;render(state.tasks.find(task=>task.id===selected),state.tasks);
  }catch(error){if(current===selected){byId('connection').textContent='状态待核对：'+error.message;detail.replaceChildren();}}
}
selector.onchange=()=>{
  selected=selector.value;
  detail.replaceChildren(node('p','正在读取所选工单…'));
  history.replaceState(null,'','?task='+encodeURIComponent(selected));
  refresh();
};
form.oninput=()=>{creation='';};
form.onsubmit=async event=>{
  event.preventDefault();if(busy)return;busy=true;
  try{
    const body=Object.fromEntries(new FormData(form));
    body.request_id=creation||=crypto.randomUUID();
    body.role_models={plan:'gpt-6-astra',execute:'gpt-6-astra',review:'gpt-6-astra'};
    const result=await request('/api/tasks',body);selected=result.task.id;
    history.replaceState(null,'','?task='+encodeURIComponent(selected));creation='';form.reset();
    notice(result.dispatch_error||'已登记。');await refresh();
  }catch(error){notice(error.message);}finally{busy=false;}
};
let outerMounted=false;
async function start(){try{csrf=(await request('/api/session')).csrf;
  if(!outerMounted && (await request('/api/outer')).enabled){
    const {mountOuter}=await import('./outer.js');
    mountOuter(request,notice,async id=>{selected=id;await refresh();detail.scrollIntoView();});outerMounted=true;
  }
  await refresh();}catch(error){notice(error.message);}}
byId('refresh').onclick=start;start();
setInterval(()=>{if(!busy&&!document.hidden&&csrf)refresh();},5000);
