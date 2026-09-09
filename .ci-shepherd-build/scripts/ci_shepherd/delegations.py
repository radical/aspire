from __future__ import annotations

"""Deterministic lifecycle and global capacity accounting for delegated tasks."""

from dataclasses import dataclass
import copy
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import re
from typing import Mapping, Sequence

from .timeutils import parse_aware_iso8601


__all__ = [
    "AgentTask",
    "AgentTaskArtifact",
    "CapacityEvidence",
    "CapacityLimits",
    "CapacityUsage",
    "DelegatedPullRequest",
    "DelegatedIssue",
    "DelegationStart",
    "PullRequestAssociation",
    "PullRequestState",
    "StartDecision",
    "StartOutcome",
    "TaskAssociationDecision",
    "TaskLifecycle",
    "TaskLifecycleResult",
    "TaskState",
    "decide_new_start",
    "delegation_starts_from_events",
    "delegation_records_from_events",
    "active_owned_task_ids_from_events",
    "derive_capacity_usage",
    "derive_delegation_tracking",
    "derive_task_lifecycle",
    "normalize_agent_task",
    "reconcile_started_task",
    "render_delegation_status_section",
]


class TaskState(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    IDLE = "idle"
    WAITING_FOR_USER = "waiting_for_user"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class TaskLifecycle(StrEnum):
    RUNNING = "running"
    AWAITING_PULL_REQUEST = "awaiting_pull_request"
    COMPLETED = "completed"
    CLOSED_UNMERGED = "closed_unmerged"
    HANDOFF_REQUIRED = "handoff_required"
    ASSOCIATION_PENDING = "association_pending"


class StartOutcome(StrEnum):
    STARTED = "started"
    INDETERMINATE = "indeterminate"


class PullRequestState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"
    UNKNOWN = "unknown"


class PullRequestAssociation(StrEnum):
    ASSOCIATED = "associated"
    NONE = "none"
    PENDING = "pending"


@dataclass(frozen=True, slots=True)
class AgentTaskArtifact:
    artifact_type: str
    provider: str | None = None
    database_id: int | None = None
    global_id: str | None = None
    head_ref: str | None = None
    base_ref: str | None = None


@dataclass(frozen=True, slots=True)
class AgentTask:
    task_id: str
    state: TaskState
    created_at: datetime
    updated_at: datetime
    session_count: int
    artifacts: tuple[AgentTaskArtifact, ...]

    @property
    def pull_artifacts(self) -> tuple[AgentTaskArtifact, ...]:
        return tuple(
            artifact
            for artifact in self.artifacts
            if artifact.artifact_type == "pull"
        )

    @property
    def branch_artifacts(self) -> tuple[AgentTaskArtifact, ...]:
        return tuple(
            artifact
            for artifact in self.artifacts
            if artifact.artifact_type == "branch"
        )


@dataclass(frozen=True, slots=True)
class DelegationStart:
    """Durable evidence that the shepherd attempted one task start."""

    started_at: datetime
    outcome: StartOutcome
    task_id: str | None
    issue_number: int | None = None

    def __post_init__(self) -> None:
        _require_aware(self.started_at, "started_at")
        if not isinstance(self.outcome, StartOutcome):
            raise ValueError("outcome must be a StartOutcome.")
        if self.task_id is not None and not self.task_id:
            raise ValueError("task_id must be nonempty when supplied.")
        if self.issue_number is not None and (
            not isinstance(self.issue_number, int)
            or isinstance(self.issue_number, bool)
            or self.issue_number <= 0
        ):
            raise ValueError("issue_number must be positive when supplied.")


@dataclass(frozen=True, slots=True)
class DelegatedPullRequest:
    database_id: int
    global_id: str | None
    state: PullRequestState
    is_draft: bool
    number: int | None = None
    changed_files: int | None = None
    human_authored: bool | None = None
    merged_at: datetime | None = None
    merge_commit_sha: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.database_id, int)
            or isinstance(self.database_id, bool)
            or self.database_id <= 0
        ):
            raise ValueError("database_id must be a positive integer.")
        if self.global_id is not None and not self.global_id:
            raise ValueError("global_id must be nonempty when supplied.")
        if not isinstance(self.state, PullRequestState):
            raise ValueError("state must be a PullRequestState.")
        if not isinstance(self.is_draft, bool):
            raise ValueError("is_draft must be a boolean.")
        if self.number is not None and (
            not isinstance(self.number, int)
            or isinstance(self.number, bool)
            or self.number <= 0
        ):
            raise ValueError("number must be a positive integer when supplied.")
        if self.changed_files is not None and (
            not isinstance(self.changed_files, int)
            or isinstance(self.changed_files, bool)
            or self.changed_files < 0
        ):
            raise ValueError("changed_files must be nonnegative when supplied.")
        if self.human_authored is not None and not isinstance(
            self.human_authored,
            bool,
        ):
            raise ValueError("human_authored must be a boolean when supplied.")
        if self.merged_at is not None:
            _require_aware(self.merged_at, "merged_at")
        if self.merge_commit_sha is not None and (
            not isinstance(self.merge_commit_sha, str)
            or re.fullmatch(r"[0-9a-f]{40}", self.merge_commit_sha) is None
        ):
            raise ValueError("merge_commit_sha must be a full lowercase commit SHA when supplied.")
        if (self.merged_at is not None or self.merge_commit_sha is not None) and self.state is not PullRequestState.MERGED:
            raise ValueError("Merge facts require a verified merged pull request.")


