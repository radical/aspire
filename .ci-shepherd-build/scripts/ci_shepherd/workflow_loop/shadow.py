"""Persistent, private snapshots; inherited operations are audit-only."""

from __future__ import annotations

from collections.abc import Collection
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import stat

from .state import WorkflowLoopStore, _workflow_scope
from .worker import WorkerPacketPaths


_DATABASE_NAME = "workflow-loop.sqlite3"
_MARKER_NAME = "shadow.json"
_PREPARING_NAME = ".shadow-preparing"
_WORKER_PATHS = {
    "request_path": "request",
    "result_path": "result",
    "detail_path": "detail",
    "lifetime_lock_path": "lifetime_lock",
}


def prepare_shadow(
    canonical: Path,
    shadow: Path,
    *,
    repository: str,
    branch: str,
    workflow_ids: Collection[int] | None,
) -> Path:
    """Snapshot once, then resume only the same source and workflow scope.

    The marker is published last. A failed preparation leaves an unbound,
    nonempty directory that cannot subsequently be mistaken for a fresh run.
    """
    if not isinstance(canonical, Path) or not isinstance(shadow, Path):
        raise ValueError("Canonical and shadow directories must be pathlib.Path values.")
    if not shadow.is_absolute():
        raise ValueError("Shadow directory must be an explicit absolute path.")
    canonical = canonical.absolute()
    _validate_path(canonical)
    _validate_path(shadow)
    if canonical == shadow or canonical in shadow.parents or shadow in canonical.parents:
        raise ValueError("Canonical and shadow directories must be distinct and not nested.")
    scope = _workflow_scope(workflow_ids)
    binding = {
        "schema_version": 1,
        "canonical_state_directory": str(canonical),
        "repository": repository,
        "branch": branch,
        "workflow_ids": None if scope == "all" else json.loads(scope),
    }
    store = WorkflowLoopStore(shadow, repository=repository, branch=branch)
    if shadow.exists():
        _validate_tree(shadow, private=True)
        metadata = read_shadow_metadata(shadow)
        if metadata is not None:
            if any(metadata.get(key) != value for key, value in binding.items()):
                raise ValueError("Shadow source or scope contradicts its persisted binding.")
            _validate_database_binding(shadow / _DATABASE_NAME, repository, branch, scope)
            with closing(sqlite3.connect(
                (shadow / _DATABASE_NAME).as_uri() + "?mode=ro", uri=True,
            )) as connection:
                for paths in connection.execute(
                    "SELECT request_path, result_path, detail_path, lifetime_lock_path FROM workers"
                ):
                    for value in paths:
                        _validate_worker_path(value, shadow)
            return shadow
        if any(shadow.iterdir()):
            raise ValueError("Shadow directory is not fresh or preparation is incomplete.")

    database = canonical / _DATABASE_NAME
    if canonical.exists():
        _validate_tree(canonical)
        if read_shadow_metadata(canonical) is not None:
            raise ValueError("Canonical state must not itself be a shadow.")
        if not database.exists() and any(canonical.iterdir()):
            raise ValueError("Canonical directory contains unexpected artifacts without a database.")
    has_source_database = database.exists()
    if has_source_database:
        _validate_database_binding(database, repository, branch, scope)

    _mkdir_private(shadow)
    _write_json(shadow / _PREPARING_NAME, binding)
    if has_source_database:
        # mode=ro (not immutable=1) sees committed WAL frames without opening
        # the canonical logical database for writing.
        destination = shadow / _DATABASE_NAME
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(destination)) as target:
                source.backup(target)
                target.execute(
                    "INSERT INTO meta(key, value) VALUES ('shadow_source', ?)",
                    (str(canonical),),
                )
                target.commit()
    store.initialize(workflow_ids=workflow_ids)
    if not has_source_database:
        with closing(sqlite3.connect(shadow / _DATABASE_NAME)) as connection:
            connection.execute(
                "INSERT INTO meta(key, value) VALUES ('shadow_source', ?)",
                (str(canonical),),
            )
            connection.commit()
    metadata = {**binding, **_isolate_inherited_operations(canonical, shadow)}
    _write_json(shadow / ".shadow-complete", metadata)
    (shadow / _PREPARING_NAME).unlink()
    (shadow / ".shadow-complete").replace(shadow / _MARKER_NAME)
    return shadow


