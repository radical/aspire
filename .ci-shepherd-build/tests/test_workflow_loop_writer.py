from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import importlib
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import ModuleType
from typing import Callable
import unittest

from ci_shepherd.workflow_loop.models import (
    ActionKind,
    ActionState,
    ItemPhase,
    JobKey,
    JobObservation,
    JudgmentDecision,
    JudgmentRequest,
    JudgmentResult,
    RunObservation,
    TaskState,
    WorkflowKey,
)
from ci_shepherd.workflow_loop.reader import (
    IssueObservation,
    IssueSearchResult,
    ItemRefresh,
    PullRequestObservation,
    TaskBranchArtifact,
    TaskObservation,
)
from ci_shepherd.workflow_loop.state import WorkflowLoopStore


NOW = "2026-09-17T20:00:00Z"
LATER = "2026-09-17T20:01:00Z"
REPOSITORY = "radical/aspire"
BRANCH = "main"


def _writer_module() -> ModuleType:
    try:
        return importlib.import_module("ci_shepherd.workflow_loop.writer")
    except ModuleNotFoundError as error:
        raise AssertionError("The workflow writer API is missing.") from error


def _job(
    *,
    run_id: int = 101,
    job_id: int = 900,
    name: str = "Build / Linux",
) -> JobObservation:
    return JobObservation(
        run_id=run_id,
        attempt=1,
        job_id=job_id,
        key=JobKey(name, ("ubuntu-latest",)),
        status="completed",
        conclusion="failure",
        started_at=NOW,
        completed_at=LATER,
        url=f"https://github.com/{REPOSITORY}/actions/runs/{run_id}/job/{job_id}",
        log_excerpt="error CS1002: ; expected",
        log_truncated=False,
    )


def _run(
    *,
    run_id: int = 101,
    conclusion: str = "failure",
    head_sha: str = "a" * 40,
    jobs: tuple[JobObservation, ...] | None = None,
) -> RunObservation:
    observations = jobs or (_job(run_id=run_id),)
    return RunObservation(
        key=WorkflowKey(REPOSITORY, 42, BRANCH),
        workflow_path=".github/workflows/ci.yml",
        workflow_name="CI",
        run_id=run_id,
        run_number=88,
        attempt=1,
        head_sha=head_sha,
        event="push",
        status="completed",
        conclusion=conclusion,
        created_at=NOW,
        updated_at=LATER,
        url=f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
        jobs_complete=True,
        jobs=observations,
    )


def _request(
    item,
    failure_run: RunObservation,
    *,
    issue_number: int | None = None,
    task_id: str | None = None,
    pull_request_number: int | None = None,
    pull_request_head_sha: str | None = None,
    pull_request_head_ref: str | None = None,
    pull_request_base_ref: str | None = None,
    pull_request_observed_at: str | None = None,
    followup_count: int = 0,
) -> JudgmentRequest:
    return JudgmentRequest(
        worker_id=f"worker-{item.id}-{item.episode}",
        session_id=f"session-{item.id}",
        item_id=item.id,
        episode=item.episode,
        evidence_fingerprint=item.evidence_fingerprint,
        round=0 if task_id is None else followup_count + 1,
        repository=REPOSITORY,
        branch=BRANCH,
        workflow_id=item.workflow_id,
        workflow_path=item.workflow_path,
        failure_run=failure_run,
        failed_jobs=failure_run.jobs,
        evidence_ids=(
            f"run:{failure_run.run_id}",
            f"job:{failure_run.run_id}:{failure_run.jobs[0].job_id}",
        ),
        issue_number=issue_number,
        task_id=task_id,
        pull_request_number=pull_request_number,
        pull_request_head_sha=pull_request_head_sha,
        pull_request_head_ref=pull_request_head_ref,
        pull_request_base_ref=pull_request_base_ref,
        pull_request_observed_at=pull_request_observed_at,
        followup_count=followup_count,
        prompt="Classify the workflow failure.",
    )


def _result(
    request: JudgmentRequest,
    decision: JudgmentDecision,
) -> JudgmentResult:
    return JudgmentResult(
        schema_version=1,
        item_id=request.item_id,
        episode=request.episode,
        evidence_fingerprint=request.evidence_fingerprint,
        decision=decision,
        summary="The workflow failure needs a bounded repair.",
        evidence_ids=request.evidence_ids,
        in_scope_job_ids=tuple(job.job_id for job in request.failed_jobs),
        copilot_request=(
            "Fix the observed build failure and add focused regression coverage."
            if decision in {JudgmentDecision.ASSIGN, JudgmentDecision.FOLLOW_UP}
            else None
        ),
    )


def _issue(number: int) -> IssueObservation:
    return IssueObservation(
        number=number,
        url=f"https://github.com/{REPOSITORY}/issues/{number}",
        title="CI workflow failure: CI",
        marker=(
            "<!-- ci-shepherd:workflow-repair "
            f"repository={REPOSITORY} workflow-id=42 branch={BRANCH} -->"
        ),
        assignees=(),
        copilot_assigned=False,
        human_assigned=False,
    )


def _task(task_id: str) -> TaskObservation:
    return TaskObservation(
        task_id=task_id,
        state="idle",
        url=None,
        repository_id=1,
        updated_at=LATER,
        session_count=1,
        pull_request_database_ids=(),
        branch_artifacts=(
            TaskBranchArtifact(
                head_ref="copilot/repair-ci",
                base_ref=BRANCH,
            ),
        ),
        outcome=None,
        explanation=None,
        explanation_available=False,
    )


def _pull_request(
    *,
    number: int = 201,
    head_sha: str = "b" * 40,
    head_ref: str = "copilot/repair-ci",
) -> PullRequestObservation:
    return PullRequestObservation(
        number=number,
        state="open",
        merged=False,
        draft=True,
        url=f"https://github.com/{REPOSITORY}/pull/{number}",
        head_repository=REPOSITORY,
        head_ref=head_ref,
        head_sha=head_sha,
        base_repository=REPOSITORY,
        base_ref=BRANCH,
        checks_state="red",
        checks_complete=True,
        review_decision="",
        review_complete=True,
        complete=True,
        incomplete_reasons=(),
    )


