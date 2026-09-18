from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import re
from typing import Any, Literal
from urllib.parse import quote, urlencode, urlsplit

from ci_shepherd.delegations import AgentTask, normalize_agent_task
from ci_shepherd.github import GitHubApiError, GitHubClient
from ci_shepherd.pull_requests import build_pull_request_current_state

from .models import ActionKind, JobKey, JobObservation, RunObservation, WorkflowItem, WorkflowKey


_PR_EVENTS = frozenset({"pull_request", "pull_request_target", "merge_group"})
_FAILED_CONCLUSIONS = frozenset({"failure", "timed_out"})
_RELEVANT_RUN_CONCLUSIONS = frozenset(
    {"success", "failure", "timed_out", "action_required", "startup_failure"}
)
_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
_TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z"
)

RecoveryStatus = Literal[
    "not_requested",
    "passed",
    "failed",
    "pending",
    "unavailable",
]
IssueSearchStatus = Literal["zero", "one", "ambiguous", "unavailable"]


@dataclass(frozen=True, slots=True)
class ReadError:
    scope: str
    code: str
    endpoint: str
    detail: str


@dataclass(frozen=True, slots=True)
class WorkflowObservation:
    key: WorkflowKey
    workflow_path: str
    workflow_name: str
    runs: tuple[RunObservation, ...]
    latest_completed: RunObservation | None
    pending_runs: tuple[RunObservation, ...]
    complete: bool
    errors: tuple[ReadError, ...]


@dataclass(frozen=True, slots=True)
class TrackedRunObservation:
    item_id: int
    run: RunObservation | None
    error: ReadError | None


@dataclass(frozen=True, slots=True)
class ReaderSnapshot:
    observed_at: str
    repository: str
    repository_id: int | None
    branch: str
    default_branch: str | None
    workflows: tuple[WorkflowObservation, ...]
    tracked_wait_runs: tuple[TrackedRunObservation, ...]
    complete: bool
    errors: tuple[ReadError, ...]
    request_count: int


@dataclass(frozen=True, slots=True)
class RunDetailResult:
    run: RunObservation | None
    complete: bool
    recovery: RecoveryStatus
    matched_job_ids: tuple[int, ...]
    missing_jobs: tuple[JobKey, ...]
    logged_job_ids: tuple[int, ...]
    truncated_log_job_ids: tuple[int, ...]
    unavailable_log_job_ids: tuple[int, ...]
    errors: tuple[ReadError, ...]
    request_count: int


@dataclass(frozen=True, slots=True)
class TaskObservation:
    task_id: str
    state: str
    url: str | None
    repository_id: int
    updated_at: str
    session_count: int
    pull_request_database_ids: tuple[int, ...]
    branch_artifacts: tuple[TaskBranchArtifact, ...]
    outcome: str | None
    explanation: str | None
    explanation_available: bool


@dataclass(frozen=True, slots=True)
class PullRequestObservation:
    number: int
    state: str
    merged: bool
    draft: bool
    url: str
    head_repository: str
    head_ref: str
    head_sha: str
    base_repository: str
    base_ref: str
    checks_state: str
    checks_complete: bool
    review_decision: str
    review_complete: bool
    complete: bool
    incomplete_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TaskBranchArtifact:
    head_ref: str
    base_ref: str


@dataclass(frozen=True, slots=True)
class ItemRefresh:
    item_id: int
    observed_at: str
    runs: tuple[RunObservation, ...]
    failure_run: RunObservation | None
    wait_run: RunObservation | None
    recovery: RecoveryStatus
    recovery_run: RunObservation | None
    issue: IssueObservation | None
    task: TaskObservation | None
    pull_request: PullRequestObservation | None
    pre_write: bool
    complete: bool
    errors: tuple[ReadError, ...]
    request_count: int


@dataclass(frozen=True, slots=True)
class IssueObservation:
    number: int
    url: str
    title: str
    marker: str
    assignees: tuple[str, ...]
    copilot_assigned: bool
    human_assigned: bool


@dataclass(frozen=True, slots=True)
class IssueSearchResult:
    status: IssueSearchStatus
    issue: IssueObservation | None
    candidate_numbers: tuple[int, ...]
    errors: tuple[ReadError, ...]
    request_count: int


@dataclass(frozen=True, slots=True)
class RepairFileObservation:
    path: str
    status: str
    additions: int
    deletions: int
    changes: int
    url: str


@dataclass(frozen=True, slots=True)
class RepairCommentObservation:
    comment_id: int
    author: str
    body: str
    url: str
    created_at: str
    updated_at: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class RepairCheckObservation:
    check_run_id: int
    name: str
    status: str
    conclusion: str
    url: str
    details_url: str | None
    output_title: str | None
    output_summary: str | None
    output_text: str | None
    log_excerpt: str | None
    log_truncated: bool
    log_available: bool


@dataclass(frozen=True, slots=True)
class RepairEvidenceResult:
    item_id: int
    observed_at: str
    pull_request_number: int | None
    head_sha: str | None
    body: str | None
    body_truncated: bool
    total_changed_files: int | None
    files: tuple[RepairFileObservation, ...]
    files_complete: bool
    bot_comments: tuple[RepairCommentObservation, ...]
    comments_complete: bool
    failed_checks: tuple[RepairCheckObservation, ...]
    checks_complete: bool
    complete: bool
    limitations: tuple[str, ...]
    errors: tuple[ReadError, ...]
    request_count: int


