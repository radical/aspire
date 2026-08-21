from __future__ import annotations

from datetime import UTC, datetime
import re
from typing import Callable, Protocol


KNOWN_OPERATIONS = frozenset({"create-comment", "edit-comment", "close-issue"})
KNOWN_CLOSE_REASONS = frozenset({"completed", "not_planned", "duplicate"})
KNOWN_ISSUE_STATES = frozenset({"open", "closed"})
REPOSITORY_PART_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
COMMON_PROPOSAL_FIELDS = frozenset(
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
    }
)
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
) -> dict[str, object]:
    if not isinstance(proposal, dict):
        raise TypeError("Each proposal must be an object.")

    action_id = _required_string(proposal.get("actionId"), field="actionId")
    issue_number = _required_int(
        proposal.get("issueNumber"),
        field=f"{action_id}.issueNumber",
    )
    operation = _required_string(
        proposal.get("operation"),
        field=f"{action_id}.operation",
    )
    if operation not in KNOWN_OPERATIONS:
        raise ValueError(f"Unsupported operation for {action_id}: {operation}")
    unsupported_fields = set(proposal) - (
        COMMON_PROPOSAL_FIELDS | OPERATION_FIELDS[operation]
    )
    if unsupported_fields:
        raise ValueError(
            f"{action_id} has unsupported fields: {sorted(unsupported_fields)}"
        )

    issue_url = _required_string(
        proposal.get("issueUrl"),
        field=f"{action_id}.issueUrl",
    )
    expected_issue_url = (
        f"https://github.com/{repository}/issues/{issue_number}"
    )
    if issue_url != expected_issue_url:
        raise ValueError(
            f"{action_id}.issueUrl does not match repository and issueNumber."
        )

    idempotency_key = _required_string(
        proposal.get("idempotencyKey"),
        field=f"{action_id}.idempotencyKey",
    )
    expected_state = _required_string(
        proposal.get("expectedIssueState"),
        field=f"{action_id}.expectedIssueState",
    )
    if expected_state not in KNOWN_ISSUE_STATES:
        raise ValueError(f"{action_id}.expectedIssueState is unsupported.")

    evidence_ids = proposal.get("evidenceIds")
    if (
        not isinstance(evidence_ids, list)
        or not evidence_ids
        or not all(isinstance(value, str) and value for value in evidence_ids)
    ):
        raise ValueError(f"{action_id}.evidenceIds must contain strings.")
    if proposal.get("requiresSeparateApproval") is not True:
        raise ValueError(f"{action_id} must require separate approval.")

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


def validate_action_proposals(document: object) -> dict[str, object]:
    if not isinstance(document, dict):
        raise TypeError("Action proposals must be an object.")
    if document.get("schemaVersion") != 1:
        raise ValueError("Action proposals schemaVersion must be 1.")

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
    for raw_proposal in proposals:
        proposal = _validate_proposal(raw_proposal, repository=repository)
        action_id = str(proposal["actionId"])
        if action_id in action_ids:
            raise ValueError(f"Duplicate actionId: {action_id}")
        action_ids.add(action_id)

    for raw_proposal in proposals:
        assert isinstance(raw_proposal, dict)
        depends_on = raw_proposal.get("dependsOn")
        if depends_on is not None and depends_on not in action_ids:
            raise ValueError(
                f"{raw_proposal['actionId']}.dependsOn references an unknown action."
            )
    return document


def select_action(
    document: dict[str, object],
    action_id: str,
) -> dict[str, object]:
    for proposal in document["proposals"]:
        if isinstance(proposal, dict) and proposal["actionId"] == action_id:
            return proposal
    raise ValueError(f"Unknown actionId: {action_id}")


def _dry_run_action(proposal: dict[str, object]) -> dict[str, object]:
    return {
        "actionId": proposal["actionId"],
        "issueNumber": proposal["issueNumber"],
        "issueUrl": proposal["issueUrl"],
        "operation": proposal["operation"],
        "body": proposal.get("body"),
        "closeReason": proposal.get("closeReason"),
        "evidenceIds": list(proposal["evidenceIds"]),
        "dependsOn": proposal.get("dependsOn"),
        "expectedIssueState": proposal["expectedIssueState"],
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
            _dry_run_action(proposal)
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


def execute_action(
    document: object,
    *,
    action_id: str,
    prior_results: object,
    client: ActorClient,
    now: Callable[[], datetime],
) -> dict[str, object]:
    validated = validate_action_proposals(document)
    repository = str(validated["repository"])
    proposal = select_action(validated, action_id)
    attempted_at = _timestamp(now)
    results = _validated_results(prior_results, repository=repository)

    if any(result.get("actionId") == action_id for result in results):
        return _terminal_result(
            action_id=action_id,
            attempted_at=attempted_at,
            outcome="stale",
            reason="action-already-attempted",
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

    issue_number = int(proposal["issueNumber"])
    try:
        issue = client.get_issue(repository, issue_number)
        issue_state = issue.get("state")
        preflight = {
            "issueState": issue_state,
            "issueUpdatedAt": issue.get("updated_at"),
        }
        if issue_state != proposal["expectedIssueState"]:
            return _terminal_result(
                action_id=action_id,
                attempted_at=attempted_at,
                outcome="stale",
                reason="issue-state-changed",
                preflight=preflight,
            )

        operation = proposal["operation"]
        if operation == "create-comment":
            key = str(proposal["idempotencyKey"])
            marker = f"ci-shepherd:idempotency-key={key}"
            comments = client.list_comments(repository, issue_number)
            login = client.get_authenticated_login()
            if any(
                marker in str(comment.get("body") or "")
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
            created = client.create_comment(
                repository,
                issue_number,
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
            return _terminal_result(
                action_id=action_id,
                attempted_at=attempted_at,
                outcome="executed",
                preflight=preflight,
                result={
                    "commentId": comment_id,
                    "commentUrl": live.get("html_url"),
                },
            )

        if operation == "edit-comment":
            comment_id = int(proposal["commentId"])
            live = client.get_comment(repository, comment_id)
            login = client.get_authenticated_login()
            marker = (
                f"ci-shepherd:idempotency-key={proposal['idempotencyKey']}"
            )
            if (
                _comment_user(live) != login
                or marker not in str(live.get("body") or "")
            ):
                return _terminal_result(
                    action_id=action_id,
                    attempted_at=attempted_at,
                    outcome="stale",
                    reason="comment-ownership-changed",
                    preflight=preflight,
                )
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
            return _terminal_result(
                action_id=action_id,
                attempted_at=attempted_at,
                outcome="executed",
                preflight=preflight,
                result={
                    "commentId": comment_id,
                    "commentUrl": reconciled.get("html_url"),
                },
            )

        close_reason = str(proposal["closeReason"])
        client.close_issue(repository, issue_number, close_reason)
        reconciled_issue = client.get_issue(repository, issue_number)
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
            outcome="failed",
            reason=str(exc),
        )
