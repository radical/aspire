"""Crash-safe, append-only state for authorized mutation attempts."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Iterator, Mapping

from .authorization import AuthorizationGrant


class ExecutionStateError(RuntimeError):
    """Raised when persisted execution state is invalid or unavailable."""


class ExecutionBudgetError(ExecutionStateError):
    """Raised before an authorized grant would exceed its persisted budget."""


@dataclass(frozen=True, slots=True)
class ActionReservation:
    mode: str
    prior_terminal: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ActionExecution:
    reservation: ActionReservation
    _store: ActionEventStore
    _grant: AuthorizationGrant

    def append_terminal(
        self,
        *,
        result: Mapping[str, Any],
        at: datetime,
    ) -> Mapping[str, Any]:
        return self._store._append_terminal_locked(
            self._grant,
            result=result,
            at=at,
        )

    def prior_results(self, *, repository: str) -> dict[str, object]:
        return self._store._prior_results_locked(repository=repository)


_TERMINAL_OUTCOMES = frozenset(
    {"executed", "skipped", "stale", "failed", "indeterminate"}
)


class ActionEventStore:
    def __init__(
        self,
        state_dir: Path,
        *,
        lock_timeout_seconds: float = 5.0,
    ) -> None:
        self._state_dir = state_dir.expanduser()
        if self._state_dir.is_symlink() or any(
            parent.is_symlink() for parent in self._state_dir.parents
        ):
            raise ExecutionStateError("State directory cannot traverse a symlink.")
        self._state_dir = self._state_dir.resolve(strict=False)
        self._events_path = self._state_dir / "action-events.jsonl"
        self._lock_path = self._state_dir / "action-events.lock"
        self._migration_marker_path = (
            self._state_dir / "action-results-migration-v1.json"
        )
        self._lock_timeout_seconds = lock_timeout_seconds

    def migrate_legacy_results(self) -> None:
        with self._locked():
            if self._migration_marker_path.exists():
                if self._migration_marker_path.is_symlink():
                    raise ExecutionStateError(
                        "Legacy migration marker cannot be a symlink."
                    )
                return

            legacy_path = self._state_dir / "action-results.json"
            if not legacy_path.exists():
                self._write_migration_marker(imported_count=0)
                return
            if legacy_path.is_symlink():
                raise ExecutionStateError(
                    "Legacy action-results file cannot be a symlink."
                )

            legacy = _read_json_object(legacy_path, "legacy action results")
            repository = legacy.get("repository")
            results = legacy.get("results")
            if (
                legacy.get("schemaVersion") != 1
                or not isinstance(repository, str)
                or not isinstance(results, list)
                or not all(isinstance(result, dict) for result in results)
            ):
                raise ExecutionStateError("Legacy action results are malformed.")

            proposal_index: dict[str, dict[str, Any]] = {}
            runs_dir = self._state_dir / "runs"
            for proposals_path in sorted(
                runs_dir.glob("*/action-proposals.json")
                if runs_dir.exists()
                else ()
            ):
                if proposals_path.is_symlink():
                    raise ExecutionStateError(
                        "Legacy proposal document cannot be a symlink."
                    )
                document = _read_json_object(
                    proposals_path,
                    "legacy proposal document",
                )
                if document.get("repository") != repository:
                    continue
                snapshot_id = document.get("snapshotId")
                shepherd_author = document.get("shepherdAuthor")
                proposals = document.get("proposals")
                if (
                    not isinstance(snapshot_id, str)
                    or not isinstance(shepherd_author, str)
                    or not isinstance(proposals, list)
                ):
                    raise ExecutionStateError(
                        "Legacy proposal document is malformed."
                    )
                for proposal in proposals:
                    if not isinstance(proposal, dict):
                        raise ExecutionStateError(
                            "Legacy proposal entry is malformed."
                        )
                    action_id = proposal.get("actionId")
                    if not isinstance(action_id, str):
                        raise ExecutionStateError(
                            "Legacy proposal actionId is malformed."
                        )
                    indexed = {
                        "snapshotId": snapshot_id,
                        "shepherdAuthor": shepherd_author,
                        **proposal,
                    }
                    previous = proposal_index.get(action_id)
                    if previous is not None and previous != indexed:
                        raise ExecutionStateError(
                            "Legacy actionId has conflicting proposal records."
                        )
                    proposal_index[action_id] = indexed

            imported_events: list[dict[str, Any]] = []
            for result in results:
                action_id = result.get("actionId")
                outcome = result.get("outcome")
                proposal = proposal_index.get(str(action_id))
                if (
                    not isinstance(action_id, str)
                    or outcome not in _TERMINAL_OUTCOMES
                    or proposal is None
                ):
                    raise ExecutionStateError(
                        "Legacy result cannot be matched to an exact proposal."
                    )
                target_kind = str(proposal.get("targetKind") or "issue")
                target_number = proposal.get(
                    "targetNumber",
                    proposal.get("issueNumber"),
                )
                if (
                    not isinstance(target_number, int)
                    or isinstance(target_number, bool)
                    or target_number <= 0
                ):
                    raise ExecutionStateError(
                        "Legacy proposal target is malformed."
                    )
                body = proposal.get("body")
                imported_events.append(
                    {
                        "schemaVersion": 1,
                        "eventType": "terminal",
                        "recordedAt": str(
                            result.get("attemptedAt")
                            or "1970-01-01T00:00:00Z"
                        ),
                        "grantId": "legacy-import",
                        "repository": repository,
                        "snapshotId": proposal["snapshotId"],
                        "operation": proposal.get("operation"),
                        "target": {
                            "kind": target_kind,
                            "number": target_number,
                        },
                        "idempotencyKey": proposal.get("idempotencyKey"),
                        "bodyDigest": (
                            "sha256:"
                            + hashlib.sha256(body.encode("utf-8")).hexdigest()
                            if isinstance(body, str)
                            else None
                        ),
                        "expectedActorLogin": proposal["shepherdAuthor"],
                        **result,
                    }
                )

            for event in imported_events:
                self._append_event(event)
            self._write_migration_marker(imported_count=len(imported_events))

    def reserve(
        self,
        grant: AuthorizationGrant,
        *,
        action_id: str,
        chain_root: str,
        operation: str,
        target_kind: str,
        target_number: int,
        idempotency_key: str,
        body_digest: str | None,
        expected_actor_login: str,
        at: datetime,
    ) -> ActionReservation:
        intent = self._prepare_intent(
            grant,
            action_id=action_id,
            chain_root=chain_root,
            operation=operation,
            target_kind=target_kind,
            target_number=target_number,
            idempotency_key=idempotency_key,
            body_digest=body_digest,
            expected_actor_login=expected_actor_login,
            at=at,
        )
        with self._locked():
            return self._reserve_locked(grant, intent)

    @contextmanager
    def transaction(
        self,
        grant: AuthorizationGrant,
        *,
        action_id: str,
        chain_root: str,
        operation: str,
        target_kind: str,
        target_number: int,
        idempotency_key: str,
        body_digest: str | None,
        expected_actor_login: str,
        at: datetime,
    ) -> Iterator[ActionExecution]:
        intent = self._prepare_intent(
            grant,
            action_id=action_id,
            chain_root=chain_root,
            operation=operation,
            target_kind=target_kind,
            target_number=target_number,
            idempotency_key=idempotency_key,
            body_digest=body_digest,
            expected_actor_login=expected_actor_login,
            at=at,
        )
        with self._locked():
            reservation = self._reserve_locked(grant, intent)
            yield ActionExecution(reservation, self, grant)

    def _prepare_intent(
        self,
        grant: AuthorizationGrant,
        *,
        action_id: str,
        chain_root: str,
        operation: str,
        target_kind: str,
        target_number: int,
        idempotency_key: str,
        body_digest: str | None,
        expected_actor_login: str,
        at: datetime,
    ) -> dict[str, Any]:
        if grant.state_directory != self._state_dir:
            raise ExecutionStateError(
                "Grant state directory does not match the event store."
            )
        if action_id not in grant.allowed_action_ids:
            raise ExecutionStateError("Action is not enumerated by the grant.")
        if chain_root not in grant.allowed_chain_roots:
            raise ExecutionStateError("Action chain root is not allowed by the grant.")
        return {
            "schemaVersion": 1,
            "eventType": "intent",
            "recordedAt": _timestamp(at),
            "grantId": grant.grant_id,
            "repository": grant.repository,
            "snapshotId": grant.snapshot_id,
            "actionId": action_id,
            "chainRoot": chain_root,
            "operation": operation,
            "target": {
                "kind": target_kind,
                "number": target_number,
            },
            "idempotencyKey": idempotency_key,
            "bodyDigest": body_digest,
            "expectedActorLogin": expected_actor_login,
        }

    def _reserve_locked(
        self,
        grant: AuthorizationGrant,
        intent: Mapping[str, Any],
    ) -> ActionReservation:
        events = self._load_events()
        action_id = str(intent["actionId"])
        identity = _intent_identity(intent)
        action_events = [
            event for event in events if event.get("actionId") == action_id
        ]
        intent_events = [
            event
            for event in action_events
            if event.get("eventType") == "intent"
        ]
        if intent_events and _intent_identity(intent_events[-1]) != identity:
            raise ExecutionStateError(
                "Persisted intent does not match the authorized action."
            )
        terminal_events = [
            event
            for event in action_events
            if event.get("eventType") == "terminal"
        ]
        if terminal_events:
            prior_terminal = terminal_events[-1]
            if prior_terminal.get("outcome") == "indeterminate":
                return ActionReservation(
                    mode="reconcile",
                    prior_terminal=prior_terminal,
                )
            return ActionReservation(
                mode="terminal",
                prior_terminal=prior_terminal,
            )
        if intent_events:
            return ActionReservation(mode="reconcile")

        grant_intents = [
            event
            for event in events
            if event.get("eventType") == "intent"
            and event.get("grantId") == grant.grant_id
        ]
        if len(grant_intents) >= grant.budget.max_mutation_attempts:
            raise ExecutionBudgetError(
                "Authorization grant mutation-attempt budget is exhausted."
            )
        grant_chain_roots = {
            event.get("chainRoot")
            for event in grant_intents
            if isinstance(event.get("chainRoot"), str)
        }
        chain_root = str(intent["chainRoot"])
        if (
            chain_root not in grant_chain_roots
            and len(grant_chain_roots) >= grant.budget.max_chains
        ):
            raise ExecutionBudgetError(
                "Authorization grant chain budget is exhausted."
            )

        self._append_event(intent)
        return ActionReservation(mode="execute")

    def append_terminal(
        self,
        grant: AuthorizationGrant,
        *,
        result: Mapping[str, Any],
        at: datetime,
    ) -> Mapping[str, Any]:
        action_id = result.get("actionId")
        outcome = result.get("outcome")
        if not isinstance(action_id, str) or not action_id:
            raise ExecutionStateError("Terminal result requires an actionId.")
        if outcome not in _TERMINAL_OUTCOMES:
            raise ExecutionStateError("Terminal result has an unsupported outcome.")

        event = {
            **dict(result),
        }
        with self._locked():
            return self._append_terminal_locked(grant, result=event, at=at)

    def _append_terminal_locked(
        self,
        grant: AuthorizationGrant,
        *,
        result: Mapping[str, Any],
        at: datetime,
    ) -> Mapping[str, Any]:
        action_id = result.get("actionId")
        outcome = result.get("outcome")
        if not isinstance(action_id, str) or not action_id:
            raise ExecutionStateError("Terminal result requires an actionId.")
        if action_id not in grant.allowed_action_ids:
            raise ExecutionStateError("Action is not enumerated by the grant.")
        if outcome not in _TERMINAL_OUTCOMES:
            raise ExecutionStateError("Terminal result has an unsupported outcome.")

        events = self._load_events()
        intents = [
            item
            for item in events
            if item.get("eventType") == "intent"
            and item.get("actionId") == action_id
        ]
        if not intents:
            raise ExecutionStateError(
                "Cannot append a terminal event without a persisted intent."
            )
        intent = intents[-1]
        event = {
            "schemaVersion": 1,
            "eventType": "terminal",
            "recordedAt": _timestamp(at),
            "grantId": intent["grantId"],
            "repository": intent["repository"],
            "snapshotId": intent["snapshotId"],
            "chainRoot": intent["chainRoot"],
            "operation": intent["operation"],
            "target": intent["target"],
            "idempotencyKey": intent["idempotencyKey"],
            "bodyDigest": intent["bodyDigest"],
            "expectedActorLogin": intent["expectedActorLogin"],
            **dict(result),
        }
        terminals = [
            item
            for item in events
            if item.get("eventType") == "terminal"
            and item.get("actionId") == action_id
        ]
        if terminals:
            previous = terminals[-1]
            if _terminal_identity(previous) == _terminal_identity(event):
                return previous
            if previous.get("outcome") != "indeterminate":
                raise ExecutionStateError(
                    "A different terminal event already exists for this action."
                )
        self._append_event(event)
        return event

    def prior_results(self, *, repository: str) -> dict[str, object]:
        with self._locked():
            return self._prior_results_locked(repository=repository)

    def _prior_results_locked(self, *, repository: str) -> dict[str, object]:
        events = [
            event
            for event in self._load_events()
            if event.get("repository") == repository
        ]
        latest_terminals: dict[str, dict[str, Any]] = {}
        for event in events:
            action_id = event.get("actionId")
            if event.get("eventType") == "terminal" and isinstance(action_id, str):
                latest_terminals[action_id] = event
        return {
            "schemaVersion": 1,
            "repository": repository,
            "results": [
                _terminal_result(event)
                for event in latest_terminals.values()
            ],
        }

    def events(self, *, repository: str) -> list[dict[str, Any]]:
        with self._locked():
            events = self._load_events()
        return [
            event
            for event in events
            if event.get("repository") == repository
        ]

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._ensure_state_directory()
        descriptor = os.open(
            self._lock_path,
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        deadline = time.monotonic() + self._lock_timeout_seconds
        acquired = False
        try:
            while not acquired:
                try:
                    _acquire_nonblocking_lock(descriptor)
                    acquired = True
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ExecutionStateError(
                            "Timed out acquiring the action-event lock."
                        )
                    time.sleep(0.05)
            yield
        finally:
            if acquired:
                _release_lock(descriptor)
            os.close(descriptor)

    def _ensure_state_directory(self) -> None:
        self._state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._state_dir.is_symlink():
            raise ExecutionStateError("State directory cannot be a symlink.")
        self._state_dir.chmod(0o700)

    def _load_events(self) -> list[dict[str, Any]]:
        if not self._events_path.exists():
            return []
        if self._events_path.is_symlink():
            raise ExecutionStateError("Action-event file cannot be a symlink.")
        events: list[dict[str, Any]] = []
        try:
            with self._events_path.open(encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, start=1):
                    if not line.strip():
                        raise ExecutionStateError(
                            f"Empty action-event record at line {line_number}."
                        )
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ExecutionStateError(
                            f"Invalid action-event record at line {line_number}."
                        )
                    events.append(event)
        except (OSError, json.JSONDecodeError) as exc:
            raise ExecutionStateError("Unable to read action-event history.") from exc
        return events

    def _append_event(self, event: Mapping[str, Any]) -> None:
        payload = (
            json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode("utf-8")
        created = not self._events_path.exists()
        descriptor = os.open(
            self._events_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            written = 0
            while written < len(payload):
                written += os.write(descriptor, payload[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if created:
            _fsync_directory(self._state_dir)

    def _write_migration_marker(self, *, imported_count: int) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._migration_marker_path.name}.",
            suffix=".tmp",
            dir=self._state_dir,
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(
                    {
                        "schemaVersion": 1,
                        "importedCount": imported_count,
                    },
                    stream,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self._migration_marker_path)
            self._migration_marker_path.chmod(0o600)
            _fsync_directory(self._state_dir)
        finally:
            temporary_path.unlink(missing_ok=True)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ExecutionStateError("Action-event timestamps must be timezone-aware.")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _intent_identity(event: Mapping[str, Any]) -> tuple[Any, ...]:
    target = event.get("target")
    return (
        event.get("repository"),
        event.get("snapshotId"),
        event.get("actionId"),
        event.get("chainRoot"),
        event.get("operation"),
        target.get("kind") if isinstance(target, dict) else None,
        target.get("number") if isinstance(target, dict) else None,
        event.get("idempotencyKey"),
        event.get("bodyDigest"),
        event.get("expectedActorLogin"),
    )


def _terminal_identity(event: Mapping[str, Any]) -> str:
    payload = {
        key: value
        for key, value in event.items()
        if key not in {"recordedAt"}
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _terminal_result(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in event.items()
        if key
        not in {
            "schemaVersion",
            "eventType",
            "recordedAt",
            "grantId",
            "repository",
            "snapshotId",
        }
    }


def _read_json_object(path: Path, description: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionStateError(f"Unable to read {description}.") from exc
    if not isinstance(payload, dict):
        raise ExecutionStateError(f"{description.capitalize()} must be an object.")
    return payload


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


if os.name == "nt":
    import msvcrt

    def _acquire_nonblocking_lock(descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError from exc

    def _release_lock(descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _acquire_nonblocking_lock(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _release_lock(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
