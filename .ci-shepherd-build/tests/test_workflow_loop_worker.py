from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, patch

from ci_shepherd.workflow_loop.models import (
    JobKey,
    JobObservation,
    JudgmentRequest,
    RunObservation,
    WorkerReservation,
    WorkerCompletion,
    WorkerView,
    WorkState,
    WorkflowKey,
    judgment_request_to_json,
    parse_judgment_request,
)
from ci_shepherd.workflow_loop.worker import (
    JudgmentWorkerLauncher,
    WorkerLaunchStatus,
    WorkerObservationStatus,
    WorkerPacketPaths,
    WorkerPreparationStatus,
    _atomic_write_json,
)
from ci_shepherd.workflow_loop.lifetime import (
    acquire_lifetime_lock,
    is_lifetime_active,
)
from ci_shepherd.workflow_loop.state import WorkflowLoopStore


NOW = "2026-09-17T20:00:00Z"
LATER = "2026-09-17T20:00:01Z"


def _request(paths: WorkerPacketPaths) -> JudgmentRequest:
    job = JobObservation(
        run_id=101,
        attempt=1,
        job_id=900,
        key=JobKey(name="Build / Linux", runner_labels=("ubuntu-latest",)),
        status="completed",
        conclusion="failure",
        started_at=NOW,
        completed_at=LATER,
        url="https://github.com/owner/repo/actions/runs/101/job/900",
        log_excerpt="error CS1002: ; expected",
        log_truncated=False,
    )
    run = RunObservation(
        key=WorkflowKey("owner/repo", 42, "main"),
        workflow_path=".github/workflows/ci.yml",
        workflow_name="CI",
        run_id=101,
        run_number=88,
        attempt=1,
        head_sha="0123456789abcdef",
        event="push",
        status="completed",
        conclusion="failure",
        created_at=NOW,
        updated_at=LATER,
        url="https://github.com/owner/repo/actions/runs/101",
        jobs_complete=True,
        jobs=(job,),
    )
    return JudgmentRequest(
        worker_id=paths.worker_directory.name,
        session_id="session-1",
        item_id=7,
        episode=2,
        evidence_fingerprint="fnv1a64:0123456789abcdef",
        round=1,
        repository="owner/repo",
        branch="main",
        workflow_id=42,
        workflow_path=".github/workflows/ci.yml",
        failure_run=run,
        failed_jobs=(job,),
        evidence_ids=("run:101", "job:101:900", "log:900"),
        issue_number=17,
        task_id="task-owned-123",
        pull_request_number=23,
        pull_request_head_sha="fedcba9876543210",
        pull_request_head_ref="copilot/fix-build",
        pull_request_base_ref="main",
        pull_request_observed_at=LATER,
        followup_count=1,
        prompt="Classify the workflow failure and return the required JSON.",
    )


def _reservation(paths: WorkerPacketPaths) -> WorkerReservation:
    return WorkerReservation(
        worker_id=paths.worker_directory.name,
        item_id=7,
        episode=2,
        evidence_fingerprint="fnv1a64:0123456789abcdef",
        session_id="session-1",
        request_path=str(paths.request),
        result_path=str(paths.result),
        detail_path=str(paths.detail),
        lifetime_lock_path=str(paths.lifetime_lock),
        queued_at=NOW,
        judgment_round=1,
    )


def _worker(reservation: WorkerReservation) -> WorkerView:
    return WorkerView(
        worker_id=reservation.worker_id,
        item_id=reservation.item_id,
        episode=reservation.episode,
        evidence_fingerprint=reservation.evidence_fingerprint,
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
        exit_code=None,
        error=None,
        judgment_round=reservation.judgment_round,
    )


