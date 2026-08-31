#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path

from ci_shepherd.models import validate_report, validate_snapshot
from ci_shepherd.poc import validate_poc_judgments


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a validated CI shepherd report as Markdown.")
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--judgments", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    prepared = json.loads(args.prepared.read_text(encoding="utf-8"))
    judgments = json.loads(args.judgments.read_text(encoding="utf-8"))
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    markdown = render_poc_markdown(
        prepared,
        judgments,
        prepared_path=args.prepared.resolve(),
        snapshot=snapshot,
    )

    print(_write_markdown(args.output, markdown))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
