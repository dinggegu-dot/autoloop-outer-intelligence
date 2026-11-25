"""通过普通 HTTP 工单入口逐项操作和核对阶段一合成实例。"""

import argparse
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlencode
from uuid import uuid4

from goal_team_phase_one_fixture import CASES, INITIAL_LIMIT, TICKET_NAMES, WORKER_INTERRUPTED, active, checked_contract, order, specs
from live_filename_index import Board, require, wait_until


def connect(instance):
    root = instance.resolve(strict=True)
    case = active(root)
    manifest = json.loads((root/'instance.json').read_text())
    port = re.fullmatch(r'http://127\.0\.0\.1:(\d{1,5})', manifest.get('board',''))
    require(manifest['phase'] == 'ready' and manifest['phase_one_fixture'] == case and port
            and int(port[1]) not in {8765, 60966, 61498, 59777}, '未能验证本次阶段一工作台地址')
    board = Board(int(port[1]))
    require(board.call('/api/outer').get('goal_team_enabled') is True, '隔离工作台未开放目标模式')
    return root, case, board


def detail(board, case, identifier):
    ticket = board.call('/api/outer/get', {'ticket_id':identifier})
    require(ticket['workbench_name'] == TICKET_NAMES[case], '原单名称与本次合成场景不符')
    if ticket['status'] == 'AWAITING_APPROVAL' or ticket.get('mandate'):
        require(checked_contract(case, ticket.get('acceptance_contract')), '真实接单未登记预期的两项合成机器验收')
    return ticket


def submit(board, case):
    submission = board.call('/api/tasks', dict(order(case), workflow='outer', request_id=str(uuid4())), status=201)
    print(f"接单事件 {submission['event_id']}", flush=True)

    def awaiting():
        event = board.call('/api/outer/event', {'event_id':submission['event_id']})
        if not event.get('ticket_id'):
            return None
        ticket = detail(board, case, event['ticket_id'])
        if ticket['status'] in ('VERIFICATION_GAP', 'NEEDS_ATTENTION', 'AWAITING_CLARIFICATION'):
            raise RuntimeError(f"真实接单需要检查，原单 {ticket['id']}，状态 {ticket['status']}")
        return ticket if ticket['status'] == 'AWAITING_APPROVAL' else None

    ticket = wait_until(awaiting, 245, '真实接单未在时限内进入待批准')
    return {'ticket':ticket['id'], 'status':ticket['status'],
            'approval_team_role_limit':ticket['approval_terms'].get('team_role_limit')}


def approve(board, case, ticket):
    require(ticket['status'] == 'AWAITING_APPROVAL' and not ticket.get('mandate'), '原单当前不能批准')
    if case == 'budget':
        require(ticket['approval_terms'].get('team_role_limit') == INITIAL_LIMIT, '合成批准条款未显示预期额度')
    response = board.call('/api/outer/approve', {
        'ticket_id':ticket['id'], 'revision':ticket['revision'], 'goal_revision':ticket['goal_revision'],
        'acceptance_revision':ticket['acceptance_revision'], 'request_id':str(uuid4()),
    })
    mandate = response['mandate']
    require(mandate['operator_kind'] == 'acceptance_test' and mandate['revision'] == 1,
            '批准未按试验操作员归属登记')
    return {'ticket':ticket['id'], 'mandate_revision':mandate['revision'], 'operator_kind':mandate['operator_kind'],
            'team_role_limit':mandate.get('team_role_limit')}


def extend(board, case, ticket):
    mandate = ticket.get('mandate') or {}
    execution = ticket.get('execution') or {}
    require(case == 'budget' and execution.get('status') == 'budget_exhausted'
            and mandate.get('team_role_limit') == INITIAL_LIMIT and ticket['mandate_revision'] == 1,
            '当前合成工单未进入预期的待追加预算状态')
    body = {'ticket_id':ticket['id'], 'revision':ticket['revision'], 'mandate_revision':1,
            'additional_minutes':0, 'additional_roles':5, 'request_id':str(uuid4())}
    endpoint = '/api/outer/extend_team_budget'
    board.call(endpoint, dict(body, additional_roles=0), status=400)
    response = board.call(endpoint, body)
    replay = board.call(endpoint, body)
    require(response['mandate_revision'] == 2 and replay.get('duplicate') is True
            and response['event_id'] == replay['event_id'], '额度续办没有按一次新授权登记')
    board.call(endpoint, dict(body, request_id=str(uuid4())), status=409)
    return {'ticket':ticket['id'], 'mandate_revision':2, 'additional_roles':5, 'same_key_replay':True,
            'zero_roles_http':400, 'stale_mandate_http':409}


