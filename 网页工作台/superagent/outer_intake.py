"""V2 接单、澄清、验收提案和授权。外层草案与正式执行共用 SQLite。"""
import base64
from contextlib import contextmanager
from contextvars import ContextVar
import json
from pathlib import PurePosixPath
import re
import time
import uuid

from workbench import Problem, TASK_RE, now, is_blind_goal, omit_call_payloads

from .goal_checks import _SECRET
from .goal_team_records import (
    ADAPTIVE_REVIEW, PREFERENCE_REVIEW_MODES, PROFILE, observation_stale, profile, project,
    public_coordination_decision, public_preference, public_progress, role_limit,
)
from .outer_model import (
    ACCEPTANCE_SCHEMA, INTAKE_SCHEMA, TEAM_ACCEPTANCE_SCHEMA, TEAM_INTAKE_SCHEMA, REQUIREMENT_SOURCES, CodexOuterModel, CodexTicketTeamModel, ModelFailure, role_invoke,
)
from .outer_verification import ROUTER_TEAM, requires_independent_evidence
from .team_profiles import new_team_profile
from .ticket_rounds import changes_old_requirement, round_view, start_round, verified_input


operator_kind = ContextVar('outer_operator_kind', default='owner')

OUTER_WORKERS = 2
_MAX_ATTEMPTS = 2
_MAX_CALLS = 16
_MAX_QUESTIONS = 4
_MAX_TOKENS = 250_000
_LEASE_SECONDS = 145
_TICKET_LIMIT = 12
_MAX_SOURCE_ID = 220
_FIELDS = ('deliverables', 'required_capabilities', 'nonfunctional_requirements',
           'constraints', 'assumptions', 'out_of_scope', 'preferences', 'design_conditions')
_OPERATIONS = ('pause', 'resume', 'stop')
_CLAUSE_BREAKS = re.compile(r'[\n。！？；，、：;!?,:]+')
_PEM = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)', re.DOTALL)
_SCOPE = ['read_workspace', 'write_workspace', 'run_local_tests']
_FORBIDDEN = ['network_tools', 'send', 'publish', 'install', 'payment', 'write_outside_workspace']
_ACTIONS = {'CREATE_DRAFT', 'ATTACH_TO_TICKET', 'ANSWER_CLARIFICATION', 'PROPOSE_CHANGE',
            'REQUEST_TICKET_SELECTION', 'ANSWER_STATUS', 'IGNORE', 'ESCALATE'}
_PROTECTED_SOURCES = {'USER_EXPLICIT', 'USER_CONFIRMED'}
_SUGGESTION_SOURCES = {'MODEL_PROPOSED', 'CONTEXT_DERIVED'}
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

# 识别早期 Router 把已预分配工单自身误当成选单候选的回执。
_MISROUTED_SELF_SELECTION = """e.role='intake_assessor' AND e.state='DONE'
    AND json_extract(e.body,'$.force_new')=1
    AND json_extract(e.result,'$.action')='REQUEST_TICKET_SELECTION'
    AND json_array_length(e.result,'$.choices')=1
    AND json_extract(e.result,'$.choices[0].id')=e.ticket"""


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


def _literal_requirement(text):
    return ' '.join(text.split()).strip('。！？；，、：.!?;,:').casefold()


_OPTIONAL_CLAUSE = re.compile(
    r'^(?:最好|尽量|尽可能|优先考虑|更倾向于?|更喜欢|可选[：:]?|如果(?:可以|方便|有条件)|'
    r'如(?:果)?有(?:可能|条件|合适的|可用的)|有条件的话|能给.{1,40}更好|'
    r'ideally\b|preferably\b|if (?:possible|available)\b|nice to have\b|I(?: would|\'d) prefer\b)', re.I)
_DELEGATE_DIRECTION = re.compile(
    r'^(?:请)?(?:你(?:们)?(?:来|帮我|自己)?(?:决定|选|选择|定方向|拍板)|(?:方案|方向)?(?:由|交给)(?:你们?|团队)(?:来)?(?:决定|选|选择|定)|'
    r'(?:按|采用|就用)团队推荐(?:的)?(?:方案|方向)?|you decide|choose for me)$', re.I)
_OWNER_DIRECTION = re.compile(r'^(?:.*(?:让我|由我|我来|给我)(?:先)?(?:选择|选方向|选方案|挑选)|重要方向由我选择)$')

_LEGACY_PROPOSAL_MESSAGES = {
    '请团队依据原要求重新研判。复杂任务请先比较方案。重要方向由我选择。': 'owner',
    '请团队依据原要求重新研判。复杂任务请先比较方案。采用团队推荐。': 'team',
}
_PROPOSAL_ONLY = frozenset(_literal_requirement(clause)
                           for message in _LEGACY_PROPOSAL_MESSAGES for clause in message.rstrip('。').split('。'))


def _proposal_direction(message, source):
    if source == 'web' and message.get('change_request'):
        return message.get('direction_owner') or _LEGACY_PROPOSAL_MESSAGES.get(message.get('text'))
    return None


def _is_proposal_requirement(description, refs, proposal_refs):
    return (_literal_requirement(description) in _PROPOSAL_ONLY
            or bool(refs) and set(refs).issubset(proposal_refs))


def _grounded_preference(item, source_text):
    # 只移动能在原话中定位的可选要求，不能顺带豁免相邻的必需交付。
    text = item['description'].strip(' 。！？.!?')
    optional = _OPTIONAL_CLAUSE.match(text) or re.search(r'(?:更好|为佳|非必需|不强求|也可以没有|可有可无|is optional)$', text, re.I)
    if not optional:
        return False
    parts = [part.strip() for part in _CLAUSE_BREAKS.split(text) if part.strip()]
    conditional_prefix = r'如果(?:可以|方便)|有条件的话|if (?:possible|available)'
    conditional_suffix = r'如果(?:有|方便|可以)(?:的话)?|如有的话|没有也(?:可以|行)|if (?:possible|available)'
    if len(parts) != 1 and not (len(parts) == 2 and (re.fullmatch(conditional_prefix,parts[0],re.I)
                                                     or re.fullmatch(conditional_suffix,parts[1],re.I))):
        return False
    for ref in item['source_refs']:
        original = [part.strip() for part in _CLAUSE_BREAKS.split(source_text.get(ref,'').strip(' 。！？.!?'))]
        if any(original[index:index + len(parts)] == parts for index in range(len(original))):
            return True
    return False


_OPERATIONAL_LIMIT = re.compile(
    r'隐私|保密|个人信息|个人资料|身份信息|工作区|读写|读取|写入|访问|联网|网络工具|外发|外传|外部发送|'
    r'上传|发送|发布|部署|安装|删除|调用|授权|权限|付费|支付|付款|花钱|扣款|消费|真实支出|账户|凭据|密钥|密码|'
    r'\b(?:privacy|personal data|network|upload|publish|send|install|pay|spend|permission|access|write|read|delete)\b', re.I)
_DESIGN_CONTEXT = re.compile(
    r'场地|场馆|展厅|会场|教室|展台|版面|版幅|纸张|页面尺寸|印刷尺寸|受众|听众|读者年龄|座位|'
    r'活动时长|可用空间|空间有限|字号|字数|海报尺寸|\b(?:venue|floor space|page size|audience)\b', re.I)


def _separate_design_conditions(goal):
    conditions = {item['description']:item for item in goal['design_conditions']}
    controls = {}
    for item in [*goal['constraints'], *goal['design_conditions']]:
        description = item['description']
        if (not _OPERATIONAL_LIMIT.search(description)
                and (_OWNER_DIRECTION.fullmatch(description.strip(' 。.!！'))
                     or _DELEGATE_DIRECTION.fullmatch(description.strip(' 。.!！')))):
            conditions.pop(description,None)
            continue
        if _OPERATIONAL_LIMIT.search(description):
            controls.setdefault(description, item)
            conditions.pop(description,None)
        elif requires_independent_evidence(description):
            if description not in {row['description'] for row in goal['required_capabilities']}:
                goal['required_capabilities'].append(item)
            conditions.pop(description,None)
        elif description in conditions or _DESIGN_CONTEXT.search(description):
            conditions.setdefault(description,item)
        else:
            controls.setdefault(description,item)
    goal['constraints'] = list(controls.values())
    goal['design_conditions'] = list(conditions.values())


def _direction_policy(current_message, previous):
    clauses = [part.strip() for part in _CLAUSE_BREAKS.split(current_message)]
    if any(_OWNER_DIRECTION.fullmatch(part) for part in clauses):
        return {'direction_owner':'owner', 'direction_to_compare':'用户要求先看团队拟出的方向，再作选择。'}
    policy = {key:previous[key] for key in ('direction_owner', 'direction_to_compare') if key in previous}
    if any(_DELEGATE_DIRECTION.fullmatch(part) for part in clauses):
        policy['direction_owner'] = 'team'
    policy.setdefault('direction_owner','owner')
    return policy


def _revision(number, name):
    if type(number) is not int or number <= 0:
        raise Problem(f'缺少有效{name}')


def _draft_file(path, data, *, include_content=False):
    extension = PurePosixPath(path).suffix.lower()
    images = {'.png': ('png', b'\x89PNG\r\n\x1a\n'), '.jpg': ('jpeg', b'\xff\xd8\xff'),
              '.jpeg': ('jpeg', b'\xff\xd8\xff'), '.gif': ('gif', b'GIF8')}
    image = images.get(extension)
    mime = image[0] if image and data.startswith(image[1]) else None
    if extension == '.webp' and data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        mime = 'webp'
    try:
        content = data.decode('utf-8') if not mime and b'\0' not in data else None
    except UnicodeDecodeError:
        content = None
    if mime:
        kind = 'image'
    elif content is None:
        kind = 'binary'
    else:
        kind = {'.html':'html', '.htm':'html', '.md':'markdown', '.markdown':'markdown'}.get(extension, 'text')
    result = {'path':path, 'kind':kind}
    if include_content:
        if content is None:
            content = base64.b64encode(data).decode('ascii')
            if mime:
                content = 'data:image/' + mime + ';base64,' + content
        result['content'] = content
    return result


