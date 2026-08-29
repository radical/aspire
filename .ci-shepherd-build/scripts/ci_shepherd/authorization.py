"""Exact, machine-readable authorization for CI shepherd mutations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


class AuthorizationError(ValueError):
    """Raised when an execution grant does not authorize an action."""


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
        or document_eligibility.get("status") != "eligible"
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
