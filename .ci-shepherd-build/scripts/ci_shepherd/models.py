from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any


class ValidationError(ValueError):
    pass


ISSUE_KINDS = frozenset({"incident", "root-cause", "tracker", "transient"})
STATES = frozenset(
    {
        "observing",
        "actionable",
        "needs-human",
        "fix-in-progress",
        "awaiting-verification",
        "resolved",
        "stale",
        "tracked-elsewhere",
        "regression",
        "duplicate",
        "insufficient-evidence",
    }
)
ACTIONS = frozenset(
    {
        "wait",
        "investigate",
        "fix",
        "ping-human",
        "merge-duplicate",
        "close",
        "close-resolved",
        "close-stale",
        "close-as-tracked",
        "recommend-close",
        "open-dedicated-issue",
        "open-regression",
    }
)
CONFIDENCE = frozenset({"high", "medium", "low"})
RELATIONSHIPS = frozenset(
    {
        "exact-duplicate",
        "probable-duplicate",
        "canonical-tracker",
        "fixed-by",
        "regression-of",
        "supersedes",
        "same-incident",
        "related",
    }
)
HIGH_RISK_ACTIONS = frozenset(
    {
        "close",
        "close-resolved",
        "close-stale",
        "close-as-tracked",
        "open-dedicated-issue",
        "merge-duplicate",
        "open-regression",
    }
)
EVIDENCE_ROLES = frozenset(
    {
        "canonical-issue",
        "canonical-search-complete",
        "current-failing-run",
        "deterministic-marker",
        "known-flaky-signature",
        "merged-fix",
        "newer-failure",
        "no-newer-matching-failure",
        "no-recent-matching-failure",
        "normalized-cause",
        "normalized-facts",
        "obsolete-surface",
        "post-fix-green",
        "prior-resolved-episode",
        "recurrence",
        "recovery",
    }
)
EVIDENCE_AVAILABILITIES = frozenset(
    {
        "available",
        "expired-or-unavailable",
        "not-enriched",
        "partial",
    }
)
EVIDENCE_REQUEST_TYPES = frozenset(
    {
        "issue-reference",
        "workflow-run",
        "canonical-search",
        "source-check",
    }
)
EXPANSION_STATUSES = frozenset({"complete", "partial"})
EVIDENCE_REQUEST_DECISION_GATES = frozenset(
    {
        "merged-fix",
        "recovery",
        "post-fix-green",
        "no-newer-matching-failure",
        "no-recent-matching-failure",
        "canonical-issue",
        "canonical-search-complete",
        "obsolete-surface",
        "current-failing-run",
        "prior-resolved-episode",
    }
)
SUPPORTING_CANDIDATE_DISPOSITIONS = frozenset(
    {
        "excluded-depth",
        "excluded-budget",
        "failed",
    }
)
HIGH_RISK_ACTION_RELEVANT_ROLES: dict[str, frozenset[str]] = {
    "close": frozenset(
        {
            "merged-fix",
            "recovery",
            "post-fix-green",
            "no-newer-matching-failure",
            "newer-failure",
        }
    ),
    "close-resolved": frozenset(
        {
            "merged-fix",
            "recovery",
            "post-fix-green",
            "no-newer-matching-failure",
            "newer-failure",
        }
    ),
    "close-stale": frozenset(
        {
            "obsolete-surface",
            "no-recent-matching-failure",
            "newer-failure",
        }
    ),
    "close-as-tracked": frozenset({"canonical-issue"}),
    "open-dedicated-issue": frozenset(
        {
            "current-failing-run",
            "recurrence",
            "known-flaky-signature",
            "canonical-search-complete",
            "canonical-issue",
        }
    ),
    "merge-duplicate": frozenset(
        {
            "canonical-issue",
            "deterministic-marker",
            "normalized-facts",
        }
    ),
    "open-regression": frozenset(
        {
            "current-failing-run",
            "prior-resolved-episode",
            "normalized-cause",
        }
    ),
}
PRIMARY_EVIDENCE_KINDS = frozenset(
    {
        "issue-event",
        "issue-comment",
        "workflow-run",
        "workflow-job",
        "workflow-log",
        "pull-request",
        "commit",
        "source-path",
        "codeowners",
    }
)
VALID_STATE_ACTIONS = {
    "observing": frozenset({"wait"}),
    "actionable": frozenset({"investigate", "fix", "open-dedicated-issue"}),
    "needs-human": frozenset({"ping-human", "recommend-close"}),
    "fix-in-progress": frozenset({"wait"}),
    "awaiting-verification": frozenset({"wait"}),
    "resolved": frozenset({"close", "close-resolved", "recommend-close"}),
    "stale": frozenset({"close-stale"}),
    "tracked-elsewhere": frozenset({"close-as-tracked"}),
    "regression": frozenset({"open-regression"}),
    "duplicate": frozenset({"merge-duplicate"}),
    "insufficient-evidence": frozenset({"wait", "investigate", "ping-human"}),
}

OCCURRENCE_CAUSES = frozenset(
    {
        "test-flake",
        "test-contention",
        "infra-transient",
        "product-regression-suspect",
        "toolchain-build-break",
        "repo-config-break",
        "unknown",
    }
)
LIFECYCLE_STATES = frozenset(
    {
        "new",
        "observing",
        "recurrent",
        "dormant-unverified",
        "dormant-verified",
        "fix-merged-unverified",
        "resolved-verified",
        "needs-policy",
        "human-owned",
        "duplicate-of",
        "data-quality-blocked",
    }
)
PROPOSAL_INTENTS = frozenset(
    {
        "no-op",
        "keep-watching",
        "investigate-now",
        "assign-copilot-investigation",
        "request-closure-review",
        "request-quarantine-review",
        "propose-retry-pattern",
        "request-rerun",
        "escalate-systemic",
        "escalate-blocking",
        "flag-data-quality",
    }
)
TARGET_KINDS = frozenset({"issue", "test", "failureFingerprint", "workflowRun", "investigation"})
EXECUTOR_CAPABILITIES = frozenset(
    {
        "post-comment",
        "apply-label",
        "remove-label",
        "close-issue",
        "assign-copilot-investigation",
        "dispatch-rerun",
        "create-policy-pr",
        "create-quarantine-pr",
    }
)


_OWNER_PATTERN = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_REPOSITORY_NAME_PATTERN = r"[A-Za-z0-9._-]+"
_REPOSITORY_PATTERN = rf"{_OWNER_PATTERN}/{_REPOSITORY_NAME_PATTERN}"
_REPOSITORY_RE = re.compile(rf"^{_REPOSITORY_PATTERN}$")
_ISSUE_ID_RE = re.compile(r"^issue:(?P<number>[1-9][0-9]*)$")
_EXTERNAL_ISSUE_ID_RE = re.compile(
    rf"^issue:(?P<repository>{_REPOSITORY_PATTERN}):(?P<number>[1-9][0-9]*)$"
)
_ISSUE_COMMENT_ID_RE = re.compile(r"^issue:(?P<number>[1-9][0-9]*):comment:(?P<comment_id>[1-9][0-9]*)$")
_ISSUE_EVENT_ID_RE = re.compile(r"^issue:(?P<number>[1-9][0-9]*):event:(?P<event_id>[1-9][0-9]*)$")
_RUN_ID_RE = re.compile(
    r"^run:(?P<run_id>[1-9][0-9]*)(?::attempt:(?P<attempt>[1-9][0-9]*|none)"
    r":job:(?P<job_id>[1-9][0-9]*)(?::log)?)?$"
)
_RUN_CHECK_ID_RE = re.compile(
    r"^run:(?P<run_id>[1-9][0-9]*):check:(?P<check_run_id>[1-9][0-9]*):annotation:(?P<annotation_id>[1-9][0-9]*)$"
)
_PR_ID_RE = re.compile(r"^pr:(?P<number>[1-9][0-9]*)$")
_EXTERNAL_PR_ID_RE = re.compile(
    rf"^pr:(?P<repository>{_REPOSITORY_PATTERN}):(?P<number>[1-9][0-9]*)$"
)
_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_COMMIT_ID_RE = re.compile(r"^commit:(?P<sha>[0-9a-fA-F]{7,40})$")
_EXTERNAL_COMMIT_ID_RE = re.compile(
    rf"^commit:(?P<repository>{_REPOSITORY_PATTERN}):(?P<sha>[0-9a-fA-F]{{7,40}})$"
)
_SOURCE_ID_RE = re.compile(r"^source:(?P<path>[^:]+)$")
_CODEOWNERS_ID_RE = re.compile(r"^codeowners:(?P<path>[^:]+):(?P<line_number>[1-9][0-9]*)$")
_GITHUB_ISSUE_URL_RE = re.compile(
    rf"^https://github\.com/(?P<repository>{_REPOSITORY_PATTERN})/issues/(?P<number>[1-9][0-9]*)$"
)


def stable_json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _repository_key(repository: str) -> str:
    return repository.casefold()


def _same_repository(left: str, right: str) -> bool:
    return _repository_key(left) == _repository_key(right)


def _issue_target_key(target: tuple[str, int]) -> tuple[str, int]:
    return _repository_key(target[0]), target[1]


def _same_issue_target(left: tuple[str, int], right: tuple[str, int]) -> bool:
    return _issue_target_key(left) == _issue_target_key(right)


def _add_issue_target(
    targets: dict[tuple[str, int], tuple[str, int]],
    target: tuple[str, int],
) -> None:
    targets.setdefault(_issue_target_key(target), target)


def _is_issue_url_for_repository(issue_url: str, repository: str, issue_number: int) -> bool:
    match = _GITHUB_ISSUE_URL_RE.fullmatch(issue_url)
    if match is None:
        return False
    return int(match.group("number")) == issue_number and _same_repository(match.group("repository"), repository)


def validate_snapshot(snapshot: object) -> None:
    mapping = _require_mapping(snapshot, "snapshot")
    _require_exact_int(mapping, "schemaVersion", 1)
    _require_repository(mapping)
    _require_nonempty_string(mapping, "collectedAt")
    _require_unique_int_list(mapping, "openIssues")
    _require_list(mapping, "collectionErrors")

    evidence = _require_mapping(mapping.get("evidence"), "evidence")
    for evidence_id, record in evidence.items():
        _validate_evidence_record(evidence_id, record)
    _validate_expansion_manifests(mapping)


