import {markdown} from './task_presentation.js';

// 目标模式和普通任务共享首页。每张目标工单的决定绑定到服务端的当前对象。
const queues = {
  decision: ['等你拍板', '现在没有需要你决定的事'],
  flight: ['正在推进', '当前没有正在推进的工单'],
  delivery: ['已交付', '交付后，成果会出现在这里'],
  dock: ['已暂停与停止', '没有已暂停或停止的工单'],
  unknown: ['待核对', '没有待核对的工单'],
};
const tabs = { progress: '进度', artifacts: '成果与验收', decisions: '决定与记录' };
const anchors = {
  mission: 'progress', decision: 'progress', scope: 'progress', criteria: 'progress',
  budget: 'progress', progress: 'progress', checks: 'artifacts', artifacts: 'artifacts', 'blind-review': 'artifacts',
  feedback: 'decisions', revisions: 'decisions', 'outer-history': 'decisions', timeline: 'decisions', decisions: 'decisions',
};
const titleOf = row => row.workbench_name || row.goal_contract?.goal_statement || row.summary || row.name || row.id;
const preferenceOf = row => row.execution?.pending_preference || row.pending_preference;
const directionQuestion = question => question?.requires_option===true ||
  (Number.isInteger(question?.step) && question.step>0);
const stepLabel = step => Number.isInteger(step) && step>=0 ? '第 '+(step+1)+' 步' : '';
const pendingTicketSelection = receipt => Boolean(!receipt?.selected_by &&
  (receipt?.router_selection===true || receipt?.choices?.length));
const needsModelConsent = row => row.needs_model_consent===true && !['PAUSED','STOPPED'].includes(row.status);
const currentClarification = row => row.status === 'AWAITING_CLARIFICATION' &&
  String(row.question?.answer_status).toLowerCase() !== 'answered' && row.question?.question_id;
const deliveriesOf = row => row.execution?.goal_execution?.artifact_reviews || [];
const canExtendBudget = row => row.status==='AUTHORIZED' &&
  (row.execution?.status || row.execution_status)==='budget_exhausted' &&
  !row.execution?.paused && Number.isInteger(row.mandate_revision) && Boolean(row.mandate?.execution_profile);
const sameBlindCandidate = (review,candidate) => Boolean(review && candidate &&
  review.candidate===candidate.assignment && review.artifact_sha256===candidate.artifact_sha256);
const blindRequired = (row,candidate) => sameBlindCandidate(row.blind_review,candidate) ||
  Boolean(row.acceptance_contract?.criteria?.some(item=>item.required && item.verifier_id==='blind-projection'));
const currentBlindStatus = (row,candidate) => row.execution
  ? sameBlindCandidate(row.blind_review,candidate) && row.blind_review.current!==false && row.blind_review.status
  : row.blind_status;
const blindLocked = (row,candidate) => currentBlindStatus(row,candidate)==='locked';
const canReadInnerRepairs = row => {
  const candidate=deliveriesOf(row).at(-1);
  return !blindRequired(row,candidate) || blindLocked(row,candidate) || row.execution?.status==='completed';
};
const innerRepairStates = {
  repairing:'执行者正在返修', rechecking:'负责人正在复验新版',
  passed:'负责人复验通过', blocked:'返修受阻', waiting:'原版和问题已保存，等待续办',
  waiting_review:'新版已保存，等待负责人复验',
};
const teamReviewModes = {
  adaptive_review: {label:'简单直接做，复杂再比较', scope:'负责人和执行者默认使用 Sol，只读顾问默认使用 Astra。复杂工单才比较方案，只有重要取舍需要你决定。'},
  review: {label:'主动比较双方案并审稿', scope:'负责人和顾问分别提出方案，确定做法后，由顾问查看初稿并提出修改意见。'},
  review_and_decide: {label:'分步交付，第 2 步请我定方向', scope:'适合至少两步能分别验收的工单。第 2 步初稿完成后请你定方向。'},
};
const canReviseAtDecision = row => row.journey?.modify ?? (row.status==='VERIFICATION_GAP');
const blindOutcomes = {win:'团队胜',tie:'相当',loss:'团队负',unrated:'未评'};
const time = value => value && !Number.isNaN(Date.parse(value)) ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '';
const renderValue = value => typeof value === 'string' ? value : value?.description || JSON.stringify(value) || '';
const phases = {plan:'规划',replan:'重新规划',execute:'实施',repair:'修复',review:'独立核验',coordinate:'协调',package:'工作包'};
const stepStates = {completed:'已完成',done:'已完成',running:'正在进行',in_progress:'正在进行',executing:'正在进行',pending:'待开始',failed:'未通过'};
const assignmentStates = {pending:'待投递',queued:'已排队',dispatched:'已派发',settled:'已结算',cancelled:'已取消',unknown:'待核对'};

export function projectGoalRow(row,{allowFreeText=true}={}) {
  const view = row.journey || {};
  const preference = preferenceOf(row);
  const clarification = currentClarification(row);
  const phase = row.execution?.status || row.execution_status;
  const candidate = deliveriesOf(row).at(-1);
  const canReview = view.artifact_review && (!row.execution || candidate?.state === 'pending');
  const stopping = phase === 'stopping';
  let station = view.station || 'unknown', anchor = station === 'delivery' ? 'artifacts' : 'progress';
  let label = view.label || '待核对', reason = view.instruction, next = view.next;
  const sequence = candidate?.sequence || row.delivery_sequence;
  if (station==='delivery' && sequence) label='v'+sequence+' 已交付';
  // 新版的列表与原单共用后台投影，旧版原单仍可从当前派工回执还原状态。
  const dispatchPhase=row.execution_phase || view.phase || row.execution?.goal_execution?.assignments?.findLast(a=>a.state==='dispatched')?.phase;
  if (station==='flight' && phase==='reviewing' && !Object.hasOwn(view,'phase')) label=dispatchPhase==='coordinate'?'正在协调返工':'正在独立核验';
  if (station==='flight' && row.last_verified) reason=row.last_verified;
  if (station==='flight' && phase==='executing' && !Object.hasOwn(view,'inner_repair_status') && canReadInnerRepairs(row)) {
    const assignment=row.execution?.goal_execution?.assignments?.at(-1);
    const repair=assignment?.inner_repairs?.at(-1);
    if (assignment?.state==='dispatched' && ['repairing','rechecking'].includes(repair?.status))
      label=innerRepairStates[repair.status];
  }
  let action = station === 'delivery' ? '查看交付' : station === 'flight' ? '看进度' : '查看工单';
  const uncertain = row.observation_stale || row.execution?.observation_stale ||
    row.execution?.runs?.some(run=>run.state==='unknown') ||
    row.execution?.goal_execution?.assignments?.some(attempt=>
      ['pending','dispatched'].includes(attempt.state) && attempt.transport==='unknown');
  if (uncertain) {
    station='unknown'; label='待核对'; reason='尚无可确认的本轮回执。'; action='查看原因';
  } else if (stopping) {
    station = 'flight'; label = '正在停止'; reason = '等待本轮退出'; action = '看进度';
  } else if (needsModelConsent(row)) {
    station='decision'; anchor='decision'; label='待授权读取原话';
    reason='原话已登记在本机。请核对是否可交给本单团队。'; next='同意后，团队会开始整理目标和验收条件。';
    action='查看原话与授权';
  } else if (station === 'decision') {
    anchor = 'decision';
    if (preference && row.status !== 'PAUSED' && row.status !== 'STOPPED') {
      const direction=directionQuestion(preference);
      label = direction ? stepLabel(preference.step)+'待定方向' : '待选样例';
      reason = preference.prompt || reason;
      next = allowFreeText ? preference.next || (direction ? '选择修改方向，或写下希望保留和修改的内容。' : '选择一组，或写下希望保留和修改的内容。') :
        (direction ? '选择一个修改方向。' : '选择一组样例。');
      action = direction ? '查看修改方向' : '查看样例';
    } else if (clarification) {
      label = '待澄清'; reason = row.question.reason || row.question.question_text || reason;
      next = row.question.question_text; action = allowFreeText ? '回答澄清' : '查看待澄清事项';
    } else if (view.approve) {
      label = '待批准'; next = '核对交付、验收、权限与预算，再决定是否批准。'; action = '查看本版授权';
    } else if (row.status === 'AWAITING_APPROVAL' && view.pause) {
      label = '待暂停旧版'; next = '暂停旧执行，停稳后再批准新版。'; action = '查看本版授权';
    } else if (canExtendBudget(row)) {
      label='待补预算';
      next='核对当前额度，决定要追加的时间或角色轮次。'; action='查看预算并决定';
    } else if (canReview) {
      const blindStatus=currentBlindStatus(row,candidate);
      if (blindStatus==='awaiting_ballot') {
        anchor='blind-review'; label='待盲评'; reason='这版需要先完成匿名对照评价。';
        next='逐例比较甲和乙，锁定这一版的评语。'; action='查看匿名材料';
      } else if (blindStatus==='locked') {
        anchor='artifacts'; label='已锁票 · 待签收';
        next='核对已锁定的评价和当前交付，再签收或退回。'; action='查看待签收版本';
      } else if (blindRequired(row,candidate)) {
        station='unknown'; anchor='blind-review'; label='匿名材料待核对';
        reason='当前候选需要匿名评阅，正式材料就绪后会出现在原单。'; action='查看材料状态';
      } else {
        anchor='artifacts'; label = sequence ? '交付 v'+sequence+' · 待签收' : '待签收';
        if (!allowFreeText) reason='核对当前候选的交付文件和验收结果。';
        next = allowFreeText ? '核对当前候选和验收结果，签收或退回。' : '核对通过后签收交付。'; action = '查看待签收版本';
      }
    } else if (canReviseAtDecision(row)) {
      label = view.label || '待补条件';
      if (allowFreeText) { anchor='revisions'; next = view.next || '补充可验证的条件或说明需要调整的要求。'; action = '查看并补充'; }
      else {
        station='unknown'; anchor='criteria';
        next='本单仅支持查看验收条件与当前授权。'; action='查看验收条件';
      }
    } else {
      station = 'unknown'; anchor = 'progress'; label = view.label || '待核对'; action = '查看原因';
    }
  }
  return {
    title: titleOf(row), id: row.id, station, anchor, label, reason, next, action, routerTeam: Boolean(row.ticket_team),
    at: (station==='delivery' && row.delivery_at) || row.last_business_at || row.execution?.updated_at || row.updated_at || '',
    waitingSince: preference?.created_at || row.decision_since || row.question?.created_at || row.updated_at || '',
    due: preference?.due_at || row.decision_due_at || '',
  };
}

// 工单可退回早先阶段。只标明已知的当前位置，历史进度由有版本的回执说明。
function workflowSteps(row, model) {
  if (['unknown','dock'].includes(model.station) || ['PAUSED','STOPPED'].includes(row.status)) return null;
  const phase=row.execution_phase || row.journey?.phase;
  const status=row.execution?.status || row.execution_status;
  let current;
  if (model.station==='delivery') current=3;
  else if (['DRAFT','ASSESSING','AWAITING_APPROVAL','AWAITING_CLARIFICATION','VERIFICATION_GAP','NEEDS_ATTENTION'].includes(row.status)) current=0;
  else if (row.status==='AUTHORIZED') {
    if (status==='awaiting_artifact_review') current=3;
    else if (phase==='review' || (!Object.hasOwn(row.journey || {},'phase') && !phase && status==='reviewing')) current=2;
    else current=1;
  } else return null;
  const stages=el('ol',null,'goal-workflow');
  const labels = row.ticket_team ? ['目标与授权','团队审稿与执行','独立验收','交付与回执'] :
    ['接单与批准','派工与推进','独立核验','签收与交付'];
  stages.setAttribute('aria-label','工单所在环节');
  for (const [index,label] of labels.entries()) {
    const item=el('li',label);
    if (index===current) { item.setAttribute('aria-current','step'); item.append(el('small','当前')); }
    stages.append(item);
  }
  return stages;
}

export function sortQueue(rows, station) {
  return [...rows].sort((a,b) => station === 'decision'
    ? (a.due || '\uffff').localeCompare(b.due || '\uffff') ||
      a.waitingSince.localeCompare(b.waitingSince) || a.id.localeCompare(b.id)
    : b.at.localeCompare(a.at) || a.id.localeCompare(b.id));
}

