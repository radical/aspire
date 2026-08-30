"""Selective model handoff for the CI shepherd POC.

The deterministic layer already decides the overwhelming majority of cases:
on the frozen 87-issue corpus only one recommendation carried model-authored
text. Sending every issue to a model each cycle therefore pays a per-issue
cost to reproduce a default the pipeline already computed, and it widens the
surface an agent can push an unsupported conclusion through.

This module narrows the handoff to cases that need a fresh judgment: every
first-seen case, every materially changed case, and every case whose periodic
reassessment is due. Stable previously reviewed cases keep their last validated
judgment without a model call.

The selection is also the merge contract: each selected case carries the exact
set of dispositions it can legitimately project into, so an override that
could never become an action is rejected at merge time rather than discovered
later by the proposal renderer.
"""
from __future__ import annotations

import copy
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Mapping

from ci_shepherd.models import EVIDENCE_REQUEST_DECISION_GATES, ValidationError
from ci_shepherd.poc import (
    ASSESSMENT_SCHEMA_VERSION,
    DISPOSITIONS,
    close_is_projectable,
    compact_issue_requires_human_decision,
    is_ambiguous_default,
    validate_poc_projectability,
)
from ci_shepherd.poc_history import compute_fingerprint


SELECTION_SCHEMA_VERSION = 2

# Dispositions an agent may always choose for a selected case. `review-close`
# and `ping-human` are deliberately absent: both are added per case only when
# the deterministic evidence that lets them project into a real action is
# already present.
_BASE_ALLOWED_DISPOSITIONS = frozenset(DISPOSITIONS - {"review-close", "ping-human"})

_OMISSION_NOT_ELIGIBLE = "not-review-required"
_OMISSION_UNCHANGED = "unchanged-stable"
_OMISSION_SUPERSEDED = "superseded-duplicate"

__all__ = [
    "SELECTION_SCHEMA_VERSION",
    "build_review_selection",
    "merge_selected_poc_judgments",
    "selected_issue_numbers",
    "validate_review_selection",
]


def build_review_selection(
    compact_input: object,
    *,
    new_issue_numbers: Iterable[int] = (),
    changed_issue_numbers: Iterable[int] = (),
    due_issue_numbers: Iterable[int] = (),
    known_issue_numbers: Iterable[int] | None = None,
    change_reasons_by_issue: Mapping[int, Iterable[str]] | None = None,
    previous_judgments: object | None = None,
    reassessment_context_by_issue: Mapping[
        int, Mapping[str, str]
    ] | None = None,
) -> dict[str, Any]:
    """Choose the cases worth a model call and state exactly what to answer.

    ``known_issue_numbers`` is the set of cases a previous cycle already
    judged. ``None`` means no prior cycle is available, so every eligible case
    is treated as first-seen -- the safe bootstrap, because omitting a case
    that was never judged would silently keep an unreviewed default.
    """
    compact = _require_mapping(compact_input, "compact input")
    _require_exact_schema(compact, "compact input")
    snapshot_id = _require_nonempty_string(compact, "snapshotId")

    issues = [
        _require_mapping(raw_issue, "compact issue")
        for raw_issue in _require_list(compact, "issues")
    ]
    issue_numbers = [_require_positive_int(issue, "issueNumber") for issue in issues]
    known = _issue_number_set(issue_numbers, known_issue_numbers, "knownIssueNumbers")
    new = _issue_number_set(issue_numbers, new_issue_numbers, "newIssueNumbers")
    changed = _issue_number_set(issue_numbers, changed_issue_numbers, "changedIssueNumbers")
    due = _issue_number_set(issue_numbers, due_issue_numbers, "dueIssueNumbers")
    change_reasons = _change_reasons_by_issue(
        issue_numbers,
        new=new,
        changed=changed,
        due=due,
        supplied=change_reasons_by_issue,
    )
    previous_by_issue = _previous_judgments_by_issue(previous_judgments)
    reassessment_context = reassessment_context_by_issue or {}
    unknown_reassessment_context = set(reassessment_context) - set(issue_numbers)
    if unknown_reassessment_context:
        raise ValidationError(
            "Reassessment context includes unknown issue "
            f"{min(unknown_reassessment_context)}."
        )

    selected: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    for issue, issue_number in zip(issues, issue_numbers):
        change_class = _change_class(
            issue_number,
            new=new,
            changed=changed,
            due=due,
            known=known,
            known_supplied=known_issue_numbers is not None,
        )
        omission_reason = _omission_reason(issue, change_class)
        if omission_reason is not None:
            omitted_case: dict[str, Any] = {
                "issueNumber": issue_number,
                "reason": omission_reason,
            }
            previous = previous_by_issue.get(issue_number)
            if (
                change_class == "unchanged"
                and omission_reason != _OMISSION_SUPERSEDED
                and previous is not None
                and previous != issue.get("defaultJudgment")
            ):
                try:
                    _require_allowed_dispositions(
                        previous,
                        issue_number,
                        set(_allowed_dispositions(issue)),
                        issue,
                    )
                except ValidationError:
                    omitted_case["retainedJudgmentDiscardedReason"] = (
                        "no-longer-projectable"
                    )
                else:
                    omitted_case["retainedJudgment"] = copy.deepcopy(previous)
            omitted.append(omitted_case)
            continue

        reasons = _review_reasons(issue, change_class)
        selected_case: dict[str, Any] = {
            "issueNumber": issue_number,
            "changeClass": change_class,
            "changeReasons": change_reasons[issue_number],
            "reviewReasons": list(reasons),
            "allowedDispositions": _allowed_dispositions(issue),
            "question": _build_question(issue, issue_number),
        }
        previous = _previous_judgment_summary(previous_by_issue.get(issue_number))
        if previous is not None:
            selected_case.update(previous)
        context = reassessment_context.get(issue_number)
        if context is not None:
            for field in ("lastReviewedAt", "reassessAt"):
                value = context.get(field)
                if isinstance(value, str) and value:
                    selected_case[field] = value
        selected.append(selected_case)

    return {
        "schemaVersion": SELECTION_SCHEMA_VERSION,
        "snapshotId": snapshot_id,
        "selected": selected,
        "omitted": omitted,
        "summary": {
            "issueCount": len(issues),
            "selectedCount": len(selected),
            "omittedCount": len(omitted),
        },
    }


