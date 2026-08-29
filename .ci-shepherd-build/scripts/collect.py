#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import time

from ci_shepherd.collector import BOT_AUTHORS, Collector, InventoryResult
from ci_shepherd.github import GitHubClient
from ci_shepherd.history import load_current
from ci_shepherd.models import stable_json, validate_snapshot
from ci_shepherd.progress import ProgressTracker
from ci_shepherd.refresh import RefreshPlan, complete_refresh_plan


DEFAULT_COLLECTION_BUDGETS = {
    "max_supporting_closed": 20,
    "max_run_refs_per_issue": 12,
    "max_issue_refs_per_issue": 5,
    "max_commit_refs_per_issue": 3,
    "marker_candidates": 3,
    "fact_candidates": 3,
}


def build_snapshot(repository: str, collected_at: datetime, inventory: InventoryResult) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "schemaVersion": 1,
        "repository": repository,
        "collectedAt": collected_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "openIssues": [int(issue["number"]) for issue in inventory.open_issues],
        "issues": inventory.open_issues,
        "openPullRequests": [
            int(pull_request["number"])
            for pull_request in inventory.open_pull_requests
        ],
        "pullRequests": inventory.open_pull_requests,
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
    return snapshot


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
) -> Path:
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
        if shepherd_author is not None:
            collector_options["shepherd_author"] = shepherd_author
        collector = Collector(
            client,
            repository,
            now,
            **collector_options,
        )
        current = load_current(state_dir, repository) if state_dir is not None else None

        current_stage = "inventory"
        progress.update(current_stage, "started", message="Refreshing the open issue inventory.")
        if current is None:
            inventory = collector.collect(
                include_supporting=True,
                include_timeline=False,
            )
        else:
            previous_snapshot = json.loads(
                (current.run_directory / "snapshot.json").read_text(encoding="utf-8")
            )
            validate_snapshot(previous_snapshot)
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

        current_stage = "github-enrichment"
        progress.update(current_stage, "started", message="Enriching selected GitHub evidence.")
        inventory = collector.enrich_github_evidence(
            inventory,
            include_issue_references=True,
            minimal_run_evidence=True,
            include_run_history=True,
        )
        progress.update(
            current_stage,
            "completed",
            message=f"Collected {len(inventory.evidence)} evidence records.",
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
        snapshot = build_snapshot(repository, now, inventory)
        validate_snapshot(snapshot)
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
    parser.add_argument("--shepherd-author")
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
        )
    finally:
        os.umask(old_umask)
    print(output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
