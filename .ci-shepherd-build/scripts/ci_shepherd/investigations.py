from __future__ import annotations

import copy
from datetime import timedelta
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping

from .jsonl import append_jsonl_rows, exclusive_jsonl_lock, read_jsonl_rows
from .timeutils import parse_aware_iso8601


_OUTCOMES = frozenset(
    {
        "fixable",
        "recovered",
        "duplicate",
        "needs-evidence",
        "needs-human",
        "not-actionable",
        "inconclusive",
    }
)
_SESSION_STATUSES = frozenset({"started", "completed", "failed", "abandoned"})
_SESSION_FAILURE_CATEGORIES = frozenset(
    {
        "worker-error",
        "invalid-result",
        "out-of-scope-evidence",
        "worker-unavailable",
    }
)
_MAX_INVESTIGATION_ATTEMPTS = 2
_MAX_INVESTIGATION_SESSION_AGE = timedelta(hours=1)


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


def _source_evidence_fingerprint(issue: Mapping[str, Any]) -> str:
    return _fingerprint(
        {
            key: value
            for key, value in issue.items()
            if key not in {"investigationResult", "investigationResults"}
        }
    )


def _worker_prompt(request: Mapping[str, Any]) -> str:
    allowed_urls = request.get("allowedEvidenceUrls", [])
    return (
        f"Investigate {request['issueUrl']} for the CI shepherd.\n\n"
        "Do not invoke issue-investigation or discover additional evidence. Use "
        "only the evidence IDs and exact URLs assigned below; do not follow links, "
        "search GitHub, or query repository history. If those inputs are "
        "insufficient, return needs-evidence. Do not edit code, post comments, "
        "assign anyone, or open a pull request.\n\n"
        f"Target: {request['target']['kind']}:{request['target']['value']}\n"
        f"Question: {request['question']}\n"
        f"Evidence already checked: {', '.join(request['evidenceIds'])}\n"
        f"Allowed evidence URLs: {', '.join(allowed_urls) or 'none'}\n"
        f"Missing evidence: {', '.join(request['missingEvidence']) or 'none'}\n"
        f"Stop condition: {request['stopCondition']}\n\n"
        "Decide whether this is fixable, recovered, a duplicate, blocked on more "
        "evidence or human input, not actionable, or still inconclusive. Return "
        "only JSON with this shape:\n"
        "{\n"
        '  "outcome": "fixable | recovered | duplicate | needs-evidence | '
        'needs-human | not-actionable | inconclusive",\n'
        '  "summary": "evidence-backed conclusion",\n'
        '  "evidenceIds": ["only IDs listed above"],\n'
        '  "reassessWhen": "one concrete wake condition",\n'
        '  "missingEvidence": [],\n'
        '  "fixHandoff": null\n'
        "}\n"
        "For a fixable result, replace fixHandoff with an object containing "
        "problem, likelyPaths, and validation. Do not include markdown."
    )


