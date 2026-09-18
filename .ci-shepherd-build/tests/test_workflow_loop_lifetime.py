from __future__ import annotations

import importlib
import os
from pathlib import Path
from queue import Empty, Queue
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time
from types import ModuleType
from typing import BinaryIO, TextIO
import unittest
from unittest.mock import patch


def _lifetime_module() -> ModuleType:
    try:
        return importlib.import_module("ci_shepherd.workflow_loop.lifetime")
    except ModuleNotFoundError as error:
        raise AssertionError("The workflow lifetime-lock API is missing.") from error


def _readline_with_timeout(
    stream: TextIO,
    process: subprocess.Popen[str],
) -> str:
    lines: Queue[str] = Queue(maxsize=1)
    Thread(target=lambda: lines.put(stream.readline()), daemon=True).start()
    try:
        return lines.get(timeout=5)
    except Empty:
        process.kill()
        process.wait(timeout=5)
        raise AssertionError("Timed out waiting for subprocess output.") from None


def _read_pipe_with_timeout(stream: BinaryIO) -> bytes:
    chunks: Queue[bytes] = Queue(maxsize=1)
    Thread(target=lambda: chunks.put(stream.readline()), daemon=True).start()
    try:
        return chunks.get(timeout=5)
    except Empty:
        raise AssertionError("Timed out waiting for pipe output.") from None


