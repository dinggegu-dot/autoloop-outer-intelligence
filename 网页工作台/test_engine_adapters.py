"""双引擎契约与真实本机替身测试；不连接供应商、不消耗模型额度。"""
import json
import os
import signal
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from agent_catalog import DEFAULT_ROLES, LEGACY_ROLES, validate_roles
from engine_adapters import (CONFIG_NAME, ConnectionProblem, DSHEvents, connection_status, signature,
                             dsh_overlay, resolve_routes, safe_dsh_log, validate_result)
from process_guard import alive, processes
from setup_dsh import atomic_json, register, probe
from task_runner import Runner, RESULT_SCHEMA, ExecutionTimeout
from workbench import Store, Problem
from execution_policy import ProgressWindow


RESULT={'status':'completed','summary':'完成','checks':['已读回文件'],'artifacts':['answer.txt'],'question':'','requested_action':''}
DSH_ROLES={'plan':'gpt-6-astra','execute':'deepseek-dsh','review':'deepseek-dsh'}


class ProtocolTest(unittest.TestCase):
    def test_final_requires_success_exit_and_valid_receipt(self):
        for code,event in ((1,{'reason':{'kind':'completed'},'text':json.dumps(RESULT)}),
                           (0,{'reason':{'kind':'aborted'},'text':json.dumps(RESULT)}),
                           (0,{'reason':{'kind':'completed'},'text':'```json\n{}\n```'}),
                           (0,{'reason':{'kind':'completed'},'text':'{}'})):
            parser=DSHEvents(lambda *args:None)
            parser.feed(json.dumps({'type':'final',**event}))
            with self.assertRaises(ConnectionProblem):parser.result(code,RESULT_SCHEMA)
        parser=DSHEvents(lambda *args:None)
        parser.feed(json.dumps({'type':'status','phase':'turn_end'}))
        with self.assertRaises(ConnectionProblem):parser.result(0,RESULT_SCHEMA)

    def test_step_usage_dedup_and_private_thinking(self):
        events=[];parser=DSHEvents(lambda *row:events.append(row))
        line=json.dumps({'type':'status','phase':'step_end','turn':1,'step':1,'usage':{'inputTokens':10}})
        parser.feed(line);parser.feed(line)
        parser.feed(line.replace('"step": 1','"step": 2'))
        self.assertEqual(len(events),2)
        thinking=json.dumps({'type':'thinking','text':'private reasoning'})
        self.assertIsNone(parser.feed(thinking));self.assertIsNone(safe_dsh_log(thinking))
        self.assertNotIn('private',safe_dsh_log(json.dumps({'type':'tool_result','result':'private source'})))

    def test_schema_and_overlay_are_bounded(self):
        with self.assertRaises(ConnectionProblem):validate_result({**RESULT,'extra':'field'},RESULT_SCHEMA)
        with self.assertRaises(ConnectionProblem):validate_result({**RESULT,'checks':'not-list'},RESULT_SCHEMA)
        rows={r['id']:r for r in dsh_overlay({'provider':'deepseek-account','model':'deepseek-flash'})}
        self.assertFalse(rows['credentials']['disabled']);self.assertFalse(rows['deepseek-account']['disabled'])
        self.assertTrue(rows['tool-subagent']['disabled']);self.assertTrue(rows['session-log-deepseek']['disabled'])
        self.assertEqual(rows['agent-default-model']['config']['model'],'deepseek-flash')

    def test_credential_error_is_actionable_without_echoing_secret(self):
        event={'type':'error','message':'MISSING_CREDENTIAL credential=private-test-secret'}
        parser=DSHEvents(lambda *args:None);parser.feed(json.dumps(event))
        with self.assertRaisesRegex(ConnectionProblem,'缺少认证') as error:parser.result(1,RESULT_SCHEMA)
        self.assertNotIn('private-test-secret',str(error.exception))
        self.assertNotIn('private-test-secret',safe_dsh_log(json.dumps(event)))


class EngineIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.store=Store(self.root)
        self.home=self.root/'dsh-home';self.home.mkdir()
        self.fake=self.root/'fake-dsh'
        self.fake.write_text('#!'+sys.executable+'\nimport sys\nprint("0.2.2-rc.2")\n')
        self.fake.chmod(0o700)
        self.config={'version':1,'program':str(self.fake),'home':str(self.home),'provider':'deepseek-account','model':'deepseek-flash'}
        self.body={'name':'双引擎测试','goal':'生成真实文件','request_id':str(uuid.uuid4())}
        self.task,_=self.store.create({**self.body,'role_models':DSH_ROLES})
        self.id=self.task['id']

    def connected(self):
        atomic_json(self.root/CONFIG_NAME,self.config)
        atomic_json(self.root/'dsh-verification.json',{'passed':True,'config_hash':signature(self.config),'fixture':True})

    def test_local_codex_mapping_is_pinned_and_rejects_commands(self):
        task={**self.task,'role_models':dict.fromkeys(DEFAULT_ROLES,'gpt-6-astra')}
        path=self.root/'codex-models.json'
        path.write_text(json.dumps({'gpt-6-astra':'account-model-1'}))
        routes=resolve_routes(task,self.root)
        self.assertTrue(all(route=={'engine':'codex','model':'account-model-1'} for route in routes.values()))
        task['role_routes']=routes
        path.write_text(json.dumps({'gpt-6-astra':'account-model-2'}))
        with self.assertRaises(ConnectionProblem):resolve_routes(task,self.root)
        path.write_text(json.dumps({'gpt-6-astra':'/bin/sh -c hello'}))
        with self.assertRaises(ConnectionProblem):resolve_routes(task,self.root)

    def fake_executor(self,body=''):
        script='''import sys, json, os
from pathlib import Path
if '--version' in sys.argv:
 print('0.2.2-rc.2');sys.exit(0)
prompt=sys.stdin.read()
patch=json.loads(Path(sys.argv[sys.argv.index('--patch')+1]).read_text())
Path('launch.json').write_text(json.dumps({'args':sys.argv,'home':os.environ.get('DSH_HOME'),'patch':patch,'prompt':prompt}))
print(json.dumps({'type':'session','sessionId':'fixture-session'}),flush=True)
'''+body+'''
Path('answer.txt').write_text('DeepSeek fixture artifact')
Path('readiness.txt').write_text('DSH_READY')
print(json.dumps({'type':'thinking','text':'private-reasoning-marker'}),flush=True)
print(json.dumps({'type':'tool_call','callId':'c1','tool':'bash','input':{'command':'read file'}}),flush=True)
print(json.dumps({'type':'tool_result','callId':'c1','status':'completed','result':'read back'}),flush=True)
print(json.dumps({'type':'status','phase':'step_end','turn':1,'step':1,'usage':{'inputTokens':20,'outputTokens':10,'cacheReadTokens':5}}),flush=True)
result={'status':'completed','summary':'完成','checks':['真实替身读回'],'artifacts':['answer.txt','readiness.txt'],'question':'','requested_action':''}
print(json.dumps({'type':'final','reason':{'kind':'completed'},'text':json.dumps(result)}),flush=True)
'''
        self.fake.write_text('#!'+sys.executable+'\n'+script)
        self.fake.chmod(0o700)

    def test_missing_connection_blocks_before_astra_or_budget(self):
        with patch('task_runner.subprocess.Popen') as launch:
            with self.assertRaises(Problem):Runner(self.store,self.id,1,{}).call('plan')
        launch.assert_not_called();self.assertEqual(self.store.get(self.id)['calls'],0)
        self.assertEqual(connection_status(self.root)['state'],'unconfigured')

    def test_defaults_legacy_and_connection_cannot_be_task_payload(self):
        new,_=self.store.create({**self.body,'request_id':str(uuid.uuid4())})
        self.assertEqual(new['role_models'],DEFAULT_ROLES)
        self.assertEqual({route['engine'] for route in resolve_routes(new,self.root).values()},{'codex'})
        old={**self.task,'role_models':LEGACY_ROLES}
        self.assertEqual(resolve_routes(old,self.root)['execute']['engine'],'codex')
        self.assertEqual(validate_roles(DSH_ROLES)['review'],'deepseek-dsh')
        with self.assertRaises(ValueError):validate_roles({**DEFAULT_ROLES,'plan':'deepseek-dsh'})
        with self.assertRaises(ValueError):validate_roles({**DEFAULT_ROLES,'execute':'/bin/sh'})

    def test_implicit_replay_keeps_historical_model_assignments(self):
        from agent_catalog import preserve_implicit_roles

        replay,created=self.store.create(self.body)
        self.assertFalse(created)
        self.assertEqual(replay['role_models'],DSH_ROLES)
        self.assertEqual(preserve_implicit_roles(self.body,self.store)['role_models'],DSH_ROLES)
        with self.assertRaises(Problem):
            self.store.create({**self.body,'role_models':DEFAULT_ROLES})

    def test_registered_but_unverified_blocks_astra(self):
        atomic_json(self.root/CONFIG_NAME,self.config)
        with self.assertRaises(Problem):Runner(self.store,self.id,1,{}).call('plan')
        self.assertEqual(self.store.get(self.id)['calls'],0)

    def test_register_is_idempotent_and_requires_explicit_replace(self):
        self.assertTrue(register(self.root,self.config))
        self.assertFalse(register(self.root,self.config))
        with self.assertRaises(ConnectionProblem):register(self.root,{**self.config,'model':'deepseek-other'})
        with self.assertRaises(ConnectionProblem):register(self.root,{**self.config,'api_key':'forbidden'})
        self.assertEqual(connection_status(self.root)['state'],'configured')

    def test_pinned_routes_cannot_silently_switch(self):
        self.connected();routes=resolve_routes(self.task,self.root)
        self.store.change(self.id,lambda t:t.update(role_routes=routes))
        atomic_json(self.root/CONFIG_NAME,{**self.config,'model':'deepseek-other'})
        with self.assertRaises(ConnectionProblem):resolve_routes(self.store.get(self.id),self.root)

    def test_configuration_swap_between_preflight_and_launch_is_rejected(self):
        self.connected();self.fake_executor()
        with patch('task_runner.load_connection',return_value={**self.config,'model':'deepseek-other'}),patch.object(Runner,'run_process') as launch:
            with self.assertRaises(Problem):Runner(self.store,self.id,1,{}).call('execute')
        launch.assert_not_called()
        self.assertEqual(self.store.get(self.id)['role_routes']['execute']['model'],'deepseek-flash')

    def test_partial_usage_never_claims_complete_call(self):
        runner=Runner(self.store,self.id,1,{})
        runner.role='execute';runner.call_number=1
        parser=DSHEvents(lambda kind,title,detail:runner.record_usage('dsh',detail['usage']) if kind in ('usage','usage_missing') else None)
        parser.feed(json.dumps({'type':'status','phase':'step_end','turn':1,'step':1,'usage':{'inputTokens':20,'outputTokens':10}}))
        parser.feed(json.dumps({'type':'status','phase':'step_end','turn':1,'step':2}))
        usage=self.store.get(self.id)['usage_by_role']['execute']
        self.assertEqual(usage['input'],20);self.assertEqual(usage['partial_calls'],[1])

    def test_real_subprocess_uses_dsh_and_records_artifact_usage_no_thinking(self):
        self.connected();self.fake_executor()
        result=Runner(self.store,self.id,1,{}).call('execute')
        self.assertEqual(result['status'],'completed')
        task=self.store.get(self.id);workspace=self.store.workspace(self.id)
        self.assertEqual((workspace/'answer.txt').read_text(),'DeepSeek fixture artifact')
        args=json.loads((workspace/'launch.json').read_text())
        self.assertNotIn('--model',args['args']);self.assertNotIn('--session-id',args['args'])
        self.assertIn('workspace-write',args['args']);self.assertIn('never',args['args'])
        self.assertEqual(args['home'],str(self.home))
        self.assertEqual(task['usage_by_role']['execute']['input'],20)
        self.assertEqual(task['role_routes']['execute']['engine'],'dsh')
        self.assertFalse(task['model_processes']);self.assertIsNone(task['model_group'])
        log=(self.root/'outputs'/self.id/'1-1/events.ndjson').read_text()
        self.assertNotIn('private-reasoning-marker',log)
        self.assertTrue(any(e['kind']=='tool_result' for e in self.store.events(self.id)))

    def test_probe_runs_only_dsh_and_invalidates_on_config_change(self):
        self.connected();self.fake_executor()
        proof=probe(self.root)
        self.assertTrue(proof['passed']);self.assertEqual(connection_status(self.root)['state'],'verified')
        self.assertEqual(len(self.store.list()),1) # 探针未写入业务任务库
        atomic_json(self.root/CONFIG_NAME,{**self.config,'model':'deepseek-other'})
        self.assertEqual(connection_status(self.root)['state'],'configured')

    def test_detached_tool_is_stopped_on_timeout(self):
        self.connected()
        marker=self.root/'detached.json'
        self.fake_executor('''import subprocess,time
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],start_new_session=True)
Path('''+repr(str(marker))+''').write_text(json.dumps({'pid':child.pid}))
time.sleep(60)
''')
        def short_window(started,remaining):return ProgressWindow(started,remaining,hard=2,idle=2)
        try:
            with patch('task_runner.ProgressWindow',side_effect=short_window):
                with self.assertRaises(ExecutionTimeout):Runner(self.store,self.id,1,{}).call('execute')
            pid=json.loads(marker.read_text())['pid']
            current=processes();self.assertTrue(pid not in current or current[pid]['state'].startswith('Z'))
            self.assertFalse(self.store.get(self.id)['model_processes'])
        finally:
            if marker.exists():
                with __import__('contextlib').suppress(ProcessLookupError):os.kill(json.loads(marker.read_text())['pid'],signal.SIGKILL)

    def test_process_monitor_failure_never_launches_model(self):
        self.connected();self.fake_executor()
        with patch('task_runner.processes',side_effect=PermissionError),patch('task_runner.subprocess.Popen') as launch:
            with self.assertRaises(Problem):Runner(self.store,self.id,1,{}).call('execute')
        launch.assert_not_called()

    def test_complete_pipeline_routes_astra_dsh_astra_and_repairs(self):
        self.store.change(self.id,lambda t:t.update(role_models={**DSH_ROLES,'review':'gpt-6-astra'}))
        self.connected();self.fake_executor()
        fake_codex=self.root/'fake-codex'
        fake_codex.write_text('#!'+sys.executable+'''\nimport sys,json
from pathlib import Path
prompt=sys.stdin.read()
out=Path(sys.argv[sys.argv.index('--output-last-message')+1])
schema=json.loads(Path(sys.argv[sys.argv.index('--output-schema')+1]).read_text())
assert sys.argv[sys.argv.index('-m')+1]=='gpt-6-astra'
if 'steps' in schema['properties']:
 result={'summary':'实施一个文件','scope':'任务工作区','assumptions':[],'steps':[{'title':'生成文件','objective':'生成 answer.txt','verification':'读回验证'}]}
else:
 assert Path('answer.txt').read_text()=='DeepSeek fixture artifact'
 counter=Path('''+repr(str(self.root/'review-count'))+''')
 count=int(counter.read_text()) if counter.exists() else 0
 counter.write_text(str(count+1))
 result={'status':'needs_changes' if count==0 else 'completed','summary':'补充一次检查' if count==0 else '独立检查通过','checks':['实际读回 answer.txt'],'artifacts':['answer.txt'],'question':'','requested_action':''}
 print(json.dumps({'type':'item.completed','item':{'type':'command_execution','exit_code':0,'status':'completed','command':'read','aggregated_output':'verified'}}),flush=True)
out.write_text(json.dumps(result))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':100,'output_tokens':50}}),flush=True)
''')
        fake_codex.chmod(0o700)
        with patch('task_runner.CODEX',str(fake_codex)):
            first=Runner(self.store,self.id,1,{}).run()
            self.assertIn('ask',first['receipt'])
            self.assertFalse((self.store.workspace(self.id)/'answer.txt').exists())
            binding=first['receipt']['ask'][0]['about']
            # 仅隔离测试注入用户批准，不触碰真实中台审批。
            result=Runner(self.store,self.id,2,{'answers':[{'card':99,'choice':0,'about':binding}]}).run()
        task=self.store.get(self.id)
        self.assertEqual(task['status'],'completed');self.assertEqual(task['calls'],5)
        self.assertEqual(task['review_failures'],1)
        self.assertEqual(task['usage_by_role']['execute']['input'],40)
        routes=[e['detail']['engine'] for e in self.store.events(self.id) if e['kind']=='model_started']
        self.assertEqual(routes,['codex','dsh','codex','dsh','codex'])
        self.assertIn('独立验收',result['summary'])

    def test_dsh_review_runs_in_copy_with_actual_command_evidence(self):
        self.connected()
        source=self.store.workspace(self.id)
        (source/'answer.txt').write_text('candidate')
        self.fake.write_text('#!'+sys.executable+'''\nimport json,sys,subprocess
from pathlib import Path
prompt=sys.stdin.read()
assert '独立审核者' in prompt and '禁止修补代码' in prompt
assert Path.cwd().name=='review-workspace'
subprocess.run([sys.executable,'-B','-c',"from pathlib import Path; assert Path('answer.txt').read_text()=='candidate'"],check=True)
print(json.dumps({'type':'session','sessionId':'independent-review'}))
print(json.dumps({'type':'tool_call','callId':'verify','tool':'bash'}))
print(json.dumps({'type':'tool_result','callId':'verify','status':'completed','result':'checked'}))
print(json.dumps({'type':'final','reason':{'kind':'completed'},'text':json.dumps({'status':'completed','summary':'真实副本检查','checks':['真实子进程读回'],'artifacts':['answer.txt'],'question':'','requested_action':''})}))
''')
        self.fake.chmod(0o700)
        result=Runner(self.store,self.id,7,{}).call('review')
        self.assertEqual(result['status'],'completed')
        self.assertEqual((source/'answer.txt').read_text(),'candidate')
        events=self.store.events(self.id)
        self.assertTrue(any(e['kind']=='command' and e['detail'].get('role')=='review' and e['detail'].get('exit_code')==0 for e in events))


if __name__=='__main__':unittest.main()
