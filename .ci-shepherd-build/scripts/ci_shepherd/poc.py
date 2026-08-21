from __future__ import annotations

import copy
import re
from datetime import datetime
from typing import Any, Mapping, Sequence

from ci_shepherd.models import ValidationError
from ci_shepherd.poc_history import compute_fingerprint, merge_occurrence_dimensions
from ci_shepherd.timeutils import parse_aware_iso8601


ASSESSMENT_SCHEMA_VERSION = 1

CATEGORIES = frozenset(
    {
        "flaky-test",
        "transient-infrastructure",
        "blocking-build",
        "product-or-tooling",
        "automation-tracker",
        "unknown",
    }
)
DISPOSITIONS = frozenset(
    {
        "investigate",
        "watch",
        "ping-human",
        "review-quarantine",
        "review-retry",
        "review-rerun",
        "review-close",
        "no-action",
    }
)
TARGET_KINDS = frozenset({"issue", "test", "failure-fingerprint", "workflow-run"})
CONFIDENCE = frozenset({"high", "medium", "low"})
__all__ = [
    "build_compact_poc_input",
    "merge_ambiguous_poc_judgments",
    "validate_poc_judgments",
    "validate_poc",
]


def validate_poc_judgments(prepared: object, judgments: object) -> None:
    prepared_mapping = _require_mapping(prepared, "prepared assessment")
    judgment_mapping = _require_mapping(judgments, "POC judgment")

    _require_exact_int(judgment_mapping, "schemaVersion", ASSESSMENT_SCHEMA_VERSION)
    _require_only_fields(judgment_mapping, {"schemaVersion", "snapshotId", "issues"}, "POC judgment")

    repository = _require_nonempty_string(prepared_mapping, "repository")
    source_collected_at = _require_nonempty_string(prepared_mapping, "sourceCollectedAt")
    expected_snapshot_id = f"snapshot:{repository}:{source_collected_at}"
    prepared_snapshot_id = _require_nonempty_string(prepared_mapping, "snapshotId")
    if prepared_snapshot_id != expected_snapshot_id:
        raise ValidationError("Prepared snapshotId must match repository and sourceCollectedAt.")

    judgment_snapshot_id = _require_nonempty_string(judgment_mapping, "snapshotId")
    if judgment_snapshot_id != prepared_snapshot_id:
        raise ValidationError("POC snapshotId must match the prepared snapshotId.")

    prepared_issues = _prepared_issues(prepared_mapping)
    issue_judgments = _require_list(judgment_mapping, "issues")
    judgment_issue_numbers: set[int] = set()

    for raw_issue in issue_judgments:
        issue = _require_mapping(raw_issue, "issue judgment")
        _require_only_fields(issue, {"issueNumber", "category", "recommendations"}, "issue judgment")

        issue_number = _require_positive_int(issue, "issueNumber")
        if issue_number in judgment_issue_numbers:
            raise ValidationError(f"Duplicate issue judgment for issue {issue_number}.")
        judgment_issue_numbers.add(issue_number)

        prepared_issue = prepared_issues.get(issue_number)
        if prepared_issue is None:
            raise ValidationError(f"Unexpected issue judgment for non-prepared issue {issue_number}.")

        category = _require_nonempty_string(issue, "category")
        if category not in CATEGORIES:
            raise ValidationError(f"Unsupported issue category: {category}.")

        recommendations = _require_list(issue, "recommendations")
        if not recommendations:
            raise ValidationError(
                f"Issue judgment for issue {issue_number} must include at least one recommendation."
            )

        evidence_bundle_ids = prepared_issue["evidenceBundle"]
        recommendation_targets: set[tuple[str, object]] = set()
        for recommendation in recommendations:
            target = _validate_recommendation(recommendation, issue_number, evidence_bundle_ids)
            if target in recommendation_targets:
                raise ValidationError(
                    f"Duplicate recommendation target for issue {issue_number}: {target[0]}:{target[1]}."
                )
            recommendation_targets.add(target)

    missing_issue_numbers = sorted(set(prepared_issues) - judgment_issue_numbers)
    if missing_issue_numbers:
        raise ValidationError(
            f"Missing issue judgment for prepared issue {missing_issue_numbers[0]}."
        )


def validate_poc(prepared: object, judgment: object) -> None:
    validate_poc_judgments(prepared, judgment)


def merge_ambiguous_poc_judgments(
    compact_input: object,
    agent_judgments: object,
) -> dict[str, Any]:
    compact_mapping = _require_mapping(compact_input, "compact input")
    agent_mapping = _require_mapping(agent_judgments, "agent judgments")
    _require_exact_int(compact_mapping, "schemaVersion", ASSESSMENT_SCHEMA_VERSION)
    _require_exact_int(agent_mapping, "schemaVersion", ASSESSMENT_SCHEMA_VERSION)

    snapshot_id = _require_nonempty_string(compact_mapping, "snapshotId")
    if _require_nonempty_string(agent_mapping, "snapshotId") != snapshot_id:
        raise ValidationError("Agent judgment snapshotId must match compact input.")

    agent_issues: dict[int, Mapping[str, Any]] = {}
    for raw_issue in _require_list(agent_mapping, "issues"):
        issue = _require_mapping(raw_issue, "agent issue judgment")
        issue_number = _require_positive_int(issue, "issueNumber")
        if issue_number in agent_issues:
            raise ValidationError(f"Duplicate agent judgment for issue {issue_number}.")
        agent_issues[issue_number] = issue

    merged_issues: list[dict[str, Any]] = []
    compact_issue_numbers: set[int] = set()
    for raw_issue in _require_list(compact_mapping, "issues"):
        issue = _require_mapping(raw_issue, "compact issue")
        issue_number = _require_positive_int(issue, "issueNumber")
        compact_issue_numbers.add(issue_number)
        agent_issue = agent_issues.get(issue_number)
        if agent_issue is None:
            raise ValidationError(f"Missing agent judgment for issue {issue_number}.")

        default_judgment = _require_mapping(issue.get("defaultJudgment"), "default judgment")
        review_required = issue.get("reviewRequired") is True
        action_context = issue.get("actionCluster")
        superseded = (
            isinstance(action_context, Mapping)
            and action_context.get("role") == "superseded"
        )
        # An agent must never be allowed to escalate to a human when the
        # issue itself does not report a decision requirement -- otherwise an
        # agent could upgrade a deterministic non-human default (e.g.
        # "investigate") to "ping-human" without any actual evidence backing
        # that escalation.
        unauthorized_human_escalation = _agent_recommends_ping_human(
            agent_issue
        ) and not _compact_issue_requires_human_decision(issue)
        selected = (
            agent_issue
            if not superseded
            and not unauthorized_human_escalation
            and (review_required or _is_ambiguous_default(default_judgment))
            else default_judgment
        )
        merged_issues.append(copy.deepcopy(dict(selected)))

    unexpected_issue_numbers = sorted(set(agent_issues) - compact_issue_numbers)
    if unexpected_issue_numbers:
        raise ValidationError(
            f"Unexpected agent judgment for issue {unexpected_issue_numbers[0]}."
        )

    return {
        "schemaVersion": ASSESSMENT_SCHEMA_VERSION,
        "snapshotId": snapshot_id,
        "issues": merged_issues,
    }


def _is_ambiguous_default(default_judgment: Mapping[str, Any]) -> bool:
    if default_judgment.get("category") == "unknown":
        return True
    recommendations = _require_list(default_judgment, "recommendations")
    return any(
        isinstance(recommendation, Mapping)
        and recommendation.get("disposition") == "investigate"
        for recommendation in recommendations
    )


def _agent_recommends_ping_human(agent_issue: Mapping[str, Any]) -> bool:
    recommendations = agent_issue.get("recommendations")
    if not isinstance(recommendations, list):
        return False
    return any(
        isinstance(recommendation, Mapping) and recommendation.get("disposition") == "ping-human"
        for recommendation in recommendations
    )


def _compact_issue_requires_human_decision(issue: Mapping[str, Any]) -> bool:
    human_context = issue.get("humanContext")
    return isinstance(human_context, Mapping) and human_context.get("decisionRequired") is True


