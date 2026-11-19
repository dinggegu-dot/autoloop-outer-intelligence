#!/usr/bin/env python3
"""FS-v1.0：固定本人私聊入口。控制用确定性代码，需求文本永不作为命令执行。"""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import queue
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import click_cards

HOME_OUTBOX_ID = 'workbench-home-v1'
HOME_BODY = '选择新建工单，或打开已送到飞书的工单。'
TASK_BUTTONS = {
    'status': '看进度', 'add': '补充资料', 'proposal': '看方案',
    'view_pause': '暂停任务', 'view_resume': '开始或继续',
    'watch': '订阅提醒', 'unwatch': '关闭提醒', 'board': '电脑看板', 'home': '工作台',
}

def now():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec='seconds')


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def atomic_json(path, data, previous=None):
    path = Path(path)
    if previous is not None and path.read_bytes() != previous:
        raise RuntimeError('配置同时发生变化，未覆盖；请重试')
    tmp = path.with_name(path.name + '.fs-' + str(os.getpid()) + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write('\n'); f.flush(); os.fsync(f.fileno())
        if previous is not None and path.read_bytes() != previous:
            raise RuntimeError('配置同时发生变化，未覆盖；请重试')
        os.replace(tmp, path)
    finally:
        if tmp.exists(): tmp.unlink()


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=20)
        os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,payload TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER DEFAULT 0,next_try REAL DEFAULT 0,error TEXT,received TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY AUTOINCREMENT,message_id TEXT UNIQUE NOT NULL,body TEXT NOT NULL,created TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS notes(message_id TEXT PRIMARY KEY,task_id INTEGER NOT NULL,body TEXT NOT NULL,created TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,body TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER DEFAULT 0,next_try REAL DEFAULT 0,ref TEXT,error TEXT);
          CREATE TABLE IF NOT EXISTS controls(id TEXT PRIMARY KEY,message_id TEXT UNIQUE,created TEXT,expires REAL,state TEXT NOT NULL DEFAULT 'pending',result TEXT);
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE IF NOT EXISTS task_cards(outbox_id TEXT PRIMARY KEY,task TEXT NOT NULL,first_attempt REAL);
          CREATE TABLE IF NOT EXISTS decision_cards(outbox_id TEXT PRIMARY KEY,binding TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS watches(task TEXT PRIMARY KEY,last_key TEXT,generation INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS routing_choices(outbox_id TEXT PRIMARY KEY,options TEXT NOT NULL,expires REAL NOT NULL,note TEXT,selected TEXT,first_attempt REAL);
          CREATE TABLE IF NOT EXISTS intake_prompts(outbox_id TEXT PRIMARY KEY,first_attempt REAL);
          CREATE TABLE IF NOT EXISTS click_prompts(outbox_id TEXT PRIMARY KEY,kind TEXT NOT NULL,context TEXT NOT NULL,claimed TEXT,first_attempt REAL);
          CREATE TABLE IF NOT EXISTS shown_actions(outbox_id TEXT PRIMARY KEY,actions TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS intake_titles(task TEXT PRIMARY KEY,title TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS shared_clicks(event TEXT PRIMARY KEY,body TEXT NOT NULL,received REAL NOT NULL);
        ''')

    def remember(self, event, owner):
        # 鉴权在落库前执行，其他身份、群聊和机器人消息不留正文。
        if not isinstance(event, dict): return False
        if not owner or event.get('sender_type') != 'user' or event.get('sender_id') != owner or event.get('chat_type') != 'p2p':
            return False
        if event.get('message_type')=='wb_click':return False
        mid = event.get('message_id')
        if not isinstance(mid, str) or not re.fullmatch(r'om_[A-Za-z0-9_-]{1,180}', mid):
            return False
        payload = {k: event.get(k) for k in ('message_id', 'sender_id', 'chat_type', 'sender_type', 'message_type', 'content','reply_to','root_id')}
        if not isinstance(payload['content'], str): payload['content'] = ''
        if len(payload['content']) > 8000:
            payload['content'] = ''; payload['message_type'] = 'too_long'
        with self.db:
            chat=event.get('chat_id')
            if isinstance(chat,str) and re.fullmatch(r'oc_[A-Za-z0-9_-]+',chat):
                self.set_meta('owner_p2p_chat',chat)
            cur = self.db.execute('INSERT OR IGNORE INTO messages(id,payload,received) VALUES(?,?,?)',
                                  (mid, json.dumps(payload, ensure_ascii=False), now()))
        return cur.rowcount == 1

    def remember_click(self,event,owner):
        chat=event.get('chat_id')
        if (event.get('type')!='card.action.trigger' or not owner or event.get('operator_id')!=owner
                or event.get('host')!='im_message' or not isinstance(chat,str)
                or not re.fullmatch(r'oc_[A-Za-z0-9_-]+',chat)):return False
        known=self.meta('owner_p2p_chat')
        if known and chat!=known:return False
        parent=event.get('message_id')
        eid=event.get('event_id')
        if not isinstance(eid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,180}',eid) or not isinstance(parent,str):return False
        decoded=click_cards.decode_click(event)
        shown=self.db.execute("""SELECT actions FROM shown_actions JOIN outbox ON id=outbox_id
            WHERE state='sent' AND ref=?""",(parent,)).fetchone()
        if not decoded or not shown or decoded[0] not in json.loads(shown[0]):return False
        mid='om_wbclick_'+digest(eid)
        payload=dict(message_id=mid,sender_id=owner,sender_type='user',chat_type='p2p',
                     message_type='wb_click',reply_to=parent,action=decoded[0],content=decoded[1])
        with self.db:
            # 首个事件也可能是从看板投递的卡。原卡消息号已由本人专用投递回执确认。
            if not known:self.set_meta('owner_p2p_chat',chat)
            added=self.db.execute('INSERT OR IGNORE INTO messages(id,payload,received) VALUES(?,?,?)',
                                  (mid,json.dumps(payload,ensure_ascii=False),now()))
        return added.rowcount==1

    def stage_click(self,event,owner):
        # 共享监听仅暂存本人私聊工作台按钮字段。原卡/动作的最终绑定在投递提交后验证。
        chat=event.get('chat_id')
        eid=event.get('event_id')
        parent=event.get('message_id')
        if (event.get('type')!='card.action.trigger' or event.get('operator_id')!=owner or not owner
                or event.get('host')!='im_message' or not isinstance(chat,str) or not re.fullmatch(r'oc_[A-Za-z0-9_-]+',chat)
                or not isinstance(eid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,180}',eid)
                or not isinstance(parent,str) or not re.fullmatch(r'om_[A-Za-z0-9_-]{1,180}',parent)):return
        decoded=click_cards.decode_click(event)
        if not decoded:return
        known=self.meta('owner_p2p_chat')
        if known and known!=chat:return
        if not known and not self.db.execute("SELECT 1 FROM outbox WHERE state='sent' AND ref=?",(parent,)).fetchone():
            return 'await_delivery'
        action,content=decoded
        safe=dict(type=event['type'],chat_id=chat,host='im_message',operator_id=owner,event_id=eid,message_id=parent,
                  action_tag='button',action_value=json.dumps({'wb_action':action}),form_value=json.dumps({'requirement':content}))
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO shared_clicks VALUES(?,?,?)',(eid,json.dumps(safe),time.time()))

    def drain_clicks(self,owner):
        with self.db:self.db.execute('DELETE FROM shared_clicks WHERE received<?',(time.time()-3600,))
        for row in self.db.execute('SELECT event,body FROM shared_clicks ORDER BY rowid').fetchall():
            event=json.loads(row['body'])
            if not self.db.execute("SELECT 1 FROM outbox WHERE state='sent' AND ref=?",(event['message_id'],)).fetchone():continue
            self.remember_click(event,owner)
            with self.db:self.db.execute('DELETE FROM shared_clicks WHERE event=?',(row['event'],))

    def enqueue(self, key, body):
        # 飞书文字消息有大小限制；正文完整留库，避免长需求/补充阻塞发送队列。
        if len(body) > 4500:
            body = body[:4400] + '\n……回执内容过长，已截断展示；需求和补充原文完整保存在本机收件箱。'
        self.db.execute('INSERT OR IGNORE INTO outbox(id,body) VALUES(?,?)', (key, body))

    def meta(self, key, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)', (key, json.dumps(value)))

    def bind_owner(self, owner):
        if not isinstance(owner,str) or not owner.strip():
            raise ValueError('缺少本人私聊身份')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            bound=self.meta('inbox_owner')
            if bound is None:
                # 旧库使用历史已鉴权消息核对。无法证明归属的旧投递队列不能接管。
                owners={json.loads(row[0]).get('sender_id') for row in self.db.execute('SELECT payload FROM messages')}
                if (owners and owners!={owner}) or (not owners and self.db.execute('SELECT 1 FROM outbox LIMIT 1').fetchone()):
                    raise ValueError('历史收件箱归属无法核对，请为该账号使用独立目录')
                self.set_meta('inbox_owner',digest(owner))
            elif bound!=digest(owner):
                raise ValueError('该收件箱已绑定其他本人账号，请使用独立目录')


class Hub:
    def __init__(self, settings): self.settings = settings

    def call(self, route, method='GET', body=None, source=False):
        config = read_json(self.settings['hub_config'])
        key = read_json(self.settings['private_config'])['source_key'] if source else config['owner']
        req = urllib.request.Request(config['url'] + route,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}, method=method)
        with urllib.request.urlopen(req, timeout=12) as resp: return json.load(resp)

    def state(self): return self.call('/v1/state')

    def runs(self):
        state = self.state(); result = list(state['runs']); cursor = state.get('nextBefore')
        # 读取所有实际运行记录，避免最近60轮窗口漏掉长任务。
        seen = set()
        while cursor is not None:
            if str(cursor) in seen: raise RuntimeError('中台分页异常')
            seen.add(str(cursor))
            page = self.call('/v1/state?before=' + str(int(cursor)))
            result.extend(page['runs']); cursor = page.get('nextBefore')
        return result

    def event(self, eid, kind, subject):
        return self.call('/v1/events', 'POST', {'id': eid, 'type': kind, 'subject': subject}, True)

    def cancel(self, number): return self.call('/v1/runs/%d/cancel' % number, 'POST', {})


def afr_run(run):
    return str(run.get('rule', '')).startswith('afr-cw-')


def afr_progress_run(run):
    return afr_run(run) and run['rule'] not in (
        'afr-cw-check', 'afr-cw-browser-decision',
        'afr-cw-chrome-basic', 'afr-cw-chrome-long', 'afr-cw-chrome-failure',
    )


def current_stage(plan):
    return next((stage for stage in plan['stages']
                 if not stage.get('superseded_by') and stage['state'] != 'verified'), None)


class Project:
    def __init__(self, settings, hub): self.s, self.hub = settings, hub

    def plan(self):
        p = read_json(self.s['plan'])
        if (not isinstance(p, dict) or type(p.get('enabled')) is not bool
            or not isinstance(p.get('stages'), list) or not p['stages']
            or any(not isinstance(s, dict) or not isinstance(s.get('id'), str)
                   or not isinstance(s.get('state'), str) for s in p['stages'])):
            raise ValueError('AFR计划结构不完整，状态未知')
        return p

    def pause(self):
        path = Path(self.s['plan']); raw = path.read_bytes(); p = json.loads(raw)
        p.update(enabled=False, paused_by_user=True,
                 user_paused={'at': now(), 'instruction': '飞书本人要求暂停；需明确确认且恢复前置通过才能继续'})
        atomic_json(path, p, raw)
        path = Path(self.s['rules']); raw = path.read_bytes(); rules = json.loads(raw)
        removed = [r for r in rules['rules'] if str(r.get('name', '')).startswith('afr-cw-')]
        if removed:
            # 只保存本项目派发条目，不以整份旧配置作为恢复依据。
            backup = Path(self.s['data_dir']) / ('pause-rules-' + digest(json.dumps(removed))[:12] + '.json')
            if not backup.exists(): atomic_json(backup, removed)
            rules['rules'] = [r for r in rules['rules'] if r not in removed]
            atomic_json(path, rules, raw)
        active = [r for r in self.hub.runs() if afr_run(r) and r['state'] in ('queued', 'claimed')]
        for run in active: self.hub.cancel(run['id'])
        return self.stop_result('AFR后续派发已暂停')

    def stop_result(self, prefix, number=None):
        pending = [r for r in self.hub.runs() if afr_run(r) and r['state'] in ('queued', 'claimed')
                   and (number is None or r['id'] == number)]
        return prefix + ('；正在停止轮次 ' + '、'.join(str(r['id']) for r in pending) + '，尚未确认退出。' if pending else '；已核实无相关运行/排队轮次。')

    def stop(self, number):
        run = next((r for r in self.hub.runs() if r['id'] == number), None)
        if not run or not afr_run(run): return '拒绝操作：该轮次不存在或不属于已绑定的AFR项目。'
        if run['state'] not in ('queued', 'claimed'): return '轮次 %d 已是 %s，无需再次停止。' % (number, run['state'])
        self.hub.cancel(number)
        return self.stop_result('已提交停止轮次 %d' % number, number)

    def resume_blocker(self):
        # 目前调度器尚未实现/验收，不因可写JSON或旧审批就恢复。未来接入需独立版本变更。
        return '飞书直接恢复调度的执行适配器尚未验收，此入口不能启用调度、不会启动模型。实际开发状态以计划和运行轮次为准。'


HELP = '''飞书工作入口 · FS-v1.0
工作台快捷指令：/new 新建工单，/tasks 选择已有工单。
新任务：目标（登记到看板）；回复本人任务卡可继续补充，回复「切到看板」可在电脑打开同一单。
状态 / AFR进度 / 现在做到哪里了
任务列表 / 任务 FS000001 / 任务 14（中台轮次）
新任务 AFR：你的需求
补充 FS000001：补充内容
暂停AFR / 停止任务 14 / 恢复AFR
恢复先发本人确认卡，并检查实际能力；目前AFR开发入口尚未验收，确认也不会越过该限制。
新需求只登记待办，不等于已经开发。仅支持本人私聊文字；收到回执才算已接收。离线期间未获回执的消息请在恢复后重发。
界面入口 http://127.0.0.1:5173 仅本机可打开。'''


class MobileProblem(ValueError):
    pass


class LocalBoard:
    def __init__(self,path,decision_key=None,*,pilot_task=None,pilot_board=None):
        self.path=path
        self.decision_key=decision_key
        self.pilot_task=pilot_task
        self.pilot_board=pilot_board

    def call(self,action,**fields):
        if self.pilot_task and fields.get('task')==self.pilot_task:
            if action=='control' and (fields.get('binding') or {}).get('operation')!='pause':
                return '这张测试卡只用于暂停，请从隔离看板继续。'
            if action not in ('brief','controls','control','status','milestone'):
                if action=='proposal':return {'message':'测试单的方案请到隔离看板查看。'}
                raise ValueError('测试单只接受查询和本人暂停')
            return self.pilot_board.call(action,**fields)
        item=dict(action=action,**fields)
        if action in ('answer','control','outer'):
            if not self.decision_key:
                if action=='outer':raise ValueError('智能入口需要已配置的决定签名')
                return '手机审批尚未启用，未执行。'
            item['signature']=hmac.new(self.decision_key,json.dumps(item,sort_keys=True,separators=(',',':')).encode(),hashlib.sha256).hexdigest()
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
            client.settimeout(8)
            client.connect(self.path)
            client.sendall(json.dumps(item).encode()+b'\n')
            with client.makefile('rb') as stream:
                result=json.loads(stream.readline(96000))
        if 'problem' in result:raise MobileProblem(result['problem'])
        if 'result' not in result:raise RuntimeError('本机工作台尚未确认请求')
        return result['result']

    def create(self,body,*,source):
        return self.call('intake',goal=body['goal'],request=body['request_id'])

    def add_note(self,task_id,message,body):return self.call('note',task=task_id,message=message,body=body)
    def get(self,task_id):return self.call('status',task=task_id)
    def pending_handoffs(self):return self.call('pending')
    def mark_handoff(self,request,state):return self.call('delivery',request=request,state=state)


class Bridge:
    def __init__(self, settings, hub=None, store=None, board=None):
        self.s = settings; self.store = store or Store(Path(settings['data_dir']) / 'inbox.sqlite3')
        self.store.bind_owner(settings.get('owner'))
        self.hub = hub or Hub(settings); self.project = Project(settings, self.hub)
        self.board = board
        if board is None and settings.get('workbench_socket'):
            key=Path(settings['workbench_decision_key']).read_bytes() if settings.get('workbench_decision_key') else None
            pilot=settings.get('stop_pilot') or {}
            pilot_board=None
            if pilot:
                if not re.fullmatch(r'wb-[a-f0-9]{32}',pilot['task']):
                    raise ValueError('测试工单编号无效')
                pilot_board=LocalBoard(pilot['socket'],Path(pilot['key']).read_bytes())
            self.board=LocalBoard(settings['workbench_socket'],key,
                                  pilot_task=pilot.get('task'),pilot_board=pilot_board)
        elif board is None and settings.get('workbench_dir'):
            directory = Path(settings['workbench_dir'])
            if not directory.is_absolute() or not (directory/'workbench.py').is_file():
                raise ValueError('工作台程序目录无效')
            sys.path.insert(0,str(directory))
            from workbench import Store as TaskStore
            self.board = TaskStore(Path(settings.get('workbench_runtime',directory/'.runtime')))
        pending=self.store.meta('pause_pending')
        if pending:
            # 进程离线期间无法观察，重启后重新计算稳定窗口，不能沿用旧计时。
            pending.update(quiet_since=None,last_check=0)
            with self.store.db:self.store.set_meta('pause_pending',pending)

    def status(self):
        p = self.project.plan(); runs = self.hub.runs()
        active = [r for r in runs if afr_run(r) and r['state'] in ('queued', 'claimed')]
        paused = not p.get('enabled') or p.get('user_paused') or p.get('paused_by_user')
        stage = current_stage(p)
        blocked = stage is not None and stage['state'] == 'blocked'
        latest = next((r for r in runs if afr_progress_run(r)), None)
        text = 'AFR：' + ('已暂停' if paused else '技术阻塞，暂停推进' if blocked else '计划已启用（不代表已验收）')
        text += '\n在跑 %d · 排队 %d' % (sum(r['state']=='claimed' for r in active), sum(r['state']=='queued' for r in active))
        if stage: text += '\n当前阶段：%s · %s' % (stage['id'], stage.get('state', '未知'))
        if blocked and stage.get('blockReason'):
            text += '\n阻塞原因：' + str(stage['blockReason'])[:500]
        if latest: text += '\n最近轮次：%s · %s\n%s' % (latest['id'], latest['state'], str(latest.get('summary') or '尚无完成回执')[:500])
        budget = read_json(self.s['budget'])
        text += '\n开发轮次计数：%s / %s（取消也计数；费用未知不记为0）' % (budget.get('used','未知'), budget.get('limit',60))
        text += '\n恢复前置：' + self.project.resume_blocker()
        text += '\n更新时间：' + now()
        return text

    def task_info(self, token):
        if token.upper().startswith('FS'):
            n = int(token[2:]); row = self.store.db.execute('SELECT * FROM tasks WHERE id=?',(n,)).fetchone()
            if not row: return '未找到该待办编号。'
            notes = self.store.db.execute('SELECT body FROM notes WHERE task_id=? ORDER BY created',(n,)).fetchall()
            return 'FS%06d · AFR · 已登记，尚未派发\n%s\n创建：%s%s' % (n, row['body'], row['created'], ''.join('\n补充：'+x[0] for x in notes))
        run = next((r for r in self.hub.runs() if str(r['id']) == token and afr_run(r)), None)
        if not run: return '未找到该AFR中台轮次。'
        out = 'AFR轮次 %s · %s\n规则：%s\n%s' % (token,run['state'],run['rule'],run.get('summary') or '暂无业务结果')
        link = run.get('link')
        if isinstance(link,str) and link.startswith('codex://threads/'): out += '\nCodex桌面会话：'+link
        return out + '\n轮次结束不代表产品验收通过。'

    def queue_card(self, key, task_id, receipt='已登记到看板。'):
        with self.store.db:
            self.store.enqueue(key,task_id+'\n'+receipt+'回复本卡可补资料；发「状态」「查看方案」「查看控制」查询，只有明确回复对应的当前卡才会操作。\n如需主动提醒请回复「订阅提醒」，可用「关闭提醒」取消。电脑回复「切到看板」；手机直接在飞书继续。')
            self.store.db.execute('INSERT OR IGNORE INTO task_cards(outbox_id,task) VALUES(?,?)',(key,task_id))

    def collect_handoffs(self):
        if self.board:
            for item in self.board.pending_handoffs():
                self.queue_card('handoff-'+item['request'],item['task'])

    def ask_target(self, event, note=None):
        rows=self.store.db.execute("""SELECT task,max(outbox.rowid) AS last_card FROM task_cards
            JOIN outbox ON outbox_id=id WHERE state='sent' GROUP BY task ORDER BY last_card DESC LIMIT 8""").fetchall()
        if not rows:
            return self.click_prompt(event,'home','','要关联哪项任务？当前还没有已送到飞书的工单。你可以新建工单，也可以在电脑看板选择「转到飞书继续」。')
        options=[row['task'] for row in rows]
        lines=['点击要继续的工单。']
        for i,task in enumerate(options,1):
            # 仅复述本人在飞书直接提交的短标题，看板来源不读取文件或目标正文。
            original=self.store.db.execute("""SELECT payload FROM messages JOIN task_cards
                ON outbox_id='reply-'||messages.id WHERE task=? ORDER BY messages.rowid LIMIT 1""",(task,)).fetchone()
            event_data=json.loads(original[0]) if original else {}
            content=event_data.get('content','').strip()
            match=re.fullmatch(r'(?:(?:新任务|看板任务)\s*[：:]\s*|/new\s+)(.+)',content,re.S)
            label='看板工单'
            if match:label=match[1]
            elif self.is_intake_prompt(event_data.get('reply_to')):label=content
            stored=self.store.db.execute('SELECT title FROM intake_titles WHERE task=?',(task,)).fetchone()
            if stored:label=stored[0]
            label=' '.join(label.split())[:32]
            lines.append(f'{i}. {label} · {task[-8:]}')
        lines.append('选定后把刚才的补充记入该单。' if note else '选定后发回对应任务卡，再在卡上查看或操作。')
        lines.append('其他工单可在电脑看板点「转到飞书继续」。')
        key='reply-'+event['message_id']
        with self.store.db:
            self.store.enqueue(key,'\n'.join(lines))
            self.store.db.execute('INSERT OR IGNORE INTO routing_choices(outbox_id,options,expires,note) VALUES(?,?,?,?)',
                                  (key,json.dumps(options),time.time()+1800,note))
        return '已生成选择卡。'

    def choose_target(self, row, event, text):
        match=re.fullmatch(r'选\s*([1-8])',text)
        options=json.loads(row['options'])
        if not match or int(match[1])>len(options):
            return '请直接回复选择卡「选 序号」。'
        task=options[int(match[1])-1]
        # 固定首次选择；跨库写入失败时仅原事件可以按同一消息号续办。
        with self.store.db:
            changed=self.store.db.execute('UPDATE routing_choices SET selected=? WHERE outbox_id=? AND selected IS NULL AND expires>?',
                                          (event['message_id'],row['outbox_id'],time.time())).rowcount
            if not changed and row['selected']!=event['message_id']:
                return '选择卡已用过或已过期，请查看原收据。需要另选时重新发「补充：内容」或「我的工单」。'
        if row['note']:
            self.board.add_note(task,row['outbox_id'][len('reply-'):],row['note'])
            return '补充已登记，当前归属可在看板核对。需要操作工单时，请回复相应任务卡或发「我的工单」。'
        self.queue_card('reply-'+event['message_id'],task,self.task_status(task)+'\n')
        return '工单已选定。'

    def task_status(self,task_id):
        if isinstance(self.board,LocalBoard):
            return self.board.call('brief',task=task_id)
        task=self.board.get(task_id)
        return f"任务 {task_id} · v{task['version']}\n工作台最后记录：{task['status']}（非实时执行证明）。可回复原卡继续补充。"

    def is_intake_prompt(self, parent):
        if not isinstance(parent,str):return False
        return self.store.db.execute("""SELECT 1 FROM intake_prompts JOIN outbox ON id=outbox_id
            WHERE state='sent' AND ref=?""",(parent,)).fetchone() is not None

    def work_message(self, event, text, compact):
        parent = event.get('reply_to')
        if not parent and event.get('root_id'):
            return '没有明确引用原任务卡，请直接回复原卡。未保存或执行。'
        if self.board and not parent and text == '工作台':
            return self.click_prompt(event, 'home', '', HOME_BODY)
        if self.board and not parent and text == '新建工单':
            text = '/new'
        if self.board and compact=='看进度':
            if not parent:
                return self.ask_target(event)
            compact='状态'
        shortcut=re.fullmatch(r'/(new|tasks)(?:\s+(.+))?',text,re.S)
        if shortcut:
            if parent:return '请在本人私聊输入框单独使用快捷指令。'
            if not self.board:return '工作台入口尚未接通。'
            if shortcut[1]=='tasks':
                if shortcut[2]:return '查看已有工单请单独发送 /tasks。'
                return self.ask_target(event)
            if shortcut[2]:return self.register_goal(event,shortcut[2].strip())
            key='reply-'+event['message_id']
            with self.store.db:
                self.store.enqueue(key,'新建工单：直接回复本卡，写下你要完成的事即可。每条回复登记一张新工单。以后也可以在输入框选 /new，接着写目标再发送。')
                self.store.db.execute('INSERT OR IGNORE INTO intake_prompts(outbox_id) VALUES(?)',(key,))
            return '请回复新建工单卡填写目标。'
        create = re.fullmatch(r'(?:新任务|看板任务)\s*[：:]\s*(.+)',text,re.S)
        if not parent and not create:
            note=re.fullmatch(r'补充\s*[：:]\s*(.+)',text,re.S)
            if self.board and (note or compact in ('我的工单','暂停','暂停任务','暂停开发','停止','停止任务','恢复','恢复任务','恢复开发','继续开发')):
                return self.ask_target(event,note[1].strip() if note else None)
            return None
        if not self.board:
            return '工作台入口尚未接通，未登记或执行。'
        if parent:
            if self.is_intake_prompt(parent):
                if compact in ('状态','查看方案','查看控制','确认执行','确认暂停','确认继续','暂不执行','暂停','暂停任务','恢复任务','好的'):
                    return '这是新建入口。请写具体目标，或在输入框用 /tasks 选择已有工单。'
                return self.register_goal(event,create[1].strip() if create else text)
            menu=self.store.db.execute("""SELECT routing_choices.* FROM routing_choices JOIN outbox ON outbox_id=id
                WHERE ref=? AND state='sent'""",(parent,)).fetchone() if isinstance(parent,str) else None
            if menu:return self.choose_target(menu,event,text)
            row = self.store.db.execute("SELECT task FROM task_cards JOIN outbox ON outbox_id=id WHERE ref=? AND state='sent'",(parent,)).fetchone() if isinstance(parent,str) else None
            if not row:
                return '无法核对这条引用。请在看板找到原任务并点「转到飞书继续」重新领取卡片，不要重新建单。未执行操作。'
            task_id = row['task']
            if re.fullmatch(r'选\s*\d+',text):
                return '请选择「我的工单」生成的选择卡，直接回复那张卡的序号。'
            if compact in ('订阅提醒','关闭提醒') and isinstance(self.board,LocalBoard):
                with self.store.db:
                    if compact=='订阅提醒':
                        self.store.db.execute('INSERT OR IGNORE INTO watches(task,generation) VALUES(?,?)',(task_id,time.time_ns()))
                    else:
                        self.store.db.execute('DELETE FROM watches WHERE task=?',(task_id,))
                        self.store.db.execute("UPDATE outbox SET state='cancelled' WHERE state='pending' AND id IN (SELECT outbox_id FROM task_cards WHERE task=? AND outbox_id LIKE 'watch-%')",(task_id,))
                return task_id+(' · 已订阅本人提醒：需要决定、阻塞或完成才提示。' if compact=='订阅提醒' else ' · 已关闭提醒。任务本身未暂停。')
            if compact in ('切到看板','返回看板','电脑看板'):
                port = int(self.s.get('workbench_port',8765))
                pilot=self.s.get('stop_pilot') or {}
                if task_id==pilot.get('task'):
                    port=int(pilot['port'])
                return f'在运行工作台的电脑打开同一张单：http://127.0.0.1:{port}/?task={task_id}#tasks\n手机不用打开此地址，仍可回复原卡。'
            decision=self.store.db.execute('SELECT binding FROM decision_cards JOIN outbox ON outbox_id=id WHERE ref=? AND state=?',(parent,'sent')).fetchone()
            if text in ('确认暂停','确认继续'):
                binding=json.loads(decision['binding']) if decision else {}
                if binding.get('operation')!=('pause' if text=='确认暂停' else 'resume'):
                    return '请直接回复当前任务的对应控制卡，未执行。'
                return self.board.call('control',task=task_id,binding=binding,parent=parent,event=event['message_id'])
            if text in ('确认执行','暂不执行'):
                if not decision:return '请先回复原任务卡「查看方案」，再直接回复新生成的方案卡。未提交决定。'
                if 'operation' in json.loads(decision['binding']):return '这不是方案审批卡，未提交。'
                return self.board.call('answer',task=task_id,binding=json.loads(decision['binding']),choice=0 if text=='确认执行' else 1,
                                       parent=parent,event=event['message_id'])
            if decision and compact not in ('查看方案','方案审批','状态','查看状态','进度','查看控制'):
                return '此卡只接受准确的「确认执行」或「暂不执行」，未提交决定、未记录补充。补资料请回复原任务卡。'
            if compact in ('查看方案','方案审批','查看控制','开始规划','暂停任务','恢复任务') and isinstance(self.board,LocalBoard):
                result=self.board.call('proposal' if compact in ('查看方案','方案审批') else 'controls',task=task_id)
                if 'binding' not in result:return result['message']
                if event.get('action') in ('view_pause', 'view_resume'):
                    if result['binding'].get('operation') != event['action'][5:]:
                        return '工单状态已变化，请在更新后的任务卡选择操作。'
                key='reply-'+event['message_id']
                with self.store.db:
                    self.store.enqueue(key,result['body'])
                    self.store.db.execute('INSERT OR IGNORE INTO task_cards(outbox_id,task) VALUES(?,?)',(key,task_id))
                    self.store.db.execute('INSERT OR IGNORE INTO decision_cards VALUES(?,?)',(key,json.dumps(result['binding'])))
                return '请核对当前方案卡。'
            if compact in ('状态','进度','查看状态'):
                return self.task_status(task_id)
            if re.match(r'^(暂停|停止|继续执行|恢复|同意|批准|拒绝|好的|切换|新任务)',compact):
                return '未执行控制或变更批准，请在看板处理当前任务。本卡支持回复补充资料。'
            if not text:
                return '补充内容为空，未保存。'
            self.board.add_note(task_id,event['message_id'],text)
            return '补充已保存，可在看板「跨端接续」核对当前归属。回复任务卡会继续操作卡片对应的工单。'
        return self.register_goal(event,create[1].strip())

    def register_goal(self,event,goal,reply_event=None):
        if self.s.get('outer_intelligence'):
            from outer_bridge import submit
            return submit(self,event,goal)
        if not 1 <= len(goal) <= 6000:
            return '任务目标须为 1–6000 字，未登记。'
        request = str(uuid.uuid5(uuid.NAMESPACE_URL,'odyssey:feishu:'+event['message_id']))
        task,_ = self.board.create({'name':goal[:80],'goal':goal,'request_id':request,
                                    'role_models':{role:'gpt-6-astra' for role in ('plan','execute','review')}},source='feishu')
        managed = task.get('workflow') == 'goal'
        receipt = '目标已登记，后台将准备方案。需要决定或交付时会提醒。' if managed else '已登记到看板。'
        self.queue_card('reply-'+(reply_event or event)['message_id'],task['id'],receipt)
        with self.store.db:
            self.store.db.execute('INSERT OR IGNORE INTO intake_titles VALUES(?,?)',(task['id'],' '.join(goal.split())[:32]))
            if managed and not self.store.meta('goal-watch:'+task['id']):
                self.store.db.execute('INSERT OR IGNORE INTO watches(task,generation) VALUES(?,?)',(task['id'],time.time_ns()))
                self.store.set_meta('goal-watch:'+task['id'],True)
        if managed:
            return task['id']+' · 目标已入账。'
        return task['id']+' · 已登记，未启动执行。'

    def queue_prompt(self,key,kind,context,body):
        with self.store.db:
            self.store.enqueue(key,body)
            self.store.db.execute('INSERT OR IGNORE INTO click_prompts(outbox_id,kind,context) VALUES(?,?,?)',
                                  (key,kind,context))

    def enqueue_home(self):
        """登记一张可置顶的本人私聊入口卡，重复调用保留原投递状态和消息号。"""
        if self.board is None:
            raise ValueError('工作台入口尚未接通')
        self.queue_prompt(HOME_OUTBOX_ID, 'home', '', HOME_BODY)
        return HOME_OUTBOX_ID

    def click_prompt(self,event,kind,context,body):
        self.queue_prompt('reply-'+event['message_id'],kind,context,body)
        return body

    def clarify(self,event):
        if self.board and event['content'].strip():
            return self.click_prompt(event,'clarify',event['message_id'],'这段需求要新建工单，还是补充到已有工单？')
        return '请写下具体需求。未执行任何控制。'

    def handle_click(self,event):
        action=event['action']
        parent=event['reply_to']
        if action=='home':return self.click_prompt(event,'home','',HOME_BODY)
        if action=='list':
            if self.s.get('outer_intelligence'):
                from outer_bridge import directory
                return directory(self,event)
            return self.ask_target(event)
        if action=='new':return self.click_prompt(event,'new','','填写需求后点击「创建工单」。')
        card=self.store.db.execute("SELECT task FROM task_cards JOIN outbox ON id=outbox_id WHERE ref=? AND state='sent'",(parent,)).fetchone()
        if action=='add' and card:return self.click_prompt(event,'note',card[0],'填写要补充到这张工单的内容。')
        prompt=self.store.db.execute("SELECT click_prompts.* FROM click_prompts JOIN outbox ON id=outbox_id WHERE ref=? AND state='sent'",(parent,)).fetchone()
        if action in ('submit_goal','submit_note','create_here','attach_here'):
            legacy_new=action=='submit_goal' and self.is_intake_prompt(parent)
            if legacy_new:
                with self.store.db:
                    self.store.db.execute("""INSERT OR IGNORE INTO click_prompts(outbox_id,kind,context)
                        SELECT id,'new','' FROM outbox WHERE ref=? AND state='sent'""",(parent,))
                prompt=self.store.db.execute('SELECT click_prompts.* FROM click_prompts JOIN outbox ON id=outbox_id WHERE ref=?',(parent,)).fetchone()
            if not prompt:return '入口已失效。'
            with self.store.db:
                claimed=self.store.db.execute('UPDATE click_prompts SET claimed=? WHERE outbox_id=? AND (claimed IS NULL OR claimed=?)',
                                              (event['message_id'],prompt['outbox_id'],event['message_id'])).rowcount
            if not claimed:return '这份需求已经提交，请查看上次回执。'
            if action=='submit_goal':return self.register_goal(event,event['content'])
            if action=='submit_note':
                self.board.add_note(prompt['context'],event['message_id'],event['content'])
                return '补充已保存到所选工单。'
            original=json.loads(self.store.db.execute('SELECT payload FROM messages WHERE id=?',(prompt['context'],)).fetchone()[0])
            if action=='create_here':return self.register_goal(original,original['content'].strip(),reply_event=event)
            return self.ask_target(event,original['content'].strip())
        commands={'status':'状态','proposal':'查看方案','controls':'查看控制','approve':'确认执行','decline':'暂不执行',
                  'view_pause':'查看控制','view_resume':'查看控制',
                  'pause':'确认暂停','resume':'确认继续','watch':'订阅提醒','unwatch':'关闭提醒','board':'切到看板'}
        cmd=commands.get(action)
        if re.fullmatch(r'pick_[1-8]',action):cmd='选 '+action[-1]
        if cmd:
            receipt=self.work_message(event,cmd,cmd.replace(' ',''))
            if card and action in ('status','proposal','controls','view_pause','view_resume','watch','unwatch','approve','decline','pause','resume'):
                self.queue_card('reply-'+event['message_id'],card['task'],receipt+'\n')
            elif action.startswith('pick_') and '过期' in receipt:
                return self.click_prompt(event,'home','','这张选单已用过或已过期，请重新选择。')
            return receipt
        return '入口已失效。'

    def handle(self, e):
        if self.s.get('outer_intelligence'):
            from outer_bridge import parent_card, respond
            card=parent_card(self,e)
            if card and e.get('message_type') in ('text','wb_click'):
                try:return respond(self,e,card)
                except MobileProblem as error:return str(error)
        if e.get('message_type')=='wb_click':return self.handle_click(e)
        if e.get('message_type') != 'text' and not (e.get('message_type')=='post' and re.fullmatch(r'/(?:new|tasks)(?:\s+.+)?',e['content'].strip(),re.S)):
            return '未执行：仅接受私聊文字（单条最多8000字）。新建工单可用 /new。'
        text = e['content'].strip(); compact = re.sub(r'[\s？?！!。]+','',text).lower(); mid=e['message_id']
        if self.s.get('outer_intelligence') and not e.get('reply_to') and compact in ('我的工单','/tasks','看进度','状态','进度','暂停','暂停任务','停止','停止任务','继续','恢复','恢复任务'):
            from outer_bridge import directory
            return directory(self,e)
        reply = self.work_message(e,text,compact)
        if reply is not None:return reply
        if self.s.get('outer_intelligence'):
            from outer_bridge import submit
            return submit(self,e,text)
        if self.s.get('workbench_only'):
            return self.clarify(e)
        if compact in ('暂停','暂停任务','暂停开发','停止','停止任务','恢复','恢复任务','恢复开发','继续开发'):
            return '要操作哪项任务？请回复对应任务卡，或明确指定 AFR。未暂停或恢复任何项目。'
        if compact in ('帮助','help','菜单','怎么用','如何使用'): return HELP
        if compact in ('状态','进度','afr状态','afr进度','现在怎么样','现在做到哪里了','查看状态'): return self.status()
        if compact == '暂停afr':
            # 先保存停止意图；停止期间新排队的轮次也由后续轮询处理，重启不丢失。
            with self.store.db:self.store.set_meta('pause_pending', {'request':mid,'quiet_since':None,'last_check':0})
            return self.project.pause()+'\n暂停收尾检查继续进行，稳定确认后另发回执。'
        if compact in ('恢复afr','继续afr'):
            p=self.project.plan()
            if p['enabled'] and not p.get('user_paused') and not p.get('paused_by_user'):
                stage = current_stage(p)
                if stage and stage['state'] == 'blocked':
                    reason = stage.get('blockReason') or '请查看执行记录中的验证结果。'
                    return f"AFR 暂停推进于 {stage['id']}。当前是技术阻塞：{reason}\n本次未触发重试或生成审批卡。"
                return 'AFR计划已启用，无需从飞书恢复。本次未改变开发状态；请发“状态”查看实际执行。'
            cid = 'R'+digest(mid)[:20]
            with self.store.db:
                self.store.db.execute('INSERT OR IGNORE INTO controls(id,message_id,created,expires) VALUES(?,?,?,?)',(cid,mid,now(),time.time()+1800))
            self.hub.event('fs-resume-'+digest(mid), 'fs.resume', cid)
            return '恢复请求已登记：%s。请查看单独的决策卡。当前AFR仍暂停；通过本人确认且前置验收通过才可恢复。' % cid
        match = re.fullmatch(r'停止(?:任务|轮次)?\s*#?(\d+)', text)
        if match: return self.project.stop(int(match[1]))
        if compact in ('任务列表','任务','待办'):
            rows=self.store.db.execute('SELECT id,body FROM tasks ORDER BY id DESC LIMIT 15').fetchall()
            lines=['AFR需求待办（已登记≠已派发）：']+['FS%06d · %s'%(r['id'],r['body'][:100]) for r in rows]
            runs=[r for r in self.hub.runs() if afr_run(r) and r.get('rule')!='afr-cw-check'][:5]
            lines+=['最近中台轮次：']+['%s · %s · %s'%(r['id'],r['rule'],r['state']) for r in runs]
            return '\n'.join(lines)
        match=re.fullmatch(r'(?:查看)?任务\s*#?(FS\d{1,10}|\d{1,10})',text,re.I)
        if match:return self.task_info(match[1].upper())
        match=re.fullmatch(r'新任务\s*AFR\s*[：:]\s*(.+)',text,re.I|re.S)
        if match:
            with self.store.db:
                self.store.db.execute('INSERT OR IGNORE INTO tasks(message_id,body,created) VALUES(?,?,?)',(mid,match[1].strip(),now()))
            row=self.store.db.execute('SELECT id FROM tasks WHERE message_id=?',(mid,)).fetchone(); tid='FS%06d'%row[0]
            self.hub.event('fs-intake-'+digest(mid),'fs.task.registered',tid)
            return '%s · AFR\n已登记，尚未派发。本次提交不会启动开发或改变当前开发状态。\n查看：任务 %s\n补充：补充 %s：内容'%(tid,tid,tid)
        match=re.fullmatch(r'补充\s*(FS\d{1,10})\s*[：:]\s*(.+)',text,re.I|re.S)
        if match:
            n=int(match[1][2:])
            if not self.store.db.execute('SELECT 1 FROM tasks WHERE id=?',(n,)).fetchone():return '未找到该待办编号，未保存补充。'
            with self.store.db:self.store.db.execute('INSERT OR IGNORE INTO notes VALUES(?,?,?,?)',(mid,n,match[2].strip(),now()))
            self.hub.event('fs-followup-'+digest(mid),'fs.task.followup','FS%06d'%n)
            return '补充已保存到 FS%06d；仍未派发开发。'%n
        return self.clarify(e)

    def process_pending(self):
        rows=self.store.db.execute("SELECT * FROM messages WHERE state IN ('pending','processing') AND next_try<=? ORDER BY received LIMIT 5",(time.time(),)).fetchall()
        for row in rows:
            with self.store.db:self.store.db.execute("UPDATE messages SET state='processing',attempts=attempts+1 WHERE id=?",(row['id'],))
            try:
                body=self.handle(json.loads(row['payload']))
                with self.store.db:
                    self.store.enqueue('reply-'+row['id'],body)
                    self.store.db.execute("UPDATE messages SET state='completed',error=NULL WHERE id=?",(row['id'],))
            except Exception as exc:
                # 不把异常中可能包含的URL、token、响应正文复制给用户。
                code=type(exc).__name__
                with self.store.db:
                    self.store.db.execute("UPDATE messages SET state='pending',error=?,next_try=? WHERE id=?",(code,time.time()+min(300,2**min(row['attempts']+1,8)),row['id']))
                    if row['attempts']==0:self.store.enqueue('deferred-'+row['id'],'处理暂未完成（%s）；已保留请求并将重试。状态未知不等于正常，未确认控制成功。'%code)

    def task_actions(self,task_id):
        actions = ['status', 'add']
        if isinstance(self.board, LocalBoard):
            if getattr(self.board, 'decision_key', None):
                try:
                    controls = self.board.call('controls', task=task_id)
                    proposal = self.board.call('proposal', task=task_id)
                except (OSError, RuntimeError, ValueError):
                    controls = proposal = {}
                for result, kind in ((controls, 'controls'), (proposal, 'proposal')):
                    binding = result.get('binding') if isinstance(result, dict) else None
                    if not isinstance(binding, dict) or binding.get('task') != task_id:
                        continue
                    if kind == 'controls' and binding.get('operation') in ('pause', 'resume'):
                        actions.append('view_' + binding['operation'])
                    elif kind == 'proposal' and binding.get('approval') and binding.get('card'):
                        actions.append('proposal')
            watched = self.store.db.execute('SELECT 1 FROM watches WHERE task=?', (task_id,)).fetchone()
            actions.append('unwatch' if watched else 'watch')
        return actions + ['board', 'home']

    def button_card(self,key,body):
        def row(table):
            return self.store.db.execute(f'SELECT * FROM {table} WHERE outbox_id=?',(key,)).fetchone()
        decision=row('decision_cards')
        menu=row('routing_choices')
        task=row('task_cards')
        prompt=row('click_prompts')
        form=None
        nav=[('new','新建工单'),('list','我的工单')]
        if decision:
            binding=json.loads(decision['binding'])
            op=binding.get('operation')
            actions=[(op,'确认暂停' if op=='pause' else '确认开始或继续')] if op else [('approve','确认执行'),('decline','暂不执行')]
            title='工单控制' if op else '方案确认'
            body=body.split('\n直接回复本卡')[0].split('\n仅直接回复本卡')[0]
        elif menu:
            title='选择工单'
            labels=[line.split('. ',1)[1] for line in body.splitlines() if re.match(r'^[1-8]\. ',line)]
            actions=[('pick_'+str(i),label) for i,label in enumerate(labels,1)]
            body='点击工单，将刚才的补充存入所选工单。' if menu['note'] else '点击工单，查看当前进度和可用操作。'
        elif prompt and prompt['kind']=='outer':
            outer=json.loads(prompt['context'])
            title='目标工单'
            actions=outer['actions']
        elif prompt and prompt['kind']=='home':
            title='工作台';actions=nav
        elif prompt and prompt['kind']=='clarify':
            title='需求归属';actions=[('create_here','新建工单'),('attach_here','补充已有工单')]
        elif prompt or row('intake_prompts'):
            note=prompt and prompt['kind']=='note'
            title='补充资料' if note else '新建工单'
            if note:body='目标工单 '+prompt['context']
            else:body='写下你要完成的事。'
            form=('submit_note','保存补充') if note else ('submit_goal','创建工单')
            actions=[]
        elif task:
            title='工单操作'
            # 同一投递键可能已被飞书接受。重试时必须复用首次展示的动作。
            saved=row('shown_actions')
            ids=json.loads(saved['actions']) if saved else self.task_actions(task['task'])
            actions=[(action,TASK_BUTTONS[action]) for action in ids]
            body=body.split('回复本卡可补资料')[0]
        else:raise ValueError('缺少卡片绑定')
        shown=[action for action,_ in actions]
        if form:shown.append(form[0])
        return click_cards.render(title,body,actions,form),shown

    def flush(self, transport):
        if self.s.get('outer_intelligence'):
            from outer_bridge import mirror_notifications
            try:mirror_notifications(self)
            except (OSError,ValueError,RuntimeError):pass  # 引擎 outbox 保留原记录，下轮续投。
        try:self.collect_handoffs()
        except (OSError,RuntimeError,ValueError):pass
        rows=self.store.db.execute("SELECT * FROM outbox WHERE state='pending' AND next_try<=? ORDER BY rowid LIMIT 5",(time.time(),)).fetchall()
        for row in rows:
            try:
                if row['id'].startswith('watch-'):
                    watch=self.store.db.execute('SELECT watches.* FROM watches JOIN task_cards USING(task) WHERE outbox_id=?',(row['id'],)).fetchone()
                    current=self.board.call('milestone',task=watch['task']) if watch else None
                    if current and not current['known']:
                        with self.store.db:self.store.db.execute('UPDATE outbox SET next_try=? WHERE id=?',(time.time()+30,row['id']))
                        continue
                    if not watch or not current.get('key') or row['id']!='watch-'+str(watch['generation'])+'-'+digest(watch['task']+':'+current['key']):
                        with self.store.db:self.store.db.execute("UPDATE outbox SET state='cancelled' WHERE id=?",(row['id'],))
                        continue
                card=self.store.db.execute('SELECT first_attempt FROM task_cards WHERE outbox_id=?',(row['id'],)).fetchone()
                table='task_cards'
                if not card:
                    table='routing_choices'
                    card=self.store.db.execute('SELECT first_attempt FROM routing_choices WHERE outbox_id=?',(row['id'],)).fetchone()
                if not card:
                    table='intake_prompts'
                    card=self.store.db.execute('SELECT first_attempt FROM intake_prompts WHERE outbox_id=?',(row['id'],)).fetchone()
                if not card:
                    table='click_prompts'
                    card=self.store.db.execute('SELECT first_attempt FROM click_prompts WHERE outbox_id=?',(row['id'],)).fetchone()
                if card and card[0] is not None and time.time()-card[0]>=3500:
                    with self.store.db:self.store.db.execute("UPDATE outbox SET state='unknown' WHERE id=?",(row['id'],))
                    continue
                if card and card[0] is None:
                    with self.store.db:self.store.db.execute(f'UPDATE {table} SET first_attempt=? WHERE outbox_id=?',(time.time(),row['id']))
                delivery='fs-'+digest(row['id'])[:40]
                if card:
                    rendered,shown=self.button_card(row['id'],row['body'])
                    if table=='task_cards':
                        with self.store.db:
                            self.store.db.execute('INSERT OR IGNORE INTO shown_actions VALUES(?,?)',
                                                  (row['id'],json.dumps(shown)))
                    ref=transport.send_card(row['body'],delivery,rendered)
                else:ref=transport.send(row['body'],delivery)
                if card and (not isinstance(ref,str) or not re.fullmatch(r'om_[A-Za-z0-9_-]{1,180}',ref)):
                    raise ValueError('任务卡回执缺少消息号')
                with self.store.db:
                    if card:self.store.db.execute('INSERT OR REPLACE INTO shown_actions VALUES(?,?)',(row['id'],json.dumps(shown)))
                    self.store.db.execute("UPDATE outbox SET state='sent',ref=?,error=NULL WHERE id=?",(ref,row['id']))
            except Exception as exc:
                with self.store.db:self.store.db.execute('UPDATE outbox SET attempts=attempts+1,error=?,next_try=? WHERE id=?',(type(exc).__name__,time.time()+min(300,2**min(row['attempts']+2,8)),row['id']))
        if self.board:
            try:items=self.board.pending_handoffs()
            except (OSError,RuntimeError,ValueError):return
            for item in items:
                state=self.store.db.execute('SELECT state FROM outbox WHERE id=?',('handoff-'+item['request'],)).fetchone()
                if state and state[0] in ('sent','unknown'):
                    try:self.board.mark_handoff(item['request'],state[0])
                    except (OSError,RuntimeError,ValueError):return

    def monitor(self):
        self.monitor_watches()
        if self.s.get('workbench_only'):return
        try:
            self.maintain_controls()
            runs=self.hub.runs(); p=self.project.plan()
            important=[r for r in runs if afr_progress_run(r) and r['state'] not in ('queued','claimed')]
            previous=self.store.meta('run_states')
            current={str(r['id']):r['state'] for r in important}
            with self.store.db:
                if previous is not None:
                    for r in important:
                        if previous.get(str(r['id']))!=r['state']:
                            self.store.enqueue('run-change-%s-%s'%(r['id'],r['state']),self.task_info(str(r['id'])))
                self.store.set_meta('run_states',current)
                self.store.set_meta('last_monitor',{'at':now(),'ok':True})
        except Exception as exc:
            with self.store.db:
                last=self.store.meta('last_monitor',{})
                if last.get('ok') is not False:self.store.enqueue('monitor-failed-'+digest(last.get('at','initial'))[:12], '暂时无法核实AFR状态，状态为未知；飞书入口将自动重试，不会自动恢复开发。')
                self.store.set_meta('last_monitor',{'at':now(),'ok':False,'error':type(exc).__name__})

    def monitor_watches(self):
        if not isinstance(self.board,LocalBoard):return
        for watch in self.store.db.execute('SELECT * FROM watches ORDER BY task').fetchall():
            task=watch['task']
            try:current=self.board.call('milestone',task=task)
            except (OSError,RuntimeError,ValueError):continue
            if not current['known'] or current.get('key')==watch['last_key']:continue
            key=current.get('key')
            generation=watch['generation']+1
            with self.store.db:
                self.store.db.execute("UPDATE outbox SET state='cancelled' WHERE state='pending' AND id IN (SELECT outbox_id FROM task_cards WHERE task=? AND outbox_id LIKE 'watch-%')",(task,))
                if key:
                    delivery='watch-'+str(generation)+'-'+digest(task+':'+key)
                    self.store.enqueue(delivery,current['body']+'\n直接回复此卡「状态」可复核。')
                    self.store.db.execute('INSERT OR IGNORE INTO task_cards(outbox_id,task) VALUES(?,?)',(delivery,task))
                self.store.db.execute('UPDATE watches SET last_key=?,generation=? WHERE task=?',(key,generation,task))

    def maintain_controls(self):
        if self.s.get('workbench_only'):return
        pending=self.store.meta('pause_pending')
        if not pending or time.time()-pending.get('last_check',0)<3:return
        if time.time()-pending.get('last_check',0)>6:pending['quiet_since']=None
        # 规则热加载与停止回执有时间差，不能仅凭一次空列表宣称收尾完成。
        try:
            self.project.pause()
            active=[r for r in self.hub.runs() if afr_run(r) and r['state'] in ('queued','claimed')]
            pending['last_check']=time.time()
            if active:pending['quiet_since']=None
            elif pending['quiet_since'] is None:pending['quiet_since']=time.time()
            elif time.time()-pending['quiet_since']>=10:
                with self.store.db:
                    self.store.enqueue('pause-finished-'+pending['request'],'AFR暂停已完成：派发规则已撤下，持续核实无相关运行/排队轮次。')
                    self.store.set_meta('pause_pending',None)
                return
            with self.store.db:self.store.set_meta('pause_pending',pending)
        except Exception:
            # 保留持久停止意图；主循环继续尝试，不能丢弃成“成功”。
            pending.update(quiet_since=None,last_check=time.time())
            with self.store.db:
                self.store.set_meta('pause_pending',pending)
                self.store.enqueue('pause-pending-'+pending['request'],'AFR暂停收尾尚未确认完成，正在重试；不能据此认为所有进程已经退出。')

    def control_result(self, context):
        events=[e for e in context.get('events',[]) if e.get('source')=='feishu-inbox' and e.get('type')=='fs.resume']
        answers=context.get('answers',[])
        # 回答轮次通常只有内部answer事件，因此从ask.about取不可执行的请求ID。
        cid=events[-1].get('subject') if events else (answers[-1].get('about') if answers else None)
        if not isinstance(cid,str) or not re.fullmatch(r'R[0-9a-f]{20}',cid):return '未执行：恢复请求身份不匹配。',{}
        row=self.store.db.execute('SELECT * FROM controls WHERE id=?',(cid,)).fetchone()
        if not row:return '未执行：没有可信的本机恢复请求。',{}
        if row['state']!='pending':return row['result'] or '该请求已处理。',{}
        if answers and (answers[-1].get('by')!=self.s['owner'] or answers[-1].get('about')!=cid):
            return '未执行：恢复确认身份不匹配。',{}
        p=self.project.plan()
        if p['enabled'] and not p.get('user_paused') and not p.get('paused_by_user'):
            result='该恢复请求已失效：AFR计划当前已启用，本次未改变开发状态。请发送“状态”查看实际执行。'
            with self.store.db:self.store.db.execute("UPDATE controls SET state='closed',result=? WHERE id=?",(result,cid))
            return result,{}
        if time.time()>row['expires']:
            result='恢复确认已过期；AFR保持暂停，请重新发“恢复AFR”。'
        elif answers:
            a=answers[-1]
            if a.get('by')!=self.s['owner'] or a.get('about')!=cid:return '未执行：恢复确认身份不匹配。',{}
            result=('仍暂停：'+self.project.resume_blocker()) if a.get('choice')==0 else '已按你的决定保持AFR暂停。'
        else:
            return '等待本人确认；AFR仍暂停。',{'ask':[{'title':'AFR恢复前置确认','text':'请求 '+cid+'\n'+self.project.resume_blocker()+'\n选择检查不会跳过前置，也不会自动扩大权限。','options':['确认检查恢复条件','保持暂停'],'recommend':1,'key':cid,'about':cid}]}
        with self.store.db:self.store.db.execute('UPDATE controls SET state=?,result=? WHERE id=?',('blocked' if answers and answers[-1].get('choice')==0 else 'closed',result,cid))
        return result,{}


class Lark:
    def __init__(self, s):
        self.s=s; self.env=dict(os.environ,LARKSUITE_CLI_NO_UPDATE_NOTIFIER='1',LARKSUITE_CLI_NO_SKILLS_NOTIFIER='1')

    def send(self, text, key):
        return self._send(text,key,['--text','-'])

    def send_card(self, text, key, card):
        return self._send(json.dumps(card,ensure_ascii=False),key,['--msg-type','interactive','--content','-'])

    def _send(self, text, key, flags):
        run=subprocess.run([self.s['lark'],'im','+messages-send','--as','bot','--user-id',self.s['owner'],*flags,
                            '--idempotency-key',key],input=text,text=True,capture_output=True,timeout=40,env=self.env)
        if run.returncode:raise RuntimeError('消息发送失败')
        data=json.loads(run.stdout)
        if data.get('ok') is False or data.get('error'):raise RuntimeError('消息发送失败')
        result=data.get('data',data); ref=result.get('message_id')
        if not ref:raise RuntimeError('缺少消息发送回执')
        return ref


def listen_events(settings,event_type,stop,incoming):
    data=Path(settings['data_dir'])
    health=data/('click-health.json' if event_type=='card.action.trigger' else 'health.json')
    def mark(status):
        atomic_json(health,{'at':now(),'pid':os.getpid(),'listener':status,'version':'FS-v1.0'})
    def read_output(child):
        for line in child.stdout:
            if len(line)>250000:continue
            try:event=json.loads(line)
            except ValueError:continue
            if not isinstance(event,dict):continue
            while not stop.is_set():
                try:incoming.put((event_type,event),timeout=.5);break
                except queue.Full:continue
    def read_status(child,ready):
        for line in child.stderr:
            if '[event] ready event_key='+event_type in line:ready.set()
    while not stop.is_set():
        ready=threading.Event()
        try:
            with subprocess.Popen([settings['lark'],'event','consume',event_type,'--as','bot'],
                                  stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                  text=True,env=Lark(settings).env) as child:
                readers=[threading.Thread(target=read_output,args=(child,),daemon=True),
                         threading.Thread(target=read_status,args=(child,ready),daemon=True)]
                for reader in readers:reader.start()
                started=time.monotonic()
                try:
                    while child.poll() is None and not stop.is_set():
                        if ready.is_set():mark('ready')
                        elif time.monotonic()-started>45:break
                        stop.wait(1)
                finally:
                    if child.poll() is None:child.terminate()
                    try:child.wait(timeout=8)
                    except subprocess.TimeoutExpired:child.kill();child.wait()
                    for reader in readers:reader.join(timeout=2)
        except OSError:pass
        mark('stopped' if stop.is_set() else 'reconnecting')
        stop.wait(5)


def serve(settings):
    os.umask(0o077)
    data=Path(settings['data_dir']);data.mkdir(mode=0o700,parents=True,exist_ok=True)
    lock=open(data/'service.lock','a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    bridge=Bridge(settings);transport=Lark(settings)
    stop=threading.Event();incoming=queue.Queue(maxsize=200)
    signal.signal(signal.SIGTERM,lambda *_:stop.set())
    signal.signal(signal.SIGINT,lambda *_:stop.set())
    events=['im.message.receive_v1']
    if bridge.board and not settings.get('shared_clicks'):events.append('card.action.trigger')
    listeners=[threading.Thread(target=listen_events,args=(settings,key,stop,incoming),daemon=True) for key in events]
    for listener in listeners:listener.start()
    last_monitor=0
    while not stop.is_set():
        # 收件和投递共用一个写入者，发送中的点击在回执提交后核验。
        for _ in range(40):
            try:key,event=incoming.get_nowait()
            except queue.Empty:break
            if key=='card.action.trigger':bridge.store.remember_click(event,settings['owner'])
            else:bridge.store.remember(event,settings['owner'])
        bridge.store.drain_clicks(settings['owner'])
        bridge.process_pending();bridge.maintain_controls();bridge.flush(transport)
        if time.monotonic()-last_monitor>30:
            bridge.monitor();last_monitor=time.monotonic()
        stop.wait(.5)
    for listener in listeners:listener.join(timeout=10)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('mode',choices=['serve','control','status'])
    args=parser.parse_args();settings=read_json(args.config)
    if args.mode=='serve':serve(settings)
    elif args.mode=='status' and settings.get('workbench_only'):
        print('仅工作台模式；从本人飞书直接回复任务卡查询状态。')
    elif args.mode=='status':print(Bridge(settings).status())
    elif settings.get('workbench_only'):raise SystemExit('仅工作台模式不承接历史项目控制')
    else:
        b=Bridge(settings);text,receipt=b.control_result(read_json(os.environ['AGENT_AUTOLOOP_CONTEXT']))
        print(text);print('```agent-autoloop\n'+json.dumps(receipt,ensure_ascii=False)+'\n```')


if __name__=='__main__':main()
