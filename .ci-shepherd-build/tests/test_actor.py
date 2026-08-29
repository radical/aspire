from __future__ import annotations

import copy
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from ci_shepherd.actor import build_dry_run, execute_action, reconcile_action
from ci_shepherd.github_actor import GitHubActorClient, MutationRepositoryError


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
        authenticated_login: str = "ankj",
    ) -> None:
        self.issues = list(issues or [])
        self.comments = list(comments or [])
        self.single_comments = list(single_comments or [])
        self.authenticated_login = authenticated_login
        self.calls: list[tuple[object, ...]] = []

    def get_authenticated_login(self) -> str:
        self.calls.append(("get_authenticated_login",))
        return self.authenticated_login

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
    def test_actor_identity_must_match_authorized_proposal_author(self) -> None:
        client = ScriptedActorClient(
            issues=[{"number": 21, "state": "open"}],
            authenticated_login="different-user",
        )

        result = execute_action(
            _proposals(),
            action_id=COMMENT_ACTION_ID,
            prior_results=_results(),
            client=client,
            now=lambda: datetime(2026, 8, 21, 20, tzinfo=UTC),
        )

        self.assertEqual("stale", result["outcome"])
        self.assertEqual("actor-identity-changed", result["reason"])
        self.assertNotIn("create_comment", [call[0] for call in client.calls])

    def test_v2_execution_rejects_ineligible_proposal_before_client_calls(self) -> None:
        proposals = _proposals()
        proposals.update(
            {
                "schemaVersion": 2,
                "generatedAtUtc": "2026-08-21T19:55:00Z",
                "proposalTtlHours": 24,
                "maxProposalsPerIssue": 2,
                "executionEligibility": {
                    "status": "blocked",
                    "violations": [
                        {
                            "actionId": COMMENT_ACTION_ID,
                            "blockingReasons": [
                                "missing-ci-label",
                                "no-parsed-occurrences",
                            ],
                        },
                        {
                            "actionId": CLOSE_ACTION_ID,
                            "blockingReasons": [
                                "missing-ci-label",
                                "no-parsed-occurrences",
                            ],
                        },
                    ],
                },
            }
        )
        for proposal in proposals["proposals"]:
            assert isinstance(proposal, dict)
            proposal.pop("requiresSeparateApproval")
            proposal["executionEligibility"] = {
                "eligible": False,
                "ciLabels": [],
                "occurrenceCount": 0,
                "collectionComplete": True,
                "unavailableEvidenceIds": [],
                "untrustedReferenceEvidenceIds": [],
                "blockingReasons": ["missing-ci-label", "no-parsed-occurrences"],
            }
            proposal["sourceEvidenceFingerprint"] = {
                "issueUpdatedAt": "2026-08-21T19:54:00Z"
            }
        client = ScriptedActorClient()

        with self.assertRaisesRegex(ValueError, "not eligible"):
            execute_action(
                proposals,
                action_id=COMMENT_ACTION_ID,
                prior_results=_results(),
                client=client,
                now=lambda: datetime(2026, 8, 21, 20, tzinfo=UTC),
            )

        self.assertEqual([], client.calls)

    def test_v2_execution_rejects_changed_source_issue_before_mutation(self) -> None:
        proposals = _proposals()
        proposals.update(
            {
                "schemaVersion": 2,
                "generatedAtUtc": "2026-08-21T19:55:00Z",
                "proposalTtlHours": 24,
                "maxProposalsPerIssue": 2,
                "executionEligibility": {"status": "eligible", "violations": []},
            }
        )
        proposal = proposals["proposals"][0]
        assert isinstance(proposal, dict)
        proposal.pop("requiresSeparateApproval")
        proposal["executionEligibility"] = {
            "eligible": True,
            "ciLabels": ["ci-failure-cause"],
            "occurrenceCount": 1,
            "collectionComplete": True,
            "unavailableEvidenceIds": [],
            "untrustedReferenceEvidenceIds": [],
            "blockingReasons": [],
        }
        proposal["sourceEvidenceFingerprint"] = {
            "issueUpdatedAt": "2026-08-21T19:54:00Z"
        }
        proposals["proposals"] = [proposal]
        client = ScriptedActorClient(
            issues=[
                {
                    "number": 21,
                    "state": "open",
                    "updated_at": "2026-08-21T19:56:00Z",
                }
            ]
        )

        result = execute_action(
            proposals,
            action_id=COMMENT_ACTION_ID,
            prior_results=_results(),
            client=client,
            now=lambda: datetime(2026, 8, 21, 20, tzinfo=UTC),
        )

        self.assertEqual("stale", result["outcome"])
        self.assertEqual("source-evidence-changed", result["reason"])
        self.assertEqual([("get_issue", 21)], client.calls)

    def test_v2_close_accepts_the_issue_version_created_by_its_dependency(self) -> None:
        proposals = _proposals()
        proposals.update(
            {
                "schemaVersion": 2,
                "generatedAtUtc": "2026-08-21T19:55:00Z",
                "proposalTtlHours": 24,
                "maxProposalsPerIssue": 2,
                "executionEligibility": {"status": "eligible", "violations": []},
            }
        )
        for proposal in proposals["proposals"]:
            assert isinstance(proposal, dict)
            proposal.pop("requiresSeparateApproval")
            proposal["executionEligibility"] = {
                "eligible": True,
                "ciLabels": ["ci-failure-cause"],
                "occurrenceCount": 1,
                "collectionComplete": True,
                "unavailableEvidenceIds": [],
                "untrustedReferenceEvidenceIds": [],
                "blockingReasons": [],
            }
            proposal["sourceEvidenceFingerprint"] = {
                "issueUpdatedAt": "2026-08-21T19:54:00Z"
            }
        client = ScriptedActorClient(
            issues=[
                {"number": 21, "state": "open", "updated_at": "2026-08-21T19:54:00Z"},
                {"number": 21, "state": "open", "updated_at": "2026-08-21T19:56:00Z"},
                {"number": 21, "state": "open", "updated_at": "2026-08-21T19:56:00Z"},
                {
                    "number": 21,
                    "state": "closed",
                    "state_reason": "completed",
                    "html_url": "https://github.com/owner/repo/issues/21",
                },
            ],
            comments=[[]],
            single_comments=[
                {
                    "id": 900,
                    "body": COMMENT_BODY,
                    "html_url": "https://github.com/owner/repo/issues/21#issuecomment-900",
                    "user": {"login": "ankj"},
                }
            ],
        )

        comment_result = execute_action(
            proposals,
            action_id=COMMENT_ACTION_ID,
            prior_results=_results(),
            client=client,
            now=lambda: datetime(2026, 8, 21, 20, tzinfo=UTC),
        )
        close_result = execute_action(
            proposals,
            action_id=CLOSE_ACTION_ID,
            prior_results=_results(comment_result),
            client=client,
            now=lambda: datetime(2026, 8, 21, 20, 1, tzinfo=UTC),
        )

        self.assertEqual("executed", comment_result["outcome"])
        self.assertEqual(
            "2026-08-21T19:56:00Z",
            comment_result["result"]["sourceIssueUpdatedAt"],
        )
        self.assertEqual("executed", close_result["outcome"])

    def test_executed_stable_identity_suppresses_a_new_snapshot_without_live_marker(
        self,
    ) -> None:
        proposals = _proposals()
        proposal = proposals["proposals"][0]
        assert isinstance(proposal, dict)
        proposal["actionId"] = "snapshot:owner/repo:2:issue:21:review-close-comment"
        proposals["proposals"] = [proposal]
        client = ScriptedActorClient()

        result = execute_action(
            proposals,
            action_id=str(proposal["actionId"]),
            prior_results=_results(
                {
                    "actionId": COMMENT_ACTION_ID,
                    "outcome": "executed",
                    "idempotencyKey": "issue:21:review-close",
                    "target": {"kind": "issue", "number": 21},
                }
            ),
            client=client,
            now=lambda: datetime(2026, 8, 21, 20, tzinfo=UTC),
        )

        self.assertEqual("stale", result["outcome"])
        self.assertEqual("stable-idempotency-key-already-executed", result["reason"])
        self.assertEqual([], client.calls)

    def test_nonterminal_intent_does_not_trigger_already_attempted_guard(self) -> None:
        client = ScriptedActorClient(
            issues=[
                {
                    "number": 21,
                    "state": "open",
                    "html_url": "https://github.com/owner/repo/issues/21",
                }
            ],
            comments=[[]],
            single_comments=[
                {
                    "id": 900,
                    "html_url": (
                        "https://github.com/owner/repo/issues/21#issuecomment-900"
                    ),
                    "body": COMMENT_BODY,
                    "user": {"login": "ankj"},
                }
            ],
        )

        result = execute_action(
            _proposals(),
            action_id=COMMENT_ACTION_ID,
            prior_results=_results(
                {
                    "actionId": COMMENT_ACTION_ID,
                    "eventType": "intent",
                    "outcome": "intent",
                }
            ),
            client=client,
            now=lambda: datetime(2026, 8, 21, 20, tzinfo=UTC),
        )

        self.assertEqual("executed", result["outcome"])
        self.assertIn(
            "create_comment",
            [call[0] for call in client.calls],
        )

    def test_uncertain_mutation_failure_is_indeterminate(self) -> None:
        class UncertainClient(ScriptedActorClient):
            def create_comment(
                self,
                repository: str,
                issue_number: int,
                body: str,
            ) -> dict[str, object]:
                self.calls.append(("create_comment", issue_number, body))
                raise RuntimeError("connection lost after request")

        client = UncertainClient(
            issues=[
                {
                    "number": 21,
                    "state": "open",
                    "html_url": "https://github.com/owner/repo/issues/21",
                }
            ],
            comments=[[]],
        )

        result = execute_action(
            _proposals(),
            action_id=COMMENT_ACTION_ID,
            prior_results=_results(),
            client=client,
            now=lambda: datetime(2026, 8, 21, 20, tzinfo=UTC),
        )

        self.assertEqual("indeterminate", result["outcome"])
        self.assertIn("connection lost", result["reason"])

    def test_reconcile_comment_requires_exact_key_body_and_author(self) -> None:
        client = ScriptedActorClient(
            comments=[
                [
                    {
                        "id": 900,
                        "html_url": (
                            "https://github.com/owner/repo/issues/21#issuecomment-900"
                        ),
                        "body": COMMENT_BODY,
                        "user": {"login": "ankj"},
                    }
                ]
            ]
        )

        result = reconcile_action(
            _proposals(),
            action_id=COMMENT_ACTION_ID,
            client=client,
            now=lambda: datetime(2026, 8, 21, 20, tzinfo=UTC),
        )

        self.assertEqual("executed", result["outcome"])
        self.assertEqual(900, result["result"]["commentId"])
        self.assertNotIn(
            "create_comment",
            [call[0] for call in client.calls],
        )

    def test_reconcile_comment_rejects_equivalent_but_inexact_marker(self) -> None:
        client = ScriptedActorClient(
            comments=[
                [
                    {
                        "id": 899,
                        "body": (
                            "[automated] Watching.\n\n"
                            "<!-- ci-shepherd:idempotency-key=issue:21:watch -->"
                        ),
                        "user": {"login": "ankj"},
                    }
                ]
            ]
        )

        result = reconcile_action(
            _proposals(),
            action_id=COMMENT_ACTION_ID,
            client=client,
            now=lambda: datetime(2026, 8, 21, 20, tzinfo=UTC),
        )

        self.assertEqual("indeterminate", result["outcome"])
        self.assertEqual("mutation-not-confirmed", result["reason"])
        self.assertEqual(
            [("list_comments", 21), ("get_authenticated_login",)],
            client.calls,
        )

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
                ("get_authenticated_login",),
                ("list_comments", 21),
            ],
            client.calls,
        )

    def test_grant_authorized_suppression_override_executes_comment(self) -> None:
        client = ScriptedActorClient(
            issues=[{"number": 21, "state": "open"}],
            comments=[
                [
                    {
                        "id": 899,
                        "body": COMMENT_BODY,
                        "user": {"login": "ankj"},
                    }
                ]
            ],
            single_comments=[
                {
                    "id": 900,
                    "body": COMMENT_BODY,
                    "user": {"login": "ankj"},
                }
            ],
        )

        result = execute_action(
            _proposals(),
            action_id=COMMENT_ACTION_ID,
            prior_results=_results(),
            client=client,
            now=lambda: datetime(2026, 8, 21, 20, tzinfo=UTC),
            override_suppression=True,
        )

        self.assertEqual("executed", result["outcome"])
        self.assertIn("create_comment", [call[0] for call in client.calls])

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
                ("get_authenticated_login",),
                ("list_comments", 21),
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
                ("get_authenticated_login",),
                ("get_comment", 900),
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
                ("get_authenticated_login",),
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
        self.call_kwargs: list[dict[str, object]] = []

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        request = None
        if "--input" in command:
            input_path = Path(command[command.index("--input") + 1])
            request = json.loads(input_path.read_text(encoding="utf-8"))
            if os.name != "nt":
                assert input_path.stat().st_mode & 0o777 == 0o600
        self.calls.append((command, request))
        self.call_kwargs.append(dict(kwargs))
        return subprocess.CompletedProcess(command, 0, json.dumps(self.payload), "")


