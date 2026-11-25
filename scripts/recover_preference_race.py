"""冷恢复已停止的偏好竞态合成原单，沿用原中台、授权和试验答复。"""

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
from urllib.parse import urlencode
from uuid import uuid4

from live_filename_index import Board, Isolated, save_json, wait_until
from prepare_formal_outer_pilot import prepare
from recover_goal_team_pilot import check_stopped, copy_snapshot, read_db, require, tree_content


PROTECTED = {8765, 60966, 61498, 59777}
TEMP_ROOT = Path('/private/tmp')


def protected_listeners():
    result = {}
    for port in sorted(PROTECTED):
        status = subprocess.run(['/usr/sbin/lsof','-nP','-t',f'-iTCP:{port}','-sTCP:LISTEN'],
                                capture_output=True,text=True,timeout=5)
        require(status.returncode in (0,1) and not status.stderr, '无法核对受保护监听')
        result[port] = sorted(int(row) for row in status.stdout.split())
    return result


def checkpoint(backup):
    workbench = backup/'workbench'
    with read_db(workbench/'tasks.sqlite3') as db:
        require(db.execute('PRAGMA integrity_check').fetchone()[0]=='ok', '工作台冷备检查失败')
        rows = db.execute('SELECT body FROM tasks').fetchall()
        require(len(rows)==1, '需要原竞态试验的唯一已批准执行工单')
        task = json.loads(rows[0][0])
        outer = json.loads(db.execute('SELECT body FROM outer_tickets WHERE id=?',
                                      (task['id'],)).fetchone()[0])
        approvals = db.execute('SELECT COUNT(*) FROM outer_revisions WHERE ticket=? AND kind=?',
                               (task['id'],'approval_snapshot')).fetchone()[0]
    flow = task['goal_execution']
    answer = flow['pending_preference']['answer']
    acceptance = json.loads((backup/'acceptance.json').read_text())
    require(acceptance.get('synthetic') is True and acceptance.get('mode')=='preference-race',
            '冷备不是专用的偏好竞态合成试验')
    check = {key:acceptance[key] for key in ('late_preference','waiting_card','cross_channel')}
    require(approvals==outer['mandate_revision']==1 and
            outer['mandate']['operator_kind']==answer['operator_kind']=='acceptance_test'
            and sum(d['kind']=='preference_answered' for d in flow['decisions'])==1
            and not check['late_preference']['answerable_question']
            and check['cross_channel']['successful_submissions']==1,
            '冷备中的批准、合成答复或旧回执约束不符')
    old = flow['assignments'][-1]
    require(task['step']==1 and len(task['plan']['steps'])==3 and len(flow['stage_deliveries'])==1 and old['run']==6
            and old['state']=='dispatched' and old['phase']=='execute',
            '冷备不在第 2 步的旧 Run 6 对账点')
    attempt = workbench/'goal-attempts'/task['id']/old['id']
    report = json.loads((attempt/'report.json').read_text())
    require(report['run']==old['run'] and report['id']==old['id']
            and report['outer_binding']==old['outer_binding']
            and report.get('error') and not report.get('checkpoint') and not report.get('delivery'),
            '旧回执必须与 Run 6 相符，且没有候选或终验')
    with read_db(backup/'hub'/'hub.db') as db:
        require(db.execute('PRAGMA quick_check').fetchone()[0]=='ok','Hub 冷备检查失败')
        states = {row['id']:row['state'] for row in db.execute('SELECT id,state FROM runs WHERE lane=?',
                                                              (task['id'],))}
        require(states=={1:'done',2:'done',3:'done',4:'done',5:'done',6:'claimed'},
                '冷备 Hub 未保留预期原账本')
    entry = backup/'worker'/'worker'/'runs'/'6-1'
    require((entry/'entry.json').is_file() and (entry/'exit-1').is_file(), '原 Worker 缺少用于对账的真实退出凭证')
    return task, outer, check


