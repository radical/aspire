from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import shutil
import threading
import unittest
from unittest.mock import patch

from ci_shepherd.authorization import (
    AuthorizationBudget,
    AuthorizationGrant,
    AutonomousPolicyLicense,
)
from ci_shepherd.coordinator_state import (
    CoordinatorStateStore,
    make_lock_free_durable_intent_reader,
)
from ci_shepherd.execution_state import (
    ActionEventStore,
    ExecutionBudgetError,
    ExecutionStateError,
)
from ci_shepherd.models import stable_json
from ci_shepherd.operation_policy import (
    DEFAULT_CAPS,
    DEFAULT_EXPIRY_DAYS,
    HARD_MAX_PER_RUN,
    HARD_MAX_ROLLING_24H,
    OPERATION_CLASSES,
)
from ci_shepherd.policy_budget import CoordinatorPolicyBudgetValidator




def _coordinator_store(state_dir: Path) -> CoordinatorStateStore:
    return CoordinatorStateStore(
        state_dir,
        durable_intent_reader=make_lock_free_durable_intent_reader(
            state_dir / "action-events.jsonl"
        ),
    )


def _validated_store(
    state_dir: Path, coordinator_store: CoordinatorStateStore | None = None
) -> ActionEventStore:
    store = coordinator_store or _coordinator_store(state_dir)
    return ActionEventStore(
        state_dir,
        policy_budget_validator=CoordinatorPolicyBudgetValidator(store),
    )


def _policy_document(
    *,
    repository: str = "microsoft/aspire",
    revision: int = 1,
    replaces: str | None = None,
    status: str = "active",
    enabled_class: str = "edit-comment",
    max_per_run: int = 10,
    max_rolling_24h: int = 30,
    denied_action_ids: tuple[str, ...] = (),
    denied_targets: tuple[str, ...] = (),
    created_at_utc: datetime = datetime(2026, 9, 3, 16, 0, tzinfo=UTC),
    expires_at_utc: datetime | None = None,
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
        "actor": "github:radical",
        "replacesRevisionId": replaces,
        "operationClasses": {
            name: {
                "enabled": name == enabled_class,
                "maxPerRun": (
                    max_per_run if name == enabled_class else DEFAULT_CAPS[name]["maxPerRun"]
                ),
                "maxRolling24h": (
                    max_rolling_24h
                    if name == enabled_class
                    else DEFAULT_CAPS[name]["maxRolling24h"]
                ),
            }
            for name in OPERATION_CLASSES
        },
        "deniedActionIds": list(denied_action_ids),
        "deniedTargets": list(denied_targets),
    }


def _proposals_document(
    *,
    repository: str = "microsoft/aspire",
    action_ids: tuple[str, ...] = ("action:1",),
    generated_at_utc: str = "2026-09-03T16:00:00Z",
    proposal_ttl_hours: int = 24,
) -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "repository": repository,
        "snapshotId": "snapshot:microsoft/aspire:2026-09-03T16:00:00Z",
        "shepherdAuthor": "radical",
        "generatedAtUtc": generated_at_utc,
        "proposalTtlHours": proposal_ttl_hours,
        "proposals": [
            {"actionId": action_id, "issueNumber": 7, "operation": "edit-comment"}
            for action_id in action_ids
        ],
    }


def _write_proposals(path: Path, **kwargs: object) -> Path:
    path.write_text(json.dumps(_proposals_document(**kwargs)), encoding="utf-8")
    return path


def _proposals_digest(path: Path) -> str:
    # Matches CoordinatorStateStore.append_exact_decision, which digests the
    # raw file bytes rather than any re-serialization of their content.
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _autonomous_grant(
    *,
    state_dir: Path,
    action_id: str,
    license_source: str,
    repository: str = "microsoft/aspire",
    snapshot_id: str = "snapshot:microsoft/aspire:2026-09-03T16:00:00Z",
    operation: str = "edit-comment",
    target: tuple[str, int] = ("issue", 7),
    operation_class: str = "edit-comment",
    satisfied_prerequisites: tuple[tuple[str, str], ...] = (),
    proposals_digest: str = "sha256:" + ("0" * 64),
    selection_state_revision: int = 1,
    run_id: str = "run-1",
) -> AuthorizationGrant:
    license_ = AutonomousPolicyLicense(
        run_id=run_id,
        operation_class=operation_class,
        selection_digest="sha256:" + ("a" * 64),
        selection_state_revision=selection_state_revision,
        license_source=license_source,
        satisfied_prerequisites=satisfied_prerequisites,
    )
    return AuthorizationGrant(
        grant_id=f"grant:{action_id}",
        repository=repository,
        state_directory=state_dir,
        issued_at=datetime(2026, 9, 3, 16, 0, tzinfo=UTC),
        expires_at=datetime(2026, 9, 3, 16, 15, tzinfo=UTC),
        snapshot_id=snapshot_id,
        proposals_digest=proposals_digest,
        allowed_action_ids=(action_id,),
        allowed_operations=frozenset({operation}),
        allowed_targets=frozenset({target}),
        allowed_chain_roots=(action_id,),
        override_suppression_for_action_ids=frozenset(),
        budget=AuthorizationBudget(max_mutation_attempts=1, max_chains=1),
        production_comment_pilot=False,
        autonomous_policy=True,
        autonomous_policy_license=license_,
        policy_selection_digest=license_.selection_digest,
    )


def _legacy_grant(
    *,
    state_dir: Path,
    action_id: str,
    repository: str = "microsoft/aspire",
    snapshot_id: str = "snapshot:microsoft/aspire:2026-09-03T16:00:00Z",
    operation: str = "edit-comment",
    target: tuple[str, int] = ("issue", 7),
) -> AuthorizationGrant:
    return AuthorizationGrant(
        grant_id=f"grant:{action_id}",
        repository=repository,
        state_directory=state_dir,
        issued_at=datetime(2026, 9, 3, 16, 0, tzinfo=UTC),
        expires_at=datetime(2026, 9, 3, 16, 15, tzinfo=UTC),
        snapshot_id=snapshot_id,
        proposals_digest="sha256:" + ("0" * 64),
        allowed_action_ids=(action_id,),
        allowed_operations=frozenset({operation}),
        allowed_targets=frozenset({target}),
        allowed_chain_roots=(action_id,),
        override_suppression_for_action_ids=frozenset(),
        budget=AuthorizationBudget(max_mutation_attempts=1, max_chains=1),
        production_comment_pilot=True,
    )


