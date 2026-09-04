"""Production ``PolicyBudgetValidator`` backed by a ``CoordinatorStateStore``.

This is the concrete implementation ``ActionEventStore`` (see
:mod:`ci_shepherd.execution_state`) calls, through its optional
``PolicyBudgetValidator`` protocol, immediately before a brand-new intent is
durably appended. It:

- acquires the coordinator's own policy lock only after the caller's
  action-events lock is already held (the caller always invokes
  ``reservation_guard`` from inside ``ActionEventStore``'s own
  ``with self._locked():`` block), matching the required single-machine
  lock order ``action-events.lock -> policy-events.lock``. This module
  never acquires the action-events lock itself, so that ordering cannot be
  inverted from here;
- re-reads the current policy/decision projection from scratch under that
  lock rather than trusting any cached view, so a just-completed revocation
  is always visible;
- keeps the policy lock held until the caller has durably appended (and
  fsynced) the new intent, closing the validate-then-revoke gap.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
from typing import Any, Iterator, Mapping, Sequence

from .authorization import AuthorizationGrant
from .coordinator_state import CoordinatorStateStore
from .execution_state import ExecutionBudgetError, ExecutionStateError
from .models import stable_json
from .operation_policy import (
    HARD_MAX_PER_RUN,
    HARD_MAX_ROLLING_24H,
    OperationPolicyError,
    load_operation_policy_document,
)
from .timeutils import parse_aware_iso8601

_ROLLING_WINDOW = timedelta(hours=24)


class CoordinatorPolicyBudgetValidator:
    """``PolicyBudgetValidator`` that enforces standing policy and repository
    hard ceilings against a ``CoordinatorStateStore``'s ledger.
    """

    def __init__(self, coordinator_store: CoordinatorStateStore) -> None:
        self._coordinator_store = coordinator_store

    @contextmanager
    def reservation_guard(
        self,
        *,
        grant: AuthorizationGrant,
        action_events: Sequence[Mapping[str, Any]],
        intent: Mapping[str, Any],
        at: datetime,
    ) -> Iterator[None]:
        # ``reservation_projection`` acquires the policy lock and keeps it
        # held for the lifetime of this ``with`` block -- including through
        # the caller's own subsequent ``_append_event(intent)`` and its
        # fsync, since the caller wraps our returned context around that
        # append. That closes the validate-then-revoke gap: a concurrent
        # revocation cannot interleave between our read and the durable
        # append it is meant to gate.
        with self._coordinator_store.reservation_projection(
            grant.repository, now=at
        ) as projection:
            self._validate(
                grant=grant,
                action_events=action_events,
                intent=intent,
                at=at,
                projection=projection,
            )
            yield

    def _validate(
        self,
        *,
        grant: AuthorizationGrant,
        action_events: Sequence[Mapping[str, Any]],
        intent: Mapping[str, Any],
        at: datetime,
        projection: Mapping[str, Any],
    ) -> None:
        repository = grant.repository
        snapshot_id = intent["snapshotId"]
        repo_events = [
            event
            for event in action_events
            if event.get("repository") == repository
            and event.get("eventType") in ("intent", "terminal")
        ]

        # Every repository intent/terminal considered here is validated and
        # deduplicated to exactly one canonical record per unique actionId,
        # preferring that action's own intent (the authoritative source of
        # its true occurrence time and, for autonomous intents, its runId
        # and operationClass) over a later terminal. A terminal-only record
        # (a legacy import that never recorded its own intent in this
        # ledger) falls back to the terminal's own fields. This is also
        # where malformed ledger data is rejected fail-closed, before any
        # budget arithmetic runs.
        canonical = _canonical_action_records(repo_events)

        # Repository-wide hard ceilings bind every intent, autonomous or
        # legacy alike, and the rolling window uses each action's canonical
        # recordedAt above, so a crash mid-flight cannot reopen a slot once
        # the intent is durable.
        #
        # "This run" is scoped by the new intent's own runId when it has
        # one (autonomous intents always do): a record counts toward this
        # run's ceiling only if it shares that exact runId, so a rescan
        # that mints a new snapshotId for the same run does not reopen the
        # run's exhausted slot, and a distinct run that happens to share a
        # snapshotId gets its own independent slot. Legacy/production-pilot
        # intents carry no runId at all, so they fall back to grouping by
        # snapshotId -- the one identifier every intent carries regardless
        # of licensing -- rather than either disappearing from hard-ceiling
        # accounting or being counted globally forever.
        new_run_id = intent.get("runId")
        used_this_run = sum(
            1
            for record in canonical.values()
            if (
                (new_run_id is not None and record["runId"] == new_run_id)
                or (record["runId"] is None and record["snapshotId"] == snapshot_id)
            )
        )
        if used_this_run + 1 > HARD_MAX_PER_RUN:
            raise ExecutionBudgetError(
                "Repository hard ceiling for this run is exhausted."
            )

        window_start = at - _ROLLING_WINDOW
        used_rolling = sum(
            1
            for record in canonical.values()
            if window_start <= record["recordedAt"] <= at
        )
        if used_rolling + 1 > HARD_MAX_ROLLING_24H:
            raise ExecutionBudgetError(
                "Repository hard ceiling for the rolling 24h window is exhausted."
            )

        license_ = grant.autonomous_policy_license
        if license_ is None:
            return  # Legacy/production-pilot grant: no standing per-class caps.

        policy = _load_effective_policy(projection)
        action_id = intent["actionId"]
        target = intent["target"]
        target_key = f"{target['kind']}:{target['number']}"
        if policy is not None and policy.active_at(at):
            if (
                action_id in policy.denied_action_ids
                or target_key in policy.denied_targets
            ):
                raise ExecutionStateError(
                    f"Standing policy denies actionId {action_id!r}."
                )

        matching_decision = _find_exact_decision(
            projection, action_id=action_id, proposals_digest=grant.proposals_digest
        )
        if (
            matching_decision is not None
            and matching_decision.get("decision") == "reject-once"
        ):
            raise ExecutionStateError(
                f"Exact decision rejects actionId {action_id!r}."
            )

        license_source = license_.license_source
        is_exact_approval = license_source.startswith("decision:")
        if license_source.startswith("policy:"):
            if (
                policy is None
                or policy.revision_id != license_source
                or policy.status != "active"
                or not policy.active_at(at)
            ):
                raise ExecutionBudgetError(
                    f"Licensing policy revision {license_source!r} is no "
                    "longer effective."
                )
        elif is_exact_approval:
            if (
                matching_decision is None
                or matching_decision.get("decision") != "approve-once"
                or f"decision:{matching_decision.get('eventRevision')}"
                != license_source
            ):
                raise ExecutionBudgetError(
                    f"Exact approval {license_source!r} is no longer effective."
                )
        else:
            raise ExecutionStateError(
                f"Unsupported licenseSource {license_source!r}."
            )

        for prerequisite in intent.get("satisfiedPrerequisites") or ():
            _revalidate_prerequisite(prerequisite, action_events)

        if is_exact_approval:
            # Exact approval bypasses standing per-class caps, but the
            # intent it licenses still carries operationClass and is still
            # counted by future usage aggregation below -- it is a bypass
            # of the cap check, not an exemption from being counted.
            return

        if policy is None or not policy.active_at(at):
            raise ExecutionBudgetError(
                "No active standing policy licenses this operation class."
            )
        op_class = license_.operation_class
        class_policy = policy.operation_classes[op_class]
        if not class_policy.enabled:
            raise ExecutionBudgetError(
                f"Operation class {op_class!r} is disabled by standing policy."
            )

        class_records = {
            action_id: record
            for action_id, record in canonical.items()
            if record["operationClass"] == op_class
        }
        run_id = intent.get("runId")
        class_used_this_run = sum(
            1 for record in class_records.values() if record["runId"] == run_id
        )
        if class_used_this_run + 1 > class_policy.max_per_run:
            raise ExecutionBudgetError(
                f"Standing per-run cap exhausted for class {op_class!r}."
            )
        class_used_rolling = sum(
            1
            for record in class_records.values()
            if window_start <= record["recordedAt"] <= at
        )
        if class_used_rolling + 1 > class_policy.max_rolling_24h:
            raise ExecutionBudgetError(
                f"Standing rolling-24h cap exhausted for class {op_class!r}."
            )


def _canonical_action_records(
    repo_events: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Validate and collapse repository intent/terminal events to one record
    per unique actionId.

    Every event considered here has its actionId and recordedAt validated
    up front (before any budget arithmetic), raising ``ExecutionStateError``
    -- not silently discarding or collapsing malformed entries into a
    shared falsy bucket, which would undercount and fail OPEN -- if either
    is missing or unusable.

    An action's own intent event, when one exists in this ledger, is always
    authoritative: its recordedAt is that action's true, immutable
    occurrence time (a later terminal must never refresh it back into the
    rolling window), and it is the only event that ever carries an
    autonomous intent's own runId/operationClass. A terminal-only record
    (no corresponding intent in this ledger -- a legacy import) falls back
    to that terminal's own fields.
    """
    intents: dict[str, dict[str, Any]] = {}
    terminals: dict[str, dict[str, Any]] = {}
    for event in repo_events:
        action_id = _require_valid_action_id(event.get("actionId"))
        recorded_at = _require_valid_recorded_at(event.get("recordedAt"))
        record = {
            "recordedAt": recorded_at,
            "snapshotId": event.get("snapshotId"),
            "runId": event.get("runId"),
            "operationClass": event.get("operationClass"),
        }
        if event.get("eventType") == "intent":
            intents.setdefault(action_id, record)
        else:
            terminals.setdefault(action_id, record)
    canonical = dict(terminals)
    canonical.update(intents)
    return canonical


