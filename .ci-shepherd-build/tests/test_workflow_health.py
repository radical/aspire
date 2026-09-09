from __future__ import annotations

import copy
import unittest
from datetime import UTC, datetime

from ci_shepherd.actions import build_action_proposals
from ci_shepherd.actor import validate_action_proposals
from ci_shepherd.investigations import build_investigation_plan
from ci_shepherd.lifecycle import prepare_assessment
from ci_shepherd.poc import build_compact_poc_input, close_is_projectable, validate_poc_judgments, validate_poc_projectability
from ci_shepherd.policy_selection import build_policy_selection
from ci_shepherd.operation_policy import DEFAULT_CAPS
from test_investigations import _judgments, _prepared
from test_observations import (
    association, evidence, issue_payload, job_payload, log_payload, results_payload, run_payload, snapshot,
)
from test_policy_selection import _comment_proposal, _document, _policy_document, _projection


def workflow_snapshot():
    issue = issue_payload(12, ledger_rows=[{
        "date": "2026-08-19", "sourceRun": 100,
        "job": "Tests / Aspire.Hosting.Tests (ubuntu-latest)", "pullRequest": None,
    }])
    run = run_payload()
    run.update(headBranch="main", workflowPath=".github/workflows/ci.yml", referencedBy=association(12))
    data = snapshot(
        issue,
        evidence("run:100", "workflow-run", run),
        evidence("run:100:attempt:1:job:900", "workflow-job", job_payload(12)),
        evidence("run:100:attempt:1:job:900:log", "workflow-log", log_payload(
            12, excerpt="src/File.cs(1,1): error CS1002: ; expected",
        )),
    )
    data["workflowDiscovery"] = {
        "defaultBranch": "main", "defaultBranchVerified": True,
        "recentScanComplete": True, "gaps": [],
        "workflows": [{
            "workflowId": 9, "workflowPath": ".github/workflows/ci.yml", "event": "push",
            "runIds": [100], "windowComplete": True, "gaps": [],
        }],
    }
    return data


def add_execution(data, run_id, observed_at, *, conclusion="failure", excerpt="##[error]Download failed: HTTP 503"):
    run = run_payload(run_id=run_id, conclusion=conclusion)
    run.update(
        headBranch="main", workflowPath=".github/workflows/ci.yml", referencedBy=association(12),
        createdAt=observed_at, updatedAt=observed_at, runStartedAt=observed_at,
    )
    job = job_payload(12, run_id=run_id, job_id=run_id * 10, conclusion=conclusion)
    job.update(startedAt=observed_at, completedAt=observed_at)
    records = [
        evidence(f"run:{run_id}", "workflow-run", run),
        evidence(f"run:{run_id}:attempt:1:job:{run_id * 10}", "workflow-job", job),
    ]
    if conclusion == "failure":
        records.append(evidence(
            f"run:{run_id}:attempt:1:job:{run_id * 10}:log", "workflow-log",
            log_payload(12, run_id=run_id, job_id=run_id * 10, excerpt=excerpt),
        ))
        data["evidence"]["issue:12"]["payload"]["ledger"]["rows"].append({
            "date": observed_at[:10], "sourceRun": run_id,
            "job": job["name"], "pullRequest": None,
        })
    data["evidence"].update(records)
    data["workflowDiscovery"]["workflows"][0]["runIds"].insert(0, run_id)
    data["workflowDiscovery"]["workflows"][0]["runIds"] = data["workflowDiscovery"]["workflows"][0]["runIds"][:5]


