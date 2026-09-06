from __future__ import annotations

import copy
import hashlib
import unittest

from ci_shepherd.actions import build_action_proposals, build_watch_proposals
from ci_shepherd.actor import build_dry_run, validate_action_proposals
from ci_shepherd.models import stable_json
from ci_shepherd.quarantine_reconciliation import reconcile_quarantine_source
from ci_shepherd.lifecycle import prepare_assessment
from ci_shepherd.poc import build_compact_poc_input
from tests.recovery_fixtures import with_exact_coverage


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
                    "title": "One transient failure",
                    "updatedAt": "2026-08-21T15:59:00Z",
                    "labels": [{"name": "ci-failure-cause"}],
                    "occurrences": [
                        {
                            "date": "2026-08-21",
                            "sourceRun": 777,
                            "job": "CI",
                            "pullRequest": None,
                        }
                    ],
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
                    "referencedBy": [
                        {
                            "sourceIssueNumber": 21,
                            "extractionMethod": "full-pull-url",
                        }
                    ],
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


def _delegate_judgments() -> dict[str, object]:
    judgments = _investigate_judgments()
    issue = judgments["issues"][0]
    assert isinstance(issue, dict)
    issue["category"] = "product-or-tooling"
    recommendations = issue["recommendations"]
    assert isinstance(recommendations, list)
    recommendation = recommendations[0]
    assert isinstance(recommendation, dict)
    recommendation.update(
        {
            "disposition": "delegate-copilot",
            "confidence": "medium",
            "summary": "The repository code has an actionable product defect.",
            "missingEvidence": [],
            "reassessWhen": "After the delegated task or pull request changes state.",
        }
    )
    return judgments


def _no_action_judgments() -> dict[str, object]:
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
            "disposition": "no-action",
            "target": {"kind": "issue", "value": 21},
            "confidence": "low",
            "summary": "The available evidence does not authorize an action.",
            "missingEvidence": [],
            "reassessWhen": "After material evidence changes.",
        }
    )
    return judgments


def _resolved_prepared() -> dict[str, object]:
    return prepare_assessment(_recovery_snapshot())


def _recovery_snapshot() -> dict[str, object]:
    value = _snapshot()
    issue = value["evidence"]["issue:21"]["payload"]
    issue.update({
        "title": "[Main CI Failure] Compilation failed",
        "url": "https://github.com/owner/repo/issues/21",
        "producer": "ci-failure-cause",
        "ledger": {"complete": True, "schemaRecognized": True, "parsedRowCount": 1,
                   "rows": [{"date": "2026-08-20", "sourceRun": 776, "job": "Build"}]},
    })
    return with_exact_coverage(value)


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
            "evidenceIds": [*_resolved_prepared()["issues"][0]["recovery"]["evidenceIds"], "pr:22"],
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
    return build_compact_poc_input(_resolved_prepared())


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


def _prepare_handoff(snapshot: dict) -> dict:
    from datetime import timedelta
    from ci_shepherd.handoff_reminders import derive_handoff_reminders
    from ci_shepherd.repository_policy import HandoffReminderPolicy

    for record in snapshot["delegationStatus"]["records"]:
        record.update(issueOpen=True, copilotAssigned=True, humanAssigned=False)
        record.setdefault("handoffStartedAt", record["startedAt"])
        if "handoffReminder" not in record:
            derive_handoff_reminders([record], [], HandoffReminderPolicy(
                interval=timedelta(days=1), stale_progress_interval=timedelta(days=7), maximum=2,
            ))
    return prepare_assessment({
        **snapshot, "openIssues": sorted(set(snapshot["openIssues"]) | set(snapshot.get("delegatedIssues", []))),
    })


def _current_code_handoff() -> tuple[dict, dict, dict]:
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from ci_shepherd.investigations import attach_latest_investigation_results
    from tests.test_production_decisions import completed_investigation

    snapshot = _recovery_snapshot()
    snapshot["repositoryPolicy"] = {"quarantinePullRequest": {"baseRef": "main"}}
    for record in snapshot["evidence"].values():
        if record["kind"] == "workflow-job" and record["payload"]["conclusion"] == "success":
            record["payload"]["conclusion"] = "skipped"
    prepared = prepare_assessment(snapshot)
    compact = build_compact_poc_input(prepared)
    judgments = {"schemaVersion": 1, "snapshotId": prepared["snapshotId"],
                 "issues": [compact["issues"][0]["defaultJudgment"]]}
    with TemporaryDirectory() as directory:
        result = completed_investigation(Path(directory), prepared, judgments, outcome="fixable")
    prepared = attach_latest_investigation_results(prepared, [result])
    judgments["issues"] = [build_compact_poc_input(prepared)["issues"][0]["defaultJudgment"]]
    return snapshot, prepared, judgments


def _handoff_judgments(snapshot: dict) -> dict:
    compact = build_compact_poc_input(_prepare_handoff(snapshot))
    return {"schemaVersion": 1, "snapshotId": compact["snapshotId"],
            "issues": [issue["defaultJudgment"] for issue in compact["issues"]]}


