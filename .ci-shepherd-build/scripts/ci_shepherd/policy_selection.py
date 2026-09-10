"""Policy-aware selection of proposed CI Shepherd actions.

This module replaces "rank the top N proposals" comment selection with a
selection pass that is aware of the operation policy, exact per-action
decisions, execution eligibility, and durable per-run/rolling budgets. It is
an authorization-boundary component: every validation failure below fails
closed (raises) rather than silently dropping or defaulting a proposal into
an executable state.

Selection precedence, per candidate, is (highest wins first):

1. ``ineligible``            -- execution eligibility says no. Wins over
                                 everything, including an exact approval.
2. ``outside-policy-surface``-- the operation has no policy class mapping.
3. ``denied`` (policy)       -- ``deniedActionIds``/``deniedTargets`` on the
                                 active policy. Absolute and wins over
                                 approve-once *while that policy revision is
                                 active*; a paused/revoked/expired policy's
                                 deny lists no longer apply (falls through to
                                 ordinary no-active-policy handling below).
4. ``denied`` (exact reject) -- an applicable ``reject-once`` decision.
                                 Absolute; wins over approve-once.
5. ``exhausted`` (prerequisite) -- a ``dependsOn`` action has not yet been
                                 durably completed.
6. ``superseded``            -- a higher-priority comment already claimed the
                                 same issue (comment operations only).
7. ``denied``/``exhausted`` (class) -- the operation class is disabled, the
                                 policy is not currently active, or its
                                 per-run/rolling/hard-ceiling budget is spent.
                                 An ``approve-once`` decision can promote this
                                 to ``exact``, but never past the hard
                                 ceilings.
8. ``automatic``/``exact``   -- admitted, in deterministic rank order.

See docs/superpowers/plans/2026-09-03-ci-shepherd-autonomous-policy.md
(Task 3) for the full specification.
"""

from __future__ import annotations

from .eligibility import repair_priority_key

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
import hashlib
import re

from .actor import validate_action_proposals
from .comment_selection import COMMENT_OPERATIONS, operation_priority, priority_for_action_id
from .models import stable_json
from .managed_coverage import coverage_exclusions
from .operation_policy import (
    HARD_MAX_PER_RUN,
    HARD_MAX_ROLLING_24H,
    OPERATION_CLASSES,
    OperationPolicyError,
    OperationPolicyRevision,
    classify_operation,
    load_operation_policy_document,
)
from .timeutils import format_utc_z, parse_aware_iso8601


class PolicySelectionError(ValueError):
    """Raised when inputs to policy-aware selection are malformed.

    Every raise site in this module is an authorization-boundary fail-closed
    check: callers must treat any ``PolicySelectionError`` as "select
    nothing", never as "select everything".
    """


__all__ = [
    "PolicySelectionError",
    "build_policy_selection",
    "render_policy_selection_section",
]


# Event types recognized on the execution ledger. An unrecognized eventType is
# refused rather than silently ignored: a future mutation-record kind that we
# do not understand could otherwise be undercounted, which would silently
# widen the effective budget available to an attacker or a bug.
_KNOWN_ACTION_EVENT_TYPES = frozenset(
    {"intent", "terminal", "delegation-baseline", "delegation-observed", "delegation-retired"}
)

# Mirrors execution_state.py's private _TERMINAL_OUTCOMES. Duplicated (not
# imported) because it is a private module constant there; the two must be
# kept in sync if execution_state.py's outcome vocabulary ever changes.
_TERMINAL_OUTCOMES = frozenset({"executed", "skipped", "stale", "failed", "indeterminate"})

_DECISION_VALUES = frozenset({"approve-once", "reject-once"})
_POLICY_PROJECTION_FIELDS = frozenset({"stateRevision", "effectivePolicy", "exactDecisions"})
_EXACT_DECISION_FIELDS = frozenset(
    {"actionId", "proposalDigest", "decision", "actor", "expiresAtUtc", "eventRevision"}
)

# Reasons a candidate's automatic-scan denial/exhaustion can still be rescued
# by an applicable approve-once exact decision. This intentionally excludes
# every "hard" or "structural" reason (policy denies, exact rejection,
# ineligibility, unsupported operations, unmet prerequisites, and same-issue
# suppression): none of those are the "operation class isn't currently
# licensed" condition that approve-once exists to bypass.
_EXACT_OVERRIDABLE_REASONS = frozenset(
    {
        "no-active-policy",
        "operation-disabled",
        "per-run-cap-exhausted",
        "rolling-24h-cap-exhausted",
        "operator-request-requires-exact-approval",
    }
)

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ROLLING_WINDOW = timedelta(hours=24)