class WorkflowReader:
    """Focused GET-only reader for one repository/branch workflow loop."""

    def __init__(
        self,
        *,
        client: GitHubClient,
        clock: Callable[[], datetime],
        request_count: Callable[[], int],
        max_log_bytes: int = 32_768,
        max_failed_logs: int = 3,
    ) -> None:
        if not callable(clock):
            raise TypeError("clock must be callable.")
        if not callable(request_count):
            raise TypeError(
                "request_count must expose the GitHubClient request_observer count."
            )
        if max_log_bytes < 1:
            raise ValueError("max_log_bytes must be positive.")
        if max_failed_logs < 0:
            raise ValueError("max_failed_logs must be nonnegative.")
        self._client = client
        self._clock = clock
        self._request_count = request_count
        self._max_log_bytes = max_log_bytes
        self._max_failed_logs = max_failed_logs
        self._repository_ids: dict[str, int] = {}

    def observe(
        self,
        *,
        repository: str,
        branch: str,
        tracked_items: Sequence[WorkflowItem],
        workflow_ids: Collection[int] | None = None,
    ) -> ReaderSnapshot:
        started = self._requests()
        observed_at = _format_time(self._clock())
        errors: list[ReadError] = []
        workflows: list[WorkflowObservation] = []
        tracked_wait_runs: list[TrackedRunObservation] = []
        default_branch: str | None = None
        repository_id: int | None = None

        try:
            repository_payload = self._client.get(f"/repos/{repository}")
            default_branch = _validate_repository(repository_payload, repository)
            repository_id = _positive_int(
                repository_payload.get("id"),
                "repository.id",
            )
            self._repository_ids[repository.casefold()] = repository_id
        except (GitHubApiError, TypeError, ValueError) as error:
            errors.append(
                _error("repository", "repository-unavailable", f"/repos/{repository}", error)
            )
            return ReaderSnapshot(
                observed_at=observed_at,
                repository=repository,
                repository_id=None,
                branch=branch,
                default_branch=None,
                workflows=(),
                tracked_wait_runs=(),
                complete=False,
                errors=tuple(errors),
                request_count=self._requests() - started,
            )

        branch_endpoint = (
            f"/repos/{repository}/branches/{quote(branch, safe='')}"
        )
        try:
            branch_payload = self._client.get(branch_endpoint)
            _validate_branch(branch_payload, branch)
        except (GitHubApiError, TypeError, ValueError) as error:
            errors.append(
                _error(
                    "branch",
                    "branch-unavailable",
                    branch_endpoint,
                    error,
                )
            )
            return ReaderSnapshot(
                observed_at=observed_at,
                repository=repository,
                repository_id=repository_id,
                branch=branch,
                default_branch=default_branch,
                workflows=(),
                tracked_wait_runs=(),
                complete=False,
                errors=tuple(errors),
                request_count=self._requests() - started,
            )

        inventory_endpoint = f"/repos/{repository}/actions/workflows"
        try:
            inventory = self._client.get_paged_inventory(
                inventory_endpoint,
                key="workflows",
            )
            if not inventory.complete:
                raise ValueError("Active workflow inventory pagination was incomplete.")
            raw_workflows = inventory.items
        except (GitHubApiError, TypeError, ValueError) as error:
            errors.append(
                _error(
                    "workflow-inventory",
                    "workflow-inventory-unavailable",
                    inventory_endpoint,
                    error,
                )
            )
            raw_workflows = ()

        allowed = frozenset(workflow_ids) if workflow_ids is not None else None
        if allowed is not None and any(
            not isinstance(workflow_id, int)
            or isinstance(workflow_id, bool)
            or workflow_id < 1
            for workflow_id in allowed
        ):
            raise ValueError("workflow_ids must contain positive integers.")
        active_workflows: list[tuple[int, str, str]] = []
        inventory_active_ids: set[int] = set()
        try:
            for index, raw_workflow in enumerate(raw_workflows):
                workflow_id, path, name, active = _normalize_workflow(
                    raw_workflow,
                    index=index,
                )
                if active:
                    if workflow_id in inventory_active_ids:
                        raise ValueError(
                            f"Active workflow ID {workflow_id} appears more than once."
                        )
                    inventory_active_ids.add(workflow_id)
                if active and (allowed is None or workflow_id in allowed):
                    active_workflows.append((workflow_id, path, name))
        except (TypeError, ValueError) as error:
            errors.append(
                _error(
                    "workflow-inventory",
                    "malformed-workflow-inventory",
                    inventory_endpoint,
                    error,
                )
            )
            active_workflows = []

        if allowed is not None:
            missing = tuple(sorted(allowed - inventory_active_ids))
            if missing:
                errors.append(
                    ReadError(
                        scope="workflow-inventory",
                        code="workflow-allowlist-missing",
                        endpoint=inventory_endpoint,
                        detail=(
                            "Requested workflow IDs are not active in the "
                            f"authoritative inventory: {missing!r}."
                        ),
                    )
                )

        ordered_workflows = sorted(active_workflows)
        with ThreadPoolExecutor(max_workers=4) as executor:
            observations = executor.map(
                lambda workflow: self._read_workflow_window(
                    repository=repository,
                    branch=branch,
                    workflow_id=workflow[0],
                    workflow_path=workflow[1],
                    workflow_name=workflow[2],
                ),
                ordered_workflows,
            )
            workflows.extend(observations)
        for observation in workflows:
            errors.extend(observation.errors)

        known_runs = {
            run.run_id
            for workflow_observation in workflows
            for run in workflow_observation.runs
        }
        for tracked in sorted(tracked_items, key=lambda item: item.id):
            if (
                tracked.repository.casefold() != repository.casefold()
                or tracked.branch != branch
                or tracked.wait_run_id is None
            ):
                continue
            if tracked.wait_run_id in known_runs:
                matching = next(
                    run
                    for workflow_observation in workflows
                    for run in workflow_observation.runs
                    if run.run_id == tracked.wait_run_id
                )
                tracked_wait_runs.append(
                    TrackedRunObservation(tracked.id, matching, None)
                )
                continue
            endpoint = (
                f"/repos/{repository}/actions/runs/{tracked.wait_run_id}"
            )
            try:
                raw_run = self._client.get(endpoint)
                normalized = _normalize_run(
                    raw_run,
                    repository=repository,
                    branch=branch,
                    workflow_id=tracked.workflow_id,
                    workflow_path=tracked.workflow_path,
                    workflow_name=tracked.workflow_name,
                )
                if normalized is None:
                    raise ValueError("The fixed wait run is not a primary repository run.")
                tracked_wait_runs.append(
                    TrackedRunObservation(tracked.id, normalized, None)
                )
            except (GitHubApiError, TypeError, ValueError) as error:
                read_error = _error(
                    f"item:{tracked.id}:wait-run",
                    "wait-run-unavailable",
                    endpoint,
                    error,
                )
                errors.append(read_error)
                tracked_wait_runs.append(
                    TrackedRunObservation(tracked.id, None, read_error)
                )

        complete = not errors and all(workflow.complete for workflow in workflows)
        return ReaderSnapshot(
            observed_at=observed_at,
            repository=repository,
            repository_id=repository_id,
            branch=branch,
            default_branch=default_branch,
            workflows=tuple(workflows),
            tracked_wait_runs=tuple(tracked_wait_runs),
            complete=complete,
            errors=tuple(errors),
            request_count=self._requests() - started,
        )

    def read_run_details(
        self,
        run: RunObservation,
        *,
        established_jobs: tuple[JobKey, ...] = (),
    ) -> RunDetailResult:
        started = self._requests()
        return self._read_run_details(
            run,
            established_jobs=established_jobs,
            started=started,
            include_logs=True,
        )

    def refresh_item(
        self,
        item: WorkflowItem,
        *,
        action: ActionKind | None = None,
    ) -> ItemRefresh:
        pre_write_requested = action is not None
        started = self._requests()
        errors: list[ReadError] = []
        window = self._read_workflow_window(
            repository=item.repository,
            branch=item.branch,
            workflow_id=item.workflow_id,
            workflow_path=item.workflow_path,
            workflow_name=item.workflow_name,
        )
        errors.extend(window.errors)

        failure_run = self._read_exact_run(
            item,
            item.failure_run_id,
            scope=f"item:{item.id}:failure-run",
            errors=errors,
        )
        wait_run = (
            self._read_exact_run(
                item,
                item.wait_run_id,
                scope=f"item:{item.id}:wait-run",
                errors=errors,
            )
            if item.wait_run_id is not None
            else None
        )

        if pre_write_requested and failure_run is not None:
            details = self._read_run_details(
                failure_run,
                established_jobs=item.failed_jobs,
                started=self._requests(),
                include_logs=False,
            )
            errors.extend(details.errors)
            failure_run = details.run

        recovery: RecoveryStatus = "unavailable"
        recovery_run: RunObservation | None = None
        target = wait_run
        if target is None and failure_run is not None:
            candidates = [
                candidate
                for candidate in window.runs
                if _is_later_execution(candidate, failure_run, item.failure_attempt)
            ]
            completed_candidates = [
                candidate
                for candidate in candidates
                if candidate.status == "completed"
            ]
            if completed_candidates:
                target = max(completed_candidates, key=_run_order_key)
            elif failure_run.attempt > item.failure_attempt:
                target = failure_run
            elif candidates:
                target = max(candidates, key=_run_order_key)
            else:
                target = failure_run

        if target is not None and target.status != "completed":
            recovery = "pending"
        elif (
            target is not None
            and failure_run is not None
            and target.run_id == failure_run.run_id
            and target.attempt == item.failure_attempt
            and target.conclusion in {
                "failure",
                "timed_out",
                "action_required",
                "startup_failure",
            }
        ):
            # The item's established failed jobs already bind this immutable
            # attempt. Polling only needs fresh run metadata; logs are selected
            # later if a judgment actually needs them.
            recovery = "failed"
        elif target is not None:
            details = self._read_run_details(
                target,
                established_jobs=item.failed_jobs,
                started=self._requests(),
                include_logs=False,
            )
            errors.extend(details.errors)
            recovery = details.recovery
            if details.run is not None:
                if details.run.run_id == item.failure_run_id:
                    failure_run = details.run
                if item.wait_run_id == details.run.run_id:
                    wait_run = details.run
            if details.recovery == "passed":
                recovery_run = details.run

        issue = (
            self._read_bound_issue(item, errors)
            if item.issue_number is not None
            else None
        )
        task = self._read_task(item, errors) if item.task_id is not None else None
        pull_number = item.pull_request_number
        expected_branches: tuple[TaskBranchArtifact, ...] = ()
        expected_database_ids: tuple[int, ...] = ()
        if task is not None:
            expected_database_ids = task.pull_request_database_ids
            expected_branches = tuple(
                artifact
                for artifact in task.branch_artifacts
                if artifact.base_ref == item.branch
            )
            if len(expected_branches) != len(task.branch_artifacts):
                errors.append(
                    ReadError(
                        scope=f"item:{item.id}:task-pull-request",
                        code="task-branch-base-mismatch",
                        endpoint=(
                            f"/agents/repos/{item.repository}/tasks/"
                            f"{item.task_id}"
                        ),
                        detail=(
                            "A task branch artifact targets a different base "
                            "branch than the monitored workflow."
                        ),
                    )
                )
            if pull_number is None and expected_branches:
                pull_number = self._find_task_pull_number(
                    item,
                    expected_branches,
                    expected_database_ids=expected_database_ids,
                    errors=errors,
                )
            elif pull_number is None and task.pull_request_database_ids:
                errors.append(
                    ReadError(
                        scope=f"item:{item.id}:task-pull-request",
                        code="task-pull-request-number-unavailable",
                        endpoint=(
                            f"/agents/repos/{item.repository}/tasks/"
                            f"{item.task_id}"
                        ),
                        detail=(
                            "The exact task exposes a pull request database ID "
                            "but no REST number or branch binding."
                        ),
                    )
                )

        pull_request = (
            self._read_pull_request(
                item,
                pull_number,
                expected_branches=expected_branches,
                expected_database_ids=expected_database_ids,
                errors=errors,
            )
            if pull_number is not None
            else None
        )
        complete = (
            window.complete
            and failure_run is not None
            and not errors
            and (item.issue_number is None or issue is not None)
            and (item.task_id is None or task is not None)
            and (pull_number is None or pull_request is not None)
        )
        return ItemRefresh(
            item_id=item.id,
            observed_at=_format_time(self._clock()),
            runs=window.runs,
            failure_run=failure_run,
            wait_run=wait_run,
            recovery=recovery,
            recovery_run=recovery_run,
            issue=issue,
            task=task,
            pull_request=pull_request,
            pre_write=pre_write_requested and complete,
            complete=complete,
            errors=tuple(errors),
            request_count=self._requests() - started,
        )

    def read_repair_evidence(
        self,
        item: WorkflowItem,
        *,
        refresh: ItemRefresh | None = None,
    ) -> RepairEvidenceResult:
        started = self._requests()
        observed_at = _format_time(self._clock())
        number = (
            refresh.pull_request.number
            if refresh is not None and refresh.pull_request is not None
            else item.pull_request_number
        )
        if number is None:
            return RepairEvidenceResult(
                item_id=item.id,
                observed_at=observed_at,
                pull_request_number=None,
                head_sha=None,
                body=None,
                body_truncated=False,
                total_changed_files=None,
                files=(),
                files_complete=False,
                bot_comments=(),
                comments_complete=False,
                failed_checks=(),
                checks_complete=False,
                complete=False,
                limitations=(
                    "No verified owned pull request binding is available.",
                ),
                errors=(),
                request_count=self._requests() - started,
            )

        errors: list[ReadError] = []
        limitations: list[str] = []
        expected_branches = (
            tuple(
                artifact
                for artifact in refresh.task.branch_artifacts
                if artifact.base_ref == item.branch
            )
            if refresh is not None and refresh.task is not None
            else ()
        )
        expected_database_ids = (
            refresh.task.pull_request_database_ids
            if refresh is not None and refresh.task is not None
            else ()
        )
        pull_endpoint = f"/repos/{item.repository}/pulls/{number}"
        try:
            raw_pull = self._client.get(pull_endpoint)
            pull = _validate_pull_request(
                raw_pull,
                repository=item.repository,
                number=number,
                base_ref=item.branch,
                expected_branches=expected_branches,
                expected_database_ids=expected_database_ids,
            )
        except (GitHubApiError, TypeError, ValueError) as error:
            errors.append(
                _error(
                    f"item:{item.id}:repair-pull-request",
                    "repair-pull-request-unavailable",
                    pull_endpoint,
                    error,
                )
            )
            return RepairEvidenceResult(
                item_id=item.id,
                observed_at=observed_at,
                pull_request_number=number,
                head_sha=None,
                body=None,
                body_truncated=False,
                total_changed_files=None,
                files=(),
                files_complete=False,
                bot_comments=(),
                comments_complete=False,
                failed_checks=(),
                checks_complete=False,
                complete=False,
                limitations=("Current owned pull request evidence is unavailable.",),
                errors=tuple(errors),
                request_count=self._requests() - started,
            )

        raw_body = raw_pull.get("body")
        if raw_body is None:
            body = None
            body_truncated = False
            limitations.append("Pull request body is unavailable.")
        elif isinstance(raw_body, str):
            body, body_truncated = _bounded_text(raw_body, 16_384)
        else:
            body = None
            body_truncated = False
            limitations.append("Pull request body has an unexpected shape.")

        changed_files_value = raw_pull.get("changed_files")
        total_changed_files = (
            changed_files_value
            if isinstance(changed_files_value, int)
            and not isinstance(changed_files_value, bool)
            and changed_files_value >= 0
            else None
        )
        if total_changed_files is None:
            limitations.append("Pull request changed-file count is unavailable.")

        files_endpoint = f"/repos/{item.repository}/pulls/{number}/files"
        files: tuple[RepairFileObservation, ...] = ()
        files_complete = False
        try:
            file_inventory = self._client.get_paged_inventory(
                files_endpoint,
                key=None,
            )
            files = tuple(
                _normalize_repair_file(raw_file)
                for raw_file in file_inventory.items
            )
            files_complete = file_inventory.complete and (
                total_changed_files is None
                or total_changed_files == len(files)
            )
            if not files_complete:
                limitations.append("Pull request file inventory is incomplete.")
        except (GitHubApiError, TypeError, ValueError) as error:
            errors.append(
                _error(
                    f"item:{item.id}:repair-files",
                    "repair-files-unavailable",
                    files_endpoint,
                    error,
                )
            )

        comment_query: dict[str, object] = {
            "per_page": 100,
            "page": 1,
        }
        if item.assignment_requested_at is not None:
            comment_query["since"] = item.assignment_requested_at
        comments_endpoint = (
            f"/repos/{item.repository}/issues/{number}/comments?"
            f"{urlencode(comment_query)}"
        )
        bot_comments: tuple[RepairCommentObservation, ...] = ()
        comments_complete = False
        try:
            raw_comments = self._client.get(comments_endpoint)
            if not isinstance(raw_comments, list):
                raise TypeError("Pull request comments response must be a list.")
            normalized_comments = [
                comment
                for raw_comment in raw_comments
                if (comment := _normalize_bot_comment(raw_comment)) is not None
            ]
            normalized_comments.sort(
                key=lambda comment: (comment.created_at, comment.comment_id)
            )
            comments_truncated = len(normalized_comments) > 20
            bot_comments = tuple(normalized_comments[-20:])
            comments_complete = (
                len(raw_comments) < 100 and not comments_truncated
            )
            if not comments_complete:
                limitations.append("Bot comment evidence is incomplete.")
        except (GitHubApiError, TypeError, ValueError) as error:
            errors.append(
                _error(
                    f"item:{item.id}:repair-comments",
                    "repair-comments-unavailable",
                    comments_endpoint,
                    error,
                )
            )

        head_sha = str(pull["head_sha"])
        checks_endpoint = (
            f"/repos/{item.repository}/commits/{head_sha}/check-runs"
        )
        failed_checks: tuple[RepairCheckObservation, ...] = ()
        checks_complete = False
        try:
            check_inventory = self._client.get_paged_inventory(
                checks_endpoint,
                key="check_runs",
            )
            normalized_checks = [
                _normalize_failed_check(
                    raw_check,
                    head_sha=head_sha,
                )
                for raw_check in check_inventory.items
            ]
            failed = [
                check
                for check in normalized_checks
                if check is not None
            ]
            failed.sort(key=lambda check: (check.name, check.check_run_id))
            if not failed:
                limitations.append(
                    "No failed current-head check-run evidence was observed."
                )
            enriched: list[RepairCheckObservation] = []
            logs_remaining = self._max_failed_logs
            for check in failed:
                job_id = _actions_job_id(
                    check.details_url,
                    repository=item.repository,
                )
                if job_id is None:
                    limitations.append(
                        f"Failed check {check.name} has no verified "
                        "Actions job log link."
                    )
                    enriched.append(check)
                    continue
                if logs_remaining <= 0:
                    limitations.append(
                        f"Failed check {check.name} log was not read because "
                        "the bounded log limit was reached."
                    )
                    enriched.append(check)
                    continue
                logs_remaining -= 1
                log_endpoint = (
                    f"/repos/{item.repository}/actions/jobs/{job_id}/logs"
                )
                try:
                    response = self._client.get_text(
                        log_endpoint,
                        max_bytes=self._max_log_bytes,
                    )
                    enriched.append(
                        RepairCheckObservation(
                            check_run_id=check.check_run_id,
                            name=check.name,
                            status=check.status,
                            conclusion=check.conclusion,
                            url=check.url,
                            details_url=check.details_url,
                            output_title=check.output_title,
                            output_summary=check.output_summary,
                            output_text=check.output_text,
                            log_excerpt=response.text,
                            log_truncated=response.truncated,
                            log_available=True,
                        )
                    )
                except (GitHubApiError, TypeError, ValueError) as error:
                    limitations.append(
                        f"Failed check {check.name} Actions log is unavailable."
                    )
                    errors.append(
                        _error(
                            f"item:{item.id}:repair-check:{check.check_run_id}",
                            "repair-check-log-unavailable",
                            log_endpoint,
                            error,
                        )
                    )
                    enriched.append(check)
            failed_checks = tuple(enriched)
            checks_complete = check_inventory.complete
            if not checks_complete:
                limitations.append("Current-head check inventory is incomplete.")
        except (GitHubApiError, TypeError, ValueError) as error:
            errors.append(
                _error(
                    f"item:{item.id}:repair-checks",
                    "repair-checks-unavailable",
                    checks_endpoint,
                    error,
                )
            )

        return RepairEvidenceResult(
            item_id=item.id,
            observed_at=observed_at,
            pull_request_number=number,
            head_sha=head_sha,
            body=body,
            body_truncated=body_truncated,
            total_changed_files=total_changed_files,
            files=files,
            files_complete=files_complete,
            bot_comments=bot_comments,
            comments_complete=comments_complete,
            failed_checks=failed_checks,
            checks_complete=checks_complete,
            complete=(
                files_complete
                and comments_complete
                and checks_complete
                and not errors
            ),
            limitations=tuple(limitations),
            errors=tuple(errors),
            request_count=self._requests() - started,
        )

    def find_tracking_issue(self, item: WorkflowItem) -> IssueSearchResult:
        started = self._requests()
        errors: list[ReadError] = []
        try:
            repository_payload = self._client.get(f"/repos/{item.repository}")
            default_branch = _validate_repository(
                repository_payload,
                item.repository,
            )
        except (GitHubApiError, TypeError, ValueError) as error:
            errors.append(
                _error(
                    f"item:{item.id}:issue-search",
                    "repository-unavailable",
                    f"/repos/{item.repository}",
                    error,
                )
            )
            return IssueSearchResult(
                "unavailable",
                None,
                (),
                tuple(errors),
                self._requests() - started,
            )

        canonical_marker = _canonical_marker(item)
        queries = [
            (
                canonical_marker,
                " ".join(
                    (
                        f"repo:{item.repository}",
                        "is:issue",
                        "is:open",
                        '"ci-shepherd:workflow-repair"',
                        f'"workflow-id={item.workflow_id}"',
                    )
                ),
            )
        ]
        if item.branch == default_branch:
            legacy_marker = (
                f"<!-- automation-broken:{item.workflow_path.rsplit('/', 1)[-1]} -->"
            )
            queries.append(
                (
                    legacy_marker,
                    " ".join(
                        (
                            f"repo:{item.repository}",
                            "is:issue",
                            "is:open",
                            f'"automation-broken:{item.workflow_path.rsplit('/', 1)[-1]}"',
                        )
                    ),
                )
            )

        candidates: dict[int, IssueObservation] = {}
        nominated_numbers: set[int] = set()
        for marker, query in queries:
            endpoint = f"/search/issues?{urlencode({'q': query, 'per_page': 10})}"
            try:
                payload = self._client.get(endpoint)
                numbers = _search_issue_numbers(payload)
                nominated_numbers.update(numbers)
            except (GitHubApiError, TypeError, ValueError) as error:
                errors.append(
                    _error(
                        f"item:{item.id}:issue-search",
                        "issue-search-unavailable",
                        endpoint,
                        error,
                    )
                )
                return IssueSearchResult(
                    "unavailable",
                    None,
                    tuple(sorted(nominated_numbers)),
                    tuple(errors),
                    self._requests() - started,
                )

            for number in numbers:
                issue_endpoint = f"/repos/{item.repository}/issues/{number}"
                try:
                    raw_issue = self._client.get(issue_endpoint)
                    issue = _normalize_issue(
                        raw_issue,
                        repository=item.repository,
                        marker=marker,
                    )
                except (GitHubApiError, TypeError, ValueError) as error:
                    errors.append(
                        _error(
                            f"item:{item.id}:issue:{number}",
                            "issue-unavailable",
                            issue_endpoint,
                            error,
                        )
                    )
                    continue
                if issue is not None:
                    candidates[number] = issue
            if candidates:
                break

        ordered = tuple(candidates[number] for number in sorted(candidates))
        if errors:
            status: IssueSearchStatus = "unavailable"
        elif not ordered:
            status = "zero"
        elif len(ordered) == 1:
            status = "one"
        else:
            status = "ambiguous"
        return IssueSearchResult(
            status=status,
            issue=ordered[0] if status == "one" else None,
            candidate_numbers=tuple(sorted(nominated_numbers)),
            errors=tuple(errors),
            request_count=self._requests() - started,
        )

    def _read_workflow_window(
        self,
        *,
        repository: str,
        branch: str,
        workflow_id: int,
        workflow_path: str,
        workflow_name: str,
    ) -> WorkflowObservation:
        endpoint = (
            f"/repos/{repository}/actions/workflows/{workflow_id}/runs?"
            f"{urlencode({'branch': branch, 'per_page': 100})}"
        )
        key = WorkflowKey(repository, workflow_id, branch)
        errors: list[ReadError] = []
        try:
            payload = self._client.get(endpoint)
            raw_runs, page_complete = _run_window(payload)
            runs = [
                normalized
                for raw_run in raw_runs
                if (
                    normalized := _normalize_run(
                        raw_run,
                        repository=repository,
                        branch=branch,
                        workflow_id=workflow_id,
                        workflow_path=workflow_path,
                        workflow_name=workflow_name,
                    )
                )
                is not None
            ]
        except (GitHubApiError, TypeError, ValueError) as error:
            errors.append(
                _error(
                    f"workflow:{workflow_id}",
                    "workflow-window-unavailable",
                    endpoint,
                    error,
                )
            )
            runs = []
            page_complete = False

        runs.sort(
            key=lambda run: (
                run.run_number,
                run.attempt,
                run.created_at,
                run.run_id,
            ),
            reverse=True,
        )
        latest_completed = next(
            (
                run
                for run in runs
                if run.status == "completed"
                and run.conclusion in _RELEVANT_RUN_CONCLUSIONS
            ),
            None,
        )
        pending_runs = tuple(
            run
            for run in runs
            if run.status != "completed"
            and (
                latest_completed is None
                or run.run_number >= latest_completed.run_number
            )
        )
        runs = [
            *pending_runs,
            *(() if latest_completed is None else (latest_completed,)),
        ]
        runs.sort(
            key=lambda run: (
                run.run_number,
                run.attempt,
                run.created_at,
                run.run_id,
            ),
            reverse=True,
        )
        complete = page_complete or latest_completed is not None
        return WorkflowObservation(
            key=key,
            workflow_path=workflow_path,
            workflow_name=workflow_name,
            runs=tuple(runs),
            latest_completed=latest_completed,
            pending_runs=pending_runs,
            complete=complete and not errors,
            errors=tuple(errors),
        )

    def _read_run_details(
        self,
        run: RunObservation,
        *,
        established_jobs: tuple[JobKey, ...],
        started: int,
        include_logs: bool,
    ) -> RunDetailResult:
        errors: list[ReadError] = []
        endpoint = f"/repos/{run.key.repository}/actions/runs/{run.run_id}"
        try:
            raw_run = self._client.get(endpoint)
            current = _normalize_run(
                raw_run,
                repository=run.key.repository,
                branch=run.key.branch,
                workflow_id=run.key.workflow_id,
                workflow_path=run.workflow_path,
                workflow_name=run.workflow_name,
            )
            if current is None or not _same_run_identity(current, run):
                raise ValueError(
                    "The exact run endpoint returned a different run identity."
                )
        except (GitHubApiError, TypeError, ValueError) as error:
            errors.append(
                _error(
                    f"run:{run.run_id}",
                    "run-detail-unavailable",
                    endpoint,
                    error,
                )
            )
            return RunDetailResult(
                None,
                False,
                "unavailable",
                (),
                established_jobs,
                (),
                (),
                (),
                tuple(errors),
                self._requests() - started,
            )

        jobs_endpoint = (
            f"/repos/{run.key.repository}/actions/runs/{run.run_id}"
            f"/attempts/{current.attempt}/jobs"
        )
        try:
            inventory = self._client.get_paged_inventory(jobs_endpoint, key="jobs")
            jobs = tuple(
                _normalize_job(
                    raw_job,
                    run=current,
                )
                for raw_job in inventory.items
            )
            if len({job.job_id for job in jobs}) != len(jobs):
                raise ValueError("The job inventory contains duplicate job IDs.")
        except (GitHubApiError, TypeError, ValueError) as error:
            errors.append(
                _error(
                    f"run:{run.run_id}:jobs",
                    "job-inventory-unavailable",
                    jobs_endpoint,
                    error,
                )
            )
            return RunDetailResult(
                None,
                False,
                "unavailable",
                (),
                established_jobs,
                (),
                (),
                (),
                tuple(errors),
                self._requests() - started,
            )

        logged: list[int] = []
        truncated: list[int] = []
        unavailable: list[int] = []
        enriched_jobs: list[JobObservation] = []
        logs_remaining = self._max_failed_logs
        for observed_job in jobs:
            excerpt: str | None = None
            log_truncated = False
            if include_logs and observed_job.conclusion in _FAILED_CONCLUSIONS:
                if logs_remaining <= 0:
                    unavailable.append(observed_job.job_id)
                else:
                    logs_remaining -= 1
                    log_endpoint = (
                        f"/repos/{run.key.repository}/actions/jobs/"
                        f"{observed_job.job_id}/logs"
                    )
                    try:
                        response = self._client.get_text(
                            log_endpoint,
                            max_bytes=self._max_log_bytes,
                        )
                        excerpt = response.text
                        log_truncated = response.truncated
                        logged.append(observed_job.job_id)
                        if response.truncated:
                            truncated.append(observed_job.job_id)
                    except (GitHubApiError, TypeError, ValueError) as error:
                        unavailable.append(observed_job.job_id)
                        errors.append(
                            _error(
                                f"run:{run.run_id}:job:{observed_job.job_id}:log",
                                "job-log-unavailable",
                                log_endpoint,
                                error,
                            )
                        )
            enriched_jobs.append(
                JobObservation(
                    run_id=observed_job.run_id,
                    attempt=observed_job.attempt,
                    job_id=observed_job.job_id,
                    key=observed_job.key,
                    status=observed_job.status,
                    conclusion=observed_job.conclusion,
                    started_at=observed_job.started_at,
                    completed_at=observed_job.completed_at,
                    url=observed_job.url,
                    log_excerpt=excerpt,
                    log_truncated=log_truncated,
                )
            )

        detailed_run = RunObservation(
            key=current.key,
            workflow_path=current.workflow_path,
            workflow_name=current.workflow_name,
            run_id=current.run_id,
            run_number=current.run_number,
            attempt=current.attempt,
            head_sha=current.head_sha,
            event=current.event,
            status=current.status,
            conclusion=current.conclusion,
            created_at=current.created_at,
            updated_at=current.updated_at,
            url=current.url,
            jobs_complete=inventory.complete,
            jobs=tuple(enriched_jobs),
        )
        recovery, matched, missing = _recovery(
            detailed_run,
            established_jobs,
        )
        return RunDetailResult(
            run=detailed_run,
            complete=inventory.complete,
            recovery=recovery,
            matched_job_ids=matched,
            missing_jobs=missing,
            logged_job_ids=tuple(logged),
            truncated_log_job_ids=tuple(truncated),
            unavailable_log_job_ids=tuple(unavailable),
            errors=tuple(errors),
            request_count=self._requests() - started,
        )

    def _read_exact_run(
        self,
        item: WorkflowItem,
        run_id: int,
        *,
        scope: str,
        errors: list[ReadError],
    ) -> RunObservation | None:
        endpoint = f"/repos/{item.repository}/actions/runs/{run_id}"
        try:
            raw_run = self._client.get(endpoint)
            result = _normalize_run(
                raw_run,
                repository=item.repository,
                branch=item.branch,
                workflow_id=item.workflow_id,
                workflow_path=item.workflow_path,
                workflow_name=item.workflow_name,
            )
            if result is None or result.run_id != run_id:
                raise ValueError("The exact run endpoint returned an unrelated run.")
            return result
        except (GitHubApiError, TypeError, ValueError) as error:
            errors.append(
                _error(scope, "exact-run-unavailable", endpoint, error)
            )
            return None

    def _read_task(
        self,
        item: WorkflowItem,
        errors: list[ReadError],
    ) -> TaskObservation | None:
        endpoint = f"/agents/repos/{item.repository}/tasks/{item.task_id}"
        try:
            raw_task = self._client.get(endpoint)
            if not isinstance(raw_task, Mapping):
                raise TypeError("The task response must be an object.")
            task = normalize_agent_task(raw_task)
            if task.task_id != item.task_id:
                raise ValueError("The exact task endpoint returned a different task.")
            raw_repository = raw_task.get("repository")
            if not isinstance(raw_repository, Mapping):
                raise ValueError("The task repository identity is unavailable.")
            repository_id = _positive_int(
                raw_repository.get("id"),
                "task.repository.id",
            )
            full_name = raw_repository.get("full_name")
            if full_name is not None and not _same_repository(
                full_name,
                item.repository,
            ):
                raise ValueError("The task repository does not match the item.")
            expected_repository_id = self._repository_ids.get(
                item.repository.casefold()
            )
            if (
                expected_repository_id is not None
                and repository_id != expected_repository_id
            ):
                raise ValueError("The task repository does not match the item.")
            pull_database_ids, branch_artifacts = _task_artifact_bindings(
                task
            )
            url_value = raw_task.get("html_url", raw_task.get("url"))
            url = url_value if isinstance(url_value, str) and url_value else None
            return TaskObservation(
                task_id=task.task_id,
                state=task.state.value,
                url=url,
                repository_id=repository_id,
                updated_at=_format_time(task.updated_at),
                session_count=task.session_count,
                pull_request_database_ids=pull_database_ids,
                branch_artifacts=branch_artifacts,
                outcome=None,
                explanation=None,
                explanation_available=False,
            )
        except (GitHubApiError, TypeError, ValueError) as error:
            errors.append(
                _error(
                    f"item:{item.id}:task",
                    "owned-task-unavailable",
                    endpoint,
                    error,
                )
            )
            return None

    def _read_bound_issue(
        self,
        item: WorkflowItem,
        errors: list[ReadError],
    ) -> IssueObservation | None:
        endpoint = f"/repos/{item.repository}/issues/{item.issue_number}"
        try:
            raw_issue = self._client.get(endpoint)
            issue = _normalize_issue(
                raw_issue,
                repository=item.repository,
                marker=_canonical_marker(item),
            )
            if issue is not None and issue.number == item.issue_number:
                return issue

            repository_payload = self._client.get(f"/repos/{item.repository}")
            default_branch = _validate_repository(
                repository_payload,
                item.repository,
            )
            if item.branch != default_branch:
                raise ValueError(
                    "The bound issue does not contain the canonical marker."
                )
            legacy_marker = (
                f"<!-- automation-broken:"
                f"{item.workflow_path.rsplit('/', 1)[-1]} -->"
            )
            issue = _normalize_issue(
                raw_issue,
                repository=item.repository,
                marker=legacy_marker,
            )
            if issue is None or issue.number != item.issue_number:
                raise ValueError(
                    "The bound issue does not contain a verified tracking marker."
                )
            return issue
        except (GitHubApiError, TypeError, ValueError) as error:
            errors.append(
                _error(
                    f"item:{item.id}:issue",
                    "bound-issue-unavailable",
                    endpoint,
                    error,
                )
            )
            return None

    def _find_task_pull_number(
        self,
        item: WorkflowItem,
        branches: tuple[TaskBranchArtifact, ...],
        *,
        expected_database_ids: tuple[int, ...],
        errors: list[ReadError],
    ) -> int | None:
        owner = item.repository.split("/", 1)[0]
        candidates: set[int] = set()
        for branch_artifact in branches:
            query = urlencode({
                "state": "all",
                "head": f"{owner}:{branch_artifact.head_ref}",
                "per_page": 10,
            })
            endpoint = (
                f"/repos/{item.repository}/pulls?{query}"
            )
            try:
                raw_pulls = self._client.get(endpoint)
                if not isinstance(raw_pulls, list):
                    raise TypeError("Head-filtered pull request response must be a list.")
                if len(raw_pulls) >= 10:
                    raise ValueError(
                        "Head-filtered pull request response may be truncated."
                    )
                for raw_pull in raw_pulls:
                    if not isinstance(raw_pull, Mapping):
                        raise TypeError("Head-filtered pull request entry must be an object.")
                    number = _positive_int(
                        raw_pull.get("number"),
                        "pull.number",
                    )
                    _validate_pull_request(
                        raw_pull,
                        repository=item.repository,
                        number=number,
                        base_ref=item.branch,
                        expected_branches=(branch_artifact,),
                        expected_database_ids=expected_database_ids,
                    )
                    candidates.add(number)
            except (GitHubApiError, TypeError, ValueError) as error:
                errors.append(
                    _error(
                        f"item:{item.id}:task-pull-request",
                        "task-pull-request-search-unavailable",
                        endpoint,
                        error,
                    )
                )
                return None
        if len(candidates) > 1:
            errors.append(
                ReadError(
                    scope=f"item:{item.id}:task-pull-request",
                    code="task-pull-request-ambiguous",
                    endpoint=f"/repos/{item.repository}/pulls",
                    detail="The owned task branches identify multiple pull requests.",
                )
            )
            return None
        return next(iter(candidates), None)

    def _read_pull_request(
        self,
        item: WorkflowItem,
        number: int,
        *,
        expected_branches: tuple[TaskBranchArtifact, ...],
        expected_database_ids: tuple[int, ...],
        errors: list[ReadError],
    ) -> PullRequestObservation | None:
        endpoint = f"/repos/{item.repository}/pulls/{number}"
        try:
            raw_pull = self._client.get(endpoint)
            pull = _validate_pull_request(
                raw_pull,
                repository=item.repository,
                number=number,
                base_ref=item.branch,
                expected_branches=expected_branches,
                expected_database_ids=expected_database_ids,
            )
        except (GitHubApiError, TypeError, ValueError) as error:
            errors.append(
                _error(
                    f"item:{item.id}:pull-request",
                    "pull-request-unavailable",
                    endpoint,
                    error,
                )
            )
            return None

        head_sha = pull["head_sha"]
        check_endpoint = (
            f"/repos/{item.repository}/commits/{head_sha}/check-runs"
        )
        status_endpoint = (
            f"/repos/{item.repository}/commits/{head_sha}/status"
        )
        reviews_endpoint = (
            f"/repos/{item.repository}/pulls/{number}/reviews"
        )
        check_runs: Sequence[Any] | None
        combined_status: Mapping[str, Any] | None
        reviews: Sequence[Any] | None
        checks_inventory_complete = True
        reviews_inventory_complete = True
        try:
            check_inventory = self._client.get_paged_inventory(
                check_endpoint,
                key="check_runs",
            )
            check_runs = check_inventory.items
            checks_inventory_complete = check_inventory.complete
        except (GitHubApiError, TypeError, ValueError) as error:
            check_runs = None
            checks_inventory_complete = False
            errors.append(
                _error(
                    f"item:{item.id}:pull-request:checks",
                    "pull-request-checks-unavailable",
                    check_endpoint,
                    error,
                )
            )
        try:
            raw_status = self._client.get(status_endpoint)
            if not isinstance(raw_status, Mapping):
                raise TypeError("Combined status response must be an object.")
            if raw_status.get("sha") != head_sha:
                raise ValueError("Combined status is for a different head SHA.")
            combined_status = raw_status
        except (GitHubApiError, TypeError, ValueError) as error:
            combined_status = None
            errors.append(
                _error(
                    f"item:{item.id}:pull-request:status",
                    "pull-request-status-unavailable",
                    status_endpoint,
                    error,
                )
            )
        try:
            reviews_inventory = self._client.get_paged_inventory(
                reviews_endpoint,
                key=None,
            )
            reviews = reviews_inventory.items
            reviews_inventory_complete = reviews_inventory.complete
        except (GitHubApiError, TypeError, ValueError) as error:
            reviews = None
            reviews_inventory_complete = False
            errors.append(
                _error(
                    f"item:{item.id}:pull-request:reviews",
                    "pull-request-reviews-unavailable",
                    reviews_endpoint,
                    error,
                )
            )

        current_state = build_pull_request_current_state(
            raw_pull,
            check_runs=check_runs,
            combined_status=combined_status,
            reviews=reviews,
        )
        checks = current_state["checks"]
        review = current_state["review"]
        incomplete_reasons = list(current_state["incompleteReasons"])
        if not checks_inventory_complete:
            incomplete_reasons.append("check-run pagination is incomplete")
        if not reviews_inventory_complete:
            incomplete_reasons.append("review pagination is incomplete")
        return PullRequestObservation(
            number=number,
            state=pull["state"],
            merged=pull["merged"],
            draft=pull["draft"],
            url=pull["url"],
            head_repository=pull["head_repository"],
            head_ref=pull["head_ref"],
            head_sha=head_sha,
            base_repository=pull["base_repository"],
            base_ref=pull["base_ref"],
            checks_state=str(checks["state"]),
            checks_complete=bool(checks["complete"] and checks_inventory_complete),
            review_decision=str(review["decision"]),
            review_complete=bool(review["complete"] and reviews_inventory_complete),
            complete=bool(
                current_state["complete"]
                and checks_inventory_complete
                and reviews_inventory_complete
            ),
            incomplete_reasons=tuple(incomplete_reasons),
        )

    def _requests(self) -> int:
        value = self._request_count()
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("request_count must return a nonnegative integer.")
        return value


