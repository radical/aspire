#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import quote

from ci_shepherd.delegation_observer import _outcome_body, closing_keyword_contract, reported_test_execution_section
from ci_shepherd.delegations import derive_delegation_tracking
from ci_shepherd.github import GitHubApiError, GitHubClient
from ci_shepherd.models import validate_report, validate_snapshot
from ci_shepherd.poc import validate_poc_judgments
from ci_shepherd.pull_requests import build_pull_request_current_state
from ci_shepherd.run_report import render_run_markdown
from ci_shepherd.investigation_worktrees import investigation_capacity_inventory
from ci_shepherd.jsonl import read_jsonl_rows
from ci_shepherd.assessment_batches import verify_assessment_completion


_OPERATIONAL_QUEUES = (
    (
        "Investigate next",
        frozenset({"investigate", "fix", "open-dedicated-issue", "open-regression"}),
    ),
    ("Needs human", frozenset({"ping-human"})),
    (
        "Closure candidates",
        frozenset(
            {
                "recommend-close",
                "close",
                "close-resolved",
                "close-stale",
                "close-as-tracked",
                "merge-duplicate",
            }
        ),
    ),
    ("Waiting or owned by automation", frozenset({"wait"})),
)

_POC_QUEUES = (
    ("Investigate", "investigate"),
    ("Watch", "watch"),
    ("Needs human", "ping-human"),
    ("Quarantine review", "review-quarantine"),
    ("Retry review", "review-retry"),
    ("Rerun review", "review-rerun"),
    ("Closure review", "review-close"),
    ("No action", "no-action"),
)


def _markdown_text(value: object) -> str:
    return " ".join(str(value).split()).replace("|", "\\|")


def _target_text(target: object) -> str:
    if not isinstance(target, dict):
        raise TypeError("Validated target must be an object.")
    return f"{_markdown_text(target['kind'])}:{_markdown_text(target['value'])}"


def _inline_code_list(values: object) -> str:
    if not isinstance(values, list):
        raise TypeError("Validated evidence values must be a list.")
    if not values:
        return "—"
    return ", ".join(f"`{_markdown_text(value)}`" for value in values)


def _prepared_issue_metadata(prepared: dict[str, object]) -> dict[int, dict[str, object]]:
    issues = prepared.get("issues")
    if not isinstance(issues, list):
        raise TypeError("Validated prepared issues must be a list.")
    metadata: dict[int, dict[str, object]] = {}
    for issue in issues:
        if not isinstance(issue, dict):
            raise TypeError("Validated prepared issue must be an object.")
        number = issue["issueNumber"]
        if not isinstance(number, int):
            raise TypeError("Validated prepared issue number must be an integer.")
        metadata[number] = issue
    return metadata


def _issue_label(number: int, metadata: dict[str, object]) -> str:
    url = metadata.get("issueUrl")
    title = metadata.get("title")
    if isinstance(url, str) and url:
        label = f"[#{number}]({url})"
    else:
        label = f"#{number}"
    if isinstance(title, str) and title:
        label += f" {_markdown_text(title)}"
    return label


