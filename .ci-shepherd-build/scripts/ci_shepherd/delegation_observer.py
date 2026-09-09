from __future__ import annotations

"""Read-only observation of shepherd-owned GitHub Copilot task lifecycles."""

from dataclasses import dataclass, field
from datetime import datetime
import copy
import re
from typing import Mapping, Protocol, Sequence
from urllib.parse import urlencode

from .delegations import (
    AgentTask,
    CapacityEvidence,
    DelegatedIssue,
    DelegatedPullRequest,
    PullRequestState,
    normalize_agent_task,
)
from .github import GitHubApiError
from .models import ValidationError, _require_repository_string, validate_commit_comparison
from .timeutils import parse_aware_iso8601


class DelegationReadClient(Protocol):
    def get(self, endpoint: str) -> object: ...

    def get_pages(self, endpoint: str, key: str | None = None) -> list[object]: ...


@dataclass(frozen=True, slots=True)
class DelegationObservation:
    tasks: tuple[AgentTask, ...]
    pull_requests: tuple[DelegatedPullRequest, ...]
    issues: tuple[DelegatedIssue, ...]
    evidence: CapacityEvidence
    pull_request_sources: Mapping[int, Mapping[str, object]] = field(default_factory=dict)
    task_pull_request_ids: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    unavailable_task_ids: frozenset[str] = frozenset()
    unavailable_issue_numbers: frozenset[int] = frozenset()
    pull_request_outcome_sources: Mapping[int, Mapping[str, object]] = field(default_factory=dict)


def _outcome_body(body: object, limit: int) -> dict[str, object] | None:
    from .investigations import _fingerprint

    if not isinstance(body, str):
        return None
    return {
        "preview": body[:limit], "length": len(body),
        "fingerprint": _fingerprint(body), "truncated": len(body) > limit,
    }


def _outcome_pull_source(repository: str, pull: Mapping[str, object], detail: Mapping[str, object]) -> dict[str, object]:
    head = detail.get("head")
    user = detail.get("user")
    number = pull.get("number")
    url = f"https://github.com/{repository}/pull/{number}" if number else None
    # REST pull details contain raw body/user/head/comments. The inventory's
    # delegatedPullRequestDetails uses body/author/url/updatedAt instead.
    observed_url = detail.get("url") if "updatedAt" in detail else detail.get("html_url")
    available = observed_url == url and url is not None
    return {
        "databaseId": pull["databaseId"], "globalId": pull.get("globalId"), "number": number,
        "state": pull["state"], "isDraft": pull["isDraft"], "changedFiles": pull.get("changedFiles"),
        "url": url,
        "author": (detail.get("author") if "updatedAt" in detail else user.get("login") if isinstance(user, Mapping) else None) if available else None,
        "headSha": (head.get("sha") if isinstance(head, Mapping) else pull.get("progressSource", {}).get("headSha")),
        "updatedAt": (detail.get("updatedAt", detail.get("updated_at"))) if available else None,
        "body": _outcome_body(detail.get("body"), 4000) if available else None,
        "commentCount": detail.get("comments") if type(detail.get("comments")) is int and detail["comments"] >= 0 else None,
        "commentsAvailability": "not-requested", "comments": [], "commentWindowTruncated": False,
    }


def _cloud_outcome(record: Mapping[str, object], sources: list[dict[str, object]], *, previous: Mapping[str, object] | None = None) -> dict[str, object]:
    from .investigations import _fingerprint

    reported = any(
        source.get("body", {}) and source["body"]["preview"].strip()
        or any(comment["body"]["preview"].strip() for comment in source["comments"])
        for source in sources
    )
    value = {
        "taskId": record.get("taskId"), "taskState": record.get("taskState"),
        "taskObservation": record.get("taskObservation", "available"),
        "assessmentRequired": (
            record.get("taskState") not in {"queued", "in_progress"}
            or record.get("taskObservation") == "unavailable"
            or record.get("lifecycle") in {"closed_unmerged"}
            or bool(previous and previous.get("assessmentRequired"))
        ),
        "availability": "reported" if reported else "unavailable",
        "detail": "Untrusted reported outcome; not verified repair or execution authority."
        if reported else "outcome evidence unavailable",
        "pullRequests": sources,
    }
    value["fingerprint"] = _fingerprint(value)
    return value


