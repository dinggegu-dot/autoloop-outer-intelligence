"""Run one approved team role over the local Codex App Server's stdio protocol."""

import contextlib
import hashlib
import json
import math
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import UUID, uuid4

if __package__:
    from .team_profiles import codex_models
    from .codex_worker import MODEL, _decode, _matches
    from .goal_codex import (
        GoalCodexError, ProcessTree, _exit_categories, _GATE, _publish,
        _SAMPLE_SECONDS, _SIGNALS, _stop, _write_record, trusted_cli_environment,
    )
else:
    from team_profiles import codex_models
    from codex_worker import MODEL, _decode, _matches
    from goal_codex import (
        GoalCodexError, ProcessTree, _exit_categories, _GATE, _publish,
        _SAMPLE_SECONDS, _SIGNALS, _stop, _write_record, trusted_cli_environment,
    )


_MODELS = codex_models()
_EFFORTS = {'medium', 'high'}
_SANDBOXES = {
    'read-only': {'type': 'readOnly', 'networkAccess': False},
    'workspace-write': {
        'type': 'workspaceWrite', 'networkAccess': False, 'writableRoots': [],
        'excludeTmpdirEnvVar': True, 'excludeSlashTmp': True,
    },
}
_ENDPOINTS = {
    'model_provider': 'openai',
    'chatgpt_base_url': 'https://chatgpt.com/backend-api/',
    'forced_login_method': 'chatgpt',
}
_DISABLED = (
    'apps', 'plugins', 'hooks', 'browser_use', 'browser_use_external',
    'in_app_browser', 'computer_use', 'multi_agent', 'multi_agent_v2',
    'skill_mcp_dependency_install', 'image_generation', 'memories',
    'request_permissions_tool', 'default_mode_request_user_input', 'remote_plugin',
    'network_proxy', 'shell_snapshot', 'shell_snapshot_v2', 'unified_exec_zsh_fork',
    'skill_search',
)
_TOOL_TYPES = {'commandExecution': 'shell', 'fileChange': 'apply_patch', 'imageView': 'view_image'}
_OTHER_TOOLS = {
    'mcpToolCall', 'dynamicToolCall', 'collabAgentToolCall', 'subAgentActivity',
    'webSearch', 'imageGeneration', 'hookPrompt', 'functionCallOutput',
}
_TOOL_STATES = {'inProgress', 'completed', 'failed', 'declined'}
_READ_CHALLENGE = re.compile(r'^workbench-read:([a-f0-9]{64})[ \t\r]*$',re.MULTILINE)
_TOKEN_FIELDS = {
    'totalTokens': 'total_tokens', 'inputTokens': 'input_tokens',
    'cachedInputTokens': 'cached_input_tokens', 'cacheWriteInputTokens': 'cache_write_input_tokens',
    'outputTokens': 'output_tokens', 'reasoningOutputTokens': 'reasoning_output_tokens',
}
_MAX_REQUEST = 256_000
_MAX_OUTPUT = 16_000_000
_MAX_LINE = 2_000_000
_INTERRUPT_GRACE = 0.5
_TURN_START = 4
_TURN_INTERRUPT = 5
_ERROR_KINDS = {
    'unauthorized': 'account',
    'inferenceHookDenied': 'policy',
    'policyCapacityUnavailable': 'capacity',
    'cyberPolicy': 'policy',
    'bioPolicy': 'policy',
    'misalignmentPolicyViolation': 'policy',
    'modelNotFound': 'model',
    'modelProviderMismatch': 'model',
    'modelProviderNotConfigured': 'model',
    'modelProviderPolicyConflict': 'policy',
    'usageLimitExceeded': 'rate_limit',
    'rateLimitExceeded': 'rate_limit',
    'sessionBudgetExceeded': 'budget',
    'contextWindowExceeded': 'budget',
    'flexUnavailable': 'capacity',
    'serverOverloaded': 'capacity',
    'httpConnectionFailed': 'network',
    'responseStreamConnectionFailed': 'network',
    'responseStreamDisconnected': 'network',
    'responseTooManyFailedAttempts': 'network',
    'internalServerError': 'provider',
    'badRequest': 'request',
    'sandboxError': 'sandbox',
    'tooManyDenials': 'sandbox',
    'threadRollbackFailed': 'protocol',
    'activeTurnNotSteerable': 'protocol',
    'other': 'unknown',
}
_HTTP_CATEGORIES = {400: 'request', 401: 'account', 403: 'account', 404: 'model',
                    408: 'network_timeout', 429: 'rate_limit', 502: 'network', 503: 'capacity'}


