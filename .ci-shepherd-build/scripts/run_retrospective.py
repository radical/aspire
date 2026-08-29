#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ci_shepherd.models import stable_json
from ci_shepherd.retrospective import (
    build_retrospective_request,
    build_run_completion,
    normalize_retrospective_result,
    render_retrospective_markdown,
    retrospective_evidence_paths,
)


def _load_object(path: Path, label: str) -> dict[str, object]:
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
    return normalized


def _write_private(path: Path, content: str) -> None:
    path = _absolute(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare or finalize one bounded CI shepherd run retrospective."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    seal = subparsers.add_parser("seal")
    seal.add_argument("--work-dir", type=Path, required=True)
    seal.add_argument("--state-dir", type=Path, required=True)
    seal.add_argument("--sealed-at", required=True)
    seal.add_argument("--output", type=Path, required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--work-dir", type=Path, required=True)
    prepare.add_argument("--reviewed-session-id", required=True)
    prepare.add_argument("--output", type=Path, required=True)

    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--request", type=Path, required=True)
    finalize.add_argument("--result", type=Path, required=True)
    finalize.add_argument("--json-output", type=Path, required=True)
    finalize.add_argument("--markdown-output", type=Path, required=True)
    args = parser.parse_args()

    old_umask = os.umask(0o077)
    try:
        if args.command == "seal":
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
                ),
            )
            completion = build_run_completion(
                args.work_dir,
                args.state_dir,
                sealed_at=args.sealed_at,
            )
            _write_private(output, stable_json(completion))
        elif args.command == "prepare":
            (output,) = _validate_outputs(
                (args.output,),
                inputs=retrospective_evidence_paths(args.work_dir),
            )
            request = build_retrospective_request(
                args.work_dir,
                reviewed_session_id=args.reviewed_session_id,
            )
            _write_private(output, stable_json(request))
        else:
            json_output, markdown_output = _validate_outputs(
                (args.json_output, args.markdown_output),
                inputs=(
                    args.request,
                    args.result,
                    *retrospective_evidence_paths(args.request.parent),
                ),
            )
            request = _load_object(args.request, "Retrospective request")
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
