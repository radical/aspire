"""Append-only cross-snapshot fingerprint ledger for the CI shepherd POC.

Recurrence in ``build_compact_poc_input`` currently only looks at the
occurrences recorded on the *current* issue (or its still-open cluster
members), so evidence of recurrence disappears the moment an issue record
closes. This module records a minimal, privacy-conscious fact per occurrence
(a stable fingerprint plus the run/attempt/date/job it was observed on) into
a JSONL ledger, so later runs can recognize recurrence even after the
original issue is gone.

No logs, titles, bodies, or prose are stored here -- only identity strings
already derived by the prepare stage.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import json
from pathlib import Path
from typing import Any

from .ci_failure_triage import logical_occurrence_id, validate_prepared_ci_failure_triage
from .jsonl import append_jsonl_rows, exclusive_jsonl_lock, read_jsonl_rows


def compute_fingerprint(identity: Mapping[str, Any]) -> str | None:
    """Derive a stable recurrence fingerprint from a prepared issue's identity.

    Specificity order: exact ``tier2TestName``, then ``tier3ErrorCode``, then
    exact ``tier1CauseId``. Returns ``None`` when none of those stable
    identities are present -- such issues are skipped for history purposes
    because they cannot be matched to future occurrences reliably.
    """
    test_name = identity.get("tier2TestName")
    if isinstance(test_name, str) and test_name.strip():
        return f"test:{_normalize(test_name)}"

    error_code = identity.get("tier3ErrorCode")
    if isinstance(error_code, str) and error_code.strip():
        return f"error:{_normalize(error_code)}"

    cause_id = identity.get("tier1CauseId")
    if isinstance(cause_id, str) and cause_id.strip():
        return f"cause:{_normalize(cause_id)}"

    return None


def _normalize(value: str) -> str:
    return " ".join(value.strip().lower().split())


def collect_rows_from_prepared(prepared: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Collect fingerprint ledger rows from a prepared assessment's issues.

    Ledger rows without a positive run ID are skipped: they cannot prove an
    independent occurrence (a distinct CI run) actually happened.
    """
    validate_prepared_ci_failure_triage(prepared, allow_absent=True)
    if prepared.get("triageRuleVersion") is not None:
        return _collect_triage_rows(prepared)
    return _collect_legacy_rows(prepared)


def _collect_legacy_rows(prepared: Mapping[str, Any]) -> list[dict[str, Any]]:
    issues = prepared.get("issues")
    if not isinstance(issues, list):
        return []

    rows: list[dict[str, Any]] = []
    for raw_issue in issues:
        if not isinstance(raw_issue, Mapping):
            continue
        issue_number = raw_issue.get("issueNumber")
        if not isinstance(issue_number, int) or isinstance(issue_number, bool) or issue_number < 1:
            continue

        identity = raw_issue.get("identity")
        if not isinstance(identity, Mapping):
            continue
        fingerprint = compute_fingerprint(identity)
        if fingerprint is None:
            continue

        test_name = identity.get("tier2TestName")
        test_name = test_name.strip() if isinstance(test_name, str) and test_name.strip() else None

        ledger = raw_issue.get("ledger")
        if not isinstance(ledger, Mapping):
            continue
        ledger_rows = ledger.get("rows")
        if not isinstance(ledger_rows, list):
            continue

        for raw_row in ledger_rows:
            if not isinstance(raw_row, Mapping):
                continue

            run_id = raw_row.get("sourceRun", raw_row.get("runId"))
            if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 1:
                continue

            attempt = raw_row.get("attempt")
            if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
                attempt = 1

            date = raw_row.get("date")
            if not (isinstance(date, str) and date.strip()):
                created_at = raw_row.get("createdAt")
                date = created_at[:10] if isinstance(created_at, str) and len(created_at) >= 10 else None

            job = raw_row.get("job")
            job = job.strip() if isinstance(job, str) and job.strip() else None

            rows.append(
                {
                    "fingerprint": fingerprint,
                    "issueNumber": issue_number,
                    "runId": run_id,
                    "attempt": attempt,
                    "date": date,
                    "job": job,
                    "testName": test_name,
                }
            )

    rows.sort(key=lambda row: (row["fingerprint"], row["issueNumber"], row["runId"], row["attempt"]))
    return rows