def validate_evidence_requests(
    snapshot: object,
    request_document: object,
) -> list[dict[str, Any]]:
    validate_snapshot(snapshot)
    snapshot_mapping = _require_mapping(snapshot, "snapshot")
    request_mapping = _require_mapping(request_document, "evidence request document")
    _require_only_fields(
        request_mapping,
        {"schemaVersion", "repository", "round", "requests"},
        "evidence request document",
    )
    _require_exact_int(request_mapping, "schemaVersion", 1)
    repository = _require_repository(snapshot_mapping)
    request_repository = _require_repository(request_mapping)
    if not _same_repository(repository, request_repository):
        raise ValidationError("Evidence request repository must match snapshot repository.")

    round_number = request_mapping.get("round")
    if (
        not isinstance(round_number, int)
        or isinstance(round_number, bool)
        or round_number not in {1, 2}
    ):
        raise ValidationError("Evidence request round must be 1 or 2.")
    _validate_expansion_round(snapshot_mapping, round_number)

    raw_requests = _require_list(request_mapping, "requests")
    if len(raw_requests) > 25:
        raise ValidationError("Evidence request documents may contain at most 25 requests per round.")

    open_issues = set(_require_unique_int_list(snapshot_mapping, "openIssues"))
    evidence_index = _load_snapshot_evidence(snapshot_mapping)
    normalized_requests: list[dict[str, Any]] = []
    seen_requests: set[tuple[object, ...]] = set()
    source_counts: dict[int, int] = {}
    canonical_count = 0

    for raw_request in raw_requests:
        request = _require_mapping(raw_request, "evidence request")
        request_type = _require_nonempty_string(request, "type")
        if request_type not in EVIDENCE_REQUEST_TYPES:
            raise ValidationError(f"Unsupported evidence request type: {request_type}.")

        allowed_fields = {
            "type",
            "sourceIssueNumber",
            "evidenceId",
            "decisionGate",
            "reason",
        }
        if request_type == "canonical-search":
            allowed_fields.add("factField")
        _require_only_fields(request, allowed_fields, f"{request_type} evidence request")

        source_issue_number = _require_positive_int(request, "sourceIssueNumber")
        if source_issue_number not in open_issues:
            raise ValidationError(
                f"Evidence request source issue {source_issue_number} must be open in the snapshot."
            )
        evidence_id = _require_nonempty_string(request, "evidenceId")
        record = evidence_index.get(evidence_id)
        if record is None:
            raise ValidationError(f"Evidence request cites unknown evidence ID: {evidence_id}.")
        if not _is_scoped_to_decision_issue(
            evidence_id,
            record,
            source_issue_number,
            repository,
        ):
            raise ValidationError(
                f"Evidence {evidence_id} is not deterministically scoped to source issue "
                f"{source_issue_number}."
            )

        decision_gate = _require_nonempty_string(request, "decisionGate")
        if decision_gate not in EVIDENCE_REQUEST_DECISION_GATES:
            raise ValidationError(f"Unsupported evidence request decision gate: {decision_gate}.")
        reason = _require_nonempty_string(request, "reason")
        normalized: dict[str, Any] = {
            "type": request_type,
            "sourceIssueNumber": source_issue_number,
            "evidenceId": evidence_id,
            "decisionGate": decision_gate,
            "reason": reason,
        }

        identity_suffix: tuple[object, ...] = ()
        if request_type == "issue-reference":
            _validate_issue_reference_request(evidence_id, record)
        elif request_type == "workflow-run":
            _validate_workflow_run_request(evidence_id, record, decision_gate)
        elif request_type == "canonical-search":
            fact_field = _require_nonempty_string(request, "factField")
            fact_value, fact_normalized = _derive_canonical_fact(
                evidence_id,
                record,
                source_issue_number,
                repository,
                fact_field,
            )
            normalized.update(
                {
                    "factField": fact_field,
                    "factValue": fact_value,
                    "factNormalized": fact_normalized,
                }
            )
            identity_suffix = (fact_field, fact_value, fact_normalized)
            canonical_count += 1
        else:
            path = _derive_source_check_path(record, repository)
            normalized["path"] = path
            identity_suffix = (path,)

        normalized_identity = (
            source_issue_number,
            request_type,
            evidence_id,
            *identity_suffix,
        )
        if normalized_identity in seen_requests:
            raise ValidationError(
                f"Evidence request document contains a duplicate normalized request for "
                f"{evidence_id}."
            )
        seen_requests.add(normalized_identity)
        source_counts[source_issue_number] = source_counts.get(source_issue_number, 0) + 1
        normalized_requests.append(normalized)

    if canonical_count > 10:
        raise ValidationError(
            "Evidence request documents may contain at most 10 canonical searches per round."
        )
    if any(count > 5 for count in source_counts.values()):
        raise ValidationError("A source issue may have at most five requests per round.")

    return sorted(
        normalized_requests,
        key=lambda request: (
            request["sourceIssueNumber"],
            request["type"],
            request["evidenceId"],
            request.get("factField", ""),
            request.get("factValue", ""),
            request.get("factNormalized", ""),
            request.get("path", ""),
        ),
    )


def _require_only_fields(
    mapping: Mapping[str, Any],
    allowed_fields: set[str],
    field_name: str,
) -> None:
    unknown_fields = sorted(set(mapping) - allowed_fields)
    if unknown_fields:
        raise ValidationError(
            f"{field_name} contains unknown or forbidden field: {unknown_fields[0]}."
        )


def _validate_expansion_round(
    snapshot: Mapping[str, Any],
    requested_round: int,
) -> None:
    expansion_history = snapshot.get("expansions", [])
    if len(expansion_history) >= 2:
        raise ValidationError("At most two adaptive evidence expansion rounds are allowed.")

    expected_round = len(expansion_history) + 1
    if requested_round != expected_round:
        raise ValidationError(
            f"Evidence request round {requested_round} cannot consume this snapshot; "
            f"the next round is {expected_round}."
        )


def _validate_expansion_manifests(snapshot: Mapping[str, Any]) -> None:
    expansion_history = snapshot.get("expansions", [])
    if not isinstance(expansion_history, list):
        raise ValidationError("Snapshot expansions must be a list.")
    if len(expansion_history) > 2:
        raise ValidationError("At most two adaptive evidence expansion rounds are allowed.")

    repository = _require_repository(snapshot)
    for expected_round, raw_manifest in enumerate(expansion_history, start=1):
        manifest = _require_mapping(raw_manifest, "snapshot expansion manifest")
        _require_only_fields(
            manifest,
            {"round", "requests", "status", "errors"},
            "snapshot expansion manifest",
        )
        round_number = manifest.get("round")
        if (
            not isinstance(round_number, int)
            or isinstance(round_number, bool)
            or round_number != expected_round
        ):
            raise ValidationError("Snapshot expansion rounds must be sequential from round 1.")

        status = _require_nonempty_string(manifest, "status")
        if status not in EXPANSION_STATUSES:
            raise ValidationError(f"Unsupported snapshot expansion status: {status}.")

        raw_requests = _require_list(manifest, "requests")
        if len(raw_requests) > 25:
            raise ValidationError(
                "Snapshot expansion manifests may contain at most 25 requests per round."
            )
        requests: list[dict[str, Any]] = []
        request_identities: set[tuple[object, ...]] = set()
        request_error_keys: set[tuple[str, int, str]] = set()
        source_counts: dict[int, int] = {}
        canonical_count = 0
        for raw_request in raw_requests:
            request, identity = _validate_expansion_request(
                raw_request,
                repository,
            )
            if identity in request_identities:
                raise ValidationError(
                    f"Snapshot expansion manifest contains a duplicate normalized request "
                    f"for {request['evidenceId']}."
                )
            request_identities.add(identity)
            request_error_keys.add(
                (
                    request["type"],
                    request["sourceIssueNumber"],
                    request["evidenceId"],
                )
            )
            source_issue_number = request["sourceIssueNumber"]
            source_counts[source_issue_number] = source_counts.get(source_issue_number, 0) + 1
            if request["type"] == "canonical-search":
                canonical_count += 1
            requests.append(request)
        if canonical_count > 10:
            raise ValidationError(
                "Snapshot expansion manifests may contain at most 10 canonical searches per round."
            )
        if any(count > 5 for count in source_counts.values()):
            raise ValidationError(
                "A source issue may have at most five expansion requests per round."
            )
        if requests != sorted(requests, key=_expansion_request_sort_key):
            raise ValidationError(
                "Snapshot expansion requests must use normalized deterministic ordering."
            )

        raw_errors = _require_list(manifest, "errors")
        for raw_error in raw_errors:
            error = _require_mapping(raw_error, "snapshot expansion error")
            _require_only_fields(
                error,
                {
                    "requestType",
                    "sourceIssueNumber",
                    "evidenceId",
                    "stage",
                    "endpoint",
                    "message",
                    "effect",
                },
                "snapshot expansion error",
            )
            request_type = _require_nonempty_string(error, "requestType")
            if request_type not in EVIDENCE_REQUEST_TYPES:
                raise ValidationError(
                    f"Unsupported snapshot expansion error request type: {request_type}."
                )
            source_issue_number = _require_positive_int(error, "sourceIssueNumber")
            evidence_id = _require_nonempty_string(error, "evidenceId")
            for field_name in ("stage", "endpoint", "message", "effect"):
                _require_nonempty_string(error, field_name)
            if (request_type, source_issue_number, evidence_id) not in request_error_keys:
                raise ValidationError(
                    "Snapshot expansion errors must match a request in the same manifest."
                )
        if status == "complete" and raw_errors:
            raise ValidationError(
                "A complete snapshot expansion manifest cannot contain errors."
            )


def _validate_expansion_request(
    raw_request: object,
    repository: str,
) -> tuple[dict[str, Any], tuple[object, ...]]:
    request = _require_mapping(raw_request, "snapshot expansion request")
    request_type = _require_nonempty_string(request, "type")
    if request_type not in EVIDENCE_REQUEST_TYPES:
        raise ValidationError(f"Unsupported evidence request type: {request_type}.")

    allowed_fields = {
        "type",
        "sourceIssueNumber",
        "evidenceId",
        "decisionGate",
        "reason",
    }
    if request_type == "canonical-search":
        allowed_fields.update({"factField", "factValue", "factNormalized"})
    elif request_type == "source-check":
        allowed_fields.add("path")
    _require_only_fields(
        request,
        allowed_fields,
        f"snapshot {request_type} expansion request",
    )

    source_issue_number = _require_positive_int(request, "sourceIssueNumber")
    evidence_id = _require_nonempty_string(request, "evidenceId")
    decision_gate = _require_nonempty_string(request, "decisionGate")
    if decision_gate not in EVIDENCE_REQUEST_DECISION_GATES:
        raise ValidationError(f"Unsupported evidence request decision gate: {decision_gate}.")
    reason = _require_nonempty_string(request, "reason")
    normalized: dict[str, Any] = {
        "type": request_type,
        "sourceIssueNumber": source_issue_number,
        "evidenceId": evidence_id,
        "decisionGate": decision_gate,
        "reason": reason,
    }
    identity_suffix: tuple[object, ...] = ()

    if request_type == "issue-reference":
        if (
            _ISSUE_ID_RE.fullmatch(evidence_id) is None
            and _EXTERNAL_ISSUE_ID_RE.fullmatch(evidence_id) is None
            and _PR_ID_RE.fullmatch(evidence_id) is None
            and _EXTERNAL_PR_ID_RE.fullmatch(evidence_id) is None
        ):
            raise ValidationError(
                "Snapshot issue-reference expansion requests must cite an issue or pull request ID."
            )
    elif request_type == "workflow-run":
        run_match = _RUN_ID_RE.fullmatch(evidence_id)
        if run_match is None or run_match.group("attempt") is not None:
            raise ValidationError(
                "Snapshot workflow-run expansion requests must cite a top-level workflow run."
            )
    elif request_type == "canonical-search":
        if not _is_exact_decision_issue_id(
            evidence_id,
            source_issue_number,
            repository,
        ):
            raise ValidationError(
                "Snapshot canonical-search expansion requests must cite the source issue "
                "in the snapshot repository."
            )
        fact_field = _require_nonempty_string(request, "factField")
        fact_value = _require_nonempty_string(request, "factValue")
        fact_normalized = _require_nonempty_string(request, "factNormalized")
        normalized.update(
            {
                "factField": fact_field,
                "factValue": fact_value,
                "factNormalized": fact_normalized,
            }
        )
        identity_suffix = (fact_field, fact_value, fact_normalized)
    else:
        if not _is_supported_source_check_manifest_id(evidence_id, repository):
            raise ValidationError(
                "Snapshot source-check expansion requests must cite supported evidence "
                "from the snapshot repository."
            )
        path = _require_nonempty_string(request, "path")
        _validate_repository_relative_path(path)
        normalized["path"] = path
        identity_suffix = (path,)

    identity = (
        source_issue_number,
        request_type,
        evidence_id,
        *identity_suffix,
    )
    return normalized, identity


