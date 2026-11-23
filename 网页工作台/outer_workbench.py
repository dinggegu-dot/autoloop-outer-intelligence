"""显式试点开关和普通新建工单入口共用的接单规则。"""
import re

from workbench import Problem


def create_task(workbench, body):
    outer_enabled = getattr(workbench, 'outer_enabled', False)
    enabled = getattr(workbench, 'outer_workbench_entry', False)
    if outer_enabled and not enabled:
        raise Problem('请在智能接单入口登记需求', 409)
    if body.get('workflow') != 'outer':
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
    fields = {}
    for key, limit in (('name', 80), ('goal', 6000), ('acceptance', 4000)):
        value = body.get(key, '')
        if not isinstance(value, str) or len(value.strip()) > limit:
            raise Problem('工单名称、目标或完成标准的格式无效')
        fields[key] = value.strip()
    request = body.get('request_id')
    if not fields['goal'] or not isinstance(request, str) or not re.fullmatch('[a-f0-9-]{36}', request):
        raise Problem('请填写工单目标，并使用有效的请求编号')
    if body.get('ticket_id') is not None:
        raise Problem('工作台试点入口仅用于新建工单', 409)
    result = workbench.outer().submit(source='web', source_event_id=request, actor_id='owner',
                                      text=fields['goal'], workbench_request=fields)
    return dict(result, workflow='outer')
