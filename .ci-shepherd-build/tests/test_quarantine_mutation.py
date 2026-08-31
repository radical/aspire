from __future__ import annotations

import json
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ci_shepherd.quarantine import (
    apply_quarantine_source_inspection,
    quarantine_tool_tree_digest,
)
from ci_shepherd.quarantine_mutation import (
    _canonical_checkout_diff,
    create_quarantine_commit_validation,
    execute_quarantine_mutation,
    revalidate_quarantine_checkout_diff,
    validate_quarantine_post_inspection,
)
from ci_shepherd.quarantine_reconciliation import (
    verify_merged_quarantine_source,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
QUARANTINE_TOOL = REPOSITORY_ROOT / "tools" / "QuarantineTools"


class QuarantineMutationValidationTests(unittest.TestCase):
    def test_executor_owns_exact_mutation_and_validation_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkout = Path(temporary_directory)
            self._create_project(checkout, "One.Tests", "OneTests.cs")
            self._create_project(checkout, "Two.Tests", "TwoTests.cs")
            (checkout / "tools" / "QuarantineTools").mkdir(parents=True)
            request = self._execution_request()
            commands: list[list[str]] = []
            environments: list[dict[str, str]] = []

            def run(
                command: list[str],
                **kwargs: object,
            ) -> subprocess.CompletedProcess:
                commands.append(command)
                environments.append(dict(kwargs["env"]))
                if "--inspect" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        json.dumps(self._post_inspection()),
                        "",
                    )
                if "-getProperty:TargetPath" in command:
                    project = Path(command[2])
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        str(project.with_suffix(".dll")),
                        "",
                    )
                if "--list-tests" in command:
                    filtered = "--filter-not-trait" in command
                    output = "" if filtered else "\n".join(
                        test["testName"] for test in request["tests"]
                    )
                    exit_code = (
                        0
                        if not filtered or ["--ignore-exit-code", "8"] == command[-2:]
                        else 8
                    )
                    return subprocess.CompletedProcess(command, exit_code, output, "")
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch(
                    "ci_shepherd.quarantine_mutation._source_revision",
                    return_value="a" * 40,
                ),
                patch(
                    "ci_shepherd.quarantine_mutation._source_tree_digest",
                    return_value="sha256:" + "b" * 64,
                ),
                patch(
                    "ci_shepherd.quarantine_mutation._require_clean_checkout",
                ),
                patch(
                    "ci_shepherd.quarantine_mutation._changed_checkout_files",
                    return_value=[
                        "tests/One.Tests/OneTests.cs",
                        "tests/Two.Tests/TwoTests.cs",
                    ],
                ),
                patch(
                    "ci_shepherd.quarantine_mutation._canonical_checkout_diff",
                    return_value=b"validated diff",
                ),
                patch(
                    "ci_shepherd.quarantine_mutation.subprocess.run",
                    side_effect=run,
                ),
            ):
                result = execute_quarantine_mutation(request, checkout)

        mutation_commands = [
            command for command in commands if "--quarantine" in command
        ]
        self.assertEqual(2, len(mutation_commands))
        self.assertEqual(
            [
                ("Tests.One", "https://github.com/radical/aspire/issues/1"),
                ("Tests.Two", "https://github.com/radical/aspire/issues/2"),
            ],
            [
                (
                    command[-1],
                    command[command.index("--url") + 1],
                )
                for command in mutation_commands
            ],
        )
        self.assertEqual(
            1,
            sum("--inspect" in command for command in commands),
        )
        self.assertEqual(
            2,
            sum(command[:2] == ["dotnet", "build"] for command in commands),
        )
        self.assertEqual(
            4,
            sum("--list-tests" in command for command in commands),
        )
        self.assertEqual(
            2,
            sum(command[-2:] == ["--ignore-exit-code", "8"] for command in commands),
        )
        self.assertTrue(environments)
        self.assertEqual(
            {"Major"},
            {environment["DOTNET_ROLL_FORWARD"] for environment in environments},
        )
        self.assertEqual(
            {
                "schemaVersion": 1,
                "sourceRevision": "a" * 40,
                "sourceTreeDigest": "sha256:" + "b" * 64,
                "completedTests": ["Tests.One", "Tests.Two"],
                "changedFiles": [
                    "tests/One.Tests/OneTests.cs",
                    "tests/Two.Tests/TwoTests.cs",
                ],
                "affectedProjects": [
                    "tests/One.Tests/One.Tests.csproj",
                    "tests/Two.Tests/Two.Tests.csproj",
                ],
                "diffDigest": (
                    "sha256:"
                    "83874515dba2f96b6bafc85595925dc05d041e9b73869533"
                    "26f4399ee07abca2"
                ),
            },
            result,
        )

    def test_executor_rejects_stale_source_before_running_commands(self) -> None:
        request = self._execution_request()
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkout = Path(temporary_directory)
            (checkout / "tests").mkdir()
            (checkout / "tools" / "QuarantineTools").mkdir(parents=True)
            with (
                patch(
                    "ci_shepherd.quarantine_mutation._source_revision",
                    return_value="c" * 40,
                ),
                patch(
                    "ci_shepherd.quarantine_mutation.subprocess.run",
                ) as run,
            ):
                with self.assertRaisesRegex(ValueError, "source revision"):
                    execute_quarantine_mutation(request, checkout)

        run.assert_not_called()

    def test_executor_rejects_an_unexpected_changed_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkout = Path(temporary_directory)
            self._create_project(checkout, "One.Tests", "OneTests.cs")
            self._create_project(checkout, "Two.Tests", "TwoTests.cs")
            (checkout / "tools" / "QuarantineTools").mkdir(parents=True)
            request = self._execution_request()

            with (
                patch(
                    "ci_shepherd.quarantine_mutation._source_revision",
                    return_value="a" * 40,
                ),
                patch(
                    "ci_shepherd.quarantine_mutation._source_tree_digest",
                    return_value="sha256:" + "b" * 64,
                ),
                patch(
                    "ci_shepherd.quarantine_mutation._require_clean_checkout",
                ),
                patch(
                    "ci_shepherd.quarantine_mutation._changed_checkout_files",
                    return_value=[
                        "tests/One.Tests/OneTests.cs",
                        "tests/Two.Tests/TwoTests.cs",
                        "Directory.Build.props",
                    ],
                ),
                patch(
                    "ci_shepherd.quarantine_mutation.subprocess.run",
                    side_effect=self._successful_execution_run,
                ),
            ):
                with self.assertRaisesRegex(ValueError, "unexpected files"):
                    execute_quarantine_mutation(request, checkout)

    def test_executor_rejects_a_quarantined_test_missing_from_discovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkout = Path(temporary_directory)
            self._create_project(checkout, "One.Tests", "OneTests.cs")
            self._create_project(checkout, "Two.Tests", "TwoTests.cs")
            (checkout / "tools" / "QuarantineTools").mkdir(parents=True)
            request = self._execution_request()

            def run(
                command: list[str],
                **_: object,
            ) -> subprocess.CompletedProcess:
                if "--inspect" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        json.dumps(self._post_inspection()),
                        "",
                    )
                if "-getProperty:TargetPath" in command:
                    project = Path(command[2])
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        str(project.with_suffix(".dll")),
                        "",
                    )
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch(
                    "ci_shepherd.quarantine_mutation._source_revision",
                    return_value="a" * 40,
                ),
                patch(
                    "ci_shepherd.quarantine_mutation._source_tree_digest",
                    return_value="sha256:" + "b" * 64,
                ),
                patch(
                    "ci_shepherd.quarantine_mutation._require_clean_checkout",
                ),
                patch(
                    "ci_shepherd.quarantine_mutation._changed_checkout_files",
                    return_value=[
                        "tests/One.Tests/OneTests.cs",
                        "tests/Two.Tests/TwoTests.cs",
                    ],
                ),
                patch(
                    "ci_shepherd.quarantine_mutation.subprocess.run",
                    side_effect=run,
                ),
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "missing from unfiltered discovery",
                ):
                    execute_quarantine_mutation(request, checkout)

    def test_executor_rejects_a_test_assembly_outside_the_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkout = Path(temporary_directory)
            self._create_project(checkout, "One.Tests", "OneTests.cs")
            self._create_project(checkout, "Two.Tests", "TwoTests.cs")
            (checkout / "tools" / "QuarantineTools").mkdir(parents=True)
            request = self._execution_request()

            def run(
                command: list[str],
                **_: object,
            ) -> subprocess.CompletedProcess:
                if "--inspect" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        json.dumps(self._post_inspection()),
                        "",
                    )
                if "-getProperty:TargetPath" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "/outside/checkout/Tests.dll\n",
                        "",
                    )
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch(
                    "ci_shepherd.quarantine_mutation._source_revision",
                    return_value="a" * 40,
                ),
                patch(
                    "ci_shepherd.quarantine_mutation._source_tree_digest",
                    return_value="sha256:" + "b" * 64,
                ),
                patch(
                    "ci_shepherd.quarantine_mutation._require_clean_checkout",
                ),
                patch(
                    "ci_shepherd.quarantine_mutation._changed_checkout_files",
                    return_value=[
                        "tests/One.Tests/OneTests.cs",
                        "tests/Two.Tests/TwoTests.cs",
                    ],
                ),
                patch(
                    "ci_shepherd.quarantine_mutation.subprocess.run",
                    side_effect=run,
                ),
            ):
                with self.assertRaisesRegex(ValueError, "outside the checkout"):
                    execute_quarantine_mutation(request, checkout)

    def test_push_revalidation_rejects_a_changed_diff_digest(self) -> None:
        request = self._execution_request()
        result = {
            "schemaVersion": 1,
            "sourceRevision": "a" * 40,
            "sourceTreeDigest": "sha256:" + "b" * 64,
            "completedTests": ["Tests.One", "Tests.Two"],
            "changedFiles": [
                "tests/One.Tests/OneTests.cs",
                "tests/Two.Tests/TwoTests.cs",
            ],
            "affectedProjects": [
                "tests/One.Tests/One.Tests.csproj",
                "tests/Two.Tests/Two.Tests.csproj",
            ],
            "diffDigest": "sha256:" + "e" * 64,
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkout = Path(temporary_directory)
            with (
                patch(
                    "ci_shepherd.quarantine_mutation._changed_checkout_files",
                    return_value=result["changedFiles"],
                ),
                patch(
                    "ci_shepherd.quarantine_mutation._canonical_checkout_diff",
                    return_value=b"different diff",
                ),
            ):
                with self.assertRaisesRegex(ValueError, "digest changed"):
                    revalidate_quarantine_checkout_diff(
                        request,
                        result,
                        checkout,
                    )

    def test_commit_validation_binds_the_exact_validated_diff(self) -> None:
        request = self._execution_request()
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkout = Path(temporary_directory)
            self._create_project(checkout, "One.Tests", "OneTests.cs")
            self._create_project(checkout, "Two.Tests", "TwoTests.cs")
            for command in (
                ["git", "--no-pager", "init", "--quiet"],
                ["git", "--no-pager", "config", "user.name", "Test"],
                [
                    "git",
                    "--no-pager",
                    "config",
                    "user.email",
                    "test@example.com",
                ],
                ["git", "--no-pager", "add", "tests"],
                [
                    "git",
                    "--no-pager",
                    "commit",
                    "--quiet",
                    "-m",
                    "baseline",
                ],
            ):
                subprocess.run(command, cwd=checkout, check=True)
            source_revision = subprocess.run(
                ["git", "--no-pager", "rev-parse", "HEAD"],
                cwd=checkout,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            request["sourceRevision"] = source_revision
            changed_files = [
                "tests/One.Tests/OneTests.cs",
                "tests/Two.Tests/TwoTests.cs",
            ]
            for changed_file in changed_files:
                (checkout / changed_file).write_text(
                    "class Tests { /* quarantined */ }\n",
                    encoding="utf-8",
                )
            diff = _canonical_checkout_diff(checkout, changed_files)
            mutation_result = {
                "schemaVersion": 1,
                "sourceRevision": source_revision,
                "sourceTreeDigest": "sha256:" + "b" * 64,
                "completedTests": ["Tests.One", "Tests.Two"],
                "changedFiles": changed_files,
                "affectedProjects": [
                    "tests/One.Tests/One.Tests.csproj",
                    "tests/Two.Tests/Two.Tests.csproj",
                ],
                "diffDigest": f"sha256:{hashlib.sha256(diff).hexdigest()}",
            }
            subprocess.run(
                ["git", "--no-pager", "add", "tests"],
                cwd=checkout,
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "--no-pager",
                    "commit",
                    "--quiet",
                    "-m",
                    "quarantine",
                ],
                cwd=checkout,
                check=True,
            )

            validated = create_quarantine_commit_validation(
                request,
                mutation_result,
                checkout,
            )

        self.assertEqual(mutation_result["diffDigest"], validated["diffDigest"])
        self.assertEqual(changed_files, validated["changedFiles"])

    def test_commit_validation_rejects_a_commit_with_the_wrong_parent(self) -> None:
        request = self._execution_request()
        mutation_result = {
            "schemaVersion": 1,
            "sourceRevision": "a" * 40,
            "sourceTreeDigest": "sha256:" + "b" * 64,
            "completedTests": ["Tests.One", "Tests.Two"],
            "changedFiles": [
                "tests/One.Tests/OneTests.cs",
                "tests/Two.Tests/TwoTests.cs",
            ],
            "affectedProjects": [
                "tests/One.Tests/One.Tests.csproj",
                "tests/Two.Tests/Two.Tests.csproj",
            ],
            "diffDigest": "sha256:" + "e" * 64,
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkout = Path(temporary_directory)
            with (
                patch(
                    "ci_shepherd.quarantine_mutation._resolve_commit",
                    return_value="c" * 40,
                ),
                patch(
                    "ci_shepherd.quarantine_mutation._single_commit_parent",
                    return_value="d" * 40,
                ),
            ):
                with self.assertRaisesRegex(ValueError, "source revision"):
                    create_quarantine_commit_validation(
                        request,
                        mutation_result,
                        checkout,
                    )

    def test_real_tool_mutation_matches_the_inspected_intent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            tests_root = Path(temporary_directory)
            source_path = tests_root / "Tests.cs"
            source_path.write_text(
                """
namespace Demo;

public class Tests
{
    [Fact]
    public void Flaky()
    {
        Assert.True(true);
    }
}
""".lstrip(),
                encoding="utf-8",
            )
            test_name = "Demo.Tests.Flaky"
            issue_url = "https://github.com/radical/aspire/issues/1"
            pre_inspection = self._inspect(tests_root, test_name)
            request = apply_quarantine_source_inspection(
                {
                    "schemaVersion": 1,
                    "repository": "radical/aspire",
                    "snapshotId": "snapshot:radical/aspire:test",
                    "batchId": "quarantine:before-inspection",
                    "tests": [
                        {
                            "testName": test_name,
                            "issueNumber": 1,
                            "issueUrl": issue_url,
                            "issueNumbers": [1],
                            "issueUrls": [issue_url],
                            "evidenceIds": ["issue:1"],
                            "summary": "The test recovered on a retry.",
                        }
                    ],
                    "blockedTargets": [],
                },
                pre_inspection,
                source_revision="a" * 40,
                source_tree_digest="sha256:" + "b" * 64,
            )
            request["inspectorTreeDigest"] = quarantine_tool_tree_digest(
                QUARANTINE_TOOL
            )

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
                    "--quarantine",
                    "--root",
                    str(tests_root),
                    "--url",
                    issue_url,
                    test_name,
                ],
                cwd=REPOSITORY_ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            validated = validate_quarantine_post_inspection(
                request,
                self._inspect(tests_root, test_name),
            )
            self.assertEqual([test_name], validated["completedTests"])
            updated_source = source_path.read_text(encoding="utf-8-sig")
            self.assertIn(f'[QuarantinedTest("{issue_url}")]', updated_source)
            self.assertIn("Assert.True(true);", updated_source)
            mutation_result = {
                **validated,
                "changedFiles": ["tests/Tests.cs"],
                "affectedProjects": ["tests/Demo.Tests.csproj"],
                "diffDigest": "sha256:" + "c" * 64,
            }
            environments: list[dict[str, str]] = []
            run = subprocess.run

            def run_with_environment(
                command: list[str],
                **kwargs: object,
            ) -> subprocess.CompletedProcess:
                environments.append(dict(kwargs["env"]))
                return run(command, **kwargs)

            with patch(
                "ci_shepherd.quarantine_reconciliation.subprocess.run",
                side_effect=run_with_environment,
            ):
                self.assertTrue(
                    verify_merged_quarantine_source(
                        request,
                        mutation_result,
                        merge_commit_sha="d" * 40,
                        tool_project=QUARANTINE_TOOL,
                        get_file=lambda _path, _revision: source_path.read_bytes(),
                    )
                )
            self.assertEqual(
                {"Major"},
                {
                    environment["DOTNET_ROLL_FORWARD"]
                    for environment in environments
                },
            )

            with patch(
                "ci_shepherd.quarantine_reconciliation.quarantine_tool_tree_digest",
                return_value="sha256:" + "e" * 64,
            ):
                self.assertFalse(
                    verify_merged_quarantine_source(
                        request,
                        mutation_result,
                        merge_commit_sha="d" * 40,
                        tool_project=QUARANTINE_TOOL,
                        get_file=lambda _path, _revision: source_path.read_bytes(),
                    )
                )

    def test_rejects_test_logic_change_even_with_expected_attribute(self) -> None:
        request = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "snapshotId": "snapshot:owner/repo:test",
            "batchId": "quarantine:test",
            "sourceRevision": "a" * 40,
            "sourceTreeDigest": "sha256:" + "b" * 64,
            "tests": [
                {
                    "testName": "Demo.Tests.Flaky",
                    "issueUrl": "https://github.com/owner/repo/issues/1",
                    "sourceLocation": {
                        "file": "Demo.Tests/Tests.cs",
                        "line": 12,
                    },
                    "sourceValidation": {
                        "fileSemanticDigest": "sha256:" + "c" * 64,
                        "fileQuarantines": [],
                    },
                }
            ],
        }
        post_inspection = {
            "schemaVersion": 1,
            "tests": [
                {
                    "testName": "Demo.Tests.Flaky",
                    "status": "resolved",
                    "matches": [
                        {
                            "file": "Demo.Tests/Tests.cs",
                            "line": 13,
                            "quarantineAttributes": [
                                {
                                    "name": "QuarantinedTest",
                                    "issueUrl": "https://github.com/owner/repo/issues/1",
                                }
                            ],
                            "activeIssueAttributes": [],
                            "fileSemanticDigest": "sha256:" + "d" * 64,
                            "fileQuarantines": [
                                {
                                    "testName": "Demo.Tests.Flaky",
                                    "issueUrl": "https://github.com/owner/repo/issues/1",
                                }
                            ],
                        }
                    ],
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "source semantics changed"):
            validate_quarantine_post_inspection(request, post_inspection)

    def test_accepts_only_the_exact_intended_quarantine(self) -> None:
        request = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "snapshotId": "snapshot:owner/repo:test",
            "batchId": "quarantine:test",
            "sourceRevision": "a" * 40,
            "sourceTreeDigest": "sha256:" + "b" * 64,
            "tests": [
                {
                    "testName": "Demo.Tests.Flaky",
                    "issueUrl": "https://github.com/owner/repo/issues/1",
                    "sourceLocation": {
                        "file": "Demo.Tests/Tests.cs",
                        "line": 12,
                    },
                    "sourceValidation": {
                        "fileSemanticDigest": "sha256:" + "c" * 64,
                        "fileQuarantines": [
                            {
                                "testName": "Demo.Tests.AlreadyQuarantined",
                                "issueUrl": "https://github.com/owner/repo/issues/2",
                            }
                        ],
                    },
                }
            ],
        }
        post_inspection = {
            "schemaVersion": 1,
            "tests": [
                {
                    "testName": "Demo.Tests.Flaky",
                    "status": "resolved",
                    "matches": [
                        {
                            "file": "Demo.Tests/Tests.cs",
                            "line": 13,
                            "quarantineAttributes": [
                                {
                                    "name": "QuarantinedTest",
                                    "issueUrl": "https://github.com/owner/repo/issues/1",
                                }
                            ],
                            "activeIssueAttributes": [],
                            "fileSemanticDigest": "sha256:" + "c" * 64,
                            "fileQuarantines": [
                                {
                                    "testName": "Demo.Tests.AlreadyQuarantined",
                                    "issueUrl": "https://github.com/owner/repo/issues/2",
                                },
                                {
                                    "testName": "Demo.Tests.Flaky",
                                    "issueUrl": "https://github.com/owner/repo/issues/1",
                                },
                            ],
                        }
                    ],
                }
            ],
        }

        validated = validate_quarantine_post_inspection(
            request,
            post_inspection,
        )

        self.assertEqual(
            {
                "schemaVersion": 1,
                "sourceRevision": "a" * 40,
                "sourceTreeDigest": "sha256:" + "b" * 64,
                "completedTests": ["Demo.Tests.Flaky"],
                "changedFiles": ["Demo.Tests/Tests.cs"],
            },
            validated,
        )

    @staticmethod
    def _create_project(
        checkout: Path,
        project_name: str,
        source_name: str,
    ) -> None:
        project_root = checkout / "tests" / project_name
        project_root.mkdir(parents=True)
        (project_root / f"{project_name}.csproj").write_text(
            "<Project />\n",
            encoding="utf-8",
        )
        (project_root / source_name).write_text(
            "class Tests { }\n",
            encoding="utf-8",
        )

    @staticmethod
    def _execution_request() -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "repository": "radical/aspire",
            "snapshotId": "snapshot:radical/aspire:test",
            "batchId": "quarantine:test",
            "sourceRevision": "a" * 40,
            "sourceTreeDigest": "sha256:" + "b" * 64,
            "tests": [
                {
                    "testName": "Tests.One",
                    "issueUrl": "https://github.com/radical/aspire/issues/1",
                    "sourceLocation": {
                        "file": "One.Tests/OneTests.cs",
                        "line": 10,
                    },
                    "sourceValidation": {
                        "fileSemanticDigest": "sha256:" + "c" * 64,
                        "fileQuarantines": [],
                    },
                },
                {
                    "testName": "Tests.Two",
                    "issueUrl": "https://github.com/radical/aspire/issues/2",
                    "sourceLocation": {
                        "file": "Two.Tests/TwoTests.cs",
                        "line": 20,
                    },
                    "sourceValidation": {
                        "fileSemanticDigest": "sha256:" + "d" * 64,
                        "fileQuarantines": [],
                    },
                },
            ],
        }

    @staticmethod
    def _post_inspection() -> dict[str, object]:
        tests = []
        for name, file_name, issue_url, digest in (
            (
                "Tests.One",
                "One.Tests/OneTests.cs",
                "https://github.com/radical/aspire/issues/1",
                "sha256:" + "c" * 64,
            ),
            (
                "Tests.Two",
                "Two.Tests/TwoTests.cs",
                "https://github.com/radical/aspire/issues/2",
                "sha256:" + "d" * 64,
            ),
        ):
            tests.append(
                {
                    "testName": name,
                    "status": "resolved",
                    "matches": [
                        {
                            "file": file_name,
                            "line": 11,
                            "quarantineAttributes": [
                                {
                                    "name": "QuarantinedTest",
                                    "issueUrl": issue_url,
                                }
                            ],
                            "activeIssueAttributes": [],
                            "fileSemanticDigest": digest,
                            "fileQuarantines": [
                                {
                                    "testName": name,
                                    "issueUrl": issue_url,
                                }
                            ],
                        }
                    ],
                }
            )
        return {"schemaVersion": 1, "tests": tests}

    def _successful_execution_run(
        self,
        command: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess:
        if "--inspect" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(self._post_inspection()),
                "",
            )
        if "-getProperty:TargetPath" in command:
            project = Path(command[2])
            return subprocess.CompletedProcess(
                command,
                0,
                str(project.with_suffix(".dll")),
                "",
            )
        if "--list-tests" in command:
            filtered = "--filter-not-trait" in command
            output = "" if filtered else "Tests.One\nTests.Two\n"
            return subprocess.CompletedProcess(command, 0, output, "")
        return subprocess.CompletedProcess(command, 0, "", "")

    def _inspect(self, tests_root: Path, test_name: str) -> dict[str, object]:
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
                test_name,
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