class _RecordingStore:
    def __init__(self, worker: WorkerView) -> None:
        self.worker = worker
        self.launch_attempt_visible = False
        self.completion: WorkerCompletion | None = None

    def list_workers(self) -> tuple[WorkerView, ...]:
        return (self.worker,)

    def mark_worker_launch_attempt(
        self,
        worker_id: str,
        *,
        launch_attempted_at: str,
    ) -> bool:
        self.launch_attempt_visible = True
        self.worker = replace(
            self.worker,
            launch_attempted_at=launch_attempted_at,
        )
        return True

    def mark_worker_launched(
        self,
        worker_id: str,
        *,
        pid: int,
        launched_at: str,
    ) -> None:
        self.worker = replace(
            self.worker,
            state=WorkState.RUNNING,
            pid=pid,
            launched_at=launched_at,
        )

    def complete_worker(self, completion: WorkerCompletion) -> None:
        if self.worker.state in {
            WorkState.SUCCEEDED,
            WorkState.FAILED,
            WorkState.INVALID,
            WorkState.SUPERSEDED,
        }:
            existing = (
                self.worker.state,
                self.worker.completed_at,
                self.worker.exit_code,
                self.worker.error,
            )
            received = (
                completion.state,
                completion.completed_at,
                completion.exit_code,
                completion.error,
            )
            if existing != received:
                raise ValueError("Worker has a conflicting completion receipt.")
            return
        self.completion = completion
        self.worker = replace(
            self.worker,
            state=completion.state,
            completed_at=completion.completed_at,
            exit_code=completion.exit_code,
            error=completion.error,
        )


class _PidPersistenceFailureStore(_RecordingStore):
    def mark_worker_launched(
        self,
        worker_id: str,
        *,
        pid: int,
        launched_at: str,
    ) -> None:
        raise RuntimeError("simulated manager failure before PID commit")


class _FailingCloseOutput:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._stream = None

    def __enter__(self):
        self._stream = self._path.open("wb")
        return self._stream

    def __exit__(self, exception_type, exception, traceback) -> None:
        assert self._stream is not None
        self._stream.close()
        raise OSError("simulated output close failure")


