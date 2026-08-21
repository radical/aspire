from __future__ import annotations

import copy
from dataclasses import dataclass
from collections.abc import Mapping
import re
from typing import Any
from urllib.parse import unquote


_ISSUE_EVIDENCE_ID_RE = re.compile(
    r"^issue:(?:(?P<repository>[^:]+/[^:]+):)?(?P<number>[1-9][0-9]*)"
    r"(?::(?P<child_kind>comment|event):(?P<child_id>[1-9][0-9]*))?$"
)
_RUN_EVIDENCE_ID_RE = re.compile(
    r"^run:(?P<run_id>[1-9][0-9]*)"
    r"(?::attempt:(?P<attempt>[1-9][0-9]*):job:(?P<job_id>[1-9][0-9]*)(?::log)?"
    r"|:check:(?P<check_run_id>[1-9][0-9]*):annotation:(?P<annotation_id>[1-9][0-9]*))?$"
)
_PR_EVIDENCE_ID_RE = re.compile(
    r"^pr:(?:(?P<repository>[^:]+/[^:]+):)?(?P<number>[1-9][0-9]*)$"
)
_COMMIT_EVIDENCE_ID_RE = re.compile(
    r"^commit:(?:(?P<repository>[^:]+/[^:]+):)?(?P<sha>[0-9a-fA-F]{3,40})$"
)
_SOURCE_EVIDENCE_ID_RE = re.compile(r"^source:(?P<path>[^:]+)$")
_CODEOWNERS_EVIDENCE_ID_RE = re.compile(
    r"^codeowners:(?P<path>[^:]+):(?P<line>[1-9][0-9]*)$"
)


class RefreshError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RefreshPlan:
    reuse: tuple[str, ...] = ()
    refresh: tuple[str, ...] = ()
    retry: tuple[str, ...] = ()
    retire: tuple[str, ...] = ()
    new_issues: tuple[int, ...] = ()
    changed_issues: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "reuse", tuple(sorted(set(self.reuse))))
        object.__setattr__(self, "refresh", tuple(sorted(set(self.refresh))))
        object.__setattr__(self, "retry", tuple(sorted(set(self.retry))))
        object.__setattr__(self, "retire", tuple(sorted(set(self.retire))))
        object.__setattr__(self, "new_issues", tuple(sorted(set(self.new_issues))))
        object.__setattr__(self, "changed_issues", tuple(sorted(set(self.changed_issues))))


