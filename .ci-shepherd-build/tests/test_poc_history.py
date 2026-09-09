from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ci_shepherd.ci_failure_triage import (
    attach_ci_failure_triage,
    build_ci_failure_triage,
)
from ci_shepherd.lifecycle import prepare_assessment
from ci_shepherd.poc_history import (
    append_new_rows,
    collect_rows_from_prepared,
    compute_fingerprint,
    current_triage_events,
    group_rows_by_fingerprint,
    merge_occurrence_dimensions,
    read_ledger_rows,
)
from test_workflow_health import add_execution, workflow_snapshot


def _identity(
    *,
    tier1_cause_id: str | None = None,
    tier2_test_name: str | None = None,
    tier3_error_code: str | None = None,
) -> dict[str, object]:
    return {
        "tier1CauseId": tier1_cause_id,
        "tier2TestName": tier2_test_name,
        "tier2ExceptionType": None,
        "tier3ErrorCode": tier3_error_code,
        "tier3Job": None,
    }


def _prepared_issue(
    issue_number: int,
    *,
    identity: dict[str, object],
    rows: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "issueNumber": issue_number,
        "identity": identity,
        "ledger": {"rows": rows},
    }


class ComputeFingerprintTests(unittest.TestCase):
    def test_prefers_exact_test_name_over_error_code_and_cause_id(self) -> None:
        identity = _identity(
            tier1_cause_id="timeout",
            tier2_test_name="Namespace.Type.Test",
            tier3_error_code="0xdeadbeef",
        )
        self.assertEqual("test:namespace.type.test", compute_fingerprint(identity))

    def test_falls_back_to_error_code_when_test_name_missing(self) -> None:
        identity = _identity(tier1_cause_id="timeout", tier3_error_code="0xDEADBEEF")
        self.assertEqual("error:0xdeadbeef", compute_fingerprint(identity))

    def test_falls_back_to_cause_id_when_test_name_and_error_code_missing(self) -> None:
        identity = _identity(tier1_cause_id="Docker Daemon Timeout")
        self.assertEqual("cause:docker daemon timeout", compute_fingerprint(identity))

    def test_normalizes_case_and_whitespace(self) -> None:
        identity = _identity(tier2_test_name="  Namespace.Type.Test  \t")
        self.assertEqual("test:namespace.type.test", compute_fingerprint(identity))

    def test_returns_none_without_any_stable_identity(self) -> None:
        self.assertIsNone(compute_fingerprint(_identity()))
        self.assertIsNone(compute_fingerprint(_identity(tier2_test_name="   ")))


