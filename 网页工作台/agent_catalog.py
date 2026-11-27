"""可信角色目录。任务只能选择已登记模型，不能提交命令或凭据。"""
import json
from pathlib import Path


CODEX_MODELS = {'gpt-6.2-astra': 'GPT‑6.2 Astra', 'gpt-6.2-sol': 'GPT‑6.2 Sol', 'gpt-6.1-terra': 'GPT‑6.1 Terra'}
MODELS = {**CODEX_MODELS, 'gpt-6-astra': 'Astra', 'deepseek-dsh': 'DeepSeek · DSH', 'gpt-6.1-sol': 'Sol'}
DEFAULT_ROLES = {'plan': 'gpt-6.2-astra', 'execute': 'gpt-6.2-sol', 'review': 'gpt-6.2-sol'}
LEGACY_ROLES = {'plan':'gpt-6-astra','execute':'gpt-6.1-terra','review':'gpt-6-astra'}
ROLE_LABELS = {'plan':'方案设计','execute':'实施执行','review':'独立验收'}


def validate_roles(value):
    if not isinstance(value,dict) or set(value)!=set(DEFAULT_ROLES) or any(v not in MODELS for v in value.values()):
        raise ValueError('请为规划、执行和验收选择已登记的模型')
    if value['plan']=='deepseek-dsh':
        raise ValueError('DeepSeek / DSH 可用于实施和独立审核；规划使用 Codex 模型')
    return dict(value)


def role_model(task,role):
    return task.get('role_models',{}).get(role,task.get('model','gpt-6-astra'))


def preserve_implicit_roles(body, store):
    """正式工作台还使用旧 Store。无角色参数的重试沿用原工单登记。"""
    request_id = body.get('request_id')
    if 'role_models' in body or not isinstance(request_id, str):
        return body
    with store.connect() as db:
        previous = db.execute('SELECT body FROM tasks WHERE request_id=?', (request_id,)).fetchone()
    if previous:
        return {**body, 'role_models': json.loads(previous[0]).get('initial_role_models', LEGACY_ROLES)}
    return body


def catalogue(root=None):
    from engine_adapters import connection_status
    dsh=connection_status(root or Path(__file__).resolve().parent/'.runtime')
    return {'models':[{'id':key,'name':name} for key,name in MODELS.items()],
            'roles':ROLE_LABELS,'defaults':DEFAULT_ROLES,'adapter':'role-engine-router','dsh':dsh,
            'execution':'串行 · 单工作区写入者','connections':[
                {'name':'本机 Codex','state':'configured','description':'按角色选择模型，实际调用结果见任务记录'},
                {'name':'DeepSeek Harness','state':dsh['state'],'description':dsh['description']},
                {'name':'MCP 工具','state':'planned','description':'待登记工具、权限和验收，不继承个人 MCP 配置'},
                {'name':'数据库','state':'planned','description':'待配置只读/写入边界和凭据引用'},
                {'name':'并行 Agent','state':'planned','description':'待接依赖调度与隔离工作区'}]}
