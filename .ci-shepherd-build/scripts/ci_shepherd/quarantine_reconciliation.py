from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Mapping

from .quarantine import (
    read_quarantine_session_events,
    record_quarantine_session_event,
)


_PULL_URL_RE = re.compile(
    r"^https://github\.com/(?P<repository>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
    r"/pull/(?P<number>[1-9][0-9]*)$",
    re.IGNORECASE,
)


def reconcile_quarantine_pull_requests(
    *,
    state_directory: Path,
    repository: str,
    recorded_at: str,
    get_pull: Callable[[str, int], Mapping[str, Any]],
) -> dict[str, object]:
    events = read_quarantine_session_events(state_directory)
    latest_by_batch: dict[str, dict[str, Any]] = {}
    for event in events:
        batch_id = event.get("batchId")
        if (
            isinstance(batch_id, str)
            and str(event.get("repository", "")).casefold()
            == repository.casefold()
        ):
            latest_by_batch[batch_id] = event

    outcomes: list[dict[str, object]] = []
    for batch_id, event in sorted(latest_by_batch.items()):
        if event.get("status") != "pull-request-open":
            continue
        url = event.get("pullRequestUrl")
        head_sha = event.get("pullRequestHeadSha")
        match = _PULL_URL_RE.fullmatch(str(url))
        if (
            match is None
            or match.group("repository").casefold() != repository.casefold()
            or not isinstance(head_sha, str)
            or re.fullmatch(r"[0-9a-fA-F]{40}", head_sha) is None
        ):
            outcomes.append(
                {
                    "batchId": batch_id,
                    "status": "unverifiable",
                    "reason": "The ledger lacks an exact pull request URL and head SHA.",
                }
            )
            continue

        pull = get_pull(repository, int(match.group("number")))
        actual_head = pull.get("head")
        actual_head_sha = (
            actual_head.get("sha") if isinstance(actual_head, Mapping) else None
        )
        if (
            pull.get("html_url") != url
            or not isinstance(actual_head_sha, str)
            or actual_head_sha.casefold() != head_sha.casefold()
        ):
            outcomes.append(
                {
                    "batchId": batch_id,
                    "status": "unverifiable",
                    "reason": "The live pull request identity or head has changed.",
                }
            )
            continue

        test_names = [
            str(test["testName"])
            for test in event.get("tests", [])
            if isinstance(test, Mapping)
            and isinstance(test.get("testName"), str)
        ]
        blocked_targets = [
            {
                "testName": str(target["test"]["testName"]),
                "reason": str(target["reason"]),
            }
            for target in event.get("blockedTargets", [])
            if isinstance(target, Mapping)
            and isinstance(target.get("test"), Mapping)
            and isinstance(target["test"].get("testName"), str)
            and isinstance(target.get("reason"), str)
        ]
        full_request = {
            **event,
            "tests": [
                *event.get("tests", []),
                *[
                    target["test"]
                    for target in event.get("blockedTargets", [])
                    if isinstance(target, Mapping)
                    and isinstance(target.get("test"), Mapping)
                ],
            ],
        }
        common = {
            "state_directory": state_directory,
            "request": full_request,
            "recorded_at": recorded_at,
            "session_id": str(event["sessionId"]),
        }
        if pull.get("state") == "closed" and pull.get("merged_at") is not None:
            record_quarantine_session_event(
                **common,
                status="completed",
                pull_request_url=str(url),
                pull_request_head_sha=head_sha,
                completed_test_names=test_names,
                blocked_targets=blocked_targets,
            )
            status = "completed"
        elif pull.get("state") == "closed" and pull.get("merged_at") is None:
            record_quarantine_session_event(
                **common,
                status="failed",
                failure_reason="The quarantine pull request closed without merging.",
                blocked_targets=blocked_targets,
            )
            status = "closed-unmerged"
        elif pull.get("state") == "open":
            status = "pending"
        else:
            outcomes.append(
                {
                    "batchId": batch_id,
                    "status": "unverifiable",
                    "reason": "GitHub returned an unsupported pull request state.",
                }
            )
            continue
        outcomes.append(
            {
                "batchId": batch_id,
                "status": status,
                "pullRequestUrl": url,
            }
        )
    return {
        "schemaVersion": 1,
        "repository": repository,
        "outcomes": outcomes,
    }
