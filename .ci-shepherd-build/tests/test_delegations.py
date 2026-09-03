from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from ci_shepherd.delegations import (
    CapacityLimits,
    CapacityEvidence,
    DelegatedIssue,
    DelegatedPullRequest,
    DelegationStart,
    PullRequestAssociation,
    PullRequestState,
    StartOutcome,
    TaskLifecycle,
    TaskState,
    active_owned_task_ids_from_events,
    decide_new_start,
    derive_capacity_usage,
    derive_delegation_tracking,
    derive_task_lifecycle,
    delegation_starts_from_events,
    normalize_agent_task,
    reconcile_started_task,
    render_delegation_status_section,
)


class AgentTaskNormalizationTests(unittest.TestCase):
    def test_normalizes_official_agent_task_record(self) -> None:
        task = normalize_agent_task(
            {
                "id": "task-19841",
                "state": "completed",
                "created_at": "2026-08-31T12:00:00Z",
                "updated_at": "2026-09-01T12:00:00Z",
                "session_count": 2,
                "artifacts": [
                    {
                        "type": "pull",
                        "provider": "github",
                        "data": {
                            "id": 4413552641,
                            "global_id": "PR_kwDOExample",
                        },
                    }
                ],
            }
        )

        self.assertEqual("task-19841", task.task_id)
        self.assertEqual(TaskState.COMPLETED, task.state)
        self.assertEqual(2, task.session_count)
        self.assertEqual("github", task.pull_artifacts[0].provider)
        self.assertEqual(4413552641, task.pull_artifacts[0].database_id)
        self.assertEqual("PR_kwDOExample", task.pull_artifacts[0].global_id)


class TaskLifecycleTests(unittest.TestCase):
    def test_derives_lifecycle_from_task_state_and_pull_request_association(
        self,
    ) -> None:
        task = normalize_agent_task(
            {
                "id": "task-1",
                "state": "queued",
                "created_at": "2026-08-31T12:00:00Z",
                "updated_at": "2026-09-01T12:00:00Z",
                "session_count": 1,
                "artifacts": [],
            }
        )

        expected = [
            (
                TaskState.QUEUED,
                PullRequestAssociation.NONE,
                TaskLifecycle.RUNNING,
                False,
            ),
            (
                TaskState.IN_PROGRESS,
                PullRequestAssociation.PENDING,
                TaskLifecycle.RUNNING,
                False,
            ),
            (
                TaskState.COMPLETED,
                PullRequestAssociation.ASSOCIATED,
                TaskLifecycle.COMPLETED,
                False,
            ),
            (
                TaskState.COMPLETED,
                PullRequestAssociation.NONE,
                TaskLifecycle.HANDOFF_REQUIRED,
                True,
            ),
            (
                TaskState.COMPLETED,
                PullRequestAssociation.PENDING,
                TaskLifecycle.HANDOFF_REQUIRED,
                True,
            ),
            *[
                (
                    state,
                    PullRequestAssociation.ASSOCIATED,
                    TaskLifecycle.HANDOFF_REQUIRED,
                    True,
                )
                for state in (
                    TaskState.FAILED,
                    TaskState.IDLE,
                    TaskState.WAITING_FOR_USER,
                    TaskState.TIMED_OUT,
                    TaskState.CANCELLED,
                )
            ],
        ]

        for state, association, lifecycle, requires_handoff in expected:
            with self.subTest(state=state, association=association):
                result = derive_task_lifecycle(
                    replace(
                        task,
                        state=state,
                        updated_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
                    ),
                    association=association,
                )
                self.assertEqual(lifecycle, result.lifecycle)
                self.assertEqual(requires_handoff, result.requires_handoff)


