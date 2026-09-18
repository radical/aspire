from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from ci_shepherd.workflow_loop.lifetime import (
    acquire_lifetime_lock,
    is_lifetime_active,
)
from ci_shepherd.workflow_loop.models import judgment_request_to_json
from ci_shepherd.workflow_loop.models import WorkState
from ci_shepherd.workflow_loop.worker import (
    JudgmentWorkerLauncher,
    WorkerObservationStatus,
    WorkerPacketPaths,
)
from ci_shepherd.workflow_loop.worker_process import (
    _judgment_text_from_output,
    build_copilot_argv,
    run_worker,
)
from test_workflow_loop_worker import (
    LATER,
    NOW,
    _RecordingStore,
    _request,
    _reservation,
    _worker,
)


class WorkerProcessTests(unittest.TestCase):
    def test_leaf_result_envelope_retains_typed_policy_on_reparse(self) -> None:
        from ci_shepherd.workflow_loop.models import parse_judgment_result
        from ci_shepherd.workflow_loop.worker_process import _judgment_result_document
        from test_workflow_loop_models import WorkflowLoopModelTests

        fixtures = WorkflowLoopModelTests()
        request = fixtures.leaf_request()
        for classification in ("suspected_flake", "external_infra"):
            with self.subTest(classification=classification):
                result = parse_judgment_result(
                    fixtures.leaf_result(classification=classification), request,
                )
                document = _judgment_result_document(result)
                self.assertEqual(classification, document["classification"])
                self.assertEqual(result.recommended_response.value, document["recommendedResponse"])
                self.assertEqual(
                    result, parse_judgment_result(json.dumps(document), request)
                )

    def test_copilot_argv_exposes_only_view_with_trusted_runtime_options(self) -> None:
        with TemporaryDirectory() as scratch:
            paths = WorkerPacketPaths.create(Path(scratch) / "state", "worker-1")
            request = _request(paths)

            argv = build_copilot_argv(
                request,
                worker_directory=paths.worker_directory,
                usage_path=paths.usage,
                model="trusted-model",
                reasoning_effort="high",
            )

            self.assertEqual(
                [
                    "copilot",
                    "--no-auto-update",
                    "--model",
                    "trusted-model",
                    "--reasoning-effort",
                    "high",
                    "--no-custom-instructions",
                    "--disable-builtin-mcps",
                    "--available-tools=view",
                    "--allow-all-tools",
                    "--silent",
                    "--output-format",
                    "json",
                    "--session-id",
                    "session-1",
                    "--usage-output-file",
                    str(paths.usage),
                    "-C",
                    str(paths.worker_directory),
                    "--prompt",
                    request.prompt,
                ],
                argv,
            )
            self.assertNotIn("shell", " ".join(argv).casefold())
            self.assertNotIn("--no-ask-user", argv)
            self.assertNotIn("--no-color", argv)

    def test_jsonl_output_extracts_one_unwrapped_final_message(self) -> None:
        content = (
            '{"schemaVersion":1,"itemId":7,"decision":'
            '"defer_ordinary_test"}'
        )
        output = "\n".join(
            (
                json.dumps({"type": "session.start", "data": {}}),
                json.dumps({
                    "type": "assistant.message",
                    "data": {
                        "phase": "final_answer",
                        "content": content,
                    },
                }),
                json.dumps({"type": "result"}),
            )
        )

        self.assertEqual(content, _judgment_text_from_output(output))
        with self.assertRaisesRegex(ValueError, "JSONL"):
            _judgment_text_from_output(content)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            _judgment_text_from_output(
                output
                + "\n"
                + json.dumps({
                    "type": "assistant.message",
                    "data": {
                        "phase": "final_answer",
                        "content": content,
                    },
                })
            )

    def test_wrapper_publishes_validated_terminal_envelope(self) -> None:
        with TemporaryDirectory() as scratch:
            paths = WorkerPacketPaths.create(Path(scratch) / "state", "worker-1")
            request = _request(paths)
            paths.request.write_text(
                judgment_request_to_json(request),
                encoding="utf-8",
            )
            paths.detail.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "workerId": request.worker_id,
                        "requestPath": str(paths.request),
                        "resultPath": str(paths.result),
                        "stdoutPath": str(paths.stdout),
                        "stderrPath": str(paths.stderr),
                        "usagePath": str(paths.usage),
                        "model": "trusted-model",
                        "reasoningEffort": "high",
                    }
                ),
                encoding="utf-8",
            )
            fake_copilot = paths.worker_directory / "fake-copilot"
            fake_copilot.write_text(
                f"""#!{sys.executable}
import json
import os
from pathlib import Path
import sys

usage_path = Path(sys.argv[sys.argv.index("--usage-output-file") + 1])
usage_path.write_text(json.dumps({{
    "requests": 1,
    "copilotHome": os.environ["COPILOT_HOME"]
}}), encoding="utf-8")
judgment = json.dumps({{
    "schemaVersion": 1,
    "itemId": 7,
    "episode": 2,
    "evidenceFingerprint": "fnv1a64:0123456789abcdef",
    "decision": "follow_up",
    "summary": "The same compiler failure remains in the failed job.",
    "evidenceIds": ["run:101", "job:101:900", "log:900"],
    "inScopeJobIds": [900],
    "copilotRequest": "Fix the compiler failure and add regression coverage."
}}, separators=(",", ":"))
print(json.dumps({{
    "type": "assistant.message",
    "data": {{"phase": "final_answer", "content": judgment}}
}}, separators=(",", ":")))
""",
                encoding="utf-8",
            )
            fake_copilot.chmod(0o700)
            lock = acquire_lifetime_lock(paths.lifetime_lock)
            descriptor = lock.fileno()
            with (
                paths.stdout.open("wb") as stdout,
                paths.stderr.open("wb") as stderr,
            ):
                environment = dict(os.environ)
                environment["PYTHONPATH"] = os.pathsep.join(
                    str(Path(entry).resolve())
                    for entry in environment["PYTHONPATH"].split(os.pathsep)
                )
                wrapper = subprocess.Popen(
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
                        str(descriptor),
                        "--model",
                        "trusted-model",
                        "--reasoning-effort",
                        "high",
                        "--copilot-executable",
                        str(fake_copilot),
                    ],
                    cwd=paths.worker_directory,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    close_fds=True,
                    pass_fds=(descriptor,),
                    start_new_session=True,
                    env=environment,
                )
            lock.close()
            self.assertEqual(0, wrapper.wait(timeout=5))
            self.assertFalse(is_lifetime_active(paths.lifetime_lock))

            envelope = json.loads(paths.result.read_text(encoding="utf-8"))
            self.assertEqual("succeeded", envelope["status"])
            self.assertEqual(0, envelope["exitCode"])
            self.assertEqual("follow_up", envelope["judgmentResult"]["decision"])
            self.assertEqual(
                {
                    "issueNumber": 17,
                    "taskId": "task-owned-123",
                    "pullRequestNumber": 23,
                    "pullRequestHeadSha": "fedcba9876543210",
                    "pullRequestHeadRef": "copilot/fix-build",
                    "pullRequestBaseRef": "main",
                    "pullRequestObservedAt": LATER,
                },
                envelope["requestIdentity"],
            )
            copilot_home = paths.worker_directory / "copilot-home"
            self.assertEqual(0o700, copilot_home.stat().st_mode & 0o777)
            self.assertEqual(
                {
                    "requests": 1,
                    "copilotHome": str(copilot_home),
                },
                json.loads(paths.usage.read_text()),
            )

    def test_wrapper_surfaces_nonzero_malformed_and_foreign_results(self) -> None:
        valid = {
            "schemaVersion": 1,
            "itemId": 7,
            "episode": 2,
            "evidenceFingerprint": "fnv1a64:0123456789abcdef",
            "decision": "follow_up",
            "summary": "The compiler failure remains.",
            "evidenceIds": ["run:101", "job:101:900", "log:900"],
            "inScopeJobIds": [900],
            "copilotRequest": "Fix the compiler failure.",
        }

        def jsonl(content: str) -> str:
            return json.dumps({
                "type": "assistant.message",
                "data": {
                    "phase": "final_answer",
                    "content": content,
                },
            })

        cases = (
            (
                "nonzero",
                jsonl(json.dumps(valid)),
                9,
                "failed",
                "status 9",
                WorkState.FAILED,
            ),
            (
                "malformed",
                "```json\n{}\n```",
                0,
                "invalid",
                "invalid",
                WorkState.INVALID,
            ),
            (
                "foreign",
                jsonl(json.dumps({**valid, "itemId": 99})),
                0,
                "invalid",
                "does not match",
                WorkState.INVALID,
            ),
        )
        for (
            name,
            output,
            exit_code,
            status,
            error_fragment,
            completion_state,
        ) in cases:
            with self.subTest(name=name), TemporaryDirectory() as scratch:
                paths = WorkerPacketPaths.create(
                    Path(scratch) / "state",
                    "worker-1",
                )
                request = _request(paths)
                paths.request.write_text(
                    judgment_request_to_json(request),
                    encoding="utf-8",
                )
                paths.detail.write_text(
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "workerId": request.worker_id,
                            "requestPath": str(paths.request),
                            "resultPath": str(paths.result),
                            "stdoutPath": str(paths.stdout),
                            "stderrPath": str(paths.stderr),
                            "usagePath": str(paths.usage),
                            "model": "trusted-model",
                            "reasoningEffort": "high",
                        }
                    ),
                    encoding="utf-8",
                )
                fake_copilot = paths.worker_directory / "fake-copilot"
                fake_copilot.write_text(
                    f"""#!{sys.executable}
import sys
sys.stdout.write({output!r})
raise SystemExit({exit_code})
""",
                    encoding="utf-8",
                )
                fake_copilot.chmod(0o700)
                lock = acquire_lifetime_lock(paths.lifetime_lock)
                descriptor = lock.fileno()
                environment = dict(os.environ)
                environment["PYTHONPATH"] = os.pathsep.join(
                    str(Path(entry).resolve())
                    for entry in environment["PYTHONPATH"].split(os.pathsep)
                )
                with (
                    paths.stdout.open("wb") as stdout,
                    paths.stderr.open("wb") as stderr,
                ):
                    wrapper = subprocess.Popen(
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
                            str(descriptor),
                            "--model",
                            "trusted-model",
                            "--reasoning-effort",
                            "high",
                            "--copilot-executable",
                            str(fake_copilot),
                        ],
                        cwd=paths.worker_directory,
                        stdin=subprocess.DEVNULL,
                        stdout=stdout,
                        stderr=stderr,
                        close_fds=True,
                        pass_fds=(descriptor,),
                        start_new_session=True,
                        env=environment,
                    )
                lock.close()
                self.assertEqual(0, wrapper.wait(timeout=5))
                envelope = json.loads(paths.result.read_text(encoding="utf-8"))
                self.assertEqual(status, envelope["status"])
                self.assertIsNone(envelope["judgmentResult"])
                self.assertIn(error_fragment, envelope["error"])

                reservation = _reservation(paths)
                worker = replace(
                    _worker(reservation),
                    state=WorkState.RUNNING,
                    pid=wrapper.pid,
                    launch_attempted_at=NOW,
                    launched_at=NOW,
                )
                store = _RecordingStore(worker)
                launcher = JudgmentWorkerLauncher(
                    paths.worker_directory.parents[1],
                    store=store,
                    clock=lambda: LATER,
                    model="trusted-model",
                    reasoning_effort="high",
                )
                observed = launcher.observe(worker)
                self.assertEqual(
                    WorkerObservationStatus.ATTENTION_REQUIRED,
                    observed.status,
                )
                self.assertEqual(completion_state, observed.completion.state)
                self.assertIn(error_fragment, observed.error)

    def test_wrapper_death_does_not_release_live_copilot_lock(self) -> None:
        with TemporaryDirectory() as scratch:
            paths = WorkerPacketPaths.create(Path(scratch) / "state", "worker-1")
            request = _request(paths)
            paths.request.write_text(
                judgment_request_to_json(request),
                encoding="utf-8",
            )
            paths.detail.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "workerId": request.worker_id,
                        "requestPath": str(paths.request),
                        "resultPath": str(paths.result),
                        "stdoutPath": str(paths.stdout),
                        "stderrPath": str(paths.stderr),
                        "usagePath": str(paths.usage),
                        "model": "trusted-model",
                        "reasoningEffort": "high",
                    }
                ),
                encoding="utf-8",
            )
            fake_copilot = paths.worker_directory / "fake-copilot"
            fake_copilot.write_text(
                f"""#!{sys.executable}
from pathlib import Path
import os
import time

Path("child.pid").write_text(str(os.getpid()), encoding="utf-8")
Path("child.ready").write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 10
while not Path("release").exists():
    if time.monotonic() >= deadline:
        raise SystemExit(2)
    time.sleep(0.01)
""",
                encoding="utf-8",
            )
            fake_copilot.chmod(0o700)
            lock = acquire_lifetime_lock(paths.lifetime_lock)
            descriptor = lock.fileno()
            environment = dict(os.environ)
            environment["PYTHONPATH"] = os.pathsep.join(
                str(Path(entry).resolve())
                for entry in environment["PYTHONPATH"].split(os.pathsep)
            )
            with (
                paths.stdout.open("wb") as stdout,
                paths.stderr.open("wb") as stderr,
            ):
                wrapper = subprocess.Popen(
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
                        str(descriptor),
                        "--model",
                        "trusted-model",
                        "--reasoning-effort",
                        "high",
                        "--copilot-executable",
                        str(fake_copilot),
                    ],
                    cwd=paths.worker_directory,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    close_fds=True,
                    pass_fds=(descriptor,),
                    start_new_session=True,
                    env=environment,
                )
            lock.close()
            child_pid: int | None = None
            try:
                deadline = time.monotonic() + 5
                while not (paths.worker_directory / "child.ready").exists():
                    if wrapper.poll() is not None:
                        self.fail(
                            paths.stderr.read_text(encoding="utf-8")
                            or "wrapper exited before child became ready"
                        )
                    if time.monotonic() >= deadline:
                        self.fail("Timed out waiting for fake Copilot.")
                    time.sleep(0.01)
                child_pid = int(
                    (paths.worker_directory / "child.pid").read_text(
                        encoding="utf-8"
                    )
                )

                wrapper.kill()
                wrapper.wait(timeout=5)
                self.assertTrue(is_lifetime_active(paths.lifetime_lock))
                self.assertFalse(paths.result.exists())

                (paths.worker_directory / "release").touch()
                deadline = time.monotonic() + 5
                while is_lifetime_active(paths.lifetime_lock):
                    if time.monotonic() >= deadline:
                        self.fail("Child retained the lifetime lock after exit.")
                    time.sleep(0.01)
                while True:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        break
                    if time.monotonic() >= deadline:
                        self.fail("Fake Copilot process did not exit.")
                    time.sleep(0.01)
            finally:
                if wrapper.poll() is None:
                    wrapper.kill()
                    wrapper.wait(timeout=5)
                if child_pid is not None:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        pass
                    else:
                        os.kill(child_pid, 9)

    def test_stdout_fsync_failure_is_a_terminal_error_not_success(self) -> None:
        with TemporaryDirectory() as scratch:
            state_directory = Path(scratch) / "state"
            paths = WorkerPacketPaths.create(state_directory, "worker-1")
            request = _request(paths)
            reservation = _reservation(paths)
            launcher = JudgmentWorkerLauncher(
                state_directory,
                store=_RecordingStore(_worker(reservation)),
                clock=lambda: NOW,
                model="trusted-model",
                reasoning_effort="high",
            )
            self.assertEqual(
                "prepared",
                launcher.prepare(reservation, request).status.value,
            )
            paths.stdout.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "itemId": request.item_id,
                        "episode": request.episode,
                        "evidenceFingerprint": request.evidence_fingerprint,
                        "decision": "follow_up",
                        "summary": "This must not be accepted after fsync fails.",
                        "evidenceIds": list(request.evidence_ids),
                        "inScopeJobIds": [900],
                        "copilotRequest": "Fix the compiler failure.",
                    }
                ),
                encoding="utf-8",
            )
            lock = acquire_lifetime_lock(paths.lifetime_lock)
            try:
                with patch(
                    "ci_shepherd.workflow_loop.worker_process."
                    "_sync_standard_streams",
                    side_effect=OSError("simulated fsync failure"),
                ):
                    self.assertEqual(
                        0,
                        run_worker(
                            request_path=paths.request,
                            result_path=paths.result,
                            detail_path=paths.detail,
                            lifetime_fd=lock.fileno(),
                            model="trusted-model",
                            reasoning_effort="high",
                            copilot_executable="/usr/bin/true",
                        ),
                    )
            finally:
                lock.close()

            envelope = json.loads(paths.result.read_text(encoding="utf-8"))
            self.assertEqual("failed", envelope["status"])
            self.assertIsNone(envelope["judgmentResult"])
            self.assertIn("durably sync", envelope["error"])


if __name__ == "__main__":
    unittest.main()
