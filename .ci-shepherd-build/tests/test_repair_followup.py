from __future__ import annotations

import copy
import unittest

from ci_shepherd.observations import build_observations
from ci_shepherd.actions import build_action_proposals
from ci_shepherd.delegation_observer import observe_commit_comparison
from ci_shepherd.lifecycle import prepare_assessment
from ci_shepherd.poc import build_compact_poc_input, validate_poc_projectability
from ci_shepherd.repair_followup import build_repair_followup
from ci_shepherd.run_report import render_run_markdown
from ci_shepherd.workflow_health import build_workflow_health
from test_observations import association, evidence, issue_payload, policy
from test_delegation_observer import ScriptedClient
from test_workflow_health import add_execution, workflow_snapshot


class RepairFollowupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = workflow_snapshot()
        self.record = {
            "actionId": "assignment:12", "repository": self.snapshot["repository"], "issueNumber": 12,
            "taskId": "task-1", "taskState": "completed", "taskObservation": "available",
            "startedAt": "2026-08-19T15:35:00Z", "lifecycle": "completed",
            "attemptOutcome": "merged", "requiresNewDecision": True,
            "requiresHuman": False, "retired": True, "issueOpen": True,
            "pullRequests": [{
                "databaseId": 101, "globalId": "PR_101", "number": 201,
                "state": "merged", "isDraft": False, "changedFiles": 4,
                "mergedAt": "2026-08-19T15:40:00Z", "mergeCommitSha": "b" * 40,
            }],
        }
        self.snapshot["delegationStatus"] = {"status": "complete", "records": [self.record]}

    def followup(self) -> dict[str, object]:
        observations = build_observations(self.snapshot, policy=policy())
        issue = {"issueNumber": 12}
        health = build_workflow_health(self.snapshot, observations, issue)
        return build_repair_followup(self.snapshot, observations, issue, health)

    def add_new_issue_failure(self, *, old_closed: bool) -> None:
        add_execution(
            self.snapshot, 102, "2026-08-19T15:50:00Z",
            excerpt="src/File.cs(1,1): error CS1002: ; expected",
        )
        self.snapshot["evidence"]["run:102"]["payload"]["headSha"] = "b" * 40
        row = self.snapshot["evidence"]["issue:12"]["payload"]["ledger"]["rows"].pop()
        new_issue = issue_payload(13, ledger_rows=[row])
        self.snapshot["issues"].append(new_issue)
        self.snapshot["openIssues"].append(13)
        self.snapshot["evidence"].update([evidence("issue:13", "issue-event", new_issue)])
        for record in self.snapshot["evidence"].values():
            if record["payload"].get("runId") == 102:
                record["payload"]["referencedBy"] = association(13)
        if old_closed:
            self.snapshot["openIssues"].remove(12)
            self.snapshot["evidence"]["issue:12"]["payload"]["state"] = "closed"
            self.record["issueOpen"] = False

    def test_failure_without_the_fix_does_not_invalidate_verified_repair(self) -> None:
        add_execution(
            self.snapshot, 101, "2026-08-19T15:42:00Z",
            excerpt="src/File.cs(1,1): error CS1002: ; expected",
        )
        add_execution(self.snapshot, 102, "2026-08-19T15:45:00Z", conclusion="success")
        self.snapshot["evidence"]["run:102"]["payload"]["headSha"] = "b" * 40
        base, head = "b" * 40, "a" * 40
        comparison = {
            "repository": self.snapshot["repository"], "baseSha": base, "headSha": head,
            "url": f"https://api.github.com/repos/{self.snapshot['repository']}/compare/{base}...{head}",
            "availability": "available", "status": "behind", "behindBy": 1,
            "baseCommitSha": base, "mergeBaseSha": head,
        }
        self.snapshot["commitComparisons"] = [comparison]
        prepared = prepare_assessment(self.snapshot)
        self.assertEqual("verified", prepared["issues"][0]["repairFollowup"]["status"])
        self.assertTrue(prepared["issues"][0]["workflowHealth"]["closureAllowed"])

        for comparisons in ([], [{**comparison, "mergeBaseSha": base}]):
            with self.subTest(comparisons=comparisons):
                self.snapshot["commitComparisons"] = comparisons
                item = prepare_assessment(self.snapshot)["issues"][0]
                self.assertEqual("unknown", item["repairFollowup"]["status"])
                self.assertEqual(101, item["repairFollowup"]["unverifiedFailures"][0]["runId"])
                self.assertFalse(item["workflowHealth"]["closureAllowed"])

    def test_other_event_or_workflow_path_cannot_verify_the_repair(self) -> None:
        for field, changed in (("event", "schedule"), ("workflowPath", ".github/workflows/other.yml")):
            with self.subTest(field=field):
                self.setUp()
                add_execution(self.snapshot, 101, "2026-08-19T15:45:00Z", conclusion="success")
                self.snapshot["evidence"]["run:101"]["payload"].update(headSha="b" * 40, **{field: changed})
                followup = self.followup()
                self.assertEqual("awaiting-post-fix-success", followup["status"])
                self.assertNotIn("verification", followup)

    def test_refresh_keeps_every_unclassified_failed_head_when_discovery_is_unavailable(self) -> None:
        from datetime import UTC, datetime
        from dataclasses import replace
        import collect as collect_script
        from ci_shepherd.collector import enrich_workflow_discovery
        from test_workflow_discovery import DiscoveryClient, api_error, empty_inventory

        add_execution(self.snapshot, 101, "2026-08-19T15:45:00Z")
        add_execution(self.snapshot, 102, "2026-08-19T15:50:00Z")
        self.snapshot["evidence"]["run:102"]["payload"]["headSha"] = "c" * 40
        self.assertEqual(2, len(self.followup()["unverifiedFailures"]))
        endpoint = f"/repos/{self.snapshot['repository']}"
        now = datetime(2026, 8, 19, 16, tzinfo=UTC)
        for cycle in range(3):
            with self.subTest(cycle=cycle):
                inventory = enrich_workflow_discovery(
                    replace(empty_inventory(), open_issues=self.snapshot["issues"]),
                    DiscoveryClient({endpoint: api_error(endpoint)}), self.snapshot["repository"],
                    now, previous_snapshot=self.snapshot,
                )
                self.snapshot = collect_script.build_snapshot(
                    self.snapshot["repository"], now, inventory, delegation_status=self.snapshot["delegationStatus"],
                )
                self.assertEqual("unavailable", inventory.workflow_discovery["status"])
                self.assertEqual("unknown", prepare_assessment(self.snapshot)["issues"][0]["repairFollowup"]["status"])
                for run_id in (101, 102):
                    self.assertEqual(run_id, inventory.evidence[f"run:{run_id}"]["payload"]["runId"])
                    self.assertEqual(
                        "failure", inventory.evidence[f"run:{run_id}:attempt:1:job:{run_id * 10}"]["payload"]["conclusion"],
                    )

    def test_recurrence_in_a_new_issue_keeps_prior_repair_and_source_identity(self) -> None:
        for old_closed in (False, True):
            with self.subTest(old_closed=old_closed):
                self.setUp()
                add_execution(self.snapshot, 101, "2026-08-19T15:45:00Z", conclusion="success")
                self.snapshot["evidence"]["run:101"]["payload"]["headSha"] = "b" * 40
                self.add_new_issue_failure(old_closed=old_closed)
                prepared = prepare_assessment(self.snapshot)
                old = next(
                    item for item in prepared.get("closedIssueFollowups", []) + prepared["issues"]
                    if item["issueNumber"] == 12
                )
                followup = old["repairFollowup"]
                self.assertEqual("reassessment-required", followup["status"])
                self.assertEqual(13, followup["laterFailures"][0]["sourceIssueNumber"])
                self.assertEqual("unknown", followup["laterFailures"][0]["sameRootCause"])
                self.assertEqual("task-1", followup["attempts"][0]["taskId"])
                compact = build_compact_poc_input(prepared)
                report = render_run_markdown(self.snapshot, prepared, {
                    "schemaVersion": 1, "snapshotId": prepared["snapshotId"],
                    "issues": [row["defaultJudgment"] for row in compact["issues"]],
                })
                self.assertIn(
                    f"source issue(s): [#13](https://github.com/{self.snapshot['repository']}/issues/13)",
                    report,
                )
                if old_closed:
                    self.assertEqual([13], [item["issueNumber"] for item in prepared["issues"]])
                    self.assertEqual("closed", old["issueState"])
                else:
                    self.assertFalse(old["workflowHealth"]["closureAllowed"])

    def test_new_issue_cannot_duplicate_active_work_even_with_an_operator_nomination(self) -> None:
        self.record.update(
            taskState="in_progress", lifecycle="running", attemptOutcome="pending",
            retired=False, requiresNewDecision=False,
        )
        self.record["pullRequests"][0].update(state="open", mergedAt=None, mergeCommitSha=None)
        self.add_new_issue_failure(old_closed=False)
        for nominations in ([], [13]):
            with self.subTest(nominations=nominations):
                self.snapshot["delegationRequests"] = nominations
                prepared = prepare_assessment(self.snapshot)
                compact = build_compact_poc_input(prepared)
                item = next(row for row in compact["issues"] if row["issueNumber"] == 13)
                self.assertEqual(12, item["relatedWorkflowRepairs"][0]["issueNumber"])
                self.assertEqual("no-action", item["defaultJudgment"]["recommendations"][0]["disposition"])
                judgments = {
                    "schemaVersion": 1, "snapshotId": prepared["snapshotId"],
                    "issues": [row["defaultJudgment"] for row in compact["issues"]],
                }
                validate_poc_projectability(compact, judgments)
                item["defaultJudgment"]["recommendations"][0].update(
                    disposition="delegate-copilot", confidence="medium", missingEvidence=[],
                )
                with self.assertRaises(ValueError):
                    build_action_proposals(self.snapshot, prepared, judgments, "radical", agent_input=compact)

    def test_ended_related_repair_needs_fresh_nomination_and_passes_frozen_prior_links(self) -> None:
        self.add_new_issue_failure(old_closed=False)
        compact = build_compact_poc_input(prepare_assessment(self.snapshot))
        issue = next(row for row in compact["issues"] if row["issueNumber"] == 13)
        self.assertEqual("no-action", issue["defaultJudgment"]["recommendations"][0]["disposition"])

        self.snapshot["delegationRequests"] = [13]
        prepared = prepare_assessment(self.snapshot)
        compact = build_compact_poc_input(prepared)
        issue = next(row for row in compact["issues"] if row["issueNumber"] == 13)
        self.assertEqual("delegate-copilot", issue["defaultJudgment"]["recommendations"][0]["disposition"])
        untrusted = next(row for row in prepared["issues"] if row["issueNumber"] == 13)
        untrusted["relatedWorkflowRepairs"][0]["issueUrl"] = f"https://github.com/{self.snapshot['repository']}/issues/999"
        untrusted["relatedWorkflowRepairs"][0]["records"][0]["pullRequests"][0]["number"] = 999
        result = build_action_proposals(self.snapshot, prepared, {
            "schemaVersion": 1, "snapshotId": prepared["snapshotId"],
            "issues": [row["defaultJudgment"] for row in compact["issues"]],
        }, "radical", agent_input=compact)
        assignments = [row for row in result["proposals"] if row["operation"] == "assign-copilot"]
        self.assertEqual([13], [row["issueNumber"] for row in assignments])
        context_urls = [line[2:] for line in assignments[0]["customInstructions"].splitlines() if line.startswith("- https://")]
        self.assertEqual([
            f"https://github.com/{self.snapshot['repository']}/issues/12",
            f"https://github.com/{self.snapshot['repository']}/pull/201",
        ], context_urls[:2])

    def test_closed_issue_with_active_work_still_blocks_a_duplicate_repair(self) -> None:
        self.record.update(taskState="in_progress", lifecycle="running", attemptOutcome="pending")
        self.record["pullRequests"][0].update(state="open", mergedAt=None, mergeCommitSha=None)
        self.add_new_issue_failure(old_closed=True)
        prepared = prepare_assessment(self.snapshot)
        issue = build_compact_poc_input(prepared)["issues"][0]

        self.assertEqual([13], [row["issueNumber"] for row in prepared["issues"]])
        self.assertEqual(12, issue["relatedWorkflowRepairs"][0]["issueNumber"])
        self.assertEqual("no-action", issue["defaultJudgment"]["recommendations"][0]["disposition"])

    def test_related_work_does_not_block_a_different_workflow_or_failure(self) -> None:
        for difference in ("workflow", "fingerprint", "repository"):
            with self.subTest(difference=difference):
                self.setUp()
                self.record.update(taskState="in_progress", lifecycle="running", attemptOutcome="pending")
                self.record["pullRequests"][0].update(state="open", mergedAt=None, mergeCommitSha=None)
                self.add_new_issue_failure(old_closed=False)
                if difference == "workflow":
                    self.snapshot["evidence"]["run:102"]["payload"]["workflowId"] = 10
                elif difference == "repository":
                    self.record["repository"] = "another/repository"
                else:
                    self.snapshot["evidence"]["run:102:attempt:1:job:1020:log"]["payload"]["excerpt"] = (
                        "src/File.cs(1,1): error CS0246: The type could not be found"
                    )
                compact = build_compact_poc_input(prepare_assessment(self.snapshot))
                item = next(row for row in compact["issues"] if row["issueNumber"] == 13)
                self.assertEqual([], item.get("relatedWorkflowRepairs", []))
                self.assertEqual("delegate-copilot", item["defaultJudgment"]["recommendations"][0]["disposition"])

    def test_preparation_tracks_merge_through_source_proven_recovery(self) -> None:
        prepared = prepare_assessment(self.snapshot)
        self.assertEqual("awaiting-post-fix-success", prepared["issues"][0]["repairFollowup"]["status"])
        self.assertFalse(prepared["issues"][0]["workflowHealth"]["closureAllowed"])

        add_execution(self.snapshot, 101, "2026-08-19T15:45:00Z", conclusion="success")
        self.snapshot["evidence"]["run:101"]["payload"]["headSha"] = "b" * 40
        prepared = prepare_assessment(self.snapshot)
        self.assertEqual("verified", prepared["issues"][0]["repairFollowup"]["status"])
        self.assertTrue(prepared["issues"][0]["workflowHealth"]["closureAllowed"])
        compact = build_compact_poc_input(prepared)
        self.assertEqual(
            {key: value for key, value in prepared["issues"][0]["repairFollowup"].items() if key != "attempts"},
            compact["issues"][0]["repairFollowup"],
        )
        judgment = compact["issues"][0]["defaultJudgment"]
        self.assertEqual("review-close", judgment["recommendations"][0]["disposition"])
        validate_poc_projectability(compact, {
            "schemaVersion": 1, "snapshotId": prepared["snapshotId"], "issues": [judgment],
        })

    def test_model_claimed_repair_verification_cannot_authorize_closure(self) -> None:
        prepared = prepare_assessment(self.snapshot)
        compact = build_compact_poc_input(prepared)
        prepared["issues"][0]["repairFollowup"] = {"status": "verified"}
        prepared["issues"][0]["workflowHealth"]["closureAllowed"] = True
        judgment = compact["issues"][0]["defaultJudgment"]
        judgment["recommendations"][0].update(disposition="review-close", missingEvidence=[])
        result = build_action_proposals(self.snapshot, prepared, {
            "schemaVersion": 1, "snapshotId": prepared["snapshotId"], "issues": [judgment],
        }, "radical", agent_input=compact)

        self.assertEqual([], [row for row in result["proposals"] if row["operation"] == "close-issue"])
        self.assertEqual(["review-close"], [row["disposition"] for row in result["blockedRecommendations"]])

    def test_verified_history_does_not_close_over_resumed_or_open_work(self) -> None:
        add_execution(self.snapshot, 101, "2026-08-19T15:45:00Z", conclusion="success")
        self.snapshot["evidence"]["run:101"]["payload"]["headSha"] = "b" * 40
        self.record.update(taskState="in_progress", lifecycle="running")
        prepared = prepare_assessment(self.snapshot)

        self.assertEqual("work-in-progress", prepared["issues"][0]["repairFollowup"]["status"])
        self.assertFalse(prepared["issues"][0]["workflowHealth"]["closureAllowed"])
        self.assertEqual("no-action", build_compact_poc_input(prepared)["issues"][0]["defaultJudgment"]["recommendations"][0]["disposition"])

    def test_closed_tracked_issue_is_report_only_and_keeps_its_actual_state(self) -> None:
        self.snapshot["openIssues"] = []
        self.snapshot["evidence"]["issue:12"]["payload"]["state"] = "closed"
        self.record["issueOpen"] = False
        prepared = prepare_assessment(self.snapshot)

        self.assertEqual([], prepared["issues"])
        self.assertEqual([], build_compact_poc_input(prepared)["issues"])
        followup = prepared["closedIssueFollowups"][0]
        self.assertEqual((12, "closed"), (followup["issueNumber"], followup["issueState"]))
        self.assertEqual("awaiting-post-fix-success", followup["repairFollowup"]["status"])
        add_execution(self.snapshot, 101, "2026-08-19T15:45:00Z", conclusion="success")
        self.snapshot["evidence"]["run:101"]["payload"]["headSha"] = "b" * 40
        self.assertEqual("verified", prepare_assessment(self.snapshot)["closedIssueFollowups"][0]["repairFollowup"]["status"])

    def test_explicit_observation_selector_does_not_reopen_closed_evidence(self) -> None:
        self.snapshot["openIssues"] = []
        self.snapshot["evidence"]["issue:12"]["payload"]["state"] = "closed"
        self.assertEqual([], build_observations(self.snapshot, policy=policy())["occurrences"])
        observations = build_observations(self.snapshot, policy=policy(), issue_numbers=[12])
        self.assertEqual({12}, {row["issueNumber"] for row in observations["occurrences"]})
        self.assertEqual("closed", self.snapshot["evidence"]["issue:12"]["payload"]["state"])
        for selected in ([True], [0], ["12"], [13], [12, 12]):
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                build_observations(self.snapshot, policy=policy(), issue_numbers=selected)

    def test_closed_repair_report_shows_verification_and_later_failure_without_reopening(self) -> None:
        self.snapshot["openIssues"] = []
        self.snapshot["evidence"]["issue:12"]["payload"]["state"] = "closed"
        self.record["issueOpen"] = False
        for phase, label in (
            ("merged", "Merged; awaiting workflow recovery"),
            ("verified", "Post-fix workflow verified"),
            ("recurrent", "Post-merge failure; reassessment needed"),
        ):
            with self.subTest(phase=phase):
                if phase == "verified":
                    add_execution(self.snapshot, 101, "2026-08-19T15:45:00Z", conclusion="success")
                    self.snapshot["evidence"]["run:101"]["payload"]["headSha"] = "b" * 40
                elif phase == "recurrent":
                    add_execution(self.snapshot, 102, "2026-08-19T15:50:00Z")
                    self.snapshot["evidence"]["run:102"]["payload"]["headSha"] = "b" * 40
                prepared = prepare_assessment(self.snapshot)
                report = render_run_markdown(self.snapshot, prepared, {
                    "schemaVersion": 1, "snapshotId": prepared["snapshotId"], "issues": [],
                })
                for expected in (
                    label, "closed", "task-1",
                    f"https://github.com/{self.snapshot['repository']}/issues/12",
                    f"https://github.com/{self.snapshot['repository']}/pull/201",
                ):
                    self.assertIn(expected, report)
                if phase == "recurrent":
                    self.assertIn("same root cause: unknown", report)
                self.assertEqual([], prepared["issues"])
                self.assertEqual("closed", self.snapshot["evidence"]["issue:12"]["payload"]["state"])

    def test_merged_pull_and_closed_issue_are_not_incident_recovery(self) -> None:
        for issue_open in (True, False):
            with self.subTest(issue_open=issue_open):
                self.record["issueOpen"] = issue_open
                before = copy.deepcopy(self.snapshot)
                followup = self.followup()
                self.assertEqual("awaiting-post-fix-success", followup["status"])
                self.assertTrue(followup["requiresNewDecision"])
                self.assertEqual("task-1", followup["attempts"][0]["taskId"])
                self.assertEqual(201, followup["attempts"][0]["pullRequests"][0]["number"])
                self.assertEqual(before, self.snapshot)

    def test_only_affected_job_success_after_merge_on_the_fix_sha_verifies(self) -> None:
        add_execution(self.snapshot, 101, "2026-08-19T15:45:00Z", conclusion="success")
        run = self.snapshot["evidence"]["run:101"]["payload"]
        run["headSha"] = "b" * 40
        verified = self.followup()
        self.assertEqual("verified", verified["status"])
        self.assertEqual(
            (101, 1, 1010, "b" * 40),
            tuple(verified["verification"][key] for key in ("runId", "attempt", "jobId", "headSha")),
        )
        self.assertTrue(verified["requiresNewDecision"])
        job = self.snapshot["evidence"]["run:101:attempt:1:job:1010"]["payload"]
        job.update(startedAt="2026-08-19T15:35:00Z", completedAt="2026-08-19T15:36:00Z")
        self.assertEqual("awaiting-post-fix-success", self.followup()["status"])
        job.update(startedAt="2026-08-19T15:45:00Z", completedAt="2026-08-19T15:46:00Z",
                   name="Another successful job")
        self.assertEqual("awaiting-post-fix-success", self.followup()["status"])

    def test_all_merged_fixes_need_containment_and_unresolved_pulls_block_verification(self) -> None:
        add_execution(self.snapshot, 101, "2026-08-19T15:45:00Z", conclusion="success")
        head = "e" * 40
        self.snapshot["evidence"]["run:101"]["payload"]["headSha"] = head
        self.record["pullRequests"].append({
            "number": 202, "databaseId": 102, "globalId": "PR_102", "state": "merged",
            "mergedAt": "2026-08-19T15:42:00Z", "mergeCommitSha": "d" * 40,
        })
        self.snapshot["commitComparisons"] = []
        for base in ("b" * 40, "d" * 40):
            self.assertEqual("unknown", self.followup()["status"])
            path = f"/repos/{self.snapshot['repository']}/compare/{base}...{head}"
            client = ScriptedClient({}, {f"{path}?per_page=1": {
                "url": f"https://api.github.com{path}", "status": "ahead", "behind_by": 0,
                "base_commit": {"sha": base}, "merge_base_commit": {"sha": base},
            }})
            self.snapshot["commitComparisons"].append(
                observe_commit_comparison(client, self.snapshot["repository"], base, head)
            )
        self.assertEqual("verified", self.followup()["status"])
        self.record["pullRequests"].append({
            "number": 203, "databaseId": 103, "globalId": "PR_103", "state": "unknown",
        })
        self.assertEqual("unknown", self.followup()["status"])
        self.record["pullRequests"][-1]["state"] = "closed"
        self.assertEqual("verified", self.followup()["status"])

    def test_historical_merge_can_verify_after_task_disappears_but_missing_facts_cannot(self) -> None:
        add_execution(self.snapshot, 101, "2026-08-19T15:45:00Z", conclusion="success")
        self.snapshot["evidence"]["run:101"]["payload"]["headSha"] = "b" * 40
        self.record.update(taskObservation="unavailable", taskState="failed", lifecycle="association_pending")
        pull = self.record["pullRequests"][0]
        pull.update(state="unknown", lastKnownState="merged")
        self.assertEqual("verified", self.followup()["status"])
        original = copy.deepcopy(pull)
        for change in (
            {"mergedAt": None}, {"mergeCommitSha": None},
            {"mergedAt": "2026-08-19T17:00:00Z"}, {"mergedAt": "2026-08-19T15:30:00Z"},
        ):
            with self.subTest(change=change):
                pull.clear()
                pull.update({**original, **change})
                result = self.followup()
                self.assertEqual("unknown", result["status"])
                self.assertEqual("task-1", result["attempts"][0]["taskId"])
                self.assertEqual(201, result["attempts"][0]["pullRequests"][0]["number"])

    def test_descendant_success_requires_an_exact_source_bound_comparison(self) -> None:
        add_execution(self.snapshot, 101, "2026-08-19T15:45:00Z", conclusion="success")
        base, head = "b" * 40, "c" * 40
        self.snapshot["evidence"]["run:101"]["payload"]["headSha"] = head
        self.assertEqual("unknown", self.followup()["status"])
        path = f"/repos/{self.snapshot['repository']}/compare/{base}...{head}"
        response = {
            "url": f"https://api.github.com{path}", "status": "ahead", "behind_by": 0,
            "base_commit": {"sha": base}, "merge_base_commit": {"sha": base},
        }
        client = ScriptedClient({}, {f"{path}?per_page=1": response})
        comparison = observe_commit_comparison(client, self.snapshot["repository"], base, head)
        self.snapshot["commitComparisons"] = [comparison]
        verified = self.followup()
        self.assertEqual("verified", verified["status"])
        self.assertEqual([comparison], verified["verification"]["commitComparisons"])
        comparison.update(status="behind", mergeBaseSha=head, behindBy=1)
        self.assertEqual("awaiting-post-fix-success", self.followup()["status"])
        comparison.update(status="ahead", mergeBaseSha=base, behindBy=0, repository="another/repo")
        self.assertEqual("unknown", self.followup()["status"])

    def test_later_job_failure_keeps_prior_fix_for_reassessment_not_same_cause_proof(self) -> None:
        add_execution(self.snapshot, 101, "2026-08-19T15:45:00Z", conclusion="success")
        self.snapshot["evidence"]["run:101"]["payload"]["headSha"] = "b" * 40
        self.assertEqual("verified", self.followup()["status"])
        add_execution(
            self.snapshot, 102, "2026-08-19T15:50:00Z",
            excerpt="src/Another.cs(1,1): error CS0246: The type could not be found",
        )
        self.snapshot["evidence"]["run:102"]["payload"]["headSha"] = "b" * 40
        self.record["taskState"] = "failed"
        result = self.followup()
        self.assertEqual("reassessment-required", result["status"])
        self.assertEqual(101, result["verification"]["runId"])
        self.assertEqual([102], [failure["runId"] for failure in result["laterFailures"]])
        self.assertEqual("unknown", result["laterFailures"][0]["sameRootCause"])
        self.assertEqual(("assignment:12", "task-1"), (
            result["attempts"][0]["actionId"], result["attempts"][0]["taskId"],
        ))
        self.assertEqual(201, result["attempts"][0]["pullRequests"][0]["number"])
        self.assertTrue(result["requiresNewDecision"])

    def test_a_new_test_failure_still_reassesses_an_original_job_level_repair(self) -> None:
        add_execution(
            self.snapshot, 102, "2026-08-19T15:50:00Z",
            excerpt="Failed Different.Tests.FailingTest [42 ms]",
        )
        self.snapshot["evidence"]["run:102"]["payload"]["headSha"] = "b" * 40
        observations = build_observations(self.snapshot, policy=policy())
        failure = next(item for item in observations["occurrences"] if item["runId"] == 102)
        self.assertEqual("Different.Tests.FailingTest", failure["testName"])
        result = self.followup()
        self.assertEqual("reassessment-required", result["status"])
        self.assertEqual("unknown", result["laterFailures"][0]["sameRootCause"])

    def test_unusable_attempts_require_humans_and_active_work_is_not_recovery(self) -> None:
        original = copy.deepcopy(self.record)
        cases = [
            ("closed_unmerged", "closed-unmerged", "completed", "closed", 4, "human-handoff"),
            ("handoff_required", "unresolved", "completed", "open", 0, "human-handoff"),
            ("handoff_required", "unresolved", "failed", None, None, "human-handoff"),
            ("awaiting_pull_request", "pending", "completed", "open", 4, "work-in-progress"),
            ("running", "pending", "in_progress", None, None, "work-in-progress"),
            ("association_pending", "pending", "failed", "unknown", None, "unknown"),
        ]
        for lifecycle, outcome, task_state, pull_state, changed_files, expected in cases:
            with self.subTest(lifecycle=lifecycle, pull_state=pull_state):
                self.record.clear()
                self.record.update(copy.deepcopy(original))
                self.record.update(
                    lifecycle=lifecycle, attemptOutcome=outcome, taskState=task_state,
                    requiresNewDecision=outcome != "pending",
                    retired=outcome == "closed-unmerged",
                    pullRequests=[] if pull_state is None else [{
                        "number": 201, "databaseId": 101, "globalId": "PR_101",
                        "state": pull_state, "changedFiles": changed_files, "isDraft": True,
                    }],
                )
                result = self.followup()
                self.assertEqual(expected, result["status"])
                self.assertEqual(task_state, result["attempts"][0]["taskState"])

    def test_failed_replacement_keeps_prior_fix_in_human_handoff(self) -> None:
        failed = copy.deepcopy(self.record)
        failed.update(
            actionId="assignment:12:second", taskId="task-2", startedAt="2026-08-19T15:50:00Z",
            taskState="failed", lifecycle="handoff_required", attemptOutcome="unresolved",
            retired=False, pullRequests=[],
        )
        self.snapshot["delegationStatus"]["records"].append(failed)
        result = self.followup()
        self.assertEqual("human-handoff", result["status"])
        self.assertEqual(["task-1", "task-2"], [attempt["taskId"] for attempt in result["attempts"]])
        self.assertEqual(201, result["attempts"][0]["pullRequests"][0]["number"])
        self.assertTrue(result["requiresNewDecision"])

    def test_new_issue_subject_cannot_replace_the_original_affected_job(self) -> None:
        add_execution(self.snapshot, 101, "2026-08-19T15:34:00Z", conclusion="success")
        add_execution(self.snapshot, 102, "2026-08-19T15:41:00Z")
        add_execution(self.snapshot, 103, "2026-08-19T15:45:00Z", conclusion="success")
        other_job = "Tests / Another.Tests (ubuntu-latest)"
        for run_id in (102, 103):
            self.snapshot["evidence"][f"run:{run_id}:attempt:1:job:{run_id * 10}"]["payload"]["name"] = other_job
        self.snapshot["evidence"]["issue:12"]["payload"]["ledger"]["rows"][-1]["job"] = other_job
        self.snapshot["evidence"]["run:103"]["payload"]["headSha"] = "b" * 40
        result = self.followup()
        self.assertEqual("awaiting-post-fix-success", result["status"])
        self.assertEqual("Tests / Aspire.Hosting.Tests (ubuntu-latest)", result["subject"]["job"])

    def test_scope_retries_incomplete_and_future_jobs_cannot_verify_a_repair(self) -> None:
        add_execution(self.snapshot, 101, "2026-08-19T15:45:00Z", conclusion="success")
        self.snapshot["evidence"]["run:101"]["payload"]["headSha"] = "b" * 40
        original = copy.deepcopy(self.snapshot)
        for condition in ("branch", "repository", "pull_request_target", "retry", "skipped", "running", "future", "unavailable"):
            with self.subTest(condition=condition):
                self.snapshot = copy.deepcopy(original)
                run = self.snapshot["evidence"]["run:101"]["payload"]
                job_record = self.snapshot["evidence"]["run:101:attempt:1:job:1010"]
                job = job_record["payload"]
                if condition == "branch":
                    run.update(headBranch="feature", branch="feature")
                elif condition == "repository":
                    run["targetRepository"] = "another/repo"
                elif condition == "pull_request_target":
                    run["event"] = condition
                elif condition == "retry":
                    run["attempt"] = job["attempt"] = 2
                    self.snapshot["evidence"]["run:101:attempt:2:job:1010"] = self.snapshot["evidence"].pop(
                        "run:101:attempt:1:job:1010"
                    )
                elif condition == "skipped":
                    job["conclusion"] = condition
                elif condition == "running":
                    job["status"] = "in_progress"
                elif condition == "future":
                    job.update(startedAt="2026-08-20T15:45:00Z", completedAt="2026-08-20T15:46:00Z")
                else:
                    job_record["availability"] = "unavailable"
                self.assertEqual("awaiting-post-fix-success", self.followup()["status"])