def build_policy_selection(
    proposals_document: object,
    *,
    run_id: str,
    policy_projection: Mapping[str, object],
    action_events: Sequence[Mapping[str, object]],
    now: datetime,
) -> dict[str, object]:
    """Select the actions this run may execute automatically or exactly.

    ``proposals_document`` is validated with
    :func:`ci_shepherd.actor.validate_action_proposals` before anything else
    runs. ``policy_projection`` must be the exact shape produced by
    :meth:`ci_shepherd.coordinator_state.CoordinatorStateStore.projection`.
    ``action_events`` is the raw execution ledger (intent/terminal events, in
    any order); only entries matching this proposals document's repository
    are counted.
    """

    if not isinstance(run_id, str) or not run_id:
        raise PolicySelectionError("run_id must be a nonempty string.")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise PolicySelectionError("now must be a timezone-aware datetime.")
    now = now.astimezone(UTC)

    validated = validate_action_proposals(proposals_document)
    repository = str(validated["repository"])
    snapshot_id = str(validated["snapshotId"])
    proposals: list[Mapping[str, object]] = list(validated["proposals"])
    proposal_by_action_id: dict[str, Mapping[str, object]] = {}
    for proposal in proposals:
        action_id = str(proposal["actionId"])
        if action_id in proposal_by_action_id:
            # validate_action_proposals already rejects duplicate actionIds,
            # but re-check here so this module never silently trusts a
            # future relaxation of that guarantee.
            raise PolicySelectionError(f"Duplicate proposal actionId {action_id!r}.")
        proposal_by_action_id[action_id] = proposal

    proposals_digest = (
        "sha256:" + hashlib.sha256(stable_json(proposals_document).encode("utf-8")).hexdigest()
    )

    state_revision, policy, policy_digest, exact_decisions = _validate_policy_projection(
        policy_projection, now=now
    )
    policy_active = policy is not None and policy.active_at(now)
    exact_by_action_id = _index_exact_decisions(exact_decisions, proposals_digest=proposals_digest)

    normalized_events = _validate_action_events(action_events, repository=repository)
    this_run_used, rolling_used, overall_this_run_used, overall_rolling_used = _compute_usage(
        normalized_events, run_id=run_id, now=now
    )
    budgets = _build_budgets(policy, policy_active, this_run_used, rolling_used)

    policy_revision_id = policy.revision_id if policy is not None else None

    base: list[dict[str, object]] = []
    global_stop, blocked_ids = coverage_exclusions(
        validated.get("productionPilotCapability", {}).get("managedItemCoverage"), proposals,
    )
    for proposal in proposals:
        base.append(
            _classify_initial(
                proposal,
                policy=policy,
                policy_active=policy_active,
                policy_revision_id=policy_revision_id,
                exact_by_action_id=exact_by_action_id,
                proposal_by_action_id=proposal_by_action_id,
                normalized_events=normalized_events,
                repository=repository,
                snapshot_id=snapshot_id,
            )
        )
        if proposal["actionId"] in blocked_ids:
            base[-1].update(status="ineligible", reason="managed-item-coverage-invalid")

    pending = [record for record in base if record["status"] is None]
    pending.sort(
        key=lambda record: (
            *_repair_order(proposal_by_action_id[str(record["actionId"])]),
            priority_for_action_id(str(record["actionId"]))[0],
            operation_priority(str(record["operation"])),
            int(record["issueNumber"]),
            str(record["actionId"]),
        )
    )

    pending_after_suppression: list[dict[str, object]] = []
    seen_issue_numbers: set[int] = set()
    for record in pending:
        if record["operation"] in COMMENT_OPERATIONS:
            issue_number = int(record["issueNumber"])
            if issue_number in seen_issue_numbers:
                record["status"] = "superseded"
                record["reason"] = "lower-priority-comment-for-same-issue"
                continue
            seen_issue_numbers.add(issue_number)
        pending_after_suppression.append(record)

    remaining_this_run = {cls: int(budgets[cls]["remainingThisRun"]) for cls in OPERATION_CLASSES}
    remaining_rolling = {
        cls: int(budgets[cls]["remainingRolling24h"]) for cls in OPERATION_CLASSES
    }
    overall_remaining_this_run = max(0, HARD_MAX_PER_RUN - overall_this_run_used)
    overall_remaining_rolling = max(0, HARD_MAX_ROLLING_24H - overall_rolling_used)

    automatic_ids: list[str] = []
    automatic_rank = 0
    for record in pending_after_suppression:
        op_class = record["operationClass"]
        assert isinstance(op_class, str)  # guaranteed by _classify_initial
        if proposal_by_action_id[str(record["actionId"])].get("evidenceBasis") == "operator-request":
            record["status"] = "denied"
            record["reason"] = "operator-request-requires-exact-approval"
            continue
        if policy is None or not policy_active:
            record["status"] = "denied"
            record["reason"] = "no-active-policy"
            continue
        if not policy.operation_classes[op_class].enabled:
            record["status"] = "denied"
            record["reason"] = "operation-disabled"
            continue
        if remaining_this_run[op_class] <= 0:
            record["status"] = "exhausted"
            record["reason"] = "per-run-cap-exhausted"
            continue
        if remaining_rolling[op_class] <= 0:
            record["status"] = "exhausted"
            record["reason"] = "rolling-24h-cap-exhausted"
            continue
        if overall_remaining_this_run <= 0:
            record["status"] = "exhausted"
            record["reason"] = "repository-hard-ceiling-run-exhausted"
            continue
        if overall_remaining_rolling <= 0:
            record["status"] = "exhausted"
            record["reason"] = "repository-hard-ceiling-rolling-exhausted"
            continue

        automatic_rank += 1
        record["status"] = "automatic"
        record["reason"] = "policy-permits-class"
        # revision_id is already formatted as "policy:<n>" (e.g. "policy:4"),
        # so licenseSource IS the revision id, not a further-wrapped string.
        record["licenseSource"] = policy.revision_id
        record["automaticRank"] = automatic_rank
        remaining_this_run[op_class] -= 1
        remaining_rolling[op_class] -= 1
        overall_remaining_this_run -= 1
        overall_remaining_rolling -= 1
        automatic_ids.append(str(record["actionId"]))

    exact_ids: list[str] = []
    exact_rank = 0
    for record in pending_after_suppression:
        if record["status"] == "automatic":
            continue
        if record["reason"] not in _EXACT_OVERRIDABLE_REASONS:
            continue
        exact_entry = exact_by_action_id.get(str(record["actionId"]))
        if exact_entry is None or exact_entry["decision"] != "approve-once":
            continue
        if overall_remaining_this_run <= 0 or overall_remaining_rolling <= 0:
            # Exact approval never increases exposure beyond the repository
            # hard ceilings; leave the candidate's prior denial/exhaustion.
            continue

        exact_rank += 1
        record["status"] = "exact"
        record["reason"] = "exact-approval"
        record["licenseSource"] = f"decision:{exact_entry['eventRevision']}"
        record["exactDecision"] = {
            "eventRevision": exact_entry["eventRevision"],
            "proposalDigest": exact_entry["proposalDigest"],
            "decision": exact_entry["decision"],
            "actor": exact_entry["actor"],
        }
        record["exactRank"] = exact_rank
        overall_remaining_this_run -= 1
        overall_remaining_rolling -= 1
        exact_ids.append(str(record["actionId"]))

    sum_remaining_this_run = sum(int(budgets[cls]["remainingThisRun"]) for cls in OPERATION_CLASSES)
    sum_remaining_rolling = sum(
        int(budgets[cls]["remainingRolling24h"]) for cls in OPERATION_CLASSES
    )
    maximum_write_exposure = {
        "thisRun": min(sum_remaining_this_run, max(0, HARD_MAX_PER_RUN - overall_this_run_used)),
        "rolling24h": min(
            sum_remaining_rolling, max(0, HARD_MAX_ROLLING_24H - overall_rolling_used)
        ),
    }
    if global_stop:
        maximum_write_exposure = {"thisRun": 0, "rolling24h": 0}

    for record in base:
        if record["status"] is None:
            # Every proposal must resolve to exactly one typed status; a
            # None here means a control-flow path above failed to assign
            # one, which is a bug in this module, not a normal outcome.
            raise PolicySelectionError(
                f"Internal error: {record['actionId']!r} has no resolved status."
            )

    return {
        "schemaVersion": 1,
        "repository": repository,
        "snapshotId": snapshot_id,
        "runId": run_id,
        "proposalsDigest": proposals_digest,
        "coordinatorStateRevision": state_revision,
        "policyRevisionId": policy_revision_id,
        "policyDigest": policy_digest,
        "generatedAtUtc": format_utc_z(now),
        "automaticActionIds": automatic_ids,
        "exactActionIds": exact_ids,
        "selectedActionIds": automatic_ids + exact_ids,
        "candidates": base,
        "budgets": budgets,
        "maximumWriteExposure": maximum_write_exposure,
        **({"mutationBlocked": True} if global_stop else {}),
    }


