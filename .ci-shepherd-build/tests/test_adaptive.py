from __future__ import annotations

import copy
from datetime import UTC, datetime
import shutil
import subprocess
from pathlib import Path
import unittest
from urllib.parse import quote

import ci_shepherd
from ci_shepherd.adaptive import AdaptiveEnricher
from ci_shepherd.models import (
    ValidationError,
    validate_evidence_requests,
    validate_report,
    validate_snapshot,
)


COLLECTED_AT = "2026-08-18T12:00:00Z"
NOW = datetime(2026, 8, 18, 13, 0, tzinfo=UTC)
TEST_ROOT = Path(__file__).resolve().parent


def association(source_issue_number: int) -> dict[str, object]:
    return {
        "sourceIssueNumber": source_issue_number,
        "sourceEvidenceId": f"issue:{source_issue_number}",
        "sourceUrl": f"https://github.com/owner/repo/issues/{source_issue_number}",
        "extractionMethod": "issue-body",
    }


def record(
    kind: str,
    url: str,
    *,
    availability: str = "available",
    **payload: object,
) -> dict[str, object]:
    return {
        "kind": kind,
        "url": url,
        "collectedAt": COLLECTED_AT,
        "availability": availability,
        "payload": payload,
    }


def issue_record(
    issue_number: int,
    *,
    availability: str = "available",
    facts: list[dict[str, object]] | None = None,
    referenced_by: list[dict[str, object]] | None = None,
    **payload: object,
) -> dict[str, object]:
    return record(
        "issue-event",
        f"https://github.com/owner/repo/issues/{issue_number}",
        availability=availability,
        number=issue_number,
        state="open",
        facts=facts or [],
        referencedBy=referenced_by or [],
        **payload,
    )


