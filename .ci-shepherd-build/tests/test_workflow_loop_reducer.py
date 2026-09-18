from __future__ import annotations

from dataclasses import replace
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
    WorkflowItem,
    WorkflowKey,
    WorkState,
)
from ci_shepherd.workflow_loop.reader import (
    IssueObservation,
    ItemRefresh,
    PullRequestObservation,
    TaskBranchArtifact,
    TaskObservation,
)
from ci_shepherd.workflow_loop.reducer import (
    ConfirmedIssueCreation,
    ItemTransition,
    NextStep,
    reduce_item,
)


NOW = "2026-09-17T20:00:00Z"
LATER = "2026-09-17T20:05:00Z"
EVIDENCE = "fnv1a64:0123456789abcdef"
BUILD = JobKey("Build / Linux", ("ubuntu-latest",))
TEST = JobKey("Tests / Linux", ("ubuntu-latest",))


def _job(
    job_id: int,
    key: JobKey,
    *,
    run_id: int,
    attempt: int = 1,
    status: str = "completed",
    conclusion: str | None = "failure",
) -> JobObservation:
    return JobObservation(
        run_id=run_id,
        attempt=attempt,
        job_id=job_id,
        key=key,
        status=status,
        conclusion=conclusion,
        started_at=NOW,
        completed_at=LATER if status == "completed" else None,
        url=f"https://github.com/owner/repo/actions/runs/{run_id}/job/{job_id}",
        log_excerpt="diagnostic",
        log_truncated=False,
    )


def _run(
    run_id: int,
    run_number: int,
    *,
    attempt: int = 1,
    status: str = "completed",
    conclusion: str | None = "failure",
    event: str = "push",
    jobs_complete: bool = True,
    jobs: tuple[JobObservation, ...] | None = None,
) -> RunObservation:
    return RunObservation(
        key=WorkflowKey("owner/repo", 42, "main"),
        workflow_path=".github/workflows/ci.yml",
        workflow_name="CI",
        run_id=run_id,
        run_number=run_number,
        attempt=attempt,
        head_sha=f"{run_id:040x}",
        event=event,
        status=status,
        conclusion=conclusion,
        created_at=NOW,
        updated_at=LATER,
        url=f"https://github.com/owner/repo/actions/runs/{run_id}",
        jobs_complete=jobs_complete,
        jobs=jobs or (),
    )


def _failure_run(
    *,
    run_id: int = 101,
    run_number: int = 10,
    attempt: int = 1,
    event: str = "push",
) -> RunObservation:
    return _run(
        run_id,
        run_number,
        attempt=attempt,
        event=event,
        jobs=(
            _job(900, BUILD, run_id=run_id, attempt=attempt),
            _job(901, TEST, run_id=run_id, attempt=attempt),
        ),
    )


def _item(**changes: object) -> WorkflowItem:
    item = WorkflowItem(
        id=7,
        repository="owner/repo",
        workflow_id=42,
        workflow_path=".github/workflows/ci.yml",
        workflow_name="CI",
        branch="main",
        episode=2,
        phase=ItemPhase.OBSERVING_FAILURE,
        first_failure_seen_at=NOW,
        last_checked_at=NOW,
        last_progressed_at=NOW,
        read_status="complete",
        failure_run_id=101,
        failure_attempt=1,
        failed_jobs=(BUILD, TEST),
        evidence_fingerprint=EVIDENCE,
        last_judged_fingerprint=None,
        wait_run_id=None,
        wait_reason=None,
        issue_number=None,
        task_id=None,
        task_state=None,
        pull_request_number=None,
        external_owner=None,
        followup_count=0,
        assignment_requested_at=None,
        assignment_confirmed_at=None,
        recovered_run_id=None,
        recovered_at=None,
        latest_action=None,
        latest_error=None,
    )
    return replace(item, **changes)


def _issue(
    number: int = 17,
    *,
    copilot_assigned: bool = False,
    human_assigned: bool = False,
) -> IssueObservation:
    return IssueObservation(
        number=number,
        url=f"https://github.com/owner/repo/issues/{number}",
        title="CI failure",
        marker="ci-shepherd",
        assignees=("someone",) if human_assigned else (),
        copilot_assigned=copilot_assigned,
        human_assigned=human_assigned,
    )


def _task(
    state: str,
    *,
    task_id: str = "task-123",
    pull_request_number: int = 23,
) -> TaskObservation:
    return TaskObservation(
        task_id=task_id,
        state=state,
        url=f"https://github.com/owner/repo/agent-tasks/{task_id}",
        repository_id=123,
        updated_at=LATER,
        session_count=1,
        pull_request_database_ids=(),
        branch_artifacts=(
            TaskBranchArtifact(
                head_ref="copilot/fix-build",
                base_ref="main",
            ),
        ),
        outcome=None,
        explanation=None,
        explanation_available=False,
    )


def _pull_request(
    *,
    number: int = 23,
    state: str = "open",
    merged: bool = False,
    draft: bool = False,
    head_sha: str = "f" * 40,
    head_ref: str = "copilot/fix-build",
    base_ref: str = "main",
    checks_state: str = "green",
    checks_complete: bool = True,
    review_decision: str = "approved",
    review_complete: bool = True,
    complete: bool = True,
) -> PullRequestObservation:
    return PullRequestObservation(
        number=number,
        state=state,
        merged=merged,
        draft=draft,
        url=f"https://github.com/owner/repo/pull/{number}",
        head_repository="owner/repo",
        head_ref=head_ref,
        head_sha=head_sha,
        base_repository="owner/repo",
        base_ref=base_ref,
        checks_state=checks_state,
        checks_complete=checks_complete,
        review_decision=review_decision,
        review_complete=review_complete,
        complete=complete,
        incomplete_reasons=() if complete else ("checks",),
    )


