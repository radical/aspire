from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest

from ci_shepherd.workflow_loop.models import (
    ActionCompletion,
    ActionIntent,
    ActionKind,
    ActionState,
    ItemPhase,
    JobKey,
    JobObservation,
    RunObservation,
    TaskState,
    WorkerCompletion,
    WorkerReservation,
    WorkState,
    WorkflowKey,
)
from ci_shepherd.workflow_loop.state import WorkflowLoopStore


NOW = "2026-09-17T20:00:00Z"
LATER = "2026-09-17T20:20:00Z"


def _job(
    job_id: int = 900,
    *,
    name: str = "Build / Linux",
    conclusion: str = "failure",
) -> JobObservation:
    return JobObservation(
        run_id=101,
        attempt=1,
        job_id=job_id,
        key=JobKey(name=name, runner_labels=("ubuntu-latest",)),
        status="completed",
        conclusion=conclusion,
        started_at=NOW,
        completed_at="2026-09-17T20:01:00Z",
        url=f"https://github.com/owner/repo/actions/runs/101/job/{job_id}",
        log_excerpt="error CS1002: ; expected",
        log_truncated=False,
    )


def _run(
    *,
    run_id: int = 101,
    attempt: int = 1,
    jobs: tuple[JobObservation, ...] | None = None,
) -> RunObservation:
    observations = jobs or (_job(),)
    if run_id != 101 or attempt != 1:
        observations = tuple(
            replace(job, run_id=run_id, attempt=attempt)
            for job in observations
        )
    return RunObservation(
        key=WorkflowKey("owner/repo", 42, "main"),
        workflow_path=".github/workflows/ci.yml",
        workflow_name="CI",
        run_id=run_id,
        run_number=88,
        attempt=attempt,
        head_sha=f"sha-{run_id}-{attempt}",
        event="push",
        status="completed",
        conclusion="failure",
        created_at=NOW,
        updated_at="2026-09-17T20:01:00Z",
        url=f"https://github.com/owner/repo/actions/runs/{run_id}",
        jobs_complete=True,
        jobs=observations,
    )


def _reservation(
    state_directory: Path,
    item_id: int,
    episode: int,
    fingerprint: str,
    *,
    judgment_round: int = 0,
) -> WorkerReservation:
    worker_root = (
        state_directory / "workers"
        / f"worker-{item_id}-{episode}-{judgment_round}"
    )
    return WorkerReservation(
        worker_id=(
            f"worker-{item_id}-{episode}-{judgment_round}-{fingerprint[-4:]}"
        ),
        item_id=item_id,
        episode=episode,
        evidence_fingerprint=fingerprint,
        session_id=f"session-{item_id}",
        request_path=str((worker_root / "request.json").resolve()),
        result_path=str((worker_root / "result.json").resolve()),
        detail_path=str((worker_root / "detail.log").resolve()),
        lifetime_lock_path=str((worker_root / "lifetime.lock").resolve()),
        queued_at=NOW,
        judgment_round=judgment_round,
    )


def _intent(
    item_id: int,
    episode: int,
    *,
    ordinal: int = 1,
    kind: ActionKind = ActionKind.ASSIGN_COPILOT,
) -> ActionIntent:
    return ActionIntent(
        action_id=f"action-{item_id}-{episode}-{kind}-{ordinal}",
        item_id=item_id,
        episode=episode,
        kind=kind,
        ordinal=ordinal,
        payload={"request": "Fix it"},
        prepared_at=NOW,
    )


