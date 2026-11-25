"""按工单授权记录现有 GitHub 交付插件的进度。此模块不调用 GitHub。"""

import json
import re
from datetime import datetime, timezone

from workbench import Problem, TASK_RE, now

PHASES = frozenset((
    'accepted', 'reviewing', 'checking', 'reviewed', 'merging', 'shipping', 'merged',
    'docs_only', 'live', 'returned', 'retrying',
))
PUBLICATION_PHASES = frozenset(('merging', 'shipping', 'merged', 'docs_only', 'live'))
MODES = frozenset(('review', 'release'))
_REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
_SHA = re.compile(r'[a-f0-9]{40}')
_DIGEST = re.compile(r'[a-f0-9]{64}')
_INTENTS = '''CREATE TABLE IF NOT EXISTS development_release_intents (ticket TEXT PRIMARY KEY)'''
_BINDINGS = '''
CREATE TABLE IF NOT EXISTS development_release_authorizations (
    id INTEGER PRIMARY KEY, ticket TEXT NOT NULL, mandate INTEGER NOT NULL, digest TEXT NOT NULL,
    repo TEXT NOT NULL, number INTEGER NOT NULL, head TEXT NOT NULL,
    mode TEXT NOT NULL, approval_event TEXT NOT NULL, actor TEXT NOT NULL, authorized_at TEXT NOT NULL
)'''
_MATCHES = '''CREATE TABLE IF NOT EXISTS development_release_matches (authorization INTEGER PRIMARY KEY, body TEXT NOT NULL)'''
_REPORTS = '''
CREATE TABLE IF NOT EXISTS development_release_reports (
    authorization INTEGER NOT NULL, at TEXT NOT NULL, body TEXT NOT NULL,
    UNIQUE(authorization,body)
)'''


def _key(repo, number, head):
    if (not isinstance(repo, str) or len(repo) > 200 or not _REPO.fullmatch(repo)
            or any(part in ('.', '..') for part in repo.split('/'))
            or type(number) is not int or not 0 < number < 2**31
            or not isinstance(head, str) or not _SHA.fullmatch(head)):
        raise Problem('请指定确切的仓库、PR 编号和完整提交 SHA')
    return repo.casefold(), number, head


def _ticket(db, ticket):
    if not isinstance(ticket, str) or not TASK_RE.fullmatch(ticket):
        raise Problem('工单编号无效', 404)
    row = db.execute('SELECT body FROM tasks WHERE id=?', (ticket,)).fetchone()
    if row is None:
        raise Problem('工单不存在', 404)
    return json.loads(row[0])


def _approved_candidate(task):
    flow = task.get('goal_execution') or {}
    outer = flow.get('outer') or {}
    delivery = task.get('goal_delivery') or {}
    if (task.get('status') != 'completed' or task.get('paused') or flow.get('stop_requested')
            or flow.get('outcome') != 'VERIFIED_COMPLETE'
            or not isinstance(delivery.get('artifact_sha256'), str)
            or not _DIGEST.fullmatch(delivery['artifact_sha256'])
            or type(outer.get('mandate_revision')) is not int or not outer.get('approval_event_id')
            or not outer.get('actor_id')):
        return None
    return outer['mandate_revision'], delivery['artifact_sha256'], outer['approval_event_id'], outer['actor_id']


def register_release(store, ticket):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        task = _ticket(db, ticket)
        if not (task.get('goal_execution') or {}).get('outer'):
            raise Problem('开发交付须绑定正式目标工单', 409)
        db.execute(_INTENTS)
        db.execute('INSERT OR IGNORE INTO development_release_intents VALUES (?)', (ticket,))
        return release_view(db, task)


