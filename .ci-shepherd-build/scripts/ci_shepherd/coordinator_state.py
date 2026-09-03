"""Append-only, compare-and-swap ledger for CI Shepherd policy revisions and exact decisions.

This module persists two kinds of authorization-relevant events into a single,
append-only, owner-only, multi-repository ledger (one ledger file shared by
every repository, not one ledger per repository):

- ``policy`` events: successive :mod:`ci_shepherd.operation_policy` revisions
  for a repository (including pauses and revocations, which are simply new
  revisions with ``status`` set accordingly).
- ``decision`` events: exact, one-shot overrides (``approve-once``,
  ``reject-once``) or a ``clear`` of a prior override, bound to a single
  proposed action by its raw-byte proposal digest.

Every mutation is a compare-and-swap against a single, ledger-wide monotonic
``stateRevision`` counter (global across all repositories, not per
repository): callers must supply the ``stateRevision`` they last observed as
``expected_revision``, and a concurrent writer that appended first wins. A
loser's call raises ``CoordinatorStateError`` containing ``stale-view`` so
callers can re-read the projection and retry. Because the counter is global,
two writers touching *different* repositories still contend with each other
for the same compare-and-swap slot; this is intentional (it keeps the ledger
a single total order that is trivial to replay) but means callers should
expect retries under concurrent load even across repositories.

IMPORTANT: an exact decision recorded here is NOT itself an authorization.
``projection()`` returns the raw ledger-derived facts (effective policy
revision, effective exact decisions) for a repository; it is the
responsibility of the caller composing this projection with the effective
policy's own capability limits and deny lists (and, eventually, budget/
reservation state from Task 5) to decide whether a specific action is
actually authorized. This module only guarantees that what it persisted was
recorded exactly, atomically, and without silent loss.

Locking, symlink rejection, and atomic append+fsync are inspired by
``ci_shepherd.execution_state.ActionEventStore`` (duplicated here rather than
extracted, since extracting would require also refactoring that module,
which is out of scope) but harden the read/open paths further: every raw
byte read (ledger, proposals document) and every lock/ledger file open goes
through an ``os.open`` call guarded with ``O_NOFOLLOW`` (POSIX) and
``O_BINARY`` (Windows) where the platform supports them, closing the
time-of-check-to-time-of-use window between an initial ``Path.is_symlink()``
check and the actual open. The two ledgers are independent files with
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
append-only action-event tail). See ``CoordinatorStateStore.__init__`` and
``make_lock_free_durable_intent_reader`` for the full contract and a
conforming implementation.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
import errno
import hashlib
import json
import os
import re
import time
from typing import Any, Callable, Iterator, Mapping

from .operation_policy import OperationPolicyError, load_operation_policy_document
from .timeutils import format_utc_z, parse_aware_iso8601


__all__ = [
    "CoordinatorStateError",
    "CoordinatorStateStore",
    "make_lock_free_durable_intent_reader",
]


class CoordinatorStateError(RuntimeError):
    """Raised when persisted coordinator state is invalid, stale, or unsafe."""


_SCHEMA_VERSION = 1
_DECISION_VALUES = frozenset({"approve-once", "reject-once", "clear"})
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# Duplicated from ci_shepherd.operation_policy (private there; these are the
# same shapes used to validate a policy document's own repository/actor
# fields). Duplicating a handful of regex literals is lower-risk than either
# importing private names or broadening that module's public surface for
# this task.
_OWNER_PATTERN = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_REPOSITORY_NAME_PATTERN = r"[A-Za-z0-9._-]+"
_REPOSITORY_RE = re.compile(rf"^{_OWNER_PATTERN}/{_REPOSITORY_NAME_PATTERN}$")
_ACTOR_RE = re.compile(rf"^github:{_OWNER_PATTERN}$")

_POLICY_EVENTS_FILENAME = "policy-events.jsonl"
_POLICY_LOCK_FILENAME = "policy-events.lock"

# The action-event shapes this module's lock-free reader understands, taken
# from ci_shepherd.execution_state.ActionEventStore. Any new eventType a
# future task adds to action-events.jsonl must also be taught to
# make_lock_free_durable_intent_reader below, or that reader will (correctly,
# per its fail-closed contract) treat every action touching this repository's
# ledger as having a durable intent and reject every clear.
_KNOWN_ACTION_EVENT_TYPES = frozenset(
    {"intent", "terminal", "delegation-baseline", "delegation-retired"}
)


class CoordinatorStateStore:
    """Persists a global, multi-repository policy/decision ledger under ``state_dir``.

    A single ledger file backs every repository; ``stateRevision`` is a
    ledger-wide monotonic counter used for compare-and-swap across all
    repositories, not a per-repository counter. See the module docstring for
    the full contract, including the explicit reminder that an exact decision
    recorded by this store is not itself an authorization decision.
    """

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
        store and rejects the clear. ``make_lock_free_durable_intent_reader``
        in this module builds a conforming callback for a real
        ``action-events.jsonl`` path.
        """
        expanded = state_dir.expanduser()
        # Leaf-only symlink check (not an ancestor walk): a legitimate
        # ancestor symlink -- for example macOS resolving /var to
        # /private/var -- must not be rejected, only the exact directory
        # entry the caller handed us. ``resolve(strict=False)`` below
        # canonicalizes the ancestor chain; the O_NOFOLLOW opens on every
        # subsequent file access close the remaining time-of-check-to-
        # time-of-use window against the leaf itself being swapped for a
        # symlink between this check and use.
        if expanded.is_symlink():
            raise CoordinatorStateError("Coordinator state directory cannot be a symlink.")
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

        # Held under the policy lock so a reader can never observe a
        # partially-applied append (the lock is exclusive for the duration of
        # the read + compute, matching the writers' current-revision-read-
        # through-fsync critical section).
        with self._locked():
            events = self._load_events()
            return self._project_from_events(events, repository, now)

    def _project_from_events(
        self, events: list[dict[str, Any]], repository: str, now: datetime
    ) -> dict[str, object]:
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
        actor = _require_actor(actor)
        if not isinstance(action_id, str) or not action_id:
            raise CoordinatorStateError("action_id must be a nonempty string.")
        # `now` is used only for proposal activity/expiry checks and (for a
        # clear) the effective-decision-expiry check below. It is never
        # persisted as recordedAtUtc: that is always freshly-observed wall
        # clock time, captured under the lock immediately before appending.
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
                self._validate_clear_target_locked(events, repository, digest, action_id, now)

            new_state_revision = len(events) + 1
            event: dict[str, object] = {
                "schemaVersion": _SCHEMA_VERSION,
                "stateRevision": new_state_revision,
                "eventType": "decision",
                "repository": repository,
                # Audit provenance: always the real wall clock, never the
                # caller-supplied `now` (which only governs proposal
                # activity/expiry semantics above).
                "recordedAtUtc": format_utc_z(datetime.now(UTC)),
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

    def _validate_clear_target_locked(
        self,
        events: list[dict[str, Any]],
        repository: str,
        digest: str,
        action_id: str,
        now: datetime,
    ) -> None:
        # C1: a clear must prove an effective decision exists for this exact
        # (raw proposal digest, actionId) key before doing anything else.
        # Without this, a clear against a nonexistent, already-expired,
        # already-cleared, or byte-different-but-otherwise-identical
        # (regenerated) proposals document would silently succeed while
        # leaving the real decision live -- a fail-open bug.
        effective = _latest_decision_payloads(events, repository).get((digest, action_id))
        if effective is None:
            raise CoordinatorStateError(
                "No effective decision exists for this exact action digest "
                "to clear."
            )
        try:
            effective_expires_at = parse_aware_iso8601(
                effective["expiresAtUtc"], "expiresAtUtc"
            )
        except ValueError as exc:
            raise CoordinatorStateError(str(exc)) from exc
        if now >= effective_expires_at:
            raise CoordinatorStateError(
                "Cannot clear an already-expired decision."
            )
        if effective["decision"] == "clear":
            raise CoordinatorStateError("Decision has already been cleared.")

        # Only after proving an effective decision exists do we consult the
        # durable-intent reader; this ordering matches the review's explicit
        # requirement and avoids an unnecessary/misleading intent check for a
        # clear that was going to be rejected anyway.
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

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._ensure_directories()
        if self._lock_path.is_symlink():
            raise CoordinatorStateError("Policy-event lock file cannot be a symlink.")
        descriptor = _open_guarded(
            self._lock_path,
            os.O_RDWR | os.O_CREAT,
            0o600,
            "Policy-event lock file",
        )
        _fchmod_if_supported(descriptor, 0o600)
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

        coordinator_dir_created = not self._coordinator_dir.exists()
        self._coordinator_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._coordinator_dir.is_symlink():
            raise CoordinatorStateError("Coordinator directory cannot be a symlink.")
        self._coordinator_dir.chmod(0o700)

        # Persist the new "coordinator" directory entry itself, not just its
        # contents: fsyncing the containing directory (state_dir) is what
        # makes a newly-created entry within it durable across a crash.
        if coordinator_dir_created:
            _fsync_directory(self._state_dir)

    def _load_events(self) -> list[dict[str, Any]]:
        if not self._events_path.exists():
            return []
        if self._events_path.is_symlink():
            raise CoordinatorStateError("Policy-event ledger cannot be a symlink.")
        descriptor = _open_guarded(
            self._events_path, os.O_RDONLY, 0o600, "Policy-event ledger"
        )
        try:
            payload = _read_all(descriptor)
        finally:
            os.close(descriptor)

        if not payload:
            return []
        if not payload.endswith(b"\n"):
            # A crash mid-write leaves a partial trailing record; treating it
            # as data loss rather than attempting to salvage a partial JSON
            # fragment keeps failure modes fail-closed and unambiguous.
            raise CoordinatorStateError(
                "Policy-event ledger has a truncated trailing record."
            )
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CoordinatorStateError("Policy-event ledger is not valid UTF-8.") from exc

        events: list[dict[str, Any]] = []
        lines = text.split("\n")[:-1]  # Drop the trailing '' after the final \n.
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                raise CoordinatorStateError(
                    f"Empty policy-event record at line {line_number}."
                )
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CoordinatorStateError(
                    f"Malformed policy-event record at line {line_number}."
                ) from exc
            if not isinstance(event, dict):
                raise CoordinatorStateError(
                    f"Invalid policy-event record at line {line_number}."
                )
            _validate_event_shape(event, line_number)
            events.append(event)
        return events

    def _append_event_locked(self, event: Mapping[str, Any]) -> None:
        if self._events_path.is_symlink():
            raise CoordinatorStateError("Policy-event ledger cannot be a symlink.")
        payload = (
            json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode("utf-8")
        created = not self._events_path.exists()
        descriptor = _open_guarded(
            self._events_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
            "Policy-event ledger",
        )
        try:
            _fchmod_if_supported(descriptor, 0o600)
            written = 0
            while written < len(payload):
                written += os.write(descriptor, payload[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if created:
            _fsync_directory(self._coordinator_dir)


def make_lock_free_durable_intent_reader(
    action_events_path: Path,
) -> Callable[[str], bool]:
    """Build a conforming ``durable_intent_reader`` callback for ``CoordinatorStateStore``.

    The returned callable reads ``action_events_path`` (normally
    ``<state>/action-events.jsonl``, written by
    ``ci_shepherd.execution_state.ActionEventStore``) directly and answers
    whether the given ``actionId`` currently has a durable, unresolved
    ``intent`` event (one with no subsequent ``terminal`` event for the same
    ``actionId``).

    Lock-freedom contract: this function and the callable it returns NEVER
    open or acquire ``action-events.lock``. They perform a single, direct,
    non-locking read of the append-only ledger file. This is required
    because ``CoordinatorStateStore`` invokes this callback while it already
    holds ``coordinator/policy-events.lock``; the documented global lock
    order for any code that must hold both locks is
    ``action-events.lock -> coordinator/policy-events.lock``, so a
    lock-holding read here (in the opposite order, from inside the policy
    lock) would risk deadlock against a future Task 5 reservation/budget
    validator that acquires the locks in the prescribed order.

    Fail-closed contract: a missing file reports no durable intent (nothing
    has ever run for this repository). Any other unsafe or unparseable
    state -- a symlinked ledger path, malformed JSON, a non-object record, or
    a truncated/incomplete trailing record -- reports a durable intent
    (``True``) for malformed/incomplete *tails*, or is surfaced as
    ``CoordinatorStateError`` for unsafe filesystem state (symlink), so that
    the caller's clear is rejected rather than proceeding against
    unverifiable action-event history.
    """

    def _read_durable_intent(action_id: str) -> bool:
        expanded = action_events_path.expanduser()
        # Leaf-only check (see CoordinatorStateStore.__init__ for rationale),
        # consistent with the rest of this module's symlink handling.
        if expanded.is_symlink():
            raise CoordinatorStateError("Action-event ledger cannot be a symlink.")
        if not expanded.exists():
            return False

        try:
            descriptor = _open_guarded(
                expanded, os.O_RDONLY, 0o600, "Action-event ledger"
            )
        except CoordinatorStateError:
            raise
        try:
            payload = _read_all(descriptor)
        finally:
            os.close(descriptor)

        if not payload:
            return False
        if not payload.endswith(b"\n"):
            return True  # Truncated trailing record: fail closed.
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            return True

        last_event_type_for_action: str | None = None
        for line in text.split("\n")[:-1]:
            if not line.strip():
                return True  # Empty record: fail closed.
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                return True  # Malformed record: fail closed.
            if not isinstance(event, dict):
                return True
            event_type = event.get("eventType")
            if event_type not in _KNOWN_ACTION_EVENT_TYPES:
                return True  # Unrecognized shape: fail closed.
            if event.get("actionId") != action_id:
                continue
            if event_type in ("intent", "terminal"):
                last_event_type_for_action = event_type

        return last_event_type_for_action == "intent"

    return _read_durable_intent


def _require_repository(repository: object) -> str:
    if not isinstance(repository, str) or _REPOSITORY_RE.fullmatch(repository) is None:
        raise CoordinatorStateError(
            "repository must be a nonempty 'owner/name' identity."
        )
    return repository


def _require_actor(actor: object) -> str:
    if not isinstance(actor, str) or _ACTOR_RE.fullmatch(actor) is None:
        raise CoordinatorStateError("actor must be a GitHub actor identity.")
    return actor


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
    if not isinstance(repository, str) or _REPOSITORY_RE.fullmatch(repository) is None:
        fail("repository must be a valid 'owner/name' identity")
    recorded_at_utc = event.get("recordedAtUtc")
    if not isinstance(recorded_at_utc, str) or not recorded_at_utc:
        fail("recordedAtUtc must be a nonempty string")
    try:
        parse_aware_iso8601(recorded_at_utc, "recordedAtUtc")
    except ValueError:
        fail("recordedAtUtc must be a timezone-aware ISO-8601 timestamp")

    if event_type == "policy":
        policy = event.get("policy")
        if not isinstance(policy, dict):
            fail("policy payload must be an object")
        revision_id = policy.get("revisionId")
        if not isinstance(revision_id, str) or not revision_id:
            fail("policy payload's revisionId must be a nonempty string")
        status = policy.get("status")
        if not isinstance(status, str) or not status:
            fail("policy payload's status must be a nonempty string")
        replaces_revision_id = policy.get("replacesRevisionId")
        if "replacesRevisionId" not in policy or (
            replaces_revision_id is not None
            and (not isinstance(replaces_revision_id, str) or not replaces_revision_id)
        ):
            fail(
                "policy payload's replacesRevisionId must be null or a "
                "nonempty string"
            )
        digest = event.get("policyDigest")
        if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
            fail("policyDigest must be a sha256 digest")
    else:
        decision = event.get("decision")
        if not isinstance(decision, dict):
            fail("decision payload must be an object")
        action_id = decision.get("actionId")
        if not isinstance(action_id, str) or not action_id:
            fail("decision payload's actionId must be a nonempty string")
        actor = decision.get("actor")
        if not isinstance(actor, str) or _ACTOR_RE.fullmatch(actor) is None:
            fail("decision payload's actor must be a valid GitHub actor identity")
        decision_value = decision.get("decision")
        if decision_value not in _DECISION_VALUES:
            fail("decision payload's decision must be approve-once, reject-once, or clear")
        digest = decision.get("proposalDigest")
        if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
            fail("proposalDigest must be a sha256 digest")
        expires_at_utc = decision.get("expiresAtUtc")
        if not isinstance(expires_at_utc, str) or not expires_at_utc:
            fail("decision payload's expiresAtUtc must be a nonempty string")
        try:
            parse_aware_iso8601(expires_at_utc, "expiresAtUtc")
        except ValueError:
            fail(
                "decision payload's expiresAtUtc must be a timezone-aware "
                "ISO-8601 timestamp"
            )


def _read_proposal_bytes(path: Path) -> bytes:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise CoordinatorStateError("Proposals path cannot be a symlink.")
    descriptor = _open_guarded(expanded, os.O_RDONLY, 0o600, "Proposals document")
    try:
        return _read_all(descriptor)
    finally:
        os.close(descriptor)


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


def _guarded_open_flags(base_flags: int) -> int:
    """OR in the strongest available symlink/text-mode protections for ``os.open``.

    ``O_NOFOLLOW`` (POSIX-only) makes the kernel refuse to open a path whose
    final component is a symlink, closing the time-of-check-to-time-of-use
    window between an earlier ``Path.is_symlink()`` check and this open.
    ``O_BINARY`` (Windows-only) disables the C runtime's default text-mode
    ``\\r\\n``/``\\n`` translation, which would otherwise silently corrupt
    raw byte reads/writes (ledger records, proposal document bytes) on that
    platform. Neither flag exists on every platform, so both are added only
    when present.
    """

    flags = base_flags
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    return flags


def _open_guarded(path: Path, base_flags: int, mode: int, description: str) -> int:
    try:
        return os.open(path, _guarded_open_flags(base_flags), mode)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise CoordinatorStateError(f"{description} cannot be a symlink.") from exc
        raise CoordinatorStateError(f"Unable to open {description.lower()}: {path}") from exc


def _read_all(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _fchmod_if_supported(descriptor: int, mode: int) -> None:
    # os.fchmod is POSIX-only (absent on Windows); the initial os.open(...,
    # mode) already applies the requested mode bits on creation there, so
    # this is purely a POSIX belt-and-suspenders re-assertion against an
    # already-existing file with looser permissions.
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, mode)


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
