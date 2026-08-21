from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping

from ci_shepherd.observations import is_annotation_evidence_id, is_scoped_to_issue
from ci_shepherd.timeutils import format_utc_z, parse_aware_iso8601


ASSESSMENT_SCHEMA_VERSION = 1
DEFAULT_MAX_BUNDLE_RECORDS = 25

_EVIDENCE_PRIORITY = {
    "issue-event": 0,
    "pull-request": 1,
    "workflow-run": 2,
    "commit": 3,
    "issue-comment": 4,
    "workflow-job": 5,
    "workflow-log": 6,
    "codeowners": 7,
    "source-path": 8,
    # Annotations are the first thing evicted when the bundle is capped. They repeat
    # what the job and log already say, and there can be dozens per job (deprecation
    # warnings, matrix-wide notices), so ranking them above real jobs, logs, or source
    # paths would let boilerplate crowd out the decision-relevant records.
    "workflow-annotation": 9,
}

_PAYLOAD_FIELDS_BY_KIND = {
    "issue-event": (
        "number",
        "state",
        "title",
        "url",
        "createdAt",
        "updatedAt",
        "closedAt",
        "labels",
        "author",
        "producer",
        "autoclose",
        "markers",
        "facts",
        "referencedBy",
        "targetRepository",
        "supportingSelection",
        "role",
        "normalizedCause",
        "sourceIssueNumber",
    ),
    "issue-comment": (
        "id",
        "url",
        "createdAt",
        "updatedAt",
        "author",
        "markers",
        "facts",
        "references",
        "sourceIssueNumber",
        "referencedBy",
        "role",
        "normalizedCause",
    ),
    "pull-request": (
        "number",
        "state",
        "mergedAt",
        "mergeCommitSha",
        "head",
        "base",
        "linkedIssues",
        "referencedBy",
        "targetRepository",
        "role",
        "normalizedCause",
        "sourceIssueNumber",
    ),
    "workflow-run": (
        "runId",
        "attempt",
        "name",
        "workflowName",
        "workflowId",
        "event",
        "status",
        "conclusion",
        "createdAt",
        "updatedAt",
        "runStartedAt",
        "completedAt",
        "headSha",
        "headBranch",
        "referencedBy",
        "targetRepository",
        "runBudgetExcluded",
        "recentHistoryCollected",
        "recentHistoryTruncated",
        "recentHistoryTotalCount",
        "historyCoversSourceRun",
        "recentHistoryGap",
        "role",
        "normalizedCause",
        "sourceIssueNumber",
    ),
    "workflow-job": (
        "jobId",
        "runId",
        "attempt",
        "name",
        "status",
        "conclusion",
        "startedAt",
        "completedAt",
        "checkRunId",
        "logEvidenceId",
        "annotationEvidenceIds",
        "referencedBy",
        "targetRepository",
        "role",
        "normalizedCause",
        "sourceIssueNumber",
    ),
    # The collector files check-run annotations under the "workflow-job" kind, so this
    # projection is selected by evidence ID shape rather than by the record's kind.
    # Both spellings of the level and detail fields are projected: the collector
    # normalizes GitHub's `annotation_level`/`raw_details` to `level`/`message`, but an
    # untransformed record must not silently lose them.
    "workflow-annotation": (
        "annotationId",
        "checkRunId",
        "runId",
        "attempt",
        "jobId",
        "path",
        "startLine",
        "endLine",
        "annotationLevel",
        "level",
        "title",
        "message",
        "rawDetails",
        "referencedBy",
        "targetRepository",
        "sourceIssueNumber",
    ),
    "workflow-log": (
        "evidenceId",
        "runId",
        "attempt",
        "jobId",
        "errorCategory",
        "referencedBy",
        "targetRepository",
        "role",
        "normalizedCause",
        "sourceIssueNumber",
    ),
    "source-path": (
        "path",
        "checkoutCommit",
        "sourceUrl",
        "exists",
        "historyAmbiguous",
        "removalCommit",
        "replacementPath",
        "replacementCommit",
        "referencedBy",
        "targetRepository",
        "role",
        "normalizedCause",
        "sourceIssueNumber",
    ),
    "codeowners": (
        "path",
        "owners",
        "matchedPattern",
        "referencedBy",
        "targetRepository",
        "role",
        "normalizedCause",
        "sourceIssueNumber",
    ),
    "commit": (
        "sha",
        "message",
        "author",
        "committer",
        "date",
        "referencedBy",
        "targetRepository",
        "role",
        "normalizedCause",
        "sourceIssueNumber",
    ),
}