class CollectRowsFromPreparedTests(unittest.TestCase):
    def test_current_unknown_revision_replaces_stale_proof_before_history_projection(self) -> None:
        value = workflow_snapshot()
        value["evidence"]["run:100"]["payload"]["workflowPath"] = ".github/workflows/ci.yml"
        log = value["evidence"]["run:100:attempt:1:job:900:log"]["payload"]
        original_diagnostic = log["excerpt"]
        prepared = prepare_assessment(value)
        rows = collect_rows_from_prepared(
            attach_ci_failure_triage(prepared, build_ci_failure_triage(prepared))
        )
        log.update(excerpt="Process exited with code 1", facts=[])
        add_execution(value, 101, "2026-08-19T15:45:00Z", excerpt=original_diagnostic)
        value["evidence"]["run:101"]["payload"]["workflowPath"] = ".github/workflows/ci.yml"
        current = build_ci_failure_triage(prepare_assessment(value), history_rows=current_triage_events(rows))
        verified, = [case for case in current["assessments"] if case["family"]["status"] == "verified"]
        self.assertEqual(1, verified["history"]["windows"]["7d"]["failedRuns"])
        self.assertEqual([101], [attempt["runId"] for attempt in verified["history"]["attempts"]])

    def test_reclassification_supersedes_the_same_physical_occurrence(self) -> None:
        rows = []
        for excerpt in ("src/File.cs(1): error CS1002: ; expected", "Process exited with code 1"):
            value = workflow_snapshot()
            value["evidence"]["run:100"]["payload"]["workflowPath"] = ".github/workflows/ci.yml"
            value["evidence"]["run:100:attempt:1:job:900:log"]["payload"].update(excerpt=excerpt, facts=[])
            prepared = prepare_assessment(value)
            row, = collect_rows_from_prepared(
                attach_ci_failure_triage(prepared, build_ci_failure_triage(prepared))
            )
            rows.append(row)

        self.assertEqual(["verified", "unknown"], [row["familyStatus"] for row in rows])
        self.assertEqual(rows[0]["logicalOccurrenceId"], rows[1]["logicalOccurrenceId"])
        self.assertEqual([rows[1]], current_triage_events(rows))

    def test_logical_identity_survives_occurrence_ordinal_renumbering(self) -> None:
        first_snapshot = workflow_snapshot()
        first_snapshot["evidence"]["run:100:attempt:1:job:900:log"]["payload"][
            "excerpt"
        ] = "Failed Namespace.Type.Z [42 ms]\nSystem.Exception: failed"
        first = prepare_assessment(first_snapshot)
        first_rows = collect_rows_from_prepared(
            attach_ci_failure_triage(first, build_ci_failure_triage(first))
        )

        expanded_snapshot = workflow_snapshot()
        expanded_snapshot["evidence"]["run:100:attempt:1:job:900:log"]["payload"][
            "excerpt"
        ] = (
            "Failed Namespace.Type.A [42 ms]\nSystem.Exception: other\n"
            "Failed Namespace.Type.Z [42 ms]\nSystem.Exception: failed"
        )
        expanded = prepare_assessment(expanded_snapshot)
        expanded_rows = collect_rows_from_prepared(
            attach_ci_failure_triage(expanded, build_ci_failure_triage(expanded))
        )

        old_z = next(row for row in first_rows if row["testName"] == "Namespace.Type.Z")
        new_z = next(row for row in expanded_rows if row["testName"] == "Namespace.Type.Z")
        new_a = next(row for row in expanded_rows if row["testName"] == "Namespace.Type.A")
        self.assertEqual(old_z["logicalOccurrenceId"], new_z["logicalOccurrenceId"])
        self.assertNotEqual(old_z["logicalOccurrenceId"], new_a["logicalOccurrenceId"])

    def test_collects_rows_using_source_run_or_run_id(self) -> None:
        prepared = {
            "issues": [
                _prepared_issue(
                    101,
                    identity=_identity(tier2_test_name="Namespace.Type.Test"),
                    rows=[
                        {"date": "2026-08-17", "sourceRun": 1001, "job": "Tests / Linux"},
                        {"createdAt": "2026-08-18T10:00:00Z", "runId": 1002},
                    ],
                )
            ]
        }

        rows = collect_rows_from_prepared(prepared)

        self.assertEqual(
            [
                {
                    "fingerprint": "test:namespace.type.test",
                    "issueNumber": 101,
                    "runId": 1001,
                    "attempt": 1,
                    "date": "2026-08-17",
                    "job": "Tests / Linux",
                    "testName": "Namespace.Type.Test",
                },
                {
                    "fingerprint": "test:namespace.type.test",
                    "issueNumber": 101,
                    "runId": 1002,
                    "attempt": 1,
                    "date": "2026-08-18",
                    "job": None,
                    "testName": "Namespace.Type.Test",
                },
            ],
            rows,
        )

    def test_uses_explicit_attempt_when_present(self) -> None:
        prepared = {
            "issues": [
                _prepared_issue(
                    101,
                    identity=_identity(tier2_test_name="Namespace.Type.Test"),
                    rows=[{"date": "2026-08-17", "sourceRun": 1001, "attempt": 2}],
                )
            ]
        }

        rows = collect_rows_from_prepared(prepared)

        self.assertEqual(1, len(rows))
        self.assertEqual(2, rows[0]["attempt"])

    def test_skips_rows_without_a_positive_run_id(self) -> None:
        prepared = {
            "issues": [
                _prepared_issue(
                    101,
                    identity=_identity(tier2_test_name="Namespace.Type.Test"),
                    rows=[
                        {"date": "2026-08-17", "sourceRun": None},
                        {"date": "2026-08-17", "sourceRun": 0},
                        {"date": "2026-08-17", "sourceRun": -5},
                    ],
                )
            ]
        }

        self.assertEqual([], collect_rows_from_prepared(prepared))

    def test_skips_issues_without_a_stable_identity(self) -> None:
        prepared = {
            "issues": [
                _prepared_issue(
                    102,
                    identity=_identity(),
                    rows=[{"date": "2026-08-17", "sourceRun": 2001}],
                )
            ]
        }

        self.assertEqual([], collect_rows_from_prepared(prepared))