def _validate_repository(raw: object, repository: str) -> str:
    if not isinstance(raw, Mapping):
        raise TypeError("Repository response must be an object.")
    if not _same_repository(raw.get("full_name"), repository):
        raise ValueError("Repository identity does not match the configured repository.")
    default_branch = raw.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise ValueError("Repository default_branch is unavailable.")
    return default_branch


def _validate_branch(raw: object, branch: str) -> None:
    if not isinstance(raw, Mapping):
        raise TypeError("Branch response must be an object.")
    if raw.get("name") != branch:
        raise ValueError("Branch identity does not match the configured branch.")
    commit = raw.get("commit")
    if not isinstance(commit, Mapping):
        raise ValueError("Branch commit identity is unavailable.")
    _sha(commit.get("sha"), "branch.commit.sha")


def _normalize_workflow(
    raw: object,
    *,
    index: int,
) -> tuple[int, str, str, bool]:
    if not isinstance(raw, Mapping):
        raise TypeError(f"workflows[{index}] must be an object.")
    workflow_id = _positive_int(raw.get("id"), f"workflows[{index}].id")
    path = _nonempty_string(raw.get("path"), f"workflows[{index}].path")
    name = _nonempty_string(raw.get("name"), f"workflows[{index}].name")
    state = _nonempty_string(raw.get("state"), f"workflows[{index}].state")
    return workflow_id, path, name, state == "active"


