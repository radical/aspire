#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import secrets
from pathlib import Path

from ci_shepherd.models import stable_json
from ci_shepherd.retrospective import (
    build_retrospective_context,
    build_retrospective_request,
    build_run_completion,
    normalize_retrospective_result,
    render_retrospective_markdown,
    retrospective_evidence_paths,
)


def _load_object(path: Path, label: str) -> dict[str, object]:
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError(f"{label} must not traverse a symlink.")
    document = json.loads(path.resolve(strict=True).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{label} must be an object.")
    return document


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _paths_alias(left: Path, right: Path) -> bool:
    if left.resolve(strict=False) == right.resolve(strict=False):
        return True
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return False


def _validate_outputs(
    outputs: tuple[Path, ...],
    *,
    inputs: tuple[Path, ...] = (),
) -> tuple[Path, ...]:
    normalized = tuple(_absolute(path) for path in outputs)
    for index, path in enumerate(normalized):
        if path.is_symlink():
            raise ValueError(f"Output path must not be a symlink: {path}")
        if any(parent.is_symlink() for parent in path.parents):
            raise ValueError(f"Output path must not traverse a symlink: {path}")
        if any(_paths_alias(path, other) for other in normalized[:index]):
            raise ValueError("Retrospective output paths must be distinct.")
        if any(_paths_alias(path, _absolute(input_path)) for input_path in inputs):
            raise ValueError("Retrospective output must not overwrite an input.")
        if path.exists():
            raise ValueError(f"Retrospective output already exists; use a new review path: {path}")
    return normalized


def _write_private(path: Path, content: str) -> None:
    path = _absolute(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        # Install without replacement, including when another writer creates the
        # destination after validation. Previously sealed evidence is immutable.
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare or finalize one bounded CI shepherd run retrospective."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    context = subparsers.add_parser("context", help="Build validated bindings for explicitly selected run evidence.")
    context.add_argument("--work-dir", type=Path, required=True)
    context.add_argument("--invocation", type=Path, required=True)
    context.add_argument("--operator-report", type=Path, help="Optional final operator report.")
    context.add_argument("--output", type=Path, required=True)

    seal = subparsers.add_parser("seal")
    seal.add_argument("--work-dir", type=Path, required=True)
    seal.add_argument("--state-dir", type=Path, required=True)
    seal.add_argument("--sealed-at", required=True)
    seal.add_argument("--context", type=Path, help="Explicit digest-bound invocation/context manifest.")
    seal.add_argument("--output", type=Path, required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--work-dir", type=Path, required=True)
    prepare.add_argument("--reviewed-session-id", required=True)
    prepare.add_argument("--context", type=Path, help="Explicit digest-bound invocation/context manifest.")
    prepare.add_argument("--completion", type=Path, help="Read a new seal without modifying the original run.")
    prepare.add_argument("--output", type=Path, required=True)

    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--request", type=Path, required=True)
    finalize.add_argument("--result", type=Path, required=True)
    finalize.add_argument("--json-output", type=Path, required=True)
    finalize.add_argument("--markdown-output", type=Path, required=True)
    args = parser.parse_args()

    old_umask = os.umask(0o077)
    try:
        if args.command == "context":
            (output,) = _validate_outputs(
                (args.output,),
                inputs=(
                    *retrospective_evidence_paths(args.work_dir),
                    args.invocation,
                    *((args.operator_report,) if args.operator_report else ()),
                ),
            )
            context = build_retrospective_context(
                args.work_dir, args.invocation, operator_report_path=args.operator_report,
            )
            _write_private(output, stable_json(context))
        elif args.command == "seal":
            completion = build_run_completion(
                args.work_dir, args.state_dir, sealed_at=args.sealed_at,
                context_path=args.context,
            )
            (output,) = _validate_outputs(
                (args.output,),
                inputs=(
                    *(
                        path
                        for path in retrospective_evidence_paths(args.work_dir)
                        if path.name != "run-completion.json"
                    ),
                    args.state_dir / "action-events.jsonl",
                    args.state_dir / "action-results.json",
                    args.state_dir / "ledgers" / "investigation-results.jsonl",
                    args.state_dir / "ledgers" / "investigation-sessions.jsonl",
                    args.state_dir / "ledgers" / "quarantine-sessions.jsonl",
                    *((args.context,) if args.context else ()),
                    *(Path(value) for value in completion.get("context", {}).get("sourcePaths", {}).values()),
                ),
            )
            _write_private(output, stable_json(completion))
        elif args.command == "prepare":
            request = build_retrospective_request(
                args.work_dir, reviewed_session_id=args.reviewed_session_id,
                context_path=args.context, completion_path=args.completion,
            )
            (output,) = _validate_outputs(
                (args.output,),
                inputs=(
                    *retrospective_evidence_paths(args.work_dir),
                    *((args.context,) if args.context else ()),
                    *((args.completion,) if args.completion else ()),
                    *(Path(value) for value in request["sourcePaths"]),
                ),
            )
            _write_private(output, stable_json(request))
        else:
            request = _load_object(args.request, "Retrospective request")
            json_output, markdown_output = _validate_outputs(
                (args.json_output, args.markdown_output),
                inputs=(
                    args.request,
                    args.result,
                    *retrospective_evidence_paths(args.request.parent),
                    *(Path(value) for value in request.get("sourcePaths", [])),
                ),
            )
            result = normalize_retrospective_result(
                request,
                _load_object(args.result, "Retrospective result"),
            )
            json_content = stable_json(result)
            markdown_content = render_retrospective_markdown(request, result)
            _write_private(json_output, json_content)
            _write_private(markdown_output, markdown_content)
            output = markdown_output
    finally:
        os.umask(old_umask)

    print(_absolute(output).resolve(strict=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
