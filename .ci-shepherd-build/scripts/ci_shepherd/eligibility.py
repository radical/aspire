from __future__ import annotations

import re
from collections.abc import Mapping, Sequence


MAX_DELEGATION_REQUESTS = 5


EXECUTABLE_CI_LABELS = frozenset(
    {"automation-broken", "ci-failure-cause", "test-failure"}
)

# These collector stages diagnose CI failures; they do not establish the current
# issue's identity, ownership, authorization, or delegation capacity.
DIAGNOSTIC_COLLECTION_STAGES = frozenset({
    "workflow-run", "workflow-jobs", "workflow-log", "workflow-history",
    "workflow-test-results", "workflow-artifacts", "workflow-annotation",
    "repair-comparison",
    "ownership-checkout", "ownership-codeowners", "ownership-history",
})


def diagnostic_collection_error(error: object) -> bool:
    if not isinstance(error, Mapping):
        return False
    scope = error.get("scope")
    return (
        error.get("stage") in DIAGNOSTIC_COLLECTION_STAGES
        and isinstance(scope, Mapping)
        and scope.get("kind") == "issue"
        and isinstance(scope.get("issueNumbers"), list)
        and bool(scope["issueNumbers"])
        and all(type(number) is int and number > 0 for number in scope["issueNumbers"])
    )


def label_names(raw_labels: object) -> frozenset[str]:
    if not isinstance(raw_labels, list):
        return frozenset()

    names: set[str] = set()
    for raw_label in raw_labels:
        name = (
            raw_label
            if isinstance(raw_label, str)
            else raw_label.get("name")
            if isinstance(raw_label, Mapping)
            else None
        )
        if isinstance(name, str) and (normalized := name.strip().casefold()):
            names.add(normalized)
    return frozenset(names)


def executable_ci_labels(raw_labels: object) -> frozenset[str]:
    return label_names(raw_labels).intersection(EXECUTABLE_CI_LABELS)


def issue_body_field(body: str, field: str) -> str | None:
    # Reports use one-line fields such as "- Assessment: Azure tenant is expired.".
    match = re.search(rf"(?im)^-\s*{re.escape(field)}:\s*(.+)$", body)
    return match.group(1).strip() if match else None


# Keep the known Azure tenant/identity decision gate narrow. Generic words such
# as "credential", "replace", or "permission" also describe ordinary code fixes.
_HUMAN_DECISION_EVIDENCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\btenant\b[^.]{0,80}\bexpired\b"),
    re.compile(r"\bexpired\b[^.]{0,80}\btenant\b"),
    re.compile(r"\baadsts5000229\b"),
    re.compile(r"\bservice[\s-]principal\b[^.]{0,80}\bidentity migration\b"),
    re.compile(r"\bidentity migration\b[^.]{0,80}\bservice[\s-]principal\b"),
)


def reported_issue_requires_human_decision(assessment: object, suggestion: object) -> bool:
    if not isinstance(assessment, str) or not isinstance(suggestion, str):
        return False
    text = f"{assessment} {suggestion}".lower()
    return any(pattern.search(text) for pattern in _HUMAN_DECISION_EVIDENCE_PATTERNS)


def human_decision_blocks_delegation(issue: Mapping[str, object]) -> bool:
    # An explicit nomination is an operator decision, not a model override.
    if issue.get("delegationRequest") == {"origin": "operator"}:
        return False
    for record in issue.get("evidenceBundle", issue.get("allowedEvidence", [])):
        if not isinstance(record, Mapping) or record.get("id") != f"issue:{issue['issueNumber']}":
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            return False
        context = payload.get("dashboardContext")
        if isinstance(context, Mapping):
            return reported_issue_requires_human_decision(
                context.get("reportedAssessment"), context.get("reportedSuggested"),
            )
        body = payload.get("body")
        return isinstance(body, str) and reported_issue_requires_human_decision(
            issue_body_field(body, "Assessment"), issue_body_field(body, "Suggested"),
        )
    return False