def baseline_snapshot(
    *,
    open_issues: list[int] | None = None,
    evidence: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    issue_numbers = open_issues or [1]
    records = {
        f"issue:{issue_number}": issue_record(issue_number)
        for issue_number in issue_numbers
    }
    if evidence:
        records.update(evidence)
    return {
        "schemaVersion": 1,
        "repository": "owner/repo",
        "collectedAt": COLLECTED_AT,
        "openIssues": issue_numbers,
        "issues": [
            {
                "number": issue_number,
                "state": "open",
                "title": f"Issue {issue_number}",
            }
            for issue_number in issue_numbers
        ],
        "supportingIssues": [],
        "evidence": records,
        "collectionErrors": [{"stage": "baseline", "message": "preserve me"}],
        "warnings": ["baseline warning"],
        "references": {str(issue_number): [] for issue_number in issue_numbers},
    }


def request_document(
    *requests: dict[str, object],
    round_number: int = 1,
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "repository": "owner/repo",
        "round": round_number,
        "requests": list(requests),
    }


def evidence_request(
    request_type: str,
    source_issue_number: int,
    evidence_id: str,
    decision_gate: str,
    **extra: object,
) -> dict[str, object]:
    request = {
        "type": request_type,
        "sourceIssueNumber": source_issue_number,
        "evidenceId": evidence_id,
        "decisionGate": decision_gate,
        "reason": "Collect evidence that can change the proposed action.",
    }
    request.update(extra)
    return request


def decision_report(
    issue_number: int,
    *,
    state: str,
    action: str,
    evidence: list[dict[str, object]],
    related_issues: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "repository": "owner/repo",
        "decisions": [
            {
                "issueNumber": issue_number,
                "issueUrl": f"https://github.com/owner/repo/issues/{issue_number}",
                "issueKind": "incident",
                "state": state,
                "proposedAction": action,
                "confidence": "high",
                "summary": "Evidence supports the bounded action.",
                "reasoning": "The current expanded snapshot satisfies every required gate.",
                "evidence": evidence,
                "contradictoryEvidence": [],
                "missingEvidence": [],
                "nextCondition": {
                    "type": "none",
                    "description": "No additional evidence is required.",
                },
                "suggestedOwners": [],
                "relatedIssues": related_issues or [],
                "changedSincePreviousRun": False,
            }
        ],
    }


class FakeClient:
    def __init__(
        self,
        responses: dict[str, object] | None = None,
        *,
        search_response: object | None = None,
    ) -> None:
        self.responses = responses or {}
        self.search_response = search_response
        self.calls: list[tuple[str, str]] = []

    def get(self, endpoint: str) -> object:
        self.calls.append(("GET", endpoint))
        response = (
            self.search_response
            if endpoint.startswith("/search/issues?")
            else self.responses.get(endpoint)
        )
        if isinstance(response, Exception):
            raise response
        if response is None:
            raise AssertionError(f"Unexpected GET endpoint: {endpoint}")
        return copy.deepcopy(response)

    def get_pages(self, endpoint: str, key: str | None = None) -> list[object]:
        self.calls.append(("GET", endpoint))
        response = self.responses.get(endpoint)
        if isinstance(response, Exception):
            raise response
        if response is None:
            raise AssertionError(f"Unexpected paged GET endpoint: {endpoint}")
        if isinstance(response, list):
            return copy.deepcopy(response)
        if isinstance(response, dict) and key is not None and isinstance(response.get(key), list):
            return copy.deepcopy(response[key])
        raise AssertionError(f"Unexpected paged response for {endpoint}: {response!r}")

    def get_text(self, endpoint: str, max_bytes: int = 200000) -> object:
        self.calls.append(("GET", endpoint))
        response = self.responses.get(endpoint)
        if isinstance(response, Exception):
            raise response
        if response is None:
            raise AssertionError(f"Unexpected text GET endpoint: {endpoint}")
        return response


def canonical_snapshot() -> dict[str, object]:
    facts = [
        {
            "field": "testName",
            "raw": "Namespace.Tests.IntermittentFailure",
            "normalized": "namespace.tests.intermittentfailure",
            "method": "labelled-line",
            "sourceEvidenceId": "issue:1",
        }
    ]
    return baseline_snapshot(
        evidence={
            "issue:1": issue_record(
                1,
                facts=facts,
                supportingSearch={
                    "complete": False,
                    "truncated": True,
                    "candidateIssueNumbers": [],
                },
            ),
            "run:42": record(
                "workflow-run",
                "https://github.com/owner/repo/actions/runs/42",
                runId=42,
                conclusion="failure",
                referencedBy=[association(1)],
            ),
        }
    )


def canonical_request() -> dict[str, object]:
    return evidence_request(
        "canonical-search",
        1,
        "issue:1",
        "canonical-search-complete",
        factField="testName",
    )


class AdaptiveEnricherTests(unittest.TestCase):
    def test_adaptive_enricher_is_exported_from_package(self) -> None:
        self.assertIs(AdaptiveEnricher, ci_shepherd.AdaptiveEnricher)

    def test_expand_deep_copies_input_and_preserves_baseline_errors(self) -> None:
        snapshot = canonical_snapshot()
        original = copy.deepcopy(snapshot)
        client = FakeClient(
            search_response={
                "total_count": 0,
                "incomplete_results": False,
                "items": [],
            }
        )

        expanded = AdaptiveEnricher(client, now=NOW).expand(
            snapshot,
            request_document(canonical_request()),
        )

        self.assertEqual(original, snapshot)
        self.assertIsNot(expanded, snapshot)
        self.assertEqual(original["collectionErrors"], expanded["collectionErrors"])
        self.assertEqual("complete", expanded["expansions"][0]["status"])
        self.assertEqual([], expanded["expansions"][0]["errors"])
        validate_snapshot(expanded)

    def test_independent_request_continues_after_partial_failure(self) -> None:
        snapshot = baseline_snapshot(
            evidence={
                "issue:2": issue_record(
                    2,
                    availability="partial",
                    referenced_by=[association(1)],
                ),
                "run:42": record(
                    "workflow-run",
                    "https://github.com/owner/repo/actions/runs/42",
                    availability="partial",
                    runId=42,
                    targetRepository="owner/repo",
                    referencedBy=[association(1)],
                ),
            }
        )
        client = FakeClient(
            {
                "/repos/owner/repo/issues/2": RuntimeError("issue unavailable"),
                "/repos/owner/repo/actions/runs/42": {
                    "id": 42,
                    "workflow_id": 9,
                    "name": "CI",
                    "event": "push",
                    "head_branch": "main",
                    "head_sha": "a" * 40,
                    "run_attempt": 1,
                    "status": "completed",
                    "conclusion": "failure",
                    "created_at": "2026-08-18T11:00:00Z",
                    "updated_at": "2026-08-18T11:10:00Z",
                    "run_started_at": "2026-08-18T11:00:00Z",
                    "html_url": "https://github.com/owner/repo/actions/runs/42",
                },
                "/repos/owner/repo/actions/runs/42/jobs?per_page=100": [],
                "/repos/owner/repo/actions/workflows/9/runs?branch=main&per_page=10": {
                    "total_count": 1,
                    "workflow_runs": [
                        {
                            "id": 42,
                            "run_attempt": 1,
                            "event": "push",
                            "head_branch": "main",
                            "head_sha": "a" * 40,
                            "conclusion": "failure",
                            "created_at": "2026-08-18T11:00:00Z",
                            "html_url": "https://github.com/owner/repo/actions/runs/42",
                        }
                    ],
                },
            }
        )
        requests = request_document(
            evidence_request("issue-reference", 1, "issue:2", "merged-fix"),
            evidence_request("workflow-run", 1, "run:42", "current-failing-run"),
        )

        expanded = AdaptiveEnricher(client, now=NOW).expand(snapshot, requests)

        self.assertEqual("partial", expanded["expansions"][0]["status"])
        self.assertEqual(1, len(expanded["expansions"][0]["errors"]))
        self.assertEqual(snapshot["collectionErrors"], expanded["collectionErrors"])
        self.assertEqual(snapshot["evidence"]["issue:2"], expanded["evidence"]["issue:2"])
        self.assertEqual("available", expanded["evidence"]["run:42"]["availability"])
        issue_call = client.calls.index(("GET", "/repos/owner/repo/issues/2"))
        run_call = client.calls.index(("GET", "/repos/owner/repo/actions/runs/42"))
        self.assertLess(issue_call, run_call)

    def test_partial_fix_and_run_become_enriched_and_allow_close_resolved(self) -> None:
        snapshot = baseline_snapshot(
            evidence={
                "issue:2": issue_record(
                    2,
                    availability="not-enriched",
                    referenced_by=[association(1)],
                    supportingBudgetExcluded=True,
                ),
                "run:42": record(
                    "workflow-run",
                    "https://github.com/owner/repo/actions/runs/42",
                    availability="partial",
                    runId=42,
                    targetRepository="owner/repo",
                    referencedBy=[association(1)],
                ),
            }
        )
        raw_issue = {
            "number": 2,
            "state": "closed",
            "title": "Fix the failure",
            "body": "Fixes #1",
            "html_url": "https://github.com/owner/repo/pull/2",
            "created_at": "2026-08-17T08:00:00Z",
            "updated_at": "2026-08-17T09:00:00Z",
            "closed_at": "2026-08-17T09:00:00Z",
            "labels": [],
            "pull_request": {"url": "https://api.github.com/repos/owner/repo/pulls/2"},
        }
        client = FakeClient(
            {
                "/repos/owner/repo/issues/2": raw_issue,
                "/repos/owner/repo/pulls/2": {
                    "number": 2,
                    "state": "closed",
                    "merged_at": "2026-08-17T09:00:00Z",
                    "merge_commit_sha": "a" * 40,
                    "html_url": "https://github.com/owner/repo/pull/2",
                    "base": {"ref": "main", "sha": "b" * 40},
                    "head": {
                        "ref": "fix",
                        "sha": "c" * 40,
                        "repo": {"full_name": "owner/repo"},
                    },
                },
                "/repos/owner/repo/pulls/2/files?per_page=100": [
                    {"filename": "src/Fix.cs", "status": "modified"}
                ],
                "/repos/owner/repo/actions/runs/42": {
                    "id": 42,
                    "workflow_id": 9,
                    "name": "CI",
                    "event": "push",
                    "head_branch": "main",
                    "head_sha": "a" * 40,
                    "run_attempt": 1,
                    "status": "completed",
                    "conclusion": "failure",
                    "created_at": "2026-08-17T10:00:00Z",
                    "updated_at": "2026-08-17T10:10:00Z",
                    "run_started_at": "2026-08-17T10:00:00Z",
                    "html_url": "https://github.com/owner/repo/actions/runs/42",
                },
                "/repos/owner/repo/actions/runs/42/jobs?per_page=100": [],
                "/repos/owner/repo/actions/workflows/9/runs?branch=main&per_page=10": {
                    "total_count": 2,
                    "workflow_runs": [
                        {
                            "id": 43,
                            "run_attempt": 1,
                            "event": "push",
                            "head_branch": "main",
                            "head_sha": "d" * 40,
                            "conclusion": "success",
                            "created_at": "2026-08-17T11:00:00Z",
                            "html_url": "https://github.com/owner/repo/actions/runs/43",
                        },
                        {
                            "id": 42,
                            "run_attempt": 1,
                            "event": "push",
                            "head_branch": "main",
                            "head_sha": "a" * 40,
                            "conclusion": "failure",
                            "created_at": "2026-08-17T10:00:00Z",
                            "html_url": "https://github.com/owner/repo/actions/runs/42",
                        },
                    ],
                },
            }
        )
        requests = request_document(
            evidence_request("issue-reference", 1, "issue:2", "merged-fix"),
            evidence_request("workflow-run", 1, "run:42", "post-fix-green"),
        )

        expanded = AdaptiveEnricher(client, now=NOW).expand(snapshot, requests)

        self.assertNotIn("issue:2", expanded["evidence"])
        self.assertEqual("available", expanded["evidence"]["pr:2"]["availability"])
        self.assertEqual("2026-08-17T09:00:00Z", expanded["evidence"]["pr:2"]["payload"]["mergedAt"])
        self.assertTrue(expanded["evidence"]["run:42"]["payload"]["historyCoversSourceRun"])
        self.assertEqual(2, expanded["evidence"]["run:42"]["payload"]["recentHistoryTotalCount"])

        report = decision_report(
            1,
            state="resolved",
            action="close-resolved",
            evidence=[
                {"id": "issue:1", "kind": "issue-event"},
                {"id": "pr:2", "kind": "pull-request", "role": "merged-fix"},
                {
                    "id": "run:42",
                    "kind": "workflow-run",
                    "roles": ["post-fix-green", "no-newer-matching-failure"],
                },
            ],
        )
        validate_report(expanded, report)

    def test_issue_enrichment_preserves_all_source_associations(self) -> None:
        body_association = association(1)
        comment_association = {
            **association(1),
            "sourceEvidenceId": "issue:1:comment:17",
            "sourceUrl": "https://github.com/owner/repo/issues/1#issuecomment-17",
            "extractionMethod": "comment",
        }
        snapshot = baseline_snapshot(
            evidence={
                "issue:2": issue_record(
                    2,
                    availability="partial",
                    referenced_by=[comment_association, body_association],
                )
            }
        )
        client = FakeClient(
            {
                "/repos/owner/repo/issues/2": {
                    "number": 2,
                    "state": "closed",
                    "title": "Referenced issue",
                    "body": "",
                    "html_url": "https://github.com/owner/repo/issues/2",
                    "created_at": "2026-08-01T00:00:00Z",
                    "updated_at": "2026-08-18T00:00:00Z",
                    "closed_at": "2026-08-18T00:00:00Z",
                    "labels": [],
                }
            }
        )

        expanded = AdaptiveEnricher(client, now=NOW).expand(
            snapshot,
            request_document(
                evidence_request("issue-reference", 1, "issue:2", "prior-resolved-episode")
            ),
        )

        self.assertEqual(
            [body_association, comment_association],
            expanded["evidence"]["issue:2"]["payload"]["referencedBy"],
        )

    def test_issue_to_pull_request_expansion_preserves_shared_source_associations(self) -> None:
        shared_associations = [association(1), association(5)]
        snapshot = baseline_snapshot(
            open_issues=[1, 5],
            evidence={
                "issue:2": issue_record(
                    2,
                    availability="not-enriched",
                    referenced_by=shared_associations,
                )
            },
        )
        client = FakeClient(
            {
                "/repos/owner/repo/issues/2": {
                    "number": 2,
                    "state": "closed",
                    "title": "Shared fix",
                    "body": "",
                    "html_url": "https://github.com/owner/repo/pull/2",
                    "created_at": "2026-08-17T08:00:00Z",
                    "updated_at": "2026-08-17T09:00:00Z",
                    "closed_at": "2026-08-17T09:00:00Z",
                    "labels": [],
                    "pull_request": {
                        "url": "https://api.github.com/repos/owner/repo/pulls/2"
                    },
                },
                "/repos/owner/repo/pulls/2": {
                    "number": 2,
                    "state": "closed",
                    "merged_at": "2026-08-17T09:00:00Z",
                    "merge_commit_sha": "a" * 40,
                    "html_url": "https://github.com/owner/repo/pull/2",
                    "base": {"ref": "main", "sha": "b" * 40},
                    "head": {
                        "ref": "fix",
                        "sha": "c" * 40,
                        "repo": {"full_name": "owner/repo"},
                    },
                },
                "/repos/owner/repo/pulls/2/files?per_page=100": [],
            }
        )

        expanded = AdaptiveEnricher(client, now=NOW).expand(
            snapshot,
            request_document(
                evidence_request("issue-reference", 1, "issue:2", "merged-fix")
            ),
        )

        self.assertNotIn("issue:2", expanded["evidence"])
        self.assertEqual(
            shared_associations,
            expanded["evidence"]["pr:2"]["payload"]["referencedBy"],
        )

        round_two_snapshot = copy.deepcopy(expanded)
        round_two_snapshot["evidence"]["pr:2"]["availability"] = "partial"
        round_two_requests = validate_evidence_requests(
            round_two_snapshot,
            request_document(
                evidence_request("issue-reference", 5, "pr:2", "merged-fix"),
                round_number=2,
            ),
        )
        self.assertEqual(5, round_two_requests[0]["sourceIssueNumber"])

    def test_workflow_run_expansion_preserves_shared_source_associations(self) -> None:
        shared_associations = [association(1), association(5)]
        snapshot = baseline_snapshot(
            open_issues=[1, 5],
            evidence={
                "run:42": record(
                    "workflow-run",
                    "https://github.com/owner/repo/actions/runs/42",
                    availability="partial",
                    runId=42,
                    targetRepository="owner/repo",
                    referencedBy=shared_associations,
                )
            },
        )
        client = FakeClient(
            {
                "/repos/owner/repo/actions/runs/42": {
                    "id": 42,
                    "workflow_id": 9,
                    "name": "CI",
                    "event": "push",
                    "head_branch": "main",
                    "head_sha": "a" * 40,
                    "run_attempt": 1,
                    "status": "completed",
                    "conclusion": "failure",
                    "created_at": "2026-08-18T11:00:00Z",
                    "updated_at": "2026-08-18T11:10:00Z",
                    "run_started_at": "2026-08-18T11:00:00Z",
                    "html_url": "https://github.com/owner/repo/actions/runs/42",
                },
                "/repos/owner/repo/actions/runs/42/jobs?per_page=100": [],
                "/repos/owner/repo/actions/workflows/9/runs?branch=main&per_page=10": {
                    "total_count": 1,
                    "workflow_runs": [
                        {
                            "id": 42,
                            "run_attempt": 1,
                            "event": "push",
                            "head_branch": "main",
                            "head_sha": "a" * 40,
                            "conclusion": "failure",
                            "created_at": "2026-08-18T11:00:00Z",
                            "html_url": "https://github.com/owner/repo/actions/runs/42",
                        }
                    ],
                },
            }
        )

        expanded = AdaptiveEnricher(client, now=NOW).expand(
            snapshot,
            request_document(
                evidence_request(
                    "workflow-run",
                    1,
                    "run:42",
                    "current-failing-run",
                )
            ),
        )

        self.assertEqual(
            shared_associations,
            expanded["evidence"]["run:42"]["payload"]["referencedBy"],
        )

        round_two_snapshot = copy.deepcopy(expanded)
        round_two_snapshot["evidence"]["run:42"]["availability"] = "partial"
        round_two_requests = validate_evidence_requests(
            round_two_snapshot,
            request_document(
                evidence_request(
                    "workflow-run",
                    5,
                    "run:42",
                    "current-failing-run",
                ),
                round_number=2,
            ),
        )
        self.assertEqual(5, round_two_requests[0]["sourceIssueNumber"])

    def test_failed_workflow_run_expansion_preserves_baseline_record(self) -> None:
        snapshot = baseline_snapshot(
            evidence={
                "run:42": record(
                    "workflow-run",
                    "https://github.com/owner/repo/actions/runs/42",
                    availability="partial",
                    runId=42,
                    targetRepository="owner/repo",
                    runBudgetExcluded=True,
                    referencedBy=[association(1)],
                )
            }
        )
        client = FakeClient(
            {
                "/repos/owner/repo/actions/runs/42": RuntimeError("run unavailable"),
            }
        )

        expanded = AdaptiveEnricher(client, now=NOW).expand(
            snapshot,
            request_document(
                evidence_request("workflow-run", 1, "run:42", "current-failing-run")
            ),
        )

        self.assertEqual(snapshot["evidence"]["run:42"], expanded["evidence"]["run:42"])
        self.assertEqual("partial", expanded["expansions"][0]["status"])
        self.assertEqual(1, len(expanded["expansions"][0]["errors"]))

    def test_less_complete_workflow_retry_preserves_job_truncation(self) -> None:
        baseline_jobs = [
            {"attempt": 2, "jobId": job_id, "conclusion": "failure"}
            for job_id in range(1, 11)
        ]
        baseline = record(
            "workflow-run",
            "https://github.com/owner/repo/actions/runs/42",
            attempt=2,
            attempts=[2],
            jobs=baseline_jobs,
            totalFailedJobs=12,
            jobsTruncated=True,
        )
        retry = record(
            "workflow-run",
            "https://github.com/owner/repo/actions/runs/42",
            attempt=2,
            attempts=[2],
            jobs=baseline_jobs[:1],
            totalFailedJobs=1,
            jobsTruncated=False,
        )

        merged = AdaptiveEnricher._merge_workflow_run_record(baseline, retry, [])

        self.assertEqual(10, len(merged["payload"]["jobs"]))
        self.assertEqual(12, merged["payload"]["totalFailedJobs"])
        self.assertTrue(merged["payload"]["jobsTruncated"])

    def test_complete_workflow_retry_clears_job_truncation(self) -> None:
        baseline_jobs = [
            {"attempt": 2, "jobId": job_id, "conclusion": "failure"}
            for job_id in range(1, 11)
        ]
        baseline = record(
            "workflow-run",
            "https://github.com/owner/repo/actions/runs/42",
            attempt=2,
            attempts=[2],
            jobs=baseline_jobs,
            totalFailedJobs=12,
            jobsTruncated=True,
        )
        retry = record(
            "workflow-run",
            "https://github.com/owner/repo/actions/runs/42",
            attempt=2,
            attempts=[2],
            jobs=[
                {"attempt": 2, "jobId": job_id, "conclusion": "failure"}
                for job_id in range(1, 13)
            ],
            totalFailedJobs=12,
            jobsTruncated=False,
        )

        merged = AdaptiveEnricher._merge_workflow_run_record(baseline, retry, [])

        self.assertEqual(12, len(merged["payload"]["jobs"]))
        self.assertEqual(12, merged["payload"]["totalFailedJobs"])
        self.assertFalse(merged["payload"]["jobsTruncated"])

    def test_workflow_retry_preserves_truncation_when_job_counts_are_unknown(
        self,
    ) -> None:
        baseline = record(
            "workflow-run",
            "https://github.com/owner/repo/actions/runs/42",
            attempt=2,
            jobs=[{"attempt": 2, "jobId": 1, "conclusion": "failure"}],
            jobsTruncated=True,
        )
        retry = record(
            "workflow-run",
            "https://github.com/owner/repo/actions/runs/42",
            attempt=2,
            jobs=[{"attempt": 2, "jobId": 1, "conclusion": "failure"}],
            jobsTruncated=False,
        )

        merged = AdaptiveEnricher._merge_workflow_run_record(baseline, retry, [])

        self.assertTrue(merged["payload"]["jobsTruncated"])

    def test_workflow_run_uses_current_attempt_failed_job_and_log_budgets(self) -> None:
        snapshot = baseline_snapshot(
            evidence={
                "run:42": record(
                    "workflow-run",
                    "https://github.com/owner/repo/actions/runs/42",
                    availability="partial",
                    runId=42,
                    targetRepository="owner/repo",
                    referencedBy=[association(1)],
                )
            }
        )
        failed_jobs = [
            {
                "id": job_id,
                "run_attempt": 2,
                "name": f"job-{job_id}",
                "status": "completed",
                "conclusion": "failure",
                "started_at": "2026-08-18T11:00:00Z",
                "completed_at": "2026-08-18T11:01:00Z",
                "html_url": f"https://github.com/owner/repo/actions/runs/42/job/{job_id}",
                "steps": [],
            }
            for job_id in range(1, 13)
        ]
        client = FakeClient(
            {
                "/repos/owner/repo/actions/runs/42": {
                    "id": 42,
                    "workflow_id": 9,
                    "name": "CI",
                    "event": "push",
                    "head_branch": "main",
                    "head_sha": "a" * 40,
                    "run_attempt": 2,
                    "status": "completed",
                    "conclusion": "failure",
                    "created_at": "2026-08-18T11:00:00Z",
                    "updated_at": "2026-08-18T11:10:00Z",
                    "run_started_at": "2026-08-18T11:00:00Z",
                    "html_url": "https://github.com/owner/repo/actions/runs/42",
                },
                "/repos/owner/repo/actions/runs/42/jobs?per_page=100": failed_jobs,
                "/repos/owner/repo/actions/workflows/9/runs?branch=main&per_page=10": {
                    "total_count": 1,
                    "workflow_runs": [
                        {
                            "id": 42,
                            "run_attempt": 2,
                            "event": "push",
                            "head_branch": "main",
                            "head_sha": "a" * 40,
                            "conclusion": "failure",
                            "created_at": "2026-08-18T11:00:00Z",
                            "html_url": "https://github.com/owner/repo/actions/runs/42",
                        }
                    ],
                },
                **{
                    f"/repos/owner/repo/actions/jobs/{job_id}/logs": RuntimeError("expired")
                    for job_id in range(1, 4)
                },
            }
        )

        expanded = AdaptiveEnricher(client, now=NOW).expand(
            snapshot,
            request_document(
                evidence_request("workflow-run", 1, "run:42", "current-failing-run")
            ),
        )

        run_payload = expanded["evidence"]["run:42"]["payload"]
        self.assertEqual(12, run_payload["totalFailedJobs"])
        self.assertEqual(10, len(run_payload["jobs"]))
        self.assertTrue(run_payload["jobsTruncated"])
        log_calls = [endpoint for _, endpoint in client.calls if endpoint.endswith("/logs")]
        self.assertEqual(3, len(log_calls))
        self.assertFalse(any("/attempts/1/" in endpoint for _, endpoint in client.calls))

    def test_workflow_history_outside_bounded_window_is_not_complete_evidence(self) -> None:
        snapshot = baseline_snapshot(
            evidence={
                "run:42": record(
                    "workflow-run",
                    "https://github.com/owner/repo/actions/runs/42",
                    availability="partial",
                    runId=42,
                    targetRepository="owner/repo",
                    referencedBy=[association(1)],
                )
            }
        )
        client = FakeClient(
            {
                "/repos/owner/repo/actions/runs/42": {
                    "id": 42,
                    "workflow_id": 9,
                    "name": "CI",
                    "event": "push",
                    "head_branch": "main",
                    "head_sha": "a" * 40,
                    "run_attempt": 1,
                    "status": "completed",
                    "conclusion": "failure",
                    "created_at": "2026-08-01T11:00:00Z",
                    "updated_at": "2026-08-01T11:10:00Z",
                    "run_started_at": "2026-08-01T11:00:00Z",
                    "html_url": "https://github.com/owner/repo/actions/runs/42",
                },
                "/repos/owner/repo/actions/runs/42/jobs?per_page=100": [],
                "/repos/owner/repo/actions/workflows/9/runs?branch=main&per_page=10": {
                    "total_count": 11,
                    "workflow_runs": [
                        {
                            "id": run_id,
                            "run_attempt": 1,
                            "event": "push",
                            "head_branch": "main",
                            "head_sha": f"{run_id:040x}",
                            "conclusion": "success",
                            "created_at": f"2026-08-{18 - index:02d}T11:00:00Z",
                            "html_url": f"https://github.com/owner/repo/actions/runs/{run_id}",
                        }
                        for index, run_id in enumerate(range(100, 110))
                    ],
                },
            }
        )

        expanded = AdaptiveEnricher(client, now=NOW).expand(
            snapshot,
            request_document(
                evidence_request(
                    "workflow-run",
                    1,
                    "run:42",
                    "no-newer-matching-failure",
                )
            ),
        )

        run_payload = expanded["evidence"]["run:42"]["payload"]
        self.assertTrue(run_payload["recentHistoryCollected"])
        self.assertTrue(run_payload["recentHistoryTruncated"])
        self.assertFalse(run_payload["historyCoversSourceRun"])
        self.assertEqual("partial", expanded["expansions"][0]["status"])

    def test_workflow_history_failure_can_be_retried_in_round_two(self) -> None:
        snapshot = baseline_snapshot(
            evidence={
                "run:42": record(
                    "workflow-run",
                    "https://github.com/owner/repo/actions/runs/42",
                    availability="partial",
                    runId=42,
                    targetRepository="owner/repo",
                    referencedBy=[association(1)],
                )
            }
        )
        raw_run = {
            "id": 42,
            "workflow_id": 9,
            "name": "CI",
            "event": "push",
            "head_branch": "main",
            "head_sha": "a" * 40,
            "run_attempt": 1,
            "status": "completed",
            "conclusion": "failure",
            "created_at": "2026-08-18T11:00:00Z",
            "updated_at": "2026-08-18T11:10:00Z",
            "run_started_at": "2026-08-18T11:00:00Z",
            "html_url": "https://github.com/owner/repo/actions/runs/42",
        }
        history_endpoint = "/repos/owner/repo/actions/workflows/9/runs?branch=main&per_page=10"
        round_one = AdaptiveEnricher(
            FakeClient(
                {
                    "/repos/owner/repo/actions/runs/42": raw_run,
                    "/repos/owner/repo/actions/runs/42/jobs?per_page=100": [],
                    history_endpoint: RuntimeError("history unavailable"),
                }
            ),
            now=NOW,
        ).expand(
            snapshot,
            request_document(
                evidence_request(
                    "workflow-run",
                    1,
                    "run:42",
                    "no-newer-matching-failure",
                )
            ),
        )

        self.assertEqual("available", round_one["evidence"]["run:42"]["availability"])
        self.assertFalse(
            round_one["evidence"]["run:42"]["payload"]["recentHistoryCollected"]
        )
        self.assertEqual("partial", round_one["expansions"][0]["status"])

        round_two = AdaptiveEnricher(
            FakeClient(
                {
                    "/repos/owner/repo/actions/runs/42": raw_run,
                    "/repos/owner/repo/actions/runs/42/jobs?per_page=100": [],
                    history_endpoint: {
                        "total_count": 1,
                        "workflow_runs": [
                            {
                                "id": 42,
                                "run_attempt": 1,
                                "event": "push",
                                "head_branch": "main",
                                "head_sha": "a" * 40,
                                "conclusion": "failure",
                                "created_at": "2026-08-18T11:00:00Z",
                                "html_url": "https://github.com/owner/repo/actions/runs/42",
                            }
                        ],
                    },
                }
            ),
            now=NOW,
        ).expand(
            round_one,
            request_document(
                evidence_request(
                    "workflow-run",
                    1,
                    "run:42",
                    "no-newer-matching-failure",
                ),
                round_number=2,
            ),
        )

        self.assertEqual(["partial", "complete"], [
            expansion["status"] for expansion in round_two["expansions"]
        ])
        self.assertTrue(
            round_two["evidence"]["run:42"]["payload"]["recentHistoryCollected"]
        )
        self.assertTrue(
            round_two["evidence"]["run:42"]["payload"]["historyCoversSourceRun"]
        )
        self.assertEqual(
            [42],
            [
                run["runId"]
                for run in round_two["evidence"]["run:42"]["payload"]["recentHistory"]
            ],
        )
        self.assertEqual(
            "",
            round_two["evidence"]["run:42"]["payload"]["recentHistoryGap"],
        )

    def test_round_two_history_failure_preserves_prior_complete_stages(self) -> None:
        history = [
            {
                "runId": run_id,
                "attempt": 1,
                "event": "push",
                "branch": "main",
                "headSha": f"{run_id:040x}",
                "conclusion": "success",
                "createdAt": f"2026-08-{18 - index:02d}T11:00:00Z",
                "url": f"https://github.com/owner/repo/actions/runs/{run_id}",
            }
            for index, run_id in enumerate(range(100, 110))
        ]
        job_payload = {
            "runId": 42,
            "targetRepository": "owner/repo",
            "attempt": 1,
            "jobId": 7,
            "checkRunId": 70,
            "name": "tests",
            "status": "completed",
            "conclusion": "failure",
            "startedAt": "2026-08-18T11:00:00Z",
            "completedAt": "2026-08-18T11:05:00Z",
            "url": "https://github.com/owner/repo/actions/runs/42/job/7",
            "steps": [],
            "annotationEvidenceIds": [],
            "logEvidenceId": "run:42:attempt:1:job:7:log",
            "referencedBy": [association(1)],
        }
        run_payload = {
            "runId": 42,
            "targetRepository": "owner/repo",
            "workflowId": 9,
            "workflow": "CI",
            "event": "push",
            "branch": "main",
            "headSha": "a" * 40,
            "attempt": 1,
            "status": "completed",
            "conclusion": "failure",
            "createdAt": "2026-08-01T11:00:00Z",
            "updatedAt": "2026-08-01T11:10:00Z",
            "runStartedAt": "2026-08-01T11:00:00Z",
            "rerunIdentity": {
                "workflowId": 9,
                "event": "push",
                "branch": "main",
            },
            "attempts": [1],
            "jobs": [job_payload],
            "totalFailedJobs": 1,
            "jobsTruncated": False,
            "artifacts": [],
            "recentHistory": history,
            "recentHistoryCollected": True,
            "recentHistoryTruncated": True,
            "recentHistoryTotalCount": 11,
            "historyCoversSourceRun": False,
            "recentHistoryGap": "source-run-outside-bounded-window",
            "referencedBy": [association(1)],
        }
        snapshot = baseline_snapshot(
            evidence={
                "run:42": record(
                    "workflow-run",
                    "https://github.com/owner/repo/actions/runs/42",
                    **run_payload,
                ),
                "run:42:attempt:1:job:7": record(
                    "workflow-job",
                    "https://github.com/owner/repo/actions/runs/42/job/7",
                    **{
                        key: value
                        for key, value in job_payload.items()
                        if key != "url"
                    },
                ),
                "run:42:attempt:1:job:7:log": record(
                    "workflow-log",
                    "https://github.com/owner/repo/actions/runs/42/job/7",
                    evidenceId="run:42:attempt:1:job:7:log",
                    runId=42,
                    attempt=1,
                    jobId=7,
                    targetRepository="owner/repo",
                    excerpt="prior complete log",
                    truncated=False,
                    status=200,
                    referencedBy=[association(1)],
                ),
            }
        )
        snapshot["expansions"] = [
            {
                "round": 1,
                "requests": [
                    evidence_request(
                        "workflow-run",
                        1,
                        "run:42",
                        "no-newer-matching-failure",
                    )
                ],
                "status": "partial",
                "errors": [],
            }
        ]
        baseline_records = copy.deepcopy(snapshot["evidence"])
        raw_run = {
            "id": 42,
            "workflow_id": 9,
            "name": "CI",
            "event": "push",
            "head_branch": "main",
            "head_sha": "a" * 40,
            "run_attempt": 1,
            "status": "completed",
            "conclusion": "failure",
            "created_at": "2026-08-01T11:00:00Z",
            "updated_at": "2026-08-01T11:10:00Z",
            "run_started_at": "2026-08-01T11:00:00Z",
            "html_url": "https://github.com/owner/repo/actions/runs/42",
        }
        history_endpoint = "/repos/owner/repo/actions/workflows/9/runs?branch=main&per_page=10"

        expanded = AdaptiveEnricher(
            FakeClient(
                {
                    "/repos/owner/repo/actions/runs/42": raw_run,
                    "/repos/owner/repo/actions/runs/42/jobs?per_page=100": [
                        {
                            "id": 7,
                            "run_id": 42,
                            "run_attempt": 1,
                            "check_run_url": "https://api.github.com/repos/owner/repo/check-runs/70",
                            "name": "tests",
                            "status": "completed",
                            "conclusion": "failure",
                            "started_at": "2026-08-18T11:00:00Z",
                            "completed_at": "2026-08-18T11:05:00Z",
                            "html_url": "https://github.com/owner/repo/actions/runs/42/job/7",
                            "steps": [],
                        }
                    ],
                    "/repos/owner/repo/actions/jobs/7/logs": RuntimeError(
                        "log unavailable"
                    ),
                    history_endpoint: RuntimeError("history unavailable"),
                }
            ),
            now=NOW,
        ).expand(
            snapshot,
            request_document(
                evidence_request(
                    "workflow-run",
                    1,
                    "run:42",
                    "no-newer-matching-failure",
                ),
                round_number=2,
            ),
        )

        retry_manifest = expanded["expansions"][1]
        self.assertEqual("partial", retry_manifest["status"])
        self.assertEqual(["workflow-log", "workflow-history"], [
            error["stage"] for error in retry_manifest["errors"]
        ])
        run_record = expanded["evidence"]["run:42"]
        self.assertEqual("available", run_record["availability"])
        self.assertEqual(history, run_record["payload"]["recentHistory"])
        self.assertTrue(run_record["payload"]["recentHistoryCollected"])
        self.assertTrue(run_record["payload"]["recentHistoryTruncated"])
        self.assertEqual(11, run_record["payload"]["recentHistoryTotalCount"])
        self.assertFalse(run_record["payload"]["historyCoversSourceRun"])
        self.assertEqual([job_payload], run_record["payload"]["jobs"])
        self.assertFalse(run_record["payload"]["jobsTruncated"])
        self.assertEqual(
            "available",
            expanded["evidence"]["run:42:attempt:1:job:7"]["availability"],
        )
        self.assertEqual(
            [association(1)],
            expanded["evidence"]["run:42:attempt:1:job:7"]["payload"]["referencedBy"],
        )
        self.assertEqual(
            baseline_records["run:42:attempt:1:job:7:log"],
            expanded["evidence"]["run:42:attempt:1:job:7:log"],
        )


class CanonicalSearchTests(unittest.TestCase):
    def test_zero_complete_results_support_open_dedicated_issue(self) -> None:
        snapshot = canonical_snapshot()
        client = FakeClient(
            search_response={
                "total_count": 0,
                "incomplete_results": False,
                "items": [],
            }
        )

        expanded = AdaptiveEnricher(client, now=NOW).expand(
            snapshot,
            request_document(canonical_request()),
        )

        search = expanded["evidence"]["issue:1"]["payload"]["supportingSearch"]
        self.assertEqual(0, search["totalCount"])
        self.assertEqual(0, search["returnedCount"])
        self.assertEqual([], search["candidateIssueNumbers"])
        self.assertFalse(search["truncated"])
        self.assertTrue(search["complete"])
        self.assertEqual(
            {
                "field": "testName",
                "value": "Namespace.Tests.IntermittentFailure",
                "normalized": "namespace.tests.intermittentfailure",
            },
            search["queryFact"],
        )
        self.assertNotIn("role", expanded["evidence"]["issue:1"]["payload"])

        report = decision_report(
            1,
            state="actionable",
            action="open-dedicated-issue",
            evidence=[
                {
                    "id": "issue:1",
                    "kind": "issue-event",
                    "role": "canonical-search-complete",
                },
                {
                    "id": "run:42",
                    "kind": "workflow-run",
                    "roles": ["current-failing-run", "recurrence"],
                },
            ],
        )
        validate_report(expanded, report)

        search_endpoints = [endpoint for _, endpoint in client.calls if endpoint.startswith("/search/issues?")]
        self.assertEqual(1, len(search_endpoints))
        self.assertIn("per_page=20", search_endpoints[0])
        self.assertIn("page=1", search_endpoints[0])

    def test_one_result_supplies_associated_canonical_evidence_for_close_as_tracked(self) -> None:
        snapshot = canonical_snapshot()
        client = FakeClient(
            search_response={
                "total_count": 1,
                "incomplete_results": False,
                "items": [
                    {
                        "number": 2,
                        "state": "open",
                        "title": "Track intermittent test",
                        "body": "Canonical problem issue",
                        "html_url": "https://github.com/owner/repo/issues/2",
                        "created_at": "2026-08-01T00:00:00Z",
                        "updated_at": "2026-08-18T00:00:00Z",
                        "closed_at": None,
                        "labels": [{"name": "test-failure"}],
                    }
                ],
            }
        )

        expanded = AdaptiveEnricher(client, now=NOW).expand(
            snapshot,
            request_document(canonical_request()),
        )

        candidate = expanded["evidence"]["issue:2"]
        self.assertEqual("available", candidate["availability"])
        self.assertEqual(
            [
                {
                    "sourceIssueNumber": 1,
                    "sourceEvidenceId": "issue:1",
                    "sourceUrl": "https://github.com/owner/repo/issues/1",
                    "extractionMethod": "adaptive-expansion",
                }
            ],
            candidate["payload"]["referencedBy"],
        )
        self.assertNotIn("role", candidate["payload"])
        self.assertEqual(
            [2],
            expanded["evidence"]["issue:1"]["payload"]["supportingSearch"]["candidateIssueNumbers"],
        )
        report = decision_report(
            1,
            state="tracked-elsewhere",
            action="close-as-tracked",
            evidence=[
                {"id": "issue:1", "kind": "issue-event"},
                {"id": "run:42", "kind": "workflow-run"},
                {"id": "issue:2", "kind": "issue-event", "role": "canonical-issue"},
            ],
            related_issues=[
                {
                    "type": "canonical-tracker",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )
        validate_report(expanded, report)

    def test_minimal_search_item_is_incomplete_and_cannot_support_close_as_tracked(self) -> None:
        expanded = AdaptiveEnricher(
            FakeClient(
                search_response={
                    "total_count": 1,
                    "incomplete_results": False,
                    "items": [
                        {
                            "number": 2,
                            "html_url": "https://github.com/owner/repo/issues/2",
                        }
                    ],
                }
            ),
            now=NOW,
        ).expand(
            canonical_snapshot(),
            request_document(canonical_request()),
        )

        search = expanded["evidence"]["issue:1"]["payload"]["supportingSearch"]
        self.assertFalse(search["complete"])
        self.assertEqual(0, search["returnedCount"])
        self.assertEqual([], search["candidateIssueNumbers"])
        self.assertNotIn("issue:2", expanded["evidence"])
        self.assertEqual("partial", expanded["expansions"][0]["status"])

        report = decision_report(
            1,
            state="tracked-elsewhere",
            action="close-as-tracked",
            evidence=[
                {"id": "issue:1", "kind": "issue-event"},
                {"id": "issue:2", "kind": "issue-event", "role": "canonical-issue"},
            ],
            related_issues=[
                {
                    "type": "canonical-tracker",
                    "sourceIssueNumber": 1,
                    "targetIssueNumber": 2,
                }
            ],
        )
        with self.assertRaisesRegex(ValidationError, "Unknown evidence ID"):
            validate_report(expanded, report)

    def test_canonical_search_rejects_malformed_issue_evidence_items(self) -> None:
        valid_item = {
            "number": 2,
            "state": "open",
            "title": "Track intermittent test",
            "html_url": "https://github.com/owner/repo/issues/2",
            "created_at": "2026-08-01T00:00:00Z",
            "updated_at": "2026-08-18T00:00:00Z",
        }
        malformed_items = (
            {**valid_item, "number": True},
            {**valid_item, "html_url": ""},
            {**valid_item, "title": ""},
            {**valid_item, "state": "draft"},
            {**valid_item, "created_at": ""},
            {**valid_item, "created_at": "not-a-timestamp"},
            {**valid_item, "updated_at": ""},
            {**valid_item, "updated_at": "not-a-timestamp"},
            {
                **valid_item,
                "pull_request": {
                    "url": "https://api.github.com/repos/owner/repo/pulls/2",
                },
            },
        )

        for malformed_item in malformed_items:
            with self.subTest(item=malformed_item):
                expanded = AdaptiveEnricher(
                    FakeClient(
                        search_response={
                            "total_count": 1,
                            "incomplete_results": False,
                            "items": [malformed_item],
                        }
                    ),
                    now=NOW,
                ).expand(
                    canonical_snapshot(),
                    request_document(canonical_request()),
                )

                search = expanded["evidence"]["issue:1"]["payload"]["supportingSearch"]
                self.assertFalse(search["complete"])
                self.assertEqual(0, search["returnedCount"])
                self.assertEqual([], search["candidateIssueNumbers"])
                self.assertNotIn("issue:2", expanded["evidence"])
                self.assertEqual("partial", expanded["expansions"][0]["status"])
                self.assertEqual(1, len(expanded["expansions"][0]["errors"]))

    def test_canonical_search_preserves_all_baseline_associations_for_later_round(self) -> None:
        snapshot = canonical_snapshot()
        snapshot["openIssues"] = [1, 5]
        snapshot["issues"].append({"number": 5, "state": "open", "title": "Issue 5"})
        snapshot["references"]["5"] = []
        snapshot["evidence"]["issue:5"] = issue_record(5)
        snapshot["evidence"]["issue:2"] = issue_record(
            2,
            availability="not-enriched",
            referenced_by=[association(1), association(5)],
        )
        client = FakeClient(
            search_response={
                "total_count": 1,
                "incomplete_results": False,
                "items": [
                    {
                        "number": 2,
                        "state": "open",
                        "title": "Track intermittent test",
                        "body": "Canonical problem issue",
                        "html_url": "https://github.com/owner/repo/issues/2",
                        "created_at": "2026-08-01T00:00:00Z",
                        "updated_at": "2026-08-18T00:00:00Z",
                        "closed_at": None,
                        "labels": [{"name": "test-failure"}],
                    }
                ],
            }
        )

        round_one = AdaptiveEnricher(client, now=NOW).expand(
            snapshot,
            request_document(canonical_request()),
        )

        self.assertEqual(
            [1, 5],
            [
                item["sourceIssueNumber"]
                for item in round_one["evidence"]["issue:2"]["payload"]["referencedBy"]
            ],
        )

        round_two_snapshot = copy.deepcopy(round_one)
        round_two_snapshot["evidence"]["issue:2"]["availability"] = "partial"
        round_two_requests = validate_evidence_requests(
            round_two_snapshot,
            request_document(
                evidence_request("issue-reference", 5, "issue:2", "canonical-issue"),
                round_number=2,
            ),
        )
        self.assertEqual(5, round_two_requests[0]["sourceIssueNumber"])

    def test_search_result_including_source_still_updates_source_search_record(self) -> None:
        snapshot = canonical_snapshot()
        client = FakeClient(
            search_response={
                "total_count": 1,
                "incomplete_results": False,
                "items": [
                    {
                        "number": 1,
                        "state": "open",
                        "title": "The source incident",
                        "body": "Test name: Namespace.Tests.IntermittentFailure",
                        "html_url": "https://github.com/owner/repo/issues/1",
                        "created_at": "2026-08-01T00:00:00Z",
                        "updated_at": "2026-08-18T00:00:00Z",
                        "closed_at": None,
                        "labels": [],
                    }
                ],
            }
        )

        expanded = AdaptiveEnricher(client, now=NOW).expand(
            snapshot,
            request_document(canonical_request()),
        )

        search = expanded["evidence"]["issue:1"]["payload"]["supportingSearch"]
        self.assertTrue(search["complete"])
        self.assertEqual([1], search["candidateIssueNumbers"])
        self.assertEqual(
            "namespace.tests.intermittentfailure",
            search["queryFact"]["normalized"],
        )

    def test_more_than_twenty_results_remains_incomplete_and_cannot_open_dedicated(self) -> None:
        snapshot = canonical_snapshot()
        items = [
            {
                "number": issue_number,
                "state": "open",
                "title": f"Candidate {issue_number}",
                "body": "",
                "html_url": f"https://github.com/owner/repo/issues/{issue_number}",
                "created_at": "2026-08-01T00:00:00Z",
                "updated_at": "2026-08-18T00:00:00Z",
                "closed_at": None,
                "labels": [],
            }
            for issue_number in range(2, 22)
        ]
        client = FakeClient(
            search_response={
                "total_count": 21,
                "incomplete_results": False,
                "items": items,
            }
        )

        expanded = AdaptiveEnricher(client, now=NOW).expand(
            snapshot,
            request_document(canonical_request()),
        )

        search = expanded["evidence"]["issue:1"]["payload"]["supportingSearch"]
        self.assertFalse(search["complete"])
        self.assertTrue(search["truncated"])
        self.assertEqual(20, search["returnedCount"])
        report_evidence = [
            {
                "id": "issue:1",
                "kind": "issue-event",
                "role": "canonical-search-complete",
            },
            {
                "id": "run:42",
                "kind": "workflow-run",
                "roles": ["current-failing-run", "recurrence"],
            },
            *[
                {"id": f"issue:{issue_number}", "kind": "issue-event"}
                for issue_number in range(2, 22)
            ],
        ]
        report = decision_report(
            1,
            state="actionable",
            action="open-dedicated-issue",
            evidence=report_evidence,
        )
        with self.assertRaisesRegex(ValidationError, "canonical-search-complete"):
            validate_report(expanded, report)

    def test_search_error_records_incomplete_attempt_and_does_not_erase_other_evidence(self) -> None:
        snapshot = canonical_snapshot()
        client = FakeClient(search_response=RuntimeError("search unavailable"))

        expanded = AdaptiveEnricher(client, now=NOW).expand(
            snapshot,
            request_document(canonical_request()),
        )

        search = expanded["evidence"]["issue:1"]["payload"]["supportingSearch"]
        self.assertFalse(search["complete"])
        self.assertFalse(search["truncated"])
        self.assertIsNone(search["totalCount"])
        self.assertEqual("partial", expanded["expansions"][0]["status"])
        self.assertEqual(1, len(expanded["expansions"][0]["errors"]))
        self.assertIn("run:42", expanded["evidence"])

    def test_incomplete_results_flag_cannot_support_open_dedicated(self) -> None:
        snapshot = canonical_snapshot()
        client = FakeClient(
            search_response={
                "total_count": 0,
                "incomplete_results": True,
                "items": [],
            }
        )

        expanded = AdaptiveEnricher(client, now=NOW).expand(
            snapshot,
            request_document(canonical_request()),
        )

        search = expanded["evidence"]["issue:1"]["payload"]["supportingSearch"]
        self.assertFalse(search["complete"])
        self.assertTrue(search["truncated"])

    def test_malformed_search_shape_cannot_support_open_dedicated(self) -> None:
        malformed_responses = (
            {"total_count": 0, "items": []},
            {"total_count": -1, "incomplete_results": False, "items": []},
            {"total_count": True, "incomplete_results": False, "items": []},
            {"total_count": 0, "incomplete_results": "false", "items": []},
            {"total_count": 0, "incomplete_results": False, "items": {}},
        )
        report = decision_report(
            1,
            state="actionable",
            action="open-dedicated-issue",
            evidence=[
                {
                    "id": "issue:1",
                    "kind": "issue-event",
                    "role": "canonical-search-complete",
                },
                {
                    "id": "run:42",
                    "kind": "workflow-run",
                    "roles": ["current-failing-run", "recurrence"],
                },
            ],
        )

        for response in malformed_responses:
            with self.subTest(response=response):
                expanded = AdaptiveEnricher(
                    FakeClient(search_response=response),
                    now=NOW,
                ).expand(
                    canonical_snapshot(),
                    request_document(canonical_request()),
                )

                search = expanded["evidence"]["issue:1"]["payload"]["supportingSearch"]
                self.assertFalse(search["complete"])
                self.assertEqual("partial", expanded["expansions"][0]["status"])
                self.assertEqual(1, len(expanded["expansions"][0]["errors"]))
                with self.assertRaisesRegex(
                    ValidationError,
                    "canonical-search-complete",
                ):
                    validate_report(expanded, report)


class FakeGitRunner:
    def __init__(self, history: str) -> None:
        self.history = history
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        if command[-2:] == ["rev-parse", "--is-inside-work-tree"]:
            stdout = "true\n"
        elif command[-3:] == ["remote", "get-url", "origin"]:
            stdout = "https://github.com/owner/repo.git\n"
        elif command[-2:] == ["rev-parse", "HEAD"]:
            stdout = f"{'a' * 40}\n"
        elif "ls-tree" in command:
            stdout = "src/Surface.cs\0" if self.history == "present" else ""
        elif "--format=%H%x09%aN%x09%aE%x09%aI" in command:
            stdout = (
                f"{'b' * 40}\tExample Author\tauthor@example.com\t"
                "2026-08-17T10:00:00+00:00\n"
                if self.history != "ambiguous"
                else ""
            )
        elif "--diff-filter=D" in command:
            stdout = f"{'d' * 40}\n" if self.history == "removed" else ""
        elif any(part.startswith("--max-count=") for part in command):
            stdout = ""
        else:
            return subprocess.CompletedProcess(command, 1, "", "unexpected command")
        return subprocess.CompletedProcess(command, 0, stdout, "")


class SourceCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.checkout = TEST_ROOT / ".artifacts" / self._testMethodName
        shutil.rmtree(self.checkout, ignore_errors=True)
        self.checkout.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.checkout, ignore_errors=True)

    def _snapshot(self) -> dict[str, object]:
        return baseline_snapshot(
            evidence={
                "pr:3": record(
                    "pull-request",
                    "https://github.com/owner/repo/pull/3",
                    availability="partial",
                    number=3,
                    targetRepository="owner/repo",
                    files=[{"path": "src/Surface.cs", "status": "modified"}],
                    referencedBy=[association(1)],
                ),
                "run:42": record(
                    "workflow-run",
                    "https://github.com/owner/repo/actions/runs/42",
                    runId=42,
                    conclusion="failure",
                    recentHistoryCollected=True,
                    recentHistoryTruncated=False,
                    recentHistory=[],
                    historyCoversSourceRun=True,
                    referencedBy=[association(1)],
                ),
            }
        )

    @staticmethod
    def _request() -> dict[str, object]:
        return evidence_request("source-check", 1, "pr:3", "obsolete-surface")

    def _expand(self, history: str) -> dict[str, object]:
        return AdaptiveEnricher(
            FakeClient(),
            now=NOW,
            checkout=self.checkout,
            git_runner=FakeGitRunner(history),
        ).expand(self._snapshot(), request_document(self._request()))

    def _git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "--no-pager", "-C", str(self.checkout), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return result.stdout.strip()

    def _initialize_repository(self) -> None:
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.name", "Source Check Test")
        self._git("config", "user.email", "source-check@example.invalid")
        self._git("remote", "add", "origin", "https://github.com/owner/repo.git")

    def _commit_all(self, message: str) -> str:
        self._git("add", "--all")
        self._git("commit", "-q", "-m", message)
        return self._git("rev-parse", "HEAD")

    def _close_stale_report(self, expanded: dict[str, object]) -> dict[str, object]:
        source_id = f"source:{quote('src/Surface.cs', safe='')}"
        return decision_report(
            1,
            state="stale",
            action="close-stale",
            evidence=[
                {"id": "issue:1", "kind": "issue-event"},
                {"id": "pr:3", "kind": "pull-request"},
                {"id": source_id, "kind": "source-path", "role": "obsolete-surface"},
                {
                    "id": "run:42",
                    "kind": "workflow-run",
                    "role": "no-recent-matching-failure",
                },
            ],
        )

    def test_source_existence_uses_recorded_commit_when_worktree_file_is_absent(self) -> None:
        self._initialize_repository()
        source = self.checkout / "src" / "Surface.cs"
        source.parent.mkdir(parents=True)
        source.write_text("tracked", encoding="utf-8")
        head = self._commit_all("add source")
        source.unlink()

        expanded = AdaptiveEnricher(
            FakeClient(),
            now=NOW,
            checkout=self.checkout,
        ).expand(self._snapshot(), request_document(self._request()))

        source_id = f"source:{quote('src/Surface.cs', safe='')}"
        source_record = expanded["evidence"][source_id]
        self.assertEqual("available", source_record["availability"])
        self.assertEqual(head, source_record["payload"]["checkoutCommit"])
        self.assertTrue(source_record["payload"]["exists"])
        with self.assertRaisesRegex(ValidationError, "obsolete-surface"):
            validate_report(expanded, self._close_stale_report(expanded))

    def test_commit_tree_lookup_ignores_dirty_worktree_symlink(self) -> None:
        self._initialize_repository()
        source = self.checkout / "src" / "Surface.cs"
        source.parent.mkdir(parents=True)
        source.write_text("tracked", encoding="utf-8")
        self._commit_all("add source")
        source.unlink()
        source.parent.rmdir()
        outside = self.checkout.with_name(f"{self.checkout.name}-outside")
        shutil.rmtree(outside, ignore_errors=True)
        outside.mkdir()
        self.addCleanup(shutil.rmtree, outside, True)
        try:
            source.parent.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory symlinks are unavailable: {exc}")

        expanded = AdaptiveEnricher(
            FakeClient(),
            now=NOW,
            checkout=self.checkout,
        ).expand(self._snapshot(), request_document(self._request()))

        source_id = f"source:{quote('src/Surface.cs', safe='')}"
        source_record = expanded["evidence"][source_id]
        self.assertEqual("available", source_record["availability"])
        self.assertTrue(source_record["payload"]["exists"])

    def test_source_absence_uses_recorded_commit_when_worktree_file_reappears(self) -> None:
        self._initialize_repository()
        source = self.checkout / "src" / "Surface.cs"
        source.parent.mkdir(parents=True)
        source.write_text("tracked", encoding="utf-8")
        self._commit_all("add source")
        source.unlink()
        removal_commit = self._commit_all("remove source")
        source.write_text("untracked replacement", encoding="utf-8")

        expanded = AdaptiveEnricher(
            FakeClient(),
            now=NOW,
            checkout=self.checkout,
        ).expand(self._snapshot(), request_document(self._request()))

        source_id = f"source:{quote('src/Surface.cs', safe='')}"
        source_record = expanded["evidence"][source_id]
        self.assertEqual("available", source_record["availability"])
        self.assertFalse(source_record["payload"]["exists"])
        self.assertEqual(removal_commit, source_record["payload"]["removalCommit"])
        validate_report(expanded, self._close_stale_report(expanded))

    def test_source_history_queries_are_pinned_to_recorded_checkout_commit(self) -> None:
        runner = FakeGitRunner("removed")

        AdaptiveEnricher(
            FakeClient(),
            now=NOW,
            checkout=self.checkout,
            git_runner=runner,
        ).expand(self._snapshot(), request_document(self._request()))

        history_calls = [call for call in runner.calls if "log" in call]
        self.assertGreaterEqual(len(history_calls), 2)
        for call in history_calls:
            self.assertIn("a" * 40, call)

    def test_real_repository_rename_records_replacement_path_and_commit(self) -> None:
        self._initialize_repository()
        source = self.checkout / "src" / "Surface.cs"
        source.parent.mkdir(parents=True)
        source.write_text("tracked", encoding="utf-8")
        self._commit_all("add source")
        self._git("mv", "src/Surface.cs", "src/RenamedSurface.cs")
        replacement_commit = self._commit_all("rename source")

        expanded = AdaptiveEnricher(
            FakeClient(),
            now=NOW,
            checkout=self.checkout,
        ).expand(self._snapshot(), request_document(self._request()))

        source_id = f"source:{quote('src/Surface.cs', safe='')}"
        source_record = expanded["evidence"][source_id]
        self.assertEqual("available", source_record["availability"])
        self.assertFalse(source_record["payload"]["exists"])
        self.assertEqual(
            "src/RenamedSurface.cs",
            source_record["payload"]["replacementPath"],
        )
        self.assertEqual(
            replacement_commit,
            source_record["payload"]["replacementCommit"],
        )
        validate_report(expanded, self._close_stale_report(expanded))

    def test_present_source_is_available_but_not_obsolete_proof(self) -> None:
        source = self.checkout / "src" / "Surface.cs"
        source.parent.mkdir(parents=True)
        source.write_text("present", encoding="utf-8")

        expanded = self._expand("present")

        source_id = f"source:{quote('src/Surface.cs', safe='')}"
        source_record = expanded["evidence"][source_id]
        self.assertEqual("available", source_record["availability"])
        self.assertTrue(source_record["payload"]["exists"])
        self.assertIsNone(source_record["payload"]["removalCommit"])
        with self.assertRaisesRegex(ValidationError, "obsolete-surface"):
            validate_report(expanded, self._close_stale_report(expanded))

    def test_removed_source_with_deterministic_history_is_available_obsolete_proof(self) -> None:
        expanded = self._expand("removed")

        source_id = f"source:{quote('src/Surface.cs', safe='')}"
        source_record = expanded["evidence"][source_id]
        self.assertEqual("available", source_record["availability"])
        self.assertFalse(source_record["payload"]["exists"])
        self.assertEqual("d" * 40, source_record["payload"]["removalCommit"])
        self.assertFalse(source_record["payload"]["historyAmbiguous"])
        validate_report(expanded, self._close_stale_report(expanded))

    def test_missing_source_without_deterministic_history_is_partial(self) -> None:
        expanded = self._expand("ambiguous")

        source_id = f"source:{quote('src/Surface.cs', safe='')}"
        source_record = expanded["evidence"][source_id]
        self.assertEqual("partial", source_record["availability"])
        self.assertFalse(source_record["payload"]["exists"])
        self.assertTrue(source_record["payload"]["historyAmbiguous"])
        self.assertEqual("partial", expanded["expansions"][0]["status"])

    def test_missing_checkout_is_partial_not_obsolete_proof(self) -> None:
        expanded = AdaptiveEnricher(FakeClient(), now=NOW).expand(
            self._snapshot(),
            request_document(self._request()),
        )

        source_id = f"source:{quote('src/Surface.cs', safe='')}"
        source_record = expanded["evidence"][source_id]
        self.assertEqual("partial", source_record["availability"])
        self.assertIsNone(source_record["payload"]["exists"])
        self.assertTrue(source_record["payload"]["historyAmbiguous"])

    def test_failed_source_check_preserves_complete_colliding_baseline_record(self) -> None:
        source_id = f"source:{quote('src/Surface.cs', safe='')}"
        baseline_record = record(
            "source-path",
            "https://github.com/owner/repo/blob/previous/src/Surface.cs",
            path="src/Surface.cs",
            targetRepository="owner/repo",
            checkoutCommit="c" * 40,
            exists=True,
            historyAmbiguous=False,
            recentCommits=[
                {
                    "commit": "b" * 40,
                    "authorName": "Existing Author",
                    "authorEmail": "author@example.invalid",
                    "authoredAt": "2026-08-17T10:00:00+00:00",
                }
            ],
            referencedBy=[association(1), association(5)],
        )
        snapshot = baseline_snapshot(
            open_issues=[1, 5],
            evidence={source_id: baseline_record},
        )

        def failing_git_runner(
            command: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 1, "", "git failed")

        enrichers = (
            AdaptiveEnricher(FakeClient(), now=NOW),
            AdaptiveEnricher(
                FakeClient(),
                now=NOW,
                checkout=self.checkout,
                git_runner=failing_git_runner,
            ),
        )
        for enricher in enrichers:
            with self.subTest(checkout=enricher._checkout is not None):
                expanded = enricher.expand(
                    snapshot,
                    request_document(
                        evidence_request(
                            "source-check",
                            1,
                            source_id,
                            "obsolete-surface",
                        )
                    ),
                )

                self.assertEqual(baseline_record, expanded["evidence"][source_id])
                self.assertEqual("available", expanded["evidence"][source_id]["availability"])
                self.assertEqual(
                    baseline_record["payload"]["recentCommits"],
                    expanded["evidence"][source_id]["payload"]["recentCommits"],
                )
                self.assertEqual("partial", expanded["expansions"][0]["status"])
                self.assertEqual(1, len(expanded["expansions"][0]["errors"]))

    def test_source_check_preserves_shared_associations_for_later_round(self) -> None:
        source_id = f"source:{quote('src/Surface.cs', safe='')}"
        snapshot = baseline_snapshot(
            open_issues=[1, 5],
            evidence={
                source_id: record(
                    "source-path",
                    "https://github.com/owner/repo/blob/main/src/Surface.cs",
                    path="src/Surface.cs",
                    targetRepository="owner/repo",
                    referencedBy=[association(1), association(5)],
                ),
            },
        )
        source = self.checkout / "src" / "Surface.cs"
        source.parent.mkdir(parents=True)
        source.write_text("present", encoding="utf-8")

        def failing_git_runner(
            command: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 1, "", "git failed")

        enrichers = {
            "no-checkout": AdaptiveEnricher(FakeClient(), now=NOW),
            "git-failure": AdaptiveEnricher(
                FakeClient(),
                now=NOW,
                checkout=self.checkout,
                git_runner=failing_git_runner,
            ),
            "success": AdaptiveEnricher(
                FakeClient(),
                now=NOW,
                checkout=self.checkout,
                git_runner=FakeGitRunner("present"),
            ),
        }
        for path, enricher in enrichers.items():
            with self.subTest(path=path):
                round_one = enricher.expand(
                    snapshot,
                    request_document(
                        evidence_request(
                            "source-check",
                            1,
                            source_id,
                            "obsolete-surface",
                        )
                    ),
                )
                self.assertEqual(
                    [1, 5],
                    [
                        item["sourceIssueNumber"]
                        for item in round_one["evidence"][source_id]["payload"][
                            "referencedBy"
                        ]
                    ],
                )

                round_two = enricher.expand(
                    round_one,
                    request_document(
                        evidence_request(
                            "source-check",
                            5,
                            source_id,
                            "obsolete-surface",
                        ),
                        round_number=2,
                    ),
                )
                self.assertEqual(
                    [1, 5],
                    [
                        item["sourceIssueNumber"]
                        for item in round_two["evidence"][source_id]["payload"][
                            "referencedBy"
                        ]
                    ],
                )


if __name__ == "__main__":
    unittest.main()
