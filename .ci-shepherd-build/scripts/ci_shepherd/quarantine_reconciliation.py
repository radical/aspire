from __future__ import annotations

from dataclasses import dataclass
import re
from pathlib import Path
import json
import os
import subprocess
from tempfile import TemporaryDirectory
from typing import Any, Callable, Mapping

from .quarantine import (
    quarantine_tool_tree_digest,
    read_quarantine_session_events,
    record_quarantine_session_event,
)
from .quarantine_mutation import (
    validate_quarantine_mutation_result,
    validate_quarantine_post_inspection,
)
from .quarantine_result import (
    validate_quarantine_pull_request_target,
    validate_required_quarantine_approvals,
)


_PULL_URL_RE = re.compile(
    r"^https://github\.com/(?P<repository>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
    r"/pull/(?P<number>[1-9][0-9]*)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MergedQuarantineSourceVerification:
    verified: bool
    code: str
    reason: str

    def __bool__(self) -> bool:
        return self.verified


def reconcile_quarantine_pull_requests(
    *,
    state_directory: Path,
    repository: str,
    recorded_at: str,
    get_pull: Callable[[str, int], Mapping[str, Any]],
    get_reviews: Callable[[str, int], list[Mapping[str, Any]]] | None = None,
    verify_merged_source: Callable[
        [Mapping[str, Any], Mapping[str, Any]],
        bool | MergedQuarantineSourceVerification,
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
        try:
            validate_quarantine_pull_request_target(event, pull)
        except ValueError as error:
            outcomes.append(
                {
                    "batchId": batch_id,
                    "status": "unverifiable",
                    "reason": str(error),
                }
            )
            continue
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
            try:
                validate_required_quarantine_approvals(
                    event,
                    pull,
                    (
                        get_reviews(repository, int(match.group("number")))
                        if get_reviews is not None
                        else None
                    ),
                )
            except ValueError as error:
                outcomes.append(
                    {
                        "batchId": batch_id,
                        "status": "unverifiable",
                        "reason": str(error),
                    }
                )
                continue
            mutation_validation = event.get("mutationValidation")
            verification = (
                verify_merged_source(event, pull)
                if (
                    isinstance(mutation_validation, Mapping)
                    and verify_merged_source is not None
                )
                else False
            )
            verification_succeeded = (
                verification is True
                or (
                    isinstance(
                        verification,
                        MergedQuarantineSourceVerification,
                    )
                    and verification.verified
                    and verification.code == "verified"
                )
            )
            if not verification_succeeded:
                reason = (
                    verification.reason
                    if isinstance(
                        verification,
                        MergedQuarantineSourceVerification,
                    )
                    else (
                        "The exact quarantine attributes were not verified "
                        "at the merged commit."
                    )
                )
                outcomes.append(
                    {
                        "batchId": batch_id,
                        "status": "unverifiable",
                        "reason": reason,
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
) -> MergedQuarantineSourceVerification:
    if re.fullmatch(r"[0-9a-fA-F]{40}", merge_commit_sha) is None:
        return _merged_verification(
            "invalid-input",
            "The merge commit SHA is invalid.",
        )
    try:
        validated_mutation = validate_quarantine_mutation_result(
            request,
            mutation_result,
        )
    except ValueError as error:
        return _merged_verification(
            "invalid-input",
            f"The merged-source input is invalid: {error}",
        )

    expected_inspector_digest = request.get("inspectorTreeDigest")
    if not isinstance(expected_inspector_digest, str):
        return _merged_verification(
            "invalid-input",
            "The request lacks an inspector tree digest.",
        )
    try:
        actual_inspector_digest = quarantine_tool_tree_digest(tool_project)
    except OSError as error:
        return _merged_verification(
            "inspector-runtime-failed",
            f"The merged-source inspector could not be read: {error}",
        )
    if actual_inspector_digest != expected_inspector_digest:
        return _merged_verification(
            "inspector-digest-drift",
            "The merged-source inspector differs from the inspected version.",
        )

    try:
        with TemporaryDirectory() as temporary_directory:
            tests_root = Path(temporary_directory)
            for changed_file in validated_mutation["changedFiles"]:
                if (
                    not isinstance(changed_file, str)
                    or not changed_file.startswith("tests/")
                ):
                    return _merged_verification(
                        "invalid-input",
                        "A changed file is outside the tests directory.",
                    )
                relative_path = Path(changed_file).relative_to("tests")
                if relative_path.is_absolute() or ".." in relative_path.parts:
                    return _merged_verification(
                        "invalid-input",
                        "A changed test path is unsafe.",
                    )
                destination = tests_root / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    content = get_file(changed_file, merge_commit_sha)
                except (OSError, ValueError) as error:
                    return _merged_verification(
                        "source-fetch-failed",
                        f"The merged source could not be fetched: {error}",
                    )
                if not isinstance(content, bytes):
                    return _merged_verification(
                        "source-fetch-failed",
                        "The merged source response was not bytes.",
                    )
                destination.write_bytes(content)
            test_names = list(validated_mutation["completedTests"])
            try:
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
                    env={
                        **os.environ,
                        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
                        "DOTNET_CLI_UI_LANGUAGE": "en-US",
                        "DOTNET_NOLOGO": "1",
                        "DOTNET_ROLL_FORWARD": "Major",
                        "MSBUILDTERMINALLOGGER": "false",
                    },
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                return _merged_verification(
                    "inspector-runtime-failed",
                    f"The merged-source inspector could not run: {error}",
                )
            if completed.returncode != 0:
                return _merged_verification(
                    "inspector-runtime-failed",
                    (
                        "The merged-source inspector exited with code "
                        f"{completed.returncode}."
                    ),
                )
            try:
                inspection = json.loads(completed.stdout)
            except json.JSONDecodeError:
                return _merged_verification(
                    "inspector-output-malformed",
                    "The merged-source inspector returned malformed JSON.",
                )
            if not _is_inspection_document(inspection):
                return _merged_verification(
                    "inspector-output-malformed",
                    "The merged-source inspector returned an invalid document.",
                )
            try:
                validated_source = validate_quarantine_post_inspection(
                    request,
                    inspection,
                )
            except ValueError as error:
                return _merged_verification(
                    "merged-source-mismatch",
                    f"The merged quarantine source does not match: {error}",
                )
            if (
                validated_source["completedTests"]
                != validated_mutation["completedTests"]
            ):
                return _merged_verification(
                    "merged-source-mismatch",
                    "The merged quarantine test set does not match the mutation.",
                )
            return MergedQuarantineSourceVerification(
                verified=True,
                code="verified",
                reason="The exact quarantine attributes exist at the merge commit.",
            )
    except OSError as error:
        return _merged_verification(
            "source-fetch-failed",
            f"The merged source could not be materialized: {error}",
        )


def _is_inspection_document(value: object) -> bool:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"schemaVersion", "tests"}
        or value.get("schemaVersion") != 1
        or not isinstance(value.get("tests"), list)
    ):
        return False
    return all(
        isinstance(test, Mapping)
        and set(test) == {"testName", "status", "matches"}
        and isinstance(test.get("testName"), str)
        and bool(test["testName"])
        and isinstance(test.get("status"), str)
        and isinstance(test.get("matches"), list)
        for test in value["tests"]
    )


def _merged_verification(
    code: str,
    reason: str,
) -> MergedQuarantineSourceVerification:
    return MergedQuarantineSourceVerification(
        verified=False,
        code=code,
        reason=reason,
    )
