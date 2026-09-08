from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
import collect as collect_script
from ci_shepherd.collector import Collector, InventoryResult
from tests.test_collector import ScriptedClient, make_issue

from ci_shepherd.actions import build_action_proposals
from ci_shepherd.actor import validate_action_proposals
from ci_shepherd.lifecycle import prepare_assessment
from ci_shepherd.poc import build_compact_poc_input, validate_poc_judgments, validate_poc_projectability
from ci_shepherd.investigations import (
    attach_latest_investigation_results, build_investigation_plan,
    read_investigation_results, record_investigation_result, record_investigation_session_event,
    render_investigation_section,
)
from tests.test_investigations import _clean_checkout
from ci_shepherd.delegations import (
    DelegatedIssue, DelegatedPullRequest, PullRequestState,
    derive_delegation_tracking, normalize_agent_task,
)
from ci_shepherd.handoff_reminders import derive_handoff_reminders
from ci_shepherd.repository_policy import HandoffReminderPolicy
from tests.test_policy import ASPIRE_REPOSITORY_POLICY_PATH
from ci_shepherd.repository_policy import load_repository_policy
import cycle
from ci_shepherd.poc_state import load_review_schedule, record_review_wakeup
from ci_shepherd.models import validate_snapshot
from ci_shepherd.quarantine_reconciliation import freeze_quarantine_source
from ci_shepherd.review_selection import build_review_selection, merge_selected_poc_judgments
from ci_shepherd.managed_coverage import build_managed_item_coverage
from ci_shepherd.policy_selection import build_policy_selection
from tests.test_policy_selection import _policy_document, _projection
from tests.test_observations import (
    association, evidence, fact, issue_payload, job_payload, log_payload, run_payload, snapshot,
)


def recovery_snapshot(*, success: str = "success", test_name: str | None = None) -> dict:
    issue = issue_payload(
        21,
        facts=[fact("failureType", "main-repository-breakage"), fact("errorCode", "CS1002")],
        ledger_rows=[{"date": "2026-08-19", "sourceRun": 100,
                      "job": "Tests / Aspire.Hosting.Tests (ubuntu-latest)"}],
    )
    issue["title"] = "[Main CI Failure] Build error CS1002"
    issue["occurrences"] = copy.deepcopy(issue["ledger"]["rows"])
    issue["episodesComplete"] = True
    failed = run_payload()
    succeeded = run_payload(run_id=200, conclusion="success")
    for run in (failed, succeeded):
        run["referencedBy"] = association(21)
    succeeded["createdAt"] = "2026-08-19T15:31:00Z"
    good_job = job_payload(21, run_id=200, job_id=901, conclusion=success)
    good_job["startedAt"] = "2026-08-19T15:31:00Z"
    good_job["completedAt"] = "2026-08-19T15:45:00Z"
    return snapshot(
        issue,
        evidence("run:100", "workflow-run", failed),
        evidence("run:200", "workflow-run", succeeded),
        evidence("run:100:attempt:1:job:900", "workflow-job", job_payload(21)),
        evidence("run:200:attempt:1:job:901", "workflow-job", good_job),
        evidence("run:100:attempt:1:job:900:log", "workflow-log",
                 log_payload(21, excerpt=f"Failed {test_name} [42 ms]" if test_name
                             else "src/Program.cs(1): error CS1002: ; expected")),
        evidence("run:200:attempt:1:job:901:log", "workflow-log",
                 log_payload(21, run_id=200, job_id=901,
                             excerpt=f"Passed {test_name} [42 ms]" if test_name else "Build succeeded.")),
    )


def assess(value: dict, *, max_bundle_records: int = 25) -> tuple[dict, dict, dict, dict]:
    prepared = prepare_assessment(value, max_bundle_records=max_bundle_records)
    compact = build_compact_poc_input(prepared)
    judgments = {
        "schemaVersion": 1, "snapshotId": prepared["snapshotId"],
        "issues": [copy.deepcopy(issue["defaultJudgment"]) for issue in compact["issues"]],
    }
    validate_poc_judgments(prepared, judgments)
    validate_poc_projectability(compact, judgments)
    proposals = build_action_proposals(value, prepared, judgments, "ankj", agent_input=compact)
    return prepared, compact, judgments, proposals


def completed_investigation(
    root: Path, prepared: dict, judgments: dict, *, outcome: str = "needs-evidence",
    evidence_ids: list[str] | None = None,
    fix_handoff: dict | None = None,
) -> dict:
    validate_poc_judgments(prepared, judgments)
    request = build_investigation_plan(prepared, judgments, [])["requests"][0]
    checkout = _clean_checkout(root)
    state = root / "state"
    state.mkdir(mode=0o700, exist_ok=True)
    record_investigation_session_event(
        state, request, status="started", recorded_at="2026-08-19T16:01:00Z",
        session_id="investigator", checkout=checkout,
    )
    return record_investigation_result(
        state, request, {
            "outcome": outcome, "summary": "Compilation fails in the entry point.",
            "evidenceIds": request["evidenceIds"] if evidence_ids is None else evidence_ids,
            "reassessWhen": "After diagnostic evidence changes.",
            "missingEvidence": ["compiler context"] if outcome == "needs-evidence" else [],
            "fixHandoff": (fix_handoff or {"problem": "Missing statement terminator in the entry point.",
                           "likelyPaths": ["src/Program.cs"],
                           "validation": ["dotnet build src/App.csproj"]}) if outcome == "fixable" else None,
        },
        recorded_at="2026-08-19T16:02:00Z", session_id="investigator", checkout=checkout,
    )


def quarantined_snapshot() -> dict:
    issue = issue_payload(21, facts=[fact("testName", "Demo.Tests.Flaky")])
    issue.update(
        title="Flaky test Demo.Tests.Flaky", labels=["quarantined-test"],
        body="Demo.Tests.Flaky intermittently times out while waiting for readiness.",
    )
    value = snapshot(issue)
    source_state = {
        "schemaVersion": 1, "sourceRevision": "a" * 40,
        "sourceTreeDigest": "sha256:" + "b" * 64,
        "inspectorTreeDigest": "sha256:" + "c" * 64,
        "tests": [],
        "quarantines": [{
            "testName": "Demo.Tests.Flaky", "issueUrl": issue["url"],
            "file": "Demo.Tests/Tests.cs", "line": 12,
        }],
    }
    return freeze_quarantine_source(value, prepare_assessment(value), source_state)


