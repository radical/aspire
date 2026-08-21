from __future__ import annotations

import copy
import shutil
import subprocess
import unittest
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from ci_shepherd.collector import CollectionError, Collector, InventoryResult
from ci_shepherd.models import validate_snapshot
from ci_shepherd import ownership


REPOSITORY = "owner/repo"
NOW = datetime(2026, 8, 17, 22, 0, tzinfo=UTC)


@dataclass
class FakeCompletedProcess:
    args: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""


class FakeGitRunner:
    def __init__(self, responses: list[FakeCompletedProcess | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, command: list[str], **kwargs: object) -> FakeCompletedProcess:
        self.calls.append((list(command), dict(kwargs)))
        if not self._responses:
            raise AssertionError(f"No fake git response left for command: {command!r}")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeApiError(RuntimeError):
    def __init__(self, category: str, status: int, message: str = "boom") -> None:
        super().__init__(message)
        self.category = category
        self.status = status


class OwnershipClient:
    def __init__(self, *, singles: dict[str, object] | None = None) -> None:
        self._singles = dict(singles or {})
        self.calls: list[tuple[str, str]] = []

    def get(self, endpoint: str) -> object:
        self.calls.append(("get", endpoint))
        response = self._singles[endpoint]
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)


def snapshot_from_result(result: InventoryResult) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "repository": REPOSITORY,
        "collectedAt": NOW.isoformat().replace("+00:00", "Z"),
        "openIssues": [issue["number"] for issue in result.open_issues],
        "evidence": result.evidence,
        "collectionErrors": [asdict(error) for error in result.collection_errors],
    }


def make_inventory(*, evidence: dict[str, dict[str, object]]) -> InventoryResult:
    return InventoryResult(
        open_issues=[],
        supporting_issues=[],
        evidence=copy.deepcopy(evidence),
        collection_errors=[],
        warnings=[],
        references={},
    )


