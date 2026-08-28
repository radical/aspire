from __future__ import annotations

import copy
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from ci_shepherd.actor import build_dry_run, execute_action
from ci_shepherd.github_actor import GitHubActorClient


COMMENT_ACTION_ID = "snapshot:owner/repo:1:issue:21:review-close-comment"
CLOSE_ACTION_ID = "snapshot:owner/repo:1:issue:21:review-close"
MARKER = "ci-shepherd:idempotency-key=issue:21:review-close"
COMMENT_BODY = f"[automated] Resolved.\n\n<!-- {MARKER} -->"


def _proposals() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "repository": "owner/repo",
        "snapshotId": "snapshot:owner/repo:1",
        "shepherdAuthor": "ankj",
        "proposals": [
            {
                "actionId": COMMENT_ACTION_ID,
                "issueNumber": 21,
                "issueUrl": "https://github.com/owner/repo/issues/21",
                "operation": "create-comment",
                "idempotencyKey": "issue:21:review-close",
                "body": COMMENT_BODY,
                "evidenceIds": ["issue:21", "run:777"],
                "expectedIssueState": "open",
                "requiresSeparateApproval": True,
            },
            {
                "actionId": CLOSE_ACTION_ID,
                "issueNumber": 21,
                "issueUrl": "https://github.com/owner/repo/issues/21",
                "operation": "close-issue",
                "closeReason": "completed",
                "requiresSeparateApproval": True,
                "idempotencyKey": "issue:21:close:completed",
                "evidenceIds": ["issue:21", "run:777"],
                "expectedIssueState": "open",
                "dependsOn": COMMENT_ACTION_ID,
            },
        ],
        "unchangedIssueNumbers": [],
    }


def _results(*records: dict[str, object]) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "repository": "owner/repo",
        "results": list(records),
    }


def _pull_proposals() -> dict[str, object]:
    key = "pull-request:23:status"
    return {
        "schemaVersion": 1,
        "repository": "owner/repo",
        "snapshotId": "snapshot:owner/repo:1",
        "shepherdAuthor": "ankj",
        "proposals": [
            {
                "actionId": "snapshot:owner/repo:1:pull-request:23:status",
                "targetKind": "pull-request",
                "targetNumber": 23,
                "targetUrl": "https://github.com/owner/repo/pull/23",
                "operation": "create-comment",
                "idempotencyKey": key,
                "body": (
                    "[automated] Human review is needed.\n\n"
                    f"<!-- ci-shepherd:idempotency-key={key} -->"
                ),
                "evidenceIds": ["pr:23"],
                "expectedTargetState": "open",
                "requiresSeparateApproval": True,
            }
        ],
        "unchangedIssueNumbers": [],
    }


class ScriptedActorClient:
    def __init__(
        self,
        *,
        issues: list[dict[str, object]] | None = None,
        comments: list[list[dict[str, object]]] | None = None,
        single_comments: list[dict[str, object]] | None = None,
    ) -> None:
        self.issues = list(issues or [])
        self.comments = list(comments or [])
        self.single_comments = list(single_comments or [])
        self.calls: list[tuple[object, ...]] = []

    def get_authenticated_login(self) -> str:
        self.calls.append(("get_authenticated_login",))
        return "ankj"

    def get_issue(self, repository: str, issue_number: int) -> dict[str, object]:
        self.calls.append(("get_issue", issue_number))
        return self.issues.pop(0)

    def list_comments(
        self,
        repository: str,
        issue_number: int,
    ) -> list[dict[str, object]]:
        self.calls.append(("list_comments", issue_number))
        return self.comments.pop(0)

    def get_comment(self, repository: str, comment_id: int) -> dict[str, object]:
        self.calls.append(("get_comment", comment_id))
        return self.single_comments.pop(0)

    def create_comment(
        self,
        repository: str,
        issue_number: int,
        body: str,
    ) -> dict[str, object]:
        self.calls.append(("create_comment", issue_number, body))
        return {"id": 900, "body": body, "user": {"login": "ankj"}}

    def edit_comment(
        self,
        repository: str,
        comment_id: int,
        body: str,
    ) -> dict[str, object]:
        self.calls.append(("edit_comment", comment_id, body))
        return {"id": comment_id, "body": body, "user": {"login": "ankj"}}

    def close_issue(
        self,
        repository: str,
        issue_number: int,
        reason: str,
    ) -> dict[str, object]:
        self.calls.append(("close_issue", issue_number, reason))
        return {"number": issue_number, "state": "closed", "state_reason": reason}


