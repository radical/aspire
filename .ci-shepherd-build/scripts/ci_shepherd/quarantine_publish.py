from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import re
import secrets
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .jsonl import append_jsonl_rows, exclusive_jsonl_lock
from .quarantine import read_quarantine_session_events
from .quarantine_authorization import _deny_production
from .quarantine_mutation import (
    _require_clean_checkout,
    create_quarantine_commit_validation,
    validate_quarantine_commit_validation,
    validate_quarantine_mutation_result,
)
from .repository_policy import load_embedded_repository_policy


_HTTPS_REMOTE_RE = re.compile(
    r"^https://github\.com/(?P<repository>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
    r"(?:\.git)?/?$",
    re.IGNORECASE,
)
_SSH_REMOTE_RE = re.compile(
    r"^(?:git@github\.com:|ssh://git@github\.com/)"
    r"(?P<repository>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:\.git)?/?$",
    re.IGNORECASE,
)


def publish_quarantine_pull_request(
    *,
    request: Mapping[str, Any],
    mutation_result: Mapping[str, Any],
    commit_validation: Mapping[str, Any],
    checkout: Path,
    state_directory: Path,
    session_id: str,
    body_file: Path,
    audit_path: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, object]:
    repository = _require_string(request, "repository")
    _deny_production(repository)
    validated_mutation = validate_quarantine_mutation_result(
        request,
        mutation_result,
    )
    validated_commit = validate_quarantine_commit_validation(
        validated_mutation,
        commit_validation,
    )
    _require_started_session(
        request,
        state_directory,
        session_id=session_id,
    )
    policy = load_embedded_repository_policy(
        request.get("repositoryPolicy"),
        repository,
    )
    allowed_heads = policy.quarantine_pull_request.allowed_head_repositories
    if len(allowed_heads) != 1:
        raise ValueError(
            "Quarantine publication requires exactly one allowed head repository."
        )
    head_repository = next(iter(allowed_heads))
    approved_body = _validate_body(request, body_file)
    checkout = checkout.expanduser().resolve(strict=True)
    _ensure_commit_unchanged(
        request,
        validated_mutation,
        validated_commit,
        checkout,
    )
    _require_clean_checkout(checkout)
    remote = _resolve_allowed_remote(
        checkout,
        head_repository,
        runner=runner,
    )
    if remote is None:
        raise ValueError(
            "No Git remote matches the quarantine policy's allowed head repository."
        )

    batch_id = _require_string(request, "batchId")
    branch = _branch_for_batch(batch_id)
    commit_sha = _require_string(validated_commit, "commitSha")
    base_ref = policy.quarantine_pull_request.base_ref
    head_owner = head_repository.split("/", 1)[0]
    head_ref = f"{head_owner}:{branch}"

    with (
        _approved_body_file(approved_body, audit_path) as approved_body_file,
        exclusive_jsonl_lock(audit_path),
    ):
        remote_sha = _read_remote_branch_sha(
            checkout,
            remote,
            branch,
            runner=runner,
        )
        if remote_sha is not None and remote_sha != commit_sha:
            raise ValueError(
                "The derived quarantine branch already points at another commit."
            )

        existing = _find_existing_pull_request(
            repository=repository,
            head_branch=branch,
            runner=runner,
        )
        if existing is not None:
            _validate_pull_request_summary(
                existing,
                repository=repository,
                head_repository=head_repository,
                base_ref=base_ref,
                commit_sha=commit_sha,
            )
            return _worker_result(
                request=request,
                validated_commit=validated_commit,
                session_id=session_id,
                pull_request_url=_require_string(existing, "url"),
            )

        _ensure_commit_unchanged(
            request,
            validated_mutation,
            validated_commit,
            checkout,
        )
        _require_clean_checkout(checkout)
        identity = {
            "schemaVersion": 1,
            "batchId": batch_id,
            "sessionId": session_id,
            "repository": repository,
            "headRepository": head_repository,
            "branch": branch,
            "commitSha": commit_sha,
        }
        if remote_sha is None:
            operation_id = _record_intent(
                audit_path,
                identity,
                operation="push-branch",
            )
            try:
                _run_text(
                    [
                        "git",
                        "--no-pager",
                        "-C",
                        str(checkout),
                        "push",
                        remote,
                        f"{commit_sha}:refs/heads/{branch}",
                    ],
                    runner=runner,
                    description="Unable to push the quarantine branch.",
                    timeout_seconds=300,
                )
            except Exception as error:
                _record_outcome(
                    audit_path,
                    identity,
                    operation="push-branch",
                    operation_id=operation_id,
                    result="failed",
                    error=str(error),
                )
                raise
            _record_outcome(
                audit_path,
                identity,
                operation="push-branch",
                operation_id=operation_id,
                result="pushed",
            )

        published_sha = _read_remote_branch_sha(
            checkout,
            remote,
            branch,
            runner=runner,
        )
        if published_sha != commit_sha:
            raise ValueError(
                "The derived quarantine branch changed before pull request creation."
            )
        title = _pull_request_title(request)
        operation_id = _record_intent(
            audit_path,
            identity,
            operation="create-pull-request",
        )
        try:
            output = _run_text(
                [
                    "gh",
                    "pr",
                    "create",
                    "--repo",
                    repository,
                    "--base",
                    base_ref,
                    "--head",
                    head_ref,
                    "--draft",
                    "--title",
                    title,
                    "--body-file",
                    str(approved_body_file),
                ],
                runner=runner,
                description="Unable to create the quarantine pull request.",
                timeout_seconds=120,
            )
            pull_request_url = _parse_pull_request_url(output, repository)
        except Exception as error:
            _record_outcome(
                audit_path,
                identity,
                operation="create-pull-request",
                operation_id=operation_id,
                result="failed",
                error=str(error),
            )
            raise
        _record_outcome(
            audit_path,
            identity,
            operation="create-pull-request",
            operation_id=operation_id,
            result="created",
            pull_request_url=pull_request_url,
        )

        created = _read_pull_request(
            pull_request_url,
            repository=repository,
            runner=runner,
        )
        _validate_pull_request_summary(
            created,
            repository=repository,
            head_repository=head_repository,
            base_ref=base_ref,
            commit_sha=commit_sha,
        )
        return _worker_result(
            request=request,
            validated_commit=validated_commit,
            session_id=session_id,
            pull_request_url=pull_request_url,
        )


