from __future__ import annotations

from dataclasses import fields, replace

from .models import (
    ActionKind,
    ActionState,
    ItemPhase,
    JobObservation,
    JudgmentDecision,
    JudgmentRequest,
    JudgmentResult,
    RunObservation,
    TaskState,
    WorkflowItem,
    WorkState,
)
from .reader import ItemRefresh, PullRequestObservation
from .scenario import ConfirmedIssueCreation, ItemTransition, NextStep


_PR_EVENTS = frozenset({"pull_request", "pull_request_target", "merge_group"})
_FAILED_CONCLUSIONS = frozenset({"failure", "timed_out"})
_TERMINAL_TASK_STATES = frozenset(
    {
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.IDLE,
        TaskState.TIMED_OUT,
        TaskState.CANCELLED,
    }
)


def reduce_item(
    item: WorkflowItem,
    refresh: ItemRefresh,
    *,
    now: str,
    request: JudgmentRequest | None = None,
    judgment: JudgmentResult | None = None,
    confirmed_issue: ConfirmedIssueCreation | None = None,
    worker_state: WorkState | None = None,
    action_state: ActionState | None = None,
    capacity_available: bool = True,
) -> ItemTransition:
    """Reduce one workflow item without performing I/O or reserving capacity."""
    if not isinstance(item, WorkflowItem):
        raise ValueError("item must be a WorkflowItem.")
    if not isinstance(refresh, ItemRefresh):
        raise ValueError("refresh must be an ItemRefresh.")
    if refresh.item_id != item.id:
        raise ValueError("refresh item_id does not match the item.")
    if not isinstance(now, str):
        raise ValueError("now must be an RFC3339 timestamp string.")
    if request is not None and not isinstance(request, JudgmentRequest):
        raise ValueError("request must be a JudgmentRequest or null.")
    if judgment is not None and not isinstance(judgment, JudgmentResult):
        raise ValueError("judgment must be a JudgmentResult or null.")
    if confirmed_issue is not None and not isinstance(
        confirmed_issue,
        ConfirmedIssueCreation,
    ):
        raise ValueError(
            "confirmed_issue must be a ConfirmedIssueCreation or null."
        )
    if worker_state is not None and not isinstance(worker_state, WorkState):
        raise ValueError("worker_state must be a WorkState or null.")
    if action_state is not None and not isinstance(action_state, ActionState):
        raise ValueError("action_state must be an ActionState or null.")
    if not isinstance(capacity_available, bool):
        raise ValueError("capacity_available must be a boolean.")

    observed = _apply_authoritative_task_and_pull(
        replace(
            item,
            last_checked_at=now,
            read_status="complete" if refresh.complete else "unavailable",
        ),
        refresh,
    )
    if observed.wait_run_id is not None or observed.phase is ItemPhase.WAITING_FOR_RUN:
        # Retire persisted waits from the previous policy without a state migration.
        observed = replace(
            observed,
            wait_run_id=None,
            phase=(
                ItemPhase.OBSERVING_FAILURE
                if observed.phase is ItemPhase.WAITING_FOR_RUN
                else observed.phase
            ),
            wait_reason=None,
        )

    recovery_run = _proven_recovery(item, refresh)
    if recovery_run is not None:
        unchanged_recovery = (
            item.phase is ItemPhase.RECOVERED
            and item.recovered_run_id == recovery_run.run_id
            and item.recovered_at is not None
        )
        recovered = replace(
            observed,
            phase=ItemPhase.RECOVERED,
            recovered_run_id=recovery_run.run_id,
            recovered_at=item.recovered_at if unchanged_recovery else now,
            wait_reason="recovered",
            latest_error=None,
        )
        if action_state is ActionState.UNCERTAIN:
            return _finish(
                item,
                replace(
                    recovered,
                    latest_error="The remote write outcome is uncertain.",
                ),
                now,
                NextStep.NEEDS_ATTENTION,
                "recovered-action-uncertain",
                "Recovery was confirmed, but the prior write outcome remains uncertain.",
            )
        if worker_state in {WorkState.FAILED, WorkState.INVALID}:
            return _finish(
                item,
                replace(
                    recovered,
                    latest_error=f"Worker ended in {worker_state.value}.",
                ),
                now,
                NextStep.NEEDS_ATTENTION,
                "recovered-worker-failed",
                "Recovery was confirmed, but the prior worker failure still needs attention.",
            )
        live_work = (
            worker_state in {WorkState.QUEUED, WorkState.RUNNING}
            or action_state in {ActionState.PREPARED, ActionState.INVOKING}
            or _task_is_active_or_unknown(recovered)
        )
        return _finish(
            item,
            recovered,
            now,
            (
                NextStep.WAIT_FOR_OWNED_WORK
                if live_work
                else NextStep.WAIT_FOR_CHANGE
            ),
            "recovered",
            "All established in-scope jobs passed on a newer main-workflow run.",
        )

    if worker_state in {WorkState.FAILED, WorkState.INVALID}:
        return _finish(
            item,
            replace(
                observed,
                phase=ItemPhase.NEEDS_ATTENTION,
                latest_error=f"Worker ended in {worker_state.value}.",
            ),
            now,
            NextStep.NEEDS_ATTENTION,
            "worker-failed",
            "The judgment worker failed and requires human attention.",
        )
    if action_state is ActionState.UNCERTAIN:
        return _finish(
            item,
            replace(
                observed,
                phase=ItemPhase.NEEDS_ATTENTION,
                latest_error="The remote write outcome is uncertain.",
            ),
            now,
            NextStep.NEEDS_ATTENTION,
            "action-uncertain",
            "The action outcome is uncertain and will not be retried.",
        )

    if worker_state in {WorkState.QUEUED, WorkState.RUNNING}:
        phase = (
            ItemPhase.JUDGMENT_QUEUED
            if worker_state is WorkState.QUEUED
            else ItemPhase.JUDGMENT_RUNNING
        )
        return _finish(
            item,
            replace(observed, phase=phase),
            now,
            NextStep.WAIT_FOR_OWNED_WORK,
            "worker-active",
            "The owned judgment worker is still active.",
        )
    if action_state in {ActionState.PREPARED, ActionState.INVOKING}:
        return _finish(
            item,
            observed,
            now,
            NextStep.WAIT_FOR_OWNED_WORK,
            "action-active",
            "The owned action attempt is still active.",
        )

    external = _external_owner(refresh)
    if external is not None and item.task_id is None:
        return _finish(
            item,
            replace(
                observed,
                phase=ItemPhase.OBSERVING_EXTERNAL_REPAIR,
                external_owner=external,
            ),
            now,
            NextStep.OBSERVE_EXTERNAL,
            "external-repair-observed",
            "An externally owned repair is already in progress.",
        )

    issue_result = _apply_confirmed_issue(
        observed,
        request,
        judgment,
        confirmed_issue,
    )
    if isinstance(issue_result, str):
        return _attention(item, observed, now, issue_result)
    observed = issue_result

    blocking_task = _reduce_blocking_owned_task(item, observed, refresh, now)
    if blocking_task is not None:
        if request is not None and judgment is not None:
            return replace(blocking_task, retain_judgment=True)
        return blocking_task

    if (
        request is not None
        and judgment is not None
        and _judgment_was_applied(observed, request)
    ):
        request = None
        judgment = None

    if request is not None or judgment is not None:
        if request is None or judgment is None:
            return _finish(
                item,
                observed,
                now,
                NextStep.WAIT_FOR_OWNED_WORK,
                "judgment-incomplete",
                "The judgment request/result pair is not complete yet.",
            )
        requires_pre_write = judgment.decision in {
            JudgmentDecision.ASSIGN,
            JudgmentDecision.FOLLOW_UP,
        }
        if not refresh.complete or (requires_pre_write and not refresh.pre_write):
            return _finish(
                item,
                observed,
                now,
                NextStep.WAIT_FOR_READ,
                "judgment-freshness-unavailable",
                "The validated judgment is retained until a complete pre-write read succeeds.",
                retain_judgment=True,
            )
        stale_reason = _judgment_stale_reason(
            observed,
            refresh,
            request,
            judgment,
            confirmed_issue,
        )
        if stale_reason is not None:
            return _attention(item, observed, now, stale_reason)
        return _apply_judgment(
            item,
            observed,
            request,
            judgment,
            now,
            capacity_available,
        )

    task_transition = _reduce_owned_task(item, observed, refresh, now)
    if task_transition is not None:
        return task_transition

    if not refresh.complete:
        return _finish(
            item,
            observed,
            now,
            NextStep.WAIT_FOR_READ,
            "read-unavailable",
            "Required fresh observations are unavailable; known facts were retained.",
        )

    if item.phase in {
        ItemPhase.JUDGMENT_QUEUED,
        ItemPhase.JUDGMENT_RUNNING,
        ItemPhase.READY_FOR_ACTION,
    }:
        return _finish(
            item,
            observed,
            now,
            NextStep.WAIT_FOR_OWNED_WORK,
            "owned-work-pending",
            "Previously prepared owned work is still awaiting an authoritative result.",
        )

    if item.last_judged_fingerprint == item.evidence_fingerprint:
        return _finish(
            item,
            observed,
            now,
            NextStep.WAIT_FOR_CHANGE,
            "evidence-unchanged",
            "Semantic failure evidence is unchanged; no new judgment was queued.",
        )

    queued = replace(
        observed,
        phase=ItemPhase.JUDGMENT_QUEUED,
        wait_reason=None,
    )
    if not capacity_available:
        return _finish(
            item,
            replace(queued, phase=ItemPhase.OBSERVING_FAILURE),
            now,
            NextStep.WAIT_FOR_CAPACITY,
            "judgment-capacity-wait",
            "Initial judgment is eligible but owned-work capacity is unavailable.",
            judgment_round=0,
        )
    return _finish(
        item,
        queued,
        now,
        NextStep.QUEUE_JUDGMENT,
        "judgment-queued",
        "Queued initial round-zero judgment for the current semantic evidence.",
        judgment_round=0,
    )


