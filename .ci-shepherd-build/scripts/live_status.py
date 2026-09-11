#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

from ci_shepherd.live_status import build_live_status, is_terminal, write_live_status


def _render(
    invocation_dir: Path,
    work_dir: Path,
    state_dir: Path | None,
) -> dict[str, object]:
    status = build_live_status(invocation_dir, work_dir, state_dir=state_dir)
    write_live_status(invocation_dir, status)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render bounded advisory live status for one CI shepherd invocation.",
    )
    parser.add_argument("command", choices=("render", "watch"))
    parser.add_argument("--invocation-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--interval-seconds", type=float, default=5)
    parser.add_argument("--heartbeat-seconds", type=float, default=30)
    args = parser.parse_args()
    if args.interval_seconds <= 0 or args.heartbeat_seconds <= 0:
        parser.error("interval and heartbeat seconds must be positive")

    if args.command == "render":
        _render(args.invocation_dir, args.work_dir, args.state_dir)
        return 0

    last_projection = None
    last_write = 0.0
    while True:
        terminal = is_terminal(args.invocation_dir)
        try:
            status = build_live_status(
                args.invocation_dir,
                args.work_dir,
                state_dir=args.state_dir,
            )
            projection = json.dumps(
                {key: value for key, value in status.items() if key != "generatedAt"},
                sort_keys=True,
                separators=(",", ":"),
            )
            current = time.monotonic()
            if projection != last_projection or current - last_write >= args.heartbeat_seconds:
                write_live_status(args.invocation_dir, status)
                last_projection = projection
                last_write = current
            if terminal:
                return 0
        except (OSError, UnicodeError, ValueError) as error:
            print(f"live-status advisory update failed: {error}", file=sys.stderr, flush=True)
            if terminal:
                return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