class ActorTests(unittest.TestCase):
    def test_pull_request_comment_aborts_when_assigned_to_copilot(self) -> None:
        client = ScriptedActorClient(
            issues=[
                {
                    "number": 23,
                    "state": "open",
                    "html_url": "https://github.com/owner/repo/pull/23",
                    "pull_request": {},
                    "assignees": [{"login": "copilot-swe-agent[bot]"}],
                }
            ]
        )

        result = execute_action(
            _pull_proposals(),
            action_id="snapshot:owner/repo:1:pull-request:23:status",
            prior_results=_results(),
            client=client,
            now=lambda: datetime(2026, 8, 21, 20, tzinfo=UTC),
        )

        self.assertEqual("stale", result["outcome"])
        self.assertEqual("target-assigned-to-copilot", result["reason"])
        self.assertEqual([("get_issue", 23)], client.calls)

    def test_dry_run_renders_all_actions_without_client_calls(self) -> None:
        rendered = build_dry_run(_proposals(), action_id=None)

        self.assertEqual("dry-run", rendered["mode"])
        self.assertEqual(
            ["create-comment", "close-issue"],
            [action["operation"] for action in rendered["actions"]],
        )
        self.assertTrue(all(action["wouldExecute"] for action in rendered["actions"]))

    def test_dry_run_renders_target_shaped_pull_request_action(self) -> None:
        rendered = build_dry_run(_pull_proposals(), action_id=None)

        self.assertEqual(
            [
                {
                    "actionId": "snapshot:owner/repo:1:pull-request:23:status",
                    "targetKind": "pull-request",
                    "targetNumber": 23,
                    "targetUrl": "https://github.com/owner/repo/pull/23",
                    "operation": "create-comment",
                    "body": (
                        "[automated] Human review is needed.\n\n"
                        "<!-- ci-shepherd:idempotency-key=pull-request:23:status -->"
                    ),
                    "closeReason": None,
                    "evidenceIds": ["pr:23"],
                    "dependsOn": None,
                    "expectedTargetState": "open",
                    "wouldExecute": True,
                }
            ],
            rendered["actions"],
        )

    def test_dry_run_can_select_one_action(self) -> None:
        rendered = build_dry_run(_proposals(), action_id=COMMENT_ACTION_ID)

        self.assertEqual(
            [COMMENT_ACTION_ID],
            [action["actionId"] for action in rendered["actions"]],
        )

    def test_dry_run_rejects_duplicate_action_ids(self) -> None:
        proposals = _proposals()
        actions = proposals["proposals"]
        assert isinstance(actions, list)
        duplicate = copy.deepcopy(actions[0])
        actions.append(duplicate)

        with self.assertRaisesRegex(ValueError, "Duplicate actionId"):
            build_dry_run(proposals, action_id=None)

    def test_dry_run_rejects_duplicate_idempotency_keys(self) -> None:
        proposals = _proposals()
        actions = proposals["proposals"]
        assert isinstance(actions, list)
        duplicate_key = copy.deepcopy(actions[0])
        duplicate_key["actionId"] = "snapshot:owner/repo:1:issue:21:other-comment"
        actions.append(duplicate_key)

        with self.assertRaisesRegex(ValueError, "Duplicate idempotencyKey"):
            build_dry_run(proposals, action_id=None)

    def test_dry_run_rejects_unknown_operation(self) -> None:
        proposals = _proposals()
        action = proposals["proposals"][0]
        assert isinstance(action, dict)
        action["operation"] = "run-arbitrary-command"

        with self.assertRaisesRegex(ValueError, "Unsupported operation"):
            build_dry_run(proposals, action_id=None)

    def test_dry_run_rejects_unknown_proposal_fields(self) -> None:
        proposals = _proposals()
        action = proposals["proposals"][0]
        assert isinstance(action, dict)
        action["endpoint"] = "/repos/owner/repo/issues/21"

        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            build_dry_run(proposals, action_id=None)

    def test_dry_run_rejects_issue_url_outside_repository(self) -> None:
        proposals = _proposals()
        action = proposals["proposals"][0]
        assert isinstance(action, dict)
        action["issueUrl"] = "https://example.com/issues/21"

        with self.assertRaisesRegex(ValueError, "does not match repository"):
            build_dry_run(proposals, action_id=None)

    def test_dry_run_rejects_repository_path_segments(self) -> None:
        proposals = _proposals()
        proposals["repository"] = "../repo"

        with self.assertRaisesRegex(ValueError, "owner/name form"):
            build_dry_run(proposals, action_id=None)

    def test_dry_run_rejects_unattributed_comment(self) -> None:
        proposals = _proposals()
        action = proposals["proposals"][0]
        assert isinstance(action, dict)
        action["body"] = "Resolved."

        with self.assertRaisesRegex(ValueError, "must start with"):
            build_dry_run(proposals, action_id=None)

    def test_dry_run_rejects_unknown_action_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown actionId"):
            build_dry_run(_proposals(), action_id="missing")

    def test_execute_requires_completed_dependency(self) -> None:
        client = ScriptedActorClient()

        result = execute_action(
            _proposals(),
            action_id=CLOSE_ACTION_ID,
            prior_results=_results(),
            client=client,
            now=lambda: datetime(2026, 8, 21, 20, tzinfo=UTC),
        )

        self.assertEqual("stale", result["outcome"])
        self.assertEqual("dependency-not-executed", result["reason"])
        self.assertEqual([], client.calls)

    def test_create_comment_aborts_when_marker_exists(self) -> None:
        client = ScriptedActorClient(
            issues=[{"number": 21, "state": "open"}],
            comments=[
                [
                    {
                        "id": 900,
                        "body": COMMENT_BODY,
                        "user": {"login": "ankj"},
                    }
                ]
            ],
        )

        result = execute_action(
            _proposals(),
            action_id=COMMENT_ACTION_ID,
            prior_results=_results(),
            client=client,
            now=lambda: datetime(2026, 8, 21, 20, tzinfo=UTC),
        )

        self.assertEqual("stale", result["outcome"])
        self.assertEqual("idempotency-marker-exists", result["reason"])
        self.assertEqual(
            [
                ("get_issue", 21),
                ("list_comments", 21),
                ("get_authenticated_login",),
            ],
            client.calls,
        )

    def test_create_comment_aborts_when_legacy_status_marker_exists(self) -> None:
        proposals = _proposals()
        action = proposals["proposals"][0]
        assert isinstance(action, dict)
        action["idempotencyKey"] = "issue:21:status"
        action["body"] = (
            "[automated] Current status.\n\n"
            "<!-- ci-shepherd:idempotency-key=issue:21:status -->"
        )
        client = ScriptedActorClient(
            issues=[{"number": 21, "state": "open"}],
            comments=[
                [
                    {
                        "id": 900,
                        "body": (
                            "[automated] Old watch status.\n\n"
                            "<!-- ci-shepherd:idempotency-key=issue:21:watch -->"
                        ),
                        "user": {"login": "ankj"},
                    }
                ]
            ],
        )

        result = execute_action(
            proposals,
            action_id=COMMENT_ACTION_ID,
            prior_results=_results(),
            client=client,
            now=lambda: datetime(2026, 8, 21, 20, tzinfo=UTC),
        )

        self.assertEqual("stale", result["outcome"])
        self.assertEqual("idempotency-marker-exists", result["reason"])
        self.assertNotIn(
            ("create_comment", 21, action["body"]),
            client.calls,
        )

    def test_create_comment_ignores_unowned_marker(self) -> None:
        client = ScriptedActorClient(
            issues=[{"number": 21, "state": "open"}],
            comments=[
                [
                    {
                        "id": 800,
                        "body": COMMENT_BODY,
                        "user": {"login": "someone-else"},
                    }
                ]
            ],
            single_comments=[
                {"id": 900, "body": COMMENT_BODY, "user": {"login": "ankj"}}
            ],
        )

        result = execute_action(
            _proposals(),
            action_id=COMMENT_ACTION_ID,
            prior_results=_results(),
            client=client,
            now=lambda: datetime(2026, 8, 21, 20, tzinfo=UTC),
        )

        self.assertEqual("executed", result["outcome"])
        self.assertIn(("create_comment", 21, COMMENT_BODY), client.calls)

    def test_create_comment_executes_and_verifies_body(self) -> None:
        client = ScriptedActorClient(
            issues=[{"number": 21, "state": "open"}],
            comments=[[]],
            single_comments=[
                {"id": 900, "body": COMMENT_BODY, "user": {"login": "ankj"}}
            ],
        )

        result = execute_action(
            _proposals(),
            action_id=COMMENT_ACTION_ID,
            prior_results=_results(),
            client=client,
            now=lambda: datetime(2026, 8, 21, 20, tzinfo=UTC),
        )

        self.assertEqual("executed", result["outcome"])
        self.assertEqual(900, result["result"]["commentId"])
        self.assertEqual(
            [
                ("get_issue", 21),
                ("list_comments", 21),
                ("get_authenticated_login",),
                ("create_comment", 21, COMMENT_BODY),
                ("get_comment", 900),
            ],
            client.calls,
        )

    def test_edit_comment_executes_and_verifies_body(self) -> None:
        proposals = _proposals()
        action = proposals["proposals"][0]
        assert isinstance(action, dict)
        action["operation"] = "edit-comment"
        action["commentId"] = 900
        existing = {
            "id": 900,
            "body": f"[automated] Old.\n\n<!-- {MARKER} -->",
            "user": {"login": "ankj"},
        }
        client = ScriptedActorClient(
            issues=[{"number": 21, "state": "open"}],
            single_comments=[existing, {"id": 900, "body": COMMENT_BODY, "user": {"login": "ankj"}}],
        )

        result = execute_action(
            proposals,
            action_id=COMMENT_ACTION_ID,
            prior_results=_results(),
            client=client,
            now=lambda: datetime(2026, 8, 21, 20, tzinfo=UTC),
        )

        self.assertEqual("executed", result["outcome"])
        self.assertEqual(
            [
                ("get_issue", 21),
                ("get_comment", 900),
                ("get_authenticated_login",),
                ("edit_comment", 900, COMMENT_BODY),
                ("get_comment", 900),
            ],
            client.calls,
        )

    def test_edit_comment_migrates_legacy_issue_status_marker(self) -> None:
        proposals = _proposals()
        action = proposals["proposals"][0]
        assert isinstance(action, dict)
        action["operation"] = "edit-comment"
        action["commentId"] = 900
        action["idempotencyKey"] = "issue:21:status"
        action["body"] = (
            "[automated] Current status.\n\n"
            "<!-- ci-shepherd:idempotency-key=issue:21:status -->"
        )
        existing = {
            "id": 900,
            "body": (
                "[automated] Old watch status.\n\n"
                "<!-- ci-shepherd:idempotency-key=issue:21:watch -->"
            ),
            "user": {"login": "ankj"},
        }
        client = ScriptedActorClient(
            issues=[{"number": 21, "state": "open"}],
            single_comments=[
                existing,
                {
                    "id": 900,
                    "body": action["body"],
                    "user": {"login": "ankj"},
                },
            ],
        )

        result = execute_action(
            proposals,
            action_id=COMMENT_ACTION_ID,
            prior_results=_results(),
            client=client,
            now=lambda: datetime(2026, 8, 21, 20, tzinfo=UTC),
        )

        self.assertEqual("executed", result["outcome"])
        self.assertIn(
            ("edit_comment", 900, action["body"]),
            client.calls,
        )

    def test_close_issue_executes_and_verifies_reason(self) -> None:
        client = ScriptedActorClient(
            issues=[
                {"number": 21, "state": "open", "state_reason": None},
                {"number": 21, "state": "closed", "state_reason": "completed"},
            ]
        )

        result = execute_action(
            _proposals(),
            action_id=CLOSE_ACTION_ID,
            prior_results=_results(
                {"actionId": COMMENT_ACTION_ID, "outcome": "executed"}
            ),
            client=client,
            now=lambda: datetime(2026, 8, 21, 20, tzinfo=UTC),
        )

        self.assertEqual("executed", result["outcome"])
        self.assertEqual("closed", result["result"]["issueState"])
        self.assertEqual("completed", result["result"]["stateReason"])
        self.assertEqual(
            [
                ("get_issue", 21),
                ("close_issue", 21, "completed"),
                ("get_issue", 21),
            ],
            client.calls,
        )

    def test_completed_action_id_is_not_attempted_twice(self) -> None:
        client = ScriptedActorClient()

        result = execute_action(
            _proposals(),
            action_id=COMMENT_ACTION_ID,
            prior_results=_results(
                {"actionId": COMMENT_ACTION_ID, "outcome": "executed"}
            ),
            client=client,
            now=lambda: datetime(2026, 8, 21, 20, tzinfo=UTC),
        )

        self.assertEqual("stale", result["outcome"])
        self.assertEqual("action-already-attempted", result["reason"])
        self.assertEqual([], client.calls)