def _reduce_owned_task(
    original: WorkflowItem,
    observed: WorkflowItem,
    refresh: ItemRefresh,
    now: str,
) -> ItemTransition | None:
    if original.task_id is None:
        return None
    if refresh.task is None:
        return _finish(
            original,
            observed,
            now,
            NextStep.WAIT_FOR_OWNED_WORK,
            "task-state-unknown",
            "The owned task state is not yet authoritatively known.",
        )
    if refresh.task.task_id != original.task_id:
        return _attention(
            original,
            observed,
            now,
            "The task observation does not match the owned task identity.",
        )
    try:
        task_state = TaskState(refresh.task.state)
    except ValueError:
        return _finish(
            original,
            observed,
            now,
            NextStep.WAIT_FOR_OWNED_WORK,
            "task-state-unknown",
            "The owned task reported an unrecognized state.",
        )
    pull_number = observed.pull_request_number
    if refresh.pull_request is not None:
        pull_number = refresh.pull_request.number
    updated = replace(
        observed,
        task_state=task_state,
        pull_request_number=pull_number,
    )
    if task_state in {TaskState.QUEUED, TaskState.IN_PROGRESS}:
        return _finish(
            original,
            replace(updated, phase=ItemPhase.COPILOT_ACTIVE),
            now,
            NextStep.WAIT_FOR_OWNED_WORK,
            "task-active",
            "The owned Agent Task is queued or running.",
        )
    if task_state is TaskState.WAITING_FOR_USER:
        return _finish(
            original,
            replace(updated, phase=ItemPhase.WAITING_FOR_HUMAN),
            now,
            NextStep.WAIT_FOR_HUMAN,
            "task-human-handoff",
            "The owned Agent Task is waiting for human input.",
        )
    if task_state not in _TERMINAL_TASK_STATES:
        return _finish(
            original,
            updated,
            now,
            NextStep.WAIT_FOR_OWNED_WORK,
            "task-state-unknown",
            "The owned task has not reached a recognized terminal state.",
        )
    if refresh.pull_request is None:
        return _attention(
            original,
            updated,
            now,
            "The owned task finished without authoritative pull request evidence.",
        )
    return _reduce_pull_request(original, updated, refresh.pull_request, now)