class TaskAssociationTests(unittest.TestCase):
    def test_normalizes_minimal_legal_agent_task_response(self) -> None:
        task = normalize_agent_task(
            {
                "id": "task-minimal",
                "state": "queued",
                "created_at": "2026-09-01T12:00:00Z",
            }
        )

        self.assertEqual(0, task.session_count)
        self.assertEqual(task.created_at, task.updated_at)
        self.assertEqual((), task.artifacts)

    def test_reconciles_only_one_new_task_from_the_pre_assignment_inventory(
        self,
    ) -> None:
        base_record = {
            "state": "queued",
            "created_at": "2026-09-01T12:00:00Z",
            "updated_at": "2026-09-01T12:00:00Z",
            "session_count": 1,
            "artifacts": [],
        }
        tasks = [
            normalize_agent_task({"id": task_id, **base_record})
            for task_id in ("existing-task", "new-task")
        ]

        unique = reconcile_started_task(
            task_ids_before={"existing-task"},
            tasks_after=tasks,
        )
        missing = reconcile_started_task(
            task_ids_before={"existing-task", "new-task"},
            tasks_after=tasks,
        )
        ambiguous = reconcile_started_task(
            task_ids_before=set(),
            tasks_after=tasks,
        )

        self.assertEqual("new-task", unique.task_id)
        self.assertIsNone(unique.problem)
        self.assertIsNone(missing.task_id)
        self.assertEqual("delegated_task_not_visible", missing.problem)
        self.assertIsNone(ambiguous.task_id)
        self.assertEqual("delegated_task_association_ambiguous", ambiguous.problem)