def _run_window(payload: object) -> tuple[Sequence[object], bool]:
    if not isinstance(payload, Mapping):
        raise TypeError("Workflow run response must be an object.")
    raw_runs = payload.get("workflow_runs")
    total_count = payload.get("total_count")
    if not isinstance(raw_runs, list):
        raise TypeError("workflow_runs must be a list.")
    if (
        not isinstance(total_count, int)
        or isinstance(total_count, bool)
        or total_count < len(raw_runs)
    ):
        raise ValueError("total_count must cover workflow_runs.")
    return raw_runs, total_count <= len(raw_runs)


def _normalize_run(
    raw: object,
    *,
    repository: str,
    branch: str,
    workflow_id: int,
    workflow_path: str,
    workflow_name: str,
) -> RunObservation | None:
    if not isinstance(raw, Mapping):
        raise TypeError("Workflow run must be an object.")
    event = _nonempty_string(raw.get("event"), "run.event")
    if event in _PR_EVENTS:
        return None
    if raw.get("head_branch") != branch:
        return None
    if not _same_repository(
        _nested(raw, "repository", "full_name"),
        repository,
    ) or not _same_repository(
        _nested(raw, "head_repository", "full_name"),
        repository,
    ):
        return None
    if raw.get("workflow_id") != workflow_id or raw.get("path") != workflow_path:
        raise ValueError("Workflow run identity does not match its workflow window.")
    run_id = _positive_int(raw.get("id"), "run.id")
    run_number = _positive_int(raw.get("run_number"), "run.run_number")
    attempt = _positive_int(raw.get("run_attempt"), "run.run_attempt")
    head_sha = _sha(raw.get("head_sha"), "run.head_sha")
    status = _nonempty_string(raw.get("status"), "run.status")
    conclusion_value = raw.get("conclusion")
    conclusion = (
        None
        if conclusion_value is None
        else _nonempty_string(conclusion_value, "run.conclusion")
    )
    if status == "completed" and conclusion is None:
        raise ValueError("Completed runs require a conclusion.")
    created_at = _timestamp(raw.get("created_at"), "run.created_at")
    updated_value = raw.get("updated_at")
    updated_at = (
        None
        if updated_value is None
        else _timestamp(updated_value, "run.updated_at")
    )
    url = _nonempty_string(raw.get("html_url"), "run.html_url")
    name_value = raw.get("name")
    name = (
        name_value
        if isinstance(name_value, str) and name_value
        else workflow_name
    )
    return RunObservation(
        key=WorkflowKey(repository, workflow_id, branch),
        workflow_path=workflow_path,
        workflow_name=name,
        run_id=run_id,
        run_number=run_number,
        attempt=attempt,
        head_sha=head_sha,
        event=event,
        status=status,
        conclusion=conclusion,
        created_at=created_at,
        updated_at=updated_at,
        url=url,
        jobs_complete=False,
        jobs=(),
    )


