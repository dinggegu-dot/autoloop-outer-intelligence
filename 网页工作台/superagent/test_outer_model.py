"""通过本地 CLI 协议替身测试外层模型的受控启动。"""
import contextlib
import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from superagent.codex_worker import _object
from superagent.coordination_tools import DESCRIPTIONS, SERVER, _handle, _read, serve
from superagent.outer_model import CodexOuterModel, ModelFailure, ACCEPTANCE_SCHEMA, TXT, role_invoke


def local_executable(directory, program):
    executable = Path(directory, 'codex-fixture')
    executable.write_text(f'#!{sys.executable}\n{program}', encoding='utf-8')
    executable.chmod(0o700)
    return str(executable)


class OuterModelTest(unittest.TestCase):
    def test_mcp_tool_rejects_cross_ticket_selectors(self):
        name = next(iter(DESCRIPTIONS))
        for arguments in ({'ticket_id': 'other-ticket'}, {'path': 'other-file'}, {'revision': 6}):
            request = {'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}}
            with patch('superagent.coordination_tools._read') as read:
                self.assertTrue(_handle(request, '/unused')['isError'])
                read.assert_not_called()

    @unittest.skipUnless(os.environ.get('GOAL_CHECKS_REAL_SEATBELT') == '1', 'requires a local Unix socket')
    def test_evidence_read_rechecks_live_gate(self):
        context = {name: {'version': 1} for name in DESCRIPTIONS}
        name = next(iter(context))
        allowed = True

        @contextlib.contextmanager
        def gate():
            if not allowed:
                raise PermissionError('scope changed')
            yield

        with serve(context, gate) as (endpoint, reads):
            self.assertEqual(json.loads(_read(endpoint, name)), context[name])
            allowed = False
            with self.assertRaisesRegex(ValueError, 'evidence unavailable'):
                _read(endpoint, name)
            self.assertEqual(set(reads), {name})

    def test_scoped_prompt_projects_evidence_and_checks_for_actual_calls(self):
        context = {
            'coordinator_profile': 'scoped-v1', 'ticket_id': 'ticket-a',
            'verification_checks': ['FAILURE-INLINE-MARKER'],
            'tool_evidence': {name: 'TOOL-ONLY-MARKER' for name in DESCRIPTIONS},
        }
        adapter = CodexOuterModel().with_execution(Path('/tmp'), contextlib.nullcontext)
        with patch.object(adapter, '_execute', side_effect=ModelFailure('probe')) as mock:
            with self.assertRaisesRegex(ModelFailure, 'probe') as error:
                role_invoke(adapter, 'goal_coordinator', context, _object(action=TXT))
        prompt, _, _, _, evidence, _ = mock.call_args.args
        self.assertIn('ticket-a', prompt)
        self.assertNotIn('FAILURE-INLINE-MARKER', prompt)
        self.assertNotIn('TOOL-ONLY-MARKER', prompt)
        self.assertEqual(evidence, context['tool_evidence'])
        self.assertEqual(error.exception.receipt()['prompt_version'], 7)

    def test_native_event_alone_cannot_claim_a_tool_read(self):
        from superagent.outer_model import _completed_tools

        name = next(iter(DESCRIPTIONS))
        recorded = hashlib.sha256(b'{}').hexdigest()
        item = {'type': 'mcp_tool_call', 'tool': name, 'server': SERVER, 'status': 'completed',
                'result': {'content': [{'type': 'text', 'text': '{}'}]}, 'error': None}
        event = {'type': 'item.completed', 'item': item}
        def wire():
            return json.dumps(event).encode()
        self.assertEqual(_completed_tools(wire(), {}), [])
        self.assertEqual(_completed_tools(wire(), {name: recorded}),
                         [{'name': name, 'status': 'completed', 'evidence_sha256': recorded}])
        self.assertEqual(_completed_tools(wire(), {name: 'wrong-response-digest'}), [])
        item['server'] = 'other'
        self.assertEqual(_completed_tools(wire(), {name: recorded}), [])
        item.update(server=SERVER, status='failed')
        self.assertEqual(_completed_tools(wire(), {name: recorded}), [])

    def test_cli_proxy_requires_an_explicit_loopback_port(self):
        adapter = CodexOuterModel()
        with patch.object(adapter,'_run_threaded',side_effect=ModelFailure('probe')) as launch:
            for port, expected in [('',None),('8071','http://127.0.0.1:8071')]:
                with patch.dict(os.environ,{'AUTOLOOP_OUTER_PROXY_PORT':port,'HTTPS_PROXY':'http://unrelated.invalid'}):
                    with self.assertRaisesRegex(ModelFailure,'probe'):
                        adapter._execute('',{},'low',lambda message: None)
                self.assertEqual(launch.call_args.args[-1].get('HTTPS_PROXY'),expected)
            with patch.dict(os.environ,{'AUTOLOOP_OUTER_PROXY_PORT':'remote:3128'}):
                with self.assertRaisesRegex(ModelFailure,'invalid_config'):
                    adapter._execute('',{},'low',lambda message: None)
            self.assertEqual(launch.call_count,2)

    def test_structured_cli_sends_minimal_context_and_disables_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            mocked = local_executable(directory, """
import json, os, sys
argv = sys.argv[1:]
assert argv[argv.index('--sandbox')+1] == 'read-only'
assert 'mcp_servers={}' in argv
for tool in ('shell_tool', 'unified_exec', 'apps', 'plugins', 'browser_use', 'multi_agent'):
    assert any(a == '--disable' and b == tool for a,b in zip(argv, argv[1:]))
assert 'GOAL-CONTEXT-MARKER' in sys.stdin.read()
assert set(os.listdir(os.getcwd())).issubset({'schema.json', 'process.json'})
print(json.dumps({'type':'thread.started','thread_id':'668d0690-bb28-4dd5-9901-56f0a21b2afb'}))
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'{"action":"REPAIR"}'}}))
print(json.dumps({'type':'turn.completed','usage': {'input_tokens':10,'output_tokens':2}}))
""")
            adapter = CodexOuterModel(executable=mocked)
            schema = _object(action=TXT)
            context = {'only_ref': 'GOAL-CONTEXT-MARKER'}
            result = role_invoke(adapter, 'goal_coordinator', context, schema)
            self.assertEqual(result['result'], {'action': 'REPAIR'})
            self.assertEqual(result['usage']['output_tokens'], 2)
            self.assertEqual(result['tools'], [])
            self.assertTrue(result['request']['prompt'].endswith(json.dumps(context,ensure_ascii=False)))
            self.assertEqual(result['request']['output_schema'],schema)
            self.assertEqual(json.loads(result['final_message']),result['result'])
            with self.assertRaisesRegex(ModelFailure, 'invalid_output') as rejected:
                role_invoke(adapter,'goal_coordinator',context,_object(other=TXT))
            self.assertEqual(json.loads(rejected.exception.receipt()['final_message']),result['result'])
            allow = []

            @contextlib.contextmanager
            def launch_gate():
                allow.append(True)
                yield

            def guarded_runner(argv, prompt, workspace, output_dir, staging, env, timeout, gate):
                self.assertEqual(output_dir, Path(directory))
                self.assertEqual(staging.parent, Path(directory))
                self.assertTrue(workspace.is_dir())
                with gate():
                    stdout, stderr, code = adapter._run_threaded(argv, prompt.decode(), str(workspace), env)
                return stdout, stderr, code, None, None

            gated = adapter.with_execution(Path(directory), launch_gate)
            with patch('superagent.goal_codex._run', side_effect=guarded_runner):
                self.assertEqual(role_invoke(gated, 'goal_coordinator', context, schema)['result'], result['result'])
            self.assertEqual(allow, [True])

    def test_per_ticket_schema_keeps_event_requirement_and_free_text_lists_independent(self):
        original = deepcopy(ACCEPTANCE_SCHEMA)
        goal = {'deliverables':[{'requirement_id':'req-one'}]}
        context = {'goal_contract':goal, 'relevant_thread_refs':['evt-one']}

        def inspect(role, snapshot, schema):
            top = schema['properties']
            fields = top['criteria']['items']['properties']
            self.assertEqual(fields['requirement_ids']['items']['enum'], ['req-one'])
            self.assertEqual(fields['source_refs']['items']['enum'], ['evt-one'])
            self.assertNotIn('enum', fields['evidence_required']['items'])
            self.assertNotIn('enum', top['artifact_types']['items'])
            raise ModelFailure('inspected')

        with self.assertRaisesRegex(ModelFailure, 'inspected'):
            role_invoke(inspect, 'acceptance_designer', context, ACCEPTANCE_SCHEMA)
        self.assertEqual(ACCEPTANCE_SCHEMA, original)

    def test_invalid_fixture_and_timeout_have_fixed_failure_category(self):
        with tempfile.TemporaryDirectory() as directory:
            mocked = local_executable(directory, 'print("incomplete")')
            adapter = CodexOuterModel(executable=mocked)
            with self.assertRaisesRegex(ModelFailure, 'invalid_output'):
                role_invoke(adapter, 'goal_coordinator', {}, _object(action=TXT))
            mocked = local_executable(directory, 'import time\ntime.sleep(10)')
            adapter = CodexOuterModel(executable=mocked, timeout=1)
            with self.assertRaisesRegex(ModelFailure, 'timeout') as caught:
                role_invoke(adapter, 'goal_coordinator', {}, _object(action=TXT))
            receipt = caught.exception.receipt()
            self.assertGreaterEqual(receipt['elapsed_ms'], 900)
            self.assertIn('request', receipt)
            self.assertNotIn('final_message', receipt)


if __name__ == '__main__':
    unittest.main()