def delegation_replacement_ready(records: Sequence[Mapping[str, object]]) -> bool:
    if not records or any(
        (
            record.get("taskObservation") == "available"
            and record.get("taskState") in {"queued", "in_progress"}
        )
        or any(pull.get("state") in {"open", "unknown"} for pull in record.get("pullRequests", []))
        for record in records
    ):
        return False
    latest = max(records, key=lambda record: (record["startedAt"], record["actionId"]))
    pulls = latest.get("pullRequests", [])
    terminal_pr = (
        latest.get("attemptOutcome") in {"merged", "closed-unmerged"}
        and bool(pulls)
        and all(pull.get("state") in {"merged", "closed"} for pull in pulls)
    )
    ended_without_pr = (
        latest.get("attemptOutcome") == "unresolved"
        and isinstance(latest.get("taskId"), str) and bool(latest["taskId"])
        and latest.get("taskObservation") == "available"
        and latest.get("taskState") in {
            "completed", "failed", "idle", "waiting_for_user", "timed_out", "cancelled",
        }
        and not pulls
    )
    return latest.get("requiresNewDecision") is True and (terminal_pr or ended_without_pr)


def related_repairs_block_delegation(issue: Mapping[str, object]) -> bool:
    related = issue.get("relatedWorkflowRepairs", [])
    return bool(related) and (
        issue.get("delegationRequest") != {"origin": "operator"}
        or any(repair.get("replacementReady") is not True for repair in related)
    )


def delegation_readiness(issue: Mapping[str, object], category: str) -> dict[str, object] | None:
    """Readiness to request investigation is not proof of a diagnosis or recovery."""
    if human_decision_blocks_delegation(issue) or related_repairs_block_delegation(issue):
        return None
    explicit = issue.get("delegationRequest") == {"origin": "operator"}
    health = issue.get("workflowHealth")
    workflow = (
        isinstance(health, Mapping) and health.get("current") is True
        and health.get("route") == "delegate-copilot" and health.get("category") == category
    )
    context = issue.get("delegationContext")
    if isinstance(context, Mapping) and (
        not explicit or not delegation_replacement_ready(context.get("records", []))
    ):
        return None
    evidence_id = f"issue:{issue['issueNumber']}"
    records = issue.get("evidenceBundle", issue.get("allowedEvidence", []))
    evidence = next(
        (record for record in records if isinstance(record, Mapping) and record.get("id") == evidence_id),
        None,
    )
    if evidence is None or evidence.get("availability") != "available":
        return None
    payload = evidence.get("payload", {})
    if not isinstance(payload, Mapping) or payload.get("state") != "open" or payload.get("assignees"):
        return None
    labels = label_names(payload.get("labels")) if isinstance(payload, Mapping) else frozenset()
    maintenance = issue.get("testMaintenance", {})
    quarantined = isinstance(maintenance, Mapping) and maintenance.get("state") == "quarantined"
    if not explicit and not (
        category in {"blocking-build", "product-or-tooling"} and labels.intersection(EXECUTABLE_CI_LABELS)
        or workflow and labels.intersection(EXECUTABLE_CI_LABELS)
        or category == "flaky-test" and quarantined and maintenance.get("evidenceComplete") is True
    ):
        return None
    evidence_ids = (
        list(health["evidenceIds"]) if workflow and not explicit
        else list(maintenance["evidenceIds"]) if quarantined and not explicit
        else [evidence_id]
    )
    if not set(evidence_ids).issubset(
        record["id"] for record in records
        if isinstance(record, Mapping) and record.get("availability") == "available"
    ):
        return None
    return {
        "origin": "operator" if explicit else "workflow-health" if workflow else "assessment",
        "intent": "investigate-and-fix",
        "evidenceIds": evidence_ids,
        "quarantine": quarantined or "quarantined-test" in labels,
    }
