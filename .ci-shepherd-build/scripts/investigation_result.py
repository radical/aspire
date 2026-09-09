#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ci_shepherd.investigations import (
    load_one_shot_result,
    record_investigation_result,
    select_investigation_request,
)
from ci_shepherd.models import stable_json


def _load_object(path: Path, label: str) -> dict[str, object]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{label} must be an object.")
    return document


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate and record one bounded investigation result."
    )
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--investigation-id", required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--recorded-at", required=True)
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--session-id", help="Actual resumable runtime session ID.")
    identity.add_argument("--attempt-id", help="Logical one-shot attempt ID from preparation.")
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--execution-evidence", help="Observed ended-invocation evidence, required for one-shot results.")
    parser.add_argument("--confirm-worker-stopped", action="store_true")
    args = parser.parse_args()

    old_umask = os.umask(0o077)
    try:
        request = select_investigation_request(
            _load_object(args.plan, "Investigation plan"), args.investigation_id,
            state_directory=args.state_dir, prefer_recorded=True,
        )
        response = (
            load_one_shot_result(
                args.state_dir, request, checkout=args.checkout, attempt_id=args.attempt_id, result_path=args.result,
            ) if args.attempt_id is not None else _load_object(args.result, "Investigation result")
        )
        event = record_investigation_result(
            args.state_dir, request, response,
            recorded_at=args.recorded_at,
            session_id=args.session_id,
            checkout=args.checkout,
            attempt_id=args.attempt_id,
            execution_evidence=args.execution_evidence,
            confirm_worker_stopped=args.confirm_worker_stopped,
        )
    finally:
        os.umask(old_umask)
    print(stable_json(event), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
