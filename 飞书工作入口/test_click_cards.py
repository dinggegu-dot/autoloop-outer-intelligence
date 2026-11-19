import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, create_autospec

from bridge import Bridge, LocalBoard


class ClickFlowTest(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings={'owner':'ou_me','data_dir':tmp.name,'workbench_dir':str(Path(__file__).resolve().parents[1]/'网页工作台'),
                       'workbench_runtime':str(Path(tmp.name).resolve()/'board')}
        self.bridge=Bridge(self.settings)
        self.addCleanup(lambda:self.bridge.store.db.close())
        self.transport=Mock()
        self.transport.send_card.side_effect=lambda text,key,card:'om_'+key
        self.transport.send.side_effect=lambda text,key:'om_'+key

    def pump(self):
        self.bridge.process_pending()
        self.bridge.flush(self.transport)
        return self.transport.send_card.call_args.args

    def click(self,tag,parent,action,text='',**fields):
        event=dict(type='card.action.trigger',operator_id='ou_me',host='im_message',chat_id='oc_me',
                   event_id=tag,message_id=parent,action_tag='button',action_value=json.dumps({'wb_action':action}),
                   form_value=json.dumps({'requirement':text}))
        return self.bridge.store.remember_click(dict(event,**fields),'ou_me')

    def say(self,mid,content):
        self.bridge.store.remember(dict(message_id=mid,sender_type='user',sender_id='ou_me',chat_type='p2p',
                                       chat_id='oc_me',message_type='text',content=content),'ou_me')
        _,key,card=self.pump()
        return 'om_'+key,card

    def test_requirements_only_then_buttons_and_form(self):
        anchor,card=self.say('om_goal','分组统计资料')
        self.assertEqual(card['schema'],'2.0')
        self.assertEqual(self.bridge.board.list(),[])
        self.assertFalse(self.click('outside',anchor,'create_here',chat_id='oc_other'))
        self.assertFalse(self.click('forward','om_forward','create_here'))
        self.assertFalse(self.click('approve',anchor,'approve'))
        self.assertFalse(self.click('imposter',anchor,'create_here',operator_id='ou_other'))
        self.assertTrue(self.click('choose',anchor,'create_here'))
        self.assertFalse(self.click('choose',anchor,'create_here'))
        _,key,card=self.pump()
        task_ref='om_'+key
        self.assertIn('看进度',json.dumps(card,ensure_ascii=False))
        self.assertTrue(self.click('duplicate',anchor,'attach_here'))
        self.pump()
        self.assertEqual(len(self.bridge.board.list()),1)
        self.assertTrue(self.click('go_home',task_ref,'home'))
        _,key,_=self.pump()
        home_ref='om_'+key
        self.assertTrue(self.click('list',home_ref,'list'))
        _,key,menu=self.pump()
        self.assertIn('分组统计资料',json.dumps(menu,ensure_ascii=False))
        self.assertTrue(self.click('pick','om_'+key,'pick_1'))
        _,key,_=self.pump()
        self.assertTrue(self.click('add','om_'+key,'add'))
        _,key,form=self.pump()
        self.assertIn('form_action_type',json.dumps(form))
        self.assertTrue(self.click('save','om_'+key,'submit_note','按日期分组'))
        self.assertTrue(self.click('save_twice','om_'+key,'submit_note','另一内容'))
        self.pump()
        task=self.bridge.board.list()[0]
        self.assertEqual(task['calls'],0)
        self.assertEqual([n['body'] for n in self.bridge.board.notes(task['id'])],['按日期分组'])
        self.assertTrue(self.click('open_new',home_ref,'new'))
        _,key,_=self.pump()
        self.assertTrue(self.click('second','om_'+key,'submit_goal','核对报表'))
        self.pump()
        self.assertEqual({t['goal'] for t in self.bridge.board.list()},{'分组统计资料','核对报表'})

    def test_typed_progress_opens_readonly_choice(self):
        original,_=self.say('om_new','/new 整理资料')
        menu,card=self.say('om_query','看进度')
        self.assertIn('选择工单',json.dumps(card,ensure_ascii=False))
        self.assertTrue(self.click('read',menu,'pick_1'))
        _,_,result=self.pump()
        self.assertIn('created',json.dumps(result,ensure_ascii=False))
        self.bridge.store.remember(dict(message_id='om_bound_query',sender_type='user',sender_id='ou_me',
                                       chat_type='p2p',chat_id='oc_me',message_type='text',
                                       reply_to=original,content='看进度'),'ou_me')
        self.pump()
        self.assertIn('created',self.transport.send.call_args.args[0])
        task,=self.bridge.board.list()
        self.assertEqual(self.bridge.board.notes(task['id']),[])
        self.assertEqual(task['calls'],0)

    def test_first_handoff_click_and_decision_binding(self):
        client=create_autospec(LocalBoard('/unused', b'test'),spec_set=True)
        client.decision_key=b'test'
        client.pending_handoffs.return_value=[]
        self.bridge.board=client
        tid='wb-'+'1'*32
        binding={'task':tid,'task_version':1,'card':11,'version':1,'approval':'a'}
        def respond(action, **fields):
            if action=='controls':return {'message':'无可用控制'}
            if action=='proposal':return {'body':'当前合成方案','binding':binding}
            if action=='answer':return '决定已送达'
            self.fail(action)
        client.call.side_effect=respond
        self.bridge.queue_card('handoff-test',tid)
        _,key,_=self.pump()
        original='om_'+key
        self.assertFalse(self.click('fake','om_forward','approve'))
        self.assertIsNone(self.bridge.store.meta('owner_p2p_chat'))
        self.assertFalse(self.click('wrong',original,'approve'))
        self.assertTrue(self.click('show',original,'proposal'))
        self.assertEqual(self.bridge.store.meta('owner_p2p_chat'),'oc_me')
        _,key,decision=self.pump()
        self.assertIn('确认执行',json.dumps(decision,ensure_ascii=False))
        self.assertFalse(self.click('bad','om_'+key,'resume'))
        self.assertTrue(self.click('approve_decision','om_'+key,'approve'))
        self.assertFalse(self.click('approve_decision','om_'+key,'approve'))
        self.pump()
        answer,=[entry for entry in client.call.call_args_list if entry.args==('answer',)]
        name,fields=answer
        self.assertEqual((name,fields['task'],fields['binding'],fields['choice'],fields['parent']),
                         (('answer',),tid,binding,0,'om_'+key))

    def test_fixed_home_survives_restart_and_can_be_used_repeatedly(self):
        self.assertEqual(self.bridge.enqueue_home(),'workbench-home-v1')
        self.bridge.enqueue_home()
        _,key,card=self.pump()
        self.assertFalse(card['config']['enable_forward'])
        self.assertEqual(card['header']['title']['content'],'工作台')
        original='om_'+key
        self.bridge.store.db.close()
        self.bridge=Bridge(self.settings)
        self.bridge.enqueue_home()
        self.pump()
        self.assertEqual(self.transport.send_card.call_count,1)
        for index in range(2):
            self.assertTrue(self.click('home_new_'+str(index),original,'new'))
            _,_,form=self.pump()
            self.assertEqual(form['header']['title']['content'],'新建工单')
        self.assertFalse(self.click('cross_chat',original,'new',chat_id='oc_other'))
        home_ref,home=self.say('om_home','工作台')
        self.assertNotEqual(home_ref,original)
        self.assertEqual(home,card)
        _,form=self.say('om_new_form','新建工单')
        self.assertEqual(form['header']['title']['content'],'新建工单')
        self.assertEqual(self.bridge.board.list(),[])

    def test_preflight_retry_and_stale_controls(self):
        task='wb-'+'2'*32
        client=create_autospec(LocalBoard('/unused', b'test'),spec_set=True)
        client.decision_key=b'test'
        client.pending_handoffs.return_value=[]
        self.bridge.board=client
        responses={'proposal':{'message':'无待审方案'},
                   'controls':{'body':'合成控制卡','binding':{'task':task,'operation':'pause'}}}
        client.call.side_effect=lambda action,**kwargs:responses[action]
        self.bridge.queue_card('changing',task)
        self.transport.send_card.side_effect=TimeoutError
        self.bridge.flush(self.transport)
        first=self.transport.send_card.call_args.args
        self.assertIn('view_pause',json.dumps(first[2]))
        responses['controls']['binding']['operation']='resume'
        self.transport.send_card.side_effect=lambda text,key,card:'om_'+key
        with self.bridge.store.db:
            self.bridge.store.db.execute('UPDATE outbox SET next_try=0')
        _,key,_=self.pump()
        self.assertEqual(self.transport.send_card.call_args.args,first)
        self.assertTrue(self.click('stale','om_'+key,'view_pause'))
        _,key,card=self.pump()
        self.assertIn('状态已变化',json.dumps(card,ensure_ascii=False))
        self.assertIn('view_resume',json.dumps(card))
        self.assertNotIn('view_pause',json.dumps(card))
        self.assertTrue(self.click('fresh','om_'+key,'view_resume'))
        _,_,confirmation=self.pump()
        self.assertEqual(confirmation['header']['title']['content'],'工单控制')
        self.assertEqual({entry.args[0] for entry in client.call.call_args_list},{'controls','proposal'})

    def test_preflight_fails_closed_and_legacy_card_remains_usable(self):
        task='wb-'+'3'*32
        client=create_autospec(LocalBoard('/unused'),spec_set=True)
        client.pending_handoffs.return_value=[]
        self.bridge.board=client
        for tag, key, failure in [('disabled',None,None),('unknown',b'test',None),
                                  ('offline',b'test',TimeoutError)]:
            with self.subTest(tag=tag):
                client.decision_key=key
                client.call.return_value={'message':'状态待核对'}
                client.call.side_effect=failure
                client.call.reset_mock()
                self.bridge.queue_card(tag,task)
                _,delivery,card=self.pump()
                shown=self.bridge.store.db.execute('SELECT actions FROM shown_actions WHERE outbox_id=?',(tag,)).fetchone()
                self.assertEqual(set(json.loads(shown[0])),{'status','add','watch','board','home'})
                self.assertIn('看进度',json.dumps(card,ensure_ascii=False))
                self.assertFalse(self.click('invented_'+tag,'om_'+delivery,'controls'))
                if key is None:client.call.assert_not_called()
        # 已投递旧卡仍按当时实际展示的动作授权，点击后重查当前状态。
        with self.bridge.store.db:
            self.bridge.store.db.execute('UPDATE shown_actions SET actions=? WHERE outbox_id=?',
                                         (json.dumps(['controls','new','list']),'offline'))
        client.call.side_effect=None
        self.assertTrue(self.click('old_controls','om_'+delivery,'controls'))
        _,_,updated=self.pump()
        self.assertIn('状态待核对',json.dumps(updated,ensure_ascii=False))
        self.assertFalse(self.click('old_unshown','om_'+delivery,'approve'))

    def test_shared_listener_preserves_original_stream_and_minimizes_workbench_copy(self):
        import shutil
        folder=Path(self.settings['data_dir'])
        ref,_=self.say('om_one','整理报表')
        old={'type':'card.action.trigger','action_value':'{"card":32,"choice":1}','operator_id':'ou_me'}
        wb=dict(type='card.action.trigger',host='im_message',operator_id='ou_me',chat_id='oc_me',
                event_id='ev_example',message_id=ref,action_tag='button',action_value='{"wb_action":"create_here"}',
                card_content='excluded_card_body',token='excluded_token')
        stream=''.join(json.dumps(e)+'\n' for e in (old,wb))
        cli=folder/'sample_cli'
        cli.write_text('#!'+sys.executable+'\nimport sys\nsys.stdout.write('+repr(stream)+')\n')
        cli.chmod(0o700)
        (folder/'settings.json').write_text(json.dumps(dict(self.settings,lark=str(cli))))
        source=Path(__file__).resolve().parent
        for name in ('shared_clicks.py','bridge.py','click_cards.py'):shutil.copyfile(source/name,folder/name)
        result=subprocess.run([sys.executable,'-B',str(folder/'shared_clicks.py'),'event','consume','card.action.trigger','--as','bot'],
                              capture_output=True,text=True,check=True,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(result.stdout,stream)
        queued=self.bridge.store.db.execute('SELECT body FROM shared_clicks').fetchall()
        self.assertEqual(len(queued),1)
        self.assertNotIn('excluded',queued[0][0])
        self.bridge.store.drain_clicks('ou_me');self.pump()
        self.assertEqual(self.bridge.board.list()[0]['goal'],'整理报表')