class RestoredPilot(Isolated):
    def __init__(self, source, manifest, stack, codex_home, proxy):
        self.root, self.stack = source, stack
        self.app, self.runtime = source/'formal-board', source/'workbench'
        self.hub, self.worker = source/'hub', source/'worker'
        self.port = int(manifest['board'].rsplit(':',1)[1])
        self.hub_port = manifest['hub_port']
        require(not {self.port,self.hub_port}.intersection(PROTECTED), '禁止操作受保护端口')
        self.env = {key:os.environ[key] for key in ('PATH','LANG','LC_ALL','LC_CTYPE') if key in os.environ}
        self.env.update(CODEX_HOME=str(codex_home),HOME=str(source/'role-home'),
                        AUTOLOOP_OUTER_PROXY_PORT=str(proxy),TMPDIR=str(source/'tmp'),SHELL='/bin/sh')
        install = Path.home()/'.agent-autoloop'
        node, cli = install/'runtime/node-v24.22.2/bin/node', install/'app/bin/agent-autoloop.ts'
        require(node.is_file() and cli.is_file(), '找不到原中台已安装的兼容版本')
        self.cli = [str(node),'--disable-warning=ExperimentalWarning',str(cli)]
        for name, root, port in (('Hub',self.hub,self.hub_port), ('Worker',self.worker,self.hub_port)):
            local = json.loads((root/'config.json').read_text())
            require(local.get('url')==f'http://127.0.0.1:{port}',f'{name} 的隔离地址不符')
            if name == 'Hub':
                require(local.get('hub',{}).get('listen')==f'127.0.0.1:{port}', 'Hub 的监听隔离地址不符')

    def start_readable(self):
        self.start([*self.cli,'up'],'resumed-hub',self.instance_env(self.hub))
        wait_until(lambda:self.ready(f'http://127.0.0.1:{self.hub_port}/healthz'),15,'恢复 Hub 未就绪')
        trial_token = (self.root/'trial-operator-token').read_text().strip()
        bridge_token = (self.root/'dsh-bridge-token').read_text().strip()
        self.start([sys.executable,'-B',str(self.app/'server.py'),'--port',str(self.port),
                    '--runtime',str(self.runtime),'--hub-home',str(self.hub),'--goal-pilot',
                    '--outer-intelligence','--outer-goal-team'], 'resumed-board',
                   dict(self.env,AUTOLOOP_TRIAL_OPERATOR_TOKEN=trial_token,
                        AUTOLOOP_DSH_BRIDGE_TOKEN=bridge_token))
        wait_until(lambda:self.ready(f'http://127.0.0.1:{self.port}/api/session'),15,'恢复工作台未就绪')
        board, dsh = Board(self.port), Board(self.port)
        board.headers['X-Odyssey-Trial-Operator'] = trial_token
        dsh.headers = {'Content-Type':'application/json', 'Authorization':'Bearer '+bridge_token,
                       'X-Odyssey-Trial-Operator':trial_token}
        return board,dsh


def read_ticket(board, ticket):
    return board.call('/api/outer/get',{'ticket_id':ticket})


def control(board, operation, ticket):
    current = read_ticket(board,ticket)
    return board.call('/api/outer/'+operation,{'ticket_id':ticket,'revision':current['revision'],
                                              'request_id':str(uuid4())})


