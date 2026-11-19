"""Regression checks for approval, resume and dispatch safety."""
import json
import tempfile
import sqlite3
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock

from task_runner import Runner
from workbench import RULE, Problem, Store, Workbench


class WorkbenchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name).resolve())
        self.hub = Mock()
        self.hub.state.return_value = {
            'lines':[{'id':RULE,'paused':None}],
            'agents':[{'id':'odyssey-runner','online':True}],
        }
        self.hub.records.return_value = ([], [])
        self.workbench = Workbench(self.store, self.hub)
        self.body = dict(name='检查', goal='生成文件', max_calls=4, request_id=str(uuid.uuid4()))
        self.task, _ = self.store.create(self.body)
        self.id = self.task['id']

    def test_note_correction_preserves_origin_and_plan_boundary(self):
        other,_=self.store.create(dict(self.body,request_id=str(uuid.uuid4())))
        target=other['id']
        mid='om_source'
        self.store.add_note(self.id,mid,'私聊资料')
        move=dict(target=target,message=mid,revision=0,request_id=str(uuid.uuid4()))
        self.store.move_note(self.id,move)
        self.store.move_note(self.id,move)
        self.store.add_note(self.id,mid,'私聊资料')
        self.assertEqual(self.store.notes(self.id),[])
        note=self.store.notes(target,for_board=True)[0]
        self.assertEqual((note['source'],note['revision']),('feishu',1))
        self.assertEqual(set(self.store.notes(target)[0]),{'at','body'})
        stale=dict(move,target=self.id,request_id=str(uuid.uuid4()))
        with self.assertRaises(Problem):self.store.move_note(target,stale)
        self.store.outbox(self.id,str(uuid.uuid4()))
        with self.assertRaises(Problem):self.store.move_note(target,dict(stale,revision=1))
        request=dict(text='电脑补充',request_id=str(uuid.uuid4()))
        self.store.board_note(target,request)
        self.store.board_note(target,request)
        self.assertEqual([n['source'] for n in self.store.notes(target,True)],['feishu','board'])
        with self.assertRaises(Problem):self.store.board_note(target,dict(request,text='替换原文'))

    def test_ledger_outage_does_not_report_stopped_or_current(self):
        self.store.change(self.id, lambda task: task.update(paused=True, status='stopping'))
        self.hub.records.side_effect = sqlite3.OperationalError('unavailable')
        snapshot = self.workbench.snapshot()
        task = snapshot['tasks'][0]
        self.assertFalse(snapshot['capability']['ledger_available'])
        self.assertEqual(task['status'], 'stopping')
        self.assertTrue(task['observation_stale'])
        self.assertIn('状态待核对', task['status_label'])

    def test_hub_outage_preserves_stopping_state(self):
        self.store.change(self.id, lambda task: task.update(paused=True, status='stopping'))
        self.hub.state.side_effect = Problem('offline', 503)
        snapshot = self.workbench.snapshot()
        self.assertTrue(snapshot['capability']['ledger_available'])
        self.assertFalse(snapshot['capability']['hub_available'])
        self.assertEqual(snapshot['tasks'][0]['status'], 'stopping')
        self.assertIn('正在停止', snapshot['tasks'][0]['status_label'])

    def test_older_unresolved_run_keeps_snapshot_uncertain(self):
        recent=[{'id':i,'lane':self.id,'state':'done'} for i in range(25,0,-1)]
        self.hub.records.return_value=(recent+[{'id':0,'lane':self.id,'state':'unknown'}],[])
        task=self.workbench.snapshot()['tasks'][0]
        self.assertTrue(task['observation_stale'])
        self.assertEqual(len(task['runs']),20)

    def test_coordinate_and_unknown_phase_keep_timeline_readable(self):
        from superagent.goal_engine import GoalEngine, decision
        goal,_ = GoalEngine(self.store).create(dict(self.body,request_id=str(uuid.uuid4())))
        def add_phases(task):
            decision(task,'dispatch_prepared',phase='coordinate')
            decision(task,'coordinated')
            decision(task,'dispatch_prepared',phase='new_phase')
        self.store.change(goal['id'],add_phases)
        row = next(t for t in self.workbench.snapshot(goal['id'])['tasks'] if t['id']==goal['id'])
        self.assertEqual([event['title'] for event in row['events'][-3:]],
                         ['待派工 · 目标协调','协调决定已记录','待派工 · new_phase'])

    def test_claimed_run_without_model_does_not_show_execution(self):
        self.hub.records.return_value=([{'id':1,'lane':self.id,'state':'claimed'}],[])
        snapshot=self.workbench.snapshot()['tasks'][0]
        self.assertEqual(snapshot['status'],'queued')
        self.assertEqual(snapshot['phase'],'plan')
        self.assertEqual(snapshot['calls'],0)

    def test_claimed_run_preserves_persisted_approval_and_completion(self):
        self.hub.records.return_value=([{'id':1,'lane':self.id,'state':'claimed'}],[])
        for status in ('awaiting_approval','completed','blocked'):
            self.store.change(self.id,lambda task:task.update(status=status))
            self.assertEqual(self.workbench.snapshot()['tasks'][0]['status'],status)

    def test_retry_reuses_source_event_after_unknown_response(self):
        self.hub.request.side_effect = [
            Problem('响应未确认', 503), {'routed':[RULE],'event':1},
        ]
        first = self.workbench.create(self.body)
        second = self.workbench.create(self.body)
        self.assertTrue(first['dispatch_error'])
        self.assertEqual(second['task']['status'], 'queued')
        self.assertEqual(self.hub.request.call_args_list[0], self.hub.request.call_args_list[1])
        self.assertEqual(len(self.store.list()), 1)

    def test_invalid_resume_does_not_unpause(self):
        self.store.change(self.id, lambda task:task.update(paused=True))
        with self.assertRaises(Problem):
            self.workbench.resume(self.id, {'request_id':'invalid','max_calls':6})
        self.assertTrue(self.store.get(self.id)['paused'])
        self.assertEqual(self.store.get(self.id)['max_calls'], 4)

    def test_decision_requires_current_card_version(self):
        self.store.change(self.id, lambda task:task.update(approval={'id':'current'}))
        self.hub.records.return_value = ([], [{
            'id':1, 'lane':self.id, 'version':3, 'state':'open', 'choice':None,
            'binding':{'task':self.id,'version':1,'approval':'current'},'options':['继续','拒绝']
        }])
        with self.assertRaises(Problem) as error:
            self.workbench.answer(self.id, {'card':1,'version':2,'choice':0})
        self.assertEqual(error.exception.status, 409)
        self.hub.request.assert_not_called()

    def test_missing_plan_approval_stops_execution_after_restart(self):
        self.store.change(self.id, lambda t:t.update(
            plan={'summary':'待确认','scope':'本工作区','steps':[]}, phase='execute'
        ))
        invoke = Mock()
        result = Runner(self.store, self.id, 10, {}, invoke=invoke).run()
        self.assertIn('ask', result['receipt'])
        invoke.assert_not_called()

    def test_refusal_is_consumed_without_model(self):
        invoke = Mock()
        runner = Runner(self.store, self.id, 11, {}, invoke=invoke)
        ask = runner.ask('plan','决定','本任务', ['继续','拒绝'],'execute')
        binding = ask['receipt']['ask'][0]['about']
        runner.context = {'answers':[{'card':5,'about':binding,'choice':1}]}
        runner.run()
        self.assertEqual(self.store.get(self.id)['status'], 'rejected')
        invoke.assert_not_called()


if __name__ == '__main__':
    unittest.main()