def authorize_release(store, ticket, *, mandate, digest, repo, number, head, mode, artifact_match=None):
    """由可信本机操作者另签，绑定工单已验版本与指定 PR 提交。"""
    key = _key(repo, number, head)
    if not isinstance(mode, str) or mode not in MODES or type(mandate) is not int:
        raise Problem('请选择只审查或允许发布的确切授权')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        candidate = _approved_candidate(_ticket(db, ticket))
        if not candidate or candidate[:2] != (mandate, digest):
            raise Problem('工单尚未完成，或授权与当前已验归档版本不一致', 409)
        db.execute(_INTENTS)
        db.execute(_BINDINGS)
        db.execute(_REPORTS)
        db.execute(_MATCHES)
        db.execute('INSERT OR IGNORE INTO development_release_intents VALUES (?)', (ticket,))
        previous = _latest_authorization(db, ticket, candidate)
        if db.execute(
                'SELECT 1 FROM development_release_authorizations WHERE repo=? AND number=? AND head=? AND ticket<>?',
                (*key, ticket)).fetchone():
            raise Problem('同一 PR 提交已绑定其他工单', 409)
        match_payload = json.dumps(artifact_match,sort_keys=True) if artifact_match is not None else None
        previous_match = previous and db.execute(
            'SELECT body FROM development_release_matches WHERE authorization=?',(previous['id'],)).fetchone()
        newly_verified = match_payload is not None and (not previous_match or previous_match[0] != match_payload)
        if (previous is None or newly_verified
                or tuple(previous[field] for field in ('repo','number','head','mode')) != (*key, mode)):
            db.execute('''INSERT INTO development_release_authorizations
                (ticket,mandate,digest,repo,number,head,mode,approval_event,actor,authorized_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (ticket, mandate, digest, *key, mode, *candidate[2:], _timestamp(now())))
        if artifact_match is not None:
            bound = _latest_authorization(db, ticket, candidate)
            db.execute('INSERT OR IGNORE INTO development_release_matches VALUES (?,?)',
                       (bound['id'],match_payload))
    return read_release(store, ticket)


def _exists(db, table='development_release_authorizations'):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()


def _latest_authorization(db, ticket, candidate):
    return db.execute(
        '''SELECT * FROM development_release_authorizations
           WHERE ticket=? AND mandate=? AND digest=? AND approval_event=? AND actor=?
           ORDER BY id DESC LIMIT 1''', (ticket, *candidate)).fetchone()


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise Problem('交付回执缺少有效时间')
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise Problem('交付回执缺少有效时间') from None
    if not value.endswith(('Z', '+00:00')):
        raise Problem('交付回执须使用 UTC')
    return stamp.isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def record_progress(store, data):
    """消费受信插件的阶段回调；其他 PR、提交或旧授权的回执不改变工单。"""
    if not isinstance(data, dict) or not isinstance(data.get('phase'), str) or data['phase'] not in PHASES:
        raise Problem('交付插件回执阶段无效')
    repo, number, head = _key(data.get('repo'), data.get('number'), data.get('head'))
    phase, at = data['phase'], _timestamp(data.get('at'))
    merge, ship = data.get('merge'), data.get('ship')
    if (merge is not None and (not isinstance(merge, str) or not _SHA.fullmatch(merge))
            or ship is not None and (type(ship) is not int or not 0 < ship < 2**63)
            or phase in ('merged', 'docs_only', 'live') and not merge
            or phase in ('docs_only', 'live') and not ship):
        raise Problem('交付回执缺少对应的合并提交或 Ship 记录')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not _exists(db):
            return {'recorded':False}
        binding = db.execute(
            '''SELECT * FROM development_release_authorizations WHERE repo=? AND number=? AND head=?
               ORDER BY id DESC LIMIT 1''', (repo, number, head)).fetchone()
        if not binding or at < binding['authorized_at']:
            return {'recorded':False}
        exact_id = data.get('authorization')
        if exact_id is not None and (type(exact_id) is not int or exact_id != binding['id']):
            return {'recorded':False}
        if exact_id is None:
            authorizations = db.execute(
                'SELECT count(*) FROM development_release_authorizations WHERE repo=? AND number=? AND head=?',
                (repo, number, head)).fetchone()[0]
            if authorizations != 1:
                return {'recorded':False}
        current = _approved_candidate(_ticket(db, binding['ticket']))
        if (current != (binding['mandate'], binding['digest'], binding['approval_event'], binding['actor'])
                or _latest_authorization(db, binding['ticket'], current)['id'] != binding['id']):
            return {'recorded':False}
        status = 'scope_mismatch' if phase in PUBLICATION_PHASES and binding['mode'] != 'release' else phase
        report = {'phase':phase, 'status':status, 'at':at, 'source':'agent-autoloop-delivery'}
        if merge is not None:
            report['merge'] = merge
        if ship is not None:
            report['ship'] = ship
        if data.get('detail') in ('closed', 'review', 'delivery', 'github', 'execution', 'missing_independent_review'):
            report['detail'] = data['detail']
        db.execute('INSERT OR IGNORE INTO development_release_reports VALUES (?,?,?)',
                   (binding['id'], at, json.dumps(report, sort_keys=True)))
        return {'recorded':True, 'ticket':binding['ticket'], 'status':status}


def release_view(db, task):
    """公开投影，仅使用调用方已验身份取到的当前工单。"""
    if not task or not _exists(db, 'development_release_intents'):
        return None
    ticket = task['id']
    if not db.execute('SELECT 1 FROM development_release_intents WHERE ticket=?', (ticket,)).fetchone():
        return None
    candidate = _approved_candidate(task)
    if not candidate or not _exists(db):
        return {'status':'unauthorized'}
    binding = _latest_authorization(db, ticket, candidate)
    if not binding:
        return {'status':'unauthorized'}
    rows = db.execute(
        '''SELECT body FROM development_release_reports WHERE authorization=?
           ORDER BY at DESC,rowid DESC LIMIT 30''', (binding['id'],)).fetchall()
    authorization = {key: binding[key] for key in (
        'id','repo','number','head','mode','mandate','digest','authorized_at',
    )}
    if _exists(db, 'development_release_matches'):
        match = db.execute('SELECT body FROM development_release_matches WHERE authorization=?',(binding['id'],)).fetchone()
        if match:
            authorization['artifact_match'] = json.loads(match[0])
    if not rows:
        return {'status':'awaiting_report','authorization':authorization}
    history = [json.loads(row[0]) for row in rows]
    return {'status':history[0]['status'],'authorization':authorization,'receipt':history[0],'history':history}


def read_release(store, ticket):
    with store.connect() as db:
        db.execute('BEGIN')
        return release_view(db, _ticket(db, ticket)) or {'status':'unauthorized'}
