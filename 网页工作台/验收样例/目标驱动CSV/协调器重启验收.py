"""在全新的 isolated_goal_hub.sh 实例中运行，证据和下载的交付物写到实例旁。"""
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta

BOARD = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BOARD))
from workbench import Store, Workbench
from superagent.goal_engine import GoalEngine, flow
from superagent.goal_checks import normalize


def read(path):
    return json.loads(path.read_text())


def sha(data):
    return hashlib.sha256(data).hexdigest()


def codex_process(pid):
    result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'pid=,pgid=,stat=,comm='],
                            capture_output=True, text=True)
    fields = result.stdout.strip().split(None, 3)
    if len(fields) == 4 and fields[:2] == [str(pid)] * 2 and 'Z' not in fields[2] and Path(fields[3]).name == 'codex':
        return dict(pid=pid, pgid=pid, stat=fields[2], executable=fields[3])
    return None


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--while-running', action='store_true',
                        help='在原执行进程存活时重启协调器，并核对其首次状态更新')
    parser.add_argument('--lost-receipt', action='store_true',
                        help='与隔离启动器的 --receipt-loss-test 配合，验证业务回执完全丢失')
    args = parser.parse_args()
    live_restart = args.while_running
    lost_receipt = args.lost_receipt
    assert not (live_restart and lost_receipt)
    root = Path(os.environ['AUTOLOOP_RUNTIME']).resolve()
    hub = Path(os.environ['AUTOLOOP_HUB']).resolve()
    assert root.parent == hub.parent and root.parent.parent.parent == Path('/private/tmp')
    assert (root.parent / '.goal-pilot').is_file()
    out = root.parent.parent / 'evidence'
    out.mkdir()
    journal = []
    def note(event, **details):
        stamp = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='milliseconds')
        entry = dict(time=stamp, event=event, **details)
        journal.append(entry)
        with (out/'时间线.jsonl').open('a') as stream:
            print(json.dumps(entry, ensure_ascii=False), file=stream)
        print(stamp, event, details, flush=True)

    fixture = 'id,category\nA,books\nB,\nA,music\nC,\n'
    checks = [dict(id=label, script='csv_check.py', args=['input.csv'], files={'input.csv': data},
                   expected=dict(empty_category_rows=empty, duplicate_id_rows=duplicates))
              for label, data, empty, duplicates in [
                  ('ordinary', fixture, [2,4], [3]),
                  ('counter','id,category\nX,\nX,a\nX,\nY,b\n',[1,3],[2,3]),
                  ('header-only','id,category\n',[],[])]]
    contract = normalize(dict(version=1, stages=[dict(id='csv-quality',checks=checks)]))
    store = Store(root)
    engine = GoalEngine(store, contract)
    task, _ = engine.create(dict(name='M3 协调器恢复 · CSV 工具',goal='交付一个本地命令行工具 csv_check.py 和中文 README.md。用 Python 标准库读取给定 UTF-8 CSV，按原记录顺序输出空 category 及重复 id 再次出现的行号，数据行从 1 开始。标准输出为契约规定的 JSON，原输入保持原样。',
                                 acceptance='三个黑盒检查通过并独立审核。',request_id=str(uuid.uuid4()), budget_minutes=15))
    ticket = task['id']
    note('登记工单', ticket=ticket)
    cmd = [sys.executable, '-B', str(BOARD/'superagent/goal_runtime.py'), 'serve',
           '--runtime', str(root), '--hub', str(hub)]
    db = sqlite3.connect(f'file:{hub}/hub.db?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    def ledger():
        return [dict(x) for x in db.execute('''
            SELECT r.id AS run, e.id AS event, e.key AS event_key, r.state, r.attempts, r.claimed_at, r.finished_at
            FROM runs r JOIN run_events j ON j.run_id=r.id JOIN events e ON e.id=j.event_id
            WHERE e.subject=? OR e.subject LIKE ? ORDER BY r.id''',(ticket,ticket+':%'))]

    log = (out.parent/'coordinator.log').open('a')
    def start():
        child = subprocess.Popen(cmd, stdout=log, stderr=log)
        note('协调器启动',pid=child.pid)
        return child
    child = start()
    interrupted = None
    seen_transport = None
    observer = root.parent.parent/'observer'
    approved = False
    try:
        deadline = time.monotonic()+900
        while time.monotonic()<deadline:
            task = store.get(ticket)
            state = flow(task)
            if child is not None:
                assert child.poll() is None, '协调器意外退出'
            if task['status']=='awaiting_approval':
                assert not approved, '二次申请授权'
                engine.approve(ticket,task['version'],task['approval']['id'])
                approved = True
                note('按本次合成验收授权确认范围',approval=task['approval']['id'])
            for row in state['assignments']:
                if row['phase']!='execute' or not row['run']:
                    continue
                attempt = root/'goal-attempts'/ticket/row['id']
                if lost_receipt:
                    if (observer/'fault.json').exists():
                        if interrupted is None:
                            interrupted = read(observer/'fault.json')
                            assert interrupted['assignment']==row['id'] and not (attempt/'report.json').exists()
                            note('实施产物已写好，写回执前注入 SIGKILL',run=row['run'],files=interrupted['files'])
                        transport = [(r['state'],r['attempts']) for r in ledger() if r['run']==row['run']]
                        if transport!=seen_transport:
                            note('核对原实施轮次的中台状态',run=row['run'],transport=transport)
                            seen_transport = transport
                    continue
                marker = attempt/'process.json'
                if interrupted is None and marker.exists():
                    process = read(marker)
                    live = codex_process(process['pid']) if process['state']=='running' else None
                    if not live:
                        continue
                    before = ledger()
                    if not any(r['run']==row['run'] and r['state'] in ('claimed','running') for r in before):
                        continue
                    assert not (attempt/'report.json').exists()
                    interrupted = dict(assignment=row['id'],event=row['event'],run=row['run'],worker=live,ledger_before=before)
                    note('中台执行中，操作系统确认真实 Codex 存活',run=row['run'],worker_pid=live['pid'])
                    old_pid = child.pid
                    child.kill()
                    assert child.wait(timeout=10)==-signal.SIGKILL
                    child=None
                    assert codex_process(live['pid'])
                    note('协调器强制退出，同一 Codex 进程仍存活',coordinator_pid=old_pid,worker_pid=live['pid'])
                    if live_restart:
                        interrupted['before_restart_update'] = store.get(ticket)['updated_at']
                        child = start()
                if (live_restart and interrupted and not interrupted.get('reconciled_live')
                        and task['updated_at'] > interrupted['before_restart_update']):
                    worker_pid = interrupted['worker']['pid']
                    assert child.poll() is None and codex_process(worker_pid)
                    assert task['status']=='executing' and row['transport']=='claimed'
                    assert row['id']==interrupted['assignment'] and row['run']==interrupted['run']
                    assert not (attempt/'report.json').exists()
                    interrupted['reconciled_live'] = dict(pid=child.pid, updated_at=task['updated_at'])
                    note('新协调器已更新原工单，原 Codex 仍在执行',coordinator_pid=child.pid,
                         worker_pid=worker_pid,run=row['run'],assignment=row['id'])
                if live_restart and interrupted and 'receipt_sha256' not in interrupted and (attempt/'report.json').exists():
                    report_path = attempt/'report.json'
                    report = read(report_path)
                    assert interrupted.get('reconciled_live')
                    assert report['id']==interrupted['assignment'] and report['run']==interrupted['run']
                    interrupted['receipt_sha256'] = sha(report_path.read_bytes())
                    note('原实施轮次交回结果',run=row['run'],sha256=interrupted['receipt_sha256'])
                if child is None and (attempt/'report.json').exists():
                    report = read(attempt/'report.json')
                    if not any(r['run']==row['run'] and r['state']=='done' for r in ledger()):
                        continue
                    assert report['run']==row['run'] and report['id']==row['id'] and not report.get('error'), report.get('error')
                    interrupted['receipt_sha256']=sha((attempt/'report.json').read_bytes())
                    assert row['state']!='settled'
                    note('协调器离线期间原 Run 完成，回执已落盘',run=row['run'],sha256=interrupted['receipt_sha256'])
                    child=start()
            if task['status'] in ('completed','blocked','budget_exhausted','stopped'):
                break
            time.sleep(.25)
        assert task['status']=='completed', (task['status'],task.get('error'))
        rows = state['assignments']
        assert [a['phase'] for a in rows]==['plan','execute','review']
        assert approved and sum(d['kind']=='authorized' for d in state['decisions'])==1
        assert interrupted and rows[1]['id']==interrupted['assignment']
        actual=ledger()
        if lost_receipt:
            assert [r['state'] for r in actual]==['done','nothing','done']
            assert actual[1]['attempts']==1 and rows[1]['report'] is None
            assert not (root/'goal-attempts'/ticket/rows[1]['id']/'report.json').exists()
            recovered = [d for d in state['decisions'] if d['kind']=='recovered_artifacts']
            assert len(recovered)==1 and recovered[0]['digest']==interrupted['checkpoint_digest']
            assert recovered[0]['assignment']==interrupted['assignment'] and recovered[0]['run']==rows[1]['run']
            assert task['goal_delivery']['files']==interrupted['files']
            turns = [read(p) for p in observer.glob('model-*.json')]
            assert len(turns)==3 and len({r['session_sha256'] for r in turns})==3
            assert {r['assignment'] for r in turns}=={r['id'] for r in rows}
        else:
            assert interrupted.get('receipt_sha256') and all(r['state']=='done' for r in actual)
        assert len(actual)==3
        assert [(a['run'],a['event'],a['event_key']) for a in rows]==[(r['run'],r['event'],r['event_key']) for r in actual]
        assert len({r['run'] for r in actual})==3
        sessions=[]
        rounds=[]
        for row in rows:
            if lost_receipt and row['phase']=='execute':
                observed = next(t for t in turns if t['assignment']==row['id'])
                assert observed['model']=='gpt-6.2-sol' and observed['run']==row['run']
                sessions.append(observed['session_sha256'])
                rounds.append(dict(observed,event=row['event'],source='test observer; business receipt absent'))
                continue
            ev=row['report']['evidence']
            assert ev['model']=='gpt-6.2-sol' and row['state']=='settled'
            sessions.append(sha(ev['session'].encode()))
            rounds.append(dict(phase=row['phase'],assignment=row['id'],run=row['run'],event=row['event'],model=ev['model'],
                               started_ns=ev['started_ns'],ended_ns=ev['ended_ns'],session_sha256=sessions[-1]))
        assert len(set(sessions))==3
        verify=rows[-1]['report']['validation']
        assert task['review']['passed'] and verify['passed'] and len(verify['checks'])==3
        assert all(c['passed'] for c in verify['checks'])
        wb=Workbench(store=store)
        for name, digest in task['goal_delivery']['files'].items():
            _,data=wb.artifact_bytes(ticket,name)
            assert sha(data)==digest
            target=out/name
            target.parent.mkdir(exist_ok=True,parents=True)
            target.write_bytes(data)
        assert (out/'csv_check.py').is_file() and (out/'README.md').is_file()
        (out/'input.csv').write_text(fixture)
        note('同单独立验收通过，已下载并校验交付物',ticket=ticket,runs=[a['run'] for a in rows],approvals=1)
        evidence=dict(ticket=ticket,status=task['status'],timeline=journal,interruption=interrupted,
                      approvals=[d for d in state['decisions'] if d['kind']=='authorized'],rounds=rounds,hub_ledger=actual,
                      contract=contract,validation=verify,review=task['review'],files=task['goal_delivery']['files'])
        if lost_receipt:
            evidence.update(reconciliation=recovered,observed_model_calls=turns,implementation_receipt=None)
        (out/'运行证据.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n')
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
        log.close()
        db.close()


if __name__=='__main__':
    main()
