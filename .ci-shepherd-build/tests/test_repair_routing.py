from __future__ import annotations

import copy
import unittest
from datetime import UTC, datetime

from ci_shepherd.actor import execute_action, validate_action_proposals
from ci_shepherd.actions import build_action_proposals
from ci_shepherd.eligibility import delegation_readiness, repair_priority, repair_priority_key
from ci_shepherd.lifecycle import prepare_assessment
from ci_shepherd.poc import build_compact_poc_input, validate_poc_judgments, validate_poc_projectability
from ci_shepherd.review_selection import build_review_selection, merge_selected_poc_judgments
from ci_shepherd.policy_selection import build_policy_selection
from ci_shepherd.collector import Collector
from ci_shepherd.signals import extract_issue_signals
from test_actor import ScriptedActorClient
from test_enrichment import EnrichmentClient, FakeTextResponse
from test_signals import occurrence_table
from test_observations import association, evidence, fact, issue_payload, job_payload, log_payload, run_payload, snapshot
from test_policy_selection import _policy_document, _projection
from test_policy import ASPIRE_REPOSITORY_POLICY_PATH
from ci_shepherd.repository_policy import load_repository_policy
from test_production_decisions import assess, quarantined_snapshot
from test_production_decisions import handoff_snapshot


NOW = datetime(2026, 8, 19, 16, tzinfo=UTC)


def repair_snapshot(*, test_name="Demo.Tests.Fails", runs=(100, 101), attempt=1, category="test"):
    issue = issue_payload(21, facts=[fact("testName", test_name)] if category == "test" else [])
    issue.update(
        title=f"Flaky test {test_name}" if category == "test" else "Unclassified failure",
        body="A failure was observed.", assignees=[], labels=["test-failure"],
        author="github-actions[bot]", authorType="Bot",
    )
    records = []
    for ordinal, run_id in enumerate(runs):
        run = run_payload(run_id=run_id, attempt=attempt)
        run.update(
            event="pull_request", branch="feature", workflowPath=".github/workflows/ci.yml",
            subjectPullRequests=[{"number": 50, "headSha": run["headSha"], "baseRepository": "microsoft/aspire"}],
            referencedBy=association(21),
        )
        job_id = 900 + ordinal
        job = job_payload(21, run_id=run_id, job_id=job_id, attempt=attempt)
        log = log_payload(
            21, run_id=run_id, job_id=job_id, attempt=attempt,
            excerpt=f"Failed {test_name} [42 ms]" if category == "test" else "src/Program.cs(1): error CS1002: ; expected",
        )
        if category == "test":
            log["facts"] = [fact("exceptionType", "System.TimeoutException")]
        records.extend([
            evidence(f"run:{run_id}", "workflow-run", run),
            evidence(f"run:{run_id}:attempt:{attempt}:job:{job_id}", "workflow-job", job),
            evidence(f"run:{run_id}:attempt:{attempt}:job:{job_id}:log", "workflow-log", log),
        ])
    value = snapshot(issue, *records)
    policy = load_repository_policy(ASPIRE_REPOSITORY_POLICY_PATH)
    value["repositoryPolicy"] = {**policy.as_public_dict(), "digest": policy.digest}
    return value


def producer_snapshot():
    value = repair_snapshot(category="build", runs=(100,))
    issue = value["evidence"]["issue:21"]["payload"]
    issue.update(
        producer="gh-aw-failure-issue", title="Agentic workflow failed", labels=["agentic-workflows"],
        author="github-actions[bot]", authorType="Bot",
        body="<!-- gh-aw-failure-issue: true, workflow_id: ci, branch: main -->\n"
             "https://github.com/microsoft/aspire/actions/runs/100",
    )
    return value


def collect_repair_logs(value, excerpts):
    jobs = [record["payload"] for record in value["evidence"].values() if record["kind"] == "workflow-job"]
    assert len(jobs) == len(excerpts)
    repository = value["repository"]
    client = EnrichmentClient(texts={
        f"/repos/{repository}/actions/jobs/{job['jobId']}/logs": FakeTextResponse(excerpt, truncated=False)
        for job, excerpt in zip(jobs, excerpts)
    })
    collector = Collector(client, repository, NOW)
    errors = []
    for job in jobs:
        job["url"] = f"https://github.com/{repository}/actions/runs/{job['runId']}/job/{job['jobId']}"
        collector._enrich_job_log(value["evidence"], errors, repository, job["runId"], job, job["referencedBy"])
    assert errors == []
    return value


def collect_triggering_pull_request(value, number=50, *, body="", source_text=None):
    repository = value["repository"]
    source_url = f"https://github.com/{repository}/issues/21"
    signals = extract_issue_signals(
        21, "issue:21", source_url,
        source_text if source_text is not None else
        occurrence_table((("2026-08-19", 100, "Tests / Aspire.Hosting.Tests (ubuntu-latest)", number),)),
        repository,
    )
    references = [dict(ref) for ref in signals.references if ref["targetType"] == "pull-request"]
    url = f"https://github.com/{repository}/pull/{number}"
    client = EnrichmentClient(
        singles={
            f"/repos/{repository}/issues/{number}": {
                "number": number, "state": "open", "body": body, "html_url": url,
                "pull_request": {"url": f"https://api.github.com/repos/{repository}/pulls/{number}"},
            },
            f"/repos/{repository}/pulls/{number}": {
                "number": number, "state": "open", "html_url": url,
                "merged_at": None, "merge_commit_sha": None,
                "base": {"ref": "main", "sha": "a" * 40},
                "head": {"ref": "feature", "sha": value["evidence"]["run:100"]["payload"]["headSha"],
                         "repo": {"full_name": repository}},
            },
        },
        pages={f"/repos/{repository}/pulls/{number}/files?per_page=100": []},
    )
    errors = []
    Collector(client, repository, NOW)._enrich_pull_request_reference(
        value["evidence"], errors, repository, number, references,
    )
    assert errors == []
    return value


