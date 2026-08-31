from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .poc_history import (
    append_new_rows,
    collect_rows_from_prepared,
    compute_fingerprint,
    read_ledger_rows,
)
from .jsonl import append_jsonl_rows, exclusive_jsonl_lock

CaseKey = tuple[str, int, str, str]
REVIEW_WAKEUP_REASONS = frozenset(
    {
        "closure-without-recurrence",
        "escalation-reminder",
        "pending-pr-timeout",
        "retry-backoff",
    }
)


def case_key(
    repository: str,
    issue_number: int,
    target: Mapping[str, Any],
) -> CaseKey:
    if not isinstance(repository, str) or not repository.strip():
        raise ValueError("Case repository must be a nonempty string.")
    if (
        not isinstance(issue_number, int)
        or isinstance(issue_number, bool)
        or issue_number < 1
    ):
        raise ValueError("Case issue number must be a positive integer.")
    target_kind = target.get("kind")
    if not isinstance(target_kind, str) or not target_kind:
        raise ValueError("Case target kind must be a nonempty string.")
    return (
        repository.casefold(),
        issue_number,
        target_kind,
        json.dumps(target.get("value"), sort_keys=True),
    )


def load_latest_case_state(
    state_directory: Path,
    repository: str,
) -> dict[CaseKey, dict[str, Any]]:
    if not isinstance(repository, str) or not repository.strip():
        raise ValueError("Case repository must be a nonempty string.")
    latest: dict[CaseKey, dict[str, Any]] = {}
    for row in read_ledger_rows(
        state_directory / "ledgers" / "case-events.jsonl"
    ):
        if str(row.get("repository", "")).casefold() != repository.casefold():
            continue
        issue_number = row.get("issueNumber")
        target_kind = row.get("targetKind")
        if (
            not isinstance(issue_number, int)
            or isinstance(issue_number, bool)
            or issue_number < 1
            or not isinstance(target_kind, str)
            or not target_kind
        ):
            continue
        latest[
            case_key(
                repository,
                issue_number,
                {"kind": target_kind, "value": row.get("targetValue")},
            )
        ] = dict(row)
    return latest


def record_review_events(
    state_directory: Path,
    repository: str,
    reviewed_at: str,
    *,
    issue_numbers: list[int],
    pull_request_numbers: list[int],
) -> list[dict[str, Any]]:
    reviewed_instant = _parse_timestamp(reviewed_at, "reviewedAt")
    rows = [
        {
            "schemaVersion": 1,
            "repository": repository,
            "targetKind": target_kind,
            "targetNumber": number,
            "reviewedAt": _format_timestamp(reviewed_instant),
        }
        for target_kind, numbers in (
            ("issue", issue_numbers),
            ("pull-request", pull_request_numbers),
        )
        for number in sorted(set(numbers))
        if isinstance(number, int) and not isinstance(number, bool) and number > 0
    ]
    path = state_directory / "ledgers" / "review-events.jsonl"
    if state_directory.is_symlink():
        raise ValueError("Review state directory must not be a symlink.")
    with exclusive_jsonl_lock(path):
        existing = read_ledger_rows(path)
        seen = {
            (
                str(row.get("repository", "")).casefold(),
                row.get("targetKind"),
                row.get("targetNumber"),
                row.get("reviewedAt"),
            )
            for row in existing
        }
        appended = [
            row
            for row in rows
            if (
                repository.casefold(),
                row["targetKind"],
                row["targetNumber"],
                row["reviewedAt"],
            )
            not in seen
        ]
        if state_directory.is_symlink():
            raise ValueError("Review state directory must not be a symlink.")
        append_jsonl_rows(path, appended)
        return appended