def selected_issue_numbers(selection: object) -> set[int]:
    validated = validate_review_selection(selection)
    return {
        int(entry["issueNumber"])
        for entry in validated["selected"]
    }


def validate_review_selection(selection: object) -> dict[str, Any]:
    document = _require_mapping(selection, "review selection")
    schema_version = document.get("schemaVersion")
    if schema_version not in {1, SELECTION_SCHEMA_VERSION}:
        raise ValidationError("Review selection schemaVersion must be 1 or 2.")
    _require_nonempty_string(document, "snapshotId")

    seen: set[int] = set()
    for entry in _require_list(document, "selected"):
        selected_entry = _require_mapping(entry, "selected case")
        issue_number = _require_positive_int(selected_entry, "issueNumber")
        if issue_number in seen:
            raise ValidationError(f"Duplicate selected case for issue {issue_number}.")
        seen.add(issue_number)
        allowed = _require_list(selected_entry, "allowedDispositions")
        if not allowed:
            raise ValidationError(
                f"Selected case {issue_number} must allow at least one disposition."
            )
        for disposition in allowed:
            if disposition not in DISPOSITIONS:
                raise ValidationError(
                    f"Selected case {issue_number} allows unsupported disposition "
                    f"{disposition}."
                )
        change_reasons = selected_entry.get("changeReasons")
        if not (schema_version == 1 and change_reasons is None):
            if (
                not isinstance(change_reasons, list)
                or not change_reasons
                or not all(
                    isinstance(reason, str) and reason.strip()
                    for reason in change_reasons
                )
            ):
                raise ValidationError(
                    f"Selected case {issue_number} changeReasons must contain "
                    "nonempty strings."
                )
        previous_disposition = selected_entry.get("previousDisposition")
        if previous_disposition is not None and (
            not isinstance(previous_disposition, str)
            or not previous_disposition.strip()
        ):
            raise ValidationError(
                f"Selected case {issue_number} previousDisposition must be "
                "a nonempty string."
            )
        previous_category = selected_entry.get("previousCategory")
        if previous_category is not None and (
            not isinstance(previous_category, str)
            or not previous_category.strip()
        ):
            raise ValidationError(
                f"Selected case {issue_number} previousCategory must be "
                "a nonempty string."
            )
        for field in ("lastReviewedAt", "reassessAt"):
            value = selected_entry.get(field)
            if value is not None:
                _validate_selection_timestamp(
                    value,
                    issue_number=issue_number,
                    field=field,
                )
    for entry in _require_list(document, "omitted"):
        omitted_entry = _require_mapping(entry, "omitted case")
        issue_number = _require_positive_int(omitted_entry, "issueNumber")
        if issue_number in seen:
            raise ValidationError(
                f"Issue {issue_number} is both selected and omitted."
            )
        seen.add(issue_number)
        reason = _require_nonempty_string(omitted_entry, "reason")
        retained = omitted_entry.get("retainedJudgment")
        discarded_reason = omitted_entry.get("retainedJudgmentDiscardedReason")
        if discarded_reason is not None:
            if discarded_reason != "no-longer-projectable":
                raise ValidationError(
                    f"Issue {issue_number} has an invalid retained judgment discard reason."
                )
            if retained is not None:
                raise ValidationError(
                    f"Issue {issue_number} cannot retain and discard the same judgment."
                )
        if retained is not None:
            if reason not in {_OMISSION_UNCHANGED, _OMISSION_NOT_ELIGIBLE}:
                raise ValidationError(
                    f"Issue {issue_number} may only retain a judgment when unchanged."
                )
            retained_judgment = _require_mapping(
                retained,
                "retained judgment",
            )
            if _require_positive_int(retained_judgment, "issueNumber") != issue_number:
                raise ValidationError(
                    f"Retained judgment for issue {issue_number} has the wrong target."
                )
    return dict(document)


