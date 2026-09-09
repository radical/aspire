#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections.abc import Iterable
from dataclasses import asdict, replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any, Mapping

from ci_shepherd.collector import (
    BOT_AUTHORS,
    MAX_DELEGATION_REQUESTS,
    CollectionError,
    Collector,
    InventoryResult,
    enrich_workflow_discovery,
    mark_workflow_issues_changed,
    validate_delegation_requests,
)
from ci_shepherd.delegation_observer import (
    DelegationReadClient, attach_cloud_outcomes, initialize_cloud_outcome,
    observe_commit_comparison, observe_delegations,
)
from ci_shepherd.delegations import (
    active_owned_task_ids_from_events,
    delegation_starts_from_events,
    delegation_records_from_events,
    derive_capacity_usage,
    derive_delegation_tracking,
)
from ci_shepherd.execution_state import ActionEventStore
from ci_shepherd.github import GitHubClient
from ci_shepherd.history import load_current
from ci_shepherd.handoff_reminders import derive_handoff_reminders
from ci_shepherd.models import stable_json, validate_commit_comparison, validate_snapshot
from ci_shepherd.meaningful_progress import attach_meaningful_progress
from ci_shepherd.lifecycle import prepare_assessment
from ci_shepherd.quarantine import collect_quarantine_source_state
from ci_shepherd.quarantine_reconciliation import freeze_quarantine_source, quarantine_labeled_test_names
from ci_shepherd.progress import ProgressTracker
from ci_shepherd.poc_state import record_review_wakeup
from ci_shepherd.refresh import COLLECTION_VERSION, RefreshPlan, complete_refresh_plan
from ci_shepherd.repository_policy import (
    RepositoryPolicy,
    load_repository_policy,
)


DEFAULT_COLLECTION_BUDGETS = {
    "max_supporting_closed": 20,
    "max_run_refs_per_issue": 12,
    "max_issue_refs_per_issue": 5,
    "max_commit_refs_per_issue": 3,
    "marker_candidates": 3,
    "fact_candidates": 3,
}
DEFAULT_REPOSITORY_POLICY_PATH = (
    Path(__file__).resolve().parents[1]
    / "policies"
    / "repositories"
    / "aspire-v1.json"
)
_MAX_REPAIR_COMPARISON_GETS = 12


def collect_repair_comparisons(
    snapshot: dict[str, Any], client: DelegationReadClient, prepared: Mapping[str, Any],
) -> tuple[int, int]:
    validate_snapshot(snapshot)
    comparisons = {
        (record["baseSha"], record["headSha"]): record
        for record in snapshot.get("commitComparisons", [])
    }
    attempted = set()
    requests_made = 0
    gaps = 0
    while True:
        pending = {}
        for issue in [*prepared["issues"], *prepared.get("closedIssueFollowups", [])]:
            for missing in issue.get("repairFollowup", {}).get("missingEvidence", []):
                pair = (missing["baseSha"], missing["headSha"])
                if pair in attempted:
                    continue
                unknown = {
                    "repository": snapshot["repository"], "baseSha": pair[0], "headSha": pair[1],
                    "url": missing["url"], "availability": "unknown", "status": "unknown",
                    "baseCommitSha": None, "mergeBaseSha": None, "behindBy": None,
                }
                validate_commit_comparison(unknown, snapshot["repository"])
                pending.setdefault(pair, (unknown, set()))[1].add(issue["issueNumber"])
        if not pending:
            return requests_made, gaps
        for pair, (unknown, issue_numbers) in sorted(pending.items()):
            attempted.add(pair)
            if requests_made < _MAX_REPAIR_COMPARISON_GETS:
                requests_made += 1
                comparison = observe_commit_comparison(client, snapshot["repository"], *pair)
                message = f"Commit comparison evidence is {comparison['availability']}."
            else:
                comparison = unknown
                message = f"Commit comparison was not queried: {_MAX_REPAIR_COMPARISON_GETS}-request collection limit reached."
            comparisons[pair] = comparison
            if comparison["availability"] != "available":
                gaps += 1
                snapshot["collectionErrors"].append(asdict(CollectionError(
                    "repair-comparison",
                    f"/repos/{snapshot['repository']}/compare/{pair[0]}...{pair[1]}?per_page=1",
                    message,
                    effect="Post-fix verification and recurrence remain unproven for this commit pair.",
                    scope={"kind": "issue", "issueNumbers": sorted(issue_numbers)},
                )))
        snapshot["commitComparisons"] = [comparisons[pair] for pair in sorted(comparisons)]
        # Resolving a failed execution may expose a previously masked successful
        # head. Re-prepare only within the same unique-pair GET budget.
        prepared = prepare_assessment(snapshot)


