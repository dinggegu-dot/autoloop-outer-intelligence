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

    def test_unsigned_mobile_and_legacy_route_declined(self):
        client=LocalBoard(self.mobile.path,b'x'*32)
        with self.assertRaises(RuntimeError):
            client.call('outer',command='submit',payload={'text':'example','request_id':'forged'})
        with self.assertRaises(HTTPError) as denied:self.post('/api/tasks',{'goal':'direct'})
        self.assertEqual(denied.exception.code,409)
        self.assertFalse(self.wb.store.list())

if __name__=='__main__':
    unittest.main()