class GoalAppServerError(GoalCodexError):
    """Sanitized failure with the App Server facts observed up to that point."""

    def __init__(self, code, message, *, categories=(), evidence=None):
        super().__init__(code, message, categories=categories)
        self.evidence = evidence or {}


def _packet(message):
    return json.dumps(message, ensure_ascii=True, allow_nan=False).encode('ascii') + b'\n'


def _diagnose(error, stderr):
    # 原始错误可能含上游返回的账号信息，只从它识别固定类别。
    error = error if isinstance(error, dict) else {}
    details = {}
    fields = {
        key: error[key] for key in ('message', 'code') if isinstance(error.get(key), str)
    }
    if isinstance(error.get('additionalDetails'), str):
        fields['message'] = fields.get('message', '') + '\n' + error['additionalDetails']
    categories = set(_exit_categories(_packet({'type': 'error', 'error': fields}), stderr))
    info = error.get('codexErrorInfo')
    kind = info if isinstance(info, str) else None
    context = {}
    if isinstance(info, dict) and len(info) == 1:
        kind, context = next(iter(info.items()))
    elif isinstance(error.get('data'), dict):
        context = error['data']
        kind = context.get('type')
    if isinstance(kind, str) and kind in _ERROR_KINDS:
        details['type'] = kind
        categories.add(_ERROR_KINDS[kind])
    status = context.get('httpStatusCode') if isinstance(context, dict) else None
    if type(status) is int and 100 <= status <= 599:
        details['http_status'] = status
        if status in _HTTP_CATEGORIES:
            categories.add(_HTTP_CATEGORIES[status])
    code = error.get('code')
    if type(code) is int and -32768 <= code <= -32000:
        details['rpc_code'] = code
    details['categories'] = sorted(categories - {'unknown'}) or ['unknown']
    return details


def _filesystem(sandbox):
    return {
        ':root': 'deny', ':minimal': 'read',
        ':workspace_roots': {
            '.': 'read' if sandbox == 'read-only' else 'write',
            '.*': 'deny', '**/.*': 'deny',
        },
    }


def _argv(executable, effort, sandbox, workspace, profile):
    argv = [executable, 'app-server', '--listen', 'stdio://']
    for feature in _DISABLED:
        argv.extend(('--disable', feature))
    settings = {
        **_ENDPOINTS,
        'approval_policy': 'never',
        'model_reasoning_effort': effort,
        'default_permissions': profile,
        'web_search': 'disabled',
        'shell_environment_policy.inherit': 'core',
        'shell_environment_policy.include_only': ['PATH', 'LANG', 'LC_*'],
        'allow_login_shell': False,
        'project_doc_max_bytes': 0,
        'developer_instructions': '',
        'instructions': '',
        'notify': [],
        'analytics.enabled': False,
    }
    for key, value in settings.items():
        argv.extend(('-c', f'{key}={json.dumps(value, ensure_ascii=False)}'))
    # CLI 将左侧按点分段。用整张 TOML 表才能保留工作目录中的点。
    workspace_key = json.dumps(str(workspace), ensure_ascii=False)
    argv.extend(('-c', f'projects={{ {workspace_key} = {{ trust_level = "untrusted" }} }}'))
    # 只给 Codex 工具进程开放当前角色的材料目录及系统运行所需文件。
    access = _filesystem(sandbox)[':workspace_roots']['.']
    argv.extend(('-c', f'permissions.{profile}={{ filesystem = {{ ":root" = "deny", '
                       f'":minimal" = "read", ":workspace_roots" = {{ "." = "{access}", '
                       '".*" = "deny", "**/.*" = "deny" } }, network = { enabled = false } }'))
    return argv


