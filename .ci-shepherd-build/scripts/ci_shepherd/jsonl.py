from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


def read_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink():
        raise ValueError(f"JSONL ledger must not be a symlink: {path}")
    if not path.exists():
        return []

    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


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
    needs_separator = False
    if path.exists() and path.stat().st_size > 0:
        with path.open("rb") as existing:
            existing.seek(-1, os.SEEK_END)
            needs_separator = existing.read(1) != b"\n"

    with path.open("a", encoding="utf-8") as stream:
        if needs_separator:
            stream.write("\n")
        for row in rows:
            stream.write(json.dumps(dict(row), sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o600)


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
