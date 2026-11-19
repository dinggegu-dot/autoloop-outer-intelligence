"""已鉴权的看板与本人私聊共用的命令和确认内容。"""
import json
import re
from workbench import Problem

OWNER = 'owner'
TICKET = re.compile(r'wb-[0-9a-f]{32}')
STATUS = {
    'DRAFT':'草案', 'ASSESSING':'正在整理目标', 'AWAITING_CLARIFICATION':'需要澄清',
    'AWAITING_APPROVAL':'等待批准', 'VERIFICATION_GAP':'验收方法待补齐',
    'NEEDS_ATTENTION':'需要处理', 'AUTHORIZED':'已授权', 'PAUSED':'已暂停', 'STOPPED':'已停止',
}

EXECUTION_STATUS = {'created':'已创建','planning':'正在规划','queued':'等待派工','executing':'正在执行',
                    'reviewing':'正在验收','stopping':'正在停办','blocked':'受阻','budget_exhausted':'预算已用完',
                    'paused':'已暂停','completed':'已完成','rejected':'已拒绝'}

def dispatch(service, source, command, body):
    """身份来自本机 session 或已经验证的桥签名，调用方不能指定 actor。"""
    if command == 'list':
        return [dict(row,journey=journey(row)) for row in service.list(OWNER)]
    if command == 'event':
        return service.event(body['event_id'], OWNER)
    if command == 'select':
        return service.select_ticket(body['event_id'], body['ticket_id'], actor_id=OWNER,
                                     source=source, source_event_id=body['request_id'])
    if command == 'submit':
        return service.submit(source=source, source_event_id=body['request_id'], actor_id=OWNER,
                              text=body['text'], source_thread_id=body.get('thread'),
                              ticket_id=body.get('ticket_id'), question_id=body.get('question_id'),
                              expected_ticket_revision=body.get('revision'),
                              replace_requirement_ids=body.get('replace_requirement_ids'))
    ticket = body['ticket_id']
    if not isinstance(ticket, str) or not TICKET.fullmatch(ticket):
        raise Problem('工单编号无效')
    if command == 'get':
        draft=service.get(ticket, OWNER,request_feedback=True)
        return dict(draft,journey=journey(draft))
    if source == 'web' and command == 'calls':
        return service.calls(ticket, OWNER)
    if source == 'web' and command == 'call':
        if not isinstance(body.get('call_id'), str) or not 0 < len(body['call_id']) <= 100:
            raise Problem('调用编号无效')
        return service.calls(ticket, OWNER, call_id=body['call_id'])
    if command == 'approve':
        return service.approve_goal_contract(ticket, body['goal_revision'], body['acceptance_revision'],
                                             body['revision'], body['request_id'], actor_id=OWNER,
                                             source=source, source_thread_id=body.get('thread'))
    if command in ('pause','resume','stop'):
        return service.control(ticket, command, body['revision'], body['request_id'], actor_id=OWNER, source=source,source_thread_id=body.get('thread'))
    raise Problem('未知操作',404)

