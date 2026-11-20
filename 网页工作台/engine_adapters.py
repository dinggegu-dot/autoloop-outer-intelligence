"""执行器契约：可信本机连接、固定路由、DSH NDJSON 与结构化回执。

不从任务正文接受程序、环境变量或凭据；DSH 承担 execute / review。
"""
import hashlib
import json
import os
import re
from pathlib import Path

DSH_ID = 'deepseek-dsh'
CONFIG_NAME = 'dsh-connection.json'
PROVIDERS = {'deepseek-account', 'deepseek-official'}
ERROR_HINTS = {
    'MISSING_CREDENTIAL':'DSH 缺少认证，请在 DSH 设置完成所选 provider 的登录或凭据配置',
    'UNKNOWN_MODEL':'所选 DeepSeek 模型不可用，请核对 provider 与实际模型 ID',
    'UNKNOWN_PROVIDER':'DSH 没有加载所选 provider，请核对版本与配置',
    'CLIENT_UPGRADE_REQUIRED':'所选模型要求更新 DSH，请先核对兼容版本',
    'UNSUPPORTED_REASONING_EFFORT':'所选模型不支持当前推理设置，请核对 DSH profile',
}
DISABLED_ROWS = ('tool-subagent', 'tool-subagent-fork', 'tool-subagent-control',
                 'tool-agent-team', 'tool-workflow', 'workflow-ptc', 'goal-round-driver',
                 'tool-goal', 'command-goal', 'session-title-llm', 'session-log-deepseek',
                 'plugin-package-inventory-deepseek', 'tool-web', 'tool-plugin-manager')


class ConnectionProblem(ValueError):
    pass


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def error_code(event):
    # 不转发供应商原始报错（可能含认证或请求内容），只提取已知分类。
    text=json.dumps(event,ensure_ascii=False)
    return next((code for code in ERROR_HINTS if code in text),None)


def validate_connection(value):
    if not isinstance(value, dict) or set(value) != {'version', 'program', 'home', 'provider', 'model'} or value.get('version') != 1:
        raise ConnectionProblem('DSH 连接格式无效，请使用 setup_dsh.py 登记；连接文件不允许凭据或自定义命令')
    if value['provider'] not in PROVIDERS:
        raise ConnectionProblem('DSH 仅允许已登记的 DeepSeek 账号或官方 API 路由')
    if not isinstance(value['model'], str) or not re.fullmatch(r'deepseek-[A-Za-z0-9._-]{1,100}', value['model']):
        raise ConnectionProblem('请填写 DSH 当前账号实际可用的 DeepSeek 模型 ID')
    for key in ('program', 'home'):
        if not isinstance(value[key], str) or not Path(value[key]).is_absolute():
            raise ConnectionProblem('DSH 程序与 home 必须是本机绝对路径')
    if not Path(value['program']).is_file() or not os.access(value['program'], os.X_OK):
        raise ConnectionProblem('DSH 程序不存在或不可执行')
    if not Path(value['home']).is_dir():
        raise ConnectionProblem('DSH home 不存在，请先在 DSH 完成配置或登录')
    return dict(value)


def load_connection(root):
    path = Path(root) / CONFIG_NAME
    if not path.is_file() or path.is_symlink():
        raise ConnectionProblem('DeepSeek 尚未接入。请让 DSH 按 DSH接入指令.md 运行 setup_dsh.py；接入后在网页继续任务')
    try:
        return validate_connection(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError) as error:
        raise ConnectionProblem('DSH 连接文件无法读取，请重新登记') from error


def connection_status(root):
    try:
        config = load_connection(root)
        verified = False
        try:
            proof = json.loads((Path(root) / 'dsh-verification.json').read_text())
            verified = proof.get('config_hash') == signature(config) and proof.get('passed') is True
        except (OSError, ValueError):
            pass
        return {'state': 'verified' if verified else 'configured', 'model': config['model'],
                'provider': config['provider'], 'engine': 'dsh',
                'description': '本机实施探针已通过；各任务的设计、实施与审核结果见任务记录' if verified else '连接已登记，尚未通过真实模型探针'}
    except (ConnectionProblem, TypeError) as error:
        return {'state': 'unconfigured', 'engine': 'dsh', 'description': str(error)}


def require_verified(root, config):
    try:
        proof=json.loads((Path(root)/'dsh-verification.json').read_text())
        if proof.get('passed') is True and proof.get('config_hash')==signature(config):return
    except (OSError,ValueError):pass
    raise ConnectionProblem('DSH 连接尚未通过真实实施探针；请按接入指令运行 setup_dsh.py probe，通过后再继续')


def resolve_routes(task, root, allow_unverified=False):
    from agent_catalog import role_model
    routes = {}
    model_file=Path(root)/'codex-models.json'
    models={}
    if model_file.exists() or model_file.is_symlink():
        from agent_catalog import MODELS
        if not model_file.is_file() or model_file.is_symlink():
            raise ConnectionProblem('本机 Codex 模型配置须为普通文件')
        try:models=json.loads(model_file.read_text())
        except (ValueError,OSError):raise ConnectionProblem('本机 Codex 模型配置无法读取') from None
        if not isinstance(models,dict) or any(key not in MODELS or key==DSH_ID or not isinstance(value,str)
                or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}',value) for key,value in models.items()):
            raise ConnectionProblem('Codex 模型配置仅允许已登记别名和单个模型 ID，不接受路径、命令或凭据')
    for role in ('plan', 'execute', 'review'):
        model = role_model(task, role)
        if model == DSH_ID:
            if role not in ('execute','review'):
                raise ConnectionProblem('DSH 当前承担实施和独立审核角色')
            config = load_connection(root)
            if not allow_unverified:require_verified(root,config)
            routes[role] = {'engine': 'dsh', 'provider': config['provider'], 'model': config['model'], 'connection_hash': signature(config)}
        else:
            routes[role] = {'engine': 'codex', 'model': models.get(model,model)}
    pinned = task.get('role_routes')
    if pinned and pinned != routes:
        raise ConnectionProblem('执行连接与本版固定分工不一致；请恢复原连接，或在网页「补充或修改要求」建立新版方案')
    return routes


