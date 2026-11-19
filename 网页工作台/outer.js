// 控制请求绑定当前可见版本；DOM 只使用 textContent 展示外部输入。
export function mountOuter(request, notice, openTask) {
  const area = document.getElementById('outer');
  const form = area.querySelector('form');
  const selector = area.querySelector('select');
  const detail = area.querySelector('article');
  const statusNames = {DRAFT:'草案', ASSESSING:'正在整理', AWAITING_CLARIFICATION:'等待澄清',
    AWAITING_APPROVAL:'等待批准', AUTHORIZED:'已授权', PAUSED:'已暂停', STOPPED:'已停止',
    NEEDS_ATTENTION:'需要处理', VERIFICATION_GAP:'需要补充验收方法'};
  let chosen = new URLSearchParams(location.search).get('goal') || '';
  let event = '';
  let pending = '';
  let active = false;
  let revisionKey = '';
  let generation = 0;
  function element(tag, text) {
    const node = document.createElement(tag);
    node.textContent = text;
    return node;
  }
  function action(label, run) {
    const button = element('button', label);
    button.type = 'button';
    button.onclick = async () => {
      if (active) return;
      active = true;
      try { await run(); }
      catch (err) { notice(err.message); }
      finally { active = false; await refresh(); }
    };
    detail.append(button);
  }
  async function call(cmd, body) { return request('/api/outer/' + cmd, body); }
  function showRecord(title, value) {
    if (!value || (Array.isArray(value) && !value.length)) return;
    detail.append(element('h4',title));
    detail.append(element('pre', typeof value === 'string' ? value : JSON.stringify(value,null,2)));
  }
  function showDraft(d) {
    const key = [d.id,d.revision,d.status,d.task_id].join('/');
    if (key === revisionKey) return;
    revisionKey = key;
    detail.replaceChildren(element('h3',statusNames[d.status] || d.status),
      element('p',d.id+' · 工单 v'+d.revision+' · 目标 v'+d.goal_revision+' · 验收 v'+d.acceptance_revision));
    showRecord('目标与范围',d.goal_contract);
    showRecord('验收标准与方法',d.acceptance_contract);
    showRecord('权限与预算',d.approval_terms);
    showRecord('待处理',d.error);
    const binding = {ticket_id:d.id, revision:d.revision, goal_revision:d.goal_revision,
      acceptance_revision:d.acceptance_revision};
    function command(name, extra={}) {return call(name,{...binding,...extra,request_id:crypto.randomUUID()});}
    if (d.status === 'AWAITING_APPROVAL' && d.approval_terms) {
      action('批准这版目标与验收',async()=>{
        if (confirm('批准当前展示的目标、验收方法、权限与预算，并开始执行？')) {
          await command('approve'); notice('授权已保存。');
        }
      });
    }
    if (!['STOPPED','PAUSED'].includes(d.status)) {
      action('暂停此单',()=>command('pause'));
      action('停止此单',async()=>{if (confirm('停止这张工单？')) await command('stop');});
    }
    if (d.status === 'PAUSED') action('继续此单',()=>command('resume'));
    if (d.task_id) action('查看执行与交付',()=>openTask(d.task_id));
    if (!['STOPPED','PAUSED'].includes(d.status)) {
      const question=d.question?.answer_status==='answered' ? null : d.question;
      const label=element('label',question?.question_text || '补充要求或申请修改范围');
      const field=element('textarea','');
      field.maxLength=6000; label.append(field); detail.append(label);
      for (const option of question?.options || []) action(String(option),()=>{field.value=String(option);});
      action(question?'提交澄清':'提交补充',async()=>{
        if (!field.value.trim()) return notice('请先填写内容。');
        const response=await command('submit',{text:field.value.trim(),question_id:question?.question_id});
        event=response.event_id; field.value=''; notice('已记录，等待核对。');
      });
    }
  }
  async function refresh() {
    if (active || area.hidden) return;
    const pass=++generation;
    try {
      if (event) {
        const current=await call('event',{event_id:event});
        if(pass!==generation)return;
        if(current.ticket_id) {chosen=current.ticket_id; event='';}
        else if(current.choices?.length) {
          revisionKey=''; detail.replaceChildren(element('h3','这条消息属于哪张工单？'));
          const original=event;
          for (const c of current.choices) action(c.summary || c.id,async()=>{
            const res=await call('select',{event_id:original,ticket_id:c.id,request_id:crypto.randomUUID()});
            chosen=res.ticket_id || c.id; event='';
          });
          action('新建工单',async()=>{
            const res=await call('select',{event_id:original,ticket_id:null,request_id:crypto.randomUUID()});
            chosen=res.ticket_id || ''; event=res.event_id || '';
          });
          return;
        } else if (current.error) notice(String(current.error));
      }
      const state=await request('/api/outer');
      if(pass!==generation)return;
      const drafts=state.drafts||[];
      selector.replaceChildren(...drafts.map(d=>new Option(
        (d.goal_contract?.goal_statement || d.summary || d.id)+' · '+(statusNames[d.status] || d.status),d.id)));
      if (!chosen && drafts.length) chosen=drafts[0].id;
      selector.value=chosen;
      if(chosen) {
        const draft=await call('get',{ticket_id:chosen});
        if(pass===generation && draft.id===chosen) showDraft(draft);
      } else detail.replaceChildren(element('p',event?'需求已入账，正在整理。':'还没有目标工单。'));
    } catch(err) { if(pass===generation)notice(err.message); }
  }
  selector.onchange=()=>{chosen=selector.value; event=''; refresh();};
  form.oninput=()=>{pending='';};
  form.onsubmit=async ev=>{
    ev.preventDefault(); if(active)return;
    active=true; generation++;
    try{
      pending ||= crypto.randomUUID();
      const res=await call('submit',{text:form.elements.goal.value.trim(),request_id:pending});
      event=res.event_id; chosen=res.ticket_id||''; pending=''; form.reset(); notice('需求已保存。');
    }catch(err){notice(err.message);}
    finally{active=false; await refresh();}
  };
  area.hidden=false;
  document.getElementById('new').hidden=true;
  refresh();
  setInterval(()=>{if(!document.hidden)refresh();},3500);
}
