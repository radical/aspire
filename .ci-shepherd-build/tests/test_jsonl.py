from __future__ import annotations

from contextlib import nullcontext
import json
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from typing import TextIO
import unittest
from unittest.mock import patch

from ci_shepherd import jsonl
from ci_shepherd.jsonl import (
    append_jsonl_rows,
    exclusive_file_lock,
    read_jsonl_rows,
    repair_incomplete_jsonl_row,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


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


def _probe_file_lock(lock_path: Path) -> subprocess.CompletedProcess[str]:
    probe_script = """
from pathlib import Path
import os
import sys

with Path(sys.argv[1]).open("r+b") as stream:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            print("contended")
        else:
            print("acquired")
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("contended")
        else:
            print("acquired")
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
"""
    return subprocess.run(
        [sys.executable, "-c", probe_script, str(lock_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )


class JsonlLedgerTests(unittest.TestCase):
    def test_exclusive_file_lock_rejects_a_symlink(self) -> None:
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            target = root / "target.lock"
            lock_path = root / "linked.lock"
            target.touch()
            try:
                lock_path.symlink_to(target)
            except OSError as error:
                self.skipTest(f"Symlinks are not available: {error}")

            with self.assertRaisesRegex(ValueError, "lock must not be a symlink"):
                with exclusive_file_lock(lock_path):
                    self.fail("The symlink lock must not be acquired.")

    def test_exclusive_file_lock_blocks_a_second_process_until_release(
        self,
    ) -> None:
        holder_script = """
from pathlib import Path
import sys
from ci_shepherd.jsonl import exclusive_file_lock

with exclusive_file_lock(Path(sys.argv[1])):
    print("locked", flush=True)
    sys.stdin.readline()
"""

        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            lock_path = root / "private" / "workflow-repair.lock"
            holder = subprocess.Popen(
                [sys.executable, "-c", holder_script, str(lock_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert holder.stdout is not None
                self.assertEqual(
                    "locked\n",
                    _readline_with_timeout(holder.stdout, holder),
                )
                if os.name != "nt":
                    self.assertEqual(0o700, lock_path.parent.stat().st_mode & 0o777)
                    self.assertEqual(0o600, lock_path.stat().st_mode & 0o777)

                contender = _probe_file_lock(lock_path)
                self.assertEqual(
                    (0, "contended"),
                    (contender.returncode, contender.stdout.strip()),
                    contender.stderr,
                )

                assert holder.stdin is not None
                holder.stdin.write("\n")
                holder.stdin.flush()
                self.assertEqual(0, holder.wait(timeout=5))

                contender = _probe_file_lock(lock_path)
                self.assertEqual(
                    (0, "acquired"),
                    (contender.returncode, contender.stdout.strip()),
                    contender.stderr,
                )
            finally:
                if holder.poll() is None:
                    holder.kill()
                    holder.wait()
                for stream in (
                    holder.stdin,
                    holder.stdout,
                    holder.stderr,
                ):
                    if stream is not None:
                        stream.close()

    def test_exclusive_jsonl_lock_keeps_its_existing_lock_name(self) -> None:
        path = Path("ledger.jsonl")
        with (
            patch.object(
                jsonl,
                "exclusive_file_lock",
                return_value=nullcontext(),
            ) as exclusive_file_lock,
            jsonl.exclusive_jsonl_lock(path),
        ):
            pass

        exclusive_file_lock.assert_called_once_with(Path("ledger.jsonl.lock"))

    def test_append_rejects_an_existing_invalid_complete_row(self) -> None:
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / "ledger.jsonl"
            corrupt_bytes = b'{"sequence": 1}\n"invalid"\n'
            path.write_bytes(corrupt_bytes)

            with self.assertRaisesRegex(ValueError, "row must be an object"):
                append_jsonl_rows(path, [{"sequence": 2}])

            self.assertEqual(corrupt_bytes, path.read_bytes())

    def test_repair_replaces_only_the_incomplete_row_and_preserves_the_original(
        self,
    ) -> None:
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / "ledger.jsonl"
            append_jsonl_rows(path, [{"sequence": 1}])
            corrupt_bytes = path.read_bytes() + b'{"sequence":'
            path.write_bytes(corrupt_bytes)

            backup = repair_incomplete_jsonl_row(path, {"sequence": 2})

            self.assertEqual(corrupt_bytes, backup.read_bytes())
            self.assertEqual(
                [{"sequence": 1}, {"sequence": 2}],
                read_jsonl_rows(path),
            )
            self.assertEqual(0o600, backup.stat().st_mode & 0o777)

    def test_repair_command_requires_an_explicit_replacement_row(self) -> None:
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            path = root / "ledger.jsonl"
            replacement_path = root / "replacement.json"
            path.write_text('{"sequence":', encoding="utf-8")
            replacement_path.write_text(
                json.dumps({"sequence": 1}),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(
                        REPOSITORY_ROOT
                        / ".ci-shepherd-build"
                        / "scripts"
                        / "repair_jsonl.py"
                    ),
                    "--ledger",
                    str(path),
                    "--replacement-row",
                    str(replacement_path),
                ],
                cwd=REPOSITORY_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(str(path), result["ledger"])
            self.assertEqual([{"sequence": 1}], read_jsonl_rows(path))
            self.assertEqual(
                b'{"sequence":',
                Path(result["corruptBackup"]).read_bytes(),
            )


if __name__ == "__main__":
    unittest.main()
