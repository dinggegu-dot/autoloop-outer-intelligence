const stations={decision:'待我决定',flight:'进行中',delivery:'已交付',dock:'停靠中'};
// 行节点按调用 ID 复用，轮询不会收起已展开的回执。
function callHistory(article, call) {
  const panel = document.createElement('section');
  panel.id = 'outer-history';
  article.after(panel);
  const element = (tag, text) => Object.assign(document.createElement(tag), {textContent:text});
  const heading = element('h3', '外层模型调用');
  const status = element('p', '');
  status.setAttribute('role', 'status');
  panel.append(heading, status);
  const roles = {intake_assessor:'Intake Assessor', acceptance_designer:'Acceptance Designer', goal_coordinator:'Goal Coordinator'};
  const states = {accepted:'成功', returned:'已返回提案', running:'调用中', failed:'失败', rejected:'提案未通过', cancelled:'已取消',
    stale:'版本已过期', lease_expired:'领取已过期', missing_receipt:'回执缺失'};
  const actions = {REPAIR:'修复', REPLAN:'重新规划', CONTINUE:'重新验收', BLOCKED:'停办待处理', REQUEST_APPROVAL:'请用户决定'};
  const phases = {plan:'规划', replan:'重新规划', execute:'实施', repair:'修复', review:'验收', package:'工作包'};
  let ticket = '';
  let locating = location.hash === '#outer-history';
  const entries = new Map();
  function select(id) {
    if (id === ticket) return;
    ticket = id;
    panel.hidden = !id;
    entries.clear();
    panel.replaceChildren(heading, status);
    heading.textContent = '外层模型调用';
    status.textContent = id ? '正在读取调用记录…' : '';
  }
  function record(container, label, value) {
    container.append(element('h4',label), element('pre',typeof value === 'string' ? value : JSON.stringify(value,null,2)));
  }
  function entry(row, id) {
    const node = element('details','');
    const time = new Date(row.started_at).toLocaleString('zh-CN', {hour12:false});
    node.append(element('summary',[time, roles[row.role] || row.role, states[row.state] || row.state,
      row.elapsed_ms == null ? '' : row.elapsed_ms+' ms'].filter(Boolean).join(' · ')));
    const body = element('div','');
    node.append(body);
    let loading = false, loaded = false;
    node.ontoggle = async () => {
      if (!node.open || loaded || loading) return;
      loading = true;
      body.textContent = '正在读取明细…';
      try {
        const d = await call('call',{ticket_id:id,call_id:row.id});
        if (id !== ticket || !node.isConnected) return;
        body.replaceChildren(element('p', '调用 '+d.id+' · 模型 '+(d.model || '未记录')));
        if (d.trace) {
          const trace = d.trace, proposal = trace.decision;
          body.append(element('h4','程序决定'+(trace.run == null ? '' : ' · Run '+trace.run)));
          body.append(element('p',proposal ? '已采纳「'+(actions[proposal.action] || proposal.action)+'」 · '+proposal.reason
                                           : '账本尚无本次提案的采纳记录。'));
          if (trace.subsequent_runs.length) {
            body.append(element('h4','同一授权下的后续执行'));
            const runs=element('ol','');body.append(runs);
            for (const item of trace.subsequent_runs) {
              const result = item.passed === true ? '验收通过' : item.passed === false ? '验收未通过' : '';
              const line = element('li', [item.run == null ? '待绑定 Run' : 'Run '+item.run,
                phases[item.phase] || item.phase,result].filter(Boolean).join(' · '));
              if (item.delivery_digest) line.append(element('p','交付检查点 SHA-256 '+item.delivery_digest));
              runs.append(line);
            }
          }
        }
        record(body,'耗时',d.elapsed_ms == null ? '未记录' : d.elapsed_ms+' ms');
        record(body,'用量（tokens）',d.usage ?? '未记录');
        record(body,'失败原因',d.error ?? (['accepted','returned'].includes(d.state) ? '无' : '尚无失败回执'));
        record(body,'实际输入快照',d.input);
        if (d.request) {
          record(body,'发送给 Codex 的提示词',d.request.prompt);
          record(body,'输出 Schema',d.request.output_schema);
        } else body.append(element('p','此回执仅保存了输入快照。'));
        record(body,'模型返回'+(d.output_truncated?'（已截断）':''),d.final_message ?? d.output ?? '未记录');
        loaded = true;
      } catch (error) {
        body.textContent = error.message+'。收起后可重新打开。';
      } finally { loading = false; }
    };
    return node;
  }
  async function refresh(id, current) {
    select(id);
    if (!id) return;
    let rows;
    try { rows = await call('calls',{ticket_id:id}); }
    catch (err) { if (current()) status.textContent=err.message; return; }
    if (!current() || ticket !== id) return;
    heading.textContent = '外层模型调用（'+rows.length+'）';
    status.textContent = rows.length ? '' : '这张工单尚无调用记录。';
    for (const row of rows) {
      const signature = JSON.stringify(row);
      const previous = entries.get(row.id);
      if (signature === previous?.signature) continue;
      const node = entry(row, id);
      if (previous) { node.open=previous.node.open; previous.node.replaceWith(node); }
      else { panel.append(node); }
      entries.set(row.id, {signature,node});
    }
    if (locating) { locating=false; panel.scrollIntoView(); }
  }
  panel.hidden=true;
  return {select,refresh};
}

