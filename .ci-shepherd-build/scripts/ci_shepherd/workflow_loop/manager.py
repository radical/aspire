from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime
from enum import StrEnum
import json
import os
from pathlib import Path
import socket
import time
import uuid

from ci_shepherd.jsonl import exclusive_file_lock
from ci_shepherd.observations import workflow_log_preview

from .models import (
    ActionKind,
    ActionState,
    ItemPhase,
    JobObservation,
    JudgmentDecision,
    JudgmentRequest,
    TaskState,
    WorkerReservation,
    WorkState,
    WorkflowItem,
)
from .reader import (
    ItemRefresh,
    ReaderSnapshot,
    RepairEvidenceResult,
    WorkflowReader,
)
from .reducer import ConfirmedIssueCreation, NextStep, reduce_item
from .state import WorkflowLoopStore
from .worker import (
    JudgmentWorkerLauncher,
    WorkerLaunchStatus,
    WorkerObservation,
    WorkerObservationStatus,
    WorkerPreparationStatus,
)
from .writer import WorkflowWriter


class EffectMode(StrEnum):
    READ_ONLY = "read-only"
    LOCAL_JUDGMENT = "local-judgment"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class PassResult:
    pass_id: str
    owner_id: str
    started_at: str
    completed_at: str
    duration_ms: int
    github_request_count: int
    discovered_items: int
    progressed_items: int
    launched_workers: int
    confirmed_assignments: int
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _QueueResult:
    launched: int
    request_count: int
    errors: tuple[str, ...]


