export function ordinaryHomeList(tasks, decisions) {
  const inDecisions = new Set(decisions.map(row => row.id));
  return tasks.filter(task => !inDecisions.has(task.id));
}

function summaryText(value) {
  const characters = Array.from(String(value || '').replace(/\s+/g,' ').trim());
  return characters.length > 140 ? characters.slice(0,140).join('')+'…' : characters.join('');
}

export function projectFormalOrdinaryDecisions(tasks, queueFor, canAnswerApproval, nextAction) {
  const reasons = {
    created: ['待开始','工单已保存，尚未开始规划。'],
    blocked: ['受阻','本轮遇到阻塞，等待处理。'],
    budget_exhausted: ['预算已用完','本单执行额度已用完。'],
    paused: ['已暂停','本单已暂停。'],
  };
  return tasks.flatMap(task=>{
    if (queueFor(task)!=='attention') return [];
    const row = {key:'ordinary:'+task.id, id:task.id, title:task.name, station:'decision', next:nextAction(task)};
    const approval = task.cards.find(card=>card.state==='open' && canAnswerApproval(task,card));
    if (approval) return [{...row, label:approval.title || '待审批', reason:approval.text || approval.title,
      action:'查看原单审批', panel:'plan', anchor:'approval'}];
    if (task.revision_pending) return [{...row, label:'待处理修改请求',
      reason:task.revision_pending.error || '修改流程正在等待恢复。',
      next:'查看修改记录，按当前状态重试或继续处理。',
      action:'查看修改记录', panel:'changes', anchor:'changes'}];
    const state = task.paused ? 'paused' : task.status;
    if (!Object.hasOwn(reasons,state)) return [];
    const research = task.workflow==='research';
    const [label, reason] = reasons[state];
    return [{...row, label:task.status_label || label, reason:task.error || reason,
      next:research ? '查看研究受阻原因和已有报告，再决定是否继续追问。' : row.next,
      action:'查看原单', panel:'overview', anchor:research ? 'research-follow' : 'actions'}];
  });
}

