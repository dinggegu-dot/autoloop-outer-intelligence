"""通过本地 CLI 协议替身测试外层模型的受控启动。"""
import contextlib
import json
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from superagent.codex_worker import _object
from superagent.outer_model import CodexOuterModel, ModelFailure, ACCEPTANCE_SCHEMA, TXT, role_invoke


def local_executable(directory, program):
    executable = Path(directory, 'codex-fixture')
    executable.write_text(f'#!{sys.executable}\n{program}', encoding='utf-8')
    executable.chmod(0o700)
    return str(executable)


class OuterModelTest(unittest.TestCase):
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
