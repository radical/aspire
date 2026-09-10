#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ci_shepherd.investigations import (
    record_investigation_session_event,
    select_investigation_request,
    select_recorded_investigation_session,
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
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--investigation-id")
    parser.add_argument(
        "--recover-recorded",
        action="store_true",
        help="Recover one exact stopped resumable session from its persisted registration without a historical plan.",
    )
    parser.add_argument(
        "--status",
        choices=("started", "prepared", "dispatching", "failed", "abandoned"),
        required=True,
    )
    parser.add_argument("--recorded-at", required=True)
    identity = parser.add_mutually_exclusive_group()
    identity.add_argument("--session-id", help="Actual addressable runtime session ID (resumable workers only).")
    identity.add_argument("--attempt-id", help="Logical registry attempt ID; never a runtime session ID.")
    parser.add_argument("--launch-mode", choices=("resumable", "one-shot"), default="resumable")
    parser.add_argument("--checkout", type=Path)
    parser.add_argument("--result-path", type=Path, help="Exact external <attemptId>.json path to freeze during preparation.")
    parser.add_argument("--execution-state", choices=("not-launched", "returned", "unknown"))
    parser.add_argument("--execution-evidence", help="Observed launcher/return evidence; not inferred from a local identifier.")
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

    recorded = None
    if args.recover_recorded:
        if args.plan is not None or args.investigation_id is not None:
            parser.error("--recover-recorded selects by session ID and cannot use --plan or --investigation-id.")
        if args.session_id is None:
            parser.error("--recover-recorded requires --session-id.")
        if args.status not in {"failed", "abandoned"} or not args.confirm_worker_stopped:
            parser.error("--recover-recorded requires a failed or abandoned status and --confirm-worker-stopped.")
        if (
            args.launch_mode != "resumable"
            or args.attempt_id is not None
            or args.result_path is not None
            or args.execution_state is not None
            or args.execution_evidence is not None
            or args.allow_reproduction_command is not None
        ):
            parser.error("--recover-recorded supports only resumable session terminalization.")
        try:
            request, recorded = select_recorded_investigation_session(
                args.state_dir, args.session_id,
            )
        except (ValueError, OSError) as error:
            parser.error(str(error))
        if recorded["status"] in {"failed", "abandoned"}:
            if args.status != recorded["status"]:
                parser.error("--recover-recorded cannot change the recorded terminal status.")
            if args.failure_reason is None:
                args.failure_reason = recorded.get("failureReason")
            if args.failure_category is None:
                args.failure_category = recorded.get("failureCategory")
        if args.checkout is None and (
            args.status == "abandoned" or request.get("investigationScope") is not None
        ):
            checkout_path = recorded.get("checkoutPath")
            if not isinstance(checkout_path, str) or not checkout_path:
                parser.error("The recorded session has no checkout path for terminal reconciliation.")
            args.checkout = Path(checkout_path)
    else:
        if args.plan is None or args.investigation_id is None:
            parser.error("--plan and --investigation-id are required unless --recover-recorded is used.")
        request = _select_request(
            args.plan, args.investigation_id, args.state_dir,
            prefer_recorded=args.status not in {"started", "prepared"},
        )
    if args.status in {"started", "prepared"} and (
        request.get("investigationScope") is None or request.get("sourceRevision") is None
    ):
        parser.error(
            "Fresh investigation sessions require a frozen source revision and an owned worktree; "
            "recollect the legacy request before starting a worker."
        )
    old_umask = os.umask(0o077)
    try:
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
                launch_mode=args.launch_mode,
                attempt_id=args.attempt_id,
                result_path=args.result_path,
                execution_state=args.execution_state,
                execution_evidence=args.execution_evidence,
                require_unique_session_identity=args.recover_recorded,
            )
        except (ValueError, OSError) as error:
            parser.error(str(error))
    finally:
        os.umask(old_umask)
    print(stable_json(event), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