def inspect_review_recovery(root, board, ticket, state, status, delivery, marker, query):
    if not marker:
        require(status != 'completed', '返修已完成，但没有在新版落盘后中断试验 Worker')
        return None
    require(marker.get('repair_review_pending') and marker['worker_exit'] == -9
            and marker['worker_before'] != marker['worker_after'], '没有真实中断并重启专用 Worker')
    assignments = state['assignments']
    original = next(row for row in assignments if row['id'] == marker['assignment'])
    require(original['run'] == marker['run'], '中断凭证与工单的原派工不同')
    directory = root/'workbench'/'goal-attempts'/ticket['id']/original['id']
    durable = (directory/'candidate-recovery.json').read_bytes()
    require(not (directory/'report.json').exists() and hashlib.sha256(durable).hexdigest() == marker['recovery_sha256'],
            '应复用中断时的耐久凭证，原派工没有业务回执')
    saved = json.loads(durable)['report']
    require(saved['repair_review_pending'] and saved['checkpoint']['digest'] == marker['checkpoint'],
            '耐久凭证没有绑定写入的新版和待复验事项')
    writer = original['role_calls'][-1]
    require((writer['role'],writer['writable'],writer['status']) == ('implementer',True,'completed')
            and writer.get('candidate_sha256') == marker['checkpoint'], '中断时应只有写入角色交回新版')
    board.call(query + '&' + urlencode({'candidate':original['id']}), status=404)
    result = {'original_run':original['run'], 'native_transport':original.get('transport'),
              'receipt_source':original.get('receipt_source'), 'primary_receipt_missing':True,
              'repaired_checkpoint':saved['checkpoint'], 'unverified_candidate_download_http':404}
    if status == 'completed':
        remaining = assignments[assignments.index(original)+1:]
        require(len(remaining) == 2 and remaining[0]['phase'] in ('execute','repair') and remaining[1]['phase']=='review',
                '恢复后须仅登记一次只读负责人复验和一次外层独立验收')
        lead, independent = remaining
        roles = [(call['role'],call['writable'],call['status']) for call in lead.get('role_calls',[])]
        continuation = lead.get('repair_continuation') or {}
        require(original.get('receipt_source') == 'recovery' and roles == [('lead',False,'completed')]
                and continuation.get('review_only') == saved['repair_review_pending']
                and lead['candidate_base']['assignment'] == original['id']
                and lead['candidate_base']['artifact_sha256'] == marker['checkpoint']
                and lead['role_calls'][0].get('candidate_sha256') == marker['checkpoint']
                and lead['role_calls'][0].get('model') == 'gpt-6.2-sol'
                and lead['role_calls'][0].get('sandbox') == 'read-only'
                and lead['role_calls'][0].get('permission_digest'),
                '续办须从中断新版只读复验，无重复实施')
        checks = independent['report']['validation']['checks']
        require(independent['report']['checkpoint'] == saved['checkpoint']
                and independent['id'] == delivery['assignment']
                and delivery['artifact_sha256'] == marker['checkpoint']
                and len(checks) == 2 and all(check['status'] == 'PASS' for check in checks)
                and ticket['mandate_revision'] == 1, '独立终验或交付未沿用原授权和同一新版')
        result.update(only_read_only_lead_reran=True, continuation_run=lead['run'], independent_review_run=independent['run'])
    return result