def _normalize_job(raw: object, *, run: RunObservation) -> JobObservation:
    if not isinstance(raw, Mapping):
        raise TypeError("Workflow job must be an object.")
    if raw.get("run_id") != run.run_id:
        raise ValueError("Job run_id does not match the selected run.")
    if raw.get("run_attempt") != run.attempt:
        raise ValueError("Job run_attempt does not match the selected attempt.")
    if raw.get("head_sha") != run.head_sha:
        raise ValueError("Job head_sha does not match the selected run.")
    if raw.get("head_branch") != run.key.branch:
        raise ValueError("Job head_branch does not match the configured branch.")
    labels_value = raw.get("labels")
    if (
        not isinstance(labels_value, list)
        or any(not isinstance(label, str) or not label for label in labels_value)
        or len(set(labels_value)) != len(labels_value)
    ):
        raise ValueError("Job labels must be unique nonempty strings.")
    status = _nonempty_string(raw.get("status"), "job.status")
    conclusion_value = raw.get("conclusion")
    conclusion = (
        None
        if conclusion_value is None
        else _nonempty_string(conclusion_value, "job.conclusion")
    )
    if status == "completed" and conclusion is None:
        raise ValueError("Completed jobs require a conclusion.")
    started_value = raw.get("started_at")
    completed_value = raw.get("completed_at")
    return JobObservation(
        run_id=run.run_id,
        attempt=run.attempt,
        job_id=_positive_int(raw.get("id"), "job.id"),
        key=JobKey(
            _nonempty_string(raw.get("name"), "job.name"),
            tuple(sorted(labels_value)),
        ),
        status=status,
        conclusion=conclusion,
        started_at=(
            None
            if started_value is None
            else _timestamp(started_value, "job.started_at")
        ),
        completed_at=(
            None
            if completed_value is None
            else _timestamp(completed_value, "job.completed_at")
        ),
        url=_nonempty_string(raw.get("html_url"), "job.html_url"),
        log_excerpt=None,
        log_truncated=False,
    )


