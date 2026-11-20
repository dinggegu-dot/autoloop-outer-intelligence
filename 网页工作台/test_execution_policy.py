import subprocess
import sys
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch, Mock

import task_runner
from task_runner import Runner, ExecutionTimeout
from workbench import Store, Workbench, Problem, RULE
from execution_policy import exhaustion, plan_limit, ProgressWindow

RESULT = dict(status='completed', summary='当前步骤完成', checks=['实际检查'], artifacts=[], question='', requested_action='')

class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name).resolve())
        self.task,_=self.store.create(dict(name='自动任务',goal='核对产物',request_id=str(uuid.uuid4())))
        self.id=self.task['id']
        self.skill=Path(self.temp.name)/'skill.md';self.skill.write_text('仅处理当前步骤。')
        self.patch=patch.dict(task_runner.SKILLS,{k:self.skill for k in task_runner.SKILLS});self.patch.start();self.addCleanup(self.patch.stop)

    def test_auto_has_no_call_cutoff_but_keeps_time_budget(self):
        t={**self.task,'calls':999}
        self.assertIsNone(exhaustion(t));self.assertEqual(plan_limit(t),5)
        self.assertIsNotNone(exhaustion({**t,'elapsed_seconds':3600}))
        self.assertIsNotNone(exhaustion({**t,'execution_mode':'bounded','max_calls':6}))

    def test_failed_call_accounts_elapsed_time(self):
        invoke=Mock(side_effect=Problem('真实失败'))
        with patch('task_runner.time.monotonic',side_effect=[100,112]):
            with self.assertRaises(Problem):Runner(self.store,self.id,1,{},invoke).call('execute')
        self.assertEqual(self.store.get(self.id)['elapsed_seconds'],12)
        self.assertEqual(self.store.get(self.id)['calls'],1)

    def test_new_file_timeout_gets_one_recovery(self):
        def invoke(role,prompt,schema,number):
            if number==1:
                (self.store.workspace(self.id)/'checkpoint.txt').write_text('已保存成果')
                raise ExecutionTimeout('本次执行达到时间保护上限')
            return RESULT
        out=Runner(self.store,self.id,1,{},invoke).guarded_call('execute')
        self.assertEqual(out['status'],'completed');self.assertEqual(self.store.get(self.id)['calls'],2)
        self.assertEqual(sum(self.store.get(self.id)['recovery_attempts'].values()),1)

    def test_no_progress_timeout_does_not_repeat(self):
        invoke=Mock(side_effect=ExecutionTimeout('本次执行达到时间保护上限'))
        with self.assertRaises(ExecutionTimeout):Runner(self.store,self.id,1,{},invoke).guarded_call('execute')
        self.assertEqual(invoke.call_count,1)

    def test_business_error_is_not_automatic_retry(self):
        def invoke(*args):
            (self.store.workspace(self.id)/'note.txt').write_text('已有文件')
            raise Problem('需要用户决定时间范围')
        with self.assertRaises(Problem):Runner(self.store,self.id,1,{},invoke).guarded_call('execute')
        self.assertEqual(self.store.get(self.id)['calls'],1)

    def test_failed_review_stops_after_three_cycles(self):
        self.store.change(self.id,lambda t:t.update(phase='review',plan={'steps':[]},decisions=[{'kind':'plan','version':1,'approved':True}]))
        def invoke(role,*args):
            return {**RESULT,'status':'needs_changes','summary':'尚未满足验收'} if role=='review' else RESULT
        Runner(self.store,self.id,1,{},invoke).run()
        task=self.store.get(self.id);self.assertEqual(task['review_failures'],3);self.assertEqual(task['status'],'blocked');self.assertEqual(task['calls'],5)

    def test_blocked_review_replaces_old_findings_and_keeps_request_as_evidence(self):
        self.store.change(self.id,lambda t:t.update(phase='review',plan={'steps':[]},review={**RESULT,'status':'needs_changes','summary':'旧问题'},decisions=[{'kind':'plan','version':1,'approved':True}]))
        latest={**RESULT,'status':'blocked','summary':'新环境阻塞','requested_action':'修复审核环境'}
        Runner(self.store,self.id,1,{},lambda *args:latest).run()
        task=self.store.get(self.id)
        self.assertEqual(task['status'],'blocked')
        self.assertEqual(task['review'],latest)
        self.assertIsNone(task['approval'])
        self.assertEqual(task['blocker']['requested_action'],'修复审核环境')

    def test_resume_migration_preserves_artifacts_and_approval(self):
        self.store.change(self.id,lambda t:t.update(execution_mode='bounded',max_calls=6,calls=6,artifacts=['kept.md'],decisions=[{'kind':'plan','version':1,'approved':True}]))
        hub=Mock();hub.records.return_value=([],[]);hub.state.return_value={'lines':[{'id':RULE}],'agents':[{'id':'odyssey-runner','online':True}]};hub.request.return_value={'routed':[RULE],'event':1}
        Workbench(self.store,hub).resume(self.id,{'request_id':str(uuid.uuid4()),'execution_mode':'auto'})
        t=self.store.get(self.id);self.assertEqual(t['execution_mode'],'auto');self.assertEqual(t['calls'],6);self.assertEqual(t['artifacts'],['kept.md']);self.assertTrue(t['decisions'][0]['approved'])

    def test_invalid_time_extension_does_not_mutate(self):
        hub=Mock();hub.records.return_value=([],[])
        with self.assertRaises(Problem):Workbench(self.store,hub).resume(self.id,{'request_id':str(uuid.uuid4()),'additional_minutes':-30})
        self.assertEqual(self.store.get(self.id)['time_budget_seconds'],3600)

    def test_identical_signals_and_heartbeat_do_not_extend_idle(self):
        w=ProgressWindow(0,3600,hard=2000,idle=600);w.note('first-check',100);w.note('first-check',650);w.note(None,680)
        self.assertIsNotNone(w.reason(701))
        w.note('new-file',701);self.assertIsNone(w.reason(702));self.assertIsNotNone(w.reason(2000))

    def test_total_budget_always_wins(self):
        w=ProgressWindow(100,30);w.note('new-file',129);self.assertIsNotNone(w.reason(130))

    def hub(self):
        hub=Mock();hub.records.return_value=([],[])
        hub.state.return_value={'lines':[{'id':RULE}],'agents':[{'id':'odyssey-runner','online':True}]}
        hub.request.return_value={'routed':[RULE],'event':1}
        return hub

    def test_resume_unknown_retry_adds_time_once(self):
        hub=self.hub();hub.request.side_effect=[Problem('响应未知',503),{'routed':[RULE],'event':1}]
        wb=Workbench(self.store,hub);body={'request_id':str(uuid.uuid4()),'additional_minutes':30}
        self.assertFalse(wb.resume(self.id,body)['queued'])
        self.assertEqual(wb.snapshot()['tasks'][0]['pending_resume'],body)
        self.assertTrue(wb.resume(self.id,body)['queued'])
        self.assertEqual(self.store.get(self.id)['time_budget_seconds'],5400)
        self.assertEqual(hub.request.call_args_list[0],hub.request.call_args_list[1])
        with self.assertRaises(Problem):wb.resume(self.id,{**body,'additional_minutes':60})

    def test_pending_resume_new_id_cannot_double_grant(self):
        hub=self.hub();hub.request.side_effect=[Problem('响应未知',503),{'routed':[RULE],'event':1}]
        wb=Workbench(self.store,hub)
        wb.resume(self.id,{'request_id':str(uuid.uuid4()),'additional_minutes':30})
        with self.assertRaises(Problem):wb.resume(self.id,{'request_id':str(uuid.uuid4()),'additional_minutes':60})
        wb.resume(self.id,{'request_id':str(uuid.uuid4()),'additional_minutes':30})
        self.assertEqual(self.store.get(self.id)['time_budget_seconds'],5400)

    def test_create_policy_conflict_and_mutated_task_retry(self):
        body={'request_id':str(uuid.uuid4()),'name':'幂等任务','goal':'检查','execution_mode':'auto'}
        task,_=self.store.create(body)
        self.store.change(task['id'],lambda t:t.update(execution_mode='bounded',time_budget_seconds=5400))
        self.assertFalse(self.store.create(body)[1])
        with self.assertRaises(Problem):self.store.create({**body,'execution_mode':'bounded'})
        with self.assertRaises(Problem):self.store.create({**body,'budget_minutes':30})

    def test_crashed_process_persists_and_recovers_reserved_time_once(self):
        # 真实子进程直接退出，故意跳过 finally；无需启动模型或中台。
        code="""import os
from task_runner import Runner
from workbench import Store
store=Store(ROOT)
def crash(*args): os._exit(23)
Runner(store,TASK,1,{},crash).call('execute')
""".replace('ROOT',repr(str(self.store.root))).replace('TASK',repr(self.id))
        result=subprocess.run([sys.executable,'-B','-c',code],cwd=Path(task_runner.__file__).parent,capture_output=True,text=True)
        self.assertEqual(result.returncode,23,result.stderr)
        task=self.store.get(self.id);self.assertIsNotNone(task['active_call']);self.assertEqual(task['elapsed_seconds'],0)
        self.store.recover_call(self.id);self.store.recover_call(self.id)
        task=self.store.get(self.id);self.assertEqual(task['elapsed_seconds'],900);self.assertEqual(task['estimated_seconds'],900);self.assertIsNone(task['active_call'])

    def test_running_usage_is_visible_without_double_persisting(self):
        self.store.change(self.id,lambda t:t.update(elapsed_seconds=10,active_call={'number':1,'started_at':100,'reserved_seconds':900}))
        with patch('workbench.time.time',return_value=130):
            self.assertEqual(Workbench(self.store,self.hub()).snapshot()['tasks'][0]['elapsed_seconds'],40)
        self.assertEqual(self.store.get(self.id)['elapsed_seconds'],10)

    def test_live_model_group_blocks_recovery(self):
        self.store.change(self.id,lambda t:t.update(model_group=234,active_call={'number':1,'started_at':100,'reserved_seconds':900}))
        with patch('workbench.subprocess.check_output',return_value='234 S\n'):
            with self.assertRaises(Problem):self.store.recover_call(self.id)
        self.assertEqual(self.store.get(self.id)['elapsed_seconds'],0)

    def test_task_lease_blocks_second_runner(self):
        from workbench import TaskBusy
        with self.store.lease(self.id):
            with self.assertRaises(TaskBusy):Runner(self.store,self.id,1,{},Mock()).run()
        self.assertEqual(self.store.get(self.id)['calls'],0)

    def test_dispatch_can_start_worker_before_http_response(self):
        hub=self.hub()
        def receive(*args,**kwargs):
            def invoke(*args):
                return {'summary':'计划','scope':'本地','assumptions':[], 'steps':[{'title':'步骤','objective':'目标','verification':'检查'}]}
            Runner(self.store,self.id,1,{},invoke).run()
            return {'routed':[RULE],'event':1}
        hub.request.side_effect=receive
        Workbench(self.store,hub).resume(self.id,{'request_id':str(uuid.uuid4())})
        task=self.store.get(self.id)
        self.assertEqual(task['status'],'awaiting_approval');self.assertEqual(task['calls'],1)

    def test_launch_gate_does_not_execute_before_registration(self):
        script=Path(task_runner.__file__).with_name('execution_child.py')
        marker=Path(self.temp.name)/'started.txt'
        command=[sys.executable,'-B',str(script),sys.executable,'-c',f'from pathlib import Path; Path({str(marker)!r}).write_text("started")']
        # EOF 模拟父进程在记录 PGID 前退出：模型命令绝不能开始。
        result=subprocess.run(command,input=b'',capture_output=True)
        self.assertEqual(result.returncode,75);self.assertFalse(marker.exists())
        result=subprocess.run(command,input=b'1',capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr);self.assertTrue(marker.exists())

    def test_launch_gate_preserves_prompt_bytes(self):
        script=Path(task_runner.__file__).with_name('execution_child.py')
        result=subprocess.run([sys.executable,'-B',str(script),sys.executable,'-c','import sys; print(sys.stdin.read(),end="")'],input='1完整任务正文'.encode(),capture_output=True)
        self.assertEqual(result.stdout.decode(),'完整任务正文')

if __name__=='__main__':unittest.main()