class WorkflowLoopManager:
    def __init__(
        self,
        *,
        state_directory: Path,
        repository: str,
        branch: str,
        store: WorkflowLoopStore,
        reader: WorkflowReader,
        launcher: JudgmentWorkerLauncher,
        writer: WorkflowWriter | None,
        clock: Callable[[], datetime],
        id_factory: Callable[[], str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        workflow_ids: Collection[int] | None = None,
        capacity_limit: int = 2,
        request_count: Callable[[], int] | None = None,
    ) -> None:
        if not state_directory.is_absolute():
            raise ValueError("state_directory must be absolute.")
        if not repository.strip() or not branch.strip():
            raise ValueError("repository and branch must be nonempty.")
        if capacity_limit < 1:
            raise ValueError("capacity_limit must be positive.")
        self._state_directory = state_directory
        self._repository = repository
        self._branch = branch
        self._store = store
        self._reader = reader
        self._launcher = launcher
        self._writer = writer
        self._clock = clock
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._monotonic = monotonic
        self._workflow_ids = (
            tuple(sorted(set(workflow_ids)))
            if workflow_ids is not None
            else None
        )
        self._capacity_limit = capacity_limit
        self._request_count = request_count
        self._owner_id = (
            f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"
        )

    def run_pass(self, *, mode: EffectMode = EffectMode.READ_ONLY) -> PassResult:
        if not isinstance(mode, EffectMode):
            mode = EffectMode(mode)
        lock_path = self._state_directory / "workflow-loop.pass.lock"
        with exclusive_file_lock(lock_path):
            return self._run_locked(mode)

    def _run_locked(self, mode: EffectMode) -> PassResult:
        pass_id = self._id_factory()
        started = self._clock()
        started_at = _timestamp(started)
        started_tick = self._monotonic()
        request_start = self._request_count() if self._request_count else 0
        self._store.initialize(workflow_ids=self._workflow_ids)
        self._store.start_pass(pass_id, started_at)
        discovered_items = 0
        progressed_items = 0
        launched_workers = 0
        confirmed_assignments = 0
        additional_requests = 0
        errors: list[str] = []
        try:
            self._store.classify_orphaned_action_invocations(
                current_pass_id=pass_id,
                current_owner_id=self._owner_id,
                classified_at=started_at,
                error=(
                    "A prior process ended while the remote invocation "
                    "outcome was unknown."
                ),
            )
            worker_observations = self._observe_workers(mode)
            launched_workers += sum(
                observation is None
                for observation in worker_observations.values()
            )
            errors.extend(
                observation.error
                for observation in worker_observations.values()
                if observation is not None and observation.error is not None
            )

            items_before = self._store.list_items()
            if self._workflow_ids is not None:
                foreign = sorted(
                    {
                        item.workflow_id
                        for item in items_before
                        if item.workflow_id not in self._workflow_ids
                    }
                )
                if foreign:
                    raise ValueError(
                        "Configured workflow IDs would exclude persisted "
                        f"state: {foreign!r}."
                    )
            snapshot = self._reader.observe(
                repository=self._repository,
                branch=self._branch,
                tracked_items=items_before,
                workflow_ids=self._workflow_ids,
            )
            errors.extend(_read_errors(snapshot))
            new_refreshes = self._discover_failures(snapshot, items_before)
            discovered_items += len(new_refreshes)

            items = self._store.list_items()
            refreshes: dict[int, ItemRefresh] = dict(new_refreshes)
            for item in items:
                if item.id in refreshes:
                    continue
                worker = _worker_for_item(
                    self._store.list_workers(),
                    item,
                )
                observation = (
                    worker_observations.get(worker.worker_id)
                    if worker is not None
                    else None
                )
                action_grade = _judgment_action(
                    item,
                    observation.judgment
                    if observation is not None
                    else None,
                )
                refresh = self._reader.refresh_item(
                    item,
                    action=action_grade,
                )
                refreshes[item.id] = refresh
                errors.extend(_read_errors(refresh))

            for item in items:
                refresh = refreshes[item.id]
                if (
                    refresh.failure_run is not None
                    and refresh.failure_run.jobs_complete
                    and refresh.recovery != "passed"
                ):
                    item = self._store.upsert_failure(
                        refresh.failure_run,
                        refresh.observed_at,
                    )
                worker = _worker_for_item(
                    self._store.list_workers(),
                    item,
                )
                observation = (
                    worker_observations.get(worker.worker_id)
                    if worker is not None
                    else None
                )
                request = observation.request if observation is not None else None
                judgment = (
                    observation.judgment if observation is not None else None
                )
                worker_state = (
                    observation.completion.state
                    if observation is not None
                    and observation.completion is not None
                    else worker.state if worker is not None else None
                )
                action = _action_for_item(self._store.list_actions(), item)
                active_count = len(self._store.active_item_ids())
                confirmed_issue = _confirmed_issue_for_request(
                    self._store.list_actions(),
                    request,
                )
                transition = reduce_item(
                    item,
                    refresh,
                    now=started_at,
                    request=request,
                    judgment=judgment,
                    confirmed_issue=confirmed_issue,
                    worker_state=worker_state,
                    action_state=action.state if action is not None else None,
                    capacity_available=(
                        active_count < self._capacity_limit
                        and mode is not EffectMode.READ_ONLY
                    ),
                )

                if _meaningful_item_change(item, transition.item):
                    self._store.update_item(
                        transition.item,
                        history_event=transition.history_event,
                        summary=transition.summary,
                        detail={"nextStep": transition.next_step.value},
                    )
                    if transition.item.last_progressed_at != item.last_progressed_at:
                        progressed_items += 1

                if (
                    worker is not None
                    and worker_state
                    in {
                        WorkState.SUCCEEDED,
                        WorkState.SUPERSEDED,
                    }
                    and not transition.retain_judgment
                    and transition.next_step is not NextStep.PREPARE_ACTION
                ):
                    self._store.consume_worker_result(
                        worker.worker_id,
                        consumed_at=started_at,
                    )

                if (
                    transition.next_step is NextStep.QUEUE_JUDGMENT
                    and mode is not EffectMode.READ_ONLY
                ):
                    queued = self._queue_judgment(
                        transition.item,
                        refresh,
                        transition.judgment_round,
                        started_at,
                    )
                    launched_workers += queued.launched
                    additional_requests += queued.request_count
                    errors.extend(queued.errors)
                    continue
                if (
                    transition.next_step is NextStep.PREPARE_ACTION
                    and mode is EffectMode.LIVE
                    and request is not None
                    and judgment is not None
                ):
                    if self._writer is None:
                        raise RuntimeError(
                            "Live mode requires a configured WorkflowWriter."
                        )
                    write = self._writer.execute(
                        request,
                        judgment,
                        pass_id=pass_id,
                        owner_id=self._owner_id,
                    )
                    if write.status == "confirmed" and write.task_id is not None:
                        current = next(
                            candidate
                            for candidate in self._store.list_items()
                            if candidate.id == item.id
                        )
                        self._store.update_item(
                            replace(
                                current,
                                issue_number=write.issue_number,
                                task_id=write.task_id,
                                task_state=TaskState.QUEUED,
                            ),
                            history_event="assignment-confirmed",
                            summary="The owned repair task was confirmed.",
                            detail={
                                "actionIds": list(write.action_ids),
                                "issueNumber": write.issue_number,
                                "taskId": write.task_id,
                            },
                        )
                        if worker is not None:
                            self._store.consume_worker_result(
                                worker.worker_id,
                                consumed_at=started_at,
                            )
                        if write.newly_confirmed:
                            confirmed_assignments += 1
                    elif write.status in {
                        "stale",
                        "superseded",
                        "uncertain",
                    }:
                        if worker is not None:
                            self._store.consume_worker_result(
                                worker.worker_id,
                                consumed_at=started_at,
                            )
                        errors.append(
                            f"writer:{write.status}:{write.reason}"
                        )
                    elif write.status != "no_op":
                        errors.append(
                            f"writer:{write.status}:{write.reason}"
                        )
        except Exception as error:
            completed_at = _timestamp(self._clock())
            duration_ms = max(
                0,
                int((self._monotonic() - started_tick) * 1000),
            )
            github_requests = self._github_requests(request_start, 0)
            self._store.finish_pass(
                pass_id,
                completed_at=completed_at,
                duration_ms=duration_ms,
                github_request_count=github_requests,
                discovered_items=discovered_items,
                progressed_items=progressed_items,
                confirmed_assignments=confirmed_assignments,
                error=f"{type(error).__name__}: {error}",
            )
            raise

        completed_at = _timestamp(self._clock())
        duration_ms = max(0, int((self._monotonic() - started_tick) * 1000))
        github_requests = self._github_requests(
            request_start,
            snapshot.request_count
            + sum(refresh.request_count for refresh in refreshes.values())
            + additional_requests,
        )
        pass_error = "; ".join(errors) if errors else None
        self._store.finish_pass(
            pass_id,
            completed_at=completed_at,
            duration_ms=duration_ms,
            github_request_count=github_requests,
            discovered_items=discovered_items,
            progressed_items=progressed_items,
            confirmed_assignments=confirmed_assignments,
            error=pass_error,
        )
        return PassResult(
            pass_id=pass_id,
            owner_id=self._owner_id,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
            github_request_count=github_requests,
            discovered_items=discovered_items,
            progressed_items=progressed_items,
            launched_workers=launched_workers,
            confirmed_assignments=confirmed_assignments,
            errors=tuple(errors),
        )

    def _observe_workers(
        self,
        mode: EffectMode,
    ) -> dict[str, WorkerObservation | None]:
        observations: dict[str, WorkerObservation | None] = {}
        for worker in self._store.list_workers():
            if worker.consumed_at is not None:
                continue
            if (
                worker.state is WorkState.QUEUED
                and worker.launch_attempted_at is None
                and mode is not EffectMode.READ_ONLY
            ):
                launch = self._launcher.launch(worker)
                observations[worker.worker_id] = None
                if launch.status not in {
                    WorkerLaunchStatus.LAUNCHED,
                    WorkerLaunchStatus.ALREADY_ATTEMPTED,
                }:
                    observations[worker.worker_id] = self._launcher.observe(worker)
                continue
            observations[worker.worker_id] = self._launcher.observe(worker)
        return observations

    def _discover_failures(
        self,
        snapshot: ReaderSnapshot,
        items: tuple[WorkflowItem, ...],
    ) -> dict[int, ItemRefresh]:
        known_workflows = {item.workflow_id for item in items}
        candidates = [
            workflow
            for workflow in snapshot.workflows
            if workflow.key.workflow_id not in known_workflows
            and workflow.latest_completed is not None
            and workflow.latest_completed.conclusion in {"failure", "timed_out"}
        ]
        refreshes: dict[int, ItemRefresh] = {}
        for workflow in candidates:
            assert workflow.latest_completed is not None
            item = self._store.upsert_failure(
                workflow.latest_completed,
                snapshot.observed_at,
            )
            refreshes[item.id] = ItemRefresh(
                item_id=item.id,
                observed_at=snapshot.observed_at,
                runs=workflow.runs,
                failure_run=workflow.latest_completed,
                wait_run=None,
                recovery="failed",
                recovery_run=None,
                issue=None,
                task=None,
                pull_request=None,
                pre_write=False,
                complete=workflow.complete,
                errors=(),
                request_count=0,
            )
        return refreshes

    def _queue_judgment(
        self,
        item: WorkflowItem,
        refresh: ItemRefresh,
        judgment_round: int | None,
        queued_at: str,
    ) -> _QueueResult:
        if judgment_round is None or refresh.failure_run is None:
            return _QueueResult(0, 0, ())
        detail_requests = 0
        errors: tuple[str, ...] = ()
        failed_jobs = tuple(
            job
            for job in refresh.failure_run.jobs
            if (job.conclusion or "").casefold() in {"failure", "timed_out"}
        )
        if not refresh.failure_run.jobs_complete or not failed_jobs:
            detail = self._reader.read_run_details(
                refresh.failure_run,
                established_jobs=item.failed_jobs,
            )
            detail_requests = detail.request_count
            errors = tuple(_read_errors(detail))
            if detail.run is None or not detail.complete:
                current = next(
                    candidate
                    for candidate in self._store.list_items()
                    if candidate.id == item.id
                )
                self._store.update_item(
                    replace(
                        current,
                        phase=ItemPhase.OBSERVING_FAILURE,
                        read_status="unavailable",
                    ),
                    history_event="failure-detail-unavailable",
                    summary=(
                        "Failure detail is unavailable; judgment will retry "
                        "on a later pass."
                    ),
                    detail={"judgmentRound": judgment_round},
                )
                return _QueueResult(0, detail_requests, errors)
            refresh = replace(
                refresh,
                failure_run=detail.run,
                complete=refresh.complete and detail.complete,
                errors=refresh.errors + detail.errors,
            )
            item = self._store.upsert_failure(
                detail.run,
                refresh.observed_at,
            )
        repair_evidence = None
        if judgment_round > 0:
            repair_evidence = self._reader.read_repair_evidence(
                item,
                refresh=refresh,
            )
            detail_requests += repair_evidence.request_count
            errors += tuple(_read_errors(repair_evidence))
        worker_id = f"worker-{self._id_factory()}"
        session_id = str(uuid.uuid4())
        paths = self._launcher.packet_paths(worker_id)
        request = _judgment_request(
            item,
            refresh,
            worker_id=worker_id,
            session_id=session_id,
            judgment_round=judgment_round,
            repair_evidence=repair_evidence,
        )
        reservation = WorkerReservation(
            worker_id=worker_id,
            item_id=item.id,
            episode=item.episode,
            evidence_fingerprint=item.evidence_fingerprint,
            session_id=session_id,
            request_path=str(paths.request),
            result_path=str(paths.result),
            detail_path=str(paths.detail),
            lifetime_lock_path=str(paths.lifetime_lock),
            queued_at=queued_at,
            judgment_round=judgment_round,
        )
        prepared = self._launcher.prepare(reservation, request)
        if prepared.status not in {
            WorkerPreparationStatus.PREPARED,
            WorkerPreparationStatus.ALREADY_PREPARED,
        }:
            return _QueueResult(0, detail_requests, errors)
        if not self._store.reserve_worker(
            reservation,
            capacity_limit=self._capacity_limit,
        ):
            return _QueueResult(0, detail_requests, errors)
        launched = self._launcher.launch(reservation)
        launch_error = (
            (launched.error,) if launched.error is not None else ()
        )
        return _QueueResult(
            int(launched.status is WorkerLaunchStatus.LAUNCHED),
            detail_requests,
            errors + launch_error,
        )

    def _github_requests(self, started: int, fallback: int) -> int:
        if self._request_count is None:
            return fallback
        return max(0, self._request_count() - started)


def _judgment_request(
    item: WorkflowItem,
    refresh: ItemRefresh,
    *,
    worker_id: str,
    session_id: str,
    judgment_round: int,
    repair_evidence: RepairEvidenceResult | None = None,
) -> JudgmentRequest:
    failure = refresh.failure_run
    if failure is None:
        raise ValueError("Judgment requires an authoritative failure run.")
    failed_jobs = tuple(
        job
        for job in failure.jobs
        if (job.conclusion or "").casefold() in {"failure", "timed_out"}
    )
    if not failed_jobs:
        raise ValueError("Judgment requires failed or timed-out jobs.")
    evidence_ids = (
        f"run:{failure.run_id}:{failure.attempt}",
        *(
            f"job:{job.run_id}:{job.attempt}:{job.job_id}"
            for job in failed_jobs
        ),
        *(
            f"log:{job.job_id}"
            for job in failed_jobs
            if job.log_excerpt is not None
        ),
    )
    if repair_evidence is not None:
        evidence_ids = (
            *evidence_ids,
            *(
                f"repair-check:{check.check_run_id}"
                for check in repair_evidence.failed_checks
            ),
            *(
                f"repair-comment:{comment.comment_id}"
                for comment in repair_evidence.bot_comments
            ),
            *(
                f"repair-file:{index}:{file.path}"
                for index, file in enumerate(repair_evidence.files, start=1)
            ),
        )
    pull = refresh.pull_request
    prompt = _judgment_prompt(
        item,
        refresh,
        judgment_round,
        evidence_ids=tuple(evidence_ids),
        repair_evidence=repair_evidence,
    )
    return JudgmentRequest(
        worker_id=worker_id,
        session_id=session_id,
        item_id=item.id,
        episode=item.episode,
        evidence_fingerprint=item.evidence_fingerprint,
        round=judgment_round,
        repository=item.repository,
        branch=item.branch,
        workflow_id=item.workflow_id,
        workflow_path=item.workflow_path,
        failure_run=failure,
        failed_jobs=failed_jobs,
        evidence_ids=tuple(evidence_ids),
        issue_number=item.issue_number,
        task_id=item.task_id if judgment_round > 0 else None,
        pull_request_number=(
            pull.number if judgment_round > 0 and pull is not None else None
        ),
        pull_request_head_sha=(
            pull.head_sha if judgment_round > 0 and pull is not None else None
        ),
        pull_request_head_ref=(
            pull.head_ref if judgment_round > 0 and pull is not None else None
        ),
        pull_request_base_ref=(
            pull.base_ref if judgment_round > 0 and pull is not None else None
        ),
        pull_request_observed_at=(
            refresh.observed_at
            if judgment_round > 0 and pull is not None
            else None
        ),
        followup_count=item.followup_count,
        prompt=prompt,
    )


def _judgment_prompt(
    item: WorkflowItem,
    refresh: ItemRefresh,
    judgment_round: int,
    *,
    evidence_ids: tuple[str, ...],
    repair_evidence: RepairEvidenceResult | None = None,
) -> str:
    failure = refresh.failure_run
    assert failure is not None
    jobs = "\n".join(
        _prompt_job_line(job)
        for job in failure.jobs
        if (job.conclusion or "").casefold() in {"failure", "timed_out"}
    )
    if judgment_round == 0:
        context = "Initial main-workflow failure."
    else:
        task = refresh.task
        explanation = (
            task.explanation
            if task is not None
            and task.explanation_available
            and task.explanation is not None
            else "[unavailable]"
        )
        pull = refresh.pull_request
        context = (
            "Owned pull-request follow-up. "
            f"task={item.task_id!r} pr={item.pull_request_number!r} "
            f"head={(pull.head_ref if pull is not None else None)!r} "
            f"headSha={(pull.head_sha if pull is not None else None)!r} "
            f"base={(pull.base_ref if pull is not None else None)!r}. "
            f"Task explanation: {explanation[:4000]}"
        )
        if repair_evidence is not None:
            failed_checks = "\n".join(
                (
                    f"- checkRunId={check.check_run_id} name={check.name!r} "
                    f"conclusion={check.conclusion!r} "
                    f"log={(check.log_excerpt or '[unavailable]')[:2500]}"
                )
                for check in repair_evidence.failed_checks[:3]
            ) or "- [no failed check details available]"
            changed_files = "\n".join(
                f"- {file.status} {file.path}"
                for file in repair_evidence.files[:50]
            ) or "- [changed files unavailable]"
            bot_comments = "\n".join(
                f"- {comment.author}: {comment.body[:500]}"
                for comment in repair_evidence.bot_comments[:3]
            ) or "- [bot comments unavailable]"
            limitations = ", ".join(repair_evidence.limitations) or "none"
            context += (
                "\nRepair failed checks:\n"
                f"{failed_checks}\nChanged files:\n{changed_files}\n"
                f"Bot feedback:\n{bot_comments}\n"
                f"Evidence limitations: {limitations}"
            )
    body = (
        "Classify only the supplied CI evidence. Ordinary test failures must "
        "use decision defer_ordinary_test. Do not perform GitHub writes or "
        "execute copilotRequest.\n\n"
        f"{context}\n"
        f"Repository: {item.repository}\n"
        f"Workflow: {item.workflow_path}\n"
        f"Run: {failure.run_id} attempt {failure.attempt}\n"
        f"{jobs}"
    )
    failed_job_ids = [
        job.job_id
        for job in failure.jobs
        if (job.conclusion or "").casefold() in {"failure", "timed_out"}
    ]
    schema = (
        "\n\n"
        "Return exactly one compact JSON object with no Markdown or surrounding "
        "text. Copy these identity values exactly and preserve their JSON types:\n"
        f'- "schemaVersion": 1\n'
        f'- "itemId": {item.id}\n'
        f'- "episode": {item.episode}\n'
        f'- "evidenceFingerprint": '
        f"{json.dumps(item.evidence_fingerprint)}\n"
        f"- evidenceIds may contain only these exact strings: "
        f"{json.dumps(list(evidence_ids))}\n"
        f"- inScopeJobIds may contain only these integer IDs: "
        f"{json.dumps(failed_job_ids)}\n"
        "The remaining fields are decision, summary, and copilotRequest. "
        "decision must be assign, follow_up, observe_external, "
        "defer_ordinary_test, needs_attention, or no_action. For assign or "
        "follow_up, include the exact in-scope failed job IDs and a bounded "
        "string copilotRequest. For every other decision, inScopeJobIds must "
        "be [] and copilotRequest must be null."
    )
    return f"{body[:18_000]}{schema}"


def _prompt_job_line(job: JobObservation) -> str:
    log = job.log_excerpt
    excerpt = (
        "[unavailable]"
        if log is None
        else workflow_log_preview(log, 4_000)
    )
    return (
        f"- jobId={job.job_id} name={job.key.name!r} "
        f"conclusion={job.conclusion!r} "
        f"logSourceTruncated={str(job.log_truncated).lower()} "
        f"promptExcerpted={str(log is not None and len(log) > 4_000).lower()} "
        f"log={excerpt}"
    )


def _worker_for_item(workers, item: WorkflowItem):
    matches = [
        worker
        for worker in workers
        if worker.item_id == item.id
        and worker.episode == item.episode
        and worker.evidence_fingerprint == item.evidence_fingerprint
        and worker.consumed_at is None
    ]
    return matches[-1] if matches else None


def _action_for_item(actions, item: WorkflowItem):
    matches = [
        action
        for action in actions
        if action.item_id == item.id and action.episode == item.episode
    ]
    return matches[-1] if matches else None


def _judgment_action(item: WorkflowItem, judgment) -> ActionKind | None:
    if judgment is None:
        return None
    if judgment.decision is JudgmentDecision.FOLLOW_UP:
        return ActionKind.FOLLOW_UP
    if judgment.decision is JudgmentDecision.ASSIGN:
        return (
            ActionKind.CREATE_ISSUE
            if item.issue_number is None
            else ActionKind.ASSIGN_COPILOT
        )
    return None


def _confirmed_issue_for_request(
    actions,
    request: JudgmentRequest | None,
) -> ConfirmedIssueCreation | None:
    if request is None:
        return None
    action = next(
        (
            candidate
            for candidate in actions
            if candidate.item_id == request.item_id
            and candidate.episode == request.episode
            and candidate.kind is ActionKind.CREATE_ISSUE
            and candidate.state is ActionState.CONFIRMED
            and candidate.remote_number is not None
            and candidate.action_id.startswith(
                f"{request.worker_id}:{request.item_id}:{request.episode}:"
                f"{request.evidence_fingerprint}:create_issue:"
            )
        ),
        None,
    )
    if action is None:
        return None
    return ConfirmedIssueCreation(
        worker_id=request.worker_id,
        item_id=request.item_id,
        episode=request.episode,
        evidence_fingerprint=request.evidence_fingerprint,
        issue_number=action.remote_number,
    )


def _read_errors(value: object) -> list[str]:
    return [
        f"{error.scope}:{error.code}:{error.detail}"
        for error in getattr(value, "errors", ())
    ]


def _meaningful_item_change(
    original: WorkflowItem,
    updated: WorkflowItem,
) -> bool:
    ignored = {"last_checked_at"}
    return any(
        getattr(original, field.name) != getattr(updated, field.name)
        for field in fields(original)
        if field.name not in ignored
    )


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