def render_poc_markdown(
    prepared: object,
    judgments: object,
    *,
    prepared_path: Path,
    snapshot: object,
    visible_issue_numbers: set[int] | None = None,
) -> str:
    validate_poc_judgments(prepared, judgments)
    if not isinstance(prepared, dict) or not isinstance(judgments, dict):
        raise TypeError("Validated prepared input and judgments must be objects.")

    issue_metadata = _prepared_issue_metadata(prepared)
    issue_judgments = judgments.get("issues")
    if not isinstance(issue_judgments, list):
        raise TypeError("Validated issue judgments must be a list.")
    if visible_issue_numbers is None:
        rendered_issue_judgments = issue_judgments
    else:
        unknown_issue_numbers = visible_issue_numbers - set(issue_metadata)
        if unknown_issue_numbers:
            raise ValueError(
                "Visible issue numbers include an unknown issue: "
                f"{min(unknown_issue_numbers)}."
            )
        rendered_issue_judgments = [
            issue
            for issue in issue_judgments
            if isinstance(issue, dict)
            and issue.get("issueNumber") in visible_issue_numbers
        ]
    carried_issue_count = len(issue_judgments) - len(rendered_issue_judgments)
    rendered_issue_numbers = {
        issue["issueNumber"]
        for issue in rendered_issue_judgments
        if isinstance(issue, dict)
    }
    carried_queue_issue_numbers: dict[str, set[int]] = {}
    for issue in issue_judgments:
        if not isinstance(issue, dict):
            raise TypeError("Validated issue judgment must be an object.")
        issue_number = issue["issueNumber"]
        if issue_number in rendered_issue_numbers:
            continue
        recommendations = issue["recommendations"]
        if not isinstance(recommendations, list):
            raise TypeError("Validated recommendations must be a list.")
        for recommendation in recommendations:
            if not isinstance(recommendation, dict):
                raise TypeError("Validated recommendation must be an object.")
            carried_queue_issue_numbers.setdefault(
                str(recommendation["disposition"]),
                set(),
            ).add(int(issue_number))

    rows: list[dict[str, object]] = []
    for issue in rendered_issue_judgments:
        if not isinstance(issue, dict):
            raise TypeError("Validated issue judgment must be an object.")
        issue_number = issue["issueNumber"]
        if not isinstance(issue_number, int):
            raise TypeError("Validated issue number must be an integer.")
        category = issue["category"]
        recommendations = issue["recommendations"]
        if not isinstance(recommendations, list):
            raise TypeError("Validated recommendations must be a list.")
        for recommendation in recommendations:
            if not isinstance(recommendation, dict):
                raise TypeError("Validated recommendation must be an object.")
            rows.append(
                {
                    "issueNumber": issue_number,
                    "category": category,
                    "disposition": recommendation["disposition"],
                    "target": recommendation["target"],
                    "confidence": recommendation["confidence"],
                    "summary": recommendation["summary"],
                    "evidenceIds": recommendation["evidenceIds"],
                    "missingEvidence": recommendation["missingEvidence"],
                    "reassessWhen": recommendation["reassessWhen"],
                    "humanEscalation": recommendation.get("humanEscalation"),
                }
            )

    category_counts = Counter(
        str(issue["category"]) for issue in rendered_issue_judgments
    )
    disposition_counts = Counter(str(row["disposition"]) for row in rows)
    confidence_counts = Counter(str(row["confidence"]) for row in rows)

    lines = [
        "# CI Shepherd POC Assessment",
        "",
        f"**Repository:** `{_markdown_text(prepared['repository'])}`  ",
        f"**Snapshot:** `{_markdown_text(prepared['snapshotId'])}`  ",
        f"**Prepared input:** `{_markdown_text(prepared_path)}`  ",
        f"**Recommendations this cycle:** {len(rows)}  ",
        f"**Carried forward unchanged cases:** {carried_issue_count}",
        "",
        "## Counts",
        "",
    ]
    _append_count_table(lines, "Category counts", "Category", category_counts)
    _append_count_table(lines, "Disposition counts", "Disposition", disposition_counts)
    _append_count_table(lines, "Confidence counts", "Confidence", confidence_counts)

    if not isinstance(snapshot, dict):
        raise TypeError("Snapshot must be an object.")
    scan = snapshot.get("openBotScan")
    scan_status = (
        str(scan.get("status"))
        if isinstance(scan, dict) and isinstance(scan.get("status"), str)
        else "not-recorded"
    )
    collection_errors = snapshot.get("collectionErrors")
    warnings = snapshot.get("warnings")
    error_count = len(collection_errors) if isinstance(collection_errors, list) else 0
    warning_rows = warnings if isinstance(warnings, list) else []
    lines.extend(
        [
            "",
            "## Collection completeness",
            "",
            f"**Open bot scan:** `{_markdown_text(scan_status)}`  ",
            f"**Collection errors:** {error_count}  ",
            f"**Collection warnings:** {len(warning_rows)}",
        ]
    )
    if warning_rows:
        lines.extend(
            [
                "",
                *[
                    f"- {_markdown_text(str(warning))}"
                    for warning in warning_rows
                ],
            ]
        )
    if isinstance(collection_errors, list) and collection_errors:
        lines.extend(["", "**Collection error details:**"])
        for error in collection_errors:
            if not isinstance(error, dict):
                continue
            stage = _markdown_text(str(error.get("stage") or "unknown"))
            message = _markdown_text(str(error.get("message") or "unknown error"))
            endpoint = _markdown_text(str(error.get("endpoint") or "unknown endpoint"))
            lines.append(f"- `{stage}`: {message} (`{endpoint}`)")

    for heading, disposition in _POC_QUEUES:
        queue = [
            row
            for row in rows
            if row["disposition"] == disposition
        ]
        queue.sort(key=lambda row: (int(row["issueNumber"]), _target_text(row["target"])))
        carried_queue_count = len(carried_queue_issue_numbers.get(disposition, set()))
        lines.extend(["", f"## {heading}", ""])
        if not queue:
            if carried_queue_count:
                issue_word = "issue" if carried_queue_count == 1 else "issues"
                lines.append(
                    "No recommendations this cycle; "
                    f"{carried_queue_count} unchanged {issue_word} carried forward "
                    "in this queue."
                )
            else:
                lines.append("None.")
            continue
        lines.extend(
            [
                "| Issue | Category | Target | Confidence | Summary | Evidence | Missing evidence | Reassess when |",
                "|---|---|---|---|---|---|---|---|",
            ]
        )
        for row in queue:
            issue_number = int(row["issueNumber"])
            lines.append(
                "| "
                + " | ".join(
                    (
                        _issue_label(issue_number, issue_metadata[issue_number]),
                        _markdown_text(row["category"]),
                        _target_text(row["target"]),
                        _markdown_text(row["confidence"]),
                        _markdown_text(row["summary"]),
                        _inline_code_list(row["evidenceIds"]),
                        _inline_code_list(row["missingEvidence"]),
                        _markdown_text(row["reassessWhen"]),
                    )
                )
                + " |"
            )
        if disposition == "ping-human":
            for row in queue:
                _append_human_comment_draft(lines, row)
        if carried_queue_count:
            issue_word = "issue" if carried_queue_count == 1 else "issues"
            lines.extend(
                [
                    "",
                    f"{carried_queue_count} unchanged {issue_word} also carried "
                    "forward in this queue.",
                ]
            )

    lines.append("")
    return "\n".join(lines)