def _is_supported_source_check_manifest_id(
    evidence_id: str,
    repository: str,
) -> bool:
    if _SOURCE_ID_RE.fullmatch(evidence_id) is not None:
        return True
    for pattern in (_PR_ID_RE, _COMMIT_ID_RE):
        if pattern.fullmatch(evidence_id) is not None:
            return True
    for pattern in (_EXTERNAL_PR_ID_RE, _EXTERNAL_COMMIT_ID_RE):
        match = pattern.fullmatch(evidence_id)
        if match is not None:
            return _same_repository(match.group("repository"), repository)
    run_match = _RUN_ID_RE.fullmatch(evidence_id)
    return (
        (run_match is not None and run_match.group("attempt") is not None)
        or _RUN_CHECK_ID_RE.fullmatch(evidence_id) is not None
    )


def _expansion_request_sort_key(request: Mapping[str, Any]) -> tuple[object, ...]:
    return (
        request["sourceIssueNumber"],
        request["type"],
        request["evidenceId"],
        request.get("factField", ""),
        request.get("factValue", ""),
        request.get("factNormalized", ""),
        request.get("path", ""),
    )


def _validate_issue_reference_request(
    evidence_id: str,
    record: Mapping[str, Any],
) -> None:
    if record["kind"] not in {"issue-event", "pull-request"}:
        raise ValidationError(
            "issue-reference requests must cite issue-event or pull-request evidence."
        )
    if (
        _ISSUE_ID_RE.fullmatch(evidence_id) is None
        and _EXTERNAL_ISSUE_ID_RE.fullmatch(evidence_id) is None
        and _PR_ID_RE.fullmatch(evidence_id) is None
        and _EXTERNAL_PR_ID_RE.fullmatch(evidence_id) is None
    ):
        raise ValidationError("issue-reference requests must cite an issue or pull request ID.")
    if not _record_needs_issue_detail(record):
        raise ValidationError(
            f"Evidence {evidence_id} already contains available enriched issue detail."
        )


def _validate_workflow_run_request(
    evidence_id: str,
    record: Mapping[str, Any],
    decision_gate: str,
) -> None:
    match = _RUN_ID_RE.fullmatch(evidence_id)
    if (
        record["kind"] != "workflow-run"
        or match is None
        or match.group("attempt") is not None
    ):
        raise ValidationError("workflow-run requests must cite a top-level workflow run.")
    target_repository = record["payload"].get("targetRepository")
    if (
        target_repository is not None
        and (
            not isinstance(target_repository, str)
            or _REPOSITORY_RE.fullmatch(target_repository) is None
        )
    ):
        raise ValidationError(
            "workflow-run evidence must contain a valid grounded target repository."
        )
    if not _record_needs_workflow_detail(record, decision_gate):
        raise ValidationError(
            f"Evidence {evidence_id} already contains the available workflow-run detail "
            f"required for {decision_gate}."
        )


def _record_needs_issue_detail(record: Mapping[str, Any]) -> bool:
    availability = record["availability"]
    if availability in {"partial", "not-enriched"}:
        return True
    if availability != "available":
        return False
    payload = record["payload"]
    if record["kind"] == "pull-request":
        return not any(
            field_name in payload
            for field_name in ("state", "mergedAt", "mergeCommitSha", "base", "head", "files")
        )
    return not any(
        field_name in payload
        for field_name in ("title", "body", "createdAt", "updatedAt", "closedAt", "labels")
    )


def _record_needs_workflow_detail(
    record: Mapping[str, Any],
    decision_gate: str,
) -> bool:
    availability = record["availability"]
    if availability != "available":
        return True
    payload = record["payload"]
    conclusion = payload.get("conclusion")
    if decision_gate in {
        "no-newer-matching-failure",
        "no-recent-matching-failure",
    }:
        return not _has_rigorous_recent_history(payload)
    if decision_gate in {"post-fix-green", "recovery"}:
        return not (
            conclusion == "success"
            or _has_rigorous_recent_history(payload)
        )
    return not (
        isinstance(payload.get("status"), str)
        and bool(payload["status"])
        and isinstance(conclusion, str)
        and bool(conclusion)
        and isinstance(payload.get("jobs"), list)
    )


def _derive_canonical_fact(
    evidence_id: str,
    record: Mapping[str, Any],
    source_issue_number: int,
    repository: str,
    fact_field: str,
) -> tuple[str, str]:
    if (
        record["kind"] != "issue-event"
        or record["availability"] != "available"
        or not _is_exact_decision_issue_id(
            evidence_id,
            source_issue_number,
            repository,
        )
    ):
        raise ValidationError(
            "canonical-search requests must cite the source issue's own available issue-event."
        )

    facts = record["payload"].get("facts")
    if not isinstance(facts, list):
        raise ValidationError(
            f"Canonical search fact field {fact_field} is not present in issue evidence."
        )
    candidates: set[tuple[str, str]] = set()
    for fact in facts:
        if not isinstance(fact, Mapping) or fact.get("field") != fact_field:
            continue
        raw_value = fact.get("raw", fact.get("value"))
        normalized_value = fact.get("normalized")
        if (
            isinstance(raw_value, str)
            and raw_value.strip()
            and isinstance(normalized_value, str)
            and normalized_value.strip()
        ):
            candidates.add((raw_value, normalized_value))
    if not candidates:
        raise ValidationError(
            f"Canonical search fact field {fact_field} is not present as an exact factual tuple."
        )
    if len(candidates) != 1:
        raise ValidationError(
            f"Canonical search fact field {fact_field} is ambiguous for source issue "
            f"{source_issue_number}."
        )
    return next(iter(candidates))


def _derive_source_check_path(
    record: Mapping[str, Any],
    repository: str,
) -> str:
    if record["kind"] not in {
        "source-path",
        "pull-request",
        "commit",
        "workflow-job",
        "workflow-log",
    }:
        raise ValidationError(
            "source-check requests must cite source, pull request, commit, job, annotation, "
            "or log evidence."
        )
    payload = record["payload"]
    target_repository = payload.get("targetRepository")
    if (
        target_repository is not None
        and (
            not isinstance(target_repository, str)
            or _REPOSITORY_RE.fullmatch(target_repository) is None
            or not _same_repository(target_repository, repository)
        )
    ):
        raise ValidationError(
            "source-check evidence must target the snapshot repository checkout."
        )

    paths: set[str] = set()
    direct_path = payload.get("path")
    if isinstance(direct_path, str) and direct_path:
        paths.add(direct_path)
    if record["kind"] == "pull-request":
        files = payload.get("files")
        if isinstance(files, list):
            for item in files:
                if isinstance(item, Mapping):
                    path = item.get("path")
                    if isinstance(path, str) and path:
                        paths.add(path)
    if record["kind"] == "commit":
        changed_paths = payload.get("changedPaths")
        if isinstance(changed_paths, list):
            paths.update(
                path
                for path in changed_paths
                if isinstance(path, str) and path
            )

    if len(paths) != 1:
        raise ValidationError(
            "source-check evidence must contain exactly one derivable affected path."
        )
    path = next(iter(paths))
    _validate_repository_relative_path(path)
    return path


def _validate_repository_relative_path(path: str) -> None:
    parsed = PurePosixPath(path)
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or not parsed.parts
        or parsed.is_absolute()
        or any(part in {"", ".", ".."} for part in parsed.parts)
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
        or re.match(r"^[A-Za-z]:", path) is not None
    ):
        raise ValidationError("source-check affected path must be a safe repository-relative path.")


def _load_assessment_candidates(
    assessment: object,
    *,
    repository: str,
    collected_at: str,
    open_issues: Sequence[int],
) -> dict[int, Mapping[str, Any]]:
    assessment_mapping = _require_mapping(assessment, "assessment")
    _require_exact_int(assessment_mapping, "schemaVersion", 1)
    if not _same_repository(_require_repository(assessment_mapping), repository):
        raise ValidationError("Assessment repository must match snapshot repository.")
    if _require_nonempty_string(assessment_mapping, "sourceCollectedAt") != collected_at:
        raise ValidationError("Assessment sourceCollectedAt must match snapshot collectedAt.")
    max_bundle_records = _require_positive_int(assessment_mapping, "maxBundleRecords")
    candidates = _require_list(assessment_mapping, "issues")
    result: dict[int, Mapping[str, Any]] = {}
    for value in candidates:
        candidate = _require_mapping(value, "assessment candidate")
        issue_number = _require_positive_int(candidate, "issueNumber")
        if issue_number in result:
            raise ValidationError(f"Duplicate assessment candidate for issue {issue_number}.")
        if issue_number not in open_issues:
            raise ValidationError(
                f"Assessment candidate issue {issue_number} is not open in the snapshot."
            )
        state = _require_nonempty_string(candidate, "candidateState")
        action = _require_nonempty_string(candidate, "candidateAction")
        if state not in STATES:
            raise ValidationError(f"Unsupported assessment candidate state: {state}.")
        if action not in ACTIONS:
            raise ValidationError(f"Unsupported assessment candidate action: {action}.")
        allowed_decisions = _require_mapping_list(
            candidate,
            "allowedDecisions",
            "allowed decision",
        )
        if not allowed_decisions:
            raise ValidationError(
                f"Assessment candidate for issue {issue_number} must allow a decision."
            )
        parsed_allowed: set[tuple[str, str]] = set()
        for allowed in allowed_decisions:
            allowed_state = _require_nonempty_string(allowed, "state")
            allowed_action = _require_nonempty_string(allowed, "action")
            if (
                allowed_state not in STATES
                or allowed_action not in ACTIONS
                or allowed_action not in VALID_STATE_ACTIONS[allowed_state]
            ):
                raise ValidationError(
                    f"Assessment candidate for issue {issue_number} contains invalid "
                    f"allowed decision {allowed_state}/{allowed_action}."
                )
            parsed_allowed.add((allowed_state, allowed_action))
        if (state, action) not in parsed_allowed:
            raise ValidationError(
                f"Assessment candidate for issue {issue_number} does not allow its own decision."
            )
        _require_bool(candidate, "automationEligible")
        _require_bool(candidate, "approvalRequired")
        bundle = _require_mapping_list(candidate, "evidenceBundle", "assessment evidence")
        if len(bundle) > max_bundle_records:
            raise ValidationError(
                f"Assessment candidate for issue {issue_number} exceeds its evidence bundle cap."
            )
        bundle_ids: set[str] = set()
        for item in bundle:
            evidence_id = _require_nonempty_string(item, "id")
            if evidence_id in bundle_ids:
                raise ValidationError(
                    f"Assessment candidate for issue {issue_number} repeats evidence {evidence_id}."
                )
            bundle_ids.add(evidence_id)
        result[issue_number] = candidate
    if set(result) != set(open_issues):
        missing = sorted(set(open_issues) - set(result))
        raise ValidationError(f"Missing assessment candidate for open issue {missing[0]}.")
    return result


