from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import unittest

from github import Response
from helpers import reconciliation_evidence
import pilot
import pilot_binding as bindings
import pilot_github as github
import pilot_results as results
import pilot_state as state
import test_pilot_tracked_only as fixtures


class WorkerResultTests(unittest.TestCase):
    def worker(self, *, legacy=False, binding=bindings.FORK):
        fixture = fixtures.TrackedOnlyTests()
        api, transport = fixture.api(binding)
        chain, operation, task = fixture.seed_worker(api, transport, completed=True)
        if legacy:
            chain["dispositions"].update({identity: "needs-human" for identity in results.basis(operation)["feedback"]})
            api.persist()
        return fixture, api, transport, chain, operation, task

    def test_legacy_completion_reenters_exact_batch_without_rewriting_history_or_billing(self):
        fixture, api, transport, chain, operation, task = self.worker(legacy=True)
        before = deepcopy(api.ledger)
        fresh = fixture.fresh(api)
        fresh.read_authority()
        fresh.reconcile_workers()
        current = fresh.ledger["chains"][0]
        observed = fresh.observe(current)
        self.assertTrue(observed["actionable"])
        self.assertEqual(results.basis(operation)["feedback"], [item["id"] for item in observed["feedback"]])
        self.assertEqual(before, fresh.ledger)
        self.assertIn("Re-evaluating 1 legacy completion entries", fresh.status(current, observed, fresh.clock()))
        fresh.reconcile_workers()
        self.assertEqual(observed, fresh.observe(current))
        self.assertEqual(before, fresh.ledger)
        self.assertEqual([], [write for write in transport.writes if write[1].endswith("/tasks")])

    def test_completed_worker_with_unchanged_failing_ci_gets_fresh_decision_and_one_dispatch(self):
        fixture, api, transport, chain, operation, task = self.worker(binding=bindings.UPSTREAM)
        pr = transport.values[f"{api.prefix}/pulls/20722"]
        check = {"id": 45, "head_sha": pr["head"]["sha"], "status": "completed", "conclusion": "failure",
                 "name": "Tests", "html_url": "https://github.com/microsoft/aspire/pull/20722",
                 "output": {"annotations_count": 0, "title": "", "summary": "", "text": ""}}
        transport.values[f"{api.prefix}/commits/{pr['head']['sha']}/check-runs"] = {
            "check_runs": [check], "total_count": 1}
        with redirect_stdout(io.StringIO()):
            packet = pilot.prepare(api, fixtures.RUN, api.clock(), present=False)
        self.assertTrue(packet["observation"]["workerResults"][0]["headChanged"] is False)
        self.assertIn(f"check:45:{pr['head']['sha']}:failure",
                      [item["id"] for item in packet["observation"]["feedback"]])
        decision = fixtures.decision(packet)
        result = pilot.settle(fixture.fresh(api), packet, reconciliation_evidence(decision), 2, api.clock())
        self.assertEqual("uncertain", result["outcome"])
        self.assertEqual(1, len([write for write in transport.writes if write[1].endswith("/tasks")]))
        self.assertEqual(2, state.parse(transport.comments[0]["body"])["chains"][0]["rounds"])

    def test_result_facts_compare_heads_without_claiming_resolution_or_worker_attribution(self):
        _, api, transport, chain, operation, task = self.worker()
        pr = transport.values[f"{api.prefix}/pulls/7"]
        task["artifacts"] = [
            {"provider": "github", "type": "pull", "data": {"id": pr["id"], "global_id": pr["node_id"]}},
            {"provider": "github", "type": "branch", "data": {"head_ref": pr["head"]["ref"], "base_ref": "main"}}]
        task["sessions"][0]["error"] = {"message": "Unable to validate changes"}
        api.reconcile_workers()
        observed = api.observe(chain)
        self.assertEqual([{
            "operation": operation["id"], "taskId": task["id"], "state": "completed",
            "sessionCount": 1, "sessionIds": ["SESSION7"], "updatedAt": "2026-10-04T00:00:00Z",
            "sessionStates": ["completed"], "sourceHead": "a" * 40,
            "artifactState": "matched", "errors": [{"sessionId": "SESSION7", "message": "Unable to validate changes"}],
            "errorsTruncated": False, "narrativeAvailable": False, "currentHead": "a" * 40, "headChanged": False,
        }], observed["workerResults"])
        pr["head"]["sha"] = "b" * 40
        self.assertTrue(api.observe(chain)["workerResults"][0]["headChanged"])
        self.assertFalse(api.observe(chain)["ready"])

    def test_failed_cancelled_and_timed_out_workers_expose_errors_and_do_not_shelve_feedback(self):
        for outcome in ("failed", "cancelled", "timed_out"):
            with self.subTest(outcome=outcome):
                _, api, transport, chain, operation, task = self.worker()
                task["state"] = task["sessions"][0]["state"] = outcome
                task["sessions"][0]["error"] = {"message": "Validation failed"}
                api.reconcile_workers()
                observed = api.observe(chain)
                self.assertTrue(observed["actionable"])
                self.assertEqual(outcome, observed["workerResults"][0]["state"])
                self.assertEqual("Validation failed", observed["workerResults"][0]["errors"][0]["message"])
                self.assertEqual({}, chain["dispositions"])

    def test_legacy_recovery_preserves_explicit_and_ambiguous_handoffs_and_unrelated_entries(self):
        for blocker in ("human-chain", "hands-off-chain", "declined", "addressed",
                        "explicit-worker-human", "later-native-human", "ambiguous-native", "unrelated"):
            with self.subTest(blocker=blocker):
                _, api, transport, chain, operation, task = self.worker(legacy=True)
                identity = results.basis(operation)["feedback"][0]
                if blocker.endswith("-chain"):
                    chain["state"] = blocker.removesuffix("-chain")
                elif blocker in {"declined", "addressed"}:
                    chain["dispositions"][identity] = blocker
                elif blocker == "explicit-worker-human":
                    operation["feedbackDecisions"] = {identity: "needs-human"}
                elif blocker in {"later-native-human", "ambiguous-native"}:
                    later = state.reserve(api.ledger, chain, github.fingerprint(api.observe(chain)) + ":round:2",
                                          api.clock(), local=False)
                    if blocker == "later-native-human":
                        later["feedbackDecisions"] = {identity: "needs-human"}
                    state.settle_native(later, 1)
                    state.finish(later, "completed")
                else:
                    identity = "review-comment:unknown:2026-10-04T00:00:00Z"
                    chain["dispositions"][identity] = "needs-human"
                self.assertFalse(results.eligible(chain, identity, api.worker_results))

    def test_unknown_or_resumed_receipt_cannot_reopen_legacy_completion(self):
        for change in ("missing", "resumed", "wrong-artifact", "malformed-error"):
            with self.subTest(change=change):
                _, api, transport, chain, operation, task = self.worker(legacy=True)
                if change == "missing":
                    del transport.values[f"agents/repos/{api.repository}/tasks/{task['id']}"]
                elif change == "resumed":
                    task["state"] = task["sessions"][0]["state"] = "in_progress"
                elif change == "wrong-artifact":
                    task["artifacts"] = [{"provider": "github", "type": "pull", "data": {"id": 999}}]
                else:
                    task["sessions"][0]["error"] = {"message": []}
                api.reconcile_workers()
                self.assertTrue(state.pending(chain))
                self.assertFalse(api.observe(chain)["actionable"])
                self.assertEqual([], api.observe(chain)["workerResults"])

    def test_cloud_mixed_decisions_preserve_declines_and_human_items_after_completion_and_restart(self):
        api, transport = fixtures.TrackedOnlyTests().api()
        comments = transport.values[f"{api.prefix}/issues/7/comments"]
        comments.extend([{**comments[0], "id": index, "body": "Other feedback"} for index in (21, 22)])
        with redirect_stdout(io.StringIO()):
            packet = pilot.prepare(api, fixtures.RUN, api.clock(), present=False)
        chain = api.ledger["chains"][0]
        operation = chain["operations"][-1]
        decision = fixtures.decision(packet)
        identities = list(decision["dispositions"])
        decision["dispositions"] = dict(zip(identities, ("addressed", "declined", "needs-human")))
        original = api.transport

        def send(method, endpoint, body):
            if method == "POST" and endpoint.endswith("/tasks"):
                transport.writes.append((method, endpoint, body))
                transport.values[endpoint + "/NEXT"] = {
                    "id": "NEXT", "state": "in_progress", "creator": {"id": 1472},
                    "repository": {"id": api.repository_id}, "session_count": 1, "artifacts": [],
                    "sessions": [{"id": "NEXTSESSION", "task_id": "NEXT", "state": "in_progress",
                                 "repository": {"id": api.repository_id}, "user": {"id": 1472},
                                 "base_ref": "main", "head_ref": "fix-7", "prompt": body["prompt"]}]}
                return Response({"id": "NEXT"}, {}, 201)
            return original(method, endpoint, body)

        api.transport = send
        api.api.transport = send
        result = pilot.settle(api, packet, reconciliation_evidence(decision), 2, api.clock())
        self.assertEqual("waiting", result["outcome"])
        self.assertEqual(decision["dispositions"], operation["feedbackDecisions"])
        self.assertEqual(dict(zip(identities[1:], ("declined", "needs-human"))), chain["dispositions"])
        prompt = next(body["prompt"] for method, endpoint, body in transport.writes if endpoint.endswith("/tasks"))
        native_plan = prompt.split("Native feedback decisions (addressed means repair requested): ", 1)[1].splitlines()[0]
        self.assertEqual(decision["dispositions"], json.loads(native_plan))
        task = transport.values[f"agents/repos/{api.repository}/tasks/NEXT"]
        task["state"] = task["sessions"][0]["state"] = "completed"
        task["sessions"][0]["usage"] = {"type": "ai_credits", "amount": 1000000000}
        fresh = fixtures.TrackedOnlyTests().fresh(api)
        fresh.read_authority()
        fresh.reconcile_workers()
        current = fresh.ledger["chains"][0]
        self.assertEqual([identities[0]], [item["id"] for item in fresh.observe(current)["feedback"]])
        self.assertEqual("open", current["state"])
        self.assertEqual(1, len([write for write in transport.writes if write[1].endswith("/tasks")]))

    def test_cloud_without_any_requested_pr_repairs_hands_off_without_dispatch(self):
        api, transport = fixtures.TrackedOnlyTests().api()
        with redirect_stdout(io.StringIO()):
            packet = pilot.prepare(api, fixtures.RUN, api.clock(), present=False)
        decision = fixtures.decision(packet)
        decision["dispositions"] = {key: "needs-human" for key in decision["dispositions"]}
        self.assertEqual({"outcome": "human"},
                         pilot.settle(api, packet, reconciliation_evidence(decision), 2, api.clock()))
        self.assertEqual("human", api.ledger["chains"][0]["state"])
        self.assertEqual([], [write for write in transport.writes if write[1].endswith("/tasks")])

    def test_native_provenance_is_optional_but_exact_and_survives_canonical_round_trip(self):
        _, api, transport, chain, operation, task = self.worker()
        self.assertEqual(api.ledger, state.parse(state.render(api.ledger)))
        operation["feedbackDecisions"] = {identity: "addressed" for identity in results.basis(operation)["feedback"]}
        self.assertEqual(api.ledger, state.parse(state.render(api.ledger)))
        for invalid in ({}, {"foreign": "declined"}, {next(iter(operation["feedbackDecisions"])): "fixed"}):
            with self.subTest(invalid=invalid):
                operation["feedbackDecisions"] = invalid
                with self.assertRaisesRegex(ValueError, "invalid operation feedback decisions"):
                    state.validate(api.ledger)

    def test_terminal_receipt_changes_between_prepare_and_settle_reject_dispatch(self):
        for change in ("session", "version", "outcome"):
            with self.subTest(change=change):
                fixture, api, transport, chain, operation, task = self.worker(legacy=True)
                with redirect_stdout(io.StringIO()):
                    packet = pilot.prepare(api, fixtures.RUN, api.clock(), present=False)
                if change == "session":
                    task["sessions"][0]["id"] = "REPLACEMENT"
                elif change == "version":
                    task["updated_at"] = "2026-10-04T00:01:00Z"
                else:
                    task["state"] = task["sessions"][0]["state"] = "failed"
                result = pilot.settle(fixture.fresh(api), packet,
                                      reconciliation_evidence(fixtures.decision(packet)), 2, api.clock())
                self.assertEqual("failed", result["outcome"])
                self.assertIn("basis changed", result["error"])
                self.assertEqual([], [write for write in transport.writes if write[1].endswith("/tasks")])

    def test_finished_issue_with_ambiguous_pr_artifacts_preserves_human_handoff(self):
        from test_pilot_github import PilotGitHubTests
        fixture = PilotGitHubTests()
        fixture.setUp()
        chain, operation, task = fixture.issue_worker()
        task["artifacts"].append(deepcopy(task["artifacts"][0]))
        fixture.api.reconcile_workers()
        self.assertEqual("completed", operation["state"])
        self.assertEqual("human", chain["state"])
        self.assertEqual(0, state.worker_slots(fixture.api.ledger))

    def test_result_error_fields_are_optional_and_bounded_without_supposing_success(self):
        _, api, transport, chain, operation, task = self.worker()
        task["sessions"][0]["error"] = {}
        api.reconcile_workers()
        self.assertEqual([{"sessionId": "SESSION7", "message": None}],
                         api.observe(chain)["workerResults"][0]["errors"])
        task["sessions"][0]["error"] = {"message": "x" * 10000}
        for index in range(1, 6):
            task["sessions"].append({**task["sessions"][0], "id": f"SESSION-{index}"})
        task["session_count"] = len(task["sessions"])
        api.reconcile_workers()
        receipt = api.observe(chain)["workerResults"][0]
        self.assertTrue(receipt["errorsTruncated"])
        self.assertEqual(4, len(receipt["errors"]))
        self.assertEqual({500}, {len(item["message"]) for item in receipt["errors"]})

    def test_no_packet_reports_actual_credit_and_request_admission_failures(self):
        from unittest.mock import patch
        for failure in ("credits", "request-size"):
            with self.subTest(failure=failure):
                fixture, api, transport, chain, operation, task = self.worker()
                if failure == "credits":
                    operation["nativeActual"] = state.chain_allowance(api.ledger) - 20
                    operation["workerActual"] = 0
                    task["sessions"][0]["usage"]["amount"] = 0
                    api.persist()
                with patch.object(pilot, "bound_worker_request",
                                  side_effect=ValueError("mandatory worker request fields exceed 20000 bytes")
                                  if failure == "request-size" else None,
                                  wraps=pilot.bound_worker_request), redirect_stdout(io.StringIO()):
                    self.assertIsNone(pilot.prepare(api, fixtures.RUN, api.clock(), present=False))
                reason = api.next_action(chain, api.observe(chain))
                self.assertIn("chain credit allowance exhausted" if failure == "credits"
                              else "mandatory worker request fields exceed 20000 bytes", reason)
                self.assertEqual(1, chain["rounds"])


if __name__ == "__main__":
    unittest.main()
