from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
from typing import Callable, Protocol

from .lifetime import (
    LifetimeLockConflictError,
    acquire_lifetime_lock,
    is_lifetime_active,
)
from .models import (
    JudgmentRequest,
    JudgmentResult,
    WorkerCompletion,
    WorkerReservation,
    WorkerView,
    WorkState,
    judgment_request_to_json,
    parse_judgment_request,
    parse_judgment_result,
)


_SOURCE_DIRECTORY = Path(__file__).resolve().parents[2]
_MAX_ENVELOPE_BYTES = 1_048_576
_DETAIL_KEYS = frozenset(
    {
        "schemaVersion",
        "workerId",
        "requestPath",
        "resultPath",
        "stdoutPath",
        "stderrPath",
        "usagePath",
        "model",
        "reasoningEffort",
    }
)
_ENVELOPE_KEYS = frozenset(
    {
        "schemaVersion",
        "workerId",
        "sessionId",
        "itemId",
        "episode",
        "evidenceFingerprint",
        "status",
        "exitCode",
        "startedAt",
        "completedAt",
        "requestPath",
        "resultPath",
        "detailPath",
        "stdoutPath",
        "stderrPath",
        "usagePath",
        "requestIdentity",
        "judgmentResult",
        "error",
    }
)


class _WorkerStore(Protocol):
    def list_workers(self) -> tuple[WorkerView, ...]: ...

    def mark_worker_launch_attempt(
        self,
        worker_id: str,
        *,
        launch_attempted_at: str,
    ) -> bool: ...

    def mark_worker_launched(
        self,
        worker_id: str,
        *,
        pid: int,
        launched_at: str,
    ) -> None: ...

    def complete_worker(self, completion: WorkerCompletion) -> None: ...


class WorkerLaunchStatus(StrEnum):
    LAUNCHED = "launched"
    ALREADY_ATTEMPTED = "already_attempted"
    FAILED = "failed"
    UNKNOWN = "unknown"


class WorkerPreparationStatus(StrEnum):
    PREPARED = "prepared"
    ALREADY_PREPARED = "already_prepared"
    FAILED = "failed"


class WorkerObservationStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    ATTENTION_REQUIRED = "attention_required"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class WorkerLaunchResult:
    status: WorkerLaunchStatus
    worker_id: str
    pid: int | None
    completion: WorkerCompletion | None
    error: str | None


@dataclass(frozen=True, slots=True)
class WorkerPreparationResult:
    status: WorkerPreparationStatus
    worker_id: str
    paths: WorkerPacketPaths
    request: JudgmentRequest | None
    error: str | None


@dataclass(frozen=True, slots=True)
class WorkerObservation:
    status: WorkerObservationStatus
    worker_id: str
    completion: WorkerCompletion | None
    request: JudgmentRequest | None
    judgment: JudgmentResult | None
    request_path: Path
    result_path: Path
    detail_path: Path
    error: str | None


