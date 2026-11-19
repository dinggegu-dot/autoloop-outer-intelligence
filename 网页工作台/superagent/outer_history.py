"""已鉴权工单的调用视图。执行决定只读取原账本，展示数据不回写。"""
from datetime import datetime, timezone
import json

from workbench import Problem


def _view(call_id, role, started, state, snapshot, receipt, detail):
    error = receipt.get('validation_error') or receipt.get('error')
    if receipt.get('settlement_reason'):
        error = ' · '.join(filter(None,(receipt['settlement_reason'], error)))
    if not error and state == 'stale':
        error = '模型返回时工单版本已变化'
    row = dict(id=call_id, role=role, started_at=started, state=state,
               model=receipt.get('model'), elapsed_ms=receipt.get('elapsed_ms'), error=error)
    if detail:
        row.update(input=snapshot, request=receipt.get('request'), output=receipt.get('result'),
                   final_message=receipt.get('final_message'), output_truncated=receipt.get('output_truncated', False),
                   usage=receipt.get('usage') or None, prompt_version=receipt.get('prompt_version'))
    return row


def ticket_calls(db, root, ticket, actor, tenant, call_id=None):
    detail = call_id is not None
    rows = []
    # 首轮建单和选单可能晚于模型领取。只追随已持久化的事件归属。
    fields = "i.snapshot,i.result" if detail else """NULL AS snapshot,
        json_object('model',json_extract(i.result,'$.model'),'elapsed_ms',json_extract(i.result,'$.elapsed_ms'),
                    'error',json_extract(i.result,'$.error'),'validation_error',json_extract(i.result,'$.validation_error'),
                    'settlement_reason',json_extract(i.result,'$.settlement_reason')) AS result"""
    query = """SELECT i.id,i.role,i.started_at,i.state,""" + fields + """
        FROM outer_invocations i JOIN outer_events e ON i.event=e.id
        LEFT JOIN outer_events choice ON choice.id=json_extract(e.result,'$.selected_by')
             AND choice.actor=e.actor AND choice.tenant=e.tenant
        WHERE e.actor=? AND e.tenant=? AND coalesce(i.ticket,e.ticket,choice.ticket)=?"""
    params = (actor, tenant, ticket)
    if detail:
        query += ' AND i.id=?'
        params += (call_id,)
    for item in db.execute(query, params):
        rows.append(_view(item['id'], item['role'], item['started_at'], item['state'],
                          json.loads(item['snapshot']) if detail else None, json.loads(item['result'] or '{}'), detail))

    # 按已批准的历代执行读取，改版后旧协调调用仍可追溯。
    assignments = """WITH executions(body) AS (
        SELECT body FROM tasks WHERE id=? UNION ALL
        SELECT body FROM outer_revisions WHERE ticket=? AND kind='execution_archive')
        SELECT a.value FROM executions,json_each(executions.body,'$.goal_execution.assignments') a
        WHERE json_extract(a.value,'$.phase')='coordinate'"""
    params = (ticket, ticket)
    if detail:
        assignments += " AND json_extract(a.value,'$.id')=?"
        params += (call_id,)
    for (body,) in db.execute(assignments, params):
        a = json.loads(body)
        report = a.get('report') or {}
        evidence = report.get('evidence') or {}
        if not evidence.get('started_ns'):
            # 正在调用或进程中断时可能还没有终态回执。待派工不算模型调用。
            marker = root / 'goal-attempts' / ticket / a['id'] / 'coordination-start.json'
            try:
                evidence = json.loads(marker.read_text())
            except FileNotFoundError:
                continue
        state = 'returned'
        code = evidence.get('failure_code')
        error = report.get('error')
        if code or error:
            state = 'rejected' if code == 'proposal_rejected' else 'failed'
        elif 'result' not in report:
            state = 'missing_receipt' if a['state'] == 'settled' else 'running'
            if state == 'missing_receipt':
                error = '执行已结束，未收到模型回执'
        audit = report.get('model_audit') or {}
        receipt = {**evidence, **audit, 'error':' · '.join(filter(None, (code, error))) or None,
                   'result':report.get('result', audit.get('returned_proposal'))}
        started = datetime.fromtimestamp(evidence['started_ns'] / 1e9, timezone.utc).isoformat(timespec='milliseconds')
        rows.append(_view(a['id'], 'goal_coordinator', started, state, a['coordination_context'], receipt, detail))
    if detail:
        if not rows:
            raise Problem('这张工单没有该调用记录', 404)
        return rows[0]
    return sorted(rows, key=lambda row: (row['started_at'], row['id']))
