"""本机工作台。历史项目只读，新任务通过 Autoloop 专用规则执行。"""
import argparse
import json
import os
import sqlite3
import hmac
import re
import secrets
import time
import threading
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit
from workbench import Workbench, Problem, Store, Hub

ROOT = Path(__file__).resolve().parent
LEGACY_BOARD = (ROOT/'legacy_snapshot.py').is_file()
if LEGACY_BOARD:
    from legacy_snapshot import snapshot


class Handler(BaseHTTPRequestHandler):
    def local(self):
        return self.headers.get('Host') in {
            f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'
        }

    def session(self):
        try:
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            token = cookie.get('odyssey_session_'+str(self.server.server_port))
            return bool(token and hmac.compare_digest(token.value, self.server.session_token))
        except (CookieError, TypeError):
            return False

    def json_response(self, payload, status=200, headers=()):
        self.respond(json.dumps(payload, ensure_ascii=False).encode(), 'application/json; charset=utf-8', status, headers)

    def do_POST(self):
        if (not self.local() or not self.session()
                or self.headers.get('Origin') != 'http://' + self.headers['Host']
                or not hmac.compare_digest(self.headers.get('X-Odyssey-CSRF', ''), self.server.csrf)):
            return self.json_response({'error':'请从本机工作台重新打开后操作'}, 403)
        try:
            size = int(self.headers.get('Content-Length', 0))
            if not 0 < size <= 48000 or self.headers.get_content_type() != 'application/json':
                raise Problem('请求格式无效')
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise Problem('请求格式无效')
            path = urlsplit(self.path).path
            workbench = self.server.workbench
            if path.startswith('/api/outer/'):
                from outer_channels import dispatch
                cmd = path.removeprefix('/api/outer/')
                if cmd not in ('submit','select','get','calls','call','event','approve','pause','resume','stop'):
                    raise Problem('接口不存在',404)
                if not workbench.outer_enabled:
                    raise Problem('当前实例未启用智能接单',404)
                return self.json_response(dispatch(workbench.outer(),'web',cmd,body))
            if path == '/api/tasks':
                if getattr(workbench,'outer_enabled',False):
                    raise Problem('请在智能接单入口登记需求',409)
                with self.server.task_actions:
                    result = workbench.create(body)
                return self.json_response(result, 201)
            match = re.fullmatch(r'/api/tasks/(wb-[a-f0-9]{32})/(answer|pause|resume|revise|reviewer|handoff|note|move-note)', path)
            if not match:
                raise Problem('接口不存在', 404)
            task_id, action = match.groups()
            if workbench.store.get(task_id).get('goal_execution',{}).get('outer'):
                raise Problem('请使用绑定目标与版本的智能工单入口',409)
            handler = {'note':workbench.store.board_note,'move-note':workbench.store.move_note,'handoff':workbench.store.handoff,'answer':workbench.answer, 'resume':workbench.resume, 'revise':workbench.revise, 'reviewer':workbench.reviewer, 'pause':lambda t, _:workbench.pause(t)}[action]
            with self.server.task_actions:
                result = handler(task_id, body)
            self.json_response(result)
        except Problem as error:
            self.json_response({'error':str(error)}, error.status)
        except (ValueError, TypeError):
            self.json_response({'error':'请求格式无效'}, 400)
        except (OSError, sqlite3.Error, KeyError):
            self.json_response({'error':'任务服务暂不可用，请刷新核对'}, 503)

    def do_GET(self):
        if not self.local():
            self.send_error(403)
            return
        route = urlsplit(self.path).path
        if route == '/api/session':
            return self.json_response({'csrf':self.server.csrf}, headers=[
                ('Set-Cookie', 'odyssey_session_'+str(self.server.server_port)+'='+self.server.session_token+'; HttpOnly; SameSite=Strict; Path=/api/')
            ])
        if route == '/api/outer':
            if not self.session():
                return self.json_response({'error':'会话已过期，请刷新'},403)
            wb=self.server.workbench
            if not getattr(wb,'outer_enabled',False):
                return self.json_response({'enabled':False})
            from outer_channels import dispatch
            try:
                return self.json_response({'enabled':True,'drafts':dispatch(wb.outer(),'web','list',{})})
            except (Problem,sqlite3.Error):
                return self.json_response({'error':'暂时无法读取目标草案'},503)
        if route.startswith('/api/tasks'):
            if not self.session():
                return self.json_response({'error':'会话已过期，请刷新'}, 403)
            try:
                if route in ('/api/tasks','/api/tasks/stream'):
                    focus = parse_qs(urlsplit(self.path).query).get('task',[None])[0]
                    if focus:self.server.workbench.store.get(focus)
                    if route.endswith('/stream'):return self.stream_tasks(focus)
                    return self.json_response(self.server.workbench.snapshot(focus))
                match = re.fullmatch(r'/api/tasks/(wb-[a-f0-9]{32})/(artifact|preview)', route)
                if match:
                    relative = parse_qs(urlsplit(self.path).query).get('path', [''])[0]
                    raw_version=parse_qs(urlsplit(self.path).query).get('version',[None])[0]
                    version=int(raw_version) if raw_version is not None else None
                    if match[2]=='preview':
                        return self.json_response(self.server.workbench.preview(match[1],relative,version))
                    path,content = self.server.workbench.artifact_bytes(match[1], relative,version)
                    return self.respond(content, 'application/octet-stream', headers=[
                        ('Content-Disposition', "attachment; filename*=UTF-8''"+quote(path.name, safe=''))
                    ])
            except Problem as error:
                return self.json_response({'error':str(error)}, error.status)
            except (OSError, sqlite3.Error, ValueError, KeyError):
                return self.json_response({'error':'任务服务暂不可用'}, 503)
        if route == '/api/state':
            if not LEGACY_BOARD:
                return self.json_response({'error':'发行版不提供历史项目快照'},404)
            try:
                body = json.dumps(snapshot(), ensure_ascii=False).encode()
                self.respond(body, 'application/json; charset=utf-8')
            except (OSError, ValueError, sqlite3.Error, KeyError):
                self.respond(b'{"error":"Local records unavailable"}', 'application/json', 503)
            return
        files = {'/': 'index.html', '/index.html': 'index.html', '/style.css': 'style.css', '/app.js': 'app.js', '/assets/research-kingdom.png': 'assets/research-kingdom.png'}
        files['/outer.js']='outer.js'
        files['/intake_dialog.js']='intake_dialog.js'
        if LEGACY_BOARD:files['/snapshot.json']='snapshot.json'
        for name in ('odyssey.css', 'kingdoms.js', 'journey.js', 'task_presentation.js', 'tasks.js', 'tasks.css', 'world.js', 'world.css', 'globe.js', 'assets/odyssey-world-map.png', 'vendor/three.module.js', 'vendor/three.core.js', 'vendor/OrbitControls.js'):
            files['/' + name] = name
        if re.fullmatch(r'/assets/odyssey/[a-z0-9-]+\.(png|jpg)', route):
            files[route] = route.lstrip('/')
        if route not in files:
            self.send_error(404)
            return
        file = ROOT / files[route]
        if not file.is_file():
            self.send_error(404)
            return
        types = {'.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.json': 'application/json; charset=utf-8', '.png': 'image/png', '.jpg': 'image/jpeg'}
        self.respond(file.read_bytes(), types[file.suffix])

    def stream_tasks(self,focus=None):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.connection.settimeout(10)
        try:
            for _ in range(150):
                snapshot = self.server.workbench.snapshot(focus)
                self.wfile.write(('data: '+json.dumps(snapshot, ensure_ascii=False)+'\n\n').encode())
                self.wfile.flush()
                time.sleep(2)
        except (OSError, sqlite3.Error):
            pass

    def respond(self, body, content_type, status=200, headers=()):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--snapshot', action='store_true')
    parser.add_argument('--feishu-socket')
    parser.add_argument('--feishu-decision-key',type=Path)
    parser.add_argument('--runtime',type=Path)
    parser.add_argument('--hub-home',type=Path)
    parser.add_argument('--goal-pilot',action='store_true')
    parser.add_argument('--goal-contract',type=Path, help='目标试点默认验收契约 JSON')
    parser.add_argument('--goal-team',action='store_true', help='启用隔离多工作包路线')
    parser.add_argument('--outer-intelligence',action='store_true',help='在隔离目标试点启用版本化智能接单')
    args = parser.parse_args()
    if args.outer_intelligence and not (args.goal_pilot and args.runtime and args.hub_home):
        parser.error('智能接单需要显式指定目标试点、runtime 和 hub-home')
    if args.outer_intelligence and (args.goal_contract or args.goal_team):
        parser.error('智能接单采用每张工单已批准的验收契约')
    if args.outer_intelligence and (args.runtime.resolve()==(ROOT/'.runtime').resolve() or
                                    args.hub_home.resolve()==(Path.home()/'.agent-autoloop').resolve()):
        parser.error('智能接单试点需要独立工作台和中台目录')
    if args.snapshot:
        if not LEGACY_BOARD:parser.error('发行版不支持生成历史项目快照')
        (ROOT / 'snapshot.json').write_text(json.dumps(snapshot(), ensure_ascii=False, indent=2))
        print('已生成脱敏展示快照')
    else:
        server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
        if args.goal_pilot and (not args.runtime or not args.hub_home):
            parser.error('目标试点须显式指定工作台目录和中台目录')
        goal_contract = None
        if args.goal_team and not (args.goal_pilot and args.goal_contract):
            parser.error('多工作包试点需要目标验收契约')
        if args.goal_contract:
            if not args.goal_pilot:
                parser.error('验收契约须用于目标试点')
            from superagent.goal_checks import normalize
            try:
                goal_contract = normalize(json.loads(args.goal_contract.read_text()))
            except (OSError, ValueError) as error:
                parser.error(f'验收契约无效：{error}')
        server.workbench = Workbench(Store(args.runtime) if args.runtime else None,
                                    Hub(args.hub_home) if args.hub_home else None,
                                    goal_enabled=args.goal_pilot, goal_contract=goal_contract,goal_team=args.goal_team,
                                    outer_enabled=args.outer_intelligence)
        server.task_actions = threading.Lock()
        if args.feishu_socket:
            from private_intake import serve_private
            decision_key=None
            if args.feishu_decision_key:
                keypath=args.feishu_decision_key
                if keypath.is_symlink() or keypath.stat().st_mode & 0o077 or keypath.stat().st_uid!=os.getuid():
                    parser.error('手机决定密钥文件必须为本人私有')
                decision_key=keypath.read_bytes()
                if len(decision_key)!=32:parser.error('手机决定密钥应为 32 字节')
            server.feishu_intake=serve_private(args.feishu_socket,server.workbench.store,server.workbench,server.task_actions,decision_key)
        server.session_token = secrets.token_urlsafe(32)
        server.csrf = secrets.token_urlsafe(32)
        if args.outer_intelligence:
            outer = server.workbench.outer()
            def outer_loop():
                while True:
                    try:
                        outer.drain(limit=1)
                    except Exception as error:
                        print('智能判断待核对',type(error).__name__,flush=True)
                    time.sleep(1)
            threading.Thread(target=outer_loop,daemon=True).start()
        def reconcile_loop():
            while True:
                try:
                    with server.task_actions:server.workbench.reconcile()
                except Exception:
                    print('修改请求协调暂不可用；持久请求保留',flush=True)
                time.sleep(2)
        threading.Thread(target=reconcile_loop,daemon=True).start()
        print(f'Local: http://127.0.0.1:{args.port}', flush=True)
        server.serve_forever()
