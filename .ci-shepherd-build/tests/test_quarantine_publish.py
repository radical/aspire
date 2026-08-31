from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ci_shepherd.jsonl import read_jsonl_rows
from ci_shepherd.quarantine_publish import publish_quarantine_pull_request
from ci_shepherd.quarantine_result import validate_quarantine_worker_result
from ci_shepherd.repository_policy import load_repository_policy_document


class QuarantinePublishTests(unittest.TestCase):
    def test_publishes_a_derived_branch_and_returns_a_worker_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout, body, audit = self._create_paths(root)
            commands: list[list[str]] = []
            commit = self._commit_validation()
            branch = "ci-shepherd/quarantine-0123456789abcdef"
            pull_request_url = "https://github.com/radical/aspire/pull/2"
            approved_body = body.read_text(encoding="utf-8")
            remote_reads = 0

            def run(
                command: list[str],
                **_: object,
            ) -> subprocess.CompletedProcess:
                nonlocal remote_reads
                commands.append(command)
                if command[-1] == "remote":
                    return subprocess.CompletedProcess(command, 0, "fork\n", "")
                if command[-3:-1] == ["remote", "get-url"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "git@github.com:radical/aspire.git\n",
                        "",
                    )
                if "ls-remote" in command:
                    remote_reads += 1
                    output = (
                        ""
                        if remote_reads == 1
                        else (
                            f"{commit['commitSha']}\trefs/heads/{branch}\n"
                        )
                    )
                    return subprocess.CompletedProcess(command, 0, output, "")
                if command[:3] == ["gh", "pr", "list"]:
                    return subprocess.CompletedProcess(command, 0, "[]\n", "")
                if "push" in command:
                    body.write_text(
                        "[automated] altered after approval\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[:3] == ["gh", "pr", "create"]:
                    body_argument = Path(
                        command[command.index("--body-file") + 1]
                    )
                    self.assertNotEqual(body_argument, body)
                    self.assertEqual(
                        body_argument.read_text(encoding="utf-8"),
                        approved_body,
                    )
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        pull_request_url + "\n",
                        "",
                    )
                if command[:3] == ["gh", "pr", "view"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        json.dumps(
                            {
                                "url": pull_request_url,
                                "headRefOid": commit["commitSha"],
                                "isDraft": True,
                                "baseRefName": "main",
                                "headRepository": {
                                    "nameWithOwner": "radical/aspire"
                                },
                            }
                        ),
                        "",
                    )
                raise AssertionError(f"Unexpected command: {command!r}")

            with (
                patch(
                    "ci_shepherd.quarantine_publish.create_quarantine_commit_validation",
                    return_value=commit,
                ),
                patch(
                    "ci_shepherd.quarantine_publish._require_clean_checkout",
                ),
                patch(
                    "ci_shepherd.quarantine_publish.read_quarantine_session_events",
                    return_value=[self._started_event()],
                ),
            ):
                result = publish_quarantine_pull_request(
                    request=self._request(),
                    mutation_result=self._mutation_result(),
                    commit_validation=commit,
                    checkout=checkout,
                    state_directory=root / "state",
                    session_id="session-1",
                    body_file=body,
                    audit_path=audit,
                    runner=run,
                )

            self.assertEqual(
                validate_quarantine_worker_result(self._request(), result),
                result,
            )
            push = next(command for command in commands if "push" in command)
            self.assertEqual(
                push[-1],
                f"{commit['commitSha']}:refs/heads/{branch}",
            )
            self.assertNotIn("--force", push)
            self.assertEqual(remote_reads, 2)
            list_command = next(
                command
                for command in commands
                if command[:3] == ["gh", "pr", "list"]
            )
            self.assertEqual(
                list_command[list_command.index("--head") + 1],
                branch,
            )
            create = next(
                command
                for command in commands
                if command[:3] == ["gh", "pr", "create"]
            )
            self.assertIn(f"radical:{branch}", create)
            self.assertEqual(
                create[create.index("--title") + 1],
                "[automated] test: quarantine 1 flaky test",
            )
            audit_rows = read_jsonl_rows(audit)
            self.assertEqual(
                audit_rows[0]["operationId"],
                audit_rows[1]["operationId"],
            )
            self.assertEqual(
                audit_rows[2]["operationId"],
                audit_rows[3]["operationId"],
            )
            self.assertNotEqual(
                audit_rows[0]["operationId"],
                audit_rows[2]["operationId"],
            )
            self.assertEqual(
                [
                    (row["operation"], row["phase"])
                    for row in audit_rows
                ],
                [
                    ("push-branch", "intent"),
                    ("push-branch", "outcome"),
                    ("create-pull-request", "intent"),
                    ("create-pull-request", "outcome"),
                ],
            )

    def test_refuses_the_production_repository_before_running_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout, body, audit = self._create_paths(root)
            commands: list[list[str]] = []

            def run(
                command: list[str],
                **_: object,
            ) -> subprocess.CompletedProcess:
                commands.append(command)
                raise AssertionError(f"Unexpected command: {command!r}")

            with self.assertRaisesRegex(ValueError, "forbidden for microsoft/aspire"):
                publish_quarantine_pull_request(
                    request=self._request(repository="microsoft/aspire"),
                    mutation_result=self._mutation_result(),
                    commit_validation=self._commit_validation(),
                    checkout=checkout,
                    state_directory=root / "state",
                    session_id="session-1",
                    body_file=body,
                    audit_path=audit,
                    runner=run,
                )

            self.assertFalse(audit.exists())
            self.assertEqual(commands, [])

    def test_refuses_a_remote_that_does_not_match_the_allowed_fork(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout, body, audit = self._create_paths(root)

            def run(
                command: list[str],
                **_: object,
            ) -> subprocess.CompletedProcess:
                if command[-1] == "remote":
                    return subprocess.CompletedProcess(command, 0, "origin\n", "")
                if command[-3:-1] == ["remote", "get-url"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "https://github.com/microsoft/aspire.git\n",
                        "",
                    )
                raise AssertionError(f"Unexpected command: {command!r}")

            with (
                patch(
                    "ci_shepherd.quarantine_publish.create_quarantine_commit_validation",
                    return_value=self._commit_validation(),
                ),
                patch(
                    "ci_shepherd.quarantine_publish._require_clean_checkout",
                ),
                patch(
                    "ci_shepherd.quarantine_publish.read_quarantine_session_events",
                    return_value=[self._started_event()],
                ),
            ):
                with self.assertRaisesRegex(ValueError, "allowed head repository"):
                    publish_quarantine_pull_request(
                        request=self._request(),
                        mutation_result=self._mutation_result(),
                        commit_validation=self._commit_validation(),
                        checkout=checkout,
                        state_directory=root / "state",
                        session_id="session-1",
                        body_file=body,
                        audit_path=audit,
                        runner=run,
                    )

            self.assertFalse(audit.exists())

    def test_issue_reference_must_match_a_complete_addresses_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout, body, audit = self._create_paths(root)
            commit = self._commit_validation()

            with (
                patch(
                    "ci_shepherd.quarantine_publish.create_quarantine_commit_validation",
                    return_value=commit,
                ),
                patch(
                    "ci_shepherd.quarantine_publish._require_clean_checkout",
                ),
                patch(
                    "ci_shepherd.quarantine_publish.read_quarantine_session_events",
                    return_value=[self._started_event()],
                ),
            ):
                for body_text in (
                    "[automated] Fixture.\n\nAddresses #10\n",
                    "[automated] Fixture.\n\nAddresses #1\nAddresses #2\n",
                ):
                    with self.subTest(body=body_text):
                        body.write_text(body_text, encoding="utf-8")
                        with self.assertRaisesRegex(
                            ValueError,
                            "source issue",
                        ):
                            publish_quarantine_pull_request(
                                request=self._request(),
                                mutation_result=self._mutation_result(),
                                commit_validation=commit,
                                checkout=checkout,
                                state_directory=root / "state",
                                session_id="session-1",
                                body_file=body,
                                audit_path=audit,
                                runner=lambda *_args, **_kwargs: self.fail(
                                    "No command should run for an invalid body."
                                ),
                            )

            self.assertFalse(audit.exists())

    def test_started_event_must_match_the_exact_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout, body, audit = self._create_paths(root)
            started = {
                **self._started_event(),
                "sourceRevision": "f" * 40,
            }

            with patch(
                "ci_shepherd.quarantine_publish.read_quarantine_session_events",
                return_value=[started],
            ):
                with self.assertRaisesRegex(ValueError, "exact active"):
                    publish_quarantine_pull_request(
                        request=self._request(),
                        mutation_result=self._mutation_result(),
                        commit_validation=self._commit_validation(),
                        checkout=checkout,
                        state_directory=root / "state",
                        session_id="session-1",
                        body_file=body,
                        audit_path=audit,
                        runner=lambda *_args, **_kwargs: self.fail(
                            "No command should run for a mismatched session."
                        ),
                    )

            self.assertFalse(audit.exists())

    def test_refuses_a_moved_head_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout, body, audit = self._create_paths(root)
            commit = self._commit_validation()
            moved = {**commit, "commitSha": "e" * 40}
            commands: list[list[str]] = []

            def run(
                command: list[str],
                **_: object,
            ) -> subprocess.CompletedProcess:
                commands.append(command)
                if command[-1] == "remote":
                    return subprocess.CompletedProcess(command, 0, "fork\n", "")
                if command[-3:-1] == ["remote", "get-url"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "https://github.com/radical/aspire.git\n",
                        "",
                    )
                if "ls-remote" in command:
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[:3] == ["gh", "pr", "list"]:
                    return subprocess.CompletedProcess(command, 0, "[]", "")
                raise AssertionError(f"Unexpected command: {command!r}")

            with (
                patch(
                    "ci_shepherd.quarantine_publish.create_quarantine_commit_validation",
                    side_effect=[commit, moved],
                ),
                patch(
                    "ci_shepherd.quarantine_publish._require_clean_checkout",
                ),
                patch(
                    "ci_shepherd.quarantine_publish.read_quarantine_session_events",
                    return_value=[self._started_event()],
                ),
            ):
                with self.assertRaisesRegex(ValueError, "changed after validation"):
                    publish_quarantine_pull_request(
                        request=self._request(),
                        mutation_result=self._mutation_result(),
                        commit_validation=commit,
                        checkout=checkout,
                        state_directory=root / "state",
                        session_id="session-1",
                        body_file=body,
                        audit_path=audit,
                        runner=run,
                    )

            self.assertFalse(audit.exists())
            self.assertFalse(any("push" in command for command in commands))

    def test_refuses_a_conflicting_remote_branch_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout, body, audit = self._create_paths(root)
            commit = self._commit_validation()
            commands: list[list[str]] = []

            def run(
                command: list[str],
                **_: object,
            ) -> subprocess.CompletedProcess:
                commands.append(command)
                if command[-1] == "remote":
                    return subprocess.CompletedProcess(command, 0, "fork\n", "")
                if command[-3:-1] == ["remote", "get-url"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "https://github.com/radical/aspire.git\n",
                        "",
                    )
                if "ls-remote" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        (
                            f"{'e' * 40}\trefs/heads/"
                            "ci-shepherd/quarantine-0123456789abcdef\n"
                        ),
                        "",
                    )
                raise AssertionError(f"Unexpected command: {command!r}")

            with (
                patch(
                    "ci_shepherd.quarantine_publish.create_quarantine_commit_validation",
                    return_value=commit,
                ),
                patch(
                    "ci_shepherd.quarantine_publish._require_clean_checkout",
                ),
                patch(
                    "ci_shepherd.quarantine_publish.read_quarantine_session_events",
                    return_value=[self._started_event()],
                ),
            ):
                with self.assertRaisesRegex(ValueError, "another commit"):
                    publish_quarantine_pull_request(
                        request=self._request(),
                        mutation_result=self._mutation_result(),
                        commit_validation=commit,
                        checkout=checkout,
                        state_directory=root / "state",
                        session_id="session-1",
                        body_file=body,
                        audit_path=audit,
                        runner=run,
                    )

            self.assertFalse(audit.exists())
            self.assertFalse(any("push" in command for command in commands))

    def test_push_crash_leaves_an_unmatched_intent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout, body, audit = self._create_paths(root)
            commit = self._commit_validation()

            def run(
                command: list[str],
                **_: object,
            ) -> subprocess.CompletedProcess:
                if command[-1] == "remote":
                    return subprocess.CompletedProcess(command, 0, "fork\n", "")
                if command[-3:-1] == ["remote", "get-url"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "https://github.com/radical/aspire.git\n",
                        "",
                    )
                if "ls-remote" in command:
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[:3] == ["gh", "pr", "list"]:
                    return subprocess.CompletedProcess(command, 0, "[]", "")
                if "push" in command:
                    raise KeyboardInterrupt("simulated process crash")
                raise AssertionError(f"Unexpected command: {command!r}")

            with (
                patch(
                    "ci_shepherd.quarantine_publish.create_quarantine_commit_validation",
                    return_value=commit,
                ),
                patch(
                    "ci_shepherd.quarantine_publish._require_clean_checkout",
                ),
                patch(
                    "ci_shepherd.quarantine_publish.read_quarantine_session_events",
                    return_value=[self._started_event()],
                ),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    publish_quarantine_pull_request(
                        request=self._request(),
                        mutation_result=self._mutation_result(),
                        commit_validation=commit,
                        checkout=checkout,
                        state_directory=root / "state",
                        session_id="session-1",
                        body_file=body,
                        audit_path=audit,
                        runner=run,
                    )

            self.assertEqual(
                [
                    (row["operation"], row["phase"])
                    for row in read_jsonl_rows(audit)
                ],
                [("push-branch", "intent")],
            )

    def test_existing_exact_branch_resumes_with_pull_request_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout, body, audit = self._create_paths(root)
            commit = self._commit_validation()
            pull_request_url = "https://github.com/radical/aspire/pull/2"
            commands: list[list[str]] = []

            def run(
                command: list[str],
                **_: object,
            ) -> subprocess.CompletedProcess:
                commands.append(command)
                if command[-1] == "remote":
                    return subprocess.CompletedProcess(command, 0, "fork\n", "")
                if command[-3:-1] == ["remote", "get-url"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "https://github.com/radical/aspire.git\n",
                        "",
                    )
                if "ls-remote" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        (
                            f"{commit['commitSha']}\trefs/heads/"
                            "ci-shepherd/quarantine-0123456789abcdef\n"
                        ),
                        "",
                    )
                if command[:3] == ["gh", "pr", "list"]:
                    return subprocess.CompletedProcess(command, 0, "[]", "")
                if command[:3] == ["gh", "pr", "create"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        pull_request_url,
                        "",
                    )
                if command[:3] == ["gh", "pr", "view"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        json.dumps(
                            {
                                "url": pull_request_url,
                                "headRefOid": commit["commitSha"],
                                "isDraft": True,
                                "baseRefName": "main",
                                "headRepository": {
                                    "nameWithOwner": "radical/aspire"
                                },
                            }
                        ),
                        "",
                    )
                raise AssertionError(f"Unexpected command: {command!r}")

            with (
                patch(
                    "ci_shepherd.quarantine_publish.create_quarantine_commit_validation",
                    return_value=commit,
                ),
                patch(
                    "ci_shepherd.quarantine_publish._require_clean_checkout",
                ),
                patch(
                    "ci_shepherd.quarantine_publish.read_quarantine_session_events",
                    return_value=[self._started_event()],
                ),
            ):
                result = publish_quarantine_pull_request(
                    request=self._request(),
                    mutation_result=self._mutation_result(),
                    commit_validation=commit,
                    checkout=checkout,
                    state_directory=root / "state",
                    session_id="session-1",
                    body_file=body,
                    audit_path=audit,
                    runner=run,
                )

            self.assertEqual(result["pullRequest"]["url"], pull_request_url)
            self.assertFalse(any("push" in command for command in commands))
            self.assertEqual(
                [
                    (row["operation"], row["phase"])
                    for row in read_jsonl_rows(audit)
                ],
                [
                    ("create-pull-request", "intent"),
                    ("create-pull-request", "outcome"),
                ],
            )

    def test_existing_exact_pull_request_is_reused_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout, body, audit = self._create_paths(root)
            commit = self._commit_validation()
            pull_request_url = "https://github.com/radical/aspire/pull/2"
            commands: list[list[str]] = []
            pull_request = {
                "url": pull_request_url,
                "headRefOid": commit["commitSha"],
                "isDraft": True,
                "baseRefName": "main",
                "headRepository": {"nameWithOwner": "radical/aspire"},
            }

            def run(
                command: list[str],
                **_: object,
            ) -> subprocess.CompletedProcess:
                commands.append(command)
                if command[-1] == "remote":
                    return subprocess.CompletedProcess(command, 0, "fork\n", "")
                if command[-3:-1] == ["remote", "get-url"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "https://github.com/radical/aspire.git\n",
                        "",
                    )
                if "ls-remote" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        (
                            f"{commit['commitSha']}\trefs/heads/"
                            "ci-shepherd/quarantine-0123456789abcdef\n"
                        ),
                        "",
                    )
                if command[:3] == ["gh", "pr", "list"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        json.dumps([pull_request]),
                        "",
                    )
                raise AssertionError(f"Unexpected command: {command!r}")

            with (
                patch(
                    "ci_shepherd.quarantine_publish.create_quarantine_commit_validation",
                    return_value=commit,
                ),
                patch(
                    "ci_shepherd.quarantine_publish._require_clean_checkout",
                ),
                patch(
                    "ci_shepherd.quarantine_publish.read_quarantine_session_events",
                    return_value=[self._started_event()],
                ),
            ):
                result = publish_quarantine_pull_request(
                    request=self._request(),
                    mutation_result=self._mutation_result(),
                    commit_validation=commit,
                    checkout=checkout,
                    state_directory=root / "state",
                    session_id="session-1",
                    body_file=body,
                    audit_path=audit,
                    runner=run,
                )

            self.assertEqual(result["pullRequest"]["url"], pull_request_url)
            self.assertFalse(audit.exists())
            self.assertFalse(any("push" in command for command in commands))
            self.assertFalse(
                any(
                    command[:3] == ["gh", "pr", "create"]
                    for command in commands
                )
            )

    @staticmethod
    def _create_paths(root: Path) -> tuple[Path, Path, Path]:
        checkout = root / "checkout"
        checkout.mkdir()
        body = root / "body.md"
        body.write_text(
            "[automated] Quarantine the fixture.\n\nAddresses #1\n",
            encoding="utf-8",
        )
        return checkout, body, root / "mutations.jsonl"

    @staticmethod
    def _request(
        repository: str = "radical/aspire",
    ) -> dict[str, object]:
        policy = load_repository_policy_document(
            {
                "schemaVersion": 1,
                "policyVersion": "test-v1",
                "repositories": [repository],
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
                    "trustedEvents": ["push"],
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
            "schemaVersion": 1,
            "repository": repository,
            "snapshotId": "snapshot:1",
            "batchId": "quarantine:fnv1a64:0123456789abcdef",
            "sourceRevision": "a" * 40,
            "sourceTreeDigest": "sha256:" + "b" * 64,
            "repositoryPolicy": {
                **policy.as_public_dict(),
                "digest": policy.digest,
            },
            "tests": [
                {
                    "testName": "Tests.Flaky",
                    "issueNumber": 1,
                    "issueUrl": f"https://github.com/{repository}/issues/1",
                    "sourceLocation": {
                        "file": "Tests/Tests.cs",
                        "line": 10,
                    },
                }
            ],
        }

    @staticmethod
    def _mutation_result() -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "sourceRevision": "a" * 40,
            "sourceTreeDigest": "sha256:" + "b" * 64,
            "completedTests": ["Tests.Flaky"],
            "changedFiles": ["tests/Tests/Tests.cs"],
            "affectedProjects": ["tests/Tests/Tests.csproj"],
            "diffDigest": "sha256:" + "c" * 64,
        }

    @staticmethod
    def _commit_validation() -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "commitSha": "d" * 40,
            "changedFiles": ["tests/Tests/Tests.cs"],
            "diffDigest": "sha256:" + "c" * 64,
        }

    @classmethod
    def _started_event(cls) -> dict[str, object]:
        return {
            **cls._request(),
            "status": "started",
            "sessionId": "session-1",
            "authorizationGrantId": "quarantine-grant:1",
            "authorizationExpiresAt": "2026-08-31T03:18:42Z",
        }


if __name__ == "__main__":
    unittest.main()
