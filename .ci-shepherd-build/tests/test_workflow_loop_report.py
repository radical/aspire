from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ci_shepherd.workflow_loop.report import render_status
from ci_shepherd.workflow_loop.reader import JobManifest, ManifestJob
from ci_shepherd.workflow_loop.models import ActionKind, ItemPhase, TaskState
from ci_shepherd.workflow_loop.state import WorkflowLoopStore
from test_workflow_loop_worker import LATER, NOW, _request
from ci_shepherd.workflow_loop.worker import WorkerPacketPaths


def _record_proposal(
    store: WorkflowLoopStore,
    item,
    kind: ActionKind,
    *,
    request_overrides: dict[str, object] | None = None,
    include_request: bool = True,
    action_id: str | None = None,
) -> None:
    request = {
        "workerId": f"worker-{item.id}",
        "itemId": item.id,
        "episode": item.episode,
        "evidenceFingerprint": item.evidence_fingerprint,
        "round": item.followup_count + (1 if kind is ActionKind.FOLLOW_UP else 0),
        "issueNumber": item.issue_number,
        "taskId": item.task_id,
        "pullRequestNumber": item.pull_request_number,
        "followupCount": item.followup_count,
    }
    request.update(request_overrides or {})
    payload: dict[str, object] = {
        "result": {
            "itemId": item.id,
            "episode": item.episode,
            "evidenceFingerprint": item.evidence_fingerprint,
            "decision": (
                "follow_up"
                if kind is ActionKind.FOLLOW_UP
                else "assign"
            ),
        },
        "write": {"repository": "owner/repo"},
    }
    if include_request:
        payload["request"] = request
    store.record_history(
        item.id,
        recorded_at=NOW,
        event="proposed",
        summary="PROPOSED: external effect was not invoked.",
        detail={
            "status": "PROPOSED",
            "actionId": action_id or (
                f"{request['workerId']}:{item.id}:{item.episode}:"
                f"{item.evidence_fingerprint}:{kind.value}:1"
            ),
            "itemId": item.id,
            "episode": item.episode,
            "kind": kind.value,
            "ordinal": 1,
            "payload": payload,
        },
    )


