from __future__ import annotations

import copy
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
import hashlib
import re
import subprocess
from typing import TYPE_CHECKING, Any, Iterable
from urllib.parse import quote
from pathlib import Path

from . import ownership
from .pull_requests import build_pull_request_current_state
from .signals import Occurrence, extract_issue_signals, select_references
from .trx import parse_test_results_archive
from .repository_policy import RepositoryPolicy

if TYPE_CHECKING:
    from .refresh import RefreshPlan


TARGET_LABELS = ("ci-failure-cause", "automation-broken")
BOT_AUTHORS = ("github-actions[bot]",)
COPILOT_ASSIGNEES = frozenset(
    {
        "copilot",
        "copilot-swe-agent",
        "copilot-swe-agent[bot]",
        "github-copilot[bot]",
    }
)
_CORRELATION_MARKER_KEYS = frozenset(
    {"automation-broken", "ci-failure", "ci-failure-cause", "gh-aw-failure-issue"}
)
_CORRELATION_FACT_FIELDS = frozenset({"testName"})
_MAX_TEST_RESULTS_ARTIFACT_BYTES = 25 * 1024 * 1024

_DEFAULT_BUDGETS = {
    "max_supporting_closed": 200,
    "max_run_refs_per_issue": 12,
    "max_issue_refs_per_issue": 5,
    "max_commit_refs_per_issue": 3,
    "marker_candidates": 20,
    "fact_candidates": 20,
    # The open bot scan pages the repository's entire open issue+PR list, so
    # both the paging and the number of newly adopted items are capped. On
    # microsoft/aspire the full list is ~2,200 records (22 pages) of which ~150
    # are bot-authored, so these defaults leave roughly an order of magnitude of
    # headroom before truncation kicks in.
    "max_open_scan_pages": 40,
    "max_bot_authored_open": 250,
    "max_primary_pull_requests": 100,
}

OPEN_SCAN_PAGE_SIZE = 100

_LEGACY_HTML_MARKER_RE = re.compile(
    r"<!--\s*ci-shepherd:(?P<key>[a-zA-Z][\w-]*)=(?P<value>.+?)\s*-->",
    re.DOTALL,
)
_ISSUE_COMMENT_EVIDENCE_ID_RE = re.compile(
    r"^issue:(?P<issue_number>[1-9][0-9]*):comment:[1-9][0-9]*$"
)

class InventoryError(RuntimeError):
    pass


def _requires_full_issue_recollection(
    plan: RefreshPlan,
    open_issue_numbers: tuple[int, ...] | list[int],
) -> bool:
    root_issue_ids = {f"issue:{number}" for number in open_issue_numbers}
    return bool(
        plan.new_issues
        or plan.changed_issues
        or root_issue_ids.intersection(plan.retry)
    )


@dataclass(frozen=True, slots=True)
class CollectionError:
    stage: str
    endpoint: str
    message: str
    effect: str | None = None
    scope: dict[str, object] | None = None


def _issue_error_scope(issue_numbers: Iterable[int]) -> dict[str, object] | None:
    scoped_numbers = sorted(set(issue_numbers))
    if not scoped_numbers:
        return None
    return {"kind": "issue", "issueNumbers": scoped_numbers}


@dataclass(frozen=True, slots=True)
class InventoryResult:
    open_issues: list[dict[str, Any]]
    supporting_issues: list[dict[str, Any]]
    evidence: dict[str, dict[str, Any]]
    collection_errors: list[CollectionError]
    warnings: list[str]
    references: dict[int, list[dict[str, Any]]]
    refresh_plan: RefreshPlan | None = None
    open_pull_requests: list[dict[str, Any]] = field(default_factory=list)
    rejected_candidates: list[dict[str, Any]] = field(default_factory=list)
    open_bot_scan: dict[str, Any] | None = None


@dataclass(slots=True)
class _NormalizedIssue:
    issue: dict[str, Any]
    references: list[dict[str, Any]]
    markers: list[dict[str, Any]]
    facts: list[dict[str, Any]]
    occurrences: list[dict[str, Any]]


@dataclass(slots=True)
class _CandidateIssue:
    issue: dict[str, Any]
    markers: list[dict[str, Any]]
    facts: list[dict[str, Any]]


@dataclass(slots=True)
class _IssueSummary:
    raw_issue: dict[str, Any]
    labels: list[str]
    issue: dict[str, Any]
    references: list[dict[str, Any]]
    excluded_references: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class _SupportingQueueEntry:
    candidate_issue_number: int
    root_open_issue_numbers: frozenset[int]
    depth: int
    source_association: dict[str, Any]
    explicit: bool


