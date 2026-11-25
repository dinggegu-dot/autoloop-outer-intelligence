"""保留合成隔离工作台，直到操作者结束本次试用。"""
import argparse
from contextlib import ExitStack
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from deploy_formal_canary import read_recovery
from prepare_formal_outer_pilot import source_only
from live_filename_index import Isolated, deployed_listener_pids, execute_trial, save_json
from goal_team_phase_one_fixture import CASES as PHASE_ONE_CASES, MARKER as PHASE_ONE_MARKER, RECOVERY_CASES, interrupt_worker


def label_browser_fixture_operator(copy):
    # 仅在明确指定的合成副本标记 UI 操作者，正常会话及 CSRF 检查在这行之前完成。
    server = copy/'server.py'
    content = server.read_text()
    original = "operator_kind.set('acceptance_test' if provided else 'owner')"
    if content.count(original) != 1:
        raise RuntimeError('试验副本未找到唯一操作身份接点')
    content = content.replace(original, "operator_kind.set('acceptance_test')", 1)
    compile(content, str(server), 'exec')
    server.write_text(content)


def enable_inner_repair_fixture(copy):
    runner = copy/'superagent/goal_team_run.py'
    content = runner.read_text()
    original = "        result = inspect('打开实际候选文件，亲验并记录依据。"
    if content.count(original) != 1:
        raise RuntimeError('试验副本未找到唯一的内层执行候选接点')
    hook = ('        from superagent.inner_repair_fixture import inject\n'
            '        inject(assignment, workspace)\n')
    patched = content.replace(original, hook + original, 1)
    compile(patched, str(runner), 'exec')
    helper = Path(__file__).with_name('goal_team_inner_repair_fixture.py')
    data = helper.read_bytes()
    destination = runner.with_name('inner_repair_fixture.py')
    compile(data, str(destination), 'exec')
    with destination.open('xb') as handle:
        handle.write(data)
    runner.write_text(patched)


