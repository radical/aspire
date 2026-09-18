from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ci_shepherd.workflow_loop.report import render_status
from ci_shepherd.workflow_loop.reader import JobManifest, ManifestJob
from ci_shepherd.workflow_loop.models import ItemPhase, TaskState
from ci_shepherd.workflow_loop.state import WorkflowLoopStore
from test_workflow_loop_worker import NOW, _request
from ci_shepherd.workflow_loop.worker import WorkerPacketPaths


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