def _require_mapping_list(
    mapping: Mapping[str, Any],
    key: str,
    item_name: str,
) -> list[Mapping[str, Any]]:
    return [
        _require_mapping(item, item_name)
        for item in _require_list(mapping, key)
    ]


def _validate_bounded_assessment_evidence(
    issue_number: int,
    candidate: Mapping[str, Any],
    *evidence_buckets: Sequence[object],
) -> None:
    bundle_ids = {
        _require_nonempty_string(item, "id")
        for item in _require_mapping_list(
            candidate,
            "evidenceBundle",
            "assessment evidence",
        )
    }
    cited_ids = _evidence_ids_in_decision_buckets(*evidence_buckets)
    outside_bundle = sorted(cited_ids - bundle_ids)
    if outside_bundle:
        raise ValidationError(
            f"Decision for issue {issue_number} cites evidence {outside_bundle[0]} "
            "outside its bounded assessment bundle."
        )


def validate_report(
    snapshot: object,
    report: object,
    *,
    assessment: object | None = None,
) -> None:
    snapshot_mapping = _require_mapping(snapshot, "snapshot")
    report_mapping = _require_mapping(report, "report")

    _require_exact_int(snapshot_mapping, "schemaVersion", 1)
    _require_exact_int(report_mapping, "schemaVersion", 1)

    repository = _require_repository(snapshot_mapping)
    if not _same_repository(_require_repository(report_mapping), repository):
        raise ValidationError("Report repository must match snapshot repository.")

    open_issues = _require_unique_int_list(snapshot_mapping, "openIssues")
    evidence_index = _load_snapshot_evidence(snapshot_mapping)
    assessment_candidates = (
        _load_assessment_candidates(
            assessment,
            repository=repository,
            collected_at=_require_nonempty_string(snapshot_mapping, "collectedAt"),
            open_issues=open_issues,
        )
        if assessment is not None
        else None
    )
    decisions = _require_list(report_mapping, "decisions")
    if len(decisions) != len(open_issues):
        missing = sorted(set(open_issues) - _decision_issue_numbers(decisions))
        if missing:
            raise ValidationError(f"Missing decision for open issue {missing[0]}.")
        raise ValidationError(
            f"Expected exactly one decision per open issue, found {len(decisions)} for {len(open_issues)} open issues."
        )

    decision_issue_numbers: set[int] = set()
    for decision in decisions:
        decision_mapping = _require_mapping(decision, "decision")
        issue_number = _require_positive_int(decision_mapping, "issueNumber")
        if issue_number in decision_issue_numbers:
            raise ValidationError(f"Duplicate decision for issue {issue_number}.")
        decision_issue_numbers.add(issue_number)
        if issue_number not in open_issues:
            raise ValidationError(f"Decision issue {issue_number} is not open in the snapshot.")

        issue_url = _require_nonempty_string(decision_mapping, "issueUrl")
        expected_issue_url = f"https://github.com/{repository}/issues/{issue_number}"
        if not _is_issue_url_for_repository(issue_url, repository, issue_number):
            raise ValidationError(f"Decision issueUrl must be {expected_issue_url}.")
        issue_kind = _require_nonempty_string(decision_mapping, "issueKind")
        if issue_kind not in ISSUE_KINDS:
            raise ValidationError(f"Unsupported issueKind: {issue_kind}.")

        state = _require_nonempty_string(decision_mapping, "state")
        if state not in STATES:
            raise ValidationError(f"Unsupported state: {state}.")

        proposed_action = _require_nonempty_string(decision_mapping, "proposedAction")
        if proposed_action not in ACTIONS:
            raise ValidationError(f"Unsupported proposedAction: {proposed_action}.")

        if proposed_action not in VALID_STATE_ACTIONS[state]:
            raise ValidationError(f"Action {proposed_action} is not valid for state {state}.")
        assessment_candidate = (
            assessment_candidates[issue_number]
            if assessment_candidates is not None
            else None
        )
        if assessment_candidate is not None:
            allowed_decisions = {
                (
                    _require_nonempty_string(item, "state"),
                    _require_nonempty_string(item, "action"),
                )
                for item in _require_mapping_list(
                    assessment_candidate,
                    "allowedDecisions",
                    "allowed decision",
                )
            }
            if (state, proposed_action) not in allowed_decisions:
                raise ValidationError(
                    f"Decision {state}/{proposed_action} for issue {issue_number} "
                    "is not allowed by deterministic candidate."
                )
        if proposed_action in {"close-as-tracked", "open-dedicated-issue"} and issue_kind != "incident":
            raise ValidationError(f"Action {proposed_action} is only valid when issueKind is incident.")

        confidence = _require_nonempty_string(decision_mapping, "confidence")
        if confidence not in CONFIDENCE:
            raise ValidationError(f"Unsupported confidence: {confidence}.")

        _require_nonempty_string(decision_mapping, "summary")
        _require_nonempty_string(decision_mapping, "reasoning")
        evidence_refs = _require_list(decision_mapping, "evidence")
        contradictory_refs = _require_list(decision_mapping, "contradictoryEvidence")
        missing_refs = _require_list(decision_mapping, "missingEvidence")
        next_condition = _require_mapping(decision_mapping.get("nextCondition"), "nextCondition")
        _require_next_condition(next_condition)
        _validate_suggested_owners(_require_list(decision_mapping, "suggestedOwners"))
        related_issues = _require_list(decision_mapping, "relatedIssues")
        _require_bool(decision_mapping, "changedSincePreviousRun")

        _validate_evidence_bucket_exclusivity(
            issue_number,
            evidence_refs,
            contradictory_refs,
            missing_refs,
        )
        _validate_evidence_refs(evidence_refs, evidence_index)
        _validate_evidence_refs(contradictory_refs, evidence_index)
        _validate_evidence_refs(missing_refs, evidence_index)
        if assessment_candidate is not None:
            _validate_bounded_assessment_evidence(
                issue_number,
                assessment_candidate,
                evidence_refs,
                contradictory_refs,
                missing_refs,
            )
        _validate_related_issues(related_issues, issue_number, repository)
        _validate_decision_scoped_completeness(
            proposed_action,
            issue_number,
            repository,
            evidence_index,
            evidence_refs,
            contradictory_refs,
            missing_refs,
        )

        if proposed_action == "close":
            _validate_close_roles(
                decision_mapping,
                repository,
                evidence_index,
                evidence_refs,
                contradictory_refs,
                missing_refs,
            )
        elif proposed_action == "close-resolved":
            _validate_close_resolved_roles(
                decision_mapping,
                repository,
                evidence_index,
                evidence_refs,
                contradictory_refs,
                missing_refs,
            )
        elif proposed_action == "close-stale":
            _validate_close_stale_roles(
                decision_mapping,
                repository,
                evidence_index,
                evidence_refs,
                contradictory_refs,
                missing_refs,
            )
        elif proposed_action == "close-as-tracked":
            _validate_close_as_tracked_roles(
                decision_mapping,
                repository,
                evidence_index,
                evidence_refs,
                contradictory_refs,
                missing_refs,
                related_issues,
            )
        elif proposed_action == "open-dedicated-issue":
            _validate_open_dedicated_issue_roles(
                decision_mapping,
                repository,
                evidence_index,
                evidence_refs,
                contradictory_refs,
                missing_refs,
                related_issues,
            )
        elif proposed_action == "merge-duplicate":
            _validate_merge_duplicate_roles(
                decision_mapping,
                repository,
                evidence_index,
                evidence_refs,
                contradictory_refs,
                missing_refs,
                related_issues,
            )
        elif proposed_action == "open-regression":
            _validate_open_regression_roles(
                decision_mapping,
                repository,
                evidence_index,
                evidence_refs,
                contradictory_refs,
                missing_refs,
                related_issues,
            )

    if decision_issue_numbers != set(open_issues):
        missing = sorted(set(open_issues) - decision_issue_numbers)
        if missing:
            raise ValidationError(f"Missing decision for open issue {missing[0]}.")
        extra = sorted(decision_issue_numbers - set(open_issues))
        raise ValidationError(f"Unexpected decision for non-open issue {extra[0]}.")


def _validate_close_roles(
    decision: Mapping[str, Any],
    repository: str,
    evidence_index: Mapping[str, Mapping[str, Any]],
    evidence_refs: Sequence[object],
    contradictory_refs: Sequence[object],
    missing_refs: Sequence[object],
) -> None:
    issue_number = _require_positive_int(decision, "issueNumber")
    role_set = _current_role_set(
        evidence_index,
        evidence_refs,
        issue_number=issue_number,
        repository=repository,
    )
    contradictory_roles = _current_role_set(
        evidence_index,
        contradictory_refs,
        issue_number=issue_number,
        repository=repository,
    )
    missing_roles = _current_role_set(
        evidence_index,
        missing_refs,
        issue_number=issue_number,
        repository=repository,
    )
    _require_close_resolution_signal(role_set, issue_number)
    _require_close_post_fix_green(role_set, issue_number)
    if "no-newer-matching-failure" not in role_set:
        raise ValidationError(
            f"High-risk close requires no-newer-matching-failure evidence associated with issue {issue_number}."
        )
    _reject_newer_failure(role_set, contradictory_roles, missing_roles, "High-risk close")


def _validate_close_resolved_roles(
    decision: Mapping[str, Any],
    repository: str,
    evidence_index: Mapping[str, Mapping[str, Any]],
    evidence_refs: Sequence[object],
    contradictory_refs: Sequence[object],
    missing_refs: Sequence[object],
) -> None:
    issue_number = _require_positive_int(decision, "issueNumber")
    role_set = _current_role_set(
        evidence_index,
        evidence_refs,
        issue_number=issue_number,
        repository=repository,
    )
    contradictory_roles = _current_role_set(
        evidence_index,
        contradictory_refs,
        issue_number=issue_number,
        repository=repository,
    )
    missing_roles = _current_role_set(
        evidence_index,
        missing_refs,
        issue_number=issue_number,
        repository=repository,
    )
    _require_close_resolution_signal(role_set, issue_number)
    _require_close_post_fix_green(role_set, issue_number)
    if "no-newer-matching-failure" not in role_set:
        raise ValidationError(
            "High-risk close-resolved requires no-newer-matching-failure "
            f"evidence associated with issue {issue_number}."
        )
    _reject_newer_failure(role_set, contradictory_roles, missing_roles, "High-risk close-resolved")


