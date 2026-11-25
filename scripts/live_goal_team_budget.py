"""F9：在私有独立试点验证角色额度、用量缺报和一次新授权。"""
import argparse
import hashlib
import inspect
import json
import os
from contextlib import ExitStack
from pathlib import Path
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode
import uuid

from deploy_formal_canary import read_recovery
from live_filename_index import Isolated, require, save_json, wait_until
from prepare_formal_outer_pilot import source_only


EXPECTED = {
    'first.json': {'value': 13},
    'second.json': {'source_value': 13, 'double': 26},
}
FIXTURE = {
    'name': '两阶段计数 · F9 额度试验操作员',
    'goal': (
        '按顺序做两个有依赖的合成阶段，给每阶段各一个独立的 JSON 验收项。'
        '阶段一只生成 first.json，记录输入 13。等待其机器验收通过后，'
        '第二阶段从 first.json 读取 13 并生成 second.json，记下原值和翻倍后结果。'
        '将两个阶段分别编入实施计划，一阶段验一份 JSON。保留 first.json 的已验字节。'
    ),
    'acceptance': (
        '阶段一 first.json 解析为 JSON 时须精确等于 {"value":13}。'
        '阶段二 second.json 解析为 JSON 时须精确等于 {"source_value":13,"double":26}。'
        '只有这两项机器验收，优先选 json-artifact v1；无需人工签收。'
    ),
}
BORROWED_PORTS = {8765, 60966, 61498}
USAGE_PROBE = '''
    if (runtime.parent / ".f9-budget-test").is_file():
        from superagent.goal_app_server import _Turn
        actual_assignment = goal_runtime.execute_assignment
        def observe_usage(store, assignment, directory, verification):
            if assignment["phase"] != "execute" or assignment["step"] != 0:
                return actual_assignment(store, assignment, directory, verification)
            collect = _Turn.collect
            suppressed = 0
            def collect_without_usage(turn, event):
                if (event.get("method") == "thread/tokenUsage/updated"
                        and (event.get("params") or {}).get("threadId") == turn.thread):
                    nonlocal suppressed
                    suppressed += 1
                    return
                return collect(turn, event)
            _Turn.collect = collect_without_usage
            try:
                return actual_assignment(store, assignment, directory, verification)
            finally:
                _Turn.collect = collect
                save_json(runtime.parent / "usage-suppression.json",
                          {"assignment": assignment["id"], "count": suppressed})
        goal_runtime.execute_assignment = observe_usage
'''


def fixture_intake_model(role, context, *, expected, goal):
    reference = context['event_id']
    if role == 'intake_assessor':
        if 'CREATE_DRAFT' not in context['available_actions']:
            raise ValueError('F9 intake fixture applies to one new ticket')
        deliverables = [
            {'description': f'生成并独立机器检查 {name}，约定内容 {json.dumps(contents)}',
             'source': 'USER_EXPLICIT', 'source_refs': [reference]}
            for name, contents in expected.items()
        ]
        return {
            'schema_version': 1, 'event_id': reference, 'input_snapshot_revision': context['snapshot_revision'],
            'intent': 'message', 'relation_type': 'none', 'candidate_ticket_ids': [], 'relation_evidence_refs': [reference],
            'goal_draft': {'goal_statement': goal, 'deliverables': deliverables, 'required_capabilities': [],
                           'nonfunctional_requirements': [], 'constraints': [], 'assumptions': [], 'out_of_scope': []},
            'assumptions': [], 'critical_ambiguities': [],
            'proposed_question': {'question_text': '', 'options': [], 'reason': '', 'blocking_scope': ''},
            'suggested_action': 'CREATE_DRAFT', 'reason': '合成 F9 接单夹具。',
        }
    if role != 'acceptance_designer':
        raise ValueError('F9 only replaces intake and acceptance design')
    criteria = [
        {'criterion_id': f'f9-stage-{index}', 'requirement_ids': [item['requirement_id']],
         'description': f'独立核对 {name} 的 JSON 内容', 'required': True, 'verification_type': 'DETERMINISTIC',
         'verifier_id': 'json-artifact', 'verifier_version': 1, 'test_spec_json': json.dumps({'path': name}),
         'expected_result_json': json.dumps(expected), 'evidence_required': ['structured_output'],
         'human_review_required': False, 'source_refs': [reference]}
        for index, ((name, expected), item) in enumerate(zip(expected.items(), context['goal_contract']['deliverables']), 1)
    ]
    return {'schema_version': 1, 'ticket_id': context['ticket_id'], 'goal_revision': context['goal_revision'],
            'artifact_types': ['data'], 'criteria': criteria, 'verification_gaps': [],
            'human_review_requirements': [], 'source_refs': [reference]}