def record_review_wakeup(
    state_directory: Path,
    repository: str,
    *,
    target_kind: str,
    target_number: int,
    evaluate_at: str,
    reason: str,
) -> dict[str, Any]:
    if not isinstance(repository, str) or not repository.strip():
        raise ValueError("Wakeup repository must be a nonempty string.")
    if target_kind not in {"issue", "pull-request"}:
        raise ValueError("Wakeup target kind must be issue or pull-request.")
    if (
        not isinstance(target_number, int)
        or isinstance(target_number, bool)
        or target_number < 1
    ):
        raise ValueError("Wakeup target number must be a positive integer.")
    if reason not in REVIEW_WAKEUP_REASONS:
        raise ValueError(f"Unsupported review wakeup reason: {reason}.")
    evaluate_instant = _parse_timestamp(evaluate_at, "evaluateAt")
    row = {
        "schemaVersion": 1,
        "repository": repository,
        "targetKind": target_kind,
        "targetNumber": target_number,
        "evaluateAt": _format_timestamp(evaluate_instant),
        "reason": reason,
    }
    path = state_directory / "ledgers" / "review-wakeups.jsonl"
    identity = (
        repository.casefold(),
        target_kind,
        target_number,
        row["evaluateAt"],
        reason,
    )
    with exclusive_jsonl_lock(path):
        if any(
            (
                str(existing.get("repository", "")).casefold(),
                existing.get("targetKind"),
                existing.get("targetNumber"),
                existing.get("evaluateAt"),
                existing.get("reason"),
            )
            == identity
            for existing in read_ledger_rows(path)
        ):
            return row
        append_jsonl_rows(path, [row])
    return row


def load_review_schedule(
    state_directory: Path,
    repository: str,
    observed_at: str,
    *,
    issue_numbers: list[int],
    pull_request_numbers: list[int],
) -> dict[str, Any]:
    observed_instant = _parse_timestamp(observed_at, "observedAt")
    current_targets = {
        ("issue", number)
        for number in issue_numbers
        if isinstance(number, int) and not isinstance(number, bool) and number > 0
    } | {
        ("pull-request", number)
        for number in pull_request_numbers
        if isinstance(number, int) and not isinstance(number, bool) and number > 0
    }
    latest: dict[tuple[str, int], datetime] = {}
    for row in read_ledger_rows(
        state_directory / "ledgers" / "review-events.jsonl"
    ):
        if str(row.get("repository", "")).casefold() != repository.casefold():
            continue
        key = (row.get("targetKind"), row.get("targetNumber"))
        if key not in current_targets:
            continue
        try:
            reviewed = _parse_timestamp(row.get("reviewedAt"), "reviewedAt")
        except ValueError:
            continue
        if reviewed > latest.get(key, datetime.min.replace(tzinfo=UTC)):
            latest[key] = reviewed

    contexts: dict[str, dict[str, dict[str, str]]] = {
        "issues": {},
        "pullRequests": {},
    }
    pending_wakeups: dict[tuple[str, int], tuple[datetime, str]] = {}
    for row in read_ledger_rows(
        state_directory / "ledgers" / "review-wakeups.jsonl"
    ):
        if str(row.get("repository", "")).casefold() != repository.casefold():
            continue
        key = (row.get("targetKind"), row.get("targetNumber"))
        if key not in current_targets or row.get("reason") not in REVIEW_WAKEUP_REASONS:
            continue
        try:
            evaluate_at = _parse_timestamp(row.get("evaluateAt"), "evaluateAt")
        except ValueError:
            continue
        reviewed_at = latest.get(key)
        if reviewed_at is not None and evaluate_at <= reviewed_at:
            continue
        candidate = (evaluate_at, str(row["reason"]))
        if candidate < pending_wakeups.get(
            key,
            (datetime.max.replace(tzinfo=UTC), ""),
        ):
            pending_wakeups[key] = candidate

    due_issues: list[int] = []
    due_pull_requests: list[int] = []
    for target_kind, number in sorted(current_targets):
        reviewed = latest.get((target_kind, number))
        wakeup = pending_wakeups.get((target_kind, number))
        context = {}
        if reviewed is not None:
            context["lastReviewedAt"] = _format_timestamp(reviewed)
        if wakeup is not None:
            evaluate_at, reason = wakeup
            context["reassessAt"] = _format_timestamp(evaluate_at)
            context["wakeReason"] = reason
        if not context:
            continue
        if target_kind == "issue":
            contexts["issues"][str(number)] = context
            if wakeup is not None and observed_instant >= wakeup[0]:
                due_issues.append(number)
        else:
            contexts["pullRequests"][str(number)] = context
            if wakeup is not None and observed_instant >= wakeup[0]:
                due_pull_requests.append(number)
    return {
        "schemaVersion": 1,
        "dueIssueNumbers": due_issues,
        "duePullRequestNumbers": due_pull_requests,
        **contexts,
    }


