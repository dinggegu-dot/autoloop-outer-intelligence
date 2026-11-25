"""用返修前后的候选快照核对改动与保留项。"""
import re
import time

from superagent import goal_checks
from superagent.outer_verification import _criterion_result, _is_human_criterion, selected_criteria, sha256
from workbench import Problem


_STRING = {'type':'string'}
_LIMIT = 12
_CLI_CHECK_SECONDS = 10
_SHA256 = re.compile(r'[a-f0-9]{64}')


def _object(*names):
    return {'type':'object', 'properties':dict.fromkeys(names, _STRING),
            'required':list(names), 'additionalProperties':False}


REPAIR_SCOPE = {
    'type':'object', 'additionalProperties':False,
    'properties':{
        'retained':{'type':'array', 'items':_object('path','excerpt')},
        'changes':{'type':'array', 'items':_object('path','before','after')},
        'removed_paths':{'type':'array', 'items':_STRING},
    },
    'required':['retained','changes','removed_paths'],
}


def _machine_criteria(assignment):
    contract = assignment['outer_acceptance']
    if not contract['criteria']:
        return []
    return [row for row in selected_criteria(
        contract, assignment['plan'], assignment['step'], additional=assignment.get('additional_review_items', ()),
    ) if row['required'] and not _is_human_criterion(row)]


def _clause(criterion):
    spec = criterion['test_spec']
    return dict(id=criterion['criterion_id'], verifier=criterion['verifier_id'],
                path=spec.get('path',spec.get('script')), clause_sha256=sha256(criterion))


def _count(files, path, excerpt):
    return files.get(path,b'').count(excerpt.encode('utf-8')) if excerpt else 0


def _path(value):
    value = goal_checks._text(value,240)
    parts = value.split('/')
    if len(parts) > goal_checks.MAX_PATH_DEPTH or any(part in ('','.','..') for part in parts):
        raise ValueError('复验路径必须精确指向候选内的相对文件')
    return value


def validate_scope_shape(requested):
    goal_checks._fields(requested, REPAIR_SCOPE['required'])
    if any(type(rows) is not list or len(rows) > _LIMIT for rows in requested.values()):
        raise ValueError('改动与保留项超过清单限制')
    for name, fields in (('retained',('path','excerpt')), ('changes',('path','before','after'))):
        for row in requested[name]:
            goal_checks._fields(row, fields)
            _path(row['path'])
            for field in fields[1:]:
                goal_checks._text(row[field],600)
    for path in requested['removed_paths']:
        _path(path)


def bind_artifact_scope(requested, before, files):
    """把保留项和改动原文绑定到实际文件。"""
    try:
        validate_scope_shape(requested)
        retained = []
        for row in requested['retained']:
            path, excerpt = row['path'], row['excerpt']
            count = _count(files,path,excerpt)
            if path not in files or (excerpt and not count):
                raise ValueError('保留项在返修前候选中不存在')
            retained.append(dict(path=path, excerpt=excerpt, count=count,
                                 sha256=before['files'][path] if not excerpt else None))
        changes = []
        for row in requested['changes']:
            path, old, new = row['path'], row['before'], row['after']
            old_count, new_count = _count(files,path,old), _count(files,path,new)
            if old == new or (old and not old_count):
                raise ValueError('明确改动的旧版原文不存在或与预期相同')
            changes.append(dict(path=path,before=old,after=new,before_count=old_count,after_count=new_count))
        removed = sorted(requested['removed_paths'])
        if (len(removed) != len(set(removed)) or not set(removed) <= files.keys()
                or set(removed).intersection(row['path'] for row in retained + changes)):
            raise ValueError('待移除文件不存在、重复或同时被列为保留与修改')
    except (TypeError, ValueError, OverflowError) as error:
        raise Problem('仍需修改。' + str(error)) from None
    return dict(before_sha256=before['digest'], before_files=before['files'], retained=retained,
                changes=changes, removed_paths=removed)


def prepare_scope(assignment, findings, before, files):
    """在允许写入前绑定可以在旧版上定位的保留项和问题。"""
    scope = bind_artifact_scope(findings.get('repair_scope'), before, files)
    if findings['retain'] and not scope['retained']:
        raise Problem('仍需修改。文字保留项须附可在旧版定位的文件或原文')
    clauses = [_clause(row) for row in _machine_criteria(assignment)]
    if not clauses and not scope['changes'] and not scope['removed_paths']:
        raise Problem('仍需修改。明确问题须能通过批准的验收器复跑，或引用可在新旧版核对的路径与原文。')
    return dict(scope, version=1, acceptance_sha256=sha256(assignment['outer_acceptance']), criteria=clauses)


