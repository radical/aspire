"""Deterministic local CI Shepherd policy coordinator CLI.

This is a thin adapter, not a new decision-making layer: every policy
validation rule, exact-decision rule, selection rule, and grant rule below
is enforced by the Task 1-5 modules this script calls
(``ci_shepherd.operation_policy``, ``ci_shepherd.coordinator_state``,
``ci_shepherd.policy_selection``, ``ci_shepherd.authorization``). This
module's own job is limited to parsing argv, deriving the handful of
fields an operator should never supply directly (a policy revision's
identity, predecessor, and timestamps; an exact decision's proposal
digest and expiry; a grant's TTL and single-action scope), invoking the
already-reviewed module functions, and formatting their result or error
as stable JSON.

It runs entirely on the local filesystem under ``--state-dir``: there is
no remote-forge client, no network call, no distributed lock, and no
scheduler here. A human (or a script standing in for one) invokes one
subcommand at a time and reads back a typed JSON result or a typed JSON
error.

See docs/superpowers/plans/2026-09-03-ci-shepherd-autonomous-policy.md
(Task 6) for the full command and behavior specification this module
implements.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from ci_shepherd.authorization import (
    AuthorizationError,
    generate_authorization_grant,
    write_authorization_grant,
)
from ci_shepherd.coordinator_state import (
    CoordinatorStateError,
    CoordinatorStateStore,
    make_lock_free_durable_intent_reader,
)
from ci_shepherd.execution_state import ActionEventStore
from ci_shepherd.models import stable_json
from ci_shepherd.operation_policy import (
    OPERATION_CLASSES,
    OperationPolicyError,
    load_operation_policy_document,
)
from ci_shepherd.policy_selection import PolicySelectionError, build_policy_selection
from ci_shepherd.timeutils import format_utc_z, parse_aware_iso8601

__all__ = ["main"]

# Task 1's OperationPolicyRevision schema requires every actor identity to
# match "github:<owner>" (an account-identity naming convention already
# baked into that schema, independent of any live client). policy-preview
# never appends to the ledger and therefore never has a real requesting
# actor, so this sentinel stands in for one; it names no real account.
_PREVIEW_ACTOR = "github:ci-shepherd-preview"


# ---------------------------------------------------------------------------
# Typed CLI errors
# ---------------------------------------------------------------------------


class _CoordinatorCliError(Exception):
    """A CLI-originated, typed, non-stale-view failure.

    Covers argument parsing/shape problems and any other CLI-level
    validation that is not itself a rule owned by one of the Task 1-5
    modules (for example: an unrecognized flag, a caps document with
    unexpected fields, or a selection artifact naming the wrong
    repository).
    """

    def __init__(self, code: str, message: str, **extra: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


class _StaleViewError(Exception):
    """Raised when a caller's ``--expected-revision`` no longer matches.

    Carries the complete, freshly recomputed projection so the caller
    can retry against current state without a second round trip.
    """

    def __init__(self, projection: dict[str, object]) -> None:
        super().__init__("stale-view")
        self.projection = projection


class _CoordinatorArgumentParser(argparse.ArgumentParser):
    """Routes every argparse-level failure through the typed-error contract.

    Only ``error()`` is overridden. ``-h``/``--help`` still exits the
    process directly via argparse's own ``exit()``, matching ordinary CLI
    behavior for every other script in this repository.
    """

    def error(self, message: str) -> None:  # pragma: no cover - trivial
        raise _CoordinatorCliError("invalid-argument", message)


# ---------------------------------------------------------------------------
# Shared derivation and read helpers
# ---------------------------------------------------------------------------


def _now_type(value: str) -> datetime:
    return parse_aware_iso8601(value, "--now")


def _read_json_file(path: Path, description: str) -> dict[str, Any]:
    """Read and parse a JSON object file, rejecting symlinks and duplicate keys.

    A local equivalent of the shared reader other scripts in this package
    keep private to their own module; duplicated deliberately rather than
    importing another module's private helper.
    """

    expanded = Path(path).expanduser()
    if expanded.is_symlink() or any(parent.is_symlink() for parent in expanded.parents):
        raise _CoordinatorCliError("invalid-argument", f"{description} cannot traverse a symlink.")
    try:
        raw = expanded.read_bytes()
    except OSError as exc:
        raise _CoordinatorCliError(
            "invalid-argument", f"Unable to read {description}: {expanded}"
        ) from exc

    def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _CoordinatorCliError(
                    "invalid-argument", f"{description} contains duplicate key: {key}"
                )
            result[key] = value
        return result

    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _CoordinatorCliError(
            "invalid-argument", f"{description} must be valid UTF-8 JSON."
        ) from exc
    if not isinstance(document, dict):
        raise _CoordinatorCliError("invalid-argument", f"{description} must be a JSON object.")
    return document


def _write_json_atomic(document: object, output_path: Path, description: str) -> Path:
    """Write ``document`` as an owner-only, fsynced, atomically-replaced file.

    Mirrors ``ci_shepherd.authorization.write_authorization_grant``'s
    algorithm exactly (same-directory temp file, O_EXCL|O_NOFOLLOW
    creation, fsync of file and parent directory, then ``os.replace``).
    That helper is reused as-is for grant output; this local copy exists
    only because the ``select`` command writes a distinct artifact
    (a policy selection, not a grant) that has no owning module of its
    own to write it for us.
    """

    expanded = Path(output_path).expanduser()
    if expanded.is_symlink() or any(parent.is_symlink() for parent in expanded.parents):
        raise _CoordinatorCliError("invalid-argument", f"{description} cannot traverse a symlink.")
    expanded.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    content = stable_json(document).encode("utf-8")
    temporary = expanded.parent / f".{expanded.name}.tmp-{os.getpid()}-{os.urandom(6).hex()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, expanded)
        os.chmod(expanded, 0o600)
        _fsync_directory(expanded.parent)
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()
    return expanded


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _build_coordinator_store(state_dir: Path) -> CoordinatorStateStore:
    return CoordinatorStateStore(
        state_dir,
        durable_intent_reader=make_lock_free_durable_intent_reader(
            state_dir / "action-events.jsonl"
        ),
    )


def _build_action_event_store(state_dir: Path) -> ActionEventStore:
    return ActionEventStore(state_dir)


def _reraise_as_stale_view_if_applicable(
    store: CoordinatorStateStore, repository: str, now: datetime, exc: CoordinatorStateError
) -> None:
    """Translate a compare-and-swap mismatch into a typed stale-view error.

    ``coordinator_state.py`` raises this one ``CoordinatorStateError``
    message shape for every other reason too (bad actor identity, bad
    document, and so on); only the CAS-mismatch case is prefixed with
    ``"stale-view"``, so that prefix is what selects this translation.
    A non-stale ``CoordinatorStateError`` is left for the caller to
    re-raise unchanged.
    """

    if str(exc).startswith("stale-view"):
        refreshed = store.projection(repository, now=now)
        raise _StaleViewError(_enrich_projection(refreshed, now)) from exc


def _stage_for(
    effective_policy: dict[str, object] | None,
    now: datetime,
    selection: dict[str, object] | None,
) -> str:
    if effective_policy is None:
        return "awaiting-policy"
    # Re-parse the recorded revision through the same schema validator that
    # accepted it (minus the read-model-only policyDigest field), so the
    # active/expired window check reuses Task 1's own rule rather than this
    # CLI re-deriving it.
    document = {key: value for key, value in effective_policy.items() if key != "policyDigest"}
    try:
        parsed = load_operation_policy_document(document)
    except OperationPolicyError:
        return "awaiting-policy"
    if not parsed.active_at(now):
        return "awaiting-policy"
    if selection is not None and selection.get("selectedActionIds"):
        return "ready"
    return "policy-active"


def _enrich_projection(
    projection: dict[str, object], now: datetime, selection: dict[str, object] | None = None
) -> dict[str, object]:
    payload = dict(projection)
    payload["stage"] = _stage_for(projection["effectivePolicy"], now, selection)
    if selection is not None:
        payload["selection"] = selection
    return payload


def _projection_payload(
    store: CoordinatorStateStore,
    repository: str,
    now: datetime,
    selection: dict[str, object] | None = None,
) -> dict[str, object]:
    projection = store.projection(repository, now=now)
    return _enrich_projection(projection, now, selection)


def _build_selection(
    store: CoordinatorStateStore,
    state_dir: Path,
    repository: str,
    proposals_path: Path,
    run_id: str,
    now: datetime,
) -> dict[str, object]:
    proposals_document = _read_json_file(proposals_path, "action proposals document")
    projection = store.projection(repository, now=now)
    action_events = _build_action_event_store(state_dir).events(repository=repository)
    return build_policy_selection(
        proposals_document,
        run_id=run_id,
        policy_projection=projection,
        action_events=action_events,
        now=now,
    )


def _read_operation_classes(caps_path: Path) -> dict[str, object]:
    document = _read_json_file(caps_path, "operation class caps document")
    if set(document) != set(OPERATION_CLASSES):
        raise _CoordinatorCliError(
            "invalid-argument",
            "Caps document must contain exactly the supported operation classes.",
        )
    return document


def _require_positive_expiry_days(expires_in_days: int) -> int:
    if (
        not isinstance(expires_in_days, int)
        or isinstance(expires_in_days, bool)
        or expires_in_days <= 0
    ):
        raise _CoordinatorCliError(
            "invalid-argument", "--expires-in-days must be a positive integer."
        )
    return expires_in_days


def _derive_activated_policy_document(
    current_effective_policy: dict[str, object] | None,
    *,
    repository: str,
    operation_classes: dict[str, object],
    expires_in_days: int,
    actor: str,
    now: datetime,
) -> dict[str, object]:
    """Derive a brand-new active revision from operator-supplied caps only.

    Every other field -- revision, revisionId, replacesRevisionId,
    createdAtUtc, expiresAtUtc, status -- is computed here so a caller can
    never smuggle an internal identity or timestamp in directly.
    """

    expires_in_days = _require_positive_expiry_days(expires_in_days)
    if current_effective_policy is None:
        revision = 1
        replaces_revision_id = None
    else:
        revision = int(current_effective_policy["revision"]) + 1
        replaces_revision_id = current_effective_policy["revisionId"]
    return {
        "schemaVersion": 1,
        "repository": repository,
        "revisionId": f"policy:{revision}",
        "revision": revision,
        "status": "active",
        "createdAtUtc": format_utc_z(now),
        "expiresAtUtc": format_utc_z(now + timedelta(days=expires_in_days)),
        "actor": actor,
        "replacesRevisionId": replaces_revision_id,
        "operationClasses": operation_classes,
        "deniedActionIds": [],
        "deniedTargets": [],
    }


def _derive_transition_policy_document(
    current_effective_policy: dict[str, object] | None,
    *,
    repository: str,
    actor: str,
    status: str,
) -> dict[str, object]:
    """Derive a pause/revoke revision, preserving the current window and caps.

    Only revision, revisionId, replacesRevisionId, actor, and status
    change; createdAtUtc, expiresAtUtc, operationClasses,
    deniedActionIds, and deniedTargets are carried over unchanged so a
    pause or revoke can never widen or narrow what the paused/revoked
    revision itself once allowed.
    """

    if current_effective_policy is None:
        raise _CoordinatorCliError(
            "invalid-request",
            f"No current policy exists for {repository!r} to {status}.",
        )
    revision = int(current_effective_policy["revision"]) + 1
    return {
        "schemaVersion": current_effective_policy["schemaVersion"],
        "repository": repository,
        "revisionId": f"policy:{revision}",
        "revision": revision,
        "status": status,
        "createdAtUtc": current_effective_policy["createdAtUtc"],
        "expiresAtUtc": current_effective_policy["expiresAtUtc"],
        "actor": actor,
        "replacesRevisionId": current_effective_policy["revisionId"],
        "operationClasses": current_effective_policy["operationClasses"],
        "deniedActionIds": current_effective_policy["deniedActionIds"],
        "deniedTargets": current_effective_policy["deniedTargets"],
    }


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def _cmd_projection(args: argparse.Namespace) -> dict[str, object]:
    if bool(args.proposals) != bool(args.run_id):
        missing = "--run-id" if args.proposals else "--proposals"
        raise _CoordinatorCliError(
            "invalid-argument",
            f"--proposals and --run-id must be supplied together (missing {missing}).",
        )
    now = args.now or datetime.now(UTC)
    store = _build_coordinator_store(args.state_dir)
    selection = None
    if args.proposals:
        selection = _build_selection(
            store, args.state_dir, args.repository, args.proposals, args.run_id, now
        )
    return _projection_payload(store, args.repository, now, selection)


def _cmd_policy_append(args: argparse.Namespace) -> dict[str, object]:
    now = datetime.now(UTC)
    store = _build_coordinator_store(args.state_dir)
    document = _read_json_file(args.document, "policy document")
    try:
        store.append_policy_revision(
            repository=args.repository,
            expected_revision=args.expected_revision,
            document=document,
        )
    except CoordinatorStateError as exc:
        _reraise_as_stale_view_if_applicable(store, args.repository, now, exc)
        raise
    return _projection_payload(store, args.repository, now)


def _cmd_policy_activate(args: argparse.Namespace) -> dict[str, object]:
    now = args.now
    store = _build_coordinator_store(args.state_dir)
    projection = store.projection(args.repository, now=now)
    operation_classes = _read_operation_classes(args.caps)
    document = _derive_activated_policy_document(
        projection["effectivePolicy"],
        repository=args.repository,
        operation_classes=operation_classes,
        expires_in_days=args.expires_in_days,
        actor=args.actor,
        now=now,
    )
    try:
        store.append_policy_revision(
            repository=args.repository,
            expected_revision=args.expected_revision,
            document=document,
        )
    except CoordinatorStateError as exc:
        _reraise_as_stale_view_if_applicable(store, args.repository, now, exc)
        raise
    return _projection_payload(store, args.repository, now)


def _transition_policy(args: argparse.Namespace, *, status: str) -> dict[str, object]:
    now = args.now
    store = _build_coordinator_store(args.state_dir)
    projection = store.projection(args.repository, now=now)
    document = _derive_transition_policy_document(
        projection["effectivePolicy"], repository=args.repository, actor=args.actor, status=status
    )
    try:
        store.append_policy_revision(
            repository=args.repository,
            expected_revision=args.expected_revision,
            document=document,
        )
    except CoordinatorStateError as exc:
        _reraise_as_stale_view_if_applicable(store, args.repository, now, exc)
        raise
    return _projection_payload(store, args.repository, now)


def _cmd_policy_pause(args: argparse.Namespace) -> dict[str, object]:
    return _transition_policy(args, status="paused")


def _cmd_policy_revoke(args: argparse.Namespace) -> dict[str, object]:
    return _transition_policy(args, status="revoked")


def _cmd_policy_preview(args: argparse.Namespace) -> dict[str, object]:
    now = args.now
    store = _build_coordinator_store(args.state_dir)
    projection = store.projection(args.repository, now=now)

    expected_revision = args.expected_revision
    if (
        not isinstance(expected_revision, int)
        or isinstance(expected_revision, bool)
        or expected_revision < 0
    ):
        raise _CoordinatorCliError(
            "invalid-argument", "--expected-revision must be a non-negative integer."
        )
    if expected_revision != projection["stateRevision"]:
        raise _StaleViewError(_enrich_projection(projection, now))

    operation_classes = _read_operation_classes(args.caps)
    # A preview never appears in the ledger, so it never has a real
    # "requesting actor" -- the actor identity only becomes meaningful
    # once the draft is actually activated with a caller-supplied
    # --actor via policy-activate. The sentinel below only needs to
    # satisfy Task 1's actor-identity shape; it names no real account.
    draft_document = _derive_activated_policy_document(
        projection["effectivePolicy"],
        repository=args.repository,
        operation_classes=operation_classes,
        expires_in_days=args.expires_in_days,
        actor=_PREVIEW_ACTOR,
        now=now,
    )
    parsed = load_operation_policy_document(draft_document)
    draft_projection = {
        "stateRevision": projection["stateRevision"],
        "effectivePolicy": {**parsed.as_public_dict(), "policyDigest": parsed.digest},
        "exactDecisions": projection["exactDecisions"],
    }
    proposals_document = _read_json_file(args.proposals, "action proposals document")
    action_events = _build_action_event_store(args.state_dir).events(repository=args.repository)
    selection = build_policy_selection(
        proposals_document,
        run_id=args.run_id,
        policy_projection=draft_projection,
        action_events=action_events,
        now=now,
    )
    return {
        "repository": args.repository,
        "draftPolicy": draft_projection["effectivePolicy"],
        "selection": selection,
    }


def _apply_decision(args: argparse.Namespace, *, decision: str) -> dict[str, object]:
    now = args.now
    store = _build_coordinator_store(args.state_dir)
    try:
        store.append_exact_decision(
            repository=args.repository,
            expected_revision=args.expected_revision,
            proposals_path=args.proposals,
            action_id=args.action_id,
            decision=decision,
            actor=args.actor,
            now=now,
        )
    except CoordinatorStateError as exc:
        _reraise_as_stale_view_if_applicable(store, args.repository, now, exc)
        raise
    return _projection_payload(store, args.repository, now)


def _cmd_decision_set(args: argparse.Namespace) -> dict[str, object]:
    return _apply_decision(args, decision=args.decision)


def _cmd_decision_clear(args: argparse.Namespace) -> dict[str, object]:
    return _apply_decision(args, decision="clear")


def _cmd_select(args: argparse.Namespace) -> dict[str, object]:
    now = args.now
    store = _build_coordinator_store(args.state_dir)
    selection = _build_selection(
        store, args.state_dir, args.repository, args.proposals, args.run_id, now
    )
    _write_json_atomic(selection, args.output, "policy selection output")
    return selection


def _cmd_grant_next(args: argparse.Namespace) -> dict[str, object]:
    now = args.now
    selection = _read_json_file(args.selection, "policy selection artifact")
    if selection.get("repository") != args.repository:
        raise _CoordinatorCliError(
            "invalid-request",
            "Policy selection artifact repository does not match the requested repository.",
        )
    exact_ids = selection.get("exactActionIds")
    automatic_ids = selection.get("automaticActionIds")
    if not isinstance(exact_ids, list) or not isinstance(automatic_ids, list):
        raise _CoordinatorCliError(
            "invalid-request", "Policy selection artifact is missing actionId lists."
        )

    if exact_ids:
        chosen = exact_ids[0]
    elif automatic_ids:
        chosen = automatic_ids[0]
    else:
        return {
            "granted": False,
            "reason": "no-eligible-action",
            "repository": args.repository,
        }

    # allow_autonomous_policy=True with exactly one action id and no
    # --ttl-minutes override (so the callee's own <=15 minute autonomous
    # cap applies) is what keeps this an internal, single-action child
    # grant rather than a general-purpose authorization.
    grant = generate_authorization_grant(
        args.proposals,
        action_ids=[chosen],
        state_dir=args.state_dir,
        allow_autonomous_policy=True,
        policy_selection_path=args.selection,
        policy_action_id=chosen,
        now=now,
    )
    write_authorization_grant(grant, args.output)
    return {"granted": True, "actionId": chosen, "repository": args.repository}


# ---------------------------------------------------------------------------
# argv wiring
# ---------------------------------------------------------------------------


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository", required=True)
    parser.add_argument("--state-dir", required=True, type=Path)


def _build_parser() -> _CoordinatorArgumentParser:
    parser = _CoordinatorArgumentParser(
        prog="coordinator.py",
        description=(
            "Deterministic, local, no-network CI Shepherd policy coordinator. "
            "Adapts the operation-policy, coordinator-state, policy-selection, "
            "and authorization modules into one operator-facing entrypoint."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    projection = subparsers.add_parser(
        "projection", help="Show the current coordinator projection for a repository."
    )
    _add_common_arguments(projection)
    projection.add_argument(
        "--proposals", type=Path, help="Compose an actionable selection into the projection."
    )
    projection.add_argument("--run-id", help="Required together with --proposals.")
    projection.add_argument("--now", type=_now_type)
    projection.set_defaults(handler=_cmd_projection)

    policy_append = subparsers.add_parser(
        "policy-append",
        help="Append an already-fully-derived policy revision document as-is.",
    )
    _add_common_arguments(policy_append)
    policy_append.add_argument("--expected-revision", required=True, type=int)
    policy_append.add_argument("--document", required=True, type=Path)
    policy_append.set_defaults(handler=_cmd_policy_append)

    policy_activate = subparsers.add_parser(
        "policy-activate", help="Derive and activate a brand-new policy revision."
    )
    _add_common_arguments(policy_activate)
    policy_activate.add_argument("--expected-revision", required=True, type=int)
    policy_activate.add_argument("--caps", required=True, type=Path)
    policy_activate.add_argument("--expires-in-days", required=True, type=int)
    policy_activate.add_argument("--actor", required=True)
    policy_activate.add_argument("--now", required=True, type=_now_type)
    policy_activate.set_defaults(handler=_cmd_policy_activate)

    policy_pause = subparsers.add_parser(
        "policy-pause", help="Derive and append a paused revision of the current policy."
    )
    _add_common_arguments(policy_pause)
    policy_pause.add_argument("--expected-revision", required=True, type=int)
    policy_pause.add_argument("--actor", required=True)
    policy_pause.add_argument("--now", required=True, type=_now_type)
    policy_pause.set_defaults(handler=_cmd_policy_pause)

    policy_revoke = subparsers.add_parser(
        "policy-revoke", help="Derive and append a revoked revision of the current policy."
    )
    _add_common_arguments(policy_revoke)
    policy_revoke.add_argument("--expected-revision", required=True, type=int)
    policy_revoke.add_argument("--actor", required=True)
    policy_revoke.add_argument("--now", required=True, type=_now_type)
    policy_revoke.set_defaults(handler=_cmd_policy_revoke)

    policy_preview = subparsers.add_parser(
        "policy-preview",
        help="Preview a draft policy's reachable actions and exposure without appending it.",
    )
    _add_common_arguments(policy_preview)
    policy_preview.add_argument("--expected-revision", required=True, type=int)
    policy_preview.add_argument("--caps", required=True, type=Path)
    policy_preview.add_argument("--expires-in-days", required=True, type=int)
    policy_preview.add_argument("--proposals", required=True, type=Path)
    policy_preview.add_argument("--run-id", required=True)
    policy_preview.add_argument("--now", required=True, type=_now_type)
    policy_preview.set_defaults(handler=_cmd_policy_preview)

    decision_set = subparsers.add_parser(
        "decision-set", help="Record an exact approve-once or reject-once decision."
    )
    _add_common_arguments(decision_set)
    decision_set.add_argument("--expected-revision", required=True, type=int)
    decision_set.add_argument("--proposals", required=True, type=Path)
    decision_set.add_argument("--action-id", required=True)
    decision_set.add_argument(
        "--decision", required=True, choices=["approve-once", "reject-once"]
    )
    decision_set.add_argument("--actor", required=True)
    decision_set.add_argument("--now", required=True, type=_now_type)
    decision_set.set_defaults(handler=_cmd_decision_set)

    decision_clear = subparsers.add_parser(
        "decision-clear", help="Clear a previously recorded exact decision."
    )
    _add_common_arguments(decision_clear)
    decision_clear.add_argument("--expected-revision", required=True, type=int)
    decision_clear.add_argument("--proposals", required=True, type=Path)
    decision_clear.add_argument("--action-id", required=True)
    decision_clear.add_argument("--actor", required=True)
    decision_clear.add_argument("--now", required=True, type=_now_type)
    decision_clear.set_defaults(handler=_cmd_decision_clear)

    select = subparsers.add_parser(
        "select", help="Compute and atomically persist a deterministic policy selection."
    )
    _add_common_arguments(select)
    select.add_argument("--proposals", required=True, type=Path)
    select.add_argument("--run-id", required=True)
    select.add_argument("--output", required=True, type=Path)
    select.add_argument("--now", required=True, type=_now_type)
    select.set_defaults(handler=_cmd_select)

    grant_next = subparsers.add_parser(
        "grant-next",
        help="Mint at most one internal, single-action autonomous-policy grant.",
    )
    _add_common_arguments(grant_next)
    grant_next.add_argument("--proposals", required=True, type=Path)
    grant_next.add_argument("--selection", required=True, type=Path)
    grant_next.add_argument("--output", required=True, type=Path)
    grant_next.add_argument("--now", required=True, type=_now_type)
    grant_next.set_defaults(handler=_cmd_grant_next)

    return parser


def _emit_error(code: str, message: str, **extra: object) -> None:
    payload: dict[str, object] = {"error": True, "code": code, "message": message}
    payload.update(extra)
    sys.stderr.write(stable_json(payload))


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        result = args.handler(args)
    except _StaleViewError as exc:
        _emit_error(
            "stale-view",
            "expected_revision does not match the current stateRevision.",
            projection=exc.projection,
        )
        return 2
    except _CoordinatorCliError as exc:
        _emit_error(exc.code, exc.message, **exc.extra)
        return 1
    except CoordinatorStateError as exc:
        _emit_error("coordinator-state-error", str(exc))
        return 1
    except OperationPolicyError as exc:
        _emit_error("operation-policy-error", str(exc))
        return 1
    except PolicySelectionError as exc:
        _emit_error("policy-selection-error", str(exc))
        return 1
    except AuthorizationError as exc:
        _emit_error("authorization-error", str(exc))
        return 1
    except (ValueError, TypeError) as exc:
        # Catches, among other things, ci_shepherd.actor.validate_action_
        # proposals' raw TypeError/ValueError, which build_policy_selection
        # deliberately does not wrap in PolicySelectionError.
        _emit_error("invalid-request", str(exc))
        return 1

    sys.stdout.write(stable_json(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