@dataclass(frozen=True, slots=True)
class DelegatedIssue:
    number: int
    is_open: bool
    copilot_assigned: bool
    human_assigned: bool | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.number, int)
            or isinstance(self.number, bool)
            or self.number <= 0
        ):
            raise ValueError("number must be a positive integer.")
        if not isinstance(self.is_open, bool):
            raise ValueError("is_open must be a boolean.")
        if not isinstance(self.copilot_assigned, bool):
            raise ValueError("copilot_assigned must be a boolean.")
        if self.human_assigned is not None and not isinstance(
            self.human_assigned,
            bool,
        ):
            raise ValueError("human_assigned must be a boolean when supplied.")


@dataclass(frozen=True, slots=True)
class CapacityEvidence:
    owned_task_inventory_complete: bool = True
    pull_request_inventory_complete: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.owned_task_inventory_complete, bool):
            raise ValueError("owned_task_inventory_complete must be a boolean.")
        if not isinstance(self.pull_request_inventory_complete, bool):
            raise ValueError("pull_request_inventory_complete must be a boolean.")


@dataclass(frozen=True, slots=True)
class CapacityLimits:
    max_running_tasks: int
    max_starts_per_rolling_24h: int
    max_open_delegated_prs: int
    max_repository_running_tasks: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "max_running_tasks",
            "max_starts_per_rolling_24h",
            "max_open_delegated_prs",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer.")
        if (
            self.max_repository_running_tasks is not None
            and (
                not isinstance(self.max_repository_running_tasks, int)
                or isinstance(self.max_repository_running_tasks, bool)
                or self.max_repository_running_tasks < 0
            )
        ):
            raise ValueError(
                "max_repository_running_tasks must be a nonnegative integer "
                "when supplied."
            )


@dataclass(frozen=True, slots=True)
class TaskLifecycleResult:
    task_id: str
    state: TaskState | None
    association: PullRequestAssociation
    lifecycle: TaskLifecycle
    requires_handoff: bool


@dataclass(frozen=True, slots=True)
class CapacityUsage:
    running_tasks: int
    starts_in_rolling_24h: int
    open_delegated_prs: int
    repository_running_tasks: int
    task_lifecycles: tuple[TaskLifecycleResult, ...]
    complete: bool
    problems: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    @property
    def handoff_task_ids(self) -> tuple[str, ...]:
        return tuple(
            item.task_id
            for item in self.task_lifecycles
            if item.requires_handoff
        )


@dataclass(frozen=True, slots=True)
class StartDecision:
    permitted: bool
    blocked_by: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TaskAssociationDecision:
    task_id: str | None
    problem: str | None


def normalize_agent_task(record: Mapping[str, object]) -> AgentTask:
    """Validate and normalize one official Agent Tasks API record."""
    task_id = _nonempty_string(record.get("id"), "id")
    state_value = _nonempty_string(record.get("state"), "state")
    try:
        state = TaskState(state_value)
    except ValueError as ex:
        raise ValueError(f"state has unsupported value {state_value!r}.") from ex

    session_count = record.get("session_count", 0)
    if (
        not isinstance(session_count, int)
        or isinstance(session_count, bool)
        or session_count < 0
    ):
        raise ValueError("session_count must be a nonnegative integer.")

    raw_artifacts = record.get("artifacts", [])
    if not isinstance(raw_artifacts, list):
        raise ValueError("artifacts must be a list.")

    artifacts: list[AgentTaskArtifact] = []
    for index, raw_artifact in enumerate(raw_artifacts):
        if not isinstance(raw_artifact, Mapping):
            raise ValueError(f"artifacts[{index}] must be an object.")
        artifact_type = _nonempty_string(
            raw_artifact.get("type"), f"artifacts[{index}].type"
        )
        if artifact_type == "branch":
            provider = _nonempty_string(
                raw_artifact.get("provider"), f"artifacts[{index}].provider"
            )
            if provider != "github":
                raise ValueError(
                    f"artifacts[{index}].provider must be 'github' for branch "
                    "artifacts."
                )
            data = raw_artifact.get("data")
            if not isinstance(data, Mapping):
                raise ValueError(f"artifacts[{index}].data must be an object.")
            artifacts.append(
                AgentTaskArtifact(
                    artifact_type="branch",
                    provider=provider,
                    head_ref=_nonempty_string(
                        data.get("head_ref"),
                        f"artifacts[{index}].data.head_ref",
                    ),
                    base_ref=_nonempty_string(
                        data.get("base_ref"),
                        f"artifacts[{index}].data.base_ref",
                    ),
                )
            )
            continue
        if artifact_type != "pull":
            artifacts.append(AgentTaskArtifact(artifact_type=artifact_type))
            continue

        provider = _nonempty_string(
            raw_artifact.get("provider"), f"artifacts[{index}].provider"
        )
        if provider != "github":
            raise ValueError(
                f"artifacts[{index}].provider must be 'github' for pull artifacts."
            )
        data = raw_artifact.get("data")
        if not isinstance(data, Mapping):
            raise ValueError(f"artifacts[{index}].data must be an object.")
        database_id = data.get("id")
        global_id_value = data.get("global_id")
        if (
            not isinstance(database_id, int)
            or isinstance(database_id, bool)
            or database_id <= 0
        ):
            raise ValueError(
                f"artifacts[{index}].data.id must be a positive integer."
            )
        if global_id_value is not None and not isinstance(global_id_value, str):
            raise ValueError(
                f"artifacts[{index}].data.global_id must be null or a string."
            )
        global_id = global_id_value or None
        artifacts.append(
            AgentTaskArtifact(
                artifact_type="pull",
                provider=provider,
                database_id=database_id,
                global_id=global_id,
            )
        )

    created_at_value = record.get("created_at")
    return AgentTask(
        task_id=task_id,
        state=state,
        created_at=parse_aware_iso8601(created_at_value, "created_at"),
        updated_at=parse_aware_iso8601(
            record.get("updated_at", created_at_value),
            "updated_at",
        ),
        session_count=session_count,
        artifacts=tuple(artifacts),
    )


