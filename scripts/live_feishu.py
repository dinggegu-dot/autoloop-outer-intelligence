"""在已有本人私聊通道中投影一条带随机前缀的隔离试验，不启动第二个监听。"""
import argparse
import re
from datetime import datetime
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '飞书工作入口'))
from bridge import Bridge, Lark, MobileProblem
from outer_bridge import submit

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('stage', type=Path, help='本次试验本人私有目录')
parser.add_argument('settings', type=Path, help='现役本人私聊桥的设置文件')
args = parser.parse_args()
os.umask(0o077)
stage = args.stage.resolve(strict=True)
if (stage.parent != Path('/private/tmp') or stage.stat().st_mode & 0o077 or
        stage.stat().st_uid != os.getuid() or not (stage/'board.sock').is_socket()):
    sys.exit('试验目录须为 /private/tmp 下的本人私有目录，内含就绪的候选看板 socket')
settings = json.loads(args.settings.read_text())
owner = settings['owner']
live = Path(settings['data_dir']) / 'inbox.sqlite3'
db = sqlite3.connect(live.as_uri() + '?mode=ro', uri=True, timeout=5)
db.execute('PRAGMA query_only=ON')
started = (stage / 'started').read_text().strip()
prefix = (stage / 'prefix').read_text().strip()
if not re.fullmatch(r'V2验收-[0-9A-F]{6,32}',prefix):
    sys.exit('需要本次试验的随机前缀')
deadline = datetime.fromisoformat(started).timestamp() + 3600
if not time.time() < deadline <= time.time() + 3610:
    sys.exit('本次一小时验收窗口无效或已经结束')

class PilotBridge(Bridge):
    def handle(self, event):
        content = event['content']
        if content.startswith(prefix) and not event.get('reply_to'):
            try:
                return submit(self, event, content[len(prefix):].lstrip(' ：:'))
            except MobileProblem as error:
                return str(error)
        return super().handle(event)

bridge = PilotBridge({
    'owner': owner, 'lark': settings['lark'], 'data_dir': str(stage / 'bridge'),
    'workbench_socket': str(stage / 'board.sock'), 'workbench_decision_key': str(stage / 'decision'),
    'workbench_only': True, 'outer_intelligence': True,
})
transport = Lark(bridge.s)
running = True

def stop(*args):
    global running
    running = False
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
print('POLL_READY', flush=True)
while running and time.time() < deadline:
    cards = [row[0] for row in bridge.store.db.execute(
        "SELECT ref FROM click_prompts JOIN outbox ON id=outbox_id WHERE kind='outer' AND state='sent' AND ref IS NOT NULL")]
    card_rows = json.dumps(cards)
    with db:
        matches = db.execute("""
        SELECT payload FROM messages
        WHERE julianday(received)>=julianday(?) AND json_extract(payload,'$.sender_id')=?
          AND json_extract(payload,'$.chat_type')='p2p'
          AND json_extract(payload,'$.sender_type')='user'
          AND json_extract(payload,'$.message_type')='text'
          AND (
            (substr(json_extract(payload,'$.content'),1,length(?))=?
             AND coalesce(json_extract(payload,'$.reply_to'),'')='')
            OR json_extract(payload,'$.reply_to') IN (SELECT value FROM json_each(?))
          )
        ORDER BY received,rowid
        """, (started, owner, prefix, prefix, card_rows)).fetchall()
        clicks = db.execute("""
        SELECT body FROM shared_clicks WHERE received>=?
          AND json_extract(body,'$.operator_id')=?
          AND json_extract(body,'$.message_id') IN (SELECT value FROM json_each(?))
          AND json_extract(body,'$.chat_id')=(SELECT json_extract(value,'$') FROM meta WHERE key='owner_p2p_chat')
        ORDER BY received,rowid
        """, (datetime.fromisoformat(started).timestamp(), owner, card_rows)).fetchall()
    for (raw,) in matches:
        if bridge.store.remember(json.loads(raw), owner):
            print('ACCEPTED_TEST_MESSAGE', flush=True)
    for (raw,) in clicks:
        if bridge.store.remember_click(json.loads(raw), owner):
            print('ACCEPTED_TEST_CLICK', flush=True)
    bridge.process_pending()
    bridge.flush(transport)
    bridge.monitor_watches()
    time.sleep(.75)
