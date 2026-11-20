"""网页任务记录与 Autoloop 适配。中台仍拥有派发、取消和审批事实。"""
from contextlib import contextmanager, nullcontext
import json
import base64
import fcntl
import hashlib
import os
import re
import sqlite3
import stat
import subprocess
import threading
import time
import uuid
from agent_catalog import DEFAULT_ROLES, LEGACY_ROLES, validate_roles, catalogue
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from execution_policy import AUTO_SECONDS, CALL_HARD_SECONDS, automatic, exhaustion
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / '.runtime'
RULE = 'odyssey-workbench'
TASK_RE = re.compile(r'^wb-[a-f0-9]{32}$')
STATES = {'created':'待派发','queued':'排队中','planning':'正在规划','awaiting_approval':'等待你的决定','executing':'正在执行','reviewing':'独立验收中','awaiting_artifact_review':'待人工签收','completed':'已验收完成','blocked':'遇到阻塞','paused':'已暂停','stopping':'正在停止','rejected':'已拒绝','budget_exhausted':'达到执行预算'}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def omit_call_payloads(task):
    """通用看板和飞书读取的是任务副本；完整新增回执只从单次调用入口加载。"""
    for assignment in (task.get('goal_execution') or {}).get('assignments', []):
        if assignment.get('report'):
            assignment['report'].pop('model_audit', None)
    return task


