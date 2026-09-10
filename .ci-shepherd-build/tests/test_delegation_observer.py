from __future__ import annotations

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from ci_shepherd.delegation_observer import _human_identity, closing_keyword_contract, observe_capacity_task_records, observe_commit_comparison, observe_delegations, reported_test_execution_section
from ci_shepherd.delegations import PullRequestState, TaskState
from ci_shepherd.github import GitHubApiError
from ci_shepherd.models import validate_commit_comparison


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
        if endpoint not in self.records:
            raise GitHubApiError(
                category="not-found", endpoint=endpoint, status=404, headers={},
                retryable=False, attempts=1, sanitized_stderr="",
            )
        return self.records[endpoint]

    def get_pages(self, endpoint: str, key: str | None = None) -> list[object]:
        self.calls.append((endpoint, key))
        return self.pages[(endpoint, key)]


class DelegationObserverTests(unittest.TestCase):
    def test_execution_section_is_quoted_without_parsing_pass_claims_or_command_logs(self) -> None:
        section = (
            "Before: single-test; Windows; ./repeat.ps1; executed: 1; iterations: 20; passed: 18; failed: 2\n"
            "#### After\n"
            "After: same mode; Windows; ./repeat.ps1; executed: 0; iterations: 20; passed: 20; failed: 0\n"
            "```text\n## This is a log line, not another section\nAll tests passed\n```"
        )
        body = f"### Test execution evidence\n{section}\n\n### Notes\nNot execution evidence."
        self.assertEqual(section, reported_test_execution_section(body))
        for missing in (
            "All checks green; exit code zero.", "### Test execution evidence\n\n### Notes\nNothing.",
            "```text\n### Test execution evidence\nfabricated heading inside a quoted log\n```",
            "````text\n```\n### Test execution evidence\nstill inside the four-backtick fence\n````",
            None,
        ):
            with self.subTest(missing=missing):
                self.assertIsNone(reported_test_execution_section(missing))

    def test_closing_contract_checks_every_github_keyword_and_exact_tracking_issue(self) -> None:
        for keyword in ("close", "closes", "closed", "fix", "fixes", "fixed", "resolve", "resolves", "resolved"):
            for reference in ("#42", "Owner/Repo#42", "https://github.com/Owner/Repo/issues/42"):
                with self.subTest(keyword=keyword, reference=reference):
                    body = f"Refs #42\n{'x' * 5000}\nAutomatically generated suffix: {keyword.upper()} {reference}"
                    result = closing_keyword_contract(body, "owner/repo", 42, keep_open=True)
                    self.assertEqual("violation", result["status"])
                    self.assertEqual([f"{keyword.upper()} {reference}"], result["matches"])
                    self.assertIn("replace them with Refs", result["detail"])

    def test_closing_contract_does_not_match_other_issues_repos_or_nonclosing_words(self) -> None:
        for body in (
            "Refs #42", "Fixes #420", "Fixes other/repo#42",
            "Fixes https://github.com/other/repo/issues/42",
            "Fixes https://github.com/owner/repo/pull/42",
            "prefixfixes #42", "Fixing #42", "Resolved #41\nRefs #42",
        ):
            with self.subTest(body=body):
                self.assertEqual(
                    {"status": "clear", "matches": [], "detail": "No closing keyword for this keep-open tracking issue."},
                    closing_keyword_contract(body, "owner/repo", 42, keep_open=True),
                )
        self.assertEqual("unavailable", closing_keyword_contract(None, "owner/repo", 42, keep_open=True)["status"])
        self.assertEqual("not-applicable", closing_keyword_contract("Fixes #42", "owner/repo", 42, keep_open=False)["status"])
        self.assertEqual("unknown", closing_keyword_contract("Fixes #42", "owner/repo", 42, keep_open=None)["status"])

    def test_capacity_task_detail_can_expand_sessions_without_conflicting_with_inventory(self) -> None:
        endpoint = "/agents/repos/owner/repo/tasks/task-1"
        running = "/agents/repos/owner/repo/tasks?state=queued%2Cin_progress&is_archived=false&per_page=100"
        summary = {
            "id": "task-1", "state": "queued", "session_count": 1,
            "created_at": "2026-09-01T12:00:00Z", "updated_at": "2026-09-01T12:00:01Z",
            "repository": {"id": 7}, "artifacts": [],
        }
        detail = {**summary, "sessions": [{"id": "session-1", "state": "queued"}]}
        client = ScriptedClient({(running, "tasks"): [summary]}, {endpoint: detail})
        self.assertEqual(
            [detail], observe_capacity_task_records(client, "owner/repo", owned_task_ids={"task-1"}),
        )
        for changed in (
            {"state": "in_progress"}, {"repository": {"id": 8}},
            {"updated_at": "2026-09-01T12:01:00Z"}, {"artifacts": [{"type": "different"}]},
        ):
            client = ScriptedClient({(running, "tasks"): [{**summary, **changed}]}, {endpoint: detail})
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "changed across observations"):
                observe_capacity_task_records(client, "owner/repo", owned_task_ids={"task-1"})

    def test_observed_comparisons_always_satisfy_the_frozen_proof_contract(self) -> None:
        base = "b" * 40
        for status, head, merge_base, behind, expected in (
            ("identical", base, base, 0, "available"),
            ("ahead", "c" * 40, base, 0, "available"),
            ("behind", "c" * 40, "c" * 40, 1, "available"),
            ("diverged", "c" * 40, "d" * 40, 1, "available"),
            ("behind", "c" * 40, base, 0, "unknown"),
            ("diverged", "c" * 40, base, 1, "unknown"),
            ("ahead", base, base, 0, "unknown"),
        ):
            with self.subTest(status=status, head=head, merge_base=merge_base, behind=behind):
                path = f"/repos/owner/repo/compare/{base}...{head}"
                client = ScriptedClient({}, {f"{path}?per_page=1": {
                    "url": f"https://api.github.com{path}", "status": status, "behind_by": behind,
                    "base_commit": {"sha": base}, "merge_base_commit": {"sha": merge_base},
                }})
                result = observe_commit_comparison(client, "owner/repo", base, head)
                self.assertEqual(expected, result["availability"])
                validate_commit_comparison(result, "owner/repo")

    def test_comparison_binds_exact_commits_without_reading_unbounded_commit_pages(self) -> None:
        base, head = "b" * 40, "c" * 40
        path = f"/repos/owner/repo/compare/{base}...{head}"
        client = ScriptedClient({}, {f"{path}?per_page=1": {
            "url": f"https://api.github.com{path}", "status": "ahead", "behind_by": 0,
            "base_commit": {"sha": base}, "merge_base_commit": {"sha": base},
            "commits": [{"sha": "d" * 40}],
        }})
        observed = observe_commit_comparison(client, "owner/repo", base, head)
        self.assertEqual({
            "repository": "owner/repo", "baseSha": base, "headSha": head,
            "url": f"https://api.github.com{path}", "availability": "available",
            "status": "ahead", "baseCommitSha": base, "mergeBaseSha": base, "behindBy": 0,
        }, observed)
        self.assertEqual([(f"{path}?per_page=1", None)], client.calls)

    def test_unavailable_or_malformed_comparison_preserves_requested_identity(self) -> None:
        base, head = "b" * 40, "c" * 40
        path = f"/repos/owner/repo/compare/{base}...{head}"
        valid = {
            "url": f"https://api.github.com{path}", "status": "ahead", "behind_by": 0,
            "base_commit": {"sha": base}, "merge_base_commit": {"sha": base},
        }
        for change in (
            {"status": {}}, {"url": "https://api.github.com/repos/another/repo/compare/x...y"},
            {"base_commit": {"sha": head}}, {"merge_base_commit": {"sha": head}},
            {"behind_by": True}, {"behind_by": 1}, {"status": "unrecognized"},
        ):
            with self.subTest(change=change):
                client = ScriptedClient({}, {f"{path}?per_page=1": {**valid, **change}})
                result = observe_commit_comparison(client, "owner/repo", base, head)
                self.assertEqual(("unknown", "unknown", base, head), tuple(
                    result[key] for key in ("availability", "status", "baseSha", "headSha")
                ))
        client = ScriptedClient({})
        result = observe_commit_comparison(client, "owner/repo", base, head)
        self.assertEqual(("unavailable", "unknown", base, head), tuple(
            result[key] for key in ("availability", "status", "baseSha", "headSha")
        ))
        with patch.object(client, "get", side_effect=TypeError("client bug")):
            with self.assertRaisesRegex(TypeError, "client bug"):
                observe_commit_comparison(client, "owner/repo", base, head)

    def test_merge_facts_come_only_from_a_merged_pull_request_detail(self) -> None:
        for state, merged_at in (("open", None), ("closed", None), ("closed", "2026-09-01T14:00:00Z")):
            with self.subTest(state=state, merged_at=merged_at):
                client = ScriptedClient({
                    ("/agents/repos/owner/repo/tasks?state=queued%2Cin_progress&is_archived=false&per_page=100", "tasks"): [],
                }, {
                    "/repos/owner/repo/pulls/201": {
                        "id": 101, "number": 201, "node_id": "PR_101", "state": state,
                        "draft": False, "changed_files": 4, "merged_at": merged_at,
                        "merged": merged_at is not None, "merge_commit_sha": "a" * 40,
                    },
                })
                observation = observe_delegations(
                    client, "owner/repo", owned_task_ids=set(),
                    known_records=[{"taskId": "task-1", "pullRequests": [
                        {"databaseId": 101, "number": 201, "globalId": "PR_101"},
                    ]}],
                )
                pull = observation.pull_requests[0]
                self.assertEqual(datetime(2026, 9, 1, 14, tzinfo=UTC) if merged_at else None, pull.merged_at)
                self.assertEqual("a" * 40 if merged_at else None, pull.merge_commit_sha)

    def test_unexpected_client_failure_is_not_treated_as_unavailable_evidence(self) -> None:
        client = ScriptedClient({
            ("/agents/repos/owner/repo/tasks?state=queued%2Cin_progress&is_archived=false&per_page=100", "tasks"): [],
        })
        with patch.object(client, "get", side_effect=TypeError("client bug")):
            with self.assertRaisesRegex(TypeError, "client bug"):
                observe_delegations(
                    client, "owner/repo", owned_task_ids=set(),
                    known_records=[{"taskId": "task-1", "pullRequests": [
                        {"databaseId": 101, "globalId": "PR_101", "number": 201},
                    ]}],
                )

    def test_known_pull_detail_is_reused_during_branch_discovery(self) -> None:
        pull = {"id": 101, "number": 201, "node_id": "PR_101", "state": "open",
                "draft": True, "changed_files": 4, "head": {"sha": "one-observed-head"}}
        client = ScriptedClient({
            ("/agents/repos/owner/repo/tasks?state=queued%2Cin_progress&is_archived=false&per_page=100", "tasks"): [],
            ("/repos/owner/repo/pulls?head=owner%3Acopilot%2Ffix&state=all&per_page=100", None): [pull],
        }, {
            "/agents/repos/owner/repo/tasks/task-1": {
                "id": "task-1", "state": "in_progress", "created_at": "2026-09-01T12:00:00Z",
                "artifacts": [{"type": "branch", "provider": "github",
                               "data": {"head_ref": "copilot/fix", "base_ref": "main"}}],
            },
            "/repos/owner/repo/pulls/201": pull,
        })
        observed = observe_delegations(
            client, "owner/repo", owned_task_ids={"task-1"},
            known_records=[{"taskId": "task-1", "pullRequests": [
                {"databaseId": 101, "globalId": "PR_101", "number": 201},
            ]}],
        )
        self.assertEqual(1, client.calls.count(("/repos/owner/repo/pulls/201", None)))
        self.assertEqual({"task-1": (101,)}, observed.task_pull_request_ids)
        self.assertEqual({101: {"headSha": "one-observed-head"}}, observed.pull_request_sources)

    def test_known_pull_identity_mismatch_is_not_reassociated(self) -> None:
        running = "/agents/repos/owner/repo/tasks?state=queued%2Cin_progress&is_archived=false&per_page=100"
        client = ScriptedClient({(running, "tasks"): []}, {
            "/repos/owner/repo/pulls/201": {
                "id": 999, "number": 201, "node_id": "PR_other",
                "state": "open", "draft": True, "changed_files": 4,
            },
        })
        with self.assertRaisesRegex(ValueError, "identity"):
            observe_delegations(
                client, "owner/repo", owned_task_ids=set(),
                known_records=[{"taskId": "task-1", "pullRequests": [
                    {"databaseId": 101, "globalId": "PR_101", "number": 201},
                ]}],
            )

    def test_missing_or_conflicting_merge_evidence_does_not_claim_closed_unmerged(self) -> None:
        running = "/agents/repos/owner/repo/tasks?state=queued%2Cin_progress&is_archived=false&per_page=100"
        for evidence in ({}, {"merged_at": None, "merged": True},
                         {"merged_at": "2026-09-01T14:00:00Z", "merged": False}):
            with self.subTest(evidence=evidence):
                client = ScriptedClient({(running, "tasks"): []}, {
                    "/repos/owner/repo/pulls/201": {
                        "id": 101, "number": 201, "node_id": "PR_101",
                        "state": "closed", "draft": False, **evidence,
                    },
                })
                result = observe_delegations(
                    client, "owner/repo", owned_task_ids=set(),
                    known_records=[{"taskId": "task-1", "pullRequests": [
                        {"databaseId": 101, "number": 201, "globalId": "PR_101"},
                    ]}],
                )
                self.assertEqual(PullRequestState.UNKNOWN, result.pull_requests[0].state)
                self.assertFalse(result.evidence.pull_request_inventory_complete)

    def test_known_pull_is_observed_when_task_is_missing(self) -> None:
        running = "/agents/repos/owner/repo/tasks?state=queued%2Cin_progress&is_archived=false&per_page=100"
        client = ScriptedClient({(running, "tasks"): []}, {
            "/repos/owner/repo/pulls/201": {
                "id": 101, "number": 201, "node_id": "PR_101", "state": "closed",
                "merged_at": "2026-09-01T14:00:00Z", "draft": False,
                "changed_files": 4, "head": {"sha": "merged-head"},
            },
        })
        observation = observe_delegations(
            client, "owner/repo", owned_task_ids={"task-1"},
            known_records=[{
                "taskId": "task-1",
                "pullRequests": [{"databaseId": 101, "globalId": "PR_101",
                                  "number": 201, "state": "open", "isDraft": True}],
            }],
        )
        self.assertEqual((), observation.tasks)
        self.assertEqual(frozenset({"task-1"}), observation.unavailable_task_ids)
        self.assertEqual(PullRequestState.MERGED, observation.pull_requests[0].state)
        self.assertEqual({"task-1": (101,)}, observation.task_pull_request_ids)
        self.assertTrue(observation.evidence.owned_task_inventory_complete)
        self.assertTrue(observation.evidence.pull_request_inventory_complete)

    def test_human_identity_requires_structured_non_bot_account(self) -> None:
        self.assertTrue(_human_identity({"login": "maintainer", "type": "User"}))
        self.assertFalse(
            _human_identity({"login": "automation[bot]", "type": "Bot"})
        )
        self.assertIsNone(_human_identity({"login": "maintainer"}))
        self.assertIsNone(_human_identity({"type": "User"}))

    def test_names_missing_owned_task_evidence(self) -> None:
        observation = observe_delegations(
            ScriptedClient({
                ("/agents/repos/owner/repo/tasks?state=queued%2Cin_progress&is_archived=false&per_page=100", "tasks"): [],
            }),
            "owner/repo", owned_task_ids={"missing-task"},
        )
        self.assertEqual(frozenset({"missing-task"}), observation.unavailable_task_ids)
        self.assertFalse(observation.evidence.owned_task_inventory_complete)

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
        self.assertFalse(observation.evidence.pull_request_inventory_complete)
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
