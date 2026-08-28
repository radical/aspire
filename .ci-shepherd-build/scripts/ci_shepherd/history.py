from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import shutil
import stat
import unicodedata
import zlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .models import ValidationError, validate_report, validate_snapshot


FRESHNESS_CLASSES = (
    "immutable",
    "source-versioned",
    "volatile",
    "derived",
    "retryable",
)

_SCHEMA_VERSION = 1
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REPOSITORY_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+$"
)
_RESERVED_RUN_FILES = frozenset({"manifest.json", "snapshot.json", "report.json"})
_POC_RESERVED_RUN_FILES = frozenset(
    {
        "manifest.json",
        "snapshot.json",
        "assessment-input.json",
        "judgments.json",
        "report.md",
    }
)
_IMMUTABLE_EVIDENCE_KINDS = frozenset({"commit", "workflow-log"})
_SOURCE_VERSIONED_EVIDENCE_KINDS = frozenset(
    {"source-path", "codeowners", "issue-comment"}
)
_EVIDENCE_RECORD_FIELDS = ("kind", "url", "collectedAt", "availability", "payload")


class HistoryError(RuntimeError):
    pass


def _strict_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


@dataclass(frozen=True, slots=True)
class CurrentHistory:
    repository: str
    run_id: str
    run_directory: Path
    _document_json: str = field(repr=False)

    @property
    def schema_version(self) -> int:
        return _load_json_text(self._document_json, "current history")["schemaVersion"]

    @property
    def evidence(self) -> dict[str, dict[str, Any]]:
        return _load_json_text(self._document_json, "current history")["evidence"]

    @property
    def previous_decisions(self) -> list[dict[str, Any]]:
        return _load_json_text(self._document_json, "current history")["previousDecisions"]

    @property
    def artifacts(self) -> tuple[str, ...]:
        document = _load_json_text(self._document_json, "current history")
        return tuple(document["artifacts"])

    @property
    def document(self) -> dict[str, Any]:
        return _load_json_text(self._document_json, "current history")


def record_history(
    state_directory: str | os.PathLike[str],
    repository: str,
    run_id: str,
    snapshot: object,
    report: object,
    artifacts: Mapping[str, bytes] | Iterable[tuple[str, bytes]] = (),
) -> CurrentHistory:
    prepared = _validate_inputs(repository, run_id, snapshot, report, artifacts)
    return _record_prepared_history(
        state_directory,
        repository,
        run_id,
        snapshot,
        prepared,
        record_kind="legacy",
    )


def record_poc_history(
    state_directory: str | os.PathLike[str],
    repository: str,
    run_id: str,
    snapshot: object,
    prepared_assessment: object,
    judgments: object,
    report_markdown: str,
    artifacts: Mapping[str, bytes] | Iterable[tuple[str, bytes]] = (),
) -> CurrentHistory:
    prepared = _validate_poc_inputs(
        repository,
        run_id,
        snapshot,
        prepared_assessment,
        judgments,
        report_markdown,
        artifacts,
    )
    return _record_prepared_history(
        state_directory,
        repository,
        run_id,
        snapshot,
        prepared,
        record_kind="poc",
    )


