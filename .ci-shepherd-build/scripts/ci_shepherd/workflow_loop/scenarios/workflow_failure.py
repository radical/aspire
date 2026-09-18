from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, replace
import json
from typing import cast, Literal

from ci_shepherd.observations import workflow_log_preview

from ..models import (
    ActionKind,
    ActionState,
    ItemPhase,
    JobObservation,
    JudgmentDecision,
    JudgmentRequest,
    JudgmentResult,
    TaskState,
    WorkflowItem,
    WorkState,
)
from ..reader import (
    ItemRefresh,
    ReaderSnapshot,
    RepairEvidenceResult,
    WorkflowReader,
)
from ..reducer import reduce_item
from ..scenario import (
    ConfirmedIssueCreation,
    ItemTransition,
    JudgmentPreparation,
    ScenarioDiscovery,
    ScenarioObservation,
)
from ..state import WorkflowLoopStore
from .workflow_policy import workflow_priority


class WorkflowFailureScenario:
    """Workflow-failure selection, evidence, judgment, and recovery policy."""

    name = "workflow-failure"

    def __init__(self, reader: WorkflowReader) -> None:
        self._reader = reader

    def observe(
        self,
        *,
        repository: str,
        branch: str,
        tracked_items: tuple[WorkflowItem, ...],
        workflow_ids: Collection[int] | None,
    ) -> ScenarioObservation:
        snapshot = self._reader.observe(
            repository=repository,
            branch=branch,
            tracked_items=tracked_items,
            workflow_ids=workflow_ids,
        )
        return ScenarioObservation(
            value=snapshot,
            request_count=snapshot.request_count,
            errors=tuple(_read_errors(snapshot)),
        )

    def discover(
        self,
        store: WorkflowLoopStore,
        observation: ScenarioObservation,
        items: tuple[WorkflowItem, ...],
    ) -> tuple[ScenarioDiscovery, ...]:
        snapshot = cast(ReaderSnapshot, observation.value)
        known_workflows = {item.workflow_id for item in items}
        discoveries: list[ScenarioDiscovery] = []
        for workflow in snapshot.workflows:
            failure = workflow.latest_completed
            if (
                workflow.key.workflow_id in known_workflows
                or failure is None
                or failure.conclusion not in {"failure", "timed_out"}
            ):
                continue
            item = store.upsert_failure(
                failure,
                snapshot.observed_at,
                scenario_name=self.name,
                case_key=f"workflow:{failure.key.workflow_id}",
            )
            discoveries.append(
                ScenarioDiscovery(
                    item=item,
                    refresh=ItemRefresh(
                        item_id=item.id,
                        observed_at=snapshot.observed_at,
                        runs=workflow.runs,
                        failure_run=failure,
                        wait_run=None,
                        recovery="failed",
                        recovery_run=None,
                        issue=None,
                        task=None,
                        pull_request=None,
                        pre_write=False,
                        complete=workflow.complete,
                        errors=(),
                        request_count=0,
                    ),
                )
            )
        return tuple(discoveries)

    def owns(self, item: WorkflowItem) -> bool:
        return True

    def priority(self, item: WorkflowItem) -> int:
        return int(workflow_priority(item.workflow_path))

    def refresh(
        self,
        item: WorkflowItem,
        *,
        judgment: JudgmentResult | None,
    ) -> ItemRefresh:
        return self._reader.refresh_item(
            item,
            action=self.action_for_judgment(item, judgment),
        )

    def normalize_item(
        self,
        store: WorkflowLoopStore,
        item: WorkflowItem,
        refresh: ItemRefresh,
    ) -> WorkflowItem:
        normalized = item
        if (
            refresh.failure_run is not None
            and refresh.failure_run.jobs_complete
            and refresh.recovery != "passed"
        ):
            normalized = store.upsert_failure(
                refresh.failure_run,
                refresh.observed_at,
                scenario_name=self.name,
                case_key=item.case_key,
            )
        task = refresh.task
        pull = refresh.pull_request
        if (
            normalized.task_id is not None
            and task is not None
            and task.task_id == normalized.task_id
        ):
            try:
                task_state = TaskState(task.state)
            except ValueError:
                task_state = normalized.task_state
            updated = replace(
                normalized,
                task_state=task_state,
                pull_request_number=(
                    pull.number
                    if pull is not None
                    else normalized.pull_request_number
                ),
            )
            if updated != normalized:
                updated = replace(
                    updated,
                    last_checked_at=refresh.observed_at,
                    last_progressed_at=refresh.observed_at,
                    read_status=(
                        "complete"
                        if refresh.complete
                        else "unavailable"
                    ),
                )
                store.update_item(
                    updated,
                    history_event="ownership-refreshed",
                    summary="Refreshed owned task and pull request state.",
                    detail={"taskState": task.state},
                )
                normalized = updated
        return normalized

    def assess(
        self,
        item: WorkflowItem,
        refresh: ItemRefresh,
        *,
        now: str,
        request: JudgmentRequest | None,
        judgment: JudgmentResult | None,
        confirmed_issue: ConfirmedIssueCreation | None,
        worker_state: WorkState | None,
        action_state: ActionState | None,
        capacity_available: bool,
    ) -> ItemTransition:
        return reduce_item(
            item,
            refresh,
            now=now,
            request=request,
            judgment=judgment,
            confirmed_issue=confirmed_issue,
            worker_state=worker_state,
            action_state=action_state,
            capacity_available=capacity_available,
        )

    def action_for_judgment(
        self,
        item: WorkflowItem,
        judgment: JudgmentResult | None,
    ) -> ActionKind | None:
        if judgment is None:
            return None
        if judgment.decision is JudgmentDecision.FOLLOW_UP:
            return ActionKind.FOLLOW_UP
        if judgment.decision is JudgmentDecision.ASSIGN:
            return (
                ActionKind.CREATE_ISSUE
                if item.issue_number is None
                else ActionKind.ASSIGN_COPILOT
            )
        return None

    def prepare_judgment(
        self,
        *,
        store: WorkflowLoopStore,
        item: WorkflowItem,
        refresh: ItemRefresh,
        judgment_round: int,
        worker_id: str,
        session_id: str,
    ) -> JudgmentPreparation:
        request_count = 0
        errors: tuple[str, ...] = ()
        failure = refresh.failure_run
        if failure is None:
            return JudgmentPreparation(None, item, 0, ())
        failed_jobs = tuple(
            job
            for job in failure.jobs
            if (job.conclusion or "").casefold() in {"failure", "timed_out"}
        )
        if not failure.jobs_complete or not failed_jobs:
            detail = self._reader.read_run_details(
                failure,
                established_jobs=item.failed_jobs,
            )
            request_count += detail.request_count
            errors += tuple(_read_errors(detail))
            if detail.run is None or not detail.run.jobs_complete:
                current = next(
                    candidate
                    for candidate in store.list_items()
                    if candidate.id == item.id
                )
                store.update_item(
                    replace(
                        current,
                        phase=ItemPhase.OBSERVING_FAILURE,
                        read_status="unavailable",
                    ),
                    history_event="failure-detail-unavailable",
                    summary=(
                        "Failure detail is unavailable; judgment will retry "
                        "on a later pass."
                    ),
                    detail={"judgmentRound": judgment_round},
                )
                return JudgmentPreparation(
                    None,
                    current,
                    request_count,
                    errors,
                )
            refresh = replace(
                refresh,
                failure_run=detail.run,
                complete=refresh.complete and detail.run.jobs_complete,
                errors=refresh.errors + detail.errors,
            )
            item = store.upsert_failure(
                detail.run,
                refresh.observed_at,
                scenario_name=self.name,
                case_key=item.case_key,
            )

        repair_evidence = None
        if judgment_round > 0:
            repair_evidence = self._reader.read_repair_evidence(
                item,
                refresh=refresh,
            )
            request_count += repair_evidence.request_count
            errors += tuple(_read_errors(repair_evidence))
        request = build_judgment_request(
            item,
            refresh,
            worker_id=worker_id,
            session_id=session_id,
            judgment_round=judgment_round,
            repair_evidence=repair_evidence,
        )
        return JudgmentPreparation(
            request=request,
            item=item,
            request_count=request_count,
            errors=errors,
        )


