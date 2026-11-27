"""由安装包登记角色档案。工单冻结已批准的版本，正文不能指定模型或可执行程序。"""
from copy import deepcopy


LEGACY_TEAM = {
    'version': 1, 'runtime': 'codex-app-server',
    'roles': {'lead': 'gpt-6.2-sol', 'advisor': 'gpt-6.2-sol', 'design_advisor': 'gpt-6.2-astra',
              'implementer': 'gpt-6.2-sol', 'reviewer': 'gpt-6.2-sol'},
    'max_roles_per_assignment': 5, 'max_roles_per_ticket': 30,
}
ROUTER_TEAM = {
    **LEGACY_TEAM, 'version': 2,
    'roles': {**LEGACY_TEAM['roles'], 'advisor': 'gpt-6.2-astra'},
    'efforts': {'lead': 'medium', 'advisor': 'high', 'design_advisor': 'high',
                'implementer': 'medium', 'reviewer': 'high'},
    'comparison_lead_effort': 'high',
}
PROFILES = {1: LEGACY_TEAM, 2: ROUTER_TEAM}
CURRENT_VERSION = 2
SELECTED_VERSION = 3
MIXED_VERSION = 4
ANTIGRAVITY = 'antigravity-cli'
ROLE_LABELS = {'lead': '负责人', 'advisor': '只读顾问', 'design_advisor': '复杂设计顾问',
               'implementer': '执行者', 'reviewer': '复核者'}
EFFORT_LABELS = {'medium': '中等', 'high': '高'}
V3_AGENTS = {
    'codex-app-server': {
        'label': 'Codex', 'provider': 'openai', 'roles': tuple(ROLE_LABELS),
        'models': {'gpt-6.2-sol': 'GPT‑6.2 Sol', 'gpt-6.2-astra': 'GPT‑6.2 Astra'},
        'efforts': tuple(EFFORT_LABELS),
    },
}
V4_AGENTS = {
    **V3_AGENTS,
    ANTIGRAVITY: {
        'label': 'Google Antigravity', 'provider': 'google', 'roles': tuple(ROLE_LABELS),
        'models': {'gemini-4-hydrogen': 'Gemini 4 Hydrogen'}, 'efforts': tuple(EFFORT_LABELS),
    },
}
# 冻结的版本仍可对账，即使以后从新工单目录撤下某个选项。
CODING_AGENTS = deepcopy(V4_AGENTS)


def selectable_agents():
    from .goal_antigravity import executable

    return {runtime: agent for runtime, agent in CODING_AGENTS.items()
            if runtime in ROLE_ADAPTERS and (runtime != ANTIGRAVITY or executable() is not None)}


def team_options():
    return {
        'roles': ROLE_LABELS, 'efforts': EFFORT_LABELS, 'defaults': new_team_profile(),
        'agents': selectable_agents(),
    }


def new_team_profile(selections=None):
    if selections is None:
        return deepcopy(PROFILES[CURRENT_VERSION])
    profile = _selected_profile(selections, selectable_agents())
    if not supported(profile):
        raise ValueError('当前工单版本尚未登记这组小队选项')
    return profile


def _selected_profile(selections, agents):
    profile = deepcopy(ROUTER_TEAM)
    if type(selections) is not dict or not selections or selections.keys() - ROLE_LABELS.keys():
        raise ValueError('小队配置须选择有效的角色')
    profile.update(version=SELECTED_VERSION, role_runtimes={}, selections=deepcopy(selections))
    for role, choice in selections.items():
        if (type(choice) is not dict or set(choice) != {'runtime', 'model', 'effort'}
                or not all(isinstance(value, str) for value in choice.values())):
            raise ValueError('小队配置须包含 Coding Agent、模型和思考强度')
        runtime, model, effort = (choice[key] for key in ('runtime', 'model', 'effort'))
        agent = agents.get(runtime)
        if (agent is None
                or role not in agent['roles'] or model not in agent['models']
                or (effort and effort not in agent['efforts'])
                or (not effort and (runtime != ROUTER_TEAM['runtime']
                    or role_effort(ROUTER_TEAM, role, '') not in agent['efforts']))):
            raise ValueError('所选角色、Coding Agent、模型或思考强度当前不可用')
        profile['role_runtimes'][role] = runtime
        profile['roles'][role] = model
        if runtime == ANTIGRAVITY:
            profile['version'] = MIXED_VERSION
        if effort:
            profile['efforts'][role] = effort
            if role == 'lead':
                profile['comparison_lead_effort'] = effort
    return profile


def supported(profile):
    if not isinstance(profile, dict) or type(profile.get('version')) is not int:
        return False
    version = profile['version']
    if version in PROFILES:
        return profile == PROFILES[version]
    registry = {SELECTED_VERSION: V3_AGENTS, MIXED_VERSION: V4_AGENTS}.get(version)
    if registry is None or 'selections' not in profile:
        return False
    try:
        return profile == _selected_profile(profile['selections'], registry)
    except ValueError:
        return False


def role_effort(profile, role, previous, route=None):
    if role == 'lead' and route == 'compare' and 'comparison_lead_effort' in profile:
        return profile['comparison_lead_effort']
    return profile.get('efforts', {}).get(role, previous)


def role_runtime(profile, role):
    return profile.get('role_runtimes', {}).get(role, profile['runtime'])


def uses_antigravity(profile):
    return ANTIGRAVITY in (profile or {}).get('role_runtimes', {}).values()


def _codex_turn(**kwargs):
    from .goal_app_server import invoke
    return invoke(**kwargs)


def _antigravity_turn(**kwargs):
    from .goal_antigravity import invoke
    return invoke(**kwargs)


# 新 coding agent 在此登记同一调用契约的适配器；保持只读/写入、时限和实物回执的语义。
ROLE_ADAPTERS = {'codex-app-server': _codex_turn, ANTIGRAVITY: _antigravity_turn}


def invoke_role(profile, role, **kwargs):
    if not supported(profile) or role not in profile['roles']:
        raise ValueError('无法识别本单冻结的角色配置')
    sandbox = kwargs.get('sandbox')
    if sandbox not in ('read-only', 'workspace-write') or (sandbox == 'workspace-write' and role not in ('lead', 'implementer')):
        raise ValueError('该角色未获所请求的工作区权限')
    adapter = ROLE_ADAPTERS.get(role_runtime(profile, role))
    if adapter is None:
        raise ValueError('本机尚未安装本单指定的角色执行器')
    return adapter(model=profile['roles'][role], **kwargs)


def codex_models():
    return set(V3_AGENTS['codex-app-server']['models']) | {
        model for profile in PROFILES.values() for role, model in profile['roles'].items()
        if role_runtime(profile, role) == 'codex-app-server'
    }
