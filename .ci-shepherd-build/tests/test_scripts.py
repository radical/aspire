from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from ci_shepherd.collector import CollectionError, InventoryResult
from ci_shepherd.history import HistoryError
from ci_shepherd.models import ValidationError, validate_report, validate_snapshot
from ci_shepherd.poc import build_compact_poc_input
from ci_shepherd.refresh import RefreshPlan


SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "scripts"
SKILL_PATH = Path(__file__).resolve().parents[1] / "SKILL.md"




def poc_prepared(issue_specs: list[tuple[int, str]]) -> dict[str, object]:
    collected_at = "2026-08-20T06:00:00Z"
    repository = "owner/repo"
    return {
        "schemaVersion": 1,
        "repository": repository,
        "sourceCollectedAt": collected_at,
        "snapshotId": f"snapshot:{repository}:{collected_at}",
        "issues": [
            {
                "issueNumber": number,
                "issueUrl": f"https://github.com/{repository}/issues/{number}",
                "title": title,
                "evidenceBundle": [
                    {
                        "id": f"issue:{number}",
                        "kind": "issue-event",
                        "availability": "available",
                        "payload": {"number": number},
                    }
                ],
            }
            for number, title in issue_specs
        ],
    }


def compact_prepared(issue_number: int = 1) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "repository": "owner/repo",
        "sourceCollectedAt": "2026-08-20T06:00:00Z",
        "snapshotId": "snapshot:owner/repo:2026-08-20T06:00:00Z",
        "issues": [
            {
                "issueNumber": issue_number,
                "title": f"Issue {issue_number}",
                "producer": "ci-failure-cause",
                "autoclose": None,
                "ledger": {"parsedRowCount": 1},
                "identity": {
                    "tier1CauseId": None,
                    "tier2TestName": "Namespace.Type.Test",
                    "tier2ExceptionType": None,
                    "tier3ErrorCode": None,
                    "tier3Job": None,
                },
                "candidateState": "resolved",
                "candidateAction": "recommend-close",
                "blockers": [],
                "missingPrerequisites": [],
                "resolutionEvidence": {
                    "pullRequestEvidenceId": f"pr:{issue_number}",
                    "runEvidenceId": f"run:{issue_number}",
                },
                "evidenceBundle": [
                    {
                        "id": f"issue:{issue_number}",
                        "kind": "issue-event",
                        "availability": "available",
                        "payload": {"noise": "x" * 64},
                    },
                    {
                        "id": f"pr:{issue_number}",
                        "kind": "pull-request",
                        "availability": "available",
                        "payload": {"noise": "x" * 64},
                    },
                    {
                        "id": f"run:{issue_number}",
                        "kind": "workflow-run",
                        "availability": "available",
                        "payload": {"noise": "x" * 64},
                    },
                ],
            }
        ],
    }


def poc_judgments(
    prepared: dict[str, object],
    recommendations: list[tuple[int, str, str, str, object, str, list[str], str]],
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "snapshotId": prepared["snapshotId"],
        "issues": [
            {
                "issueNumber": number,
                "category": category,
                "recommendations": [
                    {
                        "disposition": disposition,
                        "target": {"kind": target_kind, "value": target_value},
                        "confidence": confidence,
                        "summary": f"Misleading prose says Watch and No action for {number}.",
                        "evidenceIds": [f"issue:{number}"],
                        "missingEvidence": missing_evidence,
                        "reassessWhen": reassess_when,
                        **(
                            {
                                "humanEscalation": {
                                    "context": f"Issue {number} needs a decision.",
                                    "whyHuman": "Automation cannot make the ownership decision.",
                                    "question": "Who owns the next action?",
                                    "suggestedNextSteps": [
                                        "Choose an owner.",
                                        "Record the decision.",
                                    ],
                                    "routingHint": "area-owner",
                                }
                            }
                            if disposition == "ping-human"
                            else {}
                        ),
                    }
                ],
            }
            for (
                number,
                category,
                disposition,
                target_kind,
                target_value,
                confidence,
                missing_evidence,
                reassess_when,
            ) in recommendations
        ],
    }