class QuarantinedRemediationPipelineTests(unittest.TestCase):
    def test_duplicate_group_preserves_the_source_linked_quarantine_tracker(self) -> None:
        value = quarantined_snapshot()
        original = value["evidence"]["issue:21"]["payload"]
        original["facts"].append(fact("causeId", "test-timeout"))
        other = copy.deepcopy(value["evidence"]["issue:21"])
        other["url"] = other["payload"]["url"] = original["url"].replace("/21", "/20")
        other["payload"].update(number=20, labels=["test-failure"])
        value["evidence"]["issue:20"] = other
        value["openIssues"].append(20)
        value["issues"].append(other["payload"])
        _, compact, judgments, proposals = assess(value)
        tracked = next(issue for issue in compact["issues"] if issue["issueNumber"] == 21)
        self.assertEqual("superseded", tracked["actionCluster"]["role"])
        decision = next(issue for issue in judgments["issues"] if issue["issueNumber"] == 21)
        self.assertEqual("investigate", decision["recommendations"][0]["disposition"])
        self.assertEqual([], [action for action in proposals["proposals"] if action["operation"] == "close-issue"])

    def test_existing_quarantine_delegation_is_tracked_without_new_investigation(self) -> None:
        value = quarantined_snapshot()
        value["delegationStatus"] = handoff_snapshot(changed_files=3)["delegationStatus"]
        prepared, compact, judgments, proposals = assess(value)
        context = compact["issues"][0]["delegationContext"]
        self.assertEqual("task-21", context["records"][0]["taskId"])
        self.assertEqual(22, context["records"][0]["pullRequests"][0]["number"])
        self.assertEqual("no-action", judgments["issues"][0]["recommendations"][0]["disposition"])
        self.assertEqual([], build_investigation_plan(prepared, judgments, [])["requests"])
        self.assertEqual([], proposals["proposals"])

    def test_collection_freezes_quarantine_before_canonical_assessment(self) -> None:
        value = quarantined_snapshot()
        source_state = value["quarantineSourceState"]
        issue = value["evidence"]["issue:21"]["payload"]
        inventory = InventoryResult(
            [issue], [], {"issue:21": value["evidence"]["issue:21"]}, [], [], {},
        )
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as directory:
            root = Path(directory)
            with (
                patch.object(collect_script, "GitHubClient", return_value=object()),
                patch.object(collect_script, "Collector") as collector,
                patch.object(collect_script, "collect_quarantine_source_state", return_value=source_state) as inspect,
            ):
                collector.return_value.collect.return_value = inventory
                collector.return_value.enrich_github_evidence.return_value = inventory
                collector.return_value.enrich_ownership_evidence.return_value = inventory
                collect_script.collect(
                    value["repository"], root / "collected", None,
                    repository_policy_path=ASPIRE_REPOSITORY_POLICY_PATH,
                )
            frozen_path = root / "collected" / "input.json"
            frozen = json.loads(frozen_path.read_text())
            validate_snapshot(frozen)
            self.assertEqual(source_state, frozen["quarantineSourceState"])
            inspect.assert_called_once_with(None, ["Demo.Tests.Flaky"])
            work = root / "cycle"
            with patch.object(cycle, "collect_quarantine_source_state", side_effect=AssertionError("Source must remain frozen.")):
                started = cycle.start_cycle(
                    repository=value["repository"], state_dir=root / "state", work_dir=work,
                    checkout=None, shepherd_author="ankj", input_path=frozen_path,
                )
                if started["stage"] == "awaiting-review":
                    completed = cycle.finish_cycle(work_dir=work, agent_judgments_path=work / "agent-judgments.json")
                else:
                    completed = started
            prepared = json.loads((work / "assessment-input.json").read_text())
            self.assertEqual("quarantined", prepared["issues"][0]["testMaintenance"]["state"])
            plan = json.loads((work / "investigation-plan.json").read_text())
            self.assertEqual([21], [request["issueNumber"] for request in plan["requests"]])
            report = (work / "report.md").read_text()
            self.assertTrue(report.startswith("# CI Shepherd run report\n"))
            self.assertIn("0 executed effects", report)
            self.assertIn("[Audit details](report-details.md)", report)
            run_directory = Path(completed["runDirectory"])
            self.assertEqual(report, (run_directory / "report.md").read_text())
            self.assertEqual(
                (work / "report-details.md").read_text(),
                (run_directory / "report-details.md").read_text(),
            )

    def test_label_or_incomplete_source_cannot_supply_fix_authority(self) -> None:
        for missing in ("source-state", "attribute", "source-record", "source-revision", "source-availability"):
            with self.subTest(missing=missing), TemporaryDirectory() as directory:
                value = quarantined_snapshot()
                if missing == "source-state":
                    del value["quarantineSourceState"]
                elif missing == "attribute":
                    value["quarantineSourceState"]["quarantines"] = []
                elif missing == "source-record":
                    del value["evidence"]["source:tests/Demo.Tests/Tests.cs"]
                elif missing == "source-revision":
                    value["quarantineSourceState"]["sourceRevision"] = "d" * 40
                else:
                    value["evidence"]["source:tests/Demo.Tests/Tests.cs"]["availability"] = "partial"
                prepared, _, judgments, _ = assess(value)
                result = completed_investigation(
                    Path(directory), prepared, judgments, outcome="fixable",
                    fix_handoff={
                        "problem": "Fix the readiness wait.",
                        "likelyPaths": ["tests/Demo.Tests/Tests.cs"],
                        "validation": ["Run the exact test repeatedly."],
                    },
                )
                compact = build_compact_poc_input(attach_latest_investigation_results(prepared, [result]))
                self.assertNotIn("machineActionability", compact["issues"][0])
                self.assertEqual("investigate", compact["issues"][0]["defaultJudgment"]["recommendations"][0]["disposition"])

    def test_quarantine_handoff_requires_report_and_target_source_citations(self) -> None:
        for missing in ("diagnostic", "source-citation", "target-path"):
            with self.subTest(missing=missing), TemporaryDirectory() as directory:
                value = quarantined_snapshot()
                if missing == "diagnostic":
                    value["evidence"]["issue:21"]["payload"]["body"] = ""
                prepared, _, judgments, _ = assess(value)
                result = completed_investigation(
                    Path(directory), prepared, judgments, outcome="fixable",
                    evidence_ids=["issue:21"] if missing == "source-citation" else None,
                    fix_handoff={
                        "problem": "Fix the readiness wait.",
                        "likelyPaths": ["src/Other.cs" if missing == "target-path" else "tests/Demo.Tests/Tests.cs"],
                        "validation": ["Run the exact test repeatedly."],
                    },
                )
                compact = build_compact_poc_input(attach_latest_investigation_results(prepared, [result]))
                self.assertNotIn("machineActionability", compact["issues"][0])

    def test_quarantine_preview_limit_does_not_veto_a_complete_fix_handoff(self) -> None:
        value = quarantined_snapshot()
        value["evidence"]["issue:21"]["payload"]["body"] += "\n" + "Repeated diagnostic output.\n" * 300
        prepared, _, judgments, _ = assess(value)
        with TemporaryDirectory() as directory:
            result = completed_investigation(
                Path(directory), prepared, judgments, outcome="fixable",
                fix_handoff={
                    "problem": "Wait for resource readiness before querying it.",
                    "likelyPaths": ["tests/Demo.Tests/Tests.cs"],
                    "validation": ["Run Demo.Tests.Flaky repeatedly across operating systems."],
                },
            )
            attached = attach_latest_investigation_results(prepared, [result])
            compact = build_compact_poc_input(attached)
            self.assertEqual("delegate-copilot", compact["issues"][0]["defaultJudgment"]["recommendations"][0]["disposition"])
            incomplete = copy.deepcopy(result)
            incomplete["missingEvidence"] = ["remaining diagnostic output"]
            compact = build_compact_poc_input(attach_latest_investigation_results(prepared, [incomplete]))
            self.assertNotIn("machineActionability", compact["issues"][0])

    def test_source_confirmed_quarantine_cannot_be_closed_by_a_fix_judgment(self) -> None:
        prepared, compact, judgments, _ = assess(quarantined_snapshot())
        judgments["issues"][0]["recommendations"][0].update(disposition="review-close", missingEvidence=[])
        with self.assertRaisesRegex(ValueError, "review-close requires"):
            validate_poc_projectability(compact, judgments)

    def test_current_quarantine_fix_handoff_delegates_without_closing_tracker(self) -> None:
        value = quarantined_snapshot()
        policy = load_repository_policy(ASPIRE_REPOSITORY_POLICY_PATH)
        value["repositoryPolicy"] = {**policy.as_public_dict(), "digest": policy.digest}
        prepared, _, judgments, _ = assess(value)
        with TemporaryDirectory() as directory:
            result = completed_investigation(
                Path(directory), prepared, judgments, outcome="fixable",
                fix_handoff={
                    "problem": "Wait for resource readiness before querying it.",
                    "likelyPaths": ["tests/Demo.Tests/Tests.cs"],
                    "validation": ["Run Demo.Tests.Flaky repeatedly across operating systems."],
                },
            )
            attached = attach_latest_investigation_results(prepared, [result])
            compact = build_compact_poc_input(attached)
            final = {
                "schemaVersion": 1, "snapshotId": prepared["snapshotId"],
                "issues": [compact["issues"][0]["defaultJudgment"]],
            }
            self.assertEqual("delegate-copilot", final["issues"][0]["recommendations"][0]["disposition"])
            validate_poc_judgments(attached, final)
            validate_poc_projectability(compact, final)
            proposals = build_action_proposals(value, attached, final, "ankj", agent_input=compact)
            validate_action_proposals(proposals)
            self.assertEqual(["assign-copilot"], [proposal["operation"] for proposal in proposals["proposals"]])
            action = proposals["proposals"][0]
            self.assertEqual("source-reconciliation", action["evidenceBasis"])
            self.assertIn("Refs #21", action["customInstructions"])
            self.assertIn("Keep the tracking issue open", action["customInstructions"])
            self.assertIn("Do not modify or remove the `[QuarantinedTest]` attribute", action["customInstructions"])

    def test_source_confirmed_quarantine_starts_fix_investigation_without_recurrence(self) -> None:
        value = quarantined_snapshot()
        prepared, compact, judgments, proposals = assess(value)
        recommendation = judgments["issues"][0]["recommendations"][0]
        self.assertEqual("investigate", recommendation["disposition"])
        self.assertEqual({"kind": "issue", "value": 21}, recommendation["target"])
        self.assertEqual("quarantined", compact["issues"][0]["testMaintenance"]["state"])
        self.assertEqual([], proposals["proposals"])
        plan = build_investigation_plan(prepared, judgments, [])
        self.assertEqual(1, len(plan["requests"]))
        self.assertIn("source:tests/Demo.Tests/Tests.cs", plan["requests"][0]["evidenceIds"])
        self.assertIn("Demo.Tests.Flaky", plan["requests"][0]["workerPrompt"])
        self.assertIn("source-path record contains metadata rather than the needed source text", plan["requests"][0]["workerPrompt"])