@dataclass(frozen=True, slots=True)
class WorkerPacketPaths:
    worker_directory: Path
    request: Path
    result: Path
    detail: Path
    lifetime_lock: Path
    stdout: Path
    stderr: Path
    usage: Path

    @classmethod
    def create(cls, state_directory: Path, worker_id: str) -> WorkerPacketPaths:
        if not isinstance(state_directory, Path):
            raise ValueError("state_directory must be a pathlib.Path.")
        _require_canonical_absolute_path(state_directory, "state_directory")
        _reject_symlink_components(state_directory)
        if (
            not isinstance(worker_id, str)
            or not worker_id
            or worker_id in {".", ".."}
            or Path(worker_id).name != worker_id
        ):
            raise ValueError("worker_id must be one safe path component.")

        workers_directory = state_directory / "workers"
        worker_directory = workers_directory / worker_id
        for directory in (state_directory, workers_directory, worker_directory):
            _require_canonical_absolute_path(directory, "worker directory")
            _reject_symlink_components(directory)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            _reject_symlink_components(directory)
            os.chmod(directory, 0o700)

        paths = cls(
            worker_directory=worker_directory,
            request=worker_directory / "request.json",
            result=worker_directory / "result.json",
            detail=worker_directory / "detail.json",
            lifetime_lock=worker_directory / "lifetime.lock",
            stdout=worker_directory / "stdout.txt",
            stderr=worker_directory / "stderr.txt",
            usage=worker_directory / "usage.json",
        )
        _validate_packet_paths(paths, state_directory)
        return paths

    @classmethod
    def from_reservation(
        cls,
        state_directory: Path,
        reservation: WorkerReservation,
    ) -> WorkerPacketPaths:
        paths = cls.create(state_directory, reservation.worker_id)
        supplied = {
            "request_path": Path(reservation.request_path),
            "result_path": Path(reservation.result_path),
            "detail_path": Path(reservation.detail_path),
            "lifetime_lock_path": Path(reservation.lifetime_lock_path),
        }
        expected = {
            "request_path": paths.request,
            "result_path": paths.result,
            "detail_path": paths.detail,
            "lifetime_lock_path": paths.lifetime_lock,
        }
        for name, supplied_path in supplied.items():
            _require_canonical_absolute_path(supplied_path, name)
        if len(set(supplied.values())) != len(supplied):
            raise ValueError("Persisted worker paths must be mutually distinct.")
        for name, supplied_path in supplied.items():
            if supplied_path != expected[name]:
                raise ValueError(
                    f"{name} must name the private worker packet path "
                    f"{expected[name]}."
                )
            _reject_symlink(supplied_path)
        for output in (paths.stdout, paths.stderr, paths.usage):
            _reject_symlink(output)
        _validate_packet_paths(paths, state_directory)
        return paths

    @classmethod
    def from_worker(
        cls,
        state_directory: Path,
        worker: WorkerView,
    ) -> WorkerPacketPaths:
        return cls.from_reservation(
            state_directory,
            _reservation_from_worker(worker),
        )


