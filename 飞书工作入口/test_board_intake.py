import json
import sys
import tempfile
import unittest
import uuid
import threading
from pathlib import Path
from unittest.mock import Mock

from bridge import Bridge, LocalBoard

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'网页工作台'))
from workbench import Store, Workbench
from private_intake import serve_private


class BoardIntakeTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings = {'owner':'ou_me','data_dir':temp.name,'workbench_dir':str(Path(__file__).resolve().parents[1]/'网页工作台'),
                         'workbench_runtime':str(Path(temp.name).resolve()/'board')}
        self.bridge = Bridge(self.settings)
        self.addCleanup(lambda:self.bridge.store.db.close())
        self.board = self.bridge.board
        self.transport = Mock()
        self.transport.send_card.side_effect = lambda text,key,card:'om_'+key
        self.transport.send.side_effect = lambda text,key:'om_'+key

    def deliver(self, mid, text, **fields):
        event = dict(message_id=mid,content=text,sender_type='user',chat_type='p2p',sender_id='ou_me',message_type='text')
        self.bridge.store.remember(dict(event,**fields),'ou_me')
        self.bridge.process_pending()
        self.bridge.flush(self.transport)

    def test_slash_shortcuts_bind_only_explicit_owner_intent(self):
        def reference(mid):
            return self.bridge.store.db.execute('SELECT ref FROM outbox WHERE id=?',('reply-'+mid,)).fetchone()[0]
        self.deliver('om_foreign','/new 越权',sender_id='ou_else')
        self.deliver('om_group','/new 群聊',chat_type='group')
        self.deliver('om_open','/new')
        prompt=reference('om_open')
        self.deliver('om_plain','普通消息不能成为新任务')
        self.deliver('om_root','不是直接引用',root_id=prompt)
        self.assertEqual(self.board.list(),[])
        self.bridge.store.db.close()
        self.bridge=Bridge(self.settings)
        self.deliver('om_first','整理示例表',reply_to=prompt)
        self.deliver('om_first','整理示例表',reply_to=prompt)
        self.deliver('om_next','/new 比较 A 和 B',message_type='post')
        self.deliver('om_follow','按日期分组',reply_to=reference('om_first'))
        self.deliver('om_replyslash','/new 另一张',reply_to=reference('om_first'))
        tasks={t['goal']:t for t in self.board.list()}
        self.assertEqual(set(tasks),{'整理示例表','比较 A 和 B'})
        self.assertEqual([n['body'] for n in self.board.notes(tasks['整理示例表']['id'])],['按日期分组'])
        self.assertTrue(all(t['status']=='created' and t['calls']==0 for t in tasks.values()))
        self.deliver('om_list','/tasks')
        menu=self.transport.send_card.call_args.args[0]
        for goal in tasks:self.assertIn(goal,menu)

    def test_restart_followup_and_switch_both_ways(self):
        self.deliver('om_create','新任务：研究索引')
        self.deliver('om_other','新任务：其他工单')
        task = next(task for task in self.board.list() if task['goal']=='研究索引')
        anchor = self.bridge.store.db.execute("SELECT ref FROM outbox WHERE id='reply-om_create'").fetchone()[0]
        self.bridge.store.db.close()
        self.bridge = Bridge(self.settings)
        self.deliver('om_follow','按行业分类',reply_to=anchor)
        self.deliver('om_follow','按行业分类',reply_to=anchor)
        self.deliver('om_link','切到看板',reply_to=anchor)
        self.assertIn('?task='+task['id']+'#tasks', self.transport.send.call_args.args[0])
        hub=Mock()
        hub.records.return_value=([],[])
        hub.state.return_value={'agents':[],'lines':[]}
        snapshot=Workbench(Store(Path(self.settings['workbench_runtime'])),hub).snapshot(task['id'])
        chosen=next(row for row in snapshot['tasks'] if row['id']==task['id'])
        self.assertEqual([note['body'] for note in chosen['inbox_notes']],['按行业分类'])
        self.assertEqual(chosen['status'],'created')
        self.assertEqual(chosen['source'],'feishu')
        self.assertEqual(len(snapshot['tasks']),2)
        self.assertTrue(all('研究索引' not in call.args[0] for call in self.transport.send_card.call_args_list))
        requests=[{'request_id':str(uuid.uuid4())} for _ in range(2)]
        for request in requests:self.board.handoff(task['id'],request)
        self.bridge.flush(self.transport)
        count=self.transport.send_card.call_count
        self.assertEqual(self.board.handoff(task['id'],requests[1]),{'state':'sent'})
        self.bridge.flush(self.transport)
        self.assertEqual(self.transport.send_card.call_count,count)
        anchor=self.bridge.store.db.execute("SELECT ref FROM outbox WHERE id LIKE 'handoff-%'").fetchone()[0]
        self.deliver('om_mobile','手机接续',reply_to=anchor)
        self.assertEqual(self.board.notes(task['id'])[-1]['body'],'手机接续')

    def test_ambiguous_and_untrusted_input_has_no_effect(self):
        self.deliver('om_pause','暂停任务')
        self.assertIn('哪项任务',self.transport.send_card.call_args.args[0])
        self.assertIsNone(self.bridge.store.meta('pause_pending'))
        self.deliver('om_bad','新任务：伪造',sender_id='ou_else')
        self.deliver('om_group','新任务：群聊',chat_type='group')
        self.deliver('om_ref','暂停AFR',reply_to='om_unbound')
        self.deliver('om_root','新任务：只在话题里',root_id='om_rootonly')
        self.assertEqual(self.board.list(),[])
        self.assertIsNone(self.bridge.store.meta('pause_pending'))
        self.assertEqual(self.bridge.store.db.execute("SELECT count(*) FROM messages WHERE id IN ('om_bad','om_group')").fetchone()[0],0)

    def test_followup_menus_are_bound_once_and_never_dispatch(self):
        def menu_ref(mid):
            return self.bridge.store.db.execute('SELECT ref FROM outbox WHERE id=?',('reply-'+mid,)).fetchone()[0]
        self.deliver('om_first','新任务：第一单')
        self.deliver('om_second','新任务：第二单')
        first,second=sorted(self.board.list(),key=lambda t:t['goal'])
        self.deliver('om_a','补充：只加到第一单')
        self.deliver('om_b','暂停任务')
        parent=menu_ref('om_a')
        self.assertIn('1. 第二单',self.transport.send_card.call_args.args[0])
        self.deliver('om_untrusted','选 1',reply_to=parent,sender_id='ou_else')
        self.deliver('om_wrong','选 1',reply_to='om_foreign')
        self.deliver('om_root','选 1',root_id=parent)
        self.deliver('om_invalid','选 8',reply_to=parent)
        self.assertEqual(self.board.notes(second['id']),[])
        self.bridge.store.db.close()
        self.bridge=Bridge(self.settings)
        self.deliver('om_choose_b','选1',reply_to=menu_ref('om_b'))
        self.assertIsNone(self.bridge.store.meta('pause_pending'))
        self.deliver('om_choose_a','选 2',reply_to=parent)
        self.assertIn('当前归属',self.transport.send.call_args.args[0])
        self.assertIsNone(self.bridge.store.db.execute("SELECT 1 FROM task_cards WHERE outbox_id='reply-om_choose_a'").fetchone())
        self.deliver('om_replay','选 1',reply_to=parent)
        self.assertEqual([r['body'] for r in self.board.notes(first['id'])],['只加到第一单'])
        self.assertEqual(self.board.notes(second['id']),[])
        self.assertEqual({t['status'] for t in self.board.list()},{'created'})
        self.deliver('om_c','补充：过期')
        with self.bridge.store.db:self.bridge.store.db.execute('UPDATE routing_choices SET expires=0')
        self.deliver('om_old','选 1',reply_to=menu_ref('om_c'))
        self.assertIn('过期',self.transport.send.call_args.args[0])

    def test_account_change_cannot_adopt_pending_cards_or_old_inbox(self):
        self.deliver('om_task','新任务：仅本人可见')
        changed=dict(self.settings,owner='ou_second')
        with self.assertRaises(ValueError):Bridge(changed)
        with self.bridge.store.db:self.bridge.store.db.execute("DELETE FROM meta WHERE key='inbox_owner'")
        with self.assertRaises(ValueError):Bridge(changed)
        self.bridge.store.bind_owner(self.settings['owner'])

    def test_watch_sends_only_current_opted_in_milestones(self):
        from unittest.mock import create_autospec
        client=create_autospec(LocalBoard,instance=True)
        client.pending_handoffs.return_value=[]
        self.bridge.board=client
        task_id='wb-'+'9'*32
        self.bridge.queue_card('test-anchor',task_id)
        self.bridge.flush(self.transport)
        anchor='om_'+self.transport.send_card.call_args.args[1]
        client.call.return_value={'known':True,'key':'1:blocked','body':'需本人处理'}
        self.bridge.monitor_watches()
        self.assertEqual(self.transport.send_card.call_count,1)
        self.deliver('om_sub','订阅提醒',reply_to=anchor)
        self.bridge.monitor_watches()
        client.call.return_value={'known':True,'key':None}
        self.bridge.flush(self.transport)
        self.assertEqual(self.transport.send_card.call_count,1)
        self.bridge.monitor_watches()
        client.call.return_value={'known':True,'key':'1:completed','body':'验收通过'}
        self.bridge.monitor_watches()
        self.bridge.flush(self.transport)
        self.bridge.monitor_watches()
        self.bridge.flush(self.transport)
        self.assertEqual(self.transport.send_card.call_count,2)
        self.deliver('om_unsub','关闭提醒',reply_to=anchor)
        client.call.return_value={'known':True,'key':'2:blocked','body':'需本人处理'}
        self.bridge.monitor_watches()
        self.bridge.flush(self.transport)
        self.assertEqual(self.transport.send_card.call_count,2)

    def test_socket_returns_only_minimal_data(self):
        path=Path(self.settings['data_dir']).resolve()/'board.sock'
        server=serve_private(path,self.board)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.bridge.board=LocalBoard(str(path))
        self.deliver('om_local','新任务：私有传输')
        task=self.board.list()[0]
        anchor=self.transport.send_card.call_args.args[1]
        self.deliver('om_note','只在本机可见',reply_to='om_'+anchor)
        self.assertEqual(self.board.notes(task['id'])[0]['body'],'只在本机可见')
        self.assertEqual(set(self.bridge.board.get(task['id'])),{'status','version'})
        with self.assertRaises(RuntimeError):self.bridge.board.call('artifact',task=task['id'])

    def test_only_reply_to_presented_decision_reaches_controller(self):
        path=Path(self.settings['data_dir']).resolve()/'decide.sock'
        hub=Mock()
        hub.state.return_value={'agents':[],'lines':[]}
        hub.records.return_value=([],[])
        wb=Workbench(self.board,hub)
        key=b'x'*32
        server=serve_private(path,self.board,wb,threading.Lock(),key)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.bridge.board=LocalBoard(str(path),key)
        self.deliver('om_start','新任务：隔离验收')
        anchor='om_'+self.transport.send_card.call_args.args[1]
        task=self.board.list()[0]
        self.board.change(task['id'],lambda data:data.update(
            status='awaiting_approval',approval={'id':'test','kind':'plan','state':'open','version':1},
            plan={'steps':[{'title':'试算','objective':'样例','verification':'算术检查'}],
                  'scope':'临时目录','summary':'试验','assumptions':[]}))
        card={'id':6,'version':1,'state':'open','options':['按这个方案执行','暂不执行'],'text':'限定目录',
              'lane':task['id'],'binding':{'task':task['id'],'version':1,'approval':'test'}}
        hub.records.return_value=([],[card])
        self.deliver('om_try','确认执行',reply_to=anchor)
        hub.request.assert_not_called()
        self.deliver('om_read','查看方案',reply_to=anchor)
        self.assertIn('算术检查',self.transport.send_card.call_args.args[0])
        decision='om_'+self.transport.send_card.call_args.args[1]
        self.deliver('om_ambig','确认执行？',reply_to=decision)
        self.assertFalse(self.board.notes(task['id']))
        hub.request.assert_not_called()
        hub.request.return_value={'changed':True}
        self.deliver('om_answer','确认执行',reply_to=decision)
        binding=json.loads(self.bridge.store.db.execute('SELECT binding FROM decision_cards').fetchone()[0])
        replay=dict(task=task['id'],binding=binding,choice=0,parent=decision,event='om_answer')
        self.assertIn('决定已送达',self.bridge.board.call('answer',**replay))
        with self.assertRaises(RuntimeError):self.bridge.board.call('answer',**{**replay,'choice':1})
        with self.assertRaises(RuntimeError):LocalBoard(str(path),b'wrong').call('answer',**replay)
        hub.request.assert_called_once_with('POST','/v1/cards/6/answer',{'choice':0})
