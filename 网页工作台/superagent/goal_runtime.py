"""独立运行的目标协调器与 Autoloop 单轮 Worker。"""
import argparse
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from workbench import Hub, Problem, Store, Workbench, is_blind_goal
from superagent.goal_engine import (
    StaleCoordination,
    PACKAGE_LIMIT, GoalEngine, active_assignments, assignment_route, canonical, current, digest, flow,
    report_matches_assignment, team_candidate_assignment,
)


TERMINAL_RUNS = ('done', 'nothing', 'cancelled')
TEAM_RECOVERY_FILE = 'candidate-recovery.json'
TEAM_RECOVERY_VERSION = 1


def missing_receipt_diagnostic(run, assignment):
    # 中台错误可能含模型输出、路径或凭据。仅记录固定类别和有界数值。
    error = run.get('error')
    reason = 'no_hub_error'
    if type(error) is str and error:
        preview = error[:500].lower()
        reason = ('timeout' if any(word in preview for word in ('timeout', 'timed out', '超时'))
                  else 'hub_error_redacted')
    attempts = run.get('attempts')
    seconds = assignment.get('max_seconds')
    return {
        'assignment': assignment['id'], 'run': run['run'], 'terminal': run['state'],
        'reason': reason,
        'hub_attempts': min(attempts, 1000) if type(attempts) is int and attempts >= 0 else None,
        'reserved_seconds': min(seconds, 86400) if type(seconds) is int and seconds >= 0 else None,
    }


def hub_confirmed_unstarted(hub, run, rule):
    # 来源接口不区分排队取消和领取后取消。以本人的中台轮次时间线确认。
    try:
        facts = hub.request('GET',f'/v1/runs/{run}/why')
    except Problem as error:
        if error.status >= 500 and error.status != 501:
            raise
        return False
    if not isinstance(facts,list):
        return False
    states = []
    for fact in facts:
        if (not isinstance(fact,dict) or fact.get('version') != 1
                or not isinstance(fact.get('changes'),list)):
            return False
        for change in fact['changes']:
            if not isinstance(change,dict):
                return False
            if change.get('table') != 'runs' or change.get('key') != str(run):
                continue
            row = change.get('row')
            if (not isinstance(row,dict) or row.get('id') != run or row.get('rule') != rule
                    or 'claimed_at' not in row or row['claimed_at'] is not None
                    or type(row.get('attempts')) is not int or row['attempts'] != 0
                    or row.get('state') not in ('queued','cancelled')):
                return False
            states.append(row['state'])
    return len(states) >= 2 and states[-1] == 'cancelled' and all(state == 'queued' for state in states[:-1])


def attempt_file(root, ticket, key, name):
    # ticket 与派工 ID 在可信账本核对后传到这里。
    return root / 'goal-attempts' / ticket / key / name


@contextmanager
def package_lease(directory):
    with (directory/'package.lock').open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield


def manifest(workspace):
    paths = []
    for path in sorted(workspace.rglob('*')):
        relative = path.relative_to(workspace)
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise Problem('工作区含不支持的文件类型')
        if stat.S_ISREG(mode) and relative.name != 'AGENTS.md':
            if path.stat().st_size > 8*1024*1024 or len(paths) >= 200:
                raise Problem('工作区超出试点检查范围')
            paths.append((str(relative),hashlib.sha256(path.read_bytes()).hexdigest()))
    return {'digest':digest(paths),'artifacts':[name for name,_ in paths],'files':dict(paths)}


