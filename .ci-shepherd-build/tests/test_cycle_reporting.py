from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import cycle
from ci_shepherd.collector import Collector
from ci_shepherd.poc_state import load_review_schedule
from tests.assessment_helpers import finish_reviewed_cycle
from tests.test_collector import ScriptedClient
from tests.test_cycle import snapshot
from tests.test_refresh import current_history, issue_summary, prior_snapshot


class CycleReportingTests(unittest.TestCase):
    def test_mixed_cycle_preserves_repair_priority_classification_and_human_ownership(self) -> None:
        from tests.test_observations import association, evidence, issue_payload
        from tests.test_production_decisions import quarantined_snapshot
        from tests.test_repair_routing import producer_snapshot

        value = quarantined_snapshot()
        producer = producer_snapshot()
        root_record = producer["evidence"].pop("issue:21")
        root_record["payload"].update(number=31, url="https://github.com/microsoft/aspire/issues/31")
        root_record["url"] = root_record["payload"]["url"]
        producer["evidence"]["issue:31"] = root_record
        for record in producer["evidence"].values():
            if "referencedBy" in record["payload"]:
                record["payload"]["referencedBy"] = association(31) + association(42)
        value["evidence"].update(producer["evidence"])
        value["openIssues"].append(31)
        value["issues"].append(root_record["payload"])
        for number, assignees in ((41, []), (42, ["maintainer"])):
            if assignees:
                issue = copy.deepcopy(root_record["payload"])
                issue.update(number=number, url=f"https://github.com/microsoft/aspire/issues/{number}", assignees=assignees)
            else:
                issue = issue_payload(number, facts=[])
                issue.update(
                    title=f"Unclassified failure {number}", body="The failing subject is not known.",
                    labels=["test-failure"], assignees=assignees,
                )
            identity, record = evidence(f"issue:{number}", "issue-event", issue)
            value["evidence"][identity] = record
            value["openIssues"].append(number)
            value["issues"].append(issue)

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            for index in range(2):
                observed = copy.deepcopy(value)
                observed["collectedAt"] = f"2026-08-19T16:0{index}:00Z"
                input_path, work = root / f"input-{index}.json", root / f"work-{index}"
                input_path.write_text(json.dumps(observed), encoding="utf-8")
                started = cycle.start_cycle(
                    repository=value["repository"], state_dir=state, work_dir=work,
                    checkout=None, shepherd_author="ankj", input_path=input_path,
                )
                if index == 0:
                    self.assertEqual("awaiting-review", started["stage"])
                    finish_reviewed_cycle(work_dir=work, agent_judgments_path=work / "agent-judgments.json")
                else:
                    self.assertEqual("completed", started["stage"])
                    self.assertEqual(0, started["issueReviewCount"])
                proposals = json.loads((work / "action-proposals.json").read_text())
                assignments = {
                    row["issueNumber"]: row for row in proposals["proposals"]
                    if row["operation"] == "assign-copilot"
                }
                self.assertEqual(
                    {21, 31}, set(assignments),
                    json.loads((work / "judgments.json").read_text())["issues"],
                )
                self.assertLess(assignments[31]["repairPriority"]["rank"], assignments[21]["repairPriority"]["rank"])
                self.assertEqual("workflow-producer", assignments[31]["evidenceBasis"])
                plan = json.loads((work / "investigation-plan.json").read_text())
                self.assertIn(41, [row["issueNumber"] for row in plan["requests"]])
                self.assertNotIn(42, assignments)
                prepared = json.loads((work / "assessment-input.json").read_text())
                human_owned = next(issue for issue in prepared["issues"] if issue["issueNumber"] == 42)
                self.assertTrue(human_owned["repairEvidence"]["ready"])
                self.assertIsNotNone(human_owned["producerAdmission"])
                report = (work / "report.md").read_text()
                if index == 0:
                    self.assertIn("cloud investigate-and-fix", report)
                else:
                    self.assertIn(
                        "<summary>2 unchanged / excluded inventory items</summary>\n\nissue #21, issue #31",
                        report,
                    )

    def test_collector_uses_observation_clock_for_historical_run_backoff(self) -> None:
        previous = prior_snapshot()
        previous["evidence"]["run:99"]["availability"] = "partial"
        previous["evidence"]["run:99"]["payload"]["errorCategory"] = "not-found"
        client = ScriptedClient(pages={
            "/repos/owner/repo/issues?state=open&labels=ci-failure-cause&per_page=100": [issue_summary(1)],
            "/repos/owner/repo/issues?state=open&labels=automation-broken&per_page=100": [],
            "/repos/owner/repo/issues?state=open&creator=github-actions%5Bbot%5D&per_page=100": [],
        })
        collector = Collector(client, "owner/repo", datetime(2026, 8, 18, 1, tzinfo=UTC))
        inventory = collector.collect_incremental(
            previous, current_history(previous), include_supporting=False, include_timeline=False,
        )
        self.assertIn("run:99", inventory.refresh_plan.reuse)
        self.assertEqual("partial", inventory.evidence["run:99"]["availability"])
        self.assertEqual("2026-08-18T00:00:00Z", inventory.evidence["run:99"]["collectedAt"])

    def test_finished_cycle_reports_packet_and_capacity_facts_and_preserves_retry_wakeup(self) -> None:
        value = snapshot("2026-08-27T12:00:00Z")
        value["evidence"]["run:99"] = {
            "kind": "workflow-run", "url": "https://github.com/owner/repo/actions/runs/99",
            "collectedAt": "2026-08-27T11:00:00Z", "availability": "partial",
            "payload": {
                "runId": 99, "targetRepository": "owner/repo", "errorCategory": "not-found",
                "referencedBy": [{"sourceIssueNumber": 1, "sourceEvidenceId": "issue:1"}],
            },
        }
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, work = root / "state", root / "work"
            input_path = root / "input.json"
            input_path.write_text(json.dumps(value), encoding="utf-8")
            cycle.start_cycle(
                repository="owner/repo", state_dir=state, work_dir=work,
                checkout=None, shepherd_author="ankj", input_path=input_path,
            )
            completed = finish_reviewed_cycle(
                work_dir=work, agent_judgments_path=work / "agent-judgments.json",
            )
            self.assertEqual("completed", completed["stage"])
            report = (work / "report.md").read_text(encoding="utf-8")
            manifest = json.loads((work / "assessment-batches.json").read_text(encoding="utf-8"))
            byte_count = sum(group["byteCount"] for group in manifest["workerGroups"])
            self.assertIn(f"| Current | 1 | 1 | 1 | {byte_count} |", report)
            self.assertIn("0 occupied of 3 slots; 3 available.", report)
            before = load_review_schedule(
                state, "owner/repo", "2026-08-28T10:59:59Z",
                issue_numbers=[1], pull_request_numbers=[],
            )
            due = load_review_schedule(
                state, "owner/repo", "2026-08-28T11:00:00Z",
                issue_numbers=[1], pull_request_numbers=[],
            )
            self.assertEqual([], before["dueIssueNumbers"])
            self.assertEqual([1], due["dueIssueNumbers"])
            wakeup_log = state / "ledgers/review-wakeups.jsonl"
            recorded = wakeup_log.read_bytes()
            with self.assertRaisesRegex(ValueError, "Cycle is not awaiting review"):
                cycle.finish_cycle(work_dir=work, agent_judgments_path=work / "agent-judgments.json")
            self.assertEqual(recorded, wakeup_log.read_bytes())