def reconcile(pilot, board, task, authorized):
    sys.path.insert(0,str(pilot.app))
    from superagent.goal_runtime import GoalRuntime, ensure_process_exit
    from workbench import Hub, Problem, Store

    ticket = task['id']
    old = task['goal_execution']['assignments'][-1]
    ensure_process_exit(pilot.runtime,ticket,old['id'])
    require(read_ticket(board,ticket)['mandate']==authorized['mandate'], '恢复后原授权与冷备不同')
    control(board,'pause',ticket)
    pilot.worker_process = pilot.start_worker()
    runtime = GoalRuntime(Store(pilot.runtime),Hub(pilot.hub))

    def paused():
        # 只等待原 Worker 用已有退出记录和原 claim 完成真实传输；不会生成回执。
        try:
            item = runtime.tick(ticket)
        except Problem as error:
            if error.status not in (400,409,503):
                raise
            return None
        return item if item['status']=='paused' else None

    current = wait_until(paused,55,'原 Run 6 尚未真实结算，未启动新派工')
    flow = current['goal_execution']
    settled = flow['assignments'][-1]
    require(settled['id']==old['id'] and settled['state']=='settled'
            and settled['transport'] in ('done','failed','cancelled')
            and settled['report'] and settled['report'].get('error')
            and flow['checkpoint']==task['goal_execution']['checkpoint']
            and flow['pending_preference']['answer']==task['goal_execution']['pending_preference']['answer'],
            '原 Run 6 对账替换了候选或原答复')
    ensure_process_exit(pilot.runtime,ticket,old['id'])
    print(f"原 Run 6 已对账，Hub {settled['transport']}，旧失败回执留档，原已验阶段保留",flush=True)
    control(board,'resume',ticket)
    return runtime,settled['transport']


def retry_settled(pilot, board, task, authorized):
    sys.path.insert(0,str(pilot.app))
    from superagent.goal_engine import active_assignments
    from superagent.goal_runtime import GoalRuntime, ensure_process_exit
    from workbench import Hub, Store

    ticket = task['id']
    store, hub = Store(pilot.runtime),Hub(pilot.hub)
    latest = store.get(ticket)
    before, after = task['goal_execution'],latest['goal_execution']
    old = before['assignments'][-1]
    reconciled = next(row for row in after['assignments'] if row['id']==old['id'])
    failed = after['assignments'][-1]
    require(latest['status']=='blocked' and not active_assignments(latest)
            and failed['run']>old['run'] and failed['state']=='settled'
            and (failed.get('report') or {}).get('error') and after['stage_deliveries'][0]==before['stage_deliveries'][0]
            and after['pending_preference']['answer']==before['pending_preference']['answer']
            and read_ticket(board,ticket)['mandate']==authorized['mandate'],
            '原单不能在现存授权下从真实已结算失败继续')
    hub_result = hub.request('GET',f"/v1/events/{failed['event']}",source=True)['runs']
    require(reconciled['state']=='settled' and reconciled['report'].get('error')
            and len(hub_result)==1 and hub_result[0]['run']==failed['run']
            and hub_result[0]['state'] in ('done','failed','cancelled'),
            '不能重试未对账的 Hub Run')
    ensure_process_exit(pilot.runtime,ticket,failed['id'])
    control(board,'pause',ticket)
    pilot.worker_process = pilot.start_worker()
    control(board,'resume',ticket)
    print(f"原授权继续，已结算的失败 Run {failed['run']} 已保留",flush=True)
    return GoalRuntime(store,hub),reconciled['transport']


