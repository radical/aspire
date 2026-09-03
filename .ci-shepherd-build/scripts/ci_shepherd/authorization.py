"""Exact, machine-readable authorization for CI shepherd mutations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
from typing import Any, Mapping, Sequence

from .capacity_policy import (
    DEFAULT_PRODUCTION_DELEGATION_POLICY_PATH,
    ProductionDelegationPolicy,
    load_production_delegation_policy,
)
from .quarantine import current_quarantine_source_fingerprint
from .timeutils import format_utc_z


class AuthorizationError(ValueError):
    """Raised when an execution grant does not authorize an action."""


#: Default and hard-maximum lifetime for a generated grant. A short default
#: keeps an unused grant from lingering; the maximum matches the 1-hour limit
#: `_load_grant` already enforces on any grant it reads, so a generator can
#: never mint something the loader would reject anyway.
DEFAULT_GRANT_TTL_MINUTES = 15
MAX_GRANT_TTL_MINUTES = 60
AUTHORIZATION_SCHEMA_VERSION = 2
PRODUCTION_REPOSITORY = "microsoft/aspire"
PRODUCTION_COMMENT_OPERATIONS = frozenset({"create-comment", "edit-comment"})
MAX_PRODUCTION_COMMENT_ACTIONS = 5
PRODUCTION_DELEGATION_OPERATIONS = frozenset({"assign-copilot"})
MAX_PRODUCTION_DELEGATION_ACTIONS = 1
MAX_PRODUCTION_SNAPSHOT_AGE = timedelta(minutes=45)
DEFAULT_MAX_RUNNING_COPILOT_TASKS = 2
DEFAULT_MAX_COPILOT_STARTS_PER_ROLLING_24H = 3
DEFAULT_MAX_OPEN_DELEGATED_PRS = 5
DEFAULT_MAX_REPOSITORY_RUNNING_COPILOT_TASKS = 100


@dataclass(frozen=True, slots=True)
class AuthorizationBudget:
    max_mutation_attempts: int
    max_chains: int
    max_running_copilot_tasks: int = DEFAULT_MAX_RUNNING_COPILOT_TASKS
    max_copilot_starts_per_rolling_24h: int = (
        DEFAULT_MAX_COPILOT_STARTS_PER_ROLLING_24H
    )
    max_open_delegated_prs: int = DEFAULT_MAX_OPEN_DELEGATED_PRS
    max_repository_running_copilot_tasks: int = (
        DEFAULT_MAX_REPOSITORY_RUNNING_COPILOT_TASKS
    )


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
    production_comment_pilot: bool
    production_delegation_pilot: bool = False
    production_delegation_steady_state: bool = False
    capacity_policy_digest: str | None = None
    comment_selection_digest: str | None = None


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
        "productionCommentPilot",
    }
)
_PRODUCTION_DELEGATION_GRANT_KEY = "productionDelegationPilot"
_PRODUCTION_DELEGATION_STEADY_STATE_KEY = "productionDelegationSteadyState"
_CAPACITY_POLICY_DIGEST_KEY = "capacityPolicyDigest"
_COMMENT_SELECTION_DIGEST_KEY = "commentSelectionDigest"
_CURRENT_CAPABILITY_KEYS = frozenset(
    {
        _PRODUCTION_DELEGATION_GRANT_KEY,
        _PRODUCTION_DELEGATION_STEADY_STATE_KEY,
        _CAPACITY_POLICY_DIGEST_KEY,
    }
)
_BUDGET_KEYS = frozenset(
    {
        "maxMutationAttempts",
        "maxChains",
        "maxRunningCopilotTasks",
        "maxCopilotStartsPerRolling24h",
        "maxOpenDelegatedPullRequests",
        "maxRepositoryRunningCopilotTasks",
    }
)
_TARGET_KEYS = frozenset({"kind", "number"})


def load_authorized_execution(
    proposals_path: Path,
    authorization_path: Path,
    *,
    state_dir: Path,
    action_id: str,
    comment_selection_path: Path | None = None,
    source_checkout_path: Path | None = None,
    allow_production_comment_pilot: bool = False,
    allow_production_delegation_pilot: bool = False,
    allow_production_delegation_steady_state: bool = False,
    production_delegation_policy_path: Path = (
        DEFAULT_PRODUCTION_DELEGATION_POLICY_PATH
    ),
    now: datetime | None = None,
) -> AuthorizedExecution:
    """Read once and validate the exact proposal document and authorization grant."""

    if not isinstance(allow_production_comment_pilot, bool):
        raise AuthorizationError(
            "allow_production_comment_pilot must be a boolean."
        )
    if not isinstance(allow_production_delegation_pilot, bool):
        raise AuthorizationError(
            "allow_production_delegation_pilot must be a boolean."
        )
    if not isinstance(allow_production_delegation_steady_state, bool):
        raise AuthorizationError(
            "allow_production_delegation_steady_state must be a boolean."
        )
    if sum(
        (
            allow_production_comment_pilot,
            allow_production_delegation_pilot,
            allow_production_delegation_steady_state,
        )
    ) > 1:
        raise AuthorizationError(
            "Production mutation capabilities are mutually exclusive."
        )
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
    if grant.comment_selection_digest is not None:
        if comment_selection_path is None:
            conventional_selection_path = proposals_path.with_name(
                "comment-selection.json"
            )
            if conventional_selection_path.exists():
                comment_selection_path = conventional_selection_path
            else:
                raise AuthorizationError(
                    "Authorization grant requires the bound comment selection "
                    "artifact."
                )
        selection_bytes = _read_regular_file(
            comment_selection_path,
            "comment selection",
        )
        selection_digest = f"sha256:{hashlib.sha256(selection_bytes).hexdigest()}"
        if selection_digest != grant.comment_selection_digest:
            raise AuthorizationError(
                "Authorization grant commentSelectionDigest does not match "
                "comment selection bytes."
            )
        _validate_comment_selection(
            selection_bytes,
            proposal_document=proposal_document,
            proposals_digest=digest,
            selected_action_ids=grant.allowed_action_ids,
        )
    elif comment_selection_path is not None:
        raise AuthorizationError(
            "Authorization grant does not bind a comment selection artifact."
        )

    repository = _require_string(proposal_document, "repository")
    snapshot_id = _require_string(proposal_document, "snapshotId")
    is_production = repository.casefold() == PRODUCTION_REPOSITORY
    production_pilot_enabled = (
        allow_production_comment_pilot
        or allow_production_delegation_pilot
        or allow_production_delegation_steady_state
    )
    if is_production and not production_pilot_enabled:
        raise AuthorizationError(
            "Mutation repository is protected during remediation: "
            "microsoft/aspire"
        )
    if production_pilot_enabled and not is_production:
        raise AuthorizationError(
            "Production pilot authorization is only valid for "
            "microsoft/aspire."
        )
    if grant.production_comment_pilot != allow_production_comment_pilot:
        raise AuthorizationError(
            "Production comment pilot confirmation does not match the grant."
        )
    if grant.production_comment_pilot and grant.comment_selection_digest is None:
        raise AuthorizationError(
            "Production comment pilot grant must bind a comment selection artifact."
        )
    if (
        grant.production_delegation_pilot
        != allow_production_delegation_pilot
    ):
        raise AuthorizationError(
            "Production delegation pilot confirmation does not match the grant."
        )
    if (
        grant.production_delegation_steady_state
        != allow_production_delegation_steady_state
    ):
        raise AuthorizationError(
            "Production delegation steady-state confirmation does not match the grant."
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
    if proposal.get("evidenceBasis") == "source-reconciliation":
        if source_checkout_path is None:
            raise AuthorizationError(
                "Source-reconciliation execution requires source_checkout_path."
            )
        current_fingerprint = current_quarantine_source_fingerprint(
            source_checkout_path
        )
        expected_fingerprint = proposal.get("sourceEvidenceFingerprint")
        if (
            current_fingerprint is None
            or not isinstance(expected_fingerprint, Mapping)
            or any(
                current_fingerprint[field] != expected_fingerprint.get(field)
                for field in (
                    "sourceRevision",
                    "sourceTreeDigest",
                    "inspectorTreeDigest",
                )
            )
        ):
            raise AuthorizationError(
            "Source-reconciliation evidence is unavailable or changed before "
            "execution."
        )
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
    if is_production:
        if allow_production_comment_pilot:
            _validate_production_comment_grant(
                grant,
                action_id=action_id,
                operation=operation,
                proposals=proposals,
                capability=proposal_document.get("productionPilotCapability"),
            )
        elif allow_production_delegation_pilot:
            _validate_production_delegation_grant(
                grant,
                action_id=action_id,
                operation=operation,
                proposals=proposals,
                capability=proposal_document.get("productionPilotCapability"),
            )
        else:
            policy = _load_capacity_policy(production_delegation_policy_path)
            _validate_production_delegation_steady_state_grant(
                grant,
                action_id=action_id,
                operation=operation,
                proposals=proposals,
                capability=proposal_document.get("productionPilotCapability"),
                policy=policy,
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


def _validate_comment_selection(
    selection_bytes: bytes,
    *,
    proposal_document: Mapping[str, Any],
    proposals_digest: str,
    selected_action_ids: Sequence[str],
) -> None:
    selection = _load_json_bytes(selection_bytes, "comment selection")
    if selection.get("schemaVersion") != 1:
        raise AuthorizationError("Comment selection schemaVersion must equal 1.")
    if selection.get("repository") != proposal_document.get("repository"):
        raise AuthorizationError(
            "Comment selection repository does not match proposal document."
        )
    if selection.get("snapshotId") != proposal_document.get("snapshotId"):
        raise AuthorizationError(
            "Comment selection snapshotId does not match proposal document."
        )
    if selection.get("proposalsDigest") != proposals_digest:
        raise AuthorizationError(
            "Comment selection proposalsDigest does not match proposal bytes."
        )
    from .comment_selection import build_comment_selection

    try:
        expected_selection = build_comment_selection(
            proposal_document,
            max_comments=selection.get("maxComments"),
        )
    except (TypeError, ValueError) as exc:
        raise AuthorizationError(f"Invalid comment selection: {exc}") from exc
    if selection != expected_selection:
        raise AuthorizationError(
            "Comment selection does not match the deterministically recomputed "
            "selection."
        )
    selected = selection.get("selectedActionIds")
    if not isinstance(selected, list) or any(
        not isinstance(action_id, str) or not action_id for action_id in selected
    ):
        raise AuthorizationError(
            "Comment selection selectedActionIds must be an array of strings."
        )
    if selected != list(selected_action_ids):
        raise AuthorizationError(
            "Selected action ids must exactly match the ordered comment selection."
        )


def generate_authorization_grant(
    proposals_path: Path,
    *,
    action_ids: Sequence[str],
    state_dir: Path,
    comment_selection_path: Path | None = None,
    ttl_minutes: int = DEFAULT_GRANT_TTL_MINUTES,
    max_running_copilot_tasks: int = DEFAULT_MAX_RUNNING_COPILOT_TASKS,
    max_copilot_starts_per_rolling_24h: int = (
        DEFAULT_MAX_COPILOT_STARTS_PER_ROLLING_24H
    ),
    max_open_delegated_prs: int = DEFAULT_MAX_OPEN_DELEGATED_PRS,
    max_repository_running_copilot_tasks: int = (
        DEFAULT_MAX_REPOSITORY_RUNNING_COPILOT_TASKS
    ),
    override_suppression_for_action_ids: Sequence[str] = (),
    allow_production_comment_pilot: bool = False,
    allow_production_delegation_pilot: bool = False,
    allow_production_delegation_steady_state: bool = False,
    production_delegation_policy_path: Path = (
        DEFAULT_PRODUCTION_DELEGATION_POLICY_PATH
    ),
    now: datetime | None = None,
    grant_id: str | None = None,
) -> dict[str, Any]:
    """Derive an exact authorization grant for explicitly selected actions.

    Every allowed action id, operation, issue target, and chain root is
    derived only from proposals the caller names in ``action_ids``. Nothing
    is inferred: a selected action whose ``dependsOn`` is not itself selected
    is rejected rather than silently pulled in, so approving one action can
    never authorize another effect a human did not see. The mutation and
    chain budgets are likewise derived counts of the exact selection. Copilot
    task and pull-request capacity limits are explicit grant inputs so the
    executor cannot raise them independently after approval.

    ``now`` and ``grant_id`` exist so tests can pin the clock and identifier;
    the public CLI never exposes either, always using the real clock and a
    freshly generated identifier.
    """

    if not isinstance(allow_production_comment_pilot, bool):
        raise AuthorizationError(
            "allow_production_comment_pilot must be a boolean."
        )
    if not isinstance(allow_production_delegation_pilot, bool):
        raise AuthorizationError(
            "allow_production_delegation_pilot must be a boolean."
        )
    if not isinstance(allow_production_delegation_steady_state, bool):
        raise AuthorizationError(
            "allow_production_delegation_steady_state must be a boolean."
        )
    if sum(
        (
            allow_production_comment_pilot,
            allow_production_delegation_pilot,
            allow_production_delegation_steady_state,
        )
    ) > 1:
        raise AuthorizationError(
            "Production mutation capabilities are mutually exclusive."
        )
    if not isinstance(ttl_minutes, int) or isinstance(ttl_minutes, bool):
        raise AuthorizationError("Grant TTL must be an integer number of minutes.")
    if not (1 <= ttl_minutes <= MAX_GRANT_TTL_MINUTES):
        raise AuthorizationError(
            f"Grant TTL must be between 1 and {MAX_GRANT_TTL_MINUTES} minutes."
        )
    for name, value in (
        ("max_running_copilot_tasks", max_running_copilot_tasks),
        (
            "max_copilot_starts_per_rolling_24h",
            max_copilot_starts_per_rolling_24h,
        ),
        ("max_open_delegated_prs", max_open_delegated_prs),
        (
            "max_repository_running_copilot_tasks",
            max_repository_running_copilot_tasks,
        ),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise AuthorizationError(f"{name} must be a nonnegative integer.")

    proposal_bytes, proposal_document = _read_and_validate_proposal_document(
        proposals_path
    )

    repository = _require_repository(proposal_document, "repository")
    is_production = repository.casefold() == PRODUCTION_REPOSITORY
    production_pilot_enabled = (
        allow_production_comment_pilot
        or allow_production_delegation_pilot
        or allow_production_delegation_steady_state
    )
    if is_production and not production_pilot_enabled:
        raise AuthorizationError(
            "Mutation repository is protected during remediation: "
            "microsoft/aspire"
        )
    if production_pilot_enabled and not is_production:
        raise AuthorizationError(
            "Production pilot authorization is only valid for "
            "microsoft/aspire."
        )
    snapshot_id = _require_string(proposal_document, "snapshotId")
    issued_at = now if now is not None else datetime.now(UTC)
    if issued_at.tzinfo is None:
        raise AuthorizationError("Grant issuedAtUtc must be timezone-aware.")
    issued_at = issued_at.astimezone(UTC)

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
    selected_action_ids: list[str] = []
    selected_ids: set[str] = set()
    for action_id in action_ids:
        if not isinstance(action_id, str) or not action_id:
            raise AuthorizationError("Selected action ids must be non-empty strings.")
        if action_id in selected_ids:
            raise AuthorizationError(f"Duplicate selected actionId: {action_id}")
        selected_ids.add(action_id)
        selected_action_ids.append(action_id)

    comment_selection_digest: str | None = None
    if comment_selection_path is not None:
        selection_bytes = _read_regular_file(
            comment_selection_path,
            "comment selection",
        )
        _validate_comment_selection(
            selection_bytes,
            proposal_document=proposal_document,
            proposals_digest=(
                f"sha256:{hashlib.sha256(proposal_bytes).hexdigest()}"
            ),
            selected_action_ids=selected_action_ids,
        )
        comment_selection_digest = (
            f"sha256:{hashlib.sha256(selection_bytes).hexdigest()}"
        )
    elif allow_production_comment_pilot:
        raise AuthorizationError(
            "Production comment pilot authorization requires "
            "comment_selection_path."
        )

    selected_proposals: list[dict[str, Any]] = []
    for action_id in selected_action_ids:
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
    if is_production:
        if allow_production_comment_pilot:
            _validate_production_comment_selection(
                selected_proposals,
                ttl_minutes=ttl_minutes,
                override_ids=override_ids,
            )
        elif allow_production_delegation_pilot:
            _validate_production_delegation_selection(
                selected_proposals,
                ttl_minutes=ttl_minutes,
                override_ids=override_ids,
                max_running_copilot_tasks=max_running_copilot_tasks,
                max_copilot_starts_per_rolling_24h=(
                    max_copilot_starts_per_rolling_24h
                ),
                max_open_delegated_prs=max_open_delegated_prs,
                max_repository_running_copilot_tasks=(
                    max_repository_running_copilot_tasks
                ),
            )
        else:
            policy = _load_capacity_policy(production_delegation_policy_path)
            _validate_production_delegation_steady_state_selection(
                selected_proposals,
                ttl_minutes=ttl_minutes,
                override_ids=override_ids,
                max_running_copilot_tasks=max_running_copilot_tasks,
                max_copilot_starts_per_rolling_24h=(
                    max_copilot_starts_per_rolling_24h
                ),
                max_open_delegated_prs=max_open_delegated_prs,
                max_repository_running_copilot_tasks=(
                    max_repository_running_copilot_tasks
                ),
                policy=policy,
            )
        production_freshness_deadline = _production_freshness_deadline(
            snapshot_id,
            capability=proposal_document.get("productionPilotCapability"),
            repository=repository,
            issued_at=issued_at,
        )
    else:
        production_freshness_deadline = None

    expanded_state_dir = state_dir.expanduser()
    _reject_symlink_path(expanded_state_dir, "State directory")
    canonical_state_dir = expanded_state_dir.resolve(strict=False)

    expires_at = issued_at + timedelta(minutes=ttl_minutes)
    if production_freshness_deadline is not None:
        expires_at = min(expires_at, production_freshness_deadline)

    if grant_id is not None and (not isinstance(grant_id, str) or not grant_id):
        raise AuthorizationError("grantId must be a non-empty string.")

    grant = {
        "schemaVersion": AUTHORIZATION_SCHEMA_VERSION,
        "grantId": grant_id if grant_id is not None else _generate_grant_id(),
        "repository": repository,
        "stateDirectory": str(canonical_state_dir),
        "issuedAtUtc": format_utc_z(issued_at),
        "expiresAtUtc": format_utc_z(expires_at),
        "snapshotId": snapshot_id,
        "proposalsDigest": f"sha256:{hashlib.sha256(proposal_bytes).hexdigest()}",
        "allowedActionIds": selected_action_ids,
        "allowedOperations": allowed_operations,
        "allowedTargets": [
            {"kind": "issue", "number": number} for number in allowed_issue_numbers
        ],
        "allowedChainRoots": chain_roots,
        "overrideSuppressionForActionIds": sorted(override_ids),
        "budget": {
            "maxMutationAttempts": len(selected_ids),
            "maxChains": len(chain_roots),
            "maxRunningCopilotTasks": max_running_copilot_tasks,
            "maxCopilotStartsPerRolling24h": (
                max_copilot_starts_per_rolling_24h
            ),
            "maxOpenDelegatedPullRequests": max_open_delegated_prs,
            "maxRepositoryRunningCopilotTasks": (
                max_repository_running_copilot_tasks
            ),
        },
        "productionCommentPilot": allow_production_comment_pilot,
        "productionDelegationPilot": allow_production_delegation_pilot,
        "productionDelegationSteadyState": (
            allow_production_delegation_steady_state
        ),
        "capacityPolicyDigest": (
            policy.digest
            if is_production and allow_production_delegation_steady_state
            else None
        ),
    }
    if comment_selection_digest is not None:
        grant[_COMMENT_SELECTION_DIGEST_KEY] = comment_selection_digest
    return grant


def _generate_grant_id() -> str:
    return f"grant:{secrets.token_hex(16)}"


def _production_freshness_deadline(
    snapshot_id: str,
    *,
    capability: object,
    repository: str,
    issued_at: datetime,
) -> datetime:
    prefix = f"snapshot:{repository}:"
    if (
        not isinstance(capability, Mapping)
        or set(capability) != {"schemaVersion", "evidenceRound"}
        or capability.get("schemaVersion") != 1
    ):
        raise AuthorizationError(
            "Production comment pilot grants require a finalized-cycle capability."
        )
    evidence_round = capability.get("evidenceRound")
    if (
        not isinstance(evidence_round, int)
        or isinstance(evidence_round, bool)
        or evidence_round not in {0, 1}
    ):
        raise AuthorizationError(
            "Production comment pilot capability must identify round 0 or 1."
        )
    if not snapshot_id.startswith(prefix):
        raise AuthorizationError(
            "Production comment pilot snapshot does not match its repository."
        )
    collected_at_text = snapshot_id[len(prefix) :]
    if evidence_round == 1:
        if not collected_at_text.endswith(":r1"):
            raise AuthorizationError(
                "Production comment pilot capability does not match its snapshot round."
            )
        collected_at_text = collected_at_text.removesuffix(":r1")
    elif ":r" in collected_at_text:
        raise AuthorizationError(
            "Production comment pilot capability does not match its snapshot round."
        )
    if not collected_at_text or ":r" in collected_at_text:
        raise AuthorizationError(
            "Production comment pilot snapshot time is invalid."
        )
    try:
        collected_at = datetime.fromisoformat(
            collected_at_text.replace("Z", "+00:00")
        )
    except ValueError as error:
        raise AuthorizationError(
            "Production comment pilot snapshot time is invalid."
        ) from error
    if collected_at.tzinfo is None:
        raise AuthorizationError(
            "Production comment pilot snapshot time must be timezone-aware."
        )
    collected_at = collected_at.astimezone(UTC)
    freshness_deadline = collected_at + MAX_PRODUCTION_SNAPSHOT_AGE
    if issued_at < collected_at:
        raise AuthorizationError(
            "Production comment pilot snapshot is not active yet."
        )
    if issued_at >= freshness_deadline:
        max_age_minutes = int(MAX_PRODUCTION_SNAPSHOT_AGE.total_seconds() // 60)
        raise AuthorizationError(
            "Production comment pilot snapshots must be less than "
            f"{max_age_minutes} minutes old."
        )
    return freshness_deadline


def _validate_production_comment_selection(
    proposals: Sequence[Mapping[str, Any]],
    *,
    ttl_minutes: int,
    override_ids: set[str],
) -> None:
    if not 1 <= len(proposals) <= MAX_PRODUCTION_COMMENT_ACTIONS:
        raise AuthorizationError(
            "Production comment pilot grants must authorize between one and "
            f"{MAX_PRODUCTION_COMMENT_ACTIONS} actions."
        )
    issue_numbers: set[int] = set()
    for proposal in proposals:
        operation = _require_string(proposal, "operation")
        if operation not in PRODUCTION_COMMENT_OPERATIONS:
            raise AuthorizationError(
                "Production comment pilot grants allow comment creation or editing only."
            )
        if proposal.get("dependsOn") is not None:
            raise AuthorizationError(
                "Production comment pilot actions must be independent."
            )
        issue_number = _require_positive_int(proposal, "issueNumber")
        if issue_number in issue_numbers:
            raise AuthorizationError(
                "Production comment pilot grants allow one action per issue."
            )
        issue_numbers.add(issue_number)
    if ttl_minutes > DEFAULT_GRANT_TTL_MINUTES:
        raise AuthorizationError(
            "Production comment pilot grants may live for at most 15 minutes."
        )
    if override_ids:
        raise AuthorizationError(
            "Production comment pilot grants cannot override suppression."
        )


def _validate_production_delegation_selection(
    proposals: Sequence[Mapping[str, Any]],
    *,
    ttl_minutes: int,
    override_ids: set[str],
    max_running_copilot_tasks: int,
    max_copilot_starts_per_rolling_24h: int,
    max_open_delegated_prs: int,
    max_repository_running_copilot_tasks: int,
) -> None:
    if len(proposals) != MAX_PRODUCTION_DELEGATION_ACTIONS:
        raise AuthorizationError(
            "Production delegation pilot grants must authorize exactly one action."
        )
    proposal = proposals[0]
    if _require_string(proposal, "operation") not in PRODUCTION_DELEGATION_OPERATIONS:
        raise AuthorizationError(
            "Production delegation pilot grants allow Copilot assignment only."
        )
    if proposal.get("dependsOn") is not None:
        raise AuthorizationError(
            "Production delegation pilot actions must be independent."
        )
    if ttl_minutes > DEFAULT_GRANT_TTL_MINUTES:
        raise AuthorizationError(
            "Production delegation pilot grants may live for at most 15 minutes."
        )
    if override_ids:
        raise AuthorizationError(
            "Production delegation pilot grants cannot override suppression."
        )
    if (
        max_running_copilot_tasks,
        max_copilot_starts_per_rolling_24h,
        max_open_delegated_prs,
    ) != (1, 1, 1):
        raise AuthorizationError(
            "Production delegation pilot capacity limits must all equal one."
        )
    if max_repository_running_copilot_tasks < 1:
        raise AuthorizationError(
            "Production delegation pilot repository-wide capacity ceiling "
            "must be positive."
        )


def _load_capacity_policy(path: Path) -> ProductionDelegationPolicy:
    try:
        return load_production_delegation_policy(path)
    except ValueError as exc:
        raise AuthorizationError(str(exc)) from exc


def _validate_production_delegation_steady_state_selection(
    proposals: Sequence[Mapping[str, Any]],
    *,
    ttl_minutes: int,
    override_ids: set[str],
    max_running_copilot_tasks: int,
    max_copilot_starts_per_rolling_24h: int,
    max_open_delegated_prs: int,
    max_repository_running_copilot_tasks: int,
    policy: ProductionDelegationPolicy,
) -> None:
    if not (1 <= len(proposals) <= policy.max_actions_per_grant):
        raise AuthorizationError(
            "Production delegation steady-state grants exceed the policy action limit."
        )
    if any(
        _require_string(proposal, "operation")
        not in PRODUCTION_DELEGATION_OPERATIONS
        for proposal in proposals
    ):
        raise AuthorizationError(
            "Production delegation steady-state grants allow Copilot assignment only."
        )
    if any(proposal.get("dependsOn") is not None for proposal in proposals):
        raise AuthorizationError(
            "Production delegation steady-state actions must be independent."
        )
    if ttl_minutes > DEFAULT_GRANT_TTL_MINUTES:
        raise AuthorizationError(
            "Production delegation steady-state grants may live for at most 15 minutes."
        )
    if override_ids:
        raise AuthorizationError(
            "Production delegation steady-state grants cannot override suppression."
        )
    requested = (
        max_running_copilot_tasks,
        max_copilot_starts_per_rolling_24h,
        max_open_delegated_prs,
        max_repository_running_copilot_tasks,
    )
    maxima = (
        policy.max_running_copilot_tasks,
        policy.max_copilot_starts_per_rolling_24h,
        policy.max_open_delegated_prs,
        policy.max_repository_running_copilot_tasks,
    )
    if any(value < 1 for value in requested) or any(
        value > maximum for value, maximum in zip(requested, maxima, strict=True)
    ):
        raise AuthorizationError(
            "Production delegation steady-state capacity exceeds its pinned policy."
        )


def _validate_production_delegation_steady_state_grant(
    grant: AuthorizationGrant,
    *,
    action_id: str,
    operation: str,
    proposals: Sequence[Mapping[str, Any]],
    capability: object,
    policy: ProductionDelegationPolicy,
) -> None:
    if not grant.production_delegation_steady_state:
        raise AuthorizationError(
            "Authorization grant does not carry the production steady-state capability."
        )
    if grant.capacity_policy_digest != policy.digest:
        raise AuthorizationError(
            "Production delegation capacity policy changed after grant creation."
        )
    selected = [
        proposal
        for proposal in proposals
        if isinstance(proposal, Mapping)
        and proposal.get("actionId") in grant.allowed_action_ids
    ]
    _validate_production_delegation_steady_state_selection(
        selected,
        ttl_minutes=int(
            (grant.expires_at - grant.issued_at).total_seconds() // 60
        ),
        override_ids=set(grant.override_suppression_for_action_ids),
        max_running_copilot_tasks=grant.budget.max_running_copilot_tasks,
        max_copilot_starts_per_rolling_24h=(
            grant.budget.max_copilot_starts_per_rolling_24h
        ),
        max_open_delegated_prs=grant.budget.max_open_delegated_prs,
        max_repository_running_copilot_tasks=(
            grant.budget.max_repository_running_copilot_tasks
        ),
        policy=policy,
    )
    if (
        operation not in PRODUCTION_DELEGATION_OPERATIONS
        or grant.allowed_operations != PRODUCTION_DELEGATION_OPERATIONS
    ):
        raise AuthorizationError(
            "Production delegation steady-state grant must allow assignment only."
        )
    if len(selected) != len(grant.allowed_action_ids):
        raise AuthorizationError(
            "Production delegation steady-state grant references an unknown action."
        )
    expected_targets = frozenset(
        ("issue", _require_positive_int(proposal, "issueNumber"))
        for proposal in selected
    )
    if grant.allowed_targets != expected_targets:
        raise AuthorizationError(
            "Production delegation steady-state grant targets do not match proposals."
        )
    if grant.allowed_chain_roots != grant.allowed_action_ids:
        raise AuthorizationError(
            "Production delegation steady-state actions must be independent roots."
        )
    action_count = len(grant.allowed_action_ids)
    if (
        grant.budget.max_mutation_attempts != action_count
        or grant.budget.max_chains != action_count
    ):
        raise AuthorizationError(
            "Production delegation steady-state budget must match its action count."
        )
    freshness_deadline = _production_freshness_deadline(
        grant.snapshot_id,
        capability=capability,
        repository=grant.repository,
        issued_at=grant.issued_at,
    )
    if grant.expires_at > freshness_deadline:
        raise AuthorizationError(
            "Production delegation steady-state grant outlives its source snapshot."
        )


def _validate_production_comment_grant(
    grant: AuthorizationGrant,
    *,
    action_id: str,
    operation: str,
    proposals: Sequence[Mapping[str, Any]],
    capability: object,
) -> None:
    if grant.repository.casefold() != PRODUCTION_REPOSITORY:
        raise AuthorizationError(
            "Production comment pilot grant repository must be microsoft/aspire."
        )
    if not grant.production_comment_pilot:
        raise AuthorizationError(
            "Authorization grant does not carry the production comment capability."
        )
    if not 1 <= len(grant.allowed_action_ids) <= MAX_PRODUCTION_COMMENT_ACTIONS:
        raise AuthorizationError(
            "Production comment pilot grant has an invalid action count."
        )
    freshness_deadline = _production_freshness_deadline(
        grant.snapshot_id,
        capability=capability,
        repository=grant.repository,
        issued_at=grant.issued_at,
    )
    if grant.expires_at > freshness_deadline:
        raise AuthorizationError(
            "Production comment pilot grant outlives its source snapshot."
        )
    by_action_id = {
        proposal.get("actionId"): proposal
        for proposal in proposals
        if isinstance(proposal, Mapping)
    }
    selected = [
        by_action_id.get(allowed_action_id)
        for allowed_action_id in grant.allowed_action_ids
    ]
    if any(proposal is None for proposal in selected):
        raise AuthorizationError(
            "Production comment pilot grant references an unknown action."
        )
    selected_proposals = [
        proposal for proposal in selected if proposal is not None
    ]
    selected_operations = frozenset(
        _require_string(proposal, "operation")
        for proposal in selected_proposals
    )
    if (
        operation not in PRODUCTION_COMMENT_OPERATIONS
        or not selected_operations.issubset(PRODUCTION_COMMENT_OPERATIONS)
        or grant.allowed_operations != selected_operations
    ):
        raise AuthorizationError(
            "Production comment pilot grant must allow comment creation or editing only."
        )
    if any(proposal.get("dependsOn") is not None for proposal in selected_proposals):
        raise AuthorizationError(
            "Production comment pilot grant actions must be independent."
        )
    selected_targets = frozenset(
        ("issue", _require_positive_int(proposal, "issueNumber"))
        for proposal in selected_proposals
    )
    if (
        len(selected_targets) != len(selected_proposals)
        or grant.allowed_targets != selected_targets
    ):
        raise AuthorizationError(
            "Production comment pilot grant must bind one issue target per action."
        )
    if grant.allowed_chain_roots != grant.allowed_action_ids:
        raise AuthorizationError(
            "Production comment pilot grant must bind every action as an independent root."
        )
    if grant.override_suppression_for_action_ids:
        raise AuthorizationError(
            "Production comment pilot grant cannot override suppression."
        )
    action_count = len(grant.allowed_action_ids)
    if grant.budget != AuthorizationBudget(
        max_mutation_attempts=action_count,
        max_chains=action_count,
    ):
        raise AuthorizationError(
            "Production comment pilot grant budget must match its exact action count."
        )
    if (
        grant.expires_at - grant.issued_at
        > timedelta(minutes=DEFAULT_GRANT_TTL_MINUTES)
    ):
        raise AuthorizationError(
            "Production comment pilot grant lifetime must not exceed 15 minutes."
        )


def _validate_production_delegation_grant(
    grant: AuthorizationGrant,
    *,
    action_id: str,
    operation: str,
    proposals: Sequence[Mapping[str, Any]],
    capability: object,
) -> None:
    if grant.repository.casefold() != PRODUCTION_REPOSITORY:
        raise AuthorizationError(
            "Production delegation pilot grant repository must be microsoft/aspire."
        )
    if not grant.production_delegation_pilot:
        raise AuthorizationError(
            "Authorization grant does not carry the production delegation capability."
        )
    if len(grant.allowed_action_ids) != MAX_PRODUCTION_DELEGATION_ACTIONS:
        raise AuthorizationError(
            "Production delegation pilot grant has an invalid action count."
        )
    freshness_deadline = _production_freshness_deadline(
        grant.snapshot_id,
        capability=capability,
        repository=grant.repository,
        issued_at=grant.issued_at,
    )
    if grant.expires_at > freshness_deadline:
        raise AuthorizationError(
            "Production delegation pilot grant outlives its source snapshot."
        )
    by_action_id = {
        proposal.get("actionId"): proposal
        for proposal in proposals
        if isinstance(proposal, Mapping)
    }
    selected = [
        by_action_id.get(allowed_action_id)
        for allowed_action_id in grant.allowed_action_ids
    ]
    if any(proposal is None for proposal in selected):
        raise AuthorizationError(
            "Production delegation pilot grant references an unknown action."
        )
    selected_proposals = [
        proposal for proposal in selected if proposal is not None
    ]
    selected_operations = frozenset(
        _require_string(proposal, "operation")
        for proposal in selected_proposals
    )
    if (
        operation not in PRODUCTION_DELEGATION_OPERATIONS
        or not selected_operations.issubset(PRODUCTION_DELEGATION_OPERATIONS)
        or grant.allowed_operations != selected_operations
    ):
        raise AuthorizationError(
            "Production delegation pilot grant must allow Copilot assignment only."
        )
    if any(proposal.get("dependsOn") is not None for proposal in selected_proposals):
        raise AuthorizationError(
            "Production delegation pilot grant actions must be independent."
        )
    selected_targets = frozenset(
        ("issue", _require_positive_int(proposal, "issueNumber"))
        for proposal in selected_proposals
    )
    if (
        len(selected_targets) != len(selected_proposals)
        or grant.allowed_targets != selected_targets
    ):
        raise AuthorizationError(
            "Production delegation pilot grant must bind one issue target."
        )
    if grant.allowed_chain_roots != grant.allowed_action_ids:
        raise AuthorizationError(
            "Production delegation pilot grant must bind its action as the root."
        )
    if grant.override_suppression_for_action_ids:
        raise AuthorizationError(
            "Production delegation pilot grant cannot override suppression."
        )
    if (
        grant.budget.max_mutation_attempts,
        grant.budget.max_chains,
        grant.budget.max_running_copilot_tasks,
        grant.budget.max_copilot_starts_per_rolling_24h,
        grant.budget.max_open_delegated_prs,
    ) != (1, 1, 1, 1, 1):
        raise AuthorizationError(
            "Production delegation pilot budget must bind one assignment and "
            "1/1/1 capacity."
        )
    if grant.budget.max_repository_running_copilot_tasks < 1:
        raise AuthorizationError(
            "Production delegation pilot repository-wide capacity ceiling "
            "must be positive."
        )
    if (
        grant.expires_at - grant.issued_at
        > timedelta(minutes=DEFAULT_GRANT_TTL_MINUTES)
    ):
        raise AuthorizationError(
            "Production delegation pilot grant lifetime must not exceed 15 minutes."
        )


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
    document_keys = set(document)
    supported_key_sets = {
        _GRANT_KEYS,
        _GRANT_KEYS | {_PRODUCTION_DELEGATION_GRANT_KEY},
        _GRANT_KEYS | _CURRENT_CAPABILITY_KEYS,
    }
    supported_key_sets |= {
        keys | {_COMMENT_SELECTION_DIGEST_KEY} for keys in supported_key_sets
    }
    if document_keys not in supported_key_sets:
        raise AuthorizationError(
            "Authorization grant must contain exactly the supported fields."
        )
    if document.get("schemaVersion") != AUTHORIZATION_SCHEMA_VERSION:
        raise AuthorizationError(
            f"Authorization grant schemaVersion must equal "
            f"{AUTHORIZATION_SCHEMA_VERSION}."
        )

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
        "assign-copilot",
        "create-comment",
        "edit-comment",
        "close-issue",
        "unassign-copilot",
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
        max_running_copilot_tasks=_require_nonnegative_int(
            budget_document,
            "maxRunningCopilotTasks",
        ),
        max_copilot_starts_per_rolling_24h=_require_nonnegative_int(
            budget_document,
            "maxCopilotStartsPerRolling24h",
        ),
        max_open_delegated_prs=_require_nonnegative_int(
            budget_document,
            "maxOpenDelegatedPullRequests",
        ),
        max_repository_running_copilot_tasks=_require_nonnegative_int(
            budget_document,
            "maxRepositoryRunningCopilotTasks",
        ),
    )

    grant = AuthorizationGrant(
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
        production_comment_pilot=_require_bool(
            document,
            "productionCommentPilot",
        ),
        production_delegation_pilot=(
            _require_bool(document, _PRODUCTION_DELEGATION_GRANT_KEY)
            if _PRODUCTION_DELEGATION_GRANT_KEY in document
            else False
        ),
        production_delegation_steady_state=(
            _require_bool(document, _PRODUCTION_DELEGATION_STEADY_STATE_KEY)
            if _PRODUCTION_DELEGATION_STEADY_STATE_KEY in document
            else False
        ),
        capacity_policy_digest=(
            _require_optional_digest(document, _CAPACITY_POLICY_DIGEST_KEY)
            if _CAPACITY_POLICY_DIGEST_KEY in document
            else None
        ),
        comment_selection_digest=(
            _require_optional_digest(document, _COMMENT_SELECTION_DIGEST_KEY)
            if _COMMENT_SELECTION_DIGEST_KEY in document
            else None
        ),
    )
    if sum(
        (
            grant.production_comment_pilot,
            grant.production_delegation_pilot,
            grant.production_delegation_steady_state,
        )
    ) > 1:
        raise AuthorizationError(
            "Authorization grant production capabilities are mutually exclusive."
        )
    if (
        grant.production_delegation_steady_state
        != (grant.capacity_policy_digest is not None)
    ):
        raise AuthorizationError(
            "Authorization grant capacityPolicyDigest must exactly match "
            "the steady-state capability."
        )
    return grant


def _require_string(document: Mapping[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise AuthorizationError(f"{key} must be a non-empty string.")
    return value


def _require_bool(document: Mapping[str, Any], key: str) -> bool:
    value = document.get(key)
    if not isinstance(value, bool):
        raise AuthorizationError(f"{key} must be a boolean.")
    return value


def _require_optional_digest(
    document: Mapping[str, Any],
    key: str,
) -> str | None:
    value = document.get(key)
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
    ):
        raise AuthorizationError(f"{key} must be null or a SHA-256 digest.")
    return value


def _require_nonnegative_int(document: Mapping[str, Any], key: str) -> int:
    value = document.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AuthorizationError(f"{key} must be a nonnegative integer.")
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
