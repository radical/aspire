from __future__ import annotations

import json
import subprocess
import shutil
import unittest
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from ci_shepherd.github import GitHubApiError, GitHubClient


def build_response(
    status: int,
    body: object,
    *,
    headers: dict[str, str] | None = None,
) -> str:
    lines = [f"HTTP/2 {status}"]
    lines.extend(f"{name}: {value}" for name, value in (headers or {}).items())
    payload = body if isinstance(body, str) else json.dumps(body)
    return "\r\n".join(lines) + "\r\n\r\n" + payload


@dataclass
class FakeCompletedProcess:
    returncode: int
    stdout: str
    stderr: str = ""


class FakeRunner:
    def __init__(self, responses: list[FakeCompletedProcess]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, command: list[str], **kwargs: object) -> FakeCompletedProcess:
        expected_keys = {"env", "stdout", "stderr", "check", "text"}
        if set(kwargs) != expected_keys:
            raise AssertionError(
                f"Unexpected runner kwargs: {sorted(kwargs)}; expected {sorted(expected_keys)}"
            )

        env = kwargs["env"]
        if not isinstance(env, dict):
            raise AssertionError("runner env must be a dictionary")
        if kwargs["stdout"] is not subprocess.PIPE:
            raise AssertionError("runner stdout must be subprocess.PIPE")
        if kwargs["stderr"] is not subprocess.PIPE:
            raise AssertionError("runner stderr must be subprocess.PIPE")
        if kwargs["check"] is not False:
            raise AssertionError("runner check must be False")
        if kwargs["text"] is not True:
            raise AssertionError("runner text must be True")

        recorded_kwargs = dict(kwargs)
        recorded_kwargs["env"] = dict(env)
        self.calls.append((list(command), recorded_kwargs))
        if not self._responses:
            raise AssertionError("No fake runner response remaining.")
        return self._responses.pop(0)


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class FakeClock:
    def __init__(self, *values: float) -> None:
        self._values = list(values) or [0.0]

    def __call__(self) -> float:
        if len(self._values) == 1:
            return self._values[0]
        return self._values.pop(0)


class FakeProcess:
    def __init__(
        self,
        stdout: bytes,
        *,
        returncode: int = 0,
        stderr: bytes = b"",
    ) -> None:
        self.stdout = BytesIO(stdout)
        self.stderr = BytesIO(stderr)
        self.stderr_payload = stderr
        self.returncode = returncode
        self.terminated = False
        self.wait_calls = 0

    def terminate(self) -> None:
        self.terminated = True

    def wait(self) -> int:
        self.wait_calls += 1
        return self.returncode


class FakePopenFactory:
    def __init__(self, processes: list[FakeProcess]) -> None:
        self._processes = list(processes)
        self.calls: list[tuple[list[str], dict[str, str], object, object]] = []

    def __call__(self, command: list[str], *, env: dict[str, str], stdout: object, stderr: object) -> FakeProcess:
        self.calls.append((list(command), dict(env), stdout, stderr))
        if not self._processes:
            raise AssertionError("No fake process remaining.")
        process = self._processes.pop(0)
        if stderr is not subprocess.PIPE and hasattr(stderr, "write"):
            stderr.write(process.stderr_payload)
            stderr.flush()
        return process


class GitHubClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.artifact_dir = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(self.artifact_dir, ignore_errors=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.artifact_dir, ignore_errors=True)

    def make_client(
        self,
        runner: FakeRunner,
        *,
        popen_factory: FakePopenFactory | None = None,
        sleep: FakeSleep | None = None,
        now: FakeClock | None = None,
        max_attempts: int = 3,
        max_retry_delay: int = 60,
        audit_path: Path | None = None,
    ) -> GitHubClient:
        return GitHubClient(
            runner=runner,
            popen_factory=popen_factory or FakePopenFactory([]),
            sleep=sleep or FakeSleep(),
            now=now or FakeClock(0.0),
            max_attempts=max_attempts,
            max_retry_delay=max_retry_delay,
            audit_path=audit_path,
        )

    def test_get_pages_collects_top_level_arrays_until_short_page(self) -> None:
        runner = FakeRunner(
            [
                FakeCompletedProcess(0, build_response(200, list(range(100)))),
                FakeCompletedProcess(0, build_response(200, [100])),
            ]
        )
        client = self.make_client(runner)

        result = client.get_pages("/repos/owner/repo/actions/runs")

        self.assertEqual(list(range(101)), result)
        called_endpoints = [command[-1] for command, _ in runner.calls]
        self.assertEqual(
            [
                "/repos/owner/repo/actions/runs?per_page=100&page=1",
                "/repos/owner/repo/actions/runs?per_page=100&page=2",
            ],
            called_endpoints,
        )

    def test_get_pages_collects_keyed_jobs_across_multiple_pages(self) -> None:
        runner = FakeRunner(
            [
                FakeCompletedProcess(0, build_response(200, {"jobs": [{"id": job_id} for job_id in range(100)]})),
                FakeCompletedProcess(0, build_response(200, {"jobs": [{"id": job_id} for job_id in range(100, 200)]})),
                FakeCompletedProcess(0, build_response(200, {"jobs": [{"id": 200}]})),
            ]
        )
        client = self.make_client(runner)

        result = client.get_pages("/repos/owner/repo/actions/runs/5/jobs", key="jobs")

        self.assertEqual(201, len(result))
        self.assertEqual(0, result[0]["id"])
        self.assertEqual(100, result[100]["id"])
        self.assertEqual(200, result[-1]["id"])

    def test_get_pages_preserves_existing_query_parameters_without_duplication(self) -> None:
        runner = FakeRunner(
            [
                FakeCompletedProcess(0, build_response(200, list(range(100)))),
                FakeCompletedProcess(0, build_response(200, [])),
            ]
        )
        client = self.make_client(runner)

        client.get_pages("/repos/owner/repo/actions/runs?branch=main&page=7&per_page=50&status=completed")

        first_endpoint = runner.calls[0][0][-1]
        parsed_query = parse_qsl(urlsplit(first_endpoint).query, keep_blank_values=True)
        self.assertEqual(
            [("branch", "main"), ("status", "completed"), ("per_page", "100"), ("page", "1")],
            parsed_query,
        )

    def test_request_passes_subprocess_run_keyword_arguments(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []

        def runner(
            command: list[str],
            *,
            env: dict[str, str],
            stdout: object,
            stderr: object,
            check: bool,
            text: bool,
        ) -> FakeCompletedProcess:
            calls.append(
                (
                    list(command),
                    {
                        "env": dict(env),
                        "stdout": stdout,
                        "stderr": stderr,
                        "check": check,
                        "text": text,
                    },
                )
            )
            return FakeCompletedProcess(0, build_response(200, {"ok": True}))

        client = GitHubClient(
            runner=runner,
            popen_factory=FakePopenFactory([]),
            sleep=FakeSleep(),
            now=FakeClock(0.0),
        )

        self.assertEqual({"ok": True}, client.get("/repos/owner/repo/actions/runs/123"))
        self.assertEqual(1, len(calls))
        command, kwargs = calls[0]
        self.assertEqual("/repos/owner/repo/actions/runs/123", command[-1])
        self.assertEqual("cat", kwargs["env"]["GH_PAGER"])
        self.assertEqual({"env", "stdout", "stderr", "check", "text"}, set(kwargs))
        self.assertIs(subprocess.PIPE, kwargs["stdout"])
        self.assertIs(subprocess.PIPE, kwargs["stderr"])
        self.assertIs(False, kwargs["check"])
        self.assertIs(True, kwargs["text"])

    def test_get_retries_transient_server_errors_with_backoff(self) -> None:
        runner = FakeRunner(
            [
                FakeCompletedProcess(1, build_response(502, {"message": "bad gateway"})),
                FakeCompletedProcess(1, build_response(503, {"message": "service unavailable"})),
                FakeCompletedProcess(0, build_response(200, {"ok": True})),
            ]
        )
        sleep = FakeSleep()
        client = self.make_client(runner, sleep=sleep)

        result = client.get("/repos/owner/repo/actions/runs/123")

        self.assertEqual({"ok": True}, result)
        self.assertEqual([1, 2], sleep.calls)

    def test_get_does_not_retry_auth_or_authorization_errors(self) -> None:
        cases = [
            (401, {"message": "Bad credentials"}, "auth"),
            (403, {"message": "Resource not accessible by integration"}, "authorization"),
        ]

        for status, body, category in cases:
            with self.subTest(status=status, category=category):
                runner = FakeRunner([FakeCompletedProcess(1, build_response(status, body))])
                sleep = FakeSleep()
                client = self.make_client(runner, sleep=sleep)

                with self.assertRaises(GitHubApiError) as context:
                    client.get("/repos/owner/repo/actions/runs/123")

                self.assertEqual(category, context.exception.category)
                self.assertEqual(1, context.exception.attempts)
                self.assertEqual([], sleep.calls)

    def test_get_retries_secondary_rate_limit_using_retry_after(self) -> None:
        runner = FakeRunner(
            [
                FakeCompletedProcess(
                    1,
                    build_response(
                        403,
                        {"message": "You have exceeded a secondary rate limit"},
                        headers={"retry-after": "17"},
                    ),
                ),
                FakeCompletedProcess(0, build_response(200, {"ok": True})),
            ]
        )
        sleep = FakeSleep()
        client = self.make_client(runner, sleep=sleep)

        result = client.get("/repos/owner/repo/actions/runs/123")

        self.assertEqual({"ok": True}, result)
        self.assertEqual([17], sleep.calls)

    def test_get_retries_primary_rate_limit_until_reset_when_bounded(self) -> None:
        runner = FakeRunner(
            [
                FakeCompletedProcess(
                    1,
                    build_response(
                        403,
                        {"message": "API rate limit exceeded"},
                        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "110"},
                    ),
                ),
                FakeCompletedProcess(0, build_response(200, {"ok": True})),
            ]
        )
        sleep = FakeSleep()
        client = self.make_client(runner, sleep=sleep, now=FakeClock(100))

        result = client.get("/repos/owner/repo/actions/runs/123")

        self.assertEqual({"ok": True}, result)
        self.assertEqual([10], sleep.calls)

    def test_get_raises_rate_limit_exhausted_when_primary_reset_exceeds_max_delay(self) -> None:
        runner = FakeRunner(
            [
                FakeCompletedProcess(
                    1,
                    build_response(
                        403,
                        {"message": "API rate limit exceeded"},
                        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "200"},
                    ),
                )
            ]
        )
        client = self.make_client(runner, now=FakeClock(100), max_retry_delay=60)

        with self.assertRaises(GitHubApiError) as context:
            client.get("/repos/owner/repo/actions/runs/123")

        self.assertEqual("rate-limit-exhausted", context.exception.category)
        self.assertEqual(1, context.exception.attempts)

    def test_get_raises_malformed_json_when_body_is_invalid(self) -> None:
        runner = FakeRunner([FakeCompletedProcess(0, build_response(200, "{"))])
        client = self.make_client(runner)

        with self.assertRaises(GitHubApiError) as context:
            client.get("/repos/owner/repo/actions/runs/123")

        self.assertEqual("malformed-json", context.exception.category)

    def test_get_distinguishes_not_found_and_expired_errors(self) -> None:
        cases = [
            (FakeCompletedProcess(1, build_response(404, {"message": "Not Found"})), "not-found"),
            (FakeCompletedProcess(1, build_response(401, {"message": "Token expired"})), "expired"),
        ]

        for response, category in cases:
            with self.subTest(category=category):
                runner = FakeRunner([response])
                client = self.make_client(runner)

                with self.assertRaises(GitHubApiError) as context:
                    client.get("/repos/owner/repo/actions/runs/123")

                self.assertEqual(category, context.exception.category)

    def test_get_forces_read_only_command_and_private_audit_log(self) -> None:
        audit_path = self.artifact_dir / "private" / "audit.jsonl"
        runner = FakeRunner(
            [
                FakeCompletedProcess(
                    1,
                    build_response(401, {"message": "Bad credentials"}, headers={"x-oauth-scopes": "repo"}),
                    stderr="token github_pat_secretvalue should never reach the audit log",
                )
            ]
        )
        client = self.make_client(runner, audit_path=audit_path)

        with self.assertRaises(GitHubApiError):
            client.get("/repos/owner/repo/actions/runs/123?branch=main")

        command, kwargs = runner.calls[0]
        env = kwargs["env"]
        self.assertEqual(
            [
                "gh",
                "api",
                "--method",
                "GET",
                "--hostname",
                "github.com",
                "--include",
                "-H",
                "Accept: application/vnd.github+json",
                "-H",
                "X-GitHub-Api-Version: 2022-11-28",
                "/repos/owner/repo/actions/runs/123?branch=main",
            ],
            command,
        )
        self.assertEqual("cat", env["GH_PAGER"])
        self.assertNotIn("--input", command)
        self.assertNotIn("--field", command)
        self.assertNotIn("--raw-field", command)
        self.assertNotIn("-F", command)
        self.assertNotIn("-f", command)
        self.assertIs(subprocess.PIPE, kwargs["stdout"])
        self.assertIs(subprocess.PIPE, kwargs["stderr"])
        self.assertIs(False, kwargs["check"])
        self.assertIs(True, kwargs["text"])

        audit_text = audit_path.read_text(encoding="utf-8")
        self.assertNotIn("github_pat_secretvalue", audit_text)
        record = json.loads(audit_text.strip())
        self.assertEqual(
            {
                "attemptedAt": record["attemptedAt"],
                "endpoint": "/repos/owner/repo/actions/runs/123?branch=main",
                "method": "GET",
                "status": 401,
            },
            record,
        )
        self.assertEqual(0o600, audit_path.stat().st_mode & 0o777)

    def test_get_text_truncates_large_responses_and_terminates_child(self) -> None:
        stdout = build_response(200, "x" * 120, headers={"content-type": "text/plain"}).encode("utf-8")
        process = FakeProcess(stdout)
        runner = FakeRunner([])
        popen_factory = FakePopenFactory([process])
        client = self.make_client(runner, popen_factory=popen_factory)

        result = client.get_text("/repos/owner/repo/actions/runs/123/logs", max_bytes=100)

        self.assertTrue(result.truncated)
        self.assertTrue(process.terminated)
        self.assertEqual(1, process.wait_calls)
        self.assertEqual(200, result.status)
        self.assertEqual("text/plain", result.headers["content-type"])
        self.assertTrue(result.text.startswith("x"))

    def test_get_text_returns_final_body_and_headers_without_truncation(self) -> None:
        stdout = build_response(
            200,
            "final body",
            headers={"content-type": "text/plain", "x-final": "yes"},
        ).encode("utf-8")
        process = FakeProcess(stdout)
        runner = FakeRunner([])
        popen_factory = FakePopenFactory([process])
        client = self.make_client(runner, popen_factory=popen_factory)

        result = client.get_text("/repos/owner/repo/actions/runs/123/logs")

        self.assertFalse(result.truncated)
        self.assertEqual("final body", result.text)
        self.assertEqual(200, result.status)
        self.assertEqual("yes", result.headers["x-final"])
        command, env, _, _ = popen_factory.calls[0]
        self.assertEqual("cat", env["GH_PAGER"])
        self.assertEqual("--method", command[2])
        self.assertEqual("GET", command[3])

    def test_get_text_treats_http_lines_after_body_start_as_body_content(self) -> None:
        stdout = build_response(
            200,
            "HTTP/1.1 500 Internal Server Error\r\nContent-Type: text/plain\r\n\r\nstill body",
            headers={"content-type": "text/plain"},
        ).encode("utf-8")
        process = FakeProcess(stdout)
        runner = FakeRunner([])
        popen_factory = FakePopenFactory([process])
        client = self.make_client(runner, popen_factory=popen_factory)

        result = client.get_text("/repos/owner/repo/actions/runs/123/logs")

        self.assertEqual(200, result.status)
        self.assertEqual(
            "HTTP/1.1 500 Internal Server Error\r\nContent-Type: text/plain\r\n\r\nstill body",
            result.text,
        )

    def test_get_text_uses_final_response_after_redirect_header(self) -> None:
        stdout = (
            build_response(
                302,
                build_response(
                    200,
                    "final body",
                    headers={"content-type": "text/plain", "x-final": "yes"},
                ),
                headers={"location": "https://example.invalid/logs"},
            )
        ).encode("utf-8")
        process = FakeProcess(stdout)
        runner = FakeRunner([])
        popen_factory = FakePopenFactory([process])
        client = self.make_client(runner, popen_factory=popen_factory)

        result = client.get_text("/repos/owner/repo/actions/runs/123/logs")

        self.assertEqual(200, result.status)
        self.assertEqual("final body", result.text)
        self.assertEqual("yes", result.headers["x-final"])

    def test_get_text_raises_github_api_error_for_child_failures(self) -> None:
        failing_stdout = build_response(502, {"message": "bad gateway"}).encode("utf-8")
        processes = [
            FakeProcess(failing_stdout, returncode=1, stderr=b"bearer github_pat_secretvalue"),
            FakeProcess(failing_stdout, returncode=1, stderr=b"bearer github_pat_secretvalue"),
            FakeProcess(failing_stdout, returncode=1, stderr=b"bearer github_pat_secretvalue"),
        ]
        runner = FakeRunner([])
        popen_factory = FakePopenFactory(processes)
        client = self.make_client(runner, popen_factory=popen_factory)

        with self.assertRaises(GitHubApiError) as context:
            client.get_text("/repos/owner/repo/actions/runs/123/logs")

        self.assertEqual("transient", context.exception.category)
        self.assertEqual(3, context.exception.attempts)
        self.assertNotIn("github_pat_secretvalue", context.exception.sanitized_stderr)

    def test_get_text_uses_non_pipe_stderr_sink_and_captures_errors(self) -> None:
        process = FakeProcess(
            build_response(500, {"message": "bad gateway"}).encode("utf-8"),
            returncode=1,
            stderr=b"Authorization: Bearer ghp_secretvalue\nplain stderr line",
        )
        runner = FakeRunner([])
        popen_factory = FakePopenFactory([process])
        client = self.make_client(runner, popen_factory=popen_factory, max_attempts=1)

        with self.assertRaises(GitHubApiError) as context:
            client.get_text("/repos/owner/repo/actions/runs/123/logs")

        command, env, stdout_sink, stderr_sink = popen_factory.calls[0]
        self.assertEqual("cat", env["GH_PAGER"])
        self.assertEqual(subprocess.PIPE, stdout_sink)
        self.assertIsNot(subprocess.PIPE, stderr_sink)
        self.assertTrue(getattr(stderr_sink, "closed", False))
        self.assertEqual("/repos/owner/repo/actions/runs/123/logs", command[-1])
        self.assertIn("plain stderr line", context.exception.sanitized_stderr)
        self.assertNotIn("ghp_secretvalue", context.exception.sanitized_stderr)


if __name__ == "__main__":
    unittest.main()
