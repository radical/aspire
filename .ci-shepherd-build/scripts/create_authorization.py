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
    DEFAULT_MAX_COPILOT_STARTS_PER_ROLLING_24H,
    DEFAULT_MAX_OPEN_DELEGATED_PRS,
    DEFAULT_MAX_REPOSITORY_RUNNING_COPILOT_TASKS,
    DEFAULT_MAX_RUNNING_COPILOT_TASKS,
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
        "--comment-selection",
        type=Path,
        help=(
            "The deterministic comment-selection artifact. Required for the "
            "production comment pilot."
        ),
    )
    parser.add_argument(
        "--action-id",
        dest="action_ids",
        action="append",
        default=[],
        required=True,
        help="Repeatable. Only these exact action ids will be authorized.",
    )
    parser.add_argument(
        "--max-running-copilot-tasks",
        type=_nonnegative_int,
        default=DEFAULT_MAX_RUNNING_COPILOT_TASKS,
    )
    parser.add_argument(
        "--max-copilot-starts-per-rolling-24h",
        type=_nonnegative_int,
        default=DEFAULT_MAX_COPILOT_STARTS_PER_ROLLING_24H,
    )
    parser.add_argument(
        "--max-open-delegated-prs",
        type=_nonnegative_int,
        default=DEFAULT_MAX_OPEN_DELEGATED_PRS,
    )
    parser.add_argument(
        "--max-repository-running-copilot-tasks",
        type=_nonnegative_int,
        default=DEFAULT_MAX_REPOSITORY_RUNNING_COPILOT_TASKS,
        help="Independent repository-wide runaway ceiling.",
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
            "Permit the deterministic bounded comment selection on "
            "microsoft/aspire under the production pilot limits."
        ),
    )
    parser.add_argument(
        "--production-delegation-pilot",
        action="store_true",
        help=(
            "Permit one Copilot assignment on microsoft/aspire under exact "
            "1/1/1 capacity limits."
        ),
    )
    parser.add_argument(
        "--production-delegation-steady-state",
        action="store_true",
        help=(
            "Permit up to five exact Copilot assignments on microsoft/aspire "
            "within the checked-in production capacity policy."
        ),
    )
    parser.add_argument(
        "--policy-selection",
        type=Path,
        help=(
            "The frozen policy-selection artifact (Task 3's selector "
            "output). Required with --autonomous-policy."
        ),
    )
    parser.add_argument(
        "--policy-action-id",
        help=(
            "The single actionId the policy selection licenses. Required "
            "with --autonomous-policy, and must equal the one --action-id."
        ),
    )
    parser.add_argument(
        "--autonomous-policy",
        action="store_true",
        help=(
            "Bind exactly one child grant to a named policy revision or "
            "exact decision recorded in the frozen policy selection, "
            "instead of a human-confirmed production pilot flag."
        ),
    )
    return parser


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("capacity limits must be nonnegative")
    return parsed


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
    policy_selection_path = (
        args.policy_selection.expanduser().absolute()
        if args.policy_selection is not None
        else None
    )
    if args.autonomous_policy:
        if len(args.action_ids) != 1:
            parser.error("--autonomous-policy allows exactly one --action-id")
        if policy_selection_path is None:
            parser.error("--autonomous-policy requires --policy-selection")
        if args.policy_action_id is None:
            parser.error("--autonomous-policy requires --policy-action-id")
        if args.policy_action_id != args.action_ids[0]:
            parser.error(
                "--policy-action-id must equal the single --action-id"
            )
    elif policy_selection_path is not None or args.policy_action_id is not None:
        parser.error(
            "--policy-selection and --policy-action-id require "
            "--autonomous-policy"
        )

    grant = generate_authorization_grant(
        proposals_path,
        action_ids=args.action_ids,
        state_dir=state_dir,
        comment_selection_path=args.comment_selection,
        ttl_minutes=args.ttl_minutes,
        max_running_copilot_tasks=args.max_running_copilot_tasks,
        max_copilot_starts_per_rolling_24h=(
            args.max_copilot_starts_per_rolling_24h
        ),
        max_open_delegated_prs=args.max_open_delegated_prs,
        max_repository_running_copilot_tasks=(
            args.max_repository_running_copilot_tasks
        ),
        override_suppression_for_action_ids=(
            args.override_suppression_for_action_ids
        ),
        allow_production_comment_pilot=args.production_comment_pilot,
        allow_production_delegation_pilot=args.production_delegation_pilot,
        allow_production_delegation_steady_state=(
            args.production_delegation_steady_state
        ),
        allow_autonomous_policy=args.autonomous_policy,
        policy_selection_path=policy_selection_path,
        policy_action_id=args.policy_action_id,
    )
    written_path = write_authorization_grant(grant, output_path)
    print(written_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
