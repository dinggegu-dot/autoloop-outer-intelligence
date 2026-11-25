"""全 GPT 内层的授权配置和向后兼容的原单读取。"""

from workbench import is_blind_goal
from superagent.team_profiles import LEGACY_TEAM as PROFILE

MIN_REPAIR_ASSIGNMENT_SECONDS = 75
TEAM_ROLE_MIN_SECONDS = 8
TEAM_RUN_RESERVE_SECONDS = 3
# 派工使用整秒，并为装载封存文件的开销留出余量。
MIN_TEAM_ASSIGNMENT_SECONDS = TEAM_ROLE_MIN_SECONDS + TEAM_RUN_RESERVE_SECONDS + 1
ADAPTIVE_REVIEW = 'adaptive_review'
TEAM_REVIEW_MODES = ('review', 'review_and_decide', ADAPTIVE_REVIEW)
PREFERENCE_REVIEW_MODES = ('review_and_decide', ADAPTIVE_REVIEW)
EXECUTION_PHASES = ('plan','replan','execute','repair','review','coordinate','package','assemble')

def profile(state):
    return ((state.get('outer') or {}).get('mandate') or {}).get('execution_profile')


def role_limit(state):
    mandate = ((state.get('outer') or {}).get('mandate') or {})
    return mandate.get('team_role_limit', PROFILE['max_roles_per_ticket'])


def public_preference(state, status, *, brief=False):
    if status != 'awaiting_preference' or not profile(state):
        return None
    record = state.get('pending_preference')
    if not record or record.get('answer') is not None:
        return None
    keys = ['id', 'revision', 'prompt', 'created_at', 'step', 'requires_option']
    if not brief:
        keys += ['candidate_digest', 'options', 'draft_sha256']
    public = {key: record[key] for key in keys if key in record}
    handoff = state.get('team_pending_handoff') or {}
    if (not brief and record.get('requires_option') and record.get('draft_sha256')
            and record.get('assignment') == handoff.get('source_assignment')
            and record['draft_sha256'] == handoff.get('base_sha256')):
        instruction = handoff.get('handoff', {}).get('instruction', {})
        variants = (instruction.get('handoff_scope') or {}).get('variants', [])
        modes = {variant['option_id']:variant['mode'] for variant in variants if variant['mode'] in ('keep','edit')}
        if modes:
            public['options'] = [dict(option, mode=modes[option['id']]) if option['id'] in modes else option
                                 for option in record['options']]
    return public


def observation_stale(state):
    return any(row.get('state') in ('pending','dispatched') and row.get('transport') == 'unknown'
               for row in state.get('assignments', []))


def public_progress(task):
    state = task.get('goal_execution') or {}
    status = task.get('status')
    active = [row for row in state.get('assignments', []) if row.get('state') in ('pending','dispatched')]
    phases = {row['phase'] for row in active if row.get('phase')}
    phase = next(iter(phases)) if len(phases) == 1 else state.get('stage') or task.get('phase')
    if phase not in EXECUTION_PHASES:
        phase = None
    repair_status = None
    if (not is_blind_goal(task) and status in ('queued','executing') and phase in ('execute','repair')
            and not observation_stale(state)):
        assignment = next((row for row in reversed(active)
                           if row.get('state') == 'dispatched' and row.get('inner_repairs')), {})
        pending = (state.get('team_pending_repair') or {}).get('repair_record') or {}
        observed = (assignment.get('inner_repairs') or [pending])[-1].get('status')
        if observed in ('repairing','rechecking','waiting','waiting_review'):
            repair_status = observed
    return dict(execution_phase=phase, inner_repair_status=repair_status)


def public_coordination_decision(task):
    state = task.get('goal_execution') or {}
    recent = {entry.get('kind'): entry for entry in state.get('decisions', [])[-2:]}
    if task.get('status') != 'blocked' or recent.keys() != {'coordinated','blocked'}:
        return None
    proposal = recent['coordinated'].get('proposal') or {}
    if proposal.get('action') not in ('REQUEST_APPROVAL','BLOCKED'):
        return None
    if recent['blocked'].get('reason') != proposal.get('reason'):
        return None
    fields = ('action',) if is_blind_goal(task) else ('action','reason','plan_change','remaining_risk')
    return {key: proposal[key] for key in fields if key in proposal}


def public_role_activity(assignment, *, blind):
    calls = assignment.get('role_calls', [])
    purposes = {}
    if (not blind and assignment.get('phase') == 'execute' and assignment.get('team_review_mode') in TEAM_REVIEW_MODES
            and not assignment.get('team_handoff_continuation') and not assignment.get('repair_continuation')):
        for sequence, role, writable, purpose in (
                (1,'lead',False,'lead_plan'), (2,'advisor',False,'advisor_plan'),
                (3,'lead',True,'plan_comparison')):
            if len(calls) < sequence:
                break
            call = calls[sequence - 1]
            if ((call.get('sequence'), call.get('role'), call.get('writable')) != (sequence, role, writable)
                    or call.get('status') != 'completed' or call.get('action') != 'done'
                    or not call.get('summary')):
                break
            purposes[sequence] = dict(purpose=purpose, step=assignment.get('step'))
    return [dict(call, assignment=assignment['id'], **purposes.get(call.get('sequence'), {})) for call in calls]


def project(task, *, blind_review=None):
    state = task.get('goal_execution') or {}
    blind = is_blind_goal(task)
    candidate = (state.get('artifact_reviews') or [{}])[-1]
    review = blind_review or {}
    current_locked = (review.get('status') == 'locked' and review.get('current') is True
                      and task.get('status') == 'awaiting_artifact_review' and candidate.get('state') == 'pending'
                      and review['candidate'] == candidate['assignment']
                      and review['artifact_sha256'] == candidate['artifact_sha256'])
    if blind and task.get('status') != 'completed' and not current_locked:
        # 锁票仅打开同一待签收候选的历史，退回后的下一版继续保护。
        state.pop('team_pending_repair', None)
        for assignment in state.get('assignments', []):
            assignment.pop('inner_repairs', None)
            assignment.pop('repair_continuation', None)
            report = assignment.get('report') or {}
            report.pop('repair_review_pending', None)
    if not profile(state):
        return task
    task['pending_preference'] = public_preference(state, task['status'])
    task['active_candidate'] = state.get('active_candidate')
    task['observation_stale'] = observation_stale(state)
    task['team_activity'] = [call for assignment in state['assignments']
                             for call in public_role_activity(assignment, blind=blind)]
    return task
