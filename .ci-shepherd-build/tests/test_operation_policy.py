from __future__ import annotations

import hashlib
import unittest
from datetime import UTC, datetime, timedelta

from ci_shepherd.models import stable_json
from ci_shepherd.operation_policy import (
    DEFAULT_CAPS,
    DEFAULT_EXPIRY_DAYS,
    HARD_MAX_PER_RUN,
    HARD_MAX_ROLLING_24H,
    MAX_EXPIRY_DAYS,
    OPERATION_CLASS_BY_OPERATION,
    OPERATION_CLASSES,
    OperationPolicyError,
    classify_operation,
    load_operation_policy_document,
)


def policy_document(
    *,
    revision: int = 1,
    status: str = "active",
    created_at_utc: datetime = datetime(2026, 9, 3, 16, 0, tzinfo=UTC),
    expires_at_utc: datetime | None = None,
    repository: str = "microsoft/aspire",
    actor: str = "github:radical",
    replaces_revision_id: str | None = None,
) -> dict[str, object]:
    expires_at_utc = expires_at_utc or (
        created_at_utc + timedelta(days=DEFAULT_EXPIRY_DAYS)
    )
    return {
        "schemaVersion": 1,
        "repository": repository,
        "revisionId": f"policy:{revision}",
        "revision": revision,
        "status": status,
        "createdAtUtc": created_at_utc.isoformat().replace("+00:00", "Z"),
        "expiresAtUtc": expires_at_utc.isoformat().replace("+00:00", "Z"),
        "actor": actor,
        "replacesRevisionId": replaces_revision_id,
        "operationClasses": {
            name: {
                "enabled": name == "edit-comment",
                "maxPerRun": caps["maxPerRun"],
                "maxRolling24h": caps["maxRolling24h"],
            }
            for name, caps in DEFAULT_CAPS.items()
        },
        "deniedActionIds": [],
        "deniedTargets": [],
    }