def retention_labels(scope):
    return [f"{row['path']} 原文「{row['excerpt']}」" if row['excerpt'] else f"{row['path']} 保持原始字节"
            for row in scope['retained']]


def objective_checks(assignment, files, occupied, deadline, *, reserve_seconds=0):
    results = []
    for criterion in _machine_criteria(assignment):
        need = _CLI_CHECK_SECONDS if criterion['verifier_id'] == 'python-json-cli' else 0
        if time.monotonic() + need + reserve_seconds >= deadline:
            status, detail = 'NOT_RUN', '本次派工剩余时间不足以完成独立核验'
        else:
            status, detail = _criterion_result(criterion, files, occupied)
        results.append(dict(_clause(criterion), status=status, detail=detail))
    return results


def changed_files(before, after):
    return sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))


def _change_matches(expected, old_count, new_count):
    old, new = expected['before'], expected['after']
    previous_old, previous_new = expected['before_count'], expected['after_count']
    if any(type(count) is not int or count < 0 for count in (old_count,new_count,previous_old,previous_new)):
        return False
    if old and new:
        # 增加包裹标签或精简原文时，较短的片段可能存在于较长片段内。
        old_in_new = new.count(old)
        new_in_old = old.count(new)
        old_count, new_count = old_count - new_count * old_in_new, new_count - old_count * new_in_old
        previous_old, previous_new = (previous_old - previous_new * old_in_new,
                                      previous_new - previous_old * new_in_old)
    return (min(old_count,new_count,previous_old,previous_new) >= 0
            and (not old or old_count < previous_old) and (not new or new_count > previous_new))


def candidate_verification(assignment, scope, after, files, prior_checks, current_checks, *, keep=False):
    """结算真实快照的客观条款、原文与允许改动的路径。"""
    before_files, after_files = scope['before_files'], after['files']
    intended = {row['path'] for row in scope['changes']} | set(scope['removed_paths'])
    intended.update(row['path'] for row in prior_checks if row['status'] == 'FAIL' and row['path'])
    retained = []
    for row in scope['retained']:
        path, excerpt = row['path'], row['excerpt']
        count = _count(files,path,excerpt)
        artifact_digest = after_files.get(path)
        passing = (artifact_digest == row['sha256'] if not excerpt else count >= row['count'])
        retained.append({'path':path, 'excerpt':excerpt, 'count':count, 'sha256':artifact_digest, 'passed':passing})
    changes = []
    for row in scope['changes']:
        path = row['path']
        old_count = _count(files,path,row['before'])
        new_count = _count(files,path,row['after'])
        passing = path in files and _change_matches(row, old_count, new_count)
        changes.append(dict(path=path,before_count=old_count,after_count=new_count,passed=passing))
    actual = changed_files(before_files, after_files)
    removed = sorted(before_files.keys() - after_files.keys())
    meets_change_scope = (not (actual or changes or removed or scope['removed_paths']) if keep else (
        bool(actual) and (any(row['status'] == 'FAIL' for row in prior_checks) or bool(changes or removed))
    ))
    passed = (
        meets_change_scope and set(actual) <= intended and removed == scope['removed_paths']
        and all(row['status'] in ('PASS','FAIL') for row in prior_checks)
        and all(row['status'] == 'PASS' for row in current_checks)
        and all(row['passed'] for row in [*retained,*changes])
    )
    body = dict(version=1, assignment=assignment['id'], run=assignment.get('run'),
                acceptance_sha256=scope['acceptance_sha256'], scope_sha256=sha256(scope),
                before_sha256=scope['before_sha256'], artifact_sha256=after['digest'],
                changed_files=actual, removed_files=removed, before_checks=prior_checks, checks=current_checks,
                retained=retained, changes=changes, passed=passed)
    return dict(body,evidence_ref=f'sha256:{sha256(body)}')


def verification_labels(verification):
    rows = [f"{row['id']} · {row['detail']}" for row in verification['checks']]
    rows.extend(f"{row['path']} · {'已保留' if row['passed'] else '保留项发生变化'}"
                for row in verification['retained'])
    rows.extend(f"{row['path']} · {'已核对改动原文' if row['passed'] else '原文改动不符合预期'}"
                for row in verification['changes'])
    rows.extend(f'{path} · 已移除' for path in verification['removed_files'])
    return rows


