from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ci_shepherd.quarantine import (
    apply_quarantine_source_inspection,
    quarantine_tool_tree_digest,
    read_quarantine_session_events,
    record_quarantine_session_event,
)
from ci_shepherd.quarantine_reconciliation import (
    MergedQuarantineSourceVerification,
    reconcile_quarantine_pull_requests,
    verify_merged_quarantine_source,
)
from ci_shepherd.quarantine_mutation import validate_quarantine_post_inspection
from ci_shepherd.repository_policy import load_repository_policy_document


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
QUARANTINE_TOOL = REPOSITORY_ROOT / "tools" / "QuarantineTools"


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
                get_reviews=lambda _repository, _number: [
                    {
                        "id": 1,
                        "state": "APPROVED",
                        "commit_id": "a" * 40,
                        "author_association": "MEMBER",
                        "user": {"login": "reviewer"},
                    }
                ],
                verify_merged_source=lambda _event, _pull: True,
            )

            self.assertEqual("completed", result["outcomes"][0]["status"])
            self.assertEqual(
                "completed",
                read_quarantine_session_events(state)[-1]["status"],
            )

    def test_real_merged_source_verifier_records_completion(self) -> None:
        with TemporaryDirectory() as scratch:
            scratch_path = Path(scratch)
            state = scratch_path / "state"
            tests_root = scratch_path / "tests"
            tests_root.mkdir()
            source_path = tests_root / "Tests.cs"
            source_path.write_text(
                """
namespace Demo;

public class Tests
{
    [Fact]
    public void Flaky()
    {
        Assert.True(true);
    }
}
""".lstrip(),
                encoding="utf-8",
            )
            test_name = "Demo.Tests.Flaky"
            issue_url = "https://github.com/radical/aspire/issues/1"
            base_request = self._request()
            base_request["tests"] = [
                {
                    "testName": test_name,
                    "issueNumber": 1,
                    "issueUrl": issue_url,
                    "issueNumbers": [1],
                    "issueUrls": [issue_url],
                    "evidenceIds": ["issue:1"],
                    "summary": "The test recovered on a retry.",
                }
            ]
            request = apply_quarantine_source_inspection(
                base_request,
                self._inspect(tests_root, test_name),
                source_revision="a" * 40,
                source_tree_digest="sha256:" + "b" * 64,
            )
            request["inspectorTreeDigest"] = quarantine_tool_tree_digest(
                QUARANTINE_TOOL
            )
            completed = subprocess.run(
                [
                    str(REPOSITORY_ROOT / ".dotnet" / "dotnet"),
                    "run",
                    "--project",
                    str(QUARANTINE_TOOL),
                    "--no-restore",
                    "--verbosity",
                    "quiet",
                    "--",
                    "--quarantine",
                    "--root",
                    str(tests_root),
                    "--url",
                    issue_url,
                    test_name,
                ],
                cwd=REPOSITORY_ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
                env={
                    **os.environ,
                    "DOTNET_ROLL_FORWARD": "Major",
                    "MSBUILDTERMINALLOGGER": "false",
                },
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            validated = validate_quarantine_post_inspection(
                request,
                self._inspect(tests_root, test_name),
            )
            mutation_validation = {
                **validated,
                "changedFiles": ["tests/Tests.cs"],
                "affectedProjects": ["tests/Demo.Tests.csproj"],
                "diffDigest": "sha256:" + "d" * 64,
            }
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
                completed_test_names=[test_name],
                mutation_validation=mutation_validation,
            )

            result = reconcile_quarantine_pull_requests(
                state_directory=state,
                repository="radical/aspire",
                recorded_at="2026-08-30T00:03:00Z",
                get_pull=lambda _repository, _number: self._pull(
                    state="closed",
                    merged_at="2026-08-30T00:02:00Z",
                ),
                get_reviews=lambda _repository, _number: [
                    {
                        "id": 1,
                        "state": "APPROVED",
                        "commit_id": "a" * 40,
                        "author_association": "MEMBER",
                        "user": {"login": "reviewer"},
                    }
                ],
                verify_merged_source=lambda event, pull: (
                    verify_merged_quarantine_source(
                        event,
                        event["mutationValidation"],
                        merge_commit_sha=pull["merge_commit_sha"],
                        tool_project=QUARANTINE_TOOL,
                        get_file=lambda _path, _revision: source_path.read_bytes(),
                    )
                ),
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
                    "head": {
                        "sha": "b" * 40,
                        "repo": {"full_name": "radical/aspire"},
                    },
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
                get_reviews=lambda _repository, _number: [
                    {
                        "id": 1,
                        "state": "APPROVED",
                        "commit_id": "a" * 40,
                        "author_association": "MEMBER",
                        "user": {"login": "reviewer"},
                    }
                ],
                verify_merged_source=lambda _event, _pull: (
                    MergedQuarantineSourceVerification(
                        verified=False,
                        code="inspector-runtime-failed",
                        reason="The merged-source inspector exited with code 1.",
                    )
                ),
            )

            self.assertEqual("unverifiable", result["outcomes"][0]["status"])
            self.assertEqual(
                "The merged-source inspector exited with code 1.",
                result["outcomes"][0]["reason"],
            )
            self.assertEqual(
                "pull-request-open",
                read_quarantine_session_events(state)[-1]["status"],
            )

    def test_merged_source_verifier_distinguishes_failure_classes(self) -> None:
        request = self._request()
        request["inspectorTreeDigest"] = "sha256:" + "e" * 64
        mutation = self._mutation_validation()

        with patch(
            "ci_shepherd.quarantine_reconciliation.quarantine_tool_tree_digest",
            return_value="sha256:" + "f" * 64,
        ):
            drift = verify_merged_quarantine_source(
                request,
                mutation,
                merge_commit_sha="c" * 40,
                tool_project=QUARANTINE_TOOL,
                get_file=lambda _path, _revision: b"",
            )
        self.assertFalse(drift)
        self.assertEqual("inspector-digest-drift", drift.code)

        def fail_source_fetch(_path: str, _revision: str) -> bytes:
            raise ValueError("malformed GitHub content")

        with patch(
            "ci_shepherd.quarantine_reconciliation.quarantine_tool_tree_digest",
            return_value=request["inspectorTreeDigest"],
        ):
            source_fetch = verify_merged_quarantine_source(
                request,
                mutation,
                merge_commit_sha="c" * 40,
                tool_project=QUARANTINE_TOOL,
                get_file=fail_source_fetch,
            )
        self.assertFalse(source_fetch)
        self.assertEqual("source-fetch-failed", source_fetch.code)

        with (
            patch(
                "ci_shepherd.quarantine_reconciliation.quarantine_tool_tree_digest",
                return_value=request["inspectorTreeDigest"],
            ),
            patch(
                "ci_shepherd.quarantine_reconciliation.subprocess.run",
                return_value=subprocess.CompletedProcess([], 1, "", "failed"),
            ),
        ):
            runtime = verify_merged_quarantine_source(
                request,
                mutation,
                merge_commit_sha="c" * 40,
                tool_project=QUARANTINE_TOOL,
                get_file=lambda _path, _revision: b"",
            )
        self.assertFalse(runtime)
        self.assertEqual("inspector-runtime-failed", runtime.code)

        with (
            patch(
                "ci_shepherd.quarantine_reconciliation.quarantine_tool_tree_digest",
                return_value=request["inspectorTreeDigest"],
            ),
            patch(
                "ci_shepherd.quarantine_reconciliation.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, "{", ""),
            ),
        ):
            malformed = verify_merged_quarantine_source(
                request,
                mutation,
                merge_commit_sha="c" * 40,
                tool_project=QUARANTINE_TOOL,
                get_file=lambda _path, _revision: b"",
            )
        self.assertFalse(malformed)
        self.assertEqual("inspector-output-malformed", malformed.code)

        inspection = {
            "schemaVersion": 1,
            "tests": [
                {
                    "testName": "Tests.One",
                    "status": "missing",
                    "matches": [],
                }
            ],
        }
        with (
            patch(
                "ci_shepherd.quarantine_reconciliation.quarantine_tool_tree_digest",
                return_value=request["inspectorTreeDigest"],
            ),
            unittest.mock.patch(
                "ci_shepherd.quarantine_reconciliation.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    [],
                    0,
                    json.dumps(inspection),
                    "",
                ),
            ),
        ):
            mismatch = verify_merged_quarantine_source(
                request,
                mutation,
                merge_commit_sha="c" * 40,
                tool_project=QUARANTINE_TOOL,
                get_file=lambda _path, _revision: b"",
            )
        self.assertFalse(mismatch)
        self.assertEqual("merged-source-mismatch", mismatch.code)

    def test_malformed_merged_source_result_cannot_complete_the_batch(self) -> None:
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
                get_reviews=lambda _repository, _number: [
                    {
                        "id": 1,
                        "state": "APPROVED",
                        "commit_id": "a" * 40,
                        "author_association": "MEMBER",
                        "user": {"login": "reviewer"},
                    }
                ],
                verify_merged_source=lambda _event, _pull: {
                    "verified": True
                },
            )

            self.assertEqual("unverifiable", result["outcomes"][0]["status"])
            self.assertEqual(
                "pull-request-open",
                read_quarantine_session_events(state)[-1]["status"],
            )

    def test_merged_pull_without_required_approval_stays_unverifiable(self) -> None:
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
                get_reviews=lambda _repository, _number: [],
                verify_merged_source=lambda _event, _pull: True,
            )

            self.assertEqual("unverifiable", result["outcomes"][0]["status"])
            self.assertIn(
                "required approving reviews",
                result["outcomes"][0]["reason"],
            )

    def test_pull_request_to_wrong_base_branch_stays_unverifiable(self) -> None:
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
                    "base": {
                        "ref": "disposable-branch",
                        "repo": {"full_name": "radical/aspire"},
                    },
                },
            )

            self.assertEqual("unverifiable", result["outcomes"][0]["status"])
            self.assertIn(
                "does not match repository policy",
                result["outcomes"][0]["reason"],
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
        return {
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
            "user": {"login": "author"},
            "head": {
                "sha": "a" * 40,
                "repo": {"full_name": "radical/aspire"},
            },
            "base": {
                "ref": "main",
                "repo": {"full_name": "radical/aspire"},
            },
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

    @staticmethod
    def _inspect(tests_root: Path, test_name: str) -> dict[str, object]:
        completed = subprocess.run(
            [
                str(REPOSITORY_ROOT / ".dotnet" / "dotnet"),
                "run",
                "--project",
                str(QUARANTINE_TOOL),
                "--no-restore",
                "--verbosity",
                "quiet",
                "--",
                "--inspect",
                "--root",
                str(tests_root),
                test_name,
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            env={
                **os.environ,
                "DOTNET_ROLL_FORWARD": "Major",
                "MSBUILDTERMINALLOGGER": "false",
            },
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr)
        return json.loads(completed.stdout)


if __name__ == "__main__":
    unittest.main()
