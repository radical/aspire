from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import json
import re
from types import MappingProxyType
from typing import Any


_FINGERPRINT_PATTERN = re.compile(r"fnv1a64:[0-9a-f]{16}")
_RFC3339_UTC_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z"
)
_RESULT_KEYS = frozenset(
    {
        "schemaVersion",
        "itemId",
        "episode",
        "evidenceFingerprint",
        "decision",
        "summary",
        "evidenceIds",
        "inScopeJobIds",
        "copilotRequest",
    }
)
_REQUEST_KEYS = frozenset(
    {
        "workerId",
        "sessionId",
        "itemId",
        "episode",
        "evidenceFingerprint",
        "round",
        "repository",
        "branch",
        "workflowId",
        "workflowPath",
        "failureRun",
        "failedJobs",
        "evidenceIds",
        "issueNumber",
        "taskId",
        "pullRequestNumber",
        "pullRequestHeadSha",
        "pullRequestHeadRef",
        "pullRequestBaseRef",
        "pullRequestObservedAt",
        "followupCount",
        "prompt",
    }
)


class ItemPhase(StrEnum):
    OBSERVING_FAILURE = "observing_failure"
    WAITING_FOR_RUN = "waiting_for_run"
    JUDGMENT_QUEUED = "judgment_queued"
    JUDGMENT_RUNNING = "judgment_running"
    READY_FOR_ACTION = "ready_for_action"
    COPILOT_ACTIVE = "copilot_active"
    WAITING_FOR_CI = "waiting_for_ci"
    WAITING_FOR_HUMAN = "waiting_for_human"
    OBSERVING_EXTERNAL_REPAIR = "observing_external_repair"
    NEEDS_ATTENTION = "needs_attention"
    RECOVERED = "recovered"


class WorkState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INVALID = "invalid"
    SUPERSEDED = "superseded"


