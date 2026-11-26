"""构建正式工作台的 Codex 团队入口和签名回环服务。"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

from prepare_formal_outer_pilot import BOARD, REPO, replace_once, source_only


FRONT_FILES = ("server.py", "index.html", "tasks.js")
HOME_QUEUE_CSS = """
.goal-home .goal-home-header { display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px; }
.goal-home .goal-home-groups { display:grid;gap:14px;grid-template-columns:minmax(0,1fr); }
@media (min-width:740px) {
  .goal-home .goal-home-groups { grid-template-columns:repeat(2,minmax(0,1fr)); }
}
.goal-home .goal-home-list { display:grid;gap:8px;grid-template-columns:repeat(auto-fit,minmax(min(100%,21rem),1fr));list-style:none;margin:0;padding:0;min-width:0; }
.goal-home .goal-home-list h4 { margin:0;font-size:1rem; }
.goal-home .goal-queue { align-content:start;gap:6px; }
.goal-home .goal-empty { border:0;background:transparent;padding:0;font-size:.875rem; }
.goal-home .goal-button { text-decoration:none; }
.goal-home > button { justify-self:start; }
#formal-home-decisions .goal-card { border-inline-start:3px solid var(--goal-primary); }
#formal-home-decisions .goal-chip { background:var(--goal-wait);color:var(--goal-wait-ink); }
#formal-home-decisions .goal-card p { white-space:pre-line; overflow-wrap:anywhere; }
#view-tasks .formal-ordinary-summary {
  display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,16rem),1fr));gap:8px;
  grid-auto-flow:row;grid-auto-columns:auto;position:static;max-height:none;overflow:visible;padding:0;
}
#view-tasks .formal-ordinary-summary > button { min-width:0;max-width:none;overflow-wrap:anywhere; }
#view-tasks .formal-ordinary-detail > button { margin-block:0 12px; }
#view-tasks .goal-home { margin-block-end:16px; }
#view-tasks .goal-home .goal-card { min-width:0; overflow-wrap:anywhere; }
#formal-home-decisions .goal-count { background:var(--goal-wait);color:var(--goal-wait-ink); }
#formal-goal-home .goal-flight .goal-count { background:var(--goal-run);color:var(--goal-run-ink); }
#formal-goal-home .goal-card h4 { display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden; }
#draft-form [hidden], #outer-intake-status:empty, #task-form-status:empty,
#draft-form:not(:has(.create-options[open])) #outer-sample-orders { display:none !important; }
#outer-team-settings { margin-block:12px; }
#draft-form label.outer-team-check {
  display:flex; align-items:flex-start; gap:9px; padding:8px 0; margin:0; min-height:40px; font-size:.875rem !important;
}
#draft-form .outer-team-check input { inline-size:1.1rem; block-size:1.1rem; flex:none; margin:2px 0 0; padding:0; accent-color:var(--red); }
#outer-intake-status { color:var(--ux-red); }
"""


def write(path, text):
    if path.suffix == ".py":
        compile(text, path.name, "exec")
    path.write_text(text)


def frontend(formal, front):
    server = (formal / "server.py").read_text()
    server = replace_once(server, "            if path.startswith('/api/afr-handoff/'):", """
            canary = getattr(self.server, 'outer_canary', None)
            if (path == '/api/tasks' and body.get('workflow') == 'outer') or path.startswith('/api/outer-canary/'):
                if canary is None:
                    raise Problem('智能工单灰度尚未启用', 404)
                return canary.post(self, path, body)
            if path.startswith('/api/afr-handoff/'):""".lstrip("\n"))
    server = replace_once(server, "        if route.startswith('/api/afr-handoff/'):", """
        canary = getattr(self.server, 'outer_canary', None)
        if canary is not None:
            from outer_canary import ASSETS, PREFIX
            if route in ASSETS or route.startswith(PREFIX):
                try:
                    return canary.get(self, route)
                except OSError:
                    return self.json_response({'error':'智能工单页面暂不可用'}, 503)
        if route.startswith('/api/afr-handoff/'): """.lstrip("\n").rstrip())
    server = replace_once(server, "    args = parser.parse_args()",
                          "    parser.add_argument('--outer-canary-profile', type=Path)\n    args = parser.parse_args()")
    server = replace_once(server, "        server.task_actions = threading.Lock()", """
        if args.outer_canary_profile:
            from outer_canary import OuterCanary
            try:
                server.outer_canary = OuterCanary(args.outer_canary_profile, ROOT / 'outer-canary')
            except (OSError, ValueError, KeyError, TypeError):
                print('智能工单灰度配置无效，已关闭灰度入口', flush=True)
        server.task_actions = threading.Lock()""".lstrip("\n"))
    server = replace_once(server, "        self.respond(file.read_bytes(), types[file.suffix])", """
        body = file.read_bytes()
        if route in ('/', '/index.html') and not getattr(self.server, 'outer_canary', None):
            for region in (b'OUTER-CANARY', b'OUTER-TEAM-OPTIONS', b'OUTER-INTAKE-ADVANCED', b'OUTER-HOME-ASSETS', b'OUTER-HOME-QUEUE', b'OUTER-HOME-APPROVALS'):
                body = body.replace(b'<!--' + region + b'-BEGIN-->', b'<template>', 1)
                body = body.replace(b'<!--' + region + b'-END-->', b'</template>', 1)
        self.respond(body, types[file.suffix])""".lstrip("\n"))
    html = (formal / "index.html").read_text()
    html = replace_once(html, '</head>', '''<!--OUTER-HOME-ASSETS-BEGIN--><link rel="stylesheet" href="/outer-canary/goal_team.css"><!--OUTER-HOME-ASSETS-END-->
</head>''')
    html = replace_once(html, '<div id="live-tasks">', '''<!--OUTER-HOME-QUEUE-BEGIN-->
        <section id="formal-home-decisions" class="goal-team goal-home" aria-labelledby="formal-home-decisions-title" hidden>
          <div class="goal-queue-title"><h2 id="formal-home-decisions-title" tabindex="-1">待我决定</h2>
            <span class="goal-count" hidden></span>
          </div>
          <p class="goal-connection" id="formal-home-ordinary-status" role="status">正在读取普通工单待办。</p>
          <p class="goal-connection" id="formal-home-codex-status" role="status">正在读取团队工单待办。</p>
          <button type="button" class="goal-button" hidden>重新读取团队工单</button>
          <ul class="goal-home-list" hidden></ul>
          <details class="desk-more-decisions" hidden><summary></summary><ul class="goal-home-list"></ul></details>
          <p class="goal-empty" hidden>当前没有需要你决定的工单。</p>
        </section>
        <nav id="desk-order-switch" aria-label="选择工单列表" hidden>
          <div><button type="button" data-desk-list="team" aria-pressed="true" aria-controls="formal-goal-home">团队工单</button>
          <button type="button" data-desk-list="ordinary" aria-pressed="false" aria-controls="live-tasks">普通工单</button></div>
          <a href="/workbench">打开工单簿 <span aria-hidden="true">↗</span></a>
        </nav>
        <section id="formal-goal-home" class="goal-team goal-home" aria-labelledby="formal-goal-home-title" hidden>
          <h2 id="formal-goal-home-title" tabindex="-1">团队工单</h2>
          <div class="goal-home-groups"></div>
          <nav class="goal-actions" aria-label="其他团队工单" hidden>
            <a class="goal-button" href="/workbench?station=dock" data-station="dock" hidden>已暂停或取消</a>
            <a class="goal-button" href="/workbench?station=unknown" data-station="unknown" hidden>待核对</a>
          </nav>
        </section>
        <!--OUTER-HOME-QUEUE-END--><div id="live-tasks">''')
    html = replace_once(html, '<details id="historical-approval-panel"', '''<!--OUTER-HOME-APPROVALS-BEGIN-->
        <section id="formal-goal-approvals" class="goal-team goal-home" aria-labelledby="formal-goal-approvals-title" hidden>
          <div class="goal-home-header"><h2 id="formal-goal-approvals-title">团队工单</h2>
            <a class="goal-button" href="/workbench?station=decision">查看待决定的团队工单</a>
          </div>
          <p class="goal-connection" role="status"></p>
        </section>
        <!--OUTER-HOME-APPROVALS-END--><details id="historical-approval-panel"''')
    for old, new in (('>待审批 <span id="game-open"', '>普通工单审批 <span id="game-open"'),
                     ('</span> 待审批 <span class="nav-count red"', '</span> 普通工单审批 <span class="nav-count red"'),
                     ('aria-label="查看待审批事项"', 'aria-label="查看普通工单审批"')):
        html = replace_once(html, old, new)
    html = replace_once(html, '写下你想得到的结果。默认由 Codex 规划、执行和独立审核，实施前由你确认方案。',
                        '只要写下想得到的结果。需要取舍时，团队会带着方案回来。')
    html = replace_once(html, '描述你想要的结果，以及怎样才算做好。', '例如，帮我做一份适合第一次来访的旅行计划。')
    html = replace_once(html, '完成标准、名称与执行设置</summary>', '补充要求与执行设置（选填）</summary>')
    html = replace_once(html, '怎样算完成？（选填）', '已经想好的完成标准（选填）')
    dialog = '</span></button></form></dialog>'
    # 插在旧提交按钮后，保留表单的默认回车操作。
    html = replace_once(html, dialog, '''</span></button><!--OUTER-CANARY-BEGIN--><details id="outer-sample-orders" class="model-fields"><summary>合成试单</summary><div class="wb-actions">
<button id="outer-canary-submit" type="submit" formnovalidate class="secondary-button">提交合成索引试单</button>
<button id="outer-team-canary-submit" type="submit" formnovalidate class="secondary-button">提交合成简报试单</button>
</div></details><!--OUTER-CANARY-END--></form></dialog>''')
    html = replace_once(html, '</textarea><fieldset class="model-fields"><legend>角色与模型</legend><div class="wb-actions">',
                        '''</textarea><!--OUTER-INTAKE-ADVANCED-BEGIN-->
<label class="outer-team-check"><input type="checkbox" id="outer-goal-team" checked><span>交给工单团队处理</span></label>
<div id="outer-team-options" hidden>
<label for="outer-team-review-mode">方案比较</label>
<select id="outer-team-review-mode">
<option value="">简单直接做，复杂再比较</option>
<option value="review">主动比较双方案，由团队判断</option>
<option value="review_and_decide">分步交付，第 2 步请我定方向</option>
</select>
<p class="draft-warning" id="outer-team-review-hint" hidden>团队分步交付，到第 2 步请你选方向。</p>
</div><!--OUTER-INTAKE-ADVANCED-END--><fieldset class="model-fields"><legend>角色与模型</legend><div class="wb-actions">''')
    html = replace_once(html, '<p class="draft-warning" id="task-form-status">',
                        '''<!--OUTER-TEAM-OPTIONS-BEGIN--><div id="outer-team-settings" hidden>
<label class="outer-team-check"><input type="checkbox" id="outer-team-consent"><span>允许把本单填写的名称、目标和完成标准发送给 Codex</span></label>
</div><p class="draft-warning" id="outer-intake-status" role="status"></p><!--OUTER-TEAM-OPTIONS-END--><p class="draft-warning" id="task-form-status">''')
    tasks = replace_once((formal / "tasks.js").read_text(), """    if (working) return;
    working = true;
    const button = form.querySelector('[type=submit]');""", """    if (working) return;
    const team = Boolean(node('#outer-goal-team')?.checked);
    const synthetic = ['outer-canary-submit','outer-team-canary-submit'].includes(event.submitter?.id);
    if (team || synthetic) {
      const consent = node('#outer-team-consent');
      if (!synthetic && !consent.checked) {
        intakeStatus.textContent = '请先勾选发送许可，再提交团队工单。';
        consent.focus();
        return;
      }
      working = true;
      intakeStatus.textContent = '正在提交工单…';
      const submitter = event.submitter || form.querySelector('[type=submit]');
      submitter.disabled = true;
      try {
        let body;
        if (synthetic) {
          const fixture = submitter.id === 'outer-team-canary-submit' ? 'goal-team-brief-v1' : 'filename-index-v2';
          body = {workflow:'outer',fixture};
        } else {
          const name = form.elements.name.value.trim();
          const goal = form.elements.goal.value.trim();
          if (!goal) throw Error('请填写希望完成的目标。');
          const acceptance = form.elements.acceptance.value.trim();
          body = {workflow:'outer',name:name || Array.from(goal).slice(0,80).join(''),goal,acceptance,
                  allow_model_input:true,request_id:requestId ||= crypto.randomUUID()};
          const mode = node('#outer-team-review-mode').value;
          if (mode) body.team_review_mode = mode;
        }
        const result = await post('/api/tasks', body);
        const target = new URL('/workbench', location.origin);
        if (result.ticket_id) target.searchParams.set('goal', result.ticket_id);
        else target.searchParams.set('outer_event', result.event_id);
        location.assign(target);
      } catch (error) {
        intakeStatus.textContent = error.message;
      } finally {
        working = false;
        submitter.disabled = false;
      }
      return;
    }
    working = true;
    const button = form.querySelector('[type=submit]');""")
    tasks = replace_once(tasks, "  const modeInput=node('#task-mode'), timeInput=node('#task-budget');",
                         """  const teamMode=node('#outer-goal-team');
  const teamConsent=node('#outer-team-consent');
  const intakeStatus=node('#outer-intake-status');
  const teamModelFields=form.querySelector('fieldset.model-fields');
  const syncTeamMode=()=>{
    const enabled=Boolean(teamMode?.checked);
    if (!teamMode) return;
    node('#outer-team-settings').hidden=!enabled;
    node('#outer-team-options').hidden=!enabled;
    node('#task-form-status').hidden=enabled;
    form.querySelector('.create-options > .draft-warning:last-child').hidden=enabled;
    node('#outer-team-review-hint').hidden=node('#outer-team-review-mode').value!=='review_and_decide';
    if (!enabled) teamConsent.checked=false;
    teamModelFields.hidden=enabled;
    for (const id of ['task-mode','task-budget']) {
      node('#'+id).hidden=enabled;
      form.querySelector('label[for="'+id+'"]').hidden=enabled;
    }
    const button=form.querySelector('[type=submit]');
    if (!button.dataset.defaultText) button.dataset.defaultText=button.textContent;
    button.textContent=enabled?'提交工单':button.dataset.defaultText;
  };
  teamMode?.addEventListener('change',()=>{intakeStatus.textContent='';syncTeamMode();});
  node('#outer-team-review-mode')?.addEventListener('change',syncTeamMode);
  teamConsent?.addEventListener('change',()=>{
    if (teamConsent.checked && intakeStatus.textContent.startsWith('请先勾选')) intakeStatus.textContent='';
  });
  for (const name of ['name','goal','acceptance']) {
    form.elements[name].addEventListener('input',()=>{if(teamConsent)teamConsent.checked=false;});
  }
  node('#task-dialog').addEventListener('close',()=>{
    if (teamConsent) {teamConsent.checked=false;intakeStatus.textContent='';}
  });
  form.addEventListener('reset',()=>setTimeout(syncTeamMode,0));
  syncTeamMode();
  const modeInput=node('#task-mode'), timeInput=node('#task-budget');""")
    tasks = replace_once(tasks, '<h2>任务审批</h2>', '<h2>普通工单审批</h2>')
    activity_switch = '<div class="wb-actions">${tasks.map(t=>`<button class="secondary-button" data-wb-select="${t.id}">${html(t.name)}</button>`).join(\'\')}</div>'
    tasks = replace_once(tasks, activity_switch, '<details class="journal-switch"><summary>切换工单（${tasks.length}）</summary>' + activity_switch + '</details>')
    tasks = replace_once(tasks, '当前没有新的任务审批。', '当前没有新的普通工单审批。')
    tasks = replace_once(tasks, '<h2>我的工单</h2>', "<h2>${homeQueue?'普通工单':'我的工单'}</h2>")
    tasks = replace_once(tasks, """  function approvals(task) {
    return task.cards.filter(card => card.state === 'open').map(card => {
      const applicable = connected && !task.observation_stale && !task.runs?.some(r=>r.state==='unknown') && !task.revision_pending && !task.paused && card.binding?.approval === task.approval?.id
        && card.binding?.version === task.version;""", """  function canAnswerApproval(task, card) {
    return connected && !task.observation_stale && !task.runs?.some(r=>r.state==='unknown') && !task.revision_pending && !task.paused && card.binding?.approval === task.approval?.id
      && card.binding?.version === task.version;
  }
  function approvals(task) {
    return task.cards.filter(card => card.state === 'open').map(card => {
      const applicable = canAnswerApproval(task, card);""")
    tasks = replace_once(tasks, '<div class="next-action">', '<div class="next-action" id="section-actions" tabindex="-1">')
    tasks = replace_once(tasks, '<form class="mission-section" data-research-follow=',
                         '<form class="mission-section" id="section-research-follow" tabindex="-1" data-research-follow=')
    tasks = replace_once(tasks, '    target.innerHTML=markup;\n    if(keep)target.querySelectorAll', """    const previousQuestion=keep ? target.querySelector('#research-question') : null;
    target.innerHTML=markup;
    const currentQuestion=previousQuestion && target.querySelector('#research-question');
    if(currentQuestion){
      currentQuestion.value=previousQuestion.value;
      currentQuestion.setSelectionRange(previousQuestion.selectionStart,previousQuestion.selectionEnd,previousQuestion.selectionDirection);
    }
    if(keep)target.querySelectorAll""")
    tasks = replace_once(tasks, "    if (!tasks.some(task => task.id === selected)", """    const homepageDecisions = homeQueue && fresh ? projectFormalOrdinaryDecisions(tasks, queueFor, canAnswerApproval, nextAction) : [];
    homeQueue?.setOrdinary(fresh ? homepageDecisions : null);
    if (!tasks.some(task => task.id === selected)""")
    tasks = replace_once(tasks, "    const list=visible.map(task=>", """    const summaryTasks=homeQueue ? ordinaryHomeList(visible, homepageDecisions) : visible;
    const list=summaryTasks.map(task=>""")
    tasks = replace_once(tasks, 'aria-pressed="${task.id===selected}"',
                         'aria-pressed="${task.id===selected&&(!homeQueue||selected===linkedTask)}"')
    tasks = replace_once(tasks, "    const showDetail=current && (queue==='all'||visible.some(t=>t.id===selected));",
                         "    const showDetail=current && (!homeQueue||selected===linkedTask) && (queue==='all'||visible.some(t=>t.id===selected));")
    tasks = replace_once(tasks, '${list?`<div class="wb-grid"><div class="wb-list">${list}</div>${selectedDetail}</div>`',
                         """${homeQueue && showDetail ? `<div class="formal-ordinary-detail" id="section-ordinary-detail" tabindex="-1"><button class="secondary-button" data-close-ordinary>收起工单详情</button>${selectedDetail}</div>` :
      homeQueue && list ? `<div class="wb-list formal-ordinary-summary">${list}</div>` :
      homeQueue && visible.length ? empty('本组工单均在上方「待我决定」。') :
      list?`<div class="wb-grid"><div class="wb-list">${list}</div>${selectedDetail}</div>`""")
    tasks = replace_once(tasks, "    if(button.hasAttribute('data-reset-focus')){",
                         """    if(button.hasAttribute('data-close-ordinary')){
      const previous = selected;
      clearTaskFocus();
      start();render();
      const action = [...document.querySelectorAll('[data-home-key],[data-wb-select]')]
        .find(item=>!item.closest('[hidden]') &&
          (item.dataset.homeKey==='ordinary:'+previous || item.closest('#live-tasks') && item.dataset.wbSelect===previous));
      (action || node('#live-tasks [data-queue="all"]'))?.focus();
      return;
    }
    if(button.hasAttribute('data-reset-focus')){""")
    tasks = replace_once(tasks, "      if(visible.length&&!visible.some(t=>t.id===selected)){",
                         "      if(!homeQueue&&visible.length&&!visible.some(t=>t.id===selected)){")
    tasks = replace_once(tasks, "    if (button.dataset.wbSelect) {",
                         """    if (button.dataset.wbSelect) {
      const openingSummary=homeQueue && button.closest('.formal-ordinary-summary');""")
    tasks = replace_once(tasks, "      syncTaskURL();\n      render();\n      return;\n    }\n    const action = button.dataset.wbAnswer",
                         """      syncTaskURL();
      render();
      if(openingSummary)focusSection('ordinary-detail');
      return;
    }
    const action = button.dataset.wbAnswer""")
    tasks = replace_once(tasks, "  start();\n})();", """  const homeQueue = mountFormalGoalQueue(node('#formal-home-decisions'),node('#formal-goal-home'),session,
    openSection);
  start();
})();""")
    tasks += "\n" + (REPO / "scripts/formal_home_goal_queue.js").read_text()
    for name, text in zip(FRONT_FILES, (server, html, tasks)):
        write(front / name, text)
    shutil.copy2(REPO / "scripts/formal_outer_canary.py", front / "outer_canary.py")
    shutil.copy2(REPO / "scripts/formal_outer_canary_wire.py", front)
    ui = front / "outer-canary"
    ui.mkdir()
    candidate = (BOARD / "index.html").read_text().replace('href="/style.css"', 'href="/outer-canary/style.css"').replace(
        'src="/app.js"', 'src="/outer-canary/app.js"').replace('<form id="new">', '<form id="new" hidden>')
    candidate = replace_once(candidate, '<a id="map-link" href="/">王国地图</a><a href="#tickets">工单簿</a>',
                             '<a id="map-link" href="/#tasks">工单工作台</a><a href="/workbench">工单簿</a>')
    write(ui / "index.html", candidate)
    app = replace_once((BOARD / "app.js").read_text(), "    form.hidden=!outer.workbench_entry;\n    byId('outer-opt-in').hidden=!outer.workbench_entry;\n", "")
    app = replace_once(app, "launch.hidden=false;launch.textContent", "launch.hidden=true;launch.textContent")
    app = replace_once(app, "if(new URLSearchParams(location.search).has('new'))launch.click();", "")
    app = replace_once(app, "request,renewSession,initial:outer,callHistory,",
                       "request,renewSession,initial:outer,callHistory,formalModelInputConsent:true,")
    app = app.replace("'/api/tasks", "'/api/outer-canary/tasks").replace("'/api/outer'", "'/api/outer-canary/drafts'")
    write(ui / "app.js", app)
    outer = (BOARD / "outer.js").read_text().replace("'/api/tasks/", "'/api/outer-canary/tasks/").replace(
        "'/api/outer/'", "'/api/outer-canary/'").replace("'/api/outer'", "'/api/outer-canary/drafts'")
    edits = outer.index("    if(!v.artifact_review && !['PAUSED','STOPPED'].includes(d.status)){")
    controls = outer.index("    if(v.pause)", edits)
    outer = outer[:edits] + outer[controls:]
    signoff = outer.index("        if(v.artifact_review && candidate.state==='pending'){")
    downloads = outer.index("      for(const path of e?.artifacts", signoff)
    outer = outer[:signoff] + "      }\n" + outer[downloads:]
    # 合成试单只有机器验收；已签收和用户自由文本操作不从正式灰度转发。
    begin = outer.index("      const required=Boolean(contract.human_review_requirements?.length);")
    end = outer.index("      },true);", begin)
    outer = outer[:begin] + """      action(spec,'批准本版目标与验收',async()=>{
        if(confirm('批准上列目标、验收、权限与预算并开始执行？')){
          await command('approve');
          notice('本版授权已保存。');
        }
""" + outer[end:]
    outer = replace_once(outer, "...binding,...extra,request_id:crypto.randomUUID()",
                         "...(name==='approve'?binding:{ticket_id:d.id,revision:d.revision}),...extra,request_id:crypto.randomUUID()")
    outer = replace_once(outer, "by('launch').hidden=false;", "by('launch').hidden=true;")
    outer = replace_once(outer, "if(query.has('new')){composer.showModal();", "if(query.has('new')){")
    outer = replace_once(outer, "'发布一个目标，开始第一段航程。'", "'请从工单工作台新建工单。'")
    outer = replace_once(outer, "by('map-link').href=chosen?'/?trip='+encodeURIComponent(chosen):'/';",
                         "by('map-link').href='/#tasks';")
    write(ui / "outer.js", outer)
    team = (BOARD / "goal_team.js").read_text().replace("'/api/tasks/", "'/api/outer-canary/tasks/").replace(
        "'/api/outer/'", "'/api/outer-canary/'").replace("'/api/outer'", "'/api/outer-canary/drafts'")
    write(ui / "goal_team.js", team)
    styles = [(BOARD / "goal_team.css").read_text(), HOME_QUEUE_CSS,
              (REPO / "scripts/formal_odyssey_home.css").read_text(),
              (REPO / "scripts/formal_odyssey_views.css").read_text()]
    write(ui / "goal_team.css", "\n".join(styles))
    style = replace_once((BOARD / "style.css").read_text(),
                         ".mast{gap:.5rem}.mast nav{margin-left:auto}.mast nav a:last-child{display:none}",
                         ".mast{gap:.5rem;flex-wrap:wrap}.mast nav{margin-left:auto;gap:.6rem}"
                         ".mast nav a{display:inline-flex;align-items:center;min-height:44px;padding-inline:.25rem}")
    write(ui / "style.css", style)
    shutil.copy2(BOARD / "task_presentation.js", ui / "task_presentation.js")


def backend(app):
    shutil.copytree(BOARD, app, ignore=source_only(BOARD))
    server = replace_once((app / "server.py").read_text(), """    def local(self):
        return self.headers.get('Host') in {""", """    def local(self):
        from formal_outer_canary_wire import request_nonce
        key = os.environ.get('AUTOLOOP_CANARY_GATEWAY_KEY', '')
        self.canary_nonce = request_nonce(key, self.command, self.path,
                                          self.headers.get('X-Odyssey-Canary-Gateway', ''))
        if self.canary_nonce is None:
            return False
        return self.headers.get('Host') in {""")
    server = replace_once(server, "        for key, value in headers:", """        from formal_outer_canary_wire import response_proof
        if getattr(self, 'canary_nonce', None):
            proof = response_proof(os.environ['AUTOLOOP_CANARY_GATEWAY_KEY'], self.canary_nonce, status, body)
            self.send_header('X-Odyssey-Canary-Proof', proof)
        for key, value in headers:""")
    server = replace_once(server, "            if dsh:\n                return self.dsh_command", """            from outer_canary import (
                SYNTHETIC_ORDER, TEAM_ORDER, command_uses_model_input, valid_command, valid_custom_order,
            )
            if path == '/api/tasks':
                if body not in (SYNTHETIC_ORDER, TEAM_ORDER) and not valid_custom_order(body):
                    raise Problem('请从工作台确认本单资料范围后登记', 409)
            elif not path.startswith('/api/outer/') or not valid_command(path.removeprefix('/api/outer/'), body):
                raise Problem('灰度后台未开放此操作', 400)
            if dsh:
                return self.dsh_command""")
    server = replace_once(server, "legacy_fixture = bool(provided and body.get('allow_model_input') is not True)",
                          "legacy_fixture = body in (SYNTHETIC_ORDER, TEAM_ORDER)")
    approval = "                return self.json_response(dispatch(workbench.outer(),'web',cmd,body))"
    server = replace_once(server, approval, """                if cmd == 'select' and not workbench.outer().event(body['event_id'], 'owner').get('router_selection'):
                    raise Problem('请从原工单渠道核对已有事件', 409)
                free_text = command_uses_model_input(cmd, body)
                if cmd == 'approve' or free_text:
                    current = workbench.outer().get(body.get('ticket_id'), 'owner')
                    if free_text and current.get('allow_model_input') is not True:
                        raise Problem('当前固定试单只接受预设选项。可另建自定义目标工单。', 409)
                if cmd == 'approve':
                    criteria = (current.get('acceptance_contract') or {}).get('criteria', [])
                    if any(c.get('verifier_id') == 'blind-projection' for c in criteria):
                        raise Problem('盲评需使用独立试验工作台。可在此单调整验收方式。', 409)
                    if current.get('allow_model_input') is not True and (not criteria or any(
                            c.get('verification_type') != 'DETERMINISTIC' for c in criteria)):
                        raise Problem('固定试单只接受确定性验收', 409)
                return self.json_response(dispatch(workbench.outer(),'web',cmd,body))""")
    server = replace_once(server, "    if args.outer_goal_team and (\n", """    # 只有签名网关后的专用灰度进程可用持久 side runtime。
    signed_canary = (args.runtime is not None and args.hub_home is not None
                     and re.fullmatch(r'[a-f0-9]{64}', os.environ.get('AUTOLOOP_CANARY_GATEWAY_KEY', ''))
                     and args.runtime.resolve() == ROOT.parent / 'runtime/workbench'
                     and args.hub_home.resolve() == ROOT.parent / 'runtime/hub'
                     and (ROOT.parent / 'runtime/.goal-pilot').is_file())
    if args.outer_goal_team and (
""")
    server = replace_once(server, """            or Path('/private/tmp') not in args.runtime.resolve().parents
            or not (args.runtime.resolve().parent/'.goal-team-pilot').is_file()""", """            or (not signed_canary and (Path('/private/tmp') not in args.runtime.resolve().parents
                or not (args.runtime.resolve().parent/'.goal-team-pilot').is_file()))
            or (signed_canary and (dsh_token or trial_token))""")
    write(app / "server.py", server)
    workbench = (app / "outer_workbench.py").read_text()
    workbench = replace_once(workbench, "    team_enabled = getattr(workbench, 'outer_goal_team', False)",
                             "    team_enabled = getattr(workbench, 'outer_goal_team', False) and (\n"
                             "        not legacy_fixture or 'team_review_mode' in body or body.get('allow_model_input') is True)")
    write(app / "outer_workbench.py", workbench)
    shutil.copy2(REPO / "scripts/formal_outer_canary_wire.py", app)
    shutil.copy2(REPO / "scripts/formal_outer_canary.py", app / "outer_canary.py")
    verifier = Path("验收样例/目标驱动CSV")
    (app / verifier).mkdir(parents=True)
    for name in ("verifier.py", "input.csv"):
        shutil.copy2(BOARD / verifier / name, app / verifier / name)


def prepare(formal, target):
    if target.exists() or formal == target or formal in target.parents or target in formal.parents:
        raise ValueError("请指定一个独立且尚未创建的输出目录")
    for name in FRONT_FILES:
        if (formal / name).is_symlink() or not (formal / name).is_file():
            raise ValueError(f"正式工作台源码不可用或为链接：{name}")
    front = target / "front"
    front.mkdir(parents=True, mode=0o700)
    frontend(formal, front)
    backend(target / "app")
    (target / "baseline.json").write_text(json.dumps(
        {name: hashlib.sha256((formal / name).read_bytes()).hexdigest() for name in FRONT_FILES}, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("formal", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        prepare(args.formal.resolve(strict=True), args.output.resolve())
    except (OSError, ValueError) as error:
        parser.error(str(error))
