from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from .repository_policy import RepositoryPolicy
from .eligibility import diagnostic_collection_error


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
    global_blockers = (
        [{"reason": f"observations:collection-error:{observation_error}"}]
        if observation_error is not None else []
    )
    global_blockers.extend(
        {"reason": f"collection:{error.get('stage', 'unknown')}"}
        for error in snapshot.get("collectionErrors", [])
        if not isinstance(error.get("scope"), Mapping)
        or error["scope"].get("kind") != "issue"
        or not _positive_ints(error["scope"].get("issueNumbers"))
    )
    if policy is None or not policy.managed_automation_explicit:
        return {
            "schemaVersion": 2,
            "repository": repository,
            "valid": not global_blockers,
            "counts": {},
            "items": [],
            "blockers": [item["reason"] for item in global_blockers],
            "globalBlockers": global_blockers,
            "blockedScopes": [],
        }
    delegation_by_issue = _delegations(snapshot)
    delegated_numbers = _positive_ints(snapshot.get("delegatedIssues")) | set(delegation_by_issue)
    requested_numbers = _positive_ints(snapshot.get("delegationRequests"))
    issue_numbers = {
        number
        for number in _positive_ints(snapshot.get("openIssues")) | delegated_numbers | requested_numbers
        if number in delegated_numbers
        or number in requested_numbers
        or _issue_producer(snapshot, number).casefold() in policy.managed_issue_producers
    }
    pull_request_numbers = (
        _positive_ints(snapshot.get("openPullRequests"))
        if policy.manages_pull_requests
        else set()
    )
    proposals_by_issue = _issue_numbers(proposals.get("proposals"))
    investigation_assignments = {
        proposal["actionId"]
        for proposal in proposals.get("proposals", [])
        if proposal.get("operation") == "assign-copilot"
        and proposal.get("evidenceBasis") in {"operator-request", "investigation-request", "workflow-producer", "source-reconciliation"}
        and proposal.get("executionEligibility", {}).get("eligible") is True
    }
    assignment_issues = {
        proposal["issueNumber"] for proposal in proposals.get("proposals", [])
        if proposal.get("actionId") in investigation_assignments
    }
    investigations = {
        reason: _investigation_issue_numbers(
            investigation_plan, field, repository=repository,
            snapshot_id=proposals.get("snapshotId"),
        )
        for reason, field in (
            ("active-investigation", "activeInvestigations"),
            ("pending-investigation", "pendingInvestigations"),
            ("queued-investigation", "requests"),
            ("deferred-investigation", "deferredRequests"),
        )
    }
    awaiting_evidence = _issue_numbers(investigation_plan.get("blockedAwaitingEvidence"))
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
        issue_record = snapshot.get("evidence", {}).get(f"issue:{issue_number}", {})
        tracking_active = delegation is not None and (
            _has_open_pull_request(delegation)
            or delegation.get("lifecycle") not in {"completed", "closed_unmerged", "retired"}
        )
        if (
            issue_record.get("availability") == "available"
            and issue_record.get("payload", {}).get("state") == "closed"
            and not tracking_active
        ):
            reasons.append("terminal-disposition")
        elif issue_number in proposals_by_issue or issue_number in requested_numbers:
            reasons.append("pending-action")
        elif delegation is not None and delegation.get("requiresNewDecision") is True:
            reasons.append("awaiting-new-decision")
        if delegation is not None and _has_open_pull_request(delegation):
            reasons.append("tracked-open-pr")
        if (
            delegation is not None
            and delegation.get("lifecycle") not in {"completed", "closed_unmerged", "retired"}
        ):
            reasons.append("active-delegation")
        for reason, numbers in investigations.items():
            if issue_number in numbers:
                reasons.append(reason)
        if issue_number in awaiting_evidence:
            reasons.append("blocked-awaiting-evidence")
        if issue_number in scheduled_issues or _has_typed_wakeup(delegation):
            reasons.append("scheduled-wakeup")
        items.append(
            _project_item(
                "issue",
                issue_number,
                reasons,
                forced_uncovered=(
                    "verified workflow-run scope is unknown"
                    if issue_number in unknown_scope_issues and issue_number not in assignment_issues
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
    blocked_scopes = [
        {"kind": item["targetKind"], "issueNumber": item["targetNumber"],
         "reason": item["status"]}
        for item in invalid
    ]
    # Diagnostic gaps can prohibit a recovery claim without prohibiting a task
    # whose purpose is to investigate those gaps. Scope that exemption to the
    # exact eligible assignment; it must never license a comment or closure.
    def diagnostic_blockers(number: int, reason: str) -> list[dict[str, object]]:
        if number not in assignment_issues:
            return [{"kind": "issue", "issueNumber": number, "reason": reason}]
        return [
            {"kind": "action", "actionId": proposal["actionId"], "reason": reason}
            for proposal in proposals.get("proposals", [])
            if proposal.get("issueNumber") == number
            and proposal["actionId"] not in investigation_assignments
        ]

    for item in investigation_plan.get("blockedAwaitingEvidence", []):
        if item["issueNumber"] in assignment_issues and item["target"].get("kind") == "issue":
            blocked_scopes.extend(diagnostic_blockers(item["issueNumber"], "blocked-awaiting-evidence"))
        else:
            blocked_scopes.append({
                "kind": "target", "issueNumber": item["issueNumber"],
                "target": item["target"], "reason": "blocked-awaiting-evidence",
            })
    for number in sorted(unknown_scope_issues & assignment_issues):
        blocked_scopes.extend(diagnostic_blockers(number, "unknown-workflow-scope"))
    for error in snapshot.get("collectionErrors", []):
        scope = error.get("scope", {})
        if isinstance(scope, Mapping) and scope.get("kind") == "issue":
            for number in sorted(_positive_ints(scope.get("issueNumbers"))):
                if diagnostic_collection_error(error):
                    blocked_scopes.extend(diagnostic_blockers(number, "collection-incomplete"))
                else:
                    blocked_scopes.append({
                        "kind": "issue", "issueNumber": number, "reason": "collection-incomplete",
                    })
    return {
        "schemaVersion": 2,
        "repository": repository,
        "valid": not blocked_scopes and not global_blockers,
        "counts": dict(sorted(counts.items())),
        "items": items,
        "blockers": [
            f"{item['targetKind']}:{item['targetNumber']}:{item['status']}"
            for item in invalid
        ]
        + [item["reason"] for item in global_blockers],
        "globalBlockers": global_blockers,
        "blockedScopes": blocked_scopes,
    }


def validate_coverage_capability(coverage: object) -> None:
    if not isinstance(coverage, dict):
        raise ValueError("managedItemCoverage must be an object.")
    fields = {"schemaVersion", "valid", "blockers"}
    version = coverage.get("schemaVersion")
    if version == 2:
        fields |= {"globalBlockers", "blockedScopes"}
    if (
        type(version) is not int or version not in {1, 2}
        or set(coverage) != fields
        or type(coverage.get("valid")) is not bool
        or not isinstance(coverage.get("blockers"), list)
        or any(not isinstance(item, str) or not item.strip() for item in coverage["blockers"])
    ):
        raise ValueError("managedItemCoverage is invalid.")
    if version == 1:
        return
    if not isinstance(coverage["globalBlockers"], list) or not isinstance(coverage["blockedScopes"], list):
        raise ValueError("managedItemCoverage scopes must be arrays.")
    for blocker in coverage["globalBlockers"]:
        if not isinstance(blocker, dict) or set(blocker) != {"reason"} or not _nonempty(blocker["reason"]):
            raise ValueError("Invalid global coverage blocker.")
    for scope in coverage["blockedScopes"]:
        if not isinstance(scope, dict) or not _nonempty(scope.get("reason")):
            raise ValueError("Invalid blocked coverage scope.")
        kind = scope.get("kind")
        fields = {"kind", "reason", "actionId"} if kind == "action" else {"kind", "reason", "issueNumber"}
        if kind == "target":
            fields.add("target")
        if kind not in {"issue", "target", "action"} or set(scope) != fields:
            raise ValueError("Invalid blocked coverage scope.")
        if kind == "action":
            if not _nonempty(scope["actionId"]):
                raise ValueError("Invalid blocked action identity.")
        elif type(scope["issueNumber"]) is not int or scope["issueNumber"] < 1:
            raise ValueError("Invalid blocked issue identity.")
        if kind == "target":
            target = scope["target"]
            if (
                not isinstance(target, dict) or set(target) != {"kind", "value"}
                or target["kind"] not in {"issue", "test", "workflow-run", "failure-fingerprint"}
                or (target["kind"] == "issue" and (type(target["value"]) is not int or target["value"] != scope["issueNumber"]))
                or (target["kind"] != "issue" and not _nonempty(target["value"]))
            ):
                raise ValueError("Invalid blocked target identity.")
    if coverage["valid"] != (not coverage["globalBlockers"] and not coverage["blockedScopes"]):
        raise ValueError("Coverage validity contradicts its scopes.")


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def coverage_exclusions(coverage: Mapping[str, Any] | None, proposals: Sequence[Mapping[str, Any]]) -> tuple[bool, set[str]]:
    if coverage is None:
        return False, set()
    validate_coverage_capability(coverage)
    # Old invalid projections carry only prose scope, which cannot license an
    # item-local exception. Their entire proposal remains blocked.
    global_stop = (
        coverage.get("valid") is not True or bool(coverage.get("blockers"))
        if coverage["schemaVersion"] == 1 else bool(coverage["globalBlockers"])
    )
    blocked = set()
    for proposal in proposals:
        if global_stop:
            blocked.add(proposal["actionId"])
        for scope in coverage.get("blockedScopes", []):
            matches = (
                proposal["actionId"] == scope["actionId"] if scope["kind"] == "action"
                else proposal["issueNumber"] == scope["issueNumber"]
            )
            # Issue-state actions have no narrower target identity. They affect
            # the issue as a whole and cannot bypass an unresolved child target.
            if matches and scope["kind"] == "target" and proposal.get("target") is not None:
                matches = proposal["target"] == scope["target"]
            if matches:
                blocked.add(proposal["actionId"])
    while True:
        dependents = {p["actionId"] for p in proposals if p.get("dependsOn") in blocked}
        if dependents.issubset(blocked):
            break
        blocked.update(dependents)
    return global_stop, blocked


def block_policy_selection(
    selection: Mapping[str, Any],
    coverage: Mapping[str, Any],
) -> dict[str, object]:
    if coverage.get("schemaVersion") == 2 and not coverage.get("globalBlockers"):
        return dict(selection)
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
        f"Mutation gate: **{'blocked' if coverage.get('globalBlockers') else 'item-local' if coverage.get('blockedScopes') else 'open'}**",
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
        "awaiting-new-decision",
        "active-delegation",
        "active-investigation",
        "pending-investigation",
        "blocked-awaiting-evidence",
        "queued-investigation",
        "deferred-investigation",
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


def _investigation_issue_numbers(
    plan: Mapping[str, Any], field: str, *, repository: str, snapshot_id: object,
) -> set[int]:
    # Only the current frozen plan establishes queued work. A bare issue number
    # or an old plan is not an investigation and must not fill a coverage gap.
    if (
        not isinstance(snapshot_id, str) or not snapshot_id
        or plan.get("snapshotId") != snapshot_id
        or plan.get("repository") != repository
    ):
        return set()
    rows = plan.get(field)
    if not isinstance(rows, list):
        return set()
    result: set[int] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        number, identity, target = row.get("issueNumber"), row.get("investigationId"), row.get("target")
        if (
            type(number) is not int or number <= 0
            or not isinstance(identity, str) or not identity
            or not isinstance(target, Mapping)
            or target.get("kind") not in {"issue", "test", "workflow-run", "failure-fingerprint"}
            or (target.get("kind") == "issue" and (
                type(target.get("value")) is not int or target.get("value") != number
            ))
            or (target.get("kind") != "issue" and not _nonempty(target.get("value")))
        ):
            continue
        if field == "activeInvestigations":
            # The plan producer derives this index only from the latest
            # registered session events whose status is started.
            active_ids = plan.get("activeInvestigationIds")
            if not isinstance(active_ids, list) or identity not in active_ids:
                continue
        if field == "pendingInvestigations":
            pending_ids = plan.get("pendingInvestigationIds")
            if (
                not isinstance(pending_ids, list) or identity not in pending_ids
                or row.get("status") not in {"prepared", "dispatching"}
            ):
                continue
        if field == "deferredRequests" and not _nonempty(row.get("reason")):
            continue
        result.add(number)
    return result


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