def save_json(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent,mode='w',encoding='utf-8',delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary,path)
        descriptor = os.open(path.parent,os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_json(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('回执须为普通文件')
    return json.loads(path.read_text(encoding='utf-8'))


def save_team_recovery(assignment, directory, report):
    if (not team_candidate_assignment(assignment) or not report_matches_assignment(report,assignment)
            or report.get('error')):
        return
    # 先保存与派工及候选绑定的完整回执，再对中台确认本轮完成。
    save_json(directory/TEAM_RECOVERY_FILE, {
        'schema_version':TEAM_RECOVERY_VERSION,
        'ticket':assignment['ticket'],
        'mandate_digest':assignment['mandate_digest'],
        'report':report,
    })


def read_team_recovery(directory, assignment):
    entered = directory/'entered'
    record = read_json(directory/TEAM_RECOVERY_FILE)
    if (entered.is_symlink() or not entered.is_file() or entered.read_text() != str(assignment['run'])
            or not isinstance(record,dict) or type(record.get('schema_version')) is not int
            or record['schema_version'] != TEAM_RECOVERY_VERSION
            or record.get('ticket') != assignment['ticket']
            or record.get('mandate_digest') != assignment['mandate_digest']
            or not report_matches_assignment(record.get('report'), assignment)
            or record['report'].get('error')):
        raise Problem('原派工的团队恢复回执无法核对')
    return record['report']


def team_samples(report):
    handoff = report.get('team_handoff')
    instruction = handoff.get('instruction') if isinstance(handoff,dict) else None
    if isinstance(instruction,dict) and instruction.get('action') == 'ask':
        return {'prompt':instruction.get('summary'), 'options':instruction.get('options')}
    return report.get('preference')


def verify_team_seal(directory, report, task=None, assignment=None):
    from superagent.outer_verification import capture
    sealed = directory/'sealed'
    if sealed.is_symlink():
        raise Problem('封存版本与回执不一致')
    checkpoint,files,_ = capture(sealed)
    if checkpoint != report.get('checkpoint'):
        raise Problem('封存版本与回执不一致')
    if 'team_handoff' in report:
        from superagent.goal_team_handoff import validate_draft_observations, validate_handoff_scopes
        handoff = report['team_handoff']
        reviewer = handoff.get('draft_review') if isinstance(handoff,dict) else None
        if not isinstance(reviewer,dict) or handoff.get('draft_sha256') != checkpoint['digest']:
            raise Problem('草稿审稿回执与封存版本不一致')
        validate_draft_observations(reviewer.get('observations'),files)
        validate_handoff_scopes(handoff['instruction'],checkpoint,files)
    if 'handoff_verification' in report:
        from superagent.goal_team_handoff import make_handoff_verification
        from superagent.goal_team_run import sealed_workspace
        from superagent.goal_engine import team_preference_for_step
        origin = assignment.get('team_handoff_origin') or {
            'handoff':assignment['team_handoff_continuation'], 'base':assignment['candidate_base'],
        }
        before, before_files, _ = capture(sealed_workspace(directory,origin['base']))
        bound = dict(assignment,outer_acceptance=flow(task)['outer']['acceptance_contract'],plan=task['plan'],
                     preference_answer=team_preference_for_step(task).get('answer'))
        evidence = make_handoff_verification(bound,origin['handoff']['instruction'],before,before_files,
                                             checkpoint,files)
        if report['handoff_verification'] != evidence:
            raise Problem('候选文件与负责人登记的修改和保留证据不匹配')
    samples = team_samples(report)
    if samples is not None and read_json(directory/'samples.json') != samples:
        raise Problem('用户所见样例与封存版本不一致')


def freeze_verified(workspace, directory, checkpoint):
    import shutil
    verified = directory / 'verified'
    verified.mkdir(mode=0o700)
    for relative in checkpoint['artifacts']:
        destination = verified / relative
        destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(workspace/relative,destination)
    if manifest(verified) != checkpoint:
        raise Problem('归档期间产物发生变化')


def ensure_process_exit(root, ticket, key):
    path = attempt_file(root,ticket,key,'process.json')
    records = [record for record in [path, *path.parent.glob('role-*/process.json')] if record.is_file()]
    if not records:
        return
    from process_guard import alive, processes
    rows = processes()
    for record in records:
        tracked = json.loads(record.read_text())
        if tracked.get('version') != 1 or 'processes' not in tracked:
            raise Problem('旧模型进程记录无法校验')
        if alive(tracked['processes'],rows):
            raise Problem('旧模型进程尚未确认退出，等待核对',409)
        parent_group = tracked.get('pgid')
        if (tracked.get('state') != 'stopped' and parent_group
                and any(row['group'] == parent_group and not row['state'].startswith('Z')
                        for row in rows.values())):
            raise Problem('先前模型进程组仍可能运行，等待核对',409)


class GoalRuntime:
    def __init__(self, store, hub):
        self.store = store
        self.hub = hub
        self.engine = GoalEngine(store)
        self._blind_workbench = None

    def _event(self, task, assignment):
        event, _, subject = assignment_route(assignment)
        binding = {'assignment':assignment['id'],'version':assignment['version']}
        if assignment['phase']=='package':
            binding['ticket'] = task['id']
        return {'id':assignment['event_key'],'type':event,'subject':subject,'detail':canonical(binding)}

    def tick(self, ticket, package_slots=PACKAGE_LIMIT):
        task = self.store.get(ticket)
        if task['status'] in ('created','queued') or (
                task['status']=='executing' and any(row['phase']=='package' for row in active_assignments(task))):
            task = self.engine.prepare(ticket,package_slots)
        for assignment in active_assignments(task):
            try:
                task = self._tick_assignment(ticket,assignment['id'])
            except (Problem, OSError, ValueError, KeyError):
                if len(active_assignments(task))==1:
                    raise
                # 一条路线待对账时，其他已派出的工作包仍需处理取消和回执。
                print(f'{ticket} 派工 {assignment["id"]} 待核对',file=sys.stderr,flush=True)
        return self.store.get(ticket)

    def _tick_assignment(self,ticket,key):
        task = self.store.get(ticket)
        assignment = current(task,key)
        if not assignment:
            return task
        event_type, rule, subject = assignment_route(assignment)
        event = assignment['event']
        if event is None:
            if flow(task)['stop_requested']:
                if not assignment.get('send_attempted'):
                    return self.engine.abandon_unsent(ticket,key)
                event = self.hub.existing_event(assignment['event_key'],event_type,subject)
                if event is None:
                    # 未观察到中台事件且控制层已停止新发送。明确记录仍待对账。
                    return self.engine.observe(ticket,key,'unknown')
            else:
                task = self.engine.mark_send(ticket,key)
                assignment = current(task,key)
                if not assignment or flow(task)['stop_requested']:
                    return task
                reply = self.hub.request('POST','/v1/events',self._event(task,assignment),source=True)
                event = reply.get('event')
            self.engine.bind(ticket,key,event)
        outcome = self.hub.request('GET',f'/v1/events/{event}',source=True)
        if 'event' in outcome and (type(outcome['event']) is not int or outcome['event'] != event):
            raise Problem('中台事件查询回执不匹配，待对账',409)
        runs = outcome['runs']
        if type(runs) is not list:
            raise Problem('中台轮次回执格式不明，待对账',409)
        if runs == []:
            if outcome.get('event') == event and outcome.get('routed') == [] and assignment['run'] is None:
                return self.engine.reject_route(ticket,key)
            return self.engine.observe(ticket,key,'unknown')
        if len(runs) != 1 or runs[0]['rule'] != rule or outcome.get('routed',[rule]) != [rule]:
            raise Problem('派工匹配多个轮次或规则不符',409)
        run_row = runs[0]
        run, status = run_row['run'], run_row['state']
        task = self.engine.bind(ticket,key,event,run)
        assignment = current(task,key)
        if not assignment:
            return task
        if flow(task)['stop_requested'] and status not in TERMINAL_RUNS:
            self.hub.request('POST',f'/v1/runs/{run}/cancel',{})
        if status in TERMINAL_RUNS:
            try:
                ensure_process_exit(self.store.root,ticket,key)
            except (Problem, OSError, ValueError, subprocess.SubprocessError):
                self.engine.observe(ticket,key,'unknown')
                raise
            path = attempt_file(self.store.root,ticket,key,'report.json')
            report = None
            primary_exists = os.path.lexists(path)
            if primary_exists:
                try:
                    report = read_json(path)
                except (ValueError, RecursionError):
                    pass
            outer = assignment.get('outer_binding')
            valid = report_matches_assignment(report, assignment)
            recovery_report = None
            recovery_checkpoint = None
            recovery_error = None
            confirmed_unstarted = (flow(task)['stop_requested'] and status == 'cancelled'
                                  and assignment.get('coordination_proposal') is not None
                                  and not any(os.path.lexists(path.parent/name)
                                              for name in ('entered','report.json','process.json',TEAM_RECOVERY_FILE))
                                  and hub_confirmed_unstarted(self.hub,run,rule))
            if team_candidate_assignment(assignment) and not confirmed_unstarted:
                try:
                    if valid and not report.get('error'):
                        verify_team_seal(path.parent,report,task,assignment)
                    elif not valid:
                        if isinstance(report,dict):
                            raise Problem('业务回执与原派工绑定不一致')
                        candidate_receipt = read_team_recovery(path.parent,assignment)
                        verify_team_seal(path.parent,candidate_receipt,task,assignment)
                        recovery_report = candidate_receipt
                except (KeyError, TypeError, OSError, RuntimeError, ValueError, Problem):
                    if valid:
                        report = dict(report, error='团队封存产物不存在或摘要不匹配')
                    else:
                        recovery_error = '原派工没有可核对的团队封存回执'
            elif confirmed_unstarted:
                from superagent.outer_verification import capture
                try:
                    approved = flow(task).get('active_candidate')
                    if approved:
                        from superagent.goal_team_run import sealed_workspace
                        workspace = sealed_workspace(path.parent, approved)
                    else:
                        workspace = self.store.workspace(ticket)
                    recovery_checkpoint = capture(workspace)[0]
                except (OSError, RuntimeError, ValueError, Problem):
                    pass
            elif assignment['phase'] in ('execute','repair') and (
                    (not valid and not flow(task)['stop_requested']) or
                    (outer and (not valid or report.get('error') or flow(task)['stop_requested']))):
                # 中台已确认终态且原模型已退出。只从原派工的工作区取证。
                candidate = (path.parent/'candidate' if flow(task).get('workspace_base')
                             else self.store.workspace(ticket))
                try:
                    if (primary_exists and not valid) or (path.parent/'entered').read_text() != str(run):
                        recovery_error = '缺少与原 Run 一致的执行记录'
                    elif candidate.is_symlink() or not candidate.is_dir():
                        recovery_error = '缺少原派工的工作区，无法核对其产物'
                    else:
                        if outer:
                            from superagent.outer_verification import capture
                            recovery_checkpoint = capture(candidate)[0]
                        else:
                            recovery_checkpoint = manifest(candidate)
                except (OSError, RuntimeError, ValueError, Problem):
                    recovery_error = '原派工的执行记录或产物无法核对'
            if valid and outer and assignment['phase']=='review' and report.get('delivery'):
                from superagent.outer_verification import capture
                try:
                    if capture(path.parent/'verified')[0] != report.get('checkpoint'):
                        raise Problem('验收归档已改变')
                except (OSError, RuntimeError, ValueError, Problem):
                    report = dict(report,error='独立归档不存在、已改变或无法安全读取')
            settled = self.engine.settle(ticket,key,run,status,report,recovery_report=recovery_report,
                                         recovery_checkpoint=recovery_checkpoint,
                                         recovery_error=recovery_error,confirmed_unstarted=confirmed_unstarted)
            if not valid and recovery_report is None and not confirmed_unstarted:
                self.store.event(ticket,'goal_transport','中台轮次已结束，缺少有效业务回执',
                                 missing_receipt_diagnostic(run_row,assignment))
            return settled
        return self.engine.observe(ticket,key,status)

    def publish_blind_review(self, ticket):
        task = self.store.get(ticket)
        if task['status'] != 'awaiting_artifact_review' or not is_blind_goal(task):
            return
        state = flow(task)
        review = state['artifact_reviews'][-1]
        candidate = state['active_candidate']['assignment']
        prepared = attempt_file(self.store.root,ticket,candidate,'blind-preparation.json')
        if not prepared.is_file() or prepared.is_symlink():
            raise Problem('缺少已核验候选对应的受信盲评材料', 409)
        if self._blind_workbench is None:
            self._blind_workbench = Workbench(store=self.store, goal_enabled=True, outer_enabled=True)
        ledger = self._blind_workbench.blind_review()
        actor = state['outer']['actor_id']
        view = ledger.get(ticket,actor_id=actor)
        if view and view['current']:
            return
        with self.store.connect() as db:
            row = db.execute('SELECT body FROM outer_tickets WHERE id=?', (ticket,)).fetchone()
        metadata = json.loads(prepared.read_text())
        ledger.register(ticket, json.loads(row[0])['revision'], review['assignment'], review['artifact_sha256'],
                        actor_id=actor, **metadata)

    def run(self, *, once=False):
        lock_path = self.store.root / 'goal-runtime.lock'
        with lock_path.open('a') as handle:
            try:
                fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                raise Problem('该工作台已经有目标协调器在运行',409) from None
            while True:
                # 用持久查询取得所有活跃目标，避免 UI 最近 100 条的上限漏派工。
                with self.store.connect() as db:
                    tickets = [row[0] for row in db.execute("""
                        SELECT id FROM tasks WHERE json_extract(body,'$.workflow')='goal'
                        AND json_extract(body,'$.status') IN
                        ('created','queued','planning','executing','reviewing','stopping')
                        ORDER BY CASE WHEN json_extract(body,'$.status')='stopping' THEN 0
                                      WHEN json_extract(body,'$.status')='queued'
                                       AND json_extract(body,'$.goal_execution.stage')='review' THEN 1
                                      ELSE 2 END, updated_at, id
                    """)]
                    blind_waiters = [row[0] for row in db.execute("""
                        SELECT t.id FROM tasks t WHERE json_extract(t.body,'$.status')='awaiting_artifact_review'
                        AND EXISTS (SELECT 1 FROM json_each(t.body,'$.goal_execution.outer.acceptance_contract.criteria') c
                                    WHERE json_extract(c.value,'$.verifier_id')='blind-projection'
                                      AND json_extract(c.value,'$.required')=1)
                    """)]
                for ticket in blind_waiters:
                    try:
                        self.publish_blind_review(ticket)
                    except (ValueError, KeyError, OSError, Problem):
                        # 刚完成、暂停或替换时，下次仍按原候选核对。
                        pass
                # 多张活跃单按轮询交错提交就绪包，避免先扫描的一张单预占整个中台队列。
                # 正在运行的包照常对账；实际领取由中台规则组和 Worker 的容量共同约束。
                package_slots = 1 if len(tickets)>1 else PACKAGE_LIMIT
                for ticket in tickets:
                    try:
                        self.tick(ticket,package_slots)
                    except (ValueError, KeyError, OSError, Problem) as exc:
                        print(f'{ticket} 待核对：{exc}',file=sys.stderr,flush=True)
                if once:
                    return
                time.sleep(.5)


def verify_csv(workspace):
    # 验证器固定由本地操作者安装并通过隔离规则显式选定。
    import importlib.util
    path = Path(__file__).resolve().parents[1] / '验收样例' / '目标驱动CSV' / 'verifier.py'
    spec = importlib.util.spec_from_file_location('goal_csv_verifier',path)
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    return verifier.verify(workspace)


def worker(store, verification):
    context = json.loads(Path(os.environ['AGENT_AUTOLOOP_CONTEXT']).read_text())
    run = int(os.environ['AGENT_AUTOLOOP_RUN'])
    (event,) = context['events']
    binding = json.loads(event['detail'])
    ticket = binding.get('ticket',event['subject'])
    key = binding['assignment']
    if event['source']!='local' or event['id']!=f'{ticket}:{key}':
        raise Problem('派工的来源事件标识不符')
    engine = GoalEngine(store)
    until = time.monotonic() + 25
    while True:
        assignment = engine.assignment_input(ticket,key,run)
        if assignment is not None:
            break
        if time.monotonic() > until:
            raise Problem('等待中台 Run 绑定超时')
        time.sleep(.2)
    event_type, _, subject = assignment_route(assignment)
    if event['type']!=event_type or event['subject']!=subject:
        raise Problem('执行能力或中台事项不符')
    report_path = attempt_file(store.root,ticket,key,'report.json')
    attempt_dir = report_path.parent
    attempt_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
    # 并行包仅持自己的尝试锁。其他阶段仍复用整单单写者租约。
    lock = package_lease(attempt_dir) if assignment['phase']=='package' else store.lease(ticket)
    with lock:
        # 锁内重验当前授权与代次。尝试凭证在执行任何模型或工具前落盘。
        if not report_path.is_file():
            assignment = engine.assignment_input(ticket,key,run)
            if not assignment or binding['version'] != assignment['version']:
                raise Problem('执行版本与当前派工不符')
            try:
                marker = (attempt_dir/'entered').open('x')
            except FileExistsError:
                # 中台可能重领同一轮，业务恢复必须先核对而非重复执行。
                print('没事可做：原派工已进入执行，业务回执缺失，交由协调器核对')
                return
            else:
                with marker:
                    marker.write(str(run))
                    marker.flush()
                    os.fsync(marker.fileno())
                report = execute_assignment(store,assignment,attempt_dir,verification)
            save_team_recovery(assignment,attempt_dir,report)
            save_json(report_path,report)
    print(f'目标派工回执 {key}')


def execute_assignment(store, assignment, directory, verification):
    from superagent.goal_codex import invoke
    contract = assignment.get('verification_contract')
    outer = assignment.get('outer_binding')
    report = {'id':assignment['id'],'run':assignment['run'],'version':assignment['version'],
              'verifier_profile':'contract-v1' if contract else verification}
    phase = assignment['phase']
    workspace = store.workspace(assignment['ticket'])
    try:
        if outer:
            report.update(outer_binding=outer,verifier_profile='outer-v2')
            if assignment.get('execution_profile') and assignment.get('candidate_base'):
                from superagent.goal_team_run import sealed_workspace
                workspace = sealed_workspace(directory, assignment['candidate_base'])
            if phase == 'review':
                if assignment.get('coordination_proposal'):
                    with GoalEngine(store).launch_gate(assignment['ticket'],assignment['id'],assignment['run']):
                        pass
                return execute_outer_review(assignment,workspace,directory,report)
            if phase == 'coordinate':
                return execute_coordination(store,assignment,directory,report)
            if assignment.get('coordination_proposal'):
                from superagent.outer_verification import capture
                if capture(workspace)[0] != assignment['checkpoint']:
                    return dict(report,checkpoint_mismatch=True,error='待执行的协调动作与已核对的工作区版本不一致')
            prepare_outer_execution(assignment)
            if assignment.get('execution_profile') and phase in ('execute', 'repair'):
                from superagent.goal_team_run import run_team
                engine = GoalEngine(store)
                result = run_team(engine, assignment, workspace, directory, report, lambda:
                                  engine.launch_gate(assignment['ticket'], assignment['id'], assignment['run']))
                samples = team_samples(result)
                if samples is not None:
                    save_json(directory/'samples.json',samples)
                return result
        base_id = assignment.get('workspace_base')
        if base_id:
            workspace = directory.parent/base_id/'candidate'
            if manifest(workspace)!=assignment['checkpoint']:
                raise Problem('已接纳工作区与登记的检查点不一致')
        if phase in ('package','assemble') or (base_id and phase in ('execute','repair')):
            from superagent.goal_team_files import seed
            inputs = [(directory.parent/item['assignment']/'outputs',item['files'])
                      for item in assignment.get('inputs',[])]
            candidate = directory/'candidate'
            baseline = seed(workspace,candidate,inputs)
            workspace = candidate
            if phase=='assemble':
                return dict(report,result={'summary':'已逐文件核对并合并工作包'},workspace_id=assignment['id'],
                            checkpoint=manifest(workspace))
            if phase!='package':
                report['workspace_id'] = assignment['id']
        before = manifest(workspace) if phase == 'review' else None
        if contract:
            stages = contract['stages']
            if phase == 'plan':
                stage_input = stages
            elif phase == 'replan':
                stage_input = stages[assignment['step']:]
            elif phase in ('review','repair') and assignment['step']+1 == len(stages):
                stage_input = stages
            else:
                stage_input = [stages[assignment['step']]]
            assignment['context']['required_stages'] = stage_input
            assignment['context']['contract_instruction'] = (
                '按给出的阶段顺序执行。同一阶段的每项确定性检查都必须通过；'
                '最终验收会复跑全部阶段。阶段编号、检查脚本和预期由授权契约固定。'
            )
            if phase in ('plan', 'replan'):
                assignment['context']['contract_instruction'] += (
                    f'本轮 steps 必须恰有 {len(stage_input)} 项，与 required_stages 一一对应。'
                    '将测试条件写入对应实施步骤的 checks。框架负责另行派出独立审核。')
            if assignment['context'].get('team_enabled') and phase in ('plan','replan'):
                assignment['context']['team_instruction'] = (
                    '根据交付物和依赖自行设计工作包。可独立核验、产物互不冲突的任务并行。'
                    '每包至少一项固定检查，checks 填可信检查 ID，outputs 列出该包所有文件（含检查脚本）。'
                    '每个固定检查 ID 在所有工作包中必须恰好出现一次，其 script 必须属于该包 outputs。'
                    'README 等文档分配给一个有检查的工作包。合流和独立终验由系统自动派出，不列入工作包。'
                    '根据 verification.planning_feedback 修正被拒的计划。depends_on 只声明真实数据依赖。'
                    '能力 implement 实现、analyze 分析。'
                    '简单阶段可用空数组走串行。')
            if phase=='package':
                package = assignment['package']
                assignment['context']['required_stages'] = [{
                    **stage_input[0], 'checks':[c for c in stage_input[0]['checks'] if c['id'] in package['checks']]
                }]
                assignment['context']['package_instruction'] = (
                    '只交付 context.package 指定的 outputs。依赖已复制到当前目录。'
                    '只处理本包检查。可以运行测试，退出前移除临时文件。')
        elif not outer and verification == 'csv-v1' and phase in ('plan','replan'):
            assignment['context']['fixture_contract'] = (
                '这个小型 CSV 首验只需要一个实施阶段。交付 csv_check.py，命令为 '
                'python3 -B csv_check.py /path/input.csv。输出 JSON 键 empty_category_rows '
                '和 duplicate_id_rows，行号从表头之后的第一条数据行算 1。'
                '框架在单独的沙箱中使用受信任测试样例进行黑盒验证。'
            )
        if phase == 'review':
            if contract:
                from superagent.goal_checks import verify
                checks = [check for stage in stage_input for check in verify(workspace,stage)['checks']]
                report['validation'] = {
                    'passed':bool(checks) and all(check['passed'] for check in checks),
                    'checks':checks,'contract_digest':digest(contract),
                }
            elif verification == 'csv-v1':
                report['validation'] = verify_csv(workspace)
                if any(workspace.glob('test*.py')):
                    from superagent.goal_csv_suite import verify_suite
                    suite = verify_suite(workspace)
                    report['validation']['checks'].append(suite)
                    report['validation']['passed'] = report['validation']['passed'] and suite['passed']
                    assignment['context']['test_evidence'] = (
                        'delivered_tests 是后台对本轮交付文件独立执行 unittest 的结果。'
                        '测试在可创建临时样例的隔离副本中执行。请结合逐项证据、源码和检查点独立裁定。')
            else:
                raise Problem('没有配置可复验的目标验收器')
            assignment['context']['trusted_verification'] = report['validation']
            if not contract:
                script = workspace/'csv_check.py'
                if 'csv_check.py' in before['artifacts'] and script.stat().st_size <= 12000:
                    assignment['context']['candidate_csv_check'] = script.read_text(errors='replace')
        # 只使用工作台保存的目标与最新授权，审核结果由可信验证器复算。
        engine = GoalEngine(store)
        output = invoke(assignment,workspace,directory,launch_gate=lambda:
                        engine.launch_gate(assignment['ticket'],assignment['id'],assignment['run']))
        result = output['result']
        report.update(result=result,evidence={k:v for k,v in output.items() if k != 'result'})
        if phase=='package':
            from superagent.goal_checks import verify
            from superagent.goal_team_files import freeze_package
            stage = assignment['context']['required_stages'][0]
            by_id = {c['id']:c for c in stage['checks']}
            stage['checks'] = [by_id[key] for key in package['checks']]
            outputs = freeze_package(workspace,directory/'outputs',baseline,package['outputs'])
            checked = directory/'for-verification'
            base = directory.parent/base_id/'candidate' if base_id else store.workspace(assignment['ticket'])
            observed = seed(base,checked,[*inputs,(directory/'outputs',outputs)])
            if observed!={**baseline,**outputs}:
                raise Problem('工作包核验输入与派工基线不一致')
            validation = verify(checked,stage)
            report['validation'] = dict(validation,contract_digest=digest(contract))
            if not validation['passed']:
                failures = [f"{c['id']}: {c['detail']}" for c in validation['checks'] if not c['passed']]
                report['error'] = '工作包验收失败：'+' / '.join(failures)
            else:
                report['outputs'] = outputs
        if phase in ('execute','repair','review'):
            if outer:
                from superagent.outer_verification import capture
                after = capture(workspace)[0]
            else:
                after = manifest(workspace)
            if before is not None and before['digest'] != after['digest']:
                raise Problem('审核期间工作区发生变化')
            report['checkpoint'] = after
            if phase == 'review' and report['validation']['passed'] and result.get('passed') is True:
                freeze_verified(workspace,directory,after)
                report['delivery'] = {'assignment':assignment['id'],'files':after['files']}
    except StaleCoordination as exc:
        report.update(coordination_dispatch_stale=True,error=str(exc))
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {str(exc)[:240]}'
    return report


def prepare_outer_execution(assignment):
    contract = assignment['outer_acceptance']
    context = assignment['context']
    step = assignment['step']
    phase = assignment['phase']
    keys = None
    if phase == 'replan':
        keys = {key for stage in assignment['plan']['steps'][step:] for key in stage['acceptance']}
    elif phase != 'plan':
        keys = set(assignment['plan']['steps'][step]['acceptance'])
    instructions = (context.get('verification') or {}).get('coordination') or {}
    if phase == 'repair' and step + 1 == len(assignment['plan']['steps']):
        keys = None  # 最后一步返工可以修复最终复验发现的前序回归。
    if keys is not None and phase in ('execute','repair') and instructions.get('schema_version')==2:
        keys.update(instructions['verification_items'])
    from superagent.outer_verification import implementation_criterion
    context['approved_criteria'] = [
        implementation_criterion(row) for row in contract['criteria']
        if keys is None or row['criterion_id'] in keys
    ]
    context['contract_instruction'] = (
        '按已批准目标和验收项实现真实行为。最终验收会在隔离环境复跑全部验收项。'
        'steps[].checks 只填写对应的 criterion_id。每个阶段包含至少一项必需验收，'
        '全部必需验收项必须出现在计划中，每项只在一个阶段。'
    )
    if phase == 'plan':
        from superagent.goal_team_blind import required_blind
        if required_blind(contract):
            context['contract_instruction'] += (
                '这个计划必须只有一个 steps 元素，列出所有已批准的必要 criterion_id。'
                '本人先看小样并留意见，两路生成、封存、独立检查和之后锁票属于同一个版本，请勿拆成多个步骤。')
        elif context.get('team_review_mode') == 'review_and_decide':
            context['contract_instruction'] += (
                '按真实依赖安排至少两个阶段，每阶段有独立且不同的已批准验收条款。'
                '把前置文件放在第一阶段，第二阶段要有可供本人选择方向的明确产物。'
                '仅按实际交付依赖拆分阶段。')
        elif assignment.get('plan_route_required'):
            context['contract_instruction'] += (
                '单一交付物优先安排一步，只在有独立新产物或真实依赖时拆分。'
                '在本轮规划同时选择 execution_route。目标和做法明确、可逆且无实质取舍时用 direct。'
                '有多种会影响结果的做法、重要风险或用户明确要求比较方案时用 compare。'
                'comparison_steps 只列确需两份独立方案的阶段序号，从 1 开始，其他机械步骤直接做；direct 时填空数组。'
                'route_reason 用一句话说明本单为何需要或不需要比较。每步产生可审阅的新稿，终验由工作台安排。')
    if phase == 'replan':
        context['contract_instruction'] += '仅调整后续阶段的实施方案，保持现有阶段顺序和验收项不变。'
    if instructions.get('schema_version')==2:
        context['contract_instruction'] += (
            'verification.coordination 是已生效的协调决定。处理其中列出的受影响阶段，'
            '保留 retained_artifacts 中各文件的原始字节，并按 verification_items 复验。'
        )
    if phase == 'repair' and context.get('human_review_feedback'):
        context['contract_instruction'] += (
            'human_review_feedback 是本人对已归档候选产物的退回意见。'
            '在现有授权内修复意见指出的问题并保留已满足的条件。'
            '不得修改验收契约、跳过自动复验或代替本人通过验收。'
        )


def execute_outer_review(assignment, workspace, directory, report):
    from superagent.outer_verification import capture, validate_results, verify
    start = time.time_ns()
    checkpoint, files, occupied = capture(workspace)
    if checkpoint != assignment['checkpoint']:
        raise Problem('待验产物与执行检查点不匹配')
    contract = assignment['outer_acceptance']
    rows = verify(contract,assignment,checkpoint,files,occupied)
    machine_passed = validate_results(contract,assignment['plan'],assignment,checkpoint,rows)
    if capture(workspace)[0] != checkpoint:
        raise Problem('验收期间工作区发生变化')
    report.update(result={'passed':machine_passed},checkpoint=checkpoint,validation={'checks':rows},
                  evidence={'started_ns':start,'ended_ns':time.time_ns()})
    if machine_passed:
        # 从已实际验证的同一批字节归档，避免二次从候选路径读入不同文件。
        verified = directory/'verified'
        verified.mkdir(mode=0o700)
        for relative, content in files.items():
            destination = verified/relative
            destination.parent.mkdir(parents=True,exist_ok=True)
            with destination.open('xb') as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            destination.chmod(0o400)
        if capture(verified)[0] != checkpoint:
            raise Problem('验收归档与验证字节不一致')
        level = 'goal' if assignment['step']+1==len(assignment['plan']['steps']) else 'stage'
        report['delivery'] = {'assignment':assignment['id'],'files':checkpoint['files'],
                              'artifact_sha256':checkpoint['digest'],'level':level}
        report['evidence']['ended_ns'] = time.time_ns()
    return report


def execute_coordination(store, assignment, directory, report):
    from superagent.outer_model import (
        PROMPT_VERSION, SCOPED_PROMPT_VERSION, CodexOuterModel, ModelFailure, role_invoke,
    )
    from superagent.outer_verification import SCOPED_COORDINATOR, coordination_schema, validate_coordination
    context = assignment['context']['coordinator']
    start = time.time_ns()
    engine = GoalEngine(store)
    scoped = context.get('coordinator_profile') == SCOPED_COORDINATOR
    limit = 120 if scoped else 45
    adapter = CodexOuterModel(timeout=min(limit,assignment['max_seconds'])).with_execution(
        directory, lambda: engine.launch_gate(assignment['ticket'],assignment['id'],assignment['run'])
    )
    evidence = {
        'invocation_id':assignment['id'], 'snapshot_revision':context['input_snapshot_revision'],
        'started_ns':start, 'prompt_version':SCOPED_PROMPT_VERSION if scoped else PROMPT_VERSION,
        'model':adapter.model,
    }
    save_json(directory / 'coordination-start.json', evidence)
    try:
        invocation = role_invoke(adapter, 'goal_coordinator', context, coordination_schema(context))
        evidence.update({key:invocation[key] for key in ('model','usage','elapsed_ms','prompt_version')})
        evidence.update({key:invocation[key] for key in ('session_id','tools') if key in invocation})
        report['model_audit'] = {key:invocation[key] for key in ('request','final_message','output_truncated')
                                 if key in invocation}
        proposal = validate_coordination(invocation['result'],context)
    except ModelFailure as error:
        evidence.update(usage=error.usage, failure_code=error.code)
        if 'elapsed_ms' in error.audit:
            evidence['elapsed_ms'] = error.audit['elapsed_ms']
        report['model_audit'] = error.audit
        report['error'] = '协调模型调用未产生有效提案'
    except ValueError as error:
        evidence['failure_code'] = 'proposal_rejected'
        report['error'] = str(error)[:240]
        report['model_audit']['returned_proposal'] = invocation['result']
    else:
        report['result'] = proposal
    evidence['ended_ns'] = time.time_ns()
    report['evidence'] = evidence
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command',choices=('serve','tick','worker','submit','show','approve','stop','resume','recheck'))
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--hub',type=Path,required=True)
    parser.add_argument('--verify',choices=('csv-v1',))
    parser.add_argument('--ticket')
    parser.add_argument('--goal')
    parser.add_argument('--acceptance',default='')
    parser.add_argument('--contract',type=Path,help='提交目标时携带的验收契约 JSON')
    parser.add_argument('--request')
    parser.add_argument('--stop-generation',type=int)
    parser.add_argument('--approval')
    parser.add_argument('--version',type=int)
    parser.add_argument('--checkpoint')
    args = parser.parse_args()
    store = Store(args.runtime.resolve())
    engine = GoalEngine(store)
    cmd = args.command
    if cmd == 'worker':
        worker(store,args.verify)
        return
    if cmd in ('serve','tick'):
        GoalRuntime(store,Hub(args.hub)).run(once=cmd == 'tick')
        return
    if cmd == 'submit':
        if not args.goal:
            parser.error('请提供目标')
        if args.contract:
            from superagent.goal_checks import normalize
            engine = GoalEngine(store, normalize(json.loads(args.contract.read_text())))
        value,_ = engine.create({'goal':args.goal,'name':args.goal[:80],'acceptance':args.acceptance,
                                 'request_id':args.request or str(uuid.uuid4()),'budget_minutes':30})
    elif not args.ticket:
        parser.error('请提供工单号')
    elif cmd == 'approve':
        value = engine.approve(args.ticket,args.version,args.approval)
    elif cmd == 'stop':
        value = engine.stop(args.ticket)
    elif cmd == 'recheck':
        value = engine.recheck(args.ticket,args.version,args.checkpoint)
    elif cmd == 'resume':
        value = engine.resume(args.ticket,args.request or str(uuid.uuid4()),args.stop_generation)
    else:
        value = store.get(args.ticket)
    print(json.dumps(value,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