class OperationPolicyTests(unittest.TestCase):
    def test_loads_complete_active_policy_and_digest(self) -> None:
        document = policy_document()

        policy = load_operation_policy_document(document)

        self.assertEqual(1, policy.schema_version)
        self.assertEqual("microsoft/aspire", policy.repository)
        self.assertEqual("policy:1", policy.revision_id)
        self.assertEqual(1, policy.revision)
        self.assertEqual("active", policy.status)
        self.assertEqual("github:radical", policy.actor)
        self.assertIsNone(policy.replaces_revision_id)
        self.assertTrue(policy.operation_classes["edit-comment"].enabled)
        self.assertEqual((), policy.denied_action_ids)
        self.assertEqual((), policy.denied_targets)
        self.assertEqual(
            "sha256:"
            + hashlib.sha256(stable_json(document).encode("utf-8")).hexdigest(),
            policy.digest,
        )

    def test_digest_uses_original_non_z_timestamps(self) -> None:
        document = policy_document()
        document["createdAtUtc"] = "2026-09-03T12:00:00-04:00"
        document["expiresAtUtc"] = "2026-10-03T12:00:00-04:00"

        policy = load_operation_policy_document(document)
        normalized_digest = "sha256:" + hashlib.sha256(
            stable_json(policy.as_public_dict()).encode("utf-8")
        ).hexdigest()

        self.assertEqual(
            "sha256:" + hashlib.sha256(stable_json(document).encode("utf-8")).hexdigest(),
            policy.digest,
        )
        self.assertEqual("2026-09-03T16:00:00Z", policy.as_public_dict()["createdAtUtc"])
        self.assertNotEqual(normalized_digest, policy.digest)

    def test_rejects_non_json_serializable_document_when_digesting(self) -> None:
        document = policy_document()
        document["operationClasses"]["edit-comment"]["enabled"] = {1}

        with self.assertRaises(OperationPolicyError) as context:
            load_operation_policy_document(document)

        self.assertIn("JSON serializable", str(context.exception))
        self.assertIsInstance(context.exception.__cause__, TypeError)

    def test_accepts_total_per_run_caps_at_hard_ceiling(self) -> None:
        document = policy_document()
        for name in OPERATION_CLASSES:
            document["operationClasses"][name]["maxPerRun"] = 0
        document["operationClasses"]["edit-comment"]["maxPerRun"] = HARD_MAX_PER_RUN

        policy = load_operation_policy_document(document)

        self.assertEqual(
            HARD_MAX_PER_RUN,
            sum(policy.operation_classes[name].max_per_run for name in OPERATION_CLASSES),
        )

    def test_accepts_total_rolling_24h_caps_at_hard_ceiling(self) -> None:
        document = policy_document()
        for name in OPERATION_CLASSES:
            document["operationClasses"][name]["maxRolling24h"] = 0
        document["operationClasses"]["edit-comment"]["maxRolling24h"] = (
            HARD_MAX_ROLLING_24H
        )

        policy = load_operation_policy_document(document)

        self.assertEqual(
            HARD_MAX_ROLLING_24H,
            sum(
                policy.operation_classes[name].max_rolling_24h
                for name in OPERATION_CLASSES
            ),
        )

    def test_rejects_total_per_run_caps_above_hard_ceiling(self) -> None:
        document = policy_document()
        document["operationClasses"]["edit-comment"]["maxPerRun"] = HARD_MAX_PER_RUN
        document["operationClasses"]["create-comment"]["maxPerRun"] = 1

        with self.assertRaisesRegex(OperationPolicyError, "100 per run"):
            load_operation_policy_document(document)

    def test_rejects_total_rolling_24h_caps_above_hard_ceiling(self) -> None:
        document = policy_document()
        document["operationClasses"]["edit-comment"]["maxRolling24h"] = HARD_MAX_ROLLING_24H
        document["operationClasses"]["create-comment"]["maxRolling24h"] = 1

        with self.assertRaisesRegex(OperationPolicyError, "300 per 24h"):
            load_operation_policy_document(document)

    def test_rejects_expiry_beyond_ninety_days(self) -> None:
        document = policy_document(
            expires_at_utc=datetime(2026, 12, 2, 16, 0, 1, tzinfo=UTC)
        )

        with self.assertRaisesRegex(OperationPolicyError, "90 days"):
            load_operation_policy_document(document)

    def test_accepts_expiry_at_ninety_days(self) -> None:
        created_at = datetime(2026, 9, 3, 16, 0, tzinfo=UTC)
        expires_at = created_at + timedelta(days=MAX_EXPIRY_DAYS)
        document = policy_document(
            created_at_utc=created_at,
            expires_at_utc=expires_at,
        )

        policy = load_operation_policy_document(document)

        self.assertEqual(expires_at, policy.expires_at_utc)

    def test_rejects_non_increasing_timestamps(self) -> None:
        document = policy_document(
            expires_at_utc=datetime(2026, 9, 3, 16, 0, tzinfo=UTC)
        )

        with self.assertRaisesRegex(OperationPolicyError, "earlier than expiresAtUtc"):
            load_operation_policy_document(document)

    def test_classifies_supported_operations(self) -> None:
        self.assertEqual("create-comment", classify_operation("create-comment"))
        self.assertEqual("edit-comment", classify_operation("edit-comment"))
        self.assertEqual("close-issue", classify_operation("close-issue"))
        self.assertEqual("delegate-copilot", classify_operation("assign-copilot"))
        self.assertIsNone(classify_operation("unassign-copilot"))
        self.assertIsNone(classify_operation("prepare-quarantine-pr"))
        self.assertIsNone(classify_operation("request-rerun"))

    def test_default_caps_match_the_approved_design(self) -> None:
        self.assertEqual(
            {
                "create-comment": {"maxPerRun": 10, "maxRolling24h": 30},
                "edit-comment": {"maxPerRun": 10, "maxRolling24h": 30},
                "close-issue": {"maxPerRun": 5, "maxRolling24h": 10},
                "delegate-copilot": {"maxPerRun": 3, "maxRolling24h": 5},
                "rerun-or-retry": {"maxPerRun": 5, "maxRolling24h": 15},
            },
            DEFAULT_CAPS,
        )
        self.assertEqual(
            (
                "create-comment",
                "edit-comment",
                "close-issue",
                "delegate-copilot",
                "rerun-or-retry",
            ),
            OPERATION_CLASSES,
        )
        self.assertEqual(
            {
                "create-comment": "create-comment",
                "edit-comment": "edit-comment",
                "close-issue": "close-issue",
                "assign-copilot": "delegate-copilot",
            },
            OPERATION_CLASS_BY_OPERATION,
        )
        self.assertEqual(100, HARD_MAX_PER_RUN)
        self.assertEqual(300, HARD_MAX_ROLLING_24H)
        self.assertEqual(30, DEFAULT_EXPIRY_DAYS)
        self.assertEqual(90, MAX_EXPIRY_DAYS)

    def test_rejects_unknown_top_level_field(self) -> None:
        document = policy_document()
        document["unexpected"] = "surprise"

        with self.assertRaisesRegex(OperationPolicyError, "unknown fields"):
            load_operation_policy_document(document)

    def test_rejects_missing_top_level_field(self) -> None:
        document = policy_document()
        del document["actor"]

        with self.assertRaisesRegex(OperationPolicyError, "missing fields"):
            load_operation_policy_document(document)

    def test_rejects_missing_operation_class(self) -> None:
        document = policy_document()
        del document["operationClasses"]["rerun-or-retry"]

        with self.assertRaisesRegex(OperationPolicyError, "rerun-or-retry"):
            load_operation_policy_document(document)

    def test_rejects_unknown_operation_class(self) -> None:
        document = policy_document()
        document["operationClasses"]["unexpected"] = {
            "enabled": False,
            "maxPerRun": 0,
            "maxRolling24h": 0,
        }

        with self.assertRaisesRegex(OperationPolicyError, "unexpected"):
            load_operation_policy_document(document)

    def test_rejects_boolean_used_as_integer(self) -> None:
        document = policy_document()
        document["operationClasses"]["edit-comment"]["maxPerRun"] = True

        with self.assertRaisesRegex(OperationPolicyError, "maxPerRun"):
            load_operation_policy_document(document)

    def test_rejects_negative_caps(self) -> None:
        document = policy_document()
        document["operationClasses"]["edit-comment"]["maxRolling24h"] = -1

        with self.assertRaisesRegex(OperationPolicyError, "maxRolling24h"):
            load_operation_policy_document(document)

    def test_rejects_nonpositive_revision(self) -> None:
        document = policy_document(revision=0)

        with self.assertRaisesRegex(OperationPolicyError, "revision"):
            load_operation_policy_document(document)

    def test_rejects_replaces_revision_for_revision_one(self) -> None:
        document = policy_document(replaces_revision_id="policy:1")

        with self.assertRaisesRegex(OperationPolicyError, "revision 1"):
            load_operation_policy_document(document)

    def test_rejects_invalid_status(self) -> None:
        document = policy_document(status="draft")

        with self.assertRaisesRegex(OperationPolicyError, "status"):
            load_operation_policy_document(document)

    def test_rejects_missing_replaces_revision_for_later_revision(self) -> None:
        document = policy_document(revision=2, replaces_revision_id=None)

        with self.assertRaisesRegex(OperationPolicyError, "replacesRevisionId"):
            load_operation_policy_document(document)

    def test_accepts_non_adjacent_valid_replaces_revision_identity(self) -> None:
        document = policy_document(revision=3, replaces_revision_id="policy:1")

        policy = load_operation_policy_document(document)

        self.assertEqual("policy:1", policy.replaces_revision_id)

    def test_rejects_duplicate_denied_action_ids(self) -> None:
        document = policy_document()
        document["deniedActionIds"] = ["action-1", "action-1"]

        with self.assertRaisesRegex(OperationPolicyError, "duplicate"):
            load_operation_policy_document(document)

    def test_rejects_duplicate_denied_targets(self) -> None:
        document = policy_document()
        document["deniedTargets"] = ["issue:1", "issue:1"]

        with self.assertRaisesRegex(OperationPolicyError, "duplicate"):
            load_operation_policy_document(document)

    def test_rejects_malformed_repository_identity(self) -> None:
        document = policy_document(repository="microsoft")

        with self.assertRaisesRegex(OperationPolicyError, "repository"):
            load_operation_policy_document(document)

    def test_rejects_malformed_actor_identity(self) -> None:
        document = policy_document(actor="radical")

        with self.assertRaisesRegex(OperationPolicyError, "actor"):
            load_operation_policy_document(document)

    def test_rejects_revision_identity_mismatch(self) -> None:
        document = policy_document(revision=2, replaces_revision_id="policy:1")
        document["revisionId"] = "policy:wrong"

        with self.assertRaisesRegex(OperationPolicyError, "revisionId"):
            load_operation_policy_document(document)

    def test_rejects_naive_timestamps(self) -> None:
        document = policy_document()
        document["createdAtUtc"] = "2026-09-03T16:00:00"

        with self.assertRaisesRegex(OperationPolicyError, "timezone-aware"):
            load_operation_policy_document(document)

    def test_active_at_requires_active_unexpired_policy(self) -> None:
        now = datetime(2026, 9, 3, 16, 1, tzinfo=UTC)
        created_at = datetime(2026, 9, 3, 16, 0, tzinfo=UTC)

        active = load_operation_policy_document(policy_document(created_at_utc=created_at))
        paused = load_operation_policy_document(policy_document(status="paused"))
        revoked = load_operation_policy_document(policy_document(status="revoked"))
        expired = load_operation_policy_document(
            policy_document(expires_at_utc=datetime(2026, 9, 3, 16, 0, 1, tzinfo=UTC))
        )

        self.assertFalse(active.active_at(created_at - timedelta(seconds=1)))
        self.assertTrue(active.active_at(created_at))
        self.assertTrue(active.active_at(now))
        self.assertFalse(paused.active_at(now))
        self.assertFalse(revoked.active_at(now))
        self.assertFalse(expired.active_at(datetime(2026, 9, 3, 16, 0, 1, tzinfo=UTC)))