def prepare_assessment(
    snapshot: Mapping[str, Any],
    *,
    max_bundle_records: int = DEFAULT_MAX_BUNDLE_RECORDS,
) -> dict[str, Any]:
    if max_bundle_records < 1:
        raise ValueError("max_bundle_records must be positive.")

    evidence = snapshot.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("Snapshot evidence must be an object.")

    issue_numbers = snapshot.get("openIssues")
    if not isinstance(issue_numbers, list):
        raise ValueError("Snapshot openIssues must be an array.")

    candidates = [
        _build_candidate(
            snapshot,
            evidence,
            issue_number,
            max_bundle_records=max_bundle_records,
        )
        for issue_number in sorted(issue_numbers)
    ]
    return {
        "schemaVersion": ASSESSMENT_SCHEMA_VERSION,
        "repository": snapshot.get("repository"),
        "sourceCollectedAt": snapshot.get("collectedAt"),
        "snapshotId": f"snapshot:{snapshot.get('repository')}:{snapshot.get('collectedAt')}",
        "maxBundleRecords": max_bundle_records,
        "issues": candidates,
        "summary": {
            "issueCount": len(candidates),
            "candidateActionCounts": dict(
                sorted(Counter(item["candidateAction"] for item in candidates).items())
            ),
            "producerCounts": dict(
                sorted(Counter(item["producer"] for item in candidates).items())
            ),
        },
    }


def candidate_for(
    prepared_assessment: Mapping[str, Any],
    issue_number: int,
) -> dict[str, Any]:
    issues = prepared_assessment.get("issues")
    if not isinstance(issues, list):
        raise ValueError("Assessment issues must be an array.")
    matches = [
        item
        for item in issues
        if isinstance(item, dict) and item.get("issueNumber") == issue_number
    ]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one candidate for issue #{issue_number}.")
    return matches[0]


def _build_candidate(
    snapshot: Mapping[str, Any],
    evidence: Mapping[str, Any],
    issue_number: int,
    *,
    max_bundle_records: int,
) -> dict[str, Any]:
    issue_record = evidence.get(f"issue:{issue_number}")
    if not isinstance(issue_record, dict):
        raise ValueError(f"Missing issue evidence for #{issue_number}.")
    payload = issue_record.get("payload")
    if not isinstance(payload, dict):
        raise ValueError(f"Issue evidence for #{issue_number} has no payload.")

    scoped = [
        (evidence_id, record)
        for evidence_id, record in evidence.items()
        if isinstance(evidence_id, str)
        and isinstance(record, dict)
        and is_scoped_to_issue(evidence_id, record, issue_number)
    ]
    scoped.sort(
        key=lambda item: (
            _EVIDENCE_PRIORITY.get(_bundle_kind(item[0], item[1]), 100),
            item[0],
        )
    )
    selected = scoped[:max_bundle_records]
    excluded = scoped[max_bundle_records:]

    producer = str(payload.get("producer") or "unknown")
    autoclose = payload.get("autoclose")
    ledger = payload.get("ledger")
    if not isinstance(ledger, dict):
        ledger = {
            "source": "none",
            "schema": None,
            "schemaRecognized": False,
            "sourceRecordCount": 0,
            "parsedRowCount": 0,
            "complete": False,
            "rows": [],
        }

    identity = summarize_identity_facts(payload)
    decision = _lifecycle_decision(
        snapshot=snapshot,
        issue_number=issue_number,
        producer=producer,
        autoclose=autoclose if isinstance(autoclose, bool) else None,
        ledger=ledger,
        episodes_complete=payload.get("episodesComplete") is True,
        updated_at=payload.get("updatedAt"),
        scoped=scoped,
    )

    excluded_counts = Counter(
        _bundle_kind(evidence_id, record) or "unknown" for evidence_id, record in excluded
    )
    return {
        "issueNumber": issue_number,
        "issueUrl": payload.get("url"),
        "title": payload.get("title"),
        "producer": producer,
        "autoclose": autoclose if isinstance(autoclose, bool) else None,
        "ledger": ledger,
        "episodesComplete": payload.get("episodesComplete") is True,
        "identity": identity,
        **decision,
        "evidenceBundle": [
            {
                "id": evidence_id,
                "kind": _bundle_kind(evidence_id, record),
                "url": record.get("url"),
                "availability": record.get("availability"),
                "payload": _compact_payload(evidence_id, record),
            }
            for evidence_id, record in selected
        ],
        "completenessProof": {
            "allScopedEvidenceScanned": True,
            "scopedRecordCount": len(scoped),
            "bundledRecordCount": len(selected),
            "excludedRecordCount": len(excluded),
            "excludedCountsByKind": dict(sorted(excluded_counts.items())),
            "collectionErrorCount": _collection_error_count(snapshot, issue_number),
        },
    }