def inspect_codex_timeout(root, ticket, state, status, delivery, marker, query, board):
    if not marker:
        require(status != 'completed', '合成单完成前未观察到真实 Codex 超时')
        return None
    require(marker['error_code'] == 'timeout' and marker['deadline_injected_after_actual_shell_start']
            and marker['interrupt_sent'] and marker['interrupt_acknowledged']
            and marker['turn_terminal_status'] == 'interrupted' and marker['process_record_state'] == 'stopped'
            and marker['live_tracked_processes_at_interrupt'] >= 2
            and marker['live_tracked_after_cleanup'] == marker['live_group_members_after_cleanup'] == 0,
            '未观察到真实 Codex 命令启动、停止请求及进程组回收')
    assignments = state['assignments']
    original = next(row for row in assignments if row['id'] == marker['assignment'])
    require(original['run'] == marker['run'], '超时凭证与工单的原派工不同')
    attempts = root/'workbench'/'goal-attempts'/ticket['id']
    folder = attempts/original['id']
    candidate = json.loads((folder/'candidate-recovery.json').read_text())['report']
    require(candidate.get('repair_review_pending') and candidate['checkpoint'] == marker['sealed_checkpoint'],
            '超时后缺少与原新版一致的续验凭证')
    calls = original['role_calls']
    require((calls[-1]['role'], calls[-1]['status'], calls[-1].get('error_code')) == ('lead', 'failed', 'timeout'),
            '工单未登记真实的负责人超时')
    board.call(query + '&' + urlencode({'candidate':original['id']}), status=404)
    result = {key:marker[key] for key in ('run','error_code','deadline_injected_after_actual_shell_start',
        'interrupt_sent','interrupt_acknowledged',
        'turn_terminal_status','process_record_state','tracked_process_count','live_tracked_processes_at_interrupt',
        'live_tracked_after_cleanup','live_group_members_after_cleanup','sealed_checkpoint')}
    result['unverified_candidate_download_http'] = 404
    if status == 'completed':
        later = assignments[assignments.index(original)+1:]
        require(len(later) == 2, '超时后只能续派负责人只读复验、再做独立验收')
        lead, independent = later
        reread = lead['role_calls']
        expected = candidate['checkpoint']['digest']
        require(len(reread) == 1 and (reread[0]['role'], reread[0]['writable'], reread[0]['status'])
                == ('lead', False, 'completed') and reread[0].get('candidate_sha256') == expected
                and lead['candidate_base']['assignment'] == original['id']
                and lead['repair_continuation']['review_only'] == candidate['repair_review_pending'],
                '续办未从封存新版只读复验')
        require(independent['phase'] == 'review' and independent['id'] == delivery['assignment']
                and delivery['artifact_sha256'] == expected and ticket['mandate_revision'] == 1
                and len(independent['report']['validation']['checks']) == 2
                and all(row['status'] == 'PASS' for row in independent['report']['validation']['checks']),
                '独立终验与原授权或封存新版不同')
        result.update(only_read_only_lead_reran=True, continuation_run=lead['run'],
                      independent_review_run=independent['run'])
    return result


def inspect(root, case, board, ticket):
    execution = ticket.get('execution') or {}
    state = execution.get('goal_execution') or {}
    status = execution.get('status')
    path = specs(case)[0][0]
    delivery = execution.get('goal_delivery') or {}
    rows = []
    for row in state.get('assignments', []):
        rows.append({key:row[key] for key in ('id','run','phase','state','transport','inner_repairs') if key in row}
                    | {'role_sequence':[call['role'] for call in row.get('role_calls', [])],
                       'mandate_revision':(row.get('outer_binding') or {}).get('mandate_revision'),
                       'receipt_source':row.get('receipt_source')})
    result = {'ticket':ticket['id'], 'stage':state.get('stage'), 'status':status or ticket['status'],
              'error':execution.get('error'), 'mandate_revision':ticket['mandate_revision'], 'assignments':rows}

    def marker(name):
        local = root/name
        if local.is_file():
            value = json.loads(local.read_text())
            if value.get('ticket') == ticket['id']:
                return value
        return None

    query = f"/api/tasks/{ticket['id']}/artifact?" + urlencode({'path':path})
    if status in ('blocked', 'budget_exhausted'):
        require(not delivery, '被拦截的合成候选不能有交付归档')
        board.call(query, status=404)
        result['download_http'] = 404
    elif status == 'completed':
        data = board.call(query + '&' + urlencode({'candidate':delivery['assignment']}), raw=True)
        digest = hashlib.sha256(data).hexdigest()
        require(digest == delivery['files'][path], '实际下载字节与独立验收归档摘要不同')
        result.update(download_http=200, download_sha256=digest, archive_sha256=delivery['artifact_sha256'],
                      checks=[dict(id=row['id'], status=row['status']) for row in execution.get('checks',[])])
    interruption = marker(WORKER_INTERRUPTED)
    result.update(initial_fault=marker('phase-one-initial-fault.json'),
                  retained_fault=marker('phase-one-retained-fault.json'), worker_interruption=interruption)
    if case == 'review-recovery':
        result['review_recovery'] = inspect_review_recovery(root,board,ticket,state,status,delivery,interruption,query)
    if case == 'codex-timeout':
        result['codex_timeout'] = inspect_codex_timeout(root,ticket,state,status,delivery,
                                                       marker('phase-one-codex-timeout.json'),query,board)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    draft = actions.add_parser('case', help='输出正式 Chrome 新单表单可填写的合成字段')
    draft.add_argument('case', choices=CASES)
    actions.add_parser('submit', help='从普通新单接口发起并等到待批准').add_argument('instance', type=Path)
    for action in ('approve','inspect','extend'):
        command = actions.add_parser(action)
        command.add_argument('instance', type=Path)
        command.add_argument('ticket')
    args = parser.parse_args()
    if args.action == 'case':
        result = order(args.case)
    else:
        root, case, board = connect(args.instance)
        if args.action == 'submit':
            result = submit(board, case)
        else:
            ticket = detail(board, case, args.ticket)
            if args.action == 'approve':
                result = approve(board, case, ticket)
            elif args.action == 'extend':
                result = extend(board, case, ticket)
            else:
                result = inspect(root, case, board, ticket)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