def _comment_text(value: object) -> str:
    return " ".join(str(value).split()).replace("```", "'''")


def _append_human_comment_draft(
    lines: list[str],
    row: dict[str, object],
) -> None:
    escalation = row.get("humanEscalation")
    if not isinstance(escalation, dict):
        raise TypeError("Validated ping-human recommendation must include humanEscalation.")
    issue_number = int(row["issueNumber"])
    steps = escalation["suggestedNextSteps"]
    if not isinstance(steps, list):
        raise TypeError("Validated suggestedNextSteps must be a list.")
    lines.extend(
        [
            "",
            f"### Draft comment for #{issue_number}",
            "",
            "```markdown",
            f"[automated] {_comment_text(escalation['context'])}",
            "",
            f"**Why human input is needed:** {_comment_text(escalation['whyHuman'])}",
            "",
            f"**Decision needed:** {_comment_text(escalation['question'])}",
            "",
            "**Suggested next steps:**",
        ]
    )
    lines.extend(f"- {_comment_text(step)}" for step in steps)
    lines.extend(
        [
            "",
            f"**Routing hint:** `{_comment_text(escalation['routingHint'])}`",
            "```",
        ]
    )


def _append_count_table(
    lines: list[str],
    heading: str,
    label: str,
    counts: Counter[str],
) -> None:
    lines.extend(
        [
            f"### {heading}",
            "",
            f"| {label} | Count |",
            "|---|---:|",
        ]
    )
    for value, count in sorted(counts.items()):
        lines.append(f"| {_markdown_text(value)} | {count} |")
    if not counts:
        lines.append(f"| none | 0 |")
    lines.append("")


