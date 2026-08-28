#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from ci_shepherd.models import ValidationError
from ci_shepherd.review_selection import build_review_selection


def select_review(
    *,
    agent_input_path: Path,
    refresh_state_path: Path | None,
    output_path: Path,
) -> Path:
    resolved_agent_input = agent_input_path.resolve(strict=True)
    resolved_output = output_path.resolve()
    if resolved_agent_input == resolved_output:
        raise ValueError("Review selection must not overwrite the compact agent input.")

    compact_input = _read_json(resolved_agent_input, "compact agent input")
    new_issue_numbers, changed_issue_numbers, known_issue_numbers = _refresh_state(
        refresh_state_path,
        output_path=resolved_output,
    )
    selection = build_review_selection(
        compact_input,
        new_issue_numbers=new_issue_numbers,
        changed_issue_numbers=changed_issue_numbers,
        known_issue_numbers=known_issue_numbers,
    )
    _write_private_json(resolved_output, selection)
    return resolved_output


def _refresh_state(
    path: Path | None,
    *,
    output_path: Path,
) -> tuple[list[int], list[int], list[int] | None]:
    """Read the coordinator's new/changed/known issue numbers.

    Omitting the document means no prior cycle is available, so
    ``knownIssueNumbers`` stays ``None`` and every eligible case counts as
    first-seen. That is deliberately the conservative direction: treating an
    unknown case as unchanged would drop it from review without anyone
    having judged it once.
    """
    if path is None:
        return [], [], None

    resolved = path.resolve(strict=True)
    if resolved == output_path:
        raise ValueError("Review selection must not overwrite the refresh state.")
    document = _read_json(resolved, "refresh state")
    if document.get("schemaVersion") != 1:
        raise ValidationError("Refresh state schemaVersion must be 1.")
    unsupported = set(document) - {
        "schemaVersion",
        "newIssueNumbers",
        "changedIssueNumbers",
        "knownIssueNumbers",
    }
    if unsupported:
        raise ValidationError(
            f"Refresh state has unsupported fields: {sorted(unsupported)}"
        )
    known = (
        _issue_numbers(document, "knownIssueNumbers")
        if "knownIssueNumbers" in document
        else None
    )
    return (
        _issue_numbers(document, "newIssueNumbers"),
        _issue_numbers(document, "changedIssueNumbers"),
        known,
    )


def _issue_numbers(document: dict[str, Any], key: str) -> list[int]:
    values = document.get(key, [])
    if not isinstance(values, list):
        raise ValidationError(f"Refresh state {key} must be an array.")
    for value in values:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValidationError(
                f"Refresh state {key} must contain positive issue numbers."
            )
    return list(values)


def _read_json(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Unable to read valid {description} JSON from {path}.") from error
    if not isinstance(value, dict):
        raise ValueError(f"{description.capitalize()} must contain a JSON object.")
    return value


def _write_private_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Select the CI shepherd cases worth a model review and state the "
            "exact question each one must answer."
        )
    )
    parser.add_argument("--agent-input", type=Path, required=True)
    parser.add_argument("--refresh-state", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    old_umask = os.umask(0o077)
    try:
        output = select_review(
            agent_input_path=args.agent_input,
            refresh_state_path=args.refresh_state,
            output_path=args.output,
        )
    finally:
        os.umask(old_umask)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