def install_fixture_intake(root, code):
    module = code/'superagent/outer_model.py'
    require((root/'.f9-budget-test').is_file() and root.parent == Path('/private/tmp'),
            '只可在本次私有源码副本替换接单模型')
    source = module.read_text()
    fixture = inspect.getsource(fixture_intake_model)
    module.write_text(source + f'\n\n_F9_EXPECTED = {EXPECTED!r}\n_F9_GOAL = {FIXTURE["goal"]!r}\n\n' + fixture + '''

def _f9_intake(self, role, context, output_schema):
    return {'result': fixture_intake_model(role, context, expected=_F9_EXPECTED, goal=_F9_GOAL), 'model': 'synthetic-intake-fixture'}

CodexOuterModel.__call__ = _f9_intake
''')


def request_id():
    return str(uuid.uuid4())


def fixture_role_limit(pilot, active):
    """在试验员批准前改变合成草案的上限，保持正式默认额度及批准接口。"""
    require((pilot.root/'.f9-budget-test').is_file() and pilot.root.parent == Path('/private/tmp'),
            '额度注入仅限本次新建的合成隔离试点')
    with sqlite3.connect(pilot.runtime/'tasks.sqlite3') as db:
        db.execute('BEGIN IMMEDIATE')
        (body,) = db.execute('SELECT body FROM outer_tickets WHERE id=?', (active['id'],)).fetchone()
        version = json.loads(body)
        require(version['status'] == 'AWAITING_APPROVAL' and version['revision'] == active['revision'],
                '额度夹具的批准版本已变化')
        require(version.get('task_id') is None and version.get('mandate') is None,
                '只能给未授权草案配置合成限制')
        require(version['approval_terms']['execution_profile']['max_roles_per_ticket'] == 30,
                '基础团队配置不可被试验改写')
        version['approval_terms']['team_role_limit'] = 1
        db.execute('UPDATE outer_tickets SET body=? WHERE id=?', (json.dumps(version, ensure_ascii=False), active['id']))


def isolate_usage_observation(pilot):
    driver = pilot.root/'worker-driver.py'
    entry = '    goal_runtime.worker(Store(runtime), None)'
    source = driver.read_text()
    require(source.count(entry) == 1, '隔离 Worker 驱动入口已变化')
    driver.write_text(source.replace(entry, USAGE_PROBE + entry))


def validate_contract(ticket):
    selected = {}
    for criterion in ticket['acceptance_contract']['criteria']:
        path = (criterion.get('test_spec') or {}).get('path')
        require(path in EXPECTED and path not in selected and criterion['required'] is True
                and criterion['verifier_id'] == 'json-artifact' and criterion['verifier_version'] == 1
                and criterion['expected_result'] == EXPECTED[path],
                '合成草案的可执行验收不匹配，停止批准')
        selected[path] = criterion['criterion_id']
    require(set(selected) == set(EXPECTED), '合成草案须包含两项确定性 JSON 验收')
    return [[selected[name]] for name in EXPECTED]


def evidence_role(record):
    return {name: record.get(name) for name in (
        'role', 'requested_model', 'model', 'effort', 'sandbox', 'status', 'usage', 'usage_source',
    )}


def check_usage_replay(record):
    """以协议格式回放已有快照，检验累计量更新不会叠加。"""
    from superagent.goal_app_server import _TOKEN_FIELDS, _Turn

    observed = record.get('usage')
    if observed is None:
        return {'replayed': False, 'reason': '第二轮未上报用量'}
    wire = {field: observed[key] for field, key in _TOKEN_FIELDS.items() if key in observed}
    evidence = {'session': record['session'], 'usage': None}
    turn = _Turn(None, evidence, record['model'], record['sandbox'])
    event = {'method': 'thread/tokenUsage/updated', 'params': {
        'threadId':record['session'], 'turnId':record['turn'], 'tokenUsage':{'total':wire},
    }}
    turn.collect(event)
    first = evidence['usage']
    turn.collect(event)
    require(first == evidence['usage'] == observed, '同一用量快照不得重复结算')
    return {'replayed': True, 'receipt_count': 2, 'total_tokens_unchanged': True,
            'scope': '根据实测报告值重建协议快照，在本地回放'}


