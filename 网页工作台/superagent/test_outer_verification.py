"""V2 契约、真实 JSON 产物核验与独立派工恢复回归。"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from superagent import outer_verification as registry
from superagent.goal_engine import MODEL, GoalEngine, current, flow
from superagent.goal_runtime import execute_assignment
from superagent.outer_intake import OuterIntake
from workbench import Store


def criterion(key='one', **updates):
    return {
        'criterion_id':key,'requirement_ids':[f'requirement-{key}'],'description':f'实际 JSON 产物 {key}',
        'required':True,'verification_type':'DETERMINISTIC','verifier_id':'json-artifact','verifier_version':1,
        'test_spec':{'path':f'{key}.json'},'expected_result':{'ok':True},'evidence_required':['运行结果'],
        'human_review_required':False,'source_refs':[], **updates,
    }


def compile_rows(*criteria, goal_fields=None):
    goal = {
        'revision':1,'ticket_id':'out-1','goal_statement':'生成结构化结果',
        'required_capabilities':[{'requirement_id':f"requirement-{row['criterion_id']}"} for row in criteria],
    }
    goal.update(goal_fields or {})
    contract = registry.compile_proposal(goal,{'goal_revision':1,'ticket_id':'out-1','criteria':list(criteria)})
    return goal, contract


class RegistryTests(unittest.TestCase):
    def test_rejects_paths_arbitrary_shell_and_unregistered_versions(self):
        for proposal in (
            criterion(test_spec={'path':'/private/example.json'}),
            criterion(test_spec={'path':'../../example.json'}),
            criterion(verifier_id='shell',test_spec={'command':'echo no'}),
            criterion(verifier_version=True),
            criterion(verifier_version=2),
            criterion(evidence_required=[]),
            criterion(requirement_ids=['missing']),
        ):
            with self.subTest(proposal=proposal), self.assertRaises(ValueError):
                compile_rows(proposal)

    def test_required_human_and_coverage_gaps_cannot_compile_as_ready(self):
        manual = criterion(verifier_id='human-review',verification_type='HUMAN_REVIEW',test_spec={})
        goal, contract = compile_rows(manual)
        self.assertEqual(contract['status'],'VERIFICATION_GAP')
        self.assertTrue(contract['criteria'][0]['human_review_required'])
        optional = dict(criterion(),required=False)
        self.assertEqual(compile_rows(optional)[1]['coverage_assessment']['uncovered'],['requirement-one'])
        without_criterion = registry.compile_proposal(goal,{'goal_revision':1,'criteria':[]})
        self.assertEqual(without_criterion['status'],'VERIFICATION_GAP')

    def test_static_artifact_and_constant_cli_cannot_discharge_running_requirement(self):
        req = {'requirement_id':'requirement-one','description':'命令行工具可运行并转换文本'}
        goal = {'revision':1,'required_capabilities':[req],'goal_statement':'实现 CLI'}
        self.assertEqual(registry.compile_proposal(goal,{'goal_revision':1,'criteria':[criterion()]})['status'],
                         'VERIFICATION_GAP')
        with self.assertRaises(ValueError):
            registry.compile_proposal(goal,{'goal_revision':1,'criteria':[criterion(expected_result=True)]})
        cli = [criterion(str(index),verifier_id='python-json-cli',requirement_ids=['requirement-one'],
                         test_spec={'script':'tool.py','args':[f'input-{index}'],'files':{}},
                         expected_result=True) for index in (1,2)]
        self.assertEqual(registry.compile_proposal(goal,{'goal_revision':1,'criteria':cli})['status'],
                         'VERIFICATION_GAP')
        cli[0]['expected_result'] = False
        with patch.object(registry,'environment_capabilities',return_value=[registry.ENVIRONMENT]):
            self.assertEqual(registry.compile_proposal(goal,{'goal_revision':1,'criteria':cli})['status'],
                             'READY_FOR_APPROVAL')
        goal['goal_statement'] = '浏览器交互后点击提交并检查结果'
        self.assertEqual(registry.compile_proposal(goal,{'goal_revision':1,'criteria':cli})['status'],
                         'VERIFICATION_GAP')

    def test_unknown_explicit_constraint_cannot_be_discharged_by_unrelated_proof(self):
        goal, _ = compile_rows(criterion())
        goal['constraints'] = [
            {'requirement_id':'privacy','description':'运行时不得记录任何个人信息','source':'USER_EXPLICIT'},
            {'requirement_id':'summary','description':'建议加图表','source':'MODEL_PROPOSED'},
        ]
        for row in (criterion(), criterion(requirement_ids=['requirement-one','privacy'])):
            with self.subTest(row=row):
                compiled = registry.compile_proposal(goal,{'goal_revision':1,'criteria':[row]})
                self.assertEqual(compiled['status'],'VERIFICATION_GAP')
                self.assertEqual(compiled['coverage_assessment']['uncovered'],['privacy'])

    def test_english_execution_and_interaction_requirements_are_detected(self):
        goal, _ = compile_rows(criterion())
        for desc in ("Build a runnable command-line utility.", "Implement an API endpoint."):
            with self.subTest(desc=desc):
                goal['goal_statement'] = desc
                self.assertEqual(registry.compile_proposal(goal,{'goal_revision':1,'criteria':[criterion()]})['status'],
                                 'VERIFICATION_GAP')

    def test_catalogue_returns_detached_versioned_capabilities(self):
        snapshot = registry.catalogue()
        self.assertEqual(snapshot[0]['version'],1)
        self.assertIn('resource_limits',snapshot[0])
        snapshot[0]['resource_limits'].clear()
        self.assertTrue(registry.catalogue()[0]['resource_limits'])


class OuterExecutionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name).resolve())
        self.engine = GoalEngine(self.store)
        task, _ = self.engine.create({'name':'目标','goal':'生成 JSON','request_id':str(uuid4())})
        self.ticket = task['id']
        self.workspace = self.store.workspace(self.ticket)
        self.sequence = 0

    def authorize(self, *criteria, goal_fields=None, forbidden=None):
        goal, contract = compile_rows(*criteria,goal_fields=goal_fields)
        forbidden = ['network','publish'] if forbidden is None else forbidden
        def apply(task):
            flow(task)['outer'] = {
                'ticket_id':self.ticket,'goal_revision':1,'acceptance_revision':1,'mandate_revision':1,
                'goal_contract':dict(goal,status='APPROVED',ticket_id=self.ticket),
                'acceptance_contract':dict(contract,status='APPROVED',ticket_id=self.ticket,revision=1),
                'approval_event_id':'approval','actor_id':'owner','correlation_id':'event',
                'mandate':{'revision':1,'approval_event_id':'approval','actor_id':'owner',
                           'ticket_id':self.ticket,'goal_revision':1,'acceptance_revision':1,
                           'budget_seconds':600,'workspace':str(self.workspace),'scope':'工作区离线读写',
                           'forbidden_operations':forbidden},
            }
        self.store.change(self.ticket,apply)

    def launch(self, expected):
        task = self.engine.prepare(self.ticket)
        row = current(task)
        self.assertIsNotNone(row,task['error'])
        self.assertEqual(row['phase'],expected)
        self.sequence += 1
        self.engine.bind(self.ticket,row['id'],self.sequence,self.sequence)
        return self.engine.assignment_input(self.ticket,row['id'],self.sequence)

    def finish(self, assignment, result, **updates):
        row = {
            **{key:assignment[key] for key in ('id','run','version','outer_binding')},
            'evidence':{'session':f"session-{assignment['run']}",'model':MODEL,
                        'started_ns':0,'ended_ns':1_000_000_000},
            'result':result, **updates,
        }
        return self.engine.settle(self.ticket,assignment['id'],assignment['run'],'done',row)

    def plan(self, *stages):
        return self.finish(self.launch('plan'),{
            'summary':'真实实现及独立验收',
            'steps':[{'objective':f'实施阶段 {index}','checks':ids} for index,ids in enumerate(stages,1)],
        })

    def checkpoint(self, phase='execute'):
        assignment = self.launch(phase)
        return self.finish(assignment,{'summary':'执行者自报已完成'},
                           checkpoint=registry.capture(self.workspace)[0])

    def trusted_review(self, assignment):
        output = self.store.root / 'attempts' / assignment['id']
        output.mkdir(parents=True)
        return execute_assignment(self.store,assignment,output,None)

    def settle_report(self, assignment, row):
        return self.engine.settle(self.ticket,assignment['id'],assignment['run'],'done',row)

    def test_approved_goal_requirements_reach_actual_executor_input(self):
        from superagent.goal_codex import _prepare
        from superagent.goal_runtime import prepare_outer_execution
        fields = {
            'required_capabilities':[{'requirement_id':'requirement-one','description':'交付结构化结果'}],
            'nonfunctional_requirements':[{'requirement_id':'data-shape','description':'UTF-8 JSON'}],
            'constraints':[{'requirement_id':'scope','description':'只允许写入工单工作区',
                            'source':'USER_CONFIRMED','source_refs':['approval']}],
            'out_of_scope':[{'requirement_id':'gui','description':'不提供图形客户端'}],
        }
        self.authorize(criterion(requirement_ids=['requirement-one','data-shape']),goal_fields=fields,
                       forbidden=['write_outside_workspace'])
        self.assertEqual(self.plan(['one'])['status'],'queued')
        assignment = self.launch('execute')
        prepare_outer_execution(assignment)
        _, _, _, prompt = _prepare(assignment)
        payload = json.loads(prompt.split(b'\n',1)[1])
        approved = payload['context']['approved_goal_contract']
        for field,value in fields.items():
            self.assertEqual(approved[field],value)
        self.assertEqual(payload['mandate']['forbidden_operations'],['write_outside_workspace'])
        self.assertEqual(payload['context']['execution_contract']['scope'],payload['mandate']['scope'])
        self.assertEqual(assignment['outer_acceptance']['coverage_assessment']['authorization_controls'],
                         {'scope':'write_outside_workspace'})

    def test_configured_legacy_worker_restricts_outer_plan_to_approved_criteria(self):
        self.authorize(criterion())
        assignment = self.launch('plan')
        with patch('superagent.goal_codex.invoke', return_value={'result':{}}) as invoke:
            report = execute_assignment(self.store,assignment,self.store.root,'csv-v1')
        self.assertNotIn('error',report)
        context = invoke.call_args.args[0]['context']
        self.assertNotIn('fixture_contract',context)
        self.assertEqual([row['criterion_id'] for row in context['approved_criteria']],['one'])

    def test_engine_rejects_missing_hard_control_for_workspace_constraint(self):
        fields = {'constraints':[{'requirement_id':'scope','description':'不得写入工单工作区之外',
                                  'source':'USER_EXPLICIT'}]}
        self.authorize(criterion(),goal_fields=fields)
        task = self.engine.prepare(self.ticket)
        self.assertEqual(task['status'],'blocked')
        self.assertIsNone(current(task))

    def test_current_required_checks_and_immutable_snapshot_gate_completion(self):
        self.authorize(criterion())
        task = self.plan(['one'])
        self.assertEqual(task['status'],'queued')
        self.assertIsNone(task['approval'])
        (self.workspace/'one.json').write_text('{"ok":true}')
        self.checkpoint()
        assignment = self.launch('review')
        row = self.trusted_review(assignment)
        self.assertNotIn('error',row,row)
        task = self.settle_report(assignment,row)
        self.assertEqual(task['status'],'completed')
        self.assertEqual(flow(task)['outcome'],'VERIFIED_COMPLETE')
        self.assertEqual(task['checks'][0]['artifact_sha256'],task['goal_delivery']['artifact_sha256'])
        self.assertEqual(task['checks'][0]['run'],assignment['run'])

    def test_static_html_verified_from_actual_document(self):
        html = criterion(verifier_id='html-document',test_spec={'path':'index.html'},
                         expected_result={'title':'数据周报','headings':['总览'],'element_ids':['numbers']})
        self.authorize(html)
        self.plan(['one'])
        file = self.workspace/'index.html'
        file.write_text('<!doctype html><title>数据周报</title><h1>总览</h1><div id="numbers"></div>',
                        encoding='utf-8')
        self.checkpoint()
        assignment = self.launch('review')
        result = self.trusted_review(assignment)
        self.assertNotIn('error',result,result)
        self.assertEqual(result['validation']['checks'][0]['status'],'PASS')
        self.assertEqual(self.settle_report(assignment,result)['status'],'completed')
        # 相同内容的普通文本或重复 ID 无法作为静态语义证据。
        contract = assignment['outer_acceptance']
        row = contract['criteria'][0]
        for bad in (b'Numbers', b'<title>wrong</title><div id="numbers"></div>',
                    '<title>数据周报</title><h1>总览</h1><div id="numbers"></div><i id="numbers"></i>'.encode()):
            self.assertEqual(registry._criterion_result(row,{'index.html':bad},{})[0],'FAIL')

    def test_unapproved_revision_and_mandate_workspace_are_rejected(self):
        self.authorize(criterion())
        self.store.change(self.ticket,lambda task:flow(task)['outer']['mandate'].update(workspace='/wrong/path'))
        task = self.engine.prepare(self.ticket)
        self.assertEqual(task['status'],'blocked')
        self.assertIsNone(current(task))

    def test_review_preserves_worker_python_and_validates_other_environment_fields(self):
        self.authorize(criterion())
        self.plan(['one'])
        (self.workspace/'one.json').write_text('{"ok":true}')
        self.checkpoint()
        assignment = self.launch('review')
        with patch.object(registry.platform,'python_version',return_value='3.9.6'):
            report = self.trusted_review(assignment)
        self.assertNotIn('error',report,report)
        rows = report['validation']['checks']
        self.assertEqual(rows[0]['environment']['python'],'3.9.6')
        args = (assignment['outer_acceptance'],assignment['plan'],assignment,report['checkpoint'])
        with patch.object(registry.platform,'python_version',return_value='3.11.9'):
            for field, value in (
                ('profile','forged-profile'), ('platform','unknown'), ('os_release','99.0'),
                ('python','forged-interpreter'), ('extra','unexpected'),
            ):
                with self.subTest(field=field):
                    row = dict(rows[0],environment=dict(rows[0]['environment'],**{field:value}))
                    body = {key:item for key,item in row.items() if key not in ('id','passed','evidence_ref')}
                    row['evidence_ref'] = f"sha256:{registry.sha256(body)}"
                    with self.assertRaises(ValueError):
                        registry.validate_results(*args,[row])
            changed = dict(rows[0],environment=dict(rows[0]['environment'],python='3.12.4'))
            with self.assertRaisesRegex(ValueError,'缺少有效证据'):
                registry.validate_results(*args,[changed])
            task = self.settle_report(assignment,report)
        self.assertEqual(task['status'],'completed')
        self.assertEqual(task['checks'][0]['environment']['python'],'3.9.6')

    def test_tampered_result_and_model_only_pass_are_rejected(self):
        self.authorize(criterion())
        self.plan(['one'])
        (self.workspace/'one.json').write_text('{"ok":false}')
        self.checkpoint()
        assignment = self.launch('review')
        row = self.trusted_review(assignment)
        self.assertEqual(row['validation']['checks'][0]['status'],'FAIL')
        row['validation']['checks'][0].update(status='PASS',passed=True)
        row['result'] = {'passed':True}
        task = self.settle_report(assignment,row)
        self.assertEqual(task['status'],'blocked')
        self.assertIn('证据',task['error'])

    def test_final_review_reruns_prior_stages_and_repairs_regression(self):
        self.authorize(criterion(),criterion('two'))
        self.plan(['one'],['two'])
        (self.workspace/'one.json').write_text('{"ok":true}')
        self.checkpoint()
        first = self.launch('review')
        first_report = self.trusted_review(first)
        task = self.settle_report(first,first_report)
        self.assertEqual(task['step'],1)
        self.assertEqual(task['status'],'queued')
        self.assertEqual(flow(task)['outcome'],'PARTIAL_DELIVERY')
        self.assertEqual(flow(task)['stage_deliveries'][0]['assignment'],first['id'])
        (self.workspace/'one.json').write_text('{"ok":false}')
        (self.workspace/'two.json').write_text('{"ok":true}')
        self.checkpoint()
        second = self.launch('review')
        task = self.settle_report(second,self.trusted_review(second))
        self.assertEqual(flow(task)['stage'],'coordinate')
        self.assertEqual([r['status'] for r in flow(task)['validation']['checks']],['FAIL','PASS'])
        coordinator = self.launch('coordinate')
        context = coordinator['context']['coordinator']
        snapshot = context['input_snapshot_revision']
        proposal = {'schema_version':1,'input_snapshot_revision':snapshot,'action':'REPAIR',
                    'reason':'修复前序回归','evidence_refs':context['evidence_refs'],'plan_change':'',
                    'retained_outputs':['two.json'],'scope_change':False,'approval_required':False,
                    'remaining_risk':'需要重新跑全部验收'}
        task = self.finish(coordinator,proposal,evidence={'snapshot_revision':snapshot,
                                                         'invocation_id':coordinator['id']})
        self.assertEqual(flow(task)['stage'],'repair')
        (self.workspace/'one.json').write_text('{"ok":true}')
        self.checkpoint('repair')
        review = self.launch('review')
        task = self.settle_report(review,self.trusted_review(review))
        self.assertEqual(task['status'],'completed')
        self.assertEqual(flow(task)['revisions'][0]['plan']['steps'][0]['state'],'pending')

    def test_coordinator_actual_adapter_envelope_and_launch_gate(self):
        from superagent.test_goal_codex import _events
        self.authorize(criterion())
        self.plan(['one'])
        (self.workspace/'one.json').write_text('{"ok":false}')
        self.checkpoint()
        review = self.launch('review')
        self.settle_report(review,self.trusted_review(review))
        assignment = self.launch('coordinate')
        context = assignment['context']['coordinator']
        proposal = {'schema_version':1,'input_snapshot_revision':context['input_snapshot_revision'],
                    'action':'REPLAN','reason':'实施思路不符合预期','evidence_refs':context['evidence_refs'],
                    'plan_change':'按已批准条件调整实现','retained_outputs':['one.json'],
                    'scope_change':False,'approval_required':False,'remaining_risk':'仍须复验'}

        def subprocess_protocol(argv, prompt, workspace, output_dir, staging, env, timeout, gate):
            self.assertIn('shell_tool',argv)
            self.assertLessEqual(timeout,assignment['max_seconds'])
            self.assertFalse((workspace/'one.json').exists())
            with gate():
                self.assertIn(context['input_snapshot_revision'],prompt.decode())
            lines = "\n".join(json.dumps(event) for event in _events(proposal))
            return lines.encode(),b'',0,0,1_000_000_000

        with patch('superagent.goal_codex._run',side_effect=subprocess_protocol) as run:
            row = self.trusted_review(assignment)
            run.assert_called_once()
        self.assertNotIn('error',row,row)
        task = self.settle_report(assignment,row)
        self.assertEqual(flow(task)['stage'],'replan')
        self.assertEqual(flow(task)['decisions'][-1]['invocation']['usage']['input_tokens'],3)
        plan = self.launch('replan')
        task = self.finish(plan,{'summary':'新版实施方向','steps':[{'objective':'修复','checks':['one']}]})
        self.assertEqual(task['status'],'queued')
        self.assertEqual(task['version'],2)
        self.assertEqual(flow(task)['revisions'][-1]['previous'],1)
        self.assertEqual(flow(task)['outer']['acceptance_revision'],1)

    def test_pause_delays_final_transition_and_old_review_cannot_be_reused(self):
        self.authorize(criterion())
        self.plan(['one'])
        (self.workspace/'one.json').write_text('{"ok":true}')
        self.checkpoint()
        review = self.launch('review')
        report = self.trusted_review(review)
        ticket = {'id':self.ticket,'goal_revision':1,'status_before_pause':'AUTHORIZED'}
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            OuterIntake._control_execution(db,ticket,'pause','web','pause')
        task = self.settle_report(review,report)
        self.assertEqual(task['status'],'paused')
        self.assertEqual(task['step'],0)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            OuterIntake._control_execution(db,ticket,'resume','web','resume')
        resumed = self.launch('review')
        task = self.settle_report(resumed,report)
        self.assertNotEqual(task['status'],'completed')
        self.assertIsNone(flow(task)['assignments'][-1]['report'])

    def test_scope_expansion_and_fabricated_coordinator_evidence_rejected(self):
        for extra in ({'scope_change':True},{'evidence_refs':['unknown']}):
            with self.subTest(extra=extra):
                context = {'input_snapshot_revision':'checkpoint','available_actions':['REPAIR'],
                           'evidence_refs':['evidence'],'retained_outputs':[]}
                proposal = {'schema_version':1,'input_snapshot_revision':'checkpoint','action':'REPAIR',
                            'reason':'下一步','evidence_refs':['evidence'],'plan_change':'',
                            'retained_outputs':[],'scope_change':False,'approval_required':False,
                            'remaining_risk':'需复验', **extra}
                with self.assertRaises(ValueError):
                    registry.validate_coordination(proposal,context)

    @unittest.skipUnless(os.environ.get("GOAL_CHECKS_REAL_SEATBELT") == "1",
                         "requires a host session that can apply macOS Seatbelt")
    def test_real_python_cli_actual_behavior(self):
        rows = [criterion(key,verifier_id='python-json-cli',
                          test_spec={'script':'report.py','args':['input.txt'],'files':{'input.txt':value}},
                          expected_result={'result':value.upper()}) for key,value in [('one','alpha'),('two','beta')]]
        self.authorize(*rows)
        self.plan(['one','two'])
        (self.workspace/'report.py').write_text(
            'from pathlib import Path\nimport json, sys\n'
            'print(json.dumps({"result": Path(sys.argv[1]).read_text().upper()}))\n')
        self.checkpoint()
        review = self.launch('review')
        receipt = self.trusted_review(review)
        self.assertEqual([row['status'] for row in receipt['validation']['checks']],['PASS','PASS'],receipt)
        self.assertEqual(self.settle_report(review,receipt)['status'],'completed')
        # 可运行但固定输出、语法错误和缺少入口都无法通过。
        contract = review['outer_acceptance']
        script = self.workspace/'report.py'
        for body, expected in (
            ("""print('{"result":"ALPHA"}')""", ['PASS','FAIL']),
            ('this is not valid code !!', ['FAIL','FAIL']),
            (None, ['FAIL','FAIL']),
        ):
            if body is None:
                script.unlink()
            else:
                script.write_text(body)
            checkpoint, files, occupied = registry.capture(self.workspace)
            criteria_results = registry.verify(contract,review,checkpoint,files,occupied)
            self.assertEqual([row['status'] for row in criteria_results],expected,criteria_results)