def build_investigation_plan(
    prepared: Mapping[str, Any],
    judgments: Mapping[str, Any],
    prior_results: list[Mapping[str, Any]],
    session_events: list[Mapping[str, Any]] | None = None,
    *,
    max_requests: int = 5,
) -> dict[str, object]:
    repository = prepared.get("repository")
    snapshot_id = prepared.get("snapshotId")
    if not isinstance(repository, str) or not repository:
        raise ValueError("Prepared repository must be a nonempty string.")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise ValueError("Prepared snapshotId must be a nonempty string.")
    if judgments.get("snapshotId") != snapshot_id:
        raise ValueError("Judgments snapshotId must match prepared snapshotId.")
    if (
        not isinstance(max_requests, int)
        or isinstance(max_requests, bool)
        or max_requests < 1
    ):
        raise ValueError("max_requests must be a positive integer.")

    prepared_issues = {
        issue["issueNumber"]: issue
        for issue in prepared.get("issues", [])
        if isinstance(issue, Mapping)
        and isinstance(issue.get("issueNumber"), int)
        and not isinstance(issue.get("issueNumber"), bool)
    }
    completed_ids = {
        result.get("investigationId")
        for result in prior_results
        if isinstance(result.get("investigationId"), str)
        and str(result.get("repository", "")).casefold() == repository.casefold()
    }
    latest_session_by_id: dict[str, Mapping[str, Any]] = {}
    attempt_count_by_id: dict[str, int] = {}
    for event in session_events or []:
        if str(event.get("repository", "")).casefold() != repository.casefold():
            continue
        investigation_id = event.get("investigationId")
        status = event.get("status")
        if not isinstance(investigation_id, str) or not investigation_id:
            raise ValueError("Investigation session event has no investigationId.")
        if status not in _SESSION_STATUSES:
            raise ValueError(
                f"Unsupported investigation session status for {investigation_id}: {status}"
            )
        latest_session_by_id[investigation_id] = event
        if status == "started":
            attempt_count_by_id[investigation_id] = (
                attempt_count_by_id.get(investigation_id, 0) + 1
            )
    active_ids = {
        investigation_id
        for investigation_id, event in latest_session_by_id.items()
        if event.get("status") == "started"
    }
    requests: list[dict[str, object]] = []
    reused: list[str] = []
    active: list[str] = []
    exhausted: list[dict[str, object]] = []
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
        evidence_fingerprint = _source_evidence_fingerprint(prepared_issue)

        for recommendation in issue.get("recommendations", []):
            if (
                not isinstance(recommendation, Mapping)
                or recommendation.get("disposition") != "investigate"
            ):
                continue
            target = recommendation.get("target")
            if not isinstance(target, Mapping):
                continue
            evidence_ids = recommendation.get("evidenceIds", [])
            missing_evidence = recommendation.get("missingEvidence", [])
            if not isinstance(evidence_ids, list) or not all(
                isinstance(value, str) and value for value in evidence_ids
            ):
                raise ValueError(
                    f"Investigation for issue {issue_number} has invalid evidenceIds."
                )
            if not isinstance(missing_evidence, list) or not all(
                isinstance(value, str) and value for value in missing_evidence
            ):
                raise ValueError(
                    f"Investigation for issue {issue_number} has invalid missingEvidence."
                )
            identity = {
                "repository": repository.casefold(),
                "issueNumber": issue_number,
                "target": dict(target),
                "sourceEvidenceFingerprint": evidence_fingerprint,
            }
            investigation_id = f"investigation:{_fingerprint(identity)}"
            if investigation_id in completed_ids:
                reused.append(investigation_id)
                continue
            if investigation_id in active_ids:
                active.append(investigation_id)
                continue
            attempt = attempt_count_by_id.get(investigation_id, 0) + 1
            if attempt > _MAX_INVESTIGATION_ATTEMPTS:
                exhausted.append(
                    {
                        "investigationId": investigation_id,
                        "issueNumber": issue_number,
                        "target": dict(target),
                        "reason": "investigation-attempt-limit",
                    }
                )
                continue
            allowed_evidence_urls = sorted(
                {
                    str(record["url"])
                    for record in prepared_issue.get("evidenceBundle", [])
                    if isinstance(record, Mapping)
                    and record.get("id") in evidence_ids
                    and isinstance(record.get("url"), str)
                    and record["url"]
                }
                | (
                    {issue_url}
                    if f"issue:{issue_number}" in evidence_ids
                    else set()
                )
            )
            request: dict[str, object] = {
                "schemaVersion": 1,
                "repository": repository,
                "snapshotId": snapshot_id,
                "investigationId": investigation_id,
                "issueNumber": issue_number,
                "issueUrl": issue_url,
                "target": dict(target),
                "sourceEvidenceFingerprint": evidence_fingerprint,
                "question": str(recommendation.get("summary") or ""),
                "evidenceIds": sorted(set(evidence_ids)),
                "allowedEvidenceUrls": allowed_evidence_urls,
                "missingEvidence": list(missing_evidence),
                "stopCondition": str(recommendation.get("reassessWhen") or ""),
                "attempt": attempt,
                "maxAttempts": _MAX_INVESTIGATION_ATTEMPTS,
            }
            request["workerPrompt"] = _worker_prompt(request)
            requests.append(request)

    requests.sort(
        key=lambda item: (
            int(item["issueNumber"]),
            str(item["target"].get("kind")),
            json.dumps(item["target"].get("value"), sort_keys=True),
        )
    )
    deferred = exhausted + [
        {
            "investigationId": request["investigationId"],
            "issueNumber": request["issueNumber"],
            "target": request["target"],
            "reason": "per-cycle-investigation-budget",
        }
        for request in requests[max_requests:]
    ]
    requests = requests[:max_requests]
    reused.sort()
    active.sort()
    return {
        "schemaVersion": 1,
        "repository": repository,
        "snapshotId": snapshot_id,
        "requests": requests,
        "deferredRequests": deferred,
        "maxRequests": max_requests,
        "reusedInvestigationIds": reused,
        "activeInvestigationIds": active,
    }


