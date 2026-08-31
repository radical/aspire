from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ci_shepherd.poc_state import (
    load_review_schedule,
    record_review_events,
    record_review_wakeup,
)


class ReviewScheduleTests(unittest.TestCase):
    def test_reviewed_cases_do_not_become_due_from_elapsed_time_alone(self) -> None:
        with TemporaryDirectory() as scratch:
            state = Path(scratch) / "state"
            record_review_events(
                state,
                "owner/repo",
                "2026-08-20T12:00:00Z",
                issue_numbers=[101],
                pull_request_numbers=[202],
            )

            schedule = load_review_schedule(
                state,
                "owner/repo",
                "2027-08-27T12:00:00Z",
                issue_numbers=[101],
                pull_request_numbers=[202],
            )

            self.assertEqual([], schedule["dueIssueNumbers"])
            self.assertEqual([], schedule["duePullRequestNumbers"])
            self.assertEqual(
                {"lastReviewedAt": "2026-08-20T12:00:00Z"},
                schedule["issues"]["101"],
            )

    def test_only_explicit_typed_wakeups_become_due(self) -> None:
        with TemporaryDirectory() as scratch:
            state = Path(scratch) / "state"
            record_review_events(
                state,
                "owner/repo",
                "2026-08-20T12:00:00Z",
                issue_numbers=[101],
                pull_request_numbers=[],
            )
            record_review_wakeup(
                state,
                "owner/repo",
                target_kind="issue",
                target_number=101,
                evaluate_at="2026-08-27T12:00:00Z",
                reason="closure-without-recurrence",
            )

            before_due = load_review_schedule(
                state,
                "owner/repo",
                "2026-08-27T11:59:59Z",
                issue_numbers=[101],
                pull_request_numbers=[],
            )
            due = load_review_schedule(
                state,
                "owner/repo",
                "2026-08-27T12:00:00Z",
                issue_numbers=[101],
                pull_request_numbers=[],
            )

            self.assertEqual([], before_due["dueIssueNumbers"])
            self.assertEqual([101], due["dueIssueNumbers"])
            self.assertEqual(
                {
                    "lastReviewedAt": "2026-08-20T12:00:00Z",
                    "reassessAt": "2026-08-27T12:00:00Z",
                    "wakeReason": "closure-without-recurrence",
                },
                due["issues"]["101"],
            )


if __name__ == "__main__":
    unittest.main()
