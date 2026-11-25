"""在本次新建的合成试点冷备后，用真实隔离 Worker 续办。"""

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4

from live_filename_index import Board, Isolated, save_json, wait_until
from prepare_formal_outer_pilot import prepare
from recover_goal_team_pilot import check_stopped, copy_snapshot, inspect_ledger, require, tree_content


ROOT = Path(__file__).resolve().parents[1]
PROTECTED = (8765, 60966, 61498)
EXPECTED = {'selection': 'b', 'note': 'recovery-fixture-42'}
OPTIONS = [
    {'id':'a','label':'甲：先倾听','text':'我听见你担心别人误会。'},
    {'id':'b','label':'乙：回应进展','text':'你已经补发正确文件，可以保留这一步。'},
    {'id':'c','label':'丙：陪你理清','text':'哪部分还让你担心？'},
]
SYNTHETIC_ANSWER = {
    'option_id':'b',
    'text':'试验操作员固定选择乙。请写 result.json，selection 字符串为 b，note 字符串为 recovery-fixture-42。',
}


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()


def protected_listeners():
    listeners = {}
    for port in PROTECTED:
        result = subprocess.run(['/usr/sbin/lsof','-t','-nP',f'-iTCP:{port}','-sTCP:LISTEN'],
                                capture_output=True, text=True, timeout=5)
        require(result.returncode in (0,1), '无法核对受保护工作台监听进程')
        listeners[port] = sorted(int(pid) for pid in result.stdout.split())
    return listeners


def isolated_worker(app, runtime):
    root = runtime.parent
    require(root.parent == Path('/private/tmp') and root.name.startswith('autoloop-goal-team-resume-')
            and app == root/'formal-board' and (root/'.recovery-resume-fixture').is_file(),
            '仅允许本次专用合成试点运行 Worker')
    sys.path.insert(0, str(app))
    from superagent import goal_runtime
    from workbench import Store

    mode = (root/'mode').read_text()
    require(mode in ('waiting-fixture','recovered-live'), '恢复验收模式无效')
    if mode == 'recovered-live':
        return goal_runtime.worker(Store(runtime), None)
    calls = []

    def role_response(**kwargs):
        require(kwargs['sandbox'] == 'read-only', '等待夹具不能写入候选')
        calls.append(kwargs['model'])
        return {
            'result':{'summary':'你想保留哪一组合成回应？',
                      'action':'ask' if len(calls) in (1,5) else 'done',
                      'options':OPTIONS, 'retain':[], 'verify':[]},
            'model':kwargs['model'], 'sandbox':kwargs['sandbox'], 'effort':kwargs['effort'],
            'session':str(uuid4()), 'turn':str(uuid4()), 'started_ns':time.time_ns(),
            'ended_ns':time.time_ns(), 'usage':None, 'tools':[],
        }

    plan = {'result':{'summary':'先保存样例，后按试验意见出 JSON',
                      'steps':[{'objective':'交付 result.json 并验证','checks':['result','owner-choice']}]},
            'model':'gpt-6.2-sol', 'session':str(uuid4()), 'started_ns':time.time_ns(),
            'ended_ns':time.time_ns(), 'usage':None}
    with patch('superagent.goal_codex.invoke',return_value=plan), patch(
            'superagent.goal_app_server.invoke',side_effect=role_response):
        return goal_runtime.worker(Store(runtime), None)


class RecoveryPilot(Isolated):
    def start_worker(self):
        # 启动器与正式规则共用。入口只在本次临时目录选择夹具或真实执行。
        driver = self.root/'worker-driver.py'
        driver.write_text('from pathlib import Path\nimport sys\n'
                          f'sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n'
                          'from recover_goal_team_resume import isolated_worker\n'
                          'isolated_worker(Path(sys.argv[2]), Path(sys.argv[3]))\n')
        return super().start_worker()

    def start_coordinator(self):
        self.start([sys.executable,'-B',str(self.app/'superagent'/'goal_runtime.py'),
                    'serve','--runtime',str(self.runtime),'--hub',str(self.hub)], 'coordinator')

    def restart_from_snapshot(self, stack, trial_token):
        self.stack = stack
        (self.root/'mode').write_text('recovered-live')
        self.start([*self.cli,'up'],'hub',self.instance_env(self.hub))
        wait_until(lambda: self.ready(f'http://127.0.0.1:{self.hub_port}/healthz'),15,'恢复 Hub 未就绪')
        self.worker_process = self.start_worker()
        self.start([sys.executable,'-B',str(self.app/'server.py'),'--port',str(self.port),
                    '--runtime',str(self.runtime),'--hub-home',str(self.hub),'--goal-pilot',
                    '--outer-intelligence','--outer-goal-team'], 'board',
                   dict(self.env,AUTOLOOP_TRIAL_OPERATOR_TOKEN=trial_token))
        wait_until(lambda: self.ready(f'http://127.0.0.1:{self.port}/api/session'),15,'恢复工作台未就绪')
        board = Board(self.port)
        board.headers['X-Odyssey-Trial-Operator'] = trial_token
        self.start_coordinator()
        return board


