#!/usr/bin/env python3
"""Provision and inventory private, explicitly owned investigation worktrees."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from ci_shepherd.investigation_worktrees import (
    bind_investigation_worktree,
    cleanup_investigation_worktree,
    finish_investigation_worktree,
    list_investigation_worktrees,
    provision_investigation_worktree,
    reconcile_investigation_worktree,
    validate_investigation_worktree,
)
from ci_shepherd.investigations import select_investigation_request
from ci_shepherd.models import stable_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    for name in ("provision", "list", "bind", "verify", "finish", "reconcile", "cleanup"):
        command = commands.add_parser(name)
        command.add_argument("--state-dir", type=Path, required=True)
        if name == "list":
            continue
        if name != "verify":
            command.add_argument("--recorded-at", required=True)
        if name == "provision":
            command.add_argument("--plan", type=Path, required=True)
            command.add_argument("--investigation-id", required=True)
            command.add_argument("--source-checkout", type=Path, required=True)
            command.add_argument("--attempt", type=int, required=True)
            command.add_argument("--managed-root", type=Path)
        else:
            command.add_argument("--ownership-id", required=True)
        if name in {"bind", "verify", "finish", "cleanup"}:
            command.add_argument("--session-id", required=name == "bind")
        if name == "finish":
            command.add_argument("--status", choices=("completed", "failed", "abandoned"), required=True)
        if name in {"finish", "cleanup"}:
            command.add_argument(
                "--confirm-worker-stopped", action="store_true",
                help="Operator assertion after checking the runtime or observing the one-shot invocation end; this CLI cannot verify worker processes.",
            )
    args = parser.parse_args()
    try:
        if args.operation == "list":
            output = {"schemaVersion": 1, "worktrees": list_investigation_worktrees(args.state_dir)}
        elif args.operation == "provision":
            plan = json.loads(args.plan.read_text(encoding="utf-8"))
            if not isinstance(plan, dict):
                raise ValueError("Investigation plan must be an object.")
            request = select_investigation_request(
                plan, args.investigation_id, state_directory=args.state_dir,
            )
            output = provision_investigation_worktree(
                args.state_dir, request, source_checkout=args.source_checkout,
                attempt=args.attempt, recorded_at=args.recorded_at, managed_root=args.managed_root,
            )
        else:
            matches = [
                row for row in list_investigation_worktrees(args.state_dir)
                if row["ownershipId"] == args.ownership_id
            ]
            if len(matches) != 1:
                raise ValueError("No exact ownershipId exists in this registry.")
            record = matches[0]
            if args.operation == "finish" and record.get("launchMode") == "one-shot":
                raise ValueError("Record the one-shot terminal outcome with investigation_session.py or investigation_result.py before cleanup.")
            kwargs = {"checkout": Path(record["checkoutPath"])}
            if args.operation != "verify":
                kwargs["recorded_at"] = args.recorded_at
            if args.operation in {"bind", "verify", "finish", "cleanup"}:
                kwargs["session_id"] = args.session_id
            if args.operation in {"verify", "finish", "cleanup"} and record.get("launchMode") == "one-shot":
                kwargs["attempt_id"] = record["attemptId"]
            if args.operation in {"finish", "cleanup"}:
                kwargs["confirm_worker_stopped"] = args.confirm_worker_stopped
            if args.operation == "finish":
                kwargs["status"] = args.status
            operations = {
                "bind": bind_investigation_worktree,
                "verify": validate_investigation_worktree,
                "finish": finish_investigation_worktree,
                "reconcile": reconcile_investigation_worktree,
                "cleanup": cleanup_investigation_worktree,
            }
            output = operations[args.operation](args.state_dir, record["request"], **kwargs)
    except (ValueError, OSError) as error:
        print(f"Investigation worktree: {error}", file=sys.stderr)
        return 2
    print(stable_json(output), end="")
    if args.operation == "reconcile" and output.get("error"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
