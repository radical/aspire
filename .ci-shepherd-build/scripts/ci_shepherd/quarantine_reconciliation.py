from __future__ import annotations

import re
from pathlib import Path
import json
import subprocess
from tempfile import TemporaryDirectory
from typing import Any, Callable, Mapping

from .quarantine import (
    read_quarantine_session_events,
    record_quarantine_session_event,
)
from .quarantine_mutation import (
    validate_quarantine_mutation_result,
    validate_quarantine_post_inspection,
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
    verify_merged_source: Callable[
        [Mapping[str, Any], Mapping[str, Any]],
        bool,
    ]
    | None = None,
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
            mutation_validation = event.get("mutationValidation")
            if (
                not isinstance(mutation_validation, Mapping)
                or verify_merged_source is None
                or not verify_merged_source(event, pull)
            ):
                outcomes.append(
                    {
                        "batchId": batch_id,
                        "status": "unverifiable",
                        "reason": (
                            "The exact quarantine attributes were not verified "
                            "at the merged commit."
                        ),
                    }
                )
                continue
            record_quarantine_session_event(
                **common,
                status="completed",
                pull_request_url=str(url),
                pull_request_head_sha=head_sha,
                completed_test_names=test_names,
                blocked_targets=blocked_targets,
                mutation_validation=mutation_validation,
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


def verify_merged_quarantine_source(
    request: Mapping[str, Any],
    mutation_result: Mapping[str, Any],
    *,
    merge_commit_sha: str,
    tool_project: Path,
    get_file: Callable[[str, str], bytes],
    timeout_seconds: int = 300,
) -> bool:
    if re.fullmatch(r"[0-9a-fA-F]{40}", merge_commit_sha) is None:
        return False
    try:
        validated_mutation = validate_quarantine_mutation_result(
            request,
            mutation_result,
        )
        with TemporaryDirectory() as temporary_directory:
            tests_root = Path(temporary_directory)
            for changed_file in validated_mutation["changedFiles"]:
                if (
                    not isinstance(changed_file, str)
                    or not changed_file.startswith("tests/")
                ):
                    return False
                relative_path = Path(changed_file).relative_to("tests")
                if relative_path.is_absolute() or ".." in relative_path.parts:
                    return False
                destination = tests_root / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(
                    get_file(changed_file, merge_commit_sha)
                )
            test_names = list(validated_mutation["completedTests"])
            completed = subprocess.run(
                [
                    "dotnet",
                    "run",
                    "--project",
                    str(tool_project),
                    "--no-restore",
                    "--verbosity",
                    "quiet",
                    "--",
                    "--inspect",
                    "--root",
                    str(tests_root),
                    *test_names,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            if completed.returncode != 0:
                return False
            inspection = json.loads(completed.stdout)
            if not isinstance(inspection, Mapping):
                return False
            validated_source = validate_quarantine_post_inspection(
                request,
                inspection,
            )
            return (
                validated_source["completedTests"]
                == validated_mutation["completedTests"]
            )
    except (
        json.JSONDecodeError,
        OSError,
        subprocess.TimeoutExpired,
        ValueError,
    ):
        return False
