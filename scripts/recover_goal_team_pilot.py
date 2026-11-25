"""冷备已停止的合成目标模式实例，并用副本复查工单和归档下载。"""

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlencode, urlsplit

from live_filename_index import Board, deployed_listener_pids


ISOLATED = Path('/private/tmp')
TASK_ID = re.compile(r'wb-[a-f0-9]{32}')
ATTEMPT_ID = re.compile(r'[a-f0-9]{32}')


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1_048_576), b''):
            digest.update(block)
    return digest.hexdigest()


def tree_content(root):
    files = {}
    for path in sorted(root.rglob('*')):
        mode = path.lstat().st_mode
        relative = str(path.relative_to(root))
        if stat.S_ISDIR(mode):
            files[relative] = ('directory', stat.S_IMODE(mode))
        elif stat.S_ISREG(mode):
            files[relative] = ('file', stat.S_IMODE(mode), sha256(path))
        else:
            raise RuntimeError('合成实例含符号链接或特殊文件，不能确认冷备边界')
    return files


def _unoccupied(args):
    result = subprocess.run(['/usr/sbin/lsof', '-nP', '-t', *args],
                            capture_output=True, text=True, timeout=15)
    require(result.returncode in (0, 1) and not result.stderr, '无法核实隔离实例的进程状态')
    require(not result.stdout.strip(), '隔离实例仍有监听端口或打开的文件')


def check_stopped(source):
    require(source.parent == ISOLATED and source.name.startswith('autoloop-goal-team-')
            and source.is_dir() and source.resolve() == source
            and source.stat().st_uid == os.getuid() and source.stat().st_mode & 0o077 == 0,
            '来源须是当前用户的私有目标模式隔离目录')
    require((source/'.goal-team-pilot').is_file() and (source/'.goal-pilot').is_file(),
            '缺少合成隔离试点标记')
    marker = json.loads((source/'instance.json').read_text())
    board = urlsplit(marker['board'])
    require(marker['phase'] == 'stopped' and marker.get('instance') == str(source)
            and marker.get('workbench') == str(source/'workbench')
            and board.scheme == 'http' and board.hostname == '127.0.0.1'
            and marker['hub_port'] == int((source/'.goal-pilot').read_text()),
            '实例未达到停机状态或标记与路径不一致')
    ports = (board.port, marker['hub_port'])
    require(all(type(port) is int and 0 < port <= 65_535 and port != 8765 for port in ports)
            and ports[0] != ports[1], '隔离端口无效')
    for port in ports:
        _unoccupied(['-iTCP:' + str(port), '-sTCP:LISTEN'])
    _unoccupied(['+D', str(source)])
    attempts = source/'workbench'/'goal-attempts'
    for marker_path in attempts.glob('*/*/role-*/process.json'):
        record = json.loads(marker_path.read_text())
        require(record.get('version') == 1 and record.get('state') == 'stopped',
                '旧角色进程尚未确认停稳')


def copy_snapshot(source, backup):
    baseline = tree_content(source)
    snapshot = backup/'snapshot'
    rehearsal = backup/'rehearsal'
    # 保留 WAL 与原文件权限。拒绝使用可重放的运行目录作为备份。
    shutil.copytree(source, snapshot, symlinks=True)
    require(tree_content(snapshot) == baseline and tree_content(source) == baseline,
            '备份字节有差异或原隔离实例在复制时发生变化')
    shutil.copytree(snapshot, rehearsal, symlinks=True)
    require(tree_content(rehearsal) == baseline, '演练副本与冷备不同')
    return snapshot, rehearsal, baseline


