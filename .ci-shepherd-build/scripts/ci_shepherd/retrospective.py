from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping

from .execution_state import ActionEventStore
from .jsonl import read_jsonl_rows


RETROSPECTIVE_EVIDENCE_FILES = (
    "action-proposals.json",
    "actor-dry-run.json",
    "api-calls.jsonl",
    "cycle.json",
    "investigation-plan.json",
    "progress.json",
    "quarantine-session.json",
    "report.md",
    "run-completion.json",
)
_SEVERITIES = frozenset({"high", "medium", "low"})
_CATEGORIES = frozenset(
    {
        "correctness",
        "efficiency",
        "observability",
        "process",
        "reliability",
    }
)


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Unable to read {label}: {error}") from error
    if not isinstance(document, dict):
        raise ValueError(f"{label.capitalize()} must be an object.")
    return document


def _require_matching_identity(
    document: Mapping[str, Any],
    *,
    repository: str,
    snapshot_id: str,
    label: str,
) -> None:
    if (
        document.get("repository") != repository
        or document.get("snapshotId") != snapshot_id
    ):
        raise ValueError(f"{label} identity must match the completed cycle.")


def retrospective_evidence_paths(work_dir: Path) -> tuple[Path, ...]:
    return tuple(work_dir / name for name in RETROSPECTIVE_EVIDENCE_FILES)


