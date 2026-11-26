"""隔离 SQLite 与结构化模型协议替身。"""
import json
import hashlib
from concurrent.futures import ThreadPoolExecutor
import tempfile
from pathlib import Path
from threading import Event
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench import Problem, Store, Workbench

from superagent.outer_model import ModelFailure


class Registry:
    def catalogue(self):
        return [{'verifier_id': 'test-fixture', 'version': 1}]

    def compile_proposal(self, goal, proposal):
        criteria = proposal['criteria']
        required = {item['requirement_id'] for item in goal['deliverables']}
        covered = {key for item in criteria if item['required'] for key in item['requirement_ids']}
        status = 'READY_FOR_APPROVAL' if required == covered else 'VERIFICATION_GAP'
        return {'criteria': criteria, 'status': status,
                'verification_gaps': [] if status == 'READY_FOR_APPROVAL' else ['缺少覆盖'],
                'human_review_requirements': [],
                'coverage_assessment': {'covered': sorted(covered), 'authorization_controls': {},
                                        'uncovered': sorted(required - covered)}}


class Model:
    def __init__(self):
        self.calls = []
        self.on_call = None
        self.fails = 0

    def __call__(self, role, context, schema):
        self.calls.append((role, context))
        if self.on_call:
            self.on_call(role)
        if self.fails:
            self.fails -= 1
            raise ModelFailure('timeout')
        event = context['event_id']
        text = context['untrusted_text']['current_message']
        if role == 'acceptance_designer':
            criterion = dict(
                criterion_id='R1', requirement_ids=[item['requirement_id'] for item in context['goal_contract']['deliverables']],
                description='实际运行软件并验证基础结果', required=True, verification_type='DETERMINISTIC',
                verifier_id='test-fixture', verifier_version=1, test_spec_json='{}', expected_result_json='true',
                evidence_required=['structured_output'], human_review_required=False, source_refs=[event],
            )
            return dict(schema_version=1, ticket_id=context['ticket_id'], goal_revision=context['goal_revision'],
                        artifact_types=['software'], criteria=[] if 'gap' in text else [criterion],
                        verification_gaps=[], human_review_requirements=[], source_refs=[event])
        question = dict(question_text='', options=[], reason='', blocking_scope='')
        if 'vague' in text:
            question.update(question_text='交付命令行还是网页？', options=['命令行', '网页'],
                            reason='决定交付格式和验收行为', blocking_scope='deliverables')
        allowed = context['available_actions']
        candidates = []
        if 'select' in text and 'REQUEST_TICKET_SELECTION' in allowed:
            action = 'REQUEST_TICKET_SELECTION'
            candidates = [row['id'] for row in context['candidate_tickets']]
        elif text == 'invalid':
            action = 'AUTHORIZED'
        else:
            action = next(item for item in ('ANSWER_CLARIFICATION', 'PROPOSE_CHANGE' if 'change' in text else 'ATTACH_TO_TICKET',
                                            'PROPOSE_CHANGE', 'CREATE_DRAFT') if item in allowed)
        requirement = dict(description=text, source='USER_EXPLICIT', source_refs=[event])
        invented = dict(description='用户明确要求加载远程插件', source='USER_EXPLICIT', source_refs=[event])
        return dict(schema_version=1, event_id=event, input_snapshot_revision=context['snapshot_revision'],
                    intent='message', relation_type='thread' if context['ticket_id'] else 'none',
                    candidate_ticket_ids=candidates, relation_evidence_refs=[event],
                    goal_draft=dict(goal_statement='目标 ' + text, deliverables=[requirement],
                                    required_capabilities=[], nonfunctional_requirements=[],
                                    constraints=[invented] if 'invent' in text else [],
                                    assumptions=[], out_of_scope=[],
                                    **({'preferences':[],'design_conditions':[]} if 'preferences' in schema['properties']['goal_draft']['properties'] else {})),
                    **({'preference_support':[],'question_targets':[]} if 'preference_support' in schema['properties'] else {}),
                    assumptions=[], critical_ambiguities=['交付格式'] if question['question_text'] else [],
                    proposed_question=question, suggested_action=action, reason='根据当前线程处理')


class OuterIntakeTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name).resolve())
        self.model = Model()
        self.workbench = Workbench(self.store, goal_enabled=True, outer_enabled=True,
                                   outer_model=self.model, outer_registry=Registry())
        self.intake = self.workbench.outer()

    def send(self, message, event='source-message', **extra):
        return self.intake.submit(source='feishu', source_event_id=event, actor_id='owner', text=message, **extra)

    def latest(self, event):
        self.intake.drain()
        result = self.intake.event(event['event_id'], 'owner')
        return self.intake.get(result['ticket_id'], 'owner') if result['ticket_id'] else result

    def approve(self, draft, event='approval', **extras):
        return self.intake.approve_goal_contract(
            draft['id'], draft['goal_revision'], draft['acceptance_revision'], draft['revision'], event,
            actor_id='owner', **extras)

    def control(self, draft, operation, source_event):
        return self.intake.control(draft['id'], operation, draft['revision'], source_event, actor_id='owner')

    def router_new(self, text='交付合成简报', **fields):
        from outer_workbench import create_task

        self.workbench.outer_workbench_entry = True
        self.workbench.outer_goal_team = True
        self.intake.team_model = self.model
        body = dict(workflow='outer', request_id=str(uuid4()), name='团队工单', goal=text,
                    acceptance='核对产物内容', allow_model_input=True, **fields)
        return create_task(self.workbench, body), body

    def test_router_registers_ticket_and_keeps_team_queue_separate_from_legacy(self):
        from outer_workbench import create_task

        first, body = self.router_new()
        second, _ = self.router_new('交付另一张合成简报')
        self.assertNotEqual(first['ticket_id'], second['ticket_id'])
        self.assertEqual(create_task(self.workbench, body)['ticket_id'], first['ticket_id'])
        self.assertTrue(create_task(self.workbench, body)['duplicate'])
        ticket = self.intake.get(first['ticket_id'], 'owner')
        self.assertEqual(ticket['original_request']['goal'], body['goal'])
        self.assertEqual(ticket['approval_terms']['team_review_mode'], 'adaptive_review')
        self.assertEqual(ticket['approval_terms']['execution_profile']['roles']['advisor'], 'gpt-6.2-astra')
        self.assertEqual(ticket['approval_terms']['execution_model'], 'gpt-6.2-sol')
        self.assertEqual(ticket['subscription']['bindings'], [{'source':'web','thread_id':ticket['id']}])
        self.assertEqual(self.intake.pending_team_tickets(), [first['ticket_id'], second['ticket_id']])
        self.assertEqual(self.intake.drain(), 0)
        self.assertFalse(self.model.calls)
        self.assertEqual(self.intake.drain_ticket(first['ticket_id'], limit=2), 2)
        self.assertEqual(self.intake.pending_team_tickets(), [second['ticket_id']])
        self.assertEqual([role for role, _ in self.model.calls], ['intake_assessor','acceptance_designer'])
        for _, context in self.model.calls:
            self.assertEqual(context['ticket_team'], ticket['ticket_team'])
            self.assertEqual(context['team_review_mode'], 'adaptive_review')
            self.assertNotIn('candidate_tickets', context)
            self.assertNotIn(second['ticket_id'], json.dumps(context))
        approved = self.approve(self.intake.get(first['ticket_id'], 'owner'))
        self.assertEqual(approved['mandate']['workflow_profile'], 'router-team-v1')
        self.assertEqual(approved['goal_contract']['workflow_profile'], 'router-team-v1')

    def test_router_subscription_routes_by_exact_thread_without_model_or_cross_ticket_fallback(self):
        first, _ = self.router_new()
        second, _ = self.router_new()
        first_id, second_id = first['ticket_id'], second['ticket_id']
        initial = self.intake.get(first_id, 'owner')
        self.intake.bind_thread(first_id, 'feishu', 'source-card-a', actor_id='owner')
        self.intake.bind_thread(second_id, 'feishu', 'source-card-b', actor_id='owner')
        with self.assertRaises(Problem):
            self.intake.bind_thread(second_id, 'feishu', 'source-card-a', actor_id='owner')
        with self.assertRaises(Problem):
            self.send('错单', event='mismatch', source_thread_id='source-card-a', ticket_id=second_id)
        with self.assertRaises(Problem):
            self.send('未绑定', event='missing-thread', ticket_id=first_id)
        with self.assertRaises(Problem):
            self.send('其他租户', event='wrong-tenant', source_thread_id='source-card-a',
                      ticket_id=first_id, tenant='other')
        result = self.send('补充甲', event='linked-message', source_thread_id='source-card-a')
        retry = self.send('补充甲', event='linked-message', source_thread_id='source-card-a')
        self.assertEqual(result['action'], 'ATTACH_TO_TICKET')
        self.assertEqual(result['ticket_id'], first_id)
        self.assertEqual(retry['event_id'], result['event_id'])
        self.assertTrue(retry['duplicate'])
        self.assertFalse(self.model.calls)
        details = self.intake.get(first_id, 'owner')
        self.assertEqual(details['revision'], initial['revision'])
        self.assertEqual([entry['text'] for entry in details['subscription']['entries']], ['补充甲'])
        self.assertFalse(details['subscription']['entries'][0]['allow_model_input'])
        self.assertEqual(self.intake.get(second_id, 'owner')['subscription']['entries'], [])
        unlinked = self.send('unlinked thread text', event='unknown-card', source_thread_id='unlinked')
        self.assertTrue(unlinked['router_selection'])
        self.assertEqual({row['id'] for row in unlinked['choices']}, {first_id, second_id})
        self.assertEqual(self.intake.drain(limit=1), 0)
        self.assertFalse(self.model.calls)

    def test_router_unbound_sources_wait_for_explicit_selection_and_web_model_authorization(self):
        self.workbench.outer_goal_team = True
        self.intake.team_model = self.model
        unlinked = self.send('刚收到的原话', source_thread_id='new-thread', allow_model_input=True)
        self.assertTrue(unlinked['router_selection'])
        self.assertEqual(unlinked['choices'], [])
        self.assertIsNone(unlinked['ticket_id'])
        registered = self.intake.select_ticket(unlinked['event_id'], None, actor_id='owner', source='feishu',
                                               source_event_id='create-from-thread')
        ticket_id = registered['ticket_id']
        draft = self.intake.get(ticket_id, 'owner')
        self.assertEqual(draft['original_request'], {'text':'刚收到的原话'})
        self.assertTrue(draft['needs_model_consent'])
        self.assertEqual(self.intake.pending_team_tickets(), [])
        self.assertEqual(self.intake.drain(), 0)
        self.assertFalse(self.model.calls)
        with self.assertRaises(Problem):
            self.intake.authorize_model_input(ticket_id, draft['revision'], 'remote-consent',
                                              actor_id='owner', source='feishu')
        authorization = self.intake.authorize_model_input(ticket_id, draft['revision'], 'web-consent', actor_id='owner')
        self.assertEqual(authorization['action'], 'MODEL_INPUT_AUTHORIZED')
        self.assertTrue(self.intake.authorize_model_input(
            ticket_id, draft['revision'], 'web-consent', actor_id='owner')['duplicate'])
        self.assertEqual(self.intake.pending_team_tickets(), [ticket_id])
        self.assertEqual(self.intake.drain_ticket(ticket_id, limit=2), 2)
        self.assertEqual(self.model.calls[0][1]['untrusted_text']['current_message'], '刚收到的原话')
        self.assertFalse(self.intake.get(ticket_id, 'owner')['needs_model_consent'])

    def test_team_scopes_preferences_and_recovers_an_existing_gap_on_original_ticket(self):
        from superagent import outer_verification as verification
        import copy

        self.intake.registry = verification
        goal_text = '请整理展览内容；画一张海报；最好给我一个可直接下载的版本'
        first, _ = self.router_new(goal_text)
        ticket_id = first['ticket_id']
        old = True

        def propose(role, context, schema):
            result = self.model(role, context, schema)
            if role == 'intake_assessor':
                ref = context['original_request_refs'][0]
                draft = result['goal_draft']
                draft.update(goal_statement='整理展览内容及海报',
                             deliverables=[dict(description=text, source='USER_EXPLICIT', source_refs=[ref])
                                           for text in goal_text.split('；')],
                             required_capabilities=[dict(description='核实实时开放的下载站点', source='MODEL_PROPOSED',
                                                         source_refs=[ref])])
                result['preference_support'] = [dict(preference_quote=goal_text.split('；')[-1],
                                                     derived_description='核实实时开放的下载站点')]
                result['critical_ambiguities'] = ['团队尚未确定表现方式']
                result['proposed_question'].update(question_text='请逐项填写海报验收方式？', options=[],
                                                   reason='团队未确定制作方法', blocking_scope='acceptance')
            else:
                goal = context['goal_contract']
                required = goal['deliverables']
                # 先保留一张确实被旧版卡住的单，再经普通修改入口重评。
                if old:
                    gaps = [(required[0], 'independent_evidence')]
                else:
                    gaps = [(row,'artifact_inspection') for row in required]
                    gaps += [(goal['preferences'][0], 'independent_evidence'),
                             (goal['required_capabilities'][0], 'independent_evidence')]
                result.update(criteria=[], verification_gaps=[
                    dict(requirement_id=row['requirement_id'], reason='核对' + row['description'],
                         evidence_kind=kind) for row,kind in gaps])
            return result

        self.intake.team_model = propose
        self.assertEqual(self.intake.drain_ticket(ticket_id, limit=2), 2)
        initial = self.intake.get(ticket_id, 'owner')
        self.assertEqual(initial['status'], 'VERIFICATION_GAP')
        with self.store.connect() as db:
            original = self.intake._ticket(db,ticket_id)
            legacy = copy.deepcopy(original['goal_contract'])
            legacy['deliverables'] += legacy.pop('preferences')
            legacy.pop('direction_owner')
            legacy.pop('direction_to_compare',None)
            for row in legacy['required_capabilities']:
                row.pop('acceptance_role',None)
            original['goal_contract'] = legacy
            self.intake._save(db, original)
        old = False
        self.intake.submit(source='web', source_event_id='same-ticket-reassess', actor_id='owner',
                           ticket_id=ticket_id, expected_ticket_revision=initial['revision'],
                           text='请帮我拟方案，让我选择', change_request=True, allow_model_input=True)
        self.assertEqual(self.intake.drain_ticket(ticket_id, limit=2), 2)
        draft = self.intake.get(ticket_id, 'owner')
        self.assertEqual((draft['status'],draft['id'],draft['questions_asked']), ('AWAITING_APPROVAL',ticket_id,0))
        goal = draft['goal_contract']
        self.assertEqual([item['description'] for item in goal['deliverables']],goal_text.split('；')[:2])
        self.assertEqual([item['description'] for item in goal['preferences']],goal_text.split('；')[2:])
        self.assertEqual(goal['required_capabilities'][0]['acceptance_role'],'advisory')
        self.assertEqual(goal['direction_owner'], 'owner')
        contract = draft['acceptance_contract']
        self.assertEqual(contract['verification_gaps'], [])
        self.assertEqual(len(contract['preference_notes']), 2)
        self.assertEqual(len(contract['human_review_requirements']), 2)
        self.assertEqual(self.approve(draft)['mandate_revision'], 1)

    def test_workbench_proposal_choice_recovers_old_button_contamination(self):
        from outer_channels import dispatch
        from superagent import outer_verification as verification

        self.intake.registry = verification
        original_text = '请写读书会导读并画一张海报'
        first, _ = self.router_new(original_text)
        ticket_id = first['ticket_id']
        old_text = '请团队依据原要求重新研判。复杂任务请先比较方案。采用团队推荐。'
        old_clause = '复杂任务请先比较方案。'
        recovering = False

        def propose(role, context, schema):
            result = self.model(role, context, schema)
            if role == 'intake_assessor':
                ref = context['original_request_refs'][0]
                result['goal_draft'].update(goal_statement=original_text, deliverables=[
                    dict(description=original_text, source='USER_EXPLICIT', source_refs=[ref])])
                if recovering:
                    self.assertEqual(context['proposal_request'], 'owner')
                    self.assertEqual(context['untrusted_text']['current_message'], '')
                    self.assertNotIn(old_text, context['untrusted_text']['prior_messages'].values())
                    self.assertEqual(context['goal_contract']['constraints'], [])
                    # 模型即使把流程字段又抄回条款，编译器也要剔除。
                    result['goal_draft']['constraints'] = [dict(description=old_clause, source='MODEL_PROPOSED',
                                                                  source_refs=[context['event_id']])]
            else:
                row, = context['goal_contract']['deliverables']
                kind = 'artifact_inspection' if recovering else 'independent_evidence'
                result.update(criteria=[], verification_gaps=[dict(requirement_id=row['requirement_id'],
                    reason='读取实际导读和海报', evidence_kind=kind)])
                if recovering:
                    criterion = dict(requirement_ids=[row['requirement_id']], required=True, verification_type='HUMAN_REVIEW',
                                     verifier_id='human-review', verifier_version=1, test_spec_json='{}',
                                     expected_result_json='null', evidence_required=['检查成品'], human_review_required=True)
                    result['criteria'] = [
                        dict(criterion, criterion_id='fake-process', description=old_clause,
                             source_refs=[context['event_id']]),
                        dict(criterion, criterion_id='real-delivery', description='核对实际导读和海报',
                             source_refs=row['source_refs']),
                    ]
                    result['human_review_requirements'] = ['fake-process','real-delivery']
            return result

        self.intake.team_model = propose
        self.assertEqual(self.intake.drain_ticket(ticket_id, limit=2), 2)
        with self.store.connect() as db:
            draft = self.intake._ticket(db,ticket_id)
            identity = self.intake._source('web', 'legacy-choice', 'owner', 'default', None)
            old, _ = self.intake._insert(db, identity, ticket_id, {'text':old_text, 'change_request':True})
            self.intake._finish(db, old, 'DONE', {'action':'VERIFICATION_GAP'}, draft, deliver=False)
            draft['relevant_thread_refs'].append(old['id'])
            draft['goal_contract'].update(direction_owner='team', constraints=[
                dict(description=old_clause, source='USER_EXPLICIT', source_refs=[old['id']],
                     requirement_id='req-'+'a'*32)])
            self.intake._save(db,draft)
        recovering = True
        request = dict(ticket_id=ticket_id, revision=draft['revision'], request_id=str(uuid4()),
                       intent='proposal', direction_owner='owner', allow_model_input=True)
        with self.assertRaises(Problem):
            dispatch(self.intake, 'web', 'submit', dict(request, revision=draft['revision']-1))
        receipt = dispatch(self.intake,'web','submit',request)
        self.assertTrue(dispatch(self.intake,'web','submit',request)['duplicate'])
        with self.assertRaises(Problem):
            dispatch(self.intake,'web','submit',dict(request,request_id=str(uuid4()),direction_owner='team'))
        self.assertEqual(receipt['ticket_id'],ticket_id)
        self.assertEqual(self.intake.drain_ticket(ticket_id,limit=2),2)
        ready = self.intake.get(ticket_id,'owner')
        self.assertEqual(ready['status'],'AWAITING_APPROVAL')
        goal = ready['goal_contract']
        self.assertEqual((goal['direction_owner'],goal['proposal_requested']),('owner',True))
        self.assertEqual(goal['constraints'],[])
        self.assertEqual([row['description'] for row in goal['deliverables']],[original_text])
        self.assertEqual(ready['acceptance_contract']['verification_gaps'],[])
        self.assertEqual([row['criterion_id'] for row in ready['acceptance_contract']['criteria']],['real-delivery'])
        self.assertEqual(self.approve(ready)['mandate']['team_review_mode'],'adaptive_review')

    def test_venue_context_and_soft_suffix_are_separate_from_operational_controls(self):
        from superagent import outer_verification as verification
        self.intake.registry = verification
        original = '写一份方案。场地有限。最好也附一个公开阅读入口，如果有的话。另做一张海报。'
        first, _ = self.router_new(original)
        def propose(role, context, schema):
            result = self.model(role, context, schema)
            if role == 'intake_assessor':
                ref = context['original_request_refs'][0]
                def item(value):
                    return dict(description=value,source='USER_EXPLICIT',source_refs=[ref])
                result['goal_draft'].update(
                    deliverables=[item('写一份方案'),item('最好也附一个公开阅读入口，如果有的话'),item('另做一张海报')],
                    constraints=[item('场地有限')])
                if '不得外发' in context['untrusted_text']['current_message']:
                    result['goal_draft']['design_conditions'] = [
                        dict(description='不得外发',source='USER_EXPLICIT',source_refs=[context['event_id']])]
            else:
                result.update(criteria=[], verification_gaps=[dict(
                    requirement_id=item['requirement_id'],reason='交付后从成品核对',evidence_kind='artifact_inspection')
                    for item in context['goal_contract']['deliverables']])
            return result

        self.intake.team_model = propose
        self.intake.drain_ticket(first['ticket_id'],limit=2)
        ticket = self.intake.get(first['ticket_id'],'owner')
        goal = ticket['goal_contract']
        self.assertEqual(ticket['status'],'AWAITING_APPROVAL')
        self.assertEqual(goal['constraints'],[])
        self.assertEqual([i['description'] for i in goal['design_conditions']], ['场地有限'])
        self.assertEqual([i['description'] for i in goal['deliverables']], ['写一份方案','另做一张海报'])
        self.assertEqual([i['source'] for i in goal['preferences']], ['USER_EXPLICIT'])
        self.assertTrue(any('场地有限' in row for row in ticket['acceptance_contract']['human_review_requirements']))
        with self.store.connect() as db:
            legacy = self.intake._ticket(db,ticket['id'])
            old_goal = legacy['goal_contract']
            old_goal['constraints'] = old_goal.pop('design_conditions')
            self.intake._save(db,legacy)
        self.intake.submit(source='web',source_event_id='protected-operational-scope',actor_id='owner',
                           ticket_id=ticket['id'], expected_ticket_revision=ticket['revision'],
                           text='不得外发',change_request=True,allow_model_input=True)
        self.intake.drain_ticket(ticket['id'],limit=2)
        latest = self.intake.get(ticket['id'],'owner')
        self.assertEqual(latest['status'],'VERIFICATION_GAP')
        self.assertEqual([i['description'] for i in latest['goal_contract']['constraints']],['不得外发'])
        self.assertEqual([i['description'] for i in latest['goal_contract']['design_conditions']],['场地有限'])

    def test_optional_format_question_cannot_block_and_user_direction_is_not_a_permission(self):
        first, _ = self.router_new('为蓝色星球写导读；做海报；最好提供可下载的版本；重要方向由我选择')
        def propose(role,context,schema):
            result = self.model(role,context,schema)
            if role == 'intake_assessor':
                ref = context['event_id']
                def anchored(text):
                    return dict(description=text,source='USER_EXPLICIT',source_refs=[ref])
                result['goal_draft'].update(deliverables=[anchored('为蓝色星球写导读'),anchored('做海报')],
                                            preferences=[anchored('最好提供可下载的版本')],
                                            constraints=[anchored('重要方向由我选择')])
                result['critical_ambiguities'] = ['下载内容']
                result['proposed_question'].update(question_text='你想下载书、导读还是海报？',reason='下载对象有歧义',
                                                   blocking_scope='deliverables')
                result['question_targets'] = ['最好提供可下载的版本']
            return result
        self.intake.team_model = propose
        self.intake.drain_ticket(first['ticket_id'],limit=2)
        ticket = self.intake.get(first['ticket_id'],'owner')
        self.assertEqual((ticket['status'],ticket['questions_asked']),('AWAITING_APPROVAL',0))
        self.assertEqual(ticket['goal_contract']['constraints'],[])
        self.assertEqual(ticket['goal_contract']['direction_owner'],'owner')
        self.assertIn('direction_to_compare',ticket['goal_contract'])

    def test_missing_book_identity_still_requires_one_question(self):
        first, _ = self.router_new('为那本书写导读；最好提供可下载版本')
        def propose(role,context,schema):
            result = self.model(role,context,schema)
            if role == 'intake_assessor':
                ref = context['event_id']
                def anchored(text):
                    return dict(description=text,source='USER_EXPLICIT',source_refs=[ref])
                result['goal_draft'].update(deliverables=[anchored('为那本书写导读')],
                                            preferences=[anchored('最好提供可下载版本')])
                result['critical_ambiguities'] = ['没有书名']
                result['proposed_question'].update(question_text='你想了解哪一本书？',reason='需要知道阅读对象',
                                                   blocking_scope='deliverables')
                result['question_targets'] = ['为那本书写导读']
            return result
        self.intake.team_model = propose
        self.intake.drain_ticket(first['ticket_id'],limit=2)
        ticket = self.intake.get(first['ticket_id'],'owner')
        self.assertEqual((ticket['status'],ticket['questions_asked']),('AWAITING_CLARIFICATION',1))

    def test_user_may_delegate_or_reclaim_proposal_direction_in_same_ticket(self):
        first, _ = self.router_new('请写展览导读；你帮我决定')
        self.intake.drain_ticket(first['ticket_id'],limit=2)
        ticket = self.intake.get(first['ticket_id'],'owner')
        self.assertEqual(ticket['goal_contract']['direction_owner'],'team')
        self.intake.submit(source='web',source_event_id='choose-myself',actor_id='owner',ticket_id=ticket['id'],
                           expected_ticket_revision=ticket['revision'],text='先给我看看方向，让我选择',
                           allow_model_input=True,change_request=True)
        self.intake.drain_ticket(ticket['id'],limit=2)
        current = self.intake.get(ticket['id'],'owner')
        self.assertEqual(current['goal_contract']['direction_owner'],'owner')
        self.assertIn('direction_to_compare', current['goal_contract'])

    def test_asking_how_to_verify_a_simple_edit_does_not_force_comparison(self):
        first, _ = self.router_new('把标题改成会议指南')
        def propose(role, context, schema):
            result = self.model(role, context, schema)
            if role == 'intake_assessor':
                result['critical_ambiguities'] = ['尚未说明怎样检验']
                result['proposed_question'].update(question_text='应如何详细验收标题？', options=[],
                                                   reason='需要设计验收', blocking_scope='acceptance')
            return result
        self.intake.team_model = propose
        self.assertEqual(self.intake.drain_ticket(first['ticket_id'], limit=2), 2)
        ticket = self.intake.get(first['ticket_id'],'owner')
        self.assertEqual(ticket['questions_asked'],0)
        self.assertEqual(ticket['status'],'AWAITING_APPROVAL')
        self.assertNotIn('direction_to_compare',ticket['goal_contract'])

    def test_independent_deliverable_keeps_required_coverage_when_the_model_paraphrases_it(self):
        from superagent import outer_verification as verification

        self.intake.registry = verification
        original = '写一份导读；另请给我做一张可打印的海报；最好有下载链接'
        first, _ = self.router_new(original)
        def propose(role, context, schema):
            result = self.model(role, context, schema)
            if role == 'intake_assessor':
                def item(text):
                    return dict(description=text, source='USER_EXPLICIT', source_refs=[context['event_id']])
                result['goal_draft'].update(deliverables=[item('写一份导读'),item('制作可打印的海报')],
                                            preferences=[item('最好有下载链接')])
                # 即使模型错误地把海报关联给可选项，也不能拿掉一个交付物。
                result['preference_support'] = [dict(preference_quote='最好有下载链接',derived_description='制作可打印的海报')]
            else:
                guide = context['goal_contract']['deliverables'][0]
                result.update(criteria=[],verification_gaps=[dict(requirement_id=guide['requirement_id'],
                              evidence_kind='artifact_inspection', reason='读已交付导读核对')])
            return result
        self.intake.team_model = propose
        self.intake.drain_ticket(first['ticket_id'], limit=2)
        ticket = self.intake.get(first['ticket_id'],'owner')
        poster = ticket['goal_contract']['deliverables'][1]
        self.assertEqual(poster['source'],'MODEL_PROPOSED')
        self.assertNotIn('acceptance_role',poster)
        self.assertEqual(ticket['status'],'VERIFICATION_GAP')
        self.assertIn(poster['requirement_id'],ticket['acceptance_contract']['coverage_assessment']['uncovered'])

    def test_router_standard_form_rejects_missing_consent_and_remote_form_holds_local(self):
        from outer_workbench import create_task

        self.workbench.outer_workbench_entry = True
        self.workbench.outer_goal_team = True
        body = dict(workflow='outer',request_id=str(uuid4()),name='合成单',goal='输出 JSON',acceptance='核对结构')
        with self.assertRaisesRegex(Problem, '允许发送本单资料'):
            create_task(self.workbench, body)
        self.assertEqual(self.intake.drain(), 0)
        event = create_task(self.workbench, dict(body, allow_model_input=True), source='dsh')
        draft = self.intake.get(event['ticket_id'], 'owner')
        self.assertTrue(draft['needs_model_consent'])
        self.assertNotIn('allow_model_input', draft)
        self.assertEqual(self.intake.pending_team_tickets(), [])

    def test_router_withdraws_only_selected_suggestions_and_preserves_previous_authorization(self):
        event, _ = self.router_new()
        ticket_id = event['ticket_id']
        examples = [
            ('constraints', '禁止联网', 'MODEL_PROPOSED'),
            ('nonfunctional_requirements', '不得采集个人信息', 'CONTEXT_DERIVED'),
            ('required_capabilities', '提供交互能力', 'MODEL_PROPOSED'),
        ]

        def propose(role, context, schema):
            result = self.model(role, context, schema)
            if role == 'intake_assessor':
                for field, description, source in examples:
                    result['goal_draft'][field].append(dict(description=description, source=source,
                                                           source_refs=[context['event_id']]))
            return result

        self.intake.team_model = propose
        self.intake.drain_ticket(ticket_id, limit=2)
        approved = self.approve(self.intake.get(ticket_id, 'owner'))
        goal = approved['goal_contract']
        selected = [goal[field][0]['requirement_id'] for field, _, _ in examples]
        protected = goal['deliverables'][0]
        old_execution = self.store.get(ticket_id)['goal_execution']['outer']
        request = dict(source='web', source_event_id='withdraw-router-suggestions', actor_id='owner',
                       text='撤回所选建议', ticket_id=ticket_id, expected_ticket_revision=approved['revision'],
                       allow_model_input=True, change_request=True)
        with self.assertRaisesRegex(Problem, '只可撤回'):
            self.intake.submit(**request, withdraw_requirement_ids=[protected['requirement_id']])
        accepted = self.intake.submit(**request, withdraw_requirement_ids=selected)
        self.assertEqual(accepted['state'], 'PENDING')
        self.assertEqual(self.intake.drain_ticket(ticket_id, limit=2), 1)
        draft = self.intake.get(ticket_id, 'owner')
        self.assertEqual(draft['status'], 'AWAITING_APPROVAL')
        current = draft['goal_contract']
        self.assertEqual(current['deliverables'], [protected])
        self.assertEqual({row['requirement_id'] for row in current['withdrawn_requirements']}, set(selected))
        for field, _, _ in examples:
            self.assertEqual(current[field], [])
        self.assertEqual(self.model.calls[-1][0], 'acceptance_designer')
        self.assertEqual(self.model.calls[-1][1]['goal_contract']['withdrawn_requirements'],
                         current['withdrawn_requirements'])
        self.assertEqual(self.store.get(ticket_id)['goal_execution']['outer'], old_execution)
        self.assertIn('network_tools', draft['approval_terms']['forbidden_operations'])
        self.assertTrue(self.intake.submit(**request, withdraw_requirement_ids=selected)['duplicate'])
        self.intake.submit(source='web',source_event_id='additional-change',actor_id='owner',
                           text='另外交付一份合成说明',ticket_id=ticket_id,expected_ticket_revision=draft['revision'],
                           allow_model_input=True,change_request=True)
        self.assertEqual(self.intake.drain_ticket(ticket_id, limit=2), 2)
        current = self.intake.get(ticket_id, 'owner')['goal_contract']
        for field, _, _ in examples:
            self.assertEqual(current[field], [])
        self.assertIn(protected, current['deliverables'])

    def test_router_selected_remote_material_records_original_source_without_authorization(self):
        first, _ = self.router_new()
        ticket = first['ticket_id']
        unlinked = self.send('选择后作为本机材料', source_thread_id='incoming-remote-thread')
        attached = self.intake.select_ticket(unlinked['event_id'], ticket, actor_id='owner', source='web',
                                             source_event_id='web-select')
        self.assertEqual(attached['action'], 'ATTACH_TO_TICKET')
        entry, = self.intake.get(ticket, 'owner')['subscription']['entries']
        self.assertEqual(entry['source'], 'feishu')
        self.assertFalse(entry['allow_model_input'])
        self.assertFalse(self.model.calls)

    def test_development_delivery_is_visible_only_after_local_registration(self):
        from superagent.development_delivery import register_release

        first, _ = self.router_new()
        ticket = first['ticket_id']
        self.intake.drain_ticket(ticket, limit=2)
        self.approve(self.intake.get(ticket, 'owner'))
        self.assertNotIn('development_delivery', self.intake.get(ticket, 'owner'))
        register_release(self.store, ticket)
        self.assertEqual(self.intake.get(ticket, 'owner')['development_delivery'], {'status':'unauthorized'})
        with self.assertRaises(Problem):
            self.intake.get(ticket, 'wrong-owner')

    def test_router_plain_subscription_preserves_pending_clarification(self):
        first, _ = self.router_new('vague deliverable')
        ticket = first['ticket_id']
        self.assertEqual(self.intake.drain_ticket(ticket), 1)
        draft = self.intake.get(ticket, 'owner')
        self.assertEqual(draft['status'], 'AWAITING_CLARIFICATION')
        result = self.intake.submit(source='web', source_event_id='ordinary-note', actor_id='owner',
                                    text='交付格式：命令行', ticket_id=ticket, expected_ticket_revision=draft['revision'],
                                    allow_model_input=True)
        self.assertEqual(result['state'], 'DONE')
        self.assertEqual(self.intake.get(ticket, 'owner')['question'], draft['question'])
        self.assertEqual(self.intake.pending_team_tickets(), [])
        answer = dict(source='web', source_event_id='answer', actor_id='owner', text='命令行',
                      ticket_id=ticket, expected_ticket_revision=draft['revision'],
                      question_id=draft['question']['question_id'])
        with self.assertRaisesRegex(Problem, '发送给 Codex'):
            self.intake.submit(**answer)
        self.assertEqual(self.intake.pending_team_tickets(), [])
        self.intake.submit(**answer, allow_model_input=True)
        self.assertEqual(self.intake.drain_ticket(ticket, limit=2), 2)
        self.assertEqual(self.intake.get(ticket, 'owner')['status'], 'AWAITING_APPROVAL')
        self.assertIn('交付格式：命令行', json.dumps(self.model.calls[-1][1], ensure_ascii=False))

    def test_router_rereads_contact_material_before_publishing_inflight_question(self):
        first, _ = self.router_new('vague contact 交付联系人清单')
        ticket = first['ticket_id']
        second,_ = self.router_new('另一张工单')
        self.intake.submit(source='web',source_event_id='other-info',actor_id='owner',ticket_id=second['ticket_id'],
                           text='他单联系人陈遥',allow_model_input=True)
        self.intake.submit(source='web',source_event_id='local-only',actor_id='owner',ticket_id=ticket,
                           text='本地未授权联系人王禾')
        saved = []

        def decide(role, context, schema):
            result = self.model(role,context,schema)
            if role == 'intake_assessor':
                if not saved:
                    note = self.intake.submit(source='web',source_event_id='real-contact',actor_id='owner',
                                              ticket_id=ticket,text='联系人：李青',allow_model_input=True)
                    saved.append(note['event_id'])
                else:
                    self.assertEqual(context['authorized_intake_materials'],[{'event_id':saved[0],'text':'联系人：李青'}])
                    result['proposed_question'] = dict(question_text='',options=[],reason='',blocking_scope='')
                    result['critical_ambiguities'] = []
                    result['goal_draft']['required_capabilities'] = [
                        dict(description='联系人：李青',source='USER_EXPLICIT',source_refs=saved)]
            return result

        self.intake.team_model = decide
        self.assertEqual(self.intake.drain_ticket(ticket),1)
        pre_replay = self.intake.get(ticket,'owner')
        self.assertIsNone(pre_replay['question'])
        self.assertFalse(any(entry['handed_to_team'] for entry in pre_replay['subscription']['entries']))
        self.assertEqual(self.intake.drain_ticket(ticket,limit=2),2)
        draft = self.intake.get(ticket,'owner')
        self.assertEqual(draft['status'],'AWAITING_APPROVAL')
        self.assertEqual(draft['questions_asked'],0)
        self.assertEqual([entry['event_id'] for entry in draft['subscription']['entries']
                          if entry['handed_to_team']],saved)
        item, = draft['goal_contract']['required_capabilities']
        self.assertEqual((item['description'],item['source_refs']),('联系人：李青',saved))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM outer_invocations WHERE ticket=? AND state='stale'", (ticket,)).fetchone()[0],1)

    def test_router_rereads_at_most_once_even_if_more_material_keeps_arriving(self):
        submission,_ = self.router_new('vague deliverable')
        ticket = submission['ticket_id']

        def add_new_material(role):
            self.intake.submit(source='web',source_event_id=f'new-material-{len(self.model.calls)}',
                               actor_id='owner',ticket_id=ticket,text='另一条授权资料',allow_model_input=True)

        self.model.on_call = add_new_material
        self.assertEqual(self.intake.drain_ticket(ticket,limit=8),2)
        draft = self.intake.get(ticket,'owner')
        self.assertEqual(draft['status'],'AWAITING_CLARIFICATION')
        self.assertEqual(draft['questions_asked'],1)
        self.assertEqual(self.intake.pending_team_tickets(),[])

    def test_router_detects_missing_second_stage_before_approval(self):
        first, _ = self.router_new(team_review_mode='review_and_decide')
        self.assertEqual(self.intake.drain_ticket(first['ticket_id'], limit=2), 2)
        draft = self.intake.get(first['ticket_id'], 'owner')
        self.assertEqual(draft['status'], 'VERIFICATION_GAP')
        self.assertIn('第 2 步', draft['acceptance_contract']['verification_gaps'][0])
        self.assertEqual(self.model.calls[-1][1]['team_review_mode'], 'review_and_decide')
        self.assertIsNone(draft['task_id'])

    def test_router_self_reference_in_draft_proposal_does_not_change_routing(self):
        event, _ = self.router_new()
        ticket = event['ticket_id']

        def propose(role, context, schema):
            result = self.model(role, context, schema)
            if role == 'intake_assessor':
                result['candidate_ticket_ids'] = [context['ticket_id']]
            return result

        self.intake.team_model = propose
        self.assertEqual(self.intake.drain_ticket(ticket, limit=2), 2)
        self.assertEqual(self.intake.get(ticket, 'owner')['status'], 'AWAITING_APPROVAL')
        receipt = self.intake.event(event['event_id'], 'owner')
        self.assertNotIn('choices', receipt)

    def test_router_recovers_preassigned_draft_from_stored_self_selection(self):
        from superagent.outer_intake import OuterIntake

        event, _ = self.router_new()
        ticket_id, event_id = event['ticket_id'], event['event_id']
        previous, lease, _ = self.intake._claim(ticket_id)
        with self.intake._transaction() as db:
            ticket = self.intake._ticket(db, ticket_id)
            self.intake._finish(db, previous, 'DONE',
                                {'action':'REQUEST_TICKET_SELECTION','choices':[{'id':ticket_id}]}, ticket)
            db.execute("UPDATE outer_invocations SET state='accepted' WHERE id=?", (lease,))
        restarted = OuterIntake(self.workbench, model=self.model, registry=Registry(), team_model=self.model)
        self.assertEqual(restarted.pending_team_tickets(), [ticket_id])
        self.assertEqual(restarted.drain_ticket(ticket_id, limit=2), 2)
        self.assertEqual(restarted.get(ticket_id, 'owner')['status'], 'AWAITING_APPROVAL')
        self.assertEqual(restarted.event(event_id, 'owner')['action'], 'AWAITING_APPROVAL')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM outer_tickets').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM outer_events').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM outer_invocations').fetchone()[0], 3)

    def test_router_late_subscription_preserves_archived_candidate_and_states_it_was_unaffected(self):
        first, _ = self.router_new()
        ticket_id = first['ticket_id']
        self.intake.drain_ticket(ticket_id, limit=2)
        draft = self.intake.get(ticket_id, 'owner')
        self.approve(draft)
        result = self.intake.submit(source='web', source_event_id='sent-note', actor_id='owner', text='先到材料',
                                    ticket_id=ticket_id, allow_model_input=True)
        material = dict(event_id=result['event_id'], source='web', sha256='f'*64)

        def archive(task):
            task.update(status='completed', goal_delivery={'artifact_sha256':'a'*64,'material_refs':[material]})
            task['goal_execution']['assignments'] = [
                {'id':'b'*32, 'role_calls':[{'status':'completed','material_refs':[material]}]}]

        self.store.change(ticket_id, archive)
        self.intake.submit(source='web', source_event_id='late-note', actor_id='owner',
                           text='封存后才到', ticket_id=ticket_id,
                           expected_ticket_revision=draft['revision'], allow_model_input=True)
        result = self.intake.get(ticket_id, 'owner')['subscription']['entries']
        self.assertEqual(result[0]['text'], '封存后才到')
        self.assertTrue(result[0]['candidate_unaffected'])
        self.assertFalse(result[0]['handed_to_team'])
        self.assertTrue(result[1]['handed_to_team'])
        self.assertFalse(result[1]['candidate_unaffected'])
        self.assertEqual(self.store.get(ticket_id)['goal_delivery']['artifact_sha256'], 'a'*64)

    def test_normal_new_workbench_requests_overlap_and_third_waits_for_a_slot(self):
        from uuid import uuid4
        from outer_workbench import create_task

        self.workbench.outer_workbench_entry = True
        arrivals = {}
        release = Event()
        requests = []
        for name in ('甲', '乙', '丙'):
            request = create_task(self.workbench, dict(workflow='outer', request_id=str(uuid4()), name=name,
                                                       goal='交付合成报告'+name, acceptance='运行检查'))
            requests.append(request)
            arrivals[request['event_id']] = Event()

        def gated(role, context, schema):
            if role == 'intake_assessor':
                arrivals[context['event_id']].set()
                release.wait(8)
            return self.model(role, context, schema)

        with patch.object(self.intake, 'model', gated), ThreadPoolExecutor(max_workers=2) as workers:
            calls = [workers.submit(self.intake.drain, 1) for _ in range(2)]
            try:
                self.assertTrue(arrivals[requests[0]['event_id']].wait(5))
                self.assertTrue(arrivals[requests[1]['event_id']].wait(5))
                self.assertEqual(self.intake.drain(limit=1), 0)
                self.assertFalse(arrivals[requests[2]['event_id']].is_set())
                self.assertEqual([self.intake.event(row['event_id'], 'owner')['state'] for row in requests],
                                 ['RUNNING', 'RUNNING', 'PENDING'])
            finally:
                release.set()
            self.assertEqual([call.result(5) for call in calls], [1, 1])
        self.assertEqual(self.intake.drain(), 4)
        settled = [self.intake.event(row['event_id'], 'owner') for row in requests]
        self.assertEqual([row['state'] for row in settled], ['DONE', 'DONE', 'DONE'])
        self.assertEqual(len({row['ticket_id'] for row in settled}), 3)

    def test_same_ticket_and_unbound_thread_do_not_pass_earlier_messages(self):
        for known_ticket in (True, False):
            with self.subTest(known_ticket=known_ticket):
                if known_ticket:
                    draft = self.latest(self.send('初始交付', event='initial'))
                    binding = {'ticket_id':draft['id'], 'expected_ticket_revision':draft['revision']}
                    first = self.send('先补充甲', event='first-ticket', source_thread_id='thread-a', **binding)
                    second = self.send('再补充乙', event='second-ticket', source_thread_id='thread-b', **binding)
                else:
                    first = self.send('先建立工单', event='first-thread', source_thread_id='unbound')
                    second = self.send('再补充资料', event='second-thread', source_thread_id='unbound')
                free = self.send('其他独立工单', event=f'free-{known_ticket}', source_thread_id=f'free-{known_ticket}')
                entered, release = Event(), Event()

                def gated(role, context, schema):
                    if context['event_id'] == first['event_id'] and role == 'intake_assessor':
                        entered.set()
                        release.wait(8)
                    return self.model(role, context, schema)

                with patch.object(self.intake, 'model', gated), ThreadPoolExecutor(max_workers=1) as worker:
                    active = worker.submit(self.intake.drain, 1)
                    try:
                        self.assertTrue(entered.wait(5))
                        self.assertEqual(self.intake.drain(limit=1), 1)
                        self.assertEqual(self.intake.event(second['event_id'], 'owner')['state'], 'PENDING')
                        self.assertTrue(self.intake.event(free['event_id'], 'owner')['ticket_id'])
                    finally:
                        release.set()
                    self.assertEqual(active.result(5), 1)
                self.intake.drain()
                result = self.intake.event(second['event_id'], 'owner')
                self.assertEqual(result['state'], 'REJECTED')
                self.assertIn('工单版本已变化' if known_ticket else '会话已关联工单', result['error'])
                self.assertFalse(any(context['event_id'] == second['event_id'] for _, context in self.model.calls))

    def test_old_decision_remains_in_both_desktop_queues_after_history_grows(self):
        from outer_channels import dispatch
        pending = self.latest(self.send('vague'))
        with self.store.connect() as db:
            for number in range(101):
                current = {**pending,'id':f'wb-{number:032x}','status':'STOPPED',
                           'updated_at':'2999-01-01T00:00:00.000+00:00'}
                db.execute('INSERT INTO outer_tickets VALUES(?,?,?,?,?)',
                           (current['id'],'default','owner',json.dumps(current),current['updated_at']))
        for source in ('web','dsh'):
            with self.subTest(source=source):
                rows = dispatch(self.intake,source,'list',{})
                decisions = [row['id'] for row in rows if row['journey']['station']=='decision']
                self.assertEqual(decisions,[pending['id']])

    def test_outer_list_and_detail_use_current_coordination_phase_and_decision(self):
        from outer_channels import dispatch
        from superagent.goal_engine import decision, pause_or_block

        approved = self.approve(self.latest(self.send('生成合成工作报告')))
        key = approved['id']

        def views():
            brief = dispatch(self.intake, 'web', 'list', {})[0]
            detail = dispatch(self.intake, 'web', 'get', {'ticket_id':key})
            self.assertEqual(brief['journey'], detail['journey'])
            return detail['journey']

        def coordinate(task):
            task.update(status='reviewing', phase='review')
            task['goal_execution']['stage'] = 'coordinate'

        self.store.change(key, coordinate)
        view = views()
        self.assertEqual((view['phase'],view['label']), ('coordinate','正在协调'))

        for action, label in [('REQUEST_APPROVAL','需确认变更'), ('BLOCKED','协调后受阻')]:
            def stop_for_decision(task):
                proposal = dict(action=action, reason='原授权未覆盖备用输入', plan_change='追加一个合成输入',
                                remaining_risk='候选格式仍需核对', internal='调用细节')
                pause_or_block(task, proposal['reason'])
                decision(task, 'coordinated', proposal=proposal)

            self.store.change(key, stop_for_decision)
            view = views()
            self.assertEqual((view['station'],view['label'],view['instruction']),
                             ('decision',label,'原授权未覆盖备用输入'))
            self.assertTrue(view['modify'])
            self.assertFalse(view['approve'])
            self.assertIn('修改要求', view['next'])
            self.assertEqual([row['text'] for row in view['decision_items']], ['追加一个合成输入','候选格式仍需核对'])
        # 通过普通修改入口产生新版契约，仍需先处理旧执行才能授权。
        submitted = dispatch(self.intake, 'web', 'submit', dict(ticket_id=key, revision=approved['revision'],
                            request_id='change-after-coordination', text='change 新增一个合成文件'))
        updated = self.latest(submitted)
        self.assertEqual(updated['status'], 'AWAITING_APPROVAL')
        self.assertGreater(updated['goal_revision'], approved['goal_revision'])
        self.assertFalse(views()['approve'])

    def test_needs_attention_only_offers_edit_with_available_outer_budget(self):
        from outer_channels import card, dispatch
        from superagent.outer_intake import _MAX_TOKENS

        submitted = self.send('生成合成工作报告')
        self.intake.drain(limit=1)
        self.model.fails = 2
        draft = self.latest(submitted)
        self.assertEqual(draft['status'], 'NEEDS_ATTENTION')
        key = draft['id']

        def check(can_modify):
            brief = dispatch(self.intake, 'web', 'list', {})[0]
            detail = dispatch(self.intake, 'web', 'get', {'ticket_id':key})
            self.assertEqual(brief['journey'], detail['journey'])
            self.assertEqual(detail['journey']['modify'], can_modify)
            self.assertEqual('outer_edit' in dict(card(detail)['actions']), can_modify)
            return detail['journey']['next']

        self.assertIn('补充', check(True))
        with self.store.connect() as db:
            db.execute("UPDATE outer_invocations SET result=json_set(result,'$.usage.input_tokens',?) WHERE ticket=?",
                       (_MAX_TOKENS,key))
        self.assertEqual(check(False), '已达到外层模型预算。')

    def test_public_stage_and_repair_status_follow_active_assignments(self):
        from outer_channels import dispatch

        approved = self.approve(self.latest(self.send('生成合成工作报告')))
        key = approved['id']
        expected = [('executing','execute','repairing','执行者正在返修'),
                    ('executing','execute','rechecking','负责人正在复验新版'),
                    ('queued','execute','waiting_review','等待负责人复验'),
                    ('reviewing','review',None,'正在独立核验')]
        for status, phase, repair, label in expected:
            def set_progress(task):
                task.update(status=status, phase=phase)
                state = task['goal_execution']
                state.update(stage=phase, assignments=[dict(id='earlier', state='settled', phase='execute',
                                                          inner_repairs=[dict(status='repairing')])])
                if status == 'executing':
                    state['assignments'].append(dict(id='current', state='dispatched', phase='execute',
                        transport='claimed', inner_repairs=[dict(status=repair, issue='不会进入列表的详细问题')]))
                if status == 'queued':
                    state['team_pending_repair'] = {'repair_record':{'status':repair}}

            self.store.change(key, set_progress)
            brief = dispatch(self.intake, 'web', 'list', {})[0]
            detail = dispatch(self.intake, 'web', 'get', {'ticket_id':key})
            self.assertEqual(brief['journey'], detail['journey'])
            self.assertEqual((brief['execution_phase'],brief['inner_repair_status'],brief['journey']['label']),
                             (phase,repair,label))
            self.assertNotIn('不会进入列表', json.dumps(brief, ensure_ascii=False))

    def test_public_comparison_reuses_saved_role_summaries(self):
        from outer_channels import dispatch
        from outer_workbench import create_task
        from uuid import uuid4

        self.workbench.outer_workbench_entry = True
        self.workbench.outer_goal_team = True
        approved = self.approve(self.latest(create_task(self.workbench, dict(workflow='outer', request_id=str(uuid4()),
            name='比较合成方案', goal='生成合成报告', acceptance='通过检查', team_review_mode='review'),
            legacy_fixture=True)))
        key = approved['id']
        calls = [dict(sequence=i, role=role, writable=writable, status='completed', action='done', summary=summary)
                 for i, role, writable, summary in ((1,'lead',False,'按主题分组'), (2,'advisor',False,'按时间排序'),
                     (3,'lead',True,'按主题分组，组内按时间排序'), (4,'advisor',False,'日期列可读'))]

        def stage(task):
            task['goal_execution']['assignments'] = [
                dict(id='review-draft', state='settled', phase='execute', team_review_mode='review', step=1,
                     role_calls=calls),
                dict(id='other', state='settled', phase='execute', step=1, role_calls=calls[:2]),
                dict(id='continuation', state='settled', phase='execute', team_review_mode='review_and_decide',
                     team_handoff_continuation={'draft_sha256':'bound'}, step=1, role_calls=calls[:1]),
            ]

        self.store.change(key, stage)
        detail = dispatch(self.intake, 'web', 'get', {'ticket_id':key})
        seen = [row for row in detail['execution']['team_activity'] if 'purpose' in row]
        self.assertEqual([(row['purpose'],row['summary'],row['step'],row['assignment']) for row in seen], [
            ('lead_plan','按主题分组',1,'review-draft'), ('advisor_plan','按时间排序',1,'review-draft'),
            ('plan_comparison','按主题分组，组内按时间排序',1,'review-draft'),
        ])
        self.assertTrue(all('purpose' not in row for row in self.store.get(key)['goal_execution']['assignments'][0]['role_calls']))

    def test_clarification_reuses_ticket_and_atomic_approval_is_idempotent(self):
        first = self.send('vague', source_thread_id='root')
        self.assertTrue(self.send('vague', source_thread_id='root')['duplicate'])
        draft = self.latest(first)
        self.assertEqual(draft['status'], 'AWAITING_CLARIFICATION')
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.workbench.goal_engine().store.list(), [])
        question = draft['question']
        with self.assertRaises(Problem):
            self.send('命令行', event='bad', source_thread_id='root', question_id='wrong')
        answer = self.send('命令行', event='answer', source_thread_id='root', question_id=question['question_id'])
        ready = self.latest(answer)
        self.assertEqual(ready['id'], draft['id'])
        self.assertEqual(ready['goal_revision'], 2)
        self.assertEqual(ready['status'], 'AWAITING_APPROVAL')
        self.assertEqual(self.store.list(), [])
        with self.assertRaises(Problem):
            self.approve({**ready, 'revision': ready['revision'] - 1}, 'old')
        with self.assertRaises(Problem):
            self.approve(ready, 'bad-card', source='feishu', source_thread_id='unbound')
        self.intake.bind_thread(ready['id'], 'feishu', 'approval-card', actor_id='owner')
        authorized = self.approve(ready, source='feishu', source_thread_id='approval-card')
        self.assertTrue(self.approve(ready, source='feishu', source_thread_id='approval-card')['duplicate'])
        self.assertEqual(authorized['task_id'], first.get('ticket_id') or ready['id'])
        task = self.store.get(authorized['task_id'])
        self.assertEqual(task['goal_execution']['outer']['acceptance_contract']['revision'], 1)
        self.assertEqual(task['goal_execution']['outer']['mandate']['budget_seconds'], 3600)
        self.assertEqual(len(self.store.list()), 1)
        with self.assertRaises(Problem):
            self.workbench.pause(ready['id'])
        self.assertEqual(self.workbench.goal_engine().prepare(ready['id'])['goal_execution']['assignments'][0]['phase'],
                         'plan')
        for item in self.intake.pending_outbox(source='feishu'):
            self.intake.mark_delivered(item['id'])
        self.assertFalse(self.intake.pending_outbox(source='feishu'))

    def test_normal_workbench_opt_in_is_new_idempotent_and_bound_on_approval(self):
        from outer_workbench import create_task
        from uuid import uuid4

        legacy = self.latest(self.send('已有工单'))
        body = dict(workflow='outer', request_id=str(uuid4()), name='普通入口工单',
                    goal='生成 JSON 报告', acceptance='通过逐项检查')
        with self.assertRaisesRegex(Problem, '请在智能接单入口'):
            create_task(self.workbench, body)
        self.workbench.outer_workbench_entry = True
        first = create_task(self.workbench, body)
        retry = create_task(self.workbench, body)
        self.assertEqual(first['event_id'], retry['event_id'])
        self.assertTrue(retry['duplicate'])
        with self.assertRaisesRegex(Problem, '不能切换执行路线'):
            create_task(self.workbench, {key: value for key, value in body.items() if key != 'workflow'})
        with self.assertRaisesRegex(Problem, '不能提交不同内容'):
            create_task(self.workbench, dict(body, acceptance='另一检查'))
        with self.assertRaisesRegex(Problem, '仅用于新建'):
            create_task(self.workbench, dict(body, ticket_id=legacy['id']))
        draft = self.latest(first)
        self.assertNotEqual(draft['id'], legacy['id'])
        self.assertEqual(draft['workbench_name'], body['name'])
        roles = [context for role, context in self.model.calls if context.get('event_id') == first['event_id']
                 or context.get('ticket_id') == draft['id']]
        self.assertEqual(len(roles), 2)
        for context in roles:
            self.assertEqual(context['untrusted_text']['workbench_request']['acceptance'], body['acceptance'])
        self.approve(draft)
        task = self.store.get(draft['id'])
        self.assertEqual(task['name'], body['name'])
        self.assertEqual(task['goal_execution']['coordinator_profile'], 'scoped-v1')
        self.assertNotIn('coordinator_profile', self.intake.get(legacy['id'], 'owner'))

    def test_explicit_workbench_model_consent_stays_in_the_original_event(self):
        from outer_channels import dispatch
        from outer_workbench import create_task
        from uuid import uuid4

        self.workbench.outer_workbench_entry = True
        body = dict(workflow='outer', request_id=str(uuid4()), name='普通入口工单',
                    goal='生成合成报告', acceptance='检查标题', allow_model_input=True)
        event_id = create_task(self.workbench, body)['event_id']
        draft = self.latest({'event_id': event_id})
        self.assertIs(draft['allow_model_input'], True)
        self.assertIs(next(row for row in self.intake.list('owner') if row['id'] == draft['id'])['allow_model_input'], True)
        with self.workbench.store.connect() as db:
            authorized = db.execute(
                "SELECT json_type(body,'$.workbench_request.allow_model_input') FROM outer_events WHERE id=?",
                (event_id,)).fetchone()[0]
        self.assertEqual(authorized, 'true')
        context = next(context for _, context in self.model.calls if context.get('event_id') == event_id)
        self.assertEqual(context['untrusted_text']['workbench_request'], {
            'event_id': event_id, 'name': body['name'], 'acceptance': body['acceptance'],
        })
        followup = dict(ticket_id=draft['id'], revision=draft['revision'], request_id=str(uuid4()),
                        text='再核对一次标题', allow_model_input=True)
        response = dispatch(self.intake,'web','submit',followup)
        self.assertTrue(dispatch(self.intake,'web','submit',followup)['duplicate'])
        with self.workbench.store.connect() as db:
            authorized = db.execute("SELECT json_type(body,'$.allow_model_input') FROM outer_events WHERE id=?",
                                    (response['event_id'],)).fetchone()[0]
        self.assertEqual(authorized, 'true')

    def test_team_preference_binds_profile_pause_version_and_cross_channel_answer(self):
        from uuid import uuid4
        from outer_workbench import create_task
        from superagent.goal_engine import GoalEngine, digest
        from superagent.goal_team_records import PROFILE

        self.workbench.outer_workbench_entry = True
        self.workbench.outer_goal_team = True
        body = dict(workflow='outer', request_id=str(uuid4()), name='偏好工单', goal='生成报告',
                    acceptance='通过检查并询问样例偏好')
        approved = self.approve(self.latest(create_task(self.workbench, body, source='dsh', legacy_fixture=True)))
        ticket = approved['id']
        self.assertEqual(approved['mandate']['execution_profile'], PROFILE)
        engine = GoalEngine(self.store)
        engine.prepare(ticket)
        sample = '合成样例版本'
        choice = '先回应感受，少给建议。'
        def enter_wait(task):
            state = task['goal_execution']
            state['assignments'].clear()
            state.update(stage='awaiting_preference', pending_preference={
                'id':'question', 'prompt':'要哪个样例？', 'revision':1, 'phase':'execute', 'answer':None,
                'candidate_digest':sample, 'plan_revision':task['version'],
                'mandate_digest':digest(state['mandate']), 'stop_generation':state['stop_generation'],
                'options':[{'id':'b', 'label':'乙', 'text':'回应感受'}]})
            task.update(status='awaiting_preference', phase='execute')
        self.store.change(ticket, enter_wait)
        self.assertEqual(self.intake.get(ticket, 'owner')['execution']['pending_preference']['id'], 'question')
        self.control(approved, 'pause', 'pause-team')
        self.assertIsNone(self.intake.get(ticket, 'owner')['execution']['pending_preference'])
        paused = self.intake.get(ticket, 'owner')
        self.control(paused, 'resume', 'resume-team')
        ready = self.intake.get(ticket, 'owner')
        self.assertEqual(ready['execution']['pending_preference']['revision'], 2)
        answer = {'option_id':'b', 'text':choice}
        with self.assertRaisesRegex(Problem, '问题或候选已更新'):
            self.intake.answer_preference(ticket, ready['revision'], 'question', 1, sample,
                                          answer, 'old', actor_id='owner', source='dsh')
        result = self.intake.answer_preference(ticket, ready['revision'], 'question', 2, sample,
                                               answer, 'answer', actor_id='owner', source='dsh')
        self.assertEqual(result['action'], 'PREFERENCE_RECORDED')
        replay = self.intake.answer_preference(ticket, ready['revision'], 'question', 2, sample,
                                               answer, 'answer', actor_id='owner', source='dsh')
        self.assertTrue(replay['duplicate'])
        with self.assertRaises(Problem):
            self.intake.answer_preference(ticket, ready['revision'], 'question', 2, sample,
                                          answer, 'answer-web', actor_id='owner')
        task = self.store.get(ticket)
        self.assertEqual(task['status'], 'queued')
        self.assertEqual(task['goal_execution']['pending_preference']['answer']['text'], choice)
        self.assertEqual(len([row for row in task['goal_execution']['decisions']
                              if row['kind'] == 'preference_answered']), 1)

    def test_team_budget_extension_reuses_work_and_requires_its_own_approval(self):
        from uuid import uuid4
        from outer_workbench import create_task
        from superagent.goal_engine import GoalEngine
        from superagent.outer_intake import operator_kind

        self.workbench.outer_workbench_entry = True
        self.workbench.outer_goal_team = True
        submit = create_task(self.workbench, dict(workflow='outer', request_id=str(uuid4()),
                                                 name='预算合成单', goal='生成报告', acceptance='通过检查'),
                             legacy_fixture=True)
        approved = self.approve(self.latest(submit))
        ticket = approved['id']
        with self.assertRaisesRegex(Problem, '不可追加资源'):
            self.intake.extend_team_budget(ticket, approved['revision'], 1, 15, 5, 'too-soon', actor_id='owner')
        def exhaust(task):
            task.update(status='budget_exhausted', elapsed_seconds=3600)
            task['goal_execution']['active_candidate'] = {'assignment':'saved-version'}
        self.store.change(ticket, exhaust)
        operator = operator_kind.set('acceptance_test')
        try:
            result = self.intake.extend_team_budget(ticket, approved['revision'], 1, 15, 5, 'budget', actor_id='owner')
            self.assertEqual(result['mandate_revision'], 2)
            repeat = self.intake.extend_team_budget(ticket, approved['revision'], 1, 15, 5, 'budget', actor_id='owner')
            self.assertTrue(repeat['duplicate'])
        finally:
            operator_kind.reset(operator)
        with self.assertRaises(Problem):
            self.intake.extend_team_budget(ticket, approved['revision'], 1, 15, 5, 'stale-budget', actor_id='owner')
        task = GoalEngine(self.store).prepare(ticket)
        state = task['goal_execution']
        self.assertNotIn('operator_kind', approved['mandate'])
        self.assertEqual(state['outer']['mandate']['operator_kind'], 'acceptance_test')
        self.assertEqual(state['outer']['mandate']['team_role_limit'], 35)
        self.assertEqual(state['mandate']['budget_seconds'], 4500)
        self.assertEqual(task['elapsed_seconds'], 3600)
        self.assertEqual(state['active_candidate']['assignment'], 'saved-version')
        self.assertEqual(len(state['assignments']), 1)

    def test_normal_entry_freezes_selected_team_review_mode(self):
        from uuid import uuid4
        from outer_workbench import create_task

        self.workbench.outer_workbench_entry = True
        body = dict(workflow='outer', request_id=str(uuid4()), name='合成审稿', goal='生成报告',
                    acceptance='通过检查', team_review_mode='review', allow_model_input=True)
        with self.assertRaisesRegex(Problem, '未启用团队审稿'):
            create_task(self.workbench, body)
        self.workbench.outer_goal_team = True
        self.intake.team_model = self.model
        with self.assertRaisesRegex(Problem, '有效的方案审稿方式'):
            create_task(self.workbench, dict(body, team_review_mode='invalid'))
        submission = create_task(self.workbench, body)
        self.intake.drain_ticket(submission['ticket_id'], limit=2)
        draft = self.intake.get(submission['ticket_id'], 'owner')
        self.assertEqual(draft['approval_terms']['team_review_mode'], 'review')
        self.assertEqual(self.approve(draft)['mandate']['team_review_mode'], 'review')
        with self.assertRaises(Problem):
            create_task(self.workbench, dict(body, team_review_mode='review_and_decide'))

    def test_team_context_passes_only_relevant_approved_source_text(self):
        from superagent.goal_engine import GoalEngine

        def item(key, text, source='USER_EXPLICIT'):
            return {'requirement_id':key,'description':text,'source':source,'source_refs':[f'event-{key}']}

        fields = {
            'deliverables':[item('first','第一步的文件'),item('second','保留原文件的标题')],
            'required_capabilities':[item('assumed','模型自己补的要求','MODEL_PROPOSED')],
            'nonfunctional_requirements':[],
            'constraints':[item('offline','只在本地执行')],
            'out_of_scope':[],
        }
        task = {'workflow':'goal','step':1,'plan':{'steps':[{'acceptance':['a']},{'acceptance':['b']}]},
                'goal_execution':{'outer':{'goal_contract':fields,'acceptance_contract':{'criteria':[
                    {'criterion_id':'a','requirement_ids':['first']},
                    {'criterion_id':'b','requirement_ids':['second','assumed']},
                ]}}}}
        result = GoalEngine._team_original_requirements(task)
        self.assertEqual([row['text'] for row in result], ['保留原文件的标题','只在本地执行'])
        self.assertEqual(result[0]['source_refs'],['event-second'])

    def test_later_choice_binds_stage_and_requires_current_option(self):
        from uuid import uuid4
        from outer_workbench import create_task
        from superagent.goal_engine import GoalEngine, digest

        self.workbench.outer_workbench_entry = True
        self.workbench.outer_goal_team = True
        draft = self.latest(create_task(self.workbench, dict(
            workflow='outer', request_id=str(uuid4()), name='合成后续选择', goal='生成报告', acceptance='通过检查',
        ), legacy_fixture=True))
        ticket = self.approve(draft)['id']
        GoalEngine(self.store).prepare(ticket)
        sha = 'c' * 64

        def wait(task):
            state = task['goal_execution']
            state['assignments'].clear()
            question = {'id':'second-choice','revision':1,'step':1,'phase':'execute','requires_option':True,
                        'assignment':'draft-assignment','draft_sha256':sha,'candidate_digest':'question-sha',
                        'stop_generation':state['stop_generation'],'plan_revision':task['version'],
                        'mandate_digest':digest(state['mandate']),'prompt':'选择方向',
                        'options':[{'id':'a','text':'沿用现稿','label':'甲'}, {'id':'b','text':'简洁','label':'乙'}]}
            scope = {'variants':[{'option_id':'a','mode':'keep'}, {'option_id':'b','mode':'edit'}]}
            state.update(stage='awaiting_preference', pending_preference=question,
                         team_pending_handoff={'source_assignment':'draft-assignment','base_sha256':sha,
                                               'handoff':{'instruction':{'handoff_scope':scope}}})
            task.update(status='awaiting_preference',step=1)

        self.store.change(ticket,wait)
        original = self.intake.get(ticket,'owner')
        public = original['execution']['pending_preference']
        self.assertEqual(public['step'],1)
        self.assertEqual([option['mode'] for option in public['options']],['keep','edit'])
        args = (ticket,original['revision'],'second-choice',1,'question-sha')
        with self.assertRaisesRegex(Problem, '先选择后续方向'):
            self.intake.answer_preference(*args,{'text':'不选'},'no-option',actor_id='owner')
        self.store.change(ticket,lambda task: task.update(step=2))
        with self.assertRaisesRegex(Problem, '问题或候选已更新'):
            self.intake.answer_preference(*args,{'option_id':'a'},'old-step',actor_id='owner')
        self.store.change(ticket,lambda task: task.update(step=1))
        with self.assertRaisesRegex(Problem,'请选择可修改的方向') as conflict:
            self.intake.answer_preference(*args,{'option_id':'a','text':'  想改一句  '},'choose-a',actor_id='owner')
        self.assertEqual(conflict.exception.status,409)
        unchanged = self.intake.get(ticket,'owner')
        self.assertEqual(unchanged['revision'],original['revision'])
        self.assertEqual(unchanged['execution']['pending_preference'],public)
        with self.store.connect() as db:
            self.assertIsNone(db.execute("SELECT id FROM outer_events WHERE source_event_id='choose-a'").fetchone())
        answer = self.intake.answer_preference(*args,{'option_id':'a','text':'  '},'choose-a',actor_id='owner',
                                               allow_model_input=True)
        retry = self.intake.answer_preference(*args,{'option_id':'a'},'choose-a',actor_id='owner')
        self.assertEqual(answer['action'],'PREFERENCE_RECORDED')
        self.assertTrue(retry['duplicate'])
        self.assertEqual(self.store.get(ticket)['step'],1)

    def test_direct_coordination_enqueue_uses_same_team_candidate(self):
        from uuid import uuid4
        from outer_workbench import create_task
        from superagent.goal_engine import GoalEngine
        from superagent.goal_team_records import PROFILE

        self.workbench.outer_workbench_entry = True
        self.workbench.outer_goal_team = True
        body = dict(workflow='outer', request_id=str(uuid4()), name='协调工单', goal='生成报告',
                    acceptance='通过检查')
        ticket = self.approve(self.latest(create_task(self.workbench, body, legacy_fixture=True)))['id']
        engine = GoalEngine(self.store)
        engine.prepare(ticket)
        candidate = {'assignment':'a'*32,'artifact_sha256':'b'*64,'files':{'report.json':'c'*64}}
        def enqueue(task):
            state = task['goal_execution']
            state.update(active_candidate=candidate, coordination_review_items=['affected'])
            task.update(step=1, plan={'steps':[{'acceptance':['prior']},{'acceptance':['current']}]})
            engine._enqueue(task,'repair',60,coordination_proposal='same-proposal')
            engine._enqueue(task,'review',60,coordination_proposal='same-proposal')
        state = self.store.change(ticket,enqueue)['goal_execution']
        repair,review = state['assignments'][-2:]
        self.assertEqual(repair['candidate_base'], candidate)
        self.assertEqual(repair['execution_profile'], PROFILE)
        self.assertEqual(repair['role_slots'], PROFILE['max_roles_per_assignment'])
        self.assertEqual(review['candidate_base'], candidate)
        self.assertEqual(review['additional_review_items'], ['prior','affected'])

    def test_team_repair_defers_without_promoting_and_resumes_from_same_file(self):
        from uuid import uuid4
        from outer_workbench import create_task
        from superagent.goal_engine import GoalEngine, current

        self.workbench.outer_workbench_entry = True
        self.workbench.outer_goal_team = True
        draft = self.latest(create_task(self.workbench, dict(
            workflow='outer', request_id=str(uuid4()), name='合成续办预算',
            goal='生成报告', acceptance='按合同核对结果',
        ), legacy_fixture=True))
        with self.intake._transaction() as db:
            draft = self.intake._ticket(db,draft['id'],'owner','default')
            draft['approval_terms']['team_role_limit'] = 4
            self.intake._save(db,draft)
        approved = self.approve(draft)
        ticket = approved['id']
        engine = GoalEngine(self.store)
        engine.prepare(ticket)
        finding = {'action':'repair','summary':'报告标题错误','retain':[],'verify':['检查标题'],'options':[],
                   'repair_scope':{'retained':[],'changes':[],'removed_paths':[]}}
        checkpoint = {'digest':'b'*64,'files':{'report.json':'a'*64},'artifacts':['report.json']}

        def before_deferral(task):
            state = task['goal_execution']
            state['assignments'].clear()
            state['stage'] = task['phase'] = 'execute'
            criteria = state['outer']['acceptance_contract']['criteria']
            task['plan'] = {'steps':[{'acceptance':[row['criterion_id'] for row in criteria]}]}
            engine._enqueue(task,'execute',240)
            assignment = current(task)
            scope = {'before_files':checkpoint['files'],'before_sha256':checkpoint['digest'],
                     'acceptance_sha256':assignment['outer_binding']['contract_digest']}
            assignment.update(state='dispatched',run=42,role_calls=[
                {'session':f'session-{i}','turn':f'turn-{i}','status':'completed',
                 **(finding if i == 2 else {'action':'done'})}
                for i in range(3)
            ],inner_repairs=[{'status':'waiting','before_sha256':checkpoint['digest'],'scope':scope}])

        task = self.store.change(ticket,before_deferral)
        source = current(task)
        report = dict(id=source['id'],run=42,version=source['version'],outer_binding=source['outer_binding'],
                      workspace_id=source['id'], result=finding, checkpoint=checkpoint, deferred_repair=True)
        first = engine.settle(ticket,source['id'],42,'done',report)
        self.assertIsNone(first['goal_execution'].get('active_candidate'))
        self.assertEqual(first['goal_execution']['team_pending_repair']['base_sha256'],checkpoint['digest'])
        exhausted = engine.prepare(ticket)
        self.assertEqual(exhausted['status'],'budget_exhausted')
        self.assertIn('1 个角色轮次', exhausted['error'])
        self.assertIsNone(current(exhausted))
        current_draft = self.intake.get(ticket,'owner')
        extra = self.intake.extend_team_budget(ticket,current_draft['revision'],1,0,5,'extra-roles',actor_id='owner')
        self.assertEqual(extra['mandate_revision'],2)
        resumed = engine.prepare(ticket)
        new = current(resumed)
        self.assertEqual(new['phase'],'execute')
        self.assertEqual(new['candidate_base']['assignment'],source['id'])
        self.assertEqual(new['repair_continuation']['finding'],finding)
        self.assertEqual(new['candidate_base']['artifact_sha256'],checkpoint['digest'])
        self.assertEqual(new['outer_binding']['mandate_revision'],2)
        engine.settle(ticket,source['id'],42,'done',report)
        self.assertEqual(current(engine.prepare(ticket))['id'],new['id'])

    def test_legacy_engine_cannot_change_v2_control_state(self):
        ready = self.latest(self.send('生成 JSON 报告'))
        authorized = self.approve(ready)
        ticket = authorized['id']
        engine = self.workbench.goal_engine()

        with self.assertRaisesRegex(Problem, '当前目标契约'):
            engine.stop(ticket)
        paused = self.control(authorized, 'pause', 'pause-v2')
        state = self.store.get(ticket)
        generation = state['goal_execution']['stop_generation']
        with self.assertRaisesRegex(Problem, '当前目标契约'):
            engine.resume(ticket, 'legacy-resume', generation)
        self.assertEqual(self.store.get(ticket), state)

        authorized = self.control(paused, 'resume', 'resume-v2')
        self.assertEqual(self.store.get(ticket)['status'], 'queued')
        self.control(authorized, 'stop', 'stop-v2')
        stopped = self.store.get(ticket)
        generation = stopped['goal_execution']['stop_generation']
        with self.assertRaisesRegex(Problem, '当前目标契约'):
            engine.resume(ticket, 'legacy-restart', generation)
        with self.assertRaisesRegex(Problem, '当前目标契约'):
            engine.recheck(ticket, stopped['version'], 'legacy-checkpoint')
        self.assertEqual(self.store.get(ticket), stopped)
        self.assertEqual(self.intake.get(ticket, 'owner')['status'], 'STOPPED')

    def test_duplicate_scope_and_selection(self):
        a = self.latest(self.send('one', event='one'))
        b = self.latest(self.send('two', event='two'))
        pending = self.send('select', event='pick')
        choice = self.latest(pending)
        self.assertEqual({a['id'], b['id']}, {row['id'] for row in choice['choices']})
        with self.assertRaises(Problem):
            self.intake.select_ticket(choice['event_id'], a['id'], actor_id='stranger', source='web',
                                      source_event_id='pick-a')
        cmd = dict(actor_id='owner', source='web', source_event_id='pick-a')
        selected = self.intake.select_ticket(choice['event_id'], a['id'], **cmd)
        self.assertTrue(self.intake.select_ticket(choice['event_id'], a['id'], **cmd)['duplicate'])
        newer = self.latest(selected)
        self.assertEqual(a['id'], newer['id'])
        self.assertEqual(len(self.intake.list('owner')), 2)
        duplicate_external_id = self.send('other tenant', event='one', tenant='another')
        self.assertNotEqual(duplicate_external_id['event_id'], self.model.calls[0][1]['event_id'])
        with self.assertRaises(Problem):
            self.send('different body', event='one')

    def test_gap_and_invalid_proposal_do_not_authorize(self):
        gap = self.latest(self.send('gap', event='gap'))
        self.assertEqual(gap['status'], 'VERIFICATION_GAP')
        with self.assertRaises(Problem):
            self.approve(gap)
        invalid = self.latest(self.send('invalid', event='invalid'))
        self.assertEqual(invalid['state'], 'NEEDS_ATTENTION')
        self.assertEqual(self.store.list(), [])

    def test_history_follows_late_binding_and_restricts_detail_to_selected_ticket(self):
        from outer_channels import dispatch
        self.model.fails = 1
        first = self.latest(self.send('one', event='one'))['id']
        second = self.latest(self.send('two', event='two'))['id']
        history = self.intake.calls(first, 'owner')
        self.assertCountEqual([r['state'] for r in history], ['failed','accepted','accepted'])
        failed = next(r for r in history if r['state'] == 'failed')
        self.assertNotIn('input', failed)
        one = self.intake.calls(first, 'owner', call_id=failed['id'])
        self.assertEqual(one['error'], 'timeout')
        self.assertEqual(one['input']['untrusted_text']['current_message'], 'one')
        self.assertIsNone(one['usage'])
        self.assertIsNone(one['output'])
        self.assertGreaterEqual(one['elapsed_ms'], 0)
        for ticket, actor, tenant in [(second,'owner','default'),(first,'stranger','default'),(first,'owner','other')]:
            with self.assertRaises(Problem) as denied:
                self.intake.calls(ticket, actor, tenant=tenant, call_id=failed['id'])
            self.assertEqual(denied.exception.status, 404)
        with self.assertRaises(Problem):
            dispatch(self.intake, 'feishu','call',dict(ticket_id=first, call_id=failed['id']))
        pending = self.latest(self.send('select', event='pick'))
        selected = self.intake.select_ticket(pending['event_id'],second,actor_id='owner',source='web',source_event_id='pick-two')
        self.latest(selected)
        with self.store.connect() as db:
            picker = db.execute('SELECT id FROM outer_invocations WHERE event=?',(pending['event_id'],)).fetchone()[0]
        self.assertIn(picker, [r['id'] for r in self.intake.calls(second,'owner')])
        self.assertNotIn(picker, [r['id'] for r in self.intake.calls(first,'owner')])

    def test_archived_coordinator_reads_original_input_and_rejected_proposal(self):
        ticket = self.latest(self.send('target'))['id']
        context = {'available_actions':['REPAIR']}
        evidence = dict(started_ns=1_700_000_000_000_000_000,elapsed_ms=42,usage={'output_tokens':7},
                        failure_code='proposal_rejected')
        report = dict(evidence=evidence,error='证据不匹配',model_audit={'final_message':'{"action":"REPAIR"}'})
        assignment = dict(id='original',phase='coordinate',state='settled',coordination_context=context,report=report)
        with self.store.connect() as db:
            self.intake._record_revision(db,ticket,'execution_archive',1,{'goal_execution':{'assignments':[assignment]}})
        row = self.intake.calls(ticket,'owner',call_id='original')
        self.assertEqual((row['input'],row['state'],row['elapsed_ms'],row['usage']), (context,'rejected',42,{'output_tokens':7}))
        self.assertEqual(row['final_message'], report['model_audit']['final_message'])
        self.assertIn('证据不匹配',row['error'])

    def test_pause_during_acceptance_discards_stale_model_response_and_resumes(self):
        event = self.send('simple target')
        self.intake.drain(limit=1)
        row = self.intake.event(event['event_id'], 'owner')
        current = self.intake.get(row['ticket_id'], 'owner')

        def pause(role):
            self.assertEqual(role, 'acceptance_designer')
            self.control(current, 'pause', 'pause')

        self.model.on_call = pause
        self.intake.drain()
        paused = self.intake.get(current['id'], 'owner')
        self.assertEqual(paused['status'], 'PAUSED')
        self.assertEqual(paused['acceptance_revision'], 0)
        self.model.on_call = None
        self.control(paused, 'resume', 'resume')
        self.assertEqual(self.latest(event)['status'], 'AWAITING_APPROVAL')

    def test_restart_recovers_expired_claim_and_retry_is_finite(self):
        event = self.send('recover')
        _, lease, _ = self.intake._claim()
        with self.store.connect() as db:
            db.execute('UPDATE outer_events SET lease_until=0 WHERE lease=?', (lease,))
        restored = Workbench(self.store, goal_enabled=True, outer_enabled=True,
                             outer_model=self.model, outer_registry=Registry()).outer()
        restored.drain()
        self.assertEqual(restored.event(event['event_id'], 'owner')['state'], 'DONE')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM outer_invocations WHERE id=?', (lease,)).fetchone()[0],
                             'lease_expired')
        failure = self.send('always fails', event='failure')
        self.model.fails = 5
        self.assertEqual(restored.drain(), 2)
        self.assertEqual(restored.event(failure['event_id'], 'owner')['state'], 'NEEDS_ATTENTION')

    def test_expired_response_cannot_commit_even_before_another_claim(self):
        from superagent.outer_model import INTAKE_SCHEMA, role_invoke

        event = self.send('旧回执')
        event_id = event['event_id']
        _, lease, snapshot = self.intake._claim()
        response = role_invoke(self.model, 'intake_assessor', snapshot, INTAKE_SCHEMA)
        with self.store.connect() as db:
            db.execute('UPDATE outer_events SET lease_until=0 WHERE lease=?', (lease,))
        self.intake._settle(event_id, lease, snapshot, response)
        self.assertEqual(self.intake.event(event_id, 'owner')['state'], 'PENDING')
        self.assertEqual(self.intake.list('owner'), [])
        self.assertEqual(self.intake.drain(), 2)
        fresh = self.intake.event(event_id, 'owner')
        self.assertEqual(fresh['state'], 'DONE')
        self.intake._settle(event_id, lease, snapshot, response)
        self.assertEqual(self.intake.event(event_id, 'owner'), fresh)
        self.assertEqual(len(self.intake.list('owner')), 1)
        with self.store.connect() as db:
            failed = db.execute('SELECT state,result FROM outer_invocations WHERE id=?', (lease,)).fetchone()
        self.assertEqual(failed['state'], 'lease_expired')
        self.assertEqual(json.loads(failed['result'])['settlement_reason'], 'lease_expired')
        self.assertEqual(json.loads(failed['result'])['result'], response['result'])

    def test_pause_replay_returns_original_snapshot_after_resume_once(self):
        from outer_channels import dispatch

        ticket = self.approve(self.latest(self.send('交付合成账单')))
        body = {'ticket_id':ticket['id'], 'revision':ticket['revision'], 'request_id':'pause-key'}
        paused = dispatch(self.intake, 'web', 'pause', body)
        self.assertEqual(paused['status'], 'PAUSED')
        self.assertEqual(dispatch(self.intake, 'web', 'pause', body), {**paused, 'duplicate':True})
        resumed = self.control(paused, 'resume', 'resume-key')
        self.assertEqual(resumed['status'], 'AUTHORIZED')
        with self.assertRaises(Problem) as stale:
            dispatch(self.intake, 'web', 'pause', {**body, 'request_id':'stale-key'})
        self.assertEqual(stale.exception.status, 409)
        with self.assertRaises(Problem) as collision:
            dispatch(self.intake, 'web', 'pause', {**body, 'revision':resumed['revision']})
        self.assertEqual(collision.exception.status, 409)

        restored = Workbench(self.store, goal_enabled=True, outer_enabled=True,
                             outer_model=self.model, outer_registry=Registry()).outer()
        self.assertEqual(dispatch(restored, 'web', 'pause', body), {**paused, 'duplicate':True})
        current = restored.get(ticket['id'], 'owner')
        self.assertEqual((current['status'], current['revision']), ('AUTHORIZED', resumed['revision']))
        decisions = self.store.get(ticket['id'])['goal_execution']['decisions']
        self.assertEqual(sum(row['kind'] == 'outer_pause' for row in decisions), 1)
        deliveries = [d for d in restored.pending_outbox() if d['action'] == 'PAUSE']
        self.assertCountEqual([d['source'] for d in deliveries], ['web', 'feishu'])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM outer_events WHERE source_event_id='pause-key'").fetchone()[0], 1)
            self.assertIsNone(db.execute("SELECT id FROM outer_events WHERE source_event_id='stale-key'").fetchone())

    def test_changed_scope_requires_paused_inner_execution_and_keeps_old_approval(self):
        first = self.latest(self.send('simple', source_thread_id='root'))
        active = self.approve(first)
        old_workspace = self.store.workspace(active['id'])
        (old_workspace / 'old.txt').write_text('旧版交付')
        self.store.change(active['id'], lambda task: task.update(goal_delivery={'legacy': True}, calls=8))
        event = self.send('change target', event='change', source_thread_id='root')
        draft = self.latest(event)
        self.assertEqual(draft['id'], active['id'])
        self.assertEqual(draft['goal_revision'], 2)
        with self.assertRaises(Problem):
            self.approve(draft, 'approval2')
        paused = self.control(draft, 'pause', 'pause')
        worked = self.approve(paused, 'approval2')
        self.assertEqual(worked['mandate_revision'], 2)
        current = self.store.get(worked['task_id'])
        self.assertEqual(self.store.workspace(worked['task_id']), Path(worked['approval_terms']['workspace']))
        self.assertNotEqual(self.store.workspace(worked['task_id']), old_workspace)
        self.assertFalse((self.store.workspace(worked['task_id']) / 'old.txt').exists())
        self.assertEqual((old_workspace / 'old.txt').read_text(), '旧版交付')
        self.assertNotIn('goal_delivery', current)
        self.assertEqual(current['calls'], 0)
        self.assertEqual(self.approve(first)['mandate_revision'], 1)
        with self.store.connect() as db:
            payload = db.execute("SELECT body FROM outer_revisions WHERE kind='execution_archive'").fetchone()[0]
        self.assertEqual(json.loads(payload)['goal_execution']['outer']['goal_revision'], 1)
        self.assertEqual(len(self.store.list()), 1)


    def test_provenance_keeps_anchored_requirements_and_requires_explicit_replacement(self):
        initial = self.latest(self.send('付清全部账单', source_thread_id='root'))
        kept = initial['goal_contract']['deliverables'][0]
        self.assertEqual(kept['source'], 'USER_EXPLICIT')
        updated = self.latest(self.send('invent 只生成清单', event='append', source_thread_id='root'))
        self.assertIn(kept, updated['goal_contract']['deliverables'])
        self.assertEqual(updated['goal_contract']['constraints'][0]['source'], 'MODEL_PROPOSED')
        explicit = self.send('正式更改为生成年度账单', event='replace', ticket_id=initial['id'],
                             expected_ticket_revision=updated['revision'],
                             replace_requirement_ids=[kept['requirement_id']])
        revised = self.latest(explicit)
        snapshot = next(s for role,s in self.model.calls if role == 'intake_assessor'
                        and s['event_id'] == explicit['event_id'])
        self.assertEqual(snapshot['replace_requirements'], [dict(field='deliverables',
                         requirement_id=kept['requirement_id'], description=kept['description'])])
        self.assertEqual(snapshot['available_actions'], ['PROPOSE_CHANGE', 'ESCALATE'])
        self.assertNotIn(kept, revised['goal_contract']['deliverables'])
        self.assertEqual(revised['goal_contract']['changes']['deliverables']['removed'], [kept['description']])

    def test_selected_replacement_cannot_be_filed_as_note_or_repeat_old_requirement(self):
        draft = self.latest(self.send('交付原始账单'))
        self.approve(draft)
        prior = self.intake.get(draft['id'], 'owner')
        kept = prior['goal_contract']['deliverables'][0]
        for action in ('ATTACH_TO_TICKET','PROPOSE_CHANGE'):
            event = self.send('改为年度账单', event=action, ticket_id=prior['id'],
                              expected_ticket_revision=prior['revision'],replace_requirement_ids=[kept['requirement_id']])
            def unchanged(role,context,schema):
                result = self.model(role,context,schema)
                result['suggested_action'] = action
                result['goal_draft']['deliverables'][0]['description'] = kept['description']
                return result
            with patch.object(self.intake,'model',unchanged):
                self.intake.drain()
            self.assertEqual(self.intake.event(event['event_id'],'owner')['state'],'REJECTED')
            self.assertEqual(self.intake.get(draft['id'],'owner'),prior)
        revised = self.latest(self.send('改为年度账单',event='valid-replacement',ticket_id=prior['id'],
                              expected_ticket_revision=prior['revision'],replace_requirement_ids=[kept['requirement_id']]))
        self.assertEqual(revised['status'],'AWAITING_APPROVAL')
        self.assertEqual(revised['goal_contract']['changes']['deliverables']['removed'],[kept['description']])
        self.assertEqual(revised['goal_contract']['changes']['deliverables']['added'],['改为年度账单'])
        self.assertEqual(revised['execution'],prior['execution'])

    def test_coordination_trace_stops_at_authorization_boundary(self):
        from superagent.outer_history import _coordination_trace
        a = dict(id='coord',run=4,outer_binding={'mandate_revision':1})
        repair = dict(a,id='fix',phase='repair',run=5,state='settled')
        review = dict(a,id='verify',phase='review',run=6,state='settled')
        stale = dict(a,id='other',phase='repair',run=7,state='settled',outer_binding={'mandate_revision':2})
        ledger = [dict(kind='coordinated',assignment='coord',proposal={'action':'REPAIR'}),
                  dict(kind='dispatch_prepared',assignment='fix'),dict(kind='dispatch_prepared',assignment='verify'),
                  dict(kind='evaluated',assignment='verify',passed=True),dict(kind='delivered',assignment='verify',digest='hash')]
        def trace(records):
            return _coordination_trace(a,records,[a,repair,review,stale])
        self.assertIsNone(trace(ledger[1:])['decision'])
        self.assertEqual([x['run'] for x in trace(ledger)['subsequent_runs']],[5,6])
        self.assertEqual(trace(ledger)['subsequent_runs'][-1]['delivery_digest'],'hash')
        # 新路线在同一事务先登记派工再登记采纳决定。
        atomic = [ledger[1],dict(ledger[0],dispatch='fix'),*ledger[2:]]
        self.assertEqual([x['run'] for x in trace(atomic)['subsequent_runs']],[5,6])
        self.assertEqual(trace(atomic)['subsequent_runs'][-1]['delivery_digest'],'hash')
        for stop in [dict(kind='stop_requested'),dict(kind='authorized'),dict(kind='dispatch_prepared',assignment='other')]:
            self.assertFalse(trace([ledger[0],stop,*ledger[1:]])['subsequent_runs'])

    def test_selection_preserves_pending_question_for_explicit_answer(self):
        initial = self.latest(self.send('vague', source_thread_id='root'))
        question = initial['question']
        event = self.latest(self.send('select', event='select'))
        count = len(self.model.calls)
        picked = self.intake.select_ticket(event['event_id'], initial['id'], actor_id='owner',
                                          source='web', source_event_id='select-command')
        self.assertEqual(picked['question'], question)
        self.assertEqual(picked['ticket_revision'], initial['revision'])
        self.intake.drain()
        self.assertEqual(len(self.model.calls), count)
        self.assertEqual(self.intake.get(initial['id'], 'owner')['question'], question)

    def test_failed_proposal_does_not_restore_rolled_back_python_ticket(self):
        ready = self.latest(self.send('ready', source_thread_id='root'))
        new = self.send('more context', event='extra', source_thread_id='root')
        original_save = self.intake._save

        def fail_after_update(db, ticket):
            original_save(db, ticket)
            raise ValueError('合成失败，api-key: my-fixture-key')

        with patch.object(self.intake, '_save', side_effect=fail_after_update):
            self.intake.drain()
        self.assertEqual(self.intake.get(ready['id'], 'owner'), ready)
        self.assertEqual(self.intake.event(new['event_id'], 'owner')['state'], 'REJECTED')
        with self.store.connect() as db:
            audit = json.loads(db.execute('SELECT result FROM outer_invocations WHERE event=?',
                                         (new['event_id'],)).fetchone()[0])
        self.assertNotIn('my-fixture-key', audit['validation_error'])

    def test_feishu_controls_reject_unbound_or_outdated_cards(self):
        ready = self.latest(self.send('ready'))
        self.intake.bind_thread(ready['id'], 'feishu', 'old-card', actor_id='owner')
        paused = self.control(ready, 'pause', 'web-pause')
        with self.assertRaises(Problem):
            self.intake.control(paused['id'], 'resume', paused['revision'], 'no-card',
                                actor_id='owner', source='feishu')
        with self.assertRaises(Problem):
            self.intake.control(paused['id'], 'resume', paused['revision'], 'old-click',
                                actor_id='owner', source='feishu', source_thread_id='old-card')
        with self.assertRaises(Problem):
            self.intake.bind_thread(paused['id'], 'feishu', 'late-card', actor_id='owner',
                                    expected_ticket_revision=ready['revision'])
        self.intake.bind_thread(paused['id'], 'feishu', 'fresh-card', actor_id='owner',
                                expected_ticket_revision=paused['revision'])
        resumed = self.intake.control(paused['id'], 'resume', paused['revision'], 'right-click',
                                     actor_id='owner', source='feishu', source_thread_id='fresh-card')
        self.assertEqual(resumed['status'], 'AWAITING_APPROVAL')


    def test_signed_artifact_is_versioned_idempotent_and_race_safe(self):
        draft=self.latest(self.send('交付账单'))
        ticket=self.approve(draft,require_signoff=True)
        tid=ticket['id']
        self.assertTrue(ticket['acceptance_contract']['require_signoff'])
        def offer(n,data):
            sha=hashlib.sha256(data).hexdigest()
            key=f'{n:032x}'
            directory=self.store.root/'goal-attempts'/tid/key/'verified'
            directory.mkdir(parents=True)
            file=directory/'bill.txt'
            file.write_bytes(data)
            def stage(task):
                state=task['goal_execution']
                task.update(status='awaiting_artifact_review',plan={'steps':[{}]},step=0)
                state.update(stage='awaiting_artifact_review',checkpoint={'digest':sha})
                state.setdefault('artifact_reviews',[]).append(dict(sequence=n,state='pending',level='goal',
                    assignment=key,artifact_sha256=sha,files={'bill.txt':sha},goal_revision=1,
                    acceptance_revision=1,mandate_revision=1,plan_revision=task['version']))
            self.store.change(tid,stage)
            return key,sha,file
        one,first,_=offer(1,b'first draft')
        rev=ticket['revision']
        self.intake.bind_thread(tid,'feishu','v1-card',actor_id='owner',expected_ticket_revision=rev)
        def decide(op,revision,key,sha,reason,event,**kwargs):
            return self.intake.decide_artifact(tid,op,revision,key,sha,reason,event,actor_id='owner',**kwargs)
        args=('return',rev,one,first,'需要列明金额','return-one')
        decide(*args,source='feishu',source_thread_id='v1-card')
        self.assertTrue(decide(*args,source='feishu',source_thread_id='v1-card')['duplicate'])
        two,second,file=offer(2,b'revised draft')
        rev=self.intake.get(tid,'owner')['revision']
        with self.assertRaises(Problem):
            decide('accept',rev,two,second,'','old-card',source='feishu',source_thread_id='v1-card')
        with self.assertRaises(Problem):
            decide('accept',rev,one,first,'','stale')
        file.write_bytes(b'altered')
        with self.assertRaises(Problem):
            decide('accept',rev,two,second,'','tamper')
        file.write_bytes(b'revised draft')
        def sign(key):
            try: return decide('accept',rev,two,second,'',key)['action']
            except Problem as exc: return exc.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(sign,['web-a','web-b'])),['ARTIFACT_ACCEPTED',409])
        closed=self.store.get(tid)
        self.assertEqual(closed['goal_delivery']['assignment'],two)
        self.assertEqual([r['state'] for r in closed['goal_execution']['artifact_reviews']],['returned','accepted'])
        self.assertEqual(self.workbench.artifact_bytes(tid,'bill.txt',candidate=one)[1],b'first draft')
        self.assertEqual(self.workbench.artifact_bytes(tid,'bill.txt')[1],b'revised draft')

    def test_pending_bound_feedback_prevents_approval_of_old_scope(self):
        ready = self.latest(self.send('release', source_thread_id='root'))
        pending = self.send('补充一个必需交付', event='supplement', source_thread_id='root')
        with self.assertRaisesRegex(Problem, '待处理的新消息'):
            self.approve(ready)
        updated = self.latest(pending)
        with self.assertRaisesRegex(Problem, '版本已变化'):
            self.approve(ready)
        self.assertEqual(self.approve(updated)['status'], 'AUTHORIZED')


if __name__ == '__main__':
    unittest.main()