@contextmanager
def read_db(path):
    # 已检查为冷副本。没有 WAL 时免建共享内存文件。有 WAL 时必须读其已提交页。
    mode = '?mode=ro' if Path(str(path)+'-wal').exists() else '?mode=ro&immutable=1'
    with closing(sqlite3.connect(path.as_uri()+mode, uri=True, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        yield connection


def inspect_ledger(rehearsal, ticket_id, state='completed'):
    with read_db(rehearsal/'workbench'/'tasks.sqlite3') as db:
        workbench_check = [row[0] for row in db.execute('PRAGMA integrity_check')]
        require(workbench_check == ['ok'], '恢复工作台 SQLite 完整性检查失败')
        candidates = []
        for row in db.execute("SELECT body FROM tasks WHERE json_extract(body,'$.status')=?", (state,)):
            task = json.loads(row[0])
            outer = (task.get('goal_execution') or {}).get('outer') or {}
            profile = (outer.get('mandate') or {}).get('execution_profile') or {}
            if profile.get('runtime') == 'codex-app-server' and ticket_id in (None, task['id']):
                candidates.append(task)
        require(len(candidates) == 1, '需指定一张处于所选阶段的全 GPT 隔离工单')
        task = candidates[0]
        require(TASK_ID.fullmatch(task['id']), '工单标识无效')
        row = db.execute('SELECT body FROM outer_tickets WHERE id=?', (task['id'],)).fetchone()
        outer = json.loads(row[0]) if row else {}
        approvals = db.execute("SELECT count(*) FROM outer_revisions WHERE ticket=? AND kind='approval_snapshot'",
                               (task['id'],)).fetchone()[0]
        require(outer.get('status') == 'AUTHORIZED' and approvals >= 1, '恢复工单缺少批准记录')
    assignments = task['goal_execution']['assignments']
    expected = {row['run']:row['transport'] for row in assignments if row.get('run') is not None}
    require(len(expected) == len(assignments) and expected and set(expected.values()) == {'done'},
            '工单尚有未终止或缺失的派工')
    with read_db(rehearsal/'hub'/'hub.db') as db:
        # Hub 的部分索引依赖运行时注册的 card_hash。
        hub_check = [row[0] for row in db.execute('PRAGMA quick_check')]
        require(hub_check == ['ok'], '恢复 Hub SQLite 快速检查失败')
        observed = {row['id']:row['state'] for row in db.execute(
            'SELECT id,state FROM runs WHERE lane=?', (task['id'],))}
        require(observed == expected, '恢复 Hub Run 与工作台派工不一致')
    return task, {'workbench_integrity_check':workbench_check[0], 'hub_quick_check':hub_check[0],
                  'outer_status':outer['status'], 'mandate_revision':outer['mandate_revision'],
                  'approval_count':approvals, 'matched_completed_runs':len(expected)}


@contextmanager
def read_only_board(runtime, app):
    sys.path.insert(0, str(app))
    try:
        with _serve_copied_board(runtime) as result:
            yield result
    finally:
        sys.path.remove(str(app))


@contextmanager
def _serve_copied_board(runtime):
    from server import Handler, ThreadingHTTPServer
    from workbench import Store, Workbench
    from superagent.outer_intake import OuterIntake
    from superagent.outer_verification import capture

    class ReadOnlyStore(Store):
        def __init__(self):
            self.root = runtime
            self.path = runtime/'tasks.sqlite3'

        def connect(self):
            return read_db(self.path)

    class UnavailableHub:
        def state(self):
            raise OSError('cold rehearsal')

        records = state

    class ReadOnlyOuter(OuterIntake):
        def __init__(self, workbench):
            # 原库已有建好的结构，无需运行迁移或配置模型。
            self.workbench = workbench
            self.store = workbench.store

    class ReadOnlyHandler(Handler):
        def do_GET(self):
            path = urlsplit(self.path).path
            if path in ('/api/session', '/api/tasks', '/api/outer') or re.fullmatch(
                r'/api/tasks/wb-[0-9a-f]{32}/artifact', path
            ):
                return super().do_GET()
            self.json_response({'error':'冷备演练只提供回读和下载'}, 404)

        def do_POST(self):
            if urlsplit(self.path).path == '/api/outer/get':
                return super().do_POST()
            self.json_response({'error':'冷备演练禁止写操作'}, 405)

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), ReadOnlyHandler) as server:
        server.workbench = Workbench(store=ReadOnlyStore(), hub=UnavailableHub(), goal_enabled=True)
        server.workbench.outer_enabled = True
        server.workbench.outer_goal_team = True
        server.workbench._outer_service = ReadOnlyOuter(server.workbench)
        server.session_token = secrets.token_urlsafe(32)
        server.csrf = secrets.token_urlsafe(32)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield Board(server.server_port), capture, server.server_port, server.workbench
        finally:
            server.shutdown()
            worker.join(timeout=5)


