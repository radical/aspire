#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ci_shepherd.poc_history import append_new_rows, collect_rows_from_prepared


def record_fingerprints(*, prepared_path: Path, output_path: Path) -> Path:
    resolved_prepared = prepared_path.resolve(strict=True)
    resolved_output = output_path.resolve()
    if resolved_prepared == resolved_output:
        raise ValueError("Fingerprint ledger must not overwrite the prepared assessment.")

    prepared = json.loads(resolved_prepared.read_text(encoding="utf-8"))
    rows = collect_rows_from_prepared(prepared)
    append_new_rows(resolved_output, rows)
    return resolved_output


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Record CI shepherd occurrence fingerprints from a prepared assessment "
            "into an append-only JSONL ledger, so recurrence survives issue closure."
        )
    )
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    output = record_fingerprints(prepared_path=args.prepared, output_path=args.output)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