def _seed_raw_event(
    events_path: Path,
    *,
    repository: str,
    snapshot_id: str,
    action_id: str,
    recorded_at: datetime,
    event_type: str = "intent",
    operation_class: str | None = None,
    outcome: str | None = None,
    run_id: str | None = None,
) -> None:
    # Writes the minimal raw ledger shape directly (bypassing
    # ActionEventStore) so hard-ceiling tests can cheaply seed the O(100)
    # prior intents/terminals they need without O(cap) real reservations.
    # The validator's usage aggregation only reads these fields.
    events_path.parent.mkdir(parents=True, exist_ok=True)
    event: dict[str, object] = {
        "schemaVersion": 1,
        "eventType": event_type,
        "recordedAt": recorded_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "repository": repository,
        "snapshotId": snapshot_id,
        "actionId": action_id,
    }
    if outcome is not None:
        event["outcome"] = outcome
    if operation_class is not None:
        event["operationClass"] = operation_class
    if run_id is not None:
        event["runId"] = run_id
    with events_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n")


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
            production_comment_pilot=False,
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

    def test_transaction_records_task_inventory_before_delegation_write(
        self,
    ) -> None:
        recorded_at = datetime(2026, 8, 29, 20, 5, 1, tzinfo=UTC)

        with self.store.transaction(
            self.grant,
            action_id="action:1",
            chain_root="action:1",
            operation="assign-copilot",
            target_kind="issue",
            target_number=1,
            idempotency_key="issue:1:copilot-assignment",
            body_digest=None,
            expected_actor_login="radical",
            at=datetime(2026, 8, 29, 20, 5, tzinfo=UTC),
        ) as execution:
            event = execution.append_delegation_baseline(
                task_ids=("task-existing",),
                at=recorded_at,
            )
            self.assertEqual(
                ("task-existing",),
                execution.delegation_baseline_task_ids(),
            )

        self.assertEqual("delegation-baseline", event["eventType"])
        self.assertEqual(["task-existing"], event["taskIdsBefore"])
        self.assertEqual(
            ["intent", "delegation-baseline"],
            [
                item["eventType"]
                for item in self.store.events(repository="radical/aspire")
            ],
        )

    def test_delegation_retirement_is_persisted_once(self) -> None:
        at = datetime(2026, 8, 29, 20, 10, tzinfo=UTC)

        self.store.append_delegation_retirements(
            repository="radical/aspire",
            task_ids=("task-1",),
            at=at,
        )
        self.store.append_delegation_retirements(
            repository="radical/aspire",
            task_ids=("task-1",),
            at=at,
        )

        events = self.store.events(repository="radical/aspire")
        self.assertEqual(1, len(events))
        self.assertEqual("delegation-retired", events[0]["eventType"])
        self.assertEqual("task-1", events[0]["taskId"])

    def test_delegation_observation_survives_retirement_without_duplicate_history(self) -> None:
        at = datetime(2026, 8, 29, 20, 5, tzinfo=UTC)
        with self.store.transaction(
            self.grant, action_id="action:1", chain_root="action:1",
            operation="assign-copilot", target_kind="issue", target_number=1,
            idempotency_key="issue:1:copilot-assignment", body_digest=None,
            expected_actor_login="radical", at=at,
        ) as execution:
            execution.append_delegation_baseline(task_ids=(), at=at)
            execution.append_terminal(
                result={"actionId": "action:1", "outcome": "executed",
                        "result": {"taskId": "task-1"}},
                at=at,
            )
        record = {
            "actionId": "action:1", "repository": "radical/aspire", "issueNumber": 1,
            "startedAt": "2026-08-29T20:05:00Z", "taskId": "task-1",
            "taskState": "failed", "lifecycle": "completed", "requiresHuman": False,
            "attemptOutcome": "merged", "requiresNewDecision": True,
            "pullRequests": [{"databaseId": 101, "globalId": "PR_101", "number": 201,
                              "state": "merged", "isDraft": False, "changedFiles": 4}],
        }
        self.store.append_delegation_observations(
            repository="radical/aspire", records=[record], at=at,
        )
        self.store.append_delegation_retirements(
            repository="radical/aspire", task_ids=("task-1",), at=at,
        )
        reloaded = ActionEventStore(self.state_dir)
        reloaded.append_delegation_observations(
            repository="radical/aspire", records=[record], at=at + timedelta(minutes=1),
        )
        observations = [
            event for event in reloaded.events(repository="radical/aspire")
            if event["eventType"] == "delegation-observed"
        ]
        self.assertEqual([record], [event["record"] for event in observations])
        with self.assertRaisesRegex(ExecutionStateError, "task identity"):
            reloaded.append_delegation_observations(
                repository="radical/aspire",
                records=[{**record, "taskId": "unrelated-task"}], at=at,
            )

    def test_assignment_intent_without_baseline_retries_capacity_reservation(
        self,
    ) -> None:
        arguments = {
            "action_id": "action:1",
            "chain_root": "action:1",
            "operation": "assign-copilot",
            "target_kind": "issue",
            "target_number": 1,
            "idempotency_key": "issue:1:copilot-assignment",
            "body_digest": None,
            "expected_actor_login": "radical",
        }
        first = self.store.reserve(
            self.grant,
            **arguments,
            at=datetime(2026, 8, 29, 20, 5, tzinfo=UTC),
        )
        retry = self.store.reserve(
            self.grant,
            **arguments,
            at=datetime(2026, 8, 29, 20, 6, tzinfo=UTC),
        )

        self.assertEqual("execute", first.mode)
        self.assertEqual("execute", retry.mode)

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


