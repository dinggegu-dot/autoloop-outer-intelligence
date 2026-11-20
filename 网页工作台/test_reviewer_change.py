"""仅切换审核模型的版本边界；使用隔离账本，不调用供应商。"""
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock,patch

from agent_catalog import DEFAULT_ROLES
from workbench import Store,Workbench,Problem,RULE


class ReviewerChangeTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name).resolve())
        task,_=self.store.create({'name':'切换审核','goal':'保留成果','request_id':str(uuid.uuid4())});self.id=task['id']
        self.roles={**DEFAULT_ROLES,'review':'gpt-6-astra'}
        self.routes={'plan':{'engine':'codex','model':'gpt-6-astra'},'execute':{'engine':'dsh','model':'deepseek-flash'},'review':{'engine':'codex','model':'gpt-6-astra'}}
        (self.store.workspace(self.id)/'answer.txt').write_text('原成果')
        self.store.change(self.id,lambda t:t.update(role_models=self.roles,role_routes=self.routes,status='awaiting_approval',phase='review',calls=6,elapsed_seconds=270,
            artifacts=['answer.txt'],plan={'version':1,'steps':[{'title':'实现','state':'completed'}]},step=1,
            decisions=[{'kind':'plan','version':1,'approved':True,'approval':'original'}],approval={'id':'review-hold','kind':'review','state':'open'}))
        self.hub=Mock();self.hub.records.return_value=([],[]);self.hub.state.return_value={'lines':[{'id':RULE}],'agents':[{'id':'odyssey-runner','online':True}]};self.hub.request.return_value={'routed':[RULE],'event':1}
        self.wb=Workbench(self.store,self.hub)
        self.body={'request_id':str(uuid.uuid4()),'version':1,'model':'deepseek-dsh'}
        self.resolve=patch('engine_adapters.resolve_routes',return_value={**self.routes,'review':{'engine':'dsh','model':'deepseek-flash'}})
        self.resolve.start();self.addCleanup(self.resolve.stop)

    def test_preserves_plan_artifacts_budget_and_archives_original(self):
        self.assertTrue(self.wb.reviewer(self.id,self.body)['queued'])
        t=self.store.get(self.id)
        self.assertEqual((t['version'],t['phase'],t['calls'],t['elapsed_seconds']),(2,'review',6,270))
        self.assertEqual(t['role_models']['review'],'deepseek-dsh')
        self.assertEqual(t['plan']['steps'][0]['state'],'completed')
        self.assertEqual(t['decisions'][-1]['source'],'retained_approval')
        self.assertIsNone(t['approval'])
        self.assertEqual(self.wb.artifact(self.id,'answer.txt',1).read_text(),'原成果')
        self.assertEqual(self.wb.artifact(self.id,'answer.txt').read_text(),'原成果')

    def test_idempotent_and_rejects_changed_replay(self):
        self.wb.reviewer(self.id,self.body);self.wb.reviewer(self.id,self.body)
        self.assertEqual(self.store.get(self.id)['version'],2)
        with self.assertRaises(Problem):self.wb.reviewer(self.id,{**self.body,'model':'gpt-6.1-terra'})

    def test_active_run_and_nonreview_approval_cannot_be_skipped(self):
        self.hub.records.return_value=([{'lane':self.id,'state':'claimed'}],[])
        with self.assertRaises(Problem):self.wb.reviewer(self.id,self.body)
        self.hub.records.return_value=([],[])
        self.store.change(self.id,lambda t:t['approval'].update(kind='execute'))
        with self.assertRaises(Problem):self.wb.reviewer(self.id,self.body)
        self.assertEqual(self.store.get(self.id)['version'],1)

    def test_other_role_route_changes_cannot_be_smuggled_in(self):
        with patch('engine_adapters.resolve_routes',return_value={**self.routes,'execute':{'engine':'dsh','model':'other'}}):
            with self.assertRaises(Problem):self.wb.reviewer(self.id,self.body)
        self.assertEqual(self.store.get(self.id)['version'],1)


if __name__=='__main__':unittest.main()