def _record_prepared_history(
    state_directory: str | os.PathLike[str],
    repository: str,
    run_id: str,
    snapshot: object,
    prepared: list[tuple[str, bytes]],
    *,
    record_kind: str,
) -> CurrentHistory:
    state = _validate_state_path(state_directory)
    promoted = False
    staging: Path | None = None

    try:
        _prepare_layout(state)
        with _history_lock(state):
            runs = state / "runs"
            _reject_symlink(runs, "runs directory")
            _reject_run_aliases(runs, run_id)
            final = runs / run_id
            if final.exists() or final.is_symlink():
                raise HistoryError(f"Run ID {run_id!r} already exists.")

            staging = _create_staging_directory(runs, run_id)
            file_entries: list[dict[str, object]] = []
            artifact_directories: set[Path] = set()
            for path, content in prepared:
                target = staging / Path(*PurePosixPath(path).parts)
                artifact_directories.update(_create_private_parents(staging, target.parent))
                _write_new_private_file(target, content)
                file_entries.append(_file_entry(path, content))

            manifest = {
                "schemaVersion": _SCHEMA_VERSION,
                "repository": repository,
                "runId": run_id,
                "recordedAt": _snapshot_collected_at(snapshot),
                "complete": True,
                "freshnessClasses": list(FRESHNESS_CLASSES),
                "files": sorted(file_entries, key=lambda item: str(item["path"])),
            }
            if record_kind != "legacy":
                manifest["recordKind"] = record_kind
            _write_new_private_file(
                staging / "manifest.json",
                _strict_json(manifest).encode("utf-8"),
            )
            for directory in sorted(
                artifact_directories,
                key=lambda path: (
                    -len(path.relative_to(staging).parts),
                    path.relative_to(staging).as_posix(),
                ),
            ):
                _fsync_directory(directory)
            _fsync_directory(staging)

            # The lock makes the precondition and rename one append-only operation.
            os.rename(staging, final)
            promoted = True
            _fsync_directory(runs)

            valid_runs, _ = _scan_valid_runs(runs, repository)
            if not valid_runs:
                raise HistoryError("The promoted run could not be validated.")
            latest = max(valid_runs, key=lambda run: (run["recordedAt"], run["runId"]))
            current_document = _current_document(latest)
            try:
                _atomic_replace_private_json(state / "current.json", current_document)
            except OSError as error:
                raise HistoryError(
                    "The immutable run was recorded, but current.json could not be replaced."
                ) from error
            return _make_current(state, current_document)
    except HistoryError:
        raise
    except OSError as error:
        raise HistoryError(f"Unable to record history run {run_id!r}: {error}") from error
    finally:
        if not promoted and staging is not None and staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging)


def load_current(
    state_directory: str | os.PathLike[str],
    repository: str,
) -> CurrentHistory | None:
    _validate_repository(repository)
    state = _validate_state_path(state_directory)
    _reject_symlink(state, "state directory")
    if not state.exists():
        return None
    if not state.is_dir():
        raise HistoryError("State path is not a directory.")
    if stat.S_IMODE(state.stat().st_mode) != 0o700:
        raise HistoryError("State directory has unsafe permissions.")

    runs = state / "runs"
    _reject_symlink(runs, "runs directory")
    if not runs.exists():
        if (state / "current.json").exists() or (state / "current.json").is_symlink():
            raise HistoryError("current.json exists without any immutable runs.")
        return None
    if not runs.is_dir():
        raise HistoryError("The runs path is not a directory.")
    if stat.S_IMODE(runs.stat().st_mode) != 0o700:
        raise HistoryError("Runs directory has unsafe permissions.")

    with _history_lock(state):
        _reject_symlink(runs, "runs directory")
        valid_runs, invalid_runs = _scan_valid_runs(runs, repository)
        if not valid_runs:
            current_exists = (state / "current.json").exists() or (
                state / "current.json"
            ).is_symlink()
            if invalid_runs or current_exists:
                raise HistoryError(
                    "History contains no valid immutable runs; corrupt runs were rejected."
                )
            return None

        latest = max(valid_runs, key=lambda run: (run["recordedAt"], run["runId"]))
        expected = _current_document(latest)
        current_path = state / "current.json"
        current = _read_current_document(current_path)
        if current != expected:
            try:
                _atomic_replace_private_json(current_path, expected)
            except OSError as error:
                raise HistoryError("Unable to rebuild current.json from immutable runs.") from error
        return _make_current(state, expected)


