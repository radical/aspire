from __future__ import annotations

import copy
import json
import unittest

from ci_shepherd.lifecycle import prepare_assessment
from ci_shepherd.models import ValidationError
from ci_shepherd.poc import (
    build_compact_poc_input,
    merge_ambiguous_poc_judgments,
    validate_poc_judgments,
    validate_poc_projectability,
)
from ci_shepherd.poc_history import compute_fingerprint


COLLECTED_AT = "2026-08-17T21:24:23Z"
REPOSITORY = "owner/repo"


def _snapshot(issue_number: int = 101) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "repository": REPOSITORY,
        "collectedAt": COLLECTED_AT,
        "openIssues": [issue_number],
        "issues": [
            {
                "number": issue_number,
                "state": "open",
                "title": f"Issue {issue_number}",
                "url": f"https://github.com/{REPOSITORY}/issues/{issue_number}",
                "createdAt": COLLECTED_AT,
                "updatedAt": COLLECTED_AT,
                "closedAt": None,
                "labels": ["ci-failure-cause"],
                "producer": "ci-failure-cause",
                "autoclose": None,
                "ledger": {
                    "source": "body-table",
                    "schema": "occurrences-v1",
                    "schemaRecognized": True,
                    "sourceRecordCount": 1,
                    "parsedRowCount": 1,
                    "complete": True,
                    "rows": [
                        {
                            "date": "2026-08-17",
                            "sourceRun": 101,
                            "runUrl": f"https://github.com/{REPOSITORY}/actions/runs/101",
                            "job": "Tests",
                            "pullRequest": issue_number,
                        }
                    ],
                },
                "episodes": [{"openedAt": COLLECTED_AT, "closedAt": None}],
                "episodesComplete": False,
                "facts": [],
            }
        ],
        "supportingIssues": [],
        "evidence": {
            f"issue:{issue_number}": {
                "kind": "issue-event",
                "url": f"https://github.com/{REPOSITORY}/issues/{issue_number}",
                "collectedAt": COLLECTED_AT,
                "availability": "available",
                "payload": {
                    "number": issue_number,
                    "state": "open",
                    "title": f"Issue {issue_number}",
                    "url": f"https://github.com/{REPOSITORY}/issues/{issue_number}",
                    "createdAt": COLLECTED_AT,
                    "updatedAt": COLLECTED_AT,
                    "closedAt": None,
                    "labels": ["ci-failure-cause"],
                    "producer": "ci-failure-cause",
                    "autoclose": None,
                    "ledger": {
                        "source": "body-table",
                        "schema": "occurrences-v1",
                        "schemaRecognized": True,
                        "sourceRecordCount": 1,
                        "parsedRowCount": 1,
                        "complete": True,
                        "rows": [
                            {
                                "date": "2026-08-17",
                                "sourceRun": 101,
                                "runUrl": f"https://github.com/{REPOSITORY}/actions/runs/101",
                                "job": "Tests",
                                "pullRequest": issue_number,
                            }
                        ],
                    },
                    "episodes": [{"openedAt": COLLECTED_AT, "closedAt": None}],
                    "episodesComplete": False,
                    "facts": [],
                },
            }
        },
        "collectionErrors": [],
        "warnings": [],
        "references": {},
    }


def _prepared(issue_number: int = 101) -> dict[str, object]:
    return prepare_assessment(_snapshot(issue_number))


def _judgment(issue_number: int = 101) -> dict[str, object]:
    prepared = _prepared(issue_number)
    return {
        "schemaVersion": 1,
        "snapshotId": prepared["snapshotId"],
        "issues": [
            {
                "issueNumber": issue_number,
                "category": "flaky-test",
                "recommendations": [
                    {
                        "disposition": "review-quarantine",
                        "target": {"kind": "issue", "value": issue_number},
                        "confidence": "high",
                        "summary": "Quarantine the test until the flake is understood.",
                        "evidenceIds": [f"issue:{issue_number}"],
                        "missingEvidence": [],
                        "reassessWhen": "After three clean rolling builds.",
                    }
                ],
            }
        ],
    }


