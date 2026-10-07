from contextlib import redirect_stdout
import io
import json
import unittest
from unittest.mock import patch

from helpers import WorkspaceTest, reconciliation_evidence
import local
import pilot
import pilot_binding as bindings
import pilot_state as state
import test_pilot_github as github_tests
import test_pilot_tracked_only as tracked_tests


class WorkerBillingTests(WorkspaceTest, unittest.TestCase):
    def worker(self, outcome="completed", *, binding=bindings.FORK):
        fixture = tracked_tests.TrackedOnlyTests()
        api, transport = fixture.api(binding)
        chain, operation, task = fixture.seed_worker(api, transport)
        task["state"] = task["sessions"][0]["state"] = outcome
        return fixture, api, transport, chain, operation, task

    def test_verified_terminal_outcomes_finish_lifecycle_without_refunding_unknown_costs(self):
        for outcome in sorted(state.TERMINAL):
            with self.subTest(outcome=outcome):
                _, api, transport, chain, operation, task = self.worker(outcome)
                api.reconcile_workers()
                self.assertEqual("completed" if outcome == "completed" else "failed", operation["state"])
                self.assertEqual(outcome, operation["workerState"])
                self.assertIsNone(operation["workerActual"])
                self.assertEqual((2, 498), (operation["nativeActual"], operation["workerReserved"]))
                self.assertFalse(state.pending(chain))
                self.assertTrue(state.worker_billing_pending(chain))
                self.assertEqual(500, state.chain_spend(chain))
                with self.assertRaisesRegex(ValueError, "chain credit allowance exhausted"):
                    state.reserve(api.ledger, chain, "new round", api.clock(), local=False)
                self.assertEqual(1, chain["rounds"])
                self.assertEqual([], [write for write in transport.writes if write[1].endswith("/tasks")])

    def test_completed_result_bookkeeping_and_repeated_sweeps_do_not_wait_for_usage(self):
        fixture, api, transport, chain, operation, task = self.worker()
        api.reconcile_workers()
        feedback = json.loads(operation["identity"].rsplit(":round:", 1)[0])["feedback"]
        self.assertTrue(feedback)
        self.assertEqual({}, chain["dispositions"])
        self.assertEqual(feedback, api.observe(chain)["attemptHold"])
        self.assertEqual([], api.observe(chain)["feedback"])
        api.persist()
        fresh = fixture.fresh(api)
        fresh.read_authority()
        fresh.reconcile_workers()
        observed = fresh.ledger["chains"][0]
        self.assertEqual(chain["operations"], observed["operations"])
        self.assertEqual(chain["dispositions"], observed["dispositions"])
        self.assertEqual(1, observed["rounds"])
        self.assertEqual([], [write for write in transport.writes if write[1].endswith("/tasks")])

    def test_historical_waiting_operation_recovers_from_verified_terminal_receipt(self):
        fixture, api, transport, chain, operation, task = self.worker()
        operation["workerState"] = "completed"
        operation["state"] = "waiting"
        api.persist()
        fresh = fixture.fresh(api)
        fresh.read_authority()
        fresh.reconcile_workers()
        recovered = fresh.ledger["chains"][0]["operations"][0]
        self.assertEqual(("completed", 498, None),
                         (recovered["state"], recovered["workerReserved"], recovered["workerActual"]))
        self.assertEqual(operation["id"], recovered["id"])

    def test_later_usage_settles_the_same_completed_operation(self):
        _, api, transport, chain, operation, task = self.worker()
        api.reconcile_workers()
        identity = operation["id"]
        task["sessions"][0]["usage"] = {"type": "ai_credits", "amount": 1500000000}
        api.reconcile_workers()
        self.assertEqual(("completed", 1.5, 0),
                         (operation["state"], operation["workerActual"], operation["workerReserved"]))
        self.assertFalse(state.worker_billing_pending(chain))
        self.assertEqual(3.5, state.chain_spend(chain))
        self.assertEqual(identity, operation["id"])
        self.assertEqual(1, chain["rounds"])

    def test_partial_usage_is_still_reported_as_unknown_after_lifecycle_completion(self):
        fixture = tracked_tests.TrackedOnlyTests()
        api, transport = fixture.api()
        chain, operation, task = fixture.seed_worker(api, transport, completed=True)
        task["sessions"].append({**task["sessions"][0], "id": "UNBILLED", "usage": None})
        task["session_count"] = 2
        api.reconcile_workers()
        self.assertEqual(("completed", 1.5, 496.5),
                         (operation["state"], operation["workerActual"], operation["workerReserved"]))
        self.assertTrue(state.worker_billing_pending(chain))
        output = io.StringIO()
        with redirect_stdout(output):
            api.log_status(chain, api.observe(chain), api.clock())
        self.assertIn("billing: unknown amounts remain reserved", output.getvalue())
        self.assertIn("Tracked worker finished; billing unavailable", output.getvalue())

    def test_resumed_or_unverifiable_task_returns_to_pending_without_refunding_hold(self):
        for failure in ("resumed", "conflicting-session", "missing", "malformed-usage"):
            with self.subTest(failure=failure):
                _, api, transport, chain, operation, task = self.worker()
                api.reconcile_workers()
                if failure == "resumed":
                    task["state"] = task["sessions"][0]["state"] = "in_progress"
                elif failure == "conflicting-session":
                    task["sessions"][0]["state"] = "in_progress"
                elif failure == "malformed-usage":
                    task["sessions"][0]["usage"] = "invalid"
                else:
                    del transport.values[f"agents/repos/{api.repository}/tasks/{task['id']}"]
                api.reconcile_workers()
                self.assertTrue(state.pending(chain))
                self.assertEqual("waiting", operation["state"])
                self.assertEqual(498, operation["workerReserved"])
                self.assertEqual(1, state.worker_slots(api.ledger))

    def test_verified_issue_child_adoption_is_independent_of_billing_and_idempotent(self):
        fixture = github_tests.PilotGitHubTests()
        fixture.setUp()
        chain, operation, task = fixture.issue_worker()
        task["sessions"][0]["usage"] = None
        fixture.api.reconcile_workers()
        self.assertEqual((9, "confirmed", "completed"),
                         (chain["child"], chain["childAdoption"], operation["state"]))
        self.assertEqual(498, operation["workerReserved"])
        writes = len(fixture.transport.writes)
        fixture.api.reconcile_workers()
        self.assertEqual(writes, len(fixture.transport.writes))
        self.assertEqual(1, chain["rounds"])

    def test_local_completed_unbilled_result_names_billing_and_round_cap_without_executor(self):
        fixture, api, transport, chain, operation, task = self.worker(binding=bindings.UPSTREAM)
        for index in range(2, api.binding.round_limit + 1):
            chain["operations"].append({
                **operation, "id": f"settled-native-{index}",
                "identity": operation["identity"].rsplit(":round:", 1)[0] + f":round:{index}",
                "state": "completed", "taskId": None, "sessionId": None,
                "nativeActual": 0, "nativeReserved": 0, "workerActual": None,
                "workerState": None, "workerReserved": 0, "workerVersion": None,
            })
        chain["rounds"] = api.binding.round_limit
        api.persist()
        api.enabled = lambda: True
        api.token = "fixture-token"
        with patch.object(local.live, "clock", api.clock), redirect_stdout(io.StringIO()):
            result = local.sweep(api, self.work / "completed", "b" * 40,
                                 executor=lambda *_: self.fail("finished unbilled task must not infer"))
        self.assertEqual({
            "outcome": "observed; no inference",
            "reasons": [{"chain": chain["id"], "reason": (
                "Tracked worker finished; billing unavailable, reservation retained. No new paid repair. "
                f"Lifetime action round limit ({api.binding.round_limit}) also reached.")}],
            "roundLimitReached": True,
        }, result)
        self.assertFalse((self.work / "completed" / "agent").exists())
        chain = state.find_chain(api.ledger, 20722)
        self.assertEqual(api.binding.round_limit, chain["rounds"])
        self.assertEqual(state.chain_allowance(api.ledger) - operation["nativeActual"],
                         operation["workerReserved"])
        self.assertIn(f"Lifetime action round limit ({api.binding.round_limit}) also reached",
                      api.next_action(chain, api.observe(chain)))

    def test_local_finished_worker_hold_is_distinct_from_round_limit_and_hands_off(self):
        for hands_off in (False, True):
            with self.subTest(hands_off=hands_off):
                fixture, api, transport, chain, operation, task = self.worker(binding=bindings.UPSTREAM)
                api.enabled = lambda: True
                api.token = "fixture-token"
                if hands_off:
                    transport.values[f"{api.prefix}/pulls/20722"]["labels"] = [{"name": "shepherd-hands-off"}]
                directory = self.work / str(hands_off)
                with patch.object(local.live, "clock", api.clock), redirect_stdout(io.StringIO()):
                    result = local.sweep(api, directory, "b" * 40,
                                         executor=lambda *_: self.fail("unknown costs must not infer"))
                if hands_off:
                    self.assertEqual({"outcome": "observed; no inference",
                                      "reasons": [{"chain": chain["id"], "reason":
                                          "Adoption removed or hands-off label applied; no new repairs."}],
                                      "roundLimitReached": False}, result)
                    self.assertEqual("hands-off", state.find_chain(api.ledger, 20722)["state"])
                else:
                    self.assertEqual({
                        "outcome": "observed; no inference",
                        "reasons": [{"chain": chain["id"], "reason":
                            "Tracked worker finished; billing unavailable, reservation retained. No new paid repair."}],
                        "roundLimitReached": False,
                    }, result)
                self.assertFalse((directory / "agent").exists())
                self.assertEqual(1, state.find_chain(api.ledger, 20722)["rounds"])

    def test_upstream_increased_allowance_retains_legacy_hold_and_admits_new_feedback(self):
        _, api, transport, chain, operation, task = self.worker(binding=bindings.UPSTREAM)
        operation["workerReserved"] = 498
        api.persist()
        transport.values[f"{api.prefix}/pulls/20722/comments"].append({
            "id": 32, "body": "New review feedback", "updated_at": "2026-10-04T00:01:00Z",
            "user": {"id": 1472, "login": "radical"}})
        api.reconcile_workers()
        self.assertEqual(498, operation["workerReserved"])
        self.assertIsNone(operation["workerActual"])
        observed = api.observe(chain)
        self.assertTrue(observed["actionable"])
        self.assertEqual("Bounded repair batch due. Finished worker billing unavailable; reservation retained.",
                         api.next_action(chain, observed))
        api.persist()
        packet = pilot.prepare(api, tracked_tests.RUN, api.clock(), present=False)
        self.assertIsNotNone(packet)
        self.assertEqual(2, chain["rounds"])
        self.assertEqual(498, operation["workerReserved"])
        self.assertEqual(30, chain["operations"][-1]["nativeReserved"])
        self.assertEqual(1000, state.chain_allowance(api.ledger))
        decision = tracked_tests.decision(packet)
        pilot.settle(api, packet, reconciliation_evidence(decision), 2, api.clock())
        self.assertEqual(1, len([write for write in transport.writes
                                if write[0] == "POST" and write[1].endswith("/tasks")]))
        self.assertEqual(498, operation["workerReserved"])
        self.assertEqual(498, chain["operations"][-1]["workerReserved"])
        self.assertEqual(1000, state.chain_spend(chain))
        self.assertIsNone(operation["workerActual"])

    def test_upstream_credit_headroom_does_not_reopen_explicit_declines(self):
        _, api, transport, chain, operation, task = self.worker(binding=bindings.UPSTREAM)
        operation["workerReserved"] = 498
        chain["dispositions"].update({item: "declined"
            for item in json.loads(operation["identity"].rsplit(":round:", 1)[0])["feedback"]})
        api.persist()
        api.enabled = lambda: True
        api.token = "fixture-token"
        with patch.object(local.live, "clock", api.clock), redirect_stdout(io.StringIO()):
            result = local.sweep(api, self.work / "no-new-feedback", "b" * 40,
                                 executor=lambda *_: self.fail("completed feedback must not be retried"))
        self.assertEqual("observed; no inference", result["outcome"])
        self.assertFalse(result["roundLimitReached"])
        chain = state.find_chain(api.ledger, 20722)
        self.assertEqual(1, chain["rounds"])
        self.assertEqual(498, chain["operations"][0]["workerReserved"])
        self.assertEqual(
            "Waiting for human review / supported new feedback; no inference. "
            "Finished worker billing unavailable; reservation retained.",
            api.next_action(chain, api.observe(chain)))


if __name__ == "__main__":
    unittest.main()