function mountFormalGoalQueue(decisions, root, renewSession, openOrdinary) {
  const approvals = document.getElementById('formal-goal-approvals');
  if (!decisions || !root || !approvals) return;

  const decisionHeading = decisions.querySelector('h2');
  const ordinaryStatus = document.getElementById('formal-home-ordinary-status');
  const codexStatus = document.getElementById('formal-home-codex-status');
  const decisionCount = decisions.querySelector('.goal-count');
  const decisionList = decisions.querySelector('ul');
  const decisionEmpty = decisions.querySelector('.goal-empty');
  const retry = decisions.querySelector('button');
  const heading = root.querySelector('h2');
  const content = root.querySelector('.goal-home-groups');
  const other = root.querySelector('nav');
  const otherLinks = Array.from(other.querySelectorAll('a')).map(link=>({link,label:link.textContent}));
  const approvalLink = approvals.querySelector('a');
  const approvalStatus = approvals.querySelector('[role=status]');
  const taskView = root.closest('.view-panel');
  const approvalView = approvals.closest('.view-panel');
  const stations = [
    ['flight', '正在推进', '当前没有正在推进的团队工单。'],
    ['delivery', '已交付', '交付成果会显示在这里。'],
  ];
  const previewLimit = 3;
  const create = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const setText = (element, text) => {if (element.textContent !== text) element.textContent = text;};
  const read = () => fetch('/api/outer-canary/drafts', {cache:'no-store', credentials:'same-origin'});
  let inFlight = false, queueKey, decisionKey, projection, importAttempt = 0;
  let ordinaryRows = [], codexRows = [], codexError = '';

  function card(row) {
    const item = create('li', '', 'goal-card');
    const title = create('h4', row.title);
    title.title = row.title;
    item.append(title, create('span', row.label, 'goal-chip'));
    const ordinary = Boolean(row.key);
    const ref = ordinary ? row.id.slice(0,11) : row.id.slice(-8);
    const scope = ordinary ? '普通工单' : row.routerTeam ? '团队工单' : '历史团队工单';
    item.append(create('small', scope+' '+ref, 'goal-muted'));
    if (row.station === 'decision') {
      if (row.reason) item.append(create('p', row.reason, 'goal-muted'));
    }
    const action = create(ordinary ? 'button' : 'a', row.action, 'goal-button');
    if (ordinary) {
      action.type = 'button';
      action.addEventListener('click', ()=>openOrdinary(row.id,row.panel,row.anchor));
    } else {
      action.href = '/workbench?'+new URLSearchParams({goal:row.id, goal_panel:row.anchor});
    }
    action.dataset.homeKey = row.key || 'codex:'+row.id;
    action.setAttribute('aria-label', row.action+' · '+row.title+' · '+scope+' '+ref);
    item.append(action);
    return item;
  }

  function restoreFocus(active, scope, fallback) {
    if (active.isConnected && !active.closest('[hidden]')) return;
    const replacement = Array.from(scope.querySelectorAll('[data-home-key]'))
      .find(action=>action.dataset.homeKey===active.dataset.homeKey);
    (replacement || fallback).focus({preventScroll:true});
  }

  function drawDecisions() {
    const active = document.activeElement;
    const preserve = decisions.contains(active);
    const rows = [...ordinaryRows,...codexRows].map(row=>({...row,reason:summaryText(row.reason || row.next)}));
    const key = JSON.stringify(rows.map(row=>[row.key,row.id,row.routerTeam,row.title,row.label,row.reason,row.panel,row.anchor,row.action]));
    if (key !== decisionKey) {
      decisionKey = key;
      decisionList.replaceChildren(...rows.map(card));
    }
    retry.hidden = !codexError;
    decisionList.hidden = !rows.length;
    decisionCount.hidden = !rows.length;
    setText(decisionCount,rows.length+' 张');
    decisionEmpty.hidden = Boolean(rows.length || ordinaryStatus.textContent || codexStatus.textContent);
    if (preserve) restoreFocus(active,decisionList,decisionHeading);
  }

  function draw(groups, projected = [], otherCounts = {}, message = '') {
    const active = document.activeElement;
    const preserve = content.contains(active) || other.contains(active);
    const key = JSON.stringify(groups?.map(rows=>rows.map(row=>[row.id,row.routerTeam,row.title,row.anchor,row.label,row.action])));
    if (key !== queueKey) {
      queueKey = key;
      content.replaceChildren(...(groups || []).map((rows, index) => {
        const [station, label, empty] = stations[index];
        const group = create('section', '', 'goal-queue goal-'+station);
        const title = create('h3', label);
        title.id = 'formal-goal-'+station;
        const header = create('div', '', 'goal-queue-title');
        header.append(title);
        if (rows.length) header.append(create('span', rows.length+' 张', 'goal-count'));
        group.setAttribute('aria-labelledby', title.id);
        group.append(header);
        if (rows.length) {
          const list = create('ul', '', 'goal-home-list');
          list.append(...rows.slice(0,previewLimit).map(card));
          group.append(list);
          if (rows.length > previewLimit) {
            const more = create('a', '查看全部'+rows.length+'张'+label+'工单', 'goal-button');
            more.href = '/workbench?station='+station;
            more.dataset.homeKey = 'station:'+station;
            group.append(more);
          }
        } else group.append(create('p', empty, 'goal-empty'));
        return group;
      }));
    }
    codexRows = projected;
    codexError = message;
    setText(codexStatus,message);
    setText(approvalStatus,message ? '暂时无法核对待决定的团队工单。连接恢复后会自动更新。' : '');
    const approvalLabel = codexRows.length ? '查看 '+codexRows.length+' 张待我决定的团队工单' : '查看待决定的团队工单';
    setText(approvalLink,approvalLabel);
    root.hidden = groups === null;
    approvals.hidden = groups === null && !message;
    for (const {link,label} of otherLinks) {
      const count = otherCounts[link.dataset.station] || 0;
      setText(link,label+' · '+count+' 张');
      link.hidden = !count;
    }
    other.hidden = otherLinks.every(({link})=>link.hidden);
    drawDecisions();
    if ((root.hidden && (preserve || root.contains(active))) || (approvals.hidden && approvals.contains(active))) {
      document.getElementById('refresh')?.focus({preventScroll:true});
    } else if (preserve) {
      restoreFocus(active,content,heading);
    }
  }

  async function refresh() {
    if (inFlight || document.hidden || (taskView.hidden && approvalView.hidden)) return;
    inFlight = true;
    retry.setAttribute('aria-disabled', 'true');
    try {
      let response = await read();
      if (response.status === 403) {
        await renewSession();
        response = await read();
      }
      if (response.status === 404) return draw(null);
      if (!response.ok) throw Error('无法读取团队工单');
      const data = await response.json();
      if (data.enabled === false) return draw(null);
      if (data.enabled !== true || !Array.isArray(data.drafts)) throw Error('团队工单记录不可用');
      if (!data.drafts.length && data.goal_team_enabled !== true) return draw(null);
      if (!projection) {
        const module = new URL('/outer-canary/goal_team.js',location.origin);
        if (importAttempt) module.searchParams.set('retry',String(importAttempt));
        importAttempt++;
        projection = await import(module.href);
      }
      const {projectGoalRow, sortQueue} = projection;
      const rows = data.drafts.filter(row=>/^wb-[a-f0-9]{32}$/.test(row?.id))
        .map(row=>projectGoalRow(row,{allowFreeText:row.allow_model_input===true}));
      const queue = station=>rows.filter(row=>row.station===station);
      draw(stations.map(([station])=>sortQueue(queue(station),station)), sortQueue(queue('decision'),'decision'),
        {dock:queue('dock').length, unknown:queue('unknown').length});
    } catch {
      draw(null, [], {}, '团队工单待办暂无法核对。连接恢复后会自动更新，也可以重试。');
    } finally {
      retry.removeAttribute('aria-disabled');
      inFlight = false;
    }
  }

  const observer = new MutationObserver(refresh);
  for (const view of [taskView, approvalView]) observer.observe(view,{attributeFilter:['hidden']});
  retry.addEventListener('click',refresh);
  document.getElementById('refresh')?.addEventListener('click',refresh);
  document.addEventListener('visibilitychange',refresh);
  window.addEventListener('pageshow',refresh);
  window.addEventListener('online',refresh);
  setInterval(refresh,10000);
  document.querySelector('.nav-item[data-view="tasks"] .nav-count')?.setAttribute('hidden','');
  decisions.hidden = false;
  refresh();
  return {
    setOrdinary(rows) {
      ordinaryRows = rows || [];
      setText(ordinaryStatus,rows ? '' : '普通工单待办暂无法核对。连接恢复后会自动更新。');
      drawDecisions();
    }
  };
}