def load_recorded_run(
    state_directory: str | os.PathLike[str],
    repository: str,
    run_id: str,
) -> dict[str, Any]:
    _validate_repository(repository)
    _validate_run_id(run_id)
    state = _validate_state_path(state_directory)
    _reject_symlink(state, "state directory")
    if not state.is_dir() or stat.S_IMODE(state.stat().st_mode) != 0o700:
        raise HistoryError("State directory is missing or has unsafe permissions.")

    runs = state / "runs"
    _reject_symlink(runs, "runs directory")
    if not runs.is_dir() or stat.S_IMODE(runs.stat().st_mode) != 0o700:
        raise HistoryError("Runs directory is missing or has unsafe permissions.")

    with _history_lock(state):
        return _load_valid_run(runs / run_id, repository)


def _validate_inputs(
    repository: object,
    run_id: object,
    snapshot: object,
    report: object,
    artifacts: object,
) -> list[tuple[str, bytes]]:
    _validate_repository(repository)
    _validate_run_id(run_id)
    try:
        validate_snapshot(snapshot)
        validate_report(snapshot, report)
    except ValidationError as error:
        raise HistoryError(f"Invalid snapshot/report pair: {error}") from error

    snapshot_mapping = _mapping(snapshot, "snapshot")
    report_mapping = _mapping(report, "report")
    snapshot_repository = snapshot_mapping.get("repository")
    report_repository = report_mapping.get("repository")
    if (
        not isinstance(snapshot_repository, str)
        or not isinstance(report_repository, str)
        or snapshot_repository.casefold() != repository.casefold()
        or report_repository.casefold() != repository.casefold()
    ):
        raise HistoryError("History repository must match the snapshot and report repository.")
    _reject_previous_decisions_in_evidence(snapshot_mapping)

    try:
        snapshot_bytes = _strict_json(snapshot).encode("utf-8")
        report_bytes = _strict_json(report).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise HistoryError("Snapshot and report records must be JSON-compatible.") from error

    prepared = [
        ("snapshot.json", snapshot_bytes),
        ("report.json", report_bytes),
    ]
    prepared.extend(_validate_artifacts(artifacts))
    return prepared


def _validate_poc_inputs(
    repository: object,
    run_id: object,
    snapshot: object,
    prepared_assessment: object,
    judgments: object,
    report_markdown: object,
    artifacts: object,
) -> list[tuple[str, bytes]]:
    from .lifecycle import snapshot_id_for
    from .poc import validate_poc_judgments

    _validate_repository(repository)
    _validate_run_id(run_id)
    try:
        validate_snapshot(snapshot)
        validate_poc_judgments(prepared_assessment, judgments)
    except ValidationError as error:
        raise HistoryError(f"Invalid POC cycle: {error}") from error

    snapshot_mapping = _mapping(snapshot, "snapshot")
    prepared_mapping = _mapping(prepared_assessment, "prepared assessment")
    repository_text = str(repository)
    if (
        str(snapshot_mapping.get("repository", "")).casefold()
        != repository_text.casefold()
        or str(prepared_mapping.get("repository", "")).casefold()
        != repository_text.casefold()
        or prepared_mapping.get("sourceCollectedAt")
        != snapshot_mapping.get("collectedAt")
    ):
        raise HistoryError(
            "History repository and timestamp must match the snapshot and prepared assessment."
        )
    if prepared_mapping.get("snapshotId") != snapshot_id_for(snapshot_mapping):
        raise HistoryError(
            "Prepared snapshot identity must match the snapshot evidence round."
        )
    if not isinstance(report_markdown, str) or not report_markdown.strip():
        raise HistoryError("POC report Markdown must be nonempty.")
    _reject_previous_decisions_in_evidence(snapshot_mapping)

    try:
        prepared = [
            ("snapshot.json", _strict_json(snapshot).encode("utf-8")),
            (
                "assessment-input.json",
                _strict_json(prepared_assessment).encode("utf-8"),
            ),
            ("judgments.json", _strict_json(judgments).encode("utf-8")),
            ("report.md", report_markdown.encode("utf-8")),
        ]
    except (TypeError, ValueError, UnicodeError) as error:
        raise HistoryError("POC cycle records must be JSON-compatible UTF-8.") from error
    prepared.extend(
        _validate_artifacts(artifacts, reserved_paths=_POC_RESERVED_RUN_FILES)
    )
    return prepared