class LedgerAppendTests(unittest.TestCase):
    def test_returning_to_previous_evidence_or_rules_appends_a_current_revision(self) -> None:
        for change in ("diagnostic", "rule"):
            with self.subTest(change=change), TemporaryDirectory() as scratch:
                path = Path(scratch) / "fingerprints.jsonl"
                appended_counts = []
                for index in range(4):
                    value = workflow_snapshot()
                    value["evidence"]["run:100"]["payload"]["workflowPath"] = ".github/workflows/ci.yml"
                    excerpt = "Failed Namespace.Type.Test [42 ms]"
                    if change != "diagnostic" or index != 1:
                        excerpt += "\nAssert.Equal() Failure: Expected 1 Actual 2"
                    value["evidence"]["run:100:attempt:1:job:900:log"]["payload"]["excerpt"] = excerpt
                    prepared = prepare_assessment(value)
                    triage = build_ci_failure_triage(prepared)
                    if change == "rule" and index == 1:
                        triage["ruleVersion"] += "-revised"
                    rows = collect_rows_from_prepared(attach_ci_failure_triage(prepared, triage))
                    appended_counts.append(len(append_new_rows(path, rows)))
                    current, = current_triage_events(read_ledger_rows(path))
                    self.assertEqual(rows[0]["familyStatus"], current["familyStatus"])
                    self.assertEqual(rows[0]["ruleVersion"], current["ruleVersion"])
                self.assertEqual([1, 1, 1, 0], appended_counts)

    def test_v1_row_does_not_suppress_v2_enrichment(self) -> None:
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / "fingerprints.jsonl"
            legacy = {
                "fingerprint": "test:namespace.type.test",
                "issueNumber": 101,
                "runId": 1001,
                "attempt": 1,
                "date": "2026-08-17",
                "job": "Tests / Linux",
                "testName": "Namespace.Type.Test",
            }
            enriched = {
                **legacy,
                "schemaVersion": 2,
                "eventId": "triage-event:one",
                "logicalOccurrenceId": "triage-occurrence:one",
                "familyStatus": "verified",
                "familyId": "fnv1a64:0000000000000001",
                "caseId": "triage:occurrence:one",
                "evidenceFingerprint": "fnv1a64:0000000000000002",
                "ruleVersion": "aspire-ci-triage-v1",
                "occurredAt": "2026-08-17T10:00:00Z",
                "observedAt": "2026-08-18T10:00:00Z",
                "outcome": "failure",
            }

            append_new_rows(path, [legacy])
            appended = append_new_rows(path, [enriched])

            self.assertEqual([enriched], appended)
            self.assertEqual([enriched], current_triage_events(read_ledger_rows(path)))

    def test_current_triage_events_selects_latest_revision_per_occurrence(self) -> None:
        base = {
            "schemaVersion": 2,
            "fingerprint": "test:namespace.type.test",
            "issueNumber": 101,
            "runId": 1001,
            "attempt": 1,
            "date": "2026-08-17",
            "job": "Tests / Linux",
            "testName": "Namespace.Type.Test",
            "logicalOccurrenceId": "triage-occurrence:one",
            "caseId": "triage:occurrence:one",
            "occurredAt": "2026-08-17T10:00:00Z",
            "observedAt": "2026-08-18T10:00:00Z",
            "outcome": "failure",
        }
        verified = {
            **base,
            "eventId": "triage-event:one",
            "familyStatus": "verified",
            "familyId": "fnv1a64:0000000000000001",
            "evidenceFingerprint": "fnv1a64:0000000000000002",
            "ruleVersion": "aspire-ci-triage-v1",
        }
        corrected = {
            **base,
            "eventId": "triage-event:two",
            "familyStatus": "unknown",
            "familyId": None,
            "evidenceFingerprint": "fnv1a64:0000000000000003",
            "ruleVersion": "aspire-ci-triage-v2",
        }

        self.assertEqual([corrected], current_triage_events([verified, corrected]))

    def test_v2_events_distinguish_two_families_in_same_attempt(self) -> None:
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / "fingerprints.jsonl"
            base = {
                "schemaVersion": 2,
                "fingerprint": "test:namespace.type.test",
                "issueNumber": 101,
                "runId": 1001,
                "attempt": 1,
                "date": "2026-08-17",
                "job": "Tests / Linux",
                "testName": "Namespace.Type.Test",
                "familyStatus": "verified",
                "evidenceFingerprint": "fnv1a64:0000000000000002",
                "ruleVersion": "aspire-ci-triage-v1",
                "occurredAt": "2026-08-17T10:00:00Z",
                "observedAt": "2026-08-18T10:00:00Z",
                "outcome": "failure",
            }
            rows = [
                {
                    **base,
                    "eventId": f"triage-event:{value}",
                    "logicalOccurrenceId": f"triage-occurrence:{value}",
                    "caseId": f"triage:occurrence:{value}",
                    "familyId": f"fnv1a64:{value:016x}",
                }
                for value in (1, 2)
            ]

            self.assertEqual(rows, append_new_rows(path, rows))
            self.assertEqual(rows, current_triage_events(read_ledger_rows(path)))

    def test_append_and_read_round_trips_rows(self) -> None:
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / "fingerprints.jsonl"
            rows = [
                {
                    "fingerprint": "test:namespace.type.test",
                    "issueNumber": 101,
                    "runId": 1001,
                    "attempt": 1,
                    "date": "2026-08-17",
                    "job": "Tests / Linux",
                    "testName": "Namespace.Type.Test",
                }
            ]

            appended = append_new_rows(path, rows)

            self.assertEqual(rows, appended)
            self.assertEqual(rows, read_ledger_rows(path))
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(1, len(lines))
            json.loads(lines[0])  # each line must be valid standalone JSON

    def test_recording_the_same_rows_twice_does_not_double_count(self) -> None:
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / "fingerprints.jsonl"
            row = {
                "fingerprint": "test:namespace.type.test",
                "issueNumber": 101,
                "runId": 1001,
                "attempt": 1,
                "date": "2026-08-17",
                "job": "Tests / Linux",
                "testName": "Namespace.Type.Test",
            }

            first = append_new_rows(path, [row])
            second = append_new_rows(path, [row])

            self.assertEqual([row], first)
            self.assertEqual([], second)
            self.assertEqual([row], read_ledger_rows(path))

    def test_concurrent_appends_do_not_duplicate_the_same_row(self) -> None:
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / "fingerprints.jsonl"
            row = {
                "fingerprint": "test:namespace.type.test",
                "issueNumber": 101,
                "runId": 1001,
                "attempt": 1,
                "date": "2026-08-17",
                "job": "Tests / Linux",
                "testName": "Namespace.Type.Test",
            }

            with ThreadPoolExecutor(max_workers=8) as executor:
                appended = list(
                    executor.map(lambda _: append_new_rows(path, [row]), range(32))
                )

            self.assertEqual(1, sum(len(rows) for rows in appended))
            self.assertEqual([row], read_ledger_rows(path))

    def test_distinguishes_rows_by_full_identity_tuple(self) -> None:
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / "fingerprints.jsonl"
            base = {
                "fingerprint": "test:namespace.type.test",
                "issueNumber": 101,
                "runId": 1001,
                "attempt": 1,
                "date": "2026-08-17",
                "job": "Tests / Linux",
                "testName": "Namespace.Type.Test",
            }
            same_run_different_attempt = {**base, "attempt": 2}

            append_new_rows(path, [base])
            appended = append_new_rows(path, [same_run_different_attempt])

            self.assertEqual([same_run_different_attempt], appended)
            self.assertEqual(2, len(read_ledger_rows(path)))

    def test_read_ledger_rows_returns_empty_list_when_file_is_absent(self) -> None:
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / "missing.jsonl"
            self.assertEqual([], read_ledger_rows(path))

    def test_append_sets_owner_only_permissions(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX mode assertions are not portable to Windows")
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / "state" / "fingerprints.jsonl"
            row = {
                "fingerprint": "test:namespace.type.test",
                "issueNumber": 101,
                "runId": 1001,
                "attempt": 1,
                "date": "2026-08-17",
                "job": "Tests / Linux",
                "testName": "Namespace.Type.Test",
            }

            append_new_rows(path, [row])

            self.assertEqual(0o700, stat.S_IMODE(path.parent.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_incomplete_final_row_blocks_later_appends(self) -> None:
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / "fingerprints.jsonl"
            # Simulate a crash mid-write: a truncated JSON object with no
            # terminating newline, as if the process died partway through
            # encoding the previous row.
            truncated_row = '{"fingerprint": "test:namespace.type.stale", "issueNum'
            path.write_text(truncated_row, encoding="utf-8")

            new_row = {
                "fingerprint": "test:namespace.type.new",
                "issueNumber": 101,
                "runId": 1001,
                "attempt": 1,
                "date": "2026-08-17",
                "job": "Tests / Linux",
                "testName": "Namespace.Type.New",
            }

            with self.assertRaisesRegex(ValueError, "incomplete final row"):
                append_new_rows(path, [new_row])

            self.assertEqual(truncated_row, path.read_text(encoding="utf-8"))

    def test_malformed_complete_row_fails_closed(self) -> None:
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / "fingerprints.jsonl"
            path.write_text('{"fingerprint": "valid"}\nnot-json\n', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "invalid JSON"):
                read_ledger_rows(path)


class GroupRowsByFingerprintTests(unittest.TestCase):
    def test_groups_rows_and_ignores_rows_without_a_fingerprint(self) -> None:
        rows = [
            {"fingerprint": "test:a", "runId": 1},
            {"fingerprint": "test:a", "runId": 2},
            {"fingerprint": "test:b", "runId": 3},
            {"runId": 4},
        ]

        grouped = group_rows_by_fingerprint(rows)

        self.assertEqual(
            {
                "test:a": [{"fingerprint": "test:a", "runId": 1}, {"fingerprint": "test:a", "runId": 2}],
                "test:b": [{"fingerprint": "test:b", "runId": 3}],
            },
            grouped,
        )


class MergeOccurrenceDimensionsTests(unittest.TestCase):
    def test_merges_history_rows_into_base_dimensions(self) -> None:
        base = {
            "dates": {"2026-08-17"},
            "sourceRuns": {1001},
            "jobs": {"Tests / Linux"},
            "pullRequests": {501},
        }
        history_rows = [
            {"runId": 1001, "attempt": 1, "date": "2026-08-17", "job": "Tests / Linux"},
            {"runId": 2002, "attempt": 1, "date": "2026-07-01", "job": "Tests / Windows"},
        ]

        merged = merge_occurrence_dimensions(base, history_rows)

        self.assertEqual({"2026-08-17", "2026-07-01"}, merged["dates"])
        self.assertEqual({1001, 2002}, merged["sourceRuns"])
        self.assertEqual({"Tests / Linux", "Tests / Windows"}, merged["jobs"])
        self.assertEqual({501}, merged["pullRequests"])

    def test_does_not_double_count_repeated_run_attempt_pairs(self) -> None:
        base = {"dates": set(), "sourceRuns": set(), "jobs": set(), "pullRequests": set()}
        history_rows = [
            {"runId": 3003, "attempt": 1, "date": "2026-06-01", "job": "Tests"},
            {"runId": 3003, "attempt": 1, "date": "2026-06-01", "job": "Tests"},
        ]

        merged = merge_occurrence_dimensions(base, history_rows)

        self.assertEqual({3003}, merged["sourceRuns"])
        self.assertEqual({"2026-06-01"}, merged["dates"])

    def test_ignores_history_rows_without_a_positive_run_id(self) -> None:
        base = {"dates": set(), "sourceRuns": set(), "jobs": set(), "pullRequests": set()}
        history_rows = [{"runId": None, "date": "2026-06-01"}, {"runId": 0, "date": "2026-06-02"}]

        merged = merge_occurrence_dimensions(base, history_rows)

        self.assertEqual(set(), merged["sourceRuns"])
        self.assertEqual(set(), merged["dates"])


if __name__ == "__main__":
    unittest.main()