def build_run_completion(
    work_dir: Path,
    state_dir: Path,
    *,
    sealed_at: str,
) -> dict[str, object]:
    work_dir = work_dir.expanduser().resolve(strict=True)
    state_dir = state_dir.expanduser().resolve(strict=True)
    if not work_dir.is_dir() or work_dir.is_symlink():
        raise ValueError("Retrospective work directory must be a real directory.")
    if not state_dir.is_dir() or state_dir.is_symlink():
        raise ValueError("Retrospective state directory must be a real directory.")
    if not sealed_at:
        raise ValueError("Run completion sealedAt must be nonempty.")

    manifest = _load_object(work_dir / "cycle.json", "cycle manifest")
    if manifest.get("stage") != "completed":
        raise ValueError("Cycle must be completed before post-action reconciliation.")
    repository = manifest.get("repository")
    snapshot_id = manifest.get("snapshotId")
    if not isinstance(repository, str) or not repository:
        raise ValueError("Cycle manifest repository must be nonempty.")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise ValueError("Cycle manifest snapshotId must be nonempty.")
    if not (work_dir / "report.md").is_file():
        raise ValueError("Completed cycle must contain report.md.")

    proposals = _load_object(
        work_dir / "action-proposals.json",
        "action proposals",
    )
    _require_matching_identity(
        proposals,
        repository=repository,
        snapshot_id=snapshot_id,
        label="Action proposals",
    )
    raw_proposals = proposals.get("proposals")
    if not isinstance(raw_proposals, list):
        raise ValueError("Action proposals must contain a proposals array.")
    action_ids = {
        proposal.get("actionId")
        for proposal in raw_proposals
        if isinstance(proposal, Mapping)
        and isinstance(proposal.get("actionId"), str)
        and proposal.get("actionId")
    }
    if len(action_ids) != len(raw_proposals):
        raise ValueError("Every action proposal must have a unique actionId.")

    events_path = state_dir / "action-events.jsonl"
    action_events = (
        ActionEventStore(state_dir).events(repository=repository)
        if events_path.is_file()
        else []
    )
    action_results_document = (
        {
            "schemaVersion": 1,
            "repository": repository,
            "results": [
                {
                    key: value
                    for key, value in event.items()
                    if key
                    not in {
                        "schemaVersion",
                        "eventType",
                        "recordedAt",
                        "grantId",
                        "repository",
                        "snapshotId",
                    }
                }
                for event in action_events
                if event.get("eventType") == "terminal"
            ],
        }
        if action_events
        else _load_object(
            state_dir / "action-results.json",
            "legacy action results",
        )
        if (state_dir / "action-results.json").is_file()
        else {
            "schemaVersion": 1,
            "repository": repository,
            "results": [],
        }
    )
    if action_results_document.get("repository") != repository:
        raise ValueError("Action results repository must match the completed cycle.")
    raw_action_results = action_results_document.get("results")
    if not isinstance(raw_action_results, list) or not all(
        isinstance(result, Mapping) for result in raw_action_results
    ):
        raise ValueError("Action results must contain an array of objects.")
    action_results = sorted(
        (
            dict(result)
            for result in raw_action_results
            if result.get("actionId") in action_ids
        ),
        key=lambda result: (
            str(result.get("actionId")),
            str(result.get("attemptedAt", "")),
        ),
    )
    recorded_action_ids = {result.get("actionId") for result in action_results}
    scoped_action_events = sorted(
        (
            event
            for event in action_events
            if event.get("actionId") in action_ids
        ),
        key=lambda event: (
            str(event.get("actionId")),
            str(event.get("recordedAt", "")),
        ),
    )
    intent_action_ids = {
        event.get("actionId")
        for event in scoped_action_events
        if event.get("eventType") == "intent"
    }
    terminal_action_ids = {
        event.get("actionId")
        for event in scoped_action_events
        if event.get("eventType") == "terminal"
    }

    investigation_plan = _load_object(
        work_dir / "investigation-plan.json",
        "investigation plan",
    )
    _require_matching_identity(
        investigation_plan,
        repository=repository,
        snapshot_id=snapshot_id,
        label="Investigation plan",
    )
    requests = investigation_plan.get("requests")
    if not isinstance(requests, list):
        raise ValueError("Investigation plan must contain a requests array.")
    investigation_ids = {
        request.get("investigationId")
        for request in requests
        if isinstance(request, Mapping)
        and isinstance(request.get("investigationId"), str)
        and request.get("investigationId")
    }
    if len(investigation_ids) != len(requests):
        raise ValueError(
            "Every investigation request must have a unique investigationId."
        )
    investigation_rows = read_jsonl_rows(
        state_dir / "ledgers" / "investigation-results.jsonl"
    )
    investigation_session_rows = read_jsonl_rows(
        state_dir / "ledgers" / "investigation-sessions.jsonl"
    )
    investigation_results = sorted(
        (
            row
            for row in investigation_rows
            if row.get("investigationId") in investigation_ids
        ),
        key=lambda row: str(row.get("investigationId")),
    )
    completed_investigation_ids = {
        row.get("investigationId") for row in investigation_results
    }
    investigation_session_events = [
        row
        for row in investigation_session_rows
        if row.get("investigationId") in investigation_ids
    ]
    quarantine_plan = _load_object(
        work_dir / "quarantine-session.json",
        "quarantine session plan",
    )
    _require_matching_identity(
        quarantine_plan,
        repository=repository,
        snapshot_id=snapshot_id,
        label="Quarantine session plan",
    )
    quarantine_batch_ids = {
        value
        for value in (
            quarantine_plan.get("activeBatchId"),
            *quarantine_plan.get("openBatchIds", []),
            (
                quarantine_plan["proposal"].get("batchId")
                if isinstance(quarantine_plan.get("proposal"), Mapping)
                else None
            ),
        )
        if isinstance(value, str) and value
    }
    quarantine_rows = read_jsonl_rows(
        state_dir / "ledgers" / "quarantine-sessions.jsonl"
    )
    quarantine_events = [
        row
        for row in quarantine_rows
        if (
            row.get("batchId") in quarantine_batch_ids
            or row.get("snapshotId") == snapshot_id
        )
        and str(row.get("repository", "")).casefold() == repository.casefold()
    ]
    recorded_quarantine_batch_ids = {
        row.get("batchId")
        for row in quarantine_events
        if isinstance(row.get("batchId"), str)
    }

    return {
        "schemaVersion": 1,
        "repository": repository,
        "snapshotId": snapshot_id,
        "sealedAt": sealed_at,
        "actionEvents": scoped_action_events,
        "actionResults": action_results,
        "interruptedActionIds": sorted(intent_action_ids - terminal_action_ids),
        "unrecordedActionIds": sorted(action_ids - recorded_action_ids),
        "investigationResults": investigation_results,
        "investigationSessionEvents": investigation_session_events,
        "missingInvestigationIds": sorted(
            investigation_ids - completed_investigation_ids
        ),
        "quarantineSessionEvents": quarantine_events,
        "unrecordedQuarantineBatchIds": sorted(
            quarantine_batch_ids - recorded_quarantine_batch_ids
        ),
    }


