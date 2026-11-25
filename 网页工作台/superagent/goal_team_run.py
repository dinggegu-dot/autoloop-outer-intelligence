"""在一条已授权的 V2 派工里按顺序运行内层角色。"""
import hashlib
import json
import os
import time

from superagent.goal_engine import StaleCoordination
from superagent.goal_team_handoff import (
    CandidateReadUnproven, DRAFT_OBSERVATIONS, HANDOFF_SCOPE, candidate_read_challenge, check_handoff_candidate,
    draft_read_recorded, explicit_keep_answer, handoff_read_paths, select_handoff_scope,
    validate_draft_observations, validate_handoff_scopes,
)
from superagent.goal_team_records import MIN_REPAIR_ASSIGNMENT_SECONDS, PROFILE, TEAM_ROLE_MIN_SECONDS, TEAM_RUN_RESERVE_SECONDS
from superagent.goal_team_repair import (
    REPAIR_SCOPE, candidate_verification, changed_files, objective_checks, prepare_scope,
    retention_labels, validate_scope_shape, verification_labels, verification_problem,
)
from superagent.outer_verification import capture, needs_human_signoff
from workbench import Problem

ROLE_SUMMARY = {'type': 'string'}
OPTION = {
    'type': 'object', 'additionalProperties': False,
    'properties': {key: ROLE_SUMMARY for key in ('id', 'label', 'text')},
    'required': ['id', 'label', 'text'],
}
RESULT = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'summary': ROLE_SUMMARY,
        'action': {'type': 'string', 'enum': ['done', 'consult', 'consult_design', 'ask', 'repair']},
        'options': {'type': 'array', 'items': OPTION},
        'retain': {'type': 'array', 'items': ROLE_SUMMARY},
        'verify': {'type': 'array', 'items': ROLE_SUMMARY},
    },
    'required': ['summary', 'action', 'options', 'retain', 'verify'],
}
IMPLEMENTATION_ACTIONS = ('done','repair')
TEAM_REVIEW_MODES = ('review','review_and_decide')
MIN_FOLLOWUP_SECONDS = 70
MAX_SUMMARY_CHARS = 3600
MAX_LIST_ITEMS = 12
MAX_LIST_ITEM_CHARS = 300
MAX_OPTIONS = 3
OPTION_LENGTHS = {'id':40,'label':80,'text':1800}


def materialize(files, directory, *, readonly=False):
    directory.mkdir(mode=0o700)
    for relative, content in files.items():
        path = directory / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open('xb') as handle:
            handle.write(content)
            if readonly:
                handle.flush()
                os.fsync(handle.fileno())
        if readonly:
            path.chmod(0o400)


def sealed_workspace(directory, reference):
    identifier = reference.get('assignment')
    if (type(identifier) is not str or len(identifier) != 32
            or any(c not in '0123456789abcdef' for c in identifier)):
        raise Problem('团队候选引用不存在')
    target = directory.parent / identifier / 'sealed'
    if (not target.is_dir() or target.is_symlink()
            or capture(target)[0]['digest'] != reference.get('artifact_sha256')):
        raise Problem('团队候选与登记摘要不一致')
    return target


def seal(workspace, directory):
    checkpoint, files, occupied = capture(workspace)
    if any(not is_directory and path not in files for path, is_directory in occupied.items()):
        raise Problem('候选包含无法安全归档的路径')
    sealed = directory / 'sealed'
    if os.path.lexists(sealed):
        if sealed.is_symlink() or not sealed.is_dir() or capture(sealed)[0] != checkpoint:
            raise Problem('候选与本轮已封存的版本不一致')
        return checkpoint
    staging = directory / 'sealing'
    materialize(files, staging, readonly=True)
    if capture(staging)[0] != checkpoint:
        raise Problem('封存字节与候选副本不一致')
    staging.rename(sealed)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return checkpoint


def workspace_excerpt(workspace):
    _, files, _ = capture(workspace)
    result = {}
    remaining = 24000
    for name, data in files.items():
        if remaining <= 0:
            break
        if len(data) <= min(6000, remaining):
            try:
                result[name] = data.decode('utf-8')
            except UnicodeDecodeError:
                continue
            remaining -= len(data)
    return result