def render_markdown(
    snapshot: object,
    report: object,
    *,
    snapshot_path: Path,
) -> str:
    validate_snapshot(snapshot)
    validate_report(snapshot, report)
    if not isinstance(snapshot, dict) or not isinstance(report, dict):
        raise TypeError("Validated snapshot and report must be objects.")

    decisions = report["decisions"]
    if not isinstance(decisions, list):
        raise TypeError("Validated report decisions must be a list.")
    issues = snapshot.get("issues", [])
    titles = {
        int(issue["number"]): str(issue.get("title", ""))
        for issue in issues
        if isinstance(issue, dict) and isinstance(issue.get("number"), int)
    }
    action_counts = Counter(str(decision["proposedAction"]) for decision in decisions)
    collection_errors = snapshot.get("collectionErrors", [])
    warnings = snapshot.get("warnings", [])

    lines = [
        "# CI Shepherd Assessment",
        "",
        f"**Repository:** `{_markdown_text(snapshot['repository'])}`  ",
        f"**Collection timestamp:** `{_markdown_text(snapshot['collectedAt'])}`  ",
        f"**Open issues assessed:** {len(decisions)}  ",
        f"**Snapshot:** `{_markdown_text(snapshot_path)}`",
        "",
        "## Proposed actions",
        "",
        "| Action | Issues |",
        "|---|---:|",
    ]
    for action, count in sorted(action_counts.items()):
        lines.append(f"| `{_markdown_text(action)}` | {count} |")

    sorted_decisions = sorted(decisions, key=lambda item: int(item["issueNumber"]))
    for heading, actions in _OPERATIONAL_QUEUES:
        queue = [
            decision
            for decision in sorted_decisions
            if decision["proposedAction"] in actions
        ]
        lines.extend(
            [
                "",
                f"## {heading}",
                "",
            ]
        )
        if not queue:
            lines.append("None.")
            continue
        lines.extend(
            [
                "| Issue | Action / confidence | Assessment |",
                "|---|---|---|",
            ]
        )
        for decision in queue:
            number = int(decision["issueNumber"])
            title = titles.get(number, "")
            issue_label = f"[#{number}]({decision['issueUrl']})"
            if title:
                issue_label += f" {_markdown_text(title)}"
            lines.append(
                "| "
                + " | ".join(
                    (
                        issue_label,
                        f"`{_markdown_text(decision['proposedAction'])}` / "
                        f"`{_markdown_text(decision['confidence'])}`",
                        _markdown_text(decision["summary"]),
                    )
                )
                + " |"
            )

    lines.extend(
        [
            "",
            "## Recommendations",
            "",
            "| Issue | Kind / state | Action / confidence | Assessment | Next condition |",
            "|---|---|---|---|---|",
        ]
    )
    for decision in sorted_decisions:
        number = int(decision["issueNumber"])
        title = titles.get(number, "")
        issue_label = f"[#{number}]({decision['issueUrl']})"
        if title:
            issue_label += f" {_markdown_text(title)}"
        next_condition = decision["nextCondition"]
        lines.append(
            "| "
            + " | ".join(
                (
                    issue_label,
                    f"`{_markdown_text(decision['issueKind'])}` / `{_markdown_text(decision['state'])}`",
                    f"`{_markdown_text(decision['proposedAction'])}` / `{_markdown_text(decision['confidence'])}`",
                    _markdown_text(decision["summary"]),
                    _markdown_text(next_condition["description"]),
                )
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Collection limitations",
            "",
            f"**Collection errors:** {len(collection_errors) if isinstance(collection_errors, list) else 0}  ",
            f"**Collection warnings:** {len(warnings) if isinstance(warnings, list) else 0}",
            "",
        ]
    )
    return "\n".join(lines)


def _write_markdown(path: Path, markdown: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_TRUNC | os.O_WRONLY,
        0o600,
    )
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(markdown)
    return path.resolve()


class _ReportReadClient:
    """Bound an observer, retaining the exact GET responses for report-only checks."""

    def __init__(self, client: object, max_calls: int) -> None:
        self.client = client
        self.max_calls = max_calls
        self.calls = 0
        self.responses: dict[str, object] = {}
        self.pages: dict[tuple[str, str | None], list[object]] = {}
        self.problems: list[str] = []

    def _read(self, endpoint: str, key: str | None = None, *, pages: bool = False) -> object:
        if pages and (endpoint, key) in self.pages:
            return self.pages[(endpoint, key)]
        if not pages and endpoint in self.responses:
            return self.responses[endpoint]
        if self.calls >= self.max_calls:
            problem = f"Final observation GET budget ({self.max_calls}) exhausted: {endpoint}"
            if problem not in self.problems:
                self.problems.append(problem)
            raise GitHubApiError(
                category="report-budget", endpoint=endpoint, status=0, headers={},
                retryable=False, attempts=0, sanitized_stderr="",
            )
        self.calls += 1
        try:
            value = self.client.get_pages(endpoint, key=key) if pages else self.client.get(endpoint)
        except (GitHubApiError, ValueError, RuntimeError) as exc:
            self.problems.append(str(exc))
            raise
        if pages:
            self.pages[(endpoint, key)] = value
        else:
            self.responses[endpoint] = value
        return value

    def get(self, endpoint: str) -> object:
        return self._read(endpoint)

    def get_pages(self, endpoint: str, key: str | None = None) -> list[object]:
        return self._read(endpoint, key, pages=True)