def initialize_cloud_outcome(record: dict[str, object], sources: Mapping[int, Mapping[str, object]]) -> None:
    record["outcomeEvidence"] = _cloud_outcome(record, [
        copy.deepcopy(dict(sources.get(pull["databaseId"]) or _outcome_pull_source(record["repository"], pull, {})))
        for pull in record["pullRequests"]
    ])


def attach_cloud_outcomes(
    snapshot: dict[str, object], previous_snapshot: Mapping[str, object] | None,
    client: DelegationReadClient,
) -> None:
    """Freeze bound reported conclusions without assigning them repair authority."""
    from .investigations import _fingerprint

    previous = previous_snapshot or {}
    if previous and previous.get("repository") != snapshot["repository"]:
        raise ValueError("Cloud outcomes require the same repository.")
    previous_records = {
        record["actionId"]: record
        for record in previous.get("delegationStatus", {}).get("records", [])
    }
    details = {detail["number"]: detail for detail in snapshot.get("delegatedPullRequestDetails", [])}
    comment_windows: dict[int, list[dict[str, object]] | None] = {}
    for record in snapshot.get("delegationStatus", {}).get("records", []):
        old_record = previous_records.get(record["actionId"], {})
        old = old_record.get("outcomeEvidence", {})
        if old.get("taskId") != record.get("taskId"):
            old = {}
        existing = {
            source["databaseId"]: source
            for source in record.get("outcomeEvidence", {}).get("pullRequests", [])
        }
        old_sources = {source["databaseId"]: source for source in old.get("pullRequests", [])}
        sources = []
        for pull in record["pullRequests"]:
            source = copy.deepcopy(existing.get(pull["databaseId"]) or _outcome_pull_source(snapshot["repository"], pull, {}))
            for key in ("databaseId", "globalId", "number", "state", "isDraft", "changedFiles"):
                source[key] = pull.get(key)
            source["headSha"] = pull.get("progressSource", {}).get("headSha")
            detail = details.get(pull.get("number"))
            if detail is not None and snapshot["delegationStatus"]["status"] == "complete":
                observed = _outcome_pull_source(snapshot["repository"], pull, detail)
                # Reuse the already-collected full inventory body, but retain the
                # exact task-linked pull detail's head and discussion count.
                for key in ("body", "author", "url", "updatedAt"):
                    source[key] = observed[key]
            if snapshot["delegationStatus"]["status"] != "complete":
                source.update(body=None, comments=[], commentsAvailability="unavailable", commentWindowTruncated=False)
                sources.append(source)
                continue
            prior = old_sources.get(pull["databaseId"], {})
            identity_fields = set(source) - {"comments", "commentsAvailability", "commentWindowTruncated"}
            changed = any(source.get(key) != prior.get(key) for key in identity_fields)
            ended = record.get("taskState") not in {"queued", "in_progress"}
            transitioned = record.get("taskState") != old.get("taskState") or record.get("taskObservation", "available") != old.get("taskObservation")
            if not ended and not old.get("assessmentRequired") and not changed and not transitioned and prior:
                for key in ("comments", "commentsAvailability", "commentWindowTruncated"):
                    source[key] = copy.deepcopy(prior[key])
            elif ended or old.get("assessmentRequired") or old and changed:
                source.update(comments=[], commentsAvailability="unavailable", commentWindowTruncated=False)
                count = source["commentCount"]
                if count == 0:
                    source["commentsAvailability"] = "available"
                elif type(count) is int and pull.get("number") is not None:
                    # Issue comments are ID-ascending, without a descending sort.
                    # Reobserve the bounded outcome window while an attempt has
                    # ended/blocked instead of relying on pull.updated_at to also
                    # advance with a comment edit.
                    # Fingerprints, not fetch activity, decide reassessment.
                    # Read only the last page (at most five), never get_pages().
                    # https://docs.github.com/en/rest/issues/comments#list-issue-comments
                    endpoint = f"/repos/{snapshot['repository']}/issues/{pull['number']}/comments?per_page=5&page={(count + 4) // 5}"
                    if pull["number"] not in comment_windows:
                        comment_windows[pull["number"]] = None
                        try:
                            comments = client.get(endpoint)
                            if not isinstance(comments, list) or len(comments) > 5:
                                raise ValueError("Invalid bounded comment response.")
                            normalized = [_outcome_comment(item, snapshot["repository"], pull["number"]) for item in comments]
                            if len({item["id"] for item in normalized}) != len(normalized):
                                raise ValueError("Duplicate outcome comment identity.")
                        except (GitHubApiError, ValueError):
                            # An inaccessible conclusion is unknown, not proof
                            # the completed task repaired the incident.
                            pass
                        else:
                            comment_windows[pull["number"]] = normalized
                    normalized = comment_windows[pull["number"]]
                    if normalized is not None:
                        source.update(comments=normalized, commentsAvailability="available",
                                      commentWindowTruncated=count > len(normalized))
            sources.append(source)
        outcome = _cloud_outcome(record, sources, previous=old)
        if old and old.get("fingerprint") != outcome["fingerprint"]:
            outcome["assessmentRequired"] = True
            outcome["fingerprint"] = _fingerprint({key: value for key, value in outcome.items() if key != "fingerprint"})
        record["outcomeEvidence"] = outcome


