#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Mapping

from ci_shepherd.actions import build_action_proposals
from ci_shepherd.actor import build_dry_run
from ci_shepherd.assessment_batches import (
    assessment_artifacts, load_assessment_packets, materialize_assessment,
    merge_worker_responses, verify_assessment_completion,
)
from ci_shepherd.collector import MAX_DELEGATION_REQUESTS, validate_delegation_requests
from ci_shepherd.comment_selection import (
    build_comment_selection,
    render_comment_selection_section,
)
from ci_shepherd.ci_failure_triage import (
    attach_ci_failure_triage,
    build_ci_failure_triage,
)
from ci_shepherd.coordinator_state import (
    CoordinatorStateStore,
    make_lock_free_durable_intent_reader,
)
from ci_shepherd.delegations import render_delegation_status_section
from ci_shepherd.evidence_planning import (
    build_proposal_evidence_requests,
    record_unavailable_evidence_wakeups,
)
from ci_shepherd.execution_state import ActionEventStore
from ci_shepherd.history import load_current
from ci_shepherd.investigations import (
    attach_latest_investigation_results,
    build_investigation_plan,
    read_investigation_session_events,
    read_investigation_results,
    render_investigation_section,
)
from ci_shepherd.investigation_worktrees import investigation_capacity_inventory
from ci_shepherd.lifecycle import assessment_issue_update_times, cloud_outcome_issue_numbers, control_comment_only_issue_numbers, prepare_assessment
from ci_shepherd.managed_coverage import (
    block_policy_selection,
    build_managed_item_coverage,
    render_managed_item_coverage_section,
)
from ci_shepherd.models import ValidationError, stable_json, validate_snapshot
from ci_shepherd.operation_policy import load_operation_policy_document
from ci_shepherd.policy import load_policy
from ci_shepherd.poc import build_compact_poc_input
from ci_shepherd.poc_history import current_triage_events, read_ledger_rows
from ci_shepherd.poc_state import (
    load_review_schedule,
    record_review_events,
    record_review_wakeup,
)
from ci_shepherd.pull_requests import build_pull_request_handoff
from ci_shepherd.pull_requests import (
    merge_pull_request_judgments,
    render_pull_request_section,
)
from ci_shepherd.policy_selection import (
    build_policy_selection,
    render_policy_selection_section,
)
from ci_shepherd.quarantine import (
    build_quarantine_session_plan,
    build_quarantine_session_request,
    collect_quarantine_source_state,
    inspect_quarantine_session_request,
    read_quarantine_session_events,
    render_quarantine_session_section,
)
from ci_shepherd.quarantine_reconciliation import (
    quarantine_labeled_test_names,
    reconcile_quarantine_source,
    render_quarantine_source_reconciliation_section,
)
from ci_shepherd.repository_policy import load_embedded_repository_policy
from ci_shepherd.review_selection import build_review_selection
from ci_shepherd.run_report import render_run_markdown
from ci_shepherd.timeutils import format_utc_z, parse_aware_iso8601
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


def _schedule_positive_coverage_reviews(
    state_dir: Path,
    repository: str,
    *,
    observed_at: str,
    selected_issue_numbers: set[int],
    judgments: Mapping[str, Any],
    observations: Mapping[str, Any],
    interval_days: int,
) -> None:
    awaiting_coverage = {
        int(occurrence["issueNumber"])
        for occurrence in observations.get("occurrences", [])
        if isinstance(occurrence, Mapping)
        and isinstance(occurrence.get("issueNumber"), int)
        and not isinstance(occurrence.get("issueNumber"), bool)
        and occurrence.get("coverageState") == "needs-positive-coverage"
    }
    for judgment in judgments.get("issues", []):
        if not isinstance(judgment, Mapping):
            continue
        issue_number = judgment.get("issueNumber")
        recommendations = judgment.get("recommendations")
        if (
            not isinstance(issue_number, int)
            or isinstance(issue_number, bool)
            or issue_number not in selected_issue_numbers
            or issue_number not in awaiting_coverage
            or not isinstance(recommendations, list)
            or {
                recommendation.get("disposition")
                for recommendation in recommendations
                if isinstance(recommendation, Mapping)
            }
            != {"no-action"}
        ):
            continue
        evaluate_at = format_utc_z(
            parse_aware_iso8601(observed_at, "snapshot collectedAt")
            + timedelta(days=interval_days)
        )
        record_review_wakeup(
            state_dir,
            repository,
            target_kind="issue",
            target_number=issue_number,
            evaluate_at=evaluate_at,
            reason="positive-coverage-review",
        )