class JudgmentWorkerLauncher:
    def __init__(
        self,
        state_directory: Path,
        *,
        store: _WorkerStore,
        clock: Callable[[], str | datetime],
        process_factory: Callable[..., object] = subprocess.Popen,
        model: str,
        reasoning_effort: str,
    ) -> None:
        if not isinstance(state_directory, Path):
            raise ValueError("state_directory must be a pathlib.Path.")
        self._state_directory = state_directory
        self._store = store
        self._clock = clock
        self._process_factory = process_factory
        self._model = _trusted_option(model, "model")
        self._reasoning_effort = _trusted_option(
            reasoning_effort,
            "reasoning_effort",
        )

    def packet_paths(self, worker_id: str) -> WorkerPacketPaths:
        return WorkerPacketPaths.create(self._state_directory, worker_id)

    def prepare(
        self,
        reservation: WorkerReservation,
        request: JudgmentRequest,
    ) -> WorkerPreparationResult:
        paths = WorkerPacketPaths.create(
            self._state_directory,
            reservation.worker_id,
        )
        try:
            _validate_request_identity(reservation, request)
            paths = WorkerPacketPaths.from_reservation(
                self._state_directory,
                reservation,
            )
            request_text = judgment_request_to_json(request)
            detail = self._detail_document(reservation, paths)
            detail_text = json.dumps(
                detail,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            request_created = _atomic_create_text(
                paths.request,
                request_text,
                worker_directory=paths.worker_directory,
            )
            detail_created = _atomic_create_text(
                paths.detail,
                detail_text,
                worker_directory=paths.worker_directory,
            )
            persisted_request, _ = self._load_prepared_packet(
                reservation,
                paths,
            )
        except (OSError, UnicodeError, ValueError) as error:
            return WorkerPreparationResult(
                status=WorkerPreparationStatus.FAILED,
                worker_id=reservation.worker_id,
                paths=paths,
                request=None,
                error=f"Worker packet preparation failed: {error}",
            )
        return WorkerPreparationResult(
            status=(
                WorkerPreparationStatus.PREPARED
                if request_created or detail_created
                else WorkerPreparationStatus.ALREADY_PREPARED
            ),
            worker_id=reservation.worker_id,
            paths=paths,
            request=persisted_request,
            error=None,
        )

    def launch(
        self,
        reservation: WorkerReservation | WorkerView,
    ) -> WorkerLaunchResult:
        if isinstance(reservation, WorkerView):
            reservation = _reservation_from_worker(reservation)
        elif not isinstance(reservation, WorkerReservation):
            raise ValueError("reservation must be a WorkerReservation or WorkerView.")
        try:
            worker = self._matching_worker(reservation)
            paths = WorkerPacketPaths.from_reservation(
                self._state_directory,
                reservation,
            )
            request, _ = self._load_prepared_packet(reservation, paths)
        except (OSError, ValueError) as error:
            return WorkerLaunchResult(
                WorkerLaunchStatus.FAILED,
                reservation.worker_id,
                None,
                None,
                f"Worker packet preparation failed: {error}",
            )

        if worker.launch_attempted_at is not None:
            return WorkerLaunchResult(
                WorkerLaunchStatus.ALREADY_ATTEMPTED,
                worker.worker_id,
                worker.pid,
                None,
                "The durable launch attempt already exists; it will not be repeated.",
            )

        try:
            lock = acquire_lifetime_lock(paths.lifetime_lock)
        except LifetimeLockConflictError as error:
            return WorkerLaunchResult(
                WorkerLaunchStatus.UNKNOWN,
                reservation.worker_id,
                None,
                None,
                f"Worker lifetime is already active: {error}",
            )
        except (OSError, ValueError) as error:
            return WorkerLaunchResult(
                WorkerLaunchStatus.FAILED,
                reservation.worker_id,
                None,
                None,
                f"Worker packet preparation failed: {error}",
            )

        descriptor = lock.fileno()
        process: object | None = None
        try:
            try:
                attempted_at = _clock_timestamp(self._clock)
                marked = self._store.mark_worker_launch_attempt(
                    reservation.worker_id,
                    launch_attempted_at=attempted_at,
                )
            except Exception as error:
                return WorkerLaunchResult(
                    WorkerLaunchStatus.UNKNOWN,
                    reservation.worker_id,
                    None,
                    None,
                    "Worker launch-attempt persistence is unknown: "
                    f"{type(error).__name__}: {error}",
                )
            if not marked:
                return WorkerLaunchResult(
                    WorkerLaunchStatus.ALREADY_ATTEMPTED,
                    reservation.worker_id,
                    None,
                    None,
                    "The durable launch attempt already exists; it will not be repeated.",
                )

            argv = [
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
                str(descriptor),
                "--model",
                self._model,
                "--reasoning-effort",
                self._reasoning_effort,
            ]
            environment = dict(os.environ)
            existing_pythonpath = environment.get("PYTHONPATH")
            source_directory = str(_SOURCE_DIRECTORY)
            environment["PYTHONPATH"] = (
                source_directory
                if not existing_pythonpath
                else os.pathsep.join(
                    (
                        source_directory,
                        *(
                            str(Path(entry).resolve())
                            for entry in existing_pythonpath.split(os.pathsep)
                            if entry
                        ),
                    )
                )
            )
            try:
                with (
                    _open_output(
                        paths.stdout,
                        worker_directory=paths.worker_directory,
                    ) as stdout,
                    _open_output(
                        paths.stderr,
                        worker_directory=paths.worker_directory,
                    ) as stderr,
                ):
                    process = self._process_factory(
                        argv,
                        cwd=paths.worker_directory,
                        stdin=subprocess.DEVNULL,
                        stdout=stdout,
                        stderr=stderr,
                        close_fds=True,
                        pass_fds=(descriptor,),
                        start_new_session=True,
                        env=environment,
                    )
            except OSError as error:
                if process is None:
                    return self._known_launch_failure(reservation, error)
                return WorkerLaunchResult(
                    WorkerLaunchStatus.UNKNOWN,
                    reservation.worker_id,
                    _process_pid(process),
                    None,
                    "Worker started, but post-launch stream handling is "
                    f"unknown: {error}",
                )
            except Exception as error:
                return WorkerLaunchResult(
                    WorkerLaunchStatus.UNKNOWN,
                    reservation.worker_id,
                    None,
                    None,
                    "Worker launch outcome is unknown after the durable "
                    f"attempt: {type(error).__name__}: {error}",
                )

            pid = getattr(process, "pid", None)
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                return WorkerLaunchResult(
                    WorkerLaunchStatus.UNKNOWN,
                    reservation.worker_id,
                    None,
                    None,
                    "Worker process returned without a valid PID after launch.",
                )
            try:
                launched_at = _clock_timestamp(self._clock)
                self._store.mark_worker_launched(
                    reservation.worker_id,
                    pid=pid,
                    launched_at=launched_at,
                )
            except Exception as error:
                return WorkerLaunchResult(
                    WorkerLaunchStatus.UNKNOWN,
                    reservation.worker_id,
                    pid,
                    None,
                    "Worker started but PID persistence is unknown: "
                    f"{type(error).__name__}: {error}",
                )
            return WorkerLaunchResult(
                WorkerLaunchStatus.LAUNCHED,
                reservation.worker_id,
                pid,
                None,
                None,
            )
        finally:
            lock.close()

    def _detail_document(
        self,
        reservation: WorkerReservation,
        paths: WorkerPacketPaths,
    ) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "workerId": reservation.worker_id,
            "requestPath": str(paths.request),
            "resultPath": str(paths.result),
            "stdoutPath": str(paths.stdout),
            "stderrPath": str(paths.stderr),
            "usagePath": str(paths.usage),
            "model": self._model,
            "reasoningEffort": self._reasoning_effort,
        }

    def _load_prepared_packet(
        self,
        reservation: WorkerReservation,
        paths: WorkerPacketPaths,
    ) -> tuple[JudgmentRequest, dict[str, object]]:
        request = parse_judgment_request(
            _read_bounded_text(
                paths.request,
                _MAX_ENVELOPE_BYTES,
                worker_directory=paths.worker_directory,
            )
        )
        _validate_request_identity(reservation, request)
        detail = _load_strict_object(
            _read_bounded_text(
                paths.detail,
                _MAX_ENVELOPE_BYTES,
                worker_directory=paths.worker_directory,
            ),
            "Worker detail manifest",
        )
        if frozenset(detail) != _DETAIL_KEYS:
            raise ValueError(
                "Worker detail manifest fields do not match the schema."
            )
        if detail != self._detail_document(reservation, paths):
            raise ValueError(
                "Worker detail manifest contradicts the trusted invocation."
            )
        return request, detail

    def observe(self, worker: WorkerView) -> WorkerObservation:
        if not isinstance(worker, WorkerView):
            raise ValueError("worker must be a WorkerView.")
        try:
            paths = WorkerPacketPaths.from_worker(
                self._state_directory,
                worker,
            )
        except (OSError, ValueError) as error:
            return self._unknown_observation(
                worker,
                f"Worker paths cannot be verified: {error}",
            )
        try:
            active = is_lifetime_active(paths.lifetime_lock)
        except (OSError, ValueError, NotImplementedError) as error:
            return self._unknown_observation(
                worker,
                f"Worker lifetime cannot be verified: {error}",
            )
        if active:
            return WorkerObservation(
                status=WorkerObservationStatus.RUNNING,
                worker_id=worker.worker_id,
                completion=None,
                request=None,
                judgment=None,
                request_path=paths.request,
                result_path=paths.result,
                detail_path=paths.detail,
                error=None,
            )

        try:
            request = parse_judgment_request(
                _read_bounded_text(
                    paths.request,
                    _MAX_ENVELOPE_BYTES,
                    worker_directory=paths.worker_directory,
                )
            )
            _validate_worker_request(worker, request)
        except (OSError, UnicodeError, ValueError) as error:
            return self._record_attention(
                worker,
                paths,
                state=WorkState.INVALID,
                exit_code=None,
                error=f"Worker request is invalid after lifetime ended: {error}",
            )

        try:
            envelope = _load_strict_object(
                _read_bounded_text(
                    paths.result,
                    _MAX_ENVELOPE_BYTES,
                    worker_directory=paths.worker_directory,
                ),
                "Worker terminal envelope",
            )
            judgment, completion = _parse_terminal_envelope(
                envelope,
                worker=worker,
                request=request,
                paths=paths,
            )
        except FileNotFoundError:
            return self._record_attention(
                worker,
                paths,
                state=WorkState.FAILED,
                exit_code=None,
                error=(
                    "Worker lifetime ended without a terminal envelope; "
                    "manual attention is required."
                ),
                request=request,
            )
        except (OSError, UnicodeError, ValueError) as error:
            return self._record_attention(
                worker,
                paths,
                state=WorkState.INVALID,
                exit_code=None,
                error=f"Worker terminal envelope is invalid: {error}",
                request=request,
            )

        self._store.complete_worker(completion)
        if completion.state is WorkState.SUCCEEDED:
            return WorkerObservation(
                status=WorkerObservationStatus.COMPLETED,
                worker_id=worker.worker_id,
                completion=completion,
                request=request,
                judgment=judgment,
                request_path=paths.request,
                result_path=paths.result,
                detail_path=paths.detail,
                error=None,
            )
        return WorkerObservation(
            status=WorkerObservationStatus.ATTENTION_REQUIRED,
            worker_id=worker.worker_id,
            completion=completion,
            request=request,
            judgment=None,
            request_path=paths.request,
            result_path=paths.result,
            detail_path=paths.detail,
            error=completion.error,
        )

    def _matching_worker(self, reservation: WorkerReservation) -> WorkerView:
        workers = tuple(
            worker
            for worker in self._store.list_workers()
            if worker.worker_id == reservation.worker_id
        )
        if len(workers) != 1:
            raise ValueError("The worker reservation is not present in the store.")
        worker = workers[0]
        if (
            worker.item_id != reservation.item_id
            or worker.episode != reservation.episode
            or worker.evidence_fingerprint != reservation.evidence_fingerprint
            or worker.session_id != reservation.session_id
            or worker.request_path != reservation.request_path
            or worker.result_path != reservation.result_path
            or worker.detail_path != reservation.detail_path
            or worker.lifetime_lock_path != reservation.lifetime_lock_path
        ):
            raise ValueError("The stored worker contradicts the reservation.")
        return worker

    def _record_attention(
        self,
        worker: WorkerView,
        paths: WorkerPacketPaths,
        *,
        state: WorkState,
        exit_code: int | None,
        error: str,
        request: JudgmentRequest | None = None,
    ) -> WorkerObservation:
        current = next(
            (
                candidate
                for candidate in self._store.list_workers()
                if candidate.worker_id == worker.worker_id
            ),
            worker,
        )
        if current.state in {
            WorkState.SUCCEEDED,
            WorkState.FAILED,
            WorkState.INVALID,
            WorkState.SUPERSEDED,
        }:
            if (
                current.state is not state
                or current.exit_code != exit_code
                or current.error != error
                or current.completed_at is None
            ):
                return WorkerObservation(
                    status=WorkerObservationStatus.UNKNOWN,
                    worker_id=worker.worker_id,
                    completion=None,
                    request=request,
                    judgment=None,
                    request_path=paths.request,
                    result_path=paths.result,
                    detail_path=paths.detail,
                    error=(
                        "Stored terminal worker metadata contradicts the "
                        "current artifact observation."
                    ),
                )
            completion = WorkerCompletion(
                worker_id=current.worker_id,
                state=current.state,
                completed_at=current.completed_at,
                exit_code=current.exit_code,
                error=current.error,
            )
            return WorkerObservation(
                status=WorkerObservationStatus.ATTENTION_REQUIRED,
                worker_id=worker.worker_id,
                completion=completion,
                request=request,
                judgment=None,
                request_path=paths.request,
                result_path=paths.result,
                detail_path=paths.detail,
                error=error,
            )
        completion = WorkerCompletion(
            worker_id=worker.worker_id,
            state=state,
            completed_at=_clock_timestamp(self._clock),
            exit_code=exit_code,
            error=error,
        )
        self._store.complete_worker(completion)
        return WorkerObservation(
            status=WorkerObservationStatus.ATTENTION_REQUIRED,
            worker_id=worker.worker_id,
            completion=completion,
            request=request,
            judgment=None,
            request_path=paths.request,
            result_path=paths.result,
            detail_path=paths.detail,
            error=error,
        )

    def _known_launch_failure(
        self,
        reservation: WorkerReservation,
        error: OSError,
    ) -> WorkerLaunchResult:
        try:
            completion = WorkerCompletion(
                worker_id=reservation.worker_id,
                state=WorkState.FAILED,
                completed_at=_clock_timestamp(self._clock),
                exit_code=None,
                error=f"Worker process did not start: {error}",
            )
            self._store.complete_worker(completion)
        except Exception as persistence_error:
            return WorkerLaunchResult(
                WorkerLaunchStatus.UNKNOWN,
                reservation.worker_id,
                None,
                None,
                "Worker did not start, but failure persistence is unknown: "
                f"{type(persistence_error).__name__}: {persistence_error}",
            )
        return WorkerLaunchResult(
            WorkerLaunchStatus.FAILED,
            reservation.worker_id,
            None,
            completion,
            completion.error,
        )

    @staticmethod
    def _unknown_observation(
        worker: WorkerView,
        error: str,
    ) -> WorkerObservation:
        return WorkerObservation(
            status=WorkerObservationStatus.UNKNOWN,
            worker_id=worker.worker_id,
            completion=None,
            request=None,
            judgment=None,
            request_path=Path(worker.request_path),
            result_path=Path(worker.result_path),
            detail_path=Path(worker.detail_path),
            error=error,
        )