class InvestigationCoveragePipelineTests(unittest.TestCase):
    def test_closure_requires_its_own_explanation_not_an_unrelated_watch_comment(self) -> None:
        from tests.test_actions import _with_owned_comment

        value = recovery_snapshot()
        _, _, _, proposals = assess(_with_owned_comment(value, "[automated] Waiting for another failure."))
        self.assertEqual(["edit-comment", "close-issue"], [action["operation"] for action in proposals["proposals"]])
        comment, close = proposals["proposals"]
        self.assertEqual(comment["actionId"], close["dependsOn"])
        _, _, _, explained = assess(_with_owned_comment(value, comment["body"]))
        self.assertEqual(["close-issue"], [action["operation"] for action in explained["proposals"]])

    def test_issue_tail_changes_invalidate_results_without_growing_worker_packet(self) -> None:
        value = snapshot(issue_payload(21))
        value["evidence"]["issue:21"]["payload"].update(
            producer="unknown", body="x" * 4_000 + "old failure detail",
        )
        prepared, _, judgments, _ = assess(value)
        with TemporaryDirectory() as directory:
            result = completed_investigation(Path(directory), prepared, judgments)
            unchanged = attach_latest_investigation_results(prepared, [result])
            self.assertEqual([result], unchanged["issues"][0]["investigationResults"])
            value["evidence"]["issue:21"]["payload"]["body"] = "x" * 4_000 + "new failure detail"
            fresh, _, fresh_judgments, _ = assess(value)
            attached = attach_latest_investigation_results(fresh, [result])
            self.assertNotIn("investigationResults", attached["issues"][0])
            request = build_investigation_plan(attached, fresh_judgments, [result])["requests"][0]
            payload = next(record["payload"] for record in request["allowedEvidence"] if record["id"] == "issue:21")
            self.assertEqual("x" * 4_000, payload["body"])
            self.assertIs(True, payload["bodyTruncated"])
            self.assertNotEqual(result["sourceEvidenceFingerprint"], request["sourceEvidenceFingerprint"])

    def test_issue_diagnostic_preview_rejects_malformed_and_unbounded_payloads(self) -> None:
        value = snapshot(issue_payload(21))
        value["evidence"]["issue:21"]["payload"].update(producer="unknown", body="failure")
        prepared, _, judgments, _ = assess(value)
        for field, invalid in (
            ("body", "x" * 4_001), ("body", []),
            ("bodyTruncated", "true"), ("bodyFingerprint", "unchecked"),
        ):
            with self.subTest(field=field):
                altered = copy.deepcopy(prepared)
                altered["issues"][0]["evidenceBundle"][0]["payload"][field] = invalid
                with self.assertRaisesRegex(ValueError, "issue body"):
                    validate_poc_judgments(altered, judgments)

    def test_unstructured_issue_diagnostics_reach_assessor_and_investigator(self) -> None:
        value = snapshot(issue_payload(21))
        payload = value["evidence"]["issue:21"]["payload"]
        payload.update(
            producer="unknown",
            title="Emulator tests fail during startup",
            body=(
                "Failed Demo.EmulatorTests.ReadData\n"
                "Failed Demo.EmulatorTests.UseBindMount\n"
                "Stopped waiting for resource 'TestDb' because it failed to start."
            ),
            facts=[],
        )
        prepared, compact, judgments, _ = assess(value)
        request = build_investigation_plan(prepared, judgments, [])["requests"][0]
        embedded = next(record["payload"] for record in request["allowedEvidence"] if record["id"] == "issue:21")
        visible = next(record for record in compact["issues"][0]["allowedEvidence"] if record["id"] == "issue:21")
        self.assertEqual(payload["body"], embedded.get("body"))
        self.assertEqual(payload["body"], visible.get("body"))
        self.assertIs(False, embedded["bodyTruncated"])
        self.assertIn("Demo.EmulatorTests.ReadData", request["workerPrompt"])
        self.assertIn("Demo.EmulatorTests.UseBindMount", request["workerPrompt"])
        self.assertEqual("investigate", judgments["issues"][0]["recommendations"][0]["disposition"])

    def test_log_diagnostic_shapes_and_prepared_bounds_are_validated(self) -> None:
        log_id = "run:100:attempt:1:job:900:log"
        malformed = [
            ("truncated", "false"), ("truncated", 1),
            ("excerpt", {"message": "error CS1002"}), ("errorMessage", ["error CS1002"]),
            ("facts", {}), ("facts", ["error CS1002"]),
            ("facts", [{"field": "errorCode", "raw": ["CS1002"]}]),
            ("facts", [{"field": "errorCode", "raw": "CS1002", "unboundedLog": "extra"}]),
        ]
        for index, (field, bad_value) in enumerate(malformed):
            with self.subTest(case=index):
                value = recovery_snapshot(success="skipped")
                value["evidence"][log_id]["payload"][field] = bad_value
                with self.assertRaisesRegex(ValueError, "workflow-log"):
                    validate_snapshot(value)
                with self.assertRaisesRegex(ValueError, "workflow-log"):
                    prepare_assessment(value)
        prepared, _, judgments, _ = assess(recovery_snapshot(success="skipped"))
        for field, bad_value in (
            ("excerpt", "x" * 4_001),
            ("facts", [fact("errorCode", "CS1002")] * 21),
            ("excerptTruncated", "true"),
            ("diagnosticFingerprint", "not-a-fingerprint"),
        ):
            with self.subTest(prepared_field=field):
                changed = copy.deepcopy(prepared)
                payload = next(record["payload"] for record in changed["issues"][0]["evidenceBundle"] if record["id"] == log_id)
                payload[field] = bad_value
                with self.assertRaisesRegex(ValueError, "workflow-log"):
                    validate_poc_judgments(changed, judgments)

    def test_diagnostic_changes_retire_results_and_blockers_without_replay_churn(self) -> None:
        for outcome in ("needs-evidence", "fixable"):
            for location in ("excerpt", "excerpt-tail", "facts-tail"):
                with self.subTest(outcome=outcome, location=location), TemporaryDirectory() as directory:
                    value = recovery_snapshot(success="skipped")
                    policy = load_repository_policy(ASPIRE_REPOSITORY_POLICY_PATH)
                    value["repositoryPolicy"] = {**policy.as_public_dict(), "digest": policy.digest}
                    log_id = "run:100:attempt:1:job:900:log"
                    raw = value["evidence"][log_id]["payload"]
                    if location == "excerpt-tail":
                        raw["excerpt"] = "x" * 4_001 + "\n" + raw["excerpt"]
                    elif location == "facts-tail":
                        raw["facts"] = [fact("step", "Compile src/Program.cs") for _ in range(21)]
                    validate_snapshot(value)
                    prepared, _, judgments, _ = assess(value)
                    result = completed_investigation(Path(directory), prepared, judgments, outcome=outcome)
                    attached = attach_latest_investigation_results(prepared, [result])
                    compact = build_compact_poc_input(attached)
                    current_judgments = {"schemaVersion": 1, "snapshotId": compact["snapshotId"],
                                         "issues": [issue["defaultJudgment"] for issue in compact["issues"]]}
                    unchanged = build_investigation_plan(attached, current_judgments, [result])
                    self.assertEqual([], unchanged["requests"])
                    self.assertEqual(unchanged, build_investigation_plan(attached, current_judgments, [result]))
                    changed = copy.deepcopy(value)
                    changed_raw = changed["evidence"][log_id]["payload"]
                    if location == "facts-tail":
                        changed_raw["facts"][-1] = fact("step", "Compile src/Other.cs")
                    else:
                        changed_raw["excerpt"] = changed_raw["excerpt"].replace(
                            "src/Program.cs(1): error CS1002: ; expected",
                            "src/Other.cs(23): error CS1002: a different statement needs a terminator",
                        )
                    validate_snapshot(changed)
                    fresh, _, fresh_judgments, _ = assess(changed)
                    refreshed = attach_latest_investigation_results(fresh, [result])
                    self.assertNotIn("investigationResults", refreshed["issues"][0])
                    plan = build_investigation_plan(refreshed, fresh_judgments, [result])
                    self.assertEqual([], plan["blockedAwaitingEvidence"])
                    self.assertEqual(1, len(plan["requests"]))
                    self.assertNotEqual(result["sourceEvidenceFingerprint"], plan["requests"][0]["sourceEvidenceFingerprint"])
                    stale = build_action_proposals(changed, attached, current_judgments, "ankj", agent_input=compact)
                    self.assertEqual([], stale["proposals"])

    def test_worker_receives_bounded_diagnostic_content_and_completeness(self) -> None:
        value = recovery_snapshot(success="skipped")
        log_id = "run:100:attempt:1:job:900:log"
        diagnostic = "src/Program.cs(1): error CS1002: ; expected"
        raw = value["evidence"][log_id]["payload"]
        raw.update(excerpt=diagnostic + "\n" + "x" * 5_000, truncated=False,
                   facts=[fact("errorCode", "CS1002") for _ in range(21)],
                   unboundedLog="not part of the prepared contract " * 5_000)
        validate_snapshot(value)
        prepared, _, judgments, _ = assess(value)
        request = build_investigation_plan(prepared, judgments, [])["requests"][0]
        embedded = next(record["payload"] for record in request["allowedEvidence"] if record["id"] == log_id)
        self.assertEqual({
            "evidenceId", "runId", "attempt", "jobId", "targetRepository", "referencedBy",
            "truncated", "excerpt", "excerptTruncated", "facts", "factsTruncated", "diagnosticFingerprint",
        }, set(embedded))
        self.assertIn(diagnostic, request["workerPrompt"])
        self.assertEqual(raw["excerpt"][:4_000], embedded["excerpt"])
        self.assertEqual(raw["facts"][:20], embedded["facts"])
        self.assertIs(False, embedded["truncated"])
        self.assertIs(True, embedded["excerptTruncated"])
        self.assertIs(True, embedded["factsTruncated"])
        self.assertLess(len(request["workerPrompt"]), 15_000)

    def test_collector_truncated_failed_log_cannot_authorize_recovery_or_delegation(self) -> None:
        for purpose in ("recovery", "delegation"):
            with self.subTest(purpose=purpose), TemporaryDirectory() as directory:
                value = recovery_snapshot(success="success" if purpose == "recovery" else "skipped")
                policy = load_repository_policy(ASPIRE_REPOSITORY_POLICY_PATH)
                value["repositoryPolicy"] = {**policy.as_public_dict(), "digest": policy.digest}
                log_id = "run:100:attempt:1:job:900:log"
                value["evidence"][log_id]["payload"]["truncated"] = True
                self.assertEqual("available", value["evidence"][log_id]["availability"])
                validate_snapshot(value)
                prepared, compact, judgments, proposals = assess(value)
                if purpose == "delegation":
                    result = completed_investigation(Path(directory), prepared, judgments, outcome="fixable")
                    prepared = attach_latest_investigation_results(prepared, [result])
                    compact = build_compact_poc_input(prepared)
                    judgments = merge_selected_poc_judgments(
                        compact, build_review_selection(compact),
                        {"schemaVersion": 1, "snapshotId": compact["snapshotId"], "issues": []},
                    )
                    proposals = build_action_proposals(value, prepared, judgments, "ankj", agent_input=compact)
                self.assertEqual([], proposals["proposals"])
                self.assertFalse(prepared["issues"][0]["recovery"]["complete"])
                self.assertEqual([log_id], prepared["observations"]["occurrences"][0]["incompleteDiagnosticEvidenceIds"])
                prepared_log = next(record for record in prepared["issues"][0]["evidenceBundle"] if record["id"] == log_id)
                self.assertIs(True, prepared_log["payload"]["truncated"])

    def test_watch_reclassification_preserves_current_needs_evidence_block_before_budgets(self) -> None:
        policy = load_repository_policy(ASPIRE_REPOSITORY_POLICY_PATH)
        value = recovery_snapshot(success="skipped")
        value["repositoryPolicy"] = {**policy.as_public_dict(), "digest": policy.digest}
        safe_issue = issue_payload(22, ledger_rows=[{
            "date": "2026-08-19", "sourceRun": 300,
            "job": "Tests / Aspire.Hosting.Tests (ubuntu-latest)",
        }])
        safe_issue["title"] = "One package download failure: HTTP 503"
        safe_issue["occurrences"] = copy.deepcopy(safe_issue["ledger"]["rows"])
        safe_run = run_payload(run_id=300)
        safe_run["referencedBy"] = association(22)
        value["openIssues"].append(22)
        value["issues"].append(safe_issue)
        value["evidence"].update([
            evidence("issue:22", "issue-event", safe_issue),
            evidence("run:300", "workflow-run", safe_run),
            evidence("run:300:attempt:1:job:1900", "workflow-job", job_payload(22, run_id=300, job_id=1900)),
            evidence("run:300:attempt:1:job:1900:log", "workflow-log",
                     log_payload(22, run_id=300, job_id=1900, excerpt="##[error]HTTP 503")),
        ])
        validate_snapshot(value)
        prepared, _, judgments, _ = assess(value)

        def project(raw: dict, results: list, *, watch: bool) -> tuple[dict, dict, dict]:
            current = attach_latest_investigation_results(prepare_assessment(raw), results)
            compact = build_compact_poc_input(current)
            overrides = []
            if watch:
                override = copy.deepcopy(compact["issues"][0]["defaultJudgment"])
                override["recommendations"][0].update(
                    disposition="watch", summary="Waiting for missing compiler context.",
                    missingEvidence=["compiler context"],
                    reassessWhen="After the diagnostic evidence changes.",
                )
                overrides.append(override)
            finalized = merge_selected_poc_judgments(
                compact, build_review_selection(compact, new_issue_numbers=[21, 22]),
                {"schemaVersion": 1, "snapshotId": compact["snapshotId"], "issues": overrides},
            )
            validate_poc_judgments(current, finalized)
            proposals = build_action_proposals(raw, current, finalized, "ankj", agent_input=compact)
            plan = build_investigation_plan(current, finalized, results)
            coverage = build_managed_item_coverage(
                raw, policy=policy, proposals=proposals, investigation_plan=plan,
                review_schedule={}, observations=current["observations"],
            )
            proposals["productionPilotCapability"] = {
                "schemaVersion": 1, "evidenceRound": 0,
                "managedItemCoverage": {key: coverage[key] for key in (
                    "schemaVersion", "valid", "blockers", "globalBlockers", "blockedScopes",
                )},
            }
            now = datetime(2026, 8, 19, 16, tzinfo=UTC)
            operation_policy = _policy_document(created_at_utc=now, enabled_classes=frozenset({"create-comment"}))
            operation_policy["operationClasses"]["create-comment"]["maxPerRun"] = 1
            selected = build_policy_selection(
                proposals, run_id=f"cycle:{compact['snapshotId']}",
                policy_projection=_projection(policy_doc=operation_policy), action_events=[], now=now,
            )
            return plan, proposals, selected

        with TemporaryDirectory() as directory:
            root = Path(directory)
            result = completed_investigation(root, prepared, judgments)
            results = read_investigation_results(root / "state")
            plan, proposals, selected = project(value, results, watch=True)
            self.assertEqual([21, 22], [p["issueNumber"] for p in proposals["proposals"]])
            safe_action = next(p["actionId"] for p in proposals["proposals"] if p["issueNumber"] == 22)
            self.assertEqual([safe_action], selected["selectedActionIds"])
            self.assertEqual([result["investigationId"]], [item["investigationId"] for item in plan["blockedAwaitingEvidence"]])
            self.assertEqual([], plan["requests"])
            self.assertEqual((plan, proposals, selected), project(value, results, watch=True))
            changed = copy.deepcopy(value)
            changed["evidence"]["issue:21"]["payload"]["title"] += " with new diagnostic evidence"
            released, _, _ = project(changed, results, watch=False)
            self.assertEqual([], released["blockedAwaitingEvidence"])
            self.assertEqual(1, len(released["requests"]))
            self.assertNotEqual(result["investigationId"], released["requests"][0]["investigationId"])

    def test_fixable_handoff_must_cite_the_actual_failed_execution(self) -> None:
        value = recovery_snapshot(success="skipped")
        prepared, _, judgments, _ = assess(value)
        with TemporaryDirectory() as directory:
            result = completed_investigation(Path(directory), prepared, judgments, outcome="fixable",
                                             evidence_ids=["issue:21", "run:200"])
            compact = build_compact_poc_input(attach_latest_investigation_results(prepared, [result]))
            self.assertNotEqual("delegate-copilot", compact["issues"][0]["defaultJudgment"]["recommendations"][0]["disposition"])

    def test_default_request_does_not_license_unselected_evidence(self) -> None:
        prepared, _, judgments, _ = assess(recovery_snapshot(success="skipped"))
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "outside its request"):
                completed_investigation(
                    Path(directory), prepared, judgments, outcome="fixable",
                    evidence_ids=["issue:21", "run:200:attempt:1:job:901"],
                )

    def test_diagnostic_enrichment_stays_within_the_prepared_bundle(self) -> None:
        value = recovery_snapshot(success="skipped")
        prepared, _, judgments, _ = assess(value, max_bundle_records=3)
        request = build_investigation_plan(prepared, judgments, [])["requests"][0]
        bundled_ids = {record["id"] for record in prepared["issues"][0]["evidenceBundle"]}
        self.assertLessEqual(len(request["allowedEvidence"]), 3)
        self.assertTrue(set(request["evidenceIds"]).issubset(bundled_ids))
        self.assertIn("complete failed-execution diagnostic evidence", request["missingEvidence"])
        with TemporaryDirectory() as directory:
            result = completed_investigation(Path(directory), prepared, judgments, outcome="fixable")
            compact = build_compact_poc_input(attach_latest_investigation_results(prepared, [result]))
            self.assertNotEqual("delegate-copilot", compact["issues"][0]["defaultJudgment"]["recommendations"][0]["disposition"])

    def test_fixable_result_cannot_override_flaky_transient_or_unknown_failure_identity(self) -> None:
        for diagnostic in ("Failed Demo.Tests.Flaky [42 ms]", "##[error]HTTP 503", "Process exited with code 1"):
            with self.subTest(diagnostic=diagnostic), TemporaryDirectory() as directory:
                value = recovery_snapshot(success="skipped")
                value["evidence"]["run:100:attempt:1:job:900:log"]["payload"]["excerpt"] = diagnostic
                prepared, _, judgments, _ = assess(value)
                result = completed_investigation(Path(directory), prepared, judgments, outcome="fixable")
                attached = attach_latest_investigation_results(prepared, [result])
                compact = build_compact_poc_input(attached)
                self.assertNotEqual("delegate-copilot", compact["issues"][0]["defaultJudgment"]["recommendations"][0]["disposition"])

    def test_delegation_projectability_accepts_ci_investigation_without_claiming_actionability(self) -> None:
        _, compact, judgments, _ = assess(recovery_snapshot(success="skipped"))
        recommendation = judgments["issues"][0]["recommendations"][0]
        recommendation.update(disposition="delegate-copilot", target={"kind": "issue", "value": 21})
        validate_poc_projectability(compact, judgments)
        self.assertIsNone(compact["issues"][0].get("machineActionability"))
        self.assertEqual("investigate-and-fix", compact["issues"][0]["delegationReadiness"]["intent"])

    def test_current_fixable_investigation_derives_one_structured_assignment(self) -> None:
        value = recovery_snapshot(success="skipped")
        policy = load_repository_policy(ASPIRE_REPOSITORY_POLICY_PATH)
        value["repositoryPolicy"] = {**policy.as_public_dict(), "digest": policy.digest}
        prepared, _, judgments, _ = assess(value)
        self.assertEqual(["issue:21", "run:100", "run:200"], judgments["issues"][0]["recommendations"][0]["evidenceIds"])
        request = build_investigation_plan(prepared, judgments, [])["requests"][0]
        self.assertEqual([
            "issue:21", "run:100", "run:100:attempt:1:job:900",
            "run:100:attempt:1:job:900:log", "run:200",
        ], request["evidenceIds"])
        self.assertEqual(request["evidenceIds"], [record["id"] for record in request["allowedEvidence"]])
        untouched_defaults = copy.deepcopy(judgments)
        with TemporaryDirectory() as directory:
            result = completed_investigation(Path(directory), prepared, judgments, outcome="fixable")
            self.assertEqual(untouched_defaults, judgments)
            attached = attach_latest_investigation_results(prepared, [result])
            compact = build_compact_poc_input(attached)
            judgments["issues"] = [compact["issues"][0]["defaultJudgment"]]
            validate_poc_judgments(attached, judgments)
            validate_poc_projectability(compact, judgments)
            proposals = build_action_proposals(value, attached, judgments, "ankj", agent_input=compact)
            self.assertEqual(["assign-copilot"], [p["operation"] for p in proposals["proposals"]])
            instructions = proposals["proposals"][0]["customInstructions"]
            for text in ("src/Program.cs", "dotnet build src/App.csproj", result["fixHandoff"]["problem"]):
                self.assertIn(text, instructions)
            from ci_shepherd.actor import execute_action
            from tests.test_actor import ScriptedActorClient
            action = proposals["proposals"][0]
            client = ScriptedActorClient(issues=[
                {"number": 21, "state": "open", "html_url": action["issueUrl"],
                 "updated_at": value["evidence"]["issue:21"]["payload"]["updatedAt"],
                 "labels": [{"name": "ci-failure-cause"}], "assignees": []},
                {"number": 21, "state": "open", "html_url": action["issueUrl"],
                 "assignees": [{"login": "Copilot"}]},
            ])
            prior = {"schemaVersion": 1, "repository": value["repository"], "results": []}
            executed = execute_action(proposals, action_id=action["actionId"], prior_results=prior,
                                      client=client, now=lambda: datetime(2026, 8, 19, 16, tzinfo=UTC))
            self.assertEqual("executed", executed["outcome"])
            prior["results"].append(executed)
            first_calls = list(client.calls)
            replayed = execute_action(proposals, action_id=action["actionId"], prior_results=prior,
                                      client=client, now=lambda: datetime(2026, 8, 19, 16, tzinfo=UTC))
            self.assertEqual(first_calls, client.calls)
            self.assertEqual("stale", replayed["outcome"])
            incomplete_compact = copy.deepcopy(compact)
            del incomplete_compact["issues"][0]["machineActionability"]["fixHandoff"]
            with self.assertRaisesRegex(ValueError, "code handoff"):
                validate_poc_projectability(incomplete_compact, judgments)
            missing_citation = copy.deepcopy(judgments)
            missing_citation["issues"][0]["recommendations"][0]["evidenceIds"] = ["issue:21"]
            with self.assertRaisesRegex(ValueError, "code handoff"):
                validate_poc_projectability(compact, missing_citation)
            for change in ("stale", "partial", "unknown", "no-log", "flaky"):
                with self.subTest(change=change):
                    changed = copy.deepcopy(value)
                    if change == "stale":
                        changed["evidence"]["issue:21"]["payload"]["title"] += " changed"
                    elif change == "partial":
                        changed["evidence"]["run:100:attempt:1:job:900:log"]["availability"] = "partial"
                    elif change == "unknown":
                        del changed["evidence"]["run:100"]["payload"]["headSha"]
                    elif change == "no-log":
                        del changed["evidence"]["run:100:attempt:1:job:900:log"]
                    else:
                        changed["evidence"]["issue:21"]["payload"]["facts"].append(fact("tier2TestName", "Demo.Tests.Flaky"))
                    fresh = attach_latest_investigation_results(prepare_assessment(changed), [result])
                    current = build_compact_poc_input(fresh)
                    self.assertNotEqual("delegate-copilot", current["issues"][0]["defaultJudgment"]["recommendations"][0]["disposition"])
                    blocked = build_action_proposals(changed, attached, judgments, "ankj", agent_input=compact)
                    self.assertEqual([], blocked["proposals"])

    def test_unchanged_needs_evidence_is_durable_blocked_work_not_another_request(self) -> None:
        value = recovery_snapshot(success="skipped")
        prepared, _, judgments, _ = assess(value)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result = completed_investigation(root, prepared, judgments)
            results = read_investigation_results(root / "state")
            attached = attach_latest_investigation_results(prepared, results)
            plan = build_investigation_plan(attached, judgments, results)
            self.assertEqual([], plan["requests"])
            self.assertEqual([{
                "issueNumber": 21, "target": result["target"],
                "investigationId": result["investigationId"],
                "sourceEvidenceFingerprint": result["sourceEvidenceFingerprint"],
                "missingEvidence": ["compiler context"], "status": "blocked-awaiting-evidence",
            }], plan["blockedAwaitingEvidence"])
            self.assertEqual(plan, build_investigation_plan(attached, judgments, results))
            self.assertIn("Blocked awaiting evidence", render_investigation_section(plan))
            self.assertIn("compiler context", render_investigation_section(plan))
            changed = copy.deepcopy(value)
            changed["evidence"]["issue:21"]["payload"]["title"] += " with new diagnostic detail"
            changed_prepared, _, changed_judgments, _ = assess(changed)
            changed_plan = build_investigation_plan(changed_prepared, changed_judgments, results)
            self.assertEqual([], changed_plan["blockedAwaitingEvidence"])
            self.assertEqual(1, len(changed_plan["requests"]))


