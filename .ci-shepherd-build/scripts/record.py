#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from ci_shepherd.history import CurrentHistory, HistoryError, record_history
from ci_shepherd.models import ValidationError, validate_report, validate_snapshot


def record(
    *,
    state_dir: Path,
    input_path: Path,
    report_path: Path,
    assessment_path: Path | None = None,
    artifact_paths: Sequence[Path],
) -> Path:
    resolved_input = input_path.resolve(strict=True)
    resolved_report = report_path.resolve(strict=True)
    if resolved_input == resolved_report:
        raise HistoryError("Snapshot and report paths must be distinct.")
    resolved_assessment = (
        assessment_path.resolve(strict=True)
        if assessment_path is not None
        else None
    )
    if resolved_assessment in {resolved_input, resolved_report}:
        raise HistoryError("Assessment, snapshot, and report paths must be distinct.")

    resolved_state = state_dir.resolve()
    if (
        resolved_state == resolved_input
        or resolved_state == resolved_report
        or resolved_state in resolved_input.parents
        or resolved_state in resolved_report.parents
    ):
        raise HistoryError("State directory must not contain the input or report.")

    snapshot = json.loads(resolved_input.read_text(encoding="utf-8"))
    report_document = json.loads(resolved_report.read_text(encoding="utf-8"))
    assessment = (
        json.loads(resolved_assessment.read_text(encoding="utf-8"))
        if resolved_assessment is not None
        else None
    )
    if not isinstance(snapshot, dict) or not isinstance(
        snapshot.get("collectedAt"), str
    ):
        raise HistoryError("Snapshot collectedAt must be a string.")
    repository = snapshot.get("repository")
    if not isinstance(repository, str):
        raise HistoryError("Snapshot repository must be a string.")
    try:
        validate_snapshot(snapshot)
        validate_report(snapshot, report_document, assessment=assessment)
    except ValidationError as error:
        raise HistoryError(f"Invalid snapshot/report pair: {error}") from error

    artifacts = _read_artifacts(
        artifact_paths,
        excluded={
            path
            for path in (resolved_input, resolved_report, resolved_assessment)
            if path is not None
        },
        state_dir=resolved_state,
    )
    run_id = snapshot["collectedAt"].replace(":", "-")
    current: CurrentHistory = record_history(
        resolved_state,
        repository,
        run_id,
        snapshot,
        report_document,
        artifacts,
    )
    return current.run_directory


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
            if resolved_child in excluded:
                continue
            artifacts.append(
                (
                    resolved_child.relative_to(path).as_posix(),
                    resolved_child.read_bytes(),
                )
            )
    return artifacts


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record one validated CI shepherd assessment."
    )
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--assessment", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, action="append", default=[])
    args = parser.parse_args()

    run_directory = record(
        state_dir=args.state_dir,
        input_path=args.input,
        report_path=args.report,
        assessment_path=args.assessment,
        artifact_paths=args.artifacts,
    )
    print(run_directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