class DelegationEventTests(unittest.TestCase):
    def test_retired_tasks_are_removed_from_active_owned_inventory(self) -> None:
        events = [
            {
                "eventType": "delegation-baseline",
                "actionId": "action:1",
                "recordedAt": "2026-09-01T14:00:00Z",
                "operation": "assign-copilot",
                "target": {"kind": "issue", "number": 42},
                "taskIdsBefore": [],
            },
            {
                "eventType": "terminal",
                "actionId": "action:1",
                "outcome": "executed",
                "result": {"taskId": "task-1"},
            },
            {
                "eventType": "delegation-retired",
                "repository": "owner/repo",
                "taskId": "task-1",
            },
        ]

        self.assertEqual(frozenset(), active_owned_task_ids_from_events(events))

    def test_derives_pending_and_reconciled_starts_from_action_events(self) -> None:
        events = [
            {
                "eventType": "delegation-baseline",
                "actionId": "pending",
                "recordedAt": "2026-09-01T14:00:00Z",
                "operation": "assign-copilot",
                "taskIdsBefore": ["existing"],
            },
            {
                "eventType": "delegation-baseline",
                "actionId": "reconciled",
                "recordedAt": "2026-09-01T15:00:00Z",
                "operation": "assign-copilot",
                "taskIdsBefore": ["existing"],
            },
            {
                "eventType": "terminal",
                "actionId": "reconciled",
                "outcome": "executed",
                "result": {"taskId": "new-task"},
            },
            {
                "eventType": "delegation-baseline",
                "actionId": "preflight-failed",
                "recordedAt": "2026-09-01T15:30:00Z",
                "operation": "assign-copilot",
                "taskIdsBefore": ["existing", "new-task"],
            },
            {
                "eventType": "terminal",
                "actionId": "preflight-failed",
                "outcome": "stale",
            },
        ]

        starts = delegation_starts_from_events(events)

        self.assertEqual(2, len(starts))
        self.assertEqual(StartOutcome.INDETERMINATE, starts[0].outcome)
        self.assertIsNone(starts[0].task_id)
        self.assertEqual(StartOutcome.STARTED, starts[1].outcome)
        self.assertEqual("new-task", starts[1].task_id)

    def test_tracking_preserves_issue_task_and_pull_request_relationship(self) -> None:
        task_record = normalize_agent_task(
            {
                "id": "task-1",
                "state": "completed",
                "created_at": "2026-09-01T14:00:00Z",
                "updated_at": "2026-09-01T15:00:00Z",
                "session_count": 1,
                "artifacts": [
                    {
                        "type": "pull",
                        "provider": "github",
                        "data": {"id": 101, "global_id": "PR_101"},
                    }
                ],
            }
        )
        pull_request = DelegatedPullRequest(
            database_id=101,
            global_id="PR_101",
            state=PullRequestState.OPEN,
            is_draft=True,
        )
        events = [
            {
                "eventType": "delegation-baseline",
                "actionId": "action:1",
                "recordedAt": "2026-09-01T14:00:00Z",
                "operation": "assign-copilot",
                "repository": "owner/repo",
                "target": {"kind": "issue", "number": 42},
                "taskIdsBefore": [],
            },
            {
                "eventType": "terminal",
                "actionId": "action:1",
                "outcome": "executed",
                "result": {"taskId": "task-1"},
            },
        ]

        tracking = derive_delegation_tracking(
            events=events,
            tasks=[task_record],
            pull_requests=[pull_request],
        )

        self.assertEqual(
            {
                "actionId": "action:1",
                "repository": "owner/repo",
                "issueNumber": 42,
                "startedAt": "2026-09-01T14:00:00Z",
                "taskId": "task-1",
                "taskState": "completed",
                "lifecycle": "completed",
                "requiresHuman": False,
                "pullRequests": [
                    {
                        "databaseId": 101,
                        "globalId": "PR_101",
                        "state": "open",
                        "isDraft": True,
                    }
                ],
            },
            tracking[0],
        )

    def test_completed_task_with_unknown_pull_state_requires_handoff(self) -> None:
        task_record = normalize_agent_task(
            {
                "id": "task-1",
                "state": "completed",
                "created_at": "2026-09-01T14:00:00Z",
                "artifacts": [
                    {
                        "type": "pull",
                        "provider": "github",
                        "data": {"id": 101, "global_id": "PR_101"},
                    }
                ],
            }
        )
        events = [
            {
                "eventType": "delegation-baseline",
                "actionId": "action:1",
                "recordedAt": "2026-09-01T14:00:00Z",
                "operation": "assign-copilot",
                "repository": "owner/repo",
                "target": {"kind": "issue", "number": 42},
                "taskIdsBefore": [],
            },
            {
                "eventType": "terminal",
                "actionId": "action:1",
                "outcome": "executed",
                "result": {"taskId": "task-1"},
            },
        ]

        tracking = derive_delegation_tracking(
            events=events,
            tasks=[task_record],
            pull_requests=[
                DelegatedPullRequest(
                    database_id=101,
                    global_id="PR_101",
                    state=PullRequestState.UNKNOWN,
                    is_draft=False,
                )
            ],
        )

        self.assertEqual("handoff_required", tracking[0]["lifecycle"])
        self.assertTrue(tracking[0]["requiresHuman"])

    def test_completed_task_with_closed_unmerged_pull_requires_handoff(
        self,
    ) -> None:
        task_record = normalize_agent_task(
            {
                "id": "task-1",
                "state": "completed",
                "created_at": "2026-09-01T14:00:00Z",
                "artifacts": [
                    {
                        "type": "pull",
                        "provider": "github",
                        "data": {"id": 101, "global_id": "PR_101"},
                    }
                ],
            }
        )
        tracking = derive_delegation_tracking(
            events=[
                {
                    "eventType": "delegation-baseline",
                    "actionId": "action:1",
                    "recordedAt": "2026-09-01T14:00:00Z",
                    "operation": "assign-copilot",
                    "repository": "owner/repo",
                    "target": {"kind": "issue", "number": 42},
                    "taskIdsBefore": [],
                },
                {
                    "eventType": "terminal",
                    "actionId": "action:1",
                    "outcome": "executed",
                    "result": {"taskId": "task-1"},
                },
            ],
            tasks=[task_record],
            pull_requests=[
                DelegatedPullRequest(
                    database_id=101,
                    global_id="PR_101",
                    state=PullRequestState.CLOSED,
                    is_draft=False,
                )
            ],
            issues=[
                DelegatedIssue(
                    number=42,
                    is_open=False,
                    copilot_assigned=False,
                )
            ],
        )

        self.assertEqual("handoff_required", tracking[0]["lifecycle"])
        self.assertTrue(tracking[0]["requiresHuman"])

    def test_missing_assigned_task_requires_handoff(self) -> None:
        tracking = derive_delegation_tracking(
            events=[
                {
                    "eventType": "delegation-baseline",
                    "actionId": "action:1",
                    "recordedAt": "2026-09-01T14:00:00Z",
                    "operation": "assign-copilot",
                    "repository": "owner/repo",
                    "target": {"kind": "issue", "number": 42},
                    "taskIdsBefore": [],
                },
                {
                    "eventType": "terminal",
                    "actionId": "action:1",
                    "outcome": "executed",
                    "result": {"taskId": "missing-task"},
                },
            ],
            tasks=[],
            pull_requests=[],
        )

        self.assertEqual("handoff_required", tracking[0]["lifecycle"])
        self.assertTrue(tracking[0]["requiresHuman"])

    def test_closed_issue_does_not_hide_a_running_task(self) -> None:
        task = normalize_agent_task(
            {
                "id": "task-1",
                "state": "in_progress",
                "created_at": "2026-09-01T14:00:00Z",
                "artifacts": [],
            }
        )
        tracking = derive_delegation_tracking(
            events=[
                {
                    "eventType": "delegation-baseline",
                    "actionId": "action:1",
                    "recordedAt": "2026-09-01T14:00:00Z",
                    "operation": "assign-copilot",
                    "repository": "owner/repo",
                    "target": {"kind": "issue", "number": 42},
                    "taskIdsBefore": [],
                },
                {
                    "eventType": "terminal",
                    "actionId": "action:1",
                    "outcome": "executed",
                    "result": {"taskId": "task-1"},
                },
            ],
            tasks=[task],
            pull_requests=[],
            issues=[
                DelegatedIssue(
                    number=42,
                    is_open=False,
                    copilot_assigned=True,
                )
            ],
        )

        self.assertEqual("running", tracking[0]["lifecycle"])
        self.assertFalse(tracking[0]["requiresHuman"])

    def test_report_surfaces_terminal_delegation_handoff(self) -> None:
        report = render_delegation_status_section(
            {
                "status": "complete",
                "records": [
                    {
                        "issueNumber": 42,
                        "taskId": "task-1",
                        "taskState": "failed",
                        "lifecycle": "handoff_required",
                        "requiresHuman": True,
                        "pullRequests": [],
                    }
                ],
            }
        )

        self.assertIn("| #42 | `task-1` | failed | none | required |", report)

    def test_report_surfaces_capacity_without_owned_delegations(self) -> None:
        report = render_delegation_status_section(
            {
                "status": "complete",
                "records": [],
                "capacity": {
                    "runningTasks": 0,
                    "startsInRolling24h": 0,
                    "openDelegatedPullRequests": 0,
                    "repositoryRunningTasks": 3,
                    "complete": True,
                    "problems": [],
                    "warnings": [],
                },
            }
        )

        self.assertIn(
            "| Shepherd active | Starts (24h) | Open delegated PRs "
            "| Repository active |",
            report,
        )
        self.assertIn("| 0 | 0 | 0 | 3 |", report)

    def test_report_discloses_eligible_delegation_proposals(self) -> None:
        report = render_delegation_status_section(
            {
                "status": "complete",
                "records": [],
            },
            {
                "proposals": [
                    {
                        "actionId": "snapshot:owner/repo:time:issue:42:assign-copilot",
                        "issueNumber": 42,
                        "operation": "assign-copilot",
                        "executionEligibility": {
                            "eligible": True,
                            "blockingReasons": [],
                        },
                    }
                ]
            },
        )

        self.assertIn("**1 executable delegation proposal**", report)
        self.assertIn(
            "| #42 | `snapshot:owner/repo:time:issue:42:assign-copilot` |",
            report,
        )
        self.assertIn("not included in production comment selection", report)


