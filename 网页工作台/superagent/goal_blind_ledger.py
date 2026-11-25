"""受信侧盲评记录。原工单只通过公开投影取得当前可操作的盲评与锁定结果。"""
import hashlib
import json
import re
import sqlite3

from workbench import Problem, now

from . import goal_blind_review as blind
from .outer_intake import operator_kind


_SCHEMA = '''
CREATE TABLE IF NOT EXISTS goal_blind_reviews (
    ticket TEXT NOT NULL, experiment TEXT NOT NULL, candidate TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL, ticket_revision INTEGER NOT NULL,
    binding TEXT NOT NULL, public TEXT NOT NULL, private TEXT NOT NULL,
    registered_at TEXT NOT NULL, ballot TEXT, ballot_digest TEXT,
    locked_event TEXT, locked_at TEXT, locked_revision INTEGER, source TEXT, operator_kind TEXT,
    PRIMARY KEY(ticket, experiment),
    UNIQUE(ticket, candidate, artifact_sha256, ticket_revision)
);
'''
_REVISIONS = ('goal_revision', 'acceptance_revision', 'mandate_revision')
_SHA256 = re.compile(r'[0-9a-f]{64}')
_CANDIDATE = re.compile(r'[0-9a-f]{32}')
_EXPERIMENT = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}')


def _encode(value):
    return blind.canonical_bytes(value).decode('utf-8')


def _format(value, pattern):
    return type(value) is str and bool(pattern.fullmatch(value))


def _blind_criterion(ticket):
    acceptance = ticket.get('acceptance_contract') or {}
    required = [row for row in acceptance.get('criteria', [])
                if row.get('required') is True and row.get('verifier_id') == 'blind-projection']
    if len(required) > 1:
        raise Problem('当前工单包含多个匿名评阅对象，请先核对验收条款', 409)
    return required[0] if required else None


def _task(db, ticket):
    row = db.execute('SELECT body FROM tasks WHERE id=?', (ticket['id'],)).fetchone()
    return json.loads(row[0]) if row else None


def _binding(ticket, task, candidate, artifact_sha256):
    """同一事务读取工单和执行快照，校验当前独立复验及其实际候选。"""
    criterion = _blind_criterion(ticket)
    task = task or {}
    state = task.get('goal_execution') or {}
    outer = state.get('outer') or {}
    reviews = state.get('artifact_reviews') or []
    review = reviews[-1] if reviews else {}
    validation = state.get('validation') or {}
    checkpoint = state.get('checkpoint') or {}
    mandate = ticket.get('mandate') or {}
    acceptance = ticket.get('acceptance_contract') or {}
    plan = task.get('plan')
    plan_revision = task.get('version')
    stop_generation = state.get('stop_generation')
    if (not criterion or ticket.get('status') != 'AUTHORIZED' or ticket.get('task_id') != ticket['id']
            or task.get('workflow') != 'goal' or task.get('status') != 'awaiting_artifact_review' or task.get('paused')
            or state.get('stop_requested') or type(stop_generation) is not int
            or not mandate.get('approval_event_id') or mandate != outer.get('mandate')
            or acceptance.get('status') != 'APPROVED' or acceptance != outer.get('acceptance_contract')
            or type(plan) is not dict or not plan.get('steps') or type(plan_revision) is not int
            or review.get('state') != 'pending' or review.get('level') != 'goal'
            or review.get('assignment') != candidate or review.get('artifact_sha256') != artifact_sha256
            or checkpoint.get('digest') != artifact_sha256 or plan_revision != review.get('plan_revision')
            or validation.get('passed') is not True or validation.get('artifact_sha256') != artifact_sha256
            or validation.get('acceptance_revision') != ticket.get('acceptance_revision')
            or any(type(ticket.get(key)) is not int or ticket[key] != review.get(key) or ticket[key] != outer.get(key)
                   for key in _REVISIONS)):
        raise Problem('当前工单、授权、计划或独立验收候选已变化，请重新打开原单', 409)
    checked = any(row.get('id') == criterion['criterion_id'] and row.get('status') == 'PASS' and
                  row.get('artifact_sha256') == artifact_sha256 for row in validation.get('checks', []))
    path = (criterion.get('test_spec') or {}).get('path')
    expected_ids = (criterion.get('expected_result') or {}).get('case_ids')
    published_file = (review.get('files') or {}).get(path) if type(path) is str else None
    if (not checked or not _format(published_file, _SHA256)
            or (checkpoint.get('files') or {}).get(path) != published_file or type(expected_ids) is not list):
        raise Problem('当前候选缺少与匿名验收绑定的归档文件', 409)
    return {'ticket_revision': ticket['revision'], 'candidate': candidate, 'artifact_sha256': artifact_sha256,
            'plan_revision': plan_revision, 'plan_sha256': blind.digest(plan), 'stop_generation': stop_generation,
            'mandate_sha256': blind.digest(mandate), 'projection_path': path, 'file_sha256': published_file,
            'case_ids': expected_ids, **{key: ticket[key] for key in _REVISIONS}}


