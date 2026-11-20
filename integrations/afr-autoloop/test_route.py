import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import route

SHA = "a" * 64
EVENT = {"sha256": SHA, "attempt": 1, "generation": "initial"}


class RouteTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cfg = {key: self.root for key in ("state", "inbox", "vault")}
        self.cfg["approvals"] = self.root / "approvals.json"

    def authorize(self):
        route.write_json(self.cfg["approvals"], {"destination": "OpenAI Codex", "sha256": [SHA]})

    def test_local_only_refuses_processing_even_with_permission_file(self):
        self.authorize()
        self.cfg["local_only"] = True
        self.assertEqual(route.approvals(self.cfg), set())
        with patch.object(route, "afr") as invoke:
            self.assertEqual(route.process(self.cfg, EVENT)["state"], "not_authorized")
            invoke.assert_not_called()

    def test_retry_uses_new_stable_attempt_after_backoff(self):
        self.authorize()
        with patch.object(route, "afr", return_value={"ok":False, "error":"codex_timeout"}) as run:
            self.assertEqual(route.process(self.cfg, EVENT)["state"], "retry")
            self.assertEqual(route.process(self.cfg, EVENT)["state"], "replayed")
            self.assertIsNone(route.next_attempt(self.cfg, SHA))
            _, data = route.ledger(self.cfg, SHA)
            with patch.object(route.time, "time", return_value=data["retry_after"] + 1):
                self.assertEqual(route.next_attempt(self.cfg, SHA), (2, "initial"))
                self.assertEqual(route.process(self.cfg, dict(EVENT, attempt=2))["state"], "retry")
            self.assertEqual(run.call_count, 2)

    def test_revoked_permission_blocks_queued_job_until_reauthorized(self):
        with patch.object(route, "afr") as run:
            self.assertEqual(route.process(self.cfg, EVENT)["state"], "not_authorized")
            run.assert_not_called()
            self.authorize()
            run.return_value = {"ok": True, "sha256": SHA, "topics": 1, "board": "/private/path"}
            result = route.process(self.cfg, dict(EVENT, attempt=2))
            self.assertEqual(result["state"], "published")
            self.assertNotIn("board", result)
            self.assertIsNone(route.next_attempt(self.cfg, SHA))

    def test_scan_never_submits_during_directory_mutation(self):
        self.authorize()
        result = {"ok":True,"added":[],"pending":[{"sha256":SHA,"folder":"private"}],"errors":[]}
        with patch.object(route,"afr",return_value=result), patch.object(route,"fingerprint",side_effect=[b"before",b"after"]), patch.object(route,"post") as post:
            self.assertEqual(route.scan(self.cfg)["state"], "settling")
            post.assert_not_called()

    def test_rearm_keeps_old_hub_event_stale(self):
        self.authorize()
        route.write_json(self.root/(SHA + ".json"), {"state":"blocked","attempt":3})
        route.rearm(self.cfg, SHA)
        attempt, generation = route.next_attempt(self.cfg, SHA)
        self.assertEqual(attempt, 1)
        self.assertNotEqual(generation, "initial")
        with patch.object(route,"afr") as run:
            self.assertEqual(route.process(self.cfg,EVENT)["state"],"replayed")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
