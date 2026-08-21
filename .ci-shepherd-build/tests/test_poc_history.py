from __future__ import annotations

import json
import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ci_shepherd.poc_history import (
    append_new_rows,
    collect_rows_from_prepared,
    compute_fingerprint,
    group_rows_by_fingerprint,
    merge_occurrence_dimensions,
    read_ledger_rows,
)


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

    def test_missing_trailing_newline_does_not_corrupt_later_appends(self) -> None:
        # A prior write (e.g. a crash mid-append) can leave the ledger's last
        # line without its terminating newline. Appending after that must not
        # concatenate the next row onto it -- that would merge two JSON
        # objects onto one line and make the whole line unparseable.
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

            appended = append_new_rows(path, [new_row])

            self.assertEqual([new_row], appended)
            # The merged first line is malformed and is skipped, but the new
            # row -- and any rows appended afterward -- must still round-trip.
            self.assertEqual([new_row], read_ledger_rows(path))

            later_row = {
                "fingerprint": "test:namespace.type.later",
                "issueNumber": 102,
                "runId": 1002,
                "attempt": 1,
                "date": "2026-08-18",
                "job": "Tests / Linux",
                "testName": "Namespace.Type.Later",
            }
            append_new_rows(path, [later_row])

            self.assertEqual([new_row, later_row], read_ledger_rows(path))


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
