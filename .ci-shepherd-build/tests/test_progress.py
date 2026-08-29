from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ci_shepherd.progress import ProgressTracker, StageDeadlineExceeded


class ProgressTrackerTests(unittest.TestCase):
    def test_heartbeat_is_throttled_and_records_stage_progress(self) -> None:
        current = [datetime(2026, 8, 29, 20, tzinfo=UTC)]

        with TemporaryDirectory() as scratch:
            tracker = ProgressTracker(
                Path(scratch),
                now=lambda: current[0],
                heartbeat_interval_seconds=30,
                stage_deadline_seconds=300,
            )
            tracker.update("github-enrichment", "started")
            current[0] += timedelta(seconds=31)

            self.assertTrue(
                tracker.heartbeat(
                    "github-enrichment",
                    message="Fetching GitHub evidence.",
                )
            )
            current[0] += timedelta(seconds=1)
            self.assertFalse(tracker.heartbeat("github-enrichment"))

            document = json.loads(
                (Path(scratch) / "progress.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                ("github-enrichment", "progress"),
                (
                    document["events"][-1]["stage"],
                    document["events"][-1]["status"],
                ),
            )

    def test_heartbeat_fails_a_stage_that_exceeds_its_deadline(self) -> None:
        current = [datetime(2026, 8, 29, 20, tzinfo=UTC)]

        with TemporaryDirectory() as scratch:
            tracker = ProgressTracker(
                Path(scratch),
                now=lambda: current[0],
                heartbeat_interval_seconds=30,
                stage_deadline_seconds=60,
            )
            tracker.update("github-enrichment", "started")
            current[0] += timedelta(seconds=61)

            with self.assertRaisesRegex(
                StageDeadlineExceeded,
                "github-enrichment exceeded its 60-second deadline",
            ):
                tracker.heartbeat("github-enrichment")

            document = json.loads(
                (Path(scratch) / "progress.json").read_text(encoding="utf-8")
            )
            self.assertEqual("failed", document["status"])
            self.assertEqual(
                "stage-deadline-exceeded",
                document["events"][-1]["error"],
            )

    def test_completion_cannot_hide_an_exceeded_stage_deadline(self) -> None:
        current = [datetime(2026, 8, 29, 20, tzinfo=UTC)]

        with TemporaryDirectory() as scratch:
            tracker = ProgressTracker(
                Path(scratch),
                now=lambda: current[0],
                stage_deadline_seconds=60,
            )
            tracker.update("inventory", "started")
            current[0] += timedelta(seconds=61)

            with self.assertRaises(StageDeadlineExceeded):
                tracker.update("inventory", "completed")

            document = json.loads(
                (Path(scratch) / "progress.json").read_text(encoding="utf-8")
            )
            self.assertEqual("failed", document["status"])
            self.assertNotIn(
                "completed",
                [event["status"] for event in document["events"]],
            )

    def test_outer_collection_stage_does_not_share_leaf_deadline(self) -> None:
        current = [datetime(2026, 8, 29, 20, tzinfo=UTC)]

        with TemporaryDirectory() as scratch:
            tracker = ProgressTracker(
                Path(scratch),
                now=lambda: current[0],
                stage_deadline_seconds=60,
            )
            tracker.update("collection", "started")
            tracker.update("inventory", "started")
            current[0] += timedelta(seconds=50)
            tracker.update("inventory", "completed")
            tracker.update("github-enrichment", "started")
            current[0] += timedelta(seconds=50)
            tracker.update("github-enrichment", "completed")

            tracker.update("collection", "completed")

            document = json.loads(
                (Path(scratch) / "progress.json").read_text(encoding="utf-8")
            )
            self.assertEqual("complete", document["status"])


if __name__ == "__main__":
    unittest.main()
