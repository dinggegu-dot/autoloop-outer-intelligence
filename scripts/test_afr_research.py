import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from afr_command_agent import Reject
from afr_research_agent import (CANVAS, RECORD, SOURCES, LocalExtract, ScopedNote, action, decode_request,
                                execute, identity, research, sha)
from afr_research_models import OfflineCommand, QUESTIONS, SYNTHETIC, check_synthetic, guarded_synthetic_context, init_synthetic


def row(path, heading, text):
    ref = sha(f'{path}\0{heading}\0{0}')
    return {'ref':ref, 'revision':sha(text), 'path':path, 'heading':heading, 'line':1, 'text':text}


class Library:
    def __init__(self):
        self.rows_by_ref = {}
        self.invocations = []
        for name, content in SYNTHETIC.items():
            value = row(name, content.splitlines()[0][2:], content.strip())
            self.rows_by_ref[value['ref']] = value

    def rows(self):
        return [self.metadata(ref) for ref in self.rows_by_ref], 0, False

    def metadata(self, ref):
        return {key:value for key, value in self.rows_by_ref[ref].items() if key != 'text'}

    def search(self, query, deadline):
        self.invocations.append(('search', query))
        return [self.metadata(ref) for ref, value in self.rows_by_ref.items() if query in value['text']], 0, False

    def read(self, value, deadline):
        self.invocations.append(('read', value['ref']))
        current = self.rows_by_ref[value['ref']]
        if identity(value) != identity(current):
            raise Reject('revision_changed')
        return dict(current)


class Model:
    name = 'test-model'

    def __init__(self, decide):
        self.decide = decide
        self.calls = []

    def __call__(self, context, timeout):
        self.calls.append({'status':'fixture'})
        return self.decide(context)


