"""核对封存草稿的审稿定位、修改范围和续办凭证。"""
import hashlib
import secrets
import shlex

from superagent import goal_checks
from superagent.goal_team_repair import (
    REPAIR_SCOPE, bind_artifact_scope, candidate_verification, valid_verification, validate_scope_shape,
)
from superagent.outer_verification import sha256
from workbench import Problem


_FIELDS = ('path','excerpt','issue')
DRAFT_OBSERVATIONS = {
    'type':'array', 'items':{
        'type':'object', 'additionalProperties':False,
        'properties':{key:{'type':'string'} for key in _FIELDS}, 'required':list(_FIELDS),
    },
}
_VARIANT_PROPERTIES = {
    'option_id':{'type':'string'},
    'mode':{'type':'string','enum':['edit','keep']},
    **REPAIR_SCOPE['properties'],
}
HANDOFF_SCOPE = {
    'type':'object','additionalProperties':False,'required':['variants'],
    'properties':{'variants':{'type':'array','items':{
        'type':'object','additionalProperties':False,
        'properties':_VARIANT_PROPERTIES,'required':list(_VARIANT_PROPERTIES),
    }}},
}
_SCOPE_FIELDS = REPAIR_SCOPE['required']


def _requested(variant):
    return {key:variant[key] for key in _SCOPE_FIELDS}


def valid_directions(options, *, proposal_choice):
    count = len(options)
    return (count in ((2, 3) if proposal_choice else (3,))
            and all(len({option[field].strip().casefold() for option in options}) == count
                    for field in ('id', 'label', 'text')))


def validate_handoff_scopes(instruction, before=None, before_files=None):
    raw = instruction.get('handoff_scope')
    if raw is None:
        raise Problem('旧版审稿缺少可复验的修改与保留清单，请从原单重新审稿。')
    try:
        goal_checks._fields(raw, ('variants',))
        variants = raw['variants']
        if type(variants) is not list or not 1 <= len(variants) <= 3:
            raise ValueError('修改范围数量无效')
        options = instruction.get('options', [])
        if instruction['action'] not in ('done','ask') or (instruction['action'] == 'done' and options):
            raise ValueError('审稿清单对应继续办理或本人选择')
        expected = [row['id'] for row in options] if instruction['action'] == 'ask' else ['']
        bound = {}
        directions = set()
        for variant in variants:
            goal_checks._fields(variant, _VARIANT_PROPERTIES)
            option = goal_checks._text(variant['option_id'], 160)
            scope = _requested(variant)
            validate_scope_shape(scope)
            if option in bound or option not in expected:
                raise ValueError('修改范围必须与选择方向一一对应')
            has_edits = bool(scope['changes'] or scope['removed_paths'])
            if (variant['mode'] not in ('edit','keep') or (variant['mode'] == 'edit') != has_edits
                    or (has_edits and instruction.get('retain') and not scope['retained'])):
                raise ValueError('沿用现稿须无改动。修改方向须登记可核对的改动和保留项')
            retained_paths = {row['path'] for row in scope['retained']}
            edit_paths = {row['path'] for row in scope['changes']}
            removed_paths = set(scope['removed_paths'])
            whole_files = {row['path'] for row in scope['retained'] if not row['excerpt']}
            if whole_files.intersection(edit_paths):
                raise ValueError('同一文件不能同时要求原样保留和修改')
            if len(removed_paths) != len(scope['removed_paths']) or removed_paths.intersection(retained_paths | edit_paths):
                raise ValueError('移除路径不能重复或同时被列为保留与修改')
            if before is not None:
                from_draft = retained_paths | removed_paths | {row['path'] for row in scope['changes'] if row['before']}
                if not from_draft <= before['files'].keys():
                    raise ValueError('登记的保留或改动原文所在文件不在初稿中')
            direction = sha256([variant['mode'], sorted(sha256(row) for row in scope['changes']), sorted(removed_paths)])
            if direction in directions:
                raise ValueError('选项必须对应不同的实际产物或明确沿用现稿')
            directions.add(direction)
            bound[option] = bind_artifact_scope(scope, before, before_files) if before_files is not None else scope
        if set(bound) != set(expected) or len(expected) != len(variants):
            raise ValueError('每个选择方向须有一份可核对的修改范围')
        return bound
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise Problem('负责人审稿清单无效。' + str(error)) from None


def _selection(assignment, instruction):
    preference = assignment.get('preference_answer') or {}
    option = preference.get('option_id') if instruction['action'] == 'ask' else ''
    if instruction['action'] == 'ask' and (not preference.get('event') or not option):
        raise Problem('本步必须先从原单收到对当前草稿的有效选择')
    variant = next((row for row in instruction['handoff_scope']['variants'] if row['option_id'] == option), None)
    if variant is None:
        raise Problem('本步必须先从原单收到对当前草稿的有效选择')
    if variant['mode'] == 'keep' and (preference.get('text') or '').strip():
        raise Problem('沿用现稿会保留全部字节。补充意见需选择可修改的方向。')
    return variant


def explicit_keep_answer(instruction, answer):
    return bool(instruction['action'] == 'ask' and answer and answer.get('event')
                and not (answer.get('text') or '').strip()
                and _selection({'preference_answer':answer}, instruction)['mode'] == 'keep')


def select_handoff_scope(assignment, instruction, before, before_files):
    bound = validate_handoff_scopes(instruction, before, before_files)
    variant = _selection(assignment, instruction)
    scope = dict(bound[variant['option_id']], version=1, criteria=[],
                 acceptance_sha256=sha256(assignment['outer_acceptance']))
    return dict(mode=variant['mode'], option_id=variant['option_id'], scope=scope)


