from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from ci_shepherd.jsonl import (
    append_jsonl_rows,
    read_jsonl_rows,
    repair_incomplete_jsonl_row,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class JsonlLedgerTests(unittest.TestCase):
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