const el = (tag, text, cls) => {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (cls) node.className = cls;
  return node;
};
export function mountTeamReviewControl(anchor, initial='') {
  const field=el('label','团队审稿');
  field.id='team-review-field';
  const select=el('select');
  select.id='team-review-mode';
  select.name='team_review_mode';
  const automatic=el('option','简单直接做，复杂再比较');
  automatic.value='';
  select.append(automatic);
  for (const value of ['review','review_and_decide']) {
    const {label}=teamReviewModes[value];
    const option=el('option',label);
    option.value=value;
    select.append(option);
  }
  if (['review','review_and_decide'].includes(initial)) select.value=initial;
  const hint=el('small','请写明至少两步可以分别验收的成果。','goal-muted');
  hint.id='team-review-hint';
  const showHint=()=>{hint.hidden=select.value!=='review_and_decide';};
  select.onchange=showHint;
  select.setAttribute('aria-describedby',hint.id);
  field.append(select,hint);
  anchor.after(field);
  const sync=enabled=>{field.hidden=!enabled;select.disabled=!enabled;showHint();};
  sync(false);
  return sync;
}

export function projectTeamHandoffs(row) {
  if (!canReadInnerRepairs(row)) return [];
  const flow=row.execution?.goal_execution || {};
  const assignments=flow.assignments || [];
  const accepted=new Set((flow.decisions || []).filter(record=>record.kind==='checkpoint_saved')
    .map(record=>record.assignment));
  const checked=new Map(assignments.filter(assignment=>accepted.has(assignment.id)).flatMap(assignment=>{
    const evidence=assignment.report?.handoff_verification?.verification;
    return evidence?.passed===true ? [[assignment.step+':'+evidence.before_sha256,evidence]] : [];
  }));
  return assignments.flatMap(assignment=>{
    const handoff=assignment.report?.team_handoff;
    return handoff ? [{id:assignment.id,step:stepLabel(assignment.step),run:assignment.run,
      comparison:handoff.comparison?.summary,review:handoff.draft_review?.summary,
      instruction:handoff.instruction?.summary,retain:handoff.instruction?.retain,
      verify:handoff.instruction?.verify,draft:handoff.draft_sha256,
      observations:handoff.draft_review?.observations,
      checked:checked.get(assignment.step+':'+handoff.draft_sha256)}] : [];
  }).reverse();
}

export function preferenceDirectionConflict(option, text) {
  return option?.mode==='keep' && text.trim() ? '沿用现稿请清空意见。有补充意见时请选择可修改的方向。' : '';
}
function draftPreviewKey(row, question) {
  if (!directionQuestion(question) || !question.draft_sha256) return '';
  return JSON.stringify([row.id,question.id,question.revision,question.candidate_digest,question.draft_sha256]);
}
function stagedPreferenceKey(row, question) {
  if (!directionQuestion(question) || !question.draft_sha256) return '';
  return JSON.stringify([row.id,row.revision,row.goal_revision,row.acceptance_revision,
    question.id,question.revision,question.candidate_digest,question.draft_sha256]);
}
function button(label, action, kind='') {
  const node = el('button',label,'goal-button'+(kind ? ' '+kind : ''));
  node.type = 'button'; node.onclick = action;
  return node;
}
function details(title, id, open=false) {
  const box = el('details');
  box.open = open; box.dataset.disclosure = id;
  box.append(el('summary',title));
  return box;
}
function textList(parent, title, items) {
  if (!Array.isArray(items) || !items.length) return;
  if (title) parent.append(el('h4',title));
  const list = el('ul');
  for (const item of items) list.append(el('li',renderValue(item)));
  parent.append(list);
}
function copy(parent, title, content) {
  if (content == null || content === '') return;
  parent.append(el('h4',title),el('p',renderValue(content),'goal-copy-block'));
}
function position(parent) {
  const active = parent.contains(document.activeElement) ? document.activeElement : null;
  return {
    id: active?.id, start: active?.selectionStart, end: active?.selectionEnd,
    open: new Map([...parent.querySelectorAll('details[data-disclosure]')].map(e => [e.dataset.disclosure,e.open])),
  };
}
function restorePosition(parent, saved) {
  for (const item of parent.querySelectorAll('details[data-disclosure]'))
    if (saved.open.has(item.dataset.disclosure)) item.open = saved.open.get(item.dataset.disclosure);
  const active = saved.id && document.getElementById(saved.id);
  if (active && active!==document.activeElement && parent.contains(active) && !active.disabled) {
    active.focus({preventScroll:true});
    if (Number.isInteger(saved.start)) active.setSelectionRange(saved.start,saved.end);
  }
}
function draftStore() {
  const content = new Map();
  return key => {
    if (!content.has(key)) {
      let saved;
      try { saved = JSON.parse(sessionStorage.getItem('autoloop-goal-draft:'+key)); } catch {}
      content.set(key,saved || {});
    }
    const draft = content.get(key);
    return {
      data: draft,
      save(fields) {
        Object.assign(draft,fields);
        try { sessionStorage.setItem('autoloop-goal-draft:'+key,JSON.stringify(draft)); } catch {}
      },
      clear() {
        content.delete(key);
        try { sessionStorage.removeItem('autoloop-goal-draft:'+key); } catch {}
      },
    };
  };
}

// 同一候选的预览与下载均由服务端复核归档字节。
async function getFile(id, candidate, path) {
  const url='/api/tasks/'+encodeURIComponent(id)+'/artifact?'+new URLSearchParams({candidate,path});
  const response=await fetch(url,{cache:'no-store'});
  if(!response.ok)throw Error((await response.json().catch(()=>({}))).error || '文件暂不可读取');
  return response;
}
function previewTextFile(id, candidate, path, parent) {
  const preview=details('预览 '+path,'preview-'+candidate+'/'+path);
  const content=el('div'); content.textContent='正在核对文件…';
  preview.append(content); parent.append(preview);
  let reading=false, done=false;
  preview.ontoggle=async()=>{
    if(!preview.open || reading || done)return;
    reading=true;
    try {
      const response=await getFile(id,candidate,path);
      const text=await response.text();
      content.replaceChildren();
      const limit=12000;
      const source=text.slice(0,limit);
      if (/\.html?$/i.test(path)) {
        const reader=el('div',null,'goal-draft-reader goal-draft-rendered');
        reader.tabIndex=0; reader.setAttribute('aria-label',path+'静态正文');
        reader.append(staticMarkup(source));
        if (!reader.textContent.trim()) reader.append(el('p','这份文件没有可预览的静态正文。'));
        content.append(reader);
      } else content.append(draftSource(source,path+'文件内容'));
      if (text.length>limit) content.append(el('small','预览到此。完整内容可下载。','goal-muted'));
      done=true;
    } catch(error) { content.textContent='暂无法核对文件。'+error.message; }
    finally { reading=false; }
  };
}
function downloadFile(id, candidate, path, parent, label) {
  const node = button(label,async () => {
    if (node.disabled) return;
    node.disabled = true; status.textContent = '正在核对文件…';
    try {
      const response=await getFile(id,candidate,path);
      const object = URL.createObjectURL(await response.blob());
      const link = el('a'); link.href = object; link.download = path.split('/').at(-1);
      document.body.append(link); link.click(); link.remove();
      setTimeout(()=>URL.revokeObjectURL(object),1000);
      status.textContent = '文件已核对，浏览器正在下载。';
    } catch (error) {
      status.textContent = '文件暂无法核对。'+error.message;
    } finally { node.disabled = false; }
  });
  const status = el('small','','goal-muted'); status.setAttribute('role','status');
  parent.append(node,status);
}

const staticElements = new Set(('article aside b blockquote br caption cite code col colgroup dd del div dl dt em figcaption figure footer h1 h2 h3 h4 h5 h6 header hr i ins kbd li main mark ol p pre q s samp section small span strong sub sup table tbody td tfoot th thead time tr u ul var').split(' '));
const inertWrappers = new Map([['a','span'],['button','span'],['details','div'],['form','div'],['label','span'],['summary','div']]);
const skippedElements = new Set(('applet audio base canvas embed frame frameset head iframe link meta noscript object script style template title video').split(' '));
function staticMarkup(source) {
  // 在惰性模板中解析，只把白名单元素和文本重建到工作台。原稿属性不会进入页面。
  const template=el('template');
  template.innerHTML=source;
  const result=document.createDocumentFragment();
  const pending=[[template.content.firstChild,result]];
  while (pending.length) {
    const [node,parent]=pending.pop();
    if (!node) continue;
    if (node.nextSibling) pending.push([node.nextSibling,parent]);
    if (node.nodeType===Node.TEXT_NODE) { parent.append(document.createTextNode(node.data)); continue; }
    if (node.nodeType!==Node.ELEMENT_NODE || node.namespaceURI!=='http://www.w3.org/1999/xhtml' ||
        skippedElements.has(node.localName) || node.hasAttribute('hidden')) continue;
    const name=node.localName;
    if (name==='img') {
      const alt=node.getAttribute('alt');
      if (alt) parent.append(el('span','[图片 · '+alt+']','goal-muted'));
      continue;
    }
    const tag=staticElements.has(name) ? name : inertWrappers.get(name);
    const target=tag ? el(tag) : parent;
    if (tag) {
      if (tag==='td' || tag==='th') {
        for (const attr of ['colspan','rowspan']) {
          const value=node.getAttribute(attr);
          if (/^[1-9]\d?$/.test(value)) target.setAttribute(attr,value);
        }
      }
      parent.append(target);
    }
    if (node.firstChild) pending.push([node.firstChild,target]);
  }
  return result;
}
function draftSource(content, label) {
  const source=el('pre',content,'goal-draft-reader goal-draft-source');
  source.tabIndex=0;
  source.setAttribute('aria-label',label);
  return source;
}
function showDraftFile(parent, file) {
  const {path,kind,content}=file;
  if (kind==='html' || kind==='markdown') {
    const reader=el('div',null,'goal-draft-reader goal-draft-rendered');
    reader.tabIndex=0;
    reader.setAttribute('aria-label',path+'静态预览');
    const parsed=staticMarkup(kind==='markdown' ? markdown(content) : content);
    if (parsed.textContent?.trim() || parsed.querySelector('hr, table')) reader.append(parsed);
    else reader.append(el('p','此文件没有可显示的静态正文，可查看完整源码。','goal-muted'));
    const source=details('查看完整源码','draft-source-'+path);
    source.append(draftSource(content,path+'完整源码'));
    parent.append(reader,source);
  } else if (kind==='image' && /^data:image\/(?:png|jpeg|gif|webp|avif|bmp);base64,[a-z\d+/=]+$/i.test(content)) {
    const image=el('img',null,'goal-draft-image');
    image.alt=path; image.src=content; parent.append(image);
  } else if (kind==='text') {
    parent.append(draftSource(content,path+'完整内容'));
  } else {
    parent.append(el('p','此文件格式暂不支持内嵌预览。','goal-muted'));
  }
}
async function readDraftPreview(url, onInvalid) {
  const response=await fetch(url,{cache:'no-store'});
  if (response.status===403 || response.status===409) {
    onInvalid(response.status);
    return null;
  }
  const result=await response.json().catch(()=>({}));
  if (!response.ok) throw Error(result.error || '封存初稿暂不可读取。');
  return result;
}
function mountDraftPreview(parent, row, q, onManifest, onInvalid) {
  const card=el('section',null,'goal-draft-preview');
  const disclosure=details('查看这版初稿','draft-preview-'+q.id);
  disclosure.className='goal-draft-disclosure';
  const title=disclosure.firstElementChild; title.id='goal-draft-title';
  card.setAttribute('aria-labelledby',title.id);
  const status=el('p','','goal-muted'); status.setAttribute('role','status');
  const loadAgain=button('重试读取初稿',()=>loadManifest()); loadAgain.hidden=true;
  const filePicker=el('label','初稿文件'); filePicker.hidden=true;
  const select=el('select'); select.id='goal-draft-file'; filePicker.append(select);
  const body=el('div');
  disclosure.append(filePicker,body);
  card.append(disclosure,status,loadAgain);
  parent.append(card);
  const files=new Map();
  const url='/api/tasks/'+encodeURIComponent(row.id)+'/draft-preview?';
  const params={question_id:q.id,question_revision:q.revision,candidate_digest:q.candidate_digest,draft_sha256:q.draft_sha256};
  function read(path) {
    return readDraftPreview(url+new URLSearchParams(path===undefined?params:{...params,path}),status=>{
      if (card.isConnected) onInvalid(status);
    });
  }
  function showFile(path) {
    for (const [key,panel] of files) panel.hidden=key!==path;
    if (files.has(path)) return;
    const panel=el('div',null,'goal-draft-file');
    const feedback=el('p','','goal-muted'); feedback.setAttribute('role','status');
    const retry=button('重试读取文件',()=>load()); retry.hidden=true;
    panel.append(feedback,retry); files.set(path,panel); body.append(panel);
    async function load() {
      retry.hidden=true; feedback.textContent='正在核对初稿文件…';
      try {
        const file=await read(path);
        if (file===null || !card.isConnected) return;
        if (file.path!==path || typeof file.content!=='string') throw Error('初稿文件信息不一致。');
        feedback.textContent='';
        showDraftFile(panel,file);
      } catch (error) {
        feedback.textContent=error.message;
        retry.hidden=false;
      }
    }
    load();
  }
  async function loadManifest() {
    loadAgain.hidden=true; status.textContent='正在核对封存初稿…';
    try {
      const manifest=await read();
      if (manifest===null || !card.isConnected) return;
      if (!Array.isArray(manifest.files) || !manifest.files.length) throw Error('这版初稿没有可读取的封存文件。');
      for (const file of manifest.files) {
        const option=el('option',file.path); option.value=file.path; select.append(option);
      }
      filePicker.hidden=manifest.files.length===1;
      if (filePicker.hidden) body.before(el('p',manifest.files[0].path,'goal-muted'));
      status.textContent='';
      const preferred=manifest.files.find(file=>file.kind==='html') ||
        manifest.files.find(file=>file.kind==='markdown') || manifest.files[0];
      select.value=preferred.path; select.onchange=()=>showFile(select.value);
      if (disclosure.open) showFile(preferred.path);
      disclosure.ontoggle=()=>{if (disclosure.open) showFile(select.value);};
      onManifest(manifest,path=>{
        select.value=path;
        if (select.value!==path) return;
        disclosure.open=true;
        showFile(path);
        title.focus();
      });
    } catch (error) {
      status.textContent=error.message;
      loadAgain.hidden=false;
    }
  }
  loadManifest();
}
function showAdvisor(parent, advisor, onOpenFile) {
  if (!advisor) return;
  copy(parent,'顾问看稿意见',advisor.summary);
  if (!advisor.observations?.length) return;
  parent.append(el('h4','顾问指出的原文与问题'));
  const items=el('ul',null,'goal-draft-observations');
  for (const observation of advisor.observations) {
    const item=el('li');
    item.append(button('在初稿中看 '+observation.path,()=>onOpenFile(observation.path),'quiet'));
    if (observation.excerpt) item.append(el('blockquote',observation.excerpt,'goal-copy-block'));
    item.append(el('p',observation.issue));
    items.append(item);
  }
  parent.append(items);
}
function showDirection(parent, direction) {
  const scope=el('div',null,'goal-direction-scope');
  if (direction.mode==='keep') {
    copy(scope,'保留','完整沿用这版初稿，所有文件原样保留。');
  } else {
    const changes=direction.changes || [], removed=direction.removed_paths || [];
    if (changes.length || removed.length) {
      const list=el('ul');
      for (const change of changes) {
        const item=el('li');
        item.append(el('strong',change.path));
        if (change.before) item.append(el('p','原文 · '+change.before,'goal-copy-block'));
        const result=change.after ? (change.before?'改为 · ':'新增 · ')+change.after : '删除这段原文';
        item.append(el('p',result,'goal-copy-block'));
        list.append(item);
      }
      for (const path of removed) list.append(el('li','移除文件 · '+path));
      scope.append(el('h4','会修改'),list);
    }
    textList(scope,'会保留',direction.retained?.map(file=>
      file.path+(file.excerpt ? ' · 「'+file.excerpt+'」' : ' · 完整文件原样保留')));
    if (!direction.retained?.length) copy(scope,'会保留','这个方向没有单列需要原样保留的原文或文件。');
  }
  parent.append(scope);
}

