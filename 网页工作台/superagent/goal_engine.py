"""目标工单状态机。Autoloop 负责运行事实，工作台事务负责业务决定。"""
import hashlib
import json
import re
import uuid
from contextlib import contextmanager

from workbench import Problem, is_blind_goal, now, require_v1_control
from superagent.outer_verification import ROUTER_TEAM, SCOPED_COORDINATOR
from superagent.goal_team_handoff import explicit_keep_answer
from superagent.team_profiles import ANTIGRAVITY, role_runtime, supported as supported_team, uses_antigravity
from superagent.goal_team_records import (
    ADAPTIVE_REVIEW, MIN_REPAIR_ASSIGNMENT_SECONDS, MIN_TEAM_ASSIGNMENT_SECONDS, PREFERENCE_REVIEW_MODES,
    TEAM_REVIEW_MODES, profile, role_limit,
)
from superagent.goal_team_repair import valid_verification


MODEL = 'gpt-6.2-sol'
RULE = 'odyssey-goal'
EVENT = 'workbench.goal.assignment'
POLICY_VERSION = 1
CALL_LIMIT_SECONDS = 240
TEAM_DRAFT_LIMIT_SECONDS = 330
DRAFT_ROLE_ORDER = (('lead',False),('advisor',False),('lead',True),('advisor',False),('lead',False))
REPAIR_LIMIT = 2
PACKAGE_LIMIT = 4
PACKAGE_ROUTES = {
    'implement': ('workbench.goal.implement', 'odyssey-goal-implement'),
    'analyze': ('workbench.goal.analyze', 'odyssey-goal-analyze'),
}


class StaleCoordination(Problem):
    """当前提案的后续派工尚未启动，用户依据已改变。"""


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


def team_candidate_assignment(assignment):
    return bool(assignment.get('execution_profile')) and assignment['phase'] in ('execute','repair')


def resumable_drafting(assignment):
    return (assignment.get('team_review_mode') and assignment.get('execution_profile', {}).get('version', 1) >= 2
            and not assignment.get('team_handoff_continuation') and not assignment.get('repair_continuation'))


def draft_role_prefix(assignment):
    completed = list((assignment.get('draft_resume') or {}).get('calls', ()))
    for call in assignment.get('role_calls', ()):
        if call.get('status') != 'completed':
            break
        completed.append(call)
    if tuple((call.get('role'),call.get('writable')) for call in completed) != DRAFT_ROLE_ORDER[:len(completed)]:
        raise Problem('审稿断点的原角色顺序无法核对')
    return completed


def team_execution_route(task):
    steps = (task.get('plan') or {}).get('steps', ())
    return steps[task['step']].get('execution_route') if task['step'] < len(steps) else None


def team_review_mode(task, phase):
    if phase != 'execute':
        return None
    mode = flow(task)['outer']['mandate'].get('team_review_mode')
    if mode == ADAPTIVE_REVIEW and not is_blind_goal(task):
        return mode if team_execution_route(task) != 'direct' else None
    return mode if mode == 'review' or (mode == 'review_and_decide' and task['step'] > 0) else None


def team_proposal_owner(task, mode):
    if mode == ADAPTIVE_REVIEW and team_execution_route(task) == 'compare':
        return task['plan']['steps'][task['step']].get('direction_owner')
    return None


def team_decision_required(task, mode):
    return ((mode == 'review_and_decide' and task['step'] == 1)
            or team_proposal_owner(task, mode) == 'owner')


def team_preference_for_step(task):
    question = flow(task).get('pending_preference') or {}
    # 旧版只在首阶段提问，原单里的问题没有 step 字段。
    return question if question.get('step',0) == task['step'] else {}


def report_matches_assignment(report, assignment):
    return (isinstance(report,dict) and report.get('id') == assignment['id']
            and type(report.get('run')) is int and report['run'] == assignment['run']
            and type(report.get('version')) is int and report['version'] == assignment['version']
            and report.get('outer_binding') == assignment.get('outer_binding'))


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
    if 'team_review_mode' in mandate and (
            mandate['team_review_mode'] not in TEAM_REVIEW_MODES or not supported_team(mandate.get('execution_profile'))
            or (mandate['team_review_mode'] == ADAPTIVE_REVIEW and mandate.get('workflow_profile') != ROUTER_TEAM)):
        raise ValueError('审稿方式超出已安装的执行能力')
    validate_constraint_controls(goal,contract,mandate)


def pause_or_block(task, message, *, budget=False):
    task.update(error=message, status='budget_exhausted' if budget else 'blocked')
    decision(task, 'blocked', reason=message)


def plan_result(result, version, *, max_checks=8):
    raw = result.get('steps')
    if not isinstance(raw, list) or not 1 <= len(raw) <= 6:
        raise Problem('计划必须有 1 至 6 个带验收项的阶段')
    steps = []
    for item in raw:
        objective = item.get('objective') if isinstance(item, dict) else None
        checks = item.get('checks') if isinstance(item, dict) else None
        if (not isinstance(objective, str) or not objective.strip() or len(objective) > 1500
                or not isinstance(checks, list) or not 1 <= len(checks) <= max_checks
                or any(not isinstance(check, str) or not check.strip() or len(check) > 500 for check in checks)):
            raise Problem('阶段缺少可验证验收项或超出长度')
        steps.append({'title': objective[:90], 'objective': objective, 'acceptance': checks,
                      'verification': ' / '.join(checks), 'state': 'pending'})
    summary = result.get('summary')
    if not isinstance(summary,str) or not summary.strip() or len(summary) > 3000:
        raise Problem('缺少有效方案摘要')
    return {'version': version, 'summary': summary, 'scope': '仅在授权的本机工作区操作',
            'assumptions': [], 'steps': steps}


_COMPARE_REQUEST = re.compile(
    r'(?:^|[。！？；])(?:请|需要|要求|先|明确要求|必须)?(?:先)?(?:比较|对比)'
    r'|(?:比较|对比)(?:两种|两个|多种|不同|备选|候选|各|几种)(?:方案|做法|设计|路径)'
    r'|(?:两份|两个|两种|多份)(?:相互)?独立(?:的)?(?:方案|提案|意见)'
    r'|\b(?:compare (?:the |two |several )?(?:alternatives|approaches|options)|independent proposals)\b',
    re.I,
)
_COMPARE_NEGATION = re.compile(r'无需|不用|不必|不要|不需要|不作|免于|no need to|do not|never', re.I)


def explicit_comparison_request(requirements):
    clauses = [clause for text in requirements for clause in re.split(r'[。！？；，;,.!]', text)
               if _COMPARE_REQUEST.search(clause.strip())]
    if clauses:
        return any(not _COMPARE_NEGATION.search(clause) for clause in clauses)
    return None