def _safe_error(error):
    return Problem(f'匿名评阅材料未通过校验（{error.code}，{error.location}）', 409)


def _private_packet(private, heldout, baseline, team, markers, origins):
    if type(origins) is not dict or set(origins) != {'baseline', 'team'}:
        raise Problem('请绑定两条独立生成路线的冻结证据')
    for origin in origins.values():
        if (type(origin) is not dict or set(origin) != {'ref', 'prompt_sha256'}
                or type(origin['ref']) is not str or not 0 < len(origin['ref'].strip()) <= 220
                or origin['ref'] != origin['ref'].strip() or not _format(origin['prompt_sha256'], _SHA256)):
            raise Problem('冻结的路线证据或指令摘要无效')
    if origins['baseline']['ref'] == origins['team']['ref']:
        raise Problem('两条生成路线须引用各自的冻结证据')
    return {'mapping': private, 'heldout': heldout, 'baseline': baseline, 'team': team,
            'markers': sorted(set(markers)), 'origins': origins}


def _checked_public(row):
    public, private = json.loads(row['public']), json.loads(row['private'])
    try:
        blind.validate(public, private['mapping'], private['heldout'], private['baseline'], private['team'],
                       markers=private['markers'])
    except blind.BlindReviewError as error:
        raise _safe_error(error) from error
    return public, private


def _score(row, public, private):
    try:
        return blind.unblind(public, private['mapping'], private['heldout'], private['baseline'], private['team'],
                             json.loads(row['ballot']), expected_ballot_digest=row['ballot_digest'],
                             markers=private['markers'])
    except blind.BlindReviewError as error:
        raise _safe_error(error) from error


def _runtime_measures(task, private, experiment):
    if not task:
        return None
    state = task.get('goal_execution') or {}
    assignment = next((row for row in state.get('assignments', []) if row['id'] == experiment), None)
    calls = (assignment or {}).get('role_calls') or []
    if [row['role'] for row in calls] != ['lead','advisor','implementer','implementer']:
        return None
    for arm, call in zip(('baseline','team'), calls[-2:]):
        if private['origins'][arm] != {'ref':call['id'], 'prompt_sha256':call.get('prompt_sha256')}:
            return None
    if any(call.get('status') != 'completed' or type(call.get('started_ns')) is not int
           or type(call.get('ended_ns')) is not int or call['started_ns'] > call['ended_ns'] for call in calls):
        return None

    def measure(rows):
        duration = sum(row['ended_ns'] - row['started_ns'] for row in rows)
        return {'calls':len(rows),'model_wall_ms':round(duration/1e6)}
    return {'baseline':measure(calls[2:3]), 'team':measure([*calls[:2], calls[3]]), 'human_relays':0}