def _probe_lifetime(path: Path) -> subprocess.CompletedProcess[str]:
    script = """
from pathlib import Path
import sys
from ci_shepherd.workflow_loop.lifetime import is_lifetime_active

print("active" if is_lifetime_active(Path(sys.argv[1])) else "inactive")
"""
    return subprocess.run(
        [sys.executable, "-c", script, str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )


def _assert_probe(
    test: unittest.TestCase,
    path: Path,
    expected: str,
) -> None:
    completed = _probe_lifetime(path)
    test.assertEqual(
        (0, expected),
        (completed.returncode, completed.stdout.strip()),
        completed.stderr,
    )


def _wait_for_inactive(test: unittest.TestCase, path: Path) -> None:
    deadline = time.monotonic() + 5
    while True:
        completed = _probe_lifetime(path)
        test.assertEqual(0, completed.returncode, completed.stderr)
        if completed.stdout.strip() == "inactive":
            return
        if time.monotonic() >= deadline:
            test.fail("Lifetime lock remained active after its final owner exited.")
        time.sleep(0.01)


def _process_exists(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_for_process_exit(process_id: int) -> None:
    deadline = time.monotonic() + 5
    while _process_exists(process_id):
        if time.monotonic() >= deadline:
            raise AssertionError(f"Process {process_id} did not exit.")
        time.sleep(0.01)


@unittest.skipUnless(os.name == "posix", "POSIX lifetime-lock semantics required")
class LifetimeLockTests(unittest.TestCase):
    def test_independent_probe_tracks_last_lock_owner(self) -> None:
        lifetime = _lifetime_module()
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / "worker.lock"
            lock = lifetime.acquire_lifetime_lock(path)

            _assert_probe(self, path, "active")

            lock.close()
            _assert_probe(self, path, "inactive")

    def test_closing_parent_copy_preserves_inherited_child_lock(self) -> None:
        lifetime = _lifetime_module()
        child_script = """
from pathlib import Path
import sys
from ci_shepherd.workflow_loop.lifetime import adopt_lifetime_lock

lock = adopt_lifetime_lock(int(sys.argv[1]), Path(sys.argv[2]))
print("adopted", flush=True)
sys.stdin.readline()
"""
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / "worker.lock"
            lock = lifetime.acquire_lifetime_lock(path)
            descriptor = lock.fileno()
            self.assertFalse(os.get_inheritable(descriptor))
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    child_script,
                    str(descriptor),
                    str(path),
                ],
                pass_fds=(descriptor,),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert child.stdout is not None
                self.assertEqual(
                    "adopted\n",
                    _readline_with_timeout(child.stdout, child),
                )
                self.assertFalse(os.get_inheritable(descriptor))

                lock.close()
                _assert_probe(self, path, "active")

                assert child.stdin is not None
                child.stdin.write("\n")
                child.stdin.flush()
                self.assertEqual(0, child.wait(timeout=5))
                _assert_probe(self, path, "inactive")
            finally:
                lock.close()
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
                for stream in (child.stdin, child.stdout, child.stderr):
                    if stream is not None:
                        stream.close()

    def test_wrapper_exit_preserves_holding_child_lock(self) -> None:
        lifetime = _lifetime_module()
        wrapper_script = r"""
from pathlib import Path
import subprocess
import sys
from ci_shepherd.workflow_loop.lifetime import adopt_lifetime_lock

lifetime_fd = int(sys.argv[1])
path = Path(sys.argv[2])
ready_fd = int(sys.argv[3])
release_fd = int(sys.argv[4])
lock = adopt_lifetime_lock(lifetime_fd, path)
child_script = '''
from pathlib import Path
import os
import sys
from ci_shepherd.workflow_loop.lifetime import adopt_lifetime_lock

lock = adopt_lifetime_lock(int(sys.argv[1]), Path(sys.argv[2]))
ready_fd = int(sys.argv[3])
release_fd = int(sys.argv[4])
os.write(ready_fd, b"ready\\n")
os.read(release_fd, 1)
'''
child = subprocess.Popen(
    [
        sys.executable,
        "-c",
        child_script,
        str(lifetime_fd),
        str(path),
        str(ready_fd),
        str(release_fd),
    ],
    pass_fds=(lifetime_fd, ready_fd, release_fd),
)
print(child.pid, flush=True)
sys.stdin.readline()
"""
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / "worker.lock"
            lock = lifetime.acquire_lifetime_lock(path)
            descriptor = lock.fileno()
            ready_read, ready_write = os.pipe()
            release_read, release_write = os.pipe()
            child_id: int | None = None
            wrapper = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    wrapper_script,
                    str(descriptor),
                    str(path),
                    str(ready_write),
                    str(release_read),
                ],
                pass_fds=(descriptor, ready_write, release_read),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            os.close(ready_write)
            os.close(release_read)
            try:
                assert wrapper.stdout is not None
                child_id = int(
                    _readline_with_timeout(wrapper.stdout, wrapper).strip()
                )
                with os.fdopen(ready_read, "rb", closefd=False) as ready:
                    self.assertEqual(b"ready\n", _read_pipe_with_timeout(ready))

                lock.close()
                wrapper.kill()
                wrapper.wait(timeout=5)
                _assert_probe(self, path, "active")

                os.write(release_write, b"\0")
                os.close(release_write)
                release_write = -1
                _wait_for_inactive(self, path)
                _wait_for_process_exit(child_id)
            finally:
                lock.close()
                if wrapper.poll() is None:
                    wrapper.kill()
                    wrapper.wait(timeout=5)
                for stream in (wrapper.stdin, wrapper.stdout, wrapper.stderr):
                    if stream is not None:
                        stream.close()
                os.close(ready_read)
                if release_write >= 0:
                    os.close(release_write)
                if child_id is not None and _process_exists(child_id):
                    os.kill(child_id, signal.SIGKILL)
                    _wait_for_process_exit(child_id)

    def test_terminal_file_does_not_replace_lifetime_lock(self) -> None:
        lifetime = _lifetime_module()
        child_script = """
from pathlib import Path
import sys
from ci_shepherd.workflow_loop.lifetime import adopt_lifetime_lock

lock = adopt_lifetime_lock(int(sys.argv[1]), Path(sys.argv[2]))
Path(sys.argv[3]).write_text("complete", encoding="utf-8")
print("result-written", flush=True)
sys.stdin.readline()
"""
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            path = root / "worker.lock"
            result_path = root / "result.json"
            lock = lifetime.acquire_lifetime_lock(path)
            descriptor = lock.fileno()
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    child_script,
                    str(descriptor),
                    str(path),
                    str(result_path),
                ],
                pass_fds=(descriptor,),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert child.stdout is not None
                self.assertEqual(
                    "result-written\n",
                    _readline_with_timeout(child.stdout, child),
                )
                lock.close()

                self.assertEqual("complete", result_path.read_text(encoding="utf-8"))
                _assert_probe(self, path, "active")

                assert child.stdin is not None
                child.stdin.write("\n")
                child.stdin.flush()
                self.assertEqual(0, child.wait(timeout=5))
                _assert_probe(self, path, "inactive")
            finally:
                lock.close()
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
                for stream in (child.stdin, child.stdout, child.stderr):
                    if stream is not None:
                        stream.close()

    def test_invalid_lock_inputs_fail_explicitly(self) -> None:
        lifetime = _lifetime_module()
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            path = root / "worker.lock"
            other_path = root / "other.lock"
            other_path.touch()
            lock = lifetime.acquire_lifetime_lock(path)
            try:
                with self.assertRaisesRegex(ValueError, "does not match"):
                    lifetime.adopt_lifetime_lock(lock.fileno(), other_path)
            finally:
                lock.close()

            with self.assertRaises(FileNotFoundError):
                lifetime.is_lifetime_active(root / "missing.lock")

            target = root / "target.lock"
            target.touch()
            symlink = root / "linked.lock"
            symlink.symlink_to(target)
            for operation in (
                lifetime.acquire_lifetime_lock,
                lifetime.is_lifetime_active,
            ):
                with (
                    self.subTest(operation=operation.__name__),
                    self.assertRaisesRegex(ValueError, "symlink"),
                ):
                    operation(symlink)

            fifo = root / "malformed.lock"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(ValueError, "regular file"):
                lifetime.is_lifetime_active(fifo)

            broad_permissions = root / "broad.lock"
            broad_permissions.touch(mode=0o644)
            broad_permissions.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "owner-only"):
                lifetime.is_lifetime_active(broad_permissions)
            self.assertEqual(
                0o644,
                broad_permissions.stat().st_mode & 0o777,
            )

    def test_unsupported_platform_fails_before_lock_operations(self) -> None:
        lifetime = _lifetime_module()
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / "worker.lock"
            path.touch()
            descriptor = os.open(path, os.O_RDWR)
            try:
                with patch.object(lifetime.os, "name", "nt"):
                    for operation in (
                        lambda: lifetime.acquire_lifetime_lock(path),
                        lambda: lifetime.adopt_lifetime_lock(descriptor, path),
                        lambda: lifetime.is_lifetime_active(path),
                    ):
                        with (
                            self.subTest(operation=operation),
                            self.assertRaisesRegex(
                                NotImplementedError,
                                "POSIX",
                            ),
                        ):
                            operation()
            finally:
                os.close(descriptor)

    def test_descriptor_inheritance_is_explicit_and_does_not_leak(self) -> None:
        lifetime = _lifetime_module()
        child_script = """
from pathlib import Path
import sys
from ci_shepherd.workflow_loop.lifetime import adopt_lifetime_lock

try:
    lock = adopt_lifetime_lock(int(sys.argv[1]), Path(sys.argv[2]))
except (OSError, ValueError):
    print("unavailable")
else:
    print("adopted")
"""
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / "worker.lock"
            descriptor_count = len(os.listdir("/dev/fd"))
            lock = lifetime.acquire_lifetime_lock(path)
            descriptor = lock.fileno()
            self.assertFalse(os.get_inheritable(descriptor))

            without_pass_fds = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    child_script,
                    str(descriptor),
                    str(path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(
                (0, "unavailable"),
                (
                    without_pass_fds.returncode,
                    without_pass_fds.stdout.strip(),
                ),
                without_pass_fds.stderr,
            )

            with_pass_fds = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    child_script,
                    str(descriptor),
                    str(path),
                ],
                pass_fds=(descriptor,),
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(
                (0, "adopted"),
                (with_pass_fds.returncode, with_pass_fds.stdout.strip()),
                with_pass_fds.stderr,
            )
            self.assertFalse(os.get_inheritable(descriptor))

            before_conflict = len(os.listdir("/dev/fd"))
            with self.assertRaisesRegex(RuntimeError, "already active"):
                lifetime.acquire_lifetime_lock(path)
            self.assertEqual(before_conflict, len(os.listdir("/dev/fd")))

            lock.close()
            lock.close()
            self.assertEqual(descriptor_count, len(os.listdir("/dev/fd")))


if __name__ == "__main__":
    unittest.main()
