from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
import itertools
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from ci_shepherd.workflow_loop.manager import (
    EffectMode,
    WorkflowLoopManager,
    _judgment_request,
)
from ci_shepherd.workflow_loop.models import (
    ItemPhase,
    JudgmentDecision,
    JudgmentResult,
    TaskState,
    WorkerCompletion,
    WorkerReservation,
    WorkState,
)
from ci_shepherd.workflow_loop.reader import ItemRefresh, ReadError, ReaderSnapshot
from ci_shepherd.workflow_loop.reader import RunDetailResult
from ci_shepherd.workflow_loop.reader import WorkflowReader
from ci_shepherd.workflow_loop.report import render_status
from ci_shepherd.workflow_loop.state import WorkflowLoopStore
from ci_shepherd.workflow_loop.worker import (
    JudgmentWorkerLauncher,
    WorkerLaunchResult,
    WorkerLaunchStatus,
    WorkerObservation,
    WorkerObservationStatus,
    WorkerPacketPaths,
    WorkerPreparationResult,
    WorkerPreparationStatus,
)
from ci_shepherd.workflow_loop.writer import WorkflowWriteResult, WorkflowWriter
from ci_shepherd.workflow_loop.lifetime import is_lifetime_active
from test_workflow_loop_worker import LATER, NOW, _request
from test_workflow_loop_reader import (
    BRANCH,
    REPOSITORY,
    WORKFLOW_ID,
    base_responses,
    job,
    pull,
    repository,
    run,
    run_endpoint,
    task_record,
    workflow,
)
from workflow_loop_fakes import (
    EndpointClient,
    PagedResponse,
    SequenceResponse,
    api_error,
)


class _Reader:
    def __init__(self, refresh: ItemRefresh) -> None:
        self.refresh = refresh
        self.observe_calls = 0
        self.detail_calls = 0

    def observe(self, **kwargs: object) -> ReaderSnapshot:
        self.observe_calls += 1
        return ReaderSnapshot(
            observed_at=NOW,
            repository="owner/repo",
            repository_id=123,
            branch="main",
            default_branch="main",
            workflows=(),
            tracked_wait_runs=(),
            complete=True,
            errors=(),
            request_count=2,
        )

    def refresh_item(self, item, *, action=None) -> ItemRefresh:
        failure = self.refresh.failure_run
        if action is None and failure is not None:
            failure = replace(failure, jobs_complete=False, jobs=())
        return replace(
            self.refresh,
            item_id=item.id,
            failure_run=failure,
            pre_write=action is not None,
        )

    def read_run_details(self, run, *, established_jobs=()) -> RunDetailResult:
        self.detail_calls += 1
        failure = self.refresh.failure_run
        assert failure is not None
        return RunDetailResult(
            run=failure,
            complete=True,
            recovery="failed",
            matched_job_ids=tuple(job.job_id for job in failure.jobs),
            missing_jobs=(),
            logged_job_ids=tuple(job.job_id for job in failure.jobs),
            truncated_log_job_ids=(),
            unavailable_log_job_ids=(),
            errors=(),
            request_count=3,
        )


class _Launcher:
    def __init__(self, state_directory: Path, store: WorkflowLoopStore) -> None:
        self.state_directory = state_directory
        self.store = store
        self.request = None
        self.result_ready = False
        self.launches = 0

    def packet_paths(self, worker_id: str) -> WorkerPacketPaths:
        return WorkerPacketPaths.create(self.state_directory, worker_id)

    def prepare(self, reservation, request) -> WorkerPreparationResult:
        self.request = request
        return WorkerPreparationResult(
            WorkerPreparationStatus.PREPARED,
            reservation.worker_id,
            self.packet_paths(reservation.worker_id),
            request,
            None,
        )

    def launch(self, reservation) -> WorkerLaunchResult:
        self.launches += 1
        self.store.mark_worker_launch_attempt(
            reservation.worker_id,
            launch_attempted_at=NOW,
        )
        self.store.mark_worker_launched(
            reservation.worker_id,
            pid=12345,
            launched_at=NOW,
        )
        return WorkerLaunchResult(
            WorkerLaunchStatus.LAUNCHED,
            reservation.worker_id,
            12345,
            None,
            None,
        )

    def observe(self, worker) -> WorkerObservation:
        paths = self.packet_paths(worker.worker_id)
        if not self.result_ready:
            return WorkerObservation(
                WorkerObservationStatus.RUNNING,
                worker.worker_id,
                None,
                None,
                None,
                paths.request,
                paths.result,
                paths.detail,
                None,
            )
        assert self.request is not None
        result = JudgmentResult(
            schema_version=1,
            item_id=self.request.item_id,
            episode=self.request.episode,
            evidence_fingerprint=self.request.evidence_fingerprint,
            decision=JudgmentDecision.ASSIGN,
            summary="The compiler job is in scope.",
            evidence_ids=self.request.evidence_ids,
            in_scope_job_ids=(900,),
            copilot_request="Fix the compiler failure.",
        )
        completion = WorkerCompletion(
            worker.worker_id,
            WorkState.SUCCEEDED,
            LATER,
            0,
            None,
        )
        self.store.complete_worker(completion)
        return WorkerObservation(
            WorkerObservationStatus.COMPLETED,
            worker.worker_id,
            completion,
            self.request,
            result,
            paths.request,
            paths.result,
            paths.detail,
            None,
        )


class _Writer:
    def __init__(self) -> None:
        self.calls = []

    def execute(self, request, result, *, pass_id: str, owner_id: str):
        self.calls.append((request, result, pass_id, owner_id))
        return WorkflowWriteResult(
            "confirmed",
            "Task confirmed.",
            ("action-1",),
            issue_number=17,
            task_id="task-123",
            newly_confirmed=True,
        )


class _NetworkActor:
    def __init__(self) -> None:
        self.issue_number = 123
        self.task_ids = (
            "task-integration",
            "task-follow-up-1",
            "task-follow-up-2",
        )
        self.issue_title: str | None = None
        self.issue_body: str | None = None
        self.calls: list[str] = []

    @property
    def task_id(self) -> str:
        task_calls = self.calls.count("create_copilot_task")
        return self.task_ids[max(0, task_calls - 1)]

    def create_issue(self, repository: str, *, title: str, body: str):
        self.calls.append("create_issue")
        self.issue_title = title
        self.issue_body = body
        return {"number": self.issue_number}

    def create_copilot_task(
        self,
        repository: str,
        *,
        prompt: str,
        base_branch: str,
        head_branch: str | None = None,
        model: str | None = None,
    ):
        self.calls.append("create_copilot_task")
        return {"id": self.task_id}


