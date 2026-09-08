from __future__ import annotations

import unittest
import copy
from dataclasses import replace
from datetime import UTC, datetime

from ci_shepherd.managed_coverage import (
    block_policy_selection,
    build_managed_item_coverage,
    render_managed_item_coverage_section,
)
from ci_shepherd.repository_policy import load_repository_policy
from tests.test_policy import ASPIRE_REPOSITORY_POLICY_PATH
from tests.test_policy_selection import (
    _close_proposal, _comment_proposal, _digest_of, _document,
    _exact_decision, _policy_document, _projection,
)
from ci_shepherd.policy_selection import build_policy_selection


class ManagedCoverageTests(unittest.TestCase):
    def test_planned_and_budget_deferred_work_are_not_active_sessions(self) -> None:
        policy = replace(
            load_repository_policy(ASPIRE_REPOSITORY_POLICY_PATH),
            managed_issue_producers=frozenset({"ci-failure-cause"}),
            managed_automation_explicit=True,
        )
        snapshot = {
            "repository": "owner/repo", "openIssues": [1, 2, 3, 4],
            "evidence": {
                f"issue:{number}": {"payload": {"producer": "ci-failure-cause"}}
                for number in range(1, 5)
            },
        }
        def request(number):
            return {"issueNumber": number, "investigationId": f"investigation:{number}",
                    "target": {"kind": "issue", "value": number}}

        coverage = build_managed_item_coverage(
            snapshot, policy=policy,
            proposals={"snapshotId": "snapshot:current", "proposals": []},
            investigation_plan={
                "repository": "owner/repo", "snapshotId": "snapshot:current",
                "requests": [request(1)],
                "deferredRequests": [{**request(2), "reason": "per-cycle-investigation-budget"}],
                "activeInvestigations": [request(3)],
                "activeInvestigationIds": ["investigation:3"],
            },
            review_schedule={}, observations={},
        )
        self.assertEqual(
            ["queued-investigation", "deferred-investigation", "active-investigation", "uncovered"],
            [item["coverageReason"] for item in coverage["items"]],
        )
        self.assertEqual(
            [{"kind": "issue", "issueNumber": 4, "reason": "uncovered"}],
            coverage["blockedScopes"],
        )
        self.assertEqual(1, coverage["counts"]["active-investigation"])
        self.assertEqual(0, coverage["counts"].get("scheduled-wakeup", 0))

    def test_unbound_or_unregistered_plans_do_not_manufacture_coverage(self) -> None:
        policy = replace(
            load_repository_policy(ASPIRE_REPOSITORY_POLICY_PATH),
            managed_issue_producers=frozenset({"ci-failure-cause"}),
            managed_automation_explicit=True,
        )
        plan = {
            "repository": "owner/repo", "snapshotId": "snapshot:current",
            "activeInvestigations": [{"issueNumber": 1, "investigationId": "investigation:1",
                                      "target": {"kind": "issue", "value": 1}}],
            "activeInvestigationIds": ["investigation:1"],
        }
        for changes in (
            {"activeInvestigationIds": "investigation:1"},
            {"activeInvestigationIds": []},
            {"snapshotId": "snapshot:old"},
            {"repository": "another/repo"},
            {"activeInvestigations": [], "deferredRequests": [{"issueNumber": 1}]},
        ):
            with self.subTest(changes=changes):
                coverage = build_managed_item_coverage(
                    {"repository": "owner/repo", "openIssues": [1],
                     "evidence": {"issue:1": {"payload": {"producer": "ci-failure-cause"}}}},
                    policy=policy, proposals={"snapshotId": "snapshot:current", "proposals": []},
                    investigation_plan={**plan, **changes}, review_schedule={}, observations={},
                )
                self.assertFalse(coverage["valid"])
                self.assertEqual("uncovered", coverage["items"][0]["coverageReason"])

    def test_unresolved_issue_does_not_spend_safe_actions_single_budget_slot(self) -> None:
        policy = replace(
            load_repository_policy(ASPIRE_REPOSITORY_POLICY_PATH),
            managed_issue_producers=frozenset({"ci-failure-cause"}),
            managed_automation_explicit=True,
        )
        document = _document([
            _comment_proposal(action_id="a:ping-human-comment", issue_number=1),
            _close_proposal(action_id="a:close", issue_number=1, depends_on="a:ping-human-comment"),
            _comment_proposal(action_id="b:watch-comment", issue_number=2),
        ])
        coverage = build_managed_item_coverage(
            {"repository": document["repository"], "openIssues": [1, 2],
             "evidence": {f"issue:{n}": {"payload": {"producer": "ci-failure-cause"}} for n in (1, 2)}},
            policy=policy, proposals=document, investigation_plan={},
            review_schedule={}, observations={"occurrences": [
                {"issueNumber": 1, "verifiedScope": {"kind": "unknown"}},
            ]},
        )
        document["productionPilotCapability"] = {
            "schemaVersion": 1, "evidenceRound": 0,
            "managedItemCoverage": {
                k: v for k, v in coverage.items() if k not in {"repository", "counts", "items"}
            },
        }
        now = datetime(2026, 9, 4, tzinfo=UTC)
        policy_doc = _policy_document(enabled_classes=frozenset({"create-comment"}))
        policy_doc["operationClasses"]["create-comment"]["maxPerRun"] = 1
        selection = build_policy_selection(
            document, run_id="test", policy_projection=_projection(policy_doc=policy_doc),
            action_events=[], now=now,
        )
        self.assertFalse(coverage["valid"])
        self.assertEqual(["b:watch-comment"], selection["selectedActionIds"])
        projection = _projection(policy_doc=policy_doc, exact_decisions=[
            _exact_decision(action_id="a:ping-human-comment", proposal_digest=_digest_of(document),
                            decision="approve-once", now=now),
        ])
        for _ in range(2):
            selected = build_policy_selection(document, run_id="test", policy_projection=projection, action_events=[], now=now)
            self.assertEqual(["b:watch-comment"], selected["selectedActionIds"])
            self.assertEqual(
                {"a:ping-human-comment", "a:close"},
                {item["actionId"] for item in selected["candidates"] if item["reason"] == "managed-item-coverage-invalid"},
            )
        for invalid in (None, [], {"kind": "issue", "issueNumber": True, "reason": "unknown"},
                        {"kind": "target", "issueNumber": 1, "target": {"kind": "issue", "value": 2}, "reason": "unknown"},
                        {"kind": "action", "actionId": "", "reason": "unknown"}):
            changed = copy.deepcopy(document)
            changed["productionPilotCapability"]["managedItemCoverage"]["blockedScopes"] = [invalid]
            with self.subTest(scope=invalid), self.assertRaises(ValueError):
                build_policy_selection(changed, run_id="test", policy_projection=projection, action_events=[], now=now)
        for gate in (
            {"schemaVersion": 1, "valid": False, "blockers": ["legacy untrusted scope"]},
            {"schemaVersion": 2, "valid": False, "blockers": ["observation error"],
             "globalBlockers": [{"reason": "observation error"}], "blockedScopes": []},
        ):
            changed = copy.deepcopy(document)
            changed["productionPilotCapability"]["managedItemCoverage"] = gate
            selected = build_policy_selection(changed, run_id="test", policy_projection=projection, action_events=[], now=now)
            self.assertEqual([], selected["selectedActionIds"])
            self.assertEqual({"thisRun": 0, "rolling24h": 0}, selected["maximumWriteExposure"])

    def test_observation_collection_failure_blocks_mutation(self) -> None:
        policy = replace(
            load_repository_policy(ASPIRE_REPOSITORY_POLICY_PATH),
            managed_issue_producers=frozenset({"ci-failure-cause"}),
            managed_automation_explicit=True,
        )
        coverage = build_managed_item_coverage(
            {
                "repository": "owner/repo",
                "openIssues": [],
                "delegatedIssues": [],
                "evidence": {},
            },
            policy=policy,
            proposals={"proposals": []},
            investigation_plan={"requests": [], "deferredRequests": []},
            review_schedule={"issues": {}, "pullRequests": {}},
            observations={"occurrences": []},
            observation_error="invalid structured evidence",
        )
        selection = block_policy_selection(
            {
                "automaticActionIds": ["action-1"],
                "exactActionIds": [],
                "selectedActionIds": ["action-1"],
                "maximumWriteExposure": {"thisRun": 1, "rolling24h": 1},
                "candidates": [],
            },
            coverage,
        )

        self.assertFalse(coverage["valid"])
        self.assertEqual([], selection["selectedActionIds"])
        self.assertEqual(
            ["observations:collection-error:invalid structured evidence"],
            coverage["blockers"],
        )
        self.assertIn(
            "`collection:observations` | `collection-error` | `uncovered`",
            render_managed_item_coverage_section(coverage),
        )

    def test_projects_each_configured_active_item_exactly_once(self) -> None:
        policy = replace(
            load_repository_policy(ASPIRE_REPOSITORY_POLICY_PATH),
            managed_issue_producers=frozenset({"ci-failure-cause"}),
            manages_pull_requests=True,
            managed_automation_explicit=True,
        )
        snapshot = {
            "repository": "owner/repo",
            "openIssues": [1, 2, 3, 4],
            "delegatedIssues": [2],
            "openPullRequests": [10],
            "evidence": {
                f"issue:{number}": {
                    "payload": {
                        "number": number,
                        "producer": "ci-failure-cause",
                    }
                }
                for number in range(1, 5)
            },
            "delegationStatus": {
                "records": [
                    {
                        "issueNumber": 2,
                        "lifecycle": "awaiting_pull_request",
                        "pullRequests": [],
                    }
                ]
            },
        }
        coverage = build_managed_item_coverage(
            snapshot,
            policy=policy,
            proposals={"snapshotId": "snapshot:current", "proposals": [{"issueNumber": 1}]},
            investigation_plan={
                "repository": "owner/repo",
                "snapshotId": "snapshot:current",
                "requests": [],
                "deferredRequests": [{
                    "issueNumber": 3, "investigationId": "investigation:3",
                    "target": {"kind": "issue", "value": 3},
                    "reason": "per-cycle-investigation-budget",
                }],
            },
            review_schedule={
                "issues": {
                    "4": {
                        "reassessAt": "2026-09-01T00:00:00Z",
                        "wakeReason": "scheduled-review",
                    }
                },
                "pullRequests": {},
            },
            observations={"occurrences": []},
        )

        self.assertTrue(coverage["valid"])
        self.assertEqual(
            [
                ("issue", 1, "pending-action"),
                ("issue", 2, "active-delegation"),
                ("issue", 3, "deferred-investigation"),
                ("issue", 4, "scheduled-wakeup"),
                ("pull-request", 10, "tracked-open-pr"),
            ],
            [
                (
                    item["targetKind"],
                    item["targetNumber"],
                    item["coverageReason"],
                )
                for item in coverage["items"]
            ],
        )

    def test_uncovered_item_is_reported_without_globally_revoking_selection(self) -> None:
        policy = replace(
            load_repository_policy(ASPIRE_REPOSITORY_POLICY_PATH),
            managed_issue_producers=frozenset({"ci-failure-cause"}),
            managed_automation_explicit=True,
        )
        coverage = build_managed_item_coverage(
            {
                "repository": "owner/repo",
                "openIssues": [1],
                "delegatedIssues": [],
                "evidence": {
                    "issue:1": {
                        "payload": {
                            "number": 1,
                            "producer": "ci-failure-cause",
                        }
                    }
                },
            },
            policy=policy,
            proposals={"proposals": []},
            investigation_plan={"requests": [], "deferredRequests": []},
            review_schedule={"issues": {}, "pullRequests": {}},
            observations={"occurrences": []},
        )
        selection = block_policy_selection(
            {
                "automaticActionIds": ["action-1"],
                "exactActionIds": [],
                "selectedActionIds": ["action-1"],
                "maximumWriteExposure": {"thisRun": 1, "rolling24h": 1},
                "candidates": [
                    {
                        "actionId": "action-1",
                        "status": "automatic",
                        "reason": "policy",
                    }
                ],
            },
            coverage,
        )

        self.assertFalse(coverage["valid"])
        self.assertEqual(["action-1"], selection["selectedActionIds"])
        self.assertFalse(selection.get("mutationBlocked", False))
        self.assertEqual(
            {"thisRun": 1, "rolling24h": 1},
            selection["maximumWriteExposure"],
        )
        report = render_managed_item_coverage_section(coverage)
        self.assertIn("Mutation gate: **item-local**", report)
        self.assertIn("`issue:1` | `uncovered` | `uncovered`", report)
