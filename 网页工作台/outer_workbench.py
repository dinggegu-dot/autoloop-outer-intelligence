"""显式试点开关和普通新建工单入口共用的接单规则。"""
import json
import re

from workbench import Problem
from superagent.goal_team_records import ADAPTIVE_REVIEW, TEAM_REVIEW_MODES
from superagent.team_profiles import new_team_profile, supported, uses_antigravity


def _registered_profile(store, source, request_id, choices):
    with store.connect() as db:
        existing = db.execute(
            """SELECT json_extract(body,'$.workbench_request.execution_profile')
               FROM outer_events WHERE source=? AND tenant='default' AND source_event_id=?""",
            (source, request_id),
        ).fetchone()
    if existing is None or existing[0] is None:
        return new_team_profile(choices)
    profile = json.loads(existing[0])
    if not supported(profile) or profile.get('selections') != choices:
        raise ValueError('同一个请求编号不能更换小队配置')
    return profile


def create_task(workbench, body, *, source='web', legacy_fixture=False):
    outer_enabled = getattr(workbench, 'outer_enabled', False)
    enabled = getattr(workbench, 'outer_workbench_entry', False)
    if outer_enabled and not enabled:
        raise Problem('请在智能接单入口登记需求', 409)
    if body.get('workflow') != 'outer':
        if 'team_roles' in body:
            raise Problem('请在团队工单中配置小队', 409)
        if 'team_review_mode' in body:
            raise Problem('请在目标模式下选择审稿方式', 409)
        if enabled:
            with workbench.store.connect() as db:
                existing = db.execute(
                    """SELECT 1 FROM outer_events WHERE source='web' AND tenant='default'
                       AND source_event_id=? AND json_type(body,'$.workbench_request')='object'""",
                    (str(body.get('request_id', '')),)).fetchone()
            if existing:
                raise Problem('同一个请求编号不能切换执行路线', 409)
        return workbench.create(body)
    if not enabled:
        raise Problem('当前工作台未启用智能工单试点', 409)
    team_enabled = getattr(workbench, 'outer_goal_team', False)
    route_team = team_enabled and not legacy_fixture
    if route_team and source == 'web' and body.get('allow_model_input') is not True:
        raise Problem('请先在工作台确认允许发送本单资料给 Codex', 409)
    fields = {}
    for key, limit in (('name', 80), ('goal', 6000), ('acceptance', 4000)):
        value = body.get(key, '')
        if not isinstance(value, str) or len(value.strip()) > limit:
            raise Problem('工单名称、目标或完成标准的格式无效')
        fields[key] = value.strip()
    if 'team_review_mode' in body:
        mode = body['team_review_mode']
        if mode not in TEAM_REVIEW_MODES:
            raise Problem('请选择有效的方案审稿方式')
        if source != 'web' or not team_enabled or (mode == ADAPTIVE_REVIEW and not route_team):
            raise Problem('当前入口未启用团队审稿', 409)
        fields['team_review_mode'] = mode
    request = body.get('request_id')
    if not fields['goal'] or not isinstance(request, str) or not re.fullmatch('[a-f0-9-]{36}', request):
        raise Problem('请填写工单目标，并使用有效的请求编号')
    if body.get('ticket_id') is not None:
        raise Problem('工作台试点入口仅用于新建工单', 409)
    if 'team_roles' in body:
        if source != 'web' or not route_team:
            raise Problem('当前入口未启用小队配置', 409)
        try:
            choices = body['team_roles']
            if choices is None:
                raise ValueError('请选择有效的小队配置')
            fields['execution_profile'] = _registered_profile(workbench.store, source, request, choices)
        except ValueError as error:
            raise Problem(str(error)) from None
        if uses_antigravity(fields['execution_profile']):
            if body.get('allow_antigravity_input') is not True:
                raise Problem('请确认将本单已授权内容发送给 Google Antigravity', 409)
            fields['allow_antigravity_input'] = True
    if source == 'web' and body.get('allow_model_input') is True:
        # 只保留建单授权证据，模型快照仍读取名称、目标和完成标准。
        fields['allow_model_input'] = True
    result = workbench.outer().submit(source=source, source_event_id=request, actor_id='owner',
                                      text=fields['goal'], workbench_request=fields,
                                      goal_team=team_enabled, route_team=route_team)
    return dict(result, workflow='outer')
