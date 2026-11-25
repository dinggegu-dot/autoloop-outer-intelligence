"""构建正式工作台的 Codex 团队入口和签名回环服务。"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

from prepare_formal_outer_pilot import BOARD, REPO, replace_once, source_only


FRONT_FILES = ("server.py", "index.html", "tasks.js")


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
            body = body.replace(b'<!--OUTER-CANARY-BEGIN-->', b'<template>', 1)
            body = body.replace(b'<!--OUTER-CANARY-END-->', b'</template>', 1)
        self.respond(body, types[file.suffix])""".lstrip("\n"))
    html = (formal / "index.html").read_text()
    dialog = '</span></button></form></dialog>'
    # 插在旧提交按钮后，保留旧表单的默认回车操作。
    html = replace_once(html, dialog, '''</span></button><!--OUTER-CANARY-BEGIN--><div class="wb-actions">
<button id="outer-canary-submit" type="submit" formnovalidate class="secondary-button">提交合成索引试单</button>
<button id="outer-team-canary-submit" type="submit" formnovalidate class="secondary-button">提交合成简报试单</button>
<a href="/workbench">查看 Codex 团队工单</a></div><!--OUTER-CANARY-END--></form></dialog>''')
    html = replace_once(html, '<p class="draft-warning" id="task-form-status">',
                        '''<!--OUTER-TEAM-OPTIONS-BEGIN--><fieldset id="outer-team-options" class="model-fields">
<legend>任务交给谁</legend>
<label><input type="checkbox" id="outer-goal-team"> 使用 Codex 团队</label>
<div id="outer-team-settings" hidden>
<label for="outer-team-review-mode">团队审稿</label>
<select id="outer-team-review-mode">
<option value="">随任务自动安排</option>
<option value="review">比较方案并审稿</option>
<option value="review_and_decide">从第 2 步审稿并请我定方向</option>
</select>
<label><input type="checkbox" id="outer-team-consent"> 允许把本单填写的名称、目标和完成标准发送给 Codex</label>
<p class="draft-warning">勾选后开始研判。执行等你在工单批准。请只填写获准发送的内容。</p>
</div></fieldset><!--OUTER-TEAM-OPTIONS-END--><p class="draft-warning" id="task-form-status">''')
    server = replace_once(server, "            body = body.replace(b'<!--OUTER-CANARY-END-->', b'</template>', 1)",
                          """            body = body.replace(b'<!--OUTER-CANARY-END-->', b'</template>', 1)
            body = body.replace(b'<!--OUTER-TEAM-OPTIONS-BEGIN-->', b'<template>', 1)
            body = body.replace(b'<!--OUTER-TEAM-OPTIONS-END-->', b'</template>', 1)""")
    tasks = replace_once((formal / "tasks.js").read_text(), """    if (working) return;
    working = true;
    const button = form.querySelector('[type=submit]');""", """    if (working) return;
    const team = Boolean(node('#outer-goal-team')?.checked);
    const synthetic = ['outer-canary-submit','outer-team-canary-submit'].includes(event.submitter?.id);
    if (team || synthetic) {
      const consent = node('#outer-team-consent');
      if (!synthetic && !consent.checked) {
        node('#task-form-status').textContent = '登记前请确认是否同意将本单填写的内容发送给 Codex。';
        consent.focus();
        return;
      }
      working = true;
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
        node('#task-form-status').textContent = error.message;
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
  const teamModelFields=form.querySelector('fieldset.model-fields');
  const syncTeamMode=()=>{
    const enabled=Boolean(teamMode?.checked);
    if (!teamMode) return;
    node('#outer-team-settings').hidden=!enabled;
    if (!enabled) teamConsent.checked=false;
    teamModelFields.hidden=enabled;
    for (const id of ['task-mode','task-budget']) {
      node('#'+id).hidden=enabled;
      form.querySelector('label[for="'+id+'"]').hidden=enabled;
    }
    const button=form.querySelector('[type=submit]');
    if (!button.dataset.defaultText) button.dataset.defaultText=button.textContent;
    button.textContent=enabled?'登记目标':button.dataset.defaultText;
  };
  teamMode?.addEventListener('change',syncTeamMode);
  for (const name of ['name','goal','acceptance']) {
    form.elements[name].addEventListener('input',()=>{if(teamConsent)teamConsent.checked=false;});
  }
  node('#task-dialog').addEventListener('close',()=>{if(teamConsent)teamConsent.checked=false;});
  form.addEventListener('reset',()=>setTimeout(syncTeamMode,0));
  syncTeamMode();
  const modeInput=node('#task-mode'), timeInput=node('#task-budget');""")
    for name, text in zip(FRONT_FILES, (server, html, tasks)):
        write(front / name, text)
    shutil.copy2(REPO / "scripts/formal_outer_canary.py", front / "outer_canary.py")
    shutil.copy2(REPO / "scripts/formal_outer_canary_wire.py", front)
    ui = front / "outer-canary"
    ui.mkdir()
    candidate = (BOARD / "index.html").read_text().replace('href="/style.css"', 'href="/outer-canary/style.css"').replace(
        'src="/app.js"', 'src="/outer-canary/app.js"').replace('<form id="new">', '<form id="new" hidden>')
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
    outer = replace_once(outer, "'发布一个目标，开始第一段航程。'", "'请在普通新单入口提交合成索引试单。'")
    outer = replace_once(outer, "by('map-link').href=chosen?'/?trip='+encodeURIComponent(chosen):'/';", "by('map-link').href='/';")
    write(ui / "outer.js", outer)
    team = (BOARD / "goal_team.js").read_text().replace("'/api/tasks/", "'/api/outer-canary/tasks/").replace(
        "'/api/outer/'", "'/api/outer-canary/'").replace("'/api/outer'", "'/api/outer-canary/drafts'")
    write(ui / "goal_team.js", team)
    for name in ("style.css", "goal_team.css", "task_presentation.js"):
        shutil.copy2(BOARD / name, ui / name)


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
    approval = "                return self.json_response(dispatch(workbench.outer(),'web',cmd,body))"
    server = replace_once(server, approval, """                free_text = command_uses_model_input(cmd, body)
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
                             "        'team_review_mode' in body or body.get('allow_model_input') is True)")
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
