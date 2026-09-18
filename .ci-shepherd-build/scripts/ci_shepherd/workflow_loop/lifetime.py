from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
from types import TracebackType


class LifetimeLockConflictError(RuntimeError):
    """Raised when another process owns the requested lifetime lock."""


class LifetimeLock:
    """Owns one process-lifetime lock descriptor."""

    def __init__(self, descriptor: int) -> None:
        self._descriptor: int | None = descriptor

    def fileno(self) -> int:
        if self._descriptor is None:
            raise ValueError("Lifetime lock is closed.")
        return self._descriptor

    def close(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        # Closing only this process's descriptor preserves a lock inherited by
        # another process through the same open-file-description.
        os.close(descriptor)

    def __enter__(self) -> LifetimeLock:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def acquire_lifetime_lock(path: Path) -> LifetimeLock:
    _require_posix()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor = _open_verified_regular_file(path, create=True)
    try:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        os.close(descriptor)
        if error.errno in {errno.EACCES, errno.EAGAIN}:
            raise LifetimeLockConflictError(
                f"Lifetime lock is already active: {path}"
            ) from error
        raise
    return LifetimeLock(descriptor)


def adopt_lifetime_lock(fd: int, path: Path) -> LifetimeLock:
    _require_posix()
    if not isinstance(fd, int) or isinstance(fd, bool) or fd < 0:
        raise ValueError("Lifetime lock descriptor must be a nonnegative integer.")
    _verify_descriptor_path(fd, path)
    os.set_inheritable(fd, False)
    return LifetimeLock(fd)


def is_lifetime_active(path: Path) -> bool:
    _require_posix()
    descriptor = _open_verified_regular_file(path, create=False)
    try:
        import fcntl

        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                return True
            raise
        return False
    finally:
        os.close(descriptor)


def _require_posix() -> None:
    if os.name != "posix":
        raise NotImplementedError(
            "Lifetime locks require verified POSIX flock inheritance semantics."
        )


def _open_verified_regular_file(path: Path, *, create: bool) -> int:
    if path.is_symlink():
        raise ValueError(f"Lifetime lock must not be a symlink: {path}")
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise NotImplementedError(
            "Lifetime locks require POSIX O_NOFOLLOW support."
        )
    flags = os.O_RDWR | os.O_NONBLOCK | no_follow
    if create:
        flags |= os.O_CREAT
    descriptor = os.open(path, flags, 0o600)
    try:
        os.set_inheritable(descriptor, False)
        _verify_descriptor_path(descriptor, path)
        if create:
            os.fchmod(descriptor, 0o600)
        else:
            descriptor_status = os.fstat(descriptor)
            if (
                descriptor_status.st_uid != os.geteuid()
                or stat.S_IMODE(descriptor_status.st_mode) & 0o077
            ):
                raise ValueError(
                    f"Lifetime lock must be an owner-only file: {path}"
                )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _verify_descriptor_path(descriptor: int, path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"Lifetime lock must not be a symlink: {path}")
    descriptor_status = os.fstat(descriptor)
    if not stat.S_ISREG(descriptor_status.st_mode):
        raise ValueError("Lifetime lock descriptor must refer to a regular file.")
    path_status = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(path_status.st_mode):
        raise ValueError(f"Lifetime lock path must be a regular file: {path}")
    if (
        descriptor_status.st_dev != path_status.st_dev
        or descriptor_status.st_ino != path_status.st_ino
    ):
        raise ValueError(
            f"Lifetime lock descriptor does not match expected path: {path}"
        )
