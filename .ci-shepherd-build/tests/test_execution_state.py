from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from ci_shepherd.authorization import AuthorizationBudget, AuthorizationGrant
from ci_shepherd.execution_state import (
    ActionEventStore,
    ExecutionBudgetError,
    ExecutionStateError,
)


class ActionEventStoreTests(unittest.TestCase):
    def test_legacy_results_are_imported_using_proposal_documents(self) -> None:
        runs_dir = self.state_dir / "runs" / "run-1"
        runs_dir.mkdir(parents=True)
        unusual_action_id = "opaque-action-id-without-parseable-parts"
        (runs_dir / "action-proposals.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "repository": "radical/aspire",
                    "snapshotId": "snapshot:radical/aspire:legacy",
                    "shepherdAuthor": "radical",
                    "proposals": [
                        {
                            "actionId": unusual_action_id,
                            "issueNumber": 1,
                            "issueUrl": (
                                "https://github.com/radical/aspire/issues/1"
                            ),
                            "operation": "create-comment",
                            "idempotencyKey": "issue:1:status",
                            "body": "[automated] Watching.",
                            "evidenceIds": ["issue:1"],
                            "expectedIssueState": "open",
                            "requiresSeparateApproval": True,
                        }
                    ],
                    "unchangedIssueNumbers": [],
                }
            ),
            encoding="utf-8",
        )
        self.state_dir.mkdir(exist_ok=True)
        (self.state_dir / "action-results.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "repository": "radical/aspire",
                    "results": [
                        {
                            "actionId": unusual_action_id,
                            "attemptedAt": "2026-08-28T20:00:00Z",
                            "outcome": "executed",
                            "result": {"commentId": 900},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        self.store.migrate_legacy_results()

        events = [
            json.loads(line)
            for line in (
                self.state_dir / "action-events.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(1, len(events))
        self.assertEqual(unusual_action_id, events[0]["actionId"])
        self.assertEqual(
            "snapshot:radical/aspire:legacy",
            events[0]["snapshotId"],
        )
        self.assertEqual("issue:1:status", events[0]["idempotencyKey"])

    def setUp(self) -> None:
        self.scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch.mkdir(parents=True)
        self.state_dir = (self.scratch / "state").resolve()
        self.grant = AuthorizationGrant(
            grant_id="grant:test",
            repository="radical/aspire",
            state_directory=self.state_dir,
            issued_at=datetime(2026, 8, 29, 20, tzinfo=UTC),
            expires_at=datetime(2026, 8, 29, 20, 15, tzinfo=UTC),
            snapshot_id="snapshot:radical/aspire:2026-08-29T20:00:00Z",
            proposals_digest="sha256:" + ("0" * 64),
            allowed_action_ids=("action:1", "action:2"),
            allowed_operations=frozenset({"create-comment"}),
            allowed_targets=frozenset({("issue", 1), ("issue", 2)}),
            allowed_chain_roots=("action:1", "action:2"),
            override_suppression_for_action_ids=frozenset(),
            budget=AuthorizationBudget(max_mutation_attempts=1, max_chains=1),
        )
        self.store = ActionEventStore(self.state_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_reservation_is_persisted_and_budget_cannot_be_reset(self) -> None:
        first = self.store.reserve(
            self.grant,
            action_id="action:1",
            chain_root="action:1",
            operation="create-comment",
            target_kind="issue",
            target_number=1,
            idempotency_key="issue:1:status",
            body_digest="sha256:" + ("1" * 64),
            expected_actor_login="radical",
            at=datetime(2026, 8, 29, 20, 5, tzinfo=UTC),
        )

        self.assertEqual("execute", first.mode)
        replay = ActionEventStore(self.state_dir).reserve(
            self.grant,
            action_id="action:1",
            chain_root="action:1",
            operation="create-comment",
            target_kind="issue",
            target_number=1,
            idempotency_key="issue:1:status",
            body_digest="sha256:" + ("1" * 64),
            expected_actor_login="radical",
            at=datetime(2026, 8, 29, 20, 6, tzinfo=UTC),
        )
        self.assertEqual("reconcile", replay.mode)

        with self.assertRaisesRegex(ExecutionBudgetError, "mutation-attempt"):
            ActionEventStore(self.state_dir).reserve(
                self.grant,
                action_id="action:2",
                chain_root="action:2",
                operation="create-comment",
                target_kind="issue",
                target_number=2,
                idempotency_key="issue:2:status",
                body_digest="sha256:" + ("2" * 64),
                expected_actor_login="radical",
                at=datetime(2026, 8, 29, 20, 7, tzinfo=UTC),
            )

        event_lines = (
            self.state_dir / "action-events.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(event_lines))
        self.assertEqual("intent", json.loads(event_lines[0])["eventType"])

    def test_indeterminate_event_requires_reconciliation_on_replay(self) -> None:
        self.store.reserve(
            self.grant,
            action_id="action:1",
            chain_root="action:1",
            operation="create-comment",
            target_kind="issue",
            target_number=1,
            idempotency_key="issue:1:status",
            body_digest="sha256:" + ("1" * 64),
            expected_actor_login="radical",
            at=datetime(2026, 8, 29, 20, 5, tzinfo=UTC),
        )
        result = {
            "actionId": "action:1",
            "attemptedAt": "2026-08-29T20:05:01Z",
            "outcome": "indeterminate",
            "reason": "connection lost after request",
        }
        self.store.append_terminal(
            self.grant,
            result=result,
            at=datetime(2026, 8, 29, 20, 5, 1, tzinfo=UTC),
        )

        replay = ActionEventStore(self.state_dir).reserve(
            self.grant,
            action_id="action:1",
            chain_root="action:1",
            operation="create-comment",
            target_kind="issue",
            target_number=1,
            idempotency_key="issue:1:status",
            body_digest="sha256:" + ("1" * 64),
            expected_actor_login="radical",
            at=datetime(2026, 8, 29, 20, 6, tzinfo=UTC),
        )

        self.assertEqual("reconcile", replay.mode)
        self.assertEqual("indeterminate", replay.prior_terminal["outcome"])

    def test_transaction_holds_lock_until_terminal_is_appended(self) -> None:
        with self.store.transaction(
            self.grant,
            action_id="action:1",
            chain_root="action:1",
            operation="create-comment",
            target_kind="issue",
            target_number=1,
            idempotency_key="issue:1:status",
            body_digest="sha256:" + ("1" * 64),
            expected_actor_login="radical",
            at=datetime(2026, 8, 29, 20, 5, tzinfo=UTC),
        ) as execution:
            self.assertEqual("execute", execution.reservation.mode)
            competing_store = ActionEventStore(
                self.state_dir,
                lock_timeout_seconds=0.01,
            )
            with self.assertRaisesRegex(
                Exception,
                "Timed out acquiring",
            ):
                competing_store.reserve(
                    self.grant,
                    action_id="action:1",
                    chain_root="action:1",
                    operation="create-comment",
                    target_kind="issue",
                    target_number=1,
                    idempotency_key="issue:1:status",
                    body_digest="sha256:" + ("1" * 64),
                    expected_actor_login="radical",
                    at=datetime(2026, 8, 29, 20, 5, 1, tzinfo=UTC),
                )
            execution.append_terminal(
                result={
                    "actionId": "action:1",
                    "attemptedAt": "2026-08-29T20:05:02Z",
                    "outcome": "executed",
                },
                at=datetime(2026, 8, 29, 20, 5, 2, tzinfo=UTC),
            )

    def test_first_event_append_fsyncs_the_state_directory(self) -> None:
        with patch(
            "ci_shepherd.execution_state._fsync_directory"
        ) as fsync_directory:
            self.store.reserve(
                self.grant,
                action_id="action:1",
                chain_root="action:1",
                operation="create-comment",
                target_kind="issue",
                target_number=1,
                idempotency_key="issue:1:status",
                body_digest="sha256:" + ("1" * 64),
                expected_actor_login="radical",
                at=datetime(2026, 8, 29, 20, 5, tzinfo=UTC),
            )

        fsync_directory.assert_called_once_with(self.state_dir)

    def test_terminal_projection_preserves_stable_idempotency_identity(self) -> None:
        self.store.reserve(
            self.grant,
            action_id="action:1",
            chain_root="action:1",
            operation="create-comment",
            target_kind="issue",
            target_number=1,
            idempotency_key="issue:1:status",
            body_digest="sha256:" + ("1" * 64),
            expected_actor_login="radical",
            at=datetime(2026, 8, 29, 20, 5, tzinfo=UTC),
        )
        self.store.append_terminal(
            self.grant,
            result={
                "actionId": "action:1",
                "attemptedAt": "2026-08-29T20:05:01Z",
                "outcome": "executed",
            },
            at=datetime(2026, 8, 29, 20, 5, 1, tzinfo=UTC),
        )

        result = self.store.prior_results(repository="radical/aspire")["results"][0]

        self.assertEqual("issue:1:status", result["idempotencyKey"])
        self.assertEqual({"kind": "issue", "number": 1}, result["target"])

    def test_reconciliation_can_supersede_an_indeterminate_terminal(self) -> None:
        self.store.reserve(
            self.grant,
            action_id="action:1",
            chain_root="action:1",
            operation="create-comment",
            target_kind="issue",
            target_number=1,
            idempotency_key="issue:1:status",
            body_digest="sha256:" + ("1" * 64),
            expected_actor_login="radical",
            at=datetime(2026, 8, 29, 20, 5, tzinfo=UTC),
        )
        self.store.append_terminal(
            self.grant,
            result={
                "actionId": "action:1",
                "attemptedAt": "2026-08-29T20:05:01Z",
                "outcome": "indeterminate",
                "reason": "mutation-not-confirmed",
            },
            at=datetime(2026, 8, 29, 20, 5, 1, tzinfo=UTC),
        )

        self.store.append_terminal(
            self.grant,
            result={
                "actionId": "action:1",
                "attemptedAt": "2026-08-29T20:06:00Z",
                "outcome": "executed",
                "result": {"commentId": 900},
            },
            at=datetime(2026, 8, 29, 20, 6, tzinfo=UTC),
        )

        replay = self.store.reserve(
            self.grant,
            action_id="action:1",
            chain_root="action:1",
            operation="create-comment",
            target_kind="issue",
            target_number=1,
            idempotency_key="issue:1:status",
            body_digest="sha256:" + ("1" * 64),
            expected_actor_login="radical",
            at=datetime(2026, 8, 29, 20, 7, tzinfo=UTC),
        )
        projected = self.store.prior_results(
            repository="radical/aspire"
        )["results"]

        self.assertEqual("terminal", replay.mode)
        self.assertEqual("executed", replay.prior_terminal["outcome"])
        self.assertEqual(["executed"], [result["outcome"] for result in projected])

    def test_confirmed_terminal_cannot_be_replaced_by_a_different_outcome(self) -> None:
        self.store.reserve(
            self.grant,
            action_id="action:1",
            chain_root="action:1",
            operation="create-comment",
            target_kind="issue",
            target_number=1,
            idempotency_key="issue:1:status",
            body_digest="sha256:" + ("1" * 64),
            expected_actor_login="radical",
            at=datetime(2026, 8, 29, 20, 5, tzinfo=UTC),
        )
        self.store.append_terminal(
            self.grant,
            result={
                "actionId": "action:1",
                "attemptedAt": "2026-08-29T20:05:01Z",
                "outcome": "executed",
                "result": {"commentId": 900},
            },
            at=datetime(2026, 8, 29, 20, 5, 1, tzinfo=UTC),
        )

        with self.assertRaisesRegex(
            ExecutionStateError,
            "different terminal event",
        ):
            self.store.append_terminal(
                self.grant,
                result={
                    "actionId": "action:1",
                    "attemptedAt": "2026-08-29T20:06:00Z",
                    "outcome": "failed",
                    "reason": "late failure",
                },
                at=datetime(2026, 8, 29, 20, 6, tzinfo=UTC),
            )


if __name__ == "__main__":
    unittest.main()