def finish(runtime, board, dsh, task, authorized):
    ticket = task['id']
    previous = task['goal_execution']
    last = None

    def candidate():
        nonlocal last
        current = runtime.tick(ticket)
        flow = current['goal_execution']
        key = (current['status'],current['step'],len(flow['assignments']))
        if key != last:
            print('原工单推进',key,flush=True)
            last = key
        require(current['status'] not in ('blocked','budget_exhausted'), '续办受阻：'+str(current.get('error','')))
        return current if current['status']=='awaiting_artifact_review' else None

    staged = wait_until(candidate,720,'等待原工单剩余步骤与独立验收超时')
    state = staged['goal_execution']
    records = state['assignments'][len(previous['assignments']):]
    require(all(row['run']>6 and row['step'] in (1,2) and row['state']=='settled' for row in records),
            '旧轮结束后预期只结算剩余阶段')
    require(state['stage_deliveries'][0]==previous['stage_deliveries'][0]
            and sum(d['kind']=='authorized' for d in state['decisions'])==1
            and sum(d['kind']=='preference_answered' for d in state['decisions'])==1,
            '续办重复了批准或答复，或替换了前序交付')
    require(state['pending_preference']['answer']==previous['pending_preference']['answer'],
            '原试验答复发生变化')
    roles = [[{'role':role['role'],'model':role.get('model'),'effort':role.get('effort'),
               'sandbox':role.get('sandbox'),'status':role['status'],
               'protocol_confirmed':bool(role.get('permission_digest') and role.get('turn')),
               'error_code':role.get('error_code')} for role in row.get('role_calls',[])] for row in records]
    reviews = [row for row in records if row['phase']=='review'
               and (row.get('report') or {}).get('result',{}).get('passed') is True]
    require([row['step'] for row in reviews]==[1,2], '缺少两个剩余阶段的独立机器复验')
    for step in (1,2):
        require(any(row['step']==step and row['phase'] in ('execute','repair')
                    and (row.get('report') or {}).get('checkpoint')
                    and any(role['protocol_confirmed'] and role['status']=='completed' for role in group)
                    for row,group in zip(records,roles)), '剩余步骤缺少实际 App Server 候选')
    checks = [[{key:item.get(key) for key in ('id','verifier_id','status','passed')}
               for item in row['report']['validation']['checks']] for row in reviews]
    require(any(item['status']=='PASS' and item['verifier_id']!='human-review' for item in checks[-1])
            and reviews[-1]['report']['result']['passed'],
            '终轮独立机器验收未通过')
    review = state['artifact_reviews'][-1]
    details = read_ticket(board,ticket)
    require(details['mandate']==authorized['mandate'] and review['state']=='pending',
            '原授权版本或待签收候选不符')
    dsh.call('/api/outer-dsh/accept_artifact',{'ticket_id':ticket,'revision':details['revision'],
             'candidate':review['assignment'],'artifact_sha256':review['artifact_sha256'],
             'request_id':str(uuid4())})
    final = read_ticket(board,ticket)['execution']
    accepted = final['goal_execution']['artifact_reviews'][-1]
    require(final['status']=='completed' and accepted['state']=='accepted'
            and accepted.get('operator_kind')=='acceptance_test',
            '只允许试验操作员签收同一独立核验的候选')
    path = 'report.html'
    query = urlencode({'path':path,'candidate':review['assignment']})
    web = board.call(f'/api/tasks/{ticket}/artifact?{query}',raw=True)
    paired = dsh.call('/api/outer-dsh/download',{'ticket_id':ticket,'candidate':review['assignment'],
                                                'path':path},raw=True)
    digest = hashlib.sha256(web).hexdigest()
    require(web==paired and digest==review['files'][path]
            and review['artifact_sha256']==reviews[-1]['report']['checkpoint']['digest'],
            '两端下载、签收和末轮归档未绑定同一版本')
    return {'new_runs':[{'run':row['run'],'step':row['step']+1,'phase':row['phase'],'roles':role_rows}
                        for row,role_rows in zip(records,roles)],
            'machine_checks':checks,'artifact_sha256':review['artifact_sha256'],'file_sha256':digest,
            'web_and_paired_dsh_bytes_match':True,'signoff_operator':'acceptance_test'}