def _results_path(state_directory: Path) -> Path:
    return state_directory / "ledgers" / "investigation-results.jsonl"


def _sessions_path(state_directory: Path) -> Path:
    return state_directory / "ledgers" / "investigation-sessions.jsonl"


def read_investigation_results(
    state_directory: Path,
) -> list[dict[str, Any]]:
    return read_jsonl_rows(_results_path(state_directory))


def read_investigation_session_events(
    state_directory: Path,
) -> list[dict[str, Any]]:
    return read_jsonl_rows(_sessions_path(state_directory))


def select_investigation_request(
    plan: Mapping[str, Any],
    investigation_id: str,
    *,
    state_directory: Path | None = None,
) -> dict[str, object]:
    requests = plan.get("requests")
    if not isinstance(requests, list):
        raise ValueError("Investigation plan must contain requests.")
    repository = plan.get("repository")
    if (
        state_directory is not None
        and isinstance(repository, str)
        and repository
    ):
        latest = _latest_session_event(
            read_investigation_session_events(state_directory),
            repository=repository,
            investigation_id=investigation_id,
        )
        persisted_request = latest.get("request") if latest is not None else None
        if (
            latest is not None
            and latest.get("status") == "started"
            and isinstance(persisted_request, dict)
        ):
            return persisted_request
    matches = [
        request
        for request in requests
        if isinstance(request, dict)
        and request.get("investigationId") == investigation_id
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1 or state_directory is None:
        raise ValueError(
            f"Investigation plan must contain exactly one {investigation_id} request."
        )
    active_ids = plan.get("activeInvestigationIds")
    if (
        not isinstance(active_ids, list)
        or investigation_id not in active_ids
        or not isinstance(repository, str)
        or not repository
    ):
        raise ValueError(
            f"Investigation plan has no active {investigation_id} request."
        )
    latest = _latest_session_event(
        read_investigation_session_events(state_directory),
        repository=repository,
        investigation_id=investigation_id,
    )
    persisted_request = latest.get("request") if latest is not None else None
    if (
        latest is None
        or latest.get("status") != "started"
        or not isinstance(persisted_request, dict)
    ):
        raise ValueError(
            f"Investigation {investigation_id} has no recoverable active request."
        )
    return persisted_request


def _investigation_identity(
    request: Mapping[str, Any],
) -> tuple[str, str]:
    investigation_id = request.get("investigationId")
    repository = request.get("repository")
    if not isinstance(investigation_id, str) or not investigation_id:
        raise ValueError("Investigation request has no investigationId.")
    if not isinstance(repository, str) or not repository:
        raise ValueError("Investigation request has no repository.")
    return investigation_id, repository


def _session_event(
    request: Mapping[str, Any],
    *,
    status: str,
    recorded_at: str,
    session_id: str,
    checkout: Path | None = None,
    failure_reason: str | None = None,
    failure_category: str | None = None,
) -> dict[str, object]:
    if status not in _SESSION_STATUSES:
        raise ValueError(f"Unsupported investigation session status: {status}")
    parse_aware_iso8601(recorded_at, "recordedAt")
    investigation_id, repository = _investigation_identity(request)
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("sessionId must be nonempty.")
    if status in {"failed", "abandoned"}:
        if not isinstance(failure_reason, str) or not failure_reason:
            raise ValueError(
                f"A {status} investigation session requires a failure reason."
            )
        failure_category = failure_category or (
            "worker-unavailable" if status == "abandoned" else "worker-error"
        )
        if failure_category not in _SESSION_FAILURE_CATEGORIES:
            raise ValueError(
                f"Unsupported investigation failure category: {failure_category}"
            )
    elif failure_reason is not None or failure_category is not None:
        raise ValueError(
            "failureReason and failureCategory are valid only for failed sessions."
        )
    event: dict[str, object] = {
        "schemaVersion": 1,
        "repository": repository,
        "investigationId": investigation_id,
        "issueNumber": request.get("issueNumber"),
        "target": request.get("target"),
        "sourceEvidenceFingerprint": request.get("sourceEvidenceFingerprint"),
        "status": status,
        "recordedAt": recorded_at,
        "sessionId": session_id,
    }
    if status in {"started", "completed", "abandoned"}:
        if checkout is None:
            raise ValueError(f"A {status} investigation session requires a checkout.")
        event["checkoutPath"] = _canonical_checkout(checkout)
        event["checkoutHead"] = _checkout_head(checkout)
    elif checkout is not None:
        raise ValueError(
            "checkout is valid only for started, completed, or abandoned sessions."
        )
    if status == "started":
        event["request"] = dict(request)
    if failure_reason is not None:
        event["failureReason"] = failure_reason
        event["failureCategory"] = failure_category
    return event


def _latest_session_event(
    events: list[Mapping[str, Any]],
    *,
    repository: str,
    investigation_id: str,
) -> Mapping[str, Any] | None:
    return next(
        (
            event
            for event in reversed(events)
            if str(event.get("repository", "")).casefold() == repository.casefold()
            and event.get("investigationId") == investigation_id
        ),
        None,
    )


def _validate_session_transition(
    previous: Mapping[str, Any] | None,
    event: Mapping[str, Any],
) -> None:
    investigation_id = event["investigationId"]
    status = event["status"]
    session_id = event["sessionId"]
    if status == "started":
        if previous is not None and previous.get("status") == "started":
            raise ValueError(
                f"Investigation {investigation_id} already has an active session."
            )
        if previous is not None and previous.get("status") == "completed":
            raise ValueError(f"Investigation {investigation_id} is already completed.")
        return
    if previous is None or previous.get("status") != "started":
        raise ValueError(
            f"Investigation {investigation_id} does not have an active session."
        )
    if previous.get("sessionId") != session_id:
        raise ValueError(f"Investigation {investigation_id} belongs to another session.")
    if status in {"completed", "abandoned"} and previous.get(
        "checkoutPath"
    ) != event.get("checkoutPath"):
        raise ValueError(
            f"Investigation {investigation_id} belongs to another checkout."
        )
    if status in {"completed", "abandoned"} and previous.get(
        "checkoutHead"
    ) != event.get("checkoutHead"):
        raise ValueError(f"Investigation {investigation_id} checkout HEAD changed.")


def record_investigation_session_event(
    state_directory: Path,
    request: Mapping[str, Any],
    *,
    status: str,
    recorded_at: str,
    session_id: str,
    checkout: Path | None = None,
    failure_reason: str | None = None,
    failure_category: str | None = None,
    confirm_worker_stopped: bool = False,
) -> dict[str, object]:
    if status == "started" and checkout is not None:
        _require_clean_checkout(checkout)
    event = _session_event(
        request,
        status=status,
        recorded_at=recorded_at,
        session_id=session_id,
        checkout=checkout,
        failure_reason=failure_reason,
        failure_category=failure_category,
    )
    investigation_id = str(event["investigationId"])
    repository = str(event["repository"])
    path = _sessions_path(state_directory)
    with exclusive_jsonl_lock(path):
        previous = _latest_session_event(
            read_jsonl_rows(path),
            repository=repository,
            investigation_id=investigation_id,
        )
        if status == "abandoned":
            if not confirm_worker_stopped:
                raise ValueError(
                    "Abandonment requires confirmation that the worker stopped."
                )
            _validate_abandonment(previous, event, checkout)
        elif confirm_worker_stopped:
            raise ValueError(
                "confirm_worker_stopped is valid only for abandoned sessions."
            )
        _validate_session_transition(previous, event)
        append_jsonl_rows(path, [event])
    return event


def record_investigation_result(
    state_directory: Path,
    request: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    recorded_at: str,
    session_id: str,
    checkout: Path,
) -> dict[str, object]:
    parse_aware_iso8601(recorded_at, "recordedAt")
    outcome = result.get("outcome")
    if outcome not in _OUTCOMES:
        raise ValueError(f"Unsupported investigation outcome: {outcome}")
    investigation_id, repository = _investigation_identity(request)
    summary = result.get("summary")
    evidence_ids = result.get("evidenceIds")
    reassess_when = result.get("reassessWhen")
    if not isinstance(summary, str) or not summary:
        raise ValueError("Investigation result summary must be nonempty.")
    if not isinstance(evidence_ids, list) or not all(
        isinstance(value, str) and value for value in evidence_ids
    ):
        raise ValueError("Investigation result evidenceIds must contain strings.")
    if not set(evidence_ids).issubset(set(request.get("evidenceIds", []))):
        raise ValueError("Investigation result cites evidence outside its request.")
    if not isinstance(reassess_when, str) or not reassess_when:
        raise ValueError("Investigation result reassessWhen must be nonempty.")
    fix_handoff = result.get("fixHandoff")
    if outcome == "fixable" and not isinstance(fix_handoff, Mapping):
        raise ValueError("A fixable investigation requires fixHandoff.")
    if isinstance(fix_handoff, Mapping):
        problem = fix_handoff.get("problem")
        if not isinstance(problem, str) or not problem:
            raise ValueError("fixHandoff.problem must be nonempty.")
        for field in ("likelyPaths", "validation"):
            values = fix_handoff.get(field)
            if not isinstance(values, list) or not values or not all(
                isinstance(value, str) and value for value in values
            ):
                raise ValueError(f"fixHandoff.{field} must contain strings.")

    event: dict[str, object] = {
        "schemaVersion": 1,
        "repository": repository,
        "investigationId": investigation_id,
        "issueNumber": request.get("issueNumber"),
        "target": request.get("target"),
        "sourceEvidenceFingerprint": request.get("sourceEvidenceFingerprint"),
        "outcome": outcome,
        "summary": summary,
        "evidenceIds": sorted(set(evidence_ids)),
        "reassessWhen": reassess_when,
        "recordedAt": recorded_at,
        "sessionId": session_id,
    }
    if isinstance(fix_handoff, Mapping):
        event["fixHandoff"] = dict(fix_handoff)
    if isinstance(result.get("missingEvidence"), list):
        event["missingEvidence"] = list(result["missingEvidence"])

    session_event = _session_event(
        request,
        status="completed",
        recorded_at=recorded_at,
        session_id=session_id,
        checkout=checkout,
    )
    _require_clean_checkout(checkout)
    sessions_path = _sessions_path(state_directory)
    results_path = _results_path(state_directory)
    with exclusive_jsonl_lock(sessions_path):
        session_events = read_jsonl_rows(sessions_path)
        previous_session = _latest_session_event(
            session_events,
            repository=repository,
            investigation_id=investigation_id,
        )
        with exclusive_jsonl_lock(results_path):
            existing = next(
                (
                    row
                    for row in read_jsonl_rows(results_path)
                    if row.get("investigationId") == investigation_id
                    and str(row.get("repository", "")).casefold()
                    == repository.casefold()
                ),
                None,
            )
            if existing is not None:
                existing_payload = {
                    key: value
                    for key, value in existing.items()
                    if key != "recordedAt"
                }
                event_payload = {
                    key: value
                    for key, value in event.items()
                    if key != "recordedAt"
                }
                if existing_payload != event_payload:
                    raise ValueError(
                        f"Investigation {investigation_id} is already recorded."
                    )
                if (
                    previous_session is not None
                    and previous_session.get("status") == "started"
                ):
                    _validate_session_transition(previous_session, session_event)
                    append_jsonl_rows(sessions_path, [session_event])
                return dict(existing)
            _validate_session_transition(previous_session, session_event)
            append_jsonl_rows(results_path, [event])
            append_jsonl_rows(sessions_path, [session_event])
    return event


def _canonical_checkout(checkout: Path) -> str:
    if checkout.is_symlink():
        raise ValueError("Investigation checkout must not be a symlink.")
    try:
        resolved = checkout.expanduser().resolve(strict=True)
    except FileNotFoundError as error:
        raise ValueError(f"Investigation checkout does not exist: {checkout}") from error
    if not resolved.is_dir():
        raise ValueError("Investigation checkout must be a directory.")
    return str(resolved)


def _require_clean_checkout(checkout: Path) -> None:
    checkout_path = _canonical_checkout(checkout)
    result = subprocess.run(
        ["git", "-C", checkout_path, "status", "--porcelain", "--untracked-files=all"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise ValueError(
            "Investigation checkout status could not be verified: "
            + (result.stderr.strip() or f"git exited {result.returncode}.")
        )
    if result.stdout:
        raise ValueError("Investigation checkout is not clean.")


def _checkout_head(checkout: Path) -> str:
    checkout_path = _canonical_checkout(checkout)
    result = subprocess.run(
        [
            "git",
            "--no-pager",
            "-C",
            checkout_path,
            "rev-parse",
            "--verify",
            "HEAD",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    head = result.stdout.strip()
    if result.returncode != 0 or re.fullmatch(r"[0-9a-fA-F]{40}", head) is None:
        raise ValueError(
            "Investigation checkout HEAD could not be verified: "
            + (result.stderr.strip() or f"git exited {result.returncode}.")
        )
    return head.lower()


def _validate_abandonment(
    previous: Mapping[str, Any] | None,
    event: Mapping[str, Any],
    checkout: Path | None,
) -> None:
    if previous is None or previous.get("status") != "started":
        raise ValueError("Investigation does not have an active session to abandon.")
    if checkout is None:
        raise ValueError("Abandonment requires the investigation checkout.")
    if previous.get("checkoutPath") != _canonical_checkout(checkout):
        raise ValueError("Investigation belongs to another checkout.")
    started_at = parse_aware_iso8601(previous.get("recordedAt"), "recordedAt")
    abandoned_at = parse_aware_iso8601(event.get("recordedAt"), "recordedAt")
    if abandoned_at - started_at < _MAX_INVESTIGATION_SESSION_AGE:
        raise ValueError(
            "Investigation cannot be abandoned before its one-hour session limit."
        )
    _require_clean_checkout(checkout)


def attach_latest_investigation_results(
    prepared: Mapping[str, Any],
    results: list[Mapping[str, Any]],
) -> dict[str, Any]:
    document = copy.deepcopy(dict(prepared))
    repository = prepared.get("repository")
    for issue in document.get("issues", []):
        if not isinstance(issue, dict):
            continue
        issue_number = issue.get("issueNumber")
        fingerprint = _source_evidence_fingerprint(issue)
        issue.pop("investigationResult", None)
        issue.pop("investigationResults", None)
        matching = [
            dict(candidate)
            for candidate in results
            if str(candidate.get("repository", "")).casefold()
            == str(repository).casefold()
            and candidate.get("issueNumber") == issue_number
            and candidate.get("sourceEvidenceFingerprint") == fingerprint
        ]
        if matching:
            latest_by_target: dict[str, dict[str, Any]] = {}
            for candidate in matching:
                target_key = json.dumps(
                    candidate.get("target"),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                latest_by_target[target_key] = candidate
            issue["investigationResults"] = [
                latest_by_target[key] for key in sorted(latest_by_target)
            ]
    return document


def render_investigation_section(plan: Mapping[str, Any]) -> str:
    requests = plan.get("requests", [])
    deferred = plan.get("deferredRequests", [])
    reused = plan.get("reusedInvestigationIds", [])
    active = plan.get("activeInvestigationIds", [])
    lines = ["## Bounded investigations", ""]
    if not isinstance(requests, list) or not requests:
        lines.append("No new investigation session is needed.")
    else:
        lines.extend(
            [
                f"**New investigations:** {len(requests)}",
                "",
                "| Issue | Target | Question |",
                "|---|---|---|",
            ]
        )
        for request in requests:
            if not isinstance(request, Mapping):
                continue
            target = request.get("target", {})
            target_text = (
                f"{target.get('kind')}:{target.get('value')}"
                if isinstance(target, Mapping)
                else "unknown"
            )
            question = str(request.get("question", "")).replace("|", "\\|")
            lines.append(
                f"| [#{request.get('issueNumber')}]({request.get('issueUrl')}) "
                f"| `{target_text}` | {question} |"
            )
    if isinstance(reused, list) and reused:
        lines.extend(
            [
                "",
                f"**Reused completed investigations:** {len(reused)}",
            ]
        )
    if isinstance(active, list) and active:
        lines.extend(
            [
                "",
                f"**Active investigation sessions:** {len(active)}",
            ]
        )
    if isinstance(deferred, list) and deferred:
        lines.extend(
            [
                "",
                f"**Deferred by the per-cycle budget:** {len(deferred)}",
            ]
        )
    return "\n".join(lines) + "\n"
