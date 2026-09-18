from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest
from unittest.mock import patch

from ci_shepherd.workflow_loop.cli import _build_manager, main
from ci_shepherd.workflow_loop.manager import EffectMode, PassResult
from ci_shepherd.workflow_loop.state import WorkflowLoopStore
from ci_shepherd.workflow_loop.shadow import prepare_shadow


class _Manager:
    def __init__(self) -> None:
        self.modes = []

    def run_pass(self, *, mode: EffectMode) -> PassResult:
        self.modes.append(mode)
        return PassResult(
            pass_id=f"pass-{len(self.modes)}",
            owner_id="owner",
            started_at="2026-09-17T20:00:00Z",
            completed_at="2026-09-17T20:00:01Z",
            duration_ms=1000,
            github_request_count=3,
            discovered_items=1,
            progressed_items=1,
            launched_workers=1,
            confirmed_assignments=0,
            errors=(),
        )


class _SignalApi:
    SIGINT = 2
    SIGTERM = 15

    def __init__(self) -> None:
        self.handlers = {
            self.SIGINT: "previous-int",
            self.SIGTERM: "previous-term",
        }

    def getsignal(self, signum):
        return self.handlers[signum]

    def signal(self, signum, handler):
        previous = self.handlers[signum]
        self.handlers[signum] = handler
        return previous

    def send(self, signum) -> None:
        self.handlers[signum](signum, None)