def producer_workflow_snapshot():
    value = producer_snapshot()
    issue = value["evidence"]["issue:21"]["payload"]
    issue["body"] = issue["body"].replace("workflow_id: ci,", "workflow_id: analyze-ci-failure,")
    run = value["evidence"]["run:100"]["payload"]
    run.update(
        workflowPath=".github/workflows/analyze-ci-failure.lock.yml",
        event="workflow_run", branch="main", headBranch="main", subjectPullRequests=[],
    )
    value["workflowDiscovery"] = {
        "defaultBranch": "main", "defaultBranchVerified": True,
        "recentScanComplete": True, "gaps": [],
        "workflows": [{
            "workflowId": run["workflowId"], "workflowPath": run["workflowPath"],
            "event": "workflow_run", "runIds": [100], "windowComplete": True, "gaps": [],
        }],
    }
    return collect_repair_logs(value, ["src/Program.cs(1): error CS1002: ; expected"])


def override_category(value, category):
    prepared = prepare_assessment(value)
    compact = build_compact_poc_input(prepared)
    selection = build_review_selection(compact)
    judgment = copy.deepcopy(compact["issues"][0]["defaultJudgment"])
    judgment["category"] = category
    ready = delegation_readiness(compact["issues"][0], category)
    judgment["recommendations"] = [{
        "disposition": "delegate-copilot", "target": {"kind": "issue", "value": 21},
        "confidence": "medium", "summary": "Investigate and fix the observed failure.",
        "evidenceIds": ready["evidenceIds"] if ready else ["issue:21"],
        "missingEvidence": [], "reassessWhen": "After the repair changes state.",
    }]
    judgments = merge_selected_poc_judgments(compact, selection, {
        "schemaVersion": 1, "snapshotId": prepared["snapshotId"], "issues": [judgment],
    })
    validate_poc_judgments(prepared, judgments)
    validate_poc_projectability(compact, judgments)
    proposals = build_action_proposals(value, prepared, judgments, "ankj", agent_input=compact)
    validate_action_proposals(proposals)
    return prepared, compact, judgments, proposals