def _collect_triage_rows(prepared: Mapping[str, Any]) -> list[dict[str, Any]]:
    occurrences = {
        occurrence["occurrenceId"]: occurrence
        for occurrence in prepared.get("observations", {}).get("occurrences", [])
        if isinstance(occurrence, Mapping)
        and isinstance(occurrence.get("occurrenceId"), str)
    }
    repository = prepared.get("repository")
    observed_at = prepared.get("sourceCollectedAt")
    rows = []
    for issue in prepared.get("issues", []):
        if not isinstance(issue, Mapping):
            continue
        issue_number = issue["issueNumber"]
        fingerprint = compute_fingerprint(issue.get("identity", {}))
        legacy_rows = issue.get("ledger", {}).get("rows", [])
        for case in issue["ciFailureTriage"]["cases"]:
            occurrence = occurrences[case["occurrenceId"]]
            run_id = occurrence["runId"]
            attempt = occurrence.get("attempt") or 1
            occurred_at = occurrence.get("observedAt")
            if not isinstance(occurred_at, str) or not occurred_at:
                continue
            legacy = next(
                (
                    row
                    for row in legacy_rows
                    if isinstance(row, Mapping)
                    and row.get("sourceRun", row.get("runId")) == run_id
                    and (
                        not row.get("job")
                        or row.get("job") == occurrence.get("jobName")
                    )
                ),
                {},
            )
            date = legacy.get("date")
            if not isinstance(date, str) or not date:
                date = occurred_at[:10]
            job = legacy.get("job")
            if not isinstance(job, str) or not job:
                job = occurrence.get("jobName")
            family = case["family"]
            logical_id = logical_occurrence_id(repository, occurrence)
            event_identity = {
                "logicalOccurrenceId": logical_id,
                "evidenceFingerprint": case["evidenceFingerprint"],
                "familyStatus": family["status"],
                "familyId": family.get("familyId"),
                "ruleVersion": issue["ciFailureTriage"]["ruleVersion"],
            }
            rows.append({
                "schemaVersion": 2,
                "eventId": "triage-event:" + _stable_fingerprint(event_identity).removeprefix("fnv1a64:"),
                "logicalOccurrenceId": logical_id,
                # Preserve the original compact-reader fields. They remain a
                # legacy lookup key and are not the verified family identity.
                "fingerprint": fingerprint,
                "issueNumber": issue_number,
                "runId": run_id,
                "attempt": attempt,
                "date": date,
                "job": job,
                "testName": occurrence.get("testName"),
                "familyStatus": family["status"],
                "familyId": family.get("familyId"),
                "caseId": case["caseId"],
                "evidenceFingerprint": case["evidenceFingerprint"],
                "ruleVersion": issue["ciFailureTriage"]["ruleVersion"],
                "occurredAt": occurred_at,
                "observedAt": observed_at,
                "outcome": "failure",
            })
    rows.sort(key=lambda row: (str(row["fingerprint"]), row["issueNumber"], row["runId"], row["attempt"], row["eventId"]))
    return rows


def _stable_fingerprint(value: object) -> str:
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


def _row_identity(row: Mapping[str, Any]) -> tuple[Any, ...]:
    if row.get("schemaVersion") == 2:
        return ("v2", row.get("eventId"))
    return (row.get("fingerprint"), row.get("runId"), row.get("attempt"), row.get("issueNumber"))


def read_ledger_rows(path: Path) -> list[dict[str, Any]]:
    """Read the JSONL fingerprint ledger, failing closed on damaged state."""
    return read_jsonl_rows(path)


def append_new_rows(path: Path, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Append legacy identities once and changed current triage revisions.

    Returns the rows that were actually appended (empty if all were already
    present), so recording the same prepared snapshot twice is a no-op.
    """
    with exclusive_jsonl_lock(path):
        existing = read_ledger_rows(path)
        seen = {_row_identity(row) for row in existing}
        latest = {
            row["logicalOccurrenceId"]: _row_identity(row)
            for row in current_triage_events(existing)
        }

        new_rows: list[dict[str, Any]] = []
        for row in rows:
            identity_tuple = _row_identity(row)
            if row.get("schemaVersion") == 2:
                logical_id = row["logicalOccurrenceId"]
                # A -> B -> A is a real revision, not replay of the first A.
                # Content identities may recur; append order determines current.
                if latest.get(logical_id) == identity_tuple:
                    continue
                latest[logical_id] = identity_tuple
            else:
                if identity_tuple in seen:
                    continue
                seen.add(identity_tuple)
            new_rows.append(dict(row))

        append_jsonl_rows(path, new_rows)
        return new_rows


def group_rows_by_fingerprint(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group ledger rows by fingerprint for compact-input history lookups."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        fingerprint = row.get("fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            continue
        grouped.setdefault(fingerprint, []).append(dict(row))
    return grouped


def current_triage_events(
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Select the latest append-only revision of every v2 occurrence."""
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("schemaVersion") != 2:
            continue
        logical_id = row.get("logicalOccurrenceId")
        event_id = row.get("eventId")
        if not isinstance(logical_id, str) or not logical_id:
            raise ValueError("Triage history row requires logicalOccurrenceId.")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("Triage history row requires eventId.")
        latest[logical_id] = dict(row)
    return [latest[key] for key in sorted(latest)]


def merge_occurrence_dimensions(
    base_dimensions: Mapping[str, set[Any]],
    history_rows: Iterable[Mapping[str, Any]],
) -> dict[str, set[Any]]:
    """Merge a prepared issue's own occurrence dimensions with matching history rows.

    History rows are deduplicated by (runId, attempt) so recording the same
    run's ledger row into the history ledger more than once -- for example
    because it was already present while the issue was still open -- does
    not inflate the merged occurrence counts.
    """
    dates: set[str] = set(base_dimensions.get("dates", set()))
    source_runs: set[int] = set(base_dimensions.get("sourceRuns", set()))
    jobs: set[str] = set(base_dimensions.get("jobs", set()))
    pull_requests: set[int] = set(base_dimensions.get("pullRequests", set()))

    seen_run_attempts: set[tuple[int, int]] = set()
    for row in history_rows:
        run_id = row.get("runId")
        if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 1:
            continue
        attempt = row.get("attempt")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            attempt = 1
        identity_tuple = (run_id, attempt)
        if identity_tuple in seen_run_attempts:
            continue
        seen_run_attempts.add(identity_tuple)

        source_runs.add(run_id)
        date = row.get("date")
        if isinstance(date, str) and date.strip():
            dates.add(date.strip())
        job = row.get("job")
        if isinstance(job, str) and job.strip():
            jobs.add(job.strip())

    return {
        "dates": dates,
        "sourceRuns": source_runs,
        "jobs": jobs,
        "pullRequests": pull_requests,
    }