def plan_refresh(
    repository: str,
    open_inventory: list[dict[str, Any]],
    previous_snapshot: dict[str, Any],
    current_history: dict[str, Any],
    *,
    full_refresh: bool = False,
) -> RefreshPlan:
    _validate_repository(repository, previous_snapshot, "snapshot")
    _validate_repository(repository, current_history, "history")

    previous_evidence = _mapping(previous_snapshot.get("evidence"))
    history_evidence = _mapping(current_history.get("evidence"))
    live_by_number = _live_issue_inventory(open_inventory)
    previous_numbers = _issue_numbers(previous_snapshot.get("openIssues"))
    live_numbers = set(live_by_number)
    supporting_numbers = _supporting_issue_numbers(previous_snapshot)
    issue_roots = _issue_root_anchors(previous_snapshot, previous_numbers, supporting_numbers)
    new_issues = live_numbers - previous_numbers
    source_schema_matches = (
        previous_snapshot.get("schemaVersion") == 1
        and current_history.get("schemaVersion") == 1
        and _mapping(current_history.get("sourceSchemaVersions")).get("snapshot") == 1
    )

    changed_issues: set[int] = set()
    retry_issue_numbers: set[int] = set()
    for number in sorted(live_numbers & previous_numbers):
        evidence_id = f"issue:{number}"
        prior_record = previous_evidence.get(evidence_id)
        history_record = history_evidence.get(evidence_id)
        if not _record_is_complete(prior_record) or not _record_is_complete(history_record):
            retry_issue_numbers.add(number)
            continue
        live_updated_at = live_by_number[number].get("updated_at")
        if (
            not source_schema_matches
            or not isinstance(live_updated_at, str)
            or not live_updated_at
            or history_record.get("sourceUpdatedAt") != live_updated_at
            or not _same_record_identity(
                evidence_id,
                prior_record,
                history_record,
                repository,
            )
        ):
            changed_issues.add(number)

    reuse: set[str] = set()
    refresh: set[str] = set()
    retry: set[str] = set()
    retire: set[str] = set()
    for evidence_id, value in previous_evidence.items():
        if not isinstance(evidence_id, str):
            continue
        record = value if isinstance(value, Mapping) else None
        associated_issues = _associated_issue_numbers(evidence_id, record)
        live_root_anchors = {
            root
            for issue_number in associated_issues
            for root in issue_roots.get(issue_number, {issue_number})
            if root in live_numbers
        }
        if associated_issues and not live_root_anchors:
            retire.add(evidence_id)
            continue
        if full_refresh or not source_schema_matches:
            refresh.add(evidence_id)
            continue

        history_record = history_evidence.get(evidence_id)
        if (
            not _record_is_complete(record)
            or not _record_is_complete(history_record)
            or not _same_record_identity(
                evidence_id,
                record,
                history_record,
                repository,
            )
        ):
            retry.add(evidence_id)
            continue
        supporting_issue_number = _exact_issue_number(evidence_id)
        if (
            supporting_issue_number in supporting_numbers
            and live_root_anchors
        ):
            refresh.add(evidence_id)
            continue
        if associated_issues & retry_issue_numbers:
            retry.add(evidence_id)
            continue
        if associated_issues & changed_issues:
            refresh.add(evidence_id)
            continue

        freshness_class = history_record.get("freshnessClass")
        root_issue_number = _root_issue_number(evidence_id, live_numbers)
        if root_issue_number is not None:
            if _source_version_matches(
                record,
                history_record,
                {root_issue_number},
                live_by_number,
            ):
                reuse.add(evidence_id)
            else:
                refresh.add(evidence_id)
        elif freshness_class == "retryable":
            retry.add(evidence_id)
        elif freshness_class == "immutable":
            reuse.add(evidence_id)
        elif freshness_class == "source-versioned":
            if _source_version_matches(record, history_record, associated_issues, live_by_number):
                reuse.add(evidence_id)
            else:
                refresh.add(evidence_id)
        elif freshness_class == "derived":
            if _dependency_fingerprint_matches(record, history_record):
                reuse.add(evidence_id)
            else:
                refresh.add(evidence_id)
        elif _is_immutable_completed_record(record):
            if _contains_volatile_tracker_state(record):
                refresh.add(evidence_id)
            else:
                reuse.add(evidence_id)
        else:
            refresh.add(evidence_id)

    return RefreshPlan(
        reuse=tuple(reuse),
        refresh=tuple(refresh),
        retry=tuple(retry),
        retire=tuple(retire),
        new_issues=tuple(new_issues),
        changed_issues=tuple(changed_issues),
    )


def reconstruct_inventory(
    repository: str,
    open_inventory: list[dict[str, Any]],
    previous_snapshot: dict[str, Any],
    plan: RefreshPlan,
) -> Any:
    _validate_repository(repository, previous_snapshot, "snapshot")
    from .collector import InventoryResult

    live_numbers = set(_live_issue_inventory(open_inventory))
    previous_numbers = _issue_numbers(previous_snapshot.get("openIssues"))
    supporting_numbers = _supporting_issue_numbers(previous_snapshot)
    issue_roots = _issue_root_anchors(previous_snapshot, previous_numbers, supporting_numbers)
    open_issues = [
        copy.deepcopy(issue)
        for issue in previous_snapshot.get("issues", [])
        if isinstance(issue, dict) and issue.get("number") in live_numbers
    ]
    supporting_issues = [
        copy.deepcopy(issue)
        for issue in previous_snapshot.get("supportingIssues", [])
        if isinstance(issue, dict)
    ]
    retained_supporting_numbers = {
        number
        for number in supporting_numbers
        if issue_roots.get(number, set()) & live_numbers
    }
    supporting_issues = [
        issue
        for issue in supporting_issues
        if issue.get("number") in retained_supporting_numbers
    ]
    retired = set(plan.retire)
    previous_evidence = _mapping(previous_snapshot.get("evidence"))
    evidence = {
        evidence_id: copy.deepcopy(record)
        for evidence_id, record in sorted(previous_evidence.items())
        if isinstance(evidence_id, str)
        and isinstance(record, dict)
        and evidence_id not in retired
        and not _contains_report_content(record)
    }
    raw_references = _mapping(previous_snapshot.get("references"))
    references: dict[int, list[dict[str, Any]]] = {}
    for number in sorted(live_numbers | retained_supporting_numbers):
        raw_value = raw_references.get(str(number), raw_references.get(number))
        if not isinstance(raw_value, list) or not raw_value:
            continue
        references[number] = copy.deepcopy(raw_value)
    return InventoryResult(
        open_issues=sorted(open_issues, key=lambda issue: int(issue["number"])),
        supporting_issues=sorted(
            supporting_issues,
            key=lambda issue: int(issue.get("number", 0)),
        ),
        evidence=evidence,
        collection_errors=[],
        warnings=[],
        references=references,
        refresh_plan=plan,
    )


