from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ci_shepherd.quarantine import (
    read_quarantine_session_events,
    record_quarantine_session_event,
)
from ci_shepherd.quarantine_reconciliation import (
    reconcile_quarantine_pull_requests,
)


class QuarantineReconciliationTests(unittest.TestCase):
    def test_merged_exact_head_completes_the_batch(self) -> None:
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            request = self._request()
            self._record_open(state, request)

            result = reconcile_quarantine_pull_requests(
                state_directory=state,
                repository="radical/aspire",
                recorded_at="2026-08-30T00:03:00Z",
                get_pull=lambda _repository, _number: self._pull(
                    state="closed",
                    merged_at="2026-08-30T00:02:00Z",
                ),
                verify_merged_source=lambda _event, _pull: True,
            )

            self.assertEqual("completed", result["outcomes"][0]["status"])
            self.assertEqual(
                "completed",
                read_quarantine_session_events(state)[-1]["status"],
            )

    def test_closed_unmerged_releases_the_batch_with_a_reason(self) -> None:
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            request = self._request()
            self._record_open(state, request)

            result = reconcile_quarantine_pull_requests(
                state_directory=state,
                repository="radical/aspire",
                recorded_at="2026-08-30T00:03:00Z",
                get_pull=lambda _repository, _number: self._pull(
                    state="closed",
                    merged_at=None,
                ),
            )

            self.assertEqual("closed-unmerged", result["outcomes"][0]["status"])
            terminal = read_quarantine_session_events(state)[-1]
            self.assertEqual("failed", terminal["status"])
            self.assertIn("without merging", terminal["failureReason"])

    def test_changed_head_fails_closed_without_releasing_the_batch(self) -> None:
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            request = self._request()
            self._record_open(state, request)

            result = reconcile_quarantine_pull_requests(
                state_directory=state,
                repository="radical/aspire",
                recorded_at="2026-08-30T00:03:00Z",
                get_pull=lambda _repository, _number: {
                    **self._pull(state="open", merged_at=None),
                    "head": {"sha": "b" * 40},
                },
            )

            self.assertEqual("unverifiable", result["outcomes"][0]["status"])
            self.assertEqual(
                "pull-request-open",
                read_quarantine_session_events(state)[-1]["status"],
            )

    def test_merged_pull_without_source_verification_stays_unverifiable(
        self,
    ) -> None:
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            request = self._request()
            self._record_open(state, request)

            result = reconcile_quarantine_pull_requests(
                state_directory=state,
                repository="radical/aspire",
                recorded_at="2026-08-30T00:03:00Z",
                get_pull=lambda _repository, _number: self._pull(
                    state="closed",
                    merged_at="2026-08-30T00:02:00Z",
                ),
                verify_merged_source=lambda _event, _pull: False,
            )

            self.assertEqual("unverifiable", result["outcomes"][0]["status"])
            self.assertEqual(
                "pull-request-open",
                read_quarantine_session_events(state)[-1]["status"],
            )

    def _record_open(
        self,
        state: Path,
        request: dict[str, object],
    ) -> None:
        record_quarantine_session_event(
            state,
            request,
            status="started",
            recorded_at="2026-08-30T00:00:00Z",
            session_id="session-1",
        )
        record_quarantine_session_event(
            state,
            request,
            status="pull-request-open",
            recorded_at="2026-08-30T00:01:00Z",
            session_id="session-1",
            pull_request_url="https://github.com/radical/aspire/pull/73",
            pull_request_head_sha="a" * 40,
            completed_test_names=["Tests.One"],
            mutation_validation=self._mutation_validation(),
        )

    @staticmethod
    def _request() -> dict[str, object]:
        return {
            "repository": "radical/aspire",
            "snapshotId": "snapshot:1",
            "batchId": "quarantine:1",
            "sourceRevision": "a" * 40,
            "sourceTreeDigest": "sha256:" + "b" * 64,
            "tests": [
                {
                    "testName": "Tests.One",
                    "issueUrl": "https://github.com/radical/aspire/issues/1",
                    "sourceLocation": {
                        "file": "One.Tests/OneTests.cs",
                        "line": 10,
                    },
                    "sourceValidation": {
                        "fileSemanticDigest": "sha256:" + "c" * 64,
                        "fileQuarantines": [],
                    },
                }
            ],
        }

    @staticmethod
    def _pull(*, state: str, merged_at: str | None) -> dict[str, object]:
        return {
            "html_url": "https://github.com/radical/aspire/pull/73",
            "state": state,
            "merged_at": merged_at,
            "merge_commit_sha": "c" * 40,
            "head": {"sha": "a" * 40},
        }

    @staticmethod
    def _mutation_validation() -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "sourceRevision": "a" * 40,
            "sourceTreeDigest": "sha256:" + "b" * 64,
            "completedTests": ["Tests.One"],
            "changedFiles": ["tests/One.Tests/OneTests.cs"],
            "affectedProjects": ["tests/One.Tests/One.Tests.csproj"],
            "diffDigest": "sha256:" + "d" * 64,
        }


if __name__ == "__main__":
    unittest.main()
