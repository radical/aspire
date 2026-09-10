from __future__ import annotations

import copy
import re
import unittest

from ci_shepherd.assessment_batches import build_assessment_batches
from ci_shepherd.run_report import render_run_markdown
from tests.test_assessment_batches import issue_case


class RunReportTests(unittest.TestCase):
    def test_report_quotes_execution_fields_without_promoting_claims_to_verified_validation(self) -> None:
        from ci_shepherd.delegation_observer import attach_cloud_outcomes
        from tests.test_cloud_outcomes import outcome_snapshot
        from tests.test_delegation_observer import ScriptedClient

        section = (
            "| Phase | Mode | Commands | OS | Target | Executed per iteration | Attempts | Passed | Failed |\n"
            "| Pre-fix | quarantine-project local | ./repeat.sh -n 20 | Linux | Tests.Flakey | 3 | 20 | 18 | 2 |\n"
            "| Post-fix | quarantine-project CI | reproduce-flaky-tests.yml | Linux | Tests.Flakey | 0 | 20 | 20 | 0 |\n"
            "Claim: all tests passed. CI run: https://github.com/owner/repo/actions/runs/123"
        )
        value = outcome_snapshot(body="### Test execution evidence\n" + section)
        value["delegatedIssueDetails"][0]["labels"] = ["quarantined-test"]
        attach_cloud_outcomes(value, None, ScriptedClient({}))
        report = render_run_markdown(value, self.prepared, self.judgments)
        self.assertIn("<pre>" + section + "</pre>", report)
        self.assertIn("PR body at https://github.com/owner/repo/pull/201 by Copilot", report)
        self.assertIn("Test execution evidence: reported; not independently verified", report)
        self.assertIn("no values or successful validation are inferred", report)
        self.assertIn("zero executed tests is not validation", report)
        self.assertIn("Final-PR QuarantinedTest retention: unknown", report)
        self.assertIn("separate 21-day zero-failure reliability window; use Refs", report)
        self.assertIn("same mode/target/failing OS and all iterations must pass", report)

    def test_green_ci_and_missing_execution_source_leave_flaky_validation_unknown(self) -> None:
        from ci_shepherd.delegation_observer import attach_cloud_outcomes
        from tests.test_cloud_outcomes import outcome_snapshot
        from tests.test_delegation_observer import ScriptedClient

        value = outcome_snapshot(body="All normal CI checks passed; process exited zero.")
        value["delegatedIssueDetails"][0]["labels"] = ["quarantined-test"]
        value["delegationStatus"]["records"][0]["pullRequests"][0]["currentState"] = {
            "checks": {"state": "green"}, "draft": True,
        }
        attach_cloud_outcomes(value, None, ScriptedClient({}))
        report = render_run_markdown(value, self.prepared, self.judgments)
        visible = re.sub(r"<details\b[^>]*>.*?</details>", "", report, flags=re.DOTALL)
        self.assertIn("checks: green", visible)
        self.assertIn("Test execution evidence: unknown / missing", visible)
        self.assertIn("unknown; no explicit Test execution evidence section was captured.", report)
        self.assertIn("skill invocation and final matching-OS CI verification remain unverified", report)

    def test_cloud_outcome_keeps_reported_sources_separate_from_task_pr_and_assessment(self) -> None:
        from ci_shepherd import delegation_observer
        from tests.test_cloud_outcomes import outcome_snapshot
        from tests.test_delegation_observer import ScriptedClient

        value = outcome_snapshot(body="<script>untrusted</script> Need credentials. " + "x" * 4000)
        value["delegatedIssueDetails"][0]["labels"] = ["quarantined-test"]
        record = value["delegationStatus"]["records"][0]
        source = delegation_observer._outcome_pull_source("owner/repo", record["pullRequests"][0], {
            "html_url": "https://github.com/owner/repo/pull/201", "comments": 1,
        })
        delegation_observer.initialize_cloud_outcome(record, {101: source})
        delegation_observer.attach_cloud_outcomes(value, None, ScriptedClient({}, {
            "/repos/owner/repo/issues/201/comments?per_page=5&page=1": [{
                "id": 91, "html_url": "https://github.com/owner/repo/pull/201#issuecomment-91",
                "issue_url": "https://api.github.com/repos/owner/repo/issues/201",
                "user": {"login": "Copilot"}, "body": "### Test execution evidence\nNeed current failure logs.",
                "created_at": "2026-09-02T18:59:00Z", "updated_at": "2026-09-02T18:59:00Z",
            }],
        }))
        report = render_run_markdown(value, self.prepared, self.judgments)
        for expected in (
            "task state: completed", "[PR #201](https://github.com/owner/repo/pull/201) (open); draft: True; changed files: 3",
            "Reported cloud outcome (untrusted)", "attempt assignment:1, task task-1",
            "PR body reported at https://github.com/owner/repo/pull/201 by Copilot",
            "&lt;script&gt;untrusted&lt;/script&gt; Need credentials.",
            "source preview truncated",
            "PR comment reported at https://github.com/owner/repo/pull/201#issuecomment-91 by Copilot",
            "Need current failure logs.", "observed head: " + "a" * 40,
            "Assessed repair outcome:", "Task completion and draft contents are not verified repair.",
            "PR comment at https://github.com/owner/repo/pull/201#issuecomment-91 by Copilot",
            "<pre>Need current failure logs.</pre>",
            "Fields not explicitly reported remain unknown",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, report)

    def test_legacy_cloud_tracking_does_not_invent_an_outcome(self) -> None:
        self.snapshot["delegationStatus"] = {"records": [{
            "issueNumber": 1, "actionId": "assignment:1", "taskId": "task-1",
            "taskState": "completed", "pullRequests": [],
        }]}
        report = render_run_markdown(self.snapshot, self.prepared, self.judgments)
        self.assertIn("task task-1: outcome evidence unavailable", report)
        self.assertIn("Task completion and draft contents are not verified repair.", report)

    def test_report_shows_route_and_priority_from_frozen_repair_facts(self) -> None:
        self.prepared["issues"][0]["repairEvidence"] = {
            "current": True, "category": "blocking-build", "recurrent": True,
        }
        recommendation = self.judgments["issues"][0]["recommendations"][0]
        for disposition, route in (
            ("delegate-copilot", "cloud investigate-and-fix"),
            ("investigate", "local classification"),
        ):
            with self.subTest(disposition=disposition):
                recommendation["disposition"] = disposition
                report = render_run_markdown(self.snapshot, self.prepared, self.judgments)
                self.assertIn(f"route: {route}; scheduling priority: current-workflow-break", report)
                self.assertIn("matching recurrence: True", report)

    def test_report_separates_external_lifecycle_authority_from_diagnosis(self) -> None:
        self.prepared["issues"][0]["lifecycleAuthority"] = {
            "closeIssue": "external-watchdog",
            "reopenIssue": "external-watchdog",
        }
        self.judgments["issues"][0]["recommendations"][0].update(
            disposition="investigate",
            summary="Identify the current failed execution and repair subject.",
        )

        report = render_run_markdown(self.snapshot, self.prepared, self.judgments)

        self.assertIn(
            "lifecycle authority: closeIssue=external-watchdog, "
            "reopenIssue=external-watchdog",
            report,
        )
        self.assertIn("Identify the current failed execution and repair subject.", report)

    def test_assessment_workload_uses_packet_manifest_not_receipt_or_token_counts(self) -> None:
        manifest, _ = build_assessment_batches(
            [issue_case(1)], snapshot_id=self.prepared["snapshotId"], source_fingerprints={},
        )
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments, assessment_manifest=manifest,
        )
        byte_count = sum(group["byteCount"] for group in manifest["workerGroups"])
        self.assertIn("| Current | 1 | 1 | 1 | " + str(byte_count) + " |", report)
        self.assertIn("assessment packets, not package restore", report)
        self.assertIn("not token counts or billed usage", report)
        self.assertIn("0 issues / 0 PRs with assessment acknowledgements", report)

    def test_assessment_workload_rejects_stale_or_inconsistent_manifests(self) -> None:
        for change in ("snapshot", "bytes", "cases", "duplicate-case", "batch-membership", "packet-membership"):
            manifest, _ = build_assessment_batches(
                [issue_case(1)], snapshot_id=self.prepared["snapshotId"], source_fingerprints={},
            )
            if change == "snapshot":
                manifest["snapshotId"] = "snapshot:other"
            elif change == "bytes":
                manifest["workerGroups"][0]["byteCount"] += 1
            elif change == "cases":
                manifest["caseCount"] = 999
            elif change == "duplicate-case":
                manifest["workerGroups"][0]["caseIds"].append("issue:1")
            elif change == "batch-membership":
                manifest["workerGroups"][0]["batchIds"][0] = "batch:unknown"
            else:
                manifest["workerGroups"][0]["packetFiles"][0] = "unknown.json"
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "Assessment workload"):
                render_run_markdown(
                    self.snapshot, self.prepared, self.judgments, assessment_manifest=manifest,
                )

    def test_missing_assessment_workload_is_unknown_not_zero(self) -> None:
        report = render_run_markdown(self.snapshot, self.prepared, self.judgments)
        self.assertIn("Unknown: no assessment packet manifest supplied.", report)
        self.assertIn("Unknown: current worktree and lifecycle capacity inventory was not supplied.", report)

    def test_local_capacity_shows_unconfirmed_owner_outside_current_source_and_selection(self) -> None:
        capacity = {
            "repository": self.snapshot["repository"], "maxConcurrent": 3,
            "occupiedSlots": 1, "availableSlots": 2,
            "reservations": [{
                "issueNumber": 999, "sessionId": "older-source-session",
                "launchState": "termination-unconfirmed", "sourceRevision": "a" * 40,
            }],
        }
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            review_selection={"selectedIssues": [], "excludedIssues": []},
            investigation_capacity=capacity,
        )
        self.assertIn("1 occupied of 3 slots; 2 available. Older source pins remain included.", report)
        self.assertIn("| 999 | older-source-session | termination-unconfirmed | " + "a" * 40 + " |", report)
        self.assertIn("A deadline or missing worker response is not proof", report)
        with self.assertRaisesRegex(ValueError, "Investigation capacity"):
            render_run_markdown(
                self.snapshot, self.prepared, self.judgments,
                investigation_capacity={**capacity, "occupiedSlots": 0},
            )

    def test_one_shot_preparation_and_dispatch_do_not_claim_performed_work(self) -> None:
        for status, execution, label in (
            ("prepared", "not-dispatched", "Investigation prepared; not dispatched"),
            ("dispatching", "unknown", "Investigation dispatch unconfirmed"),
            ("failed", "not-launched", "Investigation not launched; not performed"),
            ("abandoned", "unknown", "Investigation stopped; execution unknown"),
        ):
            with self.subTest(status=status):
                report = render_run_markdown(
                    self.snapshot, self.prepared, self.judgments,
                    investigation_plan={
                        "requests": [], "pendingInvestigations": [{"issueNumber": 1, "investigationId": "inv:one"}],
                    },
                    investigation_sessions=[{
                        "repository": self.snapshot["repository"], "issueNumber": 1, "investigationId": "inv:one",
                        "launchMode": "one-shot", "attemptId": "logical-attempt", "sessionId": None,
                        "runtimeSessionId": None, "status": status, "executionState": execution,
                        "recordedAt": "2026-09-05T12:01:00Z", "failureReason": "Launcher rejected the call.",
                    }],
                )
                self.assertIn(label, report)
                self.assertIn("runtime session: unknown", report)
                self.assertIn("No performed or reused investigations recorded.", report)

    def test_one_shot_result_does_not_invent_duration_from_preparation_or_null_sessions(self) -> None:
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            investigation_plan={"requests": [{"issueNumber": 1, "investigationId": "inv:one"}]},
            investigation_results=[{
                "issueNumber": 1, "investigationId": "inv:one", "outcome": "inconclusive",
                "summary": "The bounded worker returned.", "recordedAt": "2026-09-05T12:02:00Z",
                "launchMode": "one-shot", "attemptId": "actual-attempt", "sessionId": None,
                "runtimeSessionId": None,
            }],
            investigation_sessions=[
                {"investigationId": "inv:one", "sessionId": None, "status": "started",
                 "recordedAt": "2026-09-05T12:00:00Z"},
                {"investigationId": "inv:one", "sessionId": None, "status": "completed",
                 "recordedAt": "2026-09-05T12:01:00Z"},
            ],
        )
        self.assertIn("Investigation completed; duration: unknown", report)
        self.assertIn("one-shot; runtime session: unknown", report)

    def test_observed_worker_duration_is_separate_from_result_acceptance_and_runtime_identity(self) -> None:
        result = {
            "issueNumber": 1, "repository": "owner/repo", "investigationId": "inv:one",
            "launchMode": "one-shot", "attemptId": "attempt:one", "sessionId": None,
            "outcome": "needs-evidence", "summary": "Missing failure logs.",
            "recordedAt": "2026-09-05T12:05:00Z", "acceptedResultAt": "2026-09-05T12:05:00Z",
            "runtimeObservation": {
                "runtimeSessionId": None, "workerStartedAt": "2026-09-05T12:01:00Z",
                "workerCompletedAt": "2026-09-05T12:01:35Z",
                "observationEvidence": "Observed launcher start and end.",
            },
        }
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            investigation_plan={"requests": [{"issueNumber": 1, "investigationId": "inv:one"}]},
            investigation_results=[result],
        )
        self.assertIn("duration: 35s", report)
        self.assertIn("one-shot; runtime session: unknown", report)
        self.assertIn("result accepted: 2026-09-05T12:05:00Z", report)
        self.assertIn("timing basis: Observed launcher start and end.", report)
        result["runtimeObservation"]["workerCompletedAt"] = None
        unknown = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            investigation_plan={"requests": [{"issueNumber": 1, "investigationId": "inv:one"}]},
            investigation_results=[result],
        )
        self.assertIn("Investigation completed; duration: unknown", unknown)

    def test_investigation_report_shows_work_and_observed_command_output(self) -> None:
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            investigation_plan={"requests": [{"issueNumber": 1, "investigationId": "inv:one"}]},
            investigation_results=[{
                "issueNumber": 1, "investigationId": "inv:one", "outcome": "inconclusive",
                "summary": "The source needs a targeted reproduction.",
                "recordedAt": "2026-09-05T12:01:00Z", "sourceRevision": "a" * 40,
                "workLog": [
                    {"kind": "source", "path": "example.py", "startLine": 1, "endLine": 2,
                     "finding": "The function returns a constant."},
                    {"kind": "command", "argv": ["python3", "example.py"], "exitCode": 0,
                     "output": "Observed output: 1", "finding": "The approved reproduction completed."},
                ],
            }],
        )
        for text in ("example.py:1-2", "The function returns a constant.", "exit 0", "Observed output: 1"):
            self.assertIn(text, report)
        self.assertIn("Worker-reported work", report)

    def test_frozen_launch_blocker_is_not_reported_as_running_or_only_planned(self) -> None:
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            investigation_plan={"requests": [{"issueNumber": 1, "investigationId": "inv:one"}]},
            invocation_window={"investigationBlockers": [{
                "investigationId": "inv:one", "reason": "Worker checkout could not be provisioned.",
            }]},
        )
        self.assertIn("Investigation blocked before start", report)
        self.assertIn("Worker checkout could not be provisioned.", report)

    def test_completed_receipts_distinguish_acknowledgement_from_selection(self) -> None:
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            review_selection={"snapshotId": "snapshot:current", "selected": [{"issueNumber": 1}]},
            assessment_coverage={
                "snapshotId": "snapshot:current", "status": "complete",
                "completedIssueNumbers": [1], "completedPullRequestNumbers": [],
            },
        )
        self.assertIn("1 issues / 0 PRs with assessment acknowledgements", report)
        self.assertIn("Existing; assessment acknowledged", report)

    def test_stale_or_unselected_completion_receipts_cannot_claim_coverage(self) -> None:
        for snapshot_id, completed in (("snapshot:other", [1]), ("snapshot:current", [2])):
            with self.subTest(snapshot_id=snapshot_id, completed=completed), self.assertRaises(ValueError):
                render_run_markdown(
                    self.snapshot, self.prepared, self.judgments,
                    review_selection={"snapshotId": "snapshot:current", "selected": [{"issueNumber": 1}]},
                    assessment_coverage={
                        "snapshotId": snapshot_id, "status": "complete",
                        "completedIssueNumbers": completed, "completedPullRequestNumbers": [],
                    },
                )

    def test_reselected_case_cannot_reuse_pre_expansion_completion(self) -> None:
        selection = {"snapshotId": "snapshot:current", "selected": [{"issueNumber": 1}]}
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            review_selection=selection, pre_expansion_review_selection=selection,
            pre_expansion_assessment_coverage={
                "snapshotId": "snapshot:current", "status": "complete",
                "completedIssueNumbers": [1], "completedPullRequestNumbers": [],
            },
        )
        self.assertIn("0 issues / 0 PRs with assessment acknowledgements", report)
        self.assertIn("selected; completion unverified", report)

    def test_selection_without_receipts_is_not_claimed_as_completed_review(self) -> None:
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            review_selection={"selected": [{"issueNumber": 1}]},
        )
        self.assertIn("selected; completion unverified", report)
        self.assertIn("0 issues / 0 PRs with assessment acknowledgements", report)

    def test_retired_quarantine_fix_reports_merge_separately_from_failed_task_and_open_issue(self) -> None:
        self.snapshot["issues"][0]["labels"] = ["quarantined-test"]
        self.snapshot["delegationStatus"] = {"records": [{
            "issueNumber": 1, "taskId": "task-1", "taskState": "failed",
            "lifecycle": "completed", "attemptOutcome": "merged", "retired": True,
            "requiresHuman": False, "requiresNewDecision": True, "issueOpen": True,
            "copilotAssigned": False,
            "pullRequests": [{"number": 2, "state": "merged", "changedFiles": 4}],
        }]}
        report = render_run_markdown(self.snapshot, self.prepared, self.judgments)
        for expected in ("task state: failed", "[PR #2](https://github.com/owner/repo/pull/2) (merged)", "attempt: merged",
                         "issue remains open", "quarantine reliability", "new decision required"):
            self.assertIn(expected, report)

    def test_expansion_rounds_count_each_reviewed_item_once(self) -> None:
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            review_selection={"selected": [{"issueNumber": 2}, {"issueNumber": 3}]},
            pre_expansion_review_selection={"selected": [{"issueNumber": 1}, {"issueNumber": 2}]},
            pull_request_review={"tasks": []},
            pre_expansion_pull_request_review={"tasks": [{
                "target": {"kind": "pull-request", "number": 4},
                "title": "Reviewed before expansion",
                "defaultJudgment": {"disposition": "no-action", "summary": "Checks green."},
            }]},
        )
        self.assertIn("3 issues selected for review", report)
        self.assertIn("1 PRs selected for review", report)
        self.assertIn("Checks green.", report)
        self.assertEqual(1, report.count("[#4]"))

    def test_non_due_copilot_handoff_stays_visible_without_a_new_review(self) -> None:
        self.snapshot["issues"] = []
        self.snapshot["delegatedIssueDetails"] = [{
            "number": 1, "title": "Quarantined test", "state": "open",
            "labels": ["quarantined-test"], "assignees": ["maintainer"],
        }]
        self.snapshot["delegatedPullRequestDetails"] = [{
            "number": 2, "title": "Investigate the test", "state": "open",
        }]
        self.snapshot["delegationStatus"] = {"records": [{
            "issueNumber": 1, "taskId": "task-1", "lifecycle": "handoff_required",
            "humanAssigned": True,
            "nextWakeup": {"evaluateAt": "2026-09-11T12:00:00Z", "reason": "human-stale-progress"},
            "pullRequests": [{"number": 2, "state": "open", "isDraft": True}],
        }]}
        report = render_run_markdown(
            self.snapshot, {"snapshotId": "snapshot:current", "issues": []}, {"issues": []},
            review_selection={"selected": []}, pull_request_review={"tasks": []},
        )
        visible = re.sub(r"<details\b[^>]*>.*?</details>", "", report, flags=re.DOTALL)
        for expected in ("task-1", "handoff_required", "2026-09-11T12:00:00Z",
                         "Owner: maintainer", "Investigate the test", "draft: yes",
                         "0 issues selected for review", "0 PRs selected for review"):
            self.assertIn(expected, visible)
        self.assertEqual(1, report.count("[#1]"))
        self.assertEqual(1, report.count("[#2]"))

    def test_carried_pr_uses_current_inventory_evidence_without_claiming_a_review(self) -> None:
        self.snapshot["pullRequests"] = [{
            "number": 2, "title": "Fix the failure", "labels": ["NO-MERGE"],
            "assignees": ["maintainer"],
        }]
        self.snapshot["evidence"] = {"pr:2": {"payload": {"currentState": {
            "draft": True, "checks": {"state": "pending"},
            "review": {"decision": "review-required"},
        }}}}
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            pull_request_review={"tasks": []},
            pull_request_judgments={"pullRequests": [{"pullRequestNumber": 2, "disposition": "ping-human"}]},
        )
        visible = re.sub(r"<details\b[^>]*>.*?</details>", "", report, flags=re.DOTALL)
        for expected in ("Fix the failure", "checks: pending", "draft: yes", "NO-MERGE", "Owner: maintainer"):
            self.assertIn(expected, visible)
        self.assertIn("0 PRs selected for review", report)

    def setUp(self) -> None:
        self.snapshot = {
            "repository": "owner/repo",
            "collectedAt": "2026-09-05T12:00:00Z",
            "issues": [{"number": 1, "title": "Failure", "state": "open"}],
        }
        self.prepared = {"snapshotId": "snapshot:current", "issues": [{"issueNumber": 1}]}
        self.judgments = {
            "issues": [{
                "issueNumber": 1,
                "category": "unknown",
                "recommendations": [{
                    "disposition": "no-action",
                    "summary": "No further shepherd action.",
                    "evidenceIds": ["issue:1"],
                    "reassessWhen": "New diagnostics.",
                }],
            }],
        }

    def test_decision_is_not_an_executed_action_and_green_is_not_merge_ready(self) -> None:
        report = render_run_markdown(
            self.snapshot,
            self.prepared,
            self.judgments,
            pull_request_review={"tasks": [{
                "target": {"kind": "pull-request", "number": 2},
                "title": "Update manifests",
                "labels": ["NO-MERGE"],
                "currentState": {
                    "draft": True,
                    "checks": {"state": "green"},
                    "review": {"decision": "review-required"},
                    "mergeable": True,
                    "mergeableState": "blocked",
                },
            }]},
            pull_request_judgments={"pullRequests": [{
                "pullRequestNumber": 2,
                "disposition": "no-action",
                "summary": "Checks green.",
            }]},
            action_events=[{
                "repository": "owner/repo",
                "snapshotId": "snapshot:other",
                "eventType": "terminal",
                "outcome": "executed",
                "operation": "close-issue",
                "target": {"kind": "issue", "number": 1},
            }],
        )
        self.assertIn("0 executed effects", report)
        self.assertIn("⚪ No action", report)
        self.assertNotIn("No executed action recorded", report)
        self.assertNotIn("**Executed action:**", report)
        visible = re.sub(r"<details\b[^>]*>.*?</details>", "", report, flags=re.DOTALL)
        for state in ("checks: green", "draft: yes", "review: review-required", "NO-MERGE", "mergeability: blocked"):
            self.assertIn(state, visible)
        self.assertIn("Green checks are not merge readiness", report)

    def test_only_terminal_executed_events_count_once(self) -> None:
        event = {
            "repository": "owner/repo",
            "snapshotId": "snapshot:current",
            "actionId": "action:1",
            "eventType": "terminal",
            "outcome": "executed",
            "operation": "close-issue",
            "target": {"kind": "issue", "number": 1},
        }
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            action_events=[event, dict(event), {**event, "actionId": "action:2", "outcome": "blocked"}],
        )
        self.assertIn("1 executed effects", report)
        visible = re.sub(r"<details\b[^>]*>.*?</details>", "", report, flags=re.DOTALL)
        self.assertIn("✅ Executed: close-issue", visible)
        self.assertIn("⚪ No action", report)
        self.assertEqual(report.count("[#1]"), 1)

    def test_reconciled_effect_replaces_the_indeterminate_blocker(self) -> None:
        terminal = {
            "repository": "owner/repo", "snapshotId": "snapshot:current",
            "actionId": "action:1", "eventType": "terminal",
            "operation": "close-issue", "target": {"kind": "issue", "number": 1},
        }
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            action_events=[
                {**terminal, "outcome": "indeterminate", "recordedAt": "2026-09-05T12:01:00Z"},
                {**terminal, "outcome": "executed", "recordedAt": "2026-09-05T12:02:00Z"},
            ],
            as_of="2026-09-05T12:03:00Z",
        )
        self.assertIn("1 executed effects", report)
        self.assertIn("**Blocker:** None recorded", report)
        self.assertIn("indeterminate (2026-09-05T12:01:00Z) → executed (2026-09-05T12:02:00Z) (reconciled)", report)

    def test_grouped_coverage_investigation_conclusion_and_source_readiness(self) -> None:
        self.snapshot["issues"] = [
            {"number": 1, "title": "Test failure", "labels": ["failing-test"], "assignees": [{"login": "assigned-owner"}]},
            {"number": 3, "title": "Workflow outage", "producer": "ci-failure-cause"},
            {"number": 4, "title": "Unclassified issue", "updatedAt": "2026-09-05T11:59:00Z"},
            {"number": 5, "title": "Unchanged"},
        ]
        self.prepared["issues"][0].update({
            "sourceState": "ActiveIssue-disabled",
            "fixReadiness": "requires-local-reproduction",
            "lastMatchingFailureAt": "2026-09-01T00:00:00Z",
        })
        result = {
            "repository": "owner/repo",
            "issueNumber": 1,
            "investigationId": "investigation:1",
            "sessionId": "worker:1",
            "outcome": "needs-evidence",
            "summary": "No diagnostic stack was supplied.",
            "missingEvidence": ["diagnostic-stack"],
            "reassessWhen": "Failure diagnostics arrive.",
            "recordedAt": "2026-09-05T12:02:00Z",
        }
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            review_selection={"selected": [
                {"issueNumber": 1, "previousDisposition": "watch"},
                {"issueNumber": 3, "changeClass": "new"},
                {"issueNumber": 4},
            ]},
            investigation_plan={"requests": [{"issueNumber": 1, "investigationId": "investigation:1"}]},
            investigation_results=[result],
            investigation_sessions=[
                {**result, "status": "started", "recordedAt": "2026-09-05T12:00:00Z"},
                {**result, "status": "completed", "recordedAt": "2026-09-05T12:02:00Z"},
            ],
        )
        for expected in (
            "Other issues", "Flaky / failing test issues", "Workflow / CI incidents",
            "duration: 2m", "conclusion: needs-evidence", "No diagnostic stack was supplied.",
            "diagnostic-stack", "Failure diagnostics arrive.", "sourceState: ActiveIssue-disabled",
            "fixReadiness: requires-local-reproduction", "Owner: assigned-owner",
            "prior: watch", "🆕 New", "last matching failure: unknown",
            "1 unchanged / excluded inventory items",
        ):
            self.assertIn(expected, report)
        for number in (1, 3, 4):
            self.assertEqual(report.count(f"[#{number}]"), 1)
        self.assertNotIn("2026-09-05T11:59:00Z", report)

    def test_reused_results_are_not_new_work_and_missing_timing_stays_unknown(self) -> None:
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            investigation_plan={"reusedInvestigationIds": ["investigation:old"]},
            investigation_results=[{
                "repository": "owner/repo", "issueNumber": 1,
                "investigationId": "investigation:old",
                "outcome": "fixable", "summary": "A fix is possible, not implemented.",
            }],
        )
        self.assertIn("♻ Reused result; duration: unknown", report)
        self.assertIn("A fix is possible, not implemented.", report)
        self.assertIn("0 executed effects", report)

    def test_unselected_carried_judgments_collapse_but_real_work_does_not(self) -> None:
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            review_selection={"selected": [], "omitted": [{"issueNumber": 1, "reason": "unchanged-stable"}]},
        )
        self.assertIn("1 unchanged / excluded inventory items", report)
        self.assertNotIn("[#1]", report)

    def test_recorded_effect_changes_current_state_not_the_prior_snapshot(self) -> None:
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            action_events=[{
                "repository": "owner/repo", "snapshotId": "snapshot:current",
                "eventType": "terminal", "outcome": "executed",
                "operation": "close-issue", "actionId": "action:close",
                "target": {"kind": "issue", "number": 1},
                "result": {"issueState": "closed"},
            }],
        )
        self.assertIn("closed (recorded action)", report)
        self.assertEqual(self.snapshot["issues"][0]["state"], "open")

    def test_canonical_test_maintenance_and_fix_candidate_are_not_a_fix(self) -> None:
        self.prepared["issues"][0].update({
            "testMaintenance": {"state": "quarantined", "evidenceComplete": True, "evidenceIds": ["source:tests/Test.cs"]},
            "machineActionability": {"status": "verified", "kind": "code-change"},
        })
        report = render_run_markdown(self.snapshot, self.prepared, self.judgments)
        self.assertIn("quarantine source: quarantined; evidence complete: True", report)
        self.assertIn("fix handoff: verified (not an executed fix)", report)
        self.assertIn("source:tests/Test.cs", report)

    def test_quarantine_label_never_substitutes_for_recorded_source_state(self) -> None:
        self.snapshot["issues"][0]["labels"] = ["quarantined-test"]
        self.prepared["issues"][0]["alreadyQuarantined"] = True
        cases = [
            (None, "open; last matching failure: unknown"),
            ("quarantine-mismatch", "open; quarantine source: quarantine-mismatch; evidence complete: False; last matching failure: unknown"),
            ("unverified-quarantine", "open; quarantine source: unverified-quarantine; evidence complete: False; last matching failure: unknown"),
        ]
        for state, expected in cases:
            with self.subTest(source_state=state):
                if state is not None:
                    self.prepared["issues"][0]["testMaintenance"] = {
                        "state": state, "tests": [], "evidenceIds": [], "evidenceComplete": False,
                    }
                report = render_run_markdown(self.snapshot, self.prepared, self.judgments)
                row = next(line for line in report.splitlines() if line.startswith("| [#1]"))
                self.assertEqual(row.split(" | ")[3], "No action planned")
                self.assertIn(f"**Current state:** {expected}", report)

    def test_started_session_is_running_even_before_plan_is_refreshed(self) -> None:
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            investigation_plan={"requests": [{"issueNumber": 1, "investigationId": "inv:one"}]},
            investigation_sessions=[{
                "repository": "owner/repo", "issueNumber": 1, "investigationId": "inv:one",
                "sessionId": "worker:one", "status": "started",
                "recordedAt": "2026-09-05T12:00:00Z",
            }],
        )
        self.assertIn("🔄 Investigation running", report)
        self.assertIn("investigator session: worker:one", report)

    def test_usage_cannot_be_silently_joined_to_a_different_run(self) -> None:
        with self.assertRaisesRegex(ValueError, "usage run"):
            render_run_markdown(
                self.snapshot, self.prepared, self.judgments,
                run_id="run:one", usage={"runId": "run:other"},
            )

    def test_as_of_excludes_later_effects(self) -> None:
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            as_of="2026-09-05T12:10:00Z",
            action_events=[{
                "repository": "owner/repo", "snapshotId": "snapshot:current",
                "eventType": "terminal", "outcome": "executed",
                "operation": "close-issue", "actionId": "action:later",
                "target": {"kind": "issue", "number": 1},
                "recordedAt": "2026-09-05T12:11:00Z",
            }],
        )
        self.assertIn("0 executed effects", report)

    def test_invocation_duration_is_not_the_sum_of_recorded_cycle_windows(self) -> None:
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            invocation_window={
                "scope": "whole-invocation",
                "startedAt": "2026-09-05T12:00:00Z",
                "completedAt": "2026-09-05T12:10:00Z",
            },
            recording_windows=[
                {"label": "Primary cycle", "startedAt": "2026-09-05T12:00:30Z", "completedAt": "2026-09-05T12:05:00Z"},
                {"label": "Collection", "startedAt": "2026-09-05T12:01:00Z", "completedAt": "2026-09-05T12:02:00Z"},
                {"label": "Immediate follow-up", "startedAt": "2026-09-05T12:05:00Z", "completedAt": "2026-09-05T12:08:00Z"},
            ],
        )
        for expected in (
            "**Whole invocation duration:** unknown",
            "Recorded whole-invocation window: 10m",
            "Primary cycle: 4m30s", "Collection: 1m", "Immediate follow-up: 3m",
            "Recording windows may overlap",
        ):
            self.assertIn(expected, report)

    def test_collection_window_does_not_establish_missing_invocation_boundaries(self) -> None:
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            as_of="2026-09-05T12:10:00Z",
            recording_windows=[{
                "label": "Collection", "startedAt": "2026-09-05T12:00:00Z",
                "completedAt": "2026-09-05T12:01:00Z",
            }],
        )
        self.assertIn("**Whole invocation duration:** unknown", report)
        self.assertIn("Collection: 1m", report)

    def test_invocation_usage_identity_is_distinct_from_cycle_action_identity(self) -> None:
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            run_id="invocation:one", usage={"runId": "invocation:one"},
            action_events=[{
                "repository": "owner/repo", "snapshotId": "snapshot:current",
                "runId": "cycle:current", "eventType": "terminal", "outcome": "executed",
                "operation": "create-comment", "actionId": "action:one",
                "target": {"kind": "issue", "number": 1},
            }],
        )
        self.assertIn("1 executed effects", report)

    def test_reported_failure_date_retains_its_precision_and_basis(self) -> None:
        self.prepared["issues"][0]["ledger"] = {
            "schemaRecognized": True, "complete": True,
            "rows": [{"date": "2026-09-03", "sourceRun": 10}],
        }
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments, as_of="2026-09-05T12:00:00Z",
        )
        self.assertIn("last matching failure: 2026-09-03", report)
        self.assertIn("age: 2d (calendar); basis: reported producer ledger", report)

    def test_recorded_occurrence_age_ignores_issue_updates_and_unrelated_runs(self) -> None:
        self.snapshot["issues"][0]["updatedAt"] = "2026-09-05T12:00:00Z"
        self.prepared["issues"][0]["recovery"] = {"subjects": [{
            "occurrence": {
                "issueNumber": 1, "observedAt": "2026-09-04T12:00:00Z",
                "evidenceIds": ["run:10"],
            },
        }]}
        self.prepared["issues"][0]["evidenceBundle"] = [{
            "kind": "workflow-run", "id": "run:20", "availability": "available",
            "payload": {"runId": 20, "createdAt": "2026-09-05T12:00:00Z"},
        }]
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments, as_of="2026-09-05T12:00:00Z",
        )
        self.assertIn("age: 1d; basis: recorded failure occurrence", report)

    def test_run_expense_shows_excluded_workers_and_unknown_credit_cost(self) -> None:
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments, run_id="invocation:one",
            usage={
                "runId": "invocation:one", "sessionCount": 2,
                "excludedReusedSessions": 5, "excludedSkippedSessions": 3,
                "excludedIncludedSessions": 1,
                "metrics": {"aiCredits": {"value": None, "coveredSessions": 0}},
            },
        )
        self.assertIn("New-cost roster: 2 sessions", report)
        self.assertIn("5 reused, 3 skipped, 1 already included in parent totals", report)
        self.assertIn("| AI credits | unknown | 0 / 2 |", report)

    def test_pr_progress_uses_recorded_basis_and_observation_precision(self) -> None:
        self.snapshot["pullRequests"] = [{
            "number": 2, "updatedAt": "2026-09-05T12:00:00Z",
            "meaningfulProgress": {
                "status": "observed", "at": "2026-09-04T12:00:00Z",
                "basis": "head changed between snapshots", "evidenceIds": ["pr:2:head-change"],
                "precision": "observed-change",
            },
        }]
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            pull_request_review={"tasks": [{"target": {"kind": "pull-request", "number": 2}}]},
            as_of="2026-09-05T12:00:00Z",
        )
        self.assertIn("last meaningful change: 2026-09-04T12:00:00Z; age: 1d", report)
        self.assertIn("basis: head changed between snapshots; precision: observed-change", report)
        self.assertIn("pr:2:head-change", report)

    def test_unknown_pr_progress_does_not_fall_back_to_update_or_legacy_age(self) -> None:
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            pull_request_review={"tasks": [{
                "target": {"kind": "pull-request", "number": 2},
                "updatedAt": "2026-09-05T12:00:00Z",
                "lastMeaningfulChangeAt": "2026-09-05T12:00:00Z",
                "meaningfulProgress": {"status": "unknown", "at": None, "basis": None, "precision": None, "evidenceIds": []},
            }]},
            as_of="2026-09-05T12:00:00Z",
        )
        pr_row = next(line for line in report.splitlines() if line.startswith("| [#2]"))
        self.assertEqual(pr_row.split("last meaningful change: ")[1].split(" | ")[0], "unknown")

    def test_owner_held_window_does_not_make_unrecorded_setup_zero(self) -> None:
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            invocation_window={
                "scope": "owner-held", "startedAt": "2026-09-05T12:00:00Z",
                "completedAt": "2026-09-05T12:10:00Z",
            },
        )
        self.assertIn("**Whole invocation duration:** unknown", report)
        self.assertIn("Recorded owner-held window: 10m", report)
        self.assertIn("Setup/tail outside the recorded window: unknown, not zero", report)

    def test_inferred_and_summed_intervals_keep_their_measurement_basis(self) -> None:
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            recording_windows=[
                {"label": "Model-facing gaps", "durationSeconds": 61, "measurement": "inferred", "basis": "Between artifact timestamps; N=1"},
                {"label": "Collection and ownership", "durationSeconds": 120, "measurement": "summed-recorded-windows", "basis": "Includes local work; not pure network latency"},
            ],
        )
        self.assertIn("Model-facing gaps: 1m1s; measurement: inferred", report)
        self.assertIn("basis: Between artifact timestamps; N=1", report)
        self.assertIn("Collection and ownership: 2m; measurement: summed-recorded-windows", report)
        self.assertIn("not pure network latency", report)

    def test_investigations_are_prominent_and_group_details_are_folded(self) -> None:
        self.snapshot["issues"] = [
            {"number": number, "title": f"Failure {number}", "state": "open"}
            for number in range(1, 8)
        ]
        long_conclusion = "Missing diagnostic stack. " + "Detailed evidence assessment. " * 30
        results = [{
            "repository": "owner/repo", "issueNumber": number,
            "investigationId": f"inv:{number}", "sessionId": f"worker-private-session-{number}",
            "outcome": "needs-evidence", "summary": long_conclusion,
            "missingEvidence": ["diagnostic stack"], "reassessWhen": "Diagnostics arrive.",
            "recordedAt": "2026-09-05T12:01:00Z",
        } for number in range(2, 7)]
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            investigation_plan={
                "requests": [{"issueNumber": number, "investigationId": f"inv:{number}"} for number in range(2, 7)],
                "deferredRequests": [{"issueNumber": 7, "investigationId": "inv:7", "reason": "budget"}],
            },
            investigation_results=results,
            investigation_sessions=[
                {**result, "status": status, "recordedAt": at}
                for result in results
                for status, at in [("started", "2026-09-05T12:00:00Z"), ("completed", "2026-09-05T12:01:00Z")]
            ],
            run_id="invocation:one",
            usage={"runId": "invocation:one", "sessionCount": 1, "metrics": {
                "totalNanoAiu": {"value": 1234, "coveredSessions": 1},
            }},
        )
        self.assertLess(report.index("## Acted this run"), report.index("## Investigations this run"))
        self.assertLess(report.index("## No action/closed"), report.index("## Usage"))
        visible = re.sub(r"<details\b[^>]*>.*?</details>", "", report, flags=re.DOTALL)
        overview = visible.split("## Investigations this run\n", 1)[1].split("\n## ", 1)[0]
        rows = [line for line in overview.splitlines() if line.startswith("| [Issue #")]
        self.assertEqual(len(rows), 5)
        self.assertTrue(all("Waiting for evidence" in row and "Evidence review finished" in row and "1m" in row and "needs-evidence" in row for row in rows))
        self.assertIn("5 evidence reviews finished", report)
        self.assertIn("<summary>1 deferred / not-started investigations</summary>", report)
        self.assertNotIn(long_conclusion, visible)
        self.assertNotIn("worker-private-session-", visible)
        self.assertIn(long_conclusion.rstrip(), report)
        self.assertIn("worker-private-session-2", report)
        self.assertIn("| Provider cost (nano-AI units) | 1234 |", visible)
        for line in visible.splitlines():
            if line.startswith("| [#"):
                self.assertEqual(len(line.strip("| ").split(" | ")), 7)
        for number in range(1, 8):
            self.assertEqual(report.count(f"[#{number}]"), 1)

    def test_retained_pr_override_is_carried_not_reviewed_or_excluded_twice(self) -> None:
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            pull_request_review={
                "tasks": [],
                "excluded": [
                    {"number": 23, "reason": "unchanged-stable"},
                    {"number": 24, "reason": "unchanged-stable"},
                ],
            },
            pull_request_judgments={"pullRequests": [{
                "pullRequestNumber": 23, "disposition": "ping-human",
                "summary": "Retained maintainer decision.",
                "humanEscalation": {"routingHint": "maintainer"},
            }]},
        )
        self.assertIn("0 PRs selected for review", report)
        row = next(line for line in report.splitlines() if line.startswith("| [#23]"))
        self.assertEqual(row.split(" | ")[1], "Existing; carried assessment; prior: unknown")
        self.assertIn("👤 Human input", row)
        self.assertEqual(report.count("[#23]"), 1)
        self.assertIn(
            "<summary>1 unchanged / excluded inventory items</summary>\n\nPR #24 (unchanged-stable)",
            report,
        )

    def test_outcome_sections_precede_diagnostics_and_capacity_has_a_concrete_retry(self) -> None:
        self.snapshot["issues"] = [
            {"number": number, "title": f"Failure {number}", "state": "open"}
            for number in range(1, 32)
        ]
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            investigation_plan={"deferredRequests": [
                {"issueNumber": number, "investigationId": f"inv:{number}", "reason": "per-cycle-investigation-budget"}
                for number in range(2, 32)
            ]},
        )
        self.assertEqual([
            "Acted this run", "Delegated to Copilot awaiting task/PR", "Human action required",
            "Blocked on evidence", "Watching", "Not reached due tool capacity", "No action/closed",
        ], re.findall(r"^## (.+)$", report, re.MULTILINE)[:7])
        capacity = report.split("## Not reached due tool capacity\n", 1)[1].split("## No action/closed", 1)[0]
        self.assertIn("<summary>30 capacity-deferred items — not attempted</summary>", capacity)
        self.assertIn("Next invocation with a fresh per-cycle investigation budget and an available worker slot.", capacity)
        self.assertIn("per-cycle-investigation-budget", capacity)
        self.assertIn("| Subject kind |", report)
        self.assertIn("Owner: unassigned", report)
        self.assertNotIn("No executed action recorded", report)

    def test_repeated_blockers_are_deduplicated_and_full_details_can_be_separate(self) -> None:
        self.prepared["issues"][0].update(blockers=["Missing log"], missingPrerequisites=["Missing log"])
        self.judgments["issues"][0]["recommendations"][0]["missingEvidence"] = ["Missing log"]
        details = []
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments, audit_details=details,
            audit_details_url="final-report-details.md",
        )
        self.assertIn("## Blocked on evidence", report)
        row = next(line for line in report.splitlines() if line.startswith("| [#1]"))
        self.assertEqual(1, row.count("Missing log"))
        self.assertIn("[Details](final-report-details.md#details-issue-1)", report)
        self.assertIn("**Blocker:** ⛔ Missing log", "\n".join(details))
        self.assertNotIn("**Evidence / validation:**", report)

    def test_post_effect_observation_is_report_only_and_contract_alert_is_visible(self) -> None:
        frozen = copy.deepcopy(self.snapshot)
        observation = {
            "repository": "owner/repo", "snapshotId": "snapshot:current", "status": "partial",
            "startedAt": "2026-09-05T12:10:00Z", "observedAt": "2026-09-05T12:11:00Z",
            "apiCalls": 3, "maxApiCalls": 3, "problems": ["Check inventory unavailable."],
            "delegationStatus": {"records": [{
                "issueNumber": 1, "taskId": "new-task", "taskState": "completed",
                "lifecycle": "awaiting_pull_request", "pullRequests": [{
                    "number": 99, "state": "open", "isDraft": True,
                    "currentState": {"checks": {"state": "unknown"}, "draft": True},
                    "closingContract": {"status": "violation", "detail": "Fixes #1 closes the quarantine tracker."},
                }],
            }]},
        }
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments, post_execution_observation=observation,
        )
        visible = re.sub(r"<details\b[^>]*>.*?</details>", "", report, flags=re.DOTALL)
        self.assertIn("frozen pre-effect evidence collected 2026-09-05T12:00:00Z", visible)
        self.assertIn("observed 2026-09-05T12:11:00Z", visible)
        self.assertIn("0 executed effects recorded", visible)
        self.assertIn("Read-only reporting evidence only", visible)
        self.assertIn("CLOSING CONTRACT ALERT", visible)
        human = visible.split("## Human action required\n", 1)[1].split("\n## ", 1)[0]
        self.assertIn("new-task", human)
        self.assertIn("Remove closing keywords", human)
        self.assertEqual(frozen, self.snapshot)
        with self.assertRaisesRegex(ValueError, "must match"):
            render_run_markdown(
                self.snapshot, self.prepared, self.judgments,
                post_execution_observation={**observation, "snapshotId": "different"},
            )

    def test_duration_and_age_use_compact_human_units(self) -> None:
        self.prepared["issues"][0]["recovery"] = {"subjects": [{
            "occurrence": {"issueNumber": 1, "observedAt": "2026-08-28T12:00:00Z"},
        }]}
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments, as_of="2026-09-05T12:00:00Z",
            invocation_window={
                "scope": "owner-held", "startedAt": "2026-09-05T11:00:00Z",
                "completedAt": "2026-09-05T11:43:17.660Z",
            },
            recording_windows=[{"label": "Worker recording", "durationSeconds": 86.8657}],
        )
        self.assertIn("Recorded owner-held window: 43m18s", report)
        self.assertIn("Worker recording: 1m27s", report)
        self.assertIn("age: 8d", report)

    def test_collection_failure_shows_status_and_actual_reason_not_just_counts(self) -> None:
        for status, detail, error, warning in (
            ("failed", "Open inventory could not be fetched.", "GitHub API returned 403.", None),
            ("truncated", "open scan stopped after the 2 page budget", None, "Workflow history truncated at the configured budget."),
        ):
            with self.subTest(status=status):
                self.snapshot["openBotScan"] = {"status": status, "complete": False, "detail": detail}
                self.snapshot["collectionErrors"] = [{"stage": "inventory", "message": error}] if error else []
                self.snapshot["warnings"] = [warning] if warning else []
                report = render_run_markdown(self.snapshot, self.prepared, self.judgments)
                visible = re.sub(r"<details\b[^>]*>.*?</details>", "", report, flags=re.DOTALL)
                self.assertIn(f"Open bot scan: {status}", visible)
                self.assertIn(detail, visible)
                self.assertIn(error or warning, visible)
                self.assertIn("[Full collection audit](report-details.md)", visible)
                self.assertLess(visible.index("Open bot scan:"), visible.index("## Investigations this run"))

    def test_overview_separates_current_queue_from_investigation_history(self) -> None:
        cases = [
            {"state": "Waiting for evidence"},
            {"active": True, "state": "Investigation running"},
            {"active": True, "foreign": True, "state": "Waiting for evidence"},
            {"reused": True, "state": "Waiting for evidence"},
            {"planned": True, "state": "Investigation planned"},
            {"deferred": True, "state": "Waiting for investigation capacity (per-cycle-investigation-budget)"},
            {"outcome": "fixable", "lifecycle": "running", "state": "Copilot fix in progress"},
            {"outcome": "fixable", "lifecycle": "awaiting_pull_request", "state": "Copilot pull request awaiting resolution"},
            {"outcome": "fixable", "lifecycle": "association_pending", "state": "Waiting for Copilot PR association"},
            {"outcome": "fixable", "lifecycle": "handoff_required", "state": "Waiting for human decision"},
            {"outcome": "fixable", "state": "Waiting for fix handoff"},
            {"outcome": "needs-human", "state": "Waiting for human decision"},
            {"disposition": "watch", "state": "Watching for recurrence/recovery"},
            {"disposition": "ping-human", "state": "Waiting for human decision"},
            {"disposition": "no-action", "outcome": "inconclusive", "state": "No action planned"},
            {"disposition": "no-action", "outcome": "recovered", "state": "No action planned"},
            {"close": "executed", "state": "Closed"},
            {"close": "blocked", "state": "Waiting for evidence"},
            {"close": "indeterminate", "state": "Waiting for evidence"},
        ]
        for case in cases:
            with self.subTest(case=case):
                request = {"issueNumber": 1, "investigationId": "inv:1"}
                planned = case.get("planned") or case.get("deferred")
                plan = {
                    "requests": [] if case.get("deferred") else [request],
                    "deferredRequests": [{**request, "reason": "per-cycle-investigation-budget"}] if case.get("deferred") else [],
                }
                if case.get("reused"):
                    plan["requests"] = []
                    plan["reusedInvestigationIds"] = ["inv:1"]
                result = {
                    **request, "repository": "owner/repo", "sessionId": "worker:old",
                    "outcome": case.get("outcome", "needs-evidence"),
                    "summary": "The evidence was reviewed, not fixed.",
                    "missingEvidence": ["diagnostic stack"],
                    "reassessWhen": "Old investigation wake-up.",
                    "recordedAt": "2026-09-05T12:01:00Z",
                }
                sessions = [] if planned else [
                    {**result, "status": "started", "recordedAt": "2026-09-05T12:00:00Z"},
                    {**result, "status": "completed"},
                ]
                if case.get("active"):
                    sessions.append({
                        **result, "sessionId": "worker:new", "status": "started",
                        "repository": "other/repo" if case.get("foreign") else "owner/repo",
                        "recordedAt": "2026-09-05T12:02:00Z",
                    })
                snapshot = {**self.snapshot, "delegationStatus": {"records": [
                    {"issueNumber": 1, "taskId": "task:1", "lifecycle": case["lifecycle"]}
                ] if case.get("lifecycle") else []}}
                judgments = {"issues": [{
                    "issueNumber": 1, "recommendations": [{
                        "disposition": case.get("disposition", "investigate"),
                        "summary": "Current assessment.",
                        "reassessWhen": "Current judgement wake-up.",
                    }],
                }]}
                actions = [{
                    "repository": "owner/repo", "snapshotId": "snapshot:current",
                    "actionId": "close:1", "eventType": "terminal", "outcome": case["close"],
                    "operation": "close-issue", "target": {"kind": "issue", "number": 1},
                    "result": {"issueState": "closed"},
                }] if case.get("close") else []
                report = render_run_markdown(
                    snapshot, self.prepared, judgments, investigation_plan=plan,
                    investigation_results=[] if planned else [result],
                    investigation_sessions=sessions, action_events=actions,
                )
                overview = report.split("## Investigations this run\n", 1)[1].split("\n## Usage", 1)[0]
                row = next(line for line in overview.splitlines() if line.startswith("| [Issue #"))
                cells = row.strip("| ").split(" | ")
                self.assertEqual(cells[1], case["state"])
                history = "Not started" if planned else "Prior evidence review reused" if case.get("reused") else "Evidence review finished"
                self.assertIn(history, cells[2])
                if not planned:
                    self.assertIn("1m", cells[2])
                if case.get("disposition") in ("watch", "ping-human"):
                    self.assertIn("Current judgement wake-up.", cells[3])
                if case.get("reused"):
                    self.assertIn("0 evidence reviews finished, 0 running, 1 reused results", report)
                if case.get("active") and not case.get("foreign"):
                    self.assertIn("1 evidence reviews finished, 1 running", report)

    def test_user_text_is_escaped_in_every_report_surface(self) -> None:
        self.snapshot["issues"][0].update({
            "title": "Generic<T> and <Foo<Bar>>",
            "url": "https://github.com/owner/repo/issues/1?name=<IssueUrl<T>>",
            "assignees": ["Owner<T>"],
        })
        self.snapshot["warnings"] = ["Warning<T>"]
        self.prepared["issues"][0]["sourceState"] = {"State<T>": "Value<V>"}
        self.prepared["issues"][0]["blockers"] = ["Blocker<T>"]
        self.judgments["issues"][0]["recommendations"][0].update({
            "summary": "Summary<T>", "reassessWhen": "Wake<T>",
        })
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            pull_request_review={"tasks": [{
                "target": {"kind": "pull-request", "number": 2},
                "title": "Pull<Foo<Bar>>", "url": "https://github.com/owner/repo/pull/2?name=<PullUrl<T>>",
                "actualOwner": "Reviewer<T>",
            }]},
            investigation_plan={"requests": [{"issueNumber": 1, "investigationId": "inv:1"}]},
            investigation_results=[{
                "repository": "owner/repo", "issueNumber": 1, "investigationId": "inv:1",
                "outcome": "needs-evidence", "summary": "Conclusion<T>",
                "validation": {"Generic<T>": {"<Foo<Bar>>": "Result<T>"}},
                "missingEvidence": ["Evidence<T>"], "sessionId": "Session<T>",
            }],
        )
        unexpected = {
            tag for tag in re.findall(r"<[^\n>]*>", report)
            if not re.fullmatch(r"</?(?:details|summary)>|<a id=\"(?:details-)?(?:issue|pr)-\d+\">|</a>", tag)
        }
        self.assertEqual(unexpected, set())
        for text in (
            "Generic&lt;T&gt;", "&lt;Foo&lt;Bar&gt;&gt;", "State&lt;T&gt;", "Value&lt;V&gt;",
            "Owner&lt;T&gt;", "Reviewer&lt;T&gt;", "Blocker&lt;T&gt;", "Summary&lt;T&gt;",
            "Wake&lt;T&gt;", "Conclusion&lt;T&gt;", "Evidence&lt;T&gt;", "Session&lt;T&gt;",
            "Result&lt;T&gt;", "Warning&lt;T&gt;", "&lt;IssueUrl&lt;T&gt;&gt;", "&lt;PullUrl&lt;T&gt;&gt;",
        ):
            self.assertIn(text, report)

    def test_audit_link_uses_the_same_text_escaping(self) -> None:
        report = render_run_markdown(
            self.snapshot, self.prepared, self.judgments,
            audit_details_url="reports/<Audit<T>>.md",
        )
        self.assertEqual(report.count("[Full collection audit](reports/&lt;Audit&lt;T&gt;&gt;.md)"), 2)


if __name__ == "__main__":
    unittest.main()