def _validate_request_identity(
    reservation: WorkerReservation,
    request: JudgmentRequest,
) -> None:
    if (
        request.worker_id != reservation.worker_id
        or request.session_id != reservation.session_id
        or request.item_id != reservation.item_id
        or request.episode != reservation.episode
        or request.evidence_fingerprint != reservation.evidence_fingerprint
        or request.round != reservation.judgment_round
    ):
        raise ValueError("Judgment request contradicts the worker reservation.")


def _reservation_from_worker(worker: WorkerView) -> WorkerReservation:
    return WorkerReservation(
        worker_id=worker.worker_id,
        item_id=worker.item_id,
        episode=worker.episode,
        evidence_fingerprint=worker.evidence_fingerprint,
        session_id=worker.session_id,
        request_path=worker.request_path,
        result_path=worker.result_path,
        detail_path=worker.detail_path,
        lifetime_lock_path=worker.lifetime_lock_path,
        queued_at=worker.queued_at,
        judgment_round=worker.judgment_round,
    )


def _validate_worker_request(
    worker: WorkerView,
    request: JudgmentRequest,
) -> None:
    if (
        request.worker_id != worker.worker_id
        or request.session_id != worker.session_id
        or request.item_id != worker.item_id
        or request.episode != worker.episode
        or request.evidence_fingerprint != worker.evidence_fingerprint
        or request.round != worker.judgment_round
    ):
        raise ValueError("Judgment request contradicts the stored worker.")


