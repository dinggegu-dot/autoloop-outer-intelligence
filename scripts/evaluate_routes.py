"""用合成工单和真实接单模型复跑五条已标注消息。只评估路由，不执行任务。"""
import json
from pathlib import Path
import sys
import tempfile

BOARD = Path(__file__).resolve().parents[1] / '网页工作台'
sys.path.insert(0, str(BOARD))
from superagent.outer_intake import OuterIntake
from superagent.outer_model import CodexOuterModel
from superagent.test_outer_intake import Model, Registry
from workbench import Store, Workbench

# 目标关联仅由接单系统的来源绑定和选择结果决定。
CASES = [
    ('独立且完整', '生成 report.json，内容为 {"ok":true}。', False, 'CREATE_DRAFT', False),
    ('关键歧义', '需要交付报告，必须满足采购验收的那种文件格式，我还没告诉你是哪种格式。', False, 'CREATE_DRAFT', True),
    ('两单归属不明', '把上次那份报告的交付日期改到周五。', False, 'REQUEST_TICKET_SELECTION', False),
    ('原单查状态', '这张工单目前做到哪一步了？', True, 'ANSWER_STATUS', False),
    ('原单改范围', '更改交付范围，把月报改为每周的报告。', True, 'PROPOSE_CHANGE', False),
]


def evaluate(case):
    name, message, bound, expected, needs_question = case
    with tempfile.TemporaryDirectory(prefix='autoloop-route-') as temp:
        store = Store(Path(temp).resolve())
        # 两个既有目标通过确定性替身建立，只有本条待评的输入使用真实模型。
        board = Workbench(store, None, goal_enabled=True, outer_enabled=True)
        service = OuterIntake(board, model=Model(), registry=Registry())
        for number, goal in enumerate(('生成月度采购报告', '生成季度采购报告')):
            service.submit(source='web', source_event_id='seed-'+str(number), actor_id='owner', text=goal)
            service.drain(limit=2)
        candidates=service.list('owner')
        target=next(row for row in candidates if '月度' in row['goal_contract']['goal_statement'])
        service = OuterIntake(board, model=CodexOuterModel(timeout=60))
        binding = {'ticket_id':target['id'], 'expected_ticket_revision':target['revision']} if bound else {}
        event = service.submit(source='web', source_event_id='sample', actor_id='owner', text=message, **binding)
        service.drain(limit=1)
        with store.connect() as db:
            row=db.execute('SELECT state,result FROM outer_invocations WHERE event=?',(event['event_id'],)).fetchone()
        record=json.loads(row['result'])
        proposal=record.get('result',{})
        action=proposal.get('suggested_action')
        asked=bool(proposal.get('proposed_question',{}).get('question_text','').strip())
        settled=service.event(event['event_id'],'owner')
        existing={item['id'] for item in candidates}
        wrong=bool(not bound and settled.get('ticket_id') in existing)
        return dict(sample=name,expected=expected,actual=action,accepted=row['state']=='accepted',
                    route_correct=row['state']=='accepted' and action==expected,
                    expected_question=needs_question,proposed_question=asked,
                    wrong_automatic_attachment=wrong,usage=record.get('usage',{}),
                    elapsed_ms=record.get('elapsed_ms'))


if __name__ == '__main__':
    for sample in CASES:
        print(json.dumps(evaluate(sample), ensure_ascii=False),flush=True)
