"""Runner argument-boundary regression; subprocess/TRX transport is fixture-only."""

import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from evidence import summarize
from runner import execute
from test_orchestration import BASE, CONTROL, PROJECT, SELECT, trx


class RunnerBoundaryTests(unittest.TestCase):
    def test_existing_results_directory_is_sdk_owned_in_single_and_project_modes(self):
        for mode in ("single", "project"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix="runner boundary ") as temporary:
                root = Path(temporary).resolve()
                config = {"target": SELECT, "iterations": 2, "mode": mode,
                          "source_sha": BASE, "trusted_sha": CONTROL}
                calls = []
                raw_output = b"fixture-only test output \xff\n"
                originals = {}

                def transport(command, **kwargs):
                    # The real execute() creates this directory before invoking
                    # dotnet. No SDK or MTP parser is simulated by this fixture.
                    iteration = len(calls) + 1
                    directory = root / "flaky-evidence" / f"iteration-{iteration}"
                    self.assertTrue(directory.is_dir())
                    self.assertIn(" ", str(directory))
                    calls.append((list(command), {
                        "cwd": kwargs["cwd"], "stderr": kwargs["stderr"],
                        "timeout": kwargs["timeout"], "check": kwargs["check"],
                    }))
                    self.assertEqual(directory / "output.log", Path(kwargs["stdout"].name))
                    kwargs["stdout"].write(raw_output)
                    original = trx("Failed" if iteration == 2 else "Passed", execution=str(iteration))
                    (directory / "original.trx").write_bytes(original)
                    originals[f"iteration-{iteration}/original.trx"] = original
                    if mode == "project":
                        other = trx(execution=f"other-{iteration}", method="DifferentTest")
                        (directory / "other.trx").write_bytes(other)
                        originals[f"iteration-{iteration}/other.trx"] = other
                    return subprocess.CompletedProcess(command, 2 if iteration == 2 else 0)

                with patch.dict(os.environ, {
                    "GITHUB_RUN_ID": "88", "GITHUB_RUN_ATTEMPT": "1",
                    "FLAKY_RUNNER_OS": "ubuntu-latest", "FLAKY_RUNNER_INDEX": "1",
                }), patch("runner.subprocess.check_output", return_value=BASE), \
                        patch("sys.stdout", new_callable=io.StringIO) as console:
                    exit_code = execute(root, config, run=transport)

                self.assertEqual(2, exit_code)
                self.assertEqual(2, len(calls))
                for iteration, (command, options) in enumerate(calls, 1):
                    directory = root / "flaky-evidence" / f"iteration-{iteration}"
                    expected_sdk = [
                        str(root / ("dotnet.cmd" if os.name == "nt" else "dotnet.sh")),
                        "test", "--project", PROJECT, "--no-restore", "--no-build",
                        "--no-launch-profile", "--results-directory", str(directory),
                    ]
                    expected_mtp = (
                        ["--timeout", "10m", "--report-trx",
                         "--filter-not-trait", "outerloop=true",
                         "--filter-trait", "quarantined=true"] if mode == "project" else
                        ["--timeout", "10m", "--report-trx",
                         "--filter-not-trait", "outerloop=true",
                         "--filter-method", "Sample.Tests.WidgetTests.WaitsForWidget"]
                    )
                    boundary = command.index("--")
                    self.assertEqual(expected_sdk, command[:boundary],
                                     "The existing results directory must be bound by the SDK before '--'")
                    self.assertEqual(expected_mtp, command[boundary + 1:])
                    self.assertEqual(expected_sdk + ["--"] + expected_mtp, command)
                    self.assertEqual({"cwd": root, "stderr": subprocess.STDOUT,
                                      "timeout": 720, "check": False}, options)

                output = root / "flaky-evidence"
                files = {path.relative_to(output).as_posix(): path.read_bytes()
                         for path in output.rglob("*") if path.is_file()}
                self.assertEqual({
                    "run_id": 88, "run_attempt": 1, "sha": BASE, "trusted_sha": CONTROL,
                    "os": "ubuntu-latest", "index": 1, "iterations": 2, "project": PROJECT,
                    "method": "Sample.Tests.WidgetTests.WaitsForWidget", "mode": mode,
                }, json.loads(files["receipt.json"]))
                for iteration in (1, 2):
                    self.assertEqual(raw_output, files[f"iteration-{iteration}/output.log"])
                    self.assertEqual({"exit_code": 2 if iteration == 2 else 0},
                                     json.loads(files[f"iteration-{iteration}/result.json"]))
                for name, original in originals.items():
                    self.assertEqual(original, files[name])
                self.assertEqual(
                    {"receipt.json", *originals,
                     "iteration-1/output.log", "iteration-1/result.json",
                     "iteration-2/output.log", "iteration-2/result.json"},
                    set(files))
                self.assertEqual({
                    "passed": 1, "failed": 1, "actual_tests": 4 if mode == "project" else 2,
                    "runner_iterations": 2, "errors": ["Missing widget"],
                    "cases": [{"os": "ubuntu-latest", "name": "WaitsForWidget",
                               "test_id": "test", "passed": 1, "failed": 1}],
                }, summarize({("ubuntu-latest", 1): files}, {
                    "id": 88, "sha": CONTROL, "source_sha": BASE,
                    "runners": 1, "iterations": 2, "mode": mode, "phase": "reproduce",
                }, SELECT))
                frames = [
                    json.loads(line.removeprefix("flaky-test-iteration "))
                    for line in console.getvalue().splitlines()
                ]
                self.assertEqual([
                    {"iteration": 1, "source_sha": BASE, "mode": mode,
                     "results": str(root / "flaky-evidence/iteration-1"),
                     "target": "Sample.Tests.WidgetTests.WaitsForWidget"},
                    {"iteration": 1, "exit_code": 0},
                    {"iteration": 2, "source_sha": BASE, "mode": mode,
                     "results": str(root / "flaky-evidence/iteration-2"),
                     "target": "Sample.Tests.WidgetTests.WaitsForWidget"},
                    {"iteration": 2, "exit_code": 2},
                ], frames)


if __name__ == "__main__":
    unittest.main()
