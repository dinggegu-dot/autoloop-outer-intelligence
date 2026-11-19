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
from workbench import Hub, Problem, Store
from superagent.goal_engine import (
    PACKAGE_LIMIT, GoalEngine, active_assignments, assignment_route, canonical, current, digest, flow,
)


TERMINAL_RUNS = ('done', 'nothing', 'cancelled')


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
    with tempfile.NamedTemporaryFile(dir=path.parent,mode='w',delete=False) as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(stream.name,path)


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
    if not path.is_file():
        return
    from process_guard import alive, processes
    tracked = json.loads(path.read_text())
    if tracked.get('version') != 1 or 'processes' not in tracked:
        raise Problem('旧模型进程记录无法校验')
    rows = processes()
    if alive(tracked['processes'],rows):
        raise Problem('旧模型进程尚未确认退出，等待核对',409)
    parent_group = tracked.get('pgid')
    if parent_group and any(row['group'] == parent_group and not row['state'].startswith('Z')
                            for row in rows.values()):
        raise Problem('先前模型进程组仍可能运行，等待核对',409)


class GoalRuntime:
    def __init__(self, store, hub):
        self.store = store
        self.hub = hub
        self.engine = GoalEngine(store)

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
        if flow(task)['stop_requested'] and status not in TERMINAL_RUNS:
            self.hub.request('POST',f'/v1/runs/{run}/cancel',{})
        if status in TERMINAL_RUNS:
            try:
                ensure_process_exit(self.store.root,ticket,key)
            except (Problem, OSError, ValueError, subprocess.SubprocessError):
                self.engine.observe(ticket,key,'unknown')
                raise
            path = attempt_file(self.store.root,ticket,key,'report.json')
            report = json.loads(path.read_text()) if path.is_file() else None
            outer = assignment.get('outer_binding')
            valid = (isinstance(report,dict) and report.get('id') == key and report.get('run') == run
                     and report.get('version') == assignment['version']
                     and report.get('outer_binding') == outer)
            recovery_checkpoint = None
            recovery_error = None
            if assignment['phase'] in ('execute','repair') and (
                    (not valid and not flow(task)['stop_requested']) or
                    (outer and (not valid or report.get('error') or flow(task)['stop_requested']))):
                # 中台已确认终态且原模型已退出。只从原派工的工作区取证。
                candidate = (path.parent/'candidate' if flow(task).get('workspace_base')
                             else self.store.workspace(ticket))
                try:
                    if (report is not None and not valid) or (path.parent/'entered').read_text() != str(run):
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
            settled = self.engine.settle(ticket,key,run,status,report,recovery_checkpoint=recovery_checkpoint,
                                         recovery_error=recovery_error)
            if not valid:
                self.store.event(ticket,'goal_transport','中台轮次已结束，缺少有效业务回执',
                                 missing_receipt_diagnostic(run_row,assignment))
            return settled
        return self.engine.observe(ticket,key,status)

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
            if phase == 'review':
                return execute_outer_review(assignment,workspace,directory,report)
            if phase == 'coordinate':
                return execute_coordination(store,assignment,directory,report)
            prepare_outer_execution(assignment)
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
    if phase == 'repair' and step + 1 == len(assignment['plan']['steps']):
        keys = None  # 最后一步返工可以修复最终复验发现的前序回归。
    context['approved_criteria'] = [
        {key: row[key] for key in ('criterion_id','requirement_ids','description','required','verifier_id')}
        | {'interface':{key:value for key,value in row['test_spec'].items() if key not in ('args','files')}}
        for row in contract['criteria'] if keys is None or row['criterion_id'] in keys
    ]
    context['contract_instruction'] = (
        '按已批准目标和验收项实现真实行为。最终验收会在隔离环境复跑全部验收项。'
        'steps[].checks 只填写对应的 criterion_id。每个阶段包含至少一项必需验收，'
        '全部必需验收项必须出现在计划中，每项只在一个阶段。'
    )
    if phase == 'replan':
        context['contract_instruction'] += '仅调整后续阶段的实施方案，保持现有阶段顺序和验收项不变。'


def execute_outer_review(assignment, workspace, directory, report):
    from superagent.outer_verification import capture, validate_results, verify
    start = time.time_ns()
    checkpoint, files, occupied = capture(workspace)
    if checkpoint != assignment['checkpoint']:
        raise Problem('待验产物与执行检查点不匹配')
    contract = assignment['outer_acceptance']
    rows = verify(contract,assignment,checkpoint,files,occupied)
    passed = validate_results(contract,assignment['plan'],assignment,checkpoint,rows)
    if capture(workspace)[0] != checkpoint:
        raise Problem('验收期间工作区发生变化')
    report.update(result={'passed':passed},checkpoint=checkpoint,validation={'checks':rows},
                  evidence={'started_ns':start,'ended_ns':time.time_ns()})
    if passed:
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
    from superagent.outer_model import PROMPT_VERSION, CodexOuterModel, ModelFailure, role_invoke
    from superagent.outer_verification import coordination_schema, validate_coordination
    context = assignment['context']['coordinator']
    start = time.time_ns()
    engine = GoalEngine(store)
    adapter = CodexOuterModel(timeout=min(45,assignment['max_seconds'])).with_execution(
        directory, lambda: engine.launch_gate(assignment['ticket'],assignment['id'],assignment['run'])
    )
    evidence = {
        'invocation_id':assignment['id'], 'snapshot_revision':context['input_snapshot_revision'],
        'started_ns':start, 'prompt_version':PROMPT_VERSION, 'model':adapter.model,
    }
    save_json(directory / 'coordination-start.json', evidence)
    try:
        invocation = role_invoke(adapter, 'goal_coordinator', context, coordination_schema(context))
        evidence.update({key:invocation[key] for key in ('model','usage','elapsed_ms','prompt_version')})
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
