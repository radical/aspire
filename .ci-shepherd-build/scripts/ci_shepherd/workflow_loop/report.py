from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path
import json
import sqlite3

from .state import WorkflowLoopStore
from .shadow import read_shadow_metadata


def render_status(
    state_directory: Path,
    *,
    repository: str,
    branch: str,
    now: datetime,
    workflow_ids: Collection[int] | None = None,
    capacity_limit: int = 2,
) -> str:
    if not state_directory.is_absolute():
        raise ValueError("state_directory must be absolute.")
    if capacity_limit < 1:
        raise ValueError("capacity_limit must be positive.")
    database = state_directory / "workflow-loop.sqlite3"
    if not database.is_file() or database.is_symlink():
        raise ValueError("Workflow loop state database is unavailable.")
    _validate_scope(database, repository, branch)
    store = WorkflowLoopStore(
        state_directory,
        repository=repository,
        branch=branch,
    )
    store.initialize(workflow_ids=workflow_ids)
    items = store.list_items()
    active_ids = store.active_item_ids()
    latest_pass = _latest_pass(database)
    shadow = read_shadow_metadata(state_directory)
    first_assignment = min(
        (
            _parse_time(item.assignment_confirmed_at)
            - _parse_time(item.first_failure_seen_at)
            for item in items
            if item.assignment_confirmed_at is not None
        ),
        default=None,
    )

    lines = [
        f"CI shepherd: {_safe(repository)} branch={_safe(branch)}",
        f"Capacity: {len(active_ids)}/{capacity_limit} active",
        _pass_line(latest_pass),
        (
            "Time to first confirmed assignment: unavailable"
            if first_assignment is None
            else "Time to first confirmed assignment: "
            f"{_duration(first_assignment.total_seconds())}"
        ),
    ]
    if shadow is not None:
        lines.extend((
            f"Canonical source state: {_safe(shadow['canonical_state_directory'])}",
            f"Read-only shadow state: {_safe(state_directory)}",
            "Inherited operations are frozen, not owned or resumed by this run.",
        ))
    for item in items:
        latest_would_do = next(
            (
                entry
                for entry in store.recent_history(item.id)
                if entry.event == "would-do"
            ),
            None,
        )
        elapsed = max(
            0.0,
            (
                now.astimezone(UTC)
                - _parse_time(item.last_progressed_at)
            ).total_seconds(),
        )
        activity = "active" if item.id in active_ids else "idle"
        lines.extend(
            (
                "",
                f"Item {item.id}: {item.phase.value} "
                f"activity={activity} elapsed={_duration(elapsed)}",
                f"  waiting: {_safe(item.wait_reason or 'none')}",
                f"  checked: {item.last_checked_at}",
                f"  progressed: {item.last_progressed_at}",
                "  run: "
                f"https://github.com/{_safe(item.repository)}/actions/runs/"
                f"{item.failure_run_id}",
                (
                    "  issue: none"
                    if item.issue_number is None
                    else "  issue: "
                    f"https://github.com/{_safe(item.repository)}/issues/"
                    f"{item.issue_number}"
                ),
                (
                    "  pull request: none"
                    if item.pull_request_number is None
                    else "  pull request: "
                    f"https://github.com/{_safe(item.repository)}/pull/"
                    f"{item.pull_request_number}"
                ),
                (
                    "  task ID: none"
                    if item.task_id is None
                    else f"  task ID: {_safe(item.task_id)}"
                ),
                (
                    "  latest action: none"
                    if item.latest_action is None
                    else f"  latest action: {item.latest_action.value}"
                ),
                (
                    "  would do: none"
                    if latest_would_do is None
                    else f"  would do: "
                    f"{_safe(latest_would_do.detail.get('effect', 'unknown'))}"
                ),
                f"  error: {_safe(item.latest_error or 'none')}",
            )
        )
        if shadow is not None and item.id in shadow["frozen_item_ids"]:
            lines.append(
                "  FROZEN: " + _safe(
                    json.dumps(shadow["frozen_reasons"][str(item.id)], ensure_ascii=True)
                )
            )
    for proposal in store.list_proposals():
        detail = proposal.detail
        payload = detail["payload"]
        request = payload.get("request", {})
        lines.extend(
            (
                "",
                f"PROPOSED {_safe(detail['kind'])} "
                f"item={proposal.item_id} action={_safe(detail['actionId'])}",
                "  Would-Do only; not authorized or executed.",
                f"  episode={detail['episode']} "
                f"evidence={_safe(request.get('evidenceFingerprint', 'unavailable'))}",
                json.dumps(payload.get("write", payload), indent=2, ensure_ascii=True),
            )
        )
    return "\n".join(lines)


def _validate_scope(database: Path, repository: str, branch: str) -> None:
    connection = sqlite3.connect(database)
    try:
        meta = dict(connection.execute("SELECT key, value FROM meta"))
    finally:
        connection.close()
    if meta.get("repository") != repository or meta.get("branch") != branch:
        raise ValueError(
            "Configured repository/branch does not match persisted state scope."
        )


def _latest_pass(database: Path) -> sqlite3.Row | None:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            "SELECT pass_id, duration_ms, github_request_count, "
            "discovered_items, progressed_items, confirmed_assignments, error "
            "FROM passes WHERE completed_at IS NOT NULL "
            "ORDER BY completed_at DESC, pass_id DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()


def _pass_line(row: sqlite3.Row | None) -> str:
    if row is None:
        return "Last pass: unavailable"
    status = "ok" if row["error"] is None else f"error={_safe(row['error'])}"
    return (
        f"Last pass: {_safe(row['pass_id'])} "
        f"duration={row['duration_ms'] / 1000:.3f}s "
        f"github_requests={row['github_request_count']} "
        f"discovered={row['discovered_items']} "
        f"progressed={row['progressed_items']} "
        f"assignments={row['confirmed_assignments']} status={status}"
    )


def _safe(value: object) -> str:
    text = str(value)
    return "".join(
        (
            character
            if ord(character) >= 0x20 and ord(character) != 0x7F
            else f"\\x{ord(character):02x}"
        )
        for character in text
    )


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _duration(seconds: float) -> str:
    total = int(seconds)
    minutes, second = divmod(total, 60)
    hours, minute = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minute:02d}m{second:02d}s"
    return f"{minute}m{second:02d}s"
