import copy
import hashlib
import json
from pathlib import Path
import unittest

from superagent import goal_blind_review as blind


def inputs(count=8):
    cases = [{'id': f'H{number:02}', 'context': f'情境 {number}', 'speaker': f'原话 {number}'}
             for number in range(1, count + 1)]
    baseline = [{'id': row['id'], 'response': f'普通回应 {index}'} for index, row in enumerate(cases)]
    team = [{'id': row['id'], 'response': f'另一句回应 {index}\n'} for index, row in enumerate(cases)]
    team[-1]['response'] = baseline[-1]['response']
    return {'cases': cases}, {'cases': baseline}, {'cases': team}


def rehash(public):
    public['manifest']['projection_sha256'] = blind.digest(public['projection'])
    public['manifest_digest'] = blind.digest(public['manifest'])


class BlindReviewTest(unittest.TestCase):
    def setUp(self):
        self.frozen = inputs()
        self.public, self.private = blind.prepare('fixture-01', *self.frozen, seed=42)

    def test_public_is_ordered_balanced_and_keeps_response_bytes(self):
        first, second = self.public, self.private
        self.assertEqual(blind.validate(first, second, *self.frozen)['status'], 'PASS')
        self.assertEqual(set(first), {'projection', 'manifest', 'manifest_digest'})
        self.assertEqual(set(first['projection']), {'cases'})
        self.assertEqual([item['id'] for item in first['projection']['cases']], [f'H{n:02}' for n in range(1, 9)])
        self.assertEqual(sum(item['option_a']['source'] == 'team' for item in second['cases']), 4)
        self.assertNotIn(second['seed'], blind.canonical_bytes(first).decode())
        self.assertNotIn(second['salt'], blind.canonical_bytes(first).decode())
        for item, mapping in zip(first['projection']['cases'], second['cases']):
            self.assertEqual(set(item), {'id', 'context', 'speaker', 'option_a', 'option_b'})
            self.assertEqual(item['id'], mapping['id'])
            for option in ('option_a', 'option_b'):
                self.assertEqual(hashlib.sha256(item[option].encode()).hexdigest(), mapping[option]['sha256'])
        same = first['projection']['cases'][-1]
        self.assertEqual(same['option_a'], same['option_b'])
        again, another_map = blind.prepare('fixture-01', *self.frozen, seed=42)
        self.assertEqual(again['projection'], first['projection'])
        self.assertEqual(another_map['cases'], second['cases'])
        self.assertNotEqual(again['manifest']['mapping_commitment'], first['manifest']['mapping_commitment'])

    def test_preflight_allowlist_markers_and_missing_cases(self):
        frozen = inputs(2)
        clean, private = blind.prepare('preflight-01', *frozen, expected_count=2, seed='fixture')
        self.assertEqual(blind.validate(clean, private, *frozen, expected_count=2)['status'], 'PASS')
        bad_field, bad_marker, missing = (copy.deepcopy(clean) for _ in range(3))
        bad_field['projection']['cases'][0]['source'] = 'option_a: team'
        bad_marker['projection']['cases'][0]['option_a'] = blind.REQUIRED_MARKER + '正文'
        missing['projection']['cases'].pop()
        for payload, code, location in [(bad_field, 'INVALID_FIELDS', 'projection.cases[0]'),
                                        (bad_marker, 'SOURCE_MARKER', 'projection.H01.option_a'),
                                        (missing, 'INVALID_CASE_COUNT', 'manifest.case_count')]:
            with self.subTest(code=code), self.assertRaises(blind.BlindReviewError) as error:
                blind.validate(payload, private, *frozen, expected_count=2)
            self.assertEqual((error.exception.code, error.exception.location), (code, location))
        marked = copy.deepcopy(frozen)
        marked[2]['cases'][0]['response'] = '本条由 Ｏpen\u200bＡＩ 生成。'
        with self.assertRaisesRegex(blind.BlindReviewError, 'SOURCE_MARKER'):
            blind.prepare('preflight-02', *marked, expected_count=2, markers=['OpenAI'])

    def test_public_projection_can_be_checked_without_access_to_private_mapping(self):
        projection = self.public['projection']
        expected = [row['id'] for row in self.frozen[0]['cases']]
        check = blind.validate_projection(projection, expected_ids=expected)
        self.assertEqual(check, {'status': 'PASS', 'case_count': 8, 'projection_sha256': blind.digest(projection)})
        with self.assertRaisesRegex(blind.BlindReviewError, 'CASE_ORDER_MISMATCH'):
            blind.validate_projection(projection, expected_ids=expected[::-1])
        with self.assertRaisesRegex(blind.BlindReviewError, 'INVALID_CASE_IDS'):
            blind.validate_projection(projection, expected_ids=expected + expected)

    def test_exact_identity_original_text_and_frozen_source(self):
        holdout, baseline, team = copy.deepcopy(self.frozen)
        baseline['cases'].reverse()
        self.assertEqual(blind.validate(self.public, self.private, holdout, baseline, team)['status'], 'PASS')
        baseline['cases'][0]['id'] = baseline['cases'][1]['id']
        with self.assertRaisesRegex(blind.BlindReviewError, 'DUPLICATE_CASE'):
            blind.prepare('fixture-02', holdout, baseline, team)
        baseline = self.frozen[1]
        team['cases'][0]['id'] = 'H09'
        with self.assertRaisesRegex(blind.BlindReviewError, 'CASE_SET_MISMATCH'):
            blind.prepare('fixture-02', holdout, baseline, team)
        for field, code in [('context', 'HELDOUT_TEXT_MISMATCH'), ('speaker', 'HELDOUT_TEXT_MISMATCH'),
                            ('option_b', 'RESPONSE_MISMATCH')]:
            item = copy.deepcopy(self.public)
            item['projection']['cases'][0][field] += ' '
            rehash(item)
            with self.subTest(field=field), self.assertRaisesRegex(blind.BlindReviewError, code):
                blind.validate(item, self.private, *self.frozen)
        reordered = copy.deepcopy(self.public)
        reordered['projection']['cases'].reverse()
        rehash(reordered)
        with self.assertRaisesRegex(blind.BlindReviewError, 'CASE_ORDER_MISMATCH'):
            blind.validate(reordered, self.private, *self.frozen)
        _, _, changed = copy.deepcopy(self.frozen)
        changed['cases'][0]['response'] = '更换已封存的答复'
        with self.assertRaisesRegex(blind.BlindReviewError, 'FROZEN_INPUT_OR_MAPPING_MISMATCH'):
            blind.validate(self.public, self.private, holdout, baseline, changed)

    def test_manifest_and_private_commitment_cannot_be_replaced(self):
        replaced = copy.deepcopy(self.private)
        replaced['salt'] = '0' * 64
        with self.assertRaisesRegex(blind.BlindReviewError, 'MAPPING_COMMITMENT_MISMATCH'):
            blind.validate(self.public, replaced, *self.frozen)
        replaced = copy.deepcopy(self.private)
        replaced['cases'][0]['option_a']['source'] = replaced['cases'][0]['option_b']['source']
        with self.assertRaisesRegex(blind.BlindReviewError, 'FROZEN_INPUT_OR_MAPPING_MISMATCH'):
            blind.validate(self.public, replaced, *self.frozen)
        with self.assertRaisesRegex(blind.BlindReviewError, 'RULES_DIGEST_MISMATCH'):
            blind.validate(self.public, self.private, *self.frozen, markers=())
        replaced = copy.deepcopy(self.public)
        replaced['manifest']['experiment_id'] = 'fixture-replaced'
        with self.assertRaisesRegex(blind.BlindReviewError, 'MANIFEST_DIGEST_MISMATCH'):
            blind.validate(replaced, self.private, *self.frozen)

    def test_ballot_requires_locked_digest_and_counts_full_denominator(self):
        first, second = self.private['cases'][:2]
        team_choice = 'a' if first['option_a']['source'] == 'team' else 'b'
        baseline_choice = 'a' if second['option_a']['source'] == 'baseline' else 'b'
        judgments = [{'id': 'H03', 'choice': 'tie', 'reason': '都贴切'},
                     {'id': 'H04', 'choice': None, 'reason': '还要再读一次'},
                     {'id': 'H02', 'choice': baseline_choice, 'reason': '较克制'},
                     {'id': 'H01', 'choice': team_choice, 'reason': '有回应具体处境'}]
        ballot = blind.prepare_ballot(self.public, judgments)
        choice = blind.unblind(self.public, self.private, *self.frozen, ballot,
                               expected_ballot_digest=ballot['ballot_digest'])
        self.assertEqual(choice['counts'], {'wins': 1, 'ties': 1, 'losses': 1, 'unrated': 5})
        self.assertEqual((choice['case_count'], choice['rated_count']), (8, 3))
        self.assertEqual(choice['cases'][0], {'id': 'H01', 'outcome': 'win', 'reason': '有回应具体处境'})
        self.assertEqual(choice['cases'][3], {'id': 'H04', 'outcome': 'unrated', 'reason': '还要再读一次'})
        self.assertEqual(choice['cases'][4]['reason'], '')
        self.assertNotIn('source', blind.canonical_bytes(choice).decode())
        updated = blind.prepare_ballot(self.public, [{**judgments[0], 'reason': '已改的理由'}] + judgments[1:])
        with self.assertRaisesRegex(blind.BlindReviewError, 'BALLOT_DIGEST_MISMATCH'):
            blind.unblind(self.public, self.private, *self.frozen, updated, expected_ballot_digest=ballot['ballot_digest'])
        newer, newer_private = blind.prepare('fixture-02', *self.frozen, seed=42)
        with self.assertRaisesRegex(blind.BlindReviewError, 'BALLOT_MANIFEST_MISMATCH'):
            blind.unblind(newer, newer_private, *self.frozen, ballot, expected_ballot_digest=ballot['ballot_digest'])

    def test_rejects_unexplained_score_duplicate_and_out_of_scope_case(self):
        for rows, code in [([{'id': 'H01', 'choice': 'a', 'reason': ''}], 'INVALID_TEXT'),
                           ([{'id': 'H01', 'choice': 'team', 'reason': '理由'}], 'INVALID_CHOICE'),
                           ([{'id': 'H99', 'choice': None, 'reason': ''}], 'CASE_SET_MISMATCH'),
                           ([{'id': 'H01', 'choice': 'a', 'reason': '理由'}] * 2, 'DUPLICATE_CASE')]:
            with self.subTest(code=code), self.assertRaisesRegex(blind.BlindReviewError, code):
                blind.prepare_ballot(self.public, rows)

    def test_strict_json_rejects_duplicates_and_nonfinite_numbers(self):
        self.assertEqual(blind.load_json(blind.canonical_bytes(self.public)), self.public)
        with self.assertRaisesRegex(blind.BlindReviewError, 'DUPLICATE_FIELD'):
            blind.load_json(b'{"cases":[],"cases":[]}')
        with self.assertRaisesRegex(blind.BlindReviewError, 'INVALID_JSON'):
            blind.load_json(b'{"cases":[NaN]}')



class RegisteredBlindVerifierTest(unittest.TestCase):
    def test_independent_registry_executes_all_product_preflight_cases(self):
        from superagent.outer_verification import LOCAL_DATA, _artifact_spec, _criterion_result, catalogue

        verifier, = [row for row in catalogue() if row['verifier_id'] == 'blind-projection']
        self.assertEqual(verifier['version'], 1)
        contract, expected = _artifact_spec('blind-projection', {'path':'blind.json'}, {'case_ids':['V01','V02']})
        fixture = Path(__file__).resolve().parents[1]/'验收样例'/'匿名评测'/'匿名预检夹具.json'
        criterion = dict(verifier_id='blind-projection', environment=LOCAL_DATA, human_review_required=False,
                         verification_type='DETERMINISTIC', test_spec=contract, expected_result=expected)
        for item in json.loads(fixture.read_text())['fixtures']:
            with self.subTest(case=item['case']):
                status, _ = _criterion_result(criterion, {'blind.json':blind.canonical_bytes(item['input'])}, {})
                self.assertEqual(status, item['expected'])

if __name__ == '__main__':
    unittest.main()
