from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from afr_handoff import DIRECTORY, Handoff, HandoffError


TICKET = 'wb-' + 'a' * 32
OTHER = 'wb-' + 'c' * 32
FIRST = 'b' * 32
SECOND = 'e' * 32
ARTICLE = {'kind': 'article', 'id': 'f' * 64, 'revision': 'd' * 64}


class HandoffTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.release = self.root / 'release'
        (self.release / 'afr_agent').mkdir(parents=True)
        state = self.root / 'state'
        state.mkdir()
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'root': str(self.release), 'python': sys.executable,
                                           'vault': str(self.vault)}))
        self.config.chmod(0o600)
        self.item = {**ARTICLE, 'title': '合成研报', 'links': {'canvas': 'obsidian://open?path=%2Ftest'}}
        self.tasks = {TICKET: {'name': '合成研究'}, OTHER: {'name': '另一工单'}}
        self.store = SimpleNamespace(root=state, get=self.tasks.__getitem__)
        quote = '合成引文'
        source = {'ref': '0' * 64, 'revision': hashlib.sha256(quote.encode()).hexdigest(),
                  'heading': '固定原文', 'path': '原材料/固定原文.md', 'line': 2, 'text': quote}
        self.first = {'request': FIRST, 'query': '第一轮的问题', 'claims': [
            {'title': '相关发现', 'statement': '研究结论',
             'citations': [{'ref': source['ref'], 'quote': quote}]}],
            'read': [source], 'report': '研究报告正文。'}
        self.second = {**self.first, 'request': SECOND, 'query': '后续追问'}
        self.archive = {TICKET: {'rounds': [self.first, self.second]}, OTHER: {'rounds': []}}
        self.bridge = self.controller()

    def controller(self):
        return Handoff(self.config, self.store, lambda _, ticket: self.archive[ticket], 43210)

    def ticket(self, ticket=TICKET):
        return self.bridge.handle('ticket', {'ticket': ticket})

    def export(self, request=FIRST, ticket=TICKET):
        return self.bridge.handle('export', {'ticket': ticket, 'request': request}, write=True)['export']

    def attach(self, ticket=TICKET):
        return self.bridge.handle('attach', {'ticket': ticket, **ARTICLE}, write=True)['item']

    def target(self, request=FIRST):
        return self.vault / DIRECTORY / f'{TICKET}-{request}.md'

    def assert_problem(self, status, callback, *args, **kwargs):
        with self.assertRaises(HandoffError) as caught:
            callback(*args, **kwargs)
        self.assertEqual(caught.exception.status, status)

    def test_query_passes_only_validated_input_to_isolated_reader(self):
        done = subprocess.CompletedProcess([], 0, stdout=b'{"items": []}', stderr=b'')
        with patch('afr_handoff.subprocess.run', return_value=done) as run:
            self.assertEqual(self.bridge.handle('library', {'kind': 'article', 'q': ' 测试 ', 'offset': '25'}),
                             {'items': []})
        args, kwargs = run.call_args
        self.assertEqual(args[0][-3:], [str(self.release), str(self.vault), 'library'])
        self.assertIn('-I', args[0])
        if sys.platform == 'darwin':
            self.assertEqual(args[0][0], '/usr/bin/sandbox-exec')
            self.assertIn('deny network-outbound', args[0][2])
        self.assertEqual(json.loads(kwargs['input']), {'kind': 'article', 'q': ' 测试 ', 'offset': 25})
        self.assertEqual(kwargs['timeout'], 25)

    def test_topic_uses_fixed_article_revision_and_read_only_reader(self):
        data = {'topic': {'title': '引用主题', 'index': 2, 'revision': ARTICLE['revision'],
                          'text': '主题正文', 'citations': [], 'totalCitations': 0}}
        done = subprocess.CompletedProcess([], 0, stdout=json.dumps(data).encode(), stderr=b'')
        with patch('afr_handoff.subprocess.run', return_value=done) as run:
            self.assertEqual(self.bridge.handle('topic', {**ARTICLE, 'index': '2'}), data)
            self.assertEqual(run.call_args.args[0][-1], 'topic')
            self.assertEqual(json.loads(run.call_args.kwargs['input']), {**ARTICLE, 'index': 2})
        for index in ('0', '-1', '0002', 10001, True):
            self.assert_problem(400, self.bridge.handle, 'topic', {**ARTICLE, 'index': index})
        self.assert_problem(400, self.bridge.handle, 'topic',
                            {'kind': 'question', 'id': 'q-' + 'a' * 32, 'revision': '1', 'index': 1})
        self.assert_problem(404, self.bridge.handle, 'topic', {**ARTICLE, 'index': 2}, write=True)
        self.assertFalse(self.bridge.db.exists())

    def test_invalid_identity_method_or_extra_fields_do_not_create_a_ledger(self):
        self.assert_problem(404, self.bridge.handle, 'attach', {'ticket': TICKET, **ARTICLE})
        self.assert_problem(400, self.bridge.handle, 'item', dict(ARTICLE, revision='changed'))
        self.assert_problem(400, self.bridge.handle, 'item', dict(ARTICLE, extra='ignored'))
        self.assert_problem(400, self.bridge.handle, 'library', {'offset': '-1'})
        self.assert_problem(404, self.ticket, 'wb-' + '0' * 32)
        self.assertEqual(self.ticket(), {'ticket': {'id': TICKET, 'name': '合成研究'}, 'items': [], 'exports': []})
        self.assertFalse(self.bridge.db.exists())

    def test_attach_validates_and_replays_across_instances_and_tickets(self):
        with patch.object(Handoff, '_read', return_value={'item': self.item}) as query:
            first = self.attach()
            self.bridge = self.controller()
            self.assertEqual(self.attach(), first)
            self.assertEqual(self.attach(OTHER)['revision'], first['revision'])
            self.assertEqual(query.call_args.args, ('item', ARTICLE))
        self.assertEqual(self.ticket()['items'], [first])
        self.assertEqual(len(self.ticket(OTHER)['items']), 1)
        self.assertFalse((self.vault / DIRECTORY).exists())

    def test_changed_reader_revision_rejected_without_ledger(self):
        with patch.object(Handoff, '_read', return_value={'item': {**self.item, 'revision': '1' * 64}}):
            self.assert_problem(409, self.attach)
        self.assertFalse(self.bridge.db.exists())

    def test_reader_errors_stay_bounded(self):
        error = subprocess.CompletedProcess([], 1, stdout=b'{"error":"stale","status":409}', stderr=b'')
        with patch('afr_handoff.subprocess.run', return_value=error):
            self.assert_problem(409, self.bridge.handle, 'item', ARTICLE)
        with patch('afr_handoff.subprocess.run',
                   side_effect=subprocess.TimeoutExpired('reader', 25)):
            self.assert_problem(503, self.bridge.handle, 'item', ARTICLE)

    def test_export_only_selected_verified_round_and_replay(self):
        self.assert_problem(404, self.export, FIRST, OTHER)
        self.assertFalse(self.target().exists())
        second = self.export(SECOND)
        note = self.target(SECOND)
        content = note.read_text()
        self.assertIn(f'http://127.0.0.1:43210/?task={TICKET}&panel=research&round=2&afrRequest={SECOND}#tasks', content)
        self.assertIn('合成引文', content)
        self.assertIn('后续追问', content)
        self.assertNotIn('第一轮的问题', content)
        self.assertEqual(second['sha256'], hashlib.sha256(note.read_bytes()).hexdigest())
        self.assertEqual(second['state'], 'ready')
        self.assertEqual(parse_qs(urlsplit(second['obsidian']).query), {'path': [str(note)]})
        self.assertFalse(self.target().exists())
        original_mtime = note.stat().st_mtime_ns
        self.bridge = self.controller()
        self.assertEqual(self.export(SECOND), second)
        self.assertEqual(note.stat().st_mtime_ns, original_mtime)
        self.assertEqual(self.ticket()['exports'], [second])

    def test_modified_or_missing_export_keeps_verifiable_ledger_state_without_link(self):
        exported = self.export()
        note = self.target()
        note.write_text('本人修改。')
        self.assert_problem(409, self.export)
        self.assertEqual(note.read_text(), '本人修改。')
        exported.pop('obsidian')
        self.assertEqual(self.ticket()['exports'], [{**exported, 'state': 'changed'}])
        note.unlink()
        self.assert_problem(409, self.export)
        self.assertFalse(note.exists())
        self.assertEqual(self.ticket()['exports'], [{**exported, 'state': 'missing'}])

    def test_archive_report_is_preserved_inside_an_inert_fence(self):
        content = ('%s\n![[https://remote.invalid/embedded]]\n'
                   '<img src="https://remote.invalid/img">\n[external](https://remote.invalid)') % ('`' * 8)
        self.first['report'] = content
        self.first['claims'][0]['statement'] = 'https://remote.invalid/img'
        self.export()
        rendered = self.target().read_text()
        fence = '`' * 9
        self.assertIn(f'{fence}text\n{content}\n{fence}\n', rendered)
        self.assertIn('https:\u200b//remote.invalid/img', rendered)

    def test_config_rejects_other_modes_hardlinks_and_oversize(self):
        self.config.chmod(0o644)
        self.assert_problem(503, self.ticket)
        self.config.chmod(0o600)
        alias = self.root / 'another-config-name.json'
        os.link(self.config, alias)
        self.assert_problem(503, self.ticket)
        alias.unlink()
        previous = self.config.read_bytes()
        self.config.write_bytes(previous + b' ' * 8192)
        self.assert_problem(503, self.ticket)
        self.config.write_bytes(previous)
        self.assertEqual(self.ticket()['items'], [])

    def test_existing_directory_or_file_symlinks_are_refused(self):
        outside = self.root / 'outside'
        outside.mkdir()
        directory = self.vault / DIRECTORY
        directory.symlink_to(outside, target_is_directory=True)
        self.assert_problem(409, self.export)
        self.assertEqual(list(outside.iterdir()), [])
        directory.unlink()
        directory.mkdir()
        safe = outside / 'safe.md'
        safe.write_text('保留')
        self.target().symlink_to(safe)
        self.assert_problem(409, self.export)
        self.assertTrue(self.target().is_symlink())
        self.assertEqual(safe.read_text(), '保留')

    def test_untracked_different_file_is_preserved(self):
        self.target().parent.mkdir()
        self.target().write_text('原有笔记')
        self.assert_problem(409, self.export)
        self.assertEqual(self.target().read_text(), '原有笔记')
        self.assertEqual(self.ticket()['exports'], [])

    def test_export_recovers_interruption_between_link_and_cleanup(self):
        note = self.target()
        note.parent.mkdir()
        temp = note.with_name('.handoff-' + note.name + '-' + 'a' * 32)
        task = {'id': TICKET, 'name': self.tasks[TICKET]['name']}
        _, expected = self.bridge._report(task, self.first, 1)
        temp.write_bytes(expected)
        os.link(temp, note)
        self.assertEqual(self.export()['sha256'], hashlib.sha256(expected).hexdigest())
        self.assertEqual(note.read_bytes(), expected)
        self.assertFalse(temp.exists())

    def test_concurrent_export_returns_one_identical_record(self):
        with ThreadPoolExecutor(4) as pool:
            exports = list(pool.map(lambda _: self.export(), range(8)))
        self.assertTrue(all(export == exports[0] for export in exports))
        self.assertEqual(self.ticket()['exports'], [exports[0]])
        self.assertEqual(list(self.target().parent.iterdir()), [self.target()])


if __name__ == '__main__':
    unittest.main()
