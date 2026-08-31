#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ci_shepherd.models import stable_json
from ci_shepherd.quarantine_mutation import (
    create_quarantine_commit_validation,
    write_quarantine_validation,
)
from quarantine_session import _load_request


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bind a quarantine commit to its validated mutation diff."
    )
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--mutation-result", type=Path, required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    request = _load_request(args.request, args.state_dir, args.batch_id)
    mutation_result = json.loads(
        args.mutation_result.read_text(encoding="utf-8")
    )
    if not isinstance(mutation_result, dict):
        raise ValueError("Quarantine mutation result must be an object.")
    validated = create_quarantine_commit_validation(
        request,
        mutation_result,
        args.checkout,
        args.commit,
    )
    write_quarantine_validation(args.output, validated)
    print(stable_json(validated), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
