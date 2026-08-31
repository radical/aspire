from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from .quarantine import record_quarantine_session_event
from .quarantine_mutation import (
    validate_quarantine_commit_validation,
    validate_quarantine_mutation_result,
)


_OUTCOMES = frozenset({"pull-request-open", "completed", "blocked", "failed"})
_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_RESULT_KEYS = frozenset(
    {
        "schemaVersion",
        "repository",
        "snapshotId",
        "batchId",
        "sessionId",
        "outcome",
        "completedTests",
        "blockedTargets",
        "pullRequest",
        "failureReason",
    }
)


def validate_quarantine_worker_result(
    request: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, object]:
    unknown = set(result) - _RESULT_KEYS
    if unknown:
        raise ValueError(
            "Quarantine worker result contains unsupported fields: "
            + ", ".join(sorted(unknown))
        )
    if result.get("schemaVersion") != 1:
        raise ValueError("Unsupported quarantine worker result schemaVersion.")
    for field in ("repository", "snapshotId", "batchId"):
        if result.get(field) != request.get(field):
            raise ValueError(f"Quarantine worker result {field} does not match.")
    session_id = _require_string(result, "sessionId")
    outcome = result.get("outcome")
    if outcome not in _OUTCOMES:
        raise ValueError("Quarantine worker result outcome is unsupported.")

    requested_names = {
        item.get("testName")
        for item in request.get("tests", [])
        if isinstance(item, Mapping) and isinstance(item.get("testName"), str)
    }
    if not requested_names:
        raise ValueError("Quarantine request contains no tests.")
    completed = _unique_string_list(result.get("completedTests"), "completedTests")
    unknown_completed = set(completed) - requested_names
    if unknown_completed:
        raise ValueError("Quarantine worker completed an unrequested test.")

    blocked_targets = result.get("blockedTargets")
    if not isinstance(blocked_targets, list):
        raise ValueError("blockedTargets must be a list.")
    blocked_by_name: dict[str, str] = {}
    for target in blocked_targets:
        if not isinstance(target, Mapping) or set(target) != {"testName", "reason"}:
            raise ValueError("Each blocked target requires only testName and reason.")
        test_name = _require_string(target, "testName")
        reason = _require_string(target, "reason")
        if test_name in blocked_by_name:
            raise ValueError("blockedTargets must contain unique tests.")
        blocked_by_name[test_name] = reason
    if set(blocked_by_name) - requested_names:
        raise ValueError("Quarantine worker blocked an unrequested test.")
    if set(completed) & set(blocked_by_name):
        raise ValueError("A quarantine target cannot be completed and blocked.")
    if set(completed) | set(blocked_by_name) != requested_names:
        raise ValueError("Every requested quarantine test must have an outcome.")

    pull_request = result.get("pullRequest")
    failure_reason = result.get("failureReason")
    if outcome in {"pull-request-open", "completed"}:
        if not completed:
            raise ValueError(f"A {outcome} result must complete at least one test.")
        if not isinstance(pull_request, Mapping) or set(pull_request) != {
            "url",
            "headSha",
        }:
            raise ValueError(f"A {outcome} result requires URL and head SHA.")
        _require_string(pull_request, "url")
        head_sha = _require_string(pull_request, "headSha")
        if _SHA_RE.fullmatch(head_sha) is None:
            raise ValueError("pullRequest.headSha must be a 40-character Git SHA.")
        if failure_reason is not None:
            raise ValueError(f"failureReason is invalid for {outcome}.")
    else:
        if pull_request is not None:
            raise ValueError(f"pullRequest is invalid for {outcome}.")
        if completed:
            raise ValueError(f"completedTests must be empty for {outcome}.")
        _require_string(result, "failureReason")

    return {
        "schemaVersion": 1,
        "repository": result["repository"],
        "snapshotId": result["snapshotId"],
        "batchId": result["batchId"],
        "sessionId": session_id,
        "outcome": outcome,
        "completedTests": sorted(completed),
        "blockedTargets": [
            {"testName": name, "reason": blocked_by_name[name]}
            for name in sorted(blocked_by_name)
        ],
        **({"pullRequest": dict(pull_request)} if pull_request is not None else {}),
        **({"failureReason": failure_reason} if failure_reason is not None else {}),
    }


