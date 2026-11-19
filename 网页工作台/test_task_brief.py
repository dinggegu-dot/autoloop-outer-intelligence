"""用隔离工作台快照验证手机回执；不连接真实中台或飞书。"""
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock

from task_brief import milestone,task_brief
from workbench import RULE, Store, Workbench


class DualEntryBriefTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name).resolve())
        self.hub = Mock()
        self.hub.state.return_value = {'lines': [{'id': RULE, 'paused': None}],
                                       'agents': [{'id': 'odyssey-runner', 'online': True}]}
        self.hub.records.return_value = ([], [])
        self.board = Workbench(self.store, self.hub)
        task, _ = self.store.create({'name': '索引样例', 'goal': 'PRIVATE-MATERIAL',
                                     'request_id': str(uuid.uuid4())})
        self.task_id = task['id']

    def brief(self):
        return task_brief(self.board.snapshot()['tasks'][0])

    def test_milestones_hide_reasons_and_ignore_unknown_state(self):
        self.store.change(self.task_id,lambda task:task.update(status='blocked',error='PRIVATE-PATH'))
        signal=milestone(self.board.snapshot()['tasks'][0])
        self.assertTrue(signal['known'])
        self.assertNotIn('PRIVATE-PATH',str(signal))
        self.hub.records.side_effect=sqlite3.OperationalError('offline')
        self.assertEqual(milestone(self.board.snapshot()['tasks'][0]),{'known':False})

    def test_verified_result_only_discloses_task_progress(self):
        self.store.change(self.task_id, lambda task: task.update(
            status='completed', version=2, plan={'steps': [{'state': 'completed'}]}))
        brief = self.brief()
        self.assertEqual((brief.task_id, brief.version, brief.status, brief.progress),
                         (self.task_id, 2, '已验收完成', '已完成步骤 1/1'))
        self.assertNotIn('PRIVATE-MATERIAL', brief.as_private_reply())
        self.assertNotIn(str(self.store.root), brief.as_private_reply())
        self.hub.request.assert_not_called()

    def test_disconnected_ledger_hides_last_completion(self):
        self.store.change(self.task_id, lambda task: task.update(status='completed'))
        self.hub.records.side_effect = sqlite3.OperationalError('offline')
        brief = self.brief()
        self.assertEqual(brief.status, '状态待核对')
        self.assertNotIn('已验收', brief.as_private_reply())

    def test_unconfirmed_run_is_not_reported_as_current(self):
        self.hub.records.return_value = ([{'id': 1, 'lane': self.task_id, 'state': 'unknown'}], [])
        self.assertEqual(self.brief().progress, '当前进度未知')

    def test_only_current_approval_can_request_attention(self):
        self.store.change(self.task_id, lambda task: task.update(
            status='awaiting_approval', version=2, approval={'id': 'current'}))
        card = {'lane': self.task_id, 'state': 'open',
                'binding': {'task': self.task_id, 'version': 1, 'approval': 'old'}}
        self.hub.records.return_value = ([], [card])
        self.assertIn('尚未查到', self.brief().next_action)
        card['binding'].update(version=2, approval='current')
        self.assertEqual(self.brief().next_action, '需本人处理本版待办')