def markdown_section(markdown: str, heading: str) -> str:
    marker = f"## {heading}"
    start = markdown.index(marker)
    next_start = markdown.find("\n## ", start + len(marker))
    if next_start == -1:
        return markdown[start:]
    return markdown[start:next_start]


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_ROOT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PrototypeScriptTests(unittest.TestCase):
    def test_default_collect_profile_enables_bounded_supporting_and_run_history(self) -> None:
        collect_script = load_script("collect")
        calls: dict[str, object] = {}

        class FakeCollector:
            def __init__(self, client, repository, now, *, budgets=None, bot_authors=()):
                calls["budgets"] = budgets
                calls["bot_authors"] = bot_authors

            def collect(self, **kwargs):
                calls["collect"] = kwargs
                return InventoryResult([], [], {}, [], [], {})

            def enrich_github_evidence(self, inventory, **kwargs):
                calls["github"] = kwargs
                return inventory

            def enrich_ownership_evidence(self, inventory, **kwargs):
                calls["ownership"] = kwargs
                return inventory

        output_dir = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(output_dir, ignore_errors=True)
        try:
            with (
                patch.object(collect_script, "GitHubClient", return_value=object()),
                patch.object(collect_script, "Collector", FakeCollector),
            ):
                collect_script.collect("owner/repo", output_dir, None)
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)

        self.assertEqual(
            {
                "max_supporting_closed": 20,
                "max_run_refs_per_issue": 12,
                "max_issue_refs_per_issue": 5,
                "max_commit_refs_per_issue": 3,
                "marker_candidates": 3,
                "fact_candidates": 3,
            },
            calls["budgets"],
        )
        self.assertEqual(("github-actions[bot]",), calls["bot_authors"])
        self.assertEqual(
            {"include_supporting": True, "include_timeline": False},
            calls["collect"],
        )
        self.assertEqual(
            {
                "include_issue_references": True,
                "minimal_run_evidence": True,
                "include_run_history": True,
            },
            calls["github"],
        )

    def test_collect_forwards_shepherd_author(self) -> None:
        collect_script = load_script("collect")
        calls: dict[str, object] = {}

        class FakeCollector:
            def __init__(
                self,
                client,
                repository,
                now,
                *,
                budgets=None,
                bot_authors=(),
                shepherd_author=None,
            ):
                calls["shepherd_author"] = shepherd_author

            def collect(self, **kwargs):
                return InventoryResult([], [], {}, [], [], {})

            def enrich_github_evidence(self, inventory, **kwargs):
                return inventory

            def enrich_ownership_evidence(self, inventory, **kwargs):
                return inventory

        output_dir = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(output_dir, ignore_errors=True)
        try:
            with (
                patch.object(collect_script, "GitHubClient", return_value=object()),
                patch.object(collect_script, "Collector", FakeCollector),
            ):
                collect_script.collect(
                    "owner/repo",
                    output_dir,
                    None,
                    shepherd_author="ankj",
                )
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)

        self.assertEqual("ankj", calls["shepherd_author"])

    def test_collect_cli_exposes_per_type_reference_budgets(self) -> None:
        collect_script = load_script("collect")
        output_dir = Path(__file__).parent / ".artifacts" / self._testMethodName
        with (
            patch.object(
                sys,
                "argv",
                [
                    "collect.py",
                    "--repository",
                    "owner/repo",
                    "--output-dir",
                    str(output_dir),
                    "--max-run-refs-per-issue",
                    "9",
                    "--max-issue-refs-per-issue",
                    "4",
                    "--max-commit-refs-per-issue",
                    "2",
                ],
            ),
            patch.object(collect_script, "collect", return_value=output_dir.resolve()) as collect,
        ):
            self.assertEqual(0, collect_script.main())

        collect.assert_called_once_with(
            "owner/repo",
            output_dir,
            None,
            max_run_refs_per_issue=9,
            max_issue_refs_per_issue=4,
            max_commit_refs_per_issue=2,
            state_dir=None,
            full_refresh=False,
            shepherd_author=None,
        )

    def test_collect_cli_exposes_incremental_state_and_full_refresh(self) -> None:
        collect_script = load_script("collect")
        output_dir = Path(__file__).parent / ".artifacts" / self._testMethodName
        state_dir = output_dir / "state"
        with (
            patch.object(
                sys,
                "argv",
                [
                    "collect.py",
                    "--repository",
                    "owner/repo",
                    "--output-dir",
                    str(output_dir),
                    "--state-dir",
                    str(state_dir),
                    "--full-refresh",
                ],
            ),
            patch.object(collect_script, "collect", return_value=output_dir.resolve()) as collect,
        ):
            self.assertEqual(0, collect_script.main())

        collect.assert_called_once_with(
            "owner/repo",
            output_dir,
            None,
            max_run_refs_per_issue=12,
            max_issue_refs_per_issue=5,
            max_commit_refs_per_issue=3,
            state_dir=state_dir,
            full_refresh=True,
            shepherd_author=None,
        )

    def test_collect_with_missing_state_runs_full_live_collection(self) -> None:
        collect_script = load_script("collect")
        calls: list[str] = []

        class FakeCollector:
            def __init__(self, client, repository, now, *, budgets=None, bot_authors=()):
                pass

            def collect(self, **kwargs):
                calls.append("collect")
                return InventoryResult([], [], {}, [], [], {})

            def collect_incremental(self, *args, **kwargs):
                calls.append("collect_incremental")
                raise AssertionError("Missing state must use a full live collection.")

            def enrich_github_evidence(self, inventory, **kwargs):
                return inventory

            def enrich_ownership_evidence(self, inventory, **kwargs):
                return inventory

        artifact_root = Path(__file__).parent / ".artifacts" / self._testMethodName
        output_dir = artifact_root / "output"
        state_dir = artifact_root / "missing-state"
        shutil.rmtree(artifact_root, ignore_errors=True)
        try:
            with (
                patch.object(collect_script, "GitHubClient", return_value=object()),
                patch.object(collect_script, "Collector", FakeCollector),
            ):
                result = collect_script.collect(
                    "owner/repo",
                    output_dir,
                    None,
                    state_dir=state_dir,
                )

            self.assertEqual(output_dir.resolve(), result)
            self.assertEqual(["collect"], calls)
            self.assertFalse(state_dir.exists())
            validate_snapshot(json.loads((output_dir / "input.json").read_text()))
        finally:
            shutil.rmtree(artifact_root, ignore_errors=True)

    def test_collect_records_durable_stage_progress(self) -> None:
        collect_script = load_script("collect")

        class FakeCollector:
            def __init__(self, client, repository, now, *, budgets=None, bot_authors=()):
                pass

            def collect(self, **kwargs):
                return InventoryResult([], [], {}, [], [], {})

            def enrich_github_evidence(self, inventory, **kwargs):
                return inventory

            def enrich_ownership_evidence(self, inventory, **kwargs):
                return inventory

        output_dir = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(output_dir, ignore_errors=True)
        try:
            with (
                patch.object(collect_script, "GitHubClient", return_value=object()),
                patch.object(collect_script, "Collector", FakeCollector),
            ):
                collect_script.collect("owner/repo", output_dir, None)

            progress = json.loads((output_dir / "progress.json").read_text())
            self.assertEqual("complete", progress["status"])
            self.assertEqual("complete", progress["currentStage"])
            self.assertEqual(
                [
                    ("collection", "started"),
                    ("inventory", "started"),
                    ("inventory", "completed"),
                    ("github-enrichment", "started"),
                    ("github-enrichment", "completed"),
                    ("ownership-enrichment", "started"),
                    ("ownership-enrichment", "completed"),
                    ("write-artifacts", "started"),
                    ("write-artifacts", "completed"),
                    ("collection", "completed"),
                ],
                [(event["stage"], event["status"]) for event in progress["events"]],
            )
            self.assertEqual(0o600, (output_dir / "progress.json").stat().st_mode & 0o777)
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)

    def test_collect_records_failed_stage_before_reraising(self) -> None:
        collect_script = load_script("collect")

        class FakeCollector:
            def __init__(self, client, repository, now, *, budgets=None, bot_authors=()):
                pass

            def collect(self, **kwargs):
                raise RuntimeError("inventory failed")

        output_dir = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(output_dir, ignore_errors=True)
        try:
            with (
                patch.object(collect_script, "GitHubClient", return_value=object()),
                patch.object(collect_script, "Collector", FakeCollector),
            ):
                with self.assertRaisesRegex(RuntimeError, "inventory failed"):
                    collect_script.collect("owner/repo", output_dir, None)

            progress = json.loads((output_dir / "progress.json").read_text())
            self.assertEqual("failed", progress["status"])
            self.assertEqual("inventory", progress["currentStage"])
            self.assertEqual("RuntimeError: inventory failed", progress["error"])
            self.assertEqual(("inventory", "failed"), (
                progress["events"][-1]["stage"],
                progress["events"][-1]["status"],
            ))
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)

    def test_collect_with_corrupt_state_fails_closed_with_history_error(self) -> None:
        collect_script = load_script("collect")

        class FakeCollector:
            def __init__(self, client, repository, now, *, budgets=None, bot_authors=()):
                pass

            def collect(self, **kwargs):
                raise AssertionError("Corrupt state must not fall back to live collection.")

            def collect_incremental(self, *args, **kwargs):
                raise AssertionError("Corrupt state must not start incremental collection.")

        artifact_root = Path(__file__).parent / ".artifacts" / self._testMethodName
        output_dir = artifact_root / "output"
        state_dir = artifact_root / "state"
        shutil.rmtree(artifact_root, ignore_errors=True)
        state_dir.mkdir(mode=0o700, parents=True)
        (state_dir / "current.json").write_text("{corrupt", encoding="utf-8")
        try:
            with (
                patch.object(collect_script, "GitHubClient", return_value=object()),
                patch.object(collect_script, "Collector", FakeCollector),
            ):
                with self.assertRaisesRegex(HistoryError, "without any immutable runs"):
                    collect_script.collect(
                        "owner/repo",
                        output_dir,
                        None,
                        state_dir=state_dir,
                    )
        finally:
            shutil.rmtree(artifact_root, ignore_errors=True)

    def test_snapshot_emits_deterministic_refresh_summary(self) -> None:
        collect_script = load_script("collect")
        inventory = InventoryResult(
            open_issues=[],
            supporting_issues=[],
            evidence={},
            collection_errors=[],
            warnings=[],
            references={},
            refresh_plan=RefreshPlan(
                reuse=("issue:2", "issue:1"),
                refresh=("run:2",),
                retry=("run:3",),
                retire=("issue:9",),
                new_issues=(5, 4),
                changed_issues=(3,),
            ),
        )

        snapshot = collect_script.build_snapshot(
            "owner/repo",
            collect_script.datetime(2026, 8, 17, 22, 0, tzinfo=collect_script.UTC),
            inventory,
        )

        self.assertEqual(
            {
                "reusedEvidenceIds": ["issue:1", "issue:2"],
                "refreshedEvidenceIds": ["run:2"],
                "retriedEvidenceIds": ["run:3"],
                "retiredEvidenceIds": ["issue:9"],
                "newIssueNumbers": [4, 5],
                "changedIssueNumbers": [3],
            },
            snapshot["refreshSummary"],
        )
        validate_snapshot(snapshot)

    def test_skill_documents_thin_poc_artifacts_and_commands(self) -> None:
        skill = SKILL_PATH.read_text()

        for artifact in (
            "input.json",
            "assessment-input.json",
            "related-issues.json",
            "agent-input.json",
            "agent-judgments.json",
            "judgments.json",
            "report.md",
            "progress.json",
            "api-calls.jsonl",
        ):
            with self.subTest(artifact=artifact):
                self.assertIn(artifact, skill)

        validate_command = (
            'python3 "$CI_SHEPHERD_ROOT/scripts/validate.py" \\\n'
            '  --prepared "$SCRATCH/assessment-input.json" \\\n'
            '  --judgments "$SCRATCH/judgments.json"'
        )
        compact_command = (
            'python3 "$CI_SHEPHERD_ROOT/scripts/compact.py" \\\n'
            '  --prepared "$SCRATCH/assessment-input.json" \\\n'
            '  --related-issues "$FIXTURE/related-issues.json" \\\n'
            '  --fingerprints "$STATE/ledgers/fingerprints.jsonl" \\\n'
            '  --output "$SCRATCH/agent-input.json"'
        )
        render_command = (
            'python3 "$CI_SHEPHERD_ROOT/scripts/render.py" \\\n'
            '  --prepared "$SCRATCH/assessment-input.json" \\\n'
            '  --judgments "$SCRATCH/judgments.json" \\\n'
            '  --output "$SCRATCH/report.md"'
        )
        finalize_command = (
            'python3 "$CI_SHEPHERD_ROOT/scripts/finalize.py" \\\n'
            '  --agent-input "$SCRATCH/agent-input.json" \\\n'
            '  --agent-judgments "$SCRATCH/agent-judgments.json" \\\n'
            '  --output "$SCRATCH/judgments.json"'
        )
        self.assertIn(validate_command, skill)
        self.assertIn(compact_command, skill)
        self.assertIn(finalize_command, skill)
        self.assertIn(render_command, skill)

    def test_skill_documents_thin_poc_fresh_agent_constraints(self) -> None:
        normalized_skill = " ".join(SKILL_PATH.read_text().split())

        for phrase in (
            "The compact handoff is generated by `compact.py` from `assessment-input.json`.",
            "A fresh assessment agent reads only `agent-input.json`.",
            "Copy each `defaultJudgment` into `agent-judgments.json` and make only evidence-supported overrides.",
            "Deterministic defaults already apply the safe recurrence rubric.",
            "Spend agent reasoning on `unknown` and `investigate` defaults.",
            "Do not rewrite already-safe queues merely to produce overrides.",
            "Process issues in batches of at most 10.",
            "Batches of at most 10 are a reasoning grouping, not separate file loads.",
            "Process the entire file once; do not reparse per batch.",
            "Write only `agent-judgments.json`.",
            "A `watch` recommendation must name its `watchReason` and the exact evidence event that ends the watch.",
            "Review every issue where `reviewRequired` is `true`.",
            "Aggregate `clusterOccurrenceSummary` only when the listed relationship and failure symptoms are compatible.",
            "A generic exit code with unavailable logs is an investigation, not a watch.",
            "Two independent test failures on one day do not justify quarantine, but they do justify investigation.",
            "A repeated deterministic HTTP 404 is a product or tooling investigation, not transient infrastructure.",
            "Missing machine-fetchable evidence is `investigate`, not `ping-human`.",
            "`ping-human` is reserved for a decision, permission, ownership, or access question only a person can answer.",
            "Every `ping-human` recommendation must include `humanEscalation`",
            "`context`, `whyHuman`, `question`, `suggestedNextSteps`, and `routingHint`",
            "The rendered draft comment must begin with `[automated]`",
            "Group bot-authored gh-aw failure issues by the stable `workflow_id`",
            "Treat each generated issue as an occurrence of that workflow failure, not as an independent cause.",
            "An expired gh-aw failure issue that remains open after later successful runs is a closure candidate and evidence of a producer lifecycle defect.",
            "Evaluate `actionCluster` before evaluating individual issue rows.",
            "A canonical recommendation must name the shared target and superseded issue records.",
            "Duplicate closure is not recovery",
            "useful investigation work can happen now",
            "only a named future event can change the decision",
            "Offline prompt iterations must start from a frozen `assessment-input.json` and must not rerun collection.",
            "Prefer `unknown` or `investigate` over unsupported certainty.",
            "Do not recommend quarantine from `occurrenceCount` alone.",
            "at least two independent runs on at least two distinct days",
            "Use `independentRunCount`, `distinctDayCount`, and the normalized identity",
            "Do not ping a human solely because an issue is old.",
            "A single transient occurrence remains `transient-infrastructure` and `watch`.",
            "Report the number of defaults changed",
            "Distinguish same-run reruns from independent recovery.",
            "Surface missing positive execution coverage.",
            "Quarantine, retry, rerun, and closure recommendations are review-only.",
            "Use multiple recommendations for one issue only when the targets differ.",
            "The fresh assessment agent must never access GitHub",
            "Neither role may write to GitHub",
            "The coordinator may use the existing GET-only collector",
            "The coordinator collects, prepares, validates, renders, and records artifacts.",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, normalized_skill)

    def test_skill_documents_one_round_evidence_verification_contract(self) -> None:
        normalized_skill = " ".join(SKILL_PATH.read_text().split())

        for phrase in (
            "evidence-requests.round-1.json",
            "input.round-1.json",
            "assessment-input.round-1.json",
            "agent-input.round-1.json",
            "one expansion round",
            "`issue-reference` and `workflow-run` only",
            "Do not include preliminary judgments in verifier input.",
            "Do not investigate root cause.",
            "The request-planning agent emits no judgments.",
            "at most 25 requests",
            "`partial` or `not-enriched`",
            'Use exactly this document shape; do not add `snapshotId`',
            '"schemaVersion": 1, "repository": "microsoft/aspire", "round": 1, "requests":',
            "EVIDENCE_REQUEST_DECISION_GATES",
            "merged-fix recovery post-fix-green no-newer-matching-failure no-recent-matching-failure canonical-issue canonical-search-complete obsolete-surface current-failing-run prior-resolved-episode",
            "The fresh assessment agent receives no preliminary judgments",
            "Emit a bounded investigation handoff",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, normalized_skill)

    def test_skill_documents_watch_action_contract(self) -> None:
        normalized_skill = " ".join(SKILL_PATH.read_text().split())

        for phrase in (
            "canonical CI shepherd status comment",
            "Every issue- or pull-request-visible effect requires individual user approval.",
            "All automatically posted GitHub text starts with `[automated] `.",
            "Shepherd-authored status comments must not contribute markers, facts, or references.",
            "An unchanged watch state must not create or edit a comment.",
            "The assessment agent never executes actions.",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, normalized_skill)

    def test_skill_defines_artifact_pipeline_regression_protocol(self) -> None:
        normalized_skill = " ".join(SKILL_PATH.read_text().split())

        for phrase in (
            "Validated `judgments.json` is the only decision source.",
            "Do not substitute conversation-side analysis.",
            "`action-proposals.json` is the only source of external effects.",
            "Locate the earliest incorrect artifact and replay from frozen input.",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, normalized_skill)

    def test_skill_documents_poc_lifecycle_recording_and_replay(self) -> None:
        skill = SKILL_PATH.read_text()
        normalized_skill = " ".join(skill.split())

        for phrase in (
            "`record_poc.py` records the finalized POC cycle",
            "`case-events.jsonl` records bootstrap and material disposition transitions",
            "Expanded evidence rounds use a round-qualified snapshot identity",
            "replaying unchanged evidence must append no case event",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, normalized_skill)

        self.assertIn(
            'python3 "$CI_SHEPHERD_ROOT/scripts/record_poc.py" \\\n'
            '  --state-dir "$STATE" \\\n'
            '  --input "$SCRATCH/input.round-1.json" \\\n'
            '  --prepared "$SCRATCH/assessment-input.round-1.json" \\\n'
            '  --judgments "$SCRATCH/judgments.json" \\\n'
            '  --report "$SCRATCH/report.md" \\\n'
            '  --artifacts "$SCRATCH"',
            skill,
        )
        self.assertIn(
            'python3 "$CI_SHEPHERD_ROOT/scripts/replay_scenario.py" \\\n'
            '  --scenario-dir "$SCENARIO" \\\n'
            '  --output-dir "$REPLAY" \\\n'
            '  --state-dir "$STATE"',
            skill,
        )

    def test_propose_actions_cli_writes_owner_only_output(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX mode assertions are not portable to Windows")
        propose_script = load_script("propose_actions")
        scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True)
        snapshot_path = scratch / "input.json"
        prepared_path = scratch / "prepared.json"
        agent_input_path = scratch / "agent-input.json"
        judgments_path = scratch / "judgments.json"
        output_path = scratch / "action-proposals.json"
        snapshot = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "collectedAt": "2026-08-21T16:00:00Z",
            "openIssues": [21],
            "issues": [
                {
                    "number": 21,
                    "title": "One failure",
                    "labels": ["ci-failure-cause"],
                }
            ],
            "evidence": {
                "issue:21": {
                    "kind": "issue-event",
                    "url": "https://github.com/owner/repo/issues/21",
                    "collectedAt": "2026-08-21T16:00:00Z",
                    "availability": "available",
                    "payload": {"number": 21, "state": "open"},
                }
            },
            "collectionErrors": [],
            "warnings": [],
        }
        snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
        prepared_path.write_text("{}", encoding="utf-8")
        agent_input_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "snapshotId": (
                        "snapshot:owner/repo:2026-08-21T16:00:00Z"
                    ),
                    "repository": "owner/repo",
                    "issues": [],
                }
            ),
            encoding="utf-8",
        )
        judgments_path.write_text("{}", encoding="utf-8")
        proposals = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "snapshotId": "snapshot:owner/repo:2026-08-21T16:00:00Z",
            "shepherdAuthor": "ankj",
            "proposals": [
                {
                    "issueNumber": 21,
                    "operation": "create-comment",
                }
            ],
            "unchangedIssueNumbers": [],
        }
        try:
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "propose_actions.py",
                        "--snapshot",
                        str(snapshot_path),
                        "--prepared",
                        str(prepared_path),
                        "--agent-input",
                        str(agent_input_path),
                        "--judgments",
                        str(judgments_path),
                        "--shepherd-author",
                        "ankj",
                        "--output",
                        str(output_path),
                    ],
                ),
                patch.object(
                    propose_script,
                    "build_action_proposals",
                    return_value=proposals,
                ),
            ):
                self.assertEqual(0, propose_script.main())

            document = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(
                "create-comment",
                document["proposals"][0]["operation"],
            )
            self.assertEqual(0o600, output_path.stat().st_mode & 0o777)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_execute_actions_defaults_to_dry_run_without_github_access(self) -> None:
        execute_script = load_script("execute_actions")
        scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True)
        proposals_path = scratch / "action-proposals.json"
        results_path = scratch / "action-results.json"
        proposals_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "repository": "owner/repo",
                    "snapshotId": "snapshot:owner/repo:1",
                    "shepherdAuthor": "ankj",
                    "proposals": [
                        {
                            "actionId": "action:1",
                            "issueNumber": 21,
                            "issueUrl": "https://github.com/owner/repo/issues/21",
                            "operation": "create-comment",
                            "idempotencyKey": "issue:21:watch",
                            "body": (
                                "[automated] Watching.\n\n"
                                "<!-- ci-shepherd:idempotency-key=issue:21:watch -->"
                            ),
                            "evidenceIds": ["issue:21"],
                            "expectedIssueState": "open",
                            "requiresSeparateApproval": True,
                        }
                    ],
                    "unchangedIssueNumbers": [],
                }
            ),
            encoding="utf-8",
        )
        stdout = io.StringIO()
        try:
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "execute_actions.py",
                        "--proposals",
                        str(proposals_path),
                        "--results",
                        str(results_path),
                    ],
                ),
                patch.object(execute_script, "GitHubActorClient") as client_factory,
                contextlib.redirect_stdout(stdout),
            ):
                self.assertEqual(0, execute_script.main())

            self.assertFalse(client_factory.called)
            self.assertFalse(results_path.exists())
            rendered = json.loads(stdout.getvalue())
            self.assertEqual("dry-run", rendered["mode"])
            self.assertEqual("action:1", rendered["actions"][0]["actionId"])
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_execute_actions_rejects_execute_without_action_id(self) -> None:
        execute_script = load_script("execute_actions")
        with (
            patch.object(
                sys,
                "argv",
                [
                    "execute_actions.py",
                    "--proposals",
                    "action-proposals.json",
                    "--results",
                    "action-results.json",
                    "--execute",
                ],
            ),
            self.assertRaises(SystemExit) as raised,
        ):
            execute_script.main()

        self.assertEqual(2, raised.exception.code)

    def test_execute_actions_appends_owner_only_execution_result(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX mode assertions are not portable to Windows")
        execute_script = load_script("execute_actions")
        scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True)
        proposals_path = scratch / "action-proposals.json"
        results_path = scratch / "action-results.json"
        proposals = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "proposals": [],
        }
        proposals_path.write_text(json.dumps(proposals), encoding="utf-8")
        terminal_result = {
            "actionId": "action:1",
            "attemptedAt": "2026-08-21T20:00:00Z",
            "outcome": "executed",
        }
        stdout = io.StringIO()
        try:
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "execute_actions.py",
                        "--proposals",
                        str(proposals_path),
                        "--results",
                        str(results_path),
                        "--action-id",
                        "action:1",
                        "--execute",
                    ],
                ),
                patch.object(execute_script, "GitHubActorClient", return_value=object()),
                patch.object(
                    execute_script,
                    "execute_action",
                    return_value=terminal_result,
                ),
                contextlib.redirect_stdout(stdout),
            ):
                self.assertEqual(0, execute_script.main())

            document = json.loads(results_path.read_text(encoding="utf-8"))
            self.assertEqual([terminal_result], document["results"])
            self.assertEqual(0o600, results_path.stat().st_mode & 0o777)
            self.assertEqual(terminal_result, json.loads(stdout.getvalue()))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_skill_deprecates_legacy_report_json_final_agent_path(self) -> None:
        skill = SKILL_PATH.read_text()
        normalized_skill = " ".join(skill.split())

        self.assertIn("Legacy `report.json` final-agent flow is deprecated", normalized_skill)
        self.assertNotIn("Write only `report.json`", skill)
        self.assertNotIn("writes only `report.json`", normalized_skill)
        self.assertNotIn('--report "$SCRATCH/report.json"', skill)

    def test_skill_documents_dry_run_actor_contract(self) -> None:
        normalized_skill = " ".join(SKILL_PATH.read_text().split())

        self.assertIn("The actor is dry-run by default.", normalized_skill)
        self.assertIn(
            "`--execute` requires one exact `--action-id`.",
            normalized_skill,
        )
        self.assertIn("Dry-run performs no GitHub access", normalized_skill)
        self.assertIn(
            "The actor never reinterprets `judgments.json`",
            normalized_skill,
        )

    def test_compact_script_writes_owner_only_output(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX mode assertions are not portable to Windows")
        compact_script = load_script("compact")
        scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True, mode=0o755)
        prepared_path = scratch / "assessment-input.json"
        related_path = scratch / "related-issues.json"
        output_path = scratch / "agent-input.json"
        prepared = compact_prepared()
        prepared_path.write_text(json.dumps(prepared), encoding="utf-8")
        related_path.write_text(
            json.dumps(
                [
                    {
                        "source": 1,
                        "test": "Namespace.Type.Test",
                        "hits": [
                            {
                                "number": 900,
                                "title": "[Failing test]: Namespace.Type.Test",
                                "state": "OPEN",
                                "url": "https://github.com/owner/repo/issues/900",
                                "labels": {"nodes": [{"name": "failing-test"}]},
                            }
                        ],
                    }
                ]
            ),
            encoding="utf-8",
        )
        try:
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "compact.py",
                        "--prepared",
                        str(prepared_path),
                        "--related-issues",
                        str(related_path),
                        "--output",
                        str(output_path),
                    ],
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(0, compact_script.main())

            compact = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(0o700, output_path.parent.stat().st_mode & 0o777)
            self.assertEqual(0o600, output_path.stat().st_mode & 0o777)
            self.assertEqual({"schemaVersion", "snapshotId", "issues"}, set(compact))
            self.assertNotIn("payload", compact["issues"][0]["allowedEvidence"][0])
            self.assertEqual(
                "same-test-tracker",
                compact["issues"][0]["relatedIssues"][0]["relationship"],
            )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_compact_script_merges_fingerprint_history(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX mode assertions are not portable to Windows")
        compact_script = load_script("compact")
        scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True, mode=0o755)
        prepared_path = scratch / "assessment-input.json"
        fingerprints_path = scratch / "fingerprints.jsonl"
        output_path = scratch / "agent-input.json"
        prepared = compact_prepared()
        prepared_path.write_text(json.dumps(prepared), encoding="utf-8")
        # A recurrence recorded on a run this issue's own ledger never saw
        # -- e.g. observed while an earlier, now-closed issue was open.
        history_row = {
            "fingerprint": "test:namespace.type.test",
            "issueNumber": 900,
            "runId": 4242,
            "attempt": 1,
            "date": "2026-08-01",
            "job": "Tests / Linux",
            "testName": "Namespace.Type.Test",
        }
        fingerprints_path.write_text(json.dumps(history_row, sort_keys=True) + "\n", encoding="utf-8")
        try:
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "compact.py",
                        "--prepared",
                        str(prepared_path),
                        "--fingerprints",
                        str(fingerprints_path),
                        "--output",
                        str(output_path),
                    ],
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(0, compact_script.main())

            compact = json.loads(output_path.read_text(encoding="utf-8"))
            history_summary = compact["issues"][0]["historyOccurrenceSummary"]
            self.assertEqual(1, history_summary["independentRunCount"])
            self.assertEqual("2026-08-01", history_summary["firstSeenDate"])
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_compact_script_treats_missing_state_ledger_as_empty_history(self) -> None:
        compact_script = load_script("compact")
        scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(scratch, ignore_errors=True)
        prepared_path = scratch / "assessment-input.json"
        output_path = scratch / "agent-input.json"
        fingerprints_path = scratch / "state" / "ledgers" / "fingerprints.jsonl"
        scratch.mkdir(parents=True)
        prepared_path.write_text(
            json.dumps(compact_prepared()),
            encoding="utf-8",
        )

        try:
            compact_script.compact(
                prepared_path=prepared_path,
                related_issues_path=None,
                fingerprints_path=fingerprints_path,
                output_path=output_path,
            )

            self.assertTrue(output_path.is_file())
            self.assertFalse(fingerprints_path.exists())
            self.assertFalse(fingerprints_path.parent.exists())
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_fingerprints_script_appends_ledger_rows(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX mode assertions are not portable to Windows")
        fingerprints_script = load_script("fingerprints")
        scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True, mode=0o755)
        prepared_path = scratch / "assessment-input.json"
        output_path = scratch / "state" / "fingerprints.jsonl"
        prepared = compact_prepared(101)
        prepared["issues"][0]["ledger"] = {
            "parsedRowCount": 1,
            "rows": [
                {
                    "sourceRun": 1001,
                    "attempt": 1,
                    "date": "2026-08-17",
                    "job": "Tests / Linux",
                }
            ],
        }
        prepared_path.write_text(json.dumps(prepared), encoding="utf-8")
        try:
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "fingerprints.py",
                        "--prepared",
                        str(prepared_path),
                        "--output",
                        str(output_path),
                    ],
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(0, fingerprints_script.main())

            self.assertEqual(0o700, output_path.parent.stat().st_mode & 0o777)
            self.assertEqual(0o600, output_path.stat().st_mode & 0o777)
            rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(
                [
                    {
                        "fingerprint": "test:namespace.type.test",
                        "issueNumber": 101,
                        "runId": 1001,
                        "attempt": 1,
                        "date": "2026-08-17",
                        "job": "Tests / Linux",
                        "testName": "Namespace.Type.Test",
                    }
                ],
                rows,
            )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_finalize_script_preserves_safe_defaults(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX mode assertions are not portable to Windows")
        finalize_script = load_script("finalize")
        scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True, mode=0o755)
        compact_path = scratch / "agent-input.json"
        agent_path = scratch / "agent-judgments.json"
        output_path = scratch / "judgments.json"
        try:
            compact = build_compact_poc_input(compact_prepared(410))
            compact_path.write_text(json.dumps(compact))
            agent_judgment = {
                "schemaVersion": 1,
                "snapshotId": compact["snapshotId"],
                "issues": [
                    {
                        **compact["issues"][0]["defaultJudgment"],
                        "category": "unknown",
                    }
                ],
            }
            agent_path.write_text(json.dumps(agent_judgment))

            with patch.object(
                sys,
                "argv",
                [
                    "finalize.py",
                    "--agent-input",
                    str(compact_path),
                    "--agent-judgments",
                    str(agent_path),
                    "--output",
                    str(output_path),
                ],
            ):
                self.assertEqual(0, finalize_script.main())

            finalized = json.loads(output_path.read_text())
            self.assertEqual(
                compact["issues"][0]["defaultJudgment"],
                finalized["issues"][0],
            )
            self.assertEqual(0o600, output_path.stat().st_mode & 0o777)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_validate_script_accepts_poc_judgments(self) -> None:
        validate_script = load_script("validate")
        scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True)
        prepared_path = scratch / "assessment-input.json"
        judgments_path = scratch / "judgments.json"
        prepared = poc_prepared([(1, "First failure")])
        judgments = poc_judgments(
            prepared,
            [(1, "flaky-test", "review-quarantine", "issue", 1, "high", [], "After a clean run.")],
        )
        prepared_path.write_text(json.dumps(prepared), encoding="utf-8")
        judgments_path.write_text(json.dumps(judgments), encoding="utf-8")
        try:
            stdout = io.StringIO()
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "validate.py",
                        "--prepared",
                        str(prepared_path),
                        "--judgments",
                        str(judgments_path),
                    ],
                ),
                contextlib.redirect_stdout(stdout),
            ):
                self.assertEqual(0, validate_script.main())

            self.assertEqual("valid", stdout.getvalue().strip())
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_validate_script_rejects_active_legacy_cli_mode(self) -> None:
        validate_script = load_script("validate")
        scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True)
        input_path = scratch / "input.json"
        report_path = scratch / "report.json"
        input_path.write_text("{}", encoding="utf-8")
        report_path.write_text("{}", encoding="utf-8")
        try:
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "validate.py",
                        "--input",
                        str(input_path),
                        "--report",
                        str(report_path),
                    ],
                ),
                patch.object(validate_script, "validate_snapshot", create=True) as validate_snapshot_call,
                patch.object(validate_script, "validate_report", create=True) as validate_report_call,
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as context,
            ):
                validate_script.main()

            self.assertEqual(2, context.exception.code)
            validate_snapshot_call.assert_not_called()
            validate_report_call.assert_not_called()
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_validate_script_rejects_partial_or_mixed_cli_modes(self) -> None:
        validate_script = load_script("validate")

        cases = (
            ["validate.py", "--prepared", "assessment-input.json"],
            [
                "validate.py",
                "--prepared",
                "assessment-input.json",
                "--judgments",
                "judgments.json",
                "--input",
                "input.json",
            ],
            ["validate.py", "--input", "input.json"],
        )
        for argv in cases:
            with self.subTest(argv=argv), patch.object(sys, "argv", argv):
                with (
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as context,
                ):
                    validate_script.main()
                self.assertEqual(2, context.exception.code)

    def test_render_script_rejects_partial_or_mixed_cli_modes(self) -> None:
        render_script = load_script("render")

        cases = (
            ["render.py", "--prepared", "assessment-input.json", "--output", "report.md"],
            [
                "render.py",
                "--prepared",
                "assessment-input.json",
                "--judgments",
                "judgments.json",
                "--input",
                "input.json",
                "--output",
                "report.md",
            ],
            ["render.py", "--input", "input.json", "--output", "report.md"],
        )
        for argv in cases:
            with self.subTest(argv=argv), patch.object(sys, "argv", argv):
                with (
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as context,
                ):
                    render_script.main()
                self.assertEqual(2, context.exception.code)

    def test_render_script_rejects_active_legacy_cli_mode(self) -> None:
        render_script = load_script("render")
        scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True)
        input_path = scratch / "input.json"
        report_path = scratch / "report.json"
        output_path = scratch / "report.md"
        input_path.write_text("{}", encoding="utf-8")
        report_path.write_text("{}", encoding="utf-8")
        try:
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "render.py",
                        "--input",
                        str(input_path),
                        "--report",
                        str(report_path),
                        "--output",
                        str(output_path),
                    ],
                ),
                patch.object(
                    render_script,
                    "render_markdown",
                    return_value="# legacy",
                    create=True,
                ) as render_markdown_call,
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as context,
            ):
                render_script.main()

            self.assertEqual(2, context.exception.code)
            render_markdown_call.assert_not_called()
            self.assertFalse(output_path.exists())
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_render_script_renders_poc_queues_counts_and_columns(self) -> None:
        render_script = load_script("render")
        issue_specs = [(number, f"Issue {number}") for number in range(1, 9)]
        prepared = poc_prepared(issue_specs)
        judgments = poc_judgments(
            prepared,
            [
                (1, "flaky-test", "investigate", "issue", 1, "low", ["failed log"], "After logs are collected."),
                (2, "transient-infrastructure", "watch", "workflow-run", "100", "medium", [], "After the next scheduled run."),
                (3, "automation-tracker", "ping-human", "issue", 3, "high", [], "After owner review."),
                (4, "flaky-test", "review-quarantine", "test", "Namespace.Type.Test", "high", [], "After quarantine review."),
                (5, "transient-infrastructure", "review-retry", "failure-fingerprint", "abc123", "medium", [], "After retry review."),
                (6, "blocking-build", "review-rerun", "workflow-run", "200", "medium", [], "After rerun review."),
                (7, "product-or-tooling", "review-close", "issue", 7, "low", ["post-fix green run"], "After positive coverage exists."),
                (8, "unknown", "no-action", "issue", 8, "low", [], "When new evidence appears."),
            ],
        )

        markdown = render_script.render_poc_markdown(
            prepared,
            judgments,
            prepared_path=Path("assessment-input.json"),
        )

        for heading in (
            "Investigate",
            "Watch",
            "Needs human",
            "Quarantine review",
            "Retry review",
            "Rerun review",
            "Closure review",
            "No action",
        ):
            with self.subTest(heading=heading):
                self.assertIn(f"## {heading}", markdown)

        self.assertIn(
            "| Issue | Category | Target | Confidence | Summary | Evidence | Missing evidence | Reassess when |",
            markdown,
        )
        self.assertIn("### Category counts", markdown)
        self.assertIn("| flaky-test | 2 |", markdown)
        self.assertIn("| transient-infrastructure | 2 |", markdown)
        self.assertIn("### Disposition counts", markdown)
        self.assertIn("| review-quarantine | 1 |", markdown)
        self.assertIn("### Confidence counts", markdown)
        self.assertIn("| medium | 3 |", markdown)
        self.assertIn("`failed log`", markdown)
        self.assertIn("After positive coverage exists.", markdown)
        needs_human = markdown_section(markdown, "Needs human")
        self.assertIn("### Draft comment for #3", needs_human)
        self.assertIn("[automated] Issue 3 needs a decision.", needs_human)
        self.assertIn("**Why human input is needed:**", needs_human)
        self.assertIn("**Decision needed:** Who owns the next action?", needs_human)
        self.assertIn("- Choose an owner.", needs_human)
        self.assertIn("**Routing hint:** `area-owner`", needs_human)

        quarantine_summary = "Misleading prose says Watch and No action for 4."
        self.assertEqual(1, markdown.count(quarantine_summary))
        self.assertIn(quarantine_summary, markdown_section(markdown, "Quarantine review"))
        self.assertNotIn(quarantine_summary, markdown_section(markdown, "Watch"))
        self.assertNotIn(quarantine_summary, markdown_section(markdown, "No action"))

    def test_render_poc_category_counts_count_issue_once_with_multiple_recommendations(self) -> None:
        render_script = load_script("render")
        prepared = poc_prepared([(1, "First failure")])
        recommendations = [
            {
                "disposition": "review-quarantine",
                "target": {"kind": "test", "value": "Namespace.Type.Test"},
                "confidence": "high",
                "summary": "Review the test for quarantine.",
                "evidenceIds": ["issue:1"],
                "missingEvidence": [],
                "reassessWhen": "After quarantine review.",
            },
            {
                "disposition": "review-rerun",
                "target": {"kind": "workflow-run", "value": "12345"},
                "confidence": "medium",
                "summary": "Review the workflow run for rerun.",
                "evidenceIds": ["issue:1"],
                "missingEvidence": ["positive execution coverage"],
                "reassessWhen": "After rerun review.",
            },
        ]
        judgments = {
            "schemaVersion": 1,
            "snapshotId": prepared["snapshotId"],
            "issues": [
                {
                    "issueNumber": 1,
                    "category": "flaky-test",
                    "recommendations": recommendations,
                }
            ],
        }

        markdown = render_script.render_poc_markdown(
            prepared,
            judgments,
            prepared_path=Path("assessment-input.json"),
        )

        self.assertIn("| flaky-test | 1 |", markdown_section(markdown, "Counts"))
        self.assertIn("| review-quarantine | 1 |", markdown)
        self.assertIn("| review-rerun | 1 |", markdown)
        self.assertIn("| high | 1 |", markdown)
        self.assertIn("| medium | 1 |", markdown)

    def test_render_poc_cli_writes_owner_only_output(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX mode assertions are not portable to Windows")
        render_script = load_script("render")
        scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True, mode=0o755)
        prepared_path = scratch / "assessment-input.json"
        judgments_path = scratch / "judgments.json"
        output_path = scratch / "report.md"
        prepared = poc_prepared([(1, "First failure")])
        judgments = poc_judgments(
            prepared,
            [(1, "flaky-test", "review-quarantine", "issue", 1, "high", [], "After review.")],
        )
        prepared_path.write_text(json.dumps(prepared), encoding="utf-8")
        judgments_path.write_text(json.dumps(judgments), encoding="utf-8")
        try:
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "render.py",
                        "--prepared",
                        str(prepared_path),
                        "--judgments",
                        str(judgments_path),
                        "--output",
                        str(output_path),
                    ],
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(0, render_script.main())

            self.assertTrue(output_path.is_file())
            self.assertEqual(0o700, output_path.parent.stat().st_mode & 0o777)
            self.assertEqual(0o600, output_path.stat().st_mode & 0o777)
            self.assertIn("## Quarantine review", output_path.read_text(encoding="utf-8"))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_render_script_produces_deterministic_markdown_for_every_decision(self) -> None:
        render_script = load_script("render")
        snapshot = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "collectedAt": "2026-08-19T20:00:00Z",
            "openIssues": [1, 2],
            "issues": [
                {"number": 1, "title": "First | failure", "labels": ["ci-failure-cause"]},
                {"number": 2, "title": "Second failure", "labels": ["automation-broken"]},
            ],
            "evidence": {
                "issue:1": {
                    "kind": "issue-event",
                    "url": "https://github.com/owner/repo/issues/1",
                    "collectedAt": "2026-08-19T20:00:00Z",
                    "availability": "available",
                    "payload": {"number": 1, "state": "open"},
                },
                "issue:2": {
                    "kind": "issue-event",
                    "url": "https://github.com/owner/repo/issues/2",
                    "collectedAt": "2026-08-19T20:00:00Z",
                    "availability": "available",
                    "payload": {"number": 2, "state": "open"},
                },
            },
            "collectionErrors": [],
            "warnings": ["one warning"],
        }
        report = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "decisions": [
                {
                    "issueNumber": 1,
                    "issueUrl": "https://github.com/owner/repo/issues/1",
                    "issueKind": "incident",
                    "state": "actionable",
                    "proposedAction": "investigate",
                    "confidence": "medium",
                    "summary": "Inspect | current failure.",
                    "reasoning": "Current evidence is incomplete.",
                    "evidence": [{"id": "issue:1", "kind": "issue-event"}],
                    "contradictoryEvidence": [],
                    "missingEvidence": [],
                    "nextCondition": {"type": "evidence", "description": "Collect the failed log."},
                    "suggestedOwners": [],
                    "relatedIssues": [],
                    "changedSincePreviousRun": False,
                },
                {
                    "issueNumber": 2,
                    "issueUrl": "https://github.com/owner/repo/issues/2",
                    "issueKind": "tracker",
                    "state": "observing",
                    "proposedAction": "wait",
                    "confidence": "high",
                    "summary": "Watch the next run.",
                    "reasoning": "The tracker is current.",
                    "evidence": [{"id": "issue:2", "kind": "issue-event"}],
                    "contradictoryEvidence": [],
                    "missingEvidence": [],
                    "nextCondition": {"type": "event", "description": "Observe the next scheduled run."},
                    "suggestedOwners": [],
                    "relatedIssues": [],
                    "changedSincePreviousRun": False,
                },
            ],
        }

        markdown = render_script.render_markdown(
            snapshot,
            report,
            snapshot_path=Path("input.json"),
        )

        self.assertIn("# CI Shepherd Assessment", markdown)
        self.assertIn("| `investigate` | 1 |", markdown)
        self.assertIn("| `wait` | 1 |", markdown)
        self.assertIn("[#1](https://github.com/owner/repo/issues/1) First \\| failure", markdown)
        self.assertIn("Inspect \\| current failure.", markdown)
        self.assertIn("Collect the failed log.", markdown)
        self.assertIn("[#2](https://github.com/owner/repo/issues/2) Second failure", markdown)
        self.assertIn("**Collection warnings:** 1", markdown)

    def test_render_script_groups_operational_queues_by_validated_action(self) -> None:
        render_script = load_script("render")
        open_issues = [1, 2, 3, 4]
        snapshot = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "collectedAt": "2026-08-19T20:00:00Z",
            "openIssues": open_issues,
            "issues": [
                {
                    "number": number,
                    "title": f"Issue {number}",
                    "labels": ["ci-failure-cause"],
                }
                for number in open_issues
            ],
            "evidence": {
                f"issue:{number}": {
                    "kind": "issue-event",
                    "url": f"https://github.com/owner/repo/issues/{number}",
                    "collectedAt": "2026-08-19T20:00:00Z",
                    "availability": "available",
                    "payload": {"number": number, "state": "open"},
                }
                for number in open_issues
            },
            "collectionErrors": [],
            "warnings": [],
        }

        def decision(
            number: int,
            state: str,
            action: str,
        ) -> dict[str, object]:
            return {
                "issueNumber": number,
                "issueUrl": f"https://github.com/owner/repo/issues/{number}",
                "issueKind": "incident",
                "state": state,
                "proposedAction": action,
                "confidence": "medium",
                "summary": f"Summary {number}.",
                "reasoning": f"Reasoning {number}.",
                "evidence": [{"id": f"issue:{number}", "kind": "issue-event"}],
                "contradictoryEvidence": [],
                "missingEvidence": [],
                "nextCondition": {"type": "event", "description": "Observe a change."},
                "suggestedOwners": [],
                "relatedIssues": [],
                "changedSincePreviousRun": False,
            }

        report = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "decisions": [
                decision(1, "actionable", "investigate"),
                decision(2, "needs-human", "ping-human"),
                decision(3, "resolved", "recommend-close"),
                decision(4, "observing", "wait"),
            ],
        }

        markdown = render_script.render_markdown(
            snapshot,
            report,
            snapshot_path=Path("input.json"),
        )

        self.assertIn("## Investigate next", markdown)
        self.assertIn("## Needs human", markdown)
        self.assertIn("## Closure candidates", markdown)
        self.assertIn("## Waiting or owned by automation", markdown)
        self.assertIn("[#3](https://github.com/owner/repo/issues/3)", markdown)

    def test_progress_cli_appends_assessment_updates(self) -> None:
        progress_script = load_script("progress")
        output_dir = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(output_dir, ignore_errors=True)
        try:
            with patch.object(
                sys,
                "argv",
                [
                    "progress.py",
                    "--output-dir",
                    str(output_dir),
                    "--stage",
                    "assessment",
                    "--status",
                    "started",
                    "--message",
                    "Assessing issues 1-10.",
                ],
            ):
                self.assertEqual(0, progress_script.main())

            with patch.object(
                sys,
                "argv",
                [
                    "progress.py",
                    "--output-dir",
                    str(output_dir),
                    "--stage",
                    "assessment",
                    "--status",
                    "progress",
                    "--message",
                    "Assessed 10 of 60 issues.",
                    "--completed-items",
                    "10",
                    "--total-items",
                    "60",
                ],
            ):
                self.assertEqual(0, progress_script.main())

            with patch.object(
                sys,
                "argv",
                [
                    "progress.py",
                    "--output-dir",
                    str(output_dir),
                    "--stage",
                    "pipeline",
                    "--status",
                    "completed",
                    "--message",
                    "Validated and rendered the final report.",
                ],
            ):
                self.assertEqual(0, progress_script.main())

            progress = json.loads((output_dir / "progress.json").read_text())
            self.assertEqual("complete", progress["status"])
            self.assertEqual("complete", progress["currentStage"])
            self.assertEqual(10, progress["completedItems"])
            self.assertEqual(60, progress["totalItems"])
            self.assertEqual("Validated and rendered the final report.", progress["message"])
            self.assertEqual(
                [
                    ("assessment", "started"),
                    ("assessment", "progress"),
                    ("pipeline", "completed"),
                ],
                [(event["stage"], event["status"]) for event in progress["events"]],
            )
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)

    def test_validate_requests_script_rejects_invalid_handoff_without_expansion(self) -> None:
        validate_requests_script = load_script("validate_requests")
        scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True)
        input_path = scratch / "input.json"
        requests_path = scratch / "evidence-requests.round-1.json"
        snapshot = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "collectedAt": "2026-08-19T20:00:00Z",
            "openIssues": [1],
            "evidence": {
                "issue:1": {
                    "kind": "issue-event",
                    "url": "https://github.com/owner/repo/issues/1",
                    "collectedAt": "2026-08-19T20:00:00Z",
                    "availability": "available",
                    "payload": {"number": 1, "state": "open"},
                }
            },
            "collectionErrors": [],
        }
        requests = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "round": 1,
            "requests": [
                {
                    "type": "issue-reference",
                    "sourceIssueNumber": 1,
                    "evidenceId": "issue:999",
                    "decisionGate": "merged-fix",
                    "reason": "Verify a referenced fix.",
                }
            ],
        }
        input_path.write_text(json.dumps(snapshot), encoding="utf-8")
        requests_path.write_text(json.dumps(requests), encoding="utf-8")
        try:
            with patch.object(
                sys,
                "argv",
                [
                    "validate_requests.py",
                    "--input",
                    str(input_path),
                    "--requests",
                    str(requests_path),
                ],
            ):
                with self.assertRaisesRegex(
                    ValidationError,
                    "Evidence request cites unknown evidence ID: issue:999",
                ):
                    validate_requests_script.main()
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_prepare_script_writes_a_private_bounded_assessment_input(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX mode assertions are not portable to Windows")
        prepare_script = load_script("prepare")
        scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True)
        input_path = scratch / "input.json"
        output_path = scratch / "assessment-input.json"
        payload = {
            "number": 1,
            "state": "open",
            "title": "Failure",
            "url": "https://github.com/owner/repo/issues/1",
            "createdAt": "2026-08-01T00:00:00Z",
            "updatedAt": "2026-08-02T00:00:00Z",
            "labels": ["ci-failure-cause"],
            "producer": "ci-failure-cause",
            "autoclose": None,
            "ledger": {
                "source": "body-table",
                "schema": "occurrences-v1",
                "schemaRecognized": True,
                "sourceRecordCount": 1,
                "parsedRowCount": 1,
                "complete": True,
                "rows": [
                    {
                        "date": "2026-08-01",
                        "sourceRun": 10,
                        "runUrl": "https://github.com/owner/repo/actions/runs/10",
                        "job": "Tests",
                        "pullRequest": 2,
                    }
                ],
            },
            "episodes": [{"openedAt": "2026-08-01T00:00:00Z", "closedAt": None}],
            "episodesComplete": False,
            "facts": [],
        }
        snapshot = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "collectedAt": "2026-08-19T20:00:00Z",
            "openIssues": [1],
            "issues": [payload],
            "supportingIssues": [],
            "evidence": {
                "issue:1": {
                    "kind": "issue-event",
                    "url": payload["url"],
                    "collectedAt": "2026-08-19T20:00:00Z",
                    "availability": "available",
                    "payload": payload,
                }
            },
            "collectionErrors": [],
            "warnings": [],
            "references": {},
        }
        input_path.write_text(json.dumps(snapshot), encoding="utf-8")
        try:
            result = prepare_script.prepare(
                input_path=input_path,
                output_path=output_path,
                max_bundle_records=25,
            )

            self.assertEqual(output_path.resolve(), result)
            prepared = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(1, prepared["summary"]["issueCount"])
            self.assertEqual(0o600, output_path.stat().st_mode & 0o777)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_record_script_persists_only_a_validated_run(self) -> None:
        record_script = load_script("record")
        scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        state_dir = scratch / "state"
        run_dir = scratch / "run"
        shutil.rmtree(scratch, ignore_errors=True)
        run_dir.mkdir(parents=True)
        input_path = run_dir / "input.json"
        report_path = run_dir / "report.json"
        snapshot = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "collectedAt": "2026-08-19T20:00:00Z",
            "openIssues": [1],
            "evidence": {
                "issue:1": {
                    "kind": "issue-event",
                    "url": "https://github.com/owner/repo/issues/1",
                    "collectedAt": "2026-08-19T20:00:00Z",
                    "availability": "available",
                    "payload": {"number": 1, "state": "open"},
                }
            },
            "collectionErrors": [],
        }
        report = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "decisions": [
                {
                    "issueNumber": 1,
                    "issueUrl": "https://github.com/owner/repo/issues/1",
                    "issueKind": "incident",
                    "state": "observing",
                    "proposedAction": "wait",
                    "confidence": "medium",
                    "summary": "Observe the next run.",
                    "reasoning": "No current action is justified.",
                    "evidence": [{"id": "issue:1", "kind": "issue-event"}],
                    "contradictoryEvidence": [],
                    "missingEvidence": [],
                    "nextCondition": {"type": "event", "description": "Observe the next run."},
                    "suggestedOwners": [],
                    "relatedIssues": [],
                    "changedSincePreviousRun": False,
                }
            ],
        }
        input_path.write_text(json.dumps(snapshot), encoding="utf-8")
        report_path.write_text(json.dumps(report), encoding="utf-8")
        try:
            recorded = record_script.record(
                state_dir=state_dir,
                input_path=input_path,
                report_path=report_path,
                artifact_paths=[run_dir],
            )

            self.assertEqual("2026-08-19T20-00-00Z", recorded.name)
            self.assertTrue((recorded / "manifest.json").is_file())
            self.assertTrue((state_dir / "current.json").is_file())

            assessment_path = run_dir / "assessment-input.json"
            assessment_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "repository": "owner/repo",
                        "sourceCollectedAt": snapshot["collectedAt"],
                        "maxBundleRecords": 25,
                        "issues": [
                            {
                                "issueNumber": 1,
                                "candidateState": "actionable",
                                "candidateAction": "investigate",
                                "allowedActions": ["investigate"],
                                "allowedDecisions": [
                                    {"state": "actionable", "action": "investigate"}
                                ],
                                "automationEligible": False,
                                "approvalRequired": False,
                                "blockers": [],
                                "missingPrerequisites": [],
                                "evidenceBundle": [
                                    {
                                        "id": "issue:1",
                                        "kind": "issue-event",
                                        "availability": "available",
                                        "payload": {},
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            authority_state = scratch / "authority-state"
            with self.assertRaisesRegex(
                HistoryError,
                "not allowed by deterministic candidate",
            ):
                record_script.record(
                    state_dir=authority_state,
                    input_path=input_path,
                    report_path=report_path,
                    assessment_path=assessment_path,
                    artifact_paths=[run_dir],
                )
            self.assertFalse(authority_state.exists())

            report["decisions"] = []
            report_path.write_text(json.dumps(report), encoding="utf-8")
            second_state = scratch / "invalid-state"
            with self.assertRaises(HistoryError):
                record_script.record(
                    state_dir=second_state,
                    input_path=input_path,
                    report_path=report_path,
                    artifact_paths=[run_dir],
                )
            self.assertFalse(second_state.exists())
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_record_poc_script_persists_cycle_and_state_ledgers(self) -> None:
        record_poc_script = load_script("record_poc")
        scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        state_dir = scratch / "state"
        run_dir = scratch / "run"
        shutil.rmtree(scratch, ignore_errors=True)
        run_dir.mkdir(parents=True)

        snapshot = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "collectedAt": "2026-08-20T06:00:00Z",
            "openIssues": [1],
            "evidence": {
                "issue:1": {
                    "kind": "issue-event",
                    "url": "https://github.com/owner/repo/issues/1",
                    "collectedAt": "2026-08-20T06:00:00Z",
                    "availability": "available",
                    "payload": {"number": 1, "state": "open"},
                }
            },
            "collectionErrors": [],
        }
        prepared = poc_prepared([(1, "One failing test")])
        prepared_issue = prepared["issues"][0]
        prepared_issue["identity"] = {
            "tier1CauseId": None,
            "tier2TestName": "Namespace.Type.Test",
            "tier2ExceptionType": None,
            "tier3ErrorCode": None,
            "tier3Job": None,
        }
        prepared_issue["ledger"] = {
            "rows": [
                {
                    "date": "2026-08-20",
                    "sourceRun": 1001,
                    "job": "Tests / Linux",
                }
            ]
        }
        judgments = poc_judgments(
            prepared,
            [
                (
                    1,
                    "flaky-test",
                    "watch",
                    "test",
                    "Namespace.Type.Test",
                    "low",
                    [],
                    "The test fails in another independent run.",
                )
            ],
        )
        input_path = run_dir / "input.json"
        prepared_path = run_dir / "assessment-input.json"
        judgments_path = run_dir / "judgments.json"
        report_path = run_dir / "report.md"
        input_path.write_text(json.dumps(snapshot), encoding="utf-8")
        prepared_path.write_text(json.dumps(prepared), encoding="utf-8")
        judgments_path.write_text(json.dumps(judgments), encoding="utf-8")
        report_path.write_text("# CI Shepherd POC Assessment\n", encoding="utf-8")

        try:
            with (
                patch.object(
                    record_poc_script,
                    "record_poc_ledgers",
                    side_effect=OSError("injected ledger failure"),
                ),
                self.assertRaisesRegex(OSError, "injected ledger failure"),
            ):
                record_poc_script.record_poc_cycle(
                    state_dir=state_dir,
                    input_path=input_path,
                    prepared_path=prepared_path,
                    judgments_path=judgments_path,
                    report_path=report_path,
                    artifact_paths=[run_dir],
                )

            recorded = record_poc_script.record_poc_cycle(
                state_dir=state_dir,
                input_path=input_path,
                prepared_path=prepared_path,
                judgments_path=judgments_path,
                report_path=report_path,
                artifact_paths=[run_dir],
            )

            self.assertEqual(
                "2026-08-20T06-00-00Z-r0",
                recorded.name,
            )
            self.assertEqual(
                1,
                len(
                    (state_dir / "ledgers" / "fingerprints.jsonl")
                    .read_text(encoding="utf-8")
                    .strip()
                    .splitlines()
                ),
            )
            case_events = [
                json.loads(line)
                for line in (
                    state_dir / "ledgers" / "case-events.jsonl"
                )
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(1, len(case_events))
            self.assertEqual("bootstrap", case_events[0]["eventKind"])
            self.assertEqual("watch", case_events[0]["disposition"])

            replayed = record_poc_script.record_poc_cycle(
                state_dir=state_dir,
                input_path=input_path,
                prepared_path=prepared_path,
                judgments_path=judgments_path,
                report_path=report_path,
                artifact_paths=[run_dir],
            )
            self.assertEqual(recorded, replayed)
            self.assertEqual(
                case_events,
                [
                    json.loads(line)
                    for line in (
                        state_dir / "ledgers" / "case-events.jsonl"
                    )
                    .read_text(encoding="utf-8")
                    .splitlines()
                ],
            )

            judgments["issues"][0]["recommendations"][0]["summary"] = (
                "A different cycle reused the same collection identity."
            )
            judgments_path.write_text(json.dumps(judgments), encoding="utf-8")
            with self.assertRaisesRegex(HistoryError, "different POC cycle"):
                record_poc_script.record_poc_cycle(
                    state_dir=state_dir,
                    input_path=input_path,
                    prepared_path=prepared_path,
                    judgments_path=judgments_path,
                    report_path=report_path,
                    artifact_paths=[run_dir],
                )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_expand_script_writes_private_immutable_artifacts_and_get_only_audit(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX mode assertions are not portable to Windows")
        expand_script = load_script("expand")
        scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True, mode=0o755)
        input_path = scratch / "input.json"
        requests_path = scratch / "evidence-requests.round-1.json"
        output_path = scratch / "input.round-1.json"
        errors_path = scratch / "expansion-errors.round-1.json"
        audit_path = scratch / "api-calls.jsonl"
        snapshot = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "collectedAt": "2026-08-18T12:00:00Z",
            "openIssues": [1],
            "evidence": {
                "issue:1": {
                    "kind": "issue-event",
                    "url": "https://github.com/owner/repo/issues/1",
                    "collectedAt": "2026-08-18T12:00:00Z",
                    "availability": "available",
                    "payload": {
                        "number": 1,
                        "state": "open",
                        "facts": [
                            {
                                "field": "testName",
                                "raw": "Namespace.Tests.Fails",
                                "normalized": "namespace.tests.fails",
                            }
                        ],
                    },
                }
            },
            "collectionErrors": [],
        }
        request_document = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "round": 1,
            "requests": [
                {
                    "type": "canonical-search",
                    "sourceIssueNumber": 1,
                    "evidenceId": "issue:1",
                    "factField": "testName",
                    "decisionGate": "canonical-search-complete",
                    "reason": "Find an existing canonical issue.",
                }
            ],
        }
        input_bytes = (json.dumps(snapshot, indent=2) + "\n").encode()
        input_path.write_bytes(input_bytes)
        requests_path.write_text(json.dumps(request_document), encoding="utf-8")

        def fake_run(command, **kwargs):
            self.assertEqual("GET", command[command.index("--method") + 1])
            return subprocess.CompletedProcess(
                command,
                0,
                'HTTP/1.1 200 OK\nContent-Type: application/json\n\n'
                '{"total_count":0,"incomplete_results":false,"items":[]}',
                "",
            )

        try:
            with patch.object(expand_script.subprocess, "run", side_effect=fake_run):
                returned_path = expand_script.expand_files(
                    input_path,
                    requests_path,
                    output_path,
                    errors_path,
                    checkout=None,
                    audit_path=audit_path,
                )

            self.assertEqual(output_path.resolve(), returned_path)
            self.assertEqual(input_bytes, input_path.read_bytes())
            self.assertEqual(0o700, scratch.stat().st_mode & 0o777)
            for path in (output_path, errors_path, audit_path):
                with self.subTest(path=path):
                    self.assertEqual(0o600, path.stat().st_mode & 0o777)
            audit_records = [
                json.loads(line)
                for line in audit_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(audit_records)
            self.assertEqual({"GET"}, {record["method"] for record in audit_records})
            self.assertEqual([], json.loads(errors_path.read_text(encoding="utf-8")))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_expand_script_rejects_output_input_collision_before_api_access(self) -> None:
        expand_script = load_script("expand")
        scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True)
        input_path = scratch / "input.json"
        requests_path = scratch / "requests.json"
        errors_path = scratch / "errors.json"
        audit_path = scratch / "audit.jsonl"
        input_path.write_text("{}", encoding="utf-8")
        requests_path.write_text("{}", encoding="utf-8")
        try:
            with (
                patch.object(expand_script, "GitHubClient") as client_factory,
                self.assertRaisesRegex(ValidationError, "output.*input"),
            ):
                expand_script.expand_files(
                    input_path,
                    requests_path,
                    input_path,
                    errors_path,
                    checkout=None,
                    audit_path=audit_path,
                )
            client_factory.assert_not_called()

            hard_link_output = scratch / "hard-link-output.json"
            os.link(input_path, hard_link_output)
            with (
                patch.object(expand_script, "GitHubClient") as client_factory,
                self.assertRaisesRegex(ValidationError, "output.*input"),
            ):
                expand_script.expand_files(
                    input_path,
                    requests_path,
                    hard_link_output,
                    errors_path,
                    checkout=None,
                    audit_path=audit_path,
                )
            client_factory.assert_not_called()
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_expand_script_rejects_invalid_round_before_api_access(self) -> None:
        expand_script = load_script("expand")
        scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True)
        input_path = scratch / "input.json"
        requests_path = scratch / "requests.json"
        output_path = scratch / "output.json"
        errors_path = scratch / "errors.json"
        audit_path = scratch / "audit.jsonl"
        snapshot = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "collectedAt": "2026-08-18T12:00:00Z",
            "openIssues": [1],
            "evidence": {
                "issue:1": {
                    "kind": "issue-event",
                    "url": "https://github.com/owner/repo/issues/1",
                    "collectedAt": "2026-08-18T12:00:00Z",
                    "availability": "available",
                    "payload": {"number": 1, "state": "open", "facts": []},
                }
            },
            "collectionErrors": [],
            "expansions": [
                {"round": 1, "requests": [], "status": "complete", "errors": []},
                {"round": 2, "requests": [], "status": "complete", "errors": []},
            ],
        }
        request_document = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "round": 3,
            "requests": [],
        }
        input_path.write_text(json.dumps(snapshot), encoding="utf-8")
        requests_path.write_text(json.dumps(request_document), encoding="utf-8")
        try:
            with (
                patch.object(expand_script, "GitHubClient") as client_factory,
                self.assertRaisesRegex(ValidationError, "round"),
            ):
                expand_script.expand_files(
                    input_path,
                    requests_path,
                    output_path,
                    errors_path,
                    checkout=None,
                    audit_path=audit_path,
                )
            client_factory.assert_not_called()
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_build_snapshot_preserves_agent_inputs_and_validates(self) -> None:
        collect_script = load_script("collect")
        inventory = InventoryResult(
            open_issues=[{"number": 11, "title": "Failure"}],
            supporting_issues=[],
            evidence={
                "issue:11": {
                    "kind": "issue-event",
                    "url": "https://github.com/owner/repo/issues/11",
                    "collectedAt": "2026-08-17T22:00:00Z",
                    "availability": "available",
                    "payload": {"number": 11},
                }
            },
            collection_errors=[CollectionError("logs", "/logs", "expired")],
            warnings=["partial"],
            references={11: []},
        )

        snapshot = collect_script.build_snapshot(
            "owner/repo",
            collect_script.datetime(2026, 8, 17, 22, 0, tzinfo=collect_script.UTC),
            inventory,
        )

        self.assertEqual([11], snapshot["openIssues"])
        self.assertEqual("Failure", snapshot["issues"][0]["title"])
        self.assertEqual("logs", snapshot["collectionErrors"][0]["stage"])
        self.assertEqual({}, snapshot["references"])
        validate_snapshot(snapshot)

    def test_collector_shaped_snapshot_validates_multi_role_close_resolved_end_to_end(self) -> None:
        collect_script = load_script("collect")
        referenced_by = [
            {
                "sourceIssueNumber": 11,
                "sourceEvidenceId": "issue:11",
                "sourceUrl": "https://github.com/owner/repo/issues/11",
                "extractionMethod": "issue-body",
            }
        ]
        inventory = InventoryResult(
            open_issues=[{"number": 11, "title": "Failure"}],
            supporting_issues=[],
            evidence={
                "issue:11": {
                    "kind": "issue-event",
                    "url": "https://github.com/owner/repo/issues/11",
                    "collectedAt": "2026-08-17T22:00:00Z",
                    "availability": "available",
                    "payload": {"number": 11, "state": "open"},
                },
                "pr:77": {
                    "kind": "pull-request",
                    "url": "https://github.com/owner/repo/pull/77",
                    "collectedAt": "2026-08-17T22:00:00Z",
                    "availability": "available",
                    "payload": {
                        "number": 77,
                        "merged": True,
                        "referencedBy": referenced_by,
                    },
                },
                "run:43": {
                    "kind": "workflow-run",
                    "url": "https://github.com/owner/repo/actions/runs/43",
                    "collectedAt": "2026-08-17T22:00:00Z",
                    "availability": "available",
                    "payload": {
                        "runId": 43,
                        "conclusion": "success",
                        "recentHistoryCollected": True,
                        "recentHistoryTruncated": False,
                        "recentHistory": [
                            {
                                "runId": 43,
                                "attempt": 1,
                                "event": "push",
                                "branch": "main",
                                "headSha": "a" * 40,
                                "conclusion": "success",
                                "createdAt": "2026-08-17T21:00:00Z",
                                "url": "https://github.com/owner/repo/actions/runs/43",
                            }
                        ],
                        "historyCoversSourceRun": True,
                        "referencedBy": referenced_by,
                    },
                },
            },
            collection_errors=[],
            warnings=[],
            references={11: []},
        )
        snapshot = collect_script.build_snapshot(
            "owner/repo",
            collect_script.datetime(2026, 8, 17, 22, 0, tzinfo=collect_script.UTC),
            inventory,
        )
        report = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "decisions": [
                {
                    "issueNumber": 11,
                    "issueUrl": "https://github.com/owner/repo/issues/11",
                    "issueKind": "incident",
                    "state": "resolved",
                    "proposedAction": "close-resolved",
                    "confidence": "high",
                    "summary": "The incident is resolved.",
                    "reasoning": "A merged fix is followed by a green run and no newer match.",
                    "evidence": [
                        {"id": "issue:11", "kind": "issue-event"},
                        {"id": "pr:77", "kind": "pull-request", "role": "merged-fix"},
                        {
                            "id": "run:43",
                            "kind": "workflow-run",
                            "roles": [
                                "post-fix-green",
                                "no-newer-matching-failure",
                            ],
                        },
                    ],
                    "contradictoryEvidence": [],
                    "missingEvidence": [],
                    "nextCondition": {
                        "type": "none",
                        "description": "No further evidence is required.",
                    },
                    "suggestedOwners": [],
                    "relatedIssues": [],
                    "changedSincePreviousRun": False,
                }
            ],
        }

        validate_snapshot(snapshot)
        try:
            validate_report(snapshot, report)
        except ValidationError as exc:
            self.fail(str(exc))

    def test_collector_shaped_open_regression_uses_report_normalized_causes_end_to_end(self) -> None:
        collect_script = load_script("collect")
        referenced_by = [
            {
                "sourceIssueNumber": 11,
                "sourceEvidenceId": "issue:11",
                "sourceUrl": "https://github.com/owner/repo/issues/11",
                "extractionMethod": "fact-match",
            }
        ]
        inventory = InventoryResult(
            open_issues=[{"number": 11, "title": "Failure"}],
            supporting_issues=[{"number": 22, "title": "Prior resolved incident"}],
            evidence={
                "issue:11": {
                    "kind": "issue-event",
                    "url": "https://github.com/owner/repo/issues/11",
                    "collectedAt": "2026-08-17T22:00:00Z",
                    "availability": "available",
                    "payload": {"number": 11, "state": "open"},
                },
                "run:42": {
                    "kind": "workflow-run",
                    "url": "https://github.com/owner/repo/actions/runs/42",
                    "collectedAt": "2026-08-17T22:00:00Z",
                    "availability": "available",
                    "payload": {
                        "runId": 42,
                        "conclusion": "failure",
                        "facts": [{"field": "exceptionType", "normalized": "timeouterror"}],
                        "referencedBy": referenced_by,
                    },
                },
                "issue:22": {
                    "kind": "issue-event",
                    "url": "https://github.com/owner/repo/issues/22",
                    "collectedAt": "2026-08-17T22:00:00Z",
                    "availability": "available",
                    "payload": {
                        "number": 22,
                        "state": "closed",
                        "facts": [{"field": "exceptionType", "normalized": "timeouterror"}],
                        "referencedBy": referenced_by,
                    },
                },
                "issue:11:comment:17": {
                    "kind": "issue-comment",
                    "url": "https://github.com/owner/repo/issues/11#issuecomment-17",
                    "collectedAt": "2026-08-17T22:00:00Z",
                    "availability": "available",
                    "payload": {
                        "sourceIssueNumber": 11,
                        "facts": [{"field": "exceptionType", "normalized": "timeouterror"}],
                    },
                },
            },
            collection_errors=[],
            warnings=[],
            references={11: []},
        )
        snapshot = collect_script.build_snapshot(
            "owner/repo",
            collect_script.datetime(2026, 8, 17, 22, 0, tzinfo=collect_script.UTC),
            inventory,
        )
        report = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "decisions": [
                {
                    "issueNumber": 11,
                    "issueUrl": "https://github.com/owner/repo/issues/11",
                    "issueKind": "incident",
                    "state": "regression",
                    "proposedAction": "open-regression",
                    "confidence": "high",
                    "summary": "The resolved failure has recurred.",
                    "reasoning": "Current and prior evidence normalize to the same cause.",
                    "evidence": [
                        {"id": "issue:11", "kind": "issue-event"},
                        {
                            "id": "run:42",
                            "kind": "workflow-run",
                            "role": "current-failing-run",
                            "normalizedCause": "timeout-on-startup",
                        },
                        {
                            "id": "issue:22",
                            "kind": "issue-event",
                            "role": "prior-resolved-episode",
                            "normalizedCause": "timeout-on-startup",
                        },
                        {
                            "id": "issue:11:comment:17",
                            "kind": "issue-comment",
                            "role": "normalized-cause",
                            "normalizedCause": "timeout-on-startup",
                        },
                    ],
                    "contradictoryEvidence": [],
                    "missingEvidence": [],
                    "nextCondition": {
                        "type": "triage",
                        "description": "Review the recurring failure.",
                    },
                    "suggestedOwners": [],
                    "relatedIssues": [
                        {
                            "type": "regression-of",
                            "sourceIssueNumber": 11,
                            "targetIssueNumber": 22,
                        }
                    ],
                    "changedSincePreviousRun": False,
                }
            ],
        }

        validate_snapshot(snapshot)
        validate_report(snapshot, report)

    def test_write_private_uses_owner_only_permissions(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX mode assertions are not portable to Windows")
        collect_script = load_script("collect")
        scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True)
        try:
            target = scratch / "input.json"

            collect_script.write_private(target, json.dumps({"ok": True}))

            self.assertEqual(0o600, target.stat().st_mode & 0o777)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