def _validate_repository(repository: object) -> None:
    if not isinstance(repository, str) or _REPOSITORY_RE.fullmatch(repository) is None:
        raise HistoryError("Repository must be a valid OWNER/REPO string.")


def _validate_run_id(run_id: object) -> None:
    if (
        not isinstance(run_id, str)
        or _RUN_ID_RE.fullmatch(run_id) is None
        or run_id.startswith(".")
        or run_id in {".", ".."}
    ):
        raise HistoryError("Run ID must be a safe, non-hidden path component.")


def _validate_artifacts(
    artifacts: object,
    *,
    reserved_paths: frozenset[str] = _RESERVED_RUN_FILES,
) -> list[tuple[str, bytes]]:
    if isinstance(artifacts, Mapping):
        raw_artifacts: Iterable[object] = artifacts.items()
    elif isinstance(artifacts, Iterable) and not isinstance(
        artifacts, (str, bytes, bytearray, memoryview)
    ):
        raw_artifacts = artifacts
    else:
        raise HistoryError("Artifacts must be a mapping or iterable of name/bytes pairs.")

    prepared: list[tuple[str, bytes]] = []
    aliases = {_path_alias(path) for path in reserved_paths}
    artifact_aliases: list[tuple[str, ...]] = []
    for raw_artifact in raw_artifacts:
        if (
            not isinstance(raw_artifact, (tuple, list))
            or len(raw_artifact) != 2
        ):
            raise HistoryError("Each artifact must be a name/bytes pair.")
        path, content = raw_artifact
        if not isinstance(path, str):
            raise HistoryError("Artifact names must be strings.")
        _validate_relative_path(path, "artifact")
        alias = _path_alias(path)
        if alias in aliases:
            raise HistoryError(f"Artifact path {path!r} duplicates, aliases, or reserves another path.")
        alias_parts = tuple(alias.split("/"))
        if any(
            _is_path_prefix(alias_parts, existing)
            or _is_path_prefix(existing, alias_parts)
            for existing in artifact_aliases
        ):
            raise HistoryError(
                f"Artifact path {path!r} collides with an artifact file or directory."
            )
        aliases.add(alias)
        artifact_aliases.append(alias_parts)
        if not isinstance(content, bytes):
            raise HistoryError(f"Artifact {path!r} content must be immutable bytes.")
        prepared.append((path, content))
    return sorted(prepared)


def _is_path_prefix(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return len(left) < len(right) and right[: len(left)] == left


def _validate_relative_path(path: str, description: str) -> None:
    parsed = PurePosixPath(path)
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or "//" in path
        or parsed.is_absolute()
        or not parsed.parts
        or any(part in {"", ".", ".."} for part in parsed.parts)
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
        or re.match(r"^[A-Za-z]:", path) is not None
    ):
        raise HistoryError(f"{description.capitalize()} path must be safe and relative.")
    if path != parsed.as_posix() or path != unicodedata.normalize("NFC", path):
        raise HistoryError(
            f"{description.capitalize()} path must use its canonical spelling."
        )


def _path_alias(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def _reject_previous_decisions_in_evidence(snapshot: Mapping[str, Any]) -> None:
    evidence = _mapping(snapshot.get("evidence"), "evidence")
    for evidence_id, record in evidence.items():
        record_mapping = _mapping(record, f"evidence {evidence_id}")
        if _contains_previous_decision(record_mapping):
            raise HistoryError(
                "Factual evidence cannot contain previous report decisions."
            )


def _contains_previous_decision(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str) and key.casefold() in {
                "previousdecision",
                "previousdecisions",
            }:
                return True
            if (
                isinstance(key, str)
                and key.casefold() == "source"
                and child == "previous-report"
            ):
                return True
            if _contains_previous_decision(child):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_previous_decision(item) for item in value)
    return False


