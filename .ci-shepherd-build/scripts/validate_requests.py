#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ci_shepherd.models import validate_evidence_requests, validate_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a CI shepherd evidence-request handoff without expanding it."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--requests", type=Path, required=True)
    args = parser.parse_args()

    snapshot = json.loads(args.input.read_text(encoding="utf-8"))
    requests = json.loads(args.requests.read_text(encoding="utf-8"))
    validate_snapshot(snapshot)
    normalized = validate_evidence_requests(snapshot, requests)
    print(f"valid requests: {len(normalized)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
