from __future__ import annotations

from datetime import UTC, datetime
import html
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Mapping

from .models import stable_json


MAX_LIVE_STATUS_BYTES = 64 * 1024
MAX_RECENT_EVENTS = 50
MAX_WORKERS = 32
STALE_AFTER_SECONDS = 30
_MAX_TEXT = 500


def build_live_status(
    invocation_dir: Path,
    work_dir: Path,
    *,
    state_dir: Path | None = None,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    invocation_dir = invocation_dir.expanduser().resolve(strict=True)
    work_dir = work_dir.expanduser().resolve(strict=True)
    invocation = _read_optional_json(invocation_dir / "invocation.json")
    progress = _read_optional_json(work_dir / "progress.json")
    cycle = _read_optional_json(work_dir / "cycle.json")
    assessment = _read_optional_json(work_dir / "assessment-batches.json")
    investigation_plan = _read_optional_json(work_dir / "investigation-plan.json")
    generated_at = (now or (lambda: datetime.now(UTC)))().astimezone(UTC)

    sessions_by_group = {
        session["groupId"]: session
        for session in invocation.get("sessions", [])
        if isinstance(session, Mapping)
        and isinstance(session.get("groupId"), str)
    }
    workers = []
    blockers = []
    completed_groups = 0
    groups = assessment.get("workerGroups", [])
    if not isinstance(groups, list):
        groups = []
    for group in groups:
        if not isinstance(group, Mapping) or not isinstance(group.get("groupId"), str):
            continue
        group_id = group["groupId"]
        response = _read_optional_json(work_dir / str(group.get("responseFile", "")))
        session = sessions_by_group.get(group_id)
        if group.get("status") != "ready":
            state = "blocked"
            basis = "artifact-observed"
            reason = _text(group.get("reason") or "assessment-input-incomplete")
            blockers.append({
                "id": group_id,
                "message": reason,
                "basis": basis,
            })
        elif response.get("status") == "complete":
            state = "complete"
            basis = "artifact-observed"
            reason = None
            completed_groups += 1
        elif isinstance(session, Mapping) and session.get("status") in {
            "running", "in_progress", "started",
        }:
            state = "running"
            basis = "launcher-observed"
            reason = None
        elif isinstance(session, Mapping):
            session_state = _text(session.get("status") or "awaiting-observation")
            state = (
                "awaiting-artifact"
                if session_state in {"complete", "completed"}
                else session_state
            )
            basis = "launcher-observed"
            reason = None
        else:
            state = "awaiting-observation"
            basis = "artifact-observed"
            reason = None
        workers.append({
            "id": group_id,
            "role": "assessment",
            "state": state,
            "basis": basis,
            "caseCount": len(group.get("caseIds", [])),
            "reason": reason,
        })
    repository = _text(
        invocation.get("repository") or cycle.get("repository") or "unknown"
    )
    resolved_state_dir = state_dir
    if resolved_state_dir is None and isinstance(cycle.get("stateDirectory"), str):
        resolved_state_dir = Path(cycle["stateDirectory"])
    investigation_workers = _investigation_workers(
        investigation_plan,
        invocation,
        repository=repository,
        state_dir=resolved_state_dir,
    )
    workers.extend(investigation_workers)
    assessment_group_ids = {worker["id"] for worker in workers}
    for index, session in enumerate(invocation.get("sessions", []), start=1):
        if not isinstance(session, Mapping):
            continue
        role = _text(session.get("role") or "worker")
        group_id = session.get("groupId")
        if role in {"coordinator", "investigator"} or group_id in assessment_group_ids:
            continue
        workers.append({
            "id": _text(
                session.get("investigationId")
                or session.get("sessionId")
                or f"{role}:{index}"
            ),
            "role": role,
            "state": _text(session.get("status") or "unknown"),
            "basis": "launcher-observed",
            "caseCount": 0,
            "reason": None,
        })
    if len(workers) > MAX_WORKERS:
        workers.sort(
            key=lambda worker: (
                worker["state"] in {"complete", "completed"},
                worker["id"],
            )
        )
        workers = workers[:MAX_WORKERS]

    phase = _phase(
        invocation_dir=invocation_dir,
        progress=progress,
        cycle=cycle,
        assessment=assessment,
        investigation_plan=investigation_plan,
        workers=workers,
        completed_groups=completed_groups,
        blockers=blockers,
    )
    events = _recent_events(progress, workers, cycle)
    artifacts = {
        name: str(path)
        for name, path in (
            ("json", invocation_dir / "live-status.json"),
            ("markdown", invocation_dir / "live-status.md"),
            ("html", invocation_dir / "live-status.html"),
            ("finalReport", invocation_dir / "final-operator-report.md"),
            ("retrospective", invocation_dir / "retrospective.md"),
        )
        if name in {"json", "markdown", "html"} or path.is_file()
    }
    status: dict[str, Any] = {
        "schemaVersion": 1,
        "advisoryOnly": True,
        "generatedAt": generated_at.isoformat().replace("+00:00", "Z"),
        "staleAfterSeconds": STALE_AFTER_SECONDS,
        "invocation": {
            "id": _text(invocation.get("runId") or invocation.get("invocationId") or invocation_dir.name),
            "repository": _text(
                repository
            ),
            "mode": _text(invocation.get("mode") or "unknown"),
        },
        "phase": phase,
        "assessment": (
            {
                "assessmentId": _text(assessment.get("assessmentId")),
                "caseCount": _integer(assessment.get("caseCount")),
                "completedGroups": completed_groups,
                "blockedGroups": sum(worker["state"] == "blocked" for worker in workers),
                "totalGroups": len(groups),
            }
            if assessment
            else None
        ),
        "workers": workers,
        "blockers": blockers[:20],
        "recentEvents": events[-MAX_RECENT_EVENTS:],
        "artifacts": artifacts,
    }
    status = _clip(status)
    while _largest_rendered_size(status) > MAX_LIVE_STATUS_BYTES:
        if status["recentEvents"]:
            status["recentEvents"].pop(0)
        elif status["workers"]:
            status["workers"].pop()
        elif status["blockers"]:
            status["blockers"].pop()
        else:
            raise ValueError("Live status identity exceeds the bounded document size.")
    _validate(status)
    return status


def render_live_status_markdown(status: Mapping[str, Any]) -> str:
    phase = status["phase"]
    invocation = status["invocation"]
    lines = [
        "# CI Shepherd live status",
        "",
        "> Advisory projection only. Canonical run artifacts remain authoritative.",
        "",
        f"**Repository:** {_markdown(invocation['repository'])}  ",
        f"**Mode:** {_markdown(invocation['mode'])}  ",
        f"**Updated:** {_markdown(status['generatedAt'])}  ",
        f"**Phase:** {_markdown(phase['id'])} ({_markdown(phase['state'])})",
        "",
        _markdown(phase["message"]),
    ]
    assessment = status.get("assessment")
    if isinstance(assessment, Mapping):
        lines.extend([
            "",
            "## Assessment",
            "",
            (
                f"{assessment['completedGroups']} of {assessment['totalGroups']} worker groups "
                f"complete; {assessment['blockedGroups']} blocked; "
                f"{assessment['caseCount']} logical cases."
            ),
        ])
    workers = status.get("workers", [])
    if workers:
        lines.extend(["", "## Workers", "", "| Group | State | Cases | Basis |", "|---|---:|---:|---|"])
        lines.extend(
            f"| {_markdown(worker['id'])} | {_markdown(worker['state'])} | "
            f"{worker['caseCount']} | {_markdown(worker['basis'])} |"
            for worker in workers
        )
    blockers = status.get("blockers", [])
    if blockers:
        lines.extend(["", "## Blockers", ""])
        lines.extend(
            f"- **{_markdown(blocker['id'])}:** {_markdown(blocker['message'])}"
            for blocker in blockers
        )
    events = status.get("recentEvents", [])
    if events:
        lines.extend(["", "## Recent events", ""])
        lines.extend(
            f"- `{_markdown(event['at'])}` **{_markdown(event['stage'])}** "
            f"{_markdown(event['status'])}: {_markdown(event['message'])}"
            for event in reversed(events)
        )
    return "\n".join(lines).rstrip() + "\n"


def render_live_status_html(status: Mapping[str, Any]) -> str:
    markdown = render_live_status_markdown(status)
    escaped = html.escape(markdown)
    state = html.escape(str(status["phase"]["state"]))
    generated_at = html.escape(str(status["generatedAt"]), quote=True)
    stale_after = int(status["staleAfterSeconds"])
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="5">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CI Shepherd live status</title>
  <style>
    body {{ margin: 2rem auto; max-width: 72rem; padding: 0 1rem; font: 16px/1.5 system-ui, sans-serif; color: #1f2328; }}
    header {{ display: flex; justify-content: space-between; align-items: baseline; }}
    .state {{ border-radius: 999px; padding: .25rem .75rem; background: #ddf4ff; font-weight: 600; }}
    pre {{ white-space: pre-wrap; overflow-wrap: anywhere; background: #f6f8fa; padding: 1rem; border-radius: .5rem; }}
  </style>
</head>
<body>
  <header><h1>CI Shepherd live status</h1><span class="state">{state}</span></header>
  <p id="freshness" data-generated-at="{generated_at}" data-stale-after="{stale_after}">Checking freshness...</p>
  <p>This page refreshes every five seconds. It is advisory; canonical run artifacts remain authoritative.</p>
  <pre>{escaped}</pre>
  <script>
    const freshness = document.getElementById("freshness");
    const ageSeconds = Math.max(0, (Date.now() - Date.parse(freshness.dataset.generatedAt)) / 1000);
    const staleAfter = Number(freshness.dataset.staleAfter);
    freshness.textContent = ageSeconds > staleAfter
      ? `Status is stale (${{Math.floor(ageSeconds)}} seconds old).`
      : `Status is fresh (${{Math.floor(ageSeconds)}} seconds old).`;
  </script>
</body>
</html>
"""


def write_live_status(output_dir: Path, status: Mapping[str, Any]) -> None:
    _validate(status)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_dir.chmod(0o700)
    _write_private(output_dir / "live-status.json", json.dumps(status, indent=2, sort_keys=True) + "\n")
    _write_private(output_dir / "live-status.md", render_live_status_markdown(status))
    _write_private(output_dir / "live-status.html", render_live_status_html(status))


def is_terminal(invocation_dir: Path) -> bool:
    return any(
        (invocation_dir / name).is_file()
        for name in ("retrospective.md", "retrospective-error.txt")
    )


def _investigation_workers(
    investigation_plan: Mapping[str, Any],
    invocation: Mapping[str, Any],
    *,
    repository: str,
    state_dir: Path | None,
) -> list[dict[str, Any]]:
    requests = investigation_plan.get("requests", [])
    if not isinstance(requests, list):
        return []
    latest_events: dict[str, Mapping[str, Any]] = {}
    if state_dir is not None:
        ledger = state_dir.expanduser().resolve(strict=False) / "ledgers" / "investigation-sessions.jsonl"
        for event in _read_jsonl(ledger):
            investigation_id = event.get("investigationId")
            if (
                isinstance(investigation_id, str)
                and str(event.get("repository", "")).casefold() == repository.casefold()
            ):
                latest_events[investigation_id] = event
    sessions = {
        session["investigationId"]: session
        for session in invocation.get("sessions", [])
        if isinstance(session, Mapping)
        and isinstance(session.get("investigationId"), str)
    }
    workers = []
    for request in requests:
        if not isinstance(request, Mapping) or not isinstance(request.get("investigationId"), str):
            continue
        investigation_id = request["investigationId"]
        event = latest_events.get(investigation_id)
        session = sessions.get(investigation_id)
        if event is not None:
            state = _text(event.get("status") or "unknown")
            basis = "ledger-recorded"
        elif session is not None:
            state = _text(session.get("status") or "unknown")
            basis = "launcher-observed"
        else:
            state = "planned"
            basis = "artifact-observed"
        workers.append({
            "id": investigation_id,
            "role": "investigator",
            "state": state,
            "basis": basis,
            "caseCount": 1,
            "reason": (
                "Investigation did not complete."
                if state in {"failed", "abandoned"}
                else None
            ),
        })
    return workers


def _phase(
    *,
    invocation_dir: Path,
    progress: Mapping[str, Any],
    cycle: Mapping[str, Any],
    assessment: Mapping[str, Any],
    investigation_plan: Mapping[str, Any],
    workers: list[dict[str, Any]],
    completed_groups: int,
    blockers: list[dict[str, Any]],
) -> dict[str, Any]:
    if (invocation_dir / "retrospective.md").is_file():
        return _phase_record("complete", "complete", "Run and retrospective completed.")
    if (invocation_dir / "retrospective-error.txt").is_file():
        return _phase_record("retrospective", "failed", "Retrospective failed.")
    if (invocation_dir / "final-operator-report.md").is_file():
        return _phase_record("reporting", "running", "Final report is available; retrospective is pending.")
    if cycle.get("stage") == "completed":
        investigation_workers = [
            worker for worker in workers if worker["role"] == "investigator"
        ]
        request_count = _integer(
            investigation_plan.get("investigationRequestCount")
            if "investigationRequestCount" in investigation_plan
            else len(investigation_plan.get("requests", []))
        )
        if request_count and (
            not investigation_workers
            or any(
                worker["state"] not in {"complete", "completed", "failed"}
                for worker in investigation_workers
            )
        ):
            return _phase_record(
                "investigation", "running",
                f"Read-only investigations are in progress for {request_count} request(s).",
            )
        return _phase_record(
            "post-assessment", "running",
            "Cycle completed; investigation, action processing, or final reporting is pending.",
        )
    if cycle.get("stage") == "awaiting-review":
        total_groups = len(assessment.get("workerGroups", []))
        if blockers:
            return _phase_record(
                "assessment", "blocked",
                f"Assessment is blocked in {len(blockers)} worker group(s).",
                completed_groups, total_groups,
            )
        return _phase_record(
            "assessment", "running",
            f"Assessment has completed {completed_groups} of {total_groups} worker groups.",
            completed_groups, total_groups,
        )
    if progress:
        state = "failed" if progress.get("status") == "failed" else (
            "complete" if progress.get("status") == "complete" else "running"
        )
        return _phase_record(
            "collection",
            state,
            _text(progress.get("message") or f"Collection stage: {progress.get('currentStage', 'starting')}."),
            _integer(progress.get("completedItems"), allow_none=True),
            _integer(progress.get("totalItems"), allow_none=True),
        )
    return _phase_record("initialized", "running", "Waiting for collection to start.")


def _phase_record(
    phase_id: str,
    state: str,
    message: str,
    completed_items: int | None = None,
    total_items: int | None = None,
) -> dict[str, Any]:
    return {
        "id": phase_id,
        "state": state,
        "message": _text(message),
        "completedItems": completed_items,
        "totalItems": total_items,
    }


def _recent_events(
    progress: Mapping[str, Any],
    workers: list[dict[str, Any]],
    cycle: Mapping[str, Any],
) -> list[dict[str, str]]:
    events = []
    for event in progress.get("events", []):
        if not isinstance(event, Mapping):
            continue
        events.append({
            "at": _text(event.get("at") or event.get("timestamp") or "unknown"),
            "stage": _text(event.get("stage") or "collection"),
            "status": _text(event.get("status") or "progress"),
            "message": _text(event.get("message") or ""),
            "basis": "artifact-observed",
        })
    for worker in workers:
        if worker["state"] in {"complete", "blocked", "running"}:
            events.append({
                "at": "current",
                "stage": worker["id"],
                "status": worker["state"],
                "message": (
                    worker["reason"]
                    if worker["reason"] is not None
                    else f"Assessment worker group is {worker['state']}."
                ),
                "basis": worker["basis"],
            })
    if cycle.get("stage") == "completed":
        events.append({
            "at": _text(cycle.get("completedAt") or "current"),
            "stage": "cycle",
            "status": "completed",
            "message": "Cycle finalization completed.",
            "basis": "artifact-observed",
        })
    return events


def _read_optional_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    return rows


def _text(value: object) -> str:
    return str(value)[:_MAX_TEXT]


def _integer(value: object, *, allow_none: bool = False) -> int | None:
    if allow_none and value is None:
        return None
    return value if type(value) is int and value >= 0 else 0


def _clip(value: Any) -> Any:
    if isinstance(value, str):
        return value[:_MAX_TEXT]
    if isinstance(value, list):
        return [_clip(item) for item in value]
    if isinstance(value, dict):
        return {key: _clip(item) for key, item in value.items()}
    return value


def _markdown(value: object) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("<", "\\<")
        .replace(">", "\\>")
        .replace("|", "\\|")
    )


def _validate(status: Mapping[str, Any]) -> None:
    required = {
        "schemaVersion", "advisoryOnly", "generatedAt", "staleAfterSeconds",
        "invocation", "phase", "assessment", "workers", "blockers",
        "recentEvents", "artifacts",
    }
    if set(status) != required or status.get("schemaVersion") != 1 or status.get("advisoryOnly") is not True:
        raise ValueError("Live status must use the advisory version 1 schema.")
    if not isinstance(status.get("workers"), list) or len(status["workers"]) > MAX_WORKERS:
        raise ValueError("Live status workers exceed the bounded schema.")
    if not isinstance(status.get("recentEvents"), list) or len(status["recentEvents"]) > MAX_RECENT_EVENTS:
        raise ValueError("Live status events exceed the bounded schema.")
    if _largest_rendered_size(status) > MAX_LIVE_STATUS_BYTES:
        raise ValueError("Live status artifacts exceed the bounded document size.")


def _largest_rendered_size(status: Mapping[str, Any]) -> int:
    return max(
        len(content.encode("utf-8"))
        for content in (
            json.dumps(status, indent=2, sort_keys=True) + "\n",
            render_live_status_markdown(status),
            render_live_status_html(status),
        )
    )


def _write_private(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
