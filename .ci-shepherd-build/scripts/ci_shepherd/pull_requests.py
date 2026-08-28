"""Conservative pull-request triage for the CI shepherd.

Pull requests are handled far more cautiously than issues. A bot-authored or
target-labeled pull request is a *proposed change*, so the shepherd never
closes one and never guesses at its state from age or title. Every disposition
has to be backed by the pull request's current head-commit check conclusion and
current review state, because a stale conclusion from an earlier push is
indistinguishable from "no CI has run yet" once it is summarized into prose.

The module is a vertical slice of the same pipeline the issue side already
uses:

``build_pull_request_current_state``
    Normalizes GET-only collector payloads (check runs, combined status,
    reviews, mergeability) into one bounded structured block.
``build_pull_request_handoff``
    Selects new or changed pull requests and attaches the structured state, a
    deterministic default judgment, and the exact dispositions each case may
    project into.
``validate_pull_request_judgments`` / ``merge_pull_request_judgments``
    The strict typed judgment contract and its sparse merge.
``render_pull_request_section``
    Deterministic markdown.
``build_pull_request_comment_proposals``
    Exact-action comment proposals, suppressed when nothing changed.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from .models import ValidationError, validate_snapshot


PULL_REQUEST_SCHEMA_VERSION = 1

# Closure is deliberately absent. The shepherd may queue an issue for closure
# review, but a pull request encodes someone's proposed change: withdrawing it
# is an authorship decision, not a triage outcome.
PULL_REQUEST_DISPOSITIONS = frozenset(
    {"investigate", "watch", "ping-human", "no-action"}
)

# Dispositions that assert a conclusion about the pull request. They require a
# decisive current check conclusion; otherwise the only honest answer is watch.
_CONCLUSIVE_DISPOSITIONS = frozenset({"investigate", "no-action", "ping-human"})

# Bound on how many individual check names are retained per pull request. The
# handoff is a triage queue, not a build log.
PULL_REQUEST_CHECK_LIMIT = 10

CHECKS_GREEN = "green"
CHECKS_RED = "red"
CHECKS_PENDING = "pending"
CHECKS_UNKNOWN = "unknown"

REVIEW_APPROVED = "approved"
REVIEW_CHANGES_REQUESTED = "changes-requested"
REVIEW_REQUIRED = "review-required"

# GitHub check-run conclusions grouped by what they license. `cancelled` and
# `stale` are intentionally *not* passing: a cancelled check proves nothing ran
# to completion, and treating it as green is exactly the silent failure this
# module exists to prevent.
_FAILING_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "action_required", "startup_failure"}
)
_PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
_INCONCLUSIVE_CONCLUSIONS = frozenset({"cancelled", "stale"})

_STATUS_MARKER_ROLE = "status"

COPILOT_ASSIGNEE_LOGINS = frozenset(
    {"copilot", "copilot-swe-agent", "copilot-swe-agent[bot]", "github-copilot[bot]"}
)

__all__ = [
    "CHECKS_GREEN",
    "CHECKS_PENDING",
    "CHECKS_RED",
    "CHECKS_UNKNOWN",
    "COPILOT_ASSIGNEE_LOGINS",
    "PULL_REQUEST_CHECK_LIMIT",
    "PULL_REQUEST_DISPOSITIONS",
    "PULL_REQUEST_SCHEMA_VERSION",
    "REVIEW_APPROVED",
    "REVIEW_CHANGES_REQUESTED",
    "REVIEW_REQUIRED",
    "build_pull_request_comment_proposals",
    "build_pull_request_current_state",
    "build_pull_request_handoff",
    "merge_pull_request_judgments",
    "pull_request_assigned_to_copilot",
    "pull_request_human_decision_reason",
    "pull_request_requires_human_decision",
    "render_pull_request_section",
    "unknown_pull_request_current_state",
    "validate_pull_request_judgments",
]


# ---------------------------------------------------------------------------
# Current-state normalization
# ---------------------------------------------------------------------------


def unknown_pull_request_current_state(reason: str) -> dict[str, Any]:
    """The conservative state used when no current evidence could be derived."""
    return {
        "headSha": None,
        "checks": {
            "source": "none",
            "state": CHECKS_UNKNOWN,
            "total": 0,
            "failing": [],
            "pending": [],
            "truncated": False,
            "complete": False,
        },
        "review": {
            "decision": REVIEW_REQUIRED,
            "reviewers": [],
            "complete": False,
        },
        "mergeable": None,
        "mergeableState": None,
        "draft": False,
        "complete": False,
        "incompleteReasons": [reason],
    }


def build_pull_request_current_state(
    pull_request: Mapping[str, Any],
    *,
    check_runs: Sequence[Any] | None = None,
    combined_status: Mapping[str, Any] | None = None,
    reviews: Sequence[Any] | None = None,
    limit: int = PULL_REQUEST_CHECK_LIMIT,
) -> dict[str, Any]:
    """Summarize one pull request's current CI and review state.

    ``check_runs``/``combined_status``/``reviews`` are ``None`` when the
    corresponding GET could not be completed. ``None`` and "empty list" are
    different facts: an empty check-run list with a successful combined status
    is a real green, while a failed fetch leaves the state incomplete.
    """
    if not isinstance(pull_request, Mapping):
        raise TypeError("Pull request payload must be an object.")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("Check limit must be a positive integer.")

    head = pull_request.get("head")
    head_sha = head.get("sha") if isinstance(head, Mapping) else None
    if not isinstance(head_sha, str) or not head_sha.strip():
        head_sha = None

    incomplete: list[str] = []
    if head_sha is None:
        incomplete.append("pull request head commit is unknown")

    checks = _normalize_checks(
        check_runs,
        combined_status,
        head_sha=head_sha,
        limit=limit,
    )
    if not checks["complete"]:
        incomplete.append(
            f"current head-commit check conclusion is {checks['state']}"
        )

    review = _normalize_reviews(reviews)
    if not review["complete"]:
        incomplete.append("current review state is unavailable")

    mergeable = pull_request.get("mergeable")
    if not isinstance(mergeable, bool):
        mergeable = None
    mergeable_state = pull_request.get("mergeable_state")
    if not isinstance(mergeable_state, str) or not mergeable_state.strip():
        mergeable_state = None

    return {
        "headSha": head_sha,
        "checks": checks,
        "review": review,
        "mergeable": mergeable,
        "mergeableState": mergeable_state,
        "draft": pull_request.get("draft") is True,
        "complete": head_sha is not None and checks["complete"] and review["complete"],
        "incompleteReasons": incomplete,
    }


def _normalize_checks(
    check_runs: Sequence[Any] | None,
    combined_status: Mapping[str, Any] | None,
    *,
    head_sha: str | None,
    limit: int,
) -> dict[str, Any]:
    if check_runs is None:
        return {
            "source": "none",
            "state": CHECKS_UNKNOWN,
            "total": 0,
            "failing": [],
            "pending": [],
            "truncated": False,
            "complete": False,
        }

    entries = _current_check_entries(check_runs, head_sha=head_sha)
    if entries:
        return _summarize_check_runs(entries, limit=limit)
    if combined_status is not None:
        return _summarize_combined_status(combined_status, head_sha=head_sha, limit=limit)
    # A successful check-run fetch that returned nothing for the head commit is
    # not proof of success; no workflow may have been triggered yet.
    return {
        "source": "check-runs",
        "state": CHECKS_UNKNOWN,
        "total": 0,
        "failing": [],
        "pending": [],
        "truncated": False,
        "complete": False,
    }


def _current_check_entries(
    check_runs: Sequence[Any] | None,
    *,
    head_sha: str | None,
) -> list[Mapping[str, Any]]:
    if check_runs is None:
        return []
    if isinstance(check_runs, (str, bytes)) or not isinstance(check_runs, Sequence):
        raise TypeError("Check runs must be a sequence.")
    entries: list[Mapping[str, Any]] = []
    for raw in check_runs:
        if not isinstance(raw, Mapping):
            continue
        # The check-runs endpoint is already scoped to one commit, but a
        # hand-assembled payload must not be able to smuggle in a conclusion
        # from an earlier push.
        run_sha = raw.get("head_sha")
        if (
            head_sha is not None
            and isinstance(run_sha, str)
            and run_sha
            and run_sha != head_sha
        ):
            continue
        entries.append(raw)
    return entries


def _summarize_check_runs(
    entries: Sequence[Mapping[str, Any]],
    *,
    limit: int,
) -> dict[str, Any]:
    failing: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    inconclusive = 0
    for entry in entries:
        name = entry.get("name")
        name = name if isinstance(name, str) and name.strip() else "(unnamed check)"
        url = entry.get("html_url")
        url = url if isinstance(url, str) and url.strip() else None
        status = entry.get("status")
        conclusion = entry.get("conclusion")
        conclusion = conclusion if isinstance(conclusion, str) else None

        if status != "completed":
            pending.append(
                {"name": name, "status": str(status or "unknown"), "url": url}
            )
            continue
        if conclusion in _FAILING_CONCLUSIONS:
            failing.append({"name": name, "conclusion": conclusion, "url": url})
            continue
        if conclusion in _PASSING_CONCLUSIONS:
            continue
        # `cancelled`, `stale`, and a completed run with no conclusion at all.
        inconclusive += 1

    state = _check_state(
        failing=bool(failing),
        pending=bool(pending),
        inconclusive=bool(inconclusive),
        total=len(entries),
    )
    return {
        "source": "check-runs",
        "state": state,
        "total": len(entries),
        "failing": _sorted_bounded(failing, limit),
        "pending": _sorted_bounded(pending, limit),
        "truncated": len(failing) > limit or len(pending) > limit,
        "complete": state in {CHECKS_GREEN, CHECKS_RED},
    }


def _summarize_combined_status(
    combined_status: Mapping[str, Any],
    *,
    head_sha: str | None,
    limit: int,
) -> dict[str, Any]:
    if not isinstance(combined_status, Mapping):
        raise TypeError("Combined status must be an object.")
    status_sha = combined_status.get("sha")
    if (
        head_sha is not None
        and isinstance(status_sha, str)
        and status_sha
        and status_sha != head_sha
    ):
        return {
            "source": "combined-status",
            "state": CHECKS_UNKNOWN,
            "total": 0,
            "failing": [],
            "pending": [],
            "truncated": False,
            "complete": False,
        }

    raw_statuses = combined_status.get("statuses")
    statuses = [item for item in raw_statuses if isinstance(item, Mapping)] if isinstance(raw_statuses, list) else []
    failing = [
        {
            "name": str(item.get("context") or "(unnamed status)"),
            "conclusion": str(item.get("state")),
            "url": item.get("target_url") if isinstance(item.get("target_url"), str) else None,
        }
        for item in statuses
        if item.get("state") in {"failure", "error"}
    ]
    pending = [
        {
            "name": str(item.get("context") or "(unnamed status)"),
            "status": "pending",
            "url": item.get("target_url") if isinstance(item.get("target_url"), str) else None,
        }
        for item in statuses
        if item.get("state") == "pending"
    ]

    raw_state = combined_status.get("state")
    if raw_state in {"failure", "error"}:
        state = CHECKS_RED
    elif raw_state == "pending":
        state = CHECKS_PENDING
    elif raw_state == "success" and statuses:
        state = CHECKS_GREEN
    else:
        # A "success" combined status with no contexts means nothing reported.
        state = CHECKS_UNKNOWN
    return {
        "source": "combined-status",
        "state": state,
        "total": len(statuses),
        "failing": _sorted_bounded(failing, limit),
        "pending": _sorted_bounded(pending, limit),
        "truncated": len(failing) > limit or len(pending) > limit,
        "complete": state in {CHECKS_GREEN, CHECKS_RED},
    }


def _check_state(
    *,
    failing: bool,
    pending: bool,
    inconclusive: bool,
    total: int,
) -> str:
    if failing:
        return CHECKS_RED
    if pending:
        return CHECKS_PENDING
    if inconclusive or total == 0:
        return CHECKS_UNKNOWN
    return CHECKS_GREEN


def _sorted_bounded(
    entries: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    return sorted(entries, key=lambda entry: str(entry.get("name")))[:limit]


def _normalize_reviews(reviews: Sequence[Any] | None) -> dict[str, Any]:
    if reviews is None:
        return {"decision": REVIEW_REQUIRED, "reviewers": [], "complete": False}
    if isinstance(reviews, (str, bytes)) or not isinstance(reviews, Sequence):
        raise TypeError("Reviews must be a sequence.")

    # GitHub returns reviews in submission order, so the last decisive review by
    # a login is that reviewer's current position. COMMENTED and PENDING
    # reviews carry no position and must not overwrite an earlier decision.
    latest: dict[str, str] = {}
    for raw in reviews:
        if not isinstance(raw, Mapping):
            continue
        user = raw.get("user")
        login = user.get("login") if isinstance(user, Mapping) else None
        state = raw.get("state")
        if not isinstance(login, str) or not login.strip():
            continue
        if state == "APPROVED":
            latest[login] = REVIEW_APPROVED
        elif state == "CHANGES_REQUESTED":
            latest[login] = REVIEW_CHANGES_REQUESTED
        elif state == "DISMISSED":
            latest.pop(login, None)

    reviewers = [
        {"login": login, "state": latest[login]} for login in sorted(latest)
    ]
    if any(entry["state"] == REVIEW_CHANGES_REQUESTED for entry in reviewers):
        decision = REVIEW_CHANGES_REQUESTED
    elif reviewers:
        decision = REVIEW_APPROVED
    else:
        decision = REVIEW_REQUIRED
    return {"decision": decision, "reviewers": reviewers, "complete": True}


# ---------------------------------------------------------------------------
# Human-decision proof
# ---------------------------------------------------------------------------


def pull_request_human_decision_reason(task: Mapping[str, Any]) -> str | None:
    """Name the decision only a person can make, or ``None`` if there is none.

    ``ping-human`` is reserved for a decision, permission, ownership, or access
    question. Missing machine-fetchable evidence is never one: that is
    ``watch`` or ``investigate``.
    """
    if not isinstance(task, Mapping):
        raise TypeError("Pull request task must be an object.")
    state = task.get("currentState")
    if not isinstance(state, Mapping):
        return None
    review = state.get("review")
    if (
        isinstance(review, Mapping)
        and review.get("decision") == REVIEW_CHANGES_REQUESTED
    ):
        return "a reviewer requested changes and only the author or reviewer can resolve them"
    if state.get("mergeableState") == "dirty" or state.get("mergeable") is False:
        return "the branch conflicts with its base and only a person can choose the resolution"
    return None


def pull_request_requires_human_decision(task: Mapping[str, Any]) -> bool:
    return pull_request_human_decision_reason(task) is not None


def pull_request_assigned_to_copilot(pull_request: Mapping[str, Any]) -> bool:
    """Detect Copilot assignment from either inventory or raw API shapes."""
    if not isinstance(pull_request, Mapping):
        raise TypeError("Pull request must be an object.")
    assignees = pull_request.get("assignees")
    if not isinstance(assignees, list):
        return False
    for assignee in assignees:
        login = (
            assignee.get("login")
            if isinstance(assignee, Mapping)
            else assignee
        )
        if isinstance(login, str) and login.casefold() in COPILOT_ASSIGNEE_LOGINS:
            return True
    return False


# ---------------------------------------------------------------------------
# Handoff
# ---------------------------------------------------------------------------


def build_pull_request_handoff(
    snapshot: object,
    *,
    previous_snapshot: object | None = None,
) -> dict[str, object]:
    validate_snapshot(snapshot)
    if not isinstance(snapshot, Mapping):
        raise TypeError("Snapshot must be an object.")
    repository = snapshot.get("repository")
    collected_at = snapshot.get("collectedAt")
    if not isinstance(repository, str) or not isinstance(collected_at, str):
        raise ValidationError("Snapshot identity is incomplete.")

    previous_by_number: dict[int, Mapping[str, Any]] = {}
    previous_evidence: Mapping[str, Any] = {}
    if previous_snapshot is not None:
        validate_snapshot(previous_snapshot)
        if not isinstance(previous_snapshot, Mapping):
            raise TypeError("Previous snapshot must be an object.")
        if str(previous_snapshot.get("repository")).casefold() != repository.casefold():
            raise ValidationError("Previous snapshot repository does not match.")
        previous_by_number = _pull_requests_by_number(previous_snapshot)
        raw_previous_evidence = previous_snapshot.get("evidence")
        if not isinstance(raw_previous_evidence, Mapping):
            raise TypeError("Previous snapshot evidence must be an object.")
        previous_evidence = raw_previous_evidence

    evidence = snapshot.get("evidence")
    if not isinstance(evidence, Mapping):
        raise TypeError("Snapshot evidence must be an object.")

    tasks: list[dict[str, object]] = []
    excluded: list[dict[str, object]] = []
    for number, pull_request in sorted(_pull_requests_by_number(snapshot).items()):
        if pull_request_assigned_to_copilot(pull_request):
            # Copilot owns the change; commenting would interrupt its own loop.
            excluded.append({"number": number, "reason": "assigned-to-copilot"})
            continue
        previous = previous_by_number.get(number)
        if (
            previous is not None
            and _pull_request_change_key(
                previous,
                previous_evidence.get(f"pr:{number}"),
            )
            == _pull_request_change_key(
                pull_request,
                evidence.get(f"pr:{number}"),
            )
        ):
            excluded.append({"number": number, "reason": "unchanged-stable"})
            continue

        evidence_id = f"pr:{number}"
        record = evidence.get(evidence_id)
        current_state = _task_current_state(record)
        evidence_status = (
            "complete" if current_state["complete"] else "incomplete"
        )
        allowed = _allowed_pull_request_dispositions(current_state)
        task: dict[str, object] = {
            "target": {"kind": "pull-request", "number": number},
            "url": pull_request.get("url"),
            "title": pull_request.get("title"),
            "author": pull_request.get("author"),
            "labels": list(pull_request.get("labels", [])),
            "selectionReasons": list(pull_request.get("selectionReasons", [])),
            "changeClass": "new" if previous is None else "changed",
            "evidenceIds": [evidence_id],
            "evidenceStatus": evidence_status,
            "currentState": current_state,
            "questions": [
                (
                    "Does this pull request still address an active CI "
                    "failure or automation breakage?"
                ),
                (
                    "What do the current check conclusions and review state "
                    "permit: investigate, watch, request human input, or no action?"
                ),
            ],
            "stopConditions": [
                (
                    "Stop without proposing an action if the pull request "
                    "is assigned to Copilot."
                ),
                (
                    "Use watch when current check or review evidence is "
                    "incomplete; never infer success from age."
                ),
                (
                    "Never propose closing, merging, or otherwise withdrawing "
                    "a pull request."
                ),
            ],
            "allowedDispositions": allowed,
        }
        task["humanDecision"] = pull_request_human_decision_reason(task)
        task["defaultJudgment"] = _default_pull_request_judgment(
            number,
            current_state,
            evidence_id,
            allowed,
        )
        tasks.append(task)

    return {
        "schemaVersion": PULL_REQUEST_SCHEMA_VERSION,
        "repository": repository,
        "snapshotId": f"snapshot:{repository}:{collected_at}",
        "tasks": tasks,
        "excluded": excluded,
    }


def _pull_request_change_key(
    pull_request: Mapping[str, Any],
    evidence_record: object,
) -> dict[str, Any]:
    return {
        "updatedAt": pull_request.get("updatedAt"),
        "title": pull_request.get("title"),
        "labels": pull_request.get("labels"),
        "assignees": pull_request.get("assignees"),
        "currentState": _task_current_state(evidence_record),
    }


def _task_current_state(record: object) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        return unknown_pull_request_current_state(
            "no pull request evidence was collected"
        )
    if record.get("availability") != "available":
        return unknown_pull_request_current_state(
            "pull request evidence collection was incomplete"
        )
    payload = record.get("payload")
    state = payload.get("currentState") if isinstance(payload, Mapping) else None
    if not isinstance(state, Mapping):
        return unknown_pull_request_current_state(
            "pull request evidence carries no current check or review state"
        )
    return copy.deepcopy(dict(state))


def _allowed_pull_request_dispositions(current_state: Mapping[str, Any]) -> list[str]:
    if not current_state.get("complete"):
        return ["watch"]
    allowed = {"investigate", "no-action", "watch"}
    review = current_state.get("review")
    mergeable_state = current_state.get("mergeableState")
    if (
        isinstance(review, Mapping)
        and review.get("decision") == REVIEW_CHANGES_REQUESTED
    ) or mergeable_state == "dirty" or current_state.get("mergeable") is False:
        allowed.add("ping-human")
    return sorted(allowed)


def _default_pull_request_judgment(
    number: int,
    current_state: Mapping[str, Any],
    evidence_id: str,
    allowed: Sequence[str],
) -> dict[str, Any]:
    checks = current_state.get("checks")
    check_state = checks.get("state") if isinstance(checks, Mapping) else CHECKS_UNKNOWN
    reasons = current_state.get("incompleteReasons")
    reason_text = (
        "; ".join(str(reason) for reason in reasons)
        if isinstance(reasons, list) and reasons
        else "current evidence is incomplete"
    )

    if not current_state.get("complete"):
        return {
            "pullRequestNumber": number,
            "disposition": "watch",
            "summary": (
                "Current pull request evidence is incomplete, so no conclusion "
                f"is available yet ({reason_text})."
            ),
            "evidenceIds": [evidence_id],
            "missingEvidence": [
                str(reason)
                for reason in (reasons if isinstance(reasons, list) else [])
            ]
            or ["a decisive head-commit check conclusion"],
            "reassessWhen": (
                "After the current head commit reports a complete check "
                "conclusion and the review state is readable."
            ),
        }
    if check_state == CHECKS_RED:
        return {
            "pullRequestNumber": number,
            "disposition": "investigate",
            "summary": (
                "The current head commit has failing checks, so this pull "
                "request needs a look before it can merge."
            ),
            "evidenceIds": [evidence_id],
            "missingEvidence": [],
        }
    if "ping-human" in allowed:
        return {
            "pullRequestNumber": number,
            "disposition": "watch",
            "summary": (
                "Checks are green but the pull request is blocked on a person, "
                "so automation waits rather than acting."
            ),
            "evidenceIds": [evidence_id],
            "missingEvidence": [],
            "reassessWhen": (
                "After the blocking review or merge conflict is resolved."
            ),
        }
    return {
        "pullRequestNumber": number,
        "disposition": "no-action",
        "summary": (
            "The current head commit reports green checks and no blocking "
            "review, so no shepherd action is needed."
        ),
        "evidenceIds": [evidence_id],
        "missingEvidence": [],
    }


def _pull_requests_by_number(
    snapshot: Mapping[str, Any],
) -> dict[int, Mapping[str, Any]]:
    raw_pull_requests = snapshot.get("pullRequests", [])
    if not isinstance(raw_pull_requests, list):
        raise ValidationError("Snapshot pullRequests must be a list.")
    result: dict[int, Mapping[str, Any]] = {}
    for raw_pull_request in raw_pull_requests:
        if not isinstance(raw_pull_request, Mapping):
            raise ValidationError("Snapshot pull request must be an object.")
        number = raw_pull_request.get("number")
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or number < 1
        ):
            raise ValidationError(
                "Snapshot pull request number must be a positive integer."
            )
        if number in result:
            raise ValidationError(
                f"Duplicate pull request in snapshot: {number}."
            )
        result[number] = raw_pull_request
    return result


# ---------------------------------------------------------------------------
# Judgment contract
# ---------------------------------------------------------------------------


_JUDGMENT_DOCUMENT_FIELDS = frozenset({"schemaVersion", "snapshotId", "pullRequests"})
_JUDGMENT_FIELDS = frozenset(
    {
        "pullRequestNumber",
        "disposition",
        "summary",
        "evidenceIds",
        "missingEvidence",
        "reassessWhen",
        "humanEscalation",
    }
)
_ESCALATION_FIELDS = frozenset(
    {"context", "whyHuman", "question", "suggestedNextSteps", "routingHint"}
)


def validate_pull_request_judgments(
    handoff: object,
    judgments: object,
) -> dict[str, Any]:
    """Validate sparse pull-request judgments against their handoff.

    Only pull requests that appear as handoff tasks may be judged. A stable
    unchanged pull request has no task, so it needs no judgment; a judgment for
    one is an error rather than something to silently drop, because a discarded
    override is indistinguishable from agreement.
    """
    validated_handoff = _validate_handoff(handoff)
    document = _require_mapping(judgments, "pull request judgments")
    if document.get("schemaVersion") != PULL_REQUEST_SCHEMA_VERSION:
        raise ValidationError("Pull request judgment schemaVersion must be 1.")
    unsupported = set(document) - _JUDGMENT_DOCUMENT_FIELDS
    if unsupported:
        raise ValidationError(
            f"Pull request judgments have unsupported fields: {sorted(unsupported)}."
        )
    if document.get("snapshotId") != validated_handoff["snapshotId"]:
        raise ValidationError(
            "Pull request judgment snapshotId must match the handoff."
        )

    tasks_by_number = {
        int(task["target"]["number"]): task for task in validated_handoff["tasks"]
    }
    raw_judgments = document.get("pullRequests")
    if not isinstance(raw_judgments, list):
        raise ValidationError("Pull request judgments must be a list.")

    seen: set[int] = set()
    validated: dict[int, dict[str, Any]] = {}
    for raw_judgment in raw_judgments:
        judgment = _require_mapping(raw_judgment, "pull request judgment")
        unsupported = set(judgment) - _JUDGMENT_FIELDS
        if unsupported:
            raise ValidationError(
                f"Pull request judgment has unsupported fields: {sorted(unsupported)}."
            )
        number = judgment.get("pullRequestNumber")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ValidationError(
                "Pull request judgment pullRequestNumber must be a positive integer."
            )
        if number in seen:
            raise ValidationError(f"Duplicate judgment for pull request {number}.")
        seen.add(number)
        task = tasks_by_number.get(number)
        if task is None:
            raise ValidationError(
                f"Pull request {number} was not handed off for review."
            )
        validated[number] = _validate_pull_request_judgment(judgment, task, number)
    return {
        "schemaVersion": PULL_REQUEST_SCHEMA_VERSION,
        "snapshotId": validated_handoff["snapshotId"],
        "pullRequests": [validated[number] for number in sorted(validated)],
    }


def _validate_pull_request_judgment(
    judgment: Mapping[str, Any],
    task: Mapping[str, Any],
    number: int,
) -> dict[str, Any]:
    disposition = judgment.get("disposition")
    if disposition not in PULL_REQUEST_DISPOSITIONS:
        raise ValidationError(
            f"Pull request {number} disposition {disposition!r} is unsupported; "
            f"allowed dispositions are {sorted(PULL_REQUEST_DISPOSITIONS)}."
        )
    allowed = task.get("allowedDispositions")
    if not isinstance(allowed, list) or disposition not in allowed:
        raise ValidationError(
            f"Pull request {number} may not be judged {disposition!r}; "
            f"allowed dispositions are {sorted(allowed or [])}."
        )

    summary = judgment.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValidationError(
            f"Pull request {number} judgment must include a nonempty summary."
        )

    task_evidence = task.get("evidenceIds")
    if not isinstance(task_evidence, list):
        raise ValidationError(f"Pull request {number} task evidence is malformed.")
    evidence_ids = judgment.get("evidenceIds")
    if not isinstance(evidence_ids, list) or not evidence_ids:
        raise ValidationError(
            f"Pull request {number} judgment must cite at least one evidence ID."
        )
    seen_evidence: set[str] = set()
    for evidence_id in evidence_ids:
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            raise ValidationError(
                f"Pull request {number} evidenceIds must be nonempty strings."
            )
        if evidence_id in seen_evidence:
            raise ValidationError(
                f"Pull request {number} repeats evidenceId {evidence_id}."
            )
        seen_evidence.add(evidence_id)
        if evidence_id not in task_evidence:
            raise ValidationError(
                f"Pull request {number} cites evidence {evidence_id} outside its "
                "handed-off evidence."
            )

    missing_evidence = judgment.get("missingEvidence", [])
    if not isinstance(missing_evidence, list) or not all(
        isinstance(value, str) and value.strip() for value in missing_evidence
    ):
        raise ValidationError(
            f"Pull request {number} missingEvidence must contain nonempty strings."
        )

    # Structured current evidence is the gate for every conclusive disposition.
    # Without it the only defensible answer is to keep watching.
    current_state = task.get("currentState")
    if disposition in _CONCLUSIVE_DISPOSITIONS and not (
        isinstance(current_state, Mapping) and current_state.get("complete") is True
    ):
        raise ValidationError(
            f"Pull request {number} may not be judged {disposition!r} without "
            "complete current check and review evidence."
        )

    reassess_when = judgment.get("reassessWhen")
    if disposition == "watch":
        if not isinstance(reassess_when, str) or not reassess_when.strip():
            raise ValidationError(
                f"Pull request {number} watch must name reassessWhen."
            )
    elif reassess_when is not None and (
        not isinstance(reassess_when, str) or not reassess_when.strip()
    ):
        raise ValidationError(
            f"Pull request {number} reassessWhen must be a nonempty string."
        )

    escalation = judgment.get("humanEscalation")
    if disposition == "ping-human":
        if not pull_request_requires_human_decision(task):
            raise ValidationError(
                f"Pull request {number} ping-human requires a reported human "
                "decision in its current state."
            )
        _validate_human_escalation(escalation, number)
    elif escalation is not None:
        raise ValidationError(
            f"Pull request {number} humanEscalation is only valid for ping-human."
        )
    return copy.deepcopy(dict(judgment))


def _validate_human_escalation(value: object, number: int) -> None:
    escalation = _require_mapping(value, f"pull request {number} humanEscalation")
    unsupported = set(escalation) - _ESCALATION_FIELDS
    if unsupported:
        raise ValidationError(
            f"Pull request {number} humanEscalation has unsupported fields: "
            f"{sorted(unsupported)}."
        )
    for field in ("context", "whyHuman", "question", "routingHint"):
        field_value = escalation.get(field)
        if not isinstance(field_value, str) or not field_value.strip():
            raise ValidationError(
                f"Pull request {number} humanEscalation.{field} must be a "
                "nonempty string."
            )
    steps = escalation.get("suggestedNextSteps")
    if (
        not isinstance(steps, list)
        or not steps
        or not all(isinstance(step, str) and step.strip() for step in steps)
    ):
        raise ValidationError(
            f"Pull request {number} humanEscalation.suggestedNextSteps must "
            "contain at least one nonempty step."
        )


def merge_pull_request_judgments(
    handoff: object,
    judgments: object,
) -> dict[str, Any]:
    """Apply sparse judgments over the deterministic defaults.

    Silence means "the deterministic default stands", so a cycle that reviews
    two of twelve handed-off pull requests still produces a complete, validated
    judgment for every task.
    """
    validated_handoff = _validate_handoff(handoff)
    validated = validate_pull_request_judgments(handoff, judgments)
    overrides = {
        int(judgment["pullRequestNumber"]): judgment
        for judgment in validated["pullRequests"]
    }
    merged: list[dict[str, Any]] = []
    for task in validated_handoff["tasks"]:
        number = int(task["target"]["number"])
        override = overrides.get(number)
        if override is not None:
            merged.append(copy.deepcopy(override))
            continue
        default = task.get("defaultJudgment")
        if not isinstance(default, Mapping):
            raise ValidationError(
                f"Pull request {number} task is missing its default judgment."
            )
        merged.append(copy.deepcopy(dict(default)))
    return {
        "schemaVersion": PULL_REQUEST_SCHEMA_VERSION,
        "snapshotId": validated_handoff["snapshotId"],
        "pullRequests": merged,
    }


def _validate_handoff(handoff: object) -> dict[str, Any]:
    document = _require_mapping(handoff, "pull request handoff")
    if document.get("schemaVersion") != PULL_REQUEST_SCHEMA_VERSION:
        raise ValidationError("Pull request handoff schemaVersion must be 1.")
    snapshot_id = document.get("snapshotId")
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise ValidationError("Pull request handoff snapshotId must be a string.")
    repository = document.get("repository")
    if not isinstance(repository, str) or not repository.strip():
        raise ValidationError("Pull request handoff repository must be a string.")
    raw_tasks = document.get("tasks")
    if not isinstance(raw_tasks, list):
        raise ValidationError("Pull request handoff tasks must be a list.")
    raw_excluded = document.get("excluded", [])
    if not isinstance(raw_excluded, list):
        raise ValidationError("Pull request handoff excluded must be a list.")

    tasks: list[Mapping[str, Any]] = []
    seen: set[int] = set()
    for raw_task in raw_tasks:
        task = _require_mapping(raw_task, "pull request task")
        target = task.get("target")
        if (
            not isinstance(target, Mapping)
            or target.get("kind") != "pull-request"
        ):
            raise ValidationError("Pull request task target must be a pull request.")
        number = target.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ValidationError(
                "Pull request task target number must be a positive integer."
            )
        if number in seen:
            raise ValidationError(f"Duplicate pull request task: {number}.")
        seen.add(number)
        tasks.append(task)
    excluded: list[Mapping[str, Any]] = []
    for raw_entry in raw_excluded:
        entry = _require_mapping(raw_entry, "pull request exclusion")
        number = entry.get("number")
        reason = entry.get("reason")
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or number < 1
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            raise ValidationError(
                "Pull request exclusion must contain a positive number and reason."
            )
        excluded.append(entry)
    return {
        "schemaVersion": PULL_REQUEST_SCHEMA_VERSION,
        "repository": repository,
        "snapshotId": snapshot_id,
        "tasks": tasks,
        "excluded": excluded,
    }


def _require_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{label.capitalize()} must be an object.")
    return value


# ---------------------------------------------------------------------------
# Deterministic markdown
# ---------------------------------------------------------------------------


_MARKDOWN_QUEUES = (
    ("Investigate", "investigate"),
    ("Watch", "watch"),
    ("Needs human", "ping-human"),
    ("No action", "no-action"),
)


def _markdown_text(value: object) -> str:
    return " ".join(str(value).split()).replace("|", "\\|")


def render_pull_request_section(
    handoff: object,
    judgments: object,
) -> str:
    """Render the deterministic pull-request section of the cycle report."""
    validated_handoff = _validate_handoff(handoff)
    merged = merge_pull_request_judgments(handoff, judgments)
    tasks_by_number = {
        int(task["target"]["number"]): task for task in validated_handoff["tasks"]
    }
    excluded = validated_handoff["excluded"]
    exclusion_counts: dict[str, int] = {}
    for entry in excluded:
        reason = str(entry["reason"])
        exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
    excluded_summary = (
        " ("
        + ", ".join(
            f"`{reason}`: {count}"
            for reason, count in sorted(exclusion_counts.items())
        )
        + ")"
        if exclusion_counts
        else ""
    )

    lines = [
        "## Pull requests",
        "",
        (
            f"**Handoff:** {len(merged['pullRequests'])} selected; "
            f"{len(excluded)} excluded{excluded_summary}."
        ),
        "",
    ]
    if not merged["pullRequests"]:
        lines.append("No new or changed pull requests required review.")
        lines.append("")
        return "\n".join(lines)

    by_disposition: dict[str, list[dict[str, Any]]] = {}
    for judgment in merged["pullRequests"]:
        by_disposition.setdefault(str(judgment["disposition"]), []).append(judgment)

    for heading, disposition in _MARKDOWN_QUEUES:
        entries = sorted(
            by_disposition.get(disposition, []),
            key=lambda entry: int(entry["pullRequestNumber"]),
        )
        if not entries:
            continue
        lines.extend(
            [
                f"### {heading}",
                "",
                "| Pull request | Checks | Review | Evidence | Summary |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for entry in entries:
            number = int(entry["pullRequestNumber"])
            task = tasks_by_number[number]
            lines.append(_render_pull_request_row(number, task, entry))
        lines.append("")
    return "\n".join(lines)


def _render_pull_request_row(
    number: int,
    task: Mapping[str, Any],
    judgment: Mapping[str, Any],
) -> str:
    url = task.get("url")
    title = task.get("title")
    label = f"[#{number}]({url})" if isinstance(url, str) and url else f"#{number}"
    if isinstance(title, str) and title:
        label += f" {_markdown_text(title)}"

    state = task.get("currentState")
    state = state if isinstance(state, Mapping) else {}
    checks = state.get("checks")
    checks = checks if isinstance(checks, Mapping) else {}
    review = state.get("review")
    review = review if isinstance(review, Mapping) else {}

    check_state = str(checks.get("state", CHECKS_UNKNOWN))
    failing = checks.get("failing")
    if check_state == CHECKS_RED and isinstance(failing, list) and failing:
        check_text = f"{check_state} ({len(failing)} failing)"
    else:
        check_text = check_state

    evidence_ids = judgment.get("evidenceIds")
    evidence_text = (
        ", ".join(f"`{_markdown_text(value)}`" for value in evidence_ids)
        if isinstance(evidence_ids, list) and evidence_ids
        else "—"
    )
    return (
        f"| {label} | {_markdown_text(check_text)} "
        f"| {_markdown_text(review.get('decision', REVIEW_REQUIRED))} "
        f"| {evidence_text} | {_markdown_text(judgment.get('summary', ''))} |"
    )


# ---------------------------------------------------------------------------
# Comment proposals
# ---------------------------------------------------------------------------


def _idempotency_key(number: int) -> str:
    return f"pull-request:{number}:status"


def _status_markers(number: int) -> str:
    return (
        f"<!-- ci-shepherd:role={_STATUS_MARKER_ROLE} -->\n"
        f"<!-- ci-shepherd:idempotency-key={_idempotency_key(number)} -->"
    )


def _evidence_lines(
    snapshot: Mapping[str, Any],
    evidence_ids: Sequence[str],
) -> list[str]:
    evidence = snapshot.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    lines: list[str] = []
    for evidence_id in evidence_ids:
        record = evidence.get(evidence_id)
        url = record.get("url") if isinstance(record, Mapping) else None
        lines.append(
            f"- [{evidence_id}]({url})"
            if isinstance(url, str) and url
            else f"- `{evidence_id}`"
        )
    return lines


def _check_summary_lines(current_state: Mapping[str, Any]) -> list[str]:
    checks = current_state.get("checks")
    checks = checks if isinstance(checks, Mapping) else {}
    review = current_state.get("review")
    review = review if isinstance(review, Mapping) else {}
    lines = [
        f"- Head commit: `{current_state.get('headSha') or 'unknown'}`",
        (
            f"- Checks: {checks.get('state', CHECKS_UNKNOWN)} "
            f"(source `{checks.get('source', 'none')}`, "
            f"{checks.get('total', 0)} reported)"
        ),
        f"- Review: {review.get('decision', REVIEW_REQUIRED)}",
    ]
    failing = checks.get("failing")
    if isinstance(failing, list):
        lines.extend(
            f"  - failing: `{entry.get('name')}` ({entry.get('conclusion')})"
            for entry in failing
            if isinstance(entry, Mapping)
        )
    pending = checks.get("pending")
    if isinstance(pending, list):
        lines.extend(
            f"  - pending: `{entry.get('name')}` ({entry.get('status')})"
            for entry in pending
            if isinstance(entry, Mapping)
        )
    return lines


def _render_ping_human_body(
    number: int,
    judgment: Mapping[str, Any],
    task: Mapping[str, Any],
    snapshot: Mapping[str, Any],
) -> str:
    escalation = judgment.get("humanEscalation")
    if not isinstance(escalation, Mapping):
        raise ValidationError(
            f"Pull request {number} ping-human must include humanEscalation."
        )
    steps = escalation.get("suggestedNextSteps")
    steps = steps if isinstance(steps, list) else []
    current_state = task.get("currentState")
    current_state = current_state if isinstance(current_state, Mapping) else {}
    evidence_ids = judgment.get("evidenceIds")
    evidence_ids = evidence_ids if isinstance(evidence_ids, list) else []
    return "\n".join(
        [
            f"[automated] {escalation['context']}",
            "",
            f"**Current assessment:** {judgment['summary']}",
            "",
            "**Current state:**",
            *_check_summary_lines(current_state),
            "",
            "**Evidence reviewed:**",
            *_evidence_lines(snapshot, evidence_ids),
            "",
            f"**Why human input is needed:** {escalation['whyHuman']}",
            "",
            f"**Decision needed:** {escalation['question']}",
            "",
            "**Suggested next steps:**",
            *(f"- {step}" for step in steps),
            "",
            f"**Routing hint:** `{escalation['routingHint']}`",
            "",
            "No merge, closure, rerun, or other change has been made to this "
            "pull request.",
            "",
            _status_markers(number),
        ]
    )


def _render_retired_human_request_body(
    number: int,
) -> str:
    return "\n".join(
        [
            (
                "[automated] The CI shepherd no longer needs the prior human "
                "decision on this pull request."
            ),
            "",
            (
                "The current assessment no longer requires human input. Future "
                "watch or investigation state is tracked in the shepherd report "
                "instead of being posted here."
            ),
            "",
            "No merge, closure, rerun, or other change has been made to this "
            "pull request.",
            "",
            _status_markers(number),
        ]
    )


def _owned_status_comments(
    snapshot: Mapping[str, Any],
    number: int,
    idempotency_key: str,
) -> list[Mapping[str, Any]]:
    evidence = snapshot.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    record = evidence.get(f"pr:{number}")
    payload = record.get("payload") if isinstance(record, Mapping) else None
    comments = (
        payload.get("shepherdStatusComments") if isinstance(payload, Mapping) else None
    )
    if not isinstance(comments, list):
        return []
    return [
        comment
        for comment in comments
        if isinstance(comment, Mapping)
        and comment.get("idempotencyKey") == idempotency_key
    ]


def build_pull_request_comment_proposals(
    snapshot: object,
    handoff: object,
    judgments: object,
    shepherd_author: str,
) -> dict[str, object]:
    """Render exact comment proposals for the pull requests that need one.

    Only ``ping-human`` creates a visible status comment. ``watch``,
    ``investigate``, and ``no-action`` remain report-only unless an earlier
    shepherd escalation exists, in which case they edit it to retire the stale
    human request. Closure is not representable at all.
    """
    if not isinstance(snapshot, Mapping):
        raise TypeError("Snapshot must be an object.")
    if not isinstance(shepherd_author, str) or not shepherd_author.strip():
        raise ValidationError("Shepherd author must be nonempty.")

    validated_handoff = _validate_handoff(handoff)
    merged = merge_pull_request_judgments(handoff, judgments)
    tasks_by_number = {
        int(task["target"]["number"]): task for task in validated_handoff["tasks"]
    }
    snapshot_pull_requests = _pull_requests_by_number(snapshot)

    proposals: list[dict[str, object]] = []
    unchanged: list[int] = []
    suppressed: list[dict[str, object]] = []
    for judgment in merged["pullRequests"]:
        number = int(judgment["pullRequestNumber"])
        disposition = str(judgment["disposition"])
        task = tasks_by_number[number]

        # Defense in depth. The collector excludes Copilot-assigned pull
        # requests from the inventory and the actor rechecks assignment at
        # execution time, but a snapshot assembled between those two points
        # must not be able to produce a proposal here either.
        record = snapshot_pull_requests.get(number)
        if record is not None and pull_request_assigned_to_copilot(record):
            suppressed.append({"number": number, "reason": "assigned-to-copilot"})
            continue

        key = _idempotency_key(number)
        existing = _owned_status_comments(snapshot, number, key)
        if len(existing) > 1:
            raise ValidationError(
                f"Pull request {number} has multiple owned status comments."
            )
        if disposition == "ping-human":
            body = _render_ping_human_body(number, judgment, task, snapshot)
        elif existing:
            body = _render_retired_human_request_body(number)
        else:
            continue
        if existing and str(existing[0].get("body") or "").strip() == body.strip():
            unchanged.append(number)
            continue

        url = task.get("url")
        if not isinstance(url, str) or not url:
            raise ValidationError(
                f"Pull request {number} task must carry a pull request URL."
            )
        proposal: dict[str, object] = {
            "actionId": (
                f"{validated_handoff['snapshotId']}:pull-request:{number}"
                f":{disposition}-comment"
            ),
            "targetKind": "pull-request",
            "targetNumber": number,
            "targetUrl": url,
            "expectedTargetState": "open",
            "operation": "edit-comment" if existing else "create-comment",
            "idempotencyKey": key,
            "body": body,
            "evidenceIds": list(judgment["evidenceIds"]),
            "requiresSeparateApproval": True,
        }
        if existing:
            comment_id = existing[0].get("id")
            if not isinstance(comment_id, int) or isinstance(comment_id, bool):
                raise ValidationError(
                    f"Pull request {number} owned status comment has no usable id."
                )
            proposal["commentId"] = comment_id
        proposals.append(proposal)

    proposals.sort(key=lambda item: int(item["targetNumber"]))
    unchanged.sort()
    suppressed.sort(key=lambda item: int(item["number"]))
    return {
        "schemaVersion": PULL_REQUEST_SCHEMA_VERSION,
        "repository": validated_handoff["repository"],
        "snapshotId": validated_handoff["snapshotId"],
        "shepherdAuthor": shepherd_author,
        "proposals": proposals,
        "unchangedPullRequestNumbers": unchanged,
        "suppressedPullRequests": suppressed,
    }
