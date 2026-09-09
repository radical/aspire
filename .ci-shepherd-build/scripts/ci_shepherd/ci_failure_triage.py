"""Deterministic, advisory CI failure qualification for Aspire evidence."""
from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

from .timeutils import parse_aware_iso8601


TRIAGE_SCHEMA_VERSION = 1
TRIAGE_RULE_VERSION = "aspire-ci-triage-v1"

_PHASES = frozenset({"build", "setup", "test", "harness", "unknown"})
_CAUSES = frozenset({
    "compiler-diagnostic",
    "dependency-network",
    "test-failure",
    "session-abort",
    "unknown",
})
_COMPLETENESS = frozenset({"complete", "incomplete"})
_DENOMINATOR_STATUS = frozenset({"complete", "partial", "unknown"})
_FAMILY_STATUS = frozenset({"verified", "unknown"})
_ASSESSMENT_CATEGORIES = frozenset({
    "blocking-build",
    "transient-infrastructure",
    "flaky-test",
    "unknown",
})
_ASSESSMENT_DISPOSITIONS = frozenset({"investigate", "watch"})

_COMPILER_RE = re.compile(
    r"(?im)^(?P<record>.*?\berror\s+(?P<code>[A-Z]{2,}\d{3,})\s*:[^\r\n]+)$"
)
_TEST_RESULT_RE = re.compile(
    r"(?im)^\s*(?:Passed|Failed|Skipped)\s+[^\r\n]+?\s+\[[^\r\n]*\]\s*$"
)
_FAILED_TEST_RE = re.compile(r"(?im)^\s*Failed\s+(?P<test>[^\r\n]+?)\s+\[[^\r\n]*\]\s*$")
_NETWORK_RE = re.compile(
    r"(?i)(connection reset|timed? out|temporary failure|name resolution|"
    r"unable to load the service index|http\s+5\d\d|tls|connection refused)"
)
_SETUP_OPERATION_RE = re.compile(
    r"(?i)(dotnet\s+tool\s+restore|dotnet\s+restore|nuget\s+restore|"
    r"npm\s+(?:ci|install)|setup|install dependencies)"
)
_ABORT_RE = re.compile(
    r"(?i)(test run aborted|session abort|operation timed out|"
    r"testhost.*(?:crash|terminated)|timeout)"
)
_ZERO_FAILURE_RE = re.compile(r"(?i)\b0\s+(?:failed|failures)\b")

_ASSESSMENT_KEYS = frozenset({
    "caseId",
    "issueNumber",
    "occurrenceId",
    "evidenceIds",
    "evidenceFingerprint",
    "reportedClaims",
    "observed",
    "evidenceCompleteness",
    "reasonCodes",
    "missingEvidence",
    "family",
    "history",
    "assessment",
})
_OBSERVED_KEYS = frozenset({"phase", "cause", "signature", "testFailureEstablished"})
_REPORTED_KEYS = frozenset({"testNames", "causeIds"})
_HISTORY_KEYS = frozenset({"windows", "attempts"})
_WINDOW_KEYS = frozenset({"failedRuns", "observedExecutions", "denominatorStatus"})
_ATTEMPT_KEYS = frozenset({"runId", "attempt", "outcome", "occurredAt"})
_ADVISORY_KEYS = frozenset({"category", "disposition", "summary", "reassessWhen"})
_DIMENSION_KEYS = frozenset({
    "workflowId",
    "workflowPath",
    "event",
    "job",
    "lane",
    "laneId",
    "os",
    "testName",
    "phase",
    "signature",
})