@dataclass(frozen=True, slots=True)
class FreshFailureValidation:
    refresh: ItemRefresh | None
    status: Literal["ok", "stale", "superseded", "unavailable"]
    reason: str


def validate_fresh_failure(
    reader: WorkflowReader,
    request: JudgmentRequest,
    item: WorkflowItem,
    *,
    action: ActionKind,
) -> FreshFailureValidation:
    refreshed = reader.refresh_item(item, action=action)
    if not refreshed.pre_write or not refreshed.complete or refreshed.errors:
        return FreshFailureValidation(
            None,
            "unavailable",
            "Fresh pre-write workflow evidence is unavailable or incomplete.",
        )
    if refreshed.recovery == "passed":
        return FreshFailureValidation(
            None,
            "superseded",
            "A positive recovery superseded the requested write.",
        )
    if refreshed.recovery != "failed":
        return FreshFailureValidation(
            None,
            "unavailable",
            f"Fresh recovery state is {refreshed.recovery}.",
        )
    failure = refreshed.failure_run
    if failure is None or not failure.jobs_complete:
        return FreshFailureValidation(
            None,
            "unavailable",
            "Fresh failure evidence is incomplete.",
        )
    if (
        failure.key.repository != request.repository
        or failure.key.branch != request.branch
        or failure.key.workflow_id != request.workflow_id
        or failure.run_id != request.failure_run.run_id
        or failure.attempt != request.failure_run.attempt
        or failure.head_sha != request.failure_run.head_sha
    ):
        return FreshFailureValidation(
            None,
            "stale",
            "Fresh failure run no longer matches the judged request.",
        )
    fresh_jobs = {job.job_id: job for job in failure.jobs}
    if any(
        job.job_id not in fresh_jobs
        or _job_identity(fresh_jobs[job.job_id]) != _job_identity(job)
        for job in request.failed_jobs
    ):
        return FreshFailureValidation(
            None,
            "stale",
            "Fresh failed jobs no longer match the judged request.",
        )
    return FreshFailureValidation(refreshed, "ok", "Fresh failure confirmed.")


