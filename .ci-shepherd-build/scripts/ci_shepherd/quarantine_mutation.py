from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
from typing import Any, Mapping

from .quarantine import (
    _source_revision,
    _source_tree_digest,
    _validate_source_inspection_match,
)
from .models import stable_json

_MUTATION_RESULT_KEYS = frozenset(
    {
        "schemaVersion",
        "sourceRevision",
        "sourceTreeDigest",
        "completedTests",
        "changedFiles",
        "affectedProjects",
        "diffDigest",
    }
)
_COMMIT_VALIDATION_KEYS = frozenset(
    {
        "schemaVersion",
        "commitSha",
        "changedFiles",
        "diffDigest",
    }
)


def execute_quarantine_mutation(
    request: Mapping[str, Any],
    checkout: Path,
    *,
    timeout_seconds: int = 300,
) -> dict[str, object]:
    checkout = checkout.expanduser().resolve(strict=True)
    tests_root = checkout / "tests"
    tool_project = checkout / "tools" / "QuarantineTools"
    if not tests_root.is_dir() or not tool_project.is_dir():
        raise ValueError("Checkout does not contain QuarantineTools and tests.")

    expected_revision = _require_string(request, "sourceRevision")
    expected_tree_digest = _require_string(request, "sourceTreeDigest")
    if _source_revision(checkout) != expected_revision:
        raise ValueError("Quarantine checkout source revision does not match.")
    if _source_tree_digest(checkout) != expected_tree_digest:
        raise ValueError("Quarantine checkout source tree digest does not match.")
    _require_clean_checkout(checkout)

    tests = request.get("tests")
    if not isinstance(tests, list) or not tests:
        raise ValueError("Quarantine mutation request must contain tests.")
    environment = {
        **os.environ,
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        "DOTNET_CLI_UI_LANGUAGE": "en-US",
        "DOTNET_NOLOGO": "1",
        "MSBUILDTERMINALLOGGER": "false",
    }
    for test in tests:
        if not isinstance(test, Mapping):
            raise ValueError("Quarantine mutation tests must contain objects.")
        test_name = _require_string(test, "testName")
        issue_url = _require_string(test, "issueUrl")
        _run_checked(
            [
                "dotnet",
                "run",
                "--project",
                str(tool_project),
                "--no-restore",
                "--verbosity",
                "quiet",
                "--",
                "--quarantine",
                "--root",
                str(tests_root),
                "--url",
                issue_url,
                test_name,
            ],
            checkout,
            environment,
            timeout_seconds,
            f"QuarantineTools mutation failed for {test_name}",
        )

    test_names = [_require_string(test, "testName") for test in tests]
    inspection_result = _run_checked(
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
        checkout,
        environment,
        timeout_seconds,
        "Post-mutation quarantine inspection failed",
    )
    try:
        inspection = json.loads(inspection_result.stdout)
    except json.JSONDecodeError as error:
        raise ValueError(
            "Post-mutation quarantine inspection returned invalid JSON."
        ) from error
    if not isinstance(inspection, Mapping):
        raise ValueError(
            "Post-mutation quarantine inspection must return an object."
        )
    validated = validate_quarantine_post_inspection(request, inspection)

    expected_changed_files = sorted(
        f"tests/{file_name}" for file_name in validated["changedFiles"]
    )
    changed_files = _changed_checkout_files(checkout)
    if changed_files != expected_changed_files:
        raise ValueError(
            "Quarantine mutation changed unexpected files: "
            f"expected {expected_changed_files!r}, got {changed_files!r}."
        )

    tests_by_project: dict[Path, list[str]] = defaultdict(list)
    for test in tests:
        source_location = test.get("sourceLocation")
        if not isinstance(source_location, Mapping):
            raise ValueError("Quarantine mutation sourceLocation is invalid.")
        source_file = _require_string(source_location, "file")
        project = _find_test_project(tests_root, tests_root / source_file)
        tests_by_project[project].append(_require_string(test, "testName"))

    for project, project_tests in sorted(
        tests_by_project.items(),
        key=lambda item: str(item[0]),
    ):
        _run_checked(
            [
                "dotnet",
                "build",
                str(project),
                "--no-restore",
                "--verbosity",
                "quiet",
            ],
            checkout,
            environment,
            timeout_seconds,
            f"Build failed for {project.relative_to(checkout)}",
        )
        _validate_test_discovery(
            checkout,
            project,
            sorted(project_tests),
            environment,
            timeout_seconds,
        )

    diff = _canonical_checkout_diff(checkout, expected_changed_files)
    return {
        **validated,
        "changedFiles": expected_changed_files,
        "affectedProjects": sorted(
            str(project.relative_to(checkout)).replace(os.sep, "/")
            for project in tests_by_project
        ),
        "diffDigest": f"sha256:{hashlib.sha256(diff).hexdigest()}",
    }