def verify_artifacts(rehearsal, task):
    runtime = rehearsal/'workbench'
    state = task['goal_execution']
    candidate = state['active_candidate']
    delivery = task['goal_delivery']
    require(all(ATTEMPT_ID.fullmatch(record['assignment']) for record in (candidate, delivery)),
            '封存候选或验收归档标识无效')
    root = runtime/'goal-attempts'/task['id']
    with read_only_board(runtime, rehearsal/'formal-board') as (board, capture, port, _):
        sealed = capture(root/candidate['assignment']/'sealed')[0]
        verified = capture(root/delivery['assignment']/'verified')[0]
        require(sealed == verified and sealed['digest'] == candidate['artifact_sha256']
                == delivery['artifact_sha256'] and sealed['files'] == candidate['files'] == delivery['files'],
                '封存候选、已验归档及账本的摘要不同')
        connection = HTTPConnection('127.0.0.1', port, timeout=5)
        try:
            connection.request('GET', '/api/tasks')
            denied = connection.getresponse()
            denied.read()
            require(denied.status == 403, '副本没有验证工作台会话')
        finally:
            connection.close()
        board.call('/api/tasks', {'goal':'synthetic write denial'}, status=405)
        tasks = board.call('/api/tasks?' + urlencode({'task':task['id']}))['tasks']
        restored = next((item for item in tasks if item['id'] == task['id']), {})
        require(restored.get('status') == 'completed' and restored.get('goal_delivery') == delivery,
                '工作台原单回读的交付与账本不同')
        details = board.call('/api/outer/get', {'ticket_id':task['id']})
        restored = details.get('execution') or {}
        require(restored.get('goal_delivery') == delivery, '原单详情无法还原交付')
        calls = [role for row in state['assignments'] for role in row.get('role_calls', ())]
        require(calls and restored.get('team_activity') and len(restored['team_activity']) == len(calls),
                '恢复工作台未回读团队角色历史')
        downloads = {}
        for name, expected in delivery['files'].items():
            query = urlencode({'path':name, 'candidate':delivery['assignment']})
            content = board.call(f"/api/tasks/{task['id']}/artifact?{query}", raw=True)
            observed = hashlib.sha256(content).hexdigest()
            require(observed == expected, '恢复下载字节与登记摘要不一致')
            downloads[name] = observed
        wrong = '0'*32 if delivery['assignment'] != '0'*32 else '1'*32
        query = urlencode({'path':next(iter(downloads)), 'candidate':wrong})
        board.call(f"/api/tasks/{task['id']}/artifact?{query}", status=404)
    return {'candidate_digest':verified['digest'], 'archive_matches_candidate':True,
            'role_history_count':len(calls), 'download_sha256':downloads,
            'unauthenticated_read_rejected':True, 'write_rejected':True,
            'wrong_candidate_rejected':True}


_PREFERENCE_UI = """
import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
const {drafts, detail} = JSON.parse(readFileSync(0, 'utf8'));
class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this.dataset = {}; this.style = {};
    this.classList = {add(){}, remove(){}, toggle(){}};
    this.value = ''; this.textContent = ''; this.isConnected = true;
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  querySelectorAll() { return []; }
  querySelector() { return null; }
  closest() { return null; }
  contains() { return false; }
  setAttribute() {}
  addEventListener() {}
  focus() {}
  get childElementCount() { return this.children.length; }
}
globalThis.document = {createElement:tag=>new Element(tag), addEventListener(){},
                       getElementById:()=>null, activeElement:null};
globalThis.window = {addEventListener(){}};
globalThis.location = new URL('http://127.0.0.1/?goal='+detail.id);
globalThis.history = {replaceState(){}, pushState(){}};
globalThis.sessionStorage = {getItem:()=>null, setItem(){}, removeItem(){}};
globalThis.requestAnimationFrame = ()=>{};
globalThis.setInterval = ()=>0;
const {mountGoalTeam} = await import(pathToFileURL(process.argv[1]));
const root = new Element('div');
const panel = mountGoalTeam(root,{initial:{drafts}, request:async path=>{
  if (path==='/api/outer') return {drafts};
  if (path==='/api/outer/get') return detail;
  throw Error('unexpected request while rendering');
}});
await panel.refresh();
function descend(node) { return [node,...(node?.children || []).flatMap(descend)]; }
const nodes = descend(root);
const action = nodes.find(node=>node?.id==='goal-open-'+detail.id);
const submit = nodes.find(node=>node?.id==='goal-submit-preference');
const choices = nodes.filter(node=>node?.type==='radio' && node.name==='goal-option');
console.log(JSON.stringify({action:action?.textContent,submit:submit?.textContent,options:choices.length}));
"""