def enable_phase_one_fixture(copy, case):
    def insert(module, anchor, hook):
        target = copy/'superagent'/module
        content = target.read_text()
        if content.count(anchor) != 1:
            raise RuntimeError(f'阶段一试验副本未找到唯一接点 {module}')
        patched = content.replace(anchor, hook + anchor, 1)
        compile(patched, str(target), 'exec')
        target.write_text(patched)

    module = Path(__file__).with_name('goal_team_phase_one_fixture.py')
    shutil.copyfile(module, copy/'superagent'/module.name)
    if case == 'recovery':
        insert('goal_runtime.py', '            save_json(report_path,report)\n',
               '            from superagent.goal_team_phase_one_fixture import hold_before_receipt\n'
               '            hold_before_receipt(assignment, attempt_dir, report)\n')
        return
    if case == 'review-recovery':
        insert('goal_runtime.py', '\n\n\ndef read_team_recovery(',
               '\n    if report.get("repair_review_pending"):\n'
               '        from superagent.goal_team_phase_one_fixture import hold_before_receipt\n'
               '        hold_before_receipt(assignment, directory, report)')
    insert('goal_team_run.py', "            result = output['result'] if response_schema else validated_result(output)\n",
           '            if writable:\n'
           '                from superagent.goal_team_phase_one_fixture import after_writable\n'
           '                after_writable(assignment, workspace)\n')
    if case == 'codex-timeout':
        helper = Path(__file__).with_name('goal_team_timeout_fixture.py')
        shutil.copyfile(helper, copy/'superagent'/helper.name)
        insert('goal_team_run.py', '            output = invoke(model=',
               '            from superagent.goal_team_timeout_fixture import role_invoke\n'
               '            invoke = role_invoke(assignment)\n')
    if case == 'budget':
        anchor = "                ticket['approval_terms']['execution_profile'] = PROFILE\n"
        target = copy/'superagent'/'outer_intake.py'
        content = target.read_text()
        if content.count(anchor) != 1:
            raise RuntimeError('阶段一试验副本未找到唯一授权条款接点')
        patched = content.replace(anchor, anchor +
            '                from superagent.goal_team_phase_one_fixture import limit_draft\n'
            '                limit_draft(self.store.root, ticket, request)\n', 1)
        compile(patched, str(target), 'exec')
        target.write_text(patched)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--formal-source', required=True, type=Path)
    parser.add_argument('--formal-canary-recovery', type=Path, help='现役灰度安装前的原入口源码恢复点')
    parser.add_argument('--codex-home', type=Path, help='已获授权的本机隔离 Codex 配置目录')
    parser.add_argument('--proxy', type=int, help='已获授权的本机回环代理端口')
    parser.add_argument('--workbench-only', action='store_true', help='只启动工作台并通过 Codex 执行')
    fixture_args = parser.add_mutually_exclusive_group()
    fixture_args.add_argument('--fixture', choices=('control', 'regression', 'pause-stale', 'nonterminal-regression', 'preference-race'),
                        help='通过隔离接口另起合成验收单，记录实测证据')
    fixture_args.add_argument('--browser-fixture', action='store_true',
                              help='独立 Chrome 合成试验，所有界面操作明确标为试验操作员')
    fixture_args.add_argument('--phase-one-fixture', choices=PHASE_ONE_CASES,
                              help='隔离验证真实候选、保留项、预算续办或 Worker 中断')
    parser.add_argument('--inject-inner-repair', action='store_true',
                        help='在指定合成 HTML 候选的执行与负责人亲验之间注入一次错误')
    args = parser.parse_args()
    if args.inject_inner_repair and not args.browser_fixture:
        parser.error('--inject-inner-repair 只可与 --browser-fixture 同用')
    if args.proxy is not None and not 1 <= args.proxy <= 65535:
        parser.error('--proxy 必须是有效的本机端口')
    if args.phase_one_fixture and args.codex_home is None:
        parser.error('阶段一实测须使用指定的独立 Codex 配置目录')
    repo = Path(__file__).resolve().parents[1]
    root = Path(tempfile.mkdtemp(prefix='autoloop-goal-team-', dir='/private/tmp'))
    print(f'隔离目录 {root}', flush=True)
    before = deployed_listener_pids()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    manifest = {'formal_listener_before': before, 'phase': 'preparing'}
    try:
        with ExitStack() as stack:
            code = root/'formal-board'
            formal = args.formal_source.resolve(strict=True)
            if args.formal_canary_recovery:
                originals = read_recovery(args.formal_canary_recovery.resolve(strict=True))
                copied = root/'formal-source'
                shutil.copytree(formal, copied, ignore=source_only(formal))
                for name in ('server.py', 'index.html', 'tasks.js'):
                    (copied/name).write_bytes(originals[name][0])
                formal = copied
            subprocess.run([sys.executable, '-B', str(repo/'scripts/prepare_formal_outer_pilot.py'),
                            str(formal), str(code)], check=True, timeout=40)
            if args.browser_fixture or args.phase_one_fixture:
                label_browser_fixture_operator(code)
            if args.inject_inner_repair:
                enable_inner_repair_fixture(code)
            pilot = Isolated(code, root, stack)
            if args.phase_one_fixture:
                enable_phase_one_fixture(code, args.phase_one_fixture)
                marker = root/PHASE_ONE_MARKER
                marker.write_text(args.phase_one_fixture + '\n')
                marker.chmod(0o600)
            if args.codex_home:
                codex_home = args.codex_home.resolve(strict=True)
                if not codex_home.is_dir() or Path('/private/tmp') not in codex_home.parents:
                    parser.error('请选择独立临时 Codex 配置目录')
                role_home = root/'role-home'
                role_home.mkdir(mode=0o700)
                pilot.env.update(CODEX_HOME=str(codex_home), HOME=str(role_home))
            if args.proxy is not None:
                pilot.env['AUTOLOOP_OUTER_PROXY_PORT'] = str(args.proxy)
            token = None
            if not (args.workbench_only or args.inject_inner_repair or args.phase_one_fixture):
                token = secrets.token_hex(32)
                (root/'dsh-bridge-token').write_text(token + '\n')
                (root/'dsh-bridge-token').chmod(0o600)
            trial_token = secrets.token_hex(32)
            operator_key_file = root/'trial-operator-token'
            operator_key_file.write_text(trial_token + '\n')
            operator_key_file.chmod(0o600)
            bridge = {'dsh_token': token} if token else {}
            board = pilot.launch(args.fixture or 'control', goal_team=True, trial_token=trial_token, **bridge)
            capability = board.call('/api/outer')
            if capability.get('goal_team_enabled') is not True:
                raise RuntimeError('试点未启用目标模式')
            manifest.update(phase='ready', board=board.url, hub_port=pilot.hub_port,
                            workbench=str(pilot.runtime), instance=str(root),
                            browser_fixture=bool(args.browser_fixture or args.phase_one_fixture),
                            inject_inner_repair=args.inject_inner_repair, phase_one_fixture=args.phase_one_fixture,
                            workbench_only=token is None)
            save_json(root/'instance.json', manifest)
            print(f'隔离工作台 {board.url}/#tasks', flush=True)
            if args.fixture:
                evidence = {'synthetic':True, 'entry':'普通新单业务 API', 'mode':args.fixture,
                            'runtime':'codex-app-server', 'passed':False}
                try:
                    if args.fixture == 'nonterminal-regression':
                        from live_goal_team_three_stage import execute
                        execute(pilot, board, evidence)
                    elif args.fixture == 'preference-race':
                        from live_goal_team_preference_race import execute
                        execute(pilot, board, evidence)
                    else:
                        execute_trial(pilot, board, evidence, args.fixture)
                    evidence['passed'] = True
                finally:
                    save_json(root/'acceptance.json', evidence)
                return
            coordinator = pilot.start([sys.executable, '-B', str(code/'superagent/goal_runtime.py'), 'serve',
                                       '--runtime', str(pilot.runtime), '--hub', str(pilot.hub)], 'coordinator')
            while pilot.worker_process.poll() is None and coordinator.poll() is None:
                if args.phase_one_fixture in RECOVERY_CASES:
                    interrupt_worker(pilot)
                time.sleep(.5)
            raise RuntimeError('隔离 Worker 或工单调度器已退出')
    except KeyboardInterrupt:
        pass
    finally:
        manifest.update(phase='stopped', formal_listener_after=deployed_listener_pids())
        save_json(root/'instance.json', manifest)


if __name__ == '__main__':
    main()