def dsh_overlay(config):
    # JSON 是 YAML 子集，避免模型 ID / 路径被解释成配置语法。
    rows = [{'id': row, 'disabled': True} for row in DISABLED_ROWS]
    rows += [{'id': 'credentials', 'disabled': False}]
    if config['provider'] == 'deepseek-account':
        rows += [{'id': 'authorization', 'disabled': False}, {'id': 'deepseek-account', 'disabled': False}]
    rows += [{'id': 'agent-default-model', 'config': {'provider': config['provider'], 'model': config['model']}}]
    return rows


def dsh_command(config, overlay):
    return [config['program'], '--profile', 'headless', '--patch', str(overlay), '--json',
            '--permission-mode', 'workspace-write', '--approval-policy', 'never', '-']


def validate_result(value, schema, path='result'):
    """两种引擎共用严格 JSON 契约，不依赖供应商是否原生支持 schema。"""
    kind = schema.get('type')
    if kind == 'object':
        if not isinstance(value, dict) or set(value) != set(schema['properties']):
            raise ConnectionProblem(path + ' 对象字段不符合任务回执契约')
        for key, spec in schema['properties'].items():
            validate_result(value[key], spec, path + '.' + key)
    elif kind == 'array':
        if not isinstance(value, list) or len(value) > 200:
            raise ConnectionProblem(path + ' 必须为有限数组')
        for item in value:
            validate_result(item, schema['items'], path + '[]')
    elif kind == 'string':
        if not isinstance(value, str) or len(value) > 20000:
            raise ConnectionProblem(path + ' 必须为有效文本')
    if 'enum' in schema and value not in schema['enum']:
        raise ConnectionProblem(path + ' 状态无效')
    return value


class DSHEvents:
    def __init__(self, emit):
        self.emit = emit
        self.session = None
        self.final = None
        self.failed = False
        self.error_code = None
        self.tools = {}
        self.usage_steps = set()

    def feed(self, line):
        try:
            event = json.loads(line)
        except ValueError:
            return None
        if not isinstance(event, dict):
            return None
        kind = event.get('type')
        if kind == 'session':
            self.session = event.get('sessionId')
            self.emit('engine_session', 'DeepSeek 开始独立会话', {'engine': 'dsh', 'session': self.session})
        elif kind == 'tool_call':
            self.tools[event.get('callId')] = str(event.get('tool', 'tool'))[:100]
        elif kind == 'tool_result':
            name = self.tools.get(event.get('callId'), 'tool')
            ok = event.get('status') == 'completed'
            self.emit('tool_result', 'DeepSeek 工具已结束：' + name,
                      {'engine': 'dsh', 'tool': name, 'success': ok, 'truncated': bool(event.get('truncated'))})
            # 已安装 DSH bash 在非零退出时抛 ToolError；completed 是真实零退出。
            # 不将 read/write 等工具成功伪装成测试命令通过，也不记录工具正文。
            if name == 'bash':
                self.emit('command', 'DeepSeek 检查命令已结束',
                          {'engine':'dsh','exit_code':0 if ok else None,'status':'completed' if ok else 'failed'})
            if ok:
                return 'dsh-tool:' + signature([name, event.get('result')])
        elif kind == 'status' and event.get('phase') == 'step_end':
            key = (event.get('turn'), event.get('step'))
            usage = event.get('usage')
            if key not in self.usage_steps:
                self.usage_steps.add(key)
                if isinstance(usage,dict):self.emit('usage', 'DeepSeek 上报步骤用量', {'engine': 'dsh', 'usage': usage})
                else:self.emit('usage_missing','DeepSeek 本步骤未上报用量',{'engine':'dsh','usage':{}})
        elif kind == 'final':
            if self.final is not None:
                self.failed = True
            self.final = event
            if (event.get('reason') or {}).get('kind')!='completed':self.error_code=error_code(event)
        elif kind == 'error':
            self.failed = True
            self.error_code = error_code(event)
        # thinking / text 不进入网页记录，不将心跳计为实际进展。
        return None

    def result(self, code, schema):
        if code != 0 or self.failed or not isinstance(self.final, dict) or (self.final.get('reason') or {}).get('kind') != 'completed':
            raise ConnectionProblem(ERROR_HINTS.get(self.error_code,'DSH 未正常完成本次调用；请核对 DSH 登录、模型与诊断，已有成果保留'))
        try:
            result = json.loads(self.final.get('text', ''))
        except (ValueError, TypeError):
            raise ConnectionProblem('DeepSeek 未返回有效 JSON 回执，任务没有标记完成') from None
        return validate_result(result, schema)


def safe_dsh_log(line):
    """记录协议元信息；推理、正文和工具原始内容不落工作台事件日志。"""
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if not isinstance(event, dict) or event.get('type') in ('thinking', 'text'):
        return None
    allowed = ('type', 'sessionId', 'phase', 'turn', 'step', 'usage', 'callId', 'tool', 'status', 'truncated')
    result={k: event[k] for k in allowed if k in event}
    if event.get('type') in ('error','final') and error_code(event):result['error_code']=error_code(event)
    return json.dumps(result, ensure_ascii=False)
