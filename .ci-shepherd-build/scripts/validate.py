#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ci_shepherd.poc import validate_poc_judgments


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate CI shepherd POC judgments.")
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--judgments", type=Path, required=True)
    args = parser.parse_args()

    prepared = json.loads(args.prepared.read_text(encoding="utf-8"))
    judgments = json.loads(args.judgments.read_text(encoding="utf-8"))
    validate_poc_judgments(prepared, judgments)

    print("valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