def _validate_state_path(state_directory: object) -> Path:
    if not isinstance(state_directory, (str, os.PathLike)):
        raise HistoryError("State directory must be a filesystem path.")
    try:
        state = Path(state_directory).absolute()
    except (TypeError, ValueError, OSError) as error:
        raise HistoryError("State directory must be a valid filesystem path.") from error
    for candidate in (state, *state.parents):
        if candidate.is_symlink():
            raise HistoryError(f"State path traverses symlink {candidate}.")
    return state


def _prepare_layout(state: Path) -> None:
    _reject_symlink(state, "state directory")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    _reject_symlink(state, "state directory")
    if not state.is_dir():
        raise HistoryError("State path is not a directory.")
    os.chmod(state, 0o700)

    runs = state / "runs"
    _reject_symlink(runs, "runs directory")
    runs.mkdir(mode=0o700, exist_ok=True)
    _reject_symlink(runs, "runs directory")
    if not runs.is_dir():
        raise HistoryError("The runs path is not a directory.")
    os.chmod(runs, 0o700)


class _history_lock:
    def __init__(self, state: Path) -> None:
        self._path = state / ".history.lock"
        self._file: Any = None

    def __enter__(self) -> None:
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self._path, flags, 0o600)
        except OSError as error:
            raise HistoryError("Unable to open the private history lock.") from error
        os.fchmod(descriptor, 0o600)
        self._file = os.fdopen(descriptor, "r+b", closefd=True)
        fcntl.flock(self._file.fileno(), fcntl.LOCK_EX)

    def __exit__(self, *args: object) -> None:
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()


def _reject_run_aliases(runs: Path, run_id: str) -> None:
    requested_alias = _path_alias(run_id)
    for child in runs.iterdir():
        if child.name.startswith(".") and ".staging-" in child.name:
            continue
        if _path_alias(child.name) == requested_alias and child.name != run_id:
            raise HistoryError(
                f"Run ID {run_id!r} aliases existing run {child.name!r}."
            )


def _create_staging_directory(runs: Path, run_id: str) -> Path:
    for _ in range(16):
        staging = runs / f".{run_id}.staging-{os.getpid()}-{secrets.token_hex(6)}"
        try:
            staging.mkdir(mode=0o700)
            os.chmod(staging, 0o700)
            return staging
        except FileExistsError:
            continue
    raise HistoryError("Unable to allocate a unique staging directory.")


def _create_private_parents(root: Path, parent: Path) -> tuple[Path, ...]:
    relative = parent.relative_to(root)
    current = root
    created: list[Path] = []
    for part in relative.parts:
        current /= part
        if current.exists() or current.is_symlink():
            _reject_symlink(current, "artifact directory")
            if not current.is_dir():
                raise HistoryError(f"Artifact parent {current} is not a directory.")
        else:
            current.mkdir(mode=0o700)
            created.append(current)
        os.chmod(current, 0o700)
    return tuple(created)


def _write_new_private_file(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def _file_entry(path: str, content: bytes) -> dict[str, object]:
    return {
        "path": path,
        "size": len(content),
        "crc32": f"{zlib.crc32(content):08x}",
    }


def _atomic_replace_private_json(path: Path, value: object) -> None:
    content = _strict_json(value).encode("utf-8")
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(6)}"
    try:
        _write_new_private_file(temporary, content)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()