def _worker_prompt(request: Mapping[str, Any], work_dir: Path) -> str:
    evidence_paths = "\n".join(
        f"- {path}" for path in request["evidencePaths"]
    )
    return (
        f"Review the completed CI shepherd run for {request['repository']} as a "
        "fresh, read-only reviewer.\n\n"
        f"Reviewed session: {request['reviewedSessionId']}\n"
        f"Snapshot: {request['snapshotId']}\n"
        f"Run artifacts directory: {work_dir}\n"
        "Read only these run artifacts:\n"
        f"{evidence_paths}\n\n"
        "Identify concrete correctness, reliability, efficiency, observability, "
        "or process problems encountered during this run. Also identify safeguards "
        "that demonstrably worked and conditions worth watching in future runs. "
        "Distinguish observed problems from speculative risks, and cite only the "
        "listed evidence paths.\n\n"
        "Do not access GitHub or run gh. Do not edit code, mutate state, post "
        "comments, close issues, assign actors, or start implementation work. Do not modify "
        "the shepherd automatically; recommendations require later review.\n\n"
        "Return only JSON with this shape:\n"
        "{\n"
        '  "schemaVersion": 1,\n'
        f'  "repository": "{request["repository"]}",\n'
        f'  "snapshotId": "{request["snapshotId"]}",\n'
        f'  "reviewedSessionId": "{request["reviewedSessionId"]}",\n'
        '  "summary": "short evidence-backed assessment",\n'
        '  "observations": [{\n'
        '    "severity": "high | medium | low",\n'
        '    "category": "correctness | reliability | efficiency | '
        'observability | process",\n'
        '    "title": "short finding",\n'
        '    "detail": "what happened and why it matters",\n'
        '    "recommendation": "specific improvement",\n'
        '    "evidencePaths": ["listed artifact path"]\n'
        "  }],\n"
        '  "watchItems": [{\n'
        '    "condition": "concrete future signal",\n'
        '    "reason": "why the signal matters",\n'
        '    "evidencePaths": ["listed artifact path"]\n'
        "  }],\n"
        '  "successfulSafeguards": [{\n'
        '    "title": "safeguard that worked",\n'
        '    "detail": "observable protection provided",\n'
        '    "evidencePaths": ["listed artifact path"]\n'
        "  }]\n"
        "}\n"
    )


