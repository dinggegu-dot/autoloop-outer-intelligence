"""合成工单：沿原入口签收、续改、再签收；执行产物由夹具提供。"""
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from outer_channels import dispatch
from outer_workbench import create_task
from workbench import Problem, Store, Workbench
from superagent.outer_verification import capture
from superagent.ticket_rounds import changes_old_requirement
from superagent.test_outer_intake import Model, Registry


class RoundModel(Model):
    def __call__(self, role, context, schema):
        result = super().__call__(role, context, schema)
        if 'superseded_requirements' in schema['properties']:
            # 故意让模型把新增意见也当成替换，检查服务端能否保住旧要求。
            old = context['goal_contract']['deliverables'][0]
            result['superseded_requirements'] = [
                {'requirement_id':old['requirement_id'], 'change_clause':context['untrusted_text']['current_message']}]
        return result


class TicketRoundsTest(unittest.TestCase):
    filename = '成果 附件/示例 说明.md'

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name).resolve())
        self.wb = Workbench(self.store, goal_enabled=True, outer_enabled=True, outer_registry=Registry())
        self.wb.outer_workbench_entry = self.wb.outer_goal_team = True
        self.intake = self.wb.outer()
        self.model = RoundModel()
        self.intake.team_model = self.model
        self.counter = 0
        self.ticket = self.new_ticket('整理五项说明')
        self.sign(self.ticket, b'first edition')

    def latest(self, identifier):
        return dispatch(self.intake, 'web', 'get', {'ticket_id':identifier})

    def new_ticket(self, text):
        receipt = create_task(self.wb, dict(workflow='outer', request_id=str(uuid4()), name='示例',
                              goal=text, acceptance='', allow_model_input=True))
        identifier = receipt['ticket_id']
        self.intake.drain_ticket(identifier, limit=3)
        self.approve(identifier)
        return identifier

    def approve(self, identifier):
        row = self.latest(identifier)
        return dispatch(self.intake,'web','approve',dict(ticket_id=identifier, request_id=str(uuid4()),
                        revision=row['revision'], goal_revision=row['goal_revision'],
                        acceptance_revision=row['acceptance_revision'], require_signoff=True))

    def offer(self, identifier, data):
        self.counter += 1
        candidate = f'{self.counter:032x}'
        root = self.store.root / 'goal-attempts' / identifier / candidate / 'verified'
        artifact = root / self.filename
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(data)
        checkpoint = capture(root)[0]
        ticket = self.latest(identifier)

        def offer(task):
            state = task['goal_execution']
            task.update(status='awaiting_artifact_review', plan={'steps':[{}]}, step=0)
            state.update(stage='awaiting_artifact_review', checkpoint=checkpoint)
            state['artifact_reviews'] = [dict(sequence=1,state='pending',level='goal',assignment=candidate,
                artifact_sha256=checkpoint['digest'],files=checkpoint['files'],plan_revision=task['version'],
                **{key:ticket[key] for key in ('goal_revision','acceptance_revision','mandate_revision')})]

        self.store.change(identifier, offer)
        return candidate, checkpoint['digest']

    def sign(self, identifier, data):
        candidate, digest = self.offer(identifier, data)
        dispatch(self.intake, 'web', 'accept_artifact', dict(ticket_id=identifier,request_id=str(uuid4()),
                 revision=self.latest(identifier)['revision'],candidate=candidate,artifact_sha256=digest))
        return candidate

    def request(self, text, identifier=None):
        ticket = self.latest(identifier or self.ticket)
        base = ticket['continuation']['base']
        return dict(ticket_id=ticket['id'],revision=ticket['revision'],request_id=str(uuid4()),text=text,
                    intent='continue',candidate=base['assignment'],artifact_sha256=base['artifact_sha256'],
                    allow_model_input=True)

    def submit(self, body):
        return dispatch(self.intake, 'web', 'submit', body)

    def test_can_cancel_a_waiting_candidate_and_download_previous_signed_edition(self):
        original = self.latest(self.ticket)
        first = original['continuation']['base']['assignment']
        self.assertFalse(original['journey']['stop'])
        self.submit(self.request('增加一页简述'))
        self.intake.drain_ticket(self.ticket,limit=2)
        self.approve(self.ticket)
        candidate, digest = self.offer(self.ticket,b'unsigned second edition')
        waiting = self.latest(self.ticket)
        self.assertTrue(waiting['journey']['artifact_review'])
        self.assertTrue(waiting['journey']['stop'])
        request = dict(ticket_id=self.ticket,revision=waiting['revision'],request_id=str(uuid4()))
        dispatch(self.intake,'web','stop',request)
        self.assertTrue(dispatch(self.intake,'web','stop',request)['duplicate'])
        stopped = self.latest(self.ticket)
        self.assertEqual((stopped['status'],stopped['journey']['station'],stopped['journey']['label']),
                         ('STOPPED','dock','已取消'))
        self.assertFalse(any(stopped['journey'][key] for key in ('approve','pause','resume','stop','artifact_review')))
        self.assertEqual(self.store.get(self.ticket)['goal_execution']['artifact_reviews'][-1]['state'],'cancelled')
        for command, args in (
                ('accept_artifact',dict(candidate=candidate,artifact_sha256=digest)),
                ('submit',dict(intent='supplement',text='旧页面发来意见',allow_model_input=True)),
                ('resume',{})):
            with self.subTest(command=command), self.assertRaises(Problem):
                dispatch(self.intake,'web',command,dict(request,request_id=str(uuid4()),revision=stopped['revision'],**args))
        self.assertEqual(self.wb.artifact_bytes(self.ticket,self.filename,candidate=first)[1], b'first edition')
        with patch('workbench.is_blind_goal',return_value=True), patch.object(self.wb.blind_review(),'get',return_value=None):
            self.wb.require_public_artifact(self.ticket,first)
            for hidden in (candidate,'f'*32):
                with self.subTest(hidden=hidden), self.assertRaisesRegex(Problem,'匿名评阅完成前'):
                    self.wb.require_public_artifact(self.ticket,hidden)

    def test_original_ticket_survives_three_deliveries_and_old_budget(self):
        original = self.request('改成三项说明')
        second_request = self.submit(original)
        self.assertTrue(self.submit(original)['duplicate'])
        self.assertEqual(second_request['ticket_id'], self.ticket)
        pending = self.latest(self.ticket)
        self.assertEqual(pending['status'], 'ASSESSING')
        self.assertEqual((pending['continuation']['execution_round'], pending['continuation']['round']), (1, 2))
        self.assertFalse(pending['continuation']['can_start'])
        summary = dispatch(self.intake, 'web', 'list', {})[0]
        self.assertNotIn('work_round', summary)
        self.assertEqual(summary['continuation'], {'round':2, 'execution_round':1, 'can_start':False})
        self.assertEqual(self.wb.artifact_bytes(self.ticket,self.filename)[1], b'first edition')
        # 累计用量已超过两次；新轮从自己的额度开始。
        with patch('superagent.outer_intake._MAX_CALLS', 2):
            self.assertEqual(self.intake.drain_ticket(self.ticket,limit=3), 2)
        ready = self.latest(self.ticket)
        self.assertEqual(ready['status'], 'AWAITING_APPROVAL')
        self.assertEqual([r['description'] for r in ready['goal_contract']['deliverables']], ['改成三项说明'])
        self.approve(self.ticket)
        self.assertEqual((self.store.workspace(self.ticket)/self.filename).read_bytes(), b'first edition')
        self.assertEqual(self.latest(self.ticket)['continuation']['execution_round'], 2)
        second = self.sign(self.ticket, b'second edition')
        third_request = self.request('增加一节附录')
        self.assertEqual(third_request['candidate'], second)
        self.submit(third_request)
        self.intake.drain_ticket(self.ticket, limit=2)
        ready = self.latest(self.ticket)
        self.assertCountEqual([r['description'] for r in ready['goal_contract']['deliverables']],
                              ['改成三项说明','增加一节附录'])
        self.approve(self.ticket)
        self.assertEqual((self.store.workspace(self.ticket)/self.filename).read_bytes(), b'second edition')
        self.sign(self.ticket, b'third edition')
        latest = self.latest(self.ticket)
        self.assertEqual(latest['continuation']['round'],3)
        self.assertTrue(latest['continuation']['can_start'])
        self.assertEqual([r['round'] for r in latest['prior_deliveries']], [1,2])
        self.assertEqual(latest['prior_deliveries'][1]['change_request'], original['text'])
        self.assertEqual([self.wb.artifact_bytes(self.ticket,self.filename,candidate=r['assignment'])[1]
                         for r in latest['prior_deliveries']], [b'first edition', b'second edition'])
        self.assertEqual(self.wb.artifact_bytes(self.ticket,self.filename)[1], b'third edition')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0],1)

    def test_additions_and_unrelated_replacements_keep_explicit_requirements(self):
        for text in ('再增加一个附录','保留五项说明','把7天改为5天','按你的想法优化'):
            with self.subTest(text=text):
                ticket = self.new_ticket('整理五项说明')
                self.sign(ticket, b'reference')
                self.submit(self.request(text, ticket))
                self.intake.drain_ticket(ticket, limit=2)
                goal = self.latest(ticket)['goal_contract']
                self.assertCountEqual([item['description'] for item in goal['deliverables']], ['整理五项说明',text])
                self.assertEqual(goal['changes']['deliverables']['removed'], [])

    def test_replacements_require_an_explicit_change_and_matching_old_value(self):
        for clause, old, allowed in (
            ('把7天改为5天','制作7天旅行计划',True), ('从七天改成五天','制作七天旅行计划',True),
            ('取消夜间行程','安排夜间行程',True), ('请删除“背景章节”','编写背景章节',True),
            ('改成三项说明','整理五项说明',True), ('把7天改为5天','制作17天旅行计划',False),
            ('新增如何取消夜间行程的说明','安排夜间行程',False),
            ('增加7天改为5天的示例','增加7天行程示例',False), ('保留原内容','原内容',False),
            ('删除夜间行程','整理夜间行程和地图',False), ('是否把7天改为5天？','制作7天旅行计划',False),
            ('把预算表改为行程表','制作7天旅行计划',False),
        ):
            with self.subTest(clause=clause, old=old):
                self.assertEqual(changes_old_requirement(clause,old), allowed)

    def test_concurrent_requests_bound_to_same_ticket_and_version(self):
        other = self.new_ticket('另一张说明'); self.sign(other, b'other')
        body = self.request('增加细节')
        for change in [dict(candidate=self.request('其他', other)['candidate']), dict(allow_model_input=False),
                       dict(revision=body['revision']-1), dict(artifact_sha256='0'*64), dict(question_id='test')]:
            with self.assertRaises(Problem):
                self.submit({**body, **change})
        def submit(i):
            try:
                return self.submit({**body,'request_id':str(uuid4()),'text':f'修改{i}'})['state']
            except Problem as error:
                return error.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(pool.map(submit,[1,2]), ['PENDING',409])
        self.assertTrue(self.latest(other)['continuation']['can_start'])

    def test_reapprove_current_round_starts_from_its_signed_source(self):
        self.submit(self.request('改成三项说明'))
        self.intake.drain_ticket(self.ticket, limit=2)
        self.approve(self.ticket)
        workspace = self.store.workspace(self.ticket)
        (workspace/self.filename).write_bytes(b'unverified working draft')
        (workspace/'临时稿.txt').write_text('尚未签收')
        self.submit(dict(ticket_id=self.ticket, revision=self.latest(self.ticket)['revision'], request_id=str(uuid4()),
                         intent='change', text='附上目录', allow_model_input=True))
        self.intake.drain_ticket(self.ticket, limit=2)
        row = self.latest(self.ticket)
        self.assertEqual(row['status'],'AWAITING_APPROVAL')
        dispatch(self.intake, 'web', 'pause', dict(ticket_id=self.ticket, revision=row['revision'], request_id=str(uuid4())))
        self.approve(self.ticket)
        fresh = self.store.workspace(self.ticket)
        self.assertEqual(self.latest(self.ticket)['continuation']['execution_round'], 2)
        self.assertNotEqual(fresh, workspace)
        self.assertEqual((fresh/self.filename).read_bytes(), b'first edition')
        self.assertFalse((fresh/'临时稿.txt').exists())
        self.assertEqual((workspace/self.filename).read_bytes(), b'unverified working draft')

    def test_corrupt_archive_blocks_approval_and_original_request_recovers(self):
        body = self.request('改成三项说明')
        self.submit(body); self.intake.drain_ticket(self.ticket, limit=2)
        artifact, _ = self.wb.artifact_bytes(self.ticket, self.filename, candidate=body['candidate'])
        artifact.write_bytes(b'changed after signing')
        with self.assertRaisesRegex(Problem, '归档与交付'):
            self.approve(self.ticket)
        self.assertEqual(self.latest(self.ticket)['mandate_revision'], 1)
        artifact.write_bytes(b'first edition')
        # 模拟生成新版工作区后、SQLite 提交前进程退出：重试不能覆盖非本单内容。
        workspace = self.store._create_workspace(self.ticket, generation=2, recoverable=True,
                                                  initial_files={self.filename:b'first edition'})
        (workspace/self.filename).write_bytes(b'other content')
        with self.assertRaisesRegex(Problem, '工作区与续改'):
            self.approve(self.ticket)
        (workspace/self.filename).write_bytes(b'first edition')
        self.approve(self.ticket)
        self.assertEqual(self.latest(self.ticket)['continuation']['execution_round'], 2)

    def test_plain_supplement_after_delivery_does_not_start_revision(self):
        body = self.request('保留在本单供参考')
        for key in ('candidate','artifact_sha256'):
            body.pop(key)
        self.assertEqual(self.submit({**body,'intent':'supplement'})['action'], 'ATTACH_TO_TICKET')
        self.assertEqual(self.latest(self.ticket)['continuation']['round'], 1)
        self.submit({**body,'intent':'change','request_id':str(uuid4())})
        self.assertEqual(self.latest(self.ticket)['continuation']['round'], 2)
