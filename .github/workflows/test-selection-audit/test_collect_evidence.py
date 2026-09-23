"""Tests for collect_evidence.py."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import collect_evidence  # noqa: E402


_BASE_SHA = "1" * 40
_HEAD_SHA = "2" * 40


def _payload(change_source: str) -> dict:
    return {
        "schemaVersion": 1,
        "inputs": {"changeSource": change_source},
        "selectsAll": False,
        "escalationReason": "test reason",
        "changedFiles": ["z/file.cs", "a/file.cs"],
        "excludedFiles": [],
        "unattributedFiles": [],
        "testProjects": [{"name": "Z.Tests"}, {"name": "A.Tests"}],
        "jobs": [{"name": "z-job"}, {"name": "a-job"}],
    }


class NormalizeSelectionTests(unittest.TestCase):
    def test_normalizes_diff_source_and_sorts_collections(self) -> None:
        result = collect_evidence.normalize_selection(
            _payload(f"git diff {_BASE_SHA}..{_HEAD_SHA}"),
            include_reason=True,
        )

        self.assertTrue(result["sourceHasDiff"])
        self.assertEqual(result["sourceBaseSha"], _BASE_SHA)
        self.assertEqual(result["sourceHeadSha"], _HEAD_SHA)
        self.assertEqual(result["changedFiles"], ["a/file.cs", "z/file.cs"])
        self.assertEqual(result["testProjects"], ["A.Tests", "Z.Tests"])
        self.assertEqual(result["jobs"], ["a-job", "z-job"])
        self.assertEqual(result["escalationReason"], "test reason")

    def test_normalizes_force_all_source_without_trusting_reason(self) -> None:
        payload = _payload("(none -- force-all or unset)")
        payload["selectsAll"] = True

        result = collect_evidence.normalize_selection(payload, include_reason=False)

        self.assertFalse(result["sourceHasDiff"])
        self.assertIsNone(result["sourceBaseSha"])
        self.assertIsNone(result["sourceHeadSha"])
        self.assertIsNone(result["escalationReason"])

    def test_rejects_unsupported_change_source(self) -> None:
        payload = _payload(f"git diff {_BASE_SHA}...{_HEAD_SHA}")

        with self.assertRaisesRegex(ValueError, "changeSource is unsupported"):
            collect_evidence.normalize_selection(payload, include_reason=True)

    def test_rejects_unsupported_schema(self) -> None:
        payload = _payload(f"git diff {_BASE_SHA}..{_HEAD_SHA}")
        payload["schemaVersion"] = 2

        with self.assertRaisesRegex(ValueError, "unsupported schema"):
            collect_evidence.normalize_selection(payload, include_reason=True)

    def test_rejects_duplicate_paths(self) -> None:
        payload = _payload(f"git diff {_BASE_SHA}..{_HEAD_SHA}")
        payload["changedFiles"] = ["src/a.cs", "src/a.cs"]

        with self.assertRaisesRegex(ValueError, "contains duplicate"):
            collect_evidence.normalize_selection(payload, include_reason=True)

    def test_rejects_duplicate_named_items(self) -> None:
        payload = _payload(f"git diff {_BASE_SHA}..{_HEAD_SHA}")
        payload["jobs"] = [{"name": "build"}, {"name": "build"}]

        with self.assertRaisesRegex(ValueError, "contains duplicate"):
            collect_evidence.normalize_selection(payload, include_reason=True)


class SelectionStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        collect_evidence.repository = "microsoft/aspire"
        collect_evidence.processed_index = {}

    def test_same_repository_artifact_is_creditable(self) -> None:
        record = self._find_record("microsoft/aspire", normalized_head=_HEAD_SHA)

        self.assertEqual(record["selection"]["status"], "resolved")
        self.assertTrue(record["selection"]["creditable"])

    def test_fork_artifact_is_untrusted(self) -> None:
        record = self._find_record("radical/aspire", normalized_head=_HEAD_SHA)

        self.assertEqual(record["selection"]["status"], "untrusted-fork-artifact")
        self.assertFalse(record["selection"]["creditable"])

    def test_mismatched_artifact_head_is_not_creditable(self) -> None:
        record = self._find_record("microsoft/aspire", normalized_head="3" * 40)

        self.assertEqual(record["selection"]["status"], "artifact-head-mismatch")
        self.assertFalse(record["selection"]["creditable"])

    def test_exact_processed_attempt_is_recorded(self) -> None:
        collect_evidence.processed_index = {(123, _HEAD_SHA): (456, 2)}
        pull_request = self._pull_request("microsoft/aspire")
        runs = [self._run("microsoft/aspire")]

        with mock.patch.object(
            collect_evidence,
            "paginate",
            return_value=(runs, False),
        ):
            record = collect_evidence.find_selection_record(pull_request)

        self.assertEqual(record["selection"]["status"], "recorded")

    def _find_record(self, head_repository: str, normalized_head: str) -> dict:
        pull_request = self._pull_request(head_repository)
        run = self._run(head_repository)
        associated = [pull_request]
        jobs = [{
            "name": "CI / Tests / Setup for tests",
            "run_attempt": 2,
            "status": "completed",
            "completed_at": "2026-09-22T10:05:00Z",
            "steps": [{
                "name": "Select relevant tests",
                "conclusion": "success",
                "started_at": "2026-09-22T10:00:00Z",
            }],
        }]
        artifacts = [{
            "id": 789,
            "name": collect_evidence.ARTIFACT_NAME,
            "created_at": "2026-09-22T10:02:00Z",
            "expired": False,
            "workflow_run": {"id": 456, "head_sha": _HEAD_SHA},
        }]

        def paginate(path, parameters=None, key=None, max_pages=20):
            if path.endswith("/actions/runs"):
                return [run], False
            if path.endswith(f"/commits/{_HEAD_SHA}/pulls"):
                return associated, False
            if path.endswith("/jobs"):
                return jobs, False
            if path.endswith("/artifacts"):
                return artifacts, False
            self.fail(f"Unexpected pagination path: {path}")

        normalized = collect_evidence.normalize_selection(
            _payload(f"git diff {_BASE_SHA}..{normalized_head}"),
            include_reason=head_repository == collect_evidence.repository,
        )
        with (
            mock.patch.object(collect_evidence, "paginate", side_effect=paginate),
            mock.patch.object(collect_evidence, "list_changed_files", return_value=(["src/a.cs"], False)),
            mock.patch.object(collect_evidence, "download_selection", return_value=normalized),
        ):
            return collect_evidence.find_selection_record(pull_request)

    @staticmethod
    def _pull_request(head_repository: str) -> dict:
        return {
            "number": 123,
            "head": {
                "sha": _HEAD_SHA,
                "ref": "feature",
                "repo": {"full_name": head_repository},
            },
        }

    @staticmethod
    def _run(head_repository: str) -> dict:
        return {
            "id": 456,
            "path": ".github/workflows/ci.yml",
            "head_sha": _HEAD_SHA,
            "head_branch": "feature",
            "head_repository": {"full_name": head_repository},
            "created_at": "2026-09-22T09:59:00Z",
            "run_attempt": 2,
            "status": "completed",
            "conclusion": "success",
        }


if __name__ == "__main__":
    unittest.main()
