from __future__ import annotations

import copy
import re
import unittest

from ci_shepherd import models
from ci_shepherd.models import (
    EXECUTOR_CAPABILITIES,
    EVIDENCE_REQUEST_DECISION_GATES,
    EVIDENCE_REQUEST_TYPES,
    LIFECYCLE_STATES,
    OCCURRENCE_CAUSES,
    PROPOSAL_INTENTS,
    TARGET_KINDS,
    ValidationError,
    stable_json,
    validate_evidence_requests,
    validate_report,
    validate_snapshot,
)


def evidence_record(
    evidence_id: str,
    kind: str,
    *,
    availability: str = "available",
    **payload: object,
) -> dict[str, object]:
    if "role" in payload and "sourceIssueNumber" not in payload and "referencedBy" not in payload:
        payload["sourceIssueNumber"] = 1
    if payload.get("role") == "canonical-search-complete":
        payload.setdefault(
            "supportingSearch",
            {
                "complete": True,
                "candidateIssueNumbers": [],
                "truncated": False,
            },
        )
    if payload.get("role") in {"no-newer-matching-failure", "no-recent-matching-failure"}:
        payload.setdefault("recentHistoryCollected", True)
        payload.setdefault("recentHistoryTruncated", False)
        payload.setdefault("recentHistory", [])
        payload.setdefault("historyCoversSourceRun", True)
    if payload.get("role") == "obsolete-surface":
        payload.setdefault("checkoutCommit", "a" * 40)
        payload.setdefault("exists", False)
        payload.setdefault("removalCommit", "b" * 40)
        payload.setdefault("replacementPath", None)
        payload.setdefault("replacementCommit", None)
        payload.setdefault("historyAmbiguous", False)
    if payload.get("role") == "post-fix-green":
        payload.setdefault("conclusion", "success")
    return {
        "kind": kind,
        "url": f"https://example.invalid/{evidence_id}",
        "collectedAt": "2026-08-17T21:24:23Z",
        "availability": availability,
        "payload": payload,
    }


def evidence_ref(
    evidence_id: str,
    kind: str,
    *,
    role: str | None = None,
    roles: list[str] | None = None,
    normalized_cause: str | None = None,
) -> dict[str, object]:
    reference: dict[str, object] = {"id": evidence_id, "kind": kind}
    if role is not None:
        reference["role"] = role
    if roles is not None:
        reference["roles"] = roles
    if normalized_cause is not None:
        reference["normalizedCause"] = normalized_cause
    return reference


def minimal_snapshot(*, issue_number: int = 1) -> dict[str, object]:
    evidence_id = f"issue:{issue_number}"
    return {
        "schemaVersion": 1,
        "repository": "owner/repo",
        "collectedAt": "2026-08-17T21:24:23Z",
        "openIssues": [issue_number],
        "evidence": {
            evidence_id: evidence_record(evidence_id, "issue-event"),
        },
        "collectionErrors": [],
    }


