from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ci_shepherd.workflow_loop.report import render_status
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


if __name__ == "__main__":
    unittest.main()
