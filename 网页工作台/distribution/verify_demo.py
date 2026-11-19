"""以全新 HOME/工作区验证同一工单在无中台、服务重启时的行为。"""
import contextlib
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, HTTPCookieProcessor, ProxyHandler, Request
import uuid


@contextlib.contextmanager
def running(root,env,port):
    origin='http://127.0.0.1:'+str(port)
    client=build_opener(ProxyHandler({}),HTTPCookieProcessor(http.cookiejar.CookieJar()))
    with subprocess.Popen([sys.executable,'-B','server.py','--port',str(port)],cwd=root,env=env,
                          stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True) as child:
        try:
            for _ in range(80):
                if child.poll() is not None:raise RuntimeError('隔离看板未启动：'+child.stderr.read()[-300:])
                try:
                    with client.open(origin+'/api/session',timeout=1) as response:csrf=json.load(response)['csrf']
                except URLError:
                    time.sleep(.05)
                    continue
                break
            else:raise RuntimeError('隔离看板连接超时')
            yield origin,client,csrf
        finally:
            if child.poll() is None:child.terminate()
            try:child.wait(timeout=5)
            except subprocess.TimeoutExpired:child.kill()


def verify(source):
    with tempfile.TemporaryDirectory() as temporary:
        root=Path(temporary).resolve()
        home=root/'home'
        home.mkdir(mode=0o700)
        app=root/'app'
        app.mkdir()
        for line in (source/'SHA256SUMS').read_text().splitlines():
            checksum,name=line.split('  ',1)
            if Path(name).is_absolute() or '..' in Path(name).parts:raise ValueError('无效的发行清单')
            original=source/name
            if original.resolve()!=original or hashlib.sha256(original.read_bytes()).hexdigest()!=checksum:
                raise ValueError('文件校验不一致：'+name)
            target=app/name
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(original,target)
        env={'HOME':str(home),'PATH':os.defpath,'LANG':'en_US.UTF-8','PYTHONDONTWRITEBYTECODE':'1'}
        forbidden=subprocess.run([sys.executable,'-B','server.py','--snapshot'],cwd=app,env=env,capture_output=True)
        assert forbidden.returncode!=0 and not (app/'snapshot.json').exists()
        (app/'snapshot.json').write_text('test-only sentinel')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0))
            port=probe.getsockname()[1]
        body={'name':'发行验收','goal':'本机样例，未授权运行模型','request_id':str(uuid.uuid4()),
              'role_models':dict.fromkeys(('plan','execute','review'),'gpt-6-astra')}
        task_id=None
        for attempt in range(2):
            with running(app,env,port) as (origin,client,csrf):
                with client.open(origin+'/') as response:assert '任务工作台' in response.read().decode()
                for route in ('/api/state','/snapshot.json'):
                    try:client.open(origin+route)
                    except HTTPError as error:assert error.code==404
                    else:raise AssertionError('暴露历史项目接口')
                request=Request(origin+'/api/tasks',data=json.dumps(body).encode(),headers={
                    'Content-Type':'application/json','Origin':origin,'X-Odyssey-CSRF':csrf})
                with client.open(request) as response:result=json.load(response)
                assert result['created']==(attempt==0) and result['dispatch_error']
                if task_id is None:task_id=result['task']['id']
                assert result['task']['id']==task_id
                with client.open(origin+'/api/tasks?task='+task_id) as response:state=json.load(response)
                assert len(state['tasks'])==1 and not state['capability']['ready']
                task=state['tasks'][0]
                assert task['id']==task_id and task['calls']==0 and task['status']=='created' and task['observation_stale']
        print('通过：干净 HOME、回环网页、未接中台拒绝派发、服务重启保留同一工单、重复请求未建第二单。模型调用：0。')


if __name__=='__main__':
    if not __debug__:raise SystemExit('验证器不能以 -O 模式运行')
    verify(Path(__file__).resolve().parent)
