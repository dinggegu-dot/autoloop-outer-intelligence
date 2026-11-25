import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import process_guard

if __package__:
    from . import goal_app_server as app
    from . import goal_codex
    from .codex_worker import _object
else:
    import goal_app_server as app
    import goal_codex
    from codex_worker import _object


_THREAD = '019abcde-1234-7890-8123-123456789abc'
_TURN = '019abcde-1234-7890-8123-123456789abd'
_FOREIGN = '019abcde-1234-7890-8123-123456789abe'
_SCHEMA = _object(action={'type': 'string', 'enum': ['finish']})
_POPEN = subprocess.Popen
_APP_FIXTURE = r'''
import json, os, signal, sys
from pathlib import Path

args = sys.argv[1:]
settings = dict(pair.split('=', 1) for i, pair in enumerate(args) if i > 0 and args[i-1] == '-c')
disabled = [flag for i, flag in enumerate(args) if i > 0 and args[i-1] == '--disable']
assert args[:3] == ['app-server', '--listen', 'stdio://']
assert settings['shell_environment_policy.include_only'] == '["PATH", "LANG", "LC_*"]'
assert 'openai_base_url' not in settings
assert 'apps' in disabled and 'plugins' in disabled and 'multi_agent_v2' in disabled
assert 'CODEX_TEST_SECRET' not in os.environ
profile_setting, profile_toml = next((key, value) for key, value in settings.items() if key.startswith('permissions.'))
profile = profile_setting.split('.', 1)[1]
assert f'"." = "{access}"' in profile_toml and '"**/.*" = "deny"' in profile_toml
assert ':root" = "deny"' in profile_toml and 'network = { enabled = false }' in profile_toml
record = json.loads((Path.cwd().parent / 'process.json').read_text())
assert record['state'] == 'running' and record['pid'] == os.getpid() and record['pgid'] == os.getpgrp()

def recv(expected):
    event = json.loads(sys.stdin.readline())
    assert event['method'] == expected, event['method']
    with trace.open('a') as stream:
        stream.write(expected + '\n')
    return event

def send(**event):
    print(json.dumps(event), flush=True)

def notify(method, **params):
    send(method=method, params={'threadId': thread, **params})

start = recv('initialize')
assert start['params']['capabilities']['experimentalApi'] is True
codex_home = Path.cwd().parent / 'codex_home'
send(id=start['id'], result={'userAgent':'codex-offline-fixture', 'codexHome': str(codex_home)})
recv('initialized')
config_request = recv('config/read')
workspace = config_request['params']['cwd']
config = {key: json.loads(settings[key]) for key in ('web_search', 'project_doc_max_bytes',
                                                    'model_provider', 'forced_login_method', 'chatgpt_base_url')}
config.update(features=dict.fromkeys(disabled, False), projects={workspace:{'trust_level':'untrusted'}})
config['default_permissions'] = json.loads(settings['default_permissions'])
config.update(notify=[], allow_login_shell=False, shell_environment_policy={
    'inherit':'core', 'include_only':['PATH','LANG','LC_*'],
})
config['permissions'] = {profile: {'extends': None,'workspace_roots': None,
    'filesystem':{':root':'deny', ':minimal':'read', ':workspace_roots':{'.':access, '.*':'deny', '**/.*':'deny'}},
    'network':{'enabled':False},
}}
if mode == 'active_mcp':
    config['mcp_servers'] = {'synthetic':{'enabled':True}}
if mode == 'custom_inference_endpoint':
    config['openai_base_url'] = 'https://api.openai.com/v1'
send(id=config_request['id'], result={'config':config, 'layers':[
    {'name':{'type':'user','file':str(codex_home/'config.toml')}},
]})
if mode in ('active_mcp', 'active_rules', 'custom_inference_endpoint'):
    sys.exit(0)
skills = recv('skills/list')
send(id=skills['id'], result={'data': [{'cwd':workspace, 'errors':[], 'skills':[
    {'scope':'user', 'enabled':True},
] if mode == 'active_skill' else []}]})
if mode == 'active_skill':
    sys.exit(0)

request = recv('thread/start')
params = request['params']
assert params == dict(cwd=workspace, model=model, modelProvider='openai', permissions=profile,
                      runtimeWorkspaceRoots=[workspace], ephemeral=True, approvalPolicy='never',
                      allowProviderModelFallback=False)
policy = {'type':'readOnly', 'networkAccess':False}
if access == 'write':
    policy.update(type='workspaceWrite', writableRoots=[], excludeTmpdirEnvVar=True, excludeSlashTmp=True)
if mode == 'sandbox_mismatch':
    policy['networkAccess'] = True
send(id=request['id'], result={
    'model': 'gpt-6.2-astra' if mode == 'model_mismatch' else model,
    'cwd':workspace, 'modelProvider':'openai', 'approvalPolicy':'never', 'sandbox':policy,
    'reasoningEffort':'high', 'instructionSources':[],
    'activePermissionProfile':{'id':profile,'extends':None}, 'runtimeWorkspaceRoots':[workspace],
    'thread':{'id':thread, 'ephemeral':True, 'turns':[], 'cliVersion':'0.171.0-alpha.13.2'},
})
if mode in ('sandbox_mismatch', 'model_mismatch'):
    sys.exit(0)

request = recv('turn/start')
assert request['params']['threadId'] == thread
assert request['params']['input'] == [{'type':'text', 'text':prompt, 'text_elements':[]}]
assert request['params']['outputSchema'] == schema
running = {'id':turn, 'status':'inProgress', 'error':None}
notify('turn/started', turn=running)
send(id=request['id'], result={'turn':running})

if mode in ('reroute', 'signal', 'timeout', 'unexpected_request', 'unapproved_tool'):
    if mode == 'reroute':
        notify('model/rerouted', turnId=turn, fromModel=model, toModel='gpt-6.2-astra')
    if mode == 'signal':
        os.kill(os.getppid(), signal.SIGTERM)
    if mode == 'unexpected_request':
        send(id='server-1', method='item/permissions/requestApproval', params={'turnId':turn, 'threadId':thread})
    if mode == 'unapproved_tool':
        notify('item/started', turnId=turn, item={'type':'fileChange', 'id':'file-1'})
    interrupt = recv('turn/interrupt')
    assert interrupt['params'] == {'threadId':thread, 'turnId':turn}
    send(id=interrupt['id'], result={})
    notify('turn/completed', turn={'id':turn, 'status':'interrupted', 'error':None})
else:
    send(method='item/completed', params={'threadId':foreign, 'turnId':foreign,
                                         'item':{'type':'agentMessage','text':'wrong thread'}})
    command = {'type':'commandExecution', 'id':'command-1', 'command':'unredacted_command',
               'aggregatedOutput':'unredacted_output'}
    notify('item/started', turnId=turn, item=dict(command, status='inProgress'))
    if mode in ('proved_read','failed_read'):
        command['aggregatedOutput'] += '\nworkbench-read:'+'a'*64+'\nworkbench-read:'+'c'*64
    if mode == 'command_only':
        command['command'] += '\nworkbench-read:'+'a'*64
    notify('item/completed', turnId=turn, item=dict(
        command, status='completed', exitCode=1 if mode == 'failed_read' else 0, durationMs=47,
    ))
    notify('thread/tokenUsage/updated', turnId=turn, tokenUsage={
        'total':{'totalTokens':21,'inputTokens':12,'outputTokens':9,'cachedInputTokens':4,'reasoningOutputTokens':7},
        'last':{'totalTokens':2},
    })
    text = '{"action":"finish"}'
    if mode == 'invalid_result':
        text = '{"action":"unknown"}'
    notify('item/completed', turnId=turn, item={'type':'agentMessage','id':'assistant-1',
                                              'phase':'final_answer', 'text':text})
    notify('item/completed', turnId=turn, item={'type':'agentMessage','id':'assistant-2',
                                              'phase':'commentary', 'text':'synthetic comment'})
    terminal = {'id':turn, 'status':'completed', 'error':None}
    if mode == 'turn_error':
        terminal['error'] = {'message':'internal marker: sensitive fixture',
                             'codexErrorInfo':{'responseStreamConnectionFailed':{'httpStatusCode':401}}}
    if mode == 'wrong_turn':
        terminal['id'] = foreign
    notify('turn/completed', turn=terminal)
    if mode == 'wrong_turn':
        recv('turn/interrupt')

sys.stdin.read()
'''


class GoalAppServerTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.output = self.root / 'role'
        self.workspace = self.root / 'synthetic.workspace'
        self.trace = self.root / 'received'
        self.output.mkdir()
        self.workspace.mkdir()
        self.entered = 0

    @contextlib.contextmanager
    def gate(self):
        self.entered += 1
        self.assertEqual(json.loads((self.output / 'process.json').read_text())['state'], 'running')
        yield

    def invoke(self, mode='normal', *, model='gpt-6.2-sol', sandbox='read-only', timeout=3, gate=None,
               read_challenge=None):
        fake = self.root / 'codex'
        if mode == 'active_rules':
            rules = self.output / 'codex_home' / 'rules'
            rules.mkdir(parents=True)
            (rules / 'fixture.rules').touch()
        prompt = '合成材料。 ' * 3000  # Larger than the pipe buffer.
        access = 'read' if sandbox == 'read-only' else 'write'
        values = dict(mode=mode, model=model, access=access, thread=_THREAD, turn=_TURN, schema=_SCHEMA,
                      foreign=_FOREIGN, prompt=prompt)
        prelude = ''.join(f'{key} = {value!r}\n' for key, value in values.items())
        fake.write_text(f'#!{sys.executable}\n# coding: utf-8\nfrom pathlib import Path\ntrace = Path({str(self.trace)!r})\n'
                        + prelude + _APP_FIXTURE)
        fake.chmod(0o700)

        def spawn(command, **kwargs):
            if str(app._GATE) not in command:
                return _POPEN(command, **kwargs)
            self.child = _POPEN(command, **kwargs)
            return self.child

        def snapshot():
            # 宿主沙箱限制 ps。此处保留真实子进程、进程组、管道和信号。
            if self.child.poll() is not None:
                return {}
            pid = self.child.pid
            try:
                group = os.getpgid(pid)
            except ProcessLookupError:
                return {}
            return {pid: {'pid':pid, 'parent':os.getpid(), 'group':group,
                          'birth':'synthetic birth', 'state':'S'}}

        with (patch.object(app.shutil, 'which', return_value=str(fake)),
              patch.object(app.subprocess, 'Popen', side_effect=spawn),
              patch.object(process_guard, 'processes', side_effect=snapshot),
              patch.object(goal_codex, 'processes', side_effect=snapshot),
              patch.dict(os.environ, {'CODEX_TEST_SECRET': 'sensitive fixture'})):
            try:
                return app.invoke(model=model, effort='high', sandbox=sandbox,
                                  prompt=prompt, schema=_SCHEMA, workspace=self.workspace,
                                  output_dir=self.output, deadline=time.monotonic() + timeout,
                                  launch_gate=gate or self.gate, read_challenge=read_challenge)
            finally:
                if hasattr(self, 'child'):
                    self.assertIsNotNone(self.child.poll())
                    record = json.loads((self.output / 'process.json').read_text())
                    self.assertEqual((record['state'], record['pid']), ('stopped', self.child.pid))

    def methods(self):
        return self.trace.read_text().splitlines()

    def test_one_ephemeral_turn_with_confirmed_settings_and_redacted_evidence(self):
        result = self.invoke(model='gpt-6.2-astra', sandbox='workspace-write')
        self.assertEqual(result['result'], {'action':'finish'})
        self.assertEqual((result['session'], result['turn']), (_THREAD, _TURN))
        self.assertEqual((result['model'], result['requested_model']), ('gpt-6.2-astra', 'gpt-6.2-astra'))
        self.assertEqual((result['sandbox'], result['effort']), ('workspace-write', 'high'))
        self.assertEqual(result['tools'], [{'id':'command-1','name':'shell','status':'completed',
                                          'settled':True,'paired':True,'exit_code':0,'duration_ms':47}])
        self.assertEqual(result['usage']['total_tokens'], 21)
        self.assertEqual(self.methods(), ['initialize', 'initialized', 'config/read', 'skills/list', 'thread/start', 'turn/start'])
        self.assertEqual(self.entered, 2)

    def test_read_proof_records_only_expected_hash_from_successful_actual_output(self):
        entry = {'path':'draft.html', 'sha256':'b' * 64}
        for mode in ('proved_read','command_only','failed_read'):
            with self.subTest(mode=mode):
                self.output = self.root / mode
                self.output.mkdir()
                evidence = self.invoke(mode, read_challenge={'a' * 64:entry})
                self.assertEqual(evidence['tools'][0].get('candidate_reads'),
                                 [entry] if mode == 'proved_read' else None)
                self.assertNotIn('unredacted_output',json.dumps(evidence))
                self.assertNotIn('workbench-read',json.dumps(evidence))

    def test_settings_mismatch_never_sends_the_model_prompt(self):
        for mode, code, last in (
            ('active_mcp', 'config_mismatch', 'config/read'),
            ('custom_inference_endpoint', 'config_mismatch', 'config/read'),
            ('active_rules', 'config_mismatch', 'config/read'),
            ('active_skill', 'config_mismatch', 'skills/list'),
            ('model_mismatch', 'model_mismatch', 'thread/start'),
            ('sandbox_mismatch', 'config_mismatch', 'thread/start'),
        ):
            with self.subTest(mode=mode), self.assertRaises(app.GoalAppServerError) as raised:
                self.output = self.root / mode
                self.output.mkdir()
                self.invoke(mode)
            self.assertEqual(raised.exception.code, code)
            self.assertEqual(self.methods()[-1], last)
            self.assertNotIn('turn/start', self.methods())

    def test_launch_gate_rechecks_after_thread_confirmation(self):
        @contextlib.contextmanager
        def refuse():
            self.entered += 1
            if self.entered == 2:
                raise RuntimeError('revoked')
            yield

        with self.assertRaisesRegex(RuntimeError, 'revoked'):
            self.invoke(gate=refuse)
        self.assertEqual(self.methods()[-1], 'thread/start')

    def test_unapproved_actions_interrupt_confirmed_turn(self):
        for mode, code in (('reroute', 'model_rerouted'), ('unapproved_tool', 'unapproved_tool'),
                           ('unexpected_request', 'unexpected_request'), ('wrong_turn', 'invalid_protocol')):
            with self.subTest(mode=mode), self.assertRaises(app.GoalAppServerError) as raised:
                self.output = self.root / mode
                self.output.mkdir()
                self.invoke(mode)
            self.assertEqual(raised.exception.code, code)
            self.assertEqual(raised.exception.evidence['turn'], _TURN)
            self.assertEqual(self.methods()[-1], 'turn/interrupt')
            if mode == 'unapproved_tool':
                self.assertEqual(raised.exception.evidence['app_server_error'], {'item_type':'fileChange'})

    def test_terminal_error_or_invalid_schema_cannot_return_a_result(self):
        for mode, code in (('turn_error', 'turn_failed'), ('invalid_result', 'invalid_output')):
            with self.subTest(mode=mode), self.assertRaises(app.GoalAppServerError) as raised:
                self.output = self.root / mode
                self.output.mkdir()
                self.invoke(mode)
            self.assertEqual(raised.exception.code, code)
            self.assertNotIn('sensitive fixture', str(raised.exception))
            self.assertNotIn('sensitive fixture', json.dumps(raised.exception.evidence))
            if mode == 'turn_error':
                self.assertEqual(raised.exception.evidence['app_server_error'], {
                    'type': 'responseStreamConnectionFailed', 'http_status': 401,
                    'categories': ['account', 'network'],
                })
            self.assertEqual(self.methods()[-1], 'turn/start')

    def test_shared_deadline_requests_interrupt_then_stops_process(self):
        with self.assertRaises(app.GoalAppServerError) as raised:
            self.invoke('timeout', timeout=1.2)
        self.assertEqual(raised.exception.code, 'timeout')
        self.assertEqual(self.methods()[-1], 'turn/interrupt')

    def test_signal_requests_interrupt_then_propagates_worker_exit(self):
        with self.assertRaises(SystemExit) as raised:
            self.invoke('signal')
        self.assertEqual(raised.exception.code, 143)
        self.assertEqual(self.methods()[-1], 'turn/interrupt')

    def test_pending_record_is_preserved_and_prevents_restart(self):
        record = self.output / 'process.json'
        record.write_text('previous process record')
        with self.assertRaises(app.GoalAppServerError) as raised:
            self.invoke()
        self.assertEqual(raised.exception.code, 'process_pending')
        self.assertEqual(record.read_text(), 'previous process record')
        self.assertFalse(self.trace.exists())

    def test_unrecognized_diagnostics_do_not_copy_provider_text(self):
        detail = app._diagnose({
            'message': 'provider record containing synthetic account text',
            'code': -32602, 'data': {'type': 'unknown fixture text', 'httpStatusCode': 503},
        }, b'')
        self.assertEqual(detail, {'rpc_code': -32602, 'http_status': 503, 'categories': ['capacity']})


if __name__ == '__main__':
    unittest.main()
