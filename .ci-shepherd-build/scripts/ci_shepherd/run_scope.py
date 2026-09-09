from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def verified_run_scope(payload: Mapping[str, Any], *, default_branch: str = "main") -> dict[str, object]:
    repository = payload.get("targetRepository")
    event = payload.get("event")
    ref = payload.get("branch") or payload.get("headBranch")
    head_sha = payload.get("headSha")
    identity = {
        "repository": repository,
        "event": event,
        "ref": ref,
        "headSha": head_sha,
    }
    if not all(isinstance(value, str) and value for value in identity.values()):
        return {"kind": "unknown", "reason": "incomplete-run-identity"}

    if event != "pull_request":
        if ref == default_branch:
            return {"kind": "main", **identity}
        return {"kind": "branch", **identity}

    candidates = payload.get("subjectPullRequests")
    if not isinstance(candidates, list):
        return {"kind": "unknown", **identity, "reason": "missing-subject-pull-request"}
    matches = [
        candidate
        for candidate in candidates
        if isinstance(candidate, Mapping)
        and isinstance(candidate.get("number"), int)
        and not isinstance(candidate.get("number"), bool)
        and candidate["number"] > 0
        and candidate.get("headSha") == head_sha
        and isinstance(candidate.get("baseRepository"), str)
        and candidate["baseRepository"].casefold() == repository.casefold()
    ]
    if len(matches) != 1 or len(candidates) != 1:
        return {"kind": "unknown", **identity, "reason": "ambiguous-subject-pull-request"}
    return {
        "kind": "pull-request",
        **identity,
        "pullRequest": matches[0]["number"],
    }


def reported_issue_scope(
    issue_payload: Mapping[str, Any],
    run_id: int,
) -> dict[str, object] | None:
    ledger = issue_payload.get("ledger")
    rows = ledger.get("rows") if isinstance(ledger, Mapping) else None
    if not isinstance(rows, list):
        return None
    matching = [
        row
        for row in rows
        if isinstance(row, Mapping) and row.get("sourceRun") == run_id
    ]
    if not matching:
        return None
    pull_requests = {
        row.get("pullRequest")
        for row in matching
        if isinstance(row.get("pullRequest"), int)
        and not isinstance(row.get("pullRequest"), bool)
        and row["pullRequest"] > 0
    }
    has_unscoped = any(row.get("pullRequest") is None for row in matching)
    if len(pull_requests) == 1 and not has_unscoped:
        return {"kind": "pull-request", "pullRequest": next(iter(pull_requests))}
    if not pull_requests and has_unscoped:
        return {"kind": "main"}
    return {"kind": "unknown", "reason": "conflicting-reported-scope"}


def scopes_conflict(
    reported: Mapping[str, object] | None,
    verified: Mapping[str, object],
) -> bool:
    if reported is None or "unknown" in {reported.get("kind"), verified.get("kind")}:
        return False
    return (
        reported.get("kind") != verified.get("kind")
        or reported.get("pullRequest") != verified.get("pullRequest")
    )