class RecordingRunner:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[tuple[list[str], dict[str, object] | None]] = []

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        request = None
        if "--input" in command:
            input_path = Path(command[command.index("--input") + 1])
            request = json.loads(input_path.read_text(encoding="utf-8"))
            if os.name != "nt":
                assert input_path.stat().st_mode & 0o777 == 0o600
        self.calls.append((command, request))
        return subprocess.CompletedProcess(command, 0, json.dumps(self.payload), "")


class GitHubActorClientTests(unittest.TestCase):
    def test_uses_fixed_get_issue_endpoint(self) -> None:
        runner = RecordingRunner({"number": 21, "state": "open"})
        client = GitHubActorClient(runner=runner)

        result = client.get_issue("owner/repo", 21)

        self.assertEqual(21, result["number"])
        self.assertEqual(
            "repos/owner/repo/issues/21",
            runner.calls[0][0][-1],
        )
        self.assertIn("GET", runner.calls[0][0])

    def test_create_comment_uses_private_json_input(self) -> None:
        runner = RecordingRunner({"id": 900, "body": COMMENT_BODY})
        client = GitHubActorClient(runner=runner)

        client.create_comment("owner/repo", 21, COMMENT_BODY)

        command, request = runner.calls[0]
        self.assertIn("POST", command)
        self.assertEqual(
            "repos/owner/repo/issues/21/comments",
            command[-1],
        )
        self.assertEqual({"body": COMMENT_BODY}, request)
        self.assertFalse(Path(command[command.index("--input") + 1]).exists())

    def test_close_issue_uses_state_reason_payload(self) -> None:
        runner = RecordingRunner(
            {"number": 21, "state": "closed", "state_reason": "completed"}
        )
        client = GitHubActorClient(runner=runner)

        client.close_issue("owner/repo", 21, "completed")

        command, request = runner.calls[0]
        self.assertIn("PATCH", command)
        self.assertEqual(
            {"state": "closed", "state_reason": "completed"},
            request,
        )


if __name__ == "__main__":
    unittest.main()