def build_snapshot(
    repository: str,
    collected_at: datetime,
    inventory: InventoryResult,
    *,
    repository_policy: RepositoryPolicy | None = None,
    delegation_status: dict[str, object] | None = None,
    delegation_requests: Iterable[int] = (),
) -> dict[str, object]:
    delegation_requests = validate_delegation_requests(delegation_requests)
    snapshot: dict[str, object] = {
        "schemaVersion": 1,
        "collectionVersion": COLLECTION_VERSION,
        "repository": repository,
        "collectedAt": collected_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "openIssues": [int(issue["number"]) for issue in inventory.open_issues],
        "issues": inventory.open_issues,
        "openPullRequests": [
            int(pull_request["number"])
            for pull_request in inventory.open_pull_requests
        ],
        "pullRequests": inventory.open_pull_requests,
        "delegatedIssues": [
            int(issue["number"]) for issue in inventory.delegated_issues
        ],
        "delegatedIssueDetails": inventory.delegated_issues,
        "delegatedPullRequests": [
            int(pull_request["number"])
            for pull_request in inventory.delegated_pull_requests
        ],
        "delegatedPullRequestDetails": inventory.delegated_pull_requests,
        "rejectedCandidates": inventory.rejected_candidates,
        "supportingIssues": inventory.supporting_issues,
        "evidence": inventory.evidence,
        "collectionErrors": [asdict(error) for error in inventory.collection_errors],
        "warnings": inventory.warnings,
        "openBotScan": inventory.open_bot_scan,
        "references": {
            str(number): refs
            for number, refs in inventory.references.items()
            if refs
        },
        "delegationStatus": delegation_status
        or {"status": "complete", "records": []},
    }
    if inventory.refresh_plan is not None:
        plan = inventory.refresh_plan
        snapshot["refreshSummary"] = {
            "reusedEvidenceIds": list(plan.reuse),
            "refreshedEvidenceIds": list(plan.refresh),
            "retriedEvidenceIds": list(plan.retry),
            "retiredEvidenceIds": list(plan.retire),
            "newIssueNumbers": list(plan.new_issues),
            "changedIssueNumbers": list(plan.changed_issues),
        }
    if repository_policy is not None:
        snapshot["repositoryPolicy"] = {
            **repository_policy.as_public_dict(),
            "digest": repository_policy.digest,
        }
    if delegation_requests:
        snapshot["delegationRequests"] = list(delegation_requests)
    if inventory.workflow_discovery is not None:
        snapshot["workflowDiscovery"] = inventory.workflow_discovery
    return snapshot


def build_incomplete_delegation_status(
    previous_snapshot: dict[str, object] | None,
    problem: Exception,
) -> dict[str, object]:
    previous_delegation_status = (
        previous_snapshot.get("delegationStatus")
        if previous_snapshot is not None
        else None
    )
    previous_records = (
        previous_delegation_status.get("records", [])
        if isinstance(previous_delegation_status, dict)
        else []
    )
    return {
        "status": "incomplete",
        "records": previous_records,
        "problem": str(problem),
    }