def verification_problem(verification):
    before_checks = verification['before_checks']
    checks = verification['checks']
    if any(row['status'] not in ('PASS','FAIL') for row in before_checks):
        return '无法完成返修前的独立验收，尚不能确认原问题。'
    if any(row['status'] != 'PASS' for row in checks):
        return '新版独立验收未通过。' + ' '.join(
            f"{row['id']}：{row['detail']}" for row in checks if row['status'] != 'PASS')
    if any(not row['passed'] for row in verification['retained']):
        return '新版丢失了原样保留的文件或片段。'
    if any(not row['passed'] for row in verification['changes']):
        return '新版未出现约定的原文改动。'
    return '新版改动超出已登记的文件范围，或缺少能复现的原问题。'


def valid_verification(assignment, checkpoint, scope, verification, *, handoff_mode=None):
    """状态晋级只读取 Worker 生成且与当前派工、范围、版本吻合的证据。"""
    try:
        if type(scope) is not dict or type(verification) is not dict:
            return False
        body = {key:value for key,value in verification.items() if key != 'evidence_ref'}
        contract = (assignment.get('outer_binding') or {}).get('contract_digest')
        valid_binding = (
            scope.get('version') == verification.get('version') == 1
            and _SHA256.fullmatch(scope['acceptance_sha256'])
            and (not contract or contract == scope['acceptance_sha256'])
            and verification['acceptance_sha256'] == scope['acceptance_sha256']
            and verification['scope_sha256'] == sha256(scope)
            and verification['before_sha256'] == scope['before_sha256']
            and scope['before_sha256'] == sha256(sorted(scope['before_files'].items()))
            and verification['assignment'] == assignment['id'] and verification['run'] == assignment.get('run')
            and verification['artifact_sha256'] == checkpoint['digest']
            and checkpoint['digest'] == sha256(sorted(checkpoint['files'].items()))
            and verification['evidence_ref'] == f'sha256:{sha256(body)}'
        )
        if not valid_binding or verification['passed'] is not True:
            return False
        if 'outer_acceptance' in assignment:
            expected_criteria = [] if handoff_mode else [_clause(row) for row in _machine_criteria(assignment)]
            if (scope['acceptance_sha256'] != sha256(assignment['outer_acceptance'])
                    or scope['criteria'] != expected_criteria):
                return False
        before, after = verification['before_checks'], verification['checks']
        for rows, statuses in ((before,('PASS','FAIL')), (after,('PASS',))):
            if len(rows) != len(scope['criteria']) or any(
                row['status'] not in statuses or {key:row.get(key) for key in criterion} != criterion
                for row, criterion in zip(rows,scope['criteria'])
            ):
                return False
        actual = changed_files(scope['before_files'],checkpoint['files'])
        intended = {row['path'] for row in scope['changes']} | set(scope['removed_paths'])
        intended.update(row['path'] for row in before if row['status'] == 'FAIL' and row['path'])
        removed = sorted(scope['before_files'].keys() - checkpoint['files'].keys())
        if (actual != verification['changed_files'] or not set(actual) <= intended
                or removed != verification['removed_files'] or removed != scope['removed_paths']):
            return False
        for name in ('retained','changes'):
            observations = verification[name]
            if (len(observations) != len(scope[name])
                    or any(observed['passed'] is not True or observed['path'] != expected['path']
                           for observed,expected in zip(observations,scope[name]))):
                return False
        for observed, expected in zip(verification['retained'], scope['retained']):
            if (observed['excerpt'] != expected['excerpt']
                    or observed['sha256'] != checkpoint['files'].get(expected['path'])
                    or (observed['count'] < expected['count'] if expected['excerpt']
                        else observed['sha256'] != expected['sha256'])):
                return False
        for observed, expected in zip(verification['changes'], scope['changes']):
            if (expected['path'] not in checkpoint['files']
                    or not _change_matches(expected,observed['before_count'],observed['after_count'])):
                return False
        if handoff_mode == 'keep':
            return not (actual or scope['changes'] or removed)
        return bool(actual) and (any(row['status'] == 'FAIL' for row in before) or bool(scope['changes'] or removed))
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return False