@contextlib.contextmanager
def _supervise(command, output_dir, staging, env, *, cwd=None):
    record_path = output_dir / 'process.json'
    record = {'version': 1, 'state': 'starting', 'pid': None,
              'pgid': None, 'birth': None, 'processes': []}
    interrupted = None
    handlers = {}
    process = tree = None
    owns_record = False

    def request_stop(signum, _frame):
        nonlocal interrupted
        if interrupted is None:
            interrupted = signum

    def checkpoint():
        if interrupted is not None:
            raise SystemExit(128 + interrupted)

    def sample():
        observed = tree.sample()
        if observed != record['processes']:
            record['processes'] = observed
            _publish(record_path, staging, record)

    try:
        for signum in _SIGNALS:
            handlers[signum] = signal.signal(signum, request_stop)
        try:
            with record_path.open('x', encoding='utf-8') as stream:
                owns_record = True
                _write_record(stream, record)
        except FileExistsError:
            raise GoalAppServerError('process_pending', 'Earlier role process cleanup is unconfirmed') from None
        checkpoint()
        process = subprocess.Popen(
            command, cwd=staging if cwd is None else cwd, env=env, start_new_session=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        tree = ProcessTree(process.pid)
        root = tree.known.get(process.pid)
        if not root or root['group'] != process.pid or root['state'].startswith('Z'):
            tree = None
            raise GoalAppServerError('process_error', 'Could not identify the gated role process')
        record.update(state='running', pid=root['pid'], pgid=root['group'],
                      birth=root['birth'], processes=list(tree.known.values()))
        _publish(record_path, staging, record)
        yield process, checkpoint, sample
    finally:
        try:
            if process is not None:
                try:
                    _stop(process, tree)
                except Exception:
                    if tree is not None:
                        record['processes'] = list(tree.known.values())
                    record['state'] = 'cleanup_unconfirmed'
                    with contextlib.suppress(OSError):
                        _publish(record_path, staging, record)
                    raise GoalAppServerError('cleanup_unconfirmed', 'Role process cleanup is unconfirmed') from None
                finally:
                    process.stdin.close()
                    process.stdout.close()
                    process.stderr.close()
            if owns_record:
                if tree is not None:
                    record['processes'] = list(tree.known.values())
                record['state'] = 'stopped'
                _publish(record_path, staging, record)
        finally:
            for signum, handler in handlers.items():
                signal.signal(signum, handler)
            checkpoint()


class _Connection:
    def __init__(self, process, streams, checkpoint, sample):
        self.process = process
        self.streams = streams
        self.checkpoint = checkpoint
        self.sample = sample
        self.stdin = process.stdin.fileno()
        self.stdout = process.stdout.fileno()
        self.stderr = process.stderr.fileno()
        self.buffer = bytearray()
        self.diagnostics = bytearray()
        self.bytes_read = 0
        self.closed = False
        self.last_request = None
        self.next_sample = time.monotonic() + _SAMPLE_SECONDS
        for fd in (self.stdin, self.stdout, self.stderr):
            os.set_blocking(fd, False)
        for fd in (self.stdout, self.stderr):
            streams.register(fd, selectors.EVENT_READ)

    def _remaining(self, deadline, stopping):
        if not stopping:
            self.checkpoint()
        now = time.monotonic()
        if now >= deadline:
            raise GoalAppServerError('timeout', 'Codex App Server role exceeded its deadline')
        if now >= self.next_sample:
            self.sample()
            self.next_sample = time.monotonic() + _SAMPLE_SECONDS
        return max(0, min(self.next_sample, deadline) - time.monotonic())

    def _poll(self, deadline, stopping):
        writable = False
        for key, _ in self.streams.select(self._remaining(deadline, stopping)):
            fd = key.fd
            if fd == self.stdin:
                writable = True
                continue
            try:
                data = os.read(fd, 65_536)
            except BlockingIOError:
                continue
            if not data:
                self.streams.unregister(fd)
                if fd == self.stdout:
                    self.closed = True
            elif fd == self.stdout:
                self.bytes_read += len(data)
                if self.bytes_read > _MAX_OUTPUT:
                    raise GoalAppServerError('output_budget', 'Codex App Server exceeded its output limit')
                self.buffer.extend(data)
            else:
                self.diagnostics.extend(data)
                del self.diagnostics[:-65_536]
        return writable

    def _await_write(self, deadline, stopping):
        while not self.closed:
            if self._poll(deadline, stopping):
                return
        raise self.error('process_error', 'Codex App Server closed before the request was written')

    def send(self, packet, deadline, *, gate=None, stopping=False):
        if len(packet) > _MAX_REQUEST:
            raise GoalAppServerError('input_budget', 'Codex App Server request exceeded its input limit')
        # 完整的 JSON 行才触发处理。授权事务内只发送换行，避免持锁写正文。
        pending = memoryview(packet[:-1] if gate else packet)
        self.streams.register(self.stdin, selectors.EVENT_WRITE)
        try:
            while pending:
                self._await_write(deadline, stopping)
                pending = pending[os.write(self.stdin, pending):]
            if gate:
                self._await_write(deadline, stopping)
                with gate():
                    self.checkpoint()
                    if time.monotonic() >= deadline:
                        raise GoalAppServerError('timeout', 'Codex App Server role exceeded its deadline')
                    os.write(self.stdin, packet[-1:])
        finally:
            self.streams.unregister(self.stdin)

    def error(self, code, message, error=None):
        detail = _diagnose(error, self.diagnostics)
        return GoalAppServerError(code, message, categories=tuple(detail['categories']),
                                  evidence={'app_server_error': detail})

    def read_event(self, deadline, accepts, *, stopping=False):
        while True:
            self._remaining(deadline, stopping)
            newline = self.buffer.find(b'\n')
            if newline > _MAX_LINE or (newline < 0 and len(self.buffer) > _MAX_LINE):
                raise GoalAppServerError('output_budget', 'Codex App Server event exceeded its size limit')
            if newline >= 0:
                line = bytes(self.buffer[:newline])
                del self.buffer[:newline + 1]
                try:
                    event = _decode(line)
                    if isinstance(event, dict) and accepts(event):
                        return event
                except (ValueError, RecursionError):
                    pass
                raise GoalAppServerError('invalid_protocol', 'Invalid Codex App Server event')
            if self.closed:
                raise self.error('process_error', 'Codex App Server closed before completion')
            self._poll(deadline, stopping)

    def read(self, deadline, *, stopping=False):
        return self.read_event(deadline, lambda event: 'id' in event or isinstance(event.get('method'), str),
                               stopping=stopping)

    def request(self, request_id, method, params, deadline, *, collect=None, gate=None):
        self.send(_packet({'id': request_id, 'method': method, 'params': params}), deadline, gate=gate)
        self.last_request = request_id
        while True:
            event = self.read(deadline)
            if 'id' not in event:
                if collect:
                    collect(event)
                continue
            if 'method' in event:
                raise GoalAppServerError('unexpected_request', 'Codex App Server requested an unapproved action')
            if type(event['id']) is not int or event['id'] != request_id:
                raise GoalAppServerError('invalid_protocol', 'Unexpected Codex App Server response ID')
            if isinstance(event.get('result'), dict) and 'error' not in event:
                return event['result']
            raise self.error('request_rejected', 'Codex App Server rejected an approved request', event.get('error'))


def _uuid(value):
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            parsed = UUID(value)
            if parsed.int and str(parsed) == value:
                return value
    raise GoalAppServerError('invalid_protocol', 'Invalid Codex App Server thread or turn ID')


def _check_rule_layers(layers, home):
    if not isinstance(layers, list) or not isinstance(home, str) or not Path(home).is_absolute():
        raise GoalAppServerError('invalid_protocol', 'Codex App Server did not report its config layers')
    directories = {Path(home)}
    for layer in layers:
        name = layer.get('name') if isinstance(layer, dict) else None
        if not isinstance(name, dict):
            raise GoalAppServerError('invalid_protocol', 'Malformed Codex App Server config layer')
        if layer.get('disabledReason') is not None:
            continue
        if isinstance(name.get('file'), str):
            directories.add(Path(name['file']).parent)
        if isinstance(name.get('dotCodexFolder'), str):
            directories.add(Path(name['dotCodexFolder']))
    # 命令规则可能在审批策略为 never 时允许命令脱离沙箱。
    for directory in directories:
        try:
            with os.scandir(directory / 'rules') as entries:
                if any(entry.name.endswith('.rules') for entry in entries):
                    raise GoalAppServerError('config_mismatch', 'Codex App Server has an active command rules directory')
        except FileNotFoundError:
            continue
        except OSError:
            raise GoalAppServerError('config_mismatch', 'Could not check active Codex command rules') from None


def _config(connection, workspace, deadline, sandbox, profile):
    initialized = connection.request(1, 'initialize', {
        'clientInfo': {'name': 'autoloop-goal-team', 'version': '1'},
        'capabilities': {'experimentalApi': True, 'requestAttestation': False},
    }, deadline)
    if not isinstance(initialized.get('userAgent'), str):
        raise GoalAppServerError('invalid_protocol', 'Codex App Server did not confirm initialization')
    connection.send(_packet({'method': 'initialized'}), deadline)
    loaded = connection.request(2, 'config/read', {'cwd': str(workspace), 'includeLayers': True}, deadline)
    config = loaded.get('config')
    if not isinstance(config, dict):
        raise GoalAppServerError('invalid_protocol', 'Codex App Server did not confirm configuration')
    _check_rule_layers(loaded.get('layers'), initialized.get('codexHome'))
    mcp = config.get('mcp_servers', {})
    if not isinstance(mcp, dict) or any(
        not isinstance(server, dict) or server.get('enabled') is not False for server in mcp.values()
    ):
        raise GoalAppServerError('config_mismatch', 'Codex App Server has an enabled MCP server')
    providers = config.get('model_providers')
    # 设置 OpenAI API base URL 会绕过 Codex 根据 ChatGPT 登录选择的内置推理地址。
    if (any(config.get(key) != value for key, value in _ENDPOINTS.items())
            or config.get('openai_base_url') is not None
            or (isinstance(providers, dict) and 'openai' in providers)):
        raise GoalAppServerError('config_mismatch', 'Codex App Server model transport differs from approved policy')
    features = config.get('features')
    projects = config.get('projects')
    project = projects.get(str(workspace)) if isinstance(projects, dict) else None
    shell = config.get('shell_environment_policy')
    if (not isinstance(features, dict) or any(features.get(name) is not False for name in _DISABLED)
            or not isinstance(project, dict) or project.get('trust_level') != 'untrusted'
            or config.get('web_search') != 'disabled' or config.get('project_doc_max_bytes') != 0
            or config.get('notify') != [] or config.get('allow_login_shell') is not False
            or not isinstance(shell, dict) or shell.get('inherit') != 'core'
            or shell.get('include_only') != ['PATH', 'LANG', 'LC_*']
            or any(shell.get(key) for key in ('set', 'experimental_use_profile', 'filters'))):
        raise GoalAppServerError('config_mismatch', 'Codex App Server role tools differ from approved policy')
    permissions = config.get('permissions')
    scoped = permissions.get(profile) if isinstance(permissions, dict) else None
    filesystem = scoped.get('filesystem') if isinstance(scoped, dict) else None
    network = scoped.get('network') if isinstance(scoped, dict) else None
    if (not isinstance(filesystem, dict) or not isinstance(network, dict)
            or config.get('default_permissions') != profile
            or scoped.get('extends') is not None or scoped.get('workspace_roots') is not None
            or {key: value for key, value in filesystem.items() if value is not None} != _filesystem(sandbox)
            or {key: value for key, value in network.items() if value is not None} != {'enabled': False}
            or network.get('enabled') is not False):
        raise GoalAppServerError('config_mismatch', 'Codex App Server permission profile differs from approved policy')
    skills = connection.request(6, 'skills/list', {'cwds': [str(workspace)]}, deadline).get('data')
    if (not isinstance(skills, list) or len(skills) != 1 or not isinstance(skills[0], dict)
            or skills[0].get('cwd') != str(workspace) or not isinstance(skills[0].get('skills'), list)
            or skills[0].get('errors') != []):
        raise GoalAppServerError('config_mismatch', 'Could not verify isolated Codex skills')
    if any(not isinstance(skill, dict) or (skill.get('enabled') is not False and skill.get('scope') != 'system')
           for skill in skills[0]['skills']):
        raise GoalAppServerError('config_mismatch', 'Codex App Server has an enabled user or project skill')


def _check_thread(ack, model, effort, sandbox, workspace, evidence, profile):
    if ack.get('model') != model:
        observed = ack.get('model')
        evidence['model'] = observed if isinstance(observed, str) and observed in _MODELS else 'unapproved'
        raise GoalAppServerError('model_mismatch', 'Codex App Server selected an unapproved model')
    evidence['model'] = model
    sandbox_ack = ack.get('sandbox')
    if (ack.get('modelProvider') != _ENDPOINTS['model_provider'] or ack.get('cwd') != str(workspace)
            or ack.get('approvalPolicy') != 'never' or ack.get('instructionSources') != []
            or ack.get('activePermissionProfile') != {'id': profile, 'extends': None}
            or ack.get('runtimeWorkspaceRoots') != [str(workspace)]
            or sandbox_ack != _SANDBOXES[sandbox] or sandbox_ack.get('networkAccess') is not False
            or (sandbox == 'workspace-write' and
                any(sandbox_ack.get(key) is not True for key in ('excludeTmpdirEnvVar', 'excludeSlashTmp')))
            or ack.get('reasoningEffort') != effort):
        raise GoalAppServerError('config_mismatch', 'Codex App Server thread differs from approved settings')
    policy_digest = hashlib.sha256(_packet({'filesystem': _filesystem(sandbox), 'network': False})).hexdigest()
    evidence.update(sandbox=sandbox, effort=effort, approval_policy='never',
                    permission_profile=profile, permission_digest=policy_digest)
    thread = ack.get('thread')
    if (not isinstance(thread, dict) or thread.get('ephemeral') is not True
            or thread.get('turns') != [] or thread.get('parentThreadId') is not None):
        raise GoalAppServerError('invalid_protocol', 'Codex App Server did not create a fresh ephemeral thread')
    version = thread.get('cliVersion')
    if isinstance(version, str) and re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]{1,30})?', version):
        evidence['cli_version'] = version
    return _uuid(thread.get('id'))


