"""Append-only, compare-and-swap ledger for CI Shepherd policy revisions and exact decisions.

This module persists two kinds of authorization-relevant events into a single,
append-only, owner-only ledger:

- ``policy`` events: successive :mod:`ci_shepherd.operation_policy` revisions
  for a repository (including pauses and revocations, which are simply new
  revisions with ``status`` set accordingly).
- ``decision`` events: exact, one-shot overrides (``approve-once``,
  ``reject-once``) or a ``clear`` of a prior override, bound to a single
  proposed action by its raw-byte proposal digest.

Every mutation is a compare-and-swap against a single, ledger-wide monotonic
``stateRevision`` counter (not per repository): callers must supply the
``stateRevision`` they last observed as ``expected_revision``, and a
concurrent writer that appended first wins. A loser's call raises
``CoordinatorStateError`` containing ``stale-view`` so callers can re-read the
projection and retry.

Locking, symlink rejection, and atomic append+fsync mirror
``ci_shepherd.execution_state.ActionEventStore`` exactly (duplicated here
rather than extracted, since extracting would require also refactoring that
module, which is out of scope). The two ledgers are independent files with
independent locks:

    <state>/action-events.jsonl / action-events.lock       (execution_state.py)
    <state>/coordinator/policy-events.jsonl / .lock        (this module)

Lock order for future work: any code that must hold both locks (for example a
Task 5 reservation/budget validator) MUST acquire ``action-events.lock``
before ``coordinator/policy-events.lock``. To keep this module safe against
that future order without deadlocking, the ``durable_intent_reader`` callback
injected into ``CoordinatorStateStore`` is invoked *while*
``policy-events.lock`` is held, and MUST be implemented lock-free with
respect to ``action-events.lock`` (a direct, non-locking read of the
append-only action-event tail). See ``CoordinatorStateStore.__init__`` for
the full contract.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
import hashlib
import json
import os
import re
import time
from typing import Any, Callable, Iterator, Mapping

from .operation_policy import OperationPolicyError, load_operation_policy_document
from .timeutils import format_utc_z, parse_aware_iso8601


__all__ = ["CoordinatorStateError", "CoordinatorStateStore"]


class CoordinatorStateError(RuntimeError):
    """Raised when persisted coordinator state is invalid, stale, or unsafe."""


_SCHEMA_VERSION = 1
_DECISION_VALUES = frozenset({"approve-once", "reject-once", "clear"})
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_POLICY_EVENTS_FILENAME = "policy-events.jsonl"
_POLICY_LOCK_FILENAME = "policy-events.lock"


class CoordinatorStateStore:
    """Persists a single repository-scoped policy/decision ledger under ``state_dir``."""

    def __init__(
        self,
        state_dir: Path,
        *,
        durable_intent_reader: Callable[[str], bool],
        lock_timeout_seconds: float = 5.0,
    ) -> None:
        """Create a store rooted at ``state_dir``.

        ``durable_intent_reader`` is invoked only when clearing a decision
        (``decision="clear"``), while ``coordinator/policy-events.lock`` is
        held, and must answer whether a durable action intent already exists
        for the given ``actionId``. It MUST NOT acquire
        ``action-events.lock`` (or block on anything that could be waiting
        on ``coordinator/policy-events.lock``) -- it is required to be
        lock-free relative to the action-event ledger, reading its
        append-only tail directly. It MUST fail closed: on a malformed or
        incomplete action-event tail it should behave as though a durable
        intent exists (return ``True``) rather than permit an unsafe clear.
        Any exception it raises is also treated as fail-closed by this
        store and rejects the clear.
        """
        expanded = state_dir.expanduser()
        if expanded.is_symlink() or any(
            parent.is_symlink() for parent in expanded.parents
        ):
            raise CoordinatorStateError(
                "Coordinator state directory cannot traverse a symlink."
            )
        self._state_dir = expanded.resolve(strict=False)
        self._coordinator_dir = self._state_dir / "coordinator"
        self._events_path = self._coordinator_dir / _POLICY_EVENTS_FILENAME
        self._lock_path = self._coordinator_dir / _POLICY_LOCK_FILENAME
        self._durable_intent_reader = durable_intent_reader
        self._lock_timeout_seconds = lock_timeout_seconds

    def projection(
        self, repository: str, *, now: datetime | None = None
    ) -> dict[str, object]:
        repository = _require_repository(repository)
        now = _require_optional_now(now)

        events = self._load_events()
        latest_policy_event = _latest_policy_event(events, repository)
        effective_policy: dict[str, object] | None = None
        if latest_policy_event is not None:
            effective_policy = {
                **latest_policy_event["policy"],
                "policyDigest": latest_policy_event["policyDigest"],
            }

        exact_decisions: list[dict[str, object]] = []
        for payload in _latest_decision_payloads(events, repository).values():
            try:
                expires_at = parse_aware_iso8601(
                    payload["expiresAtUtc"], "expiresAtUtc"
                )
            except ValueError as exc:
                raise CoordinatorStateError(str(exc)) from exc
            if now >= expires_at:
                continue  # Expired: remains in ledger history, not projection.
            if payload["decision"] == "clear":
                continue  # Cleared: no effective override remains.
            exact_decisions.append(dict(payload))
        exact_decisions.sort(key=lambda item: (item["actionId"], item["proposalDigest"]))

        return {
            "stateRevision": len(events),
            "effectivePolicy": effective_policy,
            "exactDecisions": exact_decisions,
        }

    def append_policy_revision(
        self,
        *,
        repository: str,
        expected_revision: int,
        document: Mapping[str, object],
    ) -> dict[str, object]:
        repository = _require_repository(repository)
        expected_revision = _require_expected_revision(expected_revision)
        try:
            parsed = load_operation_policy_document(document)
        except OperationPolicyError as exc:
            raise CoordinatorStateError(
                f"Operation policy document is invalid: {exc}"
            ) from exc
        if parsed.repository != repository:
            raise CoordinatorStateError(
                "Operation policy document repository does not match the "
                "requested repository."
            )

        with self._locked():
            events = self._load_events()
            _check_expected_revision(events, expected_revision)

            # Task 1 validates internal consistency of a single document (for
            # example, that revision 3 may declare it replaces any earlier
            # revision), but not that it replaces the revision that is
            # *currently* persisted as effective for this repository. That
            # stateful predecessor-exactness check was intentionally
            # deferred to this task, since it requires reading prior ledger
            # state under the lock.
            latest_policy_event = _latest_policy_event(events, repository)
            currently_effective_id = (
                latest_policy_event["policy"]["revisionId"]
                if latest_policy_event is not None
                else None
            )
            if parsed.replaces_revision_id != currently_effective_id:
                raise CoordinatorStateError(
                    "replacesRevisionId must reference the currently effective "
                    f"policy revision ({currently_effective_id!r}); got "
                    f"{parsed.replaces_revision_id!r}."
                )

            new_state_revision = len(events) + 1
            event: dict[str, object] = {
                "schemaVersion": _SCHEMA_VERSION,
                "stateRevision": new_state_revision,
                "eventType": "policy",
                "repository": repository,
                "recordedAtUtc": format_utc_z(datetime.now(UTC)),
                "policy": parsed.as_public_dict(),
                "policyDigest": parsed.digest,
            }
            self._append_event_locked(event)
            return {"stateRevision": new_state_revision}

    def append_exact_decision(
        self,
        *,
        repository: str,
        expected_revision: int,
        proposals_path: Path,
        action_id: str,
        decision: str,
        actor: str,
        now: datetime,
    ) -> dict[str, object]:
        repository = _require_repository(repository)
        expected_revision = _require_expected_revision(expected_revision)
        if decision not in _DECISION_VALUES:
            raise CoordinatorStateError(
                "decision must be 'approve-once', 'reject-once', or 'clear'."
            )
        if not isinstance(actor, str) or not actor:
            raise CoordinatorStateError("actor must be a nonempty string.")
        if not isinstance(action_id, str) or not action_id:
            raise CoordinatorStateError("action_id must be a nonempty string.")
        now = _require_now(now)

        # Everything below is derived from the proposal bytes and document
        # ourselves; digest and expiry are never accepted from the caller.
        proposal_bytes = _read_proposal_bytes(proposals_path)
        proposal_document = _load_proposal_json(proposal_bytes)
        if proposal_document.get("repository") != repository:
            raise CoordinatorStateError(
                "Proposals document repository does not match the requested "
                "repository."
            )
        try:
            generated_at = parse_aware_iso8601(
                proposal_document.get("generatedAtUtc"), "generatedAtUtc"
            )
        except ValueError as exc:
            raise CoordinatorStateError(str(exc)) from exc
        ttl_hours = proposal_document.get("proposalTtlHours")
        if not isinstance(ttl_hours, int) or isinstance(ttl_hours, bool) or ttl_hours <= 0:
            raise CoordinatorStateError(
                "Proposals document proposalTtlHours must be a positive integer."
            )
        expires_at = generated_at + timedelta(hours=ttl_hours)
        if now < generated_at:
            raise CoordinatorStateError("Proposals document is not active yet.")
        if now >= expires_at:
            raise CoordinatorStateError("Proposals document has expired.")

        proposals = proposal_document.get("proposals")
        if not isinstance(proposals, list):
            raise CoordinatorStateError("Proposals document proposals must be a list.")
        matches = [
            proposal
            for proposal in proposals
            if isinstance(proposal, dict) and proposal.get("actionId") == action_id
        ]
        if len(matches) != 1:
            raise CoordinatorStateError(
                f"Unable to resolve exactly one proposal for actionId "
                f"{action_id!r}; found {len(matches)}."
            )

        digest = f"sha256:{hashlib.sha256(proposal_bytes).hexdigest()}"

        with self._locked():
            events = self._load_events()
            _check_expected_revision(events, expected_revision)

            if decision == "clear":
                try:
                    intent_exists = self._durable_intent_reader(action_id)
                except Exception as exc:
                    raise CoordinatorStateError(
                        "durable action intent reader failed; failing closed "
                        "and rejecting the clear."
                    ) from exc
                if intent_exists:
                    raise CoordinatorStateError(
                        "Cannot clear this decision while a durable action "
                        "intent exists for this action."
                    )

            new_state_revision = len(events) + 1
            event: dict[str, object] = {
                "schemaVersion": _SCHEMA_VERSION,
                "stateRevision": new_state_revision,
                "eventType": "decision",
                "repository": repository,
                "recordedAtUtc": format_utc_z(now),
                "decision": {
                    "actionId": action_id,
                    "proposalDigest": digest,
                    "decision": decision,
                    "actor": actor,
                    "expiresAtUtc": format_utc_z(expires_at),
                },
            }
            self._append_event_locked(event)
            return {"stateRevision": new_state_revision}

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._ensure_directories()
        if self._lock_path.is_symlink():
            raise CoordinatorStateError("Policy-event lock file cannot be a symlink.")
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
                        raise CoordinatorStateError(
                            "Timed out acquiring the policy-event lock."
                        )
                    time.sleep(0.05)
            yield
        finally:
            if acquired:
                _release_lock(descriptor)
            os.close(descriptor)

    def _ensure_directories(self) -> None:
        self._state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._state_dir.is_symlink():
            raise CoordinatorStateError("Coordinator state directory cannot be a symlink.")
        self._state_dir.chmod(0o700)
        self._coordinator_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._coordinator_dir.is_symlink():
            raise CoordinatorStateError("Coordinator directory cannot be a symlink.")
        self._coordinator_dir.chmod(0o700)

    def _load_events(self) -> list[dict[str, Any]]:
        if not self._events_path.exists():
            return []
        if self._events_path.is_symlink():
            raise CoordinatorStateError("Policy-event ledger cannot be a symlink.")
        events: list[dict[str, Any]] = []
        try:
            with self._events_path.open(encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, start=1):
                    if not line.strip():
                        raise CoordinatorStateError(
                            f"Empty policy-event record at line {line_number}."
                        )
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise CoordinatorStateError(
                            f"Invalid policy-event record at line {line_number}."
                        )
                    _validate_event_shape(event, line_number)
                    events.append(event)
        except (OSError, json.JSONDecodeError) as exc:
            raise CoordinatorStateError("Unable to read policy-event history.") from exc
        return events

    def _append_event_locked(self, event: Mapping[str, Any]) -> None:
        if self._events_path.is_symlink():
            raise CoordinatorStateError("Policy-event ledger cannot be a symlink.")
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
            _fsync_directory(self._coordinator_dir)


def _require_repository(repository: object) -> str:
    if not isinstance(repository, str) or "/" not in repository:
        raise CoordinatorStateError("repository must be a nonempty 'owner/name' identity.")
    return repository


def _require_expected_revision(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CoordinatorStateError("expected_revision must be a non-negative integer.")
    return value


def _require_now(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CoordinatorStateError("now must be a timezone-aware datetime.")
    return value.astimezone(UTC)


def _require_optional_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    return _require_now(value)


def _check_expected_revision(events: list[dict[str, Any]], expected_revision: int) -> None:
    current = len(events)
    if expected_revision != current:
        raise CoordinatorStateError(
            f"stale-view: expected_revision {expected_revision} does not match "
            f"the current stateRevision {current}."
        )


def _latest_policy_event(
    events: list[dict[str, Any]], repository: str
) -> dict[str, Any] | None:
    latest: dict[str, Any] | None = None
    for event in events:
        if event["eventType"] == "policy" and event["repository"] == repository:
            latest = event
    return latest


def _latest_decision_payloads(
    events: list[dict[str, Any]], repository: str
) -> dict[tuple[str, str], dict[str, Any]]:
    # All events sharing a (proposalDigest, actionId) key necessarily share
    # the same derived expiresAtUtc (it is fully determined by the raw bytes
    # hashed into the digest and the proposal document's own TTL fields), so
    # keeping only the ledger-order-last entry per key is unambiguous.
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        if event["eventType"] != "decision" or event["repository"] != repository:
            continue
        payload = event["decision"]
        key = (payload["proposalDigest"], payload["actionId"])
        latest[key] = payload
    return latest


def _validate_event_shape(event: Mapping[str, Any], line_number: int) -> None:
    def fail(message: str) -> None:
        raise CoordinatorStateError(f"{message} (policy-event line {line_number}).")

    if event.get("schemaVersion") != _SCHEMA_VERSION:
        fail("schemaVersion must be 1")
    state_revision = event.get("stateRevision")
    if (
        not isinstance(state_revision, int)
        or isinstance(state_revision, bool)
        or state_revision < 1
    ):
        fail("stateRevision must be a positive integer")
    if state_revision != line_number:
        # A hand-edited or corrupted ledger could otherwise desynchronize
        # the persisted counter from the file's actual append order.
        fail("stateRevision must match its ledger position")
    event_type = event.get("eventType")
    if event_type not in ("policy", "decision"):
        fail("eventType must be 'policy' or 'decision'")
    repository = event.get("repository")
    if not isinstance(repository, str) or not repository:
        fail("repository must be a nonempty string")
    recorded_at_utc = event.get("recordedAtUtc")
    if not isinstance(recorded_at_utc, str) or not recorded_at_utc:
        fail("recordedAtUtc must be a nonempty string")

    if event_type == "policy":
        policy = event.get("policy")
        if not isinstance(policy, dict):
            fail("policy payload must be an object")
        for key in ("revisionId", "status", "replacesRevisionId"):
            if key not in policy:
                fail(f"policy payload is missing {key!r}")
        digest = event.get("policyDigest")
        if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
            fail("policyDigest must be a sha256 digest")
    else:
        decision = event.get("decision")
        if not isinstance(decision, dict):
            fail("decision payload must be an object")
        for key in ("actionId", "proposalDigest", "decision", "actor", "expiresAtUtc"):
            if key not in decision:
                fail(f"decision payload is missing {key!r}")
        if decision.get("decision") not in _DECISION_VALUES:
            fail("decision payload's decision must be approve-once, reject-once, or clear")
        digest = decision.get("proposalDigest")
        if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
            fail("proposalDigest must be a sha256 digest")


def _read_proposal_bytes(path: Path) -> bytes:
    expanded = path.expanduser()
    if expanded.is_symlink() or any(parent.is_symlink() for parent in expanded.parents):
        raise CoordinatorStateError("Proposals path cannot traverse a symlink.")
    try:
        return expanded.read_bytes()
    except OSError as exc:
        raise CoordinatorStateError(f"Unable to read proposals document: {expanded}") from exc


def _load_proposal_json(payload: bytes) -> dict[str, Any]:
    def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise CoordinatorStateError(
                    f"Proposals document contains duplicate key: {key}"
                )
            result[key] = value
        return result

    try:
        document = json.loads(
            payload.decode("utf-8"), object_pairs_hook=reject_duplicate_pairs
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CoordinatorStateError("Proposals document must be valid UTF-8 JSON.") from exc
    if not isinstance(document, dict):
        raise CoordinatorStateError("Proposals document must be an object.")
    return document


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


# Cross-platform, non-blocking advisory locking. Duplicated verbatim from
# ci_shepherd.execution_state (rather than extracted into a shared helper
# module) to avoid touching that module's lock semantics in this task; keep
# both copies in sync if this behavior ever needs to change.
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
