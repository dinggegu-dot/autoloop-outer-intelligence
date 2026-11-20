"""隔离 SQLite 与结构化模型协议替身。"""
import json
import hashlib
from concurrent.futures import ThreadPoolExecutor
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

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
                                    assumptions=[], out_of_scope=[]),
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
