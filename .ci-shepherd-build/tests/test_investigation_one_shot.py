from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ci_shepherd import investigations
from ci_shepherd.jsonl import read_jsonl_rows
from ci_shepherd.investigations import (
    build_investigation_plan,
    load_one_shot_result,
    read_investigation_results,
    read_investigation_session_events,
    record_investigation_result,
    record_investigation_session_event,
)
from ci_shepherd.investigation_worktrees import (
    bind_investigation_worktree,
    cleanup_investigation_worktree,
    finish_investigation_worktree,
    investigation_capacity_inventory,
    list_investigation_worktrees,
    provision_investigation_worktree,
)
from test_investigation_scope import _evidence_result, _owned_worker, _source_checkout, _source_request
from test_investigations import _judgments, _prepared


class OneShotInvestigationTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = Path("tests/.tmp")
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = _source_checkout(self.root)
        self.state, self.request, self.checkout = _owned_worker(self.root, self.source)
        self.owner = list_investigation_worktrees(self.state)[0]["ownershipId"]
        self.output = self.root / "results" / f"{self.owner}.json"

    def prepare(self, **kwargs) -> dict:
        return record_investigation_session_event(
            self.state, self.request, status="prepared", session_id=None,
            launch_mode="one-shot", checkout=self.checkout, result_path=self.output,
            recorded_at="2026-09-08T23:00:00Z", **kwargs,
        )

    def dispatch(self) -> dict:
        return record_investigation_session_event(
            self.state, self.request, status="dispatching", session_id=None,
            attempt_id=self.owner, recorded_at="2026-09-08T23:01:00Z",
        )

    def response(self) -> dict:
        return {
            "schemaVersion": 1, "attemptId": self.owner,
            "requestFingerprint": list_investigation_worktrees(self.state)[0]["requestFingerprint"],
            "result": _evidence_result(),
        }

    def complete(self, response=None, **kwargs) -> dict:
        return record_investigation_result(
            self.state, self.request, self.response() if response is None else response, session_id=None,
            attempt_id=self.owner, checkout=self.checkout, recorded_at="2026-09-08T23:02:00Z",
            execution_evidence="The synchronous invocation returned to the coordinator.",
            confirm_worker_stopped=True, **kwargs,
        )

    def record_failure(self, *, status="failed", execution_state="not-launched", **kwargs) -> dict:
        evidence = {
            "not-launched": "The launcher rejected the call without creating a worker.",
            "returned": "The synchronous worker returned without a valid result.",
            "unknown": "The invocation is independently confirmed stopped; whether a worker ran is unknown.",
        }[execution_state]
        return record_investigation_session_event(
            self.state, self.request, status=status, session_id=None,
            attempt_id=self.owner, recorded_at="2026-09-09T00:03:00Z",
            failure_reason="Launcher did not produce a valid result.",
            execution_state=execution_state,
            execution_evidence=evidence,
            confirm_worker_stopped=True, **kwargs,
        )

    def test_preparation_freezes_complete_envelope_without_claiming_worker_execution(self) -> None:
        prepared = self.prepare()
        self.assertEqual("prepared", prepared["status"])
        self.assertEqual("not-dispatched", prepared["executionState"])
        self.assertEqual(self.owner, prepared["attemptId"])
        self.assertIsNone(prepared["sessionId"])
        self.assertIsNone(prepared["runtimeSessionId"])
        self.assertEqual("unknown", prepared["workerIdentityKind"])
        self.assertIn(f"WORKTREE_PATH: {self.checkout}", prepared["launchEnvelope"])
        self.assertIn(f"RESULT_PATH: {self.output}", prepared["launchEnvelope"])
        self.assertIn(self.request["workerPrompt"], prepared["launchEnvelope"])
        self.assertIn(self.request["sourceRevision"], prepared["launchEnvelope"])
        self.assertIn("Do NOT switch branches", prepared["launchEnvelope"])
        self.assertIn("Do not launch subagents or background processes", prepared["launchEnvelope"])
        self.assertIn("WORK_BUDGET_SECONDS: 180", prepared["launchEnvelope"])
        self.assertIn("cooperative", prepared["launchEnvelope"])
        self.assertIn(self.request["question"], prepared["launchEnvelope"])
        self.assertEqual([], prepared["reproductionCommands"])
        self.assertFalse(self.output.exists())
        self.assertEqual(prepared, self.prepare())
        allocation, = list_investigation_worktrees(self.state)
        self.assertEqual("reserved", allocation["state"])
        self.assertIsNone(allocation["sessionId"])
        self.assertEqual(self.owner, allocation["attemptId"])
        self.assertEqual(1, len(read_investigation_session_events(self.state)))
        with self.assertRaises(ValueError):
            bind_investigation_worktree(
                self.state, self.request, checkout=self.checkout,
                session_id="unrelated-worker", recorded_at="2026-09-08T23:01:00Z",
            )

    def test_registration_counts_bound_allocations_missing_lifecycle_events(self) -> None:
        allocations = [
            self._allocate_attempt({**self.request, "investigationId": f"investigation:unrecorded-{index}"})
            for index in range(3)
        ]
        self.prepare()
        for index, allocation in enumerate(allocations[:2]):
            bind_investigation_worktree(
                self.state, allocation["request"], checkout=Path(allocation["checkoutPath"]),
                session_id=f"unrecorded-worker-{index}", recorded_at="2026-09-08T23:01:00Z",
            )
        before = read_investigation_session_events(self.state)
        with self.assertRaisesRegex(ValueError, "Three investigation slots"):
            self._register_attempt(allocations[2], "one-shot")
        self.assertEqual(before, read_investigation_session_events(self.state))

    def test_unknown_legacy_worker_remains_reserved_after_replacement_starts(self) -> None:
        request = {
            key: value for key, value in self.request.items()
            if key not in {"sourceRevision", "investigationScope"}
        }
        for status, session in (("started", "old-worker"), ("failed", "old-worker"), ("started", "replacement-worker")):
            record_investigation_session_event(
                self.state, request, status=status, session_id=session,
                checkout=self.checkout if status == "started" else None,
                recorded_at="2026-09-08T23:01:00Z",
                **({"failure_reason": "The worker did not return; termination is unknown."} if status == "failed" else {}),
            )
        inventory = investigation_capacity_inventory(self.state, request["repository"])
        self.assertEqual(2, inventory["occupiedSlots"])
        self.assertEqual({"old-worker", "replacement-worker"}, {row["sessionId"] for row in inventory["reservations"]})
        stopped = record_investigation_session_event(
            self.state, request, status="failed", session_id="old-worker",
            recorded_at="2026-09-08T23:03:00Z", confirm_worker_stopped=True,
            failure_reason="The worker did not return; termination is unknown.",
        )
        self.assertTrue(stopped["workerStopped"])
        inventory = investigation_capacity_inventory(self.state, request["repository"])
        self.assertEqual(1, inventory["occupiedSlots"])
        self.assertEqual(["replacement-worker"], [row["sessionId"] for row in inventory["reservations"]])

    def test_matching_manual_stop_releases_slot_despite_older_failure_event(self) -> None:
        allocation = list_investigation_worktrees(self.state)[0]
        registration = self._register_attempt(allocation, "resumable")
        record_investigation_session_event(
            self.state, self.request, status="failed", session_id=registration["sessionId"],
            recorded_at="2026-09-08T23:02:00Z", failure_reason="The worker is unavailable.",
        )
        self.assertEqual(1, investigation_capacity_inventory(self.state, self.request["repository"])["occupiedSlots"])
        finish_investigation_worktree(
            self.state, self.request, checkout=self.checkout, session_id=registration["sessionId"],
            status="failed", recorded_at="2026-09-08T23:03:00Z", confirm_worker_stopped=True,
        )
        cleanup_investigation_worktree(
            self.state, self.request, checkout=self.checkout, session_id=registration["sessionId"],
            recorded_at="2026-09-08T23:04:00Z", confirm_worker_stopped=True,
        )
        self.assertEqual(0, investigation_capacity_inventory(self.state, self.request["repository"])["occupiedSlots"])

    def test_exact_preparation_replay_preserves_legacy_frozen_envelope(self) -> None:
        envelope = investigations._one_shot_envelope
        budget_line = "WORK_BUDGET_SECONDS: 180 (cooperative, from investigation start)\n"
        with patch.object(investigations, "_one_shot_envelope", side_effect=lambda event: envelope(event).replace(budget_line, "")):
            prepared = self.prepare()
        self.assertEqual(prepared, self.prepare())
        self.assertEqual(prepared["launchEnvelope"], self.dispatch()["launchEnvelope"])

    def test_never_launched_failures_still_bound_owned_attempts_before_provisioning(self) -> None:
        for attempt in (1, 2):
            allocation = list_investigation_worktrees(self.state)[0] if attempt == 1 else self._allocate_attempt({
                **self.request, "attempt": 2,
            })
            finish_investigation_worktree(
                self.state, allocation["request"], checkout=Path(allocation["checkoutPath"]),
                session_id=None, status="failed", recorded_at="2026-09-08T23:02:00Z",
                launch_outcome="not-invoked", execution_evidence="The coordinator never invoked a launcher.",
            )
        before = list_investigation_worktrees(self.state)
        with self.assertRaisesRegex(ValueError, "attempt"):
            self._allocate_attempt({**self.request, "attempt": 3})
        self.assertEqual(before, list_investigation_worktrees(self.state))

    def test_dispatch_is_single_use_and_replay_does_not_authorize_another_launch(self) -> None:
        self.prepare()
        dispatched = self.dispatch()
        self.assertTrue(dispatched["dispatchAllowed"])
        self.assertEqual("unknown", dispatched["executionState"])
        replay = self.dispatch()
        self.assertFalse(replay["dispatchAllowed"])
        self.assertEqual(2, len(read_investigation_session_events(self.state)))
        self.assertEqual([], read_investigation_results(self.state))
        with self.assertRaisesRegex(ValueError, "active|pending|prepared"):
            self.prepare()

    def test_identical_result_failures_stop_dispatch_and_admission_without_accepting_results(self) -> None:
        allocations = [list_investigation_worktrees(self.state)[0], *[
            self._allocate_attempt({**self.request, "investigationId": f"investigation:breaker-{index}"})
            for index in (2, 3)
        ]]
        self.prepare()
        for allocation in allocations[1:]:
            self._register_attempt(allocation, "one-shot")
        self.dispatch()

        def dispatch(allocation):
            return record_investigation_session_event(
                self.state, allocation["request"], status="dispatching", session_id=None,
                attempt_id=allocation["ownershipId"], recorded_at="2026-09-08T23:03:00Z",
            )

        with self.assertRaisesRegex(ValueError, "Preflight the first"):
            dispatch(allocations[1])
        for index, allocation in enumerate(allocations[:2]):
            if index:
                dispatch(allocation)
                with self.assertRaisesRegex(ValueError, "Preflight the first"):
                    dispatch(allocations[2])
            response = {
                "schemaVersion": 1, "attemptId": allocation["ownershipId"],
                "requestFingerprint": allocation["requestFingerprint"],
                "result": {**_evidence_result(), "workLog": [{
                    "kind": "github-get", "url": "https://api.github.com/repos/owner/repo/commits/main",
                    "finding": "The fixture returned a disallowed history GET, matching the rejected worker failure shape.",
                }]},
            }
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, "bounded diagnostic endpoint scope"):
                    record_investigation_result(
                        self.state, allocation["request"], response, session_id=None,
                        attempt_id=allocation["ownershipId"], checkout=Path(allocation["checkoutPath"]),
                        recorded_at="2026-09-08T23:04:00Z", preflight_only=True,
                        execution_evidence="Fixture represents a returned response, not a running worker.",
                        confirm_worker_stopped=True,
                    )
            rows = read_jsonl_rows(self.state / "ledgers/investigation-validations.jsonl")
            self.assertEqual(index + 1, len(rows))
            self.assertTrue(all(row["valid"] is False for row in rows))
            record_investigation_session_event(
                self.state, allocation["request"], status="failed", session_id=None,
                attempt_id=allocation["ownershipId"], recorded_at="2026-09-08T23:05:00Z",
                failure_reason="The actual fixture response failed endpoint validation.",
                failure_category="out-of-scope-evidence", execution_state="returned",
                execution_evidence="Fixture response returned without launching a background process.",
                confirm_worker_stopped=True,
            )
        before = read_investigation_session_events(self.state)
        with self.assertRaisesRegex(ValueError, "circuit is open"):
            dispatch(allocations[2])
        with self.assertRaisesRegex(ValueError, "circuit is open"):
            self._allocate_attempt({**self.request, "investigationId": "investigation:breaker-next-wave"})
        self.assertEqual(before, read_investigation_session_events(self.state))
        self.assertEqual([], read_investigation_results(self.state))
        self.assertEqual(1, investigation_capacity_inventory(self.state, self.request["repository"])["occupiedSlots"])
        self.assertEqual(2, sum(row["status"] == "dispatching" for row in before))

    def test_structural_schema_rejection_is_durable_without_terminalizing_worker(self) -> None:
        self.prepare()
        self.dispatch()
        response = self.response()
        response["result"]["unexpected"] = "not part of the result schema"
        observation = {
            "runtimeSessionId": None, "workerStartedAt": "2026-09-08T23:01:10Z",
            "workerCompletedAt": "2026-09-08T23:01:20Z",
            "observationEvidence": "Observed fixture runtime timestamps accompanying the rejected response.",
        }
        with self.assertRaisesRegex(ValueError, "result schema"):
            self.complete(response, runtime_observation=observation)
        validation, = read_jsonl_rows(self.state / "ledgers/investigation-validations.jsonl")
        self.assertFalse(validation["valid"])
        self.assertEqual(self.owner, validation["attemptId"])
        self.assertEqual(observation, validation["runtimeObservation"])
        self.assertEqual("dispatching", read_investigation_session_events(self.state)[-1]["status"])
        self.assertEqual([], read_investigation_results(self.state))
        self.assertEqual(1, investigation_capacity_inventory(self.state, self.request["repository"])["occupiedSlots"])

    def test_admission_honors_frozen_planner_priority_before_allocating_worktrees(self) -> None:
        prepared, judgments = _prepared(), _judgments()
        prepared["sourceRevision"] = self.request["sourceRevision"]
        low = prepared["issues"][0]
        low["sourceRevision"] = self.request["sourceRevision"]
        for number, workflow, category in (
            (307, ".github/workflows/ci.yml", "blocking-build"),
            (308, ".github/workflows/another-workflow.yml", "blocking-build"),
            (309, ".github/workflows/tests.yml", "flaky-test"),
            (310, ".github/workflows/tests-quarantine.yml", "flaky-test"),
        ):
            issue = copy.deepcopy(low)
            issue.update(
                issueNumber=number, issueUrl=f"https://github.com/owner/repo/issues/{number}",
                repairEvidence={"current": True, "workflowPath": workflow, "category": category},
            )
            issue["evidenceBundle"][0]["id"] = f"issue:{number}"
            prepared["issues"].append(issue)
            judgment = copy.deepcopy(judgments["issues"][0])
            judgment["issueNumber"] = number
            judgment["recommendations"][0]["target"]["value"] = number
            judgment["recommendations"][0]["evidenceIds"][0] = f"issue:{number}"
            judgments["issues"].append(judgment)
        plan = build_investigation_plan(prepared, judgments, [])
        requests = plan["requests"]
        self.assertEqual([307, 308, 309, 310, 21], [row["issueNumber"] for row in requests])
        state = self.root / "priority-state"

        def provision(request):
            return provision_investigation_worktree(
                state, request, source_checkout=self.source, attempt=1,
                recorded_at="2026-09-08T23:00:00Z", managed_root=self.root / "priority-workers",
            )

        for index, request in enumerate(requests):
            self.assertEqual(
                [row["investigationId"] for row in requests[:index]],
                request["admissionPredecessors"],
            )
            allocation = provision(request)
            before = list_investigation_worktrees(state)
            for lower_priority in requests[index + 1:]:
                with self.subTest(issue=lower_priority["issueNumber"]), self.assertRaisesRegex(ValueError, "higher-priority"):
                    provision(lower_priority)
                self.assertEqual(before, list_investigation_worktrees(state))
            record_investigation_session_event(
                state, request, status="prepared", session_id=None, launch_mode="one-shot",
                checkout=Path(allocation["checkoutPath"]),
                result_path=self.root / "priority-results" / f"{allocation['ownershipId']}.json",
                recorded_at="2026-09-08T23:01:00Z",
            )
            record_investigation_session_event(
                state, request, status="failed", session_id=None, attempt_id=allocation["ownershipId"],
                recorded_at="2026-09-08T23:02:00Z", execution_state="not-launched",
                execution_evidence="Admission fixture did not invoke a worker.",
                failure_reason="Fixture stops after proving admission order.", confirm_worker_stopped=True,
            )
        self.assertEqual(
            [307, 308, 309, 310, 21],
            [row["issueNumber"] for row in read_investigation_session_events(state) if row["status"] == "prepared"],
        )
        self.assertEqual([], read_investigation_results(state))

    def test_observed_worker_timing_and_acceptance_are_distinct_and_replayable(self) -> None:
        self.prepare()
        self.dispatch()
        observation = {
            "runtimeSessionId": "runtime:observed-fixture",
            "workerStartedAt": "2026-09-08T23:01:10Z",
            "workerCompletedAt": "2026-09-08T23:01:45Z",
            "observationEvidence": "Recorded fixture launcher start/completion events, not result-file metadata.",
        }
        preflight = self.complete(preflight_only=True, runtime_observation=observation)
        self.assertTrue(preflight["valid"])
        self.assertEqual(observation, preflight["runtimeObservation"])
        self.assertNotIn("acceptedResultAt", preflight)
        self.assertEqual([], read_investigation_results(self.state))
        self.assertEqual("dispatching", read_investigation_session_events(self.state)[-1]["status"])
        next_allocation = self._allocate_attempt({
            **self.request, "investigationId": "investigation:after-representative-preflight",
        })
        self._register_attempt(next_allocation, "one-shot")
        next_dispatch = record_investigation_session_event(
            self.state, next_allocation["request"], status="dispatching", session_id=None,
            attempt_id=next_allocation["ownershipId"], recorded_at="2026-09-08T23:02:00Z",
        )
        self.assertTrue(next_dispatch["dispatchAllowed"])
        accepted = self.complete(runtime_observation=observation)
        self.assertEqual("2026-09-08T23:02:00Z", accepted["acceptedResultAt"])
        self.assertEqual(observation, accepted["runtimeObservation"])
        self.assertEqual("runtime:observed-fixture", accepted["runtimeSessionId"])
        self.assertEqual(accepted, self.complete())
        self.assertEqual(accepted, self.complete(runtime_observation=observation))
        with self.assertRaisesRegex(ValueError, "already recorded"):
            self.complete(runtime_observation={**observation, "runtimeSessionId": "another-worker"})
        self.assertEqual(observation, read_investigation_session_events(self.state)[-1]["runtimeObservation"])

    def test_missing_timing_stays_unknown_and_invalid_observations_cannot_accept(self) -> None:
        self.prepare()
        self.dispatch()
        observation = {
            "runtimeSessionId": None, "workerStartedAt": None, "workerCompletedAt": None,
            "observationEvidence": "Runtime did not expose timings or an event stream.",
        }
        for change in (
            {"observationEvidence": ""},
            {"workerStartedAt": "2026-09-08T23:02:01Z"},
            {"workerStartedAt": "2026-09-08T23:01:45Z", "workerCompletedAt": "2026-09-08T23:01:10Z"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.complete(runtime_observation={**observation, **change})
        self.assertEqual([], read_investigation_results(self.state))
        accepted = self.complete()
        self.assertIsNone(accepted["runtimeObservation"])
        self.assertIsNone(accepted["runtimeSessionId"])
        self.assertEqual("2026-09-08T23:02:00Z", accepted["acceptedResultAt"])

    def test_failed_worker_can_record_observed_timing_without_accepted_result(self) -> None:
        self.prepare()
        self.dispatch()
        observation = {
            "runtimeSessionId": None,
            "workerStartedAt": "2026-09-08T23:01:10Z",
            "workerCompletedAt": "2026-09-08T23:01:45Z",
            "observationEvidence": "Observed fixture worker return with no usable runtime session ID.",
        }
        failed = self.record_failure(execution_state="returned", runtime_observation=observation)
        self.assertEqual(observation, failed["runtimeObservation"])
        self.assertNotIn("acceptedResultAt", failed)
        self.assertEqual([], read_investigation_results(self.state))
        self.assertEqual(failed, self.record_failure(execution_state="returned", runtime_observation=observation))

    def test_different_structural_errors_do_not_trip_identical_failure_circuit(self) -> None:
        allocations = [list_investigation_worktrees(self.state)[0], *[
            self._allocate_attempt({**self.request, "investigationId": f"investigation:different-errors-{index}"})
            for index in (2, 3)
        ]]
        for allocation in allocations:
            self._register_attempt(allocation, "one-shot")
        for allocation, url in zip(allocations, (
            "https://api.github.com/repos/owner/repo/commits/main",
            "https://api.github.com/repos/other/repo/issues/21",
        )):
            record_investigation_session_event(
                self.state, allocation["request"], status="dispatching", session_id=None,
                attempt_id=allocation["ownershipId"], recorded_at="2026-09-08T23:02:00Z",
            )
            response = {
                "schemaVersion": 1, "attemptId": allocation["ownershipId"],
                "requestFingerprint": allocation["requestFingerprint"],
                "result": {**_evidence_result(), "workLog": [{
                    "kind": "github-get", "url": url, "finding": "A fixture diagnostic request outside the contract.",
                }]},
            }
            with self.assertRaises(ValueError):
                record_investigation_result(
                    self.state, allocation["request"], response, session_id=None,
                    attempt_id=allocation["ownershipId"], checkout=Path(allocation["checkoutPath"]),
                    recorded_at="2026-09-08T23:03:00Z", confirm_worker_stopped=True,
                    execution_evidence="The fixture response returned.",
                )
        third = allocations[2]
        dispatched = record_investigation_session_event(
            self.state, third["request"], status="dispatching", session_id=None,
            attempt_id=third["ownershipId"], recorded_at="2026-09-08T23:04:00Z",
        )
        self.assertTrue(dispatched["dispatchAllowed"])
        validations = read_jsonl_rows(self.state / "ledgers/investigation-validations.jsonl")
        self.assertEqual(2, len({row["errorCode"] for row in validations}))
        self.assertEqual([], read_investigation_results(self.state))

    def test_concurrent_dispatchers_receive_only_one_launch_authorization(self) -> None:
        self.prepare()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.dispatch(), range(2)))
        self.assertEqual([False, True], sorted(row["dispatchAllowed"] for row in results))
        self.assertEqual(2, len(read_investigation_session_events(self.state)))

    def test_result_requires_dispatch_and_exact_attempt_bound_response(self) -> None:
        self.prepare()
        with self.assertRaisesRegex(ValueError, "dispatch"):
            self.complete()
        self.dispatch()
        for field, value in (("attemptId", "another-attempt"), ("requestFingerprint", "stale")):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "attempt|fingerprint"):
                self.complete({**self.response(), field: value})
        unscoped = self.response()
        unscoped["result"]["workLog"] = [{
            "kind": "github-get", "url": "https://api.github.com/repos/other/repo/issues/21",
            "finding": "Out of scope.",
        }]
        with self.assertRaisesRegex(ValueError, "repository"):
            self.complete(unscoped)
        self.assertEqual([], read_investigation_results(self.state))
        recorded = self.complete()
        self.assertEqual(self.owner, recorded["attemptId"])
        self.assertEqual("returned", recorded["executionState"])
        self.assertIsNone(recorded["sessionId"])
        self.assertIsNone(recorded["runtimeSessionId"])
        self.assertEqual("completed", read_investigation_session_events(self.state)[-1]["status"])
        allocation, = list_investigation_worktrees(self.state)
        self.assertEqual("completed", allocation["terminalStatus"])
        self.assertTrue(allocation["workerStopped"])
        self.assertEqual(recorded, self.complete())

    def test_missing_ended_invocation_evidence_cannot_complete_or_cleanup(self) -> None:
        self.prepare()
        self.dispatch()
        for evidence, stopped in ((None, True), ("Tool returned", False)):
            with self.subTest(evidence=evidence), self.assertRaisesRegex(ValueError, "evidence|stopped"):
                record_investigation_result(
                    self.state, self.request, self.response(), session_id=None,
                    attempt_id=self.owner, checkout=self.checkout, recorded_at="2026-09-08T23:02:00Z",
                    execution_evidence=evidence, confirm_worker_stopped=stopped,
                )
        with self.assertRaisesRegex(ValueError, "terminal"):
            cleanup_investigation_worktree(
                self.state, self.request, checkout=self.checkout, attempt_id=self.owner,
                recorded_at="2026-09-08T23:02:00Z", confirm_worker_stopped=True,
            )

    def test_failed_launch_is_not_execution_and_allows_only_a_new_bounded_attempt(self) -> None:
        self.prepare()
        self.dispatch()
        failed = self.record_failure()
        self.assertEqual("not-launched", failed["executionState"])
        self.assertIsNone(failed["sessionId"])
        self.assertEqual(failed, self.record_failure())
        second_request = {**self.request, "attempt": 2}
        second = provision_investigation_worktree(
            self.state, second_request, source_checkout=self.source, attempt=2,
            managed_root=self.root / "workers", recorded_at="2026-09-09T00:04:00Z",
        )
        self.assertNotEqual(str(self.checkout), second["checkoutPath"])
        with self.assertRaisesRegex(ValueError, "attempt|terminal"):
            self.complete()

    def test_completion_replay_repairs_each_write_boundary_and_survives_cleanup(self) -> None:
        self.prepare()
        self.dispatch()
        append = investigations.append_jsonl_rows

        def interrupt(path, rows):
            if path.name == "investigation-sessions.jsonl" and rows[0]["status"] == "completed":
                raise KeyboardInterrupt("after result persistence")
            return append(path, rows)

        with patch.object(investigations, "append_jsonl_rows", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.complete()
        with patch.object(investigations, "finish_investigation_worktree", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.complete()
        recorded = self.complete()
        cleanup_investigation_worktree(
            self.state, self.request, checkout=self.checkout, attempt_id=self.owner,
            recorded_at="2026-09-09T00:04:00Z", confirm_worker_stopped=True,
        )
        self.assertFalse(self.checkout.exists())
        self.assertEqual(recorded, self.complete())
        self.assertEqual(3, len(read_investigation_session_events(self.state)))

    def test_prepared_and_uncertain_dispatch_are_pending_not_active_or_performed(self) -> None:
        prepared = _prepared()
        prepared["sourceRevision"] = self.request["sourceRevision"]
        prepared["issues"][0]["sourceRevision"] = self.request["sourceRevision"]
        self.prepare()
        for state in ("prepared", "dispatching"):
            if state == "dispatching":
                self.dispatch()
            with self.subTest(state=state):
                plan = build_investigation_plan(
                    prepared, _judgments(), [], read_investigation_session_events(self.state),
                )
                self.assertEqual([], plan["requests"])
                self.assertEqual([], plan["activeInvestigationIds"])
                self.assertEqual([self.request["investigationId"]], plan["pendingInvestigationIds"])
                self.assertEqual(state, plan["pendingInvestigations"][0]["status"])

    def test_preparation_reconciles_reserved_registry_after_interruption(self) -> None:
        with patch.object(investigations, "append_jsonl_rows", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.prepare()
        self.assertEqual([], read_investigation_session_events(self.state))
        self.assertEqual("reserved", list_investigation_worktrees(self.state)[0]["state"])
        prepared = self.prepare()
        self.assertEqual(self.owner, prepared["attemptId"])
        self.assertEqual(1, len(read_investigation_session_events(self.state)))

    def test_durable_result_cannot_be_overwritten_by_a_fault_after_interruption(self) -> None:
        self.prepare()
        self.dispatch()
        real_append = investigations.append_jsonl_rows

        def interrupt(path, rows):
            if rows[0].get("status") == "completed":
                raise KeyboardInterrupt
            return real_append(path, rows)

        with patch.object(investigations, "append_jsonl_rows", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.complete()
        with self.assertRaisesRegex(ValueError, "result.*replay|replay.*result"):
            self.record_failure(execution_state="returned")
        self.assertEqual("returned", self.complete()["executionState"])

    def test_invalid_result_can_record_fault_even_with_ignored_worker_output(self) -> None:
        self.prepare()
        self.dispatch()
        (self.source / ".git" / "info" / "exclude").write_text("ignored-output\n", encoding="utf-8")
        (self.checkout / "ignored-output").write_text("worker output", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "clean"):
            self.complete()
        failed = self.record_failure(execution_state="returned", failure_category="invalid-result")
        self.assertEqual("failed", failed["status"])
        self.assertTrue(list_investigation_worktrees(self.state)[0]["workerStopped"])
        with self.assertRaisesRegex(ValueError, "clean"):
            cleanup_investigation_worktree(
                self.state, self.request, checkout=self.checkout, attempt_id=self.owner,
                recorded_at="2026-09-09T00:04:00Z", confirm_worker_stopped=True,
            )
        self.assertEqual("worker output", (self.checkout / "ignored-output").read_text())

    def test_abandoned_uncertain_dispatch_never_implies_execution_or_allows_relaunch(self) -> None:
        self.prepare()
        self.dispatch()
        abandoned = self.record_failure(status="abandoned", execution_state="unknown")
        self.assertEqual("unknown", abandoned["executionState"])
        self.assertIsNone(abandoned["runtimeSessionId"])
        with self.assertRaisesRegex(ValueError, "terminal|prepared"):
            self.dispatch()

    def test_result_output_must_be_unique_and_outside_state_and_worker_trees(self) -> None:
        for output in (self.state / f"{self.owner}.json", self.checkout / f"{self.owner}.json",
                       self.root / "results" / "shared.json"):
            with self.subTest(output=output), self.assertRaises(ValueError):
                record_investigation_session_event(
                    self.state, self.request, status="prepared", launch_mode="one-shot", session_id=None,
                    checkout=self.checkout, result_path=output, recorded_at="2026-09-08T23:00:00Z",
                )
        self.output.parent.mkdir()
        self.output.write_text("stale", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "exists"):
            self.prepare()

    def test_result_reader_rejects_another_path_and_symlink_to_the_expected_payload(self) -> None:
        self.prepare()
        self.dispatch()
        other = self.root / "other" / self.output.name
        other.parent.mkdir()
        other.write_text(json.dumps(self.response()), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "path"):
            load_one_shot_result(
                self.state, self.request, checkout=self.checkout, attempt_id=self.owner, result_path=other,
            )
        self.output.symlink_to(other)
        with self.assertRaisesRegex(ValueError, "symlink"):
            load_one_shot_result(
                self.state, self.request, checkout=self.checkout, attempt_id=self.owner, result_path=self.output,
            )

    def test_explicit_command_grants_remain_exact_through_completion_and_replay(self) -> None:
        command = [sys.executable, "-c", "print('bounded probe')"]
        prepared = self.prepare(reproduction_commands=[command])
        self.assertEqual([command], prepared["reproductionCommands"])
        with self.assertRaisesRegex(ValueError, "envelope"):
            self.prepare(reproduction_commands=[["different-command"]])
        self.dispatch()
        response = self.response()
        response["result"]["workLog"] = [{
            "kind": "command", "argv": command, "exitCode": 0,
            "output": "bounded probe", "finding": "The authorized command returned.",
        }]
        invalid = copy.deepcopy(response)
        invalid["result"]["workLog"][0]["argv"] = ["different-command"]
        with self.assertRaises(ValueError):
            self.complete(invalid)
        recorded = self.complete(response)
        self.assertEqual([command], recorded["reproductionCommands"])
        self.assertEqual(recorded, self.complete(response))

    def test_stale_attempt_response_is_rejected_and_third_attempt_cannot_prepare(self) -> None:
        self.prepare()
        self.dispatch()
        stale = self.response()
        self.record_failure()
        for attempt in (2, 3):
            self.request = {**self.request, "attempt": attempt}
            if attempt == 3:
                with self.assertRaisesRegex(ValueError, "attempt limit"):
                    self._allocate_attempt(self.request)
                continue
            allocation = provision_investigation_worktree(
                self.state, self.request, source_checkout=self.source, attempt=attempt,
                recorded_at="2026-09-09T00:04:00Z", managed_root=self.root / "workers",
            )
            self.checkout = Path(allocation["checkoutPath"])
            self.owner = allocation["ownershipId"]
            self.output = self.root / "results" / f"{self.owner}.json"
            self.prepare()
            self.dispatch()
            with self.assertRaisesRegex(ValueError, "attempt"):
                self.complete(stale)
            self.record_failure()

    def test_preparation_respects_three_reserved_slots_and_five_cycle_attempts(self) -> None:
        allocations = []
        for index in range(6):
            request = {**self.request, "investigationId": f"investigation:capacity-{index}"}
            record = provision_investigation_worktree(
                self.state, request, source_checkout=self.source, attempt=1,
                recorded_at="2026-09-08T23:00:00Z", managed_root=self.root / "workers",
            )
            allocations.append((request, record))

        def prepare(index):
            request, record = allocations[index]
            return record_investigation_session_event(
                self.state, request, status="prepared", session_id=None, launch_mode="one-shot",
                checkout=Path(record["checkoutPath"]),
                result_path=self.root / "results" / f"{record['ownershipId']}.json",
                recorded_at="2026-09-08T23:00:00Z",
            )

        def stop(index):
            request, record = allocations[index]
            return record_investigation_session_event(
                self.state, request, status="failed", session_id=None, attempt_id=record["ownershipId"],
                recorded_at="2026-09-08T23:01:00Z", execution_state="not-launched",
                execution_evidence="Cancelled before calling the launcher.", failure_reason="Operator cancellation.",
                confirm_worker_stopped=True,
            )

        for index in range(3):
            prepare(index)
        with self.assertRaisesRegex(ValueError, "Three"):
            prepare(3)
        for index in range(3):
            stop(index)
        for index in range(3, 5):
            prepare(index)
            stop(index)
        with self.assertRaisesRegex(ValueError, "Five"):
            prepare(5)
        self.assertEqual(5, sum(row["status"] == "prepared" for row in read_investigation_session_events(self.state)))
        self.assertEqual([], read_investigation_results(self.state))

    def _allocate_attempt(self, request: dict) -> dict:
        return provision_investigation_worktree(
            self.state, request, source_checkout=self.source, attempt=request["attempt"],
            recorded_at="2026-09-08T23:00:00Z", managed_root=self.root / "workers",
        )

    def _register_attempt(self, allocation: dict, mode: str) -> dict:
        one_shot = mode == "one-shot"
        return record_investigation_session_event(
            self.state, allocation["request"], status="prepared" if one_shot else "started",
            launch_mode=mode, session_id=None if one_shot else f"synthetic-fixture:{allocation['ownershipId']}",
            checkout=Path(allocation["checkoutPath"]), recorded_at="2026-09-08T23:01:00Z",
            **({"result_path": self.root / "results" / f"{allocation['ownershipId']}.json"} if one_shot else {}),
        )

    def _stop_attempt(self, allocation: dict, registration: dict) -> dict:
        return record_investigation_session_event(
            self.state, allocation["request"], status="failed", session_id=registration["sessionId"],
            recorded_at="2026-09-08T23:02:00Z", failure_reason="Synthetic registration ended without a worker launch.",
            confirm_worker_stopped=True,
            **({
                "attempt_id": allocation["ownershipId"], "execution_state": "not-launched",
                "execution_evidence": "Fixture did not invoke a runtime launcher.",
            } if registration.get("launchMode") == "one-shot" else {}),
        )

    def test_mixed_mode_capacity_rejects_before_binding_and_preserves_replay(self) -> None:
        self.prepare()
        self.dispatch()
        allocations = [
            self._allocate_attempt({**self.request, "investigationId": f"investigation:mixed-capacity-{index}"})
            for index in range(4)
        ]
        second = self._register_attempt(allocations[0], "one-shot")
        third = self._register_attempt(allocations[1], "one-shot")
        history = read_investigation_session_events(self.state)
        inventory = list_investigation_worktrees(self.state)
        for mode in ("one-shot", "resumable"):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "Three|slots|capacity"):
                self._register_attempt(allocations[2], mode)
            self.assertEqual(history, read_investigation_session_events(self.state))
            self.assertEqual(inventory, list_investigation_worktrees(self.state))
        legacy_request = {
            key: value for key, value in allocations[2]["request"].items()
            if key not in {"sourceRevision", "investigationScope"}
        }
        with self.assertRaisesRegex(ValueError, "Three|slots|capacity"):
            record_investigation_session_event(
                self.state, legacy_request, status="started", session_id="synthetic-legacy-fixture",
                checkout=Path(allocations[2]["checkoutPath"]), recorded_at="2026-09-08T23:01:00Z",
            )
        self.assertEqual(history, read_investigation_session_events(self.state))
        self.assertEqual(second, self._register_attempt(allocations[0], "one-shot"))
        self.assertFalse(self.dispatch()["dispatchAllowed"])

        self._stop_attempt(allocations[1], third)
        started = self._register_attempt(allocations[2], "resumable")
        self.assertEqual(started, self._register_attempt(allocations[2], "resumable"))
        with self.assertRaisesRegex(ValueError, "Three|slots|capacity"):
            self._register_attempt(allocations[3], "one-shot")
        self.assertEqual(6, len(read_investigation_session_events(self.state)))

    def test_mixed_mode_cycle_limit_counts_dispatch_only_once(self) -> None:
        extra = self._allocate_attempt({**self.request, "investigationId": "investigation:mixed-cycle-extra"})
        for index in range(5):
            allocation = self._allocate_attempt({
                **self.request, "investigationId": f"investigation:mixed-cycle-{index}",
            })
            registration = self._register_attempt(allocation, "one-shot" if index % 2 == 0 else "resumable")
            if index == 0:
                record_investigation_session_event(
                    self.state, allocation["request"], status="dispatching", session_id=None,
                    attempt_id=allocation["ownershipId"], recorded_at="2026-09-08T23:01:30Z",
                )
            terminal = self._stop_attempt(allocation, registration)
            self.assertEqual(terminal, self._stop_attempt(allocation, registration))
        history = read_investigation_session_events(self.state)
        inventory = list_investigation_worktrees(self.state)
        with self.assertRaisesRegex(ValueError, "Five|cycle"):
            self._allocate_attempt({**self.request, "investigationId": "investigation:cycle-preflight"})
        for mode in ("one-shot", "resumable"):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "Five|cycle"):
                self._register_attempt(extra, mode)
            self.assertEqual(history, read_investigation_session_events(self.state))
            self.assertEqual(inventory, list_investigation_worktrees(self.state))

    def test_attempt_limit_cannot_be_reset_by_switching_launch_modes(self) -> None:
        for first, second in (("one-shot", "resumable"), ("resumable", "one-shot")):
            request = {**self.request, "investigationId": f"investigation:mixed-attempt-{first}"}
            for attempt, mode in enumerate((first, second), start=1):
                allocation = self._allocate_attempt({**request, "attempt": attempt})
                registration = self._register_attempt(allocation, mode)
                self.assertEqual(registration, self._register_attempt(allocation, mode))
                self._stop_attempt(allocation, registration)
            history = read_investigation_session_events(self.state)
            inventory = list_investigation_worktrees(self.state)
            with self.subTest(first=first), self.assertRaisesRegex(ValueError, "attempt limit"):
                self._allocate_attempt({**request, "attempt": 3})
            self.assertEqual(history, read_investigation_session_events(self.state))
            self.assertEqual(inventory, list_investigation_worktrees(self.state))

    def test_cross_mode_duplicate_active_registration_is_rejected_without_mutation(self) -> None:
        for mode, dispatch in (("one-shot", False), ("one-shot", True), ("resumable", False)):
            allocation = self._allocate_attempt({
                **self.request, "investigationId": f"investigation:mixed-duplicate-{mode}-{dispatch}",
            })
            registration = self._register_attempt(allocation, mode)
            if dispatch:
                record_investigation_session_event(
                    self.state, allocation["request"], status="dispatching", session_id=None,
                    attempt_id=allocation["ownershipId"], recorded_at="2026-09-08T23:01:30Z",
                )
            history = read_investigation_session_events(self.state)
            inventory = list_investigation_worktrees(self.state)
            with self.subTest(mode=mode, dispatch=dispatch), self.assertRaisesRegex(ValueError, "active|pending"):
                self._register_attempt(allocation, "resumable" if mode == "one-shot" else "one-shot")
            self.assertEqual(history, read_investigation_session_events(self.state))
            self.assertEqual(inventory, list_investigation_worktrees(self.state))
            self._stop_attempt(allocation, registration)

    def test_resumable_registration_checks_owned_attempt_even_without_a_prior_session(self) -> None:
        for stale_request in (False, True):
            request = {**self.request, "investigationId": f"investigation:unregistered-attempt-{stale_request}"}
            for attempt in range(1, 3 if not stale_request else 2):
                allocation = self._allocate_attempt({**request, "attempt": attempt})
                finish_investigation_worktree(
                    self.state, allocation["request"], checkout=Path(allocation["checkoutPath"]),
                    session_id=None, status="failed", recorded_at="2026-09-08T23:01:00Z",
                    confirm_worker_stopped=True,
                )
            allocation_request = {**request, "attempt": 1 if stale_request else 3}
            allocation_options = {
                "source_checkout": self.source, "attempt": 2 if stale_request else 3,
                "managed_root": self.root / "workers", "recorded_at": "2026-09-08T23:02:00Z",
            }
            with self.assertRaisesRegex(ValueError, "bounded request attempt"):
                provision_investigation_worktree(self.state, allocation_request, **allocation_options)
            # Registration must independently protect an already allocated
            # checkout even when the earlier preflight cannot be relied on.
            with patch.object(investigations, "validate_investigation_admission"):
                extra = provision_investigation_worktree(self.state, allocation_request, **allocation_options)
            inventory = list_investigation_worktrees(self.state)
            with self.subTest(stale_request=stale_request), self.assertRaisesRegex(ValueError, "bounded request attempt"):
                self._register_attempt(extra, "resumable")
            self.assertEqual([], read_investigation_session_events(self.state))
            self.assertEqual(inventory, list_investigation_worktrees(self.state))

    def test_concurrent_mixed_registrations_cannot_take_the_same_last_slot(self) -> None:
        allocations = [
            self._allocate_attempt({**self.request, "investigationId": f"investigation:mixed-concurrent-{index}"})
            for index in range(4)
        ]
        self._register_attempt(allocations[0], "one-shot")
        self._register_attempt(allocations[1], "resumable")

        def register(candidate):
            allocation, mode = candidate
            try:
                return self._register_attempt(allocation, mode)["status"]
            except ValueError as error:
                self.assertIn("Three", str(error))
                return "denied"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(register, zip(allocations[2:], ("one-shot", "resumable"))))
        self.assertEqual(1, outcomes.count("denied"))
        self.assertEqual(3, len(read_investigation_session_events(self.state)))
        self.assertEqual(3, sum(
            row["state"] in {"bound", "reserved"} for row in list_investigation_worktrees(self.state)
        ))

    def test_unknown_dispatch_requires_timeout_and_stopped_confirmation_for_abandonment(self) -> None:
        self.prepare()
        self.dispatch()
        for when, stopped, error in (
            ("2026-09-08T23:02:00Z", True, "one-hour"),
            ("2026-09-09T00:02:00Z", False, "stopped"),
        ):
            with self.subTest(when=when), self.assertRaisesRegex(ValueError, error):
                record_investigation_session_event(
                    self.state, self.request, status="abandoned", session_id=None, attempt_id=self.owner,
                    recorded_at=when, execution_state="unknown", execution_evidence="Invocation status unavailable.",
                    failure_reason="Coordinator interrupted.", confirm_worker_stopped=stopped,
                )
        self.assertEqual("dispatching", read_investigation_session_events(self.state)[-1]["status"])
        self.assertEqual("reserved", list_investigation_worktrees(self.state)[0]["state"])

    def _cli(self, script: str, *arguments: str) -> dict:
        completed = subprocess.run(
            [sys.executable, str(Path("scripts").resolve() / script), *map(str, arguments)],
            capture_output=True, text=True,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        output = json.loads(completed.stdout)
        self.cli_outputs.append((script, output))
        return output

    def test_cli_one_shot_sequence_with_real_pinned_source_and_result_artifact(self) -> None:
        self.cli_outputs = []
        source_file = self.source / "sample.py"
        source_file.write_text("def value():\n    return 1\n", encoding="utf-8")
        for arguments in (["add", "--", "sample.py"], ["commit", "--quiet", "-m", "Add sample source"]):
            subprocess.run(["git", "--no-pager", "-C", str(self.source), *arguments], check=True, capture_output=True)
        # Give this exercise its own registry/root: the fixture's older frozen
        # allocation must not be replaced or silently repinned.
        exercise = self.root / "cli"
        exercise.mkdir()
        state, request = exercise / "state", _source_request(self.source)
        plan = exercise / "plan.json"
        plan.write_text(json.dumps({"repository": request["repository"], "requests": [request]}), encoding="utf-8")
        source_file.write_text("dirty coordinator source\n", encoding="utf-8")
        selection = ["--state-dir", state, "--plan", plan, "--investigation-id", request["investigationId"]]
        provisioned = self._cli(
            "investigation_worktree.py", "provision", *selection,
            "--source-checkout", self.source, "--attempt", "1", "--managed-root", exercise / "workers",
            "--recorded-at", "2026-09-09T00:00:00Z",
        )
        checkout = Path(provisioned["checkoutPath"])
        owner = provisioned["ownershipId"]
        result_path = exercise / "results" / f"{owner}.json"
        self.assertEqual(request["sourceRevision"], provisioned["sourceRevision"])
        self.assertEqual("ready", provisioned["state"])
        prepared = self._cli(
            "investigation_session.py", *selection, "--status", "prepared", "--launch-mode", "one-shot",
            "--checkout", checkout, "--result-path", result_path, "--recorded-at", "2026-09-09T00:01:00Z",
        )
        dispatch = self._cli(
            "investigation_session.py", *selection, "--status", "dispatching", "--attempt-id", owner,
            "--recorded-at", "2026-09-09T00:02:00Z",
        )
        self.assertTrue(dispatch["dispatchAllowed"])
        self.assertEqual(prepared["launchEnvelope"], dispatch["launchEnvelope"])
        # This is a deterministic synchronous process, not an AI runtime/session.
        # It receives the entire envelope in its first and only invocation.
        subprocess.run(
            [sys.executable, "-B", "-c",
             "import json, pathlib, sys\n"
             "envelope = json.loads(sys.argv[1])\n"
             "assert 'WORKTREE_PATH: ' + envelope['checkoutPath'] in envelope['launchEnvelope']\n"
             "assert pathlib.Path('sample.py').read_text() == 'def value():\\n    return 1\\n'\n"
             "result = json.loads(sys.argv[2])\n"
             "pathlib.Path(envelope['resultPath']).write_text(json.dumps({"
             "'schemaVersion': 1, 'attemptId': envelope['attemptId'], "
             "'requestFingerprint': envelope['requestFingerprint'], 'result': result}))\n",
             json.dumps(dispatch), json.dumps({
                 **_evidence_result(),
                 "workLog": [{"kind": "source", "path": "sample.py", "startLine": 1, "endLine": 2,
                              "finding": "The pinned function returns 1."}],
             })], cwd=checkout, check=True, capture_output=True, text=True,
        )
        completion_args = [
            *selection, "--attempt-id", owner, "--checkout", checkout, "--result", result_path,
            "--recorded-at", "2026-09-09T00:03:00Z", "--confirm-worker-stopped",
            "--execution-evidence", "Synchronous process returned exit code 0; no background children launched.",
        ]
        preflight = self._cli("investigation_result.py", *completion_args, "--preflight")
        self.assertTrue(preflight["valid"])
        self.assertEqual([], read_investigation_results(state))
        self.assertEqual("dispatching", read_investigation_session_events(state)[-1]["status"])
        recorded = self._cli("investigation_result.py", *completion_args)
        self.assertIsNone(recorded["sessionId"])
        self.assertIsNone(recorded["runtimeSessionId"])
        cleaned = self._cli(
            "investigation_worktree.py", "cleanup", "--state-dir", state, "--ownership-id", owner,
            "--recorded-at", "2026-09-09T00:04:00Z", "--confirm-worker-stopped",
        )
        self.assertEqual("cleaned", cleaned["state"])
        self.assertFalse(checkout.exists())
        self.assertTrue(result_path.is_file())
        self.assertEqual(recorded, self._cli("investigation_result.py", *completion_args))
        self.assertEqual("dirty coordinator source\n", source_file.read_text())


if __name__ == "__main__":
    unittest.main()