def display(value):
    if value is None or value == [] or value == {}:
        return '无'
    if isinstance(value, list):
        return '\n'.join(display(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    return str(value)

def journey(draft):
    execution = draft.get('execution') or {}
    state = draft['status']
    phase = execution.get('status') or draft.get('execution_status')
    flow = execution.get('goal_execution') or {}
    outcome = flow.get('outcome') or draft.get('outcome')
    decision_items = []
    label = STATUS.get(state,state)
    station, instruction = 'flight', '系统正在整理目标及验收条件。'
    if state == 'STOPPED' or (state == 'PAUSED' and phase == 'stopping'):
        station = 'dock'
        instruction = '原轮次正在退出。' if phase == 'stopping' else '已停止这张工单。'
    elif state == 'PAUSED' and draft.get('status_before_pause') == 'AWAITING_APPROVAL' and draft.get('task_id'):
        station,label,instruction = 'decision','待批准新版','旧执行已暂停。核对新版目标、验收和预算后再决定。'
    elif state == 'PAUSED':
        station,instruction = 'dock','核对当前目标后可继续此单。'
    elif state == 'AUTHORIZED':
        label = EXECUTION_STATUS.get(phase, label)
        if phase == 'completed':
            station,instruction = 'delivery','已完成独立验收，可查看归档交付。'
        elif phase in ('blocked','budget_exhausted') or outcome == 'AWAITING_REVIEW':
            station = 'decision'
            instruction = execution.get('error') or '请核对阻塞原因与授权范围，再决定如何调整。'
            ledger = flow.get('decisions', [])
            if len(ledger) >= 2 and [entry.get('kind') for entry in ledger[-2:]] == ['coordinated','blocked']:
                proposal = ledger[-2].get('proposal') or {}
                if proposal.get('action') in ('REQUEST_APPROVAL','BLOCKED'):
                    label = '需确认变更' if proposal['action'] == 'REQUEST_APPROVAL' else '协调后受阻'
                    instruction = proposal.get('reason') or instruction
                    decision_items = [dict(label=k, text=v) for k,v in (
                        ('建议调整',proposal.get('plan_change')), ('待确认的风险',proposal.get('remaining_risk')),
                        ('你的下一步','选择「修改要求」写明你的决定。新版目标和验收条件就绪后，再批准执行。')
                    ) if v]
        elif phase in ('paused','rejected','stopping'):
            station,instruction = 'dock','查看本单最新执行记录。'
        else:
            instruction = '执行器在已批准的范围内推进。'
    elif state in ('AWAITING_APPROVAL','AWAITING_CLARIFICATION','VERIFICATION_GAP','NEEDS_ATTENTION'):
        station = 'decision'
        instruction = {
            'AWAITING_APPROVAL':'请核对本版交付条件与预算。批准后将开始执行。',
            'AWAITING_CLARIFICATION':(draft.get('question') or {}).get('question_text') or '请补充关键条件。',
            'VERIFICATION_GAP':'当前证据不足以证明交付。请补充可验证的条件。',
            'NEEDS_ATTENTION':draft.get('error') or '请核对失败原因并补充要求。',
        }[state]
    can_approve = (state == 'AWAITING_APPROVAL' or label == '待批准新版') and (
        not draft.get('task_id') or phase in ('paused','completed','rejected'))
    if state == 'AWAITING_APPROVAL' and not can_approve:
        instruction = '新版契约已就绪，须先暂停旧执行并等待停稳，然后批准新版。'
    if draft.get('last_request_error') and state!='NEEDS_ATTENTION':
        instruction += ' 最近一次需求未完成（'+str(draft['last_request_error'])+'）。'
    can_resume = state == 'PAUSED' and (not draft.get('task_id') or
                 (phase == 'paused' and draft.get('status_before_pause') == 'AUTHORIZED'))
    terminal = state == 'STOPPED' or state == 'AUTHORIZED' and phase in ('completed','rejected')
    return dict(station=station,label=label,instruction=instruction,decision_items=decision_items,approve=can_approve,
                pause=not terminal and state != 'PAUSED' and phase != 'stopping',
                resume=can_resume,stop=not terminal and phase != 'stopping')


def card(draft):
    """审批展示完整契约；手机承载不了全文时只保留查看入口。"""
    status = draft['status']
    binding = {key:draft[key] for key in ('revision','goal_revision','acceptance_revision')}
    binding['ticket_id'] = draft['id']
    view = journey(draft)
    title = (draft.get('goal_contract') or {}).get('goal_statement') or draft.get('summary') or '目标工单'
    lines = [view['instruction'], f"工单 {draft['id']} · v{draft['revision']} · 目标 v{draft['goal_revision']} · 验收 v{draft['acceptance_revision']}"]
    actions = []
    lines.extend(item['label']+' · '+item['text'] for item in view['decision_items'])
    if view['approve'] or status == 'AWAITING_APPROVAL':
        goal=draft.get('goal_contract') or {}
        acceptance=draft.get('acceptance_contract') or {}
        terms=draft.get('approval_terms') or {}
        lines.extend(('目标：'+goal.get('goal_statement',''), '允许操作：'+display(terms.get('scope')),
                      '限制：'+display(terms.get('forbidden_operations')),
                      f"本单隔离工作区 · 执行预算 {terms.get('budget_seconds',0)//60} 分钟"))
        lines.append('执行模型 '+str(terms.get('execution_model') or '按当前配置'))
        lines.extend('人工验收 '+display(v) for v in acceptance.get('human_review_requirements',[]))
        for field,label in [('deliverables','交付'),('required_capabilities','能力'),('nonfunctional_requirements','质量要求'),
                            ('constraints','约束'),('assumptions','假设'),('out_of_scope','范围外')]:
            for req in goal.get(field,[]):
                lines.append(f"{label} · {req['description']}")
        for item in acceptance.get('criteria',[]):
            lines.append(f"{item['criterion_id']} · {item['description']} · {item['verifier_id']} v{item['verifier_version']}")
            lines.append('检查输入 '+json.dumps(item.get('test_spec'),ensure_ascii=False)+'；预期 '+
                         json.dumps(item.get('expected_result'),ensure_ascii=False))
        for gap in acceptance.get('verification_gaps',[]):
            lines.append('缺口：'+gap)
        if view['approve'] and draft.get('approval_terms') and len('\n'.join(lines)) <= 3950:
            actions.append(('outer_approve','批准这版目标与验收'))
        elif len('\n'.join(lines)) > 3950:
            lines = [view['instruction'],lines[1], '契约全文较长，请到电脑看板核对后决定。']
    elif status == 'AWAITING_CLARIFICATION':
        question = draft.get('question') or {}
        lines.extend([question.get('question_text',''),display(question.get('options') or []),'直接回复这张卡。'])
        binding['question_id'] = question.get('question_id')
    else:
        goal = draft.get('goal_contract') or {}
        lines.append(goal.get('goal_statement') or draft.get('summary') or '')
        if draft.get('error'):
            lines.append(display(draft['error']))
    if view['pause']:
        actions.append(('outer_pause','暂停此单'))
    if view['resume']:
        actions.append(('outer_resume','继续此单'))
    if status == 'PAUSED' and view['stop']:
        actions.append(('outer_stop','确认停止此单'))
    if draft.get('execution'):
        execution=draft['execution']
        lines.append(f"执行状态 {EXECUTION_STATUS.get(execution['status'],execution['status'])} · 计划 v{execution['version']}")
        for item in execution.get('artifacts',[])[:8]:
            path = item if isinstance(item,str) else item.get('path') if isinstance(item,dict) else None
            if path:
                lines.append('交付文件 '+str(path)[:180])
    if status not in ('PAUSED','STOPPED','AWAITING_CLARIFICATION'):
        goal=draft.get('goal_contract') or {}
        binding['requirements']=[{'id':r['requirement_id'],'label':r['description']} for key in
            ('deliverables','required_capabilities','nonfunctional_requirements','constraints','assumptions','out_of_scope')
            for r in goal.get(key,[]) if r.get('requirement_id') and r.get('source') in ('USER_EXPLICIT','USER_CONFIRMED')]
        actions.append(('outer_edit','修改要求'))
    actions.append(('outer_status','看进度'))
    if status == 'VERIFICATION_GAP':
        lines.extend((draft.get('acceptance_contract') or {}).get('verification_gaps',[]))
    if status not in ('PAUSED','STOPPED','AWAITING_CLARIFICATION'):
        lines.append('回复本卡可补充资料或提出修改要求。')
    if len('\n'.join(lines)) > 3950:
        lines = [view['instruction'],lines[1],'完整内容请在电脑看板核对。']
        actions = [a for a in actions if a[0] != 'outer_approve']
    return {'title':view['label']+' · '+title[:36], 'body':'\n'.join(filter(None,lines)), 'binding':binding,'actions':actions}
