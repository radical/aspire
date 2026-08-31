from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from ci_shepherd.policy import ManualPolicy, PolicyError, load_policy, load_policy_document
from ci_shepherd.repository_policy import (
    RepositoryPolicyError,
    load_repository_policy,
    load_repository_policy_document,
)


POLICY_PATH = Path(__file__).resolve().parents[1] / "policies" / "manual-v1.json"
ASPIRE_REPOSITORY_POLICY_PATH = (
    Path(__file__).resolve().parents[1]
    / "policies"
    / "repositories"
    / "aspire-v1.json"
)


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_root = Path(__file__).resolve().parent / ".tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        self._temp_dir = tempfile.TemporaryDirectory(dir=temp_root)
        self.addCleanup(self._temp_dir.cleanup)

    def test_checked_in_policy_loads_with_expected_values(self) -> None:
        policy = load_policy(POLICY_PATH)

        self.assertEqual(1, policy.as_public_dict()["schemaVersion"])
        self.assertEqual("manual-v1", policy.policy_version)
        self.assertEqual(14, policy.systemic_transient_window_days)
        self.assertEqual(frozenset(), policy.retry_safe_pattern_ids)

    def test_manual_policy_as_public_dict_returns_fresh_camel_case_projection(self) -> None:
        policy = load_policy(POLICY_PATH)

        public = policy.as_public_dict()

        self.assertEqual(
            {
                "schemaVersion": 1,
                "policyVersion": "manual-v1",
                "systemicTransientWindowDays": 14,
                "retrySafePatternIds": [],
            },
            public,
        )

        public["retrySafePatternIds"].append("retry-safe-pattern")

        self.assertEqual(frozenset(), policy.retry_safe_pattern_ids)
        self.assertEqual([], policy.as_public_dict()["retrySafePatternIds"])

    def test_policy_rejects_unknown_fields(self) -> None:
        document = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        document["unexpectedField"] = "surprise"

        with self.assertRaisesRegex(PolicyError, "unknown fields"):
            load_policy_document(document)

    def test_policy_rejects_non_string_keys(self) -> None:
        document = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        document[1] = "surprise"

        with self.assertRaises(PolicyError):
            load_policy_document(document)

    def test_policy_rejects_duplicate_retry_safe_pattern_ids(self) -> None:
        document = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        document["retrySafePatternIds"] = ["retry-safe", "retry-safe"]

        with self.assertRaisesRegex(PolicyError, "duplicate"):
            load_policy_document(document)

    def test_policy_normalizes_retry_safe_pattern_ids_to_canonical_components(self) -> None:
        document = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        document["retrySafePatternIds"] = ["HTTP_502", "Runner Lost", "  dns-failure  "]

        policy = load_policy_document(document)

        self.assertEqual(
            frozenset({"http-502", "runner-lost", "dns-failure"}),
            policy.retry_safe_pattern_ids,
        )
        self.assertEqual(
            ["dns-failure", "http-502", "runner-lost"],
            policy.as_public_dict()["retrySafePatternIds"],
        )

    def test_policy_rejects_retry_safe_pattern_ids_that_collide_after_normalization(self) -> None:
        document = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        document["retrySafePatternIds"] = ["HTTP_502", "http-502"]

        with self.assertRaisesRegex(PolicyError, "duplicate"):
            load_policy_document(document)

    def test_policy_rejects_retry_safe_pattern_id_with_no_canonical_form(self) -> None:
        document = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        document["retrySafePatternIds"] = ["***"]

        with self.assertRaisesRegex(PolicyError, "canonical"):
            load_policy_document(document)

    def test_policy_rejects_duplicate_json_keys(self) -> None:
        path = self._policy_path()
        path.write_text(
            """
{
  "schemaVersion": 1,
  "policyVersion": "manual-v1",
  "systemicTransientWindowDays": 14,
  "retrySafePatternIds": [],
  "retrySafePatternIds": ["duplicate"]
}
""".strip(),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(PolicyError, "duplicate JSON key: retrySafePatternIds"):
            load_policy(path)

    def test_policy_rejects_invalid_utf8(self) -> None:
        path = self._policy_path()
        path.write_bytes(b'{"schemaVersion": 1, "policyVersion": "manual-v1"}\xff')

        with self.assertRaisesRegex(PolicyError, "invalid UTF-8"):
            load_policy(path)

    def test_policy_rejects_malformed_json(self) -> None:
        path = self._policy_path()
        path.write_text('{"schemaVersion": 1, "policyVersion": "manual-v1"', encoding="utf-8")

        with self.assertRaisesRegex(PolicyError, "not valid JSON"):
            load_policy(path)

    def _policy_path(self) -> Path:
        return Path(self._temp_dir.name) / "policy.json"


class RepositoryPolicyTests(unittest.TestCase):
    def test_checked_in_aspire_repository_policy_loads(self) -> None:
        policy = load_repository_policy(ASPIRE_REPOSITORY_POLICY_PATH)

        self.assertEqual("aspire-v1", policy.policy_version)
        self.assertTrue(policy.supports_repository("microsoft/aspire"))
        self.assertTrue(policy.supports_repository("radical/aspire"))
        self.assertTrue(
            policy.retry_test_results.matches_aggregate_job(
                "Tests / Final Test Results"
            )
        )
        self.assertTrue(
            policy.retry_test_results.matches_artifact("All-TestResults")
        )
        self.assertEqual(
            ("Hosting-1", "windows-latest"),
            policy.retry_test_results.identify_trx(
                "windows-latest/testresults/"
                "Hosting-1_net10.0_20260830120000.trx"
            ),
        )
        self.assertEqual(
            ("Hosting.Keycloak", "windows-latest"),
            policy.retry_test_results.identify_trx(
                "windows-latest/testresults/Hosting.Keycloak.trx"
            ),
        )
        self.assertIsNone(
            policy.retry_test_results.identify_trx(
                "windows-latest/testresults/nested/Hosting.Keycloak.trx"
            )
        )
        self.assertTrue(
            policy.retry_test_results.matches_test_job(
                "Tests / Hosting-1 (windows-latest)",
                lane="Hosting-1",
                os_name="windows-latest",
            )
        )
        self.assertTrue(
            policy.retry_test_results.trusts_run(
                event="push",
                head_repository="microsoft/aspire",
                target_repository="microsoft/aspire",
            )
        )
        self.assertFalse(
            policy.retry_test_results.trusts_run(
                event="pull_request",
                head_repository="contributor/aspire",
                target_repository="microsoft/aspire",
            )
        )

    def test_repository_policy_exposes_stable_public_identity(self) -> None:
        policy = load_repository_policy(ASPIRE_REPOSITORY_POLICY_PATH)

        self.assertEqual(
            {
                "schemaVersion": 1,
                "policyVersion": "aspire-v1",
                "repositories": [
                    "microsoft/aspire",
                    "radical/aspire",
                ],
                "retryTestResults": {
                    "aggregateJobSuffixes": ["Final Test Results"],
                    "artifactNames": ["All-TestResults"],
                    "trxPathPattern": (
                        r"^(?P<os>[^/]+)/testresults/"
                        r"(?P<lane>[^/]+?)(?=_net[^_]+_[^/]+\.trx$|\.trx$)"
                        r"(?:_net[^_]+_[^/]+)?\.trx$"
                    ),
                    "jobNamePattern": (
                        r"^(?:.* / )?(?P<lane>[^/]+) "
                        r"\((?P<os>[^()]+)\)$"
                    ),
                    "trustedEvents": [
                        "push",
                        "schedule",
                        "workflow_dispatch",
                    ],
                    "requireHeadRepositoryMatch": True,
                },
                "quarantinePullRequest": {
                    "baseRef": "main",
                    "allowedHeadRepositories": ["radical/aspire"],
                    "requiredApprovingReviews": 1,
                },
            },
            policy.as_public_dict(),
        )
        self.assertRegex(policy.digest, r"^sha256:[0-9a-f]{64}$")

    def test_repository_policy_requires_at_least_one_quarantine_approval(
        self,
    ) -> None:
        document = json.loads(
            ASPIRE_REPOSITORY_POLICY_PATH.read_text(encoding="utf-8")
        )
        document["quarantinePullRequest"]["requiredApprovingReviews"] = 0
        with self.assertRaisesRegex(RepositoryPolicyError, "1 through 10"):
            load_repository_policy_document(document)

    @unittest.skipIf(os.name == "nt", "Windows symlink creation requires privileges.")
    def test_repository_policy_rejects_symlinked_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            target.write_bytes(ASPIRE_REPOSITORY_POLICY_PATH.read_bytes())
            link = root / "policy.json"
            link.symlink_to(target)

            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                load_repository_policy(link)

    @unittest.skipIf(os.name == "nt", "Windows does not expose POSIX write modes.")
    def test_repository_policy_rejects_files_writable_by_other_users(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policy_path = Path(directory) / "policy.json"
            policy_path.write_bytes(ASPIRE_REPOSITORY_POLICY_PATH.read_bytes())
            policy_path.chmod(0o666)

            with self.assertRaisesRegex(ValueError, "writable by other users"):
                load_repository_policy(policy_path)


def manual_policy(retry_safe_pattern_ids: object) -> ManualPolicy:
    return ManualPolicy(
        policy_version="manual-v1",
        systemic_transient_window_days=14,
        retry_safe_pattern_ids=retry_safe_pattern_ids,
    )


class ManualPolicyCanonicalizationTests(unittest.TestCase):
    def test_already_canonical_mutable_set_is_replaced_with_a_frozenset(self) -> None:
        policy = manual_policy({"http-502"})

        self.assertIsInstance(policy.retry_safe_pattern_ids, frozenset)

    def test_policy_built_from_a_mutable_set_stays_hashable(self) -> None:
        policy = manual_policy({"http-502"})

        self.assertEqual(policy, manual_policy(frozenset({"http-502"})))
        self.assertEqual(hash(policy), hash(manual_policy(frozenset({"http-502"}))))

    def test_mutating_the_caller_set_afterwards_does_not_change_the_policy(self) -> None:
        pattern_ids = {"http-502"}
        policy = manual_policy(pattern_ids)

        pattern_ids.add("http-503")

        self.assertEqual(frozenset({"http-502"}), policy.retry_safe_pattern_ids)

    def test_non_canonical_values_are_normalized(self) -> None:
        policy = manual_policy({"HTTP 502"})

        self.assertEqual(frozenset({"http-502"}), policy.retry_safe_pattern_ids)