def _validate_selection_timestamp(
    value: object,
    *,
    issue_number: int,
    field: str,
) -> None:
    if not isinstance(value, str) or not value:
        raise ValidationError(
            f"Selected case {issue_number} {field} must be an ISO 8601 timestamp."
        )
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValidationError(
            f"Selected case {issue_number} {field} must be an ISO 8601 timestamp."
        ) from error
    if instant.tzinfo is None:
        raise ValidationError(
            f"Selected case {issue_number} {field} must include a UTC offset."
        )


def merge_selected_poc_judgments(
    compact_input: object,
    selection: object,
    agent_judgments: object,
) -> dict[str, Any]:
    """Apply sparse agent judgments to selected cases only.

    Unlike the full merge, the agent may return judgments for a subset of the
    selected cases -- silence means "the deterministic default stands". An
    unchanged omitted case retains its last validated judgment. A judgment for
    any other unselected case, or one whose disposition the case cannot project,
    is an error rather than something to quietly drop, because a silently
    discarded override looks identical to agreement.
    """
    compact = _require_mapping(compact_input, "compact input")
    agent = _require_mapping(agent_judgments, "agent judgments")
    _require_exact_schema(compact, "compact input")
    _require_exact_schema(agent, "agent judgments")

    snapshot_id = _require_nonempty_string(compact, "snapshotId")
    if _require_nonempty_string(agent, "snapshotId") != snapshot_id:
        raise ValidationError("Agent judgment snapshotId must match compact input.")

    validated_selection = validate_review_selection(selection)
    if validated_selection["snapshotId"] != snapshot_id:
        raise ValidationError("Review selection snapshotId must match compact input.")
    allowed_by_issue = {
        int(entry["issueNumber"]): set(entry["allowedDispositions"])
        for entry in validated_selection["selected"]
    }
    retained_by_issue = {
        int(entry["issueNumber"]): _require_mapping(
            entry["retainedJudgment"],
            "retained judgment",
        )
        for entry in validated_selection["omitted"]
        if "retainedJudgment" in entry
    }

    agent_issues: dict[int, Mapping[str, Any]] = {}
    for raw_issue in _require_list(agent, "issues"):
        issue = _require_mapping(raw_issue, "agent issue judgment")
        issue_number = _require_positive_int(issue, "issueNumber")
        if issue_number in agent_issues:
            raise ValidationError(f"Duplicate agent judgment for issue {issue_number}.")
        agent_issues[issue_number] = issue

    merged: list[dict[str, Any]] = []
    overridden: list[dict[str, Any]] = []
    compact_issue_numbers: set[int] = set()
    for raw_issue in _require_list(compact, "issues"):
        issue = _require_mapping(raw_issue, "compact issue")
        issue_number = _require_positive_int(issue, "issueNumber")
        compact_issue_numbers.add(issue_number)
        default_judgment = _require_mapping(
            issue.get("defaultJudgment"),
            "default judgment",
        )
        agent_issue = agent_issues.get(issue_number)
        if agent_issue is None:
            retained_issue = retained_by_issue.get(issue_number)
            if retained_issue is not None:
                retained_override = copy.deepcopy(dict(retained_issue))
                merged.append(retained_override)
                overridden.append(retained_override)
                continue
            merged.append(copy.deepcopy(dict(default_judgment)))
            continue

        allowed = allowed_by_issue.get(issue_number)
        if allowed is None:
            raise ValidationError(
                f"Agent judgment for issue {issue_number} was not selected for review."
            )
        _require_allowed_dispositions(agent_issue, issue_number, allowed, issue)
        override = copy.deepcopy(dict(agent_issue))
        merged.append(override)
        overridden.append(override)

    unexpected = sorted(set(agent_issues) - compact_issue_numbers)
    if unexpected:
        raise ValidationError(
            f"Unexpected agent judgment for issue {unexpected[0]}."
        )

    # Only the overrides are gated. A deterministic default that cannot project
    # is a defect in the default rubric, and failing the whole document over it
    # would block every unrelated issue in the cycle from being finalized.
    validate_poc_projectability(
        compact,
        {
            "schemaVersion": ASSESSMENT_SCHEMA_VERSION,
            "snapshotId": snapshot_id,
            "issues": overridden,
        },
    )
    return {
        "schemaVersion": ASSESSMENT_SCHEMA_VERSION,
        "snapshotId": snapshot_id,
        "issues": merged,
    }