def minimal_report(
    *,
    issue_number: int = 1,
    issue_kind: str = "incident",
    state: str = "observing",
    action: str = "wait",
    evidence: list[dict[str, object]] | None = None,
    contradictory_evidence: list[dict[str, object]] | None = None,
    missing_evidence: list[dict[str, object]] | None = None,
    related_issues: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    evidence_id = f"issue:{issue_number}"
    return {
        "schemaVersion": 1,
        "repository": "owner/repo",
        "decisions": [
            {
                "issueNumber": issue_number,
                "issueUrl": f"https://github.com/owner/repo/issues/{issue_number}",
                "issueKind": issue_kind,
                "state": state,
                "proposedAction": action,
                "confidence": "high",
                "summary": "summary",
                "reasoning": "reasoning",
                "evidence": evidence if evidence is not None else [evidence_ref(evidence_id, "issue-event")],
                "contradictoryEvidence": contradictory_evidence if contradictory_evidence is not None else [],
                "missingEvidence": missing_evidence if missing_evidence is not None else [],
                "nextCondition": {
                    "type": "monitor",
                    "description": "Wait for the next workflow run.",
                },
                "suggestedOwners": [
                    {
                        "name": "team-a",
                        "reason": "Owns the service.",
                    }
                ],
                "relatedIssues": related_issues if related_issues is not None else [],
                "changedSincePreviousRun": False,
            }
        ],
    }


def minimal_assessment(
    *,
    issue_number: int = 1,
    candidate_state: str = "resolved",
    candidate_action: str = "recommend-close",
    allowed_decisions: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    decisions = allowed_decisions or [
        {"state": candidate_state, "action": candidate_action},
        {"state": "insufficient-evidence", "action": "investigate"},
        {"state": "observing", "action": "wait"},
    ]
    return {
        "schemaVersion": 1,
        "repository": "owner/repo",
        "sourceCollectedAt": "2026-08-17T21:24:23Z",
        "maxBundleRecords": 25,
        "issues": [
            {
                "issueNumber": issue_number,
                "candidateState": candidate_state,
                "candidateAction": candidate_action,
                "allowedActions": [item["action"] for item in decisions],
                "allowedDecisions": decisions,
                "automationEligible": False,
                "approvalRequired": candidate_action == "recommend-close",
                "blockers": [],
                "missingPrerequisites": [],
                "evidenceBundle": [
                    {
                        "id": f"issue:{issue_number}",
                        "kind": "issue-event",
                        "availability": "available",
                        "payload": {},
                    }
                ],
            }
        ],
    }


_INVALID_POSITIVE_ROLE_CONDITIONS = (
    "omission",
    "unavailable",
    "partial",
    "previous-report",
    "contradictory-only",
    "missing-only",
)

_HIGH_RISK_EVIDENCE: dict[str, tuple[str, str, dict[str, object]]] = {
    "canonical-issue": ("issue:2", "issue-event", {}),
    "canonical-search-complete": ("issue:1", "issue-event", {}),
    "current-failing-run": (
        "run:46",
        "workflow-run",
        {"normalizedCause": "timeout-on-startup"},
    ),
    "deterministic-marker": ("issue:1:comment:18", "issue-comment", {}),
    "known-flaky-signature": ("issue:1:comment:19", "issue-comment", {}),
    "merged-fix": ("issue:1", "issue-event", {}),
    "no-newer-matching-failure": ("run:43", "workflow-run", {}),
    "no-recent-matching-failure": ("run:44", "workflow-run", {}),
    "normalized-cause": (
        "issue:1:comment:20",
        "issue-comment",
        {"normalizedCause": "timeout-on-startup"},
    ),
    "normalized-facts": ("issue:1:comment:21", "issue-comment", {}),
    "obsolete-surface": ("source:src/RemovedWorkflow.yml", "source-path", {}),
    "post-fix-green": ("run:42:attempt:1:job:7", "workflow-job", {}),
    "prior-resolved-episode": (
        "issue:2",
        "issue-event",
        {"normalizedCause": "timeout-on-startup"},
    ),
    "recurrence": ("run:47", "workflow-run", {}),
    "recovery": ("run:42", "workflow-run", {}),
}

_HIGH_RISK_ACTIONS: dict[str, dict[str, object]] = {
    "close": {
        "state": "resolved",
        "defaultRoles": ("merged-fix", "post-fix-green", "no-newer-matching-failure"),
        "relatedIssues": (),
    },
    "close-resolved": {
        "state": "resolved",
        "defaultRoles": ("merged-fix", "post-fix-green", "no-newer-matching-failure"),
        "relatedIssues": (),
    },
    "close-stale": {
        "state": "stale",
        "defaultRoles": ("obsolete-surface", "no-recent-matching-failure"),
        "relatedIssues": (),
    },
    "close-as-tracked": {
        "state": "tracked-elsewhere",
        "defaultRoles": ("canonical-issue",),
        "relatedIssues": (
            {
                "type": "canonical-tracker",
                "sourceIssueNumber": 1,
                "targetIssueNumber": 2,
            },
        ),
    },
    "open-dedicated-issue": {
        "state": "actionable",
        "defaultRoles": ("current-failing-run", "recurrence", "canonical-search-complete"),
        "relatedIssues": (),
    },
    "merge-duplicate": {
        "state": "duplicate",
        "defaultRoles": ("canonical-issue", "deterministic-marker"),
        "relatedIssues": (
            {
                "type": "exact-duplicate",
                "sourceIssueNumber": 1,
                "targetIssueNumber": 2,
            },
        ),
    },
    "open-regression": {
        "state": "regression",
        "defaultRoles": ("current-failing-run", "prior-resolved-episode", "normalized-cause"),
        "relatedIssues": (
            {
                "type": "regression-of",
                "sourceIssueNumber": 1,
                "targetIssueNumber": 2,
            },
        ),
    },
}

_HIGH_RISK_ROLE_MATRIX = (
    ("close", "merged-fix", ("merged-fix", "post-fix-green", "no-newer-matching-failure"), "merged-fix or recovery"),
    ("close", "recovery", ("recovery", "post-fix-green", "no-newer-matching-failure"), "merged-fix or recovery"),
    ("close", "post-fix-green", ("merged-fix", "post-fix-green", "no-newer-matching-failure"), "post-fix-green"),
    (
        "close",
        "no-newer-matching-failure",
        ("merged-fix", "post-fix-green", "no-newer-matching-failure"),
        "no-newer-matching-failure",
    ),
    (
        "close-resolved",
        "merged-fix",
        ("merged-fix", "post-fix-green", "no-newer-matching-failure"),
        "merged-fix or recovery",
    ),
    (
        "close-resolved",
        "recovery",
        ("recovery", "post-fix-green", "no-newer-matching-failure"),
        "merged-fix or recovery",
    ),
    (
        "close-resolved",
        "post-fix-green",
        ("merged-fix", "post-fix-green", "no-newer-matching-failure"),
        "post-fix-green",
    ),
    (
        "close-resolved",
        "no-newer-matching-failure",
        ("merged-fix", "post-fix-green", "no-newer-matching-failure"),
        "no-newer-matching-failure",
    ),
    (
        "close-stale",
        "obsolete-surface",
        ("obsolete-surface", "no-recent-matching-failure"),
        "obsolete-surface",
    ),
    (
        "close-stale",
        "no-recent-matching-failure",
        ("obsolete-surface", "no-recent-matching-failure"),
        "no-recent-matching-failure",
    ),
    ("close-as-tracked", "canonical-issue", ("canonical-issue",), "canonical-issue"),
    (
        "open-dedicated-issue",
        "current-failing-run",
        ("current-failing-run", "recurrence", "canonical-search-complete"),
        "current-failing-run",
    ),
    (
        "open-dedicated-issue",
        "recurrence",
        ("current-failing-run", "recurrence", "canonical-search-complete"),
        "recurrence or known-flaky-signature",
    ),
    (
        "open-dedicated-issue",
        "known-flaky-signature",
        ("current-failing-run", "known-flaky-signature", "canonical-search-complete"),
        "recurrence or known-flaky-signature",
    ),
    (
        "open-dedicated-issue",
        "canonical-search-complete",
        ("current-failing-run", "recurrence", "canonical-search-complete"),
        "canonical-search-complete",
    ),
    ("merge-duplicate", "canonical-issue", ("canonical-issue", "deterministic-marker"), "canonical-issue"),
    (
        "merge-duplicate",
        "deterministic-marker",
        ("canonical-issue", "deterministic-marker"),
        "deterministic-marker or normalized-facts",
    ),
    (
        "merge-duplicate",
        "normalized-facts",
        ("canonical-issue", "normalized-facts"),
        "deterministic-marker or normalized-facts",
    ),
    (
        "open-regression",
        "current-failing-run",
        ("current-failing-run", "prior-resolved-episode", "normalized-cause"),
        "current-failing-run, prior-resolved-episode, and normalized-cause",
    ),
    (
        "open-regression",
        "prior-resolved-episode",
        ("current-failing-run", "prior-resolved-episode", "normalized-cause"),
        "current-failing-run, prior-resolved-episode, and normalized-cause",
    ),
    (
        "open-regression",
        "normalized-cause",
        ("current-failing-run", "prior-resolved-episode", "normalized-cause"),
        "current-failing-run, prior-resolved-episode, and normalized-cause",
    ),
)

_HIGH_RISK_ALTERNATIVE_SUCCESSES = (
    ("close", "merged-fix", ("merged-fix", "post-fix-green", "no-newer-matching-failure")),
    ("close", "recovery", ("recovery", "post-fix-green", "no-newer-matching-failure")),
    ("close-resolved", "merged-fix", ("merged-fix", "post-fix-green", "no-newer-matching-failure")),
    ("close-resolved", "recovery", ("recovery", "post-fix-green", "no-newer-matching-failure")),
    (
        "open-dedicated-issue",
        "recurrence",
        ("current-failing-run", "recurrence", "canonical-search-complete"),
    ),
    (
        "open-dedicated-issue",
        "known-flaky-signature",
        ("current-failing-run", "known-flaky-signature", "canonical-search-complete"),
    ),
    ("merge-duplicate", "deterministic-marker", ("canonical-issue", "deterministic-marker")),
    ("merge-duplicate", "normalized-facts", ("canonical-issue", "normalized-facts")),
)

_HIGH_RISK_ALTERNATIVE_FAILURES = (
    (
        "close",
        "merged-fix/recovery",
        ("post-fix-green", "no-newer-matching-failure"),
        "merged-fix or recovery",
    ),
    (
        "close-resolved",
        "merged-fix/recovery",
        ("post-fix-green", "no-newer-matching-failure"),
        "merged-fix or recovery",
    ),
    (
        "open-dedicated-issue",
        "recurrence/known-flaky-signature",
        ("current-failing-run", "canonical-search-complete"),
        "recurrence or known-flaky-signature",
    ),
    (
        "merge-duplicate",
        "deterministic-marker/normalized-facts",
        ("canonical-issue",),
        "deterministic-marker or normalized-facts",
    ),
)

_HIGH_RISK_RELATIONSHIP_FAILURES = (
    ("close-as-tracked", "canonical-tracker", (), "canonical-tracker"),
    (
        "close-as-tracked",
        "canonical-tracker",
        (
            {
                "type": "probable-duplicate",
                "sourceIssueNumber": 1,
                "targetIssueNumber": 2,
            },
        ),
        "canonical-tracker",
    ),
    (
        "close-as-tracked",
        "canonical-tracker",
        (
            {
                "type": "canonical-tracker",
                "sourceIssueNumber": 1,
                "targetIssueNumber": 3,
            },
        ),
        "targetIssueNumber",
    ),
    (
        "close-as-tracked",
        "canonical-tracker",
        (
            {
                "type": "canonical-tracker",
                "sourceIssueNumber": 1,
                "targetIssueNumber": 2,
                "targetRepository": "other/repo",
            },
        ),
        "targetRepository",
    ),
    ("merge-duplicate", "exact-duplicate", (), "exact-duplicate"),
    (
        "merge-duplicate",
        "exact-duplicate",
        (
            {
                "type": "probable-duplicate",
                "sourceIssueNumber": 1,
                "targetIssueNumber": 2,
            },
        ),
        "exact-duplicate",
    ),
    (
        "merge-duplicate",
        "exact-duplicate",
        (
            {
                "type": "exact-duplicate",
                "sourceIssueNumber": 1,
                "targetIssueNumber": 3,
            },
        ),
        "targetIssueNumber",
    ),
    (
        "merge-duplicate",
        "exact-duplicate",
        (
            {
                "type": "exact-duplicate",
                "sourceIssueNumber": 1,
                "targetIssueNumber": 2,
                "targetRepository": "other/repo",
            },
        ),
        "targetRepository",
    ),
    ("open-regression", "regression-of", (), "regression-of"),
    (
        "open-regression",
        "regression-of",
        (
            {
                "type": "related",
                "sourceIssueNumber": 1,
                "targetIssueNumber": 2,
            },
        ),
        "regression-of",
    ),
    (
        "open-regression",
        "regression-of",
        (
            {
                "type": "regression-of",
                "sourceIssueNumber": 1,
                "targetIssueNumber": 3,
            },
        ),
        "targetIssueNumber",
    ),
    (
        "open-regression",
        "regression-of",
        (
            {
                "type": "regression-of",
                "sourceIssueNumber": 1,
                "targetIssueNumber": 2,
                "targetRepository": "other/repo",
            },
        ),
        "targetRepository",
    ),
)


def high_risk_case(
    action: str,
    *,
    issue_kind: str = "incident",
    roles: tuple[str, ...] | None = None,
    related_issues: tuple[dict[str, object], ...] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    action_config = _HIGH_RISK_ACTIONS[action]
    selected_roles = roles if roles is not None else action_config["defaultRoles"]
    snapshot = minimal_snapshot()
    snapshot["evidence"] = {}
    evidence_refs = []
    for role in selected_roles:
        evidence_id, kind, payload = _HIGH_RISK_EVIDENCE[role]
        snapshot["evidence"][evidence_id] = evidence_record(evidence_id, kind, role=role, **payload)
        evidence_refs.append(evidence_ref(evidence_id, kind))
    selected_relationships = related_issues if related_issues is not None else action_config["relatedIssues"]
    report = minimal_report(
        issue_kind=issue_kind,
        state=action_config["state"],
        action=action,
        evidence=evidence_refs,
        related_issues=[dict(relationship) for relationship in selected_relationships],
    )
    return snapshot, report


def evidence_id_for_role(snapshot: dict[str, object], role: str) -> str:
    for evidence_id, record in snapshot["evidence"].items():
        if record["payload"].get("role") == role:
            return evidence_id
    raise AssertionError(f"Missing test evidence role {role}.")


def move_supporting_role(
    snapshot: dict[str, object],
    report: dict[str, object],
    role: str,
    condition: str,
) -> None:
    evidence_id = evidence_id_for_role(snapshot, role)
    record = snapshot["evidence"][evidence_id]
    evidence_reference = evidence_ref(evidence_id, record["kind"])
    decision = report["decisions"][0]
    if condition in {"omission", "contradictory-only", "missing-only"}:
        decision["evidence"] = [
            item for item in decision["evidence"]
            if item["id"] != evidence_id
        ]
    if condition == "unavailable":
        record["availability"] = "expired-or-unavailable"
    elif condition == "partial":
        record["availability"] = "partial"
    elif condition == "previous-report":
        record["payload"]["source"] = "previous-report"
    elif condition == "contradictory-only":
        decision["contradictoryEvidence"] = [evidence_reference]
    elif condition == "missing-only":
        decision["missingEvidence"] = [evidence_reference]


def scoped_by_reference(issue_number: int) -> dict[str, object]:
    return {
        "referencedBy": [
            {
                "sourceIssueNumber": issue_number,
                "sourceEvidenceId": f"issue:{issue_number}",
                "sourceUrl": f"https://github.com/owner/repo/issues/{issue_number}",
                "extractionMethod": "test",
            },
            {"sourceIssueNumber": "not-an-integer"},
            {"sourceIssueNumber": 0},
            "malformed-reference",
        ]
    }


def canonical_search_case(
    evidence_id: str,
    kind: str,
    **payload: object,
) -> tuple[dict[str, object], dict[str, object]]:
    snapshot = minimal_snapshot()
    snapshot["evidence"] = {
        "run:46": evidence_record("run:46", "workflow-run", role="current-failing-run"),
        "run:47": evidence_record("run:47", "workflow-run", role="recurrence"),
        evidence_id: evidence_record(
            evidence_id,
            kind,
            supportingSearch={
                "complete": True,
                "candidateIssueNumbers": [],
                "truncated": False,
            },
            **payload,
        ),
    }
    report = minimal_report(
        state="actionable",
        action="open-dedicated-issue",
        evidence=[
            evidence_ref("run:46", "workflow-run"),
            evidence_ref("run:47", "workflow-run"),
            evidence_ref(evidence_id, kind, role="canonical-search-complete"),
        ],
    )
    return snapshot, report


class ModelsTests(unittest.TestCase):
    def test_assessment_allows_advisory_recommend_close(self) -> None:
        snapshot = minimal_snapshot()
        report = minimal_report(state="resolved", action="recommend-close")

        validate_report(snapshot, report, assessment=minimal_assessment())

    def test_assessment_allows_agent_to_downgrade_candidate(self) -> None:
        snapshot = minimal_snapshot()
        report = minimal_report(state="insufficient-evidence", action="investigate")

        validate_report(snapshot, report, assessment=minimal_assessment())

    def test_assessment_rejects_agent_upgrade_to_executable_close(self) -> None:
        snapshot = minimal_snapshot()
        report = minimal_report(state="resolved", action="close-resolved")

        with self.assertRaisesRegex(
            ValidationError,
            "not allowed by deterministic candidate",
        ):
            validate_report(snapshot, report, assessment=minimal_assessment())

    def test_assessment_rejects_evidence_outside_bounded_bundle(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"]["run:10"] = evidence_record(
            "run:10",
            "workflow-run",
            sourceIssueNumber=1,
        )
        report = minimal_report(
            state="observing",
            action="wait",
            evidence=[evidence_ref("run:10", "workflow-run")],
        )

        with self.assertRaisesRegex(
            ValidationError,
            "outside its bounded assessment bundle",
        ):
            validate_report(snapshot, report, assessment=minimal_assessment())

    def test_canonical_search_complete_rejects_supporting_issue_record(self) -> None:
        snapshot, report = canonical_search_case(
            "issue:2",
            "issue-event",
            number=2,
            **scoped_by_reference(1),
        )

        with self.assertRaisesRegex(ValidationError, "canonical-search-complete"):
            validate_report(snapshot, report)

    def test_canonical_search_complete_rejects_issue_comment(self) -> None:
        snapshot, report = canonical_search_case(
            "issue:1:comment:17",
            "issue-comment",
            sourceIssueNumber=1,
        )

        with self.assertRaisesRegex(ValidationError, "canonical-search-complete"):
            validate_report(snapshot, report)

    def test_canonical_search_complete_rejects_issue_timeline_event(self) -> None:
        snapshot, report = canonical_search_case(
            "issue:1:event:17",
            "issue-event",
            sourceIssueNumber=1,
        )

        with self.assertRaisesRegex(ValidationError, "canonical-search-complete"):
            validate_report(snapshot, report)

    def test_canonical_search_complete_rejects_workflow_run(self) -> None:
        snapshot, report = canonical_search_case(
            "run:48",
            "workflow-run",
            sourceIssueNumber=1,
        )

        with self.assertRaisesRegex(ValidationError, "canonical-search-complete"):
            validate_report(snapshot, report)

    def test_canonical_search_complete_rejects_source_path(self) -> None:
        snapshot, report = canonical_search_case(
            "source:src%2FProgram.cs",
            "source-path",
            sourceIssueNumber=1,
        )

        with self.assertRaisesRegex(ValidationError, "canonical-search-complete"):
            validate_report(snapshot, report)

    def test_canonical_search_complete_rejects_codeowners(self) -> None:
        snapshot, report = canonical_search_case(
            "codeowners:src%2FProgram.cs:7",
            "codeowners",
            sourceIssueNumber=1,
        )

        with self.assertRaisesRegex(ValidationError, "canonical-search-complete"):
            validate_report(snapshot, report)

    def test_canonical_search_complete_rejects_foreign_repository_issue(self) -> None:
        snapshot, report = canonical_search_case(
            "issue:other/repo:1",
            "issue-event",
            number=1,
            sourceIssueNumber=1,
        )

        with self.assertRaisesRegex(ValidationError, "canonical-search-complete"):
            validate_report(snapshot, report)

    def test_canonical_search_complete_rejects_payload_number_mismatch(self) -> None:
        snapshot, report = canonical_search_case(
            "issue:1",
            "issue-event",
            number=2,
        )

        with self.assertRaisesRegex(ValidationError, "canonical-search-complete"):
            validate_report(snapshot, report)

    def test_canonical_search_complete_rejects_payload_repository_mismatch(self) -> None:
        snapshot, report = canonical_search_case(
            "issue:1",
            "issue-event",
            repository="other/repo",
        )

        with self.assertRaisesRegex(ValidationError, "canonical-search-complete"):
            validate_report(snapshot, report)

    def test_canonical_search_complete_rejects_payload_target_repository_mismatch(self) -> None:
        snapshot, report = canonical_search_case(
            "issue:1",
            "issue-event",
            targetRepository="other/repo",
        )

        with self.assertRaisesRegex(ValidationError, "canonical-search-complete"):
            validate_report(snapshot, report)

    def test_canonical_search_complete_rejects_payload_url_mismatch(self) -> None:
        snapshot, report = canonical_search_case(
            "issue:1",
            "issue-event",
            url="https://github.com/owner/repo/issues/2",
        )

        with self.assertRaisesRegex(ValidationError, "canonical-search-complete"):
            validate_report(snapshot, report)

    def test_canonical_search_complete_rejects_malformed_optional_payload_identity(self) -> None:
        malformed_values = (
            ("number", "1"),
            ("repository", "not-a-repository"),
            ("targetRepository", 42),
            ("url", 42),
        )
        for field_name, value in malformed_values:
            with self.subTest(field_name=field_name, value=value):
                snapshot, report = canonical_search_case(
                    "issue:1",
                    "issue-event",
                    **{field_name: value},
                )

                with self.assertRaisesRegex(ValidationError, "canonical-search-complete"):
                    validate_report(snapshot, report)

    def test_canonical_search_complete_accepts_uppercase_equivalent_payload_identity(self) -> None:
        snapshot, report = canonical_search_case(
            "issue:OWNER/REPO:1",
            "issue-event",
            number=1,
            repository="OWNER/REPO",
            targetRepository="Owner/Repo",
            url="https://github.com/OWNER/REPO/issues/1",
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_canonical_search_complete_accepts_collector_shaped_root_issue_record(self) -> None:
        for evidence_id in ("issue:1", "issue:OWNER/REPO:1"):
            with self.subTest(evidence_id=evidence_id):
                snapshot, report = canonical_search_case(
                    evidence_id,
                    "issue-event",
                    number=1,
                    state="open",
                    title="Issue 1",
                    body="",
                    url="https://github.com/owner/repo/issues/1",
                    createdAt="2026-08-01T00:00:00Z",
                    updatedAt="2026-08-02T00:00:00Z",
                    closedAt=None,
                    labels=["ci-failure-cause"],
                    comments=[],
                    episodes=[{"openedAt": "2026-08-01T00:00:00Z", "closedAt": None}],
                    markers=[],
                    facts=[],
                    references=[],
                )

                self.assertIsNone(validate_report(snapshot, report))

    def test_canonical_search_complete_role_requires_factual_completed_search(self) -> None:
        incomplete_searches = (
            None,
            {
                "complete": False,
                "candidateIssueNumbers": [],
                "truncated": False,
            },
            {
                "candidateIssueNumbers": [],
                "truncated": False,
            },
        )
        for supporting_search in incomplete_searches:
            with self.subTest(supporting_search=supporting_search):
                snapshot = minimal_snapshot()
                search_payload: dict[str, object] = {"sourceIssueNumber": 1}
                if supporting_search is not None:
                    search_payload["supportingSearch"] = supporting_search
                snapshot["evidence"] = {
                    "run:46": evidence_record(
                        "run:46",
                        "workflow-run",
                        role="current-failing-run",
                    ),
                    "run:47": evidence_record("run:47", "workflow-run", role="recurrence"),
                    "issue:1": evidence_record(
                        "issue:1",
                        "issue-event",
                        **search_payload,
                    ),
                }
                report = minimal_report(
                    state="actionable",
                    action="open-dedicated-issue",
                    evidence=[
                        evidence_ref("run:46", "workflow-run"),
                        evidence_ref("run:47", "workflow-run"),
                        evidence_ref(
                            "issue:1",
                            "issue-event",
                            role="canonical-search-complete",
                        ),
                    ],
                )

                with self.assertRaisesRegex(ValidationError, "canonical-search-complete"):
                    validate_report(snapshot, report)

    def test_completed_zero_match_search_is_eligible_for_canonical_search_complete_role(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:46": evidence_record("run:46", "workflow-run", role="current-failing-run"),
            "run:47": evidence_record("run:47", "workflow-run", role="recurrence"),
            "issue:1": evidence_record(
                "issue:1",
                "issue-event",
                supportingSearch={
                    "complete": True,
                    "candidateIssueNumbers": [],
                    "truncated": False,
                },
            ),
        }
        report = minimal_report(
            state="actionable",
            action="open-dedicated-issue",
            evidence=[
                evidence_ref("run:46", "workflow-run"),
                evidence_ref("run:47", "workflow-run"),
                evidence_ref(
                    "issue:1",
                    "issue-event",
                    role="canonical-search-complete",
                ),
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_history_search_roles_require_strong_factual_collection_proof(self) -> None:
        cases = (
            ("close-resolved", "no-newer-matching-failure"),
            ("close-stale", "no-recent-matching-failure"),
        )
        for action, role in cases:
            invalid_payload_changes = (
                ("missing-collected", "recentHistoryCollected", None),
                ("collection-failed", "recentHistoryCollected", False),
                ("missing-list", "recentHistory", None),
                ("malformed-list", "recentHistory", {}),
                ("missing-truncated", "recentHistoryTruncated", None),
                ("malformed-truncated", "recentHistoryTruncated", "false"),
                ("missing-coverage", "historyCoversSourceRun", None),
                ("source-not-covered", "historyCoversSourceRun", False),
            )
            for case_name, field_name, value in invalid_payload_changes:
                with self.subTest(action=action, case=case_name):
                    snapshot, report = high_risk_case(action)
                    evidence_id = evidence_id_for_role(snapshot, role)
                    payload = snapshot["evidence"][evidence_id]["payload"]
                    if value is None:
                        payload.pop(field_name, None)
                    else:
                        payload[field_name] = value

                    with self.assertRaisesRegex(ValidationError, role):
                        validate_report(snapshot, report)

    def test_high_risk_completeness_includes_all_issue_scoped_evidence_kinds(self) -> None:
        scoped_records = (
            (
                "issue:1:comment:901",
                "issue-comment",
                {"sourceIssueNumber": 1},
            ),
            (
                "issue:1:event:902",
                "issue-event",
                {"sourceIssueNumber": 1},
            ),
            (
                "source:src%2Fapp.py",
                "source-path",
                scoped_by_reference(1),
            ),
            (
                "codeowners:src%2Fapp.py:1",
                "codeowners",
                scoped_by_reference(1),
            ),
        )
        for evidence_id, kind, payload in scoped_records:
            with self.subTest(evidence_id=evidence_id):
                snapshot, report = high_risk_case("open-dedicated-issue")
                snapshot["evidence"][evidence_id] = evidence_record(
                    evidence_id,
                    kind,
                    **payload,
                )

                with self.assertRaisesRegex(ValidationError, re.escape(evidence_id)):
                    validate_report(snapshot, report)

    def test_stable_json_sorts_keys_indents_and_ends_with_newline(self) -> None:
        self.assertEqual(
            '{\n  "a": 2,\n  "b": 1\n}\n',
            stable_json({"b": 1, "a": 2}),
        )

    def test_typed_vocabularies_are_exact(self) -> None:
        self.assertEqual(
            frozenset(
                {
                    "test-flake",
                    "test-contention",
                    "infra-transient",
                    "product-regression-suspect",
                    "toolchain-build-break",
                    "repo-config-break",
                    "unknown",
                }
            ),
            OCCURRENCE_CAUSES,
        )
        self.assertEqual(
            frozenset(
                {
                    "new",
                    "observing",
                    "recurrent",
                    "dormant-unverified",
                    "dormant-verified",
                    "fix-merged-unverified",
                    "resolved-verified",
                    "needs-policy",
                    "human-owned",
                    "duplicate-of",
                    "data-quality-blocked",
                }
            ),
            LIFECYCLE_STATES,
        )
        self.assertEqual(
            frozenset(
                {
                    "no-op",
                    "keep-watching",
                    "investigate-now",
                    "assign-copilot-investigation",
                    "request-closure-review",
                    "request-quarantine-review",
                    "propose-retry-pattern",
                    "request-rerun",
                    "escalate-systemic",
                    "escalate-blocking",
                    "flag-data-quality",
                }
            ),
            PROPOSAL_INTENTS,
        )
        self.assertEqual(
            frozenset({"issue", "test", "failureFingerprint", "workflowRun", "investigation"}),
            TARGET_KINDS,
        )
        self.assertEqual(
            frozenset(
                {
                    "create-comment",
                    "edit-comment",
                    "close-issue",
                }
            ),
            EXECUTOR_CAPABILITIES,
        )

    def test_minimal_snapshot_passes(self) -> None:
        self.assertIsNone(validate_snapshot(minimal_snapshot()))

    def test_collection_error_scope_is_validated(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["collectionErrors"] = [
            {
                "stage": "comments",
                "endpoint": "/repos/owner/repo/issues/1/comments",
                "message": "request failed",
                "effect": None,
                "scope": {"kind": "issue", "issueNumbers": [1]},
            }
        ]
        self.assertIsNone(validate_snapshot(snapshot))

        snapshot["collectionErrors"][0]["scope"]["issueNumbers"] = []
        with self.assertRaisesRegex(
            ValueError,
            "collectionErrors\\[0\\]\\.scope\\.issueNumbers",
        ):
            validate_snapshot(snapshot)

    def test_valid_expansion_manifests_pass_snapshot_validation(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["expansions"] = [
            {
                "round": 1,
                "requests": [
                    {
                        "type": "canonical-search",
                        "sourceIssueNumber": 1,
                        "evidenceId": "issue:1",
                        "decisionGate": "canonical-search-complete",
                        "reason": "Search for a canonical issue.",
                        "factField": "testName",
                        "factValue": "Namespace.Tests.Fails",
                        "factNormalized": "namespace.tests.fails",
                    },
                    {
                        "type": "issue-reference",
                        "sourceIssueNumber": 1,
                        "evidenceId": "issue:2",
                        "decisionGate": "merged-fix",
                        "reason": "Resolve the referenced fix.",
                    },
                    {
                        "type": "source-check",
                        "sourceIssueNumber": 1,
                        "evidenceId": "pr:3",
                        "decisionGate": "obsolete-surface",
                        "reason": "Check the affected source surface.",
                        "path": "src/Surface.cs",
                    },
                    {
                        "type": "workflow-run",
                        "sourceIssueNumber": 1,
                        "evidenceId": "run:42",
                        "decisionGate": "no-newer-matching-failure",
                        "reason": "Collect bounded run history.",
                    },
                ],
                "status": "partial",
                "errors": [
                    {
                        "requestType": "workflow-run",
                        "sourceIssueNumber": 1,
                        "evidenceId": "run:42",
                        "stage": "workflow-history",
                        "endpoint": "/repos/owner/repo/actions/workflows/9/runs",
                        "message": "History unavailable.",
                        "effect": "Recent history remains incomplete.",
                    }
                ],
            },
            {
                "round": 2,
                "requests": [],
                "status": "complete",
                "errors": [],
            },
        ]

        self.assertIsNone(validate_snapshot(snapshot))

    def test_snapshot_rejects_malformed_expansion_manifests(self) -> None:
        valid_request = {
            "type": "workflow-run",
            "sourceIssueNumber": 1,
            "evidenceId": "run:42",
            "decisionGate": "no-newer-matching-failure",
            "reason": "Collect bounded run history.",
        }
        valid_error = {
            "requestType": "workflow-run",
            "sourceIssueNumber": 1,
            "evidenceId": "run:42",
            "stage": "workflow-history",
            "endpoint": "/repos/owner/repo/actions/workflows/9/runs",
            "message": "History unavailable.",
            "effect": "Recent history remains incomplete.",
        }
        valid_manifest = {
            "round": 1,
            "requests": [valid_request],
            "status": "partial",
            "errors": [valid_error],
        }
        mutations: list[tuple[str, object, str]] = [
            ("not-list", {}, "expansions.*list"),
            (
                "too-many",
                [
                    valid_manifest,
                    {**valid_manifest, "round": 2},
                    {**valid_manifest, "round": 3},
                ],
                "[Aa]t most two",
            ),
            (
                "nonsequential",
                [{**valid_manifest, "round": 2}],
                "sequential",
            ),
            (
                "unknown-manifest-field",
                [{**valid_manifest, "method": "POST"}],
                "unknown|forbidden",
            ),
            (
                "nonstring-status",
                [{**valid_manifest, "status": 42}],
                "status",
            ),
            (
                "unknown-status",
                [{**valid_manifest, "status": "finished"}],
                "status",
            ),
            (
                "string-requests",
                [{**valid_manifest, "requests": "workflow-run"}],
                "requests.*list",
            ),
            (
                "string-request",
                [{**valid_manifest, "requests": ["workflow-run"]}],
                "request.*object",
            ),
            (
                "write-like-request",
                [{**valid_manifest, "requests": [{**valid_request, "method": "POST"}]}],
                "unknown|forbidden",
            ),
            (
                "unsafe-source-path",
                [
                    {
                        **valid_manifest,
                        "requests": [
                            {
                                "type": "source-check",
                                "sourceIssueNumber": 1,
                                "evidenceId": "pr:3",
                                "decisionGate": "obsolete-surface",
                                "reason": "Check source.",
                                "path": "../outside",
                            }
                        ],
                        "errors": [],
                    }
                ],
                "safe repository-relative",
            ),
            (
                "string-errors",
                [{**valid_manifest, "errors": "history failed"}],
                "errors.*list",
            ),
            (
                "write-like-error",
                [{**valid_manifest, "errors": [{**valid_error, "method": "POST"}]}],
                "unknown|forbidden",
            ),
            (
                "unmatched-error",
                [
                    {
                        **valid_manifest,
                        "errors": [{**valid_error, "evidenceId": "run:43"}],
                    }
                ],
                "match.*request",
            ),
            (
                "complete-with-errors",
                [{**valid_manifest, "status": "complete"}],
                "complete.*errors",
            ),
        ]

        for name, expansions, message in mutations:
            with self.subTest(name=name):
                snapshot = minimal_snapshot()
                snapshot["expansions"] = copy.deepcopy(expansions)
                with self.assertRaisesRegex(ValidationError, message):
                    validate_snapshot(snapshot)

    def test_supporting_search_candidate_disposition_cannot_also_be_selected(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"]["issue:1"]["payload"]["supportingSearch"] = {
            "complete": False,
            "candidateIssueNumbers": [403],
            "truncated": True,
            "candidateDispositions": [
                {
                    "issueNumber": 403,
                    "disposition": "excluded-depth",
                    "provenance": [
                        {
                            "sourceEvidenceId": "issue:402",
                            "sourceUrl": "https://github.com/owner/repo/issues/402",
                            "extractionMethod": "local-issue",
                        }
                    ],
                }
            ],
        }

        with self.assertRaisesRegex(ValidationError, "both selected and excluded"):
            validate_snapshot(snapshot)

    def test_minimal_report_passes(self) -> None:
        snapshot = minimal_snapshot()
        report = minimal_report()

        self.assertIsNone(validate_report(snapshot, report))

    def test_evidence_roles_are_finite_and_cover_every_high_risk_validator_role(self) -> None:
        self.assertEqual(
            {
                "canonical-issue",
                "canonical-search-complete",
                "current-failing-run",
                "deterministic-marker",
                "known-flaky-signature",
                "merged-fix",
                "newer-failure",
                "no-newer-matching-failure",
                "no-recent-matching-failure",
                "normalized-cause",
                "normalized-facts",
                "obsolete-surface",
                "post-fix-green",
                "prior-resolved-episode",
                "recurrence",
                "recovery",
            },
            models.EVIDENCE_ROLES,
        )
        self.assertEqual(
            models.EVIDENCE_ROLES,
            frozenset().union(*models.HIGH_RISK_ACTION_RELEVANT_ROLES.values()),
        )

    def test_evidence_availabilities_match_collector_output(self) -> None:
        self.assertEqual(
            {
                "available",
                "expired-or-unavailable",
                "not-enriched",
                "partial",
            },
            models.EVIDENCE_AVAILABILITIES,
        )

    def test_run_budget_excluded_partial_stub_cannot_satisfy_high_risk_role(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:11": evidence_record(
                "run:11",
                "workflow-run",
                availability="partial",
                runId=11,
                targetRepository="owner/repo",
                runBudgetExcluded=True,
                **scoped_by_reference(1),
            ),
            "run:12": evidence_record(
                "run:12",
                "workflow-run",
                role="recurrence",
            ),
            "issue:1": evidence_record(
                "issue:1",
                "issue-event",
                role="canonical-search-complete",
            ),
        }
        report = minimal_report(
            state="actionable",
            action="open-dedicated-issue",
            evidence=[
                evidence_ref("run:11", "workflow-run", role="current-failing-run"),
                evidence_ref("run:12", "workflow-run"),
                evidence_ref("issue:1", "issue-event"),
            ],
        )

        with self.assertRaisesRegex(ValidationError, "current-failing-run"):
            validate_report(snapshot, report)

    def test_valid_high_risk_decision_uses_roles_from_report_references(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:1": evidence_record("issue:1", "issue-event", number=1),
            "pr:77": evidence_record(
                "pr:77",
                "pull-request",
                merged=True,
                **scoped_by_reference(1),
            ),
            "run:42": evidence_record(
                "run:42",
                "workflow-run",
                conclusion="success",
                **scoped_by_reference(1),
            ),
            "run:43": evidence_record(
                "run:43",
                "workflow-run",
                searchResult="no-match",
                recentHistoryCollected=True,
                recentHistoryTruncated=False,
                recentHistory=[],
                historyCoversSourceRun=True,
                **scoped_by_reference(1),
            ),
        }
        report = minimal_report(
            state="resolved",
            action="close-resolved",
            evidence=[
                evidence_ref("issue:1", "issue-event"),
                evidence_ref("pr:77", "pull-request", role="merged-fix"),
                evidence_ref("run:42", "workflow-run", role="post-fix-green"),
                evidence_ref("run:43", "workflow-run", role="no-newer-matching-failure"),
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_one_workflow_run_can_satisfy_green_and_history_roles(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:1": evidence_record("issue:1", "issue-event", number=1),
            "pr:77": evidence_record(
                "pr:77",
                "pull-request",
                merged=True,
                **scoped_by_reference(1),
            ),
            "run:43": evidence_record(
                "run:43",
                "workflow-run",
                conclusion="success",
                recentHistoryCollected=True,
                recentHistoryTruncated=False,
                recentHistory=[
                    {
                        "runId": 43,
                        "conclusion": "success",
                        "createdAt": "2026-08-17T20:00:00Z",
                    }
                ],
                historyCoversSourceRun=True,
                **scoped_by_reference(1),
            ),
        }
        report = minimal_report(
            state="resolved",
            action="close-resolved",
            evidence=[
                evidence_ref("issue:1", "issue-event"),
                evidence_ref("pr:77", "pull-request", role="merged-fix"),
                evidence_ref(
                    "run:43",
                    "workflow-run",
                    roles=["post-fix-green", "no-newer-matching-failure"],
                ),
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_multi_role_green_run_requires_factual_success(self) -> None:
        snapshot, report = self._multi_role_close_resolved_case(
            conclusion="failure",
            recent_history=[
                {
                    "runId": 43,
                    "conclusion": "failure",
                    "createdAt": "2026-08-17T20:00:00Z",
                }
            ],
        )

        with self.assertRaisesRegex(ValidationError, "post-fix-green"):
            validate_report(snapshot, report)

    def test_post_fix_green_role_rejects_run_without_success(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:1": evidence_record("issue:1", "issue-event", role="merged-fix"),
            "run:42": evidence_record(
                "run:42",
                "workflow-run",
                role="post-fix-green",
                conclusion="failure",
                recentHistoryCollected=True,
                recentHistoryTruncated=False,
                recentHistory=[
                    {
                        "runId": 42,
                        "conclusion": "failure",
                        "createdAt": "2026-08-17T20:00:00Z",
                    }
                ],
                historyCoversSourceRun=True,
            ),
            "run:43": evidence_record(
                "run:43",
                "workflow-run",
                role="no-newer-matching-failure",
            ),
        }
        report = minimal_report(
            state="resolved",
            action="close-resolved",
            evidence=[
                evidence_ref("issue:1", "issue-event"),
                evidence_ref("run:42", "workflow-run"),
                evidence_ref("run:43", "workflow-run"),
            ],
        )

        with self.assertRaisesRegex(ValidationError, "post-fix-green"):
            validate_report(snapshot, report)

    def test_multi_role_green_run_accepts_success_in_covered_history(self) -> None:
        snapshot, report = self._multi_role_close_resolved_case(
            conclusion="failure",
            recent_history=[
                {
                    "runId": 44,
                    "conclusion": "success",
                    "createdAt": "2026-08-17T21:00:00Z",
                },
                {
                    "runId": 43,
                    "conclusion": "failure",
                    "createdAt": "2026-08-17T20:00:00Z",
                },
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_multi_role_green_run_requires_covered_history_for_no_newer_role(self) -> None:
        snapshot, report = self._multi_role_close_resolved_case(
            conclusion="success",
            recent_history=[
                {
                    "runId": 43,
                    "conclusion": "success",
                    "createdAt": "2026-08-17T20:00:00Z",
                }
            ],
            history_covers_source_run=False,
        )

        with self.assertRaisesRegex(ValidationError, "no-newer-matching-failure"):
            validate_report(snapshot, report)

    def test_report_roles_reject_empty_duplicate_unsupported_and_mixed_forms(self) -> None:
        invalid_references = (
            {"roles": []},
            {"roles": [""]},
            {"roles": [42]},
            {"roles": ["invented-role"]},
            {"roles": ["merged-fix", "merged-fix"]},
            {"role": "merged-fix", "roles": ["merged-fix"]},
        )
        for invalid_fields in invalid_references:
            with self.subTest(invalid_fields=invalid_fields):
                snapshot = minimal_snapshot()
                report = minimal_report()
                report["decisions"][0]["evidence"][0].update(invalid_fields)

                with self.assertRaisesRegex(ValidationError, "role"):
                    validate_report(snapshot, report)

    def test_report_roles_must_exactly_match_deterministic_snapshot_role(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"]["issue:1"]["payload"]["role"] = "merged-fix"

        matching_report = minimal_report(
            evidence=[
                evidence_ref("issue:1", "issue-event", roles=["merged-fix"]),
            ],
        )
        self.assertIsNone(validate_report(snapshot, matching_report))

        for roles in (["recovery"], ["merged-fix", "recovery"]):
            with self.subTest(roles=roles):
                conflicting_report = minimal_report(
                    evidence=[
                        evidence_ref("issue:1", "issue-event", roles=roles),
                    ],
                )
                with self.assertRaisesRegex(ValidationError, r"conflicts with snapshot role merged-fix"):
                    validate_report(snapshot, conflicting_report)

    def test_one_normalized_cause_can_apply_to_multiple_effective_roles(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:42": evidence_record(
                "run:42",
                "workflow-run",
                sourceIssueNumber=1,
            ),
            "issue:2": evidence_record(
                "issue:2",
                "issue-event",
                **scoped_by_reference(1),
            ),
        }
        report = minimal_report(
            state="regression",
            action="open-regression",
            evidence=[
                evidence_ref(
                    "run:42",
                    "workflow-run",
                    roles=["current-failing-run", "normalized-cause"],
                    normalized_cause="timeout-on-startup",
                ),
                evidence_ref(
                    "issue:2",
                    "issue-event",
                    role="prior-resolved-episode",
                    normalized_cause="timeout-on-startup",
                ),
            ],
            related_issues=[
                {
                    "type": "regression-of",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_report_role_must_not_conflict_with_deterministic_snapshot_role(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"]["run:44"] = evidence_record(
            "run:44",
            "workflow-run",
            role="newer-failure",
            sourceIssueNumber=1,
        )
        report = minimal_report(
            evidence=[
                evidence_ref("issue:1", "issue-event"),
                evidence_ref("run:44", "workflow-run", role="no-newer-matching-failure"),
            ],
        )

        with self.assertRaisesRegex(
            ValidationError,
            r"run:44.*report role no-newer-matching-failure.*snapshot role newer-failure",
        ):
            validate_report(snapshot, report)

    def _multi_role_close_resolved_case(
        self,
        *,
        conclusion: str,
        recent_history: list[dict[str, object]],
        history_covers_source_run: bool = True,
    ) -> tuple[dict[str, object], dict[str, object]]:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:1": evidence_record("issue:1", "issue-event", number=1),
            "pr:77": evidence_record(
                "pr:77",
                "pull-request",
                merged=True,
                **scoped_by_reference(1),
            ),
            "run:43": evidence_record(
                "run:43",
                "workflow-run",
                conclusion=conclusion,
                recentHistoryCollected=True,
                recentHistoryTruncated=False,
                recentHistory=recent_history,
                historyCoversSourceRun=history_covers_source_run,
                **scoped_by_reference(1),
            ),
        }
        report = minimal_report(
            state="resolved",
            action="close-resolved",
            evidence=[
                evidence_ref("issue:1", "issue-event"),
                evidence_ref("pr:77", "pull-request", role="merged-fix"),
                evidence_ref(
                    "run:43",
                    "workflow-run",
                    roles=["post-fix-green", "no-newer-matching-failure"],
                ),
            ],
        )
        return snapshot, report

    def test_required_positive_role_associated_only_with_another_issue_is_rejected(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:1": evidence_record("issue:1", "issue-event", role="merged-fix"),
            "run:42": evidence_record(
                "run:42",
                "workflow-run",
                sourceIssueNumber=2,
            ),
            "run:43": evidence_record(
                "run:43",
                "workflow-run",
                role="no-newer-matching-failure",
                sourceIssueNumber=1,
            ),
        }
        report = minimal_report(
            state="resolved",
            action="close-resolved",
            evidence=[
                evidence_ref("issue:1", "issue-event"),
                evidence_ref("run:42", "workflow-run", role="post-fix-green"),
                evidence_ref("run:43", "workflow-run"),
            ],
        )

        with self.assertRaisesRegex(ValidationError, r"post-fix-green.*issue 1"):
            validate_report(snapshot, report)

    def test_canonical_relationship_does_not_associate_unrelated_canonical_evidence(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"]["issue:2"] = evidence_record(
            "issue:2",
            "issue-event",
            number=2,
        )
        report = minimal_report(
            state="tracked-elsewhere",
            action="close-as-tracked",
            evidence=[
                evidence_ref("issue:1", "issue-event"),
                evidence_ref("issue:2", "issue-event", role="canonical-issue"),
            ],
            related_issues=[
                {
                    "type": "canonical-tracker",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        with self.assertRaisesRegex(ValidationError, r"canonical-issue.*issue 1"):
            validate_report(snapshot, report)

    def test_regression_relationship_does_not_associate_unrelated_prior_episode(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"].update(
            {
                "run:42": evidence_record(
                    "run:42",
                    "workflow-run",
                    normalizedCause="timeout-on-startup",
                    sourceIssueNumber=1,
                ),
                "issue:2": evidence_record(
                    "issue:2",
                    "issue-event",
                    number=2,
                    normalizedCause="timeout-on-startup",
                ),
                "issue:1:comment:17": evidence_record(
                    "issue:1:comment:17",
                    "issue-comment",
                    normalizedCause="timeout-on-startup",
                    sourceIssueNumber=1,
                ),
            }
        )
        report = minimal_report(
            state="regression",
            action="open-regression",
            evidence=[
                evidence_ref("issue:1", "issue-event"),
                evidence_ref("run:42", "workflow-run", role="current-failing-run"),
                evidence_ref("issue:2", "issue-event", role="prior-resolved-episode"),
                evidence_ref("issue:1:comment:17", "issue-comment", role="normalized-cause"),
            ],
            related_issues=[
                {
                    "type": "regression-of",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        with self.assertRaisesRegex(ValidationError, "prior-resolved-episode"):
            validate_report(snapshot, report)

    def test_qualified_decision_issue_record_is_associated_case_insensitively(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:OWNER/REPO:1": evidence_record(
                "issue:OWNER/REPO:1",
                "issue-event",
                number=1,
            ),
            "run:42": evidence_record(
                "run:42",
                "workflow-run",
                conclusion="success",
                sourceIssueNumber=1,
            ),
            "run:43": evidence_record(
                "run:43",
                "workflow-run",
                searchResult="no-match",
                recentHistoryCollected=True,
                recentHistoryTruncated=False,
                recentHistory=[],
                historyCoversSourceRun=True,
                sourceIssueNumber=1,
            ),
        }
        report = minimal_report(
            state="resolved",
            action="close-resolved",
            evidence=[
                evidence_ref("issue:OWNER/REPO:1", "issue-event", role="merged-fix"),
                evidence_ref("run:42", "workflow-run", role="post-fix-green"),
                evidence_ref("run:43", "workflow-run", role="no-newer-matching-failure"),
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_high_risk_decision_must_cite_scoped_roleless_snapshot_record(self) -> None:
        snapshot, report = high_risk_case("close-resolved")
        snapshot["evidence"]["issue:1:comment:99"] = evidence_record(
            "issue:1:comment:99",
            "issue-comment",
            sourceIssueNumber=1,
            body="Factual collector output with no semantic role.",
        )

        with self.assertRaisesRegex(
            ValidationError,
            r"close-resolved.*issue 1.*issue:1:comment:99",
        ):
            validate_report(snapshot, report)

    def test_snapshot_rejects_unsupported_evidence_availability(self) -> None:
        for availability in ("availble", "unavailable"):
            with self.subTest(availability=availability):
                snapshot = minimal_snapshot()
                snapshot["evidence"]["issue:1"]["availability"] = availability

                with self.assertRaisesRegex(ValidationError, rf"availability.*{availability}"):
                    validate_snapshot(snapshot)

    def test_optional_evidence_roles_must_be_supported(self) -> None:
        for role in (None, "", 42, "invented-role"):
            with self.subTest(location="snapshot", role=role):
                snapshot = minimal_snapshot()
                snapshot["evidence"]["issue:1"]["payload"]["role"] = role

                with self.assertRaisesRegex(ValidationError, "role"):
                    validate_snapshot(snapshot)

            with self.subTest(location="report", role=role):
                snapshot = minimal_snapshot()
                report = minimal_report()
                report["decisions"][0]["evidence"][0]["role"] = role

                with self.assertRaisesRegex(ValidationError, "role"):
                    validate_report(snapshot, report)

    def test_optional_normalized_causes_must_be_nonempty_strings(self) -> None:
        for normalized_cause in (None, "", "   ", 42):
            with self.subTest(location="snapshot", normalized_cause=normalized_cause):
                snapshot = minimal_snapshot()
                snapshot["evidence"]["issue:1"]["payload"]["normalizedCause"] = normalized_cause

                with self.assertRaisesRegex(ValidationError, "normalizedCause"):
                    validate_snapshot(snapshot)

            with self.subTest(location="report", normalized_cause=normalized_cause):
                snapshot = minimal_snapshot()
                report = minimal_report()
                report["decisions"][0]["evidence"][0]["normalizedCause"] = normalized_cause

                with self.assertRaisesRegex(ValidationError, "normalizedCause"):
                    validate_report(snapshot, report)

    def test_repository_strings_accept_strict_github_owner_repo_syntax(self) -> None:
        for repository in ("microsoft/aspire", "owner/re.po_name-1", "a/r"):
            with self.subTest(field="snapshot.repository", repository=repository):
                snapshot = minimal_snapshot()
                snapshot["repository"] = repository

                self.assertIsNone(validate_snapshot(snapshot))

            with self.subTest(field="report.repository", repository=repository):
                snapshot = minimal_snapshot()
                snapshot["repository"] = repository
                report = minimal_report()
                report["repository"] = repository
                report["decisions"][0]["issueUrl"] = f"https://github.com/{repository}/issues/1"

                self.assertIsNone(validate_report(snapshot, report))

    def test_repository_strings_reject_malformed_github_owner_repo_syntax(self) -> None:
        invalid_repositories = (
            "owner:bad/repo",
            "owner/bad:repo",
            "owner/re:po",
            "owner/repo?",
            "owner/repo#fragment",
            "owner/repo extra",
            "owner /repo",
            "owner/repo/extra",
            "-owner/repo",
            "owner-/repo",
            "own_er/repo",
            "owner./repo",
        )

        for repository in invalid_repositories:
            with self.subTest(field="snapshot.repository", repository=repository):
                snapshot = minimal_snapshot()
                snapshot["repository"] = repository

                with self.assertRaisesRegex(ValidationError, "owner/repo string"):
                    validate_snapshot(snapshot)

            with self.subTest(field="report.repository", repository=repository):
                snapshot = minimal_snapshot()
                report = minimal_report()
                report["repository"] = repository

                with self.assertRaisesRegex(ValidationError, "owner/repo string"):
                    validate_report(snapshot, report)

    def test_missing_decision_for_issue_11_fails(self) -> None:
        snapshot = minimal_snapshot(issue_number=11)
        report = minimal_report(issue_number=1)
        snapshot["openIssues"] = [1, 11]
        snapshot["evidence"]["issue:1"] = evidence_record("issue:1", "issue-event")

        with self.assertRaisesRegex(ValidationError, "11"):
            validate_report(snapshot, report)

    def test_high_risk_close_with_only_previous_report_evidence_is_rejected(self) -> None:
        snapshot = minimal_snapshot()
        evidence_id = "issue:1"
        snapshot["evidence"][evidence_id] = evidence_record(
            evidence_id,
            "issue-event",
            role="merged-fix",
            source="previous-report",
        )
        report = minimal_report(
            state="resolved",
            action="close",
            evidence=[evidence_ref(evidence_id, "issue-event")],
        )

        with self.assertRaisesRegex(ValidationError, "close"):
            validate_report(snapshot, report)

    def test_high_risk_close_with_comment_only_evidence_is_rejected(self) -> None:
        snapshot = minimal_snapshot()
        evidence_id = "issue:1:comment:17"
        snapshot["evidence"] = {
            evidence_id: evidence_record(
                evidence_id,
                "issue-comment",
                role="normalized-facts",
            )
        }
        report = minimal_report(
            state="resolved",
            action="close",
            evidence=[evidence_ref(evidence_id, "issue-comment")],
        )

        with self.assertRaisesRegex(ValidationError, "close"):
            validate_report(snapshot, report)

    def test_malformed_issue_evidence_ids_are_rejected(self) -> None:
        malformed_ids = [
            "issue:1:comment:2:event:3",
            "issue:1:event:2:comment:3",
            "issue:1:comment:2:comment:3",
        ]

        for malformed_id in malformed_ids:
            with self.subTest(malformed_id=malformed_id):
                snapshot = minimal_snapshot()
                snapshot["evidence"] = {
                    malformed_id: evidence_record(malformed_id, "issue-comment"),
                }

                with self.assertRaisesRegex(ValidationError, malformed_id):
                    validate_snapshot(snapshot)

    def test_workflow_evidence_ids_accept_literal_none_for_missing_attempt(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"]["run:42:attempt:none:job:7"] = evidence_record(
            "run:42:attempt:none:job:7",
            "workflow-job",
        )
        snapshot["evidence"]["run:42:attempt:none:job:7:log"] = evidence_record(
            "run:42:attempt:none:job:7:log",
            "workflow-log",
        )

        self.assertIsNone(validate_snapshot(snapshot))

    def test_decision_issue_url_must_match_snapshot_repository_and_issue_number(self) -> None:
        cases = [
            "https://github.com/owner/repo/issues/2",
            "https://github.com/other/repo/issues/1",
        ]

        for issue_url in cases:
            with self.subTest(issue_url=issue_url):
                snapshot = minimal_snapshot()
                report = minimal_report()
                report["decisions"][0]["issueUrl"] = issue_url

                with self.assertRaisesRegex(ValidationError, "issueUrl"):
                    validate_report(snapshot, report)

    def test_invalid_state_action_pair_is_rejected(self) -> None:
        snapshot = minimal_snapshot()
        report = minimal_report(state="actionable", action="close")

        with self.assertRaisesRegex(ValidationError, "actionable"):
            validate_report(snapshot, report)

    def test_high_risk_required_positive_role_matrix(self) -> None:
        for action, role, roles, expected_error in _HIGH_RISK_ROLE_MATRIX:
            for condition in _INVALID_POSITIVE_ROLE_CONDITIONS:
                with self.subTest(action=action, role=role, invalid_condition=condition):
                    snapshot, report = high_risk_case(action, roles=roles)
                    evidence_id = evidence_id_for_role(snapshot, role)
                    move_supporting_role(snapshot, report, role, condition)
                    expected_regex = expected_error
                    if condition == "omission":
                        expected_regex = rf"{action}.*issue 1.*{role}.*{re.escape(evidence_id)}"

                    with self.assertRaisesRegex(ValidationError, expected_regex):
                        validate_report(snapshot, report)

    def test_high_risk_alternative_roles_are_independently_accepted(self) -> None:
        for action, role, roles in _HIGH_RISK_ALTERNATIVE_SUCCESSES:
            with self.subTest(action=action, role=role):
                snapshot, report = high_risk_case(action, roles=roles)

                self.assertIsNone(validate_report(snapshot, report))

    def test_high_risk_alternative_roles_fail_when_neither_is_present(self) -> None:
        for action, role_pair, roles, expected_error in _HIGH_RISK_ALTERNATIVE_FAILURES:
            with self.subTest(action=action, role=role_pair, invalid_condition="neither-alternative"):
                snapshot, report = high_risk_case(action, roles=roles)

                with self.assertRaisesRegex(ValidationError, expected_error):
                    validate_report(snapshot, report)

    def test_uncited_scoped_newer_failure_rejects_close_resolved(self) -> None:
        snapshot, report = high_risk_case("close-resolved")
        snapshot["evidence"]["run:44"] = evidence_record(
            "run:44",
            "workflow-run",
            role="newer-failure",
            **scoped_by_reference(1),
        )

        with self.assertRaisesRegex(
            ValidationError,
            r"close-resolved.*issue 1.*newer-failure.*run:44",
        ):
            validate_report(snapshot, report)

    def test_uncited_scoped_newer_failure_for_another_issue_does_not_block_close_resolved(self) -> None:
        snapshot, report = high_risk_case("close-resolved")
        snapshot["evidence"]["run:44"] = evidence_record(
            "run:44",
            "workflow-run",
            role="newer-failure",
            **scoped_by_reference(2),
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_uncited_scoped_canonical_issue_rejects_open_dedicated_issue(self) -> None:
        snapshot, report = high_risk_case("open-dedicated-issue")
        snapshot["evidence"]["issue:2"] = evidence_record(
            "issue:2",
            "issue-event",
            role="canonical-issue",
            **scoped_by_reference(1),
        )

        with self.assertRaisesRegex(
            ValidationError,
            r"open-dedicated-issue.*issue 1.*canonical-issue.*issue:2",
        ):
            validate_report(snapshot, report)

    def test_direct_source_issue_number_scopes_uncited_action_role(self) -> None:
        snapshot, report = high_risk_case("close-resolved")
        snapshot["evidence"]["run:44"] = evidence_record(
            "run:44",
            "workflow-run",
            role="newer-failure",
            sourceIssueNumber=1,
        )

        with self.assertRaisesRegex(
            ValidationError,
            r"close-resolved.*issue 1.*newer-failure.*run:44",
        ):
            validate_report(snapshot, report)

    def test_all_current_scoped_records_trigger_completeness_but_previous_report_does_not(self) -> None:
        snapshot, report = high_risk_case("close-resolved")
        snapshot["evidence"]["run:44"] = evidence_record(
            "run:44",
            "workflow-run",
            role="newer-failure",
            sourceIssueNumber=1,
            availability="expired-or-unavailable",
        )

        with self.assertRaisesRegex(ValidationError, r"close-resolved.*issue 1.*run:44"):
            validate_report(snapshot, report)

        snapshot, report = high_risk_case("close-resolved")
        snapshot["evidence"]["run:44"] = evidence_record(
            "run:44",
            "workflow-run",
            role="newer-failure",
            sourceIssueNumber=1,
            source="previous-report",
        )

        self.assertIsNone(validate_report(snapshot, report))

        for condition in ("unavailable", "previous-report"):
            with self.subTest(positive_role_condition=condition):
                snapshot, report = high_risk_case("close-resolved")
                post_fix_green_id = evidence_id_for_role(snapshot, "post-fix-green")
                snapshot["evidence"][post_fix_green_id]["payload"]["sourceIssueNumber"] = 1
                move_supporting_role(snapshot, report, "post-fix-green", condition)

                with self.assertRaisesRegex(ValidationError, "post-fix-green"):
                    validate_report(snapshot, report)

    def test_cited_scoped_newer_failure_reaches_action_validation_in_any_bucket(self) -> None:
        for bucket in ("evidence", "contradictoryEvidence", "missingEvidence"):
            with self.subTest(bucket=bucket):
                snapshot, report = high_risk_case("close-resolved")
                snapshot["evidence"]["run:44"] = evidence_record(
                    "run:44",
                    "workflow-run",
                    role="newer-failure",
                    sourceIssueNumber=1,
                )
                report["decisions"][0][bucket] = [
                    *report["decisions"][0][bucket],
                    evidence_ref("run:44", "workflow-run"),
                ]

                with self.assertRaisesRegex(ValidationError, "cannot include newer-failure"):
                    validate_report(snapshot, report)

    def test_high_risk_blockers_are_filtered_to_decision_issue_in_every_bucket(self) -> None:
        blocker_cases = (
            ("close-resolved", "run:44", "workflow-run", {"role": "newer-failure"}),
            ("open-dedicated-issue", "issue:3", "issue-event", {"role": "canonical-issue"}),
            ("close-as-tracked", "issue:3", "issue-event", {"role": "canonical-issue"}),
            ("merge-duplicate", "issue:3", "issue-event", {"role": "canonical-issue"}),
            (
                "open-regression",
                "run:40",
                "workflow-run",
                {
                    "role": "prior-resolved-episode",
                    "normalizedCause": "timeout-on-startup",
                    "priorIssueNumber": 3,
                },
            ),
        )
        for action, blocker_id, kind, payload in blocker_cases:
            for bucket in ("evidence", "contradictoryEvidence", "missingEvidence"):
                for source_issue_number, should_block in ((2, False), (1, True)):
                    with self.subTest(
                        action=action,
                        bucket=bucket,
                        source_issue_number=source_issue_number,
                    ):
                        snapshot, report = high_risk_case(action)
                        snapshot["evidence"][blocker_id] = evidence_record(
                            blocker_id,
                            kind,
                            sourceIssueNumber=source_issue_number,
                            **payload,
                        )
                        report["decisions"][0][bucket] = [
                            *report["decisions"][0][bucket],
                            evidence_ref(blocker_id, kind),
                        ]

                        if should_block:
                            with self.assertRaises(ValidationError):
                                validate_report(snapshot, report)
                        else:
                            self.assertIsNone(validate_report(snapshot, report))

    def test_wrong_issue_positive_evidence_cannot_satisfy_any_high_risk_action(self) -> None:
        cases = (
            ("close-resolved", "post-fix-green"),
            ("open-dedicated-issue", "current-failing-run"),
            ("close-as-tracked", "canonical-issue"),
            ("merge-duplicate", "canonical-issue"),
            ("open-regression", "current-failing-run"),
        )
        for action, role in cases:
            with self.subTest(action=action, role=role):
                snapshot, report = high_risk_case(action)
                evidence_id = evidence_id_for_role(snapshot, role)
                payload = snapshot["evidence"][evidence_id]["payload"]
                payload.pop("referencedBy", None)
                payload["sourceIssueNumber"] = 2

                with self.assertRaisesRegex(ValidationError, role):
                    validate_report(snapshot, report)

    def test_positive_high_risk_decision_with_all_scoped_roles_cited_passes(self) -> None:
        snapshot, report = high_risk_case("close-resolved")
        for record in snapshot["evidence"].values():
            record["payload"]["sourceIssueNumber"] = 1

        self.assertIsNone(validate_report(snapshot, report))

    def test_high_risk_relationship_matrix_rejects_missing_or_mismatched_relationships(self) -> None:
        for action, relationship, related_issues, expected_error in _HIGH_RISK_RELATIONSHIP_FAILURES:
            if not related_issues:
                condition = "omission"
            else:
                relation = related_issues[0]
                if relation["type"] != _HIGH_RISK_ACTIONS[action]["relatedIssues"][0]["type"]:
                    condition = "wrong-type"
                elif relation["targetIssueNumber"] != 2:
                    condition = "wrong-target"
                else:
                    condition = "wrong-repository"
            with self.subTest(action=action, relationship=relationship, invalid_condition=condition):
                snapshot, report = high_risk_case(action, related_issues=related_issues)

                with self.assertRaisesRegex(ValidationError, expected_error):
                    validate_report(snapshot, report)

    def test_related_issue_target_repository_must_be_valid_when_present(self) -> None:
        invalid_target_repositories = (
            None,
            "",
            "   ",
            "owner",
            "owner/",
            "/repo",
            "owner/repo/extra",
            "owner:bad/repo",
            "owner/bad:repo",
            "owner/repo?",
            "owner/repo#fragment",
            "owner/repo extra",
            "-owner/repo",
            "owner-/repo",
            "own_er/repo",
            "owner./repo",
        )

        for target_repository in invalid_target_repositories:
            with self.subTest(action="wait", relationship="related", invalid_condition=target_repository):
                snapshot = minimal_snapshot()
                report = minimal_report(
                    related_issues=[
                        {
                            "type": "related",
                            "sourceIssueNumber": 1,
                            "targetIssueNumber": 2,
                            "targetRepository": target_repository,
                        }
                    ],
                )

                with self.assertRaisesRegex(ValidationError, "targetRepository"):
                    validate_report(snapshot, report)

    def test_related_issue_target_repository_absence_uses_snapshot_repository(self) -> None:
        snapshot = minimal_snapshot()
        report = minimal_report(
            related_issues=[
                {
                    "type": "related",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_related_issue_target_repository_accepts_valid_external_repository(self) -> None:
        snapshot = minimal_snapshot()
        report = minimal_report(
            related_issues=[
                {
                    "type": "related",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                    "targetRepository": "other/repo",
                }
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_related_issue_rejects_self_reference_in_snapshot_repository(self) -> None:
        cases = (
            {},
            {"targetRepository": "owner/repo"},
        )

        for extra_relationship_fields in cases:
            with self.subTest(extra_relationship_fields=extra_relationship_fields):
                snapshot = minimal_snapshot()
                report = minimal_report(
                    related_issues=[
                        {
                            "type": "related",
                            "sourceIssueNumber": 1,
                            "targetIssueNumber": 1,
                            **extra_relationship_fields,
                        }
                    ],
                )

                with self.assertRaisesRegex(ValidationError, "different issue"):
                    validate_report(snapshot, report)

    def test_related_issue_accepts_same_number_in_different_repository(self) -> None:
        snapshot = minimal_snapshot()
        report = minimal_report(
            related_issues=[
                {
                    "type": "related",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 1,
                    "targetRepository": "other/repo",
                }
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_qualified_evidence_ids_accept_strict_github_owner_repo_syntax(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:microsoft/aspire:2": evidence_record("issue:microsoft/aspire:2", "issue-event"),
            "pr:owner/re.po_name-1:3": evidence_record("pr:owner/re.po_name-1:3", "pull-request"),
            "commit:owner/re.po_name-1:abcdef1": evidence_record(
                "commit:owner/re.po_name-1:abcdef1",
                "commit",
            ),
        }

        self.assertIsNone(validate_snapshot(snapshot))

    def test_qualified_evidence_ids_reject_malformed_repository_syntax(self) -> None:
        invalid_evidence = (
            ("issue:-owner/repo:2", "issue-event"),
            ("issue:owner-/repo:2", "issue-event"),
            ("issue:own_er/repo:2", "issue-event"),
            ("issue:owner/repo?:2", "issue-event"),
            ("issue:owner/repo#fragment:2", "issue-event"),
            ("issue:owner/repo/extra:2", "issue-event"),
            ("pr:owner/repo with spaces:3", "pull-request"),
            ("commit:owner:bad/repo:abcdef1", "commit"),
        )

        for evidence_id, kind in invalid_evidence:
            with self.subTest(evidence_id=evidence_id):
                snapshot = minimal_snapshot()
                snapshot["evidence"] = {
                    evidence_id: evidence_record(evidence_id, kind),
                }

                with self.assertRaisesRegex(ValidationError, "Unsupported evidence ID"):
                    validate_snapshot(snapshot)

    def test_close_resolved_requires_merged_fix_or_recovery_evidence(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:42:attempt:1:job:7": evidence_record(
                "run:42:attempt:1:job:7",
                "workflow-job",
                role="post-fix-green",
            ),
            "run:43": evidence_record(
                "run:43",
                "workflow-run",
                role="no-newer-matching-failure",
            ),
        }
        report = minimal_report(
            state="resolved",
            action="close-resolved",
            evidence=[
                evidence_ref("run:42:attempt:1:job:7", "workflow-job"),
                evidence_ref("run:43", "workflow-run"),
            ],
        )

        with self.assertRaisesRegex(ValidationError, "merged-fix or recovery"):
            validate_report(snapshot, report)

    def test_close_resolved_requires_post_fix_green_evidence(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:1": evidence_record("issue:1", "issue-event", role="merged-fix"),
            "run:43": evidence_record(
                "run:43",
                "workflow-run",
                role="no-newer-matching-failure",
            ),
        }
        report = minimal_report(
            state="resolved",
            action="close-resolved",
            evidence=[
                evidence_ref("issue:1", "issue-event"),
                evidence_ref("run:43", "workflow-run"),
            ],
        )

        with self.assertRaisesRegex(ValidationError, "post-fix-green"):
            validate_report(snapshot, report)

    def test_close_resolved_requires_no_newer_matching_failure_evidence(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:1": evidence_record("issue:1", "issue-event", role="merged-fix"),
            "run:42:attempt:1:job:7": evidence_record(
                "run:42:attempt:1:job:7",
                "workflow-job",
                role="post-fix-green",
            ),
        }
        report = minimal_report(
            state="resolved",
            action="close-resolved",
            evidence=[
                evidence_ref("issue:1", "issue-event"),
                evidence_ref("run:42:attempt:1:job:7", "workflow-job"),
            ],
        )

        with self.assertRaisesRegex(ValidationError, "no-newer-matching-failure"):
            validate_report(snapshot, report)

    def test_valid_close_resolved_with_no_newer_matching_failure_passes(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:1": evidence_record("issue:1", "issue-event", role="merged-fix"),
            "run:42:attempt:1:job:7": evidence_record(
                "run:42:attempt:1:job:7",
                "workflow-job",
                role="post-fix-green",
            ),
            "run:43": evidence_record(
                "run:43",
                "workflow-run",
                role="no-newer-matching-failure",
            ),
        }
        report = minimal_report(
            state="resolved",
            action="close-resolved",
            evidence=[
                evidence_ref("issue:1", "issue-event"),
                evidence_ref("run:42:attempt:1:job:7", "workflow-job"),
                evidence_ref("run:43", "workflow-run"),
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_close_resolved_with_unavailable_required_roles_is_rejected(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:1": evidence_record(
                "issue:1",
                "issue-event",
                availability="expired-or-unavailable",
                role="merged-fix",
            ),
            "run:42:attempt:1:job:7": evidence_record(
                "run:42:attempt:1:job:7",
                "workflow-job",
                availability="expired-or-unavailable",
                role="post-fix-green",
            ),
            "run:43": evidence_record(
                "run:43",
                "workflow-run",
                availability="expired-or-unavailable",
                role="no-newer-matching-failure",
            ),
        }
        report = minimal_report(
            state="resolved",
            action="close-resolved",
            evidence=[
                evidence_ref("issue:1", "issue-event"),
                evidence_ref("run:42:attempt:1:job:7", "workflow-job"),
                evidence_ref("run:43", "workflow-run"),
            ],
        )

        with self.assertRaisesRegex(ValidationError, "merged-fix or recovery"):
            validate_report(snapshot, report)

    def test_close_resolved_rejects_newer_failure_in_any_bucket(self) -> None:
        for bucket in ("evidence", "contradictoryEvidence", "missingEvidence"):
            with self.subTest(bucket=bucket):
                snapshot = minimal_snapshot()
                snapshot["evidence"] = {
                    "issue:1": evidence_record("issue:1", "issue-event", role="merged-fix"),
                    "run:42:attempt:1:job:7": evidence_record(
                        "run:42:attempt:1:job:7",
                        "workflow-job",
                        role="post-fix-green",
                    ),
                    "run:43": evidence_record(
                        "run:43",
                        "workflow-run",
                        role="no-newer-matching-failure",
                    ),
                    "run:44": evidence_record(
                        "run:44",
                        "workflow-run",
                        role="newer-failure",
                    ),
                }
                report = minimal_report(
                    state="resolved",
                    action="close-resolved",
                    evidence=[
                        evidence_ref("issue:1", "issue-event"),
                        evidence_ref("run:42:attempt:1:job:7", "workflow-job"),
                        evidence_ref("run:43", "workflow-run"),
                    ],
                    contradictory_evidence=[],
                    missing_evidence=[],
                )
                report["decisions"][0][bucket] = [
                    *report["decisions"][0][bucket],
                    evidence_ref("run:44", "workflow-run"),
                ]

                with self.assertRaisesRegex(ValidationError, "newer-failure"):
                    validate_report(snapshot, report)

    def test_close_requires_supporting_evidence_for_required_roles(self) -> None:
        cases = (
            (
                "merged-fix",
                "issue:1",
                "issue-event",
                evidence_record("issue:1", "issue-event", role="merged-fix"),
                "merged-fix or recovery",
            ),
            (
                "post-fix-green",
                "run:42:attempt:1:job:7",
                "workflow-job",
                evidence_record(
                    "run:42:attempt:1:job:7",
                    "workflow-job",
                    role="post-fix-green",
                ),
                "post-fix-green",
            ),
            (
                "no-newer-matching-failure",
                "run:43",
                "workflow-run",
                evidence_record(
                    "run:43",
                    "workflow-run",
                    role="no-newer-matching-failure",
                ),
                "no-newer-matching-failure",
            ),
        )
        for bucket in ("contradictoryEvidence", "missingEvidence"):
            for role, evidence_id, kind, record, error in cases:
                with self.subTest(bucket=bucket, role=role):
                    snapshot = minimal_snapshot()
                    snapshot["evidence"] = {
                        "issue:1": evidence_record("issue:1", "issue-event", role="merged-fix"),
                        "run:42:attempt:1:job:7": evidence_record(
                            "run:42:attempt:1:job:7",
                            "workflow-job",
                            role="post-fix-green",
                        ),
                        "run:43": evidence_record(
                            "run:43",
                            "workflow-run",
                            role="no-newer-matching-failure",
                        ),
                    }
                    snapshot["evidence"][evidence_id] = record
                    report = minimal_report(
                        state="resolved",
                        action="close",
                        evidence=[],
                        contradictory_evidence=[],
                        missing_evidence=[],
                    )
                    supporting_ids = {
                        "issue:1",
                        "run:42:attempt:1:job:7",
                        "run:43",
                    }
                    supporting_ids.remove(evidence_id)
                    report["decisions"][0]["evidence"] = [
                        evidence_ref(supporting_id, snapshot["evidence"][supporting_id]["kind"])
                        for supporting_id in supporting_ids
                    ]
                    report["decisions"][0][bucket] = [evidence_ref(evidence_id, kind)]

                    with self.assertRaisesRegex(ValidationError, error):
                        validate_report(snapshot, report)

    def test_close_requires_no_newer_matching_failure_evidence(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:1": evidence_record("issue:1", "issue-event", role="merged-fix"),
            "run:42:attempt:1:job:7": evidence_record(
                "run:42:attempt:1:job:7",
                "workflow-job",
                role="post-fix-green",
            ),
        }
        report = minimal_report(
            state="resolved",
            action="close",
            evidence=[
                evidence_ref("issue:1", "issue-event"),
                evidence_ref("run:42:attempt:1:job:7", "workflow-job"),
            ],
        )

        with self.assertRaisesRegex(ValidationError, "no-newer-matching-failure"):
            validate_report(snapshot, report)

    def test_close_rejects_newer_failure_in_missing_evidence(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:1": evidence_record("issue:1", "issue-event", role="merged-fix"),
            "run:42:attempt:1:job:7": evidence_record(
                "run:42:attempt:1:job:7",
                "workflow-job",
                role="post-fix-green",
            ),
            "run:43": evidence_record(
                "run:43",
                "workflow-run",
                role="no-newer-matching-failure",
            ),
            "run:42:attempt:1:job:8": evidence_record(
                "run:42:attempt:1:job:8",
                "workflow-job",
                role="newer-failure",
            ),
        }
        report = minimal_report(
            state="resolved",
            action="close",
            evidence=[
                evidence_ref("issue:1", "issue-event"),
                evidence_ref("run:42:attempt:1:job:7", "workflow-job"),
                evidence_ref("run:43", "workflow-run"),
            ],
            missing_evidence=[
                evidence_ref("run:42:attempt:1:job:8", "workflow-job"),
            ],
        )

        with self.assertRaisesRegex(ValidationError, "newer-failure"):
            validate_report(snapshot, report)

    def test_close_resolved_with_unavailable_newer_failure_and_no_available_no_newer_signal_is_rejected(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:1": evidence_record("issue:1", "issue-event", role="merged-fix"),
            "run:42:attempt:1:job:7": evidence_record(
                "run:42:attempt:1:job:7",
                "workflow-job",
                role="post-fix-green",
            ),
            "run:44": evidence_record(
                "run:44",
                "workflow-run",
                availability="expired-or-unavailable",
                role="newer-failure",
            ),
        }
        report = minimal_report(
            state="resolved",
            action="close-resolved",
            evidence=[
                evidence_ref("issue:1", "issue-event"),
                evidence_ref("run:42:attempt:1:job:7", "workflow-job"),
            ],
            missing_evidence=[
                evidence_ref("run:44", "workflow-run"),
            ],
        )

        with self.assertRaisesRegex(ValidationError, "no-newer-matching-failure"):
            validate_report(snapshot, report)

    def test_close_resolved_with_available_no_newer_signal_and_unavailable_newer_failure_still_passes(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:1": evidence_record("issue:1", "issue-event", role="merged-fix"),
            "run:42:attempt:1:job:7": evidence_record(
                "run:42:attempt:1:job:7",
                "workflow-job",
                role="post-fix-green",
            ),
            "run:43": evidence_record(
                "run:43",
                "workflow-run",
                role="no-newer-matching-failure",
            ),
            "run:44": evidence_record(
                "run:44",
                "workflow-run",
                availability="expired-or-unavailable",
                role="newer-failure",
            ),
        }
        report = minimal_report(
            state="resolved",
            action="close-resolved",
            evidence=[
                evidence_ref("issue:1", "issue-event"),
                evidence_ref("run:42:attempt:1:job:7", "workflow-job"),
                evidence_ref("run:43", "workflow-run"),
            ],
            missing_evidence=[
                evidence_ref("run:44", "workflow-run"),
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_close_stale_requires_obsolete_surface_evidence(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:44": evidence_record(
                "run:44",
                "workflow-run",
                role="no-recent-matching-failure",
            ),
        }
        report = minimal_report(
            state="stale",
            action="close-stale",
            evidence=[
                evidence_ref("run:44", "workflow-run"),
            ],
        )

        with self.assertRaisesRegex(ValidationError, "obsolete-surface"):
            validate_report(snapshot, report)

    def test_low_risk_actions_reject_unproven_obsolete_surface_role(self) -> None:
        for state, action in (("observing", "wait"), ("actionable", "investigate")):
            with self.subTest(action=action):
                snapshot = minimal_snapshot()
                snapshot["evidence"] = {
                    "source:src/Surface.cs": evidence_record(
                        "source:src/Surface.cs",
                        "source-path",
                        sourceIssueNumber=1,
                        checkoutCommit="a" * 40,
                        exists=True,
                        removalCommit=None,
                        replacementPath=None,
                        replacementCommit=None,
                        historyAmbiguous=False,
                    ),
                }
                report = minimal_report(
                    state=state,
                    action=action,
                    evidence=[
                        evidence_ref(
                            "source:src/Surface.cs",
                            "source-path",
                            role="obsolete-surface",
                        ),
                    ],
                )

                with self.assertRaisesRegex(ValidationError, "obsolete-surface"):
                    validate_report(snapshot, report)

    def test_low_risk_actions_accept_proven_obsolete_surface_role(self) -> None:
        for state, action in (("observing", "wait"), ("actionable", "investigate")):
            with self.subTest(action=action):
                snapshot = minimal_snapshot()
                snapshot["evidence"] = {
                    "source:src/RemovedSurface.cs": evidence_record(
                        "source:src/RemovedSurface.cs",
                        "source-path",
                        sourceIssueNumber=1,
                        checkoutCommit="a" * 40,
                        exists=False,
                        removalCommit="b" * 40,
                        replacementPath=None,
                        replacementCommit=None,
                        historyAmbiguous=False,
                    ),
                }
                report = minimal_report(
                    state=state,
                    action=action,
                    evidence=[
                        evidence_ref(
                            "source:src/RemovedSurface.cs",
                            "source-path",
                            role="obsolete-surface",
                        ),
                    ],
                )

                self.assertIsNone(validate_report(snapshot, report))

    def test_close_stale_requires_obsolete_surface_and_no_recent_matching_failure(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "source:src/RemovedWorkflow.yml": evidence_record(
                "source:src/RemovedWorkflow.yml",
                "source-path",
                role="obsolete-surface",
            ),
        }
        report = minimal_report(
            state="stale",
            action="close-stale",
            evidence=[
                evidence_ref("source:src/RemovedWorkflow.yml", "source-path"),
            ],
        )

        with self.assertRaisesRegex(ValidationError, "no-recent-matching-failure"):
            validate_report(snapshot, report)

    def test_valid_close_stale_with_obsolete_surface_and_no_recent_match_passes(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "source:src/RemovedWorkflow.yml": evidence_record(
                "source:src/RemovedWorkflow.yml",
                "source-path",
                role="obsolete-surface",
            ),
            "run:44": evidence_record(
                "run:44",
                "workflow-run",
                role="no-recent-matching-failure",
            ),
        }
        report = minimal_report(
            state="stale",
            action="close-stale",
            evidence=[
                evidence_ref("source:src/RemovedWorkflow.yml", "source-path"),
                evidence_ref("run:44", "workflow-run"),
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_close_stale_rejects_available_existing_source_as_obsolete_proof(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "source:src/RemovedWorkflow.yml": evidence_record(
                "source:src/RemovedWorkflow.yml",
                "source-path",
                role="obsolete-surface",
                exists=True,
                removalCommit=None,
            ),
            "run:44": evidence_record(
                "run:44",
                "workflow-run",
                role="no-recent-matching-failure",
            ),
        }
        report = minimal_report(
            state="stale",
            action="close-stale",
            evidence=[
                evidence_ref("source:src/RemovedWorkflow.yml", "source-path"),
                evidence_ref("run:44", "workflow-run"),
            ],
        )

        with self.assertRaisesRegex(ValidationError, "obsolete-surface"):
            validate_report(snapshot, report)

    def test_close_stale_with_partial_required_roles_is_rejected(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "source:src/RemovedWorkflow.yml": evidence_record(
                "source:src/RemovedWorkflow.yml",
                "source-path",
                availability="partial",
                role="obsolete-surface",
            ),
            "run:44": evidence_record(
                "run:44",
                "workflow-run",
                availability="partial",
                role="no-recent-matching-failure",
            ),
        }
        report = minimal_report(
            state="stale",
            action="close-stale",
            evidence=[
                evidence_ref("source:src/RemovedWorkflow.yml", "source-path"),
                evidence_ref("run:44", "workflow-run"),
            ],
        )

        with self.assertRaisesRegex(ValidationError, "obsolete-surface"):
            validate_report(snapshot, report)

    def test_close_stale_requires_supporting_evidence_for_required_roles(self) -> None:
        cases = (
            (
                "obsolete-surface",
                "source:src/RemovedWorkflow.yml",
                "source-path",
                evidence_record(
                    "source:src/RemovedWorkflow.yml",
                    "source-path",
                    role="obsolete-surface",
                ),
                "obsolete-surface",
            ),
            (
                "no-recent-matching-failure",
                "run:44",
                "workflow-run",
                evidence_record(
                    "run:44",
                    "workflow-run",
                    role="no-recent-matching-failure",
                ),
                "no-recent-matching-failure",
            ),
        )
        for bucket in ("contradictoryEvidence", "missingEvidence"):
            for role, evidence_id, kind, record, error in cases:
                with self.subTest(bucket=bucket, role=role):
                    snapshot = minimal_snapshot()
                    snapshot["evidence"] = {
                        "source:src/RemovedWorkflow.yml": evidence_record(
                            "source:src/RemovedWorkflow.yml",
                            "source-path",
                            role="obsolete-surface",
                        ),
                        "run:44": evidence_record(
                            "run:44",
                            "workflow-run",
                            role="no-recent-matching-failure",
                        ),
                    }
                    snapshot["evidence"][evidence_id] = record
                    report = minimal_report(
                        state="stale",
                        action="close-stale",
                        evidence=[],
                        contradictory_evidence=[],
                        missing_evidence=[],
                    )
                    supporting_ids = {
                        "source:src/RemovedWorkflow.yml",
                        "run:44",
                    }
                    supporting_ids.remove(evidence_id)
                    report["decisions"][0]["evidence"] = [
                        evidence_ref(supporting_id, snapshot["evidence"][supporting_id]["kind"])
                        for supporting_id in supporting_ids
                    ]
                    report["decisions"][0][bucket] = [evidence_ref(evidence_id, kind)]

                    with self.assertRaisesRegex(ValidationError, error):
                        validate_report(snapshot, report)

    def test_close_stale_rejects_newer_failure_in_any_bucket(self) -> None:
        for bucket in ("evidence", "contradictoryEvidence", "missingEvidence"):
            with self.subTest(bucket=bucket):
                snapshot = minimal_snapshot()
                snapshot["evidence"] = {
                    "source:src/RemovedWorkflow.yml": evidence_record(
                        "source:src/RemovedWorkflow.yml",
                        "source-path",
                        role="obsolete-surface",
                    ),
                    "run:44": evidence_record(
                        "run:44",
                        "workflow-run",
                        role="no-recent-matching-failure",
                    ),
                    "run:45": evidence_record(
                        "run:45",
                        "workflow-run",
                        role="newer-failure",
                    ),
                }
                report = minimal_report(
                    state="stale",
                    action="close-stale",
                    evidence=[
                        evidence_ref("source:src/RemovedWorkflow.yml", "source-path"),
                        evidence_ref("run:44", "workflow-run"),
                    ],
                    contradictory_evidence=[],
                    missing_evidence=[],
                )
                report["decisions"][0][bucket] = [
                    *report["decisions"][0][bucket],
                    evidence_ref("run:45", "workflow-run"),
                ]

                with self.assertRaisesRegex(ValidationError, "newer-failure"):
                    validate_report(snapshot, report)

    def test_close_stale_with_newer_failure_is_rejected(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "source:src/RemovedWorkflow.yml": evidence_record(
                "source:src/RemovedWorkflow.yml",
                "source-path",
                role="obsolete-surface",
            ),
            "run:44": evidence_record(
                "run:44",
                "workflow-run",
                role="no-recent-matching-failure",
            ),
            "run:45": evidence_record(
                "run:45",
                "workflow-run",
                role="newer-failure",
            ),
        }
        report = minimal_report(
            state="stale",
            action="close-stale",
            evidence=[
                evidence_ref("source:src/RemovedWorkflow.yml", "source-path"),
                evidence_ref("run:44", "workflow-run"),
                evidence_ref("run:45", "workflow-run"),
            ],
        )

        with self.assertRaisesRegex(ValidationError, "newer-failure"):
            validate_report(snapshot, report)

    def test_close_as_tracked_requires_canonical_tracker_relationship(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:2": evidence_record("issue:2", "issue-event", role="canonical-issue"),
        }
        report = minimal_report(
            state="tracked-elsewhere",
            action="close-as-tracked",
            evidence=[
                evidence_ref("issue:2", "issue-event"),
            ],
            related_issues=[
                {
                    "type": "probable-duplicate",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        with self.assertRaisesRegex(ValidationError, "canonical-tracker"):
            validate_report(snapshot, report)

    def test_close_as_tracked_requires_canonical_issue_evidence(self) -> None:
        snapshot = minimal_snapshot()
        report = minimal_report(
            state="tracked-elsewhere",
            action="close-as-tracked",
            related_issues=[
                {
                    "type": "canonical-tracker",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        with self.assertRaisesRegex(ValidationError, "canonical-issue"):
            validate_report(snapshot, report)

    def test_valid_close_as_tracked_with_local_compact_issue_id_passes(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:2": evidence_record("issue:2", "issue-event", role="canonical-issue"),
        }
        report = minimal_report(
            state="tracked-elsewhere",
            action="close-as-tracked",
            evidence=[
                evidence_ref("issue:2", "issue-event"),
            ],
            related_issues=[
                {
                    "type": "canonical-tracker",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_valid_close_as_tracked_with_exact_duplicate_passes(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:2": evidence_record("issue:2", "issue-event", role="canonical-issue"),
        }
        report = minimal_report(
            state="tracked-elsewhere",
            action="close-as-tracked",
            evidence=[
                evidence_ref("issue:2", "issue-event"),
            ],
            related_issues=[
                {
                    "type": "exact-duplicate",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_close_as_tracked_requires_supporting_canonical_issue_evidence(self) -> None:
        for bucket in ("contradictoryEvidence", "missingEvidence"):
            with self.subTest(bucket=bucket):
                snapshot = minimal_snapshot()
                snapshot["evidence"] = {
                    "issue:2": evidence_record("issue:2", "issue-event", role="canonical-issue"),
                }
                report = minimal_report(
                    state="tracked-elsewhere",
                    action="close-as-tracked",
                    evidence=[],
                    contradictory_evidence=[],
                    missing_evidence=[],
                    related_issues=[
                        {
                            "type": "canonical-tracker",
                            "sourceIssueNumber": 1,
                            "targetIssueNumber": 2,
                        }
                    ],
                )
                report["decisions"][0][bucket] = [
                    evidence_ref("issue:2", "issue-event"),
                ]

                with self.assertRaisesRegex(ValidationError, "canonical-issue"):
                    validate_report(snapshot, report)

    def test_close_as_tracked_rejects_exact_duplicate_target_mismatch_when_tracker_matches(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:2": evidence_record("issue:2", "issue-event", role="canonical-issue"),
        }
        report = minimal_report(
            state="tracked-elsewhere",
            action="close-as-tracked",
            evidence=[
                evidence_ref("issue:2", "issue-event"),
            ],
            related_issues=[
                {
                    "type": "canonical-tracker",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                },
                {
                    "type": "exact-duplicate",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 3,
                },
            ],
        )

        with self.assertRaisesRegex(ValidationError, "same targetRepository and targetIssueNumber"):
            validate_report(snapshot, report)

    def test_close_as_tracked_with_unavailable_canonical_issue_evidence_is_rejected(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:2": evidence_record(
                "issue:2",
                "issue-event",
                availability="expired-or-unavailable",
                role="canonical-issue",
            ),
        }
        report = minimal_report(
            state="tracked-elsewhere",
            action="close-as-tracked",
            evidence=[
                evidence_ref("issue:2", "issue-event"),
            ],
            related_issues=[
                {
                    "type": "canonical-tracker",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        with self.assertRaisesRegex(ValidationError, "canonical-issue"):
            validate_report(snapshot, report)

    def test_close_as_tracked_rejects_canonical_issue_evidence_for_different_target_issue(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:2": evidence_record("issue:2", "issue-event", role="canonical-issue"),
        }
        report = minimal_report(
            state="tracked-elsewhere",
            action="close-as-tracked",
            evidence=[
                evidence_ref("issue:2", "issue-event"),
            ],
            related_issues=[
                {
                    "type": "canonical-tracker",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 3,
                }
            ],
        )

        with self.assertRaisesRegex(ValidationError, "targetIssueNumber"):
            validate_report(snapshot, report)

    def test_close_as_tracked_rejects_external_canonical_issue_without_target_repository(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:other/repo:2": evidence_record(
                "issue:other/repo:2",
                "issue-event",
                role="canonical-issue",
            ),
        }
        report = minimal_report(
            state="tracked-elsewhere",
            action="close-as-tracked",
            evidence=[
                evidence_ref("issue:other/repo:2", "issue-event"),
            ],
            related_issues=[
                {
                    "type": "canonical-tracker",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        with self.assertRaisesRegex(ValidationError, "owner/repo"):
            validate_report(snapshot, report)

    def test_valid_close_as_tracked_with_qualified_same_repository_issue_id_passes(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:owner/repo:2": evidence_record(
                "issue:owner/repo:2",
                "issue-event",
                role="canonical-issue",
            ),
        }
        report = minimal_report(
            state="tracked-elsewhere",
            action="close-as-tracked",
            evidence=[
                evidence_ref("issue:owner/repo:2", "issue-event"),
            ],
            related_issues=[
                {
                    "type": "canonical-tracker",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_valid_close_as_tracked_with_external_target_repository_passes(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:other/repo:2": evidence_record(
                "issue:other/repo:2",
                "issue-event",
                role="canonical-issue",
            ),
        }
        report = minimal_report(
            state="tracked-elsewhere",
            action="close-as-tracked",
            evidence=[
                evidence_ref("issue:other/repo:2", "issue-event"),
            ],
            related_issues=[
                {
                    "type": "canonical-tracker",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                    "targetRepository": "other/repo",
                }
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_close_as_tracked_rejects_target_repository_mismatch(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:other/repo:2": evidence_record(
                "issue:other/repo:2",
                "issue-event",
                role="canonical-issue",
            ),
        }
        report = minimal_report(
            state="tracked-elsewhere",
            action="close-as-tracked",
            evidence=[
                evidence_ref("issue:other/repo:2", "issue-event"),
            ],
            related_issues=[
                {
                    "type": "canonical-tracker",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                    "targetRepository": "another/repo",
                }
            ],
        )

        with self.assertRaisesRegex(ValidationError, "targetRepository"):
            validate_report(snapshot, report)

    def test_close_as_tracked_rejects_conflicting_canonical_issue_evidence_in_both_orders(self) -> None:
        for evidence_ids in (("issue:2", "issue:3"), ("issue:3", "issue:2")):
            with self.subTest(evidence_ids=evidence_ids):
                snapshot = minimal_snapshot()
                snapshot["evidence"] = {
                    "issue:2": evidence_record("issue:2", "issue-event", role="canonical-issue"),
                    "issue:3": evidence_record("issue:3", "issue-event", role="canonical-issue"),
                }
                report = minimal_report(
                    state="tracked-elsewhere",
                    action="close-as-tracked",
                    evidence=[evidence_ref(evidence_id, "issue-event") for evidence_id in evidence_ids],
                    related_issues=[
                        {
                            "type": "canonical-tracker",
                            "sourceIssueNumber": 1,
                            "targetIssueNumber": 2,
                        }
                    ],
                )

                with self.assertRaisesRegex(ValidationError, "canonical-issue"):
                    validate_report(snapshot, report)

    def test_close_as_tracked_accepts_equivalent_canonical_issue_evidence(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:2": evidence_record("issue:2", "issue-event", role="canonical-issue"),
            "issue:owner/repo:2": evidence_record(
                "issue:owner/repo:2",
                "issue-event",
                role="canonical-issue",
            ),
        }
        report = minimal_report(
            state="tracked-elsewhere",
            action="close-as-tracked",
            evidence=[
                evidence_ref("issue:2", "issue-event"),
                evidence_ref("issue:owner/repo:2", "issue-event"),
            ],
            related_issues=[
                {
                    "type": "canonical-tracker",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_canonical_identity_actions_reject_conflicting_identity_in_blocker_buckets(self) -> None:
        action_cases = (
            (
                "close-as-tracked",
                "tracked-elsewhere",
                (
                    {
                        "type": "canonical-tracker",
                        "sourceIssueNumber": 1,
                        "targetIssueNumber": 2,
                    },
                ),
                (),
            ),
            (
                "merge-duplicate",
                "duplicate",
                (
                    {
                        "type": "exact-duplicate",
                        "sourceIssueNumber": 1,
                        "targetIssueNumber": 2,
                    },
                ),
                (
                    (
                        "issue:1:comment:17",
                        evidence_record(
                            "issue:1:comment:17",
                            "issue-comment",
                            role="deterministic-marker",
                        ),
                    ),
                ),
            ),
        )
        blockers = (
            ("issue:3", evidence_record("issue:3", "issue-event", role="canonical-issue")),
            (
                "issue:other/repo:2",
                evidence_record("issue:other/repo:2", "issue-event", role="canonical-issue"),
            ),
        )

        for action, state, related_issues, additional_support in action_cases:
            for bucket in ("contradictoryEvidence", "missingEvidence"):
                for blocker_id, blocker_record in blockers:
                    with self.subTest(action=action, bucket=bucket, blocker=blocker_id):
                        snapshot = minimal_snapshot()
                        snapshot["evidence"] = {
                            "issue:2": evidence_record("issue:2", "issue-event", role="canonical-issue"),
                            blocker_id: blocker_record,
                        }
                        for evidence_id, record in additional_support:
                            snapshot["evidence"][evidence_id] = record
                        report = minimal_report(
                            state=state,
                            action=action,
                            evidence=[
                                evidence_ref("issue:2", "issue-event"),
                                *(
                                    evidence_ref(evidence_id, record["kind"])
                                    for evidence_id, record in additional_support
                                ),
                            ],
                            contradictory_evidence=[],
                            missing_evidence=[],
                            related_issues=[dict(relationship) for relationship in related_issues],
                        )
                        report["decisions"][0][bucket] = [
                            evidence_ref(blocker_id, blocker_record["kind"]),
                        ]

                        with self.assertRaisesRegex(ValidationError, "canonical-issue"):
                            validate_report(snapshot, report)

    def test_canonical_identity_actions_reject_unresolved_identity_in_blocker_buckets(self) -> None:
        action_cases = (
            (
                "close-as-tracked",
                "tracked-elsewhere",
                (
                    {
                        "type": "canonical-tracker",
                        "sourceIssueNumber": 1,
                        "targetIssueNumber": 2,
                    },
                ),
                (),
            ),
            (
                "merge-duplicate",
                "duplicate",
                (
                    {
                        "type": "exact-duplicate",
                        "sourceIssueNumber": 1,
                        "targetIssueNumber": 2,
                    },
                ),
                (
                    (
                        "issue:1:comment:17",
                        evidence_record(
                            "issue:1:comment:17",
                            "issue-comment",
                            role="deterministic-marker",
                        ),
                    ),
                ),
            ),
        )

        for action, state, related_issues, additional_support in action_cases:
            for bucket in ("contradictoryEvidence", "missingEvidence"):
                with self.subTest(action=action, bucket=bucket):
                    snapshot = minimal_snapshot()
                    snapshot["evidence"] = {
                        "issue:2": evidence_record("issue:2", "issue-event", role="canonical-issue"),
                        "run:88": evidence_record(
                            "run:88",
                            "workflow-run",
                            availability="expired-or-unavailable",
                            role="canonical-issue",
                        ),
                    }
                    for evidence_id, record in additional_support:
                        snapshot["evidence"][evidence_id] = record
                    report = minimal_report(
                        state=state,
                        action=action,
                        evidence=[
                            evidence_ref("issue:2", "issue-event"),
                            *(
                                evidence_ref(evidence_id, record["kind"])
                                for evidence_id, record in additional_support
                            ),
                        ],
                        contradictory_evidence=[],
                        missing_evidence=[],
                        related_issues=[dict(relationship) for relationship in related_issues],
                    )
                    report["decisions"][0][bucket] = [
                        evidence_ref("run:88", "workflow-run"),
                    ]

                    with self.assertRaisesRegex(ValidationError, "canonical-issue"):
                        validate_report(snapshot, report)

    def test_related_issue_target_repository_must_be_owner_repo_when_present(self) -> None:
        snapshot = minimal_snapshot()
        report = minimal_report(
            state="tracked-elsewhere",
            action="close-as-tracked",
            related_issues=[
                {
                    "type": "canonical-tracker",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                    "targetRepository": "not-a-repository",
                }
            ],
        )

        with self.assertRaisesRegex(ValidationError, "targetRepository"):
            validate_report(snapshot, report)

    def test_repository_identity_matching_is_case_insensitive_but_preserves_spelling(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["repository"] = "OWNER/REPO"
        report = minimal_report()
        report["repository"] = "owner/repo"
        report["decisions"][0]["issueUrl"] = "https://github.com/owner/repo/issues/1"

        self.assertIsNone(validate_report(snapshot, report))

    def test_related_issue_self_reference_rejects_case_variant_repository(self) -> None:
        snapshot = minimal_snapshot()
        report = minimal_report(
            related_issues=[
                {
                    "type": "related",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 1,
                    "targetRepository": "OWNER/REPO",
                }
            ],
        )

        with self.assertRaisesRegex(ValidationError, "different issue"):
            validate_report(snapshot, report)

    def test_canonical_actions_accept_case_variant_canonical_and_relationship_repositories(self) -> None:
        action_cases = (
            (
                "close-as-tracked",
                "tracked-elsewhere",
                "canonical-tracker",
                (),
            ),
            (
                "merge-duplicate",
                "duplicate",
                "exact-duplicate",
                (
                    (
                        "issue:1:comment:17",
                        evidence_record(
                            "issue:1:comment:17",
                            "issue-comment",
                            role="deterministic-marker",
                        ),
                    ),
                ),
            ),
        )

        for action, state, relationship_type, additional_support in action_cases:
            with self.subTest(action=action):
                snapshot = minimal_snapshot()
                snapshot["evidence"] = {
                    "issue:OWNER/REPO:2": evidence_record(
                        "issue:OWNER/REPO:2",
                        "issue-event",
                        role="canonical-issue",
                    ),
                }
                for evidence_id, record in additional_support:
                    snapshot["evidence"][evidence_id] = record

                report = minimal_report(
                    state=state,
                    action=action,
                    evidence=[
                        evidence_ref("issue:OWNER/REPO:2", "issue-event"),
                        *(
                            evidence_ref(evidence_id, record["kind"])
                            for evidence_id, record in additional_support
                        ),
                    ],
                    related_issues=[
                        {
                            "type": relationship_type,
                            "sourceIssueNumber": 1,
                            "targetIssueNumber": 2,
                            "targetRepository": "owner/repo",
                        }
                    ],
                )

                self.assertIsNone(validate_report(snapshot, report))

    def test_open_regression_accepts_case_variant_prior_target_and_relationship_repository(self) -> None:
        snapshot, report = high_risk_case("open-regression")
        prior_evidence_id = evidence_id_for_role(snapshot, "prior-resolved-episode")
        snapshot["evidence"]["issue:OWNER/REPO:2"] = snapshot["evidence"].pop(prior_evidence_id)
        report["decisions"][0]["evidence"] = [
            evidence_ref("issue:OWNER/REPO:2", "issue-event")
            if evidence["id"] == prior_evidence_id
            else evidence
            for evidence in report["decisions"][0]["evidence"]
        ]
        report["decisions"][0]["relatedIssues"][0]["targetRepository"] = "owner/repo"

        self.assertIsNone(validate_report(snapshot, report))

    def test_dedicated_issue_actions_require_incident_issue_kind(self) -> None:
        for action in ("close-as-tracked", "open-dedicated-issue"):
            for issue_kind in ("root-cause", "tracker", "transient"):
                with self.subTest(action=action, issue_kind=issue_kind):
                    snapshot, report = high_risk_case(action, issue_kind=issue_kind)

                    with self.assertRaisesRegex(ValidationError, "issueKind.*incident"):
                        validate_report(snapshot, report)

    def test_open_dedicated_issue_requires_recurrence_or_known_flaky_signature(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:46": evidence_record("run:46", "workflow-run", role="current-failing-run"),
            "issue:1": evidence_record(
                "issue:1",
                "issue-event",
                role="canonical-search-complete",
            ),
        }
        report = minimal_report(
            state="actionable",
            action="open-dedicated-issue",
            evidence=[
                evidence_ref("run:46", "workflow-run"),
                evidence_ref("issue:1", "issue-event"),
            ],
        )

        with self.assertRaisesRegex(ValidationError, "known-flaky-signature"):
            validate_report(snapshot, report)

    def test_open_dedicated_issue_requires_current_failing_run(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:47": evidence_record("run:47", "workflow-run", role="recurrence"),
            "issue:1": evidence_record(
                "issue:1",
                "issue-event",
                role="canonical-search-complete",
            ),
        }
        report = minimal_report(
            state="actionable",
            action="open-dedicated-issue",
            evidence=[
                evidence_ref("run:47", "workflow-run"),
                evidence_ref("issue:1", "issue-event"),
            ],
        )

        with self.assertRaisesRegex(ValidationError, "current-failing-run"):
            validate_report(snapshot, report)

    def test_open_dedicated_issue_requires_canonical_search_complete(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:46": evidence_record("run:46", "workflow-run", role="current-failing-run"),
            "run:47": evidence_record("run:47", "workflow-run", role="recurrence"),
        }
        report = minimal_report(
            state="actionable",
            action="open-dedicated-issue",
            evidence=[
                evidence_ref("run:46", "workflow-run"),
                evidence_ref("run:47", "workflow-run"),
            ],
        )

        with self.assertRaisesRegex(ValidationError, "canonical-search-complete"):
            validate_report(snapshot, report)

    def test_valid_open_dedicated_issue_with_recurrence_passes(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:46": evidence_record("run:46", "workflow-run", role="current-failing-run"),
            "run:47": evidence_record("run:47", "workflow-run", role="recurrence"),
            "issue:1": evidence_record(
                "issue:1",
                "issue-event",
                role="canonical-search-complete",
            ),
        }
        report = minimal_report(
            state="actionable",
            action="open-dedicated-issue",
            evidence=[
                evidence_ref("run:46", "workflow-run"),
                evidence_ref("run:47", "workflow-run"),
                evidence_ref("issue:1", "issue-event"),
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_valid_open_dedicated_issue_with_known_flaky_signature_passes(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:46": evidence_record("run:46", "workflow-run", role="current-failing-run"),
            "run:47": evidence_record(
                "run:47",
                "workflow-run",
                role="known-flaky-signature",
            ),
            "issue:1": evidence_record(
                "issue:1",
                "issue-event",
                role="canonical-search-complete",
            ),
        }
        report = minimal_report(
            state="actionable",
            action="open-dedicated-issue",
            evidence=[
                evidence_ref("run:46", "workflow-run"),
                evidence_ref("run:47", "workflow-run"),
                evidence_ref("issue:1", "issue-event"),
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_open_dedicated_issue_rejects_canonical_tracker_relationship_without_canonical_issue_evidence(
        self,
    ) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:46": evidence_record("run:46", "workflow-run", role="current-failing-run"),
            "run:47": evidence_record("run:47", "workflow-run", role="recurrence"),
            "issue:1": evidence_record(
                "issue:1",
                "issue-event",
                role="canonical-search-complete",
            ),
        }
        report = minimal_report(
            state="actionable",
            action="open-dedicated-issue",
            evidence=[
                evidence_ref("run:46", "workflow-run"),
                evidence_ref("run:47", "workflow-run"),
                evidence_ref("issue:1", "issue-event"),
            ],
            related_issues=[
                {
                    "type": "canonical-tracker",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        with self.assertRaisesRegex(ValidationError, "canonical-tracker"):
            validate_report(snapshot, report)

    def test_open_dedicated_issue_rejects_exact_duplicate_relationship_without_canonical_issue_evidence(
        self,
    ) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:46": evidence_record("run:46", "workflow-run", role="current-failing-run"),
            "run:47": evidence_record("run:47", "workflow-run", role="recurrence"),
            "issue:1": evidence_record(
                "issue:1",
                "issue-event",
                role="canonical-search-complete",
            ),
        }
        report = minimal_report(
            state="actionable",
            action="open-dedicated-issue",
            evidence=[
                evidence_ref("run:46", "workflow-run"),
                evidence_ref("run:47", "workflow-run"),
                evidence_ref("issue:1", "issue-event"),
            ],
            related_issues=[
                {
                    "type": "exact-duplicate",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        with self.assertRaisesRegex(ValidationError, "exact-duplicate"):
            validate_report(snapshot, report)

    def test_open_dedicated_issue_allows_related_and_probable_duplicate_relationships(self) -> None:
        for relationship_type in ("related", "probable-duplicate"):
            with self.subTest(relationship_type=relationship_type):
                snapshot = minimal_snapshot()
                snapshot["evidence"] = {
                    "run:46": evidence_record("run:46", "workflow-run", role="current-failing-run"),
                    "run:47": evidence_record("run:47", "workflow-run", role="recurrence"),
                    "issue:1": evidence_record(
                        "issue:1",
                        "issue-event",
                        role="canonical-search-complete",
                    ),
                }
                report = minimal_report(
                    state="actionable",
                    action="open-dedicated-issue",
                    evidence=[
                        evidence_ref("run:46", "workflow-run"),
                        evidence_ref("run:47", "workflow-run"),
                        evidence_ref("issue:1", "issue-event"),
                    ],
                    related_issues=[
                        {
                            "type": relationship_type,
                            "sourceIssueNumber": 1,
                            "targetIssueNumber": 2,
                        }
                    ],
                )

                self.assertIsNone(validate_report(snapshot, report))

    def test_open_dedicated_issue_with_unavailable_search_evidence_is_rejected(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:46": evidence_record("run:46", "workflow-run", role="current-failing-run"),
            "run:47": evidence_record("run:47", "workflow-run", role="recurrence"),
            "issue:1": evidence_record(
                "issue:1",
                "issue-event",
                availability="expired-or-unavailable",
                role="canonical-search-complete",
            ),
        }
        report = minimal_report(
            state="actionable",
            action="open-dedicated-issue",
            evidence=[
                evidence_ref("run:46", "workflow-run"),
                evidence_ref("run:47", "workflow-run"),
                evidence_ref("issue:1", "issue-event"),
            ],
        )

        with self.assertRaisesRegex(ValidationError, "canonical-search-complete"):
            validate_report(snapshot, report)

    def test_open_dedicated_issue_with_unavailable_recurrence_evidence_is_rejected(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:46": evidence_record("run:46", "workflow-run", role="current-failing-run"),
            "run:47": evidence_record(
                "run:47",
                "workflow-run",
                availability="expired-or-unavailable",
                role="recurrence",
            ),
            "issue:1": evidence_record(
                "issue:1",
                "issue-event",
                role="canonical-search-complete",
            ),
        }
        report = minimal_report(
            state="actionable",
            action="open-dedicated-issue",
            evidence=[
                evidence_ref("run:46", "workflow-run"),
                evidence_ref("run:47", "workflow-run"),
                evidence_ref("issue:1", "issue-event"),
            ],
        )

        with self.assertRaisesRegex(ValidationError, "known-flaky-signature"):
            validate_report(snapshot, report)

    def test_open_dedicated_issue_rejects_canonical_issue_evidence_in_any_bucket(self) -> None:
        for bucket in ("evidence", "contradictoryEvidence", "missingEvidence"):
            with self.subTest(bucket=bucket):
                snapshot = minimal_snapshot()
                snapshot["evidence"] = {
                    "run:46": evidence_record("run:46", "workflow-run", role="current-failing-run"),
                    "run:47": evidence_record("run:47", "workflow-run", role="recurrence"),
                    "issue:1": evidence_record(
                        "issue:1",
                        "issue-event",
                        role="canonical-search-complete",
                    ),
                    "issue:2": evidence_record("issue:2", "issue-event", role="canonical-issue"),
                }
                report = minimal_report(
                    state="actionable",
                    action="open-dedicated-issue",
                    evidence=[
                        evidence_ref("run:46", "workflow-run"),
                        evidence_ref("run:47", "workflow-run"),
                        evidence_ref("issue:1", "issue-event"),
                    ],
                    contradictory_evidence=[],
                    missing_evidence=[],
                )
                report["decisions"][0][bucket] = [
                    *report["decisions"][0][bucket],
                    evidence_ref("issue:2", "issue-event"),
                ]

                with self.assertRaisesRegex(ValidationError, "canonical-issue"):
                    validate_report(snapshot, report)

    def test_open_dedicated_issue_requires_supporting_evidence_for_required_roles(self) -> None:
        cases = (
            (
                "current-failing-run",
                "run:46",
                "workflow-run",
                evidence_record("run:46", "workflow-run", role="current-failing-run"),
                "current-failing-run",
            ),
            (
                "canonical-search-complete",
                "issue:1",
                "issue-event",
                evidence_record(
                    "issue:1",
                    "issue-event",
                    role="canonical-search-complete",
                ),
                "canonical-search-complete",
            ),
            (
                "recurrence",
                "run:47",
                "workflow-run",
                evidence_record("run:47", "workflow-run", role="recurrence"),
                "known-flaky-signature",
            ),
        )
        for bucket in ("contradictoryEvidence", "missingEvidence"):
            for role, evidence_id, kind, record, error in cases:
                with self.subTest(bucket=bucket, role=role):
                    snapshot = minimal_snapshot()
                    snapshot["evidence"] = {
                        "run:46": evidence_record("run:46", "workflow-run", role="current-failing-run"),
                        "run:47": evidence_record("run:47", "workflow-run", role="recurrence"),
                        "issue:1": evidence_record(
                            "issue:1",
                            "issue-event",
                            role="canonical-search-complete",
                        ),
                    }
                    snapshot["evidence"][evidence_id] = record
                    report = minimal_report(
                        state="actionable",
                        action="open-dedicated-issue",
                        evidence=[],
                        contradictory_evidence=[],
                        missing_evidence=[],
                    )
                    supporting_ids = {
                        "run:46",
                        "run:47",
                        "issue:1",
                    }
                    supporting_ids.remove(evidence_id)
                    report["decisions"][0]["evidence"] = [
                        evidence_ref(supporting_id, snapshot["evidence"][supporting_id]["kind"])
                        for supporting_id in supporting_ids
                    ]
                    report["decisions"][0][bucket] = [evidence_ref(evidence_id, kind)]

                    with self.assertRaisesRegex(ValidationError, error):
                        validate_report(snapshot, report)

    def test_valid_merge_duplicate_passes(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:2": evidence_record("issue:2", "issue-event", role="canonical-issue"),
            "issue:1:comment:17": evidence_record(
                "issue:1:comment:17",
                "issue-comment",
                role="deterministic-marker",
            ),
        }
        report = minimal_report(
            state="duplicate",
            action="merge-duplicate",
            evidence=[
                evidence_ref("issue:2", "issue-event"),
                evidence_ref("issue:1:comment:17", "issue-comment"),
            ],
            related_issues=[
                {
                    "type": "exact-duplicate",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_valid_merge_duplicate_with_external_target_repository_passes(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:other/repo:2": evidence_record(
                "issue:other/repo:2",
                "issue-event",
                role="canonical-issue",
            ),
            "issue:1:comment:17": evidence_record(
                "issue:1:comment:17",
                "issue-comment",
                role="deterministic-marker",
            ),
        }
        report = minimal_report(
            state="duplicate",
            action="merge-duplicate",
            evidence=[
                evidence_ref("issue:other/repo:2", "issue-event"),
                evidence_ref("issue:1:comment:17", "issue-comment"),
            ],
            related_issues=[
                {
                    "type": "exact-duplicate",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                    "targetRepository": "other/repo",
                }
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_merge_duplicate_rejects_conflicting_canonical_issue_evidence_in_both_orders(self) -> None:
        for evidence_ids in (("issue:2", "issue:3"), ("issue:3", "issue:2")):
            with self.subTest(evidence_ids=evidence_ids):
                snapshot = minimal_snapshot()
                snapshot["evidence"] = {
                    "issue:2": evidence_record("issue:2", "issue-event", role="canonical-issue"),
                    "issue:3": evidence_record("issue:3", "issue-event", role="canonical-issue"),
                    "issue:1:comment:17": evidence_record(
                        "issue:1:comment:17",
                        "issue-comment",
                        role="deterministic-marker",
                    ),
                }
                report = minimal_report(
                    state="duplicate",
                    action="merge-duplicate",
                    evidence=[
                        *(evidence_ref(evidence_id, "issue-event") for evidence_id in evidence_ids),
                        evidence_ref("issue:1:comment:17", "issue-comment"),
                    ],
                    related_issues=[
                        {
                            "type": "exact-duplicate",
                            "sourceIssueNumber": 1,
                            "targetIssueNumber": 2,
                        }
                    ],
                )

                with self.assertRaisesRegex(ValidationError, "canonical-issue"):
                    validate_report(snapshot, report)

    def test_merge_duplicate_accepts_equivalent_canonical_issue_evidence(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:2": evidence_record("issue:2", "issue-event", role="canonical-issue"),
            "issue:owner/repo:2": evidence_record(
                "issue:owner/repo:2",
                "issue-event",
                role="canonical-issue",
            ),
            "issue:1:comment:17": evidence_record(
                "issue:1:comment:17",
                "issue-comment",
                role="deterministic-marker",
            ),
        }
        report = minimal_report(
            state="duplicate",
            action="merge-duplicate",
            evidence=[
                evidence_ref("issue:2", "issue-event"),
                evidence_ref("issue:owner/repo:2", "issue-event"),
                evidence_ref("issue:1:comment:17", "issue-comment"),
            ],
            related_issues=[
                {
                    "type": "exact-duplicate",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_merge_duplicate_requires_supporting_evidence_for_required_roles(self) -> None:
        cases = (
            (
                "canonical-issue",
                "issue:2",
                "issue-event",
                evidence_record("issue:2", "issue-event", role="canonical-issue"),
                "canonical-issue",
            ),
            (
                "deterministic-marker",
                "issue:1:comment:17",
                "issue-comment",
                evidence_record(
                    "issue:1:comment:17",
                    "issue-comment",
                    role="deterministic-marker",
                ),
                "deterministic-marker or normalized-facts",
            ),
        )
        for bucket in ("contradictoryEvidence", "missingEvidence"):
            for role, evidence_id, kind, record, error in cases:
                with self.subTest(bucket=bucket, role=role):
                    snapshot = minimal_snapshot()
                    snapshot["evidence"] = {
                        "issue:2": evidence_record("issue:2", "issue-event", role="canonical-issue"),
                        "issue:1:comment:17": evidence_record(
                            "issue:1:comment:17",
                            "issue-comment",
                            role="deterministic-marker",
                        ),
                    }
                    snapshot["evidence"][evidence_id] = record
                    report = minimal_report(
                        state="duplicate",
                        action="merge-duplicate",
                        evidence=[],
                        contradictory_evidence=[],
                        missing_evidence=[],
                        related_issues=[
                            {
                                "type": "exact-duplicate",
                                "sourceIssueNumber": 1,
                                "targetIssueNumber": 2,
                            }
                        ],
                    )
                    supporting_ids = {
                        "issue:2",
                        "issue:1:comment:17",
                    }
                    supporting_ids.remove(evidence_id)
                    report["decisions"][0]["evidence"] = [
                        evidence_ref(supporting_id, snapshot["evidence"][supporting_id]["kind"])
                        for supporting_id in supporting_ids
                    ]
                    report["decisions"][0][bucket] = [evidence_ref(evidence_id, kind)]

                    with self.assertRaisesRegex(ValidationError, error):
                        validate_report(snapshot, report)

    def test_high_risk_open_regression_requires_supporting_evidence_for_required_roles(self) -> None:
        cases = (
            (
                "current-failing-run",
                "run:42",
                "workflow-run",
                evidence_record(
                    "run:42",
                    "workflow-run",
                    role="current-failing-run",
                    normalizedCause="timeout-on-startup",
                ),
            ),
            (
                "prior-resolved-episode",
                "run:41",
                "workflow-run",
                evidence_record(
                    "run:41",
                    "workflow-run",
                    role="prior-resolved-episode",
                    normalizedCause="timeout-on-startup",
                    priorIssueNumber=2,
                ),
            ),
            (
                "normalized-cause",
                "issue:1:comment:17",
                "issue-comment",
                evidence_record(
                    "issue:1:comment:17",
                    "issue-comment",
                    role="normalized-cause",
                    normalizedCause="timeout-on-startup",
                ),
            ),
        )
        for bucket in ("contradictoryEvidence", "missingEvidence"):
            for role, evidence_id, kind, record in cases:
                with self.subTest(bucket=bucket, role=role):
                    snapshot = minimal_snapshot()
                    snapshot["evidence"] = {
                        "run:42": evidence_record(
                            "run:42",
                            "workflow-run",
                            role="current-failing-run",
                            normalizedCause="timeout-on-startup",
                        ),
                        "run:41": evidence_record(
                            "run:41",
                            "workflow-run",
                            role="prior-resolved-episode",
                            normalizedCause="timeout-on-startup",
                            priorIssueNumber=2,
                        ),
                        "issue:1:comment:17": evidence_record(
                            "issue:1:comment:17",
                            "issue-comment",
                            role="normalized-cause",
                            normalizedCause="timeout-on-startup",
                        ),
                    }
                    snapshot["evidence"][evidence_id] = record
                    report = minimal_report(
                        state="regression",
                        action="open-regression",
                        evidence=[],
                        contradictory_evidence=[],
                        missing_evidence=[],
                        related_issues=[
                            {
                                "type": "regression-of",
                                "sourceIssueNumber": 1,
                                "targetIssueNumber": 2,
                            }
                        ],
                    )
                    supporting_ids = {
                        "run:42",
                        "run:41",
                        "issue:1:comment:17",
                    }
                    supporting_ids.remove(evidence_id)
                    report["decisions"][0]["evidence"] = [
                        evidence_ref(supporting_id, snapshot["evidence"][supporting_id]["kind"])
                        for supporting_id in supporting_ids
                    ]
                    report["decisions"][0][bucket] = [evidence_ref(evidence_id, kind)]

                    with self.assertRaisesRegex(ValidationError, "current-failing-run, prior-resolved-episode, and normalized-cause"):
                        validate_report(snapshot, report)

    def test_duplicate_evidence_ids_are_rejected(self) -> None:
        snapshot = minimal_snapshot()
        evidence_id = "issue:1:comment:17"
        snapshot["evidence"][evidence_id] = evidence_record(
            evidence_id,
            "issue-comment",
        )
        report = minimal_report(
            evidence=[
                evidence_ref(evidence_id, "issue-comment"),
                evidence_ref(evidence_id, "issue-comment"),
            ]
        )

        with self.assertRaisesRegex(ValidationError, "duplicate"):
            validate_report(snapshot, report)

    def test_same_workflow_job_in_supporting_and_contradictory_buckets_is_rejected(self) -> None:
        snapshot = minimal_snapshot()
        evidence_id = "run:42:attempt:1:job:7"
        snapshot["evidence"] = {
            evidence_id: evidence_record(
                evidence_id,
                "workflow-job",
            )
        }
        report = minimal_report(
            evidence=[
                evidence_ref(evidence_id, "workflow-job"),
            ],
            contradictory_evidence=[
                evidence_ref(evidence_id, "workflow-job"),
            ],
        )

        with self.assertRaisesRegex(
            ValidationError,
            r"appears in both evidence and contradictoryEvidence",
        ):
            validate_report(snapshot, report)

    def test_valid_close_with_merged_fix_green_and_no_newer_match_passes(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:1": evidence_record("issue:1", "issue-event", role="merged-fix"),
            "run:42:attempt:1:job:7": evidence_record(
                "run:42:attempt:1:job:7",
                "workflow-job",
                role="post-fix-green",
            ),
            "run:43": evidence_record(
                "run:43",
                "workflow-run",
                role="no-newer-matching-failure",
            ),
        }
        report = minimal_report(
            state="resolved",
            action="close",
            evidence=[
                evidence_ref("issue:1", "issue-event"),
                evidence_ref("run:42:attempt:1:job:7", "workflow-job"),
                evidence_ref("run:43", "workflow-run"),
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_high_risk_open_regression_rejects_arbitrary_target_number_for_run_prior_evidence(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:42": evidence_record(
                "run:42",
                "workflow-run",
                role="current-failing-run",
                normalizedCause="timeout-on-startup",
            ),
            "run:41": evidence_record(
                "run:41",
                "workflow-run",
                role="prior-resolved-episode",
                normalizedCause="timeout-on-startup",
                priorIssueNumber=2,
            ),
            "issue:1:comment:17": evidence_record(
                "issue:1:comment:17",
                "issue-comment",
                role="normalized-cause",
                normalizedCause="timeout-on-startup",
            ),
        }
        report = minimal_report(
            state="regression",
            action="open-regression",
            evidence=[
                evidence_ref("run:42", "workflow-run"),
                evidence_ref("run:41", "workflow-run"),
                evidence_ref("issue:1:comment:17", "issue-comment"),
            ],
            related_issues=[
                {
                    "type": "regression-of",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 3,
                }
            ],
        )

        with self.assertRaisesRegex(ValidationError, "targetIssueNumber"):
            validate_report(snapshot, report)

    def test_high_risk_open_regression_rejects_arbitrary_target_repository_for_run_prior_evidence(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:42": evidence_record(
                "run:42",
                "workflow-run",
                role="current-failing-run",
                normalizedCause="timeout-on-startup",
            ),
            "run:41": evidence_record(
                "run:41",
                "workflow-run",
                role="prior-resolved-episode",
                normalizedCause="timeout-on-startup",
                priorIssueNumber=2,
            ),
            "issue:1:comment:17": evidence_record(
                "issue:1:comment:17",
                "issue-comment",
                role="normalized-cause",
                normalizedCause="timeout-on-startup",
            ),
        }
        report = minimal_report(
            state="regression",
            action="open-regression",
            evidence=[
                evidence_ref("run:42", "workflow-run"),
                evidence_ref("run:41", "workflow-run"),
                evidence_ref("issue:1:comment:17", "issue-comment"),
            ],
            related_issues=[
                {
                    "type": "regression-of",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                    "targetRepository": "other/repo",
                }
            ],
        )

        with self.assertRaisesRegex(ValidationError, "targetRepository"):
            validate_report(snapshot, report)

    def test_high_risk_open_regression_rejects_conflicting_prior_resolved_episode_evidence(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:42": evidence_record(
                "run:42",
                "workflow-run",
                role="current-failing-run",
                normalizedCause="timeout-on-startup",
            ),
            "issue:2": evidence_record(
                "issue:2",
                "issue-event",
                role="prior-resolved-episode",
                normalizedCause="timeout-on-startup",
            ),
            "run:41": evidence_record(
                "run:41",
                "workflow-run",
                role="prior-resolved-episode",
                normalizedCause="timeout-on-startup",
                priorIssueNumber=3,
            ),
            "issue:1:comment:17": evidence_record(
                "issue:1:comment:17",
                "issue-comment",
                role="normalized-cause",
                normalizedCause="timeout-on-startup",
            ),
        }
        report = minimal_report(
            state="regression",
            action="open-regression",
            evidence=[
                evidence_ref("run:42", "workflow-run"),
                evidence_ref("issue:2", "issue-event"),
                evidence_ref("run:41", "workflow-run"),
                evidence_ref("issue:1:comment:17", "issue-comment"),
            ],
            related_issues=[
                {
                    "type": "regression-of",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        with self.assertRaisesRegex(ValidationError, "prior-resolved-episode"):
            validate_report(snapshot, report)

    def test_open_regression_rejects_conflicting_prior_identity_in_blocker_buckets(self) -> None:
        blockers = (
            (
                "issue:3",
                evidence_record(
                    "issue:3",
                    "issue-event",
                    role="prior-resolved-episode",
                    normalizedCause="timeout-on-startup",
                ),
            ),
            (
                "issue:other/repo:2",
                evidence_record(
                    "issue:other/repo:2",
                    "issue-event",
                    role="prior-resolved-episode",
                    normalizedCause="timeout-on-startup",
                ),
            ),
            (
                "run:40",
                evidence_record(
                    "run:40",
                    "workflow-run",
                    role="prior-resolved-episode",
                    normalizedCause="timeout-on-startup",
                    priorIssueNumber=3,
                ),
            ),
        )

        for bucket in ("contradictoryEvidence", "missingEvidence"):
            for blocker_id, blocker_record in blockers:
                with self.subTest(bucket=bucket, blocker=blocker_id):
                    snapshot = minimal_snapshot()
                    snapshot["evidence"] = {
                        "run:42": evidence_record(
                            "run:42",
                            "workflow-run",
                            role="current-failing-run",
                            normalizedCause="timeout-on-startup",
                        ),
                        "run:41": evidence_record(
                            "run:41",
                            "workflow-run",
                            role="prior-resolved-episode",
                            normalizedCause="timeout-on-startup",
                            priorIssueNumber=2,
                        ),
                        "issue:1:comment:17": evidence_record(
                            "issue:1:comment:17",
                            "issue-comment",
                            role="normalized-cause",
                            normalizedCause="timeout-on-startup",
                        ),
                        blocker_id: blocker_record,
                    }
                    report = minimal_report(
                        state="regression",
                        action="open-regression",
                        evidence=[
                            evidence_ref("run:42", "workflow-run"),
                            evidence_ref("run:41", "workflow-run"),
                            evidence_ref("issue:1:comment:17", "issue-comment"),
                        ],
                        contradictory_evidence=[],
                        missing_evidence=[],
                        related_issues=[
                            {
                                "type": "regression-of",
                                "sourceIssueNumber": 1,
                                "targetIssueNumber": 2,
                            }
                        ],
                    )
                    report["decisions"][0][bucket] = [
                        evidence_ref(blocker_id, blocker_record["kind"]),
                    ]

                    with self.assertRaisesRegex(ValidationError, "prior-resolved-episode"):
                        validate_report(snapshot, report)

    def test_open_regression_rejects_unresolved_prior_identity_in_blocker_buckets(self) -> None:
        for bucket in ("contradictoryEvidence", "missingEvidence"):
            with self.subTest(bucket=bucket):
                snapshot = minimal_snapshot()
                snapshot["evidence"] = {
                    "run:42": evidence_record(
                        "run:42",
                        "workflow-run",
                        role="current-failing-run",
                        normalizedCause="timeout-on-startup",
                    ),
                    "run:41": evidence_record(
                        "run:41",
                        "workflow-run",
                        role="prior-resolved-episode",
                        normalizedCause="timeout-on-startup",
                        priorIssueNumber=2,
                    ),
                    "issue:1:comment:17": evidence_record(
                        "issue:1:comment:17",
                        "issue-comment",
                        role="normalized-cause",
                        normalizedCause="timeout-on-startup",
                    ),
                    "run:40": evidence_record(
                        "run:40",
                        "workflow-run",
                        availability="expired-or-unavailable",
                        role="prior-resolved-episode",
                        normalizedCause="timeout-on-startup",
                    ),
                }
                report = minimal_report(
                    state="regression",
                    action="open-regression",
                    evidence=[
                        evidence_ref("run:42", "workflow-run"),
                        evidence_ref("run:41", "workflow-run"),
                        evidence_ref("issue:1:comment:17", "issue-comment"),
                    ],
                    contradictory_evidence=[],
                    missing_evidence=[],
                    related_issues=[
                        {
                            "type": "regression-of",
                            "sourceIssueNumber": 1,
                            "targetIssueNumber": 2,
                        }
                    ],
                )
                report["decisions"][0][bucket] = [
                    evidence_ref("run:40", "workflow-run"),
                ]

                with self.assertRaisesRegex(ValidationError, "prior-resolved-episode"):
                    validate_report(snapshot, report)

    def test_high_risk_open_regression_rejects_run_prior_evidence_without_prior_identity(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:42": evidence_record(
                "run:42",
                "workflow-run",
                role="current-failing-run",
                normalizedCause="timeout-on-startup",
            ),
            "run:41": evidence_record(
                "run:41",
                "workflow-run",
                role="prior-resolved-episode",
                normalizedCause="timeout-on-startup",
            ),
            "issue:1:comment:17": evidence_record(
                "issue:1:comment:17",
                "issue-comment",
                role="normalized-cause",
                normalizedCause="timeout-on-startup",
            ),
        }
        report = minimal_report(
            state="regression",
            action="open-regression",
            evidence=[
                evidence_ref("run:42", "workflow-run"),
                evidence_ref("run:41", "workflow-run"),
                evidence_ref("issue:1:comment:17", "issue-comment"),
            ],
            related_issues=[
                {
                    "type": "regression-of",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        with self.assertRaisesRegex(ValidationError, "priorIssueNumber"):
            validate_report(snapshot, report)

    def test_high_risk_open_regression_rejects_run_prior_evidence_with_invalid_prior_repository(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:42": evidence_record(
                "run:42",
                "workflow-run",
                role="current-failing-run",
                normalizedCause="timeout-on-startup",
            ),
            "run:41": evidence_record(
                "run:41",
                "workflow-run",
                role="prior-resolved-episode",
                normalizedCause="timeout-on-startup",
                priorIssueNumber=2,
                priorRepository="owner:bad/repo",
            ),
            "issue:1:comment:17": evidence_record(
                "issue:1:comment:17",
                "issue-comment",
                role="normalized-cause",
                normalizedCause="timeout-on-startup",
            ),
        }
        report = minimal_report(
            state="regression",
            action="open-regression",
            evidence=[
                evidence_ref("run:42", "workflow-run"),
                evidence_ref("run:41", "workflow-run"),
                evidence_ref("issue:1:comment:17", "issue-comment"),
            ],
            related_issues=[
                {
                    "type": "regression-of",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        with self.assertRaisesRegex(ValidationError, "priorRepository"):
            validate_report(snapshot, report)

    def test_high_risk_open_regression_accepts_valid_local_run_prior_evidence(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:42": evidence_record(
                "run:42",
                "workflow-run",
                role="current-failing-run",
                normalizedCause="timeout-on-startup",
            ),
            "run:41": evidence_record(
                "run:41",
                "workflow-run",
                role="prior-resolved-episode",
                normalizedCause="timeout-on-startup",
                priorIssueNumber=2,
            ),
            "issue:1:comment:17": evidence_record(
                "issue:1:comment:17",
                "issue-comment",
                role="normalized-cause",
                normalizedCause="timeout-on-startup",
            ),
        }
        report = minimal_report(
            state="regression",
            action="open-regression",
            evidence=[
                evidence_ref("run:42", "workflow-run"),
                evidence_ref("run:41", "workflow-run"),
                evidence_ref("issue:1:comment:17", "issue-comment"),
            ],
            related_issues=[
                {
                    "type": "regression-of",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_high_risk_open_regression_accepts_valid_external_run_prior_evidence(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:42": evidence_record(
                "run:42",
                "workflow-run",
                role="current-failing-run",
                normalizedCause="timeout-on-startup",
            ),
            "run:41": evidence_record(
                "run:41",
                "workflow-run",
                role="prior-resolved-episode",
                normalizedCause="timeout-on-startup",
                priorIssueNumber=2,
                priorRepository="other/repo",
            ),
            "issue:1:comment:17": evidence_record(
                "issue:1:comment:17",
                "issue-comment",
                role="normalized-cause",
                normalizedCause="timeout-on-startup",
            ),
        }
        report = minimal_report(
            state="regression",
            action="open-regression",
            evidence=[
                evidence_ref("run:42", "workflow-run"),
                evidence_ref("run:41", "workflow-run"),
                evidence_ref("issue:1:comment:17", "issue-comment"),
            ],
            related_issues=[
                {
                    "type": "regression-of",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                    "targetRepository": "other/repo",
                }
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_valid_close_with_recovery_green_and_no_newer_match_passes(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:42": evidence_record("run:42", "workflow-run", role="recovery"),
            "run:42:attempt:1:job:7": evidence_record(
                "run:42:attempt:1:job:7",
                "workflow-job",
                role="post-fix-green",
            ),
            "run:43": evidence_record(
                "run:43",
                "workflow-run",
                role="no-newer-matching-failure",
            ),
        }
        report = minimal_report(
            state="resolved",
            action="close",
            evidence=[
                evidence_ref("run:42", "workflow-run"),
                evidence_ref("run:42:attempt:1:job:7", "workflow-job"),
                evidence_ref("run:43", "workflow-run"),
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_high_risk_close_with_newer_failure_evidence_is_rejected(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:1": evidence_record("issue:1", "issue-event", role="merged-fix"),
            "run:42:attempt:1:job:7": evidence_record(
                "run:42:attempt:1:job:7",
                "workflow-job",
                role="post-fix-green",
            ),
            "run:43": evidence_record(
                "run:43",
                "workflow-run",
                role="no-newer-matching-failure",
            ),
            "run:42:attempt:1:job:8": evidence_record(
                "run:42:attempt:1:job:8",
                "workflow-job",
                role="newer-failure",
            ),
        }
        report = minimal_report(
            state="resolved",
            action="close",
            evidence=[
                evidence_ref("issue:1", "issue-event"),
                evidence_ref("run:42:attempt:1:job:7", "workflow-job"),
                evidence_ref("run:43", "workflow-run"),
                evidence_ref("run:42:attempt:1:job:8", "workflow-job"),
            ],
        )

        with self.assertRaisesRegex(ValidationError, "newer-failure"):
            validate_report(snapshot, report)

    def test_high_risk_close_with_unavailable_merged_fix_is_rejected(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:1": evidence_record(
                "issue:1",
                "issue-event",
                availability="expired-or-unavailable",
                role="merged-fix",
            ),
            "run:42:attempt:1:job:7": evidence_record(
                "run:42:attempt:1:job:7",
                "workflow-job",
                role="post-fix-green",
            ),
        }
        report = minimal_report(
            state="resolved",
            action="close",
            evidence=[
                evidence_ref("issue:1", "issue-event"),
                evidence_ref("run:42:attempt:1:job:7", "workflow-job"),
            ],
        )

        with self.assertRaisesRegex(ValidationError, "merged-fix or recovery"):
            validate_report(snapshot, report)

    def test_high_risk_close_with_partial_post_fix_green_is_rejected(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:1": evidence_record("issue:1", "issue-event", role="merged-fix"),
            "run:42:attempt:1:job:7": evidence_record(
                "run:42:attempt:1:job:7",
                "workflow-job",
                availability="partial",
                role="post-fix-green",
            ),
        }
        report = minimal_report(
            state="resolved",
            action="close",
            evidence=[
                evidence_ref("issue:1", "issue-event"),
                evidence_ref("run:42:attempt:1:job:7", "workflow-job"),
            ],
        )

        with self.assertRaisesRegex(ValidationError, "post-fix-green"):
            validate_report(snapshot, report)

    def test_merge_duplicate_with_unavailable_canonical_issue_is_rejected(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:2": evidence_record(
                "issue:2",
                "issue-event",
                availability="expired-or-unavailable",
                role="canonical-issue",
            ),
            "issue:1:comment:17": evidence_record(
                "issue:1:comment:17",
                "issue-comment",
                role="deterministic-marker",
            ),
        }
        report = minimal_report(
            state="duplicate",
            action="merge-duplicate",
            evidence=[
                evidence_ref("issue:2", "issue-event"),
                evidence_ref("issue:1:comment:17", "issue-comment"),
            ],
            related_issues=[
                {
                    "type": "exact-duplicate",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        with self.assertRaisesRegex(ValidationError, "canonical-issue"):
            validate_report(snapshot, report)

    def test_merge_duplicate_with_partial_deterministic_marker_is_rejected(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "issue:2": evidence_record("issue:2", "issue-event", role="canonical-issue"),
            "issue:1:comment:17": evidence_record(
                "issue:1:comment:17",
                "issue-comment",
                availability="partial",
                role="deterministic-marker",
            ),
        }
        report = minimal_report(
            state="duplicate",
            action="merge-duplicate",
            evidence=[
                evidence_ref("issue:2", "issue-event"),
                evidence_ref("issue:1:comment:17", "issue-comment"),
            ],
            related_issues=[
                {
                    "type": "exact-duplicate",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        with self.assertRaisesRegex(ValidationError, "deterministic-marker or normalized-facts"):
            validate_report(snapshot, report)

    def test_high_risk_open_regression_with_matching_normalized_causes_passes(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:42": evidence_record(
                "run:42",
                "workflow-run",
                role="current-failing-run",
                normalizedCause="timeout-on-startup",
            ),
            "run:41": evidence_record(
                "run:41",
                "workflow-run",
                role="prior-resolved-episode",
                normalizedCause="timeout-on-startup",
                priorIssueNumber=2,
            ),
            "issue:1:comment:17": evidence_record(
                "issue:1:comment:17",
                "issue-comment",
                role="normalized-cause",
                normalizedCause="timeout-on-startup",
            ),
        }
        report = minimal_report(
            state="regression",
            action="open-regression",
            evidence=[
                evidence_ref("run:42", "workflow-run"),
                evidence_ref("run:41", "workflow-run"),
                evidence_ref("issue:1:comment:17", "issue-comment"),
            ],
            related_issues=[
                {
                    "type": "regression-of",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_open_regression_uses_report_reference_normalized_cause_for_raw_snapshot_records(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:42": evidence_record(
                "run:42",
                "workflow-run",
                sourceIssueNumber=1,
                facts=[{"field": "exceptionType", "normalized": "timeouterror"}],
            ),
            "issue:2": evidence_record(
                "issue:2",
                "issue-event",
                number=2,
                facts=[{"field": "exceptionType", "normalized": "timeouterror"}],
                **scoped_by_reference(1),
            ),
            "issue:1:comment:17": evidence_record(
                "issue:1:comment:17",
                "issue-comment",
                sourceIssueNumber=1,
                facts=[{"field": "exceptionType", "normalized": "timeouterror"}],
            ),
        }
        report = minimal_report(
            state="regression",
            action="open-regression",
            evidence=[
                evidence_ref(
                    "run:42",
                    "workflow-run",
                    role="current-failing-run",
                    normalized_cause="timeout-on-startup",
                ),
                evidence_ref(
                    "issue:2",
                    "issue-event",
                    role="prior-resolved-episode",
                    normalized_cause="timeout-on-startup",
                ),
                evidence_ref(
                    "issue:1:comment:17",
                    "issue-comment",
                    role="normalized-cause",
                    normalized_cause="timeout-on-startup",
                ),
            ],
            related_issues=[
                {
                    "type": "regression-of",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        self.assertIsNone(validate_report(snapshot, report))

    def test_report_normalized_cause_must_not_conflict_with_snapshot_normalized_cause(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"]["run:42"] = evidence_record(
            "run:42",
            "workflow-run",
            sourceIssueNumber=1,
            normalizedCause="timeout-on-startup",
        )
        report = minimal_report(
            evidence=[
                evidence_ref("issue:1", "issue-event"),
                evidence_ref(
                    "run:42",
                    "workflow-run",
                    role="current-failing-run",
                    normalized_cause="cache-miss",
                ),
            ],
        )

        with self.assertRaisesRegex(
            ValidationError,
            r"run:42.*report normalizedCause cache-miss.*snapshot normalizedCause timeout-on-startup",
        ):
            validate_report(snapshot, report)

    def test_high_risk_open_regression_with_mismatching_normalized_causes_is_rejected(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:42": evidence_record(
                "run:42",
                "workflow-run",
                role="current-failing-run",
                normalizedCause="timeout-on-startup",
            ),
            "run:43": evidence_record(
                "run:43",
                "workflow-run",
                role="current-failing-run",
                normalizedCause="timeout-on-startup",
            ),
            "run:41": evidence_record(
                "run:41",
                "workflow-run",
                role="prior-resolved-episode",
                normalizedCause="timeout-on-startup",
                priorIssueNumber=2,
            ),
            "issue:1:comment:17": evidence_record(
                "issue:1:comment:17",
                "issue-comment",
                role="normalized-cause",
                normalizedCause="cache-miss",
            ),
        }
        report = minimal_report(
            state="regression",
            action="open-regression",
            evidence=[
                evidence_ref("run:42", "workflow-run"),
                evidence_ref("run:43", "workflow-run"),
                evidence_ref("run:41", "workflow-run"),
                evidence_ref("issue:1:comment:17", "issue-comment"),
            ],
            related_issues=[
                {
                    "type": "regression-of",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        with self.assertRaisesRegex(ValidationError, "normalizedCause"):
            validate_report(snapshot, report)

    def test_high_risk_open_regression_with_unavailable_current_failing_run_is_rejected(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:42": evidence_record(
                "run:42",
                "workflow-run",
                availability="expired-or-unavailable",
                role="current-failing-run",
                normalizedCause="timeout-on-startup",
            ),
            "run:41": evidence_record(
                "run:41",
                "workflow-run",
                role="prior-resolved-episode",
                normalizedCause="timeout-on-startup",
                priorIssueNumber=2,
            ),
            "issue:1:comment:17": evidence_record(
                "issue:1:comment:17",
                "issue-comment",
                role="normalized-cause",
                normalizedCause="timeout-on-startup",
            ),
        }
        report = minimal_report(
            state="regression",
            action="open-regression",
            evidence=[
                evidence_ref("run:42", "workflow-run"),
                evidence_ref("run:41", "workflow-run"),
                evidence_ref("issue:1:comment:17", "issue-comment"),
            ],
            related_issues=[
                {
                    "type": "regression-of",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        with self.assertRaisesRegex(ValidationError, "current-failing-run, prior-resolved-episode, and normalized-cause"):
            validate_report(snapshot, report)

    def test_high_risk_open_regression_with_partial_prior_resolved_episode_is_rejected(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:42": evidence_record(
                "run:42",
                "workflow-run",
                role="current-failing-run",
                normalizedCause="timeout-on-startup",
            ),
            "run:41": evidence_record(
                "run:41",
                "workflow-run",
                availability="partial",
                role="prior-resolved-episode",
                normalizedCause="timeout-on-startup",
                priorIssueNumber=2,
            ),
            "issue:1:comment:17": evidence_record(
                "issue:1:comment:17",
                "issue-comment",
                role="normalized-cause",
                normalizedCause="timeout-on-startup",
            ),
        }
        report = minimal_report(
            state="regression",
            action="open-regression",
            evidence=[
                evidence_ref("run:42", "workflow-run"),
                evidence_ref("run:41", "workflow-run"),
                evidence_ref("issue:1:comment:17", "issue-comment"),
            ],
            related_issues=[
                {
                    "type": "regression-of",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        with self.assertRaisesRegex(ValidationError, "current-failing-run, prior-resolved-episode, and normalized-cause"):
            validate_report(snapshot, report)

    def test_high_risk_open_regression_with_unavailable_normalized_cause_is_rejected(self) -> None:
        snapshot = minimal_snapshot()
        snapshot["evidence"] = {
            "run:42": evidence_record(
                "run:42",
                "workflow-run",
                role="current-failing-run",
                normalizedCause="timeout-on-startup",
            ),
            "run:41": evidence_record(
                "run:41",
                "workflow-run",
                role="prior-resolved-episode",
                normalizedCause="timeout-on-startup",
                priorIssueNumber=2,
            ),
            "issue:1:comment:17": evidence_record(
                "issue:1:comment:17",
                "issue-comment",
                availability="expired-or-unavailable",
                role="normalized-cause",
                normalizedCause="timeout-on-startup",
            ),
        }
        report = minimal_report(
            state="regression",
            action="open-regression",
            evidence=[
                evidence_ref("run:42", "workflow-run"),
                evidence_ref("run:41", "workflow-run"),
                evidence_ref("issue:1:comment:17", "issue-comment"),
            ],
            related_issues=[
                {
                    "type": "regression-of",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )

        with self.assertRaisesRegex(ValidationError, "current-failing-run, prior-resolved-episode, and normalized-cause"):
            validate_report(snapshot, report)


class EvidenceRequestModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "collectedAt": "2026-08-17T21:24:23Z",
            "openIssues": [1],
            "evidence": {
                "issue:1": evidence_record(
                    "issue:1",
                    "issue-event",
                    number=1,
                    state="open",
                    facts=[
                        {
                            "field": "testName",
                            "raw": "Namespace.Tests.Fails",
                            "normalized": "namespace.tests.fails",
                            "method": "labelled-line",
                            "sourceEvidenceId": "issue:1",
                        }
                    ],
                ),
                "issue:2": evidence_record(
                    "issue:2",
                    "issue-event",
                    availability="not-enriched",
                    number=2,
                    targetRepository="owner/repo",
                    supportingBudgetExcluded=True,
                    referencedBy=[self._association()],
                ),
                "run:42": evidence_record(
                    "run:42",
                    "workflow-run",
                    availability="partial",
                    runId=42,
                    targetRepository="owner/repo",
                    referencedBy=[self._association()],
                ),
                "pr:3": evidence_record(
                    "pr:3",
                    "pull-request",
                    availability="partial",
                    number=3,
                    targetRepository="owner/repo",
                    files=[{"path": "src/Only.cs", "status": "modified"}],
                    referencedBy=[self._association()],
                ),
            },
            "collectionErrors": [],
        }

    @staticmethod
    def _association() -> dict[str, object]:
        return {
            "sourceIssueNumber": 1,
            "sourceEvidenceId": "issue:1",
            "sourceUrl": "https://github.com/owner/repo/issues/1",
            "extractionMethod": "issue-body",
        }

    @staticmethod
    def _document(*requests: dict[str, object], round_number: int = 1) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "round": round_number,
            "requests": list(requests),
        }

    @staticmethod
    def _request(request_type: str, evidence_id: str, **extra: object) -> dict[str, object]:
        request = {
            "type": request_type,
            "sourceIssueNumber": 1,
            "evidenceId": evidence_id,
            "decisionGate": "merged-fix",
            "reason": "Collect evidence that can change the action.",
        }
        request.update(extra)
        return request

    def test_request_constants_are_finite(self) -> None:
        self.assertEqual(
            {
                "issue-reference",
                "workflow-run",
                "canonical-search",
                "source-check",
            },
            EVIDENCE_REQUEST_TYPES,
        )
        self.assertEqual(
            {
                "merged-fix",
                "recovery",
                "post-fix-green",
                "no-newer-matching-failure",
                "no-recent-matching-failure",
                "canonical-issue",
                "canonical-search-complete",
                "obsolete-surface",
                "current-failing-run",
                "prior-resolved-episode",
            },
            EVIDENCE_REQUEST_DECISION_GATES,
        )

    def test_requests_are_normalized_and_sorted_with_grounded_fact_and_path(self) -> None:
        document = self._document(
            self._request("source-check", "pr:3", decisionGate="obsolete-surface"),
            self._request(
                "canonical-search",
                "issue:1",
                factField="testName",
                decisionGate="canonical-search-complete",
            ),
            self._request("workflow-run", "run:42", decisionGate="post-fix-green"),
            self._request("issue-reference", "issue:2"),
        )

        normalized = validate_evidence_requests(self.snapshot, document)

        self.assertEqual(
            [
                "canonical-search",
                "issue-reference",
                "source-check",
                "workflow-run",
            ],
            [request["type"] for request in normalized],
        )
        self.assertEqual("Namespace.Tests.Fails", normalized[0]["factValue"])
        self.assertEqual("namespace.tests.fails", normalized[0]["factNormalized"])
        self.assertEqual("src/Only.cs", normalized[2]["path"])

    def test_request_document_rejects_schema_repository_round_and_unknown_fields(self) -> None:
        valid = self._document(self._request("issue-reference", "issue:2"))
        mutations = (
            ("schema", {**valid, "schemaVersion": 2}, "schemaVersion"),
            ("repository", {**valid, "repository": "other/repo"}, "repository"),
            ("round-zero", {**valid, "round": 0}, "round"),
            ("round-three", {**valid, "round": 3}, "round"),
            ("unknown-root", {**valid, "endpoint": "/repos/owner/repo"}, "unknown"),
        )
        for name, document, message in mutations:
            with self.subTest(name=name), self.assertRaisesRegex(ValidationError, message):
                validate_evidence_requests(self.snapshot, document)

    def test_round_two_is_rejected_after_two_expansion_manifests(self) -> None:
        snapshot = dict(self.snapshot)
        snapshot["expansions"] = [
            {"round": 1, "requests": [], "status": "complete", "errors": []},
            {"round": 2, "requests": [], "status": "complete", "errors": []},
        ]

        with self.assertRaisesRegex(ValidationError, "At most two adaptive"):
            validate_evidence_requests(
                snapshot,
                self._document(
                    self._request("issue-reference", "issue:2"),
                    round_number=2,
                ),
            )

    def test_round_two_is_rejected_when_expansion_history_starts_at_round_two(self) -> None:
        snapshot = dict(self.snapshot)
        snapshot["expansions"] = [
            {"round": 2, "requests": [], "status": "complete", "errors": []},
        ]

        with self.assertRaisesRegex(ValidationError, "sequential"):
            validate_evidence_requests(
                snapshot,
                self._document(
                    self._request("issue-reference", "issue:2"),
                    round_number=2,
                ),
            )

    def test_request_rejects_unknown_query_url_method_body_and_mutation_fields(self) -> None:
        forbidden_fields = (
            ("url", "https://example.invalid"),
            ("endpoint", "/repos/owner/repo"),
            ("method", "POST"),
            ("repository", "other/repo"),
            ("query", "is:issue arbitrary"),
            ("search", "arbitrary"),
            ("body", {"state": "closed"}),
            ("labels", ["closed"]),
            ("state", "closed"),
        )
        for field, value in forbidden_fields:
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(ValidationError, "unknown|forbidden"),
            ):
                validate_evidence_requests(
                    self.snapshot,
                    self._document(
                        self._request("issue-reference", "issue:2", **{field: value})
                    ),
                )

    def test_request_limits_and_duplicates_are_rejected(self) -> None:
        duplicate = self._request("issue-reference", "issue:2")
        with self.assertRaisesRegex(ValidationError, "duplicate"):
            validate_evidence_requests(
                self.snapshot,
                self._document(duplicate, dict(duplicate)),
            )

        snapshot = dict(self.snapshot)
        snapshot["evidence"] = dict(self.snapshot["evidence"])
        snapshot["evidence"]["issue:4"] = evidence_record(
            "issue:4",
            "issue-event",
            availability="partial",
            number=4,
            referencedBy=[self._association()],
        )
        snapshot["evidence"]["run:43"] = evidence_record(
            "run:43",
            "workflow-run",
            availability="partial",
            runId=43,
            targetRepository="owner/repo",
            referencedBy=[self._association()],
        )
        five_distinct = [
            self._request("issue-reference", "issue:2"),
            self._request("issue-reference", "issue:4"),
            self._request("workflow-run", "run:42"),
            self._request("source-check", "pr:3", decisionGate="obsolete-surface"),
            self._request(
                "canonical-search",
                "issue:1",
                factField="testName",
                decisionGate="canonical-search-complete",
            ),
        ]
        with self.assertRaisesRegex(ValidationError, "source issue.*five"):
            validate_evidence_requests(
                snapshot,
                self._document(
                    *five_distinct,
                    self._request("workflow-run", "run:43"),
                ),
            )

        snapshot = dict(self.snapshot)
        evidence = dict(self.snapshot["evidence"])
        open_issues = list(range(1, 27))
        for issue_number in open_issues[1:]:
            evidence[f"issue:{issue_number}"] = evidence_record(
                f"issue:{issue_number}",
                "issue-event",
                number=issue_number,
                state="open",
            )
            evidence[f"run:{100 + issue_number}"] = evidence_record(
                f"run:{100 + issue_number}",
                "workflow-run",
                availability="partial",
                runId=100 + issue_number,
                targetRepository="owner/repo",
                sourceIssueNumber=issue_number,
            )
        snapshot["openIssues"] = open_issues
        snapshot["evidence"] = evidence
        requests = [
            {
                **self._request("workflow-run", f"run:{100 + issue_number}"),
                "sourceIssueNumber": issue_number,
            }
            for issue_number in open_issues
        ]
        with self.assertRaisesRegex(ValidationError, "25"):
            validate_evidence_requests(snapshot, self._document(*requests))

    def test_canonical_search_limit_is_rejected(self) -> None:
        snapshot = dict(self.snapshot)
        evidence = dict(self.snapshot["evidence"])
        open_issues = list(range(1, 12))
        for issue_number in open_issues[1:]:
            evidence[f"issue:{issue_number}"] = evidence_record(
                f"issue:{issue_number}",
                "issue-event",
                number=issue_number,
                state="open",
                facts=[
                    {
                        "field": "testName",
                        "raw": f"Test{issue_number}",
                        "normalized": f"test{issue_number}",
                    }
                ],
            )
        snapshot["openIssues"] = open_issues
        snapshot["evidence"] = evidence
        requests = [
            {
                **self._request(
                    "canonical-search",
                    f"issue:{issue_number}",
                    factField="testName",
                    decisionGate="canonical-search-complete",
                ),
                "sourceIssueNumber": issue_number,
            }
            for issue_number in open_issues
        ]

        with self.assertRaisesRegex(ValidationError, "10 canonical"):
            validate_evidence_requests(snapshot, self._document(*requests))

    def test_request_requires_open_source_and_scoped_existing_evidence(self) -> None:
        closed_source = dict(self.snapshot)
        closed_source["openIssues"] = []
        with self.assertRaisesRegex(ValidationError, "open"):
            validate_evidence_requests(
                closed_source,
                self._document(self._request("issue-reference", "issue:2")),
            )

        with self.assertRaisesRegex(ValidationError, "unknown evidence"):
            validate_evidence_requests(
                self.snapshot,
                self._document(self._request("issue-reference", "issue:999")),
            )

        unscoped = dict(self.snapshot)
        unscoped["evidence"] = dict(self.snapshot["evidence"])
        unscoped["evidence"]["issue:2"] = evidence_record(
            "issue:2",
            "issue-event",
            availability="partial",
            number=2,
        )
        with self.assertRaisesRegex(ValidationError, "scoped"):
            validate_evidence_requests(
                unscoped,
                self._document(self._request("issue-reference", "issue:2")),
            )

    def test_issue_and_run_requests_reject_already_available_detail(self) -> None:
        snapshot = dict(self.snapshot)
        snapshot["evidence"] = dict(self.snapshot["evidence"])
        snapshot["evidence"]["issue:2"] = evidence_record(
            "issue:2",
            "issue-event",
            number=2,
            state="closed",
            title="Already enriched",
            supportingBudgetExcluded=True,
            referencedBy=[self._association()],
        )
        snapshot["evidence"]["run:42"] = evidence_record(
            "run:42",
            "workflow-run",
            runId=42,
            targetRepository="owner/repo",
            status="completed",
            conclusion="failure",
            jobs=[],
            runBudgetExcluded=True,
            referencedBy=[self._association()],
        )
        for request_type, evidence_id in (
            ("issue-reference", "issue:2"),
            ("workflow-run", "run:42"),
        ):
            with (
                self.subTest(request_type=request_type),
                self.assertRaisesRegex(ValidationError, "already.*available|enriched"),
            ):
                validate_evidence_requests(
                    snapshot,
                    self._document(self._request(request_type, evidence_id)),
                )

    def test_workflow_request_requires_grounded_repository_identity(self) -> None:
        snapshot = dict(self.snapshot)
        snapshot["evidence"] = dict(self.snapshot["evidence"])
        snapshot["evidence"]["run:42"] = evidence_record(
            "run:42",
            "workflow-run",
            availability="partial",
            runId=42,
            targetRepository="owner/repo?method=POST",
            referencedBy=[self._association()],
        )

        with self.assertRaisesRegex(ValidationError, "target repository"):
            validate_evidence_requests(
                snapshot,
                self._document(self._request("workflow-run", "run:42")),
            )

    def test_workflow_request_defaults_missing_target_repository_to_snapshot(self) -> None:
        snapshot = dict(self.snapshot)
        snapshot["evidence"] = dict(self.snapshot["evidence"])
        snapshot["evidence"]["run:42"] = evidence_record(
            "run:42",
            "workflow-run",
            availability="partial",
            runId=42,
            referencedBy=[self._association()],
        )

        normalized = validate_evidence_requests(
            snapshot,
            self._document(self._request("workflow-run", "run:42")),
        )

        self.assertEqual("run:42", normalized[0]["evidenceId"])

    def test_canonical_search_derives_collector_fact_and_rejects_invented_or_ambiguous_fact(self) -> None:
        normalized = validate_evidence_requests(
            self.snapshot,
            self._document(
                self._request(
                    "canonical-search",
                    "issue:1",
                    factField="testName",
                    decisionGate="canonical-search-complete",
                )
            ),
        )
        self.assertEqual("Namespace.Tests.Fails", normalized[0]["factValue"])
        self.assertNotIn("role", self.snapshot["evidence"]["issue:1"]["payload"])

        with self.assertRaisesRegex(ValidationError, "fact"):
            validate_evidence_requests(
                self.snapshot,
                self._document(
                    self._request(
                        "canonical-search",
                        "issue:1",
                        factField="invented",
                        decisionGate="canonical-search-complete",
                    )
                ),
            )

        ambiguous = dict(self.snapshot)
        ambiguous["evidence"] = dict(self.snapshot["evidence"])
        ambiguous["evidence"]["issue:1"] = evidence_record(
            "issue:1",
            "issue-event",
            number=1,
            state="open",
            facts=[
                {"field": "testName", "raw": "One", "normalized": "one"},
                {"field": "testName", "raw": "Two", "normalized": "two"},
            ],
        )
        with self.assertRaisesRegex(ValidationError, "ambiguous"):
            validate_evidence_requests(
                ambiguous,
                self._document(
                    self._request(
                        "canonical-search",
                        "issue:1",
                        factField="testName",
                        decisionGate="canonical-search-complete",
                    )
                ),
            )

    def test_canonical_search_requires_own_available_issue_event(self) -> None:
        for evidence_id, availability in (("issue:2", "not-enriched"), ("issue:1", "partial")):
            snapshot = dict(self.snapshot)
            snapshot["evidence"] = dict(self.snapshot["evidence"])
            snapshot["evidence"][evidence_id] = dict(snapshot["evidence"][evidence_id])
            snapshot["evidence"][evidence_id]["availability"] = availability
            request = self._request(
                "canonical-search",
                evidence_id,
                factField="testName",
                decisionGate="canonical-search-complete",
            )
            with (
                self.subTest(evidence_id=evidence_id, availability=availability),
                self.assertRaisesRegex(ValidationError, "own available issue"),
            ):
                validate_evidence_requests(snapshot, self._document(request))

    def test_source_check_derives_exactly_one_safe_path_and_rejects_request_path(self) -> None:
        normalized = validate_evidence_requests(
            self.snapshot,
            self._document(
                self._request("source-check", "pr:3", decisionGate="obsolete-surface")
            ),
        )
        self.assertEqual("src/Only.cs", normalized[0]["path"])

        with self.assertRaisesRegex(ValidationError, "unknown|forbidden"):
            validate_evidence_requests(
                self.snapshot,
                self._document(
                    self._request(
                        "source-check",
                        "pr:3",
                        decisionGate="obsolete-surface",
                        path="src/Other.cs",
                    )
                ),
            )

        ambiguous = dict(self.snapshot)
        ambiguous["evidence"] = dict(self.snapshot["evidence"])
        ambiguous["evidence"]["pr:3"] = evidence_record(
            "pr:3",
            "pull-request",
            availability="partial",
            number=3,
            targetRepository="owner/repo",
            files=[
                {"path": "src/One.cs", "status": "modified"},
                {"path": "src/Two.cs", "status": "modified"},
            ],
            referencedBy=[self._association()],
        )
        with self.assertRaisesRegex(ValidationError, "exactly one"):
            validate_evidence_requests(
                ambiguous,
                self._document(
                    self._request(
                        "source-check",
                        "pr:3",
                        decisionGate="obsolete-surface",
                    )
                ),
            )

        for unsafe_path in (".", "../escape", "/absolute", "src/\nname"):
            unsafe = dict(self.snapshot)
            unsafe["evidence"] = dict(self.snapshot["evidence"])
            unsafe["evidence"]["pr:3"] = evidence_record(
                "pr:3",
                "pull-request",
                availability="partial",
                number=3,
                targetRepository="owner/repo",
                files=[{"path": unsafe_path, "status": "modified"}],
                referencedBy=[self._association()],
            )
            with (
                self.subTest(unsafe_path=unsafe_path),
                self.assertRaisesRegex(ValidationError, "safe repository-relative"),
            ):
                validate_evidence_requests(
                    unsafe,
                    self._document(
                        self._request(
                            "source-check",
                            "pr:3",
                            decisionGate="obsolete-surface",
                        )
                    ),
                )


if __name__ == "__main__":
    unittest.main()
