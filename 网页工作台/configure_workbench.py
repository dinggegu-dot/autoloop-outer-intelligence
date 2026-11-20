"""校验并登记唯一的网页任务规则，保留现役规则及原始备份。"""
import argparse
import json
import os
import subprocess
import shutil
import sys
import tempfile
from pathlib import Path

from workbench import ROOT, RUNTIME, RULE

HUB = Path.home() / '.agent-autoloop'
CLI = shutil.which('agent-autoloop') or Path.home() / '.local/bin/agent-autoloop'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    options = parser.parse_args()
    path = HUB / 'rules.json'
    old = path.read_bytes()
    rules = json.loads(old)
    config = json.loads((HUB / 'config.json').read_text())
    agent_id = 'odyssey-runner'
    agent = {
        'name': '网页任务执行器',
        'use': 'command',
        'worker': config['worker']['name'],
        'command': [sys.executable, '-B', str(ROOT / 'task_runner.py')],
        'timeout': 65
    }
    rule = {
        'name': RULE,
        'when': {'source': 'local', 'types': ['workbench.task.requested']},
        'to': agent_id,
        'cwd': str(ROOT),
        'parallel': 1,
        'channels': ['odyssey-web-only'],
        'tell': 'never'
    }
    existing = next((row for row in rules['rules'] if row['name'] == RULE), None)
    if existing == rule:
        prior=rules['agents'].get(agent_id,{})
        command=prior.get('command',[])
        if prior==agent or (command==['/usr/bin/python3','-B',str(ROOT/'task_runner.py')] and {**prior,'command':agent['command']}==agent):
            print('网页任务规则已登记')
            return
    if existing is not None or agent_id in rules['agents']:
        raise SystemExit('现有网页任务规则有差异，请保留并核对后再更新')
    if 'odyssey-web-only' in config.get('channels', {}):
        raise SystemExit('网页专用通道存在实际投递配置，停止登记')
    rules.setdefault('channels', {})['odyssey-web-only'] = {'name': '网页审批（无投递进程）'}
    rules['agents'][agent_id] = agent
    rules['rules'].append(rule)
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    candidate = RUNTIME / 'rules.candidate.json'
    candidate.write_text(json.dumps(rules, ensure_ascii=False, indent=2)+'\n')
    subprocess.run([str(CLI), 'check', str(candidate)], check=True, capture_output=True)
    print('专用规则校验通过。新增规则 1 条、执行器 1 个，审批仅路由到无投递进程的网页通道。')
    if options.apply:
        backup = RUNTIME / 'rules.before-workbench.json'
        with backup.open('xb') as stream:
            stream.write(old)
        if path.read_bytes() != old:
            raise SystemExit('规则已被其他进程修改，未覆盖')
        fd, temporary = tempfile.mkstemp(dir=HUB, prefix='.rules-workbench-')
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(candidate.read_bytes())
            os.chmod(temporary, path.stat().st_mode & 0o777)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        print('网页任务规则已登记，等待中台加载')


if __name__ == '__main__':
    main()
