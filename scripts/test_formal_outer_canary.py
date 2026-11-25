"""验证正式灰度的本机边界。"""
import unittest
from unittest.mock import patch

from formal_outer_canary import FIXTURE, OuterCanary, SYNTHETIC_ORDER, TEAM_FIXTURE, TEAM_ORDER, valid_command
from formal_outer_canary_wire import client_ticket, request_nonce, response_proof


class FormalOuterCanaryTests(unittest.TestCase):
    def test_gateway_sends_only_builtin_fixture(self):
        gateway = OuterCanary.__new__(OuterCanary)
        with patch.object(gateway, "forward") as forward:
            gateway.post(None, "/api/tasks", FIXTURE)
            forward.assert_called_once_with(None, "/api/tasks", SYNTHETIC_ORDER)
            forward.reset_mock()
            gateway.post(None, "/api/tasks", TEAM_FIXTURE)
            forward.assert_called_once_with(None, "/api/tasks", TEAM_ORDER)
            forward.reset_mock()
            gateway.post(Response(), "/api/tasks", {**FIXTURE, "goal": "private data"})
            forward.assert_not_called()

    def test_only_known_operations_and_machine_approval(self):
        ticket_id = "wb-" + "a" * 32
        request_id = "11111111-2222-4333-8444-101010101020"
        approval = dict(ticket_id=ticket_id, request_id=request_id, revision=1,
                        goal_revision=1, acceptance_revision=1)
        self.assertTrue(valid_command("approve", approval))
        self.assertTrue(valid_command("approve", {**approval, "require_signoff": True}))
        self.assertFalse(valid_command("approve", {**approval, "require_signoff": "true"}))
        direction = {**approval, "question_id": "b"*32, "question_revision": 1, "candidate_digest": "c"*64}
        self.assertTrue(valid_command("answer_preference", {**direction, "answer": {"option_id": "keep-draft", "text": ""}}))
        self.assertFalse(valid_command("answer_preference", {**direction, "answer": {"option_id": "keep-draft", "text": "free input"}}))
        self.assertFalse(valid_command("submit", {"ticket_id": ticket_id, "text": "free input"}))
        self.assertFalse(valid_command("return_artifact", {"ticket_id": ticket_id, "reason": "free input"}))
        self.assertFalse(valid_command("get", {"ticket_id": "../another-order"}))

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
        if status != 409:
            raise AssertionError((result, status))


if __name__ == "__main__":
    unittest.main()
