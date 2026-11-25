from concurrent.futures import ThreadPoolExecutor
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench import Problem, Store, Workbench, omit_call_payloads
from superagent import goal_blind_review as blind, outer_verification
from superagent.goal_blind_ledger import BlindReviewLedger, _runtime_measures
from superagent.goal_team_blind import make_blind_draft
from superagent.outer_intake import operator_kind
from superagent.test_goal_blind_review import inputs
from superagent.test_outer_intake import Model


PROJECTION_PATH = 'paired.json'
CASE_IDS = [f'H{number:02}' for number in range(1, 9)]


class BlindModel(Model):
    """合成接单模型使用真实的独立验收器与批准链。"""
    def __call__(self, role, context, schema):
        result = super().__call__(role, context, schema)
        if role == 'acceptance_designer':
            result['artifact_types'] = ['file']
            result['criteria'][0].update(description='核对成对材料的固定例号和匿名结构',
                                         verifier_id='blind-projection',
                                         test_spec_json=blind.canonical_bytes({'path': PROJECTION_PATH}).decode(),
                                         expected_result_json=blind.canonical_bytes({'case_ids': CASE_IDS}).decode())
        return result


class BlindLedgerTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name).resolve())
        self.workbench = Workbench(self.store, goal_enabled=True, outer_enabled=True, outer_model=BlindModel())
        self.intake = self.workbench.outer()
        self.ledger = BlindReviewLedger(self.workbench)
        self.frozen = inputs()
        self.public, self.private = blind.prepare('blind-test-1', *self.frozen, seed=21)
        self.origins = {'baseline': {'ref': 'private:baseline-actual-ref', 'prompt_sha256': blind.digest('baseline prompt')},
                        'team': {'ref': 'private:team-actual-ref', 'prompt_sha256': blind.digest('team prompt')}}
        received = self.intake.submit(text='提供八例成对匿名材料', source='web', source_event_id='intake', actor_id='owner')
        self.intake.drain()
        event = self.intake.event(received['event_id'], 'owner')
        draft = self.intake.get(event['ticket_id'], 'owner')
        self.ticket = self.intake.approve_goal_contract(draft['id'], draft['goal_revision'], draft['acceptance_revision'],
                                                     draft['revision'], 'approval', actor_id='owner', require_signoff=True)
        self.candidate = uuid4().hex
        self.checkpoint = self.offer(self.public, self.candidate)

    def offer(self, public, candidate):
        ticket_id = self.ticket['id']
        directory = self.store.root / 'goal-attempts' / ticket_id / candidate / 'verified'
        directory.mkdir(parents=True)
        (directory / PROJECTION_PATH).write_bytes(blind.canonical_bytes(public['projection']))
        checkpoint, files, occupied = outer_verification.capture(directory)
        engine = self.workbench.goal_engine()

        def stage(task):
            state = task['goal_execution']
            outer = state['outer']
            plan = {'steps': [{'acceptance': ['R1'], 'state': 'reviewing', 'title': '验收匿名候选'}]}
            assignment = {'id': candidate, 'run': 71, 'plan': plan, 'step': 0, 'version': task['version'],
                          'outer_binding': {key: outer[key] for key in ('goal_revision', 'acceptance_revision', 'mandate_revision')}}
            checks = outer_verification.verify(outer['acceptance_contract'], assignment, checkpoint, files, occupied)
            state['checkpoint'] = checkpoint
            task.update(plan=plan, status='reviewing', step=0)
            engine._advance_outer_review(task, assignment, {
                'verifier_profile': 'outer-v2', 'checkpoint': checkpoint, 'validation': {'checks': checks},
                'delivery': {'assignment': candidate, 'files': checkpoint['files'], 'level': 'goal',
                             'artifact_sha256': checkpoint['digest']},
            })

        self.store.change(ticket_id, stage)
        return checkpoint

    def register(self, **changes):
        heldout, baseline, team = self.frozen
        request = dict(ticket_id=self.ticket['id'], revision=self.ticket['revision'], candidate=self.candidate,
                       artifact_sha256=self.checkpoint['digest'], actor_id='owner', public=self.public, private=self.private,
                       heldout=heldout, baseline=baseline, team=team, origins=self.origins)
        return self.ledger.register(**{**request, **changes})

    def record_repair(self, candidate, issue):
        self.store.change(self.ticket['id'], lambda task: task['goal_execution']['assignments'].append(dict(
            id=candidate, phase='execute', state='settled', inner_repairs=[dict(status='passed', issue=issue)])))

    def lock(self, **changes):
        request = dict(ticket_id=self.ticket['id'], revision=self.ticket['revision'],
                       experiment_id=self.public['manifest']['experiment_id'], manifest_digest=self.public['manifest_digest'],
                       judgments=[{'id': 'H01', 'choice': 'tie', 'reason': '两段都回应了同一句话。'}],
                       request_id='ballot', actor_id='owner', source='dsh')
        return self.ledger.lock_ballot(**{**request, **changes})

    def gate(self, *, ticket=None, candidate=None):
        ticket_id = self.ticket['id']
        with self.store.connect() as db:
            self.ledger.require_locked_ballot(db, ticket or self.intake._ticket(db, ticket_id, 'owner', 'default'),
                                              self.store.get(ticket_id), candidate or self.candidate)

    def test_original_ticket_can_read_anonymous_projection_and_locked_provenance(self):
        ticket_id = self.ticket['id']
        self.assertIsNone(self.ledger.get(ticket_id, actor_id='owner'))
        with self.assertRaisesRegex(Problem, '匿名评价'):
            self.gate()
        view = self.register()
        self.assertEqual(view['status'], 'awaiting_ballot')
        self.assertEqual(view['projection'], self.public['projection'])
        self.assertEqual(view['ticket_revision'], self.ticket['revision'])
        self.assertEqual(self.register(), view)
        self.assertNotIn('result', view)
        with self.assertRaises(Problem) as exc:
            self.ledger.get(ticket_id, actor_id='someone-else')
        self.assertEqual(exc.exception.status, 404)
        result = self.lock()
        self.assertEqual(result['status'], 'locked')
        self.assertEqual(result['lock']['source'], 'dsh')
        self.assertEqual(result['lock']['operator_kind'], 'owner')
        self.assertEqual(result['lock']['actor_id'], 'owner')
        self.assertEqual(result['manifest']['mapping_commitment'], self.public['manifest']['mapping_commitment'])
        self.assertEqual(result['result']['counts'], {'wins': 0, 'ties': 1, 'losses': 0, 'unrated': 7})
        self.assertEqual(result['result']['cases'][0]['reason'], '两段都回应了同一句话。')
        self.assertEqual(result['ticket_revision'], self.ticket['revision'] + 1)
        self.assertEqual(self.intake.event(result['event_id'], 'owner')['action'], 'BLIND_BALLOT_LOCKED')
        for token in ('"mapping":', 'private:', 'prompt_sha256', self.private['salt'], self.private['seed']):
            self.assertNotIn(token, blind.canonical_bytes(result).decode())
        self.gate()
        self.intake.decide_artifact(ticket_id, 'accept', result['ticket_revision'], self.candidate,
                                    self.checkpoint['digest'], '', 'signoff', actor_id='owner', source='web')
        self.assertEqual(self.ledger.get(ticket_id, actor_id='owner')['status'], 'locked')

    def test_mismatched_package_archive_and_origin_are_rejected(self):
        with self.assertRaisesRegex(Problem, '两条生成路线'):
            self.register(origins={'baseline': self.origins['baseline'], 'team': self.origins['baseline']})
        other_team = copy.deepcopy(self.frozen[2])
        other_team['cases'][0]['response'] = '另一份回应，即使结构合法也应拒绝。'
        other, mapping = blind.prepare('blind-test-other', self.frozen[0], self.frozen[1], other_team, seed=21)
        with self.assertRaisesRegex(Problem, '验收的文件不一致'):
            self.register(public=other, private=mapping, team=other_team)
        with self.assertRaisesRegex(Problem, '验收候选已变化'):
            self.register(artifact_sha256='0' * 64)
        self.assertIsNone(self.ledger.get(self.ticket['id'], actor_id='owner'))

    def test_same_request_returns_same_lock_and_modified_replay_is_rejected(self):
        self.register()
        with self.assertRaisesRegex(Problem, '匿名材料已变化'):
            self.lock(manifest_digest='0' * 64)
        with self.assertRaisesRegex(Problem, '工单版本已变化'):
            self.lock(revision=self.ticket['revision'] - 1)
        first = self.lock()
        retry = self.lock()
        self.assertEqual(first['event_id'], retry['event_id'])
        self.assertTrue(retry['duplicate'])
        with self.assertRaisesRegex(Problem, '不能提交不同内容'):
            self.lock(judgments=[{'id': 'H01', 'choice': 'a', 'reason': '改了决定'}])
        with self.assertRaises(Problem) as exc:
            self.lock(source='web', request_id='late')
        self.assertEqual(exc.exception.status, 409)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM outer_events WHERE json_extract(body,'$.type')='blind_ballot'").fetchone()[0], 1)

    def test_parallel_ballots_only_increment_original_ticket_once(self):
        self.register()

        def submit(source):
            try:
                return self.lock(source=source, request_id=f'submission-{source}')['status']
            except Problem as error:
                return error.status

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertCountEqual(executor.map(submit, ('dsh', 'web')), ['locked', 409])
        self.assertEqual(self.intake.get(self.ticket['id'], 'owner')['revision'], self.ticket['revision'] + 1)

    def test_cannot_lock_after_plan_changes_and_old_projection_is_hidden(self):
        self.register()
        self.store.change(self.ticket['id'], lambda task: task['plan']['steps'][0].update(title='实际计划已改变'))
        view = self.ledger.get(self.ticket['id'], actor_id='owner')
        self.assertEqual(view['status'], 'superseded')
        self.assertNotIn('projection', view)
        with self.assertRaisesRegex(Problem, '当前候选已更换'):
            self.lock()

    def test_directory_and_returned_lock_keep_separate_views(self):
        from outer_channels import dispatch

        ticket_id = self.ticket['id']
        self.record_repair(self.candidate, '旧版返修记录')
        self.register()
        self.assertEqual(self.intake.list('owner')[0]['blind_status'], 'awaiting_ballot')
        self.assertEqual(self.intake.get(ticket_id,'owner')['blind_history'], [])
        result = self.lock()
        self.assertEqual(self.intake.list('owner')[0]['blind_status'], 'locked')
        self.intake.decide_artifact(ticket_id, 'return', result['ticket_revision'], self.candidate,
                                    self.checkpoint['digest'], '请调整语气', 'return', actor_id='owner', source='web')
        draft = self.intake.get(ticket_id,'owner')
        self.assertEqual(draft['blind_review']['status'], 'superseded')
        self.assertEqual(draft['blind_history'][0]['experiment_id'], result['experiment_id'])
        earlier = self.ledger.get(ticket_id,result['experiment_id'],actor_id='owner')
        self.assertEqual(earlier['result']['counts']['ties'], 1)
        candidate = uuid4().hex
        self.offer(self.public, candidate)
        self.record_repair(candidate, '新版返修记录')
        historic = dispatch(self.intake, 'web', 'get', {'ticket_id':ticket_id, 'experiment_id':result['experiment_id']})
        self.assertEqual(historic['blind_review']['status'], 'locked')
        self.assertIs(historic['blind_review']['current'], False)
        self.assertNotIn('新版返修记录', str(historic['execution']))

    def test_public_repair_history_restored_for_current_lock_and_completion(self):
        ticket_id = self.ticket['id']
        marker = '同版返修及复验记录'
        self.record_repair(self.candidate, marker)
        self.register()
        self.assertNotIn(marker, str(self.intake.get(ticket_id, 'owner')['execution']))
        locked = self.lock()
        self.assertIn(marker, str(self.intake.get(ticket_id, 'owner')['execution']))

        # 即使并发读到自称当前的锁票，摘要也必须与返回给界面的任务快照一致。
        ledger = self.workbench.blind_review()
        for binding in ('candidate','artifact_sha256'):
            with patch.object(ledger, 'get', return_value={**locked, binding:'older'}):
                self.assertNotIn(marker, str(self.intake.get(ticket_id, 'owner')['execution']))

        self.intake.decide_artifact(ticket_id, 'accept', locked['ticket_revision'], self.candidate,
                                    self.checkpoint['digest'], '', 'signoff', actor_id='owner', source='web')
        with patch.object(ledger, 'get', return_value=None):
            detail = self.intake.get(ticket_id, 'owner')
        self.assertEqual(detail['execution']['status'], 'completed')
        self.assertIn(marker, str(detail['execution']))

    def test_public_workbench_does_not_project_unlocked_route_fingerprints(self):
        task = self.store.get(self.ticket['id'])
        state = task['goal_execution']
        state['assignments'] = [
            {'report':{'preference':{'options':[]}}, 'role_calls':[{'id':'initial-sample'}]},
            {'report':{'result':{'summary':'baseline or team'},'model_audit':{'arm':'team'}},
             'role_calls':[{'prompt_sha256':'private hash', 'usage':{'output_tokens':32}}]},
        ]
        first, arms = omit_call_payloads(task)['goal_execution']['assignments']
        self.assertEqual(first['role_calls'], [{'id':'initial-sample'}])
        self.assertEqual(arms, {'report':{}})

    def test_new_progress_projection_does_not_expose_blind_repair_or_plan(self):
        from outer_channels import dispatch
        from superagent.goal_team_records import PROFILE

        ticket_id = self.ticket['id']
        marker = '可识别来源的内部策略'

        def stage(task):
            task.update(status='executing', phase='execute')
            state = task['goal_execution']
            state['outer']['mandate']['execution_profile'] = PROFILE
            record = dict(status='repairing', issue=marker)
            state.update(stage='execute', team_pending_repair={'repair_record':record}, assignments=[dict(
                id='blind-arm', phase='execute', state='dispatched', transport='claimed', team_review_mode='review',
                inner_repairs=[record], repair_continuation={'finding':marker}, role_calls=[dict(
                    sequence=1, role='lead', writable=False, status='completed', action='done', summary=marker)])])

        self.store.change(ticket_id, stage)
        brief = dispatch(self.intake, 'web', 'list', {})[0]
        detail = dispatch(self.intake, 'web', 'get', {'ticket_id':ticket_id})
        self.assertEqual(brief['journey'], detail['journey'])
        self.assertEqual((detail['execution_phase'],detail['inner_repair_status'],detail['journey']['label']),
                         ('execute',None,'正在执行'))
        self.assertNotIn(marker, str(detail))
        self.assertEqual(detail['execution']['team_activity'], [])
        self.assertIn(marker, str(self.store.get(ticket_id)))

    def test_generation_uses_the_same_required_case_keys_with_distinct_role_receipts(self):
        root = self.store.root
        workspace = root/'candidate'
        workspace.mkdir()
        calls, input_records = [], []

        def ask(role, instruction, data, **kwargs):
            i = len(calls) + 1
            calls.append({'id':f'case-run:{i}','role':role,'status':'completed',
                          'started_ns':i * 1_000_000,'ended_ns':(i + 1) * 1_000_000,
                          'prompt_sha256':blind.digest(instruction)})
            schema = kwargs.get('response_schema')
            if schema is None:
                self.assertNotIn('heldout',data)
                return {'summary':'先回应具体顾虑。'}
            input_records.append((data,schema))
            self.assertEqual(schema['required'],CASE_IDS)
            self.assertFalse(schema['additionalProperties'])
            return {case:'先听清你具体在担心什么。' for case in reversed(CASE_IDS)}

        make_blind_draft(ask, {'id':'case-run','preference_answer':{'text':'简短'},'step':0,
                              'plan':{'steps':[{}]}},
                         {'goal':'撰写简短回应','owner_preference':{'text':'简短'}},
                         {'expected_result':{'case_ids':CASE_IDS},'test_spec':{'path':'blind.json'}},
                         workspace,root,calls,allowed=4)
        control, schema = input_records[0]
        team, team_schema = input_records[1]
        self.assertEqual(team_schema,schema)
        self.assertEqual(control, {key:team[key] for key in control})
        prepared = blind.load_json((root/'blind-preparation.json').read_bytes())
        self.assertEqual([row['id'] for row in prepared['team']['cases']],CASE_IDS)
        measures = _runtime_measures({'goal_execution':{'assignments':[{'id':'case-run','role_calls':calls}]}},
                                     prepared,'case-run')
        self.assertEqual(measures,{'baseline':{'calls':1,'model_wall_ms':1},
                                   'team':{'calls':3,'model_wall_ms':3},'human_relays':0})

    def test_cutoff_persists_before_deblinding_and_test_operator_cannot_sign_for_owner(self):
        self.register()
        original = blind.unblind

        def after_commit(*args, **kwargs):
            with self.store.connect() as db:
                row = db.execute('SELECT ballot_digest,locked_at FROM goal_blind_reviews').fetchone()
            self.assertTrue(row['locked_at'])
            self.assertEqual(row['ballot_digest'], kwargs['expected_ballot_digest'])
            return original(*args, **kwargs)

        context = operator_kind.set('acceptance_test')
        try:
            with patch.object(blind, 'unblind', side_effect=after_commit):
                result = self.lock()
            self.assertEqual(result['lock']['operator_kind'], 'acceptance_test')
            self.gate()
        finally:
            operator_kind.reset(context)
        with self.assertRaisesRegex(Problem, '先完成当前候选'):
            self.gate()

    def test_tampered_lock_digest_fails_closed(self):
        self.register()
        self.lock()
        with self.store.connect() as db:
            db.execute('UPDATE goal_blind_reviews SET ballot_digest=?', ('0' * 64,))
        with self.assertRaisesRegex(Problem, '原单锁定回执不一致'):
            self.ledger.get(self.ticket['id'], actor_id='owner')
        with self.assertRaisesRegex(Problem, '原单锁定回执不一致'):
            self.gate()


if __name__ == '__main__':
    unittest.main()
