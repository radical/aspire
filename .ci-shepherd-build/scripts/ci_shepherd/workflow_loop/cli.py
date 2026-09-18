from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
import signal
import subprocess
import threading
import time

from ci_shepherd.github import GitHubClient
from ci_shepherd.github_actor import GitHubActorClient

from .manager import EffectMode, PassResult, WorkflowLoopManager
from .reader import WorkflowReader
from .report import render_status
from .state import WorkflowLoopStore
from .worker import JudgmentWorkerLauncher
from .writer import WorkflowWriter


def main(
    argv: Sequence[str] | None = None,
    *,
    manager_factory: Callable[..., WorkflowLoopManager] | None = None,
    output: Callable[[str], None] = print,
    error_output: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    signal_api=signal,
) -> int:
    arguments = _parser().parse_args(argv)
    try:
        mode = _effect_mode(arguments)
        _validate_effect_scope(arguments, mode)
    except ValueError as error:
        error_output(str(error))
        return 2

    if arguments.command == "status":
        try:
            output(
                render_status(
                    arguments.state_dir,
                    repository=arguments.repository,
                    branch=arguments.branch,
                    workflow_ids=arguments.workflow_id or None,
                    now=datetime.now(UTC),
                )
            )
        except ValueError as error:
            error_output(str(error))
            return 2
        return 0

    factory = manager_factory or _build_manager
    try:
        manager = factory(
            state_directory=arguments.state_dir,
            repository=arguments.repository,
            branch=arguments.branch,
            workflow_ids=arguments.workflow_id,
            model=arguments.model,
            reasoning_effort=arguments.reasoning_effort,
            mode=mode,
            write_repositories=tuple(arguments.allow_write_repository),
        )
    except ValueError as error:
        error_output(str(error))
        return 2

    if arguments.command == "pass":
        result = manager.run_pass(mode=mode)
        _print_pass(result, output)
        return 1 if result.errors else 0

    stopping = False
    sleeping = False

    class StopScheduling(Exception):
        pass

    def request_stop(_signum, _frame) -> None:
        nonlocal stopping
        stopping = True
        if sleeping:
            raise StopScheduling

    watched_signals = (signal_api.SIGINT, signal_api.SIGTERM)
    previous_handlers = {
        signum: signal_api.getsignal(signum)
        for signum in watched_signals
    }
    for signum in watched_signals:
        signal_api.signal(signum, request_stop)
    try:
        while not stopping:
            pass_started = monotonic()
            result = manager.run_pass(mode=mode)
            _print_pass(result, output)
            if stopping:
                break
            remaining = max(
                0.0,
                arguments.interval_seconds - (monotonic() - pass_started),
            )
            if remaining > 0:
                sleeping = True
                try:
                    sleep(remaining)
                finally:
                    sleeping = False
    except StopScheduling:
        pass
    finally:
        for signum, handler in previous_handlers.items():
            signal_api.signal(signum, handler)
    return 0


def _effect_mode(arguments: argparse.Namespace) -> EffectMode:
    if arguments.live:
        return EffectMode.LIVE
    if arguments.local_judgment:
        return EffectMode.LOCAL_JUDGMENT
    return EffectMode.READ_ONLY


def _validate_effect_scope(
    arguments: argparse.Namespace,
    mode: EffectMode,
) -> None:
    if not arguments.state_dir.is_absolute():
        raise ValueError("--state-dir must be an absolute path.")
    if any(workflow_id < 1 for workflow_id in arguments.workflow_id):
        raise ValueError("--workflow-id values must be positive integers.")
    allowed = tuple(arguments.allow_write_repository)
    if arguments.command == "status" and mode is not EffectMode.READ_ONLY:
        raise ValueError("status does not accept effect-mode flags.")
    if mode is EffectMode.LIVE:
        if allowed != (arguments.repository,):
            raise ValueError(
                "Live mode --allow-write-repository must exactly match "
                "--repository once."
            )
    elif allowed:
        raise ValueError(
            "--allow-write-repository is only valid together with --live."
        )
    if (
        arguments.command == "watch"
        and arguments.interval_seconds <= 0
    ):
        raise ValueError("--interval-seconds must be positive.")


def _build_manager(
    *,
    state_directory: Path,
    repository: str,
    branch: str,
    workflow_ids: Sequence[int],
    model: str,
    reasoning_effort: str,
    mode: EffectMode,
    write_repositories: tuple[str, ...],
) -> WorkflowLoopManager:
    store = WorkflowLoopStore(
        state_directory,
        repository=repository,
        branch=branch,
    )
    store.initialize(workflow_ids=workflow_ids or None)
    request_count = 0
    request_count_lock = threading.Lock()

    def observe_request(_endpoint: str) -> None:
        nonlocal request_count
        with request_count_lock:
            request_count += 1

    def current_request_count() -> int:
        with request_count_lock:
            return request_count

    client = GitHubClient(
        runner=subprocess.run,
        popen_factory=subprocess.Popen,
        sleep=time.sleep,
        now=lambda: datetime.now(UTC),
        max_attempts=1,
        audit_path=state_directory / "github-reads.jsonl",
        request_observer=observe_request,
    )
    reader = WorkflowReader(
        client=client,
        clock=lambda: datetime.now(UTC),
        request_count=current_request_count,
    )
    launcher = JudgmentWorkerLauncher(
        state_directory,
        store=store,
        clock=lambda: datetime.now(UTC),
        model=model,
        reasoning_effort=reasoning_effort,
    )
    writer = None
    if mode is EffectMode.LIVE:
        actor = GitHubActorClient(
            allowed_repositories=write_repositories,
            protected_workflow_repair_repositories=(
                write_repositories
                if repository.casefold() == "microsoft/aspire"
                else ()
            ),
            audit_path=state_directory / "github-writes.jsonl",
        )
        writer = WorkflowWriter(
            store=store,
            reader=reader,
            actor=actor,
            repository=repository,
            branch=branch,
            clock=lambda: datetime.now(UTC),
            active_item_limit=2,
        )
    return WorkflowLoopManager(
        state_directory=state_directory,
        repository=repository,
        branch=branch,
        store=store,
        reader=reader,
        launcher=launcher,
        writer=writer,
        clock=lambda: datetime.now(UTC),
        workflow_ids=workflow_ids or None,
        request_count=current_request_count,
    )


def _print_pass(
    result: PassResult,
    output: Callable[[str], None],
) -> None:
    status = "degraded" if result.errors else "ok"
    output(
        f"{result.pass_id} duration={result.duration_ms / 1000:.3f}s "
        f"github_requests={result.github_request_count} "
        f"assignments={result.confirmed_assignments} status={status}"
    )
    for error in result.errors:
        output(f"  error: {error}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workflow-loop")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("pass", "watch", "status"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--repository", required=True)
        subparser.add_argument("--branch", default="main")
        subparser.add_argument("--state-dir", required=True, type=Path)
        subparser.add_argument(
            "--workflow-id",
            action="append",
            type=int,
            default=[],
        )
        effects = subparser.add_mutually_exclusive_group()
        effects.add_argument("--local-judgment", action="store_true")
        effects.add_argument("--live", action="store_true")
        subparser.add_argument(
            "--allow-write-repository",
            action="append",
            default=[],
        )
        subparser.add_argument("--model", default="gpt-5.6-sol")
        subparser.add_argument("--reasoning-effort", default="high")
        if command == "watch":
            subparser.add_argument(
                "--interval-seconds",
                type=float,
                default=300.0,
            )
    return parser