def _reduce_blocking_owned_task(
    original: WorkflowItem,
    observed: WorkflowItem,
    refresh: ItemRefresh,
    now: str,
) -> ItemTransition | None:
    if original.task_id is None:
        return None
    if refresh.task is None:
        return _finish(
            original,
            observed,
            now,
            NextStep.WAIT_FOR_OWNED_WORK,
            "task-state-unknown",
            "The owned task state is not yet authoritatively known.",
        )
    if refresh.task.task_id != original.task_id:
        return _attention(
            original,
            observed,
            now,
            "The task observation does not match the owned task identity.",
        )
    try:
        task_state = TaskState(refresh.task.state)
    except ValueError:
        return _finish(
            original,
            observed,
            now,
            NextStep.WAIT_FOR_OWNED_WORK,
            "task-state-unknown",
            "The owned task reported an unrecognized state.",
        )
    updated = replace(observed, task_state=task_state)
    if task_state in {TaskState.QUEUED, TaskState.IN_PROGRESS}:
        return _finish(
            original,
            replace(updated, phase=ItemPhase.COPILOT_ACTIVE),
            now,
            NextStep.WAIT_FOR_OWNED_WORK,
            "task-active",
            "The owned Agent Task is queued or running.",
        )
    if task_state is TaskState.WAITING_FOR_USER:
        return _finish(
            original,
            replace(updated, phase=ItemPhase.WAITING_FOR_HUMAN),
            now,
            NextStep.WAIT_FOR_HUMAN,
            "task-human-handoff",
            "The owned Agent Task is waiting for human input.",
        )
    return None


