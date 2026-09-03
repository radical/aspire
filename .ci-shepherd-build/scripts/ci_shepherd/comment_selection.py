from __future__ import annotations

from collections.abc import Mapping
import hashlib

from .authorization import MAX_PRODUCTION_COMMENT_ACTIONS
from .models import stable_json


__all__ = [
    "COMMENT_OPERATIONS",
    "PRIORITY_SUFFIXES",
    "build_comment_selection",
    "operation_priority",
    "priority_for_action_id",
    "render_comment_selection_section",
]


COMMENT_OPERATIONS = frozenset({"create-comment", "edit-comment"})
PRIORITY_SUFFIXES = (
    ("ping-human-comment", "human input requested"),
    ("quarantine-reconciliation-comment", "quarantine state reconciliation"),
    ("delegation-handoff-comment", "delegation handoff"),
    ("watch-comment", "watch status"),
    ("retire-status-comment", "status retirement"),
    ("review-close-comment", "closure review"),
)
# Legacy private aliases: nothing outside this module referenced these names,
# but they are kept so any future in-module or external private access still
# resolves to the same objects.
_COMMENT_OPERATIONS = COMMENT_OPERATIONS
_PRIORITY_SUFFIXES = PRIORITY_SUFFIXES


def priority_for_action_id(action_id: str) -> tuple[int, str]:
    """Rank an actionId by its committed semantic-suffix priority table.

    Shared with ``policy_selection`` so both modules agree on which comment
    role ranks first without duplicating the suffix table.
    """
    for priority, (suffix, reason) in enumerate(PRIORITY_SUFFIXES):
        if action_id.endswith(suffix):
            return priority, reason
    return len(PRIORITY_SUFFIXES), "other issue comment"


def operation_priority(operation: str) -> int:
    """Return the edit-before-create tiebreak used within a priority tier."""
    return 0 if operation == "edit-comment" else 1


def build_comment_selection(
    proposals_document: object,
    *,
    max_comments: int,
) -> dict[str, object]:
    """Select a deterministic, bounded set of executable issue comments."""

    if not isinstance(proposals_document, Mapping):
        raise TypeError("Action proposals must be an object.")
    if not 1 <= max_comments <= MAX_PRODUCTION_COMMENT_ACTIONS:
        raise ValueError(
            f"max_comments must be between 1 and {MAX_PRODUCTION_COMMENT_ACTIONS}."
        )
    repository = proposals_document.get("repository")
    snapshot_id = proposals_document.get("snapshotId")
    proposals = proposals_document.get("proposals")
    if not isinstance(repository, str) or not repository:
        raise ValueError("Action proposals repository must be nonempty.")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise ValueError("Action proposals snapshotId must be nonempty.")
    if not isinstance(proposals, list):
        raise TypeError("Action proposals proposals must be a list.")

    candidates: list[tuple[tuple[int, int, int, str], dict[str, object]]] = []
    excluded: list[dict[str, object]] = []
    for proposal in proposals:
        if not isinstance(proposal, Mapping):
            raise TypeError("Each action proposal must be an object.")
        action_id = proposal.get("actionId")
        issue_number = proposal.get("issueNumber")
        operation = proposal.get("operation")
        eligibility = proposal.get("executionEligibility")
        if (
            not isinstance(action_id, str)
            or not action_id
            or not isinstance(issue_number, int)
            or isinstance(issue_number, bool)
            or issue_number <= 0
            or not isinstance(operation, str)
        ):
            raise ValueError("Action proposals must carry valid action identities.")

        reasons: list[str] = []
        if operation not in COMMENT_OPERATIONS:
            reasons.append("not-comment-operation")
        if proposal.get("dependsOn") is not None:
            reasons.append("dependent-action")
        if not isinstance(eligibility, Mapping) or eligibility.get("eligible") is not True:
            blocking_reasons = (
                eligibility.get("blockingReasons")
                if isinstance(eligibility, Mapping)
                else None
            )
            if isinstance(blocking_reasons, list):
                reasons.extend(str(reason) for reason in blocking_reasons)
            if not reasons:
                reasons.append("not-execution-eligible")
        if reasons:
            excluded.append({"actionId": action_id, "reasons": sorted(set(reasons))})
            continue

        priority, priority_reason = priority_for_action_id(action_id)
        op_priority = operation_priority(operation)
        candidates.append(
            (
                (priority, op_priority, issue_number, action_id),
                {
                    "actionId": action_id,
                    "issueNumber": issue_number,
                    "operation": operation,
                    "priority": priority,
                    "priorityReason": priority_reason,
                    "operationPriority": op_priority,
                },
            )
        )

    ranked: list[dict[str, object]] = []
    seen_issue_numbers: set[int] = set()
    for _, candidate in sorted(candidates, key=lambda item: item[0]):
        issue_number = int(candidate["issueNumber"])
        if issue_number in seen_issue_numbers:
            excluded.append(
                {
                    "actionId": candidate["actionId"],
                    "reasons": ["lower-priority-comment-for-same-issue"],
                }
            )
            continue
        seen_issue_numbers.add(issue_number)
        ranked.append({**candidate, "rank": len(ranked) + 1})

    selected = ranked[:max_comments]
    eligible_count = len(ranked)
    cut_reason = (
        f"Selected the first {len(selected)} of {eligible_count} eligible issue "
        "comments by the committed priority order."
        if eligible_count > len(selected)
        else "All eligible issue comments were selected."
    )
    excluded.sort(key=lambda item: str(item["actionId"]))
    return {
        "schemaVersion": 1,
        "repository": repository,
        "snapshotId": snapshot_id,
        "proposalsDigest": (
            "sha256:"
            + hashlib.sha256(stable_json(proposals_document).encode("utf-8")).hexdigest()
        ),
        "maxComments": max_comments,
        "eligibleCount": eligible_count,
        "selectedCount": len(selected),
        "selectedActionIds": [candidate["actionId"] for candidate in selected],
        "rankedCandidates": ranked,
        "excluded": excluded,
        "cutApplied": eligible_count > len(selected),
        "cutReason": cut_reason,
    }


def render_comment_selection_section(selection: Mapping[str, object]) -> str:
    lines = [
        "## Production comment selection",
        "",
        (
            f"Selected **{selection['selectedCount']}** of "
            f"**{selection['eligibleCount']}** eligible issue comments "
            f"(limit: {selection['maxComments']})."
        ),
        "",
        f"**Selection result:** {selection['cutReason']}",
        "",
        "| Rank | Issue | Operation | Priority | Selected |",
        "|---:|---:|---|---|---|",
    ]
    selected_ids = set(selection["selectedActionIds"])
    for candidate in selection["rankedCandidates"]:
        lines.append(
            "| {rank} | #{issue} | `{operation}` | {reason} | {selected} |".format(
                rank=candidate["rank"],
                issue=candidate["issueNumber"],
                operation=candidate["operation"],
                reason=candidate["priorityReason"],
                selected="yes" if candidate["actionId"] in selected_ids else "no",
            )
        )
    if not selection["rankedCandidates"]:
        lines.append("| - | - | - | No eligible comments | - |")
    return "\n".join(lines) + "\n"