def _verify_locked_event(db, ticket, row, public):
    event = db.execute('SELECT e.* FROM outer_events e JOIN outer_tickets t ON t.id=e.ticket AND t.actor=e.actor '
                       'AND t.tenant=e.tenant WHERE e.id=? AND e.ticket=?',
                       (row['locked_event'], ticket['id'])).fetchone()
    body = json.loads(event['body']) if event else {}
    result = json.loads(event['result'] or '{}') if event else {}
    if (not event or event['state'] != 'DONE' or event['source'] != row['source']
            or body.get('type') != 'blind_ballot' or body.get('experiment_id') != row['experiment']
            or body.get('revision') != row['ticket_revision'] or row['locked_revision'] != row['ticket_revision'] + 1
            or body.get('manifest_digest') != public['manifest_digest'] or body.get('ballot_digest') != row['ballot_digest']
            or body.get('operator_kind', 'owner') != row['operator_kind'] or result.get('action') != 'BLIND_BALLOT_LOCKED'
            or result.get('manifest_digest') != public['manifest_digest'] or result.get('experiment_id') != row['experiment']
            or result.get('ballot_digest') != row['ballot_digest'] or not row['locked_at']):
        raise Problem('匿名评价的记录与原单锁定回执不一致', 409)
    return event


def _current(ticket, task, row):
    binding = json.loads(row['binding'])
    if row['locked_event'] and task and task.get('status') == 'completed':
        delivery = task.get('goal_delivery') or {}
        return (delivery.get('assignment') == row['candidate'] and delivery.get('artifact_sha256') == row['artifact_sha256']
                and task.get('version') == binding['plan_revision'] and all(ticket.get(key) == binding[key] for key in _REVISIONS))
    try:
        current = _binding(ticket, task, row['candidate'], row['artifact_sha256'])
    except Problem:
        return False
    return current == {**binding, 'ticket_revision': row['locked_revision'] or row['ticket_revision']}


