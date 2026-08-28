from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .poc_history import (
    append_new_rows,
    collect_rows_from_prepared,
    compute_fingerprint,
    read_ledger_rows,
)


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
    return (
        str(row.get("repository", "")).casefold(),
        row.get("issueNumber"),
        row.get("targetKind"),
        json.dumps(row.get("targetValue"), sort_keys=True),
    )


def _case_state(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("fingerprint"),
        row.get("category"),
        row.get("disposition"),
    )


def _append_case_events(
    path: Path,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
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
        else:
            event["eventKind"] = "transition"
            event["previousDisposition"] = previous.get("disposition")
        appended.append(event)
        latest[identity] = event

    if appended:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        needs_separator = False
        if path.exists() and path.stat().st_size > 0:
            with path.open("rb") as existing_stream:
                existing_stream.seek(-1, os.SEEK_END)
                needs_separator = existing_stream.read(1) != b"\n"
        with path.open("a", encoding="utf-8") as stream:
            if needs_separator:
                stream.write("\n")
            for row in appended:
                stream.write(json.dumps(row, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o600)
    return appended