def reconcile_started_task(
    *,
    task_ids_before: set[str] | frozenset[str],
    tasks_after: Sequence[AgentTask],
) -> TaskAssociationDecision:
    """Bind an assignment to the only task absent from its pre-write inventory."""
    new_task_ids = sorted(
        {
            task.task_id
            for task in tasks_after
            if task.task_id not in task_ids_before
        }
    )
    if len(new_task_ids) == 1:
        return TaskAssociationDecision(task_id=new_task_ids[0], problem=None)
    if not new_task_ids:
        return TaskAssociationDecision(
            task_id=None,
            problem="delegated_task_not_visible",
        )
    return TaskAssociationDecision(
        task_id=None,
        problem="delegated_task_association_ambiguous",
    )


def delegation_starts_from_events(
    events: Sequence[Mapping[str, object]],
) -> tuple[DelegationStart, ...]:
    """Project crash-safe assignment reservations into capacity start facts."""
    latest_terminals: dict[str, Mapping[str, object]] = {}
    for event in events:
        action_id = event.get("actionId")
        if event.get("eventType") == "terminal" and isinstance(action_id, str):
            latest_terminals[action_id] = event

    starts: list[DelegationStart] = []
    seen_action_ids: set[str] = set()
    for event in events:
        if (
            event.get("eventType") != "delegation-baseline"
            or event.get("operation") != "assign-copilot"
        ):
            continue
        action_id = _nonempty_string(event.get("actionId"), "actionId")
        if action_id in seen_action_ids:
            raise ValueError(
                f"Duplicate delegation baseline for action {action_id!r}."
            )
        seen_action_ids.add(action_id)
        task_ids_before = event.get("taskIdsBefore")
        if (
            not isinstance(task_ids_before, list)
            or not all(isinstance(task_id, str) and task_id for task_id in task_ids_before)
        ):
            raise ValueError(
                f"Delegation baseline {action_id!r} has invalid taskIdsBefore."
            )

        terminal = latest_terminals.get(action_id)
        terminal_outcome = terminal.get("outcome") if terminal is not None else None
        if terminal_outcome in {"failed", "skipped", "stale"}:
            continue
        outcome = (
            StartOutcome.STARTED
            if terminal_outcome == "executed"
            else StartOutcome.INDETERMINATE
        )
        result = terminal.get("result") if terminal is not None else None
        task_id_value = result.get("taskId") if isinstance(result, Mapping) else None
        task_id = (
            task_id_value
            if isinstance(task_id_value, str) and task_id_value
            else None
        )
        target = event.get("target")
        issue_number_value = (
            target.get("number")
            if isinstance(target, Mapping) and target.get("kind") == "issue"
            else None
        )
        issue_number = (
            issue_number_value
            if isinstance(issue_number_value, int)
            and not isinstance(issue_number_value, bool)
            and issue_number_value > 0
            else None
        )
        starts.append(
            DelegationStart(
                started_at=parse_aware_iso8601(
                    event.get("recordedAt"),
                    f"{action_id}.recordedAt",
                ),
                outcome=outcome,
                task_id=task_id,
                issue_number=issue_number,
            )
        )

    return tuple(starts)


def active_owned_task_ids_from_events(
    events: Sequence[Mapping[str, object]],
) -> frozenset[str]:
    retired_task_ids = {
        task_id
        for event in events
        for task_id in [event.get("taskId")]
        if event.get("eventType") == "delegation-retired"
        and isinstance(task_id, str)
        and task_id
    }
    for record in delegation_records_from_events(events):
        if record.get("retired") is False or (
            record.get("taskObservation", "available") == "available"
            and record.get("taskState") in {"queued", "in_progress"}
        ):
            retired_task_ids.discard(record.get("taskId"))
    return frozenset(
        start.task_id
        for start in delegation_starts_from_events(events)
        if start.task_id is not None and start.task_id not in retired_task_ids
    )


