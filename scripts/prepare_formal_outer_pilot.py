"""在空目录构建正式工作台副本，加入仓库中已版本化的 V2 试点入口。"""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

REPO = Path(__file__).resolve().parents[1]
BOARD = REPO / '网页工作台'
BASE = '0507674'
CONFLICT = re.compile(r'^<<<<<<< FORMAL\n(.*?)^=======\n(.*?)^>>>>>>> PILOT\n', re.M | re.S)


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f'正式工作台入口已变化，请重新核对：{old.splitlines()[0][:100]}')
    return text.replace(old, new, 1)


def merge(formal, name, scratch):
    base = scratch / name
    base.write_bytes(subprocess.check_output(['git', 'show', f'{BASE}:网页工作台/{name}'], cwd=REPO))
    result = subprocess.run(['git', 'merge-file', '-p', '-L', 'FORMAL', '-L', 'BASE', '-L', 'PILOT',
                             str(formal / name), str(base), str(BOARD / name)],
                            capture_output=True, text=True)
    if not 0 <= result.returncode < 127:
        raise ValueError(result.stderr or f'无法合并 {name}')
    return result.stdout


def resolve_server(match):
    formal, pilot = match.groups()
    if "path in ('/api/library/search'" in formal and "path.startswith('/api/outer/')" in pilot:
        marker = '            workbench = self.server.workbench\n'
        return pilot[:pilot.index(marker)] + formal + pilot[pilot.index(marker) + len(marker):]
    if "action=='research-followup'" in formal and "get('outer')" in pilot:
        return pilot + formal
    if "'/api/library/archives'" in formal and "if route == '/api/outer'" in pilot:
        return pilot[:pilot.index("        if route.startswith('/api/tasks'):")] + formal
    if "files = {'/': 'index.html'" in formal and "'cover.html'" in pilot:
        anchor = '        if route not in files:'
        entries = """        if getattr(self.server.workbench, 'outer_enabled', False):
            files.update({'/workbench':'outer-ui/index.html', '/outer-ui/app.js':'outer-ui/app.js',
                          '/outer-ui/style.css':'outer-ui/style.css', '/outer-ui/outer.js':'outer-ui/outer.js',
                          '/outer-ui/intake_dialog.js':'intake_dialog.js'})
"""
        return replace_once(formal, anchor, entries + anchor)
    if 'server.research_canvas_vault' in formal and 'outer_enabled=args.outer_intelligence' in pilot:
        anchor = '        server.research_canvas_vault'
        return pilot[:pilot.index('        server.odyssey_assets')] + formal[formal.index(anchor):] + '        server.afr_panel = afr_panel\n'
    raise ValueError('正式工作台服务有待人工合并的冲突')


def resolve_workbench(match):
    formal, pilot = match.groups()
    if "task.get('workflow')=='research'" in formal and 'omit_call_payloads(task)' in pilot:
        return pilot + formal
    raise ValueError('正式工作台数据库代码有待人工合并的冲突')


def source_only(formal):
    root_dirs = {'assets', 'superagent', 'vendor'}
    root_extensions = {'.py', '.js', '.css', '.html'}
    asset_extensions = {'.png', '.jpg', '.jpeg', '.webp', '.svg', '.gif'}
    # 历史快照入口会读取现役中台。隔离副本不携带它或其离线数据。
    excluded = {'legacy_snapshot.py'}

    def ignore(directory, names):
        path = Path(directory)
        relative = path.relative_to(formal)
        ignored = set()
        for name in names:
            entry = path / name
            if name.startswith('.') or name in excluded or entry.is_symlink():
                ignored.add(name)
                continue
            if not relative.parts:
                allowed = name in root_dirs if entry.is_dir() else entry.suffix in root_extensions
            elif relative.parts[0] == 'assets':
                allowed = entry.is_dir() or entry.suffix in asset_extensions
            elif relative.parts == ('vendor',):
                allowed = entry.is_file() and (entry.suffix == '.js' or name == 'THREE-LICENSE.txt')
            else:
                allowed = entry.is_file() and entry.suffix == '.py' and not name.startswith('test_')
            if not allowed:
                ignored.add(name)
        return ignored

    return ignore


