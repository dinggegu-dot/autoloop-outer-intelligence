"""真实 HTTP、私有 socket 与 SQLite；模型和飞书运输使用离线夹具。"""
import http.cookiejar
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPCookieProcessor
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'网页工作台'))
from bridge import Bridge, LocalBoard, MobileProblem
from private_intake import serve_private
from server import Handler
from workbench import Store, Workbench
from superagent.test_outer_intake import Model, Registry


class QuietHandler(Handler):
    def log_message(self,*args):
        pass


class CrossChannelTest(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name).resolve()
        self.wb=Workbench(Store(self.root/'board'),Mock(),goal_enabled=True,outer_enabled=True,
                          outer_model=Model(),outer_registry=Registry())
        self.key=secrets.token_bytes(32)
        control=self.root/'bridge.sock'
        sock=serve_private(control,self.wb.store,self.wb,threading.Lock(),self.key)
        self.addCleanup(sock.server_close)
        self.addCleanup(sock.shutdown)
        self.mobile=LocalBoard(str(control),self.key)
        self.bridge=Bridge({'owner':'ou_test','data_dir':str(self.root/'inbox'),'outer_intelligence':True,
                            'workbench_only':True},board=self.mobile,hub=Mock())
        self.addCleanup(self.bridge.store.db.close)
        self.http=ThreadingHTTPServer(('127.0.0.1',0),QuietHandler)
        self.http.workbench=self.wb
        self.http.task_actions=threading.Lock()
        self.http.csrf='local-test-csrf'
        self.http.session_token=secrets.token_hex(8)
        threading.Thread(target=self.http.serve_forever,daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.base='http://127.0.0.1:'+str(self.http.server_port)
        self.client=build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        with self.client.open(self.base+'/api/session') as resp:self.csrf=json.load(resp)['csrf']

    def post(self,path,body,session=True):
        req=Request(self.base+path,data=json.dumps(body).encode(),headers={
            'Content-Type':'application/json','Origin':self.base,'X-Odyssey-CSRF':self.csrf if session else ''})
        with self.client.open(req) as resp:return json.load(resp)

    def test_mobile_receive_and_web_approval_share_ticket(self):
        event=dict(message_id='om_synthetic010',sender_id='ou_test',chat_type='p2p',sender_type='user',
                   message_type='text',content='计算区间长度')
        self.assertFalse(self.bridge.store.remember({**event,'sender_id':'ou_other'},'ou_test'))
        self.assertTrue(self.bridge.store.remember(event,'ou_test'))
        self.bridge.process_pending()
        self.wb.outer().drain(limit=4)
        d=self.wb.outer().list('owner')[0]
        self.assertEqual(d['status'],'AWAITING_APPROVAL')
        self.assertFalse(self.wb.store.list())
        transport=Mock()
        sent=[]
        def send_card(body,delivery,card):
            msg='om_card'+str(len(sent))
            sent.append((msg,card))
            return msg
        transport.send_card.side_effect=send_card
        transport.send.return_value='om_receipt'
        self.bridge.flush(transport)
        self.bridge.flush(transport)
        self.assertTrue(sent)
        body={'ticket_id':d['id'],'request_id':'web-confirm','revision':d['revision'],
              'goal_revision':d['goal_revision'],'acceptance_revision':d['acceptance_revision']}
        with self.assertRaises(HTTPError) as denied:self.post('/api/outer/approve',body,False)
        self.assertEqual(denied.exception.code,403)
        result=self.post('/api/outer/approve',body)
        self.assertEqual(result['task_id'],d['id'])
        self.assertTrue(self.post('/api/outer/approve',body)['duplicate'])
        with self.assertRaises(MobileProblem):
            self.mobile.call('outer',command='approve',payload={**body,'request_id':'om_oldclick','thread':sent[-1][0]})
        self.assertEqual(len(self.wb.store.list()),1)
        self.bridge.flush(transport)
        self.assertIsNotNone(self.bridge.store.db.execute(
            'SELECT task FROM watches WHERE task=?',(d['id'],)).fetchone())

    def test_cover_and_explicit_ticket_entry_points(self):
        for route,marker in [('/',b'kingdom-screen'),('/workbench',b'ticket-list'),
                             ('/?goal=example',b'ticket-list'),('/?task=example',b'ticket-list'),
                             ('/globe.js',b'buildGlobe'),('/kingdom_relief.js',b'InstancedMesh')]:
            with self.subTest(route=route), self.client.open(self.base+route) as response:
                self.assertIn(marker,response.read())
        with self.assertRaises(HTTPError) as denied:
            self.client.open(self.base+'/assets/odyssey/sources.json')
        self.assertEqual(denied.exception.code,404)

    def test_directory_pagination_binds_displayed_ticket(self):
        from outer_bridge import directory,respond
        bridge=Mock()
        rows=[{'id':'ticket-'+str(i),'title':'同名目标','status':'待批准'} for i in range(8)]
        bridge.board.call.side_effect=lambda api,command,payload: rows if command=='directory' else {
            'binding':{'ticket_id':payload['ticket_id']},'actions':[],'body':'当前要求'}
        event={'message_id':'om_list'}
        directory(bridge,event)
        first=json.loads(bridge.queue_prompt.call_args.args[2])
        self.assertEqual(len(first['binding']['directory']),6)
        self.assertIn('outer_next',dict(first['actions']))
        event.update(message_type='wb_click',action='outer_next',reply_to='om_parent')
        respond(bridge,event,first)
        page=json.loads(bridge.queue_prompt.call_args.args[2])
        rows.reverse()
        respond(bridge,{**event,'action':'outer_open_1'},page)
        self.assertEqual(bridge.board.call.call_args.kwargs['payload']['ticket_id'],'ticket-7')

    def test_new_cards_and_terminal_permissions(self):
        from outer_channels import card,journey
        ticket={'id':'wb-'+'a'*32,'revision':7,'goal_revision':3,'acceptance_revision':2,
                'status':'AUTHORIZED','execution_status':'completed','task_id':'wb-'+'a'*32}
        view=journey(ticket)
        self.assertEqual(view['station'],'delivery')
        self.assertFalse(any(view[k] for k in ('approve','pause','resume','stop')))
        self.assertNotIn('outer_pause',dict(card(ticket)['actions']))
        stopping={**ticket,'status':'PAUSED','execution_status':'stopping','status_before_pause':'AUTHORIZED'}
        self.assertFalse(journey(stopping)['resume'])
        self.assertFalse(journey(stopping)['stop'])
        ready={**stopping,'execution_status':'paused','status_before_pause':'AWAITING_APPROVAL',
               'approval_terms':{'budget_seconds':600}}
        self.assertIn('outer_approve',dict(card(ready)['actions']))
        ready['goal_contract']={'goal_statement':'长目标'*2000}
        self.assertNotIn('outer_approve',dict(card(ready)['actions']))
        self.assertIn(ready['id'],card(ready)['body'])

    def test_goal_changes_visible_before_approval_in_both_channels(self):
        from outer_channels import card, journey
        goal = dict(goal_statement='交付年度账单', changes={'deliverables':{'removed':['月度账单'],'added':['年度账单']}})
        draft = dict(id='wb-'+'a'*32, revision=7, goal_revision=3, acceptance_revision=2,
                     status='AWAITING_APPROVAL',goal_contract=goal,approval_terms={'budget_seconds':600})
        items = journey(draft)['decision_items']
        self.assertEqual([i['label'] for i in items],['相较目标 v2 移除','相较目标 v2 新增'])
        for item in items:
            self.assertIn(item['text'],card(draft)['body'])
        self.assertIn('outer_approve',dict(card(draft)['actions']))
        goal['goal_statement'] += '长目标'*1500
        self.assertNotIn('outer_approve',dict(card(draft)['actions']))
        self.assertIn('交付物 · 月度账单',card(draft)['body'])
        self.assertFalse(journey(dict(draft,status='AUTHORIZED'))['decision_items'])

    def test_coordination_decision_uses_current_block_and_requires_new_approval(self):
        from outer_channels import card,journey
        proposal = dict(action='REQUEST_APPROVAL',reason='原授权不覆盖第二个数据源',
                        plan_change='请确认是否允许读取备用文件',remaining_risk='替代数据未经核验')
        decisions = [dict(kind='coordinated',proposal=proposal),dict(kind='blocked',reason=proposal['reason'])]
        t = dict(id='wb-'+'a'*32,revision=3,goal_revision=1,acceptance_revision=1,status='AUTHORIZED',
                 execution=dict(status='blocked',version=1,goal_execution=dict(decisions=decisions)))
        view = journey(t)
        self.assertEqual(view['station'], 'decision')
        self.assertEqual(view['instruction'], proposal['reason'])
        self.assertIn(proposal['remaining_risk'],card(t)['body'])
        self.assertIn('修改要求',card(t)['body'])
        self.assertNotIn('outer_approve', dict(card(t)['actions']))
        decisions.append(dict(kind='blocked',reason='另一轮的预算耗尽'))
        self.assertFalse(journey(t)['decision_items'])

    def test_failed_edit_on_delivered_ticket_displays_event_error(self):
        from outer_bridge import mirror_notifications
        binding={'ticket_id':'wb-'+'f'*32,'revision':6,'goal_revision':2,'acceptance_revision':2}
        def call(api,command,payload):
            if command=='notifications':return [{'id':'failed-edit','ticket_id':binding['ticket_id'],'error':'timeout'}]
            if command=='card':return {'title':'已完成','body':'旧版已交付','binding':binding,'actions':[['outer_edit','修改要求']]}
        self.bridge.board=Mock()
        self.bridge.board.call.side_effect=call
        mirror_notifications(self.bridge)
        saved=self.bridge.store.db.execute('SELECT context FROM click_prompts WHERE outbox_id=?',('outer-failed-edit',)).fetchone()
        card=json.loads(saved[0])
        self.assertIn('timeout',card['body'])
        self.assertIn('当前目标 v2',card['body'])
        self.assertEqual(card['binding'],binding)
        self.assertEqual(dict(card['actions']),{'outer_status':'打开现行工单卡'})

    def test_execution_milestone_retains_outer_card_and_cancels_stale_notice(self):
        task='wb-'+'d'*32
        binding={'ticket_id':task,'revision':6,'goal_revision':2,'acceptance_revision':2}
        current={'known':True,'outer':True,'key':'1:completed','body':'旧版提醒'}
        def project(api,**kwargs):
            if api=='milestone':return current
            return {'body':'成果已归档','title':'工单已完成','binding':binding,'actions':[['outer_status','看进度']]}
        with self.bridge.store.db:self.bridge.store.db.execute('INSERT INTO watches(task,generation) VALUES(?,0)',(task,))
        self.mobile.call=Mock(side_effect=project)
        self.bridge.monitor_watches()
        row=self.bridge.store.db.execute("SELECT id,body FROM outbox WHERE id LIKE 'watch-%'").fetchone()
        rendered,actions=self.bridge.button_card(row['id'],row['body'])
        self.assertEqual(actions,['outer_status'])
        self.assertEqual(rendered['header']['title']['content'],'工单已完成')
        current['key']=None
        self.bridge.monitor_watches()
        self.assertEqual(self.bridge.store.db.execute('SELECT state FROM outbox WHERE id=?',(row['id'],)).fetchone()[0],'cancelled')


    def test_signoff_modes_and_download_route(self):
        from outer_channels import card, dispatch
        draft=dict(id='wb-'+'a'*32,revision=7,goal_revision=3,acceptance_revision=2,
                   status='AWAITING_APPROVAL',approval_terms={'budget_seconds':600})
        actions=dict(card(draft)['actions'])
        self.assertIn('outer_approve',actions)
        self.assertIn('outer_approve_signoff',actions)
        draft['acceptance_contract']={'human_review_requirements':['核对术语']}
        required=card(draft)
        self.assertNotIn('outer_approve',dict(required['actions']))
        self.assertIn('须人工签收',required['body'])
        service=Mock()
        payload={**draft,'ticket_id':draft['id'],'request_id':'web-review'}
        dispatch(service,'web','approve',payload)
        self.assertNotIn('require_signoff',service.approve_goal_contract.call_args.kwargs)
        dispatch(service,'web','approve',dict(payload,require_signoff=True))
        self.assertIs(service.approve_goal_contract.call_args.kwargs['require_signoff'],True)
        task, _ = self.wb.store.create({'name':'普通下载','goal':'下载已验报告',
                                        'request_id':'00000000-0000-4000-8000-000000000001'})
        self.wb.artifact_bytes=Mock(return_value=(Path('报告.txt'),b'candidate file'))
        url=self.base+'/api/tasks/'+task['id']+'/artifact?path=%E6%8A%A5%E5%91%8A.txt&candidate=assignment-2'
        with self.client.open(url) as response:
            self.assertEqual(response.read(),b'candidate file')
        self.wb.artifact_bytes.assert_called_once_with(task['id'],'报告.txt',None,candidate='assignment-2')
        with self.assertRaises(HTTPError) as conflict:
            self.client.open(url+'&version=1')
        self.assertEqual(conflict.exception.code,400)

    def test_artifact_candidate_card_uses_original_binding_and_reason(self):
        from outer_bridge import respond
        from outer_channels import card, dispatch, journey
        from task_brief import milestone
        draft=dict(id='wb-'+'a'*32,revision=7,goal_revision=3,acceptance_revision=2,status='AUTHORIZED')
        candidate=dict(sequence=2,assignment='candidate-2',artifact_sha256='a'*64,files={'report.txt':'b'*64},
                       goal_revision=3,acceptance_revision=2,mandate_revision=1,state='pending')
        returned=dict(candidate,sequence=1,assignment='candidate-1',state='returned',reason='请补术语定义')
        draft['execution']={'id':draft['id'],'status':'awaiting_artifact_review','version':1,
                            'observation_stale':False,'progress':{'done':1,'total':1},
                            'goal_execution':{'artifact_reviews':[returned,candidate],
                                              'validation':{'checks':[{'id':'summary','status':'PASS'},
                                                  {'id':'wording','verifier_id':'human-review','status':'INCONCLUSIVE'}]}}}
        self.assertEqual(journey(dict(draft,execution=None,execution_status='awaiting_artifact_review'))['station'],'decision')
        self.assertEqual(journey(draft)['station'],'decision')
        review=card(draft)
        for entry in ('交付 v2','report.txt','机器验收 1/1','请补术语定义',candidate['artifact_sha256']):
            self.assertIn(entry,review['body'])
        self.assertIn('outer_accept_artifact',dict(review['actions']))
        old_key=milestone(draft['execution'])['key']
        another=dict(candidate,sequence=3,assignment='candidate-3')
        changed=dict(draft['execution'],goal_execution={'artifact_reviews':[returned,another]})
        self.assertNotEqual(old_key,milestone(changed)['key'])
        self.assertIsNone(milestone(dict(changed,status='queued'))['key'])

        bridge=Mock()
        service=Mock()
        def call(api, command, payload):
            if command=='bind' and payload['revision']!=draft['revision']:
                raise MobileProblem('工单版本已变化，请重新查看')
            if command=='card':
                return review
            if command!='bind':
                return dispatch(service,'feishu',command,payload)
        bridge.board.call.side_effect=call
        event={'message_id':'om_accept','reply_to':'om_original','message_type':'wb_click','action':'outer_accept_artifact'}
        respond(bridge,event,review)
        service.decide_artifact.assert_called_once_with(draft['id'],'accept',7,'candidate-2','a'*64,'','om_accept',
                                                       actor_id='owner',source='feishu',source_thread_id='om_original')
        service.decide_artifact.reset_mock()
        respond(bridge,{**event,'action':'outer_return_artifact','message_id':'om_start_return'},review)
        reason_card=json.loads(bridge.queue_prompt.call_args.args[2])
        service.decide_artifact.assert_not_called()
        reply={'message_id':'om_reason','reply_to':'om_reason_card','message_type':'text','content':'  '}
        self.assertIn('填写退回原因',respond(bridge,reply,reason_card))
        service.decide_artifact.assert_not_called()
        reply['content']='请说明为何使用旧定义'
        draft['revision']=8
        with self.assertRaisesRegex(MobileProblem,'版本已变化'):
            respond(bridge,reply,reason_card)
        service.decide_artifact.assert_not_called()
        draft['revision']=7
        respond(bridge,reply,reason_card)
        service.decide_artifact.assert_called_once_with(draft['id'],'return',7,'candidate-2','a'*64,reply['content'],'om_reason',
                                                       actor_id='owner',source='feishu',source_thread_id='om_reason_card')
        service.decide_artifact.reset_mock()
        direct={'message_id':'om_direct','message_type':'text','reply_to':'om_original','content':'普通文字'}
        self.assertIn('退回修改：原因',respond(bridge,direct,review))
        service.decide_artifact.assert_not_called()
        direct['content']='退回修改：请补充说明'
        respond(bridge,direct,review)
        service.decide_artifact.assert_called_once_with(draft['id'],'return',7,'candidate-2','a'*64,'请补充说明','om_direct',
                                                       actor_id='owner',source='feishu',source_thread_id='om_original')

    def test_unsigned_mobile_and_legacy_route_declined(self):
        client=LocalBoard(self.mobile.path,b'x'*32)
        with self.assertRaises(RuntimeError):
            client.call('outer',command='submit',payload={'text':'example','request_id':'forged'})
        with self.assertRaises(HTTPError) as denied:self.post('/api/tasks',{'goal':'direct'})
        self.assertEqual(denied.exception.code,409)
        self.assertFalse(self.wb.store.list())

if __name__=='__main__':
    unittest.main()