class AffectedPathTests(unittest.TestCase):
    def test_collect_affected_paths_uses_current_evidence_sorted_unique(self) -> None:
        evidence = {
            "pr:77": {
                "kind": "pull-request",
                "url": "https://github.com/owner/repo/pull/77",
                "collectedAt": "2026-08-17T22:00:00Z",
                "availability": "available",
                "payload": {
                    "files": [
                        {"path": "src/z.py", "status": "modified"},
                        {"path": "src/a.py", "status": "added"},
                        {"path": "src/z.py", "status": "modified"},
                    ]
                },
            },
            "commit:abc": {
                "kind": "commit",
                "url": "https://github.com/owner/repo/commit/abc",
                "collectedAt": "2026-08-17T22:00:00Z",
                "availability": "available",
                "payload": {"changedPaths": ["tests/test_a.py", "src/a.py"]},
            },
            "run:1:check:9:annotation:1": {
                "kind": "workflow-job",
                "url": "https://github.com/owner/repo/actions/runs/1/job/2",
                "collectedAt": "2026-08-17T22:00:00Z",
                "availability": "available",
                "payload": {"path": "infra/build.yml", "annotationId": 1},
            },
            "source:docs%2Fguide.md": {
                "kind": "source-path",
                "url": "https://github.com/owner/repo/blob/main/docs/guide.md",
                "collectedAt": "2026-08-17T22:00:00Z",
                "availability": "available",
                "payload": {"path": "docs/guide.md"},
            },
            "issue:11": {
                "kind": "issue-event",
                "url": "https://github.com/owner/repo/issues/11",
                "collectedAt": "2026-08-17T22:00:00Z",
                "availability": "available",
                "payload": {"number": 11},
            },
        }

        affected_paths = ownership.collect_affected_paths(evidence)

        self.assertEqual(
            ["docs/guide.md", "infra/build.yml", "src/a.py", "src/z.py", "tests/test_a.py"],
            affected_paths,
        )

    def test_collect_affected_paths_excludes_external_pull_request_and_commit_paths_case_insensitively(self) -> None:
        evidence = {
            "pr:owner/repo:77": {
                "kind": "pull-request",
                "payload": {
                    "targetRepository": "Other/Repo",
                    "files": [{"path": "external/pr.py", "status": "modified"}],
                },
            },
            "commit:other/repo:abc": {
                "kind": "commit",
                "payload": {
                    "targetRepository": "other/repo",
                    "changedPaths": ["external/commit.py"],
                },
            },
            "pr:other/repo:79": {
                "kind": "pull-request",
                "payload": {
                    "files": [{"path": "external/legacy-pr.py", "status": "modified"}],
                },
            },
            "commit:other/repo:def": {
                "kind": "commit",
                "payload": {
                    "changedPaths": ["external/legacy-commit.py"],
                },
            },
            "pr:78": {
                "kind": "pull-request",
                "payload": {
                    "targetRepository": "OWNER/REPO",
                    "files": [{"path": "src/local.py", "status": "modified"}],
                },
            },
            "commit:def": {
                "kind": "commit",
                "payload": {
                    "targetRepository": "owner/repo",
                    "changedPaths": ["tests/local_test.py"],
                },
            },
        }

        affected_paths = ownership.collect_affected_paths(evidence, target_repository=REPOSITORY)

        self.assertEqual(["src/local.py", "tests/local_test.py"], affected_paths)

    def test_collect_affected_paths_includes_local_and_legacy_workflow_annotations(self) -> None:
        evidence = {
            "run:1:check:9:annotation:1": {
                "kind": "workflow-job",
                "payload": {
                    "targetRepository": "OWNER/REPO",
                    "annotationId": 1,
                    "path": "src/local.py",
                },
            },
            "run:2:check:10:annotation:2": {
                "kind": "workflow-job",
                "payload": {
                    "annotationId": 2,
                    "path": "tests/legacy.py",
                },
            },
        }

        affected_paths = ownership.collect_affected_paths(evidence, target_repository=REPOSITORY)

        self.assertEqual(["src/local.py", "tests/legacy.py"], affected_paths)

    def test_collect_affected_paths_excludes_external_workflow_annotations_and_source_paths(self) -> None:
        evidence = {
            "run:1:check:9:annotation:1": {
                "kind": "workflow-job",
                "payload": {
                    "targetRepository": "Other/Repo",
                    "annotationId": 1,
                    "path": "external/annotation.py",
                },
            },
            "source:external%2Fsource.py": {
                "kind": "source-path",
                "payload": {
                    "targetRepository": "other/repo",
                    "path": "external/source.py",
                },
            },
            "run:2:check:10:annotation:2": {
                "kind": "workflow-job",
                "payload": {
                    "targetRepository": "OWNER/REPO",
                    "annotationId": 2,
                    "path": "src/local.py",
                },
            },
        }

        affected_paths = ownership.collect_affected_paths(evidence, target_repository=REPOSITORY)

        self.assertEqual(["src/local.py"], affected_paths)


