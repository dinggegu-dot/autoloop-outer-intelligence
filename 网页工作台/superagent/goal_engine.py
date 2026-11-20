"""目标工单状态机。Autoloop 负责运行事实，工作台事务负责业务决定。"""
import hashlib
import json
import uuid
from contextlib import contextmanager

from workbench import Problem, now, require_v1_control


MODEL = 'gpt-6.2-sol'
RULE = 'odyssey-goal'
EVENT = 'workbench.goal.assignment'
POLICY_VERSION = 1
CALL_LIMIT_SECONDS = 240
REPAIR_LIMIT = 2
PACKAGE_LIMIT = 4
PACKAGE_ROUTES = {
    'implement': ('workbench.goal.implement', 'odyssey-goal-implement'),
    'analyze': ('workbench.goal.analyze', 'odyssey-goal-analyze'),
}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def initial_state(source, request, verification_contract=None, team_enabled=False):
    return {
        'stage': 'plan',
        'source_event': {'source': source, 'id': request, 'policy': POLICY_VERSION},
        'mandate': None, 'revisions': [], 'assignments': [], 'decisions': [],
        'stop_requested': False, 'checkpoint': None, 'repairs': 0,
        'replans': 0, 'validation': None, 'attempts': {}, 'stop_generation':0, 'resume_requests':{},
        'verification_contract': verification_contract, 'team_enabled':team_enabled,
    }


def flow(task):
    state = task.get('goal_execution')
    if task.get('workflow') != 'goal' or not isinstance(state, dict):
        raise Problem('此任务未启用目标路线', 409)
    return state


def active_assignments(task):
    return [row for row in flow(task)['assignments'] if row['state'] in ('pending','dispatched')]


def current(task, key=None):
    return next((row for row in reversed(active_assignments(task)) if key is None or row['id']==key), None)


def assignment_route(assignment):
    if assignment['phase'] == 'package':
        event, rule = PACKAGE_ROUTES[assignment['package']['capability']]
        return event, rule, assignment['event_key']
    return EVENT, RULE, assignment['ticket']


def decision(task, kind, **fields):
    flow(task)['decisions'].append({'kind': kind, 'at': now(), **fields})


def outer_binding(task):
    state = flow(task)
    outer = state.get('outer')
    if not outer:
        return None
    return {
        **{key: outer[key] for key in ('ticket_id','goal_revision','acceptance_revision',
                                     'mandate_revision','approval_event_id','actor_id','correlation_id')},
        'contract_digest':digest(outer['acceptance_contract']),
        'goal_digest':digest(outer['goal_contract']),
        'authorization_digest':digest(outer['mandate']),
        'stop_generation':state['stop_generation'],
    }


def validate_outer_authorization(outer, *, ticket_id, workspace, budget_seconds):
    """在正式批准与派工时核对同一组授权绑定。"""
    from superagent.outer_verification import validate_constraint_controls

    contract = outer['acceptance_contract']
    mandate = outer['mandate']
    goal = outer['goal_contract']
    if (outer['ticket_id'] != ticket_id or goal['ticket_id'] != ticket_id or
            contract['ticket_id'] != ticket_id or mandate['ticket_id'] != ticket_id or
            goal['revision'] != outer['goal_revision'] or goal['status'] != 'APPROVED' or
            contract['goal_revision'] != outer['goal_revision'] or
            contract['revision'] != outer['acceptance_revision'] or
            mandate['goal_revision'] != outer['goal_revision'] or
            mandate['acceptance_revision'] != outer['acceptance_revision'] or
            contract['status'] != 'APPROVED' or contract['verification_gaps'] or
            not contract.get('criteria') or
            mandate['revision'] != outer['mandate_revision'] or
            mandate['approval_event_id'] != outer['approval_event_id'] or
            mandate['actor_id'] != outer['actor_id'] or
            mandate['workspace'] != str(workspace) or
            type(mandate['budget_seconds']) is not int or
            not 0 < mandate['budget_seconds'] <= budget_seconds):
        raise ValueError('V2 授权绑定不一致')
    validate_constraint_controls(goal,contract,mandate)


def pause_or_block(task, message, *, budget=False):
    task.update(error=message, status='budget_exhausted' if budget else 'blocked')
    decision(task, 'blocked', reason=message)


def plan_result(result, version):
    raw = result.get('steps')
    if not isinstance(raw, list) or not 1 <= len(raw) <= 6:
        raise Problem('计划必须有 1 至 6 个带验收项的阶段')
    steps = []
    for item in raw:
        objective = item.get('objective') if isinstance(item, dict) else None
        checks = item.get('checks') if isinstance(item, dict) else None
        if (not isinstance(objective, str) or not objective.strip() or len(objective) > 1500
                or not isinstance(checks, list) or not 1 <= len(checks) <= 8
                or any(not isinstance(check, str) or not check.strip() or len(check) > 500 for check in checks)):
            raise Problem('阶段缺少可验证验收项或超出长度')
        steps.append({'title': objective[:90], 'objective': objective, 'acceptance': checks,
                      'verification': ' / '.join(checks), 'state': 'pending'})
    summary = result.get('summary')
    if not isinstance(summary,str) or not summary.strip() or len(summary) > 3000:
        raise Problem('缺少有效方案摘要')
    return {'version': version, 'summary': summary, 'scope': '仅在授权的本机工作区操作',
            'assumptions': [], 'steps': steps}


