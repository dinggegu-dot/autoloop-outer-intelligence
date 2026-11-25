"""通过隔离工作台普通业务接口验证真实 Sol，或准备一张等本人选择的样例单。"""
import argparse
import hashlib
import json
from pathlib import Path
import time
import uuid

from live_filename_index import Board, save_json


CASES = {
    'short': {
        'name': '合成清单 · 最短链路',
        'goal': '把合成文件名 river.txt、road.md、rill.md 中后缀为 .md 的名称按升序写入 result.json。',
        'acceptance': 'result.json 解析后的 JSON 对象须精确等于 {"filenames":["rill.md","road.md"]}。',
    },
    'preference': {
        'name': '为什么有些 AI 的安慰听起来很假',
        'goal': (
            '请根据这个合成情形做一份安慰对话小评测。情形是“我误发了草稿，虽然已经补发，'
            '我还是怕同事觉得我不靠谱”。我想先在原工单看三种回应，选一组并说理由。'
            '请按我的意见完善 report.html，在其中保留选择的依据。'
        ),
        'acceptance': (
            '必需机器验收：report.html 的网页标题和唯一一级标题均为“合成安慰对话”。'
            '必需人工验收：由我亲自在原单查看三组小样并给意见，再核对报告是否落实这条意见，'
            '并判断回应是否贴切。交互顺序、语气与意见的采用均由我看原单和报告后本人签收。'
        ),
    },
    'blind': {
        'name': '合成情感评测 · 两种回应方式盲评',
        'goal': (
            '为已登记的八个合成情感场景制作 blind.json 匿名成对回应。'
            '判断贴切程度前，我想先看看“我误发了草稿，虽然补发了还是怕同事觉得我不靠谱”的三组小样，'
            '挑选更贴近我的语气，作为两路回应都要遵照的偏好。这里的工单就是当前新单。'
        ),
        'acceptance': (
            '必需机器验收：使用 blind-projection v1，文件 blind.json，例号顺序固定为 '
            'H01,H02,H03,H04,H05,H06,H07,H08。检查成对匿名结构和显式来源标记。'
            '必需人工验收：回应的语气和贴切程度由本人结合三组小样的选择逐例判断。'
        ),
    },
}
TRIAL_LABEL = '（试验操作员）'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('instance', type=Path)
    parser.add_argument('--case', choices=CASES, default='short')
    parser.add_argument('--ticket', help='续看这次试点已经登记的原单')
    ballot_mode = parser.add_mutually_exclusive_group()
    ballot_mode.add_argument('--test-ballot', action='store_true', help='用试验操作员核对空票锁定与下载')
    ballot_mode.add_argument('--prepare-ballot', action='store_true', help='准备匿名候选后停住，等待本人真实评阅')
    args = parser.parse_args()
    scripted_setup = args.test_ballot or args.prepare_ballot
    if scripted_setup and args.case != 'blind':
        parser.error('试验操作员设置只用于单独的盲评试单')
    root = args.instance.resolve(strict=True)
    if root.parent != Path('/private/tmp') or not (root/'.goal-team-pilot').is_file():
        parser.error('仅支持已登记的隔离目标模式实例')
    instance = json.loads((root/'instance.json').read_text())
    if instance['phase'] != 'ready' or not instance['board'].startswith('http://127.0.0.1:'):
        parser.error('隔离实例未就绪')
    port = int(instance['board'].rsplit(':', 1)[-1])
    if port == 8765:
        parser.error('禁止试单指向现役工作台')
    board = Board(port)
    if board.call('/api/outer').get('goal_team_enabled') is not True:
        parser.error('未开启全 GPT 试点')
    board.headers['X-Odyssey-Trial-Operator'] = (root/'trial-operator-token').read_text().strip()
    ticket_id = args.ticket
    if not ticket_id:
        fields = dict(CASES[args.case])
        if scripted_setup:
            fields['name'] += TRIAL_LABEL
        reply = board.call('/api/tasks', {**fields, 'workflow':'outer',
                                          'request_id':str(uuid.uuid4())}, status=201)
        ticket_id = reply.get('ticket_id')
        print(f"已登记合成接单事件 {reply['event_id']}", flush=True)
    last = None
    deadline = time.monotonic() + (760 if args.case == 'blind' else 440)
    while time.monotonic() < deadline:
        if not ticket_id:
            event = board.call('/api/outer/event', {'event_id':reply['event_id']})
            ticket_id = event.get('ticket_id')
            if not ticket_id and event['state'] in ('DONE', 'NEEDS_ATTENTION', 'FAILED'):
                raise RuntimeError('接单未生成原工单')
        else:
            ticket = board.call('/api/outer/get', {'ticket_id':ticket_id})
            if scripted_setup and ticket['workbench_name'] != CASES['blind']['name'] + TRIAL_LABEL:
                raise RuntimeError('只有明确标作试验操作员的合成盲评单允许代为操作')
            execution = ticket.get('execution') or {}
            outer, inner = ticket['status'], execution.get('status')
            if last != (outer, inner):
                print(ticket_id, outer, inner, flush=True)
                last = outer, inner
            if outer == 'AWAITING_APPROVAL':
                approved = board.call('/api/outer/approve', {
                    'ticket_id':ticket_id, 'revision':ticket['revision'],
                    'goal_revision':ticket['goal_revision'], 'acceptance_revision':ticket['acceptance_revision'],
                    'request_id':str(uuid.uuid4()), 'require_signoff':args.case!='short',
                })
                if approved['mandate'].get('operator_kind') != 'acceptance_test':
                    raise RuntimeError('试验批准未登记正确身份')
            elif args.case != 'short' and execution.get('pending_preference'):
                question = execution['pending_preference']
                if len(question['options']) != 3 or any(row.get('status') != 'completed'
                                                        for row in execution['team_activity']):
                    raise RuntimeError('样例边界或角色回执不完整')
                if scripted_setup:
                    board.call('/api/outer/answer_preference', {
                        'ticket_id':ticket_id, 'revision':ticket['revision'], 'question_id':question['id'],
                        'question_revision':question['revision'], 'candidate_digest':question['candidate_digest'],
                        'answer':{'option_id':question['options'][0]['id'], 'text':'试验夹具选择。优先回应具体的顾虑。'},
                        'request_id':str(uuid.uuid4()),
                    })
                    continue
                save_json(root/'ready-for-owner.json', {
                    'ticket':ticket_id, 'candidate_digest':question['candidate_digest'],
                    'options':len(question['options']), 'role_count':len(execution['team_activity']),
                })
                print('已保存三个真实样例，等待本人在原单选择', flush=True)
                return
            elif args.case == 'short' and inner == 'completed':
                data = board.call(f'/api/tasks/{ticket_id}/artifact?path=result.json', raw=True)
                digest = hashlib.sha256(data).hexdigest()
                if (json.loads(data) != {'filenames':['rill.md','road.md']}
                        or execution['goal_delivery']['files'].get('result.json') != digest):
                    raise RuntimeError('原单下载与独立验收不一致')
                save_json(root/'short-acceptance.json', {
                    'ticket':ticket_id, 'version':execution['version'], 'archive_sha256':digest,
                    'mandate_revision':ticket['mandate_revision'], 'calls':execution['team_activity'],
                    'verification':execution['checks'],
                })
                print(f'已验归档与下载一致 {digest}', flush=True)
                return
            elif scripted_setup and (ticket.get('blind_review') or {}).get('status') == 'awaiting_ballot':
                candidate = ticket['blind_review']
                path = f"/api/tasks/{ticket_id}/artifact?path=blind.json&candidate={candidate['candidate']}"
                board.call(path,status=409)
                if args.prepare_ballot:
                    save_json(root/'ready-for-blind-review.json', {
                        'ticket':ticket_id,'candidate':candidate['candidate'],
                        'manifest_digest':candidate['manifest_digest'],
                    })
                    print('匿名候选已通过机器验收，等本人评阅。',flush=True)
                    return
                data = {'ticket_id':ticket_id, 'revision':ticket['revision'], 'experiment_id':candidate['experiment_id'],
                        'manifest_digest':candidate['manifest_digest'], 'judgments':[], 'request_id':str(uuid.uuid4())}
                locked = board.call('/api/outer/lock_blind_ballot',data)
                if (locked['lock']['operator_kind'] != 'acceptance_test' or locked['result']['counts']['unrated'] != 8
                        or not board.call('/api/outer/lock_blind_ballot',data)['duplicate']):
                    raise RuntimeError('盲评锁票身份、未评计数或幂等不符')
                board.call('/api/outer/accept_artifact',{
                    'ticket_id':ticket_id, 'revision':locked['ticket_revision'], 'candidate':candidate['candidate'],
                    'artifact_sha256':candidate['artifact_sha256'], 'request_id':str(uuid.uuid4()),
                })
                downloaded = board.call(path,raw=True)
                if json.loads(downloaded) != candidate['projection']:
                    raise RuntimeError('盲评后下载与已登记的匿名投影不一致')
                save_json(root/'blind-integration.json',dict(ticket=ticket_id, owner_judgments=False,
                          ballot_digest=locked['lock']['ballot_digest'], result=locked['result'],
                          runtime_measures=locked.get('runtime_measures'),
                          download_sha256=hashlib.sha256(downloaded).hexdigest()))
                print('真实生成、独立验收和试验操作员锁票已通过。真人判断待实施。',flush=True)
                return
            elif outer in ('VERIFICATION_GAP','NEEDS_ATTENTION','AWAITING_CLARIFICATION') or inner in ('blocked','budget_exhausted'):
                reasons = (ticket.get('acceptance_contract') or {}).get('verification_gaps')
                raise RuntimeError(f'原单待核对：{reasons or execution.get("error") or outer}')
            elif args.case == 'preference' and inner == 'awaiting_artifact_review':
                raise RuntimeError('已进入签收但未观察到中途样例选择')
        time.sleep(2)
    raise TimeoutError('等待本次试单状态超时，先查原单与 Worker')


if __name__ == '__main__':
    main()