def _reduce_pull_request(
    original: WorkflowItem,
    item: WorkflowItem,
    pull: PullRequestObservation,
    now: str,
) -> ItemTransition:
    if item.pull_request_number is not None and (
        pull.number != item.pull_request_number
    ):
        return _attention(
            original,
            item,
            now,
            "The pull request observation does not match the owned PR.",
        )
    if pull.state == "closed" and not pull.merged:
        return _attention(
            original,
            replace(item, phase=ItemPhase.NEEDS_ATTENTION),
            now,
            "The owned pull request was closed without merge.",
        )
    if pull.draft or pull.checks_state == "action_required":
        return _finish(
            original,
            replace(item, phase=ItemPhase.WAITING_FOR_HUMAN),
            now,
            NextStep.WAIT_FOR_HUMAN,
            "pull-request-human-handoff",
            "The owned pull request requires human approval or action.",
        )
    if (
        not pull.complete
        or not pull.checks_complete
        or pull.checks_state in {"pending", "queued", "in_progress"}
    ):
        return _finish(
            original,
            replace(item, phase=ItemPhase.WAITING_FOR_CI),
            now,
            NextStep.WAIT_FOR_PR,
            "pull-request-pending",
            "The owned pull request checks are incomplete.",
        )
    if pull.merged or pull.checks_state == "green":
        return _finish(
            original,
            replace(item, phase=ItemPhase.WAITING_FOR_CI),
            now,
            NextStep.WAIT_FOR_CI,
            "main-recovery-pending",
            "The pull request is green or merged; main-workflow recovery is still required.",
        )
    if pull.checks_state == "red":
        if item.followup_count >= 2:
            return _finish(
                original,
                replace(item, phase=ItemPhase.WAITING_FOR_HUMAN),
                now,
                NextStep.WAIT_FOR_HUMAN,
                "follow-up-budget-exhausted",
                "The two-follow-up budget is exhausted; human attention is required.",
            )
        return _finish(
            original,
            replace(item, phase=ItemPhase.JUDGMENT_QUEUED),
            now,
            NextStep.QUEUE_JUDGMENT,
            "follow-up-judgment-queued",
            "Failed owned-PR checks require a fresh bounded follow-up judgment.",
            judgment_round=item.followup_count + 1,
        )
    return _attention(
        original,
        item,
        now,
        "The owned pull request reached an unsupported terminal check state.",
    )


