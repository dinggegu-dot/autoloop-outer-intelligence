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


def new_team_profile():
    return deepcopy(PROFILES[CURRENT_VERSION])


def supported(profile):
    if not isinstance(profile, dict) or type(profile.get('version')) is not int:
        return False
    return profile == PROFILES.get(profile['version'])


def role_effort(profile, role, previous, route=None):
    if role == 'lead' and route == 'compare' and 'comparison_lead_effort' in profile:
        return profile['comparison_lead_effort']
    return profile.get('efforts', {}).get(role, previous)


def role_runtime(profile, role):
    return profile.get('role_runtimes', {}).get(role, profile['runtime'])


def _codex_turn(**kwargs):
    from .goal_app_server import invoke
    return invoke(**kwargs)


# 新 coding agent 在此登记同一调用契约的适配器；保持只读/写入、时限和实物回执的语义。
ROLE_ADAPTERS = {'codex-app-server': _codex_turn}


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
    return {model for profile in PROFILES.values() for role, model in profile['roles'].items()
            if role_runtime(profile, role) == 'codex-app-server'}