def validated_result(output):
    result = output.get('result')
    if not isinstance(result, dict) or result.keys() - {'repair_scope','observations','handoff_scope'} != set(RESULT['required']):
        raise Problem('角色未提交符合契约的回执')
    if (not isinstance(result['summary'], str) or not result['summary'].strip() or len(result['summary']) > MAX_SUMMARY_CHARS
            or result['action'] not in RESULT['properties']['action']['enum']):
        raise Problem('角色回执缺少有效行动与理由')
    for field in ('retain', 'verify'):
        if (not isinstance(result[field], list) or len(result[field]) > MAX_LIST_ITEMS
                or any(not isinstance(value, str) or len(value) > MAX_LIST_ITEM_CHARS for value in result[field])):
            raise Problem('角色清单超出约定')
    options = result['options']
    if not isinstance(options, list) or len(options) > MAX_OPTIONS:
        raise Problem('样例数量超出约定')
    for option in options:
        if not isinstance(option, dict) or set(option) != set(OPTION['required']):
            raise Problem('样例格式无效')
        for key, limit in OPTION_LENGTHS.items():
            if not isinstance(option[key], str) or not 0 < len(option[key].strip()) <= limit:
                raise Problem('样例内容无效')
    if len({option['id'] for option in options}) != len(options):
        raise Problem('样例选项重复')
    if 'repair_scope' in result:
        try:
            validate_scope_shape(result['repair_scope'])
        except (ValueError, TypeError, OverflowError):
            raise Problem('角色返修回执中的相对路径、原文或清单无效') from None
    if 'observations' in result:
        validate_draft_observations(result['observations'])
    if 'handoff_scope' in result:
        validate_handoff_scopes(result)
    return result


