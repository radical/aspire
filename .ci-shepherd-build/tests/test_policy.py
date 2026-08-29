from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ci_shepherd.policy import ManualPolicy, PolicyError, load_policy, load_policy_document


POLICY_PATH = Path(__file__).resolve().parents[1] / "policies" / "manual-v1.json"


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