def _scan_valid_runs(
    runs: Path,
    repository: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    valid: list[dict[str, Any]] = []
    invalid: list[str] = []
    aliases: dict[str, str] = {}
    for child in sorted(runs.iterdir(), key=lambda path: path.name):
        if child.name.startswith(".") and ".staging-" in child.name:
            continue
        alias = _path_alias(child.name)
        if alias in aliases:
            invalid.extend((aliases[alias], child.name))
            valid = [run for run in valid if run["runId"] != aliases[alias]]
            continue
        aliases[alias] = child.name
        try:
            _validate_run_id(child.name)
            valid.append(_load_valid_run(child, repository))
        except (HistoryError, OSError):
            invalid.append(child.name)
    return valid, sorted(set(invalid))


def _load_valid_run(path: Path, repository: str) -> dict[str, Any]:
    _reject_symlink(path, "run directory")
    if not path.is_dir():
        raise HistoryError(f"Run {path.name!r} is not a directory.")
    if stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise HistoryError(f"Run {path.name!r} has unsafe directory permissions.")

    manifest_path = path / "manifest.json"
    manifest = _read_json_file(manifest_path, "manifest")
    manifest_fields = {
        "schemaVersion",
        "repository",
        "runId",
        "recordedAt",
        "complete",
        "freshnessClasses",
        "files",
    }
    if "recordKind" in manifest:
        manifest_fields.add("recordKind")
    if set(manifest) != manifest_fields:
        raise HistoryError(f"Run {path.name!r} has a malformed manifest.")
    record_kind = manifest.get("recordKind", "legacy")
    if (
        manifest.get("schemaVersion") != _SCHEMA_VERSION
        or manifest.get("complete") is not True
        or manifest.get("runId") != path.name
        or not isinstance(manifest.get("repository"), str)
        or manifest["repository"].casefold() != repository.casefold()
        or not isinstance(manifest.get("recordedAt"), str)
        or not manifest["recordedAt"]
        or manifest.get("freshnessClasses") != list(FRESHNESS_CLASSES)
        or record_kind not in {"legacy", "poc"}
    ):
        raise HistoryError(f"Run {path.name!r} has an invalid manifest.")

    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        raise HistoryError(f"Run {path.name!r} manifest files must be a list.")
    expected_files: set[str] = set()
    aliases: set[str] = set()
    for raw_entry in raw_files:
        if not isinstance(raw_entry, dict) or set(raw_entry) != {"path", "size", "crc32"}:
            raise HistoryError(f"Run {path.name!r} has a malformed file entry.")
        relative = raw_entry.get("path")
        if not isinstance(relative, str):
            raise HistoryError(f"Run {path.name!r} has a non-string file path.")
        _validate_relative_path(relative, "manifest file")
        alias = _path_alias(relative)
        if alias in aliases or relative == "manifest.json":
            raise HistoryError(f"Run {path.name!r} has colliding file paths.")
        aliases.add(alias)
        expected_files.add(relative)
        content_path = path / Path(*PurePosixPath(relative).parts)
        _validate_run_file(path, content_path)
        content = content_path.read_bytes()
        if (
            raw_entry.get("size") != len(content)
            or raw_entry.get("crc32") != f"{zlib.crc32(content):08x}"
        ):
            raise HistoryError(f"Run {path.name!r} file {relative!r} is corrupt.")
    required_files = (
        {"snapshot.json", "report.json"}
        if record_kind == "legacy"
        else {
            "snapshot.json",
            "assessment-input.json",
            "judgments.json",
            "report.md",
        }
    )
    if not required_files.issubset(expected_files):
        raise HistoryError(f"Run {path.name!r} is missing its validated pair.")

    actual_files: set[str] = set()
    for child in path.rglob("*"):
        _reject_symlink(child, "run content")
        if child.is_dir():
            if stat.S_IMODE(child.stat().st_mode) != 0o700:
                raise HistoryError(f"Run {path.name!r} has unsafe directory permissions.")
        elif child.is_file():
            relative = child.relative_to(path).as_posix()
            if relative != "manifest.json":
                actual_files.add(relative)
        else:
            raise HistoryError(f"Run {path.name!r} contains unsupported filesystem content.")
    if actual_files != expected_files:
        raise HistoryError(f"Run {path.name!r} manifest does not match its files.")

    snapshot = _read_json_file(path / "snapshot.json", "snapshot")
    if record_kind == "legacy":
        report = _read_json_file(path / "report.json", "report")
        try:
            validate_snapshot(snapshot)
            validate_report(snapshot, report)
        except ValidationError as error:
            raise HistoryError(f"Run {path.name!r} contains an invalid pair: {error}") from error
        if report.get("repository", "").casefold() != repository.casefold():
            raise HistoryError(f"Run {path.name!r} repository is inconsistent.")
        run_payload: dict[str, Any] = {"report": report}
    else:
        prepared = _read_json_file(
            path / "assessment-input.json",
            "prepared assessment",
        )
        judgments = _read_json_file(path / "judgments.json", "judgments")
        try:
            from .poc import validate_poc_judgments

            validate_snapshot(snapshot)
            validate_poc_judgments(prepared, judgments)
            report_markdown = (path / "report.md").read_text(encoding="utf-8")
        except (ValidationError, OSError, UnicodeError) as error:
            raise HistoryError(
                f"Run {path.name!r} contains an invalid POC cycle: {error}"
            ) from error
        if (
            not report_markdown.strip()
            or prepared.get("repository", "").casefold() != repository.casefold()
            or prepared.get("sourceCollectedAt") != snapshot.get("collectedAt")
        ):
            raise HistoryError(f"Run {path.name!r} POC cycle is inconsistent.")
        run_payload = {
            "preparedAssessment": prepared,
            "judgments": judgments,
            "reportMarkdown": report_markdown,
        }
    if (
        snapshot.get("repository", "").casefold() != repository.casefold()
        or snapshot.get("collectedAt") != manifest["recordedAt"]
    ):
        raise HistoryError(f"Run {path.name!r} repository or timestamp is inconsistent.")
    _reject_previous_decisions_in_evidence(snapshot)

    return {
        "runId": path.name,
        "recordKind": record_kind,
        "recordedAt": manifest["recordedAt"],
        "snapshot": snapshot,
        **run_payload,
        "artifacts": sorted(expected_files - required_files),
    }


def _validate_run_file(root: Path, path: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as error:
        raise HistoryError("Manifest file escapes its immutable run.") from error
    _reject_symlink(path, "run file")
    if not path.is_file():
        raise HistoryError(f"Run file {path} is missing.")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise HistoryError(f"Run file {path} has unsafe permissions.")


def _current_document(run: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = _mapping(run["snapshot"], "snapshot")
    evidence = _mapping(snapshot.get("evidence"), "evidence")
    normalized_evidence = {
        evidence_id: _history_evidence_record(
            evidence_id,
            _mapping(record, f"evidence {evidence_id}"),
        )
        for evidence_id, record in sorted(evidence.items())
    }
    record_kind = run.get("recordKind", "legacy")
    if record_kind == "poc":
        judgments = _mapping(run["judgments"], "judgments")
        decisions = judgments.get("issues")
        source_schema_versions = {
            "snapshot": snapshot["schemaVersion"],
            "judgments": judgments["schemaVersion"],
        }
    else:
        report = _mapping(run["report"], "report")
        decisions = report.get("decisions")
        source_schema_versions = {
            "snapshot": snapshot["schemaVersion"],
            "report": report["schemaVersion"],
        }
    if not isinstance(decisions, list):
        raise HistoryError("Validated cycle decisions are unavailable.")
    return {
        "schemaVersion": _SCHEMA_VERSION,
        "repository": snapshot["repository"],
        "runId": run["runId"],
        "runPath": f"runs/{run['runId']}",
        "recordedAt": run["recordedAt"],
        "sourceSchemaVersions": source_schema_versions,
        "freshnessClasses": list(FRESHNESS_CLASSES),
        "evidence": normalized_evidence,
        "previousDecisions": decisions,
        "artifacts": list(run["artifacts"]),
    }


def _history_evidence_record(
    evidence_id: str,
    record: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = json.loads(
        _strict_json(
            {
                field: record[field]
                for field in _EVIDENCE_RECORD_FIELDS
            }
        )
    )
    payload = _mapping(normalized.get("payload"), "evidence payload")
    normalized["observedAt"] = record["collectedAt"]
    source_updated_at = _first_nonempty_string(
        payload.get("sourceUpdatedAt"),
        payload.get("updatedAt"),
    )
    if source_updated_at is not None:
        normalized["sourceUpdatedAt"] = source_updated_at
    normalized["freshnessClass"] = _freshness_class(evidence_id, record, payload)
    return normalized


def _freshness_class(
    evidence_id: str,
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> str:
    if record.get("availability") != "available":
        return "retryable"
    if payload.get("source") == "derived" or payload.get("derived") is True:
        return "derived"
    kind = record.get("kind")
    if kind == "issue-event":
        if ":event:" in evidence_id:
            return "immutable"
        if isinstance(payload.get("updatedAt"), str):
            return "source-versioned"
    if kind == "workflow-run" and payload.get("status") == "completed":
        if (
            payload.get("recentHistoryCollected") is True
            or payload.get("recentHistoryGap") not in {None, "", "not-requested"}
        ):
            return "volatile"
        return "immutable"
    if kind == "workflow-job" and payload.get("status") == "completed":
        return "immutable"
    if kind == "pull-request" and payload.get("state") == "closed":
        return "immutable"
    if kind in _SOURCE_VERSIONED_EVIDENCE_KINDS:
        return "source-versioned"
    if kind in _IMMUTABLE_EVIDENCE_KINDS:
        return "immutable"
    return "volatile"


def _first_nonempty_string(*values: object) -> str | None:
    return next((value for value in values if isinstance(value, str) and value), None)


def _read_current_document(path: Path) -> dict[str, Any] | None:
    if path.is_symlink():
        return None
    if not path.exists():
        return None
    try:
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            return None
        document = _read_json_file(path, "current history")
        if set(document) != {
            "schemaVersion",
            "repository",
            "runId",
            "runPath",
            "recordedAt",
            "sourceSchemaVersions",
            "freshnessClasses",
            "evidence",
            "previousDecisions",
            "artifacts",
        }:
            return None
        return document
    except (HistoryError, OSError):
        return None


def _make_current(state: Path, document: Mapping[str, Any]) -> CurrentHistory:
    document_json = _strict_json(document)
    return CurrentHistory(
        repository=str(document["repository"]),
        run_id=str(document["runId"]),
        run_directory=state / "runs" / str(document["runId"]),
        _document_json=document_json,
    )


def _read_json_file(path: Path, description: str) -> dict[str, Any]:
    _reject_symlink(path, description)
    if not path.is_file():
        raise HistoryError(f"{description.capitalize()} file is missing.")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise HistoryError(f"{description.capitalize()} file has unsafe permissions.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HistoryError(f"{description.capitalize()} file is corrupt.") from error
    if not isinstance(value, dict):
        raise HistoryError(f"{description.capitalize()} must contain a JSON object.")
    return value


def _load_json_text(value: str, description: str) -> dict[str, Any]:
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError as error:
        raise HistoryError(f"{description.capitalize()} is corrupt.") from error
    if not isinstance(loaded, dict):
        raise HistoryError(f"{description.capitalize()} must be an object.")
    return loaded


def _mapping(value: object, description: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoryError(f"{description.capitalize()} must be an object.")
    return dict(value)


def _snapshot_collected_at(snapshot: object) -> str:
    mapping = _mapping(snapshot, "snapshot")
    collected_at = mapping.get("collectedAt")
    if not isinstance(collected_at, str) or not collected_at:
        raise HistoryError("Snapshot must include collectedAt.")
    return collected_at


def _reject_symlink(path: Path, description: str) -> None:
    if path.is_symlink():
        raise HistoryError(f"{description.capitalize()} cannot be a symlink.")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