def _require_valid_action_id(action_id: object) -> str:
    if not isinstance(action_id, str) or not action_id:
        raise ExecutionStateError(
            "Repository ledger contains an intent/terminal event with a "
            "missing or invalid actionId; refusing to reserve until the "
            "ledger is trustworthy."
        )
    return action_id


def _require_valid_recorded_at(recorded_at: object) -> datetime:
    if not isinstance(recorded_at, str):
        raise ExecutionStateError(
            "Repository ledger contains an intent/terminal event with a "
            "missing or non-string recordedAt; refusing to reserve until "
            "the ledger is trustworthy."
        )
    try:
        return parse_aware_iso8601(recorded_at, "recordedAt")
    except ValueError as exc:
        raise ExecutionStateError(
            "Repository ledger contains an intent/terminal event with an "
            "unparseable recordedAt; refusing to reserve until the ledger "
            "is trustworthy."
        ) from exc


def _load_effective_policy(projection: Mapping[str, Any]):
    raw = projection.get("effectivePolicy")
    if raw is None:
        return None
    # `effectivePolicy` is the strict policy document plus a projection-only
    # `policyDigest` field; strip it before strict re-parsing.
    stripped = {key: value for key, value in raw.items() if key != "policyDigest"}
    try:
        return load_operation_policy_document(stripped)
    except OperationPolicyError as exc:
        raise ExecutionStateError(
            f"Coordinator effective policy is invalid: {exc}"
        ) from exc


def _find_exact_decision(
    projection: Mapping[str, Any], *, action_id: str, proposals_digest: str
) -> Mapping[str, Any] | None:
    for entry in projection.get("exactDecisions", []):
        if (
            entry.get("actionId") == action_id
            and entry.get("proposalDigest") == proposals_digest
        ):
            return entry
    return None


def _revalidate_prerequisite(
    prerequisite: Mapping[str, Any], action_events: Sequence[Mapping[str, Any]]
) -> None:
    dependency_action_id = prerequisite["actionId"]
    expected_digest = prerequisite["eventDigest"]
    terminal_events = [
        event
        for event in action_events
        if event.get("eventType") == "terminal"
        and event.get("actionId") == dependency_action_id
    ]
    if not terminal_events:
        raise ExecutionBudgetError(
            f"Prerequisite {dependency_action_id!r} is no longer terminal."
        )
    latest_terminal = max(
        terminal_events, key=lambda event: event.get("recordedAt", "")
    )
    actual_digest = "sha256:" + hashlib.sha256(
        stable_json(latest_terminal).encode("utf-8")
    ).hexdigest()
    if actual_digest != expected_digest:
        raise ExecutionBudgetError(
            f"Prerequisite {dependency_action_id!r} terminal digest changed."
        )