def _outcome_comment(value: object, repository: str, number: int) -> dict[str, object]:
    item = _mapping(value, "outcome comment")
    identity = item.get("id")
    url = f"https://github.com/{repository}/pull/{number}#issuecomment-{identity}"
    if (
        type(identity) is not int or identity <= 0 or item.get("html_url") != url
        or item.get("issue_url") != f"https://api.github.com/repos/{repository}/issues/{number}"
        or not isinstance(item.get("body"), str)
    ):
        raise ValueError("Outcome comment does not belong to the exact pull request.")
    for key in ("created_at", "updated_at"):
        parse_aware_iso8601(item.get(key), key)
    user = item.get("user")
    return {
        "id": identity, "url": url,
        "author": user.get("login") if isinstance(user, Mapping) else None,
        "createdAt": item["created_at"], "updatedAt": item["updated_at"],
        "body": _outcome_body(item["body"], 2000),
    }


def observe_commit_comparison(
    client: DelegationReadClient, repository: str, base_sha: str, head_sha: str,
) -> dict[str, object]:
    _require_repository_string({"repository": repository}, "repository")
    if any(not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None
           for sha in (base_sha, head_sha)):
        raise ValueError("Commit comparison requires full lowercase commit SHAs.")
    endpoint = f"/repos/{repository}/compare/{base_sha}...{head_sha}"
    result: dict[str, object] = {
        "repository": repository, "baseSha": base_sha, "headSha": head_sha,
        "url": f"https://api.github.com{endpoint}", "availability": "unknown", "status": "unknown",
        "baseCommitSha": None, "mergeBaseSha": None, "behindBy": None,
    }
    try:
        response = client.get(f"{endpoint}?per_page=1")
    except GitHubApiError:
        result["availability"] = "unavailable"
        return result
    if not isinstance(response, Mapping):
        return result
    base, merge_base = response.get("base_commit"), response.get("merge_base_commit")
    if not isinstance(base, Mapping) or not isinstance(merge_base, Mapping):
        return result
    observed = {
        **result, "url": response.get("url"), "availability": "available", "status": response.get("status"),
        "baseCommitSha": base.get("sha"), "mergeBaseSha": merge_base.get("sha"),
        "behindBy": response.get("behind_by"),
    }
    try:
        validate_commit_comparison(observed, repository)
    except ValidationError:
        # Malformed source metadata is unknown evidence, retaining the requested
        # pair rather than adopting an untrusted response URL or partial proof.
        return result
    return observed


