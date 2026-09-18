from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import json
from typing import Any, Literal, Protocol

from .effects import EffectResult, GitHubEffectExecutor
from .models import (
    ActionIntent,
    ActionKind,
    ActionState,
    ActionView,
    JobObservation,
    JudgmentDecision,
    JudgmentRequest,
    JudgmentResult,
    WorkflowItem,
    judgment_request_to_json,
)
from .reader import IssueSearchResult, ItemRefresh
from .scenarios.workflow_failure import validate_fresh_failure
from .state import WorkflowLoopStore


WriterStatus = Literal[
    "confirmed",
    "capacity_wait",
    "superseded",
    "unavailable",
    "stale",
    "no_op",
    "uncertain",
]


@dataclass(frozen=True, slots=True)
class WorkflowWriteResult:
    status: WriterStatus
    reason: str
    action_ids: tuple[str, ...] = ()
    issue_number: int | None = None
    task_id: str | None = None
    newly_confirmed: bool = False


class _Reader(Protocol):
    def refresh_item(
        self,
        item: WorkflowItem,
        *,
        action: ActionKind | None = None,
    ) -> ItemRefresh: ...

    def find_tracking_issue(self, item: WorkflowItem) -> IssueSearchResult: ...


class _Actor(Protocol):
    def create_issue(
        self,
        repository: str,
        *,
        title: str,
        body: str,
    ) -> dict[str, object]: ...

    def create_copilot_task(
        self,
        repository: str,
        *,
        prompt: str,
        base_branch: str,
        head_branch: str | None = None,
        model: str | None = None,
    ) -> dict[str, object]: ...


