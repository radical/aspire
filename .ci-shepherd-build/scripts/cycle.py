#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
from typing import Any, Mapping

from ci_shepherd.actions import build_action_proposals
from ci_shepherd.actor import build_dry_run
from ci_shepherd.evidence_planning import build_proposal_evidence_requests
from ci_shepherd.history import load_current
from ci_shepherd.investigations import (
    attach_latest_investigation_results,
    build_investigation_plan,
    read_investigation_session_events,
    read_investigation_results,
    render_investigation_section,
)
from ci_shepherd.lifecycle import prepare_assessment
from ci_shepherd.models import stable_json, validate_snapshot
from ci_shepherd.observations import build_observations
from ci_shepherd.policy import load_policy
from ci_shepherd.poc import build_compact_poc_input
from ci_shepherd.poc_state import load_review_schedule, record_review_events
from ci_shepherd.pull_requests import build_pull_request_handoff
from ci_shepherd.pull_requests import (
    merge_pull_request_judgments,
    render_pull_request_section,
)
from ci_shepherd.quarantine import (
    build_quarantine_session_plan,
    build_quarantine_session_request,
    inspect_quarantine_session_request,
    read_quarantine_session_events,
    render_quarantine_session_section,
)
from ci_shepherd.review_selection import build_review_selection
from collect import collect
from expand import expand_files
from finalize import finalize
from record_poc import record_poc_cycle
from render import render_poc_markdown


DEFAULT_STATE_DIR = Path.home() / ".copilot" / "ci-shepherd" / "state"
DEFAULT_RUNS_DIR = Path.home() / ".copilot" / "ci-shepherd" / "runs"
DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[1] / "policies" / "manual-v1.json"
DEFAULT_REPOSITORY_POLICY_PATH = (
    Path(__file__).resolve().parents[1]
    / "policies"
    / "repositories"
    / "aspire-v1.json"
)


