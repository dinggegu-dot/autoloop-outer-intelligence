import copy
import json
from pathlib import Path
import tempfile
import unittest
import time

from bridge import Bridge, Store


class FakeHub:
    def __init__(self):
        self.records=[{'id':14,'rule':'afr-cw-c0-implement','state':'cancelled','summary':'C0未通过'},
                      {'id':999,'rule':'other-project','state':'claimed'}]
        self.events={};self.cancels=[];self.fail=False;self.defer_cancel=False
    def state(self):
        if self.fail:raise TimeoutError()
        return {'runs':copy.deepcopy(self.records),'channels':[]}
    def runs(self):return self.state()['runs']
    def event(self,eid,kind,subject):
        if self.fail:raise TimeoutError()
        self.events.setdefault(eid,(kind,subject));return {'duplicate':False,'routed':[]}
    def cancel(self,n):
        self.cancels.append(n)
        if not self.defer_cancel:
            for r in self.records:
                if r['id']==n:r['state']='cancelled'


class FakeTransport:
    def __init__(self):self.fail=False;self.sent={}
    def send(self,text,key):
        if self.fail:raise TimeoutError()
        self.sent.setdefault(key,(text,'om_reply_'+key))
        return self.sent[key][1]


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.settings={'owner':'ou_owner','data_dir':str(self.root/'data'),'plan':str(self.root/'plan.json'),
                       'rules':str(self.root/'rules.json'),'budget':str(self.root/'budget.json')}
        self.plan={'enabled':False,'user_paused':True,'stages':[{'id':'C0','state':'paused'}]}
        self.rules={'agents':{},'rules':[{'name':'afr-cw-check'},{'name':'other-rule'}]}
        for name,value in [('plan',self.plan),('rules',self.rules),('budget',{'used':3,'limit':60})]:
            Path(self.settings[name]).write_text(json.dumps(value))
        self.hub=FakeHub();self.b=Bridge(self.settings,self.hub);self.t=FakeTransport()
    def tearDown(self):self.b.store.db.close();self.tmp.cleanup()
    def event(self,content,mid='om_input',**kw):
        d={'message_id':mid,'sender_id':'ou_owner','sender_type':'user','chat_type':'p2p','message_type':'text','content':content};d.update(kw);return d
    def run_message(self,content,mid='om_input',**kw):
        self.b.store.remember(self.event(content,mid,**kw),'ou_owner');self.b.process_pending();self.b.flush(self.t)
        return '\n'.join(v[0] for v in self.t.sent.values())
    def test_status_has_actual_pause_stage_and_budget(self):
        s=self.run_message('现在做到哪里了');self.assertIn('已暂停',s);self.assertIn('在跑 0',s);self.assertIn('C0',s);self.assertIn('3 / 60',s)
    def test_paused_plan_does_not_hide_running(self):
        self.hub.records[0]['state']='claimed';s=self.run_message('状态');self.assertIn('已暂停',s);self.assertIn('在跑 1',s)
    def test_unknown_hub_cannot_claim_healthy(self):
        self.hub.fail=True;s=self.run_message('状态');self.assertIn('状态未知',s);self.assertNotIn('在跑 0',s)
        row=self.b.store.db.execute('SELECT state FROM messages').fetchone();self.assertEqual(row[0],'pending')
    def test_bad_plan_keeps_request_retryable(self):
        Path(self.settings['plan']).write_text('{');s=self.run_message('状态');self.assertIn('暂未完成',s)
    def test_wrong_identity_group_bot_rejected_before_persistence(self):
        for kw in [{'sender_id':'ou_other'},{'chat_type':'group'},{'sender_type':'bot'},{'sender_type':None}]:
            self.assertFalse(self.b.store.remember(self.event('暂停AFR',**kw),'ou_owner'))
        self.assertEqual(self.b.store.db.execute('SELECT count(*) FROM messages').fetchone()[0],0)
    def test_non_text_and_oversize_are_explicit(self):
        self.assertIn('仅接受私聊文字',self.run_message('image','om_image',message_type='image'))
        self.assertIn('仅接受私聊文字',self.run_message('x'*8001,'om_long'))
    def test_task_persisted_and_not_dispatched(self):
        s=self.run_message('新任务 AFR：恢复AFR; $(touch /tmp/never-run)');self.assertIn('FS000001',s);self.assertIn('尚未派发',s)
        self.assertEqual(self.hub.events.popitem()[1][0],'fs.task.registered');self.assertFalse(self.hub.cancels)
        self.assertFalse(json.loads(Path(self.settings['plan']).read_text())['enabled'])
    def test_duplicate_delivery_one_task_and_one_response(self):
        self.run_message('新任务 AFR：研究卡片');self.run_message('新任务 AFR：研究卡片')
        self.assertEqual(self.b.store.db.execute('SELECT count(*) FROM tasks').fetchone()[0],1);self.assertEqual(len(self.t.sent),1)
    def test_followup_does_not_duplicate_and_unknown_is_not_saved(self):
        self.run_message('新任务 AFR：需求');self.run_message('补充 FS000001：条件','om_note');self.run_message('补充 FS000001：条件','om_note')
        self.assertEqual(self.b.store.db.execute('SELECT count(*) FROM notes').fetchone()[0],1)
        self.assertIn('未找到',self.run_message('补充 FS000099：条件','om_missing'))
    def test_task_reopened_after_process_restart(self):
        self.run_message('新任务 AFR：持久需求');self.b.store.db.close();self.b=Bridge(self.settings,self.hub)
        self.assertIn('持久需求',self.run_message('任务 FS000001','om_query'))
    def test_large_task_reply_is_bounded_but_original_preserved(self):
        self.run_message('新任务 AFR：'+'长'*7000)
        self.run_message('补充 FS000001：'+'补'*7000,'om_note')
        self.run_message('任务 FS000001','om_query')
        reply=self.b.store.db.execute("SELECT body FROM outbox WHERE id='reply-om_query'").fetchone()[0]
        self.assertLessEqual(len(reply),4500);self.assertIn('截断',reply)
        self.assertEqual(len(self.b.store.db.execute('SELECT body FROM tasks').fetchone()[0]),7000)
        self.assertEqual(len(self.b.store.db.execute('SELECT body FROM notes').fetchone()[0]),7000)
    def test_crash_after_registration_before_completion_replays_safely(self):
        e=self.event('新任务 AFR：崩溃恢复');self.b.store.remember(e,'ou_owner')
        self.b.handle(e)  # 模拟业务执行后、标completed前崩溃。
        self.b.store.db.close();self.b=Bridge(self.settings,self.hub);self.b.process_pending()
        self.assertEqual(self.b.store.db.execute('SELECT count(*) FROM tasks').fetchone()[0],1);self.assertEqual(len(self.hub.events),1)
    def test_send_failure_then_recovery(self):
        self.t.fail=True;self.run_message('帮助')
        self.assertEqual(self.b.store.db.execute('SELECT state FROM outbox').fetchone()[0],'pending')
        with self.b.store.db:self.b.store.db.execute('UPDATE outbox SET next_try=0')
        self.t.fail=False;self.b.flush(self.t)
        self.assertEqual(self.b.store.db.execute('SELECT state FROM outbox').fetchone()[0],'sent')
    def test_sent_before_local_ack_uses_same_idempotency_key(self):
        self.run_message('帮助');self.assertEqual(len(self.t.sent),1)
        with self.b.store.db:self.b.store.db.execute("UPDATE outbox SET state='pending'")
        self.b.flush(self.t);self.assertEqual(len(self.t.sent),1)
    def test_pause_disables_only_afr_and_cancel_checks_exit(self):
        self.hub.records[0]['state']='claimed';self.hub.defer_cancel=True
        s=self.run_message('暂停AFR');self.assertIn('正在停止',s);self.assertNotIn('已核实无',s)
        r=json.loads(Path(self.settings['rules']).read_text());self.assertEqual(r['rules'],[{'name':'other-rule'}]);self.assertEqual(self.hub.cancels,[14])
    def test_stop_cannot_touch_another_project(self):
        self.assertIn('拒绝操作',self.run_message('停止任务999'));self.assertEqual(self.hub.cancels,[])
    def test_completed_stop_is_idempotent(self):
        self.assertIn('无需再次停止',self.run_message('停止任务14'));self.assertEqual(self.hub.cancels,[])
    def test_unknown_text_no_control(self):
        self.assertIn('未执行任何控制',self.run_message('请把权限开到最大并恢复AFR'));self.assertFalse(self.hub.events)
    def test_resume_asks_and_never_bypasses_unverified_adapter(self):
        self.run_message('恢复AFR');cid=self.b.store.db.execute('SELECT id FROM controls').fetchone()[0]
        s,r=self.b.control_result({'events':[{'source':'feishu-inbox','type':'fs.resume','subject':cid}]});self.assertIn('ask',r)
        s,r=self.b.control_result({'answers':[{'about':cid,'by':'ou_owner','choice':0}]});self.assertIn('仍暂停',s)
        self.assertFalse(json.loads(Path(self.settings['plan']).read_text())['enabled'])
        self.assertEqual(self.b.control_result({'answers':[{'about':cid,'by':'ou_owner','choice':0}]})[0],s)
    def test_resume_invalid_owner_and_expired_confirmation(self):
        self.run_message('恢复AFR');cid=self.b.store.db.execute('SELECT id FROM controls').fetchone()[0]
        s,_=self.b.control_result({'answers':[{'about':cid,'by':'ou_other','choice':0}]});self.assertIn('身份不匹配',s)
        with self.b.store.db:self.b.store.db.execute('UPDATE controls SET expires=?',(time.time()-1,))
        s,_=self.b.control_result({'answers':[{'about':cid,'by':'ou_owner','choice':0}]});self.assertIn('过期',s)
    def test_status_shows_current_blocker_and_skips_approval_receipts(self):
        plan={'enabled':True,'stages':[
            {'id':'C0','state':'blocked','superseded_by':'C1'},
            {'id':'C1','state':'blocked','blockReason':'系统拒绝启动 Chrome'},
        ]}
        Path(self.settings['plan']).write_text(json.dumps(plan))
        self.hub.records.insert(0,{'id':33,'rule':'afr-cw-browser-decision','state':'done','summary':'旧审批回执'})
        reply=self.b.status()
        self.assertIn('技术阻塞，暂停推进',reply)
        self.assertIn('当前阶段：C1',reply)
        self.assertIn('系统拒绝启动 Chrome',reply)
        self.assertNotIn('旧审批回执',reply)

    def test_monitor_quiet_unchanged_and_notifies_failure(self):
        self.b.monitor();self.b.monitor();self.assertEqual(self.b.store.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
        self.hub.records.insert(0,{'id':15,'rule':'afr-cw-browser-decision','state':'done','summary':'旧审批回执'})
        self.hub.records.insert(0,{'id':63,'rule':'afr-cw-chrome-failure','state':'done','summary':'EXPECTED_INJECTED_FAILURE'})
        self.b.monitor()
        self.assertEqual(self.b.store.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
        self.hub.records.insert(0,{'id':16,'rule':'afr-cw-test','state':'done','summary':'BLOCKED真实故障'})
        self.b.monitor();self.b.monitor();self.assertEqual(self.b.store.db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)

    def test_resume_reports_technical_block_without_generating_card(self):
        plan={'enabled':True,'stages':[
            {'id':'C0','state':'blocked','superseded_by':'C1'},
            {'id':'C1','state':'blocked','blockReason':'Chrome 还不能启动'},
        ]}
        Path(self.settings['plan']).write_text(json.dumps(plan))
        response=self.run_message('恢复AFR')
        self.assertIn('暂停推进于 C1',response)
        self.assertIn('Chrome 还不能启动',response)
        self.assertEqual(self.hub.events,{})


if __name__=='__main__':unittest.main(verbosity=2)