def record_quarantine_worker_result(
    *,
    state_directory: Path,
    request: Mapping[str, Any],
    result: Mapping[str, Any],
    recorded_at: str,
    pull_request_document: Mapping[str, Any] | None = None,
    pull_request_files: list[Mapping[str, Any]] | None = None,
    mutation_result: Mapping[str, Any] | None = None,
    commit_validation: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    validated = validate_quarantine_worker_result(request, result)
    outcome = validated["outcome"]
    if outcome in {"pull-request-open", "completed"}:
        pull_request = validated["pullRequest"]
        if not isinstance(pull_request, Mapping):
            raise ValueError("Validated pull request is missing.")
        _validate_live_pull_request(
            pull_request,
            pull_request_document,
            completed=outcome == "completed",
        )
        if mutation_result is None:
            raise ValueError(
                "A successful quarantine result requires deterministic "
                "mutation validation."
            )
        validated_mutation = validate_quarantine_mutation_result(
            request,
            mutation_result,
        )
        if commit_validation is None:
            raise ValueError(
                "A successful quarantine result requires commit validation."
            )
        validated_commit = validate_quarantine_commit_validation(
            validated_mutation,
            commit_validation,
        )
        if (
            str(validated_commit["commitSha"]).casefold()
            != str(pull_request["headSha"]).casefold()
        ):
            raise ValueError(
                "Quarantine pull request head does not match commit validation."
            )
        if validated_mutation["completedTests"] != validated["completedTests"]:
            raise ValueError(
                "Quarantine worker completedTests do not match mutation validation."
            )
        _validate_live_pull_request_files(
            validated_mutation,
            pull_request_document,
            pull_request_files,
        )
        return record_quarantine_session_event(
            state_directory,
            request,
            status=str(outcome),
            recorded_at=recorded_at,
            session_id=str(validated["sessionId"]),
            pull_request_url=str(pull_request["url"]),
            pull_request_head_sha=str(pull_request["headSha"]),
            completed_test_names=list(validated["completedTests"]),
            blocked_targets=list(validated["blockedTargets"]),
            allow_pull_request_head_update=outcome == "pull-request-open",
            mutation_validation=validated_mutation,
        )
    return record_quarantine_session_event(
        state_directory,
        request,
        status="failed",
        recorded_at=recorded_at,
        session_id=str(validated["sessionId"]),
        failure_reason=str(validated["failureReason"]),
        blocked_targets=list(validated["blockedTargets"]),
    )


def _validate_live_pull_request(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any] | None,
    *,
    completed: bool,
) -> None:
    if actual is None:
        raise ValueError("The pull request must be verified with a GitHub GET.")
    head = actual.get("head")
    if (
        actual.get("html_url") != expected.get("url")
        or (
            completed
            and (
                actual.get("state") != "closed"
                or actual.get("merged_at") is None
            )
        )
        or (
            not completed
            and (
                actual.get("state") != "open"
                or actual.get("draft") is not True
            )
        )
        or not isinstance(head, Mapping)
        or str(head.get("sha", "")).casefold()
        != str(expected.get("headSha", "")).casefold()
    ):
        raise ValueError(
            "The live pull request is not in the expected state at the expected head."
        )


def _validate_live_pull_request_files(
    mutation_result: Mapping[str, Any],
    pull_request: Mapping[str, Any] | None,
    files: list[Mapping[str, Any]] | None,
) -> None:
    if pull_request is None or files is None:
        raise ValueError(
            "The pull request file list must be verified with a GitHub GET."
        )
    expected = mutation_result.get("changedFiles")
    actual = sorted(
        file.get("filename")
        for file in files
        if isinstance(file.get("filename"), str)
        and file.get("status") == "modified"
    )
    if (
        not isinstance(expected, list)
        or pull_request.get("changed_files") != len(files)
        or actual != expected
        or len(actual) != len(files)
    ):
        raise ValueError(
            "The live pull request files do not match mutation validation."
        )


def _unique_string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError(f"{field} must be a list of nonempty strings.")
    if len(value) != len(set(value)):
        raise ValueError(f"{field} must contain unique values.")
    return value


def _require_string(document: Mapping[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be nonempty.")
    return value