def _apply_judgment(
    original: WorkflowItem,
    item: WorkflowItem,
    request: JudgmentRequest,
    judgment: JudgmentResult,
    now: str,
    capacity_available: bool,
) -> ItemTransition:
    judged = replace(
        item,
        last_judged_fingerprint=judgment.evidence_fingerprint,
    )
    if judgment.decision is JudgmentDecision.ASSIGN:
        jobs_by_id = {job.job_id: job.key for job in request.failed_jobs}
        scoped_jobs = tuple(jobs_by_id[job_id] for job_id in judgment.in_scope_job_ids)
        updated = replace(
            item,
            phase=ItemPhase.READY_FOR_ACTION,
            failed_jobs=scoped_jobs,
            last_judged_fingerprint=judgment.evidence_fingerprint,
            wait_reason=None,
        )
        action = (
            ActionKind.CREATE_ISSUE
            if updated.issue_number is None
            else ActionKind.ASSIGN_COPILOT
        )
        return _action_transition(
            original,
            updated,
            now,
            action,
            request.round,
            capacity_available,
            "initial-judgment-ready",
            "Initial judgment selected the exact in-scope repair jobs.",
        )
    if judgment.decision is JudgmentDecision.FOLLOW_UP:
        updated = replace(
            item,
            phase=ItemPhase.READY_FOR_ACTION,
            last_judged_fingerprint=judgment.evidence_fingerprint,
        )
        return _action_transition(
            original,
            updated,
            now,
            ActionKind.FOLLOW_UP,
            request.round,
            capacity_available,
            "follow-up-judgment-ready",
            "Fresh failed PR evidence is ready for a bounded follow-up task.",
        )
    if judgment.decision is JudgmentDecision.DEFER_ORDINARY_TEST:
        return _finish(
            original,
            replace(judged, phase=ItemPhase.WAITING_FOR_HUMAN),
            now,
            NextStep.WAIT_FOR_HUMAN,
            "ordinary-test-deferred",
            "Ordinary test failures were deferred visibly without mutation.",
            judgment_round=request.round,
        )
    if judgment.decision is JudgmentDecision.OBSERVE_EXTERNAL:
        return _finish(
            original,
            replace(
                judged,
                phase=ItemPhase.OBSERVING_EXTERNAL_REPAIR,
                external_owner=item.external_owner or "external",
            ),
            now,
            NextStep.OBSERVE_EXTERNAL,
            "external-repair-selected",
            "Judgment identified an externally owned repair to observe.",
            judgment_round=request.round,
        )
    if judgment.decision is JudgmentDecision.NEEDS_ATTENTION:
        return _attention(
            original,
            judged,
            now,
            judgment.summary,
            judgment_round=request.round,
        )
    return _finish(
        original,
        judged,
        now,
        NextStep.WAIT_FOR_CHANGE,
        "judgment-no-action",
        judgment.summary,
        judgment_round=request.round,
    )


def _apply_authoritative_task_and_pull(
    item: WorkflowItem,
    refresh: ItemRefresh,
) -> WorkflowItem:
    updated = item
    if (
        item.task_id is not None
        and refresh.task is not None
        and refresh.task.task_id == item.task_id
    ):
        try:
            task_state = TaskState(refresh.task.state)
        except ValueError:
            pass
        else:
            updated = replace(updated, task_state=task_state)
    if refresh.pull_request is not None:
        updated = replace(
            updated,
            pull_request_number=refresh.pull_request.number,
        )
    return updated


def _judgment_was_applied(
    item: WorkflowItem,
    request: JudgmentRequest,
) -> bool:
    if request.round == 0:
        return (
            item.task_id is not None
            and item.assignment_confirmed_at is not None
        )
    return item.followup_count >= request.round


def _action_transition(
    original: WorkflowItem,
    item: WorkflowItem,
    now: str,
    action: ActionKind,
    judgment_round: int,
    capacity_available: bool,
    event: str,
    summary: str,
) -> ItemTransition:
    if not capacity_available:
        return _finish(
            original,
            item,
            now,
            NextStep.WAIT_FOR_CAPACITY,
            "action-capacity-wait",
            "The fresh judgment is retained while owned-work capacity is unavailable.",
            action_kind=action,
            judgment_round=judgment_round,
            retain_judgment=True,
        )
    return _finish(
        original,
        item,
        now,
        NextStep.PREPARE_ACTION,
        event,
        summary,
        action_kind=action,
        judgment_round=judgment_round,
        retain_judgment=True,
    )


def _apply_confirmed_issue(
    item: WorkflowItem,
    request: JudgmentRequest | None,
    judgment: JudgmentResult | None,
    confirmed: ConfirmedIssueCreation | None,
) -> WorkflowItem | str:
    if confirmed is None:
        return item
    if request is None or judgment is None:
        return "Confirmed issue creation has no matching judgment request."
    if (
        confirmed.worker_id != request.worker_id
        or confirmed.item_id != request.item_id
        or confirmed.episode != request.episode
        or confirmed.evidence_fingerprint != request.evidence_fingerprint
        or request.issue_number is not None
        or confirmed.item_id != item.id
        or confirmed.episode != item.episode
        or confirmed.evidence_fingerprint != item.evidence_fingerprint
    ):
        return "Confirmed issue creation does not belong to this exact request."
    if item.issue_number not in {None, confirmed.issue_number}:
        return "Confirmed issue creation conflicts with the current issue target."
    return replace(item, issue_number=confirmed.issue_number)


