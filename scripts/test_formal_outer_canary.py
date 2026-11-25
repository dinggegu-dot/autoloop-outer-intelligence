"""验证正式灰度的本机边界。"""
import unittest
from unittest.mock import patch

from formal_outer_canary import (
    FIXTURE, PREFIX, SYNTHETIC_ORDER, TEAM_FIXTURE, TEAM_ORDER, OuterCanary, valid_command, valid_custom_order,
)
from formal_outer_canary_wire import client_ticket, request_nonce, response_proof


REQUEST_ID = "11111111-2222-4333-8444-101010101020"
VERSION = dict(ticket_id="wb-" + "a" * 32, request_id=REQUEST_ID, revision=1)
BINDING = dict(VERSION, goal_revision=1, acceptance_revision=1)
CUSTOM_ORDER = dict(workflow="outer", name="自定义工单", goal="制作一份合成演示", acceptance="检查标题",
                    request_id=REQUEST_ID, allow_model_input=True)


class FormalOuterCanaryTests(unittest.TestCase):
    def test_gateway_preserves_builtins_and_requires_explicit_custom_consent(self):
        gateway = OuterCanary.__new__(OuterCanary)
        with patch.object(gateway, "forward") as forward:
            gateway.post(None, "/api/tasks", FIXTURE)
            forward.assert_called_once_with(None, "/api/tasks", SYNTHETIC_ORDER)
            forward.reset_mock()
            gateway.post(None, "/api/tasks", TEAM_FIXTURE)
            forward.assert_called_once_with(None, "/api/tasks", TEAM_ORDER)
            forward.reset_mock()
            for denied in ({**FIXTURE, "goal": "private data"}, {**CUSTOM_ORDER, "allow_model_input": "true"},
                           {**CUSTOM_ORDER, "allow_model_input": False}, {**SYNTHETIC_ORDER, "goal": "modified"}):
                with self.subTest(denied=denied):
                    self.assertEqual(gateway.post(Response(), "/api/tasks", denied)[1], 409)
                    forward.assert_not_called()
            self.assertEqual(gateway.post(Response(), "/api/tasks", {**CUSTOM_ORDER, "attachments": []})[1], 400)
            forward.assert_not_called()
            gateway.post(None, "/api/tasks", CUSTOM_ORDER)
            forward.assert_called_once_with(None, "/api/tasks", CUSTOM_ORDER)

    def test_custom_orders_use_exact_schema_and_limits(self):
        self.assertTrue(valid_custom_order(CUSTOM_ORDER))
        self.assertTrue(valid_custom_order(dict(CUSTOM_ORDER, team_review_mode="review_and_decide")))
        self.assertTrue(valid_custom_order(dict(CUSTOM_ORDER, goal="目" * 6000, acceptance="好" * 4000)))
        for invalid in (dict(CUSTOM_ORDER, goal=" \n"), dict(CUSTOM_ORDER, goal="目" * 6001),
                        dict(CUSTOM_ORDER, name="a" * 81), dict(CUSTOM_ORDER, acceptance="a" * 4001),
                        dict(CUSTOM_ORDER, goal="\ud800"), dict(CUSTOM_ORDER, goal="x\0y"),
                        dict(CUSTOM_ORDER, goal="\x01" * 6000, acceptance="\x01" * 4000),
                        dict(CUSTOM_ORDER, request_id=TEAM_ORDER["request_id"]),
                        dict(CUSTOM_ORDER, request_id="not-a-valid-uuid"),
                        dict(CUSTOM_ORDER, team_review_mode="custom"), dict(CUSTOM_ORDER, allow_model_input=1),
                        dict(CUSTOM_ORDER, original_file="/local/file"), dict(CUSTOM_ORDER, cwd="/local/folder")):
            with self.subTest(field_changes=[key for key, value in invalid.items() if CUSTOM_ORDER.get(key) != value], changed_lengths=[len(v) for v in invalid.values() if isinstance(v, str)]):
                self.assertFalse(valid_custom_order(invalid))

    def test_known_operations_keep_version_binding(self):
        self.assertTrue(valid_command("approve", BINDING))
        self.assertTrue(valid_command("approve", dict(BINDING, require_signoff=True)))
        self.assertFalse(valid_command("approve", dict(BINDING, require_signoff="true")))
        self.assertTrue(valid_command("pause", VERSION))
        self.assertTrue(valid_command("pause", BINDING))
        self.assertFalse(valid_command("pause", dict(VERSION, revision=True)))
        self.assertFalse(valid_command("stop", dict(VERSION, revision=2**53)))
        self.assertFalse(valid_command("get", {"ticket_id": "../another-order"}))
        self.assertFalse(valid_command("lock_blind_ballot", BINDING))

    def test_followup_free_text_requires_fresh_explicit_consent(self):
        direction = dict(BINDING, question_id="b"*32, question_revision=1, candidate_digest="c"*64)
        self.assertTrue(valid_command("answer_preference", {**direction, "answer": {"option_id": "keep-draft", "text": ""}}))
        self.assertTrue(valid_command("answer_preference", dict(direction, answer={"option_id": "keep-draft"})))
        self.assertFalse(valid_command("answer_preference", dict(direction, answer={"text": " "})))
        opinion = dict(direction, answer={"option_id": "revise", "text": "请调整次序"})
        followup = dict(VERSION, text="添加一个测试", question_id="q-" + "a" * 32,
                        replace_requirement_ids=["req-" + "b" * 32])
        artifact = dict(BINDING, candidate="a" * 32, artifact_sha256="b" * 64)
        self.assertTrue(valid_command("accept_artifact", artifact))
        gateway = OuterCanary.__new__(OuterCanary)
        for command, request in (("answer_preference", opinion), ("submit", followup),
                                 ("return_artifact", dict(artifact, reason="标题需要调整"))):
            with self.subTest(command=command), patch.object(gateway, "forward") as forward:
                self.assertEqual(gateway.post(Response(), PREFIX + command, request)[1], 400)
                self.assertEqual(gateway.post(Response(), PREFIX + command, dict(request, allow_model_input=1))[1], 400)
                forward.assert_not_called()
                allowed = dict(request, allow_model_input=True)
                gateway.post(None, PREFIX + command, allowed)
                forward.assert_called_once_with(None, "/api/outer/" + command, allowed)
                self.assertFalse(valid_command(command, dict(allowed, attachments=[])))
                self.assertFalse(valid_command(command, dict(allowed, revision=0)))
        self.assertFalse(valid_command("answer_preference", dict(opinion, allow_model_input=True,
                                                                 answer={"option_id": "revise", "text": "x" * 2001})))
        self.assertFalse(valid_command("submit", dict(followup, allow_model_input=True,
                                                     replace_requirement_ids=["req-" + "b" * 32] * 2)))
        self.assertFalse(valid_command("submit", dict(followup, allow_model_input=True, thread="existing-thread")))
        self.assertFalse(valid_command("return_artifact", dict(artifact, reason=" ", allow_model_input=True)))

    def test_budget_can_only_extend_current_mandate_by_supported_choices(self):
        request = dict(VERSION, mandate_revision=2, additional_minutes=15, additional_roles=5)
        self.assertTrue(valid_command("extend_team_budget", request))
        self.assertTrue(valid_command("extend_team_budget", dict(request, additional_minutes=0)))
        self.assertTrue(valid_command("extend_team_budget", dict(request, additional_roles=0)))
        for bad in (dict(request, additional_minutes=10), dict(request, additional_minutes=True),
                    dict(request, additional_minutes=0, additional_roles=0), dict(request, mandate_revision=0),
                    dict(request, reason="free input"), dict(request, goal_revision=1)):
            with self.subTest(bad=bad):
                self.assertFalse(valid_command("extend_team_budget", bad))

    def test_proof_binds_method_path_time_and_reply(self):
        key = "ab" * 32
        route = "/api/session"
        with patch("formal_outer_canary_wire.time.time", return_value=1800000000):
            ticket = client_ticket(key, "GET", route)
            nonce = request_nonce(key, "GET", route, ticket)
            self.assertEqual(nonce, ticket.split(".")[0])
            self.assertIsNone(request_nonce(key, "POST", route, ticket))
            self.assertIsNone(request_nonce(key, "GET", route + "?a=1", ticket))
            self.assertIsNone(request_nonce("ba" * 32, "GET", route, ticket))
        with patch("formal_outer_canary_wire.time.time", return_value=1800000021):
            self.assertIsNone(request_nonce(key, "GET", route, ticket))
        self.assertNotEqual(response_proof(key, nonce, 200, b"a"), response_proof(key, nonce, 200, b"b"))
        self.assertNotEqual(response_proof(key, nonce, 200, b"a"), response_proof(key, nonce, 403, b"a"))


class Response:
    def json_response(self, result, status):
        return result, status


if __name__ == "__main__":
    unittest.main()
