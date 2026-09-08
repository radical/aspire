#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ci_shepherd.investigations import (
    record_investigation_session_event,
    select_investigation_request,
)
from ci_shepherd.models import stable_json


def _select_request(
    path: Path,
    investigation_id: str,
    state_directory: Path,
    *,
    prefer_recorded: bool,
) -> dict[str, object]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("Investigation plan must be an object.")
    return select_investigation_request(
        document,
        investigation_id,
        state_directory=state_directory,
        prefer_recorded=prefer_recorded,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record the lifecycle of one bounded investigation session."
    )
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--investigation-id", required=True)
    parser.add_argument(
        "--status",
        choices=("started", "failed", "abandoned"),
        required=True,
    )
    parser.add_argument("--recorded-at", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--checkout", type=Path)
    parser.add_argument("--failure-reason")
    parser.add_argument(
        "--failure-category",
        choices=(
            "worker-error",
            "invalid-result",
            "out-of-scope-evidence",
            "worker-unavailable",
        ),
    )
    parser.add_argument("--confirm-worker-stopped", action="store_true")
    parser.add_argument(
        "--allow-reproduction-command", action="append", type=json.loads,
        help="Explicitly authorize one exact JSON argv array for this local session; repeat up to three times. Never shell text.",
    )
    args = parser.parse_args()

    request = _select_request(
        args.plan, args.investigation_id, args.state_dir,
        prefer_recorded=args.status != "started",
    )
    if args.status == "started" and (
        request.get("investigationScope") is None or request.get("sourceRevision") is None
    ):
        parser.error(
            "Fresh investigation sessions require a frozen source revision and an owned worktree; "
            "recollect the legacy request before starting a worker."
        )
    old_umask = os.umask(0o077)
    try:
        event = record_investigation_session_event(
            args.state_dir,
            request,
            status=args.status,
            recorded_at=args.recorded_at,
            session_id=args.session_id,
            checkout=args.checkout,
            failure_reason=args.failure_reason,
            failure_category=args.failure_category,
            confirm_worker_stopped=args.confirm_worker_stopped,
            reproduction_commands=args.allow_reproduction_command,
        )
    finally:
        os.umask(old_umask)
    print(stable_json(event), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
