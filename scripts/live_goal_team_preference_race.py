"""在专用合成工单实测偏好回执暂停、旧卡和两端并发。"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sys
from threading import Barrier
from uuid import uuid4

from live_filename_index import Board, require, wait_until
from live_goal_team_short import CASES, TRIAL_LABEL


def execute(pilot, board, evidence):
    sys.path.insert(0,str(pilot.app))
    from superagent.goal_engine import digest
    from superagent.goal_runtime import GoalRuntime, attempt_file
    from workbench import Hub, Store

    store = Store(pilot.runtime)
    runtime = GoalRuntime(store, Hub(pilot.hub))
    order = dict(CASES['preference'], workflow='outer', request_id=str(uuid4()))
    order['name'] += TRIAL_LABEL
    posted = board.call('/api/tasks',order,status=201)

    def read(ticket_id):
        return board.call('/api/outer/get',{'ticket_id':ticket_id})

    def draft():
        event = board.call('/api/outer/event',{'event_id':posted['event_id']})
        if not event.get('ticket_id'):
            require(event['state'] not in ('NEEDS_ATTENTION','FAILED'), '合成接单重试后仍受阻')
            return None
        ticket = read(event['ticket_id'])
        if ticket['status'] in ('NEEDS_ATTENTION','VERIFICATION_GAP','AWAITING_CLARIFICATION'):
            evidence['intake_status'] = ticket['status']
            evidence['verification_gaps'] = (ticket.get('acceptance_contract') or {}).get('verification_gaps',[])
            raise RuntimeError('偏好试验未形成可批准的合同')
        return ticket if ticket['status']=='AWAITING_APPROVAL' else None

    ready = wait_until(draft,520,'等待偏好试验合同超时')
    require(ready['workbench_name']==order['name'],'只能操作明确标记的合成试验工单')
    criteria = ready['acceptance_contract']['criteria']
    require(any(row['verification_type']=='HUMAN_REVIEW' and row['required'] for row in criteria),
            '候选验收没有可验证的本人选择项')
    ticket = ready['id']
    approved = board.call('/api/outer/approve',{'ticket_id':ticket,'revision':ready['revision'],
                          'goal_revision':ready['goal_revision'],'acceptance_revision':ready['acceptance_revision'],
                          'request_id':str(uuid4()),'require_signoff':True})
    require(approved['mandate'].get('operator_kind')=='acceptance_test','试验批准身份不一致')
    other = board.call('/api/tasks',dict(CASES['short'],workflow='outer',request_id=str(uuid4())),status=201)

    def control(action, card):
        return board.call('/api/outer/'+action,{'ticket_id':ticket,'revision':card['revision'],
                          'request_id':str(uuid4())})

    def observe(status):
        task = runtime.tick(ticket)
        if task['status'] in ('blocked','budget_exhausted'):
            raise RuntimeError('试验执行受阻：'+str(task.get('error','')))
        if status != 'awaiting_artifact_review' and task['status'] in ('awaiting_artifact_review','completed'):
            raise RuntimeError('模型未提出需选择的样例，直接提交了成果')
        return task if task['status']==status else None

    barrier = pilot.root/'preference-barrier.json'

    def before_receipt():
        observe('executing')
        return json.loads(barrier.read_text()) if barrier.exists() else None

    initial = wait_until(before_receipt,370,'未观察到真实角色产生的首轮样例')
    receipt = attempt_file(store.root,ticket,initial['assignment'],'report.json')
    require(initial['options']==3 and not receipt.exists(),'试验屏障未卡在回执登记前')
    generation = store.get(ticket)['goal_execution']['stop_generation']
    control('pause',read(ticket))
    stopping = store.get(ticket)
    require(stopping['status']=='stopping' and stopping['goal_execution']['stop_generation']==generation+1,
            '挂起的原轮未进入正在停止状态')
    (pilot.root/'preference-release').touch()
    wait_until(receipt.exists,15,'暂停后未观察到旧候选回执')
    stopped = wait_until(lambda:observe('paused'),30,'试验暂停未结算')
    obsolete = next(row for row in stopped['goal_execution']['assignments'] if row['id']==initial['assignment'])
    actual_report = json.loads(receipt.read_text())
    require(not actual_report.get('error') and digest(actual_report['preference'])==initial['preference_sha256']
            and actual_report['checkpoint']['digest']==initial['checkpoint_sha256']
            and obsolete['state']=='settled' and obsolete['report']==actual_report
            and actual_report['outer_binding']==obsolete['outer_binding'],
            '暂停后未留存与原候选一致的真正迟到偏好回执')
    require(not stopped['goal_execution'].get('pending_preference'),'暂停后的旧结果建立了可答问题')
    evidence['late_preference'] = {'run':initial['run'],'report_bound_to_original':True,
                                    'stop_generation_increment':True,'answerable_question':False}

    control('resume',read(ticket))
    wait_until(lambda:observe('awaiting_preference'),370,'恢复后未生成当前问题')
    card = read(ticket)
    question = card['execution']['pending_preference']
    require(len(question['options'])==3,'恢复生成的样例数量不符')
    paused = control('pause',card)

    def form(current, candidate, text):
        return {'ticket_id':ticket,'revision':current['revision'],'question_id':candidate['id'],
                'question_revision':candidate['revision'],'candidate_digest':candidate['candidate_digest'],
                'answer':{'option_id':candidate['options'][0]['id'],'text':text},'request_id':str(uuid4())}

    path='/api/outer/answer_preference'
    dsh_path='/api/outer-dsh/answer_preference'
    old = form(card,question,'拒收试验')
    board.call(path,old,status=409)
    board.call(path,{**old,'revision':paused['revision'],'request_id':str(uuid4())},status=409)
    control('resume',paused)
    current = read(ticket)
    newer = current['execution']['pending_preference']
    waiting_step = store.get(ticket)['step']
    require((newer['id'],newer['revision'],newer['candidate_digest'])==
            (question['id'],question['revision']+1,question['candidate_digest']),'恢复更换了原问题或候选')
    board.call(path,{**old,'revision':current['revision'],'request_id':str(uuid4())},status=409)
    evidence['waiting_card'] = {'old_revision':question['revision'],'current_revision':newer['revision'],
                                'same_question_and_candidate':True,'old_while_paused':409,'old_after_resume':409}

    def other_ready():
        reply = board.call('/api/outer/event',{'event_id':other['event_id']})
        if reply.get('ticket_id'):
            return read(reply['ticket_id'])
        require(reply['state'] not in ('NEEDS_ATTENTION','FAILED'), '跨单绑定对照的接单受阻')
        return None

    other_ticket = wait_until(other_ready,150,'跨单绑定对照未生成工单')
    require(other_ticket['id']!=ticket,'缺少跨单绑定对照')
    wrong = form(current,newer,'错工单试验')
    wrong.update(ticket_id=other_ticket['id'],revision=other_ticket['revision'])
    board.call(path,wrong,status=409)
    with store.connect() as db:
        unwanted = db.execute("SELECT COUNT(*) FROM outer_events WHERE ticket=? AND "
                              "json_extract(body,'$.type')='execution_preference'",(other_ticket['id'],)).fetchone()[0]
    require(unwanted==0,'错投答复在另一张工单造成了写入')

    dsh = Board(pilot.port)
    dsh.headers.pop('Cookie',None)
    dsh.headers.pop('X-Odyssey-CSRF',None)
    dsh.headers['X-Odyssey-Trial-Operator'] = board.headers['X-Odyssey-Trial-Operator']
    unauthorized = form(current,newer,'未配对试验')
    dsh.call(dsh_path,unauthorized,status=403)
    dsh.headers['Authorization'] = 'Bearer ' + (pilot.root/'dsh-bridge-token').read_text().strip()
    start = Barrier(2)

    def submit(client, endpoint, text):
        data = form(current,newer,text)
        start.wait(timeout=10)
        try:
            result = client.call(endpoint,data)
        except RuntimeError as error:
            if 'HTTP 409, expected 200' not in str(error):
                raise
            return None
        require(result.get('action')=='PREFERENCE_RECORDED','并发回复返回了不同的业务动作')
        return client, endpoint, data, result['event_id']

    with ThreadPoolExecutor(max_workers=2) as pool:
        submissions = [pool.submit(submit,board,path,'先回应具体顾虑，再给一句建议。'),
                       pool.submit(submit,dsh,dsh_path,'先回应具体顾虑，让建议更简短。')]
    winners = [winner for future in submissions if (winner := future.result()) is not None]
    require(len(winners)==1,'跨端同时回答没有只接受一条')
    client, endpoint, data, event_id = winners[0]
    replay = client.call(endpoint,data)
    require(replay.get('duplicate') and replay['event_id']==event_id,'重送同一答复产生新事件')
    client.call(endpoint,{**data,'answer':{'text':'同键异内容试验'}},status=409)
    client.call(endpoint,{**data,'revision':read(ticket)['revision'],'request_id':str(uuid4())},status=409)
    actual = store.get(ticket)['goal_execution']['pending_preference']['answer']
    require(actual['event']==event_id and actual['operator_kind']=='acceptance_test' and
            actual['text']==data['answer']['text'],'胜出回复的原文、事件或身份不一致')
    evidence['cross_channel'] = {'successful_submissions':len(winners),'accepted_source':actual['source'],
        'wrong_ticket':409,'unpaired_dsh':403,'replay_same_event':True,'different_payload_same_key':409,
        'second_reply_current_revision':409,'operator_kind':actual['operator_kind']}

    # 批准可以覆盖多个实施阶段，试验不要在较短的固定期限内中断它。
    task = wait_until(lambda:observe('awaiting_artifact_review'),approved['mandate']['budget_seconds'] + 60,
                      '回答后未完成修改和独立复验')
    state = task['goal_execution']
    new_assignments = [row for row in state['assignments'] if row.get('preference_event')==event_id
                       and row['step']==waiting_step and row['phase']=='execute']
    require(len(new_assignments)==1,'此次答复未对应唯一修改派工')
    delivery = state['artifact_reviews'][-1]
    center = read(ticket)
    require(center['mandate_revision']==1,'偏好或暂停复验产生了新的授权')
    dsh.call('/api/outer-dsh/accept_artifact',{'ticket_id':ticket,'revision':center['revision'],
             'candidate':delivery['assignment'],'artifact_sha256':delivery['artifact_sha256'],
             'request_id':str(uuid4())})
    artifact = board.call(f'/api/tasks/{ticket}/artifact?path=report.html',raw=True)
    require(hashlib.sha256(artifact).hexdigest()==delivery['files']['report.html'],'最终文件与独立验收归档不符')
    evidence.update(approval_count=1, confirmed_resume_runs=len(new_assignments),
                    archive_sha256=delivery['files']['report.html'],formal_chrome_dsh_gui=False)