def _validate_close_stale_roles(
    decision: Mapping[str, Any],
    repository: str,
    evidence_index: Mapping[str, Mapping[str, Any]],
    evidence_refs: Sequence[object],
    contradictory_refs: Sequence[object],
    missing_refs: Sequence[object],
) -> None:
    issue_number = _require_positive_int(decision, "issueNumber")
    role_set = _current_role_set(
        evidence_index,
        evidence_refs,
        issue_number=issue_number,
        repository=repository,
    )
    contradictory_roles = _current_role_set(
        evidence_index,
        contradictory_refs,
        issue_number=issue_number,
        repository=repository,
    )
    missing_roles = _current_role_set(
        evidence_index,
        missing_refs,
        issue_number=issue_number,
        repository=repository,
    )
    if "obsolete-surface" not in role_set:
        raise ValidationError(
            f"High-risk close-stale requires obsolete-surface evidence associated with issue {issue_number}."
        )
    if "no-recent-matching-failure" not in role_set:
        raise ValidationError(
            "High-risk close-stale requires no-recent-matching-failure "
            f"evidence associated with issue {issue_number}."
        )
    _reject_newer_failure(role_set, contradictory_roles, missing_roles, "High-risk close-stale")


def _validate_close_as_tracked_roles(
    decision: Mapping[str, Any],
    repository: str,
    evidence_index: Mapping[str, Mapping[str, Any]],
    evidence_refs: Sequence[object],
    contradictory_refs: Sequence[object],
    missing_refs: Sequence[object],
    related_issues: Sequence[object],
) -> None:
    issue_number = _require_positive_int(decision, "issueNumber")
    role_set = _current_role_set(
        evidence_index,
        evidence_refs,
        issue_number=issue_number,
        repository=repository,
    )
    if "canonical-issue" not in role_set:
        raise ValidationError(
            f"High-risk close-as-tracked requires canonical-issue evidence associated with issue {issue_number}."
        )
    canonical_targets = _relationship_targets(
        related_issues,
        repository,
        {"canonical-tracker", "exact-duplicate"},
    )
    _validate_canonical_issue_relationship_target(
        "High-risk close-as-tracked",
        repository,
        evidence_index,
        evidence_refs,
        [*contradictory_refs, *missing_refs],
        canonical_targets,
        "canonical-tracker or exact-duplicate relationship",
        issue_number,
    )


def _validate_open_dedicated_issue_roles(
    decision: Mapping[str, Any],
    repository: str,
    evidence_index: Mapping[str, Mapping[str, Any]],
    evidence_refs: Sequence[object],
    contradictory_refs: Sequence[object],
    missing_refs: Sequence[object],
    related_issues: Sequence[object],
) -> None:
    issue_number = _require_positive_int(decision, "issueNumber")
    role_set = _current_role_set(
        evidence_index,
        evidence_refs,
        issue_number=issue_number,
        repository=repository,
    )
    contradictory_roles = _current_role_set(
        evidence_index,
        contradictory_refs,
        issue_number=issue_number,
        repository=repository,
    )
    missing_roles = _current_role_set(
        evidence_index,
        missing_refs,
        issue_number=issue_number,
        repository=repository,
    )
    if "current-failing-run" not in role_set:
        raise ValidationError("High-risk open-dedicated-issue requires current-failing-run evidence.")
    if "canonical-search-complete" not in role_set:
        raise ValidationError("High-risk open-dedicated-issue requires canonical-search-complete evidence.")
    if "recurrence" not in role_set and "known-flaky-signature" not in role_set:
        raise ValidationError(
            "High-risk open-dedicated-issue requires recurrence or known-flaky-signature evidence."
        )
    if "canonical-issue" in role_set or "canonical-issue" in contradictory_roles or "canonical-issue" in missing_roles:
        raise ValidationError(
            "High-risk open-dedicated-issue cannot coexist with canonical-issue evidence in evidence, contradictoryEvidence, or missingEvidence."
        )
    blocking_relationships = {
        relationship["type"]
        for relationship in related_issues
        if isinstance(relationship, Mapping) and relationship.get("type") in {"canonical-tracker", "exact-duplicate"}
    }
    if blocking_relationships:
        blocking_relationships_list = ", ".join(sorted(blocking_relationships))
        raise ValidationError(
            "High-risk open-dedicated-issue cannot coexist with "
            f"{blocking_relationships_list} relationships in relatedIssues."
        )


def _require_close_resolution_signal(role_set: set[str], issue_number: int) -> None:
    if "merged-fix" not in role_set and "recovery" not in role_set:
        raise ValidationError(
            f"High-risk close requires merged-fix or recovery evidence associated with issue {issue_number}."
        )


def _require_close_post_fix_green(role_set: set[str], issue_number: int) -> None:
    if "post-fix-green" not in role_set:
        raise ValidationError(
            f"High-risk close requires post-fix-green evidence associated with issue {issue_number}."
        )


def _reject_newer_failure(
    role_set: set[str],
    contradictory_roles: set[str],
    missing_roles: set[str],
    action_name: str,
) -> None:
    if "newer-failure" in role_set or "newer-failure" in contradictory_roles or "newer-failure" in missing_roles:
        raise ValidationError(f"{action_name} cannot include newer-failure evidence.")


def _parse_issue_number_from_evidence_id(
    evidence_id: str,
    repository: str,
    role: str = "canonical-issue",
) -> tuple[str, int]:
    for pattern in (_ISSUE_ID_RE, _ISSUE_COMMENT_ID_RE, _ISSUE_EVENT_ID_RE):
        match = pattern.fullmatch(evidence_id)
        if match is not None:
            return repository, int(match.group("number"))

    external_issue_match = _EXTERNAL_ISSUE_ID_RE.fullmatch(evidence_id)
    if external_issue_match is not None:
        return external_issue_match.group("repository"), int(external_issue_match.group("number"))

    issue_label = "canonical issue number" if role == "canonical-issue" else "issue number"
    raise ValidationError(
        f"{role} evidence must use an issue ID that encodes the {issue_label}."
    )


def _validate_merge_duplicate_roles(
    decision: Mapping[str, Any],
    repository: str,
    evidence_index: Mapping[str, Mapping[str, Any]],
    evidence_refs: Sequence[object],
    contradictory_refs: Sequence[object],
    missing_refs: Sequence[object],
    related_issues: Sequence[object],
) -> None:
    issue_number = _require_positive_int(decision, "issueNumber")
    role_set = _current_role_set(
        evidence_index,
        evidence_refs,
        issue_number=issue_number,
        repository=repository,
    )
    if "canonical-issue" not in role_set:
        raise ValidationError("High-risk merge-duplicate requires canonical-issue evidence.")
    if not {"deterministic-marker", "normalized-facts"} & role_set:
        raise ValidationError(
            "High-risk merge-duplicate requires deterministic-marker or normalized-facts evidence."
        )
    exact_duplicate_targets = _relationship_targets(related_issues, repository, {"exact-duplicate"})
    _validate_canonical_issue_relationship_target(
        "High-risk merge-duplicate",
        repository,
        evidence_index,
        evidence_refs,
        [*contradictory_refs, *missing_refs],
        exact_duplicate_targets,
        "exact-duplicate relationship",
        issue_number,
    )


def _validate_canonical_issue_relationship_target(
    action_name: str,
    repository: str,
    evidence_index: Mapping[str, Mapping[str, Any]],
    evidence_refs: Sequence[object],
    blocker_refs: Sequence[object],
    relationship_targets: set[tuple[str, int]],
    relationship_name: str,
    issue_number: int,
) -> None:
    if not relationship_targets:
        raise ValidationError(f"{action_name} requires a {relationship_name}.")
    if len(relationship_targets) != 1:
        raise ValidationError(
            f"{action_name} relationships must identify the same targetRepository and targetIssueNumber."
        )
    relationship_target = next(iter(relationship_targets))
    canonical_targets = _current_issue_targets_for_role(
        evidence_index,
        evidence_refs,
        "canonical-issue",
        repository,
        issue_number,
    )
    if len(canonical_targets) != 1:
        raise ValidationError(
            f"{action_name} canonical-issue evidence must identify the same repository and issue number."
        )
    canonical_repository, canonical_issue_number = next(iter(canonical_targets))
    if _same_issue_target((canonical_repository, canonical_issue_number), relationship_target):
        _reject_conflicting_issue_identity_blockers(
            action_name,
            evidence_index,
            blocker_refs,
            "canonical-issue",
            repository,
            issue_number,
            relationship_target,
        )
        return
    if _same_issue_target((repository, canonical_issue_number), relationship_target):
        raise ValidationError(
            f"{action_name} canonical-issue evidence must stay in {repository}, got {canonical_repository}."
        )
    if relationship_target[1] == canonical_issue_number:
        raise ValidationError(
            f"{action_name} canonical-issue evidence targetRepository must match the {relationship_name}."
        )
    raise ValidationError(
        f"{action_name} canonical-issue evidence must identify the same targetIssueNumber as the {relationship_name}."
    )


def _relationship_targets(
    related_issues: Sequence[object],
    repository: str,
    relationship_types: set[str],
) -> set[tuple[str, int]]:
    targets: dict[tuple[str, int], tuple[str, int]] = {}
    for relationship in related_issues:
        if not isinstance(relationship, Mapping):
            continue
        if relationship.get("type") not in relationship_types:
            continue
        _add_issue_target(targets, _relationship_target(relationship, repository))
    return set(targets.values())


def _relationship_target(relationship: Mapping[str, Any], repository: str) -> tuple[str, int]:
    target_repository = (
        _require_repository_string(relationship, "targetRepository")
        if "targetRepository" in relationship
        else repository
    )
    return target_repository, _require_positive_int(relationship, "targetIssueNumber")


