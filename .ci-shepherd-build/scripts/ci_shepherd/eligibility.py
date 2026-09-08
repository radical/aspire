from __future__ import annotations

from collections.abc import Mapping


MAX_DELEGATION_REQUESTS = 5


EXECUTABLE_CI_LABELS = frozenset(
    {"automation-broken", "ci-failure-cause", "test-failure"}
)

# These collector stages diagnose CI failures; they do not establish the current
# issue's identity, ownership, authorization, or delegation capacity.
DIAGNOSTIC_COLLECTION_STAGES = frozenset({
    "workflow-run", "workflow-jobs", "workflow-log", "workflow-history",
    "workflow-test-results", "workflow-artifacts", "workflow-annotation",
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


def delegation_readiness(issue: Mapping[str, object], category: str) -> dict[str, object] | None:
    """Readiness to request investigation is not proof of a diagnosis or recovery."""
    explicit = issue.get("delegationRequest") == {"origin": "operator"}
    context = issue.get("delegationContext")
    if isinstance(context, Mapping):
        records = context.get("records", [])
        if not explicit or not records or any(
            (
                record.get("taskObservation") == "available"
                and record.get("taskState") in {"queued", "in_progress"}
            )
            or any(pull.get("state") in {"open", "unknown"} for pull in record.get("pullRequests", []))
            for record in records
        ):
            return None
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
        if latest.get("requiresNewDecision") is not True or not (terminal_pr or ended_without_pr):
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
        or category == "flaky-test" and quarantined and maintenance.get("evidenceComplete") is True
    ):
        return None
    evidence_ids = list(maintenance["evidenceIds"]) if quarantined and not explicit else [evidence_id]
    if not set(evidence_ids).issubset(
        record["id"] for record in records
        if isinstance(record, Mapping) and record.get("availability") == "available"
    ):
        return None
    return {
        "origin": "operator" if explicit else "assessment",
        "intent": "investigate-and-fix",
        "evidenceIds": evidence_ids,
        "quarantine": quarantined or "quarantined-test" in labels,
    }
