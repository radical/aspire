from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import subprocess
import time
from typing import Sequence

from ci_shepherd.actor import build_dry_run, execute_action, reconcile_action
from ci_shepherd.authorization import load_authorized_execution
from ci_shepherd.coordinator_state import (
    CoordinatorStateStore,
    make_lock_free_durable_intent_reader,
)
from ci_shepherd.delegation_execution import (
    finalize_delegation_result,
    reserve_delegation_start,
)
from ci_shepherd.delegations import CapacityLimits
from ci_shepherd.execution_state import ActionEventStore
from ci_shepherd.github import GitHubClient
from ci_shepherd.github_actor import GitHubActorClient
from ci_shepherd.policy_budget import CoordinatorPolicyBudgetValidator


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview or execute one validated CI shepherd action."
    )
    parser.add_argument("--proposals", required=True, type=Path)
    parser.add_argument(
        "--comment-selection",
        type=Path,
        help=(
            "The deterministic comment-selection artifact bound by a "
            "production comment authorization."
        ),
    )
    parser.add_argument(
        "--source-checkout",
        type=Path,
        help=(
            "Checkout whose pinned quarantine source evidence must still match "
            "before a source-reconciliation action."
        ),
    )
    result_location = parser.add_mutually_exclusive_group()
    result_location.add_argument("--results", type=Path)
    result_location.add_argument(
        "--state-dir",
        type=Path,
        help="Persist execution history as STATE_DIR/action-results.json.",
    )
    parser.add_argument("--action-id")
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--production-comment-pilot",
        action="store_true",
        help=(
            "Permit one action from an authorized deterministic comment selection "
            "on microsoft/aspire."
        ),
    )
    parser.add_argument(
        "--production-delegation-pilot",
        action="store_true",
        help=(
            "Permit an authorized one-assignment pilot on microsoft/aspire."
        ),
    )
    parser.add_argument(
        "--production-delegation-steady-state",
        action="store_true",
        help=(
            "Permit an authorized policy-bounded assignment batch on "
            "microsoft/aspire."
        ),
    )
    parser.add_argument(
        "--autonomous-policy",
        action="store_true",
        help=(
            "Permit an authorized one-action grant bound to a coordinator "
            "policy selection on microsoft/aspire."
        ),
    )
    parser.add_argument(
        "--policy-selection",
        type=Path,
        help=(
            "The deterministic policy-selection artifact bound by an "
            "autonomous-policy authorization."
        ),
    )
    return parser


