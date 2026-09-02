from __future__ import annotations

import unittest
from datetime import UTC, datetime

from ci_shepherd.delegation_execution import (
    finalize_delegation_result,
    reserve_delegation_start,
)
from ci_shepherd.delegations import CapacityLimits


class ScriptedClient:
    def __init__(self, task_pages: list[list[object]]) -> None:
        self.task_pages = list(task_pages)

    def get(self, endpoint: str) -> object:
        task_id = endpoint.rsplit("/", 1)[-1]
        for page in self.task_pages:
            for record in page:
                if isinstance(record, dict) and record.get("id") == task_id:
                    return record
        raise KeyError(endpoint)

    def get_pages(self, endpoint: str, key: str | None = None) -> list[object]:
        if "/agents/" in endpoint:
            return self.task_pages.pop(0)
        return []


class FakeExecution:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self.events = events
        self.baselines: list[tuple[tuple[str, ...], datetime]] = []

    def action_events(self, *, repository: str) -> list[dict[str, object]]:
        return list(self.events)

    def append_delegation_baseline(
        self,
        *,
        task_ids: tuple[str, ...],
        at: datetime,
    ) -> dict[str, object]:
        self.baselines.append((task_ids, at))
        return {"taskIdsBefore": list(task_ids)}


def task(task_id: str) -> dict[str, object]:
    return {
        "id": task_id,
        "state": "queued",
        "created_at": "2026-09-01T12:00:00Z",
        "updated_at": "2026-09-01T12:00:00Z",
        "session_count": 1,
        "artifacts": [],
    }


class DelegationExecutionTests(unittest.TestCase):
    def test_reserves_capacity_and_records_baseline_before_assignment(self) -> None:
        now = datetime(2026, 9, 1, 16, tzinfo=UTC)
        execution = FakeExecution([])

        reservation = reserve_delegation_start(
            execution=execution,
            client=ScriptedClient([[task("existing")], []]),
            repository="owner/repo",
            limits=CapacityLimits(2, 3, 5),
            now=now,
        )

        self.assertTrue(reservation.permitted)
        self.assertEqual(("existing",), reservation.task_ids_before)
        self.assertEqual([(("existing",), now)], execution.baselines)

    def test_running_task_defers_without_recording_a_new_start(self) -> None:
        now = datetime(2026, 9, 1, 16, tzinfo=UTC)
        execution = FakeExecution(
            [
                {
                    "eventType": "delegation-baseline",
                    "actionId": "existing-action",
                    "recordedAt": "2026-09-01T14:00:00Z",
                    "operation": "assign-copilot",
                    "taskIdsBefore": [],
                },
                {
                    "eventType": "terminal",
                    "actionId": "existing-action",
                    "outcome": "executed",
                    "result": {"taskId": "running"},
                },
            ]
        )
        running_task = task("running")
        running_task["state"] = "in_progress"

        reservation = reserve_delegation_start(
            execution=execution,
            client=ScriptedClient([[running_task], []]),
            repository="owner/repo",
            limits=CapacityLimits(1, 10, 10),
            now=now,
        )

        self.assertFalse(reservation.permitted)
        self.assertIn("max_running_tasks", reservation.blocked_by)
        self.assertEqual([], execution.baselines)

    def test_finalization_binds_unique_new_task_or_remains_indeterminate(self) -> None:
        executed = {
            "actionId": "action:1",
            "attemptedAt": "2026-09-01T16:30:00Z",
            "outcome": "executed",
            "result": {"copilotAssigned": True},
        }

        new_task = task("new")
        new_task["created_at"] = "2026-09-01T16:00:01Z"
        reconciled = finalize_delegation_result(
            result=executed,
            task_ids_before=("existing",),
            client=ScriptedClient([[task("existing"), new_task], []]),
            repository="owner/repo",
            assignment_started_at="2026-09-01T16:00:00Z",
        )
        pending = finalize_delegation_result(
            result=executed,
            task_ids_before=("existing",),
            client=ScriptedClient([[task("existing")], []]),
            repository="owner/repo",
            assignment_started_at="2026-09-01T16:00:00Z",
        )

        self.assertEqual("executed", reconciled["outcome"])
        self.assertEqual("new", reconciled["result"]["taskId"])
        self.assertEqual("indeterminate", pending["outcome"])
        self.assertEqual("delegated_task_not_visible", pending["reason"])

    def test_finalization_does_not_bind_task_created_before_assignment(self) -> None:
        stale_task = task("unrelated")
        stale_task["created_at"] = "2026-09-01T15:59:59Z"

        result = finalize_delegation_result(
            result={
                "actionId": "action:1",
                "attemptedAt": "2026-09-01T16:00:00Z",
                "outcome": "executed",
                "result": {"copilotAssigned": True},
            },
            task_ids_before=(),
            client=ScriptedClient([[stale_task], []]),
            repository="owner/repo",
            assignment_started_at="2026-09-01T16:00:00Z",
        )

        self.assertEqual("indeterminate", result["outcome"])
        self.assertEqual("delegated_task_not_visible", result["reason"])
