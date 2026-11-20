"""由隔离启动器调用；模型使用同目录的离线替身。容量由已启动实例提供。"""
import json
import os
import shutil
import sqlite3
import sys
import time
import uuid
from pathlib import Path

board = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(board))
from workbench import Hub, Store, Workbench
from superagent.goal_checks import normalize
from superagent.goal_engine import GoalEngine, flow
from superagent.goal_runtime import GoalRuntime

root = Path(os.environ['AUTOLOOP_RUNTIME'])
hub = Hub(Path(os.environ['AUTOLOOP_HUB']))
assert Path(shutil.which('codex')).read_bytes()==Path(__file__).with_name('fixture.py').read_bytes()
capacity = json.loads((root.parent/'worker/config.json').read_text())['worker']['capacity']
store = Store(root)
checks = [dict(id=name,script=name+'.py',args=['input.csv'],files={'input.csv':'id\nA\nB\nA\n'},
               expected={name:expected}) for name,expected in [('rows',3),('unique',2)]]
engine = GoalEngine(store,normalize(dict(version=1,stages=[dict(id='counts',checks=checks)])),True)
runtime = GoalRuntime(store,hub)
tickets = [engine.create(dict(name='并发验收',goal='分别统计行数与不同 ID 数，交付两个程序',
            request_id=str(uuid.uuid4()),budget_minutes=15))[0]['id'] for _ in range(5)]
authorized = False
deadline = time.monotonic()+240
while time.monotonic()<deadline:
    runtime.run(once=True)
    tasks = [store.get(ticket) for ticket in tickets]
    statuses = [t['status'] for t in tasks]
    if any(s in ('blocked','budget_exhausted','paused') for s in statuses):
        raise RuntimeError([(t['status'],t['error']) for t in tasks])
    if not authorized and statuses==['awaiting_approval']*5:
        for task in tasks:
            engine.approve(task['id'],task['version'],task['approval']['id'])
        authorized=True
    if statuses==['completed']*5:
        break
    time.sleep(.15)
else:
    raise TimeoutError(statuses)
with sqlite3.connect(f'file:{hub.home}/hub.db?mode=ro',uri=True) as db:
    db.row_factory=sqlite3.Row
    runs=[dict(r) for r in db.execute('select id, state, claimed_at, finished_at, attempts from runs order by id')]
assert len(runs)==25 and all(r['state']=='done' and r['attempts']==0 for r in runs)
events=sorted((r[k],change) for r in runs for k,change in [('claimed_at',1),('finished_at',-1)])
active=peak=0
for stamp,change in events:
    active+=change
    peak=max(peak,active)
assert active==0 and peak==capacity, (peak,capacity)
owners={}
evidence=[]
wb=Workbench(store)
for task in tasks:
    state=flow(task)
    assert sum(d['kind']=='authorized' for d in state['decisions'])==1
    rounds=state['assignments']
    assert [r['phase'] for r in rounds]==['plan','package','package','assemble','review']
    assert task['review']['passed'] and len(task['checks'])==2
    for name in ['rows.py','unique.py']:
        wb.artifact_bytes(task['id'],name)
    for row in rounds:
        if row['phase']=='package':
            owners[row['run']]=task['id']
    evidence.append(dict(ticket=task['id'],status=task['status'],checks=task['checks'],
                         runs=[r['run'] for r in rounds],files=task['goal_delivery']['files']))
first_five=[owners[i] for i in sorted(owners)[:5]]
assert len(set(first_five))==5, first_five
result=dict(capacity=capacity,peak_simultaneously_claimed=peak,model='offline protocol fixture',
            first_five_package_tickets=first_five,tickets=evidence,hub_runs=runs)
(root.parent.parent/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print('验收通过',capacity,peak,len(tasks),len(runs))
