from __future__ import annotations

from datetime import UTC, datetime
import re
from typing import Callable, Protocol

from .collector import COPILOT_ASSIGNEES


KNOWN_OPERATIONS = frozenset({"create-comment", "edit-comment", "close-issue"})
KNOWN_CLOSE_REASONS = frozenset({"completed", "not_planned", "duplicate"})
KNOWN_ISSUE_STATES = frozenset({"open", "closed"})
REPOSITORY_PART_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
LEGACY_COMMON_PROPOSAL_FIELDS = frozenset(
    {
        "actionId",
        "dependsOn",
        "evidenceIds",
        "expectedIssueState",
        "idempotencyKey",
        "issueNumber",
        "issueUrl",
        "operation",
        "requiresSeparateApproval",
        "targetKind",
        "targetNumber",
        "targetUrl",
        "expectedTargetState",
    }
)
EXECUTABLE_COMMON_PROPOSAL_FIELDS = (
    LEGACY_COMMON_PROPOSAL_FIELDS
    - {"requiresSeparateApproval"}
    | {"executionEligibility", "sourceEvidenceFingerprint"}
)
EXECUTION_ELIGIBILITY_FIELDS = frozenset(
    {
        "eligible",
        "ciLabels",
        "occurrenceCount",
        "collectionComplete",
        "unavailableEvidenceIds",
        "untrustedReferenceEvidenceIds",
        "blockingReasons",
    }
)
EXECUTABLE_CI_LABELS = frozenset(
    {"automation-broken", "ci-failure-cause", "test-failure"}
)
ELIGIBILITY_REASONS = frozenset(
    {
        "missing-ci-label",
        "no-parsed-occurrences",
        "incomplete-collection",
        "unavailable-evidence",
        "untrusted-reference-provenance",
    }
)
MAX_EXECUTABLE_PROPOSAL_TTL_HOURS = 24
MAX_EXECUTABLE_PROPOSALS_PER_ISSUE = 2
OPERATION_FIELDS = {
    "create-comment": frozenset({"body"}),
    "edit-comment": frozenset({"body", "commentId"}),
    "close-issue": frozenset({"closeReason"}),
}


class ActorClient(Protocol):
    def get_authenticated_login(self) -> str: ...

    def get_issue(
        self,
        repository: str,
        issue_number: int,
    ) -> dict[str, object]: ...

    def get_comment(
        self,
        repository: str,
        comment_id: int,
    ) -> dict[str, object]: ...

    def list_comments(
        self,
        repository: str,
        issue_number: int,
    ) -> list[dict[str, object]]: ...

    def create_comment(
        self,
        repository: str,
        issue_number: int,
        body: str,
    ) -> dict[str, object]: ...

    def edit_comment(
        self,
        repository: str,
        comment_id: int,
        body: str,
    ) -> dict[str, object]: ...

    def close_issue(
        self,
        repository: str,
        issue_number: int,
        reason: str,
    ) -> dict[str, object]: ...