def _refresh(
    *,
    item: WorkflowItem | None = None,
    runs: tuple[RunObservation, ...] | None = None,
    failure_run: RunObservation | None = None,
    wait_run: RunObservation | None = None,
    recovery: str = "failed",
    recovery_run: RunObservation | None = None,
    issue: IssueObservation | None = None,
    task: TaskObservation | None = None,
    pull_request: PullRequestObservation | None = None,
    pre_write: bool = True,
    complete: bool = True,
) -> ItemRefresh:
    current = item or _item()
    failure = failure_run or _failure_run()
    return ItemRefresh(
        item_id=current.id,
        observed_at=LATER,
        runs=runs or (failure,),
        failure_run=failure,
        wait_run=wait_run,
        recovery=recovery,
        recovery_run=recovery_run,
        issue=issue,
        task=task,
        pull_request=pull_request,
        pre_write=pre_write,
        complete=complete,
        errors=(),
        request_count=1,
    )


def _request(
    item: WorkflowItem,
    *,
    round: int = 0,
    issue_number: int | None = None,
    task_id: str | None = None,
    pull_request: PullRequestObservation | None = None,
) -> JudgmentRequest:
    failure = _failure_run()
    follow_up = round > 0
    pull = pull_request if follow_up else None
    return JudgmentRequest(
        worker_id=f"worker-{round}",
        session_id=f"session-{round}",
        item_id=item.id,
        episode=item.episode,
        evidence_fingerprint=item.evidence_fingerprint,
        round=round,
        repository=item.repository,
        branch=item.branch,
        workflow_id=item.workflow_id,
        workflow_path=item.workflow_path,
        failure_run=failure,
        failed_jobs=failure.jobs,
        evidence_ids=("run:101", "job:101:900", "job:101:901"),
        issue_number=issue_number,
        task_id=task_id,
        pull_request_number=pull.number if pull is not None else None,
        pull_request_head_sha=pull.head_sha if pull is not None else None,
        pull_request_head_ref=pull.head_ref if pull is not None else None,
        pull_request_base_ref=pull.base_ref if pull is not None else None,
        pull_request_observed_at=LATER if pull is not None else None,
        followup_count=item.followup_count,
        prompt="Classify the failure.",
    )


def _judgment(
    item: WorkflowItem,
    *,
    decision: JudgmentDecision = JudgmentDecision.ASSIGN,
    in_scope_job_ids: tuple[int, ...] = (900,),
) -> JudgmentResult:
    return JudgmentResult(
        schema_version=1,
        item_id=item.id,
        episode=item.episode,
        evidence_fingerprint=item.evidence_fingerprint,
        decision=decision,
        summary="Build failed before ordinary tests could run.",
        evidence_ids=("run:101", "job:101:900"),
        in_scope_job_ids=in_scope_job_ids,
        copilot_request=(
            "Fix the build failure."
            if decision
            in {JudgmentDecision.ASSIGN, JudgmentDecision.FOLLOW_UP}
            else None
        ),
    )