def _validate_recommendation(
    recommendation: object,
    issue_number: int,
    bundle: Mapping[str, Any],
) -> tuple[str, object]:
    recommendation_mapping = _require_mapping(recommendation, "recommendation")
    _require_only_fields(
        recommendation_mapping,
        {
            "disposition",
            "target",
            "confidence",
            "summary",
            "evidenceIds",
            "missingEvidence",
            "reassessWhen",
            "humanEscalation",
        },
        "recommendation",
    )

    disposition = _require_nonempty_string(recommendation_mapping, "disposition")
    if disposition not in DISPOSITIONS:
        raise ValidationError(f"Unsupported recommendation disposition: {disposition}.")

    target_mapping = _require_mapping(recommendation_mapping.get("target"), "target")
    _require_only_fields(target_mapping, {"kind", "value"}, "target")

    target_kind = _require_nonempty_string(target_mapping, "kind")
    if target_kind not in TARGET_KINDS:
        raise ValidationError(f"Unsupported target kind: {target_kind}.")

    target_value = target_mapping.get("value")
    if target_kind == "issue":
        if not isinstance(target_value, int) or isinstance(target_value, bool):
            raise ValidationError("Issue targets must use the issue number as a positive integer.")
        if target_value != issue_number:
            raise ValidationError("Issue target value must match issue number.")
        normalized_target_value: object = target_value
    else:
        normalized_target_value = _require_nonempty_string(target_mapping, "value").strip()

    confidence = _require_nonempty_string(recommendation_mapping, "confidence")
    if confidence not in CONFIDENCE:
        raise ValidationError(f"Unsupported confidence: {confidence}.")

    _require_nonempty_string(recommendation_mapping, "summary")
    _require_nonempty_string(recommendation_mapping, "reassessWhen")
    human_escalation = recommendation_mapping.get("humanEscalation")
    if disposition == "ping-human":
        if human_escalation is None:
            raise ValidationError(
                "ping-human recommendations must include humanEscalation."
            )
        _validate_human_escalation(human_escalation)
    elif human_escalation is not None:
        raise ValidationError(
            "humanEscalation is only valid for ping-human recommendations."
        )

    evidence_ids = _require_list(recommendation_mapping, "evidenceIds")
    seen_evidence_ids: set[str] = set()
    bundle_ids = _bundle_ids(bundle)
    for evidence_id in evidence_ids:
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            raise ValidationError("Recommendation evidenceIds must be nonempty strings.")
        if evidence_id in seen_evidence_ids:
            raise ValidationError(f"Recommendation repeats evidenceId {evidence_id}.")
        seen_evidence_ids.add(evidence_id)
        if evidence_id not in bundle_ids:
            raise ValidationError(
                f"Recommendation cites evidence {evidence_id} outside its evidence bundle."
            )

    missing_evidence = _require_list(recommendation_mapping, "missingEvidence")
    for evidence in missing_evidence:
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValidationError("Recommendation missingEvidence must contain nonempty strings.")

    return (target_kind, normalized_target_value)


def _validate_human_escalation(value: object) -> None:
    escalation = _require_mapping(value, "humanEscalation")
    _require_only_fields(
        escalation,
        {
            "context",
            "whyHuman",
            "question",
            "suggestedNextSteps",
            "routingHint",
        },
        "humanEscalation",
    )
    for field in ("context", "whyHuman", "question", "routingHint"):
        _require_nonempty_string(escalation, field)
    steps = _require_list(escalation, "suggestedNextSteps")
    if not steps:
        raise ValidationError(
            "humanEscalation suggestedNextSteps must contain at least one step."
        )
    for step in steps:
        if not isinstance(step, str) or not step.strip():
            raise ValidationError(
                "humanEscalation suggestedNextSteps must contain nonempty strings."
            )


