from __future__ import annotations

import unittest
from types import SimpleNamespace

from ci_shepherd.delegation_observer import _human_identity, observe_delegations
from ci_shepherd.delegations import PullRequestState, TaskState


class ScriptedClient:
    def __init__(
        self,
        pages: dict[tuple[str, str | None], list[object]],
        records: dict[str, object] | None = None,
    ) -> None:
        self.pages = pages
        self.records = records or {}
        self.calls: list[tuple[str, str | None]] = []

    def get(self, endpoint: str) -> object:
        self.calls.append((endpoint, None))
        return self.records[endpoint]

    def get_pages(self, endpoint: str, key: str | None = None) -> list[object]:
        self.calls.append((endpoint, key))
        return self.pages[(endpoint, key)]


class DelegationObserverTests(unittest.TestCase):
    def test_human_identity_requires_structured_non_bot_account(self) -> None:
        self.assertTrue(_human_identity({"login": "maintainer", "type": "User"}))
        self.assertFalse(
            _human_identity({"login": "automation[bot]", "type": "Bot"})
        )
        self.assertIsNone(_human_identity({"login": "maintainer"}))
        self.assertIsNone(_human_identity({"type": "User"}))

    def test_names_missing_owned_task_evidence(self) -> None:
        with self.assertRaisesRegex(
            RuntimeError,
            "owned_task_inventory_incomplete:missing-task",
        ):
            observe_delegations(
                ScriptedClient({}, {}),
                "owner/repo",
                owned_task_ids={"missing-task"},
            )

    def test_rejects_incomplete_repository_running_inventory(self) -> None:
        repository = "owner/repo"
        running_endpoint = (
            f"/agents/repos/{repository}/tasks"
            "?state=queued%2Cin_progress&is_archived=false&per_page=100"
        )

        class IncompleteClient(ScriptedClient):
            def get_paged_inventory(
                self,
                endpoint: str,
                key: str | None = None,
            ) -> object:
                self.calls.append((endpoint, key))
                return SimpleNamespace(items=(), pages=1, complete=False)

        client = IncompleteClient({}, {})
        with self.assertRaisesRegex(
            RuntimeError,
            "repository_running_task_inventory_incomplete:1_pages",
        ):
            observe_delegations(
                client,
                repository,
                owned_task_ids=set(),
            )

        self.assertEqual([(running_endpoint, "tasks")], client.calls)

    def test_observes_agent_tasks_without_guessing_absent_pull_state(
        self,
    ) -> None:
        repository = "owner/repo"
        owned_task_endpoint = (
            f"/agents/repos/{repository}/tasks/task-1"
        )
        running_task_endpoint = (
            f"/agents/repos/{repository}/tasks"
            "?state=queued%2Cin_progress&is_archived=false&per_page=100"
        )
        pull_endpoint = (
            f"/repos/{repository}/pulls"
            "?head=owner%3Acopilot%2Ftask-1&state=all&per_page=100"
        )
        pull_detail_endpoint = f"/repos/{repository}/pulls/201"
        issue_endpoint = f"/repos/{repository}/issues/42"
        task_record = {
            "id": "task-1",
            "state": "completed",
            "created_at": "2026-09-01T12:00:00Z",
            "updated_at": "2026-09-01T13:00:00Z",
            "session_count": 1,
            "artifacts": [
                {
                    "type": "pull",
                    "provider": "github",
                    "data": {"id": 101, "global_id": "PR_open"},
                },
                {
                    "type": "branch",
                    "provider": "github",
                    "data": {
                        "head_ref": "copilot/task-1",
                        "base_ref": "main",
                    },
                },
                {
                    "type": "pull",
                    "provider": "github",
                    "data": {"id": 102, "global_id": ""},
                },
            ],
        }
        client = ScriptedClient(
            {
                (running_task_endpoint, "tasks"): [],
                (pull_endpoint, None): [
                    {
                        "id": 101,
                        "number": 201,
                        "node_id": "PR_open",
                        "state": "closed",
                        "merged_at": "2026-09-01T14:00:00Z",
                        "draft": False,
                    }
                ],
            },
            {
                owned_task_endpoint: task_record,
                pull_detail_endpoint: {
                    "id": 101,
                    "number": 201,
                    "node_id": "PR_open",
                    "state": "closed",
                    "merged_at": "2026-09-01T14:00:00Z",
                    "draft": False,
                    "changed_files": 4,
                    "head": {"sha": "current-head"},
                },
                issue_endpoint: {
                    "number": 42,
                    "state": "open",
                    "assignees": [{"login": "copilot-swe-agent[bot]"}],
                },
            },
        )

        observation = observe_delegations(
            client,
            repository,
            owned_task_ids={"task-1"},
            owned_issue_numbers={42},
        )

        self.assertTrue(observation.evidence.owned_task_inventory_complete)
        self.assertTrue(observation.evidence.pull_request_inventory_complete)
        self.assertEqual(TaskState.COMPLETED, observation.tasks[0].state)
        self.assertTrue(observation.issues[0].is_open)
        self.assertTrue(observation.issues[0].copilot_assigned)
        self.assertEqual(
            {101: {"headSha": "current-head"}},
            observation.pull_request_sources,
        )
        self.assertEqual(
            [
                (101, "PR_open", PullRequestState.MERGED, False, 4),
                (102, None, PullRequestState.UNKNOWN, False, None),
            ],
            [
                (
                    pull.database_id,
                    pull.global_id,
                    pull.state,
                    pull.is_draft,
                    pull.changed_files,
                )
                for pull in observation.pull_requests
            ],
        )
        self.assertEqual(
            [
                (owned_task_endpoint, None),
                (running_task_endpoint, "tasks"),
                (pull_endpoint, None),
                (pull_detail_endpoint, None),
                (issue_endpoint, None),
            ],
            client.calls,
        )
