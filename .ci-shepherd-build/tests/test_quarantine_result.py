from __future__ import annotations

from copy import deepcopy
import unittest

from tempfile import TemporaryDirectory
from pathlib import Path

from ci_shepherd.quarantine_result import (
    record_quarantine_worker_result,
    validate_quarantine_worker_result,
)
from ci_shepherd.quarantine import (
    build_quarantine_session_plan,
    read_quarantine_session_events,
    record_quarantine_session_event,
)


class QuarantineWorkerResultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = {
            "repository": "radical/aspire",
            "snapshotId": "snapshot:1",
            "batchId": "quarantine:1",
            "tests": [
                {
                    "testName": "Tests.One",
                    "issueNumber": 1,
                    "issueUrl": "https://github.com/radical/aspire/issues/1",
                    "issueNumbers": [1],
                    "issueUrls": ["https://github.com/radical/aspire/issues/1"],
                    "evidenceIds": ["issue:1"],
                    "summary": "Review Tests.One for quarantine.",
                },
                {
                    "testName": "Tests.Two",
                    "issueNumber": 2,
                    "issueUrl": "https://github.com/radical/aspire/issues/2",
                    "issueNumbers": [2],
                    "issueUrls": ["https://github.com/radical/aspire/issues/2"],
                    "evidenceIds": ["issue:2"],
                    "summary": "Review Tests.Two for quarantine.",
                },
            ],
        }
        self.result = {
            "schemaVersion": 1,
            "repository": "radical/aspire",
            "snapshotId": "snapshot:1",
            "batchId": "quarantine:1",
            "sessionId": "session-1",
            "outcome": "pull-request-open",
            "completedTests": ["Tests.One"],
            "blockedTargets": [
                {"testName": "Tests.Two", "reason": "Test source was not found."}
            ],
            "pullRequest": {
                "url": "https://github.com/radical/aspire/pull/73",
                "headSha": "a" * 40,
            },
        }

    def test_accepts_a_complete_partition_of_worker_outcomes(self) -> None:
        validated = validate_quarantine_worker_result(self.request, self.result)

        self.assertEqual(["Tests.One"], validated["completedTests"])
        self.assertEqual("Tests.Two", validated["blockedTargets"][0]["testName"])

    def test_requires_every_requested_test_to_have_an_outcome(self) -> None:
        self.result["blockedTargets"] = []

        with self.assertRaisesRegex(ValueError, "Every requested"):
            validate_quarantine_worker_result(self.request, self.result)

    def test_rejects_an_unrequested_completed_test(self) -> None:
        self.result["completedTests"] = ["Tests.Three"]

        with self.assertRaisesRegex(ValueError, "unrequested"):
            validate_quarantine_worker_result(self.request, self.result)

    def test_failed_result_requires_a_reason_and_no_success_shape(self) -> None:
        failed = deepcopy(self.result)
        failed.update(
            {
                "outcome": "failed",
                "completedTests": [],
                "blockedTargets": [
                    {"testName": "Tests.One", "reason": "Worker failed."},
                    {"testName": "Tests.Two", "reason": "Worker failed."},
                ],
                "pullRequest": None,
            }
        )

        with self.assertRaisesRegex(ValueError, "failureReason"):
            validate_quarantine_worker_result(self.request, failed)

    def test_blocked_target_reasons_are_persisted(self) -> None:
        failed = deepcopy(self.result)
        failed.update(
            {
                "outcome": "failed",
                "completedTests": [],
                "blockedTargets": [
                    {"testName": "Tests.One", "reason": "Worker failed."},
                    {"testName": "Tests.Two", "reason": "Source was ambiguous."},
                ],
                "pullRequest": None,
                "failureReason": "No quarantine changes were safe to publish.",
            }
        )
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            record_quarantine_session_event(
                state,
                self.request,
                status="started",
                recorded_at="2026-08-30T00:00:00Z",
                session_id="session-1",
            )
            event = record_quarantine_worker_result(
                state_directory=state,
                request=self.request,
                result=failed,
                recorded_at="2026-08-30T00:01:00Z",
            )

        self.assertEqual(
            [
                {
                    "test": next(
                        test
                        for test in self.request["tests"]
                        if test["testName"] == target["testName"]
                    ),
                    "reason": target["reason"],
                }
                for target in failed["blockedTargets"]
            ],
            event["blockedTargets"],
        )

    def test_rejects_unknown_fields(self) -> None:
        self.result["comment"] = "freeform prose"

        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            validate_quarantine_worker_result(self.request, self.result)

    def test_records_only_a_get_verified_open_draft(self) -> None:
        with TemporaryDirectory() as scratch:
            record_quarantine_session_event(
                Path(scratch),
                self.request,
                status="started",
                recorded_at="2026-08-30T00:00:00Z",
                session_id="session-1",
            )
            event = record_quarantine_worker_result(
                state_directory=Path(scratch),
                request=self.request,
                result=self.result,
                recorded_at="2026-08-30T00:01:00Z",
                pull_request_document={
                    "html_url": self.result["pullRequest"]["url"],
                    "state": "open",
                    "draft": True,
                    "head": {"sha": "a" * 40},
                },
            )

        self.assertEqual("pull-request-open", event["status"])
        self.assertEqual("a" * 40, event["pullRequestHeadSha"])

    def test_rejects_a_changed_live_pull_request_head(self) -> None:
        with TemporaryDirectory() as scratch:
            with self.assertRaisesRegex(ValueError, "expected state"):
                record_quarantine_worker_result(
                    state_directory=Path(scratch),
                    request=self.request,
                    result=self.result,
                    recorded_at="2026-08-30T00:01:00Z",
                    pull_request_document={
                        "html_url": self.result["pullRequest"]["url"],
                        "state": "open",
                        "draft": True,
                        "head": {"sha": "b" * 40},
                    },
                )

    def test_get_verified_merged_result_completes_without_manual_override(self) -> None:
        completed = deepcopy(self.result)
        completed.update(
            {
                "outcome": "completed",
                "completedTests": ["Tests.One", "Tests.Two"],
                "blockedTargets": [],
            }
        )
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            record_quarantine_session_event(
                state,
                self.request,
                status="started",
                recorded_at="2026-08-30T00:00:00Z",
                session_id="session-1",
            )
            event = record_quarantine_worker_result(
                state_directory=state,
                request=self.request,
                result=completed,
                recorded_at="2026-08-30T00:01:00Z",
                pull_request_document={
                    "html_url": completed["pullRequest"]["url"],
                    "state": "closed",
                    "merged_at": "2026-08-30T00:00:30Z",
                    "draft": False,
                    "head": {"sha": "a" * 40},
                },
            )

        self.assertEqual("completed", event["status"])

    def test_blocked_test_identity_survives_changed_evidence_metadata(self) -> None:
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            record_quarantine_session_event(
                state,
                self.request,
                status="started",
                recorded_at="2026-08-30T00:00:00Z",
                session_id="session-1",
            )
            event = record_quarantine_worker_result(
                state_directory=state,
                request=self.request,
                result=self.result,
                recorded_at="2026-08-30T00:01:00Z",
                pull_request_document={
                    "html_url": self.result["pullRequest"]["url"],
                    "state": "open",
                    "draft": True,
                    "head": {"sha": "a" * 40},
                },
            )
            unchanged = build_quarantine_session_plan(
                self.request,
                read_quarantine_session_events(state),
            )
            changed_request = deepcopy(self.request)
            changed_request["tests"][1]["evidenceIds"] = ["run:new"]
            changed = build_quarantine_session_plan(
                changed_request,
                read_quarantine_session_events(state),
            )

        self.assertEqual(
            "Tests.Two",
            event["blockedTargets"][0]["test"]["testName"],
        )
        self.assertEqual(
            "Test source was not found.",
            event["blockedTargets"][0]["reason"],
        )
        self.assertIsNone(unchanged["proposal"])
        self.assertIsNone(changed["proposal"])
        self.assertEqual(
            {
                "testName": "Tests.Two",
                "reason": "Test source was not found.",
            },
            changed["blockedTargets"][0],
        )

    def test_get_verified_result_advances_the_head_after_a_repair_push(self) -> None:
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            record_quarantine_session_event(
                state,
                self.request,
                status="started",
                recorded_at="2026-08-30T00:00:00Z",
                session_id="session-1",
            )
            record_quarantine_worker_result(
                state_directory=state,
                request=self.request,
                result=self.result,
                recorded_at="2026-08-30T00:01:00Z",
                pull_request_document={
                    "html_url": self.result["pullRequest"]["url"],
                    "state": "open",
                    "draft": True,
                    "head": {"sha": "a" * 40},
                },
            )
            updated = deepcopy(self.result)
            updated["pullRequest"]["headSha"] = "b" * 40
            event = record_quarantine_worker_result(
                state_directory=state,
                request=self.request,
                result=updated,
                recorded_at="2026-08-30T00:02:00Z",
                pull_request_document={
                    "html_url": updated["pullRequest"]["url"],
                    "state": "open",
                    "draft": True,
                    "head": {"sha": "b" * 40},
                },
            )

        self.assertEqual("b" * 40, event["pullRequestHeadSha"])


if __name__ == "__main__":
    unittest.main()
