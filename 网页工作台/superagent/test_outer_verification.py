"""V2 契约、真实 JSON 产物核验与独立派工恢复回归。"""

import json
import os
from io import BytesIO
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import Mock, patch
from uuid import uuid4

from superagent import outer_verification as registry
from superagent.goal_engine import MODEL, GoalEngine, current, digest, flow
from superagent.goal_runtime import (
    TEAM_RECOVERY_FILE, GoalRuntime, attempt_file, execute_assignment, save_json, save_team_recovery, team_samples,
    worker,
)
from superagent.goal_team_handoff import make_handoff_verification, select_handoff_scope
from superagent.goal_team_records import PROFILE
from superagent.goal_team_repair import candidate_verification, objective_checks, prepare_scope
from superagent.goal_team_run import materialize, seal, sealed_workspace
from superagent.outer_intake import OuterIntake
from server import Handler
from workbench import Problem, Store, Workbench


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

    def test_explicit_required_human_is_approvable_without_weakening_machine_gaps(self):
        manual = criterion(verifier_id='human-review',verification_type='HUMAN_REVIEW',test_spec={},
                           expected_result=None,human_review_required=True)
        goal, contract = compile_rows(manual)
        self.assertEqual(contract['status'],'READY_FOR_APPROVAL')
        self.assertEqual(contract['human_review_requirements'],[manual['description']])
        self.assertTrue(registry.needs_human_signoff(contract))
        self.assertFalse(registry.needs_human_signoff(compile_rows(criterion())[1]))
        self.assertTrue(registry.needs_human_signoff(dict(compile_rows(criterion())[1],require_signoff=True)))
        optional = dict(criterion(),required=False)
        self.assertEqual(compile_rows(optional)[1]['coverage_assessment']['uncovered'],['requirement-one'])
        without_criterion = registry.compile_proposal(goal,{'goal_revision':1,'criteria':[]})
        self.assertEqual(without_criterion['status'],'VERIFICATION_GAP')
        for invalid in (dict(manual,expected_result=True),dict(manual,human_review_required=False),
                        dict(manual,verifier_version=True),dict(manual,test_spec={'script':'manual.py'})):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                compile_rows(invalid)
        _, model_only = compile_rows(dict(manual,verification_type='MODEL_REVIEW'))
        self.assertEqual(model_only['status'],'VERIFICATION_GAP')

    def test_bare_human_prose_must_map_to_a_required_review_criterion(self):
        machine = criterion()
        manual = criterion('manual',verifier_id='human-review',verification_type='HUMAN_REVIEW',
                           test_spec={},expected_result=None,human_review_required=True)
        goal, _ = compile_rows(machine,manual)
        for text,status in (('manual','READY_FOR_APPROVAL'),(manual['description'],'READY_FOR_APPROVAL'),
                            ('尚未登记的主观判断','VERIFICATION_GAP')):
            with self.subTest(text=text):
                contract = registry.compile_proposal(goal,{'goal_revision':1,'criteria':[machine,manual],
                                                           'human_review_requirements':[text]})
                self.assertEqual(contract['status'],status)

    def test_worker_archives_machine_pass_and_never_passes_manual_criterion(self):
        from superagent.goal_runtime import execute_outer_review
        manual = criterion('manual',verifier_id='human-review',verification_type='HUMAN_REVIEW',
                           test_spec={},expected_result=None,human_review_required=True)
        _, contract = compile_rows(criterion(),manual)
        with tempfile.TemporaryDirectory(dir='/private/tmp') as temp:
            workspace = Path(temp,'candidate')
            workspace.mkdir()
            (workspace/'one.json').write_text('{"ok": true}')
            plan = {'steps':[{'acceptance':['one','manual']}]}
            checkpoint = registry.capture(workspace)[0]
            assignment = {'id':'run-one','run':1,'version':1,'step':0,'plan':plan,
                          'outer_binding':{'goal_revision':1,'acceptance_revision':1,'mandate_revision':1},
                          'outer_acceptance':contract,'checkpoint':checkpoint}
            root = Path(temp,'reports')
            root.mkdir()
            report = execute_outer_review(assignment,workspace,root,{})
            self.assertEqual(report['result'],{'passed':True})
            self.assertEqual(report['delivery']['artifact_sha256'],checkpoint['digest'])
            checks = report['validation']['checks']
            self.assertEqual([row['status'] for row in checks],['PASS','INCONCLUSIVE'])
            forged = dict(checks[1],status='PASS',passed=True)
            signed = {key:value for key,value in forged.items() if key not in ('evidence_ref','id','passed')}
            forged['evidence_ref'] = 'sha256:'+registry.sha256(signed)
            with self.assertRaisesRegex(ValueError,'不得代替人工'):
                registry.validate_results(contract,plan,assignment,checkpoint,[checks[0],forged])
            (workspace/'one.json').write_text('{"ok":false}')
            changed,files,occupied = registry.capture(workspace)
            rejected = registry.verify(contract,assignment,changed,files,occupied)
            self.assertFalse(registry.validate_results(contract,plan,assignment,changed,rejected))

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

    def authorize(self, *criteria, goal_fields=None, forbidden=None, require_signoff=False):
        goal, contract = compile_rows(*criteria,goal_fields=goal_fields)
        forbidden = ['network','publish'] if forbidden is None else forbidden
        def apply(task):
            flow(task)['outer'] = {
                'ticket_id':self.ticket,'goal_revision':1,'acceptance_revision':1,'mandate_revision':1,
                'goal_contract':dict(goal,status='APPROVED',ticket_id=self.ticket),
                'acceptance_contract':dict(contract,status='APPROVED',ticket_id=self.ticket,revision=1,
                                           require_signoff=require_signoff),
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

    def launch_team_candidate(self):
        self.authorize(criterion())
        self.store.change(self.ticket,lambda task:flow(task)['outer']['mandate'].update(execution_profile=PROFILE))
        self.plan(['one'])
        assignment = self.launch('execute')
        directory = attempt_file(self.store.root,self.ticket,assignment['id'],'report.json').parent
        return assignment,directory

    def close_team_role(self, assignment, *, sequence=1, role='lead', writable=True, action='done',
                        timing=True, status='completed',
                        **evidence):
        self.engine.reserve_team_role(assignment,sequence,role,writable,'medium')
        elapsed = {'started_ns':1_000_000_000,'ended_ns':3_000_000_000} if timing else {}
        self.engine.finish_team_role(assignment,sequence,{
            'session':f"session-{assignment['run']}-{sequence}", 'turn':f'turn-{sequence}',
            'model':MODEL, 'action':action, **elapsed, **evidence,
        },status=status)

    def make_team_report(self, assignment, directory, *, result=None, content='{"ok":true}', files=None):
        candidate = directory/'candidate'
        directory.mkdir(parents=True,exist_ok=True)
        materialize({'one.json':content.encode()} if files is None else files,candidate)
        (directory/'entered').write_text(str(assignment['run']))
        return {
            **{key:assignment[key] for key in ('id','run','version','outer_binding')},
            'workspace_id':assignment['id'], 'checkpoint':seal(candidate,directory),
            'result':result or dict(summary='已生成 JSON',action='done',options=[],retain=[],verify=[]),
        }

    def settle_team_from_disk(self, assignment, transport):
        hub = Mock()
        hub.request.return_value = {'event':assignment['event'], 'runs':[
            {'rule':'odyssey-goal','state':transport,'run':assignment['run']},
        ]}
        return GoalRuntime(Store(self.store.root),hub).tick(self.ticket)

    def staged_team_plan(self, mode, *stages):
        self.authorize(*(criterion(name) for name in stages))
        self.store.change(self.ticket,lambda task:flow(task)['outer']['mandate'].update(
            execution_profile=PROFILE,team_review_mode=mode))
        return self.plan(*([name] for name in stages))

    def launch_team_stage(self):
        assignment = self.launch('execute')
        return assignment,attempt_file(self.store.root,self.ticket,assignment['id'],'report.json').parent

    @staticmethod
    def candidate_read_tool(checkpoint, paths):
        return dict(name='shell',settled=True,paired=True,status='completed',exit_code=0,
                    candidate_reads=[dict(path=path,sha256=checkpoint['files'][path]) for path in paths])

    def write_team_handoff(self, assignment, directory, *, ask=False, quote='true', recover=False, extra=(), assets=None,
                           keep=False):
        instruction = dict(action='ask' if ask else 'done', summary='选择下一版方向' if ask else '继续完善草稿',
                           options=[{'id':str(index),'label':f'方向 {index}','text':f'样例 {index}'}
                                    for index in range(3)] if ask else [], retain=[], verify=[])
        candidate_base = assignment.get('candidate_base')
        files = registry.capture(sealed_workspace(directory,candidate_base))[1] if candidate_base else {}
        for name in ('one.json', *extra):
            files.setdefault(name,b'{"ok":true}')
        files.update(assets or {})
        target = extra[-1] if extra else 'one.json'
        instruction['handoff_scope'] = {'variants':[
            dict(option_id=option,mode='edit',retained=[dict(path=name,excerpt='') for name in files if name != target],
                 changes=[dict(path=target,before=files[target].decode(),after='{"ok":'+' '*(index+1)+'true}')],
                 removed_paths=[])
            for index,option in enumerate([row['id'] for row in instruction['options']] if ask else [''])
        ]}
        if keep:
            instruction['options'][0]['label'] = '沿用现稿'
            instruction['handoff_scope']['variants'][0].update(mode='keep', retained=[], changes=[])
        report = self.make_team_report(assignment,directory,result=instruction,files=files)
        checkpoint = report['checkpoint']
        baseline = (candidate_base or {}).get('artifact_sha256',registry.sha256([]))
        comparison = dict(action='done', summary='对比两份意见后产出草稿', options=[],retain=[],verify=[])
        review = dict(action='done', summary='独立核对草稿内容', options=[],retain=[],verify=[],
                      observations=[{'path':target,'excerpt':quote,'issue':'确认布尔字段'}])
        self.close_team_role(assignment,writable=False,candidate_sha256=baseline)
        self.close_team_role(assignment,sequence=2,role='advisor',writable=False,candidate_sha256=baseline)
        self.close_team_role(assignment,sequence=3,candidate_sha256=checkpoint['digest'],**comparison)
        self.close_team_role(assignment,sequence=4,role='advisor',writable=False,
                             candidate_sha256=checkpoint['digest'],**review,
                             tools=[self.candidate_read_tool(checkpoint,[target])])
        self.close_team_role(assignment,sequence=5,writable=False,candidate_sha256=checkpoint['digest'],**instruction)
        report['team_handoff'] = dict(comparison=comparison,draft_review=review,instruction=instruction,
                                      draft_sha256=checkpoint['digest'])
        if ask:
            save_json(directory/'samples.json',team_samples(report))
        if recover:
            save_team_recovery(assignment,directory,report)
        else:
            save_json(directory/'report.json',report)
        return report

    @staticmethod
    def handoff_source(assignment, directory):
        origin = assignment.get('team_handoff_origin') or {
            'handoff':assignment['team_handoff_continuation'], 'base':assignment['candidate_base'],
        }
        before, files, _ = registry.capture(sealed_workspace(directory,origin['base']))
        return origin['handoff']['instruction'], before, files

    def make_handoff_continuation_report(self, assignment, directory):
        instruction,before,files = self.handoff_source(assignment,directory)
        selected = select_handoff_scope(assignment,instruction,before,files)
        after_files = dict(files)
        for change in selected['scope']['changes']:
            after_files[change['path']] = change['after'].encode()
        report = self.make_team_report(assignment,directory,files=after_files)
        report['handoff_verification'] = make_handoff_verification(
            assignment,instruction,before,files,report['checkpoint'],after_files)
        self.close_team_role(assignment,role='implementer')
        checkpoint = report['checkpoint']
        self.close_team_role(assignment,sequence=2,writable=False,candidate_sha256=checkpoint['digest'],
                             tools=[self.candidate_read_tool(checkpoint,after_files)])
        return report

    def finish_team_stage(self, assignment, directory):
        if assignment.get('team_handoff_continuation'):
            report = self.make_handoff_continuation_report(assignment,directory)
        else:
            report = self.make_team_report(assignment,directory)
            self.close_team_role(assignment)
        save_json(directory/'report.json',report)
        task = self.settle_team_from_disk(assignment,'done')
        self.assertEqual(flow(task)['stage'],'review',task['error'])
        review = self.launch('review')
        directory = attempt_file(self.store.root,self.ticket,review['id'],'report.json').parent
        directory.mkdir(parents=True)
        return self.settle_report(review,execute_assignment(self.store,review,directory,None))

    def test_team_review_recovers_handoff_without_promoting_draft(self):
        self.staged_team_plan('review','one')
        assignment,directory = self.launch_team_stage()
        self.assertEqual(assignment['max_seconds'],330)
        report = self.write_team_handoff(assignment,directory,recover=True)
        task = self.settle_team_from_disk(assignment,'nothing')
        state = flow(task)
        self.assertIsNone(state.get('active_candidate'))
        self.assertIsNone(state['checkpoint'])
        self.assertEqual(state['assignments'][-1]['receipt_source'],'recovery')
        self.assertEqual(state['team_pending_handoff']['handoff'],report['team_handoff'])
        following,directory = self.launch_team_stage()
        self.assertEqual(following['max_seconds'],240)
        self.assertEqual(following['candidate_base']['assignment'],assignment['id'])
        self.assertEqual(following['team_handoff_continuation'],report['team_handoff'])
        self.assertIsNone(following['preference_answer'])
        completed = self.finish_team_stage(following,directory)
        self.assertEqual(completed['status'],'completed')
        self.assertNotIn('team_pending_handoff',flow(completed))
        self.assertEqual(flow(completed)['active_candidate']['assignment'],following['id'])

    def test_team_review_rejects_handoff_with_unverifiable_excerpt(self):
        self.staged_team_plan('review','one')
        assignment,directory = self.launch_team_stage()
        self.write_team_handoff(assignment,directory,quote='文中没有的引文')
        task = self.settle_team_from_disk(assignment,'done')
        self.assertEqual(task['status'],'blocked')
        self.assertIsNone(flow(task).get('team_pending_handoff'))
        self.assertIsNone(flow(task).get('active_candidate'))

    def test_team_review_rejects_completed_result_without_handoff(self):
        self.staged_team_plan('review','one')
        assignment,directory = self.launch_team_stage()
        self.close_team_role(assignment)
        report = self.make_team_report(assignment,directory)
        save_team_recovery(assignment,directory,report)
        task = self.settle_team_from_disk(assignment,'nothing')
        self.assertEqual(task['status'],'blocked')
        self.assertIsNone(flow(task).get('active_candidate'))

    def test_team_review_rejects_continuation_without_handoff_verification(self):
        self.staged_team_plan('review','one')
        draft,directory = self.launch_team_stage()
        self.write_team_handoff(draft,directory)
        self.settle_team_from_disk(draft,'done')
        following,directory = self.launch_team_stage()
        report = self.make_handoff_continuation_report(following,directory)
        del report['handoff_verification']
        save_json(directory/'report.json',report)
        task = self.settle_team_from_disk(following,'done')
        self.assertEqual(task['status'],'blocked')
        self.assertIn('修改、保留清单',task['error'])
        self.assertIsNone(flow(task).get('active_candidate'))
        self.assertEqual(flow(task)['team_pending_handoff']['source_assignment'],draft['id'])

    def test_team_decision_plan_can_not_combine_both_stages(self):
        self.authorize(criterion(),criterion('two'))
        self.store.change(self.ticket,lambda task:flow(task)['outer']['mandate'].update(
            execution_profile=PROFILE,team_review_mode='review_and_decide'))
        task = self.plan(['one','two'])
        self.assertIsNone(task['plan'])
        self.assertIn('至少两个',flow(task)['validation']['planning_feedback'])

    def test_handoff_repair_continuation_uses_latest_seal_and_clears_both_pending_records(self):
        self.staged_team_plan('review','one')
        initial,directory = self.launch_team_stage()
        self.write_team_handoff(initial,directory)
        self.settle_team_from_disk(initial,'done')
        follow,directory = self.launch_team_stage()
        findings = dict(summary='修复 JSON 状态',action='repair',retain=[],verify=[],options=[],
                        repair_scope={'retained':[],'changes':[{'path':'one.json','before':'false','after':'true'}],
                                      'removed_paths':[]})
        report = self.make_team_report(follow,directory,result=findings,content='{"ok":false}')
        report['deferred_repair'] = True
        before,before_files,before_occupied = registry.capture(directory/'sealed')
        scope = prepare_scope(follow,findings,before,before_files)
        self.close_team_role(follow,role='implementer')
        fields = ('action','summary','retain','verify','repair_scope')
        self.close_team_role(follow,sequence=2,writable=False,candidate_sha256=before['digest'],
                             **{key:findings[key] for key in fields})
        self.engine.record_inner_repair(follow,{
            'status':'waiting','before_sha256':before['digest'],'scope':scope,
        })
        save_team_recovery(follow,directory,report)
        waiting = self.settle_team_from_disk(follow,'nothing')
        self.assertEqual(flow(waiting)['team_pending_handoff']['source_assignment'],initial['id'])
        self.assertEqual(flow(waiting)['team_pending_repair']['source_assignment'],follow['id'])
        resumed,directory = self.launch_team_stage()
        self.assertNotIn('team_handoff_continuation',resumed)
        self.assertEqual(resumed['candidate_base']['assignment'],follow['id'])
        self.assertEqual(resumed['team_handoff_origin']['base']['assignment'],initial['id'])
        self.assertEqual(resumed['context']['execution_contract']['baseline'],before['digest'])
        report = self.make_team_report(resumed,directory,content='{"ok": true}')
        after,after_files,after_occupied = registry.capture(directory/'sealed')
        instruction,handoff_before,handoff_files = self.handoff_source(resumed,directory)
        report['handoff_verification'] = make_handoff_verification(
            resumed,instruction,handoff_before,handoff_files,after,after_files)
        deadline = time.monotonic()+10
        before_checks = objective_checks(resumed,before_files,before_occupied,deadline)
        after_checks = objective_checks(resumed,after_files,after_occupied,deadline)
        verification = candidate_verification(resumed,scope,after,after_files,before_checks,after_checks)
        self.close_team_role(resumed,role='implementer')
        self.close_team_role(resumed,sequence=2,writable=False,candidate_sha256=after['digest'],
                             verification=verification,tools=[self.candidate_read_tool(after,after_files)])
        self.engine.record_inner_repair(resumed,{
            'status':'passed','source_assignment':follow['id'],'before_sha256':before['digest'],
            'after_sha256':after['digest'],'scope':scope,'verification':verification,
        })
        save_json(directory/'report.json',report)
        review = self.settle_team_from_disk(resumed,'done')
        state = flow(review)
        self.assertEqual(state['stage'],'review',review['error'])
        self.assertNotIn('team_pending_handoff',state)
        self.assertNotIn('team_pending_repair',state)

    def test_required_team_decision_rejects_done_handoff(self):
        self.staged_team_plan('review_and_decide','one','two')
        first,directory = self.launch_team_stage()
        self.assertNotIn('team_review_mode',first)
        self.finish_team_stage(first,directory)
        assignment,directory = self.launch_team_stage()
        self.assertTrue(assignment['team_decision_required'])
        self.write_team_handoff(assignment,directory,extra=('two.json',))
        task = self.settle_team_from_disk(assignment,'done')
        self.assertEqual(task['status'],'blocked')
        self.assertEqual(flow(task)['active_candidate']['assignment'],first['id'])
        self.assertIsNone(flow(task).get('team_pending_handoff'))

    def test_team_decisions_use_current_stage_and_archive_previous_answer(self):
        self.staged_team_plan('review_and_decide','one','two','three')
        first,directory = self.launch_team_stage()
        self.finish_team_stage(first,directory)
        assignment,directory = self.launch_team_stage()
        self.write_team_handoff(assignment,directory,ask=True,recover=True,extra=('two.json',))
        waiting = self.settle_team_from_disk(assignment,'nothing')
        state = flow(waiting)
        question = state['pending_preference']
        self.assertEqual((waiting['status'],question['step']),('awaiting_preference',1))
        self.assertEqual(state['active_candidate']['assignment'],first['id'])
        self.assertEqual(question['draft_sha256'],state['team_pending_handoff']['base_sha256'])
        intake = OuterIntake(Workbench(self.store))
        ticket = {'id':self.ticket,'status':'AUTHORIZED','revision':1,'correlation_id':'event',
                  'goal_revision':1,'acceptance_revision':1,'mandate_revision':1,'task_id':self.ticket}
        with self.store.connect() as db:
            db.execute('INSERT INTO outer_tickets VALUES(?,?,?,?,?)',
                       (self.ticket,'default','owner',json.dumps(ticket),'time'))
        paused = intake.control(self.ticket,'pause',1,'pref-pause',actor_id='owner')
        resumed = intake.control(self.ticket,'resume',paused['revision'],'pref-resume',actor_id='owner')
        with self.assertRaises(Problem):
            intake.answer_preference(self.ticket,resumed['revision'],question['id'],question['revision'],
                                     question['candidate_digest'],{'option_id':'1'},'old-pref',actor_id='owner')
        question = flow(self.store.get(self.ticket))['pending_preference']
        with self.assertRaisesRegex(Problem,'选择后续方向'):
            intake.answer_preference(self.ticket,resumed['revision'],question['id'],question['revision'],
                                     question['candidate_digest'],{'text':'只留文字'},'pref-no-option',actor_id='owner')
        args = (self.ticket,resumed['revision'],question['id'],question['revision'],question['candidate_digest'],
                {'option_id':'1','text':'保留布局'},'pref-choice')
        answer = intake.answer_preference(*args,actor_id='owner')
        self.assertTrue(intake.answer_preference(*args,actor_id='owner')['duplicate'])
        following,directory = self.launch_team_stage()
        self.assertEqual(following['preference_answer']['event'],answer['event_id'])
        self.assertEqual(following['candidate_base']['assignment'],assignment['id'])
        second = self.finish_team_stage(following,directory)
        self.assertEqual(second['step'],2)
        self.assertEqual((directory/'sealed'/'two.json').read_bytes(),b'{"ok":  true}')
        first_checkpoint = flow(second)['stage_deliveries'][0]
        self.assertEqual(flow(second)['checkpoint']['files']['one.json'],first_checkpoint['files']['one.json'])
        third,directory = self.launch_team_stage()
        self.assertTrue(third['team_decision_allowed'])
        self.assertFalse(third['team_decision_required'])
        self.assertIsNone(third['preference_answer'])
        self.write_team_handoff(third,directory,ask=True,extra=('two.json','three.json'))
        waiting = self.settle_team_from_disk(third,'done')
        state = flow(waiting)
        self.assertEqual(waiting['status'],'awaiting_preference')
        self.assertEqual(state['pending_preference']['step'],2)
        self.assertEqual(state['preference_history'][-1]['answer']['event'],answer['event_id'])

    def choose_keep_draft(self, *, role_limit=7):
        self.authorize(criterion('one'),criterion('two'))
        self.store.change(self.ticket,lambda task:flow(task)['outer']['mandate'].update(
            execution_profile=PROFILE,team_review_mode='review_and_decide',team_role_limit=role_limit))
        self.plan(['one'],['two'])
        first,directory = self.launch_team_stage()
        self.finish_team_stage(first,directory)
        draft,directory = self.launch_team_stage()
        report = self.write_team_handoff(draft,directory,ask=True,extra=('two.json',),keep=True)
        waiting = self.settle_team_from_disk(draft,'done')
        question = flow(waiting)['pending_preference']
        intake = OuterIntake(Workbench(self.store))
        ticket = {'id':self.ticket,'status':'AUTHORIZED','revision':1,'correlation_id':'event',
                  'goal_revision':1,'acceptance_revision':1,'mandate_revision':1,'task_id':self.ticket}
        with self.store.connect() as db:
            db.execute('INSERT INTO outer_tickets VALUES(?,?,?,?,?)',
                       (self.ticket,'default','owner',json.dumps(ticket),'time'))
        intake.answer_preference(self.ticket,1,question['id'],question['revision'],question['candidate_digest'],
                                 {'option_id':'0'},'keep-draft',actor_id='owner')
        return draft,report['checkpoint']

    def test_keep_draft_recovers_one_read_only_role_and_reviews_the_same_files(self):
        draft,checkpoint = self.choose_keep_draft()
        following,directory = self.launch_team_stage()
        self.assertIs(following['team_handoff_read_only'],True)
        self.assertEqual(following['role_slots'],1)
        self.assertEqual(following['candidate_base']['assignment'],draft['id'])
        with self.assertRaisesRegex(Problem,'只允许负责人只读'):
            self.engine.reserve_team_role(following,1,'implementer',True,'medium')
        directory.mkdir(parents=True)
        (directory/'entered').write_text(str(following['run']))

        def invoke(**options):
            self.assertEqual(options['sandbox'],'read-only')
            self.assertEqual(registry.capture(options['workspace'])[0],checkpoint)
            context = json.loads(options['prompt'].rsplit('\n',1)[-1])
            self.assertEqual(context['read_proof_required_paths'],['one.json','two.json'])
            reads = list(options['read_challenge'].values())
            self.assertEqual({row['path']:row['sha256'] for row in reads},checkpoint['files'])
            return {
                'model':MODEL,'session':'keep-lead','turn':'keep-turn','sandbox':'read-only',
                'result':dict(summary='核对两份封存 JSON 原样交付',action='done',options=[],retain=[],verify=[]),
                'tools':[self.candidate_read_tool(checkpoint,checkpoint['files'])],
            }

        with patch('superagent.goal_app_server.invoke',side_effect=invoke) as model:
            report = execute_assignment(self.store,following,directory,None)
        model.assert_called_once()
        self.assertEqual(report.get('checkpoint'),checkpoint,report.get('error'))
        save_team_recovery(following,directory,report)
        reviewed = self.settle_team_from_disk(following,'nothing')
        state = flow(reviewed)
        self.assertEqual(state['stage'],'review',reviewed['error'])
        self.assertEqual(state['assignments'][-1]['receipt_source'],'recovery')
        self.assertEqual(state['checkpoint'],checkpoint)
        self.assertEqual([(call['role'],call['writable']) for call in state['assignments'][-1]['role_calls']],
                         [('lead',False)])
        self.assertNotIn('team_pending_handoff',state)
        review = self.launch('review')
        output = attempt_file(self.store.root,self.ticket,review['id'],'report.json').parent
        output.mkdir()
        completed = self.settle_report(review,execute_assignment(self.store,review,output,None))
        self.assertEqual(completed['status'],'completed',completed['error'])
        self.assertEqual(completed['goal_delivery']['files'],checkpoint['files'])
        self.assertEqual(completed['goal_delivery']['artifact_sha256'],checkpoint['digest'])

    def test_keep_draft_is_not_dispatched_when_role_budget_is_exhausted(self):
        draft,checkpoint = self.choose_keep_draft(role_limit=6)
        task = self.engine.prepare(self.ticket)
        self.assertEqual(task['status'],'budget_exhausted')
        self.assertIsNone(current(task))
        state = flow(task)
        self.assertEqual(state['team_pending_handoff']['source_assignment'],draft['id'])
        self.assertEqual(state['team_pending_handoff']['base_sha256'],checkpoint['digest'])

    def test_keep_draft_preserves_candidate_when_time_cannot_start_a_role(self):
        _, checkpoint = self.choose_keep_draft()
        def use_budget(task):
            total = flow(task)['mandate']['budget_seconds']
            task['elapsed_seconds'] = total - self.engine._minimum_review_seconds(task) - 10
        self.store.change(self.ticket,use_budget)
        task = self.engine.prepare(self.ticket)
        self.assertEqual(task['status'],'budget_exhausted')
        self.assertIsNone(current(task))
        self.assertEqual(flow(task)['team_pending_handoff']['base_sha256'],checkpoint['digest'])

    def test_keep_draft_receipt_without_every_file_read_is_rejected(self):
        _,checkpoint = self.choose_keep_draft()
        following,directory = self.launch_team_stage()
        instruction,_,files = self.handoff_source(following,directory)
        report = self.make_team_report(following,directory,files=files)
        report['handoff_verification'] = make_handoff_verification(
            following,instruction,checkpoint,files,checkpoint,files)
        self.close_team_role(following,writable=False,sandbox='read-only',candidate_sha256=checkpoint['digest'],
                             tools=[self.candidate_read_tool(checkpoint,['two.json'])])
        save_team_recovery(following,directory,report)
        task = self.settle_team_from_disk(following,'nothing')
        self.assertEqual(task['status'],'blocked')
        self.assertIn('核验记录',task['error'])
        self.assertIsNotNone(flow(task).get('team_pending_handoff'))

    def test_keep_draft_already_dispatched_on_legacy_route_still_settles(self):
        _,checkpoint = self.choose_keep_draft(role_limit=8)
        self.engine.prepare(self.ticket)

        def legacy_shape(task):
            assignment = current(task)
            assignment.pop('team_handoff_read_only')
            assignment['role_slots'] = 2

        self.store.change(self.ticket,legacy_shape)
        following,directory = self.launch_team_stage()
        report = self.make_handoff_continuation_report(following,directory)
        save_team_recovery(following,directory,report)
        task = self.settle_team_from_disk(following,'nothing')
        self.assertEqual(flow(task)['stage'],'review',task['error'])
        self.assertEqual(flow(task)['checkpoint'],checkpoint)

    def test_sealed_draft_preview_serves_only_current_original_order(self):
        self.staged_team_plan('review_and_decide','one','two')
        first, directory = self.launch_team_stage()
        self.finish_team_stage(first,directory)
        assignment,directory = self.launch_team_stage()
        html = '<h1>完整初稿</h1><script>window.previewExecuted = true</script>' + '文字' * 7000
        assets = {'nested/page.html':html.encode(), '说明.md':'# 完整说明\n'.encode(), 'binary.dat':b'\x00\xff'}
        self.write_team_handoff(assignment,directory,ask=True,extra=('two.json',),assets=assets)
        waiting = self.settle_team_from_disk(assignment,'done')
        question = flow(waiting)['pending_preference']
        self.assertEqual(waiting['status'],'awaiting_preference')
        wb = Workbench(self.store,outer_enabled=True)
        intake = wb.outer()
        ticket = {'id':self.ticket,'status':'AUTHORIZED','revision':1,'correlation_id':'event',
                  'goal_revision':1,'acceptance_revision':1,'mandate_revision':1,'task_id':self.ticket}
        with self.store.connect() as db:
            other = 'wb-' + uuid4().hex
            db.executemany('INSERT INTO outer_tickets VALUES(?,?,?,?,?)', (
                (self.ticket,'default','owner',json.dumps(ticket),'time'),
                (other,'default','owner',json.dumps(dict(ticket,id=other,task_id=other)),'time'),
            ))
            other_task = json.loads(json.dumps(dict(waiting,id=other)))
            flow(other_task)['outer']['ticket_id'] = other
            flow(other_task)['pending_preference']['id'] = uuid4().hex
            db.execute('INSERT INTO tasks VALUES(?,?,?,?)',(other,uuid4().hex,json.dumps(other_task),'time'))
            event_count = db.execute('SELECT count(*) FROM outer_events').fetchone()[0]

        query = {'question_id':question['id'], 'question_revision':question['revision'],
                 'candidate_digest':question['candidate_digest'], 'draft_sha256':question['draft_sha256']}
        def read(path=None, *, ticket_id=self.ticket, cookie='fixture', params=None, route='draft-preview'):
            handler = Handler.__new__(Handler)
            args = dict(query if params is None else params)
            if path is not None:
                args['path'] = path
            handler.path = f'/api/tasks/{ticket_id}/{route}?' + urlencode(args)
            handler.server = SimpleNamespace(workbench=wb,server_port=8766,session_token='fixture')
            handler.headers = {'Host':'localhost:8766','Cookie':'odyssey_session_8766=' + cookie}
            handler.wfile = BytesIO()
            handler.send_response, handler.end_headers = Mock(), Mock()
            headers = {}
            handler.send_header = lambda key, value: headers.update({key:value})
            handler.do_GET()
            return handler.send_response.call_args.args[0], headers, json.loads(handler.wfile.getvalue())

        status, headers, manifest = read()
        self.assertEqual(status,200)
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertEqual(headers['X-Content-Type-Options'],'nosniff')
        self.assertEqual({item['path']:item['kind'] for item in manifest['files']}, {
            'one.json':'text', 'two.json':'text', 'nested/page.html':'html', '说明.md':'markdown', 'binary.dat':'binary',
        })
        self.assertEqual(manifest['advisor']['observations'][0]['path'],'two.json')
        self.assertEqual(manifest['directions'][0]['changes'][0]['before'],'{"ok":true}')
        self.assertIn({'path':'nested/page.html','excerpt':''},manifest['directions'][0]['retained'])
        result, html_headers, payload = read('nested/page.html')
        self.assertEqual(result,200)
        self.assertEqual(html_headers['Content-Type'],'application/json; charset=utf-8')
        self.assertEqual(payload,{'path':'nested/page.html','kind':'html','content':html})
        self.assertEqual(read('说明.md')[2]['content'],'# 完整说明\n')
        self.assertEqual(read('binary.dat')[2]['content'],'AP8=')
        self.assertEqual(read('nested/page.html',cookie='wrong')[0],403)
        self.assertEqual(read(ticket_id=other)[0],409)
        with self.assertRaisesRegex(Problem,'无权访问'):
            intake.draft_preview(self.ticket,**query,actor_id='other')
        for missing in ('../page.html','/nested/page.html','nested/../one.json','report.json',''):
            self.assertEqual(read(missing)[0],404)
        for key, value in (('question_id','0'*32),('question_revision',2),('draft_sha256','0'*64),
                           ('candidate_digest','0'*64)):
            self.assertEqual(read(params=dict(query,**{key:value}))[0],409)
        self.assertEqual(read(params={'question_id':question['id']})[0],400)
        self.assertEqual(read('nested/page.html',params={'candidate':assignment['id']},route='artifact')[0],404)
        self.assertEqual(read('nested/page.html',params={},route='preview')[0],404)
        self.assertEqual(self.store.get(self.ticket),waiting)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM outer_events').fetchone()[0],event_count)

        sealed_file = directory/'sealed'/'nested'/'page.html'
        sealed_file.chmod(0o600)
        sealed_file.write_text('草稿已被改动')
        self.assertEqual(read('nested/page.html')[0],409)
        self.assertEqual(read()[0],409)
        outside = directory/'outside.html'
        outside.write_text(html)
        sealed_file.unlink()
        sealed_file.symlink_to(outside)
        self.assertEqual(read('nested/page.html')[0],409)
        sealed_file.unlink()
        sealed_file.write_text(html)

        capture = registry.capture
        def pause_while_reading(workspace):
            snapshot = capture(workspace)
            intake.control(self.ticket,'pause',1,'preview-pause',actor_id='owner')
            return snapshot
        with patch.object(registry,'capture',side_effect=pause_while_reading):
            self.assertEqual(read('nested/page.html')[0],409)
        self.assertEqual(read()[0],409)
        resumed = intake.control(self.ticket,'resume',2,'preview-resume',actor_id='owner')
        self.assertEqual(read()[0],409)
        query['question_revision'] += 1
        self.assertEqual(read()[0],200)
        intake.answer_preference(self.ticket,resumed['revision'],question['id'],query['question_revision'],
                                 question['candidate_digest'],{'option_id':'1'},'preview-chosen',actor_id='owner')
        self.assertEqual(read()[0],409)

    def test_worker_durable_recovery_before_primary_publication(self):
        assignment,directory = self.launch_team_candidate()
        context = self.store.root/'autoloop-context.json'
        save_json(context,{'events':[{
            'id':assignment['event_key'],'source':'local','subject':self.ticket,
            'type':'workbench.goal.assignment',
            'detail':json.dumps({'assignment':assignment['id'],'version':assignment['version']}),
        }]})
        def invoke(workspace,launch_gate,**_):
            with launch_gate():
                (workspace/'one.json').write_text('{"ok":true}')
            return {
                'model':MODEL,'session':'session-worker','turn':'turn-worker',
                'started_ns':1_000_000_000,'ended_ns':3_000_000_000,
                'result':dict(summary='已生成 JSON',action='done',options=[],retain=[],verify=[]),
            }
        def interrupt(path,value):
            if path.name == 'report.json':
                self.assertTrue((directory/TEAM_RECOVERY_FILE).is_file())
                raise SystemExit(0)
            save_json(path,value)
        with patch.dict(os.environ,{'AGENT_AUTOLOOP_CONTEXT':str(context),'AGENT_AUTOLOOP_RUN':str(assignment['run'])}), \
             patch('superagent.goal_app_server.invoke',side_effect=invoke), \
             patch('superagent.goal_runtime.save_json',side_effect=interrupt), self.assertRaises(SystemExit):
            worker(self.store,None)
        self.assertFalse((directory/'report.json').exists())
        waiting = self.settle_team_from_disk(assignment,'queued')
        self.assertEqual(current(waiting)['id'],assignment['id'])
        self.assertIsNone(flow(waiting).get('active_candidate'))
        task = self.settle_team_from_disk(assignment,'nothing')
        state = flow(task)
        self.assertEqual(state['stage'],'review')
        self.assertEqual(state['active_candidate']['assignment'],assignment['id'])
        self.assertEqual(state['assignments'][-1]['receipt_source'],'recovery')
        self.assertEqual(sum(item['kind'] == 'recovered_artifacts' for item in state['decisions']),1)
        replay = self.engine.settle(self.ticket,assignment['id'],assignment['run'],'done',
                                    state['assignments'][-1]['report'])
        self.assertEqual(flow(replay)['decisions'],state['decisions'])
        self.assertEqual(replay['calls'],task['calls'])

    def test_worker_recovers_written_repair_and_dispatches_only_read_only_review(self):
        assignment,directory = self.launch_team_candidate()
        calls = []
        content = b'{"ok":true}'
        context = self.store.root/'autoloop-context.json'

        def run_worker(row):
            save_json(context,{'events':[{
                'id':row['event_key'],'source':'local','subject':self.ticket,
                'type':'workbench.goal.assignment',
                'detail':json.dumps({'assignment':row['id'],'version':row['version']}),
            }]})
            with patch.dict(os.environ,{'AGENT_AUTOLOOP_CONTEXT':str(context),'AGENT_AUTOLOOP_RUN':str(row['run'])}):
                worker(self.store,None)

        def invoke(model, sandbox, workspace, launch_gate, **_):
            sequence = len(calls)+1
            calls.append(sandbox)
            started = time.time_ns()
            with launch_gate():
                if sequence == 1:
                    self.assertEqual(sandbox,'workspace-write')
                    (workspace/'one.json').write_text('{"ok":false}')
                elif sequence == 2:
                    self.assertEqual(sandbox,'workspace-write')
                    (workspace/'one.json').write_bytes(content)
                else:
                    self.assertEqual(sequence,3)
                    self.assertEqual(sandbox,'read-only')
                    self.assertEqual((workspace/'one.json').read_bytes(),content)
            result = dict(action='repair' if sequence == 1 else 'done', summary=f'第 {sequence} 个角色检查',
                          retain=[],verify=['核对 JSON 状态'],options=[])
            if sequence == 1:
                result['repair_scope'] = dict(retained=[],removed_paths=[],
                                              changes=[dict(path='one.json',before='false',after='true')])
            checked = registry.capture(workspace)[0]
            return dict(model=model,session=f'worker-session-{sequence}',turn=f'worker-turn-{sequence}',
                        started_ns=started,ended_ns=time.time_ns(),result=result,
                        tools=[self.candidate_read_tool(checked,['one.json'])])

        def interrupt_after_save(row, output, report):
            save_team_recovery(row,output,report)
            if 'repair_review_pending' in report:
                raise SystemExit('模拟 Worker 在写入后退出')

        with patch('superagent.goal_app_server.invoke',side_effect=invoke):
            with patch('superagent.goal_runtime.save_team_recovery',side_effect=interrupt_after_save), \
                    self.assertRaises(SystemExit):
                run_worker(assignment)
            self.assertFalse((directory/'report.json').exists())
            self.assertEqual((directory/'sealed'/'one.json').read_bytes(),content)
            recovery = json.loads((directory/TEAM_RECOVERY_FILE).read_text())['report']
            run_worker(assignment)
            self.assertEqual(calls,['workspace-write','workspace-write'])

            pending = self.settle_team_from_disk(assignment,'nothing')
            state = flow(pending)
            saved = state['team_pending_repair']
            self.assertEqual(saved['base_sha256'],recovery['checkpoint']['digest'])
            self.assertEqual(saved['source_assignment'],assignment['id'])
            self.assertIsNone(state.get('active_candidate'))
            self.assertEqual(saved['repair_record']['status'],'waiting_review')
            following = self.launch('execute')
            self.assertIn('review_only',following['repair_continuation'])
            self.assertEqual(following['candidate_base']['assignment'],assignment['id'])
            self.assertEqual(following['candidate_base']['artifact_sha256'],saved['base_sha256'])
            before_replay = self.store.get(self.ticket)
            replay = self.settle_report(assignment,recovery)
            self.assertEqual(flow(replay),flow(before_replay))
            self.assertEqual(replay['calls'],before_replay['calls'])
            self.assertEqual(replay['elapsed_seconds'],before_replay['elapsed_seconds'])
            run_worker(following)

        self.assertEqual(calls,['workspace-write','workspace-write','read-only'])
        ready = self.settle_team_from_disk(following,'done')
        state = flow(ready)
        self.assertEqual(state['stage'],'review',ready['error'])
        self.assertEqual(state['checkpoint'],recovery['checkpoint'])
        self.assertNotIn('team_pending_repair',state)
        self.assertEqual([(call['role'],call['writable']) for call in state['assignments'][-1]['role_calls']],
                         [('lead',False)])
        review = self.launch('review')
        output = attempt_file(self.store.root,self.ticket,review['id'],'report.json')
        output.parent.mkdir(parents=True)
        report = execute_assignment(self.store,review,output.parent,None)
        save_json(output,report)
        completed = self.settle_team_from_disk(review,'done')
        self.assertEqual(completed['status'],'completed')
        self.assertEqual(completed['goal_delivery']['artifact_sha256'],saved['base_sha256'])

    def test_earlier_partial_receipt_preserves_later_rejection_and_model_time(self):
        assignment,directory = self.launch_team_candidate()
        original = self.store.root/'original-candidate'
        materialize({'one.json':b'{"ok":false}'},original)
        before,files,occupied = registry.capture(original)
        findings = dict(action='repair',summary='修复 JSON 状态',retain=[],verify=[],options=[],
                        repair_scope={'retained':[],'changes':[{'path':'one.json','before':'false','after':'true'}],
                                      'removed_paths':[]})
        report = self.make_team_report(assignment,directory,result=findings)
        after = report['checkpoint']
        scope = prepare_scope(assignment,findings,before,files)
        checks = objective_checks(assignment,files,occupied,time.monotonic()+10)
        implementation = dict(action='done',summary='已改候选')
        self.close_team_role(assignment,action='repair',timing=False,started_ns=0,ended_ns=1_000_000_000)
        self.close_team_role(assignment,sequence=2,role='implementer',timing=False,
                             candidate_sha256=after['digest'],started_ns=1_000_000_000,ended_ns=2_000_000_000,
                             **implementation)
        partial = dict(report,repair_review_pending={'implementation':implementation,'before_checks':checks},
                       evidence={'started_ns':0,'ended_ns':2_000_000_000})
        save_team_recovery(assignment,directory,partial)

        # 负责人确认仍有问题之后，Worker 在最终回执落盘前退出。
        self.close_team_role(assignment,sequence=3,writable=False,timing=False,action='repair',
                             summary='负责人确认仍需修改',candidate_sha256=after['digest'],
                             started_ns=2_000_000_000,ended_ns=87_000_000_000)
        issue = '负责人复验仍需修改'
        self.engine.record_inner_repair(assignment,{
            'status':'blocked','scope':scope,'issue':findings['summary'],'changes':implementation['summary'],
            'review':issue,'before_sha256':before['digest'],'after_sha256':after['digest'],'changed_files':['one.json'],
        })
        before_settlement = self.store.get(self.ticket)
        task = self.settle_team_from_disk(assignment,'nothing')
        state = flow(task)
        self.assertEqual(task['status'],'blocked')
        self.assertIsNone(state.get('active_candidate'))
        self.assertIsNone(state.get('team_pending_repair'))
        repair = state['assignments'][-1]['inner_repairs'][-1]
        self.assertEqual((repair['status'],repair['review']),('blocked',issue))
        self.assertEqual(task['elapsed_seconds']-before_settlement['elapsed_seconds'],87)

    def test_bad_primary_report_uses_bound_recovery_and_role_time(self):
        assignment,directory = self.launch_team_candidate()
        self.close_team_role(assignment)
        report = self.make_team_report(assignment,directory)
        save_team_recovery(assignment,directory,report)
        (directory/'report.json').write_bytes(b'{"broken":')
        before = self.store.get(self.ticket)
        task = self.settle_team_from_disk(assignment,'done')
        self.assertEqual(flow(task)['checkpoint'],report['checkpoint'])
        self.assertEqual(task['elapsed_seconds'] - before['elapsed_seconds'],2)
        self.assertEqual(task.get('estimated_seconds',0),before.get('estimated_seconds',0))

    def test_legacy_team_marker_with_corrupt_primary_finishes_without_promotion(self):
        assignment,directory = self.launch_team_candidate()
        self.close_team_role(assignment)
        self.make_team_report(assignment,directory)
        (directory/'ready-for-review').write_text(str(assignment['run']))
        (directory/'report.json').write_bytes(b'\xff')
        task = self.settle_team_from_disk(assignment,'nothing')
        self.assertEqual(task['status'],'blocked')
        self.assertIsNone(current(task))
        self.assertIsNone(flow(task).get('active_candidate'))
        self.assertIsNone(flow(task)['checkpoint'])

    def test_team_recovery_rejects_seal_changes(self):
        assignment,directory = self.launch_team_candidate()
        self.close_team_role(assignment)
        report = self.make_team_report(assignment,directory)
        save_team_recovery(assignment,directory,report)
        changed = directory/'sealed'/'one.json'
        changed.chmod(0o600)
        changed.write_text('{"ok":false}')
        task = self.settle_team_from_disk(assignment,'nothing')
        self.assertEqual(task['status'],'blocked')
        self.assertIsNone(flow(task).get('active_candidate'))

    def test_team_recovery_rejects_reused_session(self):
        assignment,directory = self.launch_team_candidate()
        self.close_team_role(assignment)
        session = current(self.store.get(self.ticket))['role_calls'][0]['session']
        self.store.change(self.ticket,lambda task:flow(task)['assignments'][0].update(role_calls=[{'session':session}]))
        report = self.make_team_report(assignment,directory)
        save_team_recovery(assignment,directory,report)
        task = self.settle_team_from_disk(assignment,'nothing')
        self.assertEqual(task['status'],'blocked')
        self.assertIn('独立完成回执',task['error'])
        self.assertIsNone(flow(task).get('active_candidate'))

    def test_invalid_team_preference_leaves_admitted_candidate_intact(self):
        assignment,directory = self.launch_team_candidate()
        self.close_team_role(assignment,action='ask')
        before = {'assignment':'old','artifact_sha256':'old','files':{}}
        self.store.change(self.ticket,lambda task:flow(task).update(active_candidate=before))
        option = {'id':'one','label':'样例','text':'内容'}
        result = dict(summary='要哪个样例？',action='ask',options=[option]*3,retain=[],verify=[])
        report = self.make_team_report(assignment,directory,result=result)
        report['preference'] = {'prompt':result['summary'],'options':result['options']}
        save_json(directory/'samples.json',report['preference'])
        save_json(directory/'report.json',report)
        task = self.settle_team_from_disk(assignment,'done')
        self.assertEqual(task['status'],'blocked')
        self.assertEqual(flow(task)['active_candidate'],before)
        self.assertIsNone(flow(task).get('pending_preference'))

    def test_team_preference_recovery_keeps_original_samples(self):
        assignment,directory = self.launch_team_candidate()
        self.close_team_role(assignment,action='ask')
        result = dict(summary='要哪个样例？',action='ask',retain=[],verify=[],
                      options=[{'id':str(index),'label':f'样例 {index}','text':f'内容 {index}'}
                               for index in range(3)])
        report = self.make_team_report(assignment,directory,result=result)
        report['preference'] = {'prompt':result['summary'],'options':result['options']}
        save_json(directory/'samples.json',report['preference'])
        save_team_recovery(assignment,directory,report)
        task = self.settle_team_from_disk(assignment,'nothing')
        state = flow(task)
        self.assertEqual(task['status'],'awaiting_preference')
        self.assertEqual(state['pending_preference']['options'],result['options'])
        self.assertIsNone(state['pending_preference']['answer'])
        self.assertEqual(state['assignments'][-1]['receipt_source'],'recovery')

    def test_team_repair_wait_recovery_preserves_draft_without_promoting(self):
        assignment,directory = self.launch_team_candidate()
        result = dict(summary='修复 JSON 状态',action='repair',retain=[],verify=[],options=[],
                      repair_scope={'retained':[],'changes':[{'path':'one.json','before':'false','after':'true'}],
                                    'removed_paths':[]})
        self.close_team_role(assignment,**{key:result[key] for key in
                                          ('action','summary','retain','verify','repair_scope')})
        report = self.make_team_report(assignment,directory,result=result,content='{"ok":false}')
        report['deferred_repair'] = True
        checkpoint,files,_ = registry.capture(directory/'sealed')
        scope = prepare_scope(assignment,result,checkpoint,files)
        self.engine.record_inner_repair(assignment,{
            'status':'waiting','before_sha256':checkpoint['digest'],'scope':scope,
        })
        save_team_recovery(assignment,directory,report)
        task = self.settle_team_from_disk(assignment,'nothing')
        state = flow(task)
        self.assertEqual(state['stage'],'execute')
        self.assertIsNone(state.get('active_candidate'))
        self.assertEqual(state['team_pending_repair']['source_assignment'],assignment['id'])
        self.assertEqual(state['team_pending_repair']['base_sha256'],report['checkpoint']['digest'])

    def test_pause_accounts_for_recovered_team_receipt_without_promotion(self):
        assignment,directory = self.launch_team_candidate()
        self.close_team_role(assignment)
        report = self.make_team_report(assignment,directory)
        save_team_recovery(assignment,directory,report)
        before = self.store.get(self.ticket)
        outer = {'id':self.ticket,'goal_revision':1,'status_before_pause':'AUTHORIZED'}
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            OuterIntake._control_execution(db,outer,'pause','web','pause')
        task = self.settle_team_from_disk(assignment,'cancelled')
        self.assertEqual(task['status'],'paused')
        self.assertEqual(flow(task)['stage'],'execute')
        self.assertIsNone(flow(task).get('active_candidate'))
        self.assertEqual(task['elapsed_seconds'] - before['elapsed_seconds'],2)

    def mark_team_dispatch_stale(self):
        proposal = 'accepted-proposal'
        def scoped(task):
            state = flow(task)
            state.update(repairs=1,validation={},coordination_proposals={proposal:{'proposal':{'action':'REPAIR'}}})
            current(task).update(coordination_proposal=proposal,
                                 coordination_source_revision=self.engine._coordination_source_revision(task))
        self.store.change(self.ticket,scoped)
        with self.store.connect() as db:
            db.execute('INSERT INTO inbox_notes VALUES(?,?,?,?)',('web-new',self.ticket,'新信息','time'))
        return proposal

    def test_stale_team_dispatch_accounts_for_completed_role_before_next_launch(self):
        assignment,directory = self.launch_team_candidate()
        self.close_team_role(assignment)
        self.close_team_role(assignment,sequence=2,timing=False,status='failed',error_code='stale_before_role_launch')
        proposal = self.mark_team_dispatch_stale()
        before = self.store.get(self.ticket)
        report = self.make_team_report(assignment,directory)
        report.update(error='原派工已经过期',coordination_dispatch_stale=True)
        save_json(directory/'report.json',report)
        task = self.settle_team_from_disk(assignment,'done')
        state = flow(task)
        self.assertEqual(state['stage'],'coordinate')
        self.assertEqual(state['coordination_proposals'][proposal]['dispatch_disposition'],'stale_after_launch')
        self.assertIsNone(state.get('active_candidate'))
        self.assertEqual(task['elapsed_seconds'] - before['elapsed_seconds'],2)

    def test_stale_team_dispatch_rejects_recovery_after_final_role(self):
        assignment,directory = self.launch_team_candidate()
        self.close_team_role(assignment)
        report = self.make_team_report(assignment,directory)
        save_team_recovery(assignment,directory,report)
        proposal = self.mark_team_dispatch_stale()
        before = self.store.get(self.ticket)
        task = self.settle_team_from_disk(assignment,'nothing')
        state = flow(task)
        self.assertEqual(state['stage'],'coordinate')
        self.assertEqual(state['coordination_proposals'][proposal]['dispatch_disposition'],'stale_after_launch')
        self.assertIsNone(state.get('active_candidate'))
        self.assertEqual(task['elapsed_seconds'] - before['elapsed_seconds'],2)

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

    def scoped_regression(self, *, require_signoff=False, extra_artifacts=0):
        self.authorize(criterion(),criterion('two'),require_signoff=require_signoff)
        self.store.change(self.ticket,lambda task: flow(task).update(coordinator_profile=registry.SCOPED_COORDINATOR))
        self.plan(['one'],['two'])
        (self.workspace/'one.json').write_text('{"ok":true}')
        self.checkpoint()
        first = self.launch('review')
        self.settle_report(first,self.trusted_review(first))
        (self.workspace/'one.json').write_text('{"ok":false}')
        (self.workspace/'two.json').write_text('{"ok":true}')
        for index in range(extra_artifacts):
            (self.workspace/f'aux-{index:02d}.txt').write_text('附加文件')
        self.checkpoint()
        final = self.launch('review')
        self.settle_report(final,self.trusted_review(final))
        assignment = self.launch('coordinate')
        context = assignment['context']['coordinator']
        self.assertEqual(context['affected_steps'],[0,1])
        self.assertEqual(context['verification_items'],['one','two'])
        self.assertEqual(context['tool_evidence']['read_verified_artifacts']['verified_stages'][0]['assignment'],
                         first['id'])
        proposal, invocation = self.scoped_proposal(assignment,'REPAIR','修复导入结果')
        self.assertEqual(proposal['retained_outputs'],['two.json'])
        return assignment, proposal, invocation

    @staticmethod
    def scoped_proposal(assignment, action, plan_change):
        context = assignment['context']['coordinator']
        prefix = 'replan_' if action == 'REPLAN' else ''
        proposal = {'schema_version':2,'input_snapshot_revision':context['input_snapshot_revision'],
                    'evidence_refs':context['evidence_refs'],'action':action,'reason':'处理已登记的验收失败',
                    'plan_change':plan_change,'affected_steps':context[prefix + 'affected_steps'],
                    'verification_items':context[prefix + 'verification_items'],
                    'retained_outputs':context['retained_outputs'],
                    'retained_artifacts':[{'path':path,'sha256':sha256}
                                          for path,sha256 in context['artifact_versions'].items()],
                    'scope_change':False,'approval_required':False,'remaining_risk':'需要重新跑全部验收'}
        invocation = {'snapshot_revision':context['input_snapshot_revision'],'invocation_id':assignment['id'],
                      'started_ns':0,'ended_ns':1_000_000_000,'session_id':str(uuid4()),
                      'tools':[{'name':name,'status':'completed','evidence_sha256':digest(value)}
                               for name,value in context['tool_evidence'].items()]}
        return proposal, invocation

    def test_scoped_proposal_and_dispatch_are_committed_once(self):
        from concurrent.futures import ThreadPoolExecutor
        assignment, proposal, invocation = self.scoped_regression()
        with ThreadPoolExecutor(max_workers=2) as participants:
            results = list(participants.map(lambda _: self.finish(assignment,proposal,evidence=invocation), range(2)))
        task = self.store.get(self.ticket)
        state = flow(task)
        dispatch = current(task)
        self.assertEqual(state['stage'],'repair')
        self.assertEqual(dispatch['phase'],'repair')
        self.assertEqual(dispatch['coordination_proposal'],assignment['id'])
        self.assertEqual(state['coordination_proposals'][assignment['id']]['dispatch'],dispatch['id'])
        self.assertEqual(state['coordination_proposals'][assignment['id']]['disposition'],'accepted')
        self.assertEqual(state['repairs'],1)
        self.assertEqual(sum(row['phase']=='repair' for row in state['assignments']),1)
        self.assertEqual({result['calls'] for result in results},{6})
        self.assertEqual(current(GoalEngine(self.store).prepare(self.ticket))['id'],dispatch['id'])
        (self.workspace/'one.json').write_text('{"ok":true}')
        self.checkpoint('repair')
        review = self.launch('review')
        done = self.settle_report(review,self.trusted_review(review))
        self.assertEqual(done['status'],'completed')
        self.assertEqual([row['status'] for row in done['checks']],['PASS','PASS'])

    def test_scoped_failed_commit_can_retry_same_receipt(self):
        assignment, proposal, invocation = self.scoped_regression()
        with patch.object(GoalEngine,'_enqueue',side_effect=RuntimeError('injected storage interruption')):
            with self.assertRaisesRegex(RuntimeError,'storage interruption'):
                self.finish(assignment,proposal,evidence=invocation)
        recovered = self.store.get(self.ticket)
        self.assertEqual(current(recovered)['id'],assignment['id'])
        self.assertNotIn('coordination_proposals',flow(recovered))
        self.assertEqual(recovered['calls'],5)
        task = self.finish(assignment,proposal,evidence=invocation)
        self.assertEqual(current(task)['coordination_proposal'],assignment['id'])
        self.assertEqual(task['calls'],6)

    def test_scoped_pause_and_new_feedback_fence_coordinator_receipt(self):
        assignment, proposal, invocation = self.scoped_regression()
        with self.engine.launch_gate(self.ticket,assignment['id'],assignment['run']):
            pass
        ticket = {'id':self.ticket,'goal_revision':1,'status_before_pause':'AUTHORIZED'}
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            OuterIntake._control_execution(db,ticket,'pause','web','pause')
        task = self.finish(assignment,proposal,evidence=invocation)
        self.assertEqual(task['status'],'paused')
        self.assertEqual(flow(task)['coordination_proposals'][assignment['id']]['disposition'],'stale')
        self.assertIsNone(current(task))
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            OuterIntake._control_execution(db,ticket,'resume','web','resume')
        new_round = self.launch('coordinate')
        self.assertNotEqual(new_round['id'],assignment['id'])
        with self.store.connect() as db:
            db.execute('INSERT INTO inbox_notes VALUES(?,?,?,?)',('web-note',self.ticket,'有新反馈','time'))
        with self.assertRaisesRegex(Problem,'协调依据已变化'):
            with self.engine.launch_gate(self.ticket,new_round['id'],new_round['run']):
                self.fail('old context reached the launch gate')
        fresh = self.finish(new_round,proposal,evidence=invocation)
        self.assertEqual(flow(fresh)['coordination_proposals'][new_round['id']]['disposition'],'stale')
        self.assertIsNone(current(fresh))

    def test_scoped_retention_cannot_be_forged_or_changed_in_repair(self):
        assignment, proposal, invocation = self.scoped_regression()
        tampered = dict(proposal,retained_artifacts=[{'path':'two.json','sha256':'0'*64}])
        with self.assertRaisesRegex(ValueError,'文件版本不匹配'):
            registry.validate_coordination(tampered,assignment['context']['coordinator'])
        self.finish(assignment,proposal,evidence=invocation)
        (self.workspace/'one.json').write_text('{"ok":true}')
        (self.workspace/'two.json').write_text('{"ok":false}')
        task = self.checkpoint('repair')
        self.assertEqual(task['status'],'blocked')
        self.assertIn('保留的文件版本',task['error'])

    def test_scoped_repair_and_continue_cannot_omit_verified_versions(self):
        assignment, proposal, invocation = self.scoped_regression(extra_artifacts=35)
        omitted = dict(proposal,retained_outputs=[],retained_artifacts=[])
        context = assignment['context']['coordinator']
        with self.assertRaisesRegex(ValueError,'全部文件版本'):
            registry.validate_coordination(dict(omitted,action='CONTINUE'),
                                           dict(context,available_actions=['CONTINUE']))
        task = self.finish(assignment,omitted,evidence=invocation)
        self.assertEqual(task['status'],'blocked')
        self.assertIn('全部文件版本',task['error'])
        self.assertIsNone(current(task))

    def test_scoped_replan_can_change_verified_file_with_recorded_reason_and_full_recheck(self):
        self.authorize(criterion(),criterion('two'),criterion('three'))
        self.store.change(self.ticket,lambda task:flow(task).update(coordinator_profile=registry.SCOPED_COORDINATOR))
        self.plan(['one'],['two'],['three'])
        (self.workspace/'one.json').write_text('{"ok":true}')
        self.checkpoint()
        first = self.launch('review')
        self.settle_report(first,self.trusted_review(first))
        (self.workspace/'two.json').write_text('{"ok":false}')
        self.checkpoint()
        second = self.launch('review')
        self.settle_report(second,self.trusted_review(second))

        coordinator = self.launch('coordinate')
        context = coordinator['context']['coordinator']
        self.assertEqual(context['verification_items'],['two'])
        self.assertEqual(context['replan_verification_items'],['one','two'])
        self.assertEqual(context['retained_outputs'],['one.json'])
        proposal, invocation = self.scoped_proposal(coordinator,'REPLAN','更新 one.json 的排版，保持输出值')
        proposal.update(retained_outputs=[],retained_artifacts=[])
        for invalid, error in ((dict(proposal,plan_change=''), '不能为空'),
                               (dict(proposal,scope_change=True), '不能扩大'),
                               (dict(proposal,verification_items=['two']), '下一轮验收')):
            with self.subTest(error=error), self.assertRaisesRegex(ValueError,error):
                registry.validate_coordination(invalid,context)
        task = self.finish(coordinator,proposal,evidence=invocation)
        self.assertEqual(flow(task)['stage'],'replan')
        replan = self.launch('replan')
        self.finish(replan,{'summary':'应用排版并修复本阶段','steps':[
            {'objective':'对齐结构化输出','checks':['two']},{'objective':'完成第三阶段','checks':['three']}]})
        (self.workspace/'one.json').write_text('{"ok": true}')
        (self.workspace/'two.json').write_text('{"ok":true}')
        self.checkpoint()
        review = self.launch('review')
        report = self.trusted_review(review)
        self.assertEqual([(row['id'],row['status']) for row in report['validation']['checks']],
                         [('one','PASS'),('two','PASS')])
        self.assertNotEqual(report['checkpoint']['files']['one.json'],context['artifact_versions']['one.json'])
        task = self.settle_report(review,report)
        self.assertEqual(task['step'],2)
        self.assertEqual(flow(task)['outer']['mandate_revision'],1)
        self.assertEqual(flow(task)['revisions'][-1]['reason']['coordination']['plan_change'],proposal['plan_change'])

    def test_long_replan_history_keeps_second_coordination_within_tool_budget(self):
        from superagent.coordination_tools import _encode, _LIMIT
        from superagent.outer_model import ModelFailure, SCOPED_PROMPT_VERSION
        stages = [[f'item-{index}-{suffix}'.ljust(50,'x') for suffix in ('a','b')] for index in range(6)]
        self.authorize(*(criterion(key,description='验' * 332) for keys in stages for key in keys))
        self.store.change(self.ticket,lambda task:flow(task).update(coordinator_profile=registry.SCOPED_COORDINATOR))
        long_plan = {'summary':'😀' * 3000,'steps':[{'objective':'😀' * 1500,'checks':keys} for keys in stages]}
        self.finish(self.launch('plan'),long_plan)
        for keys in stages:
            for key in keys:
                (self.workspace/f'{key}.json').write_text('{"ok":false}' if keys == stages[-1] else '{"ok":true}')
            self.checkpoint()
            review = self.launch('review')
            self.settle_report(review,self.trusted_review(review))
        first = self.launch('coordinate')
        proposal, invocation = self.scoped_proposal(first,'REPLAN','调整最后阶段的执行方法')
        self.finish(first,proposal,evidence=invocation)
        self.finish(self.launch('replan'), {'summary':'🔍' * 3000,
                                           'steps':[{'objective':'🔍' * 1500,'checks':stages[-1]}]})
        self.checkpoint()
        review = self.launch('review')
        task = self.settle_report(review,self.trusted_review(review))
        second = self.launch('coordinate')
        evidence = second['context']['coordinator']['tool_evidence']
        plan_versions = evidence['read_plan_versions']
        current_plan = plan_versions['current']
        previous = plan_versions['previous'][0]
        original = flow(task)['revisions'][0]['plan']
        self.assertEqual(current_plan['steps'][-1]['objective'],'🔍' * 1000)
        self.assertTrue(current_plan['steps'][-1]['objective_truncated'])
        self.assertTrue(current_plan['summary_truncated'])
        self.assertEqual(task['plan']['steps'][-1]['objective'],'🔍' * 1500)
        self.assertEqual((previous['version'],previous['previous'],previous['plan_sha256']),(1,None,digest(original)))
        self.assertEqual(previous['plan']['steps'][-1]['acceptance'],stages[-1])
        self.assertEqual(plan_versions['current_plan_sha256'],digest(task['plan']))
        self.assertNotIn('verification',current_plan['steps'][-1])
        self.assertEqual(original['steps'][-1]['objective'],'😀' * 1500)
        self.assertGreater(len(_encode(dict(evidence,read_plan_versions={
            **plan_versions,'current':task['plan'],
        }))),_LIMIT)
        self.assertLess(len(_encode(evidence)),_LIMIT)
        with patch('superagent.outer_model.CodexOuterModel._execute',side_effect=ModelFailure('probe')) as invoke:
            report = self.trusted_review(second)
        invoke.assert_called_once()
        self.assertEqual(report['evidence']['failure_code'],'probe')
        self.assertEqual(report['evidence']['prompt_version'],SCOPED_PROMPT_VERSION)

    def test_machine_pass_releases_retention_before_human_return(self):
        coordinator, proposal, invocation = self.scoped_regression(require_signoff=True)
        self.finish(coordinator,proposal,evidence=invocation)
        (self.workspace/'one.json').write_text('{"ok":true}')
        self.checkpoint('repair')
        review = self.launch('review')
        task = self.settle_report(review,self.trusted_review(review))
        self.assertEqual(task['status'],'awaiting_artifact_review')
        self.assertNotIn('coordination_retention',flow(task))

        intake = OuterIntake(Workbench(self.store))
        ticket = {'id':self.ticket,'status':'AUTHORIZED','revision':1,'correlation_id':'event',
                  'goal_revision':1,'acceptance_revision':1,'mandate_revision':1}
        with self.store.connect() as db:
            db.execute('INSERT INTO outer_tickets VALUES(?,?,?,?,?)',
                       (self.ticket,'default','owner',json.dumps(ticket),'time'))
        candidate = flow(task)['artifact_reviews'][-1]
        result = intake.decide_artifact(self.ticket,'return',1,candidate['assignment'],candidate['artifact_sha256'],
                                        '调整 two.json 的排版','operator-return',actor_id='owner',allow_model_input=True)
        self.assertEqual(result['action'],'ARTIFACT_RETURNED')
        with self.store.connect() as db:
            authorized = db.execute("SELECT json_type(body,'$.allow_model_input') FROM outer_events WHERE source_event_id=?",
                                    ('operator-return',)).fetchone()[0]
        self.assertEqual(authorized,'true')
        (self.workspace/'two.json').write_text('{"ok": true}')
        self.checkpoint('repair')
        subsequent = self.launch('review')
        updated = self.settle_report(subsequent,self.trusted_review(subsequent))
        self.assertEqual(updated['status'],'awaiting_artifact_review')
        self.assertNotEqual(candidate['artifact_sha256'],flow(updated)['artifact_reviews'][-1]['artifact_sha256'])

    def pause_bound_scoped_repair(self, *, claimed=False, audit_error=None):
        coordinator, proposal, invocation = self.scoped_regression()
        task = self.finish(coordinator,proposal,evidence=invocation)
        dispatch = current(task)
        event = run = 90
        rule = 'odyssey-goal'
        def source_outcome(state):
            return {'event':event,'routed':[rule],
                    'runs':[{'run':run,'rule':rule,'state':state,'attempts':0}]}
        def hub_fact(state, claimed_at=None):
            return {'version':1,'changes':[{'table':'runs','key':str(run),
                     'row':{'id':run,'rule':rule,'state':state,'claimed_at':claimed_at,'attempts':0}}]}
        facts = [hub_fact('queued')]
        if claimed:
            facts.append(hub_fact('claimed','time'))
        facts.append(hub_fact('cancelled','time' if claimed else None))
        hub = Mock()
        hub.request.side_effect = [
            {'event':event},source_outcome('queued'),source_outcome('queued'),{'ok':True},source_outcome('cancelled'),
            *([audit_error,source_outcome('cancelled')] if audit_error else []),facts,
        ]
        runtime = GoalRuntime(self.store,hub)
        bound = runtime.tick(self.ticket)
        self.assertEqual(current(bound)['run'],run)
        ticket = {'id':self.ticket,'goal_revision':1,'status_before_pause':'AUTHORIZED'}
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            OuterIntake._control_execution(db,ticket,'pause','web','pause')
        stopped = runtime.tick(self.ticket)
        self.assertEqual(stopped['status'],'stopping')
        self.assertEqual(hub.request.call_args.args,('POST',f'/v1/runs/{run}/cancel',{}))
        return GoalRuntime(self.store,hub), ticket, coordinator['id'], dispatch['id']

    def test_paused_queued_scoped_repair_recomputes_after_hub_confirms_no_start(self):
        runtime, ticket, coordinator, dispatch = self.pause_bound_scoped_repair()
        before = self.store.get(self.ticket)
        stopped = runtime.tick(self.ticket)
        self.assertEqual(stopped['status'],'paused')
        self.assertIsNone(current(stopped))
        self.assertEqual(stopped['calls'],before['calls'])
        self.assertEqual(flow(stopped)['checkpoint'],flow(before)['checkpoint'])
        self.assertEqual(flow(stopped)['stage'],'coordinate')
        self.assertEqual(flow(stopped)['repairs'],0)
        self.assertEqual(flow(stopped)['coordination_proposals'][coordinator]['dispatch'],dispatch)
        self.assertEqual(flow(stopped)['coordination_proposals'][coordinator]['dispatch_disposition'],
                         'stale_before_launch')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            OuterIntake._control_execution(db,ticket,'resume','web','resume')
        next_round = self.launch('coordinate')
        self.assertNotEqual(next_round['id'],coordinator)

    def test_paused_claimed_repair_without_local_entry_is_not_confirmed_unstarted(self):
        runtime, _, coordinator, _ = self.pause_bound_scoped_repair(claimed=True)
        stopped = runtime.tick(self.ticket)
        state = flow(stopped)
        self.assertEqual(stopped['status'],'paused')
        self.assertIsNone(current(stopped))
        self.assertTrue(state['validation']['recovery_blocked'])
        self.assertNotIn('dispatch_disposition',state['coordination_proposals'][coordinator])

    def test_paused_repair_with_unavailable_hub_audit_keeps_conservative_recovery(self):
        runtime, _, _, _ = self.pause_bound_scoped_repair(audit_error=Problem('中台不提供轮次时间线',404))
        stopped = runtime.tick(self.ticket)
        self.assertEqual(stopped['status'],'paused')
        self.assertTrue(flow(stopped)['validation']['recovery_blocked'])

    def test_paused_repair_retries_hub_audit_after_a_transient_error(self):
        runtime, _, _, _ = self.pause_bound_scoped_repair(audit_error=Problem('中台暂不可用',503))
        with self.assertRaisesRegex(Problem,'暂不可用'):
            runtime.tick(self.ticket)
        self.assertEqual(self.store.get(self.ticket)['status'],'stopping')
        stopped = runtime.tick(self.ticket)
        self.assertEqual(stopped['status'],'paused')
        self.assertEqual(flow(stopped)['stage'],'coordinate')

    def test_paused_unstarted_repair_cannot_adopt_externally_changed_bytes(self):
        runtime, _, coordinator, _ = self.pause_bound_scoped_repair()
        before = self.store.get(self.ticket)
        (self.workspace/'two.json').write_text('{"ok": true}')
        stopped = runtime.tick(self.ticket)
        state = flow(stopped)
        self.assertEqual(stopped['status'],'paused')
        self.assertIsNone(current(stopped))
        self.assertEqual(state['checkpoint'],flow(before)['checkpoint'])
        self.assertEqual(stopped['calls'],before['calls'])
        self.assertTrue(state['validation']['recovery_blocked'])
        self.assertNotIn('dispatch_disposition',state['coordination_proposals'][coordinator])

    def test_scoped_new_feedback_cancels_accepted_dispatch_before_hub_send(self):
        appointment, proposal, invocation = self.scoped_regression()
        task = self.finish(appointment,proposal,evidence=invocation)
        dispatch = current(task)
        with self.store.connect() as db:
            db.execute('INSERT INTO inbox_notes VALUES(?,?,?,?)',('web-new',self.ticket,'新信息','time'))
        task = self.engine.mark_send(self.ticket,dispatch['id'])
        self.assertIsNone(current(task))
        self.assertEqual(flow(task)['repairs'],0)
        self.assertEqual(flow(task)['stage'],'coordinate')
        record = flow(task)['coordination_proposals'][appointment['id']]
        self.assertEqual(record['dispatch_disposition'],'stale_before_launch')
        self.assertNotEqual(self.launch('coordinate')['id'],appointment['id'])

    def test_scoped_new_feedback_fences_dispatched_repair_at_worker_gate(self):
        appointment, proposal, invocation = self.scoped_regression()
        self.finish(appointment,proposal,evidence=invocation)
        repair = self.launch('repair')
        with self.store.connect() as db:
            db.execute('INSERT INTO inbox_notes VALUES(?,?,?,?)',('web-new',self.ticket,'新信息','time'))
        def execute(*args,launch_gate):
            with launch_gate():
                self.fail('stale repair was allowed to run')
        with patch('superagent.goal_codex.invoke',side_effect=execute):
            report = self.trusted_review(repair)
        self.assertIs(report['coordination_dispatch_stale'],True)
        task = self.settle_report(repair,report)
        self.assertEqual(task['calls'],6)
        self.assertEqual(flow(task)['repairs'],0)
        self.assertEqual(flow(task)['stage'],'coordinate')
        self.assertIsNone(current(task))

    def test_scoped_new_feedback_after_recheck_prevents_delivery(self):
        from outer_channels import dispatch
        from outer_workbench import create_task
        from superagent.test_outer_intake import Model

        model = Model()
        def intake_model(role, context, schema):
            result = model(role,context,schema)
            if role == 'acceptance_designer':
                result['criteria'][0].update(description='核对 JSON 文件内容', verifier_id='json-artifact',
                                             test_spec_json='{"path":"one.json"}', expected_result_json='{"ok":true}')
            return result

        workbench = Workbench(self.store,goal_enabled=True,outer_enabled=True,outer_workbench_entry=True,
                              outer_model=intake_model)
        outer = workbench.outer()
        registered = create_task(workbench,dict(workflow='outer',request_id=str(uuid4()),name='交付 JSON',
                                                goal='生成包含指定字段的 JSON 文件',acceptance='核对根目录 one.json'))
        outer.drain()
        self.ticket = outer.event(registered['event_id'],'owner')['ticket_id']
        self.workspace = self.store.workspace(self.ticket)
        draft = outer.get(self.ticket,'owner')
        outer.approve_goal_contract(self.ticket,draft['goal_revision'],draft['acceptance_revision'],draft['revision'],
                                    str(uuid4()), actor_id='owner')
        self.plan(['R1'])
        (self.workspace/'one.json').write_text('{"ok":true}')
        self.checkpoint()
        first = self.launch('review')
        with patch.object(registry,'_criterion_result',return_value=('ERROR','隔离环境暂不可用')):
            self.settle_report(first,self.trusted_review(first))

        coordinator = self.launch('coordinate')
        proposal, invocation = self.scoped_proposal(coordinator,'CONTINUE','')
        self.finish(coordinator,proposal,evidence=invocation)
        review = self.launch('review')
        path = attempt_file(self.store.root,self.ticket,review['id'],'report.json')
        path.parent.mkdir(parents=True)
        report = execute_assignment(self.store,review,path.parent,None)
        self.assertEqual(report['validation']['checks'][0]['status'],'PASS')
        save_json(path,report)
        current_revision = outer.get(self.ticket,'owner')['revision']
        feedback = dispatch(outer,'web','submit',dict(ticket_id=self.ticket,revision=current_revision,
                                                      request_id=str(uuid4()),text='请补充一项交付要求'))
        self.assertEqual(feedback['state'],'PENDING')

        before = self.store.get(self.ticket)
        task = self.settle_team_from_disk(review,'done')
        state = flow(task)
        self.assertEqual(task['status'],'queued')
        self.assertNotIn('goal_delivery',task)
        self.assertEqual(state['stage'],'coordinate')
        self.assertEqual(state['rechecks'],0)
        self.assertEqual(state['checkpoint'],flow(before)['checkpoint'])
        self.assertEqual(state['coordination_proposals'][coordinator['id']]['dispatch_disposition'],'stale_after_launch')
        self.assertEqual(task['calls'],before['calls']+1)
        elapsed = (report['evidence']['ended_ns']-report['evidence']['started_ns'])/1e9
        self.assertAlmostEqual(task['elapsed_seconds']-before['elapsed_seconds'],elapsed)
        repeated = self.settle_report(review,report)
        self.assertEqual(flow(repeated),state)
        self.assertEqual(repeated['calls'],task['calls'])
        self.assertEqual(repeated['elapsed_seconds'],task['elapsed_seconds'])
        self.assertIsNone(current(self.engine.prepare(self.ticket)))

    def test_scoped_new_feedback_after_replan_launch_preserves_approved_plan(self):
        coordinator, _, _ = self.scoped_regression()
        proposal, invocation = self.scoped_proposal(coordinator,'REPLAN','重规划受影响阶段')
        self.finish(coordinator,proposal,evidence=invocation)
        replan = self.launch('replan')

        def execute(*args,launch_gate):
            with launch_gate():
                pass
            with self.store.connect() as db:
                db.execute('INSERT INTO inbox_notes VALUES(?,?,?,?)',('web-new',self.ticket,'新信息','time'))
            return dict(model=MODEL,session='new-replan-session',started_ns=0,ended_ns=1_000_000_000,
                        result=dict(summary='旧反馈依据下的重规划', steps=[dict(objective='修复第二阶段',checks=['two'])]))

        with patch('superagent.goal_codex.invoke',side_effect=execute):
            report = self.trusted_review(replan)
        self.assertIn('result',report)
        before = self.store.get(self.ticket)
        task = self.settle_report(replan,report)
        state = flow(task)
        self.assertEqual(task['version'],before['version'])
        self.assertEqual(task['plan'],before['plan'])
        self.assertEqual(state['revisions'],flow(before)['revisions'])
        self.assertEqual(state['stage'],'coordinate')
        self.assertEqual(state['replans'],0)
        self.assertEqual(state['coordination_proposals'][coordinator['id']]['dispatch_disposition'],'stale_after_launch')
        self.assertEqual(task['calls'],before['calls']+1)
        self.assertEqual(task['elapsed_seconds']-before['elapsed_seconds'],1)
        repeated = self.settle_report(replan,report)
        self.assertEqual(flow(repeated),state)
        self.assertEqual(repeated['calls'],task['calls'])
        self.assertEqual(repeated['elapsed_seconds'],task['elapsed_seconds'])
        self.assertNotEqual(self.launch('coordinate')['id'],coordinator['id'])

    def test_scoped_repair_will_not_launch_against_unregistered_bytes(self):
        assignment, proposal, invocation = self.scoped_regression()
        self.finish(assignment,proposal,evidence=invocation)
        (self.workspace/'one.json').write_text('{"ok":true}')
        repair = self.launch('repair')
        report = self.trusted_review(repair)
        self.assertIs(report['checkpoint_mismatch'],True)
        task = self.engine.settle(self.ticket,repair['id'],repair['run'],'done',report,
                                  recovery_checkpoint=registry.capture(self.workspace)[0])
        self.assertEqual(task['status'],'blocked')
        self.assertNotEqual(flow(task)['checkpoint']['digest'],registry.capture(self.workspace)[0]['digest'])

    def test_scoped_tool_audit_gates_dispatch(self):
        assignment, proposal, invocation = self.scoped_regression()
        task = self.finish(assignment,proposal,evidence=dict(invocation,tools=invocation['tools'][:-1]))
        self.assertEqual(task['status'],'blocked')
        self.assertIsNone(current(task))
        self.assertEqual(flow(task)['coordination_proposals'][assignment['id']]['disposition'],'rejected')

    def test_scoped_budget_is_rechecked_after_charging_coordinator(self):
        assignment, proposal, invocation = self.scoped_regression()
        self.store.change(self.ticket,lambda task: task.update(elapsed_seconds=570))
        task = self.finish(assignment,proposal,evidence=invocation)
        self.assertEqual(task['status'],'budget_exhausted')
        self.assertIsNone(current(task))
        self.assertEqual(flow(task)['coordination_proposals'][assignment['id']]['disposition'],'rejected')

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
            schema = json.loads(Path(argv[argv.index('--output-schema')+1]).read_text())['properties']
            self.assertEqual(schema['action']['enum'],context['available_actions'])
            self.assertEqual(schema['evidence_refs']['items']['enum'],context['evidence_refs'])
            self.assertEqual(schema['evidence_refs']['minItems'],1)
            self.assertEqual(schema['retained_outputs']['items']['enum'],['one.json'])
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

    def test_rejected_coordinator_proposal_keeps_usage_without_applying_result(self):
        from superagent.test_goal_codex import _events
        self.authorize(criterion())
        self.plan(['one'])
        self.checkpoint()
        review = self.launch('review')
        self.settle_report(review,self.trusted_review(review))
        assignment = self.launch('coordinate')
        context = assignment['context']['coordinator']
        proposal = {'schema_version':1,'input_snapshot_revision':context['input_snapshot_revision'],
                    'action':'REPAIR','reason':'需要修复','evidence_refs':[],
                    'plan_change':'','retained_outputs':[],
                    'scope_change':False,'approval_required':False,'remaining_risk':'仍须复验'}
        schema = registry.coordination_schema(context)
        self.assertEqual(schema['properties']['retained_outputs']['maxItems'],0)
        stdout = "\n".join(json.dumps(event) for event in _events(proposal)).encode()
        with patch('superagent.goal_codex._run',return_value=(stdout,b'',0,0,1_000_000_000)):
            report = self.trusted_review(assignment)
        self.assertNotIn('result',report)
        self.assertEqual(report['evidence']['failure_code'],'proposal_rejected')
        self.assertEqual(json.loads(report['model_audit']['final_message']),proposal)
        self.assertIn('证据', report['error'])
        self.assertEqual(report['evidence']['usage']['input_tokens'],3)
        task = self.settle_report(assignment,report)
        self.assertEqual(flow(task)['stage'],'coordinate')
        self.assertEqual(flow(task)['assignments'][-1]['report']['evidence'],report['evidence'])
        self.assertNotEqual(task['status'],'completed')

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
