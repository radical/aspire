import unittest

from helpers import FakeClock
import pilot_state as state


class PilotStateTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.ledger = state.new_ledger()
        self.chain = state.adopt(self.ledger, 7, "pr", "NODE7")

    def start(self, identity, local=True):
        return state.reserve(self.ledger, self.chain, identity, self.clock(), local=local)

    def test_oversized_identity_rejected_before_counter_or_reservation_changes(self):
        before = state.render(self.ledger)
        with self.assertRaisesRegex(ValueError, "identity"):
            self.start("x" * 16385)
        self.assertEqual(before, state.render(self.ledger))

    def finish(self, operation, cost=1):
        state.settle_native(operation, cost)
        state.finish(operation, "failed")

    def test_two_failed_local_attempts_escalate_stickily(self):
        for index in range(2):
            operation = self.start(str(index))
            self.assertEqual("local", operation["lane"])
            self.finish(operation)
        third = self.start("third")
        self.assertEqual("cloud", third["lane"])
        self.assertEqual((2, 3), (self.chain["localAttempts"], self.chain["rounds"]))
        self.assertTrue(self.chain["escalated"])

    def test_ten_rounds_survive_new_head_and_child_adoption(self):
        self.chain["kind"] = "issue"
        for index in range(10):
            operation = self.start("head-" + str(index), local=False)
            self.finish(operation)
        state.bind_child(self.ledger, self.chain, 9, "CHILD9")
        self.assertIs(self.chain, state.find_chain(self.ledger, 9))
        with self.assertRaisesRegex(ValueError, "round"):
            self.start("eleventh")
        self.assertEqual(10, self.chain["rounds"])

    def test_replay_and_uncertain_send_never_allocate_again(self):
        operation = self.start("same", local=False)
        state.settle_native(operation, 2)
        state.reserve_worker(self.ledger, self.chain, operation, self.clock())
        state.sent(operation)
        state.finish(operation, "uncertain")
        self.assertIs(operation, self.start("same"))
        with self.assertRaisesRegex(ValueError, "pending"):
            self.start("new-head")
        self.assertEqual(1, state.worker_slots(self.ledger))
        self.assertEqual(1, self.chain["rounds"])

    def test_unknown_native_usage_retains_reservation_on_failure(self):
        operation = self.start("failure")
        state.settle_native(operation, None)
        state.finish(operation, "failed")
        self.assertEqual(30, state.chain_spend(self.chain))
        self.assertIsNone(operation["nativeActual"])

    def test_shared_rolling_budget_keeps_outstanding_reservations(self):
        first = self.start("one", local=False)
        state.settle_native(first, 10)
        state.reserve_worker(self.ledger, self.chain, first, self.clock())
        state.sent(first)
        second_chain = state.adopt(self.ledger, 8, "issue", "NODE8")
        second = state.reserve(self.ledger, second_chain, "two", self.clock(), local=False)
        state.settle_native(second, 10)
        state.reserve_worker(self.ledger, second_chain, second, self.clock())
        state.sent(second)
        third_chain = state.adopt(self.ledger, 10, "pr", "NODE10")
        with self.assertRaisesRegex(ValueError, "repository"):
            state.reserve(self.ledger, third_chain, "three", self.clock(), local=False)
        self.clock.advance(hours=25)
        self.assertEqual(980, state.repository_spend(self.ledger, self.clock()))
        with self.assertRaisesRegex(ValueError, "repository"):
            state.reserve(self.ledger, third_chain, "later", self.clock(), local=False)
        self.assertEqual(2, state.worker_slots(self.ledger))

    def test_no_send_releases_worker_slot_not_round_or_usage(self):
        operation = self.start("rejected", local=False)
        state.settle_native(operation, 4)
        state.reserve_worker(self.ledger, self.chain, operation, self.clock())
        state.finish(operation, "no-send")
        self.assertEqual((0, 1, 4), (state.worker_slots(self.ledger), self.chain["rounds"],
                                     state.chain_spend(self.chain)))

    def test_round_robin_cheap_sweep_skips_waits(self):
        other = state.adopt(self.ledger, 8, "issue", "NODE8")
        observations = {7: {"actionable": False}, 8: {"actionable": True}}
        self.assertIs(other, state.select(self.ledger, observations))
        observations[7]["actionable"] = True
        self.assertIs(self.chain, state.select(self.ledger, observations))

    def test_legacy_and_ambiguous_mapping_reject(self):
        with self.assertRaisesRegex(ValueError, "legacy"):
            state.adopt(self.ledger, 121, "pr", "LEGACY")
        other = state.adopt(self.ledger, 8, "issue", "NODE8")
        with self.assertRaisesRegex(ValueError, "mapped"):
            state.bind_child(self.ledger, other, 7, "NODE7")

    def test_closed_chain_retains_actual_cost_and_unknown_worker(self):
        operation = self.start("closed", local=False)
        state.settle_native(operation, 2)
        state.reserve_worker(self.ledger, self.chain, operation, self.clock())
        state.sent(operation)
        self.chain["state"] = "closed"
        self.assertEqual(500, state.chain_spend(self.chain))
        self.assertEqual(1, state.worker_slots(self.ledger))

    def test_body_round_trip_and_bound_fail_closed(self):
        rendered = state.render(self.ledger)
        self.assertEqual(self.ledger, state.parse(rendered))
        with self.assertRaises(ValueError):
            state.parse(rendered.replace('"rounds":0', '"rounds":-1'))
        self.ledger["unexpected"] = "x" * 65000
        with self.assertRaises(ValueError):
            state.render(self.ledger)


if __name__ == "__main__":
    unittest.main()