def build_ci_failure_triage(
    prepared: Mapping[str, Any],
    *,
    history_rows: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build advisory assessments only from frozen prepared evidence."""
    snapshot_id = _nonempty_string(prepared.get("snapshotId"), "prepared.snapshotId")
    as_of = parse_aware_iso8601(
        prepared.get("sourceCollectedAt"),
        "prepared.sourceCollectedAt",
    )
    issues = _issue_index(prepared)
    occurrences = prepared.get("observations", {}).get("occurrences", [])
    if not isinstance(occurrences, list):
        raise ValueError("prepared observations.occurrences must be an array.")

    assessments = []
    occurrences_by_id = {}
    for raw_occurrence in occurrences:
        if not isinstance(raw_occurrence, Mapping):
            raise ValueError("prepared occurrence must be an object.")
        issue_number = _positive_int(raw_occurrence.get("issueNumber"), "occurrence.issueNumber")
        issue = issues.get(issue_number)
        if issue is None:
            raise ValueError(f"Occurrence references missing issue {issue_number}.")
        occurrences_by_id[str(raw_occurrence["occurrenceId"])] = raw_occurrence
        assessments.append(
            _build_assessment(
                prepared,
                issue,
                raw_occurrence,
            )
        )
    _attach_histories(
        prepared,
        assessments,
        occurrences_by_id,
        history_rows=history_rows,
        as_of=as_of,
    )

    document = {
        "schemaVersion": TRIAGE_SCHEMA_VERSION,
        "ruleVersion": TRIAGE_RULE_VERSION,
        "snapshotId": snapshot_id,
        "assessments": sorted(
            assessments,
            key=lambda item: (item["issueNumber"], item["caseId"]),
        ),
    }
    validate_ci_failure_triage(document, snapshot_id=snapshot_id, prepared=prepared)
    return document


def attach_ci_failure_triage(
    prepared: Mapping[str, Any],
    document: Mapping[str, Any],
) -> dict[str, Any]:
    snapshot_id = _nonempty_string(prepared.get("snapshotId"), "prepared.snapshotId")
    validate_ci_failure_triage(document, snapshot_id=snapshot_id, prepared=prepared)
    result = copy.deepcopy(dict(prepared))
    by_issue: dict[int, list[dict[str, Any]]] = {}
    for assessment in document["assessments"]:
        by_issue.setdefault(int(assessment["issueNumber"]), []).append(
            copy.deepcopy(dict(assessment))
        )
    for issue in result.get("issues", []):
        issue_number = int(issue["issueNumber"])
        issue["ciFailureTriage"] = {
            "ruleVersion": document["ruleVersion"],
            "cases": by_issue.get(issue_number, []),
        }
    result["triageRuleVersion"] = document["ruleVersion"]
    validate_prepared_ci_failure_triage(result, allow_absent=False)
    return result


def validate_prepared_ci_failure_triage(
    prepared: Mapping[str, Any],
    *,
    allow_absent: bool,
) -> None:
    root_version = prepared.get("triageRuleVersion")
    issues = _issue_index(prepared)
    present = [
        issue
        for issue in issues.values()
        if "ciFailureTriage" in issue
    ]
    if root_version is None and not present:
        if allow_absent:
            return
        raise ValueError("Prepared assessment is missing ciFailureTriage.")
    if not isinstance(root_version, str) or not root_version:
        raise ValueError("Prepared triageRuleVersion must be a nonempty string.")
    if len(present) != len(issues):
        raise ValueError("Prepared ciFailureTriage must be present for every issue.")

    assessments = []
    for issue_number, issue in issues.items():
        embedded = issue.get("ciFailureTriage")
        if not isinstance(embedded, Mapping) or set(embedded) != {"ruleVersion", "cases"}:
            raise ValueError(f"Issue {issue_number} ciFailureTriage has an invalid shape.")
        if embedded.get("ruleVersion") != root_version:
            raise ValueError(f"Issue {issue_number} triage ruleVersion mismatch.")
        cases = embedded.get("cases")
        if not isinstance(cases, list):
            raise ValueError(f"Issue {issue_number} triage cases must be an array.")
        if any(
            not isinstance(case, Mapping) or case.get("issueNumber") != issue_number
            for case in cases
        ):
            raise ValueError(f"Issue {issue_number} contains a foreign triage case.")
        assessments.extend(cases)

    validate_ci_failure_triage(
        {
            "schemaVersion": TRIAGE_SCHEMA_VERSION,
            "ruleVersion": root_version,
            "snapshotId": prepared.get("snapshotId"),
            "assessments": assessments,
        },
        snapshot_id=_nonempty_string(prepared.get("snapshotId"), "prepared.snapshotId"),
        prepared=prepared,
    )


def validate_ci_failure_triage(
    document: object,
    *,
    snapshot_id: str,
    prepared: Mapping[str, Any] | None = None,
) -> None:
    if not isinstance(document, Mapping):
        raise ValueError("CI failure triage document must be an object.")
    if set(document) != {"schemaVersion", "ruleVersion", "snapshotId", "assessments"}:
        raise ValueError("CI failure triage document has forbidden or missing fields.")
    if document.get("schemaVersion") != TRIAGE_SCHEMA_VERSION:
        raise ValueError("CI failure triage schemaVersion is unsupported.")
    rule_version = _nonempty_string(document.get("ruleVersion"), "triage.ruleVersion")
    if document.get("snapshotId") != snapshot_id:
        raise ValueError("CI failure triage snapshotId mismatch.")
    assessments = document.get("assessments")
    if not isinstance(assessments, list):
        raise ValueError("CI failure triage assessments must be an array.")

    occurrences: dict[str, Mapping[str, Any]] = {}
    evidence_by_issue: dict[int, dict[str, Mapping[str, Any]]] = {}
    as_of = None
    if prepared is not None:
        as_of = parse_aware_iso8601(
            prepared.get("sourceCollectedAt"),
            "prepared.sourceCollectedAt",
        )
        for occurrence in prepared.get("observations", {}).get("occurrences", []):
            if isinstance(occurrence, Mapping):
                occurrence_id = occurrence.get("occurrenceId")
                if isinstance(occurrence_id, str):
                    occurrences[occurrence_id] = occurrence
        for number, issue in _issue_index(prepared).items():
            evidence_by_issue[number] = {
                record["id"]: record
                for record in issue.get("evidenceBundle", [])
                if isinstance(record, Mapping) and isinstance(record.get("id"), str)
            }

    seen_cases: set[str] = set()
    for assessment in assessments:
        _validate_assessment(
            assessment,
            rule_version=rule_version,
            occurrences=occurrences,
            evidence_by_issue=evidence_by_issue,
            as_of=as_of,
            require_occurrence=prepared is not None,
        )
        case_id = str(assessment["caseId"])
        if case_id in seen_cases:
            raise ValueError(f"Duplicate triage caseId: {case_id}.")
        seen_cases.add(case_id)


def stable_investigation_triage(issue: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return only stable rule-aware triage facts for investigation identity."""
    embedded = issue.get("ciFailureTriage")
    if not isinstance(embedded, Mapping):
        return None
    cases = []
    for case in embedded.get("cases", []):
        cases.append({
            key: copy.deepcopy(case[key])
            for key in (
                "caseId",
                "occurrenceId",
                "evidenceIds",
                "evidenceFingerprint",
                "observed",
                "evidenceCompleteness",
                "reasonCodes",
                "missingEvidence",
                "family",
            )
        })
    return {
        "ruleVersion": embedded["ruleVersion"],
        "cases": cases,
    }


def logical_occurrence_id(repository: str, occurrence: Mapping[str, Any]) -> str:
    # A diagnosis is revisable; only execution and the named subject identify
    # the occurrence. Test names also separate failures within the same job.
    identity = _fingerprint({
        "repository": repository,
        "issueNumber": occurrence["issueNumber"],
        "runId": occurrence["runId"],
        "attempt": occurrence.get("attempt") or 1,
        "jobId": occurrence.get("jobId"),
        "testName": occurrence.get("testName"),
    })
    return "triage-occurrence:" + identity.removeprefix("fnv1a64:")


def _build_assessment(
    prepared: Mapping[str, Any],
    issue: Mapping[str, Any],
    occurrence: Mapping[str, Any],
) -> dict[str, Any]:
    issue_number = int(occurrence["issueNumber"])
    occurrence_id = _nonempty_string(occurrence.get("occurrenceId"), "occurrence.occurrenceId")
    bundle = {
        record["id"]: record
        for record in issue.get("evidenceBundle", [])
        if isinstance(record, Mapping) and isinstance(record.get("id"), str)
    }
    cited = [
        bundle[evidence_id]
        for evidence_id in occurrence.get("evidenceIds", [])
        if evidence_id in bundle
    ]
    evidence_ids = sorted(record["id"] for record in cited)
    evidence_fingerprint = _evidence_fingerprint(occurrence, cited)
    reported = _reported_claims(issue)
    observed, reason_codes, missing_evidence = _classify(occurrence, cited)
    incomplete = sorted({
        record["id"]
        for record in cited
        if record.get("payload", {}).get("truncated") is True
        or record.get("payload", {}).get("excerptTruncated") is True
        or record.get("payload", {}).get("factsTruncated") is True
    })
    if incomplete:
        reason_codes.append("diagnostic-evidence-incomplete")
        missing_evidence.append("complete failed-execution diagnostic evidence")
    completeness = "incomplete" if incomplete or observed["phase"] == "unknown" else "complete"
    family = _family(occurrence, observed, cited)
    advisory = _advisory(observed, completeness)
    return {
        "caseId": f"triage:{occurrence_id}",
        "issueNumber": issue_number,
        "occurrenceId": occurrence_id,
        "evidenceIds": evidence_ids,
        "evidenceFingerprint": evidence_fingerprint,
        "reportedClaims": reported,
        "observed": observed,
        "evidenceCompleteness": completeness,
        "reasonCodes": sorted(set(reason_codes)),
        "missingEvidence": sorted(set(missing_evidence)),
        "family": family,
        "history": {},
        "assessment": advisory,
    }


def _reported_claims(issue: Mapping[str, Any]) -> dict[str, list[str]]:
    identity = issue.get("identity", {})
    test_names = [
        value
        for key in ("tier2TestName", "tier2TestNameRaw")
        if isinstance((value := identity.get(key)), str) and value
    ]
    cause_ids = [
        value
        for key in ("tier1CauseId", "tier3ErrorCode")
        if isinstance((value := identity.get(key)), str) and value
    ]
    return {
        "testNames": sorted(set(test_names)),
        "causeIds": sorted(set(cause_ids)),
    }


def _classify(
    occurrence: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[str], list[str]]:
    # Observation models load the history validators during initialization.
    from .observations import has_diagnostic_subject, normalize_log_text

    test_name = _observed_test_name(occurrence, records)
    for record in records:
        if record.get("kind") != "workflow-test-results":
            continue
        for result in record.get("payload", {}).get("tests", []):
            if (
                isinstance(result, Mapping)
                and result.get("outcome") == "failed"
                and (test_name is None or result.get("testName") == test_name)
            ):
                error_message = result.get("errorMessage")
                diagnostic = (
                    _normalize_diagnostic(error_message)
                    if isinstance(error_message, str) and error_message.strip()
                    else None
                )
                return (
                    _observed(
                        "test",
                        "test-failure",
                        (
                            f"test:test-failure:{_fingerprint(diagnostic)}"
                            if diagnostic
                            else None
                        ),
                        True,
                    ),
                    [
                        "failed-test-result-observed"
                        if diagnostic
                        else "failed-test-result-without-diagnostic"
                    ],
                    [] if diagnostic else ["failed-test diagnostic content"],
                )

    if isinstance(test_name, str) and test_name:
        for record in records:
            if record.get("kind") != "workflow-log":
                continue
            payload = record.get("payload", {})
            text = normalize_log_text("\n".join(
                value
                for key in ("excerpt", "errorMessage")
                if isinstance((value := payload.get(key)), str)
            ))
            for match in _FAILED_TEST_RE.finditer(text):
                if match.group("test").strip() != test_name:
                    continue
                next_result = _TEST_RESULT_RE.search(text, match.end())
                end = next_result.start() if next_result else len(text)
                block = text[match.end():end]
                unrelated = [
                    candidate.start()
                    for pattern in (_COMPILER_RE, _SETUP_OPERATION_RE)
                    if (candidate := pattern.search(block)) is not None
                ]
                if unrelated:
                    block = block[:min(unrelated)]
                diagnostic = _normalize_diagnostic(block)
                return (
                    _observed(
                        "test",
                        "test-failure",
                        (
                            f"test:test-failure:{_fingerprint(diagnostic)}"
                            if diagnostic
                            else None
                        ),
                        True,
                    ),
                    [
                        "failed-test-log-observed"
                        if diagnostic
                        else "failed-test-header-without-diagnostic"
                    ],
                    [] if diagnostic else ["failed-test diagnostic content"],
                )
        return (
            _observed("unknown", "unknown", None, False),
            ["reported-test-not-attributed-to-failing-record"],
            ["matching failed-test diagnostic or failed-test result"],
        )

    candidates: list[tuple[int, str, str, str | None, bool, str]] = []
    for record in records:
        if record.get("kind") != "workflow-log":
            continue
        payload = record.get("payload", {})
        text = normalize_log_text("\n".join(
            value
            for key in ("excerpt", "errorMessage")
            if isinstance((value := payload.get(key)), str)
        ))
        if not text:
            continue
        compiler = _COMPILER_RE.search(text)
        if compiler:
            candidates.append((
                compiler.start(),
                "build",
                "compiler-diagnostic",
                compiler.group("record"),
                False,
                "compiler-diagnostic-before-test-failure",
            ))
        setup = _SETUP_OPERATION_RE.search(text)
        network = _NETWORK_RE.search(text)
        if setup and network:
            start = min(setup.start(), network.start())
            # Keep the actual resource-bearing failure, e.g.
            # "dotnet restore | error downloading https://feed/Foo: connection reset".
            # The transport phrase alone cannot establish a shared failure family.
            diagnostic = _log_line(text, network.start())
            record_text = (
                f"{_log_line(text, setup.start())} | {diagnostic}"
                if has_diagnostic_subject(diagnostic)
                else None
            )
            candidates.append((
                start,
                "setup",
                "dependency-network",
                record_text,
                False,
                "setup-network-failure-before-test-proof",
            ))
        aborted = _ABORT_RE.search(text)
        if aborted and (_ZERO_FAILURE_RE.search(text) or not _FAILED_TEST_RE.search(text)):
            candidates.append((
                aborted.start(),
                "harness",
                "session-abort",
                aborted.group(0),
                False,
                "session-aborted-without-failed-assertion",
            ))

    if candidates:
        _, phase, cause, diagnostic, established, reason = min(
            candidates,
            key=lambda item: item[0],
        )
        return (
            _observed(
                phase,
                cause,
                f"{phase}:{cause}:{_fingerprint(_normalize_diagnostic(diagnostic))}" if diagnostic else None,
                established,
            ),
            [reason],
            [] if diagnostic else ["resource-specific setup diagnostic"],
        )
    return (
        _observed("unknown", "unknown", None, False),
        ["observed-cause-unresolved"],
        ["specific failed-execution diagnostic or failed-test result"],
    )


def _observed(
    phase: str,
    cause: str,
    signature: str | None,
    established: bool,
) -> dict[str, Any]:
    return {
        "phase": phase,
        "cause": cause,
        "signature": signature,
        "testFailureEstablished": established,
    }


def _family(
    occurrence: Mapping[str, Any],
    observed: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    run_id = occurrence.get("runId")
    run_records = [
        record
        for record in records
        if record.get("kind") == "workflow-run"
        and record.get("payload", {}).get("runId") == run_id
    ]
    run_payload = run_records[0].get("payload", {}) if len(run_records) == 1 else {}
    dimensions = {
        "workflowId": occurrence.get("workflowId"),
        "workflowPath": occurrence.get("workflowPath"),
        "event": occurrence.get("event"),
        "job": occurrence.get("jobName"),
        "lane": occurrence.get("lane"),
        "laneId": occurrence.get("laneId"),
        "os": occurrence.get("os"),
        "testName": (
            _observed_test_name(occurrence, records)
            if observed["phase"] == "test"
            else None
        ),
        "phase": observed["phase"],
        "signature": observed["signature"],
    }
    required = (
        "workflowId",
        "workflowPath",
        "event",
        "job",
        "lane",
        "os",
        "phase",
        "signature",
    )
    if (
        len(run_records) != 1
        or any(
            run_payload.get(key) != occurrence.get(key)
            for key in ("workflowId", "workflowPath", "event")
        )
        or
        not isinstance(dimensions["workflowId"], int)
        or isinstance(dimensions["workflowId"], bool)
        or dimensions["workflowId"] < 1
        or any(
            not isinstance(dimensions.get(key), str)
            or not dimensions[key].strip()
            or dimensions[key] == "unknown"
            for key in required
            if key != "workflowId"
        )
    ):
        return {
            "status": "unknown",
            "reason": "complete execution scope and diagnostic signature are required",
        }
    if observed["phase"] == "test" and not dimensions["testName"]:
        return {
            "status": "unknown",
            "reason": "an exact failed test identity is required",
        }
    return {
        "status": "verified",
        "familyId": _fingerprint(dimensions),
        "dimensions": dimensions,
    }


def _observed_test_name(
    occurrence: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> str | None:
    test_name = occurrence.get("testName")
    evidence_id = occurrence.get("testNameEvidenceId")
    if not isinstance(test_name, str) or not test_name or not isinstance(evidence_id, str):
        return None
    return (
        test_name
        if any(
            record.get("id") == evidence_id
            and record.get("kind") in {"workflow-log", "workflow-test-results"}
            for record in records
        )
        else None
    )


def _attach_histories(
    prepared: Mapping[str, Any],
    assessments: list[dict[str, Any]],
    occurrences: Mapping[str, Mapping[str, Any]],
    *,
    history_rows: Sequence[Mapping[str, Any]],
    as_of,
) -> None:
    coverage = [
        item
        for item in prepared.get("observations", {}).get("coverage", [])
        if isinstance(item, Mapping) and item.get("status") == "succeeded"
    ]
    current_ids = {
        logical_occurrence_id(prepared["repository"], occurrence)
        for occurrence in occurrences.values()
    }
    # Current evidence supersedes persisted proof before this cycle is recorded,
    # including a downgrade to unknown while another case still has that family.
    history_rows = [
        row for row in history_rows
        if row.get("logicalOccurrenceId") not in current_ids
    ]
    for assessment in assessments:
        family = assessment["family"]
        if family["status"] == "verified":
            peers = [
                candidate
                for candidate in assessments
                if candidate["family"].get("status") == "verified"
                and candidate["family"].get("familyId") == family["familyId"]
            ]
        else:
            peers = [assessment]
        peer_occurrences = [
            occurrences[candidate["occurrenceId"]]
            for candidate in peers
        ]
        attempts = [_failure_attempt(occurrence) for occurrence in peer_occurrences]
        for item in coverage:
            if any(
                _coverage_matches(occurrence, item)
                for occurrence in peer_occurrences
            ):
                attempts.append({
                    "runId": item.get("runId"),
                    "attempt": item.get("attempt") or 1,
                    "outcome": "success",
                    "occurredAt": item.get("observedAt"),
                })
        assessment["history"] = _history(
            attempts,
            family,
            history_rows=history_rows,
            as_of=as_of,
        )


def _failure_attempt(occurrence: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "runId": occurrence["runId"],
        "attempt": occurrence.get("attempt") or 1,
        "outcome": "failure",
        "occurredAt": occurrence.get("observedAt"),
    }


def _coverage_matches(
    occurrence: Mapping[str, Any],
    coverage: Mapping[str, Any],
) -> bool:
    if occurrence.get("incompleteDiagnosticEvidenceIds"):
        return False
    if _scope_subject(occurrence.get("verifiedScope")) != _scope_subject(
        coverage.get("verifiedScope")
    ):
        return False
    if not _workflow_execution_scope_matches(occurrence, coverage):
        return False
    if any(
        occurrence.get(field) != coverage.get(field)
        for field in ("workflow", "jobName", "lane", "os", "laneId")
    ):
        return False
    failure_at = occurrence.get("observedAt")
    coverage_at = coverage.get("observedAt")
    if not isinstance(failure_at, str) or not isinstance(coverage_at, str):
        return False
    if parse_aware_iso8601(coverage_at, "coverage observedAt") <= parse_aware_iso8601(
        failure_at,
        "occurrence observedAt",
    ):
        return False
    test_name = occurrence.get("testName")
    return (
        coverage.get("subjectKind") == "test"
        and coverage.get("testName") == test_name
        if isinstance(test_name, str) and test_name
        else coverage.get("subjectKind") == "lane"
    )


def _workflow_execution_scope_matches(
    occurrence: Mapping[str, Any],
    coverage: Mapping[str, Any],
) -> bool:
    workflow_id = occurrence.get("workflowId")
    event = occurrence.get("event")
    if (
        not isinstance(workflow_id, int)
        or isinstance(workflow_id, bool)
        or workflow_id <= 0
        or not isinstance(event, str)
        or not event
    ):
        return False
    return all(
        occurrence.get(field) == coverage.get(field)
        for field in ("workflowId", "workflowPath", "event")
    )


def _scope_subject(scope: object) -> tuple[object, ...] | None:
    if not isinstance(scope, Mapping):
        return None
    kind = scope.get("kind")
    if kind == "main":
        return kind, scope.get("repository")
    if kind == "pull-request":
        return kind, scope.get("repository"), scope.get("pullRequest")
    if kind == "branch":
        return kind, scope.get("repository"), scope.get("ref")
    return None


def _history(
    current_attempts: Sequence[Mapping[str, Any]],
    family: Mapping[str, Any],
    *,
    history_rows: Sequence[Mapping[str, Any]],
    as_of,
) -> dict[str, Any]:
    rows = []
    if family.get("status") == "verified":
        rows = [
            row
            for row in history_rows
            if row.get("schemaVersion") == 2
            and row.get("familyStatus") == "verified"
            and row.get("familyId") == family.get("familyId")
        ]
    attempts = [*current_attempts, *[
        {
            "runId": row.get("runId"),
            "attempt": row.get("attempt"),
            "outcome": row.get("outcome"),
            "occurredAt": row.get("occurredAt"),
        }
        for row in rows
    ]]
    unique_attempts = {
        (
            attempt["runId"],
            attempt["attempt"],
            attempt["outcome"],
            attempt["occurredAt"],
        ): attempt
        for attempt in attempts
        if isinstance(attempt.get("runId"), int)
        and isinstance(attempt.get("attempt"), int)
        and attempt.get("outcome") in {"failure", "success"}
        and isinstance(attempt.get("occurredAt"), str)
    }
    ordered_attempts = sorted(
        unique_attempts.values(),
        key=lambda item: (item["occurredAt"], item["runId"], item["attempt"]),
    )
    windows = {}
    for days in (7, 14, 30):
        cutoff = as_of - timedelta(days=days)
        runs = {
            attempt["runId"]
            for attempt in ordered_attempts
            if attempt["outcome"] == "failure"
            and cutoff <= parse_aware_iso8601(attempt["occurredAt"], "occurredAt") <= as_of
        }
        windows[f"{days}d"] = {
            "failedRuns": len(runs),
            "observedExecutions": None,
            "denominatorStatus": "unknown",
        }
    return {"windows": windows, "attempts": ordered_attempts}


def _advisory(
    observed: Mapping[str, Any],
    completeness: str,
) -> dict[str, str]:
    if observed["phase"] == "build":
        category = "blocking-build"
        summary = "Observed compiler failure occurred before a test failure was established."
    elif observed["phase"] == "setup":
        category = "transient-infrastructure"
        summary = "Observed dependency setup failure occurred before test execution was established."
    elif observed["phase"] == "test":
        category = "flaky-test"
        summary = "Observed evidence establishes a failing test assertion or failed test result."
    elif observed["phase"] == "harness":
        category = "unknown"
        summary = "The test session aborted without an observed failing assertion."
    else:
        category = "unknown"
        summary = "The frozen evidence does not establish a specific failure cause."
    return {
        "category": category,
        "disposition": "investigate" if completeness == "incomplete" or category == "unknown" else "watch",
        "summary": summary,
        "reassessWhen": "After matching complete diagnostic or test-result evidence changes.",
    }


def _validate_assessment(
    assessment: object,
    *,
    rule_version: str,
    occurrences: Mapping[str, Mapping[str, Any]],
    evidence_by_issue: Mapping[int, Mapping[str, Mapping[str, Any]]],
    as_of,
    require_occurrence: bool,
) -> None:
    if not isinstance(assessment, Mapping) or set(assessment) != _ASSESSMENT_KEYS:
        raise ValueError("Triage assessment has forbidden or missing fields.")
    case_id = _nonempty_string(assessment.get("caseId"), "assessment.caseId")
    issue_number = _positive_int(assessment.get("issueNumber"), "assessment.issueNumber")
    occurrence_id = _nonempty_string(assessment.get("occurrenceId"), "assessment.occurrenceId")
    if case_id != f"triage:{occurrence_id}":
        raise ValueError("Triage caseId does not match occurrenceId.")
    occurrence = occurrences.get(occurrence_id)
    if require_occurrence and occurrence is None:
        raise ValueError(f"Triage assessment references unknown occurrence {occurrence_id}.")
    if occurrence is not None and occurrence.get("issueNumber") != issue_number:
        raise ValueError("Triage occurrence belongs to another issue.")
    evidence_ids = _string_list(assessment.get("evidenceIds"), "assessment.evidenceIds")
    _require_fingerprint(assessment.get("evidenceFingerprint"), "assessment.evidenceFingerprint")
    cited_records: list[Mapping[str, Any]] = []
    if occurrence is not None:
        bundled = evidence_by_issue.get(issue_number, {})
        if not set(evidence_ids).issubset(bundled):
            raise ValueError("Triage assessment cites evidence outside its issue bundle.")
        if not set(evidence_ids).issubset(set(occurrence.get("evidenceIds", []))):
            raise ValueError("Triage assessment cites evidence outside its occurrence.")
        expected_fingerprint = _evidence_fingerprint(
            occurrence,
            [bundled[evidence_id] for evidence_id in evidence_ids],
        )
        cited_records = [bundled[evidence_id] for evidence_id in evidence_ids]
        if assessment["evidenceFingerprint"] != expected_fingerprint:
            raise ValueError("Triage evidenceFingerprint does not match cited evidence.")
        _validate_occurrence_scope(occurrence, cited_records)

    reported = assessment.get("reportedClaims")
    if not isinstance(reported, Mapping) or set(reported) != _REPORTED_KEYS:
        raise ValueError("Triage reportedClaims has an invalid shape.")
    _string_list(reported.get("testNames"), "reportedClaims.testNames")
    _string_list(reported.get("causeIds"), "reportedClaims.causeIds")

    observed = assessment.get("observed")
    if not isinstance(observed, Mapping) or set(observed) != _OBSERVED_KEYS:
        raise ValueError("Triage observed has an invalid shape.")
    if observed.get("phase") not in _PHASES or observed.get("cause") not in _CAUSES:
        raise ValueError("Triage observed phase or cause is unsupported.")
    if type(observed.get("testFailureEstablished")) is not bool:
        raise ValueError("Triage observed testFailureEstablished must be a boolean.")
    signature = observed.get("signature")
    if signature is not None and not isinstance(signature, str):
        raise ValueError("Triage observed signature must be a string or null.")
    if observed["phase"] == "unknown" and signature is not None:
        raise ValueError("Unknown triage observations cannot carry a signature.")
    if observed["testFailureEstablished"] is not (observed["phase"] == "test"):
        raise ValueError("Triage test-failure proof is inconsistent with observed phase.")

    completeness = assessment.get("evidenceCompleteness")
    if completeness not in _COMPLETENESS:
        raise ValueError("Triage evidenceCompleteness is unsupported.")
    _string_list(assessment.get("reasonCodes"), "assessment.reasonCodes")
    _string_list(assessment.get("missingEvidence"), "assessment.missingEvidence")

    family = assessment.get("family")
    if not isinstance(family, Mapping) or family.get("status") not in _FAMILY_STATUS:
        raise ValueError("Triage family has an invalid status.")
    if family["status"] == "verified":
        if set(family) != {"status", "familyId", "dimensions"}:
            raise ValueError("Verified triage family has an invalid shape.")
        _require_fingerprint(family.get("familyId"), "family.familyId")
        dimensions = family.get("dimensions")
        if not isinstance(dimensions, Mapping) or set(dimensions) != _DIMENSION_KEYS:
            raise ValueError("Verified triage family dimensions have an invalid shape.")
        if family["familyId"] != _fingerprint(dimensions):
            raise ValueError("Verified triage familyId does not match dimensions.")
        if dimensions.get("phase") != observed["phase"] or dimensions.get("signature") != signature:
            raise ValueError("Verified triage family contradicts observed evidence.")
        if occurrence is not None and family != _family(
            occurrence,
            observed,
            cited_records,
        ):
            raise ValueError("Verified triage family does not match occurrence scope.")
    elif set(family) != {"status", "reason"} or not isinstance(family.get("reason"), str):
        raise ValueError("Unknown triage family must contain only a reason.")

    history = assessment.get("history")
    if not isinstance(history, Mapping) or set(history) != _HISTORY_KEYS:
        raise ValueError("Triage history has an invalid shape.")
    windows = history.get("windows")
    if not isinstance(windows, Mapping) or set(windows) != {"7d", "14d", "30d"}:
        raise ValueError("Triage history windows are invalid.")
    previous = -1
    for name in ("7d", "14d", "30d"):
        window = windows[name]
        if not isinstance(window, Mapping) or set(window) != _WINDOW_KEYS:
            raise ValueError("Triage history window has an invalid shape.")
        failures = window.get("failedRuns")
        if not isinstance(failures, int) or isinstance(failures, bool) or failures < 0:
            raise ValueError("Triage failedRuns must be a non-negative integer.")
        if failures < previous:
            raise ValueError("Triage history windows have inconsistent failure totals.")
        previous = failures
        denominator = window.get("observedExecutions")
        status = window.get("denominatorStatus")
        if status not in _DENOMINATOR_STATUS:
            raise ValueError("Triage denominatorStatus is unsupported.")
        if status == "unknown" and denominator is not None:
            raise ValueError("Unknown triage denominator cannot carry a count.")
        if status != "unknown" and (
            not isinstance(denominator, int)
            or isinstance(denominator, bool)
            or denominator < failures
        ):
            raise ValueError("Triage denominator is inconsistent with failures.")
    attempts = history.get("attempts")
    if not isinstance(attempts, list):
        raise ValueError("Triage history attempts must be an array.")
    run_ids = set()
    for attempt in attempts:
        if not isinstance(attempt, Mapping) or set(attempt) != _ATTEMPT_KEYS:
            raise ValueError("Triage history attempt has an invalid shape.")
        _positive_int(attempt.get("runId"), "history.attempt.runId")
        _positive_int(attempt.get("attempt"), "history.attempt.attempt")
        if attempt.get("outcome") not in {"failure", "success"}:
            raise ValueError("Triage history attempt outcome is unsupported.")
        parse_aware_iso8601(attempt.get("occurredAt"), "history.attempt.occurredAt")
        run_ids.add(attempt["runId"])
    if windows["30d"]["failedRuns"] > len(run_ids):
        raise ValueError("Triage history failures exceed retained attempts.")
    if as_of is not None:
        for days in (7, 14, 30):
            cutoff = as_of - timedelta(days=days)
            expected = len({
                attempt["runId"]
                for attempt in attempts
                if attempt["outcome"] == "failure"
                and cutoff
                <= parse_aware_iso8601(attempt["occurredAt"], "history.attempt.occurredAt")
                <= as_of
            })
            if windows[f"{days}d"]["failedRuns"] != expected:
                raise ValueError(
                    "Triage history window does not match retained failure attempts."
                )

    advisory = assessment.get("assessment")
    if not isinstance(advisory, Mapping) or set(advisory) != _ADVISORY_KEYS:
        raise ValueError("Triage advisory assessment has an invalid shape.")
    if advisory.get("category") not in _ASSESSMENT_CATEGORIES:
        raise ValueError("Triage advisory category is unsupported.")
    if advisory.get("disposition") not in _ASSESSMENT_DISPOSITIONS:
        raise ValueError("Triage advisory disposition is unsupported.")
    _nonempty_string(advisory.get("summary"), "assessment.summary")
    _nonempty_string(advisory.get("reassessWhen"), "assessment.reassessWhen")


def _validate_occurrence_scope(
    occurrence: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> None:
    _positive_int(occurrence.get("runId"), "occurrence.runId")
    for key in ("attempt", "jobId", "workflowId"):
        value = occurrence.get(key)
        if value is not None:
            _positive_int(value, f"occurrence.{key}")
    for key in ("workflowPath", "event", "workflow", "jobName", "lane", "os"):
        value = occurrence.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"occurrence.{key} must be a nonempty string or null.")
    test_name = occurrence.get("testName")
    if test_name is not None and (not isinstance(test_name, str) or not test_name.strip()):
        raise ValueError("occurrence.testName must be a nonempty string or null.")

    run_records = [
        record
        for record in records
        if record.get("kind") == "workflow-run"
        and record.get("payload", {}).get("runId") == occurrence.get("runId")
    ]
    if len(run_records) == 1 and any(
        run_records[0].get("payload", {}).get(key) != occurrence.get(key)
        for key in ("workflowId", "workflowPath", "event")
    ):
        raise ValueError("Triage occurrence workflow scope does not match its run evidence.")
    job_records = [
        record
        for record in records
        if record.get("kind") == "workflow-job"
        and record.get("payload", {}).get("runId") == occurrence.get("runId")
        and record.get("payload", {}).get("jobId") == occurrence.get("jobId")
    ]
    if len(job_records) == 1:
        payload = job_records[0].get("payload", {})
        if (
            (payload.get("attempt") or 1) != (occurrence.get("attempt") or 1)
            or payload.get("name") != occurrence.get("jobName")
        ):
            raise ValueError("Triage occurrence job scope does not match its job evidence.")


def _issue_index(prepared: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    issues = prepared.get("issues")
    if not isinstance(issues, list):
        raise ValueError("prepared issues must be an array.")
    result = {}
    for issue in issues:
        if not isinstance(issue, Mapping):
            raise ValueError("prepared issue must be an object.")
        number = _positive_int(issue.get("issueNumber"), "prepared issueNumber")
        if number in result:
            raise ValueError(f"Duplicate prepared issue {number}.")
        result[number] = issue
    return result


def _normalize_diagnostic(value: str) -> str:
    text = "\n".join(line.removeprefix("##[error]").strip() for line in value.splitlines())
    return " ".join(text.casefold().split())


def _log_line(text: str, position: int) -> str:
    start = text.rfind("\n", 0, position) + 1
    end = text.find("\n", position)
    return text[start:end if end >= 0 else len(text)].strip().removeprefix("##[error]").strip()


def _evidence_fingerprint(
    occurrence: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> str:
    return _fingerprint({
        "occurrence": {
            key: occurrence.get(key)
            for key in (
                "occurrenceId",
                "runId",
                "attempt",
                "jobId",
                "testName",
                "observedAt",
                "fingerprintId",
            )
        },
        "records": [
            {
                "id": record["id"],
                "diagnosticFingerprint": record.get("payload", {}).get("diagnosticFingerprint"),
                "payload": record.get("payload"),
            }
            for record in records
        ],
    })


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    result = 0xCBF29CE484222325
    for byte in encoded:
        result ^= byte
        result = (result * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"fnv1a64:{result:016x}"


def _require_fingerprint(value: object, name: str) -> str:
    result = _nonempty_string(value, name)
    if re.fullmatch(r"fnv1a64:[0-9a-f]{16}", result) is None:
        raise ValueError(f"{name} must be an FNV-1a fingerprint.")
    return result


def _nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty string.")
    return value


def _positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _string_list(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item
        for item in value
    ):
        raise ValueError(f"{name} must be an array of nonempty strings.")
    if len(value) != len(set(value)):
        raise ValueError(f"{name} must not contain duplicates.")
    return value
