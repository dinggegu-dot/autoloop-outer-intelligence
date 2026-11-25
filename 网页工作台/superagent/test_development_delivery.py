"""开发发布回执只进入由本人另签的原单和确切 PR 提交。"""

import base64
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import run
from tempfile import TemporaryDirectory
from unittest import TestCase

from superagent.development_delivery import (
    authorize_release, read_release, record_progress, register_release, release_view,
)
from superagent.development_handoff import additional_files, handoff_lock, prepare_handoff, verify_settings_safety
from superagent.outer_intake import OuterIntake
from superagent.outer_verification import sha256
from workbench import Problem, Store, Workbench


class DevelopmentDeliveryTests(TestCase):
    def setUp(self):
        runtime = TemporaryDirectory()
        self.addCleanup(runtime.cleanup)
        self.store = Store(runtime.name)
        self.ticket = 'wb-' + '0'*32
        self.head = 'a'*40
        files = {'app.py':hashlib.sha256(b'approved').hexdigest()}
        self.digest = sha256(sorted(files.items()))
        self.merge = 'c'*40
        self.task = {
            'id':self.ticket, 'status':'completed',
            'goal_delivery':{'artifact_sha256':self.digest,'files':files},
            'goal_execution':{'outcome':'VERIFIED_COMPLETE', 'outer':{
                'mandate_revision':1, 'approval_event_id':'approved-version', 'actor_id':'owner',
            }},
        }
        with self.store.connect() as db:
            db.execute('INSERT INTO tasks VALUES (?,?,?,?)',
                       (self.ticket,'request',json.dumps(self.task),'now'))

    def authorize(self, **changes):
        terms = dict(mandate=1, digest=self.digest, repo='Fixture/Service', number=42, head=self.head, mode='release')
        return authorize_release(self.store, self.ticket, **(terms | changes))

    def payload(self, phase, **changes):
        return dict(repo='Fixture/Service', number=42, head=self.head, phase=phase,
                    at=(datetime.now(timezone.utc) + timedelta(milliseconds=100)).isoformat()) | changes

    def test_unregistered_ordinary_ticket_is_not_projected_and_marking_does_not_authorize(self):
        with self.store.connect() as db:
            self.assertIsNone(release_view(db,self.task))
        self.assertEqual(register_release(self.store,self.ticket), {'status':'unauthorized'})
        self.assertFalse(record_progress(self.store,self.payload('accepted'))['recorded'])
        with self.assertRaises(Problem):
            self.authorize(digest='d'*64)
        self.assertEqual(read_release(self.store,self.ticket), {'status':'unauthorized'})

    def test_commit_bound_to_current_ticket_preserves_verification_differences(self):
        authorized = self.authorize()
        self.assertEqual(authorized['status'], 'awaiting_report')
        self.assertEqual(self.authorize(), authorized)
        self.assertFalse(record_progress(self.store,self.payload('accepted', head='d'*40))['recorded'])
        self.assertFalse(record_progress(self.store,self.payload('accepted', at='2020-01-01T00:00:00Z'))['recorded'])
        for phase, evidence in (
            ('reviewing', {}), ('checking', {}), ('returned', {'detail':'review'}),
            ('merged', {'merge':self.merge}), ('docs_only', {'merge':self.merge,'ship':876}),
            ('live', {'merge':self.merge,'ship':877}),
        ):
            payload = self.payload(phase, **evidence)
            self.assertEqual(record_progress(self.store,payload)['status'], phase)
            self.assertEqual(record_progress(self.store,payload)['status'], phase)
            projection = read_release(self.store,self.ticket)
            self.assertEqual(projection['receipt']['source'], 'agent-autoloop-delivery')
            self.assertEqual(projection['status'], phase)
            for key,value in evidence.items():
                self.assertEqual(projection['receipt'][key], value)
        with self.assertRaises(Problem):
            record_progress(self.store,self.payload('live', merge=self.merge))
        self.assertEqual(read_release(self.store,self.ticket)['receipt']['ship'],877)
        self.store.change(self.ticket, lambda task:task['goal_execution']['outer'].update(mandate_revision=2))
        self.assertEqual(read_release(self.store,self.ticket), {'status':'unauthorized'})
        self.assertFalse(record_progress(self.store,self.payload('returned'))['recorded'])

    def test_new_head_requires_separate_authorization_and_old_events_cannot_update_it(self):
        self.authorize()
        failure = self.payload('returned', detail='review')
        record_progress(self.store, failure)
        self.assertEqual(read_release(self.store,self.ticket)['status'],'returned')
        new_head = 'd'*40
        self.assertFalse(record_progress(self.store,self.payload('accepted',head=new_head))['recorded'])
        self.authorize(head=new_head)
        self.assertFalse(record_progress(self.store,failure)['recorded'])
        self.assertEqual(read_release(self.store,self.ticket)['status'],'awaiting_report')
        record_progress(self.store,self.payload('accepted',head=new_head))
        self.assertEqual(read_release(self.store,self.ticket)['authorization']['head'],new_head)

    def test_authenticated_original_ticket_detail_projects_received_release(self):
        intake = OuterIntake(Workbench(self.store))
        ticket = {'id':self.ticket,'status':'AUTHORIZED','task_id':self.ticket,
                  'original_request_refs':['synthetic-owner-request']}
        with self.store.connect() as db:
            db.execute('INSERT INTO outer_tickets VALUES (?,?,?,?,?)',
                       (self.ticket,'default','owner',json.dumps(ticket),'now'))
        self.assertNotIn('development_delivery',intake.get(self.ticket,'owner'))
        register_release(self.store,self.ticket)
        self.assertEqual(intake.get(self.ticket,'owner')['development_delivery']['status'],'unauthorized')
        self.authorize()
        record_progress(self.store,self.payload('live',merge=self.merge,ship=876))
        detail = intake.get(self.ticket,'owner')['development_delivery']
        self.assertEqual((detail['status'],detail['receipt']['merge'],detail['receipt']['ship']),
                         ('live',self.merge,876))
        with self.assertRaises(Problem):
            intake.get(self.ticket,'unrelated')

    def test_handoff_creates_draft_once_and_reuses_exact_head(self):
        log, prs = [], []
        body = b'approved'
        target = {'number':42, 'state':'open', 'draft':True, 'labels':[], 'auto_merge':None, 'changed_files':1,
                  'base':{'ref':'main'},
                  'head':{'sha':self.head,'ref':'fixed','repo':{'full_name':'Fixture/Service'}}}

        def github(path, data=None):
            log.append((path,data))
            if 'git/ref/heads/main' in path:
                return {'object':{'sha':'f'*40}}
            if 'contents/ops/delivery.json' in path:
                return {'content':base64.b64encode(b'{"repo":"Fixture/Service"}').decode()}
            if 'git/ref/heads/fixed' in path:
                return {'object':{'sha':self.head}}
            if '/git/commits/' in path:
                return {'tree':{'sha':'e'*40}}
            if '/git/trees/' in path:
                return {'tree':[{'path':'app.py','mode':'100644','type':'blob','sha':'d'*40}]}
            if '/git/blobs/' in path:
                return {'size':len(body),'sha':'d'*40,'encoding':'base64','content':base64.b64encode(body).decode()}
            if '/compare/' in path:
                return {'files':[{'filename':'app.py'}]}
            if '/pulls/42/files?' in path:
                return [{'filename':'app.py'}]
            if 'pulls?' in path:
                return prs
            if path.endswith('/pulls'):
                self.assertEqual(data['head'],'fixed')
                self.assertTrue(data['draft'])
                prs.append(target)
                return target
            self.assertTrue(path.endswith('/pulls/42'))
            return target

        terms = dict(mandate=1,digest=self.digest,repo='Fixture/Service',base='main',
                     branch='fixed',head=self.head,mode='release',api=github)
        first = prepare_handoff(self.store,self.ticket,**terms)
        self.assertEqual(prepare_handoff(self.store,self.ticket,**terms), first)
        self.assertEqual(first['number'],42)
        self.assertEqual(sum(data is not None for path,data in log),1)
        self.assertEqual(read_release(self.store,self.ticket)['authorization']['artifact_match'],
                         {'mapping':'same_path','files_checked':1,'pr_changes_checked':True})
        body = b'changed'
        with self.assertRaisesRegex(Problem,'路径或字节'):
            prepare_handoff(self.store,self.ticket,**terms)
        self.assertEqual(sum(data is not None for path,data in log),1)
        body = b'approved'
        target['merged_at'] = 'now'
        target['state'] = 'closed'
        log.clear()
        self.assertEqual(prepare_handoff(self.store,self.ticket,**terms)['number'],42)
        self.assertFalse(any('/heads/fixed' in path for path,data in log))

    def test_handoff_single_flight_holds_until_owner_process_exits(self):
        source = (
            'from pathlib import Path; from superagent.development_handoff import handoff_lock; '
            'import sys\n'
            'with handoff_lock(Path(sys.argv[1]),"fixture/service","main","feature"): print("acquired")'
        )
        command = [sys.executable,'-B','-c',source,str(self.store.root)]
        with handoff_lock(self.store.root,'Fixture/Service','main','feature'):
            running = run(command, capture_output=True,text=True)
            self.assertNotEqual(running.returncode,0)
            self.assertIn('已在执行',running.stderr)
        self.assertEqual(run(command,capture_output=True,text=True).stdout.strip(),'acquired')

    def test_only_explicitly_named_extra_executable_files_are_authorized(self):
        diff = [{'filename':'app.py'},{'filename':'readme.md'},{'filename':'script.py'},
                {'filename':'run.md'}]
        with self.assertRaisesRegex(Problem,'run.md、script.py'):
            additional_files(diff,{'app.py':'verified'},(),lambda:{'readme.md'})
        self.assertEqual(additional_files(diff,{'app.py':'verified'},('run.md','script.py'),lambda:{'readme.md'}),
                         ['readme.md','run.md','script.py'])

    def test_handoff_unconfigured_or_moved_branch_causes_no_remote_writes(self):
        writes = []
        configured = False

        def github(path, data=None):
            if data is not None:
                writes.append((path,data))
            if 'contents/ops/delivery.json' in path:
                if configured:
                    return {'content':base64.b64encode(b'{"repo":"Fixture/Service"}').decode()}
                raise Problem('missing config',404)
            if '/heads/fixed' in path:
                return {'object':{'sha':'d'*40}}
            return {'object':{'sha':self.head}}

        terms = dict(mandate=1,digest=self.digest,repo='Fixture/Service',base='main',
                     branch='fixed',head=self.head,mode='release',api=github)
        with self.assertRaisesRegex(Problem,'未能核对所选仓库'):
            prepare_handoff(self.store,self.ticket,**terms)
        configured = True
        with self.assertRaisesRegex(Problem,'分支与本次授权提交不一致'):
            prepare_handoff(self.store,self.ticket,**terms)
        self.assertFalse(writes)

    def test_same_pr_head_reauthorized_does_not_accept_old_or_ambiguous_callback(self):
        old = self.authorize(mode='review')['authorization']['id']
        fresh = self.authorize(mode='release')['authorization']['id']
        self.assertNotEqual(old,fresh)
        self.assertFalse(record_progress(self.store,self.payload('accepted',authorization=old))['recorded'])
        self.assertFalse(record_progress(self.store,self.payload('accepted'))['recorded'])
        self.assertTrue(record_progress(self.store,self.payload('accepted',authorization=fresh))['recorded'])

    def test_manual_receipt_is_not_reinterpreted_as_a_verified_file_match(self):
        legacy = self.authorize()['authorization']
        record_progress(self.store,self.payload('live',merge=self.merge,ship=10))
        self.assertNotIn('artifact_match',read_release(self.store,self.ticket)['authorization'])
        fresh = self.authorize(artifact_match={'mapping':'same_path','files_checked':1})
        self.assertNotEqual(fresh['authorization']['id'],legacy['id'])
        self.assertEqual(fresh['status'],'awaiting_report')

    def test_custom_local_review_commands_and_admin_merges_are_not_trusted(self):
        for settings in (
                {'review':{'command':['unexpected']}},
                {'review':{'agents':{'some-agent':{'command':['unexpected']}}}},
                {'admin_merge':['operator']}):
            with self.subTest(settings=settings),self.assertRaises(ValueError):
                verify_settings_safety({'repo':'Fixture/Service',**settings},'fixture/service')

    def test_gh_guard_prevents_stale_internal_patch_and_checks_exact_write_body(self):
        match = {'mapping':'same_path','files_checked':1}
        auth = self.authorize(artifact_match=match)['authorization']['id']
        proxy = Path(__file__).resolve().parents[2] / 'scripts/pinned_delivery_gh.py'
        fake = self.store.root/'fake-gh'
        log = self.store.root/'fake-gh.log'
        pr = {'number':42,'labels':[],'base':{'ref':'main'},
              'head':{'sha':self.head,'ref':'fixed','repo':{'full_name':'Fixture/Service'}}}
        fake.write_text(
            '#!/usr/bin/env python3\nimport json,os,sys\n'
            'print(os.environ["FAKE_PR"] if len(sys.argv) == 3 else "{}")\n'
            'if len(sys.argv) > 3:\n    open(os.environ["FAKE_LOG"],"a").write(sys.argv[4]+"\\n")\n')
        fake.chmod(0o700)
        target = {'repo':'Fixture/Service','number':42,'head':self.head,'branch':'fixed',
                  'mode':'release','ticket':self.ticket,'authorization':auth}
        env = dict(os.environ, AUTOLOOP_TICKET_DELIVERY_RUNTIME=str(self.store.root),
                   AUTOLOOP_PINNED_DELIVERY_TARGET=json.dumps(target),AUTOLOOP_PINNED_REAL_GH=str(fake),
                   FAKE_PR=json.dumps(pr),FAKE_LOG=str(log))

        def patch_pr(body):
            return run([str(proxy),'api','repos/Fixture/Service/pulls/42','--method','PATCH','--input','-'],
                       input=json.dumps(body),text=True,capture_output=True,env=env)

        self.assertNotEqual(patch_pr({'base':'unrelated'}).returncode,0)
        self.assertFalse(log.exists())
        self.assertEqual(patch_pr({'body':'trusted review report'}).returncode,0)
        self.assertEqual(log.read_text(),'PATCH\n')
        self.authorize(mode='review',artifact_match=match)
        self.assertNotEqual(patch_pr({'body':'stale review'}).returncode,0)
        self.assertEqual(log.read_text(),'PATCH\n')

    def test_callback_process_persists_receipt_but_review_only_cannot_prove_publication(self):
        self.authorize(mode='review')
        script = Path(__file__).resolve().parents[2] / 'scripts/record_ticket_delivery.py'
        payload = self.payload('live', merge=self.merge, ship=42)
        result = run([str(script)], input=json.dumps(payload), text=True, capture_output=True,
                     env=dict(os.environ, AUTOLOOP_TICKET_DELIVERY_RUNTIME=str(self.store.root)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'scope_mismatch')
        self.assertEqual(read_release(self.store,self.ticket)['status'],'scope_mismatch')
