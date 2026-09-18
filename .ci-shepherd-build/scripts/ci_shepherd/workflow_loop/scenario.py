from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from collections.abc import Collection
from typing import Protocol, TYPE_CHECKING

from .models import (
    ActionKind,
    ActionState,
    JudgmentRequest,
    JudgmentResult,
    WorkflowItem,
    WorkState,
)

if TYPE_CHECKING:
    from .state import WorkflowLoopStore


class NextStep(StrEnum):
    WAIT_FOR_CHANGE = "wait_for_change"
    WAIT_FOR_READ = "wait_for_read"
    WAIT_FOR_OWNED_WORK = "wait_for_owned_work"
    WAIT_FOR_PR = "wait_for_pr"
    WAIT_FOR_CI = "wait_for_ci"
    WAIT_FOR_HUMAN = "wait_for_human"
    WAIT_FOR_CAPACITY = "wait_for_capacity"
    QUEUE_JUDGMENT = "queue_judgment"
    PREPARE_ACTION = "prepare_action"
    OBSERVE_EXTERNAL = "observe_external"
    NEEDS_ATTENTION = "needs_attention"


@dataclass(frozen=True, slots=True)
class ConfirmedIssueCreation:
    worker_id: str
    item_id: int
    episode: int
    evidence_fingerprint: str
    issue_number: int

    def __post_init__(self) -> None:
        if not self.worker_id:
            raise ValueError("worker_id must be nonempty.")
        for name, value in (
            ("item_id", self.item_id),
            ("episode", self.episode),
            ("issue_number", self.issue_number),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer.")
        if not self.evidence_fingerprint:
            raise ValueError("evidence_fingerprint must be nonempty.")


@dataclass(frozen=True, slots=True)
class ItemTransition:
    item: WorkflowItem
    next_step: NextStep
    history_event: str
    summary: str
    action_kind: ActionKind | None = None
    judgment_round: int | None = None
    retain_judgment: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.item, WorkflowItem):
            raise ValueError("item must be a WorkflowItem.")
        if not isinstance(self.next_step, NextStep):
            raise ValueError("next_step must be a NextStep.")
        if not self.history_event:
            raise ValueError("history_event must be nonempty.")
        if not self.summary:
            raise ValueError("summary must be nonempty.")
        if self.action_kind is not None and not isinstance(
            self.action_kind,
            ActionKind,
        ):
            raise ValueError("action_kind must be an ActionKind or null.")
        if self.judgment_round is not None and (
            not isinstance(self.judgment_round, int)
            or isinstance(self.judgment_round, bool)
            or self.judgment_round < 0
        ):
            raise ValueError("judgment_round must be nonnegative or null.")
        if not isinstance(self.retain_judgment, bool):
            raise ValueError("retain_judgment must be a boolean.")


@dataclass(frozen=True, slots=True)
class ScenarioObservation:
    value: object
    request_count: int
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScenarioDiscovery:
    item: WorkflowItem
    refresh: object


@dataclass(frozen=True, slots=True)
class JudgmentPreparation:
    request: JudgmentRequest | None
    item: WorkflowItem
    request_count: int
    errors: tuple[str, ...]


class CiScenario(Protocol):
    name: str

    def observe(
        self,
        *,
        repository: str,
        branch: str,
        tracked_items: tuple[WorkflowItem, ...],
        workflow_ids: Collection[int] | None,
    ) -> ScenarioObservation: ...

    def discover(
        self,
        store: WorkflowLoopStore,
        observation: ScenarioObservation,
        items: tuple[WorkflowItem, ...],
    ) -> tuple[ScenarioDiscovery, ...]: ...

    def owns(self, item: WorkflowItem) -> bool: ...

    def priority(self, item: WorkflowItem) -> int: ...

    def refresh(
        self,
        item: WorkflowItem,
        *,
        judgment: JudgmentResult | None,
    ) -> object: ...

    def normalize_item(
        self,
        store: WorkflowLoopStore,
        item: WorkflowItem,
        refresh: object,
    ) -> WorkflowItem: ...

    def assess(
        self,
        item: WorkflowItem,
        refresh: object,
        *,
        now: str,
        request: JudgmentRequest | None,
        judgment: JudgmentResult | None,
        confirmed_issue: ConfirmedIssueCreation | None,
        worker_state: WorkState | None,
        action_state: ActionState | None,
        capacity_available: bool,
    ) -> ItemTransition: ...

    def action_for_judgment(
        self,
        item: WorkflowItem,
        judgment: JudgmentResult | None,
    ) -> ActionKind | None: ...

    def prepare_judgment(
        self,
        *,
        store: WorkflowLoopStore,
        item: WorkflowItem,
        refresh: object,
        judgment_round: int,
        worker_id: str,
        session_id: str,
    ) -> JudgmentPreparation: ...


class WriteResult(Protocol):
    status: str
    reason: str
    action_ids: tuple[str, ...]
    issue_number: int | None
    task_id: str | None
    newly_confirmed: bool


class EffectWriter(Protocol):
    def execute(
        self,
        request: JudgmentRequest,
        result: JudgmentResult,
        *,
        pass_id: str,
        owner_id: str,
    ) -> WriteResult: ...