def _bundle_kind(evidence_id: str, record: Mapping[str, Any]) -> str:
    """Kind the bundle reports for a record, which is not always the collector's.

    The collector has no annotation kind and files check-run annotations under
    ``workflow-job``, which would leave the bundle with several records that claim to
    be the same job while carrying entirely different fields. Splitting them out by
    evidence ID shape gives annotations their own projection and their own eviction
    priority.
    """
    kind = str(record.get("kind") or "")
    if kind == "workflow-job" and is_annotation_evidence_id(evidence_id):
        return "workflow-annotation"
    return kind


def _canonicalize_workflow_run_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Project the raw collector's ``branch`` field as canonical ``headBranch``.

    The raw collector emits workflow-run payloads -- and each ``recentHistory``
    entry -- with GitHub's ``branch`` field name for the run's head branch.
    Prepared payloads use ``headBranch`` everywhere else in the bundle (and in
    downstream recovery logic), so normalize at this preparation boundary
    rather than special-casing ``branch`` throughout every consumer. An
    existing ``headBranch`` always wins, so already-prepared payloads are
    unaffected.
    """
    normalized = dict(payload)
    if "headBranch" not in normalized and isinstance(normalized.get("branch"), str):
        normalized["headBranch"] = normalized["branch"]

    recent_history = normalized.get("recentHistory")
    if isinstance(recent_history, list):
        normalized["recentHistory"] = [
            _canonicalize_recent_history_entry(entry) if isinstance(entry, dict) else entry
            for entry in recent_history
        ]
    return normalized


def _canonicalize_recent_history_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(entry)
    if "headBranch" not in normalized and isinstance(normalized.get("branch"), str):
        normalized["headBranch"] = normalized["branch"]
    return normalized


def _compact_payload(evidence_id: str, record: Mapping[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return {}
    kind = _bundle_kind(evidence_id, record)
    if kind == "workflow-run":
        payload = _canonicalize_workflow_run_payload(payload)
    fields = _PAYLOAD_FIELDS_BY_KIND.get(
        kind,
        (
            "sourceIssueNumber",
            "referencedBy",
            "role",
            "normalizedCause",
        ),
    )
    compact = {
        field: payload[field]
        for field in fields
        if field in payload
    }
    if kind == "issue-comment" and isinstance(payload.get("body"), str):
        compact["body"] = payload["body"][:2_000]
    if kind == "issue-event" and isinstance(payload.get("body"), str):
        body = payload["body"]
        dashboard_context: dict[str, Any] = {}
        for field in ("Assessment", "Suggested"):
            match = re.search(rf"(?im)^-\s*{field}:\s*(.+)$", body)
            if match:
                dashboard_context[f"reported{field}"] = match.group(1).strip()
        streak_match = re.search(r"(?i)\bstreak\s+(\d+)\b", body)
        if streak_match:
            dashboard_context["streak"] = int(streak_match.group(1))
        run_match = re.search(
            r"https://github\.com/[^/\s]+/[^/\s]+/actions/runs/\d+",
            body,
        )
        if run_match:
            dashboard_context["lastRunUrl"] = run_match.group(0)
        mentions = sorted(set(re.findall(r"(?<![\w-])@[A-Za-z0-9-]+", body)))
        if mentions:
            dashboard_context["mentions"] = mentions
        if dashboard_context:
            compact["dashboardContext"] = dashboard_context
    if kind == "workflow-log" and isinstance(payload.get("errorMessage"), str):
        compact["errorMessage"] = payload["errorMessage"][:4_000]
    if kind == "workflow-job" and isinstance(payload.get("steps"), list):
        compact["steps"] = [
            {
                field: step[field]
                for field in ("number", "name", "status", "conclusion")
                if isinstance(step, dict) and field in step
            }
            for step in payload["steps"][:20]
            if isinstance(step, dict)
        ]
    if kind == "workflow-run" and isinstance(payload.get("recentHistory"), list):
        compact["recentHistory"] = [
            {
                field: run[field]
                for field in (
                    "runId",
                    "status",
                    "conclusion",
                    "createdAt",
                    "updatedAt",
                    "runStartedAt",
                    "headSha",
                    "headBranch",
                )
                if isinstance(run, dict) and field in run
            }
            for run in payload["recentHistory"][:10]
            if isinstance(run, dict)
        ]
    if kind == "source-path" and isinstance(payload.get("recentCommits"), list):
        compact["recentCommits"] = payload["recentCommits"][:3]
    if kind == "issue-event" and isinstance(payload.get("supportingSearch"), dict):
        search = payload["supportingSearch"]
        compact["supportingSearch"] = {
            field: search[field]
            for field in (
                "complete",
                "truncated",
                "candidateIssueNumbers",
            )
            if field in search
        }
    return compact


def _lifecycle_decision(
    *,
    snapshot: Mapping[str, Any],
    issue_number: int,
    producer: str,
    autoclose: bool | None,
    ledger: Mapping[str, Any],
    episodes_complete: bool,
    updated_at: object,
    scoped: list[tuple[str, Mapping[str, Any]]],
) -> dict[str, Any]:
    blockers: list[str] = []
    missing_prerequisites: list[str] = []

    if producer in {"unknown", "ci-health-dashboard"}:
        blockers.append(
            "unknown-producer"
            if producer == "unknown"
            else "producer-has-no-occurrence-contract"
        )
        return _decision(
            state="insufficient-evidence",
            action="investigate",
            allowed_decisions=(
                ("insufficient-evidence", "investigate"),
                ("observing", "wait"),
            ),
            blockers=blockers,
            missing_prerequisites=("recognized-producer-ledger",),
        )

    if producer == "tracking-issue":
        if autoclose is True:
            blockers.append("existing-watchdog-owns-closure")
            return _decision(
                state="observing",
                action="wait",
                allowed_decisions=(("observing", "wait"),),
                blockers=blockers,
                missing_prerequisites=(),
            )
        if autoclose is False:
            blockers.append("autoclose-false-requires-human-closure")
        if ledger.get("complete") is not True:
            missing_prerequisites.append("complete-comment-run-ledger")
        return _decision(
            state="actionable",
            action="investigate",
            allowed_decisions=(
                ("actionable", "investigate"),
                ("needs-human", "ping-human"),
                ("observing", "wait"),
            ),
            blockers=blockers,
            missing_prerequisites=missing_prerequisites,
        )

    if producer != "ci-failure-cause":
        blockers.append("unsupported-producer-contract")
        return _decision(
            state="insufficient-evidence",
            action="investigate",
            allowed_decisions=(("insufficient-evidence", "investigate"),),
            blockers=blockers,
            missing_prerequisites=("supported-producer-contract",),
        )

    rows = ledger.get("rows")
    if not isinstance(rows, list):
        rows = []
    if ledger.get("complete") is not True:
        missing_prerequisites.append("complete-occurrence-ledger")
        return _decision(
            state="insufficient-evidence",
            action="investigate",
            allowed_decisions=(
                ("insufficient-evidence", "investigate"),
                ("observing", "wait"),
            ),
            blockers=blockers,
            missing_prerequisites=missing_prerequisites,
        )

    recovery = _commit_anchored_recovery(scoped, rows)
    if recovery is not None:
        blockers.append("autoclose-policy-does-not-permit-shepherd")
        if not episodes_complete:
            blockers.append("episode-history-incomplete")
        state = "resolved"
        if _updated_after_fix_without_occurrence(updated_at, recovery["mergedAt"], rows):
            blockers.append("issue-updated-after-fix-without-ledger-row")
            state = "needs-human"
        return _decision(
            state=state,
            action="recommend-close",
            allowed_decisions=(
                (state, "recommend-close"),
                ("needs-human", "ping-human"),
                ("insufficient-evidence", "investigate"),
                ("observing", "wait"),
            ),
            blockers=blockers,
            missing_prerequisites=(),
            resolution_evidence=recovery,
            approval_required=True,
        )

    if len(rows) >= 2:
        return _decision(
            state="actionable",
            action="investigate",
            allowed_decisions=(
                ("actionable", "investigate"),
                ("needs-human", "ping-human"),
                ("observing", "wait"),
            ),
            blockers=blockers,
            missing_prerequisites=("verified-fix",),
        )

    if _occurrence_time_precision_required(scoped, rows):
        missing_prerequisites.append("occurrence-run-timestamp-for-fix-day")
    missing_prerequisites.append("verified-fix-or-current-recurrence-check")
    return _decision(
        state="observing",
        action="wait",
        allowed_decisions=(
            ("observing", "wait"),
            ("insufficient-evidence", "investigate"),
        ),
        blockers=blockers,
        missing_prerequisites=missing_prerequisites,
    )


def _decision(
    *,
    state: str,
    action: str,
    allowed_decisions: tuple[tuple[str, str], ...],
    blockers: list[str],
    missing_prerequisites: tuple[str, ...] | list[str],
    resolution_evidence: Mapping[str, Any] | None = None,
    approval_required: bool = False,
) -> dict[str, Any]:
    decisions = [
        {"state": allowed_state, "action": allowed_action}
        for allowed_state, allowed_action in allowed_decisions
    ]
    return {
        "candidateState": state,
        "candidateAction": action,
        "allowedActions": list(dict.fromkeys(item["action"] for item in decisions)),
        "allowedDecisions": decisions,
        "automationEligible": False,
        "approvalRequired": approval_required,
        "blockers": sorted(set(blockers)),
        "missingPrerequisites": sorted(set(missing_prerequisites)),
        "resolutionEvidence": dict(resolution_evidence or {}),
    }


def _commit_anchored_recovery(
    scoped: list[tuple[str, Mapping[str, Any]]],
    rows: list[Any],
) -> dict[str, Any] | None:
    latest_occurrence = latest_occurrence_timestamp(scoped, rows)
    pull_requests: list[tuple[str, Mapping[str, Any]]] = []
    successful_runs: list[tuple[str, Mapping[str, Any]]] = []
    for evidence_id, record in scoped:
        if record.get("availability") != "available":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if record.get("kind") == "pull-request":
            if isinstance(payload.get("mergedAt"), str) and isinstance(
                payload.get("mergeCommitSha"), str
            ):
                pull_requests.append((evidence_id, payload))
        elif (
            record.get("kind") == "workflow-run"
            and payload.get("conclusion") == "success"
        ):
            successful_runs.append((evidence_id, payload))

    recoveries: list[dict[str, Any]] = []
    for pr_id, pr in pull_requests:
        merged_at = str(pr["mergedAt"])
        merged_at_instant = parse_aware_iso8601(merged_at, f"{pr_id} mergedAt")
        merge_sha = str(pr["mergeCommitSha"])
        if latest_occurrence is not None:
            if len(latest_occurrence) == 10:
                if latest_occurrence >= format_utc_z(merged_at_instant)[:10]:
                    continue
            elif parse_aware_iso8601(latest_occurrence, "latest occurrence") >= merged_at_instant:
                continue
        for run_id, run in successful_runs:
            started_at = run.get("runStartedAt") or run.get("createdAt")
            started_at_instant = (
                parse_aware_iso8601(started_at, f"{run_id} startedAt")
                if isinstance(started_at, str)
                else None
            )
            if (
                run.get("headSha") == merge_sha
                and started_at_instant is not None
                and started_at_instant >= merged_at_instant
            ):
                recoveries.append(
                    {
                        "pullRequestEvidenceId": pr_id,
                        "runEvidenceId": run_id,
                        "mergeCommitSha": merge_sha,
                        "mergedAt": format_utc_z(merged_at_instant),
                        "successfulRunStartedAt": format_utc_z(started_at_instant),
                        "latestOccurrence": latest_occurrence,
                    }
                )
    if not recoveries:
        return None
    recoveries.sort(
        key=lambda item: (
            item["mergedAt"],
            item["successfulRunStartedAt"],
            item["pullRequestEvidenceId"],
            item["runEvidenceId"],
        )
    )
    return recoveries[-1]


def _occurrence_time_precision_required(
    scoped: list[tuple[str, Mapping[str, Any]]],
    rows: list[Any],
) -> bool:
    latest_occurrence = latest_occurrence_timestamp(scoped, rows)
    if latest_occurrence is None or len(latest_occurrence) != 10:
        return False

    pull_requests: list[Mapping[str, Any]] = []
    successful_runs: list[Mapping[str, Any]] = []
    for _, record in scoped:
        if record.get("availability") != "available":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if record.get("kind") == "pull-request":
            pull_requests.append(payload)
        elif (
            record.get("kind") == "workflow-run"
            and payload.get("conclusion") == "success"
        ):
            successful_runs.append(payload)

    return any(
        isinstance(pr.get("mergedAt"), str)
        and isinstance(pr.get("mergeCommitSha"), str)
        and format_utc_z(parse_aware_iso8601(pr["mergedAt"], "pull request mergedAt"))[:10] == latest_occurrence
        and any(
            run.get("headSha") == pr["mergeCommitSha"]
            and isinstance(run.get("runStartedAt") or run.get("createdAt"), str)
            and parse_aware_iso8601(
                run.get("runStartedAt") or run.get("createdAt"),
                "successful run startedAt",
            )
            >= parse_aware_iso8601(pr["mergedAt"], "pull request mergedAt")
            for run in successful_runs
        )
        for pr in pull_requests
    )


def _updated_after_fix_without_occurrence(
    updated_at: object,
    merged_at: object,
    rows: list[Any],
) -> bool:
    if not isinstance(updated_at, str) or not isinstance(merged_at, str):
        return False
    merged_at_instant = parse_aware_iso8601(merged_at, "resolution mergedAt")
    if parse_aware_iso8601(updated_at, "issue updatedAt") <= merged_at_instant:
        return False
    return not any(
        isinstance(row, dict)
        and isinstance(row.get("date"), str)
        and str(row["date"]) >= format_utc_z(merged_at_instant)[:10]
        for row in rows
    )


def summarize_identity_facts(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce an issue's normalized facts to the tiered identity fields."""
    facts = payload.get("facts")
    if not isinstance(facts, list):
        facts = []

    values: dict[str, list[str]] = {}
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        field = fact.get("field")
        normalized = fact.get("normalized")
        if isinstance(field, str) and isinstance(normalized, str) and normalized:
            values.setdefault(field, []).append(normalized)

    return {
        "tier1CauseId": _one(values, "causeId"),
        "tier2TestName": _one(values, "testName"),
        "tier2ExceptionType": _one(values, "exceptionType"),
        "tier3ErrorCode": _one(values, "errorCode"),
        "tier3Job": _one(values, "job"),
    }