def _keep_open_contract(issue_number: int, instructions: object, issue: dict[str, object]) -> tuple[bool | None, str]:
    if isinstance(instructions, str):
        # Read the exact final clause emitted by actions._delegation_instructions,
        # not a bare "Refs #N" that may also appear in quoted handoff context.
        references = re.findall(
            rf"Open a draft pull request whose body includes `(Refs|Fixes) #{issue_number}`\.",
            instructions,
        )
        if references:
            return references[-1] == "Refs", "frozen dispatch instructions"
    if (issue.get("testMaintenance") or {}).get("state") == "quarantined":
        return True, "frozen quarantine source facts"
    health = issue.get("workflowHealth") or {}
    if (health.get("workflowPath") or health.get("workflow")) and health.get("job") and health.get("evidenceIds"):
        return True, "frozen workflow failure facts"
    followup = issue.get("repairFollowup") or {}
    subject = followup.get("subject") or {}
    if subject.get("workflowPath") and subject.get("job") and followup.get("evidenceIds"):
        return True, "frozen workflow repair subject"
    return None, "unavailable"


def refresh_report_delegations(
    snapshot: dict[str, object], prepared: dict[str, object], events: list[dict[str, object]],
    *, client: object, max_api_calls: int = 30, now=None,
    action_proposals: dict[str, object] | None = None,
) -> dict[str, object]:
    """Observe existing assignments without recollection, scheduling, or ledger writes.

    The injected client must perform one request per get/get_pages, without
    retries. The canonical CLI enforces this with max_pages=max_attempts=1.
    """
    from collect import observe_delegation_status

    if type(max_api_calls) is not int or not 1 <= max_api_calls <= 30:
        raise ValueError("Final observation requires a GET budget from 1 through 30.")
    clock = now or (lambda: datetime.now(timezone.utc))
    started = clock()
    repository = str(snapshot["repository"])
    frozen_events = [event for event in events if event.get("repository") == repository]
    reader = _ReportReadClient(client, max_api_calls)
    status: dict[str, object] = {"status": "complete", "records": []}
    # No assignment history means there is nothing to observe. Do not spend a
    # repository-wide capacity inventory GET merely to render an empty report.
    baselines = {event["actionId"]: event for event in frozen_events if event.get("eventType") == "delegation-baseline"}
    if baselines:
        # Prefer the newest dispatches and preserve each successful observation
        # if a later attempt exhausts the bound. Shared GETs are cached, including
        # the repository running-task page used by the existing observer.
        for action_id in sorted(baselines, key=lambda key: str(baselines[key].get("recordedAt", "")), reverse=True):
            attempt_events = [event for event in frozen_events if event.get("actionId") == action_id]
            try:
                observed_status, _ = observe_delegation_status(reader, repository, events=attempt_events, now=started)
                status["records"].extend(observed_status["records"])
            except (GitHubApiError, ValueError, RuntimeError) as exc:
                reader.problems.append(str(exc))
                status["status"] = "incomplete"
                status["records"].extend(derive_delegation_tracking(
                    events=attempt_events, tasks=(), pull_requests=(), issues=(),
                    unavailable_task_ids=frozenset(
                        event["result"]["taskId"] for event in attempt_events
                        if isinstance(event.get("result"), dict) and event["result"].get("taskId")
                    ),
                    unavailable_issue_numbers=frozenset(
                        event["target"]["number"] for event in attempt_events
                        if event.get("eventType") == "delegation-baseline"
                    ),
                ))
    elif (snapshot.get("delegationStatus") or {}).get("records"):
        # A historical snapshot is not a substitute for the current assignment
        # ledger. Preserve it as explicitly stale reporting evidence.
        status = copy.deepcopy(snapshot["delegationStatus"])
        status["status"] = "incomplete"
        reader.problems.append("Assignment ledger unavailable; retained delegation evidence is frozen, not reobserved.")
    issues = {issue["issueNumber"]: issue for issue in prepared.get("issues", [])}
    proposals = action_proposals or {}
    bound_proposals = proposals.get("proposals", []) if (
        proposals.get("repository") == repository and proposals.get("snapshotId") == prepared["snapshotId"]
    ) else []
    current_states: dict[int, dict[str, object]] = {}
    for record in status["records"]:
        issue_number = record["issueNumber"]
        issue = issues.get(issue_number, {})
        instructions = next((
            event["customInstructions"] for event in reversed(frozen_events)
            if event.get("actionId") == record["actionId"] and event.get("eventType") in {"intent", "delegation-baseline"}
            and event.get("operation") == "assign-copilot"
            and event.get("target") == {"kind": "issue", "number": issue_number}
            and isinstance(event.get("customInstructions"), str)
        ), None)
        if instructions is None and baselines.get(record["actionId"], {}).get("snapshotId") == prepared["snapshotId"]:
            instructions = next((
                proposal.get("customInstructions") for proposal in bound_proposals
                if proposal.get("actionId") == record["actionId"] and proposal.get("issueNumber") == issue_number
                and proposal.get("operation") == "assign-copilot"
            ), None)
        keep_open, contract_basis = _keep_open_contract(issue_number, instructions, issue)
        for pull in record.get("pullRequests", []):
            number = pull.get("number")
            if not number:
                continue
            pull["url"] = f"https://github.com/{repository}/pull/{number}"
            detail = reader.responses.get(f"/repos/{repository}/pulls/{number}") or {}
            if detail.get("html_url") != pull["url"]:
                detail = {}
            contract = closing_keyword_contract(detail.get("body"), repository, issue_number, keep_open=keep_open)
            contract["basis"] = contract_basis
            pull["closingContract"] = contract
            section = reported_test_execution_section(detail.get("body"))
            if section is not None:
                pull["reportedTestExecution"] = _outcome_body(section, 4000)
            if contract["status"] in {"unavailable", "unknown"}:
                reader.problems.append(f"PR #{number}: {contract['detail']}")
            if number not in current_states:
                check_runs = combined_status = None
                sha = (detail.get("head") or {}).get("sha")
                if isinstance(sha, str) and sha:
                    try:
                        checks = reader.get(f"/repos/{repository}/commits/{sha}/check-runs?per_page=100")
                        if (not isinstance(checks, dict) or not isinstance(checks.get("check_runs"), list)
                                or type(checks.get("total_count")) is not int
                                or checks["total_count"] != len(checks["check_runs"])):
                            raise ValueError(f"PR #{number}: head check inventory incomplete.")
                        check_runs = checks["check_runs"]
                        # Reuse the existing check-state normalizer. Combined
                        # status is necessary only when no check runs exist.
                        if not check_runs:
                            combined_status = reader.get(f"/repos/{repository}/commits/{sha}/status?per_page=100")
                            if (not isinstance(combined_status, dict)
                                    or combined_status.get("total_count") != len(combined_status.get("statuses", []))):
                                raise ValueError(f"PR #{number}: commit status inventory incomplete.")
                    except (GitHubApiError, ValueError, RuntimeError) as exc:
                        reader.problems.append(str(exc))
                        check_runs = combined_status = None
                else:
                    reader.problems.append(f"PR #{number}: current head unavailable; checks unknown.")
                current = build_pull_request_current_state(
                    detail, check_runs=check_runs, combined_status=combined_status,
                )
                if "draft" not in detail:
                    current["draft"] = None
                current_states[number] = current
            pull["currentState"] = current_states[number]
    observed = clock()
    problems = list(dict.fromkeys(reader.problems))
    return {
        "schemaVersion": 1, "repository": repository, "snapshotId": prepared["snapshotId"],
        "frozenEvidenceCollectedAt": snapshot.get("collectedAt"),
        "startedAt": started.isoformat().replace("+00:00", "Z"),
        "observedAt": observed.isoformat().replace("+00:00", "Z"),
        "status": "partial" if problems else "complete",
        "apiCalls": reader.calls, "maxApiCalls": max_api_calls,
        "problems": problems, "delegationStatus": status,
        "authority": "report-only",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a validated CI shepherd report as Markdown.")
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--judgments", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--run-report", action="store_true",
        help="Render grouped work performed, using companion cycle artifacts when present.",
    )
    parser.add_argument("--action-events", type=Path)
    parser.add_argument("--investigation-results", type=Path)
    parser.add_argument("--investigation-sessions", type=Path)
    parser.add_argument("--state-dir", type=Path, help="Read current local reservation capacity for the run report.")
    parser.add_argument("--usage", type=Path)
    parser.add_argument("--as-of")
    parser.add_argument("--run-id")
    parser.add_argument(
        "--finalize-run", action="store_true",
        help="Bounded read-only delegation refresh, canonical final report, then record invocation completion.",
    )
    parser.add_argument("--max-observation-api-calls", type=int, default=30)
    parser.add_argument(
        "--invocation", type=Path,
        help="Existing invocation manifest with runId, boundary scope, startedAt/completedAt, and optional recordingWindows.",
    )
    args = parser.parse_args()
    if args.finalize_run and (not args.run_report or args.invocation is None or args.state_dir is None):
        parser.error("--finalize-run requires --run-report, --invocation, and --state-dir.")
    if args.finalize_run:
        if args.prepared.resolve().is_relative_to((args.state_dir / "runs").resolve()):
            parser.error("Cannot finalize or supersede a sealed historical cycle; use its original work directory.")
        if args.output.resolve().is_relative_to(args.state_dir.resolve()):
            parser.error("The final report must be outside the read-only state directory.")
        if args.output.resolve() in {path.resolve() for path in (args.prepared, args.judgments, args.snapshot, args.invocation)}:
            parser.error("The final report must not overwrite a report input.")
        if not 1 <= args.max_observation_api_calls <= 30:
            parser.error("--max-observation-api-calls must be from 1 through 30.")

    prepared = json.loads(args.prepared.read_text(encoding="utf-8"))
    judgments = json.loads(args.judgments.read_text(encoding="utf-8"))
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    if args.run_report:
        validate_poc_judgments(prepared, judgments)
        invocation = json.loads(args.invocation.read_text(encoding="utf-8")) if args.invocation else {}
        if args.finalize_run and (not invocation.get("runId") or not invocation.get("startedAt")):
            raise ValueError("Finalization requires the recorded invocation runId and startedAt.")
        if args.finalize_run and args.run_id is not None and args.run_id != invocation["runId"]:
            raise ValueError("Finalization run ID must match the recorded invocation.")

        def companion(name: str) -> object:
            path = args.prepared.parent / name
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

        def events(path: Path | None) -> list[object]:
            if path is None:
                return []
            return read_jsonl_rows(path)

        def completed_assessment(*, pre_expansion: bool = False) -> object:
            suffix = ".pre-expansion" if pre_expansion else ""
            recorded = companion(f"assessment-completion{suffix}.json")
            if recorded is None:
                return None
            cycle = companion("cycle.json")
            if not isinstance(cycle, dict):
                raise ValueError("Assessment completion requires its bound cycle manifest.")
            verified = verify_assessment_completion(
                args.prepared.parent,
                cycle.get("previousAssessment" if pre_expansion else "assessment"),
                pre_expansion=pre_expansion,
            )
            if recorded != verified:
                raise ValueError("Recorded assessment completion does not match its verified receipts.")
            return verified

        action_events = events(args.action_events or (args.state_dir / "action-events.jsonl" if args.state_dir else None))
        observation = None
        audit_details = [] if args.finalize_run else None
        render_started = datetime.now(timezone.utc)
        if args.finalize_run:
            observation = refresh_report_delegations(
                snapshot, prepared, action_events,
                client=GitHubClient(
                    runner=subprocess.run, popen_factory=subprocess.Popen, sleep=time.sleep,
                    now=lambda: datetime.now(timezone.utc), max_attempts=1, max_pages=1,
                    request_timeout_seconds=10,
                ),
                max_api_calls=args.max_observation_api_calls,
                action_proposals=companion("action-proposals.json"),
            )
            _write_markdown(
                args.output.parent / "post-execution-observation.json",
                json.dumps(observation, indent=2) + "\n",
            )
            args.as_of = observation["observedAt"]
            # A previous completion does not describe this finalization. Retain
            # all original phase windows, but seal the new completion only once
            # the canonical report and its details have been successfully written.
            invocation = {key: value for key, value in invocation.items() if key != "completedAt"}
            invocation.setdefault("recordingWindows", []).append({
                "label": "Post-effect delegation observation",
                "startedAt": observation["startedAt"], "completedAt": observation["observedAt"],
                "measurement": "recorded-window", "basis": "Bounded GET-only final report observation",
            })
        audit_path = args.prepared.parent / "report-details.md"
        if not audit_path.is_file():
            # Older cycles stored the detailed audit directly in report.md.
            audit_path = args.prepared.parent / "report.md"
        final_audit_path = args.output.parent / "final-report-details.md"
        if args.finalize_run:
            audit_path = final_audit_path
        audit_details_url = (
            quote(Path(os.path.relpath(audit_path, args.output.parent)).as_posix(), safe="/")
            if (audit_path.is_file() or args.finalize_run) and audit_path.resolve() != args.output.resolve()
            else None
        )
        markdown = render_run_markdown(
            snapshot, prepared, judgments,
            review_selection=companion("review-selection.json"),
            pull_request_review=companion("pull-request-review.json"),
            pull_request_judgments=companion("pull-request-judgments.json"),
            investigation_plan=companion("investigation-plan.json"),
            action_events=action_events,
            investigation_results=events(args.investigation_results),
            investigation_capacity=(
                investigation_capacity_inventory(args.state_dir, str(snapshot["repository"]))
                if args.state_dir is not None else None
            ),
            investigation_sessions=events(args.investigation_sessions),
            usage=json.loads(args.usage.read_text(encoding="utf-8")) if args.usage else None,
            as_of=args.as_of,
            run_id=args.run_id or invocation.get("runId"),
            invocation_window=invocation,
            recording_windows=invocation.get("recordingWindows", []),
            audit_details_url=audit_details_url,
            pre_expansion_review_selection=companion("review-selection.pre-expansion.json"),
            pre_expansion_pull_request_review=companion("pull-request-review.pre-expansion.json"),
            assessment_coverage=completed_assessment(),
            pre_expansion_assessment_coverage=completed_assessment(pre_expansion=True),
            assessment_manifest=companion("assessment-batches.json"),
            pre_expansion_assessment_manifest=companion("assessment-batches.pre-expansion.json"),
            post_execution_observation=observation,
            audit_details=audit_details,
        )
        if args.finalize_run:
            source_audit = args.prepared.parent / "report-details.md"
            original = source_audit.read_text(encoding="utf-8") if source_audit.is_file() else ""
            _write_markdown(final_audit_path, original.rstrip() + "\n\n## Post-execution report detail\n\n" + "\n".join(audit_details))
            completion_url = quote(Path(os.path.relpath(args.invocation, args.output.parent)).as_posix(), safe="/")
            markdown = markdown.replace(
                "# CI Shepherd run report",
                "# CI Shepherd run report\n\n**Canonical post-execution report.** The pre-execution decision report is superseded.\n\n"
                f"Final render completion (`completedAt`) is recorded after this file is written in the [invocation manifest]({completion_url}).",
                1,
            )
    else:
        markdown = render_poc_markdown(
            prepared,
            judgments,
            prepared_path=args.prepared.resolve(),
            snapshot=snapshot,
        )

    output = _write_markdown(args.output, markdown)
    if args.finalize_run:
        completed = datetime.now(timezone.utc)
        # This timestamp is measured after the report write, not guessed before
        # it. A render failure therefore cannot mark the invocation completed.
        invocation["completedAt"] = completed.isoformat().replace("+00:00", "Z")
        invocation["finalReport"] = str(output)
        invocation["postExecutionObservation"] = str((args.output.parent / "post-execution-observation.json").resolve())
        invocation.setdefault("recordingWindows", []).append({
            "label": "Final observation and render",
            "startedAt": render_started.isoformat().replace("+00:00", "Z"),
            "completedAt": invocation["completedAt"],
            "measurement": "recorded-window",
            "basis": "render.py --finalize-run entry through successful final report write",
        })
        pre_report = args.prepared.parent / "report.md"
        if pre_report.is_file() and pre_report.resolve() != output:
            relative = quote(Path(os.path.relpath(output, pre_report.parent)).as_posix(), safe="/")
            original_report = pre_report.read_text(encoding="utf-8")
            if original_report.startswith("<!-- superseded-report -->"):
                original_report = original_report.partition("<!-- frozen-report -->\n")[2]
            _write_markdown(
                pre_report,
                "<!-- superseded-report -->\n"
                f"**Superseded pre-execution report.** [Canonical post-execution report]({relative})\n\n"
                "<!-- frozen-report -->\n" + original_report,
            )
        _write_markdown(args.invocation, json.dumps(invocation, indent=2) + "\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
