from __future__ import annotations

from datetime import UTC, datetime
import unittest

from ci_shepherd.delegations import active_owned_task_ids_from_events, delegation_records_from_events, derive_delegation_tracking, normalize_agent_task
from ci_shepherd.collector import InventoryResult
from tests.test_delegation_observer import ScriptedClient
from tests.test_scripts import load_script


class DelegationHistoryTests(unittest.TestCase):
    def test_empty_current_artifacts_do_not_erase_a_verified_binding(self) -> None:
        collect = load_script("collect")
        client = ScriptedClient({
            ("/agents/repos/owner/repo/tasks?state=queued%2Cin_progress&is_archived=false&per_page=100", "tasks"): [],
        }, {
            "/agents/repos/owner/repo/tasks/task-1": {
                "id": "task-1", "state": "failed", "created_at": "2026-09-02T18:00:00Z",
                "artifacts": [],
            },
            "/repos/owner/repo/pulls/201": {
                "id": 101, "number": 201, "node_id": "PR_101", "state": "closed",
                "merged_at": "2026-09-02T18:30:00Z", "draft": False, "changed_files": 4,
            },
            "/repos/owner/repo/issues/42": {"number": 42, "state": "open", "assignees": []},
        })
        status, _ = collect.observe_delegation_status(
            client, "owner/repo", events=self._retired_events()[:-1],
            now=datetime(2026, 9, 2, 19, tzinfo=UTC),
        )
        self.assertEqual("failed", status["records"][0]["taskState"])
        self.assertEqual("merged", status["records"][0]["attemptOutcome"])
        self.assertEqual(201, status["records"][0]["pullRequests"][0]["number"])

    def test_terminal_attempt_does_not_remove_open_tracker_from_issue_monitoring(self) -> None:
        collect = load_script("collect")
        issue = {"number": 42, "state": "open", "labels": ["quarantined-test"]}
        record = {
            **self._retired_events()[2]["record"], "requiresHuman": False,
            "attemptOutcome": "merged", "lifecycle": "completed",
        }
        result = collect.retain_tracked_delegations(
            InventoryResult([issue], [], {}, [], [], {}), [record],
        )
        self.assertEqual([issue], result.open_issues)
        self.assertEqual([], result.delegated_issues)

    def test_issue_lookup_failure_does_not_hide_its_verified_pull_request(self) -> None:
        collect = load_script("collect")
        client = ScriptedClient({
            ("/agents/repos/owner/repo/tasks?state=queued%2Cin_progress&is_archived=false&per_page=100", "tasks"): [],
        }, {
            "/repos/owner/repo/pulls/201": {
                "id": 101, "number": 201, "node_id": "PR_101", "state": "closed",
                "merged_at": "2026-09-02T18:30:00Z", "draft": False, "changed_files": 4,
            },
        })
        status, _ = collect.observe_delegation_status(
            client, "owner/repo", events=self._retired_events(),
            now=datetime(2026, 9, 2, 19, tzinfo=UTC),
        )
        record = status["records"][0]
        self.assertEqual("merged", record["attemptOutcome"])
        self.assertEqual("unavailable", record["issueObservation"])
        self.assertNotIn("issueOpen", record)

    def test_unavailable_pr_preserves_verified_history_without_claiming_current_state(self) -> None:
        collect = load_script("collect")
        client = ScriptedClient({
            ("/agents/repos/owner/repo/tasks?state=queued%2Cin_progress&is_archived=false&per_page=100", "tasks"): [],
        }, {
            "/repos/owner/repo/issues/42": {"number": 42, "state": "open", "assignees": []},
        })
        status, retired = collect.observe_delegation_status(
            client, "owner/repo", events=self._retired_events(),
            now=datetime(2026, 9, 2, 19, tzinfo=UTC),
        )
        record = status["records"][0]
        self.assertEqual("closed-unmerged", record["attemptOutcome"])
        self.assertEqual("association_pending", record["lifecycle"])
        self.assertEqual("unknown", record["pullRequests"][0]["state"])
        self.assertEqual("closed", record["pullRequests"][0]["lastKnownState"])
        self.assertTrue(record["requiresNewDecision"])
        self.assertFalse(status["capacity"]["complete"])
        self.assertEqual((), retired)

    def test_legacy_retirement_remains_unknown_and_keeps_issue_monitoring(self) -> None:
        collect = load_script("collect")
        events = self._retired_events()
        del events[2]
        client = ScriptedClient({
            ("/agents/repos/owner/repo/tasks?state=queued%2Cin_progress&is_archived=false&per_page=100", "tasks"): [],
        }, {
            "/repos/owner/repo/issues/42": {"number": 42, "state": "open", "assignees": []},
        })
        status, retired = collect.observe_delegation_status(
            client, "owner/repo", events=events, now=datetime(2026, 9, 2, 19, tzinfo=UTC),
        )
        self.assertEqual((), retired)
        record = status["records"][0]
        self.assertEqual("legacy-unknown", record["attemptOutcome"])
        self.assertTrue(record["requiresNewDecision"])
        self.assertEqual([], record["pullRequests"])
        self.assertTrue(record["issueOpen"])

    def test_corrupt_history_cannot_bind_a_different_issue_or_task(self) -> None:
        for changed in ({"issueNumber": 99}, {"taskId": "other-task"}, {"repository": "other/repo"}):
            with self.subTest(changed=changed):
                events = self._retired_events()
                events[2]["record"].update(changed)
                with self.assertRaisesRegex(ValueError, "assignment"):
                    delegation_records_from_events(events)

    def test_indeterminate_assignment_reconciles_to_one_task_in_same_history(self) -> None:
        events = self._retired_events()[:1]
        pending = derive_delegation_tracking(events=events, tasks=[], pull_requests=[])[0]
        events.extend([
            {"eventType": "delegation-observed", "actionId": pending["actionId"], "record": pending},
            {"eventType": "terminal", "actionId": pending["actionId"], "outcome": "executed",
             "result": {"taskId": "task-1"}},
        ])
        task = normalize_agent_task({
            "id": "task-1", "state": "queued", "created_at": "2026-09-02T18:00:00Z",
        })
        records = derive_delegation_tracking(events=events, tasks=[task], pull_requests=[])
        self.assertEqual(1, len(records))
        self.assertEqual(pending["actionId"], records[0]["actionId"])
        self.assertEqual("task-1", records[0]["taskId"])
        self.assertEqual("running", records[0]["lifecycle"])

    def test_reopened_pull_resumes_same_attempt_and_retains_pr_capacity(self) -> None:
        collect = load_script("collect")
        events = self._retired_events()
        client = ScriptedClient({
            ("/agents/repos/owner/repo/tasks?state=queued%2Cin_progress&is_archived=false&per_page=100", "tasks"): [],
        }, {
            "/repos/owner/repo/pulls/201": {
                "id": 101, "node_id": "PR_101", "number": 201, "state": "open",
                "merged_at": None, "draft": True, "changed_files": 4,
            },
            "/repos/owner/repo/issues/42": {"number": 42, "state": "closed", "assignees": []},
        })
        status, retired = collect.observe_delegation_status(
            client, "owner/repo", events=events, now=datetime(2026, 9, 2, 19, tzinfo=UTC),
        )
        self.assertEqual((), retired)
        record = status["records"][0]
        self.assertEqual("assignment:42", record["actionId"])
        self.assertEqual("awaiting_pull_request", record["lifecycle"])
        self.assertFalse(record["retired"])
        self.assertFalse(record["issueOpen"])
        self.assertFalse(record["copilotAssigned"])
        self.assertTrue(record["requiresNewDecision"])
        self.assertEqual(1, status["capacity"]["openDelegatedPullRequests"])
        self.assertTrue(status["capacity"]["complete"])
        events.append({"eventType": "delegation-observed", "actionId": record["actionId"], "record": record})
        self.assertEqual(frozenset({"task-1"}), active_owned_task_ids_from_events(events))
        again, _ = collect.observe_delegation_status(
            client, "owner/repo", events=events, now=datetime(2026, 9, 2, 19, tzinfo=UTC),
        )
        self.assertEqual(status["episodeOrdinals"], again["episodeOrdinals"])

    @staticmethod
    def _retired_events() -> list[dict[str, object]]:
        return [
            {
                "eventType": "delegation-baseline", "actionId": "assignment:42",
                "recordedAt": "2026-09-02T18:00:00Z", "operation": "assign-copilot",
                "repository": "owner/repo", "target": {"kind": "issue", "number": 42},
                "taskIdsBefore": [],
            },
            {"eventType": "terminal", "actionId": "assignment:42", "outcome": "executed",
             "result": {"taskId": "task-1"}},
            {
                "eventType": "delegation-observed", "actionId": "assignment:42",
                "record": {
                    "actionId": "assignment:42", "repository": "owner/repo", "issueNumber": 42,
                    "startedAt": "2026-09-02T18:00:00Z", "taskId": "task-1", "taskState": "failed",
                    "lifecycle": "closed_unmerged", "attemptOutcome": "closed-unmerged",
                    "requiresHuman": True, "requiresNewDecision": True, "retired": True,
                    "pullRequests": [{"databaseId": 101, "globalId": "PR_101", "number": 201,
                                      "state": "closed", "isDraft": False, "changedFiles": 4}],
                },
            },
            {"eventType": "delegation-retired", "taskId": "task-1"},
        ]

    def test_merged_attempt_remains_visible_and_monitored_after_retirement(self) -> None:
        collect = load_script("collect")
        now = datetime(2026, 9, 2, 19, tzinfo=UTC)
        events = [
            {
                "eventType": "delegation-baseline", "actionId": "assignment:42",
                "recordedAt": "2026-09-02T18:00:00Z", "operation": "assign-copilot",
                "repository": "owner/repo", "target": {"kind": "issue", "number": 42},
                "taskIdsBefore": [],
            },
            {"eventType": "terminal", "actionId": "assignment:42", "outcome": "executed",
             "result": {"taskId": "task-1"}},
        ]
        task = {
            "id": "task-1", "state": "failed", "created_at": "2026-09-02T18:00:00Z",
            "artifacts": [
                {"type": "pull", "provider": "github", "data": {"id": 101, "global_id": "PR_101"}},
                {"type": "branch", "provider": "github",
                 "data": {"head_ref": "copilot/fix", "base_ref": "main"}},
            ],
        }
        pull = {
            "id": 101, "node_id": "PR_101", "number": 201, "state": "closed",
            "merged_at": "2026-09-02T18:30:00Z", "draft": False, "changed_files": 4,
        }
        issue = {"number": 42, "state": "open", "assignees": [{"login": "copilot-swe-agent[bot]", "type": "Bot"}]}
        running = "/agents/repos/owner/repo/tasks?state=queued%2Cin_progress&is_archived=false&per_page=100"
        client = ScriptedClient({
            (running, "tasks"): [],
            ("/repos/owner/repo/pulls?head=owner%3Acopilot%2Ffix&state=all&per_page=100", None): [pull],
        }, {
            "/agents/repos/owner/repo/tasks/task-1": task,
            "/repos/owner/repo/pulls/201": pull,
            "/repos/owner/repo/issues/42": issue,
        })
        status, retired = collect.observe_delegation_status(client, "owner/repo", events=events, now=now)
        self.assertEqual(("task-1",), retired)
        self.assertEqual("merged", status["records"][0]["attemptOutcome"])
        self.assertTrue(status["records"][0]["requiresNewDecision"])
        events.extend([
            {"eventType": "delegation-observed", "actionId": "assignment:42",
             "record": status["records"][0]},
            {"eventType": "delegation-retired", "taskId": "task-1"},
        ])
        client.records.pop("/agents/repos/owner/repo/tasks/task-1")
        client.calls.clear()
        again, _ = collect.observe_delegation_status(client, "owner/repo", events=events, now=now)
        self.assertEqual(1, len(again["records"]))
        record = again["records"][0]
        self.assertEqual("merged", record["attemptOutcome"])
        self.assertEqual("failed", record["taskState"])
        self.assertEqual("not-requested", record["taskObservation"])
        self.assertTrue(record["requiresNewDecision"])
        self.assertTrue(record["issueOpen"])
        self.assertEqual(201, record["pullRequests"][0]["number"])
        self.assertIn(("/repos/owner/repo/pulls/201", None), client.calls)
        self.assertIn(("/repos/owner/repo/issues/42", None), client.calls)
        self.assertEqual(0, again["capacity"]["openDelegatedPullRequests"])
