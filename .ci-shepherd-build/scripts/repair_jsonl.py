#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ci_shepherd.jsonl import repair_incomplete_jsonl_row
from ci_shepherd.models import stable_json


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Replace one incomplete JSONL ledger row while preserving the corrupt file."
        )
    )
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--replacement-row", type=Path, required=True)
    args = parser.parse_args()

    replacement = _read_json_object(args.replacement_row)
    backup = repair_incomplete_jsonl_row(args.ledger, replacement)
    print(
        stable_json(
            {
                "schemaVersion": 1,
                "ledger": str(args.ledger),
                "corruptBackup": str(backup),
            }
        ),
        end="",
    )
    return 0


def _read_json_object(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("Replacement row must not be a symlink.")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Replacement row contains duplicate key {key!r}.")
            result[key] = value
        return result

    document = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicates,
    )
    if not isinstance(document, dict):
        raise ValueError("Replacement row must be a JSON object.")
    return document


if __name__ == "__main__":
    raise SystemExit(main())
