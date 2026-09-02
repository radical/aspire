from __future__ import annotations

"""Atomic capacity reservation and task association for Copilot assignment."""

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Protocol, Sequence

from .delegation_observer import (
    DelegationReadClient,
    observe_agent_task_records,
    observe_delegations,
)
from .delegations import (
    active_owned_task_ids_from_events,
    CapacityLimits,
    decide_new_start,
    delegation_starts_from_events,
    normalize_agent_task,
    reconcile_started_task,
    derive_capacity_usage,
)
from .timeutils import parse_aware_iso8601


class DelegationExecutionState(Protocol):
    def action_events(
        self,
        *,
        repository: str,
    ) -> list[dict[str, object]]: ...

    def append_delegation_baseline(
        self,
        *,
        task_ids: tuple[str, ...],
        at: datetime,
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class DelegationReservation:
    permitted: bool
    task_ids_before: tuple[str, ...]
    blocked_by: tuple[str, ...]


def reserve_delegation_start(
    *,
    execution: DelegationExecutionState,
    client: DelegationReadClient,
    repository: str,
    limits: CapacityLimits,
    now: datetime,
) -> DelegationReservation:
    """Check capacity and persist the pre-write task inventory under the lock."""
    try:
        events = execution.action_events(repository=repository)
        starts = delegation_starts_from_events(events)
        owned_task_ids = set(active_owned_task_ids_from_events(events))
        retired_task_ids = frozenset(
            start.task_id
            for start in starts
            if start.task_id is not None
            and start.task_id not in owned_task_ids
        )
        owned_issue_numbers = {
            start.issue_number
            for start in starts
            if start.issue_number is not None
            and start.task_id in owned_task_ids
        }
        observation = observe_delegations(
            client,
            repository,
            owned_task_ids=owned_task_ids,
            owned_issue_numbers=owned_issue_numbers,
        )
        usage = derive_capacity_usage(
            tasks=observation.tasks,
            starts=starts,
            pull_requests=observation.pull_requests,
            evidence=observation.evidence,
            now=now,
            issues=observation.issues,
            retired_task_ids=retired_task_ids,
        )
        decision = decide_new_start(usage, limits)
    except Exception as exc:
        return DelegationReservation(
            permitted=False,
            task_ids_before=(),
            blocked_by=(f"delegation_observation_failed:{exc}",),
        )

    if not decision.permitted:
        return DelegationReservation(
            permitted=False,
            task_ids_before=(),
            blocked_by=decision.blocked_by,
        )

    task_ids_before = tuple(sorted(task.task_id for task in observation.tasks))
    execution.append_delegation_baseline(task_ids=task_ids_before, at=now)
    return DelegationReservation(
        permitted=True,
        task_ids_before=task_ids_before,
        blocked_by=(),
    )


def finalize_delegation_result(
    *,
    result: Mapping[str, object],
    task_ids_before: Sequence[str],
    client: DelegationReadClient,
    repository: str,
    assignment_started_at: object,
) -> dict[str, object]:
    """Attach the uniquely created task ID or leave the mutation indeterminate."""
    finalized = dict(result)
    if finalized.get("outcome") != "executed":
        return finalized

    try:
        not_before_value = assignment_started_at
        raw_result = finalized.get("result")
        if isinstance(raw_result, Mapping):
            not_before_value = raw_result.get(
                "assignmentObservedAt",
                not_before_value,
            )
        not_before = parse_aware_iso8601(
            not_before_value,
            "assignmentObservedAt",
        )
        task_records = observe_agent_task_records(
            client,
            repository,
            since=not_before,
        )
        tasks = [
            task
            for index, record in enumerate(task_records)
            for task in [
                normalize_agent_task(_mapping(record, f"tasks[{index}]"))
            ]
            if task.created_at >= not_before
        ]
        association = reconcile_started_task(
            task_ids_before=set(task_ids_before),
            tasks_after=tasks,
        )
    except Exception as exc:
        finalized["outcome"] = "indeterminate"
        finalized["reason"] = f"delegated_task_observation_failed:{exc}"
        return finalized

    if association.task_id is None:
        finalized["outcome"] = "indeterminate"
        finalized["reason"] = association.problem
        return finalized

    result_payload = dict(raw_result) if isinstance(raw_result, Mapping) else {}
    result_payload["taskId"] = association.task_id
    finalized["result"] = result_payload
    return finalized


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object.")
    return value