def run(source, local, codex_home, proxy, output, retry=False):
    check_stopped(source)
    protected = protected_listeners()
    require(all(protected.values()),'无法确认受保护实例的基线监听')
    manifest = json.loads((source/'instance.json').read_text())
    snapshot, rehearsal = local/'snapshot', local/'rehearsal'
    preserved = local/'before-restore'
    if snapshot.exists():
        baseline = tree_content(snapshot)
        checkpoint_root = preserved if retry else source
        require(tree_content(checkpoint_root)==baseline, '原件已不同于停机冷备')
    else:
        require(not retry,'只能重试已经恢复的原单')
        snapshot,rehearsal,baseline = copy_snapshot(source,local)
    task, authorized, transport_checks = checkpoint(rehearsal)
    app = local/f'candidate-board-{uuid4().hex}'
    prepare(snapshot/'formal-source',app)
    checkout = Path(__file__).resolve().parents[1]
    revision = subprocess.check_output(['git','rev-parse','HEAD'],cwd=checkout,
                                       text=True).strip()
    role_runner = Path('superagent/goal_team_run.py')
    installed = (app/role_runner).read_bytes()
    require(installed==(checkout/'网页工作台'/role_runner).read_bytes(), '候选未包含当前角色修复')
    check_stopped(source)
    require(tree_content(preserved if retry else source)==baseline, '原件在候选准备期间发生变化')
    if not retry:
        source.rename(preserved)
        shutil.copytree(snapshot,source,symlinks=True)
        require(tree_content(source)==baseline, '恢复后字节与冷备不符')
    (source/'formal-board').rename(local/f'previous-installed-board-{uuid4().hex}')
    shutil.copytree(app,source/'formal-board',symlinks=True)
    (source/'worker-driver.py').write_text(
        'from pathlib import Path\nimport sys\n'
        'sys.path.insert(0, sys.argv[2])\n'
        'from superagent.goal_runtime import worker\nfrom workbench import Store\n'
        'worker(Store(Path(sys.argv[3])), None)\n')
    try:
        with ExitStack() as stack:
            pilot = RestoredPilot(source,manifest,stack,codex_home,proxy)
            board,dsh = pilot.start_readable()
            save_json(source/'instance.json',{**manifest,'phase':'ready'})
            continue_ticket = retry_settled if retry else reconcile
            runtime,old_terminal = continue_ticket(pilot,board,task,authorized)
            outcome = finish(runtime,board,dsh,task,authorized)
    finally:
        save_json(source/'instance.json',manifest)
    check_stopped(source)
    require(protected_listeners()==protected and tree_content(preserved)==tree_content(snapshot)==baseline,
            '原备份或受保护监听发生变化')
    evidence = {'schema_version':1,'case':'F4/F3 original synthetic ticket cold resumption',
                'observed_at_utc':datetime.now(timezone.utc).isoformat(timespec='seconds'),
                'candidate_base_commit':revision,'installed_role_runner_sha256':hashlib.sha256(installed).hexdigest(),
                'synthetic':True,'approval_count':1,'effective_preference_count':1,
                'pre_stop_protocol_checks':transport_checks,'old_run':{'run':6,'prior_hub_state':'claimed',
                    'reconciled_hub_state':old_terminal,'admitted_as_final_verification':False},
                'original_snapshot_preserved':True,'protected_listeners_unchanged':True,
                'actual_resume':outcome}
    with output.open('x',encoding='utf-8') as stream:
        stream.write(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(evidence,ensure_ascii=False,indent=2),flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--cold-backup',type=Path,required=True)
    parser.add_argument('--codex-home',type=Path,required=True)
    parser.add_argument('--proxy',type=int,required=True)
    parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--retry-settled',action='store_true',help='从已对账的真实失败续办，不覆盖恢复后的账本')
    args = parser.parse_args()
    os.umask(0o077)
    directories = (args.cold_backup,args.codex_home)
    require(all(path.resolve()==path and path.parent==TEMP_ROOT and path.is_dir() for path in directories)
            and args.cold_backup.name.startswith('autoloop-preference-race-resume-')
            and 0 < args.proxy <= 65535 and args.proxy not in PROTECTED,
            '须提供私有冷备、隔离登录目录和已获授权的代理')
    signal.signal(signal.SIGTERM,lambda *_:sys.exit(130))
    run(args.source,args.cold_backup,args.codex_home,args.proxy,args.evidence,args.retry_settled)


if __name__ == '__main__':
    main()
