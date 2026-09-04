from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from .repository_policy import RepositoryPolicy


def build_managed_item_coverage(
    snapshot: Mapping[str, Any],
    *,
    policy: RepositoryPolicy | None,
    proposals: Mapping[str, Any],
    investigation_plan: Mapping[str, Any],
    review_schedule: Mapping[str, Any],
    observations: Mapping[str, Any],
    observation_error: str | None = None,
) -> dict[str, object]:
    repository = str(snapshot.get("repository") or "")
    if policy is None or not policy.managed_automation_explicit:
        return {
            "schemaVersion": 1,
            "repository": repository,
            "valid": True,
            "counts": {},
            "items": [],
            "blockers": [],
        }
    delegated_numbers = _positive_ints(snapshot.get("delegatedIssues"))
    issue_numbers = {
        number
        for number in _positive_ints(snapshot.get("openIssues")) | delegated_numbers
        if number in delegated_numbers
        or _issue_producer(snapshot, number).casefold() in policy.managed_issue_producers
    }
    pull_request_numbers = (
        _positive_ints(snapshot.get("openPullRequests"))
        if policy.manages_pull_requests
        else set()
    )
    proposals_by_issue = _issue_numbers(proposals.get("proposals"))
    investigations = _investigation_issue_numbers(investigation_plan)
    delegation_by_issue = _delegations(snapshot)
    scheduled_issues = _scheduled_numbers(review_schedule.get("issues"))
    scheduled_pull_requests = _scheduled_numbers(review_schedule.get("pullRequests"))
    unknown_scope_issues = {
        int(occurrence["issueNumber"])
        for occurrence in observations.get("occurrences", [])
        if isinstance(occurrence, Mapping)
        and isinstance(occurrence.get("issueNumber"), int)
        and isinstance(occurrence.get("verifiedScope"), Mapping)
        and occurrence["verifiedScope"].get("kind") == "unknown"
    }

    items: list[dict[str, object]] = []
    for issue_number in sorted(issue_numbers):
        delegation = delegation_by_issue.get(issue_number)
        reasons: list[str] = []
        if delegation is not None and delegation.get("lifecycle") == "completed":
            reasons.append("terminal-disposition")
        if issue_number in proposals_by_issue:
            reasons.append("pending-action")
        if delegation is not None and _has_open_pull_request(delegation):
            reasons.append("tracked-open-pr")
        if (
            delegation is not None
            and delegation.get("lifecycle") not in {"completed", "retired"}
        ):
            reasons.append("active-delegation")
        if issue_number in investigations:
            reasons.append("active-investigation")
        if issue_number in scheduled_issues or _has_typed_wakeup(delegation):
            reasons.append("scheduled-wakeup")
        items.append(
            _project_item(
                "issue",
                issue_number,
                reasons,
                forced_uncovered=(
                    "verified workflow-run scope is unknown"
                    if issue_number in unknown_scope_issues
                    else None
                ),
            )
        )
    for pull_request_number in sorted(pull_request_numbers):
        items.append(
            _project_item(
                "pull-request",
                pull_request_number,
                ["tracked-open-pr", *(["scheduled-wakeup"] if pull_request_number in scheduled_pull_requests else [])],
            )
        )

    counts = Counter(str(item["coverageReason"]) for item in items)
    invalid = [
        item
        for item in items
        if item["status"] in {"uncovered", "conflicting"}
    ]
    collection_blockers = (
        [f"observations:collection-error:{observation_error}"]
        if observation_error is not None
        else []
    )
    return {
        "schemaVersion": 1,
        "repository": repository,
        "valid": not invalid and not collection_blockers,
        "counts": dict(sorted(counts.items())),
        "items": items,
        "blockers": [
            f"{item['targetKind']}:{item['targetNumber']}:{item['status']}"
            for item in invalid
        ]
        + collection_blockers,
    }


def block_policy_selection(
    selection: Mapping[str, Any],
    coverage: Mapping[str, Any],
) -> dict[str, object]:
    if coverage.get("valid") is True:
        return dict(selection)
    blocked = dict(selection)
    blocked["automaticActionIds"] = []
    blocked["exactActionIds"] = []
    blocked["selectedActionIds"] = []
    blocked["mutationBlocked"] = True
    blocked["mutationBlockers"] = list(coverage.get("blockers", []))
    blocked["maximumWriteExposure"] = {"thisRun": 0, "rolling24h": 0}
    blocked["candidates"] = [
        {
            **candidate,
            "status": "ineligible",
            "reason": "managed-item-coverage-invalid",
        }
        if isinstance(candidate, Mapping)
        and candidate.get("status") in {"automatic", "exact"}
        else candidate
        for candidate in selection.get("candidates", [])
    ]
    return blocked