def prepare_approval(pilot):
    sys.path[:0] = [str(pilot.app), str(ROOT/'网页工作台')]
    from outer_workbench import create_task
    from superagent.test_outer_intake import Model
    from workbench import Store, Workbench

    class IntakeFixture(Model):
        def __call__(self, role, context, schema):
            result = super().__call__(role,context,schema)
            if role == 'acceptance_designer':
                prototype = result['criteria'][0]
                result['artifact_types'] = ['json']
                result['criteria'] = [
                    dict(prototype,criterion_id='result',description='实际核对 result.json 的完整 JSON 对象',
                         verifier_id='json-artifact',test_spec_json='{"path":"result.json"}',
                         expected_result_json=json.dumps(EXPECTED)),
                    dict(prototype,criterion_id='owner-choice',description='由试验操作者选择合成回应',
                         verification_type='HUMAN_REVIEW',verifier_id='human-review',
                         expected_result_json='null',human_review_required=True),
                ]
                result['human_review_requirements'] = ['owner-choice']
            return result

    wb = Workbench(Store(pilot.runtime), goal_enabled=True, outer_enabled=True,
                   outer_model=IntakeFixture(),outer_workbench_entry=True)
    wb.outer_goal_team = True
    event = create_task(wb, {'workflow':'outer','request_id':str(uuid4()),'name':'合成样例恢复续办',
                        'goal':'试验操作者先看三个合成回应并做选择。收到选择后写 result.json，'
                               '只含 selection 和 note 两个字符串字段。selection 为已选样例 ID，'
                               'note 固定为 recovery-fixture-42。',
                        'acceptance':'选择乙后，result.json 的对象恰为 '
                                     + json.dumps(EXPECTED,ensure_ascii=False)
                                     + '。合成样例的选择和报告的人工签收由试验操作者分别确认。'})
    outer = wb.outer()
    outer.drain()
    row = outer.event(event['event_id'],'owner')
    draft = outer.get(row['ticket_id'],'owner')
    require(draft['status']=='AWAITING_APPROVAL', '合成验收未达到可批准状态')
    return draft


def poll_detail(board, ticket, target, timeout):
    def ready():
        draft = board.call('/api/outer/get', {'ticket_id':ticket})
        execution = draft.get('execution') or {}
        require(execution.get('status') not in ('blocked','budget_exhausted'),
                '续办执行受阻：' + execution.get('error','原因未知'))
        return draft if execution.get('status') == target else None
    return wait_until(ready,timeout,'隔离试单未到达状态 ' + target)


def collect_resume(board, waiting):
    ticket = waiting['id']
    before = waiting['execution']
    restored = poll_detail(board,ticket,'awaiting_preference',15)
    question = restored['execution']['pending_preference']
    require(restored['mandate']==waiting['mandate'] and
            question==before['pending_preference'] and len(question['options'])==3,
            '冷恢复后待答或原授权版本不同')
    answer = {'ticket_id':ticket,'revision':restored['revision'],'question_id':question['id'],
              'question_revision':question['revision'],'candidate_digest':question['candidate_digest'],
              'request_id':str(uuid4()),'answer':SYNTHETIC_ANSWER}
    first = board.call('/api/outer/answer_preference',answer)
    duplicate = board.call('/api/outer/answer_preference',answer)
    require(first['action']=='PREFERENCE_RECORDED' and duplicate['duplicate'],
            '合成答复未成功登记或重放产生了另一项答复')
    resumed = poll_detail(board,ticket,'awaiting_artifact_review',260)
    execution = resumed['execution']
    state = execution['goal_execution']
    require(resumed['mandate']==waiting['mandate'] and resumed['mandate_revision']==1
            and not execution.get('pending_preference'), '续办改变了原授权或重复呈现待答')
    events = [row for row in state['decisions'] if row['kind']=='preference_answered']
    require(len(events)==1 and events[0].get('operator_kind')=='acceptance_test',
            '试验操作员答复没有按一次登记')
    new = state['assignments'][len(before['goal_execution']['assignments']):]
    require([row['phase'] for row in new]==['execute','review'] and all(
             row['state']=='settled' and row['transport']=='done' for row in new),
            '答复后预期由真实 Worker 完成一次执行和一次独立复验')
    roles = [{**{key:call.get(key) for key in ('role','requested_model','model','effort','sandbox','status',
                                              'cli_version','permission_digest','usage')},
              'turn_sha256':fingerprint(call['turn'])} for call in new[0]['role_calls']]
    require(roles and all(role['model']==role['requested_model']=='gpt-6.2-sol'
                         and role['status']=='completed' and role['permission_digest'] for role in roles),
            '续办角色缺少实际 App Server 确认凭证')
    review = new[1]['report']
    candidate = state['artifact_reviews'][-1]
    checks = [{key:row.get(key) for key in ('id','verifier_id','status','passed')}
              for row in review['validation']['checks']]
    require(review['result']['passed'] and any(row['id']=='result' and row['status']=='PASS' for row in checks)
            and candidate['state']=='pending', '独立机器验收或人工签收状态不符')
    query = urlencode({'path':'result.json','candidate':candidate['assignment']})
    content = board.call(f'/api/tasks/{ticket}/artifact?{query}',raw=True)
    file_sha = hashlib.sha256(content).hexdigest()
    require(json.loads(content)==EXPECTED and file_sha==candidate['files']['result.json']
            and candidate['artifact_sha256']==review['checkpoint']['digest'],
            '实际复验或下载与本次续办候选不同')
    return {'fixture_answer_replayed_idempotently':True, 'answer_operator':'acceptance_test',
            'mandate_revision':resumed['mandate_revision'], 'before_waiting_roles':len(before['team_activity']),
            'resumed_phases':[row['phase'] for row in new], 'resumed_roles':roles,
            'final_state':execution['status'], 'independent_machine_checks':checks,
            'candidate_sha256':candidate['artifact_sha256'], 'file_sha256':file_sha,
            'manual_signoff_performed':False}


