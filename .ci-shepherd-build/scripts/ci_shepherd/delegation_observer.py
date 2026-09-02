from __future__ import annotations

"""Read-only observation of shepherd-owned GitHub Copilot task lifecycles."""

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Protocol
from urllib.parse import urlencode

from .delegations import (
    AgentTask,
    CapacityEvidence,
    DelegatedIssue,
    DelegatedPullRequest,
    PullRequestState,
    normalize_agent_task,
)


class DelegationReadClient(Protocol):
    def get(self, endpoint: str) -> object: ...

    def get_pages(self, endpoint: str, key: str | None = None) -> list[object]: ...


@dataclass(frozen=True, slots=True)
class DelegationObservation:
    tasks: tuple[AgentTask, ...]
    pull_requests: tuple[DelegatedPullRequest, ...]
    issues: tuple[DelegatedIssue, ...]
    evidence: CapacityEvidence


def observe_delegations(
    client: DelegationReadClient,
    repository: str,
    *,
    owned_task_ids: set[str] | None = None,
    owned_issue_numbers: set[int] | None = None,
) -> DelegationObservation:
    if owned_task_ids is None:
        task_records = observe_agent_task_records(client, repository)
    else:
        task_records = observe_capacity_task_records(
            client,
            repository,
            owned_task_ids=owned_task_ids,
        )
    tasks = tuple(
        normalize_agent_task(_mapping(record, f"tasks[{index}]"))
        for index, record in enumerate(task_records)
    )
    pull_requests: dict[int, DelegatedPullRequest] = {}
    observed_owned_ids = (
        {task.task_id for task in tasks}
        if owned_task_ids is None
        else owned_task_ids
    )
    repository_owner = repository.split("/", 1)[0]
    for task in tasks:
        if task.task_id not in observed_owned_ids:
            continue
        for branch in task.branch_artifacts:
            assert branch.head_ref is not None
            endpoint = (
                f"/repos/{repository}/pulls?"
                + urlencode(
                    {
                        "head": f"{repository_owner}:{branch.head_ref}",
                        "state": "all",
                        "per_page": "100",
                    }
                )
            )
            for index, record in enumerate(client.get_pages(endpoint)):
                pull_request = _normalize_pull_request(record, index=index)
                previous = pull_requests.get(pull_request.database_id)
                if previous is not None and previous != pull_request:
                    raise ValueError(
                        f"Pull request {pull_request.database_id} changed "
                        "across branch observations."
                    )
                pull_requests[pull_request.database_id] = pull_request
        for artifact in task.pull_artifacts:
            assert artifact.database_id is not None
            key = artifact.database_id
            pull_requests.setdefault(
                key,
                DelegatedPullRequest(
                    database_id=artifact.database_id,
                    global_id=artifact.global_id,
                    state=PullRequestState.UNKNOWN,
                    is_draft=False,
                ),
            )

    return DelegationObservation(
        tasks=tasks,
        pull_requests=tuple(
            pull_requests[key] for key in sorted(pull_requests)
        ),
        issues=tuple(
            _normalize_issue(
                client.get(f"/repos/{repository}/issues/{issue_number}"),
                expected_number=issue_number,
            )
            for issue_number in sorted(owned_issue_numbers or set())
        ),
        evidence=CapacityEvidence(),
    )


def observe_agent_task_records(
    client: DelegationReadClient,
    repository: str,
    *,
    since: datetime | None = None,
) -> list[object]:
    records_by_id: dict[str, object] = {}
    archived_values = (False, True) if since is None else (False,)
    for archived in archived_values:
        query = {
            "is_archived": str(archived).lower(),
            "per_page": "100",
        }
        if since is not None:
            query["since"] = since.isoformat().replace("+00:00", "Z")
        endpoint = (
            f"/agents/repos/{repository}/tasks"
            f"?{urlencode(query)}"
        )
        for index, record in enumerate(client.get_pages(endpoint, key="tasks")):
            mapping = _mapping(record, f"tasks[{archived}][{index}]")
            task_id = mapping.get("id")
            if not isinstance(task_id, str) or not task_id:
                raise ValueError(f"tasks[{archived}][{index}].id must be nonempty.")
            previous = records_by_id.get(task_id)
            if previous is not None and previous != record:
                raise ValueError(f"Agent Task {task_id!r} changed across inventories.")
            records_by_id[task_id] = record
    return [records_by_id[task_id] for task_id in sorted(records_by_id)]


