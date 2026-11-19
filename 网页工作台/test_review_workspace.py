"""审核副本回归：真实文件操作，不调用模型。"""
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from task_runner import Runner, RESULT_SCHEMA, review_snapshot, review_fingerprint, fingerprint
from workbench import Store, Problem


class ReviewWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root)
        task, _ = self.store.create({'name':'审核副本测试','goal':'隔离检查','request_id':str(uuid.uuid4())})
        self.runner = Runner(self.store, task['id'], 1, {})
        self.source = self.store.workspace(task['id'])
        (self.source/'answer.txt').write_text('original')

    def test_snapshot_is_independent_and_rejects_symlinks(self):
        copy = self.root/'copy'
        before = review_snapshot(self.source, copy)
        self.assertEqual(before, review_fingerprint(copy))
        (copy/'answer.txt').write_text('changed')
        self.assertEqual((self.source/'answer.txt').read_text(), 'original')
        (self.source/'external').symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(Problem, '符号链接'):
            review_snapshot(self.source, self.root/'linked-copy')

    def test_review_covers_dependencies_paths_modes_and_new_links(self):
        dependencies=self.source/'node_modules';dependencies.mkdir()
        candidate=dependencies/'a';candidate.write_text('bc')
        before=review_fingerprint(self.source)
        candidate.rename(dependencies/'ab');(dependencies/'ab').write_text('c')
        self.assertNotEqual(before,review_fingerprint(self.source))
        before=review_fingerprint(self.source)
        (dependencies/'ab').chmod(0o700)
        self.assertNotEqual(before,review_fingerprint(self.source))
        (self.source/'conftest.py').symlink_to(dependencies/'ab')
        with self.assertRaisesRegex(Problem,'符号链接'):
            review_fingerprint(self.source)

    def run_review(self, mutate=False):
        def local_check(command, env, prompt, directory, on_line, **kwargs):
            copy = kwargs['cwd']
            self.assertNotEqual(copy, self.source)
            self.assertEqual(command[command.index('-C')+1], str(copy))
            self.assertEqual(command[command.index('--sandbox')+1], 'workspace-write')
            self.assertIn('sandbox_workspace_write.network_access=false', command)
            self.assertIn('禁止修补代码', prompt)
            with tempfile.TemporaryDirectory(dir=copy) as scratch:
                (Path(scratch)/'sample.txt').write_text('test sample')
            if mutate:
                (copy/'answer.txt').write_text('modified candidate')
            result={'status':'completed','summary':'检查','checks':['实际读回'],'artifacts':['answer.txt'],'question':'','requested_action':''}
            Path(command[command.index('--output-last-message')+1]).write_text(json.dumps(result))
            return 0
        with patch.object(self.runner, 'run_process', side_effect=local_check):
            return self.runner.codex('review', '审核', RESULT_SCHEMA, 1)

    def test_temporary_test_files_allowed_original_unchanged(self):
        before = fingerprint(self.source)
        self.assertEqual(self.run_review()['status'], 'completed')
        self.assertEqual(before, fingerprint(self.source))

    def test_changed_candidate_rejected_even_with_pass_receipt(self):
        with self.assertRaisesRegex(Problem, '拒绝本次验收'):
            self.run_review(mutate=True)
        self.assertEqual((self.source/'answer.txt').read_text(), 'original')


if __name__ == '__main__':
    unittest.main()
