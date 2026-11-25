"""阶段一合成夹具。只在新隔离实例干预真实模型产生的文件或中断输送。"""

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time


CASES = ('verification', 'retained-file', 'budget', 'recovery', 'review-recovery', 'codex-timeout')
RECOVERY_CASES = frozenset(('recovery', 'review-recovery'))
TICKET_NAMES = {
    'verification': '合成网页 · 验收覆盖',
    'retained-file': '合成网页 · 保留项回归',
    'budget': '合成网页 · 返修预算续办',
    'recovery': '合成网页 · Worker 中断恢复',
    'review-recovery': '合成网页 · 返修新版中断续验',
    'codex-timeout': '合成网页 · Codex 超时后续验',
}
TITLE = 'Daily Brief'
HEADING = 'Daily Tasks'
KEEP_HEADING = 'Status Notes'
TASK_ID = 'task-count'
KEEP_ID = 'task-notes'
INITIAL_LIMIT = 4
MARKER = '.goal-team-phase-one'
RUN_BARRIER = 'phase-one-worker-barrier.json'
WORKER_INTERRUPTED = 'phase-one-worker-interrupted.json'


def specs(case):
    path = f'phase-one-{case}.html'
    checks = []
    for heading, element_id in ((HEADING, TASK_ID), (KEEP_HEADING, KEEP_ID)):
        checks.append((path, {'title': TITLE, 'headings': [heading], 'element_ids': [element_id]}))
    return checks


def order(case):
    path = specs(case)[0][0]
    return {
        'name': TICKET_NAMES[case],
        'goal': f'创建合成简报 {path}，网页标题为 Daily Brief，有 Daily Tasks 和 Status Notes 两个一级标题，'
                '以及 id 为 task-count 和 task-notes 的元素。',
        'acceptance': f'验收一：{path} 的标题为 Daily Brief，一级标题 Daily Tasks 出现一次，'
                      f'id 为 task-count 的元素出现一次。\n验收二：{path} 的标题为 Daily Brief，'
                      '一级标题 Status Notes 出现一次，id 为 task-notes 的元素出现一次。',
    }


def checked_contract(case, contract):
    criteria = (contract or {}).get('criteria') or []
    expected = specs(case)
    if len(criteria) != len(expected):
        return False
    return all(any(row.get('required') is True and row.get('verifier_id') == 'html-document'
                   and row.get('verifier_version') == 1 and row.get('test_spec') == {'path': path}
                   and row.get('expected_result') == result for row in criteria)
               for path, result in expected)


def active(root):
    marker = root/MARKER
    if (root.parent != Path('/private/tmp') or not root.name.startswith('autoloop-goal-team-')
            or marker.is_symlink() or not marker.is_file()
            or not (root/'.filename-index-pilot').is_file()):
        raise RuntimeError('阶段一夹具只可用于专用隔离实例')
    case = marker.read_text().strip()
    if case not in CASES:
        raise RuntimeError('阶段一夹具模式不合法')
    return case