def _result_matches(value, schema):
    # 复用项目 JSON 形状检查，再校验该角色 Schema 中的枚举约束。
    def allowed(item, rule):
        if 'enum' in rule and item not in rule['enum']:
            return False
        if rule['type'] == 'object':
            return all(allowed(item[key], prop) for key, prop in rule['properties'].items())
        if rule['type'] == 'array':
            return all(allowed(child, rule['items']) for child in item)
        return True

    return _matches(value, schema) and allowed(value, schema)


class _Turn:
    def __init__(self, connection, evidence, model, sandbox, read_challenge=None):
        self.connection = connection
        self.evidence = evidence
        self.thread = evidence['session']
        self.model = model
        self.sandbox = sandbox
        self.read_challenge = read_challenge or {}
        self.id = None
        self.completed = False
        self.final = None
        self.fallback = None
        self.tools = {}

    def bind(self, value):
        turn_id = _uuid(value)
        if self.id and self.id != turn_id:
            raise GoalAppServerError('invalid_protocol', 'Codex App Server supplied conflicting turn IDs')
        self.id = turn_id
        self.evidence['turn'] = turn_id

    def collect(self, event):
        method, params = event.get('method'), event.get('params')
        if not isinstance(params, dict) or params.get('threadId') != self.thread:
            return
        if method in ('turn/started', 'turn/completed'):
            turn = params.get('turn')
            if not isinstance(turn, dict):
                raise GoalAppServerError('invalid_protocol', 'Codex App Server turn event is malformed')
            self.bind(turn.get('id'))
            if method == 'turn/completed':
                if self.completed:
                    raise GoalAppServerError('invalid_protocol', 'Duplicate Codex App Server turn completion')
                self.completed = True
                if turn.get('status') != 'completed' or 'error' not in turn or turn['error'] is not None:
                    raise self.connection.error('turn_failed', 'Codex App Server turn did not complete', turn.get('error'))
            return
        if method not in ('item/started', 'item/completed', 'model/rerouted',
                          'thread/tokenUsage/updated', 'error',
                          'modelProvider/authRecoveryStarted', 'modelProvider/authRecoveryCompleted'):
            return
        self.bind(params.get('turnId'))
        if method == 'model/rerouted':
            observed = params.get('toModel')
            if observed != self.model or params.get('fromModel') != self.model:
                self.evidence['rerouted_model'] = observed if isinstance(observed, str) and observed in _MODELS else 'unapproved'
                raise GoalAppServerError('model_rerouted', 'Codex App Server turn changed its approved model')
        elif method == 'error':
            if params.get('willRetry') is not True:
                raise self.connection.error('turn_failed', 'Codex App Server reported a turn error', params.get('error'))
            self.evidence['app_server_retry'] = _diagnose(params.get('error'), self.connection.diagnostics)
        elif method in ('modelProvider/authRecoveryStarted', 'modelProvider/authRecoveryCompleted'):
            self.evidence['auth_recovery'] = 'started' if method.endswith('Started') else 'completed'
        elif method == 'thread/tokenUsage/updated':
            count = params.get('tokenUsage')
            total = count.get('total') if isinstance(count, dict) else None
            if isinstance(total, dict) and all(
                type(total.get(key)) is int and total[key] >= 0 for key in _TOKEN_FIELDS if key != 'cacheWriteInputTokens'
            ):
                self.evidence['usage'] = {name: total[key] for key, name in _TOKEN_FIELDS.items()
                                          if type(total.get(key)) is int and total[key] >= 0}
                self.evidence['usage_source'] = 'thread/tokenUsage/updated'
        elif method in ('item/started', 'item/completed'):
            self._item(params.get('item'), method == 'item/completed')

    def _item(self, item, complete):
        if not isinstance(item, dict) or not isinstance(item.get('type'), str):
            raise GoalAppServerError('invalid_protocol', 'Malformed Codex App Server item')
        kind = item['type']
        if kind == 'agentMessage' and complete:
            text = item.get('text')
            if not isinstance(text, str):
                raise GoalAppServerError('invalid_output', 'Codex App Server returned an invalid assistant message')
            if item.get('phase') == 'final_answer':
                self.final = text
            elif item.get('phase') is None:
                self.fallback = text
        elif (kind in _OTHER_TOOLS or (kind == 'fileChange' and self.sandbox == 'read-only')):
            self.evidence['app_server_error'] = {'item_type':kind}
            raise GoalAppServerError('unapproved_tool', 'Codex App Server reported an unapproved tool')
        elif kind in _TOOL_TYPES:
            key = item.get('id')
            if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', key):
                raise GoalAppServerError('invalid_protocol', 'Invalid Codex App Server tool call ID')
            previous = self.tools.get(key)
            if previous and (previous['name'] != _TOOL_TYPES[kind] or previous['settled'] or not complete):
                raise GoalAppServerError('invalid_protocol', 'Conflicting Codex App Server tool events')
            status = item.get('status')
            self.tools[key] = dict(
                id=key, name=_TOOL_TYPES[kind], settled=complete, paired=bool(previous),
                status=status if isinstance(status, str) and status in _TOOL_STATES else 'unknown',
            )
            for wire, field in (('exitCode', 'exit_code'), ('durationMs', 'duration_ms')):
                if complete and type(item.get(wire)) is int:
                    self.tools[key][field] = item[wire]
            if (self.read_challenge and kind == 'commandExecution' and previous and complete
                    and status == 'completed' and self.tools[key].get('exit_code') == 0):
                command_output = item.get('aggregatedOutput')
                if isinstance(command_output,str):
                    proved = set(_READ_CHALLENGE.findall(command_output)) & self.read_challenge.keys()
                    if proved:
                        self.tools[key]['candidate_reads'] = sorted(
                            (self.read_challenge[value] for value in proved), key=lambda row:row['path'],
                        )
            self.evidence['tools'] = list(self.tools.values())

    def result(self, schema):
        text = self.final if self.final is not None else self.fallback
        try:
            result = _decode(text)
            if _result_matches(result, schema):
                return result
        except (ValueError, TypeError, KeyError, RecursionError):
            pass
        raise GoalAppServerError('invalid_output', 'Codex App Server result does not match the role schema')

    def interrupt(self):
        if self.completed or self.connection.last_request != _TURN_START:
            return
        deadline = time.monotonic() + _INTERRUPT_GRACE
        with contextlib.suppress(GoalCodexError, OSError):
            # 停止信号可能先于 turn/start 回执。短暂等待本轮 ID 再精确中断。
            while self.id is None:
                event = self.connection.read(deadline, stopping=True)
                if event.get('id') == _TURN_START:
                    result = event.get('result')
                    turn = result.get('turn') if isinstance(result, dict) else None
                elif event.get('method') == 'turn/started':
                    params = event.get('params')
                    turn = params.get('turn') if isinstance(params, dict) and params.get('threadId') == self.thread else None
                else:
                    continue
                if isinstance(turn, dict):
                    self.bind(turn.get('id'))
            self.connection.send(_packet({
                'id': _TURN_INTERRUPT, 'method': 'turn/interrupt', 'params': {'threadId': self.thread, 'turnId': self.id},
            }), deadline, stopping=True)
            acknowledged = terminal = False
            while not (acknowledged and terminal):
                event = self.connection.read(deadline, stopping=True)
                if event.get('id') == _TURN_INTERRUPT:
                    acknowledged = isinstance(event.get('result'), dict)
                elif event.get('method') == 'turn/completed':
                    params = event.get('params')
                    if isinstance(params, dict) and params.get('threadId') == self.thread:
                        turn = params.get('turn')
                        terminal = isinstance(turn, dict) and turn.get('id') == self.id


