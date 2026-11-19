"""仅供本机本人飞书桥使用，不挂到网页路由。"""
import json
import hashlib
import hmac
import os
from pathlib import Path
import re
import socket
import socketserver
import stat
import threading


def dispatch(store, item, workbench=None):
    action = item['action']
    if workbench and action in ('brief','proposal','answer','controls','control','milestone'):
        from mobile_decision import answer,proposal,selected,control_card,control
        from task_brief import milestone,task_brief
        if action=='answer':return answer(workbench,item['task'],item['binding'],item['choice'])
        if action=='control':return control(workbench,item['task'],item['binding'],item['event'])
        task=selected(workbench,item['task'])
        if action=='milestone':return milestone(task)
        if action=='controls':return control_card(task)
        return proposal(task) if action=='proposal' else task_brief(task).as_private_reply()
    if action == 'intake':
        goal = item['goal']
        if workbench and workbench.goal_enabled:
            result = workbench.create({'name':goal[:80],'goal':goal,'request_id':item['request'],
                                       'workflow':'goal'},source='feishu')
            return [{'id':result['task']['id'],'workflow':'goal'},result['created']]
        task, created = store.create({'name':goal[:80], 'goal':goal,'request_id':item['request'],
                                     'role_models':dict.fromkeys(('plan','execute','review'),'gpt-6-astra')},source='feishu')
        return [{'id':task['id']},created]
    if action == 'pending':
        return store.pending_handoffs()
    if action == 'note' and re.fullmatch(r'om_[A-Za-z0-9_-]{1,180}',item['message']) and 0<len(item['body'])<=8000:
        return store.add_note(item['task'],item['message'],item['body'])
    if action == 'status':
        task=store.get(item['task'])
        return {key:task[key] for key in ('version','status')}
    if action == 'delivery' and item['state'] in ('unknown','sent'):
        return store.mark_handoff(item['request'],item['state'])
    raise ValueError('未知操作')


def serve_private(path,store,workbench=None,action_lock=None,decision_key=None):
    path=Path(path)
    parent=path.parent.stat()
    if not path.is_absolute() or path.parent.resolve()!=path.parent or parent.st_uid!=os.getuid() or parent.st_mode & 0o077:
        raise ValueError('桥接 socket 必须位于本人私有目录')
    if path.exists():
        if not stat.S_ISSOCK(path.lstat().st_mode):raise ValueError('不能覆盖非 socket 文件')
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as probe:
            try:probe.connect(str(path))
            except ConnectionRefusedError:path.unlink()
            else:raise ValueError('工作台桥接已在运行')

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(5)
            try:
                raw=self.rfile.readline(96000)
                if not raw.endswith(b'\n'):raise ValueError('请求超限')
                item=json.loads(raw)
                if item.get('action') in ('answer','proposal','controls','control') and decision_key is None:
                    result={'result':{'message':'手机控制尚未启用。'} if item['action'] in ('proposal','controls') else '手机控制尚未启用，未执行。'}
                elif workbench and item.get('action') in ('answer','control'):
                    signature=item.pop('signature','')
                    signed=json.dumps(item,sort_keys=True,separators=(',',':')).encode()
                    if (not hmac.compare_digest(hmac.new(decision_key,signed,hashlib.sha256).hexdigest(),signature)
                            or not all(re.fullmatch(r'om_[A-Za-z0-9_-]{1,180}',item.get(field,'')) for field in ('parent','event'))):
                        raise ValueError('无可信的本人私聊回执')
                    stamp=hashlib.sha256(signed).hexdigest()
                    with action_lock:
                        with store.connect() as db:
                            previous=db.execute('SELECT stamp,result FROM mobile_replies WHERE event=?',(item['event'],)).fetchone()
                        if previous:
                            if previous['stamp']!=stamp:raise ValueError('决定事件冲突')
                            result={'result':previous['result']}
                        else:
                            with store.connect() as db:
                                db.execute('INSERT INTO mobile_replies VALUES(?,?,?)',(item['event'],stamp,'操作回执待核对，请查询实际状态，不会自动重送控制。'))
                            result={'result':dispatch(store,item,workbench)}
                            with store.connect() as db:
                                db.execute('UPDATE mobile_replies SET result=? WHERE event=?',(result['result'],item['event']))
                else:
                    result={'result':dispatch(store,item,workbench)}
            except Exception:
                result={'error':'请求未确认'}
            self.wfile.write(json.dumps(result).encode()+b'\n')

    server=socketserver.UnixStreamServer(str(path),Handler)
    path.chmod(0o600)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    return server
