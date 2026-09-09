from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import PurePosixPath

from .run_scope import verified_run_scope
from .signals import extract_issue_signals
from .timeutils import parse_aware_iso8601


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
        payload = record.get("payload", record)
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


def workflow_producer_admission(
    payload: Mapping[str, object], records: Sequence[Mapping[str, object]],
    *, repository: str, collected_at: str,
) -> dict[str, object] | None:
    """Recognize a gh-aw report without giving its label comment/closure authority."""
    if payload.get("author") != "github-actions[bot]" or payload.get("authorType") != "Bot":
        return None
    body = payload.get("body")
    if not isinstance(body, str) or type(payload.get("number")) is not int:
        return None
    signals = extract_issue_signals(
        payload["number"], f"issue:{payload['number']}", str(payload.get("url", "")), body, repository,
    )
    # gh-aw emits: <!-- gh-aw-failure-issue: true, workflow_id: ci-health, ... -->.
    # The stable slug must agree with the observed workflow path, not its display name.
    workflow_ids = {
        match.group(1)
        for marker in signals.markers if marker["key"] == "gh-aw-failure-issue"
        and re.match(r"^true(?:,|$)", str(marker.get("raw", "")), re.IGNORECASE)
        for match in [re.search(r"(?:^|,)\s*workflow_id:\s*([A-Za-z0-9_-]+)(?:,|$)", str(marker.get("raw", "")))]
        if match is not None
    }
    if len(workflow_ids) != 1:
        return None
    workflow_slug = next(iter(workflow_ids))
    run_ids = {
        reference["runId"] for reference in signals.references
        if reference.get("targetType") == "workflow-run"
        and str(reference.get("targetRepository", "")).casefold() == repository.casefold()
    }
    now = parse_aware_iso8601(collected_at, "collectedAt")
    matches = []
    for record in records:
        run = record.get("payload", {})
        path = run.get("workflowPath")
        if (
            record.get("kind") != "workflow-run" or record.get("availability") != "available"
            or run.get("runId") not in run_ids or run.get("targetRepository") != repository
            or run.get("status") != "completed" or run.get("conclusion") != "failure"
            or type(run.get("workflowId")) is not int
            or not isinstance(path, str) or PurePosixPath(path).stem.removesuffix(".lock") != workflow_slug
            or not run.get("headSha") or not run.get("event")
            or not (run.get("headBranch") or run.get("branch")) or not run.get("createdAt")
        ):
            continue
        if now - timedelta(days=14) <= parse_aware_iso8601(run["createdAt"], "run.createdAt") <= now:
            matches.append(record)
    if not matches:
        return None
    record = max(matches, key=lambda item: (item["payload"]["createdAt"], item["payload"]["runId"]))
    return {
        "repository": repository, "workflowSlug": workflow_slug,
        "run": {
            "id": f"run:{record['payload']['runId']}",
            **{key: record[key] for key in ("kind", "availability", "payload")},
        },
        "issue": {key: payload[key] for key in ("number", "url", "author", "authorType", "body")},
    }


def repair_priority(issue: Mapping[str, object]) -> dict[str, object]:
    """Order eligible work; priority never establishes eligibility."""
    repair = issue.get("repairEvidence", {})
    health = issue.get("workflowHealth", {})
    maintenance = issue.get("testMaintenance", {})
    current = repair.get("current") is True or health.get("current") is True
    quarantined = (
        maintenance.get("state") == "quarantined" or issue.get("alreadyQuarantined") is True
        or repair.get("quarantinedCoverage") is True
        or health.get("workflowPath") == ".github/workflows/tests-quarantine.yml"
    )
    category = repair.get("category", health.get("category"))
    recurrent = repair.get("recurrent") is True or health.get("recurrent") is True
    kind = (
        "quarantined-test-repair" if quarantined and repair.get("broaderImpact") is not True
        else "current-workflow-break" if current and repair.get("reportingOutage") is True
        else "current-workflow-break" if current and category == "blocking-build"
        else "unquarantined-test-instability" if current and category == "flaky-test"
        else "recurrent-ci-failure" if current and recurrent
        else "automation-defect" if issue.get("producer") in {"gh-aw-failure-issue", "tracking-issue", "ci-health-dashboard"}
        else "recurrent-ci-failure"
    )
    return {
        "kind": kind,
        "rank": {
            "current-workflow-break": 0, "recurrent-ci-failure": 1,
            "unquarantined-test-instability": 2, "automation-defect": 3,
            "quarantined-test-repair": 4,
        }[kind],
        "recurrent": recurrent,
        "lastFailureAt": repair.get("lastFailureAt", health.get("lastFailureAt")) if current else None,
    }