class OuterIntake:
    def __init__(self, workbench, model=None, registry=None, *, team_model=None):
        self.workbench = workbench
        self.store = workbench.store
        self.model = model if model is not None else CodexOuterModel()
        self.team_model = team_model if team_model is not None else CodexTicketTeamModel()
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
        if source not in ('web', 'feishu', 'dsh'):
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
        if operator_kind.get() != 'owner':
            body['operator_kind'] = operator_kind.get()
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
               ticket_id=None, expected_ticket_revision=None, question_id=None, replace_requirement_ids=None,
               withdraw_requirement_ids=None, workbench_request=None, goal_team=False, allow_model_input=False,
               route_team=False, change_request=False, continuation=None, direction_owner=None):
        identity = self._source(source, source_event_id, actor_id, tenant, source_thread_id)
        _, _, actor, tenant, thread = identity
        allow_model_input = source == 'web' and allow_model_input is True
        if direction_owner is None:
            _string(text, '原始需求', 6000)
        else:
            if (direction_owner not in ('owner','team') or not ticket_id or not allow_model_input
                    or not change_request or text != '' or any(value is not None for value in (
                        source_thread_id, question_id, replace_requirement_ids, withdraw_requirement_ids,
                        workbench_request, continuation))):
                raise Problem('请在原单确认方案处理方式及送模范围', 409)
            _revision(expected_ticket_revision, '工单版本')
        if route_team and (source not in ('web', 'dsh') or not goal_team or workbench_request is None
                           or (source == 'web' and workbench_request.get('allow_model_input') is not True)):
            raise Problem('团队工单须在网页确认本单资料范围后登记', 409)
        if continuation is not None:
            if (not ticket_id or not allow_model_input or not change_request or not isinstance(continuation, dict)
                    or set(continuation) != {'assignment', 'artifact_sha256'} or question_id or replace_requirement_ids
                    or withdraw_requirement_ids or workbench_request is not None):
                raise Problem('请在原单选择已交付版本，确认发送本轮修改意见', 409)
            _revision(expected_ticket_revision, '工单版本')
        if question_id is not None:
            question_id = _string(question_id, '澄清问题编号', _MAX_SOURCE_ID)
        for ids in (replace_requirement_ids, withdraw_requirement_ids):
            if ids is None:
                continue
            if (not isinstance(ids, list) or len(ids) > 12 or any(not isinstance(item, str) for item in ids)
                    or len(set(ids)) != len(ids)):
                raise Problem('请选择有效的旧要求条目')
            _revision(expected_ticket_revision, '工单版本')
        if withdraw_requirement_ids is not None and (
                not withdraw_requirement_ids or replace_requirement_ids is not None or question_id or not change_request):
            raise Problem('请单独选择并明确撤回建议，或另行替换要求')
        with self._transaction() as db:
            linked = self._thread(db, source, tenant, thread)
            requested = ticket_id
            ticket_id = ticket_id or linked
            if workbench_request is not None:
                if (source not in ('web', 'dsh') or ticket_id or thread or question_id
                        or replace_requirement_ids is not None or withdraw_requirement_ids is not None):
                    raise Problem('工作台试点入口仅用于新建工单', 409)
                if db.execute('SELECT 1 FROM tasks WHERE request_id=?', (source_event_id,)).fetchone():
                    raise Problem('同一个请求编号不能切换执行路线', 409)

            row, duplicate = self._insert(db, identity, ticket_id, {
                'type': 'message', 'text': text, 'ticket_id': requested,
                'expected_ticket_revision': expected_ticket_revision, 'question_id': question_id,
                'replace_requirement_ids': replace_requirement_ids,
                **({'withdraw_requirement_ids': withdraw_requirement_ids} if withdraw_requirement_ids else {}),
                **({'change_request': True} if change_request is True else {}),
                **({'direction_owner': direction_owner} if direction_owner is not None else {}),
                **({'continue_from': continuation} if continuation is not None else {}),
                **({'allow_model_input': True} if allow_model_input is True else {}),
                **({'force_new': True, 'workbench_request': workbench_request} if workbench_request is not None else {}),
                **({'goal_team': True} if workbench_request is not None and goal_team else {}),
            }, role='intake_assessor')
            if duplicate:
                return self._event_view(row, True)
            if linked and requested and linked != requested:
                raise Problem('会话与指定工单不一致', 409)
            if (question_id or replace_requirement_ids or withdraw_requirement_ids) and not ticket_id:
                raise Problem('澄清或替换要求必须关联有效工单', 409)
            if route_team:
                ticket = self._new_ticket(db, row, routed=True)
                self._thread(db, 'web', tenant, ticket['id'], ticket['id'])
                if source == 'web':
                    db.execute('UPDATE outer_events SET ticket=?,result=? WHERE id=?',
                               (ticket['id'], _json({'binding': {'revision': ticket['revision']}}), row['id']))
                else:
                    self._hold_for_model_consent(db, row, ticket)
            elif not ticket_id and workbench_request is None and getattr(self.workbench, 'outer_goal_team', False):
                # 未绑定自然语言只在本机登记。用户选定归属之后才可能进入本单团队。
                choices = self._router_choices(db, actor, tenant)
                db.execute('UPDATE outer_events SET role=NULL WHERE id=?', (row['id'],))
                self._finish(db, row, 'DONE', {'action':'REQUEST_TICKET_SELECTION',
                                                'choices':choices, 'router_selection':True})
            elif ticket_id is not None:
                ticket = self._ticket(db, ticket_id, actor, tenant)
                question = ticket.get('question')
                card = self._card(db, source, tenant, thread) if linked else None
                card_question = card['question_id'] if card else None
                explicit_decision = question_id or card_question or replace_requirement_ids or withdraw_requirement_ids or change_request
                team = ticket.get('ticket_team')
                if direction_owner is not None and (
                        not team or ticket['status'] != 'VERIFICATION_GAP' or question or db.execute(
                            "SELECT 1 FROM outer_events WHERE ticket=? AND id!=? AND role IS NOT NULL "
                            "AND state IN ('PENDING','RUNNING') LIMIT 1", (ticket_id,row['id'])).fetchone()):
                    raise Problem('该工单状态已变化，或团队已在重新整理，请刷新原单', 409)
                if team:
                    if source != 'web' and not linked:
                        raise Problem('消息需要绑定原工单会话', 409)
                    if explicit_decision and allow_model_input is not True:
                        raise Problem('请在工作台确认将这条澄清或修改发送给 Codex', 409)
                    if explicit_decision and ticket.get('needs_model_consent'):
                        raise Problem('请先核对并授权发送这张工单的原话', 409)
                if team and not explicit_decision:
                    if expected_ticket_revision is not None:
                        _revision(expected_ticket_revision, '工单版本')
                        if expected_ticket_revision > ticket['revision']:
                            raise Problem('工单版本已变化，请重新查看', 409)
                    self._attach_subscription(db, row, ticket)
                    return self._event_view(db.execute('SELECT * FROM outer_events WHERE id=?',
                                                       (row['id'],)).fetchone())
                if expected_ticket_revision is not None:
                    self._compare(ticket, expected_ticket_revision)
                if continuation is not None:
                    start_round(db, ticket, row, continuation)
                    self._save(db, ticket)
                elif change_request and team and ticket['status'] == 'AUTHORIZED' and ticket.get('task_id'):
                    # 以前浏览器上的“修改要求”也走同一条续改流程。
                    status = db.execute("SELECT json_extract(body,'$.status') FROM tasks WHERE id=?", (ticket_id,)).fetchone()
                    if status and status[0] == 'completed' and not withdraw_requirement_ids:
                        _revision(expected_ticket_revision, '工单版本')
                        start_round(db, ticket, row, None)
                        self._save(db, ticket)
                if replace_requirement_ids:
                    current_goal = ticket['goal_contract'] or {}
                    known = {item['requirement_id'] for field in _FIELDS for item in current_goal.get(field, [])}
                    if not set(replace_requirement_ids).issubset(known):
                        raise Problem('被替换要求已更新或不存在', 409)
                if ticket['status'] in ('PAUSED', 'STOPPED'):
                    raise Problem('工单已暂停或停止', 409)
                if question_id and card_question and question_id != card_question:
                    raise Problem('卡片与指定澄清问题不一致', 409)
                selected_question = question_id or card_question
                if selected_question and (not question or selected_question != question['question_id']):
                    raise Problem('澄清问题已更新，请重新查看', 409)
                if question and not selected_question:
                    raise Problem('请从当前澄清问题提交答案', 409)
                if withdraw_requirement_ids:
                    self._withdraw_suggestions(db, row, ticket, withdraw_requirement_ids)
                else:
                    # 入队时绑定解析后的正式版本，不能用更新后的问题解释旧回复。
                    db.execute('UPDATE outer_events SET result=? WHERE id=?',
                               (_json({'binding': {'revision': ticket['revision'],
                                                   'question_id': question['question_id'] if question else None}}), row['id']))
            row = db.execute('SELECT * FROM outer_events WHERE id=?', (row['id'],)).fetchone()
            return self._event_view(row)

    def _withdraw_suggestions(self, db, event, ticket, required_ids):
        prior = ticket.get('goal_contract')
        if not ticket.get('ticket_team') or not prior:
            raise Problem('当前工单没有可撤回的团队建议', 409)
        selected = set(required_ids)
        removed = [
            dict(item, field=field, withdrawn_by=event['id'])
            for field in _FIELDS for item in prior.get(field, ())
            if item['requirement_id'] in selected and item['source'] in _SUGGESTION_SOURCES
        ]
        if len(removed) != len(selected):
            raise Problem('只可撤回当前版本中由模型或上下文提出的建议', 409)
        if db.execute("""SELECT 1 FROM outer_events WHERE ticket=? AND id<>?
                         AND role IS NOT NULL AND state IN ('PENDING','RUNNING') LIMIT 1""",
                      (ticket['id'], event['id'])).fetchone():
            raise Problem('本单仍有待处理的修改，请核对最新条目后再撤回', 409)
        goal = {**prior, **{
            field: [item for item in prior.get(field, ()) if item['requirement_id'] not in selected]
            for field in _FIELDS
        }}
        goal.update(revision=ticket['goal_revision']+1, status='DRAFT', created_at=now(), created_by='owner',
                    source_refs=list(dict.fromkeys([*prior['source_refs'], event['id']])),
                    withdrawn_requirements=[*prior.get('withdrawn_requirements', []), *removed],
                    changes={field:{'added':[],'removed':[item['description'] for item in removed if item['field']==field]}
                             for field in dict.fromkeys(item['field'] for item in removed)})
        self._record_revision(db, ticket['id'], 'goal', goal['revision'], goal)
        ticket.update(goal_contract=goal, goal_revision=goal['revision'], acceptance_contract=None,
                      revision=ticket['revision']+1, status='ASSESSING', error='')
        ticket['relevant_thread_refs'].append(event['id'])
        if ticket['task_id']:
            ticket['approval_terms'] = {
                **ticket['approval_terms'],
                'workspace': str(self.store.workspace(ticket['id'], generation=ticket['mandate_revision']+1)),
            }
        self._save(db, ticket)
        if not goal['deliverables']:
            self._needs_attention(db,event,ticket,'撤回后缺少交付物，请在原单写明仍需交付什么')
        else:
            db.execute("UPDATE outer_events SET role='acceptance_designer',result=? WHERE id=?",
                       (_json({'binding':{'revision':ticket['revision']}}), event['id']))

    def _attach_subscription(self, db, event, ticket):
        message = json.loads(event['body'])
        # 原始消息保存在事件账本。可供执行读取的备注保持现有脱敏及来源格式。
        db.execute('INSERT INTO inbox_notes VALUES(?,?,?,?)',
                   (('web-' if event['source'] == 'web' else '') + event['id'], ticket['id'],
                    _model_text(message['text']), event['received_at']))
        db.execute('UPDATE outer_events SET role=NULL WHERE id=?', (event['id'],))
        self._finish(db, event, 'DONE', {'action': 'ATTACH_TO_TICKET'}, ticket, deliver=False)

    @staticmethod
    def _intake_can_use_materials(ticket):
        if not ticket or not ticket.get('ticket_team') or ticket.get('needs_model_consent'):
            return False
        return not ticket.get('task_id') or bool(ticket.get('work_round') and ticket['status'] in (
            'ASSESSING','AWAITING_CLARIFICATION','AWAITING_APPROVAL','VERIFICATION_GAP','NEEDS_ATTENTION'))

    @staticmethod
    def _intake_materials(db,event):
        rows = db.execute("""SELECT id,body FROM outer_events WHERE ticket=? AND actor=? AND tenant=?
            AND state='DONE' AND json_extract(result,'$.action')='ATTACH_TO_TICKET'
            AND json_type(body,'$.allow_model_input')='true' ORDER BY rowid DESC LIMIT 8""",
                          (event['ticket'],event['actor'],event['tenant'])).fetchall()
        return [{'event_id':row['id'],'text':_model_text(json.loads(row['body'])['text'],1500)}
                for row in reversed(rows)]

    @staticmethod
    def _router_choices(db, actor, tenant):
        rows = db.execute("""SELECT body FROM outer_tickets WHERE actor=? AND tenant=?
            AND json_extract(body,'$.ticket_team.workflow_profile')=?
            AND json_extract(body,'$.status') NOT IN ('PAUSED','STOPPED')
            ORDER BY updated_at DESC,id LIMIT ?""", (actor, tenant, ROUTER_TEAM, _TICKET_LIMIT))
        tickets = [json.loads(body) for (body,) in rows]
        return [{'id':ticket['id'], 'summary':ticket.get('workbench_name')
                 or (ticket.get('goal_contract') or {}).get('goal_statement')
                 or '待授权工单 ' + ticket['id'][-8:], 'revision':ticket['revision']}
                for ticket in tickets]

    def _hold_for_model_consent(self, db, event, ticket):
        ticket['needs_model_consent'] = True
        ticket['router_draft_event'] = event['id']
        self._save(db, ticket)
        db.execute('UPDATE outer_events SET role=NULL WHERE id=?', (event['id'],))
        self._finish(db, event, 'DONE', {'action':'AWAITING_MODEL_AUTHORIZATION'}, ticket)

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
            router = selection.get('router_selection') is True
            if router and identity[0] not in (original['source'], 'web'):
                raise Problem('请从原消息来源或网页工作台选择归属', 409)
            row, duplicate = self._insert(db, identity, ticket_id, {
                'type': 'selection', 'selected_from': event_id, 'ticket_id': ticket_id,
                'force_new': ticket_id is None, 'text': json.loads(original['body'])['text'],
                **({'original_source': original['source']} if router else {}),
            }, role='intake_assessor')
            if duplicate:
                return self._event_view(row, True)
            if selection.get('selected_by'):
                raise Problem('已经处理此工单选择', 409)
            if router and ticket_id is None:
                ticket = self._new_ticket(db, row, routed=True)
                ticket['correlation_id'] = original['id']
                ticket['original_request_refs'] = [original['id']]
                ticket['relevant_thread_refs'] = [original['id'], row['id']]
                self._thread(db, 'web', identity[3], ticket['id'], ticket['id'])
                self._thread(db, original['source'], identity[3], json.loads(original['body']).get('source_thread_id'),
                             ticket['id'])
                self._hold_for_model_consent(db, row, ticket)
                db.execute('UPDATE outer_events SET ticket=? WHERE id=?', (ticket['id'], original['id']))
            elif ticket_id is not None:
                choice = next((item for item in selection['choices'] if item['id'] == ticket_id), None)
                if choice is None:
                    raise Problem('工单不在当前选项中', 409)
                ticket = self._ticket(db, ticket_id, identity[2], identity[3])
                self._compare(ticket, choice['revision'])
                if ticket['status'] in ('PAUSED', 'STOPPED'):
                    raise Problem('工单已暂停或停止', 409)
                question = ticket['question']
                if ticket.get('ticket_team'):
                    if router:
                        self._thread(db, original['source'], identity[3], json.loads(original['body']).get('source_thread_id'),
                                     ticket_id)
                    self._attach_subscription(db, row, ticket)
                elif question:
                    self._finish(db, row, 'DONE', {'action': 'CLARIFY', 'question': question,
                                                  'ticket_revision': ticket['revision']}, ticket)
                else:
                    db.execute('UPDATE outer_events SET result=? WHERE id=?',
                               (_json({'binding': {'revision': ticket['revision'], 'question_id': None}}), row['id']))
            selection['selected_by'] = row['id']
            db.execute('UPDATE outer_events SET result=? WHERE id=?', (_json(selection), event_id))
            return self._event_view(db.execute('SELECT * FROM outer_events WHERE id=?', (row['id'],)).fetchone())

    def authorize_model_input(self, ticket_id, revision, request_id, *, actor_id, source='web', tenant='default'):
        if source != 'web':
            raise Problem('请在网页原单核对并授权发送原话', 409)
        identity = self._source(source, request_id, actor_id, tenant, None)
        with self._transaction() as db:
            ticket = self._ticket(db, ticket_id, identity[2], identity[3])
            event, duplicate = self._insert(db, identity, ticket_id, {
                'type':'model_input_authorization', 'ticket_id':ticket_id,
                'expected_ticket_revision':revision, 'allow_model_input':True,
            })
            if duplicate:
                return self._event_view(event, True)
            self._compare(ticket, revision)
            if (not ticket.get('ticket_team') or not ticket.get('needs_model_consent')
                    or ticket['status'] != 'DRAFT'):
                raise Problem('当前工单没有待确认的原话发送授权', 409)
            source_event = db.execute("""SELECT * FROM outer_events WHERE id=? AND ticket=? AND state='DONE'
                AND json_extract(result,'$.action')='AWAITING_MODEL_AUTHORIZATION'""",
                                      (ticket.get('router_draft_event'), ticket_id)).fetchone()
            if source_event is None:
                raise Problem('本单原话记录已变化，请重新查看', 409)
            ticket.update(needs_model_consent=False, allow_model_input=True, model_input_authorization=event['id'],
                          revision=ticket['revision'] + 1)
            self._save(db, ticket)
            db.execute("""UPDATE outer_events SET state='PENDING',role='intake_assessor',result=?
                          WHERE id=?""", (_json({'binding': {'revision':ticket['revision']},
                                                 'model_input_authorization':event['id']}), source_event['id']))
            self._finish(db, event, 'DONE', {'action':'MODEL_INPUT_AUTHORIZED',
                                            'intake_event_id':source_event['id']}, ticket, deliver=False)
            return self._event_view(db.execute('SELECT * FROM outer_events WHERE id=?', (event['id'],)).fetchone())

    def event(self, event_id, actor_id, *, tenant='default'):
        actor, tenant = self._owner(actor_id, tenant)
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM outer_events WHERE id=? AND actor=? AND tenant=?',
                             (event_id, actor, tenant)).fetchone()
            if not row:
                raise Problem('事件不存在或无权访问', 404)
            return self._event_view(row)

    def get(self, ticket_id, actor_id, *, tenant='default', request_feedback=False):
        from .development_delivery import release_view

        actor, tenant = self._owner(actor_id, tenant)
        task = {}
        with self.store.connect() as db:
            db.execute('BEGIN')
            ticket = self._ticket(db, ticket_id, actor, tenant)
            if request_feedback:
                ticket['last_request_error'] = db.execute(
                    f'SELECT ({_LAST_REQUEST_ERROR}) FROM outer_tickets d WHERE d.id=?',(ticket_id,)).fetchone()[0]
            original = db.execute('SELECT body FROM outer_events WHERE id=? AND ticket=?',
                                  (ticket['original_request_refs'][0], ticket_id)).fetchone()
            if original:
                message = json.loads(original[0])
                request = message.get('workbench_request')
                if request:
                    ticket['original_request'] = {key: request[key] for key in ('goal','acceptance')}
                elif ticket.get('ticket_team'):
                    ticket['original_request'] = {'text':message['text']}
            if ticket['task_id']:
                raw_task = db.execute('SELECT body FROM tasks WHERE id=?', (ticket['task_id'],)).fetchone()
                if raw_task is None:
                    raise Problem('工单执行记录暂不可用', 409)
                task = json.loads(raw_task[0])
                if release := release_view(db, task):
                    ticket['development_delivery'] = release
                previous = []
                for gen,reviews,delivery,work_round,change_request in db.execute("""SELECT revision,json_extract(body,'$.goal_execution.artifact_reviews'),
                        json_extract(body,'$.goal_delivery'),json_extract(body,'$.work_round_number'),
                        json_extract(body,'$.goal_execution.outer.goal_contract.continued_from.change_request')
                        FROM outer_revisions WHERE ticket=? AND kind='execution_archive'
                        ORDER BY revision""",(ticket_id,)):
                    records=json.loads(reviews or '[]')
                    if delivery and not records:
                        records=[dict(json.loads(delivery),state='automatic',sequence=1)]
                    previous.extend(dict(mandate_revision=gen,round=work_round or 1,change_request=change_request,**{k:r.get(k) for k in (
                        'sequence','state','assignment','artifact_sha256','files','reason')}) for r in records)
                if previous: ticket['prior_deliveries']=previous
            if ticket.get('ticket_team'):
                ticket['subscription'] = self._subscription(db, ticket_id, tenant, task)
            self._project_action_state(db, ticket, task)
        ticket.pop('work_round', None)
        if is_blind_goal(task):
            ticket['blind_review'] = self.workbench.blind_review().get(ticket_id,actor_id=actor,tenant=tenant)
            ticket['blind_history'] = self.workbench.blind_review().history(ticket_id,actor_id=actor,tenant=tenant)
        if task:
            ticket['execution'] = project(omit_call_payloads(task), blind_review=ticket.get('blind_review'))
        return ticket

    @staticmethod
    def _subscription(db, ticket_id, tenant, task):
        bindings = [{'source': source, 'thread_id': thread} for source, thread in db.execute(
            'SELECT source,thread FROM outer_threads WHERE tenant=? AND ticket=? ORDER BY source,thread',
            (tenant, ticket_id))]
        execution = task.get('goal_execution') or {}
        handed_to_team = {
            ref['event_id'] for assignment in execution.get('assignments', ())
            for call in assignment.get('role_calls', ()) if call.get('status') == 'completed'
            for ref in call.get('material_refs', ())
        }
        handed_to_team.update(row['event_id'] for row in db.execute("""
            SELECT json_extract(material.value,'$.event_id') AS event_id
            FROM outer_invocations AS invocation
            JOIN outer_events AS event ON event.id=invocation.event
            JOIN json_each(invocation.snapshot,'$.authorized_intake_materials') AS material
            WHERE invocation.ticket=? AND event.ticket=? AND event.tenant=?
              AND invocation.role='intake_assessor' AND invocation.state='accepted'
            """, (ticket_id, ticket_id, tenant)))
        artifact_reviews = execution.get('artifact_reviews') or []
        delivery = task.get('goal_delivery') or (artifact_reviews[-1] if artifact_reviews else {})
        candidate_refs = {ref['event_id'] for ref in delivery.get('material_refs', ())}
        sealed = (bool(delivery.get('artifact_sha256'))
                  and task.get('status') in ('awaiting_artifact_review', 'completed', 'rejected'))
        handoff = execution.get('team_pending_handoff') or {}
        draft_sealed = task.get('status') == 'awaiting_preference' and bool(handoff.get('base_sha256'))
        draft_refs = {
            ref['event_id'] for assignment in execution.get('assignments', ())
            if assignment['id'] == handoff.get('source_assignment')
            for call in assignment.get('role_calls', ()) if call.get('status') == 'completed'
            for ref in call.get('material_refs', ())
        }
        entries = []
        for row in db.execute("""SELECT id,source,body,received_at FROM outer_events
                WHERE ticket=? AND tenant=? AND state='DONE' AND json_extract(result,'$.action')='ATTACH_TO_TICKET'
                ORDER BY received_at DESC,rowid DESC LIMIT 30""", (ticket_id, tenant)):
            message = json.loads(row['body'])
            allowed = message.get('allow_model_input') is True
            entries.append(dict(event_id=row['id'], source=message.get('original_source', row['source']),
                                received_at=row['received_at'],
                                text=_model_text(message['text']), allow_model_input=allowed,
                                handed_to_team=row['id'] in handed_to_team,
                                candidate_unaffected=allowed and sealed and row['id'] not in candidate_refs,
                                draft_unaffected=allowed and draft_sealed and row['id'] not in draft_refs))
        return dict(bindings=bindings, entries=entries)

    def _project_action_state(self, db, ticket, task, *, brief=False):
        state = task.get('goal_execution') or {}
        status = task.get('status')
        if ticket.get('ticket_team'):
            ticket['continuation'] = round_view(ticket, task, brief=brief)
        ticket.update(public_progress(task), coordination_decision=public_coordination_decision(task),
                      observation_stale=observation_stale(state))
        if status in ('blocked','budget_exhausted') and not is_blind_goal(task):
            ticket['execution_error'] = task.get('error')
        if ((ticket['status'] in ('NEEDS_ATTENTION','VERIFICATION_GAP')
             or status in ('blocked','budget_exhausted'))
                and not all(self._remaining(db, ticket).values())):
            ticket['requirements_edit_blocked'] = '已达到外层模型预算。'

    def _draft_preview_handoff(self, ticket_id, actor, tenant, expected):
        from .goal_engine import digest

        # 状态和工单授权来自同一数据库快照，文件路径仅取已登记的派工。
        with self.store.connect() as db:
            db.execute('BEGIN')
            ticket = self._ticket(db, ticket_id, actor, tenant)
            row = db.execute('SELECT body FROM tasks WHERE id=?', (ticket_id,)).fetchone()
        task = json.loads(row[0]) if row else {}
        state = task.get('goal_execution') or {}
        outer = state.get('outer') or {}
        question = state.get('pending_preference') or {}
        handoff = state.get('team_pending_handoff') or {}
        review = handoff.get('handoff') or {}
        step = task.get('step', 0)
        mode = handoff.get('mode')
        key = handoff.get('source_assignment', '')
        current = tuple(question.get(field) for field in ('id','revision','candidate_digest','draft_sha256'))
        if (ticket['status'] != 'AUTHORIZED' or ticket.get('task_id') != ticket_id
                or task.get('workflow') != 'goal' or task.get('status') != 'awaiting_preference'
                or state.get('stage') != 'awaiting_preference' or task.get('paused') or state.get('stop_requested')
                or not profile(state) or is_blind_goal(task) or outer.get('ticket_id') != ticket_id
                or outer.get('mandate_revision') != ticket.get('mandate_revision')
                or current != expected or question.get('answer') is not None or question.get('requires_option') is not True
                or type(step) is not int or step < (0 if mode == ADAPTIVE_REVIEW else 1)
                or question.get('step') != step or handoff.get('step') != step
                or question.get('plan_revision') != task.get('version') or handoff.get('version') != task.get('version')
                or question.get('stop_generation') != state.get('stop_generation')
                or question.get('mandate_digest') != digest(state.get('mandate'))
                or question.get('phase') != 'execute' or handoff.get('phase') != 'execute'
                or mode not in PREFERENCE_REVIEW_MODES or handoff.get('decision_required') is not True
                or (mode == ADAPTIVE_REVIEW and (outer.get('mandate') or {}).get('workflow_profile') != ROUTER_TEAM)
                or question.get('assignment') != key or not isinstance(key, str) or not re.fullmatch(r'[a-f0-9]{32}', key)
                or handoff.get('base_sha256') != expected[3] or not handoff.get('files')
                or review.get('draft_sha256') != expected[3]
                or handoff.get('goal_digest') != digest(outer.get('goal_contract'))
                or handoff.get('contract_digest') != digest(outer.get('acceptance_contract'))):
            raise Problem('问题或初稿已更新，请重新打开原单', 409)
        return question, handoff

    def draft_preview(self, ticket_id, question_id, question_revision, candidate_digest, draft_sha256,
                      *, relative=None, actor_id, tenant='default'):
        from .goal_engine import digest
        from .goal_runtime import attempt_file
        from .outer_verification import capture

        _revision(question_revision, '问题版本')
        if any(not isinstance(value, str) or not re.fullmatch(pattern, value) for value, pattern in (
                (question_id, r'[a-f0-9]{32}'), (candidate_digest, r'[a-f0-9]{64}'), (draft_sha256, r'[a-f0-9]{64}'))):
            raise Problem('缺少有效问题与初稿版本')
        actor, tenant = self._owner(actor_id, tenant)
        expected = (question_id, question_revision, candidate_digest, draft_sha256)
        question, handoff = self._draft_preview_handoff(ticket_id, actor, tenant, expected)
        if relative is not None and (not isinstance(relative, str) or relative not in handoff['files']):
            raise Problem('初稿中没有该文件', 404)
        sealed = attempt_file(self.store.root, ticket_id, handoff['source_assignment'], 'sealed')
        try:
            checkpoint, files, occupied = capture(sealed)
        except (OSError, ValueError):
            raise Problem('封存初稿无法核对，请刷新原单', 409) from None
        preference = {'prompt':question['prompt'], 'options':question['options']}
        if (checkpoint['digest'] != draft_sha256 or checkpoint['files'] != handoff['files']
                or any(not directory and path not in files for path, directory in occupied.items())
                or digest({'samples': preference, 'checkpoint': checkpoint}) != candidate_digest):
            raise Problem('封存初稿与当前问题的登记内容不一致', 409)
        if self._draft_preview_handoff(ticket_id, actor, tenant, expected) != (question, handoff):
            raise Problem('问题或初稿已更新，请重新打开原单', 409)
        if relative is not None:
            return _draft_file(relative, files[relative], include_content=True)

        review = handoff['handoff'].get('draft_review') or {}
        instruction = handoff['handoff'].get('instruction') or {}
        variants = (instruction.get('handoff_scope') or {}).get('variants', [])
        return {
            'files': [_draft_file(path, files[path]) for path in checkpoint['artifacts']],
            'directions': [{key:variant[key] for key in ('option_id','mode','changes','retained','removed_paths')
                            if key in variant} for variant in variants],
            'advisor': {'summary':review['summary'], 'observations':review['observations']} if review else None,
        }

    def list(self, actor_id, *, tenant='default'):
        actor, tenant = self._owner(actor_id, tenant)
        with self.store.connect() as db:
            rows = db.execute(
                f"SELECT d.body, t.body, ({_LAST_REQUEST_ERROR}) FROM outer_tickets d "
                "LEFT JOIN tasks t ON t.id=d.id WHERE d.actor=? AND d.tenant=? "
                "ORDER BY d.updated_at DESC, d.id", (actor,tenant))
            result = []
            for row in rows:
                task = json.loads(row[1] or '{}')
                state = task.get('goal_execution') or {}
                draft = json.loads(row[0])
                draft.pop('router_draft_event', None)
                draft.update(execution_status=task.get('status'), outcome=state.get('outcome'),
                             last_request_error=row[2],
                             last_business_at=max(draft['updated_at'],
                                                  (state.get('decisions') or [{}])[-1].get('at', '')),
                             pending_preference=public_preference(state, task.get('status'), brief=True))
                self._project_action_state(db, draft, task, brief=True)
                draft.pop('work_round', None)
                if is_blind_goal(task):
                    draft['blind_status'] = self.workbench.blind_review().brief(db,draft,task)
                if task.get('status') == 'completed' and task.get('goal_delivery'):
                    delivery = task['goal_delivery']
                    settled = next((attempt.get('settled_at') for attempt in state['assignments']
                                    if attempt['id'] == delivery['assignment']), None)
                    signed = next((item for item in state.get('artifact_reviews', [])
                                   if item['state'] == 'accepted' and item['assignment'] == delivery['assignment']), {})
                    draft.update(delivery_at=signed.get('decided_at') or settled, delivery_sequence=signed.get('sequence'))
                result.append(draft)
        return result

    def calls(self, ticket_id, actor_id, *, tenant='default', call_id=None):
        from .outer_history import ticket_calls
        actor, tenant = self._owner(actor_id, tenant)
        with self.store.connect() as db:
            db.execute('BEGIN')
            document = self._ticket(db, ticket_id, actor, tenant)
            if document.get('task_id'):
                self.workbench.require_public_artifact(ticket_id)
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

    def _new_ticket(self, db, event, *, routed=False):
        ticket_id = _id('wb-')
        ticket = dict(id=ticket_id, correlation_id=event['id'], status='DRAFT', revision=1,
                      goal_revision=0, acceptance_revision=0, mandate_revision=0, goal_contract=None,
                      acceptance_contract=None, question=None, mandate=None, task_id=None,
                      original_request_refs=[event['id']], relevant_thread_refs=[event['id']],
                      questions_asked=0, error='', created_at=now(), updated_at=now(),
                      approval_terms={'workspace': str(self.store.workspace(ticket_id)), 'scope': _SCOPE,
                                     'forbidden_operations': _FORBIDDEN, 'budget_seconds': 3600,
                                      'execution_model': 'gpt-6.2-sol'})
        message = json.loads(event['body'])
        request = message.get('workbench_request')
        if routed:
            frozen = new_team_profile()
            ticket['ticket_team'] = {'id': ticket_id, 'workflow_profile': ROUTER_TEAM}
            ticket['coordinator_profile'] = 'scoped-v1'
            ticket['approval_terms'].update(execution_profile=frozen, execution_model=frozen['roles']['lead'],
                                            workflow_profile=ROUTER_TEAM, team_review_mode=ADAPTIVE_REVIEW)
        if request is not None:
            ticket['coordinator_profile'] = 'scoped-v1'
            ticket['workbench_name'] = request['name']
            if request.get('allow_model_input') is True:
                ticket['allow_model_input'] = True
            if message.get('goal_team') is True:
                if not routed:
                    ticket['approval_terms']['execution_profile'] = PROFILE
                if request.get('team_review_mode'):
                    ticket['approval_terms']['team_review_mode'] = request['team_review_mode']
        db.execute('INSERT INTO outer_tickets VALUES(?,?,?,?,?)',
                   (ticket_id, event['tenant'], event['actor'], _json(ticket), ticket['updated_at']))
        return ticket

    def _context(self, db, event, ticket):
        message = json.loads(event['body'])
        materials = self._intake_materials(db,event) if self._intake_can_use_materials(ticket) else []
        refs = list(dict.fromkeys([*(ticket['original_request_refs'] if ticket else []),
                                  *(ticket['relevant_thread_refs'][-6:] if ticket else []),
                                  *(item['event_id'] for item in materials),event['id']]))
        rows = db.execute(
            'SELECT id,source,body FROM outer_events WHERE id IN (' + ','.join('?' for _ in refs) + ')',
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
        if ticket and (record := ticket.get('work_round')):
            if (message.get('change_request') and not message.get('replace_requirement_ids')
                    and event['role'] == 'intake_assessor'):
                context['propose_supersessions'] = ('遇到和本轮修改直接冲突的旧要求，把旧 requirement_id 和本轮中明确要求修改的'
                                                  '完整分句列入 superseded_requirements。其余旧要求沿用。没有直接替换时填空数组。')
            context['completed_work_refs'] = [record['base']['assignment']]
            context['work_round'] = record['number']
            context['continuation'] = {
                'source_round': record['source_round'], 'files': list(record['base']['files']),
                'change_request': _model_text(record['request']),
                'instruction': '这是对已交付文件的续改。整合先前要求和本轮意见，沿用无关内容。明确冲突时以本轮要求为准，'
                               '在新目标中完整列出仍有效的要求。当前轮获批后只将上轮交付文件作为起点，重新验收。',
            }
        if ticket and (team := ticket.get('ticket_team')):
            context.update(ticket_team=team, workflow_profile=ROUTER_TEAM,
                           team_review_mode=ticket['approval_terms'].get('team_review_mode'))
            proposal_owner = _proposal_direction(message, event['source'])
            if proposal_owner:
                context['proposal_request'] = proposal_owner
                context['team_review_mode'] = ADAPTIVE_REVIEW
                context.pop('propose_supersessions', None)
                context['untrusted_text']['current_message'] = ''
            proposal_refs = {row['id'] for row in rows
                             if _proposal_direction(json.loads(row['body']), row['source'])}
            for ref in proposal_refs:
                context['untrusted_text']['prior_messages'].pop(ref, None)
            if proposal_refs or proposal_owner:
                context['proposal_control_refs'] = sorted(proposal_refs)
                if current := context['goal_contract']:
                    context['goal_contract'] = {**current, **{
                        field: [item for item in current.get(field, ()) if not _is_proposal_requirement(
                            item['description'], item['source_refs'], proposal_refs)]
                        for field in _FIELDS
                    }}
                    context['constraints'] = context['goal_contract']['constraints']
            if self._intake_can_use_materials(ticket):
                context['authorized_intake_materials'] = materials
            for item in materials:
                context['untrusted_text']['prior_messages'].pop(item['event_id'], None)
            if ticket.get('question'):
                context['available_actions'] = ['ANSWER_CLARIFICATION', 'ESCALATE']
            elif message.get('change_request') or message.get('replace_requirement_ids'):
                context['available_actions'] = ['PROPOSE_CHANGE', 'ESCALATE']
            elif message.get('force_new'):
                context['available_actions'] = ['CREATE_DRAFT', 'ESCALATE']
        for row in rows:
            request = json.loads(row['body']).get('workbench_request')
            if request:
                original = {key: _model_text(request[key], limit)
                            for key, limit in (('name', 80), ('acceptance', 4000)) if request[key]}
                context['untrusted_text']['workbench_request'] = {'event_id': row['id'], **original}
        if event['role'] == 'intake_assessor' and message.get('replace_requirement_ids'):
            selected = set(message['replace_requirement_ids'])
            context['replace_requirements'] = [
                {'field': field, 'requirement_id': item['requirement_id'], 'description': item['description']}
                for field in _FIELDS for item in ticket['goal_contract'].get(field, [])
                if item['requirement_id'] in selected]
            context['available_actions'] = ['PROPOSE_CHANGE', 'ESCALATE']
        if event['role'] == 'acceptance_designer':
            from .outer_verification import authorization_constraint_controls
            context['enforced_constraint_controls'] = authorization_constraint_controls(ticket['goal_contract'])
            context['verifier_catalogue'] = self._registry().catalogue()
            context['prior_acceptance'] = None if context.get('proposal_request') else ticket['acceptance_contract']
        elif not ticket and not message.get('force_new'):
            context['candidate_tickets'] = [
                {'id': candidate['id'], 'summary': (candidate['goal_contract'] or {}).get('goal_statement', '')[:300],
                 'status': candidate['status'], 'revision': candidate['revision']}
                for (raw,) in db.execute('SELECT body FROM outer_tickets WHERE actor=? AND tenant=? ORDER BY updated_at DESC LIMIT ?',
                                         (event['actor'], event['tenant'], _TICKET_LIMIT))
                if (candidate := json.loads(raw))['status'] != 'STOPPED' and not candidate.get('ticket_team')
            ]
        return context

    def _remaining(self, db, ticket, event=None):
        if ticket and (record := ticket.get('work_round')):
            rows = db.execute("SELECT result FROM outer_invocations WHERE ticket=? AND json_extract(snapshot,'$.work_round')=?",
                              (ticket['id'], record['number'])).fetchall()
        elif ticket:
            rows = db.execute('SELECT result FROM outer_invocations WHERE ticket=? OR (event=? AND ticket IS NULL)',
                              (ticket['id'], event['id'] if event else None)).fetchall()
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

    def _expire_claim(self, db, event):
        db.execute("UPDATE outer_invocations SET state='lease_expired',result=? WHERE id=? AND state='running'",
                   (_json({'error': 'lease_expired'}), event['lease']))
        if event['attempt'] >= _MAX_ATTEMPTS:
            ticket = self._ticket(db, event['ticket']) if event['ticket'] else None
            self._needs_attention(db, event, ticket, '模型领取已过期，达到重试上限')
        else:
            db.execute("UPDATE outer_events SET state='PENDING',lease=NULL,lease_until=NULL WHERE id=?",
                       (event['id'],))

    def _queue_keys(self, db, event):
        message = json.loads(event['body'])
        thread = message.get('source_thread_id')
        keys = set()
        if thread:
            keys.add(('thread', event['source'], event['tenant'], thread))
        linked = self._thread(db, event['source'], event['tenant'], thread)
        for ticket in (event['ticket'], linked):
            if ticket:
                keys.add(('ticket', ticket))
        if not keys and not message.get('force_new'):
            keys.add(('unrouted', event['tenant'], event['actor']))
        return keys, linked

    def pending_team_tickets(self):
        """仅列出需要本单团队消费的已绑定票，读接口不会唤醒历史队列。"""
        with self.store.connect() as db:
            return [row[0] for row in db.execute(f"""
                SELECT t.id FROM outer_tickets t JOIN outer_events e ON e.ticket=t.id
                WHERE json_extract(t.body,'$.ticket_team.workflow_profile')=?
                    AND json_extract(t.body,'$.status') NOT IN ('PAUSED','STOPPED')
                    AND (e.role IS NOT NULL AND e.state IN ('PENDING','RUNNING')
                         OR json_extract(t.body,'$.status')='DRAFT'
                         AND e.id=json_extract(t.body,'$.original_request_refs[0]') AND ({_MISROUTED_SELF_SELECTION}))
                GROUP BY t.id HAVING SUM(e.state='RUNNING' AND e.lease_until>?)=0
                ORDER BY MIN(e.rowid)
                """, (ROUTER_TEAM, time.time()))]

    def _claim(self, ticket_id=None):
        with self._transaction() as db:
            if ticket_id:
                ticket = self._ticket(db, ticket_id)
                if (ticket.get('ticket_team') or {}).get('workflow_profile') != ROUTER_TEAM:
                    return None
                if ticket['status'] == 'DRAFT':
                    # 旧调用与原话保留，原事件重新入队。不会创建工单或触发实施。
                    db.execute(f"""UPDATE outer_events SET state='PENDING',attempt=0,result=?
                        WHERE id=(SELECT e.id FROM outer_events e WHERE e.ticket=? AND e.id=?
                                  AND ({_MISROUTED_SELF_SELECTION}))""",
                               (_json({'binding': {'revision':ticket['revision']}}),
                                ticket_id, ticket['original_request_refs'][0]))
                scope = 'ticket=?'
                params = (ticket_id,)
            else:
                scope = """NOT EXISTS (SELECT 1 FROM outer_tickets t WHERE t.id=outer_events.ticket
                            AND json_extract(t.body,'$.ticket_team.workflow_profile')=?)"""
                params = (ROUTER_TEAM,)
            # 过期的领取者保留审计；只有新租约可以提交新的正式决定。
            for expired in db.execute(f"""SELECT * FROM outer_events
                    WHERE state='RUNNING' AND lease_until<=? AND {scope}""",
                                      (time.time(), *params)).fetchall():
                self._expire_claim(db, expired)
            running = db.execute(f"SELECT * FROM outer_events WHERE state='RUNNING' AND {scope}", params).fetchall()
            if len(running) >= (1 if ticket_id else OUTER_WORKERS):
                return None
            blocked = set().union(*(self._queue_keys(db, event)[0] for event in running))
            for event in db.execute(f"""SELECT * FROM outer_events WHERE state='PENDING' AND role IS NOT NULL
                    AND {scope} ORDER BY received_at,rowid""", params).fetchall():
                ticket = self._ticket(db, event['ticket']) if event['ticket'] else None
                keys, linked = self._queue_keys(db, event)
                if blocked & keys or ticket and ticket['status'] == 'PAUSED':
                    # 先到的工单/会话保留其顺序，其他独立事项仍可领取。
                    blocked.update(keys)
                    continue
                if ticket and ticket['status'] == 'STOPPED':
                    self._finish(db, event, 'CANCELLED', {'error': '工单已停止'}, ticket)
                    continue
                bound = json.loads(event['result'] or '{}').get('binding', {})
                if ticket and bound.get('revision') != ticket['revision']:
                    self._finish(db, event, 'REJECTED', {'error': '工单版本已变化，请重新提交'}, ticket)
                    continue
                if not ticket and event['role'] == 'intake_assessor' and linked:
                    self._finish(db, event, 'REJECTED', {'error': '会话已关联工单，请按当前工单继续'})
                    continue
                try:
                    snapshot = self._context(db, event, ticket)
                except (ImportError, AttributeError, TypeError, ValueError):
                    self._needs_attention(db, event, ticket, '验收目录无法读取')
                    continue
                if not all(snapshot['remaining_budget'].values()):
                    self._needs_attention(db, event, ticket, '已达到团队研判预算' if ticket_id else '已达到外层模型预算')
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
        """消费历史入口的外层队列。"""
        return self._drain(limit)

    def drain_ticket(self, ticket_id, limit=1):
        """消费指定团队工单，不领取其他工单或无绑定消息。"""
        return self._drain(limit, ticket_id=ticket_id)

    def _drain(self, limit, *, ticket_id=None):
        """模型执行在 SQLite 写事务之外，回执由原领取租约结算。"""
        settled = 0
        for _ in range(min(50, max(1, int(limit)))):
            claim = self._claim(ticket_id)
            if claim is None:
                break
            event, lease, snapshot = claim
            role = event['role']
            try:
                schemas = ((TEAM_INTAKE_SCHEMA, TEAM_ACCEPTANCE_SCHEMA) if ticket_id else
                           (INTAKE_SCHEMA, ACCEPTANCE_SCHEMA))
                response = role_invoke(self.team_model if ticket_id else self.model, role, snapshot,
                                       schemas[0] if role == 'intake_assessor' else schemas[1])
            except ModelFailure as error:
                response = error.receipt()
            self._settle(event['id'], lease, snapshot, response)
            settled += 1
        return settled

    def _settle(self, event_id, lease, snapshot, response):
        with self._transaction() as db:
            event = db.execute('SELECT * FROM outer_events WHERE id=?', (event_id,)).fetchone()
            expired = event['lease'] == lease and event['lease_until'] <= time.time()
            if expired:
                self._expire_claim(db, event)
            if expired or event['lease'] != lease:
                # 迟到的回执归入原调用，保留停办或租约过期的决定。
                db.execute("""UPDATE outer_invocations SET result=json_set(?, '$.settlement_reason',
                              COALESCE(json_extract(result,'$.settlement_reason'),json_extract(result,'$.error')))
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
                        if self._retry_stale_clarification(db,event,ticket,snapshot,response['result']):
                            result_status = 'stale'
                            response['settlement_reason'] = '新授权补充到达，先让本单负责人核对再发澄清'
                        else:
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

    def _retry_stale_clarification(self, db, event, ticket, snapshot, result):
        if not self._intake_can_use_materials(ticket) or not result['proposed_question']['question_text'].strip():
            return False
        binding = json.loads(event['result'] or '{}')
        if binding.get('material_replayed') or snapshot['remaining_budget']['model_calls'] < 2:
            return False
        seen = {row['event_id'] for row in snapshot.get('authorized_intake_materials', ())}
        if not any(row['event_id'] not in seen for row in self._intake_materials(db,event)):
            return False
        binding['material_replayed'] = True
        db.execute("UPDATE outer_events SET state='PENDING',result=?,lease=NULL,lease_until=NULL WHERE id=?",
                   (_json(binding),event['id']))
        return True

    @staticmethod
    def _normal_goal(result, ticket, snapshot, event):
        raw = result['goal_draft']
        statement = _string(raw['goal_statement'], '目标', 2000)
        prior = ticket['goal_contract'] or {}
        source_keys = set(snapshot['relevant_thread_refs'])
        confirms = bool(ticket['question'])
        untrusted = snapshot['untrusted_text']
        actual_text = {**untrusted['prior_messages'], event['id']: untrusted['current_message']}
        actual_text.update({item['event_id']:item['text'] for item in snapshot.get('authorized_intake_materials', ())})
        original = untrusted.get('workbench_request')
        if original:
            ref = original['event_id']
            actual_text[ref] += '\n' + original.get('acceptance', '')
        replacements = set(json.loads(event['body']).get('replace_requirement_ids') or ())
        if snapshot.get('propose_supersessions'):
            eligible = {item['requirement_id']: item['description'] for field in ('deliverables','required_capabilities',
                        'nonfunctional_requirements','preferences') for item in prior.get(field, ())}
            for replacement in result['superseded_requirements']:
                if (replacement['requirement_id'] not in eligible or
                        not _verbatim_clause(replacement['change_clause'], untrusted['current_message'])):
                    raise ValueError('续改提案替换旧要求须引用本轮修改原话')
                # 模型只能建议撤销。新增、保留或无法唯一指向旧条款的意见一律沿用原要求。
                targets = [key for key, old in eligible.items()
                           if changes_old_requirement(replacement['change_clause'], old)]
                if targets == [replacement['requirement_id']]:
                    replacements.add(replacement['requirement_id'])
        goal = dict(ticket_id=ticket['id'], goal_statement=statement, revision=ticket['goal_revision'] + 1,
                    source_refs=sorted(source_keys), created_at=now(), created_by='intake_assessor', status='DRAFT')
        withdrawn = []
        if ticket.get('ticket_team'):
            goal['workflow_profile'] = ROUTER_TEAM
            if record := ticket.get('work_round'):
                goal['continued_from'] = {'round':record['source_round'], 'event':record['event'],
                                          'files':list(record['base']['files']),
                                          'change_request':_model_text(record['request'])}
            # 一轮中的授权随补充保留；签收后下轮的取舍重新依据当轮原话。
            new_round = (ticket.get('work_round') or {}).get('event') == event['id']
            goal.update(_direction_policy(untrusted['current_message'], {} if new_round else prior))
            if proposal_owner := snapshot.get('proposal_request'):
                goal.update(direction_owner=proposal_owner, proposal_requested=True,
                            direction_to_compare='先比较可行方案，再按本单登记的方式确定方向。')
            elif not new_round and prior.get('proposal_requested'):
                goal['proposal_requested'] = True
            if record:
                from .goal_engine import explicit_comparison_request
                comparison = explicit_comparison_request([untrusted['current_message']])
                if comparison is None and not new_round:
                    comparison = prior.get('round_comparison_request')
                if comparison is not None:
                    goal['round_comparison_request'] = comparison
            withdrawn = [*prior.get('withdrawn_requirements', ()), *(
                dict(item, field=field, withdrawn_by=event['id'])
                for field in _FIELDS for item in prior.get(field, ())
                if item['requirement_id'] in replacements and item['source'] in _SUGGESTION_SOURCES
            )]
            if withdrawn:
                goal['withdrawn_requirements'] = withdrawn
        withdrawn_text = {_literal_requirement(item['description']) for item in withdrawn}
        fields = _FIELDS if ticket.get('ticket_team') else tuple(field for field in _FIELDS if field not in ('preferences','design_conditions'))
        for field in fields:
            requirements = raw.get(field, []) + (result['assumptions'] if field == 'assumptions' else [])
            if len(requirements) > 12:
                raise ValueError('目标条目超出上限')
            previous = {item['description']: item for item in prior.get(field, [])}
            if ticket.get('ticket_team'):
                proposal_refs = snapshot.get('proposal_control_refs', ())
                requirements = [item for item in requirements if not _is_proposal_requirement(
                    item['description'], item['source_refs'], proposal_refs)]
                previous = {key:item for key,item in previous.items() if not _is_proposal_requirement(
                    key, item['source_refs'], proposal_refs)}
            normalized = []
            for item in requirements:
                description = _string(item['description'], '目标条目', 500)
                if (_literal_requirement(description) in withdrawn_text
                        and not (item['source'] in _PROTECTED_SOURCES
                                 and event['id'] in item['source_refs']
                                 and _verbatim_clause(description, untrusted['current_message']))):
                    continue
                if any(row['description'] == description for row in normalized):
                    continue
                if description in previous:
                    if previous[description]['requirement_id'] in replacements:
                        raise ValueError('新版草案原样保留了用户指定替换的旧要求')
                    normalized.append(previous[description])
                    continue
                refs = list(dict.fromkeys(item['source_refs']))
                source = item['source']
                if source not in REQUIREMENT_SOURCES:
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
        if ticket.get('ticket_team'):
            _separate_design_conditions(goal)
            # 当前模型和旧草案都可能把“最好有”原样列为必需。仅在原文有锚点时纠正分类。
            preferences = {}
            for item in goal['preferences']:
                description = item['description']
                if _grounded_preference(item, actual_text):
                    preferences[description] = dict(item, source='USER_EXPLICIT') if item['source'] == 'MODEL_PROPOSED' else item
                elif not any(row['description'] == description for field in
                             ('deliverables','required_capabilities','nonfunctional_requirements') for row in goal[field]):
                    # 分组出错不能吞掉另一项正式交付。拿不出可选原话时继续要求覆盖。
                    field = 'required_capabilities' if goal['deliverables'] else 'deliverables'
                    goal[field].append(item)
            for field in ('deliverables', 'required_capabilities', 'nonfunctional_requirements'):
                required = []
                for item in goal[field]:
                    description = item['description']
                    if _grounded_preference(item, actual_text):
                        preferences[description] = dict(item, source='USER_EXPLICIT') if item['source'] == 'MODEL_PROPOSED' else item
                    else:
                        required.append(item)
                        preferences.pop(description, None)
                goal[field] = required
            if not goal['deliverables'] and preferences:
                # 只有一个礼貌提出的成果时，它仍然给出了本单要做的事。
                for item in raw['deliverables']:
                    outcome = preferences.pop(item['description'], None)
                    if outcome:
                        goal['deliverables'].append(outcome)
                        break
            goal['preferences'] = list(preferences.values())
            if len(result['preference_support']) > 24:
                raise ValueError('偏好关联建议数量超出上限')
            linked = {link['derived_description']:preferences[link['preference_quote']]
                      for link in result['preference_support'] if link['preference_quote'] in preferences}
            # 来源是模型并不能说明某条是可选的。必须连到一条有用户原话的偏好，
            # 且这条额外做法和偏好来自同一消息。交付物、明确功能和权限不能借此免验。
            for field in ('required_capabilities', 'nonfunctional_requirements'):
                rows = []
                for item in goal[field]:
                    pref = linked.get(item['description'])
                    if (pref and pref['source'] in _PROTECTED_SOURCES and item['source'] == 'MODEL_PROPOSED'
                            and set(item['source_refs']).intersection(pref['source_refs'])):
                        item = dict(item, acceptance_role='advisory', preference_id=pref['requirement_id'])
                    elif 'acceptance_role' in item:
                        item = {key:value for key,value in item.items() if key not in ('acceptance_role','preference_id')}
                    rows.append(item)
                goal[field] = rows
        if not goal['deliverables'] and not any(result['proposed_question'].values()):
            raise ValueError('草案没有可核查交付物')
        if prior:
            changes = {}
            for field in fields:
                old = {item['description'] for item in prior.get(field, ())}
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
            if action == 'ESCALATE' and ticket and ticket.get('work_round') and ticket['status'] == 'ASSESSING':
                self._needs_attention(db, event, ticket, _model_text(result['reason'], 500) or '本轮修改需要核对')
                return
            state = 'NEEDS_ATTENTION' if action == 'ESCALATE' else 'DONE'
            self._finish(db, event, state, {'action': action}, ticket, deliver=action != 'IGNORE')
            return
        if action == 'REQUEST_TICKET_SELECTION' or (not ticket and action == 'CREATE_DRAFT' and candidates):
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
        defer_question = False
        if ticket.get('ticket_team') and goal['deliverables'] and question['question_text'].strip():
            if question['blocking_scope'] not in ('permissions', 'cost', 'irreversible'):
                from .outer_verification import acceptance_requirements
                required, advisory = acceptance_requirements(goal)
                target_text = {_literal_requirement(text) for text in result['question_targets']}
                advisory_text = {_literal_requirement(item['description']) for item in advisory}
                core_text = {_literal_requirement(item['description']) for item in required}
                only_advisory = bool(target_text) and target_text <= (advisory_text - core_text)
                defer_question = only_advisory or question['blocking_scope'] in ('direction','acceptance')
        if defer_question:
            if question['blocking_scope'] == 'direction':
                goal['direction_to_compare'] = _string(question['question_text'], '待比较的方向', 500)
            # 验收方法由下一角色自行拟定，无须让简单要求进入比较路线。
        elif question['question_text'].strip():
            if (not result['critical_ambiguities'] or question['blocking_scope'] not in _IMPACTS
                    or len(question['options']) > 6 or ticket['questions_asked'] >= _MAX_QUESTIONS):
                raise ValueError('澄清超出预算或缺少关键影响')
            txt = _string(question['question_text'], '澄清问题', 500)
            work_round = (ticket.get('work_round') or {}).get('number', 1)
            asked = {json.loads(row[0])['question_text'] for row in db.execute(
                "SELECT body FROM outer_revisions WHERE ticket=? AND kind='clarification' "
                "AND coalesce(json_extract(body,'$.work_round'),1)=?", (ticket['id'], work_round))}
            if txt in asked:
                raise ValueError('不得重复已经回答的问题')
            next_question = dict(question_id=_id('q-'), goal_revision=goal['revision'],
                                 question_text=txt, reason=_string(question['reason'], '澄清原因', 500),
                                 options=[_string(item, '问题选项', 240) for item in question['options']],
                                 blocking_scope=question['blocking_scope'], answer_status='PENDING')
            if work_round > 1:
                next_question['work_round'] = work_round
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
        if snapshot.get('proposal_request'):
            ticket['approval_terms']['team_review_mode'] = ADAPTIVE_REVIEW
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
        proposal_refs = set(snapshot.get('proposal_control_refs', ()))
        proposal = {**result, 'criteria': [],
                    'source_refs':[ref for ref in result['source_refs'] if ref not in proposal_refs]}
        skipped = set()
        for raw in result['criteria']:
            item = dict(raw)
            if not set(item['source_refs']).issubset(refs) or not item['description'].strip():
                raise ValueError('验收条目来源无效')
            if proposal_refs and _is_proposal_requirement(item['description'], item['source_refs'], proposal_refs):
                skipped.update((item['criterion_id'], item['description']))
                continue
            item['source_refs'] = [ref for ref in item['source_refs'] if ref not in proposal_refs]
            item['test_spec'] = json.loads(item.pop('test_spec_json'))
            item['expected_result'] = json.loads(item.pop('expected_result_json'))
            proposal['criteria'].append(item)
        if skipped:
            proposal['human_review_requirements'] = [
                item for item in result['human_review_requirements'] if item not in skipped]
        goal = ticket['goal_contract']
        compiled = self._registry().compile_proposal(goal, proposal)
        if not isinstance(compiled, dict) or compiled.get('status') not in (
                'READY_FOR_APPROVAL', 'VERIFICATION_GAP', 'NEEDS_CLARIFICATION', 'FAILED'):
            raise ValueError('验收注册表状态不合法')
        if compiled['status'] == 'READY_FOR_APPROVAL':
            self._validate_ready_contract(compiled)
            if (ticket.get('ticket_team') and ticket['approval_terms'].get('team_review_mode') == 'review_and_decide'
                    and sum(row.get('required') is True for row in compiled['criteria']) < 2):
                compiled = {**compiled, 'status': 'VERIFICATION_GAP',
                            'verification_gaps': ['第 2 步定方向需要至少两项可分阶段执行的必需验收。请补充可单独验收的阶段成果。']}
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
            if operator_kind.get() != 'owner':
                mandate['operator_kind'] = operator_kind.get()
            goal = {**ticket['goal_contract'], 'status': 'APPROVED'}
            acceptance = {**ticket['acceptance_contract'], 'status': 'APPROVED'}
            mode = mandate.get('team_review_mode')
            if mode:
                from superagent.goal_team_blind import required_blind
                if required_blind(acceptance) and mode != ADAPTIVE_REVIEW:
                    raise Problem('匿名评测使用已批准的盲评流程，不能叠加方案审稿', 409)
                if mode == 'review_and_decide' and sum(
                        criterion.get('required') is True for criterion in acceptance.get('criteria', ())) < 2:
                    raise Problem('后续阶段定方向需要至少两项可分阶段执行的必需验收', 409)
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
            options = dict(request_id=str(uuid.UUID(event['id'][4:])),
                           name=ticket.get('workbench_name') or summary[:80], goal=summary,
                           acceptance=criteria, budget_minutes=budget_minutes,
                           role_models=dict.fromkeys(('plan', 'execute', 'review'), 'gpt-6.1-sol'))
            profile = ticket.get('coordinator_profile')
            if ticket['task_id']:
                self._replace_authorized(db, ticket, options, outer, profile=profile)
            else:
                self.store.create(options, source='feishu' if source == 'feishu' else 'board', workflow='goal',
                                  _db=db, _outer=outer, _coordinator_profile=profile)
            ticket.update(task_id=ticket_id, status='AUTHORIZED', revision=ticket['revision']+1,
                          goal_contract=goal, acceptance_contract=acceptance, mandate=mandate,
                          mandate_revision=mandate['revision'], error='')
            ticket.pop('status_before_pause', None)
            self._save(db, ticket)
            self._record_revision(db, ticket_id, 'mandate', mandate['revision'], mandate)
            self._record_revision(db, ticket_id, 'approval_snapshot', mandate['revision'], ticket)
            self._finish(db, event, 'DONE', {'action': 'AUTHORIZED', 'mandate_revision': mandate['revision']}, ticket)
            return ticket

    def answer_preference(self, ticket_id, revision, question_id, question_revision, candidate_digest,
                          answer, request_id, *, actor_id, source='web', tenant='default', allow_model_input=False):
        from .goal_engine import decision, digest

        identity = self._source(source, request_id, actor_id, tenant, None)
        if not isinstance(answer, dict) or set(answer) - {'option_id', 'text'}:
            raise Problem('答复格式无效')
        option = _string(answer.get('option_id') or None, '选项', 40, optional=True)
        text = answer.get('text')
        if isinstance(text, str) and len(text) <= 2000 and not text.strip():
            text = None
        _string(text, '意见', 2000, optional=True)
        if not option and not text:
            raise Problem('请选择样例或写下你的意见')
        if type(question_revision) is not int:
            raise Problem('问题版本无效')
        data = {'type':'execution_preference', 'ticket_id':ticket_id, 'revision':revision,
                'question_id':question_id, 'question_revision':question_revision,
                'candidate_digest':candidate_digest, 'option_id':option, 'text':text}
        if text and allow_model_input is True:
            data['allow_model_input'] = True
        with self._transaction() as db:
            ticket = self._ticket(db, ticket_id, identity[2], identity[3])
            event, duplicate = self._insert(db, identity, ticket_id, data)
            if duplicate:
                return self._event_view(event, True)
            if text and ticket.get('ticket_team') and allow_model_input is not True:
                raise Problem('请在工作台确认将补充意见发送给 Codex', 409)
            self._compare(ticket, revision)
            row = db.execute('SELECT body FROM tasks WHERE id=?', (ticket_id,)).fetchone()
            task = json.loads(row[0]) if row else {}
            state = task.get('goal_execution') or {}
            question = state.get('pending_preference') or {}
            handoff = state.get('team_pending_handoff') or {}
            current = (question.get('id'), question.get('revision'), question.get('candidate_digest'))
            if (not question or ticket['status'] != 'AUTHORIZED' or task.get('status') != 'awaiting_preference'
                    or current != (question_id, question_revision, candidate_digest) or question.get('answer')
                    or question.get('stop_generation') != state.get('stop_generation')
                    or question.get('plan_revision') != task.get('version')
                    or question.get('step', task.get('step')) != task.get('step')
                    or question.get('mandate_digest') != digest(state.get('mandate'))):
                raise Problem('问题或候选已更新，请重新打开原单', 409)
            if question.get('requires_option'):
                if (question['assignment'] != handoff.get('source_assignment')
                        or question['draft_sha256'] != handoff.get('base_sha256')):
                    raise Problem('草稿已更新，请重新打开原单', 409)
                if not option:
                    raise Problem('请先选择后续方向，也可以补充意见')
            selected = next((row for row in question['options'] if row['id'] == option), None)
            if option and selected is None:
                raise Problem('请选择当前工单的有效选项')
            if question.get('requires_option') and text:
                instruction = handoff.get('handoff', {}).get('instruction', {})
                variants = (instruction.get('handoff_scope') or {}).get('variants', [])
                if any(variant['option_id'] == option and variant['mode'] == 'keep' for variant in variants):
                    raise Problem('沿用现稿请清空意见。有补充意见时请选择可修改的方向。', 409)
            answer = dict(option_id=option, option=selected, text=text, event=event['id'], source=source,
                          operator_kind=operator_kind.get())
            question['answer'] = answer
            state['stage'] = question['phase']
            task.update(status='queued', phase=question['phase'], error='', updated_at=now())
            decision(task, 'preference_answered', question=question_id, candidate=candidate_digest,
                     step=question.get('step'), **answer)
            db.execute('UPDATE tasks SET body=?,updated_at=? WHERE id=?',
                       (_json(task), task['updated_at'], ticket_id))
            ticket['revision'] += 1
            self._save(db, ticket)
            self._finish(db, event, 'DONE', {'action':'PREFERENCE_RECORDED'}, ticket)
            return self._event_view(db.execute('SELECT * FROM outer_events WHERE id=?', (event['id'],)).fetchone())

    def extend_team_budget(self, ticket_id, revision, mandate_revision, minutes, roles, request_id, *,
                           actor_id, source='web', tenant='default'):
        from .goal_engine import active_assignments, decision, validate_outer_authorization

        identity = self._source(source, request_id, actor_id, tenant, None)
        _revision(mandate_revision, '授权版本')
        if (type(minutes) is not int or minutes not in (0,15,30,60)
                or type(roles) is not int or roles not in (0,5,10) or not (minutes or roles)):
            raise Problem('请选择具体的追加时间或角色轮次')
        data = dict(type='execution_budget', ticket_id=ticket_id, revision=revision,
                    mandate_revision=mandate_revision, minutes=minutes, roles=roles)
        with self._transaction() as db:
            ticket = self._ticket(db, ticket_id, identity[2], identity[3])
            event, duplicate = self._insert(db, identity, ticket_id, data)
            if duplicate:
                return self._event_view(event, True)
            self._compare(ticket, revision)
            row = db.execute('SELECT body FROM tasks WHERE id=?', (ticket_id,)).fetchone()
            task = json.loads(row[0]) if row else {}
            state = task.get('goal_execution') or {}
            if (not profile(state) or task.get('status') != 'budget_exhausted' or task.get('paused')
                    or ticket['status'] != 'AUTHORIZED' or mandate_revision != ticket['mandate_revision']
                    or active_assignments(task) or state['stop_requested']):
                raise Problem('当前授权或工单状态已变化，不可追加资源', 409)
            old = ticket['mandate']
            new = {**old, 'revision':old['revision']+1, 'actor_id':actor_id,
                   'approval_event_id':event['id'], 'source':source, 'approved_at':now(),
                   'budget_seconds':old['budget_seconds']+minutes*60,
                   'team_role_limit':role_limit(state)+roles}
            operator = operator_kind.get()
            if operator == 'owner':
                new.pop('operator_kind',None)
            else:
                new['operator_kind'] = operator
            outer = {**state['outer'], 'mandate':new, 'mandate_revision':new['revision'],
                     'approval_event_id':event['id'], 'actor_id':actor_id}
            validate_outer_authorization(outer, ticket_id=ticket_id, workspace=self.store.workspace(ticket_id),
                                         budget_seconds=new['budget_seconds'])
            state.update(outer=outer, mandate=None)
            task.update(status='queued', time_budget_seconds=new['budget_seconds'], error='', updated_at=now())
            decision(task,'budget_extended', approval=event['id'], old_mandate=old['revision'],
                     mandate=new['revision'], seconds=minutes*60, roles=roles, source=source, operator_kind=operator)
            db.execute('UPDATE tasks SET body=?,updated_at=? WHERE id=?',
                       (_json(task), task['updated_at'], ticket_id))
            ticket.update(mandate=new, mandate_revision=new['revision'], revision=ticket['revision']+1)
            self._save(db, ticket)
            self._record_revision(db,ticket_id,'mandate',new['revision'],new)
            self._record_revision(db,ticket_id,'approval_snapshot',new['revision'],ticket)
            self._finish(db,event,'DONE',{'action':'BUDGET_EXTENDED','mandate_revision':new['revision']},ticket)
            return self._event_view(db.execute('SELECT * FROM outer_events WHERE id=?', (event['id'],)).fetchone())

    def decide_artifact(self,ticket_id,operation,expected_revision,candidate,artifact_sha256,reason,request_id,
                        *,actor_id,source='web',tenant='default',source_thread_id=None,allow_model_input=False):
        from .goal_engine import decision
        if operation not in ('accept','return'):
            raise Problem('未知作品验收决定')
        reason = _string(reason,'退回原因',2000) if operation=='return' else ''
        identity = self._source(source,request_id,actor_id,tenant,source_thread_id)
        data = dict(type='artifact_review', ticket_id=ticket_id, operation=operation,
                    revision=expected_revision, candidate=candidate, artifact_sha256=artifact_sha256, reason=reason)
        if reason and allow_model_input is True:
            data['allow_model_input'] = True
        with self._transaction() as db:
            ticket = self._ticket(db,ticket_id,actor_id,tenant)
            event,duplicate = self._insert(db,identity,ticket_id,data)
            if duplicate:
                return dict(json.loads(event['result']),duplicate=True)
            if reason and ticket.get('ticket_team') and allow_model_input is not True:
                raise Problem('请在工作台确认将这条退回意见发送给 Codex', 409)
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
                self.workbench.blind_review().require_locked_ballot(db, ticket, task, candidate)
                for relative in review['files']:
                    self.workbench.artifact_bytes(ticket_id,relative,candidate=candidate)
            review.update(state='accepted' if approved else 'returned',reason=reason,actor=actor_id,source=source,
                          event=event['id'],decided_at=now())
            operator = operator_kind.get()
            if operator != 'owner':
                review['operator_kind'] = operator
            decision(task,'artifact_'+review['state'],assignment=candidate,digest=artifact_sha256,
                     sequence=review['sequence'],reason=reason,actor=actor_id,source=source,event=event['id'],
                     operator_kind=operator)
            if approved:
                task['plan']['steps'][task['step']]['state']='completed'
                task['step']+=1
                state.update(stage='verified_complete',outcome='VERIFIED_COMPLETE')
                delivery={k:review[k] for k in ('assignment','artifact_sha256','files','level')}
                accepted_by = '本人' if operator == 'owner' else '试验操作员'
                task.update(status='completed',phase='review',artifacts=list(review['files']),goal_delivery=delivery,
                            review={'passed':True,'findings':[f"{accepted_by}已签收作品第 {review['sequence']} 版"]})
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

    def _replace_authorized(self, db, ticket, options, outer, *, profile=None):
        from .goal_engine import active_assignments
        body = db.execute('SELECT body FROM tasks WHERE id=?', (ticket['id'],)).fetchone()
        previous = json.loads(body[0])
        state = previous['goal_execution']
        if previous['status'] not in ('paused', 'completed', 'rejected') or active_assignments(previous):
            raise Problem('范围变化需要先暂停原任务并等待旧派工退出', 409)
        self._record_revision(db, ticket['id'], 'execution_archive', ticket['mandate_revision'], previous)
        active = self.store._initialize_outer(outer, profile)
        generation = outer['mandate_revision']
        record = ticket.get('work_round')
        work_round = record['number'] if record else previous.get('work_round_number', 1)
        files = verified_input(db, self.store, ticket) if record else None
        self.store._create_workspace(ticket['id'], generation=generation, recoverable=True, initial_files=files)
        # 审计记录保留旧产物和执行，但新版从独立工作区与空证据链开始。
        active.update(revisions=state['revisions'], stop_generation=state['stop_generation'] + 1)
        root = previous.copy()
        for field in ('goal_delivery', 'run_id', 'active_call', 'model_group', 'model_processes',
                      'estimated_seconds', 'role_receipts', 'last_run_id'):
            root.pop(field, None)
        root.update(name=options['name'], goal=options['goal'], acceptance=options['acceptance'],
                    goal_execution=active, workspace_generation=generation, work_round_number=work_round, status='created', phase='plan',
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
                record = db.execute('SELECT body FROM outer_revisions WHERE ticket=? AND kind=? AND revision=?',
                                    (ticket_id, 'control_snapshot', expected_ticket_revision + 1)).fetchone()
                return {**(json.loads(record[0]) if record else ticket), 'duplicate': True}
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
            self._record_revision(db, ticket_id, 'control_snapshot', ticket['revision'], ticket)
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
            waiting = state['stage'] in ('awaiting_artifact_review', 'awaiting_preference')
            if state['stage'] == 'awaiting_preference':
                question = state['pending_preference']
                question.update(revision=question['revision']+1, stop_generation=state['stop_generation'])
            task.update(paused=False, status=state['stage'] if waiting else 'queued', error='')
        else:
            state['stop_generation'] += 1
            state['stop_requested'] = True
            if task['status'] not in ('completed', 'rejected'):
                task.update(paused=True, status='stopping' if active else 'paused')
        decision(task, 'outer_' + operation, event=event_id, source=source, generation=state['stop_generation'])
        task['updated_at'] = now()
        db.execute('UPDATE tasks SET body=?,updated_at=? WHERE id=?',
                   (_json(task), task['updated_at'], ticket['id']))