class GoalEngine:
    def __init__(self, store, verification_contract=None, team_enabled=False):
        self.store = store
        self.verification_contract = verification_contract
        self.team_enabled = team_enabled

    def create(self, body, source='board'):
        if source not in ('board','feishu'):
            raise Problem('来源不在允许范围',403)
        if self.team_enabled and not self.verification_contract:
            raise Problem('多工作包路线需要可信验收契约')
        options = dict(body, role_models=dict.fromkeys(('plan','execute','review'), 'gpt-6.1-sol'))
        return self.store.create(options, source=source, workflow='goal',
                                 verification_contract=self.verification_contract,team_enabled=self.team_enabled)

    def approve(self, ticket, version, approval, approval_source='board', choice=0):
        if approval_source not in ('board','feishu'):
            raise Problem('授权来源无效',403)
        if type(choice) is not int or choice not in (0,1):
            raise Problem('请选择有效决定')
        def apply(task):
            state = flow(task)
            if state.get('outer'):
                raise Problem('V2 目标须通过正式目标授权入口',409)
            if task['paused'] or state['stop_requested']:
                raise Problem('目标已停止',409)
            if type(version) is not int or task['version'] != version:
                raise Problem('方案已变化，请重新查看',409)
            card = task['approval']
            if not card or card['id'] != approval:
                raise Problem('批准与当前方案不匹配',409)
            if card['state'] == 'decided':
                if card['choice'] == choice:
                    return
                raise Problem('已有生效决定',409)
            if task['status'] != 'awaiting_approval' or card['digest'] != digest(task['plan']):
                raise Problem('方案变更或当前目标不可批准',409)
            card.update(state='decided',choice=choice)
            task['decisions'].append({'kind':'plan','version':version,'approved':choice==0,
                                      'choice':('按这个方案执行','暂不执行')[choice],'approval':approval})
            if choice:
                task['status'] = 'rejected'
                decision(task,'rejected',approval=approval,source=approval_source,version=version)
                return
            state['mandate'] = {
                'policy': POLICY_VERSION, 'goal': task['goal'], 'acceptance': task['acceptance'],
                'workspace': ticket, 'approval': approval, 'source': approval_source,
                'approved_plan': card['digest'],
                'verification_digest': digest(state.get('verification_contract')),
                'model': MODEL, 'scope': '本机隔离工作区，离线读写测试；不发送、不发布、不安装',
                'budget_seconds': task['time_budget_seconds'], 'approved_at': now(),
            }
            state['stage'] = 'execute'
            task.update(phase='execute',status='queued',error='')
            decision(task, 'authorized', approval=approval, source=approval_source, version=version)
        return self.store.change(ticket, apply)

    def stop(self, ticket, source='board', *, binding=None):
        def apply(task):
            require_v1_control(task)
            state = flow(task)
            if binding is not None and (
                binding.get('task') != ticket or binding.get('operation') != 'pause'
                or type(binding.get('version')) is not int or binding['version'] != task['version']
                or type(binding.get('stop_generation')) is not int
                or binding['stop_generation'] != state.get('stop_generation',0)
            ):
                raise Problem('暂停入口已过期，请重新查看当前工单',409)
            if task['status'] in ('completed','rejected'):
                raise Problem('工单已结束',409)
            if not state['stop_requested']:
                generation = state.get('stop_generation',0)+1
                state['stop_generation'] = generation
                decision(task, 'stop_requested', source=source,generation=generation)
            state['stop_requested'] = True
            task.update(paused=True, status='stopping' if current(task) else 'paused')
        return self.store.change(ticket, apply)

    def resume(self, ticket, request, generation, source='board'):
        if not isinstance(request,str) or not request or type(generation) is not int:
            raise Problem('缺少续办请求或明确的停办版本')
        def apply(task):
            require_v1_control(task)
            state = flow(task)
            prior = state.setdefault('resume_requests',{})
            if request in prior:
                if prior[request] != generation:
                    raise Problem('同一个续办请求绑定了其他停办版本',409)
                return
            if state.get('stop_generation',0) != generation:
                raise Problem('此续办请求属于旧停办版本',409)
            if not state['stop_requested'] or current(task) or task['status'] != 'paused':
                raise Problem('工单尚未停止或仍需核对原轮次',409)
            if state['mandate'] and state['mandate']['policy'] != POLICY_VERSION:
                raise Problem('授权策略已变化，请重新确认',409)
            state['stop_requested'] = False
            prior[request] = generation
            team = state.get('team')
            if team and team['step']==task['step'] and team['version']==task['version']:
                merges = [row for row in state['assignments'] if row['phase']=='assemble'
                          and row['step']==task['step'] and row['version']==task['version']]
                if merges and not any((row.get('report') or {}).get('workspace_id')==row['id'] and
                                      not row['report'].get('error') for row in merges):
                    state['stage'] = 'execute'
                    task['phase'] = 'execute'
            awaiting = (task.get('approval') or {}).get('state') == 'open' and not state['mandate']
            task.update(paused=False, status='awaiting_approval' if awaiting else 'queued', error='')
            decision(task, 'resumed', source=source,generation=generation)
        return self.store.change(ticket, apply)

    def prepare(self, ticket, package_slots=PACKAGE_LIMIT):
        def apply(task):
            state = flow(task)
            if task['paused'] or task['status'] not in ('created','queued','executing'):
                return
            phase = state['stage']
            if (state.get('validation') or {}).get('recovery_blocked'):
                pause_or_block(task,'暂停前派工没有可核对的原版本检查点')
                return
            outer = state.get('outer')
            if outer:
                try:
                    self._ensure_outer_mandate(task)
                except (KeyError, TypeError, ValueError):
                    pause_or_block(task,'V2 目标的授权、验收或预算绑定无法确认')
                    return
            active = active_assignments(task)
            plan = task['plan'] or {}
            steps = plan.get('steps', [])
            packages = steps[task['step']].get('packages', []) if phase=='execute' and task['step']<len(steps) else []
            if active and (not packages or any(row['phase']!='package' for row in active)):
                return
            if not active and task['status']=='executing':
                return
            if phase != 'plan' and not state['mandate']:
                raise Problem('尚未获得实施授权',409)
            if state['mandate'] and state['mandate']['policy'] != POLICY_VERSION:
                pause_or_block(task,'授权策略已变化，请核对范围')
                return
            budget = state['mandate']['budget_seconds'] if outer else task['time_budget_seconds']
            remaining = budget - task['elapsed_seconds'] - sum(row['max_seconds'] for row in active)
            if outer and task.get('max_calls') is not None and task['calls'] >= task['max_calls']:
                pause_or_block(task,'执行调用预算用完',budget=True)
                return
            if outer and phase == 'review':
                from superagent.outer_verification import selected_criteria
                criteria = selected_criteria(outer['acceptance_contract'], plan, task['step'])
                minimum = 10 + 9 * sum(row['verifier_id']=='python-json-cli' for row in criteria)
                if remaining < minimum:
                    pause_or_block(task,'剩余预算不足以完成本轮验收',budget=True)
                    return
            if packages:
                self._prepare_team(task, packages, active, int(remaining), package_slots)
                return
            if remaining < 10:
                pause_or_block(task,'执行时间预算用完',budget=True)
                return
            details = {}
            if phase == 'coordinate':
                state.setdefault('rechecks',0)
                details['coordination_context'] = self._outer_coordination_context(task)
            self._enqueue(task,phase,int(remaining),**details)
        return self.store.change(ticket, apply)

    def _ensure_outer_mandate(self, task):
        state = flow(task)
        outer = state['outer']
        if state.get('team_enabled') or state.get('verification_contract'):
            raise ValueError('V2 与固定 V1 契约必须各自通过对应入口')
        validate_outer_authorization(outer,ticket_id=task['id'],workspace=self.store.workspace(task['id']),
                                     budget_seconds=task['time_budget_seconds'])
        mandate = outer['mandate']
        binding = outer_binding(task)
        authorized = {
            'policy': POLICY_VERSION, 'goal': task['goal'], 'acceptance': task['acceptance'],
            'workspace': task['id'], 'approval': mandate['approval_event_id'],
            'source': task['source'], 'model': MODEL, 'budget_seconds': mandate['budget_seconds'],
            'outer': {key:value for key,value in binding.items() if key!='stop_generation'},
            'scope': mandate['scope'], 'forbidden_operations':mandate['forbidden_operations'],
        }
        if state['mandate'] is None:
            state['mandate'] = authorized
            decision(task,'authorized',version=outer['mandate_revision'],
                     approval=mandate['approval_event_id'],actor=mandate['actor_id'],
                     correlation_id=outer['correlation_id'])
        elif state['mandate'] != authorized:
            raise ValueError('执行授权在当前派工前已变化')

    def recheck(self,ticket,version,checkpoint):
        """验收环境修复后，原授权只允许重新审核登记过的同一检查点。"""
        def apply(task):
            require_v1_control(task)
            state = flow(task)
            if (task['status']!='blocked' or current(task) or state['stop_requested']
                    or task['version']!=version or state['stage'] not in ('review','replan','repair')
                    or not state['mandate'] or state['mandate']['policy']!=POLICY_VERSION
                    or not checkpoint or (state['checkpoint'] or {}).get('digest')!=checkpoint):
                raise Problem('当前工单、授权或检查点不支持原版本复验',409)
            state['stage']='review'
            task.update(status='queued',phase='review',error='')
            decision(task,'review_requested',source='operator',version=version,digest=checkpoint)
        return self.store.change(ticket,apply)

    @staticmethod
    def _enqueue(task, phase, seconds, **details):
        state = flow(task)
        key, ticket = uuid.uuid4().hex, task['id']
        assignment = {
            'id':key, 'ticket':ticket, 'phase':phase, 'state':'pending',
            'event_key':f'{ticket}:{key}', 'event':None, 'run':None,
            'version':task['version'], 'mandate_digest':digest(state['mandate']),
            'verification_digest':digest(state.get('verification_contract')),
            'step':task['step'], 'created_at':now(), 'model':MODEL,
            'max_seconds':min(CALL_LIMIT_SECONDS, seconds), **details,
        }
        binding = outer_binding(task)
        if binding is not None:
            assignment['outer_binding'] = binding
        state['assignments'].append(assignment)
        if task['status']!='executing':
            task.update(status='queued',error='')
        decision(task,'dispatch_prepared',assignment=key,phase=phase,version=task['version'],
                 **({'package':details['package']['id']} if 'package' in details else {}))

    def _prepare_team(self, task, packages, active, remaining, slots):
        from superagent.goal_team_plan import prerequisites, ready_packages
        state = flow(task)
        team = state.get('team')
        if not team or team['step']!=task['step'] or team['version']!=task['version']:
            team = {'step':task['step'],'version':task['version'],'accepted':{},'failures':{},'errors':{},'blocked':None}
            state['team'] = team
        accepted = team['accepted']
        if team['blocked']:
            if not active:
                pause_or_block(task,team['blocked'])
            return
        if len(accepted)==len(packages):
            if remaining < 10:
                pause_or_block(task,'执行时间预算用完',budget=True)
            else:
                self._enqueue(task,'assemble',min(remaining,30),
                              inputs=[accepted[package['id']] for package in packages])
            return
        available = min(slots, PACKAGE_LIMIT-len(active))
        if available<=0:
            return
        attempt_limit = remaining // (len(packages)-len(accepted)-len(active)+2)
        if attempt_limit < 10:
            if not active:
                pause_or_block(task,'工作包及后续验收的执行预算不足',budget=True)
            return
        ready = ready_packages(packages,set(accepted),{row['package']['id'] for row in active})
        for package in ready[:available]:
            inputs = [accepted[key] for key in prerequisites(packages,package['id'])]
            self._enqueue(task,'package',attempt_limit,package=package,inputs=inputs,
                          retry_context=team['errors'].get(package['id']))

    def bind(self, ticket, key, event, run=None):
        if type(event) is not int or event < 1 or (run is not None and (type(run) is not int or run < 1)):
            raise Problem('中台回执缺少有效事件或执行轮次',503)
        def apply(task):
            assignment = current(task,key)
            if not assignment:
                return
            if assignment['event'] not in (None,event) or (run is not None and assignment['run'] not in (None,run)):
                raise Problem('中台事件或执行轮次冲突',409)
            assignment['event'] = event
            if run is not None and assignment['run'] is None:
                assignment['run'] = run
                assignment['state'] = 'dispatched'
                task['run_id'] = run
                decision(task,'run_bound',assignment=key,run=run)
        return self.store.change(ticket, apply)

    def mark_send(self, ticket, key):
        def apply(task):
            assignment = current(task,key)
            if assignment and not flow(task)['stop_requested']:
                assignment['send_attempted'] = True
        return self.store.change(ticket, apply)

    def abandon_unsent(self, ticket, key):
        def apply(task):
            assignment = current(task,key)
            if (assignment and not assignment.get('send_attempted')
                    and flow(task)['stop_requested']):
                assignment['state'] = 'cancelled'
                self._after_stop(task,key)
        return self.store.change(ticket, apply)

    def reject_route(self, ticket, key):
        def apply(task):
            assignment = current(task,key)
            if assignment:
                assignment['state'] = 'rejected'
                if flow(task)['stop_requested']:
                    self._after_stop(task,key)
                elif assignment['phase']=='package':
                    self._fail_package(task,assignment,'工作包能力路线未配置或未接起',retry=False)
                else:
                    pause_or_block(task,'目标派工路由不可用，请检查独立中台规则')
        return self.store.change(ticket,apply)

    def observe(self, ticket, key, transport):
        status = {'plan':'planning','replan':'planning','execute':'executing','repair':'executing',
                  'review':'reviewing','coordinate':'reviewing','package':'executing','assemble':'executing'}
        def apply(task):
            assignment = current(task,key)
            if not assignment:
                return
            assignment['transport'] = transport
            if flow(task)['stop_requested']:
                task['status'] = 'stopping'
            elif transport in ('claimed','unknown'):
                task['status'] = status[assignment['phase']]
        return self.store.change(ticket,apply)

    def settle(self, ticket, key, run, transport, report=None, *, recovery_checkpoint=None,
               recovery_error=None):
        if transport not in ('done','nothing','cancelled'):
            raise Problem('原轮尚未进入可核对的终态',409)
        def apply(task):
            state = flow(task)
            assignment = current(task,key)
            if not assignment or assignment['run'] != run:
                return
            if (assignment['version'] != task['version']
                    or assignment['mandate_digest'] != digest(state['mandate'])
                    or assignment.get('verification_digest',digest(None)) != digest(state.get('verification_contract'))):
                raise Problem('旧版本派工不能修改目标',409)
            binding = assignment.get('outer_binding')
            live = outer_binding(task)
            if binding is not None and binding != ({**live,'stop_generation':binding['stop_generation']}
                                                     if state['stop_requested'] and live else live):
                raise Problem('旧版本派工不能修改 V2 目标',409)
            valid = (isinstance(report,dict) and report.get('id') == key and report.get('run') == run
                     and report.get('version') == assignment['version']
                     and report.get('outer_binding') == binding)
            assignment.update(state='settled', transport=transport, settled_at=now(), report=report if valid else None)
            if state['stop_requested']:
                if transport == 'done' and valid:
                    self._charge(task, assignment, report)
                    if assignment['phase']=='package' and not report.get('error'):
                        try:
                            self._advance(task,assignment,report)
                        except (KeyError,TypeError,ValueError,Problem) as exc:
                            decision(task,'package_unverified',assignment=key,reason=str(exc)[:180])
                else:
                    self._charge(task, assignment, None)
                if assignment['phase'] in ('execute','repair') and not state.get('workspace_base'):
                    # 停办与实施回执可能交错，续办时先验留在磁盘的效果。
                    state['stage'] = 'review'
                    state['validation'] = {'recovering_execute':True,'reason':'停办时有实施轮次'}
                    if binding:
                        checkpoint = recovery_checkpoint or (report.get('checkpoint') if valid else None)
                        if checkpoint and checkpoint.get('digest'):
                            state['checkpoint'] = checkpoint
                            state['validation']['pinned_digest'] = checkpoint['digest']
                        else:
                            state['validation']['recovery_blocked'] = True
                    task['phase'] = 'review'
                self._after_stop(task,key)
                return
            task.update(status='queued',error='')
            self._charge(task, assignment, report if valid else None)
            if transport != 'done' or not valid:
                if recovery_error:
                    pause_or_block(task,recovery_error)
                    return
                if not valid and assignment['phase'] in ('execute','repair') and recovery_checkpoint is not None:
                    if state.get('workspace_base'):
                        state['workspace_base'] = key
                    state['checkpoint'] = recovery_checkpoint
                    self._recover(task,assignment,'实施回执缺失，独立验收原派工的实际文件')
                    if state['stage']=='review':
                        state['validation']['pinned_digest'] = recovery_checkpoint['digest']
                        decision(task,'recovered_artifacts',assignment=key,run=run,digest=recovery_checkpoint['digest'])
                    return
                if not valid and assignment['phase'] in ('execute','repair') and state.get('workspace_base'):
                    pause_or_block(task,'缺少原派工的工作区，无法核对其产物')
                    return
                self._failed_assignment(task,assignment,'执行终态已确认，业务回执缺失')
                return
            if report.get('error'):
                if binding and assignment['phase'] in ('execute','repair') and recovery_checkpoint:
                    state['checkpoint'] = recovery_checkpoint
                    self._recover(task,assignment,'实施轮次失败，先核对原派工的文件')
                    if state['stage'] == 'review':
                        state['validation']['pinned_digest'] = recovery_checkpoint['digest']
                    return
                self._failed_assignment(task,assignment,str(report['error'])[:300])
                return
            try:
                self._advance(task, assignment, report)
            except (KeyError, TypeError, ValueError, Problem) as exc:
                reason=f'执行回执或证据无法验证：{str(exc)[:160]}'
                if assignment['phase']=='package':
                    self._fail_package(task,assignment,reason)
                elif assignment['phase']=='plan' and (state.get('outer') or
                                                        (state.get('team_enabled') and not state['mandate'])):
                    state['validation'] = {'planning_feedback':reason}
                    self._recover(task,assignment,reason)
                else:
                    pause_or_block(task,reason)
        return self.store.change(ticket, apply)

    @staticmethod
    def _after_stop(task, key):
        if not current(task):
            task.update(status='paused',paused=True)
            decision(task,'stopped',assignment=key)

    def _failed_assignment(self, task, assignment, reason):
        if assignment['phase']=='package':
            self._fail_package(task,assignment,reason)
        elif assignment['phase']=='assemble':
            state = flow(task)
            key = f"{assignment['version']}:{assignment['step']}:assemble"
            if state['attempts'].get(key,0):
                pause_or_block(task,'合流受阻，已保留通过的工作包：'+reason)
            else:
                state['attempts'][key] = 1
                state['stage'] = 'execute'
                decision(task,'merge_retry',assignment=assignment['id'],reason=reason)
        else:
            self._recover(task,assignment,reason)

    @staticmethod
    def _fail_package(task, assignment, reason, *, retry=True):
        team = flow(task)['team']
        package = assignment['package']['id']
        team['failures'][package] = team['failures'].get(package,0)+1
        team['errors'][package] = reason
        decision(task,'package_failed',assignment=assignment['id'],package=package,reason=reason)
        if not retry or team['failures'][package]>REPAIR_LIMIT:
            team['blocked'] = f'工作包 {package} 受阻：{reason}'
        if team['blocked'] and not current(task):
            pause_or_block(task,team['blocked'])

    @staticmethod
    def _charge(task, assignment, report):
        evidence = report.get('evidence') if report else None
        if isinstance(evidence, dict) and type(evidence.get('started_ns')) is int and type(evidence.get('ended_ns')) is int:
            used = max(0, (evidence['ended_ns']-evidence['started_ns'])/1e9)
        elif report and assignment['phase']=='assemble' and report.get('checkpoint'):
            used = 0   # 合流只运行确定性程序，不占模型额度。
        else:
            used = assignment['max_seconds']
            task['estimated_seconds'] = task.get('estimated_seconds',0)+used
        task['elapsed_seconds'] += used
        task['calls'] += 1

    def _recover(self, task, assignment, reason):
        state = flow(task)
        stage = state['stage']
        counter = f"{assignment['version']}:{assignment['step']}:{stage}"
        attempts = state['attempts'].get(counter, 0)
        if attempts >= 1:
            if state.get('outer') and stage not in ('plan','coordinate','replan'):
                self._request_outer_coordination(task, assignment, reason)
            else:
                pause_or_block(task,reason)
            return
        state['attempts'][counter] = attempts+1
        decision(task,'recovery_reconciled',assignment=assignment['id'],reason=reason)
        # 对已有实施效果先验收。验收失败再做局部修复。
        if stage in ('execute','repair'):
            state['validation'] = {'reason':reason,'recovering_execute':True}
            state['stage'] = 'review'

    def _advance(self, task, assignment, report):
        state = flow(task)
        phase = assignment['phase']
        result = report['result']
        if state.get('outer') and phase == 'review':
            self._advance_outer_review(task,assignment,report)
            return
        if state.get('outer') and phase == 'coordinate':
            self._apply_outer_coordination(task,assignment,report)
            return
        if phase=='assemble':
            checkpoint = report['checkpoint']
            if report.get('workspace_id')!=assignment['id'] or not checkpoint.get('digest'):
                raise Problem('缺少合流工作区及检查点')
            state['workspace_base'] = assignment['id']
            self._accept_checkpoint(task,assignment,checkpoint)
            return
        evidence = report['evidence']
        if not isinstance(result,dict) or not isinstance(evidence,dict) or not evidence.get('session') or evidence.get('model') != MODEL:
            raise Problem('缺少本轮的会话与模型证据')
        if any(row['id']!=assignment['id'] and
               (row.get('report') or {}).get('evidence',{}).get('session') == evidence['session']
               for row in state['assignments']):
            raise Problem('本轮需要独立的 Codex 会话')
        if phase=='package':
            self._advance_package(task,assignment,report)
            return
        contract = state.get('verification_contract')
        if phase in ('plan','replan'):
            revision = len(state['revisions'])+1
            planned = plan_result(result,revision)
            if state.get('outer'):
                self._bind_outer_plan(task, assignment, planned)
            if not contract and report.get('verifier_profile') == 'csv-v1' and len(planned['steps']) != 1:
                raise Problem('CSV 验收器只认证一个实施阶段，请合并步骤')
            if contract:
                planned['verification_digest'] = digest(contract)
                stages = contract['stages'][task['step']:] if phase == 'replan' else contract['stages']
                if len(planned['steps']) != len(stages):
                    raise Problem('计划阶段数与固定验收契约不一致')
                for step,stage,raw in zip(planned['steps'],stages,result['steps']):
                    step['contract_stage'] = stage['id']
                    step['acceptance'] = [check['id'] for check in stage['checks']]
                    descriptions = [f"{check['id']}（{check['script']}）" for check in stage['checks']]
                    step['verification'] = ' / '.join(descriptions)
                    if state.get('team_enabled'):
                        from superagent.goal_team_plan import normalize_packages
                        if 'packages' not in raw:
                            raise Problem('团队方案必须提供工作包清单')
                        step['packages'] = normalize_packages(raw['packages'],stage)
            if phase == 'replan':
                # 已验收步骤冻结，重新规划覆盖剩余阶段。
                completed = task['plan']['steps'][:task['step']]
                remaining = task['plan']['steps'][task['step']:]
                if [step['acceptance'] for step in planned['steps']] != [step['acceptance'] for step in remaining]:
                    raise Problem('重规划改变了验收要求，需要本人批准新范围')
                if [step.get('packages',[]) for step in planned['steps']] != [step.get('packages',[]) for step in remaining]:
                    raise Problem('重规划改变了经批准的工作包边界，需要本人授权')
                planned['steps'] = completed + planned['steps']
            decision(task,'plan_saved',assignment=assignment['id'],revision=revision)
            from copy import deepcopy
            state['revisions'].append({'version':revision,'plan':deepcopy(planned),'reason':state['validation'],
                                       'previous':task['version'] if phase=='replan' else None})
            task.update(plan=planned,version=revision)
            if phase == 'plan' and state.get('outer'):
                state['stage'] = 'execute'
                task.update(status='queued', phase='execute')
            elif phase == 'plan':
                task.update(status='awaiting_approval',phase='execute',
                            approval={'id':uuid.uuid4().hex,'kind':'plan','version':revision,
                                      'state':'open','digest':digest(planned)})
            else:
                team = state.get('team') or {}
                reuse = (team.get('step')==task['step'] and state.get('workspace_base')
                         and set(team['accepted'])=={p['id'] for p in remaining[0].get('packages',[])})
                state['stage'] = 'repair' if reuse else 'execute'
                if state.get('outer'):
                    task['phase'] = 'execute'
            return
        if phase in ('execute','repair'):
            checkpoint = report['checkpoint']
            if not isinstance(checkpoint,dict) or not checkpoint.get('digest'):
                raise Problem('缺少实际工作区检查点')
            if state.get('workspace_base'):
                if report.get('workspace_id')!=assignment['id']:
                    raise Problem('缺少独立工作区检查点')
                state['workspace_base'] = assignment['id']
            self._accept_checkpoint(task,assignment,checkpoint)
            return
        validation = report['validation']
        checkpoint = report['checkpoint']
        if task['plan']['steps'][task['step']].get('packages'):
            merged = any(row['phase']=='assemble' and row['step']==task['step']
                         and (row.get('report') or {}).get('workspace_id')==row['id']
                         and not row['report'].get('error') for row in state['assignments'])
            if not merged or not state.get('workspace_base'):
                raise Problem('多包阶段须先核对并登记合流快照')
        if not isinstance(validation,dict) or not isinstance(checkpoint,dict) or not checkpoint.get('digest'):
            raise Problem('缺少独立核对结果')
        old = state['checkpoint']
        recover = (state['validation'] or {}).get('recovering_execute')
        pinned = (state['validation'] or {}).get('pinned_digest')
        if pinned and checkpoint['digest'] != pinned:
            raise Problem('独立验收的文件与回执丢失后登记的产物不一致')
        if old and old['digest'] != checkpoint['digest'] and not recover:
            raise Problem('验收产物版本与实施检查点不一致')
        checks = validation.get('checks')
        if contract:
            stages = contract['stages']
            expected_stages = stages if task['step']+1 == len(stages) else [stages[task['step']]]
            expected = [check['id'] for stage in expected_stages for check in stage['checks']]
            if (report.get('verifier_profile') != 'contract-v1'
                    or validation.get('contract_digest') != digest(contract)
                    or not isinstance(checks,list)
                    or [c.get('id') for c in checks if isinstance(c,dict)] != expected
                    or not all(isinstance(c,dict) and type(c.get('passed')) is bool for c in checks)
                    or validation.get('passed') is not all(c['passed'] for c in checks)):
                raise Problem('验收回执缺少本阶段或最终交付所需的检查')
        state['validation'] = dict(validation, reviewer_findings=result.get('findings',[]))
        passed = result.get('passed') is True and bool(result.get('findings')) and validation.get('passed') is True
        decision(task,'evaluated',assignment=assignment['id'],passed=passed)
        if not passed:
            if state['repairs'] < REPAIR_LIMIT:
                state['repairs'] += 1
                state['stage'] = 'repair'
            elif state['replans'] < 1:
                state['replans'] += 1
                state['repairs'] = 0
                state['stage'] = 'replan'
            else:
                pause_or_block(task,'独立验收反复失败，已保留产物与报告')
            return
        if not isinstance(checks,list) or not checks or not all(isinstance(c,dict) and c.get('passed') is True for c in checks):
            raise Problem('缺少逐项通过的验证结果')
        delivery = report.get('delivery')
        final = task['step']+1 == len(task['plan']['steps'])
        if final and (not isinstance(delivery,dict) or delivery.get('assignment') != assignment['id']
                      or not checkpoint.get('files') or delivery.get('files') != checkpoint['files']):
            raise Problem('缺少绑定本轮验收的交付快照')
        state['checkpoint'] = checkpoint
        state['repairs'] = 0
        task['plan']['steps'][task['step']]['state'] = 'completed'
        task['step'] += 1
        if task['step'] < len(task['plan']['steps']):
            state['stage'] = 'execute'
            task['phase'] = 'execute'
            return
        task.update(status='completed',phase='review',artifacts=list(checkpoint['artifacts']),
                    checks=checks, review=result, goal_delivery=delivery)
        decision(task,'delivered',assignment=assignment['id'],digest=checkpoint['digest'])

    @staticmethod
    def _bind_outer_plan(task, assignment, planned):
        state = flow(task)
        outer = state['outer']
        contract = outer['acceptance_contract']
        known = {row['criterion_id']:row for row in contract['criteria']}
        used = set()
        for step in planned['steps']:
            keys = step['acceptance']
            if (len(keys) != len(set(keys)) or used.intersection(keys) or not set(keys) <= known.keys()
                    or not any(known[key]['required'] for key in keys)):
                raise Problem('实施计划的每个阶段必须绑定真实且不重复的必需验收项')
            used.update(keys)
            step['verification'] = ' / '.join(known[key]['description'] for key in keys)
        if assignment['phase']=='plan' and any(row['required'] and key not in used for key,row in known.items()):
            raise Problem('实施计划未覆盖已批准的必要验收项')
        planned['acceptance_revision'] = outer['acceptance_revision']
        planned['verification_digest'] = digest(contract)

    def _advance_outer_review(self, task, assignment, report):
        from superagent.outer_verification import validate_results
        state = flow(task)
        contract = state['outer']['acceptance_contract']
        checkpoint = report['checkpoint']
        baseline = state['checkpoint']
        if (report['verifier_profile'] != 'outer-v2' or not isinstance(checkpoint,dict) or not baseline
                or checkpoint != baseline or not checkpoint.get('digest')):
            raise Problem('独立验收和实施检查点必须绑定同一交付版本')
        rows = report['validation']['checks']
        passed = validate_results(contract,task['plan'],assignment,checkpoint,rows)
        final = task['step']+1 == len(task['plan']['steps'])
        state['validation'] = {'passed':passed,'checks':rows,'artifact_sha256':checkpoint['digest'],
                               'acceptance_revision':state['outer']['acceptance_revision'],
                               'level':'goal' if final else 'stage'}
        decision(task,'evaluated',assignment=assignment['id'],run=assignment['run'],
                 passed=passed,level=state['validation']['level'],
                 evidence_refs=[row['evidence_ref'] for row in rows],
                 correlation_id=state['outer']['correlation_id'])
        if not passed:
            self._request_outer_coordination(task,assignment,'已批准的必要验收项尚未全部通过')
            return
        if contract['status'] != 'APPROVED' or contract['verification_gaps']:
            raise Problem('仍有须明确确认的验收缺口')
        delivery = report.get('delivery')
        if (not isinstance(delivery,dict) or delivery.get('assignment')!=assignment['id']
                      or not checkpoint.get('files') or delivery.get('files')!=checkpoint['files']
                or delivery.get('artifact_sha256')!=checkpoint['digest']
                or delivery.get('level') != ('goal' if final else 'stage')):
            raise Problem('缺少与当前验收版本和派工匹配的归档')
        from superagent.outer_verification import needs_human_signoff
        if final and needs_human_signoff(contract):
            reviews = state.setdefault('artifact_reviews', [])
            if reviews and reviews[-1]['state'] == 'returned' and reviews[-1]['artifact_sha256'] == checkpoint['digest']:
                pause_or_block(task,'退回后作品内容没有变化，请核对修复情况')
                return
            outer = state['outer']
            reviews.append(dict(delivery, sequence=len(reviews)+1, state='pending', offered_at=now(),
                                checks=rows, plan_revision=task['version'],human_criteria=[
                                    {'id':r['criterion_id'],'description':r['description']} for r in contract['criteria']
                                    if r['required'] and r.get('verification_type')=='HUMAN_REVIEW'],
                                **{key:outer[key] for key in ('goal_revision','acceptance_revision','mandate_revision')}))
            state.update(stage='awaiting_artifact_review',outcome='AWAITING_ARTIFACT_REVIEW')
            task.update(status='awaiting_artifact_review',phase='review',checks=rows,error='')
            decision(task,'artifact_offered',assignment=assignment['id'],digest=checkpoint['digest'],sequence=len(reviews))
            return
        task['plan']['steps'][task['step']]['state'] = 'completed'
        task['step'] += 1
        state['repairs'] = 0
        if not final:
            state.setdefault('stage_deliveries',[]).append(delivery)
            state['outcome'] = 'PARTIAL_DELIVERY'
            state['stage'] = 'execute'
            task['phase'] = 'execute'
            return
        state['stage'] = 'verified_complete'
        state['outcome'] = 'VERIFIED_COMPLETE'
        task.update(status='completed',phase='review',artifacts=list(checkpoint['artifacts']),
                    checks=rows,review={'passed':True,'findings':['当前验收契约必需项全部通过']},
                    goal_delivery=delivery)
        decision(task,'delivered',assignment=assignment['id'],digest=checkpoint['digest'],
                 outcome=state['outcome'],correlation_id=state['outer']['correlation_id'])

    def _request_outer_coordination(self, task, assignment, reason):
        state = flow(task)
        validation = state['validation'] or {}
        required = {row['criterion_id'] for row in state['outer']['acceptance_contract']['criteria']
                    if row['required'] and row.get('verification_type')!='HUMAN_REVIEW'}
        failed = [row for row in validation.get('checks',[]) if row['id'] in required and row['status']!='PASS']
        identity = digest({'step':task['step'],'artifact':(state['checkpoint'] or {}).get('digest'),
                           'results':[(row['id'],row['status'],row['detail']) for row in failed],
                           'reason':reason})
        seen = state.setdefault('outer_failures',{})
        seen[identity] = seen.get(identity,0)+1
        if seen[identity] > 2:
            pause_or_block(task,'同一交付版本和失败条件反复出现，已保留验收证据')
            state['outcome'] = 'BLOCKED'
            return
        state['coordination'] = {'assignment':assignment['id'],'reason':reason,'failure':identity,
                                 'evidence_refs':[row['evidence_ref'] for row in failed] or [assignment['id']]}
        state['stage'] = 'coordinate'
        state['outcome'] = 'PARTIAL_DELIVERY' if task['step'] else None
        task['phase'] = 'review'

    def _outer_coordination_context(self, task):
        state = flow(task)
        outer = state['outer']
        request = state['coordination']
        validation = state['validation'] or {}
        remaining = max(0,int(state['mandate']['budget_seconds'] - task['elapsed_seconds']))
        actions = ['REQUEST_APPROVAL','BLOCKED']
        # 缺少验收环境或证据时允许一次复验，不能用这类错误驱动代码返工。
        rows = validation.get('checks',[])
        references = set(request['evidence_refs'])
        failed = [row for row in rows if row['evidence_ref'] in references and row['status']!='PASS']
        if remaining >= 30:
            if failed and all(row['status']=='ERROR' for row in failed):
                if not state['rechecks']:
                    actions.append('CONTINUE')
            elif not any(row['status'] in ('ERROR','INCONCLUSIVE','NOT_RUN') for row in failed):
                if state['repairs'] < REPAIR_LIMIT:
                    actions.append('REPAIR')
                if state['replans'] < 1:
                    actions.append('REPLAN')
        checks = [
            {key:row[key] for key in ('criterion_id','status','detail','evidence_ref','artifact_sha256')}
            for row in rows
        ]
        stage_evidence = [
            {key:delivery[key] for key in ('assignment','artifact_sha256','level')}
            for delivery in state.get('stage_deliveries',[])
        ]
        from superagent.goal_codex import phase_sandboxes
        from superagent.outer_intake import _model_text
        last_implementation = next((row for row in reversed(state['assignments'])
                                    if row['phase'] in ('execute','repair') and row.get('report')), None)
        implementation = (last_implementation['report'].get('result') or {}) if last_implementation else {}
        notes = self.store.notes(task['id'])
        context = {
            'phase_sandboxes':phase_sandboxes(),
            'last_implementation_summary':_model_text(str(implementation.get('summary','')),500),
            'recent_operator_notes':[_model_text(note['body'],500) for note in notes[-3:]],
            'ticket_id':outer['ticket_id'],'goal_revision':outer['goal_revision'],
            'acceptance_revision':outer['acceptance_revision'],'mandate_revision':outer['mandate_revision'],
            'plan_revision':task['version'],'stage':task['step'],
            'goal':task['goal'],'plan':task['plan'],'acceptance':[
                {key:row[key] for key in ('criterion_id','description','requirement_ids','required')}
                for row in outer['acceptance_contract']['criteria']
            ],
            'scope': state['mandate']['scope'], 'forbidden_operations':state['mandate']['forbidden_operations'],
            'failure':request,'verification_checks':checks,'remaining_budget_seconds':remaining,
            'retained_outputs':(state['checkpoint'] or {}).get('artifacts',[])[:32],
            'verified_stages':stage_evidence,
            'evidence_refs':request['evidence_refs'],'available_actions':actions,
        }
        context['input_snapshot_revision'] = digest([context,outer_binding(task)])
        return context

    def _apply_outer_coordination(self, task, assignment, report):
        from superagent.outer_verification import validate_coordination
        state = flow(task)
        context = dict(assignment['coordination_context'])
        proposal = validate_coordination(report['result'],context)
        invocation = report['evidence']
        if (invocation.get('snapshot_revision') != context['input_snapshot_revision']
                or invocation.get('invocation_id') != assignment['id']):
            raise Problem('协调建议未绑定有效的模型调用')
        current_context = self._outer_coordination_context(task)
        action = proposal['action']
        if (action not in current_context['available_actions'] or
                (action in ('CONTINUE','REPAIR','REPLAN') and task.get('max_calls') is not None
                 and task['calls'] >= task['max_calls'])):
            pause_or_block(task,'协调完成时剩余预算不足或建议已过期',budget=True)
            return
        decision(task,'coordinated',assignment=assignment['id'],run=assignment['run'],proposal=proposal,
                 invocation=invocation,correlation_id=state['outer']['correlation_id'])
        if action == 'REQUEST_APPROVAL':
            pause_or_block(task,proposal['reason'])
            state['outcome'] = 'AWAITING_REVIEW'
        elif action == 'BLOCKED':
            pause_or_block(task,proposal['reason'])
            state['outcome'] = 'BLOCKED'
        elif action == 'CONTINUE':
            state['rechecks'] += 1
            state['stage'] = 'review'
        else:
            if action == 'REPLAN':
                state['replans'] += 1
                state['repairs'] = 0
            else:
                state['repairs'] += 1
            state['validation'] = dict(state['validation'] or {}, coordination=proposal)
            state['stage'] = action.lower()
            task['phase'] = 'plan' if action=='REPLAN' else 'execute'

    @staticmethod
    def _accept_checkpoint(task, assignment, checkpoint):
        state = flow(task)
        state.update(checkpoint=checkpoint,stage='review')
        task['phase'] = 'review'
        decision(task,'checkpoint_saved',assignment=assignment['id'],digest=checkpoint['digest'])

    @staticmethod
    def _advance_package(task, assignment, report):
        package = assignment['package']
        outputs = report['outputs']
        validation = report['validation']
        checks = validation.get('checks')
        if (validation.get('contract_digest')!=assignment['verification_digest']
                or not isinstance(checks,list)
                or [(c.get('id'),c.get('passed')) for c in checks]!=[(c,True) for c in package['checks']]
                or validation.get('passed') is not True
                or not isinstance(outputs,dict) or set(outputs)!=set(package['outputs'])
                or not all(isinstance(value,str) and len(value)==64
                           and all(c in '0123456789abcdef' for c in value) for value in outputs.values())):
            raise Problem('工作包产物或确定性核验不完整')
        flow(task)['team']['accepted'][package['id']] = {
            'assignment':assignment['id'],'package':package['id'],'files':outputs,
        }
        decision(task,'package_accepted',assignment=assignment['id'],package=package['id'],run=assignment['run'])

    def assignment_input(self, ticket, key, run):
        task = self.store.get(ticket)
        state = flow(task)
        assignment = current(task,key)
        if not assignment or task['paused']:
            raise Problem('派工已过期或暂停',409)
        if assignment['run'] is None:
            return None
        if (assignment['run'] != run or assignment['version'] != task['version']
                or assignment['mandate_digest'] != digest(state['mandate'])
                or assignment.get('verification_digest',digest(None)) != digest(state.get('verification_contract'))
                or assignment.get('outer_binding') != outer_binding(task)):
            raise Problem('派工与中台轮次或授权不匹配',409)
        context = {'notes':self.store.notes(ticket),'verification':state['validation'],
                   'team_enabled':state.get('team_enabled',False)}
        reviews = state.get('artifact_reviews') or []
        if assignment['phase']=='repair' and reviews and reviews[-1]['state']=='returned':
            from superagent.outer_intake import _model_text
            context['human_review_feedback'] = {k:reviews[-1][k] for k in ('sequence','artifact_sha256','reason')}
            context['human_review_feedback']['reason'] = _model_text(reviews[-1]['reason'],2000)
        if assignment.get('outer_binding'):
            # 保留已批准的原始需求和限制。授权仍由独立的 Mandate 约束。
            from superagent.goal_codex import phase_sandboxes
            context['phase_sandboxes'] = phase_sandboxes()
            context['approved_goal_contract'] = state['outer']['goal_contract']
            if assignment['phase']=='coordinate':
                context['coordinator'] = assignment['coordination_context']
            context['execution_contract'] = {
                **assignment['outer_binding'], 'assignment':assignment['id'], 'run':run,
                'plan_revision':assignment['version'], 'work_package':None, 'stage':assignment['step'],
                'workspace_id':ticket, 'baseline':(state.get('checkpoint') or {}).get('digest'),
                'prior_stage_evidence':state.get('stage_deliveries',[]),
                'max_seconds':assignment['max_seconds'],
                'scope':state['mandate']['scope'],
                'forbidden_operations':state['mandate']['forbidden_operations'],
            }
        if assignment['phase']=='package':
            context.update(package=assignment['package'], retry=assignment.get('retry_context'),
                           dependency_outputs=[item['files'] for item in assignment['inputs']])
        return dict(assignment, goal=task['goal'], acceptance=task['acceptance'], mandate=state['mandate'],
                    plan=task['plan'], context=context, checkpoint=state['checkpoint'],
                    verification_contract=state.get('verification_contract'),workspace_base=state.get('workspace_base'),
                    outer_acceptance=(state.get('outer') or {}).get('acceptance_contract'))

    @contextmanager
    def launch_gate(self,ticket,key,run):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = json.loads(db.execute('SELECT body FROM tasks WHERE id=?',(ticket,)).fetchone()[0])
            assignment = current(task,key)
            if (not assignment or assignment['run'] != run or task['paused']
                    or flow(task)['stop_requested'] or assignment['version'] != task['version']
                    or assignment['mandate_digest'] != digest(flow(task)['mandate'])
                    or assignment.get('verification_digest',digest(None)) != digest(flow(task).get('verification_contract'))
                    or assignment.get('outer_binding') != outer_binding(task)):
                raise Problem('派工已停止或变更，未放行模型',409)
            yield
