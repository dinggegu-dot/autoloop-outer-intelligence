"""V2 接单、澄清、验收提案和授权。外层草案与正式执行共用 SQLite。"""
from contextlib import contextmanager
import json
import re
import time
import uuid

from workbench import Problem, TASK_RE, now, omit_call_payloads

from .goal_checks import _SECRET
from .outer_model import ACCEPTANCE_SCHEMA, INTAKE_SCHEMA, CodexOuterModel, ModelFailure, role_invoke


_MAX_ATTEMPTS = 2
_MAX_CALLS = 16
_MAX_QUESTIONS = 4
_MAX_TOKENS = 250_000
_LEASE_SECONDS = 145
_TICKET_LIMIT = 12
_MAX_SOURCE_ID = 220
_FIELDS = ('deliverables', 'required_capabilities', 'nonfunctional_requirements',
           'constraints', 'assumptions', 'out_of_scope')
_OPERATIONS = ('pause', 'resume', 'stop')
_CLAUSE_BREAKS = re.compile(r'[\n。！？；，、：;!?,:]+')
_PEM = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)', re.DOTALL)
_SCOPE = ['read_workspace', 'write_workspace', 'run_local_tests']
_FORBIDDEN = ['network_tools', 'send', 'publish', 'install', 'payment', 'write_outside_workspace']
_ACTIONS = {'CREATE_DRAFT', 'ATTACH_TO_TICKET', 'ANSWER_CLARIFICATION', 'PROPOSE_CHANGE',
            'REQUEST_TICKET_SELECTION', 'ANSWER_STATUS', 'IGNORE', 'ESCALATE'}
_PROTECTED_SOURCES = {'USER_EXPLICIT', 'USER_CONFIRMED'}
_IMPACTS = {'deliverables', 'functionality', 'acceptance', 'permissions', 'cost', 'irreversible'}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS outer_events(
    id TEXT PRIMARY KEY, source TEXT NOT NULL, tenant TEXT NOT NULL, source_event_id TEXT NOT NULL,
    actor TEXT NOT NULL, ticket TEXT, body TEXT NOT NULL, state TEXT NOT NULL, role TEXT,
    attempt INTEGER NOT NULL DEFAULT 0, lease TEXT, lease_until REAL, result TEXT,
    received_at TEXT NOT NULL, UNIQUE(source, tenant, source_event_id));
