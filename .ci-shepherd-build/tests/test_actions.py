from __future__ import annotations

import copy
import unittest

from ci_shepherd.actions import build_action_proposals, build_watch_proposals


def _snapshot() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "repository": "owner/repo",
        "collectedAt": "2026-08-21T16:00:00Z",
        "openIssues": [21],
        "issues": [{"number": 21, "state": "open"}],
        "supportingIssues": [],
        "evidence": {
            "issue:21": {
                "kind": "issue-event",
                "url": "https://github.com/owner/repo/issues/21",
                "availability": "available",
                "payload": {
                    "number": 21,
                    "state": "open",
                    "facts": [
                        {
                            "field": "failureType",
                            "normalized": "main-repository-breakage",
                        },
                        {
                            "field": "errorCode",
                            "normalized": "CS0117",
                        },
                    ],
                },
            },
            "run:777": {
                "kind": "workflow-run",
                "url": "https://github.com/owner/repo/actions/runs/777",
                "availability": "available",
                "payload": {
                    "runId": 777,
                    "workflow": "CI",
                    "branch": "main",
                    "status": "completed",
                    "conclusion": "success",
                    "headSha": "abc123",
                    "runStartedAt": "2026-08-21T15:00:05Z",
                },
            },
            "pr:22": {
                "kind": "pull-request",
                "url": "https://github.com/owner/repo/pull/22",
                "availability": "available",
                "payload": {
                    "number": 22,
                    "state": "closed",
                    "mergedAt": "2026-08-21T15:00:00Z",
                    "mergeCommitSha": "abc123",
                },
            },
        },
        "collectionErrors": [],
        "warnings": [],
        "references": {},
    }


def _prepared() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "repository": "owner/repo",
        "sourceCollectedAt": "2026-08-21T16:00:00Z",
        "snapshotId": "snapshot:owner/repo:2026-08-21T16:00:00Z",
        "issues": [
            {
                "issueNumber": 21,
                "issueUrl": "https://github.com/owner/repo/issues/21",
                "title": "One transient failure",
                "evidenceBundle": [
                    {"id": "issue:21", "kind": "issue-event"},
                    {"id": "run:777", "kind": "workflow-run"},
                    {"id": "pr:22", "kind": "pull-request"},
                ],
            }
        ],
    }


def _judgments() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "snapshotId": "snapshot:owner/repo:2026-08-21T16:00:00Z",
        "issues": [
            {
                "issueNumber": 21,
                "category": "transient-infrastructure",
                "recommendations": [
                    {
                        "disposition": "watch",
                        "target": {"kind": "workflow-run", "value": "777"},
                        "confidence": "medium",
                        "summary": "One matching failure has been observed.",
                        "evidenceIds": ["issue:21", "run:777"],
                        "missingEvidence": ["another independent occurrence"],
                        "reassessWhen": (
                            "After another independent matching failure or "
                            "a covered successful execution."
                        ),
                    }
                ],
            }
        ],
    }


def _investigate_judgments() -> dict[str, object]:
    judgments = _judgments()
    issue = judgments["issues"][0]
    assert isinstance(issue, dict)
    issue["category"] = "unknown"
    recommendations = issue["recommendations"]
    assert isinstance(recommendations, list)
    recommendation = recommendations[0]
    assert isinstance(recommendation, dict)
    recommendation.update(
        {
            "disposition": "investigate",
            "target": {"kind": "issue", "value": 21},
            "confidence": "low",
            "summary": "Investigate the missing diagnostic identity.",
            "missingEvidence": ["diagnostic logs"],
            "reassessWhen": "After the bounded investigation completes.",
        }
    )
    return judgments


def _resolved_prepared() -> dict[str, object]:
    prepared = _prepared()
    issue = prepared["issues"][0]
    assert isinstance(issue, dict)
    issue.update(
        {
            "candidateState": "resolved",
            "candidateAction": "recommend-close",
            "resolutionEvidence": {
                "runEvidenceId": "run:777",
                "pullRequestEvidenceId": "pr:22",
                "mergeCommitSha": "abc123",
                "mergedAt": "2026-08-21T15:00:00Z",
                "successfulRunStartedAt": "2026-08-21T15:00:05Z",
            },
        }
    )
    return prepared