def _repair_order(proposal: Mapping[str, object]) -> tuple[object, ...]:
    facts = proposal.get("repairPriorityFacts")
    rank, not_recurrent, last_failure, _ = repair_priority_key({
        **(facts if isinstance(facts, Mapping) else {}), "issueNumber": proposal["issueNumber"],
    })
    # Routine status upkeep must not take the next mutation ahead of a repair
    # start. Human decision and prerequisite explanations keep the subject rank.
    suffix = str(proposal["actionId"]).rsplit(":", 1)[-1]
    if proposal["operation"] in COMMENT_OPERATIONS and suffix in {
        "watch-comment", "retire-status-comment", "quarantine-blocked-comment", "quarantine-reconciliation-comment",
    }:
        rank = 6
    return rank, not_recurrent, last_failure


def render_policy_selection_section(
    selection: Mapping[str, object],
    *,
    heading: str = "Policy-aware action selection",
    introduction: str | None = None,
) -> str:
    """Render a deterministic markdown summary of a policy selection."""

    budgets = selection["budgets"]
    exposure = selection["maximumWriteExposure"]
    lines = [
        f"## {heading}",
        "",
    ]
    if introduction is not None:
        lines.extend([introduction, ""])
    lines.extend(
        [
            (
                f"Automatic: **{len(selection['automaticActionIds'])}** &middot; "
                f"Exact: **{len(selection['exactActionIds'])}** &middot; "
                f"Selected: **{len(selection['selectedActionIds'])}** of "
                f"**{len(selection['candidates'])}** proposed actions."
            ),
            "",
            (
                f"Policy revision: `{selection['policyRevisionId'] or 'none'}` &middot; "
                f"Coordinator state revision: {selection['coordinatorStateRevision']}."
            ),
            "",
            (
                "Maximum write exposure this run: "
                f"**{exposure['thisRun']}**, rolling 24h: **{exposure['rolling24h']}**."
            ),
            "",
            "| Class | Enabled | Used/Max (run) | Used/Max (24h) |",
            "|---|---|---:|---:|",
        ]
    )
    for cls in OPERATION_CLASSES:
        budget = budgets[cls]
        lines.append(
            "| `{cls}` | {enabled} | {used_run}/{max_run} | {used_roll}/{max_roll} |".format(
                cls=cls,
                enabled="yes" if budget["enabled"] else "no",
                used_run=budget["usedThisRun"],
                max_run=budget["maxPerRun"],
                used_roll=budget["usedRolling24h"],
                max_roll=budget["maxRolling24h"],
            )
        )

    lines += [
        "",
        "| Issue | Operation | Status | Reason | Rank |",
        "|---:|---|---|---|---:|",
    ]
    for candidate in selection["candidates"]:
        rank = candidate["automaticRank"] if candidate["automaticRank"] is not None else candidate["exactRank"]
        lines.append(
            "| #{issue} | `{operation}` | {status} | {reason} | {rank} |".format(
                issue=candidate["issueNumber"],
                operation=candidate["operation"],
                status=candidate["status"],
                reason=candidate["reason"],
                rank=rank if rank is not None else "-",
            )
        )
    if not selection["candidates"]:
        lines.append("| - | - | No candidates | - | - |")

    return "\n".join(lines) + "\n"