def _job_identity(job: JobObservation) -> tuple[object, ...]:
    return (
        job.run_id,
        job.attempt,
        job.job_id,
        job.key,
        job.status,
        job.conclusion,
        job.started_at,
        job.completed_at,
        job.url,
    )


def build_judgment_request(
    item: WorkflowItem,
    refresh: ItemRefresh,
    *,
    worker_id: str,
    session_id: str,
    judgment_round: int,
    repair_evidence: RepairEvidenceResult | None = None,
) -> JudgmentRequest:
    failure = refresh.failure_run
    if failure is None:
        raise ValueError("Judgment requires an authoritative failure run.")
    failed_jobs = tuple(
        job
        for job in failure.jobs
        if (job.conclusion or "").casefold() in {"failure", "timed_out"}
    )
    if not failed_jobs:
        raise ValueError("Judgment requires failed or timed-out jobs.")
    evidence_ids = (
        f"run:{failure.run_id}:{failure.attempt}",
        *(
            f"job:{job.run_id}:{job.attempt}:{job.job_id}"
            for job in failed_jobs
        ),
        *(
            f"log:{job.job_id}"
            for job in failed_jobs
            if job.log_excerpt is not None
        ),
    )
    if repair_evidence is not None:
        evidence_ids = (
            *evidence_ids,
            *(
                f"repair-check:{check.check_run_id}"
                for check in repair_evidence.failed_checks
            ),
            *(
                f"repair-comment:{comment.comment_id}"
                for comment in repair_evidence.bot_comments
            ),
            *(
                f"repair-file:{index}:{file.path}"
                for index, file in enumerate(repair_evidence.files, start=1)
            ),
        )
    pull = refresh.pull_request
    prompt = build_judgment_prompt(
        item,
        refresh,
        judgment_round,
        evidence_ids=tuple(evidence_ids),
        repair_evidence=repair_evidence,
    )
    return JudgmentRequest(
        worker_id=worker_id,
        session_id=session_id,
        item_id=item.id,
        episode=item.episode,
        evidence_fingerprint=item.evidence_fingerprint,
        round=judgment_round,
        repository=item.repository,
        branch=item.branch,
        workflow_id=item.workflow_id,
        workflow_path=item.workflow_path,
        failure_run=failure,
        failed_jobs=failed_jobs,
        evidence_ids=tuple(evidence_ids),
        issue_number=item.issue_number,
        task_id=item.task_id if judgment_round > 0 else None,
        pull_request_number=(
            pull.number if judgment_round > 0 and pull is not None else None
        ),
        pull_request_head_sha=(
            pull.head_sha if judgment_round > 0 and pull is not None else None
        ),
        pull_request_head_ref=(
            pull.head_ref if judgment_round > 0 and pull is not None else None
        ),
        pull_request_base_ref=(
            pull.base_ref if judgment_round > 0 and pull is not None else None
        ),
        pull_request_observed_at=(
            refresh.observed_at
            if judgment_round > 0 and pull is not None
            else None
        ),
        followup_count=item.followup_count,
        prompt=prompt,
    )


