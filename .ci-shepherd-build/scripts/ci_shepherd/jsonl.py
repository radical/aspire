from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
from typing import Any, Iterator, Mapping, Sequence


def read_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink():
        raise ValueError(f"JSONL ledger must not be a symlink: {path}")
    if not path.exists():
        return []

    payload = path.read_bytes()
    if payload and not payload.endswith(b"\n"):
        raise ValueError(f"JSONL ledger has an incomplete final row: {path}")
    _validate_jsonl_bytes(payload, path)
    return [
        json.loads(line)
        for line in payload.splitlines()
    ]


def append_jsonl_rows(
    path: Path,
    rows: Sequence[Mapping[str, Any]],
) -> None:
    if path.is_symlink():
        raise ValueError(f"JSONL ledger must not be a symlink: {path}")
    if not rows:
        return

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    existing = path.read_bytes() if path.exists() else b""
    if existing and not existing.endswith(b"\n"):
        raise ValueError(f"JSONL ledger has an incomplete final row: {path}")
    _validate_jsonl_bytes(existing, path)
    appended = "".join(
        json.dumps(dict(row), sort_keys=True) + "\n" for row in rows
    ).encode("utf-8")
    temporary_path = path.with_name(
        f".{path.name}.{secrets.token_hex(8)}.tmp"
    )
    try:
        descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(existing)
            stream.write(appended)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary_path.unlink(missing_ok=True)


def repair_incomplete_jsonl_row(
    path: Path,
    replacement_row: Mapping[str, Any],
) -> Path:
    with exclusive_jsonl_lock(path):
        if path.is_symlink():
            raise ValueError(f"JSONL ledger must not be a symlink: {path}")
        if not path.exists():
            raise ValueError(f"JSONL ledger does not exist: {path}")
        existing = path.read_bytes()
        if existing.endswith(b"\n"):
            raise ValueError(f"JSONL ledger has no incomplete final row: {path}")

        final_newline = existing.rfind(b"\n")
        prefix = existing[: final_newline + 1]
        _validate_jsonl_bytes(prefix, path)

        repaired = prefix + (
            json.dumps(dict(replacement_row), sort_keys=True) + "\n"
        ).encode("utf-8")
        backup_path = path.with_name(
            f".{path.name}.{secrets.token_hex(8)}.corrupt"
        )
        backup_descriptor = os.open(
            backup_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(backup_descriptor, "wb") as backup:
            backup.write(existing)
            backup.flush()
            os.fsync(backup.fileno())

        temporary_path = path.with_name(
            f".{path.name}.{secrets.token_hex(8)}.tmp"
        )
        try:
            descriptor = os.open(
                temporary_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(repaired)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
            path.chmod(0o600)
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            temporary_path.unlink(missing_ok=True)
        return backup_path


def _validate_jsonl_bytes(payload: bytes, path: Path) -> None:
    for line_number, line in enumerate(payload.splitlines(), start=1):
        try:
            row = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(
                f"JSONL ledger contains invalid JSON at {path}:{line_number}."
            ) from error
        if not isinstance(row, dict):
            raise ValueError(
                f"JSONL ledger row must be an object at {path}:{line_number}."
            )


@contextmanager
def exclusive_jsonl_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(f"{path.name}.lock")
    if lock_path.is_symlink():
        raise ValueError(f"JSONL lock must not be a symlink: {lock_path}")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path.parent.chmod(0o700)

    with lock_path.open("a+b") as stream:
        lock_path.chmod(0o600)
        if os.name == "nt":
            import msvcrt

            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