class SequencedRunner(RecordingRunner):
    def __init__(self, payloads: list[object]) -> None:
        super().__init__(payload=None)
        self.payloads = payloads

    def __call__(
        self,
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        self.payload = self.payloads.pop(0)
        return super().__call__(command, **kwargs)


class GitHubActorClientTests(unittest.TestCase):
    def test_requests_have_a_bounded_subprocess_timeout(self) -> None:
        runner = RecordingRunner({"number": 21, "state": "open"})
        client = GitHubActorClient(runner=runner, request_timeout_seconds=17)

        client.get_issue("owner/repo", 21)

        self.assertEqual(17, runner.call_kwargs[0]["timeout"])

    def test_list_comments_paginates_past_one_hundred(self) -> None:
        runner = SequencedRunner(
            [
                [{"id": comment_id} for comment_id in range(1, 101)],
                [{"id": 101}],
            ]
        )
        client = GitHubActorClient(runner=runner)

        comments = client.list_comments("owner/repo", 21)

        self.assertEqual(101, len(comments))
        self.assertTrue(runner.calls[0][0][-1].endswith("per_page=100&page=1"))
        self.assertTrue(runner.calls[1][0][-1].endswith("per_page=100&page=2"))

    def test_mutation_requires_explicitly_allowed_repository(self) -> None:
        runner = RecordingRunner({"id": 900, "body": COMMENT_BODY})
        client = GitHubActorClient(
            runner=runner,
            allowed_repositories={"radical/aspire"},
        )

        with self.assertRaisesRegex(
            MutationRepositoryError,
            "not explicitly allowed",
        ):
            client.create_comment("owner/repo", 21, COMMENT_BODY)

        self.assertEqual([], runner.calls)

    def test_allowed_repository_matching_is_case_insensitive(self) -> None:
        runner = RecordingRunner({"id": 900, "body": COMMENT_BODY})
        client = GitHubActorClient(
            runner=runner,
            allowed_repositories={"Owner/Repo"},
        )

        result = client.create_comment("owner/repo", 21, COMMENT_BODY)

        self.assertEqual(900, result["id"])
        self.assertEqual(1, len(runner.calls))

    def test_production_repository_is_hard_denied(self) -> None:
        for repository in ("microsoft/aspire", "Microsoft/aspire", "microsoft/Aspire"):
            with self.subTest(repository=repository):
                runner = RecordingRunner({"id": 900, "body": COMMENT_BODY})
                client = GitHubActorClient(
                    runner=runner,
                    allowed_repositories={repository},
                )

                with self.assertRaisesRegex(
                    MutationRepositoryError,
                    "protected",
                ):
                    client.create_comment(repository, 21, COMMENT_BODY)

                self.assertEqual([], runner.calls)

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
        client = GitHubActorClient(
            runner=runner,
            allowed_repositories={"owner/repo"},
        )

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
        client = GitHubActorClient(
            runner=runner,
            allowed_repositories={"owner/repo"},
        )

        client.close_issue("owner/repo", 21, "completed")

        command, request = runner.calls[0]
        self.assertIn("PATCH", command)
        self.assertEqual(
            {"state": "closed", "state_reason": "completed"},
            request,
        )


if __name__ == "__main__":
    unittest.main()