def _close_judgments() -> dict[str, object]:
    judgments = _judgments()
    issue = judgments["issues"][0]
    assert isinstance(issue, dict)
    recommendations = issue["recommendations"]
    assert isinstance(recommendations, list)
    recommendation = recommendations[0]
    assert isinstance(recommendation, dict)
    recommendation.update(
        {
            "disposition": "review-close",
            "target": {"kind": "issue", "value": 21},
            "summary": "Review this issue for closure.",
            "evidenceIds": ["issue:21", "run:777", "pr:22"],
            "missingEvidence": [],
            "reassessWhen": "After the next positive evidence or human review.",
        }
    )
    return judgments


def _duplicate_agent_input() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "snapshotId": "snapshot:owner/repo:2026-08-21T16:00:00Z",
        "repository": "owner/repo",
        "issues": [
            {
                "issueNumber": 21,
                "actionCluster": {
                    "canonicalIssueNumber": 20,
                    "memberIssueNumbers": [20, 21],
                    "relationship": "same-error-code",
                    "role": "superseded",
                },
            }
        ],
    }


def _recovered_run_agent_input() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "snapshotId": "snapshot:owner/repo:2026-08-21T16:00:00Z",
        "repository": "owner/repo",
        "issues": [
            {
                "issueNumber": 21,
                "recoveredRunEvidenceId": "run:777",
            }
        ],
    }


def _duplicate_judgments() -> dict[str, object]:
    judgments = _close_judgments()
    issue = judgments["issues"][0]
    assert isinstance(issue, dict)
    recommendations = issue["recommendations"]
    assert isinstance(recommendations, list)
    recommendation = recommendations[0]
    assert isinstance(recommendation, dict)
    recommendation.update(
        {
            "summary": "Review closure as a superseded duplicate of canonical issue #20.",
            "evidenceIds": ["issue:21"],
            "reassessWhen": "If canonical issue #20 no longer tracks the shared failure.",
        }
    )
    return judgments


def _ping_human_judgments() -> dict[str, object]:
    judgments = _judgments()
    issue = judgments["issues"][0]
    assert isinstance(issue, dict)
    recommendations = issue["recommendations"]
    assert isinstance(recommendations, list)
    recommendation = recommendations[0]
    assert isinstance(recommendation, dict)
    recommendation.update(
        {
            "disposition": "ping-human",
            "target": {"kind": "issue", "value": 21},
            "summary": "A workflow owner must choose the recovery policy.",
            "evidenceIds": ["issue:21", "run:777"],
            "missingEvidence": [],
            "reassessWhen": "After the owner records the policy decision.",
            "humanEscalation": {
                "context": "The release lane still lacks a recovery policy.",
                "whyHuman": "The repository does not encode the intended policy.",
                "question": "Should the failed lane retry or remain blocked?",
                "suggestedNextSteps": [
                    "Choose the intended retry policy.",
                    "Record it in the workflow configuration.",
                ],
                "routingHint": "release-infrastructure",
            },
        }
    )
    return judgments


def _with_owned_comment(
    snapshot: dict[str, object],
    body: str,
    *,
    comment_id: int = 900,
    idempotency_key: str = "issue:21:status",
) -> dict[str, object]:
    result = copy.deepcopy(snapshot)
    evidence = result["evidence"]
    assert isinstance(evidence, dict)
    evidence[f"issue:21:comment:{comment_id}"] = {
        "kind": "issue-comment",
        "url": (
            "https://github.com/owner/repo/issues/21"
            f"#issuecomment-{comment_id}"
        ),
        "availability": "available",
        "payload": {
            "id": comment_id,
            "sourceIssueNumber": 21,
            "author": "ankj",
            "body": body,
            "markers": [],
            "facts": [],
            "references": [],
            "shepherdStatus": {
                "role": "status",
                "idempotencyKey": idempotency_key,
                "owned": True,
            },
        },
    }
    return result