CREATE INDEX IF NOT EXISTS outer_events_queue ON outer_events(state, received_at);
CREATE INDEX IF NOT EXISTS outer_events_ticket ON outer_events(ticket, received_at);
CREATE TABLE IF NOT EXISTS outer_tickets(
    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, actor TEXT NOT NULL, body TEXT NOT NULL,
    updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS outer_tickets_owner ON outer_tickets(tenant, actor, updated_at);
CREATE TABLE IF NOT EXISTS outer_threads(
    source TEXT NOT NULL, tenant TEXT NOT NULL, thread TEXT NOT NULL, ticket TEXT NOT NULL,
    PRIMARY KEY(source, tenant, thread));
CREATE TABLE IF NOT EXISTS outer_card_bindings(
    source TEXT NOT NULL, tenant TEXT NOT NULL, thread TEXT NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY(source, tenant, thread));
CREATE TABLE IF NOT EXISTS outer_revisions(
    ticket TEXT NOT NULL, kind TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY(ticket, kind, revision));
CREATE TABLE IF NOT EXISTS outer_invocations(
    id TEXT PRIMARY KEY, event TEXT NOT NULL, ticket TEXT, role TEXT NOT NULL,
    snapshot TEXT NOT NULL, started_at TEXT NOT NULL, result TEXT, state TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS outer_invocations_ticket ON outer_invocations(ticket);
CREATE TABLE IF NOT EXISTS outer_deliveries(
    id TEXT PRIMARY KEY, event TEXT NOT NULL, ticket TEXT, body TEXT NOT NULL, delivered_at TEXT);
"""


# 这次输入的处理结果与工单上已生效的契约分别展示。
_LAST_REQUEST_ERROR = """SELECT CASE WHEN e.state IN ('NEEDS_ATTENTION','REJECTED')
    THEN COALESCE(json_extract(e.result,'$.error'),'需要人工核对') END
    FROM outer_events e WHERE e.ticket=d.id AND json_extract(e.body,'$.type')='message'
    ORDER BY e.received_at DESC,e.rowid DESC LIMIT 1"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _id(prefix):
    return prefix + uuid.uuid4().hex


def _string(value, name, limit, *, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit or len(value) > limit or '\0' in value:
        raise Problem(f'{name}无效')
    return value.strip()


def _model_text(value, limit=6000):
    # 保留原始事件，送给模型的快照只含去掉明显凭据的内容。
    return _SECRET.sub('[敏感内容]', _PEM.sub('[私钥]', value))[:limit]


def _verbatim_clause(description, source):
    normalize = lambda value: ' '.join(value.split()).strip('。！？；，、：.!?;,:')
    expected = normalize(description)
    return bool(expected) and (expected == normalize(source) or any(
        expected == normalize(clause) for clause in _CLAUSE_BREAKS.split(source)))


def _revision(number, name):
    if type(number) is not int or number <= 0:
        raise Problem(f'缺少有效{name}')


class OuterIntake:
    def __init__(self, workbench, model=None, registry=None):
        self.workbench = workbench
        self.store = workbench.store
        self.model = model if model is not None else CodexOuterModel()
        self.registry = registry
        with self.store.connect() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _transaction(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            yield db

    @staticmethod
    def _owner(actor_id, tenant):
        return _string(actor_id, '身份', _MAX_SOURCE_ID), _string(tenant, '实例', _MAX_SOURCE_ID)

    def _source(self, source, source_event_id, actor_id, tenant, source_thread_id):
        if source not in ('web', 'feishu'):
            raise Problem('来源无效', 403)
        actor_id, tenant = self._owner(actor_id, tenant)
        return (source, _string(source_event_id, '来源事件编号', _MAX_SOURCE_ID), actor_id, tenant,
                _string(source_thread_id, '来源会话编号', _MAX_SOURCE_ID, optional=True))

    @staticmethod
    def _ticket(db, ticket_id, actor=None, tenant=None):
        if not isinstance(ticket_id, str) or not TASK_RE.fullmatch(ticket_id):
            raise Problem('工单编号无效', 404)
        row = db.execute('SELECT * FROM outer_tickets WHERE id=?', (ticket_id,)).fetchone()
        if row is None or actor is not None and (row['actor'], row['tenant']) != (actor, tenant):
            raise Problem('工单不存在或无权访问', 404)
        return json.loads(row['body'])

    @staticmethod
    def _save(db, ticket):
        ticket['updated_at'] = now()
        db.execute('UPDATE outer_tickets SET body=?,updated_at=? WHERE id=?',
                   (_json(ticket), ticket['updated_at'], ticket['id']))

    @staticmethod
    def _record_revision(db, ticket, kind, revision, value):
        db.execute('INSERT INTO outer_revisions VALUES(?,?,?,?)', (ticket, kind, revision, _json(value)))

    @staticmethod
    def _compare(ticket, expected):
        _revision(expected, '工单版本')
        if ticket['revision'] != expected:
            raise Problem('工单版本已变化，请重新查看', 409)

    @staticmethod
    def _event_view(row, duplicate=False):
        return {'event_id': row['id'], 'correlation_id': row['id'], 'ticket_id': row['ticket'],
                'state': row['state'], 'duplicate': duplicate, **json.loads(row['result'] or '{}')}

    def _insert(self, db, identity, ticket, body, *, role=None):
        source, external_id, actor, tenant, thread = identity
        body = {**body, 'schema_version': 1, 'source_thread_id': thread, 'actor_type': 'human'}
        previous = db.execute('SELECT * FROM outer_events WHERE source=? AND tenant=? AND source_event_id=?',
                              (source, tenant, external_id)).fetchone()
        if previous is not None:
            if (previous['actor'] != actor or json.loads(previous['body']) != body):
                raise Problem('同一个来源事件不能提交不同内容', 409)
            return previous, True
        event = _id('evt-')
        db.execute('''
            INSERT INTO outer_events(id,source,tenant,source_event_id,actor,ticket,body,state,role,received_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)''',
                   (event, source, tenant, external_id, actor, ticket, _json(body), 'PENDING', role, now()))
        return db.execute('SELECT * FROM outer_events WHERE id=?', (event,)).fetchone(), False

    def _thread(self, db, source, tenant, thread, ticket=None):
        if not thread:
            return None
        row = db.execute('SELECT ticket FROM outer_threads WHERE source=? AND tenant=? AND thread=?',
                         (source, tenant, thread)).fetchone()
        if row and ticket and row[0] != ticket:
            raise Problem('此会话或卡片已关联其他工单', 409)
        if ticket and row is None:
            db.execute('INSERT INTO outer_threads VALUES(?,?,?,?)', (source, tenant, thread, ticket))
        return row[0] if row else ticket

    @staticmethod
    def _card(db, source, tenant, thread):
        row = db.execute('SELECT body FROM outer_card_bindings WHERE source=? AND tenant=? AND thread=?',
                         (source, tenant, thread)).fetchone()
        return json.loads(row[0]) if row else None

    def bind_thread(self, ticket_id, source, source_thread_id, *, actor_id, tenant='default',
                    expected_ticket_revision=None):
        identity = self._source(source, 'binding', actor_id, tenant, source_thread_id)
        if identity[4] is None:
            raise Problem('缺少来源会话编号')
        with self._transaction() as db:
            ticket = self._ticket(db, ticket_id, identity[2], identity[3])
            if expected_ticket_revision is not None:
                self._compare(ticket, expected_ticket_revision)
            self._thread(db, source, identity[3], identity[4], ticket_id)
            binding = {'ticket_id': ticket_id, 'ticket_revision': ticket['revision'],
                       'goal_revision': ticket['goal_revision'], 'acceptance_revision': ticket['acceptance_revision'],
                       'question_id': (ticket['question'] or {}).get('question_id'), 'status': ticket['status']}
            if ticket.get('task_id'):
                current = db.execute("""SELECT json_extract(body,'$.goal_execution.artifact_reviews[#-1]')
                                        FROM tasks WHERE id=?""",(ticket_id,)).fetchone()
                review = json.loads(current[0]) if current and current[0] else {}
                if review.get('state') == 'pending':
                    binding.update(candidate=review['assignment'],artifact_sha256=review['artifact_sha256'])
            existing = self._card(db, source, identity[3], identity[4])
            if existing is None:
                db.execute('INSERT INTO outer_card_bindings VALUES(?,?,?,?)',
                           (source, identity[3], identity[4], _json(binding)))
            elif existing != binding:
                raise Problem('已发卡片不能绑定新版工单', 409)

    def submit(self, *, source, source_event_id, actor_id, text, source_thread_id=None, tenant='default',
               ticket_id=None, expected_ticket_revision=None, question_id=None, replace_requirement_ids=None):
        identity = self._source(source, source_event_id, actor_id, tenant, source_thread_id)
        _string(text, '原始需求', 6000)
        _, _, actor, tenant, thread = identity
        if question_id is not None:
            question_id = _string(question_id, '澄清问题编号', _MAX_SOURCE_ID)
        if replace_requirement_ids is not None:
            if (not isinstance(replace_requirement_ids, list) or len(replace_requirement_ids) > 12
                    or any(not isinstance(item, str) for item in replace_requirement_ids)
                    or len(set(replace_requirement_ids)) != len(replace_requirement_ids)):
                raise Problem('请选择有效的旧要求替换项')
            _revision(expected_ticket_revision, '工单版本')
        with self._transaction() as db:
            linked = self._thread(db, source, tenant, thread)
            requested = ticket_id
            ticket_id = ticket_id or linked
            row, duplicate = self._insert(db, identity, ticket_id, {
                'type': 'message', 'text': text, 'ticket_id': requested,
                'expected_ticket_revision': expected_ticket_revision, 'question_id': question_id,
                'replace_requirement_ids': replace_requirement_ids,
            }, role='intake_assessor')
            if duplicate:
                return self._event_view(row, True)
            if linked and requested and linked != requested:
                raise Problem('会话与指定工单不一致', 409)
            if (question_id or replace_requirement_ids) and not ticket_id:
                raise Problem('澄清或替换要求必须关联有效工单', 409)
            if ticket_id is not None:
                ticket = self._ticket(db, ticket_id, actor, tenant)
                if expected_ticket_revision is not None:
                    self._compare(ticket, expected_ticket_revision)
                if replace_requirement_ids:
                    current_goal = ticket['goal_contract'] or {}
                    known = {item['requirement_id'] for field in _FIELDS for item in current_goal.get(field, [])}
                    if not set(replace_requirement_ids).issubset(known):
                        raise Problem('被替换要求已更新或不存在', 409)
                if ticket['status'] in ('PAUSED', 'STOPPED'):
                    raise Problem('工单已暂停或停止', 409)
                question = ticket.get('question')
                card = self._card(db, source, tenant, thread) if linked else None
                card_question = card['question_id'] if card else None
                if question_id and card_question and question_id != card_question:
                    raise Problem('卡片与指定澄清问题不一致', 409)
                selected_question = question_id or card_question
                if selected_question and (not question or selected_question != question['question_id']):
                    raise Problem('澄清问题已更新，请重新查看', 409)
                if question and not selected_question:
                    raise Problem('请从当前澄清问题提交答案', 409)
                # 入队时绑定解析后的正式版本，不能用更新后的问题解释旧回复。
                db.execute('UPDATE outer_events SET result=? WHERE id=?',
                           (_json({'binding': {'revision': ticket['revision'],
                                               'question_id': question['question_id'] if question else None}}), row['id']))
            row = db.execute('SELECT * FROM outer_events WHERE id=?', (row['id'],)).fetchone()
            return self._event_view(row)

    def select_ticket(self, event_id, ticket_id, *, actor_id, source, source_event_id, tenant='default'):
        identity = self._source(source, source_event_id, actor_id, tenant, None)
        with self._transaction() as db:
            original = db.execute('SELECT * FROM outer_events WHERE id=? AND actor=? AND tenant=?',
                                  (event_id, identity[2], identity[3])).fetchone()
            if not original:
                raise Problem('原始事件不存在或无权访问', 404)
            selection = json.loads(original['result'] or '{}')
            if selection.get('action') != 'REQUEST_TICKET_SELECTION':
                raise Problem('原始事件无需选择工单', 409)
            row, duplicate = self._insert(db, identity, ticket_id, {
                'type': 'selection', 'selected_from': event_id, 'ticket_id': ticket_id,
                'force_new': ticket_id is None, 'text': json.loads(original['body'])['text'],
            }, role='intake_assessor')
            if duplicate:
                return self._event_view(row, True)
            if selection.get('selected_by'):
                raise Problem('已经处理此工单选择', 409)
            if ticket_id is not None:
                choice = next((item for item in selection['choices'] if item['id'] == ticket_id), None)
                if choice is None:
                    raise Problem('工单不在当前选项中', 409)
                ticket = self._ticket(db, ticket_id, identity[2], identity[3])
                self._compare(ticket, choice['revision'])
                if ticket['status'] in ('PAUSED', 'STOPPED'):
                    raise Problem('工单已暂停或停止', 409)
                question = ticket['question']
                if question:
                    self._finish(db, row, 'DONE', {'action': 'CLARIFY', 'question': question,
                                                  'ticket_revision': ticket['revision']}, ticket)
                else:
                    db.execute('UPDATE outer_events SET result=? WHERE id=?',
                               (_json({'binding': {'revision': ticket['revision'], 'question_id': None}}), row['id']))
            selection['selected_by'] = row['id']
            db.execute('UPDATE outer_events SET result=? WHERE id=?', (_json(selection), event_id))
            return self._event_view(db.execute('SELECT * FROM outer_events WHERE id=?', (row['id'],)).fetchone())

    def event(self, event_id, actor_id, *, tenant='default'):
        actor, tenant = self._owner(actor_id, tenant)
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM outer_events WHERE id=? AND actor=? AND tenant=?',
                             (event_id, actor, tenant)).fetchone()
            if not row:
                raise Problem('事件不存在或无权访问', 404)
            return self._event_view(row)

    def get(self, ticket_id, actor_id, *, tenant='default', request_feedback=False):
        actor, tenant = self._owner(actor_id, tenant)
        with self.store.connect() as db:
            ticket = self._ticket(db, ticket_id, actor, tenant)
            if request_feedback:
                ticket['last_request_error'] = db.execute(
                    f'SELECT ({_LAST_REQUEST_ERROR}) FROM outer_tickets d WHERE d.id=?',(ticket_id,)).fetchone()[0]
            if ticket['task_id']:
                ticket['execution'] = omit_call_payloads(self.store.get(ticket['task_id']))
                previous = []
                for gen,reviews,delivery in db.execute("""SELECT revision,json_extract(body,'$.goal_execution.artifact_reviews'),
                        json_extract(body,'$.goal_delivery') FROM outer_revisions WHERE ticket=? AND kind='execution_archive'
                        ORDER BY revision""",(ticket_id,)):
                    records=json.loads(reviews or '[]')
                    if delivery and not records:
                        records=[dict(json.loads(delivery),state='automatic',sequence=1)]
                    previous.extend(dict(mandate_revision=gen,**{k:r.get(k) for k in (
                        'sequence','state','assignment','artifact_sha256','files','reason')}) for r in records)
                if previous: ticket['prior_deliveries']=previous
            return ticket

    def list(self, actor_id, *, tenant='default'):
        actor, tenant = self._owner(actor_id, tenant)
        with self.store.connect() as db:
            rows = db.execute(
                f"SELECT d.body, json_extract(t.body,'$.status'),json_extract(t.body,'$.goal_execution.outcome'),({_LAST_REQUEST_ERROR}) FROM outer_tickets d "
                "LEFT JOIN tasks t ON t.id=d.id WHERE d.actor=? AND d.tenant=? "
                "ORDER BY d.updated_at DESC LIMIT 100", (actor,tenant)).fetchall()
        return [dict(json.loads(row[0]),execution_status=row[1],outcome=row[2],last_request_error=row[3]) for row in rows]

    def calls(self, ticket_id, actor_id, *, tenant='default', call_id=None):
        from .outer_history import ticket_calls
        actor, tenant = self._owner(actor_id, tenant)
        with self.store.connect() as db:
            db.execute('BEGIN')
            self._ticket(db, ticket_id, actor, tenant)
            return ticket_calls(db, self.store.root, ticket_id, actor, tenant, call_id)

    @staticmethod
    def _delivery(db, event, ticket_id, data):
        delivery_id = _id('delivery-')
        body = {'id': delivery_id, 'event_id': event['id'], 'ticket_id': ticket_id,
                'source': event['source'], 'tenant': event['tenant'], 'actor_id': event['actor'],
                'source_thread_id': json.loads(event['body']).get('source_thread_id'), **data}
        db.execute('INSERT INTO outer_deliveries VALUES(?,?,?,?,NULL)',
                   (delivery_id, event['id'], ticket_id, _json(body)))

    def pending_outbox(self, limit=50, *, source=None):
        with self.store.connect() as db:
            clause = '' if source is None else " AND json_extract(body,'$.source')=?"
            params = () if source is None else (source,)
            return [json.loads(row[0]) for row in db.execute(
                'SELECT body FROM outer_deliveries WHERE delivered_at IS NULL' + clause + ' ORDER BY rowid LIMIT ?',
                (*params, min(max(int(limit), 1), 100)))]

    def mark_delivered(self, delivery_id):
        with self._transaction() as db:
            db.execute('UPDATE outer_deliveries SET delivered_at=COALESCE(delivered_at,?) WHERE id=?',
                       (now(), delivery_id))

    def _finish(self, db, event, state, result, ticket=None, *, deliver=True):
        ticket_id = ticket['id'] if ticket else event['ticket']
        if ticket:
            result = {'correlation_id': ticket['correlation_id'], **result}
        db.execute('UPDATE outer_events SET ticket=?,state=?,result=?,lease=NULL,lease_until=NULL WHERE id=?',
                   (ticket_id, state, _json(result), event['id']))
        if deliver:
            if ticket:
                result = {**result, 'ticket_status': ticket['status'], 'ticket_revision': ticket['revision'],
                          'goal_revision': ticket['goal_revision'], 'acceptance_revision': ticket['acceptance_revision']}
            self._delivery(db, event, ticket_id, result)
            # 已参与该工单的本人私聊接收跨端决定，各投递仍有独立回执。
            if ticket_id and event['source'] == 'web' and db.execute(
                    "SELECT 1 FROM outer_events WHERE ticket=? AND source='feishu' "
                    "AND tenant=? AND actor=? LIMIT 1", (ticket_id,event['tenant'],event['actor'])).fetchone():
                self._delivery(db, {**dict(event),'source':'feishu'}, ticket_id, result)

    def _new_ticket(self, db, event):
        ticket_id = _id('wb-')
        ticket = dict(id=ticket_id, correlation_id=event['id'], status='DRAFT', revision=1,
                      goal_revision=0, acceptance_revision=0, mandate_revision=0, goal_contract=None,
                      acceptance_contract=None, question=None, mandate=None, task_id=None,
                      original_request_refs=[event['id']], relevant_thread_refs=[event['id']],
                      questions_asked=0, error='', created_at=now(), updated_at=now(),
                      approval_terms={'workspace': str(self.store.workspace(ticket_id)), 'scope': _SCOPE,
                                     'forbidden_operations': _FORBIDDEN, 'budget_seconds': 3600,
                                      'execution_model': 'gpt-6.2-sol'})
        db.execute('INSERT INTO outer_tickets VALUES(?,?,?,?,?)',
                   (ticket_id, event['tenant'], event['actor'], _json(ticket), ticket['updated_at']))
        return ticket

    def _context(self, db, event, ticket):
        message = json.loads(event['body'])
        refs = list(dict.fromkeys([*(ticket['original_request_refs'] if ticket else []),
                                  *(ticket['relevant_thread_refs'][-6:] if ticket else []), event['id']]))
        rows = db.execute(
            'SELECT id,body FROM outer_events WHERE id IN (' + ','.join('?' for _ in refs) + ')',
            refs).fetchall()
        allowed = ['CREATE_DRAFT', 'IGNORE', 'ESCALATE'] if message.get('force_new') else self._allowed(ticket)
        approved_summary, plan_revision = None, 0
        if ticket and ticket['task_id']:
            (body,) = db.execute('SELECT body FROM tasks WHERE id=?', (ticket['task_id'],)).fetchone()
            formal_task = json.loads(body)
            approved_summary = formal_task['goal_execution']['outer']['goal_contract']['goal_statement']
            plan_revision = (formal_task['plan'] or {}).get('version', 0)
        context = dict(
            schema_version=1, event_id=event['id'], correlation_id=ticket['correlation_id'] if ticket else event['id'],
            ticket_id=ticket['id'] if ticket else None, snapshot_revision=ticket['revision'] if ticket else 0,
            goal_revision=ticket['goal_revision'] if ticket else 0,
            acceptance_revision=ticket['acceptance_revision'] if ticket else 0,
            mandate_revision=ticket['mandate_revision'] if ticket else 0, plan_revision=plan_revision,
            ticket_status=ticket['status'] if ticket else None,
            original_request_refs=ticket['original_request_refs'] if ticket else [event['id']],
            relevant_thread_refs=refs, goal_contract=ticket['goal_contract'] if ticket else None,
            approved_goal_summary=approved_summary,
            constraints=ticket['goal_contract']['constraints'] if ticket and ticket['goal_contract'] else [],
            pending_clarifications=[ticket['question']] if ticket and ticket['question'] else [],
            completed_work_refs=[], current_assignment_refs=[], recent_failure_refs=[],
            remaining_budget=self._remaining(db, ticket, event), available_actions=allowed,
            policy={'scope': _SCOPE, 'forbidden_operations': _FORBIDDEN},
            untrusted_text={'current_message': _model_text(message.get('text', '')),
                            'prior_messages': {row['id']: _model_text(json.loads(row['body']).get('text', ''), 1000)
                                               for row in rows if row['id'] != event['id']}},
        )
        if event['role'] == 'intake_assessor' and message.get('replace_requirement_ids'):
            selected = set(message['replace_requirement_ids'])
            context['replace_requirements'] = [
                {'field': field, 'requirement_id': item['requirement_id'], 'description': item['description']}
                for field in _FIELDS for item in ticket['goal_contract'].get(field, [])
                if item['requirement_id'] in selected]
            context['available_actions'] = ['PROPOSE_CHANGE', 'ESCALATE']
        if event['role'] == 'acceptance_designer':
            context['verifier_catalogue'] = self._registry().catalogue()
            context['prior_acceptance'] = ticket['acceptance_contract']
        elif not ticket and not message.get('force_new'):
            context['candidate_tickets'] = [
                {'id': candidate['id'], 'summary': (candidate['goal_contract'] or {}).get('goal_statement', '')[:300],
                 'status': candidate['status'], 'revision': candidate['revision']}
                for (raw,) in db.execute('SELECT body FROM outer_tickets WHERE actor=? AND tenant=? ORDER BY updated_at DESC LIMIT ?',
                                         (event['actor'], event['tenant'], _TICKET_LIMIT))
                if (candidate := json.loads(raw))['status'] != 'STOPPED'
            ]
        return context

    def _remaining(self, db, ticket, event):
        if ticket:
            rows = db.execute('SELECT result FROM outer_invocations WHERE ticket=? OR (event=? AND ticket IS NULL)',
                              (ticket['id'], event['id'])).fetchall()
        else:
            rows = db.execute('SELECT result FROM outer_invocations WHERE event=?', (event['id'],)).fetchall()
        tokens = 0
        for (raw,) in rows:
            usage = json.loads(raw or '{}').get('usage', {})
            tokens += usage.get('input_tokens', 0) + usage.get('output_tokens', 0)
        return {'model_calls': max(0, _MAX_CALLS - len(rows)), 'model_tokens': max(0, _MAX_TOKENS - tokens)}

    @staticmethod
    def _allowed(ticket):
        if not ticket:
            return ['CREATE_DRAFT', 'REQUEST_TICKET_SELECTION', 'ANSWER_STATUS', 'IGNORE', 'ESCALATE']
        if ticket['question']:
            return ['ANSWER_CLARIFICATION', 'ANSWER_STATUS', 'ESCALATE']
        return ['ATTACH_TO_TICKET', 'PROPOSE_CHANGE', 'ANSWER_STATUS', 'IGNORE', 'ESCALATE']

    def _registry(self):
        if self.registry is not None:
            return self.registry
        from .outer_verification import compile_proposal, catalogue
        from types import SimpleNamespace
        return SimpleNamespace(compile_proposal=compile_proposal, catalogue=catalogue)

    def _needs_attention(self, db, event, ticket, reason):
        if ticket and ticket['status'] in ('DRAFT', 'ASSESSING'):
            ticket.update(status='NEEDS_ATTENTION', error=reason, revision=ticket['revision'] + 1)
            self._save(db, ticket)
        self._finish(db, event, 'NEEDS_ATTENTION', {'error': reason}, ticket)

    def _claim(self):
        with self._transaction() as db:
            # 过期的领取者保留审计；只有新租约可以提交新的正式决定。
            for expired in db.execute("SELECT * FROM outer_events WHERE state='RUNNING' AND lease_until<=?",
                                      (time.time(),)).fetchall():
                db.execute("UPDATE outer_invocations SET state='lease_expired',result=? WHERE id=? AND state='running'",
                           (_json({'error': 'lease_expired'}), expired['lease']))
                if expired['attempt'] >= _MAX_ATTEMPTS:
                    ticket = self._ticket(db, expired['ticket']) if expired['ticket'] else None
                    self._needs_attention(db, expired, ticket, '模型领取已过期，达到重试上限')
                else:
                    db.execute("UPDATE outer_events SET state='PENDING',lease=NULL,lease_until=NULL WHERE id=?",
                               (expired['id'],))
            if db.execute("SELECT 1 FROM outer_events WHERE state='RUNNING' LIMIT 1").fetchone():
                return None
            for event in db.execute("SELECT * FROM outer_events WHERE state='PENDING' ORDER BY received_at,rowid").fetchall():
                ticket = self._ticket(db, event['ticket']) if event['ticket'] else None
                if ticket and ticket['status'] == 'PAUSED':
                    continue
                if ticket and ticket['status'] == 'STOPPED':
                    self._finish(db, event, 'CANCELLED', {'error': '工单已停止'}, ticket)
                    continue
                bound = json.loads(event['result'] or '{}').get('binding', {})
                if ticket and bound.get('revision') != ticket['revision']:
                    self._finish(db, event, 'REJECTED', {'error': '工单版本已变化，请重新提交'}, ticket)
                    continue
                if not ticket and event['role'] == 'intake_assessor':
                    thread = json.loads(event['body']).get('source_thread_id')
                    if self._thread(db, event['source'], event['tenant'], thread):
                        self._finish(db, event, 'REJECTED', {'error': '会话已关联工单，请按当前工单继续'})
                        continue
                try:
                    snapshot = self._context(db, event, ticket)
                except (ImportError, AttributeError, TypeError, ValueError):
                    self._needs_attention(db, event, ticket, '验收目录无法读取')
                    continue
                if not all(snapshot['remaining_budget'].values()):
                    self._needs_attention(db, event, ticket, '已达到外层模型预算')
                    continue
                lease = _id('call-')
                db.execute("""
                    UPDATE outer_events SET state='RUNNING',attempt=attempt+1,lease=?,lease_until=? WHERE id=?
                    """, (lease, time.time() + _LEASE_SECONDS, event['id']))
                db.execute('INSERT INTO outer_invocations VALUES(?,?,?,?,?,?,NULL,?)',
                           (lease, event['id'], event['ticket'], event['role'], _json(snapshot), now(), 'running'))
                return dict(event), lease, snapshot
        return None

    def drain(self, limit=10):
        """从持久队列消费，不在 SQLite 写事务内调用模型。"""
        settled = 0
        for _ in range(min(50, max(1, int(limit)))):
            claim = self._claim()
            if claim is None:
                break
            event, lease, snapshot = claim
            role = event['role']
            try:
                response = role_invoke(self.model, role, snapshot,
                                       INTAKE_SCHEMA if role == 'intake_assessor' else ACCEPTANCE_SCHEMA)
            except ModelFailure as error:
                response = error.receipt()
            self._settle(event['id'], lease, snapshot, response)
            settled += 1
        return settled

    def _settle(self, event_id, lease, snapshot, response):
        with self._transaction() as db:
            event = db.execute('SELECT * FROM outer_events WHERE id=?', (event_id,)).fetchone()
            if event['lease'] != lease:
                # 迟到的回执归入原调用，保留停办或租约过期的决定。
                db.execute("""UPDATE outer_invocations SET result=json_set(?, '$.settlement_reason',json_extract(result,'$.error'))
                              WHERE id=? AND event=? AND state IN ('cancelled','lease_expired')""",
                           (_json(response), lease, event_id))
                return
            ticket = self._ticket(db, event['ticket']) if event['ticket'] else None
            result_status = 'accepted'
            if ticket and ticket['revision'] != snapshot['snapshot_revision']:
                result_status = 'stale'
                bound = json.loads(event['result'] or '{}').get('binding', {})
                if bound.get('revision') == ticket['revision']:
                    # 暂停/续办重新绑定未完成事项；旧推理不会直接提交。
                    db.execute("UPDATE outer_events SET state='PENDING',lease=NULL,lease_until=NULL WHERE id=?", (event_id,))
                else:
                    self._finish(db, event, 'REJECTED', {'error': '模型提案已过期'}, ticket)
            elif 'error' in response:
                result_status = 'failed'
                if event['attempt'] < _MAX_ATTEMPTS and response['error'] not in ('context_budget', 'invalid_config'):
                    db.execute("UPDATE outer_events SET state='PENDING',lease=NULL,lease_until=NULL WHERE id=?", (event_id,))
                else:
                    reason={'timeout':'模型调用超时','process_timeout':'模型进程超时','context_budget':'输入超出预算',
                            'invalid_config':'模型配置无效','invalid_output':'模型返回格式无效',
                            'output_budget':'模型输出超出预算'}.get(response['error'],'外层模型调用失败')
                    self._needs_attention(db,event,ticket,f"第 {event['attempt']} 次判断失败（{reason}），需人工处理")
            else:
                try:
                    db.execute('SAVEPOINT outer_proposal')
                    usage = response['usage']
                    if usage.get('input_tokens', 0) + usage.get('output_tokens', 0) > snapshot['remaining_budget']['model_tokens']:
                        raise ValueError('模型用量超出剩余预算')
                    if event['role'] == 'intake_assessor':
                        self._apply_intake(db, event, ticket, snapshot, response['result'])
                    else:
                        self._apply_acceptance(db, event, ticket, snapshot, response['result'])
                except (Problem, ValueError, KeyError, TypeError, AttributeError) as error:
                    db.execute('ROLLBACK TO outer_proposal')
                    result_status = 'rejected'
                    response['validation_error'] = _model_text(str(error), 240)
                    persisted = self._ticket(db, event['ticket']) if event['ticket'] else None
                    if persisted and persisted['status'] not in ('DRAFT', 'ASSESSING'):
                        self._finish(db, event, 'REJECTED', {'error': '外层提案未通过验证，原工单保持有效'}, persisted)
                    else:
                        self._needs_attention(db, event, persisted, '外层提案未通过版本、权限或验收检查')
                finally:
                    db.execute('RELEASE outer_proposal')
            db.execute('UPDATE outer_invocations SET state=?,result=? WHERE id=?',
                       (result_status, _json(response), lease))

    @staticmethod
    def _normal_goal(result, ticket, snapshot, event):
        raw = result['goal_draft']
        statement = _string(raw['goal_statement'], '目标', 2000)
        prior = ticket['goal_contract'] or {}
        source_keys = set(snapshot['relevant_thread_refs'])
        confirms = bool(ticket['question'])
        untrusted = snapshot['untrusted_text']
        actual_text = {**untrusted['prior_messages'], event['id']: untrusted['current_message']}
        replacements = set(json.loads(event['body']).get('replace_requirement_ids') or ())
        goal = dict(ticket_id=ticket['id'], goal_statement=statement, revision=ticket['goal_revision'] + 1,
                    source_refs=sorted(source_keys), created_at=now(), created_by='intake_assessor', status='DRAFT')
        for field in _FIELDS:
            requirements = raw[field] + (result['assumptions'] if field == 'assumptions' else [])
            if len(requirements) > 12:
                raise ValueError('目标条目超出上限')
            previous = {item['description']: item for item in prior.get(field, [])}
            normalized = []
            for item in requirements:
                description = _string(item['description'], '目标条目', 500)
                if any(row['description'] == description for row in normalized):
                    continue
                if description in previous:
                    if previous[description]['requirement_id'] in replacements:
                        raise ValueError('新版草案原样保留了用户指定替换的旧要求')
                    normalized.append(previous[description])
                    continue
                refs = list(dict.fromkeys(item['source_refs']))
                source = item['source']
                if source not in ('USER_EXPLICIT', 'USER_CONFIRMED', 'CONTEXT_DERIVED', 'MODEL_PROPOSED'):
                    raise ValueError('未知需求来源')
                if not set(refs).issubset(source_keys) or (source != 'MODEL_PROPOSED' and not refs):
                    raise ValueError('无效需求引用')
                if source in _PROTECTED_SOURCES:
                    # 引用存在只说明用户曾说过话。必须有原文锚点才能归为明确要求。
                    anchored = any(_verbatim_clause(description, actual_text.get(ref, '')) for ref in refs)
                    if not anchored or source == 'USER_CONFIRMED' and (not confirms or event['id'] not in refs):
                        source = 'MODEL_PROPOSED'
                if field == 'assumptions':
                    source = 'MODEL_PROPOSED'
                normalized.append({'requirement_id': _id('req-'), 'description': description,
                                   'source': source, 'source_refs': refs})
            retained = {item['requirement_id'] for item in normalized}
            normalized.extend(item for item in previous.values()
                              if item['source'] in _PROTECTED_SOURCES
                              and item['requirement_id'] not in retained | replacements)
            if len(normalized) > 12:
                raise ValueError('合并后目标条目超出上限')
            goal[field] = normalized
        if not goal['deliverables'] and not any(result['proposed_question'].values()):
            raise ValueError('草案没有可核查交付物')
        if prior:
            changes = {}
            for field in _FIELDS:
                old = {item['description'] for item in prior[field]}
                new = {item['description'] for item in goal[field]}
                if old != new:
                    changes[field] = {'added': sorted(new - old), 'removed': sorted(old - new)}
            goal['changes'] = changes
        return goal

    def _apply_intake(self, db, event, ticket, snapshot, result):
        action = result['suggested_action']
        if (result['schema_version'] != 1 or result['event_id'] != event['id']
                or result['input_snapshot_revision'] != snapshot['snapshot_revision']
                or action not in _ACTIONS or action not in snapshot['available_actions']):
            raise ValueError('智能角色建议的动作无效')
        visible = {item['id']: item for item in snapshot.get('candidate_tickets', [])}
        if ticket:
            visible = {ticket['id']: {'id': ticket['id']}}
        candidates = list(dict.fromkeys(result['candidate_ticket_ids']))
        if not set(candidates).issubset(visible) or not set(result['relation_evidence_refs']).issubset(snapshot['relevant_thread_refs']):
            raise ValueError('无效工单或事件引用')
        if action in ('IGNORE', 'ANSWER_STATUS', 'ESCALATE'):
            state = 'NEEDS_ATTENTION' if action == 'ESCALATE' else 'DONE'
            self._finish(db, event, state, {'action': action}, ticket, deliver=action != 'IGNORE')
            return
        if action == 'REQUEST_TICKET_SELECTION' or (action == 'CREATE_DRAFT' and candidates):
            choices = [visible[key] for key in candidates]
            if not choices:
                raise ValueError('没有可以选择的工单')
            self._finish(db, event, 'DONE', {'action': 'REQUEST_TICKET_SELECTION', 'choices': choices})
            return
        if not ticket:
            ticket = self._new_ticket(db, event)
            self._thread(db, event['source'], event['tenant'], json.loads(event['body']).get('source_thread_id'), ticket['id'])
            db.execute('UPDATE outer_invocations SET ticket=? WHERE id=?', (ticket['id'], event['lease']))
        elif ticket['task_id'] and action == 'ATTACH_TO_TICKET':
            # 已授权的补充材料是证据，不能修改范围和验收。
            message = _model_text(json.loads(event['body'])['text'])
            db.execute('INSERT INTO inbox_notes VALUES(?,?,?,?)',
                       (('web-' if event['source'] == 'web' else '') + event['id'], ticket['id'], message, now()))
            ticket['relevant_thread_refs'].append(event['id'])
            self._save(db, ticket)
            self._finish(db, event, 'DONE', {'action': action}, ticket)
            return
        prior_question = ticket['question']
        goal = self._normal_goal(result, ticket, snapshot, event)
        if prior_question:
            answer = {**prior_question, 'answer_status': 'ANSWERED', 'answer_event_id': event['id'],
                      'answered_by': event['actor'], 'answered_at': now()}
            self._record_revision(db, ticket['id'], 'clarification_answer', prior_question['goal_revision'], answer)
        question = result['proposed_question']
        next_question = None
        if question['question_text'].strip():
            if (not result['critical_ambiguities'] or question['blocking_scope'] not in _IMPACTS
                    or len(question['options']) > 6 or ticket['questions_asked'] >= _MAX_QUESTIONS):
                raise ValueError('澄清超出预算或缺少关键影响')
            txt = _string(question['question_text'], '澄清问题', 500)
            asked = {json.loads(row[0])['question_text'] for row in db.execute(
                "SELECT body FROM outer_revisions WHERE ticket=? AND kind='clarification'", (ticket['id'],))}
            if txt in asked:
                raise ValueError('不得重复已经回答的问题')
            next_question = dict(question_id=_id('q-'), goal_revision=goal['revision'],
                                 question_text=txt, reason=_string(question['reason'], '澄清原因', 500),
                                 options=[_string(item, '问题选项', 240) for item in question['options']],
                                 blocking_scope=question['blocking_scope'], answer_status='PENDING')
            self._record_revision(db, ticket['id'], 'clarification', goal['revision'], next_question)
            ticket['questions_asked'] += 1
        elif result['critical_ambiguities']:
            raise ValueError('关键歧义必须提出具体问题')
        self._record_revision(db, ticket['id'], 'goal', goal['revision'], goal)
        ticket.update(goal_contract=goal, goal_revision=goal['revision'], question=next_question,
                      acceptance_contract=None, revision=ticket['revision']+1, error='',
                      status='AWAITING_CLARIFICATION' if next_question else 'ASSESSING')
        if event['id'] not in ticket['relevant_thread_refs']:
            ticket['relevant_thread_refs'].append(event['id'])
        if ticket['task_id']:
            ticket['approval_terms'] = {
                **ticket['approval_terms'],
                'workspace': str(self.store.workspace(ticket['id'], generation=ticket['mandate_revision'] + 1)),
            }
        self._save(db, ticket)
        if next_question:
            self._finish(db, event, 'DONE', {'action': 'CLARIFY'}, ticket)
        else:
            db.execute("""
                UPDATE outer_events SET ticket=?,state='PENDING',role='acceptance_designer',attempt=0,
                       lease=NULL,lease_until=NULL,result=? WHERE id=?
                """, (ticket['id'], _json({'binding': {'revision': ticket['revision']}}), event['id']))

    def _apply_acceptance(self, db, event, ticket, snapshot, result):
        if (result['schema_version'] != 1 or result['ticket_id'] != ticket['id']
                or result['goal_revision'] != ticket['goal_revision']):
            raise ValueError('验收提案版本不匹配')
        refs = set(snapshot['relevant_thread_refs'])
        if not set(result['source_refs']).issubset(refs) or len(result['criteria']) > 40:
            raise ValueError('验收引用或数量无效')
        proposal = {**result, 'criteria': []}
        for raw in result['criteria']:
            item = dict(raw)
            if not set(item['source_refs']).issubset(refs) or not item['description'].strip():
                raise ValueError('验收条目来源无效')
            item['test_spec'] = json.loads(item.pop('test_spec_json'))
            item['expected_result'] = json.loads(item.pop('expected_result_json'))
            proposal['criteria'].append(item)
        goal = ticket['goal_contract']
        compiled = self._registry().compile_proposal(goal, proposal)
        if not isinstance(compiled, dict) or compiled.get('status') not in (
                'READY_FOR_APPROVAL', 'VERIFICATION_GAP', 'NEEDS_CLARIFICATION', 'FAILED'):
            raise ValueError('验收注册表状态不合法')
        if compiled['status'] == 'READY_FOR_APPROVAL':
            self._validate_ready_contract(compiled)
        revision = ticket['acceptance_revision'] + 1
        contract = {**compiled, 'ticket_id': ticket['id'], 'goal_revision': goal['revision'],
                    'revision': revision, 'created_at': now()}
        self._record_revision(db, ticket['id'], 'acceptance', revision, contract)
        status = {'READY_FOR_APPROVAL': 'AWAITING_APPROVAL',
                  'VERIFICATION_GAP': 'VERIFICATION_GAP'}.get(compiled['status'], 'NEEDS_ATTENTION')
        ticket.update(acceptance_contract=contract, acceptance_revision=revision,
                      status=status, revision=ticket['revision'] + 1)
        if status == 'AWAITING_APPROVAL':
            ticket['goal_contract'] = {**goal, 'status': 'READY_FOR_APPROVAL'}
        self._save(db, ticket)
        self._finish(db, event, 'DONE', {'action': status}, ticket)

    @staticmethod
    def _validate_ready_contract(contract):
        human = {c.get('description') for c in contract.get('criteria',[]) if isinstance(c,dict)
                 and c.get('verification_type')=='HUMAN_REVIEW' and c.get('required') is True}
        if (contract.get('verification_gaps') != [] or not set(contract.get('human_review_requirements') or []).issubset(human)
                or not isinstance(contract.get('criteria'), list) or not contract['criteria']
                or any(not isinstance(row, dict) or not isinstance(row.get('description'), str)
                       or not row['description'].strip() for row in contract['criteria'])):
            raise ValueError('验收契约仍有缺口或缺少必要条款')
        summary = '\n'.join(row['description'] for row in contract['criteria'])
        if len(summary) > 4000:
            raise ValueError('正式验收摘要超过可批准长度')
        return summary

    def approve_goal_contract(self, ticket_id, goal_revision, acceptance_revision, expected_ticket_revision,
                              approval_event_id, *, actor_id, source='web', tenant='default', source_thread_id=None,
                              require_signoff=False):
        from .goal_engine import validate_outer_authorization

        identity = self._source(source, approval_event_id, actor_id, tenant, source_thread_id)
        if type(require_signoff) is not bool:
            raise Problem('人工签收选项无效')
        if source == 'feishu' and not identity[4]:
            raise Problem('飞书批准需要关联原卡片', 409)
        self.workbench.goal_engine()  # 外层标志不得绕过执行入口配置。
        with self._transaction() as db:
            ticket = self._ticket(db, ticket_id, identity[2], identity[3])
            event, duplicate = self._insert(db, identity, ticket_id, {
                'type': 'approval', 'ticket_id': ticket_id, 'goal_revision': goal_revision,
                'acceptance_revision': acceptance_revision, 'expected_ticket_revision': expected_ticket_revision,
                **({'require_signoff': True} if require_signoff else {}),
            })
            if duplicate:
                stored = json.loads(event['result'])
                record = db.execute('SELECT body FROM outer_revisions WHERE ticket=? AND kind=? AND revision=?',
                                    (ticket_id, 'approval_snapshot', stored['mandate_revision'])).fetchone()
                return {**json.loads(record[0]), 'duplicate': True}
            if identity[4]:
                card = self._card(db, source, identity[3], identity[4])
                if not card or (card['ticket_id'], card['ticket_revision'], card['goal_revision'], card['acceptance_revision']) != (
                        ticket_id, expected_ticket_revision, goal_revision, acceptance_revision):
                    raise Problem('批准卡片与当前契约版本不匹配', 409)
            self._compare(ticket, expected_ticket_revision)
            if db.execute("SELECT 1 FROM outer_events WHERE ticket=? AND role IS NOT NULL "
                          "AND state IN ('PENDING','RUNNING') LIMIT 1", (ticket_id,)).fetchone():
                raise Problem('工单仍有待处理的新消息，请查看最新契约后批准', 409)
            _revision(goal_revision, '目标版本')
            _revision(acceptance_revision, '验收版本')
            status = ticket.get('status_before_pause') if ticket['status'] == 'PAUSED' and ticket['task_id'] else ticket['status']
            if (status != 'AWAITING_APPROVAL' or ticket['goal_revision'] != goal_revision
                    or ticket['acceptance_revision'] != acceptance_revision):
                raise Problem('批准所指目标、验收或工单状态已变化', 409)
            budget_minutes = ticket['approval_terms']['budget_seconds'] // 60
            mandate = dict(ticket_id=ticket_id, revision=ticket['mandate_revision']+1, goal_revision=goal_revision,
                           acceptance_revision=acceptance_revision, **ticket['approval_terms'],
                           actor_id=actor_id, approval_event_id=event['id'], source=source, approved_at=now())
            goal = {**ticket['goal_contract'], 'status': 'APPROVED'}
            acceptance = {**ticket['acceptance_contract'], 'status': 'APPROVED'}
            if require_signoff:
                acceptance['require_signoff'] = True
            outer = dict(ticket_id=ticket_id, goal_revision=goal_revision, acceptance_revision=acceptance_revision,
                         mandate_revision=mandate['revision'], approval_event_id=event['id'], actor_id=actor_id,
                         correlation_id=ticket['correlation_id'], goal_contract=goal, acceptance_contract=acceptance,
                         mandate=mandate)
            summary = goal['goal_statement']
            try:
                criteria = self._validate_ready_contract(acceptance)
                workspace = self.store.workspace(ticket_id, generation=mandate['revision'])
                if mandate['scope'] != _SCOPE or mandate['forbidden_operations'] != _FORBIDDEN:
                    raise ValueError('工作区权限超出可信范围')
                validate_outer_authorization(outer, ticket_id=ticket_id, workspace=workspace,
                                             budget_seconds=budget_minutes * 60)
            except (ValueError, KeyError, TypeError):
                raise Problem('验收或授权契约未通过批准前完整性校验', 409) from None
            options = dict(request_id=str(uuid.UUID(event['id'][4:])), name=summary[:80], goal=summary,
                           acceptance=criteria, budget_minutes=budget_minutes,
                           role_models=dict.fromkeys(('plan', 'execute', 'review'), 'gpt-6.1-sol'))
            if ticket['task_id']:
                self._replace_authorized(db, ticket, options, outer)
            else:
                self.store.create(options, source='feishu' if source == 'feishu' else 'board', workflow='goal',
                                  _db=db, _outer=outer)
            ticket.update(task_id=ticket_id, status='AUTHORIZED', revision=ticket['revision']+1,
                          goal_contract=goal, acceptance_contract=acceptance, mandate=mandate,
                          mandate_revision=mandate['revision'], error='')
            ticket.pop('status_before_pause', None)
            self._save(db, ticket)
            self._record_revision(db, ticket_id, 'mandate', mandate['revision'], mandate)
            self._record_revision(db, ticket_id, 'approval_snapshot', mandate['revision'], ticket)
            self._finish(db, event, 'DONE', {'action': 'AUTHORIZED', 'mandate_revision': mandate['revision']}, ticket)
            return ticket

    def decide_artifact(self,ticket_id,operation,expected_revision,candidate,artifact_sha256,reason,request_id,
                        *,actor_id,source='web',tenant='default',source_thread_id=None):
        from .goal_engine import decision
        if operation not in ('accept','return'):
            raise Problem('未知作品验收决定')
        reason = _string(reason,'退回原因',2000) if operation=='return' else ''
        identity = self._source(source,request_id,actor_id,tenant,source_thread_id)
        with self._transaction() as db:
            ticket = self._ticket(db,ticket_id,actor_id,tenant)
            event,duplicate = self._insert(db,identity,ticket_id,dict(type='artifact_review',ticket_id=ticket_id,operation=operation,
                revision=expected_revision,candidate=candidate,artifact_sha256=artifact_sha256,reason=reason))
            if duplicate:
                return dict(json.loads(event['result']),duplicate=True)
            self._compare(ticket,expected_revision)
            if source=='feishu':
                card = self._card(db,source,tenant,identity[4]) or {}
                if (card.get('ticket_id'),card.get('ticket_revision'),card.get('candidate'),card.get('artifact_sha256')) != (
                        ticket_id,expected_revision,candidate,artifact_sha256):
                    raise Problem('验收卡与作品版本不一致，请打开最新卡片',409)
            if ticket['status']!='AUTHORIZED' or db.execute("SELECT 1 FROM outer_events WHERE ticket=? AND role IS NOT NULL AND state IN ('PENDING','RUNNING')",(ticket_id,)).fetchone():
                raise Problem('工单存在状态或范围变化，请刷新',409)
            task = json.loads(db.execute('SELECT body FROM tasks WHERE id=?',(ticket_id,)).fetchone()[0])
            state = task['goal_execution']
            reviews = state.get('artifact_reviews') or []
            review = reviews[-1] if reviews else {}
            if (task['paused'] or task['status']!='awaiting_artifact_review' or review.get('state')!='pending'
                    or review['assignment']!=candidate or review['artifact_sha256']!=artifact_sha256
                    or state['checkpoint']['digest']!=artifact_sha256 or review['plan_revision']!=task['version']
                    or any(review[k]!=ticket[k] for k in ('goal_revision','acceptance_revision','mandate_revision'))):
                raise Problem('此作品已处理或版本已变化，请刷新',409)
            approved = operation=='accept'
            if approved:
                for relative in review['files']:
                    self.workbench.artifact_bytes(ticket_id,relative,candidate=candidate)
            review.update(state='accepted' if approved else 'returned',reason=reason,actor=actor_id,source=source,
                          event=event['id'],decided_at=now())
            decision(task,'artifact_'+review['state'],assignment=candidate,digest=artifact_sha256,
                     sequence=review['sequence'],reason=reason,actor=actor_id,source=source,event=event['id'])
            if approved:
                task['plan']['steps'][task['step']]['state']='completed'
                task['step']+=1
                state.update(stage='verified_complete',outcome='VERIFIED_COMPLETE')
                delivery={k:review[k] for k in ('assignment','artifact_sha256','files','level')}
                task.update(status='completed',phase='review',artifacts=list(review['files']),goal_delivery=delivery,
                            review={'passed':True,'findings':['本人已签收作品第 '+str(review['sequence'])+' 版']})
                decision(task,'delivered',assignment=candidate,digest=artifact_sha256,outcome=state['outcome'],
                         correlation_id=ticket['correlation_id'])
            else:
                state.update(stage='repair',outcome='REVISION_REQUESTED')
                task.update(status='queued',phase='execute',error='')
            task['updated_at']=now()
            db.execute('UPDATE tasks SET body=?,updated_at=? WHERE id=?',(_json(task),task['updated_at'],ticket_id))
            ticket['revision']+=1
            self._save(db,ticket)
            result=dict(action='ARTIFACT_'+review['state'].upper(),sequence=review['sequence'],candidate=candidate)
            self._finish(db,event,'DONE',result,ticket)
            return result

    def _replace_authorized(self, db, ticket, options, outer):
        from .goal_engine import active_assignments
        body = db.execute('SELECT body FROM tasks WHERE id=?', (ticket['id'],)).fetchone()
        previous = json.loads(body[0])
        state = previous['goal_execution']
        if previous['status'] not in ('paused', 'completed', 'rejected') or active_assignments(previous):
            raise Problem('范围变化需要先暂停原任务并等待旧派工退出', 409)
        self._record_revision(db, ticket['id'], 'execution_archive', ticket['mandate_revision'], previous)
        active = self.store._initialize_outer(outer)
        generation = outer['mandate_revision']
        self.store._create_workspace(ticket['id'], generation=generation, recoverable=True)
        # 审计记录保留旧产物和执行，但新版从独立工作区与空证据链开始。
        active.update(revisions=state['revisions'], stop_generation=state['stop_generation'] + 1)
        root = previous.copy()
        for field in ('goal_delivery', 'run_id', 'active_call', 'model_group', 'model_processes',
                      'estimated_seconds', 'role_receipts', 'last_run_id'):
            root.pop(field, None)
        root.update(name=options['name'], goal=options['goal'], acceptance=options['acceptance'],
                    goal_execution=active, workspace_generation=generation, status='created', phase='plan',
                    approval=None, plan=None, paused=False, error='', step=0,
                    time_budget_seconds=outer['mandate']['budget_seconds'], elapsed_seconds=0,
                    time_accounting_since=now(), artifacts=[], checks=[], review=None, calls=0,
                    recovery_attempts={}, review_failures=0, updated_at=now())
        db.execute('UPDATE tasks SET body=?,updated_at=? WHERE id=?', (_json(root), root['updated_at'], ticket['id']))

    def control(self, ticket_id, operation, expected_ticket_revision, source_event_id, *, actor_id,
                source='web', tenant='default', source_thread_id=None):
        identity = self._source(source, source_event_id, actor_id, tenant, source_thread_id)
        if operation not in _OPERATIONS:
            raise Problem('不支持的工单操作')
        if source == 'feishu' and not identity[4]:
            raise Problem('飞书操作需要关联原卡片', 409)
        with self._transaction() as db:
            ticket = self._ticket(db, ticket_id, identity[2], identity[3])
            event, duplicate = self._insert(db, identity, ticket_id, {
                'type': operation, 'ticket_id': ticket_id, 'expected_ticket_revision': expected_ticket_revision,
            })
            if duplicate:
                return {**ticket, 'duplicate': True}
            if identity[4]:
                card = self._card(db, source, identity[3], identity[4])
                if not card or (card['ticket_id'], card['ticket_revision']) != (ticket_id, expected_ticket_revision):
                    raise Problem('卡片与工单版本不一致', 409)
            self._compare(ticket, expected_ticket_revision)
            status = ticket['status']
            if status == 'STOPPED':
                raise Problem('工单已经停止', 409)
            if ticket['task_id']:
                self._control_execution(db, ticket, operation, source, event['id'])
            if operation == 'resume':
                if status != 'PAUSED':
                    raise Problem('工单尚未暂停', 409)
                ticket['status'] = ticket.pop('status_before_pause')
            elif operation == 'pause':
                if status == 'PAUSED':
                    raise Problem('工单已经暂停', 409)
                ticket.update(status='PAUSED', status_before_pause=status)
            else:
                ticket['status'] = 'STOPPED'
            ticket['revision'] += 1
            self._save(db, ticket)
            for queued in db.execute("SELECT * FROM outer_events WHERE ticket=? AND state IN ('PENDING','RUNNING') AND role IS NOT NULL",
                                     (ticket_id,)).fetchall():
                if operation == 'stop':
                    self._finish(db, queued, 'CANCELLED', {'error': '工单已停止'}, ticket, deliver=False)
                    db.execute("UPDATE outer_invocations SET state='cancelled',result=? WHERE id=?",
                               (_json({'error': 'cancelled'}), queued['lease']))
                elif json.loads(queued['result'] or '{}').get('binding', {}).get('revision') == expected_ticket_revision:
                    binding = json.loads(queued['result'])
                    binding['binding']['revision'] = ticket['revision']
                    db.execute('UPDATE outer_events SET result=? WHERE id=?', (_json(binding), queued['id']))
            self._finish(db, event, 'DONE', {'action': operation.upper()}, ticket)
            return ticket

    @staticmethod
    def _control_execution(db, ticket, operation, source, event_id):
        from .goal_engine import active_assignments, decision
        row = db.execute('SELECT body FROM tasks WHERE id=?', (ticket['id'],)).fetchone()
        task = json.loads(row[0])
        if task['status'] in ('completed','rejected') and ticket.get('status') == 'AUTHORIZED':
            raise Problem('这版执行已结束，请刷新工单', 409)
        state = task['goal_execution']
        active = active_assignments(task)
        if operation == 'resume':
            if (task['status'] != 'paused' or active or ticket.get('status_before_pause') != 'AUTHORIZED'
                    or ticket['goal_revision'] != state['outer']['goal_revision']):
                raise Problem('旧任务仍在停止或范围已有改动', 409)
            state['stop_requested'] = False
            waiting = state['stage']=='awaiting_artifact_review'
            task.update(paused=False, status='awaiting_artifact_review' if waiting else 'queued', error='')
        else:
            state['stop_generation'] += 1
            state['stop_requested'] = True
            if task['status'] not in ('completed', 'rejected'):
                task.update(paused=True, status='stopping' if active else 'paused')
        decision(task, 'outer_' + operation, event=event_id, source=source, generation=state['stop_generation'])
        task['updated_at'] = now()
        db.execute('UPDATE tasks SET body=?,updated_at=? WHERE id=?',
                   (_json(task), task['updated_at'], ticket['id']))
