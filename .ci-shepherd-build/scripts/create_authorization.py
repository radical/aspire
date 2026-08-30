#!/usr/bin/env python3
"""Mint a short-lived, exact authorization grant for explicitly named actions.

This CLI never infers authorization: every action id it grants must be named
on the command line, and a selected action whose `dependsOn` is not also
named is rejected rather than silently included. It performs no GitHub calls
and executes nothing; it only derives and writes a grant that a later
`execute_actions.py --execute` can load and check.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from ci_shepherd.authorization import (
    DEFAULT_GRANT_TTL_MINUTES,
    generate_authorization_grant,
    write_authorization_grant,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate an exact, short-lived authorization grant for "
            "explicitly selected CI shepherd action ids."
        )
    )
    parser.add_argument("--proposals", required=True, type=Path)
    parser.add_argument(
        "--action-id",
        dest="action_ids",
        action="append",
        default=[],
        required=True,
        help="Repeatable. Only these exact action ids will be authorized.",
    )
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--ttl-minutes",
        type=int,
        default=DEFAULT_GRANT_TTL_MINUTES,
        help=f"Grant lifetime in minutes (default: {DEFAULT_GRANT_TTL_MINUTES}).",
    )
    parser.add_argument(
        "--override-suppression-for-action-id",
        dest="override_suppression_for_action_ids",
        action="append",
        default=[],
        help=(
            "Repeatable. Must also be a --action-id. Never applied unless "
            "named explicitly here."
        ),
    )
    parser.add_argument(
        "--production-comment-pilot",
        action="store_true",
        help=(
            "Permit one comment action on microsoft/aspire under the bounded "
            "production pilot limits."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    proposals_path = args.proposals.expanduser().absolute()
    state_dir = args.state_dir.expanduser().absolute()
    if state_dir.exists() and state_dir.is_symlink():
        parser.error("--state-dir must not be a symlink")
    output_path = args.output.expanduser().absolute()
    if output_path.exists() and output_path.is_symlink():
        parser.error("--output must not be a symlink")

    grant = generate_authorization_grant(
        proposals_path,
        action_ids=args.action_ids,
        state_dir=state_dir,
        ttl_minutes=args.ttl_minutes,
        override_suppression_for_action_ids=(
            args.override_suppression_for_action_ids
        ),
        allow_production_comment_pilot=args.production_comment_pilot,
    )
    written_path = write_authorization_grant(grant, output_path)
    print(written_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