def _classify_initial(
    proposal: Mapping[str, object],
    *,
    policy: OperationPolicyRevision | None,
    policy_active: bool,
    policy_revision_id: str | None,
    exact_by_action_id: Mapping[str, dict[str, object]],
    proposal_by_action_id: Mapping[str, Mapping[str, object]],
    normalized_events: list[dict[str, object]],
    repository: str,
    snapshot_id: str,
) -> dict[str, object]:
    """Resolve everything about a candidate that does not depend on peers.

    Returns a record with ``status`` still ``None`` when the candidate is
    still pending same-issue suppression and the deterministic cap-consuming
    scan; otherwise the record's status/reason are already final.
    """

    action_id = str(proposal["actionId"])
    issue_number = int(proposal["issueNumber"])  # type: ignore[arg-type]
    operation = str(proposal["operation"])
    depends_on = proposal.get("dependsOn")
    op_class = classify_operation(operation)
    eligibility = proposal.get("executionEligibility")
    eligible = isinstance(eligibility, Mapping) and eligibility.get("eligible") is True
    blocking_reasons = (
        [str(reason) for reason in eligibility.get("blockingReasons", [])]
        if isinstance(eligibility, Mapping) and isinstance(eligibility.get("blockingReasons"), list)
        else []
    )

    record: dict[str, object] = {
        "actionId": action_id,
        "issueNumber": issue_number,
        "operation": operation,
        "operationClass": op_class,
        "status": None,
        "reason": None,
        "automaticRank": None,
        "exactRank": None,
        "licenseSource": None,
        "policyRevisionId": policy_revision_id,
        "exactDecision": None,
        "blockingReasons": blocking_reasons,
        "dependsOn": depends_on,
        "satisfiedPrerequisites": None,
    }

    if not eligible:
        record["status"] = "ineligible"
        record["reason"] = "not-execution-eligible"
        return record

    if op_class is None:
        record["status"] = "outside-policy-surface"
        record["reason"] = "unsupported-operation"
        return record

    target_kind, target_number = _proposal_target(proposal)
    target_key = f"{target_kind}:{target_number}"
    body_digest = _body_digest(proposal.get("body"))
    if any(
        event["eventType"] == "terminal"
        and event["actionId"] == action_id
        and event["snapshotId"] == snapshot_id
        and event["operation"] == operation
        and event["targetKind"] == target_kind
        and event["targetNumber"] == target_number
        and event["idempotencyKey"] == proposal["idempotencyKey"]
        and event["bodyDigest"] == body_digest
        and event["outcome"] != "indeterminate"
        for event in normalized_events
    ):
        record["status"] = "exhausted"
        record["reason"] = "already-terminal"
        return record

    # Deny lists are part of the standing policy envelope, not a separate
    # exact decision: they only bind while that policy revision is active.
    # A paused/revoked/expired policy withdraws its automatic grants *and*
    # its denies together, leaving the candidate to ordinary
    # no-active-policy handling (still overridable only by an otherwise
    # valid approve-once, never bypassing eligibility/surface/hard ceilings).
    if policy is not None and policy_active and action_id in policy.denied_action_ids:
        record["status"] = "denied"
        record["reason"] = "policy-denied-action-id"
        return record
    if policy is not None and policy_active and target_key in policy.denied_targets:
        record["status"] = "denied"
        record["reason"] = "policy-denied-target"
        return record

    exact_entry = exact_by_action_id.get(action_id)
    if exact_entry is not None and exact_entry["decision"] == "reject-once":
        record["status"] = "denied"
        record["reason"] = "exact-rejected"
        record["exactDecision"] = {
            "eventRevision": exact_entry["eventRevision"],
            "proposalDigest": exact_entry["proposalDigest"],
            "decision": exact_entry["decision"],
            "actor": exact_entry["actor"],
        }
        return record

    if depends_on is not None:
        satisfied = _resolve_prerequisite(
            str(depends_on),
            proposal_by_action_id=proposal_by_action_id,
            normalized_events=normalized_events,
            repository=repository,
            snapshot_id=snapshot_id,
        )
        if satisfied is None:
            record["status"] = "exhausted"
            record["reason"] = "prerequisite-not-terminal"
            return record
        record["satisfiedPrerequisites"] = [satisfied]

    return record


