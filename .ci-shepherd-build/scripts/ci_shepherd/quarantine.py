from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .jsonl import append_jsonl_rows, exclusive_jsonl_lock, read_jsonl_rows


_SESSION_STATUSES = frozenset(
    {"started", "pull-request-open", "completed", "failed"}
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
        "Run QuarantineTools once for each test with that test's original issue URL:",
    ]
    for test in tests:
        issue_numbers = test.get("issueNumbers")
        if not isinstance(issue_numbers, list) or not issue_numbers:
            issue_numbers = [test["issueNumber"]]
        addresses = ", ".join(f"Addresses #{number}" for number in issue_numbers)
        lines.append(
            f"- `{test['testName']}` — use {test['issueUrl']} with QuarantineTools; "
            f"the PR body must include {addresses}"
        )
    lines.extend(
        [
            "",
            "After editing:",
            "1. Confirm the diff contains only the expected quarantine attributes and required using directives.",
            "2. Run `./restore.sh` once.",
            "3. Build every affected test project.",
            "4. Run each affected test through the repository's MTP filters and confirm it is excluded as quarantined.",
            "5. If a test cannot be resolved to an exact fully-qualified method, leave it unchanged, report it as blocked, and continue with the remaining targets.",
            "6. If the diff touches unexpected files or validation still fails after one quarantine-only correction, stop and report the failure.",
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
) -> dict[str, object]:
    repository = prepared.get("repository")
    snapshot_id = prepared.get("snapshotId")
    if not isinstance(repository, str) or not repository:
        raise ValueError("Prepared repository must be a nonempty string.")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise ValueError("Prepared snapshotId must be a nonempty string.")
    if judgments.get("snapshotId") != snapshot_id:
        raise ValueError("Judgments snapshotId must match prepared snapshotId.")

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
                "evidenceIds": sorted(set(evidence_ids)),
                "summary": str(recommendation.get("summary") or ""),
            }
            candidates_by_name.setdefault(test_name, []).append(candidate)

    tests: list[dict[str, object]] = []
    for test_name, candidates in sorted(candidates_by_name.items()):
        source_issues = sorted(
            candidates,
            key=lambda item: int(item["issueNumber"]),
        )
        canonical = source_issues[0]
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
                "evidenceIds": sorted(
                    {
                        evidence_id
                        for candidate in source_issues
                        for evidence_id in candidate["evidenceIds"]
                    }
                ),
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
        "operation": "prepare-quarantine-pr",
        "batchId": batch_id,
        "requiresSeparateApproval": True,
        "tests": tests,
        "workerPrompt": _worker_prompt(repository, tests) if tests else None,
    }


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
        pending_tests = [
            dict(test)
            for test in tests
            if isinstance(test, Mapping)
            and test.get("testName") not in completed_test_names
            and test.get("testName") not in in_flight_test_names
        ]
        if not pending_tests:
            proposal = None
            suppression_reason = (
                "awaiting-pull-request"
                if in_flight_test_names
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
) -> dict[str, object]:
    if status not in _SESSION_STATUSES:
        raise ValueError(f"Unsupported quarantine session status: {status}")
    batch_id = request.get("batchId")
    repository = request.get("repository")
    if not isinstance(batch_id, str) or not batch_id:
        raise ValueError("Quarantine request batchId must be nonempty.")
    if not isinstance(repository, str) or not repository:
        raise ValueError("Quarantine request repository must be nonempty.")
    if not isinstance(recorded_at, str) or not recorded_at:
        raise ValueError("recordedAt must be nonempty.")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("sessionId must be nonempty.")
    if pull_request_url is not None and (
        not isinstance(pull_request_url, str) or not pull_request_url
    ):
        raise ValueError("pullRequestUrl must be a nonempty string when provided.")
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
        "batchId": batch_id,
        "status": status,
        "recordedAt": recorded_at,
        "sessionId": session_id,
        "tests": recorded_tests,
    }
    if pull_request_url is not None:
        event["pullRequestUrl"] = pull_request_url

    path = _session_ledger_path(state_directory)
    with exclusive_jsonl_lock(path):
        events = read_jsonl_rows(path)
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
            if previous is None or previous.get("status") != "started":
                raise ValueError(
                    f"Quarantine batch {batch_id} does not have an active session."
                )
            if previous.get("sessionId") != session_id:
                raise ValueError(
                    f"Quarantine batch {batch_id} belongs to another session."
                )
        elif status == "completed":
            if previous is None or previous.get("status") != "pull-request-open":
                raise ValueError(
                    f"Quarantine batch {batch_id} has no pull request awaiting merge."
                )
            if previous.get("sessionId") != session_id:
                raise ValueError(
                    f"Quarantine batch {batch_id} belongs to another session."
                )
            if previous.get("pullRequestUrl") != pull_request_url:
                raise ValueError(
                    "Completed quarantine pull request does not match the recorded draft."
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
        else:
            lines.append("No quarantine session was proposed.")
        return "\n".join(lines) + "\n"

    lines.extend(
        [
            "One local quarantine session is ready for approval.",
            "The worker must resolve each target to an exact test method. "
            "Unresolved targets remain unchanged and are reported as blocked.",
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
    return "\n".join(lines) + "\n"
