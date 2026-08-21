#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ci_shepherd.actions import build_action_proposals
from ci_shepherd.models import stable_json, validate_snapshot


def _load(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render review-only CI shepherd action proposals."
    )
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--agent-input", type=Path, required=True)
    parser.add_argument("--judgments", type=Path, required=True)
    parser.add_argument("--shepherd-author", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    old_umask = os.umask(0o077)
    try:
        snapshot = _load(args.snapshot)
        validate_snapshot(snapshot)
        proposals = build_action_proposals(
            snapshot,
            _load(args.prepared),
            _load(args.judgments),
            args.shepherd_author,
            agent_input=_load(args.agent_input),
        )
        args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        args.output.write_text(stable_json(proposals), encoding="utf-8")
        args.output.chmod(0o600)
    finally:
        os.umask(old_umask)

    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
