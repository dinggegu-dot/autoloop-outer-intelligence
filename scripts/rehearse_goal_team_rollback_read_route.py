"""在私有副本关闭新单全 GPT 试点，验证已签收原单回读和 V2 新单路由。"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import sys
import tempfile
import threading
from urllib.parse import urlencode
from unittest.mock import patch
from uuid import uuid4

from live_filename_index import Board, require
from live_goal_team_budget import fixture_intake_model
from prepare_formal_outer_pilot import prepare
from recover_goal_team_pilot import ATTEMPT_ID, TASK_ID, read_db, sha256, tree_content


PROTECTED_PORTS = {60966, 61498, 59777, 8765}
NAME = '回退验收 · 独立合成 V2 工单'
GOAL = '生成 rollback.json，JSON 对象精确等于 {"route":"v2"}，由机器核验归档。'
EXPECTED = {'rollback.json': {'route': 'v2'}}


def copy_signed_source(source, runtime, expected_ticket_sha):
    db = source/'workbench/tasks.sqlite3'
    require(source.parent == Path('/private/tmp') and source.resolve() == source
            and source.is_dir() and source.stat().st_uid == os.getuid()
            and source.stat().st_mode & 0o077 == 0 and (source/'.goal-team-pilot').is_file(),
            '来源须为本机私有合成目标试用目录')
    require(db.resolve() == db and stat.S_ISREG(db.lstat().st_mode) and not db.with_name(db.name+'-wal').exists(),
            '只读快照需要已提交的单文件工作台账本')
    before = sha256(db)
    runtime.mkdir(mode=0o700)
    frozen_db = runtime/db.name
    shutil.copy2(db, frozen_db)
    require(before == sha256(db) == sha256(frozen_db) and not db.with_name(db.name+'-wal').exists(),
            '读取窗口内原账本变化，请重新取一致副本')
    with read_db(frozen_db) as ledger:
        require(ledger.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', '副本账本未通过完整性检查')
        signed = []
        for raw, in ledger.execute("SELECT body FROM tasks WHERE json_extract(body,'$.status')='completed'"):
            task = json.loads(raw)
            if hashlib.sha256(task['id'].encode()).hexdigest() != expected_ticket_sha:
                continue
            marks = task.get('goal_execution') or {}
            question = marks.get('pending_preference') or {}
            if (len(question.get('options', [])) == 3 and question.get('answer') and
                    (marks.get('outer', {}).get('mandate') or {}).get('execution_profile') and
                    (marks.get('artifact_reviews') or [{}])[-1].get('state') == 'accepted'):
                signed.append(task)
        require(len(signed) == 1, '副本须含有指定的三样例已签收全 GPT 原单')
        task = signed[0]
        ticket = task['id']
        deliveries = task['goal_execution']['artifact_reviews']
        candidate = deliveries[-1]
        require(TASK_ID.fullmatch(ticket) and ATTEMPT_ID.fullmatch(candidate['assignment']), '原单或候选编号无效')
        require(candidate['assignment'] == task['goal_delivery']['assignment'], '原签收与最终归档候选不一致')
    folder = source/'workbench/goal-attempts'/ticket/candidate['assignment']/'verified'
    require(folder.is_dir() and folder.resolve() == folder, '原单缺少安全验后归档')
    archived_before = tree_content(folder)
    require(bool(archived_before), '原单验后归档为空')
    destination = runtime/'goal-attempts'/ticket/candidate['assignment']/'verified'
    shutil.copytree(folder, destination)
    require(tree_content(destination) == tree_content(folder) == archived_before,
            '原归档出现变化或副本字节不同')
    return task, (db, before, folder, archived_before)


def fixture_intake(role, context, _schema):
    return {'result': fixture_intake_model(role, context, expected=EXPECTED, goal=GOAL),
            'model': 'synthetic-rollback-intake'}


@contextmanager
def isolated_board(runtime, *, team_enabled, trial_token):
    from server import Handler, ThreadingHTTPServer
    from workbench import Store, Workbench

    class NoHub:
        def state(self):
            raise OSError('本试验不接触现役 Hub')

        records = state

    class QuietHandler(Handler):
        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler) as server:
        require(server.server_port not in PROTECTED_PORTS, '试验端口触及现有受保护实例')
        server.workbench = Workbench(Store(runtime), NoHub(), goal_enabled=True, outer_enabled=True,
                                     outer_workbench_entry=True, outer_model=fixture_intake)
        server.workbench.outer_goal_team = team_enabled
        server.session_token = secrets.token_urlsafe(32)
        server.csrf = secrets.token_urlsafe(32)
        server.trial_operator_token = trial_token
        server.dsh_bridge_token = secrets.token_urlsafe(32)
        server.task_actions = threading.Lock()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            board = Board(server.server_port)
            board.headers['X-Odyssey-Trial-Operator'] = trial_token
            board.headers['Authorization'] = 'Bearer ' + server.dsh_bridge_token
            yield board, server.workbench
        finally:
            server.shutdown()
            thread.join(timeout=5)


def read_original(board, ticket, *, enabled):
    overview = board.call('/api/outer')
    require(overview['enabled'] is True and overview['goal_team_enabled'] is enabled,
            '工单读取服务或新单开关不符合试验配置')
    require(any(row['id'] == ticket for row in overview['drafts']), '签收原单未出现在目录')
    draft = board.call('/api/outer/get', {'ticket_id': ticket})
    dsh_drafts = board.call('/api/outer-dsh/list', {})
    require(any(row['id'] == ticket for row in dsh_drafts), '签收原单未出现在 DSH 目录')
    require(board.call('/api/outer-dsh/get', {'ticket_id': ticket}) == draft, 'DSH 与网页原单投影不一致')
    execution = draft['execution']
    state = execution['goal_execution']
    question = state['pending_preference']
    preference = [item for item in state['decisions'] if item['kind'] == 'preference_answered']
    signoff = [item for item in state['decisions'] if item['kind'] == 'artifact_accepted']
    require(execution['status'] == 'completed' and draft['journey']['station'] == 'delivery'
            and len(question['options']) == 3 and len(preference) == len(signoff) == 1
            and question['answer']['text'] == preference[0]['text']
            and question['answer']['option_id'] == preference[0]['option_id']
            and not execution.get('pending_preference'), '签收、样例材料或生效意见字段不一致')
    require(execution['team_activity'], '签收工单的已公开角色记录为空')
    delivery = execution['goal_delivery']
    hashes = {}
    for name, expected in delivery['files'].items():
        path = f'/api/tasks/{ticket}/artifact?' + urlencode({'path':name,'candidate':delivery['assignment']})
        body = board.call(path,raw=True)
        hashes[name] = hashlib.sha256(body).hexdigest()
        require(hashes[name] == expected, '下载文件摘要不同于签收归档')
        dsh_bytes = board.call('/api/outer-dsh/download', {
            'ticket_id': ticket, 'candidate': delivery['assignment'], 'path': name,
        }, raw=True)
        require(dsh_bytes == body, 'DSH 下载不同于网页的原归档')
    return {'draft':draft, 'hashes':hashes}


def approve_new_v2(board, workbench):
    result = board.call('/api/tasks',{'name':NAME, 'goal':GOAL, 'acceptance':GOAL,
                                     'workflow':'outer', 'request_id':str(uuid4())}, status=201)
    workbench.outer().drain(limit=6)
    receipt = board.call('/api/outer/event',{'event_id':result['event_id']})
    require(receipt['state']=='DONE' and receipt['ticket_id'], '合成 V2 工单未完成接单')
    ticket = receipt['ticket_id']
    body = {'ticket_id':ticket}
    draft = board.call('/api/outer/get',body)
    require(draft['status']=='AWAITING_APPROVAL' and not draft['approval_terms'].get('execution_profile'),
            '关闭试点后新草案不应冻结全 GPT 执行配置')
    approved = board.call('/api/outer/approve',{
        **body, 'revision':draft['revision'], 'goal_revision':draft['goal_revision'],
        'acceptance_revision':draft['acceptance_revision'], 'request_id':str(uuid4()),
    })
    mandate = approved['mandate']
    require(approved['status']=='AUTHORIZED' and mandate['revision']==1 and
            mandate['operator_kind']=='acceptance_test' and not mandate.get('execution_profile'),
            '新原单批准未选择默认 V2 授权')
    return ticket


def check_regular_entry(board):
    result = board.call('/api/tasks', {
        'name': '回退验收 · 普通工单', 'goal': '核对未勾选目标模式时的登记路线。', 'request_id': str(uuid4()),
    }, status=201)
    task = result['task']
    require(result['created'] and not result.get('workflow') and not task.get('workflow')
            and not task.get('goal_execution'), '未勾选目标模式却送入了目标工单路线')


def route_probe(workbench, board, ticket):
    from superagent.goal_engine import MODEL, active_assignments
    from superagent.goal_runtime import execute_assignment

    engine = workbench.goal_engine()
    active = []

    def default_adapter(assignment, workspace, _directory, *, launch_gate):
        phase = assignment['phase']
        with launch_gate():
            active.append(phase)
            if phase == 'plan':
                criteria = assignment['outer_acceptance']['criteria']
                result = {'summary':'生成独立合成文件', 'steps':[{'objective':'生成并验收 rollback.json',
                          'checks':[criterion['criterion_id'] for criterion in criteria]}]}
            elif phase == 'execute':
                (workspace/'rollback.json').write_text(json.dumps(EXPECTED['rollback.json']))
                result = {'summary':'合成执行夹具已写入文件'}
            else:
                raise AssertionError('此夹具只提供 V2 规划及执行返回')
        return {'result':result, 'session':'synthetic-v2-'+phase, 'model':MODEL,
                'started_ns':1_000_000_000, 'ended_ns':1_001_000_000}

    observed = []
    with patch('superagent.goal_codex.invoke',side_effect=default_adapter), patch(
        'superagent.goal_team_run.run_team',side_effect=AssertionError('全 GPT 团队路由不应接手新单')
    ) as team_adapter:
        for run,expected in enumerate(('plan','execute','review'),1000):
            pending, = active_assignments(engine.prepare(ticket))
            require(pending['phase']==expected and not pending.get('execution_profile'), '实际派工未使用预期 V2 阶段')
            engine.bind(ticket,pending['id'],run,run)
            assignment = engine.assignment_input(ticket,pending['id'],run)
            directory = workbench.store.root/'goal-attempts'/ticket/pending['id']
            directory.mkdir(mode=0o700,parents=True)
            report = execute_assignment(workbench.store, assignment, directory, None)
            require(not report.get('error'), 'V2 路由探针执行失败：'+report.get('error',''))
            settled = engine.settle(ticket,pending['id'],run,'done',report)
            observed.append(assignment['phase'])
        require(not team_adapter.called, '关闭试点后不应派发内层团队角色')
    require(active == ['plan','execute'] and observed==['plan','execute','review']
            and settled['status']=='completed', 'V2 默认执行端没有完成机验终态')
    delivery = settled['goal_delivery']
    path = f'/api/tasks/{ticket}/artifact?' + urlencode({'path':'rollback.json','candidate':delivery['assignment']})
    content = board.call(path,raw=True)
    require(json.loads(content) == EXPECTED['rollback.json'] and
            hashlib.sha256(content).hexdigest() == delivery['files']['rollback.json'],
            'V2 成果或独立核验与冻结文件不一致')
    return {'phase_sequence':observed,'adapter_called_by_runtime':active,'team_adapter_calls':0,
            'real_deterministic_review':True,'download_matches_verified':True}


def rehearse(args, root):
    task, source_state = copy_signed_source(args.source, root/'workbench', args.ticket_sha256)
    db, before, folder, archived_before = source_state
    ticket = task['id']
    app = root/'formal-board'
    prepare(args.formal_source,app)
    sys.path.insert(0,str(app))
    token = secrets.token_hex(32)
    with isolated_board(root/'workbench',team_enabled=True,trial_token=token) as (before_board,_):
        original = read_original(before_board,ticket,enabled=True)
    with isolated_board(root/'workbench',team_enabled=False,trial_token=token) as (board,workbench):
        recovered = read_original(board,ticket,enabled=False)
        require(original==recovered, '关闭新单开关改变了已签收原单的公开投影或下载')
        check_regular_entry(board)
        new_ticket = approve_new_v2(board,workbench)
        new_route = route_probe(workbench,board,new_ticket)
        require(read_original(board,ticket,enabled=False)==original, '新 V2 工单改变之前已签收的原单')
    require(sha256(db)==before and tree_content(folder)==archived_before, '原始合成资料在试验期间发生变化')
    execution = original['draft']['execution']
    return {
        'case':'P4-rollback-reader-and-new-v2-route',
        'passed':True,'synthetic':True,'observed_at_utc':datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'ticket_ref_sha256':args.ticket_sha256,
        'mode':{'new_ticket_team_flag_before':True,'new_ticket_team_flag_after':False,'same_compatible_source':True},
        'original':{'read_only_snapshot':True,'original_bytes_unchanged':True,'signed_ticket_preserved':True,
                    'saved_sample_count':3,'same_public_role_history_count':len(execution['team_activity']),
                    'same_answer_and_original_notes':True,'same_verified_download_sha256':original['hashes'],
                    'local_dsh_bridge_matches_web':True},
        'regular_workbench_route_preserved': True,
        'new_v2':{'operator_kind':'acceptance_test','mandate_revision':1,'execution_profile_present':False,**new_route},
        'limits':{'handler_and_ledger':'真实工作台源码和 HTTP 接口',
                  'model_and_hub':'新单以合成模型回执和 Hub 绑定验证实际 V2 调度分支',
                  'original_snapshot':'只读单文件工作台账本和已签收的冻结归档，未复制运行密钥',
                  'browser_or_dsh_ui_tested':False},
    }


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True,type=Path)
    parser.add_argument('--formal-source',required=True,type=Path)
    parser.add_argument('--ticket-sha256',required=True)
    parser.add_argument('--evidence',required=True,type=Path)
    args = parser.parse_args()
    require(not args.evidence.exists(), '证据路径已有文件')
    with tempfile.TemporaryDirectory(prefix='autoloop-goal-team-rollback-', dir='/private/tmp') as scratch:
        evidence = rehearse(args, Path(scratch))
    with args.evidence.open('x') as output:
        json.dump(evidence,output,ensure_ascii=False,indent=2)
        output.write('\n')
    print('关闭新单全 GPT 开关后，原单回读和默认 V2 路由通过。',flush=True)


if __name__ == '__main__':
    main()
