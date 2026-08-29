from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_HTTP_STATUS_RE = re.compile(r"(?m)^HTTP/\S+\s+(?P<status>\d{3})(?:\s+.*)?$")
_TOKEN_PATTERNS = (
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]+\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_.-]+\b"),
)


@dataclass(slots=True)
class GitHubTextResponse:
    text: str
    truncated: bool
    status: int
    headers: dict[str, str]


@dataclass(slots=True)
class _ParsedResponse:
    status: int
    headers: dict[str, str]
    body: str


class GitHubApiError(RuntimeError):
    def __init__(
        self,
        *,
        category: str,
        endpoint: str,
        status: int,
        headers: dict[str, str],
        retryable: bool,
        attempts: int,
        sanitized_stderr: str,
    ) -> None:
        message = f"GitHub API {category} error for {endpoint} (status {status})"
        super().__init__(message)
        self.category = category
        self.endpoint = endpoint
        self.status = status
        self.headers = headers
        self.retryable = retryable
        self.attempts = attempts
        self.sanitized_stderr = sanitized_stderr


class GitHubClient:
    def __init__(
        self,
        *,
        runner: Any,
        popen_factory: Any,
        sleep: Any,
        now: Any,
        max_attempts: int = 3,
        max_retry_delay: int = 60,
        audit_path: Path | str | None = None,
        request_timeout_seconds: float = 60,
        request_observer: Callable[[str], None] | None = None,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive.")
        self._runner = runner
        self._popen_factory = popen_factory
        self._sleep = sleep
        self._now = now
        self._max_attempts = max_attempts
        self._max_retry_delay = max_retry_delay
        self._audit_path = Path(audit_path) if audit_path is not None else None
        self._request_timeout_seconds = request_timeout_seconds
        self._request_observer = request_observer

    def get(self, endpoint: str) -> Any:
        parsed, attempts, stderr = self._request(endpoint)
        try:
            return json.loads(parsed.body)
        except json.JSONDecodeError as exc:
            raise GitHubApiError(
                category="malformed-json",
                endpoint=endpoint,
                status=parsed.status,
                headers=parsed.headers,
                retryable=False,
                attempts=attempts,
                sanitized_stderr=stderr,
            ) from exc

    def get_pages(self, endpoint: str, key: str | None = None) -> list[Any]:
        items: list[Any] = []
        page_number = 1

        while True:
            paged_endpoint = self._with_paging(endpoint, page_number)
            payload = self.get(paged_endpoint)
            page_items = self._extract_page_items(paged_endpoint, payload, key)
            items.extend(page_items)
            if len(page_items) < 100:
                return items
            page_number += 1

    def get_text(self, endpoint: str, max_bytes: int = 200000) -> GitHubTextResponse:
        for attempt in range(1, self._max_attempts + 1):
            self._notify_request(endpoint)
            command = self._build_command(endpoint)
            with tempfile.TemporaryFile() as stderr_file:
                process = self._popen_factory(
                    command,
                    env=self._build_env(),
                    stdout=subprocess.PIPE,
                    stderr=stderr_file,
                )
                started_at = time.monotonic()
                stdout_result: list[bytes] = []
                stdout_error: list[Exception] = []

                def read_stdout() -> None:
                    try:
                        stdout_result.append(process.stdout.read(max_bytes + 1))
                    except Exception as exc:
                        stdout_error.append(exc)

                reader = threading.Thread(target=read_stdout, daemon=True)
                reader.start()
                reader.join(self._request_timeout_seconds)
                if reader.is_alive():
                    process.kill()
                    reader.join(self._request_timeout_seconds)
                    try:
                        process.wait(timeout=self._request_timeout_seconds)
                    except subprocess.TimeoutExpired:
                        pass
                    raise self._timeout_error(endpoint, attempt)
                if stdout_error:
                    raise stdout_error[0]
                raw_stdout = stdout_result[0]
                truncated = len(raw_stdout) > max_bytes
                terminated_for_truncation = False
                if truncated:
                    raw_stdout = raw_stdout[:max_bytes]
                    terminated_for_truncation = True
                    process.terminate()

                remaining = self._request_timeout_seconds - (
                    time.monotonic() - started_at
                )
                try:
                    returncode = process.wait(timeout=max(remaining, 0.001))
                except subprocess.TimeoutExpired as exc:
                    process.kill()
                    process.wait(timeout=self._request_timeout_seconds)
                    raise self._timeout_error(endpoint, attempt) from exc
                stderr_file.seek(0)
                raw_stderr = stderr_file.read()
            parsed = _parse_response(raw_stdout.decode("utf-8", errors="replace"))
            sanitized_stderr = _sanitize_stderr(raw_stderr.decode("utf-8", errors="replace"))
            self._append_audit(endpoint, parsed.status)

            if 200 <= parsed.status < 300 and (returncode == 0 or terminated_for_truncation):
                return GitHubTextResponse(
                    text=parsed.body,
                    truncated=truncated,
                    status=parsed.status,
                    headers=parsed.headers,
                )

            error = self._classify_error(
                endpoint=endpoint,
                status=parsed.status,
                headers=parsed.headers,
                body=parsed.body,
                attempts=attempt,
                sanitized_stderr=sanitized_stderr,
            )
            delay = self._retry_delay(error)
            if truncated or delay is None or attempt >= self._max_attempts:
                raise error
            self._sleep(delay)

        raise AssertionError("Unreachable")

    @staticmethod
    def _timeout_error(endpoint: str, attempt: int) -> GitHubApiError:
        return GitHubApiError(
            category="request-timeout",
            endpoint=endpoint,
            status=0,
            headers={},
            retryable=False,
            attempts=attempt,
            sanitized_stderr="",
        )

    def _request(self, endpoint: str) -> tuple[_ParsedResponse, int, str]:
        for attempt in range(1, self._max_attempts + 1):
            self._notify_request(endpoint)
            try:
                result = self._runner(
                    self._build_command(endpoint),
                    env=self._build_env(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    text=True,
                    timeout=self._request_timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise GitHubApiError(
                    category="request-timeout",
                    endpoint=endpoint,
                    status=0,
                    headers={},
                    retryable=False,
                    attempts=attempt,
                    sanitized_stderr="",
                ) from exc
            parsed = _parse_response(result.stdout)
            sanitized_stderr = _sanitize_stderr(getattr(result, "stderr", ""))
            self._append_audit(endpoint, parsed.status)

            if getattr(result, "returncode", 0) == 0 and 200 <= parsed.status < 300:
                return parsed, attempt, sanitized_stderr

            error = self._classify_error(
                endpoint=endpoint,
                status=parsed.status,
                headers=parsed.headers,
                body=parsed.body,
                attempts=attempt,
                sanitized_stderr=sanitized_stderr,
            )
            delay = self._retry_delay(error)
            if delay is None or attempt >= self._max_attempts:
                raise error
            self._sleep(delay)

        raise AssertionError("Unreachable")

    def _notify_request(self, endpoint: str) -> None:
        if self._request_observer is not None:
            self._request_observer(endpoint)

    def _build_command(self, endpoint: str) -> list[str]:
        if not endpoint or endpoint.startswith(("http://", "https://")):
            raise ValueError("endpoint must be a GitHub API path")

        return [
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
            endpoint,
        ]

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["GH_PAGER"] = "cat"
        return env

    def _with_paging(self, endpoint: str, page_number: int) -> str:
        split = urlsplit(endpoint)
        query_items = [(name, value) for name, value in parse_qsl(split.query, keep_blank_values=True) if name not in {"page", "per_page"}]
        query_items.append(("per_page", "100"))
        query_items.append(("page", str(page_number)))
        return urlunsplit((split.scheme, split.netloc, split.path, urlencode(query_items), split.fragment))

    def _extract_page_items(self, endpoint: str, payload: Any, key: str | None) -> list[Any]:
        if isinstance(payload, list):
            return payload

        selected_key = key
        if selected_key is None and isinstance(payload, dict):
            for candidate in ("jobs", "workflow_runs", "artifacts", "check_runs"):
                if candidate in payload:
                    selected_key = candidate
                    break

        if selected_key is None or not isinstance(payload, dict) or not isinstance(payload.get(selected_key), list):
            raise GitHubApiError(
                category="generic",
                endpoint=endpoint,
                status=200,
                headers={},
                retryable=False,
                attempts=1,
                sanitized_stderr="Unexpected paged response shape.",
            )

        return payload[selected_key]

    def _classify_error(
        self,
        *,
        endpoint: str,
        status: int,
        headers: dict[str, str],
        body: str,
        attempts: int,
        sanitized_stderr: str,
    ) -> GitHubApiError:
        details = "\n".join(part for part in (body, sanitized_stderr) if part).lower()
        retryable = False
        category = "generic"

        if status == 404:
            category = "not-found"
        elif "expired" in details:
            category = "expired"
        elif status == 401:
            category = "auth"
        elif _is_secondary_rate_limit(status, headers, details):
            category = "secondary-rate-limit"
            retryable = True
        elif _is_primary_rate_limit(headers):
            category = "primary-rate-limit"
            retryable = True
        elif status == 403:
            category = "authorization"
        elif 500 <= status < 600:
            category = "transient"
            retryable = True

        return GitHubApiError(
            category=category,
            endpoint=endpoint,
            status=status,
            headers=headers,
            retryable=retryable,
            attempts=attempts,
            sanitized_stderr=sanitized_stderr,
        )

    def _retry_delay(self, error: GitHubApiError) -> int | None:
        if not error.retryable:
            return None

        if error.category == "transient":
            return min(2 ** (error.attempts - 1), 2)

        if error.category == "secondary-rate-limit":
            retry_after = error.headers.get("retry-after")
            if retry_after is None:
                return None
            delay = max(0, int(retry_after))
            return self._bounded_rate_limit_delay(error, delay)

        if error.category == "primary-rate-limit":
            reset = error.headers.get("x-ratelimit-reset")
            if reset is None:
                return None
            delay = max(0, int(reset) - int(_unix_now(self._now())))
            return self._bounded_rate_limit_delay(error, delay)

        return None

    def _bounded_rate_limit_delay(self, error: GitHubApiError, delay: int) -> int:
        if delay > self._max_retry_delay:
            raise GitHubApiError(
                category="rate-limit-exhausted",
                endpoint=error.endpoint,
                status=error.status,
                headers=error.headers,
                retryable=False,
                attempts=error.attempts,
                sanitized_stderr=error.sanitized_stderr,
            )
        return delay

    def _append_audit(self, endpoint: str, status: int) -> None:
        if self._audit_path is None:
            return

        parent = self._audit_path.parent
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(parent, 0o700)

        record = {
            "method": "GET",
            "endpoint": endpoint,
            "status": status,
            "attemptedAt": _iso_now(self._now()),
        }
        data = json.dumps(record, sort_keys=True) + "\n"
        fd = os.open(self._audit_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(data)


def _parse_response(raw_output: str) -> _ParsedResponse:
    parsed = _parse_header_block(raw_output)
    while (
        (100 <= parsed.status < 200 or 300 <= parsed.status < 400)
        and parsed.body.startswith("HTTP/")
    ):
        redirected = _parse_header_block(parsed.body)
        if redirected.status == 0:
            break
        parsed = redirected

    return parsed


def _parse_header_block(raw_output: str) -> _ParsedResponse:
    if not raw_output.startswith("HTTP/"):
        return _ParsedResponse(status=0, headers={}, body=raw_output)

    separator, separator_width = _find_header_separator(raw_output)
    if separator == -1:
        return _ParsedResponse(status=0, headers={}, body=raw_output)

    header_text = raw_output[:separator]
    lines = [line for line in header_text.splitlines() if line]
    if not lines or _HTTP_STATUS_RE.fullmatch(lines[0]) is None:
        return _ParsedResponse(status=0, headers={}, body=raw_output)

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            return _ParsedResponse(status=0, headers={}, body=raw_output)
        name, value = line.split(":", 1)
        key = name.strip().lower()
        normalized_value = value.strip()
        if key in headers:
            headers[key] = f"{headers[key]}, {normalized_value}"
        else:
            headers[key] = normalized_value

    return _ParsedResponse(
        status=int(lines[0].split()[1]),
        headers=headers,
        body=raw_output[separator + separator_width:],
    )


def _find_header_separator(raw_output: str) -> tuple[int, int]:
    for separator in ("\r\n\r\n", "\n\n"):
        index = raw_output.find(separator)
        if index != -1:
            return index, len(separator)
    return -1, 0


def _sanitize_stderr(stderr: str) -> str:
    sanitized = re.sub(r"(?im)^authorization:.*$", "Authorization: [redacted]", stderr)
    for pattern in _TOKEN_PATTERNS:
        sanitized = pattern.sub("[redacted]", sanitized)
    return sanitized.strip()


def _is_secondary_rate_limit(status: int, headers: dict[str, str], details: str) -> bool:
    return status in {403, 429} and ("retry-after" in headers or "secondary rate limit" in details)


def _is_primary_rate_limit(headers: dict[str, str]) -> bool:
    return headers.get("x-ratelimit-remaining") == "0"


def _unix_now(value: object) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    return float(value)


def _iso_now(now_value: object) -> str:
    if isinstance(now_value, datetime):
        instant = now_value.astimezone(UTC)
    else:
        instant = datetime.fromtimestamp(float(now_value), tz=UTC)
    return instant.isoformat().replace("+00:00", "Z")
