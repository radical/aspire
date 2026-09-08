#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ci_shepherd.investigations import (
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
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    args = parser.parse_args()

    old_umask = os.umask(0o077)
    try:
        event = record_investigation_result(
            args.state_dir,
            select_investigation_request(
                _load_object(args.plan, "Investigation plan"),
                args.investigation_id,
                state_directory=args.state_dir,
                prefer_recorded=True,
            ),
            _load_object(args.result, "Investigation result"),
            recorded_at=args.recorded_at,
            session_id=args.session_id,
            checkout=args.checkout,
        )
    finally:
        os.umask(old_umask)
    print(stable_json(event), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
