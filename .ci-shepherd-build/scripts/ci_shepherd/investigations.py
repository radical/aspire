from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping

from .jsonl import append_jsonl_rows, exclusive_jsonl_lock, read_jsonl_rows


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
    return (
        f"Investigate {request['issueUrl']} for the CI shepherd.\n\n"
        "Use the issue-investigation skill and any more specific CI/test skill it "
        "routes to. This is a read-only, issue-focused investigation: do not edit "
        "code, post comments, assign anyone, or open a pull request.\n\n"
        f"Target: {request['target']['kind']}:{request['target']['value']}\n"
        f"Question: {request['question']}\n"
        f"Evidence already checked: {', '.join(request['evidenceIds'])}\n"
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
    requests: list[dict[str, object]] = []
    reused: list[str] = []
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
                "missingEvidence": list(missing_evidence),
                "stopCondition": str(recommendation.get("reassessWhen") or ""),
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
    deferred = [
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
    return {
        "schemaVersion": 1,
        "repository": repository,
        "snapshotId": snapshot_id,
        "requests": requests,
        "deferredRequests": deferred,
        "maxRequests": max_requests,
        "reusedInvestigationIds": reused,
    }


def _results_path(state_directory: Path) -> Path:
    return state_directory / "ledgers" / "investigation-results.jsonl"


def read_investigation_results(
    state_directory: Path,
) -> list[dict[str, Any]]:
    return read_jsonl_rows(_results_path(state_directory))


def record_investigation_result(
    state_directory: Path,
    request: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    recorded_at: str,
    session_id: str,
) -> dict[str, object]:
    outcome = result.get("outcome")
    if outcome not in _OUTCOMES:
        raise ValueError(f"Unsupported investigation outcome: {outcome}")
    investigation_id = request.get("investigationId")
    if not isinstance(investigation_id, str) or not investigation_id:
        raise ValueError("Investigation request has no investigationId.")
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
        "repository": request.get("repository"),
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

    path = _results_path(state_directory)
    with exclusive_jsonl_lock(path):
        if any(
            row.get("investigationId") == investigation_id
            for row in read_jsonl_rows(path)
        ):
            raise ValueError(f"Investigation {investigation_id} is already recorded.")
        append_jsonl_rows(path, [event])
    return event


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
    if isinstance(deferred, list) and deferred:
        lines.extend(
            [
                "",
                f"**Deferred by the per-cycle budget:** {len(deferred)}",
            ]
        )
    return "\n".join(lines) + "\n"
