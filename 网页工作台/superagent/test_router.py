import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import router


class FakeAutoloop:
    def __init__(self):
        self.posted = []
        self.state = "claimed"

    def submit(self, row):
        self.posted.append(row["id"])
        return len(self.posted)

    def outcome(self, event):
        return {"run": event, "state": self.state}


class CoordinatorTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runtime = Path(folder.name)
        self.frozen = router.plan()
        self.approved = router.digest(self.frozen)
        router.prepare(self.runtime, self.approved)
        self.hub = FakeAutoloop()

    def attempt(self, cap):
        with router.ledger(self.runtime) as db:
            return router.latest(db)[cap]

    def claim(self, cap, run=None, key=None):
        row = self.attempt(cap)
        key = key or row["id"]
        return router.claim(self.runtime, cap, {"events":[{
            "id":key,"type":"team."+cap,"source":"team","subject":key
        }]}, run or row["event"])

    def save_result(self, cap, result="{}", status="succeeded"):
        row = self.attempt(cap)
        with router.ledger(self.runtime) as db:
            db.execute("UPDATE assignment SET state=?,result=? WHERE id=?",(status,result,row["id"]))

    def test_two_distinct_branches_and_approval_conflict(self):
        with self.assertRaises(ValueError):
            router.prepare(self.runtime, "unapproved")
        router.schedule(self.runtime, self.hub)
        self.assertEqual(len(set(self.hub.posted)),2)
        self.assertEqual(self.attempt("review")["state"],"pending")
        with self.assertRaises(ValueError):
            self.claim("aggregate",key=self.attempt("reconcile")["id"])
        with self.assertRaises(ValueError):
            self.claim("aggregate",run=999)
        self.assertIsNotNone(self.claim("aggregate"))
        with self.assertRaises(ValueError):
            self.claim("aggregate")
        self.save_result("aggregate")
        self.assertIsNone(self.claim("aggregate"))
        router.schedule(self.runtime, self.hub)
        self.assertEqual(len(self.hub.posted),2)
        self.claim("reconcile")
        self.save_result("reconcile")
        router.schedule(self.runtime, self.hub)
        self.assertEqual(len(self.hub.posted),3)
        _, inputs, _ = self.claim("review")
        self.assertEqual(inputs["reports_digest"],router.digest(inputs["reports"]))

    def test_unknown_refuses_retry_and_terminal_retries_only_failed_branch(self):
        router.schedule(self.runtime,self.hub)
        self.claim("aggregate")
        self.save_result("aggregate")
        self.claim("reconcile")
        self.save_result("reconcile",status="failed")
        old = self.attempt("reconcile")
        self.hub.state = "unknown"
        with self.assertRaises(RuntimeError):
            router.retry_failed(self.runtime,self.hub,"reconcile",self.approved)
        self.hub.state = "done"
        router.retry_failed(self.runtime,self.hub,"reconcile",self.approved)
        self.assertEqual(self.attempt("aggregate")["attempt"],1)
        self.assertEqual(self.attempt("reconcile")["attempt"],2)
        router.schedule(self.runtime,self.hub)
        with self.assertRaises(ValueError):
            self.claim("reconcile",run=old["run"],key=old["id"])

    def test_review_boolean_does_not_override_wrong_recalculation(self):
        router.schedule(self.runtime, self.hub)
        for cap in ("aggregate", "reconcile"):
            self.claim(cap)
            self.save_result(cap)
        router.schedule(self.runtime, self.hub)
        row = self.attempt("review")
        context = self.runtime / "review-context.json"
        context.write_text(router.encode({"events":[{
            "id": row["id"], "source": "team", "type": "team.review", "subject": row["id"]
        }]}))
        wrong = {"passed":True,"findings":["已复算"],"counts":[],"valid":5,"total":16,"rejected":["r4","r5"]}
        with patch.dict(os.environ,{"AGENT_AUTOLOOP_CONTEXT":str(context),"AGENT_AUTOLOOP_RUN":str(row["run"])}):
            with patch("codex_worker.invoke",return_value=(wrong,{})), patch("builtins.print"):
                router.worker(self.runtime,"review")
        self.assertEqual(self.attempt("review")["state"],"failed")

    def test_same_ticket_cannot_switch_scheduling_mode(self):
        with self.assertRaisesRegex(RuntimeError,"切换"):
            router.prepare(self.runtime,self.approved,sequential=True)

    def test_recover_reposting_same_id_after_event_ack_loss(self):
        with router.ledger(self.runtime) as db:
            db.execute("UPDATE assignment SET state='dispatching' WHERE capability='aggregate'")
        row = self.attempt("aggregate")
        with self.assertRaises(router.DispatchBindingPending):
            self.claim("aggregate",run=1)
        router.schedule(self.runtime,self.hub)
        self.assertIn(row["id"],self.hub.posted)
        self.assertEqual(self.attempt("aggregate")["event"],1)


if __name__ == "__main__":
    unittest.main()
