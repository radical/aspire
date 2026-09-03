from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
import hashlib
import json
import os
import shutil
import threading
import time
import unittest

from ci_shepherd.coordinator_state import CoordinatorStateError, CoordinatorStateStore
from ci_shepherd.operation_policy import DEFAULT_CAPS, DEFAULT_EXPIRY_DAYS, OPERATION_CLASSES


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

    def test_race_clear_against_reservation_in_action_then_policy_lock_order(
        self,
    ) -> None:
        # Task 5 will make the real reservation path acquire action-events.lock
        # then policy-events.lock, fsyncing the intent before releasing either.
        # This test proves the prescribed lock order is deadlock-free against a
        # `clear` call using only a barrier/recording lock harness: it never
        # implements Task 5 reservation integration, only the ordering.
        action_lock_path = self.state_dir / "action-events.lock"
        action_lock_path.parent.mkdir(parents=True, exist_ok=True)
        policy_lock_path = self.state_dir / "coordinator" / "policy-events.lock"
        policy_lock_path.parent.mkdir(parents=True, exist_ok=True)

        self.store.append_exact_decision(
            repository=REPOSITORY,
            expected_revision=0,
            proposals_path=self.proposals_path,
            action_id="snapshot:microsoft/aspire:time:issue:7:retire-status-comment",
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
                policy_descriptor = os.open(
                    policy_lock_path, os.O_RDWR | os.O_CREAT, 0o600
                )
                try:
                    fcntl.flock(policy_descriptor, fcntl.LOCK_EX)
                    fcntl.flock(policy_descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(policy_descriptor)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
                reservation_finished.set()

        thread = threading.Thread(target=_reservation)
        thread.start()
        self.assertTrue(action_lock_acquired.wait(timeout=5))

        # `clear` must not attempt to acquire the action lock, so it must
        # complete promptly even while the reservation thread holds it.
        started = time.monotonic()
        self.store.append_exact_decision(
            repository=REPOSITORY,
            expected_revision=1,
            proposals_path=self.proposals_path,
            action_id="snapshot:microsoft/aspire:time:issue:7:retire-status-comment",
            decision="clear",
            actor="github:radical",
            now=datetime(2026, 9, 3, 16, 6, tzinfo=UTC),
        )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 4.0)

        release_action_lock.set()
        thread.join(timeout=5)
        self.assertTrue(reservation_finished.is_set())


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


if __name__ == "__main__":
    unittest.main()