class WorkflowWriter:
    def __init__(
        self,
        *,
        store: WorkflowLoopStore,
        reader: _Reader,
        actor: _Actor,
        repository: str,
        branch: str,
        clock: Callable[[], datetime],
        active_item_limit: int,
        cloud_model: str | None = None,
    ) -> None:
        if not repository.strip():
            raise ValueError("repository must be nonempty.")
        if not branch.strip():
            raise ValueError("branch must be nonempty.")
        if active_item_limit < 1:
            raise ValueError("active_item_limit must be positive.")
        if cloud_model is not None and not cloud_model.strip():
            raise ValueError("cloud_model must be nonempty when configured.")
        self._store = store
        self._reader = reader
        self._actor = actor
        self._repository = repository
        self._branch = branch
        self._clock = clock
        self._active_item_limit = active_item_limit
        self._cloud_model = cloud_model
        self._effects = GitHubEffectExecutor(
            store=store,
            clock=self._now,
            active_item_limit=active_item_limit,
        )

    def execute(
        self,
        request: JudgmentRequest,
        result: JudgmentResult,
        *,
        pass_id: str,
        owner_id: str,
    ) -> WorkflowWriteResult:
        if result.decision not in {
            JudgmentDecision.ASSIGN,
            JudgmentDecision.FOLLOW_UP,
        }:
            return WorkflowWriteResult(
                "no_op",
                f"Judgment decision {result.decision.value} requires no write.",
            )
        stale = self._validate_judgment(request, result)
        if stale is not None:
            return stale
        final_kind = (
            ActionKind.FOLLOW_UP
            if result.decision is JudgmentDecision.FOLLOW_UP
            else ActionKind.ASSIGN_COPILOT
        )
        final_ordinal = (
            request.followup_count + 1
            if final_kind is ActionKind.FOLLOW_UP
            else 1
        )
        final_action = self._action(request, final_kind, final_ordinal)
        if (
            final_action is not None
            and final_action.state
            in {
                ActionState.CONFIRMED,
                ActionState.INVOKING,
                ActionState.UNCERTAIN,
            }
        ):
            return self._existing_action_result(final_action)
        item = self._item(request.item_id)
        allowed_created_issue = None
        if result.decision is JudgmentDecision.ASSIGN:
            allowed_created_issue = self._confirmed_issue(
                self._action(request, ActionKind.CREATE_ISSUE, 1),
                request,
                result,
            )
        stale = self._validate_item(
            request,
            item,
            allowed_created_issue=allowed_created_issue,
        )
        if stale is not None:
            return stale

        if result.decision is JudgmentDecision.FOLLOW_UP:
            return self._execute_follow_up(
                request,
                result,
                pass_id=pass_id,
                owner_id=owner_id,
            )
        return self._execute_initial(
            request,
            result,
            pass_id=pass_id,
            owner_id=owner_id,
        )

    def _execute_initial(
        self,
        request: JudgmentRequest,
        result: JudgmentResult,
        *,
        pass_id: str,
        owner_id: str,
    ) -> WorkflowWriteResult:
        action_ids: list[str] = []
        item = self._item(request.item_id)
        issue_number = request.issue_number
        if issue_number is None:
            issue_action = self._action(request, ActionKind.CREATE_ISSUE, 1)
            existing_issue = self._confirmed_issue(
                issue_action,
                request,
                result,
            )
            if existing_issue is not None:
                issue_number = existing_issue
                self._bind_issue(item, issue_number)
            elif (
                issue_action is not None
                and issue_action.state is not ActionState.PREPARED
            ):
                return self._existing_action_result(issue_action)
            else:
                fresh = self._fresh(
                    request,
                    item,
                    action=ActionKind.CREATE_ISSUE,
                )
                if isinstance(fresh, WorkflowWriteResult):
                    return fresh
                search = self._reader.find_tracking_issue(item)
                if search.status == "unavailable":
                    return WorkflowWriteResult(
                        "unavailable",
                        "Tracking issue search was unavailable.",
                    )
                if search.status != "zero":
                    return WorkflowWriteResult(
                        "stale",
                        "Tracking issue state changed after judgment.",
                    )
                title, body = self._issue_content(request, result)
                issue_payload = {
                    "repository": self._repository,
                    "title": title,
                    "body": body,
                }
                outcome = self._invoke(
                    request,
                    result,
                    kind=ActionKind.CREATE_ISSUE,
                    ordinal=1,
                    write=issue_payload,
                    pass_id=pass_id,
                    owner_id=owner_id,
                    call=lambda: self._actor.create_issue(
                        self._repository,
                        title=title,
                        body=body,
                    ),
                    validate=self._issue_number,
                    pre_invoke=lambda: self._issue_creation_guard(
                        request.item_id
                    ),
                )
                if isinstance(outcome, WorkflowWriteResult):
                    return outcome
                action, issue_number = outcome
                action_ids.append(action.action_id)
                self._bind_issue(self._item(request.item_id), issue_number)

        assert issue_number is not None
        item = self._item(request.item_id)
        fresh = self._fresh(
            request,
            item,
            action=ActionKind.ASSIGN_COPILOT,
            allowed_created_issue=issue_number,
        )
        if isinstance(fresh, WorkflowWriteResult):
            return fresh
        initial_target = self._initial_target_result(fresh, issue_number)
        if initial_target is not None:
            return replace(
                initial_target,
                action_ids=tuple(action_ids),
                issue_number=issue_number,
            )

        prompt = self._initial_prompt(request, result, issue_number)
        outcome = self._invoke(
            request,
            result,
            kind=ActionKind.ASSIGN_COPILOT,
            ordinal=1,
            write={
                "repository": self._repository,
                "prompt": prompt,
                "base_branch": self._branch,
                "head_branch": None,
                "model": self._cloud_model,
                "issue_number": issue_number,
            },
            pass_id=pass_id,
            owner_id=owner_id,
            call=lambda: self._actor.create_copilot_task(
                self._repository,
                prompt=prompt,
                base_branch=self._branch,
                model=self._cloud_model,
            ),
            validate=self._task_id,
            allowed_created_issue=issue_number,
        )
        if isinstance(outcome, WorkflowWriteResult):
            return replace(
                outcome,
                action_ids=tuple(action_ids) + outcome.action_ids,
                issue_number=issue_number,
            )
        action, task_id = outcome
        action_ids.append(action.action_id)
        return WorkflowWriteResult(
            "confirmed",
            "Initial Copilot task creation was confirmed.",
            tuple(action_ids),
            issue_number,
            task_id,
            True,
        )

    def _execute_follow_up(
        self,
        request: JudgmentRequest,
        result: JudgmentResult,
        *,
        pass_id: str,
        owner_id: str,
    ) -> WorkflowWriteResult:
        item = self._item(request.item_id)
        fresh = self._fresh(
            request,
            item,
            action=ActionKind.FOLLOW_UP,
        )
        if isinstance(fresh, WorkflowWriteResult):
            return fresh
        target_result = self._follow_up_target_result(request, item, fresh)
        if target_result is not None:
            return target_result
        assert request.issue_number is not None
        assert request.pull_request_number is not None
        assert request.pull_request_head_ref is not None
        prompt = self._follow_up_prompt(request, result)
        outcome = self._invoke(
            request,
            result,
            kind=ActionKind.FOLLOW_UP,
            ordinal=request.followup_count + 1,
            write={
                "repository": self._repository,
                "prompt": prompt,
                "base_branch": self._branch,
                "head_branch": request.pull_request_head_ref,
                "model": self._cloud_model,
                "issue_number": request.issue_number,
                "pull_request_number": request.pull_request_number,
                "pull_request_head_sha": request.pull_request_head_sha,
            },
            pass_id=pass_id,
            owner_id=owner_id,
            call=lambda: self._actor.create_copilot_task(
                self._repository,
                prompt=prompt,
                base_branch=self._branch,
                head_branch=request.pull_request_head_ref,
                model=self._cloud_model,
            ),
            validate=self._task_id,
        )
        if isinstance(outcome, WorkflowWriteResult):
            return outcome
        action, task_id = outcome
        return WorkflowWriteResult(
            "confirmed",
            "Existing-PR Copilot task creation was confirmed.",
            (action.action_id,),
            request.issue_number,
            task_id,
            True,
        )

    def _invoke(
        self,
        request: JudgmentRequest,
        result: JudgmentResult,
        *,
        kind: ActionKind,
        ordinal: int,
        write: Mapping[str, object],
        pass_id: str,
        owner_id: str,
        call: Callable[[], object],
        validate: Callable[[object], Any],
        allowed_created_issue: int | None = None,
        pre_invoke: Callable[[], WorkflowWriteResult | None] | None = None,
    ) -> tuple[ActionView, Any] | WorkflowWriteResult:
        action_id = self._action_id(request, kind, ordinal)
        payload = {
            "request": json.loads(judgment_request_to_json(request)),
            "result": self._result_document(result),
            "write": dict(write),
        }
        intent = ActionIntent(
            action_id=action_id,
            item_id=request.item_id,
            episode=request.episode,
            kind=kind,
            ordinal=ordinal,
            payload=payload,
            prepared_at=self._now(),
        )

        def guard() -> EffectResult | None:
            item = self._item(request.item_id)
            fresh = self._fresh(
                request,
                item,
                action=kind,
                allowed_created_issue=allowed_created_issue,
            )
            if isinstance(fresh, WorkflowWriteResult):
                return EffectResult(fresh.status, fresh.reason)
            target_result: WorkflowWriteResult | None = None
            if kind is ActionKind.ASSIGN_COPILOT:
                issue_number = write.get("issue_number")
                if isinstance(issue_number, int):
                    target_result = self._initial_target_result(
                        fresh,
                        issue_number,
                    )
            elif kind is ActionKind.FOLLOW_UP:
                target_result = self._follow_up_target_result(
                    request,
                    item,
                    fresh,
                )
            if target_result is not None:
                return EffectResult(
                    target_result.status,
                    target_result.reason,
                )
            if pre_invoke is not None:
                guarded = pre_invoke()
                if guarded is not None:
                    return EffectResult(guarded.status, guarded.reason)
            return None

        outcome = self._effects.execute(
            intent,
            pass_id=pass_id,
            owner_id=owner_id,
            guard=guard,
            call=call,
            validate=validate,
        )
        if outcome.status != "confirmed":
            return WorkflowWriteResult(
                outcome.status,
                outcome.reason,
                (() if outcome.action_id is None else (outcome.action_id,)),
            )
        action = self._action(request, kind, ordinal)
        assert action is not None
        return action, outcome.value

    def _fresh(
        self,
        request: JudgmentRequest,
        item: WorkflowItem,
        *,
        action: ActionKind,
        allowed_created_issue: int | None = None,
    ) -> ItemRefresh | WorkflowWriteResult:
        stale = self._validate_item(
            request,
            item,
            allowed_created_issue=allowed_created_issue,
        )
        if stale is not None:
            return stale
        validated = validate_fresh_failure(
            self._reader,
            request,
            item,
            action=action,
        )
        if validated.refresh is None:
            return WorkflowWriteResult(
                validated.status,
                validated.reason,
            )
        refreshed = validated.refresh
        current = self._item(item.id)
        stale = self._validate_item(
            request,
            current,
            allowed_created_issue=allowed_created_issue,
        )
        if stale is not None:
            return stale
        return refreshed

    def _issue_creation_guard(
        self,
        item_id: int,
    ) -> WorkflowWriteResult | None:
        item = self._item(item_id)
        search = self._reader.find_tracking_issue(item)
        if search.status == "zero":
            return None
        if search.status == "unavailable":
            return WorkflowWriteResult(
                "unavailable",
                "Tracking issue search was unavailable before invocation.",
            )
        return WorkflowWriteResult(
            "stale",
            "Tracking issue state changed before invocation.",
        )

    def _validate_judgment(
        self,
        request: JudgmentRequest,
        result: JudgmentResult,
    ) -> WorkflowWriteResult | None:
        if (
            request.repository != self._repository
            or request.branch != self._branch
        ):
            return WorkflowWriteResult(
                "stale",
                "Judgment targets a foreign repository or branch.",
            )
        if (
            result.item_id != request.item_id
            or result.episode != request.episode
            or result.evidence_fingerprint != request.evidence_fingerprint
        ):
            return WorkflowWriteResult(
                "stale",
                "Judgment result does not match its persisted request.",
            )
        if not set(result.evidence_ids).issubset(request.evidence_ids):
            return WorkflowWriteResult(
                "stale",
                "Judgment result cites evidence outside its persisted request.",
            )
        failed_job_ids = {job.job_id for job in request.failed_jobs}
        if not set(result.in_scope_job_ids).issubset(failed_job_ids):
            return WorkflowWriteResult(
                "stale",
                "Judgment result targets jobs outside its persisted request.",
            )
        if (
            result.decision is JudgmentDecision.ASSIGN
            and not result.in_scope_job_ids
        ):
            return WorkflowWriteResult(
                "stale",
                "Initial assignment requires a nonempty validated job scope.",
            )
        if result.decision in {
            JudgmentDecision.ASSIGN,
            JudgmentDecision.FOLLOW_UP,
        } and result.copilot_request is None:
            return WorkflowWriteResult(
                "stale",
                "The write judgment has no Copilot repair request.",
            )
        return None

    def _validate_item(
        self,
        request: JudgmentRequest,
        item: WorkflowItem,
        *,
        allowed_created_issue: int | None = None,
    ) -> WorkflowWriteResult | None:
        if (
            item.id != request.item_id
            or item.repository != request.repository
            or item.branch != request.branch
            or item.workflow_id != request.workflow_id
            or item.workflow_path != request.workflow_path
            or item.episode != request.episode
            or item.evidence_fingerprint != request.evidence_fingerprint
        ):
            return WorkflowWriteResult(
                "stale",
                "Current item identity, episode, or evidence changed.",
            )
        expected_issue = request.issue_number
        if expected_issue is None:
            expected_issue = allowed_created_issue
        if item.issue_number != expected_issue:
            return WorkflowWriteResult(
                "stale",
                "Current issue binding changed after judgment.",
            )
        if item.task_id != request.task_id:
            return WorkflowWriteResult(
                "stale",
                "Current task binding changed after judgment.",
            )
        if item.pull_request_number != request.pull_request_number:
            return WorkflowWriteResult(
                "stale",
                "Current pull request binding changed after judgment.",
            )
        if (
            request.task_id is not None
            and item.followup_count != request.followup_count
        ):
            return WorkflowWriteResult(
                "stale",
                "Current follow-up count changed after judgment.",
            )
        return None

    @staticmethod
    def _initial_target_result(
        refreshed: ItemRefresh,
        issue_number: int,
    ) -> WorkflowWriteResult | None:
        if refreshed.issue is None or refreshed.issue.number != issue_number:
            return WorkflowWriteResult(
                "stale",
                "The exact tracking issue is no longer available.",
            )
        if (
            refreshed.issue.human_assigned
            or refreshed.issue.copilot_assigned
            or refreshed.issue.assignees
        ):
            return WorkflowWriteResult(
                "superseded",
                "Tracking issue ownership changed before task creation.",
            )
        return None

    def _follow_up_target_result(
        self,
        request: JudgmentRequest,
        item: WorkflowItem,
        refreshed: ItemRefresh,
    ) -> WorkflowWriteResult | None:
        if request.followup_count >= 2 or item.followup_count >= 2:
            return WorkflowWriteResult(
                "stale",
                "The per-episode follow-up limit is exhausted.",
            )
        if request.issue_number is None or refreshed.issue is None:
            return WorkflowWriteResult(
                "stale",
                "The exact issue target is unavailable.",
            )
        if refreshed.issue.number != request.issue_number:
            return WorkflowWriteResult(
                "stale",
                "The issue target changed after judgment.",
            )
        if request.task_id is None or refreshed.task is None:
            return WorkflowWriteResult(
                "stale",
                "The exact task target is unavailable.",
            )
        if refreshed.task.task_id != request.task_id:
            return WorkflowWriteResult(
                "stale",
                "The task target changed after judgment.",
            )
        if refreshed.task.state in {
            "queued",
            "in_progress",
            "waiting_for_user",
        }:
            return WorkflowWriteResult(
                "unavailable",
                f"The existing task is currently {refreshed.task.state}.",
            )
        if refreshed.task.state not in {
            "idle",
            "completed",
            "failed",
            "timed_out",
            "cancelled",
        }:
            return WorkflowWriteResult(
                "unavailable",
                f"The existing task state {refreshed.task.state!r} is not eligible.",
            )
        pull = refreshed.pull_request
        if pull is None or request.pull_request_number is None:
            return WorkflowWriteResult(
                "stale",
                "The exact pull request target is unavailable.",
            )
        if (
            pull.number != request.pull_request_number
            or pull.state != "open"
            or pull.merged
            or pull.head_repository.casefold() != self._repository.casefold()
            or pull.base_repository.casefold() != self._repository.casefold()
            or pull.base_ref != self._branch
            or pull.head_ref != request.pull_request_head_ref
            or pull.head_sha != request.pull_request_head_sha
            or request.pull_request_base_ref != self._branch
        ):
            return WorkflowWriteResult(
                "stale",
                "The pull request identity changed after judgment.",
            )
        if not pull.complete or not pull.checks_complete:
            return WorkflowWriteResult(
                "unavailable",
                "Pull request checks are incomplete or require human action.",
            )
        if pull.checks_state == "green":
            return WorkflowWriteResult(
                "superseded",
                "Pull request checks recovered before follow-up.",
            )
        if pull.checks_state != "red":
            return WorkflowWriteResult(
                "unavailable",
                f"Pull request checks are {pull.checks_state}.",
            )
        return None

    def _bind_issue(self, item: WorkflowItem, issue_number: int) -> None:
        if item.issue_number == issue_number:
            return
        self._store.update_item(
            replace(item, issue_number=issue_number),
            history_event="tracking-issue-bound",
            summary="Bound the confirmed tracking issue.",
            detail={"issueNumber": issue_number},
        )

    def _confirmed_issue(
        self,
        action: ActionView | None,
        request: JudgmentRequest,
        result: JudgmentResult,
    ) -> int | None:
        if (
            action is None
            or action.state is not ActionState.CONFIRMED
            or action.remote_number is None
            or action.item_id != request.item_id
            or action.episode != request.episode
        ):
            return None
        if (
            action.payload.get("request")
            != json.loads(judgment_request_to_json(request))
            or action.payload.get("result") != self._result_document(result)
        ):
            return None
        return action.remote_number

    def _existing_action_result(
        self,
        action: ActionView,
    ) -> WorkflowWriteResult:
        if action.state is ActionState.CONFIRMED:
            return WorkflowWriteResult(
                "confirmed",
                "The action was already confirmed.",
                (action.action_id,),
                action.remote_number,
                action.remote_task_id,
            )
        if action.state in {ActionState.INVOKING, ActionState.UNCERTAIN}:
            return WorkflowWriteResult(
                "uncertain",
                "The action was already invoked and cannot be retried.",
                (action.action_id,),
            )
        if action.state is ActionState.PREPARED:
            return WorkflowWriteResult(
                "unavailable",
                "The action is prepared and awaits fresh invocation.",
                (action.action_id,),
            )
        return WorkflowWriteResult(
            "stale",
            f"The action is already {action.state.value}.",
            (action.action_id,),
        )

    def _action(
        self,
        request: JudgmentRequest,
        kind: ActionKind,
        ordinal: int,
    ) -> ActionView | None:
        action_id = self._action_id(request, kind, ordinal)
        return next(
            (
                action
                for action in self._store.list_actions()
                if action.action_id == action_id
            ),
            None,
        )

    @staticmethod
    def _action_id(
        request: JudgmentRequest,
        kind: ActionKind,
        ordinal: int,
    ) -> str:
        return (
            f"{request.worker_id}:{request.item_id}:{request.episode}:"
            f"{request.evidence_fingerprint}:{kind.value}:{ordinal}"
        )

    def _item(self, item_id: int) -> WorkflowItem:
        return next(
            item for item in self._store.list_items() if item.id == item_id
        )

    def _now(self) -> str:
        return (
            self._clock()
            .astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _issue_number(payload: object) -> int:
        if not isinstance(payload, Mapping):
            raise RuntimeError("GitHub returned a non-object issue response.")
        number = payload.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise RuntimeError("GitHub returned an issue without a valid number.")
        return number

    @staticmethod
    def _task_id(payload: object) -> str:
        if not isinstance(payload, Mapping):
            raise RuntimeError("GitHub returned a non-object task response.")
        task_id = payload.get("id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise RuntimeError("GitHub returned a task without a valid id.")
        return task_id

    @staticmethod
    def _result_document(result: JudgmentResult) -> dict[str, object]:
        return {
            "schemaVersion": result.schema_version,
            "itemId": result.item_id,
            "episode": result.episode,
            "evidenceFingerprint": result.evidence_fingerprint,
            "decision": result.decision.value,
            "summary": result.summary,
            "evidenceIds": list(result.evidence_ids),
            "inScopeJobIds": list(result.in_scope_job_ids),
            "copilotRequest": result.copilot_request,
        }

    def _issue_content(
        self,
        request: JudgmentRequest,
        result: JudgmentResult,
    ) -> tuple[str, str]:
        scoped_jobs = self._scoped_jobs(request, result)
        failures = "\n".join(
            f"- `{job.key.name}`: {job.log_excerpt or 'No bounded error text available.'} "
            f"([job]({job.url}))"
            for job in scoped_jobs
        )
        title = f"[automated] CI workflow failure: {request.failure_run.workflow_name}"
        body = (
            "[automated] **Summary**\n\n"
            f"`{request.failure_run.workflow_name}` is failing on "
            f"`{request.branch}`.\n\n"
            "**Repro**\n\n"
            f"- [Workflow run]({request.failure_run.url})\n"
            f"- Commit: `{request.failure_run.head_sha}`\n\n"
            "**Expected**\n\n"
            "The workflow completes successfully.\n\n"
            "**Actual**\n\n"
            f"{failures}\n\n"
            "<!-- ci-shepherd:workflow-repair "
            f"repository={request.repository} workflow-id={request.workflow_id} "
            f"branch={request.branch} -->"
        )
        return title, body

    def _initial_prompt(
        self,
        request: JudgmentRequest,
        result: JudgmentResult,
        issue_number: int,
    ) -> str:
        scoped_jobs = self._scoped_jobs(request, result)
        jobs = ", ".join(
            f"{job.key.name} ({job.url})" for job in scoped_jobs
        )
        return (
            "[automated] Repair only the workflow failure tracked in "
            f"{self._repository}#{issue_number}. {result.copilot_request} "
            f"Source run: {request.failure_run.url}. Failed jobs: {jobs}. "
            f"Observed source SHA: {request.failure_run.head_sha}. Work only in "
            f"{self._repository} and target base `{self._branch}`. Keep any pull "
            "request as a draft and never merge it. "
            f"Refs {self._repository}#{issue_number}."
        )

    def _follow_up_prompt(
        self,
        request: JudgmentRequest,
        result: JudgmentResult,
    ) -> str:
        assert request.issue_number is not None
        assert request.pull_request_number is not None
        assert request.pull_request_head_sha is not None
        scoped_jobs = self._scoped_jobs(request, result)
        jobs = ", ".join(
            f"{job.key.name} ({job.url})" for job in scoped_jobs
        )
        return (
            "[automated] Continue only the repair in PR "
            f"{self._repository}#{request.pull_request_number}. "
            f"{result.copilot_request} Source run: {request.failure_run.url}. "
            f"Failed jobs: {jobs}. Observed PR head SHA: "
            f"{request.pull_request_head_sha}. Work only in {self._repository} "
            f"and retain base `{self._branch}`. Keep the PR as a draft and "
            f"never merge it. Refs {self._repository}#{request.issue_number}."
        )

    @staticmethod
    def _scoped_jobs(
        request: JudgmentRequest,
        result: JudgmentResult,
    ) -> tuple[JobObservation, ...]:
        selected = set(result.in_scope_job_ids)
        return tuple(
            job for job in request.failed_jobs if job.job_id in selected
        )
