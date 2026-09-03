from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import reconcile_quarantine as reconcile_quarantine_script
from ci_shepherd.quarantine import (
    apply_quarantine_source_inspection,
    build_quarantine_session_plan,
    quarantine_tool_tree_digest,
    read_quarantine_session_events,
    record_quarantine_session_event,
)
from ci_shepherd.quarantine_reconciliation import (
    MergedQuarantineSourceVerification,
    reconcile_quarantine_pull_requests,
    reconcile_quarantine_source,
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
            self.assertEqual(
                [
                    {
                        "test": request["tests"][0],
                        "reason": (
                            "The quarantine pull request closed without merging."
                        ),
                    }
                ],
                terminal["blockedTargets"],
            )
            plan = build_quarantine_session_plan(
                request,
                read_quarantine_session_events(state),
            )
            self.assertIsNone(plan["proposal"])
            self.assertEqual("blocked-targets", plan["suppressionReason"])

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

    def test_publication_pending_recovers_the_created_pull_request(self) -> None:
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            request = self._request()
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
                status="publication-pending",
                recorded_at="2026-08-30T00:01:00Z",
                session_id="session-1",
                pull_request_head_sha="a" * 40,
                completed_test_names=["Tests.One"],
                mutation_validation=self._mutation_validation(),
            )

            result = reconcile_quarantine_pull_requests(
                state_directory=state,
                repository="radical/aspire",
                recorded_at="2026-08-30T00:03:00Z",
                get_pull=lambda _repository, _number: self.fail(
                    "Recovered publication should not need a second pull read."
                ),
                find_pull=lambda _repository, _batch_id, _head_repository: self._pull(
                    state="open",
                    merged_at=None,
                ),
            )

            self.assertEqual("recovered-open", result["outcomes"][0]["status"])
            terminal = read_quarantine_session_events(state)[-1]
            self.assertEqual("pull-request-open", terminal["status"])
            self.assertEqual(
                "https://github.com/radical/aspire/pull/73",
                terminal["pullRequestUrl"],
            )

    def test_publication_pending_recovers_a_pull_request_that_already_closed(
        self,
    ) -> None:
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            request = self._request()
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
                status="publication-pending",
                recorded_at="2026-08-30T00:01:00Z",
                session_id="session-1",
                pull_request_head_sha="a" * 40,
                completed_test_names=["Tests.One"],
                mutation_validation=self._mutation_validation(),
            )

            result = reconcile_quarantine_pull_requests(
                state_directory=state,
                repository="radical/aspire",
                recorded_at="2026-08-30T00:03:00Z",
                get_pull=lambda _repository, _number: self.fail(
                    "Recovered publication should not need a second pull read."
                ),
                find_pull=lambda _repository, _batch_id, _head_repository: self._pull(
                    state="closed",
                    merged_at=None,
                ),
            )

            self.assertEqual("recovered-closed", result["outcomes"][0]["status"])
            terminal = read_quarantine_session_events(state)[-1]
            self.assertEqual("failed", terminal["status"])
            self.assertEqual(
                [
                    {
                        "test": request["tests"][0],
                        "reason": (
                            "The quarantine pull request closed without merging."
                        ),
                    }
                ],
                terminal["blockedTargets"],
            )
            plan = build_quarantine_session_plan(
                request,
                read_quarantine_session_events(state),
            )
            self.assertIsNone(plan["proposal"])
            self.assertEqual("blocked-targets", plan["suppressionReason"])

    def test_pending_lookup_failure_does_not_block_other_batches(self) -> None:
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            pending_request = self._request()
            open_request = {
                **self._request(),
                "batchId": "quarantine:fnv1a64:fedcba9876543210",
            }
            self._record_open(state, open_request)
            record_quarantine_session_event(
                state,
                pending_request,
                status="started",
                recorded_at="2026-08-30T00:00:00Z",
                session_id="session-2",
            )
            record_quarantine_session_event(
                state,
                pending_request,
                status="publication-pending",
                recorded_at="2026-08-30T00:01:00Z",
                session_id="session-2",
                pull_request_head_sha="a" * 40,
                completed_test_names=["Tests.One"],
                mutation_validation=self._mutation_validation(),
            )

            result = reconcile_quarantine_pull_requests(
                state_directory=state,
                repository="radical/aspire",
                recorded_at="2026-08-30T00:03:00Z",
                find_pull=lambda _repository, _batch_id, _head_repository: (
                    (_ for _ in ()).throw(ValueError("ambiguous lookup"))
                ),
                get_pull=lambda _repository, _number: self._pull(
                    state="open",
                    merged_at=None,
                ),
            )

            self.assertEqual(
                ["unverifiable", "pending"],
                [outcome["status"] for outcome in result["outcomes"]],
            )

    def test_open_pull_lookup_failure_does_not_block_other_batches(self) -> None:
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            first = self._request()
            second = {
                **self._request(),
                "batchId": "quarantine:fnv1a64:fedcba9876543210",
            }
            self._record_open(state, first, pull_number=73)
            self._record_open(state, second, pull_number=74)

            def get_pull(_repository: str, number: int) -> dict[str, object]:
                if number == 73:
                    raise RuntimeError("transient lookup failure")
                return {
                    **self._pull(state="open", merged_at=None),
                    "html_url": "https://github.com/radical/aspire/pull/74",
                }

            result = reconcile_quarantine_pull_requests(
                state_directory=state,
                repository="radical/aspire",
                recorded_at="2026-08-30T00:03:00Z",
                get_pull=get_pull,
            )

            self.assertEqual(
                ["unverifiable", "pending"],
                [outcome["status"] for outcome in result["outcomes"]],
            )

    def test_live_lookup_uses_the_allowed_head_repository_owner(self) -> None:
        class Client:
            endpoint: str | None = None

            def get(self, endpoint: str) -> list[object]:
                self.endpoint = endpoint
                return []

        client = Client()

        result = reconcile_quarantine_script._find_pull_request(
            client,
            "microsoft/aspire",
            "quarantine:fnv1a64:0123456789abcdef",
            "radical/aspire",
        )

        self.assertIsNone(result)
        self.assertIn("head=radical%3A", str(client.endpoint))

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
        *,
        pull_number: int = 73,
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
            pull_request_url=(
                f"https://github.com/radical/aspire/pull/{pull_number}"
            ),
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
            "draft": True,
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