def observe_delegation_status(
    client: object,
    repository: str,
    *,
    events: list[dict[str, object]],
    now: datetime,
) -> tuple[dict[str, object], tuple[str, ...]]:
    starts = delegation_starts_from_events(events)
    known_records = delegation_records_from_events(events)
    active_task_ids = set(active_owned_task_ids_from_events(events))
    retired_task_ids = frozenset(
        start.task_id
        for start in starts
        if start.task_id is not None
        and start.task_id not in active_task_ids
    )
    observation = observe_delegations(
        client,
        repository,
        owned_task_ids=active_task_ids,
        owned_issue_numbers={
            start.issue_number
            for start in starts
            if start.issue_number is not None
        },
        **({"known_records": known_records} if known_records else {}),
    )
    usage = derive_capacity_usage(
        tasks=observation.tasks,
        starts=starts,
        pull_requests=observation.pull_requests,
        evidence=observation.evidence,
        now=now,
        issues=observation.issues,
        retired_task_ids=retired_task_ids,
        task_pull_request_ids=getattr(observation, "task_pull_request_ids", {}),
    )
    tracking_records = [
        record
        for record in derive_delegation_tracking(
            events=events,
            tasks=observation.tasks,
            pull_requests=observation.pull_requests,
            issues=observation.issues,
            task_pull_request_ids=getattr(observation, "task_pull_request_ids", {}),
            unavailable_task_ids=getattr(observation, "unavailable_task_ids", frozenset()),
            unavailable_issue_numbers=getattr(observation, "unavailable_issue_numbers", frozenset()),
        )
    ]
    for record in tracking_records:
        for pull in record.get("pullRequests", []):
            source = getattr(observation, "pull_request_sources", {}).get(pull["databaseId"])
            if source is not None:
                pull["progressSource"] = dict(source)
        initialize_cloud_outcome(record, getattr(observation, "pull_request_outcome_sources", {}))
    episode_ordinals: dict[str, int] = {}
    episode_counts: dict[str, int] = {}
    historical_retirements = {
        event.get("taskId") for event in events
        if event.get("eventType") == "delegation-retired"
    }
    for start in starts:
        if start.issue_number is None:
            continue
        key = str(start.issue_number)
        episode_counts[key] = episode_counts.get(key, 0) + 1
        episode_ordinals.setdefault(key, 1)
        if start.task_id in historical_retirements:
            episode_ordinals[key] += 1
    for number, record in {
        record["issueNumber"]: record for record in tracking_records
    }.items():
        key = str(number)
        # This is an idempotency namespace, never permission to start again.
        # An exact new decision can also replace an unresolved no-PR attempt.
        next_attempt = (
            record.get("requiresNewDecision") is True
            and record.get("taskId") is not None
        )
        episode_ordinals[key] = max(
            episode_ordinals.get(key, 1),
            episode_counts.get(key, 0) + int(next_attempt),
        )
    status: dict[str, object] = {
        "status": "complete",
        "records": tracking_records,
        "episodeOrdinals": episode_ordinals,
        "capacity": {
            "runningTasks": usage.running_tasks,
            "startsInRolling24h": usage.starts_in_rolling_24h,
            "openDelegatedPullRequests": usage.open_delegated_prs,
            "repositoryRunningTasks": usage.repository_running_tasks,
            "complete": usage.complete,
            "problems": list(usage.problems),
            "warnings": list(usage.warnings),
        },
    }
    newly_retired_task_ids = tuple(
        sorted(
            str(record["taskId"])
            for record in tracking_records
            if record.get("attemptOutcome") in {"merged", "closed-unmerged"}
            and isinstance(record.get("taskId"), str)
            and (
                record.get("taskObservation") != "available"
                or record.get("taskState") not in {"queued", "in_progress"}
            )
            and bool(record.get("pullRequests"))
            and all(
                pull.get("state") not in {"open", "unknown"}
                for pull in record.get("pullRequests", [])
                if isinstance(pull, dict)
            )
        )
    )
    return status, newly_retired_task_ids