class WorkflowLoopStoreTests(unittest.TestCase):
    def test_explicit_empty_case_key_is_not_replaced_by_a_default(self) -> None:
        with TemporaryDirectory() as scratch:
            store = WorkflowLoopStore(
                Path(scratch),
                repository="owner/repo",
                branch="main",
            )
            store.initialize()

            with self.assertRaisesRegex(ValueError, "case_key"):
                store.upsert_failure(
                    _run(),
                    NOW,
                    scenario_name="test-only",
                    case_key="",
                )

            self.assertEqual((), store.list_items())

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.state_directory = Path(self.temporary.name) / "state"
        self.store = WorkflowLoopStore(
            self.state_directory,
            repository="owner/repo",
            branch="main",
        )
        self.store.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _new_item(self, *, workflow_id: int = 42, run_id: int = 101):
        observation = replace(
            _run(run_id=run_id),
            key=WorkflowKey("owner/repo", workflow_id, "main"),
            workflow_path=f".github/workflows/{workflow_id}.yml",
            workflow_name=f"Workflow {workflow_id}",
        )
        return self.store.upsert_failure(observation, NOW)

    def _begin_action(self, action_id: str, *, pass_id: str = "pass-1") -> bool:
        return self.store.begin_action_invocation(
            action_id,
            pass_id=pass_id,
            owner_id="owner-1",
            invoked_at=NOW,
        )

    def test_initialize_creates_owner_only_bound_state(self) -> None:
        self.assertEqual(0o700, self.state_directory.stat().st_mode & 0o777)
        database = self.state_directory / "workflow-loop.sqlite3"
        self.assertEqual(0o600, database.stat().st_mode & 0o777)

        WorkflowLoopStore(
            self.state_directory,
            repository="owner/repo",
            branch="main",
        ).initialize()
        for repository, branch, message in (
            ("other/repo", "main", "repository"),
            ("owner/repo", "release", "branch"),
        ):
            with self.subTest(repository=repository, branch=branch):
                with self.assertRaisesRegex(ValueError, message):
                    WorkflowLoopStore(
                        self.state_directory,
                        repository=repository,
                        branch=branch,
                    ).initialize()

    def test_initialize_rejects_unsupported_schema_version(self) -> None:
        database = self.state_directory / "workflow-loop.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "UPDATE meta SET value = '99' WHERE key = 'schema_version'"
            )
            connection.commit()
        with self.assertRaisesRegex(ValueError, "schema version"):
            self.store.initialize()

    def test_initialize_rejects_incomplete_schema_without_migrating_it(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            state_directory.mkdir(mode=0o700)
            database = state_directory / "workflow-loop.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.executemany(
                    "INSERT INTO meta(key, value) VALUES(?, ?)",
                    (
                        ("schema_version", "1"),
                        ("repository", "owner/repo"),
                        ("branch", "main"),
                    ),
                )
                connection.execute(
                    "CREATE TABLE action_attempts(action_id TEXT PRIMARY KEY)"
                )
                connection.commit()

            with self.assertRaisesRegex(ValueError, "schema"):
                WorkflowLoopStore(
                    state_directory,
                    repository="owner/repo",
                    branch="main",
                ).initialize()

    def test_workflow_scope_is_bound_before_reads_or_effects(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            store = WorkflowLoopStore(
                state_directory,
                repository="owner/repo",
                branch="main",
            )
            store.initialize(workflow_ids=(18, 17))
            store.initialize(workflow_ids=(17, 18))

            with self.assertRaisesRegex(ValueError, "workflow scope"):
                store.initialize(workflow_ids=(17,))
            with self.assertRaisesRegex(ValueError, "workflow scope"):
                store.initialize()

    def test_unreleased_older_schema_is_rejected(self) -> None:
        database = self.state_directory / "workflow-loop.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "UPDATE meta SET value = '2' WHERE key = 'schema_version'"
            )
            connection.commit()

        with self.assertRaisesRegex(ValueError, "schema version 2"):
            self.store.initialize()

    def test_v3_workflow_rows_migrate_with_ids_links_and_scenario(self) -> None:
        item = self._new_item()
        self.store.update_item(
            replace(
                item,
                issue_number=17,
                task_id="task-legacy",
                task_state=TaskState.IN_PROGRESS,
                pull_request_number=23,
            ),
            history_event="legacy-links",
            summary="Legacy links were recorded.",
            detail={},
        )
        database = self.state_directory / "workflow-loop.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            columns = tuple(
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(workflow_items)"
                )
            )[:-3]
            names = ", ".join(columns)
            connection.execute(
                f"CREATE TABLE workflow_items_v3 AS "
                f"SELECT {names} FROM workflow_items"
            )
            connection.execute("DROP TABLE workflow_items")
            connection.execute(
                "ALTER TABLE workflow_items_v3 RENAME TO workflow_items"
            )
            connection.execute(
                "UPDATE meta SET value = '3' WHERE key = 'schema_version'"
            )
            connection.commit()

        self.store.initialize()

        migrated = self.store.list_items()[0]
        self.assertEqual(item.id, migrated.id)
        self.assertEqual(17, migrated.issue_number)
        self.assertEqual("task-legacy", migrated.task_id)
        self.assertEqual(23, migrated.pull_request_number)
        self.assertEqual("workflow-failure", migrated.scenario_name)
        self.assertEqual(
            f"workflow:{migrated.workflow_id}",
            migrated.case_key,
        )

    def test_two_processes_see_same_items_reservations_and_history(self) -> None:
        item = self._new_item()
        reservation = _reservation(
            self.state_directory,
            item.id,
            item.episode,
            item.evidence_fingerprint,
        )
        self.assertTrue(self.store.reserve_worker(reservation, capacity_limit=2))
        script = """
import json
from pathlib import Path
import sys
from ci_shepherd.workflow_loop.state import WorkflowLoopStore

store = WorkflowLoopStore(
    Path(sys.argv[1]),
    repository="owner/repo",
    branch="main",
)
store.initialize()
print(json.dumps({
    "items": len(store.list_items()),
    "workers": len(store.list_workers()),
    "history": len(store.recent_history(int(sys.argv[2]))),
}))
"""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(self.state_directory),
                str(item.id),
            ],
            check=True,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
        )

        self.assertEqual(
            {"items": 1, "workers": 1, "history": 1},
            json.loads(result.stdout),
        )

    def test_repeated_failure_updates_one_row_and_recovery_starts_episode(self) -> None:
        first = self._new_item()
        repeated = self.store.upsert_failure(_run(run_id=102), LATER)

        self.assertEqual(first.id, repeated.id)
        self.assertEqual(1, repeated.episode)
        self.assertEqual(1, len(self.store.list_items()))

        self.store.update_item(
            replace(
                repeated,
                phase=ItemPhase.RECOVERED,
                recovered_run_id=103,
                recovered_at=LATER,
            ),
            history_event="recovered",
            summary="The selected build job passed.",
            detail={"runId": 103},
        )
        next_episode = self.store.upsert_failure(
            _run(run_id=104),
            "2026-09-17T20:30:00Z",
        )

        self.assertEqual(first.id, next_episode.id)
        self.assertEqual(2, next_episode.episode)
        self.assertIs(ItemPhase.OBSERVING_FAILURE, next_episode.phase)

    def test_raw_refresh_preserves_scoped_jobs_and_episode_provenance(self) -> None:
        build = _job(900, name="Build / Linux")
        test = _job(901, name="Tests / Linux")
        item = self.store.upsert_failure(_run(jobs=(build, test)), NOW)
        scoped = replace(
            item,
            phase=ItemPhase.READY_FOR_ACTION,
            failed_jobs=(build.key,),
            last_judged_fingerprint=item.evidence_fingerprint,
        )
        self.store.update_item(
            scoped,
            history_event="judged",
            summary="Build is the repair target.",
            detail={"inScopeJobIds": [900]},
        )

        refreshed = self.store.upsert_failure(
            _run(
                run_id=102,
                jobs=(
                    replace(build, run_id=102),
                    replace(test, run_id=102),
                ),
            ),
            LATER,
        )

        self.assertEqual((build.key,), refreshed.failed_jobs)
        self.assertEqual(item.episode, refreshed.episode)
        self.assertEqual(
            item.first_failure_seen_at,
            refreshed.first_failure_seen_at,
        )
        self.assertEqual(102, refreshed.failure_run_id)
        self.assertNotEqual(
            item.evidence_fingerprint,
            refreshed.evidence_fingerprint,
        )
        with self.assertRaisesRegex(ValueError, "stale evidence"):
            self.store.update_item(
                scoped,
                history_event="late-result",
                summary="This judgment used superseded evidence.",
                detail={},
            )

    def test_reservation_and_intent_are_visible_before_side_effects(self) -> None:
        first = self._new_item()
        second = self._new_item(workflow_id=43, run_id=102)
        observer = WorkflowLoopStore(
            self.state_directory,
            repository="owner/repo",
            branch="main",
        )
        observer.initialize()

        reservation = _reservation(
            self.state_directory,
            first.id,
            first.episode,
            first.evidence_fingerprint,
        )
        self.assertTrue(self.store.reserve_worker(reservation, capacity_limit=2))
        self.assertEqual(reservation.worker_id, observer.list_workers()[0].worker_id)

        intent = _intent(second.id, second.episode)
        self.assertTrue(self.store.prepare_action(intent, capacity_limit=2))
        self.assertEqual(intent.action_id, observer.list_actions()[0].action_id)

    def test_active_union_counts_owned_items_once_and_excludes_passive_phases(self) -> None:
        local = self._new_item()
        cloud = self._new_item(workflow_id=43, run_id=102)
        passive = self._new_item(workflow_id=44, run_id=103)
        external = self._new_item(workflow_id=45, run_id=104)
        self.assertTrue(
            self.store.reserve_worker(
                _reservation(
                    self.state_directory,
                    local.id,
                    local.episode,
                    local.evidence_fingerprint,
                ),
                capacity_limit=2,
            )
        )
        self.store.update_item(
            replace(cloud, phase=ItemPhase.COPILOT_ACTIVE),
            history_event="cloud-active",
            summary="Copilot is repairing the workflow.",
            detail={},
        )
        self.store.update_item(
            replace(passive, phase=ItemPhase.WAITING_FOR_CI),
            history_event="waiting",
            summary="Waiting for CI.",
            detail={},
        )
        self.store.update_item(
            replace(
                external,
                phase=ItemPhase.OBSERVING_EXTERNAL_REPAIR,
                external_owner="github:someone",
            ),
            history_event="external",
            summary="An external repair is active.",
            detail={},
        )

        self.assertEqual(frozenset({local.id, cloud.id}), self.store.active_item_ids())

        intent = _intent(local.id, local.episode)
        self.store.mark_worker_launch_attempt(
            _reservation(
                self.state_directory,
                local.id,
                local.episode,
                local.evidence_fingerprint,
            ).worker_id,
            launch_attempted_at=NOW,
        )
        self.store.complete_worker(
            WorkerCompletion(
                worker_id=_reservation(
                    self.state_directory,
                    local.id,
                    local.episode,
                    local.evidence_fingerprint,
                ).worker_id,
                state=WorkState.SUCCEEDED,
                completed_at=LATER,
                exit_code=0,
                error=None,
            )
        )
        self.assertTrue(self.store.prepare_action(intent, capacity_limit=2))
        self._begin_action(intent.action_id)
        self.store.complete_action(
            ActionCompletion(
                action_id=intent.action_id,
                state=ActionState.UNCERTAIN,
                completed_at=LATER,
                remote_number=None,
                remote_task_id=None,
                error="Connection dropped after write.",
            )
        )
        self.assertEqual(frozenset({local.id, cloud.id}), self.store.active_item_ids())

    def test_waiting_for_user_task_is_passive_and_not_carried_to_new_episode(
        self,
    ) -> None:
        waiting = self._new_item()
        ready = self._new_item(workflow_id=43, run_id=102)
        waiting = replace(
            waiting,
            phase=ItemPhase.WAITING_FOR_HUMAN,
            task_id="task-waiting",
            task_state=TaskState.WAITING_FOR_USER,
        )
        self.store.update_item(
            waiting,
            history_event="human-wait",
            summary="The owned task is waiting for a human.",
            detail={"taskId": waiting.task_id},
        )

        self.assertEqual(frozenset(), self.store.active_item_ids())
        self.assertTrue(
            self.store.prepare_action(
                _intent(ready.id, ready.episode),
                capacity_limit=1,
            )
        )

        self.store.update_item(
            replace(
                waiting,
                phase=ItemPhase.RECOVERED,
                recovered_run_id=103,
                recovered_at=LATER,
            ),
            history_event="recovered",
            summary="CI recovered while the task waited for a human.",
            detail={},
        )
        next_episode = self.store.upsert_failure(
            _run(run_id=104),
            "2026-09-17T20:30:00Z",
        )
        self.assertIsNone(next_episode.task_id)
        self.assertIsNone(next_episode.task_state)

    def test_prepare_action_atomically_denies_overbooking_without_inserting(self) -> None:
        cloud = self._new_item()
        ready_a = self._new_item(workflow_id=43, run_id=102)
        ready_b = self._new_item(workflow_id=44, run_id=103)
        self.store.update_item(
            replace(cloud, phase=ItemPhase.COPILOT_ACTIVE),
            history_event="cloud-active",
            summary="Cloud work is active.",
            detail={},
        )
        intent_a = _intent(ready_a.id, ready_a.episode)
        intent_b = _intent(ready_b.id, ready_b.episode)

        self.assertTrue(self.store.prepare_action(intent_a, capacity_limit=2))
        self.assertFalse(self.store.prepare_action(intent_b, capacity_limit=2))
        self.assertEqual((intent_a.action_id,), tuple(
            action.action_id for action in self.store.list_actions()
        ))

        self.store.complete_action(
            ActionCompletion(
                action_id=intent_a.action_id,
                state=ActionState.REJECTED,
                completed_at=LATER,
                remote_number=None,
                remote_task_id=None,
                error="Rejected before write.",
            )
        )
        self.assertTrue(self.store.prepare_action(intent_b, capacity_limit=2))

    def test_concurrent_action_preparation_cannot_overbook_capacity(self) -> None:
        cloud = self._new_item()
        ready_a = self._new_item(workflow_id=43, run_id=102)
        ready_b = self._new_item(workflow_id=44, run_id=103)
        self.store.update_item(
            replace(cloud, phase=ItemPhase.COPILOT_ACTIVE),
            history_event="cloud-active",
            summary="Cloud work is active.",
            detail={},
        )
        barrier = threading.Barrier(2)

        def prepare(intent: ActionIntent) -> bool:
            store = WorkflowLoopStore(
                self.state_directory,
                repository="owner/repo",
                branch="main",
            )
            store.initialize()
            barrier.wait()
            return store.prepare_action(intent, capacity_limit=2)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(
                executor.map(
                    prepare,
                    (
                        _intent(ready_a.id, ready_a.episode),
                        _intent(ready_b.id, ready_b.episode),
                    ),
                )
            )

        self.assertEqual([False, True], sorted(results))
        self.assertEqual(1, len(self.store.list_actions()))

    def test_changed_fingerprint_cannot_start_second_worker_for_item(self) -> None:
        item = self._new_item()
        first = _reservation(
            self.state_directory,
            item.id,
            item.episode,
            item.evidence_fingerprint,
        )
        self.assertTrue(self.store.reserve_worker(first, capacity_limit=2))
        changed = replace(
            first,
            worker_id="worker-changed",
            evidence_fingerprint="fnv1a64:ffffffffffffffff",
        )

        self.assertFalse(self.store.reserve_worker(changed, capacity_limit=2))
        self.assertEqual(1, len(self.store.list_workers()))

    def test_log_enrichment_does_not_change_evidence_fingerprint(self) -> None:
        original = _run()
        item = self.store.upsert_failure(original, NOW)
        enriched = replace(
            original,
            jobs=tuple(
                replace(
                    job,
                    log_excerpt="more detailed diagnostics",
                    log_truncated=True,
                )
                for job in original.jobs
            ),
        )

        refreshed = self.store.upsert_failure(enriched, LATER)

        self.assertEqual(item.evidence_fingerprint, refreshed.evidence_fingerprint)

    def test_worker_paths_must_be_distinct_canonical_absolute_state_paths(
        self,
    ) -> None:
        item = self._new_item()
        valid = _reservation(
            self.state_directory,
            item.id,
            item.episode,
            item.evidence_fingerprint,
        )
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        symlink = self.state_directory / "linked"
        try:
            symlink.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"Symlinks are not available: {error}")
        cases = {
            "relative traversal": replace(
                valid,
                result_path="../../outside/result.json",
            ),
            "outside absolute": replace(
                valid,
                result_path=str((outside / "result.json").resolve()),
            ),
            "symlink component": replace(
                valid,
                lifetime_lock_path=str(symlink / "lifetime.lock"),
            ),
            "aliased fields": replace(
                valid,
                result_path=valid.request_path,
            ),
        }
        for name, reservation in cases.items():
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "worker path"):
                    self.store.reserve_worker(reservation, capacity_limit=2)
        self.assertEqual(0, len(self.store.list_workers()))

    def test_corrupt_persisted_worker_path_raises_explicitly(self) -> None:
        item = self._new_item()
        reservation = _reservation(
            self.state_directory,
            item.id,
            item.episode,
            item.evidence_fingerprint,
        )
        self.store.reserve_worker(reservation, capacity_limit=2)
        database = self.state_directory / "workflow-loop.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "UPDATE workers SET result_path = '../../outside/result.json' "
                "WHERE worker_id = ?",
                (reservation.worker_id,),
            )
            connection.commit()

        with self.assertRaisesRegex(ValueError, "worker path"):
            self.store.list_workers()

    def test_active_cloud_repair_cannot_admit_a_local_worker_for_same_item(self) -> None:
        item = self._new_item()
        self.store.update_item(
            replace(item, phase=ItemPhase.COPILOT_ACTIVE),
            history_event="cloud-active",
            summary="Cloud work is active.",
            detail={},
        )

        self.assertFalse(
            self.store.reserve_worker(
                _reservation(
                    self.state_directory,
                    item.id,
                    item.episode,
                    item.evidence_fingerprint,
                ),
                capacity_limit=2,
            )
        )
        self.assertEqual(0, len(self.store.list_workers()))

    def test_active_cloud_repair_cannot_prepare_another_action_for_same_item(self) -> None:
        item = self._new_item()
        self.store.update_item(
            replace(item, phase=ItemPhase.COPILOT_ACTIVE),
            history_event="cloud-active",
            summary="Cloud work is active.",
            detail={},
        )

        self.assertFalse(
            self.store.prepare_action(
                _intent(item.id, item.episode),
                capacity_limit=2,
            )
        )
        self.assertEqual(0, len(self.store.list_actions()))

    def test_long_running_worker_remains_reserved_without_timer_reaper(self) -> None:
        item = self._new_item()
        reservation = replace(
            _reservation(
                self.state_directory,
                item.id,
                item.episode,
                item.evidence_fingerprint,
            ),
            queued_at="2026-09-17T18:00:00Z",
        )
        self.assertTrue(self.store.reserve_worker(reservation, capacity_limit=2))
        self.store.mark_worker_launch_attempt(
            reservation.worker_id,
            launch_attempted_at="2026-09-17T18:00:30Z",
        )
        self.store.mark_worker_launched(
            reservation.worker_id,
            pid=12345,
            launched_at="2026-09-17T18:01:00Z",
        )

        self.assertEqual(frozenset({item.id}), self.store.active_item_ids())
        self.assertIs(WorkState.RUNNING, self.store.list_workers()[0].state)

    def test_worker_launch_attempt_is_durable_before_popen_and_cannot_repeat(
        self,
    ) -> None:
        item = self._new_item()
        reservation = _reservation(
            self.state_directory,
            item.id,
            item.episode,
            item.evidence_fingerprint,
        )
        self.store.reserve_worker(reservation, capacity_limit=2)
        observer = WorkflowLoopStore(
            self.state_directory,
            repository="owner/repo",
            branch="main",
        )
        observer.initialize()

        self.assertTrue(
            self.store.mark_worker_launch_attempt(
                reservation.worker_id,
                launch_attempted_at=NOW,
            )
        )
        persisted = observer.list_workers()[0]
        self.assertEqual(NOW, persisted.launch_attempted_at)
        self.assertEqual(reservation.lifetime_lock_path, persisted.lifetime_lock_path)
        self.assertFalse(
            observer.mark_worker_launch_attempt(
                reservation.worker_id,
                launch_attempted_at=LATER,
            )
        )
        self.assertEqual(frozenset({item.id}), observer.active_item_ids())

    def test_terminal_file_alone_does_not_release_worker_reservation(self) -> None:
        item = self._new_item()
        result_path = self.state_directory / "fake-result.json"
        reservation = replace(
            _reservation(
                self.state_directory,
                item.id,
                item.episode,
                item.evidence_fingerprint,
            ),
            result_path=str(result_path),
        )
        self.store.reserve_worker(reservation, capacity_limit=2)
        self.store.mark_worker_launch_attempt(
            reservation.worker_id,
            launch_attempted_at=NOW,
        )
        result_path.write_text('{"state":"succeeded"}', encoding="utf-8")

        self.assertEqual(frozenset({item.id}), self.store.active_item_ids())
        self.assertIs(WorkState.QUEUED, self.store.list_workers()[0].state)

    def test_terminal_receipts_are_idempotent_and_conflicts_fail(self) -> None:
        item = self._new_item()
        reservation = _reservation(
            self.state_directory,
            item.id,
            item.episode,
            item.evidence_fingerprint,
        )
        self.store.reserve_worker(reservation, capacity_limit=2)
        completion = WorkerCompletion(
            worker_id=reservation.worker_id,
            state=WorkState.SUCCEEDED,
            completed_at=LATER,
            exit_code=0,
            error=None,
        )
        with self.assertRaisesRegex(ValueError, "launch attempt"):
            self.store.complete_worker(completion)
        self.store.mark_worker_launch_attempt(
            reservation.worker_id,
            launch_attempted_at=NOW,
        )
        self.store.complete_worker(completion)
        self.store.complete_worker(completion)
        with self.assertRaisesRegex(ValueError, "conflicting"):
            self.store.complete_worker(replace(completion, exit_code=1))

        intent = _intent(item.id, item.episode)
        self.store.prepare_action(intent, capacity_limit=2)
        self._begin_action(intent.action_id)
        action_completion = ActionCompletion(
            action_id=intent.action_id,
            state=ActionState.REJECTED,
            completed_at=LATER,
            remote_number=None,
            remote_task_id=None,
            error="Rejected.",
        )
        self.store.complete_action(action_completion)
        self.store.complete_action(action_completion)
        with self.assertRaisesRegex(ValueError, "conflicting"):
            self.store.complete_action(
                replace(action_completion, state=ActionState.UNCERTAIN)
            )

    def test_worker_rounds_are_durable_unique_and_consumed(self) -> None:
        item = self._new_item()
        for judgment_round in range(3):
            reservation = _reservation(
                self.state_directory,
                item.id,
                item.episode,
                item.evidence_fingerprint,
                judgment_round=judgment_round,
            )
            self.assertTrue(
                self.store.reserve_worker(reservation, capacity_limit=2)
            )
            self.store.mark_worker_launch_attempt(
                reservation.worker_id,
                launch_attempted_at=NOW,
            )
            self.store.complete_worker(
                WorkerCompletion(
                    worker_id=reservation.worker_id,
                    state=WorkState.SUCCEEDED,
                    completed_at=LATER,
                    exit_code=0,
                    error=None,
                )
            )
            self.assertTrue(
                self.store.consume_worker_result(
                    reservation.worker_id,
                    consumed_at=LATER,
                )
            )
            self.assertFalse(
                self.store.consume_worker_result(
                    reservation.worker_id,
                    consumed_at=LATER,
                )
            )
            duplicate = replace(
                reservation,
                worker_id=f"duplicate-{judgment_round}",
                session_id=f"duplicate-session-{judgment_round}",
            )
            with self.assertRaisesRegex(ValueError, "judgment round"):
                self.store.reserve_worker(duplicate, capacity_limit=2)

        restarted = WorkflowLoopStore(
            self.state_directory,
            repository="owner/repo",
            branch="main",
        )
        restarted.initialize()
        self.assertEqual(
            (0, 1, 2),
            tuple(worker.judgment_round for worker in restarted.list_workers()),
        )
        self.assertTrue(all(
            worker.consumed_at == LATER
            for worker in restarted.list_workers()
        ))

    def test_followup_receipt_preserves_first_assignment_timestamp(self) -> None:
        item = self._new_item()
        initial = _intent(item.id, item.episode)
        self.assertTrue(self.store.prepare_action(initial, capacity_limit=2))
        self._begin_action(initial.action_id)
        self.store.complete_action(
            ActionCompletion(
                action_id=initial.action_id,
                state=ActionState.CONFIRMED,
                completed_at=LATER,
                remote_number=None,
                remote_task_id="task-initial",
                error=None,
            )
        )
        assigned = self.store.list_items()[0]
        self.store.update_item(
            replace(assigned, task_state=TaskState.COMPLETED),
            history_event="task-completed",
            summary="The initial task completed.",
            detail={},
        )
        follow_up = _intent(
            item.id,
            item.episode,
            ordinal=1,
            kind=ActionKind.FOLLOW_UP,
        )
        self.assertTrue(self.store.prepare_action(follow_up, capacity_limit=2))
        self._begin_action(follow_up.action_id)
        self.store.complete_action(
            ActionCompletion(
                action_id=follow_up.action_id,
                state=ActionState.CONFIRMED,
                completed_at="2026-09-17T20:10:00Z",
                remote_number=None,
                remote_task_id="task-follow-up",
                error=None,
            )
        )

        current = self.store.list_items()[0]
        self.assertEqual(LATER, current.assignment_confirmed_at)
        self.assertEqual(1, current.followup_count)
        self.assertEqual("task-follow-up", current.task_id)

    def test_uncertain_action_cannot_be_prepared_again(self) -> None:
        item = self._new_item()
        first = _intent(item.id, item.episode)
        self.store.prepare_action(first, capacity_limit=2)
        self._begin_action(first.action_id)
        self.store.complete_action(
            ActionCompletion(
                action_id=first.action_id,
                state=ActionState.UNCERTAIN,
                completed_at=LATER,
                remote_number=None,
                remote_task_id=None,
                error="Unknown remote outcome.",
            )
        )

        with self.assertRaisesRegex(ValueError, "uncertain"):
            self.store.prepare_action(
                _intent(item.id, item.episode, ordinal=2),
                capacity_limit=2,
            )

    def test_stale_episode_inputs_cannot_update_current_item(self) -> None:
        item = self._new_item()
        self.store.update_item(
            replace(
                item,
                phase=ItemPhase.RECOVERED,
                recovered_run_id=102,
                recovered_at=LATER,
            ),
            history_event="recovered",
            summary="Recovered.",
            detail={},
        )
        current = self.store.upsert_failure(
            _run(run_id=103),
            "2026-09-17T20:30:00Z",
        )
        self.assertEqual(2, current.episode)

        with self.assertRaisesRegex(ValueError, "stale episode"):
            self.store.update_item(
                replace(item, phase=ItemPhase.NEEDS_ATTENTION),
                history_event="late-result",
                summary="Late.",
                detail={},
            )
        with self.assertRaisesRegex(ValueError, "stale episode"):
            self.store.reserve_worker(
                _reservation(
                    self.state_directory,
                    current.id,
                    1,
                    item.evidence_fingerprint,
                ),
                capacity_limit=2,
            )

    def test_stale_worker_input_is_explicit_even_with_old_worker_active(self) -> None:
        item = self._new_item()
        old = _reservation(
            self.state_directory,
            item.id,
            item.episode,
            item.evidence_fingerprint,
        )
        self.store.reserve_worker(old, capacity_limit=2)
        self.store.update_item(
            replace(
                item,
                phase=ItemPhase.RECOVERED,
                recovered_run_id=102,
                recovered_at=LATER,
            ),
            history_event="recovered",
            summary="Recovered.",
            detail={},
        )
        current = self.store.upsert_failure(
            _run(run_id=103),
            "2026-09-17T20:30:00Z",
        )

        with self.assertRaisesRegex(ValueError, "stale episode"):
            self.store.reserve_worker(
                replace(old, worker_id="late-worker"),
                capacity_limit=2,
            )
        self.assertEqual(2, current.episode)

    def test_recovery_does_not_erase_physically_running_reservation(self) -> None:
        item = self._new_item()
        reservation = _reservation(
            self.state_directory,
            item.id,
            item.episode,
            item.evidence_fingerprint,
        )
        self.store.reserve_worker(reservation, capacity_limit=2)
        self.store.mark_worker_launch_attempt(
            reservation.worker_id,
            launch_attempted_at=NOW,
        )
        self.store.mark_worker_launched(
            reservation.worker_id,
            pid=12345,
            launched_at=NOW,
        )
        self.store.update_item(
            replace(
                item,
                phase=ItemPhase.RECOVERED,
                recovered_run_id=102,
                recovered_at=LATER,
            ),
            history_event="recovered",
            summary="CI recovered while judgment was running.",
            detail={},
        )

        self.assertEqual(frozenset({item.id}), self.store.active_item_ids())
        self.store.complete_worker(
            WorkerCompletion(
                worker_id=reservation.worker_id,
                state=WorkState.SUPERSEDED,
                completed_at="2026-09-17T20:21:00Z",
                exit_code=0,
                error=None,
            )
        )
        self.assertEqual(frozenset(), self.store.active_item_ids())

    def test_old_episode_completion_records_receipt_without_updating_new_item(self) -> None:
        item = self._new_item()
        reservation = _reservation(
            self.state_directory,
            item.id,
            item.episode,
            item.evidence_fingerprint,
        )
        self.store.reserve_worker(reservation, capacity_limit=2)
        self.store.update_item(
            replace(
                item,
                phase=ItemPhase.RECOVERED,
                recovered_run_id=102,
                recovered_at=LATER,
            ),
            history_event="recovered",
            summary="Recovered.",
            detail={},
        )
        current = self.store.upsert_failure(
            _run(run_id=103),
            "2026-09-17T20:30:00Z",
        )
        completion = WorkerCompletion(
            worker_id=reservation.worker_id,
            state=WorkState.SUPERSEDED,
            completed_at="2026-09-17T20:31:00Z",
            exit_code=0,
            error=None,
        )

        self.store.complete_worker(completion)
        self.store.complete_worker(completion)

        self.assertIs(WorkState.SUPERSEDED, self.store.list_workers()[0].state)
        self.assertEqual(current, self.store.list_items()[0])

    def test_old_episode_action_receipt_does_not_activate_new_episode(self) -> None:
        item = self._new_item()
        intent = _intent(item.id, item.episode)
        self.store.prepare_action(intent, capacity_limit=2)
        self._begin_action(intent.action_id)
        prepared_item = self.store.list_items()[0]
        self.store.update_item(
            replace(
                prepared_item,
                phase=ItemPhase.RECOVERED,
                recovered_run_id=102,
                recovered_at=LATER,
            ),
            history_event="recovered",
            summary="Recovered.",
            detail={},
        )
        current = self.store.upsert_failure(
            _run(run_id=103),
            "2026-09-17T20:30:00Z",
        )
        completion = ActionCompletion(
            action_id=intent.action_id,
            state=ActionState.CONFIRMED,
            completed_at="2026-09-17T20:31:00Z",
            remote_number=None,
            remote_task_id="task-old-episode",
            error=None,
        )

        self.store.complete_action(completion)
        self.store.complete_action(completion)
        action = self.store.list_actions()[0]
        persisted = self.store.list_items()[0]
        self.assertIs(ActionState.CONFIRMED, action.state)
        self.assertEqual("task-old-episode", action.remote_task_id)
        self.assertEqual("task-old-episode", persisted.task_id)
        self.assertIsNone(persisted.task_state)
        self.assertIs(ItemPhase.OBSERVING_FAILURE, persisted.phase)
        self.assertEqual(current.episode, persisted.episode)

    def test_confirmed_follow_up_replaces_task_and_increments_bounded_count(
        self,
    ) -> None:
        item = self._new_item()
        tracked = replace(
            item,
            phase=ItemPhase.READY_FOR_ACTION,
            issue_number=17,
            task_id="task-initial",
            task_state=TaskState.IDLE,
            pull_request_number=23,
            followup_count=1,
        )
        self.store.update_item(
            tracked,
            history_event="follow-up-ready",
            summary="The existing task is idle and the PR head needs follow-up.",
            detail={"taskId": tracked.task_id, "pullRequestNumber": 23},
        )
        intent = _intent(
            item.id,
            item.episode,
            ordinal=2,
            kind=ActionKind.FOLLOW_UP,
        )
        self.store.prepare_action(intent, capacity_limit=2)
        self._begin_action(intent.action_id)
        completion = ActionCompletion(
            action_id=intent.action_id,
            state=ActionState.CONFIRMED,
            completed_at=LATER,
            remote_number=None,
            remote_task_id="task-follow-up",
            error=None,
        )

        self.store.complete_action(completion)

        persisted = self.store.list_items()[0]
        self.assertEqual("task-follow-up", persisted.task_id)
        self.assertIsNone(persisted.task_state)
        self.assertEqual(2, persisted.followup_count)
        self.assertEqual(23, persisted.pull_request_number)
        self.assertEqual(frozenset({item.id}), self.store.active_item_ids())

        terminal = replace(persisted, task_state=TaskState.COMPLETED)
        self.store.update_item(
            terminal,
            history_event="follow-up-completed",
            summary="The follow-up task completed.",
            detail={"taskId": terminal.task_id},
        )
        with self.assertRaisesRegex(ValueError, "follow-up limit"):
            self.store.prepare_action(
                _intent(
                    item.id,
                    item.episode,
                    ordinal=3,
                    kind=ActionKind.FOLLOW_UP,
                ),
                capacity_limit=2,
            )

    def test_invoking_action_from_dead_manager_becomes_uncertain_without_rewrite(
        self,
    ) -> None:
        item = self._new_item()
        intent = _intent(item.id, item.episode)
        self.store.prepare_action(intent, capacity_limit=2)
        writes = 0
        if self.store.begin_action_invocation(
            intent.action_id,
            pass_id="pass-1",
            owner_id="owner-1",
            invoked_at=NOW,
        ):
            writes += 1

        classified = self.store.classify_orphaned_action_invocations(
            current_pass_id="pass-2",
            current_owner_id="owner-2",
            classified_at=LATER,
            error="Manager exited after invocation began.",
        )

        self.assertEqual((intent.action_id,), classified)
        action = self.store.list_actions()[0]
        self.assertIs(ActionState.UNCERTAIN, action.state)
        self.assertEqual(NOW, action.invoked_at)
        self.assertEqual("pass-1", action.invocation_pass_id)
        self.assertEqual("owner-1", action.invocation_owner_id)
        self.assertIs(ItemPhase.NEEDS_ATTENTION, self.store.list_items()[0].phase)
        self.assertFalse(
            self.store.begin_action_invocation(
                intent.action_id,
                pass_id="pass-2",
                owner_id="owner-2",
                invoked_at=LATER,
            )
        )
        with self.assertRaisesRegex(ValueError, "uncertain"):
            self.store.prepare_action(
                _intent(item.id, item.episode, ordinal=2),
                capacity_limit=2,
            )
        self.assertEqual(1, writes)

    def test_action_completion_requires_invocation_compare_and_set(self) -> None:
        item = self._new_item()
        intent = _intent(item.id, item.episode)
        self.store.prepare_action(intent, capacity_limit=2)
        completion = ActionCompletion(
            action_id=intent.action_id,
            state=ActionState.CONFIRMED,
            completed_at=LATER,
            remote_number=None,
            remote_task_id="task-created-77",
            error=None,
        )

        with self.assertRaisesRegex(ValueError, "invoking"):
            self.store.complete_action(completion)
        self.assertTrue(self._begin_action(intent.action_id))
        self.assertFalse(self._begin_action(intent.action_id))
        with self.assertRaisesRegex(ValueError, "remote_number"):
            self.store.complete_action(replace(completion, remote_number=77))
        self.store.complete_action(completion)
        self.store.complete_action(completion)
        persisted = self.store.list_items()[0]
        self.assertEqual("task-created-77", persisted.task_id)
        self.assertIsNone(persisted.task_state)
        self.assertEqual(frozenset({item.id}), self.store.active_item_ids())

    def test_recovered_item_keeps_live_owned_task_capacity_across_episode(
        self,
    ) -> None:
        item = self._new_item()
        intent = _intent(item.id, item.episode)
        self.store.prepare_action(intent, capacity_limit=2)
        self._begin_action(intent.action_id)
        self.store.complete_action(
            ActionCompletion(
                action_id=intent.action_id,
                state=ActionState.CONFIRMED,
                completed_at="2026-09-17T20:02:00Z",
                remote_number=None,
                remote_task_id="task-live-123",
                error=None,
            )
        )
        self.assertEqual(frozenset({item.id}), self.store.active_item_ids())
        active = replace(
            self.store.list_items()[0],
            task_state=TaskState.IN_PROGRESS,
        )
        self.store.update_item(
            active,
            history_event="task-running",
            summary="The owned task is running.",
            detail={"taskId": active.task_id},
        )
        recovered = replace(
            active,
            phase=ItemPhase.RECOVERED,
            recovered_run_id=102,
            recovered_at=LATER,
        )
        self.store.update_item(
            recovered,
            history_event="recovered",
            summary="CI recovered while the owned task is still running.",
            detail={},
        )

        self.assertEqual(frozenset({item.id}), self.store.active_item_ids())
        next_episode = self.store.upsert_failure(
            _run(run_id=103),
            "2026-09-17T20:30:00Z",
        )
        self.assertEqual("task-live-123", next_episode.task_id)
        self.assertIs(TaskState.IN_PROGRESS, next_episode.task_state)
        self.assertEqual(frozenset({item.id}), self.store.active_item_ids())
        self.assertFalse(
            self.store.prepare_action(
                _intent(item.id, next_episode.episode, ordinal=2),
                capacity_limit=2,
            )
        )
        self.assertFalse(
            self.store.reserve_worker(
                _reservation(
                    self.state_directory,
                    item.id,
                    next_episode.episode,
                    next_episode.evidence_fingerprint,
                ),
                capacity_limit=2,
            )
        )

        terminal = replace(
            next_episode,
            task_state=TaskState.COMPLETED,
        )
        self.store.update_item(
            terminal,
            history_event="task-completed",
            summary="The owned task reached a terminal state.",
            detail={"taskId": terminal.task_id},
        )
        self.assertEqual(frozenset(), self.store.active_item_ids())

    def test_followup_budget_resets_per_episode_and_late_receipt_does_not_leak(
        self,
    ) -> None:
        item = self._new_item()
        episode_one = replace(
            item,
            phase=ItemPhase.READY_FOR_ACTION,
            issue_number=17,
            task_id="task-episode-one",
            task_state=TaskState.IDLE,
            pull_request_number=23,
            followup_count=1,
        )
        self.store.update_item(
            episode_one,
            history_event="follow-up-ready",
            summary="Episode one has one prior follow-up.",
            detail={},
        )
        old_intent = _intent(
            item.id,
            item.episode,
            ordinal=2,
            kind=ActionKind.FOLLOW_UP,
        )
        self.store.prepare_action(old_intent, capacity_limit=2)
        self._begin_action(old_intent.action_id)
        self.store.update_item(
            replace(
                self.store.list_items()[0],
                phase=ItemPhase.RECOVERED,
                task_state=TaskState.IN_PROGRESS,
                recovered_run_id=102,
                recovered_at=LATER,
            ),
            history_event="recovered",
            summary="CI recovered while episode-one follow-up was invoking.",
            detail={},
        )
        episode_two = self.store.upsert_failure(
            _run(run_id=103),
            "2026-09-17T20:30:00Z",
        )
        self.assertEqual(2, episode_two.episode)
        self.assertEqual(0, episode_two.followup_count)
        self.assertEqual("task-episode-one", episode_two.task_id)

        self.store.complete_action(
            ActionCompletion(
                action_id=old_intent.action_id,
                state=ActionState.CONFIRMED,
                completed_at="2026-09-17T20:31:00Z",
                remote_number=None,
                remote_task_id="task-late-follow-up",
                error=None,
            )
        )
        after_late = self.store.list_items()[0]
        self.assertEqual("task-late-follow-up", after_late.task_id)
        self.assertEqual(0, after_late.followup_count)

        eligible = replace(after_late, task_state=TaskState.IDLE)
        self.store.update_item(
            eligible,
            history_event="old-task-idle",
            summary="The late episode-one task is now idle.",
            detail={},
        )
        current_intent = _intent(
            item.id,
            episode_two.episode,
            ordinal=1,
            kind=ActionKind.FOLLOW_UP,
        )
        self.assertTrue(
            self.store.prepare_action(current_intent, capacity_limit=2)
        )
        self._begin_action(current_intent.action_id, pass_id="pass-2")
        self.store.complete_action(
            ActionCompletion(
                action_id=current_intent.action_id,
                state=ActionState.CONFIRMED,
                completed_at="2026-09-17T20:32:00Z",
                remote_number=None,
                remote_task_id="task-episode-two-follow-up",
                error=None,
            )
        )
        self.assertEqual(1, self.store.list_items()[0].followup_count)

    def test_malformed_persisted_json_raises_explicitly(self) -> None:
        item = self._new_item()
        database = self.state_directory / "workflow-loop.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "UPDATE workflow_items SET failed_jobs_json = 'not-json' "
                "WHERE id = ?",
                (item.id,),
            )
            connection.commit()

        with self.assertRaisesRegex(ValueError, "failed_jobs_json"):
            self.store.list_items()

    def test_malformed_persisted_action_payload_raises_explicitly(self) -> None:
        item = self._new_item()
        intent = _intent(item.id, item.episode)
        self.store.prepare_action(intent, capacity_limit=2)
        database = self.state_directory / "workflow-loop.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "UPDATE action_attempts SET payload_json = 'not-json' "
                "WHERE action_id = ?",
                (intent.action_id,),
            )
            connection.commit()

        with self.assertRaisesRegex(ValueError, "payload_json"):
            self.store.list_actions()

    def test_pass_lifecycle_is_persisted(self) -> None:
        self.store.start_pass("pass-1", NOW)
        self.store.finish_pass(
            "pass-1",
            completed_at=LATER,
            duration_ms=1_200_000,
            github_request_count=4,
            discovered_items=2,
            progressed_items=1,
            confirmed_assignments=1,
            error=None,
        )

        database = self.state_directory / "workflow-loop.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            row = connection.execute(
                "SELECT duration_ms, confirmed_assignments FROM passes "
                "WHERE pass_id = 'pass-1'"
            ).fetchone()
        self.assertEqual((1_200_000, 1), row)


if __name__ == "__main__":
    unittest.main()
