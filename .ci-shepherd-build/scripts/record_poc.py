#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence

from ci_shepherd.history import (
    HistoryError,
    load_current,
    load_recorded_run,
    record_poc_history,
)
from ci_shepherd.poc_state import record_poc_ledgers


def record_poc_cycle(
    *,
    state_dir: Path,
    input_path: Path,
    prepared_path: Path,
    judgments_path: Path,
    report_path: Path,
    artifact_paths: Sequence[Path],
) -> Path:
    resolved_paths = {
        "input": input_path.resolve(strict=True),
        "prepared": prepared_path.resolve(strict=True),
        "judgments": judgments_path.resolve(strict=True),
        "report": report_path.resolve(strict=True),
    }
    if len(set(resolved_paths.values())) != len(resolved_paths):
        raise HistoryError("POC cycle input paths must be distinct.")

    resolved_state = state_dir.resolve()
    for path in resolved_paths.values():
        if resolved_state == path or resolved_state in path.parents:
            raise HistoryError("State directory must not contain POC cycle inputs.")

    snapshot = _read_json(resolved_paths["input"], "snapshot")
    prepared = _read_json(resolved_paths["prepared"], "prepared assessment")
    judgments = _read_json(resolved_paths["judgments"], "judgments")
    report_markdown = resolved_paths["report"].read_text(encoding="utf-8")
    artifacts = _read_artifacts(
        artifact_paths,
        excluded=set(resolved_paths.values()),
        state_dir=resolved_state,
    )
    run_id = _run_id(snapshot)
    repository = str(snapshot.get("repository", ""))
    try:
        current = record_poc_history(
            resolved_state,
            repository,
            run_id,
            snapshot,
            prepared,
            judgments,
            report_markdown,
            artifacts,
        )
        run_directory = current.run_directory
    except HistoryError as error:
        run_directory = resolved_state / "runs" / run_id
        if not run_directory.exists():
            raise
        # The first attempt may have promoted the run before current.json failed.
        load_current(resolved_state, repository)
        recorded = load_recorded_run(resolved_state, repository, run_id)
        if (
            recorded.get("recordKind") != "poc"
            or recorded.get("snapshot") != snapshot
            or recorded.get("preparedAssessment") != prepared
            or recorded.get("judgments") != judgments
            or recorded.get("reportMarkdown") != report_markdown
        ):
            raise HistoryError(
                f"Run ID {run_id!r} already records a different POC cycle."
            ) from error
        prepared = recorded["preparedAssessment"]
        judgments = recorded["judgments"]

    record_poc_ledgers(resolved_state, repository, prepared, judgments)
    return run_directory


def _run_id(snapshot: dict[str, object]) -> str:
    collected_at = snapshot.get("collectedAt")
    if not isinstance(collected_at, str) or not collected_at:
        raise HistoryError("Snapshot collectedAt must be a nonempty string.")
    expansions = snapshot.get("expansions", [])
    rounds = [
        expansion.get("round")
        for expansion in expansions
        if isinstance(expansion, dict)
        and isinstance(expansion.get("round"), int)
    ] if isinstance(expansions, list) else []
    round_number = max(rounds, default=0)
    return f"{collected_at.replace(':', '-')}-r{round_number}"


def _read_json(path: Path, description: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HistoryError(f"Unable to read valid {description} JSON from {path}.") from error
    if not isinstance(value, dict):
        raise HistoryError(f"{description.capitalize()} must contain a JSON object.")
    return value


def _read_artifacts(
    paths: Sequence[Path],
    *,
    excluded: set[Path],
    state_dir: Path,
) -> list[tuple[str, bytes]]:
    artifacts: list[tuple[str, bytes]] = []
    for supplied in paths:
        if supplied.is_symlink():
            raise HistoryError(f"Artifact path must not be a symbolic link: {supplied}")
        path = supplied.resolve(strict=True)
        if path == state_dir or state_dir in path.parents or path in state_dir.parents:
            raise HistoryError("Artifact paths and state directory must not overlap.")
        if path.is_file():
            if path not in excluded:
                artifacts.append((path.name, path.read_bytes()))
            continue
        if not path.is_dir():
            raise HistoryError(f"Artifact path is not a file or directory: {path}")
        for child in sorted(path.rglob("*")):
            if child.is_symlink():
                raise HistoryError(f"Artifact tree contains a symbolic link: {child}")
            if not child.is_file():
                continue
            resolved_child = child.resolve(strict=True)
            if resolved_child not in excluded:
                artifacts.append(
                    (
                        resolved_child.relative_to(path).as_posix(),
                        resolved_child.read_bytes(),
                    )
                )
    return artifacts


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record one finalized CI shepherd POC cycle and its lifecycle state."
    )
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--judgments", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, action="append", default=[])
    args = parser.parse_args()

    old_umask = os.umask(0o077)
    try:
        run_directory = record_poc_cycle(
            state_dir=args.state_dir,
            input_path=args.input,
            prepared_path=args.prepared,
            judgments_path=args.judgments,
            report_path=args.report,
            artifact_paths=args.artifacts,
        )
    finally:
        os.umask(old_umask)
    print(run_directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
