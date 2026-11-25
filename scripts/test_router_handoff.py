import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from formal_outer_canary import valid_command
from formal_outer_canary_supervisor import Supervisor


class RouterHandoffTest(unittest.TestCase):
    def test_consent_and_intents_bind_to_a_version(self):
        action = dict(ticket_id='wb-' + 'a' * 32, revision=1, request_id=str(uuid4()), allow_model_input=True)
        self.assertTrue(valid_command('authorize_model_input', action))
        self.assertFalse(valid_command('authorize_model_input', dict(action, allow_model_input=False)))
        self.assertFalse(valid_command('authorize_model_input', dict(action, revision=0)))
        selection = dict(event_id='evt-' + 'b'*32, ticket_id=None, request_id=str(uuid4()))
        self.assertTrue(valid_command('select', selection))
        self.assertTrue(valid_command('select', dict(selection, ticket_id=action['ticket_id'])))
        self.assertFalse(valid_command('select', dict(selection, ticket_id='foreign')))
        withdraw = dict(action, intent='change', text='撤回推断', withdraw_requirement_ids=['req-'+'b'*32])
        self.assertTrue(valid_command('submit', withdraw))
        self.assertFalse(valid_command('submit', dict(withdraw, intent='supplement')))
        self.assertFalse(valid_command('submit', dict(withdraw, replace_requirement_ids=['req-'+'a'*32])))
        for intent in ('change', 'supplement'):
            self.assertTrue(valid_command('submit', dict(action, intent=intent, text='合成补充')))
        self.assertFalse(valid_command('submit', dict(action, intent='supplement', text='资料',
                                                      question_id='q-' + 'c' * 32)))
        self.assertFalse(valid_command('submit', dict(action, intent='other', text='资料')))

    def test_parallel_runtime_preserves_prior_config_and_reuses_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            supervisor = Supervisor.__new__(Supervisor)
            supervisor.runtime = state
            supervisor.goal = state / 'superagent/goal_runtime.py'
            old = state / 'worker/config.json'
            old.parent.mkdir(mode=0o700)
            old.write_text(json.dumps({'worker': {'name': 'local', 'key': 'synthetic', 'capacity': 1}}))
            old.chmod(0o600)
            baseline = old.read_bytes()
            runner = supervisor.team_runtime()
            rules = json.loads((runner / 'rules.json').read_text())['rules']
            self.assertEqual(rules[0]['parallel'], 2)
            self.assertEqual(json.loads((runner / 'config.json').read_text())['worker']['capacity'], 2)
            record = runner / 'record'
            record.write_text('existing run')
            self.assertEqual(supervisor.team_runtime(), runner)
            self.assertTrue(record.exists())
            self.assertEqual(old.read_bytes(), baseline)
            self.assertEqual(supervisor.rules()['rules'][0]['parallel'], 1)