def _judgment_stale_reason(
    item: WorkflowItem,
    refresh: ItemRefresh,
    request: JudgmentRequest,
    judgment: JudgmentResult,
    confirmed_issue: ConfirmedIssueCreation | None,
) -> str | None:
    if (
        request.item_id != item.id
        or request.episode != item.episode
        or request.evidence_fingerprint != item.evidence_fingerprint
        or judgment.item_id != item.id
        or judgment.episode != item.episode
        or judgment.evidence_fingerprint != item.evidence_fingerprint
    ):
        return "Judgment identity is stale for the current item episode or evidence."
    if request.followup_count != item.followup_count:
        return "Judgment follow-up count is stale."
    if request.round == 0:
        failure = refresh.failure_run
        write_grade = judgment.decision in {
            JudgmentDecision.ASSIGN,
            JudgmentDecision.FOLLOW_UP,
        }
        if (
            request.repository != item.repository
            or request.branch != item.branch
            or request.workflow_id != item.workflow_id
            or request.workflow_path != item.workflow_path
            or failure is None
            or failure.run_id != item.failure_run_id
            or failure.attempt != item.failure_attempt
            or not _same_run_metadata(request.failure_run, failure)
            or (
                write_grade
                and (
                    not _same_run_identity(request.failure_run, failure)
                    or not _same_job_inventory(request.failure_run, failure)
                    or not _request_jobs_are_fresh(request, failure)
                )
            )
        ):
            return "Initial judgment request does not match the fresh failure identity."
        if any(
            value is not None
            for value in (
                request.task_id,
                request.pull_request_number,
                request.pull_request_head_sha,
                request.pull_request_head_ref,
                request.pull_request_base_ref,
                request.pull_request_observed_at,
            )
        ):
            return "Initial judgment unexpectedly carries task or PR identity."
        issue_matches = request.issue_number == item.issue_number
        created_matches = (
            confirmed_issue is not None
            and request.issue_number is None
            and item.issue_number == confirmed_issue.issue_number
        )
        if not issue_matches and not created_matches:
            return "Initial judgment issue target changed without its confirmed creation."
        if (
            request.issue_number is not None
            and (
                refresh.issue is None
                or refresh.issue.number != request.issue_number
            )
        ):
            return "Initial judgment issue observation is missing or stale."
        return None
    if request.round not in {1, 2}:
        return "Follow-up judgment round is outside the bounded range."
    if request.round != item.followup_count + 1:
        return "Follow-up judgment round is stale for the current budget."
    if (
        refresh.issue is None
        or refresh.task is None
        or refresh.pull_request is None
    ):
        return "Follow-up target observations are incomplete."
    pull = refresh.pull_request
    if (
        request.issue_number != item.issue_number
        or request.issue_number != refresh.issue.number
        or request.task_id != item.task_id
        or request.task_id != refresh.task.task_id
        or request.pull_request_number != item.pull_request_number
        or request.pull_request_number != pull.number
        or request.pull_request_head_sha != pull.head_sha
        or request.pull_request_head_ref != pull.head_ref
        or request.pull_request_base_ref != pull.base_ref
        or request.pull_request_observed_at != refresh.observed_at
    ):
        return "Follow-up judgment target identity is stale."
    return None


def _same_run_identity(
    request_run: RunObservation,
    fresh_run: RunObservation,
) -> bool:
    return (
        request_run.key == fresh_run.key
        and request_run.workflow_path == fresh_run.workflow_path
        and request_run.run_id == fresh_run.run_id
        and request_run.run_number == fresh_run.run_number
        and request_run.attempt == fresh_run.attempt
        and request_run.head_sha == fresh_run.head_sha
        and request_run.event == fresh_run.event
        and request_run.status == fresh_run.status
        and request_run.conclusion == fresh_run.conclusion
        and request_run.jobs_complete == fresh_run.jobs_complete
    )


def _same_run_metadata(
    request_run: RunObservation,
    fresh_run: RunObservation,
) -> bool:
    return (
        request_run.key == fresh_run.key
        and request_run.workflow_path == fresh_run.workflow_path
        and request_run.run_id == fresh_run.run_id
        and request_run.run_number == fresh_run.run_number
        and request_run.attempt == fresh_run.attempt
        and request_run.head_sha == fresh_run.head_sha
        and request_run.event == fresh_run.event
        and request_run.status == fresh_run.status
        and request_run.conclusion == fresh_run.conclusion
    )