def build_retrospective_request(
    work_dir: Path,
    *,
    reviewed_session_id: str,
) -> dict[str, object]:
    work_dir = work_dir.expanduser().resolve(strict=True)
    if not work_dir.is_dir() or work_dir.is_symlink():
        raise ValueError("Retrospective work directory must be a real directory.")
    if not reviewed_session_id:
        raise ValueError("Reviewed session ID must be nonempty.")

    manifest = _load_object(work_dir / "cycle.json", "cycle manifest")
    if manifest.get("stage") != "completed":
        raise ValueError("Cycle must be completed before retrospective review.")
    repository = manifest.get("repository")
    snapshot_id = manifest.get("snapshotId")
    if not isinstance(repository, str) or not repository:
        raise ValueError("Cycle manifest repository must be nonempty.")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise ValueError("Cycle manifest snapshotId must be nonempty.")
    if not (work_dir / "report.md").is_file():
        raise ValueError("Completed cycle must contain report.md.")
    completion_path = work_dir / "run-completion.json"
    if not completion_path.is_file() or completion_path.is_symlink():
        raise ValueError(
            "Cycle must complete post-action reconciliation before retrospective review."
        )
    completion = _load_object(completion_path, "run completion")
    if (
        completion.get("schemaVersion") != 1
        or completion.get("repository") != repository
        or completion.get("snapshotId") != snapshot_id
    ):
        raise ValueError("Run completion identity must match the completed cycle.")
    for field in (
        "actionResults",
        "investigationResults",
        "missingInvestigationIds",
        "unrecordedActionIds",
    ):
        if not isinstance(completion.get(field), list):
            raise ValueError(f"Run completion {field} must be an array.")
    sealed_at = completion.get("sealedAt")
    if not isinstance(sealed_at, str) or not sealed_at:
        raise ValueError("Run completion sealedAt must be nonempty.")

    evidence_paths = sorted(
        name
        for name in RETROSPECTIVE_EVIDENCE_FILES
        if (work_dir / name).is_file() and not (work_dir / name).is_symlink()
    )
    request: dict[str, object] = {
        "schemaVersion": 1,
        "repository": repository,
        "snapshotId": snapshot_id,
        "reviewedSessionId": reviewed_session_id,
        "evidencePaths": evidence_paths,
    }
    request["workerPrompt"] = _worker_prompt(request, work_dir)
    return request


def _require_string(entry: Mapping[str, Any], field: str, label: str) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label}.{field} must be a nonempty string.")
    return value


def _normalize_evidence_paths(
    entry: Mapping[str, Any],
    *,
    allowed_paths: set[str],
    label: str,
) -> list[str]:
    paths = entry.get("evidencePaths")
    if not isinstance(paths, list) or not paths or not all(
        isinstance(path, str) and path for path in paths
    ):
        raise ValueError(f"{label}.evidencePaths must contain strings.")
    normalized = sorted(set(paths))
    if not set(normalized).issubset(allowed_paths):
        raise ValueError(
            f"{label} cites evidence outside the retrospective request."
        )
    return normalized


def _require_entries(
    result: Mapping[str, Any],
    field: str,
    *,
    limit: int,
) -> list[Mapping[str, Any]]:
    entries = result.get(field)
    if not isinstance(entries, list) or len(entries) > limit or not all(
        isinstance(entry, Mapping) for entry in entries
    ):
        raise ValueError(f"{field} must contain at most {limit} objects.")
    return entries


