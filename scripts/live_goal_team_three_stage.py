"""验证第二阶段的回归会在第三阶段派工之前被独立发现。"""
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

from live_filename_index import require, wait_until


def execute(pilot, board, evidence):
    order = {
        'workflow':'outer','request_id':str(uuid.uuid4()),'name':'三阶段合成索引 · 非末阶段故障',
        'goal':(
            '交付 search.py 文件名前缀搜索、update.py 合并去重及增删、summarize.py 后缀分组统计。'
            '规划工作时建议按这三个程序的顺序各分一步。这个顺序是实施建议，成品依照下列实际输出验收。'
            '三个程序读取参数并将 JSON 输出到标准输出，搜索与更新结果按名称升序。'
        ),
        'acceptance':(
            'search.py alpha alphabet.md beta.txt alpha.txt 应输出 {"matches":["alpha.txt","alphabet.md"]}。'
            'search.py z beta.txt alpha.txt 应输出 {"matches":[]}。'
            'update.py beta.txt,alpha.txt alpine.md beta.txt 应输出 {"filenames":["alpha.txt","alpine.md"]}。'
            'update.py zebra.md,old.md apple.md,zebra.md old.md 应输出 {"filenames":["apple.md","zebra.md"]}。'
            'summarize.py a.TXT b.txt c.md README .env a.TXT 应输出 '
            '{"unique_count":5,"by_extension":{"(none)":2,"md":1,"txt":2}}。'
            'summarize.py 不接收参数时应输出 {"unique_count":0,"by_extension":{}}。'
            '后缀是最后一个点后的字面值小写，没后缀或仅以点开头的名字用 (none)，文件名去重。'
        ),
    }
    posted = board.call('/api/tasks',order,status=201)

    def read_ready():
        event = board.call('/api/outer/event',{'event_id':posted['event_id']})
        if not event.get('ticket_id'):
            return None
        ticket = board.call('/api/outer/get',{'ticket_id':event['ticket_id']})
        if ticket['status'] in ('VERIFICATION_GAP','NEEDS_ATTENTION','AWAITING_CLARIFICATION'):
            evidence['intake_status'] = ticket['status']
            evidence['verification_gaps'] = (ticket.get('acceptance_contract') or {}).get('verification_gaps', [])
            raise RuntimeError('三阶段合同尚不具备可执行的可信验收')
        return ticket if ticket['status']=='AWAITING_APPROVAL' else None

    ticket = wait_until(read_ready,240,'三阶段合同准备超时')
    ticket_id = ticket['id']
    checks = ticket['acceptance_contract']['criteria']
    scripts = {'search.py':set(),'update.py':set(),'summarize.py':set()}
    for row in checks:
        script = (row.get('test_spec') or {}).get('script')
        require(row.get('required') is True and row.get('verifier_id')=='python-json-cli' and script in scripts,
                '三阶段试单仅运行三个合成脚本的机器验收')
        scripts[script].add(row['criterion_id'])
    require(all(len(group)>=2 for group in scripts.values()),'每个阶段需固定两项可执行检查')
    board.call('/api/outer/approve',{'ticket_id':ticket_id,'revision':ticket['revision'],
              'goal_revision':ticket['goal_revision'],'acceptance_revision':ticket['acceptance_revision'],
              'request_id':str(uuid.uuid4())})
    evidence.update(ticket=ticket_id,approval_count=1)
    sys.path.insert(0,str(pilot.app))
    from superagent.goal_runtime import GoalRuntime
    from workbench import Hub, Store
    runtime = GoalRuntime(Store(pilot.runtime),Hub(pilot.hub))
    deadline = time.monotonic()+650
    while time.monotonic()<deadline:
        task = runtime.tick(ticket_id)
        state = task['goal_execution']
        if task['plan']:
            observed = [set(step['acceptance']) for step in task['plan']['steps']]
            require(observed == list(scripts.values()),'计划未使用已约定的三个独立阶段')
        validation = state.get('validation') or {}
        if task['step']==1 and state['stage']=='coordinate' and not evidence.get('non_terminal'):
            seen = {row['id']:row['status'] for row in validation.get('checks',[]) if row['id'] in scripts['search.py']}
            require(bool(seen) and 'FAIL' in seen.values() and not any(row['step']==2 for row in state['assignments']),
                    '第三阶段开始前未观察到可信的搜索回归')
            fault = json.loads((pilot.root/'fault.json').read_text())
            require(fault['checkpoint']==validation['artifact_sha256'],'回归检查未绑定实际候选')
            evidence['non_terminal'] = {'failed_checks':seen,'artifact_sha256':fault['checkpoint'],
                                        'step':task['step'],'third_stage_started':False,
                                        'retained_update_sha256':fault['unaffected_update_sha256']}
        if task['status']=='completed':
            require(evidence.get('non_terminal'),'三阶段工单未验证到第二阶段回归')
            delivery=task['goal_delivery']
            require(delivery['files']['update.py']==evidence['non_terminal']['retained_update_sha256'],
                    '返工改变了更新步骤的已验字节')
            for path in scripts:
                data = board.call(f'/api/tasks/{ticket_id}/artifact?path={path}',raw=True)
                require(hashlib.sha256(data).hexdigest()==delivery['files'][path],'下载未对应终验归档')
            evidence['delivery'] = delivery
            evidence['checks'] = {row['id']:row['status'] for row in task['checks']}
            return
        if task['status'] in ('blocked','budget_exhausted','paused'):
            raise RuntimeError(f"三阶段执行受阻：{task.get('error','')}")
        time.sleep(.5)
    raise TimeoutError('三阶段试验尚未进入终态')