def delegation_records_from_events(
    events: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    from .models import _validate_delegation_status

    records: dict[str, dict[str, object]] = {}
    baselines: dict[str, Mapping[str, object]] = {}
    terminals: dict[str, Mapping[str, object]] = {}
    for event in events:
        action_id = event.get("actionId")
        if event.get("eventType") == "delegation-baseline":
            baselines[action_id] = event
        elif event.get("eventType") == "terminal":
            terminals[action_id] = event
        if event.get("eventType") != "delegation-observed":
            continue
        record = event.get("record")
        _validate_delegation_status({"status": "complete", "records": [record]})
        baseline = baselines.get(action_id, {})
        result = terminals.get(action_id, {}).get("result")
        expected_task = result.get("taskId") if isinstance(result, Mapping) else None
        if (
            record["actionId"] != action_id
            or baseline.get("operation") != "assign-copilot"
            or baseline.get("repository") != record["repository"]
            or baseline.get("target") != {"kind": "issue", "number": record["issueNumber"]}
            or record.get("taskId") != expected_task
            or event.get("repository", record["repository"]) != record["repository"]
        ):
            raise ValueError("Delegation observation contradicts its assignment.")
        records[record["actionId"]] = copy.deepcopy(record)
    return tuple(records.values())


def derive_delegation_tracking(
    *,
    events: Sequence[Mapping[str, object]],
    tasks: Sequence[AgentTask],
    pull_requests: Sequence[DelegatedPullRequest],
    issues: Sequence[DelegatedIssue] = (),
    task_pull_request_ids: Mapping[str, Sequence[int]] | None = None,
    unavailable_task_ids: frozenset[str] = frozenset(),
    unavailable_issue_numbers: frozenset[int] = frozenset(),
) -> tuple[dict[str, object], ...]:
    """Preserve the durable issue -> task -> pull-request lifecycle chain."""
    latest_terminals: dict[str, Mapping[str, object]] = {}
    for event in events:
        action_id = event.get("actionId")
        if event.get("eventType") == "terminal" and isinstance(action_id, str):
            latest_terminals[action_id] = event
    tasks_by_id = {task.task_id: task for task in tasks}
    issues_by_number = {issue.number: issue for issue in issues}
    previous_by_action = {
        record["actionId"]: record for record in delegation_records_from_events(events)
    }
    retired_task_ids = {
        event.get("taskId") for event in events
        if event.get("eventType") == "delegation-retired"
    }

    tracking: list[dict[str, object]] = []
    for baseline in events:
        if (
            baseline.get("eventType") != "delegation-baseline"
            or baseline.get("operation") != "assign-copilot"
        ):
            continue
        action_id = _nonempty_string(baseline.get("actionId"), "actionId")
        terminal = latest_terminals.get(action_id)
        if terminal is not None and terminal.get("outcome") in {
            "failed",
            "skipped",
            "stale",
        }:
            continue
        repository = _nonempty_string(
            baseline.get("repository"),
            f"{action_id}.repository",
        )
        target = baseline.get("target")
        if not isinstance(target, Mapping) or target.get("kind") != "issue":
            raise ValueError(f"{action_id}.target must identify an issue.")
        issue_number = target.get("number")
        if (
            not isinstance(issue_number, int)
            or isinstance(issue_number, bool)
            or issue_number <= 0
        ):
            raise ValueError(f"{action_id}.target.number must be positive.")
        started_at = parse_aware_iso8601(
            baseline.get("recordedAt"),
            f"{action_id}.recordedAt",
        )
        result = terminal.get("result") if terminal is not None else None
        task_id_value = result.get("taskId") if isinstance(result, Mapping) else None
        task_id = (
            task_id_value
            if isinstance(task_id_value, str) and task_id_value
            else None
        )
        common = {
            "actionId": action_id,
            "repository": repository,
            "issueNumber": issue_number,
            "startedAt": started_at.isoformat().replace("+00:00", "Z"),
            "taskId": task_id,
        }
        previous = previous_by_action.get(action_id, {})
        if previous and (
            any(previous.get(key) != common[key] for key in ("repository", "issueNumber"))
            or previous.get("taskId") is not None and previous["taskId"] != task_id
        ):
            raise ValueError("Persisted delegation does not match its assignment.")
        live_issue = issues_by_number.get(issue_number)
        if issue_number in unavailable_issue_numbers:
            common["issueObservation"] = "unavailable"
        if live_issue is not None:
            common.update(
                {
                    "issueOpen": live_issue.is_open,
                    "copilotAssigned": live_issue.copilot_assigned,
                    **(
                        {"humanAssigned": live_issue.human_assigned}
                        if live_issue.human_assigned is not None
                        else {}
                    ),
                }
            )
        task = tasks_by_id.get(task_id) if task_id is not None else None
        known_pulls = previous.get("pullRequests", [])
        known_pulls_by_id = {pull["databaseId"]: pull for pull in known_pulls}
        known_states = {
            pull["databaseId"]: pull.get("lastKnownState") if pull["state"] == "unknown" else pull["state"]
            for pull in known_pulls
        }
        if task_id is None or (
            task is None and not known_pulls
            and not (task_pull_request_ids or {}).get(task_id)
        ):
            legacy_retired = task_id is not None and task_id in retired_task_ids
            tracking.append(
                {
                    **common,
                    "taskState": previous.get("taskState"),
                    "taskObservation": "unavailable" if task_id in unavailable_task_ids else "not-requested",
                    "lifecycle": "retired" if legacy_retired else TaskLifecycle.HANDOFF_REQUIRED.value,
                    "attemptOutcome": "legacy-unknown" if legacy_retired else "unresolved",
                    "requiresNewDecision": True,
                    "retired": legacy_retired,
                    "requiresHuman": True,
                    "handoffStartedAt": common["startedAt"],
                    "pullRequests": [],
                }
            )
            continue

        associated_pulls: list[DelegatedPullRequest] = []
        association = PullRequestAssociation.NONE
        identities = {
            pull["databaseId"]: pull.get("globalId") for pull in known_pulls
        }
        for identity in (task_pull_request_ids or {}).get(task_id, ()):
            identities.setdefault(identity, None)
        for artifact in task.pull_artifacts if task is not None else ():
            previous_global = identities.get(artifact.database_id)
            if previous_global is not None and artifact.global_id is not None and previous_global != artifact.global_id:
                raise ValueError("Task pull artifact contradicts its persisted identity.")
            identities[artifact.database_id] = artifact.global_id or previous_global
        if identities:
            association = PullRequestAssociation.ASSOCIATED
            for database_id, global_id in sorted(identities.items()):
                matches = [
                    pull_request
                    for pull_request in pull_requests
                    if pull_request.database_id == database_id
                    and (
                        global_id is None
                        or pull_request.global_id == global_id
                    )
                ]
                if len(matches) != 1:
                    association = PullRequestAssociation.PENDING
                    known = next((pull for pull in known_pulls if pull["databaseId"] == database_id), {})
                    associated_pulls.append(DelegatedPullRequest(
                        database_id=database_id, global_id=global_id,
                        number=known.get("number"), state=PullRequestState.UNKNOWN,
                        is_draft=False,
                    ))
                    continue
                pull_request = matches[0]
                associated_pulls.append(pull_request)
                if pull_request.state in {
                    PullRequestState.UNKNOWN,
                }:
                    association = PullRequestAssociation.PENDING
        lifecycle = derive_task_lifecycle(
            task,
            association=association,
            pull_requests=associated_pulls,
            task_id=task_id,
        )
        outcome = {
            TaskLifecycle.COMPLETED: "merged",
            TaskLifecycle.CLOSED_UNMERGED: "closed-unmerged",
            TaskLifecycle.HANDOFF_REQUIRED: "unresolved",
        }.get(lifecycle.lifecycle, "pending")
        if (
            lifecycle.lifecycle is TaskLifecycle.ASSOCIATION_PENDING
            and previous.get("attemptOutcome") in {"merged", "closed-unmerged"}
            and all(pull.state is not PullRequestState.OPEN for pull in associated_pulls)
            and set(identities).issubset(known_states)
        ):
            # Losing a current API response does not undo a verified historical
            # disposition. Current PR state remains unknown and blocks capacity.
            outcome = previous["attemptOutcome"]
        retired = outcome in {"merged", "closed-unmerged"}
        handoff_at = (
            task.updated_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
            if task is not None else previous.get("handoffStartedAt", common["startedAt"])
        )
        tracking.append(
            {
                **common,
                "taskState": task.state.value if task is not None else previous.get("taskState"),
                "taskObservation": "available" if task is not None else "unavailable" if task_id in unavailable_task_ids else "not-requested",
                "lifecycle": lifecycle.lifecycle.value,
                "attemptOutcome": outcome,
                "requiresNewDecision": outcome != "pending" or previous.get("requiresNewDecision") is True,
                "retired": retired,
                "requiresHuman": lifecycle.requires_handoff,
                **(
                    {
                        "handoffStartedAt": handoff_at,
                    }
                    if lifecycle.requires_handoff
                    else {}
                ),
                **(
                    {
                        "nextWakeup": {
                            "reason": "retry-backoff",
                            "evaluateAt": (
                                parse_aware_iso8601(handoff_at, "handoffStartedAt")
                                + timedelta(minutes=15)
                            )
                            .isoformat()
                            .replace("+00:00", "Z"),
                        }
                    }
                    if lifecycle.lifecycle is TaskLifecycle.ASSOCIATION_PENDING
                    else {}
                ),
                "pullRequests": [
                    {
                        "databaseId": pull_request.database_id,
                        "state": pull_request.state.value,
                        **(
                            {"lastKnownState": known_states[pull_request.database_id]}
                            if pull_request.state is PullRequestState.UNKNOWN
                            and known_states.get(pull_request.database_id) in {"open", "closed", "merged"}
                            else {}
                        ),
                        "isDraft": pull_request.is_draft,
                        **_merge_facts(pull_request, known_pulls_by_id.get(pull_request.database_id, {})),
                        **(
                            {"globalId": pull_request.global_id}
                            if pull_request.global_id is not None
                            else {}
                        ),
                        **(
                            {"number": pull_request.number}
                            if pull_request.number is not None
                            else {}
                        ),
                        **(
                            {"changedFiles": pull_request.changed_files}
                            if pull_request.changed_files is not None
                            else {}
                        ),
                        **(
                            {"humanAuthored": pull_request.human_authored}
                            if pull_request.human_authored is not None
                            else {}
                        ),
                    }
                    for pull_request in associated_pulls
                ],
            }
        )

    return tuple(tracking)


def _merge_facts(
    pull: DelegatedPullRequest, previous: Mapping[str, object],
) -> dict[str, object]:
    facts = {}
    for field, current in (
        ("mergedAt", pull.merged_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
         if pull.merged_at is not None else None),
        ("mergeCommitSha", pull.merge_commit_sha),
    ):
        old = previous.get(field)
        if old is not None and field == "mergedAt":
            old = parse_aware_iso8601(old, "mergedAt").astimezone(UTC).isoformat().replace("+00:00", "Z")
        if old is not None and current is not None and old != current:
            raise ValueError("Observed pull request merge facts contradict verified history.")
        if old is not None and pull.state in {PullRequestState.OPEN, PullRequestState.CLOSED}:
            raise ValueError("Previously merged pull request cannot be observed as unmerged.")
        if current is not None or old is not None:
            facts[field] = current if current is not None else old
    return facts


def derive_task_lifecycle(
    task: AgentTask | None,
    *,
    association: PullRequestAssociation,
    pull_requests: Sequence[DelegatedPullRequest] = (),
    task_id: str | None = None,
) -> TaskLifecycleResult:
    """Derive task lifecycle after accounting for PR association evidence."""
    state = task.state if task is not None else None
    if pull_requests:
        open_pulls = [
            pull for pull in pull_requests if pull.state is PullRequestState.OPEN
        ]
        if (
            association is PullRequestAssociation.PENDING
            or any(pull.state is PullRequestState.UNKNOWN for pull in pull_requests)
            or any(pull.changed_files is None for pull in open_pulls)
        ):
            lifecycle = TaskLifecycle.ASSOCIATION_PENDING
            requires_handoff = False
        elif open_pulls:
            if any(pull.changed_files > 0 for pull in open_pulls):
                lifecycle = TaskLifecycle.AWAITING_PULL_REQUEST
                requires_handoff = False
            else:
                lifecycle = TaskLifecycle.HANDOFF_REQUIRED
                requires_handoff = True
        elif any(pull.state is PullRequestState.MERGED for pull in pull_requests):
            lifecycle = TaskLifecycle.COMPLETED
            requires_handoff = False
        else:
            lifecycle = TaskLifecycle.CLOSED_UNMERGED
            requires_handoff = True
    elif state in {TaskState.QUEUED, TaskState.IN_PROGRESS}:
        lifecycle = TaskLifecycle.RUNNING
        requires_handoff = False
    elif state is TaskState.COMPLETED:
        if association is PullRequestAssociation.NONE:
            lifecycle = TaskLifecycle.HANDOFF_REQUIRED
            requires_handoff = True
        else:
            lifecycle = TaskLifecycle.ASSOCIATION_PENDING
            requires_handoff = False
    else:
        lifecycle = TaskLifecycle.HANDOFF_REQUIRED
        requires_handoff = True

    return TaskLifecycleResult(
        task_id=task.task_id if task is not None else _nonempty_string(task_id, "task_id"),
        state=state,
        association=association,
        lifecycle=lifecycle,
        requires_handoff=requires_handoff,
    )


def derive_capacity_usage(
    *,
    tasks: Sequence[AgentTask],
    starts: Sequence[DelegationStart],
    pull_requests: Sequence[DelegatedPullRequest],
    evidence: CapacityEvidence,
    now: datetime,
    issues: Sequence[DelegatedIssue] = (),
    retired_task_ids: frozenset[str] = frozenset(),
    task_pull_request_ids: Mapping[str, Sequence[int]] | None = None,
) -> CapacityUsage:
    """Derive shepherd usage and repository-wide runaway protection."""
    now = _require_aware(now, "now")
    problems: list[str] = []
    if not evidence.owned_task_inventory_complete:
        problems.append("owned_task_inventory_incomplete")
    if not evidence.pull_request_inventory_complete:
        problems.append("pull_request_inventory_incomplete")

    cutoff = now - timedelta(hours=24)
    starts_in_window = 0
    owned_task_ids: set[str] = set()
    recent_owned_task_ids: set[str] = set()
    observed_task_ids = {task.task_id for task in tasks}
    bound_pulls = task_pull_request_ids or {}
    for start in starts:
        started_at = start.started_at.astimezone(UTC)
        if started_at > now:
            problems.append("delegation_start_is_in_the_future")
        elif started_at > cutoff:
            starts_in_window += 1
        if (
            start.task_id is not None
            and (
                start.task_id not in retired_task_ids
                or start.task_id in observed_task_ids
                or start.task_id in bound_pulls
            )
        ):
            owned_task_ids.add(start.task_id)
            if started_at > cutoff:
                recent_owned_task_ids.add(start.task_id)
        elif start.task_id is None and started_at > cutoff:
            if start.outcome is StartOutcome.STARTED:
                problems.append("started_task_association_pending")
            else:
                problems.append("indeterminate_start_pending")

    tasks_by_id: dict[str, AgentTask] = {}
    duplicate_task_ids: set[str] = set()
    for task in tasks:
        if task.task_id in tasks_by_id:
            duplicate_task_ids.add(task.task_id)
        else:
            tasks_by_id[task.task_id] = task
        created_at = task.created_at.astimezone(UTC)
        if created_at > now and task.task_id in owned_task_ids:
            problems.append(f"task_created_in_the_future:{task.task_id}")
    for task_id in sorted(duplicate_task_ids):
        if task_id in owned_task_ids:
            problems.append(f"task_inventory_ambiguous:{task_id}")

    warnings = [
        f"foreign_task_inventory_ambiguous:{task_id}"
        for task_id in sorted(duplicate_task_ids - owned_task_ids)
    ]

    lifecycles: list[TaskLifecycleResult] = []
    for task_id in sorted(recent_owned_task_ids):
        task = tasks_by_id.get(task_id)
        if task is None and not bound_pulls.get(task_id):
            problems.append(f"owned_task_missing:{task_id}")

    open_pull_requests: set[tuple[int, str]] = set()
    for task_id in sorted(owned_task_ids):
        task = tasks_by_id.get(task_id)
        if task is None and not bound_pulls.get(task_id):
            continue
        association = PullRequestAssociation.NONE
        associated_pulls: list[DelegatedPullRequest] = []
        if (
            not evidence.owned_task_inventory_complete
            or not evidence.pull_request_inventory_complete
        ):
            association = PullRequestAssociation.PENDING
        identities = {database_id: None for database_id in bound_pulls.get(task_id, ())}
        for artifact in task.pull_artifacts if task is not None else ():
            identities[artifact.database_id] = artifact.global_id
        for database_id, global_id in identities.items():
            matches = [
                pull_request
                for pull_request in pull_requests
                if pull_request.database_id == database_id
                and (
                    global_id is None
                    or pull_request.global_id == global_id
                )
            ]
            if not matches:
                has_conflicting_identity = any(
                    pull_request.database_id == database_id
                    or (
                        global_id is not None
                        and pull_request.global_id == global_id
                    )
                    for pull_request in pull_requests
                )
                problem = (
                    "task_pull_request_association_ambiguous"
                    if has_conflicting_identity
                    else "task_pull_request_association_incomplete"
                )
                problems.append(f"{problem}:{task_id}")
                association = PullRequestAssociation.PENDING
                continue
            if len(matches) > 1:
                problems.append(f"task_pull_request_association_ambiguous:{task_id}")
                association = PullRequestAssociation.PENDING
                continue
            pull_request = matches[0]
            associated_pulls.append(pull_request)
            if pull_request.state is PullRequestState.UNKNOWN:
                problems.append(f"task_pull_request_state_unknown:{task_id}")
                association = PullRequestAssociation.PENDING
                continue
            if association is not PullRequestAssociation.PENDING:
                association = PullRequestAssociation.ASSOCIATED
            if pull_request.state is PullRequestState.OPEN:
                open_pull_requests.add(
                    (pull_request.database_id, pull_request.global_id)
                )
        lifecycles.append(
            derive_task_lifecycle(
                task,
                association=association,
                pull_requests=associated_pulls,
                task_id=task_id,
            )
        )

    return CapacityUsage(
        running_tasks=sum(
            item.state in {TaskState.QUEUED, TaskState.IN_PROGRESS}
            for item in lifecycles
        ),
        starts_in_rolling_24h=starts_in_window,
        open_delegated_prs=len(open_pull_requests),
        repository_running_tasks=sum(
            task.state in {TaskState.QUEUED, TaskState.IN_PROGRESS}
            for task in tasks_by_id.values()
        ),
        task_lifecycles=tuple(lifecycles),
        complete=not problems,
        problems=tuple(dict.fromkeys(problems)),
        warnings=tuple(warnings),
    )


def decide_new_start(usage: CapacityUsage, limits: CapacityLimits) -> StartDecision:
    """Decide whether exactly one additional delegated task may start."""
    blocked_by = list(usage.problems)
    if not usage.complete:
        blocked_by.append("capacity_evidence_incomplete")
    if usage.running_tasks >= limits.max_running_tasks:
        blocked_by.append("max_running_tasks")
    if usage.starts_in_rolling_24h >= limits.max_starts_per_rolling_24h:
        blocked_by.append("max_starts_per_rolling_24h")
    if usage.open_delegated_prs >= limits.max_open_delegated_prs:
        blocked_by.append("max_open_delegated_prs")
    if (
        limits.max_repository_running_tasks is not None
        and usage.repository_running_tasks
        >= limits.max_repository_running_tasks
    ):
        blocked_by.append("max_repository_running_tasks")
    return StartDecision(permitted=not blocked_by, blocked_by=tuple(blocked_by))


def render_delegation_status_section(
    status: object,
    proposals_document: object | None = None,
) -> str:
    lines = ["## Copilot delegations", ""]
    if not isinstance(status, Mapping):
        lines.append("No delegation status was collected.")
        return "\n".join(lines) + "\n"
    if status.get("status") == "incomplete":
        lines.extend(
            [
                f"Current observation is incomplete: {status.get('problem')}",
                "",
            ]
        )
    capacity = status.get("capacity")
    if isinstance(capacity, Mapping):
        lines.extend(
            [
                "| Shepherd active | Starts (24h) | Open delegated PRs "
                "| Repository active |",
                "|---:|---:|---:|---:|",
                f"| {capacity.get('runningTasks')} "
                f"| {capacity.get('startsInRolling24h')} "
                f"| {capacity.get('openDelegatedPullRequests')} "
                f"| {capacity.get('repositoryRunningTasks')} |",
                "",
            ]
        )
        problems = capacity.get("problems")
        if isinstance(problems, list) and problems:
            lines.extend(
                [
                    f"Capacity observation problems: {', '.join(map(str, problems))}",
                    "",
                ]
            )
    records = status.get("records")
    if not isinstance(records, list) or not records:
        lines.extend(
            [
                "No shepherd-owned Copilot delegations are currently being tracked.",
                "",
            ]
        )
        _append_delegation_proposals(lines, proposals_document)
        return "\n".join(lines) + "\n"
    lines.extend(
        [
            "| Issue | Task | State | Pull requests | Human handoff | Issue state | Attempt outcome | New decision |",
            "|---|---|---|---|---|---|---|---|",
        ]
    )
    for record in records:
        if not isinstance(record, Mapping):
            continue
        pull_requests = record.get("pullRequests")
        rendered_pulls = []
        if isinstance(pull_requests, list):
            for pull_request in pull_requests:
                if not isinstance(pull_request, Mapping):
                    continue
                number = pull_request.get("number")
                identity = (
                    f"#{number}"
                    if isinstance(number, int) and not isinstance(number, bool)
                    else str(pull_request.get("globalId") or "unknown")
                )
                rendered_pulls.append(
                    f"{identity} ({pull_request.get('state')})"
                    + (
                        f" [last verified: {pull_request['lastKnownState']}]"
                        if pull_request.get("lastKnownState") else ""
                    )
                )
        lifecycle = record.get("lifecycle")
        task_state = record.get("taskState")
        rendered_state = lifecycle or task_state
        if lifecycle and task_state and lifecycle != task_state:
            rendered_state = f"{lifecycle} (task: {task_state})"
        if record.get("taskObservation") in {"unavailable", "not-requested"}:
            rendered_state += f" [{record['taskObservation']}]"
        reminder = record.get("handoffReminder")
        handoff = "required" if record.get("requiresHuman") else "no"
        if isinstance(reminder, Mapping):
            handoff = (
                f"{reminder.get('state')} "
                f"(reminder {reminder.get('ordinal')})"
            )
        lines.append(
            f"| #{record.get('issueNumber')} "
            f"| `{record.get('taskId') or 'pending'}` "
            f"| {rendered_state} "
            f"| {', '.join(rendered_pulls) or 'none'} "
            f"| {handoff} "
            f"| {'open' if record.get('issueOpen') is True else 'closed' if record.get('issueOpen') is False else 'unknown'} "
            f"| {record.get('attemptOutcome', 'unknown')} "
            f"| {'required' if record.get('requiresNewDecision') else 'no'} |"
        )
    lines.append("")
    _append_delegation_proposals(lines, proposals_document)
    return "\n".join(lines) + "\n"


def _append_delegation_proposals(
    lines: list[str],
    proposals_document: object | None,
) -> None:
    if not isinstance(proposals_document, Mapping):
        return
    proposals = proposals_document.get("proposals")
    if not isinstance(proposals, list):
        return
    eligible = [
        proposal
        for proposal in proposals
        if isinstance(proposal, Mapping)
        and proposal.get("operation") == "assign-copilot"
        and isinstance(proposal.get("executionEligibility"), Mapping)
        and proposal["executionEligibility"].get("eligible") is True
    ]
    if not eligible:
        return
    count = len(eligible)
    noun = "proposal" if count == 1 else "proposals"
    verb = "was" if count == 1 else "were"
    lines.extend(
        [
            f"**{count} executable delegation {noun}** {verb} generated. They are "
            "not included in production comment selection and require the separate "
            "production delegation capability.",
            "",
            "| Issue | Action |",
            "|---|---|",
        ]
    )
    for proposal in eligible:
        lines.append(
            f"| #{proposal.get('issueNumber')} | `{proposal.get('actionId')}` |"
        )


def _nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty string.")
    return value


def _require_aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime.")
    return value.astimezone(UTC)
