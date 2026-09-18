from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ci_shepherd.workflow_loop.core import (
    CiCoordinator,
    EffectMode,
    ScenarioDiscovery,
    ScenarioObservation,
)
from ci_shepherd.workflow_loop.effects import GitHubEffectExecutor
from ci_shepherd.workflow_loop.models import (
    ActionIntent,
    ActionKind,
    ItemPhase,
    TaskState,
)
from ci_shepherd.workflow_loop.scenario import (
    ItemTransition,
    NextStep,
)
from ci_shepherd.workflow_loop.scenarios import WorkflowFailureScenario
from ci_shepherd.workflow_loop.reader import WorkflowReader
from ci_shepherd.workflow_loop.report import render_status
from ci_shepherd.workflow_loop.state import WorkflowLoopStore
from test_workflow_loop_reader import (
    BRANCH,
    REPOSITORY,
    WORKFLOW_ID,
    base_responses,
    job,
    run,
)
from test_workflow_loop_manager import _Launcher, _issue_search_endpoint
from workflow_loop_fakes import EndpointClient, PagedResponse


class _NoopLauncher:
    def observe(self, worker):
        raise AssertionError("No worker should exist.")


class _TestScenario:
    def __init__(
        self,
        name: str,
        workflow_id: int,
        priority: int,
        *,
        repository: str = "owner/repo",
        branch: str = "main",
    ) -> None:
        self.name = name
        self.workflow_id = workflow_id
        self.priority_value = priority
        self.repository = repository
        self.branch = branch
        self.assessed: list[int] = []

    def observe(
        self,
        *,
        repository="owner/repo",
        branch="main",
        tracked_items=(),
        workflow_ids=None,
    ) -> ScenarioObservation:
        return ScenarioObservation(
            value=None,
            request_count=0,
            errors=(),
        )

    def discover(self, store, observation, items) -> tuple[ScenarioDiscovery, ...]:
        if any(item.workflow_id == self.workflow_id for item in items):
            return ()
        observed_run = run(
            10_000 + self.workflow_id,
            workflow_id=self.workflow_id,
            path=f".github/workflows/{self.name}.yml",
            name=self.name,
            branch=self.branch,
            repository_name=self.repository,
            head_repository_name=self.repository,
        )
        from ci_shepherd.workflow_loop.reader import _normalize_run
        normalized = _normalize_run(
            observed_run,
            repository=self.repository,
            branch=self.branch,
            workflow_id=self.workflow_id,
            workflow_path=f".github/workflows/{self.name}.yml",
            workflow_name=self.name,
        )
        assert normalized is not None
        item = store.upsert_failure(
            normalized,
            "2026-09-18T04:00:00Z",
            scenario_name=self.name,
            case_key=f"{self.name}:{self.workflow_id}",
        )
        return (ScenarioDiscovery(item=item, refresh=None),)

    def owns(self, item) -> bool:
        return item.workflow_id == self.workflow_id

    def priority(self, item) -> int:
        return self.priority_value

    def refresh(self, item, *, judgment):
        return None

    def normalize_item(self, store, item, refresh):
        return item

    def assess(
        self,
        item,
        refresh,
        *,
        now,
        request,
        judgment,
        confirmed_issue,
        worker_state,
        action_state,
        capacity_available,
    ) -> ItemTransition:
        self.assessed.append(item.workflow_id)
        return ItemTransition(
            item=replace(
                item,
                phase=ItemPhase.OBSERVING_FAILURE,
                last_checked_at=now,
            ),
            next_step=NextStep.WAIT_FOR_CHANGE,
            history_event=f"{self.name}-observed",
            summary=f"{self.name} was assessed.",
        )

    def action_for_judgment(self, item, judgment):
        return None

    def prepare_judgment(
        self,
        *,
        store,
        item,
        refresh,
        judgment_round,
        worker_id,
        session_id,
    ):
        raise AssertionError("This scenario does not request judgment.")