def handoff_snapshot(*, changed_files: int | None = 0, events: list | None = None, human: bool = False) -> dict:
    value = recovery_snapshot(success="skipped")
    task = normalize_agent_task({
        "id": "task-21", "state": "completed", "created_at": "2026-08-19T15:00:00Z",
        "updated_at": "2026-08-19T15:55:00Z", "session_count": 1,
        "artifacts": [{"type": "pull", "provider": "github", "data": {"id": 101, "global_id": "PR_101"}}],
    })
    records = list(derive_delegation_tracking(
        events=[
            {"eventType": "delegation-baseline", "actionId": "assignment:21",
             "recordedAt": "2026-08-19T15:00:00Z", "operation": "assign-copilot",
             "repository": value["repository"], "target": {"kind": "issue", "number": 21}, "taskIdsBefore": []},
            {"eventType": "terminal", "actionId": "assignment:21", "outcome": "executed", "result": {"taskId": "task-21"}},
        ], tasks=[task],
        pull_requests=[DelegatedPullRequest(101, "PR_101", PullRequestState.OPEN, True, number=22, changed_files=changed_files)],
        issues=[DelegatedIssue(21, True, True, human)],
    ))
    derive_handoff_reminders(records, events or [], HandoffReminderPolicy(
        interval=timedelta(days=1), stale_progress_interval=timedelta(days=7), maximum=2,
    ))
    value["delegationStatus"] = {"status": "complete", "records": records}
    return value