class RepairRoutingTests(unittest.TestCase):
    def test_workflow_and_repair_context_match_the_issues_declared_job(self):
        from test_workflow_health import workflow_snapshot

        value = workflow_snapshot()
        setup_job = job_payload(12, run_id=100, job_id=901)
        setup_job["name"] = "Dependency setup (ubuntu-latest)"
        setup_log = log_payload(
            12, run_id=100, job_id=901,
            excerpt="error: downloading https://feed.example/Foo failed: HTTP 503",
        )
        value["evidence"].update([
            evidence("run:100:attempt:1:job:901", "workflow-job", setup_job),
            evidence("run:100:attempt:1:job:901:log", "workflow-log", setup_log),
        ])
        value["evidence"]["issue:12"]["payload"]["ledger"]["rows"][0]["job"] = setup_job["name"]

        issue, = prepare_assessment(value)["issues"]
        self.assertEqual(setup_job["name"], issue["workflowHealth"]["job"])
        self.assertEqual("transient-infrastructure", issue["repairEvidence"]["category"])
        self.assertEqual(
            {"issue:12", "run:100", "run:100:attempt:1:job:901", "run:100:attempt:1:job:901:log"},
            set(issue["repairEvidence"]["evidenceIds"]),
        )

    def test_unknown_declared_job_does_not_borrow_another_failed_job(self):
        from test_workflow_health import workflow_snapshot

        value = workflow_snapshot()
        value["evidence"]["issue:12"]["payload"]["ledger"]["rows"][0]["job"] = "Missing job (ubuntu-latest)"
        issue, = prepare_assessment(value)["issues"]
        self.assertFalse(issue["repairEvidence"]["ready"])
        self.assertIsNone(issue.get("workflowHealth"))

    def test_repair_witnesses_are_preserved_in_a_capped_evidence_bundle(self):
        value = repair_snapshot()
        for index in range(25):
            job_id = 5000 + index
            job = job_payload(21, run_id=100, job_id=job_id)
            job.update(name=f"Unrelated job {index} (ubuntu-latest)", conclusion="success")
            value["evidence"].update([evidence(
                f"run:100:attempt:1:job:{job_id}", "workflow-job", job,
            )])

        prepared, compact, judgments, proposals = assess(value)
        issue, = prepared["issues"]
        self.assertTrue(issue["repairEvidence"]["ready"])
        bundled = {record["id"] for record in issue["evidenceBundle"]}
        self.assertLessEqual(len(bundled), 25)
        self.assertEqual(set(), set(issue["repairEvidence"]["evidenceIds"]) - bundled)
        self.assertEqual("delegate-copilot", judgments["issues"][0]["recommendations"][0]["disposition"])
        self.assertEqual("assign-copilot", proposals["proposals"][0]["operation"])

    def test_repair_readiness_names_witnesses_that_cannot_fit_the_bundle(self):
        prepared = prepare_assessment(repair_snapshot(), max_bundle_records=3)
        issue, = prepared["issues"]
        self.assertFalse(issue["repairEvidence"]["ready"])
        missing = set(issue["repairEvidence"]["evidenceIds"]) - {
            record["id"] for record in issue["evidenceBundle"]
        }
        self.assertTrue(missing)
        self.assertTrue(all(
            any(evidence_id in fact for fact in issue["repairEvidence"]["missingFacts"])
            for evidence_id in missing
        ))

    def test_source_confirmed_quarantine_defaults_to_repair_without_local_handoff(self):
        prepared, compact, judgments, proposals = assess(quarantined_snapshot())
        recommendation = judgments["issues"][0]["recommendations"][0]
        self.assertEqual("delegate-copilot", recommendation["disposition"])
        self.assertEqual([], recommendation["missingEvidence"])
        self.assertEqual("investigate-and-fix", compact["issues"][0]["delegationReadiness"]["intent"])
        self.assertEqual("assign-copilot", proposals["proposals"][0]["operation"])
        self.assertEqual([21], [item["issueNumber"] for item in build_review_selection(compact)["selected"]])

    def test_quarantine_repair_requires_executed_reproduction_without_unquarantining(self):
        instructions = assess(quarantined_snapshot())[3]["proposals"][0]["customInstructions"]
        for required in (
            "Invoke the repository's fix-flaky-test skill",
            "run-test-repeatedly.sh", "run-test-repeatedly.ps1",
            ".github/workflows/reproduce-flaky-tests.yml",
            "/p:RunQuarantinedTests=true on BOTH dotnet build and dotnet test",
            "same reproduction mode", "nonzero executed-test count in every iteration",
            "all post-fix iterations passing", "normal green CI that excludes quarantine is NOT validation",
            "Test execution evidence", "21 consecutive days", "Refs #21",
            "Keep the tracking issue open", "Do not use closing keywords",
            "including generated suffixes",
        ):
            with self.subTest(required=required):
                self.assertIn(required, instructions)
        self.assertIn("Do not modify or remove the `[QuarantinedTest]` attribute in the final fix PR", instructions)

    def test_unquarantined_test_repair_also_requires_nonzero_executions(self):
        instructions = assess(repair_snapshot())[3]["proposals"][0]["customInstructions"]
        self.assertIn("Invoke the repository's fix-flaky-test skill", instructions)
        self.assertIn("nonzero executed-test count", instructions)
        self.assertIn("Only propose a repair when the root cause and fix are high-confidence", instructions)

    def test_broad_tracker_requires_scoped_classification_even_with_repair_evidence(self):
        value = repair_snapshot()
        value["evidence"]["issue:21"]["payload"]["producer"] = "tracking-issue"
        prepared, compact, judgments, proposals = assess(value)
        self.assertTrue(prepared["issues"][0]["repairEvidence"]["ready"])
        self.assertIsNone(delegation_readiness(compact["issues"][0], "flaky-test"))
        self.assertEqual([], [row for row in proposals["proposals"] if row["operation"] == "assign-copilot"])
        recommendation = judgments["issues"][0]["recommendations"][0]
        self.assertEqual("investigate", recommendation["disposition"])
        self.assertIn("bounded repair targets", recommendation["summary"])
        self.assertIn("do not delegate the umbrella", recommendation["reassessWhen"])

    def test_unquarantined_csharp_recurrence_reaches_selection_and_assignment(self):
        prepared, compact, judgments, proposals = assess(repair_snapshot())
        self.assertEqual(2, prepared["issues"][0]["repairEvidence"]["independentRunCount"])
        self.assertEqual("delegate-copilot", judgments["issues"][0]["recommendations"][0]["disposition"])
        self.assertIn("delegate-copilot", build_review_selection(compact)["selected"][0]["allowedDispositions"])
        assignment, = proposals["proposals"]
        self.assertTrue(assignment["executionEligibility"]["eligible"])
        self.assertEqual("current-ci-workflow-break", assignment["repairPriority"]["kind"])
        policy = _policy_document(created_at_utc=NOW, enabled_classes=frozenset({"delegate-copilot"}))
        policy["repository"] = proposals["repository"]
        selected = build_policy_selection(
            proposals, run_id=f"cycle:{proposals['snapshotId']}",
            policy_projection=_projection(policy_doc=policy), action_events=[], now=NOW,
        )
        self.assertEqual([assignment["actionId"]], selected["selectedActionIds"])

    def test_single_run_retries_unknown_scope_owner_and_different_failure_do_not_admit_repair(self):
        for mutation in ("one-run", "retry", "scope", "owner", "failure", "workflow", "lane", "unknown-test"):
            with self.subTest(mutation=mutation):
                value = repair_snapshot(runs=(100,) if mutation == "one-run" else (100, 101))
                if mutation == "retry":
                    value["evidence"].pop("run:101")
                    for key in list(value["evidence"]):
                        if key.startswith("run:101:"):
                            record = value["evidence"].pop(key)
                            record["payload"].update(runId=100, attempt=2)
                            if record["kind"] == "workflow-log":
                                record["payload"]["evidenceId"] = "run:100:attempt:2:job:901:log"
                            value["evidence"][key.replace("run:101:attempt:1:", "run:100:attempt:2:")] = record
                if mutation == "scope":
                    value["evidence"]["run:101"]["payload"]["subjectPullRequests"] = []
                if mutation == "owner":
                    value["evidence"]["issue:21"]["payload"]["assignees"] = ["human"]
                if mutation == "failure":
                    value["evidence"]["run:101:attempt:1:job:901:log"]["payload"]["facts"] = [fact("exceptionType", "System.InvalidOperationException")]
                if mutation == "workflow":
                    value["evidence"]["run:101"]["payload"]["workflowId"] = 10
                if mutation == "lane":
                    value["evidence"]["run:101:attempt:1:job:901"]["payload"]["name"] = "Tests / Other (windows-latest)"
                if mutation == "unknown-test":
                    value["evidence"]["issue:21"]["payload"]["facts"] = []
                    for record in value["evidence"].values():
                        if record["kind"] == "workflow-log":
                            record["payload"].update(excerpt="Process exited with code 1", facts=[])
                _, compact, _, proposals = assess(value)
                self.assertIsNone(compact["issues"][0].get("delegationReadiness"))
                self.assertEqual([], [p for p in proposals["proposals"] if p["operation"] == "assign-copilot"])

    def test_final_category_and_delegation_override_share_frozen_readiness(self):
        value = repair_snapshot(category="build", runs=(100,))
        prepared, compact, judgments, proposals = override_category(value, "blocking-build")
        self.assertEqual("unknown", compact["issues"][0]["defaultJudgment"]["category"])
        self.assertEqual("blocking-build", judgments["issues"][0]["category"])
        self.assertEqual("assign-copilot", proposals["proposals"][0]["operation"])
        self.assertEqual("current-ci-workflow-break", proposals["proposals"][0]["repairPriority"]["kind"])
        for mutation in ("missing-failure", "owner", "human"):
            blocked = copy.deepcopy(value)
            if mutation == "missing-failure":
                blocked["evidence"]["run:100:attempt:1:job:900:log"]["payload"]["excerpt"] = "Process exited with code 1"
            if mutation == "owner":
                blocked["evidence"]["issue:21"]["payload"]["assignees"] = ["human"]
            if mutation == "human":
                blocked["evidence"]["issue:21"]["payload"]["body"] = "- Assessment: Azure tenant expired.\n- Suggested: Renew tenant."
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                override_category(blocked, "blocking-build")

    def test_explicit_watch_does_not_get_replaced_by_available_delegation(self):
        value = repair_snapshot()
        prepared = prepare_assessment(value)
        compact = build_compact_poc_input(prepared)
        judgment = copy.deepcopy(compact["issues"][0]["defaultJudgment"])
        judgment["recommendations"][0]["disposition"] = "watch"
        merged = merge_selected_poc_judgments(compact, build_review_selection(compact), {
            "schemaVersion": 1, "snapshotId": compact["snapshotId"], "issues": [judgment],
        })
        self.assertEqual("watch", merged["issues"][0]["recommendations"][0]["disposition"])

    def test_non_csharp_scenario_is_a_repair_subject_not_a_quarantine_prerequisite(self):
        _, _, judgments, proposals = assess(repair_snapshot(test_name="VS Code opens dashboard"))
        self.assertEqual("delegate-copilot", judgments["issues"][0]["recommendations"][0]["disposition"])
        self.assertEqual("assign-copilot", proposals["proposals"][0]["operation"])

    def test_cross_pr_harness_recurrence_requires_revision_and_toolchain(self):
        value = repair_snapshot()
        value["evidence"]["run:101"]["payload"]["subjectPullRequests"][0]["number"] = 51
        self.assertIsNone(assess(value)[1]["issues"][0].get("delegationReadiness"))
        for number in (100, 101):
            run = value["evidence"][f"run:{number}"]["payload"]
            run["headSha"] = "b" * 40
            run["subjectPullRequests"][0]["headSha"] = "b" * 40
        for record in value["evidence"].values():
            if record["kind"] == "workflow-log":
                record["payload"]["excerpt"] = ".NET SDK:\n Version: 10.0.400\n" + record["payload"]["excerpt"]
        self.assertIsNotNone(assess(value)[1]["issues"][0].get("delegationReadiness"))
        log = value["evidence"]["run:101:attempt:1:job:901:log"]["payload"]
        log["excerpt"] = log["excerpt"].replace("10.0.400", "10.0.401")
        self.assertIsNone(assess(value)[1]["issues"][0].get("delegationReadiness"))
        log["excerpt"] = log["excerpt"].replace("10.0.401", "10.0.400")
        value["evidence"]["run:101"]["payload"]["headSha"] = "c" * 40
        value["evidence"]["run:101"]["payload"]["subjectPullRequests"][0]["headSha"] = "c" * 40
        self.assertIsNone(assess(value)[1]["issues"][0].get("delegationReadiness"))

    def test_matching_non_test_job_diagnostics_admit_repair_not_quarantine(self):
        value = collect_repair_logs(repair_snapshot(category="job"), [
            "##[error]Unable to locate the browser executable",
            "##[error]Unable to locate the browser executable",
        ])
        _, _, _, proposals = override_category(value, "product-or-tooling")
        self.assertEqual("assign-copilot", proposals["proposals"][0]["operation"])
        collect_repair_logs(value, [
            "##[error]Unable to locate the browser executable",
            "##[error]Browser cannot connect to server",
        ])
        with self.assertRaises(ValueError):
            override_category(value, "product-or-tooling")

    def test_http_status_recurrence_requires_the_same_collected_diagnostic_subject(self):
        value = collect_repair_logs(repair_snapshot(category="job"), [
            "##[error]runtime-archive download failed: HTTP 503",
            "##[error]browser-package download failed: HTTP 503",
        ])
        prepared, compact, judgments, proposals = assess(value)
        self.assertEqual(
            [(100, "infra:http-503:ubuntu-latest:none"), (101, "infra:http-503:ubuntu-latest:none")],
            [(row["runId"], row["fingerprintId"]) for row in prepared["observations"]["occurrences"]],
        )
        self.assertEqual(1, prepared["issues"][0]["repairEvidence"]["independentRunCount"])
        self.assertFalse(prepared["issues"][0]["repairEvidence"]["ready"])
        self.assertIsNone(compact["issues"][0].get("delegationReadiness"))
        self.assertEqual([], [proposal for proposal in proposals["proposals"] if proposal["operation"] == "assign-copilot"])
        with self.assertRaises(ValueError):
            override_category(value, "transient-infrastructure")

    def test_matching_resource_diagnostics_ignore_only_log_transport_timestamps(self):
        value = collect_repair_logs(repair_snapshot(category="job"), [
            "2026-08-19T14:01:00.123Z ##[error]runtime-archive download failed: HTTP 503",
            "2026-08-19T15:01:00.456Z ##[error]runtime-archive download failed: HTTP 503",
        ])
        prepared, _, _, proposals = override_category(value, "transient-infrastructure")
        self.assertEqual(2, prepared["issues"][0]["repairEvidence"]["independentRunCount"])
        self.assertEqual(["assign-copilot"], [proposal["operation"] for proposal in proposals["proposals"]])

    def test_generic_http_statuses_do_not_establish_a_repair_subject(self):
        for excerpt in (
            "##[error]Download failed: HTTP 503",
            "##[error]HTTP 503 Service Unavailable",
            "##[error]Unable to download: status code returned was: 429",
            "##[error]Unexpected transient network failure: HTTP 503",
            "##[error]Could not download: HTTP 503",
            "2026-08-19T15:01:00.123Z ##[error]Expected: runtime-archive HTTP 503",
        ):
            with self.subTest(excerpt=excerpt):
                value = collect_repair_logs(repair_snapshot(category="job"), [excerpt, excerpt])
                prepared = prepare_assessment(value)
                self.assertEqual(0, prepared["issues"][0]["repairEvidence"]["independentRunCount"])
                with self.assertRaises(ValueError):
                    override_category(value, "transient-infrastructure")

    def test_oversized_diagnostic_subjects_are_not_matched_by_a_shared_prefix(self):
        value = collect_repair_logs(repair_snapshot(category="job"), [
            "##[error]Unable to locate " + "x" * 4_000 + "/runtime",
            "##[error]Unable to locate " + "x" * 4_000 + "/browser",
        ])
        prepared = prepare_assessment(value)
        self.assertFalse(prepared["issues"][0]["repairEvidence"]["ready"])
        with self.assertRaises(ValueError):
            override_category(value, "product-or-tooling")

    def test_collection_error_message_cannot_supply_an_execution_diagnostic(self):
        value = collect_repair_logs(repair_snapshot(category="job"), [
            "Process completed with exit code 1.",
            "Process completed with exit code 1.",
        ])
        for record in value["evidence"].values():
            if record["kind"] == "workflow-log":
                record["payload"]["errorMessage"] = "Unable to locate the browser executable"
        with self.assertRaises(ValueError):
            override_category(value, "product-or-tooling")

    def test_coarse_workflow_health_cannot_bypass_repair_subject_matching(self):
        from test_workflow_health import add_execution, workflow_snapshot

        for excerpts in (
            ("##[error]runtime-archive download failed: HTTP 503",
             "##[error]browser-package download failed: HTTP 503"),
            ("##[error]Download failed: HTTP 503", "##[error]Download failed: HTTP 503"),
        ):
            with self.subTest(excerpts=excerpts):
                value = workflow_snapshot()
                add_execution(value, 101, "2026-08-19T15:45:00Z")
                collect_repair_logs(value, excerpts)
                prepared, compact, _, proposals = assess(value)
                issue = prepared["issues"][0]
                self.assertTrue(issue["workflowHealth"]["current"])
                self.assertEqual("delegate-copilot", issue["workflowHealth"]["route"])
                self.assertFalse(issue["repairEvidence"]["ready"])
                self.assertIsNone(delegation_readiness(compact["issues"][0], "transient-infrastructure"))
                self.assertEqual([], [
                    proposal for proposal in proposals["proposals"] if proposal["operation"] == "assign-copilot"
                ])

    def test_collected_open_triggering_pull_request_is_not_a_repair_owner(self):
        value = collect_triggering_pull_request(repair_snapshot())
        _, compact, judgments, proposals = assess(value)
        self.assertIsNotNone(compact["issues"][0].get("delegationReadiness"))
        self.assertEqual("delegate-copilot", judgments["issues"][0]["recommendations"][0]["disposition"])
        self.assertEqual(["assign-copilot"], [proposal["operation"] for proposal in proposals["proposals"]])

    def test_reporter_markdown_source_pr_preserves_default_assignment_after_collection(self):
        value = producer_snapshot()
        source = value["evidence"]["issue:21"]["payload"]
        source["body"] += "\n**Pull Request:** [#50](https://github.com/microsoft/aspire/pull/50)"
        collect_repair_logs(value, ["src/Program.cs(1): error CS1002: ; expected"])
        collect_triggering_pull_request(value, source_text=source["body"])
        _, compact, judgments, proposals = assess(value)
        self.assertEqual("blocking-build", judgments["issues"][0]["category"])
        self.assertIsNotNone(compact["issues"][0].get("delegationReadiness"))
        self.assertEqual(["assign-copilot"], [proposal["operation"] for proposal in proposals["proposals"]])
        self.assertEqual("workflow-producer", proposals["proposals"][0]["evidenceBasis"])

    def test_non_source_and_resolution_pr_links_still_block_producer_assignment(self):
        for reference in (
            "Related pull request: [#50](https://github.com/microsoft/aspire/pull/50)",
            "**Pull Request:** [#51](https://github.com/microsoft/aspire/pull/50)",
            "## Resolution\n**Pull Request:** [#50](https://github.com/microsoft/aspire/pull/50)",
            "**Pull Request:** [#50](https://github.com/microsoft/aspire/pull/50)\n"
            "Fixed by https://github.com/microsoft/aspire/pull/50",
        ):
            with self.subTest(reference=reference):
                value = producer_snapshot()
                source = value["evidence"]["issue:21"]["payload"]
                source["body"] += "\n" + reference
                collect_triggering_pull_request(value, source_text=source["body"])
                _, compact, _, proposals = assess(value)
                self.assertIsNone(compact["issues"][0].get("delegationReadiness"))
                self.assertEqual([], [
                    proposal for proposal in proposals["proposals"] if proposal["operation"] == "assign-copilot"
                ])
                with self.assertRaises(ValueError):
                    override_category(value, "blocking-build")

    def test_execution_source_exception_preserves_repair_and_unknown_pr_blockers(self):
        for blocker in ("unverified-run", "wrong-pr", "cross-repository", "unavailable",
                        "repair-reference", "missing-provenance", "linked-repair"):
            with self.subTest(blocker=blocker):
                value = collect_triggering_pull_request(
                    repair_snapshot(), body="Fixes #21" if blocker == "linked-repair" else "",
                )
                pull = value["evidence"]["pr:50"]
                if blocker == "unverified-run":
                    for record in value["evidence"].values():
                        if record["kind"] == "workflow-run":
                            record["payload"]["subjectPullRequests"] = []
                elif blocker == "wrong-pr":
                    pull["payload"]["number"] = 51
                elif blocker == "cross-repository":
                    pull["payload"]["targetRepository"] = "other/repo"
                elif blocker == "unavailable":
                    pull["availability"] = "partial"
                elif blocker == "repair-reference":
                    pull["payload"]["referencedBy"].append({
                        **pull["payload"]["referencedBy"][0], "extractionMethod": "full-pull-url",
                    })
                elif blocker == "missing-provenance":
                    pull["payload"]["referencedBy"] = association(21)
                prepared, compact, _, proposals = assess(value)
                self.assertIsNone(delegation_readiness(prepared["issues"][0], "flaky-test"))
                self.assertIsNone(delegation_readiness(compact["issues"][0], "flaky-test"))
                self.assertEqual([], [
                    proposal for proposal in proposals["proposals"] if proposal["operation"] == "assign-copilot"
                ])
                with self.assertRaises(ValueError):
                    override_category(value, "flaky-test")

    def test_current_producer_workflow_health_allows_assignment_without_ci_labels(self):
        value = producer_workflow_snapshot()
        prepared = prepare_assessment(value)
        issue = prepared["issues"][0]
        self.assertIsNotNone(issue.get("producerAdmission"))
        self.assertTrue(issue["workflowHealth"]["current"])
        self.assertEqual("delegate-copilot", issue["workflowHealth"]["route"])
        _, compact, _, proposals = override_category(value, "blocking-build")
        self.assertIsNotNone(compact["issues"][0].get("delegationReadiness"))
        self.assertEqual("workflow-producer", proposals["proposals"][0]["evidenceBasis"])
        self.assertEqual("assign-copilot", proposals["proposals"][0]["operation"])

    def test_producer_workflow_admission_preserves_ownership_identity_and_health_gates(self):
        for blocker in ("owned", "human", "unrelated-bot", "cross-repository", "recovered", "incomplete"):
            with self.subTest(blocker=blocker):
                value = producer_workflow_snapshot()
                source = value["evidence"]["issue:21"]["payload"]
                if blocker == "owned":
                    source["assignees"] = ["maintainer"]
                elif blocker == "human":
                    source.update(author="human", authorType="User")
                elif blocker == "unrelated-bot":
                    source["author"] = "unrelated[bot]"
                elif blocker == "cross-repository":
                    source["body"] = source["body"].replace("/microsoft/aspire/actions/", "/other/repo/actions/")
                elif blocker == "recovered":
                    value["evidence"]["run:100"]["payload"]["conclusion"] = "success"
                    value["evidence"]["run:100:attempt:1:job:900"]["payload"]["conclusion"] = "success"
                elif blocker == "incomplete":
                    value["workflowDiscovery"]["defaultBranchVerified"] = False
                with self.assertRaises(ValueError):
                    override_category(value, "blocking-build")
    def test_repeated_exit_boilerplate_and_issue_only_test_names_stay_local(self):
        for category in ("job", "test"):
            with self.subTest(category=category):
                value = repair_snapshot(category=category)
                for record in value["evidence"].values():
                    if record["kind"] == "workflow-log":
                        record["payload"].update(
                            excerpt="Process completed with exit code 1.",
                            errorMessage="Process completed with exit code 1.", facts=[],
                        )
                self.assertIsNone(assess(value)[1]["issues"][0].get("delegationReadiness"))

    def test_same_observed_pr_failure_with_active_repair_blocks_another_issue(self):
        value = repair_snapshot()
        second = copy.deepcopy(value["evidence"]["issue:21"])
        second["payload"].update(number=22, url="https://github.com/microsoft/aspire/issues/22")
        second["url"] = second["payload"]["url"]
        value["evidence"]["issue:22"] = second
        for record in value["evidence"].values():
            if record["kind"] in {"workflow-job", "workflow-log", "workflow-run"}:
                record["payload"]["referencedBy"].extend(association(22))
        status = handoff_snapshot(changed_files=3)["delegationStatus"]
        status["records"][0].update(
            issueNumber=22, taskState="in_progress", taskObservation="available",
            startedAt="2026-08-19T15:40:00Z",
        )
        value["delegationStatus"] = status
        compact = build_compact_poc_input(prepare_assessment(value))
        self.assertEqual(22, compact["issues"][0]["relatedWorkflowRepairs"][0]["issueNumber"])
        self.assertIsNone(compact["issues"][0].get("delegationReadiness"))
        for record in value["evidence"].values():
            if record["kind"] in {"workflow-job", "workflow-log", "workflow-run"}:
                record["payload"]["referencedBy"] = association(21)
        compact = build_compact_poc_input(prepare_assessment(value))
        self.assertEqual([], compact["issues"][0].get("relatedWorkflowRepairs", []))
        self.assertIsNotNone(compact["issues"][0].get("delegationReadiness"))

    def test_quarantine_priority_changes_only_with_observed_ordinary_ci_impact(self):
        issue = prepare_assessment(quarantined_snapshot())["issues"][0]
        self.assertEqual("quarantined-test-repair", repair_priority(issue)["kind"])
        ordinary = repair_snapshot()
        for record in ordinary["evidence"].values():
            if record["kind"] == "workflow-run":
                record["payload"]["workflowPath"] = ".github/workflows/tests.yml"
        issue["repairEvidence"] = prepare_assessment(ordinary)["issues"][0]["repairEvidence"]
        self.assertEqual("unquarantined-test-instability", repair_priority(issue)["kind"])
        issue["repairEvidence"]["broaderImpact"] = False
        self.assertEqual("quarantined-test-repair", repair_priority(issue)["kind"])
        value = repair_snapshot(category="build")
        for record in value["evidence"].values():
            if record["kind"] == "workflow-run":
                record["payload"]["workflowPath"] = ".github/workflows/tests-quarantine.yml"
        issue = prepare_assessment(value)["issues"][0]
        self.assertEqual("quarantined-test-repair", repair_priority(issue)["kind"])

    def test_priority_ties_use_recurrence_then_actual_failure_time_then_issue_number(self):
        common = {"current": True, "category": "product-or-tooling", "recurrent": True}
        issues = [
            {"issueNumber": 2, "repairEvidence": {**common, "lastFailureAt": "2026-08-19T15:00:00Z"}},
            {"issueNumber": 4, "repairEvidence": {**common, "lastFailureAt": "2026-08-19T15:10:00Z"}},
            {"issueNumber": 3, "repairEvidence": {**common, "lastFailureAt": "2026-08-19T15:10:00Z"}},
            {"issueNumber": 1, "repairEvidence": {**common, "current": False, "recurrent": False}},
        ]
        self.assertEqual([3, 4, 2, 1], [item["issueNumber"] for item in sorted(issues, key=repair_priority_key)])

    def test_explicit_upstream_artifact_failure_reuses_active_repair_for_two_downstreams(self):
        value = repair_snapshot(category="job")
        for record in value["evidence"].values():
            if record["kind"] == "workflow-log":
                record["payload"].update(
                    excerpt="Unable to download artifact packages from https://github.com/microsoft/aspire/actions/runs/80",
                    errorMessage="Unable to download artifact packages",
                )
        second = copy.deepcopy(value["evidence"]["issue:21"])
        second["payload"].update(number=23, url="https://github.com/microsoft/aspire/issues/23")
        second["url"] = second["payload"]["url"]
        value["evidence"]["issue:23"] = second
        value["openIssues"].append(23)
        value["issues"].append(second["payload"])
        for record in value["evidence"].values():
            if record["kind"] in {"workflow-job", "workflow-log", "workflow-run"}:
                record["payload"].setdefault("referencedBy", []).extend(association(23))
        producer = run_payload(run_id=80)
        producer.update(
            workflowPath=".github/workflows/producer.yml", referencedBy=association(22),
            updatedAt="2026-08-19T15:00:00Z",
        )
        key, record = evidence("run:80", "workflow-run", producer)
        value["evidence"][key] = record
        status = handoff_snapshot(changed_files=3)["delegationStatus"]
        owner = status["records"][0]
        owner.update(issueNumber=22, taskState="in_progress", taskObservation="available", startedAt="2026-08-19T15:40:00Z")
        value["delegationStatus"] = status
        prepared = prepare_assessment(value)
        compact = build_compact_poc_input(prepared)
        self.assertEqual([21, 23], [item["issueNumber"] for item in compact["issues"]])
        for item in compact["issues"]:
            self.assertEqual(22, item["relatedWorkflowRepairs"][0]["issueNumber"])
            self.assertIsNone(item.get("delegationReadiness"))
        for record in value["evidence"].values():
            if record["kind"] == "workflow-log":
                record["payload"]["excerpt"] = (
                    "A different incident: https://github.com/microsoft/aspire/actions/runs/80\n"
                    "Unable to download artifact packages"
                )
        prepared = prepare_assessment(value)
        self.assertTrue(all(not item.get("relatedWorkflowRepairs") for item in prepared["issues"]))

    def test_unsupported_upstream_link_asks_for_one_missing_fact(self):
        value = repair_snapshot(category="job")
        for record in value["evidence"].values():
            if record["kind"] == "workflow-log":
                record["payload"].update(
                    excerpt="Unable to download artifact packages from https://github.com/microsoft/aspire/actions/runs/80",
                    errorMessage="Unable to download artifact packages",
                )
        prepared = prepare_assessment(value)
        compact = build_compact_poc_input(prepared)
        self.assertIsNone(compact["issues"][0].get("delegationReadiness"))
        from ci_shepherd.investigations import build_investigation_plan
        plan = build_investigation_plan(prepared, {
            "schemaVersion": 1, "snapshotId": prepared["snapshotId"],
            "issues": [compact["issues"][0]["defaultJudgment"]],
        }, [])
        self.assertEqual(
            "Verify whether the explicitly linked artifact producer failed and already has a repair owner.",
            plan["requests"][0]["question"],
        )

    def test_routing_upgrade_wakes_old_omission_once_without_source_revision_churn(self):
        import cycle
        value = quarantined_snapshot()
        current = prepare_assessment(value)
        legacy = copy.deepcopy(current)
        legacy["issues"][0].pop("repairEvidence")
        self.assertEqual({21}, cycle._changed_prepared_issues(current, legacy))
        compact = build_compact_poc_input(current)
        selected = build_review_selection(compact, known_issue_numbers=[21], changed_issue_numbers=[21])
        self.assertEqual([21], [item["issueNumber"] for item in selected["selected"]])
        next_prepared = copy.deepcopy(current)
        next_prepared["sourceRevision"] = "d" * 40
        self.assertEqual(set(), cycle._changed_prepared_issues(next_prepared, current))
        subsequent = build_review_selection(build_compact_poc_input(next_prepared), known_issue_numbers=[21])
        self.assertEqual([], subsequent["selected"])

    def test_recognized_workflow_producer_reaches_assignment_without_ci_label(self):
        from ci_shepherd.investigations import build_investigation_plan

        value = producer_snapshot()
        prepared, compact, judgments, proposals = assess(value)
        self.assertEqual("blocking-build", judgments["issues"][0]["category"])
        self.assertEqual("delegate-copilot", judgments["issues"][0]["recommendations"][0]["disposition"])
        self.assertEqual([], judgments["issues"][0]["recommendations"][0]["missingEvidence"])
        self.assertIsNotNone(compact["issues"][0].get("delegationReadiness"))
        self.assertEqual([], build_investigation_plan(prepared, judgments, [])["requests"])
        proposal, = proposals["proposals"]
        self.assertEqual("workflow-producer", proposal["evidenceBasis"])
        self.assertTrue(proposal["executionEligibility"]["eligible"])
        source = value["evidence"]["issue:21"]["payload"]
        live = {
            "number": 21, "state": "open", "html_url": source["url"],
            "updated_at": source["updatedAt"], "labels": source["labels"], "body": source["body"],
            "assignees": [], "user": {"login": source["author"], "type": source["authorType"]},
        }
        client = ScriptedActorClient(authenticated_login="ankj", issues=[
            live, {**live, "assignees": [{"login": "Copilot"}]},
        ])
        result = execute_action(
            proposals, action_id=proposal["actionId"],
            prior_results={"schemaVersion": 1, "repository": proposals["repository"], "results": []},
            client=client, now=lambda: NOW,
        )
        self.assertEqual("executed", result["outcome"])
        self.assertEqual(1, len([call for call in client.calls if call[0] == "assign_copilot"]))

    def test_lookalike_and_cross_repository_producer_reports_are_advisory(self):
        for mutation in ("human", "other-bot", "cross-repo", "marker-only", "false-marker", "successful", "wrong-workflow", "owned"):
            value = producer_snapshot()
            issue = value["evidence"]["issue:21"]["payload"]
            if mutation == "human":
                issue.update(author="human", authorType="User")
            if mutation == "other-bot":
                issue["author"] = "unrelated[bot]"
            if mutation == "cross-repo":
                issue["body"] = issue["body"].replace("/microsoft/aspire/actions/", "/other/repo/actions/")
            if mutation == "marker-only":
                issue["body"] = issue["body"].split("\n")[0]
            if mutation == "false-marker":
                issue["body"] = issue["body"].replace("gh-aw-failure-issue: true", "gh-aw-failure-issue: false")
            if mutation == "successful":
                value["evidence"]["run:100"]["payload"]["conclusion"] = "success"
            if mutation == "wrong-workflow":
                value["evidence"]["run:100"]["payload"]["workflowPath"] = ".github/workflows/other.yml"
            if mutation == "owned":
                issue["assignees"] = ["human"]
            with self.subTest(mutation=mutation):
                _, compact, _, proposals = assess(value)
                self.assertIsNone(compact["issues"][0].get("delegationReadiness"))
                self.assertEqual([], [
                    proposal for proposal in proposals["proposals"] if proposal["operation"] == "assign-copilot"
                ])
                with self.assertRaises(ValueError):
                    override_category(value, "blocking-build")
