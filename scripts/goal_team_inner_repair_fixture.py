"""只给指定隔离浏览器合成工单的首次真实实现注入一处标题错误。"""
import json
from pathlib import Path
import re

from superagent.outer_verification import _html_result, capture


ARTIFACT = 'inner-repair-demo.html'
TITLE = 'Daily Brief'
CHECKS = (('Daily Tasks', 'task-count'), ('Status Notes', 'task-notes'))
HEADING = re.compile(rb'(<h1\b[^>]*>\s*)Daily\s+Tasks(\s*</h1\s*>)', re.I)


def inject(assignment, workspace):
    if assignment['phase'] != 'execute' or assignment['step'] != 0:
        return
    criteria = assignment['outer_acceptance']['criteria']
    stage = assignment['plan']['steps'][0]['acceptance']
    checks = [row for row in criteria if row['criterion_id'] in stage and row['required']
              and row['verifier_id'] == 'html-document' and row['test_spec'] == {'path': ARTIFACT}]
    expected = [{'title': TITLE, 'headings': [heading], 'element_ids': [element_id]}
                for heading, element_id in CHECKS]
    if len(checks) != 2 or any(not any(row['expected_result'] == spec for row in checks) for spec in expected):
        return

    root = Path(__file__).resolve().parents[2]
    if (root.parent != Path('/private/tmp') or not root.name.startswith('autoloop-goal-team-')
            or not (root/'.goal-team-pilot').is_file() or not (root/'.filename-index-pilot').is_file()
            or root/'workbench' not in workspace.resolve().parents):
        raise RuntimeError('内层返修故障只允许注入专用隔离浏览器实例')
    marker = root/'inner-repair-fault.json'
    if marker.exists():
        return

    before, files, _ = capture(workspace)
    data = files.get(ARTIFACT, b'')
    changed, count = HEADING.subn(rb'\g<1>Draft Tasks\g<2>', data)
    status_before = [_html_result(data, spec)[0] for spec in expected]
    status_after = [_html_result(changed, spec)[0] for spec in expected]
    if count != 1 or status_before != ['PASS', 'PASS'] or status_after != ['FAIL', 'PASS']:
        raise RuntimeError('原候选未呈现可单独注入的合成标题错误')

    (workspace/ARTIFACT).write_bytes(changed)
    after, _, _ = capture(workspace)
    with marker.open('x') as handle:
        json.dump({'ticket': assignment['ticket'], 'assignment': assignment['id'], 'run': assignment['run'],
                   'path': ARTIFACT, 'before': before, 'after': after,
                   'checks_before': status_before, 'checks_after': status_after}, handle, indent=2)
        handle.write('\n')
