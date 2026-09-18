from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime
from enum import StrEnum
import os
from pathlib import Path
import socket
import time
import uuid

from ci_shepherd.jsonl import exclusive_file_lock

from .models import (
    ActionKind,
    ActionState,
    JudgmentRequest,
    TaskState,
    WorkerReservation,
    WorkflowItem,
    WorkState,
)
from .scenario import (
    CiScenario,
    ConfirmedIssueCreation,
    EffectWriter,
    JudgmentPreparation,
    NextStep,
    ScenarioDiscovery,
    ScenarioObservation,
)
from .state import WorkflowLoopStore
from .worker import (
    JudgmentWorkerLauncher,
    WorkerLaunchStatus,
    WorkerObservation,
)


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
    would_do: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _QueueResult:
    launched: int
    request_count: int
    errors: tuple[str, ...]


class CiCoordinator:
    """Shared GitHub CI pass, capacity, worker, and effect coordinator."""

    def __init__(
        self,
        *,
        state_directory: Path,
        repository: str,
        branch: str,
        store: WorkflowLoopStore,
        scenarios: Sequence[CiScenario],
        launcher: JudgmentWorkerLauncher,
        writer: EffectWriter | None,
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
        if not scenarios:
            raise ValueError("At least one CI scenario must be registered.")
        names = tuple(scenario.name for scenario in scenarios)
        if len(set(names)) != len(names):
            raise ValueError("CI scenario names must be unique.")
        self._state_directory = state_directory
        self._repository = repository
        self._branch = branch
        self._store = store
        self._scenarios = tuple(scenarios)
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
        scenario_requests = 0
        errors: list[str] = []
        would_do: list[str] = []
        observations: list[tuple[CiScenario, ScenarioObservation]] = []
        refreshes: dict[int, object] = {}
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
            self._validate_workflow_scope(items_before)
            for scenario in self._scenarios:
                scenario_items = tuple(
                    item
                    for item in items_before
                    if self._scenario_for_item(item) is scenario
                )
                observation = scenario.observe(
                    repository=self._repository,
                    branch=self._branch,
                    tracked_items=scenario_items,
                    workflow_ids=self._workflow_ids,
                )
                observations.append((scenario, observation))
                scenario_requests += observation.request_count
                errors.extend(observation.errors)
                discoveries = scenario.discover(
                    self._store,
                    observation,
                    scenario_items,
                )
                discovered_items += len(discoveries)
                for discovery in discoveries:
                    self._store.bind_item_scenario(
                        discovery.item.id,
                        scenario.name,
                    )
                refreshes.update(
                    {
                        discovery.item.id: discovery.refresh
                        for discovery in discoveries
                    }
                )

            items = sorted(
                self._store.list_items(),
                key=self._item_order,
            )
            workers = self._store.list_workers()
            for item in items:
                if item.id in refreshes:
                    continue
                scenario = self._scenario_for_item(item)
                worker = _worker_for_item(workers, item)
                observation = (
                    worker_observations.get(worker.worker_id)
                    if worker is not None
                    else None
                )
                judgment = (
                    observation.judgment
                    if observation is not None
                    else None
                )
                refresh = scenario.refresh(item, judgment=judgment)
                refreshes[item.id] = refresh
                scenario_requests += getattr(refresh, "request_count", 0)
                errors.extend(_read_errors(refresh))

            normalized_items: list[WorkflowItem] = []
            for persisted in items:
                scenario = self._scenario_for_item(persisted)
                refresh = refreshes[persisted.id]
                item = scenario.normalize_item(
                    self._store,
                    persisted,
                    refresh,
                )
                if item.last_progressed_at != persisted.last_progressed_at:
                    progressed_items += 1
                normalized_items.append(item)

            for item in sorted(normalized_items, key=self._item_order):
                scenario = self._scenario_for_item(item)
                refresh = refreshes[item.id]
                workers = self._store.list_workers()
                worker = _worker_for_item(workers, item)
                observation = (
                    worker_observations.get(worker.worker_id)
                    if worker is not None
                    else None
                )
                request = (
                    observation.request
                    if observation is not None
                    else None
                )
                judgment = (
                    observation.judgment
                    if observation is not None
                    else None
                )
                worker_state = (
                    observation.completion.state
                    if observation is not None
                    and observation.completion is not None
                    else worker.state if worker is not None else None
                )
                actions = self._store.list_actions()
                action = _action_for_item(actions, item)
                transition = scenario.assess(
                    item,
                    refresh,
                    now=started_at,
                    request=request,
                    judgment=judgment,
                    confirmed_issue=_confirmed_issue_for_request(
                        actions,
                        request,
                    ),
                    worker_state=worker_state,
                    action_state=(
                        action.state if action is not None else None
                    ),
                    capacity_available=(
                        len(self._store.active_item_ids())
                        < self._capacity_limit
                    ),
                )

                dry_queue = (
                    transition.next_step is NextStep.QUEUE_JUDGMENT
                    and mode is EffectMode.READ_ONLY
                )
                if not dry_queue and _meaningful_item_change(
                    item,
                    transition.item,
                ):
                    self._store.update_item(
                        transition.item,
                        history_event=transition.history_event,
                        summary=transition.summary,
                        detail={"nextStep": transition.next_step.value},
                    )
                    if (
                        transition.item.last_progressed_at
                        != item.last_progressed_at
                    ):
                        progressed_items += 1

                if dry_queue:
                    note = (
                        f"{scenario.name}:{item.id}:"
                        f"queue_judgment:{transition.judgment_round}"
                    )
                    would_do.append(note)
                    self._store.record_history(
                        item.id,
                        recorded_at=started_at,
                        event="would-do",
                        summary="Read-only mode would queue judgment.",
                        detail={"effect": note},
                    )
                    continue

                if (
                    worker is not None
                    and worker_state
                    in {WorkState.SUCCEEDED, WorkState.SUPERSEDED}
                    and not transition.retain_judgment
                    and transition.next_step is not NextStep.PREPARE_ACTION
                ):
                    self._store.consume_worker_result(
                        worker.worker_id,
                        consumed_at=started_at,
                    )

                if transition.next_step is NextStep.QUEUE_JUDGMENT:
                    queued = self._queue_judgment(
                        scenario,
                        transition.item,
                        refresh,
                        transition.judgment_round,
                        started_at,
                    )
                    launched_workers += queued.launched
                    scenario_requests += queued.request_count
                    errors.extend(queued.errors)
                    continue

                if (
                    transition.next_step is NextStep.PREPARE_ACTION
                    and request is not None
                    and judgment is not None
                ):
                    if mode is not EffectMode.LIVE:
                        note = (
                            f"{scenario.name}:{item.id}:"
                            f"{transition.action_kind.value}"
                        )
                        would_do.append(note)
                        self._store.record_history(
                            item.id,
                            recorded_at=started_at,
                            event="would-do",
                            summary=(
                                "No-effect mode retained a proposed action."
                            ),
                            detail={"effect": note},
                        )
                        continue
                    if self._writer is None:
                        raise RuntimeError(
                            "Live mode requires a configured writer."
                        )
                    write = self._writer.execute(
                        request,
                        judgment,
                        pass_id=pass_id,
                        owner_id=self._owner_id,
                    )
                    if (
                        write.status == "confirmed"
                        and write.task_id is not None
                    ):
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
            self._finish_failed_pass(
                pass_id,
                started_tick,
                request_start,
                discovered_items,
                progressed_items,
                confirmed_assignments,
                error,
            )
            raise

        completed_at = _timestamp(self._clock())
        duration_ms = max(
            0,
            int((self._monotonic() - started_tick) * 1000),
        )
        github_requests = self._github_requests(
            request_start,
            scenario_requests,
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
            would_do=tuple(would_do),
        )

    def _queue_judgment(
        self,
        scenario: CiScenario,
        item: WorkflowItem,
        refresh: object,
        judgment_round: int | None,
        queued_at: str,
    ) -> _QueueResult:
        if judgment_round is None:
            return _QueueResult(0, 0, ())
        worker_id = f"worker-{self._id_factory()}"
        session_id = str(uuid.uuid4())
        preparation: JudgmentPreparation = scenario.prepare_judgment(
            store=self._store,
            item=item,
            refresh=refresh,
            judgment_round=judgment_round,
            worker_id=worker_id,
            session_id=session_id,
        )
        request = preparation.request
        if request is None:
            return _QueueResult(
                0,
                preparation.request_count,
                preparation.errors,
            )
        paths = self._launcher.packet_paths(worker_id)
        reservation = WorkerReservation(
            worker_id=worker_id,
            item_id=preparation.item.id,
            episode=preparation.item.episode,
            evidence_fingerprint=preparation.item.evidence_fingerprint,
            session_id=session_id,
            request_path=str(paths.request),
            result_path=str(paths.result),
            detail_path=str(paths.detail),
            lifetime_lock_path=str(paths.lifetime_lock),
            queued_at=queued_at,
            judgment_round=judgment_round,
        )
        prepared = self._launcher.prepare(reservation, request)
        if prepared.status.value not in {
            "prepared",
            "already_prepared",
        }:
            return _QueueResult(
                0,
                preparation.request_count,
                preparation.errors,
            )
        if not self._store.reserve_worker(
            reservation,
            capacity_limit=self._capacity_limit,
        ):
            return _QueueResult(
                0,
                preparation.request_count,
                preparation.errors,
            )
        launched = self._launcher.launch(reservation)
        launch_error = (
            (launched.error,) if launched.error is not None else ()
        )
        return _QueueResult(
            int(launched.status is WorkerLaunchStatus.LAUNCHED),
            preparation.request_count,
            preparation.errors + launch_error,
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
                    observations[worker.worker_id] = (
                        self._launcher.observe(worker)
                    )
                continue
            observations[worker.worker_id] = self._launcher.observe(worker)
        return observations

    def _scenario_for_item(self, item: WorkflowItem) -> CiScenario:
        bound = self._store.item_scenario(item.id)
        if bound is not None:
            match = next(
                (
                    scenario
                    for scenario in self._scenarios
                    if scenario.name == bound
                ),
                None,
            )
            if match is None:
                raise ValueError(
                    f"CI item {item.id} belongs to unregistered scenario "
                    f"{bound!r}."
                )
            return match
        matches = tuple(
            scenario for scenario in self._scenarios if scenario.owns(item)
        )
        if len(matches) != 1:
            raise ValueError(
                f"CI item {item.id} must belong to exactly one scenario; "
                f"matched {[scenario.name for scenario in matches]!r}."
            )
        self._store.bind_item_scenario(item.id, matches[0].name)
        return matches[0]

    def _item_order(self, item: WorkflowItem) -> tuple[int, int, int]:
        scenario = self._scenario_for_item(item)
        return (
            scenario.priority(item),
            self._scenarios.index(scenario),
            item.id,
        )

    def _validate_workflow_scope(
        self,
        items: tuple[WorkflowItem, ...],
    ) -> None:
        if self._workflow_ids is None:
            return
        foreign = sorted(
            {
                item.workflow_id
                for item in items
                if item.workflow_id not in self._workflow_ids
            }
        )
        if foreign:
            raise ValueError(
                "Configured workflow IDs would exclude persisted "
                f"state: {foreign!r}."
            )

    def _finish_failed_pass(
        self,
        pass_id: str,
        started_tick: float,
        request_start: int,
        discovered_items: int,
        progressed_items: int,
        confirmed_assignments: int,
        error: Exception,
    ) -> None:
        completed_at = _timestamp(self._clock())
        duration_ms = max(
            0,
            int((self._monotonic() - started_tick) * 1000),
        )
        self._store.finish_pass(
            pass_id,
            completed_at=completed_at,
            duration_ms=duration_ms,
            github_request_count=self._github_requests(request_start, 0),
            discovered_items=discovered_items,
            progressed_items=progressed_items,
            confirmed_assignments=confirmed_assignments,
            error=f"{type(error).__name__}: {error}",
        )

    def _github_requests(self, started: int, fallback: int) -> int:
        if self._request_count is None:
            return fallback
        return max(0, self._request_count() - started)


def _worker_for_item(workers: Sequence[object], item: WorkflowItem):
    matches = [
        worker
        for worker in workers
        if worker.item_id == item.id
        and worker.episode == item.episode
        and worker.evidence_fingerprint == item.evidence_fingerprint
        and worker.consumed_at is None
    ]
    return matches[-1] if matches else None


def _action_for_item(actions: Sequence[object], item: WorkflowItem):
    matches = [
        action
        for action in actions
        if action.item_id == item.id and action.episode == item.episode
    ]
    return matches[-1] if matches else None


def _confirmed_issue_for_request(
    actions: Sequence[object],
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