def render_managed_item_coverage_section(coverage: Mapping[str, Any]) -> str:
    counts = coverage.get("counts", {})
    count_text = ", ".join(
        f"{reason}: **{count}**"
        for reason, count in sorted(counts.items())
    ) or "none"
    lines = [
        "## Managed active-item coverage",
        "",
        f"Mutation gate: **{'open' if coverage.get('valid') is True else 'blocked'}**",
        "",
        f"Coverage counts: {count_text}",
        "",
        "| Target | Coverage | Status | Detail |",
        "|---|---|---|---|",
    ]
    for blocker in coverage.get("blockers", []):
        if isinstance(blocker, str) and blocker.startswith(
            "observations:collection-error:"
        ):
            lines.append(
                "| `collection:observations` | `collection-error` | "
                f"`uncovered` | {blocker.partition('collection-error:')[2]} |"
            )
    for item in coverage.get("items", []):
        if not isinstance(item, Mapping):
            continue
        target = f"{item.get('targetKind')}:{item.get('targetNumber')}"
        lines.append(
            f"| `{target}` | `{item.get('coverageReason')}` | "
            f"`{item.get('status')}` | {item.get('detail') or '—'} |"
        )
    return "\n".join(lines) + "\n"


def _project_item(
    target_kind: str,
    target_number: int,
    reasons: Sequence[str],
    *,
    forced_uncovered: str | None = None,
) -> dict[str, object]:
    unique = sorted(set(reasons))
    if forced_uncovered is not None:
        return {
            "targetKind": target_kind,
            "targetNumber": target_number,
            "coverageReason": "uncovered",
            "status": "uncovered",
            "detail": forced_uncovered,
        }
    if not unique:
        return {
            "targetKind": target_kind,
            "targetNumber": target_number,
            "coverageReason": "uncovered",
            "status": "uncovered",
            "detail": "no terminal disposition, active work, pending action, or typed wakeup",
        }
    precedence = (
        "terminal-disposition",
        "pending-action",
        "tracked-open-pr",
        "active-delegation",
        "active-investigation",
        "scheduled-wakeup",
    )
    selected = next(reason for reason in precedence if reason in unique)
    if "terminal-disposition" in unique and len(unique) > 1:
        return {
            "targetKind": target_kind,
            "targetNumber": target_number,
            "coverageReason": "conflicting",
            "status": "conflicting",
            "detail": ", ".join(unique),
        }
    return {
        "targetKind": target_kind,
        "targetNumber": target_number,
        "coverageReason": selected,
        "status": "covered",
        "detail": None,
    }


def _positive_ints(value: object) -> set[int]:
    if not isinstance(value, list):
        return set()
    return {
        item
        for item in value
        if isinstance(item, int) and not isinstance(item, bool) and item > 0
    }


def _issue_numbers(value: object) -> set[int]:
    if not isinstance(value, list):
        return set()
    return {
        int(item["issueNumber"])
        for item in value
        if isinstance(item, Mapping)
        and isinstance(item.get("issueNumber"), int)
        and not isinstance(item.get("issueNumber"), bool)
    }


def _issue_producer(snapshot: Mapping[str, Any], issue_number: int) -> str:
    evidence = snapshot.get("evidence")
    record = evidence.get(f"issue:{issue_number}") if isinstance(evidence, Mapping) else None
    payload = record.get("payload") if isinstance(record, Mapping) else None
    producer = payload.get("producer") if isinstance(payload, Mapping) else None
    return producer if isinstance(producer, str) else "unknown"


def _delegations(snapshot: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    status = snapshot.get("delegationStatus")
    records = status.get("records") if isinstance(status, Mapping) else None
    if not isinstance(records, list):
        return {}
    return {
        int(record["issueNumber"]): record
        for record in records
        if isinstance(record, Mapping)
        and isinstance(record.get("issueNumber"), int)
        and not isinstance(record.get("issueNumber"), bool)
    }


def _has_open_pull_request(delegation: Mapping[str, Any]) -> bool:
    pull_requests = delegation.get("pullRequests")
    return isinstance(pull_requests, list) and any(
        isinstance(pull_request, Mapping)
        and pull_request.get("state") == "open"
        for pull_request in pull_requests
    )


def _has_typed_wakeup(delegation: Mapping[str, Any] | None) -> bool:
    return (
        isinstance(delegation, Mapping)
        and isinstance(delegation.get("nextWakeup"), Mapping)
        and isinstance(delegation["nextWakeup"].get("reason"), str)
        and isinstance(delegation["nextWakeup"].get("evaluateAt"), str)
    )


def _investigation_issue_numbers(plan: Mapping[str, Any]) -> set[int]:
    return _issue_numbers(
        [
            *(
                plan.get("requests")
                if isinstance(plan.get("requests"), list)
                else []
            ),
            *(
                plan.get("deferredRequests")
                if isinstance(plan.get("deferredRequests"), list)
                else []
            ),
            *(
                plan.get("activeInvestigations")
                if isinstance(plan.get("activeInvestigations"), list)
                else []
            ),
        ]
    )


def _scheduled_numbers(value: object) -> set[int]:
    if not isinstance(value, Mapping):
        return set()
    return {
        int(number)
        for number, context in value.items()
        if str(number).isdigit()
        and isinstance(context, Mapping)
        and isinstance(context.get("reassessAt"), str)
        and isinstance(context.get("wakeReason"), str)
    }