def validate_quarantine_mutation_result(
    request: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, object]:
    if set(result) != _MUTATION_RESULT_KEYS or result.get("schemaVersion") != 1:
        raise ValueError("Quarantine mutation result has an invalid schema.")
    for field in ("sourceRevision", "sourceTreeDigest"):
        if result.get(field) != request.get(field):
            raise ValueError(f"Quarantine mutation result {field} does not match.")
    requested_names = sorted(
        _require_string(test, "testName")
        for test in request.get("tests", [])
        if isinstance(test, Mapping)
    )
    completed_names = result.get("completedTests")
    if completed_names != requested_names:
        raise ValueError(
            "Quarantine mutation result must complete every requested test."
        )
    expected_changed_files = sorted(
        "tests/" + _require_string(test["sourceLocation"], "file")
        for test in request.get("tests", [])
        if isinstance(test, Mapping)
        and isinstance(test.get("sourceLocation"), Mapping)
    )
    changed_files = result.get("changedFiles")
    if changed_files != expected_changed_files:
        raise ValueError(
            "Quarantine mutation result changedFiles do not match the request."
        )
    affected_projects = result.get("affectedProjects")
    if (
        not isinstance(affected_projects, list)
        or affected_projects != sorted(set(affected_projects))
        or not affected_projects
        or not all(
            isinstance(project, str)
            and project.startswith("tests/")
            and project.endswith(".csproj")
            for project in affected_projects
        )
    ):
        raise ValueError(
            "Quarantine mutation result affectedProjects are invalid."
        )
    diff_digest = result.get("diffDigest")
    if (
        not isinstance(diff_digest, str)
        or len(diff_digest) != 71
        or not diff_digest.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in diff_digest[7:])
    ):
        raise ValueError("Quarantine mutation result diffDigest is invalid.")
    return dict(result)


def revalidate_quarantine_checkout_diff(
    request: Mapping[str, Any],
    result: Mapping[str, Any],
    checkout: Path,
) -> dict[str, object]:
    validated = validate_quarantine_mutation_result(request, result)
    checkout = checkout.expanduser().resolve(strict=True)
    changed_files = _changed_checkout_files(checkout)
    if changed_files != validated["changedFiles"]:
        raise ValueError("Quarantine checkout changed after validation.")
    diff = _canonical_checkout_diff(checkout, changed_files)
    actual_digest = f"sha256:{hashlib.sha256(diff).hexdigest()}"
    if actual_digest != validated["diffDigest"]:
        raise ValueError("Quarantine diff digest changed after validation.")
    return validated


def create_quarantine_commit_validation(
    request: Mapping[str, Any],
    result: Mapping[str, Any],
    checkout: Path,
    commit_sha: str = "HEAD",
) -> dict[str, object]:
    validated = validate_quarantine_mutation_result(request, result)
    checkout = checkout.expanduser().resolve(strict=True)
    resolved_commit = _resolve_commit(checkout, commit_sha)
    parent = _single_commit_parent(checkout, resolved_commit)
    changed_files = _changed_commit_files(
        checkout,
        parent,
        resolved_commit,
    )
    if changed_files != validated["changedFiles"]:
        raise ValueError(
            "Quarantine commit files do not match mutation validation."
        )
    diff = _canonical_commit_diff(
        checkout,
        parent,
        resolved_commit,
        changed_files,
    )
    actual_digest = f"sha256:{hashlib.sha256(diff).hexdigest()}"
    if actual_digest != validated["diffDigest"]:
        raise ValueError(
            "Quarantine commit diff does not match mutation validation."
        )
    return {
        "schemaVersion": 1,
        "commitSha": resolved_commit,
        "changedFiles": changed_files,
        "diffDigest": actual_digest,
    }