class BlindReviewLedger:
    """受信服务的入口。路由须传入已认证的本人身份。"""
    def __init__(self, workbench):
        self.workbench = workbench
        self.store = workbench.store
        self.intake = workbench.outer()
        with self.store.connect() as db:
            db.executescript(_SCHEMA)

    @staticmethod
    def _row(db, ticket_id, experiment_id):
        if experiment_id is None:
            return db.execute('SELECT * FROM goal_blind_reviews WHERE ticket=? ORDER BY rowid DESC LIMIT 1', (ticket_id,)).fetchone()
        return db.execute('SELECT * FROM goal_blind_reviews WHERE ticket=? AND experiment=?', (ticket_id, experiment_id)).fetchone()

    def brief(self, db, ticket, task):
        row = self._row(db, ticket['id'], None)
        if row is None:
            return None
        if not _current(ticket, task, row):
            return 'superseded'
        return 'locked' if row['locked_event'] else 'awaiting_ballot'

    def history(self, ticket_id, *, actor_id, tenant='default'):
        actor, tenant = self.intake._owner(actor_id, tenant)
        with self.store.connect() as db:
            self.intake._ticket(db, ticket_id, actor, tenant)
            rows = db.execute('SELECT experiment,candidate,locked_at FROM goal_blind_reviews '
                              'WHERE ticket=? AND locked_event IS NOT NULL ORDER BY rowid DESC',
                              (ticket_id,)).fetchall()
        return [{'experiment_id':row['experiment'],'candidate':row['candidate'],
                 'locked_at':row['locked_at'],'status':'locked'} for row in rows]

    @staticmethod
    def _repeat(row, revision, candidate, artifact_sha256, public, private):
        if ((row['ticket_revision'], row['candidate'], row['artifact_sha256']) != (revision, candidate, artifact_sha256)
                or row['public'] != public or row['private'] != private):
            raise Problem('同一试验编号已经绑定其他候选或冻结证据', 409)

    def register(self, ticket_id, revision, candidate, artifact_sha256, *, public, private, heldout, baseline, team,
                 origins, actor_id, tenant='default', markers=blind.DEFAULT_MARKERS):
        """校验并封存指定原单候选的匿名包。只供受信生成与复验流程调用。"""
        if not _format(candidate, _CANDIDATE) or not _format(artifact_sha256, _SHA256) or type(revision) is not int:
            raise Problem('盲评须指定当前工单版本、候选和归档摘要')
        actor, tenant = self.intake._owner(actor_id, tenant)
        try:
            blind.validate(public, private, heldout, baseline, team, markers=markers)
        except blind.BlindReviewError as error:
            raise _safe_error(error) from error
        experiment = public['manifest']['experiment_id']
        public_blob = _encode(public)
        private_blob = _encode(_private_packet(private, heldout, baseline, team, markers, origins))
        with self.store.connect() as db:
            db.execute('BEGIN')
            ticket = self.intake._ticket(db, ticket_id, actor, tenant)
            row = self._row(db, ticket_id, experiment)
            if row:
                self._repeat(row, revision, candidate, artifact_sha256, public_blob, private_blob)
                binding = None
            else:
                self.intake._compare(ticket, revision)
                binding = _binding(ticket, _task(db, ticket), candidate, artifact_sha256)
        if binding is None:
            return self.get(ticket_id, experiment, actor_id=actor, tenant=tenant)

        _, content = self.workbench.artifact_bytes(ticket_id, binding['projection_path'], candidate=candidate)
        if hashlib.sha256(content).hexdigest() != binding['file_sha256']:
            raise Problem('原单匿名归档字节已变化', 409)
        try:
            frozen = blind.load_json(content)
            check = blind.validate_projection(frozen, expected_ids=binding['case_ids'], markers=markers)
        except blind.BlindReviewError as error:
            raise _safe_error(error) from error
        if check['projection_sha256'] != public['manifest']['projection_sha256']:
            raise Problem('匿名投影与原工单独立验收的文件不一致', 409)
        with self.intake._transaction() as db:
            ticket = self.intake._ticket(db, ticket_id, actor, tenant)
            row = self._row(db, ticket_id, experiment)
            if row:
                self._repeat(row, revision, candidate, artifact_sha256, public_blob, private_blob)
            else:
                self.intake._compare(ticket, revision)
                if binding != _binding(ticket, _task(db, ticket), candidate, artifact_sha256):
                    raise Problem('登记期间工单版本或归档已变化', 409)
                locked = db.execute('SELECT 1 FROM goal_blind_reviews WHERE ticket=? AND candidate=? AND artifact_sha256=? '
                                    'AND locked_event IS NOT NULL', (ticket_id, candidate, artifact_sha256)).fetchone()
                if locked:
                    raise Problem('此候选已锁定盲评记录', 409)
                try:
                    db.execute('INSERT INTO goal_blind_reviews(ticket,experiment,candidate,artifact_sha256,ticket_revision,'
                               'binding,public,private,registered_at) VALUES(?,?,?,?,?,?,?,?,?)',
                               (ticket_id, experiment, candidate, artifact_sha256, revision, _encode(binding),
                                public_blob, private_blob, now()))
                except sqlite3.IntegrityError as error:
                    raise Problem('当前候选和工单版本已有匿名评阅对象', 409) from error
        return self.get(ticket_id, experiment, actor_id=actor, tenant=tenant)

    def get(self, ticket_id, experiment_id=None, *, actor_id, tenant='default'):
        """按本人已授权原单读取匿名材料。历史锁定结果须明确指定试验编号。"""
        actor, tenant = self.intake._owner(actor_id, tenant)
        if experiment_id is not None and not _format(experiment_id, _EXPERIMENT):
            raise Problem('匿名试验编号无效', 404)
        with self.store.connect() as db:
            db.execute('BEGIN')
            ticket = self.intake._ticket(db, ticket_id, actor, tenant)
            row = self._row(db, ticket_id, experiment_id)
            if row is None:
                return None
            task = _task(db, ticket)
            current = _current(ticket, task, row)
            if experiment_id is not None and not current:
                latest = self._row(db, ticket_id, None)
                if latest and not latest['locked_event'] and _current(ticket, task, latest):
                    raise Problem('当前候选仍待匿名评阅，其历史解盲暂不可读', 409)
            public, private = _checked_public(row)
            show_locked = bool(row['locked_event'] and (current or experiment_id))
            event = _verify_locked_event(db, ticket, row, public) if show_locked else None
        view = {'status': 'superseded', 'experiment_id': row['experiment'], 'candidate': row['candidate'],
                'artifact_sha256': row['artifact_sha256'], 'manifest_digest': public['manifest_digest'],
                'ticket_revision': row['locked_revision'] or row['ticket_revision'], 'current': current}
        if current or show_locked:
            view.update(status='awaiting_ballot', projection=public['projection'], manifest=public['manifest'])
            if show_locked:
                view.update(status='locked', lock={'event_id': row['locked_event'], 'source': row['source'],
                                                 'actor_id': event['actor'],
                                                 'operator_kind': row['operator_kind'], 'locked_at': row['locked_at'],
                                                 'ballot_digest': row['ballot_digest']}, result=_score(row, public, private))
                view['runtime_measures'] = _runtime_measures(task,private,row['experiment'])
        return view

    def lock_ballot(self, ticket_id, revision, experiment_id, manifest_digest, judgments, request_id, *,
                    actor_id, source='web', tenant='default'):
        """以原工单事件号锁定逐例意见。选票先提交事务，再读取解盲结果。"""
        if source not in ('web', 'dsh'):
            raise Problem('当前渠道不支持匿名评阅', 403)
        if not _format(experiment_id, _EXPERIMENT) or not _format(manifest_digest, _SHA256) or type(revision) is not int:
            raise Problem('请选择当前工单版本和匿名材料')
        identity = self.intake._source(source, request_id, actor_id, tenant, None)
        operator = operator_kind.get()
        with self.intake._transaction() as db:
            ticket = self.intake._ticket(db, ticket_id, identity[2], identity[3])
            row = self._row(db, ticket_id, experiment_id)
            if not row:
                raise Problem('匿名评阅对象不存在或已改变，请返回原单', 409)
            public, private = _checked_public(row)
            if public['manifest_digest'] != manifest_digest:
                raise Problem('匿名材料已变化，请回到原单重新核对', 409)
            try:
                ballot = blind.prepare_ballot(public, judgments, markers=private['markers'])
            except blind.BlindReviewError as error:
                raise _safe_error(error) from error
            event, duplicate = self.intake._insert(db, identity, ticket_id, {
                'type': 'blind_ballot', 'ticket_id': ticket_id, 'revision': revision, 'experiment_id': experiment_id,
                'manifest_digest': manifest_digest, 'ballot_digest': ballot['ballot_digest'],
            })
            if duplicate:
                if row['locked_event'] != event['id']:
                    raise Problem('原匿名评价尚未完成锁定', 409)
            else:
                self.intake._compare(ticket, revision)
                if row['locked_event'] or not _current(ticket, _task(db, ticket), row):
                    raise Problem('匿名评价已锁定或当前候选已更换，请刷新', 409)
                ticket['revision'] += 1
                self.intake._save(db, ticket)
                db.execute('UPDATE goal_blind_reviews SET ballot=?,ballot_digest=?,locked_event=?,locked_at=?,'
                           'locked_revision=?,source=?,operator_kind=? WHERE ticket=? AND experiment=?',
                           (_encode(ballot), ballot['ballot_digest'], event['id'], now(), ticket['revision'],
                            source, operator, ticket_id, experiment_id))
                self.intake._finish(db, event, 'DONE', {
                    'action': 'BLIND_BALLOT_LOCKED', 'experiment_id': experiment_id,
                    'manifest_digest': manifest_digest, 'ballot_digest': ballot['ballot_digest'],
                }, ticket, deliver=False)
        view = self.get(ticket_id, experiment_id, actor_id=identity[2], tenant=identity[3])
        return {**view, 'event_id': event['id'], 'duplicate': duplicate}

    def require_locked_ballot(self, db, ticket, task, candidate):
        """签收事务调用。验收契约要求盲评时，签收绑定当前候选的本人锁定记录。"""
        if not _blind_criterion(ticket):
            return
        row = db.execute('SELECT * FROM goal_blind_reviews WHERE ticket=? AND candidate=? AND locked_event IS NOT NULL '
                         'ORDER BY rowid DESC LIMIT 1', (ticket['id'], candidate)).fetchone()
        if not row or row['operator_kind'] != operator_kind.get() or not _current(ticket, task, row):
            raise Problem('请先完成当前候选的匿名评价，再签收', 409)
        public, private = _checked_public(row)
        _verify_locked_event(db, ticket, row, public)
        _score(row, public, private)
