from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlencode

from . import ownership
from .collector import CollectionError, Collector, _merge_referenced_by, _parse_timestamp
from .models import validate_evidence_requests, validate_snapshot


_ISSUE_ID_RE = re.compile(
    r"^issue:(?:(?P<repository>[^:]+/[^:]+):)?(?P<number>[1-9][0-9]*)$"
)
_PR_ID_RE = re.compile(
    r"^pr:(?:(?P<repository>[^:]+/[^:]+):)?(?P<number>[1-9][0-9]*)$"
)
_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_RENAME_CANDIDATE_LIMIT = 20


@dataclass(slots=True)
class _RequestOutcome:
    complete: bool
    errors: list[dict[str, Any]]


class AdaptiveEnricher:
    def __init__(
        self,
        client: Any,
        *,
        now: datetime | Callable[[], datetime] | None = None,
        checkout: Path | str | None = None,
        git_runner: Any = subprocess.run,
        git_timeout_seconds: int = 10,
    ) -> None:
        self._client = client
        self._now = now
        self._checkout = Path(checkout) if checkout is not None else None
        self._git_runner = git_runner
        self._git_timeout_seconds = git_timeout_seconds
        self.errors: list[dict[str, Any]] = []

    def expand(
        self,
        snapshot: object,
        request_document: object,
    ) -> dict[str, Any]:
        normalized_requests = validate_evidence_requests(snapshot, request_document)
        baseline = copy.deepcopy(snapshot)
        if not isinstance(baseline, dict):
            raise TypeError("Validated snapshot must be an object.")
        request_mapping = request_document
        if not isinstance(request_mapping, Mapping):
            raise TypeError("Validated request document must be an object.")

        repository = str(baseline["repository"])
        collected_at = self._now_value()
        collector = Collector(self._client, repository, collected_at)
        evidence = baseline["evidence"]
        if not isinstance(evidence, dict):
            raise TypeError("Validated snapshot evidence must be an object.")

        expansion_errors: list[dict[str, Any]] = []
        all_complete = True
        for request in normalized_requests:
            try:
                if request["type"] == "issue-reference":
                    outcome = self._expand_issue_reference(
                        evidence,
                        collector,
                        repository,
                        request,
                    )
                elif request["type"] == "workflow-run":
                    outcome = self._expand_workflow_run(
                        evidence,
                        collector,
                        repository,
                        request,
                    )
                elif request["type"] == "canonical-search":
                    outcome = self._expand_canonical_search(
                        evidence,
                        repository,
                        collected_at,
                        request,
                    )
                else:
                    outcome = self._expand_source_check(
                        evidence,
                        repository,
                        collected_at,
                        request,
                    )
            except Exception as exc:
                outcome = _RequestOutcome(
                    complete=False,
                    errors=[
                        self._error(
                            request,
                            stage="adaptive-expansion",
                            endpoint=request["evidenceId"],
                            message=str(exc),
                            effect="requested evidence was not expanded",
                        )
                    ],
                )
            expansion_errors.extend(outcome.errors)
            all_complete = all_complete and outcome.complete

        baseline["evidence"] = dict(sorted(evidence.items()))
        if not isinstance(baseline.get("collectionErrors"), list):
            raise TypeError("Validated snapshot collectionErrors must be a list.")
        expansions = baseline.setdefault("expansions", [])
        if not isinstance(expansions, list):
            raise TypeError("Validated snapshot expansions must be a list.")
        manifest = {
            "round": int(request_mapping["round"]),
            "requests": copy.deepcopy(normalized_requests),
            "status": "complete" if all_complete else "partial",
            "errors": copy.deepcopy(expansion_errors),
        }
        expansions.append(manifest)
        self.errors = copy.deepcopy(expansion_errors)
        validate_snapshot(baseline)
        return baseline

    def _expand_issue_reference(
        self,
        evidence: dict[str, dict[str, Any]],
        collector: Collector,
        repository: str,
        request: dict[str, Any],
    ) -> _RequestOutcome:
        evidence_id = request["evidenceId"]
        existing_record = evidence[evidence_id]
        identity = self._issue_identity(evidence_id, repository)
        if identity is None:
            raise ValueError(f"Unsupported issue-reference evidence ID: {evidence_id}")
        target_type, target_repository, target_number = identity
        refs = self._target_references(
            existing_record,
            request,
            source_repository=repository,
            target_type=target_type,
            target_repository=target_repository,
            target_number=target_number,
        )
        expanded_records: dict[str, dict[str, Any]] = {}
        collection_errors: list[CollectionError] = []
        if target_type == "pull-request":
            collector._enrich_pull_request_reference(  # noqa: SLF001 - package-level reuse
                expanded_records,
                collection_errors,
                target_repository,
                target_number,
                refs,
            )
        else:
            collector._enrich_issue_reference(  # noqa: SLF001 - package-level reuse
                expanded_records,
                collection_errors,
                target_repository,
                target_number,
                refs,
            )

        existing_payload = existing_record.get("payload")
        baseline_references = (
            existing_payload.get("referencedBy")
            if isinstance(existing_payload, Mapping)
            else None
        )
        if isinstance(baseline_references, list):
            valid_baseline_references = [
                reference
                for reference in baseline_references
                if isinstance(reference, dict)
            ]
            for expanded_record in expanded_records.values():
                expanded_payload = expanded_record.get("payload")
                if isinstance(expanded_payload, dict):
                    expanded_payload["referencedBy"] = _merge_referenced_by(
                        expanded_record,
                        valid_baseline_references,
                    )

        errors = [
            self._collection_error(request, error)
            for error in collection_errors
        ]
        available = any(
            record.get("availability") == "available"
            for record in expanded_records.values()
        )
        if available:
            requested_identity_is_present = evidence_id in expanded_records
            if not requested_identity_is_present:
                evidence.pop(evidence_id, None)
            evidence.update(expanded_records)
        else:
            for expanded_id, expanded_record in expanded_records.items():
                if expanded_id != evidence_id and expanded_id not in evidence:
                    evidence[expanded_id] = expanded_record
        return _RequestOutcome(complete=available and not errors, errors=errors)

    def _expand_workflow_run(
        self,
        evidence: dict[str, dict[str, Any]],
        collector: Collector,
        repository: str,
        request: dict[str, Any],
    ) -> _RequestOutcome:
        evidence_id = request["evidenceId"]
        existing_record = evidence[evidence_id]
        baseline_record = copy.deepcopy(existing_record)
        baseline_children = {
            child_id: copy.deepcopy(child_record)
            for child_id, child_record in evidence.items()
            if child_id.startswith(f"{evidence_id}:")
        }
        payload = existing_record["payload"]
        target_repository = payload.get("targetRepository", repository)
        if not isinstance(target_repository, str) or not target_repository:
            raise ValueError("Workflow run evidence is missing its target repository.")
        run_id = int(evidence_id.split(":", 1)[1])
        refs = self._target_references(
            existing_record,
            request,
            source_repository=repository,
            target_type="workflow-run",
            target_repository=target_repository,
            run_id=run_id,
        )
        collection_errors: list[CollectionError] = []
        collector._enrich_workflow_run_reference(  # noqa: SLF001 - package-level reuse
            evidence,
            collection_errors,
            target_repository,
            run_id,
            refs,
            minimal=True,
            include_history=True,
        )
        errors = [
            self._collection_error(request, error)
            for error in collection_errors
        ]
        run_record = evidence.get(evidence_id)
        if (
            isinstance(run_record, dict)
            and run_record.get("availability") != "available"
        ):
            evidence[evidence_id] = baseline_record
            run_record = baseline_record
        elif isinstance(run_record, dict):
            run_record = self._merge_workflow_run_record(
                baseline_record,
                run_record,
                collection_errors,
            )
            evidence[evidence_id] = run_record
            self._merge_workflow_child_records(
                evidence,
                baseline_children,
            )
        run_payload = (
            run_record.get("payload")
            if isinstance(run_record, dict)
            else None
        )
        complete = (
            isinstance(run_record, dict)
            and run_record.get("availability") == "available"
            and isinstance(run_payload, dict)
            and run_payload.get("recentHistoryCollected") is True
            and run_payload.get("historyCoversSourceRun") is True
            and not errors
        )
        return _RequestOutcome(complete=complete, errors=errors)

    @staticmethod
    def _merge_workflow_run_record(
        baseline_record: Mapping[str, Any],
        current_record: Mapping[str, Any],
        collection_errors: list[CollectionError],
    ) -> dict[str, Any]:
        merged_record = copy.deepcopy(dict(current_record))
        baseline_payload = baseline_record.get("payload")
        current_payload = current_record.get("payload")
        if not isinstance(baseline_payload, Mapping) or not isinstance(current_payload, Mapping):
            return merged_record

        merged_payload = copy.deepcopy(dict(current_payload))
        current_references = current_payload.get("referencedBy")
        merged_payload["referencedBy"] = _merge_referenced_by(
            copy.deepcopy(dict(baseline_record)),
            [
                copy.deepcopy(reference)
                for reference in current_references
                if isinstance(reference, dict)
            ]
            if isinstance(current_references, list)
            else [],
        )
        error_stages = {error.stage for error in collection_errors}

        job_fields = ("attempts", "jobs", "totalFailedJobs", "jobsTruncated")
        if "workflow-jobs" in error_stages:
            for field_name in job_fields:
                if field_name in baseline_payload:
                    merged_payload[field_name] = copy.deepcopy(baseline_payload[field_name])
        else:
            AdaptiveEnricher._merge_workflow_jobs(
                merged_payload,
                baseline_payload,
                current_payload,
            )

        baseline_artifacts = baseline_payload.get("artifacts")
        current_artifacts = current_payload.get("artifacts")
        if (
            isinstance(baseline_artifacts, list)
            and baseline_artifacts
            and (not isinstance(current_artifacts, list) or not current_artifacts)
        ):
            merged_payload["artifacts"] = copy.deepcopy(baseline_artifacts)

        history_fields = (
            "recentHistory",
            "recentHistoryCollected",
            "recentHistoryTruncated",
            "recentHistoryTotalCount",
            "historyCoversSourceRun",
            "recentHistoryGap",
        )
        if (
            "workflow-history" in error_stages
            or AdaptiveEnricher._workflow_history_completeness(current_payload)
            < AdaptiveEnricher._workflow_history_completeness(baseline_payload)
        ):
            for field_name in history_fields:
                if field_name in baseline_payload:
                    merged_payload[field_name] = copy.deepcopy(baseline_payload[field_name])

        merged_record["payload"] = merged_payload
        return merged_record

    @staticmethod
    def _merge_workflow_jobs(
        merged_payload: dict[str, Any],
        baseline_payload: Mapping[str, Any],
        current_payload: Mapping[str, Any],
    ) -> None:
        jobs_by_key: dict[tuple[int, int], dict[str, Any]] = {}
        for jobs in (baseline_payload.get("jobs"), current_payload.get("jobs")):
            if not isinstance(jobs, list):
                continue
            for job in jobs:
                if not isinstance(job, Mapping):
                    continue
                attempt = job.get("attempt")
                job_id = job.get("jobId")
                if (
                    not isinstance(attempt, int)
                    or isinstance(attempt, bool)
                    or attempt <= 0
                    or not isinstance(job_id, int)
                    or isinstance(job_id, bool)
                    or job_id <= 0
                ):
                    continue
                key = (attempt, job_id)
                previous = jobs_by_key.get(key)
                merged_job = (
                    {**copy.deepcopy(previous), **copy.deepcopy(dict(job))}
                    if previous is not None
                    else copy.deepcopy(dict(job))
                )
                if previous is not None:
                    annotation_ids = sorted(
                        {
                            annotation_id
                            for payload in (previous, job)
                            for annotation_id in payload.get("annotationEvidenceIds", [])
                            if isinstance(payload.get("annotationEvidenceIds"), list)
                            and isinstance(annotation_id, str)
                            and annotation_id
                        }
                    )
                    if annotation_ids:
                        merged_job["annotationEvidenceIds"] = annotation_ids
                    references = job.get("referencedBy")
                    merged_job["referencedBy"] = _merge_referenced_by(
                        {"payload": {"referencedBy": previous.get("referencedBy", [])}},
                        [
                            copy.deepcopy(reference)
                            for reference in references
                            if isinstance(reference, dict)
                        ]
                        if isinstance(references, list)
                        else [],
                    )
                jobs_by_key[key] = merged_job

        merged_jobs = [job for _, job in sorted(jobs_by_key.items())]
        merged_payload["jobs"] = merged_jobs
        attempts = {
            attempt
            for payload in (baseline_payload, current_payload)
            for attempt in payload.get("attempts", [])
            if isinstance(payload.get("attempts"), list)
            and isinstance(attempt, int)
            and not isinstance(attempt, bool)
            and attempt > 0
        }
        attempts.update(job["attempt"] for job in merged_jobs)
        merged_payload["attempts"] = sorted(attempts)

        failed_conclusions = {
            "action_required",
            "failure",
            "startup_failure",
            "timed_out",
        }
        represented_failed_jobs = sum(
            job.get("conclusion") in failed_conclusions
            for job in merged_jobs
        )
        reported_failed_jobs = [
            value
            for value in (
                baseline_payload.get("totalFailedJobs"),
                current_payload.get("totalFailedJobs"),
            )
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        ]
        total_failed_jobs = max(
            [represented_failed_jobs, *reported_failed_jobs]
        )
        merged_payload["totalFailedJobs"] = total_failed_jobs

        baseline_truncated = baseline_payload.get("jobsTruncated")
        current_truncated = current_payload.get("jobsTruncated")
        merged_payload["jobsTruncated"] = (
            total_failed_jobs > represented_failed_jobs
            if reported_failed_jobs
            else baseline_truncated is True or current_truncated is True
        )

    @staticmethod
    def _workflow_history_completeness(payload: Mapping[str, Any]) -> tuple[int, ...]:
        history = payload.get("recentHistory")
        collected = payload.get("recentHistoryCollected") is True
        return (
            int(collected),
            int(collected and payload.get("historyCoversSourceRun") is True),
            int(collected and payload.get("recentHistoryTruncated") is False),
            len(history) if collected and isinstance(history, list) else 0,
            int(
                collected
                and isinstance(payload.get("recentHistoryTotalCount"), int)
                and not isinstance(payload.get("recentHistoryTotalCount"), bool)
            ),
        )

    @staticmethod
    def _merge_workflow_child_records(
        evidence: dict[str, dict[str, Any]],
        baseline_children: Mapping[str, Mapping[str, Any]],
    ) -> None:
        availability_rank = {
            "not-enriched": 0,
            "expired-or-unavailable": 1,
            "partial": 2,
            "available": 3,
        }
        for evidence_id, baseline_record in baseline_children.items():
            current_record = evidence.get(evidence_id)
            if not isinstance(current_record, Mapping):
                evidence[evidence_id] = copy.deepcopy(dict(baseline_record))
                continue

            baseline_availability = baseline_record.get("availability")
            current_availability = current_record.get("availability")
            use_current = (
                current_availability == "available"
                or availability_rank.get(str(current_availability), -1)
                > availability_rank.get(str(baseline_availability), -1)
            )
            preferred = current_record if use_current else baseline_record
            merged_record = copy.deepcopy(dict(preferred))
            baseline_payload = baseline_record.get("payload")
            current_payload = current_record.get("payload")
            preferred_payload = preferred.get("payload")
            if not isinstance(preferred_payload, Mapping):
                evidence[evidence_id] = merged_record
                continue

            merged_payload = copy.deepcopy(dict(preferred_payload))
            if (
                preferred.get("kind") == "workflow-job"
                and isinstance(baseline_payload, Mapping)
                and isinstance(current_payload, Mapping)
            ):
                merged_payload = {
                    **copy.deepcopy(dict(baseline_payload)),
                    **copy.deepcopy(dict(current_payload)),
                }
                annotation_ids = sorted(
                    {
                        annotation_id
                        for payload in (baseline_payload, current_payload)
                        for annotation_id in payload.get("annotationEvidenceIds", [])
                        if isinstance(payload.get("annotationEvidenceIds"), list)
                        and isinstance(annotation_id, str)
                        and annotation_id
                    }
                )
                if annotation_ids:
                    merged_payload["annotationEvidenceIds"] = annotation_ids

            if isinstance(baseline_payload, Mapping):
                current_references = (
                    current_payload.get("referencedBy")
                    if isinstance(current_payload, Mapping)
                    else None
                )
                merged_payload["referencedBy"] = _merge_referenced_by(
                    {"payload": copy.deepcopy(dict(baseline_payload))},
                    [
                        copy.deepcopy(reference)
                        for reference in current_references
                        if isinstance(reference, dict)
                    ]
                    if isinstance(current_references, list)
                    else [],
                )
            merged_record["payload"] = merged_payload
            evidence[evidence_id] = merged_record

    def _expand_canonical_search(
        self,
        evidence: dict[str, dict[str, Any]],
        repository: str,
        collected_at: datetime,
        request: dict[str, Any],
    ) -> _RequestOutcome:
        query_fact = {
            "field": request["factField"],
            "value": request["factValue"],
            "normalized": request["factNormalized"],
        }
        source_record = evidence[request["evidenceId"]]
        source_payload = source_record["payload"]
        query_value = self._quote_search_value(request["factValue"])
        query = f'repo:{repository} is:issue "{query_value}"'
        endpoint = f"/search/issues?{urlencode({'q': query, 'per_page': 20, 'page': 1})}"
        empty_search = {
            "mode": "adaptive-canonical-search",
            "queryFact": query_fact,
            "totalCount": None,
            "returnedCount": 0,
            "candidateIssueNumbers": [],
            "truncated": False,
            "complete": False,
        }

        try:
            response = self._client.get(endpoint)
            normalized_issues, total_count, incomplete_results = self._normalize_search_response(
                response,
                repository,
                request,
            )
        except Exception as exc:
            self._record_canonical_search(source_payload, empty_search)
            return _RequestOutcome(
                complete=False,
                errors=[
                    self._error(
                        request,
                        stage="canonical-search",
                        endpoint=endpoint,
                        message=str(exc),
                        effect="canonical search remains incomplete",
                    )
                ],
            )

        association_records = self._associations_for_request(
            source_record,
            request,
            repository,
        )
        candidate_numbers: list[int] = []
        for issue_number, issue_payload, issue_url in normalized_issues:
            candidate_numbers.append(issue_number)
            issue_payload["referencedBy"] = copy.deepcopy(association_records)
            self._merge_search_issue(
                evidence,
                issue_number,
                issue_url,
                issue_payload,
                collected_at,
            )

        returned_count = len(normalized_issues)
        complete = (
            total_count <= 20
            and returned_count == total_count
            and not incomplete_results
        )
        truncated = incomplete_results or total_count > returned_count
        search = {
            **empty_search,
            "totalCount": total_count,
            "returnedCount": returned_count,
            "candidateIssueNumbers": sorted(candidate_numbers),
            "truncated": truncated,
            "complete": complete,
        }
        current_source_record = evidence[request["evidenceId"]]
        current_source_payload = current_source_record["payload"]
        self._record_canonical_search(current_source_payload, search)
        return _RequestOutcome(complete=complete, errors=[])

    def _expand_source_check(
        self,
        evidence: dict[str, dict[str, Any]],
        repository: str,
        collected_at: datetime,
        request: dict[str, Any],
    ) -> _RequestOutcome:
        path = request["path"]
        source_id = f"source:{quote(path, safe='')}"
        source_record = evidence[request["evidenceId"]]
        referenced_by = _merge_referenced_by(
            evidence.get(source_id),
            self._associations_for_request(
                source_record,
                request,
                repository,
            ),
        )
        fallback_url = f"https://github.com/{repository}/blob/HEAD/{quote(path, safe='/')}"
        payload: dict[str, Any] = {
            "adaptiveSourceCheck": True,
            "path": path,
            "targetRepository": repository,
            "checkoutCommit": None,
            "exists": None,
            "removalCommit": None,
            "replacementPath": None,
            "replacementCommit": None,
            "historyAmbiguous": True,
            "recentCommits": [],
            "referencedBy": referenced_by,
        }

        if self._checkout is None:
            if source_id not in evidence:
                evidence[source_id] = self._evidence_record(
                    "source-path",
                    fallback_url,
                    collected_at,
                    payload,
                    availability="partial",
                )
            return _RequestOutcome(
                complete=False,
                errors=[
                    self._error(
                        request,
                        stage="source-check",
                        endpoint=path,
                        message="No checkout was supplied for source inspection.",
                        effect="source removal or replacement remains ambiguous",
                    )
                ],
            )

        try:
            checkout_info = ownership.validate_checkout(
                self._checkout,
                repository,
                git_runner=self._git_runner,
                timeout_seconds=self._git_timeout_seconds,
            )
            self._safe_checkout_path(self._checkout, path)
            exists = self._path_exists_at_commit(
                self._checkout,
                checkout_info.commit,
                path,
            )
            history = ownership.load_source_history(
                self._checkout,
                path,
                checkout_info,
                git_runner=self._git_runner,
                timeout_seconds=self._git_timeout_seconds,
            )
            removal_commit = self._removed_at_commit(
                self._checkout,
                checkout_info.commit,
                path,
            )
            replacement_commit, replacement_path = self._replacement_history(
                self._checkout,
                checkout_info.commit,
                path,
            )
        except Exception as exc:
            if source_id not in evidence:
                evidence[source_id] = self._evidence_record(
                    "source-path",
                    fallback_url,
                    collected_at,
                    payload,
                    availability="partial",
                )
            return _RequestOutcome(
                complete=False,
                errors=[
                    self._error(
                        request,
                        stage="source-check",
                        endpoint=path,
                        message=str(exc),
                        effect="source removal or replacement remains ambiguous",
                    )
                ],
            )

        recent_commits = history.get("recentCommits")
        if not isinstance(recent_commits, list):
            recent_commits = []
        deterministic_history = bool(recent_commits)
        if not exists:
            deterministic_history = deterministic_history and bool(
                removal_commit or replacement_path
            )
        payload.update(
            {
                "checkoutCommit": checkout_info.commit,
                "exists": exists,
                "removalCommit": removal_commit,
                "replacementPath": replacement_path,
                "replacementCommit": replacement_commit,
                "historyAmbiguous": not deterministic_history,
                "recentCommits": recent_commits,
            }
        )
        source_url = ownership.build_blob_url(
            repository,
            checkout_info.commit,
            replacement_path or path,
        )
        evidence[source_id] = self._evidence_record(
            "source-path",
            source_url,
            collected_at,
            payload,
            availability="available" if deterministic_history else "partial",
        )
        if deterministic_history:
            return _RequestOutcome(complete=True, errors=[])
        return _RequestOutcome(
            complete=False,
            errors=[
                self._error(
                    request,
                    stage="source-check",
                    endpoint=path,
                    message="Repository history does not deterministically show the path.",
                    effect="source removal or replacement remains ambiguous",
                )
            ],
        )

    def _normalize_search_response(
        self,
        response: object,
        repository: str,
        request: Mapping[str, Any],
    ) -> tuple[list[tuple[int, dict[str, Any], str]], int, bool]:
        if not isinstance(response, Mapping):
            raise ValueError("Canonical search response must be an object.")
        total_count = response.get("total_count")
        items = response.get("items")
        incomplete_results = response.get("incomplete_results")
        if (
            not isinstance(total_count, int)
            or isinstance(total_count, bool)
            or total_count < 0
            or not isinstance(items, list)
            or len(items) > 20
            or total_count < len(items)
            or not isinstance(incomplete_results, bool)
        ):
            raise ValueError("Canonical search response has an unexpected shape.")

        normalized: list[tuple[int, dict[str, Any], str]] = []
        seen_numbers: set[int] = set()
        for raw_issue in items:
            if not isinstance(raw_issue, Mapping):
                raise ValueError("Canonical search issue results must be objects.")
            issue_number = raw_issue.get("number")
            issue_url = raw_issue.get("html_url")
            issue_title = raw_issue.get("title")
            issue_state = raw_issue.get("state")
            created_at = raw_issue.get("created_at")
            updated_at = raw_issue.get("updated_at")
            if (
                not isinstance(issue_number, int)
                or isinstance(issue_number, bool)
                or issue_number <= 0
                or issue_number in seen_numbers
                or not isinstance(issue_url, str)
                or issue_url.casefold()
                != f"https://github.com/{repository}/issues/{issue_number}".casefold()
                or raw_issue.get("pull_request") is not None
                or not isinstance(issue_title, str)
                or not issue_title.strip()
                or issue_state not in {"open", "closed"}
                or not isinstance(created_at, str)
                or not created_at.strip()
                or not isinstance(updated_at, str)
                or not updated_at.strip()
            ):
                raise ValueError("Canonical search returned malformed or out-of-scope issue evidence.")
            try:
                _parse_timestamp(created_at)
                _parse_timestamp(updated_at)
            except ValueError as exc:
                raise ValueError(
                    "Canonical search returned malformed or out-of-scope issue evidence."
                ) from exc
            seen_numbers.add(issue_number)
            labels = raw_issue.get("labels")
            normalized_labels = sorted(
                {
                    label["name"]
                    for label in labels
                    if isinstance(label, Mapping)
                    and isinstance(label.get("name"), str)
                    and label["name"]
                }
            ) if isinstance(labels, list) else []
            normalized.append(
                (
                    issue_number,
                    {
                        "number": issue_number,
                        "targetRepository": repository,
                        "state": issue_state,
                        "title": issue_title,
                        "body": self._text(raw_issue, "body"),
                        "url": issue_url,
                        "createdAt": created_at,
                        "updatedAt": updated_at,
                        "closedAt": raw_issue.get("closed_at"),
                        "labels": normalized_labels,
                        "canonicalSearchFact": {
                            "field": request["factField"],
                            "value": request["factValue"],
                            "normalized": request["factNormalized"],
                        },
                    },
                    issue_url,
                )
            )
        normalized.sort(key=lambda item: item[0])
        return normalized, total_count, incomplete_results

    def _merge_search_issue(
        self,
        evidence: dict[str, dict[str, Any]],
        issue_number: int,
        issue_url: str,
        payload: dict[str, Any],
        collected_at: datetime,
    ) -> None:
        evidence_id = f"issue:{issue_number}"
        existing = evidence.get(evidence_id)
        if not isinstance(existing, dict):
            evidence[evidence_id] = self._evidence_record(
                "issue-event",
                issue_url,
                collected_at,
                payload,
            )
            return

        existing_payload = existing.get("payload")
        merged_payload = (
            copy.deepcopy(existing_payload)
            if isinstance(existing_payload, dict)
            else {}
        )
        for key, value in payload.items():
            if key == "referencedBy":
                baseline_associations = merged_payload.get(key)
                baseline_source_issues = {
                    association.get("sourceIssueNumber")
                    for association in baseline_associations
                    if isinstance(association, Mapping)
                } if isinstance(baseline_associations, list) else set()
                request_associations = [
                    association
                    for association in value
                    if (
                        isinstance(association, Mapping)
                        and association.get("sourceIssueNumber")
                        not in baseline_source_issues
                    )
                ] if isinstance(value, list) else []
                merged_payload[key] = self._merge_associations(
                    baseline_associations,
                    request_associations,
                )
            elif value not in ("", None, []) or key not in merged_payload:
                merged_payload[key] = copy.deepcopy(value)
        evidence[evidence_id] = {
            **copy.deepcopy(existing),
            "kind": "issue-event",
            "url": issue_url,
            "availability": "available",
            "payload": merged_payload,
        }

    def _record_canonical_search(
        self,
        source_payload: dict[str, Any],
        search: dict[str, Any],
    ) -> None:
        if (
            "supportingSearch" in source_payload
            and "baselineSupportingSearch" not in source_payload
        ):
            source_payload["baselineSupportingSearch"] = copy.deepcopy(
                source_payload["supportingSearch"]
            )
        searches = source_payload.setdefault("adaptiveCanonicalSearches", [])
        if not isinstance(searches, list):
            searches = []
            source_payload["adaptiveCanonicalSearches"] = searches
        fact_identity = (
            search["queryFact"]["field"],
            search["queryFact"]["value"],
            search["queryFact"]["normalized"],
        )
        searches[:] = [
            item
            for item in searches
            if not (
                isinstance(item, Mapping)
                and isinstance(item.get("queryFact"), Mapping)
                and (
                    item["queryFact"].get("field"),
                    item["queryFact"].get("value"),
                    item["queryFact"].get("normalized"),
                )
                == fact_identity
            )
        ]
        searches.append(copy.deepcopy(search))
        searches.sort(
            key=lambda item: (
                item["queryFact"]["field"],
                item["queryFact"]["normalized"],
                item["queryFact"]["value"],
            )
        )
        if len(searches) == 1:
            source_payload["supportingSearch"] = copy.deepcopy(searches[0])
            return

        candidate_numbers = sorted(
            {
                number
                for item in searches
                for number in item.get("candidateIssueNumbers", [])
                if isinstance(number, int) and not isinstance(number, bool)
            }
        )
        total_counts = [item.get("totalCount") for item in searches]
        source_payload["supportingSearch"] = {
            "mode": "adaptive-canonical-search",
            "queryFacts": [
                copy.deepcopy(item["queryFact"])
                for item in searches
            ],
            "totalCount": (
                sum(total_counts)
                if all(
                    isinstance(value, int) and not isinstance(value, bool)
                    for value in total_counts
                )
                else None
            ),
            "returnedCount": sum(
                item.get("returnedCount", 0)
                for item in searches
                if isinstance(item.get("returnedCount"), int)
            ),
            "candidateIssueNumbers": candidate_numbers,
            "truncated": any(item.get("truncated") is True for item in searches),
            "complete": all(item.get("complete") is True for item in searches),
            "searches": copy.deepcopy(searches),
        }

    def _issue_identity(
        self,
        evidence_id: str,
        repository: str,
    ) -> tuple[str, str, int] | None:
        for target_type, pattern in (
            ("issue", _ISSUE_ID_RE),
            ("pull-request", _PR_ID_RE),
        ):
            match = pattern.fullmatch(evidence_id)
            if match is not None:
                return (
                    target_type,
                    match.group("repository") or repository,
                    int(match.group("number")),
                )
        return None

    def _target_references(
        self,
        existing_record: Mapping[str, Any],
        request: Mapping[str, Any],
        *,
        source_repository: str,
        target_type: str,
        target_repository: str,
        target_number: int | None = None,
        run_id: int | None = None,
    ) -> list[dict[str, Any]]:
        associations = self._associations_for_request(
            existing_record,
            request,
            source_repository,
        )
        references: list[dict[str, Any]] = []
        for association in associations:
            reference = {
                **association,
                "targetType": target_type,
                "targetRepository": target_repository,
                "targetUrl": existing_record["url"],
            }
            if target_number is not None:
                reference["targetNumber"] = target_number
            if run_id is not None:
                reference["runId"] = run_id
            references.append(reference)
        return references

    def _associations_for_request(
        self,
        existing_record: Mapping[str, Any],
        request: Mapping[str, Any],
        repository: str,
    ) -> list[dict[str, Any]]:
        payload = existing_record.get("payload")
        referenced_by = (
            payload.get("referencedBy")
            if isinstance(payload, Mapping)
            else None
        )
        if isinstance(referenced_by, list):
            candidates = self._merge_associations(
                [],
                [
                    reference
                    for reference in referenced_by
                    if isinstance(reference, Mapping)
                    and reference.get("sourceIssueNumber")
                    == request["sourceIssueNumber"]
                ],
            )
            if candidates:
                return candidates
        source_issue_number = int(request["sourceIssueNumber"])
        return [
            {
                "sourceIssueNumber": source_issue_number,
                "sourceEvidenceId": f"issue:{source_issue_number}",
                "sourceUrl": f"https://github.com/{repository}/issues/{source_issue_number}",
                "extractionMethod": "adaptive-expansion",
            }
        ]

    def _removed_at_commit(
        self,
        checkout: Path,
        checkout_commit: str,
        path: str,
    ) -> str | None:
        output = self._run_git(
            checkout,
            (
                "log",
                "-1",
                "--format=%H",
                "--diff-filter=D",
                checkout_commit,
                "--",
                f":(literal){path}",
            ),
        ).strip()
        if not output:
            return None
        if _FULL_SHA_RE.fullmatch(output) is None:
            raise ValueError(f"Unexpected removal history for {path}: {output!r}")
        return output

    def _replacement_history(
        self,
        checkout: Path,
        checkout_commit: str,
        path: str,
    ) -> tuple[str | None, str | None]:
        candidate_output = self._run_git(
            checkout,
            (
                "log",
                f"--max-count={_RENAME_CANDIDATE_LIMIT}",
                "--format=%H",
                checkout_commit,
                "--",
                f":(literal){path}",
            ),
        ).strip()
        if not candidate_output:
            return None, None
        candidate_commits = candidate_output.splitlines()
        if (
            len(candidate_commits) > _RENAME_CANDIDATE_LIMIT
            or any(_FULL_SHA_RE.fullmatch(commit) is None for commit in candidate_commits)
        ):
            raise ValueError(
                f"Unexpected replacement candidate history for {path}: {candidate_output!r}"
            )

        replacements: list[tuple[str, str]] = []
        for commit in candidate_commits:
            parent_output = self._run_git(
                checkout,
                ("rev-list", "--parents", "-n", "1", commit),
            ).strip()
            parent_fields = parent_output.split()
            if (
                not parent_fields
                or parent_fields[0] != commit
                or any(_FULL_SHA_RE.fullmatch(value) is None for value in parent_fields)
            ):
                raise ValueError(
                    f"Unexpected parent history for replacement candidate {commit}."
                )
            if len(parent_fields) == 1:
                diff_arguments = (
                    "diff-tree",
                    "--root",
                    "--no-commit-id",
                    "-r",
                    "-M",
                    "--name-status",
                    "-z",
                    commit,
                )
            else:
                diff_arguments = (
                    "diff-tree",
                    "--no-commit-id",
                    "-r",
                    "-M",
                    "--name-status",
                    "-z",
                    parent_fields[1],
                    commit,
                )
            for old_path, new_path in self._renames_from_diff(
                self._run_git(checkout, diff_arguments),
            ):
                if old_path == path:
                    replacements.append((commit, new_path))

        if len(replacements) > 1:
            raise ValueError(f"Replacement history for {path} is ambiguous.")
        return replacements[0] if replacements else (None, None)

    @staticmethod
    def _renames_from_diff(output: str) -> list[tuple[str, str]]:
        fields = output.split("\0")
        if fields and fields[-1] == "":
            fields.pop()
        renames: list[tuple[str, str]] = []
        index = 0
        while index < len(fields):
            status = fields[index]
            index += 1
            path_count = 2 if status.startswith(("R", "C")) else 1
            if not status or index + path_count > len(fields):
                raise ValueError("Unexpected name-status output while inspecting replacement history.")
            paths = fields[index : index + path_count]
            index += path_count
            if status.startswith("R"):
                renames.append((paths[0], paths[1]))
        return renames

    def _path_exists_at_commit(
        self,
        checkout: Path,
        checkout_commit: str,
        path: str,
    ) -> bool:
        output = self._run_git(
            checkout,
            (
                "ls-tree",
                "--name-only",
                "-z",
                checkout_commit,
                "--",
                f":(literal){path}",
            ),
        )
        matches = [entry for entry in output.split("\0") if entry]
        if any(entry != path for entry in matches) or len(matches) > 1:
            raise ValueError(f"Unexpected commit-tree lookup result for {path}.")
        return matches == [path]

    def _run_git(
        self,
        checkout: Path,
        arguments: tuple[str, ...],
    ) -> str:
        command = ["git", "--no-pager", "-C", str(checkout), *arguments]
        result = self._git_runner(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=self._git_timeout_seconds,
        )
        if getattr(result, "returncode", 1) != 0:
            stderr = getattr(result, "stderr", "")
            stdout = getattr(result, "stdout", "")
            message = (
                stderr.strip()
                if isinstance(stderr, str) and stderr.strip()
                else stdout.strip()
                if isinstance(stdout, str) and stdout.strip()
                else f"git exited with {getattr(result, 'returncode', 1)}"
            )
            raise ValueError(message)
        stdout = getattr(result, "stdout", "")
        return stdout if isinstance(stdout, str) else ""

    @staticmethod
    def _safe_checkout_path(checkout: Path, relative_path: str) -> Path:
        root = checkout.resolve()
        parsed = PurePosixPath(relative_path)
        if (
            parsed.is_absolute()
            or not parsed.parts
            or any(part in {"", ".", ".."} for part in parsed.parts)
        ):
            raise ValueError("Evidence-backed source path escapes the supplied checkout.")
        candidate = root.joinpath(*parsed.parts)
        if not candidate.is_relative_to(root):
            raise ValueError("Evidence-backed source path escapes the supplied checkout.")
        return candidate

    @staticmethod
    def _merge_associations(
        existing: object,
        incoming: object,
    ) -> list[dict[str, Any]]:
        indexed: dict[tuple[object, ...], dict[str, Any]] = {}
        for value in (existing, incoming):
            if not isinstance(value, list):
                continue
            for reference in value:
                if not isinstance(reference, Mapping):
                    continue
                key = (
                    reference.get("sourceIssueNumber"),
                    reference.get("sourceEvidenceId"),
                    reference.get("sourceUrl"),
                    reference.get("extractionMethod"),
                )
                if (
                    isinstance(key[0], int)
                    and not isinstance(key[0], bool)
                    and all(isinstance(item, str) and item for item in key[1:])
                ):
                    indexed[key] = {
                        "sourceIssueNumber": key[0],
                        "sourceEvidenceId": key[1],
                        "sourceUrl": key[2],
                        "extractionMethod": key[3],
                    }
        return [indexed[key] for key in sorted(indexed)]

    @staticmethod
    def _evidence_record(
        kind: str,
        url: str,
        collected_at: datetime,
        payload: dict[str, Any],
        *,
        availability: str = "available",
    ) -> dict[str, Any]:
        return {
            "kind": kind,
            "url": url,
            "collectedAt": collected_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "availability": availability,
            "payload": copy.deepcopy(payload),
        }

    def _collection_error(
        self,
        request: Mapping[str, Any],
        error: CollectionError,
    ) -> dict[str, Any]:
        return self._error(
            request,
            stage=error.stage,
            endpoint=error.endpoint,
            message=error.message,
            effect=error.effect or "requested evidence remains partial",
        )

    @staticmethod
    def _error(
        request: Mapping[str, Any],
        *,
        stage: str,
        endpoint: str,
        message: str,
        effect: str,
    ) -> dict[str, Any]:
        return {
            "requestType": request["type"],
            "sourceIssueNumber": request["sourceIssueNumber"],
            "evidenceId": request["evidenceId"],
            "stage": stage,
            "endpoint": endpoint,
            "message": message,
            "effect": effect,
        }

    @staticmethod
    def _quote_search_value(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _text(mapping: Mapping[str, Any], key: str) -> str:
        value = mapping.get(key)
        return value if isinstance(value, str) else ""

    def _now_value(self) -> datetime:
        if self._now is None:
            return datetime.now(UTC)
        value = self._now() if callable(self._now) else self._now
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