def _validate_open_regression_roles(
    decision: Mapping[str, Any],
    repository: str,
    evidence_index: Mapping[str, Mapping[str, Any]],
    evidence_refs: Sequence[object],
    contradictory_refs: Sequence[object],
    missing_refs: Sequence[object],
    related_issues: Sequence[object],
) -> None:
    issue_number = _require_positive_int(decision, "issueNumber")
    role_entries = _current_role_entries(
        evidence_index,
        evidence_refs,
        issue_number=issue_number,
        repository=repository,
    )
    required_roles = {"current-failing-run", "prior-resolved-episode", "normalized-cause"}
    if not required_roles.issubset(role_entries):
        raise ValidationError(
            "High-risk open-regression requires current-failing-run, prior-resolved-episode, and normalized-cause evidence."
        )
    normalized_causes: list[str] = []
    for role in ("current-failing-run", "prior-resolved-episode", "normalized-cause"):
        for evidence_id, record, evidence_ref in role_entries[role]:
            normalized_cause = _effective_evidence_normalized_cause(
                evidence_id,
                record,
                evidence_ref,
            )
            if normalized_cause is None:
                raise ValidationError(
                    f"{role} evidence must include a nonempty normalizedCause string."
                )
            normalized_causes.append(normalized_cause)
    if len(set(normalized_causes)) != 1:
        raise ValidationError(
            "High-risk open-regression requires matching normalizedCause values on current-failing-run, prior-resolved-episode, and normalized-cause evidence."
        )
    regression_targets = _relationship_targets(related_issues, repository, {"regression-of"})
    if not regression_targets:
        raise ValidationError("High-risk open-regression requires a regression-of relationship.")
    if len(regression_targets) != 1:
        raise ValidationError(
            "High-risk open-regression relationships must identify the same targetRepository and targetIssueNumber."
        )
    prior_targets = _current_prior_resolved_episode_targets(
        evidence_index,
        evidence_refs,
        repository,
        issue_number,
    )
    if len(prior_targets) != 1:
        raise ValidationError(
            "High-risk open-regression prior-resolved-episode evidence must identify the same repository and issue number."
        )
    regression_target = next(iter(regression_targets))
    prior_target = next(iter(prior_targets))
    if _same_issue_target(regression_target, prior_target):
        _reject_conflicting_prior_identity_blockers(
            evidence_index,
            [*contradictory_refs, *missing_refs],
            repository,
            issue_number,
            prior_target,
        )
        return
    if regression_target[1] == prior_target[1]:
        raise ValidationError(
            "High-risk open-regression prior-resolved-episode evidence targetRepository must match the regression-of relationship."
        )
    raise ValidationError(
        "High-risk open-regression prior-resolved-episode evidence must identify the same targetIssueNumber as the regression-of relationship."
    )


def _optional_evidence_role(mapping: Mapping[str, Any]) -> str | None:
    if "role" not in mapping:
        return None
    role = mapping["role"]
    if not isinstance(role, str) or not role.strip():
        raise ValidationError("Evidence role must be a nonempty string when present.")
    if role not in EVIDENCE_ROLES:
        raise ValidationError(f"Unsupported evidence role: {role}.")
    return role


def _optional_report_evidence_roles(mapping: Mapping[str, Any]) -> tuple[str, ...]:
    if "role" in mapping and "roles" in mapping:
        raise ValidationError("Evidence reference must specify only one of role or roles.")
    if "roles" not in mapping:
        role = _optional_evidence_role(mapping)
        return (role,) if role is not None else ()

    roles = mapping["roles"]
    if not isinstance(roles, list) or not roles:
        raise ValidationError("Evidence roles must be a nonempty list of unique supported roles.")

    validated: list[str] = []
    seen: set[str] = set()
    for role in roles:
        if not isinstance(role, str) or not role.strip():
            raise ValidationError("Evidence roles must contain nonempty strings.")
        if role not in EVIDENCE_ROLES:
            raise ValidationError(f"Unsupported evidence role: {role}.")
        if role in seen:
            raise ValidationError(f"Evidence roles must not contain duplicate role: {role}.")
        seen.add(role)
        validated.append(role)
    return tuple(validated)


def _optional_normalized_cause(mapping: Mapping[str, Any]) -> str | None:
    if "normalizedCause" not in mapping:
        return None
    normalized_cause = mapping["normalizedCause"]
    if not isinstance(normalized_cause, str) or not normalized_cause.strip():
        raise ValidationError("Evidence normalizedCause must be a nonempty string when present.")
    return normalized_cause


def _effective_evidence_roles(
    evidence_id: str,
    record: Mapping[str, Any],
    evidence_ref: Mapping[str, Any],
) -> tuple[str, ...]:
    snapshot_role = _optional_evidence_role(record["payload"])
    report_roles = _optional_report_evidence_roles(evidence_ref)
    if snapshot_role is None:
        return report_roles
    if report_roles and report_roles != (snapshot_role,):
        report_description = (
            f"role {report_roles[0]}"
            if len(report_roles) == 1
            else f"roles {list(report_roles)}"
        )
        raise ValidationError(
            f"Evidence {evidence_id} report {report_description} conflicts with snapshot role {snapshot_role}."
        )
    return (snapshot_role,)


def _effective_evidence_normalized_cause(
    evidence_id: str,
    record: Mapping[str, Any],
    evidence_ref: Mapping[str, Any],
) -> str | None:
    snapshot_normalized_cause = _optional_normalized_cause(record["payload"])
    report_normalized_cause = _optional_normalized_cause(evidence_ref)
    if (
        snapshot_normalized_cause is not None
        and report_normalized_cause is not None
        and snapshot_normalized_cause != report_normalized_cause
    ):
        raise ValidationError(
            f"Evidence {evidence_id} report normalizedCause {report_normalized_cause} "
            f"conflicts with snapshot normalizedCause {snapshot_normalized_cause}."
        )
    return (
        snapshot_normalized_cause
        if snapshot_normalized_cause is not None
        else report_normalized_cause
    )


def _current_role_set(
    evidence_index: Mapping[str, Mapping[str, Any]],
    evidence_refs: Sequence[object],
    *,
    issue_number: int,
    repository: str,
) -> set[str]:
    return set(
        _current_role_records(
            evidence_index,
            evidence_refs,
            issue_number=issue_number,
            repository=repository,
        )
    )


def _current_role_records(
    evidence_index: Mapping[str, Mapping[str, Any]],
    evidence_refs: Sequence[object],
    *,
    issue_number: int,
    repository: str,
) -> dict[str, list[Mapping[str, Any]]]:
    return {
        role: [record for _, record, _ in entries]
        for role, entries in _current_role_entries(
            evidence_index,
            evidence_refs,
            issue_number=issue_number,
            repository=repository,
        ).items()
    }


def _current_role_entries(
    evidence_index: Mapping[str, Mapping[str, Any]],
    evidence_refs: Sequence[object],
    *,
    issue_number: int,
    repository: str,
) -> dict[str, list[tuple[str, Mapping[str, Any], Mapping[str, Any]]]]:
    entries_by_role: dict[
        str,
        list[tuple[str, Mapping[str, Any], Mapping[str, Any]]],
    ] = {}
    for evidence_ref in evidence_refs:
        ref_mapping = _require_mapping(evidence_ref, "evidence reference")
        evidence_id = _require_nonempty_string(ref_mapping, "id")
        record = evidence_index[evidence_id]
        if not _is_current_available_record(record):
            continue
        if not _is_scoped_to_decision_issue(evidence_id, record, issue_number, repository):
            continue
        for role in _effective_evidence_roles(evidence_id, record, ref_mapping):
            if _role_has_factual_collection_proof(
                role,
                evidence_id,
                record,
                issue_number=issue_number,
                repository=repository,
            ):
                entries_by_role.setdefault(role, []).append((evidence_id, record, ref_mapping))
    return entries_by_role


def _role_has_factual_collection_proof(
    role: str,
    evidence_id: str,
    record: Mapping[str, Any],
    *,
    issue_number: int,
    repository: str,
) -> bool:
    payload = record["payload"]
    if role == "canonical-search-complete":
        if (
            record["kind"] != "issue-event"
            or not _is_exact_decision_issue_id(evidence_id, issue_number, repository)
            or not _canonical_search_payload_matches_decision_issue(
                payload,
                issue_number,
                repository,
            )
        ):
            return False
        supporting_search = payload.get("supportingSearch")
        return (
            isinstance(supporting_search, Mapping)
            and supporting_search.get("complete") is True
            and supporting_search.get("truncated") is False
            and isinstance(supporting_search.get("candidateIssueNumbers"), list)
        )
    if role in {"no-newer-matching-failure", "no-recent-matching-failure"}:
        return record["kind"] == "workflow-run" and _has_rigorous_recent_history(payload)
    if role == "post-fix-green":
        if record["kind"] == "workflow-job":
            return payload.get("conclusion") == "success"
        if record["kind"] != "workflow-run":
            return False
        if payload.get("conclusion") == "success":
            return True
        recent_history = payload.get("recentHistory")
        return (
            _has_rigorous_recent_history(payload)
            and isinstance(recent_history, list)
            and any(
                isinstance(history_run, Mapping)
                and history_run.get("conclusion") == "success"
                for history_run in recent_history
            )
        )
    if role == "obsolete-surface":
        return _has_deterministic_obsolete_surface_proof(record)
    return True


def _has_deterministic_obsolete_surface_proof(record: Mapping[str, Any]) -> bool:
    payload = record["payload"]
    checkout_commit = payload.get("checkoutCommit")
    removal_commit = payload.get("removalCommit")
    replacement_commit = payload.get("replacementCommit")
    replacement_path = payload.get("replacementPath")
    replacement_proof = (
        isinstance(replacement_commit, str)
        and _FULL_SHA_RE.fullmatch(replacement_commit) is not None
        and isinstance(replacement_path, str)
        and bool(replacement_path)
    )
    if replacement_proof:
        try:
            _validate_repository_relative_path(replacement_path)
        except ValidationError:
            replacement_proof = False

    return (
        record["kind"] == "source-path"
        and payload.get("exists") is False
        and payload.get("historyAmbiguous") is False
        and isinstance(checkout_commit, str)
        and _FULL_SHA_RE.fullmatch(checkout_commit) is not None
        and (
            (
                isinstance(removal_commit, str)
                and _FULL_SHA_RE.fullmatch(removal_commit) is not None
            )
            or replacement_proof
        )
    )


def _has_rigorous_recent_history(payload: Mapping[str, Any]) -> bool:
    return (
        payload.get("recentHistoryCollected") is True
        and isinstance(payload.get("recentHistory"), list)
        and isinstance(payload.get("recentHistoryTruncated"), bool)
        and payload.get("historyCoversSourceRun") is True
    )


def _canonical_search_payload_matches_decision_issue(
    payload: Mapping[str, Any],
    issue_number: int,
    repository: str,
) -> bool:
    if "number" in payload and _positive_int_value(payload["number"]) != issue_number:
        return False

    for field_name in ("repository", "targetRepository"):
        if field_name not in payload:
            continue
        payload_repository = payload[field_name]
        if (
            not isinstance(payload_repository, str)
            or _REPOSITORY_RE.fullmatch(payload_repository) is None
            or not _same_repository(payload_repository, repository)
        ):
            return False

    if "url" in payload:
        payload_url = payload["url"]
        if (
            not isinstance(payload_url, str)
            or not _is_issue_url_for_repository(payload_url, repository, issue_number)
        ):
            return False

    return True


def _is_exact_decision_issue_id(
    evidence_id: str,
    issue_number: int,
    repository: str,
) -> bool:
    compact_match = _ISSUE_ID_RE.fullmatch(evidence_id)
    if compact_match is not None:
        return int(compact_match.group("number")) == issue_number

    qualified_match = _EXTERNAL_ISSUE_ID_RE.fullmatch(evidence_id)
    return (
        qualified_match is not None
        and int(qualified_match.group("number")) == issue_number
        and _same_repository(qualified_match.group("repository"), repository)
    )


