from __future__ import annotations

import base64
import binascii
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote


_CODEOWNERS_LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
_MAX_REMOTE_CODEOWNERS_BYTES = 3 * 1024 * 1024
_GIT_TIMEOUT_SECONDS = 10
_HTTP_REMOTE_RE = re.compile(r"^https://github\.com/(?P<repository>[^/\s]+/[^/\s]+?)(?:\.git)?/?$")
_SCP_REMOTE_RE = re.compile(r"^git@github\.com:(?P<repository>[^/\s]+/[^/\s]+?)(?:\.git)?$")
_SSH_REMOTE_RE = re.compile(r"^ssh://git@github\.com/(?P<repository>[^/\s]+/[^/\s]+?)(?:\.git)?/?$")


class OwnershipError(RuntimeError):
    def __init__(self, stage: str, endpoint: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.endpoint = endpoint


@dataclass(frozen=True, slots=True)
class CheckoutInfo:
    repository: str
    commit: str


@dataclass(frozen=True, slots=True)
class CodeownersRule:
    pattern: str
    owners: list[str]
    line_number: int


@dataclass(frozen=True, slots=True)
class CodeownersMatch:
    path: str
    pattern: str
    owners: list[str]
    line_number: int


@dataclass(frozen=True, slots=True)
class CodeownersDocument:
    source_path: str
    source_url: str
    checkout_commit: str | None
    rules: list[CodeownersRule]


def collect_affected_paths(
    evidence: dict[str, dict[str, Any]],
    *,
    target_repository: str | None = None,
) -> list[str]:
    affected_paths: set[str] = set()

    for evidence_id, record in evidence.items():
        if not isinstance(record, dict):
            continue
        kind = record.get("kind")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if not _evidence_targets_repository(evidence_id, payload, target_repository):
            continue

        if kind == "pull-request":
            for item in payload.get("files", []):
                if isinstance(item, dict):
                    path = item.get("path")
                    if isinstance(path, str) and path:
                        affected_paths.add(path)
            continue

        if kind == "commit":
            for path in payload.get("changedPaths", []):
                if isinstance(path, str) and path:
                    affected_paths.add(path)
            continue

        if kind == "workflow-job" and ":annotation:" in evidence_id:
            path = payload.get("path")
            if isinstance(path, str) and path:
                affected_paths.add(path)
            continue

        if kind == "source-path":
            path = payload.get("path")
            if isinstance(path, str) and path:
                affected_paths.add(path)

    return sorted(affected_paths)


def collect_path_referenced_by(
    evidence: dict[str, dict[str, Any]],
    *,
    target_repository: str,
) -> dict[str, list[dict[str, Any]]]:
    associations: dict[str, dict[tuple[int, str, str, str], dict[str, Any]]] = {}
    for evidence_id, record in evidence.items():
        if not isinstance(record, dict) or record.get("kind") not in {"pull-request", "commit"}:
            continue
        payload = record.get("payload")
        if (
            not isinstance(payload, dict)
            or not _evidence_targets_repository(evidence_id, payload, target_repository)
        ):
            continue

        referenced_by = _normalize_referenced_by(payload.get("referencedBy"))
        if not referenced_by:
            continue
        paths = (
            [
                item.get("path")
                for item in payload.get("files", [])
                if isinstance(item, dict)
            ]
            if record["kind"] == "pull-request"
            else payload.get("changedPaths", [])
        )
        for path in paths:
            if not isinstance(path, str) or not path:
                continue
            path_associations = associations.setdefault(path, {})
            for reference in referenced_by:
                key = (
                    reference["sourceIssueNumber"],
                    reference["sourceEvidenceId"],
                    reference["sourceUrl"],
                    reference["extractionMethod"],
                )
                path_associations[key] = reference

    return {
        path: [references[key] for key in sorted(references)]
        for path, references in sorted(associations.items())
    }


def _normalize_referenced_by(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for reference in value:
        if not isinstance(reference, dict):
            continue
        source_issue_number = reference.get("sourceIssueNumber")
        source_evidence_id = reference.get("sourceEvidenceId")
        source_url = reference.get("sourceUrl")
        extraction_method = reference.get("extractionMethod")
        if (
            not isinstance(source_issue_number, int)
            or isinstance(source_issue_number, bool)
            or source_issue_number <= 0
            or not isinstance(source_evidence_id, str)
            or not source_evidence_id
            or not isinstance(source_url, str)
            or not source_url
            or not isinstance(extraction_method, str)
            or not extraction_method
        ):
            continue
        normalized.append(
            {
                "sourceIssueNumber": source_issue_number,
                "sourceEvidenceId": source_evidence_id,
                "sourceUrl": source_url,
                "extractionMethod": extraction_method,
            }
        )
    return normalized


def _evidence_targets_repository(
    evidence_id: str,
    payload: dict[str, Any],
    target_repository: str | None,
) -> bool:
    if target_repository is None:
        return True
    evidence_repository = payload.get("targetRepository")
    if isinstance(evidence_repository, str) and evidence_repository:
        return _same_repository(evidence_repository, target_repository)

    prefix = "pr:" if evidence_id.startswith("pr:") else "commit:" if evidence_id.startswith("commit:") else None
    if prefix is None:
        return True
    identity_parts = evidence_id[len(prefix):].rsplit(":", 1)
    if len(identity_parts) != 2 or "/" not in identity_parts[0]:
        return True
    return _same_repository(identity_parts[0], target_repository)


def normalize_github_repository_url(url: str) -> str | None:
    for pattern in (_HTTP_REMOTE_RE, _SCP_REMOTE_RE, _SSH_REMOTE_RE):
        match = pattern.fullmatch(url.strip())
        if match is not None:
            return match.group("repository")
    return None


def validate_checkout(
    checkout_path: Path,
    repository: str,
    *,
    git_runner: Any = subprocess.run,
    timeout_seconds: int = _GIT_TIMEOUT_SECONDS,
) -> CheckoutInfo:
    inside_work_tree = _run_git(
        checkout_path,
        ("rev-parse", "--is-inside-work-tree"),
        git_runner=git_runner,
        timeout_seconds=timeout_seconds,
        stage="ownership-checkout",
    )
    if inside_work_tree.strip() != "true":
        raise OwnershipError("ownership-checkout", "git rev-parse --is-inside-work-tree", "checkout is not a git worktree")

    remote_urls: list[str] = []
    for remote_name in ("origin", "upstream"):
        try:
            remote_url = _run_git(
                checkout_path,
                ("remote", "get-url", remote_name),
                git_runner=git_runner,
                timeout_seconds=timeout_seconds,
                stage="ownership-checkout",
            ).strip()
        except OwnershipError:
            continue
        remote_urls.append(remote_url)
        normalized_remote = normalize_github_repository_url(remote_url)
        if normalized_remote is not None and _same_repository(normalized_remote, repository):
            commit = _run_git(
                checkout_path,
                ("rev-parse", "HEAD"),
                git_runner=git_runner,
                timeout_seconds=timeout_seconds,
                stage="ownership-checkout",
            ).strip()
            if not commit:
                raise OwnershipError("ownership-checkout", "git rev-parse HEAD", "git returned an empty HEAD commit")
            return CheckoutInfo(repository=repository, commit=commit)

    raise OwnershipError(
        "ownership-checkout",
        "git remote get-url origin|upstream",
        f"checkout remotes do not match {repository}: {remote_urls!r}",
    )


def _same_repository(left: str, right: str) -> bool:
    return left.casefold() == right.casefold()


def load_codeowners_from_checkout(checkout_path: Path, checkout_info: CheckoutInfo) -> CodeownersDocument | None:
    for relative_path in _CODEOWNERS_LOCATIONS:
        candidate = checkout_path / relative_path
        if not candidate.is_file():
            continue
        try:
            contents = candidate.read_text(encoding="utf-8")
        except OSError as exc:
            raise OwnershipError("ownership-codeowners", relative_path, f"failed to read {relative_path}: {exc}") from exc
        return CodeownersDocument(
            source_path=relative_path,
            source_url=build_blob_url(checkout_info.repository, checkout_info.commit, relative_path),
            checkout_commit=checkout_info.commit,
            rules=parse_codeowners(contents),
        )
    return None


def load_codeowners_from_api(client: Any, repository: str) -> CodeownersDocument | None:
    for relative_path in _CODEOWNERS_LOCATIONS:
        endpoint = f"/repos/{repository}/contents/{relative_path}"
        try:
            payload = client.get(endpoint)
        except Exception as exc:
            category = getattr(exc, "category", None)
            status = getattr(exc, "status", None)
            if category == "not-found" or status == 404:
                continue
            raise OwnershipError("ownership-codeowners", endpoint, str(exc)) from exc

        if not isinstance(payload, dict):
            raise OwnershipError("ownership-codeowners", endpoint, "unexpected contents payload shape")
        if payload.get("type") != "file":
            raise OwnershipError("ownership-codeowners", endpoint, "contents payload must describe a file")

        size = payload.get("size")
        if not isinstance(size, int) or size < 0:
            raise OwnershipError("ownership-codeowners", endpoint, "contents payload is missing a valid size")
        if size >= _MAX_REMOTE_CODEOWNERS_BYTES:
            raise OwnershipError("ownership-codeowners", endpoint, "CODEOWNERS payload is too large")

        encoding = payload.get("encoding")
        if encoding != "base64":
            raise OwnershipError("ownership-codeowners", endpoint, "contents payload must use base64 encoding")

        content = payload.get("content")
        html_url = payload.get("html_url")
        if not isinstance(content, str) or not isinstance(html_url, str) or not html_url:
            raise OwnershipError("ownership-codeowners", endpoint, "contents payload is missing content or html_url")

        try:
            decoded = base64.b64decode("".join(content.split()), validate=True).decode("utf-8")
        except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
            raise OwnershipError("ownership-codeowners", endpoint, "contents payload is not valid base64 utf-8") from exc

        return CodeownersDocument(
            source_path=relative_path,
            source_url=html_url,
            checkout_commit=None,
            rules=parse_codeowners(decoded),
        )

    return None


def load_source_history(
    checkout_path: Path,
    relative_path: str,
    checkout_info: CheckoutInfo,
    *,
    git_runner: Any = subprocess.run,
    timeout_seconds: int = _GIT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    command_output = _run_git(
        checkout_path,
        (
            "log",
            "-5",
            "--format=%H%x09%aN%x09%aE%x09%aI",
            checkout_info.commit,
            "--",
            f":(literal){relative_path}",
        ),
        git_runner=git_runner,
        timeout_seconds=timeout_seconds,
        stage="ownership-history",
    )
    recent_commits: list[dict[str, str]] = []
    for raw_line in command_output.splitlines():
        if not raw_line:
            continue
        parts = raw_line.split("\t")
        if len(parts) != 4 or not all(parts):
            raise OwnershipError(
                "ownership-history",
                f"git log -- {relative_path}",
                f"unexpected git log output for {relative_path}: {raw_line!r}",
            )
        commit, author_name, author_email, authored_at = parts
        recent_commits.append(
            {
                "commit": commit,
                "authorName": author_name,
                "authorEmail": author_email,
                "authoredAt": authored_at,
            }
        )
    return {
        "path": relative_path,
        "targetRepository": checkout_info.repository,
        "checkoutCommit": checkout_info.commit,
        "sourceUrl": build_blob_url(checkout_info.repository, checkout_info.commit, relative_path),
        "recentCommits": recent_commits[:5],
    }


def parse_codeowners(contents: str) -> list[CodeownersRule]:
    rules: list[CodeownersRule] = []
    for line_number, raw_line in enumerate(contents.splitlines(), start=1):
        parsed = _parse_codeowners_line(raw_line)
        if parsed is None:
            continue
        pattern, owners = parsed
        rules.append(CodeownersRule(pattern=pattern, owners=owners, line_number=line_number))
    return rules


def match_codeowners(path: str, rules: list[CodeownersRule]) -> CodeownersMatch | None:
    matched_rule: CodeownersRule | None = None
    for rule in rules:
        if _matches_codeowners_pattern(rule.pattern, path):
            matched_rule = rule
    if matched_rule is None:
        return None
    return CodeownersMatch(
        path=path,
        pattern=matched_rule.pattern,
        owners=list(matched_rule.owners),
        line_number=matched_rule.line_number,
    )


def _parse_codeowners_line(raw_line: str) -> tuple[str, list[str]] | None:
    stripped = raw_line.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith("!") or stripped.startswith("\\#") or stripped.endswith("\\"):
        return None

    tokens: list[str] = []
    current: list[str] = []
    escaped = False
    index = 0
    while index < len(raw_line):
        character = raw_line[index]
        if escaped:
            current.append(character)
            escaped = False
            index += 1
            continue
        if character == "\\":
            escaped = True
            index += 1
            continue
        if character.isspace():
            if current:
                tokens.append("".join(current))
                current = []
            index += 1
            while index < len(raw_line) and raw_line[index].isspace():
                index += 1
            if index < len(raw_line) and raw_line[index] == "#":
                break
            continue
        current.append(character)
        index += 1
    if escaped or current and "".join(current).endswith("\\"):
        return None
    if current:
        tokens.append("".join(current))
    if not tokens:
        return None

    pattern = tokens[0]
    if "[" in pattern or "]" in pattern:
        return None
    return pattern, tokens[1:]


def _matches_codeowners_pattern(pattern: str, path: str) -> bool:
    normalized_path = path.strip("/")
    if not normalized_path:
        return False

    trailing_slash = pattern.endswith("/")
    pattern_body = pattern[:-1] if trailing_slash else pattern
    rooted = pattern_body.startswith("/") or "/" in pattern_body
    pattern_body = pattern_body.lstrip("/")

    if not pattern_body:
        return False

    if not rooted:
        segment_regex = re.compile(f"^{_translate_pattern(pattern_body)}$")
        segments = normalized_path.split("/")
        if trailing_slash:
            return any(segment_regex.fullmatch(segment) for segment in segments[:-1])
        return any(segment_regex.fullmatch(segment) for segment in segments)

    regex_body = _translate_pattern(pattern_body)
    if trailing_slash:
        regex = re.compile(f"^{regex_body}/.*$")
    else:
        regex = re.compile(f"^{regex_body}$")
    return regex.fullmatch(normalized_path) is not None


def _translate_pattern(pattern: str) -> str:
    translated: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            translated.append("(?:[^/]+/)*")
            index += 3
            continue
        if pattern.startswith("**", index):
            translated.append(".*")
            index += 2
            continue
        character = pattern[index]
        if character == "*":
            translated.append("[^/]*")
        elif character == "?":
            translated.append("[^/]")
        else:
            translated.append(re.escape(character))
        index += 1
    return "".join(translated)


def _run_git(
    checkout_path: Path,
    arguments: tuple[str, ...],
    *,
    git_runner: Any,
    timeout_seconds: int,
    stage: str,
) -> str:
    command = ["git", "-C", str(checkout_path), *arguments]
    try:
        result = git_runner(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
        )
    except Exception as exc:
        raise OwnershipError(stage, " ".join(command), str(exc)) from exc

    stdout = getattr(result, "stdout", "")
    stderr = getattr(result, "stderr", "")
    if getattr(result, "returncode", 1) != 0:
        message = stderr.strip() or stdout.strip() or f"git exited with {result.returncode}"
        raise OwnershipError(stage, " ".join(command), message)
    return stdout if isinstance(stdout, str) else ""


def build_blob_url(repository: str, commit: str, relative_path: str) -> str:
    return f"https://github.com/{repository}/blob/{commit}/{quote(relative_path, safe='/')}"