def _parse_terminal_envelope(
    envelope: dict[str, object],
    *,
    worker: WorkerView,
    request: JudgmentRequest,
    paths: WorkerPacketPaths,
) -> tuple[JudgmentResult | None, WorkerCompletion]:
    if frozenset(envelope) != _ENVELOPE_KEYS:
        raise ValueError("Terminal envelope fields do not match the schema.")
    expected = {
        "schemaVersion": 1,
        "workerId": worker.worker_id,
        "sessionId": worker.session_id,
        "itemId": worker.item_id,
        "episode": worker.episode,
        "evidenceFingerprint": worker.evidence_fingerprint,
        "requestPath": str(paths.request),
        "resultPath": str(paths.result),
        "detailPath": str(paths.detail),
        "stdoutPath": str(paths.stdout),
        "stderrPath": str(paths.stderr),
        "usagePath": str(paths.usage),
    }
    for name, value in expected.items():
        if envelope.get(name) != value:
            raise ValueError(f"Terminal envelope has invalid {name}.")
    expected_request_identity = {
        "issueNumber": request.issue_number,
        "taskId": request.task_id,
        "pullRequestNumber": request.pull_request_number,
        "pullRequestHeadSha": request.pull_request_head_sha,
        "pullRequestHeadRef": request.pull_request_head_ref,
        "pullRequestBaseRef": request.pull_request_base_ref,
        "pullRequestObservedAt": request.pull_request_observed_at,
    }
    if envelope.get("requestIdentity") != expected_request_identity:
        raise ValueError("Terminal envelope request identity is invalid.")
    completed_at = envelope.get("completedAt")
    if not isinstance(completed_at, str):
        raise ValueError("Terminal envelope completedAt must be a string.")
    started_at = envelope.get("startedAt")
    if not isinstance(started_at, str):
        raise ValueError("Terminal envelope startedAt must be a string.")
    exit_code = envelope.get("exitCode")
    if exit_code is not None and (
        not isinstance(exit_code, int) or isinstance(exit_code, bool)
    ):
        raise ValueError("Terminal envelope exitCode must be an integer or null.")
    status = envelope.get("status")
    error = envelope.get("error")
    raw_result = envelope.get("judgmentResult")
    if status == "succeeded":
        if exit_code != 0 or error is not None or not isinstance(raw_result, dict):
            raise ValueError("Successful terminal envelope is inconsistent.")
        judgment = parse_judgment_result(
            json.dumps(
                raw_result,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ),
            request,
        )
        completion = WorkerCompletion(
            worker_id=worker.worker_id,
            state=WorkState.SUCCEEDED,
            completed_at=completed_at,
            exit_code=exit_code,
            error=None,
        )
        return judgment, completion
    if status not in {"failed", "invalid"}:
        raise ValueError("Terminal envelope status is invalid.")
    if raw_result is not None or not isinstance(error, str) or not error:
        raise ValueError("Unsuccessful terminal envelope is inconsistent.")
    completion = WorkerCompletion(
        worker_id=worker.worker_id,
        state=(
            WorkState.FAILED if status == "failed" else WorkState.INVALID
        ),
        completed_at=completed_at,
        exit_code=exit_code,
        error=error,
    )
    return None, completion


