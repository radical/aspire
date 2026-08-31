from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ci_shepherd.quarantine import (
    _source_tree_digest,
    inspect_quarantine_session_request,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
QUARANTINE_TOOL = REPOSITORY_ROOT / "tools" / "QuarantineTools"


class QuarantineSourceInspectionTests(unittest.TestCase):
    def test_default_scan_root_stops_at_a_git_worktree_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            (parent / ".git").mkdir()
            (parent / "tests").mkdir()
            worktree = parent / "worktree"
            tests_root = worktree / "tests"
            tests_root.mkdir(parents=True)
            (worktree / ".git").write_text(
                "gitdir: ../.git/worktrees/test\n",
                encoding="utf-8",
            )
            (tests_root / "Tests.cs").write_text(
                """
namespace Demo;
public class Tests
{
    public void Flaky() { }
}
""".lstrip(),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    str(REPOSITORY_ROOT / ".dotnet" / "dotnet"),
                    "run",
                    "--project",
                    str(QUARANTINE_TOOL),
                    "--no-restore",
                    "--verbosity",
                    "quiet",
                    "--",
                    "--inspect",
                    "Demo.Tests.Flaky",
                ],
                cwd=worktree,
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
                env=os.environ,
            )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            "resolved",
            json.loads(completed.stdout)["tests"][0]["status"],
        )

    def test_source_digest_covers_inspector_inputs_but_not_verify_outputs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkout = Path(temporary_directory)
            tests_root = checkout / "tests"
            tool_root = checkout / "tools" / "QuarantineTools"
            tests_root.mkdir(parents=True)
            tool_root.mkdir(parents=True)
            for file_name in (
                "Directory.Build.props",
                "Directory.Build.targets",
                "Directory.Packages.props",
                "NuGet.config",
                "global.json",
            ):
                (checkout / file_name).write_text(file_name, encoding="utf-8")
            source_path = tests_root / "Tests.cs"
            source_path.write_text("class Tests { }\n", encoding="utf-8")
            snapshot_path = tests_root / "Tests.received.cs"
            snapshot_path.write_text("first\n", encoding="utf-8")
            (tool_root / "Quarantine.cs").write_text(
                "class Program { }\n",
                encoding="utf-8",
            )

            baseline = _source_tree_digest(checkout)
            snapshot_path.write_text("second\n", encoding="utf-8")
            after_snapshot = _source_tree_digest(checkout)
            source_path.write_text("class Tests { int Value; }\n", encoding="utf-8")
            after_source = _source_tree_digest(checkout)

        self.assertEqual(baseline, after_snapshot)
        self.assertNotEqual(baseline, after_source)

    def test_checkout_change_during_inspection_blocks_every_candidate(self) -> None:
        request = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "snapshotId": "snapshot:owner/repo:test",
            "batchId": "quarantine:before-inspection",
            "tests": [
                {
                    "testName": "Demo.Tests.Flaky",
                    "issueNumber": 1,
                    "issueUrl": "https://github.com/owner/repo/issues/1",
                }
            ],
            "blockedTargets": [],
        }
        inspection = json.dumps(
            {
                "schemaVersion": 1,
                "tests": [
                    {
                        "testName": "Demo.Tests.Flaky",
                        "status": "not-found",
                        "matches": [],
                    }
                ],
            }
        )

        def run(command: list[str], **_: object) -> subprocess.CompletedProcess:
            if command[0] == "dotnet":
                return subprocess.CompletedProcess(command, 0, inspection, "")
            return subprocess.CompletedProcess(command, 0, "a" * 40 + "\n", "")

        with (
            patch("ci_shepherd.quarantine.subprocess.run", side_effect=run),
            patch(
                "ci_shepherd.quarantine._source_tree_digest",
                side_effect=[
                    "sha256:" + "b" * 64,
                    "sha256:" + "c" * 64,
                ],
            ),
        ):
            inspected = inspect_quarantine_session_request(
                request,
                REPOSITORY_ROOT,
            )

        self.assertEqual([], inspected["tests"])
        self.assertEqual(
            [
                {
                    "testName": "Demo.Tests.Flaky",
                    "reason": "source-inspection-unavailable",
                }
            ],
            inspected["blockedTargets"],
        )

    def test_malformed_inspector_output_blocks_every_candidate(self) -> None:
        request = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "snapshotId": "snapshot:owner/repo:test",
            "operation": "prepare-quarantine-pr",
            "batchId": "quarantine:before-inspection",
            "requiresSeparateApproval": True,
            "tests": [
                {
                    "testName": "Demo.Tests.First",
                    "issueNumber": 1,
                    "issueUrl": "https://github.com/owner/repo/issues/1",
                },
                {
                    "testName": "Demo.Tests.Second",
                    "issueNumber": 2,
                    "issueUrl": "https://github.com/owner/repo/issues/2",
                },
            ],
            "workerPrompt": "before inspection",
            "blockedTargets": [],
        }
        inspector_calls = 0

        def run(command: list[str], **_: object) -> subprocess.CompletedProcess:
            nonlocal inspector_calls
            if command[0] == "dotnet":
                inspector_calls += 1
                return subprocess.CompletedProcess(command, 0, "{not-json", "")
            if "rev-parse" in command:
                return subprocess.CompletedProcess(command, 0, "a" * 40 + "\n", "")
            return subprocess.CompletedProcess(command, 0, b"", b"")

        with patch("ci_shepherd.quarantine.subprocess.run", side_effect=run):
            inspected = inspect_quarantine_session_request(
                request,
                REPOSITORY_ROOT,
            )

        self.assertEqual(1, inspector_calls)
        self.assertEqual([], inspected["tests"])
        self.assertIsNone(inspected["batchId"])
        self.assertEqual(
            [
                {
                    "testName": "Demo.Tests.First",
                    "reason": "source-inspection-unavailable",
                },
                {
                    "testName": "Demo.Tests.Second",
                    "reason": "source-inspection-unavailable",
                },
            ],
            inspected["blockedTargets"],
        )

    def test_shepherd_inspects_a_request_against_the_checkout(self) -> None:
        request = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "snapshotId": "snapshot:owner/repo:test",
            "operation": "prepare-quarantine-pr",
            "batchId": "quarantine:before-inspection",
            "requiresSeparateApproval": True,
            "tests": [
                {
                    "testName": (
                        "Aspire.Hosting.Tests.SecretsStoreTests."
                        "GetOrSetUserSecret_SavesValueToUserSecrets"
                    ),
                    "issueNumber": 1,
                    "issueUrl": "https://github.com/owner/repo/issues/1",
                    "issueNumbers": [1],
                    "issueUrls": ["https://github.com/owner/repo/issues/1"],
                    "evidenceIds": ["issue:1"],
                    "summary": "The test recovered on a same-commit retry.",
                }
            ],
            "workerPrompt": "before inspection",
            "blockedTargets": [],
        }

        inspected = inspect_quarantine_session_request(
            request,
            REPOSITORY_ROOT,
        )

        self.assertEqual(1, len(inspected["tests"]))
        self.assertEqual(
            {
                "file": "Aspire.Hosting.Tests/SecretsStoreTests.cs",
                "line": 28,
            },
            inspected["tests"][0]["sourceLocation"],
        )
        self.assertRegex(inspected["sourceRevision"], r"^[0-9a-f]{40}$")
        self.assertRegex(
            inspected["sourceTreeDigest"],
            r"^sha256:[0-9a-f]{64}$",
        )

    def test_inspection_resolves_targets_and_never_modifies_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            tests_root = Path(temporary_directory)
            first_path = tests_root / "FirstTests.cs"
            second_path = tests_root / "SecondTests.cs"
            first_path.write_text(
                """
namespace Demo;

public class Tests
{
    public void Eligible() { }

    [QuarantinedTest("https://github.com/radical/aspire/issues/1")]
    public void AlreadyQuarantined() { }

    [ActiveIssue("https://github.com/radical/aspire/issues/2")]
    public void AlreadyDisabled() { }
}

public class Duplicate
{
    public void Same() { }
}
""".lstrip(),
                encoding="utf-8",
            )
            second_path.write_text(
                """
namespace Demo;

public class Duplicate
{
    public void Same() { }
}
""".lstrip(),
                encoding="utf-8",
            )
            original_files = {
                path: path.read_bytes() for path in (first_path, second_path)
            }

            completed = subprocess.run(
                [
                    "dotnet",
                    "run",
                    "--project",
                    str(QUARANTINE_TOOL),
                    "--",
                    "--inspect",
                    "--root",
                    str(tests_root),
                    "Demo.Tests.Eligible",
                    "Demo.Tests.AlreadyQuarantined",
                    "Demo.Tests.AlreadyDisabled",
                    "Demo.Tests.Missing",
                    "Demo.Duplicate.Same",
                ],
                cwd=REPOSITORY_ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            inspection = json.loads(completed.stdout)
            self.assertEqual(1, inspection["schemaVersion"])
            by_name = {
                result["testName"]: result for result in inspection["tests"]
            }
            self.assertEqual("resolved", by_name["Demo.Tests.Eligible"]["status"])
            self.assertEqual(
                [],
                by_name["Demo.Tests.Eligible"]["matches"][0][
                    "quarantineAttributes"
                ],
            )
            self.assertRegex(
                by_name["Demo.Tests.Eligible"]["matches"][0][
                    "fileSemanticDigest"
                ],
                r"^sha256:[0-9a-f]{64}$",
            )
            self.assertEqual(
                [
                    {
                        "testName": "Demo.Tests.AlreadyQuarantined",
                        "issueUrl": "https://github.com/radical/aspire/issues/1",
                    }
                ],
                by_name["Demo.Tests.Eligible"]["matches"][0][
                    "fileQuarantines"
                ],
            )
            self.assertEqual(
                [
                    {
                        "name": "QuarantinedTest",
                        "issueUrl": "https://github.com/radical/aspire/issues/1",
                    }
                ],
                by_name["Demo.Tests.AlreadyQuarantined"]["matches"][0][
                    "quarantineAttributes"
                ],
            )
            self.assertEqual(
                [
                    {
                        "name": "ActiveIssue",
                        "issueUrl": "https://github.com/radical/aspire/issues/2",
                    }
                ],
                by_name["Demo.Tests.AlreadyDisabled"]["matches"][0][
                    "activeIssueAttributes"
                ],
            )
            self.assertEqual("not-found", by_name["Demo.Tests.Missing"]["status"])
            self.assertEqual(
                "ambiguous",
                by_name["Demo.Duplicate.Same"]["status"],
            )
            self.assertEqual(
                2,
                len(by_name["Demo.Duplicate.Same"]["matches"]),
            )
            self.assertEqual(
                original_files,
                {path: path.read_bytes() for path in original_files},
            )

    def test_inspection_recognizes_aliased_and_global_qualified_attributes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            tests_root = Path(temporary_directory)
            (tests_root / "Tests.cs").write_text(
                """
using QT = Aspire.TestUtilities.QuarantinedTestAttribute;

namespace Demo;

public class Tests
{
    [QT("https://github.com/radical/aspire/issues/1")]
    public void Aliased() { }

    [global::Aspire.TestUtilities.QuarantinedTest(
        "https://github.com/radical/aspire/issues/2")]
    public void Qualified() { }

    [Other.QuarantinedTest(
        "https://github.com/radical/aspire/issues/3")]
    public void Foreign() { }
}
""".lstrip(),
                encoding="utf-8",
            )

            inspection = self._inspect(
                tests_root,
                "Demo.Tests.Aliased",
                "Demo.Tests.Qualified",
                "Demo.Tests.Foreign",
            )

        by_name = {
            result["testName"]: result["matches"][0]
            for result in inspection["tests"]
        }
        self.assertEqual(
            [
                {
                    "name": "QuarantinedTest",
                    "issueUrl": "https://github.com/radical/aspire/issues/1",
                }
            ],
            by_name["Demo.Tests.Aliased"]["quarantineAttributes"],
        )
        self.assertEqual(
            [
                {
                    "name": "QuarantinedTest",
                    "issueUrl": "https://github.com/radical/aspire/issues/2",
                }
            ],
            by_name["Demo.Tests.Qualified"]["quarantineAttributes"],
        )
        self.assertEqual(
            [],
            by_name["Demo.Tests.Foreign"]["quarantineAttributes"],
        )
        self.assertEqual(
            [
                {
                    "testName": "Demo.Tests.Aliased",
                    "issueUrl": "https://github.com/radical/aspire/issues/1",
                },
                {
                    "testName": "Demo.Tests.Qualified",
                    "issueUrl": "https://github.com/radical/aspire/issues/2",
                },
            ],
            by_name["Demo.Tests.Aliased"]["fileQuarantines"],
        )

    def test_semantic_digest_is_independent_of_quarantine_attribute_order(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            tests_root = Path(temporary_directory)
            source_path = tests_root / "Tests.cs"
            source_template = """
namespace Demo;

public class Tests
{
    // This comment is part of the source semantics.
    {attributes}
    public void Flaky() { }
}
""".lstrip()
            source_path.write_text(
                source_template.replace(
                    "{attributes}",
                    '[Fact]\n'
                    '    [QuarantinedTest("https://github.com/o/r/issues/1")]',
                ),
                encoding="utf-8",
            )
            appended = self._inspect(
                tests_root,
                "Demo.Tests.Flaky",
            )["tests"][0]["matches"][0]["fileSemanticDigest"]
            source_path.write_text(
                source_template.replace(
                    "{attributes}",
                    '[QuarantinedTest("https://github.com/o/r/issues/1")]\n'
                    "    [Fact]",
                ),
                encoding="utf-8",
            )
            prepended = self._inspect(
                tests_root,
                "Demo.Tests.Flaky",
            )["tests"][0]["matches"][0]["fileSemanticDigest"]

        self.assertEqual(appended, prepended)

    def _inspect(
        self,
        tests_root: Path,
        *test_names: str,
    ) -> dict[str, object]:
        completed = subprocess.run(
            [
                "dotnet",
                "run",
                "--project",
                str(QUARANTINE_TOOL),
                "--no-restore",
                "--verbosity",
                "quiet",
                "--",
                "--inspect",
                "--root",
                str(tests_root),
                *test_names,
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)


if __name__ == "__main__":
    unittest.main()
