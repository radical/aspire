from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Mapping

from .lifetime import adopt_lifetime_lock
from .models import (
    JudgmentRequest,
    JudgmentResult,
    parse_judgment_request,
    parse_judgment_result,
)
from .worker import (
    _atomic_write_json,
    _read_bounded_text,
    _require_canonical_absolute_path,
    _validate_io_path,
)


_MAX_CAPTURE_BYTES = 1_048_576
_DETAIL_KEYS = frozenset(
    {
        "schemaVersion",
        "workerId",
        "requestPath",
        "resultPath",
        "stdoutPath",
        "stderrPath",
        "usagePath",
        "model",
        "reasoningEffort",
    }
)


def build_copilot_argv(
    request: JudgmentRequest,
    *,
    worker_directory: Path,
    usage_path: Path,
    model: str,
    reasoning_effort: str,
    executable: str = "copilot",
) -> list[str]:
    return [
        executable,
        "--no-auto-update",
        "--model",
        model,
        "--reasoning-effort",
        reasoning_effort,
        "--no-custom-instructions",
        "--disable-builtin-mcps",
        "--available-tools=view",
        "--allow-all-tools",
        "--silent",
        "--output-format",
        "json",
        "--session-id",
        request.session_id,
        "--usage-output-file",
        str(usage_path),
        "-C",
        str(worker_directory),
        "--prompt",
        request.prompt,
    ]


def run_worker(
    *,
    request_path: Path,
    result_path: Path,
    detail_path: Path,
    lifetime_fd: int,
    model: str,
    reasoning_effort: str,
    copilot_executable: str = "copilot",
) -> int:
    worker_directory = request_path.parent
    _require_private_packet_path(request_path, worker_directory, "request")
    _require_private_packet_path(result_path, worker_directory, "result")
    _require_private_packet_path(detail_path, worker_directory, "detail")
    request = parse_judgment_request(
        _read_bounded_text(
            request_path,
            _MAX_CAPTURE_BYTES,
            worker_directory=worker_directory,
        )
    )
    detail = _load_detail(detail_path)
    _validate_detail(
        detail,
        request=request,
        request_path=request_path,
        result_path=result_path,
        detail_path=detail_path,
        model=model,
        reasoning_effort=reasoning_effort,
    )
    stdout_path = Path(_required_string(detail, "stdoutPath"))
    stderr_path = Path(_required_string(detail, "stderrPath"))
    usage_path = Path(_required_string(detail, "usagePath"))
    for name, path in (
        ("stdout", stdout_path),
        ("stderr", stderr_path),
        ("usage", usage_path),
    ):
        _require_private_packet_path(path, worker_directory, name)
    packet_paths = (
        request_path,
        result_path,
        detail_path,
        stdout_path,
        stderr_path,
        usage_path,
        worker_directory / "lifetime.lock",
    )
    if len(set(packet_paths)) != len(packet_paths):
        raise ValueError("Worker packet paths must be mutually distinct.")

    lifetime_path = worker_directory / "lifetime.lock"
    # This object intentionally remains live until process exit. Closing it on
    # function return could release the inherited open-file-description before
    # the wrapper process has exited.
    _lifetime_lock = adopt_lifetime_lock(lifetime_fd, lifetime_path)
    started_at = _now()
    exit_code: int | None = None
    result: JudgmentResult | None = None
    status = "failed"
    error: str | None = None
    argv = build_copilot_argv(
        request,
        worker_directory=worker_directory,
        usage_path=usage_path,
        model=model,
        reasoning_effort=reasoning_effort,
        executable=copilot_executable,
    )
    copilot_home = worker_directory / "copilot-home"
    if copilot_home.is_symlink():
        raise ValueError("Worker Copilot home must not be a symlink.")
    copilot_home.mkdir(mode=0o700, exist_ok=True)
    os.chmod(copilot_home, 0o700)
    environment = dict(os.environ)
    environment["COPILOT_HOME"] = str(copilot_home)
    try:
        process = subprocess.Popen(
            argv,
            cwd=worker_directory,
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=None,
            close_fds=True,
            pass_fds=(lifetime_fd,),
            env=environment,
        )
        exit_code = process.wait()
        try:
            _sync_standard_streams()
        except OSError as sync_error:
            error = (
                "Copilot output could not be durably synced: "
                f"{sync_error}"
            )
        if error is not None:
            pass
        elif exit_code != 0:
            error = f"Copilot exited with status {exit_code}."
        else:
            try:
                output = _read_bounded_text(
                    stdout_path,
                    _MAX_CAPTURE_BYTES,
                    worker_directory=worker_directory,
                )
                result = parse_judgment_result(
                    _judgment_text_from_output(output),
                    request,
                )
            except (OSError, UnicodeError, ValueError) as parse_error:
                status = "invalid"
                error = f"Copilot output is invalid: {parse_error}"
            else:
                status = "succeeded"
    except OSError as launch_error:
        error = f"Copilot could not be started: {launch_error}"

    completed_at = _now()
    envelope = {
        "schemaVersion": 1,
        "workerId": request.worker_id,
        "sessionId": request.session_id,
        "itemId": request.item_id,
        "episode": request.episode,
        "evidenceFingerprint": request.evidence_fingerprint,
        "status": status,
        "exitCode": exit_code,
        "startedAt": started_at,
        "completedAt": completed_at,
        "requestPath": str(request_path),
        "resultPath": str(result_path),
        "detailPath": str(detail_path),
        "stdoutPath": str(stdout_path),
        "stderrPath": str(stderr_path),
        "usagePath": str(usage_path),
        "requestIdentity": {
            "issueNumber": request.issue_number,
            "taskId": request.task_id,
            "pullRequestNumber": request.pull_request_number,
            "pullRequestHeadSha": request.pull_request_head_sha,
            "pullRequestHeadRef": request.pull_request_head_ref,
            "pullRequestBaseRef": request.pull_request_base_ref,
            "pullRequestObservedAt": request.pull_request_observed_at,
        },
        "judgmentResult": (
            _judgment_result_document(result) if result is not None else None
        ),
        "error": error,
    }
    _atomic_write_json(
        result_path,
        envelope,
        worker_directory=worker_directory,
    )
    # Do not close _lifetime_lock here. The descriptor must remain owned until
    # the operating system tears down this wrapper process.
    return 0


