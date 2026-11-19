#!/usr/bin/env python3
"""作为 Autoloop 飞书通道的 lark 可执行入口，共用原有的唯一事件订阅。"""
import json
import os
from pathlib import Path
import queue
import sqlite3
import signal
import subprocess
import sys
import threading
import time

from bridge import Store, atomic_json, now
from click_cards import decode_click


def relay(settings,program,args):
    data=Path(settings['data_dir'])
    owner=settings['owner']
    clicks=queue.Queue()
    stop=threading.Event()
    child=subprocess.Popen([program,*args],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    def terminate(*_):
        if child.poll() is None:child.terminate()
    signal.signal(signal.SIGTERM,terminate)
    signal.signal(signal.SIGINT,terminate)
    def health(state):
        atomic_json(data/'click-health.json',{'at':now(),'pid':os.getpid(),'consumer_pid':child.pid,'listener':state,'shared':True})
    def diagnostics():
        for line in child.stderr:
            sys.stderr.buffer.write(line);sys.stderr.buffer.flush()
            if b'[event] ready event_key=card.action.trigger' in line:health('ready')
    def persist():
        store=Store(data/'inbox.sqlite3')
        try:
            store.bind_owner(owner)
            while not stop.is_set() or not clicks.empty():
                try:event=clicks.get(timeout=.3)
                except queue.Empty:continue
                for _ in range(180):
                    try:
                        if store.stage_click(event,owner)!='await_delivery':break
                    except (OSError, sqlite3.OperationalError):pass
                    if stop.wait(.25):break
        finally:store.db.close()
    reader=threading.Thread(target=diagnostics,daemon=True)
    writer=threading.Thread(target=persist,daemon=True)
    reader.start();writer.start()
    try:
        for line in child.stdout:
            # 原通道收到逐字节相同的完整流，工作台只取自己的回调。
            sys.stdout.buffer.write(line);sys.stdout.buffer.flush()
            if len(line)>250000:continue
            try:event=json.loads(line)
            except ValueError:continue
            if (isinstance(event,dict) and event.get('operator_id')==owner and event.get('host')=='im_message'
                    and decode_click(event)):
                clicks.put({k:event.get(k) for k in ('type','operator_id','chat_id','event_id','message_id',
                           'action_tag','action_name','action_value','form_value','host')})
    finally:
        terminate()
        try:child.wait(timeout=8)
        except subprocess.TimeoutExpired:child.kill();child.wait()
        stop.set();reader.join(timeout=2);writer.join(timeout=6)
        health('offline')
    return child.returncode


def main():
    settings=json.loads(Path(__file__).with_name('settings.json').read_text())
    program=settings['lark']
    if not Path(program).is_absolute() or Path(program).resolve()==Path(__file__).resolve():
        raise SystemExit('需要原有 lark CLI 的绝对路径')
    args=sys.argv[1:]
    if args!=['event','consume','card.action.trigger','--as','bot']:
        os.execv(program,[program,*args])
    return relay(settings,program,args)


if __name__=='__main__':sys.exit(main())
