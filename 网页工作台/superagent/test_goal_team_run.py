"""覆盖团队审稿、实施、本人决定和返修。"""
import json
import subprocess
from stat import S_IMODE
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from workbench import Problem
from .goal_engine import GoalEngine, StaleCoordination
from .goal_runtime import read_team_recovery
from .goal_team_handoff import CandidateReadUnproven, candidate_read_challenge, valid_handoff_verification
from .goal_team_records import PROFILE
from .goal_team_repair import candidate_verification, prepare_scope, valid_verification
from .goal_team_run import run_team
from .outer_verification import LOCAL_DATA, capture, sha256


def synthetic_reads(options):
    challenge = options.get('read_challenge')
    if not challenge:
        return []
    return [dict(name='shell',settled=True,paired=True,status='completed',exit_code=0,
                 candidate_reads=list(challenge.values()))]


class TeamRunTest(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        self.base = root/'baseline'
        self.target = root/'attempt'
        self.base.mkdir()
        self.target.mkdir()
        self.engine = Mock()
        self.engine.reserve_team_role.side_effect = lambda _assignment, _sequence, role, *_: {'role':role}
        self.assignment = {
            'id':'frozen-test','phase':'execute','step':0, 'goal':'查看样例后完成报告',
            'preference_answer':{'event':'accepted-preference','text':'采用第二组'},
            'execution_profile':PROFILE,'max_seconds':120,
            'plan':{'steps':[{'objective':'据选定的意见交付报告'}]},
            'outer_acceptance':{'criteria':[]},
            'context':{'approved_criteria':[{'interface':{'path':'report.html'},'verifier_id':'html-structure'}],
                       'approved_goal_contract':{},'contract_instruction':'完成后独立复验'},
        }

    def start_review(self, target, *, gate='required', scenario='valid'):
        draft = '<title>同版初稿</title>\n<p>请保留本句，补全第二段。</p>\n'
        prior = '<title>前一步已验收</title>'
        (self.base/'report.html').write_text(prior)
        self.assignment.update(
            step=1, preference_answer=None, role_slots=5, max_seconds=240,
            plan={'steps':[{'objective':'审批上版'},{'objective':'审阅本步'},{'objective':'补下一步'}]},
            team_review_mode='review' if gate == 'disabled' else 'review_and_decide',
            team_decision_allowed=gate != 'disabled', team_decision_required=gate == 'required',
        )
        self.assignment['context'].update(
            notes=['不得出现在顾问输入的额外备注'],
            original_requirements=[{'text':'请用原话选择网站方向'}],
            execution_contract={'plan_revision':2},
        )
        seen = []

        def invoke(**options):
            data = json.loads(options['prompt'].rsplit('\n',1)[-1])
            workspace = options['workspace']
            number = len(seen) + 1
            seen.append(data)
            result = dict(action='done',summary=f'角色返回的第{number}份意见',options=[],retain=[],verify=[])
            if number in (1,2):
                self.assertEqual((workspace/'report.html').read_text(),prior)
                self.assertEqual(S_IMODE((workspace/'report.html').stat().st_mode),0o400)
                self.assertIn('请用原话选择网站方向',options['prompt'])
                self.assertNotIn('不得出现在顾问输入的额外备注',options['prompt'])
            if number == 2:
                self.assertEqual(data,seen[0])
                self.assertNotIn('角色返回的第1份意见',options['prompt'])
            if number == 3 and scenario != 'unchanged':
                self.assertEqual(data['lead_plan']['summary'],'角色返回的第1份意见')
                self.assertEqual(data['independent_plan']['summary'],'角色返回的第2份意见')
                (workspace/'report.html').write_text(draft)
            if number in (4,5):
                self.assertEqual((target/'sealed'/'report.html').read_text(),draft)
                self.assertEqual((workspace/'report.html').read_text(),draft)
                self.assertEqual(S_IMODE((workspace/'report.html').stat().st_mode),0o400)
                self.assertNotEqual(workspace,target/'candidate')
            if number == 4:
                self.assertNotIn('comparison',data)
                self.assertNotIn(draft,options['prompt'])
                result['observations'] = [{
                    'path':'report.html',
                    'excerpt':'编造的原文' if scenario == 'forged' else '请保留本句，补全第二段。',
                    'issue':'第二段缺少下一个操作。',
                }]
            if number == 5:
                self.assertEqual(data['comparison']['summary'],'角色返回的第3份意见')
                self.assertEqual(data['draft_review']['observations'][0]['issue'],'第二段缺少下一个操作。')
                expected = {'required':['ask'], 'optional':['done','ask'], 'disabled':['done']}[gate]
                self.assertEqual(options['schema']['properties']['action']['enum'],expected)
                result['verify'] = ['report.html 第二段补上下一步']
                result['retain'] = ['report.html 保留首段']
                if scenario == 'forbidden-ask' or (gate != 'disabled' and scenario not in ('forced-done','optional-done')):
                    result['action'] = 'ask'
                    result['options'] = [{'id':str(i),'label':f'方向{i}','text':f'采用布局{i}'} for i in range(3)]
                    if scenario == 'duplicate-options':
                        result['options'][1]['text'] = result['options'][0]['text']
                option_ids = [row['id'] for row in result['options']] if result['action'] == 'ask' else ['']
                result['handoff_scope'] = {'variants':[
                    dict(option_id=option,mode='keep' if option == '0' else 'edit',
                         retained=[dict(path='report.html',excerpt='请保留本句')],
                         changes=[] if option == '0' else [
                             dict(path='report.html',before='',after=f'方向{option}。' if option else '进入详情。')
                         ],removed_paths=[])
                    for option in option_ids
                ]}
                if scenario == 'unbound-scope':
                    result['handoff_scope']['variants'][-1]['changes'][0]['before'] = '初稿里没有这句'
                if scenario == 'legacy-instruction':
                    result.pop('handoff_scope')
                if scenario == 'tampered-draft':
                    sealed = target/'sealed'/'report.html'
                    sealed.chmod(0o600)
                    sealed.write_text('未经过负责人查看的版本')
            tools = synthetic_reads(options)
            if scenario == 'no-read':
                tools = []
            elif scenario == 'fake-read':
                tools = [dict(name='shell',settled=True,paired=True,status='completed',exit_code=0)]
            elif scenario == 'wrong-read' and tools:
                tools[0]['candidate_reads'] = [dict(path='report.html',sha256='0' * 64)]
            return {'model':options['model'],'result':result,'tools':tools}

        with patch('superagent.goal_app_server.invoke',side_effect=invoke):
            return run_team(self.engine,self.assignment,self.base,target,{},None)

    def test_review_seals_real_draft_and_respects_step_decision_mode(self):
        for gate, scenario, action in (
                ('disabled','valid','done'), ('required','valid','ask'),
                ('optional','valid','ask'), ('optional','optional-done','done')):
            with self.subTest(gate=gate,scenario=scenario), TemporaryDirectory() as temporary:
                target = Path(temporary).resolve()
                self.engine.reset_mock()
                report = self.start_review(target,gate=gate,scenario=scenario)
                handoff = report['team_handoff']
                self.assertEqual(handoff['instruction']['action'],action)
                self.assertEqual(report['result'],handoff['instruction'])
                if action == 'ask':
                    self.assertEqual(report['preference'],{'prompt':report['result']['summary'],
                                                           'options':report['result']['options']})
                else:
                    self.assertNotIn('preference',report)
                self.assertEqual(handoff['draft_sha256'],report['checkpoint']['digest'])
                reservations = [call.args[2:4] for call in self.engine.reserve_team_role.call_args_list]
                self.assertEqual(reservations,[('lead',False),('advisor',False),('lead',True),
                                               ('advisor',False),('lead',False)])
                completed = [call.args[2] for call in self.engine.finish_team_role.call_args_list]
                self.assertEqual(completed[0]['candidate_sha256'],completed[1]['candidate_sha256'])
                self.assertNotEqual(completed[0]['candidate_sha256'],handoff['draft_sha256'])
                self.assertTrue(all(call['candidate_sha256'] == handoff['draft_sha256'] for call in completed[2:]))
                self.assertEqual(completed[3]['observations'],handoff['draft_review']['observations'])
                self.assertEqual(completed[-1].get('options',[]),handoff['instruction']['options'])
                self.assertEqual(completed[-1]['handoff_scope'],handoff['instruction']['handoff_scope'])

    def test_review_rejects_unbound_drafts_and_invalid_owner_decisions(self):
        for gate, scenario, reason, calls in (
                ('required','forged','与实际封存文件或原文不匹配',4),
                ('disabled','unchanged','未形成',3),
                ('required','no-read','未留下',4),
                ('required','fake-read','未留下',4),
                ('required','wrong-read','未留下',4),
                ('required','forced-done','未开放的行动',5),
                ('disabled','forbidden-ask','未开放的行动',5),
                ('required','duplicate-options','有区别的选择',5),
                ('required','unbound-scope','旧版原文不存在',5),
                ('required','legacy-instruction','旧版审稿缺少',5),
                ('required','tampered-draft','封存版本发生变化',5)):
            with self.subTest(scenario=scenario), TemporaryDirectory() as temporary:
                self.engine.reset_mock()
                with self.assertRaisesRegex(Problem,reason):
                    self.start_review(Path(temporary).resolve(),gate=gate,scenario=scenario)
                self.assertEqual(self.engine.reserve_team_role.call_count,calls)

    def test_handoff_continuation_requires_current_answer_and_checks_new_version(self):
        report = self.start_review(self.target)
        handoff = report['team_handoff']
        renewed = {**self.assignment,'id':'renewed','role_slots':2,
                   'team_handoff_continuation':handoff}
        candidate = self.target/'sealed'

        for bad in (
                renewed,
                {**renewed,'preference_answer':{'text':'没有确认方向','event':'text-only'}},
                {**renewed,'preference_answer':{'option_id':'0','text':'请附一段说明','event':'keep-with-edit'}},
                {**renewed,'team_handoff_continuation':{**handoff,'instruction':{
                    key:value for key,value in handoff['instruction'].items() if key != 'handoff_scope'
                }},'preference_answer':{'option_id':'1','event':'answer-legacy'}},
                {**renewed,'team_handoff_continuation':{**handoff,'draft_sha256':'1' * 64}},
                {**renewed,'preference_answer':{'option_id':'过期','event':'stale-event'}},
        ):
            with TemporaryDirectory() as temporary, patch('superagent.goal_app_server.invoke') as invoke:
                with self.assertRaises(Problem):
                    run_team(self.engine,bad,candidate,Path(temporary).resolve(),{},None)
                invoke.assert_not_called()

        renewed['preference_answer'] = {'event':'current-answer','option_id':'1','text':'采用方向1'}
        self.engine.reset_mock()
        for found_problem in (False, True):
            with self.subTest(found_problem=found_problem), TemporaryDirectory() as temporary:
                self.engine.reset_mock()
                seen = []

                def invoke(**options):
                    seen.append(options['sandbox'])
                    file = options['workspace']/'report.html'
                    self.assertIn('采用方向1',options['prompt'])
                    self.assertIn('report.html 第二段补上下一步',options['prompt'])
                    result = dict(action='done',summary='保留了首段并补第二段',options=[],retain=[],verify=[])
                    if len(seen) == 1:
                        self.assertIn('同版初稿',file.read_text())
                        file.write_text('<title>新版</title>\n<p>请保留本句，补全第二段。</p>\n<p>采用方向1。</p>')
                    else:
                        self.assertIn('<p>采用方向1。</p>',file.read_text())
                        if found_problem:
                            result.update(action='repair',summary='补上实际入口',repair_scope={
                                'retained':[{'path':'report.html','excerpt':'请保留本句'}],
                                'changes':[{'path':'report.html','before':'采用方向1。','after':'进入方向1。'}],
                                'removed_paths':[],
                            })
                    return {'model':options['model'],'result':result,'tools':synthetic_reads(options)}

                with patch('superagent.goal_app_server.invoke',side_effect=invoke):
                    result = run_team(self.engine,renewed,candidate,Path(temporary).resolve(),{},None)
                self.assertEqual(seen,['workspace-write','read-only'])
                self.assertNotIn('team_handoff',result)
                self.assertNotEqual(result['checkpoint']['digest'],handoff['draft_sha256'])
                self.assertEqual(bool(result.get('deferred_repair')),found_problem)
                self.assertEqual(self.engine.finish_team_role.call_args.args[2]['candidate_sha256'],
                                 result['checkpoint']['digest'])

    def test_handoff_worker_checks_changed_and_retained_bytes_and_explicit_keep(self):
        initial = self.start_review(self.target)
        handoff = initial['team_handoff']
        candidate = self.target/'sealed'
        draft = candidate/'report.html'
        pending = dict(handoff=handoff, base_sha256=initial['checkpoint']['digest'],
                       files=initial['checkpoint']['files'])
        renewed = dict(self.assignment, id='continued', role_slots=2, team_handoff_continuation=handoff)

        for scenario, option, content, passed in (
            ('required edit skipped','1',None,False),
            ('edited but never added requested content','1','<p>请保留本句，但只加错的内容。</p>',False),
            ('damaged retention','1','<p>采用方向1。</p>',False),
            ('explicitly keep draft','0',None,True),
            ('lead did not read approved bytes','0',None,False),
            ('edit submitted despite keep','0','<p>请保留本句，但误改。</p>',False),
            ('edit and incorporate feedback','1',
             draft.read_text() + '<p>采用方向1。</p><p>按意见另加一段。</p>',True),
        ):
            with self.subTest(scenario=scenario), TemporaryDirectory() as temporary:
                answer = dict(event='accepted', option_id=option,
                              text='请另加一段' if scenario == 'edit and incorporate feedback' else None)
                assignment = dict(renewed,preference_answer=answer)

                def invoke(**options):
                    if options['sandbox'] == 'workspace-write' and content is not None:
                        (options['workspace']/'report.html').write_text(content)
                    tools = [] if scenario == 'lead did not read approved bytes' else synthetic_reads(options)
                    return dict(model=options['model'], tools=tools, result=dict(
                        action='done',summary='角色宣称检查通过',options=[],retain=[],verify=[],
                    ))

                with patch('superagent.goal_app_server.invoke',side_effect=invoke):
                    if scenario == 'lead did not read approved bytes':
                        with self.assertRaisesRegex(Problem,'未留下与本次实际候选文件匹配的读取记录'):
                            run_team(self.engine,assignment,candidate,Path(temporary).resolve(),{},None)
                        continue
                    result = run_team(self.engine,assignment,candidate,Path(temporary).resolve(),{},None)
                evidence = result['handoff_verification']
                self.assertIs(evidence['verification']['passed'],passed)
                self.assertEqual(evidence['option_id'], option)
                if passed:
                    checkpoint = result['checkpoint']
                    self.assertEqual(checkpoint == initial['checkpoint'], option == '0')
                    self.assertTrue(valid_handoff_verification(assignment,checkpoint,pending,evidence))
                    self.assertFalse(valid_handoff_verification(assignment,checkpoint,pending,None))
                    self.assertFalse(valid_handoff_verification(
                        assignment,checkpoint,pending,{**evidence,'option_id':'forged'},
                    ))
                else:
                    self.assertIn('审稿续办未通过',result['error'])
                    self.assertNotIn('checkpoint',result)
                    self.assertFalse((Path(temporary)/'sealed').exists())

    def test_read_challenge_hashes_the_actual_candidate_file(self):
        path = '简报 草稿.txt'
        content = '真实封存内容。'.encode()
        (self.base/path).write_bytes(content)
        checkpoint = {'files':{path:'registered-digest-for-test'}}
        prompt, expected = candidate_read_challenge(checkpoint,{path:content},[path])
        command = prompt.splitlines()[-1]
        result = subprocess.run(['/bin/sh','-c',command],cwd=self.base,capture_output=True,check=True,text=True)
        self.assertEqual(expected[result.stdout.strip().removeprefix('workbench-read:')],{
            'path':path,'sha256':'registered-digest-for-test',
        })

    def test_replacement_can_wrap_or_unwrap_original_without_leaving_a_duplicate(self):
        original = '  <h1 id="release-notice">Launch Brief</h1>\n'
        wrapped = '  <main>\n  ' + original + '  </main>\n'
        path = 'brief.html'
        for old,new in ((original,wrapped),(wrapped,original)):
            (self.base/path).write_text(old)
            before,before_files,_ = capture(self.base)
            finding = {'retain':[], 'repair_scope':{'retained':[], 'removed_paths':[],
                       'changes':[{'path':path,'before':old,'after':new}]}}
            scope = prepare_scope(self.assignment,finding,before,before_files)
            for content,passed in ((new,True),(old+new,False)):
                with self.subTest(wrap=old==original, replaces=passed):
                    (self.target/path).write_text(content)
                    candidate,files,_ = capture(self.target)
                    evidence = candidate_verification(self.assignment,scope,candidate,files,[],[])
                    self.assertIs(evidence['passed'],passed)
                    self.assertIs(valid_verification(self.assignment,candidate,scope,evidence),passed)

    def test_approved_preference_is_an_input_to_consultation_implementation_and_review(self):
        seen = []

        def invoke(**options):
            prompt = options['prompt']
            self.assertIn('采用第二组',prompt)
            self.assertIn('不再征求同一选择',prompt)
            self.assertNotIn('ask',options['schema']['properties']['action']['enum'])
            action='consult' if not seen else 'done'
            if options['sandbox']=='workspace-write':
                (options['workspace']/'report.html').write_text('<h1>采用第二组</h1>')
            seen.append(options['sandbox'])
            return {'model':options['model'],
                    'result':{'action':action,'summary':'复核已选择的第二组', 'options':[],'retain':[],'verify':[]}}

        with patch('superagent.goal_app_server.invoke',side_effect=invoke):
            report=run_team(self.engine,self.assignment,self.base,self.target,{},None)
        self.assertEqual(seen,['read-only','read-only','workspace-write','read-only','read-only'])
        self.assertEqual(set(report['checkpoint']['files']),{'report.html'})
        self.assertNotIn('preference',report)

    def test_final_human_review_does_not_trigger_preference_before_step_two(self):
        self.assignment.update(preference_answer=None,plan={'steps':[
            {'objective':'先完成可机验提纲'}, {'objective':'审稿定方向后由本人签收'},
        ]},outer_acceptance={'require_signoff':True,'criteria':[
            {'required':True,'verification_type':'HUMAN_REVIEW'},
        ]})
        self.assignment['context']['team_review_mode'] = 'review_and_decide'

        def invoke(**options):
            self.assertEqual(options['sandbox'],'workspace-write')
            (options['workspace']/'report.html').write_text('<h1>前置提纲</h1>')
            return {'model':options['model'],'result':{
                'action':'done','summary':'已完成前置提纲','options':[],'retain':[],'verify':[],
            }}

        with patch('superagent.goal_app_server.invoke',side_effect=invoke) as model:
            report = run_team(self.engine,self.assignment,self.base,self.target,{},None)
        model.assert_called_once()
        self.assertEqual(set(report['checkpoint']['files']),{'report.html'})
        self.assertNotIn('preference',report)

    def test_failed_preparation_settles_reserved_role_without_calling_model(self):
        (self.target/'role-1').mkdir()
        with patch('superagent.goal_app_server.invoke') as invoke, self.assertRaises(FileExistsError):
            run_team(self.engine,self.assignment,self.base,self.target,{},None)
        invoke.assert_not_called()
        self.engine.finish_team_role.assert_called_once_with(
            self.assignment,1,{'error_code':'role_preparation_error'},status='failed')

    def test_late_optional_review_leaves_candidate_for_independent_verification(self):
        now = 100.0
        seen = []

        def invoke(**options):
            nonlocal now
            seen.append(options['sandbox'])
            if options['sandbox'] == 'workspace-write':
                (options['workspace']/'report.html').write_text('<h1>已按选择完成</h1>')
            if len(seen) == 3:
                now = options['deadline'] - 40
            return {'model':options['model'],
                    'result':{'action':'done','summary':'候选已就绪','options':[],'retain':[],'verify':[]}}

        with patch('superagent.goal_team_run.time.monotonic',side_effect=lambda: now), \
                patch('superagent.goal_app_server.invoke',side_effect=invoke):
            report = run_team(self.engine,self.assignment,self.base,self.target,{},None)
        self.assertEqual(seen,['read-only','workspace-write','read-only'])
        self.assertEqual(set(report['checkpoint']['files']),{'report.html'})

    def test_invalid_receipt_preserves_confirmed_metadata_without_storing_result(self):
        observed = {'model':'gpt-6.2-sol','session':'session','turn':'turn','usage':{'total_tokens':13}}
        output = {**observed,'result':{'action':'done','summary':'超' * 3601, 'options':[],'retain':[],'verify':[]}}
        with patch('superagent.goal_app_server.invoke',return_value=output), self.assertRaises(Problem):
            run_team(self.engine,self.assignment,self.base,self.target,{},None)
        settlement = self.engine.finish_team_role.call_args
        self.assertEqual(settlement.kwargs,{'status':'failed'})
        evidence = settlement.args[2]
        self.assertEqual({key:evidence[key] for key in observed},observed)
        self.assertEqual(evidence['error_code'],'validation_error')
        self.assertEqual(len(evidence['prompt_sha256']),64)
        self.assertNotIn('result',evidence)

    def test_stale_gate_marks_role_as_never_started(self):
        with patch('superagent.goal_app_server.invoke',side_effect=StaleCoordination('授权已改')), \
                self.assertRaises(StaleCoordination):
            run_team(self.engine,self.assignment,self.base,self.target,{},None)
        settlement = self.engine.finish_team_role.call_args
        self.assertEqual(settlement.kwargs,{'status':'failed'})
        self.assertEqual(settlement.args[2]['error_code'],'stale_before_role_launch')

    def test_repair_requires_a_new_candidate_and_a_second_lead_verdict(self):
        for scenario in ('pass', 'unresolved', 'no-change', 'no-time'):
            with self.subTest(scenario=scenario), TemporaryDirectory() as temporary:
                self.target = Path(temporary).resolve()
                self.engine.reset_mock()
                self.assignment['max_seconds'] = 240
                self.assignment.pop('preference_answer', None)
                self.assignment['context']['approved_criteria'] *= 2
                calls = []
                records = []
                now = 10
                self.engine.record_inner_repair.side_effect = lambda _, record: records.append(dict(record))

                def invoke(**options):
                    nonlocal now
                    calls.append(options)
                    candidate = options['workspace']/'report.html'
                    number = len(calls)
                    if number == 2:
                        candidate.write_text('缺少原样例，已有新稿')
                    elif number == 3:
                        self.assertEqual(candidate.read_text(), '缺少原样例，已有新稿')
                        if scenario == 'no-time':
                            now = options['deadline'] - 30
                    elif number == 4:
                        self.assertIn('已有新稿', options['prompt'])
                        if scenario != 'no-change':
                            candidate.write_text('已恢复原样例，已有新稿')
                    elif number == 5:
                        self.assertEqual(candidate.read_text(), '已恢复原样例，已有新稿')
                    action = 'repair' if number == 3 or (number == 5 and scenario == 'unresolved') else 'done'
                    return {'model':options['model'],
                        'tools':synthetic_reads(options),
                        'result':{
                        'action':action,'summary':'人工样例原文检查','options':[],
                        'retain':[],'verify':['打开实际文件检查原样例'],
                        'repair_scope':{'retained':[{'path':'report.html','excerpt':'已有新稿'}],
                                        'changes':[{'path':'report.html','before':'缺少原样例','after':'已恢复原样例'}],
                                        'removed_paths':[]},
                    }}

                with patch('superagent.goal_team_run.time.monotonic',side_effect=lambda: now), \
                     patch('superagent.goal_app_server.invoke',side_effect=invoke):
                    report = run_team(self.engine,self.assignment,self.base,self.target,{},None)
                if scenario == 'no-time':
                    self.assertTrue(report['deferred_repair'])
                    self.assertEqual(records[-1]['status'], 'waiting')
                    self.assertEqual((self.target/'sealed'/'report.html').read_text(), '缺少原样例，已有新稿')
                    self.assertEqual(report['checkpoint']['digest'],records[-1]['before_sha256'])
                    self.assertNotIn('error',report)
                    continue
                if scenario != 'pass':
                    self.assertIn('仍需修改', report['error'])
                    self.assertNotIn('checkpoint', report)
                    self.assertEqual((self.target/'sealed').exists(), scenario == 'unresolved')
                    self.assertEqual(records[-1]['status'], 'blocked')
                    continue
                self.assertEqual([call['sandbox'] for call in calls],
                                 ['read-only','workspace-write','read-only','workspace-write','read-only'])
                self.assertEqual([record['status'] for record in records],
                                 ['repairing','rechecking','rechecking','passed'])
                self.assertNotEqual(records[-1]['before_sha256'], records[-1]['after_sha256'])
                self.assertEqual(records[-1]['changed_files'], ['report.html'])
                results = self.engine.finish_team_role.call_args.args[2]
                final = {'role':'lead','writable':False,'status':'completed',**results}
                assignment = {**self.assignment,'inner_repairs':records[-1:], 'role_calls':[final]}
                GoalEngine._verify_team_repair(assignment, report['checkpoint'])
                with self.assertRaises(Problem):
                    GoalEngine._verify_team_repair({**assignment,'role_calls':[{**final,'verification':None}]},
                                                  report['checkpoint'])
                with self.assertRaises(Problem):
                    GoalEngine._verify_team_repair(assignment, {'digest':'另一版'})
                with self.assertRaises(Problem):
                    GoalEngine._verify_team_repair({'role_calls':[
                        {'role':'lead','writable':False,'action':'repair'}, final,
                    ]}, report['checkpoint'])

    def test_worker_verifies_both_html_clauses_and_retained_paths(self):
        path = 'report.html'
        rows = [
            {'criterion_id':key, 'required':True, 'verifier_id':'html-document', 'verifier_version':1,
             'test_spec':{'path':path}, 'expected_result':{'title':'Daily Brief','headings':[heading],
                                                         'element_ids':[element]},
             'environment':LOCAL_DATA,'human_review_required':False}
            for key,heading,element in [('tasks','Daily Tasks','task-count'),('notes','Status Notes','task-notes')]
        ]
        acceptance = {'criteria':rows}
        self.assignment.update(
            run=12, max_seconds=240, outer_acceptance=acceptance, preference_answer=None,
            outer_binding={'contract_digest':sha256(acceptance)},
        )
        self.assignment['context']['approved_criteria'] = [{**row,'interface':{'path':path}} for row in rows]
        bad_html = ('<!doctype html><title>Daily Brief</title><h1>Draft Tasks</h1><p id="task-count">3</p>'
                    '<h1>Status Notes</h1><p id="task-notes">Actual notes.</p>')
        fixed = bad_html.replace('Draft Tasks','Daily Tasks')
        for scenario in ('pass','fake-command','damaged-html','deleted-file','extra-file','altered-candidate'):
            with self.subTest(scenario=scenario), TemporaryDirectory() as directory:
                target = Path(directory).resolve()
                calls, records = [], []
                self.engine.record_inner_repair.side_effect = lambda _, record: records.append(dict(record))

                def invoke(**options):
                    calls.append(options)
                    workspace = options['workspace']
                    number = len(calls)
                    if number == 2:
                        (workspace/path).write_text(bad_html)
                        (workspace/'notes.txt').write_text('另一个文件必须按原始字节保留')
                    if number == 4:
                        actual = (bad_html + '\n' if scenario == 'fake-command' else
                                  fixed.replace('id="task-notes"','') if scenario == 'damaged-html' else fixed)
                        (workspace/path).write_text(actual)
                        if scenario == 'deleted-file':
                            (workspace/'notes.txt').unlink()
                        if scenario == 'extra-file':
                            (workspace/'unexpected.txt').write_text('未经登记的增补')
                    if number == 5 and scenario == 'altered-candidate':
                        (target/'candidate'/path).write_text(bad_html)
                    return {
                        'model':options['model'],
                        'tools':synthetic_reads(options),
                        'result':dict(action='repair' if number == 3 else 'done', summary='实际标题复验',
                                      options=[],retain=[],verify=['已成功执行 true'],
                                      repair_scope=dict(retained=[{'path':'notes.txt','excerpt':''}],
                                                        changes=[],removed_paths=[])),
                    }

                with patch('superagent.goal_app_server.invoke',side_effect=invoke):
                    report = run_team(self.engine,self.assignment,self.base,target,{},None)
                verification = records[-1]['verification']
                self.assertEqual([row['status'] for row in verification['before_checks']], ['FAIL','PASS'])
                after = ('FAIL','PASS') if scenario == 'fake-command' else (
                    ('PASS','FAIL') if scenario == 'damaged-html' else ('PASS','PASS'))
                self.assertEqual([row['status'] for row in verification['checks']], list(after))
                if scenario != 'pass':
                    self.assertIn('仍需修改',report['error'])
                    self.assertNotIn('checkpoint',report)
                    self.assertTrue((target/'sealed').is_dir())
                    self.assertEqual(records[-1]['status'],'blocked')
                    continue
                self.assertEqual(records[-1]['status'],'passed')
                final = {'role':'lead','writable':False,'status':'completed',**self.engine.finish_team_role.call_args.args[2]}
                assignment = {**self.assignment,'inner_repairs':records[-1:],'role_calls':[final]}
                GoalEngine._verify_team_repair(assignment,report['checkpoint'])
                tampered = {**verification,'acceptance_sha256':'0' * 64}
                with self.assertRaises(Problem):
                    GoalEngine._verify_team_repair({
                        **assignment,'role_calls':[{**final,'verification':tampered}],
                        'inner_repairs':[{**records[-1],'verification':tampered}],
                    },report['checkpoint'])

    def test_deferred_repair_reuses_original_scope_with_two_roles(self):
        self.assignment.update(run=3,max_seconds=120,role_slots=3,preference_answer=None)
        self.assignment['context']['approved_criteria'] *= 2
        path = '简报.md'
        old, new = '旧标题\n保留备注\n', '新版标题\n保留备注\n'
        calls, records = [], []
        self.engine.record_inner_repair.side_effect = lambda _, record: records.append(dict(record))
        scope = {'retained':[{'path':path,'excerpt':'保留备注'}],
                 'changes':[{'path':path,'before':'旧标题','after':'新版标题'}],'removed_paths':[]}

        def invoke(**options):
            calls.append(options['sandbox'])
            candidate = options['workspace']/path
            if len(calls) == 2:
                candidate.write_text(old)
            elif len(calls) == 4:
                self.assertEqual(candidate.read_text(),old)
                candidate.write_text(new)
            elif len(calls) == 5:
                self.assertEqual(candidate.read_text(),new)
            result = dict(action='repair' if len(calls) == 3 else 'done', summary='修改标题并保留备注',
                          options=[],retain=[],verify=[])
            if len(calls) == 3:
                result['repair_scope'] = scope
            return {'model':options['model'],'result':result,'tools':synthetic_reads(options)}

        with patch('superagent.goal_app_server.invoke',side_effect=invoke):
            deferred = run_team(self.engine,self.assignment,self.base,self.target,{},None)
            self.assertTrue(deferred['deferred_repair'])
            source = records[-1]
            continuation = {'source_assignment':self.assignment['id'],'base_sha256':deferred['checkpoint']['digest'],
                            'repair_record':source,'finding':deferred['result']}
            renewed = {**self.assignment,'id':'renewed','run':4,'role_slots':2,'repair_continuation':continuation}
            next_attempt = self.target/'next'
            next_attempt.mkdir()
            result = run_team(self.engine,renewed,self.target/'sealed',next_attempt,{},None)
            self.assertEqual(calls[-2:],['workspace-write','read-only'])
            self.assertEqual(len(calls),5)
            self.assertEqual((next_attempt/'sealed'/path).read_text(),new)
            self.assertEqual(records[-1]['status'],'passed')
            last = {'role':'lead','writable':False,'status':'completed',**self.engine.finish_team_role.call_args.args[2]}
            GoalEngine._verify_team_repair(
                {**renewed,'inner_repairs':[records[-1]],'role_calls':[last]},result['checkpoint'])
            with self.assertRaises(Problem):
                GoalEngine._verify_team_repair({**renewed,'role_calls':[last]},result['checkpoint'])
            invalid_attempt = self.target/'invalid'
            invalid_attempt.mkdir()
            wrong = {**renewed,'repair_continuation':{**continuation,'base_sha256':'1' * 64}}
            invalid = run_team(self.engine,wrong,self.target/'sealed',invalid_attempt,{},None)
            self.assertIn('仍需修改',invalid['error'])
            self.assertNotIn('deferred_repair',invalid)
            self.assertEqual(len(calls),5)

    def test_written_repair_is_recovered_when_only_the_lead_check_needs_time(self):
        self.check_written_repair_resume('deadline', final_proof=True)

    def test_written_repair_retries_missing_lead_read_proof_once(self):
        self.check_written_repair_resume('missing-proof', final_proof=True)

    def test_read_only_retry_without_proof_stays_blocked(self):
        self.check_written_repair_resume('missing-proof', final_proof=False)

    def check_written_repair_resume(self, initial_issue, *, final_proof):
        self.assignment.update(run=3, version=1, ticket='test-ticket', mandate_digest='frozen',
                               max_seconds=240, role_slots=5, preference_answer=None)
        self.assignment['context']['approved_criteria'] *= 2
        (self.target/'entered').write_text('3')
        before, after = '旧标题\n保留正文\n', '新标题\n保留正文\n'
        now, records, sandboxes = 10, [], []
        self.engine.record_inner_repair.side_effect = lambda _, record: records.append(dict(record))

        def invoke(**options):
            nonlocal now
            sandboxes.append(options['sandbox'])
            candidate = options['workspace']/'brief.txt'
            turn = len(sandboxes)
            result = dict(action='done', summary='已逐项处理', options=[], retain=[], verify=[])
            if turn == 2:
                candidate.write_text(before)
            if turn == 3:
                result.update(action='repair', summary='改标题并保留正文', repair_scope=dict(
                    retained=[dict(path='brief.txt',excerpt='保留正文')],
                    changes=[dict(path='brief.txt',before='旧标题',after='新标题')],removed_paths=[],
                ))
            if turn == 4:
                self.assertEqual(candidate.read_text(),before)
                candidate.write_text(after)
                if initial_issue == 'deadline':
                    now = options['deadline'] - 5
            if turn >= 5:
                self.assertEqual(candidate.read_text(),after)
                self.assertEqual(S_IMODE(candidate.stat().st_mode),0o400)
            initial_proof_missing = turn == 5 and initial_issue == 'missing-proof'
            repeated_proof_missing = turn == 6 and not final_proof
            tools = [] if (initial_proof_missing or repeated_proof_missing) else synthetic_reads(options)
            return dict(model=options['model'],result=result,tools=tools)

        report_headers = {key:self.assignment[key] for key in ('id','run','version')}
        with patch('superagent.goal_team_run.time.monotonic',side_effect=lambda: now), \
             patch('superagent.goal_app_server.invoke',side_effect=invoke):
            pending = run_team(self.engine,self.assignment,self.base,self.target,report_headers,None)
            prior = ['read-only','workspace-write','read-only','workspace-write']
            if initial_issue == 'missing-proof':
                prior.append('read-only')
                failed_call = self.engine.finish_team_role.call_args
                self.assertEqual(failed_call.kwargs['status'],'failed')
                self.assertEqual(failed_call.args[2]['error_code'],'candidate_read_unproven')
            self.assertEqual(sandboxes,prior)
            self.assertEqual(pending,read_team_recovery(self.target,self.assignment))
            self.assertEqual((self.target/'sealed'/'brief.txt').read_text(),after)
            writer = self.engine.finish_team_role.call_args_list[3].args[2]
            self.assertEqual(writer['candidate_sha256'],pending['checkpoint']['digest'])
            self.assertEqual(writer['summary'],pending['repair_review_pending']['implementation']['summary'])
            self.assertNotEqual(records[-1]['before_sha256'], pending['checkpoint']['digest'])

            continued_path = self.target/'review'
            continued_path.mkdir()
            original = dict(records[-1],status='waiting_review')
            continuation = dict(source_assignment=self.assignment['id'],base_sha256=pending['checkpoint']['digest'],
                                finding=pending['result'], repair_record=original,
                                review_only=pending['repair_review_pending'])
            renewed = dict(self.assignment,id='only-lead',run=4,role_slots=1,repair_continuation=continuation)
            if not final_proof:
                with self.assertRaises(CandidateReadUnproven):
                    run_team(self.engine,renewed,self.target/'sealed',continued_path,{},None)
                self.assertEqual(sandboxes[len(prior):],['read-only'])
                self.assertFalse((continued_path/'candidate-recovery.json').exists())
                return
            final = run_team(self.engine,renewed,self.target/'sealed',continued_path,{},None)
            self.assertEqual(sandboxes[len(prior):],['read-only'])
            self.assertNotIn('repair_review_pending',final)
            self.assertEqual(final['checkpoint'],pending['checkpoint'])
            self.assertEqual((continued_path/'sealed'/'brief.txt').read_text(),after)
            self.assertEqual(records[-1]['status'],'passed')
