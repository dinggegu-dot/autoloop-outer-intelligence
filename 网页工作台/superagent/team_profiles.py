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
CATALOGUE_VERSION = 5
CODEX = 'codex-app-server'
ANTIGRAVITY = 'antigravity-cli'
ROLE_LABELS = {'lead': '负责人', 'advisor': '只读顾问', 'design_advisor': '复杂设计顾问',
               'implementer': '执行者', 'reviewer': '复核者'}
EFFORT_LABELS = {'low': '低', 'medium': '中等', 'high': '高', 'xhigh': '超高', 'max': '最高', 'auto': '自动'}
CODEX_EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
V3_AGENTS = {
    CODEX: {
        'label': 'Codex', 'provider': 'openai', 'roles': tuple(ROLE_LABELS),
        'models': {'gpt-6.2-sol': 'GPT‑6.2 Sol', 'gpt-6.2-astra': 'GPT‑6.2 Astra'},
        'efforts': ('medium', 'high'),
    },
}
V4_AGENTS = {
    **V3_AGENTS,
    ANTIGRAVITY: {
        'label': 'Google Antigravity', 'provider': 'google', 'roles': tuple(ROLE_LABELS),
        'models': {'gemini-4-hydrogen': 'Gemini 4 Hydrogen'}, 'efforts': ('medium', 'high'),
    },
}
GOOGLE_MODELS = {
    'gemini-4-neon': ('Gemini 4 Neon', ('low', 'medium', 'high', 'xhigh')),
    'gemini-4-carbon': ('Gemini 4 Carbon', ('low', 'medium', 'high')),
    'gemini-4-hydrogen': ('Gemini 4 Hydrogen', ('low', 'medium', 'high', 'xhigh')),
    'claude-sonnet-5-5': ('Claude Sonnet 5.5（Thinking）', ('auto',)),
    'claude-opus-5-5': ('Claude Opus 5.5（Thinking）', ('auto',)),
}
LEGACY_GOOGLE_MODELS = {
    'gemini-4-argon': ('Gemini 4 Argon', ('low', 'medium', 'high')),
    'gemini-3.8-flash': ('Gemini 3.8 Flash', ('low', 'medium', 'high')),
    'gpt-oss-120b': ('GPT-OSS 120B', ('medium',)),
    'claude-sonnet-4-6': ('Claude Sonnet 4.6（Thinking）', ('auto',)),
    'claude-opus-4-6': ('Claude Opus 4.6（Thinking）', ('auto',)),
}
GOOGLE_MODEL_REGISTRY = {**GOOGLE_MODELS, **LEGACY_GOOGLE_MODELS}
V5_AGENTS = {
    CODEX: {**V3_AGENTS[CODEX], 'models': {
        'gpt-6.2-astra': 'GPT‑6.2 Astra', 'gpt-6.2-sol': 'GPT‑6.2 Sol', 'gpt-6.1-terra': 'GPT‑6.1 Terra',
    }, 'efforts': CODEX_EFFORTS},
    ANTIGRAVITY: {**V4_AGENTS[ANTIGRAVITY],
                  'models': {model: spec[0] for model, spec in GOOGLE_MODEL_REGISTRY.items()},
                  'model_efforts': {model: spec[1] for model, spec in GOOGLE_MODEL_REGISTRY.items()}},
}
# 账号能选择的目录与历史档案的合同分开。
CODING_AGENTS = deepcopy(V5_AGENTS)


def selectable_agents(*, include_antigravity=True):
    from .goal_antigravity import available_models, executable

    agents = {CODEX: CODING_AGENTS[CODEX]} if CODEX in CODING_AGENTS else {}
    if not include_antigravity or ANTIGRAVITY not in CODING_AGENTS:
        return agents
    found = available_models() if executable() is not None else {}
    google = CODING_AGENTS[ANTIGRAVITY]
    offered = {model: tuple(effort for effort in google['model_efforts'][model] if effort in found.get(model, {}))
               for model in google['models']}
    agents[ANTIGRAVITY] = {**google,
        'models': {model: name for model, name in google['models'].items() if offered[model]},
        'model_efforts': {model: efforts for model, efforts in offered.items() if efforts},
        'unavailable_models': {model: google['models'][model] for model in GOOGLE_MODELS if not offered[model]},
    }
    return agents


def team_options():
    return {
        'roles': ROLE_LABELS, 'efforts': EFFORT_LABELS, 'defaults': new_team_profile(),
        'agents': selectable_agents(),
    }


def new_team_profile(selections=None):
    if selections is None:
        return deepcopy(PROFILES[CURRENT_VERSION])
    mixed = isinstance(selections, dict) and any(isinstance(choice, dict) and choice.get('runtime') == ANTIGRAVITY
                                                for choice in selections.values())
    profile = _selected_profile(selections, selectable_agents(include_antigravity=mixed), version=CATALOGUE_VERSION)
    if not supported(profile):
        raise ValueError('当前工单版本尚未登记这组小队选项')
    return profile


def _selected_profile(selections, agents, *, version=SELECTED_VERSION):
    profile = deepcopy(ROUTER_TEAM)
    if type(selections) is not dict or not selections or selections.keys() - ROLE_LABELS.keys():
        raise ValueError('小队配置须选择有效的角色')
    profile.update(version=version, role_runtimes={}, selections=deepcopy(selections))
    for role, choice in selections.items():
        if (type(choice) is not dict or set(choice) != {'runtime', 'model', 'effort'}
                or not all(isinstance(value, str) for value in choice.values())):
            raise ValueError('小队配置须包含 Coding Agent、模型和思考强度')
        runtime, model, effort = (choice[key] for key in ('runtime', 'model', 'effort'))
        agent = agents.get(runtime)
        permitted = agent.get('model_efforts', {}).get(model, agent['efforts']) if agent else ()
        if (agent is None
                or role not in agent['roles'] or model not in agent['models']
                or (effort and effort not in permitted)
                or (not effort and (runtime != ROUTER_TEAM['runtime']
                    or role_effort(ROUTER_TEAM, role, '') not in permitted))):
            raise ValueError('所选 Agent、模型或推理强度当前不可用，请刷新小队设置')
        profile['role_runtimes'][role] = runtime
        profile['roles'][role] = model
        if runtime == ANTIGRAVITY and version == SELECTED_VERSION:
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
    registry = {SELECTED_VERSION: V3_AGENTS, MIXED_VERSION: V4_AGENTS, CATALOGUE_VERSION: V5_AGENTS}.get(version)
    if registry is None or 'selections' not in profile:
        return False
    try:
        return profile == _selected_profile(profile['selections'], registry,
                                            version=CATALOGUE_VERSION if version == CATALOGUE_VERSION else SELECTED_VERSION)
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
    return set(V5_AGENTS[CODEX]['models']) | {
        model for profile in PROFILES.values() for role, model in profile['roles'].items()
        if role_runtime(profile, role) == 'codex-app-server'
    }
