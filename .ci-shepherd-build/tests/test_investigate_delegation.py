from __future__ import annotations

import copy
import unittest
from datetime import UTC, datetime

from ci_shepherd.actions import build_action_proposals
from ci_shepherd.actor import validate_action_proposals
from ci_shepherd.lifecycle import prepare_assessment
from ci_shepherd.poc import (
    build_compact_poc_input,
    validate_poc_judgments,
    validate_poc_projectability,
)
from tests.test_production_decisions import assess, handoff_snapshot, quarantined_snapshot, recovery_snapshot
from tests.test_policy import ASPIRE_REPOSITORY_POLICY_PATH
from ci_shepherd.repository_policy import load_repository_policy
from ci_shepherd.policy_selection import build_policy_selection
from ci_shepherd.managed_coverage import build_managed_item_coverage, coverage_exclusions
from tests.test_policy_selection import _digest_of, _exact_decision, _policy_document, _projection


class InvestigateDelegationTests(unittest.TestCase):
    def test_snapshot_nominations_are_bounded_unique_positive_issue_numbers(self) -> None:
        from ci_shepherd.models import validate_snapshot

        value = recovery_snapshot()
        for requests in ([True], [0], [-1], ["21"], [21, 21], list(range(1, 7)), None):
            with self.subTest(requests=requests):
                value["delegationRequests"] = requests
                with self.assertRaisesRegex(ValueError, "delegationRequests"):
                    validate_snapshot(value)

    def _propose(
        self, *, diagnostic_error: bool = False, labels: tuple[str, ...] = (),
        assignees: tuple[str, ...] = (),
    ) -> tuple[dict, dict, dict]:
        value = recovery_snapshot(success="skipped")
        policy = load_repository_policy(ASPIRE_REPOSITORY_POLICY_PATH)
        value["repositoryPolicy"] = {**policy.as_public_dict(), "digest": policy.digest}
        value["delegationRequests"] = [21]
        value["evidence"]["issue:21"]["payload"]["labels"] = list(labels)
        value["evidence"]["issue:21"]["payload"]["assignees"] = list(assignees)
        if diagnostic_error:
            value["collectionErrors"] = [{
                "stage": "workflow-log", "endpoint": "/repos/owner/repo/actions/jobs/900/logs",
                "message": "Log expired", "scope": {"kind": "issue", "issueNumbers": [21]},
            }]
        prepared = prepare_assessment(value)
        compact = build_compact_poc_input(prepared)
        judgments = {
            "schemaVersion": 1,
            "snapshotId": prepared["snapshotId"],
            "issues": [copy.deepcopy(compact["issues"][0]["defaultJudgment"])],
        }
        validate_poc_judgments(prepared, judgments)
        validate_poc_projectability(compact, judgments)
        proposals = build_action_proposals(
            value, prepared, judgments, "operator", agent_input=compact,
        )
        validate_action_proposals(proposals)
        return value, prepared, proposals

    def test_explicit_issue_without_diagnosis_proposes_investigate_and_fix(self) -> None:
        _, _, proposals = self._propose()
        assignment, = proposals["proposals"]
        self.assertEqual("assign-copilot", assignment["operation"])
        self.assertEqual("operator-request", assignment["evidenceBasis"])
        self.assertTrue(assignment["executionEligibility"]["eligible"])
        self.assertIn("Investigate and fix", assignment["customInstructions"])
        self.assertIn("Do not remove quarantine or skip attributes", assignment["customInstructions"])

    def test_current_assignee_blocks_readiness_before_live_execution(self) -> None:
        for assignee in ("human-owner", "copilot-swe-agent[bot]"):
            with self.subTest(assignee=assignee):
                _, prepared, proposals = self._propose(assignees=(assignee,))
                issue = prepared["issues"][0]
                evidence = next(item for item in issue["evidenceBundle"] if item["id"] == "issue:21")
                self.assertEqual([assignee], evidence["payload"]["assignees"])
                self.assertIsNone(build_compact_poc_input(prepared)["issues"][0].get("delegationReadiness"))
                self.assertEqual([], [
                    action for action in proposals["proposals"]
                    if action["operation"] == "assign-copilot"
                ])

    def test_operator_nomination_requires_exact_approval_even_with_standing_policy(self) -> None:
        _, _, proposals = self._propose()
        now = datetime(2026, 8, 19, 16, tzinfo=UTC)
        policy = _policy_document(
            created_at_utc=now, enabled_classes=frozenset({"delegate-copilot"}),
        )
        policy["repository"] = proposals["repository"]
        selection = build_policy_selection(
            proposals, run_id=f"cycle:{proposals['snapshotId']}",
            policy_projection=_projection(policy_doc=policy), action_events=[], now=now,
        )
        self.assertEqual([], selection["selectedActionIds"])
        self.assertEqual("operator-request-requires-exact-approval", selection["candidates"][0]["reason"])
        action_id = proposals["proposals"][0]["actionId"]
        exact = _exact_decision(
            action_id=action_id, proposal_digest=_digest_of(proposals),
            decision="approve-once", now=now,
        )
        exact["expiresAtUtc"] = "2026-08-19T17:00:00Z"
        selection = build_policy_selection(
            proposals, run_id=f"cycle:{proposals['snapshotId']}",
            policy_projection=_projection(policy_doc=policy, exact_decisions=[exact]),
            action_events=[], now=now,
        )
        self.assertEqual([action_id], selection["selectedActionIds"])
        self.assertEqual("exact", selection["candidates"][0]["status"])

    def test_missing_optional_diagnostics_do_not_prevent_requesting_investigation(self) -> None:
        value, prepared, proposals = self._propose(diagnostic_error=True)
        assignment, = proposals["proposals"]
        self.assertTrue(assignment["executionEligibility"]["eligible"])
        coverage = build_managed_item_coverage(
            value, policy=load_repository_policy(ASPIRE_REPOSITORY_POLICY_PATH),
            proposals=proposals,
            investigation_plan={"blockedAwaitingEvidence": [{
                "issueNumber": 21, "target": {"kind": "issue", "value": 21},
            }]},
            review_schedule={},
            observations={"occurrences": [{
                "issueNumber": 21, "verifiedScope": {"kind": "unknown"},
            }]},
        )
        capability = {key: coverage[key] for key in (
            "schemaVersion", "valid", "blockers", "globalBlockers", "blockedScopes",
        )}
        self.assertEqual((False, set()), coverage_exclusions(capability, proposals["proposals"]))
        self.assertEqual("pending-action", coverage["items"][0]["coverageReason"])

    def test_reported_quarantine_needs_no_local_source_inspection(self) -> None:
        value, prepared, proposals = self._propose(labels=("quarantined-test",))
        self.assertIsNone(value.get("quarantineSourceState"))
        self.assertEqual("unverified-quarantine", prepared["issues"][0]["testMaintenance"]["state"])
        assignment, = proposals["proposals"]
        self.assertEqual("operator-request", assignment["evidenceBasis"])
        self.assertIn("`Refs #21`", assignment["customInstructions"])
        self.assertIn("Keep the tracking issue open", assignment["customInstructions"])
        self.assertIsNone(assignment.get("sourceRevision"))

    def test_diagnostic_exemption_does_not_license_same_issue_recovery(self) -> None:
        from tests.test_policy_selection import _close_proposal, _comment_proposal

        value, _, proposals = self._propose(diagnostic_error=True)
        close = _close_proposal(action_id="close:21", issue_number=21)
        comment = _comment_proposal(action_id="comment:22", issue_number=22)
        proposals["proposals"].extend([close, comment])
        coverage = build_managed_item_coverage(
            value, policy=load_repository_policy(ASPIRE_REPOSITORY_POLICY_PATH),
            proposals=proposals, investigation_plan={}, review_schedule={},
            observations={"occurrences": [{"issueNumber": 21, "verifiedScope": {"kind": "unknown"}}]},
        )
        capability = {key: coverage[key] for key in (
            "schemaVersion", "valid", "blockers", "globalBlockers", "blockedScopes",
        )}
        self.assertEqual(
            (False, {"close:21"}), coverage_exclusions(capability, proposals["proposals"]),
        )

    def test_source_confirmed_quarantine_can_be_delegated_without_local_diagnosis(self) -> None:
        value = quarantined_snapshot()
        policy = load_repository_policy(ASPIRE_REPOSITORY_POLICY_PATH)
        value["repositoryPolicy"] = {**policy.as_public_dict(), "digest": policy.digest}
        prepared = prepare_assessment(value)
        compact = build_compact_poc_input(prepared)
        judgment = copy.deepcopy(compact["issues"][0]["defaultJudgment"])
        self.assertEqual("delegate-copilot", judgment["recommendations"][0]["disposition"])
        self.assertEqual([], judgment["recommendations"][0]["missingEvidence"])
        judgments = {"schemaVersion": 1, "snapshotId": prepared["snapshotId"], "issues": [judgment]}
        validate_poc_projectability(compact, judgments)
        proposals = build_action_proposals(value, prepared, judgments, "operator", agent_input=compact)
        validate_action_proposals(proposals)
        assignment, = proposals["proposals"]
        self.assertTrue(assignment["executionEligibility"]["eligible"])
        self.assertEqual("source-reconciliation", assignment["evidenceBasis"])
        self.assertEqual(
            value["quarantineSourceState"]["sourceRevision"],
            assignment["sourceEvidenceFingerprint"]["sourceRevision"],
        )
        self.assertIn("`Refs #21`", assignment["customInstructions"])

    def test_fresh_nomination_reassesses_unchanged_issue(self) -> None:
        from ci_shepherd.review_selection import build_review_selection
        _, prepared, _ = self._propose()
        selection = build_review_selection(
            build_compact_poc_input(prepared), known_issue_numbers=[21],
        )
        self.assertEqual([21], [case["issueNumber"] for case in selection["selected"]])
        self.assertIn("delegate-copilot", selection["selected"][0]["allowedDispositions"])
        self.assertEqual("no-fetch", selection["selected"][0]["question"]["costClass"])
        self.assertEqual([], selection["selected"][0]["question"]["decisionGates"])

    def test_prepared_nomination_cannot_forge_frozen_operator_intent(self) -> None:
        value, prepared, _ = self._propose()
        value.pop("delegationRequests")
        compact = build_compact_poc_input(prepared)
        proposals = build_action_proposals(value, prepared, {
            "schemaVersion": 1, "snapshotId": prepared["snapshotId"],
            "issues": [compact["issues"][0]["defaultJudgment"]],
        }, "operator", agent_input=compact)
        self.assertEqual([], proposals["proposals"])

    def test_assignment_basis_cannot_authorize_comment_or_closure(self) -> None:
        from tests.test_policy_selection import _comment_proposal, _close_proposal, _document
        for factory in (_comment_proposal, _close_proposal):
            with self.subTest(operation=factory.__name__):
                proposal = factory(action_id="action:1", issue_number=21)
                proposal["evidenceBasis"] = proposal["executionEligibility"]["evidenceBasis"] = "operator-request"
                proposal["executionEligibility"]["ciLabels"] = []
                with self.assertRaisesRegex(ValueError, "only licenses assignment"):
                    validate_action_proposals(_document([proposal]))

    def test_issue_control_error_still_blocks_assignment(self) -> None:
        value, prepared, _ = self._propose(diagnostic_error=True)
        for stage, scope in (
            ("issue", {"kind": "issue", "issueNumbers": [21]}),
            ("workflow-log", {"kind": "repository"}),
            ("timeline", {"kind": "issue", "issueNumbers": [21]}),
        ):
            with self.subTest(stage=stage, scope=scope):
                value["collectionErrors"][0].update(stage=stage, scope=scope)
                compact = build_compact_poc_input(prepared)
                proposals = build_action_proposals(value, prepared, {
                    "schemaVersion": 1, "snapshotId": prepared["snapshotId"],
                    "issues": [compact["issues"][0]["defaultJudgment"]],
                }, "operator", agent_input=compact)
                self.assertFalse(proposals["proposals"][0]["executionEligibility"]["eligible"])

    def test_operator_assignment_executes_once_without_ci_labels_and_respects_ownership(self) -> None:
        from ci_shepherd.actor import execute_action
        from tests.test_actor import ScriptedActorClient

        value, _, proposals = self._propose()
        proposal, = proposals["proposals"]
        issue = {
            "number": 21, "state": "open", "html_url": proposal["issueUrl"],
            "updated_at": value["evidence"]["issue:21"]["payload"]["updatedAt"],
            "labels": [], "assignees": [],
        }
        for assignees in ([], [{"login": "human-owner"}], [{"login": "Copilot"}]):
            with self.subTest(assignees=assignees):
                client = ScriptedActorClient(authenticated_login="operator", issues=[
                    {**issue, "assignees": assignees},
                    {**issue, "assignees": [{"login": "Copilot"}]},
                ])
                prior = {"schemaVersion": 1, "repository": value["repository"], "results": []}
                result = execute_action(
                    proposals, action_id=proposal["actionId"], prior_results=prior,
                    client=client, now=lambda: datetime(2026, 8, 19, 16, tzinfo=UTC),
                )
                writes = [call for call in client.calls if call[0] == "assign_copilot"]
                self.assertEqual(1 if not assignees else 0, len(writes))
                if not assignees:
                    self.assertEqual("executed", result["outcome"])
                    prior["results"].append(result)
                    calls = list(client.calls)
                    execute_action(
                        proposals, action_id=proposal["actionId"], prior_results=prior,
                        client=client, now=lambda: datetime(2026, 8, 19, 16, tzinfo=UTC),
                    )
                    self.assertEqual(calls, client.calls)

    def test_retired_attempt_needs_fresh_nomination_and_reopened_pr_blocks_it(self) -> None:
        value, _, _ = self._propose(labels=("ci-failure-cause",))
        value["delegationStatus"] = handoff_snapshot(changed_files=3)["delegationStatus"]
        record, = value["delegationStatus"]["records"]
        record.update(
            lifecycle="completed", attemptOutcome="merged", requiresNewDecision=True,
            retired=True, copilotAssigned=False,
        )
        record["pullRequests"][0]["state"] = "merged"
        value.pop("delegationRequests")
        prepared, compact, _, proposals = assess(value)
        self.assertEqual([], proposals["proposals"])
        self.assertEqual("merged", compact["issues"][0]["delegationContext"]["records"][0]["attemptOutcome"])
        self.assertIsNone(compact["issues"][0].get("delegationReadiness"))
        value["evidence"]["issue:21"]["payload"]["producer"] = "unknown"
        coverage = build_managed_item_coverage(
            value, policy=load_repository_policy(ASPIRE_REPOSITORY_POLICY_PATH),
            proposals=proposals, investigation_plan={}, review_schedule={}, observations={},
        )
        self.assertTrue(coverage["valid"])
        self.assertEqual("awaiting-new-decision", coverage["items"][0]["coverageReason"])

        value["delegationRequests"] = [21]
        _, _, _, proposals = assess(value)
        assignment, = proposals["proposals"]
        self.assertEqual("operator-request", assignment["evidenceBasis"])

        record["pullRequests"][0]["state"] = "open"
        _, _, _, proposals = assess(value)
        self.assertEqual([], proposals["proposals"])

    def test_retry_uses_latest_attempt_but_checks_all_history_for_active_work(self) -> None:
        value, _, _ = self._propose()
        value["delegationStatus"] = handoff_snapshot(changed_files=3)["delegationStatus"]
        latest, = value["delegationStatus"]["records"]
        latest.update(
            lifecycle="completed", attemptOutcome="merged", requiresNewDecision=True,
            taskState="in_progress", taskObservation="unavailable", retired=True,
        )
        latest["pullRequests"][0]["state"] = "merged"
        older = copy.deepcopy(latest)
        older.update(
            actionId="older-action", startedAt="2026-08-01T00:00:00Z",
            lifecycle="handoff_required", taskState="failed", taskObservation="available",
            attemptOutcome="unresolved", pullRequests=[], retired=False,
        )
        value["delegationStatus"]["records"].insert(0, older)
        prepared, compact, _, proposals = assess(value)
        self.assertEqual(2, len(compact["issues"][0]["delegationContext"]["records"]))
        self.assertEqual(["operator-request"], [proposal["evidenceBasis"] for proposal in proposals["proposals"]])
        for state in ("open", "unknown"):
            with self.subTest(state=state):
                older["pullRequests"] = [{**latest["pullRequests"][0], "state": state}]
                self.assertEqual([], assess(value)[3]["proposals"])

    def test_verified_ended_task_without_pr_needs_fresh_exact_decision(self) -> None:
        value, _, _ = self._propose()
        value["delegationStatus"] = handoff_snapshot()["delegationStatus"]
        record, = value["delegationStatus"]["records"]
        record.update(
            attemptOutcome="unresolved", requiresNewDecision=True,
            taskState="failed", taskObservation="available", pullRequests=[],
        )
        self.assertEqual(["operator-request"], [
            proposal["evidenceBasis"] for proposal in assess(value)[3]["proposals"]
        ])
        for outcome, observation in (
            ("legacy-unknown", "available"),
            ("unresolved", "unavailable"),
            ("pending", "available"),
        ):
            with self.subTest(outcome=outcome, observation=observation):
                record.update(attemptOutcome=outcome, taskObservation=observation)
                self.assertEqual([], [
                    proposal for proposal in assess(value)[3]["proposals"]
                    if proposal["operation"] == "assign-copilot"
                ])
