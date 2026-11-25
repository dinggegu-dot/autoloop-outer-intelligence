"""受信侧盲评。只有 public 包与已脱敏的检查结果可在解盲前展示。"""
import hashlib
import hmac
import json
import re
import secrets
import unicodedata


RULE_VERSION = 1
REQUIRED_MARKER = '〔评测来源=团队〕'
DEFAULT_MARKERS = ('团队版', '单 Sol 版', '按需团队版')
_MANIFEST_FIELDS = {'rule_version', 'experiment_id', 'case_count', 'projection_sha256', 'rules_sha256', 'mapping_commitment'}
_PRIVATE_FIELDS = {'rule_version', 'experiment_id', 'seed', 'salt', 'rules_sha256', 'heldout_sha256', 'arms_sha256', 'cases'}
_CASE_ID = re.compile(r'[A-Z][0-9]{2}')
_EXPERIMENT_ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}')
_SHA256 = re.compile(r'[0-9a-f]{64}')
_INPUT_FIELDS = {'id', 'context', 'speaker'}
_PROJECTION_FIELDS = _INPUT_FIELDS | {'option_a', 'option_b'}


class BlindReviewError(ValueError):
    def __init__(self, code, location):
        self.code = code
        self.location = location
        super().__init__(f'{code} · {location}')


