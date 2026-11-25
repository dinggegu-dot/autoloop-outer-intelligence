import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import recover_goal_team_pilot as recovery


class GoalTeamColdRecoveryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_snapshot_and_rehearsal_are_independent_copies(self):
        source = self.root / 'source'
        backup = self.root / 'backup'
        source.mkdir()
        backup.mkdir()
        artifact = source / 'artifact'
        artifact.write_text('synthetic')
        snapshot, rehearsal, baseline = recovery.copy_snapshot(source, backup)
        (rehearsal / 'artifact').write_text('synthetic changed')
        self.assertEqual(recovery.tree_content(snapshot), baseline)
        self.assertEqual(recovery.tree_content(source), baseline)
        self.assertNotEqual(recovery.tree_content(rehearsal), baseline)

    def test_snapshot_refuses_a_link_to_other_data(self):
        source = self.root / 'source'
        source.mkdir()
        other = self.root / 'outside'
        other.write_text('outside the fixture')
        (source / 'link').symlink_to(other)
        with self.assertRaisesRegex(RuntimeError, '符号链接'):
            recovery.tree_content(source)

    def test_read_database_after_shutdown_is_read_only(self):
        path = self.root / 'workbench.sqlite'
        original = self.root / 'checkpointed.sqlite'
        db = sqlite3.connect(original)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE record(value INTEGER)')
            db.execute('INSERT INTO record VALUES(1)')
            db.commit()
            self.assertEqual(db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone(), (0, 0, 0))
        finally:
            db.close()
        shutil.copy2(original, path)
        self.assertFalse(Path(str(path)+'-wal').exists())
        with recovery.read_db(path) as query:
            self.assertEqual(query.execute('SELECT value FROM record').fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                query.execute('INSERT INTO record VALUES(2)')
        self.assertFalse(Path(str(path)+'-shm').exists())

    def test_read_preserved_wal_contains_committed_rows(self):
        source = self.root / 'source'
        target = self.root / 'copy'
        source.mkdir()
        target.mkdir()
        db = sqlite3.connect(source / 'hub.db')
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA wal_autocheckpoint=0')
            db.execute('CREATE TABLE record(value INTEGER)')
            db.commit()
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            db.execute('INSERT INTO record VALUES(2)')
            db.commit()
            for file in source.iterdir():
                shutil.copy2(file, target/file.name)
            self.assertGreater((target/'hub.db-wal').stat().st_size, 0)
            with recovery.read_db(target/'hub.db') as query:
                self.assertEqual(query.execute('SELECT value FROM record').fetchone()[0], 2)
        finally:
            db.close()

    def test_stopped_ledger_rejects_unconfirmed_role_process(self):
        source = self.root / 'autoloop-goal-team-fixture'
        source.mkdir(mode=0o700)
        (source/'.goal-team-pilot').touch()
        (source/'.goal-pilot').write_text('43754\n')
        (source/'instance.json').write_text(json.dumps({
            'phase':'stopped', 'instance':str(source), 'workbench':str(source/'workbench'),
            'board':'http://127.0.0.1:43753', 'hub_port':43754,
        }))
        role = source/'workbench'/'goal-attempts'/'ticket'/'assignment'/'role-1'
        role.mkdir(parents=True)
        (role/'process.json').write_text(json.dumps({'version':1, 'state':'running'}))
        with patch.object(recovery, 'ISOLATED', self.root), patch.object(recovery, '_unoccupied'):
            with self.assertRaisesRegex(RuntimeError, '旧角色'):
                recovery.check_stopped(source)


if __name__ == '__main__':
    unittest.main()