def record(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('x') as handle:
        json.dump(value, handle, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    temporary.rename(path)


def assignment_matches(root, case, assignment, workspace):
    if (root/'workbench' not in workspace.resolve().parents or assignment['phase'] != 'execute'
            or assignment['step'] != 0 or not checked_contract(case, assignment.get('outer_acceptance'))):
        return False
    rows = assignment['outer_acceptance']['criteria']
    plan = assignment['plan']['steps']
    if len(plan) != 1 or set(plan[0]['acceptance']) != {row['criterion_id'] for row in rows}:
        raise RuntimeError('阶段一合成工单未把两项验收绑定到同一实施步骤')
    return True


def limit_draft(runtime, ticket, request):
    root = runtime.parent
    if active(root) == 'budget' and request.get('name') == TICKET_NAMES['budget']:
        ticket['approval_terms']['team_role_limit'] = INITIAL_LIMIT


def inject(assignment, workspace, *, retained=False):
    from superagent.outer_verification import _html_result, _StaticHtml, capture

    root = Path(__file__).resolve().parents[2]
    case = active(root)
    if case == 'recovery' or (retained and case != 'retained-file'):
        return
    if not assignment_matches(root, case, assignment, workspace):
        return
    output = root / ('phase-one-retained-fault.json' if retained else 'phase-one-initial-fault.json')
    if output.exists():
        return
    relative = specs(case)[0][0]
    before, contents, _ = capture(workspace)
    initial = contents.get(relative, b'')
    if retained:
        pattern = rb'''(?i)(?<![\w:-])(id\s*=\s*)(["']?)task-notes\2(?=[\s/>])'''
        modified, count = re.subn(pattern, rb'\g<1>\g<2>draft-notes\g<2>', initial)
        expected_status = ['PASS', 'FAIL']
    else:
        count = 0

        def replace_heading(match):
            nonlocal count
            parser = _StaticHtml()
            parser.feed(match[0].decode('utf-8'))
            parser.close()
            if parser.headings != [HEADING]:
                return match[0]
            count += 1
            return match[1] + b'Draft Tasks' + match[2]

        modified = re.sub(rb'(<h1\b[^>]*>).*?(</h1\s*>)', replace_heading, initial, flags=re.I | re.S)
        expected_status = ['FAIL', 'PASS']
    previous = [_html_result(initial, result)[0] for _, result in specs(case)]
    actual = [_html_result(modified, result)[0] for _, result in specs(case)]
    if previous != ['PASS', 'PASS'] or count != 1 or actual != expected_status:
        raise RuntimeError('实际候选不满足阶段一故障注入的前置条件')
    (workspace/relative).write_bytes(modified)
    after = capture(workspace)[0]
    record(output, {'ticket': assignment['ticket'], 'assignment': assignment['id'], 'run': assignment['run'],
                    'path': relative, 'before': before, 'after': after,
                    'checks_before': previous, 'checks_after': actual})


def after_writable(assignment, workspace):
    root = Path(__file__).resolve().parents[2]
    initial = root/'phase-one-initial-fault.json'
    if not initial.exists():
        inject(assignment, workspace)
    elif json.loads(initial.read_text())['assignment'] == assignment['id']:
        inject(assignment, workspace, retained=True)


def hold_before_receipt(assignment, directory, report):
    root = Path(__file__).resolve().parents[2]
    case = active(root)
    marker = root/RUN_BARRIER
    if case not in RECOVERY_CASES:
        raise RuntimeError('只支持专用恢复夹具中断 Worker')
    if (not assignment_matches(root, case, assignment, directory/'sealed') or marker.exists()
            or report.get('error') or not report.get('checkpoint')):
        return
    pending_review = isinstance(report.get('repair_review_pending'), dict)
    if (case == 'review-recovery') != pending_review:
        return
    recovery = directory/'candidate-recovery.json'
    if not recovery.is_file() or (directory/'report.json').exists():
        raise RuntimeError('实际恢复凭证未在业务回执之前落盘')
    data = recovery.read_bytes()
    if pending_review:
        from superagent.outer_verification import capture

        before = json.loads((root/'phase-one-initial-fault.json').read_text())
        if (before['ticket'] != assignment['ticket'] or before['after']['digest'] == report['checkpoint']['digest']
                or json.loads(data).get('report') != report
                or capture(directory/'sealed')[0] != report['checkpoint']):
            raise RuntimeError('耐久回执与执行者刚写好的封存新版不同')
    record(marker, {'ticket': assignment['ticket'], 'assignment': assignment['id'], 'run': assignment['run'],
                    'command_pid': os.getpid(), 'command_group': os.getpgrp(), 'case':case,
                    'checkpoint': report['checkpoint']['digest'], 'repair_review_pending':pending_review,
                    'recovery_sha256': hashlib.sha256(data).hexdigest()})
    # 调度器启动器在此屏障真正杀停归属明确的进程组。
    time.sleep(30)
    raise RuntimeError('隔离 Worker 中断未在屏障内观察到')


def parent_pid(pid):
    value = subprocess.check_output(['/bin/ps', '-o', 'ppid=', '-p', str(pid)], text=True, timeout=4)
    return int(value.strip())


def descendant(pid, ancestor):
    for _ in range(6):
        pid = parent_pid(pid)
        if pid == ancestor:
            return True
        if pid <= 1:
            return False
    return False


def interrupt_worker(pilot):
    root = pilot.root
    marker, outcome = root/RUN_BARRIER, root/WORKER_INTERRUPTED
    if not marker.is_file() or outcome.exists():
        return
    case = active(root)
    if case not in RECOVERY_CASES:
        raise RuntimeError('只能中断专用恢复夹具的 Worker')
    data = json.loads(marker.read_text())
    if data.get('case') != case:
        raise RuntimeError('Worker 屏障不属于本次隔离恢复场景')
    worker = pilot.worker_process
    command, group = data['command_pid'], data['command_group']
    if (worker.poll() is not None or group == worker.pid or group == os.getpgrp()
            or os.getpgid(command) != group or os.getpgid(worker.pid) != worker.pid
            or not descendant(command, group) or not descendant(group, worker.pid)):
        raise RuntimeError('隔离 Worker 与派工子进程的归属核对失败')
    try:
        os.killpg(worker.pid, signal.SIGKILL)
        os.killpg(group, signal.SIGKILL)
        status = worker.wait(timeout=5)
    except ProcessLookupError as error:
        raise RuntimeError('中断前隔离 Worker 或派工进程已经退出') from error
    if status != -signal.SIGKILL:
        raise RuntimeError('未观察到隔离 Worker 的真实 SIGKILL 退出')
    pilot.worker_process = pilot.start_worker()
    record(outcome, {**data, 'worker_before': worker.pid, 'worker_after': pilot.worker_process.pid,
                     'worker_exit': status, 'signal': 'SIGKILL'})
