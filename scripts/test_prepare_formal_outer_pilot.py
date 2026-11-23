from pathlib import Path
import shutil
import tempfile
import unittest

from prepare_formal_outer_pilot import prepare, source_only


class FormalPilotCopyTest(unittest.TestCase):
    def test_source_copy_skips_deployed_runtime_and_links(self):
        with tempfile.TemporaryDirectory() as root:
            formal = Path(root, 'deployed')
            target = Path(root, 'isolated')
            for name in ('server.py', 'afr_handoff.js', 'assets/map.png', 'superagent/goal_engine.py',
                         'afr-handoff.local.json', 'runtime-location', '.runtime/tasks.sqlite3',
                         'snapshot.json', 'legacy_snapshot.py', 'superagent/test_goal_engine.py'):
                path = formal / name
                path.parent.mkdir(exist_ok=True, parents=True)
                path.write_text('fixture')
            (formal / 'assets' / 'linked.png').symlink_to(formal / 'afr-handoff.local.json')
            shutil.copytree(formal, target, ignore=source_only(formal))
            copied = {path.relative_to(target).as_posix() for path in target.rglob('*') if path.is_file()}
            self.assertEqual(copied, {'server.py', 'afr_handoff.js', 'assets/map.png', 'superagent/goal_engine.py'})

    def test_core_file_cannot_be_a_link(self):
        with tempfile.TemporaryDirectory() as root:
            formal = Path(root, 'deployed')
            formal.mkdir()
            (formal / 'server.py').symlink_to(Path(root, 'outside.py'))
            with self.assertRaisesRegex(ValueError, '包含链接'):
                prepare(formal, Path(root, 'isolated'))