def prepare(formal, target):
    formal, target = formal.resolve(strict=True), target.resolve()
    if target.exists() or formal in target.parents or target in formal.parents:
        raise ValueError('请选择与正式工作台独立且尚未创建的目标目录')
    for name in ('server.py', 'workbench.py', 'index.html', 'tasks.js'):
        entry = formal / name
        if entry.is_symlink() or not entry.is_file():
            raise ValueError(f'正式工作台源码缺失或包含链接：{name}')
    with tempfile.TemporaryDirectory() as temp:
        scratch = Path(temp)
        server = CONFLICT.sub(resolve_server, merge(formal, 'server.py', scratch))
        workbench = CONFLICT.sub(resolve_workbench, merge(formal, 'workbench.py', scratch))
        if any('<<<<<<<' in content for content in (server, workbench)):
            raise ValueError('存在未解决的源码合并冲突')
        server = replace_once(server, '        if not file.is_file():',
                              '        file = ROOT / files[route]\n        if not file.is_file():')
        server = replace_once(server, "default=(ROOT/'assets/odyssey' if (ROOT/'assets/odyssey').is_dir() else None)", 'default=None')
        server = replace_once(server, '    if args.snapshot:\n        if not LEGACY_BOARD:',
                              '''    if not args.outer_intelligence:
        parser.error('正式工作台源码副本是隔离智能工单试点，请启用智能接单')
    if args.outer_intelligence:
        runtime, hub = args.runtime.resolve(), args.hub_home.resolve()
        marker = runtime.parent / '.goal-pilot'
        if (args.port == 8765 or runtime.name != 'workbench' or hub != runtime.parent / 'hub'
                or not marker.is_file() or marker.is_symlink()
                or Path('/private/tmp') not in runtime.parents):
            parser.error('正式工作台副本须使用隔离启动器目录及不同于 8765 的端口')
        args.runtime, args.hub_home = runtime, hub
        args.outer_workbench_entry = True
    if args.snapshot:
        if not LEGACY_BOARD:''')
        html = replace_once((formal / 'index.html').read_text(), '<p class="draft-warning" id="task-form-status">',
                            '<label id="outer-opt-in" hidden><input type="checkbox" name="workflow" value="outer">使用智能工单试点</label><p class="draft-warning" id="task-form-status">')
        tasks = (formal / 'tasks.js').read_text()
        tasks = replace_once(tasks, "    if(task.workflow==='research')return researchDetail(task);",
                             """    if(task.goal_execution?.outer){
      const href='/workbench?task='+encodeURIComponent(task.id)+'&goal='+encodeURIComponent(task.id);
      return `<article class="wb-detail mission-workspace"><h2>${html(task.name)}</h2><p>${html(task.status_label)}</p><a class="primary-button" href="${href}">查看本工单的授权、执行与交付证据</a></article>`;
    }
    if(task.workflow==='research')return researchDetail(task);""")
        tasks = replace_once(tasks, "      if (state?.capability?.goal_enabled) body.workflow='goal';",
                             """      if(!body.workflow && state?.capability?.goal_enabled && !state.capability.outer_enabled) body.workflow='goal';""")
        tasks = replace_once(tasks, "      const result = await post('/api/tasks', body);",
                             """      const result = await post('/api/tasks', body);
      if(result.workflow==='outer'){
        const url=new URL('/workbench',location.origin);
        if(result.ticket_id)url.searchParams.set('goal',result.ticket_id);
        else url.searchParams.set('outer_event',result.event_id);
        location.assign(url);
        return;
      }""")
        tasks = replace_once(tasks, "    body.role_models={plan:body.model_plan,execute:body.model_execute,review:body.model_review};",
                             """    if(body.workflow==='outer'){
      for(const key of ['model_plan','model_execute','model_review','execution_mode','budget_minutes'])delete body[key];
    } else {
      body.role_models={plan:body.model_plan,execute:body.model_execute,review:body.model_review};""")
        tasks = replace_once(tasks, "    if(body.execution_mode === 'auto')body.budget_minutes=60;",
                             "    if(body.execution_mode === 'auto')body.budget_minutes=60;\n    }")
        tasks = replace_once(tasks, "  const modeInput=node('#task-mode')",
                             """  const outerToggle=node('#outer-opt-in');
  const approvalSettings=form.querySelector('.model-fields');
  const syncPilot=()=>{
    const enabled=Boolean(state?.capability?.outer_workbench_entry);
    outerToggle.hidden=!enabled;
    const pilot=enabled&&form.elements.workflow.checked;
    approvalSettings.hidden=pilot;
    for(const id of ['task-mode','task-budget']){
      node('#'+id).hidden=pilot;
      node('label[for="'+id+'"]').hidden=pilot;
    }
    form.querySelector('.create-options .draft-warning').hidden=pilot;
  };
  form.addEventListener('change',syncPilot);
  form.addEventListener('reset',()=>setTimeout(syncPilot,0));
  const modeInput=node('#task-mode')""")
        tasks = replace_once(tasks, "  function render() {", "  function render() {\n    syncPilot();")
        for name, content in [('server.py', server), ('workbench.py', workbench)]:
            compile(content, name, 'exec')
        shutil.copytree(formal, target, ignore=source_only(formal))
        for name, content in [('server.py', server), ('workbench.py', workbench), ('index.html', html), ('tasks.js', tasks)]:
            (target / name).write_text(content)
        for name in ('outer_channels.py', 'outer_workbench.py', 'afr_panel.py', 'afr.html', 'afr.js'):
            shutil.copy2(BOARD / name, target / name)
        for source in (BOARD / 'superagent').glob('*.py'):
            if not source.name.startswith('test_'):
                shutil.copy2(source, target / 'superagent' / source.name)
        ui = target / 'outer-ui'
        ui.mkdir()
        for name in ('app.js', 'style.css', 'outer.js'):
            shutil.copy2(BOARD / name, ui / name)
        outer_page = (BOARD / 'index.html').read_text().replace('href="/style.css"', 'href="/outer-ui/style.css"').replace('src="/app.js"', 'src="/outer-ui/app.js"')
        (ui / 'index.html').write_text(outer_page)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('formal_source', type=Path)
    parser.add_argument('empty_target', type=Path)
    args = parser.parse_args()
    try:
        prepare(args.formal_source, args.empty_target)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
