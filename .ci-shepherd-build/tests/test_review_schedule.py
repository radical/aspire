from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ci_shepherd.poc_state import load_review_schedule, record_review_events


class ReviewScheduleTests(unittest.TestCase):
    def test_reviewed_cases_become_due_after_seven_days(self) -> None:
        with TemporaryDirectory() as scratch:
            state = Path(scratch) / "state"
            record_review_events(
                state,
                "owner/repo",
                "2026-08-20T12:00:00Z",
                issue_numbers=[101],
                pull_request_numbers=[202],
            )

            before_due = load_review_schedule(
                state,
                "owner/repo",
                "2026-08-27T11:59:59Z",
                issue_numbers=[101],
                pull_request_numbers=[202],
            )
            self.assertEqual([], before_due["dueIssueNumbers"])
            self.assertEqual([], before_due["duePullRequestNumbers"])

            due = load_review_schedule(
                state,
                "owner/repo",
                "2026-08-27T12:00:00Z",
                issue_numbers=[101],
                pull_request_numbers=[202],
            )
            self.assertEqual([101], due["dueIssueNumbers"])
            self.assertEqual([202], due["duePullRequestNumbers"])
            self.assertEqual(
                {
                    "lastReviewedAt": "2026-08-20T12:00:00Z",
                    "reassessAt": "2026-08-27T12:00:00Z",
                },
                due["issues"]["101"],
            )


if __name__ == "__main__":
    unittest.main()
