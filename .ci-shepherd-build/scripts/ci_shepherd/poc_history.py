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

import json
import os
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any


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


def _row_identity(row: Mapping[str, Any]) -> tuple[Any, Any, Any, Any]:
    return (row.get("fingerprint"), row.get("runId"), row.get("attempt"), row.get("issueNumber"))


def read_ledger_rows(path: Path) -> list[dict[str, Any]]:
    """Read the JSONL fingerprint ledger, returning an empty list if absent.

    A malformed or truncated line -- for example a partial write left behind
    by a crash, or a stray line missing its terminating newline -- is skipped
    rather than raising, so one bad line does not brick all later history.
    """
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def append_new_rows(path: Path, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Append only rows whose identity tuple is not already recorded.

    Returns the rows that were actually appended (empty if all were already
    present), so recording the same prepared snapshot twice is a no-op.
    """
    existing = read_ledger_rows(path)
    seen = {_row_identity(row) for row in existing}

    new_rows: list[dict[str, Any]] = []
    for row in rows:
        identity_tuple = _row_identity(row)
        if identity_tuple in seen:
            continue
        seen.add(identity_tuple)
        new_rows.append(dict(row))

    if new_rows:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)

        # An existing nonempty ledger that doesn't end with a newline (e.g. a
        # prior truncated write) would otherwise concatenate our first row
        # onto its last line. Insert a separating newline first so appends
        # never corrupt an existing line -- the merged line is then just one
        # more malformed line for read_ledger_rows to skip.
        needs_separator = False
        if path.exists() and path.stat().st_size > 0:
            with path.open("rb") as existing_stream:
                existing_stream.seek(-1, os.SEEK_END)
                needs_separator = existing_stream.read(1) != b"\n"

        with path.open("a", encoding="utf-8") as stream:
            if needs_separator:
                stream.write("\n")
            for row in new_rows:
                # Write the encoded row and its newline as a single string so
                # one row is always one append, never split across writes.
                stream.write(json.dumps(row, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o600)

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
