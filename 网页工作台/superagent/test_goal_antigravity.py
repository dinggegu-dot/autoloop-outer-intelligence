"""Validate the host side of the Antigravity role contract."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from superagent.goal_antigravity import _apply, _inputs
from superagent.goal_team_handoff import draft_read_recorded


class AntigravityArtifactsTest(TestCase):
    def test_edits_only_apply_to_the_provided_text_snapshot(self):
        with TemporaryDirectory() as directory:
            workspace = Path(directory).resolve()
            (workspace / 'known.txt').write_text('original')
            (workspace / 'binary.png').write_bytes(b'\xff\xfe')
            before, provided, occupied = _inputs(workspace)
            valid = {'path': 'known.txt', 'action': 'write', 'content': 'new'}
            for change in [
                {'path': '../outside.txt', 'action': 'write', 'content': 'new'},
                {'path': 'binary.png', 'action': 'delete', 'content': ''},
                {'path': 'agents.md', 'action': 'write', 'content': 'new'},
                {'path': 'KNOWN.txt', 'action': 'write', 'content': 'new'},
            ]:
                with self.subTest(path=change['path']), self.assertRaises(ValueError):
                    _apply(workspace, before, occupied, provided, [valid, change])
                self.assertEqual((workspace / 'known.txt').read_text(), 'original')
            _apply(workspace, before, occupied, provided, [valid])
            self.assertEqual((workspace / 'known.txt').read_text(), 'new')
            self.assertEqual((workspace / 'binary.png').read_bytes(), b'\xff\xfe')

    def test_candidate_receipt_does_not_substitute_for_a_codex_tool(self):
        files = {'sample.txt': 'a' * 64}
        tools = [{'name': 'antigravity_context', 'settled': True, 'paired': True, 'status': 'completed',
                  'exit_code': 0, 'candidate_reads': [{'path': 'sample.txt', 'sha256': files['sample.txt']}]}]
        self.assertTrue(draft_read_recorded(tools, files, ['sample.txt'], antigravity=True))
        self.assertFalse(draft_read_recorded(tools, files, ['sample.txt']))
        self.assertFalse(draft_read_recorded(tools, {'sample.txt': 'b' * 64}, ['sample.txt'], antigravity=True))