def _parse_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a nonempty timestamp.")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label} must be an ISO 8601 timestamp.") from error
    if instant.tzinfo is None:
        raise ValueError(f"{label} must include a UTC offset.")
    return instant.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def record_poc_ledgers(
    state_directory: Path,
    repository: str,
    prepared: Mapping[str, Any],
    judgments: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ledgers = state_directory / "ledgers"
    ledgers.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(ledgers, 0o700)

    fingerprint_rows = append_new_rows(
        ledgers / "fingerprints.jsonl",
        collect_rows_from_prepared(prepared),
    )
    case_event_rows = _append_case_events(
        ledgers / "case-events.jsonl",
        _collect_case_events(repository, prepared, judgments),
    )
    return fingerprint_rows, case_event_rows


def _collect_case_events(
    repository: str,
    prepared: Mapping[str, Any],
    judgments: Mapping[str, Any],
) -> list[dict[str, Any]]:
    prepared_issues = {
        issue["issueNumber"]: issue
        for issue in prepared.get("issues", [])
        if isinstance(issue, Mapping) and isinstance(issue.get("issueNumber"), int)
    }
    snapshot_id = judgments["snapshotId"]
    observed_at = prepared["sourceCollectedAt"]
    rows: list[dict[str, Any]] = []
    for issue_judgment in judgments.get("issues", []):
        if not isinstance(issue_judgment, Mapping):
            continue
        issue_number = issue_judgment.get("issueNumber")
        if not isinstance(issue_number, int):
            continue
        prepared_issue = prepared_issues.get(issue_number, {})
        source_evidence_fingerprint = _source_evidence_fingerprint(prepared_issue)
        identity = prepared_issue.get("identity")
        fingerprint = (
            compute_fingerprint(identity)
            if isinstance(identity, Mapping)
            else None
        )
        for recommendation in issue_judgment.get("recommendations", []):
            if not isinstance(recommendation, Mapping):
                continue
            target = recommendation.get("target")
            if not isinstance(target, Mapping):
                continue
            rows.append(
                {
                    "repository": repository,
                    "issueNumber": issue_number,
                    "fingerprint": fingerprint,
                    "targetKind": target.get("kind"),
                    "targetValue": target.get("value"),
                    "category": issue_judgment.get("category"),
                    "disposition": recommendation.get("disposition"),
                    "confidence": recommendation.get("confidence"),
                    "sourceEvidenceFingerprint": source_evidence_fingerprint,
                    "snapshotId": snapshot_id,
                    "observedAt": observed_at,
                }
            )
    rows.sort(
        key=lambda row: (
            row["issueNumber"],
            str(row["targetKind"]),
            json.dumps(row["targetValue"], sort_keys=True),
        )
    )
    return rows


def _case_identity(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return case_key(
        str(row.get("repository", "")),
        row.get("issueNumber"),
        {
            "kind": row.get("targetKind"),
            "value": row.get("targetValue"),
        },
    )


def _case_state(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("fingerprint"),
        row.get("category"),
        row.get("disposition"),
    )


def _source_evidence_fingerprint(prepared_issue: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        prepared_issue,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    value = 0xCBF29CE484222325
    for byte in encoded:
        value ^= byte
        value = (value * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"fnv1a64:{value:016x}"


def _append_case_events(
    path: Path,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    with exclusive_jsonl_lock(path):
        existing = read_ledger_rows(path)
        latest = {
            _case_identity(row): row
            for row in existing
        }
        bootstrap = not existing
        appended: list[dict[str, Any]] = []
        for row in rows:
            identity = _case_identity(row)
            previous = latest.get(identity)
            if previous is not None and _case_state(previous) == _case_state(row):
                continue
            event = dict(row)
            if bootstrap:
                event["eventKind"] = "bootstrap"
            elif previous is None:
                event["eventKind"] = "created"
            elif (
                previous.get("sourceEvidenceFingerprint")
                == row.get("sourceEvidenceFingerprint")
            ):
                event["eventKind"] = "convergence"
                event["previousDisposition"] = previous.get("disposition")
            else:
                event["eventKind"] = "transition"
                event["previousDisposition"] = previous.get("disposition")
            appended.append(event)
            latest[identity] = event

        append_jsonl_rows(path, appended)
        return appended