def _load_strict_object(text: str, name: str) -> dict[str, object]:
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
        raise ValueError(f"{name} must be exactly one JSON object.") from error
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object.")
    return value


def _read_bounded_text(
    path: Path,
    maximum: int,
    *,
    worker_directory: Path,
) -> str:
    _validate_io_path(path, worker_directory)
    descriptor = os.open(path, os.O_RDONLY | _no_follow_flag())
    with os.fdopen(descriptor, "rb") as stream:
        content = stream.read(maximum + 1)
    if len(content) > maximum:
        raise ValueError(f"{path.name} exceeds the {maximum}-byte limit.")
    return content.decode("utf-8")


def _trusted_option(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError(f"{name} must be a trusted nonempty string.")
    return value


def _process_pid(process: object) -> int | None:
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    return pid


def _clock_timestamp(clock: Callable[[], str | datetime]) -> str:
    value = clock()
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("clock must return an aware UTC datetime.")
        return (
            value.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
    raise ValueError("clock must return an RFC3339 string or datetime.")


def _reject_symlink(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    if path.is_symlink():
        raise ValueError(f"Worker packet path must not be a symlink: {path}")


def _open_output(path: Path, *, worker_directory: Path):
    _validate_io_path(path, worker_directory)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _no_follow_flag(),
        0o600,
    )
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "wb")


def _atomic_write_json(
    path: Path,
    document: object,
    *,
    worker_directory: Path,
) -> None:
    _atomic_write_text(
        path,
        json.dumps(
            document,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
        worker_directory=worker_directory,
    )


def _atomic_create_text(
    path: Path,
    text: str,
    *,
    worker_directory: Path,
) -> bool:
    _validate_io_path(path, worker_directory)
    try:
        existing = _read_bounded_text(
            path,
            _MAX_ENVELOPE_BYTES,
            worker_directory=worker_directory,
        )
    except FileNotFoundError:
        pass
    else:
        if existing != text:
            raise ValueError(f"Immutable worker packet already differs: {path}")
        return False

    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    _validate_io_path(temporary, worker_directory)
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow_flag(),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(
                temporary,
                path,
                follow_symlinks=False,
            )
        except FileExistsError:
            existing = _read_bounded_text(
                path,
                _MAX_ENVELOPE_BYTES,
                worker_directory=worker_directory,
            )
            if existing != text:
                raise ValueError(
                    f"Immutable worker packet already differs: {path}"
                )
            return False
        _validate_io_path(path, worker_directory)
        os.chmod(path, 0o600)
        _fsync_directory(worker_directory)
        return True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_write_text(
    path: Path,
    text: str,
    *,
    worker_directory: Path,
) -> None:
    _validate_io_path(path, worker_directory)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    _validate_io_path(temporary, worker_directory)
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow_flag(),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.close(descriptor)
        descriptor = -1
        _validate_io_path(path, worker_directory)
        os.replace(temporary, path)
        _validate_io_path(path, worker_directory)
        os.chmod(path, 0o600)
        _fsync_directory(worker_directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _fsync_directory(directory: Path) -> None:
    directory_descriptor = os.open(
        directory,
        os.O_RDONLY | _no_follow_flag(),
    )
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _validate_packet_paths(
    paths: WorkerPacketPaths,
    state_directory: Path,
) -> None:
    values = (
        paths.request,
        paths.result,
        paths.detail,
        paths.lifetime_lock,
        paths.stdout,
        paths.stderr,
        paths.usage,
    )
    if len(set(values)) != len(values):
        raise ValueError("Worker packet paths must be mutually distinct.")
    for path in (paths.worker_directory, *values):
        _require_canonical_absolute_path(path, "worker packet path")
        if not path.is_relative_to(state_directory) or path == state_directory:
            raise ValueError(
                "Worker packet paths must be strictly beneath state_directory."
            )
        _reject_symlink_components(path)
    if any(path.parent != paths.worker_directory for path in values):
        raise ValueError("Worker packet files must be direct worker-directory children.")


def _validate_io_path(path: Path, worker_directory: Path) -> None:
    _require_canonical_absolute_path(worker_directory, "worker_directory")
    _require_canonical_absolute_path(path, "worker I/O path")
    if path.parent != worker_directory:
        raise ValueError("Worker I/O path must stay in the worker directory.")
    _reject_symlink_components(worker_directory)
    _reject_symlink(path)


def _require_canonical_absolute_path(path: Path, name: str) -> None:
    if not path.is_absolute():
        raise ValueError(f"{name} must be absolute.")
    if ".." in path.parts or Path(os.path.normpath(path)) != path:
        raise ValueError(f"{name} must use its canonical path representation.")


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            status = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(status.st_mode):
            raise ValueError(f"Worker path component must not be a symlink: {current}")


def _no_follow_flag() -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise NotImplementedError("Worker packet I/O requires POSIX O_NOFOLLOW.")
    return no_follow