class WorkflowLoopReducerTests(unittest.TestCase):
    def assert_transition(
        self,
        transition: ItemTransition,
        *,
        phase: ItemPhase,
        step: NextStep,
        action: ActionKind | None = None,
    ) -> None:
        self.assertIs(phase, transition.item.phase)
        self.assertIs(step, transition.next_step)
        self.assertIs(action, transition.action_kind)
        self.assertTrue(transition.history_event)
        self.assertTrue(transition.summary)

    def test_initial_round_zero_selects_only_judged_scope(self) -> None:
        item = _item(issue_number=17)
        transition = reduce_item(
            item,
            _refresh(item=item, issue=_issue()),
            request=_request(item, issue_number=17),
            judgment=_judgment(item),
            now=LATER,
        )

        self.assert_transition(
            transition,
            phase=ItemPhase.READY_FOR_ACTION,
            step=NextStep.PREPARE_ACTION,
            action=ActionKind.ASSIGN_COPILOT,
        )
        self.assertEqual((BUILD,), transition.item.failed_jobs)
        self.assertEqual(EVIDENCE, transition.item.last_judged_fingerprint)
        self.assertEqual(0, transition.judgment_round)

    def test_initial_round_zero_mixed_test_scope_does_not_expand_targets(
        self,
    ) -> None:
        item = _item(issue_number=17)
        transition = reduce_item(
            item,
            _refresh(item=item, issue=_issue()),
            request=_request(item, issue_number=17),
            judgment=_judgment(
                item,
                decision=JudgmentDecision.DEFER_ORDINARY_TEST,
                in_scope_job_ids=(),
            ),
            now=LATER,
        )

        self.assert_transition(
            transition,
            phase=ItemPhase.WAITING_FOR_HUMAN,
            step=NextStep.WAIT_FOR_HUMAN,
        )
        self.assertEqual((BUILD, TEST), transition.item.failed_jobs)
        self.assertIsNone(transition.action_kind)

    def test_completed_failure_is_eligible_while_newer_run_is_running(self) -> None:
        item = _item()
        a = _failure_run()
        b_running = _run(102, 11, status="in_progress", conclusion=None)
        transition = reduce_item(
            item,
            _refresh(item=item, runs=(a, b_running), recovery="pending"),
            now=LATER,
        )

        self.assert_transition(
            transition,
            phase=ItemPhase.JUDGMENT_QUEUED,
            step=NextStep.QUEUE_JUDGMENT,
        )
        self.assertEqual(0, transition.judgment_round)
        self.assertIsNone(transition.item.wait_run_id)
        self.assertIsNone(transition.item.wait_reason)

    def test_identical_semantic_evidence_does_not_queue_second_judgment(self) -> None:
        item = _item(
            last_judged_fingerprint=EVIDENCE,
            last_progressed_at=NOW,
        )

        transition = reduce_item(
            item,
            _refresh(item=item),
            now=LATER,
        )

        self.assert_transition(
            transition,
            phase=ItemPhase.OBSERVING_FAILURE,
            step=NextStep.WAIT_FOR_CHANGE,
        )
        self.assertEqual(LATER, transition.item.last_checked_at)
        self.assertEqual(NOW, transition.item.last_progressed_at)
        self.assertEqual(EVIDENCE, transition.item.evidence_fingerprint)
        self.assertEqual(EVIDENCE, transition.item.last_judged_fingerprint)
        self.assertEqual((BUILD, TEST), transition.item.failed_jobs)

    def test_previous_run_wait_is_cleared_when_failure_becomes_eligible(self) -> None:
        item = _item(
            phase=ItemPhase.WAITING_FOR_RUN,
            wait_run_id=102,
            wait_reason="newer-run",
        )
        pending = _run(102, 11, status="in_progress", conclusion=None)

        transition = reduce_item(
            item,
            _refresh(item=item, runs=(_failure_run(), pending)),
            now=LATER,
        )

        self.assert_transition(
            transition,
            phase=ItemPhase.JUDGMENT_QUEUED,
            step=NextStep.QUEUE_JUDGMENT,
        )
        self.assertIsNone(transition.item.wait_run_id)
        self.assertIsNone(transition.item.wait_reason)
        self.assertEqual(0, transition.judgment_round)

    def test_recovery_requires_complete_positive_in_scope_job_proof(self) -> None:
        item = _item(
            failed_jobs=(BUILD,),
            last_judged_fingerprint=EVIDENCE,
        )
        cases = {
            "passed": (
                _run(
                    102,
                    11,
                    conclusion="success",
                    jobs=(
                        _job(
                            902,
                            JobKey(BUILD.name, ("new-runner",)),
                            run_id=102,
                            conclusion="success",
                        ),
                    ),
                ),
                "passed",
                ItemPhase.RECOVERED,
            ),
            "skipped": (
                _run(
                    102,
                    11,
                    conclusion="success",
                    jobs=(
                        _job(
                            902,
                            BUILD,
                            run_id=102,
                            conclusion="skipped",
                        ),
                    ),
                ),
                "passed",
                ItemPhase.OBSERVING_FAILURE,
            ),
            "missing": (
                _run(102, 11, conclusion="success"),
                "passed",
                ItemPhase.OBSERVING_FAILURE,
            ),
            "incomplete": (
                _run(
                    102,
                    11,
                    status="in_progress",
                    conclusion=None,
                    jobs_complete=False,
                    jobs=(
                        _job(
                            902,
                            BUILD,
                            run_id=102,
                            status="in_progress",
                            conclusion=None,
                        ),
                    ),
                ),
                "pending",
                ItemPhase.OBSERVING_FAILURE,
            ),
            "pr-run": (
                _run(
                    102,
                    11,
                    event="pull_request",
                    conclusion="success",
                    jobs=(
                        _job(
                            902,
                            BUILD,
                            run_id=102,
                            conclusion="success",
                        ),
                    ),
                ),
                "passed",
                ItemPhase.OBSERVING_FAILURE,
            ),
        }
        for name, (candidate, recovery, expected_phase) in cases.items():
            with self.subTest(name=name):
                transition = reduce_item(
                    item,
                    _refresh(
                        item=item,
                        runs=(_failure_run(), candidate),
                        recovery=recovery,
                        recovery_run=candidate if recovery == "passed" else None,
                    ),
                    now=LATER,
                )
                self.assertIs(expected_phase, transition.item.phase)

    def test_later_attempt_can_recover_but_older_retry_cannot_mask_failure(
        self,
    ) -> None:
        item = _item(
            failed_jobs=(BUILD,),
            last_judged_fingerprint=EVIDENCE,
        )
        later_attempt = _run(
            101,
            10,
            attempt=2,
            conclusion="success",
            jobs=(
                _job(
                    902,
                    BUILD,
                    run_id=101,
                    attempt=2,
                    conclusion="success",
                ),
            ),
        )
        recovered = reduce_item(
            item,
            _refresh(
                item=item,
                runs=(_failure_run(), later_attempt),
                recovery="passed",
                recovery_run=later_attempt,
            ),
            now=LATER,
        )
        self.assertIs(ItemPhase.RECOVERED, recovered.item.phase)

        older_retry = _run(
            99,
            9,
            attempt=7,
            conclusion="success",
            jobs=(
                _job(
                    903,
                    BUILD,
                    run_id=99,
                    attempt=7,
                    conclusion="success",
                ),
            ),
        )
        not_recovered = reduce_item(
            item,
            _refresh(
                item=item,
                runs=(older_retry, _failure_run()),
                recovery="passed",
                recovery_run=older_retry,
            ),
            now=LATER,
        )
        self.assertIsNot(ItemPhase.RECOVERED, not_recovered.item.phase)

    def test_unjudged_item_cannot_recover_from_overall_green_alone(self) -> None:
        item = _item(failed_jobs=(BUILD,))
        green = _run(
            102,
            11,
            conclusion="success",
            jobs=(
                _job(902, BUILD, run_id=102, conclusion="success"),
            ),
        )

        transition = reduce_item(
            item,
            _refresh(
                item=item,
                runs=(_failure_run(), green),
                recovery="passed",
                recovery_run=green,
            ),
            now=LATER,
        )

        self.assertIsNot(ItemPhase.RECOVERED, transition.item.phase)

    def test_newer_failure_is_not_masked_by_older_success(self) -> None:
        item = _item(
            failed_jobs=(BUILD,),
            last_judged_fingerprint=EVIDENCE,
        )
        success = _run(
            102,
            11,
            conclusion="success",
            jobs=(
                _job(902, BUILD, run_id=102, conclusion="success"),
            ),
        )
        newer_failure = _run(
            103,
            12,
            jobs=(_job(903, BUILD, run_id=103),),
        )

        transition = reduce_item(
            item,
            _refresh(
                item=item,
                runs=(_failure_run(), success, newer_failure),
                recovery="passed",
                recovery_run=success,
            ),
            now=LATER,
        )

        self.assertIsNot(ItemPhase.RECOVERED, transition.item.phase)

    def test_recovery_wins_over_live_work_and_late_judgment(self) -> None:
        item = _item(
            failed_jobs=(BUILD,),
            last_judged_fingerprint=EVIDENCE,
            task_id="task-123",
            task_state=TaskState.IN_PROGRESS,
        )
        success = _run(
            102,
            11,
            conclusion="success",
            jobs=(
                _job(902, BUILD, run_id=102, conclusion="success"),
            ),
        )
        transition = reduce_item(
            item,
            _refresh(
                item=item,
                runs=(_failure_run(), success),
                recovery="passed",
                recovery_run=success,
                task=_task("in_progress"),
            ),
            request=_request(item),
            judgment=_judgment(item),
            worker_state=WorkState.RUNNING,
            now=LATER,
        )

        self.assert_transition(
            transition,
            phase=ItemPhase.RECOVERED,
            step=NextStep.WAIT_FOR_OWNED_WORK,
        )
        self.assertEqual("task-123", transition.item.task_id)
        self.assertIs(TaskState.IN_PROGRESS, transition.item.task_state)
        self.assertIsNone(transition.action_kind)

    def test_recovery_is_persisted_before_uncertain_action_attention(self) -> None:
        item = _item(
            failed_jobs=(BUILD,),
            last_judged_fingerprint=EVIDENCE,
        )
        success = _run(
            102,
            11,
            conclusion="success",
            jobs=(
                _job(902, BUILD, run_id=102, conclusion="success"),
            ),
        )

        transition = reduce_item(
            item,
            _refresh(
                item=item,
                runs=(_failure_run(), success),
                recovery="passed",
                recovery_run=success,
            ),
            action_state=ActionState.UNCERTAIN,
            now=LATER,
        )

        self.assert_transition(
            transition,
            phase=ItemPhase.RECOVERED,
            step=NextStep.NEEDS_ATTENTION,
        )
        self.assertEqual(102, transition.item.recovered_run_id)
        self.assertIn("uncertain", transition.item.latest_error or "")

    def test_recovery_is_persisted_before_failed_worker_attention(self) -> None:
        item = _item(
            failed_jobs=(BUILD,),
            last_judged_fingerprint=EVIDENCE,
        )
        success = _run(
            102,
            11,
            conclusion="success",
            jobs=(
                _job(902, BUILD, run_id=102, conclusion="success"),
            ),
        )

        transition = reduce_item(
            item,
            _refresh(
                item=item,
                runs=(_failure_run(), success),
                recovery="passed",
                recovery_run=success,
            ),
            worker_state=WorkState.FAILED,
            now=LATER,
        )

        self.assert_transition(
            transition,
            phase=ItemPhase.RECOVERED,
            step=NextStep.NEEDS_ATTENTION,
        )
        self.assertEqual(102, transition.item.recovered_run_id)

    def test_external_repairs_are_observed_without_adoption(self) -> None:
        item = _item(issue_number=17)

        transition = reduce_item(
            item,
            _refresh(
                item=item,
                issue=_issue(human_assigned=True),
            ),
            now=LATER,
        )

        self.assert_transition(
            transition,
            phase=ItemPhase.WAITING_FOR_HUMAN,
            step=NextStep.WAIT_FOR_HUMAN,
        )
        self.assertEqual("human", transition.item.external_owner)

        owned = replace(
            item,
            task_id="task-123",
            task_state=TaskState.IDLE,
        )
        takeover = reduce_item(
            owned,
            _refresh(
                item=owned,
                issue=_issue(human_assigned=True),
                task=_task("idle"),
            ),
            now=LATER,
        )
        self.assert_transition(
            takeover,
            phase=ItemPhase.WAITING_FOR_HUMAN,
            step=NextStep.WAIT_FOR_HUMAN,
        )
        self.assertEqual("task-123", takeover.item.task_id)

    def test_owned_task_states_choose_wait_or_human_handoff(self) -> None:
        item = _item(task_id="task-123")
        cases = {
            "unknown": (None, False, NextStep.WAIT_FOR_OWNED_WORK),
            "queued": (_task("queued"), True, NextStep.WAIT_FOR_OWNED_WORK),
            "running": (
                _task("in_progress"),
                True,
                NextStep.WAIT_FOR_OWNED_WORK,
            ),
            "human": (
                _task("waiting_for_user"),
                True,
                NextStep.WAIT_FOR_HUMAN,
            ),
        }
        for name, (task, complete, expected) in cases.items():
            with self.subTest(name=name):
                transition = reduce_item(
                    item,
                    _refresh(
                        item=item,
                        task=task,
                        complete=complete,
                    ),
                    now=LATER,
                )
                self.assertIs(expected, transition.next_step)

    def test_active_or_unknown_owned_task_blocks_ready_judgment(self) -> None:
        for task, complete in (
            (None, False),
            (_task("queued"), True),
            (_task("in_progress"), True),
        ):
            with self.subTest(task=task):
                item = _item(
                    issue_number=17,
                    task_id="task-123",
                )
                transition = reduce_item(
                    item,
                    _refresh(
                        item=item,
                        issue=_issue(),
                        task=task,
                        complete=complete,
                    ),
                    request=_request(item, issue_number=17),
                    judgment=_judgment(item),
                    capacity_available=True,
                    now=LATER,
                )
                self.assert_transition(
                    transition,
                    phase=(
                        ItemPhase.COPILOT_ACTIVE
                        if task is not None
                        else item.phase
                    ),
                    step=NextStep.WAIT_FOR_OWNED_WORK,
                )
                self.assertTrue(transition.retain_judgment)
                self.assertIsNone(transition.action_kind)

    def test_pr_states_do_not_claim_main_workflow_recovery(self) -> None:
        item = _item(
            issue_number=17,
            task_id="task-123",
            task_state=TaskState.COMPLETED,
            pull_request_number=23,
        )
        cases = {
            "pending": (
                _pull_request(
                    checks_state="pending",
                    checks_complete=False,
                    complete=False,
                ),
                NextStep.WAIT_FOR_PR,
            ),
            "action-required": (
                _pull_request(checks_state="action_required"),
                NextStep.WAIT_FOR_HUMAN,
            ),
            "green": (_pull_request(), NextStep.WAIT_FOR_CI),
            "merged": (
                _pull_request(state="closed", merged=True),
                NextStep.WAIT_FOR_CI,
            ),
            "closed": (
                _pull_request(state="closed", merged=False),
                NextStep.NEEDS_ATTENTION,
            ),
        }
        for name, (pull, expected) in cases.items():
            with self.subTest(name=name):
                transition = reduce_item(
                    item,
                    _refresh(
                        item=item,
                        issue=_issue(),
                        task=_task("completed"),
                        pull_request=pull,
                    ),
                    now=LATER,
                )
                self.assertIs(expected, transition.next_step)
                self.assertIsNot(ItemPhase.RECOVERED, transition.item.phase)

    def test_failed_pr_queues_bounded_followup_rounds(self) -> None:
        failed_pr = _pull_request(checks_state="red")
        for count, expected_step, expected_round in (
            (0, NextStep.QUEUE_JUDGMENT, 1),
            (1, NextStep.QUEUE_JUDGMENT, 2),
            (2, NextStep.WAIT_FOR_HUMAN, None),
        ):
            with self.subTest(count=count):
                item = _item(
                    issue_number=17,
                    task_id="task-123",
                    task_state=TaskState.COMPLETED,
                    pull_request_number=23,
                    followup_count=count,
                )
                transition = reduce_item(
                    item,
                    _refresh(
                        item=item,
                        issue=_issue(),
                        task=_task("completed"),
                        pull_request=failed_pr,
                    ),
                    now=LATER,
                )
                self.assertIs(expected_step, transition.next_step)
                self.assertEqual(expected_round, transition.judgment_round)

    def test_applied_initial_judgment_does_not_mask_finished_task(self) -> None:
        pull = _pull_request(checks_state="red")
        item = _item(
            issue_number=17,
            task_id="task-123",
            task_state=TaskState.COMPLETED,
            pull_request_number=pull.number,
            assignment_confirmed_at=NOW,
        )

        transition = reduce_item(
            item,
            _refresh(
                item=item,
                issue=_issue(),
                task=_task("completed"),
                pull_request=pull,
                pre_write=False,
            ),
            request=_request(_item(), issue_number=17),
            judgment=_judgment(_item()),
            now=LATER,
        )

        self.assert_transition(
            transition,
            phase=ItemPhase.JUDGMENT_QUEUED,
            step=NextStep.QUEUE_JUDGMENT,
        )
        self.assertEqual(1, transition.judgment_round)

    def test_applied_followup_judgment_does_not_mask_green_pr(self) -> None:
        pull = _pull_request(checks_state="green")
        item = _item(
            issue_number=17,
            task_id="task-new",
            task_state=TaskState.COMPLETED,
            pull_request_number=pull.number,
            followup_count=1,
            assignment_confirmed_at=NOW,
        )
        original = replace(item, task_id="task-original", followup_count=0)

        transition = reduce_item(
            item,
            _refresh(
                item=item,
                issue=_issue(),
                task=_task("completed", task_id="task-new"),
                pull_request=pull,
                pre_write=False,
            ),
            request=_request(
                original,
                round=1,
                issue_number=17,
                task_id="task-original",
                pull_request=pull,
            ),
            judgment=_judgment(
                original,
                decision=JudgmentDecision.FOLLOW_UP,
            ),
            now=LATER,
        )

        self.assert_transition(
            transition,
            phase=ItemPhase.WAITING_FOR_CI,
            step=NextStep.WAIT_FOR_CI,
        )

    def test_followup_result_requires_exact_target_and_round(self) -> None:
        pull = _pull_request(checks_state="red")
        item = _item(
            issue_number=17,
            task_id="task-123",
            task_state=TaskState.COMPLETED,
            pull_request_number=23,
            followup_count=0,
        )
        request = _request(
            item,
            round=1,
            issue_number=17,
            task_id="task-123",
            pull_request=pull,
        )
        request = replace(
            request,
            pull_request_observed_at=NOW,
        )
        judgment = _judgment(
            item,
            decision=JudgmentDecision.FOLLOW_UP,
        )
        accepted = reduce_item(
            item,
            _refresh(
                item=item,
                issue=_issue(),
                task=_task("completed"),
                pull_request=pull,
            ),
            request=request,
            judgment=judgment,
            now=LATER,
        )
        self.assert_transition(
            accepted,
            phase=ItemPhase.READY_FOR_ACTION,
            step=NextStep.PREPARE_ACTION,
            action=ActionKind.FOLLOW_UP,
        )

        stale_cases = (
            (
                replace(request, task_id="task-other"),
                item,
                _issue(),
                _task("completed"),
                pull,
            ),
            (
                replace(request, pull_request_number=201),
                item,
                _issue(),
                _task("completed"),
                pull,
            ),
            (
                replace(request, pull_request_head_sha="e" * 40),
                item,
                _issue(),
                _task("completed"),
                pull,
            ),
            (
                replace(request, pull_request_head_ref="copilot/other"),
                item,
                _issue(),
                _task("completed"),
                pull,
            ),
            (
                replace(request, pull_request_base_ref="release"),
                item,
                _issue(),
                _task("completed"),
                pull,
            ),
            (replace(request, round=2), item, _issue(), _task("completed"), pull),
            (
                request,
                replace(item, issue_number=18),
                _issue(18),
                _task("completed"),
                pull,
            ),
            (
                request,
                item,
                _issue(),
                _task("completed", task_id="task-other"),
                pull,
            ),
            (
                request,
                item,
                _issue(),
                _task("completed"),
                _pull_request(number=202, checks_state="red"),
            ),
        )
        for stale, current, issue, task, current_pull in stale_cases:
            with self.subTest(stale=stale, current=current):
                transition = reduce_item(
                    current,
                    _refresh(
                        item=current,
                        issue=issue,
                        task=task,
                        pull_request=current_pull,
                    ),
                    request=stale,
                    judgment=judgment,
                    now=LATER,
                )
                self.assertIs(NextStep.NEEDS_ATTENTION, transition.next_step)

    def test_capacity_denial_retains_fresh_result(self) -> None:
        item = _item(issue_number=17)
        transition = reduce_item(
            item,
            _refresh(item=item, issue=_issue()),
            request=_request(item, issue_number=17),
            judgment=_judgment(item),
            capacity_available=False,
            now=LATER,
        )

        self.assert_transition(
            transition,
            phase=ItemPhase.READY_FOR_ACTION,
            step=NextStep.WAIT_FOR_CAPACITY,
            action=ActionKind.ASSIGN_COPILOT,
        )
        self.assertTrue(transition.retain_judgment)

    def test_non_mutating_judgments_do_not_require_pre_write_refresh(self) -> None:
        cases = (
            (
                JudgmentDecision.DEFER_ORDINARY_TEST,
                ItemPhase.WAITING_FOR_HUMAN,
                NextStep.WAIT_FOR_HUMAN,
            ),
            (
                JudgmentDecision.OBSERVE_EXTERNAL,
                ItemPhase.OBSERVING_EXTERNAL_REPAIR,
                NextStep.OBSERVE_EXTERNAL,
            ),
            (
                JudgmentDecision.NEEDS_ATTENTION,
                ItemPhase.NEEDS_ATTENTION,
                NextStep.NEEDS_ATTENTION,
            ),
            (
                JudgmentDecision.NO_ACTION,
                ItemPhase.OBSERVING_FAILURE,
                NextStep.WAIT_FOR_CHANGE,
            ),
        )
        for decision, expected_phase, expected_step in cases:
            with self.subTest(decision=decision):
                item = _item()
                transition = reduce_item(
                    item,
                    _refresh(item=item, pre_write=False, complete=True),
                    request=_request(item),
                    judgment=_judgment(
                        item,
                        decision=decision,
                        in_scope_job_ids=(),
                    ),
                    now=LATER,
                )

                self.assert_transition(
                    transition,
                    phase=expected_phase,
                    step=expected_step,
                )
                self.assertFalse(transition.retain_judgment)

    def test_mutating_judgments_require_pre_write_refresh(self) -> None:
        initial = _item(issue_number=17)
        pull = _pull_request(checks_state="red")
        follow_up = _item(
            issue_number=17,
            task_id="task-123",
            task_state=TaskState.COMPLETED,
            pull_request_number=pull.number,
        )
        cases = (
            (
                initial,
                _request(initial, issue_number=17),
                _judgment(initial),
                _refresh(
                    item=initial,
                    issue=_issue(),
                    pre_write=False,
                ),
            ),
            (
                follow_up,
                _request(
                    follow_up,
                    round=1,
                    issue_number=17,
                    task_id="task-123",
                    pull_request=pull,
                ),
                _judgment(
                    follow_up,
                    decision=JudgmentDecision.FOLLOW_UP,
                ),
                _refresh(
                    item=follow_up,
                    issue=_issue(),
                    task=_task("completed"),
                    pull_request=pull,
                    pre_write=False,
                ),
            ),
        )
        for item, request, judgment, refresh in cases:
            with self.subTest(decision=judgment.decision):
                transition = reduce_item(
                    item,
                    refresh,
                    request=request,
                    judgment=judgment,
                    now=LATER,
                )

                self.assert_transition(
                    transition,
                    phase=item.phase,
                    step=NextStep.WAIT_FOR_READ,
                )
                self.assertTrue(transition.retain_judgment)
                self.assertIsNone(transition.action_kind)

    def test_capacity_result_is_rechecked_for_freshness_before_action(self) -> None:
        item = _item(issue_number=17)
        request = _request(item, issue_number=17)
        judgment = _judgment(item)
        waiting = reduce_item(
            item,
            _refresh(item=item, issue=_issue()),
            request=request,
            judgment=judgment,
            capacity_available=False,
            now=LATER,
        )
        changed = replace(
            waiting.item,
            evidence_fingerprint="fnv1a64:ffffffffffffffff",
        )

        stale = reduce_item(
            changed,
            _refresh(item=changed, issue=_issue()),
            request=request,
            judgment=judgment,
            capacity_available=True,
            now="2026-09-17T20:10:00Z",
        )

        self.assert_transition(
            stale,
            phase=ItemPhase.NEEDS_ATTENTION,
            step=NextStep.NEEDS_ATTENTION,
        )
        self.assertIsNone(stale.action_kind)

    def test_capacity_result_waits_when_fresh_read_becomes_unavailable(self) -> None:
        item = _item(issue_number=17)
        request = _request(item, issue_number=17)
        judgment = _judgment(item)
        waiting = reduce_item(
            item,
            _refresh(item=item, issue=_issue()),
            request=request,
            judgment=judgment,
            capacity_available=False,
            now=LATER,
        )

        unavailable = reduce_item(
            waiting.item,
            _refresh(
                item=waiting.item,
                issue=None,
                pre_write=False,
                complete=False,
            ),
            request=request,
            judgment=judgment,
            capacity_available=True,
            now="2026-09-17T20:10:00Z",
        )

        self.assert_transition(
            unavailable,
            phase=ItemPhase.READY_FOR_ACTION,
            step=NextStep.WAIT_FOR_READ,
        )
        self.assertTrue(unavailable.retain_judgment)
        self.assertIsNone(unavailable.action_kind)

    def test_read_unavailable_retains_known_facts_without_progress(self) -> None:
        item = _item(
            issue_number=17,
            last_progressed_at=NOW,
        )

        transition = reduce_item(
            item,
            _refresh(item=item, issue=None, complete=False),
            now=LATER,
        )

        self.assert_transition(
            transition,
            phase=item.phase,
            step=NextStep.WAIT_FOR_READ,
        )
        self.assertEqual(17, transition.item.issue_number)
        self.assertEqual(LATER, transition.item.last_checked_at)
        self.assertEqual(NOW, transition.item.last_progressed_at)
        self.assertEqual("unavailable", transition.item.read_status)

    def test_recovery_applies_fresh_terminal_task_before_capacity_decision(self) -> None:
        item = _item(
            task_id="task-123",
            task_state=TaskState.QUEUED,
            failed_jobs=(BUILD,),
            last_judged_fingerprint=EVIDENCE,
        )
        recovery = _run(
            102,
            89,
            conclusion="success",
            jobs=(
                _job(
                    902,
                    BUILD,
                    run_id=102,
                    conclusion="success",
                ),
            ),
        )

        transition = reduce_item(
            item,
            _refresh(
                item=item,
                runs=(_failure_run(), recovery),
                recovery="passed",
                recovery_run=recovery,
                task=_task("idle"),
                pre_write=False,
            ),
            now=LATER,
        )

        self.assert_transition(
            transition,
            phase=ItemPhase.RECOVERED,
            step=NextStep.WAIT_FOR_CHANGE,
        )
        self.assertIs(TaskState.IDLE, transition.item.task_state)

    def test_unchanged_recovery_preserves_first_recovery_and_progress_times(self) -> None:
        recovered_at = "2026-09-17T20:03:00Z"
        item = _item(
            phase=ItemPhase.RECOVERED,
            failed_jobs=(BUILD,),
            last_judged_fingerprint=EVIDENCE,
            recovered_run_id=102,
            recovered_at=recovered_at,
            last_progressed_at=recovered_at,
            wait_reason="recovered",
        )
        recovery = _run(
            102,
            89,
            conclusion="success",
            jobs=(
                _job(
                    902,
                    BUILD,
                    run_id=102,
                    conclusion="success",
                ),
            ),
        )

        transition = reduce_item(
            item,
            _refresh(
                item=item,
                runs=(_failure_run(), recovery),
                recovery="passed",
                recovery_run=recovery,
                pre_write=False,
            ),
            now="2026-09-17T20:20:00Z",
        )

        self.assertEqual(recovered_at, transition.item.recovered_at)
        self.assertEqual(recovered_at, transition.item.last_progressed_at)
        self.assertEqual(
            "2026-09-17T20:20:00Z",
            transition.item.last_checked_at,
        )

    def test_worker_failure_and_uncertain_action_require_attention(self) -> None:
        item = _item()
        for worker_state, action_state in (
            (WorkState.FAILED, None),
            (WorkState.INVALID, None),
            (None, ActionState.UNCERTAIN),
        ):
            with self.subTest(
                worker_state=worker_state,
                action_state=action_state,
            ):
                transition = reduce_item(
                    item,
                    _refresh(item=item, complete=False),
                    worker_state=worker_state,
                    action_state=action_state,
                    now=LATER,
                )
                self.assert_transition(
                    transition,
                    phase=ItemPhase.NEEDS_ATTENTION,
                    step=NextStep.NEEDS_ATTENTION,
                )

    def test_uncertain_action_takes_priority_over_new_judgment(self) -> None:
        item = _item(issue_number=17)
        transition = reduce_item(
            item,
            _refresh(item=item, issue=_issue()),
            request=_request(item, issue_number=17),
            judgment=_judgment(item),
            action_state=ActionState.UNCERTAIN,
            now=LATER,
        )

        self.assert_transition(
            transition,
            phase=ItemPhase.NEEDS_ATTENTION,
            step=NextStep.NEEDS_ATTENTION,
        )
        self.assertIsNone(transition.action_kind)

    def test_confirmed_issue_creation_advances_same_request_only(self) -> None:
        item = _item()
        request = _request(item)
        judgment = _judgment(item)
        first = reduce_item(
            item,
            _refresh(item=item),
            request=request,
            judgment=judgment,
            now=LATER,
        )
        self.assertIs(ActionKind.CREATE_ISSUE, first.action_kind)

        confirmed = ConfirmedIssueCreation(
            worker_id=request.worker_id,
            item_id=item.id,
            episode=item.episode,
            evidence_fingerprint=item.evidence_fingerprint,
            issue_number=17,
        )
        second = reduce_item(
            first.item,
            _refresh(item=first.item),
            request=request,
            judgment=judgment,
            confirmed_issue=confirmed,
            now="2026-09-17T20:10:00Z",
        )
        self.assert_transition(
            second,
            phase=ItemPhase.READY_FOR_ACTION,
            step=NextStep.PREPARE_ACTION,
            action=ActionKind.ASSIGN_COPILOT,
        )
        self.assertEqual(17, second.item.issue_number)

        foreign = replace(confirmed, worker_id="worker-foreign")
        rejected = reduce_item(
            item,
            _refresh(item=item),
            request=request,
            judgment=judgment,
            confirmed_issue=foreign,
            now=LATER,
        )
        self.assertIs(NextStep.NEEDS_ATTENTION, rejected.next_step)
        self.assertIsNone(rejected.item.issue_number)

    def test_initial_judgment_rejects_foreign_workflow_and_stale_attempt(
        self,
    ) -> None:
        item = _item(issue_number=17)
        fresh = _request(item, issue_number=17)
        judgment = _judgment(item)
        foreign_run = replace(
            fresh.failure_run,
            key=WorkflowKey("owner/repo", 99, "main"),
            workflow_path=".github/workflows/other.yml",
        )
        stale_run = _failure_run(attempt=2)
        cases = (
            replace(
                fresh,
                workflow_id=99,
                workflow_path=".github/workflows/other.yml",
                failure_run=foreign_run,
            ),
            replace(
                fresh,
                failure_run=stale_run,
                failed_jobs=stale_run.jobs,
            ),
        )
        for request in cases:
            with self.subTest(request=request):
                transition = reduce_item(
                    item,
                    _refresh(item=item, issue=_issue()),
                    request=request,
                    judgment=judgment,
                    now=LATER,
                )
                self.assert_transition(
                    transition,
                    phase=ItemPhase.NEEDS_ATTENTION,
                    step=NextStep.NEEDS_ATTENTION,
                )
                self.assertIsNone(transition.action_kind)

    def test_initial_judgment_allows_same_identity_without_log_enrichment(self) -> None:
        item = _item(issue_number=17)
        request = _request(item, issue_number=17)
        stripped_run = replace(
            request.failure_run,
            jobs=tuple(
                replace(job, log_excerpt=None, log_truncated=False)
                for job in request.failure_run.jobs
            ),
        )
        stripped_request = replace(
            request,
            failure_run=stripped_run,
            failed_jobs=stripped_run.jobs,
        )

        transition = reduce_item(
            item,
            _refresh(item=item, issue=_issue()),
            request=stripped_request,
            judgment=_judgment(item),
            now=LATER,
        )

        self.assert_transition(
            transition,
            phase=ItemPhase.READY_FOR_ACTION,
            step=NextStep.PREPARE_ACTION,
            action=ActionKind.ASSIGN_COPILOT,
        )


if __name__ == "__main__":
    unittest.main()