class WorkflowLoopCliTests(unittest.TestCase):
    def test_read_only_reuses_private_shadow_without_changing_canonical(self) -> None:
        with TemporaryDirectory() as scratch:
            canonical = Path(scratch) / "canonical"
            shadow = Path(scratch) / "shadow"
            store = WorkflowLoopStore(
                canonical, repository="microsoft/aspire", branch="main",
            )
            store.initialize()
            database = canonical / "workflow-loop.sqlite3"

            def logical_state():
                with closing(sqlite3.connect(database)) as connection:
                    return tuple(connection.iterdump())

            before = logical_state()
            observed_paths = []

            def factory(**kwargs):
                path = kwargs["state_directory"]
                observed_paths.append(path)
                shadow_store = WorkflowLoopStore(
                    path, repository="microsoft/aspire", branch="main",
                )
                shadow_store.initialize()
                shadow_store.start_pass(
                    f"shadow-{len(observed_paths)}", "2026-09-18T00:00:00Z",
                )
                return _Manager()

            arguments = [
                "pass", "--repository", "microsoft/aspire",
                "--state-dir", str(canonical),
                "--shadow-state-dir", str(shadow),
            ]
            output = []
            for _ in range(2):
                self.assertEqual(
                    0, main(arguments, manager_factory=factory, output=output.append),
                )
                self.assertEqual(before, logical_state())
            self.assertEqual([shadow, shadow], observed_paths)
            with closing(sqlite3.connect(shadow / "workflow-loop.sqlite3")) as connection:
                self.assertEqual(2, connection.execute(
                    "SELECT COUNT(*) FROM passes"
                ).fetchone()[0])
            self.assertTrue(any(str(canonical) in line for line in output))
            self.assertTrue(any(str(shadow) in line for line in output))

    def test_removed_local_judgment_flag_is_rejected(self) -> None:
        with self.assertRaises(SystemExit) as error:
            main([
                "pass", "--repository", "microsoft/aspire",
                "--state-dir", "/unused", "--local-judgment",
            ])
        self.assertEqual(2, error.exception.code)

    def test_live_uses_canonical_and_rejects_shadow_state_or_arguments(self) -> None:
        with TemporaryDirectory() as scratch:
            canonical = Path(scratch) / "canonical"
            shadow = Path(scratch) / "shadow"
            observed = []

            def factory(**kwargs):
                observed.append(kwargs["state_directory"])
                return _Manager()

            common = ["pass", "--repository", "microsoft/aspire"]
            self.assertEqual(0, main(
                common + ["--state-dir", str(canonical), "--shadow-state-dir", str(shadow)],
                manager_factory=factory, output=lambda line: None,
            ))
            live = ["--live", "--allow-write-repository", "microsoft/aspire"]
            self.assertEqual(0, main(
                common + ["--state-dir", str(canonical)] + live,
                manager_factory=factory, output=lambda line: None,
            ))
            self.assertEqual([shadow, canonical], observed)
            for arguments in (
                ["--state-dir", str(shadow)],
                ["--state-dir", str(canonical), "--shadow-state-dir", str(shadow)],
            ):
                with self.subTest(arguments=arguments):
                    errors = []
                    self.assertEqual(2, main(
                        common + arguments + live,
                        manager_factory=factory, error_output=errors.append,
                    ))
                    self.assertTrue(errors)
            self.assertEqual([shadow, canonical], observed)

    def test_read_only_builds_no_github_actor(self) -> None:
        with TemporaryDirectory() as scratch:
            state = Path(scratch) / "shadow"
            with patch("ci_shepherd.workflow_loop.cli.GitHubActorClient") as actor:
                manager = _build_manager(
                    state_directory=state,
                    repository="microsoft/aspire",
                    branch="main",
                    workflow_ids=(),
                    model="gpt-5.6-sol",
                    reasoning_effort="medium",
                    mode=EffectMode.READ_ONLY,
                    write_repositories=(),
                )
                actor.assert_not_called()
                self.assertIsNotNone(manager._writer)
                self.assertFalse((state / "github-writes.jsonl").exists())

    def test_watch_snapshots_once_without_bootstrapping_canonical(self) -> None:
        with TemporaryDirectory() as scratch:
            canonical = Path(scratch) / "canonical"
            shadow = Path(scratch) / "shadow"
            signals = _SignalApi()
            manager = _Manager()
            factory_paths = []

            def factory(**kwargs):
                factory_paths.append(kwargs["state_directory"])
                return manager

            def sleep(_seconds):
                if len(manager.modes) == 2:
                    signals.send(signals.SIGTERM)

            with patch(
                "ci_shepherd.workflow_loop.cli.prepare_shadow",
                wraps=prepare_shadow,
            ) as snapshot:
                self.assertEqual(0, main(
                    [
                        "watch", "--repository", "microsoft/aspire",
                        "--state-dir", str(canonical),
                        "--shadow-state-dir", str(shadow),
                    ],
                    manager_factory=factory,
                    sleep=sleep,
                    monotonic=iter((0.0, 0.0, 300.0, 300.0)).__next__,
                    signal_api=signals,
                    output=lambda line: None,
                ))
                snapshot.assert_called_once()
            self.assertEqual([shadow], factory_paths)
            self.assertEqual([EffectMode.READ_ONLY] * 2, manager.modes)
            self.assertFalse(canonical.exists())

    def test_one_shot_degraded_pass_is_visible_and_nonzero(self) -> None:
        class DegradedManager(_Manager):
            def run_pass(self, *, mode):
                result = super().run_pass(mode=mode)
                return replace(
                    result,
                    errors=("repository:repository-unavailable:503",),
                )

        with TemporaryDirectory() as scratch:
            output: list[str] = []
            result = main(
                [
                    "pass",
                    "--repository",
                    "radical/aspire",
                    "--state-dir",
                    str(Path(scratch) / "state"),
                ],
                manager_factory=lambda **kwargs: DegradedManager(),
                output=output.append,
            )

        self.assertEqual(1, result)
        self.assertIn("status=degraded", output[2])
        self.assertEqual(
            "  error: repository:repository-unavailable:503",
            output[3],
        )

    def test_pass_defaults_to_read_only_and_prints_metrics(self) -> None:
        with TemporaryDirectory() as scratch:
            manager = _Manager()
            output: list[str] = []

            result = main(
                [
                    "pass",
                    "--repository",
                    "radical/aspire",
                    "--branch",
                    "main",
                    "--state-dir",
                    str(Path(scratch) / "state"),
                ],
                manager_factory=lambda **kwargs: manager,
                output=output.append,
            )

            self.assertEqual(0, result)
            self.assertEqual([EffectMode.READ_ONLY], manager.modes)
            self.assertEqual(
                [
                    "pass-1 duration=1.000s github_requests=3 "
                    "assignments=0 status=ok"
                ],
                output[2:],
            )

    def test_live_requires_exact_repository_allowlist_before_factory(self) -> None:
        called = False

        def factory(**kwargs):
            nonlocal called
            called = True
            return _Manager()

        with TemporaryDirectory() as scratch:
            errors: list[str] = []
            result = main(
                [
                    "pass",
                    "--repository",
                    "radical/aspire",
                    "--state-dir",
                    str(Path(scratch) / "state"),
                    "--live",
                    "--allow-write-repository",
                    "microsoft/aspire",
                ],
                manager_factory=factory,
                error_output=errors.append,
            )

        self.assertEqual(2, result)
        self.assertFalse(called)
        self.assertIn("must exactly match", errors[0])

    def test_watch_uses_same_pass_and_stops_cleanly(self) -> None:
        with TemporaryDirectory() as scratch:
            manager = _Manager()
            sleeps: list[float] = []
            signals = _SignalApi()

            def stop_after_first(seconds: float) -> None:
                sleeps.append(seconds)
                signals.send(signals.SIGINT)

            result = main(
                [
                    "watch",
                    "--repository",
                    "radical/aspire",
                    "--state-dir",
                    str(Path(scratch) / "state"),
                    "--interval-seconds",
                    "0.01",
                ],
                manager_factory=lambda **kwargs: manager,
                sleep=stop_after_first,
                monotonic=iter((10.0, 10.0)).__next__,
                signal_api=signals,
                output=lambda value: None,
            )

            self.assertEqual(0, result)
            self.assertEqual([EffectMode.READ_ONLY], manager.modes)
            self.assertEqual([0.01], sleeps)
            self.assertEqual("previous-int", signals.handlers[signals.SIGINT])
            self.assertEqual("previous-term", signals.handlers[signals.SIGTERM])

    def test_watch_uses_start_to_start_cadence(self) -> None:
        with TemporaryDirectory() as scratch:
            manager = _Manager()
            signals = _SignalApi()
            sleeps: list[float] = []

            def stop_during_sleep(seconds: float) -> None:
                sleeps.append(seconds)
                signals.send(signals.SIGTERM)

            result = main(
                [
                    "watch",
                    "--repository",
                    "radical/aspire",
                    "--state-dir",
                    str(Path(scratch) / "state"),
                    "--interval-seconds",
                    "5",
                ],
                manager_factory=lambda **kwargs: manager,
                sleep=stop_during_sleep,
                monotonic=iter((10.0, 12.0)).__next__,
                signal_api=signals,
                output=lambda value: None,
            )

        self.assertEqual(0, result)
        self.assertEqual([3.0], sleeps)
        self.assertEqual([EffectMode.READ_ONLY], manager.modes)

    def test_watch_reports_degraded_pass_and_retries_later(self) -> None:
        class RecoveringManager(_Manager):
            def run_pass(self, *, mode):
                result = super().run_pass(mode=mode)
                if len(self.modes) == 1:
                    return replace(
                        result,
                        errors=("repository:repository-unavailable:503",),
                    )
                return result

        with TemporaryDirectory() as scratch:
            manager = RecoveringManager()
            signals = _SignalApi()
            output: list[str] = []
            sleeps = 0

            def continue_then_stop(_seconds: float) -> None:
                nonlocal sleeps
                sleeps += 1
                if sleeps == 2:
                    signals.send(signals.SIGTERM)

            result = main(
                [
                    "watch",
                    "--repository",
                    "radical/aspire",
                    "--state-dir",
                    str(Path(scratch) / "state"),
                    "--interval-seconds",
                    "5",
                ],
                manager_factory=lambda **kwargs: manager,
                sleep=continue_then_stop,
                monotonic=iter((0.0, 0.0, 5.0, 5.0)).__next__,
                signal_api=signals,
                output=output.append,
            )

        self.assertEqual(0, result)
        self.assertEqual(2, len(manager.modes))
        self.assertIn("status=degraded", output[2])
        self.assertIn("status=ok", output[4])

    def test_keyboard_interrupt_from_run_pass_is_not_swallowed(self) -> None:
        class InterruptedManager:
            def run_pass(self, *, mode):
                raise KeyboardInterrupt

        with TemporaryDirectory() as scratch:
            with self.assertRaises(KeyboardInterrupt):
                main(
                    [
                        "watch",
                        "--repository",
                        "radical/aspire",
                        "--state-dir",
                        str(Path(scratch) / "state"),
                    ],
                    manager_factory=lambda **kwargs: InterruptedManager(),
                    signal_api=_SignalApi(),
                    output=lambda value: None,
                )

    def test_signal_during_pass_stops_after_pass_without_sleep(self) -> None:
        signals = _SignalApi()

        class SignaledManager(_Manager):
            def run_pass(self, *, mode):
                result = super().run_pass(mode=mode)
                signals.send(signals.SIGTERM)
                return result

        with TemporaryDirectory() as scratch:
            sleeps: list[float] = []
            result = main(
                [
                    "watch",
                    "--repository",
                    "radical/aspire",
                    "--state-dir",
                    str(Path(scratch) / "state"),
                ],
                manager_factory=lambda **kwargs: SignaledManager(),
                sleep=sleeps.append,
                monotonic=lambda: 1.0,
                signal_api=signals,
                output=lambda value: None,
            )

        self.assertEqual(0, result)
        self.assertEqual([], sleeps)

    def test_periodic_manager_uses_one_github_http_attempt(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            with patch(
                "ci_shepherd.workflow_loop.cli.GitHubClient"
            ) as client_type:
                manager = _build_manager(
                    state_directory=state_directory,
                    repository="radical/aspire",
                    branch="main",
                    workflow_ids=(),
                    model="gpt-5.6-sol",
                    reasoning_effort="high",
                    mode=EffectMode.READ_ONLY,
                    write_repositories=(),
                )

        self.assertEqual(1, client_type.call_args.kwargs["max_attempts"])
        observer = client_type.call_args.kwargs["request_observer"]
        with ThreadPoolExecutor(max_workers=8) as executor:
            tuple(executor.map(observer, ("/test",) * 1_000))
        self.assertEqual(1_000, manager._request_count())


if __name__ == "__main__":
    unittest.main()
