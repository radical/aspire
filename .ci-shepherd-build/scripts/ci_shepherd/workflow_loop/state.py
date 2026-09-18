from __future__ import annotations

from collections.abc import Collection, Mapping
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from .models import (
    ActionCompletion,
    ActionIntent,
    ActionKind,
    ActionState,
    ActionView,
    HistoryEntry,
    ItemPhase,
    JobKey,
    RunObservation,
    TaskState,
    WorkerCompletion,
    WorkerReservation,
    WorkerView,
    WorkflowItem,
    WorkState,
    canonical_fingerprint,
)


_SCHEMA_VERSION = 4
_DATABASE_NAME = "workflow-loop.sqlite3"
_ITEM_COLUMNS = (
    "id",
    "repository",
    "workflow_id",
    "workflow_path",
    "workflow_name",
    "branch",
    "episode",
    "phase",
    "first_failure_seen_at",
    "last_checked_at",
    "last_progressed_at",
    "read_status",
    "failure_run_id",
    "failure_attempt",
    "failed_jobs_json",
    "evidence_fingerprint",
    "last_judged_fingerprint",
    "wait_run_id",
    "wait_reason",
    "issue_number",
    "task_id",
    "task_state",
    "pull_request_number",
    "external_owner",
    "followup_count",
    "assignment_requested_at",
    "assignment_confirmed_at",
    "recovered_run_id",
    "recovered_at",
    "latest_action",
    "latest_error",
    "scenario_name",
    "case_key",
)
_WORKER_COLUMNS = (
    "worker_id",
    "item_id",
    "episode",
    "evidence_fingerprint",
    "judgment_round",
    "state",
    "session_id",
    "pid",
    "request_path",
    "result_path",
    "detail_path",
    "lifetime_lock_path",
    "queued_at",
    "launch_attempted_at",
    "launched_at",
    "completed_at",
    "consumed_at",
    "exit_code",
    "error",
)
_ACTION_COLUMNS = (
    "action_id",
    "item_id",
    "episode",
    "kind",
    "ordinal",
    "state",
    "payload_json",
    "prepared_at",
    "invoked_at",
    "invocation_pass_id",
    "invocation_owner_id",
    "completed_at",
    "remote_number",
    "remote_task_id",
    "error",
)
_FAILED_CONCLUSIONS = frozenset({"failure", "failed", "timed_out"})
_ACTIVE_TASK_STATES = frozenset(
    {
        TaskState.QUEUED,
        TaskState.IN_PROGRESS,
    }
)