def build_judgment_prompt(
    item: WorkflowItem,
    refresh: ItemRefresh,
    judgment_round: int,
    *,
    evidence_ids: tuple[str, ...],
    repair_evidence: RepairEvidenceResult | None = None,
) -> str:
    failure = refresh.failure_run
    assert failure is not None
    jobs = "\n".join(
        _prompt_job_line(job)
        for job in failure.jobs
        if (job.conclusion or "").casefold() in {"failure", "timed_out"}
    )
    if judgment_round == 0:
        context = "Initial main-workflow failure."
    else:
        task = refresh.task
        explanation = (
            task.explanation
            if task is not None
            and task.explanation_available
            and task.explanation is not None
            else "[unavailable]"
        )
        pull = refresh.pull_request
        context = (
            "Owned pull-request follow-up. "
            f"task={item.task_id!r} pr={item.pull_request_number!r} "
            f"head={(pull.head_ref if pull is not None else None)!r} "
            f"headSha={(pull.head_sha if pull is not None else None)!r} "
            f"base={(pull.base_ref if pull is not None else None)!r}. "
            f"Task explanation: {explanation[:4000]}"
        )
        if repair_evidence is not None:
            failed_checks = "\n".join(
                (
                    f"- checkRunId={check.check_run_id} name={check.name!r} "
                    f"conclusion={check.conclusion!r} "
                    f"log={(check.log_excerpt or '[unavailable]')[:2500]}"
                )
                for check in repair_evidence.failed_checks[:3]
            ) or "- [no failed check details available]"
            changed_files = "\n".join(
                f"- {file.status} {file.path}"
                for file in repair_evidence.files[:50]
            ) or "- [changed files unavailable]"
            bot_comments = "\n".join(
                f"- {comment.author}: {comment.body[:500]}"
                for comment in repair_evidence.bot_comments[:3]
            ) or "- [bot comments unavailable]"
            limitations = ", ".join(repair_evidence.limitations) or "none"
            context += (
                "\nRepair failed checks:\n"
                f"{failed_checks}\nChanged files:\n{changed_files}\n"
                f"Bot feedback:\n{bot_comments}\n"
                f"Evidence limitations: {limitations}"
            )
    body = (
        "Classify only the supplied CI evidence. Ordinary test failures must "
        "use decision defer_ordinary_test. Do not perform GitHub writes or "
        "execute copilotRequest.\n\n"
        f"{context}\n"
        f"Repository: {item.repository}\n"
        f"Workflow: {item.workflow_path}\n"
        f"Run: {failure.run_id} attempt {failure.attempt}\n"
        f"{jobs}"
    )
    failed_job_ids = [job.job_id for job in failure.jobs if (job.conclusion or "").casefold() in {"failure", "timed_out"}]
    schema = (
        "\n\n"
        "Return exactly one compact JSON object with no Markdown or surrounding "
        "text. Copy these identity values exactly and preserve their JSON types:\n"
        f'- "schemaVersion": 1\n'
        f'- "itemId": {item.id}\n'
        f'- "episode": {item.episode}\n'
        f'- "evidenceFingerprint": {json.dumps(item.evidence_fingerprint)}\n'
        f"- evidenceIds may contain only these exact strings: "
        f"{json.dumps(list(evidence_ids))}\n"
        f"- inScopeJobIds may contain only these integer IDs: "
        f"{json.dumps(failed_job_ids)}\n"
        "The remaining fields are decision, summary, and copilotRequest. "
        "decision must be assign, follow_up, observe_external, "
        "defer_ordinary_test, needs_attention, or no_action. For assign or "
        "follow_up, include the exact in-scope failed job IDs and a bounded "
        "string copilotRequest. For every other decision, inScopeJobIds must "
        "be [] and copilotRequest must be null."
    )
    return f"{body[:18_000]}{schema}"


def _prompt_job_line(job: JobObservation) -> str:
    log = job.log_excerpt
    excerpt = (
        "[unavailable]"
        if log is None
        else workflow_log_preview(log, 4_000)
    )
    return (
        f"- jobId={job.job_id} name={job.key.name!r} "
        f"conclusion={job.conclusion!r} "
        f"logSourceTruncated={str(job.log_truncated).lower()} "
        f"promptExcerpted={str(log is not None and len(log) > 4_000).lower()} "
        f"log={excerpt}"
    )


def _read_errors(value: object) -> list[str]:
    return [
        f"{error.scope}:{error.code}:{error.detail}"
        for error in getattr(value, "errors", ())
    ]
