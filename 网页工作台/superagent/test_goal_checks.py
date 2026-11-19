import copy
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from superagent import goal_checks


def contract(**overrides):
    check = {
        "id": "reads-fixture", "script": "tools/report.py",
        "args": ["sample.txt"], "files": {"sample.txt": "synthetic\n"},
        "expected": {"text": "SYNTHETIC"},
    }
    check.update(overrides)
    return {"version": 1, "stages": [{"id": "report", "checks": [check]}]}


def stage(**overrides):
    return contract(**overrides)["stages"][0]


class NormalizeTests(unittest.TestCase):
    def test_canonical_detached_and_accepts_scalar_expected(self):
        original = contract(expected=False, files={"z.txt": "z", "a.txt": "a"})
        normalized = goal_checks.normalize(original)
        self.assertEqual(goal_checks.normalize(normalized), normalized)
        check = normalized["stages"][0]["checks"][0]
        self.assertEqual(list(check["files"]), ["a.txt", "z.txt"])
        self.assertIs(check["expected"], False)
        original["stages"][0]["checks"][0]["files"]["a.txt"] = "changed"
        self.assertEqual(check["files"]["a.txt"], "a")

    def test_rejects_paths_conflicts_secrets_and_non_json(self):
        for settings in (
            {"script": "../report.py"}, {"script": "/report.py"}, {"script": "tool.sh"},
            {"args": ["--input=/private/file"]}, {"args": ["dir/../sample.txt"]},
            {"args": ["https://example.test/file"]}, {"args": ["--password", "synthetic"]},
            {"files": {"tools/REPORT.py": "conflict"}},
            {"files": {"a": "file", "a/b": "nested"}},
            {"files": {"sample.txt": "password=synthetic"}},
            {"files": {"sample.txt": "x" * (goal_checks.MAX_TEXT_BYTES + 1)}},
            {"expected": {"password": "synthetic"}}, {"expected": float("nan")},
        ):
            with self.subTest(settings=tuple(settings)), self.assertRaises(ValueError):
                goal_checks.normalize(contract(**settings))

    def test_rejects_duplicates_across_stages_and_exact_version_type(self):
        original = contract()
        other = copy.deepcopy(original["stages"][0])
        other["id"] = "other"
        original["stages"].append(other)
        with self.assertRaisesRegex(ValueError, "globally unique"):
            goal_checks.normalize(original)
        original = contract()
        original["version"] = True
        with self.assertRaises(ValueError):
            goal_checks.normalize(original)

    def test_rejects_fixture_conflict_with_another_stage_script(self):
        original = contract(files={"OTHER.py": "fixture"})
        original["stages"].append({"id": "second", "checks": [
            stage(id="second-check", script="other.py")["checks"][0]
        ]})
        with self.assertRaisesRegex(ValueError, "overlaps a contract script"):
            goal_checks.normalize(original)

    def test_strict_output_discards_order_but_rejects_duplicate_keys_and_nan(self):
        self.assertEqual(goal_checks._strict_output(b'{"z": 1, "a": true}'), '{"a":true,"z":1}')
        for invalid in (b'{"a":1, "a":1}', b'NaN', b'{} {}', b'\xff'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                goal_checks._strict_output(invalid)


class VerificationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / "workspace"
        (self.workspace / "tools").mkdir(parents=True)
        self.script = self.workspace / "tools" / "report.py"
        self.script.write_text(
            "import json, sys\nfrom pathlib import Path\nfrom support import format_text\n"
            "print(json.dumps({'text': format_text(Path(sys.argv[1]).read_text())}))\n",
            encoding="utf-8",
        )
        (self.workspace / "support.py").write_text(
            "def format_text(value):\n    return value.strip().upper()\n", encoding="utf-8"
        )

    def test_rejects_workspace_symlink_before_loading_policy(self):
        (self.workspace / "link").symlink_to(self.root)
        with patch.object(goal_checks, "_policy") as load:
            result = goal_checks.verify(self.workspace, stage())
        self.assertFalse(result["passed"])
        self.assertIn("symbolic link", result["checks"][0]["detail"])
        load.assert_not_called()

    def test_rejects_fixture_overwrite_before_starting_sandbox(self):
        (self.workspace / "SAMPLE.TXT").write_text("original", encoding="utf-8")
        with patch.object(goal_checks, "_policy") as load:
            result = goal_checks.verify(self.workspace, stage())
        self.assertFalse(result["passed"])
        self.assertIn("overlaps a workspace artifact", result["checks"][0]["detail"])
        load.return_value._run.assert_not_called()

    def test_sandbox_failure_runs_only_probe(self):
        with patch.object(goal_checks, "_policy") as load:
            policy = load.return_value
            policy._seatbelt.return_value = ("/python", "(deny default)")
            policy._run.side_effect = RuntimeError("sandbox_apply: Operation not permitted")
            result = goal_checks.verify(self.workspace, stage())
        self.assertFalse(result["passed"])
        self.assertIn("sandbox_apply: Operation not permitted", result["checks"][0]["detail"])
        self.assertEqual(policy._run.call_count, 1)
        self.assertIs(policy._run.call_args.args[-1][1], policy.PROBE)

    @unittest.skipUnless(os.environ.get("GOAL_CHECKS_REAL_SEATBELT") == "1",
                         "requires a host session that can apply macOS Seatbelt")
    def test_real_seatbelt_local_imports_and_wrong_result(self):
        good = goal_checks.verify(self.workspace, stage())
        self.assertTrue(good["passed"], good)
        bad = goal_checks.verify(self.workspace, stage(expected={"text": "wrong"}))
        self.assertFalse(bad["passed"])
        self.assertIn("did not match", bad["checks"][0]["detail"])
        self.assertFalse((self.workspace / "sample.txt").exists())

    @unittest.skipUnless(os.environ.get("GOAL_CHECKS_REAL_SEATBELT") == "1",
                         "requires a host session that can apply macOS Seatbelt")
    def test_real_seatbelt_denies_write_and_enforces_timeout(self):
        self.script.write_text(
            "from pathlib import Path\nPath('side-effect').write_text('no')\nprint('null')\n",
            encoding="utf-8",
        )
        denied = goal_checks.verify(self.workspace, stage(expected=None))
        self.assertFalse(denied["passed"])
        self.assertIn("execution 1 failed in Seatbelt", denied["checks"][0]["detail"])
        self.assertFalse((self.workspace / "side-effect").exists())
        self.script.write_text("while True:\n    pass\n", encoding="utf-8")
        timed_out = goal_checks.verify(self.workspace, stage(expected=None))
        self.assertFalse(timed_out["passed"])
        self.assertIn("3-second limit", timed_out["checks"][0]["detail"])


if __name__ == "__main__":
    unittest.main()
