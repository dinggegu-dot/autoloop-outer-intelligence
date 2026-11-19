"""隔离试点的故障注入入口。观察数据只供验收程序比对。"""
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from superagent import goal_codex, goal_runtime
from workbench import Store

real_invoke = goal_codex.invoke
real_save = goal_runtime.save_json


def main():
    runtime = Path(sys.argv[sys.argv.index('--runtime') + 1]).resolve()
    assert runtime.parent.parent.parent == Path('/private/tmp')
    assert (runtime.parent/'.goal-pilot').is_file() and runtime.name == 'workbench'
    observations = runtime.parent.parent/'observer'
    observations.mkdir(exist_ok=True)
    def observed_turn(assignment, workspace, directory, **kwargs):
        record = dict(assignment=assignment['id'], run=assignment['run'], phase=assignment['phase'],
                      pid=os.getpid(), model=assignment['model'], start_ns=time.time_ns())
        file = observations/f"model-{assignment['id']}-{os.getpid()}.json"
        real_save(file,record)
        output = real_invoke(assignment,workspace,directory,**kwargs)
        record.update(end_ns=time.time_ns(), session_sha256=hashlib.sha256(output['session'].encode()).hexdigest())
        real_save(file,record)
        return output

    def cut_before_save(path, report):
        if path.name=='report.json' and 'checkpoint' in report and 'validation' not in report and not report.get('error'):
            ticket = path.parent.parent.name
            task = Store(runtime).get(ticket)
            row = next(a for a in task['goal_execution']['assignments'] if a['id']==report['id'])
            if row['phase']=='execute' and not (observations/'fault.json').exists():
                assert row.get('report') is None and row['state']=='dispatched' and not path.exists()
                checkpoint = goal_runtime.manifest(Store(runtime).workspace(ticket))
                assert checkpoint==report['checkpoint'] and {'csv_check.py','README.md'}<=set(checkpoint['files'])
                real_save(observations/'fault.json',dict(time_ns=time.time_ns(),ticket=ticket,
                          assignment=row['id'],run=row['run'],worker_pid=os.getpid(),state=row['state'],
                          report_on_disk=path.exists(),report_in_ledger=row.get('report'),files=checkpoint['files'],
                          checkpoint_digest=checkpoint['digest']))
                os.kill(os.getpid(),signal.SIGKILL)
        real_save(path,report)
    goal_codex.invoke = observed_turn
    goal_runtime.save_json = cut_before_save
    goal_runtime.main()


if __name__=='__main__':
    main()