class PolicyBudgetValidatorTests(unittest.TestCase):
    """Task 5: budget consumption is atomic with intent append.

    Every test here wires ``ActionEventStore`` to a real
    ``CoordinatorPolicyBudgetValidator`` backed by a real
    ``CoordinatorStateStore`` -- never a mock -- so these prove the actual
    lock-order, atomicity, and revalidation contract end-to-end.
    """

    def setUp(self) -> None:
        self.scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch.mkdir(parents=True)
        self.state_dir = (self.scratch / "state").resolve()
        self.repository = "microsoft/aspire"
        self.snapshot_id = "snapshot:microsoft/aspire:2026-09-03T16:00:00Z"
        self.events_path = self.state_dir / "action-events.jsonl"
        self.proposals_path = self.scratch / "action-proposals.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)

    def _grant(self, action_id: str, *, license_source: str, **kwargs: object):
        # Most tests share one snapshot; F2's run-identity tests pass an
        # explicit override to prove two runIds sharing (or not sharing)
        # a snapshot are scoped correctly.
        snapshot_id = kwargs.pop("snapshot_id", self.snapshot_id)
        return _autonomous_grant(
            state_dir=self.state_dir,
            action_id=action_id,
            license_source=license_source,
            repository=self.repository,
            snapshot_id=snapshot_id,
            **kwargs,
        )

    def _legacy(self, action_id: str, **kwargs: object):
        return _legacy_grant(
            state_dir=self.state_dir,
            action_id=action_id,
            repository=self.repository,
            snapshot_id=self.snapshot_id,
            **kwargs,
        )

    def _reserve(
        self,
        store: ActionEventStore,
        grant,
        action_id: str,
        *,
        at: datetime,
        operation: str = "edit-comment",
        target_number: int = 7,
        idempotency_key: str | None = None,
    ):
        return store.reserve(
            grant,
            action_id=action_id,
            chain_root=action_id,
            operation=operation,
            target_kind="issue",
            target_number=target_number,
            idempotency_key=idempotency_key or f"{action_id}:body",
            body_digest=None,
            expected_actor_login="radical",
            at=at,
        )

    def _activate_policy(
        self, coordinator_store: CoordinatorStateStore, *, expected_revision: int = 0, **kwargs: object
    ) -> dict[str, object]:
        return coordinator_store.append_policy_revision(
            repository=self.repository,
            expected_revision=expected_revision,
            document=_policy_document(repository=self.repository, **kwargs),
        )

    def _write_and_digest_proposals(self, *, action_ids: tuple[str, ...]) -> str:
        _write_proposals(
            self.proposals_path, repository=self.repository, action_ids=action_ids
        )
        return _proposals_digest(self.proposals_path)

    def _read_ledger(self) -> list[dict[str, object]]:
        if not self.events_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.events_path.read_text(encoding="utf-8").splitlines()
        ]

    def _seed_hard_ceiling(
        self,
        *,
        count: int,
        snapshot_id: str | None = None,
        recorded_at: datetime | None = None,
        run_id: str | None = None,
    ) -> None:
        snapshot_id = snapshot_id or self.snapshot_id
        recorded_at = recorded_at or datetime(2026, 9, 3, 15, 0, tzinfo=UTC)
        for index in range(count):
            _seed_raw_event(
                self.events_path,
                repository=self.repository,
                snapshot_id=snapshot_id,
                action_id=f"seed:{index}",
                recorded_at=recorded_at,
                event_type="intent",
                run_id=run_id,
            )

    def test_two_threads_racing_for_last_class_slot_append_exactly_one_intent(
        self,
    ) -> None:
        coordinator_store = _coordinator_store(self.state_dir)
        self._activate_policy(coordinator_store, max_per_run=1, max_rolling_24h=5)

        barrier = threading.Barrier(2)
        results: dict[str, object] = {}
        results_lock = threading.Lock()

        def _attempt(action_id: str, at: datetime) -> None:
            store = _validated_store(self.state_dir)
            barrier.wait(timeout=5)
            try:
                reservation = self._reserve(
                    store,
                    self._grant(action_id, license_source="policy:1"),
                    action_id,
                    at=at,
                )
                outcome: object = reservation.mode
            except ExecutionBudgetError as exc:
                outcome = exc
            with results_lock:
                results[action_id] = outcome

        threads = [
            threading.Thread(
                target=_attempt,
                args=("action:1", datetime(2026, 9, 3, 16, 5, tzinfo=UTC)),
            ),
            threading.Thread(
                target=_attempt,
                args=("action:2", datetime(2026, 9, 3, 16, 5, 1, tzinfo=UTC)),
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        successes = [
            action_id for action_id, value in results.items() if value == "execute"
        ]
        failures = [
            action_id
            for action_id, value in results.items()
            if isinstance(value, ExecutionBudgetError)
        ]
        self.assertEqual(1, len(successes), results)
        self.assertEqual(1, len(failures), results)

        ledger = self._read_ledger()
        intents = [event for event in ledger if event["eventType"] == "intent"]
        self.assertEqual(1, len(intents))
        self.assertEqual(successes[0], intents[0]["actionId"])
        self.assertEqual("edit-comment", intents[0]["operationClass"])

    def test_class_per_run_cap_is_scoped_by_run_id_not_snapshot(self) -> None:
        # policy_selection.py's own per-run accounting buckets usage by
        # ``event["runId"] == run_id`` (see the ``this_run_ids`` grouping in
        # its class-capacity computation), not by snapshotId. Two runs that
        # share the exact same repository snapshot -- e.g. a rerun of the
        # same scan -- each mint their own AutonomousPolicyLicense with
        # their own runId, so each must receive its own, independent
        # standing per-class per-run slot. Scoping the guard's counter by
        # snapshotId instead would incorrectly starve the second run.
        coordinator_store = _coordinator_store(self.state_dir)
        self._activate_policy(coordinator_store, max_per_run=1, max_rolling_24h=5)
        store = _validated_store(self.state_dir, coordinator_store)

        at = datetime(2026, 9, 3, 16, 5, tzinfo=UTC)
        r0_first = self._reserve(
            store,
            self._grant("action:r0-1", license_source="policy:1", run_id="r0"),
            "action:r0-1",
            at=at,
        )
        self.assertEqual("execute", r0_first.mode)

        # r0's single per-run slot for this class is now exhausted.
        with self.assertRaises(ExecutionBudgetError):
            self._reserve(
                store,
                self._grant("action:r0-2", license_source="policy:1", run_id="r0"),
                "action:r0-2",
                at=at,
            )

        # r1 shares the exact same snapshot as r0 but is a distinct run, and
        # must receive its own per-class per-run slot rather than being
        # rejected because r0 already used the snapshot's "slot".
        r1_first = self._reserve(
            store,
            self._grant("action:r1-1", license_source="policy:1", run_id="r1"),
            "action:r1-1",
            at=at,
        )
        self.assertEqual("execute", r1_first.mode)

        ledger = self._read_ledger()
        intents = [event for event in ledger if event["eventType"] == "intent"]
        self.assertEqual(2, len(intents))
        self.assertEqual({"r0", "r1"}, {intent["runId"] for intent in intents})
        self.assertEqual(
            {"action:r0-1", "action:r1-1"},
            {intent["actionId"] for intent in intents},
        )

        # r0 remains exhausted even after r1's independent slot was granted.
        with self.assertRaises(ExecutionBudgetError):
            self._reserve(
                store,
                self._grant("action:r0-3", license_source="policy:1", run_id="r0"),
                "action:r0-3",
                at=at,
            )

    def test_restart_reconstructs_usage_from_durable_intents(self) -> None:
        coordinator_store = _coordinator_store(self.state_dir)
        self._activate_policy(coordinator_store, max_per_run=1, max_rolling_24h=5)

        first_store = _validated_store(self.state_dir, coordinator_store)
        first = self._reserve(
            first_store,
            self._grant("action:1", license_source="policy:1"),
            "action:1",
            at=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
        )
        self.assertEqual("execute", first.mode)

        # Simulate a restart: brand-new ActionEventStore and
        # CoordinatorStateStore instances that only ever read durable state.
        second_store = _validated_store(self.state_dir)
        with self.assertRaises(ExecutionBudgetError):
            self._reserve(
                second_store,
                self._grant("action:2", license_source="policy:1"),
                "action:2",
                at=datetime(2026, 9, 3, 16, 6, tzinfo=UTC),
            )

    def test_fsynced_intent_consumes_capacity_before_any_terminal(self) -> None:
        coordinator_store = _coordinator_store(self.state_dir)
        self._activate_policy(coordinator_store, max_per_run=1, max_rolling_24h=5)
        store = _validated_store(self.state_dir, coordinator_store)

        first = self._reserve(
            store,
            self._grant("action:1", license_source="policy:1"),
            "action:1",
            at=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
        )
        self.assertEqual("execute", first.mode)

        with self.assertRaises(ExecutionBudgetError):
            self._reserve(
                store,
                self._grant("action:2", license_source="policy:1"),
                "action:2",
                at=datetime(2026, 9, 3, 16, 5, 1, tzinfo=UTC),
            )

    def test_failed_and_stale_terminal_events_still_consume_class_cap(self) -> None:
        coordinator_store = _coordinator_store(self.state_dir)
        self._activate_policy(coordinator_store, max_per_run=2, max_rolling_24h=5)
        store = _validated_store(self.state_dir, coordinator_store)

        first_grant = self._grant("action:1", license_source="policy:1")
        self._reserve(
            store, first_grant, "action:1", at=datetime(2026, 9, 3, 16, 5, tzinfo=UTC)
        )
        store.append_terminal(
            first_grant,
            result={
                "actionId": "action:1",
                "attemptedAt": "2026-09-03T16:05:05Z",
                "outcome": "failed",
                "reason": "boom",
            },
            at=datetime(2026, 9, 3, 16, 5, 5, tzinfo=UTC),
        )

        second_grant = self._grant("action:2", license_source="policy:1")
        self._reserve(
            store,
            second_grant,
            "action:2",
            at=datetime(2026, 9, 3, 16, 5, 10, tzinfo=UTC),
        )
        store.append_terminal(
            second_grant,
            result={
                "actionId": "action:2",
                "attemptedAt": "2026-09-03T16:05:15Z",
                "outcome": "stale",
                "reason": "superseded",
            },
            at=datetime(2026, 9, 3, 16, 5, 15, tzinfo=UTC),
        )

        with self.assertRaises(ExecutionBudgetError):
            self._reserve(
                store,
                self._grant("action:3", license_source="policy:1"),
                "action:3",
                at=datetime(2026, 9, 3, 16, 5, 20, tzinfo=UTC),
            )

    def test_guard_rejection_before_append_consumes_nothing(self) -> None:
        # No active standing policy exists at all, so licenseSource
        # "policy:1" cannot be validated: the guard must reject before any
        # append, leaving no ledger file behind.
        store = _validated_store(self.state_dir)

        with self.assertRaises(ExecutionBudgetError):
            self._reserve(
                store,
                self._grant("action:1", license_source="policy:1"),
                "action:1",
                at=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
            )

        self.assertFalse(self.events_path.exists())

    def test_exact_approval_bypasses_class_cap_but_still_increments_usage(
        self,
    ) -> None:
        coordinator_store = _coordinator_store(self.state_dir)
        self._activate_policy(coordinator_store, max_per_run=1, max_rolling_24h=5)
        store = _validated_store(self.state_dir, coordinator_store)

        first = self._reserve(
            store,
            self._grant("action:1", license_source="policy:1"),
            "action:1",
            at=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
        )
        self.assertEqual("execute", first.mode)

        proposals_digest = self._write_and_digest_proposals(action_ids=("action:2",))
        decision = coordinator_store.append_exact_decision(
            repository=self.repository,
            expected_revision=1,
            proposals_path=self.proposals_path,
            action_id="action:2",
            decision="approve-once",
            actor="github:radical",
            now=datetime(2026, 9, 3, 16, 5, 30, tzinfo=UTC),
        )

        second = self._reserve(
            store,
            self._grant(
                "action:2",
                license_source=f"decision:{decision['stateRevision']}",
                proposals_digest=proposals_digest,
            ),
            "action:2",
            at=datetime(2026, 9, 3, 16, 6, tzinfo=UTC),
        )
        self.assertEqual("execute", second.mode)

        # Exact approval bypassed the exhausted class cap, but the intent it
        # licensed is still counted: a third, ordinarily policy-licensed
        # attempt now sees two units of usage against a cap of one and is
        # correctly rejected.
        with self.assertRaises(ExecutionBudgetError):
            self._reserve(
                store,
                self._grant("action:3", license_source="policy:1"),
                "action:3",
                at=datetime(2026, 9, 3, 16, 7, tzinfo=UTC),
            )

    def test_hard_ceiling_per_run_blocks_both_policy_and_exact_approval(
        self,
    ) -> None:
        coordinator_store = _coordinator_store(self.state_dir)
        self._activate_policy(coordinator_store, max_per_run=10, max_rolling_24h=30)
        self._seed_hard_ceiling(
            count=HARD_MAX_PER_RUN,
            recorded_at=datetime(2026, 9, 3, 15, 59, tzinfo=UTC),
        )
        store = _validated_store(self.state_dir, coordinator_store)

        with self.subTest("policy-licensed"):
            with self.assertRaises(ExecutionBudgetError):
                self._reserve(
                    store,
                    self._grant("action:policy", license_source="policy:1"),
                    "action:policy",
                    at=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
                )

        proposals_digest = self._write_and_digest_proposals(
            action_ids=("action:exact",)
        )
        decision = coordinator_store.append_exact_decision(
            repository=self.repository,
            expected_revision=1,
            proposals_path=self.proposals_path,
            action_id="action:exact",
            decision="approve-once",
            actor="github:radical",
            now=datetime(2026, 9, 3, 16, 5, 30, tzinfo=UTC),
        )
        with self.subTest("exact-approval"):
            with self.assertRaises(ExecutionBudgetError):
                self._reserve(
                    store,
                    self._grant(
                        "action:exact",
                        license_source=f"decision:{decision['stateRevision']}",
                        proposals_digest=proposals_digest,
                    ),
                    "action:exact",
                    at=datetime(2026, 9, 3, 16, 6, tzinfo=UTC),
                )

        # Neither rejected attempt appended anything new to the ledger.
        ledger = self._read_ledger()
        self.assertEqual(HARD_MAX_PER_RUN, len(ledger))

    def test_hard_ceiling_rolling_24h_blocks_new_intent(self) -> None:
        coordinator_store = _coordinator_store(self.state_dir)
        self._activate_policy(coordinator_store, max_per_run=10, max_rolling_24h=30)
        at = datetime(2026, 9, 3, 16, 5, tzinfo=UTC)
        for index in range(HARD_MAX_ROLLING_24H):
            _seed_raw_event(
                self.events_path,
                repository=self.repository,
                snapshot_id=f"snapshot:microsoft/aspire:seed-{index}",
                action_id=f"seed:{index}",
                recorded_at=at - timedelta(hours=1),
            )
        store = _validated_store(self.state_dir, coordinator_store)

        with self.assertRaises(ExecutionBudgetError):
            self._reserve(
                store,
                self._grant("action:new", license_source="policy:1"),
                "action:new",
                at=at,
            )

    def test_revoked_policy_cannot_license_a_new_intent(self) -> None:
        coordinator_store = _coordinator_store(self.state_dir)
        self._activate_policy(coordinator_store, max_per_run=10, max_rolling_24h=30)
        coordinator_store.append_policy_revision(
            repository=self.repository,
            expected_revision=1,
            document=_policy_document(
                repository=self.repository,
                revision=2,
                replaces="policy:1",
                status="revoked",
            ),
        )
        store = _validated_store(self.state_dir, coordinator_store)

        with self.assertRaises(ExecutionBudgetError):
            self._reserve(
                store,
                self._grant("action:1", license_source="policy:1"),
                "action:1",
                at=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
            )
        self.assertFalse(self.events_path.exists())

    def test_expired_policy_cannot_license_a_new_intent(self) -> None:
        coordinator_store = _coordinator_store(self.state_dir)
        self._activate_policy(
            coordinator_store,
            max_per_run=10,
            max_rolling_24h=30,
            expires_at_utc=datetime(2026, 9, 3, 16, 1, tzinfo=UTC),
        )
        store = _validated_store(self.state_dir, coordinator_store)

        with self.assertRaises(ExecutionBudgetError):
            self._reserve(
                store,
                self._grant("action:1", license_source="policy:1"),
                "action:1",
                at=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
            )

    def test_replaced_policy_cannot_license_a_new_intent(self) -> None:
        coordinator_store = _coordinator_store(self.state_dir)
        self._activate_policy(coordinator_store, max_per_run=10, max_rolling_24h=30)
        coordinator_store.append_policy_revision(
            repository=self.repository,
            expected_revision=1,
            document=_policy_document(
                repository=self.repository,
                revision=2,
                replaces="policy:1",
                status="active",
            ),
        )
        store = _validated_store(self.state_dir, coordinator_store)

        with self.assertRaises(ExecutionBudgetError):
            self._reserve(
                store,
                self._grant("action:1", license_source="policy:1"),
                "action:1",
                at=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
            )

    def test_cleared_exact_decision_cannot_license_a_new_intent(self) -> None:
        coordinator_store = _coordinator_store(self.state_dir)
        proposals_digest = self._write_and_digest_proposals(action_ids=("action:1",))
        approval = coordinator_store.append_exact_decision(
            repository=self.repository,
            expected_revision=0,
            proposals_path=self.proposals_path,
            action_id="action:1",
            decision="approve-once",
            actor="github:radical",
            now=datetime(2026, 9, 3, 16, 0, tzinfo=UTC),
        )
        coordinator_store.append_exact_decision(
            repository=self.repository,
            expected_revision=1,
            proposals_path=self.proposals_path,
            action_id="action:1",
            decision="clear",
            actor="github:radical",
            now=datetime(2026, 9, 3, 16, 1, tzinfo=UTC),
        )
        store = _validated_store(self.state_dir, coordinator_store)

        with self.assertRaises(ExecutionBudgetError):
            self._reserve(
                store,
                self._grant(
                    "action:1",
                    license_source=f"decision:{approval['stateRevision']}",
                    proposals_digest=proposals_digest,
                ),
                "action:1",
                at=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
            )

    def test_existing_intent_reconciles_after_policy_revocation(self) -> None:
        coordinator_store = _coordinator_store(self.state_dir)
        self._activate_policy(coordinator_store, max_per_run=10, max_rolling_24h=30)
        store = _validated_store(self.state_dir, coordinator_store)
        grant = self._grant("action:1", license_source="policy:1")
        first = self._reserve(
            store, grant, "action:1", at=datetime(2026, 9, 3, 16, 5, tzinfo=UTC)
        )
        self.assertEqual("execute", first.mode)

        coordinator_store.append_policy_revision(
            repository=self.repository,
            expected_revision=1,
            document=_policy_document(
                repository=self.repository,
                revision=2,
                replaces="policy:1",
                status="revoked",
            ),
        )

        replay = self._reserve(
            _validated_store(self.state_dir, coordinator_store),
            grant,
            "action:1",
            at=datetime(2026, 9, 3, 16, 6, tzinfo=UTC),
        )
        self.assertEqual("reconcile", replay.mode)

        ledger = self._read_ledger()
        intents = [event for event in ledger if event["eventType"] == "intent"]
        self.assertEqual(1, len(intents))
        self.assertEqual("action:1", intents[0]["actionId"])

    def test_legacy_pilot_intent_consumes_repository_hard_ceiling(self) -> None:
        self._seed_hard_ceiling(count=HARD_MAX_PER_RUN)
        store = _validated_store(self.state_dir)

        with self.assertRaises(ExecutionBudgetError):
            self._reserve(
                store,
                self._legacy("action:legacy"),
                "action:legacy",
                at=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
            )

    def test_legacy_pilot_intent_does_not_consume_standing_class_cap(self) -> None:
        coordinator_store = _coordinator_store(self.state_dir)
        self._activate_policy(coordinator_store, max_per_run=1, max_rolling_24h=5)
        store = _validated_store(self.state_dir, coordinator_store)

        legacy = self._reserve(
            store,
            self._legacy("action:legacy"),
            "action:legacy",
            at=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
        )
        self.assertEqual("execute", legacy.mode)

        autonomous = self._reserve(
            store,
            self._grant("action:autonomous", license_source="policy:1"),
            "action:autonomous",
            at=datetime(2026, 9, 3, 16, 6, tzinfo=UTC),
        )
        self.assertEqual("execute", autonomous.mode)

    def test_replay_of_same_action_id_does_not_double_count_class_usage(
        self,
    ) -> None:
        coordinator_store = _coordinator_store(self.state_dir)
        self._activate_policy(coordinator_store, max_per_run=2, max_rolling_24h=5)
        store = _validated_store(self.state_dir, coordinator_store)
        grant_one = self._grant("action:1", license_source="policy:1")

        first = self._reserve(
            store, grant_one, "action:1", at=datetime(2026, 9, 3, 16, 5, tzinfo=UTC)
        )
        self.assertEqual("execute", first.mode)

        replay = self._reserve(
            store,
            grant_one,
            "action:1",
            at=datetime(2026, 9, 3, 16, 5, 30, tzinfo=UTC),
        )
        self.assertEqual("reconcile", replay.mode)

        second = self._reserve(
            store,
            self._grant("action:2", license_source="policy:1"),
            "action:2",
            at=datetime(2026, 9, 3, 16, 6, tzinfo=UTC),
        )
        self.assertEqual("execute", second.mode)

    def test_rolling_window_excludes_events_older_than_24h_from_recorded_at(
        self,
    ) -> None:
        coordinator_store = _coordinator_store(self.state_dir)
        self._activate_policy(coordinator_store, max_per_run=10, max_rolling_24h=1)
        store = _validated_store(self.state_dir, coordinator_store)
        at = datetime(2026, 9, 3, 16, 5, tzinfo=UTC)
        _seed_raw_event(
            self.events_path,
            repository=self.repository,
            snapshot_id="snapshot:microsoft/aspire:prior-run",
            action_id="seed:old",
            recorded_at=at - timedelta(hours=25),
            operation_class="edit-comment",
        )

        reservation = self._reserve(
            store,
            self._grant("action:new", license_source="policy:1"),
            "action:new",
            at=at,
        )
        self.assertEqual("execute", reservation.mode)

    def test_rolling_window_includes_events_within_24h_of_recorded_at(
        self,
    ) -> None:
        coordinator_store = _coordinator_store(self.state_dir)
        self._activate_policy(coordinator_store, max_per_run=10, max_rolling_24h=1)
        store = _validated_store(self.state_dir, coordinator_store)
        at = datetime(2026, 9, 3, 16, 5, tzinfo=UTC)
        _seed_raw_event(
            self.events_path,
            repository=self.repository,
            snapshot_id="snapshot:microsoft/aspire:prior-run",
            action_id="seed:recent",
            recorded_at=at - timedelta(hours=1),
            operation_class="edit-comment",
        )

        with self.assertRaises(ExecutionBudgetError):
            self._reserve(
                store,
                self._grant("action:new", license_source="policy:1"),
                "action:new",
                at=at,
            )

    def test_rolling_window_class_cap_ignores_later_terminal_recorded_at(
        self,
    ) -> None:
        # F1: an action's rolling-window occurrence time must be its own
        # intent.recordedAt, not whichever of {intent, terminal} happens to
        # be most recent. A terminal recorded 24h later than its own intent
        # (e.g. a slow retry/reconciliation) must not "refresh" that action
        # back into the window and wrongly consume the next reservation's
        # standing per-class rolling slot.
        coordinator_store = _coordinator_store(self.state_dir)
        self._activate_policy(coordinator_store, max_per_run=10, max_rolling_24h=1)
        at = datetime(2026, 9, 3, 16, 5, tzinfo=UTC)
        _seed_raw_event(
            self.events_path,
            repository=self.repository,
            snapshot_id="snapshot:microsoft/aspire:prior-run",
            action_id="seed:old",
            recorded_at=at - timedelta(hours=25),
            event_type="intent",
            operation_class="edit-comment",
        )
        _seed_raw_event(
            self.events_path,
            repository=self.repository,
            snapshot_id="snapshot:microsoft/aspire:prior-run",
            action_id="seed:old",
            recorded_at=at - timedelta(hours=1),
            event_type="terminal",
            operation_class="edit-comment",
            outcome="failed",
        )
        store = _validated_store(self.state_dir, coordinator_store)

        reservation = self._reserve(
            store,
            self._grant("action:new", license_source="policy:1"),
            "action:new",
            at=at,
        )
        self.assertEqual("execute", reservation.mode)

    def test_rolling_window_repository_hard_ceiling_ignores_later_terminal_recorded_at(
        self,
    ) -> None:
        # F1, repository-hard-ceiling variant of the same bug: pad with
        # HARD_MAX_ROLLING_24H - 1 unambiguous recent actions, then add one
        # more action whose intent is 25h old but whose terminal is 1h old.
        # Under the union-of-{intent,terminal} bug this 300th action wrongly
        # counts as "recent" and exhausts the ceiling; the fix must exclude
        # it because its own intent.recordedAt falls outside the window.
        at = datetime(2026, 9, 3, 16, 5, tzinfo=UTC)
        for index in range(HARD_MAX_ROLLING_24H - 1):
            _seed_raw_event(
                self.events_path,
                repository=self.repository,
                snapshot_id=f"snapshot:microsoft/aspire:seed-{index}",
                action_id=f"seed:{index}",
                recorded_at=at - timedelta(hours=1),
            )
        _seed_raw_event(
            self.events_path,
            repository=self.repository,
            snapshot_id="snapshot:microsoft/aspire:prior-run",
            action_id="seed:paired",
            recorded_at=at - timedelta(hours=25),
            event_type="intent",
        )
        _seed_raw_event(
            self.events_path,
            repository=self.repository,
            snapshot_id="snapshot:microsoft/aspire:prior-run",
            action_id="seed:paired",
            recorded_at=at - timedelta(hours=1),
            event_type="terminal",
            outcome="failed",
        )
        store = _validated_store(self.state_dir)

        reservation = self._reserve(
            store,
            self._legacy("action:new"),
            "action:new",
            at=at,
        )
        self.assertEqual("execute", reservation.mode)

    def test_rolling_window_terminal_only_legacy_import_consumes_repository_hard_ceiling(
        self,
    ) -> None:
        # Regression/documentation companion to the two tests above: an
        # action that only ever has a terminal event (a legacy import with
        # no recorded intent) must still fall back to that terminal's own
        # recordedAt for rolling-window purposes -- it is not simply
        # excluded because it lacks an intent.
        at = datetime(2026, 9, 3, 16, 5, tzinfo=UTC)
        for index in range(HARD_MAX_ROLLING_24H - 1):
            _seed_raw_event(
                self.events_path,
                repository=self.repository,
                snapshot_id=f"snapshot:microsoft/aspire:seed-{index}",
                action_id=f"seed:{index}",
                recorded_at=at - timedelta(hours=1),
            )
        _seed_raw_event(
            self.events_path,
            repository=self.repository,
            snapshot_id="snapshot:microsoft/aspire:legacy-import",
            action_id="seed:legacy-terminal-only",
            recorded_at=at - timedelta(hours=1),
            event_type="terminal",
            outcome="failed",
        )
        store = _validated_store(self.state_dir)

        with self.assertRaises(ExecutionBudgetError):
            self._reserve(
                store,
                self._legacy("action:new"),
                "action:new",
                at=at,
            )

    def test_repository_hard_ceiling_same_run_id_persists_across_snapshot_change(
        self,
    ) -> None:
        # F2: the repository-wide "this run" hard ceiling must be scoped by
        # the autonomous intent's own runId, not by snapshotId. A rescan
        # that mints a new snapshotId for the same run must not reopen the
        # run's exhausted hard-ceiling slot. A standing policy is activated
        # (as the sibling hard-ceiling tests do) so the rejection under
        # test is unambiguously the hard ceiling, not an absent policy.
        coordinator_store = _coordinator_store(self.state_dir)
        self._activate_policy(coordinator_store, max_per_run=50, max_rolling_24h=90)
        self._seed_hard_ceiling(
            count=HARD_MAX_PER_RUN,
            run_id="r0",
            recorded_at=datetime(2026, 9, 3, 15, 59, tzinfo=UTC),
        )
        store = _validated_store(self.state_dir, coordinator_store)
        rescanned_grant = self._grant(
            "action:r0-rescan",
            license_source="policy:1",
            snapshot_id="snapshot:microsoft/aspire:2026-09-03T16:10:00Z",
            run_id="r0",
        )

        with self.assertRaises(ExecutionBudgetError):
            self._reserve(
                store,
                rescanned_grant,
                "action:r0-rescan",
                at=datetime(2026, 9, 3, 16, 15, tzinfo=UTC),
            )
        self.assertEqual(HARD_MAX_PER_RUN, len(self._read_ledger()))

    def test_repository_hard_ceiling_different_run_id_resets_on_shared_snapshot(
        self,
    ) -> None:
        # F2, converse case: a distinct runId that happens to share the
        # exact same snapshotId as an exhausted run must receive its own,
        # independent repository hard-ceiling slot.
        coordinator_store = _coordinator_store(self.state_dir)
        self._activate_policy(coordinator_store, max_per_run=50, max_rolling_24h=90)
        self._seed_hard_ceiling(
            count=HARD_MAX_PER_RUN,
            run_id="r0",
            recorded_at=datetime(2026, 9, 3, 15, 59, tzinfo=UTC),
        )
        store = _validated_store(self.state_dir, coordinator_store)
        r1_grant = self._grant(
            "action:r1-first",
            license_source="policy:1",
            snapshot_id=self.snapshot_id,
            run_id="r1",
        )

        reservation = self._reserve(
            store,
            r1_grant,
            "action:r1-first",
            at=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
        )
        self.assertEqual("execute", reservation.mode)

    def test_repository_hard_ceiling_new_legacy_intent_counts_prior_autonomous_use(
        self,
    ) -> None:
        # Critical: a NEW legacy/production-pilot intent (no runId of its
        # own) must count every prior record on its exact snapshot toward
        # the repository-wide hard ceiling, autonomous or legacy alike. The
        # buggy predicate only counted prior records that *also* had no
        # runId, so HARD_MAX_PER_RUN autonomous intents on this snapshot
        # left the ceiling looking untouched to a legacy reservation and
        # admitted a 101st action.
        self._seed_hard_ceiling(
            count=HARD_MAX_PER_RUN,
            run_id="r0",
            recorded_at=datetime(2026, 9, 3, 15, 59, tzinfo=UTC),
        )
        store = _validated_store(self.state_dir)

        with self.assertRaises(ExecutionBudgetError):
            self._reserve(
                store,
                self._legacy("action:legacy-101st"),
                "action:legacy-101st",
                at=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
            )
        self.assertEqual(HARD_MAX_PER_RUN, len(self._read_ledger()))

    def test_300_malformed_recorded_at_values_fail_reservation_closed(self) -> None:
        # F3: unusable recordedAt values must raise a typed failure before
        # reservation, not silently be treated as "not within the rolling
        # window" (which would fail OPEN by undercounting usage). Every
        # seeded event here has a distinct snapshotId so none of them touch
        # the (unrelated) per-run hard ceiling -- only the malformed-time
        # handling itself can make this reservation succeed or fail.
        at = datetime(2026, 9, 3, 16, 5, tzinfo=UTC)
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for index in range(300):
            event = {
                "schemaVersion": 1,
                "eventType": "intent",
                "recordedAt": f"not-a-real-timestamp-{index}",
                "repository": self.repository,
                "snapshotId": f"snapshot:microsoft/aspire:seed-{index}",
                "actionId": f"seed:{index}",
            }
            lines.append(json.dumps(event, sort_keys=True))
        self.events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        store = _validated_store(self.state_dir)

        with self.assertRaises(ExecutionStateError) as raised:
            self._reserve(store, self._legacy("action:new"), "action:new", at=at)
        self.assertIs(ExecutionStateError, type(raised.exception))
        self.assertEqual(
            300, len(self.events_path.read_text(encoding="utf-8").splitlines())
        )

    def test_missing_or_invalid_action_ids_fail_reservation_closed(self) -> None:
        # F3: a missing/None/non-string actionId must not silently collapse
        # into one falsy `None` bucket entry (which would undercount how
        # many distinct actions are actually present); it must raise a
        # typed failure before reservation instead.
        at = datetime(2026, 9, 3, 16, 5, tzinfo=UTC)
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        base = {
            "schemaVersion": 1,
            "eventType": "intent",
            "recordedAt": at.isoformat().replace("+00:00", "Z"),
            "repository": self.repository,
        }
        events = [
            {**base, "snapshotId": "snapshot:microsoft/aspire:seed-0"},
            {**base, "snapshotId": "snapshot:microsoft/aspire:seed-1", "actionId": None},
            {**base, "snapshotId": "snapshot:microsoft/aspire:seed-2", "actionId": 42},
            {**base, "snapshotId": "snapshot:microsoft/aspire:seed-3", "actionId": ""},
        ]
        lines = [json.dumps(event, sort_keys=True) for event in events]
        self.events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        store = _validated_store(self.state_dir)

        with self.assertRaises(ExecutionStateError) as raised:
            self._reserve(store, self._legacy("action:new"), "action:new", at=at)
        self.assertIs(ExecutionStateError, type(raised.exception))
        self.assertEqual(
            4, len(self.events_path.read_text(encoding="utf-8").splitlines())
        )

    def test_dependent_close_intent_revalidates_matching_prerequisite_digest(
        self,
    ) -> None:
        coordinator_store = _coordinator_store(self.state_dir)
        self._activate_policy(
            coordinator_store,
            enabled_class="close-issue",
            max_per_run=10,
            max_rolling_24h=30,
        )
        store = _validated_store(self.state_dir, coordinator_store)

        dep_grant = self._grant(
            "action:dep",
            license_source="policy:1",
            operation_class="close-issue",
            operation="close-issue",
        )
        first = self._reserve(
            store, dep_grant, "action:dep", at=datetime(2026, 9, 3, 16, 5, tzinfo=UTC)
        )
        self.assertEqual("execute", first.mode)
        store.append_terminal(
            dep_grant,
            result={
                "actionId": "action:dep",
                "attemptedAt": "2026-09-03T16:05:05Z",
                "outcome": "executed",
                "result": {"closed": True},
            },
            at=datetime(2026, 9, 3, 16, 5, 5, tzinfo=UTC),
        )
        dep_terminal = next(
            event
            for event in self._read_ledger()
            if event["eventType"] == "terminal" and event["actionId"] == "action:dep"
        )
        expected_digest = "sha256:" + hashlib.sha256(
            stable_json(dep_terminal).encode("utf-8")
        ).hexdigest()

        close_grant = self._grant(
            "action:close",
            license_source="policy:1",
            operation_class="close-issue",
            operation="close-issue",
            satisfied_prerequisites=(("action:dep", expected_digest),),
        )
        second = self._reserve(
            store,
            close_grant,
            "action:close",
            at=datetime(2026, 9, 3, 16, 6, tzinfo=UTC),
        )
        self.assertEqual("execute", second.mode)

    def test_dependent_close_intent_rejects_missing_prerequisite_terminal(
        self,
    ) -> None:
        coordinator_store = _coordinator_store(self.state_dir)
        self._activate_policy(
            coordinator_store,
            enabled_class="close-issue",
            max_per_run=10,
            max_rolling_24h=30,
        )
        store = _validated_store(self.state_dir, coordinator_store)
        stale_digest = "sha256:" + ("f" * 64)

        close_grant = self._grant(
            "action:close",
            license_source="policy:1",
            operation_class="close-issue",
            operation="close-issue",
            satisfied_prerequisites=(("action:dep", stale_digest),),
        )
        with self.assertRaises(ExecutionBudgetError):
            self._reserve(
                store,
                close_grant,
                "action:close",
                at=datetime(2026, 9, 3, 16, 6, tzinfo=UTC),
            )

    def test_dependent_close_intent_rejects_changed_prerequisite_terminal(
        self,
    ) -> None:
        coordinator_store = _coordinator_store(self.state_dir)
        self._activate_policy(
            coordinator_store,
            enabled_class="close-issue",
            max_per_run=10,
            max_rolling_24h=30,
        )
        store = _validated_store(self.state_dir, coordinator_store)
        dep_grant = self._grant(
            "action:dep",
            license_source="policy:1",
            operation_class="close-issue",
            operation="close-issue",
        )
        self._reserve(
            store, dep_grant, "action:dep", at=datetime(2026, 9, 3, 16, 5, tzinfo=UTC)
        )
        store.append_terminal(
            dep_grant,
            result={
                "actionId": "action:dep",
                "attemptedAt": "2026-09-03T16:05:05Z",
                "outcome": "executed",
                "result": {"closed": True},
            },
            at=datetime(2026, 9, 3, 16, 5, 5, tzinfo=UTC),
        )
        dep_terminal = next(
            event
            for event in self._read_ledger()
            if event["eventType"] == "terminal" and event["actionId"] == "action:dep"
        )
        original_digest = "sha256:" + hashlib.sha256(
            stable_json(dep_terminal).encode("utf-8")
        ).hexdigest()

        # The store itself refuses a second, differing terminal for the same
        # actionId, but the ledger format on disk does not forbid it (e.g. a
        # future reconciliation writer, or corruption); a restart must not
        # trust a cached digest over the durable tail, so append another raw
        # terminal line directly to prove the guard re-reads it.
        _seed_raw_event(
            self.events_path,
            repository=self.repository,
            snapshot_id=self.snapshot_id,
            action_id="action:dep",
            recorded_at=datetime(2026, 9, 3, 16, 5, 6, tzinfo=UTC),
            event_type="terminal",
            outcome="failed",
        )

        close_grant = self._grant(
            "action:close",
            license_source="policy:1",
            operation_class="close-issue",
            operation="close-issue",
            satisfied_prerequisites=(("action:dep", original_digest),),
        )
        with self.assertRaises(ExecutionBudgetError):
            self._reserve(
                store,
                close_grant,
                "action:close",
                at=datetime(2026, 9, 3, 16, 6, tzinfo=UTC),
            )


if __name__ == "__main__":
    unittest.main()
