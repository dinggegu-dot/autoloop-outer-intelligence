import hashlib
import tempfile
import unittest
import uuid
from pathlib import Path

from workbench import Problem, Store, Workbench
from superagent.goal_engine import MODEL, GoalEngine, current, digest, flow
from superagent.goal_runtime import GoalRuntime


class HubDouble:
    def __init__(self):
        self.events = []
        self.lose_response = False
        self.run_state = 'claimed'

    def existing_event(self,key,event_type,subject):
        for index, event in enumerate(self.events,1):
            if (event['id'],event['type'],event['subject']) == (key,event_type,subject):
                return index
        return None

    def state(self):
        return {'lines':[{'id':'odyssey-goal'}],'agents':[{'id':'odyssey-goal','online':True}]}

    def records(self):
        return [],[]

    def request(self, method, path, body=None, source=False):
        if method == 'POST' and path == '/v1/events':
            if body not in self.events:
                self.events.append(body)
            if self.lose_response:
                self.lose_response = False
                raise Problem('模拟回执中断',503)
            return {'routed':['odyssey-goal'],'event':self.events.index(body)+1}
        if method == 'POST' and path.endswith('/cancel'):
            self.run_state = 'cancelled'
            return {}
        event = int(path.rsplit('/',1)[1])
        return {'runs':[{'rule':'odyssey-goal','state':self.run_state,'run':event}]}


class GoalEngineTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name).resolve())
        self.engine = GoalEngine(self.store)
        self.body = {'name':'检查表格','goal':'实现 CSV 检查','acceptance':'四行输出一致',
                     'request_id':str(uuid.uuid4())}
        task,_ = self.engine.create(self.body,'feishu')
        self.ticket = task['id']
        self.run_id = 0

    def launch(self, expected):
        task = self.engine.prepare(self.ticket)
        row = current(task)
        self.assertEqual(row['phase'],expected)
        self.run_id += 1
        self.engine.bind(self.ticket,row['id'],self.run_id,self.run_id)
        return row

    def finish(self, row, result, **rest):
        receipt = {'id':row['id'],'version':row['version'],'run':self.run_id,'result':result,
                   'evidence':{'session':str(self.run_id),'model':MODEL,'started_ns':0,'ended_ns':1_000_000_000},
                   **rest}
        return self.engine.settle(self.ticket,row['id'],self.run_id,'done',receipt)

    def plan(self):
        return self.finish(self.launch('plan'),{'summary':'构建并检查工具','steps':[{'objective':'实现','checks':['真实输出']} ]})

    def approve(self):
        task = self.plan()
        self.engine.approve(self.ticket,task['version'],task['approval']['id'])
        return task

    def checkpoint(self):
        sha = hashlib.sha256(b'valid output').hexdigest()
        return {'digest':sha,'artifacts':['csv_check.py'],'files':{'csv_check.py':sha}}

    def verification(self, passed):
        return {'passed':passed,'checks':[{'id':'actual','passed':passed,'detail':'输出'}]}

    def test_recheck_keeps_mandate_and_checkpoint(self):
        self.approve()
        point = self.checkpoint()
        self.finish(self.launch('execute'),{'summary':'完成'},checkpoint=point)
        self.store.change(self.ticket,lambda task:task.update(status='blocked'))
        before = self.store.get(self.ticket)
        for version,checksum in [(2,point['digest']),(1,'different')]:
            with self.assertRaises(Problem):
                self.engine.recheck(self.ticket,version,checksum)
        result = self.engine.recheck(self.ticket,1,point['digest'])
        self.assertEqual(flow(result)['mandate'],flow(before)['mandate'])
        self.assertEqual(flow(result)['checkpoint'],point)
        self.assertEqual(self.launch('review')['step'],0)
        with self.assertRaises(Problem):
            self.engine.recheck(self.ticket,1,point['digest'])

    def test_merge_rebuilds_candidate_from_verified_files(self):
        from superagent.goal_runtime import execute_assignment
        parent = self.store.root/'goal-attempts'/self.ticket
        frozen = parent/'branch'/'outputs'
        frozen.mkdir(parents=True)
        (frozen/'module.py').write_bytes(b'pass\n')
        checksum = hashlib.sha256(b'pass\n').hexdigest()
        assignment = {'ticket':self.ticket,'phase':'assemble','run':3,'id':'merge','version':1,
                      'inputs':[{'assignment':'branch','package':'logic','files':{'module.py':checksum}}]}
        target=parent/'merge'
        target.mkdir()
        result=execute_assignment(self.store,assignment,target,None)
        self.assertEqual(result['workspace_id'],'merge')
        self.assertEqual(result['checkpoint']['files'],{'module.py':checksum})
        self.assertFalse((self.store.workspace(self.ticket)/'module.py').exists())
        (frozen/'module.py').write_bytes(b'changed')
        target=parent/'retry'
        target.mkdir()
        self.assertIn('error',execute_assignment(self.store,assignment,target,None))

    def test_trigger_and_approval_binding(self):
        old,fresh = self.engine.create(self.body,'feishu')
        self.assertFalse(fresh)
        self.assertEqual(old['id'],self.ticket)
        with self.assertRaises(Problem):
            self.engine.create(dict(self.body,goal='另一个目标'),'feishu')
        task = self.plan()
        with self.assertRaises(Problem):
            self.engine.approve(self.ticket,task['version']+1,task['approval']['id'])
        self.assertIsNone(current(self.engine.prepare(self.ticket)))
        workbench = Workbench(store=self.store,goal_enabled=True)
        answer = {'card':task['approval']['id'],'version':task['version'],'choice':0}
        workbench.answer(self.ticket,answer)
        workbench.answer(self.ticket,answer)
        with self.assertRaises(Problem):
            workbench.answer(self.ticket,dict(answer,choice=1),source='feishu')
        actual = self.store.get(self.ticket)
        self.assertEqual(len(actual['decisions']),1)
        self.assertEqual(flow(actual)['mandate']['source'],'board')

    def test_automatic_repair_and_final_delivery(self):
        self.approve()
        self.finish(self.launch('execute'),{'summary':'初稿'},checkpoint=self.checkpoint())
        blocked = self.finish(self.launch('review'),{'passed':True,'findings':['有错误']},
                              checkpoint=self.checkpoint(),validation=self.verification(False))
        self.assertEqual(flow(blocked)['stage'],'repair')
        self.finish(self.launch('repair'),{'summary':'修复'},checkpoint=self.checkpoint())
        final = self.launch('review')
        done = self.finish(final,{'passed':True,'findings':['已核对']},
                           checkpoint=self.checkpoint(),validation=self.verification(True),
                           delivery={'assignment':final['id'],'files':self.checkpoint()['files']})
        self.assertEqual(done['status'],'completed')
        self.assertEqual(len(flow(done)['assignments']),5)
        self.assertIsNone(current(self.engine.prepare(self.ticket)))
        frozen = self.store.root/'goal-attempts'/self.ticket/final['id']/'verified'
        frozen.mkdir(parents=True)
        (frozen/'csv_check.py').write_bytes(b'valid output')
        (self.store.workspace(self.ticket)/'csv_check.py').write_bytes(b'changed')
        wb = Workbench(store=self.store,goal_enabled=True)
        self.assertEqual(wb.artifact_bytes(self.ticket,'csv_check.py')[1],b'valid output')
        (frozen/'csv_check.py').write_bytes(b'changed')
        with self.assertRaises(Problem):
            wb.artifact_bytes(self.ticket,'csv_check.py')

    def test_lost_worker_receipt_requires_recheck_before_repair(self):
        self.approve()
        row = self.launch('execute')
        self.engine.observe(self.ticket,row['id'],'unknown')
        self.assertEqual(current(self.engine.prepare(self.ticket))['id'],row['id'])
        with self.assertRaises(Problem):
            self.engine.settle(self.ticket,row['id'],self.run_id,'unknown')
        restored = self.engine.settle(self.ticket,row['id'],self.run_id,'done')
        self.assertEqual(flow(restored)['stage'],'review')
        self.assertTrue(flow(restored)['validation']['recovering_execute'])

    def test_outbox_replays_stable_event_after_runtime_restart(self):
        hub = HubDouble()
        hub.lose_response = True
        with self.assertRaises(Problem):
            GoalRuntime(self.store,hub).tick(self.ticket)
        previous = current(self.store.get(self.ticket))
        GoalRuntime(Store(self.store.root),hub).tick(self.ticket)
        after = current(self.store.get(self.ticket))
        self.assertEqual(len(hub.events),1)
        self.assertEqual(after['id'],previous['id'])
        self.assertEqual(after['run'],1)
        with self.assertRaises(Problem):
            self.engine.assignment_input(self.ticket,after['id'],99)

    def test_lost_receipt_pins_original_candidate_through_independent_review(self):
        self.approve()
        self.store.change(self.ticket,lambda task:flow(task).update(workspace_base='prior'))
        row = self.launch('execute')
        candidate = self.store.root/'goal-attempts'/self.ticket/row['id']/'candidate'
        candidate.mkdir(parents=True)
        (candidate.parent/'entered').write_text(str(self.run_id))
        (candidate/'csv_check.py').write_bytes(b'new implementation')
        hub = HubDouble()
        hub.run_state = 'nothing'
        recovered = GoalRuntime(self.store,hub).tick(self.ticket)
        state = flow(recovered)
        self.assertEqual(state['workspace_base'],row['id'])
        expected = hashlib.sha256(b'new implementation').hexdigest()
        self.assertEqual(state['checkpoint']['files'],{'csv_check.py':expected})
        self.assertIsNone(state['assignments'][-1]['report'])
        review = self.launch('review')
        rejected = self.finish(review,{'passed':True,'findings':['检查通过']},
                               checkpoint=self.checkpoint(),validation=self.verification(True),
                               delivery={'assignment':review['id'],'files':self.checkpoint()['files']})
        self.assertEqual(rejected['status'],'blocked')
        self.assertIn('产物不一致',rejected['error'])

    def test_missing_candidate_cannot_fall_back_to_previous_implementation(self):
        self.approve()
        self.store.change(self.ticket,lambda task:flow(task).update(workspace_base='prior'))
        assignment = self.launch('execute')
        attempt = self.store.root/'goal-attempts'/self.ticket/assignment['id']
        attempt.mkdir(parents=True)
        (attempt/'entered').write_text(str(self.run_id))
        hub = HubDouble()
        hub.run_state = 'nothing'
        task = GoalRuntime(self.store,hub).tick(self.ticket)
        self.assertEqual(task['status'],'blocked')
        self.assertIn('缺少原派工的工作区',task['error'])

    def test_wrong_entry_cannot_claim_an_existing_artifact(self):
        self.approve()
        row = self.launch('execute')
        attempt = self.store.root/'goal-attempts'/self.ticket/row['id']
        attempt.mkdir(parents=True)
        (attempt/'entered').write_text(str(self.run_id-1))
        (self.store.workspace(self.ticket)/'csv_check.py').write_text('old file')
        hub = HubDouble()
        hub.run_state = 'nothing'
        task = GoalRuntime(self.store,hub).tick(self.ticket)
        self.assertEqual(task['status'],'blocked')
        self.assertIsNone(flow(task)['checkpoint'])

    def test_stopping_prevents_future_dispatch_until_explicit_resume(self):
        pending = current(self.engine.prepare(self.ticket))
        stopped = self.engine.stop(self.ticket)
        self.assertEqual(stopped['status'],'stopping')
        hub = HubDouble()
        GoalRuntime(self.store,hub).tick(self.ticket)
        self.assertEqual(self.store.get(self.ticket)['status'],'paused')
        self.assertFalse(hub.events)
        request = str(uuid.uuid4())
        self.engine.resume(self.ticket,request,1)
        self.assertNotEqual(current(self.engine.prepare(self.ticket))['id'],pending['id'])
        self.engine.stop(self.ticket)
        GoalRuntime(self.store,hub).tick(self.ticket)
        self.engine.resume(self.ticket,request,1)
        self.assertEqual(self.store.get(self.ticket)['status'],'paused')

    def test_stop_after_missing_transport_response_only_reconciles_existing_run(self):
        hub = HubDouble()
        hub.lose_response = True
        with self.assertRaises(Problem):
            GoalRuntime(self.store,hub).tick(self.ticket)
        self.engine.stop(self.ticket)
        runtime = GoalRuntime(self.store,hub)
        runtime.tick(self.ticket)
        runtime.tick(self.ticket)
        self.assertEqual(self.store.get(self.ticket)['status'],'paused')
        self.assertEqual(len(hub.events),1)

    def test_stop_after_effect_then_resume_reviews_the_existing_workspace(self):
        self.approve()
        row = self.launch('execute')
        self.engine.stop(self.ticket)
        with self.assertRaises(Problem):
            with self.engine.launch_gate(self.ticket,row['id'],self.run_id):
                self.fail('停止工单不能放行模型')
        stopped = self.finish(row,{'summary':'停办前已写入'},checkpoint=self.checkpoint())
        self.assertEqual(stopped['status'],'paused')
        self.engine.resume(self.ticket,str(uuid.uuid4()),1)
        self.launch('review')

    def test_mobile_binding_authorizes_same_ticket_and_rejects_later_conflict(self):
        from mobile_decision import answer, proposal, selected
        self.plan()
        wb = Workbench(store=self.store,hub=HubDouble(),goal_enabled=True)
        binding = proposal(selected(wb,self.ticket))['binding']
        self.assertIn('已送达',answer(wb,self.ticket,binding,0))
        self.assertIn('已处理',answer(wb,self.ticket,binding,1))
        state = flow(wb.store.get(self.ticket))
        self.assertEqual(state['mandate']['source'],'feishu')
        self.assertEqual(len([d for d in state['decisions'] if d['kind']=='authorized']),1)

    def test_mobile_pause_and_board_resume_return_to_the_same_approval(self):
        from mobile_decision import control, control_card, selected
        self.plan()
        wb = Workbench(store=self.store,hub=HubDouble(),goal_enabled=True)
        prior = selected(wb,self.ticket)
        binding = control_card(prior)['binding']
        self.assertEqual(binding['operation'],'pause')
        self.assertIn('已暂停',control(wb,self.ticket,binding,'om_stop'))
        paused = selected(wb,self.ticket)
        self.assertTrue(paused['goal_actions']['resume'])
        result = wb.resume(self.ticket,{'request_id':str(uuid.uuid4()),'stop_generation':1})
        self.assertEqual(result,{'queued':False,'awaiting_approval':True})
        with self.assertRaisesRegex(Problem,'暂停入口已过期'):
            wb.pause(self.ticket,source='feishu',binding=binding)
        task = self.store.get(self.ticket)
        self.assertEqual(task['approval']['id'],prior['approval']['id'])
        state = flow(task)
        self.assertIsNone(state['mandate'])
        stops = [item for item in state['decisions'] if item['kind']=='stop_requested']
        resumes = [item for item in state['decisions'] if item['kind']=='resumed']
        self.assertEqual([(r['source'],r['generation']) for r in stops+resumes],
                         [('feishu',1),('board',1)])
        self.store.change(self.ticket,lambda row: row.update(status='blocked'))
        blocked = selected(wb,self.ticket)
        self.assertEqual(blocked['goal_actions'],{'pause':False,'resume':False})
        self.assertNotIn('binding',control_card(blocked))

    def two_stage_ticket(self):
        contract = {'version':1,'stages':[
            {'id':'parse','checks':[{'id':'parse-output','script':'read_data.py','args':[],
                                      'files':{},'expected':{'lines':4}}]},
            {'id':'summarize','checks':[{'id':'summary-output','script':'report.py','args':[],
                                          'files':{},'expected':{'valid':3}}]},
        ]}
        engine = GoalEngine(self.store,contract)
        body = dict(self.body,request_id=str(uuid.uuid4()))
        task,_ = engine.create(body)
        self.ticket = task['id']
        plan = self.finish(self.launch('plan'),{'summary':'两阶段方案','steps':[
            {'objective':'解析','checks':['由可信验收契约确认']},
            {'objective':'输出报告','checks':['由可信验收契约确认']}
        ]},verifier_profile='contract-v1')
        self.assertEqual(plan['plan']['verification_digest'],digest(contract))
        engine.approve(self.ticket,plan['version'],plan['approval']['id'])
        return contract

    def verify_contract_phase(self, contract, ids, passed=True):
        row = self.launch('review')
        return self.finish(row,{'passed':passed,'findings':['按契约复验']},
                           checkpoint=self.checkpoint(),verifier_profile='contract-v1',
                           validation={'contract_digest':digest(contract),'passed':passed,
                                       'checks':[{'id':key,'passed':passed} for key in ids]},
                           delivery={'assignment':row['id'],'files':self.checkpoint()['files']})

    def test_final_review_requires_both_stages(self):
        contract = self.two_stage_ticket()
        self.finish(self.launch('execute'),{'summary':'解析器'},checkpoint=self.checkpoint())
        first = self.verify_contract_phase(contract,['parse-output'])
        self.assertEqual(first['step'],1)
        self.finish(self.launch('execute'),{'summary':'报告'},checkpoint=self.checkpoint())
        denied = self.verify_contract_phase(contract,['summary-output'])
        self.assertEqual(denied['status'],'blocked')
        self.assertNotIn('goal_delivery',denied)

    def test_complete_contract_checks_hold_through_final_delivery(self):
        contract = self.two_stage_ticket()
        self.finish(self.launch('execute'),{'summary':'解析器'},checkpoint=self.checkpoint())
        self.verify_contract_phase(contract,['parse-output'])
        self.finish(self.launch('execute'),{'summary':'报告'},checkpoint=self.checkpoint())
        done = self.verify_contract_phase(contract,['parse-output','summary-output'])
        self.assertEqual(done['status'],'completed')
        self.assertEqual([check['id'] for check in done['checks']],['parse-output','summary-output'])


if __name__ == '__main__':
    unittest.main()