class WatchActionTests(unittest.TestCase):
    def test_legacy_watch_comment_is_migrated_in_place(self) -> None:
        result = build_watch_proposals(
            _with_owned_comment(
                _snapshot(),
                "[automated] Old watch status",
                idempotency_key="issue:21:watch",
            ),
            _prepared(),
            _judgments(),
            "ankj",
        )

        self.assertEqual(1, len(result["proposals"]))
        proposal = result["proposals"][0]
        self.assertEqual("edit-comment", proposal["operation"])
        self.assertEqual(900, proposal["commentId"])
        self.assertEqual("issue:21:status", proposal["idempotencyKey"])

    def test_newest_legacy_status_comment_is_migrated_when_multiple_exist(self) -> None:
        snapshot = _with_owned_comment(
            _snapshot(),
            "[automated] Old watch status",
            comment_id=900,
            idempotency_key="issue:21:watch",
        )
        snapshot = _with_owned_comment(
            snapshot,
            "[automated] Old close status",
            comment_id=901,
            idempotency_key="issue:21:review-close",
        )

        result = build_action_proposals(
            snapshot,
            _resolved_prepared(),
            _close_judgments(),
            "ankj",
        )

        comment, close = result["proposals"]
        self.assertEqual("edit-comment", comment["operation"])
        self.assertEqual(901, comment["commentId"])
        self.assertEqual("issue:21:status", comment["idempotencyKey"])
        self.assertEqual(comment["actionId"], close["dependsOn"])

    def test_review_close_wins_over_watch_for_the_canonical_status_comment(self) -> None:
        judgments = _close_judgments()
        issue = judgments["issues"][0]
        assert isinstance(issue, dict)
        recommendations = issue["recommendations"]
        assert isinstance(recommendations, list)
        recommendations.append(copy.deepcopy(_judgments()["issues"][0]["recommendations"][0]))

        result = build_action_proposals(
            _snapshot(),
            _resolved_prepared(),
            judgments,
            "ankj",
        )

        self.assertEqual(
            ["create-comment", "close-issue"],
            [proposal["operation"] for proposal in result["proposals"]],
        )
        self.assertIn("supports closing this issue", result["proposals"][0]["body"])

    def test_ping_human_edits_the_canonical_status_comment(self) -> None:
        result = build_action_proposals(
            _with_owned_comment(_snapshot(), "[automated] Old watch status"),
            _prepared(),
            _ping_human_judgments(),
            "ankj",
        )

        self.assertEqual(1, len(result["proposals"]))
        proposal = result["proposals"][0]
        self.assertEqual("edit-comment", proposal["operation"])
        self.assertEqual(900, proposal["commentId"])
        self.assertEqual("issue:21:status", proposal["idempotencyKey"])
        self.assertIn(
            "**Decision needed:** Should the failed lane retry or remain blocked?",
            proposal["body"],
        )
        self.assertIn("`release-infrastructure`", proposal["body"])
        self.assertIn("<!-- ci-shepherd:role=status -->", proposal["body"])

    def test_build_action_proposals_renders_resolved_review_close(self) -> None:
        result = build_action_proposals(
            _snapshot(),
            _resolved_prepared(),
            _close_judgments(),
            "ankj",
        )

        self.assertEqual(
            ["create-comment", "close-issue"],
            [proposal["operation"] for proposal in result["proposals"]],
        )
        comment, close = result["proposals"]
        self.assertTrue(comment["body"].startswith("[automated] "))
        self.assertIn("Review this issue for closure.", comment["body"])
        self.assertIn(
            "https://github.com/owner/repo/actions/runs/777",
            comment["body"],
        )
        self.assertIn(
            "compiler error `CS0117`",
            comment["body"],
        )
        self.assertIn(
            "PR [#22](https://github.com/owner/repo/pull/22) merged commit "
            "`abc123`",
            comment["body"],
        )
        self.assertIn(
            "CI run [777](https://github.com/owner/repo/actions/runs/777) "
            "completed successfully on `main` for that exact merge commit",
            comment["body"],
        )
        self.assertIn(
            "That successful post-fix run satisfies the recovery gate",
            comment["body"],
        )
        self.assertIn(
            "**Resolution:** The recovery evidence supports closing this issue "
            "as completed.",
            comment["body"],
        )
        self.assertNotIn("Proposed action", comment["body"])
        self.assertNotIn("separate approval", comment["body"])
        self.assertTrue(comment["requiresSeparateApproval"])
        self.assertEqual("completed", close["closeReason"])
        self.assertTrue(close["requiresSeparateApproval"])
        self.assertEqual(comment["actionId"], close["dependsOn"])

    def test_build_action_proposals_renders_direct_run_recovery_close(self) -> None:
        result = build_action_proposals(
            _snapshot(),
            _prepared(),
            _close_judgments(),
            "ankj",
            agent_input=_recovered_run_agent_input(),
        )

        self.assertEqual(
            ["create-comment", "close-issue"],
            [proposal["operation"] for proposal in result["proposals"]],
        )
        self.assertIn(
            "completed successfully on `main` after the last recorded failure",
            result["proposals"][0]["body"],
        )

    def test_direct_run_recovery_satisfies_missing_verified_fix(self) -> None:
        judgments = _close_judgments()
        recommendation = judgments["issues"][0]["recommendations"][0]
        assert isinstance(recommendation, dict)
        recommendation["missingEvidence"] = [
            "occurrence-run-timestamp-for-fix-day",
            "verified-fix",
        ]

        result = build_action_proposals(
            _snapshot(),
            _prepared(),
            judgments,
            "ankj",
            agent_input=_recovered_run_agent_input(),
        )

        self.assertEqual(
            ["create-comment", "close-issue"],
            [proposal["operation"] for proposal in result["proposals"]],
        )

    def test_build_action_proposals_rejects_unresolved_review_close(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "review-close requires deterministic resolution evidence",
        ):
            build_action_proposals(
                _snapshot(),
                _prepared(),
                _close_judgments(),
                "ankj",
            )

    def test_build_action_proposals_renders_superseded_duplicate_close(self) -> None:
        result = build_action_proposals(
            _snapshot(),
            _prepared(),
            _duplicate_judgments(),
            "ankj",
            agent_input=_duplicate_agent_input(),
        )

        self.assertEqual(
            ["create-comment", "close-issue"],
            [proposal["operation"] for proposal in result["proposals"]],
        )
        comment, close = result["proposals"]
        self.assertIn(
            "canonical issue [#20](https://github.com/owner/repo/issues/20)",
            comment["body"],
        )
        self.assertIn(
            "**Resolution:** The duplicate relationship supports closing this issue "
            "as a duplicate.",
            comment["body"],
        )
        self.assertEqual("duplicate", close["closeReason"])
        self.assertEqual(comment["actionId"], close["dependsOn"])

    def test_build_watch_proposals_renders_new_status_comment(self) -> None:
        result = build_watch_proposals(
            _snapshot(),
            _prepared(),
            _judgments(),
            "ankj",
        )

        self.assertEqual([], result["unchangedIssueNumbers"])
        self.assertEqual(1, len(result["proposals"]))
        proposal = result["proposals"][0]
        self.assertEqual("create-comment", proposal["operation"])
        self.assertIs(True, proposal["requiresSeparateApproval"])
        self.assertEqual("issue:21:status", proposal["idempotencyKey"])
        self.assertTrue(proposal["body"].startswith("[automated] "))
        self.assertIn(
            "One matching failure has been observed.",
            proposal["body"],
        )
        self.assertIn(
            "After another independent matching failure or "
            "a covered successful execution.",
            proposal["body"],
        )
        self.assertIn(
            "https://github.com/owner/repo/actions/runs/777",
            proposal["body"],
        )
        self.assertIn(
            "<!-- ci-shepherd:role=status -->",
            proposal["body"],
        )
        self.assertIn(
            "<!-- ci-shepherd:idempotency-key=issue:21:status -->",
            proposal["body"],
        )

    def test_report_only_investigation_does_not_propose_a_status_comment(self) -> None:
        result = build_action_proposals(
            _snapshot(),
            _prepared(),
            _investigate_judgments(),
            "ankj",
        )

        self.assertEqual([], result["proposals"])

    def test_investigation_retires_an_existing_watch_comment(self) -> None:
        result = build_action_proposals(
            _with_owned_comment(
                _snapshot(),
                "[automated] The CI shepherd is watching this failure.",
            ),
            _prepared(),
            _investigate_judgments(),
            "ankj",
        )

        self.assertEqual(1, len(result["proposals"]))
        proposal = result["proposals"][0]
        self.assertEqual("edit-comment", proposal["operation"])
        self.assertEqual(900, proposal["commentId"])
        self.assertEqual("issue:21:status", proposal["idempotencyKey"])
        self.assertIn("no longer watching or requesting input", proposal["body"])
        self.assertIn("report-only investigation", proposal["body"])

    def test_retired_investigation_comment_is_not_edited_repeatedly(self) -> None:
        first = build_action_proposals(
            _with_owned_comment(
                _snapshot(),
                "[automated] The CI shepherd is watching this failure.",
            ),
            _prepared(),
            _investigate_judgments(),
            "ankj",
        )
        retired_body = first["proposals"][0]["body"]

        result = build_action_proposals(
            _with_owned_comment(_snapshot(), retired_body),
            _prepared(),
            _investigate_judgments(),
            "ankj",
        )

        self.assertEqual([], result["proposals"])
        self.assertEqual([21], result["unchangedIssueNumbers"])

    def test_multiple_report_only_investigations_share_one_retirement_edit(self) -> None:
        judgments = _investigate_judgments()
        recommendations = judgments["issues"][0]["recommendations"]
        assert isinstance(recommendations, list)
        second = copy.deepcopy(recommendations[0])
        second.update(
            {
                "target": {
                    "kind": "failure-fingerprint",
                    "value": "second-cause",
                },
                "summary": "Investigate the second failure target.",
                "evidenceIds": ["issue:21", "pr:22"],
            }
        )
        recommendations.append(second)

        result = build_action_proposals(
            _with_owned_comment(
                _snapshot(),
                "[automated] The CI shepherd is watching this failure.",
            ),
            _prepared(),
            judgments,
            "ankj",
        )

        self.assertEqual(1, len(result["proposals"]))
        proposal = result["proposals"][0]
        self.assertEqual("edit-comment", proposal["operation"])
        self.assertEqual(["issue:21", "run:777", "pr:22"], proposal["evidenceIds"])

    def test_build_watch_proposals_edits_changed_owned_comment(self) -> None:
        result = build_watch_proposals(
            _with_owned_comment(_snapshot(), "[automated] Old status"),
            _prepared(),
            _judgments(),
            "ankj",
        )

        self.assertEqual(1, len(result["proposals"]))
        proposal = result["proposals"][0]
        self.assertEqual("edit-comment", proposal["operation"])
        self.assertEqual(900, proposal["commentId"])

    def test_build_watch_proposals_omits_identical_owned_comment(self) -> None:
        first = build_watch_proposals(
            _snapshot(),
            _prepared(),
            _judgments(),
            "ankj",
        )
        body = first["proposals"][0]["body"]
        snapshot = _with_owned_comment(_snapshot(), f"{body}\n")

        result = build_watch_proposals(
            snapshot,
            _prepared(),
            _judgments(),
            "ankj",
        )

        self.assertEqual([], result["proposals"])
        self.assertEqual([21], result["unchangedIssueNumbers"])

    def test_build_watch_proposals_rejects_multiple_owned_comments(self) -> None:
        first = build_watch_proposals(
            _snapshot(),
            _prepared(),
            _judgments(),
            "ankj",
        )
        snapshot = _with_owned_comment(
            _with_owned_comment(_snapshot(), first["proposals"][0]["body"]),
            first["proposals"][0]["body"],
            comment_id=901,
        )

        with self.assertRaisesRegex(
            ValueError,
            "multiple owned canonical status comments",
        ):
            build_watch_proposals(snapshot, _prepared(), _judgments(), "ankj")

    def test_build_watch_proposals_rejects_multiple_watch_recommendations(self) -> None:
        judgments = _judgments()
        issue = judgments["issues"][0]
        assert isinstance(issue, dict)
        recommendations = issue["recommendations"]
        assert isinstance(recommendations, list)
        second = copy.deepcopy(recommendations[0])
        second["target"] = {"kind": "workflow-run", "value": "778"}
        recommendations.append(second)

        with self.assertRaisesRegex(
            ValueError,
            "multiple watch recommendations",
        ):
            build_watch_proposals(
                _snapshot(),
                _prepared(),
                judgments,
                "ankj",
            )


if __name__ == "__main__":
    unittest.main()
