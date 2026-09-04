from __future__ import annotations

import unittest
from dataclasses import replace

from ci_shepherd.managed_coverage import (
    block_policy_selection,
    build_managed_item_coverage,
    render_managed_item_coverage_section,
)
from ci_shepherd.repository_policy import load_repository_policy
from tests.test_policy import ASPIRE_REPOSITORY_POLICY_PATH


class ManagedCoverageTests(unittest.TestCase):
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
            proposals={"proposals": [{"issueNumber": 1}]},
            investigation_plan={
                "requests": [],
                "deferredRequests": [{"issueNumber": 3}],
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
                ("issue", 3, "active-investigation"),
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

    def test_uncovered_item_blocks_all_mutation_and_remains_visible(self) -> None:
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
        self.assertEqual([], selection["selectedActionIds"])
        self.assertTrue(selection["mutationBlocked"])
        self.assertEqual(
            {"thisRun": 0, "rolling24h": 0},
            selection["maximumWriteExposure"],
        )
        report = render_managed_item_coverage_section(coverage)
        self.assertIn("Mutation gate: **blocked**", report)
        self.assertIn("`issue:1` | `uncovered` | `uncovered`", report)