def _run(connection, model, effort, sandbox, prompt, schema, workspace, deadline, launch_gate, evidence, profile,
         read_challenge):
    _config(connection, workspace, deadline, sandbox, profile)
    started = connection.request(3, 'thread/start', {
        'model': model, 'modelProvider': _ENDPOINTS['model_provider'], 'cwd': str(workspace),
        'permissions': profile, 'runtimeWorkspaceRoots': [str(workspace)],
        'approvalPolicy': 'never', 'ephemeral': True, 'allowProviderModelFallback': False,
    }, deadline)
    evidence['session'] = _check_thread(started, model, effort, sandbox, workspace, evidence, profile)
    turn = _Turn(connection, evidence, model, sandbox, read_challenge)
    try:
        ack = connection.request(_TURN_START, 'turn/start', {
            'threadId': turn.thread, 'input': [{'type': 'text', 'text': prompt, 'text_elements': []}],
            'outputSchema': schema,
        }, deadline, collect=turn.collect, gate=launch_gate)
        turn_response = ack.get('turn')
        if not isinstance(turn_response, dict):
            raise GoalAppServerError('invalid_protocol', 'Codex App Server did not confirm the role turn')
        turn.bind(turn_response.get('id'))
        if turn_response.get('status') != 'inProgress' or turn_response.get('error') is not None:
            raise GoalAppServerError('invalid_protocol', 'Codex App Server did not start a new role turn')
        while not turn.completed:
            event = connection.read(deadline)
            if 'id' in event:
                raise GoalAppServerError('unexpected_request', 'Unexpected request or response during the role turn')
            turn.collect(event)
        return turn.result(schema)
    finally:
        turn.interrupt()


