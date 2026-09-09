from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import collect as collect_script
from ci_shepherd.collector import enrich_workflow_discovery
from ci_shepherd.lifecycle import prepare_assessment
from ci_shepherd.models import validate_snapshot
from tests.test_delegation_observer import ScriptedClient
from tests.test_workflow_discovery import (
    DiscoveryClient, NOW, REPOSITORY, issue_inventory, responses_for, run,
)


class ComparisonCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        runs = [run(2, conclusion="success"), run(1)]
        runs[0].update(
            created_at="2026-09-08T14:00:00Z", updated_at="2026-09-08T15:00:00Z",
            run_started_at="2026-09-08T14:00:00Z",
        )
        responses = responses_for(runs)
        responses[f"/repos/{REPOSITORY}/actions/runs/2/attempts/1/jobs"]["jobs"][0].update(
            started_at="2026-09-08T14:00:00Z", completed_at="2026-09-08T15:00:00Z",
        )
        self.runs, self.responses = runs, responses
        self.inventory = enrich_workflow_discovery(
            issue_inventory(), DiscoveryClient(responses), REPOSITORY, NOW,
        )
        self.base, self.head = "b" * 40, f"{2:040x}"
        self.status = {"status": "complete", "records": [{
            "actionId": "assignment:42", "repository": REPOSITORY, "issueNumber": 42,
            "startedAt": "2026-09-08T13:02:00Z", "taskId": "task-1", "taskState": "completed",
            "lifecycle": "completed", "attemptOutcome": "merged", "requiresHuman": False,
            "requiresNewDecision": True, "retired": True, "issueOpen": True, "copilotAssigned": False,
            "pullRequests": [{
                "databaseId": 101, "globalId": "PR_101", "number": 201,
                "state": "merged", "isDraft": False, "changedFiles": 4,
                "mergedAt": "2026-09-08T13:03:00Z", "mergeCommitSha": self.base,
            }],
        }]}
        self.client = ScriptedClient({})
        self.add_comparison(self.base, self.head)

    def add_comparison(self, base: str, head: str) -> None:
        path = f"/repos/{REPOSITORY}/compare/{base}...{head}"
        self.client.records[f"{path}?per_page=1"] = {
            "url": f"https://api.github.com{path}", "status": "ahead", "behind_by": 0,
            "base_commit": {"sha": base}, "merge_base_commit": {"sha": base},
        }

    def collect(self, *, previous_snapshot=None, include_workflow_discovery=False) -> tuple[dict, dict]:
        collector = Mock()
        collector.collect.return_value = self.inventory
        collector.collect_incremental.return_value = self.inventory
        collector.enrich_github_evidence.side_effect = lambda inventory, **options: inventory
        collector.enrich_ownership_evidence.side_effect = lambda inventory, **options: inventory
        with (
            TemporaryDirectory() as temporary,
            patch.object(collect_script, "GitHubClient", return_value=self.client),
            patch.object(collect_script, "Collector", return_value=collector),
            patch.object(collect_script, "ActionEventStore") as store,
            patch.object(collect_script, "observe_delegation_status", return_value=(self.status, ())),
            patch.object(collect_script, "record_delegation_wakeups"),
            patch.object(collect_script, "datetime") as clock,
            patch.object(collect_script, "load_current") as current,
            patch.object(collect_script, "enrich_workflow_discovery", return_value=self.inventory) as enrich,
        ):
            clock.now.return_value = NOW
            store.return_value.events.return_value = []
            root = Path(temporary)
            current.return_value = None
            if previous_snapshot is not None:
                (root / "snapshot.json").write_text(json.dumps(previous_snapshot))
                current.return_value = SimpleNamespace(run_directory=root, document={})
            output = collect_script.collect(
                REPOSITORY, root / "collected", None, state_dir=root,
                include_workflow_discovery=include_workflow_discovery,
            )
            self.discovery_options = enrich.call_args.kwargs if enrich.called else {}
            snapshot = json.loads((output / "input.json").read_text())
            progress = json.loads((output / "progress.json").read_text())
        validate_snapshot(snapshot)
        return snapshot, progress

    def test_discovery_receives_validated_previous_snapshot_for_repair_retention(self) -> None:
        previous = copy.deepcopy(collect_script.build_snapshot(
            REPOSITORY, NOW, self.inventory, delegation_status=self.status,
        ))
        self.collect(previous_snapshot=previous, include_workflow_discovery=True)
        self.assertEqual(previous, self.discovery_options["previous_snapshot"])
        self.assertEqual(previous["workflowDiscovery"], self.discovery_options["previous_discovery"])

    def test_refresh_retains_immutable_ancestry_for_still_observed_repair_heads(self) -> None:
        previous, _ = self.collect()
        self.client.records.clear()
        self.client.calls.clear()
        refreshed, _ = self.collect(previous_snapshot=previous)
        self.assertEqual([], self.client.calls)
        self.assertEqual(previous["commitComparisons"], refreshed["commitComparisons"])
        self.assertEqual("verified", prepare_assessment(refreshed)["issues"][0]["repairFollowup"]["status"])

    def test_canonical_collection_verifies_successful_descendant_and_freezes_exact_proof(self) -> None:
        before = collect_script.build_snapshot(REPOSITORY, NOW, self.inventory, delegation_status=self.status)
        self.assertEqual("unknown", prepare_assessment(before)["issues"][0]["repairFollowup"]["status"])
        snapshot, progress = self.collect()
        followup = prepare_assessment(snapshot)["issues"][0]["repairFollowup"]
        self.assertEqual("verified", followup["status"])
        self.assertEqual(self.head, followup["verification"]["headSha"])
        self.assertEqual(snapshot["commitComparisons"], followup["verification"]["commitComparisons"])
        self.assertEqual(1, len(self.client.calls))
        self.assertEqual([], snapshot["collectionErrors"])
        events = [event for event in progress["events"] if event["stage"] == "repair-comparison"]
        self.assertEqual(["started", "completed"], [event["status"] for event in events])

    def test_unavailable_or_malformed_proof_is_scoped_and_remains_unknown(self) -> None:
        for malformed in (False, True):
            with self.subTest(malformed=malformed):
                self.client.records.clear()
                self.client.calls.clear()
                if malformed:
                    self.add_comparison(self.base, self.head)
                    next(iter(self.client.records.values()))["url"] = "https://example.com/untrusted"
                snapshot, progress = self.collect()
                followup = prepare_assessment(snapshot)["issues"][0]["repairFollowup"]
                self.assertEqual("unknown", followup["status"])
                self.assertEqual("unknown" if malformed else "unavailable", snapshot["commitComparisons"][0]["availability"])
                self.assertEqual([{"kind": "issue", "issueNumbers": [42]}], [
                    error["scope"] for error in snapshot["collectionErrors"]
                ])
                self.assertEqual(["repair-comparison"], [error["stage"] for error in snapshot["collectionErrors"]])
                self.assertEqual(1, len(self.client.calls))
                events = [event for event in progress["events"] if event["stage"] == "repair-comparison"]
                self.assertIn("1 scoped proof gaps", events[-1]["message"])

    def test_unique_get_budget_preserves_unqueried_pairs_as_unknown(self) -> None:
        pull = self.status["records"][0]["pullRequests"][0]
        self.status["records"][0]["pullRequests"] = [
            {**pull, "databaseId": 101 + index, "number": 201 + index, "globalId": f"PR_{101 + index}",
             "mergeCommitSha": f"{100 + index:040x}"}
            for index in range(14)
        ]
        for item in self.status["records"][0]["pullRequests"]:
            self.add_comparison(item["mergeCommitSha"], self.head)
        snapshot, progress = self.collect()
        self.assertEqual(12, len(self.client.calls))
        self.assertEqual(12, len(set(self.client.calls)))
        self.assertEqual(["available"] * 12 + ["unknown"] * 2, [
            comparison["availability"] for comparison in snapshot["commitComparisons"]
        ])
        followup = prepare_assessment(snapshot)["issues"][0]["repairFollowup"]
        self.assertEqual("unknown", followup["status"])
        self.assertEqual(2, len(followup["missingEvidence"]))
        self.assertEqual(2, len(snapshot["collectionErrors"]))
        self.assertTrue(all("12-request" in error["message"] for error in snapshot["collectionErrors"]))

    def test_closed_issue_followup_collects_deduplicated_proof_without_reopening(self) -> None:
        self.inventory.evidence["issue:42"]["payload"]["state"] = "closed"
        self.inventory = replace(self.inventory, open_issues=[])
        record = self.status["records"][0]
        record["issueOpen"] = False
        record["pullRequests"].append({
            **record["pullRequests"][0], "databaseId": 102, "number": 202, "globalId": "PR_102",
        })
        snapshot, _ = self.collect()
        prepared = prepare_assessment(snapshot)
        self.assertEqual([], snapshot["openIssues"])
        self.assertEqual([], prepared["issues"])
        self.assertEqual("closed", prepared["closedIssueFollowups"][0]["issueState"])
        self.assertEqual("verified", prepared["closedIssueFollowups"][0]["repairFollowup"]["status"])
        self.assertEqual(1, len(self.client.calls))

    def test_failed_head_comparison_can_reveal_successful_head_within_same_budget(self) -> None:
        failed = run(3)
        failed.update(
            created_at="2026-09-08T14:10:00Z", updated_at="2026-09-08T15:10:00Z",
            run_started_at="2026-09-08T14:10:00Z",
        )
        responses = responses_for([failed, *self.runs])
        responses[f"/repos/{REPOSITORY}/actions/runs/2/attempts/1/jobs"] = self.responses[
            f"/repos/{REPOSITORY}/actions/runs/2/attempts/1/jobs"
        ]
        responses[f"/repos/{REPOSITORY}/actions/runs/3/attempts/1/jobs"]["jobs"][0].update(
            started_at="2026-09-08T14:10:00Z", completed_at="2026-09-08T15:10:00Z",
        )
        self.inventory = enrich_workflow_discovery(
            issue_inventory(), DiscoveryClient(responses), REPOSITORY, NOW,
        )
        failed_head = f"{3:040x}"
        self.add_comparison(self.base, failed_head)
        failed_path = f"/repos/{REPOSITORY}/compare/{self.base}...{failed_head}?per_page=1"
        self.client.records[failed_path].update(
            status="behind", behind_by=1, merge_base_commit={"sha": failed_head},
        )
        snapshot, _ = self.collect()
        self.assertEqual("verified", prepare_assessment(snapshot)["issues"][0]["repairFollowup"]["status"])
        self.assertEqual([failed_path, f"/repos/{REPOSITORY}/compare/{self.base}...{self.head}?per_page=1"], [
            path for path, _ in self.client.calls
        ])
        self.client.calls.clear()
        self.add_comparison(self.base, failed_head)
        snapshot, _ = self.collect()
        followup = prepare_assessment(snapshot)["issues"][0]["repairFollowup"]
        self.assertEqual("reassessment-required", followup["status"])
        self.assertEqual([3], [failure["runId"] for failure in followup["laterFailures"]])
        self.assertEqual(1, len(self.client.calls))