def run_team(engine, assignment, baseline, directory, report, launch_gate):
    from superagent.goal_app_server import GoalAppServerError, invoke
    from superagent.goal_runtime import save_team_recovery

    config = assignment['execution_profile']
    if config != PROFILE:
        raise Problem('该 Worker 无法验证本单冻结的执行配置')
    start = time.time_ns()
    deadline = time.monotonic() + max(0, assignment['max_seconds'] - TEAM_RUN_RESERVE_SECONDS)
    allowed = assignment.get('role_slots', config['max_roles_per_assignment'])
    calls = []
    model_context = {
        'goal': assignment['goal'], 'phase': assignment['phase'],
        'stage': assignment['plan']['steps'][assignment['step']]['objective'],
        **{key: assignment['context'][key] for key in
           ('approved_criteria', 'approved_goal_contract', 'contract_instruction')},
        'repair_or_review_feedback': assignment['context'].get('human_review_feedback')
        or assignment['context'].get('verification'),
        'owner_preference': assignment.get('preference_answer'),
    }
    workspace = directory / 'candidate'
    baseline_checkpoint, baseline_files, _ = capture(baseline)
    materialize(baseline_files, workspace)

    def ask(role, instruction, data, *, writable=False, effort='high', response_schema=None,
            actions=None, verification=None, candidate_source=None, expected_sha256=None,
            capture_output=False, draft_observations=False, handoff_scope=False, read_paths=None):
        if len(calls) >= allowed or deadline - time.monotonic() < TEAM_ROLE_MIN_SECONDS:
            raise Problem('本次派工剩余额度不足以继续内层调用')
        has_preference = bool(model_context['owner_preference'])
        schema = response_schema or RESULT
        if response_schema is None and (actions or has_preference):
            choices = actions or RESULT['properties']['action']['enum']
            choices = [action for action in choices if action != 'ask' or not has_preference]
            if not choices:
                raise Problem('已生效的偏好不能再作为待答事项')
            schema = {**RESULT, 'properties':{**RESULT['properties'],
                      'action':{**RESULT['properties']['action'], 'enum':choices}}}
        scope_required = response_schema is None and actions and 'repair' in actions and verification is None
        if scope_required:
            schema = {**schema, 'properties':{**schema['properties'], 'repair_scope':REPAIR_SCOPE},
                      'required':[*schema['required'], 'repair_scope']}
        if draft_observations:
            schema = {**schema, 'properties':{**schema['properties'], 'observations':DRAFT_OBSERVATIONS},
                      'required':[*schema['required'], 'observations']}
        if handoff_scope:
            schema = {**schema, 'properties':{**schema['properties'], 'handoff_scope':HANDOFF_SCOPE},
                      'required':[*schema['required'], 'handoff_scope']}
        sequence = len(calls) + 1
        record = engine.reserve_team_role(assignment, sequence, role, writable, effort)
        calls.append(record)
        output = {}
        started = False
        checked = None
        read_challenge = None
        read_instruction = ''
        try:
            role_dir = directory / f'role-{sequence}'
            role_dir.mkdir(mode=0o700)
            view = workspace if writable else role_dir / 'materials'
            if not writable:
                if candidate_source is not None:
                    checked, files, _ = capture(candidate_source)
                    if expected_sha256 is not None and checked['digest'] != expected_sha256:
                        raise Problem('当前材料与登记的上版或封存草稿不一致')
                    materialize(files, view, readonly=True)
                    if capture(view)[0] != checked:
                        raise Problem('复核角色材料不对应执行候选')
                    data = {**data, 'candidate_checkpoint':checked}
                else:
                    view.mkdir(mode=0o700)
            format_note = ('返回要求的结构化 JSON。' if response_schema else (
                f'summary 用简短证据写理由，最多 {MAX_SUMMARY_CHARS} 字。'
                f'retain 和 verify 分别最多 {MAX_LIST_ITEMS} 条，每条最多 {MAX_LIST_ITEM_CHARS} 字。'
                f'只在展示样例时填 options（最多 {MAX_OPTIONS} 组，各字段长度上限为 '
                + '、'.join(f'{name} {limit} 字' for name,limit in OPTION_LENGTHS.items()) + '）。'
            ))
            if scope_required:
                format_note += (
                    '仅对 action=repair 填 repair_scope，其他行动的三个清单留空。'
                    'retained 写精确相对文件 path 和从旧版逐字引用的 excerpt；整文件原样保留时 excerpt 留空。'
                    'changes 写精确相对 path、旧版 before、预期新版 after，均为长度不超过 600 字节的原文片段；'
                    '只增加原文时 before 留空，只删片段时 after 留空。完整删除文件须列入 removed_paths。'
                    '批准的客观检查覆盖的问题可由系统定位；其他明确问题必须给出上述可核对的原文改动。'
                    '需要改动其他文件时在 changes 逐项登记。普通 retain 字段可留空。'
                )
            preference_note = (
                '样例选择和意见已生效。请直接用 owner_preference 办好本步，不再征求同一选择或等待确认。'
                if has_preference else '')
            if draft_observations or read_paths is not None:
                if not checked or not files:
                    raise Problem('本轮没有可以留下实际读取证据的候选文件')
                if read_paths is not None:
                    read_paths = sorted(set(read_paths) & files.keys()) or [next(iter(files))]
                    data = {**data, 'read_proof_required_paths':read_paths}
                read_instruction, read_challenge = candidate_read_challenge(checked,files,read_paths)
            prompt = ('当前数据是此工单提供的材料。按授权和角色范围处理，不替用户批准、签收或扩大权限。'
                      '只使用获准的本地 shell、读图及可写角色的工作区补丁。'
                      '禁止使用浏览器、联网、MCP 或子 Agent。网页成果请直接读取本地文件进行检查。'
                      + read_instruction + preference_note + format_note + '\n' + instruction + '\n'
                      + json.dumps(data, ensure_ascii=False))
            record['prompt_sha256'] = hashlib.sha256(prompt.encode()).hexdigest()
            started = True
            output = invoke(model=config['roles'][role], effort=effort,
                            sandbox='workspace-write' if writable else 'read-only', prompt=prompt,
                            schema=schema, workspace=view, output_dir=role_dir,
                            deadline=deadline, launch_gate=launch_gate, read_challenge=read_challenge)
            result = output['result'] if response_schema else validated_result(output)
            if actions and result['action'] not in actions:
                raise Problem('角色回执使用了未开放的行动')
            if draft_observations:
                validate_draft_observations(result.get('observations'), files)
                read_paths = [row['path'] for row in result['observations']]
            if read_paths is not None and not draft_read_recorded(output.get('tools'),checked['files'],read_paths):
                raise CandidateReadUnproven('只读角色未留下与本次实际候选文件匹配的读取记录')
            if handoff_scope:
                validate_handoff_scopes(result, checked, files)
            if capture_output:
                checked = capture(workspace)[0]
        except Exception as error:
            evidence = {key:value for key,value in output.items() if key != 'result'}
            evidence.update(getattr(error,'evidence',{}))
            if 'prompt_sha256' in record:
                evidence['prompt_sha256'] = record['prompt_sha256']
            evidence['error_code'] = (
                'stale_before_role_launch' if isinstance(error, StaleCoordination)
                else getattr(error,'code','validation_error' if started else 'role_preparation_error')
            )
            engine.finish_team_role(assignment, sequence, evidence, status='failed')
            raise
        evidence = {**output, 'prompt_sha256':record['prompt_sha256']}
        if response_schema is None:
            evidence.update({key:result[key] for key in ('summary','action','retain','verify')})
            if result['action'] == 'repair' and 'repair_scope' in result:
                evidence['repair_scope'] = result['repair_scope']
            for field in ('options','observations','handoff_scope'):
                if result.get(field):
                    evidence[field] = result[field]
        if checked is not None:
            record['candidate_sha256'] = evidence['candidate_sha256'] = checked['digest']
        if verification is not None:
            evidence['verification'] = verification
        engine.finish_team_role(assignment, sequence, evidence, status='completed')
        return result

    def inspect(instruction, evidence, *, role='lead', verification=None, read_paths=None):
        excerpts = {'candidate_files':workspace_excerpt(workspace)} if read_paths is None else {}
        return ask(role, instruction,
                   {**model_context, **excerpts, **evidence},
                   candidate_source=workspace, actions=IMPLEMENTATION_ACTIONS, verification=verification,
                   read_paths=read_paths)

    def repair(findings, found_by, *, continuation=None, handoff_selection=None):
        before, before_files, before_occupied = capture(workspace)
        record = {
            'status':'repairing', 'issue':findings['summary'], 'found_by':found_by, 'retain':[],
            'checks':findings['verify'], 'before_sha256':before['digest'],
        }
        def update(status, **values):
            record.update(status=status, **values)
            engine.record_inner_repair(assignment, record)

        def blocked(reason, **values):
            update('blocked', review=reason, **values)
            return dict(report, error=reason, evidence={'started_ns':start,'ended_ns':time.time_ns()})

        def recheck(modification, prior_checks, pending_report=None):
            after, after_files, after_occupied = capture(workspace)
            if after['digest'] != record['after_sha256']:
                return None, blocked('待复验的候选与执行者封存的新版不一致。')
            current_checks = objective_checks(assignment, after_files, after_occupied, deadline, reserve_seconds=8)
            if any(row['status'] == 'NOT_RUN' for row in current_checks) or deadline - time.monotonic() < 8:
                return None, pending_report or blocked('独立核验所需的复验时间不足。')
            verification = candidate_verification(assignment, record['scope'], after, after_files,
                                                 prior_checks, current_checks)
            observed = dict(verification=verification, review_checks=verification_labels(verification))
            update('rechecking', **observed)
            read_paths = set(record['changed_files']) | {item['path'] for item in record['scope']['retained']}
            if handoff_selection:
                handoff_check = check_handoff_candidate(assignment,handoff_selection,after,after_files)
                read_paths.update(handoff_read_paths(after,handoff_check))
            try:
                result = inspect('你是本步负责人，用本地 shell 或读图工具打开新版实际文件，逐项复查上一版指出的明确问题和保留项。'
                                 '在 summary 说明核对的位置和结果，verify 列出实际做的检查。输入同时提供本机检查的结果。'
                                 '明确要求仍未满足时返回 repair，全部解决才返回 done。', {
                                     'repair_findings':findings, 'implementation':modification,
                                     'changed_files':record['changed_files'],
                                     'previous_candidate_sha256':record['before_sha256'],
                                     'local_verification':verification,
                                 }, verification=verification, read_paths=read_paths)
            except (GoalAppServerError, CandidateReadUnproven) as error:
                if pending_report and error.code in ('timeout', 'candidate_read_unproven') and verification['passed']:
                    return None, pending_report
                raise
            if capture(workspace)[0] != after:
                return None, blocked('仍需修改。负责人复验期间候选版本发生变化。', **observed)
            if not verification['passed']:
                return None, blocked('仍需修改。' + verification_problem(verification), **observed)
            if result['action'] != 'done':
                return None, blocked('负责人复验仍需修改。' + result['summary'], **observed)
            update('passed', review=result['summary'], **observed)
            return result, None

        if continuation and 'review_only' in continuation:
            original = continuation['repair_record']
            pending = continuation['review_only']
            if (original.get('status') != 'waiting_review'
                    or original.get('after_sha256') != before['digest']
                    or before['digest'] != continuation['base_sha256']):
                raise Problem('待续办复验没有绑定执行者刚完成的新版')
            record = dict(original,source_assignment=continuation['source_assignment'])
            return recheck(pending['implementation'],pending['before_checks'])
        try:
            scope = prepare_scope(assignment, findings, before, before_files)
        except Problem as error:
            return None, blocked(str(error))
        record.update(scope=scope, retain=retention_labels(scope))
        if continuation:
            original = continuation['repair_record']
            if (original.get('status') != 'waiting' or original.get('scope') != scope
                    or before['digest'] != continuation['base_sha256']):
                return None, blocked('仍需修改。待续办的返修范围与封存候选不一致。')
            record['source_assignment'] = continuation['source_assignment']
        def defer_or_block():
            if continuation:
                return None, blocked('仍需修改。续办的剩余时间或调用额度不足以完成返修和负责人复验。')
            frozen = seal(workspace, directory)
            if frozen != before or not frozen['files']:
                return None, blocked('仍需修改。待续办的返修候选缺少可以封存的原版。')
            update('waiting', review='已封存问题所在的原版，等待本单下一轮返修与复验。')
            return None, dict(report, result=findings, checkpoint=frozen, workspace_id=assignment['id'],
                              deferred_repair=True, evidence={'started_ns':start,'ended_ns':time.time_ns()})

        required_time = MIN_REPAIR_ASSIGNMENT_SECONDS - TEAM_RUN_RESERVE_SECONDS
        if allowed - len(calls) < 2 or deadline - time.monotonic() < required_time:
            return defer_or_block()
        update('repairing')
        prior_checks = objective_checks(assignment, before_files, before_occupied, deadline, reserve_seconds=16)
        if any(row['status'] not in ('PASS','FAIL','NOT_RUN') for row in prior_checks):
            return None, blocked('仍需修改。无法完成返修前的独立验收，尚不能确认原问题。',
                                 review_checks=[f"{row['id']} · {row['detail']}" for row in prior_checks])
        if any(row['status'] == 'NOT_RUN' for row in prior_checks):
            return defer_or_block()
        if not (any(row['status'] == 'FAIL' for row in prior_checks) or scope['changes'] or scope['removed_paths']):
            return None, blocked('仍需修改。旧版客观验收已通过，负责人尚未给出可核对的原文问题。')
        modification = ask('implementer', '修复负责人指出的明确问题，保留记录中的正确部分。'
                           '只修改原文改动清单或实际失败的验收检查所指的文件。'
                           '编辑实际文件并自测，说明改了哪里。若核对单个文件的 SHA-256，'
                           '与 candidate_snapshot.files 中该文件的摘要比对。digest 是整个文件清单的摘要。', {
                               **model_context, 'repair_findings':findings, 'candidate_snapshot':before,
                               'registered_repair_scope':scope, 'observed_before_checks':prior_checks,
                           }, writable=True, actions=IMPLEMENTATION_ACTIONS,capture_output=True)
        after = capture(workspace)[0]
        changed = changed_files(before['files'], after['files'])
        record.update(after_sha256=after['digest'], changes=modification['summary'], changed_files=changed)
        if not changed:
            return None, blocked('仍需修改。执行者返修后，候选文件与原版相同。')
        update('rechecking')
        frozen = seal(workspace, directory)
        if frozen != after or frozen['digest'] != calls[-1].get('candidate_sha256'):
            return None, blocked('封存版本与执行者实际写入的新版不一致。')
        pending_report = dict(report,result=findings,checkpoint=frozen,workspace_id=assignment['id'],
                              repair_review_pending={'implementation':modification,'before_checks':prior_checks},
                              evidence={'started_ns':start,'ended_ns':time.time_ns()})
        # Worker 中断发生在角色间时，从耐久回执续派只读复验，保留已完成的写入。
        save_team_recovery(assignment,directory,pending_report)
        return recheck(modification,prior_checks,pending_report)

    def verify_handoff_candidate(selection):
        checkpoint, files, _ = capture(workspace)
        return check_handoff_candidate(assignment, selection, checkpoint, files)

    def finish_implementation(result, *, require_checked=False, handoff_selection=None):
        if result['action'] == 'repair':
            result, blocked_report = repair(result, calls[-1]['role'], handoff_selection=handoff_selection)
            if blocked_report:
                return blocked_report
        if result['action'] != 'done':
            raise Problem('执行角色未提交可交外层核验的候选')
        handoff_evidence = None
        if handoff_selection:
            handoff_evidence = verify_handoff_candidate(handoff_selection)
            verification = handoff_evidence['verification']
            if not verification['passed']:
                reason = ('选择沿用现稿，但候选字节已变化。' if handoff_selection['mode'] == 'keep'
                          else verification_problem(verification))
                return dict(report, error='审稿续办未通过。' + reason, handoff_verification=handoff_evidence,
                            evidence={'started_ns':start,'ended_ns':time.time_ns()})
        checkpoint = seal(workspace, directory)
        if not checkpoint['files']:
            raise Problem('执行角色没有生成可供独立验收的实际文件')
        if require_checked and calls[-1].get('candidate_sha256') != checkpoint['digest']:
            raise Problem('负责人看到的新版与待送外层验收的封存版本不一致')
        final = dict(report, result=result, checkpoint=checkpoint, workspace_id=assignment['id'],
                     evidence={'started_ns':start,'ended_ns':time.time_ns()})
        if handoff_evidence is not None:
            if handoff_evidence['verification']['artifact_sha256'] != checkpoint['digest']:
                raise Problem('审稿续办所核对的候选与最终封存版本不一致')
            final['handoff_verification'] = handoff_evidence
        return final

    def original_handoff_selection():
        origin = assignment.get('team_handoff_origin')
        if not origin:
            if assignment.get('team_review_mode'):
                raise Problem('本次续办缺少审稿时的原版封存草稿。')
            return None
        handoff, reference = origin['handoff'], origin['base']
        before, files, _ = capture(sealed_workspace(directory, reference))
        if before['digest'] != handoff['draft_sha256'] or before['files'] != reference['files']:
            raise Problem('本次续办与负责人审稿时的原版草稿不一致。')
        return select_handoff_scope(assignment, handoff['instruction'], before, files)

    continuation = assignment.get('repair_continuation')
    draft_handoff = assignment.get('team_handoff_continuation')
    read_only_handoff = assignment.get('team_handoff_read_only') is True
    if read_only_handoff and (continuation or draft_handoff is None):
        raise Problem('沿用现稿的只读核对缺少原单草稿')
    if continuation:
        handoff_selection = original_handoff_selection()
        original = continuation['repair_record']
        result, blocked_report = repair(continuation['finding'],original['found_by'],continuation=continuation,
                                        handoff_selection=handoff_selection)
        if blocked_report:
            return blocked_report
        return finish_implementation(result, require_checked=True, handoff_selection=handoff_selection)

    from superagent.goal_team_blind import make_blind_draft, required_blind
    blind = required_blind(assignment['outer_acceptance'])
    review_mode = assignment.get('team_review_mode')
    decision_required = assignment.get('team_decision_required') is True
    decision_allowed = decision_required or assignment.get('team_decision_allowed') is True
    stage_context = {
        'original_requirements':assignment['context'].get('original_requirements', []),
        'execution_contract':assignment['context'].get('execution_contract'),
    }
    if review_mode or draft_handoff is not None or decision_allowed:
        if blind or assignment['phase'] != 'execute':
            raise Problem('当前派工不能使用本步草稿审阅和决定流程')
    if draft_handoff is not None:
        if (not isinstance(draft_handoff,dict)
                or draft_handoff.keys() != {'comparison','draft_review','instruction','draft_sha256'}
                or draft_handoff['draft_sha256'] != baseline_checkpoint['digest'] or not baseline_files):
            raise Problem('审稿续派的材料与登记的封存草稿不一致')
        for key in ('comparison','draft_review','instruction'):
            validated_result({'result':draft_handoff[key]})
        validate_draft_observations(draft_handoff['draft_review'].get('observations'), baseline_files)
        instruction = draft_handoff['instruction']
        if instruction['action'] not in ('ask','done'):
            raise Problem('审稿续派没有负责人确认的修改清单')
        if instruction['action'] == 'ask':
            preference = model_context['owner_preference'] or {}
            option = preference.get('option_id')
            if not preference.get('event') or not option or not any(
                    row['id'] == option for row in instruction['options']):
                raise Problem('本步必须先从原单收到对当前草稿的有效选择')
        handoff_selection = select_handoff_scope(assignment, instruction, baseline_checkpoint, baseline_files)
        handoff_context = {**model_context, **stage_context, 'team_handoff':draft_handoff,
                           'selected_handoff_scope':handoff_selection}
        if read_only_handoff:
            if allowed != 1 or not explicit_keep_answer(instruction,model_context['owner_preference']):
                raise Problem('只读核对须绑定当前已选的沿用现稿方向和一个角色轮次')
            handoff_check = verify_handoff_candidate(handoff_selection)
            result = ask(
                'lead', '本人已选沿用现稿。逐个打开封存文件，核对文件实际内容、批准的本步验收和审稿记录。'
                '在 summary 写明核对的位置和结果，在 verify 列出已做的检查。'
                '发现明确问题时返回 repair 并说明理由，全部满足时返回 done。',
                {**handoff_context, 'handoff_check':handoff_check},
                candidate_source=workspace, expected_sha256=baseline_checkpoint['digest'],
                actions=IMPLEMENTATION_ACTIONS, verification=handoff_check['verification'],
                read_paths=handoff_read_paths(baseline_checkpoint,handoff_check,whole_candidate=True),
            )
            if result['action'] == 'repair':
                return dict(report, error='沿用现稿未通过负责人复核。' + result['summary'],
                            evidence={'started_ns':start,'ended_ns':time.time_ns()})
            return finish_implementation(result, require_checked=True, handoff_selection=handoff_selection)
        if allowed < 2:
            raise Problem('审稿续派需要执行者和负责人两个可用角色轮次')
        implementation = ask(
            'implementer', '从已封存草稿继续，仅按负责人确认的修改清单、已批准验收和本步本人意见修改。'
            'mode=keep 时维持所有文件字节，检查现有文件即可。mode=edit 时只改登记文件并符合原文变更；'
            '同一文件可以落实本人补充的意见，同时须保留清单标明的内容。自测并在 summary 逐项说明实际处理结果。',
            handoff_context, writable=True, actions=IMPLEMENTATION_ACTIONS,
        )
        implementation_check = verify_handoff_candidate(handoff_selection)
        result = inspect(
            '打开执行者提交的实际文件，核对负责人修改清单、保留项、验收条款及本步本人意见。'
            '系统按原文与文件实测 handoff_check；失败时应指出如何修复实际内容。'
            '明确要求仍有缺漏时返回 repair 并给出可核对的范围，全部满足才返回 done。',
            {**handoff_context, 'implementation':implementation, 'handoff_check':implementation_check},
            read_paths=handoff_read_paths(capture(workspace)[0],implementation_check),
        )
        return finish_implementation(result, require_checked=True, handoff_selection=handoff_selection)
    if review_mode in TEAM_REVIEW_MODES:
        if allowed < 5:
            raise Problem('草稿审阅首派需要五个可用角色轮次')
        if decision_allowed and (review_mode != 'review_and_decide' or model_context['owner_preference']):
            raise Problem('本步选择状态与派工登记不一致')
        independent_context = {key:model_context[key] for key in
                               ('goal','stage','approved_criteria','approved_goal_contract')}
        independent_context['original_requirements'] = stage_context['original_requirements']
        baseline_binding = {'candidate_source':workspace, 'expected_sha256':baseline_checkpoint['digest']}
        lead_plan = ask(
            'lead', '先读原话、批准的条款和实际上一版，提出本步方案与保留项。',
            independent_context, effort='medium', actions=('done',), **baseline_binding,
        )
        independent_plan = ask(
            'advisor', '自己读同样的原话、条款和实际上一版，独立提出方案与一个可观察的反例。',
            independent_context, effort='medium', actions=('done',), **baseline_binding,
        )
        comparison = ask(
            'lead', '比较负责人方案与顾问独立方案，说明具体采纳与舍弃的理由。'
            '在工作区写出本步可审阅的实际初稿。需要本人选择时，先把共同部分做成真实初稿，'
            '并给出基于该草稿的可选方向，留待本人决定。',
            {**model_context, **stage_context, 'lead_plan':lead_plan, 'independent_plan':independent_plan,
             'decision_allowed':decision_allowed, 'decision_required':decision_required,
             'previous_checkpoint':baseline_checkpoint},
            writable=True, effort='medium', actions=('done',), capture_output=True,
        )
        draft = seal(workspace,directory)
        if (not draft['files'] or draft['digest'] == baseline_checkpoint['digest']
                or draft['digest'] != calls[-1].get('candidate_sha256')):
            raise Problem('负责人未形成与回执摘要一致的可审阅新草稿')
        draft_binding = {'candidate_source':directory/'sealed','expected_sha256':draft['digest']}
        draft_review = ask(
            'advisor', '用本地读取工具打开实际封存草稿。observations 每条写精确相对 path、从文件逐字引用的 '
            'excerpt 和具体 issue。优先指出确定的问题或可观察风险；若未发现问题，说明核对的位置与结论。'
            '只有不可逐字引用的非文本文件可将 excerpt 留空。',
            independent_context, effort='medium', actions=('done',), draft_observations=True, **draft_binding,
        )
        decision_note = '本步可以直接继续。交回 done，options 留空。'
        lead_actions = ('done',)
        if decision_allowed:
            decision_note = (
                '本步必须由本人选择，交回 ask。' if decision_required else
                '只有存在新的实质取舍确需本人判断时才交回 ask；可以自行决定就交 done，options 留空。'
            ) + '交回 ask 时附三组有实质差别的 options，说明各自影响，summary 写清要本人决定的问题。'
            lead_actions = ('ask',) if decision_required else ('done','ask')
        instruction = ask(
            'lead', '打开同一封存草稿，对顾问的实物意见说明采纳或舍弃。'
            '在 verify、retain 用自然语言概括复查和保留项。用 handoff_scope.variants 写可自动核对的范围。'
            '交 done 时仅一项且 option_id 留空，交 ask 时每个 options.id 恰有一项。'
            '每项 mode=keep 表示明确沿用初稿的全部文件字节，changes、removed_paths 全空；'
            'mode=edit 至少登记一处确定改动或移除，文件相对路径须精确。'
            'retained 用 excerpt 引用要保留的原文，为空则整个文件原样保留。changes 中 before、after'
            '分别是旧、新的短而具体的逐字原文，限 600 字节，系统检查前后计数。新增片段的 before 留空，'
            '移除片段的 after 留空，删除文件另列 removed_paths。至少登记实际选项所需的目标文本，'
            '可使用短原文为本人在同一文件的补充意见留空间。' + decision_note,
            {**model_context, **stage_context, 'comparison':comparison, 'draft_review':draft_review},
            effort='medium', actions=lead_actions, handoff_scope=True, **draft_binding,
        )
        options = instruction['options']
        asks_owner = instruction['action'] == 'ask'
        if asks_owner and (len(options) != MAX_OPTIONS or any(
                len({row[field].strip().casefold() for row in options}) != MAX_OPTIONS
                for field in ('label','text'))):
            raise Problem('待本人决定的草稿必须给出三组有区别的选择')
        if (not asks_owner and options) or not any(item.strip() for item in instruction['verify']):
            raise Problem('负责人没有提交与本步决定状态匹配的执行清单')
        if capture(directory/'sealed')[0] != draft:
            raise Problem('草稿审阅期间封存版本发生变化')
        handoff_report = dict(report, result=instruction, checkpoint=draft, workspace_id=assignment['id'],
                              team_handoff=dict(comparison=comparison, draft_review=draft_review,
                                                instruction=instruction, draft_sha256=draft['digest']),
                              evidence={'started_ns':start,'ended_ns':time.time_ns()})
        if asks_owner:
            handoff_report['preference'] = {'prompt':instruction['summary'], 'options':options}
        return handoff_report
    if review_mode or decision_allowed:
        raise Problem('该 Worker 无法识别本步草稿审阅模式')

    can_request_preference = (
        needs_human_signoff(assignment['outer_acceptance'])
        and assignment['context'].get('team_review_mode') != 'review_and_decide'
        and not assignment.get('preference_answer')
        and assignment['phase'] == 'execute'
        and assignment['step'] == 0
        and any(criterion.get('verification_type') == 'HUMAN_REVIEW'
                for criterion in assignment['outer_acceptance']['criteria'])
    )
    criteria = model_context['approved_criteria']
    scripts = {criterion['interface'].get('script') for criterion in criteria}
    # 多个参数用例可验证同一个短脚本，拆成多个角色会耗掉复验需要的时间。
    simple = (len(criteria) == 1 or
              len(scripts) == 1 and None not in scripts and
              all(criterion['verifier_id'] == 'python-json-cli' for criterion in criteria))
    coordinated_repair = assignment['phase'] == 'repair' and bool(
        (assignment['context'].get('verification') or {}).get('coordination'))
    proposal = None
    if can_request_preference and allowed >= 5:
        independent_context = {key: model_context[key] for key in
                               ('goal','stage','approved_criteria','approved_goal_contract')}
        proposal = ask('lead',
                       '提出独立做法。已批准目标明确要求先看样例，或关键偏好确实影响成品时，'
                       '返回 action=ask，说明为何需要本人并拟三组有区别的合成样例。'
                       '仅仅需要最终签收时，不要请求中途偏好。', independent_context, effort='medium')
    if proposal and (proposal['action'] == 'ask' or blind):
        advisor = 'advisor'
        comparison = ask(advisor, '只读根据同样的用户原话独立出方案，说明一个可观察的反例。',
                         independent_context, effort='medium')
        samples = ask('lead', '比较两份方案并说明采纳或舍弃。用三组选项拟出具体内容。',
                      {**independent_context, 'own':proposal, 'independent':comparison}, effort='medium')
        notes = ask('advisor', '点评这三组样例，指出一处不自然或会误导选择的句子。', {
            **independent_context, 'samples':samples}, effort='medium')
        final = ask('lead', '依据已有反馈保存三个具体样例。action=ask，summary 用一句可由用户回答的问题。',
                    {**independent_context, 'draft':samples, 'review':notes}, effort='medium', actions=('ask',))
        if final['action'] != 'ask' or len(final['options']) != 3:
            raise Problem('等待前未形成三个可由本人选择的样例')
        checkpoint = seal(workspace, directory)
        return dict(report, result=final, checkpoint=checkpoint, workspace_id=assignment['id'],
                    preference={'prompt':final['summary'], 'options':final['options']},
                    evidence={'started_ns':start,'ended_ns':time.time_ns()})
    if blind:
        result = make_blind_draft(ask,assignment,model_context,blind,workspace,directory,calls,allowed=allowed)
    elif (simple or coordinated_repair) and not model_context['owner_preference'] and proposal is None:
        result = ask('lead', '在当前工作区完成并检查本步骤文件，交回 done。可用本地命令测试。',
                     model_context, writable=True, effort='medium', actions=IMPLEMENTATION_ACTIONS)
    else:
        handoff = proposal or ask(
            'lead', '拟定本步修改与保留项。确需独立意见时交 consult；只对高难设计交 consult_design。',
            model_context, actions=('done','consult','consult_design'),
        )
        consultation = None
        if handoff['action'] in ('consult', 'consult_design') and allowed >= 4:
            role = 'design_advisor' if handoff['action'] == 'consult_design' else 'advisor'
            consultation = ask(role, '从用户原话和验收要求独立给建议，指出一个可复现的反例。',
                               model_context, actions=('done',))
        result = ask('implementer', '实现并自测本步产物。说明采纳或舍弃的意见及理由，保留注明的现有文件。',
                     {**model_context,'lead_handoff':handoff,'independent_advice':consultation}, writable=True,
                     actions=IMPLEMENTATION_ACTIONS)
        result = inspect('打开实际候选文件，亲验并记录依据。仅当明确要求未满足时返回 repair。'
                         '请指出具体位置、预期和实见，repair_scope 写可核对的保留与改动范围，verify 写怎么核对。'
                         '可选建议和待本人判断的偏好可以在摘要说明。', {'implementation':result})
        followup_time_available = deadline - time.monotonic() >= MIN_FOLLOWUP_SECONDS
        if (result['action'] == 'done' and model_context['owner_preference']
                and allowed > len(calls) and followup_time_available):
            result = inspect('从原目标独立查证实际文件，确认意见是否落实。只在明确要求未满足时返回 repair。',
                             {}, role='reviewer')
    return finish_implementation(result)
