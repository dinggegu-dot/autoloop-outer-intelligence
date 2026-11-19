import copy
import unittest
from unittest.mock import Mock

from mobile_decision import answer, proposal, control, control_card


class MobileDecisionTest(unittest.TestCase):
    def setUp(self):
        self.task_id='wb-'+'a'*32
        binding={'task':self.task_id,'version':2,'approval':'plan1'}
        self.card={'id':12,'version':5,'state':'open','binding':binding,'choice':None,
                   'text':'仅在工作区内运行','options':['按这个方案执行','暂不执行']}
        self.task={'id':self.task_id,'name':'测试','version':2,'goal':'按行业整理','acceptance':'与清单一致',
                   'status':'awaiting_approval','observation_stale':False,'progress':{'done':0,'total':1},
                   'approval':{'id':'plan1','kind':'plan'},'cards':[self.card],
                   'plan':{'summary':'汇总','scope':'本机','assumptions':[],
                           'steps':[{'title':'整理','objective':'按行业','verification':'逐行对比'}]}}
        self.board=Mock()
        self.board.snapshot.return_value={'tasks':[self.task]}
        self.board.answer.return_value={'recorded':True,'duplicate':False}

    def test_display_and_submit(self):
        card=proposal(self.task)
        self.assertIn('逐行对比',card['body'])
        self.assertIn('决定已送达',answer(self.board,self.task_id,card['binding'],1))
        self.board.answer.assert_called_once_with(self.task_id,{'card':12,'version':5,'choice':1})

    def test_replaced_content_cannot_be_approved_even_without_version_bump(self):
        binding=proposal(self.task)['binding']
        self.task['plan']['scope']='不同范围'
        self.assertIn('过期',answer(self.board,self.task_id,binding,0))
        self.board.answer.assert_not_called()

    def test_closed_card_cannot_replace_web_decision(self):
        binding=proposal(self.task)['binding']
        self.card.update(state='decided',choice=0)
        self.assertIn('未重复提交',answer(self.board,self.task_id,binding,0))
        self.assertIn('已处理',answer(self.board,self.task_id,binding,1))
        self.board.answer.assert_not_called()

    def test_same_version_budget_change_invalidates_decision(self):
        binding=proposal(self.task)['binding']
        self.task['time_budget_seconds']=5400
        self.assertIn('过期',answer(self.board,self.task_id,binding,0))
        self.board.answer.assert_not_called()

    def test_control_stale_and_pause(self):
        binding=control_card(self.task)['binding']
        self.task['version']+=1
        self.assertIn('过期',control(self.board,self.task_id,binding,'om_x'))
        self.board.pause.assert_not_called()
        self.board.pause.return_value={'stopping':True}
        self.assertIn('等待实际停止',control(self.board,self.task_id,control_card(self.task)['binding'],'om_y'))
        self.board.pause.assert_called_once_with(self.task_id)

    def test_resume_requests_no_increase_or_approval(self):
        self.task.update(status='paused',paused=True)
        self.board.resume.return_value={'queued':True}
        binding=control_card(self.task)['binding']
        self.assertEqual(binding['operation'],'resume')
        self.assertIn('已接单',control(self.board,self.task_id,binding,'om_z'))
        self.assertEqual(self.board.resume.call_args.args[0],self.task_id)
        self.assertEqual(set(self.board.resume.call_args.args[1]),{'request_id'})
        self.board.answer.assert_not_called()

    def test_pending_goal_stop_offers_status_without_a_second_pause(self):
        self.task.update(workflow='goal',status='stopping',goal_actions={'pause':True})
        card=control_card(self.task)
        self.assertNotIn('binding',card)
        self.assertIn('看进度',card['message'])

    def test_unsafe_or_ambiguous_state_disables_mobile_approval(self):
        for change in [{'paused':True},{'revision_pending':{'state':'waiting_stop'}},{'observation_stale':True},
                       {'goal':'长'*4200},{'cards':[self.card,copy.deepcopy(self.card)]},{'approval':{'kind':'review'}}]:
            with self.subTest(change=list(change)):
                self.assertNotIn('binding',proposal({**self.task,**change}))