class WorkflowHealthTests(unittest.TestCase):
    def test_unverified_default_branch_cannot_authorize_workflow_delegation_or_closure(self) -> None:
        data = workflow_snapshot()
        add_execution(data, 101, "2026-08-19T15:45:00Z", conclusion="success")
        data["workflowDiscovery"].update(defaultBranch=None, defaultBranchVerified=False, workflows=[])
        issue = build_compact_poc_input(prepare_assessment(data))["issues"][0]
        self.assertFalse(issue["workflowHealth"]["current"])
        self.assertFalse(issue["workflowHealth"]["closureAllowed"])
        self.assertFalse(close_is_projectable(issue))
        self.assertNotEqual("delegate-copilot", issue["defaultJudgment"]["recommendations"][0]["disposition"])

    def test_incomplete_window_cannot_prove_recurrence_or_closure(self) -> None:
        for recovered in (False, True):
            with self.subTest(recovered=recovered):
                data = workflow_snapshot()
                data["evidence"]["run:100:attempt:1:job:900:log"]["payload"]["excerpt"] = (
                    "##[error]Download failed: HTTP 503"
                )
                add_execution(data, 101, "2026-08-19T15:45:00Z", conclusion="success" if recovered else "failure")
                if recovered:
                    data["collectedAt"] = "2026-09-20T16:00:00Z"
                data["workflowDiscovery"]["workflows"][0].update(
                    windowComplete=False, gaps=[{"code": "job-inventory-incomplete"}],
                )
                issue = build_compact_poc_input(prepare_assessment(data))["issues"][0]
                self.assertFalse(issue["workflowHealth"]["coverageComplete"])
                self.assertFalse(issue["workflowHealth"]["recurrent"])
                self.assertFalse(issue["workflowHealth"]["closureAllowed"])
                self.assertEqual("watch", issue["defaultJudgment"]["recommendations"][0]["disposition"])

    def test_source_evidence_outside_window_cannot_supply_consecutive_runs(self) -> None:
        data = workflow_snapshot()
        data["evidence"]["run:100:attempt:1:job:900:log"]["payload"]["excerpt"] = (
            "##[error]Download failed: HTTP 503"
        )
        add_execution(data, 101, "2026-08-19T15:45:00Z")
        data["workflowDiscovery"]["workflows"][0]["runIds"] = [101]
        health = prepare_assessment(data)["issues"][0]["workflowHealth"]
        self.assertEqual([101], health["sampleRunIds"])
        self.assertFalse(health["recurrent"])

    def test_future_run_does_not_hide_a_current_failure(self) -> None:
        data = workflow_snapshot()
        add_execution(data, 101, "2026-08-22T15:45:00Z")
        prepared = prepare_assessment(data)

        self.assertTrue(prepared["issues"][0]["workflowHealth"]["current"])
        self.assertEqual(
            "delegate-copilot",
            build_compact_poc_input(prepared)["issues"][0]["defaultJudgment"]["recommendations"][0]["disposition"],
        )

    def test_different_workflow_with_identical_names_cannot_establish_recovery(self) -> None:
        data = workflow_snapshot()
        data["collectedAt"] = "2026-09-20T16:00:00Z"
        data["evidence"]["issue:12"]["payload"]["occurrences"] = (
            data["evidence"]["issue:12"]["payload"]["ledger"]["rows"]
        )
        data["evidence"]["run:100:attempt:1:job:900:log"]["payload"]["excerpt"] = (
            "##[error]Download failed: HTTP 503"
        )
        add_execution(data, 101, "2026-09-19T15:45:00Z", conclusion="success")
        data["evidence"]["run:101"]["payload"]["workflowId"] = 10
        prepared = prepare_assessment(data)
        compact = build_compact_poc_input(prepared)
        judgments = {
            "schemaVersion": 1, "snapshotId": prepared["snapshotId"],
            "issues": [compact["issues"][0]["defaultJudgment"]],
        }

        self.assertFalse(prepared["issues"][0]["workflowHealth"]["closureAllowed"])
        result = build_action_proposals(data, prepared, judgments, "radical", agent_input=compact)
        self.assertEqual([], [row for row in result["proposals"] if row["operation"] == "close-issue"])

    def test_model_cannot_turn_a_known_human_decision_into_assignment(self) -> None:
        for structured in (False, True):
            with self.subTest(structured=structured):
                data = workflow_snapshot()
                payload = data["evidence"]["issue:12"]["payload"]
                payload["body"] = (
                    "- Assessment: Azure tenant is expired.\n"
                    "- Suggested: Choose a replacement tenant and workflow identity."
                )
                prepared = prepare_assessment(data)
                if not structured:
                    for record in prepared["issues"][0]["evidenceBundle"]:
                        record["payload"].pop("dashboardContext", None)
                compact = build_compact_poc_input(prepared)
                judgment = compact["issues"][0]["defaultJudgment"]
                recommendation = judgment["recommendations"][0]
                self.assertEqual("ping-human", recommendation["disposition"])
                recommendation.pop("humanEscalation")
                recommendation.update(disposition="delegate-copilot", confidence="medium")
                judgments = {"schemaVersion": 1, "snapshotId": prepared["snapshotId"], "issues": [judgment]}

                with self.assertRaises(ValueError):
                    validate_poc_projectability(compact, judgments)
                compact["issues"][0]["humanContext"]["decisionRequired"] = False
                with self.assertRaises(ValueError):
                    build_action_proposals(data, prepared, judgments, "radical", agent_input=compact)

    def test_explicit_nomination_remains_separate_from_automatic_human_decision_gate(self) -> None:
        data = workflow_snapshot()
        data["delegationRequests"] = [12]
        data["evidence"]["issue:12"]["payload"]["body"] = (
            "- Assessment: Azure tenant is expired.\n"
            "- Suggested: Choose a replacement tenant and workflow identity."
        )
        issue = build_compact_poc_input(prepare_assessment(data))["issues"][0]

        self.assertEqual("operator", issue["delegationReadiness"]["origin"])
        self.assertEqual("delegate-copilot", issue["defaultJudgment"]["recommendations"][0]["disposition"])

    def test_retries_keep_one_complete_occurrence_per_independent_run_witness(self) -> None:
        data = workflow_snapshot()
        add_execution(data, 101, "2026-08-19T15:45:00Z")
        expected_ids = ["issue:12"]
        for run_id, initial_job, minute in ((100, 900, 30), (101, 1010, 45)):
            data["evidence"][f"run:{run_id}"]["payload"]["attempt"] = 3
            for attempt in (1, 2, 3):
                job_id = initial_job if attempt == 1 else initial_job + attempt
                prefix = f"run:{run_id}:attempt:{attempt}:job:{job_id}"
                if attempt > 1:
                    job = job_payload(12, run_id=run_id, attempt=attempt, job_id=job_id)
                    job.update(
                        startedAt=f"2026-08-19T15:{minute + attempt}:00Z",
                        completedAt=f"2026-08-19T15:{minute + attempt}:00Z",
                    )
                    data["evidence"].update([
                        evidence(prefix, "workflow-job", job),
                        evidence(
                            f"{prefix}:log", "workflow-log",
                            log_payload(
                                12, run_id=run_id, attempt=attempt, job_id=job_id,
                                excerpt="test assertion failure",
                            ),
                        ),
                    ])
                data["evidence"].update([evidence(
                    f"{prefix}:test-results", "workflow-test-results",
                    results_payload(
                        12, run_id=run_id, attempt=attempt, job_id=job_id,
                        tests=[{"testName": "Tests.Type.Method", "outcome": "failed"}],
                    ),
                )])
            expected_ids.extend([f"run:{run_id}", prefix, f"{prefix}:log", f"{prefix}:test-results"])
        prepared = prepare_assessment(data)
        compact = build_compact_poc_input(prepared)
        issue = compact["issues"][0]

        self.assertTrue(issue["workflowHealth"]["recurrent"])
        self.assertEqual([101, 100], issue["workflowHealth"]["sampleRunIds"])
        self.assertEqual(sorted(expected_ids), issue["workflowHealth"]["evidenceIds"])
        self.assertEqual("delegate-copilot", issue["defaultJudgment"]["recommendations"][0]["disposition"])
        validate_poc_projectability(compact, {
            "schemaVersion": 1, "snapshotId": prepared["snapshotId"],
            "issues": [issue["defaultJudgment"]],
        })

    def test_recurrence_with_test_results_keeps_bounded_delegation_proof(self) -> None:
        for outcomes, witnesses in (
            (("failure",) * 5, ["run:103", "run:104"]),
            (("failure", "success", "failure", "success", "failure"), ["run:100", "run:102", "run:104"]),
        ):
            with self.subTest(outcomes=outcomes):
                data = workflow_snapshot()
                data["evidence"]["run:100:attempt:1:job:900:log"]["payload"]["excerpt"] = "test assertion failure"
                for index in range(1, 5):
                    add_execution(
                        data, 100 + index, f"2026-08-19T15:{30 + index * 5}:00Z",
                        conclusion=outcomes[index], excerpt="test assertion failure",
                    )
                for index, outcome in enumerate(outcomes):
                    run_id = 100 + index
                    job_id = 900 if index == 0 else run_id * 10
                    data["evidence"].update([evidence(
                        f"run:{run_id}:attempt:1:job:{job_id}:test-results", "workflow-test-results",
                        results_payload(
                            12, run_id=run_id, attempt=1, job_id=job_id,
                            tests=[{
                                "testName": "Tests.Type.Method",
                                "outcome": "failed" if outcome == "failure" else "passed",
                            }],
                        ),
                    )])
                prepared = prepare_assessment(data)
                compact = build_compact_poc_input(prepared)
                judgment = compact["issues"][0]["defaultJudgment"]

                self.assertTrue(prepared["issues"][0]["workflowHealth"]["recurrent"])
                self.assertEqual("delegate-copilot", judgment["recommendations"][0]["disposition"])
                proof = judgment["recommendations"][0]["evidenceIds"]
                self.assertEqual(witnesses, [
                    identifier for identifier in proof
                    if identifier.startswith("run:") and identifier.count(":") == 1
                ])
                self.assertLessEqual(len(proof), 16)
                validate_poc_projectability(compact, {
                    "schemaVersion": 1, "snapshotId": prepared["snapshotId"], "issues": [judgment],
                })

    def test_quarantine_trackers_do_not_enter_workflow_cleanup(self) -> None:
        data = workflow_snapshot()
        data["evidence"]["issue:12"]["payload"]["labels"].append("quarantined-test")
        self.assertNotIn("workflowHealth", prepare_assessment(data)["issues"][0])

    def test_known_human_owned_failure_is_not_sent_to_copilot(self) -> None:
        data = workflow_snapshot()
        data["evidence"]["issue:12"]["payload"]["body"] = (
            "- Assessment: Azure tenant is expired.\n"
            "- Suggested: Choose a replacement tenant and workflow identity."
        )
        prepared = prepare_assessment(data)
        compact = build_compact_poc_input(prepared)
        judgment = compact["issues"][0]["defaultJudgment"]
        self.assertEqual("ping-human", judgment["recommendations"][0]["disposition"])
        validate_poc_projectability(compact, {
            "schemaVersion": 1, "snapshotId": prepared["snapshotId"], "issues": [judgment],
        })

    def test_verified_repository_default_branch_is_not_hardcoded_to_main(self) -> None:
        data = workflow_snapshot()
        data["workflowDiscovery"]["defaultBranch"] = "development"
        data["evidence"]["run:100"]["payload"].update(branch="development", headBranch="development")
        prepared = prepare_assessment(data)
        issue = build_compact_poc_input(prepared)["issues"][0]

        self.assertEqual("development", issue["workflowHealth"]["defaultBranch"])
        self.assertEqual("delegate-copilot", issue["defaultJudgment"]["recommendations"][0]["disposition"])

    def test_stale_closure_rejects_a_retry_or_a_pr_target_success(self) -> None:
        for unsafe_success in ("retry", "pull_request_target", "future"):
            with self.subTest(unsafe_success=unsafe_success):
                data = workflow_snapshot()
                data["collectedAt"] = "2026-09-20T16:00:00Z"
                data["evidence"]["run:100:attempt:1:job:900:log"]["payload"]["excerpt"] = (
                    "##[error]Download failed: HTTP 503"
                )
                add_execution(
                    data, 101,
                    "2026-09-21T15:45:00Z" if unsafe_success == "future" else "2026-09-19T15:45:00Z",
                    conclusion="success",
                )
                if unsafe_success == "retry":
                    job = data["evidence"].pop("run:101:attempt:1:job:1010")
                    job["payload"].update(runId=100, attempt=2)
                    data["evidence"]["run:100:attempt:2:job:1010"] = job
                    data["evidence"].pop("run:101")
                elif unsafe_success == "pull_request_target":
                    data["evidence"]["run:101"]["payload"]["event"] = unsafe_success
                prepared = prepare_assessment(data)
                self.assertFalse(prepared["issues"][0]["workflowHealth"]["closureAllowed"])

    def test_model_cannot_close_a_young_transient_by_claiming_recovery(self) -> None:
        data = workflow_snapshot()
        data["evidence"]["run:100:attempt:1:job:900:log"]["payload"]["excerpt"] = (
            "##[error]Download failed: HTTP 503"
        )
        add_execution(data, 101, "2026-08-19T15:45:00Z", conclusion="success")
        prepared = prepare_assessment(data)
        compact = build_compact_poc_input(prepared)
        judgment = compact["issues"][0]["defaultJudgment"]
        judgment["recommendations"][0].update(
            disposition="review-close", missingEvidence=[],
            evidenceIds=prepared["issues"][0]["recovery"]["evidenceIds"],
        )
        prepared["issues"][0]["workflowHealth"]["closureAllowed"] = True

        result = build_action_proposals(data, prepared, {
            "schemaVersion": 1, "snapshotId": prepared["snapshotId"], "issues": [judgment],
        }, "radical", agent_input=compact)

        self.assertEqual([], result["proposals"])
        self.assertEqual(["review-close"], [item["disposition"] for item in result["blockedRecommendations"]])

    def test_workflow_priority_wins_budget_but_never_overrides_ineligibility(self) -> None:
        old = _comment_proposal(action_id="old:ping-human-comment", issue_number=1)
        current = _comment_proposal(action_id="current:ping-human-comment", issue_number=300)
        caps = copy.deepcopy(DEFAULT_CAPS)
        caps["create-comment"]["maxPerRun"] = 1
        policy = _projection(policy_doc=_policy_document(
            enabled_classes=frozenset({"create-comment"}), caps=caps,
        ))
        for eligible, expected in ((True, current["actionId"]), (False, old["actionId"])):
            with self.subTest(eligible=eligible):
                candidate = _comment_proposal(
                    action_id=current["actionId"], issue_number=300, eligible=eligible,
                )
                from ci_shepherd.eligibility import repair_priority
                candidate["repairPriorityFacts"] = {"workflowHealth": {"current": True, "category": "blocking-build"}}
                candidate["repairPriority"] = repair_priority(candidate["repairPriorityFacts"])
                selected = build_policy_selection(
                    _document([old, candidate]), run_id="cycle:test",
                    policy_projection=policy, action_events=[],
                    now=datetime(2026, 9, 3, 16, tzinfo=UTC),
                )
                self.assertEqual([expected], selected["selectedActionIds"])

    def test_isolated_network_failure_and_different_workflow_do_not_escalate(self) -> None:
        for second_workflow in (None, 10):
            with self.subTest(second_workflow=second_workflow):
                data = workflow_snapshot()
                data["evidence"]["run:100:attempt:1:job:900:log"]["payload"]["excerpt"] = (
                    "##[error]Download failed: HTTP 503"
                )
                if second_workflow is not None:
                    add_execution(data, 101, "2026-08-19T15:45:00Z")
                    data["evidence"]["run:101"]["payload"]["workflowId"] = second_workflow
                issue = build_compact_poc_input(prepare_assessment(data))["issues"][0]
                self.assertFalse(issue["workflowHealth"]["recurrent"])
                self.assertEqual("watch", issue["defaultJudgment"]["recommendations"][0]["disposition"])

    def test_newer_isolated_failure_does_not_hide_another_broken_workflow(self) -> None:
        data = workflow_snapshot()
        add_execution(data, 101, "2026-08-19T15:45:00Z")
        data["evidence"]["run:101"]["payload"].update(workflowId=10, workflow="Another workflow")

        issue = build_compact_poc_input(prepare_assessment(data))["issues"][0]

        self.assertEqual(9, issue["workflowHealth"]["workflowId"])
        self.assertEqual("blocking-build", issue["defaultJudgment"]["category"])
        self.assertEqual("delegate-copilot", issue["defaultJudgment"]["recommendations"][0]["disposition"])

    def test_recovered_build_does_not_bypass_another_incidents_retention_window(self) -> None:
        data = workflow_snapshot()
        data["evidence"]["run:100:attempt:1:job:900:log"]["payload"]["excerpt"] = (
            "##[error]Download failed: HTTP 503"
        )
        add_execution(data, 101, "2026-08-19T15:45:00Z", excerpt="src/File.cs(1,1): error CS1002: ; expected")
        add_execution(data, 102, "2026-08-19T15:50:00Z", conclusion="success")
        add_execution(data, 103, "2026-08-19T15:55:00Z", conclusion="success")
        for run_id in (101, 103):
            data["evidence"][f"run:{run_id}"]["payload"].update(
                workflowId=10, workflow="Another workflow",
            )
        data["workflowDiscovery"]["workflows"].append({
            "workflowId": 10, "workflowPath": ".github/workflows/ci.yml", "event": "push",
            "runIds": [103, 101], "windowComplete": True, "gaps": [],
        })
        prepared = prepare_assessment(data)

        self.assertEqual("verified", prepared["issues"][0]["recovery"]["status"])
        self.assertFalse(prepared["issues"][0]["workflowHealth"]["closureAllowed"])
        issue = build_compact_poc_input(prepared)["issues"][0]
        self.assertEqual("watch", issue["defaultJudgment"]["recommendations"][0]["disposition"])

    def test_three_failures_in_five_comparable_runs_escalate(self) -> None:
        for outcomes in (("success", "failure", "success", "failure"), ("failure", "failure", "success", "success")):
            with self.subTest(outcomes=outcomes):
                data = workflow_snapshot()
                data["evidence"]["run:100:attempt:1:job:900:log"]["payload"]["excerpt"] = (
                    "##[error]runtime-archive download failed: HTTP 503"
                )
                for index, conclusion in enumerate(outcomes, start=1):
                    add_execution(
                        data, 100 + index, f"2026-08-19T15:{30 + index * 5}:00Z", conclusion=conclusion,
                        excerpt="##[error]runtime-archive download failed: HTTP 503",
                    )
                issue = build_compact_poc_input(prepare_assessment(data))["issues"][0]
                self.assertEqual([104, 103, 102, 101, 100], issue["workflowHealth"]["sampleRunIds"])
                self.assertTrue(issue["workflowHealth"]["recurrent"])
                self.assertEqual("delegate-copilot", issue["defaultJudgment"]["recommendations"][0]["disposition"])

    def test_stale_transient_requires_newer_matching_success(self) -> None:
        for outcome, expected in ((None, "watch"), ("skipped", "watch"), ("success", "review-close")):
            with self.subTest(outcome=outcome):
                data = workflow_snapshot()
                data["collectedAt"] = "2026-09-20T16:00:00Z"
                data["evidence"]["run:100:attempt:1:job:900:log"]["payload"]["excerpt"] = (
                    "##[error]Download failed: HTTP 503"
                )
                if outcome is not None:
                    add_execution(data, 101, "2026-09-19T15:45:00Z", conclusion=outcome)
                prepared = prepare_assessment(data)
                compact = build_compact_poc_input(prepared)
                judgment = compact["issues"][0]["defaultJudgment"]
                self.assertEqual(expected, judgment["recommendations"][0]["disposition"])
                validate_poc_projectability(compact, {
                    "schemaVersion": 1, "snapshotId": prepared["snapshotId"], "issues": [judgment],
                })

    def test_pr_events_and_other_branches_never_become_default_branch_health(self) -> None:
        for event, branch in (
            ("pull_request", "main"), ("pull_request_target", "main"),
            ("merge_group", "main"), ("push", "feature"),
        ):
            with self.subTest(event=event, branch=branch):
                data = workflow_snapshot()
                data["evidence"]["run:100"]["payload"].update(event=event, headBranch=branch, branch=branch)
                self.assertNotIn("workflowHealth", prepare_assessment(data)["issues"][0])

    def test_workflow_repair_pr_does_not_auto_close_the_incident_on_merge(self) -> None:
        data = workflow_snapshot()
        data["repositoryPolicy"] = {"quarantinePullRequest": {"baseRef": "main"}}
        prepared = prepare_assessment(data)
        compact = build_compact_poc_input(prepared)
        judgments = {
            "schemaVersion": 1, "snapshotId": prepared["snapshotId"],
            "issues": [compact["issues"][0]["defaultJudgment"]],
        }

        result = build_action_proposals(data, prepared, judgments, "radical", agent_input=compact)

        self.assertEqual(["assign-copilot"], [proposal["operation"] for proposal in result["proposals"]])
        proposal = result["proposals"][0]
        self.assertIn("`Refs #12`", proposal["customInstructions"])
        self.assertIn("post-merge", proposal["customInstructions"])
        self.assertTrue(proposal["workflowPriority"])
        validate_action_proposals(result)

    def test_recent_transient_success_waits_for_the_retention_window(self) -> None:
        data = workflow_snapshot()
        data["evidence"]["run:100:attempt:1:job:900:log"]["payload"]["excerpt"] = (
            "##[error]Download failed: HTTP 503"
        )
        add_execution(data, 101, "2026-08-19T15:45:00Z", conclusion="success")
        prepared = prepare_assessment(data)
        compact = build_compact_poc_input(prepared)

        self.assertEqual("verified", prepared["issues"][0]["recovery"]["status"])
        self.assertEqual("watch", compact["issues"][0]["defaultJudgment"]["recommendations"][0]["disposition"])

    def test_two_independent_network_failures_escalate_on_the_same_day(self) -> None:
        data = workflow_snapshot()
        data["evidence"]["run:100:attempt:1:job:900:log"]["payload"]["excerpt"] = (
            "##[error]runtime-archive download failed: HTTP 503"
        )
        add_execution(data, 101, "2026-08-19T15:45:00Z", excerpt="##[error]runtime-archive download failed: HTTP 503")
        prepared = prepare_assessment(data)
        compact = build_compact_poc_input(prepared)
        judgment = compact["issues"][0]["defaultJudgment"]

        self.assertEqual("transient-infrastructure", judgment["category"])
        self.assertEqual("delegate-copilot", judgment["recommendations"][0]["disposition"])
        self.assertEqual([101, 100], compact["issues"][0]["workflowHealth"]["sampleRunIds"])
        validate_poc_judgments(prepared, {
            "schemaVersion": 1, "snapshotId": prepared["snapshotId"], "issues": [judgment],
        })
        validate_poc_projectability(compact, {
            "schemaVersion": 1, "snapshotId": prepared["snapshotId"], "issues": [judgment],
        })

    def test_current_build_failure_delegates_without_local_diagnosis(self) -> None:
        prepared = prepare_assessment(workflow_snapshot())
        compact = build_compact_poc_input(prepared)
        judgment = compact["issues"][0]["defaultJudgment"]

        self.assertEqual("blocking-build", judgment["category"])
        self.assertEqual("delegate-copilot", judgment["recommendations"][0]["disposition"])
        self.assertEqual("workflow-health", compact["issues"][0]["delegationReadiness"]["origin"])
        validate_poc_judgments(prepared, {
            "schemaVersion": 1, "snapshotId": prepared["snapshotId"], "issues": [judgment],
        })

    def test_current_workflow_investigation_precedes_older_test_backlog(self) -> None:
        prepared = _prepared()
        judgments = _judgments()
        current = copy.deepcopy(prepared["issues"][0])
        current.update(
            issueNumber=300,
            issueUrl="https://github.com/owner/repo/issues/300",
            workflowHealth={"current": True, "category": "blocking-build"},
        )
        current["evidenceBundle"] = [
            {"id": "issue:300", "kind": "issue-event"},
            {"id": "run:3000", "kind": "workflow-run"},
        ]
        prepared["issues"].append(current)
        judgment = copy.deepcopy(judgments["issues"][0])
        judgment["issueNumber"] = 300
        judgment["recommendations"][0].update(
            target={"kind": "issue", "value": 300},
            evidenceIds=["issue:300", "run:3000"],
        )
        judgments["issues"].append(judgment)

        plan = build_investigation_plan(prepared, judgments, [], max_requests=1)

        self.assertEqual([300], [request["issueNumber"] for request in plan["requests"]])
        self.assertEqual([21], [request["issueNumber"] for request in plan["deferredRequests"]])
