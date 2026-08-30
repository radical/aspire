"""Exact, machine-readable authorization for CI shepherd mutations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import secrets
from typing import Any, Mapping, Sequence

from .timeutils import format_utc_z


class AuthorizationError(ValueError):
    """Raised when an execution grant does not authorize an action."""


#: Default and hard-maximum lifetime for a generated grant. A short default
#: keeps an unused grant from lingering; the maximum matches the 1-hour limit
#: `_load_grant` already enforces on any grant it reads, so a generator can
#: never mint something the loader would reject anyway.
DEFAULT_GRANT_TTL_MINUTES = 15
MAX_GRANT_TTL_MINUTES = 60


@dataclass(frozen=True, slots=True)
class AuthorizationBudget:
    max_mutation_attempts: int
    max_chains: int


@dataclass(frozen=True, slots=True)
class AuthorizationGrant:
    grant_id: str
    repository: str
    state_directory: Path
    issued_at: datetime
    expires_at: datetime
    snapshot_id: str
    proposals_digest: str
    allowed_action_ids: tuple[str, ...]
    allowed_operations: frozenset[str]
    allowed_targets: frozenset[tuple[str, int]]
    allowed_chain_roots: tuple[str, ...]
    override_suppression_for_action_ids: frozenset[str]
    budget: AuthorizationBudget


@dataclass(frozen=True, slots=True)
class AuthorizedExecution:
    proposal_document: Mapping[str, Any]
    proposal_bytes: bytes
    proposal: Mapping[str, Any]
    chain_root: str
    grant: AuthorizationGrant


_GRANT_KEYS = frozenset(
    {
        "schemaVersion",
        "grantId",
        "repository",
        "stateDirectory",
        "issuedAtUtc",
        "expiresAtUtc",
        "snapshotId",
        "proposalsDigest",
        "allowedActionIds",
        "allowedOperations",
        "allowedTargets",
        "allowedChainRoots",
        "overrideSuppressionForActionIds",
        "budget",
    }
)
_BUDGET_KEYS = frozenset({"maxMutationAttempts", "maxChains"})
_TARGET_KEYS = frozenset({"kind", "number"})


def load_authorized_execution(
    proposals_path: Path,
    authorization_path: Path,
    *,
    state_dir: Path,
    action_id: str,
    now: datetime | None = None,
) -> AuthorizedExecution:
    """Read once and validate the exact proposal document and authorization grant."""

    proposal_bytes, proposal_document = _read_and_validate_proposal_document(
        proposals_path
    )
    grant = _load_grant(
        _read_regular_file(authorization_path, "authorization grant")
    )
    current_time = now or datetime.now(UTC)

    if current_time.tzinfo is None:
        raise AuthorizationError("Authorization time must be timezone-aware.")
    if current_time < grant.issued_at:
        raise AuthorizationError("Authorization grant is not active yet.")
    if current_time >= grant.expires_at:
        raise AuthorizationError("Authorization grant has expired.")
    generated_at = _parse_timestamp(proposal_document, "generatedAtUtc")
    proposal_ttl_hours = _require_positive_int(
        proposal_document,
        "proposalTtlHours",
    )
    if current_time < generated_at:
        raise AuthorizationError("Proposal document is not active yet.")
    if current_time >= generated_at + timedelta(hours=proposal_ttl_hours):
        raise AuthorizationError("Proposal document has expired.")

    expanded_state_dir = state_dir.expanduser()
    _reject_symlink_path(expanded_state_dir, "State directory")
    canonical_state_dir = expanded_state_dir.resolve(strict=False)
    if canonical_state_dir != grant.state_directory:
        raise AuthorizationError(
            "Authorization grant stateDirectory does not match --state-dir."
        )

    digest = f"sha256:{hashlib.sha256(proposal_bytes).hexdigest()}"
    if digest != grant.proposals_digest:
        raise AuthorizationError(
            "Authorization grant proposalsDigest does not match proposal bytes."
        )

    repository = _require_string(proposal_document, "repository")
    snapshot_id = _require_string(proposal_document, "snapshotId")
    if repository.casefold() == "microsoft/aspire":
        raise AuthorizationError(
            "Mutation repository is protected during remediation: "
            "microsoft/aspire"
        )
    if repository != grant.repository:
        raise AuthorizationError(
            "Authorization grant repository does not match proposal document."
        )
    if snapshot_id != grant.snapshot_id:
        raise AuthorizationError(
            "Authorization grant snapshotId does not match proposal document."
        )
    if action_id not in grant.allowed_action_ids:
        raise AuthorizationError(
            f"Authorization grant does not enumerate actionId: {action_id}"
        )

    proposals = proposal_document.get("proposals")
    if not isinstance(proposals, list):
        raise AuthorizationError("Proposal document proposals must be an array.")
    matches = [
        proposal
        for proposal in proposals
        if isinstance(proposal, dict) and proposal.get("actionId") == action_id
    ]
    if len(matches) != 1:
        raise AuthorizationError(
            "Authorized actionId must identify exactly one proposal."
        )
    proposal = matches[0]
    document_eligibility = proposal_document.get("executionEligibility")
    if (
        not isinstance(document_eligibility, dict)
        or document_eligibility.get("status")
        not in {"eligible", "partially-eligible"}
    ):
        raise AuthorizationError("Proposal document is not eligible for execution.")
    eligibility = proposal.get("executionEligibility")
    if not isinstance(eligibility, dict) or eligibility.get("eligible") is not True:
        raise AuthorizationError(
            f"Authorized actionId is not eligible for execution: {action_id}"
        )
    operation = _require_string(proposal, "operation")
    issue_number = _require_positive_int(proposal, "issueNumber")
    if operation not in grant.allowed_operations:
        raise AuthorizationError(
            f"Authorization grant does not allow operation: {operation}"
        )
    if ("issue", issue_number) not in grant.allowed_targets:
        raise AuthorizationError(
            f"Authorization grant does not allow issue target: {issue_number}"
        )
    chain_root = _resolve_chain_root(proposals, action_id)
    if chain_root not in grant.allowed_chain_roots:
        raise AuthorizationError(
            f"Authorization grant does not allow chain root: {chain_root}"
        )
    if chain_root not in grant.allowed_action_ids:
        raise AuthorizationError(
            "Authorization grant chain roots must also be allowedActionIds."
        )

    return AuthorizedExecution(
        proposal_document=proposal_document,
        proposal_bytes=proposal_bytes,
        proposal=proposal,
        chain_root=chain_root,
        grant=grant,
    )


def _read_and_validate_proposal_document(
    proposals_path: Path,
) -> tuple[bytes, dict[str, Any]]:
    """Read the exact proposal bytes and validate schema-v2 structure.

    Shared by the authorization loader and the grant generator so both enforce
    identical schema, eligibility, and dependency-graph rules. This keeps a
    grant from ever being generated for a document the executor would reject.
    """

    proposal_bytes = _read_regular_file(proposals_path, "proposal document")
    proposal_document = _load_json_bytes(proposal_bytes, "proposal document")
    if proposal_document.get("schemaVersion") != 2:
        raise AuthorizationError(
            "Only action proposal schemaVersion 2 is executable."
        )
    from .actor import validate_action_proposals

    try:
        validate_action_proposals(proposal_document)
    except (TypeError, ValueError) as exc:
        raise AuthorizationError(f"Invalid proposal document: {exc}") from exc
    return proposal_bytes, proposal_document


def generate_authorization_grant(
    proposals_path: Path,
    *,
    action_ids: Sequence[str],
    state_dir: Path,
    ttl_minutes: int = DEFAULT_GRANT_TTL_MINUTES,
    override_suppression_for_action_ids: Sequence[str] = (),
    now: datetime | None = None,
    grant_id: str | None = None,
) -> dict[str, Any]:
    """Derive an exact, schema-v1 authorization grant for explicitly selected actions.

    Every allowed action id, operation, issue target, and chain root is
    derived only from proposals the caller names in ``action_ids``. Nothing
    is inferred: a selected action whose ``dependsOn`` is not itself selected
    is rejected rather than silently pulled in, so approving one action can
    never authorize another effect a human did not see. The mutation and
    chain budgets are likewise derived counts of the exact selection, not a
    caller-supplied number.

    ``now`` and ``grant_id`` exist so tests can pin the clock and identifier;
    the public CLI never exposes either, always using the real clock and a
    freshly generated identifier.
    """

    if not isinstance(ttl_minutes, int) or isinstance(ttl_minutes, bool):
        raise AuthorizationError("Grant TTL must be an integer number of minutes.")
    if not (1 <= ttl_minutes <= MAX_GRANT_TTL_MINUTES):
        raise AuthorizationError(
            f"Grant TTL must be between 1 and {MAX_GRANT_TTL_MINUTES} minutes."
        )

    proposal_bytes, proposal_document = _read_and_validate_proposal_document(
        proposals_path
    )

    repository = _require_repository(proposal_document, "repository")
    if repository.casefold() == "microsoft/aspire":
        raise AuthorizationError(
            "Mutation repository is protected during remediation: "
            "microsoft/aspire"
        )
    snapshot_id = _require_string(proposal_document, "snapshotId")

    document_eligibility = proposal_document.get("executionEligibility")
    if (
        not isinstance(document_eligibility, dict)
        or document_eligibility.get("status")
        not in {"eligible", "partially-eligible"}
    ):
        raise AuthorizationError("Proposal document is not eligible for execution.")

    proposals = proposal_document.get("proposals")
    if not isinstance(proposals, list):
        raise AuthorizationError("Proposal document proposals must be an array.")
    by_action_id = {
        proposal["actionId"]: proposal
        for proposal in proposals
        if isinstance(proposal, dict)
    }

    if not action_ids:
        raise AuthorizationError("At least one actionId must be selected.")
    selected_ids: set[str] = set()
    for action_id in action_ids:
        if not isinstance(action_id, str) or not action_id:
            raise AuthorizationError("Selected action ids must be non-empty strings.")
        if action_id in selected_ids:
            raise AuthorizationError(f"Duplicate selected actionId: {action_id}")
        selected_ids.add(action_id)

    selected_proposals: list[dict[str, Any]] = []
    for action_id in selected_ids:
        proposal = by_action_id.get(action_id)
        if proposal is None:
            raise AuthorizationError(
                f"Selected actionId is not in the proposal document: {action_id}"
            )
        eligibility = proposal.get("executionEligibility")
        if not isinstance(eligibility, dict) or eligibility.get("eligible") is not True:
            raise AuthorizationError(
                f"Selected actionId is not eligible for execution: {action_id}"
            )
        depends_on = proposal.get("dependsOn")
        if depends_on is not None and depends_on not in selected_ids:
            raise AuthorizationError(
                f"Selected actionId {action_id} depends on {depends_on}, which "
                "is not also selected. Approving one action never authorizes "
                "another effect."
            )
        selected_proposals.append(proposal)

    allowed_operations = sorted(
        {_require_string(proposal, "operation") for proposal in selected_proposals}
    )
    allowed_issue_numbers = sorted(
        {
            _require_positive_int(proposal, "issueNumber")
            for proposal in selected_proposals
        }
    )
    chain_roots = sorted(
        {_resolve_chain_root(proposals, action_id) for action_id in selected_ids}
    )
    for chain_root in chain_roots:
        # Guaranteed unreachable by the per-action dependsOn check above
        # (dependsOn is a single chain, so requiring every selected step's
        # dependency to also be selected forces the whole path up to the
        # root to be selected). Kept as a fail-closed invariant check.
        if chain_root not in selected_ids:
            raise AuthorizationError(
                f"Chain root {chain_root} is not among the selected action ids."
            )

    override_ids: set[str] = set()
    for override_id in override_suppression_for_action_ids:
        if not isinstance(override_id, str) or not override_id:
            raise AuthorizationError(
                "Suppression override action ids must be non-empty strings."
            )
        if override_id in override_ids:
            raise AuthorizationError(
                f"Duplicate suppression override actionId: {override_id}"
            )
        override_ids.add(override_id)
        if override_id not in selected_ids:
            raise AuthorizationError(
                "Suppression overrides must reference a selected actionId: "
                f"{override_id}"
            )

    expanded_state_dir = state_dir.expanduser()
    _reject_symlink_path(expanded_state_dir, "State directory")
    canonical_state_dir = expanded_state_dir.resolve(strict=False)

    issued_at = now if now is not None else datetime.now(UTC)
    if issued_at.tzinfo is None:
        raise AuthorizationError("Grant issuedAtUtc must be timezone-aware.")
    issued_at = issued_at.astimezone(UTC)
    expires_at = issued_at + timedelta(minutes=ttl_minutes)

    if grant_id is not None and (not isinstance(grant_id, str) or not grant_id):
        raise AuthorizationError("grantId must be a non-empty string.")

    return {
        "schemaVersion": 1,
        "grantId": grant_id if grant_id is not None else _generate_grant_id(),
        "repository": repository,
        "stateDirectory": str(canonical_state_dir),
        "issuedAtUtc": format_utc_z(issued_at),
        "expiresAtUtc": format_utc_z(expires_at),
        "snapshotId": snapshot_id,
        "proposalsDigest": f"sha256:{hashlib.sha256(proposal_bytes).hexdigest()}",
        "allowedActionIds": sorted(selected_ids),
        "allowedOperations": allowed_operations,
        "allowedTargets": [
            {"kind": "issue", "number": number} for number in allowed_issue_numbers
        ],
        "allowedChainRoots": chain_roots,
        "overrideSuppressionForActionIds": sorted(override_ids),
        "budget": {
            "maxMutationAttempts": len(selected_ids),
            "maxChains": len(chain_roots),
        },
    }


def _generate_grant_id() -> str:
    return f"grant:{secrets.token_hex(16)}"


def write_authorization_grant(grant: Mapping[str, Any], output_path: Path) -> Path:
    """Atomically write a generated grant as an owner-only JSON file.

    Refuses to write through a symlinked output path or a symlinked parent
    directory, mirroring the checks `load_authorized_execution` applies when
    reading a grant back. The write itself uses a same-directory temporary
    file (exclusive creation, no symlink following) that is fsynced and then
    renamed into place, so a crash mid-write can never leave a partially
    written grant at the final path.
    """

    expanded = output_path.expanduser()
    _reject_symlink_path(expanded, "Authorization output path")
    expanded.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    content = (json.dumps(grant, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = expanded.parent / f".{expanded.name}.tmp-{os.getpid()}-{secrets.token_hex(6)}"
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


def _read_regular_file(path: Path, description: str) -> bytes:
    expanded = path.expanduser()
    _reject_symlink_path(expanded, description.capitalize())
    try:
        return expanded.read_bytes()
    except OSError as exc:
        raise AuthorizationError(f"Unable to read {description}: {expanded}") from exc


def _load_json_bytes(payload: bytes, description: str) -> dict[str, Any]:
    def reject_duplicate_pairs(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise AuthorizationError(
                    f"{description.capitalize()} contains duplicate key: {key}"
                )
            result[key] = value
        return result

    try:
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=reject_duplicate_pairs,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthorizationError(
            f"{description.capitalize()} must be valid UTF-8 JSON."
        ) from exc
    if not isinstance(document, dict):
        raise AuthorizationError(f"{description.capitalize()} must be an object.")
    return document


def _load_grant(payload: bytes) -> AuthorizationGrant:
    document = _load_json_bytes(payload, "authorization grant")
    if set(document) != _GRANT_KEYS:
        raise AuthorizationError(
            "Authorization grant must contain exactly the supported fields."
        )
    if document.get("schemaVersion") != 1:
        raise AuthorizationError("Authorization grant schemaVersion must equal 1.")

    grant_id = _require_string(document, "grantId")
    repository = _require_repository(document, "repository")
    state_directory_text = _require_string(document, "stateDirectory")
    state_directory = Path(state_directory_text)
    if not state_directory.is_absolute():
        raise AuthorizationError(
            "Authorization grant stateDirectory must be absolute."
        )
    _reject_symlink_path(state_directory, "Authorization stateDirectory")
    state_directory = state_directory.resolve(strict=False)

    issued_at = _parse_timestamp(document, "issuedAtUtc")
    expires_at = _parse_timestamp(document, "expiresAtUtc")
    if expires_at <= issued_at:
        raise AuthorizationError(
            "Authorization grant expiresAtUtc must follow issuedAtUtc."
        )
    if expires_at - issued_at > timedelta(hours=1):
        raise AuthorizationError(
            "Authorization grant lifetime must be at most 1 hour."
        )

    snapshot_id = _require_string(document, "snapshotId")
    proposals_digest = _require_string(document, "proposalsDigest")
    if not proposals_digest.startswith("sha256:") or len(proposals_digest) != 71:
        raise AuthorizationError(
            "Authorization grant proposalsDigest must be a SHA-256 digest."
        )
    try:
        int(proposals_digest.removeprefix("sha256:"), 16)
    except ValueError as exc:
        raise AuthorizationError(
            "Authorization grant proposalsDigest must be hexadecimal."
        ) from exc

    allowed_action_ids = _require_unique_strings(document, "allowedActionIds")
    allowed_operations = frozenset(
        _require_unique_strings(document, "allowedOperations")
    )
    unsupported_operations = allowed_operations - {
        "create-comment",
        "edit-comment",
        "close-issue",
    }
    if unsupported_operations:
        raise AuthorizationError(
            "Authorization grant contains unsupported operations: "
            f"{sorted(unsupported_operations)}"
        )
    allowed_chain_roots = _require_unique_strings(document, "allowedChainRoots")
    override_ids = frozenset(
        _require_unique_strings(
            document,
            "overrideSuppressionForActionIds",
            allow_empty=True,
        )
    )
    if not override_ids.issubset(set(allowed_action_ids)):
        raise AuthorizationError(
            "Suppression overrides must reference allowedActionIds."
        )
    if not set(allowed_chain_roots).issubset(set(allowed_action_ids)):
        raise AuthorizationError(
            "Authorization chain roots must reference allowedActionIds."
        )

    target_values = document.get("allowedTargets")
    if not isinstance(target_values, list) or not target_values:
        raise AuthorizationError(
            "Authorization grant allowedTargets must be a non-empty array."
        )
    targets: set[tuple[str, int]] = set()
    for target in target_values:
        if not isinstance(target, dict) or set(target) != _TARGET_KEYS:
            raise AuthorizationError(
                "Each authorization target must contain kind and number."
            )
        kind = _require_string(target, "kind")
        number = _require_positive_int(target, "number")
        if kind != "issue":
            raise AuthorizationError(
                f"Unsupported authorization target kind: {kind}"
            )
        if (kind, number) in targets:
            raise AuthorizationError("Authorization targets must be unique.")
        targets.add((kind, number))

    budget_document = document.get("budget")
    if not isinstance(budget_document, dict) or set(budget_document) != _BUDGET_KEYS:
        raise AuthorizationError(
            "Authorization grant budget must contain maxMutationAttempts and maxChains."
        )
    budget = AuthorizationBudget(
        max_mutation_attempts=_require_positive_int(
            budget_document, "maxMutationAttempts"
        ),
        max_chains=_require_positive_int(budget_document, "maxChains"),
    )

    return AuthorizationGrant(
        grant_id=grant_id,
        repository=repository,
        state_directory=state_directory,
        issued_at=issued_at,
        expires_at=expires_at,
        snapshot_id=snapshot_id,
        proposals_digest=proposals_digest,
        allowed_action_ids=allowed_action_ids,
        allowed_operations=allowed_operations,
        allowed_targets=frozenset(targets),
        allowed_chain_roots=allowed_chain_roots,
        override_suppression_for_action_ids=override_ids,
        budget=budget,
    )


def _require_string(document: Mapping[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise AuthorizationError(f"{key} must be a non-empty string.")
    return value


def _require_repository(document: Mapping[str, Any], key: str) -> str:
    value = _require_string(document, key)
    parts = value.split("/")
    if len(parts) != 2 or not all(parts):
        raise AuthorizationError(f"{key} must have owner/repository form.")
    return value


def _require_positive_int(document: Mapping[str, Any], key: str) -> int:
    value = document.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise AuthorizationError(f"{key} must be a positive integer.")
    return value


def _require_unique_strings(
    document: Mapping[str, Any],
    key: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    value = document.get(key)
    if not isinstance(value, list) or (not value and not allow_empty):
        expected = "a string array" if allow_empty else "a non-empty string array"
        raise AuthorizationError(f"{key} must be {expected}.")
    if not all(isinstance(item, str) and item for item in value):
        raise AuthorizationError(f"{key} must contain only non-empty strings.")
    if len(set(value)) != len(value):
        raise AuthorizationError(f"{key} must not contain duplicates.")
    return tuple(value)


def _parse_timestamp(document: Mapping[str, Any], key: str) -> datetime:
    value = _require_string(document, key)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthorizationError(f"{key} must be an ISO 8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise AuthorizationError(f"{key} must include a UTC offset.")
    return parsed.astimezone(UTC)


def _reject_symlink_path(path: Path, description: str) -> None:
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise AuthorizationError(f"{description} cannot traverse a symlink.")


def _resolve_chain_root(
    proposals: list[Any],
    action_id: str,
) -> str:
    by_action_id = {
        proposal.get("actionId"): proposal
        for proposal in proposals
        if isinstance(proposal, dict)
        and isinstance(proposal.get("actionId"), str)
    }
    current = action_id
    visited: set[str] = set()
    while True:
        if current in visited:
            raise AuthorizationError("Proposal dependency graph contains a cycle.")
        visited.add(current)
        proposal = by_action_id.get(current)
        if proposal is None:
            raise AuthorizationError(
                f"Proposal dependency references unknown actionId: {current}"
            )
        depends_on = proposal.get("dependsOn")
        if depends_on is None:
            return current
        if not isinstance(depends_on, str) or not depends_on:
            raise AuthorizationError("Proposal dependsOn must be a non-empty string.")
        current = depends_on
