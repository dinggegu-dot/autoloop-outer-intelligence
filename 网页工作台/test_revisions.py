import json
import io
import os
import task_runner
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock,patch
from agent_catalog import DEFAULT_ROLES,role_model
from task_runner import Runner
from workbench import Store,Workbench,Problem,RULE

class RevisionTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name).resolve());self.task,_=self.store.create({'name':'迭代测试','goal':'交付报告','request_id':str(uuid.uuid4())});self.id=self.task['id']
        self.hub=Mock();self.hub.records.return_value=([],[]);self.hub.state.return_value={'lines':[{'id':RULE}],'agents':[{'id':'odyssey-runner','online':True}]};self.hub.request.return_value={'routed':[RULE],'event':1}
        self.wb=Workbench(self.store,self.hub)
        self.body={'request_id':str(uuid.uuid4()),'version':1,'text':'保留已完成内容，增加清晰图表','artifact':'report.md','role_models':DEFAULT_ROLES.copy()}
        (self.store.workspace(self.id)/'report.md').write_text('第一版原件')
        self.store.change(self.id,lambda t:t.update(status='completed',calls=7,elapsed_seconds=20,artifacts=['report.md'],plan={'summary':'旧方案','steps':[]},review={'status':'completed','summary':'通过'},decisions=[{'version':1,'kind':'plan','approved':True}]))

    def test_promote_archives_actual_content_and_preserves_budget(self):
        self.wb.revise(self.id,self.body);self.wb.reconcile()
        t=self.store.get(self.id)
        self.assertEqual(t['version'],2);self.assertEqual(t['phase'],'plan');self.assertIsNone(t['approval']);self.assertIsNone(t['review']);self.assertEqual(t['calls'],7);self.assertEqual(t['elapsed_seconds'],20);self.assertEqual(t['decisions'][0]['version'],1)
        self.assertEqual(t['role_models']['execute'],DEFAULT_ROLES['execute'])
        (self.store.workspace(self.id)/'report.md').write_text('第二版内容')
        self.assertEqual(self.wb.artifact(self.id,'report.md',1).read_text(),'第一版原件')
        self.assertEqual(self.wb.preview(self.id,'report.md',1)['content'],'第一版原件')
        with self.assertRaises(Problem):self.wb.artifact(self.id,'report.md')
        event=json.loads(self.store.outbox(self.id,self.body['request_id'])['event']);self.assertEqual(json.loads(event['detail'])['version'],2)

    def test_running_and_unknown_runs_do_not_promote(self):
        for state in ('claimed','unknown','queued'):
            self.hub.records.return_value=([{'id':1,'lane':self.id,'state':state}],[])
            self.wb.revise(self.id,self.body);self.wb.reconcile();self.assertEqual(self.store.get(self.id)['version'],1)
        self.assertTrue(self.store.get(self.id)['paused'])
        self.hub.records.return_value=([],[]);self.wb.reconcile();self.assertEqual(self.store.get(self.id)['version'],2)

    def test_cancel_error_retains_request_until_explicit_retry(self):
        self.hub.records.return_value=([{'id':1,'lane':self.id,'state':'claimed'}],[])
        self.hub.request.side_effect=Problem('断线',503);self.wb.revise(self.id,self.body)
        self.hub.records.return_value=([],[]);self.wb.reconcile();self.assertEqual(self.store.get(self.id)['version'],1)
        self.hub.request.side_effect=None;self.wb.revise(self.id,self.body);self.wb.reconcile();self.assertEqual(self.store.get(self.id)['version'],2)

    def test_revision_idempotency_and_conflict(self):
        self.wb.revise(self.id,self.body);self.wb.reconcile();self.wb.revise(self.id,self.body);self.wb.reconcile()
        self.assertEqual(self.store.get(self.id)['version'],2)
        with self.assertRaises(Problem):self.wb.revise(self.id,{**self.body,'text':'不同内容'})
        with self.assertRaises(Problem):self.wb.revise(self.id,{**self.body,'request_id':str(uuid.uuid4())})

    def test_lease_and_archive_error_block_revision(self):
        self.wb.revise(self.id,self.body)
        with self.store.lease(self.id):self.wb.reconcile()
        self.assertEqual(self.store.get(self.id)['version'],1)
        with patch('revisions.shutil.copyfile',side_effect=OSError('磁盘不可写')):self.wb.reconcile()
        self.assertEqual(self.store.get(self.id)['version'],1);self.assertTrue(self.store.get(self.id)['paused'])

    def test_restart_recovers_pending_request_without_browser(self):
        self.wb.revise(self.id,self.body)
        restarted=Workbench(Store(self.store.root),self.hub);restarted.reconcile()
        self.assertEqual(restarted.store.get(self.id)['version'],2)

    def test_dispatch_unknown_retry_does_not_cancel_new_worker(self):
        self.wb.revise(self.id,self.body);self.hub.request.side_effect=Problem('响应未确认',503);self.wb.reconcile()
        self.hub.records.return_value=([{'id':9,'lane':self.id,'state':'claimed'}],[])
        self.hub.request.reset_mock();self.wb.revise(self.id,self.body)
        self.hub.request.assert_not_called()
        self.hub.request.side_effect=None;self.wb.reconcile();self.assertEqual(self.store.get(self.id)['version'],2);self.assertFalse(self.store.get(self.id)['paused'])

    def test_pause_after_promotion_then_resume_uses_new_event(self):
        self.wb.revise(self.id,self.body);self.hub.request.side_effect=Problem('响应未确认',503);self.wb.reconcile()
        old=self.store.get(self.id)['revision_requests'][self.body['request_id']]['dispatch_id']
        self.hub.request.side_effect=None;self.wb.pause(self.id)
        self.wb.revise(self.id,self.body);self.wb.reconcile()
        t=self.store.get(self.id);self.assertFalse(t['paused']);self.assertNotEqual(t['revision_requests'][self.body['request_id']]['dispatch_id'],old);self.assertEqual(t['version'],2)

    def test_old_events_answers_and_results_cannot_change_new_version(self):
        old_runner=Runner(self.store,self.id,1,{})
        self.wb.revise(self.id,self.body);self.wb.reconcile()
        with self.assertRaises(Problem):old_runner.change(status='completed')
        for index,context in enumerate(({'events':[{'subject':self.id,'detail':json.dumps({'version':1})}]},{'answers':[{'about':json.dumps({'task':self.id,'version':1,'approval':'old'})}]})):
            invoke=Mock();Runner(self.store,self.id,2+index,context,invoke).run();invoke.assert_not_called()
        self.assertNotEqual(self.store.get(self.id)['status'],'completed')

    def test_preview_is_text_and_paths_are_confined(self):
        path=self.store.workspace(self.id)/'unsafe.html';path.write_text('<script>alert(1)</script>')
        self.store.change(self.id,lambda t:t.update(artifacts=['report.md','unsafe.html','link.md','../outside']))
        (self.store.workspace(self.id)/'link.md').symlink_to(self.store.workspace(self.id)/'report.md')
        self.assertEqual(self.wb.preview(self.id,'unsafe.html')['kind'],'text')
        for name in ('link.md','../outside','missing'):
            with self.assertRaises(Problem):self.wb.preview(self.id,name)

    def test_trusted_role_policy(self):
        self.assertEqual(role_model(self.store.get(self.id),'execute'),DEFAULT_ROLES['execute'])
        with self.assertRaises(Problem):self.wb.revise(self.id,{**self.body,'role_models':{**DEFAULT_ROLES,'execute':'arbitrary-shell'}})
        self.assertEqual(role_model({'model':'gpt-6-astra'},'execute'),'gpt-6-astra')

    def test_old_open_card_is_not_a_current_todo(self):
        self.wb.revise(self.id,self.body);self.wb.reconcile()
        self.hub.records.return_value=([],[{'id':1,'lane':self.id,'state':'open','version':1,'binding':{'task':self.id,'version':1,'approval':'old'}}])
        task=self.wb.snapshot()['tasks'][0]
        self.assertEqual(task['cards'],[]);self.assertEqual(len(task['expired_cards']),1)
        with self.assertRaises(Problem):self.wb.answer(self.id,{'card':1,'version':1,'choice':0})

    def test_unrouted_revision_retry_gets_new_dispatch_without_repromotion(self):
        self.wb.revise(self.id,self.body);self.hub.request.return_value={'routed':[],'event':1}
        self.wb.reconcile();self.wb.reconcile()
        task=self.store.get(self.id);self.assertEqual(task['revision_requests'][self.body['request_id']]['state'],'blocked_dispatch')
        old=task['revision_requests'][self.body['request_id']]['dispatch_id']
        self.hub.request.return_value={'routed':[RULE],'event':2}
        self.wb.revise(self.id,self.body);self.wb.reconcile();task=self.store.get(self.id)
        self.assertEqual(task['version'],2);self.assertIsNone(task['pending_revision']);self.assertNotEqual(task['revision_requests'][self.body['request_id']]['dispatch_id'],old)

    def test_file_replaced_by_symlink_during_read_is_rejected(self):
        target=self.store.workspace(self.id)/'report.md'
        external=Path(self.temp.name)/'outside.txt';external.write_text('不得被下载')
        original=self.wb.artifact
        def replace(*args,**kwargs):
            path=original(*args,**kwargs);target.unlink();target.symlink_to(external);return path
        with patch.object(self.wb,'artifact',side_effect=replace):
            with self.assertRaises(Problem):self.wb.artifact_bytes(self.id,'report.md')

    def test_expired_runner_error_returns_receipt_without_overwriting_new_version(self):
        context=Path(self.temp.name)/'context.json';context.write_text(json.dumps({'events':[{'subject':self.id,'detail':json.dumps({'version':1})}]}))
        def late_failure(runner):
            self.store.change(self.id,lambda t:t.update(version=2,status='planning'))
            raise Problem('旧版错误')
        output=io.StringIO()
        with patch('task_runner.Store',return_value=self.store),patch.object(Runner,'run',autospec=True,side_effect=late_failure),patch.dict(os.environ,{'AGENT_AUTOLOOP_CONTEXT':str(context),'AGENT_AUTOLOOP_RUN':'42'}),patch('sys.stdout',output):
            task_runner.main()
        self.assertIn('旧版本执行已失效',output.getvalue());self.assertEqual(self.store.get(self.id)['status'],'planning');self.assertIsNotNone(self.store.receipt(42))

if __name__=='__main__':unittest.main()
