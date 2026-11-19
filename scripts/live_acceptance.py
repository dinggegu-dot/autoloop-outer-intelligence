"""受控真实模型验收。使用独立 Hub 回环规则，输入和用户审批均为合成业务测试身份。"""
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'网页工作台'))
from workbench import Store, Hub, Workbench
from superagent.goal_runtime import GoalRuntime
from superagent.outer_model import CodexOuterModel

REQUEST = ('做一个会议排期工具 intervals.py。命令行的第一个参数是若干分钟区间组成的 JSON 数组，'
           '输出这些区间去重覆盖的分钟数。参数 [[0,30],[20,40],[60,75]] 返回 {"minutes":55}，'
           '参数 [[5,8],[6,7]] 返回 {"minutes":3}，空数组返回 {"minutes":0}。')
CASES = {'[[0,30],[20,40],[60,75]]':55, '[[5,8],[6,7]]':3, '[]':0}

def main():
    directory = Path(os.environ['AUTOLOOP_RUNTIME']).resolve()
    hub_home = Path(os.environ['AUTOLOOP_HUB']).resolve()
    if not (directory.parent==hub_home.parent and (directory.parent/'.goal-pilot').is_file()
            and directory.name=='workbench' and hub_home.name=='hub' and directory.parent.parent==Path('/private/tmp')):
        raise SystemExit('需要 isolated_goal_hub.sh 新建的独立测试实例')
    store = Store(directory)
    hub = Hub(hub_home)
    outer = Workbench(store,hub,goal_enabled=True,outer_enabled=True,outer_model=CodexOuterModel(timeout=100)).outer()
    if sys.argv[1:] == ['--resume-review']:
        tickets=outer.list('owner')
        if len(tickets)!=1:
            raise SystemExit('恢复需要原隔离库中的唯一合成工单')
        draft=tickets[0]
        task=store.get(draft['id'])
        with store.connect() as db:
            event=db.execute("SELECT body FROM outer_events WHERE id=?", (draft['original_request_refs'][0],)).fetchone()
        if (not event or json.loads(event[0]).get('text')!=REQUEST or task['status']!='blocked'
                or task['phase']!='review' or task['goal_execution']['stage']!='review'
                or '验收结果绑定' not in task.get('error','')):
            raise SystemExit('只可复验已批准且因运行环境记录差异受阻的这张合成工单')
        for control in ('pause','resume'):
            current=outer.get(draft['id'],'owner')
            outer.control(draft['id'],control,current['revision'],'synthetic-review-recovery-'+control,actor_id='owner')
    elif not sys.argv[1:]:
        if store.list() or outer.list('owner'):
            raise SystemExit('新建测试需要空工单库')
        draft=create_and_approve(outer)
    else:
        raise SystemExit('用法：live_acceptance.py [--resume-review]')
    runtime=GoalRuntime(store,hub)
    deadline=time.monotonic()+540
    while time.monotonic()<deadline:
        task=runtime.tick(draft['id'])
        current=(task['status'],task['phase'],task['step'],len(task['goal_execution']['assignments']))
        if current!=getattr(main,'previous',None):
            print('execution',current,flush=True)
            main.previous=current
        if task['status']=='completed':
            print('verified',len(task['checks']), 'criteria,',len(task['artifacts']),'artifacts',flush=True)
            return
        if task['status'] in ('blocked','budget_exhausted','paused','rejected'):
            raise SystemExit('执行受阻：'+task.get('error',''))
        time.sleep(1)
    outer.control(draft['id'],'stop',outer.get(draft['id'],'owner')['revision'],'synthetic-time-limit',actor_id='owner')
    raise SystemExit('已到测试期限，已记录停止意图')

def create_and_approve(outer):
    event=outer.submit(source='web',source_event_id='synthetic-meeting-intervals',actor_id='owner',text=REQUEST)
    for _ in range(5):
        outer.drain(limit=1)
        state=outer.event(event['event_id'],'owner')
        tickets=outer.list('owner')
        print('intake',state['state'],tickets[0]['status'] if tickets else 'pending',flush=True)
        if tickets and tickets[0]['status']=='AWAITING_APPROVAL':break
        if state['state']=='NEEDS_ATTENTION' or tickets and tickets[0]['status'] in ('VERIFICATION_GAP','AWAITING_CLARIFICATION'):
            raise SystemExit('未形成可批准合约；已保留合成测试库用于诊断')
    draft=tickets[0]
    if draft['status']!='AWAITING_APPROVAL':raise SystemExit('未达到审批条件')
    # CI 不执行此脚本。合成审批只能批准这个夹具的文件、参数和预期值。
    observed=set()
    for c in draft['acceptance_contract']['criteria']:
        spec=c['test_spec']
        if c['verifier_id']!='python-json-cli' or spec['script']!='intervals.py' or spec['files'] or len(spec['args'])!=1:
            raise SystemExit('提案偏离受控验收范围')
        arg=json.dumps(json.loads(spec['args'][0]),separators=(',',':'))
        intervals=json.loads(arg)
        if not isinstance(intervals,list) or len(intervals)>20 or any(
                not isinstance(pair,list) or len(pair)!=2 or any(type(n) is not int or not 0<=n<=1440 for n in pair)
                or pair[0]>pair[1] for pair in intervals):
            raise SystemExit('测试输入超出工单范围')
        expected=len({minute for start,end in intervals for minute in range(start,end)})
        if c['expected_result']!={'minutes':expected}:raise SystemExit('验收预期有误')
        if c['required']:observed.add(arg)
    if not set(CASES)<=observed:raise SystemExit('缺必需验收用例')
    outer.approve_goal_contract(draft['id'],draft['goal_revision'],draft['acceptance_revision'],draft['revision'],
                                'synthetic-owner-confirm',actor_id='owner',source='web')
    return draft

if __name__=='__main__':
    main()
