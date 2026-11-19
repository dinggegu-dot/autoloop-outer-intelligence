"""Bounded synthetic Phase C. Invokes real Codex through the candidate's normal gates."""
import argparse
import json
import sys
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('candidate', type=Path)
parser.add_argument('--fixture-only', action='store_true')
args = parser.parse_args()
sys.path.insert(0, str(args.candidate.resolve() / '网页工作台'))

from superagent.goal_engine import flow
from superagent.test_outer_verification import OuterExecutionTests, criterion

fixture = OuterExecutionTests()
report_path = Path.cwd() / ('fixture-report.json' if args.fixture_only else 'phase-c-report.json')
report = {'fixture': {'approval': 'synthetic', 'plan_and_initial_execution': 'protocol fixture',
                      'hub_event_and_run_ids': 'locally bound fixture', 'verification': 'candidate actual JSON verifier',
                      'regression': 'one.json changed from {"ok":true} to {"ok":false} in stage 2'},
          'calls': {'coordinator_attempted': 0, 'repair_attempted': 0}}

def record_rows(task):
    return [[r['id'], r['status'], r['artifact_sha256'], r['evidence_ref']]
            for r in flow(task)['validation']['checks']]

def run():
    fixture.authorize(criterion(), criterion('two'))
    fixture.plan(['one'], ['two'])
    (fixture.workspace / 'one.json').write_text('{"ok":true}')
    fixture.checkpoint()
    first = fixture.launch('review')
    task = fixture.settle_report(first, fixture.trusted_review(first))
    assert task['step'] == 1
    report['stage_one'] = record_rows(task)
    (fixture.workspace / 'one.json').write_text('{"ok":false}')
    (fixture.workspace / 'two.json').write_text('{"ok":true}')
    fixture.checkpoint()
    second = fixture.launch('review')
    task = fixture.settle_report(second, fixture.trusted_review(second))
    report['stage_two_regression'] = record_rows(task)
    assert [row[1] for row in report['stage_two_regression']] == ['FAIL', 'PASS']
    assignment = fixture.launch('coordinate')
    context = assignment['context']['coordinator']
    report['scope'] = {'available_actions': context['available_actions'], 'evidence_refs': context['evidence_refs'],
                       'snapshot_revision': context['input_snapshot_revision']}
    if args.fixture_only:
        return
    report['calls']['coordinator_attempted'] += 1
    actual = fixture.trusted_review(assignment)
    report['coordinator'] = {k: actual[k] for k in ('result', 'evidence', 'error') if k in actual}
    task = fixture.settle_report(assignment, actual)
    report['coordinator_application'] = {'status': task['status'], 'stage': flow(task)['stage']}
    if actual.get('error'):
        raise RuntimeError('模型协调轮次失败')
    if flow(task)['stage'] != 'repair':
        return
    repair = fixture.launch('repair')
    report['calls']['repair_attempted'] += 1
    implementation = fixture.trusted_review(repair)
    report['repair'] = {k: implementation[k] for k in ('result', 'evidence', 'error') if k in implementation}
    task = fixture.settle_report(repair, implementation)
    if flow(task)['stage'] != 'review':
        return
    review = fixture.launch('review')
    task = fixture.settle_report(review, fixture.trusted_review(review))
    report['final'] = {'status': task['status'], 'rows': record_rows(task),
                       'artifact_manifest': task.get('goal_delivery', {}).get('files')}

fixture.setUp()
try:
    run()
finally:
    fixture.doCleanups()
    for phase in ('coordinator','repair'):
        report.get(phase,{}).get('evidence',{}).pop('session',None)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(report_path)
