"""验证不同工单能并行，同一工单不重复领，慢单不会占掉所有名额。"""
import threading
import unittest
from unittest.mock import Mock

from .ticket_dispatcher import TeamDispatcher


class DispatchTest(unittest.TestCase):
    def test_slots_fairness_and_restart(self):
        ready = ['A', 'B', 'C']
        started = {ticket: threading.Event() for ticket in ready}
        release = {ticket: threading.Event() for ticket in ready}
        calls = []

        def consume(ticket, limit):
            calls.append((ticket, limit))
            started[ticket].set()
            release[ticket].wait(3)

        intake = Mock()
        intake.pending_team_tickets.side_effect = lambda: list(ready)
        intake.drain_ticket.side_effect = consume
        router = TeamDispatcher(intake)
        try:
            router.dispatch_ready()
            self.assertTrue(started['A'].wait(1) and started['B'].wait(1))
            router.dispatch_ready()
            self.assertFalse(started['C'].is_set())
            release['B'].set()
            router.active['B'].join(1)
            router.dispatch_ready()
            self.assertTrue(started['C'].wait(1))
            self.assertCountEqual(calls, [('A', 1), ('B', 1), ('C', 1)])
        finally:
            for gate in release.values():
                gate.set()
            for thread in router.active.values():
                thread.join(2)
        ready[:] = ['A']
        fresh = TeamDispatcher(intake)
        fresh.dispatch_ready()
        fresh.active['A'].join(2)
        self.assertEqual(calls.count(('A', 1)), 2)