def _write_private_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(stable_json(document), encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_private_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _ensure_separate_directories(state_dir: Path, work_dir: Path) -> None:
    state = state_dir.expanduser().resolve(strict=False)
    work = work_dir.expanduser().resolve(strict=False)
    if state == work or state in work.parents or work in state.parents:
        raise ValueError("State and cycle work directories must not contain each other.")
    for path, label in ((state, "state"), (work, "cycle work")):
        if path.exists() and path.is_symlink():
            raise ValueError(f"{label.capitalize()} directory must not be a symlink.")


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Unable to read {label}: {error}") from error
    if not isinstance(document, dict):
        raise ValueError(f"{label.capitalize()} must be a JSON object.")
    return document


def _previous_context(
    state_dir: Path,
    repository: str,
    current: Any | None = None,
) -> tuple[
    set[int] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
    list[dict[str, Any]] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    current = current if current is not None else load_current(state_dir, repository)
    if current is None:
        return None, None, None, None, None, None
    known = {
        issue["issueNumber"]
        for issue in current.previous_decisions
        if isinstance(issue, Mapping)
        and isinstance(issue.get("issueNumber"), int)
        and not isinstance(issue.get("issueNumber"), bool)
    }
    previous_snapshot = _load_json(current.run_directory / "snapshot.json", "previous snapshot")
    validate_snapshot(previous_snapshot)
    previous_prepared_path = current.run_directory / "assessment-input.json"
    previous_prepared = (
        _load_json(previous_prepared_path, "previous prepared assessment")
        if previous_prepared_path.is_file()
        else None
    )
    previous_pull_request_handoff_path = (
        current.run_directory / "pull-request-review.json"
    )
    previous_pull_request_handoff = (
        _load_json(
            previous_pull_request_handoff_path,
            "previous pull request handoff",
        )
        if previous_pull_request_handoff_path.is_file()
        else None
    )
    previous_pull_request_judgments_path = (
        current.run_directory / "pull-request-judgments.json"
    )
    previous_pull_request_judgments = (
        _load_json(
            previous_pull_request_judgments_path,
            "previous pull request judgments",
        )
        if previous_pull_request_judgments_path.is_file()
        else None
    )
    if (
        previous_pull_request_handoff is not None
        and previous_pull_request_judgments is None
    ):
        # Runs recorded before pull-request judgment persistence have a handoff
        # but no judgment document. Treat them as unreviewed once so rollout
        # reselects their open pull requests instead of failing or retaining a
        # judgment that was never recorded.
        previous_pull_request_handoff = None
    return (
        known,
        previous_snapshot,
        previous_prepared,
        current.previous_decisions,
        previous_pull_request_handoff,
        previous_pull_request_judgments,
    )


def _refresh_issue_numbers(snapshot: Mapping[str, Any], field: str) -> list[int]:
    summary = snapshot.get("refreshSummary")
    if not isinstance(summary, Mapping):
        return []
    values = summary.get(field, [])
    if not isinstance(values, list):
        raise ValueError(f"refreshSummary.{field} must be a list.")
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in values):
        raise ValueError(f"refreshSummary.{field} must contain integers.")
    return values


def _empty_agent_judgments(snapshot_id: str) -> dict[str, object]:
    return {"schemaVersion": 1, "snapshotId": snapshot_id, "issues": []}


def _empty_pull_request_judgments(snapshot_id: str) -> dict[str, object]:
    return {"schemaVersion": 1, "snapshotId": snapshot_id, "pullRequests": []}


def _retain_pull_request_reviews(
    handoff: Mapping[str, Any],
    judgments: Mapping[str, Any],
    *,
    snapshot_id: str,
) -> dict[str, object]:
    judgments_by_number = {
        int(judgment["pullRequestNumber"]): judgment
        for judgment in judgments["pullRequests"]
    }
    retained = list(handoff["excluded"])
    for task in handoff["tasks"]:
        retained_task = {**task, "changeClass": "retained"}
        number = int(task["target"]["number"])
        retained.append(
            {
                "number": number,
                "reason": "unchanged-stable",
                "retainedTask": retained_task,
                "retainedJudgment": judgments_by_number[number],
            }
        )
    return {
        **handoff,
        "snapshotId": snapshot_id,
        "tasks": [],
        "excluded": retained,
    }


def _restart_after_evidence_expansion(
    *,
    work_dir: Path,
    manifest_path: Path,
    manifest: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    final_judgments: Mapping[str, Any],
    proposals: Mapping[str, Any],
    pull_request_handoff: Mapping[str, Any],
    pull_request_judgments: Mapping[str, Any],
) -> dict[str, object] | None:
    if manifest.get("evidenceExpansionRound") is not None:
        return None

    request_document, deferred_evidence_ids = build_proposal_evidence_requests(
        snapshot,
        proposals,
    )
    requests = request_document["requests"]
    if not requests:
        return None

    input_path = work_dir / "input.json"
    requests_path = work_dir / "evidence-requests.json"
    expanded_path = work_dir / "input.expanded.json"
    errors_path = work_dir / "evidence-expansion-errors.json"
    audit_path = work_dir / "api-calls.jsonl"
    shutil.copyfile(input_path, work_dir / "input.pre-expansion.json")
    (work_dir / "input.pre-expansion.json").chmod(0o600)
    shutil.copyfile(
        work_dir / "review-selection.json",
        work_dir / "review-selection.pre-expansion.json",
    )
    (work_dir / "review-selection.pre-expansion.json").chmod(0o600)
    shutil.copyfile(
        work_dir / "pull-request-review.json",
        work_dir / "pull-request-review.pre-expansion.json",
    )
    (work_dir / "pull-request-review.pre-expansion.json").chmod(0o600)
    _write_private_json(work_dir / "action-proposals.pre-expansion.json", proposals)
    _write_private_json(requests_path, request_document)
    _write_private_json(
        work_dir / "evidence-expansion-plan.json",
        {
            "schemaVersion": 1,
            "repository": manifest["repository"],
            "round": 1,
            "requestCount": len(requests),
            "deferredEvidenceIds": deferred_evidence_ids,
        },
    )
    checkout_value = manifest.get("checkout")
    expand_files(
        input_path,
        requests_path,
        expanded_path,
        errors_path,
        checkout=(
            Path(checkout_value) if isinstance(checkout_value, str) else None
        ),
        audit_path=audit_path,
    )
    expanded_snapshot = _load_json(expanded_path, "expanded snapshot")
    validate_snapshot(expanded_snapshot)
    repository = str(manifest["repository"])
    if (
        str(expanded_snapshot.get("repository", "")).casefold()
        != repository.casefold()
    ):
        raise ValueError(
            "Expanded snapshot repository does not match the cycle repository."
        )
    _write_private_json(input_path, expanded_snapshot)

    prepared = attach_latest_investigation_results(
        prepare_assessment(expanded_snapshot),
        read_investigation_results(Path(str(manifest["stateDirectory"]))),
    )
    compact = build_compact_poc_input(prepared)
    expanded_issue_numbers = {
        int(request["sourceIssueNumber"]) for request in requests
    }
    selection = build_review_selection(
        compact,
        new_issue_numbers=[],
        changed_issue_numbers=expanded_issue_numbers,
        due_issue_numbers=set(),
        known_issue_numbers={
            int(issue["issueNumber"])
            for issue in compact["issues"]
            if isinstance(issue, Mapping)
            and isinstance(issue.get("issueNumber"), int)
            and not isinstance(issue.get("issueNumber"), bool)
        },
        change_reasons_by_issue={
            issue_number: ["exact-evidence-expanded"]
            for issue_number in expanded_issue_numbers
        },
        previous_judgments=final_judgments["issues"],
        reassessment_context_by_issue={},
    )
    selected_issue_numbers = {
        int(item["issueNumber"])
        for item in selection["selected"]
        if isinstance(item, Mapping)
        and isinstance(item.get("issueNumber"), int)
        and not isinstance(item.get("issueNumber"), bool)
    }
    agent_compact = {
        **compact,
        "issues": [
            issue
            for issue in compact["issues"]
            if isinstance(issue, Mapping)
            and issue.get("issueNumber") in selected_issue_numbers
        ],
    }
    expanded_pull_request_handoff = _retain_pull_request_reviews(
        pull_request_handoff,
        pull_request_judgments,
        snapshot_id=str(prepared["snapshotId"]),
    )
    _write_private_json(work_dir / "assessment-input.json", prepared)
    _write_private_json(work_dir / "assessment-defaults.json", compact)
    _write_private_json(work_dir / "agent-input.json", agent_compact)
    _write_private_json(work_dir / "review-selection.json", selection)
    _write_private_json(
        work_dir / "pull-request-review.json",
        expanded_pull_request_handoff,
    )
    _write_private_json(
        work_dir / "agent-judgments.json",
        _empty_agent_judgments(str(prepared["snapshotId"])),
    )
    _write_private_json(
        work_dir / "agent-pull-request-judgments.json",
        _empty_pull_request_judgments(str(prepared["snapshotId"])),
    )
    restarted: dict[str, object] = {
        **manifest,
        "snapshotId": prepared["snapshotId"],
        "stage": "awaiting-review",
        "issueReviewCount": len(selection["selected"]),
        "pullRequestReviewCount": len(expanded_pull_request_handoff["tasks"]),
        "evidenceExpansionRound": 1,
        "evidenceExpansionRequestCount": len(requests),
        "deferredEvidenceExpansionCount": len(deferred_evidence_ids),
    }
    _write_private_json(manifest_path, restarted)
    return restarted


def _changed_prepared_issues(
    prepared: Mapping[str, Any],
    previous_prepared: Mapping[str, Any] | None,
) -> set[int]:
    if previous_prepared is None:
        return set()
    compact = build_compact_poc_input(prepared)
    previous_compact = build_compact_poc_input(previous_prepared)
    previous = {
        issue["issueNumber"]: issue
        for issue in previous_compact.get("issues", [])
        if isinstance(issue, Mapping)
        and isinstance(issue.get("issueNumber"), int)
        and not isinstance(issue.get("issueNumber"), bool)
    }
    return {
        issue["issueNumber"]
        for issue in compact.get("issues", [])
        if isinstance(issue, Mapping)
        and isinstance(issue.get("issueNumber"), int)
        and not isinstance(issue.get("issueNumber"), bool)
        and previous.get(issue["issueNumber"]) != issue
    }


def start_cycle(
    *,
    repository: str,
    state_dir: Path,
    work_dir: Path,
    checkout: Path | None,
    shepherd_author: str,
    input_path: Path | None = None,
    full_refresh: bool = False,
    repository_policy_path: Path = DEFAULT_REPOSITORY_POLICY_PATH,
) -> dict[str, object]:
    _ensure_separate_directories(state_dir, work_dir)
    if work_dir.exists() and any(work_dir.iterdir()):
        raise ValueError(f"Cycle work directory is not empty: {work_dir}")
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    work_dir.chmod(0o700)

    current_history = load_current(state_dir, repository)
    (
        known_issue_numbers,
        previous_snapshot,
        previous_prepared,
        previous_judgments,
        previous_pull_request_handoff,
        previous_pull_request_judgments,
    ) = _previous_context(state_dir, repository, current_history)
    target_input = work_dir / "input.json"
    if input_path is None:
        collect(
            repository,
            work_dir,
            checkout,
            state_dir=state_dir,
            full_refresh=full_refresh,
            shepherd_author=shepherd_author,
            repository_policy_path=repository_policy_path,
        )
    else:
        supplied_input = input_path.expanduser().resolve(strict=True)
        if supplied_input == target_input.resolve(strict=False):
            raise ValueError("Supplied input must be outside the cycle work directory.")
        shutil.copyfile(supplied_input, target_input)
        target_input.chmod(0o600)

    snapshot = _load_json(target_input, "snapshot")
    validate_snapshot(snapshot)
    if str(snapshot.get("repository", "")).casefold() != repository.casefold():
        raise ValueError("Snapshot repository does not match the requested repository.")
    open_issue_numbers = [
        number
        for number in snapshot.get("openIssues", [])
        if isinstance(number, int) and not isinstance(number, bool)
    ]
    open_pull_request_numbers = [
        number
        for number in snapshot.get("openPullRequests", [])
        if isinstance(number, int) and not isinstance(number, bool)
    ]
    review_schedule = load_review_schedule(
        state_dir,
        repository,
        str(snapshot["collectedAt"]),
        issue_numbers=open_issue_numbers,
        pull_request_numbers=open_pull_request_numbers,
    )
    issue_reassessment_context = {
        int(number): context
        for number, context in review_schedule["issues"].items()
    }
    pull_request_reassessment_context = {
        int(number): context
        for number, context in review_schedule["pullRequests"].items()
    }
    due_issue_numbers = set(review_schedule["dueIssueNumbers"])
    due_pull_request_numbers = set(review_schedule["duePullRequestNumbers"])
    reviewed_known_issue_numbers = (
        None
        if known_issue_numbers is None
        else known_issue_numbers & set(issue_reassessment_context)
    )
    initial_review_pull_request_numbers: set[int] = set()
    if previous_snapshot is not None:
        previous_pull_request_numbers = {
            number
            for number in previous_snapshot.get("openPullRequests", [])
            if isinstance(number, int) and not isinstance(number, bool)
        }
        previous_open_pull_requests = (
            set(open_pull_request_numbers) & previous_pull_request_numbers
        )
        initial_review_pull_request_numbers = (
            previous_open_pull_requests
            if previous_pull_request_handoff is None
            else previous_open_pull_requests - set(pull_request_reassessment_context)
        )

    prepared = attach_latest_investigation_results(
        prepare_assessment(snapshot),
        read_investigation_results(state_dir),
    )
    compact = build_compact_poc_input(prepared)
    source_changed_issue_numbers = set(
        _refresh_issue_numbers(snapshot, "changedIssueNumbers")
    )
    derived_changed_issue_numbers = _changed_prepared_issues(
        prepared,
        previous_prepared,
    )
    changed_issue_numbers = (
        source_changed_issue_numbers | derived_changed_issue_numbers
    )
    change_reasons_by_issue = {
        issue_number: [
            *(
                ["issue-source-updated"]
                if issue_number in source_changed_issue_numbers
                else []
            ),
            *(
                ["derived-assessment-changed"]
                if issue_number in derived_changed_issue_numbers
                else []
            ),
        ]
        for issue_number in changed_issue_numbers
    }
    selection = build_review_selection(
        compact,
        new_issue_numbers=_refresh_issue_numbers(snapshot, "newIssueNumbers"),
        changed_issue_numbers=changed_issue_numbers,
        due_issue_numbers=due_issue_numbers,
        known_issue_numbers=reviewed_known_issue_numbers,
        change_reasons_by_issue=change_reasons_by_issue,
        previous_judgments=previous_judgments,
        reassessment_context_by_issue=issue_reassessment_context,
    )
    pull_request_handoff = {
        **build_pull_request_handoff(
            snapshot,
            previous_snapshot=previous_snapshot,
            initial_review_pull_request_numbers=initial_review_pull_request_numbers,
            due_pull_request_numbers=due_pull_request_numbers,
            reassessment_context_by_pull_request=pull_request_reassessment_context,
            previous_handoff=previous_pull_request_handoff,
            previous_judgments=previous_pull_request_judgments,
        ),
        "snapshotId": prepared["snapshotId"],
    }
    selected_issue_numbers = {
        int(item["issueNumber"])
        for item in selection["selected"]
        if isinstance(item, Mapping)
        and isinstance(item.get("issueNumber"), int)
        and not isinstance(item.get("issueNumber"), bool)
    }
    agent_compact = {
        **compact,
        "issues": [
            issue
            for issue in compact["issues"]
            if isinstance(issue, Mapping)
            and issue.get("issueNumber") in selected_issue_numbers
        ],
    }
    paths = {
        "prepared": work_dir / "assessment-input.json",
        "defaults": work_dir / "assessment-defaults.json",
        "compact": work_dir / "agent-input.json",
        "selection": work_dir / "review-selection.json",
        "pullRequests": work_dir / "pull-request-review.json",
    }
    _write_private_json(paths["prepared"], prepared)
    _write_private_json(paths["defaults"], compact)
    _write_private_json(paths["compact"], agent_compact)
    _write_private_json(paths["selection"], selection)
    _write_private_json(paths["pullRequests"], pull_request_handoff)

    issue_review_count = len(selection["selected"])
    pull_request_review_count = len(pull_request_handoff["tasks"])
    _write_private_json(
        work_dir / "agent-judgments.json",
        _empty_agent_judgments(prepared["snapshotId"]),
    )
    _write_private_json(
        work_dir / "agent-pull-request-judgments.json",
        _empty_pull_request_judgments(prepared["snapshotId"]),
    )
    manifest: dict[str, object] = {
        "schemaVersion": 1,
        "repository": repository,
        "snapshotId": prepared["snapshotId"],
        "stateDirectory": str(state_dir.expanduser().resolve(strict=False)),
        "checkout": (
            str(checkout.expanduser().resolve(strict=False))
            if checkout is not None
            else None
        ),
        "shepherdAuthor": shepherd_author,
        "baseRunId": (
            getattr(current_history, "run_id", None)
            if current_history is not None
            else None
        ),
        "stage": "awaiting-review",
        "issueReviewCount": issue_review_count,
        "pullRequestReviewCount": pull_request_review_count,
    }
    _write_private_json(work_dir / "cycle.json", manifest)

    if issue_review_count == 0 and pull_request_review_count == 0:
        agent_judgments = work_dir / "agent-judgments.json"
        _write_private_json(agent_judgments, _empty_agent_judgments(prepared["snapshotId"]))
        return finish_cycle(
            work_dir=work_dir,
            agent_judgments_path=agent_judgments,
            pull_request_judgments_path=work_dir / "agent-pull-request-judgments.json",
        )
    return manifest


def finish_cycle(
    *,
    work_dir: Path,
    agent_judgments_path: Path,
    pull_request_judgments_path: Path | None = None,
) -> dict[str, object]:
    work_dir = work_dir.expanduser().resolve(strict=True)
    manifest_path = work_dir / "cycle.json"
    manifest = _load_json(manifest_path, "cycle manifest")
    if manifest.get("stage") != "awaiting-review":
        raise ValueError("Cycle is not awaiting review.")
    repository = manifest.get("repository")
    state_directory = manifest.get("stateDirectory")
    checkout_value = manifest.get("checkout")
    shepherd_author = manifest.get("shepherdAuthor")
    if not all(isinstance(value, str) and value for value in (repository, state_directory, shepherd_author)):
        raise ValueError("Cycle manifest identity is incomplete.")
    state_dir = Path(state_directory)
    _ensure_separate_directories(state_dir, work_dir)

    paths = {
        "input": work_dir / "input.json",
        "prepared": work_dir / "assessment-input.json",
        "defaults": work_dir / "assessment-defaults.json",
        "compact": work_dir / "agent-input.json",
        "selection": work_dir / "review-selection.json",
        "judgments": work_dir / "judgments.json",
        "pullRequestHandoff": work_dir / "pull-request-review.json",
        "pullRequestJudgments": work_dir / "pull-request-judgments.json",
        "report": work_dir / "report.md",
        "proposals": work_dir / "action-proposals.json",
        "dryRun": work_dir / "actor-dry-run.json",
        "quarantineSession": work_dir / "quarantine-session.json",
        "quarantineEvidence": work_dir / "quarantine-evidence.json",
        "investigationPlan": work_dir / "investigation-plan.json",
    }
    finalize(
        agent_input_path=paths["defaults"],
        agent_judgments_path=agent_judgments_path,
        output_path=paths["judgments"],
        selection_path=paths["selection"],
    )
    final_judgments = _load_json(paths["judgments"], "final judgments")
    if pull_request_judgments_path is None:
        pull_request_judgments_path = work_dir / "agent-pull-request-judgments.json"
    sparse_pull_request_judgments = _load_json(
        pull_request_judgments_path,
        "pull request agent judgments",
    )
    snapshot = _load_json(paths["input"], "snapshot")
    prepared = _load_json(paths["prepared"], "prepared assessment")
    compact = _load_json(paths["defaults"], "assessment defaults")
    pull_request_handoff = _load_json(
        paths["pullRequestHandoff"],
        "pull request handoff",
    )
    pull_request_judgments = merge_pull_request_judgments(
        pull_request_handoff,
        sparse_pull_request_judgments,
    )
    _write_private_json(paths["pullRequestJudgments"], pull_request_judgments)
    try:
        quarantine_evidence = build_observations(
            snapshot,
            policy=load_policy(DEFAULT_POLICY_PATH),
        )
    except ValueError as error:
        quarantine_evidence = {
            "occurrences": [],
            "coverage": [],
            "fingerprints": [],
            "error": str(error),
        }
    _write_private_json(paths["quarantineEvidence"], quarantine_evidence)
    quarantine_request = build_quarantine_session_request(
        prepared,
        final_judgments,
        quarantine_evidence,
    )
    quarantine_request = inspect_quarantine_session_request(
        quarantine_request,
        Path(checkout_value) if isinstance(checkout_value, str) else None,
    )
    quarantine_plan = build_quarantine_session_plan(
        quarantine_request,
        read_quarantine_session_events(state_dir),
    )
    _write_private_json(paths["quarantineSession"], quarantine_plan)
    investigation_plan = build_investigation_plan(
        prepared,
        final_judgments,
        read_investigation_results(state_dir),
        read_investigation_session_events(state_dir),
    )
    _write_private_json(paths["investigationPlan"], investigation_plan)
    issue_proposals = build_action_proposals(
        snapshot,
        prepared,
        final_judgments,
        shepherd_author,
        agent_input=compact,
    )
    proposals = issue_proposals
    _write_private_json(paths["proposals"], proposals)
    restarted = _restart_after_evidence_expansion(
        work_dir=work_dir,
        manifest_path=manifest_path,
        manifest=manifest,
        snapshot=snapshot,
        final_judgments=final_judgments,
        proposals=proposals,
        pull_request_handoff=pull_request_handoff,
        pull_request_judgments=pull_request_judgments,
    )
    if restarted is not None:
        return restarted
    review_selection = _load_json(paths["selection"], "review selection")
    visible_issue_numbers = {
        int(entry["issueNumber"])
        for entry in [
            *review_selection["selected"],
            *proposals["proposals"],
        ]
        if isinstance(entry, Mapping)
        and isinstance(entry.get("issueNumber"), int)
        and not isinstance(entry.get("issueNumber"), bool)
    }
    report_markdown = render_poc_markdown(
        prepared,
        final_judgments,
        prepared_path=paths["prepared"],
        snapshot=snapshot,
        visible_issue_numbers=visible_issue_numbers,
    )
    report_markdown = (
        report_markdown.rstrip()
        + "\n\n"
        + render_pull_request_section(
            pull_request_handoff,
            pull_request_judgments,
        ).lstrip()
        + "\n"
        + render_investigation_section(investigation_plan)
        + "\n"
        + render_quarantine_session_section(quarantine_plan)
    )
    _write_private_text(paths["report"], report_markdown)
    dry_run = build_dry_run(proposals, action_id=None)
    _write_private_json(paths["dryRun"], dry_run)

    run_directory = record_poc_cycle(
        state_dir=state_dir,
        input_path=paths["input"],
        prepared_path=paths["prepared"],
        judgments_path=paths["judgments"],
        report_path=paths["report"],
        artifact_paths=[
            paths["compact"],
            paths["defaults"],
            paths["selection"],
            work_dir / "pull-request-review.json",
            paths["pullRequestJudgments"],
            paths["proposals"],
            paths["dryRun"],
            paths["quarantineSession"],
            paths["investigationPlan"],
            *[
                path
                for path in (
                    work_dir / "api-calls.jsonl",
                    work_dir / "progress.json",
                    work_dir / "input.pre-expansion.json",
                    work_dir / "action-proposals.pre-expansion.json",
                    work_dir / "review-selection.pre-expansion.json",
                    work_dir / "pull-request-review.pre-expansion.json",
                    work_dir / "evidence-requests.json",
                    work_dir / "evidence-expansion-plan.json",
                    work_dir / "evidence-expansion-errors.json",
                )
                if path.is_file()
            ],
        ],
        expected_current_run_id=manifest.get("baseRunId"),
        enforce_expected_current=True,
    )
    reviewed_issue_numbers = {
        int(entry["issueNumber"])
        for entry in review_selection["selected"]
        if isinstance(entry, Mapping)
        and isinstance(entry.get("issueNumber"), int)
        and not isinstance(entry.get("issueNumber"), bool)
    }
    reviewed_pull_request_numbers = {
        int(task["target"]["number"])
        for task in pull_request_handoff["tasks"]
        if isinstance(task, Mapping)
        and isinstance(task.get("target"), Mapping)
        and isinstance(task["target"].get("number"), int)
        and not isinstance(task["target"].get("number"), bool)
    }
    pre_expansion_selection_path = work_dir / "review-selection.pre-expansion.json"
    if pre_expansion_selection_path.is_file():
        pre_expansion_selection = _load_json(
            pre_expansion_selection_path,
            "pre-expansion review selection",
        )
        reviewed_issue_numbers.update(
            int(entry["issueNumber"])
            for entry in pre_expansion_selection["selected"]
            if isinstance(entry, Mapping)
            and isinstance(entry.get("issueNumber"), int)
            and not isinstance(entry.get("issueNumber"), bool)
        )
    pre_expansion_pull_requests_path = (
        work_dir / "pull-request-review.pre-expansion.json"
    )
    if pre_expansion_pull_requests_path.is_file():
        pre_expansion_pull_requests = _load_json(
            pre_expansion_pull_requests_path,
            "pre-expansion pull request handoff",
        )
        reviewed_pull_request_numbers.update(
            int(task["target"]["number"])
            for task in pre_expansion_pull_requests["tasks"]
            if isinstance(task, Mapping)
            and isinstance(task.get("target"), Mapping)
            and isinstance(task["target"].get("number"), int)
            and not isinstance(task["target"].get("number"), bool)
        )
    record_review_events(
        state_dir,
        repository,
        str(snapshot["collectedAt"]),
        issue_numbers=sorted(reviewed_issue_numbers),
        pull_request_numbers=sorted(reviewed_pull_request_numbers),
    )
    completed = {
        **manifest,
        "stage": "completed",
        "runDirectory": str(run_directory),
        "proposalCount": len(proposals["proposals"]),
        "quarantineTestCount": len(quarantine_request["tests"]),
        "quarantineSessionProposed": quarantine_plan["proposal"] is not None,
        "quarantineActiveBatchId": quarantine_plan["activeBatchId"],
        "quarantinePendingPullRequestCount": len(
            quarantine_plan["pendingPullRequests"]
        ),
        "investigationRequestCount": len(investigation_plan["requests"]),
        "deferredInvestigationCount": len(
            investigation_plan["deferredRequests"]
        ),
        "reusedInvestigationCount": len(
            investigation_plan["reusedInvestigationIds"]
        ),
        "activeInvestigationCount": len(
            investigation_plan["activeInvestigationIds"]
        ),
    }
    _write_private_json(manifest_path, completed)
    return completed


def _default_work_dir() -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return DEFAULT_RUNS_DIR / f"manual-{timestamp}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run or resume one incremental CI shepherd cycle."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start")
    start.add_argument("--repository", required=True)
    start.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    start.add_argument("--work-dir", type=Path)
    start.add_argument("--checkout", type=Path)
    start.add_argument("--shepherd-author", required=True)
    start.add_argument("--input", type=Path)
    start.add_argument("--full-refresh", action="store_true")
    start.add_argument(
        "--repository-policy",
        type=Path,
        default=DEFAULT_REPOSITORY_POLICY_PATH,
    )
    finish = subparsers.add_parser("finish")
    finish.add_argument("--work-dir", type=Path, required=True)
    finish.add_argument("--agent-judgments", type=Path, required=True)
    finish.add_argument("--pull-request-judgments", type=Path)
    args = parser.parse_args()

    old_umask = os.umask(0o077)
    try:
        if args.command == "start":
            result = start_cycle(
                repository=args.repository,
                state_dir=args.state_dir,
                work_dir=args.work_dir or _default_work_dir(),
                checkout=args.checkout,
                shepherd_author=args.shepherd_author,
                input_path=args.input,
                full_refresh=args.full_refresh,
                repository_policy_path=args.repository_policy,
            )
        else:
            result = finish_cycle(
                work_dir=args.work_dir,
                agent_judgments_path=args.agent_judgments,
                pull_request_judgments_path=args.pull_request_judgments,
            )
    finally:
        os.umask(old_umask)
    print(stable_json(result), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
