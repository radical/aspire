from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from typing import Sequence

from ci_shepherd.actor import build_dry_run, execute_action, reconcile_action
from ci_shepherd.authorization import load_authorized_execution
from ci_shepherd.execution_state import ActionEventStore
from ci_shepherd.github_actor import GitHubActorClient


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview or execute one validated CI shepherd action."
    )
    parser.add_argument("--proposals", required=True, type=Path)
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
    )
    proposals = authorized.proposal_document
    proposal = authorized.proposal
    body = proposal.get("body")
    body_digest = (
        f"sha256:{hashlib.sha256(body.encode('utf-8')).hexdigest()}"
        if isinstance(body, str)
        else None
    )
    store = ActionEventStore(state_dir)
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
                }
            }
            _print_json(result)
            return 0

        client = GitHubActorClient(
            allowed_repositories={authorized.grant.repository}
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
        execution.append_terminal(
            result=result,
            at=datetime.now(UTC),
        )
    _print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