def read_shadow_metadata(state_directory: Path) -> dict | None:
    """Read the completed marker, rejecting unsafe or malformed metadata."""
    _validate_path(state_directory)
    marker = state_directory / _MARKER_NAME
    _validate_path(marker)
    source = _read_shadow_source(state_directory)
    if not marker.exists():
        if source is not None:
            raise ValueError("Shadow database has a missing shadow.json marker.")
        return None
    _validate_regular_file(marker, private=True)
    try:
        metadata = json.loads(marker.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Malformed shadow metadata.") from error
    if (
        not isinstance(metadata, dict)
        or type(metadata.get("schema_version")) is not int
        or metadata["schema_version"] != 1
    ):
        raise ValueError("Unsupported or malformed shadow metadata.")
    if any(
        not isinstance(metadata.get(key), str) or not metadata[key].strip()
        for key in ("canonical_state_directory", "repository", "branch")
    ) or not Path(metadata["canonical_state_directory"]).is_absolute():
        raise ValueError("Malformed shadow source binding.")
    if metadata["canonical_state_directory"] != source:
        raise ValueError("Shadow marker source binding contradicts database provenance.")
    if "workflow_ids" not in metadata or (
        metadata["workflow_ids"] is not None
        and (
            not isinstance(metadata["workflow_ids"], list)
            or any(type(value) is not int or value < 1 for value in metadata["workflow_ids"])
            or metadata["workflow_ids"] != sorted(set(metadata["workflow_ids"]))
        )
    ):
        raise ValueError("Malformed shadow workflow scope.")
    frozen = metadata.get("frozen_item_ids")
    reasons = metadata.get("frozen_reasons")
    workers = metadata.get("inherited_worker_ids")
    provenance = metadata.get("inherited_workers")
    if (
        not isinstance(frozen, list)
        or any(type(value) is not int or value < 1 for value in frozen)
        or frozen != sorted(set(frozen))
        or not isinstance(reasons, dict)
        or set(reasons) != {str(value) for value in frozen}
        or any(
            not isinstance(value, list) or not value
            or any(not isinstance(reason, str) or not reason for reason in value)
            for value in reasons.values()
        )
        or not isinstance(workers, list)
        or any(not isinstance(value, str) or not value for value in workers)
        or workers != sorted(set(workers))
        or not isinstance(provenance, list)
        or any(not isinstance(worker, dict) for worker in provenance)
        or [worker.get("worker_id") for worker in provenance] != workers
    ):
        raise ValueError("Malformed shadow inherited-operation metadata.")
    return metadata


def _read_shadow_source(directory: Path) -> str | None:
    database = directory / _DATABASE_NAME
    _validate_path(database)
    if not database.exists():
        return None
    _validate_regular_file(database)
    for suffix in ("-wal", "-shm", "-journal"):
        companion = directory / f"{_DATABASE_NAME}{suffix}"
        _validate_path(companion)
        if companion.exists():
            _validate_regular_file(companion)
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            row = connection.execute(
                "SELECT value FROM meta WHERE key = 'shadow_source'",
            ).fetchone()
    except sqlite3.DatabaseError as error:
        raise ValueError("Cannot validate database shadow provenance.") from error
    if row is None:
        return None
    if not isinstance(row[0], str) or not row[0] or not Path(row[0]).is_absolute():
        raise ValueError("Malformed database shadow source binding.")
    return row[0]


def _isolate_inherited_operations(canonical: Path, shadow: Path) -> dict:
    frozen: dict[str, list[str]] = {}
    inherited = []
    with closing(sqlite3.connect(shadow / _DATABASE_NAME)) as connection:
        connection.row_factory = sqlite3.Row
        for worker in connection.execute("SELECT * FROM workers ORDER BY worker_id").fetchall():
            original_paths = {name: worker[name] for name in _WORKER_PATHS}
            for value in original_paths.values():
                _validate_worker_path(value, canonical)
            paths = WorkerPacketPaths.create(shadow, worker["worker_id"])
            inherited.append({
                "worker_id": worker["worker_id"],
                "item_id": worker["item_id"],
                "state": worker["state"],
                "consumed_at": worker["consumed_at"],
                "original_pid": worker["pid"],
                "original_session_id": worker["session_id"],
                "original_paths": original_paths,
                "artifacts_status": "unavailable: inherited artifacts are not copied",
            })
            if worker["consumed_at"] is None:
                frozen.setdefault(str(worker["item_id"]), []).append(
                    f"Inherited unconsumed {worker['state']} worker {worker['worker_id']}; "
                    "canonical completion and process ownership cannot be adopted."
                )
            # Even consumed terminal rows may be inspected by a launcher. No
            # inherited executable manifest, result envelope, or lifetime lock is
            # copied: they can change independently of the SQLite snapshot and
            # contain canonical absolute paths. Provenance lives only in metadata.
            connection.execute(
                "UPDATE workers SET request_path = ?, result_path = ?, detail_path = ?, "
                "lifetime_lock_path = ?, pid = NULL, session_id = ? WHERE worker_id = ?",
                (
                    *(str(getattr(paths, name)) for name in _WORKER_PATHS.values()),
                    f"shadow-inherited:{worker['worker_id']}",
                    worker["worker_id"],
                ),
            )
        for action in connection.execute(
            "SELECT action_id, item_id, state FROM action_attempts "
            "WHERE state IN ('prepared', 'invoking', 'uncertain') ORDER BY action_id"
        ):
            frozen.setdefault(str(action["item_id"]), []).append(
                f"Inherited {action['state']} action {action['action_id']}; "
                "canonical invocation ownership and outcome cannot be adopted."
            )
        connection.commit()
    return {
        "frozen_item_ids": sorted(int(item_id) for item_id in frozen),
        "frozen_reasons": frozen,
        "inherited_worker_ids": [worker["worker_id"] for worker in inherited],
        "inherited_workers": inherited,
    }


def _validate_worker_path(value: str, directory: Path) -> None:
    path = Path(value)
    if not path.is_absolute() or not path.is_relative_to(directory / "workers"):
        raise ValueError("Worker path must be inside its state directory's workers.")
    _validate_path(path)
    if path.exists():
        _validate_regular_file(path)


def _validate_database_binding(
    database: Path, repository: str, branch: str, scope: str,
) -> None:
    _validate_regular_file(database)
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            metadata = dict(connection.execute("SELECT key, value FROM meta"))
    except sqlite3.DatabaseError as error:
        raise ValueError("Canonical or shadow database metadata is invalid.") from error
    expected = {"repository": repository, "branch": branch, "workflow_scope": scope}
    if "schema_version" not in metadata or any(
        metadata.get(key) != value for key, value in expected.items()
    ):
        raise ValueError("Database source scope contradicts repository, branch or workflow scope.")


def _validate_path(path: Path) -> None:
    if ".." in path.parts:
        raise ValueError("Paths must not contain parent traversal.")
    for component in (*reversed(path.parents), path):
        if component.is_symlink():
            raise ValueError(f"Symlink paths are not allowed: {component}")


def _validate_regular_file(path: Path, *, private: bool = False) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"Expected a safe regular file: {path}")
    if private and (stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid()):
        raise ValueError(f"Shadow files must be owner-only: {path}")


def _validate_tree(directory: Path, *, private: bool = False) -> None:
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"Expected a safe directory: {directory}")
    if private and (stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid()):
        raise ValueError(f"Shadow directories must be owner-only: {directory}")
    for child in directory.iterdir():
        _validate_path(child)
        if child.is_dir():
            # The owner-only state root protects descendants, including provider
            # files created with 0755/0644. Keep checking links and file types.
            _validate_tree(child)
        else:
            _validate_regular_file(child, private=private)


def _mkdir_private(directory: Path) -> None:
    if not directory.exists():
        _mkdir_private(directory.parent)
        directory.mkdir(mode=0o700)


def _write_json(path: Path, value: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