def _require_allowed_dispositions(
    agent_issue: Mapping[str, Any],
    issue_number: int,
    allowed: set[str],
    compact_issue: Mapping[str, Any],
) -> None:
    for raw_recommendation in _require_list(agent_issue, "recommendations"):
        recommendation = _require_mapping(raw_recommendation, "recommendation")
        disposition = recommendation.get("disposition")
        if disposition not in allowed:
            raise ValidationError(
                f"Issue {issue_number} may not be judged {disposition!r}; "
                f"allowed dispositions are {sorted(allowed)}."
            )
        # Defense in depth: the allow-list is derived from the same predicate,
        # but a hand-edited selection document must not be able to authorize an
        # escalation the issue never reported.
        if disposition == "ping-human" and not compact_issue_requires_human_decision(
            compact_issue
        ):
            raise ValidationError(
                f"Issue {issue_number} ping-human requires a reported human decision."
            )


def _change_class(
    issue_number: int,
    *,
    new: set[int],
    changed: set[int],
    due: set[int],
    known: set[int],
    known_supplied: bool,
) -> str:
    if issue_number in new:
        return "new"
    if issue_number in changed:
        return "changed"
    if issue_number in due:
        return "due"
    if not known_supplied or issue_number not in known:
        return "first-seen"
    return "unchanged"


def _change_reasons_by_issue(
    issue_numbers: list[int],
    *,
    new: set[int],
    changed: set[int],
    due: set[int],
    supplied: Mapping[int, Iterable[str]] | None,
) -> dict[int, list[str]]:
    issue_number_set = set(issue_numbers)
    reasons_by_issue: dict[int, set[str]] = {
        issue_number: set()
        for issue_number in issue_numbers
    }
    if supplied is not None:
        for issue_number, raw_reasons in supplied.items():
            if issue_number not in issue_number_set:
                raise ValidationError(
                    f"Change reasons include unknown issue {issue_number}."
                )
            if isinstance(raw_reasons, str):
                raise ValidationError(
                    f"Change reasons for issue {issue_number} must be an iterable "
                    "of strings."
                )
            for reason in raw_reasons:
                if not isinstance(reason, str) or not reason.strip():
                    raise ValidationError(
                        f"Change reasons for issue {issue_number} must contain "
                        "nonempty strings."
                    )
                reasons_by_issue[issue_number].add(reason.strip())
    for issue_number in new:
        reasons_by_issue[issue_number].add("new-issue")
    for issue_number in changed:
        if not reasons_by_issue[issue_number]:
            reasons_by_issue[issue_number].add("material-change")
    for issue_number in due:
        reasons_by_issue[issue_number].add("scheduled-reassessment")
    for issue_number in issue_numbers:
        if not reasons_by_issue[issue_number]:
            reasons_by_issue[issue_number].add("first-seen")
    return {
        issue_number: sorted(reasons)
        for issue_number, reasons in reasons_by_issue.items()
    }


