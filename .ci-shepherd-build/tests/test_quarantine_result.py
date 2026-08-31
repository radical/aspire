from __future__ import annotations

from copy import deepcopy
import unittest

from tempfile import TemporaryDirectory
from pathlib import Path

from ci_shepherd.quarantine_result import (
    record_quarantine_worker_result,
    validate_required_quarantine_approvals,
    validate_quarantine_worker_result,
)
from ci_shepherd.quarantine import (
    build_quarantine_session_plan,
    read_quarantine_session_events,
    record_quarantine_session_event,
)
from ci_shepherd.repository_policy import load_repository_policy_document


class QuarantineWorkerResultTests(unittest.TestCase):
    def setUp(self) -> None:
        policy = load_repository_policy_document(
            {
                "schemaVersion": 1,
                "policyVersion": "test-v1",
                "repositories": ["radical/aspire"],
                "retryTestResults": {
                    "aggregateJobSuffixes": ["Final Test Results"],
                    "artifactNames": ["All-TestResults"],
                    "trxPathPattern": (
                        r"^(?P<os>[^/]+)/testresults/"
                        r"(?P<lane>.+)_net[^_]+_[^/]+\.trx$"
                    ),
                    "jobNamePattern": (
                        r"^(?:.* / )?(?P<lane>[^/]+) "
                        r"\((?P<os>[^()]+)\)$"
                    ),
                    "trustedEvents": ["push", "workflow_dispatch"],
                    "requireHeadRepositoryMatch": True,
                },
                "quarantinePullRequest": {
                    "baseRef": "main",
                    "allowedHeadRepositories": ["radical/aspire"],
                    "requiredApprovingReviews": 1,
                },
            }
        )
        self.request = {
            "repository": "radical/aspire",
            "snapshotId": "snapshot:1",
            "batchId": "quarantine:1",
            "sourceRevision": "a" * 40,
            "sourceTreeDigest": "sha256:" + "b" * 64,
            "repositoryPolicy": {
                **policy.as_public_dict(),
                "digest": policy.digest,
            },
            "tests": [
                {
                    "testName": "Tests.One",
                    "issueNumber": 1,
                    "issueUrl": "https://github.com/radical/aspire/issues/1",
                    "issueNumbers": [1],
                    "issueUrls": ["https://github.com/radical/aspire/issues/1"],
                    "evidenceIds": ["issue:1"],
                    "summary": "Review Tests.One for quarantine.",
                    "sourceLocation": {
                        "file": "One.Tests/OneTests.cs",
                        "line": 10,
                    },
                    "sourceValidation": {
                        "fileSemanticDigest": "sha256:" + "c" * 64,
                        "fileQuarantines": [],
                    },
                },
                {
                    "testName": "Tests.Two",
                    "issueNumber": 2,
                    "issueUrl": "https://github.com/radical/aspire/issues/2",
                    "issueNumbers": [2],
                    "issueUrls": ["https://github.com/radical/aspire/issues/2"],
                    "evidenceIds": ["issue:2"],
                    "summary": "Review Tests.Two for quarantine.",
                    "sourceLocation": {
                        "file": "Two.Tests/TwoTests.cs",
                        "line": 20,
                    },
                    "sourceValidation": {
                        "fileSemanticDigest": "sha256:" + "d" * 64,
                        "fileQuarantines": [],
                    },
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
        result = self._successful_result()
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
                result=result,
                recorded_at="2026-08-30T00:01:00Z",
                pull_request_document={
                    "html_url": result["pullRequest"]["url"],
                    "state": "open",
                    "draft": True,
                    "changed_files": 2,
                    "user": {"login": "author"},
                    "head": {
                        "sha": "a" * 40,
                        "repo": {"full_name": "radical/aspire"},
                    },
                    "base": {
                        "ref": "main",
                        "repo": {"full_name": "radical/aspire"},
                    },
                },
                pull_request_files=self._pull_request_files(),
                mutation_result=self._mutation_result(),
                commit_validation=self._commit_validation(),
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
                        "changed_files": 2,
                        "head": {
                            "sha": "b" * 40,
                            "repo": {"full_name": "radical/aspire"},
                        },
                        "base": {
                            "ref": "main",
                            "repo": {"full_name": "radical/aspire"},
                        },
                    },
                )

    def test_rejects_a_pull_request_to_the_wrong_base_branch(self) -> None:
        result = self._successful_result()
        with TemporaryDirectory() as scratch:
            with self.assertRaisesRegex(ValueError, "does not match repository policy"):
                record_quarantine_worker_result(
                    state_directory=Path(scratch),
                    request=self.request,
                    result=result,
                    recorded_at="2026-08-30T00:01:00Z",
                    pull_request_document={
                        "html_url": result["pullRequest"]["url"],
                        "state": "open",
                        "draft": True,
                        "changed_files": 2,
                        "head": {
                            "sha": "a" * 40,
                            "repo": {"full_name": "radical/aspire"},
                        },
                        "base": {
                            "ref": "disposable-branch",
                            "repo": {"full_name": "radical/aspire"},
                        },
                    },
                )

    def test_rejects_a_pull_request_from_an_unapproved_head_repository(self) -> None:
        result = self._successful_result()
        with TemporaryDirectory() as scratch:
            with self.assertRaisesRegex(ValueError, "does not match repository policy"):
                record_quarantine_worker_result(
                    state_directory=Path(scratch),
                    request=self.request,
                    result=result,
                    recorded_at="2026-08-30T00:01:00Z",
                    pull_request_document={
                        "html_url": result["pullRequest"]["url"],
                        "state": "open",
                        "draft": True,
                        "changed_files": 2,
                        "head": {
                            "sha": "a" * 40,
                            "repo": {"full_name": "someone/aspire"},
                        },
                        "base": {
                            "ref": "main",
                            "repo": {"full_name": "radical/aspire"},
                        },
                    },
                )

    def test_rejects_live_pull_request_with_an_extra_file(self) -> None:
        result = self._successful_result()
        files = self._pull_request_files()
        files.append(
            {
                "filename": "Directory.Build.props",
                "status": "modified",
            }
        )
        with TemporaryDirectory() as scratch:
            with self.assertRaisesRegex(ValueError, "files do not match"):
                record_quarantine_worker_result(
                    state_directory=Path(scratch),
                    request=self.request,
                    result=result,
                    recorded_at="2026-08-30T00:01:00Z",
                    pull_request_document={
                        "html_url": result["pullRequest"]["url"],
                        "state": "open",
                        "draft": True,
                        "changed_files": 3,
                        "head": {
                            "sha": "a" * 40,
                            "repo": {"full_name": "radical/aspire"},
                        },
                        "base": {
                            "ref": "main",
                            "repo": {"full_name": "radical/aspire"},
                        },
                    },
                    pull_request_files=files,
                    mutation_result=self._mutation_result(),
                    commit_validation=self._commit_validation(),
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
                    "changed_files": 2,
                    "user": {"login": "author"},
                    "head": {
                        "sha": "a" * 40,
                        "repo": {"full_name": "radical/aspire"},
                    },
                    "base": {
                        "ref": "main",
                        "repo": {"full_name": "radical/aspire"},
                    },
                },
                pull_request_files=self._pull_request_files(),
                pull_request_reviews=[
                    {
                        "id": 1,
                        "state": "APPROVED",
                        "commit_id": "a" * 40,
                        "author_association": "MEMBER",
                        "user": {"login": "reviewer"},
                    }
                ],
                mutation_result=self._mutation_result(),
                commit_validation=self._commit_validation(),
            )

        self.assertEqual("completed", event["status"])

    def test_merged_result_requires_the_policy_approval_count(self) -> None:
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
            with self.assertRaisesRegex(ValueError, "required approving reviews"):
                record_quarantine_worker_result(
                    state_directory=state,
                    request=self.request,
                    result=completed,
                    recorded_at="2026-08-30T00:01:00Z",
                    pull_request_document={
                        "html_url": completed["pullRequest"]["url"],
                        "state": "closed",
                        "merged_at": "2026-08-30T00:00:30Z",
                        "draft": False,
                        "changed_files": 2,
                        "user": {"login": "author"},
                        "head": {
                            "sha": "a" * 40,
                            "repo": {"full_name": "radical/aspire"},
                        },
                        "base": {
                            "ref": "main",
                            "repo": {"full_name": "radical/aspire"},
                        },
                    },
                    pull_request_files=self._pull_request_files(),
                    pull_request_reviews=[],
                    mutation_result=self._mutation_result(),
                    commit_validation=self._commit_validation(),
                )

    def test_approval_must_match_the_validated_head_and_an_independent_reviewer(
        self,
    ) -> None:
        pull = {
            "head": {"sha": "a" * 40},
            "user": {"login": "author"},
        }
        invalid_reviews = [
            {
                "id": 1,
                "state": "APPROVED",
                "commit_id": "b" * 40,
                "author_association": "MEMBER",
                "user": {"login": "reviewer"},
            },
            {
                "id": 2,
                "state": "APPROVED",
                "commit_id": "a" * 40,
                "author_association": "OWNER",
                "user": {"login": "author"},
            },
            {
                "id": 3,
                "state": "APPROVED",
                "commit_id": "a" * 40,
                "author_association": "CONTRIBUTOR",
                "user": {"login": "contributor"},
            },
        ]

        with self.assertRaisesRegex(ValueError, "required approving reviews"):
            validate_required_quarantine_approvals(
                self.request,
                pull,
                invalid_reviews,
            )

    def test_review_comments_do_not_revoke_an_existing_approval(self) -> None:
        pull = {
            "head": {"sha": "a" * 40},
            "user": {"login": "author"},
        }
        validate_required_quarantine_approvals(
            self.request,
            pull,
            [
                {
                    "id": 1,
                    "state": "APPROVED",
                    "commit_id": "a" * 40,
                    "author_association": "MEMBER",
                    "user": {"login": "reviewer"},
                },
                {
                    "id": 2,
                    "state": "COMMENTED",
                    "commit_id": "a" * 40,
                    "author_association": "MEMBER",
                    "user": {"login": "reviewer"},
                },
            ],
        )

    def test_latest_decisive_review_state_can_revoke_an_approval(self) -> None:
        pull = {
            "head": {"sha": "a" * 40},
            "user": {"login": "author"},
        }
        with self.assertRaisesRegex(ValueError, "required approving reviews"):
            validate_required_quarantine_approvals(
                self.request,
                pull,
                [
                    {
                        "id": 1,
                        "state": "APPROVED",
                        "commit_id": "a" * 40,
                        "author_association": "MEMBER",
                        "user": {"login": "reviewer"},
                    },
                    {
                        "id": 2,
                        "state": "CHANGES_REQUESTED",
                        "commit_id": "a" * 40,
                        "author_association": "MEMBER",
                        "user": {"login": "reviewer"},
                    },
                ],
            )

    def test_successful_partial_result_cannot_bypass_mutation_validation(self) -> None:
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            record_quarantine_session_event(
                state,
                self.request,
                status="started",
                recorded_at="2026-08-30T00:00:00Z",
                session_id="session-1",
            )
            with self.assertRaisesRegex(ValueError, "completedTests"):
                record_quarantine_worker_result(
                    state_directory=state,
                    request=self.request,
                    result=self.result,
                    recorded_at="2026-08-30T00:01:00Z",
                    pull_request_document={
                        "html_url": self.result["pullRequest"]["url"],
                        "state": "open",
                        "draft": True,
                        "changed_files": 2,
                        "head": {
                            "sha": "a" * 40,
                            "repo": {"full_name": "radical/aspire"},
                        },
                        "base": {
                            "ref": "main",
                            "repo": {"full_name": "radical/aspire"},
                        },
                    },
                    pull_request_files=self._pull_request_files(),
                    mutation_result=self._mutation_result(),
                    commit_validation=self._commit_validation(),
                )

    def test_get_verified_result_advances_the_head_after_a_repair_push(self) -> None:
        result = self._successful_result()
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
                result=result,
                recorded_at="2026-08-30T00:01:00Z",
                pull_request_document={
                    "html_url": result["pullRequest"]["url"],
                    "state": "open",
                    "draft": True,
                    "changed_files": 2,
                    "head": {
                        "sha": "a" * 40,
                        "repo": {"full_name": "radical/aspire"},
                    },
                    "base": {
                        "ref": "main",
                        "repo": {"full_name": "radical/aspire"},
                    },
                },
                pull_request_files=self._pull_request_files(),
                mutation_result=self._mutation_result(),
                commit_validation=self._commit_validation(),
            )
            updated = deepcopy(result)
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
                    "changed_files": 2,
                    "head": {
                        "sha": "b" * 40,
                        "repo": {"full_name": "radical/aspire"},
                    },
                    "base": {
                        "ref": "main",
                        "repo": {"full_name": "radical/aspire"},
                    },
                },
                pull_request_files=self._pull_request_files(),
                mutation_result=self._mutation_result(),
                commit_validation={
                    **self._commit_validation(),
                    "commitSha": "b" * 40,
                },
            )

        self.assertEqual("b" * 40, event["pullRequestHeadSha"])

    def _successful_result(self) -> dict[str, object]:
        result = deepcopy(self.result)
        result["completedTests"] = ["Tests.One", "Tests.Two"]
        result["blockedTargets"] = []
        return result

    def _mutation_result(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "sourceRevision": self.request["sourceRevision"],
            "sourceTreeDigest": self.request["sourceTreeDigest"],
            "completedTests": ["Tests.One", "Tests.Two"],
            "changedFiles": [
                "tests/One.Tests/OneTests.cs",
                "tests/Two.Tests/TwoTests.cs",
            ],
            "affectedProjects": [
                "tests/One.Tests/One.Tests.csproj",
                "tests/Two.Tests/Two.Tests.csproj",
            ],
            "diffDigest": "sha256:" + "e" * 64,
        }

    @staticmethod
    def _pull_request_files() -> list[dict[str, object]]:
        return [
            {
                "filename": "tests/One.Tests/OneTests.cs",
                "status": "modified",
            },
            {
                "filename": "tests/Two.Tests/TwoTests.cs",
                "status": "modified",
            },
        ]

    @staticmethod
    def _commit_validation() -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "commitSha": "a" * 40,
            "changedFiles": [
                "tests/One.Tests/OneTests.cs",
                "tests/Two.Tests/TwoTests.cs",
            ],
            "diffDigest": "sha256:" + "e" * 64,
        }


if __name__ == "__main__":
    unittest.main()