def validate_quarantine_commit_validation(
    mutation_result: Mapping[str, Any],
    commit_validation: Mapping[str, Any],
) -> dict[str, object]:
    if (
        set(commit_validation) != _COMMIT_VALIDATION_KEYS
        or commit_validation.get("schemaVersion") != 1
        or commit_validation.get("changedFiles")
        != mutation_result.get("changedFiles")
        or commit_validation.get("diffDigest")
        != mutation_result.get("diffDigest")
    ):
        raise ValueError("Quarantine commit validation does not match mutation.")
    commit_sha = commit_validation.get("commitSha")
    if (
        not isinstance(commit_sha, str)
        or len(commit_sha) != 40
        or any(character not in "0123456789abcdef" for character in commit_sha)
    ):
        raise ValueError("Quarantine commit validation has an invalid commit SHA.")
    return dict(commit_validation)


def write_quarantine_validation(
    path: Path,
    validation: Mapping[str, Any],
) -> None:
    if path.is_symlink():
        raise ValueError("Quarantine validation output must not be a symlink.")
    path = path.expanduser().resolve()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(
        f".{path.name}.{secrets.token_hex(8)}.tmp"
    )
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(stable_json(dict(validation)))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _run_checked(
    command: list[str],
    checkout: Path,
    environment: Mapping[str, str],
    timeout_seconds: int,
    description: str,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=checkout,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(description) from error
    if result.returncode != 0:
        detail = result.stderr.strip()
        suffix = f": {detail}" if detail else ""
        raise ValueError(f"{description}{suffix}")
    return result


def _find_test_project(tests_root: Path, source_file: Path) -> Path:
    if (
        not source_file.is_file()
        or source_file.is_symlink()
        or tests_root not in source_file.parents
    ):
        raise ValueError(f"Invalid quarantine source file {source_file}.")
    directory = source_file.parent
    while directory != tests_root.parent:
        projects = sorted(directory.glob("*.csproj"))
        if len(projects) == 1:
            return projects[0]
        if len(projects) > 1:
            raise ValueError(
                f"Multiple test projects contain {source_file}."
            )
        if directory == tests_root:
            break
        directory = directory.parent
    raise ValueError(f"No test project contains {source_file}.")


def _validate_test_discovery(
    checkout: Path,
    project: Path,
    test_names: list[str],
    environment: Mapping[str, str],
    timeout_seconds: int,
) -> None:
    base_command = [
        "dotnet",
        "test",
        "--project",
        str(project),
        "--no-build",
        "--no-launch-profile",
        "--",
        "--list-tests",
        *[
            argument
            for test_name in test_names
            for argument in ("--filter-method", test_name)
        ],
    ]
    unfiltered = _run_checked(
        base_command,
        checkout,
        environment,
        timeout_seconds,
        f"Unfiltered test discovery failed for {project.relative_to(checkout)}",
    )
    for test_name in test_names:
        if not _discovery_contains(unfiltered.stdout, test_name):
            raise ValueError(
                f"{test_name} is missing from unfiltered discovery."
            )

    filtered = _run_checked(
        [
            *base_command,
            "--filter-not-trait",
            "quarantined=true",
            "--filter-not-trait",
            "outerloop=true",
        ],
        checkout,
        environment,
        timeout_seconds,
        f"Filtered test discovery failed for {project.relative_to(checkout)}",
    )
    for test_name in test_names:
        if _discovery_contains(filtered.stdout, test_name):
            raise ValueError(
                f"{test_name} remains in quarantine-filtered discovery."
            )


def _discovery_contains(output: str, test_name: str) -> bool:
    return any(
        line == test_name or line.startswith(f"{test_name}(")
        for line in (raw_line.strip() for raw_line in output.splitlines())
    )


def _require_clean_checkout(checkout: Path) -> None:
    result = subprocess.run(
        [
            "git",
            "--no-pager",
            "-C",
            str(checkout),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0 or result.stdout:
        raise ValueError(
            "Quarantine mutation requires a clean dedicated checkout."
        )


def _changed_checkout_files(checkout: Path) -> list[str]:
    result = subprocess.run(
        [
            "git",
            "--no-pager",
            "-c",
            "diff.renames=false",
            "-C",
            str(checkout),
            "diff",
            "--name-only",
            "--no-ext-diff",
            "HEAD",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    untracked = subprocess.run(
        [
            "git",
            "--no-pager",
            "-C",
            str(checkout),
            "ls-files",
            "--others",
            "--exclude-standard",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0 or untracked.returncode != 0:
        raise ValueError("Unable to enumerate quarantine mutation changes.")
    return sorted(
        {
            *result.stdout.splitlines(),
            *untracked.stdout.splitlines(),
        }
    )


def _canonical_checkout_diff(
    checkout: Path,
    changed_files: list[str],
) -> bytes:
    result = subprocess.run(
        [
            "git",
            "--no-pager",
            "-c",
            "core.abbrev=40",
            "-c",
            "diff.noprefix=false",
            "-c",
            "diff.renames=false",
            "-C",
            str(checkout),
            "diff",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-textconv",
            "HEAD",
            "--",
            *changed_files,
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0 or not result.stdout:
        raise ValueError("Unable to produce the validated quarantine diff.")
    return result.stdout


def _resolve_commit(checkout: Path, commit_sha: str) -> str:
    result = subprocess.run(
        [
            "git",
            "--no-pager",
            "-C",
            str(checkout),
            "rev-parse",
            "--verify",
            f"{commit_sha}^{{commit}}",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    resolved = result.stdout.strip().lower()
    if (
        result.returncode != 0
        or len(resolved) != 40
        or any(character not in "0123456789abcdef" for character in resolved)
    ):
        raise ValueError("Unable to resolve quarantine commit.")
    return resolved


def _single_commit_parent(checkout: Path, commit_sha: str) -> str:
    result = subprocess.run(
        [
            "git",
            "--no-pager",
            "-C",
            str(checkout),
            "rev-list",
            "--parents",
            "-n",
            "1",
            commit_sha,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    parts = result.stdout.split()
    if result.returncode != 0 or len(parts) != 2:
        raise ValueError("Quarantine change must be one non-merge commit.")
    return parts[1]


def _changed_commit_files(
    checkout: Path,
    parent: str,
    commit_sha: str,
) -> list[str]:
    result = subprocess.run(
        [
            "git",
            "--no-pager",
            "-c",
            "diff.renames=false",
            "-C",
            str(checkout),
            "diff",
            "--name-only",
            "--no-ext-diff",
            parent,
            commit_sha,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise ValueError("Unable to enumerate quarantine commit files.")
    return sorted(result.stdout.splitlines())


def _canonical_commit_diff(
    checkout: Path,
    parent: str,
    commit_sha: str,
    changed_files: list[str],
) -> bytes:
    result = subprocess.run(
        [
            "git",
            "--no-pager",
            "-c",
            "core.abbrev=40",
            "-c",
            "diff.noprefix=false",
            "-c",
            "diff.renames=false",
            "-C",
            str(checkout),
            "diff",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-textconv",
            parent,
            commit_sha,
            "--",
            *changed_files,
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0 or not result.stdout:
        raise ValueError("Unable to produce the quarantine commit diff.")
    return result.stdout


def validate_quarantine_post_inspection(
    request: Mapping[str, Any],
    inspection: Mapping[str, Any],
) -> dict[str, object]:
    tests = request.get("tests")
    if not isinstance(tests, list) or not tests:
        raise ValueError("Quarantine mutation request must contain tests.")
    if set(inspection) != {"schemaVersion", "tests"}:
        raise ValueError("Post-mutation inspection has unexpected or missing fields.")
    if inspection.get("schemaVersion") != 1:
        raise ValueError("Unsupported post-mutation inspection schema.")
    raw_results = inspection.get("tests")
    if not isinstance(raw_results, list):
        raise ValueError("Post-mutation inspection tests must be a list.")

    results_by_name: dict[str, Mapping[str, Any]] = {}
    for result in raw_results:
        if not isinstance(result, Mapping) or set(result) != {
            "testName",
            "status",
            "matches",
        }:
            raise ValueError("Post-mutation inspection test is invalid.")
        test_name = result.get("testName")
        if not isinstance(test_name, str) or not test_name:
            raise ValueError("Post-mutation inspection testName must be nonempty.")
        if test_name in results_by_name:
            raise ValueError(
                f"Post-mutation inspection contains duplicate {test_name}."
            )
        results_by_name[test_name] = result

    tests_by_name: dict[str, Mapping[str, Any]] = {}
    expected_additions_by_file: dict[str, list[dict[str, str]]] = defaultdict(list)
    baseline_by_file: dict[str, list[dict[str, str | None]]] = {}
    semantic_digest_by_file: dict[str, str] = {}
    for test in tests:
        if not isinstance(test, Mapping):
            raise ValueError("Quarantine mutation tests must contain objects.")
        test_name = _require_string(test, "testName")
        issue_url = _require_string(test, "issueUrl")
        if test_name in tests_by_name:
            raise ValueError("Quarantine mutation test names must be unique.")
        location = test.get("sourceLocation")
        validation = test.get("sourceValidation")
        if (
            not isinstance(location, Mapping)
            or set(location) != {"file", "line"}
            or not isinstance(validation, Mapping)
            or set(validation) != {"fileSemanticDigest", "fileQuarantines"}
        ):
            raise ValueError(
                f"Quarantine mutation source baseline for {test_name} is invalid."
            )
        source_file = _require_string(location, "file")
        semantic_digest = _require_string(validation, "fileSemanticDigest")
        baseline = validation.get("fileQuarantines")
        if not isinstance(baseline, list) or not all(
            isinstance(item, Mapping)
            and set(item) == {"testName", "issueUrl"}
            and isinstance(item.get("testName"), str)
            and item["testName"]
            and (
                item.get("issueUrl") is None
                or isinstance(item.get("issueUrl"), str)
                and item["issueUrl"]
            )
            for item in baseline
        ):
            raise ValueError(
                f"Quarantine mutation inventory for {test_name} is invalid."
            )
        normalized_baseline = [dict(item) for item in baseline]
        previous_baseline = baseline_by_file.setdefault(
            source_file,
            normalized_baseline,
        )
        previous_digest = semantic_digest_by_file.setdefault(
            source_file,
            semantic_digest,
        )
        if (
            previous_baseline != normalized_baseline
            or previous_digest != semantic_digest
        ):
            raise ValueError(
                f"Quarantine mutation baselines disagree for {source_file}."
            )
        expected_additions_by_file[source_file].append(
            {"testName": test_name, "issueUrl": issue_url}
        )
        tests_by_name[test_name] = test

    if set(results_by_name) != set(tests_by_name):
        raise ValueError(
            "Post-mutation inspection test names must exactly match the request."
        )

    for test_name, test in tests_by_name.items():
        result = results_by_name[test_name]
        matches = result.get("matches")
        if result.get("status") != "resolved" or not isinstance(matches, list):
            raise ValueError(f"Post-mutation target {test_name} is not resolved.")
        if len(matches) != 1:
            raise ValueError(
                f"Post-mutation target {test_name} must resolve exactly once."
            )
        match = _validate_source_inspection_match(test_name, matches[0])
        source_file = str(test["sourceLocation"]["file"])
        if match["file"] != source_file:
            raise ValueError(
                f"Post-mutation target {test_name} moved to another file."
            )
        expected_attribute = [
            {
                "name": "QuarantinedTest",
                "issueUrl": test["issueUrl"],
            }
        ]
        if match["quarantineAttributes"] != expected_attribute:
            raise ValueError(
                f"Post-mutation target {test_name} has the wrong quarantine attribute."
            )
        if match["activeIssueAttributes"]:
            raise ValueError(
                f"Post-mutation target {test_name} also carries ActiveIssue."
            )
        if (
            match["fileSemanticDigest"]
            != test["sourceValidation"]["fileSemanticDigest"]
        ):
            raise ValueError(
                f"Post-mutation source semantics changed in {source_file}."
            )
        expected_inventory = sorted(
            [
                *baseline_by_file[source_file],
                *expected_additions_by_file[source_file],
            ],
            key=lambda item: (item["testName"], item["issueUrl"] or ""),
        )
        if match["fileQuarantines"] != expected_inventory:
            raise ValueError(
                f"Post-mutation quarantine inventory changed unexpectedly in {source_file}."
            )

    return {
        "schemaVersion": 1,
        "sourceRevision": _require_string(request, "sourceRevision"),
        "sourceTreeDigest": _require_string(request, "sourceTreeDigest"),
        "completedTests": sorted(tests_by_name),
        "changedFiles": sorted(baseline_by_file),
    }


def _require_string(document: Mapping[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be nonempty.")
    return value
