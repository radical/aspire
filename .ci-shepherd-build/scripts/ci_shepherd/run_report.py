"""Read-only run views derived from the existing evidence and event ledgers."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from html import escape
import math
from typing import Any

from ci_shepherd.lifecycle import latest_occurrence_timestamp


def _rows(value: object) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, (list, tuple)) else []


def _text(value: object) -> str:
    if value is None or value == "":
        return "unknown"
    if isinstance(value, (list, tuple)):
        return "; ".join(_text(item) for item in value) or "none recorded"
    if isinstance(value, Mapping):
        return "; ".join(f"{_text(key)}: {_text(item)}" for key, item in value.items())
    return escape(" ".join(str(value).split()), quote=False).replace("|", "\\|")


def _human_duration(seconds: float) -> str:
    if 0 < seconds < 1:
        return "<1s"
    total = int(seconds + 0.5)
    days, remainder = divmod(total, 86400)
    if days:
        return f"{days}d"
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    return "".join(
        f"{value}{unit}" for value, unit in ((hours, "h"), (minutes, "m"), (seconds, "s")) if value
    ) or "0s"


def _duration(start: object, end: object) -> str:
    try:
        seconds = (datetime.fromisoformat(str(end).replace("Z", "+00:00"))
                   - datetime.fromisoformat(str(start).replace("Z", "+00:00"))).total_seconds()
        if seconds < 0:
            return "unknown"
        return _human_duration(seconds)
    except (TypeError, ValueError):
        return "unknown"


def _number(row: Mapping[str, Any]) -> int | None:
    target = row.get("target", {})
    value = row.get("issueNumber", row.get("pullRequestNumber", row.get("number")))
    if value is None and isinstance(target, Mapping):
        value = target.get("number", target.get("value"))
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _not_after(row: Mapping[str, Any], as_of: str | None) -> bool:
    if as_of is None or not row.get("recordedAt"):
        return True
    try:
        return datetime.fromisoformat(row["recordedAt"].replace("Z", "+00:00")) <= datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False


def _failure_age(assessment: Mapping[str, Any], as_of: str | None) -> str:
    candidates: list[tuple[str, str]] = []
    for subject in _rows((assessment.get("recovery") or {}).get("subjects")):
        occurrence = subject.get("occurrence") or {}
        if (occurrence.get("issueNumber") == assessment.get("issueNumber")
                and not occurrence.get("scopeConflict") and isinstance(occurrence.get("observedAt"), str)):
            candidates.append((occurrence["observedAt"], "recorded failure occurrence"))
    ledger = assessment.get("ledger") or {}
    if ledger.get("schemaRecognized") is True:
        # Only recognized producer rows supply reported dates. Unrelated linked
        # runs, free-form issue text, and updatedAt cannot establish failure age.
        reported = latest_occurrence_timestamp([], _rows(ledger.get("rows")))
        if reported:
            basis = "reported producer ledger"
            if ledger.get("complete") is not True:
                basis += " (incomplete coverage)"
            candidates.append((reported, basis))
    if assessment.get("lastMatchingFailureAt") and assessment.get("lastMatchingFailureBasis"):
        candidates.append((assessment["lastMatchingFailureAt"], assessment["lastMatchingFailureBasis"]))
    observed = []
    cutoff = datetime.fromisoformat(as_of.replace("Z", "+00:00")) if as_of else None
    for value, basis in candidates:
        try:
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
            date_only = len(value) == 10
            if date_only:
                # A reported date is day-precision, not an invented midnight
                # failure. Sort it by its upper bound, as the ledger helper does.
                instant = instant.replace(hour=23, minute=59, second=59, microsecond=999999, tzinfo=timezone.utc)
            elif instant.tzinfo is None:
                continue
            if cutoff is not None and (instant.date() > cutoff.date() if date_only else instant > cutoff):
                continue
        except (TypeError, ValueError):
            continue
        observed.append((instant, value, basis, date_only))
    if not observed:
        return "unknown"
    instant, value, basis, date_only = max(observed)
    if date_only:
        age = f"{(cutoff.date() - instant.date()).days}d (calendar)" if cutoff else "unknown"
    else:
        age = _duration(value, as_of)
    return f"{_text(value)}; age: {age}; basis: {_text(basis)}"


def _progress_age(progress: Mapping[str, Any], as_of: str | None) -> str:
    at = progress.get("at")
    if (progress.get("status") != "observed" or not isinstance(at, str)
            or not progress.get("basis")
            or progress.get("precision") not in ("source-event", "observed-change")
            or not _not_after({"recordedAt": at}, as_of)):
        return "unknown"
    return (
        f"{_text(at)}; age: {_duration(at, as_of)}; basis: {_text(progress['basis'])}; "
        f"precision: {_text(progress['precision'])}"
    )


def _status(disposition: object) -> str:
    return {
        "no-action": "⚪ No action",
        "watch": "⏸ Deferred / watching",
        "ping-human": "👤 Human input",
        "investigate": "🔎 Investigation requested",
    }.get(str(disposition), _text(disposition))


def _owner(row: Mapping[str, Any]) -> str:
    assignees = row.get("assignees")
    if isinstance(assignees, list) and assignees:
        return _text([person.get("login") if isinstance(person, Mapping) else person for person in assignees])
    return _text(row.get("actualOwner"))


def _tracking_summary(records: list[Mapping[str, Any]]) -> str:
    return "; ".join(
        f"task {_text(record.get('taskId'))}: {_text(record.get('lifecycle'))}; "
        + f"task state: {_text(record.get('taskState'))}; "
        + (f"task observation: {_text(record['taskObservation'])}; " if record.get("taskObservation") else "")
        + f"attempt: {_text(record.get('attemptOutcome', record.get('lifecycle')))}; "
        + ("new decision required; " if record.get("requiresNewDecision") is True else "")
        + ("issue remains open; " if record.get("issueOpen") is True else "issue closed; " if record.get("issueOpen") is False else "")
        + ("issue observation unavailable; " if record.get("issueObservation") == "unavailable" else "")
        + ("human-owned; " if record.get("humanAssigned") is True else "")
        + "PRs: " + _text([
            f"#{pull['number']} ({pull.get('state', 'unknown')})"
            + (f" [last verified: {pull['lastKnownState']}]" if pull.get("lastKnownState") else "")
            for pull in _rows(record.get("pullRequests")) if pull.get("number")
        ])
        for record in records
    )


def _readiness(task: Mapping[str, Any]) -> str:
    state = task.get("currentState") or {}
    checks = state.get("checks") or {}
    review = state.get("review") or {}
    draft = state.get("draft")
    labels = task.get("labels", [])
    no_merge = any(
        str(label.get("name") if isinstance(label, Mapping) else label).casefold() == "no-merge"
        for label in labels
    )
    parts = [
        f"checks: {_text(checks.get('state'))}",
        f"draft: {('yes' if draft else 'no') if isinstance(draft, bool) else 'unknown'}",
        f"review: {_text(review.get('decision'))}",
        f"mergeability: {_text(state.get('mergeableState'))}",
        f"mergeable: {_text(state.get('mergeable'))}",
    ]
    if no_merge:
        parts.append("NO-MERGE")
    if state.get("incompleteReasons"):
        parts.append("missing: " + _text(state["incompleteReasons"]))
    return "; ".join(parts)


def _investigation(
    number: int,
    plan: Mapping[str, Any],
    results: list[Mapping[str, Any]],
    sessions: list[Mapping[str, Any]],
    repository: str,
    launch_blockers: object = None,
) -> tuple[str, list[object], object, object]:
    requests = [
        row for key in ("requests", "deferredRequests", "activeInvestigations", "blockedAwaitingEvidence")
        for row in _rows(plan.get(key)) if row.get("issueNumber") == number
    ]
    ids = {row.get("investigationId") for row in requests}
    # A reused result belongs in current state, not in the run's new work count.
    reused = set(plan.get("reusedInvestigationIds", []))
    matching = [
        result for result in results
        if result.get("issueNumber") == number
        and result.get("repository", repository) == repository
        and result.get("investigationId") in ids | reused
    ]
    if matching:
        result = max(matching, key=lambda row: str(row.get("recordedAt", "")))
        events = [
            row for row in sessions
            if row.get("investigationId") == result.get("investigationId")
            and row.get("sessionId") == result.get("sessionId")
            and row.get("repository", repository) == repository
        ]
        starts = [row.get("recordedAt") for row in events if row.get("status") in ("started", "running")]
        ends = [row.get("recordedAt") for row in events if row.get("status") == "completed"]
        elapsed = _duration(min(starts), max(ends)) if starts and ends else "unknown"
        prefix = "♻ Reused result" if result.get("investigationId") in reused else "✅ Investigation completed"
        text = f"{prefix}; duration: {elapsed}; conclusion: {_text(result.get('outcome'))} — {_text(result.get('summary'))}"
        if result.get("validation"):
            text += "; validation: " + _text(result["validation"])
        work = []
        for entry in _rows(result.get("workLog")):
            if entry.get("kind") == "source":
                target = f"{_text(entry.get('path'))}:{entry.get('startLine')}-{entry.get('endLine')}"
            elif entry.get("kind") == "github-get":
                target = "GET " + _text(entry.get("url"))
            elif entry.get("kind") == "command":
                target = f"argv: {_text(str(entry.get('argv')))}; exit {_text(entry.get('exitCode'))}; output: {_text(entry.get('output'))}"
            else:
                target = _text(entry.get("evidenceId"))
            work.append(f"{target} — {_text(entry.get('finding'))}")
        if work:
            text += "<br>**Worker-reported work** (advisory; not independently verified tool history):<br>"
            text += "<br>".join(work)
        return text, list(result.get("missingEvidence", [])), result.get("reassessWhen"), result.get("sessionId")
    matching_sessions = [
        row for row in sessions if row.get("investigationId") in ids
        and row.get("repository", repository) == repository
    ]
    if matching_sessions:
        latest = max(matching_sessions, key=lambda row: str(row.get("recordedAt", "")))
        if latest.get("status") in ("started", "running"):
            return "🔄 Investigation running; duration: unknown; conclusion: pending", [], None, latest.get("sessionId")
        if latest.get("status") in ("failed", "completed"):
            return "⛔ Investigation ended without a recorded conclusion; duration: unknown", ["investigation-result-missing"], None, latest.get("sessionId")
    if any(row.get("issueNumber") == number for row in _rows(plan.get("activeInvestigations"))):
        return "🔄 Investigation running; duration: unknown; conclusion: pending", [], None, None
    blockers = [
        row.get("reason", row.get("detail", "Launch blocked without a recorded reason."))
        for row in _rows(launch_blockers) if row.get("investigationId") in ids
    ]
    if blockers:
        return "⛔ Investigation blocked before start; " + _text(blockers), blockers, None, None
    deferred = [row for row in _rows(plan.get("deferredRequests")) if row.get("issueNumber") == number]
    if deferred:
        return "⏸ Investigation deferred; not performed", [row.get("reason") for row in deferred], None, None
    if requests:
        return "🔎 Investigation requested; execution not recorded; duration: unknown", [], None, None
    return "⚪ No investigation recorded", [], None, None


def _short(value: str, limit: int = 100) -> str:
    if len(value) <= limit:
        return value
    return value[:limit - 3].rsplit(" ", 1)[0] + "..."


def _collection_summary(snapshot: Mapping[str, Any], audit_details_url: str | None) -> str:
    scan = snapshot.get("openBotScan") or {}
    errors = _rows(snapshot.get("collectionErrors"))
    warnings = snapshot.get("warnings") or []
    parts = [
        f"Open bot scan: {_text(scan.get('status'))}",
        f"errors: {len(errors)}; warnings: {len(warnings)}",
    ]
    if scan.get("status") != "complete" and scan.get("detail"):
        parts.append(_short(_text(scan["detail"]), 160))
    if errors:
        parts.append(f"{_text(errors[0].get('stage'))}: {_short(_text(errors[0].get('message')), 160)}")
    if warnings:
        parts.append("warning: " + _short(_text(warnings[0]), 160))
    link = f" [Full collection audit]({_text(audit_details_url)})" if audit_details_url else ""
    return "**Collection:** " + "; ".join(parts) + "." + link


def _investigation_summary(description: str) -> tuple[str, str, str]:
    # Split only the delimiters emitted by _investigation:
    # "✅ Investigation completed; duration: 120s; conclusion: needs-evidence — ..."
    # The conclusion is opaque and may contain further semicolons.
    states = (
        ("⛔ Investigation blocked before start", "launch-blocked"),
        ("✅ Investigation completed", "completed"),
        ("🔄 Investigation running", "running"),
        ("♻ Reused result", "reused"),
        ("⏸ Investigation deferred", "deferred"),
        ("🔎 Investigation requested", "requested"),
        ("⛔ Investigation ended", "blocked"),
    )
    state = next((state for prefix, state in states if description.startswith(prefix)), "none")
    _, delimiter, suffix = description.partition("; duration: ")
    duration = suffix.partition(";")[0] if delimiter else "unknown"
    conclusion = description.partition("; conclusion: ")[2]
    return state, duration, conclusion


def _operational_state(
    number: int, plan: Mapping[str, Any], sessions: list[Mapping[str, Any]], repository: str,
    recommendations: list[Mapping[str, Any]], tracking: list[Mapping[str, Any]],
    attempts: list[Mapping[str, Any]], history: str,
) -> tuple[str, object]:
    if tracking:
        # Historical failed attempts must not hide a newer running or merged fix.
        tracking = [max(enumerate(tracking), key=lambda item: (
            str(item[1].get("startedAt", "")), item[0],
        ))[1]]
        current = tracking[0]
        if current.get("attemptOutcome") == "merged" and current.get("lifecycle") != "association_pending":
            return "Fix PR merged", "Issue monitoring remains separate from the completed attempt."
        if current.get("attemptOutcome") == "closed-unmerged" and current.get("lifecycle") != "association_pending":
            return "PR closed unmerged", "A new attempt requires a fresh decision."
        if current.get("attemptOutcome") == "legacy-unknown":
            return "Prior attempt outcome unknown", "Review legacy history before deciding on another attempt."
    state_effects = [
        row["result"]["issueState"]
        for row in sorted(attempts, key=lambda row: str(row.get("recordedAt", "")))
        if row.get("outcome") == "executed" and (row.get("result") or {}).get("issueState")
    ]
    if state_effects and state_effects[-1] == "closed" and not tracking:
        return "Closed", "No next event recorded after closure."

    requests = [
        row for key in ("requests", "deferredRequests", "activeInvestigations", "blockedAwaitingEvidence")
        for row in _rows(plan.get(key)) if row.get("issueNumber") == number
    ]
    ids = {row.get("investigationId") for row in requests} | set(plan.get("reusedInvestigationIds", []))
    # A retry can be running while an earlier session's finished review remains
    # useful history. Inspect each session's latest event, not the result prefix.
    latest_sessions = {}
    for row in sorted(sessions, key=lambda row: str(row.get("recordedAt", ""))):
        if (row.get("investigationId") in ids and row.get("issueNumber") == number
                and row.get("repository", repository) == repository):
            latest_sessions[(row.get("investigationId"), row.get("sessionId"))] = row
    if (any(row.get("status") in ("started", "running") for row in latest_sessions.values())
            or not latest_sessions and any(row.get("issueNumber") == number for row in _rows(plan.get("activeInvestigations")))):
        return "Investigation running", "Investigation result."

    decisions = {row.get("disposition"): row for row in recommendations}
    if "ping-human" in decisions:
        return "Waiting for human decision", decisions["ping-human"].get("reassessWhen")
    for record in tracking:
        if record.get("requiresHuman") is True or record.get("lifecycle") == "handoff_required":
            return "Waiting for human decision", record.get("nextWakeup")
    history_state, _, conclusion = _investigation_summary(history)
    if history_state == "launch-blocked":
        return "Investigation blocked before start", "Resolve the recorded launch blocker before starting the worker."
    outcome = conclusion.partition(" — ")[0]
    for lifecycle, label in (
        ("running", "Copilot fix in progress" if outcome == "fixable" else "Copilot task in progress"),
        ("awaiting_pull_request", "Copilot pull request awaiting resolution"),
        ("association_pending", "Waiting for Copilot PR association"),
    ):
        for record in tracking:
            if record.get("lifecycle") == lifecycle:
                return label, record.get("nextWakeup")
    if "watch" in decisions:
        return "Watching for recurrence/recovery", decisions["watch"].get("reassessWhen")
    if outcome == "needs-evidence" or any(row.get("issueNumber") == number for row in _rows(plan.get("blockedAwaitingEvidence"))):
        return "Waiting for evidence", None
    if outcome == "needs-human":
        return "Waiting for human decision", None
    if history_state == "deferred":
        reasons = [row.get("reason") for row in _rows(plan.get("deferredRequests")) if row.get("issueNumber") == number]
        label = "Waiting for investigation capacity" if "per-cycle-investigation-budget" in reasons else "Investigation planned"
        return f"{label} ({_text(reasons)})", None
    if history_state == "requested":
        return "Investigation planned", None
    if history_state == "blocked":
        return "Waiting for investigation result", None
    if "no-action" in decisions:
        return "No action planned", decisions["no-action"].get("reassessWhen")
    if outcome == "fixable":
        return "Waiting for fix handoff", None
    return "Current queue unknown", None


def _append_investigation_overview(
    lines: list[str], rows: list[list[str]], identities: Mapping[str, str],
    next_evidence: Mapping[str, str], operational_states: Mapping[str, tuple[str, object]],
) -> None:
    states = {
        "completed": "Evidence review finished", "running": "Evidence review in progress",
        "blocked": "Ended without a result", "reused": "Prior evidence review reused",
        "deferred": "Not started", "requested": "Not started",
        "launch-blocked": "Launch blocked; not started",
    }
    active, deferred = [], []
    for row in rows:
        state, duration, conclusion = _investigation_summary(row[4])
        if state == "none":
            continue
        identity = identities[row[0]]
        title = row[0].partition(") ")[2]
        summary = _short(conclusion.partition(" — ")[0] or "No conclusion recorded.", 100)
        current_state, next_event = operational_states[row[0]]
        needed = next_evidence.get(row[0], "none recorded")
        if next_event:
            summary += "; next: " + _short(_text(next_event), 90)
        elif current_state == "Waiting for evidence" and needed != "none recorded":
            summary += "; next evidence: " + _short(needed, 140)
        elif row[9] not in ("unknown", "none recorded"):
            summary += "; next: " + _short(row[9], 90)
        output = [
            f"[Issue #{identity.removeprefix('issue-')}: {_short(title, 48)}](#{identity})",
            current_state, f"{states[state]}; duration: {duration}", summary,
        ]
        (deferred if state in ("deferred", "requested", "launch-blocked") else active).append((state, output))
    lines.extend(["## Investigations this run", ""])
    header = [
        "| Item | Current state | Investigation | Conclusion / next event |",
        "|---|---|---|---|",
    ]
    if active:
        lines.extend(header)
        priority = {"completed": 0, "running": 1, "blocked": 2, "reused": 3}
        lines.extend("| " + " | ".join(row) + " |" for _, row in sorted(active, key=lambda item: priority[item[0]]))
    else:
        lines.append("No performed or reused investigations recorded.")
    lines.append("")
    if deferred:
        lines.extend(["<details>", f"<summary>{len(deferred)} deferred / not-started investigations</summary>", "", *header])
        lines.extend("| " + " | ".join(row) + " |" for _, row in deferred)
        lines.extend(["", "</details>", ""])


def _append_group(
    lines: list[str], rows: list[list[str]], identities: Mapping[str, str],
    decisions: Mapping[str, str],
) -> None:
    def priority(row: list[str]) -> int:
        state = _investigation_summary(row[4])[0]
        if row[6] != "No executed action recorded" or state in ("completed", "running", "blocked"):
            return 0
        return 1 if state == "reused" else 2

    rows = sorted(rows, key=priority)
    lines.extend([
        "| Item | This run / prior | Status / blocker | Investigation / actual effect | Decision / next | Owner |",
        "|---|---|---|---|---|---|",
    ])
    for row in rows:
        identity = identities[row[0]]
        link, _, title = row[0].partition(") ")
        state, _, conclusion = _investigation_summary(row[4])
        work = {
            "completed": "✅ Investigated", "running": "🔄 Investigating",
            "blocked": "⛔ Investigation blocked", "reused": "♻ Reused investigation",
            "deferred": "⏸ Deferred", "requested": "🔎 Not started", "none": "⚪ No investigation",
            "launch-blocked": "⛔ Investigation blocked before start",
        }[state]
        if conclusion:
            work += ": " + _short(conclusion.partition(" — ")[0], 50)
        work += "; " + row[6]
        if identity.startswith("pr-"):
            readiness, marker, progress = row[2].partition("; last meaningful change: ")
            status = readiness + (marker + _short(progress, 90) if marker else "")
        else:
            status = _short(row[2], 300)
        if row[7] not in ("None recorded", "none recorded"):
            status += "; " + _short(row[7], 100)
        visible = [
            f"{link}) {_short(title, 72)} <a id=\"{identity}\"></a> [Details](#details-{identity})",
            row[1], status, work,
            decisions[row[0]] + "; next: " + _short(row[9], 90),
            _short(row[8].partition("; investigator session: ")[0], 100),
        ]
        lines.append("| " + " | ".join(visible) + " |")
    lines.extend(["", "<details>", f"<summary>Evidence and full assessments ({len(rows)} items)</summary>", ""])
    labels = (
        "This run / prior", "Current state", "Evidence / validation", "Investigation",
        "Decision", "Executed action", "Blocker", "Owner / suggested next actor", "Next wake-up event",
    )
    for row in rows:
        identity = identities[row[0]]
        lines.extend([
            f"<a id=\"details-{identity}\"></a>",
            f"### {identity}: {row[0].partition(') ')[2]}", "",
        ])
        lines.extend(f"**{label}:** {value}\n" for label, value in zip(labels, row[1:]))
    lines.extend(["</details>", ""])


def render_run_markdown(
    snapshot: Mapping[str, Any],
    prepared: Mapping[str, Any],
    judgments: Mapping[str, Any],
    *,
    review_selection: Mapping[str, Any] | None = None,
    pull_request_review: Mapping[str, Any] | None = None,
    pull_request_judgments: Mapping[str, Any] | None = None,
    investigation_plan: Mapping[str, Any] | None = None,
    investigation_results: Iterable[Mapping[str, Any]] = (),
    investigation_sessions: Iterable[Mapping[str, Any]] = (),
    action_events: Iterable[Mapping[str, Any]] = (),
    prior_snapshot: Mapping[str, Any] | None = None,
    usage: Mapping[str, Any] | None = None,
    run_id: str | None = None,
    as_of: str | None = None,
    invocation_window: Mapping[str, Any] | None = None,
    recording_windows: Iterable[Mapping[str, Any]] = (),
    audit_details_url: str | None = "report-details.md",
    pre_expansion_review_selection: Mapping[str, Any] | None = None,
    pre_expansion_pull_request_review: Mapping[str, Any] | None = None,
    assessment_coverage: Mapping[str, Any] | None = None,
    pre_expansion_assessment_coverage: Mapping[str, Any] | None = None,
) -> str:
    """Render projections only; callers supply canonical records and refresh time.

    run_id binds invocation-wide usage, not the cycle's action-policy identity.
    Invocation boundaries must come from the invocation recorder, never from
    collection/cycle timestamps. Separately labelled recording windows may overlap.
    """
    if usage is not None and (run_id is None or usage.get("runId") != run_id):
        raise ValueError("The usage run must match the explicit report run_id.")
    repository = str(snapshot["repository"])
    snapshot_id = prepared["snapshotId"]
    # Expansion replaces the assessment handoff, not the work already done in
    # that cycle. Deduplicate reviewed identities, preferring the latest round.
    selected = {
        _number(row): row
        for selection in (pre_expansion_review_selection, review_selection)
        for row in _rows((selection or {}).get("selected"))
    }
    acknowledged_issues: set[int] = set()
    acknowledged_prs: set[int] = set()
    for coverage, selection, pr_review in (
        (pre_expansion_assessment_coverage, pre_expansion_review_selection, pre_expansion_pull_request_review),
        (assessment_coverage, review_selection, pull_request_review),
    ):
        selected_issues = {_number(row) for row in _rows((selection or {}).get("selected"))}
        selected_prs = {_number(row) for row in _rows((pr_review or {}).get("tasks"))}
        # Re-selected cases need the newer packet acknowledged; an old receipt
        # cannot stand in for examining newly expanded evidence.
        acknowledged_issues -= selected_issues
        acknowledged_prs -= selected_prs
        if coverage is None:
            continue
        expected_snapshot = (selection or {}).get("snapshotId", snapshot_id)
        if coverage.get("snapshotId") != expected_snapshot or coverage.get("status") != "complete":
            raise ValueError("Assessment coverage must match its completed handoff snapshot.")
        for field, allowed, completed in (
            ("completedIssueNumbers", selected_issues, acknowledged_issues),
            ("completedPullRequestNumbers", selected_prs, acknowledged_prs),
        ):
            numbers = coverage.get(field)
            if (
                not isinstance(numbers, list) or any(type(number) is not int or number < 1 for number in numbers)
                or len(set(numbers)) != len(numbers) or not set(numbers) <= allowed
            ):
                raise ValueError("Assessment coverage contains invalid or unselected cases.")
            completed.update(numbers)
    metadata = {
        _number(row): row for field in ("delegatedIssueDetails", "issues")
        for row in _rows(snapshot.get(field))
    }
    tracking_by_issue: dict[int, list[Mapping[str, Any]]] = {}
    tracked_pulls: dict[int, Mapping[str, Any]] = {}
    for record in _rows((snapshot.get("delegationStatus") or {}).get("records")):
        number = _number(record)
        if number is not None:
            tracking_by_issue.setdefault(number, []).append(record)
        for pull in _rows(record.get("pullRequests")):
            if _number(pull) is not None:
                tracked_pulls[_number(pull)] = pull
    prepared_issues = {_number(row): row for row in _rows(prepared.get("issues"))}
    issue_judgments = {_number(row): row for row in _rows(judgments.get("issues"))}
    previous = {_number(row): row for row in _rows((prior_snapshot or {}).get("issues"))}
    plan = investigation_plan or {}
    results = [row for row in investigation_results if _not_after(row, as_of)]
    sessions = [row for row in investigation_sessions if _not_after(row, as_of)]
    effects: dict[tuple[object, ...], Mapping[str, Any]] = {}
    terminals: dict[tuple[object, ...], Mapping[str, Any]] = {}
    attempts: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    for event in action_events:
        if event.get("repository") != repository or event.get("eventType") != "terminal" or not _not_after(event, as_of):
            continue
        if event.get("snapshotId") != snapshot_id:
            continue
        target = event.get("target") or {}
        number = _number(event)
        kind = target.get("kind", "issue")
        if number is None:
            continue
        identity = (event.get("actionId"), kind, number, event.get("operation"))
        # Ledger order preserves superseding reconciliation: a resolved action
        # must not retain its earlier indeterminate outcome as a current blocker.
        terminals[identity] = event
    for identity, event in terminals.items():
        _, kind, number, _ = identity
        attempts.setdefault((kind, number), []).append(event)
        if event.get("outcome") == "executed":
            effects[identity] = event

    groups: dict[str, list[list[str]]] = {
        "Pull requests": [], "Other issues": [], "Flaky / failing test issues": [], "Workflow / CI incidents": [],
    }
    identities: dict[str, str] = {}
    brief_decisions: dict[str, str] = {}
    next_evidence: dict[str, str] = {}
    operational_states: dict[str, tuple[str, object]] = {}
    issue_numbers = set(metadata) | set(prepared_issues) | set(issue_judgments) | set(selected) | set(tracking_by_issue)
    issue_numbers.update(number for kind, number in attempts if kind == "issue")
    unchanged: list[str] = []

    def executed(kind: str, number: int) -> str:
        matching = [row for row in effects.values() if _number(row) == number and row.get("target", {}).get("kind", "issue") == kind]
        if matching:
            return "; ".join("✅ Executed: " + _text(row.get("operation")) for row in matching)
        return "No executed action recorded"

    for number in sorted(value for value in issue_numbers if value is not None):
        item = metadata.get(number, {})
        assessment = prepared_issues.get(number, {})
        judgment = issue_judgments.get(number, {})
        recommendations = _rows(judgment.get("recommendations"))
        selection = selected.get(number, {})
        tracking = tracking_by_issue.get(number) or _rows((assessment.get("delegationContext") or {}).get("records"))
        investigation, missing, wake, investigator = _investigation(
            number, plan, results, sessions, repository,
            (invocation_window or {}).get("investigationBlockers"),
        )
        if (review_selection is not None and number not in selected
                and investigation == "⚪ No investigation recorded"
                and ("issue", number) not in attempts and not tracking):
            unchanged.append(f"issue #{number}")
            continue
        labels = [str(label.get("name") if isinstance(label, Mapping) else label).casefold() for label in item.get("labels", [])]
        category = judgment.get("category", assessment.get("producer", item.get("producer")))
        if category in ("workflow-failure", "infrastructure-failure", "ci-failure", "ci-failure-cause", "transient-infrastructure", "automation-tracker"):
            group = "Workflow / CI incidents"
        elif ("testMaintenance" in assessment
                or any(label in labels for label in ("failing-test", "flaky-test", "quarantined-test"))
                or category in ("test-failure", "flaky-test")):
            group = "Flaky / failing test issues"
        elif item.get("producer") == "ci-failure-cause" or "ci-failure-cause" in labels:
            group = "Workflow / CI incidents"
        else:
            group = "Other issues"
        url = item.get("html_url") or item.get("url") or assessment.get("issueUrl") or f"https://github.com/{repository}/issues/{number}"
        prior = selection.get("previousDisposition", previous.get(number, {}).get("state"))
        change = "🆕 New" if selection.get("changeClass") == "new" else "Existing"
        review = (
            "assessment acknowledged" if number in acknowledged_issues
            else "selected; completion unverified" if number in selected
            else "carried assessment" if judgment else "tracked; not reviewed this run"
        )
        blockers = [*missing, *assessment.get("blockers", []), *assessment.get("missingPrerequisites", [])]
        blockers.extend(value for row in recommendations for value in row.get("missingEvidence", []))
        blockers.extend(row.get("outcome") for row in attempts.get(("issue", number), []) if row.get("outcome") != "executed")
        suggested = [
            row["humanEscalation"].get("routingHint")
            for row in recommendations if isinstance(row.get("humanEscalation"), Mapping)
        ]
        state = _text(item.get("state"))
        for effect in sorted(attempts.get(("issue", number), []), key=lambda row: str(row.get("recordedAt", ""))):
            if effect.get("outcome") == "executed" and effect.get("result", {}).get("issueState"):
                state = _text(effect["result"]["issueState"]) + " (recorded action)"
        # Source proof, not a GitHub label, distinguishes quarantine from ActiveIssue.
        for key in ("sourceState", "quarantineSourceState", "fixReadiness", "copilotTracking"):
            if key in assessment:
                state += f"; {key}: {_text(assessment[key])}"
        maintenance = assessment.get("testMaintenance") or {}
        if maintenance:
            state += f"; quarantine source: {_text(maintenance.get('state'))}; evidence complete: {_text(maintenance.get('evidenceComplete'))}"
            if maintenance.get("reason"):
                blockers.append(maintenance["reason"])
        actionability = assessment.get("machineActionability") or {}
        if actionability:
            state += f"; fix handoff: {_text(actionability.get('status'))} (not an executed fix)"
        if tracking:
            state += "; Copilot tracking: " + _tracking_summary(tracking)
            if "quarantined-test" in labels and any(
                record.get("attemptOutcome") == "merged" and record.get("issueOpen") is True
                for record in tracking
            ):
                state += "; quarantine reliability review still required; a merged fix does not authorize unquarantine"
        wakeups = [
            f"{record['nextWakeup']['evaluateAt']} ({record['nextWakeup']['reason']})"
            for record in tracking if record.get("nextWakeup")
        ]
        state += "; last matching failure: " + _failure_age(assessment, as_of)
        groups[group].append([
            f"[#{number}]({_text(url)}) {_text(item.get('title', assessment.get('title')))}",
            f"{change}; {review}; prior: {_text(prior)}",
            state,
            _text(list(dict.fromkeys([
                *[value for row in recommendations for value in row.get("evidenceIds", [])],
                *maintenance.get("evidenceIds", []),
            ]))),
            investigation,
            "; ".join(f"{_status(row.get('disposition'))}: {_text(row.get('summary'))}" for row in recommendations) or "No decision recorded",
            executed("issue", number),
            ("⛔ " + _text(blockers)) if blockers else "None recorded",
            f"Owner: {_owner(item)}; suggested next actor: {_text(suggested) if suggested else 'unknown'}"
            + (f"; investigator session: {_text(investigator)}" if investigator else ""),
            _text(wake or wakeups or [row.get("reassessWhen") for row in recommendations]),
        ])
        label = groups[group][-1][0]
        identities[label] = f"issue-{number}"
        brief_decisions[label] = "; ".join(_status(row.get("disposition")) for row in recommendations) or "No decision recorded"
        next_evidence[label] = _text(missing)
        operational_states[label] = _operational_state(
            number, plan, sessions, repository, recommendations, tracking,
            attempts.get(("issue", number), []), investigation,
        )

    pr_judgments = {_number(row): row for row in _rows((pull_request_judgments or {}).get("pullRequests"))}
    tasks = {
        _number(row): row
        for review in (pre_expansion_pull_request_review, pull_request_review)
        for row in _rows((review or {}).get("tasks"))
    }
    inventory_prs = {
        _number(row): row for field in ("delegatedPullRequestDetails", "pullRequests")
        for row in _rows(snapshot.get(field))
    }
    displayed_pr_numbers = {number for number in set(tasks) | set(pr_judgments) | set(tracked_pulls) if number is not None}
    for number in sorted(displayed_pr_numbers):
        source_state = ((snapshot.get("evidence") or {}).get(f"pr:{number}") or {}).get("payload", {}).get("currentState")
        tracked = tracked_pulls.get(number, {})
        task = {
            **inventory_prs.get(number, {}),
            "currentState": source_state or {"draft": tracked.get("isDraft")},
            **tasks.get(number, {}),
        }
        if source_state is not None:
            task["currentState"] = source_state
        judgment = pr_judgments.get(number, task.get("defaultJudgment") or {})
        review = (
            "assessment acknowledged" if number in acknowledged_prs
            else "selected; completion unverified" if number in tasks
            else "carried assessment" if judgment else "tracked; not reviewed this run"
        )
        progress = task.get("meaningfulProgress", inventory_prs.get(number, {}).get("meaningfulProgress")) or {}
        url = task.get("html_url") or task.get("url") or f"https://github.com/{repository}/pull/{number}"
        groups["Pull requests"].append([
            f"[#{number}]({_text(url)}) {_text(task.get('title'))}",
            f"{'🆕 New' if task.get('changeClass') == 'new' else 'Existing'}; {review}; prior: {_text(task.get('previousDefaultDisposition'))}",
            _readiness(task) + "; last meaningful change: " + _progress_age(progress, as_of),
            _text(list(dict.fromkeys([
                *judgment.get("evidenceIds", task.get("evidenceIds", [])),
                *progress.get("evidenceIds", []),
            ]))),
            "⚪ No investigation recorded",
            f"{_status(judgment.get('disposition'))}: {_text(judgment.get('summary'))}" if judgment else "No fresh assessment",
            executed("pull-request", number),
            _text(judgment.get("missingEvidence", [])),
            f"Owner: {_owner(task)}; suggested next actor: {_text((judgment.get('humanEscalation') or {}).get('routingHint'))}",
            _text(judgment.get("reassessWhen") or task.get("nextWakeupEvent")),
        ])
        label = groups["Pull requests"][-1][0]
        identities[label] = f"pr-{number}"
        brief_decisions[label] = _status(judgment.get("disposition")) if judgment else "No fresh assessment"

    lines = [
        "# CI Shepherd run report", "",
        f"**{_text(repository)}** · report as of {_text(as_of)} · snapshot collected {_text(snapshot.get('collectedAt'))}",
        f"**{len(effects)} executed effects recorded** · {len(selected)} issues selected for review · {len(tasks)} PRs selected for review",
        f"{len(acknowledged_issues)} issues / {len(acknowledged_prs)} PRs with assessment acknowledgements.",
        "Acknowledgements establish packet coverage, not independent proof of reasoning quality.", "",
        "Decisions are not actions. Investigated is not fixed. Green checks are not merge readiness.",
        "⚪ No action · 🆕 New · 🔄 Running · ✅ Evidence review finished · ⛔ Blocked · ⏸ Deferred · 👤 Human input", "",
        _collection_summary(snapshot, audit_details_url), "",
    ]
    investigation_cells = [row[4] for rows in groups.values() for row in rows]
    lines.extend([
        f"Investigations: {sum(cell.startswith('✅ Investigation completed') for cell in investigation_cells)} evidence reviews finished, "
        f"{sum(state == 'Investigation running' for state, _ in operational_states.values())} running, "
        f"{sum(cell.startswith('♻ Reused result') for cell in investigation_cells)} reused results.", "",
    ])
    window = invocation_window or {}
    window_duration = _duration(window.get("startedAt"), window.get("completedAt"))
    lines.extend([
        "**Whole invocation duration:** unknown.",
        "Recorded windows do not independently establish runtime session boundaries, even when declared whole-invocation.",
    ])
    if window:
        lines.append(
            f"Recorded {_text(window.get('scope') or 'invocation')} window: {window_duration} "
            f"(recorded start: {_text(window.get('startedAt'))}; recorded completion: {_text(window.get('completedAt'))})."
        )
    lines.append("Setup/tail outside the recorded window: unknown, not zero.")
    for window in recording_windows:
        duration = _duration(window.get("startedAt"), window.get("completedAt"))
        seconds = window.get("durationSeconds")
        measurement = window.get("measurement") or ("recorded-window" if duration != "unknown" else "unknown")
        if (duration == "unknown" and isinstance(seconds, (int, float)) and not isinstance(seconds, bool)
                and math.isfinite(seconds) and seconds >= 0):
            duration = _human_duration(seconds)
        lines.append(
            f"- {_text(window.get('label'))}: {duration}; measurement: {_text(measurement)}; "
            f"basis: {_text(window.get('basis'))} "
            f"({_text(window.get('startedAt'))} to {_text(window.get('completedAt'))})"
        )
    lines.extend([
        "Recording windows may overlap; collection/cycle windows are not a substitute for whole-invocation timing.",
        "Inferred intervals are not measured model compute. These windows do not establish request-level latency or billable usage.", "",
    ])
    _append_investigation_overview(
        lines, [row for rows in groups.values() for row in rows], identities, next_evidence, operational_states,
    )
    lines.extend(["## Usage", ""])
    if usage is None:
        lines.append("Unknown — no run session roster / usage export supplied. Tokens and AI credits are separate; missing is not zero.")
    else:
        lines.extend(usage_markdown(usage))
    lines.append("")
    for heading, rows in groups.items():
        lines.extend([f"## {heading}", ""])
        if not rows:
            lines.extend(["None recorded.", ""])
            continue
        _append_group(lines, rows, identities, brief_decisions)
    excluded_prs = _rows((pull_request_review or {}).get("excluded"))
    unchanged.extend(
        f"PR #{row['number']} ({_text(row.get('reason'))})"
        for row in excluded_prs
        if isinstance(row.get("number"), int) and row["number"] not in displayed_pr_numbers
    )
    excluded_pr_numbers = {_number(row) for row in excluded_prs}
    unchanged.extend(
        f"PR #{number} (not selected)"
        for number in sorted(set(inventory_prs) - displayed_pr_numbers - excluded_pr_numbers)
        if number is not None
    )
    if unchanged:
        lines.extend(["<details>", f"<summary>{len(unchanged)} unchanged / excluded inventory items</summary>", "",
                      ", ".join(unchanged), "", "</details>", ""])
    lines.extend(["", "## Collection limitations", "",
                  f"[Full collection audit]({_text(audit_details_url)})" if audit_details_url else "No separate collection audit supplied.",
                  "Ages use recorded matching failures or meaningful changes only; updatedAt is not an age signal.", ""])
    return "\n".join(lines)


def usage_markdown(usage: Mapping[str, Any]) -> list[str]:
    labels = {
        "inputTokens": "Input tokens",
        "outputTokens": "Output tokens",
        "cacheReadTokens": "Cache-read tokens",
        "cacheWriteTokens": "Cache-write tokens",
        "totalNanoAiu": "Provider cost (nano-AI units)",
        "premiumRequests": "Premium requests (legacy)",
        "aiCredits": "AI credits",
    }
    lines = [
        f"As of {_text(usage.get('asOf'))}; {_text(usage.get('coverage'))}.",
        f"New-cost roster: {_text(usage.get('sessionCount'))} sessions. Excluded from new cost: "
        f"{_text(usage.get('excludedReusedSessions'))} reused, "
        f"{_text(usage.get('excludedSkippedSessions'))} skipped, "
        f"{_text(usage.get('excludedIncludedSessions'))} already included in parent totals.",
        "Tokens are provider counts (cache categories may overlap input); never a credit conversion.",
        "| Metric | Known subtotal | Sessions covered | Source as-of range |",
        "|---|---:|---|---|",
    ]
    for name, metric in (usage.get("metrics") or {}).items():
        lines.append(
            f"| {_text(labels.get(name, name))} | {_text(metric.get('value'))} | "
            f"{_text(metric.get('coveredSessions'))} / {_text(usage.get('sessionCount'))} | "
            f"{_text(metric.get('earliestAsOf'))} – {_text(metric.get('latestAsOf'))} |"
        )
    lines.extend("- " + _text(note) for note in usage.get("limitations", []))
    return lines