def observe_delegations(
    client: DelegationReadClient,
    repository: str,
    *,
    owned_task_ids: set[str] | None = None,
    owned_issue_numbers: set[int] | None = None,
    known_records: Sequence[Mapping[str, object]] = (),
) -> DelegationObservation:
    unavailable_tasks: set[str] = set()
    if owned_task_ids is None:
        task_records = observe_agent_task_records(client, repository)
    else:
        task_records = observe_capacity_task_records(
            client,
            repository,
            owned_task_ids=owned_task_ids,
            unavailable_tasks=unavailable_tasks,
        )
    tasks = tuple(
        normalize_agent_task(_mapping(record, f"tasks[{index}]"))
        for index, record in enumerate(task_records)
    )
    pull_requests: dict[int, DelegatedPullRequest] = {}
    pull_request_sources: dict[int, Mapping[str, object]] = {}
    pull_request_outcome_sources: dict[int, Mapping[str, object]] = {}
    task_pull_request_ids: dict[str, set[int]] = {}
    for record in known_records:
        task_id = record.get("taskId")
        if not isinstance(task_id, str):
            continue
        for known in record.get("pullRequests", []):
            key = known["databaseId"]
            task_pull_request_ids.setdefault(task_id, set()).add(key)
            expected = DelegatedPullRequest(
                database_id=key, global_id=known.get("globalId"),
                number=known.get("number"), state=PullRequestState.UNKNOWN,
                is_draft=False,
            )
            if key in pull_requests:
                if (
                    pull_requests[key].number != expected.number
                    or expected.global_id is not None
                    and pull_requests[key].global_id != expected.global_id
                ):
                    raise ValueError("Conflicting persisted pull request identities.")
                continue
            pull_requests[key] = expected
            if expected.number is None:
                continue
            try:
                detail = client.get(f"/repos/{repository}/pulls/{expected.number}")
            except GitHubApiError:
                # Preserve the binding, not a stale open/closed claim, when
                # GitHub cannot currently return the exact pull request.
                continue
            pull = _normalize_pull_request(detail, index=expected.number)
            if (
                pull.database_id != key or pull.number != expected.number
                or expected.global_id is not None and pull.global_id != expected.global_id
            ):
                raise ValueError("Pull request detail identity does not match its persisted binding.")
            pull_requests[key] = pull
            head = detail.get("head")
            sha = head.get("sha") if isinstance(head, Mapping) else None
            pull_request_sources[key] = {"headSha": sha if isinstance(sha, str) and sha.strip() else None}
            pull_request_outcome_sources[key] = _outcome_pull_source(repository, {
                "databaseId": key, "globalId": pull.global_id, "number": pull.number,
                "state": pull.state.value, "isDraft": pull.is_draft, "changedFiles": pull.changed_files,
            }, detail)
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
                summary = _normalize_pull_request(record, index=index)
                assert summary.number is not None
                previous = pull_requests.get(summary.database_id)
                if previous is not None:
                    if (
                        previous.number is not None and previous.number != summary.number
                        or previous.global_id is not None and previous.global_id != summary.global_id
                    ):
                        raise ValueError("Branch observation contradicts its persisted pull identity.")
                    if previous.state is not PullRequestState.UNKNOWN:
                        # Reuse one exact detail response so task progress between
                        # discovery reads cannot mix head/file counts in this cycle.
                        task_pull_request_ids.setdefault(task.task_id, set()).add(previous.database_id)
                        continue
                detail = _mapping(
                    client.get(f"/repos/{repository}/pulls/{summary.number}"),
                    f"pull_requests[{index}]",
                )
                pull_request = _normalize_pull_request(detail, index=index)
                if (
                    pull_request.database_id != summary.database_id
                    or pull_request.global_id != summary.global_id
                    or pull_request.number != summary.number
                ):
                    raise ValueError(
                        f"Pull request {summary.number} detail identity does not "
                        "match its branch observation."
                    )
                previous = pull_requests.get(pull_request.database_id)
                if previous is not None and previous.state is not PullRequestState.UNKNOWN and previous != pull_request:
                    raise ValueError(
                        f"Pull request {pull_request.database_id} changed "
                        "across branch observations."
                    )
                pull_requests[pull_request.database_id] = pull_request
                task_pull_request_ids.setdefault(task.task_id, set()).add(pull_request.database_id)
                head = detail.get("head")
                head_sha = head.get("sha") if isinstance(head, Mapping) else None
                source = {
                    "headSha": head_sha
                    if isinstance(head_sha, str) and head_sha.strip()
                    else None,
                }
                previous_source = pull_request_sources.get(pull_request.database_id)
                if previous_source is not None and previous_source != source:
                    raise ValueError(
                        f"Pull request {pull_request.database_id} head changed "
                        "across branch observations."
                    )
                pull_request_sources[pull_request.database_id] = source
                pull_request_outcome_sources[pull_request.database_id] = _outcome_pull_source(repository, {
                    "databaseId": pull_request.database_id, "globalId": pull_request.global_id,
                    "number": pull_request.number, "state": pull_request.state.value,
                    "isDraft": pull_request.is_draft, "changedFiles": pull_request.changed_files,
                }, detail)
        for artifact in task.pull_artifacts:
            assert artifact.database_id is not None
            key = artifact.database_id
            task_pull_request_ids.setdefault(task.task_id, set()).add(key)
            pull_requests.setdefault(
                key,
                DelegatedPullRequest(
                    database_id=artifact.database_id,
                    global_id=artifact.global_id,
                    state=PullRequestState.UNKNOWN,
                    is_draft=False,
                ),
            )

    issues: list[DelegatedIssue] = []
    unavailable_issues: set[int] = set()
    for issue_number in sorted(owned_issue_numbers or set()):
        try:
            record = client.get(f"/repos/{repository}/issues/{issue_number}")
        except GitHubApiError:
            unavailable_issues.add(issue_number)
            continue
        issues.append(_normalize_issue(record, expected_number=issue_number))

    return DelegationObservation(
        tasks=tasks,
        pull_requests=tuple(
            pull_requests[key] for key in sorted(pull_requests)
        ),
        issues=tuple(issues),
        evidence=CapacityEvidence(
            owned_task_inventory_complete=all(task_pull_request_ids.get(task_id) for task_id in unavailable_tasks),
            pull_request_inventory_complete=all(
                pull.state is not PullRequestState.UNKNOWN for pull in pull_requests.values()
            ),
        ),
        pull_request_sources=pull_request_sources,
        task_pull_request_ids={key: tuple(sorted(value)) for key, value in task_pull_request_ids.items()},
        unavailable_task_ids=frozenset(unavailable_tasks),
        unavailable_issue_numbers=frozenset(unavailable_issues),
        pull_request_outcome_sources=pull_request_outcome_sources,
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
    unavailable_tasks: set[str] | None = None,
) -> list[object]:
    """Read shepherd-owned tasks exactly plus repository-wide running tasks."""
    records_by_id: dict[str, object] = {}
    for task_id in sorted(owned_task_ids):
        endpoint = f"/agents/repos/{repository}/tasks/{task_id}"
        try:
            record = client.get(endpoint)
        except GitHubApiError as exc:
            if unavailable_tasks is not None:
                unavailable_tasks.add(task_id)
                continue
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
        # A closed detail response supplies merged_at:null for an unmerged PR.
        # An omitted field or contradictory merged flag is incomplete evidence,
        # not permission to retire a potentially merged or still-open attempt.
        merged_at = pull_request.get("merged_at")
        if (
            "merged_at" not in pull_request
            or "merged" in pull_request
            and pull_request["merged"] is not (merged_at is not None)
        ):
            state = PullRequestState.UNKNOWN
        elif merged_at is None:
            state = PullRequestState.CLOSED
        else:
            try:
                parse_aware_iso8601(merged_at, "merged_at")
            except ValueError:
                state = PullRequestState.UNKNOWN
            else:
                state = PullRequestState.MERGED
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
    changed_files = pull_request.get("changed_files")
    if changed_files is not None and (
        not isinstance(changed_files, int)
        or isinstance(changed_files, bool)
        or changed_files < 0
    ):
        raise ValueError(
            f"pull_requests[{index}].changed_files must be nonnegative when supplied."
        )
    # Before merge this is a synthetic test commit, not the landed fix.
    # After merge it identifies the base-branch merge/squash/rebase result:
    # https://docs.github.com/en/rest/pulls/pulls#get-a-pull-request
    merge_sha = pull_request.get("merge_commit_sha")
    if state is not PullRequestState.MERGED or not isinstance(merge_sha, str) or re.fullmatch(r"[0-9a-fA-F]{40}", merge_sha) is None:
        merge_sha = None
    return DelegatedPullRequest(
        database_id=database_id,
        global_id=global_id,
        state=state,
        is_draft=is_draft,
        number=number,
        changed_files=changed_files,
        human_authored=_human_identity(pull_request.get("user")),
        merged_at=parse_aware_iso8601(pull_request["merged_at"], "merged_at")
        if state is PullRequestState.MERGED else None,
        merge_commit_sha=merge_sha.lower() if merge_sha is not None else None,
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
    human_identities = [_human_identity(assignee) for assignee in assignees]
    human_assigned = (
        True
        if any(identity is True for identity in human_identities)
        else None
        if any(identity is None for identity in human_identities)
        else False
    )
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
        human_assigned=human_assigned,
    )


def _human_identity(value: object) -> bool | None:
    if not isinstance(value, Mapping):
        return None
    login = value.get("login")
    account_type = value.get("type")
    if (
        not isinstance(login, str)
        or not login
        or account_type not in {"User", "Bot"}
    ):
        return None
    return account_type == "User" and not login.casefold().endswith("[bot]")


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object.")
    return value