def latest_occurrence_timestamp(
    scoped: list[tuple[str, Mapping[str, Any]]],
    rows: list[Any],
) -> str | None:
    """Return the newest run start time backing the given ledger rows, as UTC ``Z`` text."""
    run_started_at: dict[int, str] = {}
    for _, record in scoped:
        if (
            record.get("availability") != "available"
            or record.get("kind") != "workflow-run"
        ):
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("runId"), int):
            continue
        started_at = payload.get("runStartedAt") or payload.get("createdAt")
        if isinstance(started_at, str):
            run_started_at[int(payload["runId"])] = started_at

    occurrences: list[tuple[datetime, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        source_run = row.get("sourceRun")
        if isinstance(source_run, int) and source_run in run_started_at:
            observed_at = parse_aware_iso8601(run_started_at[source_run], "latest occurrence")
            occurrences.append((observed_at, format_utc_z(observed_at)))
        elif isinstance(row.get("date"), str):
            date_text = str(row["date"])
            if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", date_text):
                date_value = datetime.fromisoformat(date_text).replace(
                    hour=23,
                    minute=59,
                    second=59,
                    microsecond=999999,
                    tzinfo=timezone.utc,
                )
                occurrences.append((date_value, date_text))
            else:
                observed_at = parse_aware_iso8601(date_text, "latest occurrence")
                occurrences.append((observed_at, format_utc_z(observed_at)))
    return max(occurrences, default=(None, None))[1]


def _one(values: Mapping[str, list[str]], field: str) -> str | None:
    distinct = sorted(set(values.get(field, [])))
    return distinct[0] if len(distinct) == 1 else None


def _collection_error_count(
    snapshot: Mapping[str, Any],
    issue_number: int,
) -> int:
    errors = snapshot.get("collectionErrors")
    if not isinstance(errors, list):
        return 0
    return sum(
        1
        for error in errors
        if isinstance(error, dict)
        and (
            error.get("sourceIssueNumber") == issue_number
            or error.get("evidenceId") == f"issue:{issue_number}"
        )
    )