def _proposal_target(proposal: Mapping[str, object]) -> tuple[str, int]:
    """Extract the ``(kind, number)`` target identity from a proposal.

    Proposals validated by :func:`actor.validate_action_proposals` use
    either the legacy ``issueNumber`` addressing style or the explicit
    ``targetKind``/``targetNumber`` style; both are already validated by that
    function, so this only re-extracts the values.
    """

    if "targetKind" in proposal:
        kind = proposal.get("targetKind")
        number = proposal.get("targetNumber")
    else:
        kind = "issue"
        number = proposal.get("issueNumber")
    if not isinstance(kind, str) or not kind:
        raise PolicySelectionError("Proposal target kind must be a nonempty string.")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise PolicySelectionError("Proposal target number must be a positive integer.")
    return kind, number


def _body_digest(body: object) -> str | None:
    if not isinstance(body, str):
        return None
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _resolve_prerequisite(
    depends_on: str,
    *,
    proposal_by_action_id: Mapping[str, Mapping[str, object]],
    normalized_events: list[dict[str, object]],
    repository: str,
    snapshot_id: str,
) -> dict[str, object] | None:
    """Look for a durable terminal ``executed`` event for ``depends_on``.

    Returns a ``satisfiedPrerequisites`` entry (actionId + event digest) when
    an exact match is found, or ``None`` when the dependency has not yet
    completed. Never grants or selects the dependency itself.
    """

    dependency_proposal = proposal_by_action_id.get(depends_on)
    if dependency_proposal is None:
        # validate_action_proposals guarantees dependsOn resolves to exactly
        # one proposal in this same document; this should be unreachable.
        raise PolicySelectionError(f"dependsOn {depends_on!r} does not resolve to a proposal.")

    dep_kind, dep_number = _proposal_target(dependency_proposal)
    dep_operation = str(dependency_proposal["operation"])
    dep_idempotency_key = str(dependency_proposal["idempotencyKey"])
    dep_body_digest = _body_digest(dependency_proposal.get("body"))

    matches = [
        event
        for event in normalized_events
        if event["eventType"] == "terminal"
        and event["outcome"] == "executed"
        and event["actionId"] == depends_on
        and event["snapshotId"] == snapshot_id
        and event["operation"] == dep_operation
        and event["targetKind"] == dep_kind
        and event["targetNumber"] == dep_number
        and event["idempotencyKey"] == dep_idempotency_key
        and event["bodyDigest"] == dep_body_digest
    ]
    if not matches:
        return None

    # Ignore/reject stale or conflicting terminal records deterministically:
    # among exact-identity matches, the most recently recorded one wins.
    terminal_event = max(matches, key=lambda event: event["recordedAt"])["raw"]
    event_digest = "sha256:" + hashlib.sha256(stable_json(terminal_event).encode("utf-8")).hexdigest()
    return {"actionId": depends_on, "eventDigest": event_digest}