def _required_string(
    value: object,
    *,
    field: str,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string.")
    return value


def _required_int(
    value: object,
    *,
    field: str,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field} must be a positive integer.")
    return value


def _validate_proposal(
    proposal: object,
    *,
    repository: str,
    schema_version: int,
) -> dict[str, object]:
    if not isinstance(proposal, dict):
        raise TypeError("Each proposal must be an object.")

    action_id = _required_string(proposal.get("actionId"), field="actionId")
    operation = _required_string(
        proposal.get("operation"),
        field=f"{action_id}.operation",
    )
    if operation not in KNOWN_OPERATIONS:
        raise ValueError(f"Unsupported operation for {action_id}: {operation}")
    common_fields = (
        EXECUTABLE_COMMON_PROPOSAL_FIELDS
        if schema_version == 2
        else LEGACY_COMMON_PROPOSAL_FIELDS
    )
    unsupported_fields = set(proposal) - (
        common_fields | OPERATION_FIELDS[operation]
    )
    if unsupported_fields:
        raise ValueError(
            f"{action_id} has unsupported fields: {sorted(unsupported_fields)}"
        )

    target_kind, target_number, target_url, expected_state = _proposal_target(
        proposal,
        repository=repository,
        action_id=action_id,
    )
    if operation == "close-issue" and target_kind != "issue":
        raise ValueError(f"{action_id} cannot close a pull request.")
    if schema_version == 2 and target_kind != "issue":
        raise ValueError(f"{action_id} executable target must be an issue.")

    idempotency_key = _required_string(
        proposal.get("idempotencyKey"),
        field=f"{action_id}.idempotencyKey",
    )
    if expected_state not in KNOWN_ISSUE_STATES:
        raise ValueError(f"{action_id} expected target state is unsupported.")

    evidence_ids = proposal.get("evidenceIds")
    if (
        not isinstance(evidence_ids, list)
        or not evidence_ids
        or not all(isinstance(value, str) and value for value in evidence_ids)
    ):
        raise ValueError(f"{action_id}.evidenceIds must contain strings.")
    if schema_version == 1:
        if proposal.get("requiresSeparateApproval") is not True:
            raise ValueError(f"{action_id} must require separate approval.")
    else:
        _validate_execution_eligibility(
            proposal.get("executionEligibility"),
            action_id=action_id,
        )
        _validate_source_evidence_fingerprint(
            proposal.get("sourceEvidenceFingerprint"),
            action_id=action_id,
        )

    if operation in {"create-comment", "edit-comment"}:
        body = _required_string(
            proposal.get("body"),
            field=f"{action_id}.body",
        )
        if not body.startswith("[automated] "):
            raise ValueError(f"{action_id}.body must start with '[automated] '.")
        marker = f"ci-shepherd:idempotency-key={idempotency_key}"
        if marker not in body:
            raise ValueError(f"{action_id}.body must contain its idempotency marker.")
        if operation == "edit-comment":
            _required_int(
                proposal.get("commentId"),
                field=f"{action_id}.commentId",
            )
    else:
        close_reason = _required_string(
            proposal.get("closeReason"),
            field=f"{action_id}.closeReason",
        )
        if close_reason not in KNOWN_CLOSE_REASONS:
            raise ValueError(f"{action_id}.closeReason is unsupported.")

    depends_on = proposal.get("dependsOn")
    if depends_on is not None:
        _required_string(depends_on, field=f"{action_id}.dependsOn")

    return proposal


def _validate_source_evidence_fingerprint(
    value: object,
    *,
    action_id: str,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"issueUpdatedAt"}:
        raise ValueError(
            f"{action_id}.sourceEvidenceFingerprint must contain exactly "
            "issueUpdatedAt."
        )
    _required_string(
        value.get("issueUpdatedAt"),
        field=f"{action_id}.sourceEvidenceFingerprint.issueUpdatedAt",
    )
    return value


def _validate_execution_eligibility(
    value: object,
    *,
    action_id: str,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != EXECUTION_ELIGIBILITY_FIELDS:
        raise ValueError(
            f"{action_id}.executionEligibility must contain exactly the "
            "supported fields."
        )
    eligible = value.get("eligible")
    ci_labels = value.get("ciLabels")
    occurrence_count = value.get("occurrenceCount")
    collection_complete = value.get("collectionComplete")
    unavailable_evidence_ids = value.get("unavailableEvidenceIds")
    untrusted_reference_evidence_ids = value.get(
        "untrustedReferenceEvidenceIds"
    )
    blocking_reasons = value.get("blockingReasons")
    if not isinstance(eligible, bool):
        raise ValueError(f"{action_id}.executionEligibility.eligible must be boolean.")
    if (
        not isinstance(ci_labels, list)
        or not all(
            isinstance(label, str) and label in EXECUTABLE_CI_LABELS
            for label in ci_labels
        )
        or len(set(ci_labels)) != len(ci_labels)
    ):
        raise ValueError(
            f"{action_id}.executionEligibility.ciLabels is invalid."
        )
    if (
        not isinstance(occurrence_count, int)
        or isinstance(occurrence_count, bool)
        or occurrence_count < 0
    ):
        raise ValueError(
            f"{action_id}.executionEligibility.occurrenceCount is invalid."
        )
    if not isinstance(collection_complete, bool):
        raise ValueError(
            f"{action_id}.executionEligibility.collectionComplete must be boolean."
        )
    if (
        not isinstance(unavailable_evidence_ids, list)
        or not all(
            isinstance(evidence_id, str) and evidence_id
            for evidence_id in unavailable_evidence_ids
        )
        or len(set(unavailable_evidence_ids))
        != len(unavailable_evidence_ids)
    ):
        raise ValueError(
            f"{action_id}.executionEligibility.unavailableEvidenceIds is invalid."
        )
    if (
        not isinstance(untrusted_reference_evidence_ids, list)
        or not all(
            isinstance(evidence_id, str) and evidence_id
            for evidence_id in untrusted_reference_evidence_ids
        )
        or len(set(untrusted_reference_evidence_ids))
        != len(untrusted_reference_evidence_ids)
    ):
        raise ValueError(
            f"{action_id}.executionEligibility."
            "untrustedReferenceEvidenceIds is invalid."
        )
    if (
        not isinstance(blocking_reasons, list)
        or not all(reason in ELIGIBILITY_REASONS for reason in blocking_reasons)
        or len(set(blocking_reasons)) != len(blocking_reasons)
    ):
        raise ValueError(
            f"{action_id}.executionEligibility.blockingReasons is invalid."
        )

    derived_reasons: list[str] = []
    if not ci_labels:
        derived_reasons.append("missing-ci-label")
    if occurrence_count <= 0:
        derived_reasons.append("no-parsed-occurrences")
    if not collection_complete:
        derived_reasons.append("incomplete-collection")
    if unavailable_evidence_ids:
        derived_reasons.append("unavailable-evidence")
    if untrusted_reference_evidence_ids:
        derived_reasons.append("untrusted-reference-provenance")
    if blocking_reasons != derived_reasons or eligible != (not derived_reasons):
        raise ValueError(
            f"{action_id}.executionEligibility is internally inconsistent."
        )
    return value


def _validate_document_execution_eligibility(
    value: object,
    *,
    proposals: list[object],
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"status", "violations"}:
        raise ValueError(
            "executionEligibility must contain exactly status and violations."
        )
    status = value.get("status")
    violations = value.get("violations")
    if status not in {"eligible", "blocked"} or not isinstance(violations, list):
        raise ValueError("executionEligibility document status is invalid.")

    derived_violations: list[dict[str, object]] = []
    for proposal in proposals:
        assert isinstance(proposal, dict)
        action_id = str(proposal["actionId"])
        eligibility = _validate_execution_eligibility(
            proposal.get("executionEligibility"),
            action_id=action_id,
        )
        if eligibility["eligible"] is not True:
            derived_violations.append(
                {
                    "actionId": action_id,
                    "blockingReasons": list(eligibility["blockingReasons"]),
                }
            )
    expected = {
        "status": "blocked" if derived_violations else "eligible",
        "violations": derived_violations,
    }
    if value != expected:
        raise ValueError(
            "executionEligibility document status is internally inconsistent."
        )
    return value


def _proposal_target(
    proposal: dict[str, object],
    *,
    repository: str,
    action_id: str,
) -> tuple[str, int, str, str]:
    if "targetKind" in proposal:
        target_kind = _required_string(
            proposal.get("targetKind"),
            field=f"{action_id}.targetKind",
        )
        if target_kind not in {"issue", "pull-request"}:
            raise ValueError(f"{action_id}.targetKind is unsupported.")
        target_number = _required_int(
            proposal.get("targetNumber"),
            field=f"{action_id}.targetNumber",
        )
        target_url = _required_string(
            proposal.get("targetUrl"),
            field=f"{action_id}.targetUrl",
        )
        expected_state = _required_string(
            proposal.get("expectedTargetState"),
            field=f"{action_id}.expectedTargetState",
        )
        expected_url = (
            f"https://github.com/{repository}/pull/{target_number}"
            if target_kind == "pull-request"
            else f"https://github.com/{repository}/issues/{target_number}"
        )
        if target_url != expected_url:
            raise ValueError(
                f"{action_id}.targetUrl does not match repository and target."
            )
        return target_kind, target_number, target_url, expected_state

    issue_number = _required_int(
        proposal.get("issueNumber"),
        field=f"{action_id}.issueNumber",
    )
    issue_url = _required_string(
        proposal.get("issueUrl"),
        field=f"{action_id}.issueUrl",
    )
    expected_issue_url = f"https://github.com/{repository}/issues/{issue_number}"
    if issue_url != expected_issue_url:
        raise ValueError(
            f"{action_id}.issueUrl does not match repository and issueNumber."
        )
    expected_state = _required_string(
        proposal.get("expectedIssueState"),
        field=f"{action_id}.expectedIssueState",
    )
    return "issue", issue_number, issue_url, expected_state


def validate_action_proposals(document: object) -> dict[str, object]:
    if not isinstance(document, dict):
        raise TypeError("Action proposals must be an object.")
    schema_version = document.get("schemaVersion")
    if schema_version not in {1, 2}:
        raise ValueError("Action proposals schemaVersion must be 1 or 2.")
    if schema_version == 2:
        expected_fields = {
            "schemaVersion",
            "repository",
            "snapshotId",
            "shepherdAuthor",
            "generatedAtUtc",
            "proposalTtlHours",
            "maxProposalsPerIssue",
            "executionEligibility",
            "proposals",
            "unchangedIssueNumbers",
        }
        if set(document) != expected_fields:
            raise ValueError(
                "Executable action proposals must contain exactly the "
                "supported document fields."
            )
        _required_string(document.get("generatedAtUtc"), field="generatedAtUtc")
        proposal_ttl_hours = _required_int(
            document.get("proposalTtlHours"),
            field="proposalTtlHours",
        )
        if proposal_ttl_hours > MAX_EXECUTABLE_PROPOSAL_TTL_HOURS:
            raise ValueError(
                "proposalTtlHours exceeds the executor safety maximum."
            )
        max_proposals = _required_int(
            document.get("maxProposalsPerIssue"),
            field="maxProposalsPerIssue",
        )
        if max_proposals != MAX_EXECUTABLE_PROPOSALS_PER_ISSUE:
            raise ValueError(
                "maxProposalsPerIssue must equal the executor safety limit."
            )
        _required_string(document.get("snapshotId"), field="snapshotId")
        _required_string(document.get("shepherdAuthor"), field="shepherdAuthor")
        unchanged = document.get("unchangedIssueNumbers")
        if (
            not isinstance(unchanged, list)
            or not all(
                isinstance(issue_number, int)
                and not isinstance(issue_number, bool)
                and issue_number > 0
                for issue_number in unchanged
            )
        ):
            raise ValueError(
                "unchangedIssueNumbers must contain positive integers."
            )

    repository = _required_string(document.get("repository"), field="repository")
    repository_parts = repository.split("/")
    if (
        len(repository_parts) != 2
        or not all(REPOSITORY_PART_RE.fullmatch(part) for part in repository_parts)
        or any(part in {".", ".."} for part in repository_parts)
    ):
        raise ValueError("repository must have owner/name form.")

    proposals = document.get("proposals")
    if not isinstance(proposals, list):
        raise TypeError("proposals must be a list.")
    action_ids: set[str] = set()
    idempotency_keys: set[str] = set()
    for raw_proposal in proposals:
        proposal = _validate_proposal(
            raw_proposal,
            repository=repository,
            schema_version=int(schema_version),
        )
        action_id = str(proposal["actionId"])
        if action_id in action_ids:
            raise ValueError(f"Duplicate actionId: {action_id}")
        action_ids.add(action_id)
        idempotency_key = str(proposal["idempotencyKey"])
        if idempotency_key in idempotency_keys:
            raise ValueError(f"Duplicate idempotencyKey: {idempotency_key}")
        idempotency_keys.add(idempotency_key)

    for raw_proposal in proposals:
        assert isinstance(raw_proposal, dict)
        depends_on = raw_proposal.get("dependsOn")
        if depends_on is not None and depends_on not in action_ids:
            raise ValueError(
                f"{raw_proposal['actionId']}.dependsOn references an unknown action."
            )
    for action_id in action_ids:
        visited: set[str] = set()
        current = action_id
        while True:
            if current in visited:
                raise ValueError("Action proposal dependency graph contains a cycle.")
            visited.add(current)
            current_proposal = next(
                proposal
                for proposal in proposals
                if isinstance(proposal, dict)
                and proposal.get("actionId") == current
            )
            depends_on = current_proposal.get("dependsOn")
            if depends_on is None:
                break
            current = str(depends_on)
    if schema_version == 2:
        _validate_document_execution_eligibility(
            document.get("executionEligibility"),
            proposals=proposals,
        )
        counts_by_issue: dict[int, int] = {}
        for proposal in proposals:
            assert isinstance(proposal, dict)
            issue_number = _required_int(
                proposal.get("issueNumber"),
                field=f"{proposal['actionId']}.issueNumber",
            )
            counts_by_issue[issue_number] = counts_by_issue.get(issue_number, 0) + 1
        if any(count > max_proposals for count in counts_by_issue.values()):
            raise ValueError("Action proposals exceed maxProposalsPerIssue.")
    return document


def select_action(
    document: dict[str, object],
    action_id: str,
) -> dict[str, object]:
    for proposal in document["proposals"]:
        if isinstance(proposal, dict) and proposal["actionId"] == action_id:
            return proposal
    raise ValueError(f"Unknown actionId: {action_id}")


def _dry_run_action(
    proposal: dict[str, object],
    *,
    repository: str,
) -> dict[str, object]:
    action_id = str(proposal["actionId"])
    target_kind, target_number, target_url, expected_state = _proposal_target(
        proposal,
        repository=repository,
        action_id=action_id,
    )
    return {
        "actionId": action_id,
        "targetKind": target_kind,
        "targetNumber": target_number,
        "targetUrl": target_url,
        "operation": proposal["operation"],
        "body": proposal.get("body"),
        "closeReason": proposal.get("closeReason"),
        "evidenceIds": list(proposal["evidenceIds"]),
        "dependsOn": proposal.get("dependsOn"),
        "expectedTargetState": expected_state,
        "wouldExecute": True,
    }


def build_dry_run(
    document: object,
    *,
    action_id: str | None,
) -> dict[str, object]:
    validated = validate_action_proposals(document)
    proposals = validated["proposals"]
    assert isinstance(proposals, list)
    selected = (
        [select_action(validated, action_id)]
        if action_id is not None
        else proposals
    )
    return {
        "schemaVersion": 1,
        "repository": validated["repository"],
        "mode": "dry-run",
        "actions": [
            _dry_run_action(proposal, repository=str(validated["repository"]))
            for proposal in selected
            if isinstance(proposal, dict)
        ],
    }


def _validated_results(
    document: object,
    *,
    repository: str,
) -> list[dict[str, object]]:
    if not isinstance(document, dict):
        raise TypeError("Action results must be an object.")
    if document.get("schemaVersion") != 1:
        raise ValueError("Action results schemaVersion must be 1.")
    if document.get("repository") != repository:
        raise ValueError("Action results repository does not match proposals.")
    results = document.get("results")
    if not isinstance(results, list) or not all(
        isinstance(result, dict) for result in results
    ):
        raise TypeError("Action results must contain result objects.")
    return results


def _terminal_result(
    *,
    action_id: str,
    attempted_at: str,
    outcome: str,
    reason: str | None = None,
    preflight: dict[str, object] | None = None,
    result: dict[str, object] | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "actionId": action_id,
        "attemptedAt": attempted_at,
        "outcome": outcome,
    }
    if reason is not None:
        record["reason"] = reason
    if preflight is not None:
        record["preflight"] = preflight
    if result is not None:
        record["result"] = result
    return record


def _timestamp(now: Callable[[], datetime]) -> str:
    return now().astimezone(UTC).isoformat().replace("+00:00", "Z")


def _comment_user(comment: dict[str, object]) -> str | None:
    user = comment.get("user")
    return str(user.get("login")) if isinstance(user, dict) and user.get("login") else None


def _accepted_comment_keys(
    target_kind: str,
    target_number: int,
    key: str,
) -> set[str]:
    accepted = {key}
    if target_kind == "issue" and key == f"issue:{target_number}:status":
        accepted.update(
            {
                f"issue:{target_number}:watch",
                f"issue:{target_number}:review-close",
                f"issue:{target_number}:investigate",
                f"issue:{target_number}:ping-human",
            }
        )
    return accepted


def _assigned_to_copilot(target: dict[str, object]) -> bool:
    assignees = target.get("assignees")
    if not isinstance(assignees, list):
        return False
    return any(
        isinstance(assignee, dict)
        and isinstance(assignee.get("login"), str)
        and assignee["login"].casefold() in COPILOT_ASSIGNEES
        for assignee in assignees
    )


def execute_action(
    document: object,
    *,
    action_id: str,
    prior_results: object,
    client: ActorClient,
    now: Callable[[], datetime],
    override_suppression: bool = False,
) -> dict[str, object]:
    validated = validate_action_proposals(document)
    repository = str(validated["repository"])
    proposal = select_action(validated, action_id)
    if validated["schemaVersion"] == 2:
        document_eligibility = _validate_document_execution_eligibility(
            validated.get("executionEligibility"),
            proposals=validated["proposals"],
        )
        if document_eligibility["status"] != "eligible":
            raise ValueError("Proposal document is not eligible for execution.")
        eligibility = _validate_execution_eligibility(
            proposal.get("executionEligibility"),
            action_id=action_id,
        )
        if eligibility["eligible"] is not True:
            raise ValueError(f"{action_id} is not eligible for execution.")
    attempted_at = _timestamp(now)
    results = _validated_results(prior_results, repository=repository)
    target_kind, target_number, target_url, expected_state = _proposal_target(
        proposal,
        repository=repository,
        action_id=action_id,
    )

    if any(
        result.get("actionId") == action_id
        and result.get("outcome")
        in {"executed", "skipped", "stale", "failed", "indeterminate"}
        for result in results
    ):
        return _terminal_result(
            action_id=action_id,
            attempted_at=attempted_at,
            outcome="stale",
            reason="action-already-attempted",
        )

    if (
        proposal["operation"] == "create-comment"
        and not override_suppression
        and any(
            result.get("outcome") == "executed"
            and result.get("idempotencyKey") == proposal["idempotencyKey"]
            and result.get("target")
            == {"kind": target_kind, "number": target_number}
            for result in results
        )
    ):
        return _terminal_result(
            action_id=action_id,
            attempted_at=attempted_at,
            outcome="stale",
            reason="stable-idempotency-key-already-executed",
        )

    depends_on = proposal.get("dependsOn")
    if depends_on is not None and not any(
        result.get("actionId") == depends_on
        and result.get("outcome") == "executed"
        for result in results
    ):
        return _terminal_result(
            action_id=action_id,
            attempted_at=attempted_at,
            outcome="stale",
            reason="dependency-not-executed",
        )

    mutation_attempted = False
    try:
        issue = client.get_issue(repository, target_number)
        issue_state = issue.get("state")
        preflight = {
            (
                "pullRequestState"
                if target_kind == "pull-request"
                else "issueState"
            ): issue_state,
            (
                "pullRequestUpdatedAt"
                if target_kind == "pull-request"
                else "issueUpdatedAt"
            ): issue.get("updated_at"),
        }
        is_pull_request = isinstance(issue.get("pull_request"), dict)
        if is_pull_request != (target_kind == "pull-request"):
            return _terminal_result(
                action_id=action_id,
                attempted_at=attempted_at,
                outcome="stale",
                reason="target-kind-changed",
                preflight=preflight,
            )
        if issue.get("html_url") not in {None, target_url}:
            return _terminal_result(
                action_id=action_id,
                attempted_at=attempted_at,
                outcome="stale",
                reason="target-url-changed",
                preflight=preflight,
            )
        if target_kind == "pull-request" and _assigned_to_copilot(issue):
            return _terminal_result(
                action_id=action_id,
                attempted_at=attempted_at,
                outcome="stale",
                reason="target-assigned-to-copilot",
                preflight=preflight,
            )
        if issue_state != expected_state:
            return _terminal_result(
                action_id=action_id,
                attempted_at=attempted_at,
                outcome="stale",
                reason="issue-state-changed",
                preflight=preflight,
            )
        if validated["schemaVersion"] == 2:
            fingerprint = _validate_source_evidence_fingerprint(
                proposal.get("sourceEvidenceFingerprint"),
                action_id=action_id,
            )
            expected_updated_at = fingerprint["issueUpdatedAt"]
            if depends_on is not None:
                dependency_result = next(
                    (
                        result
                        for result in reversed(results)
                        if result.get("actionId") == depends_on
                        and result.get("outcome") == "executed"
                    ),
                    None,
                )
                dependency_payload = (
                    dependency_result.get("result")
                    if isinstance(dependency_result, dict)
                    else None
                )
                dependency_updated_at = (
                    dependency_payload.get("sourceIssueUpdatedAt")
                    if isinstance(dependency_payload, dict)
                    else None
                )
                if isinstance(dependency_updated_at, str):
                    expected_updated_at = dependency_updated_at
            if issue.get("updated_at") != expected_updated_at:
                return _terminal_result(
                    action_id=action_id,
                    attempted_at=attempted_at,
                    outcome="stale",
                    reason="source-evidence-changed",
                    preflight=preflight,
                )

        login = client.get_authenticated_login()
        if login != validated.get("shepherdAuthor"):
            return _terminal_result(
                action_id=action_id,
                attempted_at=attempted_at,
                outcome="stale",
                reason="actor-identity-changed",
                preflight=preflight,
            )

        operation = proposal["operation"]
        if operation == "create-comment":
            key = str(proposal["idempotencyKey"])
            accepted_keys = _accepted_comment_keys(
                target_kind,
                target_number,
                key,
            )
            comments = client.list_comments(repository, target_number)
            if not override_suppression and any(
                any(
                    f"ci-shepherd:idempotency-key={accepted_key}"
                    in str(comment.get("body") or "")
                    for accepted_key in accepted_keys
                )
                and _comment_user(comment) == login
                for comment in comments
            ):
                return _terminal_result(
                    action_id=action_id,
                    attempted_at=attempted_at,
                    outcome="stale",
                    reason="idempotency-marker-exists",
                    preflight=preflight,
                )
            mutation_attempted = True
            created = client.create_comment(
                repository,
                target_number,
                str(proposal["body"]),
            )
            comment_id = _required_int(
                created.get("id"),
                field=f"{action_id}.createdCommentId",
            )
            live = client.get_comment(repository, comment_id)
            if (
                live.get("body") != proposal["body"]
                or _comment_user(live) != login
            ):
                raise RuntimeError("Created comment did not reconcile.")
            source_issue = (
                client.get_issue(repository, target_number)
                if validated["schemaVersion"] == 2
                else None
            )
            return _terminal_result(
                action_id=action_id,
                attempted_at=attempted_at,
                outcome="executed",
                preflight=preflight,
                result={
                    "commentId": comment_id,
                    "commentUrl": live.get("html_url"),
                    **(
                        {
                            "sourceIssueUpdatedAt": _required_string(
                                source_issue.get("updated_at"),
                                field=f"{action_id}.sourceIssueUpdatedAt",
                            )
                        }
                        if isinstance(source_issue, dict)
                        else {}
                    ),
                },
            )

        if operation == "edit-comment":
            comment_id = int(proposal["commentId"])
            live = client.get_comment(repository, comment_id)
            key = str(proposal["idempotencyKey"])
            accepted_keys = _accepted_comment_keys(
                target_kind,
                target_number,
                key,
            )
            body = str(live.get("body") or "")
            if (
                _comment_user(live) != login
                or not any(
                    f"ci-shepherd:idempotency-key={accepted_key}" in body
                    for accepted_key in accepted_keys
                )
            ):
                return _terminal_result(
                    action_id=action_id,
                    attempted_at=attempted_at,
                    outcome="stale",
                    reason="comment-ownership-changed",
                    preflight=preflight,
                )
            mutation_attempted = True
            client.edit_comment(
                repository,
                comment_id,
                str(proposal["body"]),
            )
            reconciled = client.get_comment(repository, comment_id)
            if (
                reconciled.get("body") != proposal["body"]
                or _comment_user(reconciled) != login
            ):
                raise RuntimeError("Edited comment did not reconcile.")
            source_issue = (
                client.get_issue(repository, target_number)
                if validated["schemaVersion"] == 2
                else None
            )
            return _terminal_result(
                action_id=action_id,
                attempted_at=attempted_at,
                outcome="executed",
                preflight=preflight,
                result={
                    "commentId": comment_id,
                    "commentUrl": reconciled.get("html_url"),
                    **(
                        {
                            "sourceIssueUpdatedAt": _required_string(
                                source_issue.get("updated_at"),
                                field=f"{action_id}.sourceIssueUpdatedAt",
                            )
                        }
                        if isinstance(source_issue, dict)
                        else {}
                    ),
                },
            )

        close_reason = str(proposal["closeReason"])
        mutation_attempted = True
        client.close_issue(repository, target_number, close_reason)
        reconciled_issue = client.get_issue(repository, target_number)
        if (
            reconciled_issue.get("state") != "closed"
            or reconciled_issue.get("state_reason") != close_reason
        ):
            raise RuntimeError("Closed issue did not reconcile.")
        return _terminal_result(
            action_id=action_id,
            attempted_at=attempted_at,
            outcome="executed",
            preflight=preflight,
            result={
                "issueState": "closed",
                "stateReason": close_reason,
                "issueUrl": reconciled_issue.get("html_url"),
            },
        )
    except Exception as exc:
        return _terminal_result(
            action_id=action_id,
            attempted_at=attempted_at,
            outcome="indeterminate" if mutation_attempted else "failed",
            reason=str(exc),
        )


def reconcile_action(
    document: object,
    *,
    action_id: str,
    client: ActorClient,
    now: Callable[[], datetime],
) -> dict[str, object]:
    """Reconcile an interrupted action without issuing a second mutation."""

    validated = validate_action_proposals(document)
    repository = str(validated["repository"])
    proposal = select_action(validated, action_id)
    attempted_at = _timestamp(now)
    target_kind, target_number, _, _ = _proposal_target(
        proposal,
        repository=repository,
        action_id=action_id,
    )

    try:
        operation = proposal["operation"]
        if operation == "create-comment":
            comments = client.list_comments(repository, target_number)
            login = client.get_authenticated_login()
            expected_login = str(validated["shepherdAuthor"])
            marker = (
                "ci-shepherd:idempotency-key="
                f"{proposal['idempotencyKey']}"
            )
            matches = [
                comment
                for comment in comments
                if marker in str(comment.get("body") or "")
                and comment.get("body") == proposal["body"]
                and _comment_user(comment) == login == expected_login
            ]
            if matches:
                match = max(
                    matches,
                    key=lambda comment: (
                        comment.get("id")
                        if isinstance(comment.get("id"), int)
                        else -1
                    ),
                )
                source_issue = (
                    client.get_issue(repository, target_number)
                    if validated["schemaVersion"] == 2
                    else None
                )
                return _terminal_result(
                    action_id=action_id,
                    attempted_at=attempted_at,
                    outcome="executed",
                    result={
                        "commentId": _required_int(
                            match.get("id"),
                            field=f"{action_id}.reconciledCommentId",
                        ),
                        "commentUrl": match.get("html_url"),
                        "reconciledAfterInterruption": True,
                        **(
                            {
                                "sourceIssueUpdatedAt": _required_string(
                                    source_issue.get("updated_at"),
                                    field=f"{action_id}.sourceIssueUpdatedAt",
                                )
                            }
                            if isinstance(source_issue, dict)
                            else {}
                        ),
                    },
                )

        elif operation == "edit-comment":
            comment_id = int(proposal["commentId"])
            comment = client.get_comment(repository, comment_id)
            login = client.get_authenticated_login()
            expected_login = str(validated["shepherdAuthor"])
            marker = (
                "ci-shepherd:idempotency-key="
                f"{proposal['idempotencyKey']}"
            )
            if (
                marker in str(comment.get("body") or "")
                and comment.get("body") == proposal["body"]
                and _comment_user(comment) == login == expected_login
            ):
                source_issue = (
                    client.get_issue(repository, target_number)
                    if validated["schemaVersion"] == 2
                    else None
                )
                return _terminal_result(
                    action_id=action_id,
                    attempted_at=attempted_at,
                    outcome="executed",
                    result={
                        "commentId": comment_id,
                        "commentUrl": comment.get("html_url"),
                        "reconciledAfterInterruption": True,
                        **(
                            {
                                "sourceIssueUpdatedAt": _required_string(
                                    source_issue.get("updated_at"),
                                    field=f"{action_id}.sourceIssueUpdatedAt",
                                )
                            }
                            if isinstance(source_issue, dict)
                            else {}
                        ),
                    },
                )

        elif operation == "close-issue":
            issue = client.get_issue(repository, target_number)
            if (
                target_kind == "issue"
                and issue.get("state") == "closed"
                and issue.get("state_reason") == proposal["closeReason"]
            ):
                return _terminal_result(
                    action_id=action_id,
                    attempted_at=attempted_at,
                    outcome="executed",
                    result={
                        "issueState": "closed",
                        "stateReason": issue.get("state_reason"),
                        "issueUrl": issue.get("html_url"),
                        "reconciledAfterInterruption": True,
                    },
                )
        else:
            raise ValueError(f"Unsupported action operation: {operation}")
    except Exception as exc:
        return _terminal_result(
            action_id=action_id,
            attempted_at=attempted_at,
            outcome="indeterminate",
            reason=f"reconciliation-failed: {exc}",
        )

    return _terminal_result(
        action_id=action_id,
        attempted_at=attempted_at,
        outcome="indeterminate",
        reason="mutation-not-confirmed",
    )