def observe_capacity_task_records(
    client: DelegationReadClient,
    repository: str,
    *,
    owned_task_ids: set[str],
) -> list[object]:
    """Read shepherd-owned tasks exactly plus repository-wide running tasks."""
    records_by_id: dict[str, object] = {}
    for task_id in sorted(owned_task_ids):
        endpoint = f"/agents/repos/{repository}/tasks/{task_id}"
        try:
            record = client.get(endpoint)
        except Exception as exc:
            raise RuntimeError(
                f"owned_task_inventory_incomplete:{task_id}"
            ) from exc
        mapping = _mapping(record, f"tasks[{task_id}]")
        observed_id = mapping.get("id")
        if observed_id != task_id:
            raise ValueError(
                f"Task endpoint for {task_id!r} returned {observed_id!r}."
            )
        records_by_id[task_id] = record

    endpoint = (
        f"/agents/repos/{repository}/tasks"
        "?state=queued%2Cin_progress&is_archived=false&per_page=100"
    )
    paged_reader = getattr(client, "get_paged_inventory", None)
    if callable(paged_reader):
        inventory = paged_reader(endpoint, key="tasks")
        if getattr(inventory, "complete", None) is not True:
            raise RuntimeError(
                "repository_running_task_inventory_incomplete:"
                f"{getattr(inventory, 'pages', 'unknown')}_pages"
            )
        running_records = getattr(inventory, "items", ())
    else:
        running_records = client.get_pages(endpoint, key="tasks")
    for index, record in enumerate(running_records):
        mapping = _mapping(record, f"running_tasks[{index}]")
        task_id = mapping.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError(f"running_tasks[{index}].id must be nonempty.")
        previous = records_by_id.get(task_id)
        if previous is not None and previous != record:
            raise ValueError(f"Agent Task {task_id!r} changed across observations.")
        records_by_id[task_id] = record

    return [records_by_id[task_id] for task_id in sorted(records_by_id)]


def _normalize_pull_request(
    record: object,
    *,
    index: int,
) -> DelegatedPullRequest:
    pull_request = _mapping(record, f"pull_requests[{index}]")
    database_id = pull_request.get("id")
    if (
        not isinstance(database_id, int)
        or isinstance(database_id, bool)
        or database_id <= 0
    ):
        raise ValueError(f"pull_requests[{index}].id must be a positive integer.")
    global_id = pull_request.get("node_id")
    if not isinstance(global_id, str) or not global_id:
        raise ValueError(f"pull_requests[{index}].node_id must be nonempty.")
    raw_state = pull_request.get("state")
    if raw_state == "open":
        state = PullRequestState.OPEN
    elif raw_state == "closed":
        state = (
            PullRequestState.MERGED
            if pull_request.get("merged_at") is not None
            else PullRequestState.CLOSED
        )
    else:
        raise ValueError(
            f"pull_requests[{index}].state must be 'open' or 'closed'."
        )
    is_draft = pull_request.get("draft")
    if not isinstance(is_draft, bool):
        raise ValueError(f"pull_requests[{index}].draft must be a boolean.")
    number = pull_request.get("number")
    if (
        not isinstance(number, int)
        or isinstance(number, bool)
        or number <= 0
    ):
        raise ValueError(f"pull_requests[{index}].number must be a positive integer.")
    return DelegatedPullRequest(
        database_id=database_id,
        global_id=global_id,
        state=state,
        is_draft=is_draft,
        number=number,
    )


def _normalize_issue(
    record: object,
    *,
    expected_number: int,
) -> DelegatedIssue:
    issue = _mapping(record, f"issues[{expected_number}]")
    if issue.get("number") != expected_number:
        raise ValueError(
            f"Issue endpoint for {expected_number} returned "
            f"{issue.get('number')!r}."
        )
    state = issue.get("state")
    if state not in {"open", "closed"}:
        raise ValueError(f"issues[{expected_number}].state is invalid.")
    assignees = issue.get("assignees", [])
    if not isinstance(assignees, list):
        raise ValueError(f"issues[{expected_number}].assignees must be a list.")
    logins = {
        assignee.get("login", "").casefold()
        for assignee in assignees
        if isinstance(assignee, Mapping)
        and isinstance(assignee.get("login"), str)
    }
    return DelegatedIssue(
        number=expected_number,
        is_open=state == "open",
        copilot_assigned=bool(
            logins
            & {
                "copilot",
                "copilot-swe-agent",
                "copilot-swe-agent[bot]",
                "github-copilot[bot]",
            }
        ),
    )


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object.")
    return value
