"""Behavioral tests exercise the real Bash exit boundary and private-file reader."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import unittest


ROOT = Path(__file__).parent
BASELINE = os.environ.get("GATEWAY_OBSERVATION_BASELINE") == "1"
INFO = [
    ("=== MCP Gateway Startup ===", "validation"),
    ("Reading MCP configuration from stdin...", "input_config"),
    ("Starting gateway container...", "spawn"),
    ("=== Health Check Progress ===", "health"),
    ("Reading gateway output configuration...", "read_output"),
    ("Converting gateway configuration to agent format...", "conversion"),
    ("Checking MCP server functionality...", "server_check"),
    ("Saving MCP CLI manifest...", "manifest"),
    ("Cleaning up gateway configuration file...", "config_cleanup"),
]
ERRORS = [
    ("ERROR: Gateway process stdin is not available", "stdin_unavailable"),
    ("ERROR: Failed to start gateway container", "spawn_failed"),
    ("ERROR: Gateway process exited immediately after start", "process_exited_early"),
    ("ERROR: Gateway failed to become ready", "health_failed"),
    ("ERROR: Gateway did not write output configuration", "output_missing"),
    ("ERROR: Gateway returned an error payload instead of configuration", "output_rejected"),
    ("ERROR: MCP server checks failed - no servers could be connected", "server_check_failed"),
]


class GatewayStartupObservationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ROOT / "test-fixtures" / self.id().split(".")[-1]
        self.fixture.mkdir(parents=True, exist_ok=False)
        self.private = self.fixture / "private.log"
        self.env = {
            "PATH": str(Path(sys.executable).parent) + ":" + os.defpath,
            "HOME": str(self.fixture),
        }

    def tearDown(self):
        shutil.rmtree(self.fixture)
        try:
            (ROOT / "test-fixtures").rmdir()
        except OSError:
            pass

    def execute(self, body):
        quote = shlex.quote
        hook = "" if BASELINE else (
            f"source {quote(str(ROOT / 'gateway_startup_observation.sh'))} "
            f"{quote(str(self.private))} {quote(str(ROOT / 'gateway_startup_observer.py'))}\n"
        )
        script = (
            "set -euo pipefail\n" + hook +
            f"{{\n{body}\n}} > {quote(str(self.private))} 2>&1\n"
            "provider_gateway_stage=complete\n"
        )
        result = subprocess.run(
            ["/bin/bash", "-c", script], cwd=ROOT, env=self.env, timeout=5,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(result.stderr, b"")
        return result.returncode, [json.loads(line) for line in result.stdout.splitlines()]

    def record(self, status, stage="launcher_pipeline", node="unknown",
               reason="unknown", capture="bounded_regular_file"):
        return {
            "boundary": "gateway_startup", "stage": stage,
            "last_logged_node_stage": node, "reason": reason, "status": status,
            "capture": capture, "inference_authorized": False,
        }

    def logged(self, text, status=1):
        return self.execute(
            "provider_gateway_stage=launcher_pipeline\n"
            f"printf '%s' {shlex.quote(text)}\nexit {status}"
        )

    def reader(self, status=1):
        result = subprocess.run(
            [sys.executable, str(ROOT / "gateway_startup_observer.py"),
             "--log", str(self.private), "--stage", "launcher_pipeline",
             "--status", str(status)],
            cwd=ROOT, env=self.env, timeout=5,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(result.stderr, b"")
        return result.returncode, json.loads(result.stdout)

    def test_exact_fixed_failure_markers_are_normalized(self):
        for message, reason in ERRORS:
            with self.subTest(reason=reason):
                code, records = self.logged("[error] " + message + "\n")
                self.assertEqual(code, 1)
                self.assertEqual(records, [self.record(1, reason=reason)])

    def test_progress_uses_fixed_last_stage_not_runtime_text(self):
        for message, stage in INFO:
            with self.subTest(stage=stage):
                code, records = self.logged(
                    "[info] " + message + "\nfixture-private-catalog\n", 23
                )
                self.assertEqual(code, 23)
                self.assertEqual(records, [self.record(23, node=stage)])

    def test_unknown_dynamic_error_and_private_output_are_not_echoed(self):
        code, records = self.logged(
            "[info] === Health Check Progress ===\n"
            "[error] FATAL: fixture-private-account https://fixture.invalid/auth\n"
            "fixture-private-token\n"
        )
        self.assertEqual(code, 1)
        self.assertEqual(records, [self.record(1, node="health")])
        self.assertIn(b"fixture-private-token", self.private.read_bytes())

    def test_duplicate_or_competing_errors_are_unknown(self):
        for second in (
            ERRORS[3][0], ERRORS[4][0], "FATAL: fixture-private-detail",
        ):
            with self.subTest(second=second):
                code, records = self.logged(
                    "[error] " + ERRORS[3][0] + "\n[error] " + second + "\n"
                )
                self.assertEqual(code, 1)
                self.assertEqual(records, [self.record(1)])

    def test_out_of_order_stages_are_unknown(self):
        code, records = self.logged(
            "[info] Reading gateway output configuration...\n"
            "[info] === Health Check Progress ===\n"
            "[error] " + ERRORS[3][0] + "\n"
        )
        self.assertEqual(code, 1)
        self.assertEqual(records, [self.record(1)])

    def test_prefix_and_substring_are_not_matches(self):
        code, records = self.logged(
            "prefix [info] === Health Check Progress ===\n"
            "[error] prefix " + ERRORS[3][0] + " suffix\n"
        )
        self.assertEqual(code, 1)
        self.assertEqual(records, [self.record(1)])

    def test_source_exit_preserves_status_and_shell_stage(self):
        source = self.fixture / "socket.sh"
        source.write_text("echo fixture-private-socket-error >&2\nexit 7\n")
        code, records = self.execute(
            "provider_gateway_stage=socket_resolution\n"
            "source " + shlex.quote(str(source))
        )
        self.assertEqual(code, 7)
        self.assertEqual(records, [self.record(7, stage="socket_resolution")])

    def test_nounset_config_expansion_stays_failed_and_private(self):
        code, records = self.execute(
            'provider_gateway_stage=launcher_pipeline\nprintf "%s" "$UNSET_FIXTURE_VALUE"'
        )
        self.assertEqual(code, 1)
        self.assertEqual(records, [self.record(1)])
        self.assertIn(b"unbound variable", self.private.read_bytes())

    def test_pipeline_failure_cannot_be_masked_by_right_side_zero(self):
        code, records = self.execute(
            "provider_gateway_stage=launcher_pipeline\n(exit 17) | cat"
        )
        self.assertEqual(code, 17)
        self.assertEqual(records, [self.record(17)])

    def test_zero_exit_is_only_boundary_status_not_readiness_or_inference(self):
        code, records = self.execute("echo fixture-private-value")
        self.assertEqual(code, 0)
        self.assertEqual(records, [self.record(0, stage="complete", reason="exit_zero")])

    def test_early_zero_without_completion_marker_is_failed(self):
        code, records = self.execute("exit 0")
        self.assertEqual(code, 1)
        self.assertEqual(records, [self.record(1, stage="prepare")])

    def test_shell_stage_is_not_arbitrary_public_text(self):
        code, records = self.execute(
            'provider_gateway_stage="fixture-private-account https://fixture.invalid"\nexit 9'
        )
        self.assertEqual(code, 9)
        self.assertEqual(records, [self.record(9, stage="unknown")])

    def test_symlink_fifo_missing_or_oversize_capture_is_unknown(self):
        target = self.fixture / "target"
        target.write_text("[error] " + ERRORS[3][0] + "\n")
        self.private.symlink_to(target)
        code, record = self.reader()
        self.assertEqual(code, 3)
        self.assertEqual(record, self.record(1, capture="unknown"))
        self.private.unlink()
        os.mkfifo(self.private)
        code, record = self.reader()
        self.assertEqual(code, 3)
        self.assertEqual(record, self.record(1, capture="unknown"))
        self.private.unlink()
        code, record = self.reader()
        self.assertEqual(code, 3)
        self.assertEqual(record, self.record(1, capture="unknown"))
        self.private.write_bytes(b"x" * (1024 * 1024 + 1))
        code, record = self.reader()
        self.assertEqual(code, 3)
        self.assertEqual(record, self.record(1, capture="unknown"))

    def test_malformed_capture_does_not_authorize_known_reason(self):
        for data in (b"\xff", b"\x00[error] " + ERRORS[3][0].encode()):
            with self.subTest(data=data[:1]):
                self.private.write_bytes(data)
                code, record = self.reader()
                self.assertEqual(code, 3)
                self.assertEqual(record, self.record(1, capture="unknown"))

    def test_missing_reporter_never_converts_failure_to_success(self):
        reporter = ROOT / "gateway_startup_observer.py"
        code, records = self.execute(
            "provider_gateway_stage=launcher_pipeline\n"
            # Override only in this offline fixture, never in the workflow.
            f"provider_gateway_reporter={shlex.quote(str(reporter) + '.missing')}\nexit 7"
        )
        self.assertEqual(code, 7)
        self.assertEqual(records, [self.record(7, capture="unknown")])

    def test_missing_reporter_after_zero_startup_fails_observability(self):
        code, records = self.execute(
            "provider_gateway_reporter=fixture-missing-reporter\n"
            "echo fixture-private-value"
        )
        self.assertEqual(code, 1)
        self.assertEqual(records, [
            self.record(0, stage="complete", capture="unknown"),
        ])

    def test_missing_capture_after_zero_startup_is_not_success_fallback(self):
        code, records = self.execute(
            "rm " + shlex.quote(str(self.private))
        )
        self.assertEqual(code, 1)
        self.assertEqual(records, [
            self.record(0, stage="complete", capture="unknown"),
        ])


if __name__ == "__main__":
    unittest.main()