def _validate_decision_scoped_completeness(
    proposed_action: str,
    issue_number: int,
    repository: str,
    evidence_index: Mapping[str, Mapping[str, Any]],
    evidence_refs: Sequence[object],
    contradictory_refs: Sequence[object],
    missing_refs: Sequence[object],
) -> None:
    if proposed_action not in HIGH_RISK_ACTIONS:
        return

    cited_ids = _evidence_ids_in_decision_buckets(evidence_refs, contradictory_refs, missing_refs)
    for evidence_id, record in sorted(evidence_index.items()):
        if not _is_current_record(record):
            continue
        if not _is_scoped_to_decision_issue(evidence_id, record, issue_number, repository):
            continue
        if evidence_id not in cited_ids:
            role = record["payload"].get("role")
            role_description = f"{role} evidence" if role is not None else "evidence"
            raise ValidationError(
                f"Action {proposed_action} for issue {issue_number} omits decision-scoped "
                f"{role_description} {evidence_id}."
            )


def _evidence_ids_in_decision_buckets(*evidence_ref_lists: Sequence[object]) -> set[str]:
    evidence_ids: set[str] = set()
    for evidence_refs in evidence_ref_lists:
        for evidence_ref in evidence_refs:
            ref_mapping = _require_mapping(evidence_ref, "evidence reference")
            evidence_ids.add(_require_nonempty_string(ref_mapping, "id"))
    return evidence_ids


def _is_scoped_to_decision_issue(
    evidence_id: str,
    record: Mapping[str, Any],
    issue_number: int,
    repository: str,
) -> bool:
    compact_issue_match = _ISSUE_ID_RE.fullmatch(evidence_id)
    if compact_issue_match is not None and int(compact_issue_match.group("number")) == issue_number:
        return True

    qualified_issue_match = _EXTERNAL_ISSUE_ID_RE.fullmatch(evidence_id)
    if (
        qualified_issue_match is not None
        and int(qualified_issue_match.group("number")) == issue_number
        and _same_repository(qualified_issue_match.group("repository"), repository)
    ):
        return True

    payload = record["payload"]
    if _positive_int_value(payload.get("sourceIssueNumber")) == issue_number:
        return True

    referenced_by = payload.get("referencedBy")
    if not isinstance(referenced_by, list):
        return False

    # Collector references are emitted as:
    #   {"sourceIssueNumber": 123, "sourceEvidenceId": "issue:123", ...}
    # Ignore malformed association entries so unrelated payload shapes do not
    # make otherwise valid snapshots fail completeness validation.
    for reference in referenced_by:
        if not isinstance(reference, Mapping):
            continue
        if _positive_int_value(reference.get("sourceIssueNumber")) == issue_number:
            return True
    return False