def _previous_judgments_by_issue(
    previous_judgments: object | None,
) -> dict[int, dict[str, Any]]:
    if previous_judgments is None:
        return {}
    if not isinstance(previous_judgments, list):
        raise ValidationError("Previous judgments must be a list.")
    previous_by_issue: dict[int, dict[str, Any]] = {}
    for raw_judgment in previous_judgments:
        judgment = _require_mapping(raw_judgment, "previous judgment")
        issue_number = _require_positive_int(judgment, "issueNumber")
        if issue_number in previous_by_issue:
            raise ValidationError(
                f"Duplicate previous judgment for issue {issue_number}."
            )
        previous_by_issue[issue_number] = copy.deepcopy(dict(judgment))
    return previous_by_issue


def _previous_judgment_summary(
    judgment: Mapping[str, Any] | None,
) -> dict[str, str] | None:
    if judgment is None:
        return None
    summary: dict[str, str] = {}
    category = judgment.get("category")
    if isinstance(category, str) and category.strip():
        summary["previousCategory"] = category
    raw_recommendations = judgment.get("recommendations")
    if raw_recommendations is None:
        return summary
    if not isinstance(raw_recommendations, list):
        raise ValidationError("recommendations must be an array.")
    if raw_recommendations:
        recommendation = _require_mapping(
            raw_recommendations[0],
            "previous recommendation",
        )
        disposition = recommendation.get("disposition")
        if isinstance(disposition, str) and disposition.strip():
            summary["previousDisposition"] = disposition
    return summary


def _omission_reason(issue: Mapping[str, Any], change_class: str) -> str | None:
    action_cluster = issue.get("actionCluster")
    if (
        isinstance(action_cluster, Mapping)
        and action_cluster.get("role") == "superseded"
    ):
        # The full merge already refuses agent judgments for superseded members,
        # so selecting one would spend a model call on an override that can only
        # be discarded.
        return _OMISSION_SUPERSEDED
    if change_class in {"first-seen", "new", "changed", "due"}:
        return None
    if not _is_eligible(issue):
        return _OMISSION_NOT_ELIGIBLE
    if change_class == "unchanged":
        return _OMISSION_UNCHANGED
    return None


def _is_eligible(issue: Mapping[str, Any]) -> bool:
    if issue.get("reviewRequired") is True:
        return True
    default_judgment = issue.get("defaultJudgment")
    return isinstance(default_judgment, Mapping) and is_ambiguous_default(
        default_judgment
    )


def _review_reasons(
    issue: Mapping[str, Any],
    change_class: str,
) -> tuple[str, ...]:
    default_judgment = _require_mapping(issue.get("defaultJudgment"), "default judgment")
    reasons: list[str] = []
    if change_class in {"first-seen", "new"}:
        reasons.append("initial-assessment")
    if change_class == "changed":
        reasons.append("material-change")
    if change_class == "due":
        reasons.append("scheduled-reassessment")
    if issue.get("reviewRequired") is True:
        reasons.append("review-required")
    if default_judgment.get("category") == "unknown":
        reasons.append("unknown-category")
    if any(
        isinstance(recommendation, Mapping)
        and recommendation.get("disposition") == "investigate"
        for recommendation in _require_list(default_judgment, "recommendations")
    ):
        reasons.append("investigate-default")
    if issue.get("relatedIssues"):
        reasons.append("related-issue-candidate")
    summary = issue.get("occurrenceSummary")
    if isinstance(summary, Mapping) and _as_int(summary.get("independentRunCount")) > 1:
        reasons.append("recurrence-observed")
    if compact_issue_requires_human_decision(issue):
        reasons.append("human-decision-reported")
    return tuple(reasons)


def _allowed_dispositions(issue: Mapping[str, Any]) -> list[str]:
    allowed = set(_BASE_ALLOWED_DISPOSITIONS)
    if close_is_projectable(issue):
        allowed.add("review-close")
    if compact_issue_requires_human_decision(issue):
        allowed.add("ping-human")
    return sorted(allowed)


