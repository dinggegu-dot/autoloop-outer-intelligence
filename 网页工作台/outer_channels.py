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

def dispatch(service, source, command, body):
    """身份来自本机 session 或已经验证的桥签名，调用方不能指定 actor。"""
    if command == 'list':
        return service.list(OWNER)
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
        return service.get(ticket, OWNER)
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

def card(draft):
    """审批展示完整契约；手机承载不了全文时只保留查看入口。"""
    status = draft['status']
    binding = {key:draft[key] for key in ('revision','goal_revision','acceptance_revision')}
    binding['ticket_id'] = draft['id']
    lines = [f"工单 {draft['id']} · v{draft['revision']} · {STATUS.get(status,status)}"]
    actions = []
    if status == 'AWAITING_APPROVAL':
        goal=draft.get('goal_contract') or {}
        acceptance=draft.get('acceptance_contract') or {}
        terms=draft.get('approval_terms') or {}
        lines.extend(('目标：'+goal.get('goal_statement',''), '允许操作：'+display(terms.get('scope')),
                      '限制：'+display(terms.get('forbidden_operations')),
                      f"本单隔离工作区 · 执行预算 {terms.get('budget_seconds',0)//60} 分钟"))
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
        if draft.get('approval_terms') and len('\n'.join(lines)) <= 3950:
            actions.append(('outer_approve','批准这版目标与验收'))
        else:
            lines = [lines[0], '契约全文超过手机卡片长度。请到看板查看并决定。']
    elif status == 'AWAITING_CLARIFICATION':
        question = draft.get('question') or {}
        lines.extend([question.get('question_text',''),display(question.get('options') or []),'直接回复这张卡。'])
        binding['question_id'] = question.get('question_id')
    else:
        goal = draft.get('goal_contract') or {}
        lines.append(goal.get('goal_statement') or draft.get('summary') or '')
        if draft.get('error'):
            lines.append(display(draft['error']))
    if status not in ('PAUSED','STOPPED'):
        actions.append(('outer_pause','暂停此单'))
    if status == 'PAUSED':
        actions.append(('outer_resume','继续此单'))
        actions.append(('outer_stop','确认停止此单'))
    if draft.get('execution'):
        execution=draft['execution']
        lines.append(f"执行状态 {execution['status']} · 计划 v{execution['version']}")
        for item in execution.get('artifacts',[])[:8]:
            if isinstance(item,dict) and item.get('path'):
                lines.append('交付文件 '+str(item['path'])[:180])
    actions.append(('outer_status','看进度'))
    return {'body':'\n'.join(filter(None,lines)), 'binding':binding,'actions':actions}