class Problem(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class TaskBusy(Problem):
    pass


def require_v1_control(task):
    if (task.get('goal_execution') or {}).get('outer'):
        raise Problem('请从当前目标契约工单操作', 409)


class Store:
    def __init__(self, root=RUNTIME):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / 'tasks.sqlite3'
        with self.connect() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, body TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS progress(seq INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT NOT NULL, at TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, task TEXT NOT NULL, event TEXT NOT NULL, response TEXT);
            CREATE TABLE IF NOT EXISTS receipts(run INTEGER PRIMARY KEY, task TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS inbox_notes(message TEXT PRIMARY KEY,task TEXT NOT NULL,body TEXT NOT NULL,at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS inbox_task ON inbox_notes(task);
            CREATE TABLE IF NOT EXISTS note_moves(request TEXT PRIMARY KEY,message TEXT NOT NULL,previous TEXT NOT NULL,target TEXT NOT NULL,revision INTEGER NOT NULL,UNIQUE(message,revision));
            CREATE TABLE IF NOT EXISTS handoffs(request TEXT PRIMARY KEY,task TEXT NOT NULL,state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS handoff_aliases(id TEXT PRIMARY KEY,request TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS mobile_replies(event TEXT PRIMARY KEY,stamp TEXT NOT NULL,result TEXT NOT NULL);
            ''')
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, task_id):
        if not TASK_RE.fullmatch(str(task_id)):
            raise Problem('任务编号无效', 404)
        with self.connect() as db:
            row = db.execute('SELECT body FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not row:
            raise Problem('任务不存在', 404)
        return json.loads(row[0])

    def change(self, task_id, fn):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                raise Problem('任务不存在', 404)
            task = json.loads(row[0])
            fn(task)
            task['updated_at'] = now()
            db.execute('UPDATE tasks SET body=?,updated_at=? WHERE id=?', (json.dumps(task, ensure_ascii=False), task['updated_at'], task_id))
        return task

    @contextmanager
    def lease(self, task_id):
        # 同一任务只能有一个执行者或恢复操作；进程退出后系统释放锁。
        self.get(task_id)
        with (self.workspace(task_id).parent / 'execution.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise TaskBusy('已有任务操作或执行器运行，请等待后重试', 409) from None
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def recover_call(self, task_id):
        task = self.get(task_id)
        if task.get('model_processes'):
            from process_guard import alive, processes
            if alive(task['model_processes'], processes()):
                raise Problem('先前模型或工具后代仍在运行，请确认停止后恢复',409)
        if task.get('model_group'):
            processes = subprocess.check_output(['/bin/ps', '-ax', '-o', 'pgid=', '-o', 'stat='], text=True)
            if any(len(row) == 2 and row[0] == str(task['model_group']) and not row[1].startswith('Z')
                   for row in (line.split() for line in processes.splitlines())):
                raise Problem('先前执行进程仍存在，请等待退出后再恢复', 409)
        def settle(t):
            active = t.get('active_call')
            if active:
                # 崩溃后无法准确复原实际结束时刻，保守记入预留额度并明确标为估计。
                seconds = active['reserved_seconds']
                t['elapsed_seconds'] = t.get('elapsed_seconds', 0) + seconds
                t['estimated_seconds'] = t.get('estimated_seconds', 0) + seconds
                t['active_call'] = None
            t['model_group'] = None
            t['model_processes'] = []
        return self.change(task_id, settle)

    def event(self, task_id, kind, title, detail=None):
        with self.connect() as db:
            db.execute('INSERT INTO progress(task,at,kind,title,detail) VALUES(?,?,?,?,?)', (task_id, now(), kind, title[:300], json.dumps(detail or {}, ensure_ascii=False)))

    def workspace(self, task_id, *, generation=None):
        if not TASK_RE.fullmatch(task_id):
            raise Problem('任务编号无效')
        if generation is None:
            with self.connect() as db:
                row = db.execute("SELECT json_extract(body,'$.workspace_generation') FROM tasks WHERE id=?",
                                 (task_id,)).fetchone()
            generation = (row[0] if row else None) or 1
        if type(generation) is not int or generation < 1:
            raise Problem('任务工作区代次无效')
        parent = self.root / 'tasks' / task_id
        if generation > 1:
            parent = parent / 'mandates' / str(generation)
        path = parent / 'workspace'
        if path.resolve() != path.absolute():
            raise Problem('任务路径不能使用符号链接')
        return path

    def _create_workspace(self, task_id, *, generation=1, recoverable=False):
        workspace = self.workspace(task_id, generation=generation)
        # 若事务提交前进程退出，可用同一工单号及已指定代次重试。
        workspace.mkdir(parents=True, mode=0o700, exist_ok=recoverable)
        guidance = workspace / 'AGENTS.md'
        if recoverable and any(path != guidance for path in workspace.iterdir()):
            raise Problem('新授权的工作区必须为空', 409)
        if guidance.is_symlink():
            raise Problem('任务工作区指引不能使用符号链接', 409)
        guidance.write_text('# 本任务工作区\n\n仅修改本目录内文件。先阅读任务中给出的完整目标、已批准方案和已有成果。不得读取凭据、修改工作台控制记录、调用中台本人接口。外部发布、发送、付费、安装以及范围外写入必须先提出具体审批；业务审批不提升操作系统权限。验收需实际运行，未测内容明确说明。\n')
        return workspace

    @staticmethod
    def _initialize_outer(outer):
        from superagent.goal_engine import initial_state
        source = 'feishu' if outer['mandate']['source'] == 'feishu' else 'board'
        state = initial_state(source, outer['approval_event_id'])
        state['outer'] = outer
        return state

    def create(self, body, *, source='board', workflow=None, verification_contract=None, team_enabled=False,
               _db=None, _outer=None):
        name = str(body.get('name', '')).strip()
        goal = str(body.get('goal', '')).strip()
        acceptance = str(body.get('acceptance', '')).strip()
        request_id = str(body.get('request_id', ''))
        if not re.fullmatch(r'[a-f0-9-]{36}', request_id):
            raise Problem('缺少有效请求编号，请刷新后重试')
        if not 1 <= len(name) <= 80 or not 1 <= len(goal) <= 6000 or len(acceptance) > 4000:
            raise Problem('请填写任务名称和目标，目标不超过 6000 字')
        mode = body.get('execution_mode', 'bounded' if 'max_calls' in body else 'auto')
        if mode not in ('auto', 'bounded'):
            raise Problem('执行模式无效')
        budget = body.get('max_calls')
        if budget is not None and (type(budget) is not int or budget not in (4, 6, 10)):
            raise Problem('旧版调用预算必须为 4、6 或 10 次')
        minutes = body.get('budget_minutes', 60 if mode == 'auto' else 30)
        if type(minutes) is not int or minutes not in (15, 30, 60):
            raise Problem('执行时间预算须为 15、30 或 60 分钟')
        try:
            roles=validate_roles(body.get('role_models',DEFAULT_ROLES))
        except ValueError as e:
            raise Problem(str(e)) from None
        if _outer is not None and (_db is None or workflow != 'goal' or verification_contract is not None or team_enabled):
            raise Problem('外层授权必须在接单事务内登记', 409)
        task_id = _outer['ticket_id'] if _outer else 'wb-' + uuid.uuid4().hex
        task = dict(id=task_id, role_models=roles, initial_role_models=roles, versions=[], changes=[], revision_requests={}, pending_revision=None, name=name, goal=goal, acceptance=acceptance, version=1, status='created', phase='plan', step=0, plan=None, approval=None, decisions=[], artifacts=[], checks=[], calls=0, max_calls=budget, initial_max_calls=budget, initial_execution_mode=mode, initial_budget_minutes=minutes, execution_mode=mode, time_budget_seconds=minutes*60, elapsed_seconds=0, time_accounting_since=now(), recovery_attempts={}, review_failures=0, paused=False, created_at=now(), updated_at=now(), run_id=None, model='gpt-6-astra', error='', review=None)
        with (nullcontext(_db) if _db is not None else self.connect()) as db:
            if _db is None:
                db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT body FROM tasks WHERE request_id=?', (request_id,)).fetchone()
            if old:
                prior = json.loads(old[0])
                if prior.get('source','board') != source:
                    raise Problem('请求来源不一致',409)
                if prior.get('workflow') != workflow:
                    raise Problem('同一个请求编号不能切换执行路线',409)
                if workflow == 'goal' and prior['goal_execution'].get('verification_contract') != verification_contract:
                    raise Problem('同一个请求编号不能更换验收契约',409)
                if workflow == 'goal' and prior['goal_execution'].get('team_enabled',False) != team_enabled:
                    raise Problem('同一个请求编号不能切换团队执行路线',409)
                if any(prior[k] != task[k] for k in ('name', 'goal', 'acceptance', 'initial_max_calls')):
                    raise Problem('同一个请求编号不能提交不同内容', 409)
                if prior.get('initial_execution_mode', 'bounded') != mode or prior.get('initial_budget_minutes', 30) != minutes:
                    raise Problem('同一个请求编号不能提交不同执行政策', 409)
                if prior.get('initial_role_models',LEGACY_ROLES)!=roles:
                    raise Problem('同一个请求编号不能提交不同模型分工',409)
                return prior, False
            task['source'] = source
            if workflow == 'goal':
                from superagent.goal_engine import initial_state
                task['workflow'] = workflow
                task['goal_execution'] = self._initialize_outer(_outer) if _outer else initial_state(source, request_id, verification_contract, team_enabled)
            workspace = self._create_workspace(task_id, recoverable=_outer is not None)
            db.execute('INSERT INTO tasks VALUES(?,?,?,?)', (task_id, request_id, json.dumps(task, ensure_ascii=False), task['updated_at']))
            db.execute('INSERT INTO progress(task,at,kind,title,detail) VALUES(?,?,?,?,?)',
                       (task_id, now(), 'created', '任务已保存，准备交给中台', json.dumps({'workspace': str(workspace)})))
        return task, True

    def list(self):
        with self.connect() as db:
            rows = db.execute('SELECT body FROM tasks ORDER BY updated_at DESC LIMIT 100').fetchall()
        return [json.loads(row[0]) for row in rows]

    def add_note(self, task_id, message, body):
        require_v1_control(self.get(task_id))
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT task,body FROM inbox_notes WHERE message=?',(message,)).fetchone()
            origin=db.execute('SELECT previous FROM note_moves WHERE message=? AND revision=1',(message,)).fetchone()
            if old and ((origin[0] if origin else old['task'])!=task_id or old['body']!=body):
                raise Problem('同一消息不能改投其他工单或覆盖已保存内容',409)
            if not old:
                stamp = now()
                db.execute('INSERT INTO inbox_notes VALUES(?,?,?,?)',(message,task_id,body,stamp))
                db.execute('UPDATE tasks SET updated_at=? WHERE id=?',(stamp,task_id))

    def notes(self, task_id, for_board=False):
        with self.connect() as db:
            if for_board:
                return [dict(row) for row in db.execute("""SELECT n.message,n.at,n.body,
                    CASE WHEN n.message LIKE 'web-%' THEN 'board' ELSE 'feishu' END AS source,
                    (SELECT count(*) FROM note_moves WHERE message=n.message) AS revision
                    FROM inbox_notes n WHERE task=? ORDER BY n.rowid""",(task_id,))]
            return [dict(row) for row in db.execute('SELECT at,body FROM inbox_notes WHERE task=? ORDER BY rowid',(task_id,))]

    def board_note(self, task_id, body):
        text=body.get('text')
        if not isinstance(text,str) or not 1<=len(text.strip())<=8000 or not re.fullmatch(r'[a-f0-9-]{36}',str(body.get('request_id'))):
            raise Problem('请填写 1–8000 字的补充资料及有效请求号')
        self.add_note(task_id,'web-'+body['request_id'],text.strip())
        return {'saved':True}

    def move_note(self, task_id, body):
        target,message,revision,request=(body.get(k) for k in ('target','message','revision','request_id'))
        if (not TASK_RE.fullmatch(str(target)) or target==task_id or not isinstance(message,str)
                or type(revision)!=int or revision<0 or not re.fullmatch(r'[a-f0-9-]{36}',str(request))):
            raise Problem('请选择另一张工单及当前补充')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous=db.execute('SELECT message,previous,target,revision FROM note_moves WHERE request=?',(request,)).fetchone()
            move=(message,task_id,target,revision+1)
            if previous:
                if tuple(previous)!=move:raise Problem('同一请求不能用于不同改投',409)
                return {'saved':True}
            current=db.execute('SELECT task FROM inbox_notes WHERE message=?',(message,)).fetchone()
            count=db.execute('SELECT count(*) FROM note_moves WHERE message=?',(message,)).fetchone()[0]
            if not current or current[0]!=task_id or count!=revision:raise Problem('资料关联已变化，请刷新后重选',409)
            for candidate in (task_id,target):
                row=db.execute('SELECT body FROM tasks WHERE id=?',(candidate,)).fetchone()
                queued=db.execute('SELECT 1 FROM outbox WHERE task=? LIMIT 1',(candidate,)).fetchone()
                task=json.loads(row[0]) if row else {}
                require_v1_control(task)
                if queued or task.get('status')!='created' or task.get('calls')!=0:
                    raise Problem('仅可在两张尚未派发的工单之间改投。已派发请走方案修改。',409)
            db.execute('INSERT INTO note_moves VALUES(?,?,?,?,?)',(request,*move))
            db.execute('UPDATE inbox_notes SET task=? WHERE message=?',(target,message))
            for candidate,title,other in ((task_id,'补充已移至另一工单',target),(target,'补充已从另一工单移入',task_id)):
                db.execute('INSERT INTO progress(task,at,kind,title,detail) VALUES(?,?,?,?,?)',
                           (candidate,now(),'note_move',title,json.dumps({'task':other})))
        return {'saved':True}

    def handoff(self, task_id, body):
        require_v1_control(self.get(task_id))
        request = body.get('request_id')
        if not isinstance(request,str) or not re.fullmatch(r'[a-f0-9-]{36}',request):
            raise Problem('缺少切换请求编号')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT task,state FROM handoffs WHERE request=COALESCE((SELECT request FROM handoff_aliases WHERE id=?),?)',(request,request)).fetchone()
            if prior and prior['task'] != task_id:
                raise Problem('切换请求与工单不符',409)
            if prior:
                return {'state':prior['state']}
            pending = db.execute("SELECT request FROM handoffs WHERE task=? AND state='pending'",(task_id,)).fetchone()
            if pending:
                db.execute('INSERT INTO handoff_aliases VALUES(?,?)',(request,pending[0]))
            else:
                db.execute("INSERT INTO handoffs VALUES(?,?,'pending')",(request,task_id))
        return {'state':'pending'}

    def pending_handoffs(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT request,task FROM handoffs WHERE state='pending' ORDER BY rowid LIMIT 10")]

    def mark_handoff(self, request, state):
        with self.connect() as db:
            db.execute("UPDATE handoffs SET state=? WHERE request=? AND state='pending'",(state,request))

    def events(self, task_id):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM progress WHERE task=? ORDER BY seq DESC LIMIT 80', (task_id,)).fetchall()
        return [{**dict(row), 'detail': json.loads(row['detail'])} for row in reversed(rows)]

    def outbox(self, task_id, request_id):
        if not re.fullmatch(r'[a-f0-9-]{36}', str(request_id)):
            raise Problem('缺少有效请求编号')
        event_id = task_id + ':' + request_id
        event = dict(id=event_id, type='workbench.task.requested', subject=task_id, summary='网页任务待执行', detail=json.dumps({'version':self.get(task_id)['version'],'request_id':request_id}))
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO outbox VALUES(?,?,?,NULL)', (event_id, task_id, json.dumps(event)))
            row = db.execute('SELECT * FROM outbox WHERE id=?', (event_id,)).fetchone()
        return dict(row)

    def pending_request(self, task_id):
        with self.connect() as db:
            row = db.execute('SELECT id FROM outbox WHERE task=? AND response IS NULL ORDER BY rowid DESC LIMIT 1', (task_id,)).fetchone()
        return row[0].split(':', 1)[1] if row else None

    def dispatch_result(self, task_id, request_id):
        with self.connect() as db:
            row = db.execute('SELECT response FROM outbox WHERE id=?', (task_id + ':' + request_id,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def finish_outbox(self, event_id, response):
        with self.connect() as db:
            db.execute('UPDATE outbox SET response=? WHERE id=?', (json.dumps(response), event_id))

    def receipt(self, run_id, task_id=None, body=None):
        with self.connect() as db:
            if body is not None:
                db.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?)', (run_id, task_id, json.dumps(body, ensure_ascii=False)))
            row = db.execute('SELECT body FROM receipts WHERE run=?', (run_id,)).fetchone()
        return json.loads(row[0]) if row else None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, newurl):
        return None


class Hub:
    def __init__(self, home=None):
        self.home = Path(home or Path.home() / '.agent-autoloop')
        self.cache = None
        self.fetched = 0
        self.lock = threading.Lock()

    def request(self, method, path, body=None, source=False):
        config = json.loads((self.home / 'config.json').read_text())
        if source and config.get('source', {}).get('name') != 'local':
            raise Problem('本机来源配置不匹配，任务尚未派发', 503)
        key = config['source']['key'] if source else config['owner']
        endpoint = urlsplit(config['url'])
        if endpoint.scheme != 'http' or endpoint.hostname not in ('localhost', '127.0.0.1') or endpoint.username or endpoint.password:
            raise Problem('中台必须使用本机地址', 503)
        request = Request(config['url'].rstrip('/') + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        try:
            with build_opener(NoRedirect()).open(request, timeout=8) as response:
                result = json.load(response)
        except HTTPError as error:
            raise Problem('中台拒绝请求（HTTP %s），请刷新核对当前状态' % error.code, error.code) from None
        except (OSError, URLError, ValueError):
            raise Problem('暂时无法连接中台；记录已保留，请稍后刷新', 503) from None
        self.fetched = 0 if method != 'GET' else self.fetched
        return result

    def state(self):
        with self.lock:
            if self.cache is None or time.monotonic() - self.fetched > 1.5:
                self.cache = self.request('GET', '/v1/state')
                self.fetched = time.monotonic()
            return self.cache

    def existing_event(self,key,event_type,subject):
        with sqlite3.connect('file:'+str(self.home/'hub.db')+'?mode=ro',uri=True,timeout=3) as db:
            row = db.execute("""SELECT id FROM events WHERE source='local' AND key=?
                              AND type=? AND subject=?""",(key,event_type,subject)).fetchone()
        return row[0] if row else None

    def records(self):
        # 精确读取专用规则历史，避免 /v1/state 最近页的 20/60 条截断。
        db = sqlite3.connect('file:' + str(self.home / 'hub.db') + '?mode=ro', uri=True, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA query_only=ON')
            runs = [dict(r) for r in db.execute("""SELECT id,lane,state,cancel_at,claimed_at,finished_at,summary,error,usage
                FROM runs WHERE rule=? AND (state IN ('unknown','claimed','queued') OR id IN
                (SELECT id FROM runs WHERE rule=? ORDER BY id DESC LIMIT 500)) ORDER BY id DESC""",(RULE,RULE))]
            cards = [dict(r) for r in db.execute('SELECT id,lane,version,state,title,text,options,choice,answer,about,created_at,decided_at,result FROM cards WHERE rule=? ORDER BY id DESC LIMIT 500', (RULE,))]
        finally:
            db.close()
        for card in cards:
            card['options'] = json.loads(card['options'] or '[]')
            try:
                card['binding'] = json.loads(card['about'] or '{}')
            except ValueError:
                card['binding'] = {}
            card.pop('about', None)
        return runs, cards


class Workbench:
    def __init__(self, store=None, hub=None, goal_enabled=False, goal_contract=None, goal_team=False, *,
                 outer_enabled=False, outer_model=None, outer_registry=None):
        self.store = store or Store()
        self.hub = hub or Hub()
        self.goal_enabled = goal_enabled
        self.goal_contract = goal_contract
        self.goal_team = goal_team
        self.outer_enabled = outer_enabled
        self._outer_service = None
        if outer_enabled:
            from superagent.outer_intake import OuterIntake
            self._outer_service = OuterIntake(self, outer_model, outer_registry)

    def outer(self):
        if self._outer_service is None:
            raise Problem('外层智能接单尚未启用', 503)
        return self._outer_service

    def goal_engine(self):
        if not self.goal_enabled:
            raise Problem('当前工作台尚未启用目标执行路线',503)
        from superagent.goal_engine import GoalEngine
        return GoalEngine(self.store,self.goal_contract,self.goal_team)

    def capability(self):
        try:
            state = self.hub.state()
            rule_id = 'odyssey-goal' if self.goal_enabled else RULE
            line = next((l for l in state['lines'] if l['id'] == rule_id), None)
            if self.goal_enabled:
                agents = [a for a in state['agents'] if a['id'] == 'odyssey-goal']
            else:
                agents = [a for a in state['agents'] if a['id'] == 'odyssey-runner']
            online = any(a.get('online') for a in agents)
            ready = bool(line and online and not line.get('paused'))
            return {'hub_available':True,'ready':ready,'configured':bool(line),'worker_online':online,
                    'model':'gpt-6.2-sol' if self.goal_enabled else 'gpt-6-astra',
                    'message':'可创建真实任务' if ready else '执行入口未就绪，任务会保留在服务端','at':now()}
        except (Problem, OSError, ValueError):
            return {'hub_available': False, 'ready': False, 'configured': False, 'worker_online': False, 'message': '中台连接中断，已保存任务不丢失', 'at': now()}

    def dispatch(self, task_id, request_id):
        if self.store.get(task_id).get('workflow') == 'goal':
            raise Problem('目标工单由后台协调器派工',409)
        entry = self.store.outbox(task_id, request_id)
        if entry['response']:
            response = json.loads(entry['response'])
        else:
            capability = self.capability()
            if not capability['configured']:
                raise Problem('执行规则尚未配置，任务已保存', 503)
            response = self.hub.request('POST', '/v1/events', json.loads(entry['event']), source=True)
            self.store.finish_outbox(entry['id'], response)
        if RULE not in response.get('routed', []):
            raise Problem('中台未接下这项任务，请核对规则暂停状态', 503)
        if entry['response']:
            return response
        self.store.change(task_id, lambda t: t.update(status='queued') if t['status'] == 'created' else None)
        self.store.event(task_id, 'queued', '中台已接收任务', {'event': response.get('event')})
        return response

    def create(self, body, *, source='board'):
        if body.get('workflow') == 'goal':
            task, fresh = self.goal_engine().create(body,source)
            return {'task':task,'created':fresh,'dispatch_error':None}
        task, fresh = self.store.create(body,source=source)
        try:
            if fresh or task['status'] == 'created':
                self.dispatch(task['id'], body['request_id'])
            error = None
        except Problem as problem:
            error = str(problem)
        return {'task': self.store.get(task['id']), 'created': fresh, 'dispatch_error': error}

    def snapshot(self, focus=None):
        tasks = self.store.list()
        if focus and not any(task['id']==focus for task in tasks):
            tasks.append(self.store.get(focus))
        capability = self.capability()
        capability['goal_enabled'] = self.goal_enabled
        capability['outer_enabled'] = self.outer_enabled
        capability['ledger_available'] = True
        try:
            runs, cards = self.hub.records()
        except (OSError, sqlite3.Error, ValueError):
            runs, cards = [], []
            capability['message'] = '账本暂不可用，执行状态可能过期'
            capability['ready'] = False
            capability['ledger_available'] = False
        for task in tasks:
            omit_call_payloads(task)
            task['inbox_notes'] = self.store.notes(task['id'],for_board=True)
            with self.store.connect() as db:
                handoff = db.execute('SELECT state FROM handoffs WHERE task=? ORDER BY rowid DESC LIMIT 1',(task['id'],)).fetchone()
                task['can_move_notes']=not (task.get('goal_execution') or {}).get('outer') and task['status']=='created' and task['calls']==0 and not db.execute('SELECT 1 FROM outbox WHERE task=? LIMIT 1',(task['id'],)).fetchone()
            task['handoff_state'] = handoff[0] if handoff else None
            pending = self.store.pending_request(task['id'])
            policy = task.get('resume_requests',{}).get(pending)
            task['pending_resume'] = {'request_id':pending, **policy} if policy is not None else None
            active_call = task.get('active_call')
            if active_call:
                duration = max(0, time.time()-active_call['started_at'])
                task['elapsed_seconds'] = task.get('elapsed_seconds',0) + min(duration, active_call['reserved_seconds'])
            task_runs = [r for r in runs if r['lane'] == task['id']]
            all_cards = [c for c in cards if c['lane'] == task['id']]
            task_cards = [c for c in all_cards if c.get('binding',{}).get('version')==task['version'] and c.get('binding',{}).get('approval')==(task.get('approval') or {}).get('id')]
            goal_route = task.get('workflow') == 'goal'
            if goal_route:
                state = task['goal_execution']
                task['goal_actions'] = {
                    'pause': not state.get('outer') and (task['status'] == 'stopping' or (
                        not task['paused'] and task['status'] in
                        ('created','queued','planning','executing','reviewing','awaiting_approval'))),
                    'resume': not state.get('outer') and task['status'] == 'paused' and state['stop_requested'],
                }
                task_runs = [{'id':a['run'],'state':a.get('transport','queued'),'summary':a['phase']}
                             for a in reversed(state['assignments']) if a['run'] is not None]
                approval = task.get('approval')
                if approval and approval['version'] == task['version']:
                    task_cards = [{
                        'id':approval['id'],'version':task['version'],'state':approval['state'],
                        'choice':approval.get('choice'),'title':'确认目标范围',
                        'text':f"仅在本机隔离目录工作，预算 {task['time_budget_seconds']//60} 分钟。",
                        'options':['按这个方案执行','暂不执行'],
                        'binding':{'task':task['id'],'version':task['version'],'approval':approval['id']},
                    }]
            task['expired_cards'] = [c for c in all_cards if c not in task_cards]
            request=task.get('revision_requests',{}).get(task.get('pending_revision'))
            task['revision_pending']=request
            active = [r for r in task_runs if r['state'] in ('queued', 'claimed', 'unknown')]
            status = task['status']
            if goal_route or not capability['ledger_available'] or not capability.get('hub_available', True):
                pass  # 缺少账本时保留最后状态，不把空轮次误当成已停止。
            elif task['paused']:
                status = 'stopping' if active else 'paused'
            elif active and status in ('created','queued'):
                # 领取轮次不等于开始实施；真实阶段由执行器的模型事件决定。
                # 等待审批、完成和受阻等已写入的业务状态也不能被 claimed 覆盖。
                status = 'queued'
            elif not active and task_runs and status in ('queued','planning','executing','reviewing'):
                status = 'blocked'
                task['error'] = '执行轮次已结束但没有完成回执，请核对进度后恢复。'
            for run in task_runs:
                run.pop('error', None)
            task.update(status=status, status_label=STATES.get(status, status), runs=task_runs[:20], cards=task_cards, events=self.store.events(task['id']), workspace=str(self.store.workspace(task['id'])))
            if goal_route:
                from superagent.goal_engine import MODEL
                phase_titles = {'plan':'规划','execute':'实施','repair':'修复','review':'独立验收','replan':'重新规划',
                                'package':'工作包','assemble':'核验并合并产物','coordinate':'目标协调'}
                titles = {'dispatch_prepared':'待派工','run_bound':'中台已接单','authorized':'本人授权已保存',
                          'plan_saved':'计划已保存','checkpoint_saved':'工作区检查点已保存',
                          'evaluated':'证据已核验','recovery_reconciled':'原轮已对账，开始核对产物',
                          'coordinated':'协调决定已记录','blocked':'需要处理','delivered':'目标已验收交付','stopped':'已停止'}
                task['role_models'] = dict.fromkeys(('plan','execute','review'),MODEL)
                for item in state['decisions']:
                    title = titles.get(item['kind'],'工单状态更新')
                    if item.get('phase'):
                        title += ' · '+phase_titles.get(item['phase'], item['phase'])
                    if item.get('kind') == 'evaluated':
                        title += ' · '+('通过' if item['passed'] else '待修复')
                    task['events'].append({'at':item['at'],'kind':item['kind'],'title':title,'detail':{}})
            unresolved=any(run['state']=='unknown' for run in active)
            task['observation_stale'] = unresolved or not capability['ledger_available'] or not capability.get('hub_available', True)
            if task['observation_stale']:
                task['status_label'] = '状态待核对（上次：' + STATES.get(status, status) + '）'
            if request:
                task['status_label'] = {'waiting_stop':'等待旧执行停稳后改版','suspended':'修改流程已暂停','resume_planning':'等待新版旧进程停止后恢复','blocked_dispatch':'新版派发受阻'}.get(request['state'],'新版方案待派发')
                if task['observation_stale']:task['status_label']='状态待核对 · '+task['status_label']
            task['progress'] = {'done': sum(s.get('state') == 'completed' for s in (task.get('plan') or {}).get('steps', [])), 'total': len((task.get('plan') or {}).get('steps', []))}
        return {'tasks': tasks, 'capability': capability, 'catalogue':catalogue(self.store.root), 'at': now()}

    def answer(self, task_id, body, *, source='board'):
        task = self.store.get(task_id)
        require_v1_control(task)
        if task.get('workflow') == 'goal':
            self.goal_engine().approve(task_id,body.get('version'),body.get('card'),source,body.get('choice'))
            return {'recorded':True}
        if task['paused']:
            raise Problem('任务已暂停，请先恢复再决定', 409)
        runs, cards = self.hub.records()
        card = next((c for c in cards if c['id'] == body.get('card') and c['lane'] == task_id), None)
        if not card:
            raise Problem('此审批不属于该任务', 404)
        if card['state'] == 'open' and (type(body.get('version')) is not int or card['version'] != body['version']):
            raise Problem('审批内容已变化，请刷新后重新核对', 409)
        binding = card['binding']
        if binding.get('task') != task_id or binding.get('version') != task['version'] or binding.get('approval') != (task.get('approval') or {}).get('id'):
            raise Problem('这是旧方案的审批，不能用于当前任务', 409)
        choice = body.get('choice')
        if type(choice) is not int or choice < 0 or choice >= len(card['options']):
            raise Problem('请选择一个有效选项')
        if card['state'] != 'open':
            if card['choice'] == choice:
                return {'recorded': True, 'duplicate': True}
            raise Problem('此审批已被处理，不能覆盖旧决定', 409)
        if any(run['lane']==task_id and run['state']=='unknown' for run in runs):
            raise Problem('本单仍有执行轮次待核对，请稍后再决定',409)
        response = self.hub.request('POST', '/v1/cards/%d/answer' % card['id'], {'choice': choice})
        if not isinstance(response,dict) or type(response.get('changed')) is not bool:
            raise Problem('中台决定回执不完整，结果待核对',503)
        if response.get('changed') is False:
            _, current_cards = self.hub.records()
            current = next((c for c in current_cards if c['id'] == card['id']), None)
            if current and current['choice'] == choice:
                return {'recorded': True, 'duplicate': True}
            raise Problem('中台已有不同决定，请刷新核对', 409)
        self.store.event(task_id, 'decision', '你的决定已送达中台，等待续办', {'card': card['id'], 'choice': card['options'][choice]})
        return {'recorded': True, 'duplicate': False}

    def pause(self, task_id, *, source='board', binding=None):
        task = self.store.get(task_id)
        require_v1_control(task)
        if task.get('workflow') == 'goal':
            task = self.goal_engine().stop(task_id,source=source,binding=binding)
            return {'requested':True,'stopping':task['status']=='stopping','retry_cancel':[]}
        def mark_paused(task):
            if task.get('pending_revision'):
                task['revision_requests'][task['pending_revision']].update(state='suspended',error='修改处理已暂停，点击重试后继续')
            elif task['status'] in ('completed', 'rejected'):
                raise Problem('任务已经结束', 409)
            task['paused'] = True
        self.store.change(task_id, mark_paused)
        runs, _ = self.hub.records()
        active = [r for r in runs if r['lane'] == task_id and r['state'] in ('queued','claimed','unknown')]
        failures = []
        for run in active:
            try:
                self.hub.request('POST', '/v1/runs/%d/cancel' % run['id'], {})
            except Problem:
                failures.append(run['id'])
        self.store.event(task_id, 'pause', '已请求暂停；运行中的进程需由中台确认退出', {'pending': [r['id'] for r in active], 'retry_cancel': failures})
        return {'requested': True, 'stopping': bool(active), 'retry_cancel': failures}

    def resume(self, task_id, body, *, source='board'):
        task = self.store.get(task_id)
        require_v1_control(task)
        if task.get('workflow') == 'goal':
            task = self.goal_engine().resume(task_id,body.get('request_id'),body.get('stop_generation'),source=source)
            return {'queued':task['status']=='queued','awaiting_approval':task['status']=='awaiting_approval'}
        with self.store.lease(task_id):
            prepared = self._resume(task_id, body)
        # 派发可能在 HTTP 返回前启动 worker；网络请求不得持执行锁。
        if '_dispatch_request' not in prepared:
            return prepared
        try:
            result = self.dispatch(task_id, prepared['_dispatch_request'])
        except Problem as error:
            return {'queued': False, 'dispatch_error': str(error)}
        return {'queued': True, 'event': result.get('event')}

    def _resume(self, task_id, body):
        if self.store.get(task_id).get('pending_revision'):
            raise Problem('正在处理修改请求，请在调整面板重试或等待',409)
        request_id = body.get('request_id')
        if not re.fullmatch(r'[a-f0-9-]{36}', str(request_id)):
            raise Problem('缺少有效请求编号')
        task = self.store.get(task_id)
        policy = {key:body[key] for key in ('execution_mode','max_calls','additional_minutes') if key in body}
        known = task.get('resume_requests', {}).get(request_id)
        if known is not None and known != policy:
            raise Problem('同一恢复请求不能更改执行政策', 409)
        prior = self.store.dispatch_result(task_id, request_id)
        if prior:
            if RULE in prior.get('routed', []):
                return {'queued': True, 'event': prior.get('event')}
            return {'queued': False, 'dispatch_error': '中台未接下原请求，请重新尝试'}
        runs, _ = self.hub.records()
        if any(r['lane'] == task_id and r['state'] in ('queued','claimed','unknown') for r in runs):
            raise Problem('已有执行轮次，请等待停止或完成后再恢复', 409)
        task = self.store.recover_call(task_id)
        if task['status'] in ('completed','rejected'):
            raise Problem('该任务已结束，请创建新任务', 409)
        pending = self.store.pending_request(task_id)
        request_id = pending or request_id
        previous_policy = task.get('resume_requests', {}).get(request_id)
        if previous_policy is not None:
            if previous_policy != policy:
                raise Problem('上一项恢复派发尚未确认，请先重试原政策', 409)
            return {'_dispatch_request': request_id}
        mode = body.get('execution_mode', task.get('execution_mode', 'bounded'))
        if mode not in ('auto', 'bounded'):
            raise Problem('执行模式无效')
        limit = body.get('max_calls', task.get('max_calls'))
        if limit is not None and (type(limit) is not int or limit < (task.get('max_calls') or 0) or limit > 12):
            raise Problem('旧版调用预算不能降低，最多 12 次；也可选择自动完成')
        extra = body.get('additional_minutes', 0)
        if type(extra) is not int or extra not in (0, 30, 60):
            raise Problem('追加时间须为 30 或 60 分钟')
        seconds = task.get('time_budget_seconds', AUTO_SECONDS) + extra * 60
        proposed = {**task, 'execution_mode':mode, 'max_calls':limit, 'time_budget_seconds':seconds}
        if exhaustion(proposed):
            raise Problem(exhaustion(proposed), 409)
        if not self.capability()['configured']:
            raise Problem('执行规则暂不可用，请稍后恢复', 503)
        self.store.change(task_id, lambda t: t.update(resume_requests={**t.get('resume_requests',{}),request_id:policy}, paused=False, max_calls=limit, execution_mode=mode, time_budget_seconds=seconds, elapsed_seconds=t.get('elapsed_seconds',0), time_accounting_since=t.get('time_accounting_since') or now(), error='', status='created'))
        self.store.outbox(task_id, request_id)
        if mode != task.get('execution_mode', 'bounded') or extra:
            self.store.event(task_id, 'policy', '已按用户选择更新执行模式或时间预算', {'mode':mode,'time_budget_seconds':seconds})
        return {'_dispatch_request': request_id}

    def revise(self, task_id, body):
        if self.store.get(task_id).get('workflow') == 'goal':
            raise Problem('目标工单范围变更须保留原授权，请先停止并登记新的范围',409)
        from revisions import request_revision
        return request_revision(self,task_id,body)

    def reviewer(self, task_id, body):
        if self.store.get(task_id).get('workflow') == 'goal':
            raise Problem('目标工单的模型由有效授权确定',409)
        from revisions import change_reviewer
        return change_reviewer(self,task_id,body)

    def reconcile(self):
        from revisions import reconcile
        return reconcile(self)

    def _artifact_delivery(self,task,candidate):
        if candidate is None:
            return task.get('goal_delivery') or {}
        if not isinstance(candidate,str) or not re.fullmatch(r'[a-f0-9]{32}',candidate):
            raise Problem('作品版本无效',404)
        for record in task['goal_execution'].get('artifact_reviews',[]):
            if record['assignment']==candidate:
                return record
        delivery = task.get('goal_delivery') or {}
        if delivery.get('assignment')==candidate:
            return delivery
        with self.store.connect() as db:
            row=db.execute("""SELECT r.value FROM outer_revisions v,json_each(v.body,'$.goal_execution.artifact_reviews') r
                              WHERE v.ticket=? AND v.kind='execution_archive' AND json_extract(r.value,'$.assignment')=?""",
                           (task['id'],candidate)).fetchone()
            if row is None:
                row=db.execute("""SELECT json_extract(body,'$.goal_delivery') FROM outer_revisions
                                  WHERE ticket=? AND kind='execution_archive' AND json_extract(body,'$.goal_delivery.assignment')=?""",
                               (task['id'],candidate)).fetchone()
        if row: return json.loads(row[0])
        raise Problem('工单没有该作品版本',404)

    def artifact(self, task_id, relative, version=None, candidate=None):
        task = self.store.get(task_id)
        root = self.store.workspace(task_id)
        if candidate is not None and not (task.get('goal_execution') or {}).get('outer'):
            raise Problem('工单没有该作品版本',404)
        if task.get('workflow') == 'goal':
            delivery = self._artifact_delivery(task,candidate)
            key = delivery.get('assignment','')
            if not re.fullmatch(r'[a-f0-9]{32}',key) or version not in (None,task['version']):
                raise Problem('已核验产物不存在',404)
            root = self.store.root/'goal-attempts'/task_id/key/'verified'
            allowed = delivery['files']
        elif version is not None and version != task['version']:
            if type(version) is not int or not any(v['version']==version for v in task.get('versions',[])):
                raise Problem('成果版本不存在',404)
            archive = root.parent/'versions'/str(version)
            manifest = json.loads((archive/'manifest.json').read_text())
            allowed = manifest['files']
            root = archive/'artifacts'
        else:
            allowed=task['artifacts']
        if relative not in allowed or not isinstance(relative,str):
            raise Problem('产物未登记',404)
        candidate=root/relative
        # 拒绝任意路径分量上的符号链接，包括指向工作区内部的链接。
        if Path(relative).is_absolute() or '..' in Path(relative).parts or any(p.is_symlink() for p in [candidate,*candidate.parents] if p!=root.parent):
            raise Problem('产物路径无效',404)
        path=candidate.resolve();root=root.resolve()
        if root not in path.parents or not path.is_file() or path.stat().st_size>20*1024*1024:
            raise Problem('产物不可下载或超过 20 MB',404)
        return path

    def artifact_bytes(self, task_id, relative, version=None, candidate=None):
        task = self.store.get(task_id)
        path=self.artifact(task_id,relative,version,candidate)
        root=self.store.workspace(task_id)
        if task.get('workflow') == 'goal':
            delivery = self._artifact_delivery(task,candidate)
            root=self.store.root/'goal-attempts'/task_id/delivery['assignment']/'verified'
        elif version is not None and version!=task['version']:
            root=root.parent/'versions'/str(version)/'artifacts'
        descriptors=[]
        try:
            directory=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);descriptors.append(directory)
            parts=Path(relative).parts
            for component in parts[:-1]:
                directory=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory);descriptors.append(directory)
            handle=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory);descriptors.append(handle)
            info=os.fstat(handle)
            if not stat.S_ISREG(info.st_mode) or info.st_size>20*1024*1024:raise Problem('成果不是可读取的普通文件',404)
            chunks=[];remaining=20*1024*1024+1
            while remaining:
                chunk=os.read(handle,min(65536,remaining))
                if not chunk:break
                chunks.append(chunk);remaining-=len(chunk)
            content=b''.join(chunks)
            if len(content)>20*1024*1024:raise Problem('成果超过下载大小上限',413)
            if task.get('workflow') == 'goal' and hashlib.sha256(content).hexdigest()!=delivery['files'][relative]:
                raise Problem('已验收文件摘要不匹配',409)
            return path,content
        except OSError:
            raise Problem('成果在读取时发生变化，请刷新后重试',409) from None
        finally:
            for descriptor in reversed(descriptors):os.close(descriptor)

    def preview(self, task_id, relative, version=None):
        path,content=self.artifact_bytes(task_id,relative,version)
        if path.suffix.lower() in ('.png','.jpg','.jpeg') and len(content)<=2*1024*1024:
            mime='image/png' if content.startswith(b'\x89PNG\r\n\x1a\n') else 'image/jpeg' if content.startswith(b'\xff\xd8\xff') else None
            if mime:return {'kind':'image','data':'data:'+mime+';base64,'+base64.b64encode(content).decode()}
        if len(content)>512*1024:
            return {'kind':'download','message':'文件较大，请下载查看'}
        extension=path.suffix.lower()
        text_types={'.md','.txt','.json','.csv','.py','.js','.ts','.mjs','.css','.html','.yaml','.yml','.toml','.xml','.log','.sql'}
        if extension not in text_types:
            return {'kind':'download','message':'该格式暂不支持安全预览，请下载查看'}
        try:content=content.decode('utf-8')
        except UnicodeError:return {'kind':'download','message':'非 UTF-8 文本，请下载查看'}
        return {'kind':'markdown' if extension=='.md' else 'text','content':content,'name':path.name,'version':version or self.store.get(task_id)['version']}