export function mountGoalTeam(root, {request, renewSession, initial, regularOpen, regularVisibility, viewChanged, callHistory,
  formalModelInputConsent=false} = {}) {
  const call = (command, data) => request('/api/outer/'+command,data);
  const supportsExperiments = !formalModelInputConsent;
  const requiresModelConsent = row => formalModelInputConsent || Boolean(row.ticket_team);
  const canSendText = row => !requiresModelConsent(row) || row.allow_model_input===true;
  const project = row => projectGoalRow(row,{allowFreeText:canSendText(row)});
  const getDraft = draftStore();
  const queueHead = el('div',null,'goal-toolbar');
  const filterBar = el('div',null,'goal-actions');
  const search = el('input'); search.type = 'search'; search.placeholder = '搜索标题或工单号'; search.setAttribute('aria-label','搜索标题或工单号');
  const connection = el('p','正在读取工单','goal-connection'); connection.setAttribute('role','status');
  const queueBox = el('div',null,'goal-queues');
  const main = el('article',null,'goal-ticket');
  const notice = el('p','','goal-feedback'); notice.setAttribute('role','status');
  const sidebar = el('div',null,'goal-sidebar');
  const confirmBox = el('dialog',null,'goal-confirm');
  confirmBox.setAttribute('aria-labelledby','goal-confirm-title');
  let data = initial, regular = [], regularFresh = true, focused = '', eventId = '', activeRegular = '';
  let filter = '', panel = 'progress', section = '', selectedSnapshot, connected = Boolean(initial), checkedAt = '', commandInFlight = false;
  let loadVersion = 0, listKey = '', detailKey = '', focusNext = false, returnFocus = '', confirmation = null;
  let openDirection = null, sessionExpired = false;
  const rejectedDrafts = new Map();
  const tokens = new Map();
  const known = new Set((initial?.drafts || []).map(row=>row.id));
  const cache = new Map();
  const confirmTitle = el('h3'); confirmTitle.id = 'goal-confirm-title';
  const confirmText = el('p'); confirmText.id = 'goal-confirm-text';
  confirmBox.setAttribute('aria-describedby',confirmText.id);
  const confirmActions = el('div',null,'goal-actions');
  const cancelButton = button('取消',()=>confirmBox.close());
  const confirmButton = button('确认',()=>{
    const action = confirmation;
    confirmBox.close();
    action?.();
  },'primary');
  confirmActions.append(cancelButton,confirmButton); confirmBox.append(confirmTitle,confirmText,confirmActions);
  confirmBox.addEventListener('close',()=>{if (!confirmBox.open) confirmation = null;});
  function approveAction(heading, text, label, fn, cancelLabel='取消') {
    confirmTitle.textContent = heading; confirmText.textContent = text; confirmButton.textContent = label;
    cancelButton.textContent=cancelLabel;
    ++loadVersion;
    confirmation = fn; confirmBox.showModal(); cancelButton.focus();
  }
  function askModelInputConsent(purpose, label, submit, consequence='') {
    approveAction('允许发送本次内容给 Codex 模型？',
      [purpose,consequence].filter(Boolean).join('\n'),label,()=>submit({allow_model_input:true}));
  }
  function route() {
    const query = new URLSearchParams(location.search);
    const hash = location.hash.slice(1);
    return {
      id: query.get('goal') || '',
      regular: query.get('task') || '',
      event: query.get('outer_event') || '',
      filter: query.get('station') || '',
      section: query.get('goal_panel') || (Object.hasOwn(anchors,hash) ? hash : ''),
    };
  }
  function writeRoute({id='',event='',anchor='',regularId='',replace=false}={}) {
    const url = new URL(location.href);
    for (const key of ['goal','outer_event','goal_panel','task','new','station','panel','mode','round','node','source','revision','native','ci','cite'])
      url.searchParams.delete(key);
    if (id) url.searchParams.set('goal',id);
    if (event) url.searchParams.set('outer_event',event);
    if (anchor) url.searchParams.set('goal_panel',anchor);
    if (regularId) url.searchParams.set('task',regularId);
    url.hash = 'tasks';
    history[replace?'replaceState':'pushState'](history.state,'',url);
  }
  function syncFilter() {
    for (const btn of filterBar.children)
      btn.setAttribute('aria-pressed',String(btn.dataset.filter===filter));
  }
  function showFilters() {
    const options = [['','工作台'],['all','全部工单'],['decision','待我处理'],['dock','已暂停与停止'],['unknown','待核对']];
    for (const [key,label] of options) {
      const btn = button(label,()=>{
        filter=key; syncFilter(); drawList();
      });
      btn.dataset.filter=key; filterBar.append(btn);
    }
  }
  function syncRoute() {
    const from = route();
    ++loadVersion;
    eventId = from.event;
    focused = from.id || (known.has(from.regular) ? from.regular : '');
    activeRegular = focused ? '' : from.regular;
    section = from.section || 'progress';
    panel = anchors[section] || 'progress';
    filter = Object.hasOwn(queues,from.filter) ? from.filter : '';
    focusNext=Boolean(focused && from.section);
    syncFilter();
    regularVisibility?.(Boolean(activeRegular));
    drawList(); drawDetail(cache.get(focused));
    refresh();
  }
  function open(id, anchor, isRegular=false) {
    returnFocus = (isRegular ? 'goal-regular-' : 'goal-open-')+id;
    ++loadVersion;
    focused = isRegular ? '' : id; activeRegular = isRegular ? id : ''; eventId = '';
    section = anchor || 'progress'; panel = anchors[section] || 'progress';
    focusNext = !isRegular;
    writeRoute(isRegular ? {regularId:id} : {id,anchor:section});
    regularVisibility?.(isRegular); viewChanged?.();
    if (isRegular) regularOpen?.(id,anchor);
    drawList(); drawDetail(cache.get(focused)); refresh();
  }
  function back() {
    const previous = returnFocus || (focused?'goal-open-'+focused:'goal-regular-'+activeRegular);
    ++loadVersion;
    focused = ''; eventId = ''; activeRegular = ''; selectedSnapshot = undefined; focusNext = false;
    writeRoute(); regularVisibility?.(false);
    drawList(); drawDetail();
    requestAnimationFrame(()=>document.getElementById(previous)?.focus());
  }
  function drawList() {
    if (data) for (const row of data.drafts || []) known.add(row.id);
    const goalRows = (data?.drafts || []).map(project);
    const rows = [...goalRows,...regular.filter(row=>!known.has(row.id))];
    const match = search.value.trim().toLowerCase();
    const shown = rows.filter(row=>(!match || (row.title+' '+row.id).toLowerCase().includes(match)) &&
      (!filter || filter==='all' || row.station===filter));
    const counts = new Map();
    for (const row of shown) counts.set(row.title,(counts.get(row.title)||0)+1);
    const layout = filter==='all' ? Object.keys(queues) : filter ? [filter] : ['decision','flight','delivery'];
    const unconfirmed = !connected || !regularFresh;
    const key = JSON.stringify([focused,activeRegular,shown,layout,connected,regularFresh,unconfirmed && checkedAt,Boolean(data)]);
    if (key===listKey) return;
    listKey = key;
    const saved = position(queueBox);
    queueBox.replaceChildren();
    connection.textContent = !data ? '正在读取工单' : unconfirmed
      ? '连接中断 · 上次更新 '+(checkedAt || '待核对') : '';
    for (const station of layout) {
      const [label,empty] = queues[station];
      const queue = el('section',null,'goal-queue goal-'+station); queue.setAttribute('aria-labelledby','goal-queue-'+station);
      const head = el('div',null,'goal-queue-title');
      const heading = el('h2',label); heading.id = 'goal-queue-'+station;
      const group = sortQueue(shown.filter(row=>row.station===station),station);
      const count = el('span',unconfirmed ? '—' : String(group.length),'goal-count');
      head.append(heading,count); queue.append(head);
      if (!group.length) queue.append(el('p',unconfirmed?'暂未取得队列状态。':empty,'goal-empty'));
      for (const card of group) {
        const tile = el('article',null,'goal-card');
        const selected = (card.regular ? activeRegular : focused) === card.id;
        if (selected) tile.classList.add('is-selected');
        tile.append(el('span',card.label,'goal-chip'),el('h3',card.title));
        if (counts.get(card.title)>1) tile.append(el('small',card.id.slice(-8),'goal-muted'));
        if (station==='decision' && card.reason && card.next && card.next!==card.reason) {
          const explanation=el('dl',null,'goal-why');
          explanation.append(el('dt','为什么等你'),el('dd',card.reason),el('dt','你要做什么'),el('dd',card.next));
          tile.append(explanation);
        } else {
          if (card.reason) tile.append(el('p',card.reason,'goal-muted'));
          if (card.next && card.next!==card.reason) tile.append(el('p',card.next));
        }
        if (card.at && station==='delivery') tile.append(el('small',time(card.at),'goal-muted'));
        const go = button(card.action,()=>open(card.id,card.anchor,card.regular),station==='decision'?'primary':'');
        go.id = (card.regular?'goal-regular-':'goal-open-')+card.id;
        go.setAttribute('aria-label',card.action+' · '+card.title);
        if (selected) go.setAttribute('aria-current','true');
        tile.append(go); queue.append(tile);
      }
      queueBox.append(queue);
    }
    restorePosition(queueBox,saved);
  }
  function input(label, draft, key, id, options={}) {
    const group = el('label',label);
    const field = el('textarea');
    field.id = id; field.rows = 3; field.maxLength = options.maxLength || 6000;
    field.value = draft.data[key] || ''; field.required = Boolean(options.required);
    field.oninput = () => draft.save({[key]:field.value});
    group.append(field);
    return {group,field};
  }
  function binding(row) {
    return {ticket_id:row.id,revision:row.revision,goal_revision:row.goal_revision,acceptance_revision:row.acceptance_revision};
  }
  async function perform(row, command, fields, feedback, success, draft, nextAnchor) {
    if (commandInFlight || !connected || row.id!==focused || selectedSnapshot!==row) return;
    const useTicketRevision = ['extend_team_budget','lock_blind_ballot','authorize_model_input'].includes(command) ||
      (requiresModelConsent(row) && command==='submit');
    const version = useTicketRevision ? {ticket_id:row.id,revision:row.revision} : binding(row);
    const payload = {...version,...fields};
    const fingerprint = JSON.stringify([command,payload]);
    let requestId = tokens.get(fingerprint) || draft?.data.requests?.[fingerprint];
    if (!requestId) requestId = crypto.randomUUID();
    tokens.set(fingerprint,requestId);
    draft?.save({requests:{...draft.data.requests,[fingerprint]:requestId}});
    commandInFlight = true; ++loadVersion;
    root.querySelectorAll('[data-goal-write]').forEach(node=>{node.disabled=true;});
    feedback.textContent = '正在提交…';
    try {
      const result = await call(command,{...payload,request_id:requestId});
      tokens.delete(fingerprint); draft?.clear();
      if (row.id===focused && result.event_id && command==='submit') {
        eventId = result.event_id;
        writeRoute({id:row.id,event:eventId,anchor:section,replace:true});
      }
      if (row.id===focused && nextAnchor) {
        section=nextAnchor; panel=anchors[section]; focusNext=true;
        writeRoute({id:row.id,anchor:section,replace:true});
      }
      notice.textContent = success;
      feedback.textContent = success;
    } catch (error) {
      notice.textContent = error.message+'。已保留填写内容，正在核对原单状态。';
      feedback.textContent = notice.textContent;
    } finally {
      commandInFlight = false;
      root.querySelectorAll('[data-goal-write]').forEach(node=>{node.disabled=!connected || node.dataset.requiresSelection==='true';});
      await refresh();
    }
  }
  function writeButton(label, id, fn, primary=false) {
    const node=button(label,fn,primary?'primary':'');
    node.id=id; node.dataset.goalWrite=''; node.disabled=!connected || commandInFlight;
    return node;
  }
  function actionStatus(parent) {
    const status = el('p','','goal-feedback'); status.setAttribute('role','status');
    parent.append(status); return status;
  }
  function scope(row, open=false) {
    const box=details('目标与授权','scope',open); box.id='goal-section-scope';
    const goal=row.goal_contract || {}, accept=row.acceptance_contract || {}, terms=row.approval_terms || {};
    copy(box,'你的原话',row.original_request?.goal || row.original_request?.text);
    copy(box,'目标',goal.goal_statement || row.summary);
    copy(box,'登记的完成标准',row.original_request?.acceptance || row.workbench_acceptance || row.workbench_request?.acceptance);
    for (const [field,label] of Object.entries({deliverables:'交付物',required_capabilities:'必需能力',nonfunctional_requirements:'质量要求',constraints:'限制',assumptions:'假设',out_of_scope:'范围外'}))
      textList(box,label,goal[field]);
    const criteria=el('div'); criteria.id='goal-section-criteria'; box.append(criteria);
    textList(criteria,'机器验收',accept.criteria?.filter(item=>item.verifier_id!=='human-review').map(item=>[item.description,[item.verifier_id,item.verifier_version].filter(Boolean).join(' '),'预期 '+renderValue(item.expected_result)].filter(Boolean).join(' · ')));
    textList(criteria,'本人验收',accept.human_review_requirements);
    textList(criteria,'验收缺口',accept.verification_gaps);
    const budget=el('div'); budget.id='goal-section-budget'; box.append(budget);
    const mandate=open ? terms : (row.mandate || terms);
    copy(budget,'允许范围',Array.isArray(mandate.scope)?mandate.scope.map(renderValue).join('\n'):mandate.scope);
    textList(budget,'禁止操作',mandate.forbidden_operations);
    if (Number.isFinite(mandate.budget_seconds)) copy(budget,'执行时间上限',mandate.budget_seconds+' 秒');
    if (mandate.execution_model) copy(budget,'执行模型',mandate.execution_model);
    const review=Object.hasOwn(teamReviewModes,mandate.team_review_mode) && teamReviewModes[mandate.team_review_mode];
    if (review) copy(budget,'团队审稿',[review.label,review.scope].join('\n'));
    const config=mandate.execution_profile;
    if(config?.roles){
      const roleNames={lead:'负责人',advisor:'顾问',design_advisor:'复杂设计顾问',implementer:'执行',reviewer:'复核'};
      textList(budget,'角色与模型',Object.entries(config.roles).map(([role,model])=>(roleNames[role]||role)+' · '+model));
      if(config.max_roles_per_assignment)copy(budget,'每次派工角色轮次上限',String(config.max_roles_per_assignment));
      if(config.max_roles_per_ticket)copy(budget,'整单角色轮次上限',String(mandate.team_role_limit ?? config.max_roles_per_ticket));
    }
    return box;
  }
  function budgetDecision(row, parent) {
    const mandate=row.mandate, execution=row.execution;
    const currentMinutes=mandate.budget_seconds/60;
    const currentRoles=mandate.team_role_limit ?? mandate.execution_profile.max_roles_per_ticket;
    const draft=getDraft(row.id+'/budget/'+row.revision+'/'+row.mandate_revision);
    const box=details('查看预算并决定','extend-budget',true);
    const usage=el('p','当前授权 v'+row.mandate_revision+' · '+currentMinutes+' 分钟 · '+currentRoles+' 个角色轮次');
    const elapsed=execution.elapsed_seconds;
    const roleCount=execution.team_roles_used ?? (blindRequired(row) ? null : execution.team_activity?.length);
    box.append(usage);
    const used=[Number.isFinite(elapsed)?'已用 '+(elapsed/60).toFixed(1)+' 分钟':'',
      Number.isInteger(roleCount)?'已登记 '+roleCount+' 个角色轮次':''].filter(Boolean);
    if (used.length) box.append(el('p',used.join(' · '),'goal-muted'));
    function selectAmount(title,key,choices,unit) {
      const group=el('label',title), selector=el('select');
      selector.id='goal-budget-'+key;
      for (const value of choices) {
        const option=el('option',value ? '追加 '+value+' '+unit : '保持当前额度');
        option.value=String(value); selector.append(option);
      }
      selector.value=String(draft.data[key] || 0);
      group.append(selector); box.append(group); return selector;
    }
    const minutes=selectAmount('执行时间','minutes',[0,15,30,60],'分钟');
    const roles=selectAmount('角色轮次','roles',[0,5,10],'轮');
    const total=el('p'); total.setAttribute('aria-live','polite'); box.append(total);
    const feedback=actionStatus(box);
    const submit=writeButton('查看并确认追加','goal-extend-budget',()=>{
      const additional_minutes=Number(minutes.value),additional_roles=Number(roles.value);
      if (!additional_minutes && !additional_roles) return;
      approveAction('确认追加本单预算',total.textContent+'。确认后，系统将按新额度从当前进度续办。','确认追加',
        ()=>perform(row,'extend_team_budget',{mandate_revision:row.mandate_revision,additional_minutes,additional_roles},
          feedback,'新增额度已批准，续办已登记。',draft),'暂不追加');
    },true);
    const sync=()=>{
      const minutesValue=Number(minutes.value), roleValue=Number(roles.value);
      draft.save({minutes:minutesValue,roles:roleValue});
      total.textContent='追加后共 '+(currentMinutes+minutesValue)+' 分钟、'+(currentRoles+roleValue)+' 个角色轮次';
      submit.dataset.requiresSelection=String(!(minutesValue || roleValue));
      submit.disabled=!connected || commandInFlight || submit.dataset.requiresSelection==='true';
    };
    minutes.onchange=roles.onchange=sync;
    box.append(submit); sync(); parent.append(box);
  }
  function invalidateDirection(direction, q, status) {
    if (!direction || direction!==openDirection || direction.row.id!==focused) return;
    rejectedDrafts.set(draftPreviewKey(direction.row,q),status);
    ++loadVersion;
    if (status===403) {
      sessionExpired=true;
      connected=false;
      drawList();
    }
    drawDetail(direction.row);
    refresh();
  }
  function preferenceForm(row, q, parent, currentDirection=null) {
    const previewKey=stagedPreferenceKey(row,q);
    const draft=getDraft(previewKey || row.id+'/preference/'+q.id+'/'+q.revision);
    const fieldset=el('fieldset');
    const allowFreeText=canSendText(row);
    const direction=directionQuestion(q);
    const digest=q.draft_sha256 || q.candidate_digest;
    const scopes=new Map();
    if (previewKey) {
      const advice=el('div',null,'goal-draft-advice');
      mountDraftPreview(parent,row,q,(manifest,openFile)=>{
        showAdvisor(advice,manifest.advisor,openFile);
        for (const item of manifest.directions || []) {
          const option=scopes.get(item.option_id);
          if (option) showDirection(option,item);
        }
      },status=>invalidateDirection(currentDirection,q,status));
      parent.append(advice);
    } else if (direction && digest) draftVersion(parent,stepLabel(q.step)+'初稿版本',digest);
    fieldset.append(el('legend',direction ? '选择'+stepLabel(q.step)+'的修改方向' : '选择一组样例'));
    const options=q.options || [];
    for (const option of options) {
      const label=el('label',null,previewKey?'goal-direction-label':'goal-option');
      const radio=el('input'); radio.type='radio'; radio.name='goal-option';
      radio.value=option.id; radio.checked=draft.data.option_id===option.id;
      radio.id='goal-option-'+option.id;
      radio.onchange=()=>{draft.save({option_id:radio.value});feedback.textContent='';};
      const copy=el('span'); copy.append(el('strong',option.label));
      if (option.text) copy.append(el('span',option.text,'goal-sample-text'));
      label.append(radio,copy);
      if (previewKey) {
        const card=el('div',null,'goal-option goal-direction');
        card.append(label); fieldset.append(card); scopes.set(option.id,card);
      } else fieldset.append(label);
    }
    const {group,field}=input('希望保留或修改什么？',draft,'text','goal-preference-text',{maxLength:2000});
    parent.append(fieldset);
    if (allowFreeText) parent.append(group);
    const feedback=actionStatus(parent);
    field.addEventListener('input',()=>{feedback.textContent='';});
    parent.append(writeButton(direction ? '提交修改方向' : '提交选择','goal-submit-preference',()=>{
      const option=options.find(item=>item.id===draft.data.option_id);
      const option_id=option?.id;
      const text=!allowFreeText || (requiresModelConsent(row) && !field.value.trim()) ? '' : field.value;
      const conflict=preferenceDirectionConflict(option,text);
      if (conflict) { feedback.textContent=conflict; field.focus(); return; }
      if (!option_id && (q.requires_option || !text.trim())) {
        if (q.requires_option || !allowFreeText) {
          feedback.textContent=(direction ? '请先选择一个修改方向。' : '请先选择一组样例。')+
            (allowFreeText ? '你也可以补充意见。' : '');
          fieldset.querySelector('input')?.focus();
        } else {
          feedback.textContent=direction ? '请选择一个方向，或写下你的意见。' : '请选择一组样例，或写下你的意见。';
          field.focus();
        }
        return;
      }
      const submit=(consent={})=>perform(currentDirection?.row || row,'answer_preference',{
        question_id:q.id,question_revision:q.revision,candidate_digest:q.candidate_digest,
        answer:{option_id,text},...consent,
      },feedback,'选择已记录，正在刷新原单。',draft);
      if (requiresModelConsent(row) && text) {
        askModelInputConsent('你填写的意见将发送给 Codex 模型，用于继续处理这张工单。','同意并提交',submit);
      } else submit();
    },true));
  }
  function draftVersion(parent,label,digest) {
    parent.append(el('h4',label),el('code',digest,'goal-muted'));
  }
  function authorizeIntake(row,parent) {
    const original=row.original_request?.text || row.original_request?.goal;
    if (!original) {
      parent.append(el('p','暂时无法读取原话。请重新打开原单后再核对。','goal-muted'));
      return;
    }
    copy(parent,'原话',original);
    const scope='团队会使用本单原话整理目标和验收条件。执行前仍需在原单批准。';
    parent.append(el('p',scope,'goal-muted'));
    const feedback=actionStatus(parent);
    parent.append(writeButton('允许把本单原话交给团队','goal-authorize-model-input',()=>{
      approveAction('允许把本单原话交给团队？',scope,'确认交给团队',
        ()=>perform(row,'authorize_model_input',{allow_model_input:true},feedback,'本次授权已登记。'));
    },true));
  }
  function clarificationForm(row, parent) {
    const q=row.question, draft=getDraft(row.id+'/clarification/'+q.question_id+'/'+row.revision);
    const feedback=actionStatus(parent);
    const submit=text=>{
      const send=(consent={})=>perform(row,'submit',{text,question_id:q.question_id,...consent},feedback,'澄清回复已登记。',draft);
      if (requiresModelConsent(row)) {
        askModelInputConsent('这条澄清答复将发送给 Codex 模型，用于补齐原单条件。','同意并提交',send);
      } else send();
    };
    const options=el('div',null,'goal-actions');
    for (const [index,choice] of (q.options || []).entries())
      options.append(writeButton(renderValue(choice),'goal-clarification-option-'+index,()=>submit(renderValue(choice)),true));
    if (options.childElementCount) parent.append(options);
    const {group,field}=input(options.childElementCount?'其他说明':'补充关键条件',draft,'text','goal-clarification-text');
    parent.append(group,writeButton('提交澄清','goal-submit-clarification',()=>{
      if (!field.value.trim()) { feedback.textContent='请填写澄清内容。'; field.focus(); return; }
      submit(field.value);
    },!options.childElementCount));
  }
  function approval(row, parent) {
    const draft=getDraft(row.id+'/approval/'+row.revision);
    const contract=scope(row,true);
    parent.append(contract);
    const signoff=el('input'); signoff.type='checkbox'; signoff.id='goal-signoff';
    const required=Boolean(row.acceptance_contract?.human_review_requirements?.length);
    signoff.checked=required || Boolean(draft.data.signoff); signoff.disabled=required;
    signoff.onchange=()=>draft.save({signoff:signoff.checked});
    const label=el('label',null,'goal-optional-signoff');
    label.append(signoff,el('span',required?'本版包含本人验收条款，机器验收通过后需要签收':'机器验收通过后由我签收交付'));
    parent.append(label);
    const feedback=actionStatus(parent);
    parent.append(writeButton('批准本版目标与验收','goal-approve',()=>{
      const require_signoff=signoff.checked;
      approveAction('批准目标与验收',[
        '目标 v'+row.goal_revision+' · 验收 v'+row.acceptance_revision,
        require_signoff?'机器验收通过后会请你签收。':'机器验收与归档通过后会交付。',
        '确认后，系统按这一版的权限和预算登记执行。',
      ].join('\n'),'确认批准',()=>perform(row,'approve',{require_signoff},feedback,'本版授权已保存。',draft));
    },true));
  }
  function displayProgress(row, parent) {
    const execution=row.execution, flow=execution?.goal_execution || {};
    const steps=execution?.plan?.steps || [];
    const activeAssignment=flow.assignments?.findLast(item=>item.step===execution?.step && item.state==='dispatched');
    parent.append(el('h3','执行进度'));
    const routeLabels={direct:'直接实施',compare:'比较双方案'};
    const route=routeLabels[execution?.plan?.execution_route];
    if (route) copy(parent,'已定路线 · '+route,execution.plan.route_reason);
    if (row.ticket_team) {
      const names={lead:'负责人',implementer:'执行者',advisor:'只读顾问',design_advisor:'只读顾问',reviewer:'独立验收者'};
      const used=[...new Set((execution?.team_activity || []).filter(call=>call.status==='completed' && names[call.role] && call.model)
        .map(call=>names[call.role]+' '+call.model))];
      if (used.length) parent.append(el('p','已完成的角色调用 · '+used.join(' · '),'goal-muted'));
    }
    if (!steps.length) parent.append(el('p','尚无已登记的执行步骤。','goal-muted'));
    else {
      const list=el('ol',null,'goal-steps');
      for (const [index,step] of steps.entries()) {
        const item=el('li');
        let label=stepStates[step.state] || '待核对';
        if(index===execution?.step && step.state!=='completed'){
          if(execution.status==='awaiting_preference')label=directionQuestion(preferenceOf(row))?'待定方向':'待选样例';
          else if(execution.status==='awaiting_artifact_review')label='待签收';
          else if(execution.status==='budget_exhausted')label='待补预算';
          else if(execution.status==='blocked')label='执行受阻';
          else if(row.journey?.inner_repair_status || row.journey?.phase==='coordinate')label=row.journey.label;
          else if(activeAssignment && ['executing','reviewing'].includes(execution.status))label='正在'+(phases[activeAssignment.phase] || '推进');
        }
        item.append(el('span',label,'goal-chip'),el('strong',step.title||step.objective));
        const stepRoute=routeLabels[step.execution_route];
        if (stepRoute) item.append(el('small',stepRoute,'goal-muted'));
        list.append(item);
      }
      parent.append(list);
    }
    displayTeamHandoffs(row,parent);
    if (row.ticket_team) {
      const box=details('Router 回执','router-dispatch');
      box.append(el('p','Router 已接单，本单有独立团队。'));
      const receipts=(flow.decisions || []).filter(item=>['run_bound','delivered'].includes(item.kind));
      for (const receipt of [...receipts].reverse()) {
        const label=receipt.kind==='delivered'?'本单已验收并登记交付':'中台已接单 · Run '+receipt.run;
        box.append(el('p',[time(receipt.at),label].filter(Boolean).join(' · ')));
      }
      parent.append(box);
    }
    displayInnerRepairs(row,parent);
    const assignments=flow.assignments || [];
    if (assignments.length) {
      const box=details('派工与执行凭据','dispatches');
      textList(box,null,assignments.map(a=>[phases[a.phase]||a.phase,assignmentStates[a.state]||a.state,
        a.run==null?'': 'Run '+a.run,a.receipt_source==='recovery'?'已核对封存回执并恢复':'',
        '派工 '+a.id].filter(Boolean).join(' · ')));
      parent.append(box);
    }
    if (execution?.error) copy(parent,'执行反馈',execution.error);
    if (!row.journey?.approve || !row.approval_terms) parent.append(scope(row));
  }
  function displayTeamHandoffs(row,parent) {
    const records=projectTeamHandoffs(row);
    if (!records.length) return;
    const section=el('section',null,'goal-inner-repairs');
    const heading=el('h3','双方案与初稿审阅');
    heading.id='goal-team-handoffs-title';
    section.setAttribute('aria-labelledby',heading.id);
    section.append(heading);
    for (const [index,record] of records.entries()) {
      const label=[record.step,record.run==null?'':'Run '+record.run].filter(Boolean).join(' · ');
      const box=details(label || '审稿记录','team-handoff-'+record.id,index===0);
      const plans=(row.execution?.team_activity || []).filter(call=>call.assignment===record.id &&
        ['lead_plan','advisor_plan'].includes(call.purpose) && call.summary);
      if (plans.length) {
        const versions=details('查看双方原方案','team-plans-'+record.id);
        const columns=el('div',null,'goal-plans');
        for (const plan of plans) {
          const part=el('section');
          copy(part,plan.purpose==='lead_plan'?'负责人方案':'只读顾问的独立方案',plan.summary);
          columns.append(part);
        }
        versions.append(columns); box.append(versions);
      }
      copy(box,'负责人比较方案与取舍',record.comparison);
      if (record.draft) draftVersion(box,'审稿所见的初稿版本',record.draft);
      copy(box,'只读顾问看稿意见',record.review);
      textList(box,'顾问核对的原文与问题',record.observations?.map(item=>
        [item.path,item.excerpt && '「'+item.excerpt+'」',item.issue].filter(Boolean).join(' · ')));
      copy(box,'负责人给执行者的修改清单',record.instruction);
      textList(box,'需要保留',record.retain);
      textList(box,'需要复查',record.verify);
      if (record.checked) {
        draftVersion(box,'实际交回的候选版本',record.checked.artifact_sha256);
        textList(box,'已核对的改动文件',record.checked.changed_files);
        textList(box,'已核对保留',record.checked.retained.map(item=>
          item.excerpt ? item.path+' · 「'+item.excerpt+'」' : item.path+' · 原样保留'));
      }
      section.append(box);
    }
    parent.append(section);
  }
  function displayInnerRepairs(row,parent) {
    if (!canReadInnerRepairs(row)) return;
    const execution=row.execution;
    const records=(execution?.goal_execution?.assignments || []).flatMap(assignment=>
      (assignment.inner_repairs || []).map((repair,index)=>({assignment,repair,index}))).reverse();
    if (!records.length) return;
    const section=el('section',null,'goal-inner-repairs');
    const heading=el('h3','返修与复验');
    heading.id='goal-inner-repairs-title';
    section.setAttribute('aria-labelledby',heading.id);
    section.append(heading);
    for (const [order,{assignment,repair,index}] of records.entries()) {
      const step=assignment.version===execution.version ? execution.plan?.steps?.[assignment.step] : null;
      const id='inner-repair-'+assignment.id+'-'+index;
      const status=innerRepairStates[repair.status] || '待核对';
      const box=details([step?.title || step?.objective,status].filter(Boolean).join(' · '),id,order===0);
      box.children[0].id='goal-'+id;
      copy(box,repair.found_by==='reviewer'?'独立复核发现的问题':'负责人发现的问题',repair.issue);
      textList(box,'本轮保留',repair.retain);
      copy(box,'执行者改了什么',repair.changes);
      textList(box,'改动文件',repair.changed_files);
      textList(box,'新版怎么验',repair.checks);
      textList(box,repair.verification?'本机实际复验':'负责人实际复查',repair.review_checks);
      const reviewLabel=repair.status==='blocked' ? '阻塞原因' :
        ['waiting','waiting_review'].includes(repair.status) ? '续办安排' : '负责人复验结论';
      copy(box,reviewLabel,repair.review);
      if (repair.before_sha256 || repair.after_sha256) {
        const versions=details('候选版本摘要 SHA-256',id+'-versions');
        versions.append(el('p','这里记录整个候选文件清单的摘要。单个文件的摘要在下载处显示。','goal-muted'));
        versions.children[0].id='goal-'+id+'-versions';
        for (const [label,digest] of [['返修前候选',repair.before_sha256],['返修后候选',repair.after_sha256]]) {
          if (digest) versions.append(el('h4',label),el('code',digest));
        }
        box.append(versions);
      }
      section.append(box);
    }
    parent.append(section);
  }
  function machineChecks(parent, checks, id) {
    const actual=(checks || []).filter(c=>c.verifier_id!=='human-review' &&
      c.verification_type!=='HUMAN_REVIEW' && !['HUMAN_REQUIRED','HUMAN_PENDING'].includes(c.status));
    if (!actual.length) { parent.append(el('p','尚无逐项机器验收结果。','goal-muted')); return; }
    const passed=actual.filter(c=>c.status==='PASS' || c.passed===true).length;
    const box=details('机器验收 '+passed+'/'+actual.length,id);
    if(id==='machine')box.id='goal-section-checks';
    for (const check of actual) {
      const state=check.status==='PASS' || check.passed===true ? '通过' :
        check.status==='FAIL' || check.passed===false ? '未通过' : '待核对';
      const result=check.actual_result ?? check.actual ?? check.detail;
      const expected=check.expected_result ?? check.expected;
      const verifier=[check.verifier_id,check.verifier_version].filter(Boolean).join(' · ');
      copy(box,state+' · '+(check.description || check.id || check.criterion_id),[
        expected!=null?'预期 '+renderValue(expected):'',result!=null?'实际 '+renderValue(result):'',
        verifier,check.run==null?'':'Run '+check.run,
      ].filter(Boolean).join('\n') || '已登记'+state);
    }
    parent.append(box);
  }
  function displayFiles(row, candidate, parent, label, downloadable=true) {
    if (candidate.artifact_sha256) {
      const meta=details('归档摘要 SHA-256','summary-'+candidate.assignment);
      meta.append(el('code',candidate.artifact_sha256)); parent.append(meta);
    }
    const files=Object.entries(candidate.files || {});
    for (const [path,sha] of files) {
      const file=el('div',null,'goal-file');
      if (downloadable && candidate.assignment) {
        if(/\.(html?|md|txt|csv|json)$/i.test(path)) previewTextFile(row.id,candidate.assignment,path,file);
        downloadFile(row.id,candidate.assignment,path,file,'下载 '+label+' · '+path);
      }
      else file.append(el('strong',path));
      if (typeof sha==='string') file.append(el('code',sha,'goal-muted'));
      parent.append(file);
    }
  }
  function lockedBlindResult(review, parent) {
    const actor=review.lock?.operator_kind==='acceptance_test'?'测试操作员':'本人';
    parent.append(el('p',actor+'评语已锁定。'));
    if (!review.result) { parent.append(el('p','正在核对逐例结果。')); return; }
    const {counts,cases}=review.result;
    parent.append(el('p','团队胜 '+counts.wins+' · 平 '+counts.ties+' · 负 '+counts.losses+' · 未评 '+counts.unrated));
    const measures=review.runtime_measures;
    if (measures?.baseline && measures.team) {
      const describe=arm=>arm.calls+' 轮 / '+(arm.model_wall_ms/1000).toFixed(1)+' 秒';
      parent.append(el('p','单 Sol '+describe(measures.baseline)+' · 团队 '+describe(measures.team)+' · 人工转述 '+measures.human_relays+' 次','goal-muted'));
    }
    const results=details('逐例结果与原话','blind-results-'+review.experiment_id);
    for (const item of cases) {
      results.append(el('h4',item.id+' · '+(blindOutcomes[item.outcome] || '待核对')),
        el('p',item.reason || '未留理由','goal-copy-block'));
    }
    parent.append(results);
  }
  function displayBlindHistory(row,parent) {
    const current=row.blind_review;
    const history=(row.blind_history || []).filter(item=>item.status==='locked' &&
      (item.experiment_id!==current?.experiment_id || current?.status!=='locked'));
    if (!history.length) return;
    const candidates=[...deliveriesOf(row),...(row.prior_deliveries || [])];
    const section=details('历史盲评','blind-history');
    parent.append(section);
    for (const item of history) {
      const candidate=candidates.find(entry=>entry.assignment===item.candidate);
      const label=candidate ? '交付 v'+candidate.sequence : '试验 '+item.experiment_id;
      const record=details(label+' · '+time(item.locked_at),'blind-history-'+item.experiment_id);
      const body=el('div'); record.append(body); section.append(record);
      let loading=false, done=false;
      record.ontoggle=async()=>{
        if (!record.open || loading || done) return;
        loading=true; body.textContent='正在读取旧版评语…';
        try {
          const response=await call('get',{ticket_id:row.id,experiment_id:item.experiment_id});
          if (!record.isConnected || focused!==row.id) return;
          const review=response.blind_review;
          if (response.id!==row.id || review?.status!=='locked' || review.experiment_id!==item.experiment_id ||
              review.candidate!==item.candidate) throw Error('旧版评语的绑定信息暂无法核对');
          body.replaceChildren();
          lockedBlindResult(review,body); done=true;
        } catch (error) { body.textContent=error.message+'。收起后可重新打开。'; }
        finally { loading=false; }
      };
    }
  }
  function displayBlindReview(row,candidate,parent) {
    const review=row.blind_review;
    const section=el('section',null,'goal-blind'); section.id='goal-section-blind-review';
    section.append(el('h4','逐例盲评'));
    parent.append(section);
    if (!sameBlindCandidate(review,candidate) || review.status==='superseded') {
      section.append(el('p','正在核对本版的匿名材料。','goal-muted')); return;
    }
    section.append(el('p','交付 v'+candidate.sequence+' · 试验 '+review.experiment_id));
    const version=details('查看匿名材料摘要','blind-manifest');
    version.append(el('code',review.manifest_digest)); section.append(version);
    if (review.status==='locked') { lockedBlindResult(review,section); return; }
    const cases=review.projection?.cases;
    if (review.status!=='awaiting_ballot' || candidate.state!=='pending' || !cases?.length) return;
    if (!supportsExperiments) {
      section.append(el('p','这版安排了匿名评阅，请核对本单的验收安排。','goal-muted'));
      return;
    }
    const draft=getDraft(row.id+'/blind/'+review.experiment_id+'/'+review.manifest_digest);
    section.append(el('p','逐例比较甲和乙。选甲、乙或相当时请写理由，也可以留为未评。'));
    const judgments=new Map();
    const form=el('form');
    for (const current of cases) {
      const group=el('fieldset',null,'goal-blind-case');
      group.append(el('legend',current.id),el('p',current.context,'goal-copy-block'));
      copy(group,'说话者原话',current.speaker);
      const sides=el('div',null,'goal-blind-options');
      for (const [label,value] of [['甲',current.option_a],['乙',current.option_b]]) {
        const side=el('div'); copy(side,label,value); sides.append(side);
      }
      group.append(sides);
      const fields=draft.data.cases?.[current.id] || {};
      const select=el('select'), label=el('label','你的判断');
      select.id='goal-blind-choice-'+current.id;
      for (const [key,title] of [['','未评'],['a','甲'],['b','乙'],['tie','相当']]) {
        const option=el('option',title); option.value=key; select.append(option);
      }
      select.value=fields.choice || '';
      label.append(select); group.append(label);
      const reasonLabel=el('label','理由');
      const reason=el('textarea'); reason.id='goal-blind-reason-'+current.id;
      reason.rows=2; reason.maxLength=2000; reason.value=fields.reason || '';
      reasonLabel.append(reason); group.append(reasonLabel);
      const save=()=>{
        reason.required=Boolean(select.value);
        draft.save({cases:{...draft.data.cases,[current.id]:{choice:select.value,reason:reason.value}}});
      };
      select.onchange=reason.oninput=save;
      reason.required=Boolean(select.value);
      judgments.set(current.id,{select,reason});
      form.append(group);
    }
    const feedback=actionStatus(form);
    const submit=writeButton('查看并锁定本版评语','goal-lock-blind-ballot',()=>{
      const records=[...judgments].map(([id,{select,reason}])=>({id,choice:select.value || null,reason:reason.value}));
      const missing=records.find(item=>(item.choice || item.reason) && !item.reason.trim());
      if(missing){
        feedback.textContent='请填写 '+missing.id+' 的理由，或将本例留为未评。';
        judgments.get(missing.id).reason.focus(); return;
      }
      const rated=records.filter(item=>item.choice).length;
      approveAction('锁定交付 v'+candidate.sequence+' 的评语',
        '试验 '+review.experiment_id+' · 匿名材料 '+review.manifest_digest.slice(0,12)+' · 已评 '+rated+' 例 · 未评 '+(records.length-rated)+' 例。\n确认后，本版评价会固定在你刚读的匿名材料上，随后显示评阅结果。',
        '确认锁定',()=>perform(row,'lock_blind_ballot',{experiment_id:review.experiment_id,manifest_digest:review.manifest_digest,
          judgments:records},feedback,'本版评语已锁定。',draft,'blind-review'));
    },true);
    form.onsubmit=event=>{event.preventDefault();};
    form.append(submit); section.append(form);
  }
  function artifactDecision(row, candidate, parent) {
    const name='v'+candidate.sequence, reviewed={candidate:candidate.assignment,artifact_sha256:candidate.artifact_sha256};
    const draft=getDraft(row.id+'/artifact/'+candidate.assignment+'/'+candidate.artifact_sha256);
    const feedback=actionStatus(parent);
    const signoff=writeButton('签收 '+name,'goal-accept-artifact',()=>{
      approveAction('签收交付 '+name,'签收后归档当前候选。\n归档摘要 '+candidate.artifact_sha256,'确认签收',
        ()=>perform(row,'accept_artifact',reviewed,feedback,'已签收交付 '+name+'。',draft,'artifacts'));
    },true);
    if (!blindRequired(row,candidate) || blindLocked(row,candidate)) parent.append(signoff);
    if (!canSendText(row)) return;
    const rejection=details('退回 '+name,'return-'+candidate.assignment,Boolean(draft.data.reason));
    const {group,field}=input('这版需要改哪里？',draft,'reason','goal-return-reason',{required:true,maxLength:1000});
    rejection.append(group,writeButton('退回 '+name+' 继续修改','goal-return-artifact',()=>{
      if (!field.value.trim()) { feedback.textContent='请输入退回理由。'; field.focus(); return; }
      const reason=field.value;
      const submit=(consent={})=>perform(row,'return_artifact',{...reviewed,reason,...consent},
        feedback,'已记录 '+name+' 的退回理由。',draft,'artifacts');
      if (requiresModelConsent(row)) {
        askModelInputConsent('退回 '+name+' 后，退回理由将发送给 Codex 模型，用于继续修改这版交付。','同意并退回',submit);
      } else submit();
    }));
    parent.append(rejection);
  }
  function displayDevelopmentDelivery(parent, delivery) {
    if (!delivery) return;
    const labels = {
      unauthorized:'等待发布授权', awaiting_report:'已授权，等待交付回执',
      accepted:'发布流程已接单', reviewing:'正在独立审查', checking:'正在运行自动检查',
      reviewed:'审查已通过', merging:'正在合并', shipping:'正在发布',
      returned:'发布流程已退回', retrying:'正在重试发布流程',
      merged:'PR 已合并，项目无线上服务', docs_only:'文档已合并，无需发布', live:'已上线，线上验证通过',
      scope_mismatch:'发布记录超出本次授权范围',
    };
    const {status,authorization,receipt,history}=delivery;
    const box=details('开发发布回执','development-delivery');
    box.id='goal-section-development-delivery';
    const summary=box.querySelector('summary');
    const label=status==='live' && !(receipt?.merge && receipt?.ship) ? '线上验证待核对' : labels[status];
    if (label) summary.append(el('small',label,'goal-chip'));
    if (authorization) {
      copy(box,'合并请求',authorization.repo+' #'+authorization.number);
      copy(box,'授权范围',authorization.mode==='release'?'合并与发布':'仅独立审查');
      copy(box,'审查提交',authorization.head);
    }
    if (receipt?.merge) copy(box,'合并提交',receipt.merge);
    if (receipt?.ship) copy(box,'Ship 运行',String(receipt.ship));
    if (receipt?.at) copy(box,'最近回执',time(receipt.at));
    if (history?.length>1) textList(box,'同版发布记录',history.slice(1).flatMap(item=>
      labels[item.status] ? [[time(item.at),labels[item.status]].filter(Boolean).join(' · ')] : []));
    parent.append(box);
  }
  function displayArtifacts(row, parent) {
    const execution=row.execution, flow=execution?.goal_execution || {};
    const reviews=deliveriesOf(row);
    const current=reviews.at(-1);
    const delivery=execution?.goal_delivery;
    const blindMode=blindRequired(row,current);
    const canDownloadHistory=!blindMode || execution?.status==='completed';
    const canDownloadCurrent=current?.state==='pending' && blindLocked(row,current);
    const protectedBallot=current?.state==='pending' && blindMode && !canDownloadCurrent;
    const existing=current?.state==='pending' || current?.state==='accepted' ? current : delivery ||
      (current?.state==='returned' ? current : null);
    parent.append(el('h3','成果与验收'));
    if (existing) {
      const name='v'+(existing.sequence || Math.max(1,reviews.length));
      const state=protectedBallot ?
        (currentBlindStatus(row,existing)==='awaiting_ballot'?'待盲评':'匿名材料待核对') :
        ({pending:'待签收',returned:'已退回',accepted:'已签收'}[existing.state] || '已归档');
      parent.append(el('h4','交付 '+name+' · '+state));
      if (!protectedBallot) displayFiles(row,existing,parent,name,canDownloadHistory || (existing===current && canDownloadCurrent));
      machineChecks(parent,existing.checks || flow.validation?.checks,'machine');
      if ((existing===current && blindRequired(row,existing)) || sameBlindCandidate(row.blind_review,existing))
        displayBlindReview(row,existing,parent);
      textList(parent,'本人验收',existing.human_criteria || row.acceptance_contract?.human_review_requirements);
      if (existing.reason) copy(parent,'退回理由',existing.reason);
      if (row.journey?.artifact_review && current===existing && current.state==='pending')
        artifactDecision(row,current,parent);
    } else {
      parent.append(el('p','尚无经过独立核验的交付文件。','goal-muted'));
      machineChecks(parent,flow.validation?.checks,'machine');
    }
    displayDevelopmentDelivery(parent,row.development_delivery);
    displayInnerRepairs(row,parent);
    const past=[...(row.prior_deliveries || []),...reviews.filter(item=>item!==existing)].reverse();
    if (past.length && !protectedBallot) {
      const history=details('历史交付版本','past-deliveries');
      for (const item of past) {
        const name='v'+item.sequence;
        const version=details('授权 v'+item.mandate_revision+' · 交付 '+name,'archive-'+item.assignment);
        copy(version,'退回理由',item.reason); displayFiles(row,item,version,name,canDownloadHistory);
        machineChecks(version,item.checks,'machine-'+item.assignment); history.append(version);
      }
      parent.append(history);
    }
  }
  function displayRouterFeedback(row,parent) {
    if (!row.ticket_team) return;
    const box=details('补充资料与反馈','router-feedback',section==='feedback');
    box.id='goal-section-feedback';
    const subscribed=row.status!=='STOPPED';
    box.append(el('p',subscribed ? '已订阅本单。补充资料和测试、评审结果会登记到原单。' :
      '此单已停止。','goal-muted'));
    const channels={web:'网页',dsh:'DSH',feishu:'飞书'};
    const bindings=[...new Set((row.subscription?.bindings || []).map(item=>channels[item.source]).filter(channel=>channel && channel!=='网页'))];
    if (bindings.length) box.append(el('p','已关联'+bindings.join('、')+'会话。','goal-muted'));
    const feedbackArticle=item=>{
      const article=el('article',null,'goal-router-feedback');
      const state=item.handed_to_team?'已交给团队':item.allow_model_input===false?'仅本机登记':'已登记';
      const stamp=[time(item.received_at),channels[item.source],state].filter(Boolean).join(' · ');
      if (stamp) article.append(el('small',stamp,'goal-muted'));
      if (item.text) article.append(el('p',item.text,'goal-copy-block'));
      if (item.draft_unaffected) article.append(el('p','当前初稿保持原版，补充已登记供后续使用。','goal-muted'));
      if (item.candidate_unaffected) article.append(el('p',row.execution?.status==='awaiting_artifact_review' ?
        '当前候选已封存。这条补充未纳入本版验收。若作品需要修改，请在「成果与验收」中退回。' :
        '交付版本已封存。这条补充未纳入本版验收。如需继续处理，可另开工单。','goal-muted'));
      return article;
    };
    if (subscribed && canSendText(row)) {
      const draft=getDraft(row.id+'/router-feedback');
      const {group,field}=input('补充资料、测试结果或评审意见',draft,'text','goal-supplement-text',{required:true});
      box.append(group);
      const status=actionStatus(box);
      box.append(writeButton('补充到原单','goal-submit-supplement',()=>{
        const text=field.value.trim();
        if (!text) { status.textContent='请填写要补充的内容。'; field.focus(); return; }
        const submit=(consent={})=>perform(row,'submit',{intent:'supplement',text,...consent},
          status,'补充已登记到原单。',draft);
        if (requiresModelConsent(row))
          askModelInputConsent('这次补充会发送给 Codex，作为本单团队处理时的参考。','同意并补充',submit);
        else submit();
      }));
    }
    const [latest,...older]=row.subscription?.entries || [];
    if (latest) box.append(feedbackArticle(latest));
    if (older.length) {
      const history=details('更早的补充','router-feedback-history');
      history.append(...older.map(feedbackArticle));
      box.append(history);
    }
    parent.append(box);
  }
  function displayDecisions(row,parent) {
    const execution=row.execution, flow=execution?.goal_execution || {};
    const decisions=flow.decisions || [];
    const candidate=deliveriesOf(row).at(-1);
    const protectedBallot=candidate?.state==='pending' && blindRequired(row,candidate) && !blindLocked(row,candidate);
    parent.append(el('h3','决定与记录'));
    displayRouterFeedback(row,parent);
    if (row.question?.answer && String(row.question.answer_status).toLowerCase()==='answered')
      copy(parent,'已记录的澄清',row.question.answer);
    const answers=execution?.preference_answers || decisions.filter(entry=>entry.kind==='preference_answered');
    const question=flow.pending_preference;
    const answered=!protectedBallot && question?.answer && Array.isArray(question.options) && question.options.length;
    const isQuestion=record=>answered && ((question.id && record.question===question.id) ||
      (question.answer.event && (record.answer || record).event===question.answer.event));
    if (answered) {
      const answer=question.answer;
      const chosen=answer.option_id || answer.option?.id;
      const selected=question.options.find(option=>chosen && option.id===chosen);
      const direction=directionQuestion(question);
      parent.append(el('h4',[direction?stepLabel(question.step):'',selected?'已选 · '+selected.label:
        (direction?'已提交修改方向':'已提交样例意见')].filter(Boolean).join(' · ')));
      if (answer.text) parent.append(el('p',answer.text,'goal-copy-block'));
      const record=answers.find(isQuestion);
      const channel={web:'网页',dsh:'DSH',feishu:'飞书'}[record?.source || answer.source] || record?.source || answer.source;
      if (record?.at || channel) parent.append(el('small',[time(record?.at),channel].filter(Boolean).join(' · '),'goal-muted'));
      const previous=details(direction?'回看当时的方向':(question.options.length===3?'回看三组样例':'回看当时的样例'),
        'answered-preference-'+question.id);
      const digest=question.draft_sha256 || question.candidate_digest;
      if (direction && digest) draftVersion(previous,'当时所见的初稿版本',digest);
      if (question.prompt) previous.append(el('p',question.prompt,'goal-copy-block'));
      for (const option of question.options) {
        previous.append(el('h4',option.label+(selected===option?' · 已选':'')));
        if (option.text) previous.append(el('p',option.text,'goal-copy-block'));
      }
      parent.append(previous);
    }
    for (const record of [...answers].reverse()) {
      if (isQuestion(record)) continue;
      const answer=record.answer || record;
      const label=answer.option?.label || answer.option_label || answer.option_id;
      copy(parent,'已记录的选择',[label,answer.text].filter(Boolean).join('\n'));
      const channel={web:'网页',dsh:'DSH',feishu:'飞书'}[record.source] || record.source;
      if (record.at || channel) parent.append(el('small',[time(record.at),channel].filter(Boolean).join(' · '),'goal-muted'));
      if (answer.option?.text) {
        const sample=details('查看当时选择的样例','chosen-'+record.question);
        sample.append(el('p',answer.option.text,'goal-copy-block')); parent.append(sample);
      }
    }
    const history=details('工单记录','timeline',section==='timeline'); history.id='goal-section-timeline';
    for (const item of [...(execution?.events || [])].reverse())
      history.append(el('p',[time(item.at),item.title].filter(Boolean).join(' · ')));
    if (!history.querySelector('p')) history.append(el('p','尚无执行记录。','goal-muted'));
    parent.append(history);
    if (protectedBallot || !canReadInnerRepairs(row)) return;
    if (supportsExperiments) displayBlindHistory(row,parent);
    const diagnostics=details('角色、工具与派工记录','outer-history',section==='outer-history');
    diagnostics.id='goal-section-outer-history';
    for (const item of execution?.team_activity || []) {
      const roleLabel={lead:'负责人',advisor:'只读顾问',design_advisor:'设计顾问',implementer:'执行者',reviewer:'独立复核者'}[item.role] || item.role;
      const activity=details([roleLabel,item.model,item.effort].filter(Boolean).join(' · '),'activity-'+(item.turn || item.assignment));
      copy(activity,'回执',item.summary);
      const tokens=item.usage?.total_tokens;
      activity.append(el('p',Number.isSafeInteger(tokens) && tokens>=0 ? '已上报 '+tokens.toLocaleString('zh-CN')+' tokens' : '用量未报告','goal-muted'));
      copy(activity,'会话与轮次',[item.session,item.turn].filter(Boolean).join(' / '));
      copy(activity,'派工',item.assignment);
      textList(activity,'工具记录',item.tools);
      diagnostics.append(activity);
    }
    parent.append(diagnostics);
    if (callHistory && canReadInnerRepairs(row)) {
      const placeholder=el('div');
      diagnostics.append(placeholder);
      let calls;
      const load=()=>{
        if (!diagnostics.open || calls) return;
        calls=callHistory(placeholder,call);
        calls.refresh(row.id,()=>placeholder.isConnected && focused===row.id);
      };
      diagnostics.ontoggle=load; load();
    }
    if (decisions.length) {
      const ledger=details('程序已登记的决定','engine-decisions');
      textList(ledger,null,decisions); diagnostics.append(ledger);
    }
    if (!canSendText(row) || row.journey?.modify===false || ['PAUSED','STOPPED'].includes(row.status) || row.journey?.artifact_review || preferenceOf(row) || currentClarification(row)) return;
    const update=details(row.ticket_team?'变更目标或验收要求':'补充或修改要求','revisions',section==='revisions'); update.id='goal-section-revisions';
    const draft=getDraft(row.id+'/changes/'+row.revision);
    const contract=row.goal_contract || {};
    const replaceable=['deliverables','required_capabilities','nonfunctional_requirements','constraints','assumptions','out_of_scope']
      .flatMap(key=>contract[key] || []).filter(item=>['USER_EXPLICIT','USER_CONFIRMED'].includes(item.source));
    const choices=[];
    if(replaceable.length){
      const replace=details('选择要替换的原要求','replace');
      for(const item of replaceable){
        const label=el('label',null,'goal-optional-signoff');
        const selected=el('input');selected.type='checkbox';selected.value=item.requirement_id;
        selected.checked=(draft.data.replacing || []).includes(selected.value);
        selected.onchange=()=>draft.save({replacing:choices.filter(option=>option.checked).map(option=>option.value)});
        choices.push(selected);label.append(selected,el('span',item.description));replace.append(label);
      }
      update.append(replace);
    }
    const {group,field}=input(row.ticket_team?'目标或验收要求要怎么改？':'希望补充或修改什么？',draft,'text','goal-revision-text',{required:true});
    update.append(group);
    const feedback=actionStatus(update);
    update.append(writeButton(row.ticket_team?'提交变更':'提交本单要求','goal-submit-revision',()=>{
      if (!field.value.trim()) { feedback.textContent='请填写要求。'; field.focus(); return; }
      const replace_requirement_ids=choices.filter(option=>option.checked).map(option=>option.value);
      const text=field.value;
      const submit=(consent={})=>perform(row,'submit',{text,...consent,...(row.ticket_team?{intent:'change'}:{}),
        replace_requirement_ids:replace_requirement_ids.length?replace_requirement_ids:undefined},
        feedback,row.ticket_team?'变更请求已登记。':'补充或修改已登记。',draft);
      const replacement=replace_requirement_ids.length ? '替换选中的 '+replace_requirement_ids.length+' 项要求，并重新核对契约。' : '';
      if (requiresModelConsent(row)) {
        askModelInputConsent('你填写的要求将发送给 Codex 模型，用于重新整理本单的目标和验收条件。',
          '同意并提交',submit,replacement);
      } else if(replacement)approveAction('确认替换原要求',replacement,'确认替换',submit);
      else submit();
    }));
    parent.append(update);
  }
  function tabNav(row) {
    const nav=el('nav',null,'goal-tabs'); nav.setAttribute('aria-label','工单内容分区');
    for (const [name,label] of Object.entries(tabs)) {
      const tab=button(label,()=>{
        panel=name; section=name; writeRoute({id:row.id,anchor:name}); drawDetail(row);
        document.getElementById('goal-tab-'+name)?.focus({preventScroll:true});
      });
      tab.id='goal-tab-'+name; if (name===panel) tab.setAttribute('aria-current','page');
      nav.append(tab);
    }
    return nav;
  }
  function focusSection() {
    if (!focusNext) return;
    focusNext=false;
    requestAnimationFrame(()=>{
      let target=document.getElementById('goal-section-'+section);
      if (!target || section==='progress') target=document.getElementById('goal-ticket-title');
      if (!target) return;
      for (let parent=target;parent && parent!==main;parent=parent.parentElement)
        if (parent instanceof HTMLDetailsElement) parent.open=true;
      target.tabIndex=-1;
      target.scrollIntoView({block:'nearest'});
      target.focus({preventScroll:true});
    });
  }
  function drawDetail(row, receipt) {
    root.classList.toggle('has-ticket',Boolean(focused || eventId));
    if (activeRegular) {
      const key='regular/'+activeRegular;
      if (detailKey!==key) {
        detailKey=key; openDirection=null; main.replaceChildren(button('← 返回工单',back));
      }
      main.hidden=false; return;
    }
    main.hidden=!focused && !eventId;
    if (main.hidden) { detailKey=''; openDirection=null; main.replaceChildren(); return; }
    const pref=row && preferenceOf(row);
    const rejected=row && rejectedDrafts.get(draftPreviewKey(row,pref));
    const key=JSON.stringify([focused,eventId,row,receipt,panel,section,connected,rejected]);
    if (key===detailKey) { focusSection(); return; }
    const saved=position(main);
    selectedSnapshot=row;
    detailKey=key;
    const backButton=button('← 返回工单',back,'quiet'); backButton.id='goal-back';
    if (!row) {
      openDirection=null; main.replaceChildren(backButton);
      const selecting=pendingTicketSelection(receipt);
      main.append(el('h2',selecting?'这条需求属于哪张工单？':eventId?'需求已收到，正在建立工单':'正在核对原单'));
      if (eventId) main.append(el('p','收件号 '+eventId,'goal-muted'));
      if (selecting && !supportsExperiments && !receipt.router_selection) {
        main.append(el('p','请从工单簿打开已有工单，再提交补充要求。','goal-muted'));
      } else if (selecting) {
        const choices=el('div',null,'goal-actions');
        for (const item of [...(receipt.choices || []),{id:null,summary:'新建工单'}]) {
          const choice=button(item.summary || item.id,async()=>{
            if (commandInFlight || !connected) return;
            commandInFlight=true; choice.disabled=true;
            const original=eventId, fingerprint=JSON.stringify(['select',original,item.id]);
            const request_id=tokens.get(fingerprint) || crypto.randomUUID();
            tokens.set(fingerprint,request_id);
            try {
              const result=await call('select',{event_id:original,ticket_id:item.id,request_id});
              focused=result.ticket_id || ''; eventId=result.event_id || ''; focusNext=true;
              writeRoute({id:focused,event:eventId,replace:true});
            } catch (error) { notice.textContent=error.message; }
            finally { commandInFlight=false; choice.disabled=false; await refresh(); }
          });
          choices.append(choice);
        }
        main.append(choices);
      }
      return;
    }
    const model=project(row), header=el('header',null,'goal-detail-head');
    const heading=el('h2',model.title); heading.id='goal-ticket-title'; heading.tabIndex=-1;
    const identity=el('div',null,'goal-actions');
    identity.append(el('span',model.label,'goal-chip'),el('small',time(model.at),'goal-muted'));
    const copyId=button('工单 '+row.id.slice(-8),async()=>{
      try { await navigator.clipboard.writeText(row.id); notice.textContent='工单号已复制。'; }
      catch { notice.textContent=row.id; }
    },'quiet');
    copyId.id='goal-copy-id'; copyId.title=row.id;
    identity.append(copyId); header.append(heading,identity);
    if (row.ticket_team && row.status!=='STOPPED' && canSendText(row)) {
      identity.append(button('补充到原单',()=>{
        panel='decisions'; section='feedback'; focusNext=true;
        writeRoute({id:row.id,anchor:section}); drawDetail(row);
      },'quiet'));
    }
    const workflow=workflowSteps(row,model);
    if (workflow) header.append(workflow);
    const view=row.journey || {};
    const canDecide=model.station==='decision' && pref && !['PAUSED','STOPPED'].includes(row.status);
    const stagedKey=canDecide && !rejected ? stagedPreferenceKey(row,pref) : '';
    const directionKey=stagedKey && JSON.stringify([stagedKey,canSendText(row)]);
    // 同一版待定方向的表单和预览保持挂载，后台更新不重置阅读位置、选区和焦点。
    const keepDirection=directionKey && openDirection?.key===directionKey;
    const now=keepDirection ? openDirection.now : el('section',null,'goal-now');
    const summary=el('div');
    const nowTitle=el('h3',model.station==='decision'?'等你决定':'现在与下一步'); nowTitle.id='goal-section-decision'; nowTitle.tabIndex=-1;
    summary.append(nowTitle);
    if (!connected) summary.append(el('p','连接中断。操作暂停，正在核对工单状态。','goal-muted'));
    if (!rejected && model.reason) summary.append(el('p',model.reason));
    if (!rejected && model.next && model.next!==model.reason) summary.append(el('p',model.next,'goal-muted'));
    for (const item of row.journey?.decision_items || []) copy(summary,item.label,item.text);
    if (keepDirection) now.firstChild.replaceWith(summary);
    else now.append(summary);
    if (model.station==='decision') {
      if (canDecide && rejected) {
        const message=rejected===403 ? '会话已失效。请重新打开原单，核对当前初稿。' :
          '这版初稿已更新或无法核对。重新打开原单可查看当前状态。';
        now.append(el('p',message,'goal-feedback'),button('重新打开原单',()=>location.reload()));
      } else if (keepDirection) {
        openDirection.row=row;
        now.querySelector('#goal-submit-preference').disabled=!connected || commandInFlight;
      } else if (directionKey) {
        const direction={key:directionKey,now,row};
        openDirection=direction;
        preferenceForm(row,pref,now,direction);
      } else if (pref && !['PAUSED','STOPPED'].includes(row.status)) preferenceForm(row,pref,now);
      else if (needsModelConsent(row)) authorizeIntake(row,now);
      else if (canSendText(row) && currentClarification(row)) clarificationForm(row,now);
      else if (view.approve && row.approval_terms) approval(row,now);
      else if (row.execution && canExtendBudget(row)) budgetDecision(row,now);
      else if (view.artifact_review && panel!=='artifacts') now.append(button(
        model.anchor==='blind-review'?'查看匿名材料':'查看当前候选',()=>{
        panel='artifacts'; section=model.anchor; focusNext=true; writeRoute({id:row.id,anchor:section}); drawDetail(row);
      },'primary'));
      else if (panel!=='decisions' && model.anchor==='revisions' && canSendText(row) && canReviseAtDecision(row)) now.append(button('补充条件',()=>{
        panel='decisions'; section='revisions'; focusNext=true; writeRoute({id:row.id,anchor:section}); drawDetail(row);
      },'primary'));
    }
    const oldControls=keepDirection ? now.querySelector('details[data-disclosure="controls"]') : null;
    oldControls?.remove();
    if (view.pause || view.resume || (row.status==='PAUSED'&&view.stop)) {
      const controls=details('工单操作','controls');
      const actions=el('div',null,'goal-actions');
      const feedback=actionStatus(controls);
      if (view.pause && model.label!=='正在停止') actions.append(writeButton('暂停此单','goal-pause',()=>
        perform(row,'pause',{},feedback,'已请求暂停，正在核对执行状态。')));
      if (view.resume && model.label!=='正在停止') actions.append(writeButton('继续此单','goal-resume',()=>
        perform(row,'resume',{},feedback,'继续请求已登记。')));
      if (row.status==='PAUSED' && view.stop) actions.append(writeButton('停止此单','goal-stop',()=>
        approveAction('停止此单','停止后保留当前原单与已登记的成果。','确认停止',
          ()=>perform(row,'stop',{},feedback,'停止请求已登记。'))));
      controls.append(actions); now.append(controls);
    }
    const nav=tabNav(row);
    const content=el('section',null,'goal-content'); content.id='goal-section-'+panel;
    if (panel==='progress') displayProgress(row,content);
    else if (panel==='artifacts') displayArtifacts(row,content);
    else displayDecisions(row,content);
    if (keepDirection) {
      main.firstChild.replaceWith(backButton);
      now.previousSibling.replaceWith(header);
      now.nextSibling.replaceWith(nav);
      main.lastChild.replaceWith(content);
    } else {
      if (!directionKey) openDirection=null;
      main.replaceChildren(backButton,header,now,nav,content);
    }
    restorePosition(main,saved); focusSection();
  }
  async function refresh() {
    if (commandInFlight || confirmBox.open || root.closest('[hidden]')) return;
    const pass=++loadVersion;
    try {
      if (sessionExpired) {
        await renewSession?.();
        if (pass!==loadVersion) return;
        sessionExpired=false;
      }
      const result=await request('/api/outer');
      if (pass!==loadVersion) return;
      data=result; connected=true;
      checkedAt=new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});
      for (const row of data.drafts || []) known.add(row.id);
      const urlState=route();
      if (!focused && !eventId && known.has(urlState.regular)) {
        focused=urlState.regular; activeRegular=''; focusNext=true; regularVisibility?.(false);
      }
      if (eventId) {
        const receipt=await call('event',{event_id:eventId});
        if (pass!==loadVersion) return;
        if (receipt.ticket_id) { focusNext ||= !focused; focused=receipt.ticket_id; }
        const selecting=pendingTicketSelection(receipt);
        if (['DONE','REJECTED','NEEDS_ATTENTION','CANCELLED'].includes(receipt.state) && !selecting) {
          if (receipt.error || receipt.state!=='DONE') notice.textContent=receipt.error || '请核对这次收件的状态。';
          eventId=''; writeRoute({id:focused,anchor:section,replace:true});
        }
        if (!focused || selecting) { drawList(); drawDetail(undefined,receipt); return; }
      }
      drawList();
      const row=focused ? await call('get',{ticket_id:focused}) : undefined;
      if (pass!==loadVersion) return;
      if (row?.id===focused) cache.set(row.id,row);
      drawDetail(row);
    } catch (error) {
      if (pass!==loadVersion) return;
      connected=false; drawList(); drawDetail(cache.get(focused));
      if (!data || (focused && !cache.has(focused))) notice.textContent=error.message;
    }
  }
  showFilters();
  search.oninput=drawList;
  queueHead.append(search,filterBar,connection);
  sidebar.append(queueHead,queueBox);
  root.classList.add('goal-team');
  root.replaceChildren(notice,sidebar,main,confirmBox);
  root.hidden=false;
  window.addEventListener('popstate',syncRoute);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
  const timer=setInterval(()=>{
    if (!root.isConnected) { clearInterval(timer); return; }
    if (!document.hidden) refresh();
  },3500);
  syncRoute();
  return {
    refresh,
    hasTicket:id=>known.has(id),
    open,
    showRegular:id=>{
      if(activeRegular===id)return;
      ++loadVersion; activeRegular=id; focused=''; eventId=''; focusNext=false;
      regularVisibility?.(true); drawList(); drawDetail();
    },
    register: result=>{
      ++loadVersion;
      focused=result.ticket_id || ''; eventId=result.event_id || ''; activeRegular=''; panel='progress'; section='progress'; focusNext=true;
      writeRoute({id:focused,event:eventId,anchor:section});
      regularVisibility?.(false); viewChanged?.(); drawList(); drawDetail(cache.get(focused)); refresh();
    },
    setRegular: (rows,fresh)=>{
      for (const row of rows) if(row.goal_execution?.outer) known.add(row.id);
      if (activeRegular && known.has(activeRegular)) {
        focused=activeRegular; activeRegular=''; focusNext=true;
        regularVisibility?.(false); writeRoute({id:focused,anchor:section,replace:true}); refresh();
      }
      regular=rows.filter(row=>!row.goal_execution?.outer).map(row=>{
        const station=row.station || 'unknown';
        const anchors={decision:'approval',delivery:'artifacts',flight:'progress',dock:'progress',unknown:'progress'};
        const actions={decision:'查看待办',delivery:'查看交付',flight:'看进度',dock:'查看工单',unknown:'查看原因'};
        return {id:row.id,title:row.name,label:row.status_label,station,anchor:anchors[station],action:actions[station],
          reason:row.reason,at:row.updated_at || row.created_at || '',waitingSince:row.decision_since || row.updated_at || row.created_at || '',due:'',regular:true};
      });
      regularFresh=fresh; drawList();
    },
  };
}