def _ensure_commit_unchanged(
    request: Mapping[str, Any],
    mutation_result: Mapping[str, Any],
    expected: Mapping[str, Any],
    checkout: Path,
) -> None:
    actual = create_quarantine_commit_validation(
        request,
        mutation_result,
        checkout,
    )
    if actual != expected:
        raise ValueError("Quarantine commit changed after validation.")


def _require_started_session(
    request: Mapping[str, Any],
    state_directory: Path,
    *,
    session_id: str,
) -> None:
    batch_id = _require_string(request, "batchId")
    events = read_quarantine_session_events(state_directory)
    latest = next(
        (
            event
            for event in reversed(events)
            if event.get("batchId") == batch_id
        ),
        None,
    )
    if (
        latest is None
        or latest.get("status") != "started"
        or latest.get("sessionId") != session_id
        or not isinstance(latest.get("authorizationGrantId"), str)
        or any(
            latest.get(field) != request.get(field)
            for field in (
                "schemaVersion",
                "repository",
                "snapshotId",
                "batchId",
                "sourceRevision",
                "sourceTreeDigest",
                "inspectorTreeDigest",
                "repositoryPolicyDigest",
                "repositoryPolicy",
                "tests",
            )
        )
    ):
        raise ValueError(
            "Quarantine publication requires the exact active started session."
        )


def _validate_body(
    request: Mapping[str, Any],
    body_file: Path,
) -> str:
    if body_file.is_symlink():
        raise ValueError("Quarantine pull request body must not be a symlink.")
    body_file = body_file.expanduser().resolve(strict=True)
    if not body_file.is_file() or body_file.stat().st_size > 64 * 1024:
        raise ValueError("Quarantine pull request body is invalid.")
    body = body_file.read_text(encoding="utf-8")
    if len(body.encode("utf-8")) > 64 * 1024:
        raise ValueError("Quarantine pull request body is invalid.")
    if not body.startswith("[automated] "):
        raise ValueError(
            "Quarantine pull request body must begin with [automated]."
        )
    issue_numbers = {
        test.get("issueNumber")
        for test in request.get("tests", [])
        if isinstance(test, Mapping)
    }
    addressed_issues = {
        int(number)
        for number in re.findall(
            r"^Addresses #([1-9][0-9]*)[ \t]*$",
            body,
            flags=re.MULTILINE,
        )
    }
    if (
        not issue_numbers
        or any(
            not (
                isinstance(number, int)
                and not isinstance(number, bool)
                and number > 0
            )
            for number in issue_numbers
        )
        or addressed_issues != issue_numbers
    ):
        raise ValueError(
            "Quarantine pull request body must address exactly every source issue."
        )
    return body


