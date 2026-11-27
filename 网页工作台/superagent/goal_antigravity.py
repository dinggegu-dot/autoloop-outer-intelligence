"""Run an approved Autoloop role through Google's local Antigravity CLI."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import selectors
import stat
import subprocess
import sys
import tempfile
import time
from uuid import NAMESPACE_URL, uuid5

from . import goal_checks
from .goal_app_server import GoalAppServerError, _Connection, _result_matches, _supervise, _uuid
from .outer_verification import capture


_SETTINGS = {
    'agentMode': '',
    'toolPermission': 'strict',
    'allowNonWorkspaceAccess': False,
    'disableSlashCommands': True,
    'useG1Credits': False,
    'trustedWorkspaces': [],
    'permissions': {'allow': [], 'ask': [], 'deny': [
        f'{tool}(*)' for tool in ('command', 'unsandboxed', 'read_file', 'write_file', 'read_url',
                                 'execute_url', 'mcp')
    ]},
}
_MAX_CONTEXT_BYTES = 180_000
_SANDBOX = Path('/usr/bin/sandbox-exec')
_ARTIFACT_CHANGE = {
    'type': 'object',
    'properties': {
        'path': {'type': 'string'},
        'action': {'type': 'string', 'enum': ['write', 'delete']},
        'content': {'type': 'string'},
    },
    'required': ['path', 'action', 'content'],
    'additionalProperties': False,
}


def _owner_home():
    import pwd

    # The Codex worker has a separate HOME. Google authentication belongs to
    # the macOS login account and stays in the official CLI's keychain path.
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def executable():
    if sys.platform != 'darwin' or not _SANDBOX.is_file():
        return None
    override = os.environ.get('AUTOLOOP_ANTIGRAVITY_EXECUTABLE')
    path = Path(override) if override else _owner_home() / '.local/share/autoloop/antigravity/agy'
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        return None
    try:
        info = path.stat()
    except OSError:
        return None
    if info.st_uid != os.getuid() or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return None
    return path.resolve()


def _seatbelt(program, staging, private_directories):
    home = _owner_home()
    # The provider keeps its own login. Personal rules, hooks, local apps and
    # workspace files must not reach the CLI's independent agent session.
    discovery = [home / '.gemini' / name for name in (
        'agents', 'extensions', 'skills', 'AGENTS.md', 'GEMINI.md', 'settings.json',
    )] + [home / name for name in ('.agents', 'AGENTS.md', 'GEMINI.md')]
    discovery += [home / '.gemini/config' / name for name in (
        'agents', 'plugins', 'skills', 'hooks.json', 'mcp_config.json', 'rules', 'sidecars',
    )]
    discovery += [home / '.gemini/antigravity-cli' / name for name in (
        'settings.json', 'hooks.json', 'mcp_config.json', 'plugins', 'skills', 'rules', 'agents',
    )]
    discovery += [home / name for name in (
        'Documents', 'Desktop', 'Downloads', '.codex', '.ssh', '.aws', '.agent-autoloop',
        'Library/Application Support/Autoloop',
    )]
    return '\n'.join([
        '(version 1)', '(allow default)',
        '(deny process-exec)',
        f'(allow process-exec (literal {json.dumps(str(program))}) (literal "/usr/bin/security"))',
        *[f'(deny file-read* file-write* (subpath {json.dumps(str(path), ensure_ascii=False)}))'
          for path in discovery],
        *[f'(deny file-read* file-write* (subpath {json.dumps(str(path), ensure_ascii=False)}))'
          for path in private_directories],
        f'(deny file-write* (literal {json.dumps(str(program))}))',
        f'(allow file-read* file-write* (subpath {json.dumps(str(staging), ensure_ascii=False)}))',
    ])


def _inputs(workspace):
    fingerprint, files, occupied = capture(workspace)
    contents = {}
    for name, data in files.items():
        if any(part.startswith('.') for part in name.split('/')):
            continue
        try:
            contents[name] = data.decode('utf-8')
        except UnicodeDecodeError:
            continue
    return fingerprint, contents, occupied


def _apply(workspace, before, occupied, provided, changes):
    if (type(changes) is not list or len(changes) > goal_checks.MAX_ENTRIES
            or capture(workspace)[0] != before):
        raise ValueError('candidate changed or too many proposed edits')
    staged = {}
    aliases = {goal_checks._fold(path): (path, directory) for path, directory in occupied.items()}
    for item in changes:
        path = goal_checks._path(item['path'])
        parts = path.split('/')
        parents = ['/'.join(parts[:index]) for index in range(1,len(parts))]
        if (goal_checks._fold(Path(path).name) == 'agents.md'
                or aliases.get(goal_checks._fold(path), (path, False)) != (path, False)
                or any(aliases.get(goal_checks._fold(parent), (parent, True)) != (parent, True)
                       for parent in parents)
                or any(goal_checks._overlap(other, path) for other in staged)
                or (path in occupied and path not in provided)
                or item['action'] not in ('write', 'delete')):
            raise ValueError('artifact path conflicts with the candidate')
        content = item['content'].encode('utf-8')
        if item['action'] == 'delete':
            if content or path not in before['files']:
                raise ValueError('cannot delete an unregistered artifact')
            staged[path] = None
            continue
        if len(content) > goal_checks.MAX_WORKSPACE_FILE or '\x00' in item['content']:
            raise ValueError('artifact exceeds the text file limit')
        staged[path] = content
    for path, content in staged.items():
        target = workspace / path
        if content is None:
            target.unlink()
            continue
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='wb', dir=target.parent, delete=False) as f:
            temporary = Path(f.name)
            f.write(content)
        try:
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)


def _response_schema(schema, writable):
    if not writable:
        return schema
    return {'type': 'object', 'properties': {
        'result': schema, 'changes': {'type': 'array', 'items': _ARTIFACT_CHANGE},
    }, 'required': ['result', 'changes'], 'additionalProperties': False}


def _get_event(connection, deadline):
    return connection.read_event(deadline, lambda event: event.get('event') in ('init', 'step_update', 'result'))


def _exchange(connection, *, staging, model, schema, packet, deadline, launch_gate, evidence):
    init = _get_event(connection, deadline)
    if init['event'] == 'result' and 'authentication' in init.get('result', {}).get('error', '').lower():
        raise GoalAppServerError('auth_required', '请在本机 Google Antigravity 中确认登录后重试')
    config = init.get('init') or {}
    if (init['event'] != 'init' or config.get('cwd') != str(staging) or config.get('model') != model
            or config.get('permission_mode') != 'strict' or config.get('json_schema') != schema):
        raise GoalAppServerError('config_mismatch', 'Antigravity did not apply the approved role settings')
    session = _uuid(init.get('conversation_id'))
    evidence.update(session=session, model=model, approval_policy='strict')
    connection.send(packet, deadline, gate=launch_gate)
    # EOF completes the single-turn streaming session.
    connection.process.stdin.close()
    while True:
        event = _get_event(connection, deadline)
        if event['event'] == 'step_update':
            step = event.get('step_update') or {}
            if (step.get('conversation_id') != session
                    or step.get('step_type') not in ('user_input', 'agent_response', 'checkpoint', 'finish')
                    or step.get('tool_info') or step.get('subagent_info')):
                raise GoalAppServerError('unexpected_request', 'Antigravity requested a tool outside the role')
            continue
        result = event.get('result') or {}
        reply = result.get('structured_output')
        if event['event'] != 'result' or result.get('conversation_id') != session:
            raise GoalAppServerError('invalid_protocol', 'Antigravity returned an unrelated role result')
        if result.get('status') != 'SUCCESS' or not _result_matches(reply, schema):
            raise GoalAppServerError('invalid_output', 'Antigravity returned an unsuccessful or invalid role result')
        counts = result.get('usage') or {}
        usage = {key: counts[source] for key, source in {
            'input_tokens': 'input_tokens', 'output_tokens': 'output_tokens',
            'cached_input_tokens': 'cache_read_tokens', 'reasoning_output_tokens': 'thinking_tokens',
            'total_tokens': 'total_tokens',
        }.items() if type(counts.get(source)) is int and counts[source] >= 0}
        # The CLI only exposes a conversation ID. Derive the local one-turn
        # receipt ID from that conversation, without claiming a provider turn ID.
        evidence.update(turn=str(uuid5(NAMESPACE_URL, f'antigravity:{session}:1')),
                        usage=usage or None, usage_source='antigravity' if usage else None)
        return reply


def invoke(*, model, effort, sandbox, prompt, schema, workspace, output_dir, deadline,
           launch_gate=contextlib.nullcontext, read_challenge=None):
    program = executable()
    if program is None:
        raise GoalAppServerError('provider_unavailable', '本机的 Google Antigravity CLI 暂不可用')
    writable = sandbox == 'workspace-write'
    evidence = dict(requested_model=model, effort=effort, sandbox=sandbox, started_ns=time.time_ns(),
                    usage=None, tools=[])
    try:
        workspace = Path(workspace).resolve(strict=True)
        checkpoint, files, occupied = _inputs(workspace)
        response_schema = _response_schema(schema, writable)
        delivery = ('只通过 changes 提交文件变更。write 的 content 写完整文本，delete 的 content 留空。'
                    '先仔细自查，不得声称运行了 shell 或工作台验收。' if writable else
                    '本角色只读，直接返回要求的回执。')
        input_text = (f'{prompt}\n你只能处理下方工单候选文本。系统会核对路径并应用变更。'
                      f'不要调用任何工具。{delivery}\n候选文本文件：\n'
                      + json.dumps(files, ensure_ascii=False))
        packet = (json.dumps({'event': 'user', 'message': {'content': input_text}}, ensure_ascii=False)+'\n').encode()
        if len(packet) > _MAX_CONTEXT_BYTES:
            raise GoalAppServerError('input_budget', 'Antigravity 可处理的本轮候选文本超出大小上限')
        with tempfile.TemporaryDirectory(prefix='autoloop-antigravity-') as temporary:
            staging = Path(temporary).resolve()
            settings = staging / 'settings.json'
            settings.write_text(json.dumps(_SETTINGS))
            schema_path = staging / 'response.json'
            schema_path.write_text(json.dumps(response_schema))
            policy = staging / 'role.sb'
            private_directory = Path(output_dir).resolve().parent
            policy.write_text(_seatbelt(program, staging, (workspace, private_directory)))
            remaining = int(deadline-time.monotonic())
            if remaining <= 0:
                raise GoalAppServerError('timeout', 'Antigravity 的角色调用已超过时限')
            command = [
                str(_SANDBOX), '-f', str(policy), str(program), '--settings', str(settings),
                '--sandbox', '--disable-slash-commands', '--model', model, '--effort', effort,
                '--print=', '--input-format', 'stream-json', '--output-format', 'stream-json',
                '--json-schema', str(schema_path), '--print-timeout', f'{remaining}s',
                '--log-file', str(staging / 'cli.log'),
            ]
            env = {'PATH': '/usr/bin:/bin', 'HOME': str(_owner_home()), 'TMPDIR': str(staging), 'LANG': 'en_US.UTF-8'}
            # Process receipts remain in the existing recovery directory. Publication is atomic on the same volume.
            with tempfile.TemporaryDirectory(prefix='role-antigravity-', dir=output_dir) as record:
                with _supervise(command, output_dir, Path(record), env, cwd=staging) as (process, check, sample):
                    with selectors.DefaultSelector() as streams:
                        reply = _exchange(_Connection(process, streams, check, sample), staging=staging, model=model,
                                          schema=response_schema, packet=packet, deadline=deadline,
                                          launch_gate=launch_gate, evidence=evidence)
        with launch_gate():
            if writable:
                _apply(workspace, checkpoint, occupied, files, reply['changes'])
                result = reply['result']
            else:
                result = reply
                if read_challenge:
                    evidence['tools'].append({
                        'name':'antigravity_context', 'settled':True, 'paired':True, 'status':'completed', 'exit_code':0,
                        'candidate_reads': [
                            {'path':path,'sha256':hashlib.sha256(text.encode()).hexdigest()}
                            for path, text in files.items()
                        ],
                    })
    except GoalAppServerError as error:
        error.evidence.update(evidence, ended_ns=time.time_ns(), failure_code=error.code)
        raise
    except (OSError, ValueError, OverflowError, subprocess.SubprocessError):
        raise GoalAppServerError(
            'process_error', '无法启动 Antigravity 或应用本轮候选修改',
            evidence=dict(evidence, ended_ns=time.time_ns()),
        ) from None
    return dict(evidence, result=result, ended_ns=time.time_ns())