class JudgmentWorkerLauncherTests(unittest.TestCase):
    def test_packet_paths_are_private_and_confined_to_worker_directory(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"

            paths = WorkerPacketPaths.create(state_directory, "worker-1")

            self.assertEqual(
                state_directory / "workers" / "worker-1",
                paths.worker_directory,
            )
            self.assertEqual(paths.worker_directory / "request.json", paths.request)
            self.assertEqual(paths.worker_directory / "result.json", paths.result)
            self.assertEqual(paths.worker_directory / "detail.json", paths.detail)
            self.assertEqual(
                paths.worker_directory / "lifetime.lock",
                paths.lifetime_lock,
            )
            self.assertEqual(paths.worker_directory / "stdout.txt", paths.stdout)
            self.assertEqual(paths.worker_directory / "stderr.txt", paths.stderr)
            self.assertEqual(paths.worker_directory / "usage.json", paths.usage)
            self.assertEqual(0o700, paths.worker_directory.stat().st_mode & 0o777)

            with self.assertRaisesRegex(ValueError, "worker_id"):
                WorkerPacketPaths.create(state_directory, "../escape")

    def test_packet_paths_require_canonical_absolute_distinct_paths(self) -> None:
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            state_directory = root / "state"
            paths = WorkerPacketPaths.create(state_directory, "worker-1")
            reservation = _reservation(paths)

            with self.assertRaisesRegex(ValueError, "absolute"):
                WorkerPacketPaths.create(Path("relative-state"), "worker-1")
            with self.assertRaisesRegex(ValueError, "canonical"):
                WorkerPacketPaths.create(
                    root / "unused" / ".." / "aliased-state",
                    "worker-1",
                )
            with self.assertRaisesRegex(ValueError, "absolute"):
                WorkerPacketPaths.from_reservation(
                    state_directory,
                    replace(reservation, request_path="request.json"),
                )
            with self.assertRaisesRegex(ValueError, "canonical"):
                WorkerPacketPaths.from_reservation(
                    state_directory,
                    replace(
                        reservation,
                        request_path=str(
                            paths.worker_directory
                            / ".."
                            / paths.worker_directory.name
                            / "request.json"
                        ),
                    ),
                )
            with self.assertRaisesRegex(ValueError, "distinct"):
                WorkerPacketPaths.from_reservation(
                    state_directory,
                    replace(
                        reservation,
                        result_path=reservation.request_path,
                    ),
                )

    def test_packet_paths_reject_symlinked_state_components(self) -> None:
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            real_state = root / "real-state"
            real_state.mkdir()
            linked_state = root / "linked-state"
            linked_state.symlink_to(real_state, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symlink"):
                WorkerPacketPaths.create(linked_state, "worker-1")

    def test_atomic_publication_revalidates_worker_directory(self) -> None:
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            state_directory = root / "state"
            paths = WorkerPacketPaths.create(state_directory, "worker-1")
            original_directory = root / "original-worker"
            paths.worker_directory.rename(original_directory)
            outside_directory = root / "outside"
            outside_directory.mkdir()
            paths.worker_directory.symlink_to(
                outside_directory,
                target_is_directory=True,
            )

            with self.assertRaisesRegex(ValueError, "symlink"):
                _atomic_write_json(
                    paths.result,
                    {"status": "must-not-write"},
                    worker_directory=paths.worker_directory,
                )

            self.assertFalse((outside_directory / "result.json").exists())

    def test_launch_durably_prepares_packet_before_exact_wrapper_invocation(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            paths = WorkerPacketPaths.create(state_directory, "worker-1")
            reservation = _reservation(paths)
            request = _request(paths)
            store = _RecordingStore(_worker(reservation))
            captured: dict[str, object] = {}

            def process_factory(
                argv: list[str],
                **kwargs: object,
            ) -> SimpleNamespace:
                self.assertTrue(store.launch_attempt_visible)
                self.assertEqual(request, parse_judgment_request(
                    paths.request.read_text(encoding="utf-8")
                ))
                captured["argv"] = argv
                captured["kwargs"] = kwargs
                captured["detail"] = json.loads(
                    paths.detail.read_text(encoding="utf-8")
                )
                return SimpleNamespace(pid=12345)

            times = iter((NOW, LATER))
            launcher = JudgmentWorkerLauncher(
                state_directory,
                store=store,
                clock=lambda: next(times),
                process_factory=process_factory,
                model="trusted-model",
                reasoning_effort="high",
            )

            preparation = launcher.prepare(reservation, request)
            self.assertEqual(
                WorkerPreparationStatus.PREPARED,
                preparation.status,
            )
            outcome = launcher.launch(reservation)

            self.assertEqual(WorkerLaunchStatus.LAUNCHED, outcome.status)
            self.assertEqual(12345, outcome.pid)
            self.assertEqual(
                [
                    sys.executable,
                    "-m",
                    "ci_shepherd.workflow_loop.worker_process",
                    "--request",
                    str(paths.request),
                    "--result",
                    str(paths.result),
                    "--detail",
                    str(paths.detail),
                    "--lifetime-fd",
                    ANY,
                    "--model",
                    "trusted-model",
                    "--reasoning-effort",
                    "high",
                ],
                captured["argv"],
            )
            kwargs = captured["kwargs"]
            assert isinstance(kwargs, dict)
            self.assertEqual(paths.worker_directory, kwargs["cwd"])
            self.assertIs(subprocess.DEVNULL, kwargs["stdin"])
            self.assertTrue(kwargs["close_fds"])
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(1, len(kwargs["pass_fds"]))
            environment = kwargs["env"]
            assert isinstance(environment, dict)
            self.assertTrue(
                Path(environment["PYTHONPATH"].split(os.pathsep)[0]).is_absolute()
            )
            self.assertEqual(0o600, paths.request.stat().st_mode & 0o777)
            self.assertEqual(0o600, paths.detail.stat().st_mode & 0o777)
            self.assertEqual(
                {
                    "schemaVersion": 1,
                    "workerId": "worker-1",
                    "requestPath": str(paths.request),
                    "resultPath": str(paths.result),
                    "stdoutPath": str(paths.stdout),
                    "stderrPath": str(paths.stderr),
                    "usagePath": str(paths.usage),
                    "model": "trusted-model",
                    "reasoningEffort": "high",
                },
                captured["detail"],
            )
            self.assertFalse(paths.lifetime_lock.exists() and paths.lifetime_lock.is_symlink())

    def test_prepared_packet_is_immutable_and_safe_to_replay(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            paths = WorkerPacketPaths.create(state_directory, "worker-1")
            reservation = _reservation(paths)
            request = _request(paths)
            store = _RecordingStore(_worker(reservation))
            launcher = JudgmentWorkerLauncher(
                state_directory,
                store=store,
                clock=lambda: NOW,
                model="trusted-model",
                reasoning_effort="high",
            )

            first = launcher.prepare(reservation, request)
            replay = launcher.prepare(reservation, request)
            changed = launcher.prepare(
                reservation,
                replace(request, prompt="Different prompt must not overwrite."),
            )

            self.assertEqual(WorkerPreparationStatus.PREPARED, first.status)
            self.assertEqual(
                WorkerPreparationStatus.ALREADY_PREPARED,
                replay.status,
            )
            self.assertEqual(WorkerPreparationStatus.FAILED, changed.status)
            self.assertEqual(
                request,
                parse_judgment_request(paths.request.read_text(encoding="utf-8")),
            )
            self.assertIn("already differs", changed.error)

    def test_launch_attempt_is_visible_to_another_store_before_popen(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            store = WorkflowLoopStore(
                state_directory,
                repository="owner/repo",
                branch="main",
            )
            store.initialize()
            paths = WorkerPacketPaths.create(state_directory, "worker-real-store")
            request = _request(paths)
            item = store.upsert_failure(request.failure_run, NOW)
            request = replace(
                request,
                worker_id=paths.worker_directory.name,
                item_id=item.id,
                episode=item.episode,
                evidence_fingerprint=item.evidence_fingerprint,
            )
            reservation = WorkerReservation(
                worker_id=request.worker_id,
                item_id=request.item_id,
                episode=request.episode,
                evidence_fingerprint=request.evidence_fingerprint,
                session_id=request.session_id,
                request_path=str(paths.request),
                result_path=str(paths.result),
                detail_path=str(paths.detail),
                lifetime_lock_path=str(paths.lifetime_lock),
                queued_at=NOW,
                judgment_round=request.round,
            )

            def process_factory(
                argv: list[str],
                **kwargs: object,
            ) -> SimpleNamespace:
                observer = WorkflowLoopStore(
                    state_directory,
                    repository="owner/repo",
                    branch="main",
                )
                observer.initialize()
                visible = observer.list_workers()[0]
                self.assertEqual(NOW, visible.launch_attempted_at)
                self.assertEqual(WorkState.QUEUED, visible.state)
                return SimpleNamespace(pid=23456)

            launcher = JudgmentWorkerLauncher(
                state_directory,
                store=store,
                clock=lambda: NOW,
                process_factory=process_factory,
                model="trusted-model",
                reasoning_effort="high",
            )

            preparation = launcher.prepare(reservation, request)
            self.assertEqual(
                WorkerPreparationStatus.PREPARED,
                preparation.status,
            )
            self.assertTrue(store.reserve_worker(reservation, capacity_limit=1))
            outcome = launcher.launch(reservation)

            self.assertEqual(WorkerLaunchStatus.LAUNCHED, outcome.status)
            self.assertEqual(WorkState.RUNNING, store.list_workers()[0].state)
            script = """
from pathlib import Path
import sys
from ci_shepherd.workflow_loop.state import WorkflowLoopStore

store = WorkflowLoopStore(
    Path(sys.argv[1]),
    repository="owner/repo",
    branch="main",
)
store.initialize()
worker = store.list_workers()[0]
print(f"{worker.worker_id}:{worker.state.value}:{worker.launch_attempted_at}")
"""
            environment = dict(os.environ)
            environment["PYTHONPATH"] = os.pathsep.join(
                str(Path(entry).resolve())
                for entry in environment["PYTHONPATH"].split(os.pathsep)
            )
            observed = subprocess.run(
                [sys.executable, "-c", script, str(state_directory)],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
                env=environment,
            )
            self.assertEqual(
                (0, "worker-real-store:running:2026-09-17T20:00:00Z"),
                (observed.returncode, observed.stdout.strip()),
                observed.stderr,
            )

            duplicate_calls = 0

            def duplicate_factory(
                argv: list[str],
                **kwargs: object,
            ) -> SimpleNamespace:
                nonlocal duplicate_calls
                duplicate_calls += 1
                return SimpleNamespace(pid=34567)

            observer_store = WorkflowLoopStore(
                state_directory,
                repository="owner/repo",
                branch="main",
            )
            observer_store.initialize()
            observer_launcher = JudgmentWorkerLauncher(
                state_directory,
                store=observer_store,
                clock=lambda: LATER,
                process_factory=duplicate_factory,
                model="trusted-model",
                reasoning_effort="high",
            )
            duplicate = observer_launcher.launch(reservation)
            self.assertEqual(
                WorkerLaunchStatus.ALREADY_ATTEMPTED,
                duplicate.status,
            )
            self.assertEqual(0, duplicate_calls)

    def test_new_process_launches_prepared_reserved_packet_exactly_once(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            store = WorkflowLoopStore(
                state_directory,
                repository="owner/repo",
                branch="main",
            )
            store.initialize()
            paths = WorkerPacketPaths.create(state_directory, "worker-cold-start")
            request = _request(paths)
            item = store.upsert_failure(request.failure_run, NOW)
            request = replace(
                request,
                worker_id=paths.worker_directory.name,
                item_id=item.id,
                episode=item.episode,
                evidence_fingerprint=item.evidence_fingerprint,
                prompt="Use this original immutable prompt.",
            )
            reservation = WorkerReservation(
                worker_id=request.worker_id,
                item_id=request.item_id,
                episode=request.episode,
                evidence_fingerprint=request.evidence_fingerprint,
                session_id=request.session_id,
                request_path=str(paths.request),
                result_path=str(paths.result),
                detail_path=str(paths.detail),
                lifetime_lock_path=str(paths.lifetime_lock),
                queued_at=NOW,
                judgment_round=request.round,
            )
            launcher = JudgmentWorkerLauncher(
                state_directory,
                store=store,
                clock=lambda: NOW,
                model="trusted-model",
                reasoning_effort="high",
            )
            self.assertEqual(
                WorkerPreparationStatus.PREPARED,
                launcher.prepare(reservation, request).status,
            )
            self.assertTrue(store.reserve_worker(reservation, capacity_limit=1))

            source_directory = str(
                Path(__file__).resolve().parents[1] / "scripts"
            )
            script = """
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from ci_shepherd.workflow_loop.state import WorkflowLoopStore
from ci_shepherd.workflow_loop.worker import JudgmentWorkerLauncher

state_directory = Path(sys.argv[1])
capture_path = Path(sys.argv[2])
store = WorkflowLoopStore(
    state_directory,
    repository="owner/repo",
    branch="main",
)
store.initialize()
worker = store.list_workers()[0]

def process_factory(argv, **kwargs):
    request_index = argv.index("--request") + 1
    request = json.loads(Path(argv[request_index]).read_text(encoding="utf-8"))
    capture_path.write_text(
        json.dumps({"argv": argv, "prompt": request["prompt"]}),
        encoding="utf-8",
    )
    return SimpleNamespace(pid=24680)

launcher = JudgmentWorkerLauncher(
    state_directory,
    store=store,
    clock=lambda: "2026-09-17T20:00:01Z",
    process_factory=process_factory,
    model="trusted-model",
    reasoning_effort="high",
)
outcome = launcher.launch(worker)
print(outcome.status.value)
"""
            capture_path = paths.worker_directory / "cold-start.json"
            environment = dict(os.environ)
            environment["PYTHONPATH"] = source_directory
            first = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(state_directory),
                    str(capture_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
                env=environment,
            )
            self.assertEqual((0, "launched"), (
                first.returncode,
                first.stdout.strip(),
            ), first.stderr)
            captured = json.loads(capture_path.read_text(encoding="utf-8"))
            self.assertEqual(
                "Use this original immutable prompt.",
                captured["prompt"],
            )

            capture_path.unlink()
            second = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(state_directory),
                    str(capture_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
                env=environment,
            )
            self.assertEqual((0, "already_attempted"), (
                second.returncode,
                second.stdout.strip(),
            ), second.stderr)
            self.assertFalse(capture_path.exists())

    def test_active_lifetime_wins_over_terminal_file_without_expiry(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            paths = WorkerPacketPaths.create(state_directory, "worker-1")
            reservation = _reservation(paths)
            request = _request(paths)
            paths.request.write_text(
                judgment_request_to_json(request),
                encoding="utf-8",
            )
            envelope = {
                "schemaVersion": 1,
                "workerId": request.worker_id,
                "sessionId": request.session_id,
                "itemId": request.item_id,
                "episode": request.episode,
                "evidenceFingerprint": request.evidence_fingerprint,
                "status": "succeeded",
                "exitCode": 0,
                "startedAt": NOW,
                "completedAt": LATER,
                "requestPath": str(paths.request),
                "resultPath": str(paths.result),
                "detailPath": str(paths.detail),
                "stdoutPath": str(paths.stdout),
                "stderrPath": str(paths.stderr),
                "usagePath": str(paths.usage),
                "requestIdentity": {
                    "issueNumber": request.issue_number,
                    "taskId": request.task_id,
                    "pullRequestNumber": request.pull_request_number,
                    "pullRequestHeadSha": request.pull_request_head_sha,
                    "pullRequestHeadRef": request.pull_request_head_ref,
                    "pullRequestBaseRef": request.pull_request_base_ref,
                    "pullRequestObservedAt": request.pull_request_observed_at,
                },
                "judgmentResult": {
                    "schemaVersion": 1,
                    "itemId": request.item_id,
                    "episode": request.episode,
                    "evidenceFingerprint": request.evidence_fingerprint,
                    "decision": "follow_up",
                    "summary": "The compiler failure remains.",
                    "evidenceIds": list(request.evidence_ids),
                    "inScopeJobIds": [900],
                    "copilotRequest": "Fix the compiler failure.",
                },
                "error": None,
            }
            paths.result.write_text(json.dumps(envelope), encoding="utf-8")
            worker = replace(
                _worker(reservation),
                state=WorkState.RUNNING,
                pid=98765,
                launch_attempted_at=NOW,
                launched_at=NOW,
            )
            store = _RecordingStore(worker)
            launcher = JudgmentWorkerLauncher(
                state_directory,
                store=store,
                clock=lambda: "2026-09-17T20:20:01Z",
                model="trusted-model",
                reasoning_effort="high",
            )
            lock = acquire_lifetime_lock(paths.lifetime_lock)
            try:
                active = launcher.observe(worker)
                self.assertEqual(WorkerObservationStatus.RUNNING, active.status)
                self.assertIsNone(active.completion)
                self.assertIsNone(store.completion)
            finally:
                lock.close()

            completed = launcher.observe(worker)
            self.assertEqual(WorkerObservationStatus.COMPLETED, completed.status)
            self.assertEqual(WorkState.SUCCEEDED, completed.completion.state)
            self.assertEqual("follow_up", completed.judgment.decision.value)
            self.assertEqual("task-owned-123", completed.request.task_id)

    def test_post_spawn_pid_failure_never_relaunches_live_worker(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            paths = WorkerPacketPaths.create(state_directory, "worker-1")
            reservation = _reservation(paths)
            request = _request(paths)
            store = _PidPersistenceFailureStore(_worker(reservation))
            child: subprocess.Popen[bytes] | None = None

            def process_factory(
                argv: list[str],
                **kwargs: object,
            ) -> subprocess.Popen[bytes]:
                nonlocal child
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "import time; time.sleep(60)",
                    ],
                    **kwargs,
                )
                return child

            launcher = JudgmentWorkerLauncher(
                state_directory,
                store=store,
                clock=lambda: "2026-09-17T20:20:01Z",
                process_factory=process_factory,
                model="trusted-model",
                reasoning_effort="high",
            )
            self.assertEqual(
                WorkerPreparationStatus.PREPARED,
                launcher.prepare(reservation, request).status,
            )
            try:
                outcome = launcher.launch(reservation)
                self.assertEqual(WorkerLaunchStatus.UNKNOWN, outcome.status)
                self.assertIsNotNone(child)

                observed = launcher.observe(store.worker)
                self.assertEqual(WorkerObservationStatus.RUNNING, observed.status)

                duplicate = launcher.launch(reservation)
                self.assertEqual(
                    WorkerLaunchStatus.ALREADY_ATTEMPTED,
                    duplicate.status,
                )
                self.assertIsNone(duplicate.pid)
                self.assertIsNotNone(store.worker.launch_attempted_at)
            finally:
                if child is not None and child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)

            missing = launcher.observe(store.worker)
            self.assertEqual(
                WorkerObservationStatus.ATTENTION_REQUIRED,
                missing.status,
            )
            self.assertIn("without a terminal envelope", missing.error)

    def test_symlink_packet_path_is_rejected_before_launch(self) -> None:
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            state_directory = root / "state"
            paths = WorkerPacketPaths.create(state_directory, "worker-1")
            reservation = _reservation(paths)
            store = _RecordingStore(_worker(reservation))
            launcher = JudgmentWorkerLauncher(
                state_directory,
                store=store,
                clock=lambda: NOW,
                process_factory=lambda argv, **kwargs: SimpleNamespace(pid=1),
                model="trusted-model",
                reasoning_effort="high",
            )
            self.assertEqual(
                WorkerPreparationStatus.PREPARED,
                launcher.prepare(reservation, _request(paths)).status,
            )
            paths.request.unlink()
            target = root / "outside.json"
            target.write_text("do not replace", encoding="utf-8")
            paths.request.symlink_to(target)
            calls = 0

            def process_factory(
                argv: list[str],
                **kwargs: object,
            ) -> SimpleNamespace:
                nonlocal calls
                calls += 1
                return SimpleNamespace(pid=1)

            launcher = JudgmentWorkerLauncher(
                state_directory,
                store=store,
                clock=lambda: NOW,
                process_factory=process_factory,
                model="trusted-model",
                reasoning_effort="high",
            )

            outcome = launcher.launch(reservation)

            self.assertEqual(WorkerLaunchStatus.FAILED, outcome.status)
            self.assertIn("symlink", outcome.error)
            self.assertEqual(0, calls)
            self.assertFalse(store.launch_attempt_visible)
            self.assertEqual("do not replace", target.read_text(encoding="utf-8"))

    def test_known_popen_oserror_is_recorded_as_failed(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            paths = WorkerPacketPaths.create(state_directory, "worker-1")
            reservation = _reservation(paths)
            store = _RecordingStore(_worker(reservation))
            times = iter((NOW, LATER))
            launcher = JudgmentWorkerLauncher(
                state_directory,
                store=store,
                clock=lambda: next(times),
                process_factory=lambda argv, **kwargs: (_ for _ in ()).throw(
                    OSError("executable missing")
                ),
                model="trusted-model",
                reasoning_effort="high",
            )

            self.assertEqual(
                WorkerPreparationStatus.PREPARED,
                launcher.prepare(reservation, _request(paths)).status,
            )
            outcome = launcher.launch(reservation)

            self.assertEqual(WorkerLaunchStatus.FAILED, outcome.status)
            self.assertEqual(WorkState.FAILED, outcome.completion.state)
            self.assertEqual(outcome.completion, store.completion)
            self.assertIn("did not start", outcome.error)

    def test_keyboard_interrupt_propagates_without_spawn_retry_or_lock_leak(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            paths = WorkerPacketPaths.create(state_directory, "worker-1")
            reservation = _reservation(paths)
            store = _RecordingStore(_worker(reservation))
            calls = 0

            def interrupted_factory(
                argv: list[str],
                **kwargs: object,
            ) -> SimpleNamespace:
                nonlocal calls
                calls += 1
                raise KeyboardInterrupt

            launcher = JudgmentWorkerLauncher(
                state_directory,
                store=store,
                clock=lambda: NOW,
                process_factory=interrupted_factory,
                model="trusted-model",
                reasoning_effort="high",
            )
            self.assertEqual(
                WorkerPreparationStatus.PREPARED,
                launcher.prepare(reservation, _request(paths)).status,
            )

            with self.assertRaises(KeyboardInterrupt):
                launcher.launch(reservation)

            self.assertEqual(1, calls)
            self.assertFalse(is_lifetime_active(paths.lifetime_lock))
            self.assertIsNone(store.completion)
            retry = launcher.launch(reservation)
            self.assertEqual(WorkerLaunchStatus.ALREADY_ATTEMPTED, retry.status)
            self.assertEqual(1, calls)

    def test_post_spawn_output_close_error_retains_live_unknown_ownership(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            paths = WorkerPacketPaths.create(state_directory, "worker-1")
            reservation = _reservation(paths)
            store = _RecordingStore(_worker(reservation))
            child: subprocess.Popen[bytes] | None = None

            def process_factory(
                argv: list[str],
                **kwargs: object,
            ) -> subprocess.Popen[bytes]:
                nonlocal child
                child = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    **kwargs,
                )
                return child

            launcher = JudgmentWorkerLauncher(
                state_directory,
                store=store,
                clock=lambda: NOW,
                process_factory=process_factory,
                model="trusted-model",
                reasoning_effort="high",
            )
            self.assertEqual(
                WorkerPreparationStatus.PREPARED,
                launcher.prepare(reservation, _request(paths)).status,
            )

            try:
                with patch(
                    "ci_shepherd.workflow_loop.worker._open_output",
                    side_effect=lambda path, **kwargs: _FailingCloseOutput(path),
                ):
                    outcome = launcher.launch(reservation)

                self.assertEqual(WorkerLaunchStatus.UNKNOWN, outcome.status)
                self.assertIsNone(outcome.completion)
                self.assertIsNone(store.completion)
                self.assertIsNotNone(child)
                self.assertTrue(is_lifetime_active(paths.lifetime_lock))
                observed = launcher.observe(store.worker)
                self.assertEqual(WorkerObservationStatus.RUNNING, observed.status)
            finally:
                if child is not None and child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)

            self.assertFalse(is_lifetime_active(paths.lifetime_lock))

    def test_missing_lifetime_lock_is_unknown_not_inactive(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            paths = WorkerPacketPaths.create(state_directory, "worker-1")
            reservation = _reservation(paths)
            worker = replace(
                _worker(reservation),
                launch_attempted_at=NOW,
            )
            store = _RecordingStore(worker)
            launcher = JudgmentWorkerLauncher(
                state_directory,
                store=store,
                clock=lambda: LATER,
                model="trusted-model",
                reasoning_effort="high",
            )

            observed = launcher.observe(worker)

            self.assertEqual(WorkerObservationStatus.UNKNOWN, observed.status)
            self.assertIn("cannot be verified", observed.error)
            self.assertIsNone(store.completion)

    def test_missing_envelope_observation_reuses_terminal_receipt(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            paths = WorkerPacketPaths.create(state_directory, "worker-1")
            reservation = _reservation(paths)
            request = _request(paths)
            worker = replace(
                _worker(reservation),
                state=WorkState.RUNNING,
                pid=12345,
                launch_attempted_at=NOW,
                launched_at=NOW,
            )
            store = _RecordingStore(worker)
            times = iter((LATER, "2026-09-17T20:30:00Z"))
            launcher = JudgmentWorkerLauncher(
                state_directory,
                store=store,
                clock=lambda: next(times),
                model="trusted-model",
                reasoning_effort="high",
            )
            self.assertEqual(
                WorkerPreparationStatus.PREPARED,
                launcher.prepare(reservation, request).status,
            )
            lock = acquire_lifetime_lock(paths.lifetime_lock)
            lock.close()

            first = launcher.observe(worker)
            second = launcher.observe(worker)

            self.assertEqual(
                WorkerObservationStatus.ATTENTION_REQUIRED,
                first.status,
            )
            self.assertEqual(first.completion, second.completion)
            self.assertEqual(LATER, second.completion.completed_at)
            self.assertEqual(first.error, second.error)


if __name__ == "__main__":
    unittest.main()
