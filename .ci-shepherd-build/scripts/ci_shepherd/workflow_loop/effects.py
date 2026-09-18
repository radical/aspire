from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from .models import (
    ActionCompletion,
    ActionIntent,
    ActionKind,
    ActionState,
    ActionView,
)
from .state import WorkflowLoopStore


EffectStatus = Literal[
    "proposed",
    "confirmed",
    "capacity_wait",
    "superseded",
    "unavailable",
    "stale",
    "uncertain",
]


@dataclass(frozen=True, slots=True)
class EffectResult:
    status: EffectStatus
    reason: str
    action_id: str | None = None
    value: Any = None


class GitHubEffectExecutor:
    """Durable allowed-effect invocation shared by registered CI scenarios."""

    def __init__(
        self,
        *,
        store: WorkflowLoopStore,
        clock: Callable[[], str],
        active_item_limit: int,
    ) -> None:
        self._store = store
        self._clock = clock
        self._active_item_limit = active_item_limit

    def execute(
        self,
        intent: ActionIntent,
        *,
        pass_id: str,
        owner_id: str,
        guard: Callable[[], EffectResult | None],
        call: Callable[[], object],
        validate: Callable[[object], Any],
        propose_only: bool = False,
    ) -> EffectResult:
        if propose_only:
            guarded = guard()
            if guarded is not None:
                return guarded
            # A proposal is not a prepared invocation and owns no capacity.
            # Retain the complete intent rather than reconstructing its payload.
            self._store.record_history(
                intent.item_id,
                recorded_at=self._clock(),
                event="proposed",
                summary="PROPOSED: external effect was not invoked.",
                detail={
                    "status": "PROPOSED",
                    "actionId": intent.action_id,
                    "itemId": intent.item_id,
                    "episode": intent.episode,
                    "kind": intent.kind.value,
                    "ordinal": intent.ordinal,
                    "payload": dict(intent.payload),
                },
            )
            return EffectResult(
                "proposed",
                "External effect retained as a proposal; no invocation occurred.",
                intent.action_id,
            )
        existing = self._action(intent.action_id)
        if existing is not None:
            if (
                existing.item_id != intent.item_id
                or existing.episode != intent.episode
                or existing.kind is not intent.kind
                or existing.ordinal != intent.ordinal
                or dict(existing.payload) != dict(intent.payload)
            ):
                return EffectResult(
                    "stale",
                    "Prepared action payload does not match the current request.",
                    existing.action_id,
                )
            if existing.state is ActionState.CONFIRMED:
                return EffectResult(
                    "confirmed",
                    "The action was already confirmed.",
                    existing.action_id,
                    (
                        existing.remote_number
                        if existing.kind is ActionKind.CREATE_ISSUE
                        else existing.remote_task_id
                    ),
                )
            if existing.state in {
                ActionState.INVOKING,
                ActionState.UNCERTAIN,
            }:
                return EffectResult(
                    "uncertain",
                    "The action was already invoked and cannot be retried.",
                    existing.action_id,
                )
            if existing.state is not ActionState.PREPARED:
                return EffectResult(
                    "stale",
                    f"The action is already {existing.state.value}.",
                    existing.action_id,
                )
        elif not self._store.prepare_action(
            intent,
            capacity_limit=self._active_item_limit,
        ):
            return EffectResult(
                "capacity_wait",
                "Active-item capacity is unavailable.",
            )

        guarded = guard()
        if guarded is not None:
            if guarded.status in {"stale", "superseded"}:
                self._store.complete_action(
                    ActionCompletion(
                        action_id=intent.action_id,
                        state=ActionState.SUPERSEDED,
                        completed_at=self._clock(),
                        remote_number=None,
                        remote_task_id=None,
                        error=guarded.reason,
                    )
                )
            return EffectResult(
                guarded.status,
                guarded.reason,
                intent.action_id,
                guarded.value,
            )

        if not self._store.begin_action_invocation(
            intent.action_id,
            pass_id=pass_id,
            owner_id=owner_id,
            invoked_at=self._clock(),
        ):
            action = self._action(intent.action_id)
            assert action is not None
            return self._existing(action)
        try:
            value = validate(call())
        except Exception as error:
            message = f"{type(error).__name__}: {error}"
            self._store.complete_action(
                ActionCompletion(
                    action_id=intent.action_id,
                    state=ActionState.UNCERTAIN,
                    completed_at=self._clock(),
                    remote_number=None,
                    remote_task_id=None,
                    error=message,
                )
            )
            return EffectResult(
                "uncertain",
                message,
                intent.action_id,
            )

        self._store.complete_action(
            ActionCompletion(
                action_id=intent.action_id,
                state=ActionState.CONFIRMED,
                completed_at=self._clock(),
                remote_number=(
                    value
                    if intent.kind is ActionKind.CREATE_ISSUE
                    else None
                ),
                remote_task_id=(
                    value
                    if intent.kind is not ActionKind.CREATE_ISSUE
                    else None
                ),
                error=None,
            )
        )
        return EffectResult(
            "confirmed",
            "The remote effect was confirmed.",
            intent.action_id,
            value,
        )

    def _action(self, action_id: str) -> ActionView | None:
        return next(
            (
                action
                for action in self._store.list_actions()
                if action.action_id == action_id
            ),
            None,
        )

    @staticmethod
    def _existing(action: ActionView) -> EffectResult:
        if action.state is ActionState.CONFIRMED:
            return EffectResult(
                "confirmed",
                "The action was already confirmed.",
                action.action_id,
                (
                    action.remote_number
                    if action.kind is ActionKind.CREATE_ISSUE
                    else action.remote_task_id
                ),
            )
        if action.state in {
            ActionState.INVOKING,
            ActionState.UNCERTAIN,
        }:
            return EffectResult(
                "uncertain",
                "The action was already invoked and cannot be retried.",
                action.action_id,
            )
        return EffectResult(
            "unavailable",
            "The action is prepared and awaits fresh invocation.",
            action.action_id,
        )
