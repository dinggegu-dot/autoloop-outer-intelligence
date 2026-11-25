"""全 GPT 内层的授权配置和向后兼容的原单读取。"""

MIN_REPAIR_ASSIGNMENT_SECONDS = 75
TEAM_ROLE_MIN_SECONDS = 8
TEAM_RUN_RESERVE_SECONDS = 3
# 派工使用整秒，并为装载封存文件的开销留出余量。
MIN_TEAM_ASSIGNMENT_SECONDS = TEAM_ROLE_MIN_SECONDS + TEAM_RUN_RESERVE_SECONDS + 1
TEAM_REVIEW_MODES = ('review', 'review_and_decide')

PROFILE = {
    'version': 1,
    'runtime': 'codex-app-server',
    'roles': {'lead': 'gpt-6.2-sol', 'advisor': 'gpt-6.2-sol', 'design_advisor': 'gpt-6.2-astra',
              'implementer': 'gpt-6.2-sol', 'reviewer': 'gpt-6.2-sol'},
    'max_roles_per_assignment': 5,
    'max_roles_per_ticket': 30,
}


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
    keys = ['id', 'revision', 'prompt', 'created_at', 'step']
    if not brief:
        keys += ['candidate_digest', 'options', 'draft_sha256', 'requires_option']
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


def project(task):
    state = task.get('goal_execution') or {}
    if not profile(state):
        return task
    task['pending_preference'] = public_preference(state, task['status'])
    task['active_candidate'] = state.get('active_candidate')
    task['observation_stale'] = observation_stale(state)
    task['team_activity'] = [
        dict(call, assignment=assignment['id'])
        for assignment in state['assignments']
        for call in assignment.get('role_calls', [])
    ]
    return task