def bind_router_route(task, assignment, planned, result):
    mandate = flow(task)['outer']['mandate']
    if mandate.get('workflow_profile') != ROUTER_TEAM or mandate.get('team_review_mode') != ADAPTIVE_REVIEW:
        return
    if is_blind_goal(task):
        return
    previous = task['plan'] or {}
    if assignment['phase'] == 'replan':
        for key in ('execution_route','route_reason','comparison_steps'):
            if key in previous:
                planned[key] = previous[key]
        for step, old_step in zip(planned['steps'], previous['steps'][task['step']:]):
            for key in ('execution_route', 'direction_owner'):
                if key in old_step:
                    step[key] = old_step[key]
        return

    goal = flow(task)['outer']['goal_contract']
    requirements = [
        row['description'] for field in ('required_capabilities','deliverables','nonfunctional_requirements')
        for row in goal.get(field, ()) if row.get('source') in ('USER_EXPLICIT','USER_CONFIRMED')
    ]
    if followup := goal.get('continued_from'):
        # 本轮只改日期时，上轮已兑现的“先比较”不再是一项新的流程指令。
        requirements = [followup['change_request']]
    order = explicit_comparison_request(requirements)
    if followup and isinstance(goal.get('round_comparison_request'), bool):
        order = goal['round_comparison_request']
    if goal.get('proposal_requested'):
        order = True
    compare_requested = order is True
    skip_comparison = order is False
    route = result.get('execution_route')
    reason = result.get('route_reason')
    selected = result.get('comparison_steps')
    if not assignment.get('plan_route_required') and route is None and reason is None and selected is None:
        # 升级前已派发的计划从原审稿路径恢复。
        return
    if (route not in ('direct','compare') or not isinstance(reason,str) or not reason.strip() or len(reason)>400
            or not isinstance(selected,list)
            or any(type(index) is not int or not 1 <= index <= len(planned['steps']) for index in selected)
            or len(set(selected)) != len(selected) or bool(selected) != (route == 'compare')):
        raise Problem('实施计划缺少有效的复杂度判定或实际需比较的阶段')
    if compare_requested and route == 'direct':
        route, reason, selected = 'compare', '用户明确要求比较方案，由独立顾问在首个实施阶段提案并审稿。', [1]
    elif skip_comparison and route == 'compare':
        route, reason, selected = 'direct', '用户明确要求按既定做法直接实施。', []
    if goal.get('direction_to_compare') and route == 'direct' and not skip_comparison:
        route, reason, selected = 'compare', '用户目标明确，团队先比较会改变交付结果的方向。', [1]
    if goal.get('proposal_requested') and 1 not in selected:
        selected = [1, *selected]
        reason = '按原单的方案选择，从第一个实施阶段开始比较。'
    planned.update(execution_route=route, route_reason=reason.strip(), comparison_steps=sorted(selected))
    for index, step in enumerate(planned['steps'], 1):
        step['execution_route'] = 'compare' if index in selected else 'direct'
        if index in selected and goal.get('direction_owner') in ('team', 'owner'):
            step['direction_owner'] = goal['direction_owner']

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
            if outer and (phase=='review' or (phase in ('execute','repair') and 'coordination_retention' in state)):
                minimum = self._minimum_review_seconds(task)
                if phase!='review':
                    remaining -= minimum
                    minimum = 10
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
            if profile(state):
                if not supported_team(profile(state)):
                    pause_or_block(task,'无法识别冻结的团队执行配置')
                    return
                if phase in ('execute','repair'):
                    if 'coordination_retention' not in state:
                        remaining -= self._minimum_review_seconds(task)
                    pending = state.get('team_pending_repair')
                    handoff = state.get('team_pending_handoff')
                    draft = self._draft_resume(task) if not pending and not handoff and team_review_mode(task,phase) else None
                    conclusion_only = draft and len(draft['calls']) == len(DRAFT_ROLE_ORDER)
                    if any(record and not self._same_team_candidate_scope(task,record)
                           for record in (pending,handoff)) or (
                               handoff and handoff['mode'] != team_review_mode(task,phase)):
                        pause_or_block(task,'待续办草稿所属的计划或验收版本已变化，需要重新核对')
                        return
                    answer = self._handoff_answer(task,handoff) if handoff and handoff['decision_required'] else None
                    if handoff and handoff['decision_required'] and not answer:
                        pause_or_block(task,'继续实施前须在原单确认草稿方向')
                        return
                    if pending:
                        reviewing_repair = 'review_only' in pending
                        slots_missing = max(0, (1 if reviewing_repair else 2) - self._team_role_slots(state))
                        seconds_missing = max(0, MIN_REPAIR_ASSIGNMENT_SECONDS - int(remaining))
                        if slots_missing or seconds_missing:
                            missing = [label for amount, label in (
                                (slots_missing, f'{slots_missing} 个角色轮次'),
                                (seconds_missing, f'{seconds_missing} 秒可用时间'),
                            ) if amount]
                            action = '继续复验' if reviewing_repair else '继续返修'
                            saved = '返修新版和问题已保存。' if reviewing_repair else '原候选和问题已保存。'
                            pause_or_block(task, action + '还需' + '和'.join(missing) + '。' + saved, budget=True)
                            return
                    elif self._team_role_slots(state) <= 0 and not conclusion_only:
                        pause_or_block(task,'内层角色调用额度用完',budget=True)
                        return
                    elif remaining < MIN_TEAM_ASSIGNMENT_SECONDS:
                        pause_or_block(task,'剩余预算不足以实施并保留独立验收',budget=True)
                        return
                    elif handoff or team_review_mode(task,phase):
                        needed = 5
                        if handoff:
                            needed = 1 if explicit_keep_answer(handoff['handoff']['instruction'],answer) else 2
                        else:
                            if draft:
                                needed = len(DRAFT_ROLE_ORDER)-len(draft['calls'])
                        missing = needed - self._team_role_slots(state)
                        if missing > 0:
                            pause_or_block(task,f'审稿及续办本轮还需 {missing} 个角色轮次',budget=True)
                            return
            if phase == 'coordinate':
                if state.get('coordinator_profile') == SCOPED_COORDINATOR:
                    inputs = self._outer_coordination_inputs(task['id'])
                    if inputs.get('pending'):
                        return
                    ticket = inputs.get('ticket')
                    if ticket is not None and (ticket['status'] != 'AUTHORIZED' or any(
                            ticket[key] != outer[key] for key in ('goal_revision','acceptance_revision','mandate_revision'))):
                        pause_or_block(task,'工单已有变更，等待确认当前目标和授权')
                        return
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

    @staticmethod
    def _team_original_requirements(task):
        # 只交接已批准且可追溯到本人原话的条款。
        from superagent.outer_intake import _model_text
        state = flow(task)
        outer = state['outer']
        steps = (task['plan'] or {}).get('steps', [])
        acceptance = set(steps[task['step']]['acceptance']) if task['step'] < len(steps) else set()
        relevant = {requirement for row in outer['acceptance_contract']['criteria']
                    if row['criterion_id'] in acceptance for requirement in row.get('requirement_ids', ())}
        goal = outer['goal_contract']
        return [
            {'requirement_id':item['requirement_id'],'text':_model_text(item['description'],420),
             'source_refs':item['source_refs']}
            for field in ('deliverables','required_capabilities','nonfunctional_requirements','constraints','out_of_scope')
            for item in goal.get(field, ())
            if item.get('source') in ('USER_EXPLICIT','USER_CONFIRMED')
            and (item['requirement_id'] in relevant or field in ('constraints','out_of_scope'))
        ][:20]

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
    def _team_role_slots(state):
        used = sum(len(row.get('role_calls', [])) for row in state['assignments'])
        return min(role_limit(state)-used, profile(state)['max_roles_per_assignment'])

    @staticmethod
    def _same_team_candidate_scope(task, pending):
        state = flow(task)
        return pending['version'] == task['version'] and pending['step'] == task['step'] and (
            pending['phase'] == state['stage']
            and pending['contract_digest'] == digest(state['outer']['acceptance_contract'])
            and pending['goal_digest'] == digest(state['outer']['goal_contract']))

    @staticmethod
    def _pending_candidate_base(pending):
        return {
            'assignment':pending['source_assignment'], 'artifact_sha256':pending['base_sha256'],
            'files':pending['files'],
        }

    @staticmethod
    def _recoverable_draft_end(assignment):
        report = assignment.get('report')
        if report and report.get('draft_checkpoint') is True and not report.get('error'):
            return True
        if report and report.get('draft_time_slice_ended') is True:
            return True
        last = (assignment.get('role_calls') or [{}])[-1]
        return (last.get('error_code') in ('timeout','process_timeout')
                or report is None and assignment.get('transport') in ('nothing','cancelled','done'))

    @staticmethod
    def _draft_resume(task):
        state = flow(task)
        stage = state['stage']
        previous = next((row for row in reversed(state['assignments'])
                         if row['phase'] == stage and row['step'] == task['step'] and row['version'] == task['version']), None)
        if not previous or previous['state'] != 'settled' or not resumable_drafting(previous):
            return None
        old = previous.get('outer_binding') or {}
        live = outer_binding(task)
        previous_error = (previous.get('report') or {}).get('error')
        answer = team_preference_for_step(task).get('answer') or {}
        if (not live or not GoalEngine._draft_authorization_continues(state,old,live)
                or previous.get('preference_event') != answer.get('event')
                or previous.get('execution_profile') != profile(state)
                or previous['team_review_mode'] != team_review_mode(task,stage)
                or not (GoalEngine._recoverable_draft_end(previous) or previous_error)):
            return None
        calls = draft_role_prefix(previous)
        if len(calls) == len(DRAFT_ROLE_ORDER) and previous_error and not GoalEngine._recoverable_draft_end(previous):
            calls = calls[:-1]
        old_resume = previous.get('draft_resume') or {}
        stalled = 0 if len(calls) > len(old_resume.get('calls', ())) else old_resume.get('stalled',0)+1
        return {'calls':calls, 'stalled':stalled, 'previous_assignment':previous['id']}

    @staticmethod
    def _draft_authorization_continues(state, old, live):
        budget_fields = {'mandate_revision','approval_event_id','authorization_digest'}
        if any(old.get(key) != live[key] for key in set(live)-budget_fields-{'stop_generation'}):
            return False
        if old.get('mandate_revision') == live['mandate_revision']:
            return all(old.get(key) == live[key] for key in budget_fields)
        # 经本人另签的追加预算复制原权限，只改变资源额度与批准记录。
        revision, approval = old.get('mandate_revision'), old.get('approval_event_id')
        for event in state['decisions']:
            if event['kind'] == 'budget_extended' and event['old_mandate'] == revision:
                revision, approval = event['mandate'], event['approval']
        return (revision,approval) == (live['mandate_revision'],live['approval_event_id'])

    @staticmethod
    def _handoff_answer(task, handoff):
        question = team_preference_for_step(task)
        answer = question.get('answer') or {}
        selected = answer.get('option')
        if (question.get('requires_option') is True
                and question.get('assignment') == handoff['source_assignment']
                and question.get('draft_sha256') == handoff['base_sha256']
                and answer.get('event') and isinstance(selected,dict)
                and selected in question.get('options', ())
                and answer.get('option_id') == selected.get('id')):
            return answer
        return None

    @staticmethod
    def _defer_team_repair(task, assignment, report):
        state = flow(task)
        notes = assignment.get('inner_repairs') or []
        last = (assignment.get('role_calls') or [{}])[-1]
        repair = notes[-1] if notes else {}
        scope = repair.get('scope') or {}
        checkpoint = report['checkpoint']
        finding = report['result']
        binding = assignment['outer_binding']
        if (state.get('team_pending_repair') or assignment.get('repair_continuation')
                or not isinstance(checkpoint, dict) or not checkpoint.get('digest') or not checkpoint.get('files')
                or not isinstance(finding, dict) or repair.get('status') != 'waiting'
                or repair.get('before_sha256') != checkpoint['digest']
                or scope.get('before_files') != checkpoint['files']
                or scope.get('before_sha256') != checkpoint['digest']
                or scope.get('acceptance_sha256') != binding['contract_digest']
                or last.get('action') != 'repair' or finding.get('action') != 'repair'
                or not isinstance(finding.get('repair_scope'), dict)
                or any(finding.get(field) != last.get(field)
                       for field in ('summary', 'retain', 'verify', 'repair_scope'))):
            raise Problem('返修续办缺少与原问题和文件绑定的完整回执')
        state['team_pending_repair'] = {
            'source_assignment':assignment['id'], 'base_sha256':checkpoint['digest'],
            'files':checkpoint['files'], 'finding':finding, 'repair_record':repair,
            'phase':assignment['phase'], 'step':assignment['step'], 'version':assignment['version'],
            'goal_digest':binding['goal_digest'], 'contract_digest':binding['contract_digest'],
        }
        task.update(status='queued', error='')
        decision(task,'inner_repair_deferred', assignment=assignment['id'], digest=checkpoint['digest'])

    @staticmethod
    def _defer_team_review(task, assignment, report, calls):
        state = flow(task)
        repair = (assignment.get('inner_repairs') or [{}])[-1]
        pending = state.get('team_pending_repair')
        continuation = assignment.get('repair_continuation')
        checkpoint = report['checkpoint']
        evidence = report['repair_review_pending']
        scope = repair.get('scope') or {}
        binding = assignment['outer_binding']
        writer = calls[-1]
        if (not isinstance(evidence,dict) or set(evidence) != {'implementation','before_checks'}
                or not isinstance(evidence['implementation'],dict)
                or bool(pending) != bool(continuation) or (pending and 'review_only' in pending)
                or (continuation and (report['result'] != continuation['finding']
                                     or scope != continuation['repair_record']['scope']))
                or 'preference' in report or 'deferred_repair' in report or report['result']['action'] != 'repair'
                or repair.get('status') not in ('rechecking','passed') or not checkpoint['files']
                or repair.get('after_sha256') != checkpoint['digest'] or not repair.get('changed_files')
                or scope.get('acceptance_sha256') != binding['contract_digest']
                or repair.get('before_sha256') != scope.get('before_sha256')
                or repair.get('issue') != report['result']['summary']
                or writer.get('action') != evidence['implementation'].get('action')
                or writer.get('summary') != evidence['implementation'].get('summary')
                or writer.get('summary') != repair.get('changes')
                or writer.get('candidate_sha256') != checkpoint['digest']
                or (writer.get('role'),writer.get('writable')) != ('implementer',True)):
            raise Problem('无法将待复验的新版与返修范围及执行者回执绑定')
        rows = evidence['before_checks']
        clauses = scope['criteria']
        if (not isinstance(rows,list) or len(rows) != len(clauses)
                or any(not isinstance(row,dict) or row.get('status') not in ('PASS','FAIL')
                       or any(row.get(key) != value for key,value in clause.items())
                       for row,clause in zip(rows,clauses))):
            raise Problem('返修新版缺少对应原问题的客观检查记录')
        repair.update(status='waiting_review',review='已保存执行者的返修新版，等待负责人复验。')
        state['team_pending_repair'] = {
            'source_assignment':assignment['id'], 'base_sha256':checkpoint['digest'],
            'files':checkpoint['files'], 'finding':report['result'], 'repair_record':dict(repair),
            'review_only':evidence, 'phase':assignment['phase'], 'step':assignment['step'],
            'version':assignment['version'], 'goal_digest':binding['goal_digest'],
            'contract_digest':binding['contract_digest'],
        }
        task.update(status='queued', error='')
        decision(task,'inner_repair_review_deferred',assignment=assignment['id'],digest=checkpoint['digest'])

    @staticmethod
    def _enqueue(task, phase, seconds, **details):
        state = flow(task)
        mandate = (state.get('outer') or {}).get('mandate', {})
        if (phase == 'plan' and mandate.get('workflow_profile') == ROUTER_TEAM
                and mandate.get('team_review_mode') == ADAPTIVE_REVIEW and not is_blind_goal(task)):
            details['plan_route_required'] = True
        if profile(state) and phase in ('execute','repair','review'):
            if not supported_team(profile(state)):
                raise Problem('无法识别冻结的团队执行配置')
            details.update(execution_profile=profile(state), candidate_base=state.get('active_candidate'))
            if phase in ('execute','repair'):
                slots = GoalEngine._team_role_slots(state)
                mode = team_review_mode(task,phase)
                pending = state.get('team_pending_repair')
                handoff = state.get('team_pending_handoff')
                draft = GoalEngine._draft_resume(task) if mode and not pending and not handoff else None
                if slots <= 0 and not (draft and len(draft['calls']) == len(DRAFT_ROLE_ORDER)):
                    raise Problem('内层角色调用额度用完')
                details['role_slots'] = max(0,slots)
                route = team_execution_route(task)
                if route is not None:
                    details.update(execution_route=route, route_reason=task['plan']['route_reason'])
                    if route == 'direct' and phase == 'execute':
                        details['role_slots'] = 1
                if mode:
                    decision_owner = team_proposal_owner(task, mode)
                    details.update(team_review_mode=mode,
                                   team_decision_allowed=(mode in PREFERENCE_REVIEW_MODES and decision_owner != 'team'),
                                   team_decision_required=team_decision_required(task, mode))
                    if decision_owner:
                        details['direction_owner'] = decision_owner
                if handoff and (not GoalEngine._same_team_candidate_scope(task,handoff)
                                or not mode or handoff['mode'] != mode):
                    raise Problem('不能续办旧阶段或旧验收下的审稿草稿')
                if pending:
                    if not GoalEngine._same_team_candidate_scope(task, pending):
                        raise Problem('不能续办旧计划或旧验收下的候选')
                    details['candidate_base'] = GoalEngine._pending_candidate_base(pending)
                    details['repair_continuation'] = {
                        field:pending[field] for field in ('source_assignment', 'base_sha256', 'finding', 'repair_record')
                    }
                    if 'review_only' in pending:
                        details['repair_continuation']['review_only'] = pending['review_only']
                    if handoff:
                        details['team_handoff_origin'] = {
                            'handoff':handoff['handoff'], 'base':GoalEngine._pending_candidate_base(handoff),
                        }
                elif handoff:
                    details['candidate_base'] = GoalEngine._pending_candidate_base(handoff)
                    details['team_handoff_continuation'] = handoff['handoff']
                elif draft:
                    details.update(draft_resume=draft, role_slots=len(DRAFT_ROLE_ORDER)-len(draft['calls']))
                answer = (GoalEngine._handoff_answer(task,handoff) if handoff and handoff['decision_required']
                          else team_preference_for_step(task).get('answer'))
                if handoff and handoff['decision_required'] and not answer:
                    raise Problem('继续实施前须在原单确认草稿方向')
                if answer:
                    details['preference_event'] = answer['event']
                if (handoff and not pending and handoff['decision_required']
                        and explicit_keep_answer(handoff['handoff']['instruction'],answer)):
                    # 派工冻结所需角色，已发出的旧派工继续按原回执核对。
                    details.update(team_handoff_read_only=True, role_slots=1)
            elif task['step'] > 0:
                # 协调器直接登记的复验也必须读同一候选和前序验收项。
                details['team_prior_review'] = [
                    check for step in task['plan']['steps'][:task['step']] for check in step['acceptance']
                ]
        # 五个审稿角色共用截止。隔离 Worker 上限为 6 分钟，留 30 秒收尾。
        drafting = (details.get('team_review_mode') and not details.get('team_handoff_continuation')
                    and not details.get('repair_continuation'))
        call_limit = TEAM_DRAFT_LIMIT_SECONDS if drafting else CALL_LIMIT_SECONDS
        key, ticket = uuid.uuid4().hex, task['id']
        role_profile = profile(state)
        if phase in ('plan', 'replan') and role_profile and role_profile.get('version', 1) >= 2:
            if not supported_team(role_profile):
                raise Problem('无法识别本单负责人的执行配置')
            details.update(planning_profile=role_profile, model=role_profile['roles']['lead'])
        assignment = {
            'id':key, 'ticket':ticket, 'phase':phase, 'state':'pending',
            'event_key':f'{ticket}:{key}', 'event':None, 'run':None,
            'version':task['version'], 'mandate_digest':digest(state['mandate']),
            'verification_digest':digest(state.get('verification_contract')),
            'step':task['step'], 'created_at':now(), 'model':MODEL,
            'max_seconds':min(call_limit, seconds), **details,
        }
        binding = outer_binding(task)
        if binding is not None:
            assignment['outer_binding'] = binding
            if phase == 'review':
                additional = list(dict.fromkeys([*details.get('team_prior_review', []),
                                                 *state.get('coordination_review_items', [])]))
                if additional:
                    assignment['additional_review_items'] = additional
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
                if (not assignment.get('send_attempted') and self._stale_coordination_dispatch(task,assignment)):
                    assignment['state'] = 'cancelled'
                    self._expire_coordination_dispatch(task,assignment)
                    return
                assignment['send_attempted'] = True
        return self.store.change(ticket, apply)

    def abandon_unsent(self, ticket, key):
        def apply(task):
            assignment = current(task,key)
            if (assignment and not assignment.get('send_attempted')
                    and flow(task)['stop_requested']):
                assignment['state'] = 'cancelled'
                if assignment.get('coordination_proposal'):
                    self._expire_coordination_dispatch(task,assignment)
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

    def settle(self, ticket, key, run, transport, report=None, *, recovery_report=None, recovery_checkpoint=None,
               recovery_error=None, confirmed_unstarted=False):
        if transport not in ('done','nothing','cancelled'):
            raise Problem('原轮尚未进入可核对的终态',409)
        primary_report = report
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
            team_candidate = team_candidate_assignment(assignment)
            recovered = (team_candidate and not isinstance(primary_report,dict)
                         and report_matches_assignment(recovery_report,assignment) and not recovery_report.get('error'))
            report = recovery_report if recovered else primary_report
            valid = report_matches_assignment(report,assignment)
            assignment.update(state='settled', transport=transport, settled_at=now(), report=report if valid else None)
            if recovered:
                assignment['receipt_source'] = 'recovery'
            repairs = assignment.get('inner_repairs') or []
            pending_review = valid and 'repair_review_pending' in report and not state['stop_requested']
            if repairs and repairs[-1]['status'] in ('repairing', 'rechecking') and not pending_review:
                interrupted = ('工单暂停，返修尚未通过负责人复验。' if state['stop_requested']
                               else '返修没有收到完整复验回执，仍需核对。')
                if valid and report.get('error') and not state['stop_requested']:
                    interrupted += str(report['error'])[:240]
                repairs[-1].update(status='blocked', review=interrupted)
            stale_at_launch = valid and report.get('coordination_dispatch_stale') is True
            if assignment.get('coordination_proposal') and (
                    stale_at_launch or (not state['stop_requested']
                                        and self._stale_coordination_dispatch(task,assignment))):
                # 普通派工缺失启动证据时沿用额度估算，除非 Worker 确认在放行时拦截。
                started = (self._has_charge_evidence(assignment,report if valid else None)
                           or (not team_candidate and not stale_at_launch))
                if started:
                    self._charge(task,assignment,report if valid else None)
                self._expire_coordination_dispatch(task,assignment,started=started)
                if state['stop_requested']:
                    self._after_stop(task,key)
                else:
                    task.update(status='queued',error='')
                return
            if state['stop_requested']:
                proposal_id = assignment.get('coordination_proposal')
                if confirmed_unstarted and transport == 'cancelled' and report is None and proposal_id:
                    coordinator = next((row for row in state['assignments'] if row['id'] == proposal_id), {})
                    tool_evidence = coordinator.get('coordination_context',{}).get('tool_evidence',{})
                    candidate = tool_evidence.get('read_verified_artifacts',{}).get('current_candidate',{})
                    if (recovery_checkpoint and recovery_checkpoint == state['checkpoint']
                            and recovery_checkpoint['digest'] == candidate.get('artifact_sha256')):
                        self._expire_coordination_dispatch(task,assignment)
                    else:
                        # 未执行的派工不能把外部改动登记成原轮的产物。
                        state['validation']['recovery_blocked'] = True
                    self._after_stop(task,key)
                    return
                if assignment.get('coordination_context',{}).get('coordinator_profile') == SCOPED_COORDINATOR:
                    self._record_scoped_proposal(task,assignment,report if valid else None,'stale','工单已暂停')
                self._charge(task, assignment, report if valid else None)
                if transport == 'done' and valid and assignment['phase']=='package' and not report.get('error'):
                    try:
                        self._advance(task,assignment,report)
                    except (KeyError,TypeError,ValueError,Problem) as exc:
                        decision(task,'package_unverified',assignment=key,reason=str(exc)[:180])
                if team_candidate:
                    # 已暂停的内层候选不能替代原基线，旧问题也不能生效。
                    self._after_stop(task,key)
                    return
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
            coordination_context = assignment.get('coordination_context',{})
            scoped = coordination_context.get('coordinator_profile') == SCOPED_COORDINATOR
            if scoped and coordination_context['guard'] != self._coordination_guard(task):
                self._record_scoped_proposal(task,assignment,report if valid else None,'stale',
                                             '协调期间工单或反馈已变化')
                return
            if scoped and (transport != 'done' or not valid or report.get('error')):
                self._record_scoped_proposal(task,assignment,report if valid else None,'rejected',
                                             '未收到可验证的协调提案')
            if resumable_drafting(assignment) and self._recoverable_draft_end(assignment):
                resume = self._draft_resume(task)
                if resume and resume['stalled'] <= 1:
                    decision(task,'draft_resume_queued',assignment=key,completed_roles=len(resume['calls']))
                    return
                if resume and resume['calls']:
                    pause_or_block(task,'审稿未能继续，已保留完成回执和可核对草稿。可先暂停，再从原单继续剩余角色。')
                    return
            if not valid or (transport != 'done' and not (recovered and transport == 'nothing')):
                if team_candidate:
                    pause_or_block(task,recovery_error or '团队派工已结束，缺少可核对的候选回执')
                    return
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
                if assignment.get('coordination_proposal') and report.get('checkpoint_mismatch') is True:
                    pause_or_block(task,'待执行的协调动作与已核对的工作区版本不一致')
                    return
                if team_candidate:
                    repair = assignment.get('repair_continuation') or {}
                    last_role = (assignment.get('role_calls') or [{}])[-1]
                    if 'review_only' in repair and last_role.get('error_code') == 'timeout':
                        pause_or_block(task, '负责人复验超时，执行者封存的新版仍在。可在工单操作中先暂停，再继续此单，仅续办复验。')
                    else:
                        pause_or_block(task, str(report['error'])[:300])
                    return
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
                if recovered:
                    kind = 'recovered_artifacts' if state['stage'] == 'review' else 'recovered_team_receipt'
                    decision(task,kind,assignment=key,run=run,digest=report['checkpoint']['digest'])
            except (KeyError, TypeError, ValueError, Problem) as exc:
                reason=f'执行回执或证据无法验证：{str(exc)[:160]}'
                if assignment['phase']=='package':
                    self._fail_package(task,assignment,reason)
                elif assignment['phase']=='plan' and (state.get('outer') or
                                                        (state.get('team_enabled') and not state['mandate'])):
                    state['validation'] = {'planning_feedback':reason}
                    self._recover(task,assignment,reason)
                else:
                    if assignment.get('coordination_context',{}).get('coordinator_profile') == SCOPED_COORDINATOR:
                        self._record_scoped_proposal(task,assignment,report,'rejected',reason)
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
    def _valid_elapsed_time(evidence):
        if not isinstance(evidence, dict):
            return None
        start, end = evidence.get('started_ns'), evidence.get('ended_ns')
        if type(start) is int and type(end) is int and 0 <= start <= end:
            return start, end
        return None

    @staticmethod
    def _has_charge_evidence(assignment, report):
        return (GoalEngine._valid_elapsed_time((report or {}).get('evidence')) is not None
                or any(GoalEngine._valid_elapsed_time(call) is not None
                       for call in assignment.get('role_calls', ())))

    @staticmethod
    def _charge(task, assignment, report):
        observed = GoalEngine._valid_elapsed_time((report or {}).get('evidence'))
        role_times = [GoalEngine._valid_elapsed_time(call) for call in assignment.get('role_calls', ())
                      if call.get('error_code') not in ('role_preparation_error', 'stale_before_role_launch')]
        if report and report.get('repair_review_pending') and role_times:
            # 回执写于执行者结束时，之后若已开始复验，也要结算那段用时。
            spans = [*role_times,observed] if observed else role_times
            observed = (min(a for a,_ in spans),max(b for _,b in spans)) if all(spans) else None
        elif not observed and role_times and all(role_times):
            observed = (min(start for start, _ in role_times), max(end for _, end in role_times))
        if observed:
            used = min(assignment['max_seconds'], (observed[1] - observed[0]) / 1e9)
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
        if assignment.get('execution_profile') and phase in ('execute','repair'):
            self._advance_team_candidate(task, assignment, report)
            return
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
        if not isinstance(result,dict) or not isinstance(evidence,dict) or not evidence.get('session') or evidence.get('model') != assignment['model']:
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
            from superagent.goal_team_blind import required_blind
            revision = len(state['revisions'])+1
            outer_contract = (state.get('outer') or {}).get('acceptance_contract') or {}
            blind = profile(state) and required_blind(outer_contract)
            planned = plan_result(result, revision, max_checks=len(outer_contract['criteria']) if blind else 8)
            if state.get('outer'):
                self._bind_outer_plan(task, assignment, planned)
                bind_router_route(task, assignment, planned, result)
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
            route = {key:planned[key] for key in ('execution_route','route_reason','comparison_steps') if key in planned}
            decision(task,'plan_saved',assignment=assignment['id'],revision=revision,**route)
            from copy import deepcopy
            entry = {'version':revision,'plan':deepcopy(planned),'reason':state['validation'],
                     'previous':task['version'] if phase=='replan' else None}
            if state.get('coordinator_profile') == SCOPED_COORDINATOR:
                entry['mandate_revision'] = state['outer']['mandate_revision']
                if phase == 'replan':
                    state['coordination_review_items'] = self._replan_review_items(task)
            state['revisions'].append(entry)
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
            self._verify_retained_artifacts(task, checkpoint)
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
        from superagent.goal_team_blind import required_blind
        if profile(state) and required_blind(contract) and len(planned['steps']) != 1:
            raise Problem('匿名评测的生成与封存应共同绑定到一步，之后另由本人评阅')
        if (assignment['phase'] == 'plan' and outer['mandate'].get('team_review_mode') == 'review_and_decide'
                and len(planned['steps']) < 2):
            raise Problem('已批准在第二阶段选择方案，计划须包含至少两个独立验收阶段')
        planned['acceptance_revision'] = outer['acceptance_revision']
        planned['verification_digest'] = digest(contract)

    def _advance_team_candidate(self, task, assignment, report):
        from superagent.goal_team_run import validated_result

        state = flow(task)
        calls = assignment.get('role_calls') or []
        pending_review = 'repair_review_pending' in report
        settled_calls = calls
        if 'team_handoff' in report and resumable_drafting(assignment):
            settled_calls = draft_role_prefix(assignment)
            if len(settled_calls) != len(DRAFT_ROLE_ORDER) or (
                    len(calls) + len((assignment.get('draft_resume') or {}).get('calls', ())) != len(settled_calls)):
                raise Problem('恢复的完整审稿回执缺少既定角色')
        if pending_review and len(calls) > 1 and (calls[-1]['role'],calls[-1]['writable']) == ('lead',False):
            # 复验过程中 Worker 退出时，只把已完成的写入纳入待复验凭证。
            settled_calls = calls[:-1]
        sessions = [call.get('session') for call in settled_calls]
        previous = {call.get('session') for prior in state['assignments'] if prior is not assignment
                    for call in prior.get('role_calls', [])}
        reused = {call.get('session') for call in (assignment.get('draft_resume') or {}).get('calls', ())}
        previous.difference_update(reused)
        if (assignment['step'] != task['step'] or not settled_calls
                or any(call.get('status') != 'completed' or not call.get('turn') for call in settled_calls)
                or any(not session or session in previous for session in sessions)
                or len(sessions) != len(set(sessions)) or report.get('workspace_id') != assignment['id']):
            raise Problem('内层没有与本次派工绑定的独立完成回执')
        checkpoint = report['checkpoint']
        if (not isinstance(checkpoint,dict) or not checkpoint.get('digest')
                or not isinstance(checkpoint.get('files'),dict)):
            raise Problem('团队候选缺少封存摘要')
        result = validated_result(report)
        mode = team_review_mode(task,assignment['phase'])
        decision_owner = team_proposal_owner(task, mode)
        decision_allowed = mode in PREFERENCE_REVIEW_MODES and decision_owner != 'team'
        decision_required = team_decision_required(task, mode)
        if mode != assignment.get('team_review_mode') or (mode and
                (assignment.get('team_decision_allowed') is not decision_allowed or
                 assignment.get('team_decision_required') is not decision_required)):
            raise Problem('内层派工与批准的审稿阶段不一致')
        if assignment.get('execution_route') != team_execution_route(task):
            raise Problem('内层派工与已冻结的实施路线不一致')
        if assignment.get('execution_route') == 'direct' and assignment['phase'] == 'execute':
            if [(call['role'],call['writable']) for call in calls] != [('implementer',True)]:
                raise Problem('直接实施只允许一个可写执行者')
        pending = state.get('team_pending_handoff')
        if 'team_handoff' in report:
            self._accept_team_handoff(task,assignment,report,settled_calls)
            return
        if mode:
            self._verify_handoff_continuation(task,assignment,settled_calls,checkpoint,pending,partial=pending_review)
        if pending_review:
            self._defer_team_review(task,assignment,report,settled_calls)
            return
        if 'deferred_repair' in report:
            if (report['deferred_repair'] is not True or 'preference' in report or result['action'] != 'repair'):
                raise Problem('内层返修等待回执无法核对')
            self._defer_team_repair(task, assignment, report)
            return
        if 'preference' in report:
            if mode:
                raise Problem('同一阶段的草稿决定已登记')
            question = self._team_question(task,assignment,report,calls[-1])
            self._verify_retained_artifacts(task, checkpoint)
            self._set_team_candidate(state, assignment, checkpoint)
            state['checkpoint'] = checkpoint
            self._publish_team_question(task,question)
            return
        if (result['action'] != 'done' or calls[-1].get('action','done') != 'done'
                or not checkpoint['files']):
            raise Problem('仍需修改。内层候选尚未通过复验。')
        self._verify_team_repair({
            **assignment, 'outer_acceptance':state['outer']['acceptance_contract'], 'plan':task['plan'],
        }, checkpoint)
        if mode:
            from superagent.goal_team_handoff import handoff_read_recorded, valid_handoff_verification
            bound = dict(assignment,outer_acceptance=state['outer']['acceptance_contract'],plan=task['plan'],
                         preference_answer=team_preference_for_step(task).get('answer'))
            evidence = report.get('handoff_verification')
            if (not valid_handoff_verification(bound,checkpoint,pending,evidence)
                    or not handoff_read_recorded(calls[-1].get('tools'),checkpoint,evidence,
                                                 antigravity=role_runtime(assignment['execution_profile'], 'lead') == ANTIGRAVITY,
                                                 whole_candidate=assignment.get('team_handoff_read_only') is True)):
                raise Problem('当前候选缺少与本人选择或负责人修改、保留清单对应的核验记录')
        self._verify_retained_artifacts(task, checkpoint)
        self._set_team_candidate(state, assignment, checkpoint)
        self._accept_checkpoint(task, assignment, checkpoint)

    @staticmethod
    def _team_question(task, assignment, report, last, *, staged=False):
        from superagent.goal_team_handoff import valid_directions

        result = report['result']
        preference = {'prompt':result['summary'], 'options':result['options']}
        reported = report.get('preference', preference if staged else None)
        previous = flow(task).get('pending_preference')
        if (team_preference_for_step(task) or (previous and not previous.get('answer'))
                or assignment['phase'] != 'execute'):
            raise Problem('该阶段已有本人决定')
        if (result['action'] != 'ask' or last.get('action') != 'ask'
                or not valid_directions(result['options'], proposal_choice=staged and assignment.get('direction_owner') == 'owner')
                or reported != preference):
            raise Problem('中途问题缺少可比较的选项')
        checkpoint = report['checkpoint']
        question = {
            **preference, 'id':uuid.uuid4().hex, 'revision':1, 'assignment':assignment['id'],
            'step':assignment['step'], 'plan_revision':task['version'], 'phase':assignment['phase'],
            'candidate_digest':digest({'samples':preference, 'checkpoint':checkpoint}),
            'mandate_digest':assignment['mandate_digest'],
            'stop_generation':flow(task)['stop_generation'], 'answer':None, 'created_at':now(),
        }
        if staged:
            question.update(requires_option=True,draft_sha256=checkpoint['digest'])
            if assignment.get('direction_owner') == 'owner':
                question.update(decision_kind='proposal_choice', recommended_option_id=result['options'][0]['id'])
        return question

    @staticmethod
    def _publish_team_question(task, question):
        state = flow(task)
        previous = state.get('pending_preference')
        if previous:
            state.setdefault('preference_history', []).append(previous)
        state.update(stage='awaiting_preference', pending_preference=question)
        task.update(status='awaiting_preference', error='')
        decision(task,'preference_requested',assignment=question['assignment'],question=question['id'],
                 step=question['step'])

    def _accept_team_handoff(self, task, assignment, report, calls):
        from superagent.goal_team_handoff import draft_read_recorded, validate_handoff_scopes
        from superagent.goal_team_run import validated_result

        state = flow(task)
        handoff = report['team_handoff']
        checkpoint = report['checkpoint']
        asks = report['result']['action'] == 'ask'
        identity = tuple((call['role'],call['writable']) for call in calls)
        if (not assignment.get('team_review_mode') or state.get('team_pending_handoff')
                or assignment.get('team_handoff_continuation') or assignment.get('repair_continuation')
                or 'deferred_repair' in report or 'repair_review_pending' in report
                or not isinstance(handoff,dict)
                or set(handoff) != {'comparison','draft_review','instruction','draft_sha256'}
                or handoff['instruction'] != report['result']
                or handoff['draft_sha256'] != checkpoint['digest'] or not checkpoint['files']
                or identity != DRAFT_ROLE_ORDER):
            raise Problem('缺少与封存草稿对应的分阶段审稿回执')
        baseline = calls[0].get('candidate_sha256')
        candidate_base = assignment.get('candidate_base')
        if (not baseline or baseline == checkpoint['digest'] or baseline != calls[1].get('candidate_sha256')
                or (candidate_base and baseline != candidate_base['artifact_sha256'])
                or any(call.get('candidate_sha256') != checkpoint['digest'] for call in calls[2:])):
            raise Problem('审稿角色读取的原稿或草稿版本不一致')
        for field, call in zip(('comparison','draft_review','instruction'),calls[2:]):
            result = validated_result({'result':handoff[field]})
            fields = ('action','summary','retain','verify','repair_scope','observations','handoff_scope')
            if any(result.get(key) != call.get(key) for key in fields):
                raise Problem('团队审稿说明与本次角色回执不一致')
        validate_handoff_scopes(handoff['instruction'],checkpoint)
        draft_paths = [row['path'] for row in handoff['draft_review']['observations']]
        if (handoff['comparison']['action'] != 'done' or handoff['draft_review']['action'] != 'done'
                or not draft_read_recorded(calls[3].get('tools'),checkpoint['files'],draft_paths,
                                          antigravity=role_runtime(assignment['execution_profile'], 'advisor') == ANTIGRAVITY)
                or (not asks and ('preference' in report or report['result']['action'] != 'done'
                                  or report['result']['options']))
                or (asks and not assignment.get('team_decision_allowed'))
                or (assignment.get('team_decision_required') and not asks)
                or handoff['instruction']['options'] != calls[-1].get('options', [])):
            raise Problem('审稿阶段未按授权留下续办说明或本人选项')
        question = self._team_question(task,assignment,report,calls[-1],staged=True) if asks else None
        binding = assignment['outer_binding']
        state['team_pending_handoff'] = {
            'source_assignment':assignment['id'], 'base_sha256':checkpoint['digest'],
            'files':checkpoint['files'], 'handoff':handoff, 'phase':assignment['phase'],
            'step':assignment['step'], 'version':assignment['version'],
            'goal_digest':binding['goal_digest'], 'contract_digest':binding['contract_digest'],
            'mode':assignment['team_review_mode'], 'decision_required':asks,
        }
        decision(task,'team_handoff_recorded',assignment=assignment['id'],step=assignment['step'],
                 digest=checkpoint['digest'])
        if question:
            self._publish_team_question(task,question)

    def _verify_handoff_continuation(self, task, assignment, calls, checkpoint, pending, *, partial=False):
        state = flow(task)
        deferred = state.get('team_pending_repair')
        accepted = self._handoff_answer(task,pending) if pending and pending['decision_required'] else None
        expected = deferred or pending
        route = [(call['role'],call['writable']) for call in calls]
        implementation = [('implementer',True),('lead',False)]
        if deferred and 'review_only' in deferred:
            expected_route = implementation[1:]
        elif partial:
            expected_route = implementation[:1] if deferred else implementation + implementation[:1]
        else:
            expected_route = implementation
        if (not pending or not self._same_team_candidate_scope(task,pending)
                or pending['mode'] != assignment.get('team_review_mode')
                or assignment.get('candidate_base') != self._pending_candidate_base(expected)
                or (pending['decision_required'] and (
                    not accepted or accepted['event'] != assignment.get('preference_event')))
                or calls[-1].get('candidate_sha256') != checkpoint['digest']):
            raise Problem('尚无同一阶段已审草稿或本人决定，候选不能进入独立验收')
        if deferred:
            actual = assignment.get('repair_continuation') or {}
            origin = {'handoff':pending['handoff'], 'base':self._pending_candidate_base(pending)}
            if (route != expected_route or assignment.get('team_handoff_origin') != origin
                    or any(actual.get(key) != deferred.get(key)
                    for key in ('source_assignment','base_sha256','finding','repair_record','review_only'))):
                raise Problem('待续办返修没有绑定审稿后的原候选')
        elif assignment.get('team_handoff_read_only') is True:
            if (partial or not explicit_keep_answer(pending['handoff']['instruction'],accepted)
                    or assignment.get('team_handoff_continuation') != pending['handoff']
                    or assignment.get('role_slots') != 1 or route != [('lead',False)]
                    or calls[0].get('sandbox') != 'read-only' or calls[0].get('action') != 'done'
                    or assignment.get('inner_repairs') or checkpoint['digest'] != pending['base_sha256']
                    or checkpoint['files'] != pending['files']):
                raise Problem('沿用现稿须由负责人只读核对同一封存版')
        elif (assignment.get('team_handoff_continuation') != pending['handoff']
                or (route != expected_route
                    and (partial or route != implementation*2 or not assignment.get('inner_repairs')))):
            raise Problem('团队审稿后的续办缺少独立实施和新版复查回执')

    @staticmethod
    def _verify_team_repair(assignment, checkpoint):
        repairs = assignment.get('inner_repairs') or []
        calls = assignment.get('role_calls') or []
        last = calls[-1] if calls else {}
        if not repairs:
            if assignment.get('repair_continuation') or last.get('action') == 'repair' or any(
                    call.get('action') == 'repair' and call.get('writable') is False for call in calls):
                raise Problem('缺少负责人对返修版本的复验回执')
            return
        repair = repairs[-1]
        verification = repair.get('verification')
        scope = repair.get('scope')
        continuation = assignment.get('repair_continuation')
        if (repair['status'] != 'passed' or repair.get('after_sha256') != checkpoint.get('digest')
                or not isinstance(scope,dict) or repair.get('before_sha256') != scope.get('before_sha256')
                or (continuation and (
                    repair.get('source_assignment') != continuation.get('source_assignment')
                    or (repair.get('after_sha256') if 'review_only' in continuation else repair.get('before_sha256'))
                        != continuation.get('base_sha256')
                    or scope != (continuation.get('repair_record') or {}).get('scope')
                ))
                or last.get('candidate_sha256') != repair.get('after_sha256')
                or last.get('role') != 'lead' or last.get('writable') is not False
                or last.get('status') != 'completed' or last.get('action') != 'done'
                or last.get('verification') != verification
                or not valid_verification(assignment, checkpoint, scope, verification)):
            raise Problem('仍需修改。当前版本缺少负责人通过的复验回执。')

    def record_inner_repair(self, assignment, record):
        def apply(task):
            stored = current(task, assignment['id'])
            if (not stored or task['paused'] or flow(task)['stop_requested']
                    or stored['run'] != assignment['run'] or stored.get('outer_binding') != outer_binding(task)):
                raise Problem('内层返修派工已停止或变更', 409)
            stored['inner_repairs'] = [dict(record)]
        self.store.change(assignment['ticket'], apply)

    @staticmethod
    def _set_team_candidate(state, assignment, checkpoint):
        state['active_candidate'] = dict(assignment=assignment['id'], artifact_sha256=checkpoint['digest'],
                                         files=checkpoint['files'])
        if state['outer']['mandate'].get('workflow_profile') == ROUTER_TEAM:
            # 只根据当前候选和其封存祖先的已完成角色回执登记来源，不推断资料采纳。
            base = assignment.get('candidate_base')
            history = {row['id']: row for row in state['assignments']}
            lineage = [assignment]
            seen = {assignment['id']}
            while base and base['assignment'] in history and base['assignment'] not in seen:
                parent = history[base['assignment']]
                lineage.append(parent)
                seen.add(parent['id'])
                base = parent.get('candidate_base')
            refs = {
                (ref['event_id'], ref['sha256']): ref for row in reversed(lineage)
                for role in [*(row.get('draft_resume') or {}).get('calls', ()), *row.get('role_calls', ())]
                if role['status'] == 'completed'
                for ref in role.get('material_refs', [])
            }
            state['active_candidate']['material_refs'] = list(refs.values())

    def reserve_team_role(self, assignment, sequence, role, writable, effort):
        expected = assignment['execution_profile']
        model = expected['roles'].get(role)
        if not model or (writable and role not in ('lead', 'implementer')):
            raise Problem('该角色未获权执行所请求操作')
        record = dict(sequence=sequence, id=f"{assignment['id']}:{sequence}", role=role,
                      requested_model=model, effort=effort, writable=writable, status='reserved',
                      runtime=role_runtime(expected, role))
        def apply(task):
            stored = current(task, assignment['id'])
            state = flow(task)
            if (not stored or task['paused'] or state['stop_requested'] or profile(state) != expected
                    or stored['run'] != assignment['run'] or stored.get('outer_binding') != outer_binding(task)):
                raise Problem('派工已停止或变更，未预留模型调用', 409)
            if stored.get('team_handoff_read_only') is True and (role,writable) != ('lead',False):
                raise Problem('沿用现稿只允许负责人只读核对', 409)
            calls = stored.setdefault('role_calls', [])
            total = sum(len(row.get('role_calls', [])) for row in state['assignments'])
            if (len(calls) + 1 != sequence or len(calls) >= stored['role_slots']
                    or total >= role_limit(state)):
                raise Problem('角色调用额度不足或动作已登记', 409)
            calls.append(record)
        self.store.change(assignment['ticket'], apply)
        return record

    def finish_team_role(self, assignment, sequence, output, *, status):
        fields = ('session','turn','model','effort','sandbox','summary','action','retain','verify',
                  'permission_profile','permission_digest','approval_policy','cli_version',
                  'started_ns','ended_ns','usage','usage_source','tools','rerouted_model','error_code',
                  'app_server_error','app_server_retry','auth_recovery','prompt_sha256','candidate_sha256',
                  'repair_scope','handoff_scope','verification','observations','options','material_refs')
        def apply(task):
            stored = current(task, assignment['id'])
            if not stored or stored['run'] != assignment['run']:
                raise Problem('原角色调用已结束，未改写记录', 409)
            record = stored['role_calls'][sequence - 1]
            if record['status'] != 'reserved':
                raise Problem('原角色调用已经结算', 409)
            if status == 'completed' and output.get('model') != record['requested_model']:
                raise Problem('实际路由超出许可模型')
            record.update({key: output[key] for key in fields if key in output}, status=status)
        self.store.change(assignment['ticket'], apply)

    def _advance_outer_review(self, task, assignment, report):
        from superagent.outer_verification import validate_results
        state = flow(task)
        contract = state['outer']['acceptance_contract']
        checkpoint = report['checkpoint']
        baseline = state['checkpoint']
        if (report['verifier_profile'] != 'outer-v2' or not isinstance(checkpoint,dict) or not baseline
                or checkpoint != baseline or not checkpoint.get('digest')):
            raise Problem('独立验收和实施检查点必须绑定同一交付版本')
        self._verify_retained_artifacts(task,checkpoint)
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
            if profile(state) and self._retry_first_team_review(task, assignment, rows, contract):
                return
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
        state.pop('coordination_retention',None)
        state.pop('coordination_review_items',None)
        candidate = state.get('active_candidate') or {}
        if 'material_refs' in candidate and candidate['artifact_sha256'] == checkpoint['digest']:
            delivery = dict(delivery, material_refs=candidate['material_refs'])
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
            if state.get('coordinator_profile') == SCOPED_COORDINATOR:
                delivery = dict(delivery, step=assignment['step'], plan_revision=assignment['version'],
                                evidence_refs=[row['evidence_ref'] for row in rows])
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

    @staticmethod
    def _retry_first_team_review(task, assignment, rows, contract):
        state = flow(task)
        criteria = {row['criterion_id']:row for row in contract['criteria']}
        failed = [row for row in rows if row['status'] != 'PASS' and criteria[row['id']]['required']
                  and criteria[row['id']]['verification_type'] != 'HUMAN_REVIEW']
        local = set(task['plan']['steps'][task['step']]['acceptance'])
        if not failed or any(row['id'] not in local for row in failed):
            return False
        kinds = {row['status'] for row in failed}
        phase = {'FAIL':'repair', 'ERROR':'review'}.get(next(iter(kinds))) if len(kinds) == 1 else None
        if not phase:
            return False
        key = f"{assignment['version']}:{assignment['step']}:{phase}"
        attempts = state.setdefault('team_review_retries', {})
        if attempts.get(key):
            return False
        attempts[key] = True
        if phase == 'repair':
            failed_paths = {criteria[row['id']]['test_spec'].get('path',
                            criteria[row['id']]['test_spec'].get('script')) for row in failed}
            passed_paths = {criteria[row['id']]['test_spec'].get('path',
                            criteria[row['id']]['test_spec'].get('script')) for row in rows if row['passed']}
            state['coordination_retention'] = [
                {'path':path,'sha256':value} for path,value in state['checkpoint']['files'].items()
                if path in passed_paths - failed_paths
            ]
        state['stage'] = phase
        task['phase'] = phase
        decision(task, 'local_review_retry', assignment=assignment['id'], next_phase=phase,
                 failed=[row['id'] for row in failed], retained=state.get('coordination_retention', []))
        return True

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
        notes = (self._authorized_materials(task)
                 if outer['mandate'].get('workflow_profile') == ROUTER_TEAM
                 else [] if is_blind_goal(task) else self.store.notes(task['id']))
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
        if state.get('coordinator_profile') == SCOPED_COORDINATOR:
            self._scope_coordination_context(task,context,rows,failed)
        context['input_snapshot_revision'] = digest([context,outer_binding(task)])
        return context

    @staticmethod
    def _minimum_review_seconds(task, *, additional=None):
        from superagent.outer_verification import selected_criteria
        state = flow(task)
        contract = state['outer']['acceptance_contract']
        if additional is None:
            additional = list(state.get('coordination_review_items', ()))
            if profile(state):
                additional.extend(key for step in task['plan']['steps'][:task['step']]
                                  for key in step['acceptance'])
        criteria = selected_criteria(contract,task['plan'],task['step'],additional=additional)
        return 10 + 9 * sum(row['verifier_id']=='python-json-cli' for row in criteria)

    @staticmethod
    def _replan_review_items(task):
        from superagent.outer_verification import selected_criteria
        plan, step = task['plan'], task['step']
        prior = {key for stage in plan['steps'][:step] for key in stage['acceptance']}
        criteria = selected_criteria(flow(task)['outer']['acceptance_contract'],plan,step,additional=prior)
        return [row['criterion_id'] for row in criteria]

    def _scope_coordination_context(self, task, context, rows, failed):
        from superagent.outer_intake import _model_text
        from superagent.outer_verification import ROUTER_TEAM, SCOPED_COORDINATOR_TOOLS
        state = flow(task)
        plan = task['plan']
        checkpoint = state.get('checkpoint') or {}
        failed_criteria = {row['criterion_id'] for row in failed}
        affected_steps = [i for i, step in enumerate(plan['steps'][:task['step']+1])
                          if i == task['step'] or failed_criteria.intersection(step['acceptance'])]
        verification_items = [row['criterion_id'] for row in rows]
        criteria = state['outer']['acceptance_contract']['criteria']
        criterion_paths = {row['criterion_id']:row['test_spec'].get('path',row['test_spec'].get('script'))
                           for row in criteria}
        failing_paths = {criterion_paths[row['criterion_id']] for row in rows if row['status']!='PASS'}
        passed_paths = {criterion_paths[row['criterion_id']] for row in rows if row['status']=='PASS'}
        verified_stage_versions = self._verified_stage_versions(state,criterion_paths)
        files = checkpoint.get('files',{})
        unchanged = {path for version in verified_stage_versions for path,value in version['files'].items()
                     if files.get(path) == value}
        retained = (passed_paths | unchanged) - failing_paths
        artifact_versions = {path:files[path] for path in checkpoint.get('artifacts',[]) if path in retained}
        context['retained_outputs'] = list(artifact_versions)
        if not failed or not checkpoint.get('digest'):
            context['available_actions'] = ['REQUEST_APPROVAL','BLOCKED']
        minimum_review_seconds = self._minimum_review_seconds(task)
        replan_steps = list(range(task['step']+1))
        replan_items = self._replan_review_items(task)
        replan_review_seconds = self._minimum_review_seconds(task,additional=replan_items)
        followup_rounds = {'CONTINUE':1, 'REPAIR':2, 'REPLAN':3}
        remaining = context['remaining_budget_seconds']
        remaining_calls = None if task.get('max_calls') is None else max(0,task['max_calls'] - task['calls'])
        if profile(state) and self._team_role_slots(state) <= 0:
            context['available_actions'] = [action for action in context['available_actions']
                                            if action not in ('REPAIR','REPLAN')]
        for action, rounds in followup_rounds.items():
            review_seconds = replan_review_seconds if action == 'REPLAN' else minimum_review_seconds
            if (action in context['available_actions']
                    and (remaining < review_seconds + 10 * (rounds - 1)
                         or (remaining_calls is not None and remaining_calls < rounds))):
                context['available_actions'].remove(action)
        affected_assignments = {delivery['assignment'] for delivery in state.get('stage_deliveries',[])
                                if delivery.get('step') in affected_steps}
        budget = {'seconds':remaining, 'calls':remaining_calls,
                  'mandate_revision':context['mandate_revision'],
                  'minimum_review_seconds':minimum_review_seconds,
                  'replan_review_seconds':replan_review_seconds}
        context.update(
            coordinator_profile=SCOPED_COORDINATOR, affected_steps=affected_steps,
            verification_items=verification_items, artifact_versions=artifact_versions,
            replan_affected_steps=replan_steps, replan_verification_items=replan_items,
            guard=self._coordination_guard(task),
        )
        revisions = [entry for entry in state['revisions'] if entry.get('mandate_revision') == context['mandate_revision']]
        # 验收描述在提示中。保留完整计划的哈希，并限制 MCP 中叙述性文字的长度。
        current_steps = []
        for index, step in enumerate(plan['steps']):
            limit = 1000 if index == task['step'] else 400
            current_steps.append({
                **{key:step[key] for key in ('title','acceptance','state')},
                'objective':_model_text(step['objective'],limit),
                'objective_truncated':len(step['objective']) > limit,
            })
        current_plan = dict(plan, summary=_model_text(plan['summary'],800), steps=current_steps,
                            summary_truncated=len(plan['summary']) > 800)
        previous_plans = [
            {'version':entry['version'], 'previous':entry.get('previous'), 'plan_sha256':digest(entry['plan']),
             'plan':{'summary':_model_text(entry['plan']['summary'],400),
                     'steps':[{key:step[key] for key in ('title','acceptance','state')}
                              for step in entry['plan']['steps']]}}
            for entry in revisions[-3:-1]
        ]
        context['tool_evidence'] = dict(zip(SCOPED_COORDINATOR_TOOLS, (
            {'failure':context['failure'], 'checks':context['verification_checks'],
             'affected_steps':affected_steps, 'verification_items':verification_items,
             'replan_affected_steps':replan_steps, 'replan_verification_items':replan_items,
             'prior_stage_assignments_to_reverify':sorted(affected_assignments),
             'recent_failed_reviews':self._recent_failed_reviews(task)},
            {'current_candidate':{'artifact_sha256':checkpoint.get('digest')},
             'retainable_file_versions':artifact_versions, 'verified_stages':verified_stage_versions},
            {'current':current_plan, 'current_plan_sha256':digest(plan), 'previous':previous_plans},
            budget,
        )))

    @staticmethod
    def _verified_stage_versions(state, criterion_paths):
        assignments = {row['id']:row for row in state['assignments']}
        versions = []
        for delivery in state.get('stage_deliveries',[]):
            source = assignments[delivery['assignment']]['report']
            passed = [row for row in source['validation']['checks'] if row['status']=='PASS']
            paths = {criterion_paths[row['criterion_id']] for row in passed}
            versions.append({
                **{key:delivery[key] for key in ('assignment','artifact_sha256','step','plan_revision')},
                'files':{path:value for path,value in delivery['files'].items() if path in paths},
                'evidence_refs':[row['evidence_ref'] for row in passed],
            })
        return versions

    @staticmethod
    def _recent_failed_reviews(task):
        state = flow(task)
        latest = state['coordination']['assignment']
        evaluated = {row['assignment'] for row in state['decisions']
                     if row['kind']=='evaluated' and row.get('passed') is False}
        recent = []
        for assignment in reversed(state['assignments']):
            if assignment['id'] not in evaluated or assignment['id']==latest:
                continue
            report = assignment.get('report') or {}
            failed = [row for row in (report.get('validation') or {}).get('checks',[])
                      if isinstance(row,dict) and row.get('status') in ('FAIL','ERROR','NOT_RUN','INCONCLUSIVE')
                      and all(key in row for key in ('criterion_id','status','evidence_ref','artifact_sha256'))]
            if failed:
                recent.append({'assignment':assignment['id'],'run':assignment['run'],
                               'step':assignment['step'],'plan_revision':assignment['version'],
                               'checks':[{key:row[key] for key in ('criterion_id','status','evidence_ref','artifact_sha256')}
                                         for row in failed]})
            if len(recent)==3:
                break
        return recent

    def _outer_coordination_inputs(self, ticket_id):
        with self.store.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outer_tickets'").fetchone():
                return {}
            row = db.execute('SELECT body FROM outer_tickets WHERE id=?',(ticket_id,)).fetchone()
            if not row:
                return {}
            ticket = json.loads(row[0])
            pending = [value[0] for value in db.execute(
                "SELECT id FROM outer_events WHERE ticket=? AND state IN ('PENDING','RUNNING') "
                "AND json_extract(body,'$.type') IN ('message','selection') ORDER BY id",(ticket_id,))]
        return {
            'ticket':{key:ticket[key] for key in ('revision','goal_revision','acceptance_revision',
                                                 'mandate_revision','status')},
            'pending':pending,
        }

    def _coordination_guard(self, task):
        state = flow(task)
        return digest({
            'authorization':outer_binding(task), 'mandate':state['mandate'],
            'plan_revision':task['version'], 'plan':task['plan'], 'step':task['step'],
            'checkpoint':state['checkpoint'], 'coordination':state['coordination'],
            'validation':state['validation'], 'stage_deliveries':state.get('stage_deliveries',[]),
            'user_inputs':self._coordination_source_revision(task),
        })

    def _coordination_source_revision(self, task):
        return digest([
            self.store.notes(task['id'],for_board=True),
            self._outer_coordination_inputs(task['id']),
            flow(task)['stop_generation'],
        ])

    def _stale_coordination_dispatch(self, task, assignment):
        original = assignment.get('coordination_source_revision')
        return original is not None and original != self._coordination_source_revision(task)

    @staticmethod
    def _expire_coordination_dispatch(task, assignment, *, started=False):
        state = flow(task)
        proposal_id = assignment['coordination_proposal']
        record = state['coordination_proposals'][proposal_id]
        record['dispatch_disposition'] = 'stale_after_launch' if started else 'stale_before_launch'
        action = record['proposal']['action']
        if action == 'REPLAN':
            state['replans'] -= 1
            state['repairs'] = record['previous_repairs']
        elif action == 'REPAIR':
            state['repairs'] -= 1
        elif action == 'CONTINUE':
            state['rechecks'] -= 1
        state.pop('coordination_retention',None)
        state['validation'].pop('coordination',None)
        state['stage'] = 'coordinate'
        task['phase'] = 'review'
        decision(task,'coordination_dispatch_stale',assignment=assignment['id'],proposal=proposal_id)

    @staticmethod
    def _record_scoped_proposal(task, assignment, report, disposition, reason=None, dispatch=None):
        state = flow(task)
        ledger = state.setdefault('coordination_proposals',{})
        evidence = (report or {}).get('evidence')
        context = assignment['coordination_context']
        ledger[assignment['id']] = {
            'assignment':assignment['id'], 'run':assignment['run'], 'at':now(),
            'input_snapshot_revision':context['input_snapshot_revision'], 'guard':context['guard'],
            'binding':assignment['outer_binding'], 'disposition':disposition,
            'proposal':(report or {}).get('result'), 'invocation':evidence, 'reason':reason,
            'dispatch': dispatch,
        }
        if disposition == 'stale':
            decision(task,'coordination_stale',assignment=assignment['id'],run=assignment['run'],reason=reason)

    @staticmethod
    def _verify_retained_artifacts(task, checkpoint):
        retained = flow(task).get('coordination_retention') or []
        if retained and any((checkpoint.get('files') or {}).get(item['path']) != item['sha256']
                            for item in retained):
            raise Problem('实施改动了协调决定要求保留的文件版本')

    @staticmethod
    def _validate_scoped_invocation(invocation, context):
        tools = invocation.get('tools')
        session = invocation.get('session_id')
        expected = context['tool_evidence']
        if (not isinstance(session,str) or not re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', session)
                or not isinstance(tools,list) or len(tools)!=len(expected)
                or any(not isinstance(entry,dict) or entry.get('name') not in expected
                       or entry.get('status')!='completed'
                       or entry.get('evidence_sha256')!=digest(expected[entry['name']]) for entry in tools)
                or {entry['name'] for entry in tools}!=set(expected)):
            raise Problem('协调证据未绑定本轮 Codex 会话和真实工具读取')

    def _apply_outer_coordination(self, task, assignment, report):
        from superagent.outer_verification import validate_coordination
        state = flow(task)
        context = dict(assignment['coordination_context'])
        scoped = context.get('coordinator_profile') == SCOPED_COORDINATOR
        proposal = validate_coordination(report['result'],context)
        invocation = report['evidence']
        if (invocation.get('snapshot_revision') != context['input_snapshot_revision']
                or invocation.get('invocation_id') != assignment['id']):
            raise Problem('协调建议未绑定有效的模型调用')
        if scoped:
            self._validate_scoped_invocation(invocation,context)
            if any(row['id'] != assignment['id']
                   and ((row.get('report') or {}).get('evidence') or {}).get('session_id') == invocation['session_id']
                   for row in state['assignments']):
                raise Problem('协调提案必须使用本轮独立的 Codex 会话')
        current_context = self._outer_coordination_context(task)
        action = proposal['action']
        if (action not in current_context['available_actions'] or
                (action in ('CONTINUE','REPAIR','REPLAN') and task.get('max_calls') is not None
                 and task['calls'] >= task['max_calls'])):
            if scoped:
                self._record_scoped_proposal(task,assignment,report,'rejected','剩余预算或动作已变化')
            pause_or_block(task,'协调完成时剩余预算不足或建议已过期',budget=True)
            return
        previous_repairs = state['repairs']
        if not scoped:
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
        if scoped:
            dispatch = None
            if action in ('CONTINUE','REPAIR','REPLAN'):
                state['coordination_retention'] = proposal['retained_artifacts']
                seconds = current_context['remaining_budget_seconds']
                budget = current_context['tool_evidence']['read_remaining_budget']
                if action == 'REPLAN':
                    # 重规划后还需要一轮实际实施和当前及已交付阶段的复验。
                    seconds -= budget['replan_review_seconds'] + 10
                elif action == 'REPAIR':
                    seconds -= budget['minimum_review_seconds']
                self._enqueue(task,state['stage'],seconds,coordination_proposal=assignment['id'],
                              coordination_source_revision=self._coordination_source_revision(task))
                dispatch = state['assignments'][-1]['id']
            self._record_scoped_proposal(task,assignment,report,'accepted',dispatch=dispatch)
            if action=='REPLAN':
                state['coordination_proposals'][assignment['id']]['previous_repairs'] = previous_repairs
            decision(task,'coordinated',assignment=assignment['id'],run=assignment['run'],proposal=proposal,
                     invocation=invocation,correlation_id=state['outer']['correlation_id'],dispatch=dispatch)

    @staticmethod
    def _accept_checkpoint(task, assignment, checkpoint):
        state = flow(task)
        if assignment.get('repair_continuation'):
            state.pop('team_pending_repair', None)
        if assignment.get('repair_continuation') or assignment.get('team_handoff_continuation'):
            state.pop('team_pending_handoff', None)
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

    def _authorized_materials(self, task):
        if is_blind_goal(task):
            return []
        from superagent.outer_intake import _model_text

        with self.store.connect() as db:
            rows = db.execute("""
                SELECT e.id, e.source, n.body
                FROM outer_events e
                JOIN outer_tickets t ON t.id=e.ticket AND t.actor=e.actor AND t.tenant=e.tenant
                JOIN inbox_notes n ON n.task=t.id AND n.message=
                    CASE WHEN e.source='web' THEN 'web-' || e.id ELSE e.id END
                WHERE t.id=? AND e.state='DONE'
                    AND json_extract(e.result, '$.action')='ATTACH_TO_TICKET'
                    AND json_type(e.body, '$.allow_model_input')='true'
                    AND (json_type(t.body, '$.allow_antigravity_input') IS NOT 'true'
                         OR json_type(e.body, '$.allow_antigravity_input')='true')
                ORDER BY n.rowid
                """, (task['id'],)).fetchall()
        return [
            {'event_id':row['id'], 'source':row['source'], 'body':_model_text(row['body'])}
            for row in rows
        ]

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
        context = {'notes':[] if is_blind_goal(task) else self.store.notes(ticket),
                   'verification':state['validation'], 'team_enabled':state.get('team_enabled',False)}
        if (state.get('outer') or {}).get('mandate', {}).get('workflow_profile') == ROUTER_TEAM:
            context['notes'] = self._authorized_materials(task)
            context['ticket_team'] = {'id':ticket, 'workflow_profile':ROUTER_TEAM}
            if prefix := (assignment.get('draft_resume') or {}).get('calls'):
                original = prefix[0].get('material_refs', [])
                current_materials = {
                    (item['event_id'],item['source'],hashlib.sha256(item['body'].encode()).hexdigest()):item
                    for item in context['notes']
                }
                keys = [(ref['event_id'],ref['source'],ref['sha256']) for ref in original]
                if (any(call.get('material_refs',[]) != original for call in prefix)
                        or any(key not in current_materials for key in keys)):
                    raise Problem('草稿续办的原授权资料无法按原指纹核对',409)
                context['notes'] = [current_materials[key] for key in keys]
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
            if team_candidate_assignment(assignment):
                context['original_requirements'] = self._team_original_requirements(task)
                context['team_review_mode'] = state['outer']['mandate'].get('team_review_mode')
            if assignment['phase'] in ('plan','replan'):
                context['team_review_mode'] = state['outer']['mandate'].get('team_review_mode')
            if assignment['phase']=='coordinate':
                context['coordinator'] = assignment['coordination_context']
            baseline_digest = (state.get('checkpoint') or {}).get('digest')
            if assignment.get('team_handoff_continuation') or assignment.get('repair_continuation'):
                baseline_digest = assignment['candidate_base']['artifact_sha256']
            context['execution_contract'] = {
                **assignment['outer_binding'], 'assignment':assignment['id'], 'run':run,
                'plan_revision':assignment['version'], 'work_package':None, 'stage':assignment['step'],
                'workspace_id':ticket, 'baseline':baseline_digest,
                'prior_stage_evidence':state.get('stage_deliveries',[]),
                'max_seconds':assignment['max_seconds'],
                'scope':state['mandate']['scope'],
                'forbidden_operations':state['mandate']['forbidden_operations'],
            }
        if assignment['phase']=='package':
            context.update(package=assignment['package'], retry=assignment.get('retry_context'),
                           dependency_outputs=[item['files'] for item in assignment['inputs']])
        preference = team_preference_for_step(task).get('answer')
        if (team_candidate_assignment(assignment) and (assignment.get('team_review_mode') or preference)
                and assignment.get('preference_event') != (preference.get('event') if preference else None)):
            raise Problem('本步本人决定已更新，派工须回原单核对',409)
        return dict(assignment, preference_answer=preference, goal=task['goal'], acceptance=task['acceptance'], mandate=state['mandate'],
                    plan=task['plan'], context=context, checkpoint=state['checkpoint'],
                    verification_contract=state.get('verification_contract'),workspace_base=state.get('workspace_base'),
                    outer_acceptance=(state.get('outer') or {}).get('acceptance_contract'))

    @contextmanager
    def launch_gate(self,ticket,key,run):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = json.loads(db.execute('SELECT body FROM tasks WHERE id=?',(ticket,)).fetchone()[0])
            assignment = current(task,key)
            if (assignment and assignment['run'] == run and self._stale_coordination_dispatch(task,assignment)):
                raise StaleCoordination('协调后新收到工单反馈，原派工未启动',409)
            if (not assignment or assignment['run'] != run or task['paused']
                    or flow(task)['stop_requested'] or assignment['version'] != task['version']
                    or assignment['mandate_digest'] != digest(flow(task)['mandate'])
                    or assignment.get('verification_digest',digest(None)) != digest(flow(task).get('verification_contract'))
                    or assignment.get('outer_binding') != outer_binding(task)):
                raise Problem('派工已停止或变更，未放行模型',409)
            selected = assignment.get('execution_profile') or assignment.get('planning_profile')
            if uses_antigravity(selected):
                source = db.execute('SELECT body FROM outer_tickets WHERE id=?', (ticket,)).fetchone()
                scope = json.loads(source[0]) if source else {}
                if (scope.get('allow_antigravity_input') is not True
                        or scope.get('approval_terms', {}).get('execution_profile') != selected):
                    raise Problem('本单未确认 Antigravity 资料发送范围', 409)
            context = assignment.get('coordination_context',{})
            if (context.get('coordinator_profile') == SCOPED_COORDINATOR
                    and context['guard'] != self._coordination_guard(task)):
                raise Problem('协调依据已变化，未放行原版本工具读取',409)
            yield
