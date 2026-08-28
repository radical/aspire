from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from .history import HistoryError


_SCRIPT_ROOT = Path(__file__).resolve().parents[1]
_COMPACT_SCRIPT = _SCRIPT_ROOT / "compact.py"
_FINALIZE_SCRIPT = _SCRIPT_ROOT / "finalize.py"
_PREPARE_SCRIPT = _SCRIPT_ROOT / "prepare.py"
_RECORD_SCRIPT = _SCRIPT_ROOT / "record_poc.py"
_RENDER_SCRIPT = _SCRIPT_ROOT / "render.py"


def replay_lifecycle_scenario(
    *,
    scenario_directory: Path,
    output_directory: Path,
    state_directory: Path,
) -> dict[str, Any]:
    scenario = scenario_directory.resolve(strict=True)
    output = output_directory.resolve()
    state = state_directory.resolve()
    if output.exists():
        raise HistoryError("Replay output directory must not already exist.")
    if state == scenario or state in scenario.parents or scenario in state.parents:
        raise HistoryError("Replay scenario and state directories must not overlap.")
    if output == scenario or output in scenario.parents or scenario in output.parents:
        raise HistoryError("Replay scenario and output directories must not overlap.")
    if output == state or output in state.parents or state in output.parents:
        raise HistoryError("Replay output and state directories must not overlap.")

    cycles = [
        path
        for path in sorted(scenario.iterdir(), key=lambda item: item.name)
        if path.is_dir() and not path.is_symlink()
    ]
    if not cycles:
        raise HistoryError("Replay scenario must contain at least one cycle directory.")

    output.mkdir(parents=True, mode=0o700)
    os.chmod(output, 0o700)
    summaries: list[dict[str, Any]] = []
    for source_cycle in cycles:
        input_path = source_cycle / "input.json"
        if input_path.is_symlink() or not input_path.is_file():
            raise HistoryError(
                f"Replay cycle {source_cycle.name!r} is missing a regular input.json."
            )
        for child in source_cycle.rglob("*"):
            if child.is_symlink():
                raise HistoryError(
                    f"Replay cycle {source_cycle.name!r} contains a symbolic link."
                )

        cycle_output = output / source_cycle.name
        cycle_output.mkdir(mode=0o700)
        replay_input = cycle_output / "input.json"
        shutil.copyfile(input_path, replay_input)
        os.chmod(replay_input, 0o600)

        prepared_path = cycle_output / "assessment-input.json"
        agent_input_path = cycle_output / "agent-input.json"
        agent_judgments_path = cycle_output / "agent-judgments.json"
        judgments_path = cycle_output / "judgments.json"
        report_path = cycle_output / "report.md"
        fingerprint_ledger = state / "ledgers" / "fingerprints.jsonl"
        case_ledger = state / "ledgers" / "case-events.jsonl"

        _run_script(
            _PREPARE_SCRIPT,
            "--input",
            replay_input,
            "--output",
            prepared_path,
        )
        _run_script(
            _COMPACT_SCRIPT,
            "--prepared",
            prepared_path,
            "--fingerprints",
            fingerprint_ledger,
            "--output",
            agent_input_path,
        )
        supplied_overrides = source_cycle / "agent-overrides.json"
        overrides = None
        if supplied_overrides.exists():
            if not supplied_overrides.is_file():
                raise HistoryError(
                    f"Replay cycle {source_cycle.name!r} has invalid agent overrides."
                )
            overrides = _read_json(supplied_overrides, "agent overrides")
            replay_overrides = cycle_output / "agent-overrides.json"
            shutil.copyfile(supplied_overrides, replay_overrides)
            os.chmod(replay_overrides, 0o600)
        compact_input = _read_json(agent_input_path, "compact agent input")
        _atomic_write_private_json(
            agent_judgments_path,
            _agent_judgments(compact_input, overrides),
        )
        _run_script(
            _FINALIZE_SCRIPT,
            "--agent-input",
            agent_input_path,
            "--agent-judgments",
            agent_judgments_path,
            "--output",
            judgments_path,
        )
        _run_script(
            _RENDER_SCRIPT,
            "--prepared",
            prepared_path,
            "--judgments",
            judgments_path,
            "--snapshot",
            replay_input,
            "--output",
            report_path,
        )

        fingerprints_before = _jsonl_count(fingerprint_ledger)
        case_events_before = _jsonl_count(case_ledger)
        completed = _run_script(
            _RECORD_SCRIPT,
            "--state-dir",
            state,
            "--input",
            replay_input,
            "--prepared",
            prepared_path,
            "--judgments",
            judgments_path,
            "--report",
            report_path,
            "--artifacts",
            cycle_output,
        )
        run_directory = Path(completed.stdout.strip())
        summaries.append(
            {
                "cycle": source_cycle.name,
                "runId": run_directory.name,
                "fingerprintsAppended": (
                    _jsonl_count(fingerprint_ledger) - fingerprints_before
                ),
                "caseEventsAppended": (
                    _jsonl_count(case_ledger) - case_events_before
                ),
            }
        )

    summary = {
        "schemaVersion": 1,
        "cycles": summaries,
    }
    _atomic_write_private_json(output / "replay-summary.json", summary)
    return summary


def _run_script(script: Path, *arguments: str | Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [sys.executable, str(script), *(str(argument) for argument in arguments)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise HistoryError(
            f"{script.name} failed: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed


def _read_json(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HistoryError(f"Unable to read {description}.") from error
    if not isinstance(value, dict):
        raise HistoryError(f"{description.capitalize()} must be an object.")
    return value


def _agent_judgments(
    compact_input: dict[str, Any],
    overrides: dict[str, Any] | None,
) -> dict[str, Any]:
    issues = compact_input.get("issues")
    if not isinstance(issues, list):
        raise HistoryError("Compact agent input has invalid issues.")
    defaults: dict[int, Any] = {}
    order: list[int] = []
    for issue in issues:
        if not isinstance(issue, dict) or not isinstance(issue.get("issueNumber"), int):
            raise HistoryError("Compact agent input contains an invalid issue.")
        issue_number = issue["issueNumber"]
        default = issue.get("defaultJudgment")
        if not isinstance(default, dict):
            raise HistoryError("Compact agent input contains an invalid default judgment.")
        defaults[issue_number] = default
        order.append(issue_number)

    if overrides is not None:
        if set(overrides) != {"schemaVersion", "issues"}:
            raise HistoryError("Agent overrides must contain only schemaVersion and issues.")
        if overrides.get("schemaVersion") != compact_input.get("schemaVersion"):
            raise HistoryError("Agent override schemaVersion must match compact input.")
        override_issues = overrides.get("issues")
        if not isinstance(override_issues, list):
            raise HistoryError("Agent overrides issues must be a list.")
        seen: set[int] = set()
        for override in override_issues:
            if not isinstance(override, dict) or not isinstance(
                override.get("issueNumber"), int
            ):
                raise HistoryError("Agent overrides contain an invalid issue judgment.")
            issue_number = override["issueNumber"]
            if issue_number in seen or issue_number not in defaults:
                raise HistoryError(
                    f"Agent override issue {issue_number} is duplicate or not prepared."
                )
            seen.add(issue_number)
            defaults[issue_number] = override

    return {
        "schemaVersion": compact_input.get("schemaVersion"),
        "snapshotId": compact_input.get("snapshotId"),
        "issues": [defaults[issue_number] for issue_number in order],
    }


def _jsonl_count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line)


def _atomic_write_private_json(path: Path, value: object) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