def record_delegation_wakeups(
    state_dir: Path,
    repository: str,
    records: list[dict[str, object]],
) -> None:
    for record in records:
        wakeup = record.get("nextWakeup")
        if wakeup is None:
            continue
        if not isinstance(wakeup, dict):
            raise TypeError("Delegation nextWakeup must be an object.")
        issue_number = record.get("issueNumber")
        evaluate_at = wakeup.get("evaluateAt")
        reason = wakeup.get("reason")
        if (
            not isinstance(issue_number, int)
            or isinstance(issue_number, bool)
            or issue_number <= 0
            or not isinstance(evaluate_at, str)
            or not evaluate_at
            or not isinstance(reason, str)
            or not reason
        ):
            raise ValueError("Delegation nextWakeup is incomplete.")
        record_review_wakeup(
            state_dir,
            repository,
            target_kind="issue",
            target_number=issue_number,
            evaluate_at=evaluate_at,
            reason=reason,
        )


def retain_tracked_delegations(
    inventory: InventoryResult,
    records: list[dict[str, object]],
) -> InventoryResult:
    issue_numbers = {
        record["issueNumber"]
        for record in records
        if isinstance(record.get("issueNumber"), int)
        and not isinstance(record.get("issueNumber"), bool)
        and record.get("requiresHuman") is not True
        and record.get("retired") is not True
        and record.get("lifecycle") not in {"completed", "closed_unmerged", "retired"}
    }
    pull_request_numbers = {
        pull_request["number"]
        for record in records
        for pull_request in (
            record.get("pullRequests")
            if isinstance(record.get("pullRequests"), list)
            else []
        )
        if isinstance(pull_request, dict)
        and isinstance(pull_request.get("number"), int)
        and not isinstance(pull_request.get("number"), bool)
    }
    delegated_issues = {
        int(issue["number"]): issue
        for issue in inventory.delegated_issues
    }
    open_issues: dict[int, dict[str, object]] = {}
    for issue in inventory.open_issues:
        number = int(issue["number"])
        if number in issue_numbers:
            delegated_issues[number] = issue
        else:
            open_issues[number] = issue
    delegated_pull_requests = {
        int(pull_request["number"]): pull_request
        for pull_request in inventory.delegated_pull_requests
    }
    open_pull_requests: list[dict[str, object]] = []
    for pull_request in inventory.open_pull_requests:
        number = int(pull_request["number"])
        if number in pull_request_numbers:
            delegated_pull_requests[number] = pull_request
        else:
            open_pull_requests.append(pull_request)
    return replace(
        inventory,
        open_issues=[open_issues[number] for number in sorted(open_issues)],
        delegated_issues=[
            delegated_issues[number] for number in sorted(delegated_issues)
        ],
        open_pull_requests=open_pull_requests,
        delegated_pull_requests=[
            delegated_pull_requests[number]
            for number in sorted(delegated_pull_requests)
        ],
    )