@contextmanager
def _approved_body_file(body: str, audit_path: Path) -> Iterator[Path]:
    parent = audit_path.expanduser().resolve().parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(parent, 0o700)
    with tempfile.TemporaryDirectory(
        prefix=".quarantine-pr-body-",
        dir=parent,
    ) as temporary_directory:
        directory = Path(temporary_directory)
        os.chmod(directory, 0o700)
        path = directory / "body.md"
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        yield path


def _resolve_allowed_remote(
    checkout: Path,
    repository: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> str | None:
    remotes = _run_text(
        ["git", "--no-pager", "-C", str(checkout), "remote"],
        runner=runner,
        description="Unable to enumerate Git remotes.",
    ).splitlines()
    matches = []
    for remote in sorted(set(remotes)):
        if not remote:
            continue
        url = _run_text(
            [
                "git",
                "--no-pager",
                "-C",
                str(checkout),
                "remote",
                "get-url",
                remote,
            ],
            runner=runner,
            description=f"Unable to resolve Git remote {remote}.",
        ).strip()
        resolved = _github_repository_from_remote_url(url)
        if resolved is not None and resolved.casefold() == repository.casefold():
            matches.append(remote)
    if len(matches) > 1:
        raise ValueError(
            "Multiple Git remotes match the allowed head repository."
        )
    return matches[0] if matches else None


def _github_repository_from_remote_url(url: str) -> str | None:
    for pattern in (_HTTPS_REMOTE_RE, _SSH_REMOTE_RE):
        match = pattern.fullmatch(url)
        if match is not None:
            return match.group("repository").removesuffix(".git")
    return None


def _branch_for_batch(batch_id: str) -> str:
    match = re.fullmatch(
        r"quarantine:fnv1a64:(?P<digest>[0-9a-f]{16})",
        batch_id,
    )
    if match is None:
        raise ValueError("Quarantine batch ID cannot produce a safe branch.")
    return f"ci-shepherd/quarantine-{match.group('digest')}"


def _read_remote_branch_sha(
    checkout: Path,
    remote: str,
    branch: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> str | None:
    ref = f"refs/heads/{branch}"
    output = _run_text(
        [
            "git",
            "--no-pager",
            "-C",
            str(checkout),
            "ls-remote",
            "--heads",
            remote,
            ref,
        ],
        runner=runner,
        description="Unable to inspect the derived quarantine branch.",
    )
    if not output.strip():
        return None
    lines = output.splitlines()
    if len(lines) != 1:
        raise ValueError("The derived quarantine branch is ambiguous.")
    fields = lines[0].split()
    if (
        len(fields) != 2
        or fields[1] != ref
        or not re.fullmatch(r"[0-9a-f]{40}", fields[0])
    ):
        raise ValueError("The derived quarantine branch response is malformed.")
    return fields[0]


def _find_existing_pull_request(
    *,
    repository: str,
    head_branch: str,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> dict[str, Any] | None:
    output = _run_text(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repository,
            "--head",
            head_branch,
            "--state",
            "open",
            "--limit",
            "2",
            "--json",
            "url,headRefOid,isDraft,baseRefName,headRepository",
        ],
        runner=runner,
        description="Unable to inspect existing quarantine pull requests.",
        timeout_seconds=120,
    )
    try:
        value = json.loads(output)
    except json.JSONDecodeError as error:
        raise ValueError(
            "Existing quarantine pull request response is malformed."
        ) from error
    if not isinstance(value, list) or any(
        not isinstance(item, dict) for item in value
    ):
        raise ValueError(
            "Existing quarantine pull request response is malformed."
        )
    if len(value) > 1:
        raise ValueError(
            "Multiple open pull requests use the derived quarantine branch."
        )
    return value[0] if value else None


def _read_pull_request(
    pull_request_url: str,
    *,
    repository: str,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> dict[str, Any]:
    output = _run_text(
        [
            "gh",
            "pr",
            "view",
            pull_request_url,
            "--repo",
            repository,
            "--json",
            "url,headRefOid,isDraft,baseRefName,headRepository",
        ],
        runner=runner,
        description="Unable to verify the created quarantine pull request.",
        timeout_seconds=120,
    )
    try:
        value = json.loads(output)
    except json.JSONDecodeError as error:
        raise ValueError(
            "Created quarantine pull request response is malformed."
        ) from error
    if not isinstance(value, dict):
        raise ValueError(
            "Created quarantine pull request response is malformed."
        )
    return value


def _validate_pull_request_summary(
    summary: Mapping[str, Any],
    *,
    repository: str,
    head_repository: str,
    base_ref: str,
    commit_sha: str,
) -> None:
    head = summary.get("headRepository")
    actual_head = (
        head.get("nameWithOwner")
        if isinstance(head, Mapping)
        else None
    )
    url = _require_string(summary, "url")
    _parse_pull_request_url(url, repository)
    if (
        summary.get("headRefOid") != commit_sha
        or summary.get("isDraft") is not True
        or summary.get("baseRefName") != base_ref
        or not isinstance(actual_head, str)
        or actual_head.casefold() != head_repository.casefold()
    ):
        raise ValueError(
            "Quarantine pull request does not match the publication policy."
        )


def _parse_pull_request_url(output: str, repository: str) -> str:
    owner, name = map(re.escape, repository.split("/", 1))
    matches = re.findall(
        rf"https://github\.com/{owner}/{name}/pull/[1-9][0-9]*",
        output,
        flags=re.IGNORECASE,
    )
    if len(matches) != 1:
        raise ValueError(
            "Quarantine pull request creation returned no unique URL."
        )
    return matches[0]


def _pull_request_title(request: Mapping[str, Any]) -> str:
    tests = request.get("tests")
    if not isinstance(tests, list) or not tests:
        raise ValueError("Quarantine request has no tests.")
    noun = "test" if len(tests) == 1 else "tests"
    return f"[automated] test: quarantine {len(tests)} flaky {noun}"


def _worker_result(
    *,
    request: Mapping[str, Any],
    validated_commit: Mapping[str, Any],
    session_id: str,
    pull_request_url: str,
) -> dict[str, object]:
    completed_tests = sorted(
        _require_string(test, "testName")
        for test in request.get("tests", [])
        if isinstance(test, Mapping)
    )
    return {
        "schemaVersion": 1,
        "repository": _require_string(request, "repository"),
        "snapshotId": _require_string(request, "snapshotId"),
        "batchId": _require_string(request, "batchId"),
        "sessionId": session_id,
        "outcome": "pull-request-open",
        "completedTests": completed_tests,
        "blockedTargets": [],
        "pullRequest": {
            "url": pull_request_url,
            "headSha": _require_string(validated_commit, "commitSha"),
        },
    }


def _record_intent(
    audit_path: Path,
    identity: Mapping[str, Any],
    *,
    operation: str,
) -> str:
    operation_id = f"quarantine-operation:{secrets.token_hex(16)}"
    append_jsonl_rows(
        audit_path,
        [
            {
                **identity,
                "operationId": operation_id,
                "operation": operation,
                "phase": "intent",
                "recordedAt": _utc_now(),
            }
        ],
    )
    return operation_id


def _record_outcome(
    audit_path: Path,
    identity: Mapping[str, Any],
    *,
    operation: str,
    operation_id: str,
    result: str,
    error: str | None = None,
    pull_request_url: str | None = None,
) -> None:
    row = {
        **identity,
        "operationId": operation_id,
        "operation": operation,
        "phase": "outcome",
        "result": result,
        "recordedAt": _utc_now(),
    }
    if error is not None:
        row["error"] = error
    if pull_request_url is not None:
        row["pullRequestUrl"] = pull_request_url
    append_jsonl_rows(audit_path, [row])


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _run_text(
    command: list[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    description: str,
    timeout_seconds: int = 60,
) -> str:
    completed = runner(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        raise ValueError(f"{description} {completed.stderr.strip()}")
    return completed.stdout


def _require_string(mapping: Mapping[str, Any], field: str) -> str:
    value = mapping.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be nonempty.")
    return value
