from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path
import json
import sqlite3

from .models import (
    FailureClassification,
    ItemPhase,
    RecommendedResponse,
    TaskState,
    WorkflowItem,
)
from .scenarios.workflow_policy import workflow_priority
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
    lines.extend(("", *_workflow_health(store), ""))
    classifications = {
        item.id: _classification(store, item)
        for item in items
    }
    lines.extend(
        _cause_and_leaf_status(
            store,
            items,
            classifications,
            _task_ranks(items, classifications),
        )
    )
    migration = store.leaf_migration_counts()
    lines.append("")
    if migration is None:
        lines.append("Legacy migration: not applicable (no migration receipt)")
    else:
        lines.append(
            "Legacy migration: "
            f"items={migration['items']} workers={migration['workers']} "
            f"actions={migration['actions']} ownership=not-transferred"
        )
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
        if (
            item.task_id is not None
            and item.pull_request_number is None
            and item.task_state in {
                TaskState.COMPLETED,
                TaskState.FAILED,
                TaskState.IDLE,
                TaskState.TIMED_OUT,
                TaskState.CANCELLED,
            }
        ):
            lines.append(
                "  task result: unavailable "
                "(no authoritative pull request/result channel)"
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


def _workflow_health(store: WorkflowLoopStore) -> list[str]:
    manifests = store.list_manifest_observations()
    if not manifests:
        return ["Workflow health: unavailable (no persisted manifests)"]
    lines = ["Workflow health:"]
    for manifest in manifests:
        run = manifest.get("run")
        source = manifest.get("source")
        current = run if isinstance(run, dict) else source
        current = current if isinstance(current, dict) else {}
        jobs = manifest.get("jobs")
        jobs = jobs if isinstance(jobs, list) else []
        roles = manifest.get("job_roles")
        roles = roles if isinstance(roles, dict) else {}
        role_counts = {
            role: sum(1 for value in roles.values() if value == role)
            for role in ("leaf", "ambiguous_leaf", "aggregate")
        }
        total = manifest.get("total_count")
        total_text = str(total) if isinstance(total, int) else "unavailable"
        workflow = _safe(current.get("workflowName", "unknown"))
        workflow_id = _nested_value(current, "key", "workflowId")
        run_id = current.get("runId", "unknown")
        attempt = current.get("attempt", "unknown")
        conclusion = _safe(current.get("conclusion", "unknown"))
        inventory = (
            "complete"
            if manifest.get("complete") is True
            and manifest.get("read_status") == "complete"
            else "incomplete"
        )
        lines.append(
            f"  {workflow} workflow={workflow_id} run={run_id} "
            f"attempt={attempt} conclusion={conclusion} "
            f"inventory={inventory} returned={len(jobs)} total={total_text} "
            f"leaves={role_counts['leaf']} "
            f"ambiguous={role_counts['ambiguous_leaf']} "
            f"aggregate fallout={role_counts['aggregate']}"
        )
        errors = manifest.get("errors")
        if inventory == "incomplete" and isinstance(errors, list):
            codes = sorted(
                {
                    _safe(error.get("code", "unknown"))
                    for error in errors
                    if isinstance(error, dict)
                }
            )
            lines.append(
                "    inventory errors: "
                + (", ".join(codes) if codes else "unavailable")
            )
    return lines


def _nested_value(
    value: dict[str, object],
    parent: str,
    child: str,
) -> object:
    nested = value.get(parent)
    return (
        nested.get(child, "unknown")
        if isinstance(nested, dict)
        else "unknown"
    )


def _classification(
    store: WorkflowLoopStore,
    item: WorkflowItem,
) -> tuple[FailureClassification, RecommendedResponse] | None:
    for entry in store.recent_history(item.id, limit=1000):
        classification = entry.detail.get("classification")
        response = entry.detail.get("recommendedResponse")
        try:
            return (
                FailureClassification(classification),
                RecommendedResponse(response),
            )
        except (TypeError, ValueError):
            continue
    return None


def _task_ranks(
    items: tuple[WorkflowItem, ...],
    classifications: dict[
        int,
        tuple[FailureClassification, RecommendedResponse] | None,
    ],
) -> dict[int, int]:
    candidates = [
        item
        for item in items
        if item.leaf_job is not None
        and item.cause_leader_id in {None, item.id}
        and (
            item.wait_reason == "deferred_by_episode_budget"
            or (
                classifications[item.id] is not None
                and classifications[item.id][1]
                in {
                    RecommendedResponse.REPAIR,
                    RecommendedResponse.INVESTIGATE,
                }
            )
        )
    ]
    episodes: dict[tuple[str, int, int, int], list[WorkflowItem]] = {}
    for item in candidates:
        key = (
            item.repository,
            item.workflow_id,
            item.failure_run_id,
            item.failure_attempt,
        )
        episodes.setdefault(key, []).append(item)
    ranked: dict[int, int] = {}
    for episode_items in episodes.values():
        ordered = sorted(
            episode_items,
            key=lambda item: (
                workflow_priority(item.workflow_path),
                _classification_rank(
                    classifications[item.id][0]
                    if classifications[item.id] is not None
                    else None
                ),
                item.case_key,
            ),
        )
        ranked.update(
            {
                item.id: index
                for index, item in enumerate(ordered, start=1)
            }
        )
    return ranked


def _classification_rank(
    classification: FailureClassification | None,
) -> int:
    return {
        FailureClassification.REPOSITORY_INFRA: 0,
        FailureClassification.PRODUCT_OR_BUILD: 0,
        FailureClassification.DETERMINISTIC_TEST: 1,
        FailureClassification.SUSPECTED_FLAKE: 2,
        FailureClassification.INSUFFICIENT_EVIDENCE: 3,
        FailureClassification.EXTERNAL_INFRA: 3,
    }.get(classification, 4)


def _cause_and_leaf_status(
    store: WorkflowLoopStore,
    items: tuple[WorkflowItem, ...],
    classifications: dict[
        int,
        tuple[FailureClassification, RecommendedResponse] | None,
    ],
    task_ranks: dict[int, int],
) -> list[str]:
    leaf_items = tuple(item for item in items if item.leaf_job is not None)
    if not leaf_items:
        return ["Cause groups: none", "Leaf policy: none"]
    starts = store.list_cause_starts()
    starts_by_item = {
        int(start["item_id"]): start
        for start in starts
    }
    starts_by_group = {
        str(start["group_id"]): start
        for start in starts
    }
    groups: dict[str, list[WorkflowItem]] = {}
    for item in leaf_items:
        group_id = item.cause_group_id or f"ungrouped:{item.id}"
        groups.setdefault(group_id, []).append(item)
    lines = ["Cause groups:"]
    for group_id, members in sorted(groups.items()):
        leader = next(
            (
                member
                for member in members
                if member.cause_leader_id in {None, member.id}
            ),
            members[0],
        )
        member_ids = ",".join(
            str(member.id)
            for member in sorted(members, key=lambda value: value.id)
        )
        lines.append(
            f"  {_safe(group_id)} state={_group_state(members)} "
            f"leader={leader.id} leaves={member_ids}"
        )
    lines.append("Leaf policy:")
    for item in sorted(leaf_items, key=lambda value: value.case_key):
        role = (
            "leader"
            if item.cause_leader_id in {None, item.id}
            else f"follower-of-{item.cause_leader_id}"
        )
        lines.append(
            f"  Leaf {item.id}: key={_safe(item.case_key)} "
            f"lane={_safe(item.leaf_job.name)} role={role} "
            f"state={item.phase.value}"
        )
        classification = classifications[item.id]
        if classification is None:
            lines.append(
                "    classification=unavailable response=unavailable"
            )
        else:
            lines.append(
                f"    classification={classification[0].value} "
                f"response={classification[1].value}"
            )
        rank = task_ranks.get(item.id)
        if rank is not None:
            start = (
                starts_by_group.get(item.cause_group_id)
                if item.cause_group_id is not None
                else starts_by_item.get(item.id)
            )
            if start is not None:
                kind = (
                    "proposal"
                    if bool(start.get("proposal"))
                    else "confirmed"
                )
                lines.append(f"    task rank={rank} started={kind}")
            elif item.wait_reason == "deferred_by_episode_budget":
                lines.append(
                    f"    task rank={rank} "
                    "deferred=deferred_by_episode_budget"
                )
            else:
                lines.append(
                    f"    task rank={rank} not-started="
                    f"{_safe(item.wait_reason or item.phase.value)}"
                )
        for witness in _recovery_witnesses(store, item):
            lines.append(
                "    Recovery witness: "
                f"run={witness['runId']} attempt={witness['attempt']} "
                f"head={_safe(witness['headSha'])} "
                f"job={witness['jobId']} "
                f"leaf={_safe(witness['leafCaseKey'])}"
            )
    return lines


def _group_state(members: list[WorkflowItem]) -> str:
    if any(member.wait_reason == "cause_conflict" for member in members):
        return "frozen-cause-conflict"
    recovered = sum(
        member.phase is ItemPhase.RECOVERED for member in members
    )
    if recovered == len(members):
        return "recovered"
    if recovered:
        return "partial-recovery"
    if any(member.phase is ItemPhase.NEEDS_ATTENTION for member in members):
        return "needs-attention"
    if all(member.phase is ItemPhase.SUPERSEDED for member in members):
        return "superseded"
    if any(
        member.task_state in {TaskState.QUEUED, TaskState.IN_PROGRESS}
        for member in members
    ):
        return "active-task"
    return "observing"


def _recovery_witnesses(
    store: WorkflowLoopStore,
    item: WorkflowItem,
) -> tuple[dict[str, object], ...]:
    expected = {
        "leafCaseKey",
        "runId",
        "attempt",
        "headSha",
        "jobId",
    }
    for entry in store.recent_history(item.id, limit=1000):
        raw = entry.detail.get("recoveryWitnesses")
        if not isinstance(raw, list):
            continue
        witnesses = tuple(
            witness
            for witness in raw
            if isinstance(witness, dict)
            and set(witness) == expected
            and witness.get("leafCaseKey") == item.case_key
            and isinstance(witness.get("runId"), int)
            and isinstance(witness.get("attempt"), int)
            and isinstance(witness.get("jobId"), int)
            and isinstance(witness.get("headSha"), str)
        )
        if witnesses:
            return witnesses
    return ()


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