def _refresh(
    item,
    failure_run: RunObservation,
    *,
    issue: IssueObservation | None = None,
    task: TaskObservation | None = None,
    pull_request: PullRequestObservation | None = None,
    pre_write: bool = True,
    complete: bool = True,
    recovery: str = "failed",
) -> ItemRefresh:
    return ItemRefresh(
        item_id=item.id,
        observed_at=LATER,
        runs=(failure_run,),
        failure_run=failure_run,
        wait_run=None,
        recovery=recovery,
        recovery_run=(
            replace(failure_run, conclusion="success")
            if recovery == "passed"
            else None
        ),
        issue=issue,
        task=task,
        pull_request=pull_request,
        pre_write=pre_write,
        complete=complete,
        errors=(),
        request_count=1,
    )


class FakeReader:
    def __init__(
        self,
        refresh: Callable[[object, ActionKind | None], ItemRefresh],
        *,
        issue_search: IssueSearchResult | None = None,
    ) -> None:
        self._refresh = refresh
        self._issue_search = issue_search or IssueSearchResult(
            status="zero",
            issue=None,
            candidate_numbers=(),
            errors=(),
            request_count=1,
        )
        self.refresh_calls: list[tuple[object, ActionKind | None]] = []
        self.issue_search_calls: list[object] = []

    def refresh_item(
        self,
        item,
        *,
        action: ActionKind | None = None,
    ) -> ItemRefresh:
        self.refresh_calls.append((item, action))
        return self._refresh(item, action)

    def find_tracking_issue(self, item) -> IssueSearchResult:
        self.issue_search_calls.append(item)
        return self._issue_search


class SequencedReader(FakeReader):
    def __init__(
        self,
        refreshes: list[ItemRefresh | Callable[[object], ItemRefresh]],
        *,
        issue_search: IssueSearchResult | None = None,
    ) -> None:
        self._refreshes = list(refreshes)
        super().__init__(self._next, issue_search=issue_search)

    def _next(self, item, action: ActionKind | None) -> ItemRefresh:
        del action
        value = self._refreshes.pop(0)
        return value(item) if callable(value) else value


class FakeActor:
    def __init__(
        self,
        *,
        issue_number: int = 123,
        task_ids: tuple[object, ...] = ("task-initial", "task-follow-up"),
        before_write: Callable[[], None] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.issue_number = issue_number
        self.task_ids = list(task_ids)
        self.before_write = before_write
        self.error = error
        self.calls: list[tuple[object, ...]] = []

    def create_issue(
        self,
        repository: str,
        *,
        title: str,
        body: str,
    ) -> dict[str, object]:
        self.calls.append(("create_issue", repository, title, body))
        if self.before_write is not None:
            self.before_write()
        if self.error is not None:
            raise self.error
        return {"number": self.issue_number}

    def create_copilot_task(
        self,
        repository: str,
        *,
        prompt: str,
        base_branch: str,
        head_branch: str | None = None,
        model: str | None = None,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "create_copilot_task",
                repository,
                prompt,
                base_branch,
                head_branch,
                model,
            )
        )
        if self.before_write is not None:
            self.before_write()
        if self.error is not None:
            raise self.error
        return {"id": self.task_ids.pop(0)}


class WorkflowWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.state_directory = Path(self.temporary.name) / "state"
        self.store = WorkflowLoopStore(
            self.state_directory,
            repository=REPOSITORY,
            branch=BRANCH,
        )
        self.store.initialize()
        self.failure_run = _run()
        self.item = self.store.upsert_failure(self.failure_run, NOW)
        self.item = replace(
            self.item,
            phase=ItemPhase.READY_FOR_ACTION,
            read_status="complete",
            last_judged_fingerprint=self.item.evidence_fingerprint,
        )
        self.store.update_item(
            self.item,
            history_event="judgment-ready",
            summary="A judgment is ready for action.",
            detail={},
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _writer(self, reader: FakeReader, actor: FakeActor):
        module = _writer_module()
        return module.WorkflowWriter(
            store=self.store,
            reader=reader,
            actor=actor,
            repository=REPOSITORY,
            branch=BRANCH,
            clock=lambda: datetime(2026, 9, 17, 20, 2, tzinfo=UTC),
            active_item_limit=2,
            cloud_model=None,
        )

    def test_initial_creation_and_task_start_use_one_judgment(self) -> None:
        request = _request(self.item, self.failure_run)
        result = _result(request, JudgmentDecision.ASSIGN)
        reader = FakeReader(
            lambda item, action: _refresh(
                item,
                self.failure_run,
                issue=(_issue(item.issue_number) if item.issue_number else None),
            )
        )
        actor = FakeActor()

        outcome = self._writer(reader, actor).execute(
            request,
            result,
            pass_id="pass-1",
            owner_id="owner-1",
        )

        self.assertEqual("confirmed", outcome.status)
        self.assertEqual(123, outcome.issue_number)
        self.assertEqual("task-initial", outcome.task_id)
        self.assertEqual(
            ["create_issue", "create_copilot_task"],
            [call[0] for call in actor.calls],
        )
        issue_call, task_call = actor.calls
        self.assertEqual(REPOSITORY, issue_call[1])
        self.assertTrue(str(issue_call[2]).startswith("[automated] "))
        self.assertTrue(str(issue_call[3]).startswith("[automated] "))
        self.assertIn(self.failure_run.url, str(issue_call[3]))
        self.assertIn("error CS1002", str(issue_call[3]))
        self.assertEqual(REPOSITORY, task_call[1])
        self.assertIn(f"Refs {REPOSITORY}#123", str(task_call[2]))
        self.assertIn(self.failure_run.head_sha, str(task_call[2]))
        self.assertIn("draft", str(task_call[2]).casefold())
        self.assertIn("never merge", str(task_call[2]).casefold())
        self.assertEqual(BRANCH, task_call[3])
        self.assertIsNone(task_call[4])
        self.assertGreaterEqual(len(reader.refresh_calls), 4)
        self.assertEqual(
            [
                ActionKind.CREATE_ISSUE,
                ActionKind.CREATE_ISSUE,
                ActionKind.ASSIGN_COPILOT,
                ActionKind.ASSIGN_COPILOT,
            ],
            [action for _, action in reader.refresh_calls],
        )
        persisted = self.store.list_items()[0]
        self.assertEqual(123, persisted.issue_number)
        self.assertEqual("task-initial", persisted.task_id)

    def test_targeted_existing_issue_skips_issue_creation(self) -> None:
        tracked = replace(self.item, issue_number=77)
        self.store.update_item(
            tracked,
            history_event="issue-bound",
            summary="The tracking issue is bound.",
            detail={},
        )
        request = _request(tracked, self.failure_run, issue_number=77)
        result = _result(request, JudgmentDecision.ASSIGN)
        reader = FakeReader(
            lambda item, action: _refresh(
                item,
                self.failure_run,
                issue=_issue(77),
            )
        )
        actor = FakeActor()

        outcome = self._writer(reader, actor).execute(
            request,
            result,
            pass_id="pass-1",
            owner_id="owner-1",
        )

        self.assertEqual("confirmed", outcome.status)
        self.assertEqual(
            ["create_copilot_task"],
            [call[0] for call in actor.calls],
        )
        self.assertIn(f"Refs {REPOSITORY}#77", str(actor.calls[0][2]))

    def test_follow_up_targets_exact_existing_pull_request_without_comment(
        self,
    ) -> None:
        pull = _pull_request()
        tracked = replace(
            self.item,
            issue_number=77,
            task_id="task-initial",
            task_state=TaskState.IDLE,
            pull_request_number=pull.number,
            followup_count=1,
        )
        self.store.update_item(
            tracked,
            history_event="follow-up-ready",
            summary="The existing pull request needs follow-up.",
            detail={},
        )
        request = _request(
            tracked,
            self.failure_run,
            issue_number=77,
            task_id="task-initial",
            pull_request_number=pull.number,
            pull_request_head_sha=pull.head_sha,
            pull_request_head_ref=pull.head_ref,
            pull_request_base_ref=pull.base_ref,
            pull_request_observed_at=LATER,
            followup_count=1,
        )
        result = _result(request, JudgmentDecision.FOLLOW_UP)
        reader = FakeReader(
            lambda item, action: _refresh(
                item,
                self.failure_run,
                issue=_issue(77),
                task=_task("task-initial"),
                pull_request=pull,
            )
        )
        actor = FakeActor(task_ids=("task-follow-up",))

        outcome = self._writer(reader, actor).execute(
            request,
            result,
            pass_id="pass-1",
            owner_id="owner-1",
        )

        self.assertEqual("confirmed", outcome.status)
        self.assertEqual(1, len(actor.calls))
        call = actor.calls[0]
        self.assertEqual("create_copilot_task", call[0])
        self.assertIn(f"PR {REPOSITORY}#{pull.number}", str(call[2]))
        self.assertIn(f"Refs {REPOSITORY}#77", str(call[2]))
        self.assertEqual(pull.head_ref, call[4])
        self.assertEqual(
            [ActionKind.FOLLOW_UP, ActionKind.FOLLOW_UP],
            [action for _, action in reader.refresh_calls],
        )
        self.assertEqual(2, self.store.list_items()[0].followup_count)

    def test_capacity_wait_and_replay_do_not_duplicate_writes(self) -> None:
        other = self.store.upsert_failure(
            replace(
                _run(run_id=102),
                key=WorkflowKey(REPOSITORY, 43, BRANCH),
                workflow_path=".github/workflows/other.yml",
                workflow_name="Other",
            ),
            NOW,
        )
        other = replace(
            other,
            phase=ItemPhase.COPILOT_ACTIVE,
            task_id="task-cloud",
            task_state=TaskState.IN_PROGRESS,
        )
        self.store.update_item(
            other,
            history_event="cloud-active",
            summary="Cloud work is active.",
            detail={},
        )
        request = _request(self.item, self.failure_run)
        result = _result(request, JudgmentDecision.ASSIGN)
        reader = FakeReader(
            lambda item, action: _refresh(
                item,
                self.failure_run,
                issue=(_issue(item.issue_number) if item.issue_number else None),
            )
        )
        actor = FakeActor()
        writer = _writer_module().WorkflowWriter(
            store=self.store,
            reader=reader,
            actor=actor,
            repository=REPOSITORY,
            branch=BRANCH,
            clock=lambda: datetime(2026, 9, 17, 20, 2, tzinfo=UTC),
            active_item_limit=1,
            cloud_model=None,
        )

        waiting = writer.execute(
            request,
            result,
            pass_id="pass-1",
            owner_id="owner-1",
        )
        self.assertEqual("capacity_wait", waiting.status)
        self.assertEqual([], actor.calls)
        self.assertEqual((), self.store.list_actions())

        self.store.update_item(
            replace(other, task_state=TaskState.COMPLETED),
            history_event="cloud-complete",
            summary="Cloud work completed.",
            detail={},
        )
        first = writer.execute(
            request,
            result,
            pass_id="pass-2",
            owner_id="owner-2",
        )
        second = writer.execute(
            request,
            result,
            pass_id="pass-3",
            owner_id="owner-3",
        )

        self.assertEqual("confirmed", first.status)
        self.assertEqual("confirmed", second.status)
        self.assertTrue(first.newly_confirmed)
        self.assertFalse(second.newly_confirmed)
        self.assertEqual(
            ["create_issue", "create_copilot_task"],
            [call[0] for call in actor.calls],
        )

    def test_recovery_between_prepare_and_invoke_supersedes_without_write(
        self,
    ) -> None:
        request = _request(self.item, self.failure_run)
        result = _result(request, JudgmentDecision.ASSIGN)
        reader = SequencedReader(
            [
                _refresh(self.item, self.failure_run),
                _refresh(
                    self.item,
                    self.failure_run,
                    recovery="passed",
                ),
            ]
        )
        actor = FakeActor()

        outcome = self._writer(reader, actor).execute(
            request,
            result,
            pass_id="pass-1",
            owner_id="owner-1",
        )

        self.assertEqual("superseded", outcome.status)
        self.assertEqual([], actor.calls)
        self.assertIs(ActionState.SUPERSEDED, self.store.list_actions()[0].state)

    def test_prepared_uninvoked_action_revalidates_and_can_resume(self) -> None:
        tracked = replace(self.item, issue_number=77)
        self.store.update_item(
            tracked,
            history_event="issue-bound",
            summary="The issue is bound.",
            detail={},
        )
        request = _request(tracked, self.failure_run, issue_number=77)
        result = _result(request, JudgmentDecision.ASSIGN)
        unavailable = _refresh(
            tracked,
            self.failure_run,
            issue=_issue(77),
            complete=False,
        )
        first_actor = FakeActor()
        first = self._writer(
            SequencedReader(
                [
                    _refresh(tracked, self.failure_run, issue=_issue(77)),
                    unavailable,
                ]
            ),
            first_actor,
        ).execute(
            request,
            result,
            pass_id="pass-1",
            owner_id="owner-1",
        )
        self.assertEqual("unavailable", first.status)
        self.assertEqual([], first_actor.calls)
        self.assertIs(ActionState.PREPARED, self.store.list_actions()[0].state)

        second_actor = FakeActor(task_ids=("task-resumed",))
        second = self._writer(
            FakeReader(
                lambda item, action: _refresh(
                    item,
                    self.failure_run,
                    issue=_issue(77),
                )
            ),
            second_actor,
        ).execute(
            request,
            result,
            pass_id="pass-2",
            owner_id="owner-2",
        )

        self.assertEqual("confirmed", second.status)
        self.assertEqual(1, len(second_actor.calls))
        self.assertEqual("task-resumed", self.store.list_items()[0].task_id)

    def test_fresh_job_identity_ignores_optional_log_enrichment(self) -> None:
        tracked = replace(self.item, issue_number=77)
        self.store.update_item(
            tracked,
            history_event="issue-bound",
            summary="The issue is bound.",
            detail={},
        )
        request = _request(tracked, self.failure_run, issue_number=77)
        fresh_run = replace(
            self.failure_run,
            jobs=tuple(
                replace(job, log_excerpt=None, log_truncated=False)
                for job in self.failure_run.jobs
            ),
        )
        actor = FakeActor()

        outcome = self._writer(
            FakeReader(
                lambda item, action: _refresh(
                    item,
                    fresh_run,
                    issue=_issue(77),
                )
            ),
            actor,
        ).execute(
            request,
            _result(request, JudgmentDecision.ASSIGN),
            pass_id="pass-1",
            owner_id="owner-1",
        )

        self.assertEqual("confirmed", outcome.status)
        self.assertEqual(1, len(actor.calls))

    def test_changed_execution_or_job_identity_prevents_write(self) -> None:
        tracked = replace(self.item, issue_number=77)
        self.store.update_item(
            tracked,
            history_event="issue-bound",
            summary="The issue is bound.",
            detail={},
        )
        request = _request(tracked, self.failure_run, issue_number=77)
        changed_attempt = replace(
            self.failure_run,
            attempt=2,
            jobs=tuple(
                replace(job, attempt=2)
                for job in self.failure_run.jobs
            ),
        )
        changed_job = replace(
            self.failure_run,
            jobs=(replace(self.failure_run.jobs[0], job_id=901),),
        )
        changed_status = replace(
            self.failure_run,
            jobs=(
                replace(
                    self.failure_run.jobs[0],
                    status="in_progress",
                ),
            ),
        )
        variants = (
            changed_attempt,
            replace(self.failure_run, head_sha="c" * 40),
            changed_job,
            changed_status,
        )
        for current_run in variants:
            with self.subTest(current_run=current_run):
                actor = FakeActor()
                outcome = self._writer(
                    FakeReader(
                        lambda item, action, run=current_run: _refresh(
                            item,
                            run,
                            issue=_issue(77),
                        )
                    ),
                    actor,
                ).execute(
                    request,
                    _result(request, JudgmentDecision.ASSIGN),
                    pass_id="pass-1",
                    owner_id="owner-1",
                )
                self.assertEqual("stale", outcome.status)
                self.assertEqual([], actor.calls)

    def test_initial_content_uses_only_the_validated_job_scope(self) -> None:
        build = _job(job_id=900, name="Build / Linux")
        ordinary_test = replace(
            _job(job_id=901, name="Tests / Linux"),
            log_excerpt="Ordinary test failed.",
            url=f"https://github.com/{REPOSITORY}/actions/runs/101/job/901",
        )
        failure_run = _run(jobs=(build, ordinary_test))
        item = self.store.upsert_failure(failure_run, NOW)
        item = replace(
            item,
            phase=ItemPhase.READY_FOR_ACTION,
            read_status="complete",
            last_judged_fingerprint=item.evidence_fingerprint,
        )
        self.store.update_item(
            item,
            history_event="judgment-ready",
            summary="A scoped judgment is ready.",
            detail={},
        )
        request = _request(item, failure_run)
        result = replace(
            _result(request, JudgmentDecision.ASSIGN),
            in_scope_job_ids=(build.job_id,),
        )
        actor = FakeActor()

        outcome = self._writer(
            FakeReader(
                lambda current, action: _refresh(
                    current,
                    failure_run,
                    issue=(
                        _issue(current.issue_number)
                        if current.issue_number
                        else None
                    ),
                )
            ),
            actor,
        ).execute(
            request,
            result,
            pass_id="pass-1",
            owner_id="owner-1",
        )

        self.assertEqual("confirmed", outcome.status)
        issue_body = str(actor.calls[0][3])
        self.assertEqual(
            (
                "[automated] **Summary**\n\n"
                "`CI` is failing on `main`.\n\n"
                "**Repro**\n\n"
                "- [Workflow run]"
                "(https://github.com/radical/aspire/actions/runs/101)\n"
                "- Commit: `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`\n\n"
                "**Expected**\n\n"
                "The workflow completes successfully.\n\n"
                "**Actual**\n\n"
                "- `Build / Linux`: error CS1002: ; expected "
                "([job](https://github.com/radical/aspire/actions/runs/101/job/900))"
                "\n\n<!-- ci-shepherd:workflow-repair "
                "repository=radical/aspire workflow-id=42 branch=main -->"
            ),
            issue_body,
        )
        self.assertEqual(
            (
                "[automated] Repair only the workflow failure tracked in "
                "radical/aspire#123. Fix the observed build failure and add "
                "focused regression coverage. Source run: "
                "https://github.com/radical/aspire/actions/runs/101. Failed "
                "jobs: Build / Linux "
                "(https://github.com/radical/aspire/actions/runs/101/job/900). "
                "Observed source SHA: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa. "
                "Work only in radical/aspire and target base `main`. Keep any "
                "pull request as a draft and never merge it. "
                "Refs radical/aspire#123."
            ),
            actor.calls[1][2],
        )

    def test_assignment_requires_a_nonempty_validated_job_scope(self) -> None:
        request = _request(self.item, self.failure_run)
        result = replace(
            _result(request, JudgmentDecision.ASSIGN),
            in_scope_job_ids=(),
        )
        actor = FakeActor()

        outcome = self._writer(
            FakeReader(lambda item, action: _refresh(item, self.failure_run)),
            actor,
        ).execute(
            request,
            result,
            pass_id="pass-1",
            owner_id="owner-1",
        )

        self.assertEqual("stale", outcome.status)
        self.assertEqual([], actor.calls)

    def test_prepared_issue_creation_revalidates_and_can_resume(self) -> None:
        request = _request(self.item, self.failure_run)
        result = _result(request, JudgmentDecision.ASSIGN)
        first = self._writer(
            SequencedReader(
                [
                    _refresh(self.item, self.failure_run),
                    _refresh(self.item, self.failure_run, complete=False),
                ]
            ),
            FakeActor(),
        ).execute(
            request,
            result,
            pass_id="pass-1",
            owner_id="owner-1",
        )
        self.assertEqual("unavailable", first.status)
        self.assertIs(ActionState.PREPARED, self.store.list_actions()[0].state)

        actor = FakeActor()
        second = self._writer(
            FakeReader(
                lambda item, action: _refresh(
                    item,
                    self.failure_run,
                    issue=(_issue(item.issue_number) if item.issue_number else None),
                )
            ),
            actor,
        ).execute(
            request,
            result,
            pass_id="pass-2",
            owner_id="owner-2",
        )

        self.assertEqual("confirmed", second.status)
        self.assertEqual(
            ["create_issue", "create_copilot_task"],
            [call[0] for call in actor.calls],
        )

    def test_unavailable_or_partial_reads_never_authorize_a_write(self) -> None:
        request = _request(self.item, self.failure_run)
        result = _result(request, JudgmentDecision.ASSIGN)
        cases = (
            _refresh(self.item, self.failure_run, complete=False),
            _refresh(
                self.item,
                self.failure_run,
                pre_write=False,
            ),
            _refresh(self.item, self.failure_run, recovery="pending"),
        )
        for refreshed in cases:
            with self.subTest(refreshed=refreshed):
                actor = FakeActor()
                outcome = self._writer(
                    FakeReader(lambda item, action: refreshed),
                    actor,
                ).execute(
                    request,
                    result,
                    pass_id="pass-1",
                    owner_id="owner-1",
                )
                self.assertEqual("unavailable", outcome.status)
                self.assertEqual([], actor.calls)

    def test_stale_request_identity_and_foreign_target_are_rejected(self) -> None:
        actor = FakeActor()
        reader = FakeReader(
            lambda item, action: _refresh(item, self.failure_run)
        )
        requests = (
            replace(
                _request(self.item, self.failure_run),
                episode=self.item.episode + 1,
            ),
            replace(
                _request(self.item, self.failure_run),
                evidence_fingerprint="fnv1a64:0000000000000000",
            ),
        )
        for request in requests:
            with self.subTest(request=request):
                result = _result(request, JudgmentDecision.ASSIGN)
                outcome = self._writer(reader, actor).execute(
                    request,
                    result,
                    pass_id="pass-1",
                    owner_id="owner-1",
                )
                self.assertEqual("stale", outcome.status)

        foreign_run = replace(
            self.failure_run,
            key=WorkflowKey("owner/repo", 42, BRANCH),
        )
        foreign_request = replace(
            _request(self.item, self.failure_run),
            repository="owner/repo",
            failure_run=foreign_run,
        )
        foreign = self._writer(reader, actor).execute(
            foreign_request,
            _result(foreign_request, JudgmentDecision.ASSIGN),
            pass_id="pass-1",
            owner_id="owner-1",
        )
        self.assertEqual("stale", foreign.status)
        self.assertEqual([], actor.calls)

    def test_changed_result_evidence_is_rejected_before_actor(self) -> None:
        request = _request(self.item, self.failure_run)
        result = replace(
            _result(request, JudgmentDecision.ASSIGN),
            evidence_ids=("run:999",),
        )
        actor = FakeActor()

        outcome = self._writer(
            FakeReader(lambda item, action: _refresh(item, self.failure_run)),
            actor,
        ).execute(
            request,
            result,
            pass_id="pass-1",
            owner_id="owner-1",
        )

        self.assertEqual("stale", outcome.status)
        self.assertEqual([], actor.calls)

    def test_late_old_episode_judgment_cannot_consume_new_episode(self) -> None:
        request = _request(self.item, self.failure_run)
        recovered = replace(
            self.item,
            phase=ItemPhase.RECOVERED,
            recovered_run_id=102,
            recovered_at=LATER,
        )
        self.store.update_item(
            recovered,
            history_event="recovered",
            summary="The first episode recovered.",
            detail={},
        )
        next_run = _run(run_id=103, head_sha="d" * 40)
        next_item = self.store.upsert_failure(
            next_run,
            "2026-09-17T20:03:00Z",
        )
        self.assertEqual(2, next_item.episode)
        actor = FakeActor()

        outcome = self._writer(
            FakeReader(lambda item, action: _refresh(item, next_run)),
            actor,
        ).execute(
            request,
            _result(request, JudgmentDecision.ASSIGN),
            pass_id="pass-2",
            owner_id="owner-2",
        )

        self.assertEqual("stale", outcome.status)
        self.assertEqual([], actor.calls)
        self.assertEqual(0, self.store.list_items()[0].followup_count)

    def test_unrelated_issue_binding_cannot_replace_request_target(self) -> None:
        changed = replace(self.item, issue_number=999)
        self.store.update_item(
            changed,
            history_event="external-issue-bound",
            summary="An unrelated actor changed the issue binding.",
            detail={},
        )
        request = _request(self.item, self.failure_run)
        actor = FakeActor()

        outcome = self._writer(
            FakeReader(
                lambda item, action: _refresh(
                    item,
                    self.failure_run,
                    issue=_issue(999),
                )
            ),
            actor,
        ).execute(
            request,
            _result(request, JudgmentDecision.ASSIGN),
            pass_id="pass-1",
            owner_id="owner-1",
        )

        self.assertEqual("stale", outcome.status)
        self.assertEqual([], actor.calls)

    def test_issue_appearing_after_prepare_supersedes_creation(self) -> None:
        class IssueChangingReader(FakeReader):
            def __init__(self) -> None:
                super().__init__(
                    lambda item, action: _refresh(item, self_failure_run)
                )
                self.searches = [
                    IssueSearchResult("zero", None, (), (), 1),
                    IssueSearchResult("one", _issue(91), (91,), (), 1),
                ]

            def find_tracking_issue(self, item) -> IssueSearchResult:
                self.issue_search_calls.append(item)
                return self.searches.pop(0)

        self_failure_run = self.failure_run
        request = _request(self.item, self.failure_run)
        actor = FakeActor()

        outcome = self._writer(
            IssueChangingReader(),
            actor,
        ).execute(
            request,
            _result(request, JudgmentDecision.ASSIGN),
            pass_id="pass-1",
            owner_id="owner-1",
        )

        self.assertEqual("stale", outcome.status)
        self.assertEqual([], actor.calls)
        self.assertIs(ActionState.SUPERSEDED, self.store.list_actions()[0].state)

    def test_issue_ownership_appearing_before_invocation_blocks_task(self) -> None:
        tracked = replace(self.item, issue_number=77)
        self.store.update_item(
            tracked,
            history_event="issue-bound",
            summary="The issue is bound.",
            detail={},
        )
        request = _request(tracked, self.failure_run, issue_number=77)
        clean = _issue(77)
        owned = replace(
            clean,
            assignees=("human-owner",),
            human_assigned=True,
        )
        actor = FakeActor()

        outcome = self._writer(
            SequencedReader(
                [
                    _refresh(
                        tracked,
                        self.failure_run,
                        issue=clean,
                    ),
                    _refresh(
                        tracked,
                        self.failure_run,
                        issue=owned,
                    ),
                ]
            ),
            actor,
        ).execute(
            request,
            _result(request, JudgmentDecision.ASSIGN),
            pass_id="pass-1",
            owner_id="owner-1",
        )

        self.assertEqual("superseded", outcome.status)
        self.assertEqual([], actor.calls)
        self.assertIs(ActionState.SUPERSEDED, self.store.list_actions()[0].state)

    def test_new_task_or_pull_binding_before_invocation_blocks_initial_task(
        self,
    ) -> None:
        variants = (
            {
                "task_id": "external-task",
                "task_state": TaskState.IN_PROGRESS,
            },
            {"pull_request_number": 202},
        )
        for changes in variants:
            with self.subTest(changes=changes):
                with TemporaryDirectory() as scratch:
                    store = WorkflowLoopStore(
                        Path(scratch) / "state",
                        repository=REPOSITORY,
                        branch=BRANCH,
                    )
                    store.initialize()
                    item = store.upsert_failure(self.failure_run, NOW)
                    item = replace(
                        item,
                        phase=ItemPhase.READY_FOR_ACTION,
                        read_status="complete",
                        last_judged_fingerprint=item.evidence_fingerprint,
                        issue_number=77,
                    )
                    store.update_item(
                        item,
                        history_event="issue-bound",
                        summary="The issue is bound.",
                        detail={},
                    )
                    request = _request(
                        item,
                        self.failure_run,
                        issue_number=77,
                    )
                    refresh_count = 0

                    def refresh(current, action):
                        nonlocal refresh_count
                        refresh_count += 1
                        if refresh_count == 2:
                            latest = store.list_items()[0]
                            store.update_item(
                                replace(latest, **changes),
                                history_event="external-binding",
                                summary="An external binding appeared.",
                                detail={},
                            )
                        return _refresh(
                            current,
                            self.failure_run,
                            issue=_issue(77),
                        )

                    actor = FakeActor()
                    writer = _writer_module().WorkflowWriter(
                        store=store,
                        reader=FakeReader(refresh),
                        actor=actor,
                        repository=REPOSITORY,
                        branch=BRANCH,
                        clock=lambda: datetime(
                            2026,
                            9,
                            17,
                            20,
                            2,
                            tzinfo=UTC,
                        ),
                        active_item_limit=2,
                        cloud_model=None,
                    )

                    outcome = writer.execute(
                        request,
                        _result(request, JudgmentDecision.ASSIGN),
                        pass_id="pass-1",
                        owner_id="owner-1",
                    )

                    self.assertEqual("stale", outcome.status)
                    self.assertEqual([], actor.calls)
                    self.assertIs(
                        ActionState.SUPERSEDED,
                        store.list_actions()[0].state,
                    )

    def test_follow_up_rejects_changed_pr_head_task_and_number(self) -> None:
        pull = _pull_request()
        tracked = replace(
            self.item,
            issue_number=77,
            task_id="task-initial",
            task_state=TaskState.IDLE,
            pull_request_number=pull.number,
            followup_count=0,
        )
        self.store.update_item(
            tracked,
            history_event="follow-up-ready",
            summary="The pull request needs follow-up.",
            detail={},
        )
        request = _request(
            tracked,
            self.failure_run,
            issue_number=77,
            task_id="task-initial",
            pull_request_number=pull.number,
            pull_request_head_sha=pull.head_sha,
            pull_request_head_ref=pull.head_ref,
            pull_request_base_ref=pull.base_ref,
            pull_request_observed_at=LATER,
        )
        variants = (
            (
                _task("other-task"),
                pull,
            ),
            (
                _task("task-initial"),
                replace(pull, number=202),
            ),
            (
                _task("task-initial"),
                replace(pull, head_sha="c" * 40),
            ),
            (
                _task("task-initial"),
                replace(pull, head_ref="copilot/other"),
            ),
        )
        for task, current_pull in variants:
            with self.subTest(task=task, pull=current_pull):
                actor = FakeActor(task_ids=("task-follow-up",))
                outcome = self._writer(
                    FakeReader(
                        lambda item, action: _refresh(
                            item,
                            self.failure_run,
                            issue=_issue(77),
                            task=task,
                            pull_request=current_pull,
                        )
                    ),
                    actor,
                ).execute(
                    request,
                    _result(request, JudgmentDecision.FOLLOW_UP),
                    pass_id="pass-1",
                    owner_id="owner-1",
                )
                self.assertEqual("stale", outcome.status)
                self.assertEqual([], actor.calls)

    def test_follow_up_waits_for_eligible_task_and_failed_complete_checks(
        self,
    ) -> None:
        pull = _pull_request()
        cases = (
            (
                _task("task-initial"),
                replace(pull, checks_state="green"),
                "superseded",
                ActionState.SUPERSEDED,
            ),
            (
                _task("task-initial"),
                replace(
                    pull,
                    checks_state="action_required",
                    checks_complete=False,
                ),
                "unavailable",
                ActionState.PREPARED,
            ),
            (
                replace(_task("task-initial"), state="in_progress"),
                pull,
                "unavailable",
                ActionState.PREPARED,
            ),
            (
                replace(_task("task-initial"), state="waiting_for_user"),
                pull,
                "unavailable",
                ActionState.PREPARED,
            ),
        )
        for task, current_pull, expected_status, expected_action_state in cases:
            with (
                self.subTest(task=task, pull=current_pull),
                TemporaryDirectory() as scratch,
            ):
                store = WorkflowLoopStore(
                    Path(scratch) / "state",
                    repository=REPOSITORY,
                    branch=BRANCH,
                )
                store.initialize()
                item = store.upsert_failure(self.failure_run, NOW)
                tracked = replace(
                    item,
                    phase=ItemPhase.READY_FOR_ACTION,
                    read_status="complete",
                    last_judged_fingerprint=item.evidence_fingerprint,
                    issue_number=77,
                    task_id="task-initial",
                    task_state=TaskState.IDLE,
                    pull_request_number=pull.number,
                )
                store.update_item(
                    tracked,
                    history_event="follow-up-ready",
                    summary="The pull request needs follow-up.",
                    detail={},
                )
                request = _request(
                    tracked,
                    self.failure_run,
                    issue_number=77,
                    task_id="task-initial",
                    pull_request_number=pull.number,
                    pull_request_head_sha=pull.head_sha,
                    pull_request_head_ref=pull.head_ref,
                    pull_request_base_ref=pull.base_ref,
                    pull_request_observed_at=LATER,
                )
                actor = FakeActor()
                writer = _writer_module().WorkflowWriter(
                    store=store,
                    reader=SequencedReader(
                        [
                            _refresh(
                                tracked,
                                self.failure_run,
                                issue=_issue(77),
                                task=_task("task-initial"),
                                pull_request=pull,
                            ),
                            _refresh(
                                tracked,
                                self.failure_run,
                                issue=_issue(77),
                                task=task,
                                pull_request=current_pull,
                            ),
                        ]
                    ),
                    actor=actor,
                    repository=REPOSITORY,
                    branch=BRANCH,
                    clock=lambda: datetime(
                        2026,
                        9,
                        17,
                        20,
                        2,
                        tzinfo=UTC,
                    ),
                    active_item_limit=2,
                    cloud_model=None,
                )

                outcome = writer.execute(
                    request,
                    _result(request, JudgmentDecision.FOLLOW_UP),
                    pass_id="pass-1",
                    owner_id="owner-1",
                )

                self.assertEqual(expected_status, outcome.status)
                self.assertEqual([], actor.calls)
                self.assertIs(
                    expected_action_state,
                    store.list_actions()[0].state,
                )

    def test_follow_up_content_keeps_only_the_validated_scope(self) -> None:
        build = _job(job_id=900, name="Build / Linux")
        ordinary_test = replace(
            _job(job_id=901, name="Tests / Linux"),
            log_excerpt="Ordinary test failed.",
            url=f"https://github.com/{REPOSITORY}/actions/runs/101/job/901",
        )
        failure_run = _run(jobs=(build, ordinary_test))
        item = self.store.upsert_failure(failure_run, NOW)
        pull = _pull_request()
        item = replace(
            item,
            phase=ItemPhase.READY_FOR_ACTION,
            read_status="complete",
            last_judged_fingerprint=item.evidence_fingerprint,
            issue_number=77,
            task_id="task-initial",
            task_state=TaskState.IDLE,
            pull_request_number=pull.number,
        )
        self.store.update_item(
            item,
            history_event="follow-up-ready",
            summary="A scoped follow-up is ready.",
            detail={},
        )
        request = _request(
            item,
            failure_run,
            issue_number=77,
            task_id="task-initial",
            pull_request_number=pull.number,
            pull_request_head_sha=pull.head_sha,
            pull_request_head_ref=pull.head_ref,
            pull_request_base_ref=pull.base_ref,
            pull_request_observed_at=LATER,
        )
        result = replace(
            _result(request, JudgmentDecision.FOLLOW_UP),
            in_scope_job_ids=(build.job_id,),
        )
        actor = FakeActor(task_ids=("task-follow-up",))

        outcome = self._writer(
            FakeReader(
                lambda current, action: _refresh(
                    current,
                    failure_run,
                    issue=_issue(77),
                    task=_task("task-initial"),
                    pull_request=pull,
                )
            ),
            actor,
        ).execute(
            request,
            result,
            pass_id="pass-1",
            owner_id="owner-1",
        )

        self.assertEqual("confirmed", outcome.status)
        self.assertEqual(
            (
                "[automated] Continue only the repair in PR radical/aspire#201. "
                "Fix the observed build failure and add focused regression "
                "coverage. Source run: "
                "https://github.com/radical/aspire/actions/runs/101. Failed "
                "jobs: Build / Linux "
                "(https://github.com/radical/aspire/actions/runs/101/job/900). "
                "Observed PR head SHA: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb. "
                "Work only in radical/aspire and retain base `main`. Keep the "
                "PR as a draft and never merge it. Refs radical/aspire#77."
            ),
            actor.calls[0][2],
        )

    def test_invoking_intent_is_visible_before_each_api_call(self) -> None:
        request = _request(self.item, self.failure_run)
        result = _result(request, JudgmentDecision.ASSIGN)
        observed: list[tuple[ActionState, dict[str, object]]] = []

        def inspect_state() -> None:
            observer = WorkflowLoopStore(
                self.state_directory,
                repository=REPOSITORY,
                branch=BRANCH,
            )
            invoking = tuple(
                action
                for action in observer.list_actions()
                if action.state is ActionState.INVOKING
            )
            self.assertEqual(1, len(invoking))
            action = invoking[0]
            observed.append((action.state, dict(action.payload)))

        actor = FakeActor(before_write=inspect_state)
        reader = FakeReader(
            lambda item, action: _refresh(
                item,
                self.failure_run,
                issue=(_issue(item.issue_number) if item.issue_number else None),
            )
        )

        outcome = self._writer(reader, actor).execute(
            request,
            result,
            pass_id="pass-visible",
            owner_id="owner-visible",
        )

        self.assertEqual("confirmed", outcome.status)
        self.assertEqual(2, len(observed))
        for state, payload in observed:
            self.assertIs(ActionState.INVOKING, state)
            self.assertIn("request", payload)
            self.assertIn("result", payload)
            self.assertIn("write", payload)

    def test_malformed_success_becomes_uncertain_and_is_not_retried(self) -> None:
        tracked = replace(self.item, issue_number=77)
        self.store.update_item(
            tracked,
            history_event="issue-bound",
            summary="The issue is bound.",
            detail={},
        )
        request = _request(tracked, self.failure_run, issue_number=77)
        result = _result(request, JudgmentDecision.ASSIGN)
        reader = FakeReader(
            lambda item, action: _refresh(
                item,
                self.failure_run,
                issue=_issue(77),
            )
        )
        actor = FakeActor(task_ids=(None,))
        writer = self._writer(reader, actor)

        first = writer.execute(
            request,
            result,
            pass_id="pass-1",
            owner_id="owner-1",
        )
        second = writer.execute(
            request,
            result,
            pass_id="pass-2",
            owner_id="owner-2",
        )

        self.assertEqual("uncertain", first.status)
        self.assertEqual("uncertain", second.status)
        self.assertEqual(1, len(actor.calls))
        self.assertIs(ActionState.UNCERTAIN, self.store.list_actions()[0].state)

    def test_uncertain_write_blocks_only_its_item(self) -> None:
        first_request = _request(self.item, self.failure_run)
        first_actor = FakeActor(error=RuntimeError("connection lost"))
        first = self._writer(
            FakeReader(lambda item, action: _refresh(item, self.failure_run)),
            first_actor,
        ).execute(
            first_request,
            _result(first_request, JudgmentDecision.ASSIGN),
            pass_id="pass-1",
            owner_id="owner-1",
        )
        self.assertEqual("uncertain", first.status)

        second_run = replace(
            _run(run_id=102),
            key=WorkflowKey(REPOSITORY, 43, BRANCH),
            workflow_path=".github/workflows/other.yml",
            workflow_name="Other",
        )
        second_item = self.store.upsert_failure(second_run, NOW)
        second_item = replace(
            second_item,
            phase=ItemPhase.READY_FOR_ACTION,
            read_status="complete",
            last_judged_fingerprint=second_item.evidence_fingerprint,
            issue_number=88,
        )
        self.store.update_item(
            second_item,
            history_event="judgment-ready",
            summary="Another judgment is ready.",
            detail={},
        )
        second_request = _request(
            second_item,
            second_run,
            issue_number=88,
        )
        second_actor = FakeActor(task_ids=("task-other",))
        writer = _writer_module().WorkflowWriter(
            store=self.store,
            reader=FakeReader(
                lambda item, action: _refresh(
                    item,
                    second_run,
                    issue=_issue(88),
                )
            ),
            actor=second_actor,
            repository=REPOSITORY,
            branch=BRANCH,
            clock=lambda: datetime(2026, 9, 17, 20, 2, tzinfo=UTC),
            active_item_limit=2,
            cloud_model=None,
        )

        second = writer.execute(
            second_request,
            _result(second_request, JudgmentDecision.ASSIGN),
            pass_id="pass-2",
            owner_id="owner-2",
        )

        self.assertEqual("confirmed", second.status)
        self.assertEqual(1, len(second_actor.calls))

    def test_non_write_decision_is_an_explicit_no_op(self) -> None:
        request = _request(self.item, self.failure_run)
        result = _result(request, JudgmentDecision.NO_ACTION)
        actor = FakeActor()

        outcome = self._writer(
            FakeReader(lambda item, action: _refresh(item, self.failure_run)),
            actor,
        ).execute(
            request,
            result,
            pass_id="pass-1",
            owner_id="owner-1",
        )

        self.assertEqual("no_op", outcome.status)
        self.assertEqual([], actor.calls)
    def test_interruption_propagates_and_durable_invoking_blocks_replay(self) -> None:
        tracked = replace(self.item, issue_number=77)
        self.store.update_item(
            tracked,
            history_event="issue-bound",
            summary="The issue is bound.",
            detail={},
        )
        request = _request(tracked, self.failure_run, issue_number=77)
        result = _result(request, JudgmentDecision.ASSIGN)
        reader = FakeReader(
            lambda item, action: _refresh(
                item,
                self.failure_run,
                issue=_issue(77),
            )
        )
        actor = FakeActor(error=KeyboardInterrupt())
        writer = self._writer(reader, actor)

        with self.assertRaises(KeyboardInterrupt):
            writer.execute(
                request,
                result,
                pass_id="pass-1",
                owner_id="owner-1",
            )

        self.assertIs(ActionState.INVOKING, self.store.list_actions()[0].state)
        replay = writer.execute(
            request,
            result,
            pass_id="pass-2",
            owner_id="owner-2",
        )
        self.assertEqual("uncertain", replay.status)
        self.assertEqual(1, len(actor.calls))

    def test_follow_up_limit_rejects_a_third_confirmed_request(self) -> None:
        pull = _pull_request()
        tracked = replace(
            self.item,
            issue_number=77,
            task_id="task-second-follow-up",
            task_state=TaskState.IDLE,
            pull_request_number=pull.number,
            followup_count=2,
        )
        self.store.update_item(
            tracked,
            history_event="follow-up-limit",
            summary="Two follow-ups are already confirmed.",
            detail={},
        )
        request = _request(
            tracked,
            self.failure_run,
            issue_number=77,
            task_id="task-second-follow-up",
            pull_request_number=pull.number,
            pull_request_head_sha=pull.head_sha,
            pull_request_head_ref=pull.head_ref,
            pull_request_base_ref=pull.base_ref,
            pull_request_observed_at=LATER,
            followup_count=2,
        )
        actor = FakeActor()

        outcome = self._writer(
            FakeReader(
                lambda item, action: _refresh(
                    item,
                    self.failure_run,
                    issue=_issue(77),
                    task=_task("task-second-follow-up"),
                    pull_request=pull,
                )
            ),
            actor,
        ).execute(
            request,
            _result(request, JudgmentDecision.FOLLOW_UP),
            pass_id="pass-1",
            owner_id="owner-1",
        )

        self.assertEqual("stale", outcome.status)
        self.assertEqual([], actor.calls)


if __name__ == "__main__":
    unittest.main()
