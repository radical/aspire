#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile

from ci_shepherd.poc import build_compact_poc_input
from ci_shepherd.poc_history import group_rows_by_fingerprint, read_ledger_rows


def compact(
    *,
    prepared_path: Path,
    related_issues_path: Path | None,
    fingerprints_path: Path | None,
    output_path: Path,
) -> Path:
    resolved_prepared = prepared_path.resolve(strict=True)
    resolved_output = output_path.resolve()
    if resolved_prepared == resolved_output:
        raise ValueError("Compact agent input must not overwrite the prepared assessment.")

    prepared = json.loads(resolved_prepared.read_text(encoding="utf-8"))
    related_issue_matches = None
    if related_issues_path is not None:
        resolved_related_issues = related_issues_path.resolve(strict=True)
        if resolved_related_issues == resolved_output:
            raise ValueError("Compact agent input must not overwrite frozen related issues.")
        related_issue_matches = json.loads(
            resolved_related_issues.read_text(encoding="utf-8")
        )
    history_occurrences = None
    if fingerprints_path is not None:
        resolved_fingerprints = fingerprints_path.resolve()
        if resolved_fingerprints == resolved_output:
            raise ValueError("Compact agent input must not overwrite the fingerprint ledger.")
        history_occurrences = group_rows_by_fingerprint(read_ledger_rows(resolved_fingerprints))
    compact_input = build_compact_poc_input(
        prepared,
        related_issue_matches=related_issue_matches,
        history_occurrences=history_occurrences,
    )

    resolved_output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(resolved_output.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=resolved_output.parent,
        prefix=f".{resolved_output.name}.",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(compact_input, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, resolved_output)
    finally:
        temporary_path.unlink(missing_ok=True)
    return resolved_output


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compact a prepared CI shepherd assessment for a fresh agent."
    )
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--related-issues", type=Path)
    parser.add_argument("--fingerprints", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    output = compact(
        prepared_path=args.prepared,
        related_issues_path=args.related_issues,
        fingerprints_path=args.fingerprints,
        output_path=args.output,
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