def _coordinator_stage(
    projection: Mapping[str, object],
    selection: Mapping[str, object],
    *,
    now: datetime,
) -> str:
    if selection["selectedActionIds"]:
        return "ready"
    effective_policy = projection["effectivePolicy"]
    if isinstance(effective_policy, Mapping):
        policy = load_operation_policy_document(
            {
                key: value
                for key, value in effective_policy.items()
                if key != "policyDigest"
            }
        )
        if policy.active_at(now):
            return "policy-active"
    return "awaiting-policy"


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


def _empty_agent_assessment(snapshot_id: str) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "snapshotId": snapshot_id,
        "issues": [],
        "pullRequests": [],
    }


def _split_agent_assessment(
    document: object,
    *,
    snapshot_id: str,
) -> tuple[dict[str, object], dict[str, object]]:
    if not isinstance(document, Mapping):
        raise ValidationError("Agent assessment must be a JSON object.")
    expected_fields = {"schemaVersion", "snapshotId", "issues", "pullRequests"}
    unsupported = set(document) - expected_fields
    missing = expected_fields - set(document)
    if unsupported:
        raise ValidationError(
            f"Agent assessment has unsupported fields: {sorted(unsupported)}."
        )
    if missing:
        raise ValidationError(
            f"Agent assessment is missing fields: {sorted(missing)}."
        )
    if document.get("schemaVersion") != 1:
        raise ValidationError("Agent assessment schemaVersion must be 1.")
    if document.get("snapshotId") != snapshot_id:
        raise ValidationError(
            "Agent assessment snapshotId must match the current cycle."
        )
    issues = document.get("issues")
    if not isinstance(issues, list):
        raise ValidationError("Agent assessment issues must be an array.")
    pull_requests = document.get("pullRequests")
    if not isinstance(pull_requests, list):
        raise ValidationError("Agent assessment pullRequests must be an array.")
    return (
        {
            "schemaVersion": 1,
            "snapshotId": snapshot_id,
            "issues": issues,
        },
        {
            "schemaVersion": 1,
            "snapshotId": snapshot_id,
            "pullRequests": pull_requests,
        },
    )


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

    for path in assessment_artifacts(work_dir):
        preserved = path.with_name(path.stem + ".pre-expansion.json")
        shutil.copyfile(path, preserved)
        preserved.chmod(0o600)
    _write_private_json(work_dir / "judgments.pre-expansion.json", final_judgments)
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

    # Delegated cases enter assessment only when due. Expansion must retain that
    # frozen review set without enrolling every active delegation.
    reviewed_delegations = set(expanded_snapshot.get("delegatedIssues", [])) & {
        issue["issueNumber"] for issue in final_judgments["issues"]
    }
    assessment_snapshot = {
        **expanded_snapshot,
        "openIssues": sorted(set(expanded_snapshot["openIssues"]) | reviewed_delegations),
    }
    state_dir = Path(str(manifest["stateDirectory"]))
    prepared, triage = _prepare_with_ci_failure_triage(
        assessment_snapshot,
        state_dir,
        issue_update_times=assessment_issue_update_times(
            assessment_snapshot, snapshot,
            _load_json(work_dir / "assessment-input.json", "pre-expansion assessment"),
        ),
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
    _write_private_json(work_dir / "ci-failure-triage.json", triage)
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
    _write_private_json(
        work_dir / "agent-assessment.json",
        _empty_agent_assessment(str(prepared["snapshotId"])),
    )
    restarted: dict[str, object] = {
        **manifest,
        "previousAssessment": manifest["assessment"],
        "assessment": materialize_assessment(work_dir),
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
    *,
    control_only_issue_numbers: frozenset[int] = frozenset(),
) -> set[int]:
    if previous_prepared is None:
        return set()
    compact = build_compact_poc_input(prepared)
    previous_compact = build_compact_poc_input(previous_prepared)
    for document in (compact, previous_compact):
        for issue in document["issues"]:
            if issue["issueNumber"] not in control_only_issue_numbers:
                continue
            # Normalize comparison copies only. Frozen evidence timestamps still
            # bind action preflight and remain visible in the assessment/report.
            if isinstance(issue.get("automationContext"), dict):
                issue["automationContext"].pop("updatedAt", None)
            context = issue.get("delegationContext", {})
            if isinstance(context.get("activity"), dict):
                context["activity"].pop("issueUpdatedAt", None)
            # Reported test identities can cite the issue itself. Its raw
            # timestamp changes this audit fingerprint even when all independent
            # evidence is proven unchanged. Keep rules, facts and history compared.
            for case in issue.get("ciFailureTriage", {}).get("cases", []):
                case.pop("evidenceFingerprint", None)
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


def _prepare_with_ci_failure_triage(
    snapshot: Mapping[str, Any],
    state_dir: Path,
    *,
    issue_update_times: Mapping[int, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    prepared = prepare_assessment(snapshot, issue_update_times=issue_update_times)
    triage = build_ci_failure_triage(
        prepared,
        history_rows=current_triage_events(
            read_ledger_rows(state_dir / "ledgers" / "fingerprints.jsonl")
        ),
    )
    prepared = attach_ci_failure_triage(prepared, triage)
    prepared = attach_latest_investigation_results(
        prepared,
        read_investigation_results(state_dir),
    )
    return prepared, triage


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
    max_comments: int = 5,
    delegation_requests: Iterable[int] = (),
    state_origin: str = "explicit",
) -> dict[str, object]:
    delegation_requests = validate_delegation_requests(delegation_requests)
    if delegation_requests and input_path is not None:
        raise ValueError("--delegate-issue requires live collection and cannot be combined with --input.")
    if state_origin not in {"explicit", "canonical"}:
        raise ValueError("State origin must be explicit or canonical.")
    started_at = format_utc_z(datetime.now(UTC))
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
            delegation_requests=delegation_requests,
        )
    else:
        supplied_input = input_path.expanduser().resolve(strict=True)
        if supplied_input == target_input.resolve(strict=False):
            raise ValueError("Supplied input must be outside the cycle work directory.")
        shutil.copyfile(supplied_input, target_input)
        target_input.chmod(0o600)

    snapshot = _load_json(target_input, "snapshot")
    if input_path is not None:
        # Replaying collection artifacts must not renew an earlier operator request.
        snapshot.pop("delegationRequests", None)
        _write_private_json(target_input, snapshot)
    validate_snapshot(snapshot)
    if str(snapshot.get("repository", "")).casefold() != repository.casefold():
        raise ValueError("Snapshot repository does not match the requested repository.")
    open_issue_numbers = {
        number
        for number in snapshot.get("openIssues", [])
        if isinstance(number, int) and not isinstance(number, bool)
    }
    active_delegated_issue_numbers = {
        number
        for number in snapshot.get("delegatedIssues", [])
        if isinstance(number, int) and not isinstance(number, bool)
    }
    open_pull_request_numbers = [
        number
        for number in snapshot.get("openPullRequests", [])
        if isinstance(number, int) and not isinstance(number, bool)
    ]
    review_schedule = load_review_schedule(
        state_dir,
        repository,
        str(snapshot["collectedAt"]),
        issue_numbers=sorted(open_issue_numbers | active_delegated_issue_numbers),
        pull_request_numbers=open_pull_request_numbers,
    )
    _write_private_json(work_dir / "review-schedule.json", review_schedule)
    due_issue_numbers = set(review_schedule["dueIssueNumbers"])
    outcome_delegated_issue_numbers = (
        cloud_outcome_issue_numbers(snapshot) & active_delegated_issue_numbers
    )
    # Outcome assessment is independent of public reminder eligibility. Retain
    # these attempts in ordinary validated history so unchanged conclusions carry
    # forward instead of disappearing and being selected again on the next cycle.
    issue_reassessment_context = {
        int(number): context
        for number, context in review_schedule["issues"].items()
        if int(number) in open_issue_numbers | due_issue_numbers | outcome_delegated_issue_numbers
    }
    pull_request_reassessment_context = {
        int(number): context
        for number, context in review_schedule["pullRequests"].items()
    }
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

    due_delegated_issue_numbers = (
        due_issue_numbers & active_delegated_issue_numbers
    )
    assessment_delegated_issue_numbers = (
        due_delegated_issue_numbers | outcome_delegated_issue_numbers
    )
    assessment_snapshot = snapshot
    if assessment_delegated_issue_numbers:
        assessment_snapshot = {
            **snapshot,
            "openIssues": sorted(
                open_issue_numbers | assessment_delegated_issue_numbers
            ),
        }
    prepared, triage = _prepare_with_ci_failure_triage(
        assessment_snapshot,
        state_dir,
        issue_update_times=assessment_issue_update_times(
            assessment_snapshot, previous_snapshot, previous_prepared,
        ),
    )
    compact = build_compact_poc_input(prepared)
    control_only_issue_numbers = frozenset(control_comment_only_issue_numbers(snapshot, previous_snapshot))
    source_changed_issue_numbers = set(
        _refresh_issue_numbers(snapshot, "changedIssueNumbers")
    ) - control_only_issue_numbers
    derived_changed_issue_numbers = _changed_prepared_issues(
        prepared,
        previous_prepared,
        control_only_issue_numbers=control_only_issue_numbers,
    )
    triage_rule_changed = (
        previous_prepared is not None
        and previous_prepared.get("triageRuleVersion")
        != prepared.get("triageRuleVersion")
    )
    assessed_issue_numbers = {issue["issueNumber"] for issue in compact["issues"]}
    requested_delegation_issue_numbers = (
        set(snapshot.get("delegationRequests", [])) & assessed_issue_numbers
    )
    withdrawn_delegation_issue_numbers = (
        set((previous_snapshot or {}).get("delegationRequests", []))
        - requested_delegation_issue_numbers
    ) & assessed_issue_numbers
    # Operator intent belongs to this invocation. Both renewal and withdrawal
    # invalidate retained judgments, even when the issue's source is unchanged.
    changed_issue_numbers = (
        source_changed_issue_numbers | derived_changed_issue_numbers
        | requested_delegation_issue_numbers | withdrawn_delegation_issue_numbers
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
            *(
                ["triage-rule-changed"]
                if triage_rule_changed
                else []
            ),
            *(
                ["operator-delegation-request"]
                if issue_number in requested_delegation_issue_numbers
                else []
            ),
            *(
                ["operator-delegation-request-withdrawn"]
                if issue_number in withdrawn_delegation_issue_numbers
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
        "triage": work_dir / "ci-failure-triage.json",
        "defaults": work_dir / "assessment-defaults.json",
        "compact": work_dir / "agent-input.json",
        "selection": work_dir / "review-selection.json",
        "pullRequests": work_dir / "pull-request-review.json",
    }
    _write_private_json(paths["prepared"], prepared)
    _write_private_json(paths["triage"], triage)
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
    _write_private_json(
        work_dir / "agent-assessment.json",
        _empty_agent_assessment(prepared["snapshotId"]),
    )
    coordinator_revision, coordinator_error = _checkout_revision(Path(__file__).resolve().parents[2])
    checkout_revision, checkout_error = _checkout_revision(checkout) if checkout is not None else (None, None)
    manifest: dict[str, object] = {
        "schemaVersion": 1,
        "startedAt": started_at,
        "repository": repository,
        "snapshotId": prepared["snapshotId"],
        "stateDirectory": str(state_dir.expanduser().resolve(strict=False)),
        "checkout": (
            str(checkout.expanduser().resolve(strict=False))
            if checkout is not None
            else None
        ),
        "shepherdAuthor": shepherd_author,
        "maxComments": max_comments,
        "maxDelegationRequests": MAX_DELEGATION_REQUESTS,
        "baseRunId": (
            getattr(current_history, "run_id", None)
            if current_history is not None
            else None
        ),
        "stage": "awaiting-review",
        "issueReviewCount": issue_review_count,
        "pullRequestReviewCount": pull_request_review_count,
        "assessment": materialize_assessment(work_dir),
        "invocation": {
            "collectionMode": "live" if input_path is None else "replay",
            "stateOrigin": state_origin,
            "stateMode": "resume" if current_history is not None else "bootstrap",
            "coordinatorRevision": coordinator_revision,
            "checkoutRevision": checkout_revision,
            "provenanceDiagnostics": [
                {"source": source, "message": error}
                for source, error in (("coordinator", coordinator_error), ("checkout", checkout_error))
                if error is not None
            ],
            "frozenSourceRevisions": sorted({
                record["payload"]["checkoutCommit"]
                for record in snapshot.get("evidence", {}).values()
                if isinstance(record, Mapping)
                and isinstance(record.get("payload"), Mapping)
                and isinstance(record["payload"].get("checkoutCommit"), str)
            } | (
                {snapshot["sourceRevision"]}
                if isinstance(snapshot.get("sourceRevision"), str) else set()
            ) | (
                {snapshot["quarantineSourceState"]["sourceRevision"]}
                if isinstance(snapshot.get("quarantineSourceState"), Mapping)
                and isinstance(snapshot["quarantineSourceState"].get("sourceRevision"), str)
                else set()
            )),
        },
    }
    _write_private_json(work_dir / "cycle.json", manifest)

    if issue_review_count == 0 and pull_request_review_count == 0:
        return finish_cycle(
            work_dir=work_dir,
            agent_assessment_path=work_dir / "agent-assessment.json",
        )
    return manifest


def merge_assessments(
    *,
    work_dir: Path,
    response_paths: Iterable[Path] = (),
) -> dict[str, object]:
    work_dir = work_dir.expanduser().resolve(strict=True)
    cycle_manifest = _load_json(work_dir / "cycle.json", "cycle manifest")
    if cycle_manifest.get("stage") != "awaiting-review":
        raise ValueError("Cycle is not awaiting assessment responses.")
    manifest, packets = load_assessment_packets(work_dir, cycle_manifest.get("assessment"))
    paths = list(response_paths)
    if not paths:
        paths = [work_dir / group["responseFile"] for group in manifest["workerGroups"]]
    responses = [_load_json(path, "assessment worker response") for path in paths]
    combined, receipts, summary = merge_worker_responses(manifest, packets, responses)
    # Publish receipts last, so an interrupted merge cannot leave old complete
    # coverage paired with a partially replaced aggregate response.
    _write_private_json(work_dir / "assessment-receipts.json", {
        "schemaVersion": 1, "assessmentId": manifest["assessmentId"], "batches": [],
    })
    (work_dir / "assessment-completion.json").unlink(missing_ok=True)
    _write_private_json(work_dir / "agent-assessment.json", combined)
    _write_private_json(work_dir / "assessment-receipts.json", receipts)
    return summary


def finish_cycle(
    *,
    work_dir: Path,
    agent_assessment_path: Path | None = None,
    agent_judgments_path: Path | None = None,
    pull_request_judgments_path: Path | None = None,
    assessment_receipts_path: Path | None = None,
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
    max_comments = manifest.get("maxComments", 5)
    if not all(isinstance(value, str) and value for value in (repository, state_directory, shepherd_author)):
        raise ValueError("Cycle manifest identity is incomplete.")
    state_dir = Path(state_directory)
    _ensure_separate_directories(state_dir, work_dir)
    assessment_completion = verify_assessment_completion(
        work_dir, manifest.get("assessment"), receipts_path=assessment_receipts_path,
    )
    prior_assessment_completion = (
        verify_assessment_completion(
            work_dir, manifest.get("previousAssessment"), pre_expansion=True,
        )
        if manifest.get("evidenceExpansionRound") is not None else None
    )
    if agent_assessment_path is not None:
        if agent_judgments_path is not None or pull_request_judgments_path is not None:
            raise ValueError(
                "Combined agent assessment cannot be used with legacy judgment paths."
            )
        combined = _load_json(agent_assessment_path, "agent assessment")
        issue_judgments, pull_request_judgments = _split_agent_assessment(
            combined,
            snapshot_id=str(manifest.get("snapshotId")),
        )
        canonical_assessment_path = work_dir / "agent-assessment.json"
        _write_private_json(canonical_assessment_path, combined)
        agent_judgments_path = work_dir / "agent-judgments.json"
        pull_request_judgments_path = (
            work_dir / "agent-pull-request-judgments.json"
        )
        _write_private_json(agent_judgments_path, issue_judgments)
        _write_private_json(
            pull_request_judgments_path,
            pull_request_judgments,
        )
    elif agent_judgments_path is None:
        raise ValueError(
            "Finish requires a combined agent assessment or legacy issue judgments."
        )

    paths = {
        "input": work_dir / "input.json",
        "prepared": work_dir / "assessment-input.json",
        "triage": work_dir / "ci-failure-triage.json",
        "defaults": work_dir / "assessment-defaults.json",
        "compact": work_dir / "agent-input.json",
        "selection": work_dir / "review-selection.json",
        "judgments": work_dir / "judgments.json",
        "pullRequestHandoff": work_dir / "pull-request-review.json",
        "pullRequestJudgments": work_dir / "pull-request-judgments.json",
        "report": work_dir / "report.md",
        "proposals": work_dir / "action-proposals.json",
        "commentSelection": work_dir / "comment-selection.json",
        "policySelection": work_dir / "policy-selection.json",
        "coordinatorProjection": work_dir / "coordinator-projection.json",
        "dryRun": work_dir / "actor-dry-run.json",
        "quarantineSession": work_dir / "quarantine-session.json",
        "quarantineReconciliation": work_dir / "quarantine-reconciliation.json",
        "quarantineEvidence": work_dir / "quarantine-evidence.json",
        "investigationPlan": work_dir / "investigation-plan.json",
        "reviewSchedule": work_dir / "review-schedule.json",
        "managedCoverage": work_dir / "managed-item-coverage.json",
    }
    assert agent_judgments_path is not None
    _write_private_json(work_dir / "assessment-completion.json", assessment_completion)
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
    quarantine_evidence = prepared["observations"]
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
    labeled_test_names = quarantine_labeled_test_names({
        **prepared,
        "issues": [
            issue for issue in prepared["issues"]
            if issue["issueNumber"] not in snapshot.get("delegationRequests", [])
        ],
    })
    quarantine_reconciliation = reconcile_quarantine_source(
        prepared,
        (
            snapshot["quarantineSourceState"]
            if "quarantineSourceState" in snapshot
            else collect_quarantine_source_state(
                Path(checkout_value) if isinstance(checkout_value, str) else None,
                labeled_test_names,
            )
            if labeled_test_names is not None
            else None
        ),
        read_quarantine_session_events(state_dir),
    )
    _write_private_json(
        paths["quarantineReconciliation"],
        quarantine_reconciliation,
    )
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
        quarantine_reconciliation=quarantine_reconciliation,
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
    # action-proposals.json is written before expansion planning so the planner can
    # inspect it. Add this capability only after the planner has confirmed that no
    # restart is needed; authorization can then distinguish a finalized round-0
    # document from provisional proposals that will be superseded by round 1.
    evidence_round = manifest.get("evidenceExpansionRound")
    if evidence_round is None:
        evidence_round = 0
    if evidence_round not in {0, 1}:
        raise ValueError("Completed cycle has an unsupported evidence round.")
    proposals = {
        **proposals,
        "productionPilotCapability": {
            "schemaVersion": 1,
            "evidenceRound": evidence_round,
        },
    }
    _write_private_json(paths["proposals"], proposals)
    review_selection = _load_json(paths["selection"], "review selection")
    selected_issue_numbers = {
        int(entry["issueNumber"])
        for entry in review_selection["selected"]
        if isinstance(entry, Mapping)
        and isinstance(entry.get("issueNumber"), int)
        and not isinstance(entry.get("issueNumber"), bool)
    }
    manual_policy = load_policy(DEFAULT_POLICY_PATH)
    _schedule_positive_coverage_reviews(
        state_dir,
        repository,
        observed_at=str(snapshot["collectedAt"]),
        selected_issue_numbers=selected_issue_numbers,
        judgments=final_judgments,
        observations=quarantine_evidence,
        interval_days=manual_policy.systemic_transient_window_days,
    )
    review_schedule = load_review_schedule(
        state_dir,
        repository,
        str(snapshot["collectedAt"]),
        issue_numbers=sorted(
            {
                *(
                    int(number)
                    for number in snapshot.get("openIssues", [])
                    if isinstance(number, int) and not isinstance(number, bool)
                ),
                *(
                    int(number)
                    for number in snapshot.get("delegatedIssues", [])
                    if isinstance(number, int) and not isinstance(number, bool)
                ),
            }
        ),
        pull_request_numbers=[
            int(number)
            for number in snapshot.get("openPullRequests", [])
            if isinstance(number, int) and not isinstance(number, bool)
        ],
    )
    _write_private_json(paths["reviewSchedule"], review_schedule)
    embedded_repository_policy = snapshot.get("repositoryPolicy")
    repository_policy = (
        load_embedded_repository_policy(
            embedded_repository_policy,
            repository,
        )
        if isinstance(embedded_repository_policy, Mapping)
        else None
    )
    managed_coverage = build_managed_item_coverage(
        snapshot,
        policy=repository_policy,
        proposals=proposals,
        investigation_plan=investigation_plan,
        review_schedule=review_schedule,
        observations=quarantine_evidence,
        observation_error=(
            str(quarantine_evidence["error"])
            if isinstance(quarantine_evidence.get("error"), str)
            else None
        ),
    )
    _write_private_json(paths["managedCoverage"], managed_coverage)
    capability = proposals.get("productionPilotCapability")
    if not isinstance(capability, Mapping):
        raise ValueError("Finalized proposals are missing production capability.")
    proposals = {
        **proposals,
        "productionPilotCapability": {
            **capability,
            "managedItemCoverage": {
                "schemaVersion": 2,
                "valid": managed_coverage["valid"],
                "blockers": managed_coverage["blockers"],
                "globalBlockers": managed_coverage["globalBlockers"],
                "blockedScopes": managed_coverage["blockedScopes"],
            },
        },
    }
    _write_private_json(paths["proposals"], proposals)
    comment_selection = build_comment_selection(
        proposals,
        max_comments=max_comments,
    )
    _write_private_json(paths["commentSelection"], comment_selection)
    coordinator_now = datetime.now(UTC)
    coordinator_store = CoordinatorStateStore(
        state_dir,
        durable_intent_reader=make_lock_free_durable_intent_reader(
            state_dir / "action-events.jsonl"
        ),
    )
    coordinator_projection = coordinator_store.projection(
        repository,
        now=coordinator_now,
    )
    policy_selection = build_policy_selection(
        proposals,
        run_id=f"cycle:{prepared['snapshotId']}",
        policy_projection=coordinator_projection,
        action_events=ActionEventStore(state_dir).events(repository=repository),
        now=coordinator_now,
    )
    policy_selection = block_policy_selection(policy_selection, managed_coverage)
    _write_private_json(paths["policySelection"], policy_selection)
    _write_private_json(paths["coordinatorProjection"], coordinator_projection)
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
    visible_issue_numbers.update(
        int(issue["issueNumber"])
        for issue in final_judgments["issues"]
        if isinstance(issue, Mapping)
        and isinstance(issue.get("issueNumber"), int)
        and not isinstance(issue.get("issueNumber"), bool)
        and any(
            isinstance(recommendation, Mapping)
            and recommendation.get("disposition") == "review-quarantine"
            for recommendation in issue.get("recommendations", [])
        )
    )
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
        + render_comment_selection_section(comment_selection)
        + "\n"
        + render_policy_selection_section(
            policy_selection,
            heading="Autonomous policy selection at cycle finalization",
            introduction=(
                "This section is authoritative for autonomous policy execution. "
                "The legacy production comment pilot selection above is retained "
                "only for migration diagnostics and does not authorize actions."
            ),
        )
        + "\n"
        + render_investigation_section(investigation_plan)
        + "\n"
        + render_quarantine_session_section(quarantine_plan)
        + "\n"
        + render_quarantine_source_reconciliation_section(
            quarantine_reconciliation
        )
        + "\n"
        + render_delegation_status_section(
            snapshot.get("delegationStatus"),
            proposals,
        )
        + "\n"
        + render_managed_item_coverage_section(managed_coverage)
    )
    audit_report_path = work_dir / "report-details.md"
    _write_private_text(audit_report_path, report_markdown)
    report_as_of = format_utc_z(datetime.now(UTC))
    recording_windows = []
    if manifest.get("startedAt") is not None:
        recording_windows.append({
            "label": "Cycle start to decision projection",
            "startedAt": manifest["startedAt"], "completedAt": report_as_of,
        })
    report_markdown = render_run_markdown(
        snapshot, prepared, final_judgments,
        review_selection=review_selection,
        pull_request_review=pull_request_handoff,
        pull_request_judgments=pull_request_judgments,
        investigation_plan=investigation_plan,
        investigation_results=read_investigation_results(state_dir),
        investigation_sessions=read_investigation_session_events(state_dir),
        investigation_capacity=investigation_capacity_inventory(state_dir, repository),
        action_events=ActionEventStore(state_dir).events(repository=repository),
        prior_snapshot=_previous_context(state_dir, repository)[1],
        as_of=report_as_of,
        recording_windows=recording_windows,
        pre_expansion_review_selection=(
            _load_json(work_dir / "review-selection.pre-expansion.json", "pre-expansion review selection")
            if (work_dir / "review-selection.pre-expansion.json").is_file() else None
        ),
        pre_expansion_pull_request_review=(
            _load_json(work_dir / "pull-request-review.pre-expansion.json", "pre-expansion pull request handoff")
            if (work_dir / "pull-request-review.pre-expansion.json").is_file() else None
        ),
        assessment_coverage=assessment_completion,
        pre_expansion_assessment_coverage=prior_assessment_completion,
        assessment_manifest=_load_json(work_dir / "assessment-batches.json", "assessment manifest"),
        pre_expansion_assessment_manifest=(
            _load_json(work_dir / "assessment-batches.pre-expansion.json", "pre-expansion assessment manifest")
            if (work_dir / "assessment-batches.pre-expansion.json").is_file() else None
        ),
    )
    _write_private_text(paths["report"], report_markdown.rstrip() + "\n\n[Audit details](report-details.md)\n")
    dry_run = build_dry_run(proposals, action_id=None)
    _write_private_json(paths["dryRun"], dry_run)

    verify_assessment_completion(work_dir, manifest["assessment"])
    if prior_assessment_completion is not None:
        verify_assessment_completion(work_dir, manifest["previousAssessment"], pre_expansion=True)
    run_directory = record_poc_cycle(
        state_dir=state_dir,
        input_path=paths["input"],
        prepared_path=paths["prepared"],
        judgments_path=paths["judgments"],
        report_path=paths["report"],
        artifact_paths=[
            *assessment_artifacts(work_dir),
            *sorted(work_dir.glob("assessment-*.pre-expansion.json")),
            audit_report_path,
            work_dir / "agent-assessment.json",
            paths["compact"],
            paths["defaults"],
            paths["selection"],
            work_dir / "pull-request-review.json",
            paths["pullRequestJudgments"],
            paths["proposals"],
            paths["commentSelection"],
            paths["policySelection"],
            paths["coordinatorProjection"],
            paths["dryRun"],
            paths["quarantineSession"],
            paths["quarantineReconciliation"],
            paths["investigationPlan"],
            paths["triage"],
            paths["reviewSchedule"],
            paths["managedCoverage"],
            *[
                path
                for path in (
                    work_dir / "api-calls.jsonl",
                    work_dir / "progress.json",
                    work_dir / "input.pre-expansion.json",
                    work_dir / "action-proposals.pre-expansion.json",
                    work_dir / "review-selection.pre-expansion.json",
                    work_dir / "pull-request-review.pre-expansion.json",
                    work_dir / "judgments.pre-expansion.json",
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
    record_unavailable_evidence_wakeups(state_dir, snapshot)
    completed = {
        **manifest,
        "assessment": {**manifest["assessment"], **assessment_completion},
        "decisionProjectedAt": report_as_of,
        "stage": "completed",
        "coordinatorStage": _coordinator_stage(
            coordinator_projection,
            policy_selection,
            now=coordinator_now,
        ),
        "runDirectory": str(run_directory),
        "proposalCount": len(proposals["proposals"]),
        "selectedCommentCount": comment_selection["selectedCount"],
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
        "managedItemCoverageValid": managed_coverage["valid"],
        "managedItemCoverageBlockers": managed_coverage["blockers"],
    }
    _write_private_json(manifest_path, completed)
    return completed


def _checkout_revision(checkout: Path) -> tuple[str | None, str | None]:
    # Inherited Git routing/config variables must not redirect this identity
    # probe into another worktree or invoke caller-configured helpers.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        result = subprocess.run(
            [
                "git", "--no-pager", "-C", str(checkout),
                "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false",
                "rev-parse", "HEAD",
            ],
            capture_output=True, text=True, check=False, timeout=10, env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, f"Revision probe unavailable: {error}"
    revision = result.stdout.strip()
    if result.returncode != 0:
        return None, f"Revision probe failed (exit {result.returncode}): {result.stderr.strip()}"
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        return None, "Revision probe returned an invalid commit identity."
    return revision, None


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
    start.add_argument(
        "--state-dir", type=Path,
        help=f"Durable state directory (default: {DEFAULT_STATE_DIR}). Use an explicit path for isolated trials.",
    )
    start.add_argument("--work-dir", type=Path)
    start.add_argument("--checkout", type=Path)
    start.add_argument("--shepherd-author", required=True)
    start.add_argument("--input", type=Path)
    start.add_argument("--full-refresh", action="store_true")
    start.add_argument(
        "--delegate-issue", type=int, action="append", default=[],
        help=f"Nominate an open issue in --repository for delegation review (repeatable; maximum {MAX_DELEGATION_REQUESTS}). Not approval.",
    )
    start.add_argument("--max-comments", type=int, default=5)
    start.add_argument(
        "--repository-policy",
        type=Path,
        default=DEFAULT_REPOSITORY_POLICY_PATH,
    )
    finish = subparsers.add_parser("finish")
    finish.add_argument("--work-dir", type=Path, required=True)
    response = finish.add_mutually_exclusive_group(required=True)
    response.add_argument("--agent-assessment", type=Path)
    response.add_argument("--agent-judgments", type=Path)
    finish.add_argument("--pull-request-judgments", type=Path)
    finish.add_argument(
        "--assessment-receipts", type=Path,
        help="Explicit batch/case/evidence coverage receipts (default: work-dir/assessment-receipts.json).",
    )
    merge = subparsers.add_parser(
        "merge-assessments",
        help="Merge whole-group worker responses; missing groups remain incomplete.",
    )
    merge.add_argument("--work-dir", type=Path, required=True)
    merge.add_argument(
        "--response", type=Path, action="append", default=[],
        help="Worker response file (repeatable). Defaults to all materialized group response files.",
    )
    args = parser.parse_args()
    if args.command == "start":
        try:
            validate_delegation_requests(args.delegate_issue)
        except ValueError as error:
            parser.error(str(error))

    old_umask = os.umask(0o077)
    try:
        if args.command == "start":
            result = start_cycle(
                repository=args.repository,
                state_dir=args.state_dir or DEFAULT_STATE_DIR,
                work_dir=args.work_dir or _default_work_dir(),
                checkout=args.checkout,
                shepherd_author=args.shepherd_author,
                input_path=args.input,
                full_refresh=args.full_refresh,
                repository_policy_path=args.repository_policy,
                max_comments=args.max_comments,
                delegation_requests=args.delegate_issue,
                state_origin="explicit" if args.state_dir is not None else "canonical",
            )
        elif args.command == "merge-assessments":
            result = merge_assessments(work_dir=args.work_dir, response_paths=args.response)
        else:
            result = finish_cycle(
                work_dir=args.work_dir,
                agent_assessment_path=args.agent_assessment,
                agent_judgments_path=args.agent_judgments,
                pull_request_judgments_path=args.pull_request_judgments,
                assessment_receipts_path=args.assessment_receipts,
            )
    finally:
        os.umask(old_umask)
    print(stable_json(result), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