class WorkflowLoopReportTests(unittest.TestCase):
    def test_complete_status_renders_persisted_state_without_github(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            store = WorkflowLoopStore(
                state_directory,
                repository="owner/repo",
                branch="main",
            )
            store.initialize()
            paths = WorkerPacketPaths.create(state_directory, "seed")
            item = store.upsert_failure(_request(paths).failure_run, NOW)
            store.update_item(
                replace(
                    item,
                    wait_reason="human\u001b[31m",
                    issue_number=17,
                    task_id="task-123",
                    pull_request_number=23,
                    latest_error="needs\u0007attention",
                ),
                history_event="waiting",
                summary="Waiting for a human.",
                detail={},
            )
            store.start_pass("pass-1", NOW)
            store.finish_pass(
                "pass-1",
                completed_at="2026-09-17T20:00:02Z",
                duration_ms=2000,
                github_request_count=7,
                discovered_items=1,
                progressed_items=1,
                confirmed_assignments=0,
                error=None,
            )

            report = render_status(
                state_directory,
                repository="owner/repo",
                branch="main",
                now=datetime(2026, 9, 17, 20, 10, tzinfo=UTC),
                capacity_limit=2,
            )

            self.assertEqual(
                "\n".join(
                    (
                        "CI shepherd: owner/repo branch=main",
                        "Capacity: 1/2 active",
                        "Last pass: pass-1 duration=2.000s github_requests=7 "
                        "discovered=1 progressed=1 assignments=0 status=ok",
                        "Time to first confirmed assignment: unavailable",
                        "",
                        "Workflow health: unavailable (no persisted manifests)",
                        "",
                        "Cause groups: none",
                        "Leaf policy: none",
                        "",
                        "Legacy migration: not applicable (no migration receipt)",
                        "",
                        "Item 1: observing_failure activity=active elapsed=10m00s",
                        "  waiting: human\\x1b[31m",
                        "  checked: 2026-09-17T20:00:00Z",
                        "  progressed: 2026-09-17T20:00:00Z",
                        "  run: https://github.com/owner/repo/actions/runs/101",
                        "  issue: https://github.com/owner/repo/issues/17",
                        "  pull request: https://github.com/owner/repo/pull/23",
                        "  task ID: task-123",
                        "  latest action: none",
                        "  would do: none",
                        "  error: needs\\x07attention",
                    )
                ),
                report,
            )

    def test_status_labels_exact_ready_proposal_current(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            store = WorkflowLoopStore(
                state_directory,
                repository="owner/repo",
                branch="main",
            )
            store.initialize()
            paths = WorkerPacketPaths.create(state_directory, "seed")
            item = store.upsert_failure(_request(paths).failure_run, NOW)
            item = replace(
                item,
                phase=ItemPhase.READY_FOR_ACTION,
                last_judged_fingerprint=item.evidence_fingerprint,
            )
            store.update_item(
                item,
                history_event="judgment-ready",
                summary="Exact action is ready.",
                detail={},
            )
            _record_proposal(store, item, ActionKind.CREATE_ISSUE)

            report = render_status(
                state_directory,
                repository="owner/repo",
                branch="main",
                now=datetime(2026, 9, 17, 20, 10, tzinfo=UTC),
            )

            self.assertIn("Proposals: current=1 stale=0", report)
            self.assertIn("PROPOSED CURRENT create_issue", report)

    def test_status_keeps_old_proposal_stale_after_evidence_and_target_advance(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            store = WorkflowLoopStore(
                state_directory,
                repository="owner/repo",
                branch="main",
            )
            store.initialize()
            paths = WorkerPacketPaths.create(state_directory, "seed")
            item = store.upsert_failure(_request(paths).failure_run, NOW)
            item = replace(
                item,
                phase=ItemPhase.READY_FOR_ACTION,
                last_judged_fingerprint=item.evidence_fingerprint,
            )
            store.update_item(
                item,
                history_event="judgment-ready",
                summary="Issue creation is ready.",
                detail={},
            )
            _record_proposal(store, item, ActionKind.CREATE_ISSUE)

            changed_job = replace(
                _request(paths).failure_run.jobs[0],
                log_excerpt="src/Changed.cs(1): error CS1002: ; expected",
            )
            current = store.upsert_failure(
                replace(
                    _request(paths).failure_run,
                    jobs=(changed_job,),
                ),
                LATER,
            )
            current = replace(
                current,
                phase=ItemPhase.READY_FOR_ACTION,
                last_judged_fingerprint=current.evidence_fingerprint,
                issue_number=17,
            )
            store.update_item(
                current,
                history_event="issue-bound",
                summary="Issue is now attached to current evidence.",
                detail={},
            )
            _record_proposal(store, current, ActionKind.ASSIGN_COPILOT)

            report = render_status(
                state_directory,
                repository="owner/repo",
                branch="main",
                now=datetime(2026, 9, 17, 20, 10, tzinfo=UTC),
            )

            self.assertIn("Proposals: current=1 stale=1", report)
            self.assertIn("PROPOSED STALE create_issue", report)
            self.assertIn("PROPOSED CURRENT assign_copilot", report)

    def test_status_fail_closes_stale_proposal_identity_and_state(self) -> None:
        cases = (
            ("item-mismatch", ItemPhase.READY_FOR_ACTION, {"itemId": 99}, True),
            ("episode-mismatch", ItemPhase.READY_FOR_ACTION, {"episode": 99}, True),
            ("recovered", ItemPhase.RECOVERED, {}, True),
            ("superseded", ItemPhase.SUPERSEDED, {}, True),
            ("generic-effect", ItemPhase.READY_FOR_ACTION, {}, False),
        )
        for name, phase, overrides, include_request in cases:
            with self.subTest(name=name), TemporaryDirectory() as scratch:
                state_directory = Path(scratch) / "state"
                store = WorkflowLoopStore(
                    state_directory,
                    repository="owner/repo",
                    branch="main",
                )
                store.initialize()
                paths = WorkerPacketPaths.create(state_directory, "seed")
                item = store.upsert_failure(_request(paths).failure_run, NOW)
                item = replace(
                    item,
                    phase=phase,
                    last_judged_fingerprint=item.evidence_fingerprint,
                )
                store.update_item(
                    item,
                    history_event="state-changed",
                    summary="Persist the exact current state.",
                    detail={},
                )
                _record_proposal(
                    store,
                    item,
                    ActionKind.CREATE_ISSUE,
                    request_overrides=overrides,
                    include_request=include_request,
                )

                report = render_status(
                    state_directory,
                    repository="owner/repo",
                    branch="main",
                    now=datetime(2026, 9, 17, 20, 10, tzinfo=UTC),
                )

                self.assertIn("Proposals: current=0 stale=1", report)
                self.assertIn("PROPOSED STALE create_issue", report)

    def test_status_rejects_proposal_with_mismatched_action_identity(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            store = WorkflowLoopStore(
                state_directory,
                repository="owner/repo",
                branch="main",
            )
            store.initialize()
            paths = WorkerPacketPaths.create(state_directory, "seed")
            item = store.upsert_failure(_request(paths).failure_run, NOW)
            item = replace(
                item,
                phase=ItemPhase.READY_FOR_ACTION,
                last_judged_fingerprint=item.evidence_fingerprint,
            )
            store.update_item(
                item,
                history_event="judgment-ready",
                summary="Exact action is ready.",
                detail={},
            )
            _record_proposal(
                store,
                item,
                ActionKind.CREATE_ISSUE,
                action_id="worker-99:99:99:fnv1a64:ffffffffffffffff:create_issue:1",
            )

            report = render_status(
                state_directory,
                repository="owner/repo",
                branch="main",
                now=datetime(2026, 9, 17, 20, 10, tzinfo=UTC),
            )

            self.assertIn("Proposals: current=0 stale=1", report)
            self.assertIn("PROPOSED STALE create_issue", report)

    def test_status_marks_old_followup_round_stale_after_state_advances(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            store = WorkflowLoopStore(
                state_directory,
                repository="owner/repo",
                branch="main",
            )
            store.initialize()
            paths = WorkerPacketPaths.create(state_directory, "seed")
            item = store.upsert_failure(_request(paths).failure_run, NOW)
            item = replace(
                item,
                phase=ItemPhase.READY_FOR_ACTION,
                last_judged_fingerprint=item.evidence_fingerprint,
                issue_number=17,
                task_id="task-1",
                task_state=TaskState.IDLE,
                pull_request_number=23,
                followup_count=1,
            )
            store.update_item(
                item,
                history_event="follow-up-ready",
                summary="The first follow-up round is ready.",
                detail={},
            )
            _record_proposal(store, item, ActionKind.FOLLOW_UP)
            current_report = render_status(
                state_directory,
                repository="owner/repo",
                branch="main",
                now=datetime(2026, 9, 17, 20, 10, tzinfo=UTC),
            )
            self.assertIn("Proposals: current=1 stale=0", current_report)
            self.assertIn("PROPOSED CURRENT follow_up", current_report)

            store.update_item(
                replace(item, followup_count=2),
                history_event="follow-up-advanced",
                summary="The item advanced beyond the old proposal.",
                detail={},
            )

            report = render_status(
                state_directory,
                repository="owner/repo",
                branch="main",
                now=datetime(2026, 9, 17, 20, 10, tzinfo=UTC),
            )

            self.assertIn("Proposals: current=0 stale=1", report)
            self.assertIn("PROPOSED STALE follow_up", report)

    def test_report_includes_manifest_group_policy_and_recovery_details(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            store = WorkflowLoopStore(
                state_directory,
                repository="owner/repo",
                branch="main",
            )
            store.initialize()
            failure = _request(
                WorkerPacketPaths.create(state_directory, "seed")
            ).failure_run
            job = replace(
                failure.jobs[0],
                log_excerpt=(
                    "/repo/source.cs(1,1): error CS1002: ; expected"
                ),
            )
            follower_job = replace(
                job,
                job_id=901,
                key=replace(job.key, name="Build / macOS"),
            )
            aggregate = replace(
                job,
                job_id=902,
                key=replace(job.key, name="CI / Final Results"),
                log_excerpt=None,
            )
            failure = replace(
                failure,
                jobs=(job, follower_job, aggregate),
            )
            store.record_job_manifest(
                failure,
                JobManifest(
                    run=failure,
                    jobs=(
                        ManifestJob(job, failure.head_sha, ("Build",)),
                        ManifestJob(
                            follower_job,
                            failure.head_sha,
                            ("Build",),
                        ),
                        ManifestJob(
                            aggregate,
                            failure.head_sha,
                            ("Fail if any dependency failed",),
                        ),
                    ),
                    total_count=3,
                    complete=True,
                    errors=(),
                    request_count=1,
                ),
                NOW,
                {
                    job.job_id: "leaf",
                    follower_job.job_id: "leaf",
                    aggregate.job_id: "aggregate",
                },
            )
            item = store.upsert_leaf_failure(failure, job.key, NOW)
            item = store.record_cause(item.id, failure, observed_at=NOW)
            follower = store.upsert_leaf_failure(
                failure,
                follower_job.key,
                NOW,
            )
            follower = store.record_cause(
                follower.id,
                failure,
                observed_at=NOW,
            )
            store.update_item(
                replace(
                    item,
                    phase=ItemPhase.RECOVERED,
                    wait_reason=None,
                    recovered_run_id=102,
                    recovered_at="2026-09-17T20:05:00Z",
                ),
                history_event="recovery-observed",
                summary="Exact leaf passed independently.",
                detail={
                    "classification": "deterministic_test",
                    "recommendedResponse": "repair",
                    "recoveryWitnesses": [
                        {
                            "runId": 102,
                            "attempt": 1,
                            "headSha": "fedcba9876543210",
                            "jobId": 1001,
                            "leafCaseKey": item.case_key,
                        }
                    ],
                },
            )
            store.update_item(
                replace(
                    follower,
                    phase=ItemPhase.NEEDS_ATTENTION,
                    wait_reason="cause_conflict",
                    task_id="task-no-pr",
                    task_state=TaskState.IDLE,
                    latest_error=(
                        "Exact cause evidence conflicts with existing ownership."
                    ),
                ),
                history_event="cause-conflict",
                summary="Group frozen for attention.",
                detail={
                    "classification": "repository_infra",
                    "recommendedResponse": "repair",
                },
            )

            report = render_status(
                state_directory,
                repository="owner/repo",
                branch="main",
                now=datetime(2026, 9, 17, 20, 10, tzinfo=UTC),
                capacity_limit=2,
            )

            self.assertIn("Workflow health:", report)
            self.assertIn("inventory=complete returned=3 total=3", report)
            self.assertIn("aggregate fallout=1", report)
            self.assertIn("Cause groups:", report)
            self.assertIn("state=frozen-cause-conflict", report)
            self.assertNotIn("state=recovered leader=", report)
            self.assertIn("role=follower-of-", report)
            self.assertIn("classification=deterministic_test response=repair", report)
            self.assertIn("Recovery witness: run=102 attempt=1", report)
            self.assertIn(
                "task result: unavailable "
                "(no authoritative pull request/result channel)",
                report,
            )
            self.assertIn("Legacy migration: not applicable", report)


if __name__ == "__main__":
    unittest.main()