@dataclass(slots=True)
class _SupportingTraversalState:
    remaining: int
    probed_issue_numbers: set[int]
    selected_candidates_by_root: dict[int, set[int]]
    truncated_by_root: dict[int, bool]
    queue: deque[_SupportingQueueEntry] = field(default_factory=deque)
    roots_by_issue: dict[int, set[int]] = field(default_factory=lambda: defaultdict(set))
    dispositions_by_issue_and_root: dict[tuple[int, int], str] = field(default_factory=dict)
    provenance_by_issue_root_disposition: dict[tuple[int, int, str], list[dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    loaded_supporting_details: dict[int, _NormalizedIssue] = field(default_factory=dict)
    fetched_issue_summaries: dict[int, _IssueSummary] = field(default_factory=dict)
    failed_issue_numbers: set[int] = field(default_factory=set)
    processed_depth_by_root: dict[tuple[int, int], int] = field(default_factory=dict)
    budget_excluded_issue_numbers: set[int] = field(default_factory=set)
    skipped_explicit_detail_issue_numbers: set[int] = field(default_factory=set)
    skipped_other_issue_numbers: set[int] = field(default_factory=set)


class Collector:
    def __init__(
        self,
        client: Any,
        repository: str,
        now: datetime,
        lookback_days: int = 90,
        budgets: dict[str, int] | None = None,
        bot_authors: tuple[str, ...] = (),
        shepherd_author: str | None = None,
        repository_policy: RepositoryPolicy | None = None,
    ) -> None:
        self._client = client
        self._repository = repository
        self._repository_policy = repository_policy
        if (
            self._repository_policy is not None
            and not self._repository_policy.supports_repository(repository)
        ):
            raise ValueError(
                f"Repository policy does not support repository {repository}."
            )
        self._now = now.astimezone(UTC)
        self._lookback_days = lookback_days
        self._bot_authors = bot_authors
        self._shepherd_author = (
            shepherd_author.casefold()
            if isinstance(shepherd_author, str) and shepherd_author.strip()
            else None
        )
        self._budgets = dict(_DEFAULT_BUDGETS)
        if budgets:
            self._budgets.update(budgets)
        self._collected_at = _isoformat(self._now)
        self._cutoff = self._now - timedelta(days=lookback_days)
        self._cutoff_text = _isoformat(self._cutoff)
        self._collection_errors: list[CollectionError] = []
        self._warnings: list[str] = []
        self._evidence: dict[str, dict[str, Any]] = {}
        self._supporting_searches: dict[int, dict[str, Any]] = {}
        self._supporting_roots_by_issue: dict[int, set[int]] = {}
        self._supporting_dispositions_by_issue_and_root: dict[tuple[int, int], str] = {}
        self._supporting_provenance_by_issue_root_disposition: dict[
            tuple[int, int, str], list[dict[str, Any]]
        ] = defaultdict(list)
        self._reference_truncated_issues: set[int] = set()
        self._supporting_budget_excluded_issue_numbers: set[int] = set()
        self._supporting_probe_failed_issue_numbers: set[int] = set()
        self._supporting_comment_failed_issue_numbers: set[int] = set()
        self._open_pull_requests: dict[int, dict[str, Any]] = {}
        self._rejected_candidates: dict[tuple[str, int], dict[str, Any]] = {}
        self._open_bot_scan: dict[str, Any] | None = None

    def collect(
        self,
        *,
        include_supporting: bool = True,
        include_timeline: bool = True,
        _open_seed: dict[int, dict[str, Any]] | None = None,
    ) -> InventoryResult:
        open_seed = (
            copy.deepcopy(_open_seed)
            if _open_seed is not None
            else self._fetch_open_inventory()
        )
        recent_closed_seed: dict[int, dict[str, Any]] = {}
        closed_inventory_failed = False
        self._supporting_budget_excluded_issue_numbers.clear()
        self._supporting_probe_failed_issue_numbers.clear()
        self._supporting_comment_failed_issue_numbers.clear()
        self._supporting_roots_by_issue.clear()
        self._supporting_dispositions_by_issue_and_root.clear()
        self._supporting_provenance_by_issue_root_disposition.clear()

        if include_supporting:
            for label in TARGET_LABELS:
                closed_endpoint = self._issue_query_endpoint("closed", label)
                try:
                    closed_items = self._client.get_pages(closed_endpoint)
                except Exception as exc:
                    self._collection_errors.append(CollectionError("closed-label", closed_endpoint, str(exc)))
                    closed_inventory_failed = True
                    continue
                self._merge_issue_inventory(recent_closed_seed, closed_items, label, require_recently_closed=True)
            for author in self._bot_authors:
                closed_endpoint = self._bot_issue_query_endpoint("closed", author)
                try:
                    closed_items = self._client.get_pages(closed_endpoint)
                except Exception as exc:
                    self._collection_errors.append(
                        CollectionError("closed-author", closed_endpoint, str(exc))
                    )
                    closed_inventory_failed = True
                    continue
                self._merge_issue_inventory(
                    recent_closed_seed,
                    closed_items,
                    None,
                    require_recently_closed=True,
                )

        self._initialize_supporting_searches(
            open_seed,
            enabled=include_supporting,
            inventory_complete=not closed_inventory_failed,
        )
        open_details = self._load_known_issues(
            open_seed,
            include_timeline=include_timeline,
        )
        if not include_supporting:
            references = {
                number: detail.references
                for number, detail in sorted(open_details.items())
                if detail.references
            }
            return InventoryResult(
                open_issues=[
                    self._finalize_issue(detail, [])
                    for _, detail in sorted(open_details.items())
                ],
                supporting_issues=[],
                evidence=self._finalize_evidence(
                    kept_issue_numbers=set(open_details),
                    references=references,
                    fetched_issue_summaries={},
                ),
                collection_errors=list(self._collection_errors),
                warnings=sorted(set(self._warnings)),
                references=references,
                open_pull_requests=[
                    copy.deepcopy(pull)
                    for _, pull in sorted(self._open_pull_requests.items())
                ],
                rejected_candidates=list(self._rejected_candidates.values()),
                open_bot_scan=copy.deepcopy(self._open_bot_scan),
            )

        recent_closed_candidates = self._load_candidate_issues(recent_closed_seed)
        traversal = _SupportingTraversalState(
            remaining=max(0, self._budgets["max_supporting_closed"]),
            probed_issue_numbers=set(),
            selected_candidates_by_root={number: set() for number in open_details},
            truncated_by_root={
                number: bool(self._supporting_searches[number]["truncated"])
                for number in open_details
            },
        )
        self._supporting_roots_by_issue = traversal.roots_by_issue
        self._supporting_dispositions_by_issue_and_root = traversal.dispositions_by_issue_and_root
        self._supporting_provenance_by_issue_root_disposition = (
            traversal.provenance_by_issue_root_disposition
        )
        self._supporting_budget_excluded_issue_numbers = traversal.budget_excluded_issue_numbers
        self._supporting_probe_failed_issue_numbers = traversal.failed_issue_numbers
        self._follow_explicit_references(
            open_details,
            recent_closed_seed,
            recent_closed_candidates,
            traversal,
            include_timeline=include_timeline,
        )

        marker_index, fact_index = self._build_candidate_indexes(recent_closed_candidates)
        self._match_recent_closed_candidates(
            open_details,
            marker_index,
            fact_index,
            recent_closed_candidates,
            recent_closed_seed,
            traversal,
            include_timeline=include_timeline,
        )
        self._append_supporting_budget_warnings(traversal)

        supporting_by_open = traversal.selected_candidates_by_root
        combined_support = dict(sorted(traversal.loaded_supporting_details.items()))

        open_issues = [self._finalize_issue(detail, sorted(supporting_by_open.get(number, set()))) for number, detail in sorted(open_details.items())]
        supporting_issues = [self._finalize_issue(detail, []) for number, detail in sorted(combined_support.items())]

        references = {
            number: detail.references
            for number, detail in sorted({**open_details, **combined_support}.items())
            if detail.references
        }

        final_evidence = self._finalize_evidence(
            kept_issue_numbers=set(open_details) | set(combined_support),
            references=references,
            fetched_issue_summaries=traversal.fetched_issue_summaries,
        )

        return InventoryResult(
            open_issues=open_issues,
            supporting_issues=supporting_issues,
            evidence=final_evidence,
            collection_errors=list(self._collection_errors),
            warnings=sorted(set(self._warnings)),
            references=references,
            open_pull_requests=[
                copy.deepcopy(pull)
                for _, pull in sorted(self._open_pull_requests.items())
            ],
            rejected_candidates=list(self._rejected_candidates.values()),
            open_bot_scan=copy.deepcopy(self._open_bot_scan),
        )

    def collect_incremental(
        self,
        previous_snapshot: dict[str, Any],
        current_history: dict[str, Any],
        *,
        include_supporting: bool = True,
        include_timeline: bool = True,
        full_refresh: bool = False,
    ) -> InventoryResult:
        from .refresh import plan_refresh, reconstruct_inventory

        open_seed = self._fetch_open_inventory()
        open_inventory = [
            {
                **copy.deepcopy(entry["issue"]),
                "labels": [{"name": label} for label in sorted(entry["labels"])],
            }
            for _, entry in sorted(open_seed.items())
        ]
        plan = plan_refresh(
            self._repository,
            open_inventory,
            previous_snapshot,
            current_history,
            full_refresh=full_refresh,
        )
        if (
            not full_refresh
            and not _requires_full_issue_recollection(
                plan,
                tuple(sorted(live_by_number for live_by_number in open_seed)),
            )
        ):
            return replace(
                reconstruct_inventory(
                    self._repository,
                    open_inventory,
                    previous_snapshot,
                    plan,
                ),
                open_pull_requests=[
                    copy.deepcopy(pull)
                    for _, pull in sorted(self._open_pull_requests.items())
                ],
                rejected_candidates=list(self._rejected_candidates.values()),
                open_bot_scan=copy.deepcopy(self._open_bot_scan),
            )

        inventory = self.collect(
            include_supporting=include_supporting,
            include_timeline=include_timeline,
            _open_seed=open_seed,
        )
        reused = set(plan.reuse)
        previous_evidence = previous_snapshot.get("evidence")
        if isinstance(previous_evidence, dict):
            for evidence_id in reused:
                record = previous_evidence.get(evidence_id)
                if isinstance(record, dict) and evidence_id in inventory.evidence:
                    inventory.evidence[evidence_id] = copy.deepcopy(record)
        return replace(inventory, refresh_plan=plan)

    def enrich_github_evidence(
        self,
        inventory: InventoryResult,
        *,
        include_issue_references: bool = True,
        minimal_run_evidence: bool = False,
        include_run_history: bool | None = None,
        include_retry_evidence: bool = False,
    ) -> InventoryResult:
        if include_retry_evidence and self._repository_policy is None:
            raise ValueError(
                "Retry evidence collection requires an explicit repository policy."
            )
        evidence = copy.deepcopy(inventory.evidence)
        collection_errors = list(inventory.collection_errors)
        warnings = list(inventory.warnings)
        references = copy.deepcopy(inventory.references)
        supporting_issues = copy.deepcopy(inventory.supporting_issues)
        refresh_plan = inventory.refresh_plan
        budget_deferred_refreshes: set[str] = set()
        refreshed_primary_pull_requests: set[str] = set()
        reusable_evidence_ids = (
            set(refresh_plan.reuse)
            if refresh_plan is not None
            else set()
        )
        supporting_issue_numbers = {
            int(issue["number"])
            for issue in inventory.supporting_issues
            if isinstance(issue, dict)
            and isinstance(issue.get("number"), int)
            and not isinstance(issue["number"], bool)
        }
        self._restore_supporting_roots_from_evidence(
            evidence,
            supporting_issue_numbers,
            {
                int(issue["number"])
                for issue in inventory.open_issues
                if isinstance(issue, dict)
                and isinstance(issue.get("number"), int)
                and not isinstance(issue["number"], bool)
            },
        )
        if refresh_plan is not None:
            self._recollect_supporting_comments(
                evidence,
                collection_errors,
                references,
                supporting_issues,
                supporting_issue_numbers,
                refresh_plan,
            )
        bounded_supporting_issue_numbers = set(supporting_issue_numbers)
        for issue in inventory.open_issues:
            if not isinstance(issue, dict):
                continue
            supporting_search = issue.get("supportingSearch")
            if not isinstance(supporting_search, dict):
                continue
            candidate_dispositions = supporting_search.get("candidateDispositions")
            if not isinstance(candidate_dispositions, list):
                continue
            for candidate_disposition in candidate_dispositions:
                if not isinstance(candidate_disposition, dict):
                    continue
                candidate_number = candidate_disposition.get("issueNumber")
                if isinstance(candidate_number, int) and not isinstance(candidate_number, bool):
                    bounded_supporting_issue_numbers.add(candidate_number)

        issue_targets: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
        pull_targets: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
        run_targets: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
        commit_targets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

        for pull_request in inventory.open_pull_requests:
            number = pull_request.get("number")
            if not isinstance(number, int) or isinstance(number, bool):
                continue
            pull_targets[(self._repository, number)].append(
                {
                    "sourceIssueNumber": number,
                    "sourceEvidenceId": f"pr:{number}",
                    "sourceUrl": pull_request.get("url"),
                    "targetType": "pull-request",
                    "targetRepository": self._repository,
                    "targetNumber": number,
                    "targetUrl": pull_request.get("url"),
                    "extractionMethod": "primary-inventory",
                }
            )

        for issue_refs in references.values():
            for ref in issue_refs:
                target_repository = ref.get("targetRepository")
                if not isinstance(target_repository, str) or not target_repository:
                    continue
                target_type = ref.get("targetType")
                if target_type == "issue" and include_issue_references:
                    target_number = ref.get("targetNumber")
                    if isinstance(target_number, int):
                        issue_targets[(target_repository, target_number)].append(ref)
                elif target_type == "pull-request":
                    target_number = ref.get("targetNumber")
                    if isinstance(target_number, int):
                        pull_targets[(target_repository, target_number)].append(ref)
                elif target_type == "workflow-run":
                    run_id = ref.get("runId")
                    if isinstance(run_id, int):
                        run_targets[(target_repository, run_id)].append(ref)
                elif target_type == "commit":
                    sha = ref.get("sha")
                    if isinstance(sha, str) and sha:
                        commit_targets[(target_repository, sha)].append(ref)

        if refresh_plan is not None:
            refreshable_ids = set(refresh_plan.refresh) | set(refresh_plan.retry)
            for target_number in sorted(supporting_issue_numbers):
                evidence_id = f"issue:{target_number}"
                if evidence_id not in refreshable_ids:
                    continue
                target_key = (self._repository, target_number)
                if target_key in issue_targets:
                    continue
                existing_record = evidence.get(evidence_id)
                existing_payload = (
                    existing_record.get("payload")
                    if isinstance(existing_record, dict)
                    else None
                )
                referenced_by = (
                    existing_payload.get("referencedBy")
                    if isinstance(existing_payload, dict)
                    else None
                )
                if not isinstance(referenced_by, list):
                    continue
                target_url = (
                    str(existing_record.get("url"))
                    if isinstance(existing_record, dict)
                    and isinstance(existing_record.get("url"), str)
                    else f"https://github.com/{self._repository}/issues/{target_number}"
                )
                issue_targets[target_key].extend(
                    {
                        **copy.deepcopy(reference),
                        "targetType": "issue",
                        "targetRepository": self._repository,
                        "targetNumber": target_number,
                        "targetUrl": target_url,
                    }
                    for reference in referenced_by
                    if isinstance(reference, dict)
                )

        for (target_repository, target_number), refs in sorted(issue_targets.items()):
            evidence_id = _repository_scoped_evidence_id(
                "issue", target_repository, self._repository, target_number
            )
            if evidence_id in reusable_evidence_ids:
                continue
            self._enrich_issue_reference(
                evidence,
                collection_errors,
                target_repository,
                target_number,
                refs,
                preserve_existing=(
                    _same_repository(target_repository, self._repository)
                    and target_number in bounded_supporting_issue_numbers
                    and (
                        refresh_plan is None
                        or evidence_id not in refreshable_ids
                    )
                ),
            )

        primary_pull_updated_at = {
            (self._repository, int(pull_request["number"])): str(
                pull_request.get("updatedAt") or ""
            )
            for pull_request in inventory.open_pull_requests
            if isinstance(pull_request, dict)
            and isinstance(pull_request.get("number"), int)
            and not isinstance(pull_request["number"], bool)
        }
        primary_pull_targets = [
            key
            for key, refs in sorted(
                pull_targets.items(),
                key=lambda item: (
                    primary_pull_updated_at.get(item[0], ""),
                    item[0][1],
                    item[0][0],
                ),
                reverse=True,
            )
            if _has_primary_inventory_reference(refs)
        ]
        max_primary_pull_requests = max(
            0, int(self._budgets["max_primary_pull_requests"])
        )
        selected_primary_pull_targets = set(
            primary_pull_targets[:max_primary_pull_requests]
        )
        if len(primary_pull_targets) > max_primary_pull_requests:
            warnings.append(
                "primary pull request current-state budget retained "
                f"{max_primary_pull_requests} of {len(primary_pull_targets)} "
                "selected pull requests; deferred pull requests remain in the "
                "inventory with incomplete current-state evidence."
            )

        for target_key, refs in sorted(pull_targets.items()):
            target_repository, target_number = target_key
            evidence_id = _repository_scoped_evidence_id(
                "pr", target_repository, self._repository, target_number
            )
            if evidence_id in reusable_evidence_ids:
                if (
                    not _has_primary_inventory_reference(refs)
                    or target_key not in selected_primary_pull_targets
                ):
                    continue
                refreshed_primary_pull_requests.add(evidence_id)
            self._enrich_pull_request_reference(
                evidence,
                collection_errors,
                target_repository,
                target_number,
                refs,
                include_primary_current_state=(
                    target_key in selected_primary_pull_targets
                ),
            )

        selected_run_targets = sorted(
            run_targets.items(),
            key=lambda item: (item[0][1], item[0][0]),
            reverse=True,
        )
        if minimal_run_evidence and len(selected_run_targets) > 10:
            warnings.append(
                f"minimal run evidence retained the 10 newest of {len(selected_run_targets)} referenced runs."
            )
            for (target_repository, run_id), refs in selected_run_targets[10:]:
                evidence_id = f"run:{run_id}"
                if evidence_id in reusable_evidence_ids:
                    continue
                existing_record = evidence.get(evidence_id)
                if (
                    refresh_plan is not None
                    and evidence_id in refresh_plan.refresh
                    and isinstance(existing_record, dict)
                    and existing_record.get("availability") == "available"
                ):
                    budget_deferred_refreshes.add(evidence_id)
                    continue
                record = self._make_evidence_record(
                    "workflow-run",
                    _reference_target_url(
                        refs,
                        f"https://github.com/{target_repository}/actions/runs/{run_id}",
                    ),
                    {
                        "runId": run_id,
                        "targetRepository": target_repository,
                        "runBudgetExcluded": True,
                        "referencedBy": _normalize_referenced_by(refs),
                    },
                )
                record["availability"] = "partial"
                evidence[evidence_id] = record
            selected_run_targets = selected_run_targets[:10]
        for (target_repository, run_id), refs in selected_run_targets:
            evidence_id = f"run:{run_id}"
            if evidence_id in reusable_evidence_ids:
                continue
            existing_record = evidence.get(evidence_id)
            if (
                refresh_plan is not None
                and evidence_id in refresh_plan.refresh
                and self._can_refresh_completed_run_history(existing_record)
                and (
                    not minimal_run_evidence
                    if include_run_history is None
                    else include_run_history
                )
            ):
                self._refresh_completed_run_history(
                    evidence,
                    collection_errors,
                    target_repository,
                    run_id,
                )
                continue
            self._enrich_workflow_run_reference(
                evidence,
                collection_errors,
                target_repository,
                run_id,
                refs,
                minimal=minimal_run_evidence,
                include_retry_evidence=include_retry_evidence,
                include_history=(
                    not minimal_run_evidence
                    if include_run_history is None
                    else include_run_history
                ),
            )

        for (target_repository, sha), refs in sorted(commit_targets.items()):
            evidence_id = _repository_scoped_evidence_id(
                "commit", target_repository, self._repository, sha
            )
            if evidence_id in reusable_evidence_ids:
                continue
            self._enrich_commit_reference(evidence, collection_errors, target_repository, sha, refs)

        for issue in supporting_issues:
            if not isinstance(issue, dict):
                continue
            number = issue.get("number")
            if not isinstance(number, int) or isinstance(number, bool):
                continue
            record = evidence.get(f"issue:{number}")
            payload = record.get("payload") if isinstance(record, dict) else None
            if not isinstance(payload, dict):
                continue
            for field in tuple(issue):
                if field in payload:
                    issue[field] = copy.deepcopy(payload[field])

        if refresh_plan is not None and budget_deferred_refreshes:
            refresh_plan = replace(
                refresh_plan,
                refresh=tuple(
                    set(refresh_plan.refresh) - budget_deferred_refreshes
                ),
                retry=tuple(set(refresh_plan.retry) | budget_deferred_refreshes),
            )

        if refresh_plan is not None and refreshed_primary_pull_requests:
            refresh_plan = replace(
                refresh_plan,
                reuse=tuple(
                    evidence_id
                    for evidence_id in refresh_plan.reuse
                    if evidence_id not in refreshed_primary_pull_requests
                ),
                refresh=(
                    *refresh_plan.refresh,
                    *sorted(refreshed_primary_pull_requests),
                ),
            )

        return InventoryResult(
            open_issues=copy.deepcopy(inventory.open_issues),
            supporting_issues=supporting_issues,
            evidence=dict(sorted(evidence.items())),
            collection_errors=collection_errors,
            warnings=warnings,
            references=references,
            refresh_plan=refresh_plan,
            open_pull_requests=copy.deepcopy(inventory.open_pull_requests),
            rejected_candidates=copy.deepcopy(inventory.rejected_candidates),
            open_bot_scan=copy.deepcopy(inventory.open_bot_scan),
        )

    def _restore_supporting_roots_from_evidence(
        self,
        evidence: dict[str, dict[str, Any]],
        supporting_issue_numbers: set[int],
        open_issue_numbers: set[int],
    ) -> None:
        for issue_number in supporting_issue_numbers:
            record = evidence.get(f"issue:{issue_number}")
            payload = record.get("payload") if isinstance(record, dict) else None
            referenced_by = payload.get("referencedBy") if isinstance(payload, dict) else None
            if not isinstance(referenced_by, list):
                continue
            roots = {
                source_issue_number
                for reference in referenced_by
                if isinstance(reference, dict)
                and isinstance(
                    source_issue_number := reference.get("sourceIssueNumber"),
                    int,
                )
                and not isinstance(source_issue_number, bool)
                and source_issue_number in open_issue_numbers
            }
            if roots:
                self._supporting_roots_by_issue.setdefault(issue_number, set()).update(roots)

    def _recollect_supporting_comments(
        self,
        evidence: dict[str, dict[str, Any]],
        collection_errors: list[CollectionError],
        references: dict[int, list[dict[str, Any]]],
        supporting_issues: list[dict[str, Any]],
        supporting_issue_numbers: set[int],
        refresh_plan: RefreshPlan,
    ) -> None:
        planned_ids = set(refresh_plan.refresh) | set(refresh_plan.retry)
        issue_numbers = {
            int(match.group("issue_number"))
            for evidence_id in planned_ids
            if (match := _ISSUE_COMMENT_EVIDENCE_ID_RE.fullmatch(evidence_id)) is not None
            and int(match.group("issue_number")) in supporting_issue_numbers
        }
        issue_numbers.update(
            issue_number
            for issue_number in supporting_issue_numbers
            if f"issue:{issue_number}" in planned_ids
        )
        supporting_by_number = {
            int(issue["number"]): issue
            for issue in supporting_issues
            if isinstance(issue, dict)
            and isinstance(issue.get("number"), int)
            and not isinstance(issue["number"], bool)
        }
        for issue_number in sorted(issue_numbers):
            endpoint = f"/repos/{self._repository}/issues/{issue_number}/comments"
            try:
                raw_comments = self._client.get_pages(endpoint)
            except Exception as exc:
                affected_issue_numbers = self._supporting_roots_by_issue.get(
                    issue_number,
                    set(),
                )
                collection_errors.append(
                    CollectionError(
                        "comments",
                        endpoint,
                        str(exc),
                        scope=_issue_error_scope(affected_issue_numbers),
                    )
                )
                comments: list[dict[str, Any]] = []
                comments_complete = False
            else:
                comments = _normalize_comments(raw_comments)
                comments_complete = True
            self._replace_issue_comments(
                issue_number,
                comments,
                comments_complete,
                evidence,
                references,
                supporting_by_number.get(issue_number),
            )

    def _replace_issue_comments(
        self,
        issue_number: int,
        comments: list[dict[str, Any]],
        comments_complete: bool,
        evidence: dict[str, dict[str, Any]],
        references: dict[int, list[dict[str, Any]]],
        supporting_issue: dict[str, Any] | None,
    ) -> None:
        comment_prefix = f"issue:{issue_number}:comment:"
        for evidence_id in tuple(evidence):
            if evidence_id.startswith(comment_prefix):
                evidence.pop(evidence_id)

        issue_evidence_id = f"issue:{issue_number}"
        issue_record = evidence.get(issue_evidence_id)
        issue_payload = (
            issue_record.get("payload")
            if isinstance(issue_record, dict)
            and isinstance(issue_record.get("payload"), dict)
            else {}
        )
        issue_url = str(
            issue_payload.get("url")
            or (supporting_issue or {}).get("url")
            or f"https://github.com/{self._repository}/issues/{issue_number}"
        )
        issue_title = str(
            issue_payload.get("title")
            or (supporting_issue or {}).get("title")
            or ""
        )
        issue_body = str(
            issue_payload.get("body")
            or (supporting_issue or {}).get("body")
            or ""
        )
        issue_text = _join_issue_text(issue_title, issue_body)
        issue_markers = self._extract_markers(issue_text, issue_evidence_id)
        issue_facts = self._extract_facts(issue_text, issue_evidence_id)
        issue_signals = extract_issue_signals(
            issue_number,
            issue_evidence_id,
            issue_url,
            issue_body,
            self._repository,
        )

        comment_payloads: list[
            tuple[
                str,
                dict[str, Any],
                list[dict[str, Any]],
                list[dict[str, Any]],
                list[dict[str, Any]],
            ]
        ] = []
        for comment in comments:
            evidence_id = f"{comment_prefix}{comment['id']}"
            markers = self._extract_markers(comment["body"], evidence_id)
            facts = self._extract_facts(comment["body"], evidence_id)
            comment_references = self._extract_references(
                issue_number,
                comment["body"],
                evidence_id,
                comment["url"],
            )
            comment_payloads.append(
                (evidence_id, comment, markers, facts, comment_references)
            )

        all_references, excluded_references = self._select_references(
            issue_number,
            _sorted_unique_records(
                [dict(reference) for reference in issue_signals.references]
                + [
                    reference
                    for _, _, _, _, comment_references in comment_payloads
                    for reference in comment_references
                ],
                (
                    "sourceEvidenceId",
                    "targetType",
                    "targetRepository",
                    "targetNumber",
                    "runId",
                    "sha",
                ),
            ),
            issue_signals.occurrences,
        )
        references_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for reference in all_references:
            references_by_source[reference["sourceEvidenceId"]].append(reference)
        excluded_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for reference in excluded_references:
            excluded_by_source[reference["sourceEvidenceId"]].append(reference)

        for evidence_id, comment, markers, facts, _ in comment_payloads:
            evidence[evidence_id] = self._make_evidence_record(
                "issue-comment",
                comment["url"],
                {
                    **comment,
                    "sourceIssueNumber": issue_number,
                    "markers": markers,
                    "facts": facts,
                    "references": references_by_source.get(evidence_id, []),
                    **(
                        {"excludedReferences": excluded_by_source[evidence_id]}
                        if evidence_id in excluded_by_source
                        else {}
                    ),
                },
            )

        all_markers = _sorted_unique_records(
            issue_markers
            + [marker for _, _, markers, _, _ in comment_payloads for marker in markers],
            ("key", "normalized", "method", "sourceEvidenceId"),
        )
        all_facts = _sorted_unique_records(
            issue_facts
            + [fact for _, _, _, facts, _ in comment_payloads for fact in facts],
            ("field", "normalized", "method", "sourceEvidenceId"),
        )
        if isinstance(issue_record, dict):
            updated_payload = copy.deepcopy(issue_payload)
            lifecycle_metadata = _issue_lifecycle_metadata(
                title=str(updated_payload.get("title") or ""),
                labels=updated_payload.get("labels"),
                body=issue_body,
                body_markers=issue_markers,
                issue_signals=issue_signals,
                comments=comment_payloads,
                comments_complete=comments_complete,
                episodes_complete=updated_payload.get("episodesComplete") is True,
            )
            updated_payload.update(
                {
                    "comments": copy.deepcopy(comments),
                    "markers": all_markers,
                    "facts": all_facts,
                    "occurrences": [
                        occurrence.as_record()
                        for occurrence in issue_signals.occurrences
                    ],
                    "references": all_references,
                    **lifecycle_metadata,
                }
            )
            if excluded_references:
                updated_payload["excludedReferences"] = excluded_references
            else:
                updated_payload.pop("excludedReferences", None)
            issue_record["payload"] = updated_payload
        if supporting_issue is not None:
            supporting_issue["comments"] = copy.deepcopy(comments)
            supporting_issue["markers"] = copy.deepcopy(all_markers)
            supporting_issue["facts"] = copy.deepcopy(all_facts)
            supporting_issue["occurrences"] = [
                occurrence.as_record()
                for occurrence in issue_signals.occurrences
            ]
        if all_references:
            references[issue_number] = copy.deepcopy(all_references)
        else:
            references.pop(issue_number, None)

    def enrich_ownership_evidence(
        self,
        inventory: InventoryResult,
        *,
        checkout_path: str | None = None,
        git_runner: Any = subprocess.run,
        git_timeout_seconds: int = 10,
    ) -> InventoryResult:
        evidence = copy.deepcopy(inventory.evidence)
        collection_errors = list(inventory.collection_errors)
        affected_paths = ownership.collect_affected_paths(evidence, target_repository=self._repository)
        path_referenced_by = ownership.collect_path_referenced_by(
            evidence,
            target_repository=self._repository,
        )
        refresh_plan = inventory.refresh_plan
        if refresh_plan is not None and not refresh_plan.new_issues and not refresh_plan.changed_issues:
            ownership_evidence_ids = {
                evidence_id
                for evidence_id, record in evidence.items()
                if isinstance(record, dict)
                and record.get("kind") in {"source-path", "codeowners"}
            }
            if not affected_paths or ownership_evidence_ids <= set(refresh_plan.reuse):
                return InventoryResult(
                    open_issues=copy.deepcopy(inventory.open_issues),
                    supporting_issues=copy.deepcopy(inventory.supporting_issues),
                    evidence=dict(sorted(evidence.items())),
                    collection_errors=collection_errors,
                    warnings=list(inventory.warnings),
                    references=copy.deepcopy(inventory.references),
                    refresh_plan=refresh_plan,
                    open_pull_requests=copy.deepcopy(
                        inventory.open_pull_requests
                    ),
                    rejected_candidates=copy.deepcopy(
                        inventory.rejected_candidates
                    ),
                    open_bot_scan=copy.deepcopy(inventory.open_bot_scan),
                )

        codeowners_document: ownership.CodeownersDocument | None = None
        checkout_info: ownership.CheckoutInfo | None = None

        if checkout_path is not None:
            try:
                checkout_info = ownership.validate_checkout(
                    Path(checkout_path),
                    self._repository,
                    git_runner=git_runner,
                    timeout_seconds=git_timeout_seconds,
                )
            except ownership.OwnershipError as exc:
                collection_errors.append(CollectionError(exc.stage, exc.endpoint, str(exc)))
                return InventoryResult(
                    open_issues=copy.deepcopy(inventory.open_issues),
                    supporting_issues=copy.deepcopy(inventory.supporting_issues),
                    evidence=dict(sorted(evidence.items())),
                    collection_errors=collection_errors,
                    warnings=list(inventory.warnings),
                    references=copy.deepcopy(inventory.references),
                    refresh_plan=refresh_plan,
                    open_pull_requests=copy.deepcopy(
                        inventory.open_pull_requests
                    ),
                    rejected_candidates=copy.deepcopy(
                        inventory.rejected_candidates
                    ),
                    open_bot_scan=copy.deepcopy(inventory.open_bot_scan),
                )

            try:
                codeowners_document = ownership.load_codeowners_from_checkout(Path(checkout_path), checkout_info)
            except ownership.OwnershipError as exc:
                collection_errors.append(CollectionError(exc.stage, exc.endpoint, str(exc)))

            for affected_path in affected_paths:
                evidence_id = f"source:{quote(affected_path, safe='')}"
                fallback_payload = {
                    "path": affected_path,
                    "targetRepository": self._repository,
                    "checkoutCommit": checkout_info.commit,
                    "sourceUrl": ownership.build_blob_url(self._repository, checkout_info.commit, affected_path),
                    "recentCommits": [],
                    "referencedBy": copy.deepcopy(path_referenced_by.get(affected_path, [])),
                }
                try:
                    payload = ownership.load_source_history(
                        Path(checkout_path),
                        affected_path,
                        checkout_info,
                        git_runner=git_runner,
                        timeout_seconds=git_timeout_seconds,
                    )
                except ownership.OwnershipError as exc:
                    collection_errors.append(CollectionError(exc.stage, exc.endpoint, str(exc)))
                    evidence[evidence_id] = self._make_partial_record(
                        "source-path",
                        fallback_payload["sourceUrl"],
                        fallback_payload,
                        exc,
                    )
                    continue
                payload["referencedBy"] = copy.deepcopy(path_referenced_by.get(affected_path, []))
                evidence[evidence_id] = self._make_evidence_record("source-path", payload["sourceUrl"], payload)
        else:
            try:
                codeowners_document = ownership.load_codeowners_from_api(self._client, self._repository)
            except ownership.OwnershipError as exc:
                collection_errors.append(CollectionError(exc.stage, exc.endpoint, str(exc)))

        if codeowners_document is not None:
            for affected_path in affected_paths:
                match = ownership.match_codeowners(affected_path, codeowners_document.rules)
                if match is None:
                    continue
                evidence_id = f"codeowners:{quote(affected_path, safe='')}:{match.line_number}"
                payload = {
                    "path": affected_path,
                    "owners": list(match.owners),
                    "pattern": match.pattern,
                    "line": match.line_number,
                    "sourcePath": codeowners_document.source_path,
                    "sourceUrl": codeowners_document.source_url,
                    "checkoutCommit": codeowners_document.checkout_commit,
                    "referencedBy": copy.deepcopy(path_referenced_by.get(affected_path, [])),
                }
                evidence[evidence_id] = self._make_evidence_record("codeowners", codeowners_document.source_url, payload)

        return InventoryResult(
            open_issues=copy.deepcopy(inventory.open_issues),
            supporting_issues=copy.deepcopy(inventory.supporting_issues),
            evidence=dict(sorted(evidence.items())),
            collection_errors=collection_errors,
            warnings=list(inventory.warnings),
            references=copy.deepcopy(inventory.references),
            refresh_plan=refresh_plan,
            open_pull_requests=copy.deepcopy(inventory.open_pull_requests),
            rejected_candidates=copy.deepcopy(inventory.rejected_candidates),
            open_bot_scan=copy.deepcopy(inventory.open_bot_scan),
        )

    def _fetch_open_inventory(self) -> dict[int, dict[str, Any]]:
        self._open_pull_requests.clear()
        self._rejected_candidates.clear()
        self._open_bot_scan = None
        open_seed: dict[int, dict[str, Any]] = {}
        for label in TARGET_LABELS:
            endpoint = self._issue_query_endpoint("open", label)
            try:
                items = self._client.get_pages(endpoint)
            except Exception as exc:  # pragma: no cover - exercised through tests
                raise InventoryError(f"Failed open issue query for {label}: {endpoint}: {exc}") from exc
            self._merge_issue_inventory(open_seed, items, label)
        for author in self._bot_authors:
            endpoint = self._bot_issue_query_endpoint("open", author)
            try:
                items = self._client.get_pages(endpoint)
            except Exception as exc:  # pragma: no cover - exercised through tests
                raise InventoryError(
                    f"Failed open issue query for author {author}: {endpoint}: {exc}"
                ) from exc
            self._merge_issue_inventory(open_seed, items, None)
        self._merge_bot_authored_open_inventory(open_seed)
        return open_seed

    def _merge_bot_authored_open_inventory(
        self, open_seed: dict[int, dict[str, Any]]
    ) -> None:
        """Adopt every open bot-authored issue and pull request.

        The label and `creator=` queries above only find the bot logins that
        were configured up front, so any other app that opens issues stays
        invisible. GitHub's search API cannot close that gap: an author
        wildcard such as `author:app/*` is rejected with HTTP 422
        ("The listed users cannot be searched"), and `creator=` accepts exactly
        one login per request. Paging the full open list and filtering on
        `user.type == "Bot"` is therefore the only complete option.

        The scan is deliberately bounded twice -- by pages and by adopted items
        -- and reports which bound it hit, because an inventory that quietly
        drops bot-authored work would make the whole cycle look clean when it
        merely stopped looking.
        """
        max_pages = max(0, int(self._budgets["max_open_scan_pages"]))
        max_items = max(0, int(self._budgets["max_bot_authored_open"]))
        bot_items: list[dict[str, Any]] = []
        scanned_pages = 0
        reached_end = False
        status = "complete"
        detail: str | None = None

        for page in range(1, max_pages + 1):
            endpoint = self._open_scan_endpoint(page)
            try:
                payload = self._client.get(endpoint)
            except Exception as exc:
                status = "failed"
                detail = str(exc)
                self._collection_errors.append(
                    CollectionError("open-bot-scan", endpoint, str(exc))
                )
                break
            if not isinstance(payload, list):
                status = "failed"
                detail = "Unexpected open issue list payload shape"
                self._collection_errors.append(
                    CollectionError("open-bot-scan", endpoint, detail)
                )
                break
            scanned_pages += 1
            bot_items.extend(
                raw_issue
                for raw_issue in payload
                if isinstance(raw_issue, dict) and _is_bot_authored(raw_issue)
            )
            if len(payload) < OPEN_SCAN_PAGE_SIZE:
                reached_end = True
                break

        if status == "complete" and not reached_end:
            # Either the page budget ran out mid-list, or it was zero. Both mean
            # unscanned open items remain.
            status = "truncated"
            detail = f"open scan stopped after the {max_pages} page budget"

        found = len(bot_items)
        if found > max_items:
            bot_items = bot_items[:max_items]
            if status == "complete":
                status = "truncated"
                detail = (
                    f"kept the {max_items} most recently updated of {found} "
                    "bot-authored open items"
                )

        self._merge_issue_inventory(
            open_seed, bot_items, None, selection_reason="bot-author"
        )
        self._open_bot_scan = {
            "status": status,
            "complete": status == "complete",
            "scannedPages": scanned_pages,
            "pageBudget": max_pages,
            "itemBudget": max_items,
            "botAuthoredFound": found,
            "botAuthoredAdopted": len(bot_items),
            "detail": detail,
        }
        if status != "complete":
            self._warnings.append(
                "open bot-authored inventory is incomplete "
                f"({status}): {detail}"
            )

    def _open_scan_endpoint(self, page: int) -> str:
        # Sorted by recency so that hitting the item budget deterministically
        # keeps the freshest work rather than an arbitrary slice.
        return (
            f"/repos/{self._repository}/issues?state=open"
            f"&sort=updated&direction=desc"
            f"&per_page={OPEN_SCAN_PAGE_SIZE}&page={page}"
        )

    def _issue_query_endpoint(self, state: str, label: str) -> str:
        encoded_label = quote(label, safe="")
        if state == "open":
            return f"/repos/{self._repository}/issues?state=open&labels={encoded_label}&per_page=100"
        return (
            f"/repos/{self._repository}/issues?state=closed&labels={encoded_label}"
            f"&since={self._cutoff_text}&per_page=100"
        )

    def _bot_issue_query_endpoint(self, state: str, author: str) -> str:
        encoded_author = quote(author, safe="")
        if state == "open":
            return (
                f"/repos/{self._repository}/issues?state=open"
                f"&creator={encoded_author}&per_page=100"
            )
        return (
            f"/repos/{self._repository}/issues?state=closed"
            f"&creator={encoded_author}&since={self._cutoff_text}&per_page=100"
        )

    def _merge_issue_inventory(
        self,
        destination: dict[int, dict[str, Any]],
        items: object,
        label: str | None,
        *,
        require_recently_closed: bool = False,
        selection_reason: str | None = None,
    ) -> None:
        if not isinstance(items, list):
            return

        for raw_issue in items:
            if not isinstance(raw_issue, dict):
                continue
            number = raw_issue.get("number")
            if not isinstance(number, int):
                continue
            is_pull_request = bool(raw_issue.get("pull_request"))
            if self._is_assigned_to_copilot(raw_issue):
                target_kind = "pull-request" if is_pull_request else "issue"
                self._rejected_candidates[(target_kind, number)] = {
                    "number": number,
                    "targetKind": target_kind,
                    "reason": "assigned-to-copilot",
                }
                continue
            if is_pull_request:
                if (
                    not require_recently_closed
                    and raw_issue.get("state") == "open"
                ):
                    self._merge_pull_request_inventory(
                        raw_issue, label, selection_reason=selection_reason
                    )
                continue
            if require_recently_closed and not self._is_recently_closed(raw_issue):
                continue

            merged_labels = set(_extract_labels(raw_issue))
            if label is not None:
                merged_labels.add(label)
            existing = destination.get(number)
            if existing is not None:
                merged_labels.update(existing["labels"])
            destination[number] = {
                "issue": dict(raw_issue),
                "labels": merged_labels,
            }

    @staticmethod
    def _is_assigned_to_copilot(raw_issue: dict[str, Any]) -> bool:
        assignees = raw_issue.get("assignees")
        if not isinstance(assignees, list):
            return False
        return any(
            isinstance(assignee, dict)
            and isinstance(assignee.get("login"), str)
            and assignee["login"].casefold() in COPILOT_ASSIGNEES
            for assignee in assignees
        )

    def _merge_pull_request_inventory(
        self,
        raw_pull_request: dict[str, Any],
        label: str | None,
        *,
        selection_reason: str | None = None,
    ) -> None:
        number = int(raw_pull_request["number"])
        existing = self._open_pull_requests.get(number)
        labels = set(_extract_labels(raw_pull_request))
        if label is not None:
            labels.add(label)
        if existing is not None:
            labels.update(existing.get("labels", []))
        selection_reasons = set(
            existing.get("selectionReasons", []) if existing is not None else []
        )
        if label is not None:
            selection_reasons.add(f"label:{label}")
        else:
            selection_reasons.add(selection_reason or "automation-author")
        self._open_pull_requests[number] = {
            "number": number,
            "state": raw_pull_request.get("state"),
            "title": raw_pull_request.get("title"),
            "body": raw_pull_request.get("body"),
            "url": raw_pull_request.get("html_url"),
            "createdAt": raw_pull_request.get("created_at"),
            "updatedAt": raw_pull_request.get("updated_at"),
            "labels": sorted(labels),
            "author": _nested_text(raw_pull_request, ("user", "login")),
            "assignees": sorted(
                {
                    str(assignee["login"])
                    for assignee in raw_pull_request.get("assignees", [])
                    if isinstance(assignee, dict)
                    and isinstance(assignee.get("login"), str)
                }
            ),
            "selectionReasons": sorted(selection_reasons),
        }

    def _is_recently_closed(self, raw_issue: dict[str, Any]) -> bool:
        closed_at = raw_issue.get("closed_at")
        if not isinstance(closed_at, str) or not closed_at:
            return False
        try:
            return _parse_timestamp(closed_at) >= self._cutoff
        except ValueError:
            self._warnings.append(f"ignored issue {raw_issue.get('number')} with invalid closed_at {closed_at!r}")
            return False

    def _load_known_issues(
        self,
        seed: dict[int, dict[str, Any]],
        *,
        include_timeline: bool,
    ) -> dict[int, _NormalizedIssue]:
        loaded: dict[int, _NormalizedIssue] = {}
        for number, entry in sorted(seed.items()):
            loaded[number] = self._load_issue_detail(
                entry["issue"],
                sorted(entry["labels"]),
                include_timeline=include_timeline,
            )
        return loaded

    def _load_candidate_issues(self, seed: dict[int, dict[str, Any]]) -> dict[int, _CandidateIssue]:
        loaded: dict[int, _CandidateIssue] = {}
        for number, entry in sorted(seed.items()):
            loaded[number] = self._load_candidate_issue(entry["issue"], sorted(entry["labels"]))
        return loaded

    def _load_candidate_issue(self, raw_issue: dict[str, Any], labels: list[str]) -> _CandidateIssue:
        normalized_issue = self._normalize_issue(raw_issue, labels)
        issue_text = _join_issue_text(normalized_issue["title"], normalized_issue["body"])
        markers = self._extract_markers(issue_text, f"issue:{normalized_issue['number']}")
        facts = self._extract_facts(issue_text, f"issue:{normalized_issue['number']}")
        return _CandidateIssue(issue=normalized_issue, markers=markers, facts=facts)

    def _load_issue_summary(self, raw_issue: dict[str, Any], labels: list[str]) -> _IssueSummary:
        normalized_issue = self._normalize_issue(raw_issue, labels)
        number = int(normalized_issue["number"])
        issue_evidence_id = f"issue:{number}"
        signals = extract_issue_signals(
            number,
            issue_evidence_id,
            normalized_issue["url"],
            normalized_issue["body"],
            self._repository,
        )
        references, excluded_references = self._select_references(
            number,
            [dict(reference) for reference in signals.references],
            signals.occurrences,
        )
        return _IssueSummary(
            raw_issue=dict(raw_issue),
            labels=list(labels),
            issue=normalized_issue,
            references=references,
            excluded_references=excluded_references,
        )

    def _load_issue_detail(
        self,
        raw_issue: dict[str, Any],
        labels: list[str],
        *,
        include_timeline: bool = True,
    ) -> _NormalizedIssue:
        normalized_issue = self._normalize_issue(raw_issue, labels)
        number = int(normalized_issue["number"])
        issue_url = normalized_issue["url"]
        issue_body = normalized_issue["body"]
        issue_text = _join_issue_text(normalized_issue["title"], issue_body)

        comments = self._load_comments(number)
        comments_complete = not any(
            error.stage == "comments"
            and error.endpoint == f"/repos/{self._repository}/issues/{number}/comments"
            for error in self._collection_errors
        )
        timeline_events = self._load_timeline(number) if include_timeline else []
        episodes_complete = include_timeline and not any(
            error.stage == "timeline"
            and error.endpoint == f"/repos/{self._repository}/issues/{number}/timeline"
            for error in self._collection_errors
        )
        episodes = self.normalize_timeline(
            number,
            normalized_issue["state"],
            normalized_issue["createdAt"],
            timeline_events,
            normalized_issue["closedAt"],
        )
        normalized_issue["episodes"] = episodes
        normalized_issue["episodesComplete"] = episodes_complete

        issue_evidence_id = f"issue:{number}"
        markers = self._extract_markers(issue_text, issue_evidence_id)
        facts = self._extract_facts(issue_text, issue_evidence_id)
        issue_signals = extract_issue_signals(
            number,
            issue_evidence_id,
            issue_url,
            issue_body,
            self._repository,
        )
        refs = [dict(reference) for reference in issue_signals.references]
        occurrences = [
            occurrence.as_record()
            for occurrence in issue_signals.occurrences
        ]

        comment_markers: list[dict[str, Any]] = []
        comment_facts: list[dict[str, Any]] = []
        comment_refs: list[dict[str, Any]] = []
        comment_evidence_payloads: list[
            tuple[
                str,
                dict[str, Any],
                list[dict[str, Any]],
                list[dict[str, Any]],
                dict[str, object] | None,
            ]
        ] = []
        for comment in comments:
            evidence_id = f"issue:{number}:comment:{comment['id']}"
            (
                shepherd_status,
                extracted_markers,
                extracted_facts,
                extracted_refs,
            ) = self._extract_comment_payload(number, comment, evidence_id)
            comment_markers.extend(extracted_markers)
            comment_facts.extend(extracted_facts)
            comment_refs.extend(extracted_refs)
            comment_evidence_payloads.append(
                (
                    evidence_id,
                    comment,
                    extracted_markers,
                    extracted_facts,
                    shepherd_status,
                )
            )

        all_markers = _sorted_unique_records(
            markers
            + [
                marker
                for entry in comment_evidence_payloads
                for marker in entry[2]
            ],
            ("key", "normalized", "method", "sourceEvidenceId"),
        )
        all_facts = _sorted_unique_records(
            facts
            + [
                fact
                for entry in comment_evidence_payloads
                for fact in entry[3]
            ],
            ("field", "normalized", "method", "sourceEvidenceId"),
        )
        all_refs, excluded_refs = self._select_references(
            number,
            _sorted_unique_records(refs + comment_refs, ("sourceEvidenceId", "targetType", "targetRepository", "targetNumber", "runId", "sha")),
            issue_signals.occurrences,
        )
        refs_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for ref in all_refs:
            refs_by_source[ref["sourceEvidenceId"]].append(ref)
        excluded_refs_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for ref in excluded_refs:
            excluded_refs_by_source[ref["sourceEvidenceId"]].append(ref)

        for (
            evidence_id,
            comment,
            extracted_markers,
            extracted_facts,
            shepherd_status,
        ) in comment_evidence_payloads:
            self._evidence[evidence_id] = self._make_evidence_record(
                "issue-comment",
                comment["url"],
                {
                    **comment,
                    "sourceIssueNumber": number,
                    "markers": extracted_markers,
                    "facts": extracted_facts,
                    "references": refs_by_source.get(evidence_id, []),
                    **(
                        {"shepherdStatus": shepherd_status}
                        if shepherd_status is not None
                        else {}
                    ),
                    **(
                        {"excludedReferences": excluded_refs_by_source[evidence_id]}
                        if evidence_id in excluded_refs_by_source
                        else {}
                    ),
                },
            )

        normalized_issue["comments"] = [
            {
                **comment,
                **({"body": ""} if shepherd_status is not None else {}),
            }
            for _, comment, _, _, shepherd_status in comment_evidence_payloads
        ]
        normalized_issue["markers"] = all_markers
        normalized_issue["facts"] = all_facts
        normalized_issue["occurrences"] = occurrences
        normalized_issue.update(
            _issue_lifecycle_metadata(
                title=normalized_issue["title"],
                labels=labels,
                body=issue_body,
                body_markers=markers,
                issue_signals=issue_signals,
                comments=comment_evidence_payloads,
                comments_complete=comments_complete,
                episodes_complete=episodes_complete,
            )
        )

        self._evidence[issue_evidence_id] = self._make_evidence_record(
            "issue-event",
            issue_url,
            {
                **normalized_issue,
                "markers": markers,
                "facts": facts,
                "occurrences": occurrences,
                "references": all_refs,
                **({"excludedReferences": excluded_refs} if excluded_refs else {}),
            },
        )

        for event in timeline_events:
            event_id = event.get("id")
            if not isinstance(event_id, int):
                continue
            evidence_id = f"issue:{number}:event:{event_id}"
            self._evidence[evidence_id] = self._make_evidence_record(
                "issue-event",
                issue_url,
                {
                    "id": event_id,
                    "sourceIssueNumber": number,
                    "event": _text(event, "event"),
                    "createdAt": _text(event, "created_at"),
                    "actor": _nested_text(event, ("actor", "login")),
                },
            )

        return _NormalizedIssue(
            issue=normalized_issue,
            references=all_refs,
            markers=all_markers,
            facts=all_facts,
            occurrences=occurrences,
        )

    def _load_comments(self, issue_number: int) -> list[dict[str, Any]]:
        endpoint = f"/repos/{self._repository}/issues/{issue_number}/comments"
        try:
            raw_comments = self._client.get_pages(endpoint)
        except Exception as exc:
            supporting_roots = self._supporting_roots_by_issue.get(issue_number, set())
            affected_issue_numbers = supporting_roots or {issue_number}
            self._collection_errors.append(
                CollectionError(
                    "comments",
                    endpoint,
                    str(exc),
                    scope=_issue_error_scope(affected_issue_numbers),
                )
            )
            self._mark_supporting_search_incomplete(issue_number)
            if supporting_roots:
                self._supporting_comment_failed_issue_numbers.add(issue_number)
            for root_issue_number in supporting_roots:
                self._mark_supporting_search_incomplete(root_issue_number)
            return []

        return _normalize_comments(raw_comments)

    def _load_timeline(self, issue_number: int) -> list[dict[str, Any]]:
        endpoint = f"/repos/{self._repository}/issues/{issue_number}/timeline"
        try:
            raw_events = self._client.get_pages(endpoint)
        except Exception as exc:
            supporting_roots = self._supporting_roots_by_issue.get(issue_number, set())
            affected_issue_numbers = supporting_roots or {issue_number}
            self._collection_errors.append(
                CollectionError(
                    "timeline",
                    endpoint,
                    str(exc),
                    scope=_issue_error_scope(affected_issue_numbers),
                )
            )
            return []

        events: list[dict[str, Any]] = []
        if isinstance(raw_events, list):
            for raw_event in raw_events:
                if not isinstance(raw_event, dict):
                    continue
                event_name = raw_event.get("event")
                if event_name not in {"closed", "reopened"}:
                    continue
                event_id = raw_event.get("id")
                if not isinstance(event_id, int):
                    self._warnings.append(f"issue {issue_number} has {event_name} event without integer id")
                    continue
                created_at = raw_event.get("created_at")
                if not isinstance(created_at, str) or not created_at:
                    self._warnings.append(f"issue {issue_number} has {event_name} event {event_id} without created_at")
                    continue
                events.append(dict(raw_event))
        events.sort(key=lambda item: (_text(item, "created_at"), int(item.get("id", 0))))
        return events

    def normalize_timeline(
        self,
        issue_number: int,
        issue_state: str,
        issue_created_at: str,
        events: list[dict[str, Any]],
        issue_closed_at: str | None,
    ) -> list[dict[str, Any]]:
        episodes: list[dict[str, Any]] = []
        opened_at = issue_created_at
        is_open = True
        valid_closed_at: str | None = None
        if isinstance(issue_closed_at, str) and issue_closed_at:
            try:
                _parse_timestamp(issue_closed_at)
            except ValueError:
                valid_closed_at = None
            else:
                valid_closed_at = issue_closed_at

        for event in events:
            event_name = _text(event, "event")
            created_at = _text(event, "created_at")
            event_id = int(event["id"])
            if event_name == "closed":
                if not is_open:
                    self._warnings.append(f"issue {issue_number} duplicate close event {event_id}")
                    continue
                episodes.append({"openedAt": opened_at, "closedAt": created_at})
                opened_at = ""
                is_open = False
                continue

            if is_open:
                self._warnings.append(f"issue {issue_number} duplicate reopen event {event_id}")
                continue

            opened_at = created_at
            is_open = True

        if is_open:
            episodes.append({"openedAt": opened_at, "closedAt": None})

        if issue_state == "closed" and episodes and episodes[-1]["closedAt"] is None:
            if valid_closed_at is not None:
                # GitHub's timeline can omit the final close event; the issue's own
                # closed_at timestamp is the authoritative close time in that case.
                episodes[-1]["closedAt"] = valid_closed_at
                self._warnings.append(
                    f"issue {issue_number} missing-close-event warning; using issue.closed_at {valid_closed_at}"
                )
            else:
                self._warnings.append(
                    f"issue {issue_number} missing-close-event warning; issue.closed_at missing or invalid"
                )
        return episodes

    def _owned_shepherd_status(
        self,
        comment: dict[str, Any],
        markers: list[dict[str, Any]],
    ) -> dict[str, object] | None:
        if (
            self._shepherd_author is None
            or str(comment.get("author", "")).casefold() != self._shepherd_author
            or not str(comment.get("body", "")).startswith("[automated] ")
        ):
            return None

        marker_values = {
            str(marker.get("key")): str(marker.get("normalized"))
            for marker in markers
        }
        if marker_values.get("role") != "status":
            return None
        idempotency_key = marker_values.get("idempotency-key")
        if not idempotency_key:
            return None
        return {
            "role": "status",
            "idempotencyKey": idempotency_key,
            "owned": True,
        }

    def _extract_comment_payload(
        self,
        issue_number: int,
        comment: dict[str, Any],
        evidence_id: str,
    ) -> tuple[
        dict[str, object] | None,
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        markers = self._extract_markers(comment["body"], evidence_id)
        shepherd_status = self._owned_shepherd_status(comment, markers)
        if shepherd_status is not None:
            return shepherd_status, [], [], []
        return (
            None,
            markers,
            self._extract_facts(comment["body"], evidence_id),
            self._extract_references(
                issue_number,
                comment["body"],
                evidence_id,
                comment["url"],
            ),
        )

    def _extract_markers(self, text: str, source_evidence_id: str) -> list[dict[str, Any]]:
        markers = [
            dict(marker)
            for marker in extract_issue_signals(
                0,
                source_evidence_id,
                "",
                text,
                self._repository,
            ).markers
        ]
        # Old collector snapshots used this namespaced marker shape. Keep it at
        # the compatibility boundary; the focused parser accepts only current
        # Aspire marker names.
        for match in _LEGACY_HTML_MARKER_RE.finditer(text):
            raw = match.group("value").strip()
            markers.append(
                {
                    "key": match.group("key"),
                    "raw": raw,
                    "normalized": " ".join(raw.split()).lower(),
                    "method": "html-comment",
                    "sourceEvidenceId": source_evidence_id,
                }
            )
        return _sorted_unique_records(
            markers,
            ("key", "normalized", "method", "sourceEvidenceId"),
        )

    def _extract_facts(self, text: str, source_evidence_id: str) -> list[dict[str, Any]]:
        return [
            dict(fact)
            for fact in extract_issue_signals(
                0,
                source_evidence_id,
                "",
                text,
                self._repository,
            ).facts
        ]

    def _extract_references(
        self,
        issue_number: int,
        text: str,
        source_evidence_id: str,
        source_url: str,
        *,
        local_repository: str | None = None,
    ) -> list[dict[str, Any]]:
        reference_repository = local_repository or self._repository
        return [
            dict(reference)
            for reference in extract_issue_signals(
                issue_number,
                source_evidence_id,
                source_url,
                text,
                reference_repository,
            ).references
        ]

    def _select_references(
        self,
        issue_number: int,
        refs: list[dict[str, Any]],
        occurrences: tuple[Occurrence, ...],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        selection = select_references(
            tuple(refs),
            occurrences,
            max_run_refs_per_issue=self._budgets["max_run_refs_per_issue"],
            max_issue_refs_per_issue=self._budgets["max_issue_refs_per_issue"],
            max_commit_refs_per_issue=self._budgets["max_commit_refs_per_issue"],
        )
        kept = [dict(reference) for reference in selection.selected]
        excluded = [dict(reference) for reference in selection.excluded]
        if not excluded:
            return kept, excluded
        self._reference_truncated_issues.add(issue_number)
        self._mark_supporting_search_incomplete(issue_number, truncated=True)
        excluded_by_reason: dict[str, int] = defaultdict(int)
        for reference in excluded:
            excluded_by_reason[str(reference["exclusionReason"])] += 1
        for reason, count in sorted(excluded_by_reason.items()):
            self._warnings.append(
                f"{reason} budget excluded {count} reference(s) for issue {issue_number}."
            )
        return kept, excluded

    def _follow_explicit_references(
        self,
        open_details: dict[int, _NormalizedIssue],
        recent_closed_seed: dict[int, dict[str, Any]],
        recent_closed_candidates: dict[int, _CandidateIssue],
        traversal: _SupportingTraversalState,
        *,
        include_timeline: bool,
    ) -> None:
        initial_entries: list[_SupportingQueueEntry] = []
        for open_number, detail in sorted(open_details.items()):
            for ref in detail.references:
                if ref["targetType"] == "issue" and _same_repository(ref["targetRepository"], self._repository):
                    initial_entries.append(
                        _SupportingQueueEntry(
                            candidate_issue_number=int(ref["targetNumber"]),
                            root_open_issue_numbers=frozenset({open_number}),
                            depth=1,
                            source_association=ref,
                            explicit=True,
                        )
                    )

        def initial_sort_key(entry: _SupportingQueueEntry) -> tuple[float, float, int, int, str]:
            candidate = recent_closed_candidates.get(entry.candidate_issue_number)
            if candidate is not None:
                return (
                    0,
                    _closed_sort_value(candidate.issue),
                    entry.candidate_issue_number,
                    min(entry.root_open_issue_numbers),
                    str(entry.source_association.get("sourceEvidenceId", "")),
                )
            return (
                1,
                float(min(entry.root_open_issue_numbers)),
                entry.candidate_issue_number,
                min(entry.root_open_issue_numbers),
                str(entry.source_association.get("sourceEvidenceId", "")),
            )

        traversal.queue.extend(sorted(initial_entries, key=initial_sort_key))
        self._drain_supporting_queue(
            open_details,
            recent_closed_seed,
            traversal,
            include_timeline=include_timeline,
        )

    def _build_candidate_indexes(
        self, recent_closed_details: dict[int, _CandidateIssue]
    ) -> tuple[dict[tuple[str, str], list[int]], dict[tuple[str, str], list[int]]]:
        marker_index: dict[tuple[str, str], list[int]] = defaultdict(list)
        fact_index: dict[tuple[str, str], list[int]] = defaultdict(list)
        for number, detail in sorted(recent_closed_details.items()):
            for marker in detail.markers:
                if marker["key"] not in _CORRELATION_MARKER_KEYS:
                    continue
                marker_index[(marker["key"], marker["normalized"])].append(number)
            for fact in detail.facts:
                if fact["field"] not in _CORRELATION_FACT_FIELDS:
                    continue
                fact_index[(fact["field"], fact["normalized"])].append(number)
        return marker_index, fact_index

    def _match_recent_closed_candidates(
        self,
        open_details: dict[int, _NormalizedIssue],
        marker_index: dict[tuple[str, str], list[int]],
        fact_index: dict[tuple[str, str], list[int]],
        recent_closed_candidates: dict[int, _CandidateIssue],
        recent_closed_seed: dict[int, dict[str, Any]],
        traversal: _SupportingTraversalState,
        *,
        include_timeline: bool,
    ) -> None:
        entries_by_candidate: dict[int, list[_SupportingQueueEntry]] = defaultdict(list)
        for open_number, detail in sorted(open_details.items()):
            marker_candidates: list[int] = []
            for marker in detail.markers:
                if marker["key"] not in _CORRELATION_MARKER_KEYS:
                    continue
                marker_candidates.extend(marker_index.get((marker["key"], marker["normalized"]), ()))
            fact_candidates: list[int] = []
            for fact in detail.facts:
                if fact["field"] not in _CORRELATION_FACT_FIELDS:
                    continue
                fact_candidates.extend(fact_index.get((fact["field"], fact["normalized"]), ()))

            unique_marker_candidates = sorted(set(marker_candidates))
            unique_fact_candidates = sorted(set(fact_candidates))

            if len(unique_marker_candidates) > self._budgets["marker_candidates"]:
                self._mark_traversal_root_incomplete(traversal, open_number, truncated=True)
                self._warnings.append(
                    f"marker_candidates budget truncated candidates for issue {open_number} from {len(unique_marker_candidates)} to {self._budgets['marker_candidates']}."
                )
                unique_marker_candidates = unique_marker_candidates[: self._budgets["marker_candidates"]]
            if len(unique_fact_candidates) > self._budgets["fact_candidates"]:
                self._mark_traversal_root_incomplete(traversal, open_number, truncated=True)
                self._warnings.append(
                    f"fact_candidates budget truncated candidates for issue {open_number} from {len(unique_fact_candidates)} to {self._budgets['fact_candidates']}."
                )
                unique_fact_candidates = unique_fact_candidates[: self._budgets["fact_candidates"]]

            for candidate_number in unique_marker_candidates:
                entries_by_candidate[candidate_number].append(
                    _SupportingQueueEntry(
                        candidate_issue_number=candidate_number,
                        root_open_issue_numbers=frozenset({open_number}),
                        depth=1,
                        source_association={
                            "sourceIssueNumber": open_number,
                            "sourceEvidenceId": f"issue:{open_number}",
                            "sourceUrl": detail.issue["url"],
                            "extractionMethod": "marker-match",
                        },
                        explicit=False,
                    )
                )
            for candidate_number in unique_fact_candidates:
                entries_by_candidate[candidate_number].append(
                    _SupportingQueueEntry(
                        candidate_issue_number=candidate_number,
                        root_open_issue_numbers=frozenset({open_number}),
                        depth=1,
                        source_association={
                            "sourceIssueNumber": open_number,
                            "sourceEvidenceId": f"issue:{open_number}",
                            "sourceUrl": detail.issue["url"],
                            "extractionMethod": "fact-match",
                        },
                        explicit=False,
                    )
                )

        for candidate_number in sorted(
            entries_by_candidate,
            key=lambda number: (
                min(
                    0
                    if entry.source_association.get("extractionMethod") == "marker-match"
                    else 1
                    for entry in entries_by_candidate[number]
                ),
                _closed_sort_value(recent_closed_candidates[number].issue),
                number,
            ),
        ):
            traversal.queue.extend(
                sorted(
                    entries_by_candidate[candidate_number],
                    key=lambda entry: (
                        0
                        if entry.source_association.get("extractionMethod") == "marker-match"
                        else 1,
                        min(entry.root_open_issue_numbers),
                    ),
                )
            )
        self._drain_supporting_queue(
            open_details,
            recent_closed_seed,
            traversal,
            include_timeline=include_timeline,
        )

    def _drain_supporting_queue(
        self,
        open_details: dict[int, _NormalizedIssue],
        recent_closed_seed: dict[int, dict[str, Any]],
        traversal: _SupportingTraversalState,
        *,
        include_timeline: bool,
    ) -> None:
        while traversal.queue:
            entry = traversal.queue.popleft()
            target_number = entry.candidate_issue_number
            roots = {
                root_number
                for root_number in entry.root_open_issue_numbers
                if root_number != target_number
            }
            if not roots or target_number in open_details:
                continue

            active_roots = {
                root_number
                for root_number in roots
                if (
                    traversal.processed_depth_by_root.get((target_number, root_number))
                    is None
                    or entry.depth
                    < traversal.processed_depth_by_root[(target_number, root_number)]
                )
            }
            if not active_roots:
                continue
            for root_number in active_roots:
                traversal.processed_depth_by_root[(target_number, root_number)] = entry.depth
            traversal.roots_by_issue[target_number].update(active_roots)

            if entry.depth > 2:
                self._record_supporting_disposition(
                    traversal,
                    target_number,
                    active_roots,
                    entry,
                    "excluded-depth",
                )
                if entry.explicit:
                    _record_supporting_reference_exclusion(
                        entry.source_association,
                        active_roots,
                        reason="depth-limit",
                    )
                for root_number in active_roots:
                    self._mark_traversal_root_incomplete(
                        traversal,
                        root_number,
                        truncated=True,
                    )
                continue

            if target_number in traversal.budget_excluded_issue_numbers:
                self._record_supporting_disposition(
                    traversal,
                    target_number,
                    active_roots,
                    entry,
                    "excluded-budget",
                )
                if entry.explicit:
                    _record_supporting_reference_exclusion(
                        entry.source_association,
                        active_roots,
                        reason="global-budget",
                    )
                for root_number in active_roots:
                    self._mark_traversal_root_incomplete(
                        traversal,
                        root_number,
                        truncated=True,
                    )
                continue
            if target_number in traversal.failed_issue_numbers:
                self._record_supporting_disposition(
                    traversal,
                    target_number,
                    active_roots,
                    entry,
                    "failed",
                )
                for root_number in active_roots:
                    self._mark_traversal_root_incomplete(traversal, root_number)
                continue

            if traversal.remaining <= 0:
                self._record_supporting_disposition(
                    traversal,
                    target_number,
                    active_roots,
                    entry,
                    "excluded-budget",
                )
                if entry.explicit:
                    _record_supporting_reference_exclusion(
                        entry.source_association,
                        active_roots,
                        reason="global-budget",
                    )
                if target_number not in traversal.probed_issue_numbers:
                    traversal.budget_excluded_issue_numbers.add(target_number)
                    if entry.explicit and target_number not in recent_closed_seed:
                        traversal.skipped_explicit_detail_issue_numbers.add(target_number)
                    else:
                        traversal.skipped_other_issue_numbers.add(target_number)
                for root_number in active_roots:
                    self._mark_traversal_root_incomplete(
                        traversal,
                        root_number,
                        truncated=True,
                    )
                continue

            if target_number not in traversal.probed_issue_numbers:
                traversal.probed_issue_numbers.add(target_number)
                traversal.remaining -= 1

            detail = traversal.loaded_supporting_details.get(target_number)
            summary = traversal.fetched_issue_summaries.get(target_number)
            if detail is None and summary is None:
                seed_entry = recent_closed_seed.get(target_number)
                if seed_entry is not None:
                    raw_issue = seed_entry["issue"]
                    labels = sorted(seed_entry["labels"])
                else:
                    endpoint = f"/repos/{self._repository}/issues/{target_number}"
                    try:
                        raw_issue = self._client.get(endpoint)
                    except Exception as exc:
                        self._collection_errors.append(CollectionError("reference", endpoint, str(exc)))
                        traversal.failed_issue_numbers.add(target_number)
                        self._record_supporting_disposition(
                            traversal,
                            target_number,
                            active_roots,
                            entry,
                            "failed",
                        )
                        for root_number in active_roots:
                            self._mark_traversal_root_incomplete(traversal, root_number)
                        continue
                    labels = sorted(_extract_labels(raw_issue)) if isinstance(raw_issue, dict) else []

                if not isinstance(raw_issue, dict):
                    traversal.failed_issue_numbers.add(target_number)
                    self._record_supporting_disposition(
                        traversal,
                        target_number,
                        active_roots,
                        entry,
                        "failed",
                    )
                    for root_number in active_roots:
                        self._mark_traversal_root_incomplete(traversal, root_number)
                    continue
                if raw_issue.get("pull_request"):
                    continue

                normalized_state = _text(raw_issue, "state")
                if normalized_state == "closed":
                    detail = self._load_issue_detail(
                        raw_issue,
                        labels,
                        include_timeline=include_timeline,
                    )
                    traversal.loaded_supporting_details[target_number] = detail
                else:
                    summary = self._load_issue_summary(raw_issue, labels)
                    traversal.fetched_issue_summaries[target_number] = summary

            if detail is not None or summary is not None:
                self._record_supporting_disposition(
                    traversal,
                    target_number,
                    active_roots,
                    entry,
                    "selected",
                )
            if detail is not None:
                for root_number in active_roots:
                    traversal.selected_candidates_by_root[root_number].add(target_number)
                    self._add_supporting_search_candidate(root_number, target_number)
            if target_number in self._reference_truncated_issues:
                for root_number in active_roots:
                    self._mark_traversal_root_incomplete(
                        traversal,
                        root_number,
                        truncated=True,
                    )
            if target_number in self._supporting_comment_failed_issue_numbers:
                for root_number in active_roots:
                    self._mark_traversal_root_incomplete(traversal, root_number)

            references = detail.references if detail is not None else summary.references if summary is not None else []
            for ref in references:
                if ref["targetType"] != "issue" or not _same_repository(
                    ref["targetRepository"],
                    self._repository,
                ):
                    continue
                traversal.queue.append(
                    _SupportingQueueEntry(
                        candidate_issue_number=int(ref["targetNumber"]),
                        root_open_issue_numbers=frozenset(active_roots),
                        depth=entry.depth + 1,
                        source_association=ref,
                        explicit=True,
                    )
                )

    def _record_supporting_disposition(
        self,
        traversal: _SupportingTraversalState,
        target_number: int,
        roots: set[int],
        entry: _SupportingQueueEntry,
        disposition: str,
    ) -> None:
        extraction_method = str(entry.source_association.get("extractionMethod", "explicit-reference"))
        for root_number in roots:
            disposition_key = (target_number, root_number)
            existing_disposition = traversal.dispositions_by_issue_and_root.get(disposition_key)
            if existing_disposition != "selected" or disposition == "selected":
                traversal.dispositions_by_issue_and_root[disposition_key] = disposition
            provenance_key = (target_number, root_number, disposition)
            traversal.provenance_by_issue_root_disposition[provenance_key].append(
                {
                    "sourceIssueNumber": root_number,
                    "sourceEvidenceId": entry.source_association["sourceEvidenceId"],
                    "sourceUrl": entry.source_association["sourceUrl"],
                    "extractionMethod": extraction_method,
                }
            )

    def _mark_traversal_root_incomplete(
        self,
        traversal: _SupportingTraversalState,
        root_number: int,
        *,
        truncated: bool = False,
    ) -> None:
        self._mark_supporting_search_incomplete(root_number, truncated=truncated)
        if truncated:
            traversal.truncated_by_root[root_number] = True

    def _append_supporting_budget_warnings(self, traversal: _SupportingTraversalState) -> None:
        used = self._budgets["max_supporting_closed"] - traversal.remaining
        if traversal.skipped_explicit_detail_issue_numbers:
            self._warnings.append(
                "max_supporting_closed budget truncated "
                f"{len(traversal.skipped_explicit_detail_issue_numbers)} explicit issue detail candidate(s) "
                f"after {used} bounded fetch(es)."
            )
        if traversal.skipped_other_issue_numbers:
            discarded = len(traversal.skipped_other_issue_numbers)
            self._warnings.append(
                "max_supporting_closed budget discarded "
                f"{discarded} supporting issue candidate(s); kept {used} of {used + discarded} "
                "after prioritizing explicit references."
            )

    def _finalize_issue(self, detail: _NormalizedIssue, supporting_issue_numbers: list[int]) -> dict[str, Any]:
        finalized = dict(detail.issue)
        finalized["supportingIssueNumbers"] = supporting_issue_numbers
        finalized["markers"] = detail.markers
        finalized["facts"] = detail.facts
        finalized["occurrences"] = detail.occurrences
        issue_number = int(finalized["number"])
        supporting_search = self._supporting_search_payload(issue_number)
        if supporting_search is not None:
            finalized["supportingSearch"] = supporting_search
            issue_record = self._evidence.get(f"issue:{issue_number}")
            if isinstance(issue_record, dict) and isinstance(issue_record.get("payload"), dict):
                issue_record["payload"]["supportingSearch"] = copy.deepcopy(supporting_search)
        return finalized

    def _initialize_supporting_searches(
        self,
        open_seed: dict[int, dict[str, Any]],
        *,
        enabled: bool,
        inventory_complete: bool,
    ) -> None:
        self._supporting_searches = {
            issue_number: {
                "complete": enabled and inventory_complete,
                "candidateIssueNumbers": set(),
                "truncated": False,
            }
            for issue_number in sorted(open_seed)
        }

    def _add_supporting_search_candidate(self, issue_number: int, candidate_number: int) -> None:
        search = self._supporting_searches.get(issue_number)
        if search is not None:
            search["candidateIssueNumbers"].add(candidate_number)

    def _mark_supporting_search_incomplete(self, issue_number: int, *, truncated: bool = False) -> None:
        search = self._supporting_searches.get(issue_number)
        if search is None:
            return
        search["complete"] = False
        if truncated:
            search["truncated"] = True

    def _supporting_search_payload(self, issue_number: int) -> dict[str, Any] | None:
        search = self._supporting_searches.get(issue_number)
        if search is None:
            return None
        payload = {
            "complete": bool(search["complete"]),
            "candidateIssueNumbers": sorted(search["candidateIssueNumbers"]),
            "truncated": bool(search["truncated"]),
        }
        candidate_dispositions: list[dict[str, Any]] = []
        for (candidate_number, root_number), disposition in sorted(
            self._supporting_dispositions_by_issue_and_root.items()
        ):
            if root_number != issue_number or disposition == "selected":
                continue
            provenance = self._supporting_provenance_by_issue_root_disposition.get(
                (candidate_number, root_number, disposition),
                [],
            )
            candidate_dispositions.append(
                {
                    "issueNumber": candidate_number,
                    "disposition": disposition,
                    "provenance": _sorted_unique_records(
                        [
                            {
                                "sourceEvidenceId": item["sourceEvidenceId"],
                                "sourceUrl": item["sourceUrl"],
                                "extractionMethod": item["extractionMethod"],
                            }
                            for item in provenance
                        ],
                        ("sourceEvidenceId", "sourceUrl", "extractionMethod"),
                    ),
                }
            )
        if candidate_dispositions:
            payload["candidateDispositions"] = candidate_dispositions
        return payload

    def _make_evidence_record(self, kind: str, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": kind,
            "url": url,
            "collectedAt": self._collected_at,
            "availability": "available",
            "payload": payload,
        }

    def _normalize_issue(self, raw_issue: dict[str, Any], labels: list[str]) -> dict[str, Any]:
        return {
            "number": int(raw_issue["number"]),
            "state": _text(raw_issue, "state"),
            "title": _text(raw_issue, "title"),
            "body": _text(raw_issue, "body"),
            "url": _text(raw_issue, "html_url"),
            "createdAt": _text(raw_issue, "created_at"),
            "updatedAt": _text(raw_issue, "updated_at"),
            "closedAt": raw_issue.get("closed_at"),
            "labels": labels,
            "author": _nested_text(raw_issue, ("user", "login")),
        }

    def _finalize_evidence(
        self,
        *,
        kept_issue_numbers: set[int],
        references: dict[int, list[dict[str, Any]]],
        fetched_issue_summaries: dict[int, _IssueSummary],
    ) -> dict[str, dict[str, Any]]:
        final_evidence: dict[str, dict[str, Any]] = {}
        for evidence_id, record in sorted(self._evidence.items()):
            issue_number = _issue_number_from_evidence_id(evidence_id)
            if issue_number is None or issue_number not in kept_issue_numbers:
                continue
            final_evidence[evidence_id] = record

        for evidence_id, record in self._build_reference_stub_evidence(
            references, kept_issue_numbers, fetched_issue_summaries
        ).items():
            final_evidence[evidence_id] = record
        self._associate_issue_reference_evidence(final_evidence, references)
        self._associate_support_evidence(final_evidence)

        return dict(sorted(final_evidence.items()))

    def _associate_issue_reference_evidence(
        self,
        evidence: dict[str, dict[str, Any]],
        references: dict[int, list[dict[str, Any]]],
    ) -> None:
        for issue_refs in references.values():
            for ref in issue_refs:
                if ref["targetType"] != "issue" or not _same_repository(
                    ref["targetRepository"], self._repository
                ):
                    continue
                evidence_id = f"issue:{int(ref['targetNumber'])}"
                record = evidence.get(evidence_id)
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                if self._has_supporting_disposition(int(ref["targetNumber"])):
                    continue
                payload["referencedBy"] = _merge_referenced_by(
                    record,
                    [ref],
                )

    def _associate_support_evidence(
        self,
        evidence: dict[str, dict[str, Any]],
    ) -> None:
        issue_numbers = sorted(
            {
                issue_number
                for issue_number, _ in self._supporting_dispositions_by_issue_and_root
            }
        )
        for issue_number in issue_numbers:
            record = evidence.get(f"issue:{issue_number}")
            if not isinstance(record, dict):
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            required_disposition = (
                "selected" if record.get("availability") == "available" else None
            )
            refs: list[dict[str, Any]] = []
            for (candidate_number, root_number), disposition in sorted(
                self._supporting_dispositions_by_issue_and_root.items()
            ):
                if candidate_number != issue_number:
                    continue
                if required_disposition is not None and disposition != required_disposition:
                    continue
                if required_disposition is None and disposition == "selected":
                    continue
                refs.extend(
                    self._supporting_provenance_by_issue_root_disposition.get(
                        (candidate_number, root_number, disposition),
                        [],
                    )
                )
            payload["referencedBy"] = _merge_referenced_by(record, refs)

    def _has_supporting_disposition(self, issue_number: int) -> bool:
        return any(
            candidate_number == issue_number
            for candidate_number, _ in self._supporting_dispositions_by_issue_and_root
        )

    def _build_reference_stub_evidence(
        self,
        references: dict[int, list[dict[str, Any]]],
        kept_issue_numbers: set[int],
        fetched_issue_summaries: dict[int, _IssueSummary],
    ) -> dict[str, dict[str, Any]]:
        stubs: dict[str, dict[str, Any]] = {}
        for issue_number, issue_refs in references.items():
            for ref in issue_refs:
                evidence_id: str | None = None
                kind: str | None = None
                payload_key: str | None = None
                payload_value: Any = None
                if ref["targetType"] == "issue":
                    if not _same_repository(ref["targetRepository"], self._repository):
                        continue
                    target_number = int(ref["targetNumber"])
                    if target_number in kept_issue_numbers:
                        continue
                    summary = fetched_issue_summaries.get(target_number)
                    exclusion_reasons = _supporting_reference_exclusion_reasons(ref)
                    if (
                        summary is None
                        and target_number not in self._supporting_budget_excluded_issue_numbers
                        and target_number not in self._supporting_probe_failed_issue_numbers
                        and not exclusion_reasons
                    ):
                        continue
                    evidence_id = f"issue:{target_number}"
                    record = stubs.get(evidence_id)
                    if record is None:
                        if summary is None:
                            record = self._make_evidence_record(
                                "issue-event",
                                ref["targetUrl"],
                                {
                                    "number": target_number,
                                    "targetRepository": self._repository,
                                    "referencedBy": [],
                                },
                            )
                            if target_number in self._supporting_budget_excluded_issue_numbers:
                                record["payload"]["supportingBudgetExcluded"] = True
                                record["availability"] = "not-enriched"
                            elif "depth-limit" in exclusion_reasons:
                                record["payload"]["supportingDepthExcluded"] = True
                                record["availability"] = "not-enriched"
                            else:
                                record["payload"]["supportingProbeFailed"] = True
                                record["availability"] = "partial"
                        else:
                            record = self._make_evidence_record(
                                "issue-event",
                                summary.issue["url"],
                                {
                                    **summary.issue,
                                    "references": summary.references,
                                    **(
                                        {"excludedReferences": summary.excluded_references}
                                        if summary.excluded_references
                                        else {}
                                    ),
                                    "referencedBy": [],
                                },
                            )
                            if summary.issue["state"] == "closed":
                                record["payload"]["supportingBudgetExcluded"] = True
                                record["availability"] = "not-enriched"
                        stubs[evidence_id] = record
                    if not self._has_supporting_disposition(target_number):
                        record["payload"]["referencedBy"].append(
                            {
                                "sourceIssueNumber": issue_number,
                                "sourceEvidenceId": ref["sourceEvidenceId"],
                                "sourceUrl": ref["sourceUrl"],
                                "extractionMethod": ref["extractionMethod"],
                            }
                        )
                    continue
                if ref["targetType"] == "workflow-run":
                    evidence_id = f"run:{ref['runId']}"
                    kind = "workflow-run"
                    payload_key = "runId"
                    payload_value = ref["runId"]
                elif ref["targetType"] == "pull-request":
                    evidence_id = _repository_scoped_evidence_id(
                        "pr", ref["targetRepository"], self._repository, ref["targetNumber"]
                    )
                    kind = "pull-request"
                    payload_key = "number"
                    payload_value = ref["targetNumber"]
                elif ref["targetType"] == "commit":
                    evidence_id = _repository_scoped_evidence_id(
                        "commit", ref["targetRepository"], self._repository, ref["sha"]
                    )
                    kind = "commit"
                    payload_key = "sha"
                    payload_value = ref["sha"]
                if evidence_id is None or kind is None or payload_key is None:
                    continue

                record = stubs.get(evidence_id)
                if record is None:
                    record = self._make_evidence_record(
                        kind,
                        ref["targetUrl"],
                        {
                            payload_key: payload_value,
                            "targetRepository": ref["targetRepository"],
                            "referencedBy": [],
                        },
                    )
                    stubs[evidence_id] = record

                record["payload"]["referencedBy"].append(
                    {
                        "sourceIssueNumber": issue_number,
                        "sourceEvidenceId": ref["sourceEvidenceId"],
                        "sourceUrl": ref["sourceUrl"],
                        "extractionMethod": ref["extractionMethod"],
                    }
                )

        for record in stubs.values():
            record["payload"]["referencedBy"] = sorted(
                record["payload"]["referencedBy"],
                key=lambda item: (item["sourceIssueNumber"], item["sourceEvidenceId"], item["extractionMethod"]),
            )
        return stubs

    def _enrich_issue_reference(
        self,
        evidence: dict[str, dict[str, Any]],
        collection_errors: list[CollectionError],
        target_repository: str,
        target_number: int,
        refs: list[dict[str, Any]],
        *,
        preserve_existing: bool = False,
    ) -> None:
        endpoint = f"/repos/{target_repository}/issues/{target_number}"
        evidence_id = _repository_scoped_evidence_id(
            "issue", target_repository, self._repository, target_number
        )
        existing_record = evidence.get(evidence_id)
        if preserve_existing and isinstance(existing_record, dict):
            evidence[evidence_id] = copy.deepcopy(existing_record)
            return
        referenced_by = _merge_referenced_by(existing_record, refs)
        fallback_url = _reference_target_url(refs, f"https://github.com/{target_repository}/issues/{target_number}")
        if _all_supporting_references_excluded(refs):
            if isinstance(existing_record, dict):
                evidence[evidence_id] = copy.deepcopy(existing_record)
                return
            exclusion_reasons = {
                reason
                for ref in refs
                for reason in _supporting_reference_exclusion_reasons(ref)
            }
            payload = {
                "number": target_number,
                "targetRepository": target_repository,
                "referencedBy": referenced_by,
            }
            if "global-budget" in exclusion_reasons:
                payload["supportingBudgetExcluded"] = True
            if "depth-limit" in exclusion_reasons:
                payload["supportingDepthExcluded"] = True
            record = self._make_evidence_record("issue-event", fallback_url, payload)
            record["availability"] = "not-enriched"
            evidence[evidence_id] = record
            return
        if isinstance(existing_record, dict):
            existing_payload = existing_record.get("payload")
            if isinstance(existing_payload, dict):
                if (
                    existing_payload.get("supportingBudgetExcluded") is True
                    or existing_payload.get("supportingDepthExcluded") is True
                    or existing_payload.get("supportingProbeFailed") is True
                ):
                    evidence[evidence_id] = copy.deepcopy(existing_record)
                    return
        try:
            raw_issue = self._client.get(endpoint)
        except Exception as exc:
            collection_errors.append(CollectionError("issue", endpoint, str(exc)))
            if isinstance(existing_record, dict):
                evidence[evidence_id] = copy.deepcopy(existing_record)
                evidence[evidence_id]["availability"] = "partial"
                carried_payload = evidence[evidence_id].setdefault("payload", {})
                carried_payload["referencedBy"] = referenced_by
                carried_payload["errorCategory"] = _error_category(exc)
                carried_payload["errorMessage"] = str(exc)
                return
            evidence[evidence_id] = self._make_partial_record(
                "issue-event",
                fallback_url,
                {
                    "number": target_number,
                    "targetRepository": target_repository,
                    "referencedBy": referenced_by,
                },
                exc,
            )
            return

        if not isinstance(raw_issue, dict):
            collection_errors.append(CollectionError("issue", endpoint, "Unexpected issue payload shape"))
            if isinstance(existing_record, dict):
                evidence[evidence_id] = copy.deepcopy(existing_record)
                evidence[evidence_id]["availability"] = "partial"
                carried_payload = evidence[evidence_id].setdefault("payload", {})
                carried_payload["referencedBy"] = referenced_by
                carried_payload["errorCategory"] = "unexpected-response"
                carried_payload["errorMessage"] = "Unexpected issue payload shape"
                return
            evidence[evidence_id] = self._make_partial_record(
                "issue-event",
                fallback_url,
                {
                    "number": target_number,
                    "targetRepository": target_repository,
                    "referencedBy": referenced_by,
                },
                RuntimeError("Unexpected issue payload shape"),
            )
            return

        if raw_issue.get("pull_request"):
            evidence.pop(evidence_id, None)
            self._enrich_pull_request_reference(
                evidence,
                collection_errors,
                target_repository,
                target_number,
                refs,
            )
            return

        payload = _merge_issue_evidence_payload(
            existing_record,
            {
                "number": target_number,
                "targetRepository": target_repository,
                "state": _text(raw_issue, "state"),
                "title": _text(raw_issue, "title"),
                "body": _text(raw_issue, "body"),
                "url": _text(raw_issue, "html_url") or fallback_url,
                "createdAt": _text(raw_issue, "created_at"),
                "updatedAt": _text(raw_issue, "updated_at"),
                "closedAt": raw_issue.get("closed_at"),
                "labels": _extract_labels(raw_issue),
                "referencedBy": referenced_by,
            },
        )
        evidence[evidence_id] = self._make_evidence_record(
            "issue-event",
            _text(raw_issue, "html_url") or fallback_url,
            payload,
        )

    def _enrich_pull_request_reference(
        self,
        evidence: dict[str, dict[str, Any]],
        collection_errors: list[CollectionError],
        target_repository: str,
        target_number: int,
        refs: list[dict[str, Any]],
        *,
        include_primary_current_state: bool = True,
    ) -> None:
        issue_endpoint = f"/repos/{target_repository}/issues/{target_number}"
        issue_url = _reference_target_url(refs, f"https://github.com/{target_repository}/pull/{target_number}")
        evidence_id = _repository_scoped_evidence_id(
            "pr", target_repository, self._repository, target_number
        )
        referenced_by = _merge_referenced_by(evidence.get(evidence_id), refs)

        try:
            raw_issue = self._client.get(issue_endpoint)
        except Exception as exc:
            collection_errors.append(CollectionError("pull-request", issue_endpoint, str(exc)))
            evidence[evidence_id] = self._make_partial_record(
                "pull-request",
                issue_url,
                {
                    "number": target_number,
                    "targetRepository": target_repository,
                    "referencedBy": referenced_by,
                },
                exc,
            )
            return

        if not isinstance(raw_issue, dict):
            collection_errors.append(CollectionError("pull-request", issue_endpoint, "Unexpected pull request issue payload shape"))
            evidence[evidence_id] = self._make_partial_record(
                "pull-request",
                issue_url,
                {
                    "number": target_number,
                    "targetRepository": target_repository,
                    "referencedBy": referenced_by,
                },
                RuntimeError("Unexpected pull request issue payload shape"),
            )
            return

        if not raw_issue.get("pull_request"):
            self._enrich_issue_reference(evidence, collection_errors, target_repository, target_number, refs)
            evidence.pop(evidence_id, None)
            return

        pull_endpoint = f"/repos/{target_repository}/pulls/{target_number}"
        files_endpoint = f"/repos/{target_repository}/pulls/{target_number}/files?per_page=100"

        try:
            raw_pull = self._client.get(pull_endpoint)
        except Exception as exc:
            collection_errors.append(CollectionError("pull-request", pull_endpoint, str(exc)))
            evidence[evidence_id] = self._make_partial_record(
                "pull-request",
                _text(raw_issue, "html_url") or issue_url,
                {
                    "number": target_number,
                    "targetRepository": target_repository,
                    "referencedBy": referenced_by,
                },
                exc,
            )
            return

        if not isinstance(raw_pull, dict):
            collection_errors.append(CollectionError("pull-request", pull_endpoint, "Unexpected pull request payload shape"))
            evidence[evidence_id] = self._make_partial_record(
                "pull-request",
                _text(raw_issue, "html_url") or issue_url,
                {
                    "number": target_number,
                    "targetRepository": target_repository,
                    "referencedBy": referenced_by,
                },
                RuntimeError("Unexpected pull request payload shape"),
            )
            return

        primary_only = all(
            isinstance(ref, dict)
            and ref.get("extractionMethod") == "primary-inventory"
            for ref in refs
        )
        raw_files = (
            []
            if primary_only
            else self._load_paged_list(
                collection_errors,
                stage="pull-request-files",
                endpoint=files_endpoint,
                key="files",
            )
        )
        linked_issues = [
            {
                "targetNumber": int(ref["targetNumber"]),
                "targetUrl": ref["targetUrl"],
                "extractionMethod": ref["extractionMethod"],
            }
            for ref in self._extract_references(
                target_number,
                _text(raw_issue, "body"),
                evidence_id,
                _text(raw_issue, "html_url") or issue_url,
                local_repository=target_repository,
            )
            if ref["targetType"] == "issue"
        ]

        # Only pull requests the inventory selected are triaged, so only they
        # pay for the extra current-state GETs. A pull request that merely got
        # mentioned in an issue body keeps the cheaper reference shape.
        primary_state: dict[str, Any] = {}
        if (
            include_primary_current_state
            and _has_primary_inventory_reference(refs)
        ):
            primary_state = self._collect_pull_request_current_state(
                collection_errors,
                target_repository,
                target_number,
                raw_pull,
                raw_issue,
            )

        evidence[evidence_id] = self._make_evidence_record(
            "pull-request",
            _text(raw_pull, "html_url") or _text(raw_issue, "html_url") or issue_url,
            {
                "number": target_number,
                "targetRepository": target_repository,
                "state": _text(raw_pull, "state") or _text(raw_issue, "state"),
                "mergedAt": _text(raw_pull, "merged_at"),
                "mergeCommitSha": _text(raw_pull, "merge_commit_sha"),
                "base": {
                    "ref": _nested_text(raw_pull, ("base", "ref")),
                    "sha": _nested_text(raw_pull, ("base", "sha")),
                },
                "head": {
                    "ref": _nested_text(raw_pull, ("head", "ref")),
                    "sha": _nested_text(raw_pull, ("head", "sha")),
                    "repository": _nested_text(raw_pull, ("head", "repo", "full_name")) or target_repository,
                },
                "files": [
                    {
                        "path": _text(raw_file, "filename"),
                        "status": _text(raw_file, "status"),
                    }
                    for raw_file in raw_files
                    if _text(raw_file, "filename")
                ],
                "linkedIssues": linked_issues,
                "referencedBy": referenced_by,
                **primary_state,
            },
        )

    def _collect_pull_request_current_state(
        self,
        collection_errors: list[CollectionError],
        target_repository: str,
        target_number: int,
        raw_pull: dict[str, Any],
        raw_issue: dict[str, Any],
    ) -> dict[str, Any]:
        """Fetch the bounded, GET-only current CI and review state.

        Every endpoint is derived from the already-fetched pull request record
        (its number and its current head SHA), never from free text, so no
        caller can steer collection at an arbitrary path. A failed fetch stays
        ``None`` rather than becoming an empty result, because "nothing failed"
        and "we could not look" must not summarize to the same conclusion.
        """
        head_sha = _nested_text(raw_pull, ("head", "sha"))
        check_runs: list[dict[str, Any]] | None = None
        combined_status: dict[str, Any] | None = None
        if head_sha:
            checks_endpoint = (
                f"/repos/{target_repository}/commits/{head_sha}/check-runs?per_page=100"
            )
            check_runs = self._load_optional_paged_list(
                collection_errors,
                stage="pull-request-checks",
                endpoint=checks_endpoint,
                key="check_runs",
            )
            # The combined-status API is the legacy commit-status surface. It
            # is only consulted when no check run reported, because a
            # repository can use either mechanism (or both).
            if check_runs == []:
                status_endpoint = f"/repos/{target_repository}/commits/{head_sha}/status"
                combined_status = self._load_optional_object(
                    collection_errors,
                    stage="pull-request-status",
                    endpoint=status_endpoint,
                )

        reviews_endpoint = (
            f"/repos/{target_repository}/pulls/{target_number}/reviews?per_page=100"
        )
        reviews = self._load_optional_paged_list(
            collection_errors,
            stage="pull-request-reviews",
            endpoint=reviews_endpoint,
            key="reviews",
        )

        current_state = build_pull_request_current_state(
            raw_pull,
            check_runs=check_runs,
            combined_status=combined_status,
            reviews=reviews,
        )
        payload: dict[str, Any] = {
            "currentState": current_state,
            "assignees": sorted(
                {
                    str(assignee["login"])
                    for assignee in raw_issue.get("assignees", [])
                    if isinstance(assignee, dict)
                    and isinstance(assignee.get("login"), str)
                }
            ),
        }
        status_comments = self._collect_pull_request_status_comments(
            collection_errors,
            target_repository,
            target_number,
        )
        if status_comments is not None:
            payload["shepherdStatusComments"] = status_comments
        return payload

    def _collect_pull_request_status_comments(
        self,
        collection_errors: list[CollectionError],
        target_repository: str,
        target_number: int,
    ) -> list[dict[str, Any]] | None:
        """Retain only the shepherd's own canonical status comments.

        Everything else on the pull request is deliberately dropped: the
        shepherd needs its own comment identity to stay idempotent, and
        ingesting third-party comment text would let arbitrary prose reach the
        assessment handoff.
        """
        if self._shepherd_author is None:
            return None
        endpoint = f"/repos/{target_repository}/issues/{target_number}/comments?per_page=100"
        raw_comments = self._load_optional_paged_list(
            collection_errors,
            stage="pull-request-comments",
            endpoint=endpoint,
            key="comments",
        )
        if raw_comments is None:
            return None

        owned: list[dict[str, Any]] = []
        for raw_comment in raw_comments:
            comment_id = raw_comment.get("id")
            body = _text(raw_comment, "body")
            author = _nested_text(raw_comment, ("user", "login"))
            if (
                not isinstance(comment_id, int)
                or isinstance(comment_id, bool)
                or not body.startswith("[automated] ")
                or str(author or "").casefold() != self._shepherd_author
            ):
                continue
            markers = {
                str(marker.get("key")): str(marker.get("normalized"))
                for marker in self._extract_markers(
                    body, f"pr:{target_number}:comment:{comment_id}"
                )
            }
            idempotency_key = markers.get("idempotency-key")
            if markers.get("role") != "status" or not idempotency_key:
                continue
            owned.append(
                {
                    "id": comment_id,
                    "url": _text(raw_comment, "html_url"),
                    "body": body,
                    "idempotencyKey": idempotency_key,
                }
            )
        owned.sort(key=lambda comment: int(comment["id"]))
        return owned

    def _load_optional_paged_list(
        self,
        collection_errors: list[CollectionError],
        *,
        stage: str,
        endpoint: str,
        key: str,
    ) -> list[dict[str, Any]] | None:
        try:
            payload = self._client.get_pages(endpoint, key=key)
        except Exception as exc:
            collection_errors.append(CollectionError(stage, endpoint, str(exc)))
            return None
        return _paged_dict_items(payload, key)

    def _load_optional_object(
        self,
        collection_errors: list[CollectionError],
        *,
        stage: str,
        endpoint: str,
    ) -> dict[str, Any] | None:
        try:
            payload = self._client.get(endpoint)
        except Exception as exc:
            collection_errors.append(CollectionError(stage, endpoint, str(exc)))
            return None
        if not isinstance(payload, dict):
            collection_errors.append(
                CollectionError(stage, endpoint, f"Unexpected {stage} payload shape")
            )
            return None
        return payload

    def _enrich_commit_reference(
        self,
        evidence: dict[str, dict[str, Any]],
        collection_errors: list[CollectionError],
        target_repository: str,
        sha: str,
        refs: list[dict[str, Any]],
    ) -> None:
        endpoint = f"/repos/{target_repository}/commits/{sha}"
        fallback_url = _reference_target_url(refs, f"https://github.com/{target_repository}/commit/{sha}")
        try:
            raw_commit = self._client.get(endpoint)
        except Exception as exc:
            collection_errors.append(CollectionError("commit", endpoint, str(exc)))
            evidence[_repository_scoped_evidence_id(
                "commit", target_repository, self._repository, sha
            )] = self._make_partial_record(
                "commit",
                fallback_url,
                {
                    "sha": sha,
                    "targetRepository": target_repository,
                    "referencedBy": _normalize_referenced_by(refs),
                },
                exc,
            )
            return

        if not isinstance(raw_commit, dict):
            collection_errors.append(CollectionError("commit", endpoint, "Unexpected commit payload shape"))
            evidence[_repository_scoped_evidence_id(
                "commit", target_repository, self._repository, sha
            )] = self._make_partial_record(
                "commit",
                fallback_url,
                {
                    "sha": sha,
                    "targetRepository": target_repository,
                    "referencedBy": _normalize_referenced_by(refs),
                },
                RuntimeError("Unexpected commit payload shape"),
            )
            return

        full_sha = _text(raw_commit, "sha") or sha
        evidence_id = _repository_scoped_evidence_id(
            "commit", target_repository, self._repository, full_sha
        )
        referenced_by = _merge_referenced_by(evidence.get(evidence_id), refs)
        evidence[evidence_id] = self._make_evidence_record(
            "commit",
            _text(raw_commit, "html_url") or fallback_url,
            {
                "sha": full_sha,
                "targetRepository": target_repository,
                "author": {
                    "login": _nested_text(raw_commit, ("author", "login")),
                    "name": _nested_text(raw_commit, ("commit", "author", "name")),
                    "email": _nested_text(raw_commit, ("commit", "author", "email")),
                    "date": _nested_text(raw_commit, ("commit", "author", "date")),
                },
                "message": _nested_text(raw_commit, ("commit", "message")),
                "changedPaths": _unique_preserving_order(
                    [
                        _text(raw_file, "filename")
                        for raw_file in raw_commit.get("files", [])
                        if isinstance(raw_file, dict) and _text(raw_file, "filename")
                    ]
                ),
                "referencedBy": referenced_by,
            },
        )
        for ref in refs:
            original_sha = ref.get("sha")
            if isinstance(original_sha, str) and original_sha and original_sha != full_sha:
                evidence.pop(
                    _repository_scoped_evidence_id(
                        "commit", target_repository, self._repository, original_sha
                    ),
                    None,
                )

    def _enrich_workflow_run_reference(
        self,
        evidence: dict[str, dict[str, Any]],
        collection_errors: list[CollectionError],
        target_repository: str,
        run_id: int,
        refs: list[dict[str, Any]],
        *,
        minimal: bool,
        include_history: bool,
        include_retry_evidence: bool = False,
    ) -> None:
        run_endpoint = f"/repos/{target_repository}/actions/runs/{run_id}"
        evidence_id = f"run:{run_id}"
        referenced_by = _merge_referenced_by(evidence.get(evidence_id), refs)
        fallback_url = _reference_target_url(refs, f"https://github.com/{target_repository}/actions/runs/{run_id}")
        try:
            raw_run = self._client.get(run_endpoint)
        except Exception as exc:
            collection_errors.append(CollectionError("workflow-run", run_endpoint, str(exc)))
            evidence[evidence_id] = self._make_partial_record(
                "workflow-run",
                fallback_url,
                {
                    "runId": run_id,
                    "targetRepository": target_repository,
                    "recentHistory": [],
                    "recentHistoryCollected": False,
                    "recentHistoryTruncated": False,
                    "recentHistoryTotalCount": None,
                    "historyCoversSourceRun": False,
                    "recentHistoryGap": "run-detail-unavailable" if include_history else "not-requested",
                    "referencedBy": referenced_by,
                },
                exc,
            )
            return

        if not isinstance(raw_run, dict):
            collection_errors.append(CollectionError("workflow-run", run_endpoint, "Unexpected workflow run payload shape"))
            evidence[evidence_id] = self._make_partial_record(
                "workflow-run",
                fallback_url,
                {
                    "runId": run_id,
                    "targetRepository": target_repository,
                    "recentHistory": [],
                    "recentHistoryCollected": False,
                    "recentHistoryTruncated": False,
                    "recentHistoryTotalCount": None,
                    "historyCoversSourceRun": False,
                    "recentHistoryGap": "run-detail-unavailable" if include_history else "not-requested",
                    "referencedBy": referenced_by,
                },
                RuntimeError("Unexpected workflow run payload shape"),
            )
            return

        workflow_id = raw_run.get("workflow_id")
        workflow_id_value = workflow_id if isinstance(workflow_id, int) else 0
        attempt = raw_run.get("run_attempt")
        run_attempt = attempt if isinstance(attempt, int) and attempt > 0 else 1

        jobs_by_key: dict[tuple[int, int], dict[str, Any]] = {}
        current_jobs_endpoint = f"/repos/{target_repository}/actions/runs/{run_id}/jobs?per_page=100"
        for raw_job in self._load_paged_list(
            collection_errors,
            stage="workflow-jobs",
            endpoint=current_jobs_endpoint,
            key="jobs",
        ):
            normalized_job = self._normalize_workflow_job(
                raw_job,
                default_attempt=run_attempt,
                target_repository=target_repository,
            )
            if normalized_job is not None:
                jobs_by_key[(normalized_job["attempt"], normalized_job["jobId"])] = normalized_job

        for prior_attempt in (
            range(max(1, run_attempt - 2), run_attempt)
            if include_retry_evidence or not minimal
            else range(0)
        ):
            attempt_endpoint = f"/repos/{target_repository}/actions/runs/{run_id}/attempts/{prior_attempt}/jobs?per_page=100"
            for raw_job in self._load_paged_list(
                collection_errors,
                stage="workflow-jobs",
                endpoint=attempt_endpoint,
                key="jobs",
            ):
                normalized_job = self._normalize_workflow_job(
                    raw_job,
                    default_attempt=prior_attempt,
                    target_repository=target_repository,
                )
                if normalized_job is not None:
                    jobs_by_key[(normalized_job["attempt"], normalized_job["jobId"])] = normalized_job

        job_payloads: list[dict[str, Any]] = []
        failed_logs_collected = 0
        sorted_jobs = sorted(
            jobs_by_key.values(),
            key=lambda job: (-job["attempt"], job["jobId"]),
        )
        failed_jobs = [
            job
            for job in sorted_jobs
            if job["conclusion"] in {
                "action_required",
                "failure",
                "startup_failure",
                "timed_out",
            }
        ]
        failed_job_names = {
            job["name"]
            for job in failed_jobs
            if isinstance(job.get("name"), str) and job["name"]
        }
        recovery_jobs = [
            job
            for job in sorted_jobs
            if (
                include_retry_evidence
                and job["conclusion"] == "success"
                and job["attempt"] > 1
                and job.get("name") in failed_job_names
                and any(
                    failed.get("name") == job.get("name")
                    and failed["attempt"] < job["attempt"]
                    for failed in failed_jobs
                )
            )
        ]
        selected_failed_jobs = failed_jobs[:10] if minimal else failed_jobs
        selected_jobs = (
            [*selected_failed_jobs, *recovery_jobs[:3]]
            if minimal
            else sorted_jobs
        )
        for job_payload in selected_jobs:
            failed_job = job_payload["conclusion"] in {
                "action_required",
                "failure",
                "startup_failure",
                "timed_out",
            }
            log_eligible = _is_log_eligible_conclusion(job_payload["conclusion"])
            collect_log = (
                failed_job
                and log_eligible
                and (not minimal or failed_logs_collected < 3)
                and job_payload["attempt"] == run_attempt
            )
            annotation_ids = (
                self._enrich_job_annotations(
                    evidence,
                    collection_errors,
                    target_repository,
                    run_id,
                    job_payload,
                    referenced_by,
                )
                if failed_job and not minimal
                else []
            )
            job_payload["annotationEvidenceIds"] = annotation_ids
            log_evidence_id = (
                self._enrich_job_log(
                    evidence,
                    collection_errors,
                    target_repository,
                    run_id,
                    job_payload,
                    referenced_by,
                )
                if collect_log
                else None
            )
            if collect_log:
                failed_logs_collected += 1
            if log_evidence_id is not None:
                job_payload["logEvidenceId"] = log_evidence_id

            job_evidence_id = f"run:{run_id}:attempt:{job_payload['attempt']}:job:{job_payload['jobId']}"
            job_payload["referencedBy"] = referenced_by
            evidence[job_evidence_id] = self._make_evidence_record(
                "workflow-job",
                job_payload["url"],
                copy.deepcopy(job_payload),
            )
            job_payloads.append(job_payload)

        raw_artifacts: list[Any] = []
        artifacts: list[dict[str, Any]] = []
        if not minimal or include_retry_evidence:
            artifacts_endpoint = f"/repos/{target_repository}/actions/runs/{run_id}/artifacts?per_page=100"
            raw_artifacts = self._load_paged_list(
                collection_errors,
                stage="workflow-artifacts",
                endpoint=artifacts_endpoint,
                key="artifacts",
            )
            artifacts = [
                {
                    "name": _text(raw_artifact, "name"),
                    "expired": bool(raw_artifact.get("expired", False)),
                }
                for raw_artifact in raw_artifacts
                if isinstance(raw_artifact, dict)
                if _text(raw_artifact, "name")
            ]
        if include_retry_evidence and run_attempt > 1:
            selected_job_keys = {
                (job["attempt"], job["jobId"])
                for job in job_payloads
            }
            final_jobs = [
                job
                for job in sorted_jobs
                if (job["attempt"], job["jobId"])
                not in selected_job_keys
                if isinstance(job.get("name"), str)
                and self._repository_policy.retry_test_results.matches_aggregate_job(
                    job["name"]
                )
            ]
            self._enrich_retry_test_results(
                evidence,
                collection_errors,
                target_repository,
                run_id,
                raw_artifacts,
                [*job_payloads, *final_jobs],
                referenced_by,
            )

        branch = _text(raw_run, "head_branch")
        recent_history: list[dict[str, Any]] = []
        recent_history_collected = False
        recent_history_truncated = False
        recent_history_total_count: int | None = None
        history_covers_source_run = False
        recent_history_gap = "not-requested"
        if include_history:
            source_run_id = raw_run.get("id")
            source_created_at = _text(raw_run, "created_at")
            source_identity_available = (
                isinstance(source_run_id, int)
                and not isinstance(source_run_id, bool)
                and source_run_id == run_id
                and bool(source_created_at)
            )
            if source_identity_available:
                try:
                    _parse_timestamp(source_created_at)
                except ValueError:
                    source_identity_available = False

            if not source_identity_available:
                recent_history_gap = "source-run-identity-unavailable"
                collection_errors.append(
                    CollectionError(
                        "workflow-history",
                        run_endpoint,
                        "source run identity/timestamp unavailable for bounded recent-history collection",
                        "recent workflow history not collected",
                    )
                )
            elif workflow_id_value <= 0 or not branch:
                recent_history_gap = "workflow-or-branch-unavailable"
                collection_errors.append(
                    CollectionError(
                        "workflow-history",
                        run_endpoint,
                        "workflow/branch identity unavailable for bounded recent-history collection",
                        "recent workflow history not collected",
                    )
                )
            else:
                history_endpoint = (
                    f"/repos/{target_repository}/actions/workflows/{workflow_id_value}/runs"
                    f"?branch={quote(branch, safe='')}&per_page=10"
                )
                try:
                    raw_history = self._client.get(history_endpoint)
                except Exception as exc:
                    recent_history_gap = "request-failed"
                    collection_errors.append(
                        CollectionError(
                            "workflow-history",
                            history_endpoint,
                            str(exc),
                            "recent workflow history not collected",
                        )
                    )
                else:
                    raw_history_runs = (
                        raw_history.get("workflow_runs")
                        if isinstance(raw_history, dict)
                        else None
                    )
                    raw_total_count = (
                        raw_history.get("total_count")
                        if isinstance(raw_history, dict)
                        else None
                    )
                    total_count_is_valid = (
                        raw_total_count is None
                        or (
                            isinstance(raw_total_count, int)
                            and not isinstance(raw_total_count, bool)
                            and raw_total_count >= 0
                        )
                    )
                    if (
                        not isinstance(raw_history_runs, list)
                        or not total_count_is_valid
                        or (
                            isinstance(raw_total_count, int)
                            and not isinstance(raw_total_count, bool)
                            and raw_total_count < len(raw_history_runs)
                        )
                    ):
                        recent_history_gap = "unexpected-response"
                        collection_errors.append(
                            CollectionError(
                                "workflow-history",
                                history_endpoint,
                                "Unexpected workflow history payload shape",
                                "recent workflow history not collected",
                            )
                        )
                    else:
                        try:
                            normalized_history = [
                                self._normalize_recent_run(history_run)
                                for history_run in raw_history_runs
                            ]
                        except ValueError as exc:
                            recent_history_gap = "unexpected-response"
                            collection_errors.append(
                                CollectionError(
                                    "workflow-history",
                                    history_endpoint,
                                    f"Malformed workflow history response: {exc}",
                                    "recent workflow history not collected",
                                )
                            )
                        else:
                            recent_history_total_count = (
                                raw_total_count
                                if isinstance(raw_total_count, int)
                                and not isinstance(raw_total_count, bool)
                                else None
                            )
                            if (
                                recent_history_total_count is not None
                                and recent_history_total_count <= 10
                                and recent_history_total_count != len(normalized_history)
                            ):
                                recent_history_gap = "unexpected-response"
                                collection_errors.append(
                                    CollectionError(
                                        "workflow-history",
                                        history_endpoint,
                                        "Malformed workflow history response: total_count does not match the complete first page",
                                        "recent workflow history not collected",
                                    )
                                )
                            else:
                                normalized_history.sort(
                                    key=lambda item: (item["createdAt"], item["runId"]),
                                    reverse=True,
                                )
                                recent_history = normalized_history[:10]
                                recent_history_truncated = (
                                    recent_history_total_count > len(recent_history)
                                    if recent_history_total_count is not None
                                    else len(normalized_history) >= 10
                                )
                                source_is_in_window = any(
                                    item["runId"] == run_id
                                    for item in recent_history
                                )
                                full_history_is_in_window = (
                                    recent_history_total_count is not None
                                    and recent_history_total_count <= 10
                                ) or (
                                    recent_history_total_count is None
                                    and len(normalized_history) < 10
                                )
                                history_covers_source_run = (
                                    source_is_in_window or full_history_is_in_window
                                )
                                recent_history_collected = True
                                recent_history_gap = (
                                    ""
                                    if history_covers_source_run
                                    else "source-run-outside-bounded-window"
                                )

        evidence[evidence_id] = self._make_evidence_record(
            "workflow-run",
            _text(raw_run, "html_url") or fallback_url,
            {
                "runId": run_id,
                "targetRepository": target_repository,
                "workflowId": workflow_id_value,
                "workflow": _text(raw_run, "name"),
                "event": _text(raw_run, "event"),
                "branch": branch,
                "headSha": _text(raw_run, "head_sha"),
                "attempt": run_attempt,
                "status": _text(raw_run, "status"),
                "conclusion": _text(raw_run, "conclusion"),
                "createdAt": _text(raw_run, "created_at"),
                "updatedAt": _text(raw_run, "updated_at"),
                "runStartedAt": _text(raw_run, "run_started_at"),
                "rerunIdentity": {
                    "workflowId": workflow_id_value,
                    "event": _text(raw_run, "event"),
                    "branch": branch,
                },
                "attempts": sorted({job["attempt"] for job in job_payloads}) or [run_attempt],
                "jobs": copy.deepcopy(job_payloads),
                "totalFailedJobs": len(failed_jobs),
                "jobsTruncated": (
                    minimal
                    and len(failed_jobs) > len(selected_failed_jobs)
                ),
                "artifacts": artifacts,
                "recentHistory": recent_history,
                "recentHistoryCollected": recent_history_collected,
                "recentHistoryTruncated": recent_history_truncated,
                "recentHistoryTotalCount": recent_history_total_count,
                "historyCoversSourceRun": history_covers_source_run,
                "recentHistoryGap": recent_history_gap,
                "referencedBy": referenced_by,
            },
        )

    def _can_refresh_completed_run_history(self, record: object) -> bool:
        if not isinstance(record, dict) or record.get("availability") != "available":
            return False
        payload = record.get("payload")
        return (
            isinstance(payload, dict)
            and payload.get("status") == "completed"
            and isinstance(payload.get("workflowId"), int)
            and payload["workflowId"] > 0
            and isinstance(payload.get("branch"), str)
            and bool(payload["branch"])
            and isinstance(payload.get("createdAt"), str)
            and bool(payload["createdAt"])
        )

    def _refresh_completed_run_history(
        self,
        evidence: dict[str, dict[str, Any]],
        collection_errors: list[CollectionError],
        target_repository: str,
        run_id: int,
    ) -> None:
        evidence_id = f"run:{run_id}"
        existing = evidence.get(evidence_id)
        if not self._can_refresh_completed_run_history(existing):
            return
        record = copy.deepcopy(existing)
        payload = record["payload"]
        workflow_id = int(payload["workflowId"])
        branch = str(payload["branch"])
        endpoint = (
            f"/repos/{target_repository}/actions/workflows/{workflow_id}/runs"
            f"?branch={quote(branch, safe='')}&per_page=10"
        )
        try:
            raw_history = self._client.get(endpoint)
            if not isinstance(raw_history, dict):
                raise ValueError("Unexpected workflow history payload shape")
            raw_runs = raw_history.get("workflow_runs")
            raw_total = raw_history.get("total_count")
            if (
                not isinstance(raw_runs, list)
                or (
                    raw_total is not None
                    and (
                        not isinstance(raw_total, int)
                        or isinstance(raw_total, bool)
                        or raw_total < len(raw_runs)
                    )
                )
            ):
                raise ValueError("Unexpected workflow history payload shape")
            normalized = [self._normalize_recent_run(raw_run) for raw_run in raw_runs]
            normalized.sort(
                key=lambda item: (item["createdAt"], item["runId"]),
                reverse=True,
            )
            recent_history = normalized[:10]
            total_count = raw_total if isinstance(raw_total, int) else None
            if total_count is not None and total_count <= 10 and total_count != len(normalized):
                raise ValueError(
                    "Malformed workflow history response: total_count does not match the complete first page"
                )
            truncated = (
                total_count > len(recent_history)
                if total_count is not None
                else len(normalized) >= 10
            )
            source_in_window = any(item["runId"] == run_id for item in recent_history)
            complete_window = (
                total_count is not None and total_count <= 10
            ) or (
                total_count is None and len(normalized) < 10
            )
            covers_source = source_in_window or complete_window
        except Exception as exc:
            collection_errors.append(
                CollectionError(
                    "workflow-history",
                    endpoint,
                    str(exc),
                    "recent workflow history not collected",
                )
            )
            record["availability"] = "partial"
            payload["recentHistory"] = []
            payload["recentHistoryCollected"] = False
            payload["recentHistoryTruncated"] = False
            payload["recentHistoryTotalCount"] = None
            payload["historyCoversSourceRun"] = False
            payload["recentHistoryGap"] = (
                "request-failed"
                if not isinstance(exc, ValueError)
                else "unexpected-response"
            )
        else:
            payload["recentHistory"] = recent_history
            payload["recentHistoryCollected"] = True
            payload["recentHistoryTruncated"] = truncated
            payload["recentHistoryTotalCount"] = total_count
            payload["historyCoversSourceRun"] = covers_source
            payload["recentHistoryGap"] = (
                "" if covers_source else "source-run-outside-bounded-window"
            )
        record["collectedAt"] = self._collected_at
        evidence[evidence_id] = record

    def _normalize_workflow_job(
        self,
        raw_job: object,
        *,
        default_attempt: int,
        target_repository: str,
    ) -> dict[str, Any] | None:
        if not isinstance(raw_job, dict):
            return None
        job_id = raw_job.get("id")
        if not isinstance(job_id, int):
            return None
        attempt = raw_job.get("run_attempt")
        run_attempt = attempt if isinstance(attempt, int) and attempt > 0 else default_attempt
        return {
            "runId": int(raw_job.get("run_id", 0)) if isinstance(raw_job.get("run_id"), int) else 0,
            "targetRepository": target_repository,
            "attempt": run_attempt,
            "jobId": job_id,
            "checkRunId": _parse_trailing_int(_text(raw_job, "check_run_url")),
            "name": _text(raw_job, "name"),
            "status": _text(raw_job, "status"),
            "conclusion": _text(raw_job, "conclusion"),
            "startedAt": _text(raw_job, "started_at"),
            "completedAt": _text(raw_job, "completed_at"),
            "url": _text(raw_job, "html_url"),
            "steps": [
                {
                    "number": step.get("number"),
                    "name": _text(step, "name"),
                    "status": _text(step, "status"),
                    "conclusion": _text(step, "conclusion"),
                    "startedAt": _text(step, "started_at"),
                    "completedAt": _text(step, "completed_at"),
                }
                for step in raw_job.get("steps", [])
                if isinstance(step, dict)
            ],
        }

    def _enrich_job_annotations(
        self,
        evidence: dict[str, dict[str, Any]],
        collection_errors: list[CollectionError],
        target_repository: str,
        run_id: int,
        job_payload: dict[str, Any],
        referenced_by: list[dict[str, Any]],
    ) -> list[str]:
        check_run_id = job_payload.get("checkRunId")
        if not isinstance(check_run_id, int) or check_run_id <= 0:
            return []

        endpoint = f"/repos/{target_repository}/check-runs/{check_run_id}/annotations?per_page=100"
        annotation_ids: list[str] = []
        for index, raw_annotation in enumerate(
            self._load_paged_list(
                collection_errors,
                stage="workflow-annotation",
                endpoint=endpoint,
                key="annotations",
            ),
            start=1,
        ):
            raw_id = raw_annotation.get("id") if isinstance(raw_annotation, dict) else None
            annotation_number = raw_id if isinstance(raw_id, int) and raw_id > 0 else index
            evidence_id = f"run:{run_id}:check:{check_run_id}:annotation:{annotation_number}"
            evidence[evidence_id] = self._make_evidence_record(
                "workflow-job",
                job_payload["url"],
                {
                    "runId": run_id,
                    "targetRepository": target_repository,
                    "attempt": job_payload["attempt"],
                    "jobId": job_payload["jobId"],
                    "checkRunId": check_run_id,
                    "annotationId": annotation_number,
                    "path": _text(raw_annotation, "path"),
                    "startLine": raw_annotation.get("start_line") if isinstance(raw_annotation, dict) else None,
                    "endLine": raw_annotation.get("end_line") if isinstance(raw_annotation, dict) else None,
                    "level": _text(raw_annotation, "annotation_level"),
                    "message": _text(raw_annotation, "message"),
                    "title": _text(raw_annotation, "title"),
                    "referencedBy": referenced_by,
                },
            )
            annotation_ids.append(evidence_id)
        return annotation_ids

    def _enrich_job_log(
        self,
        evidence: dict[str, dict[str, Any]],
        collection_errors: list[CollectionError],
        target_repository: str,
        run_id: int,
        job_payload: dict[str, Any],
        referenced_by: list[dict[str, Any]],
    ) -> str | None:
        conclusion = str(job_payload.get("conclusion", "")).lower()
        if conclusion not in {"success", "failure", "timed_out", "cancelled"}:
            return None

        endpoint = f"/repos/{target_repository}/actions/jobs/{job_payload['jobId']}/logs"
        evidence_id = f"run:{run_id}:attempt:{job_payload['attempt']}:job:{job_payload['jobId']}:log"
        try:
            response = self._client.get_text(endpoint, max_bytes=200000)
        except Exception as exc:
            unavailable = _is_unavailable_log_error(exc)
            collection_errors.append(
                CollectionError(
                    "workflow-log",
                    endpoint,
                    str(exc),
                    "workflow-log evidence unavailable" if unavailable else None,
                )
            )
            evidence[evidence_id] = self._make_partial_record(
                "workflow-log",
                job_payload["url"],
                {
                    "evidenceId": evidence_id,
                    "runId": run_id,
                    "attempt": job_payload["attempt"],
                    "jobId": job_payload["jobId"],
                    "targetRepository": target_repository,
                    "referencedBy": referenced_by,
                },
                exc,
                availability="expired-or-unavailable" if unavailable else "partial",
            )
            return evidence_id

        evidence[evidence_id] = self._make_evidence_record(
            "workflow-log",
            job_payload["url"],
            {
                "evidenceId": evidence_id,
                "runId": run_id,
                "attempt": job_payload["attempt"],
                "jobId": job_payload["jobId"],
                "targetRepository": target_repository,
                "excerpt": response.text,
                "facts": self._extract_facts(response.text, evidence_id),
                "truncated": bool(getattr(response, "truncated", False)),
                "status": getattr(response, "status", 0),
                "referencedBy": referenced_by,
            },
        )
        return evidence_id

    def _enrich_retry_test_results(
        self,
        evidence: dict[str, dict[str, Any]],
        collection_errors: list[CollectionError],
        target_repository: str,
        run_id: int,
        raw_artifacts: list[Any],
        jobs: list[dict[str, Any]],
        referenced_by: list[dict[str, Any]],
    ) -> None:
            final_jobs = [
                job
                for job in jobs
                if isinstance(job.get("name"), str)
                and (
                    self._repository_policy.retry_test_results.matches_aggregate_job(
                        job["name"]
                    )
                )
            ]
            artifacts_by_attempt: list[tuple[int, dict[str, Any]]] = []
            for raw_artifact in raw_artifacts:
                if (
                    not isinstance(raw_artifact, dict)
                    or not self._repository_policy.retry_test_results.matches_artifact(
                        _text(raw_artifact, "name")
                    )
                    or bool(raw_artifact.get("expired", False))
                ):
                    continue
                artifact_id = raw_artifact.get("id")
                created_at = _text(raw_artifact, "created_at")
                if (
                    not isinstance(artifact_id, int)
                    or isinstance(artifact_id, bool)
                    or artifact_id < 1
                    or not created_at
                ):
                    continue
                try:
                    created = _parse_timestamp(created_at)
                    matching_jobs = [
                        job
                        for job in final_jobs
                        if _job_contains_timestamp(job, created)
                    ]
                except ValueError:
                    continue
                if len(matching_jobs) != 1:
                    continue
                attempt = matching_jobs[0].get("attempt")
                if isinstance(attempt, int) and not isinstance(attempt, bool):
                    artifacts_by_attempt.append((attempt, raw_artifact))

            parsed_by_attempt: list[
                tuple[int, dict[str, Any], list[dict[str, str]]]
            ] = []
            for attempt, artifact in sorted(
                artifacts_by_attempt,
                key=lambda item: item[0],
                reverse=True,
            )[:3]:
                artifact_id = int(artifact["id"])
                endpoint = (
                    f"/repos/{target_repository}/actions/artifacts/{artifact_id}/zip"
                )
                try:
                    content = self._client.get_bytes(
                        endpoint,
                        max_bytes=_MAX_TEST_RESULTS_ARTIFACT_BYTES,
                    )
                    expected_digest = _text(artifact, "digest")
                    actual_digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
                    if expected_digest != actual_digest:
                        raise ValueError(
                            "Test-results artifact digest does not match metadata."
                        )
                    parsed = parse_test_results_archive(content)
                except (RuntimeError, ValueError) as exc:
                    collection_errors.append(
                        CollectionError(
                            "workflow-test-results",
                            endpoint,
                            str(exc),
                            "test-results artifact unavailable",
                        )
                    )
                    continue
                parsed_by_attempt.append((attempt, artifact, parsed))

            failed_names = {
                result["testName"]
                for _, _, results in parsed_by_attempt
                for result in results
                if result["outcome"] == "Failed"
            }
            for attempt, artifact, results in parsed_by_attempt:
                results_by_job: dict[int, list[dict[str, str]]] = defaultdict(list)
                for result in results:
                    if (
                        result["outcome"] != "Failed"
                        and result["testName"] not in failed_names
                    ):
                        continue
                    matching_jobs = [
                        job
                        for job in jobs
                        if job.get("attempt") == attempt
                        and _job_matches_test_result(job, result)
                    ]
                    if len(matching_jobs) == 1:
                        results_by_job[int(matching_jobs[0]["jobId"])].append(result)

                for job in jobs:
                    job_id = int(job["jobId"])
                    matching_results = results_by_job.get(job_id, [])
                    if not matching_results:
                        continue
                    evidence_id = (
                        f"run:{run_id}:attempt:{attempt}:job:{job_id}:test-results"
                    )
                    artifact_id = int(artifact["id"])
                    evidence[evidence_id] = self._make_evidence_record(
                        "workflow-test-results",
                        (
                            f"https://github.com/{target_repository}/actions/runs/"
                            f"{run_id}/artifacts/{artifact_id}"
                        ),
                        {
                            "evidenceId": evidence_id,
                            "runId": run_id,
                            "attempt": attempt,
                            "jobId": job_id,
                            "targetRepository": target_repository,
                            "artifactId": artifact_id,
                            "artifactName": _text(artifact, "name"),
                            "artifactDigest": artifact["digest"],
                            "results": matching_results,
                            "tests": [
                                {
                                    "testName": result["testName"],
                                    "outcome": result["outcome"].lower(),
                                }
                                for result in matching_results
                            ],
                            "referencedBy": referenced_by,
                        },
                    )
                    job["testResultsEvidenceId"] = evidence_id
                    job_evidence_id = (
                        f"run:{run_id}:attempt:{attempt}:job:{job_id}"
                    )
                    evidence[job_evidence_id]["payload"][
                        "testResultsEvidenceId"
                    ] = evidence_id

    def _normalize_recent_run(self, raw_run: object) -> dict[str, Any]:
        if not isinstance(raw_run, dict):
            raise ValueError("workflow_runs entries must be objects")
        run_id = raw_run.get("id")
        created_at = _text(raw_run, "created_at")
        if (
            not isinstance(run_id, int)
            or isinstance(run_id, bool)
            or run_id <= 0
            or not created_at
        ):
            raise ValueError("workflow_runs entries require positive integer id and created_at")
        try:
            _parse_timestamp(created_at)
        except ValueError as exc:
            raise ValueError("workflow_runs entries require a valid created_at timestamp") from exc
        attempt = raw_run.get("run_attempt")
        return {
            "runId": run_id,
            "attempt": attempt if isinstance(attempt, int) else 0,
            "event": _text(raw_run, "event"),
            "branch": _text(raw_run, "head_branch"),
            "headSha": _text(raw_run, "head_sha"),
            "conclusion": _text(raw_run, "conclusion"),
            "createdAt": created_at,
            "url": _text(raw_run, "html_url"),
        }

    def _load_paged_list(
        self,
        collection_errors: list[CollectionError],
        *,
        stage: str,
        endpoint: str,
        key: str,
    ) -> list[dict[str, Any]]:
        try:
            payload = self._client.get_pages(endpoint, key=key)
        except Exception as exc:
            collection_errors.append(CollectionError(stage, endpoint, str(exc)))
            return []
        return _paged_dict_items(payload, key)

    def _make_partial_record(
        self,
        kind: str,
        url: str,
        payload: dict[str, Any],
        exc: Exception,
        *,
        availability: str = "partial",
    ) -> dict[str, Any]:
        record = self._make_evidence_record(kind, url, dict(payload))
        record["availability"] = availability
        record["payload"]["errorCategory"] = _error_category(exc)
        record["payload"]["errorMessage"] = str(exc)
        return record


def _has_primary_inventory_reference(refs: list[dict[str, Any]]) -> bool:
    return any(
        isinstance(ref, dict) and ref.get("extractionMethod") == "primary-inventory"
        for ref in refs
    )


def _is_bot_authored(raw_issue: dict[str, Any]) -> bool:
    """Report whether GitHub attributes this item to an app rather than a person.

    `user.type` is `"Bot"` for every GitHub App author (`github-actions[bot]`,
    `dependabot[bot]`, `Copilot`, and any repository-specific app), `"User"`
    for people, and `"Organization"`/`"Mannequin"` for the remaining cases.
    """
    user = raw_issue.get("user")
    if not isinstance(user, dict):
        return False
    user_type = user.get("type")
    return isinstance(user_type, str) and user_type.casefold() == "bot"


def _repository_scoped_evidence_id(
    kind: str,
    target_repository: str,
    primary_repository: str,
    identifier: object,
) -> str:
    if _same_repository(target_repository, primary_repository):
        return f"{kind}:{identifier}"
    return f"{kind}:{target_repository}:{identifier}"


def _same_repository(left: str, right: str) -> bool:
    return left.casefold() == right.casefold()


def _extract_labels(raw_issue: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for raw_label in raw_issue.get("labels", []):
        if isinstance(raw_label, dict):
            name = raw_label.get("name")
        else:
            name = raw_label
        if isinstance(name, str) and name:
            labels.append(name)
    return sorted(set(labels))


def _text(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    return value if isinstance(value, str) else ""


def _nested_text(mapping: dict[str, Any], keys: tuple[str, ...]) -> str:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return current if isinstance(current, str) else ""


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _job_contains_timestamp(job: dict[str, Any], value: datetime) -> bool:
    started_at = job.get("startedAt")
    completed_at = job.get("completedAt")
    if not isinstance(started_at, str) or not isinstance(completed_at, str):
        return False
    return _parse_timestamp(started_at) <= value <= _parse_timestamp(completed_at)


def _job_matches_test_result(
    job: dict[str, Any],
    result: dict[str, str],
) -> bool:
    name = job.get("name")
    if not isinstance(name, str):
        return False
    runner_match = re.search(r"\((?P<runner>[^()]+)\)\s*$", name)
    if runner_match is None:
        return False
    runner = runner_match.group("runner").strip()
    without_runner = name[:runner_match.start()].strip()
    parts = [part.strip() for part in without_runner.split("/") if part.strip()]
    lane = parts[-1] if parts else without_runner
    artifact_os = result["os"]
    return (
        lane == result["lane"]
        and (runner == artifact_os or runner.endswith(f"-{artifact_os}"))
    )


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _join_issue_text(title: str, body: str) -> str:
    return "\n".join(part for part in (title.strip(), body.strip()) if part)


def _issue_lifecycle_metadata(
    *,
    title: str,
    labels: object,
    body: str,
    body_markers: list[dict[str, Any]],
    issue_signals: Any,
    comments: list[tuple[Any, ...]],
    comments_complete: bool,
    episodes_complete: bool,
) -> dict[str, Any]:
    label_names = (
        {str(label) for label in labels if isinstance(label, str)}
        if isinstance(labels, list)
        else set()
    )
    marker_keys = {
        str(marker.get("key"))
        for marker in body_markers
        if isinstance(marker, dict)
    }
    if "ci-failure-cause" in marker_keys:
        producer = "ci-failure-cause"
    elif "_Filed from the CI Health dashboard._" in body:
        producer = "ci-health-dashboard"
    elif (
        "gh-aw-failure-issue" in marker_keys
        or (
            "gh-aw-agentic-workflow" in marker_keys
            and re.search(r"(?i)^\[aw\].*\bfailed\b", title) is not None
        )
    ):
        producer = "gh-aw-failure-issue"
    elif (
        "automation-broken" in label_names
        and marker_keys.intersection(
            {"automation-broken", "autoclose", "ci-failure", "gh-aw-agentic-workflow"}
        )
    ):
        producer = "tracking-issue"
    else:
        producer = "unknown"

    autoclose_values = {
        str(marker.get("normalized")).lower()
        for marker in body_markers
        if marker.get("key") == "autoclose"
        and str(marker.get("normalized")).lower() in {"true", "false"}
    }
    autoclose = (
        next(iter(autoclose_values)) == "true"
        if len(autoclose_values) == 1
        else None
    )

    if producer == "ci-failure-cause":
        ledger = issue_signals.occurrence_ledger.as_record()
        ledger["rows"] = [
            occurrence.as_record()
            for occurrence in issue_signals.occurrences
        ]
    elif producer == "tracking-issue":
        rows: list[dict[str, Any]] = []
        source_record_count = 0
        parsed_row_count = 0
        for entry in comments:
            _, comment, markers, *_ = entry
            for marker in markers:
                if marker.get("key") != "run":
                    continue
                source_record_count += 1
                raw_run_id = str(marker.get("raw", "")).strip()
                if (
                    not raw_run_id.isdigit()
                    or raw_run_id.startswith("0")
                    or len(raw_run_id) > 20
                ):
                    continue
                parsed_row_count += 1
                rows.append(
                    {
                        "commentId": int(comment["id"]),
                        "createdAt": comment["createdAt"],
                        "runId": int(raw_run_id),
                    }
                )
        rows.sort(key=lambda row: (row["createdAt"], row["runId"], row["commentId"]))
        ledger = {
            "source": "run-comments",
            "schema": "tracking-comments-v1",
            "schemaRecognized": True,
            "sourceRecordCount": source_record_count,
            "parsedRowCount": parsed_row_count,
            "complete": (
                comments_complete
                and source_record_count > 0
                and parsed_row_count == source_record_count
            ),
            "rows": rows,
        }
    else:
        ledger = {
            "source": "none",
            "schema": None,
            "schemaRecognized": False,
            "sourceRecordCount": 0,
            "parsedRowCount": 0,
            "complete": False,
            "rows": [],
        }

    return {
        "producer": producer,
        "autoclose": autoclose,
        "ledger": ledger,
        "episodesComplete": episodes_complete,
    }


def _closed_sort_value(issue: dict[str, Any]) -> float:
    closed_at = issue.get("closedAt")
    if isinstance(closed_at, str) and closed_at:
        try:
            return -_parse_timestamp(closed_at).timestamp()
        except ValueError:
            pass
    return float("inf")


def _issue_number_from_evidence_id(evidence_id: str) -> int | None:
    parts = evidence_id.split(":")
    if len(parts) >= 2 and parts[0] == "issue" and parts[1].isdigit():
        return int(parts[1])
    return None


def _normalize_referenced_by(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = [
        {
            "sourceIssueNumber": int(ref["sourceIssueNumber"]),
            "sourceEvidenceId": str(ref["sourceEvidenceId"]),
            "sourceUrl": str(ref["sourceUrl"]),
            "extractionMethod": str(ref["extractionMethod"]),
        }
        for ref in refs
    ]
    return _sorted_unique_records(
        normalized,
        ("sourceIssueNumber", "sourceEvidenceId", "sourceUrl", "extractionMethod"),
    )


def _record_supporting_reference_exclusion(
    reference: dict[str, Any],
    root_issue_numbers: set[int],
    *,
    reason: str,
) -> None:
    selection = reference.get("supportingSelection")
    if not isinstance(selection, dict) or selection.get("state") != "excluded":
        selection = {
            "state": "excluded",
            "reasons": [],
            "rootIssueNumbers": [],
        }
        reference["supportingSelection"] = selection

    existing_reasons = selection.get("reasons")
    reasons = {
        item
        for item in (existing_reasons if isinstance(existing_reasons, list) else [])
        if isinstance(item, str) and item
    }
    reasons.add(reason)
    selection["reasons"] = sorted(reasons)

    existing_roots = selection.get("rootIssueNumbers")
    roots = {
        item
        for item in (existing_roots if isinstance(existing_roots, list) else [])
        if isinstance(item, int) and not isinstance(item, bool) and item > 0
    }
    roots.update(root_issue_numbers)
    selection["rootIssueNumbers"] = sorted(roots)


def _supporting_reference_exclusion_reasons(reference: dict[str, Any]) -> set[str]:
    selection = reference.get("supportingSelection")
    if not isinstance(selection, dict) or selection.get("state") != "excluded":
        return set()
    reasons = selection.get("reasons")
    if not isinstance(reasons, list):
        return set()
    return {reason for reason in reasons if isinstance(reason, str) and reason}


def _all_supporting_references_excluded(refs: list[dict[str, Any]]) -> bool:
    return bool(refs) and all(_supporting_reference_exclusion_reasons(ref) for ref in refs)


def _merge_referenced_by(
    existing_record: dict[str, Any] | None,
    refs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    existing: list[dict[str, Any]] = []
    if isinstance(existing_record, dict):
        payload = existing_record.get("payload")
        if isinstance(payload, dict):
            referenced_by = payload.get("referencedBy")
            if isinstance(referenced_by, list):
                existing = [item for item in referenced_by if isinstance(item, dict)]
    return _sorted_unique_records(
        existing + _normalize_referenced_by(refs),
        ("sourceIssueNumber", "sourceEvidenceId", "sourceUrl", "extractionMethod"),
    )


def _merge_issue_evidence_payload(
    existing_record: dict[str, Any] | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(existing_record, dict):
        return payload

    existing_payload = existing_record.get("payload")
    if not isinstance(existing_payload, dict):
        return payload

    merged = {**copy.deepcopy(existing_payload), **payload}
    existing_labels = existing_payload.get("labels")
    payload_labels = payload.get("labels")
    if isinstance(existing_labels, list) or isinstance(payload_labels, list):
        labels = [
            label
            for labels_value in (existing_labels, payload_labels)
            if isinstance(labels_value, list)
            for label in labels_value
            if isinstance(label, str) and label
        ]
        merged["labels"] = sorted(set(labels))

    for transient_key in ("supportingBudgetExcluded", "errorCategory", "errorMessage"):
        merged.pop(transient_key, None)
    return merged


def _reference_target_url(refs: list[dict[str, Any]], fallback: str) -> str:
    for ref in refs:
        target_url = ref.get("targetUrl")
        if isinstance(target_url, str) and target_url:
            return target_url
    return fallback


def _paged_dict_items(payload: object, key: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _normalize_comments(raw_comments: object) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    if isinstance(raw_comments, list):
        for raw_comment in raw_comments:
            if not isinstance(raw_comment, dict):
                continue
            comment_id = raw_comment.get("id")
            if not isinstance(comment_id, int) or isinstance(comment_id, bool):
                continue
            comments.append(
                {
                    "id": comment_id,
                    "url": _text(raw_comment, "html_url"),
                    "createdAt": _text(raw_comment, "created_at"),
                    "updatedAt": _text(raw_comment, "updated_at"),
                    "author": _nested_text(raw_comment, ("user", "login")),
                    "body": _text(raw_comment, "body"),
                }
            )
    comments.sort(key=lambda item: (item["createdAt"], item["id"]))
    return comments


def _parse_trailing_int(value: str) -> int | None:
    tail = value.rstrip("/").rsplit("/", 1)[-1]
    if tail.isdigit():
        return int(tail)
    return None


def _error_category(exc: Exception) -> str:
    category = getattr(exc, "category", None)
    if isinstance(category, str) and category:
        return category
    return "generic"


def _is_unavailable_log_error(exc: Exception) -> bool:
    category = _error_category(exc)
    status = getattr(exc, "status", None)
    return category in {"not-found", "expired"} or status == 404


def _is_log_eligible_conclusion(conclusion: object) -> bool:
    return str(conclusion).lower() in {"failure", "timed_out", "cancelled"}


def _unique_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def _sorted_unique_records(records: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    unique: list[dict[str, Any]] = []
    for record in sorted(records, key=lambda item: tuple(item.get(key) for key in keys)):
        marker = tuple(record.get(key) for key in keys)
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(record)
    return unique