def verify_waiting(rehearsal, task):
    runtime, app = rehearsal/'workbench', rehearsal/'formal-board'
    state = task['goal_execution']
    question = state.get('pending_preference') or {}
    candidate = state.get('active_candidate') or {}
    require(question.get('answer') is None and question.get('assignment') == candidate.get('assignment')
            and ATTEMPT_ID.fullmatch(candidate.get('assignment',''))
            and len(question.get('options', ())) == 3,
            '恢复工单缺少与候选绑定的三个未答选项')
    attempt = runtime/'goal-attempts'/task['id']/candidate['assignment']
    with read_only_board(runtime, app) as (board, capture, _, workbench):
        from superagent.goal_engine import active_assignments, digest
        from superagent.goal_runtime import GoalRuntime

        sealed = capture(attempt/'sealed')[0]
        samples = json.loads((attempt/'samples.json').read_text())
        require(sealed == state['checkpoint'] and sealed['digest'] == candidate['artifact_sha256']
                and samples == {'prompt':question['prompt'], 'options':question['options']}
                and digest({'samples':samples,'checkpoint':sealed}) == question['candidate_digest'],
                '保存的样例或候选已改变')
        summary = board.call('/api/outer')
        waiting = [row for row in summary['drafts'] if row.get('pending_preference')]
        require(len(waiting) == 1 and waiting[0]['id'] == task['id'], '副本未读回唯一待选样例工单')
        details = board.call('/api/outer/get', {'ticket_id':task['id']})
        restored = details.get('execution') or {}
        shown = restored.get('pending_preference') or {}
        require(details['status'] == 'AUTHORIZED' and restored.get('status') == 'awaiting_preference'
                and all(shown.get(key) == question[key] for key in
                        ('id','revision','prompt','options','candidate_digest'))
                and restored['active_candidate'] == candidate, '恢复的偏好问题或样例不一致')
        require(not active_assignments(task), '待答时仍有活动派工')
        runtime_task = GoalRuntime(workbench.store, workbench.hub).tick(task['id'])
        require(runtime_task == task, '待答时调度器改变了派工或业务状态')
        board.call('/api/outer/answer_preference', {'ticket_id':task['id']}, status=405)
        ui = app/'outer-ui'/'goal_team.js'
        if not ui.is_file():
            ui = app/'goal_team.js'
        rendered = subprocess.run(
            ['node','--input-type=module','--eval',_PREFERENCE_UI,str(ui)], check=True, timeout=10,
            text=True, capture_output=True, input=json.dumps({'drafts':waiting,'detail':details}),
        )
        controls = json.loads(rendered.stdout)
        require(controls == {'action':'查看样例','submit':'提交选择','options':3},
                '工作台发布的原单视图未渲染三个选项及按钮')
    return {'pending_questions':len(waiting), 'option_count':len(shown['options']),
            'candidate_digest':shown['candidate_digest'], 'sealed_samples_match':True,
            'decision_queue_action':controls['action'], 'submit_action':controls['submit'],
            'ui_probe':'real_view_module_in_node_dom_fixture', 'browser_used':False,
            'active_assignments':0, 'scheduler_tick_dispatched':False, 'answer_write_rejected':True}


def rehearse(source, ticket_id=None, case='completed'):
    check_stopped(source)
    formal = deployed_listener_pids()
    private = Path(tempfile.mkdtemp(prefix='autoloop-goal-team-recovery-', dir=ISOLATED))
    print(f'本机私有恢复目录 {private}', flush=True)
    snapshot, rehearsal, baseline = copy_snapshot(source, private)
    state = 'awaiting_preference' if case == 'waiting' else 'completed'
    task, ledger = inspect_ledger(rehearsal, ticket_id, state)
    if case == 'waiting':
        require(ledger['approval_count'] == 1 and ledger['mandate_revision'] == 1,
                '待答工单的批准或授权版次增加')
        artifacts = verify_waiting(rehearsal, task)
    else:
        artifacts = verify_artifacts(rehearsal, task)
    check_stopped(source)
    require(tree_content(source) == tree_content(snapshot) == baseline, '原实例或冷备在演练期间发生变化')
    require(formal == deployed_listener_pids() and formal, '无法确认正式工作台监听进程保持一致')
    return {
        'schema_version':1, 'case':f'{case}_team_ticket_cold_recovery', 'synthetic':True,
        **({'source_kind':('engine_checkpoint_with_model_and_hub_fixtures'
                           if (snapshot/'.waiting-recovery-synthetic').exists()
                           else 'stopped_isolated_instance')} if case == 'waiting' else {}),
        'observed_at_utc':datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'ticket_ref_sha256':hashlib.sha256(task['id'].encode()).hexdigest(),
        'cold_backup':{'original_unchanged':True, 'whole_tree_verified':True,
                       'entries':len(baseline), 'wal_files':sum(name.endswith('-wal') for name in baseline)},
        'ledger':ledger, 'read_only_workbench':artifacts, 'formal_listener_unchanged':True,
        'limits':{'waiting_case_validated':case == 'waiting', 'resumed_execution':False},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True, help='已停止的合成隔离实例绝对路径')
    parser.add_argument('--ticket', help='存在多张同阶段工单时，指定工单编号')
    parser.add_argument('--case', choices=('completed','waiting'), default='completed',
                        help='验证已交付成果，或验证待回答的样例问题')
    parser.add_argument('--evidence', type=Path, help='将脱敏验证记录写入新 JSON 文件')
    args = parser.parse_args()
    os.umask(0o077)
    if args.ticket and not TASK_ID.fullmatch(args.ticket):
        parser.error('工单编号无效')
    try:
        evidence = rehearse(args.source, args.ticket, args.case)
        encoded = json.dumps(evidence, ensure_ascii=False, indent=2) + '\n'
        if args.evidence:
            with args.evidence.open('x', encoding='utf-8') as stream:
                stream.write(encoded)
        print(encoded, end='')
    except (RuntimeError, ValueError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        sys.exit(f'冷备演练未通过：{exc}')


if __name__ == '__main__':
    main()
