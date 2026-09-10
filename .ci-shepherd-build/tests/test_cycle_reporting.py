from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import cycle
import render
from ci_shepherd.collector import Collector
from ci_shepherd.poc_state import load_review_schedule
from tests.assessment_helpers import finish_reviewed_cycle
from tests.test_collector import ScriptedClient
from tests.test_cycle import snapshot
from tests.test_refresh import current_history, issue_summary, prior_snapshot


class CycleReportingTests(unittest.TestCase):
    def test_finalizer_writes_canonical_report_before_recording_completion_without_ledger_writes(self) -> None:
        from tests.test_scripts import poc_prepared, poc_judgments

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            work, state = root / "work", root / "state"
            work.mkdir()
            state.mkdir()
            prepared = poc_prepared([(1, "Failure")])
            judgments = poc_judgments(
                prepared, [(1, "flaky-test", "no-action", "issue", 1, "high", [], "New evidence.")],
            )
            value = {
                "repository": "owner/repo", "collectedAt": prepared["sourceCollectedAt"],
                "issues": [{"number": 1, "title": "Failure"}],
            }
            for name, document in (
                ("assessment-input.json", prepared), ("judgments.json", judgments), ("input.json", value),
            ):
                (work / name).write_text(json.dumps(document))
            (work / "report.md").write_text("Pre-execution report.\n")
            (work / "report-details.md").write_text("Frozen evidence and decisions.\n")
            ledger = state / "action-events.jsonl"
            ledger.write_text("")
            invocation = root / "invocation.json"
            invocation.write_text(json.dumps({"runId": "invocation:1", "startedAt": "2026-09-10T12:00:00Z"}))
            original_invocation = invocation.read_bytes()
            original_inputs = {path: path.read_bytes() for path in work.glob("*.json")}
            output = root / "final-operator-report.md"
            client = ScriptedClient()
            original_write = render._write_markdown

            def write(path, content):
                if path == output:
                    self.assertEqual(original_invocation, invocation.read_bytes())
                    self.assertFalse("completedAt" in json.loads(invocation.read_text()))
                return original_write(path, content)

            args = [
                "render.py", "--run-report", "--finalize-run", "--prepared", str(work / "assessment-input.json"),
                "--judgments", str(work / "judgments.json"), "--snapshot", str(work / "input.json"),
                "--state-dir", str(state), "--invocation", str(invocation), "--output", str(output),
            ]
            with patch("sys.argv", args + ["--run-id", "different-invocation"]), patch.object(render, "GitHubClient") as rejected_client:
                with self.assertRaisesRegex(ValueError, "must match the recorded invocation"):
                    render.main()
                rejected_client.assert_not_called()
            with patch("sys.argv", args), patch.object(render, "GitHubClient", return_value=client) as client_factory, patch.object(render, "_write_markdown", side_effect=write):
                self.assertEqual(0, render.main())
            self.assertEqual(1, client_factory.call_args.kwargs["max_attempts"])
            self.assertEqual(1, client_factory.call_args.kwargs["max_pages"])
            self.assertEqual(10, client_factory.call_args.kwargs["request_timeout_seconds"])
            self.assertEqual([], client.calls)
            self.assertEqual(b"", ledger.read_bytes())
            self.assertEqual(original_inputs, {path: path.read_bytes() for path in work.glob("*.json")})
            final = json.loads(invocation.read_text())
            self.assertEqual(str(output.resolve()), final["finalReport"])
            self.assertGreaterEqual(datetime.fromisoformat(final["completedAt"].replace("Z", "+00:00")).timestamp(), output.stat().st_mtime)
            self.assertIn("Canonical post-execution report", output.read_text())
            self.assertIn("after this file is written in the [invocation manifest](invocation.json)", output.read_text())
            self.assertIn("**Superseded pre-execution report.**", (work / "report.md").read_text())
            self.assertIn("Pre-execution report.", (work / "report.md").read_text())
            self.assertIn("Frozen evidence and decisions.", (root / "final-report-details.md").read_text())
            self.assertIn("**Current state:**", (root / "final-report-details.md").read_text())
            self.assertNotIn("**Current state:**", output.read_text())
            self.assertEqual("complete", json.loads((root / "post-execution-observation.json").read_text())["status"])

            invocation.write_bytes(original_invocation)
            before_report = (work / "report.md").read_bytes()

            def fail_final_report(path, content):
                if path == output:
                    raise OSError("report disk unavailable")
                return original_write(path, content)

            with patch("sys.argv", args), patch.object(render, "GitHubClient", return_value=client), patch.object(render, "_write_markdown", side_effect=fail_final_report):
                with self.assertRaisesRegex(OSError, "report disk unavailable"):
                    render.main()
            self.assertEqual(original_invocation, invocation.read_bytes())
            self.assertEqual(before_report, (work / "report.md").read_bytes())

    def test_final_observation_reuses_task_pr_observer_and_never_mutates_frozen_evidence(self) -> None:
        from tests.test_cloud_outcomes import outcome_snapshot
        from tests.test_delegation_observer import ScriptedClient as DelegationClient

        value = outcome_snapshot(body="Old body")
        prepared = {
            "snapshotId": "snapshot:frozen", "issues": [
                {"issueNumber": 1, "testMaintenance": {"state": "quarantined"}},
            ],
        }
        existing = value["delegationStatus"]["records"][0]
        baseline = {
            "eventType": "delegation-baseline", "actionId": "assignment:1", "recordedAt": "2026-09-02T18:00:00Z",
            "operation": "assign-copilot", "repository": "owner/repo", "snapshotId": prepared["snapshotId"],
            "target": {"kind": "issue", "number": 1}, "taskIdsBefore": [],
        }
        events = [
            baseline,
            {**baseline, "eventType": "terminal", "outcome": "executed", "result": {"taskId": "task-1"}},
            {**baseline, "eventType": "delegation-observed", "record": existing},
        ]
        frozen = copy.deepcopy((value, prepared, events))
        client = DelegationClient({
            ("/agents/repos/owner/repo/tasks?state=queued%2Cin_progress&is_archived=false&per_page=100", "tasks"): [],
        }, {
            "/agents/repos/owner/repo/tasks/task-1": {
                "id": "task-1", "state": "completed", "created_at": "2026-09-02T18:00:00Z",
                "artifacts": [{"type": "pull", "provider": "github", "data": {"id": 101, "global_id": "PR_101"}}],
            },
            "/repos/owner/repo/pulls/201": {
                "id": 101, "number": 201, "node_id": "PR_101", "state": "open", "draft": True,
                "changed_files": 3, "head": {"sha": "a" * 40}, "comments": 0,
                "html_url": "https://github.com/owner/repo/pull/201",
                "body": "Refs #1\n" + "x" * 5000 + "\n### Test execution evidence\n"
                        "Before/after: single-test, Windows, repeat.ps1, executed 1 per iteration; passed 20/20.\n"
                        "### Generated suffix\nFiXeS OWNER/REPO#1",
                "user": {"login": "Copilot"}, "updated_at": "2026-09-02T18:59:00Z",
            },
            "/repos/owner/repo/issues/1": {"number": 1, "state": "open", "assignees": []},
            f"/repos/owner/repo/commits/{'a' * 40}/check-runs?per_page=100": {
                "total_count": 1, "check_runs": [{
                    "id": 3, "name": "CI", "status": "completed", "conclusion": "success",
                    "head_sha": "a" * 40,
                }],
            },
        })
        now = datetime(2026, 9, 2, 19, tzinfo=UTC)
        observation = render.refresh_report_delegations(value, prepared, events, client=client, now=lambda: now)
        self.assertEqual("complete", observation["status"], observation["problems"])
        self.assertEqual(5, observation["apiCalls"])
        record, = observation["delegationStatus"]["records"]
        pull, = record["pullRequests"]
        self.assertEqual(("task-1", "completed", True), (record["taskId"], record["taskState"], record["issueOpen"]))
        self.assertEqual(("violation", "green", True), (
            pull["closingContract"]["status"], pull["currentState"]["checks"]["state"], pull["isDraft"],
        ))
        self.assertEqual(
            "Before/after: single-test, Windows, repeat.ps1, executed 1 per iteration; passed 20/20.",
            pull["reportedTestExecution"]["preview"],
        )
        self.assertEqual(frozen, (value, prepared, events))
        self.assertEqual("2026-09-02T19:00:00Z", observation["observedAt"])
        from ci_shepherd.actions import _delegation_instructions

        normal_prepared = {**prepared, "issues": [{"issueNumber": 1}]}
        proposal = {
            "actionId": "assignment:1", "issueNumber": 1, "operation": "assign-copilot",
            "customInstructions": _delegation_instructions(1, None, test_failure=False),
        }
        proposal_document = {
            "repository": "owner/repo", "snapshotId": prepared["snapshotId"], "proposals": [proposal],
        }
        workflow_facts = {"workflowHealth": {
            "workflowPath": ".github/workflows/ci.yml", "job": "build", "evidenceIds": ["run:1", "job:2"],
        }}
        for facts, proposals, expected_status, expected_basis in (
            (workflow_facts, None, "violation", "frozen workflow failure facts"),
            ({}, proposal_document, "not-applicable", "frozen dispatch instructions"),
            ({}, None, "unknown", "unavailable"),
            ({}, {**proposal_document, "snapshotId": "another-snapshot"}, "unknown", "unavailable"),
        ):
            with self.subTest(facts=facts, expected_status=expected_status):
                checked = render.refresh_report_delegations(
                    value, {**normal_prepared, "issues": [{"issueNumber": 1, **facts}]},
                    events, client=client, now=lambda: now, action_proposals=proposals,
                )
                contract = checked["delegationStatus"]["records"][0]["pullRequests"][0]["closingContract"]
                self.assertEqual((expected_status, expected_basis), (contract["status"], contract["basis"]))
                self.assertEqual(["FiXeS OWNER/REPO#1"], contract["matches"])
        baseline_instructions = _delegation_instructions(1, None, test_failure=False, workflow_failure=True)
        checked = render.refresh_report_delegations(
            value, normal_prepared,
            [{**events[0], "customInstructions": baseline_instructions}, *events[1:]],
            client=client, now=lambda: now, action_proposals=proposal_document,
        )
        self.assertEqual(
            "violation", checked["delegationStatus"]["records"][0]["pullRequests"][0]["closingContract"]["status"],
        )
        client.calls.clear()
        client.records["/agents/repos/owner/repo/tasks/task-1"]["artifacts"].append({
            "type": "branch", "provider": "github",
            "data": {"head_ref": "copilot/fix", "base_ref": "main"},
        })
        client.pages[("/repos/owner/repo/pulls?head=owner%3Acopilot%2Ffix&state=all&per_page=100", None)] = [
            client.records["/repos/owner/repo/pulls/201"],
        ]
        fresh_value = copy.deepcopy(value)
        fresh_value.pop("delegationStatus")
        fresh = render.refresh_report_delegations(
            fresh_value, prepared, events[:2], client=client, now=lambda: now,
        )
        self.assertEqual("complete", fresh["status"], fresh["problems"])
        self.assertEqual(6, fresh["apiCalls"])
        discovered, = fresh["delegationStatus"]["records"][0]["pullRequests"]
        self.assertEqual("https://github.com/owner/repo/pull/201", discovered["url"])
        self.assertEqual("violation", discovered["closingContract"]["status"])
        self.assertNotIn("delegationStatus", fresh_value)
        client.calls.clear()
        partial = render.refresh_report_delegations(value, prepared, events, client=client, max_api_calls=1, now=lambda: now)
        self.assertEqual("partial", partial["status"])
        self.assertEqual(1, len(client.calls))
        self.assertIn("budget", " ".join(partial["problems"]))
        self.assertEqual("task-1", partial["delegationStatus"]["records"][0]["taskId"])
        self.assertEqual(frozen, (value, prepared, events))

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
