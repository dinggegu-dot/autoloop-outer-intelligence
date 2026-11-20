import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock
import uuid

from superagent.goal_engine import current
from superagent.goal_runtime import GoalRuntime
from workbench import Problem, Store


class GoalTransportTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name).resolve())
        self.hub = Mock()
        self.runtime = GoalRuntime(self.store,self.hub)
        self.engine = self.runtime.engine
        task,_ = self.engine.create({'name':'传输测试','goal':'离线夹具',
                                     'request_id':str(uuid.uuid4())})
        self.ticket = task['id']
        self.assignment = current(self.engine.prepare(self.ticket))
        self.key = self.assignment['id']

    def stop_after_send(self):
        self.engine.mark_send(self.ticket,self.key)
        self.engine.stop(self.ticket)
        self.hub.existing_event.return_value = 12

    def test_stopping_reconciles_known_unrouted_event_without_charge(self):
        self.stop_after_send()
        self.hub.request.return_value = {
            'event':12,'routed':[],'paused':['odyssey-goal'],'runs':[],
        }
        result = self.runtime.tick(self.ticket)
        self.assertEqual(result['status'],'paused')
        self.assertIsNone(current(result))
        self.assertEqual(result['elapsed_seconds'],0)
        self.assertEqual(result['calls'],0)
        self.hub.request.assert_called_once_with('GET','/v1/events/12',source=True)

    def test_unknown_or_routed_without_run_stays_stopping(self):
        self.stop_after_send()
        for extra in ({}, {'routed':['odyssey-goal']}):
            with self.subTest(extra=extra):
                self.hub.request.return_value = {'event':12,'runs':[],**extra}
                result = self.runtime.tick(self.ticket)
                self.assertEqual(result['status'],'stopping')
                self.assertEqual(current(result)['transport'],'unknown')
                self.assertEqual(result['elapsed_seconds'],0)
        self.assertTrue(all(call.args[0]=='GET' for call in self.hub.request.call_args_list))

    def test_wrong_event_does_not_release_booking(self):
        self.stop_after_send()
        self.hub.request.return_value = {'event':13,'routed':[],'runs':[]}
        with self.assertRaisesRegex(Problem,'不匹配'):
            self.runtime.tick(self.ticket)
        self.assertEqual(self.store.get(self.ticket)['status'],'stopping')
        self.assertIsNotNone(current(self.store.get(self.ticket)))

    def test_missing_receipt_records_bounded_metadata_without_raw_error(self):
        self.engine.bind(self.ticket,self.key,12,31)
        self.engine.stop(self.ticket)
        self.hub.request.return_value = {
            'event':12,'routed':['odyssey-goal'],
            'runs':[{'run':31,'rule':'odyssey-goal','state':'cancelled',
                     'attempts':10**20,'error':'timeout Bearer fixture-secret /private/path\n' * 30}],
        }
        result = self.runtime.tick(self.ticket)
        self.assertEqual(result['status'],'paused')
        self.assertEqual(result['elapsed_seconds'],self.assignment['max_seconds'])
        events = [row for row in self.store.events(self.ticket) if row['kind']=='goal_transport']
        self.assertEqual(len(events),1)
        detail = events[0]['detail']
        self.assertEqual(detail['reason'],'timeout')
        self.assertEqual(detail['reserved_seconds'],self.assignment['max_seconds'])
        self.assertEqual(detail['hub_attempts'],1000)
        self.assertNotIn('fixture-secret',json.dumps(detail))
        self.assertNotIn('/private/path',json.dumps(detail))
        self.runtime.tick(self.ticket)
        self.assertEqual(len([row for row in self.store.events(self.ticket)
                              if row['kind']=='goal_transport']),1)


if __name__ == '__main__':
    unittest.main()