def _prepared_issues(prepared: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    issues = _require_list(prepared, "issues")
    result: dict[int, Mapping[str, Any]] = {}
    for raw_issue in issues:
        issue = _require_mapping(raw_issue, "prepared issue")
        issue_number = _require_positive_int(issue, "issueNumber")
        if issue_number in result:
            raise ValidationError(f"Prepared assessment contains duplicate issue {issue_number}.")
        evidence_bundle = _require_list(issue, "evidenceBundle")
        result[issue_number] = {"evidenceBundle": evidence_bundle}
    return result


def _bundle_ids(bundle: object) -> set[str]:
    ids: set[str] = set()
    for entry in _require_list({"bundle": bundle}, "bundle"):
        item = _require_mapping(entry, "evidence bundle entry")
        evidence_id = _require_nonempty_string(item, "id")
        ids.add(evidence_id)
    return ids


def _require_mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{name} must be an object.")
    return value


def _require_list(mapping: Mapping[str, Any], key: str) -> list[Any]:
    value = mapping.get(key)
    if not isinstance(value, list):
        raise ValidationError(f"{key} must be an array.")
    return value


def _require_nonempty_string(mapping: Mapping[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{key} must be a nonempty string.")
    return value


def _require_positive_int(mapping: Mapping[str, Any], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError(f"{key} must be a positive integer.")
    return value


def _require_nonnegative_int(mapping: Mapping[str, Any], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError(f"{key} must be a non-negative integer.")
    return value


def _require_exact_int(mapping: Mapping[str, Any], key: str, expected: int) -> None:
    value = mapping.get(key)
    if value != expected or not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"{key} must be exactly {expected}.")


def _require_only_fields(
    mapping: Mapping[str, Any],
    allowed_fields: set[str],
    name: str,
) -> None:
    unknown_fields = sorted(set(mapping) - allowed_fields)
    if unknown_fields:
        raise ValidationError(f"{name} contains unknown or forbidden field: {unknown_fields[0]}.")


_DEFAULT_SUMMARY_BY_DISPOSITION = {
    "review-close": "Review this issue for closure.",
    "review-quarantine": "Review this recurrent test for quarantine.",
    "review-retry": "Review this recurrent infrastructure failure for retry.",
    "investigate": "Investigate this issue.",
    "ping-human": "Route this issue to a human.",
    "watch": "Watch this issue.",
    "no-action": "No shepherd action is needed.",
}

_DEFAULT_REASSESS_WHEN_BY_DISPOSITION = {
    "review-close": "After the next positive evidence or human review.",
    "review-quarantine": "After the quarantine review or new recurrence evidence.",
    "review-retry": "After the retry review or new recurrence evidence.",
    "investigate": "After the next evidence update.",
    "ping-human": "After human review.",
    "watch": "When new evidence appears.",
    "no-action": "When automation ownership or blockers change.",
}

_WATCH_SUMMARY_BY_REASON = {
    "single-test-occurrence": (
        "Watch this single-test-occurrence for another independent failure "
        "of the same test on a different day."
    ),
    "same-day-test-recurrence": (
        "Watch this same-day-test-recurrence for a cross-day failure or positive recovery."
    ),
    "subthreshold-test-recurrence": (
        "Watch this subthreshold-test-recurrence for enough cross-day evidence "
        "to review quarantine."
    ),
    "single-infrastructure-occurrence": (
        "Watch this single-infrastructure-occurrence for recurrence or positive recovery."
    ),
    "subthreshold-infrastructure-recurrence": (
        "Watch this subthreshold-infrastructure-recurrence for a third independent "
        "failure or positive recovery."
    ),
    "missing-diagnostic-identity": (
        "Watch this missing-diagnostic-identity until logs or recurrence identify the cause."
    ),
    "insufficient-evidence": (
        "Watch this insufficient-evidence case until a specific failure or recovery is observed."
    ),
}

_WATCH_REASSESS_WHEN_BY_REASON = {
    "single-test-occurrence": (
        "After another independent failure of the same test on a different day "
        "or positive recovery."
    ),
    "same-day-test-recurrence": (
        "After an independent failure on a different day or positive recovery."
    ),
    "subthreshold-test-recurrence": (
        "After another independent cross-day failure or positive recovery."
    ),
    "single-infrastructure-occurrence": (
        "After another independent occurrence or positive recovery."
    ),
    "subthreshold-infrastructure-recurrence": (
        "After a third independent failure or positive recovery."
    ),
    "missing-diagnostic-identity": (
        "After diagnostic logs identify the cause or another independent occurrence."
    ),
    "insufficient-evidence": (
        "After a specific failure signature, recurrence, or positive recovery is observed."
    ),
}


def build_compact_poc_input(
    prepared: object,
    *,
    related_issue_matches: object | None = None,
    history_occurrences: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    prepared_mapping = _require_mapping(prepared, "prepared assessment")
    _require_exact_int(prepared_mapping, "schemaVersion", ASSESSMENT_SCHEMA_VERSION)
    snapshot_id = _require_nonempty_string(prepared_mapping, "snapshotId")
    issues = _require_list(prepared_mapping, "issues")

    prepared_issues: list[Mapping[str, Any]] = []
    seen_issue_numbers: set[int] = set()
    for raw_issue in sorted(issues, key=_prepared_issue_sort_key):
        issue = _require_mapping(raw_issue, "prepared issue")
        issue_number = _require_positive_int(issue, "issueNumber")
        if issue_number in seen_issue_numbers:
            raise ValidationError(f"Prepared assessment contains duplicate issue {issue_number}.")
        seen_issue_numbers.add(issue_number)
        prepared_issues.append(issue)

    relationships, cluster_summaries, cluster_dimensions = _build_relationship_context(
        prepared_issues,
        related_issue_matches=related_issue_matches,
    )
    action_contexts = _build_action_contexts(prepared_issues, relationships)
    history_rows_by_fingerprint = history_occurrences or {}
    compact_issues = [
        _build_compact_issue(
            issue,
            related_issues=relationships[_require_positive_int(issue, "issueNumber")],
            cluster_occurrence_summary=cluster_summaries.get(
                _require_positive_int(issue, "issueNumber")
            ),
            cluster_dimensions=cluster_dimensions.get(
                _require_positive_int(issue, "issueNumber")
            ),
            action_context=action_contexts[_require_positive_int(issue, "issueNumber")],
            history_rows_by_fingerprint=history_rows_by_fingerprint,
        )
        for issue in prepared_issues
    ]

    return {
        "schemaVersion": ASSESSMENT_SCHEMA_VERSION,
        "snapshotId": snapshot_id,
        "issues": compact_issues,
    }


def _build_compact_issue(
    issue: Mapping[str, Any],
    *,
    related_issues: list[dict[str, Any]],
    cluster_occurrence_summary: dict[str, Any] | None,
    cluster_dimensions: Mapping[str, set[Any]] | None,
    action_context: dict[str, Any] | None,
    history_rows_by_fingerprint: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    issue_number = _require_positive_int(issue, "issueNumber")
    title = _require_nonempty_string(issue, "title")
    producer = _require_nonempty_string(issue, "producer")
    autoclose = issue.get("autoclose")
    if autoclose is not None and not isinstance(autoclose, bool):
        raise ValidationError("autoclose must be a boolean or null.")

    ledger = _require_mapping(issue.get("ledger"), "prepared issue ledger")
    occurrence_count = _require_nonnegative_int(ledger, "parsedRowCount")
    own_dimensions = _occurrence_dimensions(ledger)
    ledger_complete = ledger.get("complete") is True
    ledger_recognized = ledger.get("schemaRecognized") is True
    occurrence_summary = _occurrence_summary(
        dates=own_dimensions["dates"],
        source_runs=own_dimensions["sourceRuns"],
        jobs=own_dimensions["jobs"],
        pull_requests=own_dimensions["pullRequests"],
        complete=ledger_complete,
        recognized=ledger_recognized,
    )

    identity = _require_mapping(issue.get("identity"), "prepared issue identity")

    # Cross-snapshot recurrence: an append-only fingerprint ledger records past
    # occurrences by test/error/cause identity, so recurrence evidence survives
    # even after the issue record that observed it has closed. Only the exact
    # fingerprint for *this* issue is used -- no fuzzy relationship broadening.
    fingerprint = compute_fingerprint(identity)
    history_rows = history_rows_by_fingerprint.get(fingerprint, []) if fingerprint else []
    history_dimensions = merge_occurrence_dimensions(own_dimensions, history_rows)
    history_occurrence_summary = _occurrence_summary(
        dates=history_dimensions["dates"],
        source_runs=history_dimensions["sourceRuns"],
        jobs=history_dimensions["jobs"],
        pull_requests=history_dimensions["pullRequests"],
        complete=ledger_complete,
        recognized=ledger_recognized,
    )

    candidate_state = _require_nonempty_string(issue, "candidateState")
    candidate_action = _require_nonempty_string(issue, "candidateAction")
    blockers = _copy_string_list(issue.get("blockers"), "blockers")
    missing_prerequisites = _copy_string_list(issue.get("missingPrerequisites"), "missingPrerequisites")
    resolution_evidence = _require_mapping(issue.get("resolutionEvidence"), "prepared issue resolutionEvidence")
    evidence_bundle = _require_list(issue, "evidenceBundle")
    human_context = _build_human_context(evidence_bundle)
    automation_context = _build_automation_context(issue_number, evidence_bundle)

    allowed_evidence, allowed_evidence_ids = _select_allowed_evidence(evidence_bundle)

    if cluster_occurrence_summary is not None:
        if history_rows and cluster_dimensions is not None:
            # A cluster (fuzzy relationship) still takes precedence over this
            # issue's own ledger, but exact-fingerprint history is merged in
            # rather than discarded so recurrence isn't lost to closure.
            merged_cluster_dimensions = merge_occurrence_dimensions(cluster_dimensions, history_rows)
            effective_occurrence_summary = _occurrence_summary(
                dates=merged_cluster_dimensions["dates"],
                source_runs=merged_cluster_dimensions["sourceRuns"],
                jobs=merged_cluster_dimensions["jobs"],
                pull_requests=merged_cluster_dimensions["pullRequests"],
                complete=cluster_occurrence_summary["ledgerComplete"],
                recognized=cluster_occurrence_summary["schemaRecognized"],
            )
        else:
            effective_occurrence_summary = cluster_occurrence_summary
    else:
        effective_occurrence_summary = history_occurrence_summary
    verification_context = _build_verification_context(
        evidence_bundle,
        last_seen_date=effective_occurrence_summary.get("lastSeenDate"),
    )
    default_judgment = _build_default_judgment(
        issue_number=issue_number,
        title=title,
        producer=producer,
        autoclose=autoclose,
        candidate_action=candidate_action,
        identity=identity,
        occurrence_summary=effective_occurrence_summary,
        missing_prerequisites=missing_prerequisites,
        resolution_evidence=resolution_evidence,
        allowed_evidence_ids=allowed_evidence_ids,
        human_context=human_context,
        verification_context=verification_context,
    )
    _apply_superseded_default(default_judgment, issue_number, action_context)
    _apply_canonical_cluster_summary(default_judgment, action_context)
    watch_reason = _watch_reason(default_judgment, effective_occurrence_summary)
    _apply_watch_explanation(default_judgment, watch_reason)
    review_required = _review_required(
        default_judgment=default_judgment,
        candidate_action=candidate_action,
        occurrence_summary=effective_occurrence_summary,
        related_issues=related_issues,
        watch_reason=watch_reason,
    )

    compact_issue = {
        "issueNumber": issue_number,
        "title": title,
        "producer": producer,
        "autoclose": autoclose,
        "occurrenceCount": occurrence_count,
        "occurrenceSummary": occurrence_summary,
        "clusterOccurrenceSummary": cluster_occurrence_summary,
        "historyOccurrenceSummary": history_occurrence_summary,
        "identity": dict(identity),
        "relatedIssues": related_issues,
        "reviewRequired": review_required,
        "watchReason": watch_reason,
        "humanContext": human_context,
        "automationContext": automation_context,
        "candidateState": candidate_state,
        "candidateAction": candidate_action,
        "blockers": blockers,
        "missingPrerequisites": missing_prerequisites,
        "resolutionEvidence": dict(resolution_evidence),
        "allowedEvidence": allowed_evidence,
        "defaultJudgment": default_judgment,
    }
    if action_context is not None:
        compact_issue["actionCluster"] = action_context
    return compact_issue


def _build_action_contexts(
    issues: list[Mapping[str, Any]],
    related_issues: Mapping[int, list[dict[str, Any]]],
) -> dict[int, dict[str, Any] | None]:
    issue_by_number = {
        _require_positive_int(issue, "issueNumber"): issue for issue in issues
    }
    contexts: dict[int, dict[str, Any] | None] = {
        issue_number: None for issue_number in issue_by_number
    }

    for relationship in (
        "same-workflow-failure",
        "same-test",
        "same-error-code",
    ):
        adjacency: dict[int, set[int]] = {
            issue_number: set() for issue_number in issue_by_number
        }
        for issue_number, entries in related_issues.items():
            for entry in entries:
                if entry.get("relationship") != relationship:
                    continue
                related_number = entry.get("issueNumber")
                if (
                    not isinstance(related_number, int)
                    or isinstance(related_number, bool)
                    or related_number not in issue_by_number
                    or not _action_pair_compatible(
                        issue_by_number[issue_number],
                        issue_by_number[related_number],
                        relationship,
                    )
                ):
                    continue
                adjacency[issue_number].add(related_number)
                adjacency[related_number].add(issue_number)

        visited: set[int] = set()
        for issue_number in sorted(issue_by_number):
            if issue_number in visited or contexts[issue_number] is not None:
                continue
            component: set[int] = set()
            pending = [issue_number]
            while pending:
                current = pending.pop()
                if current in component or contexts[current] is not None:
                    continue
                component.add(current)
                pending.extend(adjacency[current])
            visited.update(component)
            if len(component) < 2:
                continue

            members = sorted(component)
            canonical = max(members) if relationship == "same-workflow-failure" else min(members)
            for member in members:
                contexts[member] = {
                    "canonicalIssueNumber": canonical,
                    "memberIssueNumbers": members,
                    "relationship": relationship,
                    "role": "canonical" if member == canonical else "superseded",
                }

    return contexts


def _action_pair_compatible(
    issue: Mapping[str, Any],
    related_issue: Mapping[str, Any],
    relationship: str,
) -> bool:
    if relationship != "same-workflow-failure":
        return True
    return _workflow_action_signature(issue) == _workflow_action_signature(related_issue)


def _workflow_action_signature(issue: Mapping[str, Any]) -> str:
    title = issue.get("title")
    if not isinstance(title, str):
        return ""
    return " ".join(title.lower().split())


def _apply_superseded_default(
    default_judgment: dict[str, Any],
    issue_number: int,
    action_context: Mapping[str, Any] | None,
) -> None:
    if action_context is None or action_context.get("role") != "superseded":
        return
    canonical_issue_number = _require_positive_int(
        action_context,
        "canonicalIssueNumber",
    )
    default_judgment["recommendations"] = [
        {
            "disposition": "review-close",
            "target": {"kind": "issue", "value": issue_number},
            "confidence": "medium",
            "summary": (
                f"Review closure as a superseded duplicate of canonical issue "
                f"#{canonical_issue_number}."
            ),
            "evidenceIds": [f"issue:{issue_number}"],
            "missingEvidence": [],
            "reassessWhen": (
                f"If canonical issue #{canonical_issue_number} is closed without "
                "resolving the shared failure target."
            ),
        }
    ]


def _apply_canonical_cluster_summary(
    default_judgment: dict[str, Any],
    action_context: Mapping[str, Any] | None,
) -> None:
    if action_context is None or action_context.get("role") != "canonical":
        return
    canonical_issue_number = _require_positive_int(
        action_context,
        "canonicalIssueNumber",
    )
    members: list[int] = []
    for issue_number in _require_list(action_context, "memberIssueNumbers"):
        if (
            not isinstance(issue_number, int)
            or isinstance(issue_number, bool)
            or issue_number <= 0
        ):
            raise ValidationError("memberIssueNumbers must contain positive integers.")
        if issue_number != canonical_issue_number:
            members.append(issue_number)
    if not members:
        return

    relationship = _require_nonempty_string(action_context, "relationship")
    recommendation = _require_mapping(
        _require_list(default_judgment, "recommendations")[0],
        "default recommendation",
    )
    member_text = ", ".join(f"#{issue_number}" for issue_number in members)
    if relationship == "same-test":
        recommendation["summary"] = (
            "Review this recurrent test once for quarantine; superseded issue "
            f"records {member_text} track the same test."
        )
    elif relationship == "same-error-code":
        recommendation["summary"] = (
            "Review this shared infrastructure fingerprint once for retry; "
            f"superseded issue records {member_text} track the same failure."
        )
    elif relationship == "same-workflow-failure":
        recommendation["summary"] = (
            "Investigate this repeated workflow failure as the canonical owner; "
            f"superseded issue records {member_text} track the same failure shape."
        )


def _build_relationship_context(
    issues: list[Mapping[str, Any]],
    *,
    related_issue_matches: object | None,
) -> tuple[
    dict[int, list[dict[str, Any]]],
    dict[int, dict[str, Any]],
    dict[int, dict[str, set[Any]]],
]:
    relationships: dict[int, dict[int, str]] = {
        _require_positive_int(issue, "issueNumber"): {} for issue in issues
    }
    issue_by_number = {
        _require_positive_int(issue, "issueNumber"): issue for issue in issues
    }
    error_code_groups: dict[str, list[int]] = {}
    test_groups: dict[str, list[int]] = {}
    workflow_groups: dict[str, list[int]] = {}

    for issue_number, issue in issue_by_number.items():
        identity = _require_mapping(issue.get("identity"), "prepared issue identity")
        test_name = identity.get("tier2TestName")
        if isinstance(test_name, str) and test_name.strip():
            test_groups.setdefault(test_name.strip().lower(), []).append(issue_number)

        for error_code in _normalized_error_codes(issue):
            error_code_groups.setdefault(error_code, []).append(issue_number)
        for workflow_identity in _workflow_failure_identities(issue):
            workflow_groups.setdefault(workflow_identity, []).append(issue_number)

    for group in test_groups.values():
        unique_group = sorted(set(group))
        for index, issue_number in enumerate(unique_group):
            for related_number in unique_group[index + 1 :]:
                relationship = (
                    "same-test"
                    if _symptoms_compatible(
                        issue_by_number[issue_number],
                        issue_by_number[related_number],
                    )
                    else "same-test-different-symptom"
                )
                _link_relationship_pair(
                    relationships,
                    issue_number,
                    related_number,
                    relationship,
                )
    for group in error_code_groups.values():
        _link_relationship_group(relationships, group, "same-error-code")
    for group in workflow_groups.values():
        _link_relationship_group(relationships, group, "same-workflow-failure")
    issue_numbers = sorted(issue_by_number)
    for index, issue_number in enumerate(issue_numbers):
        for related_number in issue_numbers[index + 1 :]:
            if _same_cause_family(
                issue_by_number[issue_number],
                issue_by_number[related_number],
            ):
                _link_relationship_pair(
                    relationships,
                    issue_number,
                    related_number,
                    "same-cause-family",
                )

    related_issues: dict[int, list[dict[str, Any]]] = {
        issue_number: [
            {"issueNumber": related_number, "relationship": relationship}
            for related_number, relationship in sorted(related.items())
        ]
        for issue_number, related in relationships.items()
    }
    _merge_frozen_related_issue_matches(
        related_issues,
        issue_by_number,
        related_issue_matches,
    )

    cluster_summaries: dict[int, dict[str, Any]] = {}
    cluster_dimensions: dict[int, dict[str, set[Any]]] = {}
    for issue_number in issue_by_number:
        cluster = _relationship_cluster(
            issue_number,
            relationships,
            issue_by_number,
        )
        if len(cluster) > 1:
            cluster_issues = [issue_by_number[number] for number in sorted(cluster)]
            dimensions = _collect_cluster_dimensions(cluster_issues)
            cluster_dimensions[issue_number] = dimensions
            cluster_summaries[issue_number] = _build_cluster_occurrence_summary(cluster_issues)

    return related_issues, cluster_summaries, cluster_dimensions


def _build_automation_context(
    issue_number: int,
    evidence_bundle: list[Any],
) -> dict[str, Any] | None:
    context: dict[str, Any] = {}
    run_ids: set[int] = set()
    run_summaries: list[dict[str, Any]] = []

    for raw_evidence in evidence_bundle:
        evidence = _require_mapping(raw_evidence, "prepared evidence bundle entry")
        kind = evidence.get("kind")
        payload = evidence.get("payload")
        if not isinstance(payload, Mapping):
            continue

        if kind == "workflow-run":
            run_id = payload.get("runId")
            if isinstance(run_id, int) and not isinstance(run_id, bool) and run_id > 0:
                run_ids.add(run_id)
                summary = {"runId": run_id}
                for field in ("status", "conclusion", "createdAt", "updatedAt"):
                    value = payload.get(field)
                    if isinstance(value, str) and value:
                        summary[field] = value
                if len(summary) > 1:
                    run_summaries.append(summary)
            continue

        if kind != "issue-event" or payload.get("number") != issue_number:
            continue

        for field in ("author", "state", "createdAt", "updatedAt", "closedAt"):
            value = payload.get(field)
            if isinstance(value, str) and value:
                context[field] = value
            elif field == "closedAt" and value is None:
                context[field] = None

        dashboard_context = payload.get("dashboardContext")
        if isinstance(dashboard_context, Mapping):
            context["dashboardContext"] = copy.deepcopy(dict(dashboard_context))

        markers = payload.get("markers")
        if not isinstance(markers, list):
            continue
        for raw_marker in markers:
            if not isinstance(raw_marker, Mapping):
                continue
            key = raw_marker.get("key")
            normalized = raw_marker.get("normalized")
            if not isinstance(normalized, str) or not normalized.strip():
                continue
            if key == "gh-aw-agentic-workflow":
                workflow_name = " ".join(
                    normalized.split(",", 1)[0].strip().lower().split()
                )
                if workflow_name:
                    context["workflowName"] = workflow_name
                workflow_id = re.search(
                    r"(?i)(?:^|,\s*)workflow_id:\s*([^,\s]+)",
                    normalized,
                )
                if workflow_id is not None:
                    context["workflowId"] = workflow_id.group(1).lower()
                run_match = re.search(r"/actions/runs/([1-9][0-9]*)", normalized)
                if run_match is not None:
                    run_ids.add(int(run_match.group(1)))
            elif key == "gh-aw-failure-issue":
                workflow_id = re.search(
                    r"(?i)(?:^|,\s*)workflow_id:\s*([^,\s]+)",
                    normalized,
                )
                if workflow_id is not None:
                    context["workflowId"] = workflow_id.group(1).lower()
                categories = re.search(
                    r"(?i)(?:^|,\s*)failure_categories:\s*([^,]+)",
                    normalized,
                )
                if categories is not None:
                    context["failureCategories"] = sorted(
                        {
                            value.strip().lower()
                            for value in categories.group(1).split()
                            if value.strip()
                        }
                    )
            elif key == "gh-aw-expires":
                context["expiresAt"] = normalized

    if run_ids:
        context["runIds"] = sorted(run_ids)
    if run_summaries:
        context["runSummaries"] = sorted(run_summaries, key=lambda item: item["runId"])
    return context or None


def _merge_frozen_related_issue_matches(
    related_issues: dict[int, list[dict[str, Any]]],
    issue_by_number: Mapping[int, Mapping[str, Any]],
    related_issue_matches: object | None,
) -> None:
    if related_issue_matches is None:
        return
    if not isinstance(related_issue_matches, list):
        raise ValidationError("Frozen related issue matches must be an array.")

    for raw_match in related_issue_matches:
        match = _require_mapping(raw_match, "frozen related issue match")
        source = _require_positive_int(match, "source")
        source_issue = issue_by_number.get(source)
        if source_issue is None:
            continue

        test_name = _require_nonempty_string(match, "test").lower()
        identity = _require_mapping(source_issue.get("identity"), "prepared issue identity")
        source_test_name = identity.get("tier2TestName")
        if not isinstance(source_test_name, str) or source_test_name.lower() != test_name:
            raise ValidationError(
                f"Frozen related issue match for issue {source} does not match its canonical test."
            )

        hits = _require_list(match, "hits")
        by_issue_number = {
            entry["issueNumber"]: entry for entry in related_issues[source]
        }
        for raw_hit in hits:
            hit = _require_mapping(raw_hit, "frozen related issue hit")
            related_number = _require_positive_int(hit, "number")
            if related_number == source:
                continue
            title = _require_nonempty_string(hit, "title")
            state = _require_nonempty_string(hit, "state").lower()
            if state not in {"open", "closed"}:
                raise ValidationError("Frozen related issue state must be open or closed.")
            labels = _related_issue_labels(hit)
            relationship = _external_test_relationship(title, labels, test_name)
            if relationship is None:
                continue

            entry = by_issue_number.get(related_number)
            if entry is None:
                entry = {
                    "issueNumber": related_number,
                    "relationship": relationship,
                }
                related_issues[source].append(entry)
                by_issue_number[related_number] = entry
            entry.update(
                {
                    "state": state,
                    "labels": labels,
                    "title": title[:200],
                }
            )

        related_issues[source].sort(key=lambda entry: entry["issueNumber"])


def _related_issue_labels(hit: Mapping[str, Any]) -> list[str]:
    labels_value = hit.get("labels")
    if labels_value is None:
        return []
    labels = _require_mapping(labels_value, "frozen related issue labels")
    nodes = _require_list(labels, "nodes")
    result: set[str] = set()
    for raw_node in nodes:
        node = _require_mapping(raw_node, "frozen related issue label")
        result.add(_require_nonempty_string(node, "name").lower())
    return sorted(result)


def _workflow_failure_identities(issue: Mapping[str, Any]) -> set[str]:
    identities: set[str] = set()
    producer = issue.get("producer")
    title = issue.get("title")
    if producer == "ci-health-dashboard" and isinstance(title, str):
        match = re.fullmatch(r'(?i)CI lane "(.+)" red', title.strip())
        if match is not None:
            identities.add(f"name:{' '.join(match.group(1).lower().split())}")

    evidence_bundle = issue.get("evidenceBundle")
    if not isinstance(evidence_bundle, list):
        return identities

    for raw_evidence in evidence_bundle:
        if not isinstance(raw_evidence, Mapping) or raw_evidence.get("kind") != "issue-event":
            continue
        payload = raw_evidence.get("payload")
        if not isinstance(payload, Mapping):
            continue
        markers = payload.get("markers")
        if not isinstance(markers, list):
            continue
        for raw_marker in markers:
            if not isinstance(raw_marker, Mapping):
                continue
            key = raw_marker.get("key")
            normalized = raw_marker.get("normalized")
            if not isinstance(normalized, str) or not normalized.strip():
                continue
            if key == "gh-aw-failure-issue":
                match = re.search(r"(?i)(?:^|,\s*)workflow_id:\s*([^,\s]+)", normalized)
                if match is not None:
                    identities.add(f"id:{match.group(1).lower()}")
            elif key == "gh-aw-agentic-workflow":
                workflow_name = " ".join(
                    normalized.split(",", 1)[0].strip().lower().split()
                )
                if workflow_name:
                    identities.add(f"name:{workflow_name}")
    return identities


def _external_test_relationship(
    title: str,
    labels: list[str],
    canonical_test_name: str,
) -> str | None:
    label_set = set(labels)
    if "ci-failure-cause" in label_set:
        return "same-test-ci-issue"
    if "failing-test" in label_set:
        return "same-test-tracker"
    if label_set & {"disabled-tests", "quarantined-test"}:
        return "same-test-history"

    test_leaf = canonical_test_name.rsplit(".", 1)[-1].lower()
    normalized_title = title.lower().replace("\\_", "_")
    if test_leaf in normalized_title and any(
        token in normalized_title for token in ("quarantine", "failing test", "disabled")
    ):
        return "same-test-history"
    return None


def _link_relationship_group(
    relationships: dict[int, dict[int, str]],
    group: list[int],
    relationship: str,
) -> None:
    unique_group = sorted(set(group))
    if len(unique_group) < 2:
        return
    for issue_number in unique_group:
        for related_number in unique_group:
            if issue_number == related_number:
                continue
            _set_relationship(
                relationships,
                issue_number,
                related_number,
                relationship,
            )


def _link_relationship_pair(
    relationships: dict[int, dict[int, str]],
    issue_number: int,
    related_number: int,
    relationship: str,
) -> None:
    _set_relationship(relationships, issue_number, related_number, relationship)
    _set_relationship(relationships, related_number, issue_number, relationship)


def _set_relationship(
    relationships: dict[int, dict[int, str]],
    issue_number: int,
    related_number: int,
    relationship: str,
) -> None:
    priority = {
        "same-cause-family": 1,
        "same-test-different-symptom": 2,
        "same-workflow-failure": 3,
        "same-error-code": 4,
        "same-test": 5,
    }
    existing = relationships[issue_number].get(related_number)
    if existing is None or priority[relationship] > priority[existing]:
        relationships[issue_number][related_number] = relationship


def _relationship_cluster(
    issue_number: int,
    relationships: Mapping[int, Mapping[int, str]],
    issue_by_number: Mapping[int, Mapping[str, Any]],
) -> set[int]:
    cluster: set[int] = set()
    pending = [issue_number]
    while pending:
        current = pending.pop()
        if current in cluster:
            continue
        cluster.add(current)
        pending.extend(
            related_number
            for related_number, relationship in relationships[current].items()
            if relationship in {"same-test", "same-error-code", "same-workflow-failure"}
            and _action_pair_compatible(
                issue_by_number[current],
                issue_by_number[related_number],
                relationship,
            )
        )
    return cluster


_CAUSE_TOKEN_STOP_WORDS = frozenset(
    {
        "ci",
        "failure",
        "flaky",
        "hosting",
        "job",
        "linux",
        "macos",
        "runner",
        "test",
        "tests",
        "ubuntu",
        "windows",
    }
)


def _cause_tokens(issue: Mapping[str, Any]) -> set[str]:
    identity = _require_mapping(issue.get("identity"), "prepared issue identity")
    cause_id = identity.get("tier1CauseId")
    if not isinstance(cause_id, str):
        return set()
    return {
        token
        for token in re.findall(r"[a-z0-9]+", cause_id.lower())
        if len(token) > 2 and token not in _CAUSE_TOKEN_STOP_WORDS
    }


def _symptoms_compatible(
    issue: Mapping[str, Any],
    related_issue: Mapping[str, Any],
) -> bool:
    identity = _require_mapping(issue.get("identity"), "prepared issue identity")
    related_identity = _require_mapping(
        related_issue.get("identity"),
        "prepared issue identity",
    )
    exception_type = identity.get("tier2ExceptionType")
    related_exception_type = related_identity.get("tier2ExceptionType")
    if (
        isinstance(exception_type, str)
        and exception_type
        and isinstance(related_exception_type, str)
        and related_exception_type
        and exception_type.lower() != related_exception_type.lower()
    ):
        return False

    error_codes = _normalized_error_codes(issue)
    related_error_codes = _normalized_error_codes(related_issue)
    if error_codes and related_error_codes and error_codes.isdisjoint(related_error_codes):
        return False

    tokens = _cause_tokens(issue)
    related_tokens = _cause_tokens(related_issue)
    if tokens and related_tokens:
        overlap = tokens & related_tokens
        return len(overlap) >= 2 or len(overlap) / len(tokens | related_tokens) >= 0.5

    return bool(
        isinstance(exception_type, str)
        and exception_type
        and isinstance(related_exception_type, str)
        and related_exception_type
    )


def _same_cause_family(
    issue: Mapping[str, Any],
    related_issue: Mapping[str, Any],
) -> bool:
    tokens = _cause_tokens(issue)
    related_tokens = _cause_tokens(related_issue)
    if not tokens or not related_tokens:
        return False
    overlap = tokens & related_tokens
    return len(overlap) >= 3 and len(overlap) / len(tokens | related_tokens) >= 0.6


def _normalized_error_codes(issue: Mapping[str, Any]) -> set[str]:
    identity = _require_mapping(issue.get("identity"), "prepared issue identity")
    values = [_require_nonempty_string(issue, "title")]
    values.extend(
        value
        for value in identity.values()
        if isinstance(value, str) and value.strip()
    )
    text = " ".join(values).lower()
    codes = set(re.findall(r"0x[0-9a-f]{8}", text))
    for match in re.findall(r"(?<!\d)-\d{9,}(?!\d)", text):
        value = int(match)
        if -(2**31) <= value < 0:
            codes.add(f"0x{value & 0xFFFFFFFF:08x}")
    return codes


def _build_cluster_occurrence_summary(
    issues: list[Mapping[str, Any]],
) -> dict[str, Any]:
    dimensions = _collect_cluster_dimensions(issues)
    complete = all(
        _require_mapping(issue.get("ledger"), "prepared issue ledger").get("complete") is True
        for issue in issues
    )
    recognized = all(
        _require_mapping(issue.get("ledger"), "prepared issue ledger").get("schemaRecognized") is True
        for issue in issues
    )

    return _occurrence_summary(
        dates=dimensions["dates"],
        source_runs=dimensions["sourceRuns"],
        jobs=dimensions["jobs"],
        pull_requests=dimensions["pullRequests"],
        complete=complete,
        recognized=recognized,
    )


def _collect_cluster_dimensions(
    issues: list[Mapping[str, Any]],
) -> dict[str, set[Any]]:
    dates: set[str] = set()
    source_runs: set[int] = set()
    jobs: set[str] = set()
    pull_requests: set[int] = set()

    for issue in issues:
        ledger = _require_mapping(issue.get("ledger"), "prepared issue ledger")
        dimensions = _occurrence_dimensions(ledger)
        dates.update(dimensions["dates"])
        source_runs.update(dimensions["sourceRuns"])
        jobs.update(dimensions["jobs"])
        pull_requests.update(dimensions["pullRequests"])

    return {
        "dates": dates,
        "sourceRuns": source_runs,
        "jobs": jobs,
        "pullRequests": pull_requests,
    }


def _build_human_context(
    evidence_bundle: list[Any],
) -> dict[str, Any] | None:
    for raw_evidence in evidence_bundle:
        evidence = _require_mapping(raw_evidence, "prepared evidence")
        if evidence.get("kind") != "issue-event":
            continue
        payload = _require_mapping(evidence.get("payload"), "prepared evidence payload")
        dashboard_context_value = payload.get("dashboardContext")
        if isinstance(dashboard_context_value, Mapping):
            assessment = dashboard_context_value.get("reportedAssessment")
            suggestion = dashboard_context_value.get("reportedSuggested")
            labels_value = payload.get("labels", [])
            labels = (
                sorted(
                    label.lower()
                    for label in labels_value
                    if isinstance(label, str) and label.strip()
                )
                if isinstance(labels_value, list)
                else []
            )
            return {
                "reportedAssessment": assessment,
                "reportedSuggestion": suggestion,
                "streak": dashboard_context_value.get("streak"),
                "lastRunUrl": dashboard_context_value.get("lastRunUrl"),
                "labels": labels,
                "mentions": dashboard_context_value.get("mentions", []),
                "decisionRequired": _reported_issue_requires_human_decision(
                    assessment if isinstance(assessment, str) else None,
                    suggestion if isinstance(suggestion, str) else None,
                ),
            }
        body = payload.get("body")
        if not isinstance(body, str) or not body.strip():
            return None

        assessment = _issue_body_field(body, "Assessment")
        suggestion = _issue_body_field(body, "Suggested")
        labels_value = payload.get("labels", [])
        labels = (
            sorted(
                label.lower()
                for label in labels_value
                if isinstance(label, str) and label.strip()
            )
            if isinstance(labels_value, list)
            else []
        )
        streak_match = re.search(r"(?i)\bstreak\s+(\d+)\b", body)
        run_match = re.search(
            r"https://github\.com/[^/\s]+/[^/\s]+/actions/runs/\d+",
            body,
        )
        mentions = sorted(set(re.findall(r"(?<![\w-])@[A-Za-z0-9-]+", body)))
        return {
            "reportedAssessment": assessment,
            "reportedSuggestion": suggestion,
            "streak": int(streak_match.group(1)) if streak_match else None,
            "lastRunUrl": run_match.group(0) if run_match else None,
            "labels": labels,
            "mentions": mentions,
            "decisionRequired": _reported_issue_requires_human_decision(
                assessment,
                suggestion,
            ),
        }
    return None


def _issue_body_field(body: str, field: str) -> str | None:
    match = re.search(rf"(?im)^-\s*{re.escape(field)}:\s*(.+)$", body)
    return match.group(1).strip() if match else None


# This POC only has a grounded known decision gate for Azure tenant/workflow
# identity failures (see the "Deployment Environment Cleanup" fixture in
# test_poc.py). Generic words like "renew", "replace", "credential", or
# "permission" show up in unrelated issues too (an ordinary secret rotation,
# a yanked package, a permissions bug) and must not turn those into a human
# escalation on their own. Require one of the specific Azure tenant/identity
# evidence shapes actually seen in reported assessments/suggestions instead.
_HUMAN_DECISION_EVIDENCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "tenant ... is expired" / "the expired ... tenant" (either order).
    re.compile(r"\btenant\b[^.]{0,80}\bexpired\b"),
    re.compile(r"\bexpired\b[^.]{0,80}\btenant\b"),
    # AADSTS5000229 is the Azure AD STS error code for a disabled/expired
    # tenant, so its presence alongside "tenant" is unambiguous evidence.
    re.compile(r"\baadsts5000229\b"),
    # Migrating away from the current service-principal identity (either
    # phrase order).
    re.compile(r"\bservice[\s-]principal\b[^.]{0,80}\bidentity migration\b"),
    re.compile(r"\bidentity migration\b[^.]{0,80}\bservice[\s-]principal\b"),
)


def _reported_issue_requires_human_decision(
    assessment: str | None,
    suggestion: str | None,
) -> bool:
    if assessment is None or suggestion is None:
        return False
    text = f"{assessment} {suggestion}".lower()
    return any(pattern.search(text) for pattern in _HUMAN_DECISION_EVIDENCE_PATTERNS)


def _latest_referenced_failed_run_instant(evidence_bundle: Sequence[Any]) -> datetime | None:
    """Return the latest directly referenced failed workflow-run instant, if any.

    The occurrence ledger's ``lastSeenDate`` is date-only, so comparing a
    success's timestamp against it cannot distinguish a same-day recovery
    from a same-day recurrence. When a failed run is directly available in
    the issue's own evidence bundle, its own timestamp is a precise
    lower bound -- comparing full instants against it is what lets a
    same-day recovery close instead of being rejected outright.
    """
    latest: datetime | None = None
    for raw_evidence in evidence_bundle:
        if not isinstance(raw_evidence, Mapping):
            continue
        if raw_evidence.get("availability") != "available":
            continue
        if raw_evidence.get("kind") != "workflow-run":
            continue
        payload = raw_evidence.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if payload.get("conclusion") != "failure":
            continue
        created_at = payload.get("createdAt")
        if not isinstance(created_at, str) or not created_at.strip():
            continue
        try:
            instant = parse_aware_iso8601(created_at, "workflow-run createdAt")
        except ValueError:
            continue
        if latest is None or instant > latest:
            latest = instant
    return latest


def _build_verification_context(
    evidence_bundle: list[Any],
    *,
    last_seen_date: str | None,
) -> dict[str, Any]:
    """Surface bounded, issue-scoped recovery facts from this issue's own evidence.

    This only reports what is directly in the issue's own evidence bundle --
    the evidence bundle is already issue-scoped, so any pull-request or
    workflow-run record here is one the issue itself references (for example,
    a PR the body says fixed the failure, or a run URL the body links to).

    It does NOT infer root cause or claim a merged pull request is "the fix":
    merged pull requests are reported as context only. A later directly
    referenced successful run on ``main`` is reported separately and can, on
    its own, support a recovered one-off closure -- independent of whether a
    merged PR can be proven to be the actual fix.
    """
    merged_pull_requests: list[dict[str, Any]] = []
    later_successful_runs: list[dict[str, Any]] = []
    latest_failed_run_instant = _latest_referenced_failed_run_instant(evidence_bundle)
    for raw_evidence in evidence_bundle:
        evidence = _require_mapping(raw_evidence, "prepared evidence")
        if evidence.get("availability") != "available":
            continue
        payload = evidence.get("payload")
        if not isinstance(payload, Mapping):
            continue
        evidence_id = evidence.get("id")
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            continue

        kind = evidence.get("kind")
        if kind == "pull-request":
            merged_at = payload.get("mergedAt")
            if isinstance(merged_at, str) and merged_at.strip():
                merged_pull_requests.append({"evidenceId": evidence_id, "mergedAt": merged_at})
        elif kind == "workflow-run":
            # "Later than the issue's last occurrence" is only meaningful when a
            # baseline is known -- an issue with no recorded occurrence and no
            # directly referenced failed run has no baseline to compare a run's
            # timing against.
            if last_seen_date is None and latest_failed_run_instant is None:
                continue
            if payload.get("conclusion") != "success":
                continue
            head_branch = payload.get("headBranch")
            created_at = payload.get("createdAt")
            if head_branch != "main" or not isinstance(created_at, str) or not created_at.strip():
                continue
            try:
                created_at_instant = parse_aware_iso8601(created_at, "workflow-run createdAt")
            except ValueError:
                continue
            success_date = created_at_instant.date().isoformat()

            # The ledger's lastSeenDate and a directly referenced failed run's
            # own instant are two independent signals -- neither is allowed to
            # override the other. The ledger can record a later occurrence
            # date than any run this issue happens to have expanded into full
            # evidence (for example, a later ledger row whose run was never
            # directly referenced), so an instant comparison against an
            # *older* expanded failed run must never be used to paper over a
            # later, unexpanded ledger occurrence.
            if last_seen_date is not None and success_date < last_seen_date:
                continue

            if last_seen_date is not None and success_date == last_seen_date:
                # A same-day recovery can only be proven with full timestamps
                # when the latest expanded failed run is itself on that same
                # lastSeenDate -- that is the only case where the failed run's
                # own instant is a precise lower bound for *this* occurrence.
                # Otherwise there is no precise same-day baseline to compare
                # against, and a date-only match is not enough evidence of
                # recovery.
                if (
                    latest_failed_run_instant is None
                    or latest_failed_run_instant.date().isoformat() != last_seen_date
                ):
                    continue
                if created_at_instant <= latest_failed_run_instant:
                    continue
            elif latest_failed_run_instant is not None:
                # Even when the success is on a calendar day later than the
                # ledger's lastSeenDate, it must still be later than any
                # directly referenced failed run's own instant.
                if created_at_instant <= latest_failed_run_instant:
                    continue
            later_successful_runs.append(
                {"evidenceId": evidence_id, "headBranch": head_branch, "createdAt": created_at}
            )

    merged_pull_requests.sort(key=lambda item: item["mergedAt"])
    later_successful_runs.sort(key=lambda item: item["createdAt"])
    return {
        "issueScopedMergedPullRequests": merged_pull_requests,
        "laterSuccessfulRuns": later_successful_runs,
    }


def _recovered_run_evidence_id(verification_context: Mapping[str, Any] | None) -> str | None:
    """Return the earliest directly referenced later successful run, if any.

    The earliest run after the last occurrence is the most direct recovery
    evidence -- it is the first positive signal after the failure, rather than
    an arbitrarily later success that could be explained by unrelated changes.
    """
    if not verification_context:
        return None
    later_successful_runs = verification_context.get("laterSuccessfulRuns")
    if not isinstance(later_successful_runs, list) or not later_successful_runs:
        return None
    first = later_successful_runs[0]
    evidence_id = first.get("evidenceId") if isinstance(first, Mapping) else None
    return evidence_id if isinstance(evidence_id, str) and evidence_id.strip() else None


def _occurrence_dimensions(ledger: Mapping[str, Any]) -> dict[str, set[Any]]:
    rows_value = ledger.get("rows", [])
    if not isinstance(rows_value, list):
        raise ValidationError("rows must be an array.")
    rows = rows_value
    dates: set[str] = set()
    source_runs: set[int] = set()
    jobs: set[str] = set()
    pull_requests: set[int] = set()

    for raw_row in rows:
        row = _require_mapping(raw_row, "prepared occurrence row")
        date = row.get("date")
        if date is None:
            created_at = row.get("createdAt")
            if isinstance(created_at, str) and len(created_at) >= 10:
                date = created_at[:10]
        if date is not None:
            if not isinstance(date, str) or not date.strip():
                raise ValidationError("Prepared occurrence date must be a nonempty string.")
            dates.add(date)

        source_run = row.get("sourceRun", row.get("runId"))
        if source_run is not None:
            if not isinstance(source_run, int) or isinstance(source_run, bool) or source_run < 1:
                raise ValidationError("Prepared occurrence run identity must be a positive integer.")
            source_runs.add(source_run)

        job = row.get("job")
        if isinstance(job, str) and job.strip():
            jobs.add(job.strip())

        pull_request = row.get("pullRequest")
        if isinstance(pull_request, int) and not isinstance(pull_request, bool) and pull_request > 0:
            pull_requests.add(pull_request)

    return {
        "dates": dates,
        "sourceRuns": source_runs,
        "jobs": jobs,
        "pullRequests": pull_requests,
    }


def _occurrence_summary(
    *,
    dates: set[str],
    source_runs: set[int],
    jobs: set[str],
    pull_requests: set[int],
    complete: bool,
    recognized: bool,
) -> dict[str, Any]:
    sorted_dates = sorted(dates)
    return {
        "independentRunCount": len(source_runs),
        "distinctDayCount": len(dates),
        "distinctJobCount": len(jobs),
        "distinctPullRequestCount": len(pull_requests),
        "firstSeenDate": sorted_dates[0] if sorted_dates else None,
        "lastSeenDate": sorted_dates[-1] if sorted_dates else None,
        "ledgerComplete": complete,
        "schemaRecognized": recognized,
    }


def _watch_reason(
    default_judgment: Mapping[str, Any],
    occurrence_summary: Mapping[str, Any],
) -> str | None:
    recommendations = _require_list(default_judgment, "recommendations")
    recommendation = _require_mapping(recommendations[0], "default recommendation")
    if recommendation.get("disposition") != "watch":
        return None

    category = _require_nonempty_string(default_judgment, "category")
    independent_runs = _require_nonnegative_int(occurrence_summary, "independentRunCount")
    distinct_days = _require_nonnegative_int(occurrence_summary, "distinctDayCount")
    if category == "flaky-test":
        if independent_runs <= 1:
            return "single-test-occurrence"
        if distinct_days <= 1:
            return "same-day-test-recurrence"
        return "subthreshold-test-recurrence"
    if category == "transient-infrastructure":
        if independent_runs <= 1:
            return "single-infrastructure-occurrence"
        return "subthreshold-infrastructure-recurrence"
    if category == "unknown":
        return "missing-diagnostic-identity"
    return "insufficient-evidence"


def _review_required(
    *,
    default_judgment: Mapping[str, Any],
    candidate_action: str,
    occurrence_summary: Mapping[str, Any],
    related_issues: list[dict[str, Any]],
    watch_reason: str | None,
) -> bool:
    recommendations = _require_list(default_judgment, "recommendations")
    recommendation = _require_mapping(recommendations[0], "default recommendation")
    disposition = recommendation.get("disposition")
    category = default_judgment.get("category")
    independent_runs = _require_nonnegative_int(occurrence_summary, "independentRunCount")
    return (
        category == "unknown"
        or disposition == "investigate"
        or bool(related_issues)
        or candidate_action == "investigate"
        or independent_runs > 1
        or watch_reason == "missing-diagnostic-identity"
    )


def _apply_watch_explanation(
    default_judgment: dict[str, Any],
    watch_reason: str | None,
) -> None:
    if watch_reason is None:
        return
    recommendation = _require_mapping(
        _require_list(default_judgment, "recommendations")[0],
        "default recommendation",
    )
    recommendation["summary"] = _WATCH_SUMMARY_BY_REASON[watch_reason]
    recommendation["reassessWhen"] = _WATCH_REASSESS_WHEN_BY_REASON[watch_reason]


def _build_default_judgment(
    *,
    issue_number: int,
    title: str,
    producer: str,
    autoclose: bool | None,
    candidate_action: str,
    identity: Mapping[str, Any],
    occurrence_summary: Mapping[str, Any],
    missing_prerequisites: list[str],
    resolution_evidence: Mapping[str, Any],
    allowed_evidence_ids: list[str],
    human_context: Mapping[str, Any] | None,
    verification_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    category = _default_category(title, producer, identity)
    # A recovered run only counts as recovery evidence for the default
    # judgment when it is actually citable -- i.e. present in the capped
    # allowedEvidence the agent (and any downstream reviewer) can see.
    # Otherwise review-close would cite a run that isn't in evidenceIds,
    # and isn't visible in allowedEvidence, at all.
    recovered_run_evidence_id = _recovered_run_evidence_id(verification_context)
    if (
        recovered_run_evidence_id is not None
        and recovered_run_evidence_id not in allowed_evidence_ids
    ):
        recovered_run_evidence_id = None
    disposition = _default_disposition(
        producer=producer,
        autoclose=autoclose,
        candidate_action=candidate_action,
        category=category,
        occurrence_summary=occurrence_summary,
        human_context=human_context,
        recovered_run_evidence_id=recovered_run_evidence_id,
    )
    target = _default_target(issue_number, category, identity)
    confidence = (
        "high"
        if category == "blocking-build" and disposition == "ping-human"
        else "medium"
        if disposition in {"review-close", "review-quarantine", "review-retry", "no-action"}
        else "low"
    )
    evidence_ids = _default_evidence_ids(
        issue_number,
        resolution_evidence,
        allowed_evidence_ids,
        priority_evidence_ids=(
            (recovered_run_evidence_id,) if recovered_run_evidence_id else ()
        ),
    )
    human_escalation = (
        _build_human_escalation(
            title=title,
            human_context=human_context,
        )
        if disposition == "ping-human"
        else None
    )
    missing_evidence = (
        [] if human_escalation is not None else list(missing_prerequisites)
    )
    if not missing_evidence and disposition in {"watch", "investigate"}:
        missing_evidence = ["agent review needed"]

    recommendation: dict[str, Any] = {
        "disposition": disposition,
        "target": target,
        "confidence": confidence,
        "summary": (
            f"Human decision needed: {human_escalation['question']}"
            if human_escalation is not None
            else _DEFAULT_SUMMARY_BY_DISPOSITION[disposition]
        ),
        "evidenceIds": evidence_ids,
        "missingEvidence": missing_evidence,
        "reassessWhen": (
            "After the decision is recorded and an owner is identified."
            if human_escalation is not None
            else _DEFAULT_REASSESS_WHEN_BY_DISPOSITION[disposition]
        ),
    }
    if human_escalation is not None:
        recommendation["humanEscalation"] = human_escalation

    return {
        "issueNumber": issue_number,
        "category": category,
        "recommendations": [recommendation],
    }


def _build_human_escalation(
    *,
    title: str,
    human_context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    # `_build_default_judgment` only calls this helper when the deterministic
    # default disposition is "ping-human". Every code path in
    # `_default_disposition` that can produce "ping-human" already requires
    # `human_context.get("decisionRequired") is True`, so that condition
    # always holds here -- there is no reachable case with a false or
    # missing decisionRequired. A generic fallback for that case would be
    # dead code, so this raises instead of silently inventing one.
    if human_context is None or human_context.get("decisionRequired") is not True:
        raise AssertionError(
            "_build_human_escalation requires a human_context with decisionRequired=True"
        )

    assessment = human_context.get("reportedAssessment")
    suggestion = human_context.get("reportedSuggestion")
    streak = human_context.get("streak")
    context = (
        f"{title} failed {streak} consecutive times. {assessment}"
        if isinstance(streak, int) and isinstance(assessment, str)
        else f"{title}. {assessment}"
        if isinstance(assessment, str)
        else title
    )
    labels = human_context.get("labels")
    routing_hint = (
        next(
            (
                label
                for label in labels
                if isinstance(label, str) and label.startswith("area-")
            ),
            "workflow-owner",
        )
        if isinstance(labels, list)
        else "workflow-owner"
    )
    suggestion_text = (
        suggestion if isinstance(suggestion, str) and suggestion.strip() else None
    )
    # Ground whyHuman/question/suggestedNextSteps in what the issue itself
    # reported (assessment + suggestion) instead of asserting a specific
    # root cause or remediation path (e.g. "migrate to a service principal")
    # that the reported text never actually claimed.
    why_human = (
        f"{assessment} Resolving this requires an authorized owner to choose "
        "and configure the workflow's Azure tenant or identity."
        if isinstance(assessment, str)
        else "Resolving this requires an authorized owner to choose and "
        "configure the workflow's Azure tenant or identity."
    )
    question = (
        f'The reported suggestion is: "{suggestion_text}" Should the tenant be '
        "renewed, or should the workflow migrate to a different identity, and "
        "who owns that decision?"
        if suggestion_text is not None
        else "Should the Azure tenant be renewed, or should the workflow "
        "migrate to a different identity, and who owns that decision?"
    )
    suggested_next_steps = [
        (
            f"Follow the reported suggestion: {suggestion_text}"
            if suggestion_text is not None
            else "Choose the tenant renewal or identity migration path and identify an owner."
        ),
        "Update the workflow's Azure identity configuration to implement that decision.",
        "Rerun the workflow and link the first successful run.",
    ]
    return {
        "context": context,
        "whyHuman": why_human,
        "question": question,
        "suggestedNextSteps": suggested_next_steps,
        "routingHint": routing_hint,
    }


def _default_category(title: str, producer: str, identity: Mapping[str, Any]) -> str:
    if producer in {"tracking-issue", "ci-health-dashboard", "gh-aw-failure-issue"}:
        return "automation-tracker"

    normalized_identity = " ".join(
        value.lower()
        for value in identity.values()
        if isinstance(value, str) and value.strip()
    )
    normalized_text = f"{title.lower()} {normalized_identity}"
    if any(
        token in normalized_text
        for token in ("[main ci failure]", "did not compile", "fails to compile", "build is broken")
    ):
        return "blocking-build"

    tier2_test_name = identity.get("tier2TestName")
    if isinstance(tier2_test_name, str) and tier2_test_name.strip():
        if any(
            token in normalized_text
            for token in ("evaluation-period-expired", "evaluation period expired", "dependency unavailable")
        ):
            return "product-or-tooling"
        return "flaky-test"

    if any(
        token in normalized_text
        for token in (
            "nuget",
            "npm",
            "registry",
            "rate-limit",
            "rate limit",
            "http 429",
            "http 503",
            "service-unavailable",
            "service unavailable",
            "connection-reset",
            "connection reset",
            "dns",
            "download",
            "ssl",
            "tls",
            "codeload",
            "process-init",
            "process init",
            "0xc0000142",
            "cdn",
            "feed",
        )
    ):
        return "transient-infrastructure"

    return "unknown"


def _default_disposition(
    *,
    producer: str,
    autoclose: bool | None,
    candidate_action: str,
    category: str,
    occurrence_summary: Mapping[str, Any],
    human_context: Mapping[str, Any] | None,
    recovered_run_evidence_id: str | None = None,
) -> str:
    if candidate_action == "recommend-close":
        return "review-close"

    independent_runs = _require_nonnegative_int(occurrence_summary, "independentRunCount")
    distinct_days = _require_nonnegative_int(occurrence_summary, "distinctDayCount")

    if category == "automation-tracker":
        if autoclose is True:
            return "no-action"
        if producer == "ci-health-dashboard":
            return (
                "ping-human"
                if human_context is not None
                and human_context.get("decisionRequired") is True
                else "investigate"
            )
        return "investigate"
    if category == "flaky-test":
        if independent_runs >= 2 and distinct_days >= 2:
            return "review-quarantine"
        return "watch"
    if category == "transient-infrastructure":
        if independent_runs >= 3 and distinct_days >= 2:
            return "review-retry"
        return "watch"
    if category == "blocking-build":
        # A directly issue-scoped, later successful run on main is verified
        # recovery evidence and takes priority even over a reported human
        # decision requirement -- the failure is already known to be resolved.
        # Only counts here when it is citable (see _build_default_judgment);
        # otherwise review-close would report a run absent from evidenceIds.
        if recovered_run_evidence_id is not None:
            return "review-close"
        if human_context is not None and human_context.get("decisionRequired") is True:
            return "ping-human"
        return "investigate"
    if category == "product-or-tooling":
        return "investigate"
    if candidate_action == "investigate":
        return "investigate"
    return "watch"


def _default_target(
    issue_number: int,
    category: str,
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    tier2_test_name = identity.get("tier2TestName")
    if category == "flaky-test" and isinstance(tier2_test_name, str) and tier2_test_name.strip():
        return {"kind": "test", "value": tier2_test_name}
    tier1_cause_id = identity.get("tier1CauseId")
    if (
        category == "transient-infrastructure"
        and isinstance(tier1_cause_id, str)
        and tier1_cause_id.strip()
    ):
        return {"kind": "failure-fingerprint", "value": tier1_cause_id}
    return {"kind": "issue", "value": issue_number}


def _default_evidence_ids(
    issue_number: int,
    resolution_evidence: Mapping[str, Any],
    allowed_evidence_ids: list[str],
    *,
    priority_evidence_ids: Sequence[str] = (),
) -> list[str]:
    allowed = list(dict.fromkeys(allowed_evidence_ids))
    if not allowed:
        raise ValidationError(f"Prepared issue {issue_number} has no allowed evidence.")

    issue_evidence_id = f"issue:{issue_number}"
    if issue_evidence_id not in allowed:
        raise ValidationError(
            f"Prepared issue {issue_number} default judgment requires issue evidence in allowedEvidence."
        )

    evidence_ids: list[str] = [issue_evidence_id]
    for evidence_id in (*priority_evidence_ids, *_resolution_evidence_ids(resolution_evidence)):
        if evidence_id in allowed and evidence_id not in evidence_ids:
            evidence_ids.append(evidence_id)
        if len(evidence_ids) == 3:
            return evidence_ids

    for evidence_id in allowed:
        if evidence_id not in evidence_ids:
            evidence_ids.append(evidence_id)
        if len(evidence_ids) == 3:
            break
    return evidence_ids


def _resolution_evidence_ids(resolution_evidence: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []
    for key, value in resolution_evidence.items():
        if key == "evidenceIds" and isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    ids.append(item)
            continue
        if key == "evidenceId" or key.endswith("EvidenceId"):
            if isinstance(value, str) and value.strip():
                ids.append(value)
    return list(dict.fromkeys(ids))


_MAX_ALLOWED_EVIDENCE = 8
# The source issue always leads; workflow runs come next because a recovery
# judgment (verificationContext) cites a specific run, and that run must
# survive the cap regardless of how many pull requests or source-path records
# an issue has accumulated. Pull requests rank above the remaining kinds but
# below workflow runs so an issue-linked PR is still likely to be cited.
_ALLOWED_EVIDENCE_PRIORITY_BY_KIND = {
    "issue-event": 0,
    "workflow-run": 1,
    "pull-request": 2,
}
_ALLOWED_EVIDENCE_DEFAULT_PRIORITY = 3


def _select_allowed_evidence(
    evidence_bundle: Sequence[Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Deterministically cap the evidence bundle for the agent-visible allowedEvidence.

    A plain prefix slice of the (lifecycle-ordered) evidence bundle can evict
    the one workflow-run record a recovery judgment depends on once an issue
    has accumulated many linked pull requests or source-path records. Ranking
    by kind first -- ties broken by the original bundle order -- keeps the
    cap small while making sure recovery-relevant evidence is prioritized.
    """
    records = [
        _require_mapping(entry, "prepared evidence bundle entry") for entry in evidence_bundle
    ]
    ranked = sorted(
        enumerate(records),
        key=lambda item: (
            _ALLOWED_EVIDENCE_PRIORITY_BY_KIND.get(
                item[1].get("kind"), _ALLOWED_EVIDENCE_DEFAULT_PRIORITY
            ),
            item[0],
        ),
    )
    allowed_evidence: list[dict[str, Any]] = []
    allowed_evidence_ids: list[str] = []
    for _, record in ranked[:_MAX_ALLOWED_EVIDENCE]:
        projected = _project_allowed_evidence(record)
        allowed_evidence.append(projected)
        allowed_evidence_ids.append(_require_nonempty_string(projected, "id"))
    return allowed_evidence, allowed_evidence_ids


def _project_allowed_evidence(record: Mapping[str, Any]) -> dict[str, Any]:
    evidence_id = _require_nonempty_string(record, "id")
    kind = _require_nonempty_string(record, "kind")
    availability = _require_nonempty_string(record, "availability")
    _require_only_fields(
        record,
        {"id", "kind", "url", "availability", "payload"},
        "prepared evidence bundle entry",
    )
    projected: dict[str, Any] = {
        "id": evidence_id,
        "kind": kind,
        "availability": availability,
    }
    payload = record.get("payload")
    if isinstance(payload, Mapping):
        projected.update(_evidence_payload_summary(kind, payload))
    return projected


_PULL_REQUEST_SUMMARY_FIELDS = ("state", "mergedAt", "mergeCommitSha")
_WORKFLOW_RUN_SUMMARY_FIELDS = (
    "status",
    "conclusion",
    "event",
    "headBranch",
    "headSha",
    "createdAt",
    "updatedAt",
)
_WORKFLOW_RUN_HISTORY_ENTRY_FIELDS = ("runId", "status", "conclusion", "createdAt", "headBranch")
_MAX_PROJECTED_RECENT_HISTORY = 5


def _evidence_payload_summary(kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Bounded, per-kind summary of otherwise-hidden evidence payload fields.

    This intentionally keeps free-text bodies (issue/comment bodies, workflow
    logs) and arbitrary payload fields out of what the assessing agent can
    see -- only a small, named allowlist of structured fields per evidence
    kind is ever surfaced here.
    """
    if kind == "pull-request":
        summary = {field: payload[field] for field in _PULL_REQUEST_SUMMARY_FIELDS if field in payload}
        base = payload.get("base")
        if isinstance(base, Mapping):
            base_branch = base.get("ref")
            if isinstance(base_branch, str) and base_branch.strip():
                summary["baseBranch"] = base_branch
        return summary
    if kind == "workflow-run":
        summary = {field: payload[field] for field in _WORKFLOW_RUN_SUMMARY_FIELDS if field in payload}
        recent_history = payload.get("recentHistory")
        if isinstance(recent_history, list) and recent_history:
            projected_history = []
            for raw_entry in recent_history[:_MAX_PROJECTED_RECENT_HISTORY]:
                if not isinstance(raw_entry, Mapping):
                    continue
                projected_history.append(
                    {
                        field: raw_entry[field]
                        for field in _WORKFLOW_RUN_HISTORY_ENTRY_FIELDS
                        if field in raw_entry
                    }
                )
            if projected_history:
                summary["recentHistory"] = projected_history
        return summary
    return {}


def _copy_string_list(value: object, name: str) -> list[str]:
    if not isinstance(value, list):
        raise ValidationError(f"{name} must be an array.")
    copied: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValidationError(f"{name} must contain nonempty strings.")
        copied.append(item)
    return copied


def _prepared_issue_sort_key(issue: object) -> tuple[int, str]:
    if not isinstance(issue, Mapping):
        raise ValidationError("prepared issue must be an object.")
    return (_require_positive_int(issue, "issueNumber"), _require_nonempty_string(issue, "title"))