def _cleanup_process(process) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


class WorkflowLoopManagerTests(unittest.TestCase):
    def test_real_request_prompt_keeps_late_python_failure_in_budget(self) -> None:
        checkout_prefix = "".join(
            f"checkout setup line {index:04d} {'x' * 60}\n"
            for index in range(120)
        )
        failure_text = (
            "Traceback (most recent call last):\n"
            '  File ".github/ci-shepherd-fixture/resolve_config.py", '
            "line 10, in <module>\n"
            '    print(configuration["output_dir"])\n'
            "          ~~~~~~~~~~~~~^^^^^^^^^^^^^^\n"
            "KeyError: 'output_dir'\n"
            "##[error]Process completed with exit code 1.\n"
        )
        cleanup_suffix = (
            "Node 20 is being deprecated.\n"
            "Post job cleanup.\n"
            + "".join(f"cleanup line {index:04d}\n" for index in range(150))
        )
        full_log = checkout_prefix + failure_text + cleanup_suffix
        observed_run = run(101)
        client = EndpointClient({
            **base_responses(observed_run),
            f"/repos/{REPOSITORY}/actions/runs/101": observed_run,
            (
                f"/repos/{REPOSITORY}/actions/runs/101/attempts/1/jobs"
            ): PagedResponse((job(101, 1001, "Build"),)),
            f"/repos/{REPOSITORY}/actions/jobs/1001/logs": full_log,
        })
        reader = WorkflowReader(
            client=client,
            clock=lambda: datetime(2026, 9, 17, 20, 0, tzinfo=UTC),
            request_count=lambda: client.request_count,
        )
        snapshot = reader.observe(
            repository=REPOSITORY,
            branch=BRANCH,
            tracked_items=(),
            workflow_ids=(WORKFLOW_ID,),
        )
        detail = reader.read_run_details(
            snapshot.workflows[0].latest_completed
        )
        self.assertFalse(detail.run.jobs[0].log_truncated)
        self.assertGreater(
            detail.run.jobs[0].log_excerpt.index("KeyError"),
            4_000,
        )

        with TemporaryDirectory() as scratch:
            store = WorkflowLoopStore(
                Path(scratch) / "state",
                repository=REPOSITORY,
                branch=BRANCH,
            )
            store.initialize(workflow_ids=(WORKFLOW_ID,))
            item = store.upsert_failure(
                detail.run,
                "2026-09-17T20:00:00Z",
            )
            request = _judgment_request(
                item,
                ItemRefresh(
                    item_id=item.id,
                    observed_at="2026-09-17T20:00:00Z",
                    runs=(detail.run,),
                    failure_run=detail.run,
                    wait_run=None,
                    recovery="failed",
                    recovery_run=None,
                    issue=None,
                    task=None,
                    pull_request=None,
                    pre_write=False,
                    complete=True,
                    errors=(),
                    request_count=detail.request_count,
                ),
                worker_id="worker-prompt",
                session_id="6bab0076-b91c-4e01-a049-bf36b92cb34f",
                judgment_round=0,
            )

        self.assertIn("Traceback (most recent call last):", request.prompt)
        self.assertIn("KeyError: 'output_dir'", request.prompt)
        self.assertIn("Post job cleanup.", request.prompt)
        self.assertIn("logSourceTruncated=false", request.prompt)
        self.assertIn("promptExcerpted=true", request.prompt)
        self.assertLessEqual(len(request.prompt), 20_000)

    def test_read_failure_is_persisted_as_a_degraded_pass(self) -> None:
        class UnavailableReader:
            calls = 0

            def observe(self, **kwargs):
                self.calls += 1
                return ReaderSnapshot(
                    observed_at=NOW,
                    repository="owner/repo",
                    repository_id=None,
                    branch="main",
                    default_branch=None,
                    workflows=(),
                    tracked_wait_runs=(),
                    complete=False,
                    errors=(
                        ReadError(
                            scope="repository",
                            code="repository-unavailable",
                            endpoint="/repos/owner/repo",
                            detail="GitHub returned 503.",
                        ),
                    ),
                    request_count=1,
                )

        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            store = WorkflowLoopStore(
                state_directory,
                repository="owner/repo",
                branch="main",
            )
            store.initialize()
            reader = UnavailableReader()
            manager = WorkflowLoopManager(
                state_directory=state_directory,
                repository="owner/repo",
                branch="main",
                store=store,
                reader=reader,
                launcher=_Launcher(state_directory, store),
                writer=None,
                clock=lambda: datetime(2026, 9, 17, 20, 0, tzinfo=UTC),
                id_factory=lambda: "degraded-pass",
            )

            result = manager.run_pass()

            self.assertEqual(1, reader.calls)
            self.assertEqual(1, len(result.errors))
            database = state_directory / "workflow-loop.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                persisted = connection.execute(
                    "SELECT error FROM passes WHERE pass_id = 'degraded-pass'"
                ).fetchone()[0]
            self.assertIn("repository-unavailable", persisted)
            report = render_status(
                state_directory,
                repository="owner/repo",
                branch="main",
                now=datetime(2026, 9, 17, 20, 1, tzinfo=UTC),
            )
            self.assertIn("status=error=repository:repository-unavailable", report)

    def test_workflow_scope_mismatch_stops_before_reader(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            store = WorkflowLoopStore(
                state_directory,
                repository="owner/repo",
                branch="main",
            )
            store.initialize(workflow_ids=(17,))
            reader = _Reader(
                ItemRefresh(
                    item_id=1,
                    observed_at=NOW,
                    runs=(),
                    failure_run=None,
                    wait_run=None,
                    recovery="unavailable",
                    recovery_run=None,
                    issue=None,
                    task=None,
                    pull_request=None,
                    pre_write=False,
                    complete=False,
                    errors=(),
                    request_count=0,
                )
            )
            manager = WorkflowLoopManager(
                state_directory=state_directory,
                repository="owner/repo",
                branch="main",
                store=store,
                reader=reader,
                launcher=_Launcher(state_directory, store),
                writer=None,
                clock=lambda: datetime(2026, 9, 17, 20, 0, tzinfo=UTC),
                workflow_ids=(18,),
            )

            with self.assertRaisesRegex(ValueError, "workflow scope"):
                manager.run_pass()

            self.assertEqual(0, reader.observe_calls)

    def test_all_failures_are_tracked_before_capacity_gates_detail_reads(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            workflows = tuple(
                workflow(
                    workflow_id,
                    path=f".github/workflows/{workflow_id}.yml",
                    name=f"Workflow {workflow_id}",
                )
                for workflow_id in (17, 18, 19)
            )
            runs = {
                workflow_id: run(
                    100 + workflow_id,
                    workflow_id=workflow_id,
                    path=f".github/workflows/{workflow_id}.yml",
                    name=f"Workflow {workflow_id}",
                )
                for workflow_id in (17, 18, 19)
            }
            responses = {
                f"/repos/{REPOSITORY}": repository(),
                f"/repos/{REPOSITORY}/branches/{BRANCH}": {
                    "name": BRANCH,
                    "commit": {"sha": "b" * 40},
                },
                f"/repos/{REPOSITORY}/actions/workflows": PagedResponse(workflows),
            }
            for workflow_id, observed_run in runs.items():
                responses[run_endpoint(workflow_id)] = {
                    "total_count": 1,
                    "workflow_runs": [observed_run],
                }
            for workflow_id in (17, 18):
                observed_run = runs[workflow_id]
                responses[
                    f"/repos/{REPOSITORY}/actions/runs/{observed_run['id']}"
                ] = observed_run
                responses[
                    f"/repos/{REPOSITORY}/actions/runs/{observed_run['id']}"
                    "/attempts/1/jobs"
                ] = PagedResponse((
                    job(observed_run["id"], 1000 + workflow_id, "Build"),
                ))
                responses[
                    f"/repos/{REPOSITORY}/actions/jobs/{1000 + workflow_id}/logs"
                ] = "compiler failure"
            client = EndpointClient(responses)
            reader = WorkflowReader(
                client=client,
                clock=lambda: datetime(2026, 9, 17, 20, 0, tzinfo=UTC),
                request_count=lambda: client.request_count,
            )
            store = WorkflowLoopStore(
                state_directory,
                repository=REPOSITORY,
                branch=BRANCH,
            )
            store.initialize()
            launcher = _Launcher(state_directory, store)
            manager = WorkflowLoopManager(
                state_directory=state_directory,
                repository=REPOSITORY,
                branch=BRANCH,
                store=store,
                reader=reader,
                launcher=launcher,
                writer=None,
                clock=lambda: datetime(2026, 9, 17, 20, 0, tzinfo=UTC),
                id_factory=iter(("pass", "worker-1", "worker-2")).__next__,
                request_count=lambda: client.request_count,
            )

            result = manager.run_pass(mode=EffectMode.LOCAL_JUDGMENT)

            self.assertEqual(3, result.discovered_items)
            self.assertEqual(3, len(store.list_items()))
            self.assertEqual(2, len(store.list_workers()))
            self.assertEqual(
                (0, 0),
                tuple(worker.judgment_round for worker in store.list_workers()),
            )
            deferred_run_id = runs[19]["id"]
            self.assertFalse(any(
                endpoint
                == f"/repos/{REPOSITORY}/actions/runs/{deferred_run_id}"
                or f"/runs/{deferred_run_id}/attempts/" in endpoint
                for _, endpoint, _ in client.calls
            ))

    def test_unavailable_failure_detail_remains_visible_and_retries(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            observed_run = run(101)
            jobs_endpoint = (
                f"/repos/{REPOSITORY}/actions/runs/101/attempts/1/jobs"
            )
            client = EndpointClient({
                **base_responses(observed_run),
                f"/repos/{REPOSITORY}/actions/runs/101": observed_run,
                jobs_endpoint: SequenceResponse((
                    api_error(
                        jobs_endpoint,
                        category="server",
                        status=503,
                    ),
                    PagedResponse((job(101, 1001, "Build"),)),
                )),
                f"/repos/{REPOSITORY}/actions/jobs/1001/logs": "failure",
            })
            reader = WorkflowReader(
                client=client,
                clock=lambda: datetime(2026, 9, 17, 20, 0, tzinfo=UTC),
                request_count=lambda: client.request_count,
            )
            store = WorkflowLoopStore(
                state_directory,
                repository=REPOSITORY,
                branch=BRANCH,
            )
            store.initialize(workflow_ids=(WORKFLOW_ID,))
            launcher = _Launcher(state_directory, store)
            manager = WorkflowLoopManager(
                state_directory=state_directory,
                repository=REPOSITORY,
                branch=BRANCH,
                store=store,
                reader=reader,
                launcher=launcher,
                writer=None,
                clock=lambda: datetime(2026, 9, 17, 20, 0, tzinfo=UTC),
                id_factory=iter(("pass-1", "pass-2", "worker-1")).__next__,
                workflow_ids=(WORKFLOW_ID,),
                request_count=lambda: client.request_count,
            )

            first = manager.run_pass(mode=EffectMode.LOCAL_JUDGMENT)
            self.assertEqual(1, len(store.list_items()))
            self.assertEqual(0, len(store.list_workers()))
            self.assertEqual(
                ItemPhase.OBSERVING_FAILURE,
                store.list_items()[0].phase,
            )
            self.assertTrue(first.errors)

            second = manager.run_pass(mode=EffectMode.LOCAL_JUDGMENT)
            self.assertEqual(1, second.launched_workers)
            self.assertEqual(1, len(store.list_workers()))
            self.assertFalse(second.errors)

    def test_real_components_complete_initial_assignment_in_two_passes(self) -> None:
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            state_directory = root / "state"
            actor = _NetworkActor()
            observed_run = run(101)
            jobs_endpoint = (
                f"/repos/{REPOSITORY}/actions/runs/101/attempts/1/jobs"
            )
            search_endpoint = (
                "/search/issues?q=repo%3Aradical%2Faspire+is%3Aissue+"
                "is%3Aopen+%22ci-shepherd%3Aworkflow-repair%22+"
                "%22workflow-id%3D17%22&per_page=10"
            )

            def issue_payload(_endpoint: str):
                if actor.issue_body is None or actor.issue_title is None:
                    raise AssertionError("Issue was read before it was created.")
                return {
                    "id": 9001,
                    "number": actor.issue_number,
                    "state": "open",
                    "title": actor.issue_title,
                    "body": actor.issue_body,
                    "html_url": (
                        f"https://github.com/{REPOSITORY}/issues/"
                        f"{actor.issue_number}"
                    ),
                    "repository_url": (
                        f"https://api.github.com/repos/{REPOSITORY}"
                    ),
                    "assignees": [],
                }

            client = EndpointClient(
                {
                    **base_responses(observed_run),
                    f"/repos/{REPOSITORY}/actions/runs/101": observed_run,
                    jobs_endpoint: PagedResponse(
                        (job(101, 1001, "Build"),)
                    ),
                    f"/repos/{REPOSITORY}/actions/jobs/1001/logs": (
                        "error CS1002: ; expected"
                    ),
                    search_endpoint: {"total_count": 0, "items": []},
                    f"/repos/{REPOSITORY}/issues/{actor.issue_number}": (
                        issue_payload
                    ),
                    (
                        f"/agents/repos/{REPOSITORY}/tasks/{actor.task_id}"
                    ): task_record(
                        task_id=actor.task_id,
                        state="queued",
                    ),
                }
            )
            reader = WorkflowReader(
                client=client,
                clock=lambda: datetime(2026, 9, 17, 20, 0, tzinfo=UTC),
                request_count=lambda: client.request_count,
            )
            store = WorkflowLoopStore(
                state_directory,
                repository=REPOSITORY,
                branch=BRANCH,
            )
            store.initialize(workflow_ids=(WORKFLOW_ID,))
            wrapper_processes = []

            def process_factory(argv, **kwargs):
                process = __import__("subprocess").Popen(argv, **kwargs)
                wrapper_processes.append(process)
                self.addCleanup(_cleanup_process, process)
                return process

            launcher = JudgmentWorkerLauncher(
                state_directory,
                store=store,
                clock=lambda: datetime(2026, 9, 17, 20, 0, tzinfo=UTC),
                model="trusted-model",
                reasoning_effort="high",
                process_factory=process_factory,
            )
            writer = WorkflowWriter(
                store=store,
                reader=reader,
                actor=actor,
                repository=REPOSITORY,
                branch=BRANCH,
                clock=lambda: datetime(2026, 9, 17, 20, 0, tzinfo=UTC),
                active_item_limit=2,
            )
            counter = itertools.count(1)
            manager = WorkflowLoopManager(
                state_directory=state_directory,
                repository=REPOSITORY,
                branch=BRANCH,
                store=store,
                reader=reader,
                launcher=launcher,
                writer=writer,
                clock=lambda: datetime(2026, 9, 17, 20, 0, tzinfo=UTC),
                id_factory=lambda: f"id-{next(counter)}",
                workflow_ids=(WORKFLOW_ID,),
                request_count=lambda: client.request_count,
            )

            bin_directory = root / "bin"
            bin_directory.mkdir()
            fake_copilot = bin_directory / "copilot"
            fake_copilot.write_text(
                f"""#!{sys.executable}
import json
from pathlib import Path
import sys

request = json.loads(Path("request.json").read_text(encoding="utf-8"))
usage = Path(sys.argv[sys.argv.index("--usage-output-file") + 1])
usage.write_text('{{"requests":1}}', encoding="utf-8")
decision = "assign" if request["round"] == 0 else "follow_up"
judgment = json.dumps({{
    "schemaVersion": 1,
    "itemId": request["itemId"],
    "episode": request["episode"],
    "evidenceFingerprint": request["evidenceFingerprint"],
    "decision": decision,
    "summary": "The compiler failure is in scope.",
    "evidenceIds": request["evidenceIds"],
    "inScopeJobIds": (
        [request["failedJobs"][0]["jobId"]]
        if request["round"] == 0
        else []
    ),
    "copilotRequest": "Fix the compiler failure."
}}, separators=(",", ":"))
print(json.dumps({{
    "type": "assistant.message",
    "data": {{"phase": "final_answer", "content": judgment}}
}}, separators=(",", ":")))
""",
                encoding="utf-8",
            )
            fake_copilot.chmod(0o700)

            environment_path = os.pathsep.join(
                (str(bin_directory), os.environ.get("PATH", ""))
            )
            with patch.dict(os.environ, {"PATH": environment_path}):
                first = manager.run_pass(mode=EffectMode.LIVE)
                self.assertEqual(1, first.launched_workers)
                self.assertEqual([], actor.calls)

                worker = store.list_workers()[0]
                self.assertEqual(
                    worker.session_id,
                    str(uuid.UUID(worker.session_id)),
                )
                result_path = Path(worker.result_path)
                lifetime_path = Path(worker.lifetime_lock_path)
                deadline = time.monotonic() + 10
                while True:
                    if (
                        result_path.exists()
                        and not is_lifetime_active(lifetime_path)
                    ):
                        break
                    if time.monotonic() >= deadline:
                        self.fail("Timed out waiting for the real worker wrapper.")
                    time.sleep(0.01)

                second = manager.run_pass(mode=EffectMode.LIVE)
                self.assertEqual(
                    1,
                    second.confirmed_assignments,
                    (
                        second,
                        store.list_items(),
                        store.list_workers(),
                        store.list_actions(),
                        (Path(worker.detail_path).parent / "stderr.txt").read_text(
                            encoding="utf-8"
                        ),
                    ),
                )
                self.assertEqual(
                    ["create_issue", "create_copilot_task"],
                    actor.calls,
                )
                current = store.list_items()[0]
                self.assertEqual(actor.issue_number, current.issue_number)
                self.assertEqual(actor.task_id, current.task_id)
                first_assignment_at = current.assignment_confirmed_at

                third = manager.run_pass(mode=EffectMode.LIVE)
                self.assertEqual(0, third.confirmed_assignments)
                self.assertEqual(
                    ["create_issue", "create_copilot_task"],
                    actor.calls,
                )
                self.assertEqual(1, len(store.list_workers()))

                store = WorkflowLoopStore(
                    state_directory,
                    repository=REPOSITORY,
                    branch=BRANCH,
                )
                store.initialize(workflow_ids=(WORKFLOW_ID,))
                reader = WorkflowReader(
                    client=client,
                    clock=lambda: datetime(
                        2026,
                        9,
                        17,
                        20,
                        0,
                        tzinfo=UTC,
                    ),
                    request_count=lambda: client.request_count,
                )
                launcher = JudgmentWorkerLauncher(
                    state_directory,
                    store=store,
                    clock=lambda: datetime(
                        2026,
                        9,
                        17,
                        20,
                        0,
                        tzinfo=UTC,
                    ),
                    model="trusted-model",
                    reasoning_effort="high",
                    process_factory=process_factory,
                )
                writer = WorkflowWriter(
                    store=store,
                    reader=reader,
                    actor=actor,
                    repository=REPOSITORY,
                    branch=BRANCH,
                    clock=lambda: datetime(
                        2026,
                        9,
                        17,
                        20,
                        0,
                        tzinfo=UTC,
                    ),
                    active_item_limit=2,
                )
                manager = WorkflowLoopManager(
                    state_directory=state_directory,
                    repository=REPOSITORY,
                    branch=BRANCH,
                    store=store,
                    reader=reader,
                    launcher=launcher,
                    writer=writer,
                    clock=lambda: datetime(
                        2026,
                        9,
                        17,
                        20,
                        0,
                        tzinfo=UTC,
                    ),
                    id_factory=lambda: f"id-{next(counter)}",
                    workflow_ids=(WORKFLOW_ID,),
                    request_count=lambda: client.request_count,
                )

                head_sha = "a" * 40
                pull_payload = pull(
                    number=55,
                    database_id=9001,
                    head_sha=head_sha,
                    head_ref="copilot/fix-ci",
                    base_ref=BRANCH,
                )
                task_artifacts = [{
                    "type": "branch",
                    "provider": "github",
                    "data": {
                        "head_ref": "copilot/fix-ci",
                        "base_ref": BRANCH,
                    },
                }]
                head_search = (
                    f"/repos/{REPOSITORY}/pulls?"
                    "state=all&head=radical%3Acopilot%2Ffix-ci&per_page=10"
                )
                client.set_response(
                    f"/agents/repos/{REPOSITORY}/tasks/{actor.task_ids[0]}",
                    task_record(
                        task_id=actor.task_ids[0],
                        state="idle",
                        artifacts=task_artifacts,
                    ),
                )
                client.set_response(head_search, [pull_payload])
                client.set_response(
                    f"/repos/{REPOSITORY}/pulls/55",
                    pull_payload,
                )
                client.set_response(
                    f"/repos/{REPOSITORY}/commits/{head_sha}/check-runs",
                    PagedResponse(({
                        "id": 7001,
                        "name": "CI / Build",
                        "head_sha": head_sha,
                        "status": "completed",
                        "conclusion": "failure",
                        "html_url": (
                            f"https://github.com/{REPOSITORY}/runs/7001"
                        ),
                        "details_url": None,
                        "output": {},
                    },)),
                )
                client.set_response(
                    f"/repos/{REPOSITORY}/commits/{head_sha}/status",
                    {"sha": head_sha, "state": "failure", "statuses": []},
                )
                client.set_response(
                    f"/repos/{REPOSITORY}/pulls/55/reviews",
                    PagedResponse(()),
                )
                client.set_response(
                    f"/repos/{REPOSITORY}/pulls/55/files",
                    PagedResponse(({
                        "filename": "src/Compiler.cs",
                        "status": "modified",
                        "additions": 1,
                        "deletions": 1,
                        "changes": 2,
                        "patch": "@@ -1 +1 @@",
                    },)),
                )
                client.set_response(
                    (
                        f"/repos/{REPOSITORY}/issues/55/comments"
                        "?per_page=100&page=1&since="
                        "2026-09-17T20%3A00%3A00Z"
                    ),
                    [],
                )

                red_refresh = reader.refresh_item(store.list_items()[0])
                self.assertEqual(
                    "red",
                    red_refresh.pull_request.checks_state,
                )
                fourth = manager.run_pass(mode=EffectMode.LIVE)
                self.assertEqual(
                    1,
                    fourth.launched_workers,
                    (
                        fourth,
                        store.list_items(),
                        store.list_workers(),
                        store.recent_history(store.list_items()[0].id),
                    ),
                )
                self.assertEqual(2, len(store.list_workers()))
                follow_up_worker = store.list_workers()[-1]
                deadline = time.monotonic() + 10
                while True:
                    if (
                        Path(follow_up_worker.result_path).exists()
                        and not is_lifetime_active(
                            Path(follow_up_worker.lifetime_lock_path)
                        )
                    ):
                        break
                    if time.monotonic() >= deadline:
                        self.fail("Timed out waiting for the follow-up worker.")
                    time.sleep(0.01)

                fifth = manager.run_pass(mode=EffectMode.LIVE)
                self.assertEqual(
                    1,
                    fifth.confirmed_assignments,
                    (
                        fifth,
                        store.list_items(),
                        store.list_workers(),
                        store.list_actions(),
                        store.recent_history(store.list_items()[0].id),
                    ),
                )
                self.assertEqual(
                    [
                        "create_issue",
                        "create_copilot_task",
                        "create_copilot_task",
                    ],
                    actor.calls,
                )
                current = store.list_items()[0]
                self.assertEqual(actor.task_ids[1], current.task_id)
                self.assertEqual(1, current.followup_count)
                self.assertEqual(
                    (0, 1),
                    tuple(
                        worker.judgment_round
                        for worker in store.list_workers()
                    ),
                )
                self.assertTrue(all(
                    worker.consumed_at is not None
                    for worker in store.list_workers()
                ))

                client.set_response(
                    f"/agents/repos/{REPOSITORY}/tasks/{actor.task_ids[1]}",
                    task_record(
                        task_id=actor.task_ids[1],
                        state="idle",
                        artifacts=task_artifacts,
                    ),
                )

                sixth = manager.run_pass(mode=EffectMode.LIVE)
                self.assertEqual(1, sixth.launched_workers)
                self.assertEqual(3, len(store.list_workers()))
                second_follow_up_worker = store.list_workers()[-1]
                self.assertEqual(2, second_follow_up_worker.judgment_round)
                deadline = time.monotonic() + 10
                while True:
                    if (
                        Path(second_follow_up_worker.result_path).exists()
                        and not is_lifetime_active(
                            Path(second_follow_up_worker.lifetime_lock_path)
                        )
                    ):
                        break
                    if time.monotonic() >= deadline:
                        self.fail(
                            "Timed out waiting for the second follow-up worker."
                        )
                    time.sleep(0.01)

                seventh = manager.run_pass(mode=EffectMode.LIVE)
                self.assertEqual(1, seventh.confirmed_assignments)
                self.assertEqual(
                    [
                        "create_issue",
                        "create_copilot_task",
                        "create_copilot_task",
                        "create_copilot_task",
                    ],
                    actor.calls,
                )
                current = store.list_items()[0]
                self.assertEqual(actor.task_ids[2], current.task_id)
                self.assertEqual(2, current.followup_count)
                self.assertEqual(
                    first_assignment_at,
                    current.assignment_confirmed_at,
                )

                client.set_response(
                    f"/agents/repos/{REPOSITORY}/tasks/{actor.task_ids[2]}",
                    task_record(
                        task_id=actor.task_ids[2],
                        state="idle",
                        artifacts=task_artifacts,
                    ),
                )
                client.set_response(
                    f"/repos/{REPOSITORY}/commits/{head_sha}/check-runs",
                    PagedResponse(({
                        "id": 7002,
                        "name": "CI / Build",
                        "head_sha": head_sha,
                        "status": "completed",
                        "conclusion": "success",
                        "html_url": (
                            f"https://github.com/{REPOSITORY}/runs/7002"
                        ),
                        "details_url": None,
                        "output": {},
                    },)),
                )
                client.set_response(
                    f"/repos/{REPOSITORY}/commits/{head_sha}/status",
                    {"sha": head_sha, "state": "success", "statuses": []},
                )

                green_refresh = reader.refresh_item(store.list_items()[0])
                self.assertEqual(
                    "green",
                    green_refresh.pull_request.checks_state,
                )
                eighth = manager.run_pass(mode=EffectMode.LIVE)
                self.assertEqual(0, eighth.confirmed_assignments)
                current = store.list_items()[0]
                self.assertEqual(ItemPhase.WAITING_FOR_CI, current.phase)
                self.assertEqual(55, current.pull_request_number)

                recovery = run(
                    102,
                    conclusion="success",
                    created_at="2026-09-17T20:10:00Z",
                )
                client.set_response(
                    run_endpoint(),
                    {
                        "total_count": 2,
                        "workflow_runs": [recovery, observed_run],
                    },
                )
                client.set_response(
                    f"/repos/{REPOSITORY}/actions/runs/102",
                    recovery,
                )
                client.set_response(
                    (
                        f"/repos/{REPOSITORY}/actions/runs/102/"
                        "attempts/1/jobs"
                    ),
                    PagedResponse((
                        job(
                            102,
                            2001,
                            "Build",
                            conclusion="success",
                        ),
                    )),
                )
                client.set_response(
                    f"/agents/repos/{REPOSITORY}/tasks/{actor.task_ids[2]}",
                    task_record(
                        task_id=actor.task_ids[2],
                        state="idle",
                        artifacts=task_artifacts,
                    ),
                )

                ninth = manager.run_pass(mode=EffectMode.LIVE)
                self.assertEqual(0, ninth.confirmed_assignments)
                current = store.list_items()[0]
                self.assertEqual(ItemPhase.RECOVERED, current.phase)
                self.assertEqual(102, current.recovered_run_id)
                self.assertEqual(55, current.pull_request_number)
                self.assertEqual(actor.task_ids[2], current.task_id)
                self.assertIs(TaskState.IDLE, current.task_state)
                self.assertNotIn(current.id, store.active_item_ids())
                self.assertEqual(3, len(store.list_workers()))
                recovered_at = current.recovered_at
                last_progressed_at = current.last_progressed_at

                tenth = manager.run_pass(mode=EffectMode.LIVE)
                self.assertEqual(0, tenth.confirmed_assignments)
                current = store.list_items()[0]
                self.assertEqual(recovered_at, current.recovered_at)
                self.assertEqual(last_progressed_at, current.last_progressed_at)
                self.assertEqual(3, len(store.list_workers()))
                self.assertTrue(all(
                    worker.consumed_at is not None
                    for worker in store.list_workers()
                ))
                self.assertEqual(4, len(actor.calls))
            for process in wrapper_processes:
                process.wait(timeout=5)

    def test_recovery_is_persisted_before_failed_worker_attention(self) -> None:
        class FailedLauncher:
            def __init__(self, paths, completion):
                self.paths = paths
                self.completion = completion

            def observe(self, worker):
                return WorkerObservation(
                    WorkerObservationStatus.ATTENTION_REQUIRED,
                    worker.worker_id,
                    self.completion,
                    None,
                    None,
                    self.paths.request,
                    self.paths.result,
                    self.paths.detail,
                    self.completion.error,
                )

        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            store = WorkflowLoopStore(
                state_directory,
                repository="owner/repo",
                branch="main",
            )
            store.initialize()
            paths = WorkerPacketPaths.create(state_directory, "worker-failed")
            failure = _request(paths).failure_run
            item = store.upsert_failure(failure, NOW)
            store.update_item(
                replace(
                    item,
                    last_judged_fingerprint=item.evidence_fingerprint,
                ),
                history_event="judged",
                summary="Judgment completed.",
                detail={},
            )
            reservation = WorkerReservation(
                worker_id="worker-failed",
                item_id=item.id,
                episode=item.episode,
                evidence_fingerprint=item.evidence_fingerprint,
                session_id="session-failed",
                request_path=str(paths.request),
                result_path=str(paths.result),
                detail_path=str(paths.detail),
                lifetime_lock_path=str(paths.lifetime_lock),
                queued_at=NOW,
            )
            self.assertTrue(store.reserve_worker(reservation, capacity_limit=2))
            store.mark_worker_launch_attempt(
                reservation.worker_id,
                launch_attempted_at=NOW,
            )
            store.mark_worker_launched(
                reservation.worker_id,
                pid=12345,
                launched_at=NOW,
            )
            completion = WorkerCompletion(
                reservation.worker_id,
                WorkState.FAILED,
                LATER,
                1,
                "worker failed",
            )
            store.complete_worker(completion)
            recovery_jobs = tuple(
                replace(job, conclusion="success")
                for job in failure.jobs
            )
            recovery = replace(
                failure,
                run_id=102,
                run_number=89,
                head_sha="recovered-sha",
                created_at="2026-09-17T20:02:00Z",
                updated_at="2026-09-17T20:03:00Z",
                url="https://github.com/owner/repo/actions/runs/102",
                conclusion="success",
                jobs=tuple(
                    replace(job, run_id=102, url=job.url.replace("101", "102"))
                    for job in recovery_jobs
                ),
            )
            refresh = ItemRefresh(
                item_id=item.id,
                observed_at="2026-09-17T20:04:00Z",
                runs=(failure, recovery),
                failure_run=failure,
                wait_run=None,
                recovery="passed",
                recovery_run=recovery,
                issue=None,
                task=None,
                pull_request=None,
                pre_write=False,
                complete=True,
                errors=(),
                request_count=2,
            )
            reader = _Reader(refresh)
            times = iter(
                (
                    datetime(2026, 9, 17, 20, 4, tzinfo=UTC),
                    datetime(2026, 9, 17, 20, 4, 1, tzinfo=UTC),
                )
            )
            manager = WorkflowLoopManager(
                state_directory=state_directory,
                repository="owner/repo",
                branch="main",
                store=store,
                reader=reader,
                launcher=FailedLauncher(paths, completion),
                writer=None,
                clock=lambda: next(times),
                id_factory=lambda: "pass-recovery",
            )

            manager.run_pass()

            current = store.list_items()[0]
            self.assertEqual(ItemPhase.RECOVERED, current.phase)
            self.assertEqual(102, current.recovered_run_id)
            self.assertIn("Worker ended in failed", current.latest_error)

    def test_failed_and_invalid_workers_remain_sticky_across_passes(self) -> None:
        cases = {
            "failed": "import sys\nsys.exit(7)\n",
            "invalid": "print('not-json')\n",
        }
        for expected_state, script in cases.items():
            with self.subTest(expected_state=expected_state):
                with TemporaryDirectory() as scratch:
                    root = Path(scratch)
                    state_directory = root / "state"
                    observed_run = run(101)
                    client = EndpointClient({
                        **base_responses(observed_run),
                        f"/repos/{REPOSITORY}/actions/runs/101": observed_run,
                        (
                            f"/repos/{REPOSITORY}/actions/runs/101/"
                            "attempts/1/jobs"
                        ): PagedResponse((job(101, 1001, "Build"),)),
                        f"/repos/{REPOSITORY}/actions/jobs/1001/logs": (
                            "compiler failure"
                        ),
                    })
                    reader = WorkflowReader(
                        client=client,
                        clock=lambda: datetime(
                            2026,
                            9,
                            17,
                            20,
                            0,
                            tzinfo=UTC,
                        ),
                        request_count=lambda: client.request_count,
                    )
                    store = WorkflowLoopStore(
                        state_directory,
                        repository=REPOSITORY,
                        branch=BRANCH,
                    )
                    store.initialize(workflow_ids=(WORKFLOW_ID,))
                    marker = root / "copilot-invocations.txt"
                    bin_directory = root / "bin"
                    bin_directory.mkdir()
                    fake_copilot = bin_directory / "copilot"
                    fake_copilot.write_text(
                        (
                            f"#!{sys.executable}\n"
                            "from pathlib import Path\n"
                            f"marker = Path({str(marker)!r})\n"
                            "with marker.open('a', encoding='utf-8') as stream:\n"
                            "    stream.write('invoked\\n')\n"
                            f"{script}"
                        ),
                        encoding="utf-8",
                    )
                    fake_copilot.chmod(0o700)
                    wrapper_processes = []

                    def process_factory(argv, **kwargs):
                        process = __import__("subprocess").Popen(
                            argv,
                            **kwargs,
                        )
                        wrapper_processes.append(process)
                        return process

                    launcher = JudgmentWorkerLauncher(
                        state_directory,
                        store=store,
                        clock=lambda: datetime(
                            2026,
                            9,
                            17,
                            20,
                            0,
                            tzinfo=UTC,
                        ),
                        model="trusted-model",
                        reasoning_effort="high",
                        process_factory=process_factory,
                    )
                    counter = itertools.count(1)
                    manager = WorkflowLoopManager(
                        state_directory=state_directory,
                        repository=REPOSITORY,
                        branch=BRANCH,
                        store=store,
                        reader=reader,
                        launcher=launcher,
                        writer=None,
                        clock=lambda: datetime(
                            2026,
                            9,
                            17,
                            20,
                            0,
                            tzinfo=UTC,
                        ),
                        id_factory=lambda: f"id-{next(counter)}",
                        workflow_ids=(WORKFLOW_ID,),
                        request_count=lambda: client.request_count,
                    )

                    environment_path = os.pathsep.join(
                        (str(bin_directory), os.environ.get("PATH", ""))
                    )
                    with patch.dict(os.environ, {"PATH": environment_path}):
                        first = manager.run_pass(
                            mode=EffectMode.LOCAL_JUDGMENT
                        )
                        self.assertEqual(1, first.launched_workers)
                        worker = store.list_workers()[0]
                        deadline = time.monotonic() + 10
                        while True:
                            if (
                                Path(worker.result_path).exists()
                                and not is_lifetime_active(
                                    Path(worker.lifetime_lock_path)
                                )
                            ):
                                break
                            if time.monotonic() >= deadline:
                                self.fail(
                                    "Timed out waiting for failed worker."
                                )
                            time.sleep(0.01)

                        second = manager.run_pass(
                            mode=EffectMode.LOCAL_JUDGMENT
                        )
                        third = manager.run_pass(
                            mode=EffectMode.LOCAL_JUDGMENT
                        )

                    current = store.list_items()[0]
                    workers = store.list_workers()
                    self.assertIs(ItemPhase.NEEDS_ATTENTION, current.phase)
                    self.assertIn(
                        f"Worker ended in {expected_state}",
                        current.latest_error,
                    )
                    self.assertEqual(1, len(workers))
                    self.assertEqual(expected_state, workers[0].state.value)
                    self.assertIsNone(workers[0].consumed_at)
                    self.assertEqual(1, len(marker.read_text().splitlines()))
                    self.assertEqual((), store.list_actions())
                    self.assertEqual(0, second.launched_workers)
                    self.assertEqual(0, third.launched_workers)
                    self.assertEqual(0, second.confirmed_assignments)
                    self.assertEqual(0, third.confirmed_assignments)
                    for process in wrapper_processes:
                        process.wait(timeout=5)

    def test_read_only_pass_does_not_queue_or_launch_judgment(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            store = WorkflowLoopStore(
                state_directory,
                repository="owner/repo",
                branch="main",
            )
            store.initialize()
            paths = WorkerPacketPaths.create(state_directory, "seed")
            run = _request(paths).failure_run
            item = store.upsert_failure(run, NOW)
            reader = _Reader(
                ItemRefresh(
                    item_id=item.id,
                    observed_at=NOW,
                    runs=(run,),
                    failure_run=run,
                    wait_run=None,
                    recovery="failed",
                    recovery_run=None,
                    issue=None,
                    task=None,
                    pull_request=None,
                    pre_write=False,
                    complete=True,
                    errors=(),
                    request_count=1,
                )
            )
            launcher = _Launcher(state_directory, store)
            times = iter(
                (
                    datetime(2026, 9, 17, 20, 0, tzinfo=UTC),
                    datetime(2026, 9, 17, 20, 0, 1, tzinfo=UTC),
                )
            )
            manager = WorkflowLoopManager(
                state_directory=state_directory,
                repository="owner/repo",
                branch="main",
                store=store,
                reader=reader,
                launcher=launcher,
                writer=None,
                clock=lambda: next(times),
                id_factory=lambda: "pass-read-only",
            )

            result = manager.run_pass()

            self.assertEqual(0, result.launched_workers)
            self.assertEqual(0, launcher.launches)
            self.assertEqual((), store.list_workers())
            self.assertEqual(
                ItemPhase.OBSERVING_FAILURE,
                store.list_items()[0].phase,
            )

    def test_two_pass_initial_assignment_uses_original_judgment(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            store = WorkflowLoopStore(
                state_directory,
                repository="owner/repo",
                branch="main",
            )
            store.initialize()
            seed_paths = WorkerPacketPaths.create(
                state_directory,
                "seed-worker",
            )
            run = _request(seed_paths).failure_run
            item = store.upsert_failure(run, NOW)
            refresh = ItemRefresh(
                item_id=item.id,
                observed_at=NOW,
                runs=(run,),
                failure_run=run,
                wait_run=None,
                recovery="failed",
                recovery_run=None,
                issue=None,
                task=None,
                pull_request=None,
                pre_write=False,
                complete=True,
                errors=(),
                request_count=2,
            )
            reader = _Reader(refresh)
            launcher = _Launcher(state_directory, store)
            writer = _Writer()
            times = iter(
                (
                    datetime(2026, 9, 17, 20, 0, tzinfo=UTC),
                    datetime(2026, 9, 17, 20, 0, 1, tzinfo=UTC),
                    datetime(2026, 9, 17, 20, 0, 2, tzinfo=UTC),
                    datetime(2026, 9, 17, 20, 0, 3, tzinfo=UTC),
                    datetime(2026, 9, 17, 20, 0, 4, tzinfo=UTC),
                    datetime(2026, 9, 17, 20, 0, 5, tzinfo=UTC),
                )
            )
            manager = WorkflowLoopManager(
                state_directory=state_directory,
                repository="owner/repo",
                branch="main",
                store=store,
                reader=reader,
                launcher=launcher,
                writer=writer,
                clock=lambda: next(times),
                id_factory=iter(
                    (
                        "pass-1",
                        "worker-1",
                        "pass-2",
                        "pass-3",
                    )
                ).__next__,
            )

            first = manager.run_pass(mode=EffectMode.LIVE)
            self.assertEqual(1, first.launched_workers)
            self.assertEqual(1, reader.detail_calls)
            self.assertEqual(0, first.confirmed_assignments)
            self.assertEqual(0, len(writer.calls))

            launcher.result_ready = True
            second = manager.run_pass(mode=EffectMode.LIVE)

            self.assertEqual(
                1,
                second.confirmed_assignments,
                (second, store.list_items(), store.list_workers()),
            )
            self.assertEqual(1, len(writer.calls))
            request, result, _, _ = writer.calls[0]
            self.assertIs(request, launcher.request)
            self.assertEqual(
                launcher.request.evidence_fingerprint,
                result.evidence_fingerprint,
            )
            current = store.list_items()[0]
            self.assertEqual("task-123", current.task_id)
            self.assertEqual(TaskState.QUEUED, current.task_state)

            third = manager.run_pass(mode=EffectMode.LIVE)
            self.assertEqual(0, third.launched_workers)
            self.assertEqual(0, third.confirmed_assignments)
            self.assertEqual(1, len(writer.calls))
            self.assertEqual(1, launcher.launches)

    def test_unexpected_error_records_failed_pass_and_propagates(self) -> None:
        class FailingReader:
            def observe(self, **kwargs):
                raise RuntimeError("programmer failure")

        class EmptyLauncher:
            pass

        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            store = WorkflowLoopStore(
                state_directory,
                repository="owner/repo",
                branch="main",
            )
            times = iter(
                (
                    datetime(2026, 9, 17, 20, 0, tzinfo=UTC),
                    datetime(2026, 9, 17, 20, 0, 1, tzinfo=UTC),
                )
            )
            manager = WorkflowLoopManager(
                state_directory=state_directory,
                repository="owner/repo",
                branch="main",
                store=store,
                reader=FailingReader(),
                launcher=EmptyLauncher(),
                writer=None,
                clock=lambda: next(times),
                id_factory=lambda: "pass-failed",
            )

            with self.assertRaisesRegex(RuntimeError, "programmer failure"):
                manager.run_pass()

            connection = sqlite3.connect(
                state_directory / "workflow-loop.sqlite3"
            )
            try:
                row = connection.execute(
                    "SELECT error FROM passes WHERE pass_id = ?",
                    ("pass-failed",),
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(
                ("RuntimeError: programmer failure",),
                row,
            )


if __name__ == "__main__":
    unittest.main()