def _same_job_inventory(
    request_run: RunObservation,
    fresh_run: RunObservation,
) -> bool:
    return {
        _job_identity(job)
        for job in request_run.jobs
    } == {
        _job_identity(job)
        for job in fresh_run.jobs
    }


def _request_jobs_are_fresh(
    request: JudgmentRequest,
    fresh_run: RunObservation,
) -> bool:
    fresh = {_job_identity(job) for job in fresh_run.jobs}
    return all(
        _job_identity(job) in fresh
        and job.conclusion in _FAILED_CONCLUSIONS
        for job in request.failed_jobs
    )


def _job_identity(job: JobObservation) -> tuple[object, ...]:
    return (
        job.run_id,
        job.attempt,
        job.job_id,
        job.key,
        job.status,
        job.conclusion,
    )


def _proven_recovery(
    item: WorkflowItem,
    refresh: ItemRefresh,
) -> RunObservation | None:
    candidate = refresh.recovery_run
    failure = refresh.failure_run
    if (
        item.last_judged_fingerprint is None
        or refresh.recovery != "passed"
        or candidate is None
        or failure is None
        or candidate.event in _PR_EVENTS
        or candidate.status != "completed"
        or not candidate.jobs_complete
        or not item.failed_jobs
        or not _is_newer_execution(candidate, failure)
    ):
        return None
    for target in item.failed_jobs:
        matches = [job for job in candidate.jobs if job.key == target]
        if not matches:
            matches = [job for job in candidate.jobs if job.key.name == target.name]
        if len(matches) != 1:
            return None
        job = matches[0]
        if job.status != "completed" or job.conclusion != "success":
            return None
    candidate_key = _execution_key(candidate)
    if any(
        run.event not in _PR_EVENTS
        and run.status == "completed"
        and run.conclusion in _FAILED_CONCLUSIONS
        and _execution_key(run) > candidate_key
        for run in refresh.runs
    ):
        return None
    return candidate


def _task_is_active_or_unknown(item: WorkflowItem) -> bool:
    return item.task_id is not None and (
        item.task_state is None
        or item.task_state in {TaskState.QUEUED, TaskState.IN_PROGRESS}
    )


def _external_owner(refresh: ItemRefresh) -> str | None:
    if refresh.issue is None:
        return None
    if refresh.issue.copilot_assigned:
        return "copilot"
    if refresh.issue.human_assigned:
        return "human"
    return None


def _is_newer_execution(
    candidate: RunObservation,
    failure: RunObservation,
) -> bool:
    if candidate.run_id == failure.run_id:
        return candidate.attempt > failure.attempt
    return candidate.run_number > failure.run_number


def _execution_key(run: RunObservation) -> tuple[int, int, str, int]:
    return (run.run_number, run.attempt, run.created_at, run.run_id)


def _attention(
    original: WorkflowItem,
    item: WorkflowItem,
    now: str,
    summary: str,
    *,
    judgment_round: int | None = None,
) -> ItemTransition:
    return _finish(
        original,
        replace(item, phase=ItemPhase.NEEDS_ATTENTION, latest_error=summary),
        now,
        NextStep.NEEDS_ATTENTION,
        "needs-attention",
        summary,
        judgment_round=judgment_round,
    )


def _finish(
    original: WorkflowItem,
    item: WorkflowItem,
    now: str,
    next_step: NextStep,
    history_event: str,
    summary: str,
    *,
    action_kind: ActionKind | None = None,
    judgment_round: int | None = None,
    retain_judgment: bool = False,
) -> ItemTransition:
    updated = item
    if _semantic_signature(original) != _semantic_signature(item):
        updated = replace(updated, last_progressed_at=now)
    return ItemTransition(
        item=updated,
        next_step=next_step,
        history_event=history_event,
        summary=summary,
        action_kind=action_kind,
        judgment_round=judgment_round,
        retain_judgment=retain_judgment,
    )


def _semantic_signature(item: WorkflowItem) -> tuple[object, ...]:
    ignored = {"last_checked_at", "last_progressed_at", "read_status"}
    return tuple(
        getattr(item, field.name)
        for field in fields(item)
        if field.name not in ignored
    )
