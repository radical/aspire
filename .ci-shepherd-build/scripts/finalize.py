#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile

from ci_shepherd.poc import merge_ambiguous_poc_judgments


def finalize(
    *,
    agent_input_path: Path,
    agent_judgments_path: Path,
    output_path: Path,
) -> Path:
    compact_input = json.loads(agent_input_path.resolve(strict=True).read_text(encoding="utf-8"))
    agent_judgments = json.loads(
        agent_judgments_path.resolve(strict=True).read_text(encoding="utf-8")
    )
    finalized = merge_ambiguous_poc_judgments(compact_input, agent_judgments)

    resolved_output = output_path.resolve()
    resolved_output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(resolved_output.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=resolved_output.parent,
        prefix=f".{resolved_output.name}.",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(finalized, stream, indent=2, sort_keys=True)
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
        description="Merge ambiguous agent judgments with safe deterministic defaults."
    )
    parser.add_argument("--agent-input", type=Path, required=True)
    parser.add_argument("--agent-judgments", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    output = finalize(
        agent_input_path=args.agent_input,
        agent_judgments_path=args.agent_judgments,
        output_path=args.output,
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