class WatchActionTests(unittest.TestCase):
    def test_delegate_copilot_recommendation_creates_assignment_proposal(self) -> None:
        snapshot, prepared, judgments = _current_code_handoff()

        proposals = build_action_proposals(
            snapshot,
            prepared,
            judgments,
            "ankj",
        )

        self.assertEqual(1, len(proposals["proposals"]))
        proposal = proposals["proposals"][0]
        self.assertEqual("assign-copilot", proposal["operation"])
        self.assertEqual("owner/repo", proposal["targetRepository"])
        self.assertEqual("main", proposal["baseBranch"])
        self.assertEqual("", proposal["model"])
        self.assertIn("Fixes #21", proposal["customInstructions"])
        self.assertNotIn("Demo.Tests.Flaky", proposal["customInstructions"])
        self.assertNotIn("[QuarantinedTest]", proposal["customInstructions"])
        self.assertEqual(
            "issue:21:copilot-assignment:episode-1",
            proposal["idempotencyKey"],
        )
        build_dry_run(proposals, action_id=str(proposal["actionId"]))

    def test_materially_new_delegation_uses_new_episode_identity(self) -> None:
        snapshot, prepared, judgments = _current_code_handoff()
        first = build_action_proposals(
            snapshot,
            prepared,
            judgments,
            "ankj",
        )
        successor_snapshot = copy.deepcopy(snapshot)
        successor_snapshot["delegationStatus"] = {
            "status": "complete",
            "records": [],
            "episodeOrdinals": {"21": 2},
        }

        successor = build_action_proposals(
            successor_snapshot,
            prepared,
            judgments,
            "ankj",
        )
        replay = build_action_proposals(
            successor_snapshot,
            prepared,
            judgments,
            "ankj",
        )

        self.assertNotEqual(
            first["proposals"][0]["idempotencyKey"],
            successor["proposals"][0]["idempotencyKey"],
        )
        self.assertEqual(
            successor["proposals"][0]["idempotencyKey"],
            replay["proposals"][0]["idempotencyKey"],
        )

    def test_model_only_delegation_recommendation_is_blocked(self) -> None:
        prepared = _prepared()
        prepared["repositoryPolicy"] = {
            "quarantinePullRequest": {"baseRef": "main"},
        }

        proposals = build_action_proposals(
            _snapshot(),
            prepared,
            _delegate_judgments(),
            "ankj",
        )

        self.assertEqual([], proposals["proposals"])
        self.assertEqual(
            ["machine-actionability-not-verified"],
            proposals["blockedRecommendations"][0]["blockingReasons"],
        )

    def test_no_action_with_verified_quarantine_does_not_propose_assignment(
        self,
    ) -> None:
        prepared = _prepared()
        prepared["repositoryPolicy"] = {
            "quarantinePullRequest": {"baseRef": "main"},
        }

        proposals = build_action_proposals(
            _snapshot(),
            prepared,
            _no_action_judgments(),
            "ankj",
            quarantine_reconciliation=_verified_reconciliation(),
        )

        self.assertEqual(
            [],
            [
                proposal
                for proposal in proposals["proposals"]
                if proposal["operation"] == "assign-copilot"
            ],
        )

    def test_frozen_prior_live_quarantine_replay_preserves_comment_bytes(
        self,
    ) -> None:
        baseline = build_action_proposals(
            _snapshot(),
            _prepared(),
            _judgments(),
            "ankj",
        )
        snapshot = _snapshot()
        prepared = _prepared()
        judgments = _judgments()
        reconciliation = _verified_reconciliation()
        reconciliation["verifiedIssues"] = [
            {
                "issueNumber": 6866,
                "issueUrl": "https://github.com/microsoft/aspire/issues/6866",
                "tests": [
                    {
                        "file": "Aspire.Playground.Tests/AppHostTests.cs",
                        "line": 33,
                        "testName": (
                            "Aspire.Playground.Tests.AppHostTests."
                            "TestEndpointsReturnOk"
                        ),
                    }
                ],
            },
            {
                "issueNumber": 8728,
                "issueUrl": "https://github.com/microsoft/aspire/issues/8728",
                "tests": [
                    {
                        "file": (
                            "Aspire.Hosting.Tests/"
                            "DistributedApplicationTests.cs"
                        ),
                        "line": 1708,
                        "testName": (
                            "Aspire.Hosting.Tests.DistributedApplicationTests."
                            "ProxylessEndpointWorks"
                        ),
                    }
                ],
            },
        ]
        for issue_number in (6866, 8728):
            snapshot["openIssues"].append(issue_number)
            snapshot["issues"].append({"number": issue_number, "state": "open"})
            snapshot["evidence"][f"issue:{issue_number}"] = {
                "kind": "issue-event",
                "url": f"https://github.com/microsoft/aspire/issues/{issue_number}",
                "availability": "available",
                "payload": {
                    "number": issue_number,
                    "state": "open",
                    "title": "Frozen prior-live no-action issue",
                    "updatedAt": "2026-09-04T18:40:55Z",
                    "labels": [{"name": "test-failure"}],
                    "occurrences": [],
                    "facts": [],
                },
            }
            prepared["issues"].append(
                {
                    "issueNumber": issue_number,
                    "issueUrl": (
                        f"https://github.com/microsoft/aspire/issues/{issue_number}"
                    ),
                    "title": "Frozen prior-live no-action issue",
                    "evidenceBundle": [
                        {
                            "id": f"issue:{issue_number}",
                            "kind": "issue-event",
                        }
                    ],
                }
            )
            judgments["issues"].append(
                {
                    "category": "unknown",
                    "issueNumber": issue_number,
                    "recommendations": [
                        {
                            "confidence": "medium",
                            "disposition": "no-action",
                            "evidenceIds": [f"issue:{issue_number}"],
                            "missingEvidence": ["recognized-producer-ledger"],
                            "reassessWhen": (
                                "When automation ownership or blockers change."
                            ),
                            "summary": "No shepherd action is needed.",
                            "target": {
                                "kind": "issue",
                                "value": issue_number,
                            },
                        }
                    ],
                }
            )

        replay = build_action_proposals(
            snapshot,
            prepared,
            judgments,
            "ankj",
            quarantine_reconciliation=reconciliation,
        )
        production_prepared = prepare_assessment(snapshot)
        production_compact = build_compact_poc_input(production_prepared)
        production_judgments = {
            "schemaVersion": 1, "snapshotId": production_prepared["snapshotId"],
            "issues": [issue["defaultJudgment"] for issue in production_compact["issues"]],
        }
        production_proposals = build_action_proposals(
            snapshot, production_prepared, production_judgments, "ankj",
            agent_input=production_compact, quarantine_reconciliation=reconciliation,
        )
        self.assertEqual([], [
            proposal for proposal in production_proposals["proposals"]
            if proposal["issueNumber"] in {6866, 8728} and proposal["operation"] == "assign-copilot"
        ])

        self.assertEqual(
            stable_json(baseline["proposals"]),
            stable_json(replay["proposals"]),
        )
        self.assertEqual(
            [],
            [
                proposal
                for proposal in replay["proposals"]
                if proposal["issueNumber"] in {6866, 8728}
                and proposal["operation"] == "assign-copilot"
            ],
        )

    def test_delegate_copilot_rejects_flake_classification(self) -> None:
        judgments = _delegate_judgments()
        issue = judgments["issues"][0]
        assert isinstance(issue, dict)
        issue["category"] = "flaky-test"

        with self.assertRaisesRegex(
            ValueError,
            "Flaky-test delegation requires a source-confirmed quarantine fix handoff",
        ):
            build_action_proposals(
                _snapshot(),
                _prepared(),
                judgments,
                "ankj",
            )

    def test_executable_proposal_carries_source_issue_version(self) -> None:
        proposals = build_action_proposals(
            _snapshot(),
            _prepared(),
            _judgments(),
            "ankj",
        )

        proposal = proposals["proposals"][0]
        self.assertEqual(
            {"issueUpdatedAt": "2026-08-21T15:59:00Z"},
            proposal["sourceEvidenceFingerprint"],
        )

    def test_unverified_bare_reference_blocks_the_complete_document(self) -> None:
        snapshot = _recovery_snapshot()
        pull_request = snapshot["evidence"]["pr:22"]
        pull_request["payload"]["referencedBy"] = [
            {
                "sourceIssueNumber": 21,
                "extractionMethod": "local-issue",
            }
        ]

        proposals = build_action_proposals(
            snapshot,
            _resolved_prepared(),
            _close_judgments(),
            "ankj",
        )

        self.assertEqual("blocked", proposals["executionEligibility"]["status"])
        for proposal in proposals["proposals"]:
            self.assertIn(
                "untrusted-reference-provenance",
                proposal["executionEligibility"]["blockingReasons"],
            )
        build_dry_run(proposals, action_id=None)

    def test_unlabeled_issue_is_explicitly_ineligible_for_execution(self) -> None:
        snapshot = _snapshot()
        issue = snapshot["evidence"]["issue:21"]["payload"]
        assert isinstance(issue, dict)
        issue["labels"] = []

        proposals = build_action_proposals(
            snapshot,
            _prepared(),
            _judgments(),
            "ankj",
        )

        self.assertEqual(2, proposals["schemaVersion"])
        self.assertFalse(
            proposals["proposals"][0]["executionEligibility"]["eligible"]
        )
        self.assertEqual("blocked", proposals["executionEligibility"]["status"])
        self.assertIn(
            "missing-ci-label",
            proposals["proposals"][0]["executionEligibility"]["blockingReasons"],
        )

    def test_unavailable_evidence_blocks_the_produced_action(self) -> None:
        snapshot = _snapshot()
        snapshot["evidence"]["run:777"]["availability"] = "unavailable"

        proposals = build_action_proposals(
            snapshot,
            _prepared(),
            _judgments(),
            "ankj",
        )

        eligibility = proposals["proposals"][0]["executionEligibility"]
        self.assertFalse(eligibility["eligible"])
        self.assertTrue(eligibility["collectionComplete"])
        self.assertEqual(["run:777"], eligibility["unavailableEvidenceIds"])
        self.assertEqual(
            ["unavailable-evidence"],
            eligibility["blockingReasons"],
        )

    def test_ci_label_matching_is_case_insensitive(self) -> None:
        snapshot = _snapshot()
        issue = snapshot["evidence"]["issue:21"]["payload"]
        assert isinstance(issue, dict)
        issue["labels"] = [{"name": "Test-Failure"}]

        proposals = build_action_proposals(
            snapshot,
            _prepared(),
            _judgments(),
            "ankj",
        )

        eligibility = proposals["proposals"][0]["executionEligibility"]
        self.assertTrue(eligibility["eligible"])
        self.assertEqual(["test-failure"], eligibility["ciLabels"])

    def test_any_collection_error_blocks_the_entire_proposal_document(self) -> None:
        snapshot = _snapshot()
        snapshot["collectionErrors"] = [
            {
                "stage": "comments",
                "endpoint": "/repos/owner/repo/issues/21/comments?page=1",
                "message": "request failed",
            }
        ]

        proposals = build_action_proposals(
            snapshot,
            _prepared(),
            _judgments(),
            "ankj",
        )

        eligibility = proposals["proposals"][0]["executionEligibility"]
        self.assertFalse(eligibility["eligible"])
        self.assertIn("incomplete-collection", eligibility["blockingReasons"])
        self.assertEqual(
            {
                "status": "blocked",
                "violations": [
                    {
                        "actionId": proposals["proposals"][0]["actionId"],
                        "blockingReasons": ["incomplete-collection"],
                    }
                ],
            },
            proposals["executionEligibility"],
        )

    def test_issue_scoped_collection_error_blocks_only_that_issue(self) -> None:
        snapshot = _snapshot()
        snapshot["openIssues"].append(22)
        snapshot["issues"].append({"number": 22, "state": "open"})
        issue_evidence = copy.deepcopy(snapshot["evidence"]["issue:21"])
        issue_evidence["url"] = "https://github.com/owner/repo/issues/22"
        issue_evidence["payload"]["number"] = 22
        snapshot["evidence"]["issue:22"] = issue_evidence
        snapshot["collectionErrors"] = [
            {
                "stage": "comments",
                "endpoint": "/repos/owner/repo/issues/21/comments",
                "message": "request failed",
                "scope": {"kind": "issue", "issueNumbers": [21]},
            }
        ]

        prepared = _prepared()
        prepared_issue = copy.deepcopy(prepared["issues"][0])
        prepared_issue.update(
            {
                "issueNumber": 22,
                "issueUrl": "https://github.com/owner/repo/issues/22",
            }
        )
        prepared_issue["evidenceBundle"][0]["id"] = "issue:22"
        prepared["issues"].append(prepared_issue)

        judgments = _judgments()
        second_issue = copy.deepcopy(judgments["issues"][0])
        second_issue["issueNumber"] = 22
        second_recommendation = second_issue["recommendations"][0]
        second_recommendation["evidenceIds"][0] = "issue:22"
        judgments["issues"].append(second_issue)

        proposals = build_action_proposals(snapshot, prepared, judgments, "ankj")

        by_issue = {
            proposal["issueNumber"]: proposal for proposal in proposals["proposals"]
        }
        self.assertFalse(by_issue[21]["executionEligibility"]["eligible"])
        self.assertTrue(by_issue[22]["executionEligibility"]["eligible"])
        self.assertEqual(
            "partially-eligible",
            proposals["executionEligibility"]["status"],
        )

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
        self.assertEqual(
            {
                "bodySha256": hashlib.sha256(
                    b"[automated] Old watch status"
                ).hexdigest()
            },
            proposal["sourceCommentFingerprint"],
        )

    def test_missing_source_comment_body_blocks_only_its_edit(self) -> None:
        snapshot = _with_owned_comment(
            _snapshot(),
            "[automated] Existing status",
        )
        del snapshot["evidence"]["issue:21:comment:900"]["payload"]["body"]
        snapshot["openIssues"].append(22)
        snapshot["issues"].append({"number": 22, "state": "open"})
        issue_evidence = copy.deepcopy(snapshot["evidence"]["issue:21"])
        issue_evidence["url"] = "https://github.com/owner/repo/issues/22"
        issue_evidence["payload"]["number"] = 22
        snapshot["evidence"]["issue:22"] = issue_evidence

        prepared = _prepared()
        prepared_issue = copy.deepcopy(prepared["issues"][0])
        prepared_issue["issueNumber"] = 22
        prepared_issue["issueUrl"] = "https://github.com/owner/repo/issues/22"
        prepared_issue["evidenceBundle"][0]["id"] = "issue:22"
        prepared["issues"].append(prepared_issue)

        judgments = _judgments()
        second_issue = copy.deepcopy(judgments["issues"][0])
        second_issue["issueNumber"] = 22
        second_issue["recommendations"][0]["evidenceIds"][0] = "issue:22"
        judgments["issues"].append(second_issue)

        result = build_action_proposals(snapshot, prepared, judgments, "ankj")

        by_issue = {
            proposal["issueNumber"]: proposal for proposal in result["proposals"]
        }
        self.assertFalse(by_issue[21]["executionEligibility"]["eligible"])
        self.assertEqual(
            ["source-comment-unavailable"],
            by_issue[21]["executionEligibility"]["blockingReasons"],
        )
        self.assertNotIn("sourceCommentFingerprint", by_issue[21])
        self.assertTrue(by_issue[22]["executionEligibility"]["eligible"])
        build_dry_run(result, action_id=None)

    def test_deleted_owned_comment_is_replaced_with_a_create_proposal(self) -> None:
        snapshot = _with_owned_comment(
            _snapshot(),
            "[automated] Existing status",
        )
        del snapshot["evidence"]["issue:21:comment:900"]

        result = build_watch_proposals(
            snapshot,
            _prepared(),
            _judgments(),
            "ankj",
        )

        self.assertEqual(1, len(result["proposals"]))
        proposal = result["proposals"][0]
        self.assertEqual("create-comment", proposal["operation"])
        self.assertNotIn("commentId", proposal)
        self.assertNotIn("sourceCommentFingerprint", proposal)

    def test_newest_legacy_status_comment_is_migrated_when_multiple_exist(self) -> None:
        snapshot = _with_owned_comment(
            _recovery_snapshot(),
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
            _recovery_snapshot(),
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

    def test_ping_human_ignores_evidence_citation_only_changes(self) -> None:
        first = build_action_proposals(
            _snapshot(),
            _prepared(),
            _ping_human_judgments(),
            "ankj",
        )
        body = first["proposals"][0]["body"]
        body = body.replace(
            "**Evidence reviewed:**",
            (
                "**Evidence reviewed:**\n"
                "- [issue:21:comment:900]"
                "(https://github.com/owner/repo/issues/21#issuecomment-900)"
            ),
        )

        result = build_action_proposals(
            _with_owned_comment(_snapshot(), body),
            _prepared(),
            _ping_human_judgments(),
            "ankj",
        )

        self.assertEqual([], result["proposals"])
        self.assertEqual([21], result["unchangedIssueNumbers"])

    def test_ping_human_uses_issue_state_without_requiring_occurrences(self) -> None:
        snapshot = _snapshot()
        payload = snapshot["evidence"]["issue:21"]["payload"]
        assert isinstance(payload, dict)
        payload["occurrences"] = []

        result = build_action_proposals(
            snapshot,
            _prepared(),
            _ping_human_judgments(),
            "ankj",
        )

        proposal = result["proposals"][0]
        self.assertEqual("issue-state", proposal["evidenceBasis"])
        self.assertTrue(proposal["executionEligibility"]["eligible"])
        self.assertEqual([], proposal["executionEligibility"]["blockingReasons"])

    def test_ping_human_blocks_body_that_contradicts_zero_occurrences(self) -> None:
        snapshot = _snapshot()
        payload = snapshot["evidence"]["issue:21"]["payload"]
        assert isinstance(payload, dict)
        payload["occurrences"] = []
        judgments = _ping_human_judgments()
        issue = judgments["issues"][0]
        assert isinstance(issue, dict)
        recommendation = issue["recommendations"][0]
        assert isinstance(recommendation, dict)
        recommendation["summary"] = "The release lane failed 10 consecutive times."

        result = build_action_proposals(
            snapshot,
            _prepared(),
            judgments,
            "ankj",
        )

        proposal = result["proposals"][0]
        self.assertFalse(proposal["executionEligibility"]["eligible"])
        self.assertIn(
            "body-occurrence-contradiction",
            proposal["executionEligibility"]["blockingReasons"],
        )
        rendered = build_dry_run(result, action_id=proposal["actionId"])
        self.assertFalse(rendered["actions"][0]["wouldExecute"])
        self.assertIn(
            "body-occurrence-contradiction",
            rendered["actions"][0]["blockingReasons"],
        )

    def test_build_action_proposals_renders_resolved_review_close(self) -> None:
        result = build_action_proposals(
            _recovery_snapshot(),
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
        self.assertIn("`CI` / `Build (ubuntu-latest)`", comment["body"])
        self.assertIn(
            "**Resolution:** The matched execution evidence supports closing this issue "
            "as completed.",
            comment["body"],
        )
        self.assertNotIn("Proposed action", comment["body"])
        self.assertNotIn("separate approval", comment["body"])
        self.assertNotIn("requiresSeparateApproval", comment)
        self.assertTrue(comment["executionEligibility"]["eligible"])
        self.assertEqual("completed", close["closeReason"])
        self.assertNotIn("requiresSeparateApproval", close)
        self.assertTrue(close["executionEligibility"]["eligible"])
        self.assertEqual(comment["actionId"], close["dependsOn"])

    def test_build_action_proposals_renders_direct_run_recovery_close(self) -> None:
        result = build_action_proposals(
            _recovery_snapshot(),
            _resolved_prepared(),
            _close_judgments(),
            "ankj",
            agent_input=_recovered_run_agent_input(),
        )

        self.assertEqual(
            ["create-comment", "close-issue"],
            [proposal["operation"] for proposal in result["proposals"]],
        )
        self.assertIn(
            "in `main` completed successfully after the recorded failure",
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
            _recovery_snapshot(),
            _resolved_prepared(),
            judgments,
            "ankj",
            agent_input=_recovered_run_agent_input(),
        )

        self.assertEqual(
            ["create-comment", "close-issue"],
            [proposal["operation"] for proposal in result["proposals"]],
        )

    def test_build_action_proposals_rejects_unresolved_review_close(self) -> None:
        snapshot = _snapshot()
        snapshot["openIssues"].append(22)
        snapshot["issues"].append({"number": 22, "state": "open"})
        issue_evidence = copy.deepcopy(snapshot["evidence"]["issue:21"])
        issue_evidence["url"] = "https://github.com/owner/repo/issues/22"
        issue_evidence["payload"]["number"] = 22
        snapshot["evidence"]["issue:22"] = issue_evidence

        prepared = _prepared()
        prepared_issue = copy.deepcopy(prepared["issues"][0])
        prepared_issue.update(
            {
                "issueNumber": 22,
                "issueUrl": "https://github.com/owner/repo/issues/22",
                "title": "Unsupported close recommendation",
            }
        )
        prepared_issue["evidenceBundle"][0]["id"] = "issue:22"
        prepared["issues"].append(prepared_issue)

        judgments = _judgments()
        closing_issue = copy.deepcopy(_close_judgments()["issues"][0])
        closing_issue["issueNumber"] = 22
        closing_recommendation = closing_issue["recommendations"][0]
        closing_recommendation["target"]["value"] = 22
        closing_recommendation["evidenceIds"] = ["issue:22", "run:777", "pr:22"]
        judgments["issues"].append(closing_issue)

        result = build_action_proposals(snapshot, prepared, judgments, "ankj")

        self.assertEqual(
            [21],
            [proposal["issueNumber"] for proposal in result["proposals"]],
        )
        self.assertEqual(
            [
                {
                    "issueNumber": 22,
                    "disposition": "review-close",
                    "blockingReasons": [
                        "missing-deterministic-resolution-evidence"
                    ],
                    "evidenceIds": ["issue:22", "run:777", "pr:22"],
                }
            ],
            result["blockedRecommendations"],
        )
        self.assertEqual(result, validate_action_proposals(result))

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

    def test_verified_quarantine_does_not_manufacture_blocked_delegation(
        self,
    ) -> None:
        prepared = _prepared()
        prepared["repositoryPolicy"] = {
            "quarantinePullRequest": {"baseRef": "main"},
        }

        result = build_action_proposals(
            _snapshot(),
            prepared,
            _duplicate_judgments(),
            "ankj",
            agent_input=_duplicate_agent_input(),
            quarantine_reconciliation=_verified_reconciliation(),
        )

        self.assertEqual(
            ["create-comment", "close-issue"],
            [proposal["operation"] for proposal in result["proposals"]],
        )
        self.assertEqual([], result["blockedRecommendations"])

    def test_superseded_duplicate_close_suppresses_model_delegation(self) -> None:
        prepared = _prepared()
        prepared["repositoryPolicy"] = {
            "quarantinePullRequest": {"baseRef": "main"},
        }
        judgments = _delegate_judgments()
        issue = judgments["issues"][0]
        assert isinstance(issue, dict)
        recommendations = issue["recommendations"]
        assert isinstance(recommendations, list)
        recommendations.append(
            {
                "confidence": "medium",
                "disposition": "review-close",
                "evidenceIds": ["issue:21"],
                "missingEvidence": [],
                "reassessWhen": (
                    "If canonical issue #20 no longer tracks the shared failure."
                ),
                "summary": (
                    "Review closure as a superseded duplicate of canonical issue #20."
                ),
                "target": {"kind": "test", "value": "Demo.Tests.Broken"},
            }
        )

        result = build_action_proposals(
            _snapshot(),
            prepared,
            judgments,
            "ankj",
            agent_input=_duplicate_agent_input(),
        )

        self.assertEqual(
            ["create-comment", "close-issue"],
            [proposal["operation"] for proposal in result["proposals"]],
        )
        self.assertEqual(
            [
                {
                    "issueNumber": 21,
                    "disposition": "delegate-copilot",
                    "blockingReasons": ["superseded-by-closure-review"],
                    "evidenceIds": ["issue:21", "run:777"],
                }
            ],
            result["blockedRecommendations"],
        )
        self.assertEqual(result, validate_action_proposals(result))

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
        self.assertEqual(
            {"status": "eligible", "violations": []},
            result["executionEligibility"],
        )
        self.assertEqual("create-comment", proposal["operation"])
        self.assertNotIn("requiresSeparateApproval", proposal)
        self.assertTrue(proposal["executionEligibility"]["eligible"])
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


class DelegationHandoffActionTests(unittest.TestCase):
    def test_handoff_for_issue_outside_open_inventory_does_not_propose_comment(
        self,
    ) -> None:
        snapshot = _snapshot()
        snapshot["openIssues"] = []
        snapshot["issues"] = []
        snapshot["delegationStatus"] = {
            "status": "complete",
            "records": [
                {
                    "actionId": "assignment:21",
                    "repository": "owner/repo",
                    "issueNumber": 21,
                    "startedAt": "2026-08-21T15:00:00Z",
                    "taskId": "task-21",
                    "taskState": "failed",
                    "lifecycle": "handoff_required",
                    "requiresHuman": True,
                    "pullRequests": [],
                }
            ],
        }
        prepared = _prepared()
        prepared["issues"] = []
        judgments = _judgments()
        judgments["issues"] = []

        proposals = build_action_proposals(
            snapshot,
            prepared,
            judgments,
            "ankj",
        )

        self.assertEqual([], proposals["proposals"])

    def test_delegated_issue_proposes_one_due_canonical_human_handoff(self) -> None:
        snapshot = _snapshot()
        snapshot["openIssues"] = []
        snapshot["issues"] = []
        snapshot["delegatedIssues"] = [21]
        snapshot["delegationStatus"] = {
            "status": "complete",
            "records": [
                {
                    "actionId": "assignment:21",
                    "repository": "owner/repo",
                    "issueNumber": 21,
                    "startedAt": "2026-08-21T15:00:00Z",
                    "taskId": "task-21",
                    "taskState": "waiting_for_user",
                    "lifecycle": "handoff_required",
                    "requiresHuman": True,
                    "issueOpen": True,
                    "copilotAssigned": True,
                    "handoffStartedAt": "2026-08-21T15:00:00Z",
                    "nextWakeup": {
                        "reason": "escalation-reminder",
                        "evaluateAt": "2026-08-21T15:00:00Z",
                    },
                    "handoffReminder": {
                        "episodeId": "assignment:21:handoff",
                        "ordinal": 1,
                        "state": "pending",
                        "nextWakeup": {
                            "reason": "escalation-reminder",
                            "evaluateAt": "2026-08-21T15:00:00Z",
                        },
                    },
                    "pullRequests": [
                        {
                            "databaseId": 101,
                            "globalId": "PR_101",
                            "number": 22,
                            "state": "open",
                            "isDraft": True,
                        }
                    ],
                }
            ],
        }

        proposals = build_action_proposals(
            snapshot,
            _prepare_handoff(snapshot),
            _handoff_judgments(snapshot),
            "ankj",
        )

        self.assertEqual(1, len(proposals["proposals"]))
        proposal = proposals["proposals"][0]
        self.assertEqual(
            "snapshot:owner/repo:2026-08-21T16:00:00Z:"
            "issue:21:ping-human-comment:"
            "assignment:21:handoff:reminder-1",
            proposal["actionId"],
        )
        self.assertEqual("create-comment", proposal["operation"])
        self.assertIn("Task `task-21`: waiting_for_user", proposal["body"])
        self.assertIn("PR #22 (open)", proposal["body"])
        self.assertIn("**Reminder:** 1", proposal["body"])
        self.assertNotIn("watch-comment", proposal["actionId"])
        build_dry_run(proposals, action_id=proposal["actionId"])
        replay_snapshot = _with_owned_comment(snapshot, str(proposal["body"]))

        replay = build_action_proposals(
            replay_snapshot,
            _prepare_handoff(replay_snapshot),
            _handoff_judgments(replay_snapshot),
            "ankj",
        )

        self.assertEqual([], replay["proposals"])
        self.assertEqual([21], replay["unchangedIssueNumbers"])

    def test_pending_reminder_before_its_wakeup_does_not_propose(self) -> None:
        snapshot = _snapshot()
        snapshot["delegationStatus"] = {
            "status": "complete",
            "records": [
                {
                    "actionId": "assignment:21",
                    "repository": "owner/repo",
                    "issueNumber": 21,
                    "startedAt": "2026-08-21T15:00:00Z",
                    "taskId": "task-21",
                    "taskState": "waiting_for_user",
                    "lifecycle": "handoff_required",
                    "requiresHuman": True,
                    "handoffStartedAt": "2026-08-21T15:00:00Z",
                    "handoffReminder": {
                        "episodeId": "assignment:21:handoff",
                        "ordinal": 2,
                        "state": "pending",
                        "nextWakeup": {
                            "reason": "escalation-reminder",
                            "evaluateAt": "2026-08-22T15:00:00Z",
                        },
                    },
                    "pullRequests": [],
                }
            ],
        }

        proposals = build_action_proposals(
            snapshot,
            _prepared(),
            _ping_human_judgments(),
            "ankj",
        )

        self.assertEqual([], proposals["proposals"])

    def test_handoff_wakeup_without_ping_human_judgment_cannot_propose(self) -> None:
        snapshot = _snapshot()
        snapshot["delegationStatus"] = {
            "status": "complete",
            "records": [
                {
                    "actionId": "assignment:21",
                    "repository": "owner/repo",
                    "issueNumber": 21,
                    "startedAt": "2026-08-21T15:00:00Z",
                    "taskId": "task-21",
                    "taskState": "completed",
                    "lifecycle": "handoff_required",
                    "requiresHuman": True,
                    "handoffStartedAt": "2026-08-21T15:00:00Z",
                    "nextWakeup": {
                        "reason": "escalation-reminder",
                        "evaluateAt": "2026-08-22T15:00:00Z",
                    },
                    "handoffReminder": {
                        "episodeId": "assignment:21:handoff",
                        "ordinal": 1,
                        "state": "pending",
                        "nextWakeup": {
                            "reason": "escalation-reminder",
                            "evaluateAt": "2026-08-22T15:00:00Z",
                        },
                    },
                    "pullRequests": [],
                }
            ],
        }

        proposals = build_action_proposals(
            snapshot,
            _prepared(),
            _judgments(),
            "ankj",
        )

        self.assertEqual([], proposals["proposals"])
        self.assertEqual(
            ["validated-ping-human-required"],
            proposals["blockedRecommendations"][0]["blockingReasons"],
        )

    def test_delegation_handoff_without_ci_label_is_not_executable(self) -> None:
        snapshot = _snapshot()
        issue_evidence = snapshot["evidence"]["issue:21"]
        assert isinstance(issue_evidence, dict)
        issue_payload = issue_evidence["payload"]
        assert isinstance(issue_payload, dict)
        issue_payload["labels"] = []
        snapshot["delegationStatus"] = {
            "status": "complete",
            "records": [
                {
                    "actionId": "assignment:21",
                    "repository": "owner/repo",
                    "issueNumber": 21,
                    "startedAt": "2026-08-21T15:00:00Z",
                    "taskId": "task-21",
                    "taskState": "waiting_for_user",
                    "lifecycle": "handoff_required",
                    "requiresHuman": True,
                    "pullRequests": [],
                }
            ],
        }

        proposals = build_action_proposals(
            snapshot,
            _prepare_handoff(snapshot),
            _handoff_judgments(snapshot),
            "ankj",
        )

        proposal = proposals["proposals"][0]
        self.assertEqual(
            ["missing-ci-label"],
            proposal["executionEligibility"]["blockingReasons"],
        )
        rendered = build_dry_run(proposals, action_id=proposal["actionId"])
        self.assertFalse(rendered["actions"][0]["wouldExecute"])

    def test_handoff_supersedes_model_status_recommendation(self) -> None:
        snapshot = _snapshot()
        snapshot["delegationStatus"] = {
            "status": "complete",
            "records": [
                {
                    "actionId": "assignment:21",
                    "repository": "owner/repo",
                    "issueNumber": 21,
                    "startedAt": "2026-08-21T15:00:00Z",
                    "taskId": "task-21",
                    "taskState": "failed",
                    "lifecycle": "handoff_required",
                    "requiresHuman": True,
                    "pullRequests": [],
                }
            ],
        }

        proposals = build_action_proposals(
            snapshot,
            _prepare_handoff(snapshot),
            _handoff_judgments(snapshot),
            "ankj",
        )

        self.assertEqual(1, len(proposals["proposals"]))
        self.assertIn(
            "ping-human-comment:assignment:21:handoff:reminder-1",
            proposals["proposals"][0]["actionId"],
        )
        build_dry_run(
            proposals,
            action_id=proposals["proposals"][0]["actionId"],
        )


class QuarantineSourceReconciliationActionTests(unittest.TestCase):
    def test_superseded_status_recommendations_pass_action_validation(self) -> None:
        for disposition, judgments in (
            ("watch", _judgments()),
            ("ping-human", _ping_human_judgments()),
            ("review-close", _close_judgments()),
        ):
            with self.subTest(disposition=disposition):
                result = build_action_proposals(
                    _recovery_snapshot(),
                    _resolved_prepared(),
                    judgments,
                    "ankj",
                    quarantine_reconciliation=_reconciliation(),
                )

                self.assertEqual(
                    [{
                        "issueNumber": 21,
                        "disposition": disposition,
                        "blockingReasons": [
                            "superseded-by-quarantine-source-reconciliation"
                        ],
                        "evidenceIds": judgments["issues"][0]["recommendations"][0]["evidenceIds"],
                    }],
                    result["blockedRecommendations"],
                )
                self.assertEqual(
                    ["source-reconciliation"],
                    [proposal["evidenceBasis"] for proposal in result["proposals"]],
                )
                self.assertEqual(result, validate_action_proposals(result))

    def test_source_reconciliation_comment_does_not_require_ci_occurrences(
        self,
    ) -> None:
        snapshot = _snapshot()
        issue = snapshot["evidence"]["issue:21"]["payload"]
        assert isinstance(issue, dict)
        issue["labels"] = [{"name": "quarantined-test"}]
        issue["occurrences"] = []

        result = build_action_proposals(
            snapshot,
            _prepared(),
            _judgments(),
            "ankj",
            quarantine_reconciliation=_reconciliation(),
        )

        proposal = result["proposals"][0]
        self.assertEqual("source-reconciliation", proposal["evidenceBasis"])
        self.assertTrue(proposal["executionEligibility"]["eligible"])
        self.assertEqual([], proposal["executionEligibility"]["blockingReasons"])
        self.assertEqual(
            {
                "issueUpdatedAt": "2026-08-21T15:59:00Z",
                "sourceRevision": "a" * 40,
                "sourceTreeDigest": "sha256:" + "b" * 64,
                "inspectorTreeDigest": "sha256:" + "c" * 64,
                "findingDigest": proposal["sourceEvidenceFingerprint"][
                    "findingDigest"
                ],
            },
            proposal["sourceEvidenceFingerprint"],
        )

    def test_source_reconciliation_requires_a_pinned_source_tree(self) -> None:
        reconciliation = _reconciliation()
        reconciliation["sourceTreeDigest"] = "sha256:not-a-digest"

        with self.assertRaisesRegex(ValueError, "sourceTreeDigest"):
            build_action_proposals(
                _snapshot(),
                _prepared(),
                _judgments(),
                "ankj",
                quarantine_reconciliation=reconciliation,
            )

    def test_label_without_attribute_uses_the_canonical_status_comment(self) -> None:
        result = build_action_proposals(
            _snapshot(),
            _prepared(),
            _judgments(),
            "ankj",
            quarantine_reconciliation=_reconciliation(),
        )

        self.assertEqual(1, len(result["proposals"]))
        proposal = result["proposals"][0]
        self.assertEqual("create-comment", proposal["operation"])
        self.assertEqual("issue:21:status", proposal["idempotencyKey"])
        self.assertEqual(["issue:21"], proposal["evidenceIds"])
        self.assertTrue(proposal["executionEligibility"]["eligible"])
        self.assertTrue(proposal["body"].startswith("[automated] "))
        self.assertIn("`Demo.Tests.Flaky`", proposal["body"])
        self.assertIn("`tests/Demo.Tests/Tests.cs:31`", proposal["body"])
        self.assertIn("no `[QuarantinedTest]` attribute", proposal["body"])
        self.assertIn("a" * 40, proposal["body"])
        self.assertEqual(
            [
                {
                    "kind": "source-method-match",
                    "testName": "Demo.Tests.Flaky",
                    "file": "Demo.Tests/Tests.cs",
                    "line": 31,
                    "quarantineIssueUrls": [],
                },
                {
                    "kind": "no-quarantine-link-to-current-issue",
                    "issueUrl": "https://github.com/owner/repo/issues/21",
                },
            ],
            proposal["licensedClaims"],
        )
        self.assertIn(
            "<!-- ci-shepherd:idempotency-key=issue:21:status -->",
            proposal["body"],
        )
        self.assertNotIn("ci-shepherd:finding-digest", proposal["body"])
        self.assertEqual(
            [
                {
                    "issueNumber": 21,
                    "disposition": "watch",
                    "blockingReasons": [
                        "superseded-by-quarantine-source-reconciliation"
                    ],
                    "evidenceIds": ["issue:21", "run:777"],
                }
            ],
            result["blockedRecommendations"],
        )

    def test_unchanged_reconciliation_comment_is_not_reproposed(self) -> None:
        first = build_action_proposals(
            _snapshot(),
            _prepared(),
            _judgments(),
            "ankj",
            quarantine_reconciliation=_reconciliation(),
        )
        body = first["proposals"][0]["body"]
        assert isinstance(body, str)

        result = build_action_proposals(
            _with_owned_comment(_snapshot(), body),
            _prepared(),
            _judgments(),
            "ankj",
            quarantine_reconciliation=_reconciliation(),
        )

        self.assertEqual([], result["proposals"])
        self.assertEqual([21], result["unchangedIssueNumbers"])

    def test_unchanged_reconciliation_finding_is_not_reproposed_for_new_revision(
        self,
    ) -> None:
        first = build_action_proposals(
            _snapshot(),
            _prepared(),
            _judgments(),
            "ankj",
            quarantine_reconciliation=_reconciliation(),
        )
        body = first["proposals"][0]["body"]
        assert isinstance(body, str)
        reconciliation = _reconciliation()
        reconciliation["sourceRevision"] = "e" * 40

        result = build_action_proposals(
            _with_owned_comment(_snapshot(), body),
            _prepared(),
            _judgments(),
            "ankj",
            quarantine_reconciliation=reconciliation,
        )

        self.assertEqual([], result["proposals"])
        self.assertEqual([21], result["unchangedIssueNumbers"])

    def test_unresolved_test_name_does_not_claim_source_method_is_absent(
        self,
    ) -> None:
        reconciliation = _reconciliation()
        finding = reconciliation["findings"][0]
        assert isinstance(finding, dict)
        finding["kind"] = "unresolved-test-identity"
        finding["claimedTestName"] = None
        finding["currentSource"] = []

        result = build_action_proposals(
            _snapshot(),
            _prepared(),
            _judgments(),
            "ankj",
            quarantine_reconciliation=reconciliation,
        )

        body = result["proposals"][0]["body"]
        self.assertNotIn("No matching method exists in the inspected source.", body)
        self.assertIn("no source method was checked", body)
        self.assertIn(
            "The test may still be quarantined against another issue.",
            body,
        )

    def test_cross_linked_claim_requires_an_actual_attribute_link(self) -> None:
        reconciliation = _reconciliation()
        finding = reconciliation["findings"][0]
        assert isinstance(finding, dict)
        finding["kind"] = "quarantined-against-other-issue"

        with self.assertRaisesRegex(
            ValueError,
            "requires another quarantine issue link",
        ):
            build_action_proposals(
                _snapshot(),
                _prepared(),
                _judgments(),
                "ankj",
                quarantine_reconciliation=reconciliation,
            )

    def test_attribute_name_drift_licenses_the_current_issue_link(self) -> None:
        reconciliation = _reconciliation()
        finding = reconciliation["findings"][0]
        assert isinstance(finding, dict)
        finding.update(
            {
                "kind": "attribute-name-drift",
                "claimedTestName": "Demo.Tests.OldName",
                "currentSource": [
                    {
                        "testName": "Demo.Tests.Flaky",
                        "file": "Demo.Tests/Tests.cs",
                        "line": 31,
                        "quarantineIssueUrls": [
                            "https://github.com/owner/repo/issues/21"
                        ],
                    }
                ],
            }
        )

        result = build_action_proposals(
            _snapshot(),
            _prepared(),
            _judgments(),
            "ankj",
            quarantine_reconciliation=reconciliation,
        )

        claims = result["proposals"][0]["licensedClaims"]
        self.assertIn(
            {
                "kind": "quarantine-link-to-current-issue",
                "issueUrl": "https://github.com/owner/repo/issues/21",
            },
            claims,
        )
        self.assertNotIn(
            {
                "kind": "no-quarantine-link-to-current-issue",
                "issueUrl": "https://github.com/owner/repo/issues/21",
            },
            claims,
        )

    def test_ambiguous_move_claim_licenses_both_absence_and_candidates(
        self,
    ) -> None:
        reconciliation = _reconciliation()
        finding = reconciliation["findings"][0]
        assert isinstance(finding, dict)
        finding.update(
            {
                "kind": "ambiguous-absence",
                "currentSource": [
                    {
                        "testName": "Moved.Tests.Flaky",
                        "file": "Moved.Tests/Tests.cs",
                        "line": 47,
                        "quarantineIssueUrls": [
                            "https://github.com/owner/repo/issues/22"
                        ],
                    }
                ],
            }
        )

        result = build_action_proposals(
            _snapshot(),
            _prepared(),
            _judgments(),
            "ankj",
            quarantine_reconciliation=reconciliation,
        )

        claims = result["proposals"][0]["licensedClaims"]
        self.assertIn(
            {
                "kind": "source-method-not-found",
                "testName": "Demo.Tests.Flaky",
            },
            claims,
        )
        self.assertIn(
            {
                "kind": "source-method-match",
                "testName": "Moved.Tests.Flaky",
                "file": "Moved.Tests/Tests.cs",
                "line": 47,
                "quarantineIssueUrls": [
                    "https://github.com/owner/repo/issues/22"
                ],
            },
            claims,
        )

    def test_renderer_change_replaces_comment_with_legacy_finding_digest(
        self,
    ) -> None:
        reconciliation = _reconciliation()
        finding = reconciliation["findings"][0]
        assert isinstance(finding, dict)
        finding["kind"] = "unresolved-test-identity"
        finding["claimedTestName"] = None
        finding["currentSource"] = []
        legacy_digest = "sha256:" + hashlib.sha256(
            stable_json(finding).encode("utf-8")
        ).hexdigest()
        snapshot = _with_owned_comment(
            _snapshot(),
            (
                "[automated] stale reconciliation body\n"
                "<!-- ci-shepherd:role=status -->\n"
                "<!-- ci-shepherd:idempotency-key=issue:21:status -->\n"
                f"<!-- ci-shepherd:finding-digest={legacy_digest} -->"
            ),
        )

        result = build_action_proposals(
            snapshot,
            _prepared(),
            _judgments(),
            "ankj",
            quarantine_reconciliation=reconciliation,
        )

        self.assertEqual([], result["unchangedIssueNumbers"])
        proposal = result["proposals"][0]
        self.assertEqual("edit-comment", proposal["operation"])
        self.assertNotEqual(
            legacy_digest,
            proposal["sourceEvidenceFingerprint"]["findingDigest"],
        )

    def test_closure_review_finding_never_proposes_an_issue_close(self) -> None:
        reconciliation = _reconciliation()
        finding = reconciliation["findings"][0]
        assert isinstance(finding, dict)
        finding.update(
            {
                "kind": "removed-test-closure-review",
                "currentSource": [],
                "priorQuarantine": {
                    "pullRequestUrl": "https://github.com/owner/repo/pull/73",
                    "recordedAt": "2026-08-30T00:03:00Z",
                },
                "summary": "The quarantined method is gone.",
                "humanAction": "Confirm removal and close this issue.",
            }
        )

        result = build_action_proposals(
            _snapshot(),
            _prepared(),
            _judgments(),
            "ankj",
            quarantine_reconciliation=reconciliation,
        )

        self.assertEqual(
            ["create-comment"],
            [proposal["operation"] for proposal in result["proposals"]],
        )
        self.assertIn(
            "https://github.com/owner/repo/pull/73",
            result["proposals"][0]["body"],
        )
        self.assertIn(
            "No matching method exists in the inspected source.",
            result["proposals"][0]["body"],
        )


def _reconciliation() -> dict[str, object]:
    return reconcile_quarantine_source(
        {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "snapshotId": "snapshot:owner/repo:2026-08-21T16:00:00Z",
            "issues": [
                {
                    "issueNumber": 21,
                    "issueUrl": "https://github.com/owner/repo/issues/21",
                    "identity": {"tier2TestName": "Demo.Tests.Flaky"},
                    "evidenceBundle": [
                        {
                            "id": "issue:21",
                            "kind": "issue-event",
                            "payload": {"labels": ["quarantined-test"]},
                        }
                    ],
                }
            ],
        },
        {
            "schemaVersion": 1,
            "sourceRevision": "a" * 40,
            "sourceTreeDigest": "sha256:" + "b" * 64,
            "inspectorTreeDigest": "sha256:" + "c" * 64,
            "quarantines": [],
            "tests": [
                {
                    "testName": "Demo.Tests.Flaky",
                    "status": "resolved",
                    "matches": [
                        {
                            "file": "Demo.Tests/Tests.cs",
                            "line": 31,
                            "quarantineAttributes": [],
                            "activeIssueAttributes": [],
                            "fileSemanticDigest": "sha256:" + "d" * 64,
                            "fileQuarantines": [],
                        }
                    ],
                }
            ],
        },
    )


def _verified_reconciliation() -> dict[str, object]:
    reconciliation = _reconciliation()
    reconciliation["findings"] = []
    reconciliation["verifiedIssues"] = [
        {
            "issueNumber": 21,
            "issueUrl": "https://github.com/owner/repo/issues/21",
            "tests": [
                {
                    "testName": "Demo.Tests.Flaky",
                    "file": "Demo.Tests/Tests.cs",
                    "line": 31,
                }
            ],
        }
    ]
    return reconciliation


if __name__ == "__main__":
    unittest.main()