def _compact_issue(
    issue_number: int,
    *,
    title: str = "Issue",
    producer: str = "ci-failure-cause",
    autoclose: bool | None = None,
    parsed_row_count: int = 1,
    tier1_cause_id: str | None = None,
    tier2_exception_type: str | None = None,
    tier2_test_name: str | None = "Namespace.Type.Test",
    tier3_error_code: str | None = None,
    candidate_state: str = "resolved",
    candidate_action: str = "recommend-close",
    blockers: list[str] | None = None,
    missing_prerequisites: list[str] | None = None,
    resolution_evidence: dict[str, object] | None = None,
    ledger_rows: list[dict[str, object]] | None = None,
    bundle_size: int = 9,
    payload_size: int = 0,
    issue_body: str | None = None,
    labels: list[str] | None = None,
    markers: list[dict[str, object]] | None = None,
    author: str | None = None,
    run_payload: dict[str, object] | None = None,
    run_availability: str = "available",
    pr_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    evidence_bundle: list[dict[str, object]] = []
    bundle_specs = [
        (f"issue:{issue_number}", "issue-event"),
        (f"pr:{issue_number}", "pull-request"),
        (f"run:{issue_number}", "workflow-run"),
    ]
    extra_kinds = ("issue-comment", "workflow-log", "commit", "source-path", "workflow-annotation")
    for index in range(max(0, bundle_size - len(bundle_specs))):
        kind = extra_kinds[index % len(extra_kinds)]
        bundle_specs.append((f"{kind}:{issue_number}:{index}", kind))

    for evidence_id, kind in bundle_specs:
        payload: dict[str, object] = {
            "noise": "x" * payload_size,
            "kind": kind,
        }
        if kind == "issue-event":
            payload.update(
                {
                    "title": title,
                    "body": issue_body,
                    "labels": labels or [],
                    "markers": markers or [],
                    "author": author,
                    "state": "open",
                    "createdAt": "2026-08-17T20:00:00Z",
                    "updatedAt": "2026-08-17T21:00:00Z",
                }
            )
        if kind == "workflow-run":
            payload.update({"runId": issue_number, "conclusion": "success"})
            if run_payload:
                payload.update(run_payload)
        elif kind == "pull-request":
            payload.update({"number": issue_number, "mergedAt": "2026-08-17T21:24:23Z"})
            if pr_payload:
                payload.update(pr_payload)
        else:
            payload.update({"number": issue_number})
        evidence_bundle.append(
            {
                "id": evidence_id,
                "kind": kind,
                "availability": (
                    run_availability if kind == "workflow-run" else "available"
                ),
                "payload": payload,
            }
        )

    return {
        "issueNumber": issue_number,
        "title": title,
        "producer": producer,
        "autoclose": autoclose,
        "ledger": {
            "parsedRowCount": parsed_row_count,
            "complete": True,
            "schemaRecognized": True,
            "rows": ledger_rows or [
                {
                    "date": "2026-08-17",
                    "sourceRun": issue_number,
                    "job": "Tests",
                    "pullRequest": issue_number,
                }
            ][:parsed_row_count],
        },
        "identity": {
            "tier1CauseId": tier1_cause_id,
            "tier2TestName": tier2_test_name,
            "tier2ExceptionType": tier2_exception_type,
            "tier3ErrorCode": tier3_error_code,
            "tier3Job": None,
        },
        "candidateState": candidate_state,
        "candidateAction": candidate_action,
        "blockers": blockers or [],
        "missingPrerequisites": missing_prerequisites or [],
        "resolutionEvidence": resolution_evidence or {},
        "evidenceBundle": evidence_bundle,
    }


def _compact_prepared(issues: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "repository": REPOSITORY,
        "sourceCollectedAt": COLLECTED_AT,
        "snapshotId": f"snapshot:{REPOSITORY}:{COLLECTED_AT}",
        "issues": issues,
    }


def _category_and_disposition(judgment: dict[str, object]) -> tuple[str, str]:
    recommendations = judgment["recommendations"]
    assert isinstance(recommendations, list)
    recommendation = recommendations[0]
    assert isinstance(recommendation, dict)
    category = judgment["category"]
    disposition = recommendation["disposition"]
    assert isinstance(category, str)
    assert isinstance(disposition, str)
    return category, disposition


def _real_evidence(
    evidence_id: str,
    kind: str,
    payload: dict[str, object],
    *,
    availability: str = "available",
) -> tuple[str, dict[str, object]]:
    """Build a raw, collector-shaped evidence entry (mirrors collector.py's output)."""
    return (
        evidence_id,
        {
            "kind": kind,
            "url": f"https://github.com/{REPOSITORY}/issues/1",
            "collectedAt": COLLECTED_AT,
            "availability": availability,
            "payload": payload,
        },
    )


def _real_snapshot(
    issue_number: int,
    *,
    ledger_rows: list[dict[str, object]],
    title: str = "[Main CI Failure] Project did not compile",
    producer: str = "ci-failure-cause",
    extra_evidence: tuple[tuple[str, dict[str, object]], ...] = (),
) -> dict[str, object]:
    """Build a real, collector-shaped raw snapshot for the prepare_assessment path.

    Unlike ``_compact_issue``/``_compact_prepared`` above (which construct an
    already-prepared evidence bundle directly, bypassing lifecycle.py
    entirely), this snapshot is shaped like the raw collector's output and
    must be run through the real ``prepare_assessment`` boundary -- so it
    exercises the same raw-field projection (e.g. collector ``branch`` ->
    prepared ``headBranch``) and evidence-bundle ordering/capping that
    production snapshots go through.
    """
    issue_url = f"https://github.com/{REPOSITORY}/issues/{issue_number}"
    issue_payload = {
        "number": issue_number,
        "state": "open",
        "title": title,
        "url": issue_url,
        "createdAt": "2026-08-01T00:00:00Z",
        "updatedAt": COLLECTED_AT,
        "closedAt": None,
        "labels": ["ci-failure-cause"],
        "producer": producer,
        "autoclose": None,
        "ledger": {
            "source": "body-table",
            "schema": "occurrences-v1",
            "schemaRecognized": True,
            "sourceRecordCount": len(ledger_rows),
            "parsedRowCount": len(ledger_rows),
            "complete": True,
            "rows": ledger_rows,
        },
        "episodes": [{"openedAt": "2026-08-01T00:00:00Z", "closedAt": None}],
        "episodesComplete": False,
        "facts": [],
    }
    issue_evidence = (
        f"issue:{issue_number}",
        {
            "kind": "issue-event",
            "url": issue_url,
            "collectedAt": COLLECTED_AT,
            "availability": "available",
            "payload": issue_payload,
        },
    )
    return {
        "schemaVersion": 1,
        "repository": REPOSITORY,
        "collectedAt": COLLECTED_AT,
        "openIssues": [issue_number],
        "issues": [issue_payload],
        "supportingIssues": [],
        "evidence": dict((issue_evidence, *extra_evidence)),
        "collectionErrors": [],
        "warnings": [],
        "references": {},
    }


class PocValidationTests(unittest.TestCase):
    def test_valid_example_passes(self) -> None:
        validate_poc_judgments(_prepared(), _judgment())

    def test_rejects_missing_issue_judgment(self) -> None:
        prepared = _prepared()
        judgment = {"schemaVersion": 1, "snapshotId": prepared["snapshotId"], "issues": []}

        with self.assertRaisesRegex(ValidationError, "Missing issue judgment"):
            validate_poc_judgments(prepared, judgment)

    def test_rejects_unknown_category(self) -> None:
        prepared = _prepared()
        judgment = _judgment()
        judgment["issues"][0]["category"] = "bogus"

        with self.assertRaisesRegex(ValidationError, "Unsupported issue category"):
            validate_poc_judgments(prepared, judgment)

    def test_rejects_unknown_disposition(self) -> None:
        prepared = _prepared()
        judgment = _judgment()
        judgment["issues"][0]["recommendations"][0]["disposition"] = "bogus"

        with self.assertRaisesRegex(ValidationError, "Unsupported recommendation disposition"):
            validate_poc_judgments(prepared, judgment)

    def test_rejects_unknown_target_kind(self) -> None:
        prepared = _prepared()
        judgment = _judgment()
        judgment["issues"][0]["recommendations"][0]["target"]["kind"] = "bogus"

        with self.assertRaisesRegex(ValidationError, "Unsupported target kind"):
            validate_poc_judgments(prepared, judgment)

    def test_rejects_unknown_confidence(self) -> None:
        prepared = _prepared()
        judgment = _judgment()
        judgment["issues"][0]["recommendations"][0]["confidence"] = "bogus"

        with self.assertRaisesRegex(ValidationError, "Unsupported confidence"):
            validate_poc_judgments(prepared, judgment)

    def test_rejects_outside_bundle_evidence(self) -> None:
        prepared = _prepared()
        judgment = _judgment()
        judgment["issues"][0]["recommendations"][0]["evidenceIds"].append("run:999")

        with self.assertRaisesRegex(ValidationError, "outside its evidence bundle"):
            validate_poc_judgments(prepared, judgment)

    def test_rejects_duplicate_issue_judgment(self) -> None:
        prepared = _prepared()
        judgment = _judgment()
        judgment["issues"].append(copy.deepcopy(judgment["issues"][0]))

        with self.assertRaisesRegex(ValidationError, "Duplicate issue judgment"):
            validate_poc_judgments(prepared, judgment)

    def test_rejects_empty_recommendations(self) -> None:
        prepared = _prepared()
        judgment = _judgment()
        judgment["issues"][0]["recommendations"] = []

        with self.assertRaisesRegex(ValidationError, "must include at least one recommendation"):
            validate_poc_judgments(prepared, judgment)

    def test_rejects_invalid_issue_target(self) -> None:
        prepared = _prepared()
        judgment = _judgment()
        judgment["issues"][0]["recommendations"][0]["target"]["value"] = 999

        with self.assertRaisesRegex(ValidationError, "must match issue number"):
            validate_poc_judgments(prepared, judgment)

    def test_rejects_multiple_recommendations_for_same_canonical_target(self) -> None:
        prepared = _prepared()
        cases = (
            ("issue", 101, 101),
            ("test", "Namespace.Type.Test", " Namespace.Type.Test "),
        )

        for kind, first_value, second_value in cases:
            with self.subTest(kind=kind):
                judgment = _judgment()
                first_recommendation = judgment["issues"][0]["recommendations"][0]
                first_recommendation["target"] = {"kind": kind, "value": first_value}
                first_recommendation["disposition"] = "investigate"

                second_recommendation = copy.deepcopy(first_recommendation)
                second_recommendation["target"] = {"kind": kind, "value": second_value}
                second_recommendation["disposition"] = "no-action"
                judgment["issues"][0]["recommendations"].append(second_recommendation)

                with self.assertRaisesRegex(ValidationError, "Duplicate recommendation target"):
                    validate_poc_judgments(prepared, judgment)

    def test_accepts_multiple_recommendations_for_distinct_targets(self) -> None:
        prepared = _prepared()
        judgment = _judgment()
        issue_recommendation = judgment["issues"][0]["recommendations"][0]
        issue_recommendation["target"] = {"kind": "issue", "value": 101}
        issue_recommendation["disposition"] = "investigate"

        test_recommendation = copy.deepcopy(issue_recommendation)
        test_recommendation["target"] = {"kind": "test", "value": "Namespace.Type.Test"}
        test_recommendation["disposition"] = "review-quarantine"
        judgment["issues"][0]["recommendations"].append(test_recommendation)

        validate_poc_judgments(prepared, judgment)

    def test_rejects_extra_field(self) -> None:
        prepared = _prepared()
        judgment = _judgment()
        judgment["extra"] = True

        with self.assertRaisesRegex(ValidationError, "contains unknown or forbidden field"):
            validate_poc_judgments(prepared, judgment)

    def test_snapshot_id_is_stable(self) -> None:
        prepared = _prepared()
        self.assertEqual("snapshot:owner/repo:2026-08-17T21:24:23Z", prepared["snapshotId"])
        self.assertEqual(prepared["snapshotId"], _prepared()["snapshotId"])

    def test_build_compact_poc_input_projects_default_judgments(self) -> None:
        prepared = _compact_prepared(
            [
                _compact_issue(
                    101,
                    title="Flaky test",
                    producer="ci-failure-cause",
                    autoclose=False,
                    parsed_row_count=3,
                    tier2_test_name="Namespace.Type.Test",
                    candidate_state="resolved",
                    candidate_action="recommend-close",
                    blockers=["autoclose-policy-does-not-permit-shepherd"],
                    resolution_evidence={
                        "pullRequestEvidenceId": "pr:101",
                        "runEvidenceId": "run:101",
                        "latestOccurrence": "2026-08-17T21:24:23Z",
                    },
                    ledger_rows=[
                        {
                            "date": "2026-08-10",
                            "sourceRun": 1001,
                            "job": "Tests / Linux",
                            "pullRequest": 501,
                        },
                        {
                            "date": "2026-08-10",
                            "sourceRun": 1001,
                            "job": "Tests / Linux",
                            "pullRequest": 501,
                        },
                        {
                            "date": "2026-08-17",
                            "sourceRun": 1002,
                            "job": "Tests / Windows",
                            "pullRequest": 502,
                        },
                    ],
                ),
                _compact_issue(
                    102,
                    title="Tracker issue",
                    producer="tracking-issue",
                    tier2_test_name=None,
                    candidate_state="actionable",
                    candidate_action="investigate",
                    missing_prerequisites=["complete-comment-run-ledger"],
                    resolution_evidence={},
                    ledger_rows=[
                        {
                            "createdAt": "2026-08-15T10:00:00Z",
                            "runId": 2001,
                        },
                        {
                            "createdAt": "2026-08-17T10:00:00Z",
                            "runId": 2002,
                        },
                    ],
                    parsed_row_count=2,
                ),
                _compact_issue(
                    103,
                    title="Watch later",
                    producer="ci-failure-cause",
                    tier2_test_name=None,
                    candidate_state="observing",
                    candidate_action="wait",
                    parsed_row_count=0,
                    missing_prerequisites=[],
                    resolution_evidence={},
                ),
            ]
        )

        compact = build_compact_poc_input(prepared)

        self.assertEqual({"schemaVersion", "snapshotId", "issues"}, set(compact))
        self.assertEqual(1, compact["schemaVersion"])
        self.assertEqual(prepared["snapshotId"], compact["snapshotId"])
        self.assertEqual(3, len(compact["issues"]))

        issue_101 = compact["issues"][0]
        self.assertEqual(
            {
                "issueNumber",
                "title",
                "producer",
                "autoclose",
                "occurrenceCount",
                "occurrenceSummary",
                "clusterOccurrenceSummary",
                "historyOccurrenceSummary",
                "identity",
                "relatedIssues",
                "reviewRequired",
                "watchReason",
                "humanContext",
                "automationContext",
                "candidateState",
                "candidateAction",
                "blockers",
                "missingPrerequisites",
                "resolutionEvidence",
                "recoveredRunEvidenceId",
                "allowedEvidence",
                "defaultJudgment",
            },
            set(issue_101),
        )
        self.assertEqual(3, issue_101["occurrenceCount"])
        self.assertEqual(
            {
                "distinctDayCount": 2,
                "distinctJobCount": 2,
                "distinctPullRequestCount": 2,
                "firstSeenDate": "2026-08-10",
                "independentRunCount": 2,
                "lastSeenDate": "2026-08-17",
                "ledgerComplete": True,
                "schemaRecognized": True,
            },
            issue_101["occurrenceSummary"],
        )
        self.assertEqual("Namespace.Type.Test", issue_101["identity"]["tier2TestName"])
        self.assertEqual(8, len(issue_101["allowedEvidence"]))
        self.assertNotIn("payload", issue_101["allowedEvidence"][0])
        self.assertEqual(
            ["issue:101", "pr:101", "run:101"],
            issue_101["defaultJudgment"]["recommendations"][0]["evidenceIds"],
        )
        self.assertEqual(
            {
                "issueNumber": 101,
                "category": "flaky-test",
                "recommendations": [
                    {
                        "disposition": "review-close",
                        "target": {"kind": "test", "value": "Namespace.Type.Test"},
                        "confidence": "medium",
                        "summary": "Review this issue for closure.",
                        "evidenceIds": ["issue:101", "pr:101", "run:101"],
                        "missingEvidence": [],
                        "reassessWhen": "After the next positive evidence or human review.",
                    }
                ],
            },
            issue_101["defaultJudgment"],
        )

        issue_102 = compact["issues"][1]
        self.assertEqual("automation-tracker", issue_102["defaultJudgment"]["category"])
        self.assertEqual(2, issue_102["occurrenceSummary"]["independentRunCount"])
        self.assertEqual(2, issue_102["occurrenceSummary"]["distinctDayCount"])
        self.assertEqual("2026-08-15", issue_102["occurrenceSummary"]["firstSeenDate"])
        self.assertEqual("2026-08-17", issue_102["occurrenceSummary"]["lastSeenDate"])
        self.assertEqual("investigate", issue_102["defaultJudgment"]["recommendations"][0]["disposition"])
        self.assertEqual({"kind": "issue", "value": 102}, issue_102["defaultJudgment"]["recommendations"][0]["target"])
        self.assertEqual(["complete-comment-run-ledger"], issue_102["defaultJudgment"]["recommendations"][0]["missingEvidence"])

        issue_103 = compact["issues"][2]
        self.assertEqual(0, issue_103["occurrenceCount"])
        self.assertEqual("watch", issue_103["defaultJudgment"]["recommendations"][0]["disposition"])
        self.assertEqual({"kind": "issue", "value": 103}, issue_103["defaultJudgment"]["recommendations"][0]["target"])
        self.assertEqual(["agent review needed"], issue_103["defaultJudgment"]["recommendations"][0]["missingEvidence"])

        compact_judgments = {
            "schemaVersion": 1,
            "snapshotId": compact["snapshotId"],
            "issues": [issue["defaultJudgment"] for issue in compact["issues"]],
        }
        validate_poc_judgments(prepared, compact_judgments)

    def test_build_compact_poc_input_keeps_evidence_ids_within_allowed_evidence(self) -> None:
        prepared = _compact_prepared(
            [
                _compact_issue(
                    201,
                    bundle_size=9,
                    payload_size=1024,
                    resolution_evidence={
                        "pullRequestEvidenceId": "pr:201",
                        "runEvidenceId": "run:201",
                    },
                )
            ]
        )

        compact = build_compact_poc_input(prepared)
        issue = compact["issues"][0]
        allowed_ids = [entry["id"] for entry in issue["allowedEvidence"]]
        evidence_ids = issue["defaultJudgment"]["recommendations"][0]["evidenceIds"]

        self.assertLessEqual(len(issue["allowedEvidence"]), 8)
        self.assertLessEqual(len(evidence_ids), 3)
        self.assertEqual(["issue:201", "pr:201", "run:201"], evidence_ids)
        self.assertTrue(set(evidence_ids).issubset(set(allowed_ids)))
        self.assertNotIn("payload", issue["resolutionEvidence"])

    def test_build_compact_poc_input_applies_safe_deterministic_rules(self) -> None:
        recurring_rows = [
            {"date": "2026-08-10", "sourceRun": 1001, "job": "Tests"},
            {"date": "2026-08-17", "sourceRun": 1002, "job": "Tests"},
        ]
        recurrent_infrastructure_rows = recurring_rows + [
            {"date": "2026-08-17", "sourceRun": 1003, "job": "Tests"},
        ]
        prepared = _compact_prepared(
            [
                _compact_issue(
                    301,
                    title="Flaky test timeout",
                    parsed_row_count=2,
                    ledger_rows=recurring_rows,
                    tier1_cause_id="test-timeout",
                    candidate_state="actionable",
                    candidate_action="investigate",
                ),
                _compact_issue(
                    302,
                    title="Emulator evaluation period expired",
                    parsed_row_count=2,
                    ledger_rows=recurring_rows,
                    tier1_cause_id="emulator-evaluation-period-expired",
                    candidate_state="actionable",
                    candidate_action="investigate",
                ),
                _compact_issue(
                    303,
                    title="NPM registry returned HTTP 429",
                    tier1_cause_id="npm-registry-rate-limit",
                    tier2_test_name=None,
                    candidate_state="observing",
                    candidate_action="wait",
                ),
                _compact_issue(
                    304,
                    title="Windows process initialization failure",
                    parsed_row_count=3,
                    ledger_rows=recurrent_infrastructure_rows,
                    tier1_cause_id="windows-process-init-failure-0xc0000142",
                    tier2_test_name=None,
                    candidate_state="actionable",
                    candidate_action="investigate",
                ),
                _compact_issue(
                    305,
                    title="CI lane red",
                    producer="ci-health-dashboard",
                    tier2_test_name=None,
                    candidate_state="actionable",
                    candidate_action="investigate",
                ),
                _compact_issue(
                    309,
                    title='CI lane "Deployment Environment Cleanup" red',
                    producer="ci-health-dashboard",
                    tier2_test_name=None,
                    candidate_state="actionable",
                    candidate_action="investigate",
                    labels=["area-deployment", "automation-broken"],
                    issue_body=(
                        "Workflow lane **Deployment Environment Cleanup** is red at tip.\n\n"
                        "- Failing since 10h · streak 10\n"
                        "- Last run: https://github.com/owner/repo/actions/runs/9001\n"
                        "- Assessment: Azure Login fails with AADSTS5000229 because "
                        "tenant 'Aspire Testing' is expired.\n"
                        "- Suggested: Renew or replace the expired Azure tenant or "
                        "switch to a valid service principal, then rerun cleanup."
                    ),
                ),
                _compact_issue(
                    306,
                    title="Release tracker",
                    producer="tracking-issue",
                    autoclose=True,
                    tier2_test_name=None,
                    candidate_state="observing",
                    candidate_action="wait",
                ),
                _compact_issue(
                    307,
                    title="[Main CI Failure] Project did not compile",
                    tier2_test_name=None,
                    candidate_state="actionable",
                    candidate_action="investigate",
                ),
                _compact_issue(
                    308,
                    title="[Main CI Failure] Project did not compile",
                    tier2_test_name=None,
                    candidate_state="resolved",
                    candidate_action="recommend-close",
                ),
                _compact_issue(
                    310,
                    title="[aw] Milestone Changelog Generator failed",
                    producer="gh-aw-failure-issue",
                    tier2_test_name=None,
                    candidate_state="actionable",
                    candidate_action="investigate",
                    labels=["agentic-workflows"],
                    markers=[
                        {
                            "key": "gh-aw-agentic-workflow",
                            "normalized": (
                                "milestone changelog generator, engine: copilot, "
                                "id: 1001, workflow_id: milestone-changelog, "
                                "run: https://github.com/owner/repo/actions/runs/1001"
                            ),
                        },
                        {
                            "key": "gh-aw-failure-issue",
                            "normalized": (
                                "true, workflow_id: milestone-changelog, branch: main, "
                                "failure_categories: agent_failure"
                            ),
                        },
                        {
                            "key": "gh-aw-expires",
                            "normalized": "2026-08-24t00:00:00z",
                        },
                    ],
                    author="github-actions[bot]",
                ),
                _compact_issue(
                    311,
                    title="[aw] Milestone Changelog Generator failed",
                    producer="gh-aw-failure-issue",
                    tier2_test_name=None,
                    candidate_state="actionable",
                    candidate_action="investigate",
                    labels=["agentic-workflows"],
                    markers=[
                        {
                            "key": "gh-aw-failure-issue",
                            "normalized": (
                                "true, workflow_id: milestone-changelog, branch: main, "
                                "failure_categories: agent_failure"
                            ),
                        }
                    ],
                ),
            ]
        )

        compact = build_compact_poc_input(prepared)
        defaults = {
            issue["issueNumber"]: issue["defaultJudgment"]
            for issue in compact["issues"]
        }

        self.assertEqual(("flaky-test", "review-quarantine"), _category_and_disposition(defaults[301]))
        self.assertEqual(("product-or-tooling", "investigate"), _category_and_disposition(defaults[302]))
        self.assertEqual(("transient-infrastructure", "watch"), _category_and_disposition(defaults[303]))
        self.assertEqual(("transient-infrastructure", "review-retry"), _category_and_disposition(defaults[304]))
        self.assertEqual(("automation-tracker", "investigate"), _category_and_disposition(defaults[305]))
        self.assertEqual(("automation-tracker", "ping-human"), _category_and_disposition(defaults[309]))
        escalation = defaults[309]["recommendations"][0]["humanEscalation"]
        self.assertIn("AADSTS5000229", escalation["context"])
        self.assertIn("authorized owner", escalation["whyHuman"])
        self.assertIn("renewed", escalation["question"])
        self.assertEqual("area-deployment", escalation["routingHint"])
        self.assertEqual(3, len(escalation["suggestedNextSteps"]))
        self.assertEqual(("automation-tracker", "no-action"), _category_and_disposition(defaults[306]))
        # Issue 307 is a blocking-build issue with no reported humanContext
        # (no dashboard-style body, no decisionRequired signal). With no
        # explicit human decision requirement and no directly referenced
        # later successful main run, the deterministic default is
        # "investigate", not "ping-human" -- ping-human is reserved for
        # cases that actually carry a reported decision requirement.
        self.assertEqual(("blocking-build", "investigate"), _category_and_disposition(defaults[307]))
        self.assertEqual(("blocking-build", "investigate"), _category_and_disposition(defaults[308]))
        self.assertEqual(("automation-tracker", "review-close"), _category_and_disposition(defaults[310]))
        self.assertEqual(("automation-tracker", "investigate"), _category_and_disposition(defaults[311]))
        self.assertIn("#310", defaults[311]["recommendations"][0]["summary"])
        self.assertEqual(
            [{"issueNumber": 311, "relationship": "same-workflow-failure"}],
            next(issue for issue in compact["issues"] if issue["issueNumber"] == 310)["relatedIssues"],
        )
        self.assertEqual(
            {
                "canonicalIssueNumber": 311,
                "memberIssueNumbers": [310, 311],
                "relationship": "same-workflow-failure",
                "role": "superseded",
            },
            next(issue for issue in compact["issues"] if issue["issueNumber"] == 310)[
                "actionCluster"
            ],
        )
        automation_context = next(
            issue for issue in compact["issues"] if issue["issueNumber"] == 310
        )["automationContext"]
        self.assertEqual("github-actions[bot]", automation_context["author"])
        self.assertEqual("milestone-changelog", automation_context["workflowId"])
        self.assertEqual("milestone changelog generator", automation_context["workflowName"])
        self.assertEqual("2026-08-24t00:00:00z", automation_context["expiresAt"])
        self.assertEqual(["agent_failure"], automation_context["failureCategories"])
        self.assertEqual([310, 1001], automation_context["runIds"])

    def test_bot_failure_relates_to_legacy_dashboard_tracker_for_same_workflow(self) -> None:
        prepared = _compact_prepared(
            [
                _compact_issue(
                    401,
                    title='CI lane "Milestone Changelog Generator" red',
                    producer="ci-health-dashboard",
                    tier2_test_name=None,
                    candidate_state="actionable",
                    candidate_action="investigate",
                ),
                _compact_issue(
                    402,
                    title="[aw] Milestone Changelog Generator failed",
                    producer="gh-aw-failure-issue",
                    tier2_test_name=None,
                    candidate_state="actionable",
                    candidate_action="investigate",
                    labels=["agentic-workflows"],
                    markers=[
                        {
                            "key": "gh-aw-agentic-workflow",
                            "normalized": (
                                "milestone changelog generator, engine: copilot, id: 1001"
                            ),
                        }
                    ],
                ),
            ]
        )

        compact = build_compact_poc_input(prepared)

        dashboard = next(issue for issue in compact["issues"] if issue["issueNumber"] == 401)
        bot_issue = next(issue for issue in compact["issues"] if issue["issueNumber"] == 402)
        self.assertEqual(
            [{"issueNumber": 402, "relationship": "same-workflow-failure"}],
            dashboard["relatedIssues"],
        )
        self.assertNotIn("actionCluster", dashboard)
        self.assertIsNone(dashboard["clusterOccurrenceSummary"])
        self.assertIsNone(bot_issue["clusterOccurrenceSummary"])

    def test_ping_human_requires_a_structured_human_escalation(self) -> None:
        prepared = _compact_prepared(
            [
                _compact_issue(
                    310,
                    title="[Main CI Failure] Project did not compile",
                    tier2_test_name=None,
                    candidate_state="actionable",
                    candidate_action="investigate",
                    # A blocking-build default is only "ping-human" when the issue
                    # itself reports a human decision requirement; without this body
                    # the deterministic default would be "investigate" instead.
                    issue_body=(
                        "- Assessment: Azure Login fails with AADSTS5000229 because "
                        "tenant 'Aspire Testing' is expired.\n"
                        "- Suggested: Renew or replace the expired Azure tenant, "
                        "then rerun the workflow."
                    ),
                )
            ]
        )
        compact = build_compact_poc_input(prepared)
        judgment = copy.deepcopy(compact["issues"][0]["defaultJudgment"])
        judgments = {
            "schemaVersion": 1,
            "snapshotId": compact["snapshotId"],
            "issues": [judgment],
        }
        validate_poc_judgments(prepared, judgments)

        del judgment["recommendations"][0]["humanEscalation"]

        with self.assertRaisesRegex(
            ValidationError,
            "ping-human recommendations must include humanEscalation",
        ):
            validate_poc_judgments(prepared, judgments)

    def test_blocking_build_without_decision_required_defaults_to_investigate(self) -> None:
        # Regression for the unconditional blocking-build -> ping-human mapping:
        # a blocking-build issue with no reported humanContext.decisionRequired
        # (no dashboard-style body, no labels) and no verified recovery must
        # default to "investigate", not "ping-human". ping-human is reserved
        # for issues that actually report a human decision requirement.
        prepared = _compact_prepared(
            [
                _compact_issue(
                    19001,
                    title="[Main CI Failure] Project did not compile",
                    tier2_test_name=None,
                    candidate_state="actionable",
                    candidate_action="investigate",
                )
            ]
        )

        compact = build_compact_poc_input(prepared)
        default_judgment = compact["issues"][0]["defaultJudgment"]

        self.assertIsNone(compact["issues"][0]["humanContext"])
        self.assertEqual(
            ("blocking-build", "investigate"),
            _category_and_disposition(default_judgment),
        )
        self.assertNotIn("humanEscalation", default_judgment["recommendations"][0])

    def test_generic_credential_tokens_do_not_trigger_ping_human(self) -> None:
        # Regression: "renew", "replace", "credential", and "permission" are
        # generic words that show up in issues with nothing to do with an
        # Azure tenant or workflow identity -- an unrelated yanked package, or
        # an ordinary credential/permission bug. Reporting one of those words
        # alone must not be treated as a reported human-decision requirement;
        # this POC's only grounded decision gate is explicit Azure
        # tenant/identity evidence (see the expired-tenant fixture below).
        prepared = _compact_prepared(
            [
                _compact_issue(
                    19301,
                    title="[Main CI Failure] Build fails after dependency package was yanked",
                    tier2_test_name=None,
                    candidate_state="actionable",
                    candidate_action="investigate",
                    issue_body=(
                        "- Assessment: The pinned package version was yanked "
                        "from the registry, so restore fails.\n"
                        "- Suggested: Renew the lockfile and replace the "
                        "yanked dependency with a supported version."
                    ),
                ),
                _compact_issue(
                    19302,
                    title="[Main CI Failure] Deploy step lacks permission to publish artifacts",
                    tier2_test_name=None,
                    candidate_state="actionable",
                    candidate_action="investigate",
                    issue_body=(
                        "- Assessment: The deploy step fails because the "
                        "stored credential no longer has permission to "
                        "publish.\n"
                        "- Suggested: Renew the credential or replace it "
                        "with one that has publish permission."
                    ),
                ),
            ]
        )

        compact = build_compact_poc_input(prepared)
        defaults = {
            issue["issueNumber"]: issue["defaultJudgment"] for issue in compact["issues"]
        }
        human_contexts = {
            issue["issueNumber"]: issue["humanContext"] for issue in compact["issues"]
        }

        for issue_number in (19301, 19302):
            self.assertFalse(human_contexts[issue_number]["decisionRequired"])
            self.assertEqual(
                ("blocking-build", "investigate"),
                _category_and_disposition(defaults[issue_number]),
            )
            recommendation = defaults[issue_number]["recommendations"][0]
            self.assertNotIn("humanEscalation", recommendation)
            serialized = json.dumps(recommendation).lower()
            self.assertNotIn("tenant", serialized)
            self.assertNotIn("service principal", serialized)
            self.assertNotIn("service-principal", serialized)

    def test_expired_tenant_ping_human_grounds_escalation_in_reported_text(self) -> None:
        # The expired-Azure-tenant case (mirroring issue #18784) must still
        # produce a precise, grounded escalation: a decision question, a
        # whyHuman rationale, and actionable next steps that quote the
        # issue's own reported assessment/suggestion rather than assuming an
        # unreported remediation path.
        prepared = _compact_prepared(
            [
                _compact_issue(
                    19303,
                    title='CI lane "Deployment Environment Cleanup" red',
                    producer="ci-health-dashboard",
                    tier2_test_name=None,
                    candidate_state="actionable",
                    candidate_action="investigate",
                    labels=["area-deployment"],
                    issue_body=(
                        "Workflow lane **Deployment Environment Cleanup** is "
                        "red at tip.\n\n"
                        "- Failing since 6h · streak 6\n"
                        "- Assessment: Azure Login fails with AADSTS5000229 "
                        "because tenant 'Aspire Testing' is expired.\n"
                        "- Suggested: Renew the expired Azure tenant, then "
                        "rerun cleanup."
                    ),
                )
            ]
        )

        compact = build_compact_poc_input(prepared)
        default_judgment = compact["issues"][0]["defaultJudgment"]
        self.assertTrue(compact["issues"][0]["humanContext"]["decisionRequired"])
        self.assertEqual(
            ("automation-tracker", "ping-human"),
            _category_and_disposition(default_judgment),
        )

        escalation = default_judgment["recommendations"][0]["humanEscalation"]
        self.assertIn("AADSTS5000229", escalation["context"])
        self.assertIn("authorized owner", escalation["whyHuman"])
        self.assertIn("renewed", escalation["question"])
        self.assertIn(
            "Renew the expired Azure tenant, then rerun cleanup.",
            escalation["question"],
        )
        self.assertIn(
            "Follow the reported suggestion: Renew the expired Azure tenant, "
            "then rerun cleanup.",
            escalation["suggestedNextSteps"],
        )
        self.assertEqual(3, len(escalation["suggestedNextSteps"]))
        self.assertEqual("area-deployment", escalation["routingHint"])

    def test_blocking_build_with_later_direct_successful_main_run_supports_review_close(
        self,
    ) -> None:
        # Regression for issue #19149: the issue body references fixing PR #19148
        # and successful main run 31211923676. After expansion, that run is
        # available in the issue's own evidence bundle with conclusion "success"
        # on "main", created after the issue's last recorded occurrence. That
        # directly issue-scoped later success is enough to support a recovered
        # one-off "review-close" -- independent of whether the lifecycle-level
        # commit-anchored recovery (exact merge-commit-sha match) also fired.
        prepared = _compact_prepared(
            [
                _compact_issue(
                    19149,
                    title="[Main CI Failure] Project did not compile",
                    tier2_test_name=None,
                    candidate_state="actionable",
                    candidate_action="investigate",
                    ledger_rows=[
                        {"date": "2026-08-15", "sourceRun": 31211900000, "job": "Build"},
                    ],
                    # The referenced fix PR is present in the bundle (merged), but
                    # we never claim it -- on its own -- is proven to be the fix.
                    pr_payload={"number": 19148, "mergedAt": "2026-08-15T23:00:00Z"},
                    run_payload={
                        "runId": 31211923676,
                        "headBranch": "main",
                        "createdAt": "2026-08-18T12:00:00Z",
                    },
                )
            ]
        )

        compact = build_compact_poc_input(prepared)
        default_judgment = compact["issues"][0]["defaultJudgment"]

        self.assertEqual(
            ("blocking-build", "review-close"),
            _category_and_disposition(default_judgment),
        )
        recommendation = default_judgment["recommendations"][0]
        self.assertIn("run:19149", recommendation["evidenceIds"])

    def test_old_unknown_one_off_with_later_success_supports_review_close(self) -> None:
        issue = _compact_issue(
            19452,
            title=(
                "Java SDK Validation failed with exit code 1; "
                "job logs unavailable"
            ),
            tier1_cause_id="polyglot-java-sdk-validation-build-image-failure",
            tier2_test_name=None,
            tier3_error_code="1",
            candidate_state="observing",
            candidate_action="wait",
            ledger_rows=[
                {
                    "date": "2026-05-01",
                    "sourceRun": 32082892403,
                    "job": "Java SDK Validation",
                }
            ],
            run_payload={
                "runId": 32099999999,
                "workflowId": 42,
                "workflowName": "Tests",
                "headBranch": "main",
                "createdAt": "2026-08-10T12:00:00Z",
            },
        )
        issue["evidenceBundle"].append(
            {
                "id": "run:19452:failed",
                "kind": "workflow-run",
                "availability": "available",
                "payload": {
                    "runId": 32082892403,
                    "workflowId": 42,
                    "workflowName": "Tests",
                    "conclusion": "failure",
                    "headBranch": "main",
                    "createdAt": "2026-05-01T12:00:00Z",
                },
            }
        )
        prepared = _compact_prepared([issue])

        compact = build_compact_poc_input(prepared)
        compact_issue = compact["issues"][0]

        self.assertEqual(
            ("unknown", "review-close"),
            _category_and_disposition(compact_issue["defaultJudgment"]),
        )
        self.assertEqual("run:19452", compact_issue["recoveredRunEvidenceId"])
        self.assertIsNone(compact_issue["watchReason"])

    def test_unknown_one_off_ignores_success_from_an_unrelated_workflow(self) -> None:
        issue = _compact_issue(
            19453,
            title="Java SDK Validation failed with exit code 1; job logs unavailable",
            tier1_cause_id="polyglot-java-sdk-validation-build-image-failure",
            tier2_test_name=None,
            tier3_error_code="1",
            candidate_state="observing",
            candidate_action="wait",
            ledger_rows=[
                {
                    "date": "2026-05-01",
                    "sourceRun": 32082892403,
                    "job": "Java SDK Validation",
                }
            ],
            run_payload={
                "runId": 32099999999,
                "workflowId": 99,
                "workflowName": "Tests",
                "headBranch": "main",
                "createdAt": "2026-08-10T12:00:00Z",
            },
        )
        issue["evidenceBundle"].append(
            {
                "id": "run:19453:failed",
                "kind": "workflow-run",
                "availability": "available",
                "payload": {
                    "runId": 32082892403,
                    "workflowId": 42,
                    "workflowName": "Tests",
                    "conclusion": "failure",
                    "headBranch": "main",
                    "createdAt": "2026-05-01T12:00:00Z",
                },
            }
        )

        compact_issue = build_compact_poc_input(_compact_prepared([issue]))["issues"][0]

        self.assertEqual(
            ("unknown", "investigate"),
            _category_and_disposition(compact_issue["defaultJudgment"]),
        )

    def test_unknown_one_off_does_not_match_missing_workflow_id_sentinels(self) -> None:
        issue = _compact_issue(
            19456,
            title="Java SDK Validation failed with exit code 1; job logs unavailable",
            tier1_cause_id="polyglot-java-sdk-validation-build-image-failure",
            tier2_test_name=None,
            tier3_error_code="1",
            candidate_state="observing",
            candidate_action="wait",
            ledger_rows=[
                {
                    "date": "2026-05-01",
                    "sourceRun": 32082892403,
                    "job": "Java SDK Validation",
                }
            ],
            run_payload={
                "runId": 32099999999,
                "workflowId": 0,
                "workflow": "Documentation Spellcheck",
                "headBranch": "main",
                "createdAt": "2026-08-10T12:00:00Z",
            },
        )
        issue["evidenceBundle"].append(
            {
                "id": "run:19456:failed",
                "kind": "workflow-run",
                "availability": "available",
                "payload": {
                    "runId": 32082892403,
                    "workflowId": 0,
                    "workflow": "Tests",
                    "conclusion": "failure",
                    "headBranch": "main",
                    "createdAt": "2026-05-01T12:00:00Z",
                },
            }
        )

        compact_issue = build_compact_poc_input(_compact_prepared([issue]))["issues"][0]

        self.assertEqual(
            ("unknown", "investigate"),
            _category_and_disposition(compact_issue["defaultJudgment"]),
        )

    def test_unknown_one_off_with_contradictory_blocker_is_not_closed(self) -> None:
        issue = _compact_issue(
            19454,
            title="Java SDK Validation failed with exit code 1; job logs unavailable",
            tier1_cause_id="polyglot-java-sdk-validation-build-image-failure",
            tier2_test_name=None,
            tier3_error_code="1",
            candidate_state="needs-human",
            candidate_action="recommend-close",
            blockers=["issue-updated-after-fix-without-ledger-row"],
            ledger_rows=[
                {
                    "date": "2026-05-01",
                    "sourceRun": 32082892403,
                    "job": "Java SDK Validation",
                }
            ],
            run_payload={
                "runId": 32099999999,
                "workflowId": 42,
                "workflowName": "Tests",
                "headBranch": "main",
                "createdAt": "2026-08-10T12:00:00Z",
            },
        )
        issue["evidenceBundle"].append(
            {
                "id": "run:19454:failed",
                "kind": "workflow-run",
                "availability": "available",
                "payload": {
                    "runId": 32082892403,
                    "workflowId": 42,
                    "workflowName": "Tests",
                    "conclusion": "failure",
                    "headBranch": "main",
                    "createdAt": "2026-05-01T12:00:00Z",
                },
            }
        )

        compact_issue = build_compact_poc_input(_compact_prepared([issue]))["issues"][0]

        self.assertEqual(
            ("unknown", "investigate"),
            _category_and_disposition(compact_issue["defaultJudgment"]),
        )

    def test_recurrent_unknown_failure_is_not_closed_by_one_later_success(self) -> None:
        prepared = _compact_prepared(
            [
                _compact_issue(
                    19453,
                    title="Validation failed with exit code 1; job logs unavailable",
                    parsed_row_count=2,
                    tier1_cause_id="validation-exit-1",
                    tier2_test_name=None,
                    tier3_error_code="1",
                    candidate_state="observing",
                    candidate_action="wait",
                    ledger_rows=[
                        {
                            "date": "2026-05-01",
                            "sourceRun": 32080000001,
                            "job": "Validation",
                        },
                        {
                            "date": "2026-06-01",
                            "sourceRun": 32080000002,
                            "job": "Validation",
                        },
                    ],
                    run_payload={
                        "runId": 32099999998,
                        "headBranch": "main",
                        "createdAt": "2026-08-10T12:00:00Z",
                    },
                )
            ]
        )

        compact = build_compact_poc_input(prepared)

        self.assertEqual(
            ("unknown", "investigate"),
            _category_and_disposition(compact["issues"][0]["defaultJudgment"]),
        )

    def test_unknown_failure_waiting_only_for_recurrence_remains_watch(self) -> None:
        prepared = _compact_prepared(
            [
                _compact_issue(
                    19454,
                    title="Validation failed with exit code 1",
                    tier1_cause_id="validation-exit-1",
                    tier2_test_name=None,
                    tier3_error_code="1",
                    candidate_state="observing",
                    candidate_action="wait",
                    bundle_size=2,
                )
            ]
        )

        compact_issue = build_compact_poc_input(prepared)["issues"][0]

        self.assertEqual(
            ("unknown", "watch"),
            _category_and_disposition(compact_issue["defaultJudgment"]),
        )
        self.assertEqual(
            "missing-diagnostic-identity",
            compact_issue["watchReason"],
        )

    def test_generic_unknown_with_partial_run_evidence_defaults_to_investigate(self) -> None:
        prepared = _compact_prepared(
            [
                _compact_issue(
                    19455,
                    title="Validation failed with exit code 1",
                    tier1_cause_id="validation-exit-1",
                    tier2_test_name=None,
                    tier3_error_code="1",
                    candidate_state="observing",
                    candidate_action="wait",
                    run_availability="partial",
                )
            ]
        )

        compact_issue = build_compact_poc_input(prepared)["issues"][0]

        self.assertEqual(
            ("unknown", "investigate"),
            _category_and_disposition(compact_issue["defaultJudgment"]),
        )
        self.assertIsNone(compact_issue["watchReason"])

    def test_real_snapshot_same_day_recovery_closes_citing_recovery_run(self) -> None:
        # Regression for issue #19149, built through the real collector-shaped
        # pipeline (raw snapshot -> prepare_assessment -> build_compact_poc_input)
        # rather than the synthetic already-prepared bundle above. Failure and
        # recovery both land on 2026-08-07, with recovery later that same day --
        # a date-only lastSeenDate comparison would reject this as "not later",
        # but the failed run's own timestamp is a precise lower bound that the
        # later same-day success clears.
        issue_number = 19149
        referenced_by = [{"sourceIssueNumber": issue_number}]
        failed_run = _real_evidence(
            f"run:{issue_number}:failed",
            "workflow-run",
            {
                "runId": 31211900000,
                "conclusion": "failure",
                "status": "completed",
                # Real #19149 failure occurrences were on "main" (a
                # "[Main CI Failure]" issue), per the ledger row this mirrors.
                # Raw collector field name -- never "headBranch".
                "branch": "main",
                "createdAt": "2026-08-07T09:00:00Z",
                "referencedBy": referenced_by,
            },
        )
        recovery_run = _real_evidence(
            f"run:{issue_number}:recovery",
            "workflow-run",
            {
                "runId": 31211923676,
                "conclusion": "success",
                "status": "completed",
                "branch": "main",
                "createdAt": "2026-08-07T15:00:00Z",
                "referencedBy": referenced_by,
            },
        )
        raw_snapshot = _real_snapshot(
            issue_number,
            ledger_rows=[
                {"date": "2026-08-06", "sourceRun": 31211800000, "job": "Build"},
                {"date": "2026-08-07", "sourceRun": 31211900000, "job": "Build"},
            ],
            extra_evidence=(failed_run, recovery_run),
        )

        prepared = prepare_assessment(raw_snapshot)
        compact = build_compact_poc_input(prepared)
        default_judgment = compact["issues"][0]["defaultJudgment"]

        self.assertEqual(
            ("blocking-build", "review-close"),
            _category_and_disposition(default_judgment),
        )
        recommendation = default_judgment["recommendations"][0]
        self.assertIn(f"run:{issue_number}:recovery", recommendation["evidenceIds"])

    def test_real_snapshot_unknown_recovery_matches_projected_workflow_name(self) -> None:
        issue_number = 19457
        referenced_by = [{"sourceIssueNumber": issue_number}]
        failed_run = _real_evidence(
            f"run:{issue_number}:failed",
            "workflow-run",
            {
                "runId": 32082892403,
                "workflowId": 0,
                "workflow": "Tests",
                "conclusion": "failure",
                "status": "completed",
                "branch": "main",
                "createdAt": "2026-05-01T09:00:00Z",
                "referencedBy": referenced_by,
            },
        )
        recovery_run = _real_evidence(
            f"run:{issue_number}:recovery",
            "workflow-run",
            {
                "runId": 32099999999,
                "workflowId": 0,
                "workflow": "Tests",
                "conclusion": "success",
                "status": "completed",
                "branch": "main",
                "createdAt": "2026-08-10T12:00:00Z",
                "referencedBy": referenced_by,
            },
        )
        raw_snapshot = _real_snapshot(
            issue_number,
            title="Validation failed with exit code 1; job logs unavailable",
            ledger_rows=[
                {
                    "date": "2026-05-01",
                    "sourceRun": 32082892403,
                    "job": "Tests / Validation",
                }
            ],
            extra_evidence=(failed_run, recovery_run),
        )

        prepared = prepare_assessment(raw_snapshot)
        compact_issue = build_compact_poc_input(prepared)["issues"][0]

        self.assertEqual(
            ("unknown", "review-close"),
            _category_and_disposition(compact_issue["defaultJudgment"]),
        )
        self.assertEqual(
            f"run:{issue_number}:recovery",
            compact_issue["recoveredRunEvidenceId"],
        )

    def test_real_snapshot_earlier_success_does_not_close(self) -> None:
        # A success run at or before the latest directly referenced failed run
        # is not recovery evidence -- it must not support review-close, even
        # though it is still "same day" as the failure.
        issue_number = 19150
        referenced_by = [{"sourceIssueNumber": issue_number}]
        failed_run = _real_evidence(
            f"run:{issue_number}:failed",
            "workflow-run",
            {
                "runId": 31211900001,
                "conclusion": "failure",
                "status": "completed",
                "branch": "feature/unrelated",
                "createdAt": "2026-08-07T15:00:00Z",
                "referencedBy": referenced_by,
            },
        )
        earlier_success_run = _real_evidence(
            f"run:{issue_number}:earlier-success",
            "workflow-run",
            {
                "runId": 31211900002,
                "conclusion": "success",
                "status": "completed",
                "branch": "main",
                "createdAt": "2026-08-07T09:00:00Z",
                "referencedBy": referenced_by,
            },
        )
        raw_snapshot = _real_snapshot(
            issue_number,
            ledger_rows=[
                {"date": "2026-08-06", "sourceRun": 31211800001, "job": "Build"},
                {"date": "2026-08-07", "sourceRun": 31211900001, "job": "Build"},
            ],
            extra_evidence=(failed_run, earlier_success_run),
        )

        prepared = prepare_assessment(raw_snapshot)
        compact = build_compact_poc_input(prepared)
        default_judgment = compact["issues"][0]["defaultJudgment"]

        category, disposition = _category_and_disposition(default_judgment)
        self.assertEqual("blocking-build", category)
        self.assertNotEqual("review-close", disposition)

    def test_real_snapshot_malformed_success_timestamp_does_not_close(self) -> None:
        issue_number = 19152
        malformed_success_run = _real_evidence(
            f"run:{issue_number}:malformed-success",
            "workflow-run",
            {
                "runId": 31211900005,
                "conclusion": "success",
                "status": "completed",
                "branch": "main",
                "createdAt": "not-a-real-timestamp",
                "referencedBy": [{"sourceIssueNumber": issue_number}],
            },
        )
        raw_snapshot = _real_snapshot(
            issue_number,
            ledger_rows=[
                {"date": "2026-08-07", "sourceRun": 31211900004, "job": "Build"},
            ],
            extra_evidence=(malformed_success_run,),
        )

        prepared = prepare_assessment(raw_snapshot)
        compact = build_compact_poc_input(prepared)
        category, disposition = _category_and_disposition(
            compact["issues"][0]["defaultJudgment"]
        )

        self.assertEqual("blocking-build", category)
        self.assertNotEqual("review-close", disposition)

    def test_real_snapshot_success_between_older_expanded_run_and_later_unexpanded_occurrence_does_not_close(
        self,
    ) -> None:
        # Regression: the ledger's lastSeenDate and a directly referenced
        # failed run's own instant are independent signals -- one must not be
        # used to override the other. Here the *directly referenced* (i.e.
        # expanded into full workflow-run evidence) failed run is on
        # 2026-08-06, but the ledger records a *later* occurrence on
        # 2026-08-08 whose run was never expanded (it only appears as a
        # ledger row). A success on 2026-08-07 is later than the expanded
        # failed run's own instant, but it is still earlier than the ledger's
        # lastSeenDate -- so it must not support review-close, even though a
        # naive instant-only comparison against the expanded failed run would
        # wrongly treat it as "later than the failure".
        issue_number = 19152
        referenced_by = [{"sourceIssueNumber": issue_number}]
        older_expanded_failed_run = _real_evidence(
            f"run:{issue_number}:failed",
            "workflow-run",
            {
                "runId": 31211800002,
                "conclusion": "failure",
                "status": "completed",
                "branch": "main",
                "createdAt": "2026-08-06T09:00:00Z",
                "referencedBy": referenced_by,
            },
        )
        in_between_success_run = _real_evidence(
            f"run:{issue_number}:in-between-success",
            "workflow-run",
            {
                "runId": 31211850002,
                "conclusion": "success",
                "status": "completed",
                "branch": "main",
                "createdAt": "2026-08-07T12:00:00Z",
                "referencedBy": referenced_by,
            },
        )
        raw_snapshot = _real_snapshot(
            issue_number,
            ledger_rows=[
                {"date": "2026-08-06", "sourceRun": 31211800002, "job": "Build"},
                # This later occurrence's run is never directly referenced
                # (no matching workflow-run evidence entry) -- it only shows
                # up as a ledger row, which is what makes lastSeenDate
                # (2026-08-08) later than the latest expanded failed run.
                {"date": "2026-08-08", "sourceRun": 31211900005, "job": "Build"},
            ],
            extra_evidence=(older_expanded_failed_run, in_between_success_run),
        )

        prepared = prepare_assessment(raw_snapshot)
        compact = build_compact_poc_input(prepared)
        default_judgment = compact["issues"][0]["defaultJudgment"]

        category, disposition = _category_and_disposition(default_judgment)
        self.assertEqual("blocking-build", category)
        self.assertNotEqual("review-close", disposition)

    def test_real_snapshot_recovery_run_beyond_original_index_8_is_still_cited(self) -> None:
        # Regression: an issue with many pull-request/source-path records ahead
        # of the recovery workflow-run in bundle order must not let a plain
        # prefix cap evict that run from allowedEvidence/evidenceIds -- the
        # disposition must not claim review-close without a citable run.
        issue_number = 19151
        referenced_by = [{"sourceIssueNumber": issue_number}]
        crowding_pull_requests = tuple(
            _real_evidence(
                f"pr:{issue_number}:{index}",
                "pull-request",
                {
                    "number": 20000 + index,
                    "state": "closed",
                    "mergedAt": f"2026-08-0{index + 1}T10:00:00Z",
                    "referencedBy": referenced_by,
                },
            )
            for index in range(9)
        )
        failed_run = _real_evidence(
            f"run:{issue_number}:failed",
            "workflow-run",
            {
                "runId": 31211900003,
                "conclusion": "failure",
                "status": "completed",
                "branch": "feature/unrelated",
                "createdAt": "2026-08-07T09:00:00Z",
                "referencedBy": referenced_by,
            },
        )
        recovery_run = _real_evidence(
            f"run:{issue_number}:recovery",
            "workflow-run",
            {
                "runId": 31211900004,
                "conclusion": "success",
                "status": "completed",
                "branch": "main",
                "createdAt": "2026-08-07T15:00:00Z",
                "referencedBy": referenced_by,
            },
        )
        raw_snapshot = _real_snapshot(
            issue_number,
            ledger_rows=[
                {"date": "2026-08-06", "sourceRun": 31211800003, "job": "Build"},
                {"date": "2026-08-07", "sourceRun": 31211900003, "job": "Build"},
            ],
            extra_evidence=(*crowding_pull_requests, failed_run, recovery_run),
        )

        prepared = prepare_assessment(raw_snapshot, max_bundle_records=25)
        compact_issue = prepared["issues"][0]
        # Sanity-check the setup: the recovery run's evidenceBundle index is
        # indeed past the old plain-prefix cap of 8.
        bundle_ids = [entry["id"] for entry in compact_issue["evidenceBundle"]]
        self.assertGreaterEqual(bundle_ids.index(f"run:{issue_number}:recovery"), 8)

        compact = build_compact_poc_input(prepared)
        compact_issue_out = compact["issues"][0]
        default_judgment = compact_issue_out["defaultJudgment"]

        allowed_evidence_ids = [entry["id"] for entry in compact_issue_out["allowedEvidence"]]
        self.assertIn(f"run:{issue_number}:recovery", allowed_evidence_ids)
        self.assertEqual(
            ("blocking-build", "review-close"),
            _category_and_disposition(default_judgment),
        )
        recommendation = default_judgment["recommendations"][0]
        self.assertIn(f"run:{issue_number}:recovery", recommendation["evidenceIds"])

    def test_finalizer_rejects_agent_ping_human_without_decision_required(self) -> None:
        # Regression: an agent judgment must never be allowed to escalate to a
        # human when the compact issue's humanContext does not report
        # decisionRequired -- the deterministic non-human default must win.
        prepared = _compact_prepared(
            [
                _compact_issue(
                    19201,
                    title="[Main CI Failure] Project did not compile",
                    tier2_test_name=None,
                    candidate_state="actionable",
                    candidate_action="investigate",
                )
            ]
        )
        compact = build_compact_poc_input(prepared)
        default_judgment = copy.deepcopy(compact["issues"][0]["defaultJudgment"])
        self.assertEqual(
            ("blocking-build", "investigate"),
            _category_and_disposition(default_judgment),
        )
        self.assertIsNone(compact["issues"][0]["humanContext"])

        agent_judgments = {
            "schemaVersion": 1,
            "snapshotId": compact["snapshotId"],
            "issues": [
                {
                    "issueNumber": 19201,
                    "category": "blocking-build",
                    "recommendations": [
                        {
                            "disposition": "ping-human",
                            "target": {"kind": "issue", "value": 19201},
                            "confidence": "high",
                            "summary": "Route this to a human.",
                            "evidenceIds": ["issue:19201"],
                            "missingEvidence": [],
                            "reassessWhen": "After human review.",
                            "humanEscalation": {
                                "context": "Project did not compile",
                                "whyHuman": "Needs a human call.",
                                "question": "Who owns this?",
                                "suggestedNextSteps": ["Investigate"],
                                "routingHint": "main-ci-owner",
                            },
                        }
                    ],
                }
            ],
        }

        merged = merge_ambiguous_poc_judgments(compact, agent_judgments)

        self.assertEqual(default_judgment, merged["issues"][0])
        self.assertNotEqual(
            "ping-human", merged["issues"][0]["recommendations"][0]["disposition"]
        )

    def test_build_compact_poc_input_aggregates_exact_test_duplicates(self) -> None:
        test_name = "Namespace.Type.DuplicateTest"
        prepared = _compact_prepared(
            [
                _compact_issue(
                    311,
                    title="Duplicate test timed out",
                    tier1_cause_id="duplicate-test-timeout",
                    tier2_test_name=test_name,
                    ledger_rows=[
                        {"date": "2026-08-10", "sourceRun": 1001, "job": "Tests A"}
                    ],
                    candidate_state="observing",
                    candidate_action="wait",
                    markers=[
                        {
                            "key": "gh-aw-agentic-workflow",
                            "normalized": "shared workflow, id: 1001",
                        }
                    ],
                ),
                _compact_issue(
                    312,
                    title="Duplicate test timed out again",
                    tier1_cause_id="duplicate-test-rpc-timeout",
                    tier2_test_name=test_name,
                    ledger_rows=[
                        {"date": "2026-08-17", "sourceRun": 1002, "job": "Tests B"}
                    ],
                    candidate_state="observing",
                    candidate_action="wait",
                    markers=[
                        {
                            "key": "gh-aw-agentic-workflow",
                            "normalized": "shared workflow, id: 1002",
                        }
                    ],
                ),
            ]
        )

        compact = build_compact_poc_input(prepared)
        issues = {issue["issueNumber"]: issue for issue in compact["issues"]}

        self.assertEqual(
            [{"issueNumber": 312, "relationship": "same-test"}],
            issues[311]["relatedIssues"],
        )
        self.assertEqual(2, issues[311]["clusterOccurrenceSummary"]["independentRunCount"])
        self.assertEqual(2, issues[311]["clusterOccurrenceSummary"]["distinctDayCount"])
        self.assertTrue(issues[311]["reviewRequired"])
        self.assertIsNone(issues[311]["watchReason"])
        self.assertEqual(
            ("flaky-test", "review-quarantine"),
            _category_and_disposition(issues[311]["defaultJudgment"]),
        )
        self.assertIn(
            "#312",
            issues[311]["defaultJudgment"]["recommendations"][0]["summary"],
        )
        self.assertEqual(
            {
                "canonicalIssueNumber": 311,
                "memberIssueNumbers": [311, 312],
                "relationship": "same-test",
                "role": "canonical",
            },
            issues[311]["actionCluster"],
        )
        self.assertEqual(
            [{"issueNumber": 311, "relationship": "same-test"}],
            issues[312]["relatedIssues"],
        )
        self.assertEqual(
            ("flaky-test", "review-close"),
            _category_and_disposition(issues[312]["defaultJudgment"]),
        )

    def test_build_compact_poc_input_aggregates_equivalent_error_codes(self) -> None:
        prepared = _compact_prepared(
            [
                _compact_issue(
                    321,
                    title="Windows process failed with exit code -1073741502",
                    tier1_cause_id="windows-process-init-failure",
                    tier2_test_name=None,
                    ledger_rows=[
                        {"date": "2026-08-10", "sourceRun": 1001, "job": "Tests A"}
                    ],
                    candidate_state="observing",
                    candidate_action="wait",
                ),
                _compact_issue(
                    322,
                    title="Windows process failed with 0xC0000142",
                    tier1_cause_id="windows-test-host-crash",
                    tier2_test_name=None,
                    ledger_rows=[
                        {"date": "2026-08-11", "sourceRun": 1002, "job": "Tests B"},
                        {"date": "2026-08-17", "sourceRun": 1003, "job": "Tests C"},
                    ],
                    parsed_row_count=2,
                    candidate_state="observing",
                    candidate_action="wait",
                ),
            ]
        )

        compact = build_compact_poc_input(prepared)
        issues = {issue["issueNumber"]: issue for issue in compact["issues"]}

        self.assertEqual(
            [{"issueNumber": 322, "relationship": "same-error-code"}],
            issues[321]["relatedIssues"],
        )
        self.assertEqual(3, issues[321]["clusterOccurrenceSummary"]["independentRunCount"])
        self.assertEqual(3, issues[321]["clusterOccurrenceSummary"]["distinctDayCount"])
        self.assertEqual(
            ("transient-infrastructure", "review-retry"),
            _category_and_disposition(issues[321]["defaultJudgment"]),
        )
        self.assertIn(
            "#322",
            issues[321]["defaultJudgment"]["recommendations"][0]["summary"],
        )
        self.assertEqual(
            {
                "canonicalIssueNumber": 321,
                "memberIssueNumbers": [321, 322],
                "relationship": "same-error-code",
                "role": "superseded",
            },
            issues[322]["actionCluster"],
        )
        self.assertEqual(
            ("transient-infrastructure", "review-close"),
            _category_and_disposition(issues[322]["defaultJudgment"]),
        )

    def test_build_compact_poc_input_relates_cause_families_without_aggregating(self) -> None:
        prepared = _compact_prepared(
            [
                _compact_issue(
                    325,
                    title="First parent process test fails parsing a DCP timestamp",
                    tier1_cause_id="hosting-parentprocess-dcp-timestamp-badrequest",
                    tier2_exception_type="k8s.Autorest.HttpOperationException",
                    tier2_test_name="Namespace.Type.FirstTest",
                    candidate_state="observing",
                    candidate_action="wait",
                ),
                _compact_issue(
                    326,
                    title="Second parent process test fails parsing a DCP timestamp",
                    tier1_cause_id="hosting-parentprocess-reuse-dcp-timestamp-badrequest",
                    tier2_exception_type="k8s.Autorest.HttpOperationException",
                    tier2_test_name="Namespace.Type.SecondTest",
                    candidate_state="observing",
                    candidate_action="wait",
                ),
            ]
        )

        compact = build_compact_poc_input(prepared)
        issues = {issue["issueNumber"]: issue for issue in compact["issues"]}

        self.assertEqual(
            [{"issueNumber": 326, "relationship": "same-cause-family"}],
            issues[325]["relatedIssues"],
        )
        self.assertIsNone(issues[325]["clusterOccurrenceSummary"])
        self.assertTrue(issues[325]["reviewRequired"])
        self.assertEqual("single-test-occurrence", issues[325]["watchReason"])

    def test_build_compact_poc_input_does_not_aggregate_incompatible_same_test(self) -> None:
        test_name = "Namespace.Type.SameTest"
        prepared = _compact_prepared(
            [
                _compact_issue(
                    327,
                    title="Test timed out",
                    tier1_cause_id="same-test-timeout",
                    tier2_exception_type="System.TimeoutException",
                    tier2_test_name=test_name,
                    candidate_state="observing",
                    candidate_action="wait",
                ),
                _compact_issue(
                    328,
                    title="Test assertion failed",
                    tier1_cause_id="same-test-assertion-mismatch",
                    tier2_exception_type="Xunit.Sdk.EqualException",
                    tier2_test_name=test_name,
                    candidate_state="observing",
                    candidate_action="wait",
                ),
            ]
        )

        compact = build_compact_poc_input(prepared)
        issues = {issue["issueNumber"]: issue for issue in compact["issues"]}

        self.assertEqual(
            [{"issueNumber": 328, "relationship": "same-test-different-symptom"}],
            issues[327]["relatedIssues"],
        )
        self.assertIsNone(issues[327]["clusterOccurrenceSummary"])
        self.assertEqual(
            ("flaky-test", "watch"),
            _category_and_disposition(issues[327]["defaultJudgment"]),
        )

    def test_build_compact_poc_input_omits_history_occurrences_preserves_prior_behavior(
        self,
    ) -> None:
        # Regression guard for task requirement: "Omission must preserve current
        # behavior." Calling without history_occurrences (the prior signature)
        # must be byte-identical to calling with an explicit empty/None value.
        prepared = _compact_prepared(
            [
                _compact_issue(
                    401,
                    tier2_test_name="Namespace.Type.HistoryOmittedTest",
                    candidate_state="observing",
                    candidate_action="wait",
                ),
            ]
        )

        without_kwarg = build_compact_poc_input(prepared)
        with_none = build_compact_poc_input(prepared, history_occurrences=None)
        with_empty = build_compact_poc_input(prepared, history_occurrences={})

        self.assertEqual(without_kwarg, with_none)
        self.assertEqual(without_kwarg, with_empty)

        issue = without_kwarg["issues"][0]
        self.assertEqual(issue["occurrenceSummary"], issue["historyOccurrenceSummary"])
        self.assertEqual(
            ("flaky-test", "watch"),
            _category_and_disposition(issue["defaultJudgment"]),
        )

    def test_build_compact_poc_input_merges_matching_fingerprint_history(self) -> None:
        test_name = "Namespace.Type.RecurringHistoryTest"
        issue = _compact_issue(
            402,
            tier2_test_name=test_name,
            candidate_state="observing",
            candidate_action="wait",
            ledger_rows=[{"date": "2026-08-17", "sourceRun": 4020, "job": "Tests"}],
        )
        prepared = _compact_prepared([issue])
        fingerprint = compute_fingerprint(issue["identity"])
        unrelated_fingerprint = compute_fingerprint(
            {"tier2TestName": "Namespace.Type.UnrelatedTest"}
        )

        history_occurrences = {
            fingerprint: [
                {
                    "fingerprint": fingerprint,
                    "issueNumber": 199,
                    "runId": 4001,
                    "attempt": 1,
                    "date": "2026-08-01",
                    "job": "Tests",
                    "testName": test_name,
                },
            ],
            # A different fingerprint's history must never leak into this issue
            # -- only the exact fingerprint match is used, no fuzzy broadening.
            unrelated_fingerprint: [
                {
                    "fingerprint": unrelated_fingerprint,
                    "issueNumber": 198,
                    "runId": 9001,
                    "attempt": 1,
                    "date": "2026-01-01",
                    "job": "Other",
                    "testName": "Namespace.Type.UnrelatedTest",
                },
            ],
        }

        compact = build_compact_poc_input(prepared, history_occurrences=history_occurrences)
        compact_issue = compact["issues"][0]

        # The existing occurrenceSummary reflects only this issue's own ledger.
        self.assertEqual(1, compact_issue["occurrenceSummary"]["independentRunCount"])
        self.assertEqual(1, compact_issue["occurrenceSummary"]["distinctDayCount"])

        # historyOccurrenceSummary merges in the matching-fingerprint historical
        # run, and only that run.
        self.assertEqual(2, compact_issue["historyOccurrenceSummary"]["independentRunCount"])
        self.assertEqual(2, compact_issue["historyOccurrenceSummary"]["distinctDayCount"])
        self.assertEqual("2026-08-01", compact_issue["historyOccurrenceSummary"]["firstSeenDate"])
        self.assertEqual("2026-08-17", compact_issue["historyOccurrenceSummary"]["lastSeenDate"])

        # The merged history crosses the flaky-test recurrence threshold
        # (independentRuns >= 2 and distinctDays >= 2), which the issue's own
        # single occurrence alone would not.
        self.assertEqual(
            ("flaky-test", "review-quarantine"),
            _category_and_disposition(compact_issue["defaultJudgment"]),
        )

        # Regression guard: reviewRequired must be driven by the same
        # effective (history-merged) occurrence summary that promoted the
        # disposition to review-quarantine, not by the issue's own
        # single-occurrence ledger. Otherwise a history-promoted
        # review-quarantine is emitted with reviewRequired: false.
        self.assertTrue(compact_issue["reviewRequired"])
        self.assertIsNone(compact_issue["watchReason"])

    def test_build_compact_poc_input_watch_reason_uses_effective_history_summary(
        self,
    ) -> None:
        # Regression guard: watchReason must reflect the effective
        # (history-merged) occurrence summary, not the issue's own
        # single-occurrence ledger. Otherwise a two-run infrastructure
        # history is mislabeled "single-infrastructure-occurrence" instead
        # of "subthreshold-infrastructure-recurrence".
        issue = _compact_issue(
            403,
            title="NuGet download timed out",
            tier1_cause_id="nuget-download-timeout",
            tier2_test_name=None,
            candidate_state="observing",
            candidate_action="wait",
            ledger_rows=[{"date": "2026-08-17", "sourceRun": 4030, "job": "Tests"}],
        )
        prepared = _compact_prepared([issue])
        fingerprint = compute_fingerprint(issue["identity"])

        history_occurrences = {
            fingerprint: [
                {
                    "fingerprint": fingerprint,
                    "issueNumber": 199,
                    "runId": 4029,
                    "attempt": 1,
                    "date": "2026-08-10",
                    "job": "Tests",
                    "testName": None,
                },
            ],
        }

        compact = build_compact_poc_input(prepared, history_occurrences=history_occurrences)
        compact_issue = compact["issues"][0]

        # The issue's own occurrenceSummary reflects only its own ledger and
        # is unaffected by history merging.
        self.assertEqual(1, compact_issue["occurrenceSummary"]["independentRunCount"])

        # historyOccurrenceSummary merges in the matching-fingerprint run,
        # crossing the two-run threshold, but staying below the review-retry
        # threshold (independentRuns >= 3), so the disposition remains watch.
        self.assertEqual(2, compact_issue["historyOccurrenceSummary"]["independentRunCount"])
        self.assertEqual(
            ("transient-infrastructure", "watch"),
            _category_and_disposition(compact_issue["defaultJudgment"]),
        )

        self.assertEqual(
            "subthreshold-infrastructure-recurrence",
            compact_issue["watchReason"],
        )
        self.assertTrue(compact_issue["reviewRequired"])

    def test_build_compact_poc_input_includes_frozen_test_tracker_matches(self) -> None:
        prepared = _compact_prepared(
            [
                _compact_issue(
                    329,
                    title="Flaky collection race",
                    tier1_cause_id="collection-modified-race",
                    tier2_test_name="Namespace.Type.RacyTest",
                    candidate_state="observing",
                    candidate_action="wait",
                )
            ]
        )
        related_issue_matches = [
            {
                "source": 329,
                "test": "Namespace.Type.RacyTest",
                "hits": [
                    {
                        "number": 900,
                        "title": "[Failing test]: Namespace.Type.RacyTest",
                        "state": "OPEN",
                        "url": "https://github.com/owner/repo/issues/900",
                        "labels": {
                            "nodes": [
                                {"name": "failing-test"},
                                {"name": "flaky-test"},
                            ]
                        },
                    }
                ],
            }
        ]

        compact = build_compact_poc_input(
            prepared,
            related_issue_matches=related_issue_matches,
        )
        issue = compact["issues"][0]

        self.assertEqual(
            [
                {
                    "issueNumber": 900,
                    "relationship": "same-test-tracker",
                    "state": "open",
                    "labels": ["failing-test", "flaky-test"],
                    "title": "[Failing test]: Namespace.Type.RacyTest",
                }
            ],
            issue["relatedIssues"],
        )
        self.assertIsNone(issue["clusterOccurrenceSummary"])
        self.assertTrue(issue["reviewRequired"])

    def test_build_compact_poc_input_explains_reviewable_watch_cases(self) -> None:
        prepared = _compact_prepared(
            [
                _compact_issue(
                    331,
                    title="Flaky test timeout",
                    tier1_cause_id="test-timeout",
                    tier2_test_name="Namespace.Type.SingleTest",
                    candidate_state="observing",
                    candidate_action="wait",
                ),
                _compact_issue(
                    332,
                    title="Port conflict on two independent jobs",
                    parsed_row_count=2,
                    ledger_rows=[
                        {"date": "2026-08-17", "sourceRun": 1001, "job": "Tests A"},
                        {"date": "2026-08-17", "sourceRun": 1002, "job": "Tests B"},
                    ],
                    tier1_cause_id="fixture-port-conflict",
                    tier2_test_name="Namespace.Type.PortTest",
                    candidate_state="actionable",
                    candidate_action="wait",
                ),
                _compact_issue(
                    333,
                    title="Job failed with exit code 1; logs unavailable",
                    tier1_cause_id="generic-job-exit-1",
                    tier2_test_name=None,
                    tier3_error_code="1",
                    candidate_state="observing",
                    candidate_action="wait",
                ),
                _compact_issue(
                    334,
                    title="NuGet download timed out",
                    parsed_row_count=2,
                    ledger_rows=[
                        {"date": "2026-08-10", "sourceRun": 1003, "job": "Tests A"},
                        {"date": "2026-08-17", "sourceRun": 1004, "job": "Tests B"},
                    ],
                    tier1_cause_id="nuget-download-timeout",
                    tier2_test_name=None,
                    candidate_state="actionable",
                    candidate_action="wait",
                ),
            ]
        )

        compact = build_compact_poc_input(prepared)
        issues = {issue["issueNumber"]: issue for issue in compact["issues"]}

        self.assertEqual("single-test-occurrence", issues[331]["watchReason"])
        self.assertFalse(issues[331]["reviewRequired"])
        single_test_watch = issues[331]["defaultJudgment"]["recommendations"][0]
        self.assertIn("single-test-occurrence", single_test_watch["summary"])
        self.assertIn("different day", single_test_watch["reassessWhen"])
        self.assertEqual("same-day-test-recurrence", issues[332]["watchReason"])
        self.assertTrue(issues[332]["reviewRequired"])
        self.assertIsNone(issues[333]["watchReason"])
        self.assertTrue(issues[333]["reviewRequired"])
        self.assertEqual(
            ("unknown", "investigate"),
            _category_and_disposition(issues[333]["defaultJudgment"]),
        )
        self.assertEqual("subthreshold-infrastructure-recurrence", issues[334]["watchReason"])
        self.assertTrue(issues[334]["reviewRequired"])
        infrastructure_watch = issues[334]["defaultJudgment"]["recommendations"][0]
        self.assertIn(
            "subthreshold-infrastructure-recurrence",
            infrastructure_watch["summary"],
        )
        self.assertIn("third independent failure", infrastructure_watch["reassessWhen"])

    def test_merge_ambiguous_poc_judgments_preserves_safe_defaults(self) -> None:
        compact = build_compact_poc_input(
            _compact_prepared(
                [
                    _compact_issue(
                        401,
                        title="NPM registry returned HTTP 429",
                        tier1_cause_id="npm-registry-rate-limit",
                        tier2_test_name=None,
                        candidate_state="observing",
                        candidate_action="wait",
                    ),
                    _compact_issue(
                        402,
                        title="Unclassified failure",
                        tier2_test_name=None,
                        candidate_state="actionable",
                        candidate_action="investigate",
                    ),
                ]
            )
        )
        safe_default = copy.deepcopy(compact["issues"][0]["defaultJudgment"])
        agent_judgments = {
            "schemaVersion": 1,
            "snapshotId": compact["snapshotId"],
            "issues": [
                {
                    **copy.deepcopy(compact["issues"][0]["defaultJudgment"]),
                    "category": "unknown",
                },
                {
                    **copy.deepcopy(compact["issues"][1]["defaultJudgment"]),
                    "category": "transient-infrastructure",
                },
            ],
        }

        merged = merge_ambiguous_poc_judgments(compact, agent_judgments)

        self.assertEqual(safe_default, merged["issues"][0])
        self.assertEqual("transient-infrastructure", merged["issues"][1]["category"])

    def test_merge_ambiguous_poc_judgments_accepts_review_required_watch_override(self) -> None:
        compact = build_compact_poc_input(
            _compact_prepared(
                [
                    _compact_issue(
                        411,
                        title="Port conflict on two independent jobs",
                        parsed_row_count=2,
                        ledger_rows=[
                            {"date": "2026-08-17", "sourceRun": 1001, "job": "Tests A"},
                            {"date": "2026-08-17", "sourceRun": 1002, "job": "Tests B"},
                        ],
                        tier1_cause_id="fixture-port-conflict",
                        tier2_test_name=None,
                        candidate_state="actionable",
                        candidate_action="investigate",
                    ),
                    _compact_issue(
                        412,
                        title="Single test timeout",
                        tier1_cause_id="single-test-timeout",
                        tier2_test_name="Namespace.Type.ProtectedSingleTest",
                        candidate_state="observing",
                        candidate_action="wait",
                    ),
                ]
            )
        )
        agent_judgments = {
            "schemaVersion": 1,
            "snapshotId": compact["snapshotId"],
            "issues": [
                {
                    **copy.deepcopy(compact["issues"][0]["defaultJudgment"]),
                    "category": "product-or-tooling",
                    "recommendations": [
                        {
                            **copy.deepcopy(
                                compact["issues"][0]["defaultJudgment"]["recommendations"][0]
                            ),
                            "disposition": "investigate",
                        }
                    ],
                },
                {
                    **copy.deepcopy(compact["issues"][1]["defaultJudgment"]),
                    "category": "unknown",
                },
            ],
        }

        merged = merge_ambiguous_poc_judgments(compact, agent_judgments)

        self.assertEqual("product-or-tooling", merged["issues"][0]["category"])
        self.assertEqual(
            compact["issues"][1]["defaultJudgment"],
            merged["issues"][1],
        )

    def test_merge_ambiguous_poc_judgments_preserves_superseded_defaults(self) -> None:
        test_name = "Namespace.Type.DuplicateTest"
        compact = build_compact_poc_input(
            _compact_prepared(
                [
                    _compact_issue(
                        421,
                        title="Duplicate test timed out",
                        tier1_cause_id="duplicate-test-timeout",
                        tier2_test_name=test_name,
                        ledger_rows=[
                            {"date": "2026-08-10", "sourceRun": 1001, "job": "Tests A"}
                        ],
                        candidate_state="observing",
                        candidate_action="wait",
                    ),
                    _compact_issue(
                        422,
                        title="Duplicate test timed out again",
                        tier1_cause_id="duplicate-test-rpc-timeout",
                        tier2_test_name=test_name,
                        ledger_rows=[
                            {"date": "2026-08-17", "sourceRun": 1002, "job": "Tests B"}
                        ],
                        candidate_state="observing",
                        candidate_action="wait",
                    ),
                ]
            )
        )
        agent_judgments = {
            "schemaVersion": 1,
            "snapshotId": compact["snapshotId"],
            "issues": [
                copy.deepcopy(compact["issues"][0]["defaultJudgment"]),
                {
                    **copy.deepcopy(compact["issues"][1]["defaultJudgment"]),
                    "recommendations": [
                        {
                            **copy.deepcopy(
                                compact["issues"][1]["defaultJudgment"]["recommendations"][0]
                            ),
                            "disposition": "investigate",
                        }
                    ],
                },
            ],
        }

        merged = merge_ambiguous_poc_judgments(compact, agent_judgments)

        self.assertEqual(
            compact["issues"][1]["defaultJudgment"],
            merged["issues"][1],
        )

    def test_build_compact_poc_input_stays_under_size_cap_for_many_issues(self) -> None:
        issues = [
            _compact_issue(
                number,
                title=f"Issue {number} with a long enough title to exercise compaction",
                producer="ci-failure-cause" if number % 3 else "tracking-issue",
                autoclose=number % 2 == 0,
                parsed_row_count=(number % 5) + 1,
                tier2_test_name=f"Namespace.Type.Test{number}" if number % 2 else None,
                candidate_state=("resolved" if number % 3 == 0 else "actionable" if number % 3 == 1 else "observing"),
                candidate_action=("recommend-close" if number % 3 == 0 else "investigate" if number % 3 == 1 else "wait"),
                blockers=[f"blocker-{number}"] if number % 4 == 0 else [],
                missing_prerequisites=[f"prereq-{number}"] if number % 4 == 1 else [],
                resolution_evidence={
                    "pullRequestEvidenceId": f"pr:{number}",
                    "runEvidenceId": f"run:{number}",
                    "latestOccurrence": f"2026-08-{(number % 28) + 1:02d}T21:24:23Z",
                } if number % 3 == 0 else {},
                bundle_size=9,
                payload_size=2048,
            )
            for number in range(1, 61)
        ]
        prepared = _compact_prepared(issues)

        compact = build_compact_poc_input(prepared)
        serialized = json.dumps(compact, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        # Cap raised from 120_000 to account for the historyOccurrenceSummary
        # field added to every compact issue (same shape as occurrenceSummary).
        self.assertLessEqual(len(serialized), 140_000)
        self.assertEqual(60, len(compact["issues"]))


def _override_judgments(
    compact: dict[str, object],
    issue_number: int,
    recommendation: dict[str, object],
    *,
    category: str = "flaky-test",
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "snapshotId": compact["snapshotId"],
        "issues": [
            {
                "issueNumber": issue_number,
                "category": category,
                "recommendations": [recommendation],
            }
        ],
    }


def _close_recommendation(issue_number: int) -> dict[str, object]:
    return {
        "disposition": "review-close",
        "target": {"kind": "issue", "value": issue_number},
        "confidence": "medium",
        "summary": "Close this issue.",
        "evidenceIds": [f"issue:{issue_number}"],
        "missingEvidence": [],
        "reassessWhen": "If the failure returns.",
    }


def _human_decision_compact_issue(issue_number: int = 309) -> dict[str, object]:
    return _compact_issue(
        issue_number,
        title='CI lane "Deployment Environment Cleanup" red',
        producer="ci-health-dashboard",
        tier2_test_name=None,
        candidate_state="actionable",
        candidate_action="investigate",
        labels=["area-deployment", "automation-broken"],
        issue_body=(
            "Workflow lane **Deployment Environment Cleanup** is red at tip.\n\n"
            "- Failing since 10h \u00b7 streak 10\n"
            "- Last run: https://github.com/owner/repo/actions/runs/9001\n"
            "- Assessment: Azure Login fails with AADSTS5000229 because "
            "tenant 'Aspire Testing' is expired.\n"
            "- Suggested: Renew or replace the expired Azure tenant or "
            "switch to a valid service principal, then rerun cleanup."
        ),
    )


class PocProjectabilityTests(unittest.TestCase):
    def test_rejects_review_close_without_recovery_or_duplicate_prerequisites(self) -> None:
        compact = build_compact_poc_input(
            _compact_prepared(
                [
                    _compact_issue(
                        101,
                        candidate_state="active",
                        candidate_action="investigate",
                        resolution_evidence={},
                    )
                ]
            )
        )
        judgments = _override_judgments(compact, 101, _close_recommendation(101))

        with self.assertRaisesRegex(ValidationError, "review-close"):
            validate_poc_projectability(compact, judgments)

    def test_accepts_review_close_backed_by_deterministic_resolution_evidence(self) -> None:
        compact = build_compact_poc_input(
            _compact_prepared(
                [
                    _compact_issue(
                        101,
                        candidate_state="resolved",
                        candidate_action="recommend-close",
                        resolution_evidence={
                            "pullRequestEvidenceId": "pr:101",
                            "runEvidenceId": "run:101",
                            "mergeCommitSha": "a" * 40,
                        },
                    )
                ]
            )
        )
        judgments = _override_judgments(compact, 101, _close_recommendation(101))

        validate_poc_projectability(compact, judgments)

    def test_rejects_ping_human_without_a_reported_human_decision(self) -> None:
        compact = build_compact_poc_input(
            _compact_prepared([_compact_issue(101, candidate_action="investigate")])
        )
        judgments = _override_judgments(
            compact,
            101,
            {
                "disposition": "ping-human",
                "target": {"kind": "issue", "value": 101},
                "confidence": "low",
                "summary": "Ask somebody about this.",
                "evidenceIds": ["issue:101"],
                "missingEvidence": [],
                "reassessWhen": "After human review.",
                "humanEscalation": {
                    "context": "The test keeps failing.",
                    "whyHuman": "Somebody should look.",
                    "question": "Who owns this?",
                    "suggestedNextSteps": ["Find an owner."],
                    "routingHint": "area-unknown",
                },
            },
        )

        with self.assertRaisesRegex(ValidationError, "ping-human"):
            validate_poc_projectability(compact, judgments)

    def test_rejects_ping_human_without_structured_human_escalation(self) -> None:
        compact = build_compact_poc_input(
            _compact_prepared([_human_decision_compact_issue(309)])
        )
        judgments = _override_judgments(
            compact,
            309,
            {
                "disposition": "ping-human",
                "target": {"kind": "issue", "value": 309},
                "confidence": "low",
                "summary": "Route this to a human.",
                "evidenceIds": ["issue:309"],
                "missingEvidence": [],
                "reassessWhen": "After human review.",
            },
            category="automation-tracker",
        )

        with self.assertRaisesRegex(ValidationError, "humanEscalation"):
            validate_poc_projectability(compact, judgments)

    def test_accepts_ping_human_grounded_in_a_reported_decision(self) -> None:
        compact = build_compact_poc_input(
            _compact_prepared([_human_decision_compact_issue(309)])
        )
        default = compact["issues"][0]["defaultJudgment"]
        judgments = {
            "schemaVersion": 1,
            "snapshotId": compact["snapshotId"],
            "issues": [default],
        }

        validate_poc_projectability(compact, judgments)

    def test_rejects_a_judgment_for_an_unprepared_issue(self) -> None:
        compact = build_compact_poc_input(_compact_prepared([_compact_issue(101)]))
        judgments = _override_judgments(compact, 999, _close_recommendation(999))

        with self.assertRaisesRegex(ValidationError, "non-prepared issue 999"):
            validate_poc_projectability(compact, judgments)

    def test_rejects_a_judgment_from_another_snapshot(self) -> None:
        compact = build_compact_poc_input(_compact_prepared([_compact_issue(101)]))
        judgments = _override_judgments(compact, 101, _close_recommendation(101))
        judgments["snapshotId"] = "snapshot:owner/repo:2020-01-01T00:00:00Z"

        with self.assertRaisesRegex(ValidationError, "snapshotId"):
            validate_poc_projectability(compact, judgments)