class CapacityAccountingTests(unittest.TestCase):
    def test_only_recent_missing_owned_tasks_block_new_starts(self) -> None:
        now = datetime(2026, 9, 1, 16, tzinfo=UTC)
        old = derive_capacity_usage(
            tasks=[],
            starts=[
                DelegationStart(
                    started_at=datetime(2026, 8, 1, 16, tzinfo=UTC),
                    outcome=StartOutcome.STARTED,
                    task_id="expired-task",
                )
            ],
            pull_requests=[],
            evidence=CapacityEvidence(),
            now=now,
        )
        recent = derive_capacity_usage(
            tasks=[],
            starts=[
                DelegationStart(
                    started_at=datetime(2026, 9, 1, 15, tzinfo=UTC),
                    outcome=StartOutcome.STARTED,
                    task_id="missing-task",
                )
            ],
            pull_requests=[],
            evidence=CapacityEvidence(),
            now=now,
        )

        self.assertTrue(old.complete)
        self.assertNotIn("owned_task_missing:expired-task", old.problems)
        self.assertFalse(recent.complete)
        self.assertIn("owned_task_missing:missing-task", recent.problems)

    def test_pending_successful_and_indeterminate_starts_count_and_fail_closed(
        self,
    ) -> None:
        now = datetime(2026, 9, 1, 16, tzinfo=UTC)
        starts = [
            DelegationStart(
                started_at=datetime(2026, 9, 1, 14, tzinfo=UTC),
                outcome=StartOutcome.STARTED,
                task_id=None,
            ),
            DelegationStart(
                started_at=datetime(2026, 9, 1, 15, tzinfo=UTC),
                outcome=StartOutcome.INDETERMINATE,
                task_id=None,
            ),
        ]
        usage = derive_capacity_usage(
            tasks=[],
            starts=starts,
            pull_requests=[],
            evidence=CapacityEvidence(),
            now=now,
        )

        self.assertEqual(2, usage.starts_in_rolling_24h)
        self.assertIn("started_task_association_pending", usage.problems)
        self.assertIn("indeterminate_start_pending", usage.problems)
        self.assertFalse(
            decide_new_start(
                usage,
                CapacityLimits(
                    max_running_tasks=10,
                    max_starts_per_rolling_24h=10,
                    max_open_delegated_prs=10,
                ),
            ).permitted
        )

        reconciled_task_ids = ("successful-task", "indeterminate-task")
        reconciled = derive_capacity_usage(
            tasks=[
                normalize_agent_task(
                    {
                        "id": task_id,
                        "state": "in_progress",
                        "created_at": "2026-09-01T14:00:00Z",
                        "updated_at": "2026-09-01T15:30:00Z",
                        "session_count": 1,
                        "artifacts": [],
                    }
                )
                for task_id in reconciled_task_ids
            ],
            starts=[
                replace(start, task_id=task_id)
                for start, task_id in zip(starts, reconciled_task_ids, strict=True)
            ],
            pull_requests=[],
            evidence=CapacityEvidence(),
            now=now,
        )

        self.assertTrue(reconciled.complete)
        self.assertEqual(2, reconciled.running_tasks)

    def test_completed_task_releases_execution_but_open_draft_pr_retains_pr_slot(
        self,
    ) -> None:
        now = datetime(2026, 9, 1, 16, tzinfo=UTC)
        task = normalize_agent_task(
            {
                "id": "task-19841",
                "state": "completed",
                "created_at": "2026-09-01T12:00:00Z",
                "updated_at": "2026-09-01T15:00:00Z",
                "session_count": 1,
                "artifacts": [
                    {
                        "type": "pull",
                        "provider": "github",
                        "data": {
                            "id": 4413552641,
                            "global_id": "PR_kwDO19842",
                        },
                    }
                ],
            }
        )
        usage = derive_capacity_usage(
            tasks=[task],
            starts=[
                DelegationStart(
                    started_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
                    outcome=StartOutcome.STARTED,
                    task_id=task.task_id,
                )
            ],
            pull_requests=[
                DelegatedPullRequest(
                    database_id=4413552641,
                    global_id="PR_kwDO19842",
                    state=PullRequestState.OPEN,
                    is_draft=True,
                )
            ],
            evidence=CapacityEvidence(),
            now=now,
        )

        self.assertEqual(0, usage.running_tasks)
        self.assertEqual(1, usage.open_delegated_prs)
        self.assertEqual((), usage.handoff_task_ids)
        self.assertTrue(usage.complete)
        self.assertFalse(
            decide_new_start(
                usage,
                CapacityLimits(
                    max_running_tasks=1,
                    max_starts_per_rolling_24h=2,
                    max_open_delegated_prs=1,
                ),
            ).permitted
        )
        self.assertTrue(
            decide_new_start(
                usage,
                CapacityLimits(
                    max_running_tasks=1,
                    max_starts_per_rolling_24h=2,
                    max_open_delegated_prs=2,
                ),
            ).permitted
        )

    def test_completed_task_without_pr_requires_handoff_after_reconciliation(
        self,
    ) -> None:
        now = datetime(2026, 9, 1, 16, tzinfo=UTC)
        task = normalize_agent_task(
            {
                "id": "completed-without-pr",
                "state": "completed",
                "created_at": "2026-09-01T12:00:00Z",
                "updated_at": "2026-09-01T15:00:00Z",
                "session_count": 1,
                "artifacts": [],
            }
        )
        start = DelegationStart(
            started_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
            outcome=StartOutcome.STARTED,
            task_id=task.task_id,
        )

        reconciled = derive_capacity_usage(
            tasks=[task],
            starts=[start],
            pull_requests=[],
            evidence=CapacityEvidence(),
            now=now,
        )
        pending = derive_capacity_usage(
            tasks=[task],
            starts=[start],
            pull_requests=[],
            evidence=CapacityEvidence(pull_request_inventory_complete=False),
            now=now,
        )

        self.assertEqual(("completed-without-pr",), reconciled.handoff_task_ids)
        self.assertEqual(("completed-without-pr",), pending.handoff_task_ids)
        self.assertEqual(
            TaskLifecycle.HANDOFF_REQUIRED,
            pending.task_lifecycles[0].lifecycle,
        )
        self.assertFalse(pending.complete)

    def test_rolling_window_excludes_start_at_exactly_24_hours(self) -> None:
        now = datetime(2026, 9, 1, 16, tzinfo=UTC)
        tasks = [
            normalize_agent_task(
                {
                    "id": task_id,
                    "state": "completed",
                    "created_at": "2026-08-20T12:00:00Z",
                    "updated_at": "2026-09-01T15:00:00Z",
                    "session_count": 1,
                    "artifacts": [],
                }
            )
            for task_id in ("at-boundary", "inside-window")
        ]
        starts = [
            DelegationStart(
                started_at=now.replace(day=31, month=8),
                outcome=StartOutcome.STARTED,
                task_id="at-boundary",
            ),
            DelegationStart(
                started_at=now.replace(day=31, month=8, microsecond=1),
                outcome=StartOutcome.INDETERMINATE,
                task_id="inside-window",
            ),
        ]
        usage = derive_capacity_usage(
            tasks=tasks,
            starts=starts,
            pull_requests=[],
            evidence=CapacityEvidence(),
            now=now,
        )
        boundary_only_usage = derive_capacity_usage(
            tasks=tasks[:1],
            starts=starts[:1],
            pull_requests=[],
            evidence=CapacityEvidence(),
            now=now,
        )
        limits = CapacityLimits(
            max_running_tasks=10,
            max_starts_per_rolling_24h=1,
            max_open_delegated_prs=10,
        )

        self.assertEqual(1, usage.starts_in_rolling_24h)
        self.assertFalse(decide_new_start(usage, limits).permitted)
        self.assertTrue(decide_new_start(boundary_only_usage, limits).permitted)

    def test_old_indeterminate_start_does_not_block_unrelated_work_forever(
        self,
    ) -> None:
        now = datetime(2026, 9, 2, 16, tzinfo=UTC)
        usage = derive_capacity_usage(
            tasks=[],
            starts=[
                DelegationStart(
                    started_at=now - timedelta(hours=24),
                    outcome=StartOutcome.INDETERMINATE,
                    task_id=None,
                )
            ],
            pull_requests=[],
            evidence=CapacityEvidence(),
            now=now,
        )

        self.assertTrue(usage.complete)
        self.assertEqual(0, usage.starts_in_rolling_24h)
        self.assertTrue(
            decide_new_start(
                usage,
                CapacityLimits(
                    max_running_tasks=1,
                    max_starts_per_rolling_24h=1,
                    max_open_delegated_prs=1,
                ),
            ).permitted
        )

    def test_externally_resumed_task_consumes_running_capacity_again(self) -> None:
        now = datetime(2026, 9, 1, 16, tzinfo=UTC)
        idle_task = normalize_agent_task(
            {
                "id": "resumable-task",
                "state": "idle",
                "created_at": "2026-08-20T12:00:00Z",
                "updated_at": "2026-09-01T15:00:00Z",
                "session_count": 1,
                "artifacts": [],
            }
        )
        start = DelegationStart(
            started_at=datetime(2026, 8, 20, 12, tzinfo=UTC),
            outcome=StartOutcome.STARTED,
            task_id=idle_task.task_id,
        )

        idle_usage = derive_capacity_usage(
            tasks=[idle_task],
            starts=[start],
            pull_requests=[],
            evidence=CapacityEvidence(),
            now=now,
        )
        resumed_usage = derive_capacity_usage(
            tasks=[
                replace(
                    idle_task,
                    state=TaskState.IN_PROGRESS,
                    updated_at=datetime(2026, 9, 1, 15, 30, tzinfo=UTC),
                    session_count=2,
                )
            ],
            starts=[start],
            pull_requests=[],
            evidence=CapacityEvidence(),
            now=now,
        )

        self.assertEqual(0, idle_usage.running_tasks)
        self.assertEqual(("resumable-task",), idle_usage.handoff_task_ids)
        self.assertEqual(1, resumed_usage.running_tasks)
        decision = decide_new_start(
            resumed_usage,
            CapacityLimits(
                max_running_tasks=1,
                max_starts_per_rolling_24h=10,
                max_open_delegated_prs=10,
            ),
        )
        self.assertFalse(decision.permitted)
        self.assertIn("max_running_tasks", decision.blocked_by)

    def test_unassigned_issue_does_not_release_executing_task_or_open_pr_slot(
        self,
    ) -> None:
        now = datetime(2026, 9, 1, 16, tzinfo=UTC)
        task = normalize_agent_task(
            {
                "id": "owned-task",
                "state": "in_progress",
                "created_at": "2026-09-01T12:00:00Z",
                "artifacts": [
                    {
                        "type": "pull",
                        "provider": "github",
                        "data": {"id": 101, "global_id": "PR_101"},
                    }
                ],
            }
        )
        usage = derive_capacity_usage(
            tasks=[task],
            starts=[
                DelegationStart(
                    started_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
                    outcome=StartOutcome.STARTED,
                    task_id=task.task_id,
                    issue_number=42,
                )
            ],
            pull_requests=[
                DelegatedPullRequest(
                    database_id=101,
                    global_id="PR_101",
                    state=PullRequestState.OPEN,
                    is_draft=True,
                )
            ],
            issues=[
                DelegatedIssue(
                    number=42,
                    is_open=True,
                    copilot_assigned=False,
                )
            ],
            evidence=CapacityEvidence(),
            now=now,
        )

        self.assertEqual(1, usage.running_tasks)
        self.assertEqual(1, usage.open_delegated_prs)
        self.assertEqual(TaskLifecycle.RUNNING, usage.task_lifecycles[0].lifecycle)

    def test_unassigned_issue_does_not_hide_failed_task_handoff(self) -> None:
        now = datetime(2026, 9, 1, 16, tzinfo=UTC)
        task = normalize_agent_task(
            {
                "id": "owned-task",
                "state": "failed",
                "created_at": "2026-09-01T12:00:00Z",
                "artifacts": [],
            }
        )
        usage = derive_capacity_usage(
            tasks=[task],
            starts=[
                DelegationStart(
                    started_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
                    outcome=StartOutcome.STARTED,
                    task_id=task.task_id,
                    issue_number=42,
                )
            ],
            pull_requests=[],
            issues=[
                DelegatedIssue(
                    number=42,
                    is_open=True,
                    copilot_assigned=False,
                )
            ],
            evidence=CapacityEvidence(),
            now=now,
        )

        self.assertEqual(("owned-task",), usage.handoff_task_ids)
        self.assertEqual(
            TaskLifecycle.HANDOFF_REQUIRED,
            usage.task_lifecycles[0].lifecycle,
        )

    def test_queued_tasks_do_not_consume_active_session_capacity(self) -> None:
        now = datetime(2026, 9, 1, 16, tzinfo=UTC)
        queued = normalize_agent_task(
            {
                "id": "queued-task",
                "state": "queued",
                "created_at": "2026-08-01T12:00:00Z",
                "updated_at": "2026-08-01T12:00:00Z",
                "session_count": 0,
                "artifacts": [],
            }
        )
        usage = derive_capacity_usage(
            tasks=[queued],
            starts=[],
            pull_requests=[],
            evidence=CapacityEvidence(),
            now=now,
        )

        self.assertEqual(0, usage.running_tasks)
        self.assertEqual(0, usage.repository_running_tasks)

    def test_recent_retired_start_counts_without_blocking_on_association(
        self,
    ) -> None:
        now = datetime(2026, 9, 1, 16, tzinfo=UTC)
        usage = derive_capacity_usage(
            tasks=[],
            starts=[
                DelegationStart(
                    started_at=now - timedelta(hours=3),
                    outcome=StartOutcome.STARTED,
                    task_id="retired-task",
                    issue_number=42,
                )
            ],
            pull_requests=[],
            evidence=CapacityEvidence(),
            now=now,
            retired_task_ids=frozenset({"retired-task"}),
        )

        self.assertEqual(1, usage.starts_in_rolling_24h)
        self.assertEqual(0, usage.running_tasks)
        self.assertTrue(usage.complete)

    def test_incomplete_or_ambiguous_evidence_fails_closed(self) -> None:
        now = datetime(2026, 9, 1, 16, tzinfo=UTC)
        task = normalize_agent_task(
            {
                "id": "owned-task",
                "state": "completed",
                "created_at": "2026-08-20T12:00:00Z",
                "updated_at": "2026-09-01T15:00:00Z",
                "session_count": 1,
                "artifacts": [
                    {
                        "type": "pull",
                        "provider": "github",
                        "data": {
                            "id": 10,
                            "global_id": "PR_10",
                        },
                    }
                ],
            }
        )
        start = DelegationStart(
            started_at=datetime(2026, 8, 20, 12, tzinfo=UTC),
            outcome=StartOutcome.STARTED,
            task_id=task.task_id,
        )

        incomplete = derive_capacity_usage(
            tasks=[task],
            starts=[start],
            pull_requests=[],
            evidence=CapacityEvidence(),
            now=now,
        )
        ambiguous = derive_capacity_usage(
            tasks=[task],
            starts=[start],
            pull_requests=[
                DelegatedPullRequest(
                    database_id=10,
                    global_id="PR_other",
                    state=PullRequestState.OPEN,
                    is_draft=True,
                ),
                DelegatedPullRequest(
                    database_id=11,
                    global_id="PR_10",
                    state=PullRequestState.OPEN,
                    is_draft=False,
                ),
            ],
            evidence=CapacityEvidence(),
            now=now,
        )
        inventory_incomplete = derive_capacity_usage(
            tasks=[task],
            starts=[start],
            pull_requests=[],
            evidence=CapacityEvidence(pull_request_inventory_complete=False),
            now=now,
        )

        limits = CapacityLimits(
            max_running_tasks=10,
            max_starts_per_rolling_24h=10,
            max_open_delegated_prs=10,
        )
        self.assertIn(
            "task_pull_request_association_incomplete:owned-task",
            decide_new_start(incomplete, limits).blocked_by,
        )
        self.assertIn(
            "task_pull_request_association_ambiguous:owned-task",
            decide_new_start(ambiguous, limits).blocked_by,
        )
        self.assertIn(
            "pull_request_inventory_incomplete",
            decide_new_start(inventory_incomplete, limits).blocked_by,
        )

    def test_foreign_tasks_do_not_consume_shepherd_capacity(self) -> None:
        now = datetime(2026, 9, 1, 16, tzinfo=UTC)
        base_record = {
            "created_at": "2026-09-01T14:00:00Z",
            "updated_at": "2026-09-01T15:00:00Z",
            "session_count": 1,
        }
        owned = normalize_agent_task(
            {
                **base_record,
                "id": "owned-task",
                "state": "failed",
                "artifacts": [],
            }
        )
        external = normalize_agent_task(
            {
                **base_record,
                "id": "externally-assigned-copilot-task",
                "state": "in_progress",
                "artifacts": [
                    {
                        "type": "pull",
                        "provider": "github",
                        "data": {
                            "id": 20,
                            "global_id": "PR_20",
                        },
                    }
                ],
            }
        )

        usage = derive_capacity_usage(
            tasks=[owned, external],
            starts=[
                DelegationStart(
                    started_at=datetime(2026, 8, 20, 12, tzinfo=UTC),
                    outcome=StartOutcome.STARTED,
                    task_id=owned.task_id,
                )
            ],
            pull_requests=[
                DelegatedPullRequest(
                    database_id=20,
                    global_id="PR_20",
                    state=PullRequestState.OPEN,
                    is_draft=True,
                )
            ],
            evidence=CapacityEvidence(),
            now=now,
        )

        self.assertEqual(0, usage.running_tasks)
        self.assertEqual(0, usage.open_delegated_prs)
        self.assertEqual(1, usage.repository_running_tasks)
        self.assertEqual(0, usage.starts_in_rolling_24h)
        self.assertEqual(("owned-task",), usage.handoff_task_ids)
        self.assertTrue(usage.complete)
        self.assertTrue(
            decide_new_start(
                usage,
                CapacityLimits(
                    max_running_tasks=1,
                    max_starts_per_rolling_24h=1,
                    max_open_delegated_prs=1,
                ),
            ).permitted
        )

    def test_repository_running_ceiling_blocks_independently(self) -> None:
        now = datetime(2026, 9, 1, 16, tzinfo=UTC)
        foreign = normalize_agent_task(
            {
                "id": "foreign-task",
                "state": "in_progress",
                "created_at": "2026-09-01T14:00:00Z",
                "updated_at": "2026-09-01T15:00:00Z",
                "session_count": 1,
                "artifacts": [],
            }
        )
        usage = derive_capacity_usage(
            tasks=[foreign],
            starts=[],
            pull_requests=[],
            evidence=CapacityEvidence(),
            now=now,
        )

        decision = decide_new_start(
            usage,
            CapacityLimits(
                max_running_tasks=10,
                max_starts_per_rolling_24h=10,
                max_open_delegated_prs=10,
                max_repository_running_tasks=1,
            ),
        )

        self.assertFalse(decision.permitted)
        self.assertEqual(("max_repository_running_tasks",), decision.blocked_by)


if __name__ == "__main__":
    unittest.main()