class HandoffPipelineTests(unittest.TestCase):
    def test_snapshot_rejects_unverified_progress_shapes(self) -> None:
        for progress in (
            {"headSha": ""},
            {"headSha": True},
            {"headSha": "current-head", "updatedAt": "2026-08-20T12:00:00Z"},
        ):
            with self.subTest(progress=progress):
                value = handoff_snapshot()
                value["delegationStatus"]["records"][0]["pullRequests"][0]["progressSource"] = progress
                with self.assertRaisesRegex(ValueError, "progressSource"):
                    validate_snapshot(value)
        value = handoff_snapshot()
        value["delegationStatus"]["records"][0]["meaningfulProgress"] = {
            "status": "observed", "at": "2026-08-20T12:00:00Z",
            "basis": "updated-at", "evidenceIds": ["issue:21"], "precision": "source-event",
        }
        with self.assertRaisesRegex(ValueError, "basis"):
            validate_snapshot(value)

    def test_collection_persists_human_progress_before_scheduling_reminder(self) -> None:
        policy_path = Path(__file__).parent / "fixtures" / "repository-policy-widget-v1.json"
        for body, expected_at in (
            ("I am working on this.", "2026-08-27T12:00:00Z"),
            ("[automated] Refreshed status.", "2026-08-26T15:55:00Z"),
        ):
            with self.subTest(body=body), TemporaryDirectory() as temporary:
                root = Path(temporary)
                state = root / "state"
                state.mkdir(mode=0o700)
                record = copy.deepcopy(handoff_snapshot(human=True)["delegationStatus"]["records"][0])
                record["repository"] = "owner/repo"
                client = ScriptedClient(pages={
                    "/repos/owner/repo/issues?state=open&labels=ci-failure-cause&per_page=100": [
                        make_issue(21, labels=["ci-failure-cause"]),
                    ],
                    "/repos/owner/repo/issues?state=open&labels=automation-broken&per_page=100": [],
                    "/repos/owner/repo/issues?state=open&creator=github-actions%5Bbot%5D&per_page=100": [],
                    "/repos/owner/repo/issues/21/comments": [{
                        "id": 99,
                        "html_url": "https://github.com/owner/repo/issues/21#issuecomment-99",
                        "user": {"login": "reviewer", "type": "User"},
                        "body": body,
                        "created_at": "2026-08-20T12:00:00Z",
                        "updated_at": "2026-08-22T12:00:00Z",
                    }],
                })
                with (
                    patch.object(collect_script, "GitHubClient", return_value=client),
                    patch.object(collect_script, "observe_delegation_status", return_value=(
                        {"status": "complete", "records": [record]}, (),
                    )),
                    patch.object(collect_script.ActionEventStore, "append_delegation_observations"),
                    patch.object(Collector, "enrich_github_evidence", side_effect=lambda inventory, **kwargs: inventory),
                    patch.object(Collector, "enrich_ownership_evidence", side_effect=lambda inventory, **kwargs: inventory),
                ):
                    collect_script.collect(
                        "owner/repo", root / "output", None, state_dir=state,
                        shepherd_author="operator", repository_policy_path=policy_path,
                    )
                collected = json.loads((root / "output" / "input.json").read_text())
                actual = collected["delegationStatus"]["records"][0]
                self.assertEqual(expected_at, actual["nextWakeup"]["evaluateAt"])
                self.assertEqual(1, actual["handoffReminder"]["ordinal"])
                schedule = load_review_schedule(
                    state, "owner/repo", collected["collectedAt"],
                    issue_numbers=[21], pull_request_numbers=[],
                )
                self.assertEqual(expected_at, schedule["issues"]["21"]["reassessAt"])
                self.assertEqual("human-stale-progress", schedule["issues"]["21"]["wakeReason"])

    def test_unassessed_delegated_handoff_remains_valid_blocked_work(self) -> None:
        value = handoff_snapshot()
        value["delegatedIssues"] = [21]
        value["delegatedIssueDetails"] = value["issues"]
        value["openIssues"] = []
        value["issues"] = []

        _, compact, judgments, proposals = assess(value)

        self.assertEqual([], compact["issues"])
        self.assertEqual([], judgments["issues"])
        self.assertEqual([], proposals["proposals"])
        self.assertEqual(
            [{
                "issueNumber": 21,
                "disposition": "delegation-handoff",
                "blockingReasons": ["validated-ping-human-required"],
                "evidenceIds": ["issue:21"],
            }],
            proposals["blockedRecommendations"],
        )
        self.assertEqual(proposals, validate_action_proposals(proposals))

    def test_comment_activity_is_context_not_human_takeover(self) -> None:
        value = handoff_snapshot()
        value["evidence"]["issue:21:comment:99"] = {
            "kind": "issue-comment", "availability": "available",
            "url": "https://github.com/microsoft/aspire/issues/21#issuecomment-99",
            "payload": {"sourceIssueNumber": 21, "author": "reviewer", "createdAt": "2026-08-19T15:56:00Z"},
        }
        _, compact, _, proposals = assess(value)
        activity = compact["issues"][0]["delegationContext"]["activity"]
        self.assertEqual("issue:21:comment:99", activity["comments"][0]["evidenceId"])
        self.assertEqual(["create-comment"], [p["operation"] for p in proposals["proposals"]])

    def test_handoff_cannot_be_licensed_by_unprepared_lifecycle_facts(self) -> None:
        value = handoff_snapshot()
        prepared, compact, judgments, _ = assess(value)
        del prepared["issues"][0]["delegationContext"]
        proposals = build_action_proposals(value, prepared, judgments, "ankj", agent_input=compact)
        self.assertEqual([], proposals["proposals"])

    def test_due_delegated_issue_is_prepared_by_supported_cycle(self) -> None:
        value = handoff_snapshot()
        value["delegatedIssues"] = [21]
        value["delegatedIssueDetails"] = value["issues"]
        value["openIssues"] = []
        value["issues"] = []
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.json"
            source.write_text(json.dumps(value))
            (root / "state").mkdir(mode=0o700)
            record_review_wakeup(root / "state", value["repository"], target_kind="issue",
                                 target_number=21, evaluate_at=value["collectedAt"], reason="escalation-reminder")
            cycle.start_cycle(repository=value["repository"], state_dir=root / "state",
                              work_dir=root / "work", checkout=None, shepherd_author="ankj", input_path=source)
            compact = json.loads((root / "work" / "assessment-defaults.json").read_text())
            self.assertTrue(compact["issues"][0]["delegationContext"]["decisionRequired"])
            cycle.finish_cycle(work_dir=root / "work", agent_judgments_path=root / "work" / "agent-judgments.json")
            proposals = json.loads((root / "work" / "action-proposals.json").read_text())
            self.assertEqual(["create-comment"], [p["operation"] for p in proposals["proposals"]])

    def test_incomplete_lifecycle_blocks_even_a_previously_valid_handoff(self) -> None:
        value = handoff_snapshot()
        prepared, compact, judgments, _ = assess(value)
        value["delegationStatus"]["status"] = "incomplete"
        proposals = build_action_proposals(value, prepared, judgments, "ankj", agent_input=compact)
        self.assertEqual([], proposals["proposals"])

    def test_empty_pr_completion_reaches_compact_validated_human_handoff(self) -> None:
        value = handoff_snapshot()
        prepared, compact, judgments, proposals = assess(value)
        context = compact["issues"][0]["delegationContext"]
        self.assertEqual(prepared["issues"][0]["delegationContext"], context)
        self.assertEqual("completed", context["records"][0]["taskState"])
        self.assertEqual(0, context["records"][0]["pullRequests"][0]["changedFiles"])
        self.assertEqual(1, context["records"][0]["handoffReminder"]["ordinal"])
        recommendation = judgments["issues"][0]["recommendations"][0]
        self.assertEqual("ping-human", recommendation["disposition"])
        escalation = recommendation["humanEscalation"]
        self.assertIn("delegat", escalation["whyHuman"].lower())
        self.assertNotIn("Azure", str(escalation))
        self.assertEqual(["create-comment"], [p["operation"] for p in proposals["proposals"]])
        self.assertIn("assignment:21:handoff:reminder-1", proposals["proposals"][0]["actionId"])

    def test_pending_delivery_replay_and_verified_takeover_do_not_create_new_ordinal(self) -> None:
        from tests.test_actions import _with_owned_comment
        from tests.test_actor import ScriptedActorClient
        from ci_shepherd.actor import reconcile_action
        value = handoff_snapshot()
        _, _, _, first = assess(value)
        action = first["proposals"][0]
        for outcome in ("failed", "stale", "indeterminate", "skipped"):
            event = {
                "eventType": "terminal", "outcome": outcome,
                "operation": action["operation"], "actionId": action["actionId"],
                "idempotencyKey": action["idempotencyKey"],
                "target": {"kind": "issue", "number": 21}, "recordedAt": value["collectedAt"],
            }
            _, compact, _, proposals = assess(handoff_snapshot(events=[event]))
            self.assertEqual(1, compact["issues"][0]["delegationContext"]["records"][0]["handoffReminder"]["ordinal"])
            self.assertEqual(action, proposals["proposals"][0])
        client = ScriptedActorClient(issues=[{"number": 21, "updated_at": value["collectedAt"]}], comments=[[{
            "id": 900, "body": action["body"], "user": {"login": "ankj"},
        }]])
        reconciled = reconcile_action(first, action_id=action["actionId"], client=client,
                                      now=lambda: datetime(2026, 8, 19, 16, tzinfo=UTC))
        self.assertEqual("executed", reconciled["outcome"])
        self.assertEqual({"list_comments", "get_authenticated_login", "get_issue"}, {call[0] for call in client.calls})
        delivered = {**event, "outcome": reconciled["outcome"]}
        next_value = handoff_snapshot(events=[delivered])
        _, compact, _, proposals = assess(next_value)
        self.assertEqual([], proposals["proposals"])
        self.assertEqual(2, compact["issues"][0]["delegationContext"]["records"][0]["handoffReminder"]["ordinal"])
        next_value["collectedAt"] = "2026-08-20T16:00:00Z"
        _, _, _, due = assess(next_value)
        self.assertIn("reminder-2", due["proposals"][0]["actionId"])
        _, _, _, replay = assess(_with_owned_comment(value, action["body"]))
        self.assertEqual([], replay["proposals"])
        _, compact, _, takeover = assess(handoff_snapshot(human=True))
        self.assertEqual([], takeover["proposals"])
        self.assertEqual("human-stale-progress", compact["issues"][0]["delegationContext"]["records"][0]["nextWakeup"]["reason"])
        _, compact, _, unknown = assess(handoff_snapshot(changed_files=None))
        self.assertEqual([], unknown["proposals"])
        self.assertFalse(compact["issues"][0]["delegationContext"]["decisionRequired"])