def write_private(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def collect(
    repository: str,
    output_dir: Path,
    checkout: Path | None,
    *,
    max_run_refs_per_issue: int = DEFAULT_COLLECTION_BUDGETS["max_run_refs_per_issue"],
    max_issue_refs_per_issue: int = DEFAULT_COLLECTION_BUDGETS["max_issue_refs_per_issue"],
    max_commit_refs_per_issue: int = DEFAULT_COLLECTION_BUDGETS["max_commit_refs_per_issue"],
    state_dir: Path | None = None,
    full_refresh: bool = False,
    shepherd_author: str | None = None,
    repository_policy_path: Path | None = None,
    delegation_requests: Iterable[int] = (),
    include_workflow_discovery: bool = True,
) -> Path:
    delegation_requests = validate_delegation_requests(delegation_requests)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_dir.chmod(0o700)
    progress = ProgressTracker(output_dir)
    current_stage = "collection"
    progress.update("collection", "started", message="Starting deterministic evidence collection.")
    now = datetime.now(UTC)
    try:
        client = GitHubClient(
            runner=subprocess.run,
            popen_factory=subprocess.Popen,
            sleep=time.sleep,
            now=lambda: datetime.now(UTC),
            audit_path=output_dir / "api-calls.jsonl",
            request_timeout_seconds=60,
            request_observer=lambda _endpoint: progress.heartbeat(
                current_stage,
                message=f"GitHub GET activity in {current_stage}.",
            ),
        )
        budgets = {
            **DEFAULT_COLLECTION_BUDGETS,
            "max_run_refs_per_issue": max_run_refs_per_issue,
            "max_issue_refs_per_issue": max_issue_refs_per_issue,
            "max_commit_refs_per_issue": max_commit_refs_per_issue,
        }
        collector_options: dict[str, object] = {
            "budgets": budgets,
            "bot_authors": BOT_AUTHORS,
        }
        if delegation_requests:
            collector_options["delegation_requests"] = delegation_requests
        if shepherd_author is not None:
            collector_options["shepherd_author"] = shepherd_author
        repository_policy = (
            load_repository_policy(repository_policy_path)
            if repository_policy_path is not None
            else None
        )
        if repository_policy is not None:
            collector_options["repository_policy"] = repository_policy
        current = load_current(state_dir, repository) if state_dir is not None else None
        previous_snapshot: dict[str, object] | None = None
        if current is not None:
            previous_snapshot = json.loads(
                (current.run_directory / "snapshot.json").read_text(encoding="utf-8")
            )
            validate_snapshot(previous_snapshot)

        delegation_status: dict[str, object] = {
            "status": "complete",
            "records": [],
        }
        events = []
        if state_dir is not None and state_dir.exists():
            event_store = ActionEventStore(state_dir)
            events = event_store.events(repository=repository)
            try:
                delegation_status, retired_task_ids = (
                    observe_delegation_status(
                        client,
                        repository,
                        events=events,
                        now=now,
                    )
                )
                if delegation_status["records"]:
                    # The snapshot may fail later. Never retire the only task
                    # lookup before its verified PR binding is durable.
                    event_store.append_delegation_observations(
                        repository=repository,
                        records=delegation_status["records"],
                        at=now,
                    )
                if retired_task_ids:
                    event_store.append_delegation_retirements(
                        repository=repository,
                        task_ids=retired_task_ids,
                        at=now,
                    )
            except Exception as exc:
                delegation_status = build_incomplete_delegation_status(
                    previous_snapshot,
                    exc,
                )
        monitored_issue_numbers = sorted({
            start.issue_number
            for start in delegation_starts_from_events(events)
            if start.issue_number is not None
        })
        if monitored_issue_numbers:
            # Baseline ownership survives task retirement; it is monitoring, not a new request.
            collector_options["monitored_issue_numbers"] = monitored_issue_numbers
        latest_delegations = {
            record["issueNumber"]: record
            for record in delegation_status["records"]
        }
        released_issue_numbers = sorted(
            number for number, record in latest_delegations.items()
            if record.get("retired") is True
            or record.get("lifecycle") in {"completed", "closed_unmerged", "retired"}
        )
        if released_issue_numbers:
            collector_options["released_delegation_issue_numbers"] = released_issue_numbers
        collector = Collector(
            client,
            repository,
            now,
            **collector_options,
        )

        current_stage = "inventory"
        progress.update(current_stage, "started", message="Refreshing the open issue inventory.")
        if current is None:
            inventory = collector.collect(
                include_supporting=True,
                include_timeline=False,
            )
        else:
            assert previous_snapshot is not None
            inventory = collector.collect_incremental(
                previous_snapshot,
                current.document,
                include_supporting=True,
                include_timeline=False,
                full_refresh=full_refresh,
            )
        progress.update(
            current_stage,
            "completed",
            message=f"Collected {len(inventory.open_issues)} open issues.",
        )
        inventory = retain_tracked_delegations(
            inventory,
            list(delegation_status["records"]),
        )

        current_stage = "github-enrichment"
        progress.update(current_stage, "started", message="Enriching selected GitHub evidence.")
        inventory = collector.enrich_github_evidence(
            inventory,
            include_issue_references=True,
            minimal_run_evidence=True,
            include_run_history=True,
            include_retry_evidence=True,
        )
        progress.update(
            current_stage,
            "completed",
            message=f"Collected {len(inventory.evidence)} evidence records.",
        )

        if include_workflow_discovery:
            current_stage = "workflow-discovery"
            progress.update(current_stage, "started", message="Observing bounded default-branch workflow windows.")
            inventory = enrich_workflow_discovery(
                inventory, client, repository, now,
                previous_discovery=previous_snapshot.get("workflowDiscovery") if previous_snapshot is not None else None,
                previous_snapshot=previous_snapshot,
            )
            discovery = inventory.workflow_discovery
            assert discovery is not None
            progress.update(
                current_stage, "completed",
                message=f"Workflow discovery {discovery['status']}; {len(discovery['gaps'])} scoped gaps.",
            )
        else:
            inventory = mark_workflow_issues_changed(
                inventory, previous_snapshot.get("workflowDiscovery") if previous_snapshot is not None else None,
            )

        current_stage = "ownership-enrichment"
        progress.update(current_stage, "started", message="Resolving repository ownership evidence.")
        inventory = collector.enrich_ownership_evidence(
            inventory,
            checkout_path=str(checkout.resolve()) if checkout is not None else None,
        )
        progress.update(current_stage, "completed", message="Ownership enrichment completed.")

        if inventory.refresh_plan is not None:
            inventory = replace(
                inventory,
                refresh_plan=complete_refresh_plan(
                    inventory.refresh_plan,
                    inventory.evidence,
                ),
            )
        elif state_dir is not None or full_refresh:
            inventory = replace(
                inventory,
                refresh_plan=RefreshPlan(
                    refresh=tuple(inventory.evidence),
                    new_issues=tuple(int(issue["number"]) for issue in inventory.open_issues),
                ),
            )

        current_stage = "write-artifacts"
        progress.update(current_stage, "started", message="Validating and writing collection artifacts.")
        snapshot = build_snapshot(
            repository,
            now,
            inventory,
            repository_policy=repository_policy,
            delegation_status=delegation_status,
            delegation_requests=delegation_requests,
        )
        if checkout is not None:
            from ci_shepherd.ownership import OwnershipError, validate_checkout

            try:
                snapshot["sourceRevision"] = validate_checkout(checkout, repository).commit
            except OwnershipError as error:
                # Source availability affects local investigation, not permission
                # to ask cloud Copilot to investigate without a local diagnosis.
                snapshot["warnings"].append(f"Local investigation source unavailable: {error}")
        attach_meaningful_progress(snapshot, previous_snapshot, shepherd_author=shepherd_author)
        attach_cloud_outcomes(snapshot, previous_snapshot, client)
        if previous_snapshot is not None:
            repair_shas = {
                pull["mergeCommitSha"]
                for record in snapshot["delegationStatus"]["records"]
                for pull in record.get("pullRequests", []) if pull.get("mergeCommitSha")
            }
            observed_heads = {
                record["payload"]["headSha"] for record in snapshot["evidence"].values()
                if record.get("kind") == "workflow-run" and record.get("availability") == "available"
                and record["payload"].get("targetRepository") == repository and record["payload"].get("headSha")
            }
            # Ancestry between full immutable commit IDs does not change when
            # an API endpoint becomes unavailable. Keep only still-relevant,
            # previously validated proof; unavailable comparisons are retried.
            comparisons = [
                comparison for comparison in previous_snapshot.get("commitComparisons", [])
                if comparison["availability"] == "available" and comparison["repository"] == repository
                and comparison["baseSha"] in repair_shas and comparison["headSha"] in observed_heads
            ]
            if comparisons:
                snapshot["commitComparisons"] = comparisons
        if snapshot["delegationStatus"]["status"] == "complete" and repository_policy is not None:
            derive_handoff_reminders(
                snapshot["delegationStatus"]["records"], events, repository_policy.handoff_reminders,
            )
        prepared = prepare_assessment(snapshot)
        # An operator can delegate investigation without first inspecting attributes.
        # Other managed quarantine issues still require their usual source evidence.
        labeled_test_names = quarantine_labeled_test_names({
            **prepared,
            "issues": [
                issue for issue in prepared["issues"]
                if issue["issueNumber"] not in delegation_requests
            ],
        })
        if labeled_test_names is not None:
            current_stage = "quarantine-source"
            progress.update(current_stage, "started", message="Inspecting existing quarantine targets.")
            snapshot = freeze_quarantine_source(
                snapshot, prepared, collect_quarantine_source_state(checkout, labeled_test_names),
            )
            progress.update(
                current_stage, "completed",
                message=(
                    "Quarantine source evidence captured."
                    if snapshot["quarantineSourceState"] is not None
                    else "Source inspection unavailable; quarantine targets remain unverified."
                ),
            )
            current_stage = "write-artifacts"
        if labeled_test_names is not None:
            prepared = prepare_assessment(snapshot)
        if any(
            issue.get("repairFollowup", {}).get("missingEvidence")
            for issue in [*prepared["issues"], *prepared.get("closedIssueFollowups", [])]
        ):
            current_stage = "repair-comparison"
            progress.update(current_stage, "started", message="Observing bounded post-repair commit comparisons.")
            requests_made, gaps = collect_repair_comparisons(snapshot, client, prepared)
            progress.update(
                current_stage, "completed",
                message=f"Observed {requests_made} commit comparisons; {gaps} scoped proof gaps.",
            )
            current_stage = "write-artifacts"
        validate_snapshot(snapshot)
        if state_dir is not None and snapshot["delegationStatus"]["status"] == "complete":
            record_delegation_wakeups(state_dir, repository, snapshot["delegationStatus"]["records"])
        write_private(output_dir / "input.json", stable_json(snapshot))
        write_private(
            output_dir / "collection-errors.json",
            stable_json(snapshot["collectionErrors"]),
        )
        progress.update(
            current_stage,
            "completed",
            message="Collection artifacts are ready for assessment.",
        )
        progress.update(
            "collection",
            "completed",
            message="Deterministic evidence collection completed.",
        )
    except Exception as exc:
        progress.update(
            current_stage,
            "failed",
            message="Deterministic evidence collection failed.",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    return output_dir.resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect read-only Aspire CI shepherd evidence.")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--checkout", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--full-refresh", action="store_true")
    parser.add_argument(
        "--skip-workflow-discovery", action="store_true",
        help="Omit default-branch workflow discovery for legacy/offline collection callers.",
    )
    parser.add_argument(
        "--delegate-issue", type=int, action="append", default=[],
        help=f"Nominate an open issue in --repository for delegation review (repeatable; maximum {MAX_DELEGATION_REQUESTS}). Not approval.",
    )
    parser.add_argument("--shepherd-author")
    parser.add_argument(
        "--repository-policy",
        type=Path,
        default=DEFAULT_REPOSITORY_POLICY_PATH,
    )
    parser.add_argument(
        "--max-run-refs-per-issue",
        type=int,
        default=DEFAULT_COLLECTION_BUDGETS["max_run_refs_per_issue"],
    )
    parser.add_argument(
        "--max-issue-refs-per-issue",
        type=int,
        default=DEFAULT_COLLECTION_BUDGETS["max_issue_refs_per_issue"],
    )
    parser.add_argument(
        "--max-commit-refs-per-issue",
        type=int,
        default=DEFAULT_COLLECTION_BUDGETS["max_commit_refs_per_issue"],
    )
    args = parser.parse_args()
    try:
        validate_delegation_requests(args.delegate_issue)
    except ValueError as error:
        parser.error(str(error))

    old_umask = os.umask(0o077)
    try:
        output_dir = collect(
            args.repository,
            args.output_dir,
            args.checkout,
            max_run_refs_per_issue=args.max_run_refs_per_issue,
            max_issue_refs_per_issue=args.max_issue_refs_per_issue,
            max_commit_refs_per_issue=args.max_commit_refs_per_issue,
            state_dir=args.state_dir,
            full_refresh=args.full_refresh,
            shepherd_author=args.shepherd_author,
            repository_policy_path=args.repository_policy,
            delegation_requests=args.delegate_issue,
            include_workflow_discovery=not args.skip_workflow_discovery,
        )
    finally:
        os.umask(old_umask)
    print(output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