def _validate_policy_projection(
    policy_projection: object,
    *,
    now: datetime,
) -> tuple[int, OperationPolicyRevision | None, str | None, list[dict[str, object]]]:
    if not isinstance(policy_projection, Mapping):
        raise PolicySelectionError("policy_projection must be an object.")
    if set(policy_projection) != _POLICY_PROJECTION_FIELDS:
        raise PolicySelectionError(
            "policy_projection must contain exactly stateRevision, "
            "effectivePolicy, and exactDecisions."
        )

    state_revision = policy_projection["stateRevision"]
    if (
        not isinstance(state_revision, int)
        or isinstance(state_revision, bool)
        or state_revision < 0
    ):
        raise PolicySelectionError("policy_projection.stateRevision must be a non-negative integer.")

    effective_policy_raw = policy_projection["effectivePolicy"]
    policy: OperationPolicyRevision | None = None
    policy_digest: str | None = None
    if effective_policy_raw is not None:
        if not isinstance(effective_policy_raw, Mapping):
            raise PolicySelectionError("policy_projection.effectivePolicy must be an object or null.")
        policy_digest = effective_policy_raw.get("policyDigest")
        if not isinstance(policy_digest, str) or _DIGEST_RE.fullmatch(policy_digest) is None:
            raise PolicySelectionError("policy_projection.effectivePolicy.policyDigest is invalid.")
        stripped = {key: value for key, value in effective_policy_raw.items() if key != "policyDigest"}
        try:
            policy = load_operation_policy_document(stripped)
        except OperationPolicyError as exc:
            raise PolicySelectionError(f"policy_projection.effectivePolicy is invalid: {exc}") from exc
        if policy.digest != policy_digest:
            raise PolicySelectionError(
                "policy_projection.effectivePolicy.policyDigest does not match its policy document."
            )

    exact_decisions_raw = policy_projection["exactDecisions"]
    if not isinstance(exact_decisions_raw, list):
        raise PolicySelectionError("policy_projection.exactDecisions must be a list.")

    exact_decisions: list[dict[str, object]] = []
    seen_keys: set[tuple[str, str]] = set()
    for entry in exact_decisions_raw:
        if not isinstance(entry, Mapping) or set(entry) != _EXACT_DECISION_FIELDS:
            raise PolicySelectionError(
                "Each policy_projection.exactDecisions entry must contain exactly "
                "actionId, proposalDigest, decision, actor, expiresAtUtc, and eventRevision."
            )
        action_id = entry["actionId"]
        proposal_digest = entry["proposalDigest"]
        decision = entry["decision"]
        actor = entry["actor"]
        expires_at_raw = entry["expiresAtUtc"]
        event_revision = entry["eventRevision"]
        if not isinstance(action_id, str) or not action_id:
            raise PolicySelectionError("exactDecisions[].actionId must be a nonempty string.")
        if not isinstance(proposal_digest, str) or _DIGEST_RE.fullmatch(proposal_digest) is None:
            raise PolicySelectionError("exactDecisions[].proposalDigest must be a sha256 digest.")
        if decision not in _DECISION_VALUES:
            raise PolicySelectionError(
                "exactDecisions[].decision must be approve-once or reject-once."
            )
        if not isinstance(actor, str) or not actor:
            raise PolicySelectionError("exactDecisions[].actor must be a nonempty string.")
        if (
            not isinstance(event_revision, int)
            or isinstance(event_revision, bool)
            or event_revision <= 0
        ):
            raise PolicySelectionError("exactDecisions[].eventRevision must be a positive integer.")
        try:
            expires_at = parse_aware_iso8601(expires_at_raw, "exactDecisions[].expiresAtUtc")
        except ValueError as exc:
            raise PolicySelectionError(str(exc)) from exc

        key = (action_id, proposal_digest)
        if key in seen_keys:
            raise PolicySelectionError(
                f"Duplicate exact decision for actionId {action_id!r} against the same "
                "proposal digest."
            )
        seen_keys.add(key)

        if now >= expires_at:
            continue  # Defense-in-depth: an expired decision is never effective.
        exact_decisions.append(
            {
                "actionId": action_id,
                "proposalDigest": proposal_digest,
                "decision": decision,
                "actor": actor,
                "expiresAtUtc": expires_at_raw,
                "eventRevision": event_revision,
            }
        )

    return state_revision, policy, policy_digest, exact_decisions


