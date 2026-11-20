import os
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from . import goal_team_files
from .goal_checks import MAX_WORKSPACE_FILE
from .goal_team_files import freeze_package, seed


def checksum(data):
    return sha256(data).hexdigest()


class GoalTeamFilesTest(unittest.TestCase):
    def setUp(self):
        holder = TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name).resolve()
        self.base = self.root / "base"
        self.base.mkdir()
        (self.base / "AGENTS.md").write_bytes(b"local sample instructions")
        (self.base / "sample.txt").write_bytes(b"initial")
        self.target = self.root / "candidate"

    def accepted(self, name, path, data):
        directory = self.root / name
        file = directory / path
        file.parent.mkdir(parents=True)
        file.write_bytes(data)
        return directory, {path: checksum(data)}

    def test_seed_freeze_and_merge_in_input_order(self):
        first = self.accepted("first", "sample.txt", b"first")
        second = self.accepted("second", "sample.txt", b"second")
        baseline = seed(self.base, self.target, [first, second])
        self.assertEqual(baseline, {
            "AGENTS.md": checksum(b"local sample instructions"),
            "sample.txt": checksum(b"second"),
        })
        (self.target / "sample.txt").write_bytes(b"third")
        (self.target / "out").mkdir()
        (self.target / "out/result.txt").write_bytes(b"result")
        (self.target / "empty").mkdir()
        frozen = self.root / "frozen"
        outputs = freeze_package(self.target, frozen, baseline, ["sample.txt", "out/result.txt"])
        self.assertFalse((frozen / "empty").exists())
        self.assertFalse((frozen / "AGENTS.md").exists())
        (self.target / "out/result.txt").write_bytes(b"late modification")
        merged = self.root / "merged"
        result = seed(self.base, merged, [first, (frozen, outputs)])
        self.assertEqual(result["out/result.txt"], checksum(b"result"))
        self.assertEqual((merged / "out/result.txt").read_bytes(), b"result")
        self.assertEqual((merged / "sample.txt").read_bytes(), b"third")
        self.assertEqual((self.base / "sample.txt").read_bytes(), b"initial")

    def test_accepted_snapshot_must_match_exactly(self):
        source, manifest = self.accepted("accepted", "sample.txt", b"accepted")
        for extra in ("sample.txt", "extra.txt"):
            with self.subTest(extra=extra):
                (source / extra).write_bytes(b"changed")
                with self.assertRaises(ValueError):
                    seed(self.base, self.target, [(source, manifest)])
                self.assertFalse(self.target.exists())
                if extra != "sample.txt":
                    (source / extra).unlink()
                (source / "sample.txt").write_bytes(b"accepted")

    def test_freeze_rejects_undeclared_changes(self):
        baseline = seed(self.base, self.target, [])
        frozen = self.root / "frozen"
        (self.target / "allowed.txt").write_bytes(b"yes")
        sample = self.target / "sample.txt"
        extra = self.target / "extra.txt"
        for change in (lambda: sample.unlink(), lambda: sample.write_bytes(b"changed"),
                       lambda: extra.write_bytes(b"new")):
            with self.subTest(change=change):
                change()
                with self.assertRaises(ValueError):
                    freeze_package(self.target, frozen, baseline, ["allowed.txt"])
                self.assertFalse(frozen.exists())
                sample.write_bytes(b"initial")
                if extra.exists():
                    extra.unlink()
        with self.assertRaises(ValueError):
            freeze_package(self.target, frozen, baseline, ["missing.txt"])
        self.assertFalse(frozen.exists())

    def test_dangerous_paths_and_prefix_collisions(self):
        first = self.accepted("first", "Data/one.txt", b"one")
        second = self.accepted("second", "data/two.txt", b"two")
        third = self.accepted("third", "Data", b"file")
        for inputs in ([first, second], [first, third]):
            with self.assertRaises(ValueError):
                seed(self.base, self.target, inputs)
        baseline = seed(self.base, self.target, [])
        for path in ("../escape", "/absolute", ".git/config", "sample.txt/child"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                freeze_package(self.target, self.root / "frozen", baseline, [path])

    def test_rejects_symlink_hardlink_and_oversize(self):
        path = self.base / "unexpected"
        path.symlink_to(self.base / "sample.txt")
        with self.assertRaises(ValueError):
            seed(self.base, self.target, [])
        path.unlink()
        os.link(self.base / "sample.txt", path)
        with self.assertRaises(ValueError):
            seed(self.base, self.target, [])
        path.unlink()
        with path.open("wb") as stream:
            stream.truncate(MAX_WORKSPACE_FILE + 1)
        with self.assertRaises(ValueError):
            seed(self.base, self.target, [])
        self.assertFalse(self.target.exists())

    def test_rejects_merged_size_and_existing_or_nested_destination(self):
        source = self.accepted("accepted", "other.txt", b"more")
        with patch.object(goal_team_files, "MAX_WORKSPACE_BYTES", 30):
            with self.assertRaises(ValueError):
                seed(self.base, self.target, [source])
        self.assertFalse(self.target.exists())
        self.target.mkdir()
        with self.assertRaises(FileExistsError):
            seed(self.base, self.target, [])
        with self.assertRaises(ValueError):
            seed(self.base, self.base / "nested", [])

    def test_rejects_symlinked_destination_parent(self):
        alias = self.root / "alias"
        alias.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(OSError):
            seed(self.base, alias / "candidate", [])
        self.assertFalse((self.base / "candidate").exists())


if __name__ == "__main__":
    unittest.main()
