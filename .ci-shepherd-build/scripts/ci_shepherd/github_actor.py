from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Collection


_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REPOSITORY_ENDPOINT_RE = re.compile(
    r"^repos/(?P<repository>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:/|$)"
)
_PROTECTED_REPOSITORIES = frozenset({"microsoft/aspire"})


class MutationRepositoryError(ValueError):
    """Raised before a mutation targets a repository outside the allowed set."""


class GitHubActorClient:
    def __init__(
        self,
        *,
        allowed_repositories: Collection[str] = (),
        runner: Any = subprocess.run,
        request_timeout_seconds: float = 60,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive.")
        self._allowed_repositories = frozenset(
            self._repository(repository).casefold()
            for repository in allowed_repositories
        )
        self._runner = runner
        self._request_timeout_seconds = request_timeout_seconds

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
                raise MutationRepositoryError(
                    f"Mutation repository is protected: {repository}"
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
        ]
        temporary_path: Path | None = None
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
            if completed.returncode != 0:
                raise RuntimeError(
                    f"GitHub API {method} failed: {completed.stderr.strip()}"
                )
            try:
                return json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError("GitHub returned malformed JSON.") from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

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