def _judgment_text_from_output(output: str) -> str:
    final_messages: list[str] = []
    for line_number, line in enumerate(output.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"Copilot JSONL line {line_number} is not valid JSON."
            ) from error
        if not isinstance(event, dict):
            raise ValueError(
                f"Copilot JSONL line {line_number} must be an object."
            )
        if event.get("type") != "assistant.message":
            continue
        data = event.get("data")
        if not isinstance(data, dict) or data.get("phase") != "final_answer":
            continue
        content = data.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Copilot final assistant message has no text content.")
        final_messages.append(content)
    if len(final_messages) != 1:
        raise ValueError(
            "Copilot JSONL must contain exactly one final assistant message."
        )
    return final_messages[0]


def _load_detail(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(
            _read_bounded_text(
                path,
                _MAX_CAPTURE_BYTES,
                worker_directory=path.parent,
            )
        )
    except json.JSONDecodeError as error:
        raise ValueError("Worker detail manifest must be valid JSON.") from error
    if not isinstance(value, dict) or frozenset(value) != _DETAIL_KEYS:
        raise ValueError("Worker detail manifest fields do not match the schema.")
    return value


def _validate_detail(
    detail: Mapping[str, object],
    *,
    request: JudgmentRequest,
    request_path: Path,
    result_path: Path,
    detail_path: Path,
    model: str,
    reasoning_effort: str,
) -> None:
    if detail.get("schemaVersion") != 1:
        raise ValueError("Worker detail schemaVersion must be 1.")
    expected = {
        "workerId": request.worker_id,
        "requestPath": str(request_path),
        "resultPath": str(result_path),
        "model": model,
        "reasoningEffort": reasoning_effort,
    }
    for name, value in expected.items():
        if detail.get(name) != value:
            raise ValueError(f"Worker detail manifest has invalid {name}.")
    if Path(_required_string(detail, "resultPath")) != result_path:
        raise ValueError("Worker detail resultPath is invalid.")
    if detail_path.name != "detail.json":
        raise ValueError("Worker detail path must be detail.json.")


def _required_string(document: Mapping[str, object], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Worker detail {name} must be a nonempty string.")
    return value


def _require_private_packet_path(
    path: Path,
    worker_directory: Path,
    name: str,
) -> None:
    _require_canonical_absolute_path(worker_directory, "worker_directory")
    try:
        _validate_io_path(path, worker_directory)
    except ValueError as error:
        raise ValueError(f"Worker {name} path is invalid: {error}") from error


def _judgment_result_document(result: JudgmentResult) -> dict[str, object]:
    return {
        "schemaVersion": result.schema_version,
        "itemId": result.item_id,
        "episode": result.episode,
        "evidenceFingerprint": result.evidence_fingerprint,
        "decision": result.decision.value,
        "summary": result.summary,
        "evidenceIds": list(result.evidence_ids),
        "inScopeJobIds": list(result.in_scope_job_ids),
        "copilotRequest": result.copilot_request,
        **(
            {
                "classification": result.classification.value,
                "recommendedResponse": result.recommended_response.value,
            }
            if result.classification is not None and result.recommended_response is not None
            else {}
        ),
    }


def _sync_standard_streams() -> None:
    for descriptor in (sys.stdout.fileno(), sys.stderr.fileno()):
        os.fsync(descriptor)


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--detail", type=Path, required=True)
    parser.add_argument("--lifetime-fd", type=int, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--reasoning-effort", required=True)
    parser.add_argument("--copilot-executable", default="copilot")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    return run_worker(
        request_path=arguments.request,
        result_path=arguments.result,
        detail_path=arguments.detail,
        lifetime_fd=arguments.lifetime_fd,
        model=arguments.model,
        reasoning_effort=arguments.reasoning_effort,
        copilot_executable=arguments.copilot_executable,
    )


if __name__ == "__main__":
    raise SystemExit(main())