class RecoveryPipelineTests(unittest.TestCase):
    def test_unattributed_exact_test_across_failed_runs_cannot_use_lane_recovery(self) -> None:
        for title in ("Failure in Demo.Tests.Unverified", "[Main CI Failure] Build error CS1002"):
            with self.subTest(title=title):
                value = recovery_snapshot()
                issue = value["evidence"]["issue:21"]["payload"]
                issue["title"] = title
                issue["facts"].append(fact("testName", "Demo.Tests.Unverified"))
                issue["ledger"]["rows"].append({
                    "date": "2026-08-19", "sourceRun": 101,
                    "job": "Tests / Aspire.Hosting.Tests (ubuntu-latest)",
                })
                issue["ledger"].update(parsedRowCount=2, sourceRecordCount=2)
                issue["occurrences"] = copy.deepcopy(issue["ledger"]["rows"])
                second_run = run_payload(run_id=101)
                second_run["referencedBy"] = association(21)
                second_job = job_payload(21, run_id=101, job_id=902)
                second_job["errorMessage"] = "src/Program.cs(1): error CS1002: ; expected"
                value["evidence"].update([
                    evidence("run:101", "workflow-run", second_run),
                    evidence("run:101:attempt:1:job:902", "workflow-job", second_job),
                ])
                validate_snapshot(value)
                prepared, compact, _, _ = assess(value)
                recovery = prepared["issues"][0]["recovery"]
                self.assertEqual("needs-positive-coverage", recovery["status"])
                self.assertEqual([100, 101], [gap["runId"] for gap in recovery["testAttributionGaps"]])
                self.assertEqual(["Demo.Tests.Unverified"] * 2, [gap["testName"] for gap in recovery["testAttributionGaps"]])
                defaults = merge_selected_poc_judgments(compact, build_review_selection(compact), {
                    "schemaVersion": 1, "snapshotId": compact["snapshotId"], "issues": [],
                })
                validate_poc_judgments(prepared, defaults)
                proposals = build_action_proposals(value, prepared, defaults, "ankj", agent_input=compact)
                self.assertEqual([], [p for p in proposals["proposals"] if "review-close" in p["actionId"]])
                close = copy.deepcopy(defaults)
                close["issues"][0]["recommendations"][0].update(
                    disposition="review-close", evidenceIds=recovery["evidenceIds"],
                )
                with self.assertRaisesRegex(ValueError, "review-close"):
                    validate_poc_projectability(compact, close)
                blocked = build_action_proposals(value, prepared, close, "ankj", agent_input=compact)
                self.assertEqual([], blocked["proposals"])
                exact = copy.deepcopy(value)
                del exact["evidence"]["run:100:attempt:1:job:900:log"]
                for evidence_id in ("run:100:attempt:1:job:900", "run:101:attempt:1:job:902"):
                    exact["evidence"][evidence_id]["payload"]["facts"] = [fact("testName", "Demo.Tests.Unverified")]
                exact["evidence"]["run:200:attempt:1:job:901:log"]["payload"]["excerpt"] = "Passed Demo.Tests.Unverified [42 ms]"
                validate_snapshot(exact)
                exact_prepared, exact_compact, exact_defaults, _ = assess(exact)
                exact_recovery = exact_prepared["issues"][0]["recovery"]
                self.assertEqual("verified", exact_recovery["status"])
                self.assertEqual([], exact_recovery["testAttributionGaps"])
                exact_close = copy.deepcopy(exact_defaults["issues"][0])
                exact_close["recommendations"][0].update(
                    disposition="review-close", evidenceIds=exact_recovery["evidenceIds"],
                )
                finalized = merge_selected_poc_judgments(
                    exact_compact, build_review_selection(exact_compact, new_issue_numbers=[21]),
                    {"schemaVersion": 1, "snapshotId": exact_compact["snapshotId"], "issues": [exact_close]},
                )
                exact_proposals = build_action_proposals(exact, exact_prepared, finalized, "ankj", agent_input=exact_compact)
                self.assertEqual(["create-comment", "close-issue"], [p["operation"] for p in exact_proposals["proposals"]])

    def test_mixed_covered_and_uncovered_tests_cannot_recover_issue(self) -> None:
        value = recovery_snapshot(test_name="Demo.Tests.One")
        value["evidence"]["run:100:attempt:1:job:900:log"]["payload"]["excerpt"] += "\nFailed Demo.Tests.Two [42 ms]"
        prepared, _, _, proposals = assess(value)
        self.assertEqual(2, len(prepared["issues"][0]["recovery"]["subjects"]))
        self.assertEqual([], [p for p in proposals["proposals"] if "review-close" in p["actionId"]])

    def test_recovery_judgment_must_retain_all_proof_citations(self) -> None:
        _, compact, judgments, _ = assess(recovery_snapshot())
        judgments["issues"][0]["recommendations"][0]["evidenceIds"] = ["issue:21"]
        with self.assertRaisesRegex(ValueError, "recovery proof"):
            validate_poc_projectability(compact, judgments)

    def test_uncollected_failure_in_same_run_blocks_whole_issue_recovery(self) -> None:
        value = recovery_snapshot()
        value["evidence"]["issue:21"]["payload"]["ledger"]["rows"].append({
            "date": "2026-08-19", "sourceRun": 100, "job": "Tests (windows-latest)",
        })
        _, _, _, proposals = assess(value)
        self.assertEqual([], [p for p in proposals["proposals"] if "review-close" in p["actionId"]])

    def test_successful_workflow_with_skipped_affected_job_cannot_claim_recovery(self) -> None:
        _, _, _, proposals = assess(recovery_snapshot(success="skipped"))
        self.assertEqual(
            [], [p for p in proposals["proposals"] if "review-close" in p["actionId"]],
        )

    def test_proposal_boundary_rechecks_frozen_coverage(self) -> None:
        value = recovery_snapshot()
        prepared, compact, judgments, proposals = assess(value)
        self.assertEqual(["create-comment", "close-issue"],
                         [p["operation"] for p in proposals["proposals"]])
        value["evidence"]["run:200:attempt:1:job:901"]["payload"]["conclusion"] = "skipped"
        proposals = build_action_proposals(value, prepared, judgments, "ankj", agent_input=compact)
        self.assertEqual([], [p for p in proposals["proposals"] if "review-close" in p["actionId"]])

    def test_incomplete_collection_cannot_render_a_recovery_claim(self) -> None:
        value = recovery_snapshot()
        value["evidence"]["run:100"]["payload"]["jobsTruncated"] = True
        _, _, _, proposals = assess(value)
        self.assertEqual([], [p for p in proposals["proposals"] if "review-close" in p["actionId"]])

    def test_exact_test_and_scope_are_required_through_the_entire_pipeline(self) -> None:
        value = recovery_snapshot(test_name="Demo.Tests.Exact")
        _, _, _, proposals = assess(value)
        self.assertEqual(["create-comment", "close-issue"], [p["operation"] for p in proposals["proposals"]])
        self.assertIn("exact test `Demo.Tests.Exact` passed", proposals["proposals"][0]["body"])
        for change in ("no-test", "unknown", "conflict", "newer", "missing-citation", "partial", "wrong-job", "workflow-only"):
            with self.subTest(change=change):
                changed = copy.deepcopy(value)
                records = changed["evidence"]
                if change == "no-test":
                    records["run:200:attempt:1:job:901:log"]["payload"]["excerpt"] = "All tests passed."
                elif change == "unknown":
                    del records["run:100"]["payload"]["headSha"]
                elif change == "conflict":
                    records["issue:21"]["payload"]["ledger"]["rows"][0]["pullRequest"] = 22
                elif change == "newer":
                    records["run:100:attempt:1:job:900"]["payload"]["completedAt"] = "2026-08-19T15:59:00Z"
                elif change == "partial":
                    records["run:100"]["availability"] = "partial"
                elif change == "wrong-job":
                    records["run:200:attempt:1:job:901"]["payload"]["name"] = "Different (ubuntu-latest)"
                elif change == "workflow-only":
                    del records["run:200:attempt:1:job:901"]
                    del records["run:200:attempt:1:job:901:log"]
                _, _, _, denied = assess(changed, max_bundle_records=2 if change == "missing-citation" else 25)
                self.assertEqual([], [p for p in denied["proposals"] if "review-close" in p["actionId"]])

    def test_associated_merged_pr_does_not_override_missing_main_coverage(self) -> None:
        value = recovery_snapshot(success="skipped")
        value["evidence"]["pr:22"] = {
            "kind": "pull-request", "availability": "available",
            "url": "https://github.com/microsoft/aspire/pull/22",
            "payload": {"number": 22, "state": "closed", "mergedAt": "2026-08-19T15:30:01Z",
                        "mergeCommitSha": "a" * 40, "referencedBy": association(21)},
        }
        _, _, _, proposals = assess(value)
        self.assertEqual([], [p for p in proposals["proposals"] if "review-close" in p["actionId"]])

    def test_recovery_owned_comment_replay_does_not_create_another_comment(self) -> None:
        from tests.test_actions import _with_owned_comment
        value = recovery_snapshot()
        _, _, _, first = assess(value)
        value = _with_owned_comment(value, first["proposals"][0]["body"])
        _, _, _, replay = assess(value)
        self.assertEqual(["close-issue"], [p["operation"] for p in replay["proposals"]])