def check_completed_role(pilot, ticket_id, assignment):
    directory = pilot.runtime/'goal-attempts'/ticket_id/assignment['id']
    process = json.loads((directory/'role-1/process.json').read_text())
    require(process['state'] == 'stopped', '真实角色进程结束状态未核对')
    timing = assignment['report']['evidence']
    elapsed = (timing['ended_ns'] - timing['started_ns']) / 1e9
    require(0 < elapsed <= assignment['max_seconds'], '角色所在派工实际用时应在指派额度内')
    return {'wall_seconds': round(elapsed, 2), 'assignment_limit_seconds': assignment['max_seconds'],
            'process_state': process['state']}


def run_case(pilot, board, outcome, timeout):
    submission = board.call('/api/tasks', {**FIXTURE, 'workflow': 'outer', 'request_id': request_id()}, status=201)

    def ready():
        event = board.call('/api/outer/event', {'event_id': submission['event_id']})
        if not event.get('ticket_id'):
            if event['state'] in ('NEEDS_ATTENTION', 'REJECTED', 'FAILED'):
                raise RuntimeError('接单未生成合成原单')
            return None
        item = board.call('/api/outer/get', {'ticket_id': event['ticket_id']})
        if item['status'] in ('VERIFICATION_GAP', 'NEEDS_ATTENTION', 'AWAITING_CLARIFICATION'):
            raise RuntimeError(f"合成工单需要人工核对：{item['status']}")
        return item if item['status'] == 'AWAITING_APPROVAL' else None

    awaiting = wait_until(ready, 245, '接单审核没有进入待批准')
    ticket_id = awaiting['id']
    ref = {'ticket_id': ticket_id}
    outcome['ticket_ref_sha256'] = hashlib.sha256(ticket_id.encode()).hexdigest()
    print('合成工单已准备批准', flush=True)
    checks = validate_contract(awaiting)
    fixture_role_limit(pilot, awaiting)
    approved = board.call('/api/outer/approve', {
        **ref, 'revision': awaiting['revision'], 'goal_revision': awaiting['goal_revision'],
        'acceptance_revision': awaiting['acceptance_revision'], 'request_id': request_id(),
    })
    mandate = approved['mandate']
    require(mandate['team_role_limit'] == 1 and mandate['operator_kind'] == 'acceptance_test',
            '试验批准未记录单轮上限和试验员身份')
    outcome['initial_authorization'] = {
        'contract_approvals': 1, 'revision': mandate['revision'], 'role_limit': mandate['team_role_limit'],
        'operator_kind': mandate['operator_kind'],
    }

    sys.path.insert(0, str(pilot.app))
    from superagent.goal_runtime import GoalRuntime
    from workbench import Hub, Store
    runtime = GoalRuntime(Store(pilot.runtime), Hub(pilot.hub))
    latest = None

    def until_status(expected):
        nonlocal latest
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            task = runtime.tick(ticket_id)
            actual = task['status']
            state = task['goal_execution']
            stage = (actual, state['stage'], task['step'])
            if stage != latest:
                print(f'原单状态 {stage}', flush=True)
                latest = stage
            if task['plan']:
                require([step['acceptance'] for step in task['plan']['steps']] == checks,
                        '真实计划没有遵照约定的两个阶段')
            if actual == expected:
                return task
            if actual in ('blocked','budget_exhausted','paused','rejected','completed','awaiting_artifact_review'):
                raise RuntimeError(f'工单进入预期外状态 {actual}：{task.get("error","")}')
            time.sleep(.7)
        raise TimeoutError(f'真实 Worker 未在试验时限达到 {expected}')

    exhausted = until_status('budget_exhausted')
    state_before = exhausted['goal_execution']
    blocked = board.call('/api/outer/get', ref)
    require(blocked['execution']['status'] == 'budget_exhausted' and exhausted['step'] == 1
            and '角色调用额度' in exhausted['error'], '第一阶段通过后，第二阶段按角色额度停下')
    assignments = state_before['assignments']
    require([a['phase'] for a in assignments] == ['plan','execute','review']
            and all(a['state'] == 'settled' for a in assignments),
            '额度耗尽前只能登记规划、第一阶段执行、第一阶段复验')
    first, = blocked['execution']['team_activity']
    require(first['usage'] is None and first.get('usage_source') is None and first['status'] == 'completed'
            and first['model'] == first['requested_model'] == 'gpt-6.2-sol',
            '真实角色缺少用量事件时应回读明确的未知')
    injection = json.loads((pilot.root/'usage-suppression.json').read_text())
    require(injection['assignment'] == first['assignment'] and injection['count'] > 0,
            '用量未知夹具必须实际丢弃至少一条匹配的供应商协议更新')
    first_files = state_before['stage_deliveries'][0]['files']
    require(set(first_files) == {'first.json'}, '追加预算前不应产生第二阶段文件')
    verified = first_files['first.json']
    first_archive = pilot.runtime/'goal-attempts'/ticket_id/assignments[-1]['id']/'verified'/'first.json'
    require(hashlib.sha256(first_archive.read_bytes()).hexdigest() == verified,
            '预算阻塞时必须保留第一阶段的已验文件')
    outcome['exhausted'] = {
        'execution_status': exhausted['status'], 'execution_error': exhausted['error'], 'completed_stages': 1,
        'assignments': {a['phase']:a['run'] for a in assignments}, 'verified_first_file_sha256': verified,
        'role': evidence_role(first), 'suppressed_notifications': injection['count'],
        'role_timing':check_completed_role(pilot,ticket_id,assignments[1]),
        'scope': '真实模型轮输出不变。隔离 Worker 在本地收件时丢弃首角色的用量更新。',
    }

    endpoint = '/api/outer/extend_team_budget'
    binding = {**ref, 'revision': blocked['revision'], 'mandate_revision': blocked['mandate_revision']}
    board.call(endpoint, {**binding, 'additional_minutes': 0, 'additional_roles': 0, 'request_id': request_id()}, status=400)
    # 先观察不批准追加时，主持调度器也不产生下一步派工。
    for _ in range(4):
        unchanged = runtime.tick(ticket_id)
        require(unchanged['status'] == 'budget_exhausted'
                and unchanged['goal_execution']['assignments'] == assignments,
                '拒绝零追加或不授权时不得自动续办')
        time.sleep(.35)

    extension = {**binding, 'additional_minutes': 0, 'additional_roles': 5, 'request_id': request_id()}
    accepted = board.call(endpoint, extension)
    require(accepted['state'] == 'DONE' and accepted['mandate_revision'] == 2,
            '新增额度应为独立第二份授权')
    duplicate = board.call(endpoint, extension)
    require(duplicate['duplicate'] and duplicate['event_id'] == accepted['event_id'],
            '同一个追加请求应回读首条回执')
    board.call(endpoint, {**extension, 'additional_roles': 10}, status=409)
    board.call(endpoint, {**extension, 'request_id':request_id()}, status=409)
    current = board.call('/api/outer/get', ref)
    require(current['mandate_revision'] == 2 and current['mandate']['team_role_limit'] == 6
            and current['mandate']['budget_seconds'] == mandate['budget_seconds'],
            '续办只扩大本人批准的轮次额度')
    outcome['extension'] = {
        'new_authorizations':1, 'old_mandate_revision':1, 'new_mandate_revision':2,
        'additional_minutes':0, 'additional_roles':5, 'new_role_limit':6,
        'decline_is_read_only':True, 'zero_increase_http':400, 'duplicate_event_reused':True,
        'changed_same_key_http':409, 'stale_version_http':409,
    }

    completed = until_status('completed')
    finished = board.call('/api/outer/get', ref)['execution']
    new_assignments = completed['goal_execution']['assignments'][len(assignments):]
    require([a['phase'] for a in new_assignments] == ['execute', 'review']
            and all(a['step'] == 1 and a['outer_binding']['mandate_revision'] == 2 for a in new_assignments),
            '续办只允许登记一次剩余阶段的执行及复验')
    require(len(finished['team_activity']) == 2, '每个短阶段需要一条真实角色执行')
    second = finished['team_activity'][1]
    require(second['status'] == 'completed' and second['model'] == second['requested_model'] == 'gpt-6.2-sol',
            '追加后角色应继续使用原批准模型')
    report = finished['goal_delivery']
    digest_by_file = {}
    for name, expected in EXPECTED.items():
        url = f'/api/tasks/{ticket_id}/artifact?' + urlencode({'path': name, 'candidate': report['assignment']})
        content = board.call(url, raw=True)
        checksum = hashlib.sha256(content).hexdigest()
        require(json.loads(content) == expected and report['files'][name] == checksum,
                f'最终归档中的 {name} 不匹配')
        digest_by_file[name] = checksum
    require(digest_by_file['first.json'] == verified, '续办更改了前一阶段已核验文件的字节')
    require(len(finished['checks']) == 2 and all(c['status']=='PASS' for c in finished['checks']),
            '终验应重新核对两个已批准条件')
    with sqlite3.connect((pilot.runtime/'tasks.sqlite3').as_uri()+'?mode=ro&immutable=1',uri=True) as db:
        recorded = db.execute("""SELECT json_extract(body,'$.type'), state, count(*) FROM outer_events
                                  WHERE ticket=? AND json_extract(body,'$.type') IN ('approval','execution_budget')
                                  GROUP BY 1,2 ORDER BY 1""", (ticket_id,)).fetchall()
    require(recorded == [('approval','DONE',1),('execution_budget','DONE',1)], '新授权次数与原批准次数须分别计数')
    outcome['final'] = {
        'status':'completed', 'stage_two_runs':{a['phase']:a['run'] for a in new_assignments},
        'checks':{item['id']:item['status'] for item in finished['checks']}, 'download_sha256':digest_by_file,
        'preserved_first_stage_bytes':True, 'role':evidence_role(second),
        'role_timing':check_completed_role(pilot,ticket_id,new_assignments[0]),
        'usage_replay':check_usage_replay(second), 'independent_billing':None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--formal-source', type=Path, required=True)
    parser.add_argument('--formal-canary-recovery', type=Path)
    parser.add_argument('--codex-home', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=250)
    parser.add_argument('--fixture-intake', action='store_true', help='只替换合成接单和验收设计，保留真实 Worker')
    args = parser.parse_args()
    codex_home = args.codex_home.resolve(strict=True)
    if codex_home.parent != Path('/private/tmp') or not codex_home.is_dir():
        parser.error('请选择已授权的私有临时 Codex 配置')
    repo = Path(__file__).resolve().parents[1]
    root = Path(tempfile.mkdtemp(prefix='autoloop-goal-team-f9-', dir='/private/tmp'))
    print(f'本次 F9 隔离目录 {root}', flush=True)
    (root/'.f9-budget-test').touch(mode=0o600)
    outcome = {'case':'F9-role-allowance-real-api', 'passed':False, 'synthetic':True, 'runtime':'codex-app-server',
               'transport':'isolated Web API', 'approval_operator':'acceptance_test',
               'intake':'synthetic-intake-fixture' if args.fixture_intake else 'real-codex'}
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        with ExitStack() as stack:
            code = root/'formal-board'
            formal = args.formal_source.resolve(strict=True)
            if args.formal_canary_recovery:
                originals = read_recovery(args.formal_canary_recovery.resolve(strict=True))
                copied = root/'formal-source'
                shutil.copytree(formal,copied,ignore=source_only(formal))
                for name in ('server.py','index.html','tasks.js'):
                    (copied/name).write_bytes(originals[name][0])
                formal = copied
            subprocess.run([sys.executable,'-B',str(repo/'scripts/prepare_formal_outer_pilot.py'),
                            str(formal), str(code)],check=True,timeout=45)
            if args.fixture_intake:
                install_fixture_intake(root, code)
            pilot = Isolated(code, root, stack)
            require(not {pilot.port,pilot.hub_port}&BORROWED_PORTS,'本试验端口与现有实例冲突')
            launch_home = root/'role-home'
            launch_home.mkdir(mode=0o700)
            pilot.env.update(CODEX_HOME=str(codex_home),HOME=str(launch_home),AUTOLOOP_OUTER_PROXY_PORT='7897')
            board = pilot.launch('control',goal_team=True,trial_token=secrets.token_hex(32))
            isolate_usage_observation(pilot)
            require(board.call('/api/outer')['goal_team_enabled'] is True, '隔离实例必须已启用目标模式')
            run_case(pilot,board,outcome,args.timeout)
            outcome['passed'] = True
    finally:
        save_json(root/'f9-acceptance.json',outcome)
        print(f'F9 脱敏记录 {root / "f9-acceptance.json"}', flush=True)


if __name__ == '__main__':
    main()