class _CapacityScenario(_TestScenario):
    def __init__(self, *args, completes_task: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.completes_task = completes_task
        self.capacity_seen: list[bool] = []

    def normalize_item(self, store, item, refresh):
        if not self.completes_task:
            return item
        updated = replace(item, task_state=TaskState.COMPLETED)
        store.update_item(
            updated,
            history_event="task-completed",
            summary="The test task completed.",
            detail={},
        )
        return updated

    def assess(
        self,
        item,
        refresh,
        *,
        now,
        request,
        judgment,
        confirmed_issue,
        worker_state,
        action_state,
        capacity_available,
    ) -> ItemTransition:
        self.capacity_seen.append(capacity_available)
        return super().assess(
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


class CiCoordinatorBoundaryTests(unittest.TestCase):
    def test_core_does_not_import_workflow_scenario_implementation(self) -> None:
        import ci_shepherd.workflow_loop.core as core

        source = Path(core.__file__).read_text(encoding="utf-8")
        self.assertNotIn("workflow_failure", source)
        self.assertNotIn("workflow_loop.reader", source)
        self.assertNotIn("workflow_loop.reducer", source)
        self.assertNotIn("workflow_loop.policy", source)

    def test_test_scenario_runs_alongside_workflow_scenario(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            observed_run = run(101)
            client = EndpointClient({
                **base_responses(observed_run),
                f"/repos/{REPOSITORY}/actions/runs/101": observed_run,
                _issue_search_endpoint(WORKFLOW_ID): {"total_count": 0, "items": []},
                f"/repos/{REPOSITORY}/actions/runs/101/attempts/1/jobs": (
                    PagedResponse((job(101, 1001, "Build"),))
                ),
                f"/repos/{REPOSITORY}/actions/jobs/1001/logs": "error CS1002: ; expected",
            })
            reader = WorkflowReader(
                client=client,
                clock=lambda: datetime(2026, 9, 18, 4, tzinfo=UTC),
                request_count=lambda: client.request_count,
            )
            workflow_scenario = WorkflowFailureScenario(reader)
            test_scenario = _TestScenario(
                "test-only",
                workflow_id=WORKFLOW_ID,
                priority=5,
                repository=REPOSITORY,
                branch=BRANCH,
            )
            store = WorkflowLoopStore(
                state_directory,
                repository=REPOSITORY,
                branch=BRANCH,
            )
            store.initialize()
            pass_ids = iter(("pass-1", "worker-1", "pass-2"))
            coordinator = CiCoordinator(
                state_directory=state_directory,
                repository=REPOSITORY,
                branch=BRANCH,
                store=store,
                scenarios=(workflow_scenario, test_scenario),
                launcher=_Launcher(state_directory, store),
                writer=None,
                clock=lambda: datetime(2026, 9, 18, 4, tzinfo=UTC),
                id_factory=pass_ids.__next__,
                workflow_ids=None,
                capacity_limit=1,
            )

            result = coordinator.run_pass(mode=EffectMode.READ_ONLY)

            self.assertEqual(2, result.discovered_items)
            items = store.list_items()
            self.assertEqual(
                {
                    ("workflow-failure", f"workflow:{WORKFLOW_ID}"),
                    ("test-only", f"test-only:{WORKFLOW_ID}"),
                },
                {
                    (item.scenario_name, item.case_key)
                    for item in items
                },
            )
            self.assertEqual([WORKFLOW_ID], test_scenario.assessed)
            test_item = next(
                item for item in store.list_items()
                if item.scenario_name == "test-only"
            )
            effects = GitHubEffectExecutor(
                store=store,
                clock=lambda: "2026-09-18T04:01:00Z",
                active_item_limit=2,
            )
            outcome = effects.execute(
                ActionIntent(
                    action_id="test-only-issue-1",
                    item_id=test_item.id,
                    episode=test_item.episode,
                    kind=ActionKind.CREATE_ISSUE,
                    ordinal=1,
                    payload={"title": "Synthetic test effect"},
                    prepared_at="2026-09-18T04:01:00Z",
                ),
                pass_id="test-effect-pass",
                owner_id="test-owner",
                guard=lambda: None,
                call=lambda: {"number": 77},
                validate=lambda value: value["number"],
            )
            self.assertEqual("confirmed", outcome.status)
            self.assertEqual(77, outcome.value)
            task_outcome = effects.execute(
                ActionIntent(
                    action_id="test-only-task-1",
                    item_id=test_item.id,
                    episode=test_item.episode,
                    kind=ActionKind.ASSIGN_COPILOT,
                    ordinal=1,
                    payload={"prompt": "Synthetic test task"},
                    prepared_at="2026-09-18T04:01:01Z",
                ),
                pass_id="test-task-effect-pass",
                owner_id="test-owner",
                guard=lambda: None,
                call=lambda: {"id": "test-task"},
                validate=lambda value: value["id"],
            )
            self.assertEqual("confirmed", task_outcome.status)
            self.assertEqual("test-task", task_outcome.value)
            self.assertEqual(2, len(store.list_actions()))

            coordinator.run_pass(mode=EffectMode.READ_ONLY)

            self.assertEqual(
                [WORKFLOW_ID, WORKFLOW_ID],
                test_scenario.assessed,
            )
            self.assertEqual(frozenset(item.id for item in items), store.active_item_ids())
            self.assertEqual(1, len(store.list_workers()))
            self.assertEqual(
                {
                    ("workflow-failure", f"workflow:{WORKFLOW_ID}"),
                    ("test-only", f"test-only:{WORKFLOW_ID}"),
                },
                {
                    (item.scenario_name, item.case_key)
                    for item in store.list_items()
                },
            )

    def test_core_runs_explicit_scenarios_with_shared_store_and_order(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            store = WorkflowLoopStore(
                state_directory,
                repository="owner/repo",
                branch="main",
            )
            store.initialize()
            lower = _TestScenario("lower", workflow_id=902, priority=20)
            higher = _TestScenario("higher", workflow_id=901, priority=10)
            coordinator = CiCoordinator(
                state_directory=state_directory,
                repository="owner/repo",
                branch="main",
                store=store,
                scenarios=(lower, higher),
                launcher=_NoopLauncher(),
                writer=None,
                clock=lambda: datetime(2026, 9, 18, 4, tzinfo=UTC),
                id_factory=lambda: "core-boundary-pass",
                workflow_ids=None,
            )

            result = coordinator.run_pass(mode=EffectMode.READ_ONLY)

            self.assertEqual(2, result.discovered_items)
            self.assertEqual([901], higher.assessed)
            self.assertEqual([902], lower.assessed)
            self.assertEqual(2, len(store.list_items()))
            self.assertEqual(frozenset(), store.active_item_ids())

    def test_generic_effect_proposal_retains_payload_without_invocation(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            store = WorkflowLoopStore(
                state_directory,
                repository="owner/repo",
                branch="main",
            )
            store.initialize()
            scenario = _TestScenario(
                "would-do",
                workflow_id=903,
                priority=1,
            )
            item = scenario.discover(store, scenario.observe(), ())[0].item
            effects = GitHubEffectExecutor(
                store=store,
                clock=lambda: "2026-09-18T04:00:00Z",
                active_item_limit=2,
            )
            intent = ActionIntent(
                action_id="synthetic-action",
                item_id=item.id,
                episode=item.episode,
                kind=ActionKind.CREATE_ISSUE,
                ordinal=1,
                payload={
                    "repository": "owner/repo",
                    "title": "Exact title",
                    "body": "Exact body",
                },
                prepared_at="2026-09-18T04:00:00Z",
            )
            for _ in range(2):
                result = effects.execute(
                    intent,
                    pass_id="pass",
                    owner_id="owner",
                    guard=lambda: None,
                    call=lambda: self.fail("A proposed effect must not invoke GitHub."),
                    validate=lambda value: value,
                    propose_only=True,
                )
                self.assertEqual("proposed", result.status)
            self.assertEqual((), store.list_workers())
            self.assertEqual((), store.list_actions())
            self.assertEqual(1, len(store.list_proposals()))
            self.assertEqual(dict(intent.payload), store.list_proposals()[0].detail["payload"])
            report = render_status(
                state_directory,
                repository="owner/repo",
                branch="main",
                now=datetime(2026, 9, 18, 4, tzinfo=UTC),
            )
            self.assertIn(
                "PROPOSED create_issue",
                report,
            )
            self.assertIn('"body": "Exact body"', report)

    def test_all_ownership_refreshes_precede_priority_assessment(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            store = WorkflowLoopStore(
                state_directory,
                repository="owner/repo",
                branch="main",
            )
            store.initialize()
            low = _CapacityScenario(
                "low",
                workflow_id=920,
                priority=20,
                completes_task=True,
            )
            high = _CapacityScenario(
                "high",
                workflow_id=921,
                priority=1,
            )
            for scenario in (low, high):
                discovery = scenario.discover(
                    store,
                    scenario.observe(),
                    store.list_items(),
                )[0]
                store.bind_item_scenario(
                    discovery.item.id,
                    scenario.name,
                )
            low_item = next(
                item
                for item in store.list_items()
                if item.workflow_id == low.workflow_id
            )
            store.update_item(
                replace(
                    low_item,
                    phase=ItemPhase.COPILOT_ACTIVE,
                    task_id="task-low",
                    task_state=TaskState.QUEUED,
                ),
                history_event="task-active",
                summary="The low-priority task is active.",
                detail={},
            )
            coordinator = CiCoordinator(
                state_directory=state_directory,
                repository="owner/repo",
                branch="main",
                store=store,
                scenarios=(low, high),
                launcher=_NoopLauncher(),
                writer=None,
                clock=lambda: datetime(2026, 9, 18, 4, tzinfo=UTC),
                id_factory=lambda: "capacity-refresh-pass",
                workflow_ids=None,
                capacity_limit=1,
            )

            coordinator.run_pass(mode=EffectMode.READ_ONLY)

            self.assertEqual([True], high.capacity_seen)
            self.assertNotIn(low_item.id, store.active_item_ids())


if __name__ == "__main__":
    unittest.main()
