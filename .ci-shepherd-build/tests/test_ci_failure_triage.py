from __future__ import annotations

import copy
import unittest
from datetime import timedelta

from ci_shepherd.ci_failure_triage import (
    TRIAGE_RULE_VERSION,
    attach_ci_failure_triage,
    build_ci_failure_triage,
    validate_prepared_ci_failure_triage,
)
from ci_shepherd.lifecycle import prepare_assessment
from ci_shepherd.timeutils import parse_aware_iso8601
from test_observations import (
    evidence,
    fact,
    issue_payload,
    results_payload,
    run_payload,
    snapshot,
)
from test_workflow_health import add_execution, workflow_snapshot


class CiFailureTriageTests(unittest.TestCase):
    def test_preview_preserves_setup_context_already_inside_prefix(self) -> None:
        text = (
            "Run dotnet restore\n"
            + "Preparing dependency inputs.\n" * 95
            + "error: downloading https://nuget.example/Foo failed: connection reset by peer\n"
            + "Cleaning up dependency outputs.\n" * 90
        )
        prepared = self._prepared(text)
        case = build_ci_failure_triage(prepared)["assessments"][0]
        self.assertEqual("setup", case["observed"]["phase"])
        self.assertEqual("verified", case["family"]["status"])
        log = next(record["payload"] for record in prepared["issues"][0]["evidenceBundle"]
                   if record["kind"] == "workflow-log")
        self.assertEqual(text[:4000], log["excerpt"])

    def test_bounded_log_preview_retains_failure_after_job_startup(self) -> None:
        startup = "2026-08-19T15:00:00Z Preparing repository checkout and runner environment.\n" * 100
        for diagnostic, phase in (
            ("src/File.cs(1,1): error CS1525: Invalid expression term ';'", "build"),
            ("Failed Namespace.Type.Test [42 ms]\nAssert.Equal() Failure: Expected 1 Actual 2", "test"),
        ):
            with self.subTest(phase=phase):
                prepared = self._prepared(startup + diagnostic)
                case = build_ci_failure_triage(prepared)["assessments"][0]
                self.assertEqual(phase, case["observed"]["phase"])
                self.assertEqual("verified", case["family"]["status"])
                log = next(record["payload"] for record in prepared["issues"][0]["evidenceBundle"]
                           if record["kind"] == "workflow-log")
                self.assertLessEqual(len(log["excerpt"]), 4000)
                self.assertTrue(log["excerptTruncated"])
                self.assertEqual("incomplete", case["evidenceCompleteness"])
                self.assertIn(diagnostic, log["excerpt"])

    def test_setup_family_retains_the_failing_resource(self) -> None:
        families = []
        for resource in ("Foo", "Bar"):
            case = build_ci_failure_triage(self._prepared(
                "Run dotnet restore\n"
                f"error: downloading https://nuget.example/{resource} failed: connection reset by peer"
            ))["assessments"][0]
            self.assertEqual("setup", case["observed"]["phase"])
            self.assertEqual("verified", case["family"]["status"])
            families.append(case["family"]["familyId"])
        self.assertNotEqual(families[0], families[1])

    def test_noisy_timeout_options_do_not_hide_late_artifact_failure(self) -> None:
        from ci_shepherd.observations import _repair_diagnostic_lines

        noise = "".join(
            f"2026-08-19T15:00:00Z ##[command]dotnet test Project{index} --timeout {index}m\n"
            f"2026-08-19T15:00:00Z   --hangdump-timeout {index}m\n"
            f"2026-08-19T15:00:00Z Parsing option --timeout with value {index}m\n"
            f"2026-08-19T15:00:00Z \x1b[36;1m'--ignore-exit-code 8 "
            f"--hangdump-timeout {index}m --timeout {index}m'\x1b[0m\n"
            for index in range(30)
        )
        diagnostic = "##[error]Failed to CreateArtifact: Unable to make request: ETIMEDOUT"
        startup = noise + "Preparing repository checkout.\n" * 6400
        # The observed transport-limited log was 196415 characters, with its
        # actual upload failure at 193070, after many distinct option echoes.
        text = (startup[:193069] + "\n" + diagnostic).ljust(196415, "\n")
        self.assertEqual(193070, text.index(diagnostic))
        self.assertEqual(196415, len(text))
        self.assertEqual([diagnostic.removeprefix("##[error]")], _repair_diagnostic_lines(text))
        data = workflow_snapshot()
        payload = data["evidence"]["run:100:attempt:1:job:900:log"]["payload"]
        payload.update(excerpt=text, truncated=True)
        prepared = prepare_assessment(data)
        log = next(record["payload"] for record in prepared["issues"][0]["evidenceBundle"]
                   if record["kind"] == "workflow-log")
        self.assertIn(diagnostic, log["excerpt"])
        self.assertEqual(4000, len(log["excerpt"]))
        self.assertTrue(log["truncated"])
        self.assertTrue(log["excerptTruncated"])
        self.assertFalse(prepared["issues"][0]["repairEvidence"]["ready"])
        case = build_ci_failure_triage(prepared)["assessments"][0]
        self.assertEqual("incomplete", case["evidenceCompleteness"])

    def test_command_echoes_and_assertion_values_are_not_diagnostic_identity(self) -> None:
        from ci_shepherd.observations import _repair_diagnostic_lines, workflow_log_preview

        for line in (
            "Run dotnet test --timeout 5m",
            "##[command]dotnet test --timeout 5m",
            "+ dotnet test --timeout 5m",
            "dotnet test Example.csproj --timeout 5m",
            "dotnet --timeout 5m",
            '"/usr/local/bin/dotnet" test Example.csproj --timeout 5m',
            "  --timeout <time>",
            "Parsing option --timeout with value 5m",
            "timeout-minutes: 60",
            "Command: dotnet test --timeout 5m",
            "\x1b[36;1m'--ignore-exit-code 8 --hangdump-timeout 5m --timeout 5m'\x1b[0m",
            "Expected: Failed to CreateArtifact: Unable to make request: ETIMEDOUT",
            "Actual: error CS1002: unexpected response",
            "##[error]Assert.Equal() Failure: Expected HTTP 503",
        ):
            with self.subTest(line=line):
                text = "Preparing repository checkout.\n" * 150 + "2026-08-19T15:00:00Z " + line
                self.assertEqual([], _repair_diagnostic_lines(text))
                self.assertEqual(text[:4000], workflow_log_preview(text, 4000))

    def test_many_real_diagnostics_still_require_narrower_identity(self) -> None:
        from ci_shepherd.observations import _repair_diagnostic_lines

        text = "\n".join(f"error: runtime-{index} download failed" for index in range(21))
        self.assertEqual([], _repair_diagnostic_lines(text))

    def test_generic_setup_transport_error_does_not_verify_a_family(self) -> None:
        case = build_ci_failure_triage(self._prepared(
            "Run dotnet tool restore\nerror: connection reset by peer"
        ))["assessments"][0]
        self.assertEqual("setup", case["observed"]["phase"])
        self.assertIsNone(case["observed"]["signature"])
        self.assertEqual("unknown", case["family"]["status"])
        self.assertIn("resource-specific setup diagnostic", case["missingEvidence"])

    def test_log_timestamp_envelopes_do_not_change_family_identity(self) -> None:
        for diagnostic in (
            "src/File.cs(1,1): error CS1002: ; expected",
            "Run dotnet restore\nerror: downloading https://nuget.example/Foo failed: connection reset by peer",
        ):
            with self.subTest(diagnostic=diagnostic):
                families = []
                for prefix in ("", "2026-08-19T15:01:00.123Z ", "2026-08-19T15:42:00.456Z ##[error]"):
                    text = "\n".join(prefix + line for line in diagnostic.splitlines())
                    case = build_ci_failure_triage(self._prepared(text))["assessments"][0]
                    self.assertEqual("verified", case["family"]["status"])
                    families.append(case["family"]["familyId"])
                self.assertEqual([families[0]] * 3, families)

    def test_shared_normalization_preserves_annotation_only_repair_diagnostics(self) -> None:
        from ci_shepherd.observations import _repair_diagnostic_lines

        self.assertEqual(
            ["HTTP 503 downloading https://feed.example/Foo"],
            _repair_diagnostic_lines(
                "2026-08-19T15:01:00.123Z ##[error]HTTP 503 downloading https://feed.example/Foo"
            ),
        )

    def _prepared(self, excerpt: str, *, reported_test: str | None = None):
        data = workflow_snapshot()
        data["evidence"]["run:100"]["payload"]["workflowPath"] = (
            ".github/workflows/ci.yml"
        )
        data["evidence"]["run:100:attempt:1:job:900:log"]["payload"]["excerpt"] = excerpt
        if reported_test is not None:
            data["evidence"]["issue:12"]["payload"]["facts"] = [
                fact("testName", reported_test),
                fact("causeId", "browser flake"),
            ]
        return prepare_assessment(data)

    def test_observed_phase_takes_precedence_over_reported_test_claim(self) -> None:
        cases = (
            (
                "src/File.cs(1,1): error CS0618: 'OldApi' is obsolete",
                "build",
                "compiler-diagnostic",
                False,
            ),
            (
                "Run dotnet tool restore\nerror: connection reset by peer",
                "setup",
                "dependency-network",
                False,
            ),
            (
                "46 passed, 0 failed\nTest Run Aborted.\nThe operation timed out",
                "harness",
                "session-abort",
                False,
            ),
            (
                "Failed Namespace.Type.Test [42 ms]\nAssert.Equal() Failure: Expected 1 Actual 2",
                "test",
                "test-failure",
                True,
            ),
        )
        for excerpt, phase, cause, established in cases:
            with self.subTest(phase=phase):
                triage = build_ci_failure_triage(
                    self._prepared(excerpt, reported_test="Namespace.Type.Test")
                )
                assessment = triage["assessments"][0]
                self.assertEqual(phase, assessment["observed"]["phase"])
                self.assertEqual(cause, assessment["observed"]["cause"])
                self.assertIs(established, assessment["observed"]["testFailureEstablished"])
                self.assertEqual(["Namespace.Type.Test"], assessment["reportedClaims"]["testNames"])

    def test_successful_setup_and_unrelated_compiler_output_do_not_steal_test_failures(self) -> None:
        cases = (
            (
                "Run dotnet tool restore\nRestore succeeded.\n"
                "Failed Namespace.Type.Test [42 ms]\n"
                "System.Net.Http.HttpRequestException: connection reset by peer",
                "Namespace.Type.Test",
            ),
            (
                "Failed Namespace.Type.A [42 ms]\nAssert.Equal() Failure\n"
                "other/Project.cs(1): error CS0618: old API\n"
                "Failed Namespace.Type.B [42 ms]\nSystem.TimeoutException: browser",
                "Namespace.Type.B",
            ),
        )
        for excerpt, test_name in cases:
            with self.subTest(test_name=test_name):
                prepared = self._prepared(excerpt)
                document = build_ci_failure_triage(prepared)
                occurrence_id = next(
                    occurrence["occurrenceId"]
                    for occurrence in prepared["observations"]["occurrences"]
                    if occurrence.get("testName") == test_name
                )
                assessment = next(
                    case
                    for case in document["assessments"]
                    if case["occurrenceId"] == occurrence_id
                )
                self.assertEqual("test", assessment["observed"]["phase"])
                self.assertEqual("test-failure", assessment["observed"]["cause"])

    def test_family_signature_ignores_duration_and_requires_diagnostic_content(self) -> None:
        signatures = []
        for duration in (42, 43):
            assessment = build_ci_failure_triage(
                self._prepared(
                    f"Failed Namespace.Type.Test [{duration} ms]\n"
                    "Assert.Equal() Failure: Expected 1 Actual 2"
                )
            )["assessments"][0]
            signatures.append(assessment["observed"]["signature"])
        self.assertEqual(signatures[0], signatures[1])

        header_only = build_ci_failure_triage(
            self._prepared("Failed Namespace.Type.Test [42 ms]")
        )["assessments"][0]
        self.assertIsNone(header_only["observed"]["signature"])
        self.assertEqual("unknown", header_only["family"]["status"])

        data = workflow_snapshot()
        data["evidence"]["run:100:attempt:1:job:900:log"]["payload"]["excerpt"] = ""
        data["evidence"].update([
            evidence(
                "run:100:attempt:1:job:900:test-results",
                "workflow-test-results",
                results_payload(
                    12,
                    run_id=100,
                    attempt=1,
                    job_id=900,
                    tests=[{"testName": "Namespace.Type.Test", "outcome": "failed"}],
                ),
            )
        ])
        trx_only = build_ci_failure_triage(prepare_assessment(data))["assessments"][0]
        self.assertIsNone(trx_only["observed"]["signature"])
        self.assertEqual("unknown", trx_only["family"]["status"])

    def test_family_uses_each_occurrence_workflow_scope(self) -> None:
        data = workflow_snapshot()
        data["evidence"]["run:100"]["payload"]["workflowPath"] = (
            ".github/workflows/ci.yml"
        )
        add_execution(
            data,
            101,
            "2026-08-19T15:45:00Z",
            excerpt="src/File.cs(1,1): error CS0618: old API",
        )
        data["evidence"]["run:101"]["payload"].update(
            workflowId=10,
            workflowPath=".github/workflows/different.yml",
        )
        prepared = prepare_assessment(data)
        assessments = build_ci_failure_triage(prepared)["assessments"]
        workflow_by_run = {
            occurrence["occurrenceId"]: data["evidence"][
                f"run:{occurrence['runId']}"
            ]["payload"]["workflowId"]
            for occurrence in prepared["observations"]["occurrences"]
        }
        self.assertEqual(
            workflow_by_run,
            {
                case["occurrenceId"]: case["family"]["dimensions"]["workflowId"]
                for case in assessments
            },
        )

    def test_same_test_with_different_diagnostics_has_different_family(self) -> None:
        first = build_ci_failure_triage(
            self._prepared(
                "Failed Namespace.Type.Test [42 ms]\nAssert.Equal() Failure: Expected 1 Actual 2"
            )
        )
        second = build_ci_failure_triage(
            self._prepared(
                "Failed Namespace.Type.Test [42 ms]\nSystem.TimeoutException: browser did not start"
            )
        )
        self.assertNotEqual(
            first["assessments"][0]["family"]["familyId"],
            second["assessments"][0]["family"]["familyId"],
        )

    def test_later_test_diagnostic_does_not_change_earlier_test_signature(self) -> None:
        base = (
            "Failed Namespace.Type.Test [42 ms]\n"
            "Assert.Equal() Failure: Expected 1 Actual 2\n"
            "Passed Namespace.Type.Other [1 ms]"
        )
        first = build_ci_failure_triage(self._prepared(base))
        second_prepared = self._prepared(
            base
            + "\nChrome failed to start for a later test\n"
            + "Failed Namespace.Type.Later [3 ms]"
        )
        second = build_ci_failure_triage(second_prepared)
        earlier_occurrence_id = next(
            occurrence["occurrenceId"]
            for occurrence in second_prepared["observations"]["occurrences"]
            if occurrence["testName"] == "Namespace.Type.Test"
        )
        earlier_assessment = next(
            assessment
            for assessment in second["assessments"]
            if assessment["occurrenceId"] == earlier_occurrence_id
        )
        self.assertEqual(
            first["assessments"][0]["observed"]["signature"],
            earlier_assessment["observed"]["signature"],
        )

    def test_history_uses_source_clock_and_counts_runs_not_retry_attempts(self) -> None:
        prepared = self._prepared(
            "Failed Namespace.Type.Test [42 ms]\n"
            "Assert.Equal() Failure: Expected 1 Actual 2"
        )
        prepared["issues"][0]["workflowHealth"]["workflowPath"] = ".github/workflows/ci.yml"
        initial = build_ci_failure_triage(prepared)["assessments"][0]
        self.assertEqual("verified", initial["family"]["status"])
        family_id = initial["family"]["familyId"]
        as_of = parse_aware_iso8601(
            prepared["sourceCollectedAt"],
            "prepared.sourceCollectedAt",
        )
        rows = [
            {
                "schemaVersion": 2,
                "familyStatus": "verified",
                "familyId": family_id,
                "runId": 100,
                "attempt": 2,
                "outcome": "failure",
                "occurredAt": (as_of - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
            },
            {
                "schemaVersion": 2,
                "familyStatus": "verified",
                "familyId": family_id,
                "runId": 99,
                "attempt": 1,
                "outcome": "failure",
                "occurredAt": (as_of - timedelta(days=8)).isoformat().replace("+00:00", "Z"),
            },
        ]

        history = build_ci_failure_triage(
            prepared,
            history_rows=rows,
        )["assessments"][0]["history"]

        self.assertEqual(3, len(history["attempts"]))
        self.assertEqual(1, history["windows"]["7d"]["failedRuns"])
        self.assertEqual(2, history["windows"]["14d"]["failedRuns"])
        self.assertIsNone(history["windows"]["30d"]["observedExecutions"])
        self.assertEqual("unknown", history["windows"]["30d"]["denominatorStatus"])

    def test_same_snapshot_failures_converge_and_passing_retry_is_retained(self) -> None:
        data = workflow_snapshot()
        data["evidence"]["run:100"]["payload"]["workflowPath"] = (
            ".github/workflows/ci.yml"
        )
        add_execution(
            data,
            101,
            "2026-08-19T15:45:00Z",
            excerpt=data["evidence"]["run:100:attempt:1:job:900:log"]["payload"]["excerpt"],
        )
        data["evidence"]["run:101"]["payload"]["workflowPath"] = (
            ".github/workflows/ci.yml"
        )
        prepared = prepare_assessment(data)
        first = build_ci_failure_triage(prepared)
        self.assertEqual(
            [2, 2],
            [
                case["history"]["windows"]["7d"]["failedRuns"]
                for case in first["assessments"]
            ],
        )

        attached = attach_ci_failure_triage(prepared, first)
        from ci_shepherd.poc_history import collect_rows_from_prepared, current_triage_events

        second = build_ci_failure_triage(
            prepared,
            history_rows=current_triage_events(collect_rows_from_prepared(attached)),
        )
        self.assertEqual(
            first["assessments"],
            second["assessments"],
        )

        retry_data = workflow_snapshot()
        retry_data["evidence"]["run:100"]["payload"]["workflowPath"] = (
            ".github/workflows/ci.yml"
        )
        retry_data["evidence"].update([
            evidence(
                "run:100:attempt:2:job:902",
                "workflow-job",
                {
                    **retry_data["evidence"]["run:100:attempt:1:job:900"]["payload"],
                    "attempt": 2,
                    "jobId": 902,
                    "checkRunId": 1902,
                    "conclusion": "success",
                    "completedAt": "2026-08-19T15:45:00Z",
                },
            ),
            evidence(
                "run:100:attempt:2:job:902:test-results",
                "workflow-test-results",
                results_payload(
                    12,
                    run_id=100,
                    attempt=2,
                    job_id=902,
                    tests=[{"testName": "Namespace.Type.Test", "outcome": "passed"}],
                ),
            ),
        ])
        retry_history = build_ci_failure_triage(
            prepare_assessment(retry_data)
        )["assessments"][0]["history"]
        self.assertEqual(
            ["failure", "success"],
            [attempt["outcome"] for attempt in retry_history["attempts"]],
        )

    def test_failed_test_result_establishes_test_failure(self) -> None:
        data = workflow_snapshot()
        data["evidence"].update([
            evidence(
                "run:100:attempt:1:job:900:test-results",
                "workflow-test-results",
                results_payload(
                    12,
                    run_id=100,
                    attempt=1,
                    job_id=900,
                    tests=[{
                        "testName": "Namespace.Type.Test",
                        "outcome": "failed",
                        "errorMessage": "Expected true but was false",
                    }],
                ),
            )
        ])
        assessment = build_ci_failure_triage(prepare_assessment(data))["assessments"][0]
        self.assertEqual("test", assessment["observed"]["phase"])
        self.assertTrue(assessment["observed"]["testFailureEstablished"])

    def test_attach_validates_occurrence_linkage(self) -> None:
        prepared = self._prepared("src/File.cs(1,1): error CS0618: obsolete")
        triage = build_ci_failure_triage(prepared)
        triage["assessments"][0]["occurrenceId"] = "occurrence:foreign"

        with self.assertRaisesRegex(ValueError, "occurrence"):
            attach_ci_failure_triage(prepared, triage)

    def test_incomplete_occurrence_scope_remains_unknown(self) -> None:
        prepared = prepare_assessment(
            snapshot(
                issue_payload(
                    12,
                    facts=[fact("testName", "Namespace.Type.Z")],
                    ledger_rows=[{"sourceRun": 100, "date": "2026-08-19"}],
                ),
                evidence("run:100", "workflow-run", run_payload()),
            )
        )

        assessment = build_ci_failure_triage(prepared)["assessments"][0]

        self.assertEqual("unknown", assessment["family"]["status"])

    def test_success_coverage_requires_matching_workflow_identity(self) -> None:
        data = workflow_snapshot()
        data["evidence"]["run:100"]["payload"]["workflowPath"] = (
            ".github/workflows/ci.yml"
        )
        add_execution(data, 101, "2026-08-19T15:45:00Z", conclusion="success")
        data["evidence"]["run:101"]["payload"].update(
            workflowId=10,
            workflowPath=".github/workflows/unrelated.yml",
        )

        attempts = build_ci_failure_triage(
            prepare_assessment(data)
        )["assessments"][0]["history"]["attempts"]

        self.assertEqual(["failure"], [attempt["outcome"] for attempt in attempts])

    def test_prepared_validation_accepts_legacy_absence_and_rejects_rule_mismatch(self) -> None:
        prepared = self._prepared("src/File.cs(1,1): error CS0618: obsolete")
        validate_prepared_ci_failure_triage(prepared, allow_absent=True)

        attached = attach_ci_failure_triage(prepared, build_ci_failure_triage(prepared))
        broken = copy.deepcopy(attached)
        broken["issues"][0]["ciFailureTriage"]["ruleVersion"] = TRIAGE_RULE_VERSION + "-other"
        with self.assertRaisesRegex(ValueError, "ruleVersion"):
            validate_prepared_ci_failure_triage(broken, allow_absent=False)

        broken = copy.deepcopy(attached)
        broken["issues"][0]["ciFailureTriage"]["cases"][0]["history"]["windows"]["7d"][
            "failedRuns"
        ] = 0
        with self.assertRaisesRegex(ValueError, "history window"):
            validate_prepared_ci_failure_triage(broken, allow_absent=False)

        broken = copy.deepcopy(attached)
        broken["observations"]["occurrences"] = []
        with self.assertRaisesRegex(ValueError, "unknown occurrence"):
            validate_prepared_ci_failure_triage(broken, allow_absent=False)

        broken = copy.deepcopy(attached)
        family = broken["issues"][0]["ciFailureTriage"]["cases"][0]["family"]
        family["dimensions"]["workflowId"] = "not-a-workflow"
        family["dimensions"]["testName"] = "Foreign.Test"
        from ci_shepherd.ci_failure_triage import _fingerprint

        family["familyId"] = _fingerprint(family["dimensions"])
        with self.assertRaisesRegex(ValueError, "family"):
            validate_prepared_ci_failure_triage(broken, allow_absent=False)


if __name__ == "__main__":
    unittest.main()
