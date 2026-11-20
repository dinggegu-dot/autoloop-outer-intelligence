import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import process_guard

if __package__:
    from . import goal_codex
else:
    import goal_codex


_SESSION = "019abcde-1234-7890-8123-123456789abc"
_POPEN = subprocess.Popen


def _events(result):
    return [
        {"type": "thread.started", "thread_id": _SESSION},
        {"type": "item.completed", "item": {
            "type": "agent_message", "text": json.dumps(result),
        }},
        {"type": "turn.completed", "usage": {"input_tokens": 3}},
    ]


class GoalCodexTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Path(tmp.name, "workspace")
        self.output = Path(tmp.name, "output")
        self.workspace.mkdir()
        self.output.mkdir()
        self.assignment = {
            "id": "assignment-1", "ticket": "ticket-1", "phase": "plan",
            "goal": "Inspect a sample", "acceptance": ["check sample"],
            "mandate": {}, "plan": None, "step": None, "context": {},
            "checkpoint": {"previous": "sample"}, "max_seconds": 3,
            "model": "gpt-6.2-sol",
        }

    def _invoke_with_child(self, script):
        fake = self.output / "fake-codex"
        gate_check = (
            "import json, os\n"
            "with open(" + repr(str(self.output / "process.json")) + ") as stream:\n"
            "    record = json.load(stream)\n"
            "assert record['state'] == 'running' and record['version'] == 1\n"
            "assert record['pid'] == os.getpid() and record['pgid'] == os.getpgrp()\n"
            "assert record['birth'] and record['processes'][0]['birth'] == record['birth']\n"
        )
        fake.write_text("#!" + sys.executable + "\n" + gate_check + script + "\n")
        fake.chmod(0o700)

        def spawn(command, **kwargs):
            if str(goal_codex._GATE) not in command:
                return _POPEN(command, **kwargs)
            self.argv = ["codex", *command[4:]]
            self.env = kwargs["env"]
            self.cwd = kwargs["cwd"]
            schema = Path(command[command.index("--output-schema") + 1])
            self.schema = json.loads(schema.read_text())
            self.child = _POPEN(command, **kwargs)
            return self.child

        def snapshot():
            # 桌面沙箱禁用 ps。保留真实进程组和信号，只替换进程表的观测。
            if self.child.poll() is not None:
                return {}
            pid = self.child.pid
            try:
                group = os.getpgid(pid)
            except ProcessLookupError:
                return {}
            return {pid: {"pid": pid, "parent": os.getpid(), "group": group,
                          "birth": "synthetic process birth", "state": "S"}}

        try:
            with patch.object(goal_codex.shutil, "which", return_value=str(fake)), patch.object(
                goal_codex.subprocess, "Popen", side_effect=spawn
            ), patch.object(goal_codex, "processes", side_effect=snapshot), patch.object(
                process_guard, "processes", side_effect=snapshot
            ):
                return goal_codex.invoke(self.assignment, self.workspace, self.output)
        finally:
            fake.unlink()

    def _emit(self, events):
        lines = "\n".join(json.dumps(event) for event in events)
        return self._invoke_with_child(
            "import json, sys\n"
            "payload = json.loads(sys.stdin.buffer.read().split(b'\\n', 1)[1])\n"
            "assert payload == " + repr(self.assignment) + "\n"
            "print(" + repr(lines) + ")"
        )

    def test_optional_loopback_proxy_is_cli_only(self):
        self.assignment["phase"] = "execute"
        result = {"summary": "Sample checked", "artifacts": ["sample.txt"]}
        with patch.dict(os.environ, {"AUTOLOOP_OUTER_PROXY_PORT": "56789", "HTTPS_PROXY": "untrusted-source"}):
            self._emit(_events(result))
        self.assertEqual(self.env["HTTPS_PROXY"], "http://127.0.0.1:56789")
        self.assertNotIn("AUTOLOOP_OUTER_PROXY_PORT", self.env)
        self.assertNotIn("56789", " ".join(self.argv))
        self.assertIn('shell_environment_policy.exclude=["*PROXY","AUTOLOOP_*"]', self.argv)
        self.assertIn("sandbox_workspace_write.network_access=false", self.argv)
        with patch.dict(os.environ, {"AUTOLOOP_OUTER_PROXY_PORT": "remote:8080"}), self.assertRaisesRegex(
                goal_codex.GoalCodexError, "Invalid loopback"):
            goal_codex.invoke(self.assignment, self.workspace, self.output)

    def test_all_phases(self):
        plan = {"steps": [{"objective": "Check sample", "checks": ["Read sample"]}],
                "summary": "One step"}
        execute = {"summary": "Sample checked", "artifacts": ["sample.txt"]}
        review = {"passed": True, "findings": ["Sample checked"]}
        for phase, result, writable in (
            ("plan", plan, False), ("replan", plan, False), ("review", review, False),
            ("execute", execute, True), ("repair", execute, True),
        ):
            with self.subTest(phase=phase), patch.dict(os.environ, {
                "OPENAI_API_KEY": "synthetic-key", "HTTPS_PROXY": "synthetic-proxy",
                "CODEX_HOME": "/synthetic/codex-home",
            }):
                self.assignment["phase"] = phase
                receipt = self._emit(_events(result))
                self.assertEqual(receipt["result"], result)
                self.assertEqual(receipt["session"], _SESSION)
                self.assertEqual(receipt["usage"], {"input_tokens": 3})
                self.assertEqual(receipt["model"], "gpt-6.2-sol")
                self.assertLessEqual(receipt["started_ns"], receipt["ended_ns"])
                self.assertEqual(self.cwd, self.workspace)
                self.assertEqual(self.argv[:4], ["codex", "--ask-for-approval", "never", "exec"])
                self.assertEqual(self.argv[self.argv.index("--sandbox") + 1],
                                 "workspace-write" if writable else "read-only")
                self.assertEqual("shell_tool" in self.argv, phase in ("plan", "replan"))
                self.assertTrue({
                    "--ephemeral", "--json", "--ignore-user-config", "--ignore-rules",
                    "hooks", 'web_search="disabled"', "sandbox_workspace_write.network_access=false",
                }.issubset(self.argv))
                self.assertEqual(self.env["CODEX_HOME"], "/synthetic/codex-home")
                self.assertNotIn("OPENAI_API_KEY", self.env)
                self.assertNotIn("HTTPS_PROXY", self.env)
                self.assertEqual(set(self.schema["required"]), set(result))
                self.assertEqual(list(self.output.iterdir()), [])

    def test_invalid_outputs(self):
        self.assignment["phase"] = "review"
        good = _events({"passed": True, "findings": []})
        for events in (
            good[1:], good[:-1], _events({"passed": 1, "findings": []}),
            good + [{"type": "turn.failed"}],
        ):
            with self.subTest(events=events), self.assertRaises(goal_codex.GoalCodexError) as failure:
                self._emit(events)
            self.assertEqual(failure.exception.code, "invalid_output")

    def test_nonzero_exit_without_diagnostic_is_unknown(self):
        with self.assertRaises(goal_codex.GoalCodexError) as failure:
            self._invoke_with_child("import sys; sys.stderr.write('private'); sys.exit(7)")
        self.assertEqual(failure.exception.code, "nonzero_exit")
        self.assertEqual(failure.exception.exit_code, 7)
        self.assertEqual(failure.exception.categories, ("unknown",))
        self.assertNotIn("private", str(failure.exception))
        self.assertEqual(list(self.output.iterdir()), [])

    def test_nonzero_exit_reports_only_fixed_diagnostic_categories(self):
        event = {"type": "turn.failed", "error": {
            "message": "model requested-private-model is not supported",
            "code": "unsupported_model",
        }}
        agent_message = _events({"summary": "sandbox denied"})[1]
        with self.assertRaises(goal_codex.GoalCodexError) as failure:
            self._invoke_with_child(
                "import sys\n"
                f"print({json.dumps(event)!r})\n"
                f"print({json.dumps(agent_message)!r})\n"
                "sys.stderr.write('401 Unauthorized synthetic-secret; request timed out; "
                "429 Too Many Requests; connection reset')\n"
                "sys.exit(1)"
            )
        self.assertEqual(failure.exception.categories,
                         ("account", "model", "network", "network_timeout", "rate_limit"))
        self.assertEqual(str(failure.exception),
                         "Codex CLI exited with status 1 (account, model, network, network_timeout, rate_limit)")
        self.assertEqual(list(self.output.iterdir()), [])

    def test_sandbox_error_event_is_classified(self):
        stdout = b'{"type":"error","message":"sandbox initialization failed at /private/path"}'
        self.assertEqual(goal_codex._exit_categories(stdout, b""), ("sandbox",))

    def test_timeout_kills_group_and_reaps_child(self):
        self.assignment["max_seconds"] = 1
        with self.assertRaises(goal_codex.GoalCodexError) as failure:
            self._invoke_with_child("import time; time.sleep(30)")
        self.assertEqual(failure.exception.code, "timeout")
        self.assertEqual(self.child.returncode, -signal.SIGKILL)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_signals_cancel_without_a_business_receipt(self):
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signum=signum):
                old_handler = signal.getsignal(signum)
                with self.assertRaises(SystemExit) as cancelled:
                    self._invoke_with_child(
                        f"import time; os.kill(os.getppid(), {int(signum)}); time.sleep(30)"
                    )
                self.assertEqual(cancelled.exception.code, 128 + signum)
                self.assertEqual(self.child.returncode, -signal.SIGKILL)
                self.assertIs(signal.getsignal(signum), old_handler)
                self.assertEqual(list(self.output.iterdir()), [])

    def test_pending_record_blocks_process_launch(self):
        marker = self.output / "process.json"
        marker.write_text('{"state":"cleanup_unconfirmed"}')
        with patch.object(goal_codex.shutil, "which", return_value="/fake/codex"), patch.object(
            goal_codex.subprocess, "Popen"
        ) as spawn:
            with self.assertRaises(goal_codex.GoalCodexError) as failure:
                goal_codex.invoke(self.assignment, self.workspace, self.output)
        self.assertEqual(failure.exception.code, "process_pending")
        self.assertEqual(json.loads(marker.read_text())["state"], "cleanup_unconfirmed")
        spawn.assert_not_called()

    def test_failed_identity_publication_does_not_release_gate(self):
        marker = self.workspace / "started"
        with patch.object(goal_codex, "_publish", side_effect=OSError("disk failure")):
            with self.assertRaises(goal_codex.GoalCodexError) as failure:
                self._invoke_with_child(f"open({str(marker)!r}, 'w').close()")
        self.assertEqual(failure.exception.code, "process_error")
        self.assertFalse(marker.exists())
        self.assertIsNotNone(self.child.returncode)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_cleanup_failure_retains_identity(self):
        original_stop = goal_codex._stop

        def fail_after_stop(process, tree):
            original_stop(process, tree)
            raise OSError("synthetic cleanup proof failure")

        with patch.object(goal_codex, "_stop", side_effect=fail_after_stop):
            with self.assertRaises(goal_codex.GoalCodexError) as failure:
                self._emit(_events({"steps": [], "summary": "Sample"}))
        self.assertEqual(failure.exception.code, "cleanup_unconfirmed")
        record = json.loads((self.output / "process.json").read_text())
        self.assertEqual(record["state"], "cleanup_unconfirmed")
        self.assertEqual(record["pid"], self.child.pid)
        self.assertEqual(record["pgid"], self.child.pid)
        self.assertTrue(record["birth"])

    def test_bad_assignment_never_starts_process(self):
        with patch.object(goal_codex.subprocess, "Popen") as spawn:
            for field, invalid in (("max_seconds", True), ("phase", "unknown"), ("model", "other")):
                assignment = dict(self.assignment, **{field: invalid})
                with self.assertRaises(goal_codex.GoalCodexError) as failure:
                    goal_codex.invoke(assignment, self.workspace, self.output)
                self.assertEqual(failure.exception.code, "invalid_assignment")
            spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