# Mirrors the Agent Tasks REST task status.state values so persisted observations
# retain the service's exact lifecycle vocabulary.
# https://docs.github.com/en/rest/agent-tasks/agent-tasks
class TaskState(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    IDLE = "idle"
    WAITING_FOR_USER = "waiting_for_user"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class ActionKind(StrEnum):
    CREATE_ISSUE = "create_issue"
    ASSIGN_COPILOT = "assign_copilot"
    FOLLOW_UP = "follow_up"


class ActionState(StrEnum):
    PREPARED = "prepared"
    INVOKING = "invoking"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"
    SUPERSEDED = "superseded"


class JudgmentDecision(StrEnum):
    ASSIGN = "assign"
    FOLLOW_UP = "follow_up"
    OBSERVE_EXTERNAL = "observe_external"
    DEFER_ORDINARY_TEST = "defer_ordinary_test"
    NEEDS_ATTENTION = "needs_attention"
    NO_ACTION = "no_action"


def _nonempty_string(value: object, name: str, *, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string.")
    if maximum is not None and len(value) > maximum:
        raise ValueError(f"{name} must contain at most {maximum} characters.")
    return value


def _optional_nonempty_string(
    value: object,
    name: str,
    *,
    maximum: int | None = None,
) -> str | None:
    if value is None:
        return None
    return _nonempty_string(value, name, maximum=maximum)


def _optional_string(value: object, name: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{name} must be a string or null.")
    return value


def _positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _optional_positive_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, name)


def _nonnegative_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer.")
    return value


def _optional_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer or null.")
    return value


def _timestamp(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or _RFC3339_UTC_PATTERN.fullmatch(value) is None
    ):
        raise ValueError(f"{name} must be an aware UTC RFC3339 timestamp ending Z.")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(
            f"{name} must be an aware UTC RFC3339 timestamp ending Z."
        ) from error
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError(f"{name} must be an aware UTC RFC3339 timestamp ending Z.")
    return value


def _optional_timestamp(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _timestamp(value, name)


def _fingerprint(value: object, name: str = "evidence_fingerprint") -> str:
    if not isinstance(value, str) or _FINGERPRINT_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be an fnv1a64 fingerprint.")
    return value


def _string_tuple(
    value: object,
    name: str,
    *,
    nonempty: bool = False,
    unique: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{name} must be a tuple.")
    result = tuple(_nonempty_string(entry, f"{name} entry") for entry in value)
    if nonempty and not result:
        raise ValueError(f"{name} must not be empty.")
    if unique and len(set(result)) != len(result):
        raise ValueError(f"{name} must contain unique values.")
    return result


def _json_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise ValueError(f"{name} must be a string-keyed mapping.")
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        copied = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must contain JSON-compatible values.") from error
    if not isinstance(copied, dict):
        raise ValueError(f"{name} must be a JSON object.")
    return MappingProxyType(copied)


@dataclass(frozen=True, slots=True)
class WorkflowKey:
    repository: str
    workflow_id: int
    branch: str

    def __post_init__(self) -> None:
        _nonempty_string(self.repository, "repository")
        _positive_int(self.workflow_id, "workflow_id")
        _nonempty_string(self.branch, "branch")


@dataclass(frozen=True, slots=True)
class JobKey:
    name: str
    runner_labels: tuple[str, ...]

    def __post_init__(self) -> None:
        _nonempty_string(self.name, "name")
        _string_tuple(self.runner_labels, "runner_labels", unique=True)


@dataclass(frozen=True, slots=True)
class JobObservation:
    run_id: int
    attempt: int
    job_id: int
    key: JobKey
    status: str
    conclusion: str | None
    started_at: str | None
    completed_at: str | None
    url: str
    log_excerpt: str | None
    log_truncated: bool

    def __post_init__(self) -> None:
        _positive_int(self.run_id, "run_id")
        _positive_int(self.attempt, "attempt")
        _positive_int(self.job_id, "job_id")
        if not isinstance(self.key, JobKey):
            raise ValueError("key must be a JobKey.")
        _nonempty_string(self.status, "status")
        _optional_nonempty_string(self.conclusion, "conclusion")
        _optional_timestamp(self.started_at, "started_at")
        _optional_timestamp(self.completed_at, "completed_at")
        _nonempty_string(self.url, "url")
        if self.log_excerpt is not None and not isinstance(self.log_excerpt, str):
            raise ValueError("log_excerpt must be a string or null.")
        if not isinstance(self.log_truncated, bool):
            raise ValueError("log_truncated must be a boolean.")


@dataclass(frozen=True, slots=True)
class RunObservation:
    key: WorkflowKey
    workflow_path: str
    workflow_name: str
    run_id: int
    run_number: int
    attempt: int
    head_sha: str
    event: str
    status: str
    conclusion: str | None
    created_at: str
    updated_at: str | None
    url: str
    jobs_complete: bool
    jobs: tuple[JobObservation, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.key, WorkflowKey):
            raise ValueError("key must be a WorkflowKey.")
        _nonempty_string(self.workflow_path, "workflow_path")
        _nonempty_string(self.workflow_name, "workflow_name")
        _positive_int(self.run_id, "run_id")
        _positive_int(self.run_number, "run_number")
        _positive_int(self.attempt, "attempt")
        _nonempty_string(self.head_sha, "head_sha")
        _nonempty_string(self.event, "event")
        _nonempty_string(self.status, "status")
        _optional_nonempty_string(self.conclusion, "conclusion")
        _timestamp(self.created_at, "created_at")
        _optional_timestamp(self.updated_at, "updated_at")
        _nonempty_string(self.url, "url")
        if not isinstance(self.jobs_complete, bool):
            raise ValueError("jobs_complete must be a boolean.")
        if not isinstance(self.jobs, tuple) or any(
            not isinstance(job, JobObservation) for job in self.jobs
        ):
            raise ValueError("jobs must be a tuple of JobObservation values.")
        if len({job.job_id for job in self.jobs}) != len(self.jobs):
            raise ValueError("jobs must contain unique job IDs.")
        if any(
            job.run_id != self.run_id or job.attempt != self.attempt
            for job in self.jobs
        ):
            raise ValueError("jobs must match the run ID and attempt.")


@dataclass(frozen=True, slots=True)
class WorkflowItem:
    id: int
    repository: str
    workflow_id: int
    workflow_path: str
    workflow_name: str
    branch: str
    episode: int
    phase: ItemPhase
    first_failure_seen_at: str
    last_checked_at: str
    last_progressed_at: str
    read_status: str
    failure_run_id: int
    failure_attempt: int
    failed_jobs: tuple[JobKey, ...]
    evidence_fingerprint: str
    last_judged_fingerprint: str | None
    wait_run_id: int | None
    wait_reason: str | None
    issue_number: int | None
    task_id: str | None
    task_state: TaskState | None
    pull_request_number: int | None
    external_owner: str | None
    followup_count: int
    assignment_requested_at: str | None
    assignment_confirmed_at: str | None
    recovered_run_id: int | None
    recovered_at: str | None
    latest_action: ActionKind | None
    latest_error: str | None

    def __post_init__(self) -> None:
        _positive_int(self.id, "id")
        _nonempty_string(self.repository, "repository")
        _positive_int(self.workflow_id, "workflow_id")
        _nonempty_string(self.workflow_path, "workflow_path")
        _nonempty_string(self.workflow_name, "workflow_name")
        _nonempty_string(self.branch, "branch")
        _positive_int(self.episode, "episode")
        if not isinstance(self.phase, ItemPhase):
            raise ValueError("phase must be an ItemPhase.")
        _timestamp(self.first_failure_seen_at, "first_failure_seen_at")
        _timestamp(self.last_checked_at, "last_checked_at")
        _timestamp(self.last_progressed_at, "last_progressed_at")
        _nonempty_string(self.read_status, "read_status")
        _positive_int(self.failure_run_id, "failure_run_id")
        _positive_int(self.failure_attempt, "failure_attempt")
        if not isinstance(self.failed_jobs, tuple) or any(
            not isinstance(job, JobKey) for job in self.failed_jobs
        ):
            raise ValueError("failed_jobs must be a tuple of JobKey values.")
        if len(set(self.failed_jobs)) != len(self.failed_jobs):
            raise ValueError("failed_jobs must contain unique values.")
        _fingerprint(self.evidence_fingerprint)
        if self.last_judged_fingerprint is not None:
            _fingerprint(
                self.last_judged_fingerprint,
                "last_judged_fingerprint",
            )
        _optional_positive_int(self.wait_run_id, "wait_run_id")
        _optional_nonempty_string(self.wait_reason, "wait_reason")
        _optional_positive_int(self.issue_number, "issue_number")
        _optional_nonempty_string(self.task_id, "task_id")
        if self.task_state is not None and not isinstance(
            self.task_state,
            TaskState,
        ):
            raise ValueError("task_state must be a TaskState or null.")
        if self.task_id is None and self.task_state is not None:
            raise ValueError(
                "task_state requires a nonempty task_id."
            )
        _optional_positive_int(
            self.pull_request_number,
            "pull_request_number",
        )
        _optional_nonempty_string(self.external_owner, "external_owner")
        _nonnegative_int(self.followup_count, "followup_count")
        _optional_timestamp(
            self.assignment_requested_at,
            "assignment_requested_at",
        )
        _optional_timestamp(
            self.assignment_confirmed_at,
            "assignment_confirmed_at",
        )
        _optional_positive_int(self.recovered_run_id, "recovered_run_id")
        _optional_timestamp(self.recovered_at, "recovered_at")
        if self.latest_action is not None and not isinstance(
            self.latest_action,
            ActionKind,
        ):
            raise ValueError("latest_action must be an ActionKind or null.")
        _optional_nonempty_string(self.latest_error, "latest_error")


@dataclass(frozen=True, slots=True)
class JudgmentRequest:
    worker_id: str
    session_id: str
    item_id: int
    episode: int
    evidence_fingerprint: str
    round: int
    repository: str
    branch: str
    workflow_id: int
    workflow_path: str
    failure_run: RunObservation
    failed_jobs: tuple[JobObservation, ...]
    evidence_ids: tuple[str, ...]
    issue_number: int | None
    task_id: str | None
    pull_request_number: int | None
    pull_request_head_sha: str | None
    pull_request_head_ref: str | None
    pull_request_base_ref: str | None
    pull_request_observed_at: str | None
    followup_count: int
    prompt: str

    def __post_init__(self) -> None:
        _nonempty_string(self.worker_id, "worker_id")
        _nonempty_string(self.session_id, "session_id")
        _positive_int(self.item_id, "item_id")
        _positive_int(self.episode, "episode")
        _fingerprint(self.evidence_fingerprint)
        _nonnegative_int(self.round, "round")
        _nonempty_string(self.repository, "repository")
        _nonempty_string(self.branch, "branch")
        _positive_int(self.workflow_id, "workflow_id")
        _nonempty_string(self.workflow_path, "workflow_path")
        if not isinstance(self.failure_run, RunObservation):
            raise ValueError("failure_run must be a RunObservation.")
        if (
            self.failure_run.key.repository != self.repository
            or self.failure_run.key.branch != self.branch
            or self.failure_run.key.workflow_id != self.workflow_id
            or self.failure_run.workflow_path != self.workflow_path
        ):
            raise ValueError("failure_run must match the request workflow identity.")
        if not isinstance(self.failed_jobs, tuple) or not self.failed_jobs or any(
            not isinstance(job, JobObservation) for job in self.failed_jobs
        ):
            raise ValueError(
                "failed_jobs must be a nonempty tuple of JobObservation values."
            )
        run_jobs = {job.job_id: job for job in self.failure_run.jobs}
        if any(run_jobs.get(job.job_id) != job for job in self.failed_jobs):
            raise ValueError("failed_jobs must be exact jobs from failure_run.")
        _string_tuple(
            self.evidence_ids,
            "evidence_ids",
            nonempty=True,
            unique=True,
        )
        _optional_positive_int(self.issue_number, "issue_number")
        _optional_nonempty_string(self.task_id, "task_id")
        _optional_positive_int(
            self.pull_request_number,
            "pull_request_number",
        )
        _optional_nonempty_string(
            self.pull_request_head_sha,
            "pull_request_head_sha",
        )
        _optional_nonempty_string(
            self.pull_request_head_ref,
            "pull_request_head_ref",
        )
        _optional_nonempty_string(
            self.pull_request_base_ref,
            "pull_request_base_ref",
        )
        _optional_timestamp(
            self.pull_request_observed_at,
            "pull_request_observed_at",
        )
        pr_identity = (
            self.task_id,
            self.pull_request_number,
            self.pull_request_head_sha,
            self.pull_request_head_ref,
            self.pull_request_base_ref,
            self.pull_request_observed_at,
        )
        if any(value is None for value in pr_identity) and any(
            value is not None for value in pr_identity
        ):
            raise ValueError(
                "task ID, pull request number, head SHA, head/base refs, "
                "and observation timestamp must be supplied together."
            )
        _nonnegative_int(self.followup_count, "followup_count")
        _nonempty_string(self.prompt, "prompt", maximum=20_000)


@dataclass(frozen=True, slots=True)
class JudgmentResult:
    schema_version: int
    item_id: int
    episode: int
    evidence_fingerprint: str
    decision: JudgmentDecision
    summary: str
    evidence_ids: tuple[str, ...]
    in_scope_job_ids: tuple[int, ...]
    copilot_request: str | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != 1
        ):
            raise ValueError("schema_version must be 1.")
        _positive_int(self.item_id, "item_id")
        _positive_int(self.episode, "episode")
        _fingerprint(self.evidence_fingerprint)
        if not isinstance(self.decision, JudgmentDecision):
            raise ValueError("decision must be a JudgmentDecision.")
        _nonempty_string(self.summary, "summary", maximum=2_000)
        _string_tuple(
            self.evidence_ids,
            "evidence_ids",
            nonempty=True,
            unique=True,
        )
        if not isinstance(self.in_scope_job_ids, tuple):
            raise ValueError("in_scope_job_ids must be a tuple.")
        for job_id in self.in_scope_job_ids:
            _positive_int(job_id, "in_scope_job_ids entry")
        if len(set(self.in_scope_job_ids)) != len(self.in_scope_job_ids):
            raise ValueError("in_scope_job_ids must contain unique values.")
        _optional_nonempty_string(
            self.copilot_request,
            "copilot_request",
            maximum=8_000,
        )


@dataclass(frozen=True, slots=True)
class WorkerReservation:
    worker_id: str
    item_id: int
    episode: int
    evidence_fingerprint: str
    session_id: str
    request_path: str
    result_path: str
    detail_path: str
    lifetime_lock_path: str
    queued_at: str
    judgment_round: int = 0

    def __post_init__(self) -> None:
        _nonempty_string(self.worker_id, "worker_id")
        _positive_int(self.item_id, "item_id")
        _positive_int(self.episode, "episode")
        _fingerprint(self.evidence_fingerprint)
        _nonempty_string(self.session_id, "session_id")
        _nonempty_string(self.request_path, "request_path")
        _nonempty_string(self.result_path, "result_path")
        _nonempty_string(self.detail_path, "detail_path")
        _nonempty_string(self.lifetime_lock_path, "lifetime_lock_path")
        _timestamp(self.queued_at, "queued_at")
        _nonnegative_int(self.judgment_round, "judgment_round")


@dataclass(frozen=True, slots=True)
class WorkerCompletion:
    worker_id: str
    state: WorkState
    completed_at: str
    exit_code: int | None
    error: str | None

    def __post_init__(self) -> None:
        _nonempty_string(self.worker_id, "worker_id")
        if self.state not in {
            WorkState.SUCCEEDED,
            WorkState.FAILED,
            WorkState.INVALID,
            WorkState.SUPERSEDED,
        }:
            raise ValueError("state must be a terminal WorkState.")
        _timestamp(self.completed_at, "completed_at")
        _optional_int(self.exit_code, "exit_code")
        _optional_nonempty_string(self.error, "error")


@dataclass(frozen=True, slots=True)
class WorkerView:
    worker_id: str
    item_id: int
    episode: int
    evidence_fingerprint: str
    session_id: str
    state: WorkState
    pid: int | None
    request_path: str
    result_path: str
    detail_path: str
    lifetime_lock_path: str
    queued_at: str
    launch_attempted_at: str | None
    launched_at: str | None
    completed_at: str | None
    exit_code: int | None
    error: str | None
    judgment_round: int = 0
    consumed_at: str | None = None

    def __post_init__(self) -> None:
        _nonempty_string(self.worker_id, "worker_id")
        _positive_int(self.item_id, "item_id")
        _positive_int(self.episode, "episode")
        _fingerprint(self.evidence_fingerprint)
        _nonempty_string(self.session_id, "session_id")
        if not isinstance(self.state, WorkState):
            raise ValueError("state must be a WorkState.")
        _optional_positive_int(self.pid, "pid")
        _nonempty_string(self.request_path, "request_path")
        _nonempty_string(self.result_path, "result_path")
        _nonempty_string(self.detail_path, "detail_path")
        _nonempty_string(self.lifetime_lock_path, "lifetime_lock_path")
        _timestamp(self.queued_at, "queued_at")
        _optional_timestamp(
            self.launch_attempted_at,
            "launch_attempted_at",
        )
        _optional_timestamp(self.launched_at, "launched_at")
        _optional_timestamp(self.completed_at, "completed_at")
        _optional_int(self.exit_code, "exit_code")
        _optional_nonempty_string(self.error, "error")
        _nonnegative_int(self.judgment_round, "judgment_round")
        _optional_timestamp(self.consumed_at, "consumed_at")


@dataclass(frozen=True, slots=True)
class ActionIntent:
    action_id: str
    item_id: int
    episode: int
    kind: ActionKind
    ordinal: int
    payload: Mapping[str, object]
    prepared_at: str

    def __post_init__(self) -> None:
        _nonempty_string(self.action_id, "action_id")
        _positive_int(self.item_id, "item_id")
        _positive_int(self.episode, "episode")
        if not isinstance(self.kind, ActionKind):
            raise ValueError("kind must be an ActionKind.")
        _positive_int(self.ordinal, "ordinal")
        object.__setattr__(self, "payload", _json_mapping(self.payload, "payload"))
        _timestamp(self.prepared_at, "prepared_at")


@dataclass(frozen=True, slots=True)
class ActionCompletion:
    action_id: str
    state: ActionState
    completed_at: str
    remote_number: int | None
    remote_task_id: str | None
    error: str | None

    def __post_init__(self) -> None:
        _nonempty_string(self.action_id, "action_id")
        if self.state not in {
            ActionState.CONFIRMED,
            ActionState.REJECTED,
            ActionState.UNCERTAIN,
            ActionState.SUPERSEDED,
        }:
            raise ValueError("state must be a terminal ActionState.")
        _timestamp(self.completed_at, "completed_at")
        _optional_positive_int(self.remote_number, "remote_number")
        _optional_nonempty_string(self.remote_task_id, "remote_task_id")
        _optional_nonempty_string(self.error, "error")


@dataclass(frozen=True, slots=True)
class ActionView:
    action_id: str
    item_id: int
    episode: int
    kind: ActionKind
    ordinal: int
    state: ActionState
    payload: Mapping[str, object]
    prepared_at: str
    invoked_at: str | None
    invocation_pass_id: str | None
    invocation_owner_id: str | None
    completed_at: str | None
    remote_number: int | None
    remote_task_id: str | None
    error: str | None

    def __post_init__(self) -> None:
        _nonempty_string(self.action_id, "action_id")
        _positive_int(self.item_id, "item_id")
        _positive_int(self.episode, "episode")
        if not isinstance(self.kind, ActionKind):
            raise ValueError("kind must be an ActionKind.")
        _positive_int(self.ordinal, "ordinal")
        if not isinstance(self.state, ActionState):
            raise ValueError("state must be an ActionState.")
        object.__setattr__(self, "payload", _json_mapping(self.payload, "payload"))
        _timestamp(self.prepared_at, "prepared_at")
        _optional_timestamp(self.invoked_at, "invoked_at")
        _optional_nonempty_string(
            self.invocation_pass_id,
            "invocation_pass_id",
        )
        _optional_nonempty_string(
            self.invocation_owner_id,
            "invocation_owner_id",
        )
        invocation_identity = (
            self.invoked_at,
            self.invocation_pass_id,
            self.invocation_owner_id,
        )
        if any(value is None for value in invocation_identity) and any(
            value is not None for value in invocation_identity
        ):
            raise ValueError(
                "invoked_at, invocation_pass_id, and invocation_owner_id "
                "must be supplied together."
            )
        if self.state is ActionState.PREPARED and self.invoked_at is not None:
            raise ValueError("A prepared action cannot have invocation identity.")
        if self.state is ActionState.INVOKING and self.invoked_at is None:
            raise ValueError("An invoking action requires invocation identity.")
        if (
            self.state in {ActionState.CONFIRMED, ActionState.UNCERTAIN}
            and self.invoked_at is None
        ):
            raise ValueError(
                "A confirmed or uncertain action requires invocation identity."
            )
        if self.state is ActionState.SUPERSEDED and self.invoked_at is not None:
            raise ValueError(
                "A superseded action cannot have invocation identity."
            )
        _optional_timestamp(self.completed_at, "completed_at")
        _optional_positive_int(self.remote_number, "remote_number")
        _optional_nonempty_string(self.remote_task_id, "remote_task_id")
        _optional_nonempty_string(self.error, "error")


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    sequence: int
    item_id: int
    recorded_at: str
    event: str
    summary: str
    detail: Mapping[str, object]

    def __post_init__(self) -> None:
        _positive_int(self.sequence, "sequence")
        _positive_int(self.item_id, "item_id")
        _timestamp(self.recorded_at, "recorded_at")
        _nonempty_string(self.event, "event")
        _nonempty_string(self.summary, "summary")
        object.__setattr__(self, "detail", _json_mapping(self.detail, "detail"))


def canonical_fingerprint(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("Fingerprint input must be JSON-compatible.") from error
    result = 0xCBF29CE484222325
    for byte in encoded:
        result ^= byte
        result = (result * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"fnv1a64:{result:016x}"


def _job_key_document(key: JobKey) -> dict[str, object]:
    return {"name": key.name, "runnerLabels": list(key.runner_labels)}


def _job_document(job: JobObservation) -> dict[str, object]:
    return {
        "runId": job.run_id,
        "attempt": job.attempt,
        "jobId": job.job_id,
        "key": _job_key_document(job.key),
        "status": job.status,
        "conclusion": job.conclusion,
        "startedAt": job.started_at,
        "completedAt": job.completed_at,
        "url": job.url,
        "logExcerpt": job.log_excerpt,
        "logTruncated": job.log_truncated,
    }


def _run_document(run: RunObservation) -> dict[str, object]:
    return {
        "key": {
            "repository": run.key.repository,
            "workflowId": run.key.workflow_id,
            "branch": run.key.branch,
        },
        "workflowPath": run.workflow_path,
        "workflowName": run.workflow_name,
        "runId": run.run_id,
        "runNumber": run.run_number,
        "attempt": run.attempt,
        "headSha": run.head_sha,
        "event": run.event,
        "status": run.status,
        "conclusion": run.conclusion,
        "createdAt": run.created_at,
        "updatedAt": run.updated_at,
        "url": run.url,
        "jobsComplete": run.jobs_complete,
        "jobs": [_job_document(job) for job in run.jobs],
    }


def judgment_request_to_json(request: JudgmentRequest) -> str:
    if not isinstance(request, JudgmentRequest):
        raise ValueError("request must be a JudgmentRequest.")
    return json.dumps(
        {
            "workerId": request.worker_id,
            "sessionId": request.session_id,
            "itemId": request.item_id,
            "episode": request.episode,
            "evidenceFingerprint": request.evidence_fingerprint,
            "round": request.round,
            "repository": request.repository,
            "branch": request.branch,
            "workflowId": request.workflow_id,
            "workflowPath": request.workflow_path,
            "failureRun": _run_document(request.failure_run),
            "failedJobs": [_job_document(job) for job in request.failed_jobs],
            "evidenceIds": list(request.evidence_ids),
            "issueNumber": request.issue_number,
            "taskId": request.task_id,
            "pullRequestNumber": request.pull_request_number,
            "pullRequestHeadSha": request.pull_request_head_sha,
            "pullRequestHeadRef": request.pull_request_head_ref,
            "pullRequestBaseRef": request.pull_request_base_ref,
            "pullRequestObservedAt": request.pull_request_observed_at,
            "followupCount": request.followup_count,
            "prompt": request.prompt,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _strict_json_object(text: str, name: str) -> dict[str, Any]:
    if not isinstance(text, str):
        raise ValueError(f"{name} must be JSON text.")

    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{name} contains duplicate key {key!r}.")
            result[key] = value
        return result

    try:
        value = json.loads(text.strip(), object_pairs_hook=pairs_hook)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{name} must be exactly one JSON object.") from error
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object.")
    return value


def _exact_keys(
    document: Mapping[str, object],
    expected: frozenset[str],
    name: str,
) -> None:
    actual = frozenset(document)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise ValueError(
            f"{name} fields do not match the schema; "
            f"missing={missing}, unknown={unknown}."
        )


def _sequence(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON array.")
    return value


def _job_key_from_document(value: object) -> JobKey:
    if not isinstance(value, dict):
        raise ValueError("Job key must be an object.")
    _exact_keys(value, frozenset({"name", "runnerLabels"}), "Job key")
    labels = _sequence(value["runnerLabels"], "runnerLabels")
    return JobKey(
        name=_nonempty_string(value["name"], "name"),
        runner_labels=tuple(
            _nonempty_string(label, "runnerLabels entry") for label in labels
        ),
    )


def _job_from_document(value: object) -> JobObservation:
    if not isinstance(value, dict):
        raise ValueError("Job observation must be an object.")
    keys = frozenset(
        {
            "runId",
            "attempt",
            "jobId",
            "key",
            "status",
            "conclusion",
            "startedAt",
            "completedAt",
            "url",
            "logExcerpt",
            "logTruncated",
        }
    )
    _exact_keys(value, keys, "Job observation")
    return JobObservation(
        run_id=_positive_int(value["runId"], "runId"),
        attempt=_positive_int(value["attempt"], "attempt"),
        job_id=_positive_int(value["jobId"], "jobId"),
        key=_job_key_from_document(value["key"]),
        status=_nonempty_string(value["status"], "status"),
        conclusion=_optional_nonempty_string(value["conclusion"], "conclusion"),
        started_at=_optional_timestamp(value["startedAt"], "startedAt"),
        completed_at=_optional_timestamp(value["completedAt"], "completedAt"),
        url=_nonempty_string(value["url"], "url"),
        log_excerpt=_optional_string(value["logExcerpt"], "logExcerpt"),
        log_truncated=value["logTruncated"],
    )


def _run_from_document(value: object) -> RunObservation:
    if not isinstance(value, dict):
        raise ValueError("Run observation must be an object.")
    keys = frozenset(
        {
            "key",
            "workflowPath",
            "workflowName",
            "runId",
            "runNumber",
            "attempt",
            "headSha",
            "event",
            "status",
            "conclusion",
            "createdAt",
            "updatedAt",
            "url",
            "jobsComplete",
            "jobs",
        }
    )
    _exact_keys(value, keys, "Run observation")
    raw_key = value["key"]
    if not isinstance(raw_key, dict):
        raise ValueError("Run key must be an object.")
    _exact_keys(
        raw_key,
        frozenset({"repository", "workflowId", "branch"}),
        "Run key",
    )
    jobs = _sequence(value["jobs"], "jobs")
    return RunObservation(
        key=WorkflowKey(
            repository=_nonempty_string(raw_key["repository"], "repository"),
            workflow_id=_positive_int(raw_key["workflowId"], "workflowId"),
            branch=_nonempty_string(raw_key["branch"], "branch"),
        ),
        workflow_path=_nonempty_string(value["workflowPath"], "workflowPath"),
        workflow_name=_nonempty_string(value["workflowName"], "workflowName"),
        run_id=_positive_int(value["runId"], "runId"),
        run_number=_positive_int(value["runNumber"], "runNumber"),
        attempt=_positive_int(value["attempt"], "attempt"),
        head_sha=_nonempty_string(value["headSha"], "headSha"),
        event=_nonempty_string(value["event"], "event"),
        status=_nonempty_string(value["status"], "status"),
        conclusion=_optional_nonempty_string(value["conclusion"], "conclusion"),
        created_at=_timestamp(value["createdAt"], "createdAt"),
        updated_at=_optional_timestamp(value["updatedAt"], "updatedAt"),
        url=_nonempty_string(value["url"], "url"),
        jobs_complete=value["jobsComplete"],
        jobs=tuple(_job_from_document(job) for job in jobs),
    )


def parse_judgment_request(text: str) -> JudgmentRequest:
    document = _strict_json_object(text, "Judgment request")
    _exact_keys(document, _REQUEST_KEYS, "Judgment request")
    failed_jobs = _sequence(document["failedJobs"], "failedJobs")
    evidence_ids = _sequence(document["evidenceIds"], "evidenceIds")
    return JudgmentRequest(
        worker_id=_nonempty_string(document["workerId"], "workerId"),
        session_id=_nonempty_string(document["sessionId"], "sessionId"),
        item_id=_positive_int(document["itemId"], "itemId"),
        episode=_positive_int(document["episode"], "episode"),
        evidence_fingerprint=_fingerprint(
            document["evidenceFingerprint"],
            "evidenceFingerprint",
        ),
        round=_nonnegative_int(document["round"], "round"),
        repository=_nonempty_string(document["repository"], "repository"),
        branch=_nonempty_string(document["branch"], "branch"),
        workflow_id=_positive_int(document["workflowId"], "workflowId"),
        workflow_path=_nonempty_string(
            document["workflowPath"],
            "workflowPath",
        ),
        failure_run=_run_from_document(document["failureRun"]),
        failed_jobs=tuple(_job_from_document(job) for job in failed_jobs),
        evidence_ids=tuple(
            _nonempty_string(evidence_id, "evidenceIds entry")
            for evidence_id in evidence_ids
        ),
        issue_number=_optional_positive_int(
            document["issueNumber"],
            "issueNumber",
        ),
        task_id=_optional_nonempty_string(
            document["taskId"],
            "taskId",
        ),
        pull_request_number=_optional_positive_int(
            document["pullRequestNumber"],
            "pullRequestNumber",
        ),
        pull_request_head_sha=_optional_nonempty_string(
            document["pullRequestHeadSha"],
            "pullRequestHeadSha",
        ),
        pull_request_head_ref=_optional_nonempty_string(
            document["pullRequestHeadRef"],
            "pullRequestHeadRef",
        ),
        pull_request_base_ref=_optional_nonempty_string(
            document["pullRequestBaseRef"],
            "pullRequestBaseRef",
        ),
        pull_request_observed_at=_optional_timestamp(
            document["pullRequestObservedAt"],
            "pullRequestObservedAt",
        ),
        followup_count=_nonnegative_int(
            document["followupCount"],
            "followupCount",
        ),
        prompt=_nonempty_string(document["prompt"], "prompt", maximum=20_000),
    )


def parse_judgment_result(
    text: str,
    request: JudgmentRequest,
) -> JudgmentResult:
    if not isinstance(request, JudgmentRequest):
        raise ValueError("request must be a JudgmentRequest.")
    document = _strict_json_object(text, "Judgment result")
    _exact_keys(document, _RESULT_KEYS, "Judgment result")
    if (
        not isinstance(document["schemaVersion"], int)
        or isinstance(document["schemaVersion"], bool)
        or document["schemaVersion"] != 1
    ):
        raise ValueError("schemaVersion must be 1.")
    item_id = _positive_int(document["itemId"], "itemId")
    episode = _positive_int(document["episode"], "episode")
    evidence_fingerprint = _fingerprint(
        document["evidenceFingerprint"],
        "evidenceFingerprint",
    )
    if item_id != request.item_id:
        raise ValueError("Judgment result itemId does not match the request.")
    if episode != request.episode:
        raise ValueError("Judgment result episode does not match the request.")
    if evidence_fingerprint != request.evidence_fingerprint:
        raise ValueError(
            "Judgment result evidenceFingerprint does not match the request."
        )
    try:
        decision = JudgmentDecision(document["decision"])
    except (TypeError, ValueError) as error:
        raise ValueError("Judgment result decision is invalid.") from error
    summary = _nonempty_string(document["summary"], "summary", maximum=2_000)
    evidence_ids = tuple(
        _nonempty_string(value, "evidenceIds entry")
        for value in _sequence(document["evidenceIds"], "evidenceIds")
    )
    if len(set(evidence_ids)) != len(evidence_ids):
        raise ValueError("evidenceIds must contain unique values.")
    foreign_evidence = set(evidence_ids) - set(request.evidence_ids)
    if foreign_evidence:
        raise ValueError("evidenceIds must come from the request.")
    in_scope_job_ids = tuple(
        _positive_int(value, "inScopeJobIds entry")
        for value in _sequence(document["inScopeJobIds"], "inScopeJobIds")
    )
    if len(set(in_scope_job_ids)) != len(in_scope_job_ids):
        raise ValueError("inScopeJobIds must contain unique values.")
    failed_jobs = {
        job.job_id: job
        for job in request.failed_jobs
        if (job.conclusion or "").casefold()
        in {"failure", "failed", "timed_out"}
    }
    if any(job_id not in failed_jobs for job_id in in_scope_job_ids):
        raise ValueError(
            "inScopeJobIds must identify failed or timed-out request jobs."
        )
    copilot_request = _optional_nonempty_string(
        document["copilotRequest"],
        "copilotRequest",
        maximum=8_000,
    )
    if decision in {JudgmentDecision.ASSIGN, JudgmentDecision.FOLLOW_UP}:
        if copilot_request is None:
            raise ValueError(
                "copilotRequest is required for assign and follow_up."
            )
    elif copilot_request is not None:
        raise ValueError(
            "copilotRequest is only valid for assign and follow_up."
        )
    if decision is JudgmentDecision.ASSIGN:
        if not in_scope_job_ids:
            raise ValueError("Initial assign requires nonempty inScopeJobIds.")
        if (
            request.task_id is not None
            or request.pull_request_number is not None
            or request.pull_request_head_sha is not None
            or request.pull_request_head_ref is not None
            or request.pull_request_base_ref is not None
            or request.pull_request_observed_at is not None
        ):
            raise ValueError(
                "Initial assign requests cannot already have a task or pull request."
            )
    if (
        decision is JudgmentDecision.DEFER_ORDINARY_TEST
        and in_scope_job_ids
    ):
        raise ValueError(
            "defer_ordinary_test requires empty inScopeJobIds."
        )
    if decision is JudgmentDecision.FOLLOW_UP:
        if (
            request.issue_number is None
            or request.task_id is None
            or request.pull_request_number is None
            or request.pull_request_head_sha is None
            or request.pull_request_head_ref is None
            or request.pull_request_base_ref is None
            or request.pull_request_observed_at is None
        ):
            raise ValueError(
                "follow_up requires the exact owned issue, task, pull request, "
                "and fresh head identity."
            )
        if request.followup_count >= 2:
            raise ValueError("follow_up is limited to fewer than two follow-ups.")
    return JudgmentResult(
        schema_version=1,
        item_id=item_id,
        episode=episode,
        evidence_fingerprint=evidence_fingerprint,
        decision=decision,
        summary=summary,
        evidence_ids=evidence_ids,
        in_scope_job_ids=in_scope_job_ids,
        copilot_request=copilot_request,
    )