class WorkflowLoopStore:
    def __init__(
        self,
        state_directory: Path,
        *,
        repository: str,
        branch: str,
    ) -> None:
        if not isinstance(state_directory, Path):
            raise ValueError("state_directory must be a pathlib.Path.")
        if not isinstance(repository, str) or not repository.strip():
            raise ValueError("repository must be a nonempty string.")
        if not isinstance(branch, str) or not branch.strip():
            raise ValueError("branch must be a nonempty string.")
        self._state_directory = state_directory
        self._database_path = state_directory / _DATABASE_NAME
        self._repository = repository
        self._branch = branch

    def initialize(
        self,
        *,
        workflow_ids: Collection[int] | None = None,
    ) -> None:
        workflow_scope = _workflow_scope(workflow_ids)
        if self._state_directory.is_symlink():
            raise ValueError("State directory must not be a symlink.")
        self._state_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._state_directory, 0o700)
        if self._database_path.is_symlink():
            raise ValueError("State database must not be a symlink.")

        with self._connect() as connection:
            os.chmod(self._database_path, 0o600)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS meta("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            meta = dict(connection.execute("SELECT key, value FROM meta"))
            if "schema_version" in meta:
                try:
                    schema_version = int(meta["schema_version"])
                except ValueError as error:
                    raise ValueError(
                        "Stored schema version is malformed."
                    ) from error
                if schema_version not in {3, _SCHEMA_VERSION}:
                    raise ValueError(
                        f"Unsupported schema version {schema_version}."
                    )
                if meta.get("repository") != self._repository:
                    raise ValueError(
                        "Stored repository binding contradicts the constructor."
                    )
                if meta.get("branch") != self._branch:
                    raise ValueError(
                        "Stored branch binding contradicts the constructor."
                    )
                if meta.get("workflow_scope") != workflow_scope:
                    raise ValueError(
                        "Configured workflow scope contradicts persisted state: "
                        f"expected {meta.get('workflow_scope')!r}, "
                        f"received {workflow_scope!r}."
                    )
                if schema_version == 3:
                    connection.commit()
                    connection.execute("PRAGMA foreign_keys = OFF")
                    connection.execute("BEGIN IMMEDIATE")
                    self._migrate_v3_to_v4(connection)
                    connection.execute(
                        "UPDATE meta SET value = ? "
                        "WHERE key = 'schema_version'",
                        (str(_SCHEMA_VERSION),),
                    )
                    connection.commit()
                    connection.execute("PRAGMA foreign_keys = ON")
                    connection.execute("BEGIN IMMEDIATE")
            elif meta:
                raise ValueError("State metadata is incomplete.")
            else:
                connection.executemany(
                    "INSERT INTO meta(key, value) VALUES(?, ?)",
                    (
                        ("schema_version", str(_SCHEMA_VERSION)),
                        ("repository", self._repository),
                        ("branch", self._branch),
                        ("workflow_scope", workflow_scope),
                    ),
                )
            self._create_schema(connection)
            self._validate_schema(connection)
            connection.commit()

    def start_pass(self, pass_id: str, started_at: str) -> None:
        _nonempty(pass_id, "pass_id")
        _validate_timestamp(started_at, "started_at")
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO passes(pass_id, started_at) VALUES(?, ?)",
                (pass_id, started_at),
            )

    def finish_pass(
        self,
        pass_id: str,
        *,
        completed_at: str,
        duration_ms: int,
        github_request_count: int,
        discovered_items: int,
        progressed_items: int,
        confirmed_assignments: int,
        error: str | None,
    ) -> None:
        _nonempty(pass_id, "pass_id")
        _validate_timestamp(completed_at, "completed_at")
        counts = (
            duration_ms,
            github_request_count,
            discovered_items,
            progressed_items,
            confirmed_assignments,
        )
        if any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            for value in counts
        ):
            raise ValueError("Pass counts and duration must be nonnegative integers.")
        if error is not None:
            _nonempty(error, "error")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT completed_at, duration_ms, github_request_count, "
                "discovered_items, progressed_items, confirmed_assignments, error "
                "FROM passes WHERE pass_id = ?",
                (pass_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown pass_id {pass_id!r}.")
            values = (
                completed_at,
                duration_ms,
                github_request_count,
                discovered_items,
                progressed_items,
                confirmed_assignments,
                error,
            )
            if row["completed_at"] is not None:
                if tuple(row) == values:
                    return
                raise ValueError("Pass has a conflicting completion receipt.")
            connection.execute(
                "UPDATE passes SET completed_at = ?, duration_ms = ?, "
                "github_request_count = ?, discovered_items = ?, "
                "progressed_items = ?, confirmed_assignments = ?, error = ? "
                "WHERE pass_id = ?",
                (*values, pass_id),
            )

    def list_items(self) -> tuple[WorkflowItem, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {', '.join(_ITEM_COLUMNS)} "
                "FROM workflow_items ORDER BY id"
            ).fetchall()
        return tuple(_item_from_row(row) for row in rows)

    def bind_item_scenario(self, item_id: int, scenario_name: str) -> None:
        _positive(item_id, "item_id")
        _nonempty(scenario_name, "scenario_name")
        with self._connect() as connection:
            item = self._current_item(connection, item_id)
        if item.scenario_name != scenario_name:
            raise ValueError(
                f"Item {item_id} is already bound to scenario "
                f"{item.scenario_name!r}."
            )

    def item_scenario(self, item_id: int) -> str | None:
        _positive(item_id, "item_id")
        with self._connect() as connection:
            item = self._current_item(connection, item_id)
        return item.scenario_name

    def upsert_failure(
        self,
        observation: RunObservation,
        observed_at: str,
        *,
        scenario_name: str = "workflow-failure",
        case_key: str | None = None,
    ) -> WorkflowItem:
        if not isinstance(observation, RunObservation):
            raise ValueError("observation must be a RunObservation.")
        _validate_timestamp(observed_at, "observed_at")
        _nonempty(scenario_name, "scenario_name")
        if case_key is None:
            case_key = f"workflow:{observation.key.workflow_id}"
        _nonempty(case_key, "case_key")
        self._validate_observation_binding(observation)
        failed_jobs = tuple(
            job.key
            for job in observation.jobs
            if (job.conclusion or "").casefold() in _FAILED_CONCLUSIONS
        )
        fingerprint = _observation_fingerprint(observation)

        with self._transaction() as connection:
            row = connection.execute(
                f"SELECT {', '.join(_ITEM_COLUMNS)} FROM workflow_items "
                "WHERE repository = ? AND branch = ? "
                "AND scenario_name = ? AND case_key = ?",
                (
                    self._repository,
                    self._branch,
                    scenario_name,
                    case_key,
                ),
            ).fetchone()
            if row is None:
                cursor = connection.execute(
                    "INSERT INTO workflow_items("
                    "repository, workflow_id, workflow_path, workflow_name, "
                    "branch, episode, phase, first_failure_seen_at, "
                    "last_checked_at, last_progressed_at, read_status, "
                    "failure_run_id, failure_attempt, failed_jobs_json, "
                    "evidence_fingerprint, followup_count, scenario_name, case_key"
                    ") VALUES(?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    (
                        self._repository,
                        observation.key.workflow_id,
                        observation.workflow_path,
                        observation.workflow_name,
                        self._branch,
                        ItemPhase.OBSERVING_FAILURE.value,
                        observed_at,
                        observed_at,
                        observed_at,
                        "unread",
                        observation.run_id,
                        observation.attempt,
                        _job_keys_json(failed_jobs),
                        fingerprint,
                        scenario_name,
                        case_key,
                    ),
                )
                item_id = cursor.lastrowid
                assert item_id is not None
                self._insert_history(
                    connection,
                    item_id,
                    observed_at,
                    "failure-observed",
                    "Observed a failing workflow run.",
                    {
                        "runId": observation.run_id,
                        "attempt": observation.attempt,
                    },
                )
            else:
                current = _item_from_row(row)
                if current.phase is ItemPhase.RECOVERED:
                    live_task = (
                        current.task_id is not None
                        and (
                            current.task_state is None
                            or current.task_state in _ACTIVE_TASK_STATES
                        )
                    )
                    connection.execute(
                        "UPDATE workflow_items SET "
                        "workflow_path = ?, workflow_name = ?, episode = ?, "
                        "phase = ?, first_failure_seen_at = ?, "
                        "last_checked_at = ?, last_progressed_at = ?, "
                        "read_status = ?, failure_run_id = ?, failure_attempt = ?, "
                        "failed_jobs_json = ?, evidence_fingerprint = ?, "
                        "last_judged_fingerprint = NULL, wait_run_id = NULL, "
                        "wait_reason = NULL, issue_number = ?, task_id = ?, "
                        "task_state = ?, pull_request_number = ?, "
                        "external_owner = NULL, followup_count = ?, "
                        "assignment_requested_at = ?, "
                        "assignment_confirmed_at = ?, recovered_run_id = NULL, "
                        "recovered_at = NULL, latest_action = ?, "
                        "latest_error = NULL WHERE id = ?",
                        (
                            observation.workflow_path,
                            observation.workflow_name,
                            current.episode + 1,
                            ItemPhase.OBSERVING_FAILURE.value,
                            observed_at,
                            observed_at,
                            observed_at,
                            "unread",
                            observation.run_id,
                            observation.attempt,
                            _job_keys_json(failed_jobs),
                            fingerprint,
                            current.issue_number if live_task else None,
                            current.task_id if live_task else None,
                            (
                                current.task_state.value
                                if live_task and current.task_state is not None
                                else None
                            ),
                            (
                                current.pull_request_number
                                if live_task
                                else None
                            ),
                            0,
                            None,
                            None,
                            (
                                current.latest_action.value
                                if live_task
                                and current.latest_action is not None
                                else None
                            ),
                            current.id,
                        ),
                    )
                    self._insert_history(
                        connection,
                        current.id,
                        observed_at,
                        "failure-episode-started",
                        "Observed a new failure after confirmed recovery.",
                        {
                            "episode": current.episode + 1,
                            "runId": observation.run_id,
                            "attempt": observation.attempt,
                        },
                    )
                elif current.last_judged_fingerprint is not None:
                    # Once judgment establishes the repair targets, raw polling may
                    # refresh evidence but cannot broaden those targets. The changed
                    # fingerprint makes a late judgment detectably stale while the
                    # episode and first-failure provenance remain stable.
                    connection.execute(
                        "UPDATE workflow_items SET workflow_path = ?, "
                        "workflow_name = ?, last_checked_at = ?, "
                        "failure_run_id = ?, failure_attempt = ?, "
                        "evidence_fingerprint = ? WHERE id = ?",
                        (
                            observation.workflow_path,
                            observation.workflow_name,
                            observed_at,
                            observation.run_id,
                            observation.attempt,
                            fingerprint,
                            current.id,
                        ),
                    )
                else:
                    connection.execute(
                        "UPDATE workflow_items SET workflow_path = ?, "
                        "workflow_name = ?, last_checked_at = ?, "
                        "failure_run_id = ?, failure_attempt = ?, "
                        "failed_jobs_json = ?, evidence_fingerprint = ? "
                        "WHERE id = ?",
                        (
                            observation.workflow_path,
                            observation.workflow_name,
                            observed_at,
                            observation.run_id,
                            observation.attempt,
                            _job_keys_json(failed_jobs),
                            fingerprint,
                            current.id,
                        ),
                    )
                item_id = current.id
            updated = connection.execute(
                f"SELECT {', '.join(_ITEM_COLUMNS)} "
                "FROM workflow_items WHERE id = ?",
                (item_id,),
            ).fetchone()
            assert updated is not None
            return _item_from_row(updated)

    def update_item(
        self,
        item: WorkflowItem,
        *,
        history_event: str,
        summary: str,
        detail: Mapping[str, object],
    ) -> None:
        if not isinstance(item, WorkflowItem):
            raise ValueError("item must be a WorkflowItem.")
        _nonempty(history_event, "history_event")
        _nonempty(summary, "summary")
        detail_json = _mapping_json(detail, "detail")
        with self._transaction() as connection:
            current = self._current_item(connection, item.id)
            self._validate_item_identity(current, item)
            assignments = ", ".join(
                f"{column} = ?" for column in _ITEM_COLUMNS[1:]
            )
            connection.execute(
                f"UPDATE workflow_items SET {assignments} WHERE id = ?",
                (*_item_values(item)[1:], item.id),
            )
            self._insert_history_json(
                connection,
                item.id,
                item.last_checked_at,
                history_event,
                summary,
                detail_json,
            )

    def reserve_worker(
        self,
        reservation: WorkerReservation,
        *,
        capacity_limit: int,
    ) -> bool:
        if not isinstance(reservation, WorkerReservation):
            raise ValueError("reservation must be a WorkerReservation.")
        _positive(capacity_limit, "capacity_limit")
        _validate_worker_paths(reservation, self._state_directory)
        with self._transaction() as connection:
            existing_id = connection.execute(
                f"SELECT {', '.join(_WORKER_COLUMNS)} FROM workers "
                "WHERE worker_id = ?",
                (reservation.worker_id,),
            ).fetchone()
            if existing_id is not None:
                existing = _worker_from_row(
                    existing_id,
                    self._state_directory,
                )
                expected = WorkerView(
                    worker_id=reservation.worker_id,
                    item_id=reservation.item_id,
                    episode=reservation.episode,
                    evidence_fingerprint=reservation.evidence_fingerprint,
                    judgment_round=reservation.judgment_round,
                    session_id=reservation.session_id,
                    state=WorkState.QUEUED,
                    pid=None,
                    request_path=reservation.request_path,
                    result_path=reservation.result_path,
                    detail_path=reservation.detail_path,
                    lifetime_lock_path=reservation.lifetime_lock_path,
                    queued_at=reservation.queued_at,
                    launch_attempted_at=None,
                    launched_at=None,
                    completed_at=None,
                    consumed_at=None,
                    exit_code=None,
                    error=None,
                )
                if existing == expected:
                    return True
                raise ValueError("worker_id has a conflicting reservation.")
            item = self._current_item(connection, reservation.item_id)
            self._validate_episode(item, reservation.episode)
            active_worker = connection.execute(
                "SELECT 1 FROM workers WHERE item_id = ? "
                "AND state IN (?, ?) LIMIT 1",
                (
                    reservation.item_id,
                    WorkState.QUEUED.value,
                    WorkState.RUNNING.value,
                ),
            ).fetchone()
            if active_worker is not None:
                return False
            if item.evidence_fingerprint != reservation.evidence_fingerprint:
                raise ValueError(
                    "Worker reservation evidence fingerprint is stale."
                )
            prior_round = connection.execute(
                "SELECT worker_id FROM workers WHERE item_id = ? "
                "AND episode = ? AND evidence_fingerprint = ? "
                "AND judgment_round = ? LIMIT 1",
                (
                    reservation.item_id,
                    reservation.episode,
                    reservation.evidence_fingerprint,
                    reservation.judgment_round,
                ),
            ).fetchone()
            if prior_round is not None:
                raise ValueError(
                    "This item episode, evidence fingerprint, and judgment "
                    "round already has a worker reservation."
                )
            active = self._active_item_ids(connection)
            if reservation.item_id in active:
                return False
            if len(active) >= capacity_limit:
                return False
            connection.execute(
                "INSERT INTO workers("
                "worker_id, item_id, episode, evidence_fingerprint, "
                "judgment_round, state, "
                "session_id, request_path, result_path, detail_path, "
                "lifetime_lock_path, queued_at"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    reservation.worker_id,
                    reservation.item_id,
                    reservation.episode,
                    reservation.evidence_fingerprint,
                    reservation.judgment_round,
                    WorkState.QUEUED.value,
                    reservation.session_id,
                    reservation.request_path,
                    reservation.result_path,
                    reservation.detail_path,
                    reservation.lifetime_lock_path,
                    reservation.queued_at,
                ),
            )
            connection.execute(
                "UPDATE workflow_items SET phase = ? WHERE id = ?",
                (
                    ItemPhase.JUDGMENT_QUEUED.value,
                    reservation.item_id,
                ),
            )
            return True

    def consume_worker_result(
        self,
        worker_id: str,
        *,
        consumed_at: str,
    ) -> bool:
        _nonempty(worker_id, "worker_id")
        _validate_timestamp(consumed_at, "consumed_at")
        with self._transaction() as connection:
            row = connection.execute(
                f"SELECT {', '.join(_WORKER_COLUMNS)} FROM workers "
                "WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown worker_id {worker_id!r}.")
            worker = _worker_from_row(row, self._state_directory)
            if worker.consumed_at is not None:
                return False
            if worker.state not in {
                WorkState.SUCCEEDED,
                WorkState.FAILED,
                WorkState.INVALID,
                WorkState.SUPERSEDED,
            }:
                raise ValueError("Only a terminal worker result can be consumed.")
            changed = connection.execute(
                "UPDATE workers SET consumed_at = ? "
                "WHERE worker_id = ? AND consumed_at IS NULL",
                (consumed_at, worker_id),
            ).rowcount
            return changed == 1

    def mark_worker_launch_attempt(
        self,
        worker_id: str,
        *,
        launch_attempted_at: str,
    ) -> bool:
        _nonempty(worker_id, "worker_id")
        _validate_timestamp(launch_attempted_at, "launch_attempted_at")
        with self._transaction() as connection:
            row = connection.execute(
                f"SELECT {', '.join(_WORKER_COLUMNS)} FROM workers "
                "WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown worker_id {worker_id!r}.")
            worker = _worker_from_row(row, self._state_directory)
            if worker.launch_attempted_at is not None:
                return False
            if worker.state is not WorkState.QUEUED:
                raise ValueError(
                    "Only a queued worker can record a launch attempt."
                )
            item = self._current_item(connection, worker.item_id)
            self._validate_episode(item, worker.episode)
            changed = connection.execute(
                "UPDATE workers SET launch_attempted_at = ? "
                "WHERE worker_id = ? AND state = ? "
                "AND launch_attempted_at IS NULL",
                (
                    launch_attempted_at,
                    worker_id,
                    WorkState.QUEUED.value,
                ),
            ).rowcount
            return changed == 1

    def mark_worker_launched(
        self,
        worker_id: str,
        *,
        pid: int,
        launched_at: str,
    ) -> None:
        _nonempty(worker_id, "worker_id")
        _positive(pid, "pid")
        _validate_timestamp(launched_at, "launched_at")
        with self._transaction() as connection:
            row = connection.execute(
                f"SELECT {', '.join(_WORKER_COLUMNS)} FROM workers "
                "WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown worker_id {worker_id!r}.")
            worker = _worker_from_row(row, self._state_directory)
            if worker.state is WorkState.RUNNING:
                if worker.pid == pid and worker.launched_at == launched_at:
                    return
                raise ValueError("Worker has conflicting launch metadata.")
            if worker.state is not WorkState.QUEUED:
                raise ValueError("Only a queued worker can be launched.")
            if worker.launch_attempted_at is None:
                raise ValueError(
                    "Worker launch must be attempted durably before Popen."
                )
            connection.execute(
                "UPDATE workers SET state = ?, pid = ?, launched_at = ? "
                "WHERE worker_id = ?",
                (WorkState.RUNNING.value, pid, launched_at, worker_id),
            )
            connection.execute(
                "UPDATE workflow_items SET phase = ? WHERE id = ?",
                (ItemPhase.JUDGMENT_RUNNING.value, worker.item_id),
            )

    def list_workers(self) -> tuple[WorkerView, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {', '.join(_WORKER_COLUMNS)} "
                "FROM workers ORDER BY queued_at, worker_id"
            ).fetchall()
        return tuple(
            _worker_from_row(row, self._state_directory)
            for row in rows
        )

    def complete_worker(self, completion: WorkerCompletion) -> None:
        if not isinstance(completion, WorkerCompletion):
            raise ValueError("completion must be a WorkerCompletion.")
        with self._transaction() as connection:
            row = connection.execute(
                f"SELECT {', '.join(_WORKER_COLUMNS)} FROM workers "
                "WHERE worker_id = ?",
                (completion.worker_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown worker_id {completion.worker_id!r}.")
            worker = _worker_from_row(row, self._state_directory)
            if worker.state in {
                WorkState.SUCCEEDED,
                WorkState.FAILED,
                WorkState.INVALID,
                WorkState.SUPERSEDED,
            }:
                existing = (
                    worker.state,
                    worker.completed_at,
                    worker.exit_code,
                    worker.error,
                )
                received = (
                    completion.state,
                    completion.completed_at,
                    completion.exit_code,
                    completion.error,
                )
                if existing == received:
                    return
                raise ValueError("Worker has a conflicting completion receipt.")
            if (
                completion.state is not WorkState.SUPERSEDED
                and worker.launch_attempted_at is None
            ):
                raise ValueError(
                    "Worker completion requires a durable launch attempt."
                )
            connection.execute(
                "UPDATE workers SET state = ?, completed_at = ?, "
                "exit_code = ?, error = ? WHERE worker_id = ?",
                (
                    completion.state.value,
                    completion.completed_at,
                    completion.exit_code,
                    completion.error,
                    completion.worker_id,
                ),
            )

    def prepare_action(
        self,
        intent: ActionIntent,
        *,
        capacity_limit: int,
    ) -> bool:
        if not isinstance(intent, ActionIntent):
            raise ValueError("intent must be an ActionIntent.")
        _positive(capacity_limit, "capacity_limit")
        payload_json = _mapping_json(intent.payload, "payload")
        with self._transaction() as connection:
            row = connection.execute(
                f"SELECT {', '.join(_ACTION_COLUMNS)} FROM action_attempts "
                "WHERE action_id = ?",
                (intent.action_id,),
            ).fetchone()
            if row is not None:
                existing = _action_from_row(row)
                if existing.state is ActionState.UNCERTAIN:
                    raise ValueError("An uncertain action cannot be retried.")
                if (
                    existing.item_id == intent.item_id
                    and existing.episode == intent.episode
                    and existing.kind is intent.kind
                    and existing.ordinal == intent.ordinal
                    and existing.state is ActionState.PREPARED
                    and dict(existing.payload) == dict(intent.payload)
                    and existing.prepared_at == intent.prepared_at
                ):
                    return True
                raise ValueError("action_id has a conflicting prepared intent.")
            item = self._current_item(connection, intent.item_id)
            self._validate_episode(item, intent.episode)
            if (
                intent.kind is ActionKind.FOLLOW_UP
                and item.followup_count >= 2
            ):
                raise ValueError(
                    "The item has reached the bounded follow-up limit."
                )
            uncertain = connection.execute(
                "SELECT 1 FROM action_attempts WHERE item_id = ? "
                "AND episode = ? AND kind = ? AND state = ? LIMIT 1",
                (
                    intent.item_id,
                    intent.episode,
                    intent.kind.value,
                    ActionState.UNCERTAIN.value,
                ),
            ).fetchone()
            if uncertain is not None:
                raise ValueError("An uncertain action cannot be retried.")
            prior_ordinal = connection.execute(
                "SELECT action_id FROM action_attempts WHERE item_id = ? "
                "AND episode = ? AND kind = ? AND ordinal = ?",
                (
                    intent.item_id,
                    intent.episode,
                    intent.kind.value,
                    intent.ordinal,
                ),
            ).fetchone()
            if prior_ordinal is not None:
                raise ValueError(
                    "This item episode, action kind, and ordinal already has "
                    "an action intent."
                )
            active_action = connection.execute(
                "SELECT 1 FROM action_attempts WHERE item_id = ? "
                "AND state IN (?, ?, ?) LIMIT 1",
                (
                    intent.item_id,
                    ActionState.PREPARED.value,
                    ActionState.INVOKING.value,
                    ActionState.UNCERTAIN.value,
                ),
            ).fetchone()
            if active_action is not None:
                raise ValueError("The item already has an active action intent.")
            active_worker = connection.execute(
                "SELECT 1 FROM workers WHERE item_id = ? "
                "AND state IN (?, ?) LIMIT 1",
                (
                    intent.item_id,
                    WorkState.QUEUED.value,
                    WorkState.RUNNING.value,
                ),
            ).fetchone()
            if active_worker is not None:
                raise ValueError(
                    "An action cannot be prepared while its worker is active."
                )
            active = self._active_item_ids(connection)
            if intent.item_id in active:
                return False
            if len(active) >= capacity_limit:
                return False
            connection.execute(
                "INSERT INTO action_attempts("
                "action_id, item_id, episode, kind, ordinal, state, "
                "payload_json, prepared_at"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    intent.action_id,
                    intent.item_id,
                    intent.episode,
                    intent.kind.value,
                    intent.ordinal,
                    ActionState.PREPARED.value,
                    payload_json,
                    intent.prepared_at,
                ),
            )
            assignment_requested_at = (
                intent.prepared_at
                if intent.kind
                in {ActionKind.ASSIGN_COPILOT, ActionKind.FOLLOW_UP}
                else item.assignment_requested_at
            )
            connection.execute(
                "UPDATE workflow_items SET latest_action = ?, "
                "assignment_requested_at = ? WHERE id = ?",
                (
                    intent.kind.value,
                    assignment_requested_at,
                    intent.item_id,
                ),
            )
            return True

    def begin_action_invocation(
        self,
        action_id: str,
        *,
        pass_id: str,
        owner_id: str,
        invoked_at: str,
    ) -> bool:
        _nonempty(action_id, "action_id")
        _nonempty(pass_id, "pass_id")
        _nonempty(owner_id, "owner_id")
        _validate_timestamp(invoked_at, "invoked_at")
        with self._transaction() as connection:
            row = connection.execute(
                f"SELECT {', '.join(_ACTION_COLUMNS)} FROM action_attempts "
                "WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown action_id {action_id!r}.")
            action = _action_from_row(row)
            if action.state is not ActionState.PREPARED:
                return False
            item = self._current_item(connection, action.item_id)
            self._validate_episode(item, action.episode)
            changed = connection.execute(
                "UPDATE action_attempts SET state = ?, invoked_at = ?, "
                "invocation_pass_id = ?, invocation_owner_id = ? "
                "WHERE action_id = ? AND state = ? AND invoked_at IS NULL",
                (
                    ActionState.INVOKING.value,
                    invoked_at,
                    pass_id,
                    owner_id,
                    action_id,
                    ActionState.PREPARED.value,
                ),
            ).rowcount
            return changed == 1

    def classify_orphaned_action_invocations(
        self,
        *,
        current_pass_id: str,
        current_owner_id: str,
        classified_at: str,
        error: str,
    ) -> tuple[str, ...]:
        _nonempty(current_pass_id, "current_pass_id")
        _nonempty(current_owner_id, "current_owner_id")
        _validate_timestamp(classified_at, "classified_at")
        _nonempty(error, "error")
        classified: list[str] = []
        with self._transaction() as connection:
            rows = connection.execute(
                f"SELECT {', '.join(_ACTION_COLUMNS)} FROM action_attempts "
                "WHERE state = ? AND NOT (invocation_pass_id = ? "
                "AND invocation_owner_id = ?) ORDER BY prepared_at, action_id",
                (
                    ActionState.INVOKING.value,
                    current_pass_id,
                    current_owner_id,
                ),
            ).fetchall()
            for row in rows:
                action = _action_from_row(row)
                changed = connection.execute(
                    "UPDATE action_attempts SET state = ?, completed_at = ?, "
                    "error = ? WHERE action_id = ? AND state = ?",
                    (
                        ActionState.UNCERTAIN.value,
                        classified_at,
                        error,
                        action.action_id,
                        ActionState.INVOKING.value,
                    ),
                ).rowcount
                if changed != 1:
                    continue
                classified.append(action.action_id)
                item = self._current_item(connection, action.item_id)
                if item.episode == action.episode:
                    connection.execute(
                        "UPDATE workflow_items SET phase = ?, latest_error = ? "
                        "WHERE id = ?",
                        (
                            ItemPhase.NEEDS_ATTENTION.value,
                            error,
                            action.item_id,
                        ),
                    )
                    self._insert_history(
                        connection,
                        action.item_id,
                        classified_at,
                        "action-invocation-uncertain",
                        "A prior manager stopped after action invocation began.",
                        {
                            "actionId": action.action_id,
                            "invocationPassId": action.invocation_pass_id,
                            "invocationOwnerId": action.invocation_owner_id,
                        },
                    )
        return tuple(classified)

    def list_actions(self) -> tuple[ActionView, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {', '.join(_ACTION_COLUMNS)} "
                "FROM action_attempts ORDER BY prepared_at, action_id"
            ).fetchall()
        return tuple(_action_from_row(row) for row in rows)

    def complete_action(self, completion: ActionCompletion) -> None:
        if not isinstance(completion, ActionCompletion):
            raise ValueError("completion must be an ActionCompletion.")
        with self._transaction() as connection:
            row = connection.execute(
                f"SELECT {', '.join(_ACTION_COLUMNS)} FROM action_attempts "
                "WHERE action_id = ?",
                (completion.action_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown action_id {completion.action_id!r}.")
            action = _action_from_row(row)
            if action.state not in {
                ActionState.PREPARED,
                ActionState.INVOKING,
            }:
                existing = (
                    action.state,
                    action.completed_at,
                    action.remote_number,
                    action.remote_task_id,
                    action.error,
                )
                received = (
                    completion.state,
                    completion.completed_at,
                    completion.remote_number,
                    completion.remote_task_id,
                    completion.error,
                )
                if existing == received:
                    return
                raise ValueError("Action has a conflicting completion receipt.")
            if (
                action.state is ActionState.PREPARED
                and completion.state
                not in {ActionState.REJECTED, ActionState.SUPERSEDED}
            ):
                raise ValueError(
                    "Confirmed or uncertain completion requires an invoking action."
                )
            if (
                action.state is ActionState.INVOKING
                and completion.state is ActionState.SUPERSEDED
            ):
                raise ValueError(
                    "An invoking action cannot be superseded as if it never ran."
                )
            confirmed_task = (
                completion.state is ActionState.CONFIRMED
                and action.kind
                in {ActionKind.ASSIGN_COPILOT, ActionKind.FOLLOW_UP}
            )
            if confirmed_task and completion.remote_task_id is None:
                raise ValueError(
                    "Confirmed task creation requires remote_task_id."
                )
            if confirmed_task and completion.remote_number is not None:
                raise ValueError(
                    "Confirmed task creation cannot use remote_number."
                )
            if not confirmed_task and completion.remote_task_id is not None:
                raise ValueError(
                    "remote_task_id is only valid for confirmed task creation."
                )
            changed = connection.execute(
                "UPDATE action_attempts SET state = ?, completed_at = ?, "
                "remote_number = ?, remote_task_id = ?, error = ? "
                "WHERE action_id = ? AND state = ?",
                (
                    completion.state.value,
                    completion.completed_at,
                    completion.remote_number,
                    completion.remote_task_id,
                    completion.error,
                    completion.action_id,
                    action.state.value,
                ),
            ).rowcount
            if changed != 1:
                raise ValueError("Action state changed before completion.")
            item = self._current_item(connection, action.item_id)
            if confirmed_task:
                if (
                    action.kind is ActionKind.FOLLOW_UP
                    and item.episode == action.episode
                    and item.followup_count >= 2
                ):
                    raise ValueError(
                        "The item has reached the bounded follow-up limit."
                    )
                phase = (
                    ItemPhase.COPILOT_ACTIVE
                    if item.episode == action.episode
                    else item.phase
                )
                followup_count = (
                    item.followup_count + 1
                    if (
                        action.kind is ActionKind.FOLLOW_UP
                        and item.episode == action.episode
                    )
                    else item.followup_count
                )
                if action.kind is ActionKind.ASSIGN_COPILOT:
                    connection.execute(
                        "UPDATE workflow_items SET phase = ?, task_id = ?, "
                        "task_state = NULL, followup_count = ?, "
                        "assignment_confirmed_at = COALESCE("
                        "assignment_confirmed_at, ?), latest_action = ?, "
                        "latest_error = NULL WHERE id = ?",
                        (
                            phase.value,
                            completion.remote_task_id,
                            followup_count,
                            completion.completed_at,
                            action.kind.value,
                            action.item_id,
                        ),
                    )
                else:
                    connection.execute(
                        "UPDATE workflow_items SET phase = ?, task_id = ?, "
                        "task_state = NULL, followup_count = ?, "
                        "latest_action = ?, latest_error = NULL WHERE id = ?",
                        (
                            phase.value,
                            completion.remote_task_id,
                            followup_count,
                            action.kind.value,
                            action.item_id,
                        ),
                    )
                return
            if item.episode != action.episode:
                return
            if (
                completion.state is ActionState.CONFIRMED
                and action.kind
                in {ActionKind.ASSIGN_COPILOT, ActionKind.FOLLOW_UP}
            ):
                connection.execute(
                    "UPDATE workflow_items SET phase = ?, "
                    "assignment_confirmed_at = ?, latest_error = NULL "
                    "WHERE id = ?",
                    (
                        ItemPhase.COPILOT_ACTIVE.value,
                        completion.completed_at,
                        action.item_id,
                    ),
                )
            elif completion.error is not None:
                connection.execute(
                    "UPDATE workflow_items SET latest_error = ? WHERE id = ?",
                    (completion.error, action.item_id),
                )

    def active_item_ids(self) -> frozenset[int]:
        with self._connect() as connection:
            return self._active_item_ids(connection)

    def recent_history(
        self,
        item_id: int,
        limit: int = 10,
    ) -> tuple[HistoryEntry, ...]:
        _positive(item_id, "item_id")
        _positive(limit, "limit")
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM workflow_items WHERE id = ?",
                (item_id,),
            ).fetchone() is None:
                raise ValueError(f"Unknown item_id {item_id}.")
            rows = connection.execute(
                "SELECT sequence, item_id, recorded_at, event, summary, detail_json "
                "FROM item_history WHERE item_id = ? "
                "ORDER BY sequence DESC LIMIT ?",
                (item_id, limit),
            ).fetchall()
        return tuple(
            HistoryEntry(
                sequence=row["sequence"],
                item_id=row["item_id"],
                recorded_at=row["recorded_at"],
                event=row["event"],
                summary=row["summary"],
                detail=_json_object(row["detail_json"], "detail_json"),
            )
            for row in rows
        )

    def record_history(
        self,
        item_id: int,
        *,
        recorded_at: str,
        event: str,
        summary: str,
        detail: Mapping[str, object],
    ) -> None:
        _positive(item_id, "item_id")
        _validate_timestamp(recorded_at, "recorded_at")
        _nonempty(event, "event")
        _nonempty(summary, "summary")
        detail_json = _mapping_json(detail, "detail")
        with self._transaction() as connection:
            self._current_item(connection, item_id)
            existing = connection.execute(
                "SELECT 1 FROM item_history WHERE item_id = ? "
                "AND event = ? AND detail_json = ? LIMIT 1",
                (item_id, event, detail_json),
            ).fetchone()
            if existing is not None:
                return
            self._insert_history_json(
                connection,
                item_id,
                recorded_at,
                event,
                summary,
                detail_json,
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self._database_path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        finally:
            connection.close()

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        script = """
CREATE TABLE IF NOT EXISTS workflow_items(
    id INTEGER PRIMARY KEY,
    repository TEXT NOT NULL,
    workflow_id INTEGER NOT NULL CHECK(workflow_id > 0),
    workflow_path TEXT NOT NULL,
    workflow_name TEXT NOT NULL,
    branch TEXT NOT NULL,
    episode INTEGER NOT NULL CHECK(episode > 0),
    phase TEXT NOT NULL,
    first_failure_seen_at TEXT NOT NULL,
    last_checked_at TEXT NOT NULL,
    last_progressed_at TEXT NOT NULL,
    read_status TEXT NOT NULL,
    failure_run_id INTEGER NOT NULL CHECK(failure_run_id > 0),
    failure_attempt INTEGER NOT NULL CHECK(failure_attempt > 0),
    failed_jobs_json TEXT NOT NULL,
    evidence_fingerprint TEXT NOT NULL,
    last_judged_fingerprint TEXT,
    wait_run_id INTEGER CHECK(wait_run_id > 0),
    wait_reason TEXT,
    issue_number INTEGER CHECK(issue_number > 0),
    task_id TEXT,
    task_state TEXT,
    pull_request_number INTEGER CHECK(pull_request_number > 0),
    external_owner TEXT,
    followup_count INTEGER NOT NULL DEFAULT 0 CHECK(followup_count >= 0),
    assignment_requested_at TEXT,
    assignment_confirmed_at TEXT,
    recovered_run_id INTEGER CHECK(recovered_run_id > 0),
    recovered_at TEXT,
    latest_action TEXT,
    latest_error TEXT,
    scenario_name TEXT NOT NULL,
    case_key TEXT NOT NULL,
    UNIQUE(repository, branch, scenario_name, case_key)
);
CREATE TABLE IF NOT EXISTS workers(
    worker_id TEXT PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES workflow_items(id),
    episode INTEGER NOT NULL CHECK(episode > 0),
    evidence_fingerprint TEXT NOT NULL,
    judgment_round INTEGER NOT NULL CHECK(judgment_round >= 0),
    state TEXT NOT NULL,
    session_id TEXT NOT NULL,
    pid INTEGER CHECK(pid > 0),
    request_path TEXT NOT NULL,
    result_path TEXT NOT NULL,
    detail_path TEXT NOT NULL,
    lifetime_lock_path TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    launch_attempted_at TEXT,
    launched_at TEXT,
    completed_at TEXT,
    consumed_at TEXT,
    exit_code INTEGER,
    error TEXT,
    UNIQUE(item_id, episode, evidence_fingerprint, judgment_round)
);
CREATE TABLE IF NOT EXISTS action_attempts(
    action_id TEXT PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES workflow_items(id),
    episode INTEGER NOT NULL CHECK(episode > 0),
    kind TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal > 0),
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prepared_at TEXT NOT NULL,
    invoked_at TEXT,
    invocation_pass_id TEXT,
    invocation_owner_id TEXT,
    completed_at TEXT,
    remote_number INTEGER CHECK(remote_number > 0),
    remote_task_id TEXT,
    error TEXT,
    UNIQUE(item_id, episode, kind, ordinal)
);
CREATE TABLE IF NOT EXISTS item_history(
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES workflow_items(id),
    recorded_at TEXT NOT NULL,
    event TEXT NOT NULL,
    summary TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS passes(
    pass_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_ms INTEGER CHECK(duration_ms >= 0),
    github_request_count INTEGER NOT NULL DEFAULT 0
        CHECK(github_request_count >= 0),
    discovered_items INTEGER NOT NULL DEFAULT 0 CHECK(discovered_items >= 0),
    progressed_items INTEGER NOT NULL DEFAULT 0 CHECK(progressed_items >= 0),
    confirmed_assignments INTEGER NOT NULL DEFAULT 0
        CHECK(confirmed_assignments >= 0),
    error TEXT
);
"""
        for statement in script.split(";"):
            if statement.strip():
                connection.execute(statement)

    @staticmethod
    def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
CREATE TABLE workflow_items_v4(
    id INTEGER PRIMARY KEY,
    repository TEXT NOT NULL,
    workflow_id INTEGER NOT NULL CHECK(workflow_id > 0),
    workflow_path TEXT NOT NULL,
    workflow_name TEXT NOT NULL,
    branch TEXT NOT NULL,
    episode INTEGER NOT NULL CHECK(episode > 0),
    phase TEXT NOT NULL,
    first_failure_seen_at TEXT NOT NULL,
    last_checked_at TEXT NOT NULL,
    last_progressed_at TEXT NOT NULL,
    read_status TEXT NOT NULL,
    failure_run_id INTEGER NOT NULL CHECK(failure_run_id > 0),
    failure_attempt INTEGER NOT NULL CHECK(failure_attempt > 0),
    failed_jobs_json TEXT NOT NULL,
    evidence_fingerprint TEXT NOT NULL,
    last_judged_fingerprint TEXT,
    wait_run_id INTEGER CHECK(wait_run_id > 0),
    wait_reason TEXT,
    issue_number INTEGER CHECK(issue_number > 0),
    task_id TEXT,
    task_state TEXT,
    pull_request_number INTEGER CHECK(pull_request_number > 0),
    external_owner TEXT,
    followup_count INTEGER NOT NULL DEFAULT 0 CHECK(followup_count >= 0),
    assignment_requested_at TEXT,
    assignment_confirmed_at TEXT,
    recovered_run_id INTEGER CHECK(recovered_run_id > 0),
    recovered_at TEXT,
    latest_action TEXT,
    latest_error TEXT,
    scenario_name TEXT NOT NULL,
    case_key TEXT NOT NULL,
    UNIQUE(repository, branch, scenario_name, case_key)
)
"""
        )
        has_scenario_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'item_scenarios'"
        ).fetchone() is not None
        if has_scenario_table:
            connection.execute(
                """
INSERT INTO workflow_items_v4
SELECT w.*, COALESCE(s.scenario_name, 'workflow-failure'),
       'workflow:' || w.workflow_id
FROM workflow_items AS w
LEFT JOIN item_scenarios AS s ON s.item_id = w.id
"""
            )
            connection.execute("DROP TABLE item_scenarios")
        else:
            connection.execute(
                """
INSERT INTO workflow_items_v4
SELECT w.*, 'workflow-failure', 'workflow:' || w.workflow_id
FROM workflow_items AS w
"""
            )
        connection.execute("DROP TABLE workflow_items")
        connection.execute(
            "ALTER TABLE workflow_items_v4 RENAME TO workflow_items"
        )

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        expected_columns = {
            "meta": ("key", "value"),
            "workflow_items": _ITEM_COLUMNS,
            "workers": _WORKER_COLUMNS,
            "action_attempts": _ACTION_COLUMNS,
            "item_history": (
                "sequence",
                "item_id",
                "recorded_at",
                "event",
                "summary",
                "detail_json",
            ),
            "passes": (
                "pass_id",
                "started_at",
                "completed_at",
                "duration_ms",
                "github_request_count",
                "discovered_items",
                "progressed_items",
                "confirmed_assignments",
                "error",
            ),
        }
        for table, expected in expected_columns.items():
            actual = tuple(
                row["name"]
                for row in connection.execute(
                    f"PRAGMA table_info({table})"
                )
            )
            if actual != expected:
                raise ValueError(
                    f"Stored schema for {table} is incompatible; "
                    f"expected columns {expected}, found {actual}."
                )

    def _validate_observation_binding(self, observation: RunObservation) -> None:
        if observation.key.repository != self._repository:
            raise ValueError("Observation repository contradicts the store binding.")
        if observation.key.branch != self._branch:
            raise ValueError("Observation branch contradicts the store binding.")

    def _current_item(
        self,
        connection: sqlite3.Connection,
        item_id: int,
    ) -> WorkflowItem:
        _positive(item_id, "item_id")
        row = connection.execute(
            f"SELECT {', '.join(_ITEM_COLUMNS)} "
            "FROM workflow_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown item_id {item_id}.")
        return _item_from_row(row)

    def _validate_item_identity(
        self,
        current: WorkflowItem,
        item: WorkflowItem,
    ) -> None:
        if item.repository != self._repository or item.branch != self._branch:
            raise ValueError("Item contradicts the store binding.")
        if (
            item.id != current.id
            or item.repository != current.repository
            or item.workflow_id != current.workflow_id
            or item.branch != current.branch
            or item.scenario_name != current.scenario_name
            or item.case_key != current.case_key
        ):
            raise ValueError("Item identity does not match persisted state.")
        self._validate_episode(current, item.episode)
        if item.evidence_fingerprint != current.evidence_fingerprint:
            raise ValueError(
                "Item has stale evidence fingerprint "
                f"{item.evidence_fingerprint}; current evidence is "
                f"{current.evidence_fingerprint}."
            )

    @staticmethod
    def _validate_episode(item: WorkflowItem, episode: int) -> None:
        if item.episode != episode:
            raise ValueError(
                f"Input has stale episode {episode}; current episode is "
                f"{item.episode}."
            )

    @staticmethod
    def _insert_history(
        connection: sqlite3.Connection,
        item_id: int,
        recorded_at: str,
        event: str,
        summary: str,
        detail: Mapping[str, object],
    ) -> None:
        WorkflowLoopStore._insert_history_json(
            connection,
            item_id,
            recorded_at,
            event,
            summary,
            _mapping_json(detail, "detail"),
        )

    @staticmethod
    def _insert_history_json(
        connection: sqlite3.Connection,
        item_id: int,
        recorded_at: str,
        event: str,
        summary: str,
        detail_json: str,
    ) -> None:
        connection.execute(
            "INSERT INTO item_history("
            "item_id, recorded_at, event, summary, detail_json"
            ") VALUES(?, ?, ?, ?, ?)",
            (item_id, recorded_at, event, summary, detail_json),
        )

    @staticmethod
    def _active_item_ids(
        connection: sqlite3.Connection,
    ) -> frozenset[int]:
        rows = connection.execute(
            "SELECT item_id FROM workers WHERE state IN (?, ?) "
            "UNION SELECT item_id FROM action_attempts WHERE state IN (?, ?, ?) "
            "UNION SELECT id AS item_id FROM workflow_items "
            "WHERE task_id IS NOT NULL AND "
            "(task_state IS NULL OR task_state IN (?, ?)) "
            "UNION SELECT id AS item_id FROM workflow_items "
            "WHERE phase = ? AND task_id IS NULL",
            (
                WorkState.QUEUED.value,
                WorkState.RUNNING.value,
                ActionState.PREPARED.value,
                ActionState.INVOKING.value,
                ActionState.UNCERTAIN.value,
                TaskState.QUEUED.value,
                TaskState.IN_PROGRESS.value,
                ItemPhase.COPILOT_ACTIVE.value,
            ),
        ).fetchall()
        return frozenset(row["item_id"] for row in rows)


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string.")
    return value


def _positive(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _validate_worker_paths(
    worker: WorkerReservation | WorkerView,
    state_directory: Path,
) -> None:
    state_root = state_directory.resolve(strict=True)
    paths = {
        "request_path": worker.request_path,
        "result_path": worker.result_path,
        "detail_path": worker.detail_path,
        "lifetime_lock_path": worker.lifetime_lock_path,
    }
    resolved_paths: dict[str, Path] = {}
    for name, value in paths.items():
        path = Path(value)
        if not path.is_absolute():
            raise ValueError(
                f"worker path {name} must be an absolute path inside "
                "the state directory."
            )
        resolved = path.resolve(strict=False)
        if str(path) != str(resolved):
            raise ValueError(
                f"worker path {name} must be canonical and contain no "
                "symlink or traversal components."
            )
        try:
            resolved.relative_to(state_root)
        except ValueError as error:
            raise ValueError(
                f"worker path {name} must be inside the state directory."
            ) from error
        if resolved == state_root:
            raise ValueError(
                f"worker path {name} must identify a file below "
                "the state directory."
            )
        resolved_paths[name] = resolved
    if len(set(resolved_paths.values())) != len(resolved_paths):
        raise ValueError("worker paths must identify distinct files.")


def _validate_timestamp(value: object, name: str) -> str:
    from .models import _timestamp

    return _timestamp(value, name)


def _mapping_json(value: object, name: str) -> str:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise ValueError(f"{name} must be a string-keyed mapping.")
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must contain JSON-compatible values.") from error
    decoded = _json_object(encoded, name)
    if not isinstance(decoded, dict):
        raise ValueError(f"{name} must encode a JSON object.")
    return encoded


def _json_object(text: object, name: str) -> dict[str, Any]:
    if not isinstance(text, str):
        raise ValueError(f"{name} must be stored as JSON text.")

    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{name} contains duplicate key {key!r}.")
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=pairs_hook)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"Stored {name} is malformed.") from error
    if not isinstance(value, dict):
        raise ValueError(f"Stored {name} must be a JSON object.")
    return value


def _json_array(text: object, name: str) -> list[object]:
    if not isinstance(text, str):
        raise ValueError(f"{name} must be stored as JSON text.")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"Stored {name} is malformed.") from error
    if not isinstance(value, list):
        raise ValueError(f"Stored {name} must be a JSON array.")
    return value


def _job_keys_json(keys: tuple[JobKey, ...]) -> str:
    return json.dumps(
        [
            {"name": key.name, "runnerLabels": list(key.runner_labels)}
            for key in keys
        ],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _job_keys_from_json(text: object) -> tuple[JobKey, ...]:
    values = _json_array(text, "failed_jobs_json")
    keys: list[JobKey] = []
    for value in values:
        if not isinstance(value, dict) or frozenset(value) != {
            "name",
            "runnerLabels",
        }:
            raise ValueError("Stored failed_jobs_json has an invalid job key.")
        labels = value["runnerLabels"]
        if not isinstance(labels, list):
            raise ValueError("Stored failed_jobs_json has invalid runner labels.")
        try:
            keys.append(
                JobKey(
                    name=value["name"],
                    runner_labels=tuple(labels),
                )
            )
        except ValueError as error:
            raise ValueError(
                "Stored failed_jobs_json has an invalid job key."
            ) from error
    return tuple(keys)


def _observation_fingerprint(observation: RunObservation) -> str:
    return canonical_fingerprint(
        {
            "repository": observation.key.repository,
            "workflowId": observation.key.workflow_id,
            "branch": observation.key.branch,
            "workflowPath": observation.workflow_path,
            "runId": observation.run_id,
            "attempt": observation.attempt,
            "headSha": observation.head_sha,
            "status": observation.status,
            "conclusion": observation.conclusion,
            "jobsComplete": observation.jobs_complete,
            "jobs": [
                {
                    "jobId": job.job_id,
                    "name": job.key.name,
                    "runnerLabels": list(job.key.runner_labels),
                    "status": job.status,
                    "conclusion": job.conclusion,
                }
                for job in observation.jobs
            ],
        }
    )


def _item_values(item: WorkflowItem) -> tuple[object, ...]:
    return (
        item.id,
        item.repository,
        item.workflow_id,
        item.workflow_path,
        item.workflow_name,
        item.branch,
        item.episode,
        item.phase.value,
        item.first_failure_seen_at,
        item.last_checked_at,
        item.last_progressed_at,
        item.read_status,
        item.failure_run_id,
        item.failure_attempt,
        _job_keys_json(item.failed_jobs),
        item.evidence_fingerprint,
        item.last_judged_fingerprint,
        item.wait_run_id,
        item.wait_reason,
        item.issue_number,
        item.task_id,
        item.task_state.value if item.task_state is not None else None,
        item.pull_request_number,
        item.external_owner,
        item.followup_count,
        item.assignment_requested_at,
        item.assignment_confirmed_at,
        item.recovered_run_id,
        item.recovered_at,
        item.latest_action.value if item.latest_action is not None else None,
        item.latest_error,
        item.scenario_name,
        item.case_key,
    )


def _item_from_row(row: sqlite3.Row) -> WorkflowItem:
    failed_jobs = _job_keys_from_json(row["failed_jobs_json"])
    try:
        phase = ItemPhase(row["phase"])
    except ValueError as error:
        raise ValueError("Stored workflow item phase is invalid.") from error
    latest_action_value = row["latest_action"]
    try:
        latest_action = (
            ActionKind(latest_action_value)
            if latest_action_value is not None
            else None
        )
    except ValueError as error:
        raise ValueError("Stored workflow item latest_action is invalid.") from error
    task_state_value = row["task_state"]
    try:
        task_state = (
            TaskState(task_state_value)
            if task_state_value is not None
            else None
        )
    except ValueError as error:
        raise ValueError("Stored workflow item task_state is invalid.") from error
    try:
        return WorkflowItem(
            id=row["id"],
            repository=row["repository"],
            workflow_id=row["workflow_id"],
            workflow_path=row["workflow_path"],
            workflow_name=row["workflow_name"],
            branch=row["branch"],
            episode=row["episode"],
            phase=phase,
            first_failure_seen_at=row["first_failure_seen_at"],
            last_checked_at=row["last_checked_at"],
            last_progressed_at=row["last_progressed_at"],
            read_status=row["read_status"],
            failure_run_id=row["failure_run_id"],
            failure_attempt=row["failure_attempt"],
            failed_jobs=failed_jobs,
            evidence_fingerprint=row["evidence_fingerprint"],
            last_judged_fingerprint=row["last_judged_fingerprint"],
            wait_run_id=row["wait_run_id"],
            wait_reason=row["wait_reason"],
            issue_number=row["issue_number"],
            task_id=row["task_id"],
            task_state=task_state,
            pull_request_number=row["pull_request_number"],
            external_owner=row["external_owner"],
            followup_count=row["followup_count"],
            assignment_requested_at=row["assignment_requested_at"],
            assignment_confirmed_at=row["assignment_confirmed_at"],
            recovered_run_id=row["recovered_run_id"],
            recovered_at=row["recovered_at"],
            latest_action=latest_action,
            latest_error=row["latest_error"],
            scenario_name=row["scenario_name"],
            case_key=row["case_key"],
        )
    except ValueError as error:
        raise ValueError("Stored workflow item is invalid.") from error


def _worker_from_row(
    row: sqlite3.Row,
    state_directory: Path,
) -> WorkerView:
    try:
        state = WorkState(row["state"])
        worker = WorkerView(
            worker_id=row["worker_id"],
            item_id=row["item_id"],
            episode=row["episode"],
            evidence_fingerprint=row["evidence_fingerprint"],
            judgment_round=row["judgment_round"],
            session_id=row["session_id"],
            state=state,
            pid=row["pid"],
            request_path=row["request_path"],
            result_path=row["result_path"],
            detail_path=row["detail_path"],
            lifetime_lock_path=row["lifetime_lock_path"],
            queued_at=row["queued_at"],
            launch_attempted_at=row["launch_attempted_at"],
            launched_at=row["launched_at"],
            completed_at=row["completed_at"],
            consumed_at=row["consumed_at"],
            exit_code=row["exit_code"],
            error=row["error"],
        )
    except ValueError as error:
        raise ValueError("Stored worker is invalid.") from error
    _validate_worker_paths(worker, state_directory)
    return worker


def _workflow_scope(workflow_ids: Collection[int] | None) -> str:
    if workflow_ids is None:
        return "all"
    values = tuple(sorted(set(workflow_ids)))
    if any(
        not isinstance(workflow_id, int)
        or isinstance(workflow_id, bool)
        or workflow_id < 1
        for workflow_id in values
    ):
        raise ValueError("workflow_ids must contain positive integers.")
    return json.dumps(values, separators=(",", ":"))


def _action_from_row(row: sqlite3.Row) -> ActionView:
    payload = _json_object(row["payload_json"], "payload_json")
    try:
        return ActionView(
            action_id=row["action_id"],
            item_id=row["item_id"],
            episode=row["episode"],
            kind=ActionKind(row["kind"]),
            ordinal=row["ordinal"],
            state=ActionState(row["state"]),
            payload=payload,
            prepared_at=row["prepared_at"],
            invoked_at=row["invoked_at"],
            invocation_pass_id=row["invocation_pass_id"],
            invocation_owner_id=row["invocation_owner_id"],
            completed_at=row["completed_at"],
            remote_number=row["remote_number"],
            remote_task_id=row["remote_task_id"],
            error=row["error"],
        )
    except ValueError as error:
        raise ValueError("Stored action is invalid.") from error