def _recovery(
    run: RunObservation,
    established_jobs: tuple[JobKey, ...],
) -> tuple[RecoveryStatus, tuple[int, ...], tuple[JobKey, ...]]:
    if not established_jobs:
        return "not_requested", (), ()
    if not run.jobs_complete:
        return "unavailable", (), established_jobs
    established_name_counts = Counter(key.name for key in established_jobs)
    observed_by_name: dict[str, list[JobObservation]] = {}
    for job in run.jobs:
        observed_by_name.setdefault(job.key.name, []).append(job)
    if any(count > 1 for count in established_name_counts.values()):
        return "unavailable", (), established_jobs

    matched: list[JobObservation] = []
    missing: list[JobKey] = []
    for expected in established_jobs:
        candidates = observed_by_name.get(expected.name, [])
        if len(candidates) != 1:
            missing.append(expected)
            continue
        matched.append(candidates[0])
    if missing:
        return "unavailable", tuple(job.job_id for job in matched), tuple(missing)
    if any(job.status != "completed" for job in matched):
        return "pending", tuple(job.job_id for job in matched), ()
    if any(job.conclusion in {None, "skipped", "cancelled", "neutral", "stale"} for job in matched):
        return "unavailable", tuple(job.job_id for job in matched), ()
    if all(job.conclusion == "success" for job in matched):
        return "passed", tuple(job.job_id for job in matched), ()
    return "failed", tuple(job.job_id for job in matched), ()


