"""构建正式 8765 的合成单灰度叠加层和独立 V2 服务目录。"""
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
<a href="/workbench">查看智能试单</a></div><!--OUTER-CANARY-END--></form></dialog>''')
    tasks = replace_once((formal / "tasks.js").read_text(), """    if (working) return;
    working = true;
    const button = form.querySelector('[type=submit]');""", """    if (working) return;
    if (event.submitter?.id === 'outer-canary-submit') {
      working = true;
      event.submitter.disabled = true;
      try {
        const result = await post('/api/tasks', {workflow:'outer',fixture:'filename-index-v1'});
        const target = new URL('/workbench', location.origin);
        if (result.ticket_id) target.searchParams.set('goal', result.ticket_id);
        else target.searchParams.set('outer_event', result.event_id);
        location.assign(target);
      } catch (error) {
        node('#task-form-status').textContent = error.message;
      } finally {
        working = false;
        event.submitter.disabled = false;
      }
      return;
    }
    working = true;
    const button = form.querySelector('[type=submit]');""")
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
    shutil.copy2(BOARD / "style.css", ui / "style.css")


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
    server = replace_once(server, "            path = urlsplit(self.path).path", """            path = urlsplit(self.path).path
            from outer_canary import SYNTHETIC_ORDER, valid_command
            if path == '/api/tasks':
                if body != SYNTHETIC_ORDER:
                    raise Problem('灰度后台仅接收内置合成索引工单', 409)
            elif not path.startswith('/api/outer/') or not valid_command(path.removeprefix('/api/outer/'), body):
                raise Problem('灰度后台未开放此操作', 400)""")
    approval = "                return self.json_response(dispatch(workbench.outer(),'web',cmd,body))"
    server = replace_once(server, approval, """                if cmd == 'approve':
                    current = workbench.outer().get(body.get('ticket_id'), 'owner')
                    criteria = (current.get('acceptance_contract') or {}).get('criteria', [])
                    if not criteria or any(c.get('verification_type') != 'DETERMINISTIC' for c in criteria):
                        raise Problem('合成索引灰度只接受确定性验收', 409)
                return self.json_response(dispatch(workbench.outer(),'web',cmd,body))""")
    write(app / "server.py", server)
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