def check_handoff_candidate(assignment, selection, checkpoint, files):
    verification = candidate_verification(assignment, selection['scope'], checkpoint, files, [], [],
                                          keep=selection['mode'] == 'keep')
    return dict(selection, verification=verification)


def make_handoff_verification(assignment, instruction, before, before_files, checkpoint, files):
    selection = select_handoff_scope(assignment, instruction, before, before_files)
    return check_handoff_candidate(assignment, selection, checkpoint, files)


def valid_handoff_verification(assignment, checkpoint, pending, evidence):
    """核对持久回执是否匹配同一步的初稿、决定和候选版本。"""
    try:
        instruction = pending['handoff']['instruction']
        validate_handoff_scopes(instruction)
        variant = _selection(assignment, instruction)
        if (not isinstance(evidence, dict) or set(evidence) != {'mode','option_id','scope','verification'}
                or evidence['option_id'] != variant['option_id'] or evidence['mode'] != variant['mode']):
            return False
        scope = evidence['scope']
        if (scope['before_sha256'] != pending['base_sha256'] or scope['before_files'] != pending['files']
                or pending['handoff']['draft_sha256'] != scope['before_sha256']
                or scope['criteria'] != []):
            return False
        requested = _requested(variant)
        for field in ('retained','changes'):
            if len(scope[field]) != len(requested[field]) or any(
                any(row.get(key) != value for key,value in expected.items())
                for row,expected in zip(scope[field],requested[field])
            ):
                return False
        if scope['removed_paths'] != sorted(requested['removed_paths']):
            return False
        if any((type(row['count']) is not int or row['count'] < 1) if row['excerpt'] else
               row['sha256'] != pending['files'].get(row['path']) for row in scope['retained']):
            return False
        return valid_verification(assignment, checkpoint, scope, evidence['verification'],
                                  handoff_mode=variant['mode'])
    except (AttributeError, KeyError, Problem, TypeError, ValueError, OverflowError):
        return False


class CandidateReadUnproven(Problem):
    code = 'candidate_read_unproven'


def candidate_read_challenge(checkpoint, files, read_paths=None):
    nonce = secrets.token_hex(16)
    checks = {}
    for path, data in files.items():
        digest = hashlib.sha256(nonce.encode() + b'\0' + path.encode() + b'\0' + data).hexdigest()
        checks[digest] = {'path':path,'sha256':checkpoint['files'][path]}
    def command(path):
        return (
            f'(set -o pipefail; proof_path={shlex.quote(path)}; '
            "checksum=$({ /usr/bin/printf '%s\\000%s\\000' "
            f"'{nonce}' "
            '"$proof_path" && /bin/cat -- "$proof_path"; } | /usr/bin/openssl dgst -sha256 -r)'
            ' && /usr/bin/printf \'workbench-read:%s\\n\' "${checksum%% *}")'
        )

    directions = (
        '必须先逐条运行下列取证命令。每条成功时会输出 workbench-read: 加校验值。'
        if read_paths is not None else
        '必须用下列命令给每个 observations.path 取证，把 proof_path=PATH 的 PATH 替换成精确相对路径。'
        '路径含空格时加 shell 引号。'
    )
    commands = '\n'.join(command(path) for path in (read_paths if read_paths is not None else ('PATH',)))
    return ('\n' + directions + '其他查看或普通校验和不能替代。'
            '取证后打开文件内容作判断，摘要里无需复制校验值。\n' + commands + '\n'), checks


def draft_read_recorded(tools, files, paths):
    observed = set()
    if not isinstance(tools,list):
        return False
    for tool in tools:
        if (not isinstance(tool,dict) or tool.get('name') != 'shell'
                or tool.get('settled') is not True or tool.get('paired') is not True
                or tool.get('status') != 'completed' or tool.get('exit_code') != 0
                or not isinstance(tool.get('candidate_reads'),list)):
            continue
        for row in tool['candidate_reads']:
            if (isinstance(row,dict) and isinstance(row.get('path'),str) and row['path'] in files
                    and row.get('sha256') == files[row['path']]):
                observed.add(row['path'])
    return bool(observed) and set(paths) <= observed


def handoff_read_paths(checkpoint, evidence, *, whole_candidate=False):
    if whole_candidate:
        return sorted(checkpoint['files'])
    scope = evidence['scope']
    paths = {row['path'] for row in scope['retained']} | set(evidence['verification']['changed_files'])
    return sorted(paths & checkpoint['files'].keys())


def handoff_read_recorded(tools, checkpoint, evidence, *, whole_candidate=False):
    return draft_read_recorded(tools, checkpoint['files'],
                               handoff_read_paths(checkpoint,evidence,whole_candidate=whole_candidate))


def validate_draft_observations(observations, files=None):
    if type(observations) is not list or not 1 <= len(observations) <= 12:
        raise Problem('草稿审稿须提供至少一处可核对的文件与意见')
    try:
        for row in observations:
            goal_checks._fields(row, _FIELDS)
            path = goal_checks._text(row['path'],240)
            excerpt = goal_checks._text(row['excerpt'],600)
            issue = goal_checks._text(row['issue'],300)
            parts = path.split('/')
            if (len(parts) > goal_checks.MAX_PATH_DEPTH or any(part in ('','.','..') for part in parts)
                    or not issue.strip() or (excerpt and not excerpt.strip())):
                raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise Problem('草稿审稿的文件路径、原文或意见无效') from None
    if files is None:
        return
    for row in observations:
        path, excerpt = row['path'], row['excerpt']
        data = files.get(path)
        if data is None or (excerpt and excerpt.encode('utf-8') not in data):
            raise Problem('草稿审稿意见与实际封存文件或原文不匹配')
        if not excerpt:
            try:
                data.decode('utf-8')
            except UnicodeDecodeError:
                continue
            raise Problem('草稿审稿须引用可读文本的实际原文')
