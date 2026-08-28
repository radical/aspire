from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import tempfile
from typing import Sequence

from ci_shepherd.actor import build_dry_run, execute_action
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


def _load_results(
    path: Path,
    *,
    repository: str,
) -> dict[str, object]:
    if not path.exists():
        return {
            "schemaVersion": 1,
            "repository": repository,
            "results": [],
        }
    return _load_json(path)


def _write_private_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary_path, path)
        path.chmod(0o600)
    finally:
        temporary_path.unlink(missing_ok=True)


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.execute and args.action_id is None:
        parser.error("--execute requires --action-id")
    if args.execute and args.results is None and args.state_dir is None:
        parser.error("--execute requires --results or --state-dir")

    proposals_path = args.proposals.resolve()
    if args.state_dir is not None:
        state_dir = args.state_dir.expanduser().absolute()
        if state_dir.exists() and state_dir.is_symlink():
            parser.error("--state-dir must not be a symlink")
        results_path = state_dir / "action-results.json"
    else:
        results_path = args.results.resolve() if args.results is not None else None
    if results_path is not None and proposals_path == results_path:
        parser.error("--proposals and --results must be different paths")

    proposals = _load_json(proposals_path)
    if not args.execute:
        _print_json(build_dry_run(proposals, action_id=args.action_id))
        return 0
    assert results_path is not None

    repository = proposals.get("repository")
    if not isinstance(repository, str) or not repository:
        raise ValueError("Proposal repository must be a non-empty string.")
    results = _load_results(results_path, repository=repository)
    result = execute_action(
        proposals,
        action_id=args.action_id,
        prior_results=results,
        client=GitHubActorClient(),
        now=lambda: datetime.now(UTC),
    )
    result_items = results.setdefault("results", [])
    if not isinstance(result_items, list):
        raise ValueError("Action results must contain a results array.")
    result_items.append(result)
    _write_private_json(results_path, results)
    _print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
