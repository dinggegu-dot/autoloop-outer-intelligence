"""在已封存新版的合成派工中，缩短真实 Codex 复验的共享截止时间。"""

import json
import os
from pathlib import Path
import time
from unittest.mock import patch

from superagent import goal_app_server as app
from superagent.goal_team_phase_one_fixture import active, assignment_matches, record
from process_guard import alive, processes


RESULT = 'phase-one-codex-timeout.json'
PROBE = '/bin/sleep 45'
REMAINING_SECONDS = 3


def role_invoke(assignment):
    def invoke(**settings):
        return _invoke(assignment, settings)
    return invoke


def _invoke(assignment, settings):
    root = Path(__file__).resolve().parents[2]
    role_dir = Path(settings['output_dir'])
    directory = role_dir.parent
    recovery = directory/'candidate-recovery.json'
    result = root/RESULT
    if (active(root) != 'codex-timeout' or result.exists() or assignment.get('repair_continuation')
            or settings['sandbox'] != 'read-only' or not recovery.is_file()
            or '逐项复查上一版指出的明确问题和保留项' not in settings['prompt']
            or not assignment_matches(root, 'codex-timeout', assignment, directory/'sealed')):
        return app.invoke(**settings)

    from superagent.outer_verification import capture

    saved = json.loads(recovery.read_text())['report']
    checkpoint = saved['checkpoint']
    if not saved.get('repair_review_pending') or capture(directory/'sealed')[0] != checkpoint:
        raise RuntimeError('超时前缺少与本次复验绑定的封存新版')
    record_path = role_dir/'process.json'
    observation = {'ticket':assignment['ticket'], 'assignment':assignment['id'], 'run':assignment['run'],
                   'worker_command_pid':os.getpid(), 'sealed_checkpoint':checkpoint,
                   'deadline_injected_after_actual_shell_start':False, 'interrupt_sent':False,
                   'interrupt_acknowledged':False, 'turn_terminal_status':None}
    cutoff = min(settings['deadline'], time.monotonic()+45)
    read = app._Connection.read
    send = app._Connection.send

    def read_event(connection, deadline, *, stopping=False):
        nonlocal cutoff
        event = read(connection, deadline if stopping else min(deadline,cutoff), stopping=stopping)
        params = event.get('params') or {}
        item = params.get('item') or {}
        if (not stopping and event.get('method') == 'item/started' and item.get('type') == 'commandExecution'
                and PROBE in item.get('command','') and not observation['deadline_injected_after_actual_shell_start']):
            cutoff = min(cutoff,time.monotonic()+REMAINING_SECONDS)
            observation['deadline_injected_after_actual_shell_start'] = True
        if stopping and observation['interrupt_sent']:
            if event.get('id') == app._TURN_INTERRUPT:
                observation['interrupt_acknowledged'] = isinstance(event.get('result'),dict)
            if event.get('method') == 'turn/completed':
                observation['turn_terminal_status'] = (params.get('turn') or {}).get('status')
        return event

    def send_packet(connection, packet, deadline, **options):
        if options.get('stopping') and json.loads(packet).get('method') == 'turn/interrupt':
            running = json.loads(record_path.read_text())
            observation['process_group'] = running['pgid']
            observation['live_tracked_processes_at_interrupt'] = len(alive(running['processes'],processes()))
            send(connection, packet, deadline, **options)
            observation['interrupt_sent'] = True
            return
        send(connection, packet, deadline, **options)

    prompt = (f'本隔离合成复验先验证可取消的工具：第一条 shell 命令只执行 `{PROBE}`，如果完成再继续本地只读复验。'
              f'不要并行发起其他命令。\n{settings["prompt"]}')
    try:
        with patch.object(app._Connection,'read',read_event), patch.object(app._Connection,'send',send_packet):
            return app.invoke(**dict(settings,prompt=prompt))
    except app.GoalAppServerError as error:
        process = json.loads(record_path.read_text()) if record_path.is_file() else {}
        current = processes()
        observation.update(error_code=error.code, tracked_process_count=len(process.get('processes',[])),
                           process_record_state=process.get('state'),
                           live_tracked_after_cleanup=len(alive(process.get('processes',[]),current)),
                           live_group_members_after_cleanup=sum(
                               row['group']==process.get('pgid') and not row['state'].startswith('Z')
                               for row in current.values()))
        record(result,observation)
        raise
