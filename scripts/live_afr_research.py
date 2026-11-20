"""Start a real, ticket-bound Autoloop Hub and research Worker against a synthetic vault."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs, quote, urlencode

from live_afr_command import http, put, stop, wait_status, TICKET
from afr_command_agent import SOURCE
from threading import Lock

AGENT = 'afr-research'
EVENT = 'afr.research.requested'
HERE = Path(__file__).resolve().parent
BOARD = HERE.parent / '网页工作台'
sys.path.insert(0, str(BOARD))
from server import Handler
from workbench import Problem

TURN = re.compile(r'[a-f0-9]{32}')
FILES = ('研究白板.canvas', '原文出处.md', '研究记录.json')


class Trial:
    def __init__(self, root, ticket, url, key):
        self.root, self.ticket, self.url, self.key = root, ticket, url, key
        self.prefix = '/research/' + ticket
        self.lock = Lock()
        self.out = root / 'synthetic-vault' / '研究交付' / ticket

    def state(self, rid):
        receipt = self.root / 'dispatch' / (rid + '.json')
        if not receipt.exists():
            return {'state':'unconfirmed'}
        event = json.loads(receipt.read_text())['event']
        rows = http(self.url, '/v1/events/'+str(event), self.key)['runs']
        if not rows:
            return {'event':event,'state':'queued'}
        row = rows[-1]
        result = {'event':event,'run':row['run'],'state':row['state']}
        if row['state'] == 'done':
            try:
                result['status'] = json.loads(row['summary'])['status']
            except (ValueError, KeyError, TypeError):
                result['status'] = 'invalid_receipt'
        return result

    def inspect(self):
        turns = []
        for item in sorted((self.root/'requests').glob('*.json'),key=lambda p:p.stat().st_mtime_ns):
            q = json.loads(item.read_text())
            if q['ticket'] != self.ticket:
                continue
            rid = item.stem
            state = self.state(rid)
            files = [f for f in FILES if (self.out/rid/f).is_file()] if state['state']=='done' else []
            preview = getattr(self, 'preview_vault', None)
            dest = preview/'研究交付'/self.ticket/rid/FILES[0] if preview else None
            uri = ('obsidian://open?' + urlencode({'path':str(dest)})) if dest and dest.is_file() else None
            turns.append(dict(request_id=rid,query=q['query'],parent=q['parent'],files=files,obsidian=uri,can_export=bool(preview),**state))
        return turns

    def export_canvas(self, rid):
        if not self.preview_vault or not isinstance(rid,str) or not TURN.fullmatch(rid):
            raise Problem('当前实例未配置试点白板库。',404)
        state = self.state(rid)
        if state.get('state')!='done' or state.get('status')!='ok':
            raise Problem('本轮尚未交付白板。',409)
        parent = self.preview_vault/'研究交付'/self.ticket
        if parent.resolve()!=parent: raise Problem('白板输出路径无效。',409)
        parent.mkdir(parents=True,exist_ok=True)
        target = parent/rid
        with self.lock, tempfile.TemporaryDirectory(dir=parent,prefix='.export-') as staging:
            bundle = Path(staging)/'turn'
            bundle.mkdir()
            for name in (FILES[0],FILES[1]):
                src = self.out/rid/name
                if src.resolve()!=src or not src.is_file(): raise Problem('研究交付缺少原件。',409)
                data = src.read_bytes()
                if target.exists():
                    copy = target/name
                    if copy.resolve()!=copy or not copy.is_file() or copy.read_bytes()!=data:
                        raise Problem('已有白板被修改，保留该版本。',409)
                else:
                    (bundle/name).write_bytes(data)
            if not target.exists(): bundle.rename(target)
        return {'obsidian':'obsidian://open?'+urlencode({'path':str(target/FILES[0])})}

    def publish(self, data):
        rid, q, parent = (data.get(k) for k in ('request_id','query','parent'))
        if (not isinstance(rid,str) or not TURN.fullmatch(rid) or not isinstance(q,str)
                or not 1 <= len(q.strip()) <= 1000 or data.get('consent') is not True):
            raise Problem('请输入研究任务，并确认本轮使用合成资料。')
        if parent is not None and (not isinstance(parent,str) or not TURN.fullmatch(parent)):
            raise Problem('追问必须绑定本工单内已完成的研究。')
        doc = {'ticket':self.ticket,'query':q.strip(),'parent':parent,'consent':'synthetic_query_to_codex'}
        path = self.root/'requests'/(rid+'.json')
        with self.lock:
            if path.exists() and json.loads(path.read_text()) != doc:
                raise Problem('相同请求号已绑定其他输入。',409)
            previous = self.inspect()
            for row in previous:
                if row['request_id']==rid and row['state']!='unconfirmed':
                    return row
            if parent and not any(x['request_id']==parent and '研究白板.canvas' in x['files'] for x in previous):
                raise Problem('请等待原轮交付后再追问。',409)
            if any(x['request_id']!=rid and x['state'] not in ('done','failed','cancelled','nothing','unconfirmed') for x in previous):
                raise Problem('另一轮正在执行，或在等待确认，请先查看该轮。',409)
            if not path.exists():
                with path.open('x') as stream:
                    json.dump(doc,stream,ensure_ascii=False)
            created = http(self.url,'/v1/events',self.key,{'id':rid,'type':EVENT,'subject':rid})
            if not created.get('duplicate') and created.get('routed') != [AGENT]:
                raise Problem('中台没有将本轮派给研究执行器。',503)
            tmp = self.root/'dispatch'/(rid+'.tmp')
            put(tmp,{'event':created['event']})
            tmp.replace(tmp.with_suffix('.json'))
            return self.state(rid)


class ResearchHandler(Handler):
    def do_GET(self):
        if not self.local(): return self.send_error(403)
        trial = self.server.trial
        path = urlsplit(self.path)
        if path.path == '/api/session': return super().do_GET()
        assets = {trial.prefix:('afr_research.html','text/html'),'/afr_research.js':('afr_research.js','text/javascript'),
                  '/style.css':('style.css','text/css'),'/afr_research.css':('afr_research.css','text/css')}
        if path.path in assets:
            name, mime = assets[path.path]
            return self.respond((BOARD/name).read_bytes(),mime+'; charset=utf-8')
        if not self.session(): return self.send_error(403)
        try:
            if path.path == '/api'+trial.prefix:
                return self.json_response({'ticket':trial.ticket,'turns':trial.inspect()})
            if path.path.startswith('/api'+trial.prefix+'/'):
                rid = path.path.split('/')[-1]
                q = parse_qs(path.query)
                name = q.get('file',[''])[0]
                if not TURN.fullmatch(rid) or name not in FILES: return self.send_error(404)
                file = trial.out/rid/name
                if file.resolve() != file or not file.is_file(): return self.send_error(404)
                if (trial.state(rid).get('state') != 'done'): return self.send_error(409)
                if name == '研究记录.json' and q.get('view')==['inline']:
                    return self.json_response(json.loads(file.read_text()))
                if name == '研究白板.canvas' and q.get('view')==['inline']:
                    return self.json_response(json.loads(file.read_text()))
                return self.respond(file.read_bytes(),'application/octet-stream',headers=[
                    ('Content-Disposition',"attachment; filename*=UTF-8''"+quote(name))])
        except (OSError,ValueError,KeyError): return self.json_response({'error':'研究账本暂时不可用。'},503)
        self.send_error(404)

    def do_POST(self):
        trial = self.server.trial
        if self.path not in ('/api'+trial.prefix, '/api'+trial.prefix+'/canvas'): return self.send_error(404)
        if (not self.local() or not self.session() or self.headers.get('Origin')!='http://'+self.headers['Host']
                or self.headers.get('X-Odyssey-CSRF')!=self.server.csrf): return self.send_error(403)
        try:
            size = int(self.headers.get('Content-Length',0))
            if not 0 < size < 12000 or self.headers.get_content_type()!='application/json': raise ValueError()
            data = json.loads(self.rfile.read(size))
            if not isinstance(data,dict): raise ValueError()
            if self.path.endswith('/canvas'): return self.json_response(trial.export_canvas(data.get('request_id')))
            return self.json_response(trial.publish(data),201)
        except Problem as exc: return self.json_response({'error':str(exc)},exc.status)
        except (ValueError,TypeError): return self.json_response({'error':'输入无效。'},400)
        except (KeyError,OSError): return self.json_response({'error':'派发结果待查，请使用原请求号重试。'},503)

    def log_request(self, code='-', size='-'):
        self.log_message('HTTP %s',code)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('stage','afr-root','afr-python','node','autoloop-entry','codex'):
        parser.add_argument('--'+name,required=True,type=Path)
    parser.add_argument('--ticket',required=True)
    parser.add_argument('--obsidian-preview-vault',type=Path,help='用于交付合成白板的既有试点库')
    parser.add_argument('--port',type=int,default=8767)
    args = parser.parse_args()
    os.umask(0o077)
    root = args.stage.resolve()
    if (args.stage.is_symlink() or root.parent!=Path('/private/tmp') or not root.is_dir()
            or root.stat().st_uid!=os.getuid() or root.stat().st_mode & 0o077 or not TICKET.fullmatch(args.ticket)):
        parser.error('需要本人私有试点目录与有效工单号')
    with ExitStack() as services:
        server = services.enter_context(ThreadingHTTPServer(('127.0.0.1',args.port),ResearchHandler))
        manifest = root/'trial.json'
        if not manifest.exists() and any(root.iterdir()): parser.error('首次启动需要空目录')
        cfg = dict(ticket=args.ticket,root=str(args.afr_root),python=str(args.afr_python),codex=str(args.codex))
        if manifest.exists() and json.loads(manifest.read_text())!=cfg: parser.error('恢复须沿用原工单和工具')
        cli = [str(args.node),'--disable-warning=ExperimentalWarning',str(args.autoloop_entry)]
        def env(home):
            return {'HOME':str(Path.home()),'PATH':os.defpath,'TMPDIR':str(root),'AGENT_AUTOLOOP_HOME':str(home)}
        def run(home,*cmd):
            return subprocess.run([*cli,*cmd],env=env(home),check=True,capture_output=True,text=True,timeout=15).stdout.strip()
        hub,worker,vault=(root/k for k in ('hub','worker','synthetic-vault'))
        if not manifest.exists():
            for name in ('hub','worker','requests','dispatch','cwd'): (root/name).mkdir()
            adapter=HERE/'afr_research_agent.py'
            subprocess.run([str(args.afr_python),'-B',str(adapter),'--init-synthetic',str(vault)],check=True)
            command=[str(args.afr_python),'-B',str(adapter),'--afr-root',str(args.afr_root),'--afr-python',str(args.afr_python),
                     '--vault',str(vault),'--requests',str(root/'requests'),'--output',str(vault/'研究交付'),'--synthetic-codex',str(args.codex),'--allow-synthetic-query-consent']
            put(hub/'rules.json',{'sources':{SOURCE:{'name':'AFR'}},'channels':{},
                'agents':{AGENT:{'name':'AFR 研究执行','worker':'local','use':'command','command':command,'timeout':12}},
                'rules':[{'name':AGENT,'when':{'source':SOURCE,'types':[EVENT]},'to':AGENT,'cwd':str(root/'cwd'),
                          'parallel':1,'tell':'never','prompt':'Process the ticket-bound private research request.'}]})
            run(hub,'check')
            owner=run(hub,'key','new','owner','owner')
            source=run(hub,'key','new','source',SOURCE)
            wk=run(hub,'key','new','worker','local')
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0)); address='127.0.0.1:'+str(sock.getsockname()[1])
            url='http://'+address
            put(hub/'config.json',{'url':url,'hub':{'listen':address},'owner':owner,'source':{'name':SOURCE,'key':source}})
            put(worker/'config.json',{'url':url,'worker':{'name':'local','key':wk,'capacity':1}})
            put(manifest,cfg)
        c=json.loads((hub/'config.json').read_text());url=c['url']
        for home in (hub,worker):
            log=services.enter_context((home/'up.log').open('a'))
            proc=subprocess.Popen([*cli,'up'],env=env(home),stdout=log,stderr=subprocess.STDOUT)
            services.callback(stop,proc)
            def ready():
                state=http(url,'/v1/state',c['owner'])
                return home==hub or any(a['id']==AGENT and a['online'] for a in state['agents'])
            wait_status(ready,proc)
        server.trial=Trial(root,args.ticket,url,c['source']['key'])
        server.trial.preview_vault = args.obsidian_preview_vault
        if args.obsidian_preview_vault and (args.obsidian_preview_vault.resolve()!=args.obsidian_preview_vault or not (args.obsidian_preview_vault/'.obsidian').is_dir()):
            parser.error('需要配置现有试点库的绝对路径')
        server.csrf=secrets.token_urlsafe(32);server.session_token=secrets.token_urlsafe(32)
        signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
        print(f'READY http://127.0.0.1:{args.port}'+server.trial.prefix,flush=True)
        server.serve_forever()

if __name__=='__main__':main()
