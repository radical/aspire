from __future__ import annotations

import copy
import unittest
from datetime import UTC, datetime

from ci_shepherd.eligibility import repair_priority, repair_priority_key
from ci_shepherd.investigations import build_investigation_plan
from ci_shepherd.policy_selection import build_policy_selection
from test_investigations import _judgments, _prepared
from test_policy_selection import _comment_proposal, _document, _policy_document, _projection


def priority_cases():
    return [
        {"issueNumber": 1},
        {"issueNumber": 2, "producer": "tracking-issue"},
        {"issueNumber": 3, "testMaintenance": {"state": "quarantined", "evidenceIds": ["issue:3"]}},
        {"issueNumber": 4, "repairEvidence": {"observedFailure": {
            "current": True, "workflowPath": ".github/workflows/tests.yml",
            "category": "unknown", "lastFailureAt": "2026-09-03T15:00:00Z",
        }}},
        {"issueNumber": 5, "workflowHealth": {
            "current": True, "workflowPath": ".github/workflows/release.yml",
            "category": "blocking-build", "lastFailureAt": "2026-09-03T14:00:00Z",
        }},
        {"issueNumber": 6, "repairEvidence": {"observedFailure": {
            "current": True, "workflowPath": ".github/workflows/ci.yml",
            "category": "unknown", "lastFailureAt": "2026-09-03T13:00:00Z",
        }}},
    ]


class RepairPriorityTests(unittest.TestCase):
    def test_current_ci_other_workflow_normal_tests_quarantine_then_unknown(self):
        issues = priority_cases()
        self.assertEqual([6, 5, 4, 3, 2, 1], [
            issue["issueNumber"] for issue in sorted(issues, key=repair_priority_key)
        ])
        self.assertEqual([
            "unclassified-issue", "automation-defect", "quarantined-test-repair",
            "unquarantined-test-instability", "current-workflow-break", "current-ci-workflow-break",
        ], [repair_priority(issue)["kind"] for issue in issues])
        self.assertNotIn("ready", issues[-1]["repairEvidence"])

    def test_stale_ci_workflow_does_not_outrank_current_test_or_quarantine(self):
        issues = priority_cases()
        issues[-1]["repairEvidence"]["observedFailure"]["current"] = False
        self.assertEqual([5, 4, 3, 2, 1, 6], [
            issue["issueNumber"] for issue in sorted(issues, key=repair_priority_key)
        ])
        self.assertIsNone(repair_priority(issues[-1])["lastFailureAt"])

    def test_incomplete_ci_diagnostic_precedes_another_diagnosed_workflow(self):
        issue = priority_cases()[-1]
        issue["repairEvidence"].update(
            current=True, ready=True, workflowPath=".github/workflows/release.yml",
            category="blocking-build", lastFailureAt="2026-09-03T15:00:00Z",
        )
        self.assertEqual("current-ci-workflow-break", repair_priority(issue)["kind"])
        self.assertEqual("2026-09-03T13:00:00Z", repair_priority(issue)["lastFailureAt"])

    def test_current_other_workflow_precedes_diagnosed_test_or_quarantine(self):
        for quarantined in (False, True):
            issue = {
                "issueNumber": 1,
                "alreadyQuarantined": quarantined,
                "repairEvidence": {
                    "current": True, "ready": True, "category": "flaky-test",
                    "quarantinedCoverage": quarantined,
                    "workflowPath": ".github/workflows/tests-quarantine.yml" if quarantined else ".github/workflows/tests.yml",
                    "observedFailure": {
                        "current": True, "category": "unknown",
                        "workflowPath": ".github/workflows/release.yml",
                    },
                },
            }
            with self.subTest(quarantined=quarantined):
                self.assertEqual("current-workflow-break", repair_priority(issue)["kind"])

    def test_same_priority_uses_latest_current_failure_even_without_diagnostic(self):
        issue = priority_cases()[-1]
        issue["repairEvidence"].update(
            current=True, ready=True, workflowPath=".github/workflows/ci.yml",
            category="blocking-build", lastFailureAt="2026-09-02T15:00:00Z",
        )
        self.assertEqual("2026-09-03T13:00:00Z", repair_priority(issue)["lastFailureAt"])

    def test_planner_allocates_budget_in_the_same_priority_order(self):
        prepared = _prepared()
        judgments = _judgments()
        template = prepared["issues"][0]
        judgment = judgments["issues"][0]
        prepared["issues"] = []
        judgments["issues"] = []
        for case in priority_cases():
            number = case["issueNumber"]
            issue = {**copy.deepcopy(template), **case}
            issue["issueUrl"] = f"https://github.com/owner/repo/issues/{number}"
            issue["evidenceBundle"][0]["id"] = f"issue:{number}"
            prepared["issues"].append(issue)
            decision = copy.deepcopy(judgment)
            decision["issueNumber"] = number
            decision["recommendations"][0]["target"]["value"] = number
            decision["recommendations"][0]["evidenceIds"] = [f"issue:{number}", "run:210"]
            judgments["issues"].append(decision)
        plan = build_investigation_plan(prepared, judgments, [], max_requests=4)
        self.assertEqual([6, 5, 4, 3], [row["issueNumber"] for row in plan["requests"]])
        self.assertEqual([2, 1], [row["issueNumber"] for row in plan["deferredRequests"]])
        self.assertEqual({"per-cycle-investigation-budget"}, {
            row["reason"] for row in plan["deferredRequests"]
        })

    def test_copilot_selection_allocates_budget_in_the_same_priority_order(self):
        proposals = []
        for case in priority_cases():
            number = case["issueNumber"]
            proposal = _comment_proposal(action_id=f"assignment:{number}", issue_number=number)
            proposal.pop("body")
            proposal.update(
                operation="assign-copilot", targetRepository="microsoft/aspire", baseBranch="main",
                customInstructions="Investigate the scoped failure.", model="",
                repairPriorityFacts={key: value for key, value in case.items() if key != "issueNumber"},
            )
            proposal["repairPriority"] = repair_priority(case)
            proposals.append(proposal)
        policy = _policy_document(enabled_classes=frozenset({"delegate-copilot"}))
        policy["operationClasses"]["delegate-copilot"].update(maxPerRun=4, maxRolling24h=10)
        document = _document(proposals)
        selection = build_policy_selection(
            document, run_id=f"cycle:{document['snapshotId']}",
            policy_projection=_projection(policy_doc=policy), action_events=[],
            now=datetime(2026, 9, 3, 16, tzinfo=UTC),
        )
        self.assertEqual(["assignment:6", "assignment:5", "assignment:4", "assignment:3"],
                         selection["selectedActionIds"])
