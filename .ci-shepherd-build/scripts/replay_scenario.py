#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ci_shepherd.replay import replay_lifecycle_scenario


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Replay frozen finalized CI shepherd cycles through one shared "
            "lifecycle state directory."
        )
    )
    parser.add_argument("--scenario-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()

    old_umask = os.umask(0o077)
    try:
        summary = replay_lifecycle_scenario(
            scenario_directory=args.scenario_dir,
            output_directory=args.output_dir,
            state_directory=args.state_dir,
        )
    finally:
        os.umask(old_umask)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
