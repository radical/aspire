#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile

from ci_shepherd.lifecycle import DEFAULT_MAX_BUNDLE_RECORDS, prepare_assessment
from ci_shepherd.models import validate_snapshot


def prepare(
    *,
    input_path: Path,
    output_path: Path,
    max_bundle_records: int,
) -> Path:
    resolved_input = input_path.resolve(strict=True)
    resolved_output = output_path.resolve()
    if resolved_input == resolved_output:
        raise ValueError("Assessment input must not overwrite the source snapshot.")

    snapshot = json.loads(resolved_input.read_text(encoding="utf-8"))
    validate_snapshot(snapshot)
    assessment = prepare_assessment(
        snapshot,
        max_bundle_records=max_bundle_records,
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
            json.dump(assessment, stream, indent=2, sort_keys=True)
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
        description="Prepare bounded lifecycle candidates for CI shepherd assessment."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--max-bundle-records",
        type=int,
        default=DEFAULT_MAX_BUNDLE_RECORDS,
    )
    args = parser.parse_args()

    output = prepare(
        input_path=args.input,
        output_path=args.output,
        max_bundle_records=args.max_bundle_records,
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
