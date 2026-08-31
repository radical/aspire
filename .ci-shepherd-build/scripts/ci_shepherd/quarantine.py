from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping

from .jsonl import append_jsonl_rows, exclusive_jsonl_lock, read_jsonl_rows
from .timeutils import parse_aware_iso8601


_SESSION_STATUSES = frozenset(
    {"started", "pull-request-open", "completed", "failed"}
)
_PULL_REQUEST_URL_RE = re.compile(
    r"^https://github\.com/(?P<repository>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
    r"/pull/(?P<number>[1-9][0-9]*)$",
    re.IGNORECASE,
)
_TEST_METHOD_NAME_RE = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]*[.+])+[A-Za-z_][A-Za-z0-9_]*$"
)


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    result = 0xCBF29CE484222325
    for byte in encoded:
        result ^= byte
        result = (result * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"fnv1a64:{result:016x}"


def _worker_prompt(repository: str, tests: list[dict[str, object]]) -> str:
    lines = [
        f"Prepare one quarantine pull request for {repository}.",
        "",
        "Use the test-management skill. Work only in the provided worktree and do "
        "not switch branches.",
        "",
        "Do not edit test source by hand or invoke QuarantineTools directly. "
        "Use the authorized deterministic quarantine executor, which invokes "
        "QuarantineTools once for each test with its original issue URL:",
    ]
    for test in tests:
        issue_numbers = test.get("issueNumbers")
        if not isinstance(issue_numbers, list) or not issue_numbers:
            issue_numbers = [test["issueNumber"]]
        addresses = ", ".join(f"Addresses #{number}" for number in issue_numbers)
        source_location = test.get("sourceLocation")
        source_description = (
            f"; the inspected source is "
            f"`{source_location['file']}:{source_location['line']}`"
            if isinstance(source_location, Mapping)
            else ""
        )
        lines.append(
            f"- `{test['testName']}` — use {test['issueUrl']} with QuarantineTools"
            f"{source_description}; "
            f"the PR body must include {addresses}"
        )
    lines.extend(
        [
            "",
            "The executor must revalidate the recorded source revision and source-input digest before changing the clean worktree. It then owns post-mutation Roslyn inspection, exact changed-file validation, affected-project builds, and both filtered and unfiltered MTP discovery.",
            "",
            "Before push, run the deterministic diff and commit validators. Stop on any mismatch; do not repair or broaden the diff manually.",
            "",
            "Do not push or open a pull request yet. Return the validated and blocked target lists, validated diff, exact commands and outcomes, and a draft PR title and body. The PR body must begin with `[automated] ` and use `Addresses #N` for every original issue represented by a changed test. Those issues must remain open until the underlying failures are fixed.",
        ]
    )
    return "\n".join(lines) + "\n"


def _request_for_tests(
    request: Mapping[str, Any],
    tests: list[dict[str, object]],
) -> dict[str, object]:
    repository = request.get("repository")
    if not isinstance(repository, str) or not repository:
        raise ValueError("Quarantine request repository must be nonempty.")
    return {
        **dict(request),
        "batchId": f"quarantine:{_fingerprint(tests)}" if tests else None,
        "tests": tests,
        "workerPrompt": _worker_prompt(repository, tests) if tests else None,
    }


def _issue_labels(issue: Mapping[str, Any]) -> frozenset[str] | None:
    evidence_bundle = issue.get("evidenceBundle")
    if not isinstance(evidence_bundle, list):
        return None
    issue_number = issue.get("issueNumber")
    source_evidence_id = (
        f"issue:{issue_number}"
        if isinstance(issue_number, int) and not isinstance(issue_number, bool)
        else None
    )
    for evidence in evidence_bundle:
        if (
            not isinstance(evidence, Mapping)
            or evidence.get("kind") != "issue-event"
            or evidence.get("id") != source_evidence_id
        ):
            continue
        payload = evidence.get("payload")
        if not isinstance(payload, Mapping):
            return None
        labels = payload.get("labels")
        if not isinstance(labels, list):
            return None
        if not all(isinstance(label, str) and label for label in labels):
            return None
        return frozenset(
            label.casefold()
            for label in labels
        )
    return None


def select_quarantine_session_request(
    request: Mapping[str, Any],
    test_name: str,
) -> dict[str, object]:
    tests = request.get("tests")
    if not isinstance(tests, list):
        raise ValueError("Quarantine request tests must be a list.")
    matches = [
        dict(test)
        for test in tests
        if isinstance(test, Mapping) and test.get("testName") == test_name
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Quarantine request must contain exactly one test named {test_name}."
        )
    return _request_for_tests(request, matches)


def build_quarantine_session_request(
    prepared: Mapping[str, Any],
    judgments: Mapping[str, Any],
    observations: Mapping[str, Any],
) -> dict[str, object]:
    repository = prepared.get("repository")
    snapshot_id = prepared.get("snapshotId")
    if not isinstance(repository, str) or not repository:
        raise ValueError("Prepared repository must be a nonempty string.")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise ValueError("Prepared snapshotId must be a nonempty string.")
    if judgments.get("snapshotId") != snapshot_id:
        raise ValueError("Judgments snapshotId must match prepared snapshotId.")
    repository_policy_digest = prepared.get("repositoryPolicyDigest")
    repository_policy = prepared.get("repositoryPolicy")
    repository_policy_available = (
        isinstance(repository_policy, Mapping)
        and isinstance(repository_policy_digest, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", repository_policy_digest)
        is not None
        and repository_policy.get("digest") == repository_policy_digest
    )

    prepared_issues = {
        issue["issueNumber"]: issue
        for issue in prepared.get("issues", [])
        if isinstance(issue, Mapping)
        and isinstance(issue.get("issueNumber"), int)
        and not isinstance(issue.get("issueNumber"), bool)
    }
    candidates_by_name: dict[str, list[dict[str, object]]] = {}
    for issue in judgments.get("issues", []):
        if not isinstance(issue, Mapping):
            continue
        issue_number = issue.get("issueNumber")
        if not isinstance(issue_number, int) or isinstance(issue_number, bool):
            continue
        prepared_issue = prepared_issues.get(issue_number)
        if not isinstance(prepared_issue, Mapping):
            raise ValueError(f"Missing prepared issue {issue_number}.")
        issue_url = prepared_issue.get("issueUrl")
        if not isinstance(issue_url, str) or not issue_url:
            raise ValueError(f"Prepared issue {issue_number} has no issueUrl.")

        for recommendation in issue.get("recommendations", []):
            if (
                not isinstance(recommendation, Mapping)
                or recommendation.get("disposition") != "review-quarantine"
            ):
                continue
            target = recommendation.get("target")
            if not isinstance(target, Mapping) or target.get("kind") != "test":
                continue
            test_name = target.get("value")
            if not isinstance(test_name, str) or not test_name:
                continue
            evidence_ids = recommendation.get("evidenceIds", [])
            if not isinstance(evidence_ids, list) or not all(
                isinstance(value, str) and value for value in evidence_ids
            ):
                raise ValueError(
                    f"Quarantine recommendation for {test_name} has invalid evidenceIds."
                )
            candidate = {
                "issueNumber": issue_number,
                "issueUrl": issue_url,
                "issueLabels": _issue_labels(prepared_issue),
                "evidenceIds": sorted(set(evidence_ids)),
                "summary": str(recommendation.get("summary") or ""),
            }
            candidates_by_name.setdefault(test_name, []).append(candidate)

    tests: list[dict[str, object]] = []
    blocked_targets: list[dict[str, str]] = []
    for test_name, candidates in sorted(candidates_by_name.items()):
        source_issues = sorted(
            candidates,
            key=lambda item: int(item["issueNumber"]),
        )
        if not repository_policy_available:
            blocked_targets.append(
                {
                    "testName": test_name,
                    "reason": "repository-policy-unavailable",
                }
            )
            continue
        if _TEST_METHOD_NAME_RE.fullmatch(test_name) is None:
            blocked_targets.append(
                {
                    "testName": test_name,
                    "reason": "not-a-test-method",
                }
            )
            continue
        if any(
            isinstance(candidate["issueLabels"], frozenset)
            and "quarantined-test" in candidate["issueLabels"]
            for candidate in source_issues
        ):
            blocked_targets.append(
                {
                    "testName": test_name,
                    "reason": "already-quarantined-by-label",
                }
            )
            continue
        if any(candidate["issueLabels"] is None for candidate in source_issues):
            blocked_targets.append(
                {
                    "testName": test_name,
                    "reason": "source-labels-unavailable",
                }
            )
            continue
        flaky_evidence, evidence_gap = _classify_flaky_evidence(
            test_name,
            source_issues,
            observations,
        )
        if flaky_evidence is None:
            blocked_targets.append(
                {
                    "testName": test_name,
                    "reason": "insufficient-evidence-class",
                    "evidenceReason": evidence_gap,
                }
            )
            continue
        evidence_issue_number = flaky_evidence.pop("issueNumber")
        canonical = next(
            candidate
            for candidate in source_issues
            if candidate["issueNumber"] == evidence_issue_number
        )
        tests.append(
            {
                "testName": test_name,
                "issueNumber": canonical["issueNumber"],
                "issueUrl": canonical["issueUrl"],
                "issueNumbers": [
                    candidate["issueNumber"] for candidate in source_issues
                ],
                "issueUrls": [
                    candidate["issueUrl"] for candidate in source_issues
                ],
                **flaky_evidence,
                "summary": " ".join(
                    dict.fromkeys(
                        str(candidate["summary"])
                        for candidate in source_issues
                        if candidate["summary"]
                    )
                ),
            }
        )
    batch_id = f"quarantine:{_fingerprint(tests)}" if tests else None
    return {
        "schemaVersion": 1,
        "repository": repository,
        "snapshotId": snapshot_id,
        "repositoryPolicyDigest": (
            repository_policy_digest
            if repository_policy_available
            else None
        ),
        "repositoryPolicy": (
            dict(repository_policy)
            if repository_policy_available
            else None
        ),
        "operation": "prepare-quarantine-pr",
        "batchId": batch_id,
        "requiresSeparateApproval": True,
        "tests": tests,
        "workerPrompt": _worker_prompt(repository, tests) if tests else None,
        "blockedTargets": blocked_targets,
    }


def _classify_flaky_evidence(
    test_name: str,
    source_issues: list[dict[str, object]],
    observations: Mapping[str, Any],
) -> tuple[dict[str, object] | None, str]:
    occurrences = observations.get("occurrences")
    coverage = observations.get("coverage")
    if not isinstance(occurrences, list) or not isinstance(coverage, list):
        return None, "deterministic observations are unavailable"

    issue_numbers = {
        candidate["issueNumber"]
        for candidate in source_issues
        if isinstance(candidate.get("issueNumber"), int)
        and not isinstance(candidate.get("issueNumber"), bool)
    }
    exact_failures = [
        occurrence
        for occurrence in occurrences
        if (
            isinstance(occurrence, Mapping)
            and occurrence.get("issueNumber") in issue_numbers
            and occurrence.get("testName") == test_name
            and _is_test_results_evidence_id(
                occurrence.get("testNameEvidenceId")
            )
            and _valid_retry_identity(occurrence)
            and occurrence.get("testNameEvidenceId")
            in occurrence.get("evidenceIds", [])
        )
    ]
    exact_recoveries = [
        item
        for item in coverage
        if (
            isinstance(item, Mapping)
            and item.get("subjectKind") == "test"
            and item.get("testName") == test_name
            and item.get("status") == "succeeded"
            and _valid_retry_identity(item)
            and any(
                _is_test_results_evidence_id(evidence_id)
                for evidence_id in item.get("evidenceIds", [])
            )
        )
    ]

    for failure in sorted(
        exact_failures,
        key=lambda item: str(item.get("occurrenceId") or ""),
    ):
        for recovery in sorted(
            exact_recoveries,
            key=lambda item: str(item.get("coverageId") or ""),
        ):
            if not _is_later_equivalent_retry(failure, recovery):
                continue
            evidence_ids = sorted(
                {
                    evidence_id
                    for record in (failure, recovery)
                    for evidence_id in record.get("evidenceIds", [])
                    if isinstance(evidence_id, str) and evidence_id
                }
            )
            return (
                {
                    "issueNumber": failure["issueNumber"],
                    "evidenceClass": "A",
                    "evidenceReason": (
                        "the exact test failed and later passed in the same "
                        "run, commit, and job lane"
                    ),
                    "evidenceIds": evidence_ids,
                    "failureOccurrenceId": failure["occurrenceId"],
                    "recoveryCoverageId": recovery["coverageId"],
                    "failureIdentity": _retry_identity(failure),
                    "recoveryIdentity": _retry_identity(recovery),
                },
                "",
            )

    if any(
        _matching_successful_lane_exists(failure, coverage)
        for failure in exact_failures
    ):
        return (
            None,
            "a later equivalent lane succeeded, but no exact passing test result "
            "proved that the retry selected this test",
        )
    if exact_failures:
        return None, "no later equivalent retry passed the exact test"
    return None, "no artifact-derived exact failing test occurrence was collected"


def _valid_retry_identity(record: Mapping[str, Any]) -> bool:
    attempt = record.get("attempt")
    head_sha = record.get("headSha")
    return (
        isinstance(record.get("runId"), int)
        and not isinstance(record.get("runId"), bool)
        and isinstance(attempt, int)
        and not isinstance(attempt, bool)
        and attempt > 0
        and isinstance(head_sha, str)
        and re.fullmatch(r"[0-9a-f]{40}", head_sha) is not None
        and all(
            isinstance(record.get(field), str) and bool(record[field])
            for field in ("workflow", "jobName", "lane", "os")
        )
    )


def _retry_identity(record: Mapping[str, Any]) -> dict[str, object]:
    return {
        field: record[field]
        for field in (
            "runId",
            "attempt",
            "jobId",
            "headSha",
            "workflow",
            "jobName",
            "lane",
            "os",
        )
    }


def _is_later_equivalent_retry(
    failure: Mapping[str, Any],
    recovery: Mapping[str, Any],
) -> bool:
    return (
        recovery["runId"] == failure["runId"]
        and recovery["attempt"] > failure["attempt"]
        and recovery["headSha"] == failure["headSha"]
        and all(
            recovery[field] == failure[field]
            for field in ("workflow", "jobName", "lane", "os")
        )
    )


def _matching_successful_lane_exists(
    failure: Mapping[str, Any],
    coverage: list[Any],
) -> bool:
    return any(
        isinstance(item, Mapping)
        and item.get("subjectKind") == "lane"
        and item.get("status") == "succeeded"
        and _valid_retry_identity(item)
        and _is_later_equivalent_retry(failure, item)
        for item in coverage
    )


def _is_test_results_evidence_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(
            r"run:[1-9][0-9]*:attempt:[1-9][0-9]*:"
            r"job:[1-9][0-9]*:test-results",
            value,
        )
        is not None
    )


def apply_quarantine_source_inspection(
    request: Mapping[str, Any],
    inspection: Mapping[str, Any],
    *,
    source_revision: str,
    source_tree_digest: str,
) -> dict[str, object]:
    tests = request.get("tests")
    if not isinstance(tests, list):
        raise ValueError("Quarantine request tests must be a list.")
    if (
        not isinstance(source_revision, str)
        or re.fullmatch(r"[0-9a-f]{40}", source_revision) is None
    ):
        raise ValueError("Source revision must be a lowercase 40-character SHA.")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", source_tree_digest) is None:
        raise ValueError("Source tree digest must be a SHA-256 digest.")
    if set(inspection) != {"schemaVersion", "tests"}:
        raise ValueError("Source inspection has unexpected or missing fields.")
    if inspection.get("schemaVersion") != 1:
        raise ValueError("Unsupported source inspection schema.")
    inspection_tests = inspection.get("tests")
    if not isinstance(inspection_tests, list):
        raise ValueError("Source inspection tests must be a list.")

    results_by_name: dict[str, Mapping[str, Any]] = {}
    for result in inspection_tests:
        if not isinstance(result, Mapping):
            raise ValueError("Source inspection tests must contain objects.")
        if set(result) != {"testName", "status", "matches"}:
            raise ValueError(
                "Source inspection test has unexpected or missing fields."
            )
        test_name = result.get("testName")
        if not isinstance(test_name, str) or not test_name:
            raise ValueError("Source inspection testName must be nonempty.")
        if test_name in results_by_name:
            raise ValueError(f"Source inspection contains duplicate {test_name}.")
        results_by_name[test_name] = result

    request_names = {
        str(test["testName"])
        for test in tests
        if isinstance(test, Mapping)
        and isinstance(test.get("testName"), str)
        and test["testName"]
    }
    if set(results_by_name) != request_names:
        raise ValueError(
            "Source inspection test names must exactly match the request."
        )

    eligible: list[dict[str, object]] = []
    blocked = [
        dict(target)
        for target in request.get("blockedTargets", [])
        if isinstance(target, Mapping)
    ]
    for test in tests:
        if not isinstance(test, Mapping):
            raise ValueError("Quarantine request tests must contain objects.")
        test_name = test.get("testName")
        if not isinstance(test_name, str) or not test_name:
            raise ValueError("Quarantine request testName must be nonempty.")
        result = results_by_name[test_name]
        status = result.get("status")
        matches = result.get("matches")
        if (
            status not in {"resolved", "not-found", "ambiguous"}
            or not isinstance(matches, list)
        ):
            raise ValueError(f"Invalid source inspection result for {test_name}.")
        validated_matches = [
            _validate_source_inspection_match(test_name, match)
            for match in matches
        ]
        if status == "not-found":
            if validated_matches:
                raise ValueError(
                    f"Not-found source inspection for {test_name} has matches."
                )
            blocked.append(
                {
                    "testName": test_name,
                    "reason": "target-not-found-in-checkout",
                }
            )
            continue
        if status == "ambiguous":
            if len(validated_matches) < 2:
                raise ValueError(
                    f"Ambiguous source inspection for {test_name} needs multiple matches."
                )
            blocked.append(
                {
                    "testName": test_name,
                    "reason": "ambiguous-target-in-checkout",
                }
            )
            continue
        if len(validated_matches) != 1:
            raise ValueError(
                f"Resolved source inspection for {test_name} needs one match."
            )

        match = validated_matches[0]
        quarantine_attributes = match["quarantineAttributes"]
        active_issue_attributes = match["activeIssueAttributes"]
        if quarantine_attributes:
            blocked_target: dict[str, object] = {
                "testName": test_name,
                "reason": "already-quarantined",
                "sourceFile": match["file"],
            }
            issue_url = quarantine_attributes[0]["issueUrl"]
            if issue_url is not None:
                blocked_target["existingIssueUrl"] = issue_url
            blocked.append(blocked_target)
            continue
        if active_issue_attributes:
            blocked_target = {
                "testName": test_name,
                "reason": "already-suppressed",
                "sourceFile": match["file"],
                "existingAttribute": "ActiveIssue",
            }
            issue_url = active_issue_attributes[0]["issueUrl"]
            if issue_url is not None:
                blocked_target["existingIssueUrl"] = issue_url
            blocked.append(blocked_target)
            continue

        eligible.append(
            {
                **dict(test),
                "sourceLocation": {
                    "file": match["file"],
                    "line": match["line"],
                },
                "sourceValidation": {
                    "fileSemanticDigest": match["fileSemanticDigest"],
                    "fileQuarantines": match["fileQuarantines"],
                },
            }
        )

    inspected_request = {
        **dict(request),
        "sourceRevision": source_revision,
        "sourceTreeDigest": source_tree_digest,
        "blockedTargets": blocked,
    }
    return _request_for_tests(inspected_request, eligible)


def inspect_quarantine_session_request(
    request: Mapping[str, Any],
    checkout: Path | None,
    *,
    timeout_seconds: int = 120,
) -> dict[str, object]:
    tests = request.get("tests")
    if not isinstance(tests, list):
        raise ValueError("Quarantine request tests must be a list.")
    if not tests:
        return dict(request)
    if checkout is None:
        return _block_source_inspection_unavailable(request)

    try:
        checkout = checkout.expanduser().resolve(strict=True)
        tool_project = checkout / "tools" / "QuarantineTools"
        tests_root = checkout / "tests"
        if not tool_project.is_dir() or not tests_root.is_dir():
            raise ValueError("Checkout does not contain QuarantineTools and tests.")

        source_revision = _source_revision(checkout)
        source_tree_digest = _source_tree_digest(checkout)

        test_names = []
        for test in tests:
            if not isinstance(test, Mapping):
                raise ValueError("Quarantine request tests must contain objects.")
            test_name = test.get("testName")
            if not isinstance(test_name, str) or not test_name:
                raise ValueError("Quarantine request testName must be nonempty.")
            test_names.append(test_name)
        inspection_result = subprocess.run(
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
            cwd=checkout,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env={
                **os.environ,
                "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
                "DOTNET_CLI_UI_LANGUAGE": "en-US",
                "DOTNET_NOLOGO": "1",
                "MSBUILDTERMINALLOGGER": "false",
            },
        )
        if inspection_result.returncode != 0:
            raise ValueError("Quarantine source inspector failed.")
        inspection = json.loads(inspection_result.stdout)
        if not isinstance(inspection, Mapping):
            raise ValueError("Quarantine source inspector returned invalid JSON.")
        if (
            _source_revision(checkout) != source_revision
            or _source_tree_digest(checkout) != source_tree_digest
        ):
            raise ValueError("Checkout changed during quarantine source inspection.")
        return apply_quarantine_source_inspection(
            request,
            inspection,
            source_revision=source_revision,
            source_tree_digest=source_tree_digest,
        )
    except (
        FileNotFoundError,
        NotADirectoryError,
        OSError,
        subprocess.TimeoutExpired,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
    ):
        return _block_source_inspection_unavailable(request)


def _block_source_inspection_unavailable(
    request: Mapping[str, Any],
) -> dict[str, object]:
    tests = request.get("tests")
    if not isinstance(tests, list):
        raise ValueError("Quarantine request tests must be a list.")
    blocked = [
        dict(target)
        for target in request.get("blockedTargets", [])
        if isinstance(target, Mapping)
    ]
    blocked.extend(
        {
            "testName": test["testName"],
            "reason": "source-inspection-unavailable",
        }
        for test in tests
        if isinstance(test, Mapping)
        and isinstance(test.get("testName"), str)
        and test["testName"]
    )
    return _request_for_tests(
        {
            **dict(request),
            "blockedTargets": blocked,
        },
        [],
    )


def _source_revision(checkout: Path) -> str:
    result = subprocess.run(
        [
            "git",
            "--no-pager",
            "-C",
            str(checkout),
            "rev-parse",
            "HEAD",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    revision = result.stdout.strip()
    if result.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("Unable to resolve checkout revision.")
    return revision


def _source_tree_digest(checkout: Path) -> str:
    digest = hashlib.sha256()
    digest.update(b"ci-shepherd-quarantine-source-v2\0")
    for candidate in _source_input_files(checkout):
        relative_path = candidate.relative_to(checkout)
        encoded_path = os.fsencode(relative_path)
        digest.update(b"\0file\0")
        digest.update(encoded_path)
        digest.update(b"\0")
        digest.update(candidate.read_bytes())
    return f"sha256:{digest.hexdigest()}"


def _source_input_files(checkout: Path) -> list[Path]:
    skip_directories = {
        ".git",
        ".github",
        ".idea",
        ".vs",
        ".vscode",
        "artifacts",
        "bin",
        "dist",
        "node_modules",
        "obj",
        "out",
        "packages",
    }
    files: set[Path] = set()

    def collect(
        root: Path,
        *,
        accepted_suffixes: tuple[str, ...] | None,
    ) -> None:
        for directory, child_directories, child_files in os.walk(root):
            directory_path = Path(directory)
            child_directories[:] = sorted(
                child
                for child in child_directories
                if child.casefold()
                not in {name.casefold() for name in skip_directories}
            )
            for child in child_directories:
                if (directory_path / child).is_symlink():
                    raise ValueError(
                        "Quarantine source inputs must not traverse symlinks."
                    )
            for file_name in sorted(child_files):
                candidate = directory_path / file_name
                if candidate.is_symlink() or not candidate.is_file():
                    raise ValueError(
                        "Quarantine source inputs must contain regular files."
                    )
                if accepted_suffixes is not None and not file_name.endswith(
                    accepted_suffixes
                ):
                    continue
                if (
                    ".received." in file_name.casefold()
                    or ".verified." in file_name.casefold()
                ):
                    continue
                files.add(candidate)

    collect(checkout / "tests", accepted_suffixes=(".cs",))
    collect(checkout / "tools" / "QuarantineTools", accepted_suffixes=None)
    collect(
        checkout / "eng",
        accepted_suffixes=(".props", ".targets"),
    )
    for file_name in (
        "Directory.Build.props",
        "Directory.Build.targets",
        "Directory.Packages.props",
        "NuGet.config",
        "global.json",
    ):
        candidate = checkout / file_name
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError(f"Missing quarantine source input {file_name}.")
        files.add(candidate)
    return sorted(
        files,
        key=lambda path: os.fsencode(path.relative_to(checkout)),
    )


def _validate_source_inspection_match(
    test_name: str,
    match: object,
) -> dict[str, Any]:
    if not isinstance(match, Mapping):
        raise ValueError(f"Source inspection matches for {test_name} must be objects.")
    if set(match) != {
        "file",
        "line",
        "quarantineAttributes",
        "activeIssueAttributes",
        "fileSemanticDigest",
        "fileQuarantines",
    }:
        raise ValueError(
            f"Source inspection match for {test_name} has unexpected or missing fields."
        )
    file = match.get("file")
    line = match.get("line")
    if (
        not isinstance(file, str)
        or not file
        or Path(file).is_absolute()
        or ".." in Path(file).parts
    ):
        raise ValueError(f"Source inspection file for {test_name} is invalid.")
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        raise ValueError(f"Source inspection line for {test_name} is invalid.")
    file_semantic_digest = match.get("fileSemanticDigest")
    if (
        not isinstance(file_semantic_digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", file_semantic_digest) is None
    ):
        raise ValueError(
            f"Source inspection semantic digest for {test_name} is invalid."
        )
    return {
        "file": file,
        "line": line,
        "fileSemanticDigest": file_semantic_digest,
        "fileQuarantines": _validate_file_quarantines(
            test_name,
            match.get("fileQuarantines"),
        ),
        "quarantineAttributes": _validate_source_inspection_attributes(
            test_name,
            match.get("quarantineAttributes"),
            "QuarantinedTest",
        ),
        "activeIssueAttributes": _validate_source_inspection_attributes(
            test_name,
            match.get("activeIssueAttributes"),
            "ActiveIssue",
        ),
    }


def _validate_source_inspection_attributes(
    test_name: str,
    attributes: object,
    expected_name: str,
) -> list[dict[str, str | None]]:
    if not isinstance(attributes, list):
        raise ValueError(
            f"Source inspection attributes for {test_name} must be a list."
        )
    validated: list[dict[str, str | None]] = []
    for attribute in attributes:
        if not isinstance(attribute, Mapping) or set(attribute) != {
            "name",
            "issueUrl",
        }:
            raise ValueError(
                f"Source inspection attribute for {test_name} is invalid."
            )
        issue_url = attribute.get("issueUrl")
        if attribute.get("name") != expected_name or (
            issue_url is not None
            and (not isinstance(issue_url, str) or not issue_url)
        ):
            raise ValueError(
                f"Source inspection attribute for {test_name} is invalid."
            )
        validated.append({"name": expected_name, "issueUrl": issue_url})
    return validated


def _validate_file_quarantines(
    test_name: str,
    quarantines: object,
) -> list[dict[str, str | None]]:
    if not isinstance(quarantines, list):
        raise ValueError(
            f"Source file quarantine inventory for {test_name} must be a list."
        )
    validated: list[dict[str, str | None]] = []
    for quarantine in quarantines:
        if not isinstance(quarantine, Mapping) or set(quarantine) != {
            "testName",
            "issueUrl",
        }:
            raise ValueError(
                f"Source file quarantine inventory for {test_name} is invalid."
            )
        quarantined_test_name = quarantine.get("testName")
        issue_url = quarantine.get("issueUrl")
        if (
            not isinstance(quarantined_test_name, str)
            or not quarantined_test_name
            or (
                issue_url is not None
                and (not isinstance(issue_url, str) or not issue_url)
            )
        ):
            raise ValueError(
                f"Source file quarantine inventory for {test_name} is invalid."
            )
        validated.append(
            {
                "testName": quarantined_test_name,
                "issueUrl": issue_url,
            }
        )
    if validated != sorted(
        validated,
        key=lambda item: (item["testName"], item["issueUrl"] or ""),
    ):
        raise ValueError(
            f"Source file quarantine inventory for {test_name} is not sorted."
        )
    return validated


def build_quarantine_session_plan(
    request: Mapping[str, Any],
    session_events: list[Mapping[str, Any]],
) -> dict[str, object]:
    repository = request.get("repository")
    if not isinstance(repository, str) or not repository:
        raise ValueError("Quarantine request repository must be nonempty.")
    latest_by_batch: dict[str, Mapping[str, Any]] = {}
    for event in session_events:
        if (
            str(event.get("repository", "")).casefold()
            != repository.casefold()
        ):
            continue
        batch_id = event.get("batchId")
        status = event.get("status")
        if not isinstance(batch_id, str) or not batch_id:
            raise ValueError("Quarantine session event batchId must be nonempty.")
        if status not in _SESSION_STATUSES:
            raise ValueError(
                f"Unsupported quarantine session status for {batch_id}: {status}"
            )
        latest_by_batch[batch_id] = event

    active = next(
        (
            event
            for event in reversed(list(latest_by_batch.values()))
            if event.get("status") == "started"
        ),
        None,
    )
    open_pull_requests = [
        event
        for event in latest_by_batch.values()
        if event.get("status") == "pull-request-open"
    ]
    tests = request.get("tests")
    proposal: Mapping[str, Any] | None = request
    suppression_reason: str | None = None
    request_blocked_targets = request.get("blockedTargets", [])
    blocked_targets = (
        [
            dict(target)
            for target in request_blocked_targets
            if isinstance(target, Mapping)
        ]
        if isinstance(request_blocked_targets, list)
        else []
    )
    active_batch_id = str(active["batchId"]) if active is not None else None
    if active is not None:
        proposal = None
        suppression_reason = (
            "session-already-active"
            if active_batch_id == request.get("batchId")
            else "another-session-active"
        )
    elif not isinstance(tests, list) or not tests:
        proposal = None
        suppression_reason = (
            "awaiting-pull-request"
            if open_pull_requests
            else "blocked-targets"
            if blocked_targets
            else "no-candidates"
        )
    else:
        completed_test_names = {
            test["testName"]
            for event in latest_by_batch.values()
            if event.get("status") == "completed"
            for test in event.get("tests", [])
            if isinstance(test, Mapping)
            and isinstance(test.get("testName"), str)
            and test["testName"]
        }
        in_flight_test_names = {
            test["testName"]
            for event in latest_by_batch.values()
            if event.get("status") == "pull-request-open"
            for test in event.get("tests", [])
            if isinstance(test, Mapping)
            and isinstance(test.get("testName"), str)
            and test["testName"]
        }
        blocked_by_test = {
            target["test"]["testName"]: target
            for event in latest_by_batch.values()
            if event.get("status") in {"pull-request-open", "completed", "failed"}
            for target in event.get("blockedTargets", [])
            if isinstance(target, Mapping)
            and isinstance(target.get("test"), Mapping)
            and isinstance(target["test"].get("testName"), str)
            and target["test"]["testName"]
            and isinstance(target.get("reason"), str)
        }
        pending_tests = [
            dict(test)
            for test in tests
            if isinstance(test, Mapping)
            and test.get("testName") not in completed_test_names
            and test.get("testName") not in in_flight_test_names
            and test.get("testName") not in blocked_by_test
        ]
        blocked_targets.extend(
            {
                "testName": test.get("testName"),
                "reason": blocked_by_test[str(test.get("testName"))]["reason"],
            }
            for test in tests
            if isinstance(test, Mapping)
            and test.get("testName") in blocked_by_test
        )
        if not pending_tests:
            proposal = None
            suppression_reason = (
                "awaiting-pull-request"
                if in_flight_test_names
                else "blocked-targets"
                if blocked_targets
                else "batch-already-completed"
            )
        else:
            proposal = _request_for_tests(request, pending_tests)
        batch_id = proposal.get("batchId") if proposal is not None else None
        previous = latest_by_batch.get(batch_id) if isinstance(batch_id, str) else None
        if previous is not None and previous.get("status") == "completed":
            proposal = None
            suppression_reason = "batch-already-completed"

    return {
        "schemaVersion": 1,
        "repository": request.get("repository"),
        "snapshotId": request.get("snapshotId"),
        "proposal": dict(proposal) if proposal is not None else None,
        "suppressionReason": suppression_reason,
        "activeBatchId": active_batch_id,
        "openBatchIds": sorted(
            str(event["batchId"])
            for event in open_pull_requests
        ),
        "pendingPullRequests": sorted(
            {
                str(event["pullRequestUrl"])
                for event in open_pull_requests
                if isinstance(event.get("pullRequestUrl"), str)
            }
        ),
        "blockedTargets": blocked_targets if isinstance(tests, list) else [],
    }


def _session_ledger_path(state_directory: Path) -> Path:
    return state_directory / "ledgers" / "quarantine-sessions.jsonl"


def read_quarantine_session_events(
    state_directory: Path,
) -> list[dict[str, Any]]:
    return read_jsonl_rows(_session_ledger_path(state_directory))


def record_quarantine_session_event(
    state_directory: Path,
    request: Mapping[str, Any],
    *,
    status: str,
    recorded_at: str,
    session_id: str,
    pull_request_url: str | None = None,
    completed_test_names: list[str] | None = None,
    failure_reason: str | None = None,
    authorization_grant_id: str | None = None,
    pull_request_head_sha: str | None = None,
    blocked_targets: list[dict[str, str]] | None = None,
    allow_pull_request_head_update: bool = False,
    mutation_validation: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    if status not in _SESSION_STATUSES:
        raise ValueError(f"Unsupported quarantine session status: {status}")
    batch_id = request.get("batchId")
    repository = request.get("repository")
    if not isinstance(batch_id, str) or not batch_id:
        raise ValueError("Quarantine request batchId must be nonempty.")
    if not isinstance(repository, str) or not repository:
        raise ValueError("Quarantine request repository must be nonempty.")
    parse_aware_iso8601(recorded_at, "recordedAt")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("sessionId must be nonempty.")
    if authorization_grant_id is not None and (
        status != "started"
        or not isinstance(authorization_grant_id, str)
        or not authorization_grant_id
    ):
        raise ValueError(
            "authorizationGrantId must be nonempty and is valid only for started sessions."
        )
    if allow_pull_request_head_update and status != "pull-request-open":
        raise ValueError(
            "allowPullRequestHeadUpdate is valid only for pull-request-open sessions."
        )
    snapshot_id = request.get("snapshotId")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise ValueError("Quarantine request snapshotId must be nonempty.")
    if pull_request_url is not None and (
        not isinstance(pull_request_url, str) or not pull_request_url
    ):
        raise ValueError("pullRequestUrl must be a nonempty string when provided.")
    if pull_request_url is not None:
        match = _PULL_REQUEST_URL_RE.fullmatch(pull_request_url)
        if (
            match is None
            or match.group("repository").casefold() != repository.casefold()
        ):
            raise ValueError(
                f"pullRequestUrl must identify an {repository} pull request."
            )
    if status in {"pull-request-open", "completed"} and pull_request_head_sha is None:
        raise ValueError(
            f"A {status} quarantine session must record its pull request head SHA."
        )
    if pull_request_head_sha is not None and (
        status not in {"pull-request-open", "completed"}
        or re.fullmatch(r"[0-9a-fA-F]{40}", pull_request_head_sha) is None
    ):
        raise ValueError(
            "pullRequestHeadSha must be a 40-character Git SHA and is valid "
            "only for pull-request-open or completed sessions."
        )
    if status in {"pull-request-open", "completed"} and pull_request_url is None:
        raise ValueError(
            f"A {status} quarantine session must record its pull request."
        )
    if status in {"pull-request-open", "completed"} and completed_test_names is None:
        raise ValueError(
            f"Completed test names are required for a {status} quarantine session."
        )
    if completed_test_names is not None and status not in {
        "pull-request-open",
        "completed",
    }:
        raise ValueError(
            "Completed test names are valid only for pull-request-open or completed sessions."
        )
    if status == "failed":
        if not isinstance(failure_reason, str) or not failure_reason:
            raise ValueError("A failed quarantine session requires a failure reason.")
    elif failure_reason is not None:
        raise ValueError("failureReason is valid only for failed sessions.")
    if blocked_targets is not None and status not in {
        "pull-request-open",
        "completed",
        "failed",
    }:
        raise ValueError(
            "blockedTargets is valid only for pull-request-open, completed, "
            "or failed sessions."
        )
    if mutation_validation is not None and status not in {
        "pull-request-open",
        "completed",
    }:
        raise ValueError(
            "mutationValidation is valid only for pull-request-open or "
            "completed sessions."
        )

    request_tests = request.get("tests", [])
    if not isinstance(request_tests, list):
        raise ValueError("Quarantine request tests must be a list.")
    recorded_tests = request_tests
    if completed_test_names is not None:
        if not completed_test_names or not all(
            isinstance(test_name, str) and test_name
            for test_name in completed_test_names
        ):
            raise ValueError("Completed test names must contain strings.")
        requested_by_name = {
            test.get("testName"): test
            for test in request_tests
            if isinstance(test, Mapping)
            and isinstance(test.get("testName"), str)
            and test["testName"]
        }
        unknown = sorted(set(completed_test_names) - set(requested_by_name))
        if unknown:
            raise ValueError(
                "Completed tests are not present in the quarantine request: "
                + ", ".join(unknown)
            )
        recorded_tests = [
            requested_by_name[test_name]
            for test_name in sorted(set(completed_test_names))
        ]

    event: dict[str, object] = {
        "schemaVersion": 1,
        "repository": repository,
        "snapshotId": snapshot_id,
        "batchId": batch_id,
        "status": status,
        "recordedAt": recorded_at,
        "sessionId": session_id,
        "tests": recorded_tests,
    }
    for source_field in ("sourceRevision", "sourceTreeDigest"):
        source_value = request.get(source_field)
        if source_value is not None:
            event[source_field] = source_value
    if pull_request_url is not None:
        event["pullRequestUrl"] = pull_request_url
    if pull_request_head_sha is not None:
        event["pullRequestHeadSha"] = pull_request_head_sha.lower()
    normalized_blocked_targets: list[dict[str, object]] | None = None
    if blocked_targets is not None:
        requested_by_name = {
            test.get("testName"): test
            for test in request_tests
            if isinstance(test, Mapping)
            and isinstance(test.get("testName"), str)
            and test["testName"]
        }
        completed_names = set(completed_test_names or [])
        normalized_blocked_targets = []
        for target in blocked_targets:
            if not isinstance(target, Mapping):
                raise ValueError("Each blocked target must be an object.")
            test_name = target.get("testName")
            reason = target.get("reason")
            if (
                not isinstance(test_name, str)
                or test_name not in requested_by_name
                or test_name in completed_names
                or not isinstance(reason, str)
                or not reason
            ):
                raise ValueError(
                    "Each blocked target must identify an uncompleted requested "
                    "test and a nonempty reason."
                )
            normalized_blocked_targets.append(
                {
                    "test": dict(requested_by_name[test_name]),
                    "reason": reason,
                }
            )
    if failure_reason is not None:
        event["failureReason"] = failure_reason
    if normalized_blocked_targets is not None:
        event["blockedTargets"] = normalized_blocked_targets
    if authorization_grant_id is not None:
        event["authorizationGrantId"] = authorization_grant_id
    if mutation_validation is not None:
        event["mutationValidation"] = dict(mutation_validation)

    path = _session_ledger_path(state_directory)
    with exclusive_jsonl_lock(path):
        events = read_jsonl_rows(path)
        if authorization_grant_id is not None and any(
            existing.get("authorizationGrantId") == authorization_grant_id
            for existing in events
        ):
            raise ValueError("Quarantine authorization grant has already been consumed.")
        latest_by_batch = {
            str(existing["batchId"]): existing
            for existing in events
            if isinstance(existing.get("batchId"), str)
            and str(existing.get("repository", "")).casefold()
            == repository.casefold()
        }
        active = [
            existing
            for existing in latest_by_batch.values()
            if existing.get("status") == "started"
        ]
        previous = latest_by_batch.get(batch_id)
        if status == "started":
            if active:
                raise ValueError(
                    f"Quarantine session {active[-1]['batchId']} is already active."
                )
            if previous is not None and previous.get("status") in {
                "pull-request-open",
                "completed",
            }:
                raise ValueError(f"Quarantine batch {batch_id} is already completed.")
        elif status == "pull-request-open":
            if previous is None or previous.get("status") not in {
                "started",
                "pull-request-open",
            }:
                raise ValueError(
                    f"Quarantine batch {batch_id} does not have an active session."
                )
            if previous.get("sessionId") != session_id:
                raise ValueError(
                    f"Quarantine batch {batch_id} belongs to another session."
                )
            if previous.get("status") == "pull-request-open":
                previous_test_names = {
                    test.get("testName")
                    for test in previous.get("tests", [])
                    if isinstance(test, Mapping)
                }
                previous_head = previous.get("pullRequestHeadSha")
                if (
                    previous.get("pullRequestUrl") != pull_request_url
                    or previous_test_names != set(completed_test_names or [])
                    or previous.get("mutationValidation")
                    != event.get("mutationValidation")
                    or (
                        previous_head is not None
                        and not allow_pull_request_head_update
                    )
                ):
                    raise ValueError(
                        "Only a GET-verified exact pull-request-open event can "
                        "enrich or update its head SHA."
                    )
        elif status == "completed":
            if previous is None or previous.get("status") not in {
                "started",
                "pull-request-open",
            }:
                raise ValueError(
                    f"Quarantine batch {batch_id} has no active or open pull request."
                )
            if previous.get("sessionId") != session_id:
                raise ValueError(
                    f"Quarantine batch {batch_id} belongs to another session."
                )
            if (
                previous.get("status") == "pull-request-open"
                and previous.get("pullRequestHeadSha") is None
            ):
                raise ValueError(
                    "A legacy open pull request must be GET-verified and enriched "
                    "with its head SHA before completion."
                )
            # A worker can discover that a stale proposal was already satisfied
            # by a merged PR. In that case there is no truthful open-PR event to
            # record, so reconcile the started batch directly to completion.
            if (
                previous.get("status") == "pull-request-open"
                and previous.get("pullRequestUrl") != pull_request_url
            ):
                raise ValueError(
                    "Completed quarantine pull request does not match the recorded draft."
                )
            if (
                previous.get("status") == "pull-request-open"
                and previous.get("pullRequestHeadSha") is not None
                and previous.get("pullRequestHeadSha")
                != (pull_request_head_sha or "").lower()
            ):
                raise ValueError(
                    "Completed quarantine pull request head does not match the recorded draft."
                )
            if (
                previous.get("status") == "pull-request-open"
                and previous.get("mutationValidation")
                != event.get("mutationValidation")
            ):
                raise ValueError(
                    "Completed quarantine mutation validation does not match "
                    "the recorded draft."
                )
            previous_test_names = {
                test.get("testName")
                for test in previous.get("tests", [])
                if isinstance(test, Mapping)
            }
            if previous_test_names != set(completed_test_names or []):
                raise ValueError(
                    "Completed tests must exactly match the tests in the merged pull request."
                )
        else:
            if previous is None or previous.get("status") not in {
                "started",
                "pull-request-open",
            }:
                raise ValueError(
                    f"Quarantine batch {batch_id} has no active or open pull request."
                )
            if previous.get("sessionId") != session_id:
                raise ValueError(
                    f"Quarantine batch {batch_id} belongs to another session."
                )
        append_jsonl_rows(path, [event])
    return event


def render_quarantine_session_section(plan: Mapping[str, Any]) -> str:
    proposal = plan.get("proposal")
    lines = ["## Quarantine session", ""]
    if not isinstance(proposal, Mapping):
        reason = plan.get("suppressionReason")
        if reason == "no-candidates":
            lines.append("No quarantine session is needed.")
        elif reason in {"another-session-active", "session-already-active"}:
            lines.append(
                "No new session was proposed because quarantine session "
                f"`{plan.get('activeBatchId')}` is already active."
            )
        elif reason == "batch-already-completed":
            lines.append(
                "The current quarantine batch was already completed; no duplicate "
                "session was proposed."
            )
        elif reason == "awaiting-pull-request":
            pull_requests = plan.get("pendingPullRequests", [])
            suffix = ""
            if isinstance(pull_requests, list) and pull_requests:
                suffix = ": " + ", ".join(f"`{url}`" for url in pull_requests)
            lines.append(
                "No new session was proposed because the candidate tests are "
                f"already covered by an unmerged quarantine pull request{suffix}."
            )
        elif reason == "blocked-targets":
            lines.append(
                "No new session was proposed because every unchanged target is "
                "blocked. New source evidence will make the target eligible again."
            )
        else:
            lines.append("No quarantine session was proposed.")
        blocked_targets = plan.get("blockedTargets", [])
        if isinstance(blocked_targets, list) and blocked_targets:
            lines.extend(["", "| Blocked test | Reason |", "|---|---|"])
            for target in blocked_targets:
                if isinstance(target, Mapping):
                    lines.append(
                        f"| `{target.get('testName')}` | {target.get('reason')} |"
                    )
        return "\n".join(lines) + "\n"

    lines.extend(
        [
            "One local quarantine session is ready for approval.",
            "Every candidate was resolved to exactly one unsuppressed source method "
            "at the recorded checkout revision and tree digest.",
            "",
            f"**Batch:** `{proposal.get('batchId')}`",
            "",
            "| Candidate test target | Original issue |",
            "|---|---|",
        ]
    )
    tests = proposal.get("tests", [])
    if isinstance(tests, list):
        for test in tests:
            if not isinstance(test, Mapping):
                continue
            test_name = str(test.get("testName", "")).replace("|", "\\|")
            issue_numbers = test.get("issueNumbers")
            issue_urls = test.get("issueUrls")
            if not isinstance(issue_numbers, list) or not isinstance(issue_urls, list):
                issue_numbers = [test.get("issueNumber")]
                issue_urls = [test.get("issueUrl")]
            issue_links = ", ".join(
                f"[#{number}]({url})"
                for number, url in zip(issue_numbers, issue_urls, strict=True)
            )
            lines.append(
                f"| `{test_name}` | {issue_links} |"
            )
    pending_pull_requests = plan.get("pendingPullRequests", [])
    blocked_targets = plan.get("blockedTargets", [])
    if isinstance(blocked_targets, list) and blocked_targets:
        lines.extend(["", "| Blocked test | Reason |", "|---|---|"])
        for target in blocked_targets:
            if isinstance(target, Mapping):
                lines.append(
                    f"| `{target.get('testName')}` | {target.get('reason')} |"
                )
    if isinstance(pending_pull_requests, list) and pending_pull_requests:
        lines.extend(
            [
                "",
                "**Outstanding quarantine pull requests:** "
                + ", ".join(f"`{url}`" for url in pending_pull_requests),
            ]
        )
    return "\n".join(lines) + "\n"
