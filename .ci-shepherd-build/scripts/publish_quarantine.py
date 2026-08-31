#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ci_shepherd.models import stable_json
from ci_shepherd.quarantine_mutation import write_quarantine_validation
from ci_shepherd.quarantine_publish import publish_quarantine_pull_request
from quarantine_session import _load_request


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Push an exact validated quarantine commit and create its draft PR."
        )
    )
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--mutation-result", type=Path, required=True)
    parser.add_argument("--commit-validation", type=Path, required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--body-file", type=Path, required=True)
    parser.add_argument("--mutation-audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    old_umask = os.umask(0o077)
    try:
        request = _load_request(
            args.request,
            args.state_dir,
            args.batch_id,
        )
        mutation_result = _load_object(
            args.mutation_result,
            "Quarantine mutation result",
        )
        commit_validation = _load_object(
            args.commit_validation,
            "Quarantine commit validation",
        )
        result = publish_quarantine_pull_request(
            request=request,
            mutation_result=mutation_result,
            commit_validation=commit_validation,
            checkout=args.checkout,
            state_directory=args.state_dir,
            session_id=args.session_id,
            body_file=args.body_file,
            audit_path=args.mutation_audit,
        )
        write_quarantine_validation(args.output, result)
    finally:
        os.umask(old_umask)

    print(stable_json(result), end="")
    return 0


def _load_object(path: Path, description: str) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{description} must be a JSON object.")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