def _index_exact_decisions(
    exact_decisions: list[dict[str, object]],
    *,
    proposals_digest: str,
) -> dict[str, dict[str, object]]:
    indexed: dict[str, dict[str, object]] = {}
    for entry in exact_decisions:
        if entry["proposalDigest"] != proposals_digest:
            # Stale: recorded against a different (earlier) proposals
            # document, so it must not license the current proposal.
            continue
        action_id = str(entry["actionId"])
        if action_id in indexed:
            raise PolicySelectionError(
                f"Conflicting exact decisions for actionId {action_id!r} against the "
                "current proposals digest."
            )
        indexed[action_id] = entry
    return indexed


def _validate_action_events(
    action_events: object,
    *,
    repository: str,
) -> list[dict[str, object]]:
    if not isinstance(action_events, Sequence) or isinstance(action_events, (str, bytes)):
        raise PolicySelectionError("action_events must be a sequence of event objects.")

    normalized: list[dict[str, object]] = []
    identities_by_action_id: dict[str, tuple[str, str, int, str]] = {}
    for raw_event in action_events:
        if not isinstance(raw_event, Mapping):
            raise PolicySelectionError("Each action event must be an object.")

        event_type = raw_event.get("eventType")
        if event_type not in _KNOWN_ACTION_EVENT_TYPES:
            raise PolicySelectionError(f"Unsupported action event eventType: {event_type!r}.")

        event_repository = raw_event.get("repository")
        if not isinstance(event_repository, str) or not event_repository:
            raise PolicySelectionError("Each action event must carry a nonempty repository.")

        if event_repository != repository or event_type not in {"intent", "terminal"}:
            # Budget accounting only counts this repository's intent/terminal
            # events; other repositories and non-budget event kinds
            # (delegation baselines/retirements) do not consume any class or
            # hard-ceiling allowance here.
            continue

        action_id = raw_event.get("actionId")
        operation = raw_event.get("operation")
        target = raw_event.get("target")
        idempotency_key = raw_event.get("idempotencyKey")
        recorded_at_raw = raw_event.get("recordedAt")
        snapshot_id = raw_event.get("snapshotId")
        body_digest = raw_event.get("bodyDigest")
        run_id = raw_event.get("runId")

        if not isinstance(action_id, str) or not action_id:
            raise PolicySelectionError("Action event actionId must be a nonempty string.")
        if not isinstance(operation, str) or not operation:
            raise PolicySelectionError(f"Action event {action_id} operation must be a nonempty string.")
        if (
            not isinstance(target, Mapping)
            or not isinstance(target.get("kind"), str)
            or not target.get("kind")
            or not isinstance(target.get("number"), int)
            or isinstance(target.get("number"), bool)
            or target.get("number") <= 0
        ):
            raise PolicySelectionError(f"Action event {action_id} target is invalid.")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise PolicySelectionError(
                f"Action event {action_id} idempotencyKey must be a nonempty string."
            )
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise PolicySelectionError(f"Action event {action_id} snapshotId must be a nonempty string.")
        if body_digest is not None and (
            not isinstance(body_digest, str) or _DIGEST_RE.fullmatch(body_digest) is None
        ):
            raise PolicySelectionError(f"Action event {action_id} bodyDigest is invalid.")
        if run_id is not None and (not isinstance(run_id, str) or not run_id):
            raise PolicySelectionError(
                f"Action event {action_id} runId must be a nonempty string when present."
            )
        try:
            recorded_at = parse_aware_iso8601(recorded_at_raw, f"{action_id}.recordedAt")
        except ValueError as exc:
            raise PolicySelectionError(str(exc)) from exc

        outcome = None
        if event_type == "terminal":
            outcome = raw_event.get("outcome")
            if outcome not in _TERMINAL_OUTCOMES:
                raise PolicySelectionError(f"Action event {action_id} has an unsupported outcome.")

        identity = (operation, str(target["kind"]), int(target["number"]), idempotency_key)
        previous_identity = identities_by_action_id.get(action_id)
        if previous_identity is not None and previous_identity != identity:
            raise PolicySelectionError(
                f"Action events for {action_id!r} disagree on operation/target/idempotencyKey."
            )
        identities_by_action_id[action_id] = identity

        normalized.append(
            {
                "eventType": event_type,
                "actionId": action_id,
                "operation": operation,
                "operationClass": classify_operation(operation),
                "targetKind": str(target["kind"]),
                "targetNumber": int(target["number"]),
                "idempotencyKey": idempotency_key,
                "bodyDigest": body_digest,
                "snapshotId": snapshot_id,
                "recordedAt": recorded_at,
                "outcome": outcome,
                "runId": run_id,
                "raw": dict(raw_event),
            }
        )
    return normalized