class CodeownersPatternTests(unittest.TestCase):
    def test_parse_and_match_uses_last_valid_rule_and_empty_owners_clear(self) -> None:
        rules = ownership.parse_codeowners(
            """
            *.py @python
            src/*.py @src
            /src/app.py @root
            src/app.py @final-owner
            src/app.py # clear owners
            """.strip()
        )

        match = ownership.match_codeowners("src/app.py", rules)

        self.assertIsNotNone(match)
        self.assertEqual("src/app.py", match.pattern)
        self.assertEqual([], match.owners)
        self.assertEqual(5, match.line_number)

    def test_parse_and_match_supports_wildcards_anchoring_and_case_sensitivity(self) -> None:
        rules = ownership.parse_codeowners(
            """
            docs/*.md @docs
            README.md @readers
            */config?.yml @ops
            **/OWNERS @owners
            docs/ @docs-tree
            """.strip()
        )

        self.assertEqual(["@docs-tree"], ownership.match_codeowners("docs/readme.md", rules).owners)
        self.assertEqual(["@docs-tree"], ownership.match_codeowners("src/docs/readme.md", rules).owners)
        self.assertEqual(["@readers"], ownership.match_codeowners("nested/README.md", rules).owners)
        self.assertIsNone(ownership.match_codeowners("nested/readme.md", rules))
        self.assertEqual(["@ops"], ownership.match_codeowners("prod/config1.yml", rules).owners)
        self.assertIsNone(ownership.match_codeowners("prod/nested/config1.yml", rules))
        self.assertEqual(["@owners"], ownership.match_codeowners("OWNERS", rules).owners)
        self.assertEqual(["@owners"], ownership.match_codeowners("src/team/OWNERS", rules).owners)
        self.assertEqual(["@docs-tree"], ownership.match_codeowners("docs/sub/page.txt", rules).owners)

    def test_parse_and_match_allows_globstar_to_match_zero_or_more_directories(self) -> None:
        rules = ownership.parse_codeowners("a/**/b @team\n")

        self.assertEqual(["@team"], ownership.match_codeowners("a/b", rules).owners)
        self.assertEqual(["@team"], ownership.match_codeowners("a/x/b", rules).owners)
        self.assertEqual(["@team"], ownership.match_codeowners("a/x/y/b", rules).owners)
        self.assertIsNone(ownership.match_codeowners("a/x/y/c", rules))

    def test_parse_skips_invalid_patterns_and_inline_comments(self) -> None:
        rules = ownership.parse_codeowners(
            """
            !negated @nope
            [ab]range.txt @nope
            \\#escaped-leading-comment @nope
            trailing\\
            valid.txt @ok # inline comment
            """.strip()
        )

        self.assertEqual(1, len(rules))
        self.assertEqual("valid.txt", rules[0].pattern)
        self.assertEqual(["@ok"], rules[0].owners)


class RemoteNormalizationTests(unittest.TestCase):
    def test_normalize_github_repository_url_accepts_supported_forms(self) -> None:
        self.assertEqual("owner/repo", ownership.normalize_github_repository_url("https://github.com/owner/repo"))
        self.assertEqual("owner/repo", ownership.normalize_github_repository_url("https://github.com/owner/repo.git"))
        self.assertEqual("owner/repo", ownership.normalize_github_repository_url("git@github.com:owner/repo.git"))
        self.assertEqual("owner/repo", ownership.normalize_github_repository_url("ssh://git@github.com/owner/repo.git"))

    def test_normalize_github_repository_url_rejects_other_hosts_or_shapes(self) -> None:
        self.assertIsNone(ownership.normalize_github_repository_url("http://github.com/owner/repo"))
        self.assertIsNone(ownership.normalize_github_repository_url("https://example.com/owner/repo.git"))
        self.assertIsNone(ownership.normalize_github_repository_url("ssh://git@example.com/owner/repo.git"))
        self.assertIsNone(ownership.normalize_github_repository_url("https://github.com/owner/repo/subdir"))

    def test_validate_checkout_accepts_case_variant_repository_remote(self) -> None:
        runner = FakeGitRunner(
            [
                FakeCompletedProcess([], 0, "true\n"),
                FakeCompletedProcess([], 0, "https://github.com/OWNER/REPO.git\n"),
                FakeCompletedProcess([], 0, "1111111111111111111111111111111111111111\n"),
            ]
        )

        checkout = ownership.validate_checkout(Path("checkout"), "owner/repo", git_runner=runner)

        self.assertEqual("owner/repo", checkout.repository)
        self.assertEqual("1111111111111111111111111111111111111111", checkout.commit)


class OwnershipEnrichmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.artifact_dir = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(self.artifact_dir, ignore_errors=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.artifact_dir, ignore_errors=True)

    def write_checkout_file(self, relative_path: str, content: str) -> None:
        path = self.artifact_dir / "checkout" / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def make_collector(self, client: object | None = None) -> Collector:
        return Collector(client or OwnershipClient(), REPOSITORY, NOW)

    def make_git_runner(self, *responses: FakeCompletedProcess | Exception) -> FakeGitRunner:
        return FakeGitRunner(list(responses))

    def inventory_with_paths(self) -> InventoryResult:
        return make_inventory(
            evidence={
                "pr:77": {
                    "kind": "pull-request",
                    "url": "https://github.com/owner/repo/pull/77",
                    "collectedAt": "2026-08-17T22:00:00Z",
                    "availability": "available",
                    "payload": {
                        "files": [
                            {"path": "tests/test_app.py", "status": "added"},
                            {"path": "src/app.py", "status": "modified"},
                        ]
                    },
                },
                "commit:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": {
                    "kind": "commit",
                    "url": "https://github.com/owner/repo/commit/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "collectedAt": "2026-08-17T22:00:00Z",
                    "availability": "available",
                    "payload": {
                        "sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "changedPaths": ["src/lib.py", "src/app.py"],
                    },
                },
                "run:1:check:2:annotation:1": {
                    "kind": "workflow-job",
                    "url": "https://github.com/owner/repo/actions/runs/1/job/2",
                    "collectedAt": "2026-08-17T22:00:00Z",
                    "availability": "available",
                    "payload": {"path": "tests/test_app.py", "annotationId": 1},
                },
                "source:docs%2Fguide.md": {
                    "kind": "source-path",
                    "url": "https://github.com/owner/repo/blob/main/docs/guide.md",
                    "collectedAt": "2026-08-17T22:00:00Z",
                    "availability": "available",
                    "payload": {"path": "docs/guide.md"},
                },
            }
        )

    def test_enrich_ownership_evidence_uses_first_checkout_location_and_git_history(self) -> None:
        self.write_checkout_file(".github/CODEOWNERS", "src/ @src-team\nsrc/app.py @app-team\n")
        self.write_checkout_file("CODEOWNERS", "src/app.py @wrong-team\n")
        runner = self.make_git_runner(
            FakeCompletedProcess([], 0, "true\n"),
            FakeCompletedProcess([], 0, "https://github.com/owner/repo.git\n"),
            FakeCompletedProcess([], 0, "1111111111111111111111111111111111111111\n"),
            FakeCompletedProcess([], 0, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\tMona\tmona@example.com\t2026-08-10T00:00:00+00:00\n"),
            FakeCompletedProcess([], 0, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\tMona\tmona@example.com\t2026-08-11T00:00:00+00:00\n"),
            FakeCompletedProcess([], 0, "cccccccccccccccccccccccccccccccccccccccc\tOcto\tocto@example.com\t2026-08-12T00:00:00+00:00\n"),
            FakeCompletedProcess([], 0, "dddddddddddddddddddddddddddddddddddddddd\tDocs\tdocs@example.com\t2026-08-13T00:00:00+00:00\n"),
        )
        inventory = self.inventory_with_paths()
        original = copy.deepcopy(inventory)

        enriched = self.make_collector().enrich_ownership_evidence(
            inventory,
            checkout_path=self.artifact_dir / "checkout",
            git_runner=runner,
        )

        self.assertEqual(original, inventory)
        self.assertEqual(
            [
                "codeowners:src%2Fapp.py:2",
                "codeowners:src%2Flib.py:1",
                "source:docs%2Fguide.md",
                "source:src%2Fapp.py",
                "source:src%2Flib.py",
                "source:tests%2Ftest_app.py",
            ],
            [key for key in enriched.evidence if key.startswith(("codeowners:", "source:"))],
        )
        self.assertEqual(["@app-team"], enriched.evidence["codeowners:src%2Fapp.py:2"]["payload"]["owners"])
        self.assertEqual(".github/CODEOWNERS", enriched.evidence["codeowners:src%2Fapp.py:2"]["payload"]["sourcePath"])
        self.assertEqual(
            "1111111111111111111111111111111111111111",
            enriched.evidence["source:src%2Fapp.py"]["payload"]["checkoutCommit"],
        )
        self.assertEqual(
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            enriched.evidence["source:src%2Fapp.py"]["payload"]["recentCommits"][0]["commit"],
        )
        first_command, first_kwargs = runner.calls[0]
        self.assertEqual(
            ["git", "-C", str(self.artifact_dir / "checkout"), "rev-parse", "--is-inside-work-tree"],
            first_command,
        )
        self.assertIs(False, first_kwargs["check"])
        self.assertIs(True, first_kwargs["text"])
        self.assertIs(subprocess.PIPE, first_kwargs["stdout"])
        self.assertIs(subprocess.PIPE, first_kwargs["stderr"])
        self.assertEqual(10, first_kwargs["timeout"])
        self.assertNotIn("shell", first_kwargs)
        validate_snapshot(snapshot_from_result(enriched))

    def test_enrich_ownership_evidence_ignores_external_pull_request_and_commit_paths(self) -> None:
        self.write_checkout_file(".github/CODEOWNERS", "external/ @external-team\nsrc/ @src-team\n")
        runner = self.make_git_runner(
            FakeCompletedProcess([], 0, "true\n"),
            FakeCompletedProcess([], 0, "https://github.com/owner/repo.git\n"),
            FakeCompletedProcess([], 0, "1111111111111111111111111111111111111111\n"),
            FakeCompletedProcess([], 0, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\tMona\tmona@example.com\t2026-08-10T00:00:00+00:00\n"),
            FakeCompletedProcess([], 0, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\tMona\tmona@example.com\t2026-08-11T00:00:00+00:00\n"),
            FakeCompletedProcess([], 0, "cccccccccccccccccccccccccccccccccccccccc\tOcto\tocto@example.com\t2026-08-12T00:00:00+00:00\n"),
        )
        inventory = make_inventory(
            evidence={
                "pr:other/repo:77": {
                    "kind": "pull-request",
                    "url": "https://github.com/other/repo/pull/77",
                    "collectedAt": "2026-08-17T22:00:00Z",
                    "availability": "available",
                    "payload": {
                        "targetRepository": "Other/Repo",
                        "files": [{"path": "external/pr.py", "status": "modified"}],
                    },
                },
                "commit:other/repo:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": {
                    "kind": "commit",
                    "url": "https://github.com/other/repo/commit/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "collectedAt": "2026-08-17T22:00:00Z",
                    "availability": "available",
                    "payload": {
                        "targetRepository": "other/repo",
                        "sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "changedPaths": ["external/commit.py"],
                    },
                },
                "pr:78": {
                    "kind": "pull-request",
                    "url": "https://github.com/OWNER/REPO/pull/78",
                    "collectedAt": "2026-08-17T22:00:00Z",
                    "availability": "available",
                    "payload": {
                        "targetRepository": "OWNER/REPO",
                        "files": [{"path": "src/local.py", "status": "modified"}],
                    },
                },
            }
        )

        enriched = self.make_collector().enrich_ownership_evidence(
            inventory,
            checkout_path=self.artifact_dir / "checkout",
            git_runner=runner,
        )

        self.assertNotIn("source:external%2Fpr.py", enriched.evidence)
        self.assertNotIn("source:external%2Fcommit.py", enriched.evidence)
        self.assertFalse(any(key.startswith("codeowners:external%2F") for key in enriched.evidence))
        self.assertIn("source:src%2Flocal.py", enriched.evidence)
        self.assertEqual(["@src-team"], enriched.evidence["codeowners:src%2Flocal.py:2"]["payload"]["owners"])
        validate_snapshot(snapshot_from_result(enriched))

    def test_source_and_codeowners_evidence_merge_local_path_issue_associations(self) -> None:
        self.write_checkout_file(".github/CODEOWNERS", "src/ @src-team\n")
        runner = self.make_git_runner(
            FakeCompletedProcess([], 0, "true\n"),
            FakeCompletedProcess([], 0, "https://github.com/owner/repo.git\n"),
            FakeCompletedProcess([], 0, "1111111111111111111111111111111111111111\n"),
            FakeCompletedProcess(
                [],
                0,
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\tMona\tmona@example.com\t2026-08-10T00:00:00+00:00\n",
            ),
        )
        issue_11 = {
            "sourceIssueNumber": 11,
            "sourceEvidenceId": "issue:11",
            "sourceUrl": f"https://github.com/{REPOSITORY}/issues/11",
            "extractionMethod": "commit-url",
        }
        issue_12 = {
            "sourceIssueNumber": 12,
            "sourceEvidenceId": "issue:12",
            "sourceUrl": f"https://github.com/{REPOSITORY}/issues/12",
            "extractionMethod": "full-pull-url",
        }
        external_issue = {
            "sourceIssueNumber": 99,
            "sourceEvidenceId": "issue:99",
            "sourceUrl": f"https://github.com/{REPOSITORY}/issues/99",
            "extractionMethod": "full-pull-url",
        }
        inventory = make_inventory(
            evidence={
                "pr:77": {
                    "kind": "pull-request",
                    "payload": {
                        "targetRepository": REPOSITORY,
                        "files": [{"path": "src/shared.py", "status": "modified"}],
                        "referencedBy": [issue_12],
                    },
                },
                "commit:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": {
                    "kind": "commit",
                    "payload": {
                        "targetRepository": REPOSITORY,
                        "changedPaths": ["src/shared.py"],
                        "referencedBy": [issue_12, issue_11, issue_11],
                    },
                },
                "pr:other/repo:88": {
                    "kind": "pull-request",
                    "payload": {
                        "targetRepository": "other/repo",
                        "files": [{"path": "src/shared.py", "status": "modified"}],
                        "referencedBy": [external_issue],
                    },
                },
            }
        )

        enriched = self.make_collector().enrich_ownership_evidence(
            inventory,
            checkout_path=self.artifact_dir / "checkout",
            git_runner=runner,
        )

        expected = [issue_11, issue_12]
        self.assertEqual(
            expected,
            enriched.evidence["source:src%2Fshared.py"]["payload"].get("referencedBy"),
        )
        self.assertEqual(
            expected,
            enriched.evidence["codeowners:src%2Fshared.py:1"]["payload"].get("referencedBy"),
        )

    def test_enrich_ownership_evidence_accepts_upstream_when_origin_mismatches(self) -> None:
        self.write_checkout_file(".github/CODEOWNERS", "src/ @src-team\n")
        runner = self.make_git_runner(
            FakeCompletedProcess([], 0, "true\n"),
            FakeCompletedProcess([], 0, "https://github.com/other/repo.git\n"),
            FakeCompletedProcess([], 0, "git@github.com:owner/repo.git\n"),
            FakeCompletedProcess([], 0, "2222222222222222222222222222222222222222\n"),
            FakeCompletedProcess([], 0, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\tMona\tmona@example.com\t2026-08-10T00:00:00+00:00\n"),
            FakeCompletedProcess([], 0, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\tMona\tmona@example.com\t2026-08-10T00:00:00+00:00\n"),
            FakeCompletedProcess([], 0, "cccccccccccccccccccccccccccccccccccccccc\tMona\tmona@example.com\t2026-08-10T00:00:00+00:00\n"),
            FakeCompletedProcess([], 0, "dddddddddddddddddddddddddddddddddddddddd\tMona\tmona@example.com\t2026-08-10T00:00:00+00:00\n"),
        )

        enriched = self.make_collector().enrich_ownership_evidence(
            self.inventory_with_paths(),
            checkout_path=self.artifact_dir / "checkout",
            git_runner=runner,
        )

        self.assertIn("codeowners:src%2Fapp.py:1", enriched.evidence)

    def test_enrich_ownership_evidence_records_remote_mismatch_without_reading_checkout(self) -> None:
        self.write_checkout_file(".github/CODEOWNERS", "src/ @src-team\n")
        runner = self.make_git_runner(
            FakeCompletedProcess([], 0, "true\n"),
            FakeCompletedProcess([], 0, "https://github.com/other/repo.git\n"),
            FakeCompletedProcess([], 0, "ssh://git@github.com/another/repo.git\n"),
        )

        enriched = self.make_collector().enrich_ownership_evidence(
            self.inventory_with_paths(),
            checkout_path=self.artifact_dir / "checkout",
            git_runner=runner,
        )

        self.assertFalse(any(key.startswith("codeowners:") for key in enriched.evidence))
        self.assertFalse(any(key == "source:src%2Fapp.py" for key in enriched.evidence))
        self.assertEqual("ownership-checkout", enriched.collection_errors[-1].stage)

    def test_enrich_ownership_evidence_falls_back_to_contents_api_after_not_found(self) -> None:
        client = OwnershipClient(
            singles={
                f"/repos/{REPOSITORY}/contents/.github/CODEOWNERS": FakeApiError("not-found", 404, "missing"),
                f"/repos/{REPOSITORY}/contents/CODEOWNERS": {
                    "type": "file",
                    "size": 28,
                    "encoding": "base64",
                    "content": "c3JjLyBAdGVh\nbQo=",
                    "html_url": f"https://github.com/{REPOSITORY}/blob/main/CODEOWNERS",
                },
            }
        )

        enriched = self.make_collector(client).enrich_ownership_evidence(self.inventory_with_paths())

        self.assertIn(("get", f"/repos/{REPOSITORY}/contents/.github/CODEOWNERS"), client.calls)
        self.assertIn(("get", f"/repos/{REPOSITORY}/contents/CODEOWNERS"), client.calls)
        self.assertEqual(
            "https://github.com/owner/repo/blob/main/CODEOWNERS",
            enriched.evidence["codeowners:src%2Fapp.py:1"]["payload"]["sourceUrl"],
        )

    def test_enrich_ownership_evidence_records_api_errors_and_stops_on_malformed_payload(self) -> None:
        client = OwnershipClient(
            singles={
                f"/repos/{REPOSITORY}/contents/.github/CODEOWNERS": {
                    "type": "file",
                    "size": 4000000,
                    "encoding": "base64",
                    "content": "",
                    "html_url": f"https://github.com/{REPOSITORY}/blob/main/.github/CODEOWNERS",
                }
            }
        )

        enriched = self.make_collector(client).enrich_ownership_evidence(self.inventory_with_paths())

        self.assertFalse(any(key.startswith("codeowners:") for key in enriched.evidence))
        self.assertEqual("ownership-codeowners", enriched.collection_errors[-1].stage)

    def test_enrich_ownership_evidence_emits_partial_source_records_for_history_errors(self) -> None:
        self.write_checkout_file(".github/CODEOWNERS", "src/ @src-team\n")
        runner = self.make_git_runner(
            FakeCompletedProcess([], 0, "true\n"),
            FakeCompletedProcess([], 0, "https://github.com/owner/repo.git\n"),
            FakeCompletedProcess([], 0, "3333333333333333333333333333333333333333\n"),
            FakeCompletedProcess([], 0, "dddddddddddddddddddddddddddddddddddddddd\tDocs\tdocs@example.com\t2026-08-13T00:00:00+00:00\n"),
            FakeCompletedProcess([], 0, "malformed-line\n"),
            FakeCompletedProcess([], 1, "", "fatal: no path history"),
            FakeCompletedProcess([], 0, "cccccccccccccccccccccccccccccccccccccccc\tOcto\tocto@example.com\t2026-08-12T00:00:00+00:00\n"),
        )

        enriched = self.make_collector().enrich_ownership_evidence(
            self.inventory_with_paths(),
            checkout_path=self.artifact_dir / "checkout",
            git_runner=runner,
        )

        self.assertEqual("partial", enriched.evidence["source:src%2Fapp.py"]["availability"])
        self.assertEqual("partial", enriched.evidence["source:src%2Flib.py"]["availability"])
        self.assertEqual("ownership-history", enriched.collection_errors[-2].stage)
        self.assertEqual("ownership-history", enriched.collection_errors[-1].stage)
        self.assertEqual("available", enriched.evidence["source:tests%2Ftest_app.py"]["availability"])

    def test_enrich_ownership_evidence_is_deterministic(self) -> None:
        self.write_checkout_file(".github/CODEOWNERS", "*.py @python\n")

        def run_once() -> dict[str, dict[str, object]]:
            runner = self.make_git_runner(
                FakeCompletedProcess([], 0, "true\n"),
                FakeCompletedProcess([], 0, "https://github.com/owner/repo.git\n"),
                FakeCompletedProcess([], 0, "4444444444444444444444444444444444444444\n"),
                FakeCompletedProcess([], 0, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\tMona\tmona@example.com\t2026-08-10T00:00:00+00:00\n"),
                FakeCompletedProcess([], 0, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\tMona\tmona@example.com\t2026-08-10T00:00:00+00:00\n"),
                FakeCompletedProcess([], 0, "cccccccccccccccccccccccccccccccccccccccc\tMona\tmona@example.com\t2026-08-10T00:00:00+00:00\n"),
                FakeCompletedProcess([], 0, "dddddddddddddddddddddddddddddddddddddddd\tMona\tmona@example.com\t2026-08-10T00:00:00+00:00\n"),
            )
            return self.make_collector().enrich_ownership_evidence(
                self.inventory_with_paths(),
                checkout_path=self.artifact_dir / "checkout",
                git_runner=runner,
            ).evidence

        self.assertEqual(run_once(), run_once())

    def test_enrich_ownership_evidence_limits_source_history_to_five_commits(self) -> None:
        self.write_checkout_file(".github/CODEOWNERS", "src/ @src-team\n")
        runner = self.make_git_runner(
            FakeCompletedProcess([], 0, "true\n"),
            FakeCompletedProcess([], 0, "https://github.com/owner/repo.git\n"),
            FakeCompletedProcess([], 0, "5555555555555555555555555555555555555555\n"),
            FakeCompletedProcess([], 0, "\n".join(
                [
                    "0000000000000000000000000000000000000000\tA\tone@example.com\t2026-08-01T00:00:00+00:00",
                    "1111111111111111111111111111111111111111\tB\ttwo@example.com\t2026-08-02T00:00:00+00:00",
                    "2222222222222222222222222222222222222222\tC\tthree@example.com\t2026-08-03T00:00:00+00:00",
                    "3333333333333333333333333333333333333333\tD\tfour@example.com\t2026-08-04T00:00:00+00:00",
                    "4444444444444444444444444444444444444444\tE\tfive@example.com\t2026-08-05T00:00:00+00:00",
                    "5555555555555555555555555555555555555555\tF\tsix@example.com\t2026-08-06T00:00:00+00:00",
                ]
            ) + "\n"),
            FakeCompletedProcess([], 0, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\tMona\tmona@example.com\t2026-08-10T00:00:00+00:00\n"),
            FakeCompletedProcess([], 0, "cccccccccccccccccccccccccccccccccccccccc\tMona\tmona@example.com\t2026-08-10T00:00:00+00:00\n"),
            FakeCompletedProcess([], 0, "dddddddddddddddddddddddddddddddddddddddd\tMona\tmona@example.com\t2026-08-10T00:00:00+00:00\n"),
        )

        enriched = self.make_collector().enrich_ownership_evidence(
            self.inventory_with_paths(),
            checkout_path=self.artifact_dir / "checkout",
            git_runner=runner,
        )

        self.assertEqual(5, len(enriched.evidence["source:docs%2Fguide.md"]["payload"]["recentCommits"]))


if __name__ == "__main__":
    unittest.main()