def findings(context):
    claims = []
    for value in context['read']:
        quote = value['text'].splitlines()[1]
        claims.append({'statement':quote, 'citations':[{'ref':value['ref'], 'quote':quote}]})
    return action('finish', claims=claims, questions=['需复核夜间记录和报价。'])


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.library = Library()
        self.ticket = 'wb-' + 'a' * 32
        self.deadline = time.monotonic() + 90

    def run_turn(self, request_id, query, parent=None, factory=lambda value: LocalExtract(value['query']), ticket=None):
        value = {'ticket':ticket or self.ticket, 'query':query, 'parent':parent}
        return execute(self.library, self.output, self.output, request_id, value, factory, self.deadline)

    def record(self, request_id):
        return json.loads((self.output/self.ticket/request_id/RECORD).read_text())

    def test_lists_seven_sources_across_two_reads_and_seals_canvas(self):
        decisions = []

        def decide(context):
            count = len(context['read'])
            if not context['candidates'] and not count:
                return action('list')
            if context['candidates']:
                return action('read', refs=[item['ref'] for item in context['candidates'][:6]])
            if len(decisions) == 3:
                return action('search', query='夜间误报')
            return findings(context)

        model = Model(lambda context: decisions.append(context) or decide(context))
        self.assertEqual(self.run_turn('r1', QUESTIONS[0], factory=lambda _:model), 7)
        result = self.record('r1')
        self.assertEqual([len(item['references']) for item in result['actions'] if item['action'] == 'read'], [6, 1])
        self.assertEqual(result['open_questions'], ['需复核夜间记录和报价。'])
        folder = self.output/self.ticket/'r1'
        board = json.loads((folder/CANVAS).read_text())
        ids = {item['id'] for item in board['nodes']}
        self.assertEqual(len(ids), len(board['nodes']))
        self.assertTrue(all({edge['fromNode'], edge['toNode']} <= ids for edge in board['edges']))
        notes = (folder/SOURCES).read_text()
        for card in (item for item in board['nodes'] if item['type']=='file'):
            self.assertEqual(self.output/card['file'], folder/SOURCES)
            self.assertIn('## ' + card['subpath'][1:], notes)

    def test_follow_up_has_same_ticket_context_and_is_idempotent(self):
        self.assertEqual(self.run_turn('r1', QUESTIONS[0]), 7)
        contexts = []
        model = Model(lambda context: contexts.append(context) or findings(context))
        self.assertEqual(self.run_turn('r2', QUESTIONS[1], 'r1', lambda _: model), 7)
        self.assertEqual(contexts[0]['prior']['query'], QUESTIONS[0])
        self.assertEqual(len(contexts[0]['read']), 7)
        saved = self.record('r2')
        self.assertEqual((saved['parent'], saved['sequence']), ('r1', 2))
        self.assertEqual(self.run_turn('r2', QUESTIONS[1], 'r1', factory=lambda _: self.fail('repeat called model')), 7)
        with self.assertRaisesRegex(Reject, 'request_conflict'):
            self.run_turn('r2', '其他问题', 'r1')
        with self.assertRaisesRegex(Reject, 'stale_parent'):
            self.run_turn('r3', QUESTIONS[1], 'r1')
        with self.assertRaisesRegex(Reject, 'invalid_parent'):
            self.run_turn('r3', QUESTIONS[1], 'r2', ticket='wb-' + 'b'*32)

    def test_changed_read_is_rejected_before_follow_up(self):
        self.run_turn('r1', QUESTIONS[0])
        original = next(iter(self.library.rows_by_ref.values()))
        original['revision'] = sha('内容已变化')
        with self.assertRaisesRegex(Reject, 'revision_changed'):
            self.run_turn('r2', QUESTIONS[1], 'r1', factory=lambda _: Model(lambda context: self.fail('stale sent')))
        self.assertFalse((self.output/self.ticket/'r2').exists())
        self.assertEqual(self.record('r1')['sequence'], 1)

    def test_change_between_read_and_finish_prevents_delivery(self):
        def decide(context):
            if not context['read']:
                if not context['candidates']:
                    return action('list')
                return action('read', refs=[context['candidates'][0]['ref']])
            self.library.rows_by_ref[context['read'][0]['ref']]['revision'] = sha('改过')
            return findings(context)

        with self.assertRaisesRegex(Reject, 'revision_changed'):
            self.run_turn('r1', QUESTIONS[0], factory=lambda _:Model(decide))
        self.assertFalse((self.output/self.ticket/'r1').exists())

    def test_followup_search_can_refine_and_reopen_previously_read_evidence(self):
        self.run_turn('r1', QUESTIONS[0])
        queries = iter(['报价', '误报'])
        stage = []
        def decide(context):
            stage.append(len(context['candidates']))
            if len(stage) <= 2:
                return action('search', query=next(queries))
            if len(stage) == 3:
                self.assertTrue(context['candidates'])
                return action('read', refs=[context['candidates'][0]['ref']])
            return findings(context)
        self.run_turn('r2', QUESTIONS[1], 'r1', lambda _: Model(decide))
        actions = self.record('r2')['actions']
        self.assertEqual([a['query'] for a in actions if a['action']=='search'], ['报价','误报'])
        self.assertTrue(all(a['count'] > 0 for a in actions if a['action']=='search'))
        self.assertEqual([a['action'] for a in actions][-2:], ['read','finish'])

    def test_false_quote_rejected_then_same_request_can_resume(self):
        def decide(context):
            if context['read']:
                value = findings(context)
                value['claims'][0]['citations'][0]['quote'] = '编造的引用'
                return value
            if context['candidates']:
                return action('read', refs=[context['candidates'][0]['ref']])
            return action('list')

        with self.assertRaisesRegex(Reject, 'invalid_citation'):
            self.run_turn('r1', QUESTIONS[0], factory=lambda _:Model(decide))
        self.assertFalse((self.output/self.ticket/'r1').exists())
        failed = json.loads((self.output/self.ticket/'.failed-r1.json').read_text())
        self.assertEqual(failed['status'], 'invalid_citation')
        self.assertNotIn('编造的引用', str(failed))
        self.assertEqual(self.run_turn('r1', QUESTIONS[0]), 7)

    def test_tool_and_input_scope_are_closed(self):
        context = {'query':QUESTIONS[0], 'ticket':self.ticket, 'parent':None, 'vault':'other'}
        with self.assertRaisesRegex(Reject, 'invalid_request'):
            decode_request(context, 'r1')
        for kind, refs in [('read', ['f'*64]), ('shell', [])]:
            with self.assertRaisesRegex(Reject, 'invalid_action'):
                research(self.library, context, None, Model(lambda _:action(kind, refs=refs)), self.deadline)

    def test_tampered_history_and_mode_switch_never_reach_model(self):
        self.run_turn('r1', QUESTIONS[0])
        synthetic = Model(lambda _: self.fail('cross-mode context reached model'))
        synthetic.name = 'synthetic-codex'
        with self.assertRaisesRegex(Reject, 'invalid_research_history'):
            self.run_turn('r2', QUESTIONS[1], 'r1', factory=lambda _:synthetic)
        result = self.record('r1')
        result['open_questions'] = ['伪造历史']
        (self.output/self.ticket/'r1'/RECORD).write_text(json.dumps(result))
        with self.assertRaisesRegex(Reject, 'invalid_research_history'):
            self.run_turn('r2', QUESTIONS[1], 'r1', factory=lambda _:self.fail('tampered history reached adapter'))

    def test_synthetic_followup_receives_authenticated_prior(self):
        capture = []

        def decide(context):
            safe = guarded_synthetic_context(context, context['question'])
            capture.append(safe)
            if context['read']:
                return findings(context)
            if context['candidates']:
                return action('read', refs=[context['candidates'][0]['ref']])
            return action('list')

        model = Model(decide)
        model.name = 'synthetic-codex'
        self.assertEqual(self.run_turn('r1', QUESTIONS[0], factory=lambda _: model), 1)
        self.assertEqual(self.run_turn('r2', QUESTIONS[1], 'r1', factory=lambda _: model), 1)
        self.assertEqual(capture[-1]['prior']['query'], QUESTIONS[0])
        self.assertEqual(capture[-1]['prior']['open_questions'], ['需复核夜间记录和报价。'])
        self.assertEqual(capture[-1]['prior']['claims'], self.record('r1')['claims'])
        self.assertEqual(self.record('r1')['coverage']['inventory']['unread_passages'], 6)
        board = (self.output/self.ticket/'r1'/CANVAS).read_text()
        self.assertIn('覆盖状态：选读。', board)

    def test_offline_sandbox_failure_records_stable_local_diagnostic(self):
        from subprocess import CompletedProcess

        with patch('afr_research_models.sys.platform', 'darwin'), patch('afr_research_models.Path.is_file', return_value=True):
            model = OfflineCommand(Path('/test/engine'))
        reply = CompletedProcess([], 71, b'', b'sandbox-exec: sandbox_apply: private host detail')
        with patch('afr_research_models.subprocess.run', return_value=reply):
            with self.assertRaisesRegex(Reject, 'offline_sandbox_unavailable'):
                self.run_turn('offline1', QUESTIONS[0], factory=lambda _:model)
        failed = json.loads((self.output/self.ticket/'.failed-offline1.json').read_text())
        self.assertEqual(failed['model_calls'], [{'status':'offline_sandbox_unavailable'}])
        self.assertNotIn('private host detail', str(failed))

    def test_scoped_open_rejects_replaced_file_and_parent_symlink(self):
        vault = self.output/'vault'
        vault.mkdir()
        inside = vault/'folder'
        inside.mkdir()
        inside_file = inside/'note.md'
        inside_file.write_text('allowed')
        outside = self.output/'outside'
        outside.mkdir()
        secret = outside/'note.md'
        secret.write_text('outside scope')
        note = ScopedNote(inside_file)
        self.assertEqual(note.relative_to(vault).as_posix(), 'folder/note.md')
        self.assertEqual(note.read_text('utf-8'), 'allowed')
        inside_file.unlink()
        inside_file.symlink_to(secret)
        with self.assertRaises(OSError):
            note.read_text('utf-8')
        inside_file.unlink()
        inside.rmdir()
        inside.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            note.read_text('utf-8')

    def test_synthetic_provider_only_receives_fixtures_and_explicitly_approved_question(self):
        vault = self.output/'synthetic'
        init_synthetic(vault)
        output = vault/'research'
        question = '别的问题'
        with self.assertRaisesRegex(Reject, 'synthetic_scope_denied'):
            check_synthetic(vault, output, question, None, 'synthetic_query_to_codex', False)
        check_synthetic(vault, output, question, None, 'synthetic_query_to_codex', True)
        known = list(self.library.rows_by_ref.values())[0]
        context = {'question':'private', 'prior':{'query':'secret'}, 'follow_up':False,
                   'available_actions':['finish'], 'turns_left':1, 'candidates':[], 'read':[known]}
        safe = guarded_synthetic_context(context, question)
        self.assertNotIn('secret', str(safe))
        self.assertEqual(safe['question'], question)
        with self.assertRaisesRegex(Reject, 'synthetic_scope_denied'):
            guarded_synthetic_context(dict(context, read=[dict(known, text='秘密')]), question)
        (vault/'private.md').write_text('not for model')
        with self.assertRaisesRegex(Reject, 'synthetic_scope_denied'):
            check_synthetic(vault, output, QUESTIONS[0], None, None, False)


if __name__ == '__main__':
    unittest.main()