def _load_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to read JSON document: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return payload


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.execute and args.action_id is None:
        parser.error("--execute requires --action-id")
    if args.execute and args.authorization is None:
        parser.error("--execute requires --authorization")
    if args.execute and args.results is not None:
        parser.error("--execute requires --state-dir; --results is dry-run only")
    if args.execute and args.state_dir is None:
        parser.error("--execute requires --state-dir")

    proposals_path = args.proposals.expanduser().absolute()
    if args.state_dir is not None:
        state_dir = args.state_dir.expanduser().absolute()
        if state_dir.exists() and state_dir.is_symlink():
            parser.error("--state-dir must not be a symlink")
    else:
        state_dir = None
    results_path = args.results.resolve() if args.results is not None else None
    if results_path is not None and proposals_path == results_path:
        parser.error("--proposals and --results must be different paths")

    if not args.execute:
        proposals = _load_json(proposals_path)
        _print_json(build_dry_run(proposals, action_id=args.action_id))
        return 0
    assert state_dir is not None

    authorized = load_authorized_execution(
        proposals_path,
        args.authorization,
        state_dir=args.state_dir,
        action_id=args.action_id,
        comment_selection_path=args.comment_selection,
        source_checkout_path=args.source_checkout,
        allow_production_comment_pilot=args.production_comment_pilot,
        allow_production_delegation_pilot=args.production_delegation_pilot,
        allow_production_delegation_steady_state=(
            args.production_delegation_steady_state
        ),
        allow_autonomous_policy=args.autonomous_policy,
        policy_selection_path=args.policy_selection,
    )
    proposals = authorized.proposal_document
    proposal = authorized.proposal
    body = proposal.get("body")
    body_digest = (
        f"sha256:{hashlib.sha256(body.encode('utf-8')).hexdigest()}"
        if isinstance(body, str)
        else None
    )
    store = ActionEventStore(
        state_dir,
        policy_budget_validator=CoordinatorPolicyBudgetValidator(
            CoordinatorStateStore(
                state_dir,
                durable_intent_reader=make_lock_free_durable_intent_reader(
                    state_dir / "action-events.jsonl"
                ),
            )
        ),
    )
    store.migrate_legacy_results()
    with store.transaction(
        authorized.grant,
        action_id=args.action_id,
        chain_root=authorized.chain_root,
        operation=str(proposal["operation"]),
        target_kind="issue",
        target_number=int(proposal["issueNumber"]),
        idempotency_key=str(proposal["idempotencyKey"]),
        body_digest=body_digest,
        expected_actor_login=str(proposals["shepherdAuthor"]),
        at=datetime.now(UTC),
    ) as execution:
        reservation = execution.reservation
        if reservation.mode == "terminal":
            assert reservation.prior_terminal is not None
            result = {
                key: value
                for key, value in reservation.prior_terminal.items()
                if key
                not in {
                    "schemaVersion",
                    "eventType",
                    "recordedAt",
                    "grantId",
                    "repository",
                    "snapshotId",
                    "chainRoot",
                    "operation",
                    "target",
                    "idempotencyKey",
                    "bodyDigest",
                    "expectedActorLogin",
                }
            }
            _print_json(result)
            return 0

        production_comment_overrides = (
            {authorized.grant.repository}
            if authorized.grant.production_comment_pilot
            else set()
        )
        production_delegation_overrides = (
            {authorized.grant.repository}
            if (
                authorized.grant.production_delegation_pilot
                or authorized.grant.production_delegation_steady_state
            )
            else set()
        )
        client = GitHubActorClient(
            allowed_repositories={authorized.grant.repository},
            protected_comment_repositories=production_comment_overrides,
            protected_delegation_repositories=production_delegation_overrides,
            audit_path=state_dir / "api-calls.jsonl",
        )
        operation = str(proposal["operation"])
        delegation_reader = (
            GitHubClient(
                runner=subprocess.run,
                popen_factory=subprocess.Popen,
                sleep=time.sleep,
                now=lambda: datetime.now(UTC),
                audit_path=state_dir / "api-calls.jsonl",
            )
            if operation == "assign-copilot"
            else None
        )
        delegation_baseline: tuple[str, ...] | None = None
        if operation == "assign-copilot" and reservation.mode == "execute":
            assert delegation_reader is not None
            capacity = reserve_delegation_start(
                execution=execution,
                client=delegation_reader,
                repository=authorized.grant.repository,
                limits=CapacityLimits(
                    max_running_tasks=(
                        authorized.grant.budget.max_running_copilot_tasks
                    ),
                    max_starts_per_rolling_24h=(
                        authorized.grant.budget
                        .max_copilot_starts_per_rolling_24h
                    ),
                    max_open_delegated_prs=(
                        authorized.grant.budget.max_open_delegated_prs
                    ),
                    max_repository_running_tasks=(
                        authorized.grant.budget
                        .max_repository_running_copilot_tasks
                    ),
                ),
                now=datetime.now(UTC),
            )
            if not capacity.permitted:
                result = {
                    "actionId": args.action_id,
                    "attemptedAt": datetime.now(UTC)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "outcome": "deferred",
                    "reason": "delegation-capacity-blocked",
                    "blockedBy": list(capacity.blocked_by),
                }
                _print_json(result)
                return 0
            delegation_baseline = capacity.task_ids_before
        elif operation == "assign-copilot":
            delegation_baseline = execution.delegation_baseline_task_ids()
            if delegation_baseline is None:
                raise RuntimeError(
                    "Assignment reconciliation requires a persisted task baseline."
                )

        if reservation.mode == "reconcile":
            result = reconcile_action(
                proposals,
                action_id=args.action_id,
                client=client,
                now=lambda: datetime.now(UTC),
            )
        else:
            result = execute_action(
                proposals,
                action_id=args.action_id,
                prior_results=execution.prior_results(
                    repository=authorized.grant.repository
                ),
                client=client,
                now=lambda: datetime.now(UTC),
                override_suppression=(
                    args.action_id
                    in authorized.grant.override_suppression_for_action_ids
                ),
            )
        if operation == "assign-copilot":
            assert delegation_reader is not None
            assert delegation_baseline is not None
            assignment_started_at = execution.delegation_baseline_recorded_at()
            if assignment_started_at is None:
                raise RuntimeError(
                    "Assignment finalization requires a persisted baseline timestamp."
                )
            result = finalize_delegation_result(
                result=result,
                task_ids_before=delegation_baseline,
                client=delegation_reader,
                repository=authorized.grant.repository,
                assignment_started_at=assignment_started_at,
            )
        execution.append_terminal(
            result=result,
            at=datetime.now(UTC),
        )
    _print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
