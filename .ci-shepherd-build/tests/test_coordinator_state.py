from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock
import hashlib
import json
import os
import shutil
import stat
import threading
import unittest

from ci_shepherd import coordinator_state, operation_policy
from ci_shepherd.coordinator_state import (
    CoordinatorStateError,
    CoordinatorStateStore,
    make_lock_free_durable_intent_reader,
)
from ci_shepherd.execution_state import ActionEventStore
from ci_shepherd.operation_policy import DEFAULT_CAPS, DEFAULT_EXPIRY_DAYS, OPERATION_CLASSES

# Task 5's concrete PolicyBudgetValidator is deliberately not reimplemented
# here: it is exercised end-to-end (including every budget/revocation
# invariant) in tests.test_execution_state, so this module only reuses it,
# via a thin pausing wrapper, to prove the real single-machine lock order
# and the absence of a validate-then-revoke gap.
from tests.test_execution_state import CoordinatorPolicyBudgetValidator, _autonomous_grant


REPOSITORY = "microsoft/aspire"


def policy_document(
    *,
    revision: int = 1,
    replaces: str | None = None,
    status: str = "active",
    created_at_utc: datetime = datetime(2026, 9, 3, 16, 0, tzinfo=UTC),
    expires_at_utc: datetime | None = None,
    repository: str = REPOSITORY,
    actor: str = "github:radical",
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
        "replacesRevisionId": replaces,
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


def _proposals_document(
    *,
    repository: str = REPOSITORY,
    action_ids: tuple[str, ...] = ("snapshot:microsoft/aspire:time:issue:7:retire-status-comment",),
    generated_at_utc: str = "2026-09-03T16:00:00Z",
    proposal_ttl_hours: int = 24,
    duplicate_action_id: str | None = None,
) -> dict[str, object]:
    proposals = [
        {
            "actionId": action_id,
            "issueNumber": 7,
            "operation": "edit-comment",
        }
        for action_id in action_ids
    ]
    if duplicate_action_id is not None:
        proposals.append(
            {
                "actionId": duplicate_action_id,
                "issueNumber": 7,
                "operation": "edit-comment",
            }
        )
    return {
        "schemaVersion": 2,
        "repository": repository,
        "snapshotId": "snapshot:microsoft/aspire:time",
        "shepherdAuthor": "radical",
        "generatedAtUtc": generated_at_utc,
        "proposalTtlHours": proposal_ttl_hours,
        "proposals": proposals,
    }


def _write_proposals(path: Path, **kwargs: object) -> Path:
    path.write_text(json.dumps(_proposals_document(**kwargs)), encoding="utf-8")
    return path


def _no_durable_intent(_action_id: str) -> bool:
    return False


class CoordinatorStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch.mkdir(parents=True)
        self.state_dir = (self.scratch / "state").resolve()
        self.proposals_path = self.scratch / "action-proposals.json"
        _write_proposals(self.proposals_path)
        self.store = CoordinatorStateStore(
            self.state_dir,
            durable_intent_reader=_no_durable_intent,
        )

    def test_first_policy_append_at_expected_revision_zero_returns_state_revision_one(
        self,
    ) -> None:
        result = self.store.append_policy_revision(
            repository=REPOSITORY,
            expected_revision=0,
            document=policy_document(revision=1, replaces=None),
        )

        self.assertEqual(1, result["stateRevision"])

    def test_stale_expected_revision_raises_stale_view_error(self) -> None:
        self.store.append_policy_revision(
            repository=REPOSITORY,
            expected_revision=0,
            document=policy_document(revision=1, replaces=None),
        )

        with self.assertRaisesRegex(CoordinatorStateError, "stale-view"):
            self.store.append_policy_revision(
                repository=REPOSITORY,
                expected_revision=0,
                document=policy_document(revision=2, replaces="policy:1"),
            )

    def test_exact_decision_derives_digest_and_expiry_and_advances_revision(
        self,
    ) -> None:
        self.store.append_policy_revision(
            repository=REPOSITORY,
            expected_revision=0,
            document=policy_document(revision=1, replaces=None),
        )

        decision = self.store.append_exact_decision(
            repository=REPOSITORY,
            expected_revision=1,
            proposals_path=self.proposals_path,
            action_id="snapshot:microsoft/aspire:time:issue:7:retire-status-comment",
            decision="reject-once",
            actor="github:radical",
            now=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
        )

        self.assertEqual(2, decision["stateRevision"])
        projection = self.store.projection(REPOSITORY)
        self.assertEqual(
            "reject-once", projection["exactDecisions"][0]["decision"]
        )
        self.assertTrue(
            projection["exactDecisions"][0]["proposalDigest"].startswith("sha256:")
        )

    def test_concurrent_appends_at_same_expected_revision_yield_one_success(
        self,
    ) -> None:
        results: list[object] = []

        def _attempt(revision: int) -> None:
            try:
                results.append(
                    self.store.append_policy_revision(
                        repository=REPOSITORY,
                        expected_revision=0,
                        document=policy_document(revision=1, replaces=None),
                    )
                )
            except CoordinatorStateError as exc:
                results.append(exc)

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(_attempt, range(8)))

        successes = [r for r in results if isinstance(r, dict)]
        failures = [r for r in results if isinstance(r, CoordinatorStateError)]
        self.assertEqual(1, len(successes))
        self.assertEqual(7, len(failures))
        self.assertTrue(all("stale-view" in str(exc) for exc in failures))

    def test_revision_two_must_replace_currently_effective_revision_one_exactly(
        self,
    ) -> None:
        self.store.append_policy_revision(
            repository=REPOSITORY,
            expected_revision=0,
            document=policy_document(revision=1, replaces=None),
        )

        with self.assertRaisesRegex(CoordinatorStateError, "currently effective"):
            self.store.append_policy_revision(
                repository=REPOSITORY,
                expected_revision=1,
                document=policy_document(revision=3, replaces="policy:2"),
            )

        second = self.store.append_policy_revision(
            repository=REPOSITORY,
            expected_revision=1,
            document=policy_document(revision=2, replaces="policy:1"),
        )
        self.assertEqual(2, second["stateRevision"])

    def test_pause_and_revoke_append_new_revisions_and_preserve_prior_bytes(
        self,
    ) -> None:
        self.store.append_policy_revision(
            repository=REPOSITORY,
            expected_revision=0,
            document=policy_document(revision=1, replaces=None),
        )
        ledger_path = self.state_dir / "coordinator" / "policy-events.jsonl"
        original_first_line = ledger_path.read_text(encoding="utf-8").splitlines()[0]

        self.store.append_policy_revision(
            repository=REPOSITORY,
            expected_revision=1,
            document=policy_document(revision=2, replaces="policy:1", status="paused"),
        )
        self.store.append_policy_revision(
            repository=REPOSITORY,
            expected_revision=2,
            document=policy_document(revision=3, replaces="policy:2", status="revoked"),
        )

        lines = ledger_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(3, len(lines))
        self.assertEqual(original_first_line, lines[0])
        projection = self.store.projection(REPOSITORY)
        self.assertEqual("revoked", projection["effectivePolicy"]["status"])

    def test_clear_appends_a_decision_event_instead_of_deleting_history(
        self,
    ) -> None:
        action_id = "snapshot:microsoft/aspire:time:issue:7:retire-status-comment"
        self.store.append_exact_decision(
            repository=REPOSITORY,
            expected_revision=0,
            proposals_path=self.proposals_path,
            action_id=action_id,
            decision="approve-once",
            actor="github:radical",
            now=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
        )
        self.store.append_exact_decision(
            repository=REPOSITORY,
            expected_revision=1,
            proposals_path=self.proposals_path,
            action_id=action_id,
            decision="clear",
            actor="github:radical",
            now=datetime(2026, 9, 3, 16, 6, tzinfo=UTC),
        )

        ledger_path = self.state_dir / "coordinator" / "policy-events.jsonl"
        self.assertEqual(
            2, len(ledger_path.read_text(encoding="utf-8").splitlines())
        )
        projection = self.store.projection(
            REPOSITORY, now=datetime(2026, 9, 3, 16, 7, tzinfo=UTC)
        )
        self.assertEqual([], projection["exactDecisions"])

    def test_decision_is_bound_to_action_id_digest_actor_and_derived_expiry(
        self,
    ) -> None:
        action_id = "snapshot:microsoft/aspire:time:issue:7:retire-status-comment"
        self.store.append_exact_decision(
            repository=REPOSITORY,
            expected_revision=0,
            proposals_path=self.proposals_path,
            action_id=action_id,
            decision="approve-once",
            actor="github:radical",
            now=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
        )

        entry = self.store.projection(
            REPOSITORY, now=datetime(2026, 9, 3, 16, 6, tzinfo=UTC)
        )["exactDecisions"][0]
        self.assertEqual(action_id, entry["actionId"])
        self.assertEqual("github:radical", entry["actor"])
        self.assertTrue(entry["proposalDigest"].startswith("sha256:"))
        self.assertEqual("2026-09-04T16:00:00Z", entry["expiresAtUtc"])

    def test_caller_supplied_digest_or_expiry_embedded_in_proposals_is_ignored(
        self,
    ) -> None:
        # The store must derive digest/expiry itself even if the proposals
        # document has been tampered with to carry fields that look like a
        # caller-supplied digest or expiry.
        action_id = "snapshot:microsoft/aspire:time:issue:7:retire-status-comment"
        tampered_path = self.scratch / "tampered-proposals.json"
        document = _proposals_document(action_ids=(action_id,))
        document["proposalDigest"] = f"sha256:{'0' * 64}"
        document["expiresAtUtc"] = "2099-01-01T00:00:00Z"
        tampered_path.write_text(json.dumps(document), encoding="utf-8")
        real_digest = f"sha256:{hashlib.sha256(tampered_path.read_bytes()).hexdigest()}"

        self.store.append_exact_decision(
            repository=REPOSITORY,
            expected_revision=0,
            proposals_path=tampered_path,
            action_id=action_id,
            decision="approve-once",
            actor="github:radical",
            now=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
        )

        entry = self.store.projection(
            REPOSITORY, now=datetime(2026, 9, 3, 16, 6, tzinfo=UTC)
        )["exactDecisions"][0]
        self.assertEqual(real_digest, entry["proposalDigest"])
        self.assertNotEqual(f"sha256:{'0' * 64}", entry["proposalDigest"])
        # Derived from generatedAtUtc (16:00Z) + proposalTtlHours (24), not the
        # bogus far-future value embedded above.
        self.assertEqual("2026-09-04T16:00:00Z", entry["expiresAtUtc"])

    def test_clear_after_durable_action_intent_exists_is_rejected(self) -> None:
        action_id = "snapshot:microsoft/aspire:time:issue:7:retire-status-comment"
        store = CoordinatorStateStore(
            self.state_dir,
            durable_intent_reader=lambda _action_id: True,
        )
        store.append_exact_decision(
            repository=REPOSITORY,
            expected_revision=0,
            proposals_path=self.proposals_path,
            action_id=action_id,
            decision="approve-once",
            actor="github:radical",
            now=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
        )

        with self.assertRaisesRegex(CoordinatorStateError, "durable action intent"):
            store.append_exact_decision(
                repository=REPOSITORY,
                expected_revision=1,
                proposals_path=self.proposals_path,
                action_id=action_id,
                decision="clear",
                actor="github:radical",
                now=datetime(2026, 9, 3, 16, 6, tzinfo=UTC),
            )

    def test_clear_of_nonexistent_decision_is_rejected(self) -> None:
        # C1: a clear must never silently no-op. There is no effective
        # decision at all for this (digest, actionId) yet.
        with self.assertRaises(CoordinatorStateError):
            self.store.append_exact_decision(
                repository=REPOSITORY,
                expected_revision=0,
                proposals_path=self.proposals_path,
                action_id="snapshot:microsoft/aspire:time:issue:7:retire-status-comment",
                decision="clear",
                actor="github:radical",
                now=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
            )

    def test_clear_is_rejected_when_proposals_document_itself_has_expired(
        self,
    ) -> None:
        # This exercises the shared proposal-activity/expiry gate at the top
        # of append_exact_decision (checked for every decision, not just
        # clear), NOT the in-lock effective-decision-expiry check inside
        # _validate_clear_target_locked. The two can never be told apart
        # through the public API alone: a decision's persisted expiresAtUtc
        # is derived from the exact same generatedAtUtc/proposalTtlHours as
        # the proposals document's own expiry, so for the *same* proposal
        # bytes they always expire at the same instant, and the shared gate
        # above rejects first. See
        # test_clear_is_rejected_when_ledger_decision_has_already_expired for
        # a test that reaches _validate_clear_target_locked's own check, by
        # tampering the ledger's persisted expiresAtUtc directly.
        action_id = "snapshot:microsoft/aspire:time:issue:7:retire-status-comment"
        self.store.append_exact_decision(
            repository=REPOSITORY,
            expected_revision=0,
            proposals_path=self.proposals_path,
            action_id=action_id,
            decision="approve-once",
            actor="github:radical",
            now=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
        )

        with self.assertRaises(CoordinatorStateError) as ctx:
            self.store.append_exact_decision(
                repository=REPOSITORY,
                expected_revision=1,
                proposals_path=self.proposals_path,
                action_id=action_id,
                decision="clear",
                actor="github:radical",
                # Proposal TTL is 24h from 2026-09-03T16:00Z, so this is well
                # past expiry.
                now=datetime(2026, 9, 6, 0, 0, tzinfo=UTC),
            )
        self.assertIn("Proposals document has expired", str(ctx.exception))

        ledger_path = self.state_dir / "coordinator" / "policy-events.jsonl"
        self.assertEqual(
            1, len(ledger_path.read_text(encoding="utf-8").splitlines())
        )

    def test_clear_is_rejected_when_ledger_decision_has_already_expired(
        self,
    ) -> None:
        # Reaches _validate_clear_target_locked's own effective-decision-
        # expiry check (as opposed to the earlier proposal-activity/expiry
        # gate above) by hand-tampering the ledger's persisted decision
        # expiresAtUtc to be in the past while the real proposals document
        # -- used to compute the clear's digest -- is still active. This is
        # the only way to reach that branch: an untampered ledger's decision
        # expiry always equals the proposals document's own expiry for the
        # same proposal bytes.
        action_id = "snapshot:microsoft/aspire:time:issue:7:retire-status-comment"
        self.store.append_exact_decision(
            repository=REPOSITORY,
            expected_revision=0,
            proposals_path=self.proposals_path,
            action_id=action_id,
            decision="approve-once",
            actor="github:radical",
            now=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
        )

        ledger_path = self.state_dir / "coordinator" / "policy-events.jsonl"
        event = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[0])
        event["decision"]["expiresAtUtc"] = "2026-09-03T17:00:00Z"
        with ledger_path.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")

        with self.assertRaises(CoordinatorStateError) as ctx:
            self.store.append_exact_decision(
                repository=REPOSITORY,
                expected_revision=1,
                proposals_path=self.proposals_path,
                action_id=action_id,
                decision="clear",
                actor="github:radical",
                # Between the tampered expiry (17:00) and the proposals
                # document's real, untampered expiry (2026-09-04T16:00Z), so
                # only the ledger-level check below can be firing.
                now=datetime(2026, 9, 3, 18, 0, tzinfo=UTC),
            )
        self.assertIn("already-expired", str(ctx.exception))

    def test_clear_twice_is_rejected_the_second_time(self) -> None:
        action_id = "snapshot:microsoft/aspire:time:issue:7:retire-status-comment"
        self.store.append_exact_decision(
            repository=REPOSITORY,
            expected_revision=0,
            proposals_path=self.proposals_path,
            action_id=action_id,
            decision="approve-once",
            actor="github:radical",
            now=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
        )
        self.store.append_exact_decision(
            repository=REPOSITORY,
            expected_revision=1,
            proposals_path=self.proposals_path,
            action_id=action_id,
            decision="clear",
            actor="github:radical",
            now=datetime(2026, 9, 3, 16, 6, tzinfo=UTC),
        )

        with self.assertRaises(CoordinatorStateError):
            self.store.append_exact_decision(
                repository=REPOSITORY,
                expected_revision=2,
                proposals_path=self.proposals_path,
                action_id=action_id,
                decision="clear",
                actor="github:radical",
                now=datetime(2026, 9, 3, 16, 7, tzinfo=UTC),
            )

    def test_clear_against_regenerated_proposal_bytes_is_rejected_and_old_decision_survives(
        self,
    ) -> None:
        # A clear is bound to the exact raw-byte digest of the proposals
        # document it names. Regenerating an otherwise-identical proposals
        # document (same actionId, different bytes/timestamp) must not be
        # able to clear a decision recorded against the original bytes --
        # that would silently leave the real decision live while reporting
        # success (fail-open).
        action_id = "snapshot:microsoft/aspire:time:issue:7:retire-status-comment"
        self.store.append_exact_decision(
            repository=REPOSITORY,
            expected_revision=0,
            proposals_path=self.proposals_path,
            action_id=action_id,
            decision="approve-once",
            actor="github:radical",
            now=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
        )
        regenerated_path = _write_proposals(
            self.scratch / "regenerated-proposals.json",
            generated_at_utc="2026-09-03T16:01:00Z",
        )
        self.assertNotEqual(
            self.proposals_path.read_bytes(), regenerated_path.read_bytes()
        )

        with self.assertRaises(CoordinatorStateError):
            self.store.append_exact_decision(
                repository=REPOSITORY,
                expected_revision=1,
                proposals_path=regenerated_path,
                action_id=action_id,
                decision="clear",
                actor="github:radical",
                now=datetime(2026, 9, 3, 16, 6, tzinfo=UTC),
            )

        projection = self.store.projection(
            REPOSITORY, now=datetime(2026, 9, 3, 16, 7, tzinfo=UTC)
        )
        self.assertEqual(1, len(projection["exactDecisions"]))
        self.assertEqual("approve-once", projection["exactDecisions"][0]["decision"])

    def test_malformed_jsonl_fails_closed(self) -> None:
        self.store.append_policy_revision(
            repository=REPOSITORY,
            expected_revision=0,
            document=policy_document(revision=1, replaces=None),
        )
        ledger_path = self.state_dir / "coordinator" / "policy-events.jsonl"
        with ledger_path.open("a", encoding="utf-8") as stream:
            stream.write("{not valid json")

        with self.assertRaises(CoordinatorStateError):
            self.store.projection(REPOSITORY)

    def test_truncated_jsonl_fails_closed(self) -> None:
        self.store.append_policy_revision(
            repository=REPOSITORY,
            expected_revision=0,
            document=policy_document(revision=1, replaces=None),
        )
        ledger_path = self.state_dir / "coordinator" / "policy-events.jsonl"
        with ledger_path.open("a", encoding="utf-8") as stream:
            stream.write("\n")

        with self.assertRaises(CoordinatorStateError):
            self.store.projection(REPOSITORY)

    def test_state_directory_may_not_be_a_symlink(self) -> None:
        real_target = self.scratch / "real-state"
        real_target.mkdir()
        symlinked = self.scratch / "symlinked-state"
        symlinked.symlink_to(real_target, target_is_directory=True)

        with self.assertRaises(CoordinatorStateError):
            CoordinatorStateStore(
                symlinked,
                durable_intent_reader=_no_durable_intent,
            )

    def test_ledger_file_may_not_be_a_symlink(self) -> None:
        coordinator_dir = self.state_dir / "coordinator"
        coordinator_dir.mkdir(parents=True)
        decoy_target = self.scratch / "decoy.jsonl"
        decoy_target.write_text("", encoding="utf-8")
        (coordinator_dir / "policy-events.jsonl").symlink_to(decoy_target)

        with self.assertRaises(CoordinatorStateError):
            self.store.append_policy_revision(
                repository=REPOSITORY,
                expected_revision=0,
                document=policy_document(revision=1, replaces=None),
            )

    def test_dangling_ledger_symlink_fails_closed_instead_of_appearing_absent(
        self,
    ) -> None:
        # Path.exists() follows symlinks and reports False for a dangling
        # (target-missing) symlink, while Path.is_symlink() is a pure lstat
        # check that reports True regardless of target existence. If
        # `_load_events` probed `exists()` before `is_symlink()`, a dangling
        # ledger symlink would be silently treated as "no ledger yet" --
        # discarding all prior history -- instead of failing closed.
        coordinator_dir = self.state_dir / "coordinator"
        coordinator_dir.mkdir(parents=True)
        missing_target = self.scratch / "missing-target.jsonl"
        (coordinator_dir / "policy-events.jsonl").symlink_to(missing_target)

        with self.assertRaises(CoordinatorStateError):
            self.store.append_policy_revision(
                repository=REPOSITORY,
                expected_revision=0,
                document=policy_document(revision=1, replaces=None),
            )
        with self.assertRaises(CoordinatorStateError):
            self.store.projection(REPOSITORY)

    def test_lock_file_may_not_be_a_symlink(self) -> None:
        coordinator_dir = self.state_dir / "coordinator"
        coordinator_dir.mkdir(parents=True)
        decoy_target = self.scratch / "decoy.lock"
        decoy_target.write_text("", encoding="utf-8")
        (coordinator_dir / "policy-events.lock").symlink_to(decoy_target)

        with self.assertRaises(CoordinatorStateError):
            self.store.append_policy_revision(
                repository=REPOSITORY,
                expected_revision=0,
                document=policy_document(revision=1, replaces=None),
            )

    def test_proposals_path_may_not_be_a_symlink(self) -> None:
        real_target = self.scratch / "real-proposals.json"
        _write_proposals(real_target)
        symlinked = self.scratch / "symlinked-proposals.json"
        symlinked.symlink_to(real_target)

        with self.assertRaises(CoordinatorStateError):
            self.store.append_exact_decision(
                repository=REPOSITORY,
                expected_revision=0,
                proposals_path=symlinked,
                action_id="snapshot:microsoft/aspire:time:issue:7:retire-status-comment",
                decision="approve-once",
                actor="github:radical",
                now=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
            )

    def test_corrupt_decision_action_id_type_fails_closed(self) -> None:
        self._append_corrupt_decision_field("actionId", 12345)

    def test_corrupt_decision_actor_type_fails_closed(self) -> None:
        self._append_corrupt_decision_field("actor", ["github:radical"])

    def test_corrupt_decision_expires_at_utc_type_fails_closed(self) -> None:
        self._append_corrupt_decision_field("expiresAtUtc", 1234567890)

    def test_corrupt_decision_expires_at_utc_unparseable_fails_closed(self) -> None:
        self._append_corrupt_decision_field("expiresAtUtc", "not-a-timestamp")

    def _append_corrupt_decision_field(self, field: str, value: object) -> None:
        ledger_path = self.state_dir / "coordinator" / "policy-events.jsonl"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        decision = {
            "actionId": "snapshot:microsoft/aspire:time:issue:7:retire-status-comment",
            "proposalDigest": f"sha256:{'0' * 64}",
            "decision": "approve-once",
            "actor": "github:radical",
            "expiresAtUtc": "2026-09-04T16:00:00Z",
        }
        decision[field] = value
        event = {
            "schemaVersion": 1,
            "stateRevision": 1,
            "eventType": "decision",
            "repository": REPOSITORY,
            "recordedAtUtc": "2026-09-03T16:05:00Z",
            "decision": decision,
        }
        with ledger_path.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")

        with self.assertRaises(CoordinatorStateError):
            self.store.projection(REPOSITORY)

    def test_corrupt_policy_revision_id_type_fails_closed(self) -> None:
        ledger_path = self.state_dir / "coordinator" / "policy-events.jsonl"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "schemaVersion": 1,
            "stateRevision": 1,
            "eventType": "policy",
            "repository": REPOSITORY,
            "recordedAtUtc": "2026-09-03T16:00:00Z",
            "policy": {"revisionId": 1, "status": "active", "replacesRevisionId": None},
            "policyDigest": f"sha256:{'0' * 64}",
        }
        with ledger_path.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")

        with self.assertRaises(CoordinatorStateError):
            self.store.projection(REPOSITORY)

    def test_corrupt_repository_type_fails_closed(self) -> None:
        ledger_path = self.state_dir / "coordinator" / "policy-events.jsonl"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "schemaVersion": 1,
            "stateRevision": 1,
            "eventType": "policy",
            "repository": "not-a-valid-repository",
            "recordedAtUtc": "2026-09-03T16:00:00Z",
            "policy": {
                "revisionId": "policy:1",
                "status": "active",
                "replacesRevisionId": None,
            },
            "policyDigest": f"sha256:{'0' * 64}",
        }
        with ledger_path.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")

        with self.assertRaises(CoordinatorStateError):
            self.store.projection(REPOSITORY)

    def test_state_revision_position_mismatch_fails_closed(self) -> None:
        self.store.append_policy_revision(
            repository=REPOSITORY,
            expected_revision=0,
            document=policy_document(revision=1, replaces=None),
        )
        ledger_path = self.state_dir / "coordinator" / "policy-events.jsonl"
        event = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[0])
        event["stateRevision"] = 99  # Corrupt: does not match ledger position 1.
        with ledger_path.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")

        with self.assertRaises(CoordinatorStateError):
            self.store.projection(REPOSITORY)

    def test_real_mid_record_truncation_fails_closed(self) -> None:
        self.store.append_policy_revision(
            repository=REPOSITORY,
            expected_revision=0,
            document=policy_document(revision=1, replaces=None),
        )
        ledger_path = self.state_dir / "coordinator" / "policy-events.jsonl"
        # Simulate a crash mid-write: a second record that is a genuine
        # partial JSON fragment (no closing brace, no trailing newline),
        # rather than the appended-garbage or blank-line cases above.
        with ledger_path.open("a", encoding="utf-8") as stream:
            stream.write(
                '{"schemaVersion": 1, "stateRevision": 2, "eventType": "decis'
            )

        with self.assertRaises(CoordinatorStateError):
            self.store.projection(REPOSITORY)

    def test_ledger_path_being_a_directory_fails_closed(self) -> None:
        # os.open() on a directory succeeds even with O_NOFOLLOW (it is not
        # a symlink), so the failure this must catch happens later, at
        # os.read() time (EISDIR/IsADirectoryError). _read_all must convert
        # that into a CoordinatorStateError rather than leaking a raw OSError.
        ledger_path = self.state_dir / "coordinator" / "policy-events.jsonl"
        ledger_path.mkdir(parents=True)

        with self.assertRaises(CoordinatorStateError):
            self.store.projection(REPOSITORY)

    def test_proposals_path_being_a_directory_fails_closed(self) -> None:
        directory_proposals_path = self.scratch / "proposals-directory"
        directory_proposals_path.mkdir()

        with self.assertRaises(CoordinatorStateError):
            self.store.append_exact_decision(
                repository=REPOSITORY,
                expected_revision=0,
                proposals_path=directory_proposals_path,
                action_id="does-not-matter",
                decision="approve-once",
                actor="github:radical",
                now=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
            )

    def test_zero_proposal_matches_is_rejected(self) -> None:
        with self.assertRaises(CoordinatorStateError):
            self.store.append_exact_decision(
                repository=REPOSITORY,
                expected_revision=0,
                proposals_path=self.proposals_path,
                action_id="does-not-exist-in-the-proposals-document",
                decision="approve-once",
                actor="github:radical",
                now=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
            )

    def test_duplicate_proposal_matches_is_rejected(self) -> None:
        action_id = "snapshot:microsoft/aspire:time:issue:7:retire-status-comment"
        duplicate_path = _write_proposals(
            self.scratch / "duplicate-proposals.json",
            duplicate_action_id=action_id,
        )

        with self.assertRaises(CoordinatorStateError):
            self.store.append_exact_decision(
                repository=REPOSITORY,
                expected_revision=0,
                proposals_path=duplicate_path,
                action_id=action_id,
                decision="approve-once",
                actor="github:radical",
                now=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
            )

    def test_restart_via_new_store_instance_observes_prior_state(self) -> None:
        self.store.append_policy_revision(
            repository=REPOSITORY,
            expected_revision=0,
            document=policy_document(revision=1, replaces=None),
        )

        second_store = CoordinatorStateStore(
            self.state_dir,
            durable_intent_reader=_no_durable_intent,
        )
        projection = second_store.projection(REPOSITORY)

        self.assertEqual(1, projection["stateRevision"])
        self.assertEqual("policy:1", projection["effectivePolicy"]["revisionId"])

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not meaningful on Windows.")
    def test_owner_only_permissions_on_posix(self) -> None:
        self.store.append_policy_revision(
            repository=REPOSITORY,
            expected_revision=0,
            document=policy_document(revision=1, replaces=None),
        )

        coordinator_dir = self.state_dir / "coordinator"
        ledger_path = coordinator_dir / "policy-events.jsonl"
        lock_path = coordinator_dir / "policy-events.lock"

        self.assertEqual(0o700, stat.S_IMODE(self.state_dir.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(coordinator_dir.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(ledger_path.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(lock_path.stat().st_mode))

    def test_decision_recorded_at_utc_is_real_wall_clock_not_caller_now(self) -> None:
        caller_now = datetime(2026, 9, 3, 16, 5, 0, tzinfo=UTC)
        before = datetime.now(UTC)
        self.store.append_exact_decision(
            repository=REPOSITORY,
            expected_revision=0,
            proposals_path=self.proposals_path,
            action_id="snapshot:microsoft/aspire:time:issue:7:retire-status-comment",
            decision="approve-once",
            actor="github:radical",
            now=caller_now,
        )
        after = datetime.now(UTC)

        ledger_path = self.state_dir / "coordinator" / "policy-events.jsonl"
        event = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[0])
        recorded_at = datetime.fromisoformat(event["recordedAtUtc"].replace("Z", "+00:00"))

        self.assertNotEqual(caller_now, recorded_at)
        self.assertLessEqual(before, recorded_at)
        self.assertLessEqual(recorded_at, after)

    def test_policy_recorded_at_utc_is_real_wall_clock(self) -> None:
        before = datetime.now(UTC)
        self.store.append_policy_revision(
            repository=REPOSITORY,
            expected_revision=0,
            document=policy_document(revision=1, replaces=None),
        )
        after = datetime.now(UTC)

        ledger_path = self.state_dir / "coordinator" / "policy-events.jsonl"
        event = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[0])
        recorded_at = datetime.fromisoformat(event["recordedAtUtc"].replace("Z", "+00:00"))

        self.assertLessEqual(before, recorded_at)
        self.assertLessEqual(recorded_at, after)

    def test_malformed_repository_is_rejected_by_append_policy_revision(self) -> None:
        with self.assertRaises(CoordinatorStateError):
            self.store.append_policy_revision(
                repository="not-a-repository",
                expected_revision=0,
                document=policy_document(
                    revision=1, replaces=None, repository="not-a-repository"
                ),
            )

    def test_malformed_repository_is_rejected_by_projection(self) -> None:
        with self.assertRaises(CoordinatorStateError):
            self.store.projection("not-a-repository")

    def test_malformed_actor_is_rejected_by_append_exact_decision(self) -> None:
        with self.assertRaises(CoordinatorStateError):
            self.store.append_exact_decision(
                repository=REPOSITORY,
                expected_revision=0,
                proposals_path=self.proposals_path,
                action_id="snapshot:microsoft/aspire:time:issue:7:retire-status-comment",
                decision="approve-once",
                actor="not-an-actor",
                now=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
            )

    def test_actor_with_embedded_newline_is_rejected(self) -> None:
        with self.assertRaises(CoordinatorStateError):
            self.store.append_exact_decision(
                repository=REPOSITORY,
                expected_revision=0,
                proposals_path=self.proposals_path,
                action_id="snapshot:microsoft/aspire:time:issue:7:retire-status-comment",
                decision="approve-once",
                actor="github:radical\nX-Injected: true",
                now=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
            )

    def test_projection_returns_monotonic_state_revision_and_latest_values(
        self,
    ) -> None:
        self.assertEqual(0, self.store.projection(REPOSITORY)["stateRevision"])

        self.store.append_policy_revision(
            repository=REPOSITORY,
            expected_revision=0,
            document=policy_document(revision=1, replaces=None),
        )
        self.assertEqual(1, self.store.projection(REPOSITORY)["stateRevision"])

        self.store.append_exact_decision(
            repository=REPOSITORY,
            expected_revision=1,
            proposals_path=self.proposals_path,
            action_id="snapshot:microsoft/aspire:time:issue:7:retire-status-comment",
            decision="approve-once",
            actor="github:radical",
            now=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
        )
        projection = self.store.projection(REPOSITORY)
        self.assertEqual(2, projection["stateRevision"])
        self.assertEqual("policy:1", projection["effectivePolicy"]["revisionId"])

    def test_latest_non_expired_decision_wins_and_expired_is_history_only(
        self,
    ) -> None:
        action_id = "snapshot:microsoft/aspire:time:issue:7:retire-status-comment"
        self.store.append_exact_decision(
            repository=REPOSITORY,
            expected_revision=0,
            proposals_path=self.proposals_path,
            action_id=action_id,
            decision="approve-once",
            actor="github:radical",
            now=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
        )
        self.store.append_exact_decision(
            repository=REPOSITORY,
            expected_revision=1,
            proposals_path=self.proposals_path,
            action_id=action_id,
            decision="reject-once",
            actor="github:someone-else",
            now=datetime(2026, 9, 3, 16, 6, tzinfo=UTC),
        )

        not_yet_expired = self.store.projection(
            REPOSITORY, now=datetime(2026, 9, 3, 16, 7, tzinfo=UTC)
        )
        self.assertEqual(1, len(not_yet_expired["exactDecisions"]))
        self.assertEqual("reject-once", not_yet_expired["exactDecisions"][0]["decision"])

        expired = self.store.projection(
            REPOSITORY, now=datetime(2026, 9, 6, 0, 0, tzinfo=UTC)
        )
        self.assertEqual([], expired["exactDecisions"])

        ledger_path = self.state_dir / "coordinator" / "policy-events.jsonl"
        self.assertEqual(
            2, len(ledger_path.read_text(encoding="utf-8").splitlines())
        )

    def test_projected_exact_decision_contains_its_exact_event_revision(
        self,
    ) -> None:
        # Task 3 receives only `policy_projection` and must emit exact-
        # decision license sources as `decision:<eventRevision>`. That
        # identity is impossible to derive from the payload alone (it is a
        # property of the *ledger event* that made the decision effective,
        # not of the decision payload itself), so the projection must
        # enrich each exact decision with the enclosing event's
        # stateRevision under a distinct `eventRevision` key.
        self.store.append_policy_revision(
            repository=REPOSITORY,
            expected_revision=0,
            document=policy_document(revision=1, replaces=None),
        )
        self.store.append_exact_decision(
            repository=REPOSITORY,
            expected_revision=1,
            proposals_path=self.proposals_path,
            action_id="snapshot:microsoft/aspire:time:issue:7:retire-status-comment",
            decision="approve-once",
            actor="github:radical",
            now=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
        )

        projection = self.store.projection(REPOSITORY)

        self.assertEqual(1, len(projection["exactDecisions"]))
        self.assertEqual(2, projection["exactDecisions"][0]["eventRevision"])

    def test_later_replacement_for_same_key_updates_event_revision(self) -> None:
        action_id = "snapshot:microsoft/aspire:time:issue:7:retire-status-comment"
        self.store.append_exact_decision(
            repository=REPOSITORY,
            expected_revision=0,
            proposals_path=self.proposals_path,
            action_id=action_id,
            decision="approve-once",
            actor="github:radical",
            now=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
        )
        first = self.store.projection(REPOSITORY)["exactDecisions"][0]
        self.assertEqual(1, first["eventRevision"])

        self.store.append_exact_decision(
            repository=REPOSITORY,
            expected_revision=1,
            proposals_path=self.proposals_path,
            action_id=action_id,
            decision="reject-once",
            actor="github:someone-else",
            now=datetime(2026, 9, 3, 16, 6, tzinfo=UTC),
        )
        second = self.store.projection(REPOSITORY)["exactDecisions"][0]

        # Same (proposalDigest, actionId) key, but the replacement was
        # recorded as ledger event 2, so its eventRevision must advance to
        # match -- it is not frozen at the key's first appearance.
        self.assertEqual(2, second["eventRevision"])
        self.assertEqual("reject-once", second["decision"])

    def test_unrelated_ledger_event_advances_state_revision_but_not_event_revision(
        self,
    ) -> None:
        action_id = "snapshot:microsoft/aspire:time:issue:7:retire-status-comment"
        self.store.append_exact_decision(
            repository=REPOSITORY,
            expected_revision=0,
            proposals_path=self.proposals_path,
            action_id=action_id,
            decision="approve-once",
            actor="github:radical",
            now=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
        )
        before = self.store.projection(REPOSITORY)
        self.assertEqual(1, before["stateRevision"])
        self.assertEqual(1, before["exactDecisions"][0]["eventRevision"])

        # A global, unrelated policy append (a different repository) still
        # advances the shared ledger-wide stateRevision counter, but must
        # not change this decision's own eventRevision: eventRevision is
        # pinned to the specific ledger event that made *this* key
        # effective, not to "however many events now exist in total".
        other_repository = "microsoft/other"
        self.store.append_policy_revision(
            repository=other_repository,
            expected_revision=1,
            document=policy_document(
                revision=1, replaces=None, repository=other_repository
            ),
        )

        after = self.store.projection(REPOSITORY)
        self.assertEqual(2, after["stateRevision"])
        self.assertEqual(1, after["exactDecisions"][0]["eventRevision"])

    def test_clear_remains_absent_from_effective_projection_event_revision(
        self,
    ) -> None:
        action_id = "snapshot:microsoft/aspire:time:issue:7:retire-status-comment"
        self.store.append_exact_decision(
            repository=REPOSITORY,
            expected_revision=0,
            proposals_path=self.proposals_path,
            action_id=action_id,
            decision="approve-once",
            actor="github:radical",
            now=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
        )
        self.store.append_exact_decision(
            repository=REPOSITORY,
            expected_revision=1,
            proposals_path=self.proposals_path,
            action_id=action_id,
            decision="clear",
            actor="github:radical",
            now=datetime(2026, 9, 3, 16, 6, tzinfo=UTC),
        )

        projection = self.store.projection(REPOSITORY)

        # A clear removes only the *effective* projection entry (and thus
        # its eventRevision); it never deletes ledger history.
        self.assertEqual([], projection["exactDecisions"])
        ledger_path = self.state_dir / "coordinator" / "policy-events.jsonl"
        self.assertEqual(
            2, len(ledger_path.read_text(encoding="utf-8").splitlines())
        )

    def test_other_repositories_do_not_affect_requested_projection(self) -> None:
        other_repository = "microsoft/other"
        other_proposals = _write_proposals(
            self.scratch / "other-proposals.json",
            repository=other_repository,
            action_ids=("snapshot:microsoft/other:time:issue:1:watch-comment",),
        )

        self.store.append_policy_revision(
            repository=other_repository,
            expected_revision=0,
            document=policy_document(
                revision=1, replaces=None, repository=other_repository
            ),
        )
        self.store.append_exact_decision(
            repository=other_repository,
            expected_revision=1,
            proposals_path=other_proposals,
            action_id="snapshot:microsoft/other:time:issue:1:watch-comment",
            decision="approve-once",
            actor="github:radical",
            now=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
        )

        projection = self.store.projection(REPOSITORY)
        self.assertIsNone(projection["effectivePolicy"])
        self.assertEqual([], projection["exactDecisions"])
        self.assertEqual(2, projection["stateRevision"])

    @unittest.skipIf(os.name == "nt", "Test uses fcntl-based POSIX advisory locking directly.")
    def test_race_clear_against_reservation_in_action_then_policy_lock_order(
        self,
    ) -> None:
        # Task 5 will make the real reservation path acquire action-events.lock
        # then policy-events.lock, fsyncing the intent before releasing either.
        # This test proves the prescribed lock order is deadlock-free against a
        # `clear` call using only a barrier/recording lock harness: it never
        # implements Task 5 reservation integration, only the ordering. The
        # store's durable_intent_reader is the real lock-free factory reading a
        # real action-events.jsonl, and the proof that `clear` never contends
        # for the action lock is a deterministic guard on ``os.open`` (not a
        # wall-clock elapsed-time assertion, which would be flaky under load).
        action_events_path = self.state_dir / "action-events.jsonl"
        action_lock_path = self.state_dir / "action-events.lock"
        action_lock_path.parent.mkdir(parents=True, exist_ok=True)
        action_id = "snapshot:microsoft/aspire:time:issue:7:retire-status-comment"
        # A "terminal" event means the lock-free reader reports no durable
        # intent, so the clear below is expected to be permitted.
        with action_events_path.open("w", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "eventType": "terminal",
                        "actionId": action_id,
                        "outcome": "success",
                    }
                )
                + "\n"
            )

        store = CoordinatorStateStore(
            self.state_dir,
            durable_intent_reader=make_lock_free_durable_intent_reader(
                action_events_path
            ),
        )
        store.append_exact_decision(
            repository=REPOSITORY,
            expected_revision=0,
            proposals_path=self.proposals_path,
            action_id=action_id,
            decision="approve-once",
            actor="github:radical",
            now=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
        )

        import fcntl

        action_lock_acquired = threading.Event()
        release_action_lock = threading.Event()
        reservation_finished = threading.Event()

        def _reservation() -> None:
            descriptor = os.open(action_lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                action_lock_acquired.set()
                # Simulate holding the action lock while budget validation
                # would, per the documented order, subsequently take the
                # policy lock -- proving `clear` never needs to wait on us.
                release_action_lock.wait(timeout=5)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
                reservation_finished.set()

        thread = threading.Thread(target=_reservation)
        thread.start()
        self.assertTrue(action_lock_acquired.wait(timeout=5))

        # Deterministic proof (not timing-based): if `clear` -- via the
        # injected durable_intent_reader -- ever attempted to open the action
        # lock file, this guard raises immediately regardless of whether the
        # real open would have blocked.
        real_open = os.open
        guarded_path = os.fspath(action_lock_path)

        def _guarded_open(path: object, *args: object, **kwargs: object) -> int:
            if os.fspath(path) == guarded_path:  # type: ignore[arg-type]
                raise AssertionError(
                    "clear must never attempt to open the action-events lock "
                    "file; durable_intent_reader is required to be lock-free."
                )
            return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

        with mock.patch("os.open", side_effect=_guarded_open):
            store.append_exact_decision(
                repository=REPOSITORY,
                expected_revision=1,
                proposals_path=self.proposals_path,
                action_id=action_id,
                decision="clear",
                actor="github:radical",
                now=datetime(2026, 9, 3, 16, 6, tzinfo=UTC),
            )

        release_action_lock.set()
        thread.join(timeout=5)
        self.assertTrue(reservation_finished.is_set())


class _PausingPolicyBudgetValidator:
    """Wraps a real ``CoordinatorPolicyBudgetValidator``, pausing after the
    inner guard has validated (and is holding the coordinator's policy
    lock) but before letting the caller append and fsync the intent.

    This lets a test prove the lock is genuinely held across that window
    using a deterministic ``threading.Event`` handshake rather than a
    wall-clock race.
    """

    def __init__(
        self,
        inner: CoordinatorPolicyBudgetValidator,
        *,
        entered: threading.Event,
        release: threading.Event,
    ) -> None:
        self._inner = inner
        self._entered = entered
        self._release = release

    @contextmanager
    def reservation_guard(self, **kwargs: object):
        with self._inner.reservation_guard(**kwargs):
            self._entered.set()
            if not self._release.wait(timeout=5):
                raise AssertionError("release event was never set")
            yield


class PolicyBudgetLockOrderTests(unittest.TestCase):
    """Task 5: the real reservation path holds the policy lock, acquired
    after the action lock, through the durable intent append -- so a
    concurrent revocation cannot open a validate-then-revoke gap, and an
    intent that already committed under a since-revoked license is neither
    erased nor re-validated as new.
    """

    def setUp(self) -> None:
        self.scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch.mkdir(parents=True)
        self.state_dir = (self.scratch / "state").resolve()
        self.events_path = self.state_dir / "action-events.jsonl"

    def tearDown(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)

    def _coordinator_store(self, **kwargs: object) -> CoordinatorStateStore:
        return CoordinatorStateStore(
            self.state_dir,
            durable_intent_reader=make_lock_free_durable_intent_reader(
                self.events_path
            ),
            **kwargs,
        )

    def _reserve(self, store: ActionEventStore, grant, *, at: datetime):
        return store.reserve(
            grant,
            action_id="action:1",
            chain_root="action:1",
            operation="edit-comment",
            target_kind="issue",
            target_number=7,
            idempotency_key="action:1:body",
            body_digest=None,
            expected_actor_login="radical",
            at=at,
        )

    @unittest.skipIf(
        os.name == "nt", "Test uses fcntl-based POSIX advisory locking directly."
    )
    def test_reservation_guard_holds_policy_lock_through_append_blocking_concurrent_revocation(
        self,
    ) -> None:
        coordinator_store = self._coordinator_store()
        coordinator_store.append_policy_revision(
            repository=REPOSITORY,
            expected_revision=0,
            document=policy_document(revision=1, replaces=None),
        )

        entered = threading.Event()
        release = threading.Event()
        pausing_validator = _PausingPolicyBudgetValidator(
            CoordinatorPolicyBudgetValidator(coordinator_store),
            entered=entered,
            release=release,
        )
        store = ActionEventStore(
            self.state_dir, policy_budget_validator=pausing_validator
        )
        grant = _autonomous_grant(
            state_dir=self.state_dir,
            action_id="action:1",
            license_source="policy:1",
            repository=REPOSITORY,
        )

        reservation_result: dict[str, object] = {}

        def _reserve_in_background() -> None:
            try:
                reservation = self._reserve(
                    store, grant, at=datetime(2026, 9, 3, 16, 5, tzinfo=UTC)
                )
                reservation_result["mode"] = reservation.mode
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                reservation_result["error"] = exc

        thread = threading.Thread(target=_reserve_in_background)
        thread.start()
        self.assertTrue(entered.wait(timeout=5))

        # While the guard holds the policy lock (validated, not yet
        # appended), a concurrent revocation attempt using a short lock
        # timeout must time out -- proving the lock is genuinely held
        # across that window, not merely documented as being held.
        racing_store = self._coordinator_store(lock_timeout_seconds=0.2)
        with self.assertRaises(CoordinatorStateError) as raised:
            racing_store.append_policy_revision(
                repository=REPOSITORY,
                expected_revision=1,
                document=policy_document(
                    revision=2, replaces="policy:1", status="revoked"
                ),
            )
        self.assertIn(
            "Timed out acquiring the policy-event lock", str(raised.exception)
        )

        release.set()
        thread.join(timeout=5)
        self.assertEqual("execute", reservation_result.get("mode"), reservation_result)

        # Now that the reservation has durably appended (and released the
        # lock), the very same revocation succeeds -- proving it was
        # blocked by the lock, not permanently rejected by some other
        # check.
        coordinator_store.append_policy_revision(
            repository=REPOSITORY,
            expected_revision=1,
            document=policy_document(
                revision=2, replaces="policy:1", status="revoked"
            ),
        )
        projection = coordinator_store.projection(REPOSITORY)
        self.assertEqual("revoked", projection["effectivePolicy"]["status"])

        # The intent that committed under the (now-revoked) license is
        # neither erased nor reopened: replaying the same grant reconciles
        # against it rather than being treated as a new, now-unlicensed
        # reservation attempt.
        ledger_before_replay = self.events_path.read_text(encoding="utf-8")
        replay = self._reserve(
            store, grant, at=datetime(2026, 9, 3, 16, 6, tzinfo=UTC)
        )
        self.assertEqual("reconcile", replay.mode)
        self.assertEqual(
            ledger_before_replay, self.events_path.read_text(encoding="utf-8")
        )

    @unittest.skipIf(
        os.name == "nt", "Test uses fcntl-based POSIX advisory locking directly."
    )
    def test_reservation_guard_holds_policy_lock_through_the_intent_fsync_itself(
        self,
    ) -> None:
        # The previous test proves the lock is held from validation up to
        # the moment ``_append_event`` is invoked. This test proves the
        # stronger, literal requirement: the lock stays held while
        # ``_append_event`` is writing and fsyncing, not merely up to the
        # point the call begins. A regression that exited the guard's
        # context before the write/fsync completed (for example, by moving
        # the append outside the ``with`` block) would let this concurrent
        # revocation attempt succeed instead of timing out.
        coordinator_store = self._coordinator_store()
        coordinator_store.append_policy_revision(
            repository=REPOSITORY,
            expected_revision=0,
            document=policy_document(revision=1, replaces=None),
        )
        store = ActionEventStore(
            self.state_dir,
            policy_budget_validator=CoordinatorPolicyBudgetValidator(
                coordinator_store
            ),
        )
        grant = _autonomous_grant(
            state_dir=self.state_dir,
            action_id="action:1",
            license_source="policy:1",
            repository=REPOSITORY,
        )

        entered_fsync = threading.Event()
        release_fsync = threading.Event()
        paused_once = threading.Event()
        real_fsync = os.fsync

        def _pausing_fsync(descriptor: int) -> None:
            if not paused_once.is_set():
                paused_once.set()
                entered_fsync.set()
                if not release_fsync.wait(timeout=5):
                    raise AssertionError("release_fsync event was never set")
            real_fsync(descriptor)

        reservation_result: dict[str, object] = {}

        def _reserve_in_background() -> None:
            try:
                with mock.patch("os.fsync", side_effect=_pausing_fsync):
                    reservation = self._reserve(
                        store, grant, at=datetime(2026, 9, 3, 16, 5, tzinfo=UTC)
                    )
                reservation_result["mode"] = reservation.mode
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                reservation_result["error"] = exc

        thread = threading.Thread(target=_reserve_in_background)
        thread.start()
        self.assertTrue(entered_fsync.wait(timeout=5))

        racing_store = self._coordinator_store(lock_timeout_seconds=0.2)
        with self.assertRaises(CoordinatorStateError) as raised:
            racing_store.append_policy_revision(
                repository=REPOSITORY,
                expected_revision=1,
                document=policy_document(
                    revision=2, replaces="policy:1", status="revoked"
                ),
            )
        self.assertIn(
            "Timed out acquiring the policy-event lock", str(raised.exception)
        )

        release_fsync.set()
        thread.join(timeout=5)
        self.assertEqual("execute", reservation_result.get("mode"), reservation_result)


class MakeLockFreeDurableIntentReaderTests(unittest.TestCase):
    """Unit tests for the ``make_lock_free_durable_intent_reader`` factory in
    isolation from ``CoordinatorStateStore`` -- these cover reader
    correctness (missing file, open intent, resolved intent, malformed and
    truncated tails); the lock-order/no-contention proof lives in
    ``test_race_clear_against_reservation_in_action_then_policy_lock_order``
    above.
    """

    def setUp(self) -> None:
        self.scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch.mkdir(parents=True)
        self.action_events_path = self.scratch / "action-events.jsonl"

    def _write_lines(self, *lines: dict[str, object]) -> None:
        text = "".join(json.dumps(line) + "\n" for line in lines)
        self.action_events_path.write_text(text, encoding="utf-8")

    def test_missing_file_reports_no_durable_intent(self) -> None:
        reader = make_lock_free_durable_intent_reader(self.action_events_path)

        self.assertFalse(reader("some-action"))

    def test_open_intent_without_terminal_is_durable(self) -> None:
        self._write_lines(
            {"schemaVersion": 1, "eventType": "intent", "actionId": "a1"}
        )
        reader = make_lock_free_durable_intent_reader(self.action_events_path)

        self.assertTrue(reader("a1"))

    def test_terminal_after_intent_is_not_durable(self) -> None:
        self._write_lines(
            {"schemaVersion": 1, "eventType": "intent", "actionId": "a1"},
            {
                "schemaVersion": 1,
                "eventType": "terminal",
                "actionId": "a1",
                "outcome": "success",
            },
        )
        reader = make_lock_free_durable_intent_reader(self.action_events_path)

        self.assertFalse(reader("a1"))

    def test_unrelated_action_ids_do_not_affect_result(self) -> None:
        self._write_lines(
            {"schemaVersion": 1, "eventType": "intent", "actionId": "other-action"},
        )
        reader = make_lock_free_durable_intent_reader(self.action_events_path)

        self.assertFalse(reader("a1"))

    def test_truncated_tail_fails_closed(self) -> None:
        # No trailing newline: simulates a crash mid-write.
        self.action_events_path.write_text(
            '{"schemaVersion": 1, "eventType": "intent", "actionId": "a1"}',
            encoding="utf-8",
        )
        reader = make_lock_free_durable_intent_reader(self.action_events_path)

        self.assertTrue(reader("a1"))

    def test_malformed_line_fails_closed(self) -> None:
        self.action_events_path.write_text("{not valid json}\n", encoding="utf-8")
        reader = make_lock_free_durable_intent_reader(self.action_events_path)

        self.assertTrue(reader("a1"))

    def test_unknown_event_type_raises_typed_error(self) -> None:
        # An unknown eventType must never fail open by returning True: doing
        # so would falsely claim a durable intent exists for the *queried*
        # action_id, even when the unrecognized event belongs to a
        # completely different action. It must instead raise, naming the
        # type and line, so the caller can tell "reader refused to answer"
        # apart from "reader answered: yes, intent exists".
        self._write_lines(
            {"schemaVersion": 1, "eventType": "mystery-event", "actionId": "a1"}
        )
        reader = make_lock_free_durable_intent_reader(self.action_events_path)

        with self.assertRaises(CoordinatorStateError) as ctx:
            reader("a1")
        self.assertIn("mystery-event", str(ctx.exception))
        self.assertIn("line 1", str(ctx.exception))

    def test_unknown_event_type_for_an_unrelated_action_still_raises(self) -> None:
        # Same as above but the unrecognized event does not even mention the
        # queried action_id: this must still raise rather than silently
        # answering False (or True) for a1, because the reader cannot prove
        # anything about the ledger's completeness once it contains a shape
        # it does not recognize.
        self._write_lines(
            {"schemaVersion": 1, "eventType": "mystery-event", "actionId": "other"}
        )
        reader = make_lock_free_durable_intent_reader(self.action_events_path)

        with self.assertRaises(CoordinatorStateError):
            reader("a1")

    def test_delegation_baseline_event_does_not_report_a_durable_intent(
        self,
    ) -> None:
        # delegation-baseline/-retired are known, valid execution_state.py
        # event shapes that are not intent/terminal events; they must pass
        # through without being misclassified as an unknown type (which
        # would raise) or as an open intent (which would report True).
        self._write_lines(
            {"schemaVersion": 1, "eventType": "delegation-baseline", "actionId": "a1"}
        )
        reader = make_lock_free_durable_intent_reader(self.action_events_path)

        self.assertFalse(reader("a1"))

    def test_delegation_retired_event_does_not_report_a_durable_intent(
        self,
    ) -> None:
        self._write_lines(
            {"schemaVersion": 1, "eventType": "delegation-retired", "actionId": "a1"}
        )
        reader = make_lock_free_durable_intent_reader(self.action_events_path)

        self.assertFalse(reader("a1"))

    def test_action_events_path_being_a_directory_fails_closed(self) -> None:
        self.action_events_path.mkdir()
        reader = make_lock_free_durable_intent_reader(self.action_events_path)

        with self.assertRaises(CoordinatorStateError):
            reader("a1")

    def test_symlinked_action_events_path_is_rejected(self) -> None:
        real_target = self.scratch / "real-action-events.jsonl"
        real_target.write_text("", encoding="utf-8")
        self.action_events_path.symlink_to(real_target)
        reader = make_lock_free_durable_intent_reader(self.action_events_path)

        with self.assertRaises(CoordinatorStateError):
            reader("a1")

    def test_dangling_action_events_symlink_fails_closed_instead_of_absent(
        self,
    ) -> None:
        # Same TOCTOU-adjacent ordering hazard as
        # test_dangling_ledger_symlink_fails_closed_instead_of_appearing_absent
        # above: a dangling (target-missing) symlink reports False from
        # Path.exists() but True from Path.is_symlink(). This reader already
        # checks is_symlink() before exists() -- unlike the bug this
        # regression test guards against in _load_events -- so it must keep
        # raising rather than silently reporting "no durable intent" for a
        # ledger that cannot actually be read.
        missing_target = self.scratch / "missing-target.jsonl"
        self.action_events_path.symlink_to(missing_target)
        reader = make_lock_free_durable_intent_reader(self.action_events_path)

        with self.assertRaises(CoordinatorStateError):
            reader("a1")

    @unittest.skipIf(os.name == "nt", "Test asserts on POSIX-style os.open path arguments.")
    def test_reader_never_opens_the_action_lock_file(self) -> None:
        self._write_lines(
            {"schemaVersion": 1, "eventType": "intent", "actionId": "a1"}
        )
        lock_path = self.scratch / "action-events.lock"
        real_open = os.open

        def _guarded_open(path: object, *args: object, **kwargs: object) -> int:
            if os.fspath(path) == os.fspath(lock_path):  # type: ignore[arg-type]
                raise AssertionError("reader must not open the action lock file")
            return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

        reader = make_lock_free_durable_intent_reader(self.action_events_path)
        with mock.patch("os.open", side_effect=_guarded_open):
            self.assertTrue(reader("a1"))


class CoordinatorStateStoreDurableIntentReaderTests(unittest.TestCase):
    def test_reader_exception_fails_closed_and_rejects_clear(self) -> None:
        scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True)
        state_dir = (scratch / "state").resolve()
        proposals_path = scratch / "action-proposals.json"
        _write_proposals(proposals_path)

        def _raising_reader(_action_id: str) -> bool:
            raise RuntimeError("durable intent reader unavailable")

        store = CoordinatorStateStore(
            state_dir,
            durable_intent_reader=_raising_reader,
        )
        action_id = "snapshot:microsoft/aspire:time:issue:7:retire-status-comment"
        store.append_exact_decision(
            repository=REPOSITORY,
            expected_revision=0,
            proposals_path=proposals_path,
            action_id=action_id,
            decision="approve-once",
            actor="github:radical",
            now=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
        )

        with self.assertRaises(CoordinatorStateError):
            store.append_exact_decision(
                repository=REPOSITORY,
                expected_revision=1,
                proposals_path=proposals_path,
                action_id=action_id,
                decision="clear",
                actor="github:radical",
                now=datetime(2026, 9, 3, 16, 6, tzinfo=UTC),
            )


class RegexIdentityDriftTests(unittest.TestCase):
    """Test-only cross-check: coordinator_state.py deliberately duplicates
    operation_policy.py's private repository/actor identity regexes (see the
    module docstring's rationale for duplicating rather than importing
    private names or broadening that module's public surface). This asserts
    the two stay byte-identical so Task 1 changes to those shapes cannot
    silently drift out of sync with this module's own validation. The
    private-name import here is test-only and adds no production coupling.
    """

    def test_repository_regex_matches_operation_policy(self) -> None:
        self.assertEqual(
            operation_policy._REPOSITORY_RE.pattern,
            coordinator_state._REPOSITORY_RE.pattern,
        )

    def test_actor_regex_matches_operation_policy(self) -> None:
        self.assertEqual(
            operation_policy._ACTOR_RE.pattern,
            coordinator_state._ACTOR_RE.pattern,
        )


if __name__ == "__main__":
    unittest.main()
