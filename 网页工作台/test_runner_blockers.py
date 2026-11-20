"""回归测试覆盖正式 Runner 的审批与补证恢复。"""
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock

from task_runner import Runner
from workbench import RULE, Problem, Store, Workbench


class RunnerBlockerTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name).resolve())
        task, _ = self.store.create(dict(name='证据验收', goal='核对真实证据',
                                        max_calls=6, request_id=str(uuid.uuid4())))
        self.id = task['id']
        self.workspace = self.store.workspace(self.id)
        self.store.change(self.id, lambda t: t.update(
            plan={'summary':'核验证据','scope':'本工作区','steps':[{'state':'pending'}]},
            status='queued', phase='execute', decisions=[{'kind':'plan','version':1,'approved':True}],
        ))
        self.result = dict(status='blocked', summary='缺执行方原始日志', checks=['核对已有交付'],
                           artifacts=['README.md','../outside'], question='',
                           requested_action='研究执行器补交原始日志')
        (self.workspace/'README.md').write_text('当前证据')

    def run_role(self, number, invoke, context=None):
        return Runner(self.store, self.id, number, context or {}, invoke=invoke).run()

    def test_execution_blocks_without_card_and_resumes_after_new_notes(self):
        invoke = Mock(return_value=self.result)
        first = self.run_role(1, invoke)
        blocked = self.store.get(self.id)
        self.assertEqual(first['receipt'], {})
        self.assertEqual(blocked['status'], 'blocked')
        self.assertEqual(blocked['plan']['steps'][0]['state'], 'blocked')
        self.assertEqual(blocked['decisions'], [{'kind':'plan','version':1,'approved':True}])
        self.assertEqual(blocked['blocker']['checks'], ['核对已有交付'])
        self.assertEqual(blocked['blocker']['artifacts'], ['README.md'])
        self.assertEqual(blocked['blocker']['requested_action'], self.result['requested_action'])
        self.assertEqual(self.run_role(1, invoke), first)
        self.assertEqual(self.run_role(2, invoke)['receipt'], {})
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(sum(event['kind']=='blocked' for event in self.store.events(self.id)), 1)
        self.store.board_note(self.id, {'request_id':str(uuid.uuid4()),'text':'待核对的新说明'})
        self.run_role(3, invoke)
        self.assertEqual(invoke.call_count, 2)
        self.assertIn('待核对的新说明', invoke.call_args.args[1])

    def test_review_waits_for_new_workspace_evidence(self):
        self.store.change(self.id, lambda t: t.update(phase='review', step=1))
        invoke = Mock(return_value=self.result)
        self.assertEqual(self.run_role(1, invoke)['receipt'], {})
        self.assertEqual(self.store.get(self.id)['review'], self.result)
        self.assertEqual(self.store.get(self.id)['blocker']['phase'], 'review')
        self.run_role(2, invoke)
        self.assertEqual(invoke.call_count, 1)
        (self.workspace/'raw.log').write_text('新交接')
        self.run_role(3, invoke)
        self.assertEqual(invoke.call_count, 2)
        self.assertEqual(invoke.call_args.args[0], 'review')

    def test_real_review_approval_is_bound_to_version_and_consumed(self):
        self.store.change(self.id, lambda t: t.update(phase='review', step=1))
        action = '授权使用指定验证渠道'
        invoke = Mock(return_value={**self.result, 'status':'needs_approval', 'requested_action':action})
        ask = self.run_role(1, invoke)['receipt']['ask'][0]
        self.assertEqual(ask['text'], action)
        self.assertEqual(self.store.get(self.id)['approval']['kind'], 'review')
        result = self.run_role(2, invoke, {'answers':[{'card':17,'choice':0,'about':ask['about']}]})
        self.assertEqual(result['receipt'], {})
        task = self.store.get(self.id)
        self.assertEqual(task['status'], 'blocked')
        self.assertIn('同版本已有该项批准', task['blocker']['reason'])
        self.assertEqual(task['decisions'][-1]['card'], 17)
        self.assertEqual(len(task['decisions']), 2)

    def test_legacy_same_version_approval_deduplicates_exact_action(self):
        action = '核对研究执行器已交接的记录'
        self.store.change(self.id, lambda t: t['decisions'].append(
            {'kind':'action','version':1,'choice':'已完成或同意所述条件，继续','action':action}
        ))
        invoke = Mock(return_value={**self.result, 'status':'needs_approval','requested_action':action+'。'})
        self.assertEqual(self.run_role(1, invoke)['receipt'], {})
        (self.workspace/'new.log').write_text('新交接')
        invoke.return_value = {**invoke.return_value,'requested_action':'另行授权新增检查范围'}
        self.assertIn('ask', self.run_role(2, invoke)['receipt'])

    def test_approval_without_specific_action_records_blocker(self):
        invoke = Mock(return_value={**self.result,'status':'needs_approval','requested_action':''})
        self.assertEqual(self.run_role(1, invoke)['receipt'], {})
        self.assertIn('未说明',self.store.get(self.id)['blocker']['reason'])

    def test_resume_can_add_exactly_fifteen_minutes_once(self):
        self.store.change(self.id, lambda t: t.update(time_budget_seconds=900,paused=True))
        hub = Mock()
        hub.records.return_value = ([], [])
        hub.state.return_value = {'lines':[{'id':RULE}],'agents':[{'id':'odyssey-runner','online':True}]}
        hub.request.return_value = {'routed':[RULE],'event':1}
        board = Workbench(self.store, hub)
        body = {'request_id':str(uuid.uuid4()),'additional_minutes':15}
        self.assertTrue(board.resume(self.id,body)['queued'])
        self.assertTrue(board.resume(self.id,body)['queued'])
        self.assertEqual(self.store.get(self.id)['time_budget_seconds'],1800)
        self.assertEqual(hub.request.call_count,1)

    def test_resume_avoids_dispatch_and_budget_change_until_new_inputs(self):
        invoke = Mock(return_value=self.result)
        self.run_role(1, invoke)
        hub = Mock()
        hub.records.return_value = ([], [])
        hub.state.return_value = {'lines':[{'id':RULE}],'agents':[{'id':'odyssey-runner','online':True}]}
        hub.request.return_value = {'routed':[RULE],'event':1}
        board = Workbench(self.store, hub)
        body = {'request_id':str(uuid.uuid4()),'additional_minutes':15}
        budget = self.store.get(self.id)['time_budget_seconds']
        with self.assertRaisesRegex(Problem,'执行条件仍未改变'):
            board.resume(self.id,body)
        hub.request.assert_not_called()
        self.assertEqual(self.store.get(self.id)['time_budget_seconds'],budget)
        self.store.board_note(self.id, {'request_id':str(uuid.uuid4()),'text':'补证已交接'})
        self.assertTrue(board.resume(self.id,body)['queued'])
        hub.request.assert_called_once()


if __name__ == '__main__':
    unittest.main()