def canonical_bytes(value):
    """规范化 JSON 编码。字符串原文和空白随 UTF-8 字节保存。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def load_json(raw):
    """加载受信或可见 JSON，同时拒绝重复字段和无效数字。"""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise BlindReviewError('DUPLICATE_FIELD', 'JSON')
            result[key] = value
        return result

    def invalid_number(_):
        raise BlindReviewError('INVALID_JSON', 'JSON')

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_number)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BlindReviewError('INVALID_JSON', 'JSON') from error


def _require(valid, code, location):
    if not valid:
        raise BlindReviewError(code, location)


def _record(value, fields, location):
    _require(type(value) is dict, 'INVALID_OBJECT', location)
    _require(set(value) == fields, 'INVALID_FIELDS', location)


def _ordered_rows(payload, fields, location):
    _record(payload, {'cases'}, location)
    rows = payload['cases']
    _require(type(rows) is list, 'INVALID_LIST', f'{location}.cases')
    by_id = {}
    for index, row in enumerate(rows):
        row_location = f'{location}.cases[{index}]'
        _record(row, fields, row_location)
        case_id = row['id']
        _require(type(case_id) is str and _CASE_ID.fullmatch(case_id), 'INVALID_CASE_ID', f'{row_location}.id')
        _require(case_id not in by_id, 'DUPLICATE_CASE', f'{row_location}.id')
        by_id[case_id] = row
    return by_id


def _text(value, location):
    _require(type(value) is str and bool(value.strip()), 'INVALID_TEXT', location)
    try:
        return value.encode('utf-8')
    except UnicodeEncodeError as error:
        raise BlindReviewError('INVALID_TEXT', location) from error


def _marker_text(value):
    normalized = unicodedata.normalize('NFKC', value).casefold()
    return ''.join(character for character in normalized if unicodedata.category(character) != 'Cf')


def _scan(value, markers, location):
    _text(value, location)
    normalized = _marker_text(value)
    _require(not any(marker in normalized for marker in markers), 'SOURCE_MARKER', location)


def _markers(markers):
    _require(type(markers) in (tuple, list), 'INVALID_MARKERS', 'rules.markers')
    values = set()
    for index, marker in enumerate((REQUIRED_MARKER, *markers)):
        location = f'rules.markers[{index}]'
        _text(marker, location)
        normalized = _marker_text(marker)
        _require(bool(normalized.strip()), 'INVALID_MARKER', location)
        values.add(normalized)
    return sorted(values)


def _rules(markers, expected_count):
    _require(type(expected_count) is int and expected_count > 0 and expected_count % 2 == 0,
             'INVALID_CASE_COUNT', 'rules.case_count')
    return {'rule_version': RULE_VERSION, 'case_count': expected_count, 'markers': _markers(markers)}


def _digest_matches(value, expected):
    return type(value) is str and bool(_SHA256.fullmatch(value)) and hmac.compare_digest(value, expected)


def _freeze_inputs(heldout, baseline, team, rules):
    inputs = _ordered_rows(heldout, _INPUT_FIELDS, 'heldout')
    _require(len(inputs) == rules['case_count'], 'INVALID_CASE_COUNT', 'heldout.cases')
    markers = rules['markers']
    for case_id, case in inputs.items():
        for field in ('context', 'speaker'):
            _scan(case[field], markers, f'heldout.{case_id}.{field}')
    arms, arm_digests = {}, {}
    for name, source in (('baseline', baseline), ('team', team)):
        rows = _ordered_rows(source, {'id', 'response'}, name)
        _require(rows.keys() == inputs.keys(), 'CASE_SET_MISMATCH', f'{name}.cases')
        for case_id, row in rows.items():
            _scan(row['response'], markers, f'{name}.{case_id}.response')
        arms[name] = rows
        arm_digests[name] = digest({'cases': [rows[case_id] for case_id in inputs]})
    return inputs, arms, arm_digests


def _seed(seed):
    if seed is None:
        return secrets.token_hex(32)
    _require(type(seed) is int or (type(seed) is str and bool(seed)), 'INVALID_SEED', 'seed')
    return digest({'seed': seed})


def _order(case_ids, seed):
    # 有盐承诺覆盖种子。固定规则和种子总能按相同例号重建两种方向各半。
    chosen = sorted(case_ids, key=lambda case_id: digest({'seed': seed, 'id': case_id}))[:len(case_ids) // 2]
    chosen = set(chosen)
    return {case_id: ('team' if case_id in chosen else 'baseline') for case_id in case_ids}


def _experiment(value, markers):
    _require(type(value) is str and _EXPERIMENT_ID.fullmatch(value), 'INVALID_EXPERIMENT_ID', 'manifest.experiment_id')
    _scan(value, markers, 'manifest.experiment_id')


def _private(experiment_id, inputs, arms, arm_digests, rules, seed, salt):
    mapping = []
    for case_id, source_a in _order(inputs, seed).items():
        source_b = 'baseline' if source_a == 'team' else 'team'
        row = {'id': case_id}
        for option, source in (('option_a', source_a), ('option_b', source_b)):
            response = arms[source][case_id]['response'].encode('utf-8')
            row[option] = {'source': source, 'sha256': hashlib.sha256(response).hexdigest()}
        mapping.append(row)
    return {'rule_version': RULE_VERSION, 'experiment_id': experiment_id, 'seed': seed, 'salt': salt,
            'rules_sha256': digest(rules), 'heldout_sha256': digest({'cases': list(inputs.values())}),
            'arms_sha256': arm_digests, 'cases': mapping}


def prepare(experiment_id, heldout, baseline, team, *, markers=DEFAULT_MARKERS, expected_count=8, seed=None):
    """返回 (public, private)。调用方单独封存 private 和原始输入。"""
    rules = _rules(markers, expected_count)
    _experiment(experiment_id, rules['markers'])
    inputs, arms, arm_digests = _freeze_inputs(heldout, baseline, team, rules)
    private = _private(experiment_id, inputs, arms, arm_digests, rules, _seed(seed), secrets.token_hex(32))
    projection = {'cases': [dict(inputs[row['id']], **{
        option: arms[row[option]['source']][row['id']]['response'] for option in ('option_a', 'option_b')
    }) for row in private['cases']]}
    manifest = {'rule_version': RULE_VERSION, 'experiment_id': experiment_id, 'case_count': expected_count,
                'projection_sha256': digest(projection), 'rules_sha256': private['rules_sha256'],
                'mapping_commitment': digest(private)}
    return {'projection': projection, 'manifest': manifest, 'manifest_digest': digest(manifest)}, private


def _read_projection(projection, markers):
    rows = _ordered_rows(projection, _PROJECTION_FIELDS, 'projection')
    for case_id, row in rows.items():
        for field in ('context', 'speaker', 'option_a', 'option_b'):
            _scan(row[field], markers, f'projection.{case_id}.{field}')
    return rows


def validate_projection(projection, *, expected_ids, markers=DEFAULT_MARKERS):
    """核对可见候选的字段、例号顺序与泄露标记。供无需读取映射的检查调用。"""
    _require(type(expected_ids) in (list, tuple) and bool(expected_ids), 'INVALID_CASE_IDS', 'rules.case_ids')
    _require(all(type(case_id) is str and _CASE_ID.fullmatch(case_id) for case_id in expected_ids) and
             len(set(expected_ids)) == len(expected_ids), 'INVALID_CASE_IDS', 'rules.case_ids')
    rows = _read_projection(projection, _markers(markers))
    _require(list(rows) == list(expected_ids), 'CASE_ORDER_MISMATCH', 'projection.cases')
    return {'status': 'PASS', 'case_count': len(rows), 'projection_sha256': digest(projection)}


def _read_public(public, markers):
    _record(public, {'projection', 'manifest', 'manifest_digest'}, 'public')
    manifest = public['manifest']
    _record(manifest, _MANIFEST_FIELDS, 'manifest')
    _experiment(manifest['experiment_id'], markers)
    _require(type(manifest['rule_version']) is int and manifest['rule_version'] == RULE_VERSION,
             'RULE_VERSION_MISMATCH', 'manifest.rule_version')
    for field in ('projection_sha256', 'rules_sha256', 'mapping_commitment'):
        _require(type(manifest[field]) is str and _SHA256.fullmatch(manifest[field]), 'INVALID_SHA256', f'manifest.{field}')
    projection = _read_projection(public['projection'], markers)
    _require(type(manifest['case_count']) is int and manifest['case_count'] > 0 and
             manifest['case_count'] == len(projection), 'INVALID_CASE_COUNT', 'manifest.case_count')
    _require(_digest_matches(manifest['projection_sha256'], digest(public['projection'])),
             'PROJECTION_DIGEST_MISMATCH', 'manifest.projection_sha256')
    _require(_digest_matches(public['manifest_digest'], digest(manifest)), 'MANIFEST_DIGEST_MISMATCH', 'manifest_digest')
    return manifest, projection


def validate(public, private, heldout, baseline, team, *, markers=DEFAULT_MARKERS, expected_count=8):
    """重新校验可见结构、冻结原文、每例来源映射及其承诺。"""
    rules = _rules(markers, expected_count)
    manifest, projection = _read_public(public, rules['markers'])
    inputs, arms, arm_digests = _freeze_inputs(heldout, baseline, team, rules)
    _require(list(projection) == list(inputs), 'CASE_ORDER_MISMATCH', 'projection.cases')
    _require(_digest_matches(manifest['rules_sha256'], digest(rules)), 'RULES_DIGEST_MISMATCH', 'manifest.rules_sha256')
    _record(private, _PRIVATE_FIELDS, 'private')
    for field in ('seed', 'salt'):
        _require(type(private[field]) is str and _SHA256.fullmatch(private[field]), 'INVALID_PRIVATE_STATE', f'private.{field}')
    reconstructed = _private(manifest['experiment_id'], inputs, arms, arm_digests, rules, private['seed'], private['salt'])
    _require(private == reconstructed, 'FROZEN_INPUT_OR_MAPPING_MISMATCH', 'private')
    _require(canonical_bytes(private) == canonical_bytes(reconstructed), 'INVALID_PRIVATE_STATE', 'private')
    _require(_digest_matches(manifest['mapping_commitment'], digest(reconstructed)),
             'MAPPING_COMMITMENT_MISMATCH', 'manifest.mapping_commitment')
    for row in reconstructed['cases']:
        case_id = row['id']
        shown = projection[case_id]
        for field in ('context', 'speaker'):
            _require(shown[field] == inputs[case_id][field], 'HELDOUT_TEXT_MISMATCH', f'projection.{case_id}.{field}')
        for option in ('option_a', 'option_b'):
            sha = hashlib.sha256(shown[option].encode('utf-8')).hexdigest()
            _require(_digest_matches(sha, row[option]['sha256']), 'RESPONSE_MISMATCH', f'projection.{case_id}.{option}')
    return {'status': 'PASS', 'case_count': len(projection), 'manifest_digest': public['manifest_digest']}


def prepare_ballot(public, judgments, *, markers=DEFAULT_MARKERS):
    """整理真人原话，保留未评并固定顺序。认证和原子锁定由调用方完成。"""
    _, projection = _read_public(public, _markers(markers))
    rows = _ordered_rows({'cases': judgments}, {'id', 'choice', 'reason'}, 'ballot')
    _require(rows.keys() <= projection.keys(), 'CASE_SET_MISMATCH', 'ballot.cases')
    normalized = []
    for case_id in projection:
        row = rows.get(case_id, {'id': case_id, 'choice': None, 'reason': ''})
        choice, reason = row['choice'], row['reason']
        _require(choice is None or (type(choice) is str and choice in ('a', 'b', 'tie')), 'INVALID_CHOICE', f'ballot.{case_id}.choice')
        _require(type(reason) is str and len(reason) <= 2000, 'INVALID_REASON', f'ballot.{case_id}.reason')
        if reason or choice is not None:
            _text(reason, f'ballot.{case_id}.reason')
        normalized.append(dict(row))
    ballot = {'manifest_digest': public['manifest_digest'], 'cases': normalized}
    return dict(ballot, ballot_digest=digest(ballot))


def unblind(public, private, heldout, baseline, team, ballot, *, expected_ballot_digest,
            markers=DEFAULT_MARKERS, expected_count=8):
    """读取已锁定的选票。单独提供账本中的选票摘要，避免采用临时改写。"""
    check = validate(public, private, heldout, baseline, team, markers=markers, expected_count=expected_count)
    _record(ballot, {'manifest_digest', 'cases', 'ballot_digest'}, 'ballot')
    _require(ballot['manifest_digest'] == check['manifest_digest'], 'BALLOT_MANIFEST_MISMATCH', 'ballot.manifest_digest')
    locked = prepare_ballot(public, ballot['cases'], markers=markers)
    _require(ballot == locked and _digest_matches(expected_ballot_digest, locked['ballot_digest']),
             'BALLOT_DIGEST_MISMATCH', 'ballot.ballot_digest')
    count_keys = {'win': 'wins', 'tie': 'ties', 'loss': 'losses', 'unrated': 'unrated'}
    counts = dict.fromkeys(count_keys.values(), 0)
    cases = []
    for row, mapping in zip(ballot['cases'], private['cases']):
        choice = row['choice']
        if choice is None:
            result = 'unrated'
        elif choice == 'tie':
            result = 'tie'
        else:
            result = 'win' if mapping[f'option_{choice}']['source'] == 'team' else 'loss'
        counts[count_keys[result]] += 1
        cases.append(dict(id=row['id'], outcome=result, reason=row['reason']))
    return {'manifest_digest': check['manifest_digest'], 'ballot_digest': locked['ballot_digest'],
            'counts': counts, 'case_count': len(cases), 'rated_count': len(cases) - counts['unrated'], 'cases': cases}