def repair_priority_key(issue: Mapping[str, object]) -> tuple[object, ...]:
    priority = repair_priority(issue)
    observed = priority["lastFailureAt"]
    return (
        priority["rank"], not priority["recurrent"],
        -parse_aware_iso8601(observed, "lastFailureAt").timestamp() if observed else float("inf"),
        issue["issueNumber"],
    )


def delegation_readiness(issue: Mapping[str, object], category: str) -> dict[str, object] | None:
    """Readiness to request investigation is not proof of a diagnosis or recovery."""
    if human_decision_blocks_delegation(issue) or related_repairs_block_delegation(issue):
        return None
    if issue.get("actionCluster", {}).get("role") == "superseded":
        return None
    explicit = issue.get("delegationRequest") == {"origin": "operator"}
    if not explicit and issue.get("investigationResults"):
        return None
    health = issue.get("workflowHealth")
    repair = issue.get("repairEvidence", {})
    workflow = (
        isinstance(health, Mapping) and health.get("current") is True
        and health.get("route") == "delegate-copilot" and health.get("category") == category
        and category in repair.get("allowedCategories", [])
        # A three-in-five health incident remains current after a successful
        # run. Preserve that window policy, but require matching repair subjects
        # rather than the health layer's coarse infrastructure fingerprint.
        and repair.get("lastFailureAt") == health.get("lastFailureAt")
        and (
            repair.get("ready") is True
            or category != "flaky-test" and repair.get("recurrent") is True
        )
    )
    context = issue.get("delegationContext")
    if isinstance(context, Mapping) and (
        not explicit or not delegation_replacement_ready(context.get("records", []))
    ):
        return None
    evidence_id = f"issue:{issue['issueNumber']}"
    records = issue.get("evidenceBundle", issue.get("allowedEvidence", []))
    if any(
        record.get("kind") == "pull-request"
        and record.get("payload", record).get("state") not in {"closed", "merged"}
        and not _verified_execution_source_pull_request(issue, record, records)
        for record in records if isinstance(record, Mapping)
    ):
        return None
    evidence = next(
        (record for record in records if isinstance(record, Mapping) and record.get("id") == evidence_id),
        None,
    )
    if evidence is None or evidence.get("availability") != "available":
        return None
    payload = evidence.get("payload", evidence)
    if not isinstance(payload, Mapping) or payload.get("state") != "open" or payload.get("assignees"):
        return None
    labels = label_names(payload.get("labels")) if isinstance(payload, Mapping) else frozenset()
    maintenance = issue.get("testMaintenance", {})
    quarantined = isinstance(maintenance, Mapping) and maintenance.get("state") == "quarantined"
    repair_ready = (
        repair.get("current") is True and repair.get("ready") is True
        and category in repair.get("allowedCategories", [])
        and not isinstance(health, Mapping)
    )
    producer = issue.get("producerAdmission")
    if not explicit and not (
        repair_ready and (labels.intersection(EXECUTABLE_CI_LABELS) or producer)
        or workflow and (labels.intersection(EXECUTABLE_CI_LABELS) or producer)
        or category == "flaky-test" and quarantined and maintenance.get("evidenceComplete") is True
    ):
        return None
    evidence_ids = (
        sorted(set(health["evidenceIds"]) | set(repair["evidenceIds"])) if workflow and not explicit
        else list(maintenance["evidenceIds"]) if quarantined and not explicit
        else list(repair["evidenceIds"]) if repair_ready and not explicit
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


def _verified_execution_source_pull_request(
    issue: Mapping[str, object], record: Mapping[str, object], records: list[object],
) -> bool:
    """A verified triggering PR is execution context, not an existing repair."""
    if record.get("availability") != "available":
        return False
    payload = record.get("payload", record)
    references = [
        ref for ref in payload.get("referencedBy", [])
        if ref.get("sourceIssueNumber") == issue["issueNumber"]
    ]
    if not references or any(
        ref.get("extractionMethod") not in {"occurrence-pull-request", "triggering-pull-request"}
        for ref in references
    ):
        return False
    if any(link.get("targetNumber") == issue["issueNumber"] for link in payload.get("linkedIssues", [])):
        return False
    for run in records:
        if (
            not isinstance(run, Mapping) or run.get("kind") != "workflow-run"
            or run.get("availability") != "available"
            or run.get("id") not in issue.get("repairEvidence", {}).get("evidenceIds", [])
        ):
            continue
        scope = verified_run_scope(run.get("payload", run))
        if (
            scope.get("kind") == "pull-request"
            and isinstance(payload.get("targetRepository"), str)
            and scope["repository"].casefold() == payload["targetRepository"].casefold()
            and scope.get("pullRequest") == payload.get("number")
        ):
            return True
    return False