def normalize_retrospective_result(
    request: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, object]:
    if request.get("schemaVersion") != 1:
        raise ValueError("Retrospective request schemaVersion must be 1.")
    if result.get("schemaVersion") != 1:
        raise ValueError("Retrospective result schemaVersion must be 1.")
    identity: dict[str, str] = {}
    for field in ("repository", "snapshotId", "reviewedSessionId"):
        value = request.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"Retrospective request {field} must be a nonempty string."
            )
        if result.get(field) != value:
            raise ValueError(
                f"Retrospective result identity field {field} must match its request."
            )
        identity[field] = value
    evidence_paths = request.get("evidencePaths")
    if not isinstance(evidence_paths, list) or not all(
        isinstance(path, str) and path for path in evidence_paths
    ):
        raise ValueError("Retrospective request evidencePaths must contain strings.")
    allowed_paths = set(evidence_paths)
    summary = _require_string(result, "summary", "retrospective")

    observations: list[dict[str, object]] = []
    for index, entry in enumerate(
        _require_entries(result, "observations", limit=20)
    ):
        label = f"observations[{index}]"
        severity = _require_string(entry, "severity", label)
        category = _require_string(entry, "category", label)
        if severity not in _SEVERITIES:
            raise ValueError(f"{label}.severity is unsupported.")
        if category not in _CATEGORIES:
            raise ValueError(f"{label}.category is unsupported.")
        observations.append(
            {
                "severity": severity,
                "category": category,
                "title": _require_string(entry, "title", label),
                "detail": _require_string(entry, "detail", label),
                "recommendation": _require_string(
                    entry,
                    "recommendation",
                    label,
                ),
                "evidencePaths": _normalize_evidence_paths(
                    entry,
                    allowed_paths=allowed_paths,
                    label=label,
                ),
            }
        )

    watch_items: list[dict[str, object]] = []
    for index, entry in enumerate(
        _require_entries(result, "watchItems", limit=10)
    ):
        label = f"watchItems[{index}]"
        watch_items.append(
            {
                "condition": _require_string(entry, "condition", label),
                "reason": _require_string(entry, "reason", label),
                "evidencePaths": _normalize_evidence_paths(
                    entry,
                    allowed_paths=allowed_paths,
                    label=label,
                ),
            }
        )

    safeguards: list[dict[str, object]] = []
    for index, entry in enumerate(
        _require_entries(result, "successfulSafeguards", limit=10)
    ):
        label = f"successfulSafeguards[{index}]"
        safeguards.append(
            {
                "title": _require_string(entry, "title", label),
                "detail": _require_string(entry, "detail", label),
                "evidencePaths": _normalize_evidence_paths(
                    entry,
                    allowed_paths=allowed_paths,
                    label=label,
                ),
            }
        )

    return {
        "schemaVersion": 1,
        **identity,
        "summary": summary,
        "observations": observations,
        "watchItems": watch_items,
        "successfulSafeguards": safeguards,
    }


def _evidence_suffix(paths: object) -> str:
    assert isinstance(paths, list)
    return ", ".join(f"`{path}`" for path in paths)


def render_retrospective_markdown(
    request: Mapping[str, Any],
    result: Mapping[str, Any],
) -> str:
    normalized = copy.deepcopy(dict(result))
    lines = [
        "# CI Shepherd Run Retrospective",
        "",
        f"**Repository:** `{request['repository']}`  ",
        f"**Snapshot:** `{request['snapshotId']}`  ",
        f"**Reviewed session:** `{request['reviewedSessionId']}`",
        "",
        str(normalized["summary"]),
        "",
        "## Improvement findings",
        "",
    ]
    observations = normalized["observations"]
    assert isinstance(observations, list)
    if observations:
        for entry in observations:
            assert isinstance(entry, Mapping)
            lines.extend(
                [
                    f"### [{str(entry['severity']).upper()}] {entry['title']}",
                    "",
                    f"**Category:** `{entry['category']}`  ",
                    f"**Evidence:** {_evidence_suffix(entry['evidencePaths'])}",
                    "",
                    str(entry["detail"]),
                    "",
                    f"**Recommendation:** {entry['recommendation']}",
                    "",
                ]
            )
    else:
        lines.extend(["No improvement findings were supported by this run.", ""])

    lines.extend(["## Watch items", ""])
    watch_items = normalized["watchItems"]
    assert isinstance(watch_items, list)
    if watch_items:
        for entry in watch_items:
            assert isinstance(entry, Mapping)
            lines.extend(
                [
                    f"- **{entry['condition']}** {entry['reason']} "
                    f"({_evidence_suffix(entry['evidencePaths'])})"
                ]
            )
    else:
        lines.append("No watch items were identified.")

    lines.extend(["", "## Safeguards that worked", ""])
    safeguards = normalized["successfulSafeguards"]
    assert isinstance(safeguards, list)
    if safeguards:
        for entry in safeguards:
            assert isinstance(entry, Mapping)
            lines.extend(
                [
                    f"- **{entry['title']}** {entry['detail']} "
                    f"({_evidence_suffix(entry['evidencePaths'])})"
                ]
            )
    else:
        lines.append("No successful safeguards were identified.")

    return "\n".join(lines).rstrip() + "\n"