def complete_refresh_plan(
    plan: RefreshPlan,
    evidence: object,
) -> RefreshPlan:
    records = evidence if isinstance(evidence, Mapping) else {}
    current_ids = {
        evidence_id
        for evidence_id in records
        if isinstance(evidence_id, str)
    }
    failed_ids = {
        evidence_id
        for evidence_id, record in records.items()
        if isinstance(evidence_id, str) and not _record_is_complete(record)
    }
    planned_reuse = set(plan.reuse)
    planned_refresh = set(plan.refresh)
    classified = planned_reuse | planned_refresh | set(plan.retry) | set(plan.retire)
    complete_ids = current_ids - failed_ids
    retire = set(plan.retire)
    retry = (
        set(plan.retry)
        | failed_ids
        | ((planned_reuse | planned_refresh) - complete_ids)
    ) - retire
    reuse = (planned_reuse & complete_ids) - retry - retire
    refresh = (
        (planned_refresh | (current_ids - classified)) & complete_ids
    ) - reuse - retry - retire
    return RefreshPlan(
        reuse=tuple(reuse),
        refresh=tuple(refresh),
        retry=tuple(retry),
        retire=tuple(retire),
        new_issues=plan.new_issues,
        changed_issues=plan.changed_issues,
    )


def _validate_repository(
    repository: str,
    document: Mapping[str, Any],
    description: str,
) -> None:
    actual = document.get("repository")
    if not isinstance(actual, str) or actual.casefold() != repository.casefold():
        raise RefreshError(f"{description} repository does not match {repository}.")


def _mapping(value: object) -> Mapping[Any, Any]:
    return value if isinstance(value, Mapping) else {}


def _issue_numbers(value: object) -> set[int]:
    if not isinstance(value, list):
        return set()
    return {
        number
        for number in value
        if isinstance(number, int) and not isinstance(number, bool) and number > 0
    }