def _positive_int_value(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    return value


def _current_issue_targets_for_role(
    evidence_index: Mapping[str, Mapping[str, Any]],
    evidence_refs: Sequence[object],
    role: str,
    repository: str,
    issue_number: int,
) -> set[tuple[str, int]]:
    targets: dict[tuple[str, int], tuple[str, int]] = {}
    for evidence_ref in evidence_refs:
        ref_mapping = _require_mapping(evidence_ref, "evidence reference")
        evidence_id = _require_nonempty_string(ref_mapping, "id")
        record = evidence_index[evidence_id]
        if not _is_current_available_record(record):
            continue
        if not _is_scoped_to_decision_issue(evidence_id, record, issue_number, repository):
            continue
        if role not in _effective_evidence_roles(evidence_id, record, ref_mapping):
            continue
        _add_issue_target(targets, _parse_issue_number_from_evidence_id(evidence_id, repository, role))
    return set(targets.values())


def _current_prior_resolved_episode_targets(
    evidence_index: Mapping[str, Mapping[str, Any]],
    evidence_refs: Sequence[object],
    repository: str,
    issue_number: int,
) -> set[tuple[str, int]]:
    targets: dict[tuple[str, int], tuple[str, int]] = {}
    for evidence_ref in evidence_refs:
        ref_mapping = _require_mapping(evidence_ref, "evidence reference")
        evidence_id = _require_nonempty_string(ref_mapping, "id")
        record = evidence_index[evidence_id]
        if not _is_current_available_record(record):
            continue
        if not _is_scoped_to_decision_issue(evidence_id, record, issue_number, repository):
            continue
        if "prior-resolved-episode" not in _effective_evidence_roles(
            evidence_id,
            record,
            ref_mapping,
        ):
            continue
        target = _try_parse_issue_number_from_evidence_id(
            evidence_id,
            repository,
            "prior-resolved-episode",
        )
        if target is not None:
            _add_issue_target(targets, target)
            continue
        payload = record["payload"]
        prior_repository = (
            _require_repository_string(payload, "priorRepository")
            if "priorRepository" in payload
            else repository
        )
        _add_issue_target(targets, (prior_repository, _require_positive_int(payload, "priorIssueNumber")))
    return set(targets.values())


def _reject_conflicting_issue_identity_blockers(
    action_name: str,
    evidence_index: Mapping[str, Mapping[str, Any]],
    evidence_refs: Sequence[object],
    role: str,
    repository: str,
    issue_number: int,
    expected_target: tuple[str, int],
) -> None:
    targets, has_unresolved_identity = _current_issue_identity_blockers(
        evidence_index,
        evidence_refs,
        role,
        repository,
        issue_number,
    )
    if has_unresolved_identity:
        raise ValidationError(
            f"{action_name} {role} evidence in contradictoryEvidence or missingEvidence must identify a repository and issue number."
        )
    if any(not _same_issue_target(target, expected_target) for target in targets):
        raise ValidationError(
            f"{action_name} contradictoryEvidence or missingEvidence contains conflicting {role} identity."
        )


def _current_issue_identity_blockers(
    evidence_index: Mapping[str, Mapping[str, Any]],
    evidence_refs: Sequence[object],
    role: str,
    repository: str,
    issue_number: int,
) -> tuple[set[tuple[str, int]], bool]:
    targets: dict[tuple[str, int], tuple[str, int]] = {}
    has_unresolved_identity = False
    for evidence_ref in evidence_refs:
        ref_mapping = _require_mapping(evidence_ref, "evidence reference")
        evidence_id = _require_nonempty_string(ref_mapping, "id")
        record = evidence_index[evidence_id]
        if not _is_current_record(record):
            continue
        if not _is_scoped_to_decision_issue(evidence_id, record, issue_number, repository):
            continue
        if role not in _effective_evidence_roles(evidence_id, record, ref_mapping):
            continue
        target = _try_parse_issue_number_from_evidence_id(evidence_id, repository, role)
        if target is None:
            has_unresolved_identity = True
            continue
        _add_issue_target(targets, target)
    return set(targets.values()), has_unresolved_identity


def _reject_conflicting_prior_identity_blockers(
    evidence_index: Mapping[str, Mapping[str, Any]],
    evidence_refs: Sequence[object],
    repository: str,
    issue_number: int,
    expected_target: tuple[str, int],
) -> None:
    targets, has_unresolved_identity = _current_prior_identity_blockers(
        evidence_index,
        evidence_refs,
        repository,
        issue_number,
    )
    if has_unresolved_identity:
        raise ValidationError(
            "High-risk open-regression prior-resolved-episode evidence in contradictoryEvidence or missingEvidence must identify a repository and issue number."
        )
    if any(not _same_issue_target(target, expected_target) for target in targets):
        raise ValidationError(
            "High-risk open-regression contradictoryEvidence or missingEvidence contains conflicting prior-resolved-episode identity."
        )


def _current_prior_identity_blockers(
    evidence_index: Mapping[str, Mapping[str, Any]],
    evidence_refs: Sequence[object],
    repository: str,
    issue_number: int,
) -> tuple[set[tuple[str, int]], bool]:
    targets: dict[tuple[str, int], tuple[str, int]] = {}
    has_unresolved_identity = False
    for evidence_ref in evidence_refs:
        ref_mapping = _require_mapping(evidence_ref, "evidence reference")
        evidence_id = _require_nonempty_string(ref_mapping, "id")
        record = evidence_index[evidence_id]
        if not _is_current_record(record):
            continue
        if not _is_scoped_to_decision_issue(evidence_id, record, issue_number, repository):
            continue
        if "prior-resolved-episode" not in _effective_evidence_roles(
            evidence_id,
            record,
            ref_mapping,
        ):
            continue
        target = _try_resolve_prior_resolved_episode_target(evidence_id, record, repository)
        if target is None:
            has_unresolved_identity = True
            continue
        _add_issue_target(targets, target)
    return set(targets.values()), has_unresolved_identity


def _try_resolve_prior_resolved_episode_target(
    evidence_id: str,
    record: Mapping[str, Any],
    repository: str,
) -> tuple[str, int] | None:
    target = _try_parse_issue_number_from_evidence_id(
        evidence_id,
        repository,
        "prior-resolved-episode",
    )
    if target is not None:
        return target

    payload = record["payload"]
    if "priorIssueNumber" not in payload:
        return None
    try:
        prior_repository = (
            _require_repository_string(payload, "priorRepository")
            if "priorRepository" in payload
            else repository
        )
        return prior_repository, _require_positive_int(payload, "priorIssueNumber")
    except ValidationError:
        return None


def _try_parse_issue_number_from_evidence_id(
    evidence_id: str,
    repository: str,
    role: str,
) -> tuple[str, int] | None:
    try:
        return _parse_issue_number_from_evidence_id(evidence_id, repository, role)
    except ValidationError:
        return None


def _is_current_available_record(record: Mapping[str, Any]) -> bool:
    # High-risk role gates require evidence the current snapshot could actually fetch.
    return record["availability"] == "available" and _is_current_record(record)


def _is_current_record(record: Mapping[str, Any]) -> bool:
    return record["payload"].get("source") != "previous-report"


def _validate_related_issues(
    related_issues: Sequence[object],
    issue_number: int,
    repository: str,
) -> None:
    for relationship in related_issues:
        relationship_mapping = _require_mapping(relationship, "relatedIssues item")
        relation_type = _require_nonempty_string(relationship_mapping, "type")
        if relation_type not in RELATIONSHIPS:
            raise ValidationError(f"Unsupported relationship type: {relation_type}.")
        source_issue_number = _require_positive_int(relationship_mapping, "sourceIssueNumber")
        target_issue_number = _require_positive_int(relationship_mapping, "targetIssueNumber")
        if source_issue_number != issue_number:
            raise ValidationError(
                f"Relationship sourceIssueNumber {source_issue_number} does not match decision issue {issue_number}."
            )
        target_repository = (
            _require_repository_string(relationship_mapping, "targetRepository")
            if "targetRepository" in relationship_mapping
            else repository
        )
        if target_issue_number == issue_number and _same_repository(target_repository, repository):
            raise ValidationError("Relationship targetIssueNumber must refer to a different issue.")


def _validate_evidence_refs(
    evidence_refs: Sequence[object],
    evidence_index: Mapping[str, Mapping[str, Any]],
) -> None:
    seen: set[str] = set()
    for evidence_ref in evidence_refs:
        ref_mapping = _require_mapping(evidence_ref, "evidence reference")
        evidence_id = _require_nonempty_string(ref_mapping, "id")
        evidence_kind = _require_nonempty_string(ref_mapping, "kind")
        if evidence_id in seen:
            raise ValidationError(f"duplicate evidence reference: {evidence_id}.")
        seen.add(evidence_id)
        record = evidence_index.get(evidence_id)
        if record is None:
            raise ValidationError(f"Unknown evidence ID: {evidence_id}.")
        if record["kind"] != evidence_kind:
            raise ValidationError(
                f"Evidence kind mismatch for {evidence_id}: expected {record['kind']}, got {evidence_kind}."
            )
        effective_roles = _effective_evidence_roles(evidence_id, record, ref_mapping)
        if (
            "obsolete-surface" in effective_roles
            and not _has_deterministic_obsolete_surface_proof(record)
        ):
            raise ValidationError(
                f"Evidence {evidence_id} cannot claim obsolete-surface without "
                "deterministic source removal or replacement proof."
            )
        _effective_evidence_normalized_cause(evidence_id, record, ref_mapping)


def _validate_evidence_bucket_exclusivity(
    issue_number: int,
    evidence_refs: Sequence[object],
    contradictory_refs: Sequence[object],
    missing_refs: Sequence[object],
) -> None:
    seen: dict[str, str] = {}
    for field_name, refs in (
        ("evidence", evidence_refs),
        ("contradictoryEvidence", contradictory_refs),
        ("missingEvidence", missing_refs),
    ):
        for evidence_ref in refs:
            ref_mapping = _require_mapping(evidence_ref, "evidence reference")
            evidence_id = _require_nonempty_string(ref_mapping, "id")
            previous_field = seen.get(evidence_id)
            if previous_field is not None and previous_field != field_name:
                raise ValidationError(
                    f"Evidence ID {evidence_id} for issue {issue_number} appears in both "
                    f"{previous_field} and {field_name}. Move it to only one of "
                    "evidence, contradictoryEvidence, or missingEvidence."
                )
            seen[evidence_id] = field_name


def _validate_suggested_owners(suggested_owners: Sequence[object]) -> None:
    for owner in suggested_owners:
        owner_mapping = _require_mapping(owner, "suggestedOwners item")
        _require_nonempty_string(owner_mapping, "name")
        _require_nonempty_string(owner_mapping, "reason")


def _require_next_condition(next_condition: Mapping[str, Any]) -> None:
    _require_nonempty_string(next_condition, "type")
    _require_nonempty_string(next_condition, "description")


def _load_snapshot_evidence(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    evidence = _require_mapping(snapshot.get("evidence"), "evidence")
    loaded: dict[str, dict[str, Any]] = {}
    for evidence_id, record in evidence.items():
        loaded[evidence_id] = _validate_evidence_record(evidence_id, record)
    return loaded


def _validate_evidence_record(evidence_id: object, record: object) -> dict[str, Any]:
    evidence_id_text = _require_nonempty_string(evidence_id, "evidence ID")
    if (
        _ISSUE_ID_RE.fullmatch(evidence_id_text) is None
        and _EXTERNAL_ISSUE_ID_RE.fullmatch(evidence_id_text) is None
        and _ISSUE_COMMENT_ID_RE.fullmatch(evidence_id_text) is None
        and _ISSUE_EVENT_ID_RE.fullmatch(evidence_id_text) is None
        and _RUN_ID_RE.fullmatch(evidence_id_text) is None
        and _RUN_CHECK_ID_RE.fullmatch(evidence_id_text) is None
        and _PR_ID_RE.fullmatch(evidence_id_text) is None
        and _EXTERNAL_PR_ID_RE.fullmatch(evidence_id_text) is None
        and _COMMIT_ID_RE.fullmatch(evidence_id_text) is None
        and _EXTERNAL_COMMIT_ID_RE.fullmatch(evidence_id_text) is None
        and _SOURCE_ID_RE.fullmatch(evidence_id_text) is None
        and _CODEOWNERS_ID_RE.fullmatch(evidence_id_text) is None
    ):
        raise ValidationError(f"Unsupported evidence ID: {evidence_id_text}.")

    record_mapping = _require_mapping(record, f"evidence record {evidence_id_text}")
    kind = _require_nonempty_string(record_mapping, "kind")
    if kind not in PRIMARY_EVIDENCE_KINDS:
        raise ValidationError(f"Unsupported evidence kind: {kind}.")
    if not isinstance(record_mapping.get("url"), str) or not record_mapping["url"]:
        raise ValidationError(f"Evidence {evidence_id_text} must include a nonempty url.")
    _require_nonempty_string(record_mapping, "collectedAt")
    availability = _require_nonempty_string(record_mapping, "availability")
    if availability not in EVIDENCE_AVAILABILITIES:
        raise ValidationError(f"Unsupported evidence availability: {availability}.")
    payload = record_mapping.get("payload")
    if not isinstance(payload, Mapping):
        raise ValidationError(f"Evidence {evidence_id_text} must include a payload object.")
    payload_mapping = dict(payload)
    _optional_evidence_role(payload_mapping)
    _optional_normalized_cause(payload_mapping)
    _validate_supporting_candidate_dispositions(payload_mapping)

    _validate_evidence_id_kind_pair(evidence_id_text, kind)
    return {
        "kind": kind,
        "url": record_mapping["url"],
        "collectedAt": record_mapping["collectedAt"],
        "availability": availability,
        "payload": payload_mapping,
    }


def _validate_supporting_candidate_dispositions(payload: Mapping[str, Any]) -> None:
    supporting_search = payload.get("supportingSearch")
    if not isinstance(supporting_search, Mapping) or "candidateDispositions" not in supporting_search:
        return

    candidate_numbers = supporting_search.get("candidateIssueNumbers")
    if not isinstance(candidate_numbers, list):
        raise ValidationError(
            "supportingSearch candidateIssueNumbers must be a list when candidateDispositions are present."
        )
    selected_numbers = {
        number
        for number in candidate_numbers
        if isinstance(number, int) and not isinstance(number, bool) and number > 0
    }
    candidate_dispositions = supporting_search.get("candidateDispositions")
    if not isinstance(candidate_dispositions, list):
        raise ValidationError("supportingSearch candidateDispositions must be a list.")

    seen_numbers: set[int] = set()
    for raw_disposition in candidate_dispositions:
        disposition = _require_mapping(
            raw_disposition,
            "supportingSearch candidate disposition",
        )
        issue_number = _require_positive_int(disposition, "issueNumber")
        if issue_number in seen_numbers:
            raise ValidationError(
                "supportingSearch candidateDispositions must contain one disposition per issue."
            )
        seen_numbers.add(issue_number)
        if issue_number in selected_numbers:
            raise ValidationError(
                f"Supporting candidate {issue_number} cannot be both selected and excluded."
            )

        disposition_name = _require_nonempty_string(disposition, "disposition")
        if disposition_name not in SUPPORTING_CANDIDATE_DISPOSITIONS:
            raise ValidationError(
                f"Unsupported supporting candidate disposition: {disposition_name}."
            )
        provenance = _require_list(disposition, "provenance")
        if not provenance:
            raise ValidationError(
                f"Supporting candidate {issue_number} disposition must include provenance."
            )
        for raw_provenance in provenance:
            provenance_entry = _require_mapping(
                raw_provenance,
                "supporting candidate provenance",
            )
            _require_nonempty_string(provenance_entry, "sourceEvidenceId")
            _require_nonempty_string(provenance_entry, "sourceUrl")
            _require_nonempty_string(provenance_entry, "extractionMethod")


def _validate_evidence_id_kind_pair(evidence_id: str, kind: str) -> None:
    if _ISSUE_ID_RE.fullmatch(evidence_id) or _EXTERNAL_ISSUE_ID_RE.fullmatch(evidence_id):
        expected = "issue-event"
    elif _ISSUE_COMMENT_ID_RE.fullmatch(evidence_id):
        expected = "issue-comment"
    elif _ISSUE_EVENT_ID_RE.fullmatch(evidence_id):
        expected = "issue-event"
    elif _RUN_ID_RE.fullmatch(evidence_id):
        expected = "workflow-log" if evidence_id.endswith(":log") else (
            "workflow-job" if ":attempt:" in evidence_id else "workflow-run"
        )
    elif _RUN_CHECK_ID_RE.fullmatch(evidence_id):
        expected = "workflow-job"
    elif _PR_ID_RE.fullmatch(evidence_id) or _EXTERNAL_PR_ID_RE.fullmatch(evidence_id):
        expected = "pull-request"
    elif _COMMIT_ID_RE.fullmatch(evidence_id) or _EXTERNAL_COMMIT_ID_RE.fullmatch(evidence_id):
        expected = "commit"
    elif _SOURCE_ID_RE.fullmatch(evidence_id):
        expected = "source-path"
    elif _CODEOWNERS_ID_RE.fullmatch(evidence_id):
        expected = "codeowners"
    else:
        raise ValidationError(f"Unsupported evidence ID: {evidence_id}.")

    if kind != expected:
        raise ValidationError(f"Evidence kind mismatch for {evidence_id}: expected {expected}, got {kind}.")


def _decision_issue_numbers(decisions: Sequence[object]) -> set[int]:
    issue_numbers: set[int] = set()
    for decision in decisions:
        mapping = _require_mapping(decision, "decision")
        issue_numbers.add(_require_positive_int(mapping, "issueNumber"))
    return issue_numbers


def _require_mapping(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{field_name} must be an object.")
    return dict(value)


def _require_list(mapping: Mapping[str, Any], field_name: str) -> list[Any]:
    value = mapping.get(field_name)
    if not isinstance(value, list):
        raise ValidationError(f"{field_name} must be a list.")
    return value


def _require_unique_int_list(mapping: Mapping[str, Any], field_name: str) -> list[int]:
    value = _require_list(mapping, field_name)
    numbers: list[int] = []
    seen: set[int] = set()
    for item in value:
        if not isinstance(item, int) or isinstance(item, bool) or item <= 0:
            raise ValidationError(f"{field_name} must contain unique positive integers.")
        if item in seen:
            raise ValidationError(f"{field_name} must not contain duplicate issue numbers.")
        seen.add(item)
        numbers.append(item)
    return numbers


def _require_exact_int(mapping: Mapping[str, Any], field_name: str, expected: int) -> int:
    value = mapping.get(field_name)
    if not isinstance(value, int) or isinstance(value, bool) or value != expected:
        raise ValidationError(f"{field_name} must be {expected}.")
    return value


def _require_positive_int(mapping: Mapping[str, Any], field_name: str) -> int:
    value = mapping.get(field_name)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValidationError(f"{field_name} must be a positive integer.")
    return value


def _require_nonempty_string(mapping: Mapping[str, Any] | object, field_name: str) -> str:
    if isinstance(mapping, Mapping):
        value = mapping.get(field_name)
    else:
        value = mapping
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field_name} must be a nonempty string.")
    return value


def _require_bool(mapping: Mapping[str, Any], field_name: str) -> bool:
    value = mapping.get(field_name)
    if not isinstance(value, bool):
        raise ValidationError(f"{field_name} must be a boolean.")
    return value


def _require_repository(mapping: Mapping[str, Any]) -> str:
    return _require_repository_string(mapping, "repository")


def _require_repository_string(mapping: Mapping[str, Any], field_name: str) -> str:
    repository = _require_nonempty_string(mapping, field_name)
    if _REPOSITORY_RE.fullmatch(repository) is None:
        raise ValidationError(f"{field_name} must be a nonempty owner/repo string.")
    return repository
