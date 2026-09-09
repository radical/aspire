"""Validate advisory discoveries without expanding frozen mutation evidence."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


def validate_scoped_result(result: Mapping[str, Any]) -> None:
    fields = {"outcome", "summary", "evidenceIds", "reassessWhen", "missingEvidence", "fixHandoff", "workLog"}
    if set(result) != fields:
        raise ValueError("Scoped investigation results must match the result schema, including workLog.")
    for field, limit in (("summary", 4000), ("reassessWhen", 2000)):
        value = result[field]
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"Investigation {field} must be nonempty and bounded.")
    missing = result["missingEvidence"]
    if (
        not isinstance(missing, list) or len(missing) > 20
        or any(not isinstance(item, str) or not item.strip() or len(item) > 2000 for item in missing)
    ):
        raise ValueError("Investigation missingEvidence must be a bounded array of strings.")
    handoff = result["fixHandoff"]
    if handoff is not None:
        if not isinstance(handoff, Mapping) or set(handoff) != {"problem", "likelyPaths", "validation"}:
            raise ValueError("Investigation fixHandoff must match the suggested-fix schema.")
        if not isinstance(handoff["problem"], str) or not handoff["problem"].strip() or len(handoff["problem"]) > 4000:
            raise ValueError("Investigation fixHandoff.problem must be nonempty and bounded.")
        for field in ("likelyPaths", "validation"):
            values = handoff[field]
            if (
                not isinstance(values, list) or not values or len(values) > 20
                or any(not isinstance(value, str) or not value.strip() or len(value) > 2000 for value in values)
            ):
                raise ValueError(f"Investigation fixHandoff.{field} must be a bounded array of strings.")


def validate_reproduction_commands(value: object) -> list[list[str]]:
    if not isinstance(value, list) or len(value) > 3:
        raise ValueError("reproductionCommands must contain at most three exact argv arrays.")
    for command in value:
        if (
            not isinstance(command, list) or not command or len(command) > 40
            or any(not isinstance(arg, str) or not arg or "\0" in arg or len(arg) > 2048 for arg in command)
        ):
            raise ValueError("reproductionCommands must contain nonempty exact argv arrays.")
    return [list(command) for command in value]


def validate_work_log(
    request: Mapping[str, Any],
    value: object,
    checkout: Path,
    reproduction_commands: Sequence[Sequence[str]],
) -> list[dict[str, Any]]:
    scope = request["investigationScope"]
    if not isinstance(value, list) or not value or len(value) > 60:
        raise ValueError("workLog must contain one to sixty actual-work entries.")
    if len(json.dumps(value, ensure_ascii=True).encode("utf-8")) > 64_000:
        raise ValueError("workLog exceeds its 64,000-byte limit.")
    source_paths: set[str] = set()
    read_count = command_count = 0
    rows: list[dict[str, Any]] = []
    # Match ownership checks: inherited Git overrides must not redirect source
    # receipts to another repository or index.
    git_environment = {key: item for key, item in os.environ.items() if not key.startswith("GIT_")}
    git_environment["GIT_OPTIONAL_LOCKS"] = "0"
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("workLog entries must be objects.")
        finding = item.get("finding")
        if not isinstance(finding, str) or not finding.strip() or len(finding) > 4000:
            raise ValueError("workLog entries need a bounded finding, not only a disposition.")
        kind = item.get("kind")
        if kind == "evidence":
            fields = {"kind", "evidenceId", "finding"}
            if item.get("evidenceId") not in request["evidenceIds"]:
                raise ValueError("workLog cites an unknown frozen evidence ID.")
        elif kind == "source":
            fields = {"kind", "path", "startLine", "endLine", "finding"}
            path = item.get("path")
            if (
                not isinstance(path, str) or not path or len(path) > 1024
                or "\\" in path or ":" in path or "\0" in path
                or any(part in {"", ".", "..", ".git"} for part in path.split("/"))
            ):
                raise ValueError("workLog source paths must be safe repository-relative paths.")
            source = checkout / path
            if source.is_symlink() or not source.resolve().is_relative_to(checkout.resolve()):
                raise ValueError("workLog source path must remain in its owned checkout.")
            start, end = item.get("startLine"), item.get("endLine")
            if type(start) is not int or type(end) is not int or not 1 <= start <= end:
                raise ValueError("workLog source line range is invalid.")
            # Read the committed object, not an ignored file or a mutable file
            # reached through a checkout symlink. The caller separately checks HEAD.
            object_name = f"{scope['sourceRevision']}:{path}"
            size = subprocess.run(
                ["git", "--no-pager", "-C", str(checkout), "cat-file", "-s", object_name],
                check=False, capture_output=True, text=True, timeout=30, env=git_environment,
            )
            if size.returncode or not size.stdout.strip().isdigit() or int(size.stdout.strip()) > 2 * 1024 * 1024:
                raise ValueError("workLog source object is unavailable or exceeds the two-MiB inspection limit.")
            result = subprocess.run(
                ["git", "--no-pager", "-C", str(checkout), "show",
                 object_name],
                check=False, capture_output=True, text=True, timeout=30, env=git_environment,
            )
            if result.returncode or end > len(result.stdout.splitlines()):
                raise ValueError("workLog source range is not present in the pinned revision.")
            source_paths.add(path)
        elif kind == "github-get":
            fields = {"kind", "url", "finding"}
            url = item.get("url")
            if not isinstance(url, str) or len(url) > 2048:
                raise ValueError("workLog GET URL is invalid.")
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https" or parsed.netloc not in {"api.github.com", "github.com"}
                or parsed.fragment
            ):
                raise ValueError("workLog GETs must use direct HTTPS GitHub URLs.")
            prefix = ("/repos/" if parsed.netloc == "api.github.com" else "/") + request["repository"] + "/"
            if not parsed.path.startswith(prefix):
                raise ValueError("workLog GET is outside the investigation repository.")
            suffix = parsed.path[len(prefix):]
            patterns = (
                rf"issues/{request['issueNumber']}(?:/comments)?",
                r"pulls/[1-9]\d*(?:/(?:files|reviews|commits))?",
                r"pull/[1-9]\d*(?:/files)?",
                r"actions/runs/[1-9]\d*(?:/(?:jobs|logs|artifacts|job/[1-9]\d*))?",
                r"actions/runs/[1-9]\d*/attempts/[1-9]\d*/jobs",
                r"actions/jobs/[1-9]\d*(?:/logs)?",
                r"actions/artifacts/[1-9]\d*(?:/zip)?",
            )
            if not any(re.fullmatch(pattern, suffix) for pattern in patterns):
                raise ValueError("workLog GET is outside the bounded diagnostic endpoint scope.")
            read_count += 1
        elif kind == "command":
            fields = {"kind", "argv", "exitCode", "output", "finding"}
            if item.get("argv") not in reproduction_commands:
                raise ValueError("workLog command was not explicitly authorized for this session.")
            if type(item.get("exitCode")) is not int:
                raise ValueError("workLog command must record its exit code.")
            if not isinstance(item.get("output"), str) or len(item["output"]) > 8000:
                raise ValueError("workLog command must record bounded observed output.")
            command_count += 1
        else:
            raise ValueError("workLog kind is unsupported.")
        if set(item) != fields:
            raise ValueError("workLog entry fields do not match its kind.")
        rows.append(dict(item))
    if len(source_paths) > scope["maxSourceFiles"] or read_count > scope["maxReadOnlyRequests"]:
        raise ValueError("workLog exceeds its source or GET budget.")
    if command_count > 3:
        raise ValueError("workLog exceeds its reproduction-attempt budget.")
    return rows