def _compute_usage(
    normalized_events: list[dict[str, object]],
    *,
    run_id: str,
    now: datetime,
) -> tuple[dict[str, int], dict[str, int], int, int]:
    this_run_ids: dict[str | None, set[str]] = {}
    rolling_ids: dict[str | None, set[str]] = {}
    window_start = now - _ROLLING_WINDOW

    for event in normalized_events:
        op_class = event["operationClass"]
        if event["runId"] == run_id:
            this_run_ids.setdefault(op_class, set()).add(str(event["actionId"]))
        if event["eventType"] == "terminal" and window_start < event["recordedAt"] <= now:
            rolling_ids.setdefault(op_class, set()).add(str(event["actionId"]))

    this_run_used = {cls: len(this_run_ids.get(cls, ())) for cls in OPERATION_CLASSES}
    rolling_used = {cls: len(rolling_ids.get(cls, ())) for cls in OPERATION_CLASSES}
    overall_this_run = len({action_id for ids in this_run_ids.values() for action_id in ids})
    overall_rolling = len({action_id for ids in rolling_ids.values() for action_id in ids})
    return this_run_used, rolling_used, overall_this_run, overall_rolling


def _build_budgets(
    policy: OperationPolicyRevision | None,
    policy_active: bool,
    this_run_used: dict[str, int],
    rolling_used: dict[str, int],
) -> dict[str, dict[str, object]]:
    budgets: dict[str, dict[str, object]] = {}
    for cls in OPERATION_CLASSES:
        if policy is not None:
            class_policy = policy.operation_classes[cls]
            max_per_run = class_policy.max_per_run
            max_rolling_24h = class_policy.max_rolling_24h
            enabled = policy_active and class_policy.enabled
        else:
            max_per_run = 0
            max_rolling_24h = 0
            enabled = False

        used_this_run = this_run_used[cls]
        used_rolling_24h = rolling_used[cls]
        budgets[cls] = {
            "enabled": enabled,
            "maxPerRun": max_per_run,
            "maxRolling24h": max_rolling_24h,
            "usedThisRun": used_this_run,
            "usedRolling24h": used_rolling_24h,
            "remainingThisRun": max(0, max_per_run - used_this_run) if enabled else 0,
            "remainingRolling24h": max(0, max_rolling_24h - used_rolling_24h) if enabled else 0,
        }
    return budgets
