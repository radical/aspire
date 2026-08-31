from __future__ import annotations

import json
from datetime import UTC, datetime
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Callable, Collection

from .jsonl import append_jsonl_rows, exclusive_jsonl_lock


_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REPOSITORY_ENDPOINT_RE = re.compile(
    r"^repos/(?P<repository>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:/|$)"
)
_CREATE_COMMENT_ENDPOINT_RE = re.compile(
    r"^repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/[1-9][0-9]*/comments$"
)
_EDIT_COMMENT_ENDPOINT_RE = re.compile(
    r"^repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/comments/[1-9][0-9]*$"
)
_PROTECTED_REPOSITORIES = frozenset({"microsoft/aspire"})
_HTTP_STATUS_RE = re.compile(r"(?m)^HTTP/\S+\s+(?P<status>[1-5][0-9]{2})\b")


class MutationRepositoryError(ValueError):
    """Raised before a mutation targets a repository outside the allowed set."""


class GitHubActorClient:
    def __init__(
        self,
        *,
        allowed_repositories: Collection[str] = (),
        protected_comment_repositories: Collection[str] = (),
        runner: Any = subprocess.run,
        request_timeout_seconds: float = 60,
        audit_path: Path | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive.")
        self._allowed_repositories = frozenset(
            self._repository(repository).casefold()
            for repository in allowed_repositories
        )
        self._protected_comment_repositories = frozenset(
            self._repository(repository).casefold()
            for repository in protected_comment_repositories
        )
        if not self._protected_comment_repositories.issubset(
            _PROTECTED_REPOSITORIES & self._allowed_repositories
        ):
            raise ValueError(
                "Protected comment repositories must be protected repositories "
                "that are also explicitly allowed."
            )
        self._runner = runner
        self._request_timeout_seconds = request_timeout_seconds
        self._audit_path = audit_path
        self._now = now or (lambda: datetime.now(UTC))

    def get_authenticated_login(self) -> str:
        payload = self._request("GET", "user")
        login = payload.get("login") if isinstance(payload, dict) else None
        if not isinstance(login, str) or not login:
            raise RuntimeError("GitHub did not return the authenticated login.")
        return login

    def get_issue(
        self,
        repository: str,
        issue_number: int,
    ) -> dict[str, object]:
        return self._object(
            self._request(
                "GET",
                f"repos/{self._repository(repository)}/issues/{self._number(issue_number)}",
            )
        )

    def get_comment(
        self,
        repository: str,
        comment_id: int,
    ) -> dict[str, object]:
        return self._object(
            self._request(
                "GET",
                (
                    f"repos/{self._repository(repository)}/issues/comments/"
                    f"{self._number(comment_id)}"
                ),
            )
        )

    def list_comments(
        self,
        repository: str,
        issue_number: int,
    ) -> list[dict[str, object]]:
        comments: list[dict[str, object]] = []
        for page in range(1, 101):
            payload = self._request(
                "GET",
                (
                    f"repos/{self._repository(repository)}/issues/"
                    f"{self._number(issue_number)}/comments"
                    f"?per_page=100&page={page}"
                ),
            )
            if not isinstance(payload, list) or not all(
                isinstance(comment, dict) for comment in payload
            ):
                raise RuntimeError("GitHub returned malformed issue comments.")
            comments.extend(payload)
            if len(payload) < 100:
                return comments
        raise RuntimeError(
            "Issue comment pagination exceeded the 10,000-comment safety bound."
        )

    def create_comment(
        self,
        repository: str,
        issue_number: int,
        body: str,
    ) -> dict[str, object]:
        return self._object(
            self._request(
                "POST",
                (
                    f"repos/{self._repository(repository)}/issues/"
                    f"{self._number(issue_number)}/comments"
                ),
                {"body": body},
            )
        )

    def edit_comment(
        self,
        repository: str,
        comment_id: int,
        body: str,
    ) -> dict[str, object]:
        return self._object(
            self._request(
                "PATCH",
                (
                    f"repos/{self._repository(repository)}/issues/comments/"
                    f"{self._number(comment_id)}"
                ),
                {"body": body},
            )
        )

    def close_issue(
        self,
        repository: str,
        issue_number: int,
        reason: str,
    ) -> dict[str, object]:
        if reason not in {"completed", "not_planned", "duplicate"}:
            raise ValueError("Unsupported issue close reason.")
        return self._object(
            self._request(
                "PATCH",
                f"repos/{self._repository(repository)}/issues/{self._number(issue_number)}",
                {"state": "closed", "state_reason": reason},
            )
        )

    def _request(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, object] | None = None,
    ) -> object:
        if method != "GET":
            match = _REPOSITORY_ENDPOINT_RE.match(endpoint)
            repository = match.group("repository") if match else None
            if repository is None:
                raise MutationRepositoryError(
                    "Mutation endpoint must identify one repository."
                )
            normalized_repository = repository.casefold()
            if normalized_repository in _PROTECTED_REPOSITORIES:
                if normalized_repository not in self._protected_comment_repositories:
                    raise MutationRepositoryError(
                        f"Mutation repository is protected: {repository}"
                    )
                if not (
                    method == "PATCH"
                    and _EDIT_COMMENT_ENDPOINT_RE.fullmatch(endpoint)
                ):
                    raise MutationRepositoryError(
                        "Protected repository pilot permits existing comment edits only."
                    )
            if normalized_repository not in self._allowed_repositories:
                raise MutationRepositoryError(
                    f"Mutation repository is not explicitly allowed: {repository}"
                )

        command = [
            "gh",
            "api",
            "--method",
            method,
            "--hostname",
            "github.com",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
            "--include",
        ]
        temporary_path: Path | None = None
        request_attempted = False
        response_status: int | None = None
        try:
            if payload is not None:
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix="ci-shepherd-action-",
                    suffix=".json",
                )
                temporary_path = Path(temporary_name)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, sort_keys=True)
                    stream.write("\n")
                temporary_path.chmod(0o600)
                command.extend(["--input", str(temporary_path)])
            command.append(endpoint)
            try:
                request_attempted = True
                completed = self._runner(
                    command,
                    env=self._environment(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    text=True,
                    timeout=self._request_timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    f"GitHub API {method} timed out after "
                    f"{self._request_timeout_seconds} seconds."
                ) from exc
            response_status, response_body = _response_status_and_body(
                completed.stdout,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"GitHub API {method} failed: {completed.stderr.strip()}"
                )
            try:
                return json.loads(response_body)
            except json.JSONDecodeError as exc:
                raise RuntimeError("GitHub returned malformed JSON.") from exc
        finally:
            if request_attempted:
                self._append_audit(method, endpoint, response_status)
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _append_audit(
        self,
        method: str,
        endpoint: str,
        status: int | None,
    ) -> None:
        if self._audit_path is None:
            return
        record = {
            "method": method,
            "endpoint": endpoint,
            "status": status,
            "attemptedAt": self._now().astimezone(UTC).isoformat().replace(
                "+00:00",
                "Z",
            ),
        }
        with exclusive_jsonl_lock(self._audit_path):
            append_jsonl_rows(self._audit_path, [record])

    @staticmethod
    def _environment() -> dict[str, str]:
        env = dict(os.environ)
        env["GH_PAGER"] = "cat"
        return env

    @staticmethod
    def _repository(repository: str) -> str:
        parts = repository.split("/")
        if (
            not _REPOSITORY_RE.fullmatch(repository)
            or any(part in {".", ".."} for part in parts)
        ):
            raise ValueError("repository must have a safe owner/name form.")
        return repository

    @staticmethod
    def _number(value: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError("GitHub identifiers must be positive integers.")
        return value

    @staticmethod
    def _object(payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise RuntimeError("GitHub returned a non-object response.")
        return payload


def _response_status_and_body(
    stdout: str,
) -> tuple[int | None, str]:
    matches = list(_HTTP_STATUS_RE.finditer(stdout))
    if not matches:
        return None, stdout
    status = int(matches[-1].group("status"))
    header_end = max(stdout.rfind("\r\n\r\n"), stdout.rfind("\n\n"))
    if header_end < 0:
        return status, stdout
    separator_length = 4 if stdout.startswith("\r\n", header_end) else 2
    return status, stdout[header_end + separator_length :]