def _live_issue_inventory(open_inventory: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    live: dict[int, dict[str, Any]] = {}
    for raw_issue in open_inventory:
        if not isinstance(raw_issue, dict) or raw_issue.get("pull_request"):
            continue
        number = raw_issue.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            continue
        existing = live.get(number)
        if existing is None:
            live[number] = copy.deepcopy(raw_issue)
            continue
        labels = {
            label.get("name")
            for source in (existing, raw_issue)
            for label in source.get("labels", [])
            if isinstance(label, dict) and isinstance(label.get("name"), str)
        }
        live[number] = copy.deepcopy(raw_issue)
        live[number]["labels"] = [{"name": label} for label in sorted(labels)]
    return dict(sorted(live.items()))


def _record_is_complete(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    if value.get("availability") != "available":
        return False
    if not isinstance(value.get("kind"), str) or not isinstance(value.get("url"), str):
        return False
    payload = value.get("payload")
    if not isinstance(payload, Mapping) or _contains_report_content(value):
        return False
    return True


def _contains_report_content(value: object) -> bool:
    if isinstance(value, Mapping):
        if value.get("source") == "previous-report":
            return True
        for key, child in value.items():
            if str(key) in {"previousDecision", "previousDecisions", "report", "reports"}:
                return True
            if _contains_report_content(child):
                return True
    elif isinstance(value, list):
        return any(_contains_report_content(item) for item in value)
    return False


def _same_record_identity(
    evidence_id: str,
    record: Mapping[str, Any],
    history_record: Mapping[str, Any],
    repository: str,
) -> bool:
    if record.get("kind") != history_record.get("kind") or record.get("url") != history_record.get("url"):
        return False
    payload = _mapping(record.get("payload"))
    history_payload = _mapping(history_record.get("payload"))
    if (
        not _payload_matches_evidence_id(evidence_id, payload, repository)
        or not _payload_matches_evidence_id(evidence_id, history_payload, repository)
    ):
        return False
    for key in ("number", "runId", "sha", "jobId", "id"):
        if key in payload or key in history_payload:
            if payload.get(key) != history_payload.get(key):
                return False
    record_repository = payload.get("targetRepository")
    history_repository = history_payload.get("targetRepository")
    if isinstance(record_repository, str) or isinstance(history_repository, str):
        if (
            not isinstance(record_repository, str)
            or not isinstance(history_repository, str)
            or record_repository.casefold() != history_repository.casefold()
        ):
            return False
        expected_repository = _evidence_id_repository_scope(evidence_id, repository)
        if (
            expected_repository is not None
            and record_repository.casefold() != expected_repository.casefold()
        ):
            return False
    return True


def _payload_matches_evidence_id(
    evidence_id: str,
    payload: Mapping[Any, Any],
    repository: str,
) -> bool:
    issue_match = _ISSUE_EVIDENCE_ID_RE.fullmatch(evidence_id)
    if issue_match is not None:
        expected_number = int(issue_match.group("number"))
        child_kind = issue_match.group("child_kind")
        if child_kind is None:
            if not _same_int(payload.get("number"), expected_number):
                return False
        else:
            if (
                not _same_int(payload.get("sourceIssueNumber"), expected_number)
                or not _same_int(payload.get("id"), int(issue_match.group("child_id")))
            ):
                return False
        return _payload_repository_matches(
            payload,
            issue_match.group("repository"),
            repository,
            required=issue_match.group("repository") is not None,
        )

    run_match = _RUN_EVIDENCE_ID_RE.fullmatch(evidence_id)
    if run_match is not None:
        if not _same_int(payload.get("runId"), int(run_match.group("run_id"))):
            return False
        for group, field in (
            ("attempt", "attempt"),
            ("job_id", "jobId"),
            ("check_run_id", "checkRunId"),
            ("annotation_id", "annotationId"),
        ):
            expected = run_match.group(group)
            if expected is not None and not _same_int(payload.get(field), int(expected)):
                return False
        return _payload_repository_matches(payload, repository, repository, required=True)

    pr_match = _PR_EVIDENCE_ID_RE.fullmatch(evidence_id)
    if pr_match is not None:
        return (
            _same_int(payload.get("number"), int(pr_match.group("number")))
            and _payload_repository_matches(
                payload,
                pr_match.group("repository"),
                repository,
                required=True,
            )
        )

    commit_match = _COMMIT_EVIDENCE_ID_RE.fullmatch(evidence_id)
    if commit_match is not None:
        sha = payload.get("sha")
        return (
            isinstance(sha, str)
            and sha.casefold() == commit_match.group("sha").casefold()
            and _payload_repository_matches(
                payload,
                commit_match.group("repository"),
                repository,
                required=True,
            )
        )

    source_match = _SOURCE_EVIDENCE_ID_RE.fullmatch(evidence_id)
    if source_match is not None:
        return (
            payload.get("path") == unquote(source_match.group("path"))
            and _payload_repository_matches(payload, repository, repository, required=True)
        )

    codeowners_match = _CODEOWNERS_EVIDENCE_ID_RE.fullmatch(evidence_id)
    if codeowners_match is not None:
        return (
            payload.get("path") == unquote(codeowners_match.group("path"))
            and _same_int(payload.get("line"), int(codeowners_match.group("line")))
            and _payload_repository_matches(payload, repository, repository, required=False)
        )

    return True


def _same_int(value: object, expected: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == expected


def _payload_repository_matches(
    payload: Mapping[Any, Any],
    scoped_repository: str | None,
    default_repository: str,
    *,
    required: bool,
) -> bool:
    expected = scoped_repository or default_repository
    actual = payload.get("targetRepository")
    if actual is None and not required:
        return True
    return isinstance(actual, str) and actual.casefold() == expected.casefold()


def _supporting_issue_numbers(snapshot: Mapping[str, Any]) -> set[int]:
    supporting = snapshot.get("supportingIssues")
    if not isinstance(supporting, list):
        return set()
    return {
        number
        for issue in supporting
        if isinstance(issue, Mapping)
        and isinstance((number := issue.get("number")), int)
        and not isinstance(number, bool)
        and number > 0
    }


def _issue_root_anchors(
    snapshot: Mapping[str, Any],
    root_numbers: set[int],
    supporting_numbers: set[int],
) -> dict[int, set[int]]:
    parents: dict[int, set[int]] = {number: set() for number in supporting_numbers}
    raw_references = _mapping(snapshot.get("references"))
    for raw_source_number, raw_rows in raw_references.items():
        try:
            source_number = int(raw_source_number)
        except (TypeError, ValueError):
            continue
        if not isinstance(raw_rows, list):
            continue
        for row in raw_rows:
            if not isinstance(row, Mapping) or row.get("targetType") != "issue":
                continue
            target_number = row.get("targetNumber")
            if (
                isinstance(target_number, int)
                and not isinstance(target_number, bool)
                and target_number in supporting_numbers
            ):
                parents[target_number].add(source_number)

    evidence = _mapping(snapshot.get("evidence"))
    for number in supporting_numbers:
        record = evidence.get(f"issue:{number}")
        if not isinstance(record, Mapping):
            continue
        for source_number in _associated_issue_numbers(f"issue:{number}", record):
            if source_number != number:
                parents[number].add(source_number)

    resolved: dict[int, set[int]] = {number: {number} for number in root_numbers}

    def resolve(number: int, visiting: set[int]) -> set[int]:
        if number in resolved:
            return resolved[number]
        if number in visiting:
            return set()
        roots = {
            root
            for parent in parents.get(number, set())
            for root in resolve(parent, visiting | {number})
        }
        resolved[number] = roots
        return roots

    for number in supporting_numbers:
        resolve(number, set())
    return resolved


def _exact_issue_number(evidence_id: str) -> int | None:
    parts = evidence_id.split(":")
    if len(parts) != 2 or parts[0] != "issue" or not parts[1].isdigit():
        return None
    return int(parts[1])


def _evidence_id_repository_scope(evidence_id: str, repository: str) -> str | None:
    parts = evidence_id.split(":")
    if not parts or parts[0] not in {"issue", "pr", "commit"}:
        return None
    if len(parts) >= 3 and "/" in parts[1]:
        return parts[1]
    return repository


def _associated_issue_numbers(
    evidence_id: str,
    record: Mapping[str, Any] | None,
) -> set[int]:
    numbers: set[int] = set()
    if evidence_id.startswith("issue:"):
        raw_number = evidence_id.split(":", 2)[1]
        if raw_number.isdigit():
            numbers.add(int(raw_number))
    if not isinstance(record, Mapping):
        return numbers
    payload = _mapping(record.get("payload"))
    source_number = payload.get("sourceIssueNumber")
    if isinstance(source_number, int) and not isinstance(source_number, bool):
        numbers.add(source_number)
    referenced_by = payload.get("referencedBy")
    if isinstance(referenced_by, list):
        for reference in referenced_by:
            if not isinstance(reference, Mapping):
                continue
            number = reference.get("sourceIssueNumber")
            if isinstance(number, int) and not isinstance(number, bool):
                numbers.add(number)
    return numbers


def _root_issue_number(
    evidence_id: str,
    live_numbers: set[int],
) -> int | None:
    parts = evidence_id.split(":")
    if len(parts) != 2 or parts[0] != "issue" or not parts[1].isdigit():
        return None
    number = int(parts[1])
    return number if number in live_numbers else None


def _source_version_matches(
    record: Mapping[str, Any],
    history_record: Mapping[str, Any],
    associated_issues: set[int],
    live_by_number: Mapping[int, Mapping[str, Any]],
) -> bool:
    payload = _mapping(record.get("payload"))
    source_updated_at = history_record.get("sourceUpdatedAt")
    payload_updated_at = payload.get("sourceUpdatedAt", payload.get("updatedAt"))
    if isinstance(payload_updated_at, str) and payload_updated_at:
        if source_updated_at != payload_updated_at:
            return False
        return True
    if len(associated_issues) == 1:
        issue_number = next(iter(associated_issues))
        live = live_by_number.get(issue_number)
        return (
            isinstance(live, Mapping)
            and isinstance(source_updated_at, str)
            and source_updated_at == live.get("updated_at")
        )
    return False


def _dependency_fingerprint_matches(
    record: Mapping[str, Any],
    history_record: Mapping[str, Any],
) -> bool:
    fingerprint = _mapping(record.get("payload")).get("dependencyFingerprint")
    return (
        isinstance(fingerprint, str)
        and bool(fingerprint)
        and fingerprint == _mapping(history_record.get("payload")).get("dependencyFingerprint")
    )


def _is_immutable_completed_record(record: Mapping[str, Any]) -> bool:
    kind = record.get("kind")
    payload = _mapping(record.get("payload"))
    if kind in {"commit", "workflow-log"}:
        return True
    if kind == "pull-request":
        return isinstance(payload.get("mergedAt"), str) and bool(payload["mergedAt"])
    if kind in {"workflow-run", "workflow-job"}:
        return payload.get("status") == "completed" or bool(payload.get("conclusion"))
    if kind == "issue-event" and ":event:" in str(payload.get("evidenceId", "")):
        return True
    return False


def _contains_volatile_tracker_state(record: Mapping[str, Any]) -> bool:
    payload = _mapping(record.get("payload"))
    return record.get("kind") == "workflow-run" and (
        payload.get("recentHistoryCollected") is True
        or payload.get("recentHistoryGap") not in {None, "not-requested"}
    )