class QuarantineSourceReconciliationTests(unittest.TestCase):
    def test_quarantine_label_without_a_current_attribute_asks_a_human(self) -> None:
        result = reconcile_quarantine_source(
            _labeled_prepared(),
            _source_state(
                quarantines=[],
                tests=[
                    _inspection_result(
                        "Demo.Tests.Flaky",
                        "resolved",
                        [_match("Demo.Tests/Tests.cs", 31)],
                    )
                ],
            ),
        )

        self.assertEqual(
            [
                {
                    "issueNumber": 22,
                    "issueUrl": "https://github.com/owner/repo/issues/22",
                    "kind": "label-without-attribute",
                    "claimedTestName": "Demo.Tests.Flaky",
                    "currentSource": [
                        {
                            "testName": "Demo.Tests.Flaky",
                            "file": "Demo.Tests/Tests.cs",
                            "line": 31,
                            "quarantineIssueUrls": [],
                        }
                    ],
                    "summary": (
                        "The `quarantined-test` label is on this issue, but "
                        "`Demo.Tests.Flaky` at `Demo.Tests/Tests.cs:31` carries no "
                        "`[QuarantinedTest]` attribute in the inspected source."
                    ),
                    "humanAction": (
                        "Confirm whether this test should be quarantined. Either "
                        "quarantine it against this issue or remove the "
                        "`quarantined-test` label."
                    ),
                }
            ],
            result["findings"],
        )
        self.assertEqual("a" * 40, result["sourceRevision"])
        self.assertEqual("sha256:" + "b" * 64, result["sourceTreeDigest"])

    def test_quarantine_attribute_for_another_issue_does_not_satisfy_label(
        self,
    ) -> None:
        other_issue_url = "https://github.com/owner/repo/issues/99"
        result = reconcile_quarantine_source(
            _labeled_prepared(),
            _source_state(
                quarantines=[
                    {
                        "testName": "Demo.Tests.Flaky",
                        "issueUrl": other_issue_url,
                        "file": "Demo.Tests/Tests.cs",
                        "line": 31,
                    }
                ],
                tests=[
                    _inspection_result(
                        "Demo.Tests.Flaky",
                        "resolved",
                        [
                            _match(
                                "Demo.Tests/Tests.cs",
                                31,
                                quarantine_issue_url=other_issue_url,
                            )
                        ],
                    )
                ],
            ),
        )

        finding = result["findings"][0]
        self.assertEqual("quarantined-against-other-issue", finding["kind"])
        self.assertEqual([other_issue_url], finding["currentSource"][0]["quarantineIssueUrls"])
        self.assertIn("link other issues", finding["summary"])
        self.assertEqual(
            "Review whether this issue duplicates https://github.com/owner/repo/issues/99. "
            "Close the duplicate or repoint the existing attribute if this issue is the "
            "canonical tracker; do not add a second quarantine for the same method.",
            finding["humanAction"],
        )

    def test_renamed_method_keeps_the_issue_link_and_asks_for_a_correction(
        self,
    ) -> None:
        result = reconcile_quarantine_source(
            _labeled_prepared(),
            _source_state(
                quarantines=[
                    {
                        "testName": "Demo.Tests.FlakyRenamed",
                        "issueUrl": "https://github.com/owner/repo/issues/22",
                        "file": "Demo.Tests/Tests.cs",
                        "line": 44,
                    }
                ],
                tests=[
                    _inspection_result("Demo.Tests.Flaky", "not-found", []),
                ],
            ),
        )

        self.assertEqual(
            [
                {
                    "issueNumber": 22,
                    "issueUrl": "https://github.com/owner/repo/issues/22",
                    "kind": "attribute-name-drift",
                    "claimedTestName": "Demo.Tests.Flaky",
                    "currentSource": [
                        {
                            "testName": "Demo.Tests.FlakyRenamed",
                            "file": "Demo.Tests/Tests.cs",
                            "line": 44,
                            "quarantineIssueUrls": [
                                "https://github.com/owner/repo/issues/22"
                            ],
                        }
                    ],
                    "summary": (
                        "This issue is still linked from a `[QuarantinedTest]` "
                        "attribute, but the quarantined method is now "
                        "`Demo.Tests.FlakyRenamed` at `Demo.Tests/Tests.cs:44` "
                        "rather than the `Demo.Tests.Flaky` this issue names."
                    ),
                    "humanAction": (
                        "Update the issue title and metadata to the current "
                        "method name. The shepherd does not edit issue metadata."
                    ),
                }
            ],
            result["findings"],
        )

    def test_normalized_claim_matches_linked_source_name_case_insensitively(
        self,
    ) -> None:
        result = reconcile_quarantine_source(
            _labeled_prepared(test_name="demo.tests.flaky"),
            _source_state(
                quarantines=[
                    {
                        "testName": "Demo.Tests.Flaky",
                        "issueUrl": "https://github.com/owner/repo/issues/22",
                        "file": "Demo.Tests/Tests.cs",
                        "line": 31,
                    }
                ],
                tests=[
                    _inspection_result("demo.tests.flaky", "not-found", []),
                ],
            ),
        )

        self.assertEqual([], result["findings"])
        self.assertEqual(
            [
                {
                    "issueNumber": 22,
                    "issueUrl": "https://github.com/owner/repo/issues/22",
                    "tests": [
                        {
                            "testName": "Demo.Tests.Flaky",
                            "file": "Demo.Tests/Tests.cs",
                            "line": 31,
                        }
                    ],
                }
            ],
            result["verifiedIssues"],
        )

    def test_ambiguous_source_match_requires_human_review(self) -> None:
        result = reconcile_quarantine_source(
            _labeled_prepared(),
            _source_state(
                quarantines=[],
                tests=[
                    _inspection_result(
                        "Demo.Tests.Flaky",
                        "ambiguous",
                        [
                            _match("Demo.Tests/One.cs", 10),
                            _match("Demo.Tests/Two.cs", 20),
                        ],
                    )
                ],
            ),
        )

        finding = result["findings"][0]
        self.assertEqual("ambiguous-inspection", finding["kind"])
        self.assertEqual(
            ["Demo.Tests/One.cs", "Demo.Tests/Two.cs"],
            [entry["file"] for entry in finding["currentSource"]],
        )
        self.assertIn("multiple source matches", finding["summary"])

    def test_confidently_removed_test_becomes_a_closure_review_candidate(
        self,
    ) -> None:
        result = reconcile_quarantine_source(
            _labeled_prepared(),
            _source_state(
                quarantines=[],
                tests=[_inspection_result("Demo.Tests.Flaky", "not-found", [])],
            ),
            _completed_session_events(),
        )

        self.assertEqual(
            [
                {
                    "issueNumber": 22,
                    "issueUrl": "https://github.com/owner/repo/issues/22",
                    "kind": "removed-test-closure-review",
                    "claimedTestName": "Demo.Tests.Flaky",
                    "currentSource": [],
                    "priorQuarantine": {
                        "pullRequestUrl": "https://github.com/owner/repo/pull/73",
                        "recordedAt": "2026-08-30T00:03:00Z",
                    },
                    "summary": (
                        "`Demo.Tests.Flaky` was quarantined for this issue by "
                        "https://github.com/owner/repo/pull/73, and the inspected "
                        "source now contains neither that method nor any "
                        "`[QuarantinedTest]` attribute linking this issue."
                    ),
                    "humanAction": (
                        "Confirm the test was deleted rather than renamed, then "
                        "close this issue. The shepherd does not close issues on "
                        "absence."
                    ),
                }
            ],
            result["findings"],
        )

    def test_absent_test_without_a_recorded_quarantine_stays_ambiguous(self) -> None:
        result = reconcile_quarantine_source(
            _labeled_prepared(),
            _source_state(
                quarantines=[],
                tests=[_inspection_result("Demo.Tests.Flaky", "not-found", [])],
            ),
        )

        self.assertEqual(
            [
                {
                    "issueNumber": 22,
                    "issueUrl": "https://github.com/owner/repo/issues/22",
                    "kind": "ambiguous-absence",
                    "reason": "no-recorded-quarantine",
                    "claimedTestName": "Demo.Tests.Flaky",
                    "currentSource": [],
                    "summary": (
                        "`Demo.Tests.Flaky` is absent from the inspected source "
                        "and no `[QuarantinedTest]` attribute links this issue, "
                        "but the shepherd has no record of quarantining it, so "
                        "removal and rename are indistinguishable."
                    ),
                    "humanAction": (
                        "Decide whether the test was renamed or removed. The "
                        "shepherd will not close this issue on absence alone."
                    ),
                }
            ],
            result["findings"],
        )

    def test_absent_test_with_a_same_leaf_quarantine_stays_ambiguous(self) -> None:
        result = reconcile_quarantine_source(
            _labeled_prepared(),
            _source_state(
                quarantines=[
                    {
                        "testName": "Demo.OtherTests.Flaky",
                        "issueUrl": "https://github.com/owner/repo/issues/99",
                        "file": "Demo.Tests/OtherTests.cs",
                        "line": 12,
                    }
                ],
                tests=[_inspection_result("Demo.Tests.Flaky", "not-found", [])],
            ),
            _completed_session_events(),
        )

        finding = result["findings"][0]
        self.assertEqual("ambiguous-absence", finding["kind"])
        self.assertEqual("possible-move-candidate", finding["reason"])
        self.assertEqual(
            [
                {
                    "testName": "Demo.OtherTests.Flaky",
                    "file": "Demo.Tests/OtherTests.cs",
                    "line": 12,
                    "quarantineIssueUrls": [
                        "https://github.com/owner/repo/issues/99"
                    ],
                }
            ],
            finding["currentSource"],
        )

    def test_unnamed_labeled_issue_without_any_linking_attribute_asks_a_human(
        self,
    ) -> None:
        result = reconcile_quarantine_source(
            _labeled_prepared(test_name=None),
            _source_state(
                quarantines=[
                    {
                        "testName": "Demo.Tests.Unrelated",
                        "issueUrl": "https://github.com/owner/repo/issues/99",
                        "file": "Demo.Tests/Tests.cs",
                        "line": 12,
                    }
                ],
                tests=[],
            ),
        )

        self.assertEqual(
            [
                {
                    "issueNumber": 22,
                    "issueUrl": "https://github.com/owner/repo/issues/22",
                    "kind": "unresolved-test-identity",
                    "claimedTestName": None,
                    "currentSource": [],
                    "summary": (
                        "The `quarantined-test` label is on this issue, but the "
                        "collected issue evidence does not resolve a test method "
                        "name. No source method was checked; the repository-wide "
                        "inventory only confirms that no `[QuarantinedTest]` "
                        "attribute links this issue."
                    ),
                    "humanAction": (
                        "Identify the test method this issue tracks, or remove the "
                        "`quarantined-test` label if it does not track a test."
                    ),
                }
            ],
            result["findings"],
        )

    def test_unavailable_source_state_makes_no_claims(self) -> None:
        result = reconcile_quarantine_source(_labeled_prepared(), None)

        self.assertEqual([], result["findings"])
        self.assertEqual([22], result["unverifiableIssueNumbers"])
        self.assertIsNone(result["sourceRevision"])