def _decision_gates(issue: Mapping[str, Any]) -> list[str]:
    default_judgment = _require_mapping(issue.get("defaultJudgment"), "default judgment")
    gates: list[str] = []
    if issue.get("resolutionEvidence"):
        gates.extend(("merged-fix", "post-fix-green", "recovery"))
    if issue.get("relatedIssues"):
        gates.extend(("canonical-issue", "canonical-search-complete"))
    if (
        default_judgment.get("category") == "unknown"
        or issue.get("watchReason") == "missing-diagnostic-identity"
    ):
        gates.append("current-failing-run")
    if not gates:
        gates.extend(("no-recent-matching-failure", "no-newer-matching-failure"))

    ordered = list(dict.fromkeys(gates))
    unsupported = [gate for gate in ordered if gate not in EVIDENCE_REQUEST_DECISION_GATES]
    if unsupported:
        raise ValidationError(f"Unsupported decision gate: {unsupported[0]}.")
    return ordered


def _build_question(issue: Mapping[str, Any], issue_number: int) -> dict[str, Any]:
    """State the decidable question in terms of this case's own evidence.

    The deterministic default summary is deliberately generic ("Investigate
    this issue."), which tells a reviewer nothing about what to check or when
    to stop. Everything here is derived from facts the pipeline already
    computed, so the question names the observed identity, the exact evidence
    already cited, the gates that could move the decision, and the point at
    which the deterministic default simply stands.
    """
    identity = issue.get("identity")
    fingerprint = (
        compute_fingerprint(identity) if isinstance(identity, Mapping) else None
    ) or "unresolved-identity"
    default_judgment = _require_mapping(issue.get("defaultJudgment"), "default judgment")
    recommendations = _require_list(default_judgment, "recommendations")
    recommendation = _require_mapping(recommendations[0], "default recommendation")
    evidence_checked = [
        value
        for value in recommendation.get("evidenceIds", [])
        if isinstance(value, str) and value.strip()
    ]
    missing_facts = [
        value
        for value in recommendation.get("missingEvidence", [])
        if isinstance(value, str) and value.strip()
    ]
    gates = _decision_gates(issue)
    summary = issue.get("occurrenceSummary")
    runs = _as_int(summary.get("independentRunCount")) if isinstance(summary, Mapping) else 0
    days = _as_int(summary.get("distinctDayCount")) if isinstance(summary, Mapping) else 0
    evidence_text = ", ".join(evidence_checked) if evidence_checked else "no cited evidence"
    gate_text = ", ".join(gates)
    default_disposition = recommendation.get("disposition")

    return {
        "observedIdentity": fingerprint,
        "evidenceChecked": evidence_checked,
        "missingFacts": missing_facts,
        "decisionGates": gates,
        "defaultDisposition": default_disposition,
        "ask": (
            f"Issue #{issue_number} ({fingerprint}) has {runs} independent "
            f"occurrence(s) across {days} distinct day(s); the deterministic "
            f"default is {default_disposition}. Using only {evidence_text}, is "
            f"any of these gates provable now: {gate_text}?"
        ),
        "stopCondition": (
            f"Stop after checking {evidence_text} for {gate_text}. If no gate is "
            f"provable from that evidence, keep the deterministic "
            f"{default_disposition} default for #{issue_number} and record the "
            f"missing fact instead of inferring one."
        ),
        "costClass": "no-fetch" if not missing_facts else "bounded-probe",
    }


def _issue_number_set(
    issue_numbers: list[int],
    values: Iterable[int] | None,
    field: str,
) -> set[int]:
    if values is None:
        return set()
    known = set(issue_numbers)
    result: set[int] = set()
    for value in values:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValidationError(f"{field} must contain positive issue numbers.")
        if value not in known:
            raise ValidationError(
                f"{field} references issue {value}, which is not in the compact input."
            )
        result.add(value)
    return result


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


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
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValidationError(f"{key} must be a positive integer.")
    return value


def _require_exact_schema(mapping: Mapping[str, Any], name: str) -> None:
    if mapping.get("schemaVersion") != ASSESSMENT_SCHEMA_VERSION:
        raise ValidationError(f"{name} schemaVersion must be {ASSESSMENT_SCHEMA_VERSION}.")