def run(formal, codex_home, proxy, evidence_path):
    protected = protected_listeners()
    require(protected[8765], '无法确认正式工作台仍在监听')
    source = Path(tempfile.mkdtemp(prefix='autoloop-goal-team-resume-',dir='/private/tmp'))
    print(f'恢复续办专用合成实例 {source}', flush=True)
    (source/'.recovery-resume-fixture').touch(mode=0o600)
    app = source/'formal-board'
    prepare(formal,app)
    trial_token = secrets.token_hex(32)
    with ExitStack() as original_stack:
        pilot = RecoveryPilot(app,source,original_stack)
        require(not set((pilot.port,pilot.hub_port)).intersection(PROTECTED),'试点端口触及受保护工作台')
        home = source/'role-home'
        home.mkdir(mode=0o700)
        pilot.env.update(HOME=str(home),CODEX_HOME=str(codex_home),AUTOLOOP_OUTER_PROXY_PORT=str(proxy))
        draft = prepare_approval(pilot)
        board = pilot.launch('waiting-fixture',goal_team=True,trial_token=trial_token)
        pilot.start_coordinator()
        approved = board.call('/api/outer/approve',{'ticket_id':draft['id'],'revision':draft['revision'],
                   'goal_revision':draft['goal_revision'],'acceptance_revision':draft['acceptance_revision'],
                   'request_id':str(uuid4()),'require_signoff':True})
        require(approved['mandate']['operator_kind']=='acceptance_test','未启用试验操作者')
        waiting = poll_detail(board,draft['id'],'awaiting_preference',90)
    manifest = {'phase':'stopped','board':board.url,'hub_port':pilot.hub_port,
                'workbench':str(pilot.runtime),'instance':str(source)}
    save_json(source/'instance.json',manifest)
    check_stopped(source)
    private = Path(tempfile.mkdtemp(prefix='autoloop-goal-resume-backup-',dir='/private/tmp'))
    print(f'冷备与恢复前原件 {private}',flush=True)
    snapshot, rehearsal, baseline = copy_snapshot(source,private)
    _, ledger = inspect_ledger(rehearsal,draft['id'],'awaiting_preference')
    require(ledger['approval_count']==1 and ledger['matched_completed_runs']==2,'等待态的 Hub 派工不一致')
    # 原授权绑定绝对路径。仅归档本次新建的可丢弃试点原件，再恢复到其原路径。
    original = private/'before-restore'
    source.rename(original)
    shutil.copytree(snapshot,source,symlinks=True)
    require(tree_content(source)==baseline,'恢复文件不一致')
    with ExitStack() as restored_stack:
        board = pilot.restart_from_snapshot(restored_stack,trial_token)
        result = collect_resume(board,waiting)
    check_stopped(source)
    require(tree_content(original)==tree_content(snapshot)==baseline,'恢复前原件或冷备发生变化')
    observed = protected_listeners()
    require(protected==observed,'受保护监听进程在演练期间发生变化')
    payload = {'schema_version':1,'synthetic':True,'ticket_ref_sha256':fingerprint(draft['id']),
               'cold_recovery':{'tree_unchanged':True,'original_absolute_path_reused':True,**ledger},
               'waiting':{'intake_and_model_outputs':'fixtures','hub_worker':'actual'},
               'resumption':{'hub_worker':'actual','model_transport':'actual_codex_app_server',**result},
               'protected_listener_processes_unchanged':True}
    with evidence_path.open('x',encoding='utf-8') as out:
        out.write(json.dumps(payload,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(payload,ensure_ascii=False,indent=2),flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--formal-source',type=Path,required=True)
    parser.add_argument('--codex-home',type=Path,required=True)
    parser.add_argument('--proxy',type=int,required=True)
    parser.add_argument('--evidence',type=Path,required=True)
    args = parser.parse_args()
    formal, codex_home = args.formal_source.resolve(strict=True), args.codex_home.resolve(strict=True)
    require(all(Path('/private/tmp') in path.parents and path.is_dir() for path in (formal,codex_home))
            and 0 < args.proxy <= 65_535 and args.proxy not in PROTECTED,
            '需要独立源码副本和已经授权的隔离 Codex 配置')
    os.umask(0o077)
    def stop(_signum,_frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,stop)
    run(formal,codex_home,args.proxy,args.evidence)


if __name__ == '__main__':
    main()