class NetworkFreeConvergenceTests(unittest.TestCase):
    def test_two_unchanged_cycles_do_not_append_work_or_effect_events(self) -> None:
        value = recovery_snapshot(success="skipped")
        policy = load_repository_policy(ASPIRE_REPOSITORY_POLICY_PATH)
        value["repositoryPolicy"] = {**policy.as_public_dict(), "digest": policy.digest}
        prepared, _, judgments, _ = assess(value)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            completed_investigation(root, prepared, judgments)
            state = root / "state"
            before = None
            for index, at in enumerate(("2026-08-19T16:03:00Z", "2026-08-19T16:03:01Z", "2026-08-19T16:03:02Z")):
                value["collectedAt"] = at
                source = root / f"input-{index}.json"
                source.write_text(json.dumps(value))
                work = root / f"cycle-{index}"
                manifest = cycle.start_cycle(repository=value["repository"], state_dir=state, work_dir=work,
                                             checkout=None, shepherd_author="ankj", input_path=source)
                if manifest["stage"] == "awaiting-review":
                    cycle.finish_cycle(work_dir=work, agent_judgments_path=work / "agent-judgments.json")
                plan = json.loads((work / "investigation-plan.json").read_text())
                proposals = json.loads((work / "action-proposals.json").read_text())
                self.assertEqual([], plan["requests"])
                self.assertEqual(1, len(plan["blockedAwaitingEvidence"]))
                self.assertEqual([], proposals["proposals"])
                current = {str(path.relative_to(state)): path.read_bytes() for path in state.rglob("*.jsonl")}
                if before is not None:
                    self.assertEqual(before, current)
                before = current
