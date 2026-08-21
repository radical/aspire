#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path

from ci_shepherd.progress import ProgressTracker


def main() -> int:
    parser = argparse.ArgumentParser(description="Record CI shepherd stage progress.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument(
        "--status",
        choices=("started", "progress", "completed", "failed"),
        required=True,
    )
    parser.add_argument("--message")
    parser.add_argument("--completed-items", type=int)
    parser.add_argument("--total-items", type=int)
    parser.add_argument("--error")
    args = parser.parse_args()

    old_umask = os.umask(0o077)
    try:
        ProgressTracker(args.output_dir).update(
            args.stage,
            args.status,
            message=args.message,
            completed_items=args.completed_items,
            total_items=args.total_items,
            error=args.error,
        )
    finally:
        os.umask(old_umask)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