def _is_later_execution(
    candidate: RunObservation,
    failure: RunObservation,
    failure_attempt: int,
) -> bool:
    if candidate.run_number > failure.run_number:
        return True
    return (
        candidate.run_id == failure.run_id
        and candidate.run_number == failure.run_number
        and candidate.attempt > failure_attempt
    )


def _run_order_key(run: RunObservation) -> tuple[int, int, str, int]:
    return run.run_number, run.attempt, run.created_at, run.run_id


def _same_run_identity(
    current: RunObservation,
    expected: RunObservation,
) -> bool:
    return (
        current.key == expected.key
        and current.workflow_path == expected.workflow_path
        and current.run_id == expected.run_id
        and current.run_number == expected.run_number
        and current.attempt == expected.attempt
        and current.head_sha == expected.head_sha
        and current.event == expected.event
        and current.created_at == expected.created_at
    )


def _task_artifact_bindings(
    raw_task: AgentTask,
) -> tuple[tuple[int, ...], tuple[TaskBranchArtifact, ...]]:
    pull_database_ids = {
        artifact.database_id
        for artifact in raw_task.pull_artifacts
        if artifact.database_id is not None
    }
    branch_artifacts = {
        TaskBranchArtifact(
            head_ref=artifact.head_ref,
            base_ref=artifact.base_ref,
        )
        for artifact in raw_task.branch_artifacts
        if artifact.head_ref is not None and artifact.base_ref is not None
    }
    return (
        tuple(sorted(pull_database_ids)),
        tuple(
            sorted(
                branch_artifacts,
                key=lambda artifact: (artifact.head_ref, artifact.base_ref),
            )
        ),
    )


def _validate_pull_request(
    raw: object,
    *,
    repository: str,
    number: int,
    base_ref: str,
    expected_branches: tuple[TaskBranchArtifact, ...],
    expected_database_ids: tuple[int, ...],
) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        raise TypeError("Pull request response must be an object.")
    if raw.get("number") != number:
        raise ValueError("The exact pull request endpoint returned a different PR.")
    database_id = _positive_int(raw.get("id"), "pull.id")
    if (
        expected_database_ids
        and database_id not in expected_database_ids
    ):
        raise ValueError(
            "Pull request database ID does not match the owned task artifact."
        )
    head = raw.get("head")
    base = raw.get("base")
    if not isinstance(head, Mapping) or not isinstance(base, Mapping):
        raise ValueError("Pull request head/base identity is unavailable.")
    head_repository = _nested(head, "repo", "full_name")
    base_repository = _nested(base, "repo", "full_name")
    head_ref = _nonempty_string(head.get("ref"), "pull.head.ref")
    actual_base_ref = _nonempty_string(base.get("ref"), "pull.base.ref")
    if not _same_repository(head_repository, repository):
        raise ValueError("Pull request head repository is foreign.")
    if not _same_repository(base_repository, repository):
        raise ValueError("Pull request base repository is foreign.")
    if actual_base_ref != base_ref:
        raise ValueError("Pull request base branch does not match the workflow branch.")
    if expected_branches and not any(
        artifact.head_ref == head_ref and artifact.base_ref == actual_base_ref
        for artifact in expected_branches
    ):
        raise ValueError(
            "Pull request branches do not match the owned task artifact."
        )
    return {
        "database_id": database_id,
        "state": _nonempty_string(raw.get("state"), "pull.state"),
        "merged": raw.get("merged") is True or raw.get("merged_at") is not None,
        "draft": raw.get("draft") is True,
        "url": _nonempty_string(raw.get("html_url"), "pull.html_url"),
        "head_repository": str(head_repository),
        "head_ref": head_ref,
        "head_sha": _sha(head.get("sha"), "pull.head.sha"),
        "base_repository": str(base_repository),
        "base_ref": actual_base_ref,
    }