def _labeled_prepared(
    *,
    test_name: str | None = "Demo.Tests.Flaky",
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "repository": "owner/repo",
        "snapshotId": "snapshot:owner/repo:test",
        "issues": [
            {
                "issueNumber": 22,
                "issueUrl": "https://github.com/owner/repo/issues/22",
                "identity": {"tier2TestName": test_name},
                "evidenceBundle": [
                    {
                        "id": "issue:22",
                        "kind": "issue-event",
                        "payload": {"labels": ["quarantined-test"]},
                    }
                ],
            }
        ],
    }


def _completed_session_events() -> list[dict[str, object]]:
    request = {
        "repository": "owner/repo",
        "snapshotId": "snapshot:owner/repo:test",
        "batchId": "quarantine:removed",
        "tests": [
            {
                "testName": "Demo.Tests.Flaky",
                "issueUrl": "https://github.com/owner/repo/issues/22",
            }
        ],
    }
    with TemporaryDirectory() as scratch:
        state = Path(scratch)
        record_quarantine_session_event(
            state,
            request,
            status="started",
            recorded_at="2026-08-30T00:00:00Z",
            session_id="session-removed",
        )
        record_quarantine_session_event(
            state,
            request,
            status="completed",
            recorded_at="2026-08-30T00:03:00Z",
            session_id="session-removed",
            pull_request_url="https://github.com/owner/repo/pull/73",
            pull_request_head_sha="a" * 40,
            completed_test_names=["Demo.Tests.Flaky"],
        )
        return read_quarantine_session_events(state)


def _source_state(
    *,
    quarantines: list[dict[str, object]],
    tests: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "sourceRevision": "a" * 40,
        "sourceTreeDigest": "sha256:" + "b" * 64,
        "inspectorTreeDigest": "sha256:" + "c" * 64,
        "quarantines": quarantines,
        "tests": tests,
    }


def _inspection_result(
    test_name: str,
    status: str,
    matches: list[dict[str, object]],
) -> dict[str, object]:
    return {"testName": test_name, "status": status, "matches": matches}


def _match(
    file: str,
    line: int,
    *,
    quarantine_issue_url: str | None = None,
    file_quarantines: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "file": file,
        "line": line,
        "quarantineAttributes": (
            [{"name": "QuarantinedTest", "issueUrl": quarantine_issue_url}]
            if quarantine_issue_url is not None
            else []
        ),
        "activeIssueAttributes": [],
        "fileSemanticDigest": "sha256:" + "d" * 64,
        "fileQuarantines": file_quarantines or [],
    }


if __name__ == "__main__":
    unittest.main()