export function mountOuter(request,notice,openTask){
  const by=id=>document.getElementById(id);
  const element=(tag,text='')=>Object.assign(document.createElement(tag),{textContent:text});
  const call=(cmd,body)=>request('/api/outer/'+cmd,body);
  const area=by('outer'),detail=by('mission'),composer=by('composer'),form=composer.querySelector('form');
  const historyPanel=callHistory(detail,call);
  const title=d=>d.goal_contract?.goal_statement || d.summary || d.id;
  const query=new URLSearchParams(location.search);
  let chosen=query.get('goal') || '', filter=Object.hasOwn(stations,query.get('station'))?query.get('station'):'', rows=[], event='', active=false, generation=0, key='', listKey='', pending='';
  const oldTask=query.get('task');
  function updateUrl(){
    const url=new URL(location.href);
    if(filter)url.searchParams.set('station',filter);else url.searchParams.delete('station');
    if(chosen)url.searchParams.set('goal',chosen);else url.searchParams.delete('goal');
    if(url.hash==='#outer-history')url.hash='';
    history.replaceState(null,'',url);
  }
  function select(id){
    if(active)return;
    chosen=id;event='';key='';generation++;
    historyPanel.select(id);detail.replaceChildren();by('ticket-ref').textContent='';
    updateUrl();drawList();refresh();
  }
  function changeFilter(id){
    if(active)return;
    filter=id;const visible=rows.filter(r=>!id||r.journey.station===id);
    select(visible.some(r=>r.id===chosen)?chosen:(visible[0]?.id || ''));
  }
  function drawList(){
    by('all-stations').setAttribute('aria-pressed',String(!filter));
    for(const b of by('stations').children){
      const station=b.dataset.station;
      b.textContent=stations[station]+' · '+rows.filter(r=>r.journey.station===station).length;
      b.setAttribute('aria-pressed',String(filter===station));
    }
    by('map-link').href=chosen?'/?trip='+encodeURIComponent(chosen):'/';
    const visible=rows.filter(r=>!filter || r.journey.station===filter);
    const signature=JSON.stringify([chosen,filter,visible.map(r=>[r.id,title(r),r.journey.label,r.revision])]);
    if(signature===listKey)return;
    listKey=signature;by('count').textContent=visible.length+' 张';
    const list=by('ticket-list');list.replaceChildren();
    if(filter&&chosen&&rows.some(r=>r.id===chosen&&r.journey.station!==filter))list.append(element('p','当前详情已转入其他状态。'));
    if(!visible.length)list.append(element('p',filter?'此状态暂无工单。':'发布一个目标，开始第一段航程。'));
    for(const r of visible){
      const b=element('button');b.className='ticket';b.setAttribute('aria-current',String(r.id===chosen));
      b.append(element('small',r.journey.label+' · '+r.id.slice(-6)),element('strong',title(r)));
      b.onclick=()=>{select(r.id);if(matchMedia('(max-width:720px)').matches)detail.scrollIntoView();};list.append(b);
    }
  }
  function fold(parent,title,open=false){
    const box=element('details');box.open=open;box.append(element('summary',title));parent.append(box);return box;
  }
  function show(parent,label,value){
    if(value==null || value==='' || Array.isArray(value)&&!value.length)return;
    parent.append(element('h4',label),element('pre',typeof value==='string'?value:JSON.stringify(value,null,2)));
  }
  function action(parent,label,run,primary=false){
    const button=element('button',label);button.type='button';if(primary)button.className='primary';
    button.onclick=async()=>{
      if(active)return;active=true;generation++;button.disabled=true;
      try{await run();}catch(err){notice(err.message);}
      finally{active=false;button.disabled=false;await refresh();}
    };parent.append(button);
  }
  function showDraft(d){
    const e=d.execution, flow=e?.goal_execution, decisions=flow?.decisions || [];
    const reviews=flow?.artifact_reviews || [], candidate=reviews.at(-1);
    const signature=JSON.stringify([d.id,d.revision,d.status,d.journey,e?.status,e?.step,e?.error,decisions.length,reviews,flow?.validation?.checks]);
    if(signature===key)return;
    const oldInput=detail.querySelector('textarea[data-binding]'),keep=oldInput?.dataset.binding===d.id+'/'+d.revision;
    const oldReview=detail.querySelector('textarea[data-candidate]');
    const reasonDraft=oldReview?.dataset.candidate===candidate?.assignment ? oldReview?.value : '';
    const reasonOpen=Boolean(reasonDraft || oldReview?.closest('details')?.open && oldReview.dataset.candidate===candidate?.assignment);
    const oldSignoff=detail.querySelector('input[data-signoff]');
    const optionalSignoff=oldSignoff?.dataset.signoff===d.id+'/'+d.revision && oldSignoff.checked;
    const saved=keep?oldInput.value:'';
    const checked=new Set(keep?[...detail.querySelectorAll('input:checked')].map(i=>i.value):[]);
    key=signature;detail.replaceChildren();by('ticket-ref').textContent=d.id.slice(-8)+' / v'+d.revision;
    const v=d.journey,g=d.goal_contract||{},contract=d.acceptance_contract||{};
    const binding={ticket_id:d.id,revision:d.revision,goal_revision:d.goal_revision,acceptance_revision:d.acceptance_revision};
    const command=(name,extra={})=>call(name,{...binding,...extra,request_id:crypto.randomUUID()});
    const heading=element('h3',title(d));heading.className='ticket-title';detail.append(heading);
    const meta=element('p',v.label+' · 目标 v'+d.goal_revision+' · 验收 v'+d.acceptance_revision);
    meta.className='meta';detail.append(meta);
    const decision=element('section');decision.id='decision';decision.className='decision';
    decision.append(element('h4',v.station==='decision'?'需要你的决定':v.label),element('p',v.instruction));detail.append(decision);
    if(d.question?.answer_status!=='answered' && d.question?.reason)decision.append(element('p',d.question.reason));
    if(e?.error && v.instruction!==e.error)show(decision,'执行反馈',e.error);
    show(decision,'验收缺口',contract.verification_gaps);
    show(decision,'人工验收',contract.human_review_requirements);
    for(const item of v.decision_items || [])show(decision,item.label,item.text);
    const spec=fold(detail,'目标、验收条件与授权范围',v.approve);spec.id='scope';
    const fields={deliverables:'交付物',required_capabilities:'必需能力',nonfunctional_requirements:'质量要求',constraints:'限制',assumptions:'假设',out_of_scope:'范围外'};
    const replaceable=[];
    for(const [field,label] of Object.entries(fields)){
      if(!g[field]?.length)continue;
      spec.append(element('h4',label));const ul=element('ul');spec.append(ul);
      for(const r of g[field]){
        const source={USER_EXPLICIT:'用户原文',USER_CONFIRMED:'用户确认',CONTEXT_DERIVED:'上下文推断',MODEL_PROPOSED:'模型建议'};
        ul.append(element('li',r.description+' · '+(source[r.source] || r.source)));
        if(['USER_EXPLICIT','USER_CONFIRMED'].includes(r.source))replaceable.push(r);
      }
    }
    const criteria=element('div');criteria.id='criteria';spec.append(criteria);if(!contract.criteria?.length)criteria.append(element('p','本版验收条件尚待整理。'));
    for(const c of contract.criteria||[])show(criteria,c.description,{验证器:c.verifier_id,版本:c.verifier_version,输入:c.test_spec,预期:c.expected_result});
    const budget=element('div');budget.id='budget';spec.append(budget);show(budget,'权限与预算',d.approval_terms);if(!d.approval_terms)budget.append(element('p','当前尚无可批准的授权预算。'));
    if(v.approve && d.approval_terms){
      const required=Boolean(contract.human_review_requirements?.length);
      const label=element('label'), signoff=element('input');
      signoff.type='checkbox'; signoff.checked=required||optionalSignoff; signoff.disabled=required;
      signoff.dataset.signoff=d.id+'/'+d.revision;
      label.append(signoff,document.createTextNode(required?'包含人工验收条款，交付必须签收':'机器验收通过后由我签收交付'));
      spec.append(label);
      action(spec,'批准本版目标与验收',async()=>{
        const mode=signoff.checked?'机器验收通过后，最终交付还需要你签收。':'机器验收通过后将归档交付。';
        if(confirm('批准上列目标、验收、权限与预算并开始执行？'+mode)){
          await command('approve',{require_signoff:signoff.checked});
          notice('本版授权已保存。');
        }
      },true);
    }
    {
      const plan=e?.plan?.steps || [], checks=flow?.validation?.checks || [];
      const box=fold(detail,'执行进度与交付'+(plan.length?' · '+Math.min(e.step,plan.length)+' / '+plan.length:''),
        v.station==='delivery'||v.station==='flight'||v.artifact_review);box.id='progress';
      if(plan.length){const steps=element('ol');box.append(steps);for(const p of plan)steps.append(element('li',(p.state==='completed'?'✓ ':'')+(p.title||p.objective)));}
      if(!plan.length)box.append(element('p','尚无已登记的执行步骤。'));
      const evidence=element('div');evidence.id='checks';box.append(evidence);
      for(const c of checks)evidence.append(element('p',c.status+' · '+(c.description||c.id)+' · '+(c.detail||'')));
      if(!checks.length)evidence.append(element('p','尚无逐项验收结果。'));
      const artifacts=element('div');artifacts.id='artifacts';box.append(artifacts);
      if(d.prior_deliveries?.length){
        const past=fold(artifacts,'历次授权中的作品');
        for(const item of d.prior_deliveries){
          show(past,'授权 v'+item.mandate_revision+' · 交付 v'+item.sequence,item.artifact_sha256);
          if(item.reason)show(past,'退回原因',item.reason);
          for(const path of Object.keys(item.files || {})){
            const link=element('a','下载 '+path);
            link.href='/api/tasks/'+encodeURIComponent(d.id)+'/artifact?'+new URLSearchParams({path,candidate:item.assignment});
            past.append(link,element('br'));
          }
        }
      }
      if(candidate){
        const states={pending:'待签收',returned:'已退回',accepted:'已签收'};
        artifacts.append(element('h4','交付 v'+candidate.sequence+' · '+states[candidate.state]),
          element('p','目标 v'+candidate.goal_revision+' · 验收 v'+candidate.acceptance_revision+' · 授权 v'+candidate.mandate_revision));
        show(artifacts,'归档摘要 SHA-256',candidate.artifact_sha256);
        if(candidate.human_criteria?.length)show(artifacts,'本人验收 · '+states[candidate.state],
          candidate.human_criteria.map(c=>c.description).join('\n'));
        if(candidate.state==='pending'||candidate.state==='accepted'){
          for(const [path,sha] of Object.entries(candidate.files||{})){
            const link=element('a','下载 '+path);
            link.href='/api/tasks/'+encodeURIComponent(d.id)+'/artifact?'+new URLSearchParams({path,candidate:candidate.assignment});
            artifacts.append(link);
            show(artifacts,'文件 SHA-256',sha);
          }
        }
        const returned=reviews.filter(r=>r.state==='returned');
        if(returned.length){
          const history=fold(artifacts,'退回记录（'+returned.length+'）',candidate.state==='returned');
          for(const row of [...returned].reverse()){
            show(history,'交付 v'+row.sequence+' · '+row.artifact_sha256,row.reason||'无记录');
            for(const path of Object.keys(row.files || {})){
              const link=element('a','下载 v'+row.sequence+' · '+path);
              link.href='/api/tasks/'+encodeURIComponent(d.id)+'/artifact?'+new URLSearchParams({path,candidate:row.assignment});
              history.append(link,element('br'));
            }
          }
        }
        if(v.artifact_review && candidate.state==='pending'){
          const reviewed={candidate:candidate.assignment,artifact_sha256:candidate.artifact_sha256};
          const link=element('a','查看交付 v'+candidate.sequence+' 的文件与摘要');link.href='#artifacts';decision.append(link);
          action(artifacts,'签收本版交付',async()=>{
            if(confirm('确认签收交付 v'+candidate.sequence+'？')){
              await command('accept_artifact',reviewed);
              notice('已签收本版交付。');
            }
          },true);
          const rejection=fold(artifacts,'退回本版交付',reasonOpen), reason=element('textarea'), label=element('label','这版需要改哪里？');
          reason.value=reasonDraft||'';reason.maxLength=1000;reason.dataset.candidate=candidate.assignment;
          label.append(reason);rejection.append(label);
          action(rejection,'提交退回原因',async()=>{
            if(!reason.value.trim()){reason.focus();return notice('请先填写退回原因。');}
            await command('return_artifact',{...reviewed,reason:reason.value.trim()});
            notice('退回原因已记录。');
          });
        }
      }
      for(const path of e?.artifacts||[]){
        const a=element('a','下载 '+path);a.href='/api/tasks/'+encodeURIComponent(e.id)+'/artifact?path='+encodeURIComponent(path)+'&version='+e.version;
        artifacts.append(a,element('br'));
      }
      if(!e?.artifacts?.length && !candidate)artifacts.append(element('p','尚无可下载的交付文件。'));
      if(e)action(box,'查看完整执行时间线',()=>openTask(e.id));
    }
    if(!v.artifact_review && !['PAUSED','STOPPED'].includes(d.status)){
      const q=d.question?.answer_status==='answered'?null:d.question;
      const edits=fold(detail,q?'回复澄清':'补充或修改要求',Boolean(q)||d.status==='VERIFICATION_GAP'||v.station==='decision'&&!v.approve);
      edits.id='revisions';const choices=[];
      if(!q && replaceable.length){
        const group=fold(edits,'选择要替换的原要求');
        for(const r of replaceable){
          const line=element('label',r.description),i=element('input');i.type='checkbox';i.value=r.requirement_id;
          i.checked=checked.has(i.value);line.prepend(i);group.append(line);choices.push(i);
        }
      }
      const field=element('textarea');field.maxLength=6000;field.value=saved;field.dataset.binding=d.id+'/'+d.revision;
      const label=element('label',q?.question_text || '补充内容或新的要求');label.append(field);edits.append(label);
      for(const option of q?.options || []){const b=element('button',String(option));b.onclick=()=>{field.value=String(option);field.focus();};edits.append(b);}
      action(edits,q?'提交澄清':'提交当前工单修改',async()=>{
        const selected=choices.filter(i=>i.checked).map(i=>i.value);
        if(!field.value.trim()){field.focus();return notice('请先填写内容。');}
        if(selected.length&&!confirm('替换所选要求并重新核对契约？'))return;
        const res=await command('submit',{text:field.value.trim(),question_id:q?.question_id,replace_requirement_ids:selected.length?selected:undefined});
        event=res.event_id;field.value='';notice('修改已登记。');
      },true);
    }
    if(!by('revisions')){const msg=element('p','这张工单当前不可修改。');msg.id='revisions';detail.append(msg);}
    if(v.pause)action(detail,'暂停此单',()=>command('pause'));
    if(v.resume)action(detail,'继续此单',()=>command('resume'));
    if(v.stop)action(detail,'停止此单',async()=>{if(confirm('停止这张工单？'))await command('stop');});
  }
  let initial=true, destination=location.hash.slice(1);
  function focusSection(d){
    if(!destination||destination==='outer-history')return;
    const id=destination;destination='';
    if(id==='timeline'&&d.execution){openTask(d.execution.id);return;}
    const target=by(id)||(id==='timeline'?by('progress'):null)||detail;
    if(target instanceof HTMLDetailsElement)target.open=true;
    const parent=target.closest('details');if(parent)parent.open=true;
    target.scrollIntoView();
  }
  async function refresh(){
    if(active || area.hidden)return;
    const pass=++generation;
    try{
      const data=await request('/api/outer');if(pass!==generation)return;rows=data.drafts||[];
      if(initial){initial=false;
        if(oldTask&&!chosen&&!rows.some(r=>r.id===oldTask)){by('execution').open=true;by('execution').scrollIntoView();}
        else if(filter&&!chosen)chosen=rows.find(r=>r.journey.station===filter)?.id||'';
        if(!chosen&&!filter&&(!oldTask||rows.some(r=>r.id===oldTask)))chosen=rows.find(r=>r.id===oldTask)?.id || rows.find(r=>r.journey.station==='decision')?.id || rows[0]?.id || '';
      }
      if(event){
        const current=await call('event',{event_id:event});if(pass!==generation)return;
        if(current.choices?.length){
          key='';historyPanel.select('');by('ticket-ref').textContent='';detail.replaceChildren(element('h3','这条需求属于哪张工单？'));
          const original=event;
          for(const c of [...current.choices,{id:null,summary:'新建工单'}])action(detail,c.summary||c.id,async()=>{
            const res=await call('select',{event_id:original,ticket_id:c.id,request_id:crypto.randomUUID()});
            chosen=res.ticket_id||'';event=res.event_id||'';filter='';updateUrl();
          });drawList();return;
        }
        if(current.ticket_id&&chosen!==current.ticket_id){chosen=current.ticket_id;filter='';updateUrl();}
        if(['DONE','REJECTED','NEEDS_ATTENTION','CANCELLED'].includes(current.state)){
          if(current.error||current.state!=='DONE')notice('本次请求需要处理 · '+(current.error||'请核对当前版本后重试。'));
          event='';
        }
      }
      drawList();historyPanel.select(chosen);
      if(!chosen){key='';detail.replaceChildren(element('p',event?'目标已登记，正在整理。':'选择工单查看决定与进度。'));return;}
      const d=await call('get',{ticket_id:chosen});
      if(pass===generation && d.id===chosen){showDraft(d);focusSection(d);await historyPanel.refresh(d.id,()=>pass===generation && d.id===chosen);}
    }catch(err){if(pass===generation)notice(err.message);}
  }
  by('launch').hidden=false;by('launch').onclick=()=>composer.showModal();by('dismiss').onclick=()=>composer.close();
  for(const [id,label] of Object.entries(stations)){
    const b=element('button',label);b.dataset.station=id;b.onclick=()=>changeFilter(id);by('stations').append(b);
  }
  by('all-stations').onclick=()=>changeFilter('');
  form.oninput=()=>{pending='';};
  form.onsubmit=async ev=>{
    ev.preventDefault();if(active || !form.elements.goal.value.trim())return;
    active=true;generation++;
    try{pending ||= crypto.randomUUID();const res=await call('submit',{text:form.elements.goal.value.trim(),request_id:pending});
      chosen=res.ticket_id||'';event=res.event_id;filter='';pending='';key='';historyPanel.select(chosen);
      form.reset();composer.close();updateUrl();by('tickets').scrollIntoView();notice('目标已登记。');
    }catch(err){notice(err.message);}finally{active=false;await refresh();}
  };
  area.hidden=false;by('execution').open=false;by('new').hidden=true;
  if(query.has('new')){composer.showModal();const url=new URL(location.href);url.searchParams.delete('new');history.replaceState(null,'',url);}
  refresh();setInterval(()=>{if(!document.hidden)refresh();},3500);
}
