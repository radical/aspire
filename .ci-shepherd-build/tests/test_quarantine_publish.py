from __future__ import annotations

from datetime import UTC, datetime
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ci_shepherd.jsonl import append_jsonl_rows, read_jsonl_rows
from ci_shepherd.quarantine import (
    read_quarantine_session_events,
    record_quarantine_session_event,
)
from ci_shepherd.quarantine_authorization import (
    AuthorizedQuarantinePublication,
)
from ci_shepherd.quarantine_publish import (
    _require_started_session,
    _validate_pull_request_summary,
    publish_quarantine_pull_request,
)
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
            state = root / "state"
            record_quarantine_session_event(
                state,
                self._request(),
                status="started",
                recorded_at="2026-08-31T03:00:00Z",
                session_id="session-1",
                authorization_grant_id="quarantine-grant:1",
                authorization_expires_at="2099-08-31T03:18:42Z",
                checkout=checkout,
            )

            def run(
                command: list[str],
                **_: object,
            ) -> subprocess.CompletedProcess:
                nonlocal remote_reads
                commands.append(command)
                if "config" in command:
                    return subprocess.CompletedProcess(command, 1, "", "")
                if command[-1] == "remote":
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "radical\nrf\n",
                        "",
                    )
                if "get-url" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "git@github.com:radical/aspire.git\n",
                        "",
                    )
                if "ls-remote" in command:
                    if command[-1] == "refs/heads/main":
                        return subprocess.CompletedProcess(
                            command,
                            0,
                            f"{'a' * 40}\trefs/heads/main\n",
                            "",
                        )
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
                    fields = command[command.index("--json") + 1].split(",")
                    self.assertIn("state", fields)
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        json.dumps(
                            {
                                "url": pull_request_url,
                                "state": "OPEN",
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
            ):
                result = publish_quarantine_pull_request(
                    request=self._request(),
                    mutation_result=self._mutation_result(),
                    commit_validation=commit,
                    checkout=checkout,
                    state_directory=state,
                    session_id="session-1",
                    body_file=body,
                    audit_path=audit,
                    authorization=self._authorization(),
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
            self.assertIn(
                f"--force-with-lease=refs/heads/{branch}:",
                push,
            )
            self.assertIn("git@github.com:radical/aspire.git", push)
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
            self.assertEqual(
                ["started", "publication-pending", "pull-request-open"],
                [
                    event["status"]
                    for event in read_quarantine_session_events(state)
                ],
            )
            self.assertEqual(
                "quarantine-publication-grant:1",
                read_quarantine_session_events(state)[1][
                    "publicationAuthorizationGrantId"
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
                    authorization=self._authorization(
                        self._request(repository="microsoft/aspire")
                    ),
                    runner=run,
                )

            self.assertFalse(audit.exists())
            self.assertEqual(commands, [])

    def test_refuses_the_production_push_target_before_running_commands(self) -> None:
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
                    request=self._request(head_repository="microsoft/aspire"),
                    mutation_result=self._mutation_result(),
                    commit_validation=self._commit_validation(),
                    checkout=checkout,
                    state_directory=root / "state",
                    session_id="session-1",
                    body_file=body,
                    audit_path=audit,
                    authorization=self._authorization(
                        self._request(head_repository="microsoft/aspire")
                    ),
                    runner=run,
                )

            self.assertFalse(audit.exists())
            self.assertEqual(commands, [])

    def test_pending_publication_reuses_the_authorized_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout, _, _ = self._create_paths(root)
            state = root / "state"
            request = self._request()
            mutation = self._mutation_result()
            record_quarantine_session_event(
                state,
                request,
                status="started",
                recorded_at="2026-08-31T03:00:00Z",
                session_id="session-1",
                authorization_grant_id="quarantine-grant:1",
                authorization_expires_at="2099-08-31T03:18:42Z",
                checkout=checkout,
            )
            record_quarantine_session_event(
                state,
                request,
                status="publication-pending",
                recorded_at="2026-08-31T03:01:00Z",
                session_id="session-1",
                pull_request_head_sha="d" * 40,
                completed_test_names=["Tests.Flaky"],
                mutation_validation=mutation,
            )

            _require_started_session(
                request,
                state,
                session_id="session-1",
                checkout=checkout,
            )

            other_checkout = root / "other-checkout"
            other_checkout.mkdir()
            with self.assertRaisesRegex(ValueError, "authorized checkout"):
                _require_started_session(
                    request,
                    state,
                    session_id="session-1",
                    checkout=other_checkout,
                )

    def test_refuses_a_remote_that_does_not_match_the_allowed_fork(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout, body, audit = self._create_paths(root)

            def run(
                command: list[str],
                **_: object,
            ) -> subprocess.CompletedProcess:
                if "config" in command:
                    return subprocess.CompletedProcess(command, 1, "", "")
                if command[-1] == "remote":
                    return subprocess.CompletedProcess(command, 0, "origin\n", "")
                if "get-url" in command:
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
                    return_value=[self._started_event(checkout)],
                ),
                patch(
                    "ci_shepherd.quarantine_publish.record_quarantine_session_event",
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
                        authorization=self._authorization(),
                        runner=run,
                    )

            self.assertFalse(audit.exists())

    def test_refuses_a_remote_whose_push_url_targets_production(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout, body, audit = self._create_paths(root)
            commands: list[list[str]] = []

            def run(
                command: list[str],
                **_: object,
            ) -> subprocess.CompletedProcess:
                commands.append(command)
                if "config" in command:
                    return subprocess.CompletedProcess(command, 1, "", "")
                if command[-1] == "remote":
                    return subprocess.CompletedProcess(command, 0, "radical\n", "")
                if "get-url" in command:
                    url = (
                        "https://github.com/microsoft/aspire.git\n"
                        if "--push" in command
                        else "https://github.com/radical/aspire.git\n"
                    )
                    return subprocess.CompletedProcess(command, 0, url, "")
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
                    return_value=[self._started_event(checkout)],
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
                        authorization=self._authorization(),
                        runner=run,
                    )

            self.assertFalse(audit.exists())
            self.assertFalse(any("push" in command for command in commands))

    def test_refuses_git_url_rewrite_configuration_before_remote_access(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout, body, audit = self._create_paths(root)
            commands: list[list[str]] = []

            def run(
                command: list[str],
                **_: object,
            ) -> subprocess.CompletedProcess:
                commands.append(command)
                if "config" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        (
                            "url.https://github.com/microsoft/.pushinsteadof "
                            "https://github.com/radical/\n"
                        ),
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
                    return_value=[self._started_event(checkout)],
                ),
            ):
                with self.assertRaisesRegex(ValueError, "rewrite configuration"):
                    publish_quarantine_pull_request(
                        request=self._request(),
                        mutation_result=self._mutation_result(),
                        commit_validation=self._commit_validation(),
                        checkout=checkout,
                        state_directory=root / "state",
                        session_id="session-1",
                        body_file=body,
                        audit_path=audit,
                        authorization=self._authorization(),
                        runner=run,
                    )

            self.assertEqual(1, len(commands))
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
                    return_value=[self._started_event(checkout)],
                ),
                patch(
                    "ci_shepherd.quarantine_publish.record_quarantine_session_event",
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
                                authorization=self._authorization(),
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
                **self._started_event(checkout),
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
                        authorization=self._authorization(),
                        runner=lambda *_args, **_kwargs: self.fail(
                            "No command should run for a mismatched session."
                        ),
                    )

            self.assertFalse(audit.exists())

    def test_refuses_publication_after_authorization_expires(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout, body, audit = self._create_paths(root)
            expired = {
                **self._started_event(checkout),
                "authorizationExpiresAt": "2026-08-31T03:18:42Z",
            }
            commands: list[list[str]] = []

            def run(
                command: list[str],
                **_: object,
            ) -> subprocess.CompletedProcess:
                commands.append(command)
                if "config" in command:
                    return subprocess.CompletedProcess(command, 1, "", "")
                if command[-1] == "remote":
                    return subprocess.CompletedProcess(command, 0, "fork\n", "")
                if "get-url" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "https://github.com/radical/aspire.git\n",
                        "",
                    )
                if "ls-remote" in command:
                    output = (
                        f"{'a' * 40}\trefs/heads/main\n"
                        if command[-1] == "refs/heads/main"
                        else ""
                    )
                    return subprocess.CompletedProcess(command, 0, output, "")
                if command[:3] == ["gh", "pr", "list"]:
                    return subprocess.CompletedProcess(command, 0, "[]", "")
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
                    return_value=[expired],
                ),
            ):
                with self.assertRaisesRegex(ValueError, "authorization expired"):
                    publish_quarantine_pull_request(
                        request=self._request(),
                        mutation_result=self._mutation_result(),
                        commit_validation=self._commit_validation(),
                        checkout=checkout,
                        state_directory=root / "state",
                        session_id="session-1",
                        body_file=body,
                        audit_path=audit,
                        authorization=self._authorization(
                            expires_at="2026-08-31T03:18:42Z"
                        ),
                        runner=run,
                        now=datetime(2026, 8, 31, 3, 18, 42, tzinfo=UTC),
                    )

            self.assertFalse(audit.exists())
            self.assertFalse(any("push" in command for command in commands))
            self.assertFalse(
                any(
                    command[:3] == ["gh", "pr", "create"]
                    for command in commands
                )
            )

    def test_rechecks_authorization_expiry_immediately_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout, body, audit = self._create_paths(root)
            commit = self._commit_validation()
            started = {
                **self._started_event(checkout),
                "authorizationExpiresAt": "2026-08-31T04:00:00Z",
            }
            commands: list[list[str]] = []
            times = iter(
                [
                    datetime(2026, 8, 31, 3, 59, tzinfo=UTC),
                    datetime(2026, 8, 31, 4, 1, tzinfo=UTC),
                ]
            )

            def run(
                command: list[str],
                **_: object,
            ) -> subprocess.CompletedProcess:
                commands.append(command)
                if "config" in command:
                    return subprocess.CompletedProcess(command, 1, "", "")
                if command[-1] == "remote":
                    return subprocess.CompletedProcess(command, 0, "fork\n", "")
                if "get-url" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "https://github.com/radical/aspire.git\n",
                        "",
                    )
                if "ls-remote" in command:
                    output = (
                        f"{'a' * 40}\trefs/heads/main\n"
                        if command[-1] == "refs/heads/main"
                        else ""
                    )
                    return subprocess.CompletedProcess(command, 0, output, "")
                if command[:3] == ["gh", "pr", "list"]:
                    return subprocess.CompletedProcess(command, 0, "[]", "")
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
                    return_value=[started],
                ),
            ):
                with self.assertRaisesRegex(ValueError, "authorization expired"):
                    publish_quarantine_pull_request(
                        request=self._request(),
                        mutation_result=self._mutation_result(),
                        commit_validation=commit,
                        checkout=checkout,
                        state_directory=root / "state",
                        session_id="session-1",
                        body_file=body,
                        audit_path=audit,
                        authorization=self._authorization(
                            issued_at="2026-08-31T03:00:00Z",
                            expires_at="2026-08-31T04:00:00Z",
                        ),
                        runner=run,
                        clock=lambda: next(times),
                    )

            self.assertFalse(audit.exists())
            self.assertFalse(any("push" in command for command in commands))

    def test_rechecks_authorization_after_push_before_creating_pull_request(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout, body, audit = self._create_paths(root)
            commit = self._commit_validation()
            commands: list[list[str]] = []
            remote_reads = 0
            times = iter(
                [
                    datetime(2026, 8, 31, 3, 50, tzinfo=UTC),
                    datetime(2026, 8, 31, 3, 51, tzinfo=UTC),
                    datetime(2026, 8, 31, 3, 52, tzinfo=UTC),
                    datetime(2026, 8, 31, 4, 1, tzinfo=UTC),
                ]
            )

            def run(
                command: list[str],
                **_: object,
            ) -> subprocess.CompletedProcess:
                nonlocal remote_reads
                commands.append(command)
                if "config" in command:
                    return subprocess.CompletedProcess(command, 1, "", "")
                if command[-1] == "remote":
                    return subprocess.CompletedProcess(command, 0, "radical\n", "")
                if "get-url" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "https://github.com/radical/aspire.git\n",
                        "",
                    )
                if "ls-remote" in command:
                    if command[-1] == "refs/heads/main":
                        return subprocess.CompletedProcess(
                            command,
                            0,
                            f"{'a' * 40}\trefs/heads/main\n",
                            "",
                        )
                    remote_reads += 1
                    output = (
                        ""
                        if remote_reads == 1
                        else (
                            f"{commit['commitSha']}\trefs/heads/"
                            "ci-shepherd/quarantine-0123456789abcdef\n"
                        )
                    )
                    return subprocess.CompletedProcess(command, 0, output, "")
                if command[:3] == ["gh", "pr", "list"]:
                    return subprocess.CompletedProcess(command, 0, "[]", "")
                if "push" in command:
                    return subprocess.CompletedProcess(command, 0, "", "")
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
                    return_value=[self._started_event(checkout)],
                ),
                patch(
                    "ci_shepherd.quarantine_publish.record_quarantine_session_event",
                ),
            ):
                with self.assertRaisesRegex(ValueError, "authorization expired"):
                    publish_quarantine_pull_request(
                        request=self._request(),
                        mutation_result=self._mutation_result(),
                        commit_validation=commit,
                        checkout=checkout,
                        state_directory=root / "state",
                        session_id="session-1",
                        body_file=body,
                        audit_path=audit,
                        authorization=self._authorization(
                            issued_at="2026-08-31T03:00:00Z",
                            expires_at="2026-08-31T04:00:00Z",
                        ),
                        runner=run,
                        clock=lambda: next(times),
                    )

            self.assertTrue(any("push" in command for command in commands))
            self.assertFalse(
                any(
                    command[:3] == ["gh", "pr", "create"]
                    for command in commands
                )
            )
            self.assertEqual(
                [
                    ("push-branch", "intent"),
                    ("push-branch", "outcome"),
                ],
                [
                    (row["operation"], row["phase"])
                    for row in read_jsonl_rows(audit)
                ],
            )

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
                if "config" in command:
                    return subprocess.CompletedProcess(command, 1, "", "")
                if command[-1] == "remote":
                    return subprocess.CompletedProcess(command, 0, "fork\n", "")
                if "get-url" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "https://github.com/radical/aspire.git\n",
                        "",
                    )
                if "ls-remote" in command:
                    output = (
                        f"{'a' * 40}\trefs/heads/main\n"
                        if command[-1] == "refs/heads/main"
                        else ""
                    )
                    return subprocess.CompletedProcess(command, 0, output, "")
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
                    return_value=[self._started_event(checkout)],
                ),
                patch(
                    "ci_shepherd.quarantine_publish.record_quarantine_session_event",
                ) as record_session_event,
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
                        authorization=self._authorization(),
                        runner=run,
                    )

            self.assertFalse(audit.exists())
            self.assertFalse(any("push" in command for command in commands))
            record_session_event.assert_not_called()

    def test_closed_existing_pull_request_cannot_be_republished(self) -> None:
        with self.assertRaisesRegex(ValueError, "publication policy"):
            _validate_pull_request_summary(
                {
                    "url": "https://github.com/radical/aspire/pull/2",
                    "state": "CLOSED",
                    "headRefOid": "d" * 40,
                    "isDraft": True,
                    "baseRefName": "main",
                    "headRepository": {"nameWithOwner": "radical/aspire"},
                },
                repository="radical/aspire",
                head_repository="radical/aspire",
                base_ref="main",
                commit_sha="d" * 40,
            )

    def test_failed_atomic_append_preserves_the_previous_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "ledger.jsonl"
            append_jsonl_rows(path, [{"sequence": 1}])

            with (
                patch(
                    "ci_shepherd.jsonl.os.replace",
                    side_effect=OSError("simulated replace failure"),
                ),
                self.assertRaisesRegex(OSError, "simulated replace failure"),
            ):
                append_jsonl_rows(path, [{"sequence": 2}])

            self.assertEqual([{"sequence": 1}], read_jsonl_rows(path))

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
                if "config" in command:
                    return subprocess.CompletedProcess(command, 1, "", "")
                if command[-1] == "remote":
                    return subprocess.CompletedProcess(command, 0, "fork\n", "")
                if "get-url" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "https://github.com/radical/aspire.git\n",
                        "",
                    )
                if "ls-remote" in command:
                    if command[-1] == "refs/heads/main":
                        return subprocess.CompletedProcess(
                            command,
                            0,
                            f"{'a' * 40}\trefs/heads/main\n",
                            "",
                        )
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
                    return_value=[self._started_event(checkout)],
                ),
                patch(
                    "ci_shepherd.quarantine_publish.record_quarantine_session_event",
                ) as record_session_event,
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
                        authorization=self._authorization(),
                        runner=run,
                    )

            self.assertFalse(audit.exists())
            self.assertFalse(any("push" in command for command in commands))
            record_session_event.assert_not_called()

    def test_refuses_a_base_ref_that_differs_from_the_validated_source(self) -> None:
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
                if "config" in command:
                    return subprocess.CompletedProcess(command, 1, "", "")
                if command[-1] == "remote":
                    return subprocess.CompletedProcess(command, 0, "fork\n", "")
                if "get-url" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "https://github.com/radical/aspire.git\n",
                        "",
                    )
                if "ls-remote" in command:
                    output = ""
                    if command[-1] == "refs/heads/main":
                        output = f"{'e' * 40}\trefs/heads/main\n"
                    return subprocess.CompletedProcess(command, 0, output, "")
                if command[:3] == ["gh", "pr", "list"]:
                    return subprocess.CompletedProcess(command, 0, "[]", "")
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
                    return_value=[self._started_event(checkout)],
                ),
                patch(
                    "ci_shepherd.quarantine_publish.record_quarantine_session_event",
                ) as record_session_event,
            ):
                with self.assertRaisesRegex(ValueError, "base ref"):
                    publish_quarantine_pull_request(
                        request=self._request(),
                        mutation_result=self._mutation_result(),
                        commit_validation=commit,
                        checkout=checkout,
                        state_directory=root / "state",
                        session_id="session-1",
                        body_file=body,
                        audit_path=audit,
                        authorization=self._authorization(),
                        runner=run,
                    )

            self.assertFalse(audit.exists())
            self.assertFalse(any("push" in command for command in commands))
            record_session_event.assert_not_called()

    def test_push_crash_leaves_an_unmatched_intent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout, body, audit = self._create_paths(root)
            commit = self._commit_validation()

            def run(
                command: list[str],
                **_: object,
            ) -> subprocess.CompletedProcess:
                if "config" in command:
                    return subprocess.CompletedProcess(command, 1, "", "")
                if command[-1] == "remote":
                    return subprocess.CompletedProcess(command, 0, "fork\n", "")
                if "get-url" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "https://github.com/radical/aspire.git\n",
                        "",
                    )
                if "ls-remote" in command:
                    output = (
                        f"{'a' * 40}\trefs/heads/main\n"
                        if command[-1] == "refs/heads/main"
                        else ""
                    )
                    return subprocess.CompletedProcess(command, 0, output, "")
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
                    return_value=[self._started_event(checkout)],
                ),
                patch(
                    "ci_shepherd.quarantine_publish.record_quarantine_session_event",
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
                        authorization=self._authorization(),
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
            started = {
                **self._started_event(checkout),
                "authorizationExpiresAt": "2026-08-31T04:00:00Z",
            }
            pending = {
                **self._request(),
                "status": "publication-pending",
                "sessionId": "session-1",
                "pullRequestHeadSha": commit["commitSha"],
                "mutationValidation": self._mutation_result(),
            }

            def run(
                command: list[str],
                **_: object,
            ) -> subprocess.CompletedProcess:
                commands.append(command)
                if "config" in command:
                    return subprocess.CompletedProcess(command, 1, "", "")
                if command[-1] == "remote":
                    return subprocess.CompletedProcess(command, 0, "fork\n", "")
                if "get-url" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "https://github.com/radical/aspire.git\n",
                        "",
                    )
                if "ls-remote" in command:
                    if command[-1] == "refs/heads/main":
                        return subprocess.CompletedProcess(
                            command,
                            0,
                            f"{'a' * 40}\trefs/heads/main\n",
                            "",
                        )
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
                                "state": "OPEN",
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
                    return_value=[started, pending],
                ),
                patch(
                    "ci_shepherd.quarantine_publish.record_quarantine_session_event",
                ) as record_session_event,
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
                    authorization=self._authorization(
                        issued_at="2026-08-31T04:30:00Z",
                        expires_at="2026-08-31T05:30:00Z",
                    ),
                    runner=run,
                    now=datetime(2026, 8, 31, 5, 0, tzinfo=UTC),
                )

            self.assertEqual(result["pullRequest"]["url"], pull_request_url)
            self.assertFalse(any("push" in command for command in commands))
            self.assertEqual(
                [
                    "publication-pending",
                    "pull-request-open",
                ],
                [
                    call.kwargs["status"]
                    for call in record_session_event.call_args_list
                ],
            )
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
                "state": "OPEN",
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
                if "config" in command:
                    return subprocess.CompletedProcess(command, 1, "", "")
                if command[-1] == "remote":
                    return subprocess.CompletedProcess(command, 0, "fork\n", "")
                if "get-url" in command:
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
                    return_value=[self._started_event(checkout)],
                ),
                patch(
                    "ci_shepherd.quarantine_publish.record_quarantine_session_event",
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
                    authorization=self._authorization(),
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

    def test_successful_publication_replay_returns_the_existing_pull_request(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout, body, audit = self._create_paths(root)
            state = root / "state"
            request = self._request()
            mutation = self._mutation_result()
            commit = self._commit_validation()
            pull_request_url = "https://github.com/radical/aspire/pull/2"
            record_quarantine_session_event(
                state,
                request,
                status="started",
                recorded_at="2026-08-31T03:00:00Z",
                session_id="session-1",
                authorization_grant_id="quarantine-grant:1",
                authorization_expires_at="2026-08-31T04:00:00Z",
                checkout=checkout,
            )
            record_quarantine_session_event(
                state,
                request,
                status="pull-request-open",
                recorded_at="2026-08-31T03:10:00Z",
                session_id="session-1",
                pull_request_url=pull_request_url,
                pull_request_head_sha=commit["commitSha"],
                completed_test_names=["Tests.Flaky"],
                mutation_validation=mutation,
            )
            event_count = len(read_quarantine_session_events(state))

            def run(
                command: list[str],
                **_: object,
            ) -> subprocess.CompletedProcess:
                if "config" in command:
                    return subprocess.CompletedProcess(command, 1, "", "")
                if command[-1] == "remote":
                    return subprocess.CompletedProcess(command, 0, "fork\n", "")
                if "get-url" in command:
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
                        json.dumps(
                            [
                                {
                                    "url": pull_request_url,
                                    "state": "OPEN",
                                    "headRefOid": commit["commitSha"],
                                    "isDraft": True,
                                    "baseRefName": "main",
                                    "headRepository": {
                                        "nameWithOwner": "radical/aspire"
                                    },
                                }
                            ]
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
            ):
                result = publish_quarantine_pull_request(
                    request=request,
                    mutation_result=mutation,
                    commit_validation=commit,
                    checkout=checkout,
                    state_directory=state,
                    session_id="session-1",
                    body_file=body,
                    audit_path=audit,
                    authorization=self._authorization(
                        request,
                        issued_at="2026-08-31T03:00:00Z",
                        expires_at="2026-08-31T04:00:00Z",
                    ),
                    runner=run,
                    now=datetime(2026, 8, 31, 5, 0, tzinfo=UTC),
                )

            self.assertEqual(pull_request_url, result["pullRequest"]["url"])
            self.assertEqual(
                event_count,
                len(read_quarantine_session_events(state)),
            )
            self.assertFalse(audit.exists())

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
        head_repository: str = "radical/aspire",
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
                    "allowedHeadRepositories": [head_repository],
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
    def _authorization(
        cls,
        request: dict[str, object] | None = None,
        *,
        issued_at: str = "2026-08-31T03:00:00Z",
        expires_at: str = "2099-08-31T04:00:00Z",
    ) -> AuthorizedQuarantinePublication:
        return AuthorizedQuarantinePublication(
            request=request or cls._request(),
            grant_id="quarantine-publication-grant:1",
            issued_at=issued_at,
            expires_at=expires_at,
        )

    @classmethod
    def _started_event(cls, checkout: Path) -> dict[str, object]:
        return {
            **cls._request(),
            "status": "started",
            "sessionId": "session-1",
            "authorizationGrantId": "quarantine-grant:1",
            "authorizationExpiresAt": "2099-08-31T03:18:42Z",
            "checkoutPath": str(checkout.resolve()),
        }


if __name__ == "__main__":
    unittest.main()
