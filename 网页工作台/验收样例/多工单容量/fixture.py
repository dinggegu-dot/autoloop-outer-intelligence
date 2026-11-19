#!/usr/bin/python3
"""容量实验的协议替身，无网络请求。每个包生成可独立运行的统计程序。"""
import json
import sys
import time
import uuid
from pathlib import Path

job = json.loads(sys.stdin.read().split('\n', 1)[1])
stage = job['phase']
time.sleep(1.5)
if stage == 'plan':
    packages = [dict(id=name, capability=capability, objective='统计 '+name,
                     depends_on=[], outputs=[name+'.py'], checks=[name])
                for name, capability in [('rows','implement'),('unique','analyze')]]
    result = dict(summary='分别统计行数与不同 ID 数量', steps=[
        dict(objective='提供两个独立的本地统计程序',checks=['rows','unique'],packages=packages)])
elif stage == 'package':
    name = job['context']['package']['id']
    expression = {'rows': 'len(ids)', 'unique': 'len(set(ids))'}[name]
    script = name+'.py'
    Path(script).write_text('import csv, json, sys\nwith open(sys.argv[1], newline="") as f:\n'
                            '    ids = [r["id"] for r in csv.DictReader(f)]\n'
                            +f'print(json.dumps({{"{name}": {expression}}}))\n')
    result = dict(summary='已写入统计程序',artifacts=[script])
elif stage == 'review':
    result = dict(passed=job['context']['trusted_verification']['passed'],findings=['固定输入检查已返回'])
else:
    raise ValueError(stage)
for event in [dict(type='thread.started',thread_id=str(uuid.uuid4())),
              dict(type='item.completed',item=dict(type='agent_message',text=json.dumps(result))),
              dict(type='turn.completed',usage=dict(input_tokens=0,output_tokens=0))]:
    print(json.dumps(event))