def invoke(*, model, effort, sandbox, prompt, schema, workspace, output_dir, deadline,
           launch_gate=contextlib.nullcontext, read_challenge=None):
    """Run exactly one role turn before a shared monotonic deadline."""
    if (not isinstance(model, str) or model not in _MODELS
            or not isinstance(effort, str) or effort not in _EFFORTS
            or not isinstance(sandbox, str) or sandbox not in _SANDBOXES
            or not isinstance(prompt, str) or not prompt.strip()
            or type(schema) is not dict or schema.get('type') != 'object'
            or type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise GoalAppServerError('invalid_assignment', 'Unsupported App Server role settings')
    if time.monotonic() >= deadline:
        raise GoalAppServerError('timeout', 'Codex App Server role deadline already elapsed')
    try:
        env = trusted_cli_environment()
    except ValueError:
        raise GoalAppServerError('invalid_config', 'Invalid loopback model transport configuration') from None
    executable = shutil.which('codex', path=env.get('PATH', os.defpath))
    if executable is None:
        raise GoalAppServerError('process_error', 'Codex CLI was not found')
    evidence = {'requested_model': model, 'started_ns': time.time_ns(), 'usage': None, 'tools': []}
    try:
        workspace = Path(workspace).resolve(strict=True)
        output_dir = Path(output_dir).resolve(strict=True)
        profile = f'autoloop_role_{uuid4().hex}'
        command = [sys.executable, '-B', str(_GATE), *_argv(os.path.abspath(executable), effort, sandbox, workspace, profile)]
        with tempfile.TemporaryDirectory(prefix='role-app-server-', dir=output_dir) as staging:
            with _supervise(command, output_dir, staging, env) as (process, checkpoint, sample):
                with launch_gate():
                    checkpoint()
                    if time.monotonic() >= deadline:
                        raise GoalAppServerError('timeout', 'Codex App Server role deadline already elapsed')
                    os.write(process.stdin.fileno(), b'1')
                with selectors.DefaultSelector() as streams:
                    connection = _Connection(process, streams, checkpoint, sample)
                    result = _run(connection, model, effort, sandbox, prompt, schema, workspace, deadline,
                                  launch_gate, evidence, profile, read_challenge)
    except GoalAppServerError as exc:
        exc.evidence.update(evidence, ended_ns=time.time_ns(), failure_code=exc.code)
        raise
    except (OSError, ValueError, OverflowError, subprocess.SubprocessError):
        raise GoalAppServerError(
            'process_error', 'Could not prepare, launch, or supervise Codex App Server',
            evidence=dict(evidence, ended_ns=time.time_ns()),
        ) from None
    return dict(evidence, result=result, ended_ns=time.time_ns())