def _canonical_marker(item: WorkflowItem) -> str:
    return (
        "<!-- ci-shepherd:workflow-repair "
        f"repository={item.repository} workflow-id={item.workflow_id} "
        f"branch={item.branch} -->"
    )


def _normalize_repair_file(raw: object) -> RepairFileObservation:
    if not isinstance(raw, Mapping):
        raise TypeError("Pull request file entry must be an object.")
    return RepairFileObservation(
        path=_nonempty_string(raw.get("filename"), "pull file.filename"),
        status=_nonempty_string(raw.get("status"), "pull file.status"),
        additions=_nonnegative_int(raw.get("additions"), "pull file.additions"),
        deletions=_nonnegative_int(raw.get("deletions"), "pull file.deletions"),
        changes=_nonnegative_int(raw.get("changes"), "pull file.changes"),
        url=_nonempty_string(raw.get("blob_url"), "pull file.blob_url"),
    )


def _normalize_bot_comment(
    raw: object,
) -> RepairCommentObservation | None:
    if not isinstance(raw, Mapping):
        raise TypeError("Pull request comment entry must be an object.")
    user = raw.get("user")
    if not isinstance(user, Mapping):
        raise ValueError("Pull request comment user is unavailable.")
    author = _nonempty_string(user.get("login"), "pull comment.user.login")
    if user.get("type") != "Bot" and not author.casefold().endswith("[bot]"):
        return None
    body, truncated = _bounded_text(
        _nonempty_string(raw.get("body"), "pull comment.body"),
        8_192,
    )
    return RepairCommentObservation(
        comment_id=_positive_int(raw.get("id"), "pull comment.id"),
        author=author,
        body=body,
        url=_nonempty_string(raw.get("html_url"), "pull comment.html_url"),
        created_at=_timestamp(
            raw.get("created_at"),
            "pull comment.created_at",
        ),
        updated_at=_timestamp(
            raw.get("updated_at"),
            "pull comment.updated_at",
        ),
        truncated=truncated,
    )


def _normalize_failed_check(
    raw: object,
    *,
    head_sha: str,
) -> RepairCheckObservation | None:
    if not isinstance(raw, Mapping):
        raise TypeError("Check run entry must be an object.")
    if raw.get("head_sha") != head_sha:
        raise ValueError("Check run head SHA does not match the current pull request.")
    conclusion = raw.get("conclusion")
    if conclusion not in {
        "failure",
        "timed_out",
        "action_required",
        "startup_failure",
    }:
        return None
    output = raw.get("output")
    if output is not None and not isinstance(output, Mapping):
        raise ValueError("Check run output must be an object or null.")
    details_value = raw.get("details_url")
    details_url = (
        details_value
        if isinstance(details_value, str) and details_value
        else None
    )
    return RepairCheckObservation(
        check_run_id=_positive_int(raw.get("id"), "check_run.id"),
        name=_nonempty_string(raw.get("name"), "check_run.name"),
        status=_nonempty_string(raw.get("status"), "check_run.status"),
        conclusion=conclusion,
        url=_nonempty_string(raw.get("html_url"), "check_run.html_url"),
        details_url=details_url,
        output_title=_optional_bounded_text(
            output.get("title") if output is not None else None,
            2_000,
        ),
        output_summary=_optional_bounded_text(
            output.get("summary") if output is not None else None,
            8_192,
        ),
        output_text=_optional_bounded_text(
            output.get("text") if output is not None else None,
            8_192,
        ),
        log_excerpt=None,
        log_truncated=False,
        log_available=False,
    )


def _actions_job_id(
    details_url: str | None,
    *,
    repository: str,
) -> int | None:
    if details_url is None:
        return None
    split = urlsplit(details_url)
    expected_prefix = f"/{repository}/actions/runs/"
    if (
        split.scheme != "https"
        or split.netloc != "github.com"
        or not split.path.casefold().startswith(expected_prefix.casefold())
    ):
        return None
    match = re.fullmatch(
        rf"/{re.escape(repository)}/actions/runs/\d+/job/(\d+)",
        split.path,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    return int(match.group(1))


def _bounded_text(value: str, maximum: int) -> tuple[str, bool]:
    if len(value) <= maximum:
        return value, False
    return value[:maximum], True


def _optional_bounded_text(
    value: object,
    maximum: int,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Optional evidence text must be a string or null.")
    return _bounded_text(value, maximum)[0]


def _search_issue_numbers(payload: object) -> tuple[int, ...]:
    if not isinstance(payload, Mapping):
        raise TypeError("Issue search response must be an object.")
    total_count = payload.get("total_count")
    items = payload.get("items")
    if (
        not isinstance(total_count, int)
        or isinstance(total_count, bool)
        or not isinstance(items, list)
        or total_count < len(items)
        or total_count > 10
    ):
        raise ValueError("Issue search response is incomplete or malformed.")
    numbers: list[int] = []
    for index, raw_issue in enumerate(items):
        if not isinstance(raw_issue, Mapping):
            raise TypeError(f"items[{index}] must be an object.")
        number = _positive_int(raw_issue.get("number"), f"items[{index}].number")
        numbers.append(number)
    return tuple(sorted(set(numbers)))


def _normalize_issue(
    raw: object,
    *,
    repository: str,
    marker: str,
) -> IssueObservation | None:
    if not isinstance(raw, Mapping):
        raise TypeError("Issue response must be an object.")
    if "pull_request" in raw or raw.get("state") != "open":
        return None
    if raw.get("repository_url") != f"https://api.github.com/repos/{repository}":
        raise ValueError("Issue repository does not match the tracked repository.")
    body = raw.get("body")
    if not isinstance(body, str) or marker not in body:
        return None
    raw_assignees = raw.get("assignees", [])
    if not isinstance(raw_assignees, list):
        raise ValueError("Issue assignees must be a list.")
    assignees = tuple(
        sorted(
            {
                login
                for raw_assignee in raw_assignees
                if isinstance(raw_assignee, Mapping)
                if isinstance((login := raw_assignee.get("login")), str)
                and login
            }
        )
    )
    copilot = {
        "copilot",
        "copilot-swe-agent",
        "copilot-swe-agent[bot]",
        "github-copilot[bot]",
    }
    return IssueObservation(
        number=_positive_int(raw.get("number"), "issue.number"),
        url=_nonempty_string(raw.get("html_url"), "issue.html_url"),
        title=_nonempty_string(raw.get("title"), "issue.title"),
        marker=marker,
        assignees=assignees,
        copilot_assigned=any(login.casefold() in copilot for login in assignees),
        human_assigned=any(
            login.casefold() not in copilot and not login.casefold().endswith("[bot]")
            for login in assignees
        ),
    )


def _nested(mapping: Mapping[str, object], first: str, second: str) -> object:
    value = mapping.get(first)
    return value.get(second) if isinstance(value, Mapping) else None


def _same_repository(value: object, repository: str) -> bool:
    return (
        isinstance(value, str)
        and value.casefold() == repository.casefold()
    )


def _positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer.")
    return value


def _nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty string.")
    return value


def _sha(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a full lowercase commit SHA.")
    return value


def _timestamp(value: object, name: str) -> str:
    if not isinstance(value, str) or _TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be an RFC3339 UTC timestamp.")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{name} must be an RFC3339 UTC timestamp.") from error
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError(f"{name} must be an RFC3339 UTC timestamp.")
    return value


def _format_time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return an aware datetime.")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _error(
    scope: str,
    code: str,
    endpoint: str,
    error: Exception,
) -> ReadError:
    if isinstance(error, GitHubApiError):
        detail = (
            f"{error.category} status={error.status} "
            f"attempts={error.attempts} retryable={str(error.retryable).lower()}"
        )
    else:
        detail = str(error)
    return ReadError(scope=scope, code=code, endpoint=endpoint, detail=detail)
