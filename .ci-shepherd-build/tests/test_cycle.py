from __future__ import annotations

import copy
import contextlib
import io
import json
from datetime import UTC, datetime
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cycle as cycle_script
from ci_shepherd.history import HistoryError
from ci_shepherd.investigations import (
    record_investigation_result,
    record_investigation_session_event,
)
from ci_shepherd.models import ValidationError
from ci_shepherd.poc_state import load_review_schedule, record_review_wakeup
from ci_shepherd.repository_policy import load_repository_policy
from tests.test_collector import ScriptedClient, make_issue
from tests.assessment_helpers import finish_reviewed_cycle, write_assessment_receipts

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_POLICY = load_repository_policy(
    Path(__file__).resolve().parent
    / "fixtures"
    / "repository-policy-widget-v1.json"
)


def snapshot(
    collected_at: str,
    *,
    title: str = "Unclassified CI failure",
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "repository": "owner/repo",
        "collectedAt": collected_at,
        "repositoryPolicy": {
            **REPOSITORY_POLICY.as_public_dict(),
            "digest": REPOSITORY_POLICY.digest,
        },
        "openIssues": [1],
        "openPullRequests": [],
        "pullRequests": [],
        "rejectedCandidates": [],
        "evidence": {
            "issue:1": {
                "kind": "issue-event",
                "url": "https://github.com/owner/repo/issues/1",
                "collectedAt": collected_at,
                "availability": "available",
                "payload": {
                    "number": 1,
                    "state": "open",
                    "title": title,
                    "url": "https://github.com/owner/repo/issues/1",
                    "author": "github-actions[bot]",
                },
            }
        },
        "collectionErrors": [],
        "warnings": [],
        "openBotScan": {
            "status": "complete",
            "complete": True,
            "scannedPages": 1,
            "pageBudget": 40,
            "itemBudget": 250,
            "botAuthoredFound": 1,
            "botAuthoredAdopted": 1,
            "detail": None,
        },
    }


def add_class_a_retry_evidence(
    value: dict[str, object],
    test_name: str,
) -> None:
    evidence = value["evidence"]
    assert isinstance(evidence, dict)
    issue_reference = [
        {"sourceIssueNumber": 1, "sourceEvidenceId": "issue:1"}
    ]
    job_name = "CI Tests / tests-linux (ubuntu-latest)"
    head_sha = "a" * 40
    evidence.update(
        {
            "run:200": {
                "kind": "workflow-run",
                "url": "https://github.com/owner/repo/actions/runs/200",
                "collectedAt": value["collectedAt"],
                "availability": "available",
                "payload": {
                    "runId": 200,
                    "targetRepository": "owner/repo",
                    "workflowId": 9,
                    "workflow": "CI Tests",
                    "event": "push",
                    "branch": "main",
                    "headSha": head_sha,
                    "status": "completed",
                    "conclusion": "success",
                    "attempt": 2,
                    "createdAt": "2026-08-28T19:00:00Z",
                    "updatedAt": "2026-08-28T20:00:00Z",
                    "runStartedAt": "2026-08-28T19:00:00Z",
                    "recentHistoryCollected": True,
                    "recentHistoryTotalCount": 1,
                    "recentHistory": [],
                },
            },
            "run:200:attempt:1:job:901": {
                "kind": "workflow-job",
                "url": "https://github.com/owner/repo/actions/runs/200/job/901",
                "collectedAt": value["collectedAt"],
                "availability": "available",
                "payload": {
                    "runId": 200,
                    "targetRepository": "owner/repo",
                    "attempt": 1,
                    "jobId": 901,
                    "checkRunId": 1901,
                    "name": job_name,
                    "status": "completed",
                    "conclusion": "failure",
                    "startedAt": "2026-08-28T19:01:00Z",
                    "completedAt": "2026-08-28T19:30:00Z",
                    "steps": [],
                    "annotationEvidenceIds": [],
                    "referencedBy": issue_reference,
                },
            },
            "run:200:attempt:1:job:901:test-results": {
                "kind": "workflow-test-results",
                "url": "https://github.com/owner/repo/actions/runs/200",
                "collectedAt": value["collectedAt"],
                "availability": "available",
                "payload": {
                    "runId": 200,
                    "attempt": 1,
                    "jobId": 901,
                    "targetRepository": "owner/repo",
                    "tests": [
                        {
                            "testName": test_name,
                            "outcome": "failed",
                        }
                    ],
                    "referencedBy": issue_reference,
                },
            },
            "run:200:attempt:2:job:902": {
                "kind": "workflow-job",
                "url": "https://github.com/owner/repo/actions/runs/200/job/902",
                "collectedAt": value["collectedAt"],
                "availability": "available",
                "payload": {
                    "runId": 200,
                    "targetRepository": "owner/repo",
                    "attempt": 2,
                    "jobId": 902,
                    "checkRunId": 1902,
                    "name": job_name,
                    "status": "completed",
                    "conclusion": "success",
                    "startedAt": "2026-08-28T19:31:00Z",
                    "completedAt": "2026-08-28T20:00:00Z",
                    "steps": [],
                    "annotationEvidenceIds": [],
                    "referencedBy": issue_reference,
                },
            },
            "run:200:attempt:2:job:902:test-results": {
                "kind": "workflow-test-results",
                "url": "https://github.com/owner/repo/actions/runs/200",
                "collectedAt": value["collectedAt"],
                "availability": "available",
                "payload": {
                    "runId": 200,
                    "attempt": 2,
                    "jobId": 902,
                    "targetRepository": "owner/repo",
                    "tests": [
                        {
                            "testName": test_name,
                            "outcome": "passed",
                        }
                    ],
                    "referencedBy": issue_reference,
                },
            },
        }
    )


def add_source_path_evidence(
    value: dict[str, object],
    revision: str,
) -> None:
    evidence = value["evidence"]
    assert isinstance(evidence, dict)
    path = "src/Product/Parser.cs"
    source_url = f"https://github.com/owner/repo/blob/{revision}/{path}"
    evidence[f"source:{path}"] = {
        "kind": "source-path",
        "url": source_url,
        "collectedAt": value["collectedAt"],
        "availability": "available",
        "payload": {
            "checkoutCommit": revision,
            "path": path,
            "recentCommits": [],
            "referencedBy": [
                {
                    "extractionMethod": "local-issue",
                    "sourceEvidenceId": "issue:1",
                    "sourceIssueNumber": 1,
                    "sourceUrl": "https://github.com/owner/repo/issues/1",
                }
            ],
            "sourceUrl": source_url,
            "targetRepository": "owner/repo",
        },
    }


def pull_request_snapshot(collected_at: str) -> dict[str, object]:
    current_state = {
        "headSha": "abc",
        "checks": {
            "source": "check-runs",
            "state": "green",
            "total": 1,
            "failing": [],
            "pending": [],
            "truncated": False,
            "complete": True,
        },
        "review": {
            "decision": "review-required",
            "reviewers": [],
            "complete": True,
        },
        "mergeable": True,
        "mergeableState": "clean",
        "draft": False,
        "complete": True,
        "incompleteReasons": [],
    }
    return {
        "schemaVersion": 1,
        "repository": "owner/repo",
        "collectedAt": collected_at,
        "openIssues": [],
        "openPullRequests": [23],
        "pullRequests": [
            {
                "number": 23,
                "state": "open",
                "title": "Repair CI automation",
                "url": "https://github.com/owner/repo/pull/23",
                "updatedAt": collected_at,
                "labels": ["automation-broken"],
                "author": "github-actions[bot]",
                "assignees": [],
                "selectionReasons": ["label:automation-broken"],
            }
        ],
        "rejectedCandidates": [],
        "evidence": {
            "pr:23": {
                "kind": "pull-request",
                "url": "https://github.com/owner/repo/pull/23",
                "collectedAt": collected_at,
                "availability": "available",
                "payload": {
                    "number": 23,
                    "state": "open",
                    "head": {"sha": "abc", "ref": "automation/fix"},
                    "base": {"sha": "def", "ref": "main"},
                    "files": [],
                    "currentState": current_state,
                },
            }
        },
        "collectionErrors": [],
    }


class CycleTests(unittest.TestCase):
    def test_advisory_triage_does_not_gate_direct_repair_in_real_cycle(self) -> None:
        from test_repair_routing import repair_snapshot

        for diagnostic in ("missing", "complete"):
            with self.subTest(diagnostic=diagnostic), TemporaryDirectory() as scratch:
                root = Path(scratch).resolve()
                value = repair_snapshot()
                if diagnostic == "complete":
                    for record in value["evidence"].values():
                        if record["kind"] == "workflow-log":
                            record["payload"]["excerpt"] += "\nSystem.TimeoutException: browser did not start"
                source = root / "input.json"
                source.write_text(json.dumps(value), encoding="utf-8")
                work = root / "work"
                cycle_script.start_cycle(
                    repository=value["repository"], state_dir=root / "state", work_dir=work,
                    checkout=None, shepherd_author="ankj", input_path=source,
                )
                prepared = json.loads((work / "assessment-input.json").read_text())
                issue, = prepared["issues"]
                self.assertEqual(2, len(issue["ciFailureTriage"]["cases"]))
                for case in issue["ciFailureTriage"]["cases"]:
                    self.assertEqual("unknown" if diagnostic == "missing" else "verified", case["family"]["status"])
                    self.assertEqual("watch", case["assessment"]["disposition"])
                    self.assertEqual("unknown", case["history"]["windows"]["7d"]["denominatorStatus"])
                completed = finish_reviewed_cycle(
                    work_dir=work, agent_judgments_path=work / "agent-judgments.json",
                )
                self.assertEqual("completed", completed["stage"])
                judgments = json.loads((work / "judgments.json").read_text())
                self.assertEqual("delegate-copilot", judgments["issues"][0]["recommendations"][0]["disposition"])
                proposals = json.loads((work / "action-proposals.json").read_text())
                assignment, = [p for p in proposals["proposals"] if p["operation"] == "assign-copilot"]
                self.assertTrue(assignment["executionEligibility"]["eligible"])
                plan = json.loads((work / "investigation-plan.json").read_text())
                self.assertEqual([], plan["requests"])

    def test_triage_rule_change_reselects_once_and_ledger_replay_converges(self) -> None:
        from ci_shepherd.ci_failure_triage import TRIAGE_RULE_VERSION
        from test_repair_routing import repair_snapshot

        with TemporaryDirectory() as scratch:
            root = Path(scratch).resolve()
            state = root / "state"
            original_ledger = None
            for index, version in enumerate((
                TRIAGE_RULE_VERSION, TRIAGE_RULE_VERSION,
                TRIAGE_RULE_VERSION + "-revised", TRIAGE_RULE_VERSION + "-revised",
            )):
                value = repair_snapshot()
                value["collectedAt"] = f"2026-08-19T16:{index:02}:00Z"
                source = root / f"input-{index}.json"
                source.write_text(json.dumps(value), encoding="utf-8")
                work = root / f"work-{index}"
                with patch("ci_shepherd.ci_failure_triage.TRIAGE_RULE_VERSION", version):
                    started = cycle_script.start_cycle(
                        repository=value["repository"], state_dir=state, work_dir=work,
                        checkout=None, shepherd_author="ankj", input_path=source,
                    )
                    self.assertEqual(1 if index in (0, 2) else 0, started["issueReviewCount"])
                    if started["stage"] == "awaiting-review":
                        finish_reviewed_cycle(work_dir=work, agent_judgments_path=work / "agent-judgments.json")
                if index == 2:
                    selection = json.loads((work / "review-selection.json").read_text())
                    self.assertIn("triage-rule-changed", selection["selected"][0]["changeReasons"])
                ledger = (state / "ledgers" / "fingerprints.jsonl").read_bytes()
                if index in (1, 3):
                    self.assertEqual(original_ledger, ledger)
                else:
                    rows = [json.loads(line) for line in ledger.splitlines()]
                    self.assertEqual(2 if index == 0 else 4, len(rows))
                    original_ledger = ledger

    def test_triage_expansion_preserves_control_normalized_recovery(self) -> None:
        from test_semantic_review_changes import resolved_snapshot

        with TemporaryDirectory() as scratch:
            root = Path(scratch).resolve()
            state = root / "state"
            source = root / "input.json"
            value = resolved_snapshot()
            for record in value["evidence"].values():
                record["collectedAt"] = value["collectedAt"]
            source.write_text(json.dumps(value), encoding="utf-8")
            work = root / "first"
            cycle_script.start_cycle(
                repository=value["repository"], state_dir=state, work_dir=work,
                checkout=None, shepherd_author="ankj", input_path=source,
            )
            finish_reviewed_cycle(work_dir=work, agent_judgments_path=work / "agent-judgments.json")
            value["collectedAt"] = "2026-08-19T16:01:00Z"
            value["evidence"]["issue:14"]["payload"]["updatedAt"] = value["collectedAt"]
            value["evidence"]["issue:14:comment:900"]["payload"].update(
                body="[automated] Updated recovery status.", updatedAt=value["collectedAt"],
            )
            value["refreshSummary"] = {"changedIssueNumbers": [14]}
            source.write_text(json.dumps(value), encoding="utf-8")
            record_review_wakeup(
                state, value["repository"], target_kind="issue", target_number=14,
                evaluate_at=value["collectedAt"], reason="positive-coverage-review",
            )
            work = root / "second"
            cycle_script.start_cycle(
                repository=value["repository"], state_dir=state, work_dir=work,
                checkout=None, shepherd_author="ankj", input_path=source,
            )
            request_document = {
                "schemaVersion": 1, "repository": value["repository"], "round": 1,
                "requests": [{
                    "type": "workflow-run", "sourceIssueNumber": 14, "evidenceId": "run:201",
                    "decisionGate": "current-failing-run", "reason": "Refresh matching execution evidence.",
                }],
            }

            def expand(source_path, requests_path, output_path, errors_path, **kwargs):
                expanded = json.loads(source_path.read_text())
                expanded["expansions"] = [{
                    "round": 1, "requests": request_document["requests"],
                    "status": "complete", "errors": [],
                }]
                output_path.write_text(json.dumps(expanded), encoding="utf-8")
                errors_path.write_text("[]\n", encoding="utf-8")
                return output_path

            with (
                patch.object(cycle_script, "build_proposal_evidence_requests", return_value=(request_document, [])),
                patch.object(cycle_script, "expand_files", side_effect=expand),
            ):
                restarted = finish_reviewed_cycle(work_dir=work, agent_judgments_path=work / "agent-judgments.json")
                self.assertEqual("awaiting-review", restarted["stage"])
                prepared = json.loads((work / "assessment-input.json").read_text())
                issue, = prepared["issues"]
                self.assertEqual("resolved", issue["candidateState"])
                self.assertEqual("verified", issue["recovery"]["status"])
                triage = json.loads((work / "ci-failure-triage.json").read_text())
                self.assertEqual(restarted["snapshotId"], triage["snapshotId"])
                self.assertEqual(issue["ciFailureTriage"]["cases"], triage["assessments"])
                completed = finish_reviewed_cycle(work_dir=work, agent_judgments_path=work / "agent-judgments.json")
            sealed = Path(completed["runDirectory"])
            self.assertEqual(triage, json.loads((sealed / "ci-failure-triage.json").read_text()))
            self.assertEqual(
                value["collectedAt"],
                json.loads((sealed / "snapshot.json").read_text())["evidence"]["issue:14"]["payload"]["updatedAt"],
            )

    def test_start_writes_advisory_triage_into_cycle_inputs(self) -> None:
        artifacts = Path(__file__).resolve().parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            input_path = root / "input.json"
            input_path.write_text(
                json.dumps(snapshot("2026-09-09T12:00:00Z")),
                encoding="utf-8",
            )
            work = root / "work"

            cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=root / "state",
                work_dir=work,
                checkout=None,
                shepherd_author="ankj",
                input_path=input_path,
            )

            triage = json.loads(
                (work / "ci-failure-triage.json").read_text(encoding="utf-8")
            )
            prepared = json.loads(
                (work / "assessment-input.json").read_text(encoding="utf-8")
            )
            compact = json.loads(
                (work / "agent-input.json").read_text(encoding="utf-8")
            )
            self.assertEqual(prepared["snapshotId"], triage["snapshotId"])
            self.assertEqual(
                prepared["issues"][0]["ciFailureTriage"],
                compact["issues"][0]["ciFailureTriage"],
            )
            packet = json.loads((work / "assessment-batch-0001.json").read_text())
            case, = packet["cases"]
            self.assertEqual(prepared["issues"][0]["ciFailureTriage"], case["input"]["ciFailureTriage"])
            self.assertEqual(
                {
                    key: value for key, value in compact["issues"][0].items()
                    if key not in {"allowedEvidence", "defaultJudgment"}
                    and (key not in prepared["issues"][0] or prepared["issues"][0][key] != value)
                },
                case["decisionContext"],
            )
            judgments_path = work / "agent-judgments.json"
            judgments_path.write_text(
                json.dumps({
                    "schemaVersion": 1,
                    "snapshotId": prepared["snapshotId"],
                    "issues": [compact["issues"][0]["defaultJudgment"]],
                }),
                encoding="utf-8",
            )
            completed = finish_reviewed_cycle(
                work_dir=work,
                agent_judgments_path=judgments_path,
            )
            recorded = Path(completed["runDirectory"])
            self.assertEqual(
                triage,
                json.loads(
                    (recorded / "ci-failure-triage.json").read_text(encoding="utf-8")
                ),
            )

    def test_quarantined_nomination_finishes_without_source_inspection(self) -> None:
        issue = make_issue(42, labels=["quarantined-test"])
        client = ScriptedClient(
            pages={
                "/repos/owner/repo/issues?state=open&labels=ci-failure-cause&per_page=100": [],
                "/repos/owner/repo/issues?state=open&labels=automation-broken&per_page=100": [],
                "/repos/owner/repo/issues?state=open&labels=quarantined-test&per_page=100": [issue],
                "/repos/owner/repo/issues?state=open&creator=github-actions%5Bbot%5D&per_page=100": [],
                "/repos/owner/repo/issues/42/comments": [],
            },
            singles={"/repos/owner/repo/issues/42": issue},
        )
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            work = root / "work"
            with (
                patch("collect.GitHubClient", return_value=client),
                patch(
                    "collect.collect_quarantine_source_state",
                    side_effect=AssertionError("Collection must not inspect a nomination"),
                ) as collect_source,
                patch.object(
                    cycle_script, "collect_quarantine_source_state",
                    side_effect=AssertionError("Finishing must not inspect a nomination"),
                ) as finish_source,
            ):
                cycle_script.start_cycle(
                    repository="owner/repo", state_dir=root / "state", work_dir=work,
                    checkout=None, shepherd_author="ankj", delegation_requests=[42],
                    repository_policy_path=(
                        Path(__file__).parent / "fixtures" / "repository-policy-widget-v1.json"
                    ),
                )
                finish_reviewed_cycle(
                    work_dir=work, agent_judgments_path=work / "agent-judgments.json",
                )
                collect_source.assert_not_called()
                finish_source.assert_not_called()
            proposals = json.loads((work / "action-proposals.json").read_text(encoding="utf-8"))
            assignment, = [item for item in proposals["proposals"] if item["operation"] == "assign-copilot"]
            self.assertEqual("operator-request", assignment["evidenceBasis"])

    def test_withdrawn_nomination_discards_previous_delegation(self) -> None:
        issue = make_issue(42, title="[main CI failure] Build is broken", labels=["ci-failure-cause"])
        client = ScriptedClient(
            pages={
                "/repos/owner/repo/issues?state=open&labels=ci-failure-cause&per_page=100": [issue],
                "/repos/owner/repo/issues?state=open&labels=automation-broken&per_page=100": [],
                "/repos/owner/repo/issues?state=open&creator=github-actions%5Bbot%5D&per_page=100": [],
                "/repos/owner/repo/issues/42/comments": [],
            },
            singles={"/repos/owner/repo/issues/42": issue},
        )
        with TemporaryDirectory() as scratch, patch("collect.GitHubClient", return_value=client):
            root = Path(scratch)
            first = root / "first"
            arguments = {
                "repository": "owner/repo", "state_dir": root / "state",
                "checkout": None, "shepherd_author": "ankj",
                "repository_policy_path": (
                    Path(__file__).parent / "fixtures" / "repository-policy-widget-v1.json"
                ),
            }
            cycle_script.start_cycle(
                **arguments, work_dir=first, delegation_requests=[42],
            )
            finish_reviewed_cycle(
                work_dir=first, agent_judgments_path=first / "agent-judgments.json",
            )
            proposals = json.loads((first / "action-proposals.json").read_text(encoding="utf-8"))
            assignment, = [item for item in proposals["proposals"] if item["operation"] == "assign-copilot"]
            self.assertEqual("operator-request", assignment["evidenceBasis"])
            second = root / "second"
            result = cycle_script.start_cycle(**arguments, work_dir=second)

            self.assertEqual("awaiting-review", result["stage"])
            collected = json.loads((second / "input.json").read_text(encoding="utf-8"))
            self.assertEqual([], collected.get("delegationRequests", []))
            self.assertEqual([], collected["refreshSummary"]["changedIssueNumbers"])
            selection = json.loads((second / "review-selection.json").read_text(encoding="utf-8"))
            selected, = selection["selected"]
            self.assertEqual(42, selected["issueNumber"])
            self.assertEqual("changed", selected["changeClass"])
            self.assertIn("operator-delegation-request-withdrawn", selected["changeReasons"])
            self.assertEqual([], selection["omitted"])
            finish_reviewed_cycle(
                work_dir=second, agent_judgments_path=second / "agent-judgments.json",
            )
            proposals = json.loads((second / "action-proposals.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [], [item for item in proposals["proposals"] if item["operation"] == "assign-copilot"],
            )

    def test_fresh_nomination_requires_live_collection_not_supplied_input(self) -> None:
        with TemporaryDirectory() as scratch, patch("collect.GitHubClient") as client:
            root = Path(scratch)
            work = root / "work"
            with self.assertRaisesRegex(ValueError, "--delegate-issue.*--input"):
                cycle_script.start_cycle(
                    repository="owner/repo", state_dir=root / "state", work_dir=work,
                    checkout=None, shepherd_author="ankj",
                    input_path=root / "old-input.json", delegation_requests=[42],
                )
            client.assert_not_called()
            self.assertFalse(work.exists())

    def test_evidence_expansion_restart_preserves_current_nomination(self) -> None:
        client = ScriptedClient(
            pages={
                "/repos/owner/repo/issues?state=open&labels=ci-failure-cause&per_page=100": [],
                "/repos/owner/repo/issues?state=open&labels=automation-broken&per_page=100": [],
                "/repos/owner/repo/issues?state=open&creator=github-actions%5Bbot%5D&per_page=100": [],
                "/repos/owner/repo/issues/42/comments": [],
            },
            singles={
                "/repos/owner/repo/issues/42": make_issue(
                    42, body="https://github.com/owner/repo/actions/runs/123",
                ),
            },
        )
        requests = {
            "schemaVersion": 1, "repository": "owner/repo", "round": 1,
            "requests": [{
                "type": "workflow-run", "sourceIssueNumber": 42, "evidenceId": "run:123",
                "decisionGate": "current-failing-run", "reason": "Refresh the cited run.",
            }],
        }
        with TemporaryDirectory() as scratch, patch("collect.GitHubClient", return_value=client):
            root = Path(scratch)
            work = root / "work"
            cycle_script.start_cycle(
                repository="owner/repo", state_dir=root / "state", work_dir=work,
                checkout=None, shepherd_author="ankj", delegation_requests=[42],
                repository_policy_path=(
                    Path(__file__).parent / "fixtures" / "repository-policy-widget-v1.json"
                ),
            )
            with (
                patch.object(cycle_script, "build_proposal_evidence_requests", return_value=(requests, [])),
                patch("expand.GitHubClient", return_value=client),
            ):
                result = finish_reviewed_cycle(
                    work_dir=work, agent_judgments_path=work / "agent-judgments.json",
                )

            self.assertEqual("awaiting-review", result["stage"])
            self.assertEqual(1, result["evidenceExpansionRound"])
            collected = json.loads((work / "input.json").read_text(encoding="utf-8"))
            self.assertEqual([42], collected["delegationRequests"])
            selected = json.loads((work / "agent-input.json").read_text(encoding="utf-8"))
            self.assertEqual([42], [issue["issueNumber"] for issue in selected["issues"]])
            self.assertEqual({"origin": "operator"}, selected["issues"][0]["delegationRequest"])

    def test_fresh_nomination_reassesses_unchanged_issue_without_renewing_later(self) -> None:
        client = ScriptedClient(
            pages={
                "/repos/owner/repo/issues?state=open&labels=ci-failure-cause&per_page=100": [],
                "/repos/owner/repo/issues?state=open&labels=automation-broken&per_page=100": [],
                "/repos/owner/repo/issues?state=open&creator=github-actions%5Bbot%5D&per_page=100": [],
                "/repos/owner/repo/issues/42/comments": [],
            },
            singles={"/repos/owner/repo/issues/42": make_issue(42)},
        )
        with TemporaryDirectory() as scratch, patch("collect.GitHubClient", return_value=client):
            root = Path(scratch)
            for index, requests in enumerate(([42], [42], [])):
                work = root / f"work-{index}"
                result = cycle_script.start_cycle(
                    repository="owner/repo", state_dir=root / "state", work_dir=work,
                    checkout=None, shepherd_author="ankj", delegation_requests=requests,
                    repository_policy_path=(
                        Path(__file__).parent / "fixtures" / "repository-policy-widget-v1.json"
                    ),
                )
                collected = json.loads((work / "input.json").read_text(encoding="utf-8"))
                self.assertEqual(requests, collected.get("delegationRequests", []))
                self.assertEqual(requests, collected["openIssues"])
                if index == 1:
                    self.assertEqual([], collected["refreshSummary"]["newIssueNumbers"])
                    self.assertEqual([], collected["refreshSummary"]["changedIssueNumbers"])
                selected = json.loads((work / "agent-input.json").read_text(encoding="utf-8"))
                self.assertEqual(requests, [issue["issueNumber"] for issue in selected["issues"]])
                if requests:
                    self.assertEqual({"origin": "operator"}, selected["issues"][0]["delegationRequest"])
                    selection = json.loads((work / "review-selection.json").read_text(encoding="utf-8"))
                    self.assertIn(
                        "operator-delegation-request", selection["selected"][0]["changeReasons"],
                    )
                    self.assertEqual("awaiting-review", result["stage"])
                    finish_reviewed_cycle(
                        work_dir=work, agent_judgments_path=work / "agent-judgments.json",
                    )
        self.assertEqual(2, client.calls.count(("get", "/repos/owner/repo/issues/42")))

    def test_replaying_input_does_not_renew_prior_delegation_request(self) -> None:
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            value = snapshot("2026-08-28T20:00:00Z")
            value["delegationRequests"] = [1]
            input_path = root / "input.json"
            input_path.write_text(json.dumps(value), encoding="utf-8")
            work = root / "work"

            cycle_script.start_cycle(
                repository="owner/repo", state_dir=root / "state", work_dir=work,
                checkout=None, shepherd_author="ankj", input_path=input_path,
            )

            collected = json.loads((work / "input.json").read_text(encoding="utf-8"))
            self.assertEqual([], collected.get("delegationRequests", []))
            prepared = json.loads((work / "assessment-input.json").read_text(encoding="utf-8"))
            self.assertIsNone(prepared["issues"][0].get("delegationRequest"))

    def test_start_cli_accepts_repeatable_arbitrary_issue_nominations(self) -> None:
        client = ScriptedClient(
            pages={
                "/repos/owner/repo/issues?state=open&labels=ci-failure-cause&per_page=100": [],
                "/repos/owner/repo/issues?state=open&labels=automation-broken&per_page=100": [],
                "/repos/owner/repo/issues?state=open&creator=github-actions%5Bbot%5D&per_page=100": [],
                "/repos/owner/repo/issues/42/comments": [],
                "/repos/owner/repo/issues/43/comments": [],
            },
            singles={
                "/repos/owner/repo/issues/42": make_issue(42),
                "/repos/owner/repo/issues/43": make_issue(43),
            },
        )
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            work = root / "work"
            output = io.StringIO()
            with (
                patch("collect.GitHubClient", return_value=client),
                patch.object(sys, "argv", [
                    "cycle.py", "start", "--repository", "owner/repo",
                    "--state-dir", str(root / "state"), "--work-dir", str(work),
                    "--shepherd-author", "ankj", "--repository-policy",
                    str(Path(__file__).parent / "fixtures" / "repository-policy-widget-v1.json"),
                    "--delegate-issue", "42", "--delegate-issue", "43",
                ]),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(0, cycle_script.main())

            result = json.loads(output.getvalue())
            self.assertEqual(5, result["maxDelegationRequests"])
            collected = json.loads((work / "input.json").read_text(encoding="utf-8"))
            self.assertEqual([42, 43], collected["delegationRequests"])
            self.assertEqual([42, 43], collected["openIssues"])
            prepared = json.loads((work / "agent-input.json").read_text(encoding="utf-8"))
            self.assertEqual([42, 43], [issue["issueNumber"] for issue in prepared["issues"]])

    def test_no_action_case_needing_positive_coverage_gets_one_durable_wakeup(
        self,
    ) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            state = Path(scratch) / "state"
            arguments = {
                "state_dir": state,
                "repository": "owner/repo",
                "observed_at": "2026-08-28T20:00:00Z",
                "selected_issue_numbers": {1},
                "judgments": {
                    "issues": [
                        {
                            "issueNumber": 1,
                            "recommendations": [{"disposition": "no-action"}],
                        }
                    ]
                },
                "observations": {
                    "occurrences": [
                        {
                            "issueNumber": 1,
                            "coverageState": "needs-positive-coverage",
                        }
                    ]
                },
                "interval_days": 14,
            }

            cycle_script._schedule_positive_coverage_reviews(**arguments)
            cycle_script._schedule_positive_coverage_reviews(**arguments)

            schedule = load_review_schedule(
                state,
                "owner/repo",
                "2026-08-28T20:00:00Z",
                issue_numbers=[1],
                pull_request_numbers=[],
            )
            self.assertEqual(
                {
                    "reassessAt": "2026-09-11T20:00:00Z",
                    "wakeReason": "positive-coverage-review",
                },
                schedule["issues"]["1"],
            )
            rows = (
                state / "ledgers" / "review-wakeups.jsonl"
            ).read_text(encoding="utf-8").splitlines()
            self.assertEqual(1, len(rows))

    def test_unknown_verified_run_scope_blocks_mutation_and_still_reports(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            state = root / "state"
            input_path = root / "input.json"
            input_snapshot = snapshot("2026-08-28T20:00:00Z")
            issue_payload = input_snapshot["evidence"]["issue:1"]["payload"]
            issue_payload["producer"] = "ci-failure-cause"
            add_class_a_retry_evidence(
                input_snapshot,
                "Namespace.Type.FlakyTest",
            )
            input_snapshot["evidence"]["run:200"]["payload"]["event"] = "pull_request"
            managed_policy = replace(
                REPOSITORY_POLICY,
                managed_issue_producers=frozenset({"ci-failure-cause"}),
                managed_automation_explicit=True,
            )
            input_snapshot["repositoryPolicy"] = {
                **managed_policy.as_public_dict(),
                "digest": managed_policy.digest,
            }
            input_path.write_text(json.dumps(input_snapshot), encoding="utf-8")
            work = root / "work"
            cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=work,
                checkout=None,
                shepherd_author="ankj",
                input_path=input_path,
            )

            completed = finish_reviewed_cycle(
                work_dir=work,
                agent_judgments_path=work / "agent-judgments.json",
            )

            self.assertFalse(completed["managedItemCoverageValid"])
            policy_selection = json.loads(
                (work / "policy-selection.json").read_text(encoding="utf-8")
            )
            self.assertEqual([], policy_selection["selectedActionIds"])
            self.assertFalse(policy_selection.get("mutationBlocked", False))
            report = (work / "report-details.md").read_text(encoding="utf-8")
            self.assertIn("## Managed active-item coverage", report)
            self.assertIn("verified workflow-run scope is unknown", report)
            first_coverage = (work / "managed-item-coverage.json").read_bytes()
            ledgers = state / "ledgers"
            first_ledger_bytes = {
                path.name: path.read_bytes()
                for path in ledgers.glob("*.jsonl")
            }

            second_input = root / "input-2.json"
            second_snapshot = copy.deepcopy(input_snapshot)
            second_snapshot["collectedAt"] = "2026-08-28T20:00:01Z"
            second_input.write_text(json.dumps(second_snapshot), encoding="utf-8")
            second_work = root / "work-2"
            repeated = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=second_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=second_input,
            )

            self.assertEqual("completed", repeated["stage"])
            self.assertEqual(
                first_coverage,
                (second_work / "managed-item-coverage.json").read_bytes(),
            )
            self.assertEqual(
                first_ledger_bytes,
                {
                    path.name: path.read_bytes()
                    for path in ledgers.glob("*.jsonl")
                },
            )

    def test_coordinator_stage_requires_active_policy_or_selected_action(
        self,
    ) -> None:
        now = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
        operation_classes = {
            operation_class: {
                "enabled": operation_class == "create-comment",
                "maxPerRun": 1 if operation_class == "create-comment" else 0,
                "maxRolling24h": (
                    2 if operation_class == "create-comment" else 0
                ),
            }
            for operation_class in (
                "create-comment",
                "edit-comment",
                "close-issue",
                "delegate-copilot",
                "rerun-or-retry",
            )
        }
        policy = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "revisionId": "policy:1",
            "revision": 1,
            "status": "active",
            "createdAtUtc": "2026-09-04T11:00:00Z",
            "expiresAtUtc": "2026-09-05T11:00:00Z",
            "actor": "github:radical",
            "replacesRevisionId": None,
            "operationClasses": operation_classes,
            "deniedActionIds": [],
            "deniedTargets": [],
            "policyDigest": "sha256:" + ("0" * 64),
        }
        selection = {"selectedActionIds": []}

        self.assertEqual(
            "policy-active",
            cycle_script._coordinator_stage(
                {"effectivePolicy": policy},
                selection,
                now=now,
            ),
        )
        self.assertEqual(
            "awaiting-policy",
            cycle_script._coordinator_stage(
                {"effectivePolicy": {**policy, "status": "paused"}},
                selection,
                now=now,
            ),
        )
        self.assertEqual(
            "awaiting-policy",
            cycle_script._coordinator_stage(
                {
                    "effectivePolicy": {
                        **policy,
                        "expiresAtUtc": "2026-09-04T11:30:00Z",
                    }
                },
                selection,
                now=now,
            ),
        )
        self.assertEqual(
            "ready",
            cycle_script._coordinator_stage(
                {"effectivePolicy": {**policy, "status": "revoked"}},
                {"selectedActionIds": ["action:1"]},
                now=now,
            ),
        )

    def test_blocking_evidence_expands_once_and_requires_fresh_review(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            state = root / "state"
            input_path = root / "source.json"
            source_snapshot = snapshot("2026-08-31T12:00:00Z")
            pull_requests = pull_request_snapshot("2026-08-31T12:00:00Z")
            source_snapshot["openPullRequests"] = pull_requests["openPullRequests"]
            source_snapshot["pullRequests"] = pull_requests["pullRequests"]
            source_evidence = source_snapshot["evidence"]
            pull_request_evidence = pull_requests["evidence"]
            assert isinstance(source_evidence, dict)
            assert isinstance(pull_request_evidence, dict)
            source_evidence.update(pull_request_evidence)
            from tests.test_production_decisions import handoff_snapshot
            handoff = json.loads(json.dumps(handoff_snapshot()).replace("microsoft/aspire", "owner/repo"))
            source_snapshot["delegatedIssues"] = [21]
            source_snapshot["delegatedIssueDetails"] = handoff["issues"]
            source_snapshot["delegationStatus"] = handoff["delegationStatus"]
            source_evidence.update(handoff["evidence"])
            state.mkdir(mode=0o700)
            record_review_wakeup(state, "owner/repo", target_kind="issue", target_number=21,
                                 evaluate_at=source_snapshot["collectedAt"], reason="escalation-reminder")
            input_path.write_text(json.dumps(source_snapshot), encoding="utf-8")
            work = root / "work"
            started = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=work,
                checkout=None,
                shepherd_author="ankj",
                input_path=input_path,
            )
            self.assertEqual(
                {
                    "schemaVersion": 1,
                    "snapshotId": started["snapshotId"],
                    "issues": [],
                    "pullRequests": [],
                },
                json.loads(
                    (work / "agent-assessment.json").read_text(encoding="utf-8")
                ),
            )
            request_document = {
                "schemaVersion": 1,
                "repository": "owner/repo",
                "round": 1,
                "requests": [
                    {
                        "type": "workflow-run",
                        "sourceIssueNumber": 1,
                        "evidenceId": "run:123",
                        "decisionGate": "current-failing-run",
                        "reason": "Refresh the exact run.",
                    }
                ],
            }
            expansion_calls = 0

            def fake_expand(
                source_path: Path,
                requests_path: Path,
                output_path: Path,
                errors_path: Path,
                *,
                checkout: Path | None,
                audit_path: Path,
            ) -> Path:
                nonlocal expansion_calls
                expansion_calls += 1
                self.assertIsNone(checkout)
                self.assertEqual(
                    request_document,
                    json.loads(requests_path.read_text(encoding="utf-8")),
                )
                expanded = json.loads(source_path.read_text(encoding="utf-8"))
                expanded["expansions"] = [
                    {
                        "round": 1,
                        "requests": request_document["requests"],
                        "status": "complete",
                        "errors": [],
                    }
                ]
                output_path.write_text(json.dumps(expanded), encoding="utf-8")
                errors_path.write_text("[]\n", encoding="utf-8")
                audit_path.touch()
                return output_path

            with (
                patch.object(
                    cycle_script,
                    "build_proposal_evidence_requests",
                    return_value=(request_document, []),
                ),
                patch.object(cycle_script, "expand_files", side_effect=fake_expand),
            ):
                restarted = finish_reviewed_cycle(
                    work_dir=work,
                    agent_assessment_path=work / "agent-assessment.json",
                )
                self.assertEqual("awaiting-review", restarted["stage"])
                self.assertEqual(1, restarted["evidenceExpansionRound"])
                old_receipts = work / "assessment-receipts.pre-expansion.json"
                self.assertTrue(old_receipts.is_file())
                self.assertTrue((work / "assessment-completion.pre-expansion.json").is_file())
                self.assertFalse((work / "assessment-completion.json").exists())
                self.assertNotEqual(started["assessment"]["assessmentId"], restarted["assessment"]["assessmentId"])
                with self.assertRaisesRegex(ValueError, "stale"):
                    cycle_script.finish_cycle(
                        work_dir=work,
                        agent_assessment_path=work / "agent-assessment.json",
                        assessment_receipts_path=old_receipts,
                    )
                preserved_receipts = old_receipts.read_bytes()
                old_receipts.unlink()
                write_assessment_receipts(work)
                with self.assertRaisesRegex(ValueError, "assessment-receipts.pre-expansion"):
                    cycle_script.finish_cycle(
                        work_dir=work, agent_assessment_path=work / "agent-assessment.json",
                    )
                old_receipts.write_bytes(preserved_receipts)
                old_receipts.chmod(0o600)
                self.assertEqual(
                    "snapshot:owner/repo:2026-08-31T12:00:00Z:r1",
                    restarted["snapshotId"],
                )
                self.assertTrue((work / "input.pre-expansion.json").is_file())
                self.assertTrue(
                    (work / "action-proposals.pre-expansion.json").is_file()
                )
                provisional_proposals = json.loads(
                    (work / "action-proposals.json").read_text(encoding="utf-8")
                )
                self.assertNotIn(
                    "productionPilotCapability",
                    provisional_proposals,
                )
                self.assertEqual(0, restarted["pullRequestReviewCount"])
                restarted_pull_requests = json.loads(
                    (work / "pull-request-review.json").read_text(encoding="utf-8")
                )
                self.assertEqual([], restarted_pull_requests["tasks"])
                self.assertEqual(
                    [23],
                    [
                        entry["number"]
                        for entry in restarted_pull_requests["excluded"]
                        if "retainedJudgment" in entry
                    ],
                )
                reset_judgments = json.loads(
                    (work / "agent-judgments.json").read_text(encoding="utf-8")
                )
                self.assertEqual(restarted["snapshotId"], reset_judgments["snapshotId"])
                reset_assessment = json.loads(
                    (work / "agent-assessment.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    {
                        "schemaVersion": 1,
                        "snapshotId": restarted["snapshotId"],
                        "issues": [],
                        "pullRequests": [],
                    },
                    reset_assessment,
                )
                restarted_prepared = json.loads((work / "assessment-input.json").read_text())
                handoff_issue = next((issue for issue in restarted_prepared["issues"] if issue["issueNumber"] == 21), None)
                self.assertIsNotNone(handoff_issue, "Expansion must retain the already-due delegated issue.")
                self.assertTrue(handoff_issue["delegationContext"]["decisionRequired"])

                # The domain-specific files are derived audit artifacts. Stale or
                # cross-routed copies must not override the combined response.
                (work / "agent-judgments.json").write_text(
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "snapshotId": restarted["snapshotId"],
                            "pullRequests": [],
                        }
                    ),
                    encoding="utf-8",
                )
                (work / "agent-pull-request-judgments.json").write_text(
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "snapshotId": restarted["snapshotId"],
                            "issues": [],
                        }
                    ),
                    encoding="utf-8",
                )
                with patch.object(
                    cycle_script, "render_run_markdown", wraps=cycle_script.render_run_markdown,
                ) as render_report:
                    completed = finish_reviewed_cycle(
                        work_dir=work,
                        agent_assessment_path=work / "agent-assessment.json",
                    )
                render_report.assert_called_once()
                self.assertEqual(
                    json.loads((work / "assessment-batches.json").read_text(encoding="utf-8")),
                    render_report.call_args.kwargs["assessment_manifest"],
                )
                self.assertEqual(
                    json.loads((work / "assessment-batches.pre-expansion.json").read_text(encoding="utf-8")),
                    render_report.call_args.kwargs["pre_expansion_assessment_manifest"],
                )

            self.assertEqual("completed", completed["stage"])
            self.assertEqual(1, expansion_calls)
            self.assertIn(
                "issues",
                json.loads(
                    (work / "agent-judgments.json").read_text(encoding="utf-8")
                ),
            )
            self.assertIn(
                "pullRequests",
                json.loads(
                    (work / "agent-pull-request-judgments.json").read_text(
                        encoding="utf-8"
                    )
                ),
            )
            completed_proposals = json.loads(
                (work / "action-proposals.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                {
                    "schemaVersion": 1,
                    "evidenceRound": 1,
                    "managedItemCoverage": {
                        "schemaVersion": 2,
                        "valid": True,
                        "blockers": [],
                        "globalBlockers": [],
                        "blockedScopes": [],
                    },
                },
                completed_proposals["productionPilotCapability"],
            )
            self.assertTrue(
                (Path(completed["runDirectory"]) / "evidence-requests.json").is_file()
            )
            self.assertTrue(
                (Path(completed["runDirectory"]) / "agent-assessment.json").is_file()
            )
            self.assertTrue(
                (
                    Path(completed["runDirectory"])
                    / "action-proposals.pre-expansion.json"
                ).is_file()
            )
            self.assertTrue(
                (
                    Path(completed["runDirectory"])
                    / "review-selection.pre-expansion.json"
                ).is_file()
            )

            self.assertTrue(
                (
                    Path(completed["runDirectory"])
                    / "pull-request-review.pre-expansion.json"
                ).is_file()
            )
            pull_request_judgments = json.loads(
                (work / "pull-request-judgments.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [23],
                [
                    judgment["pullRequestNumber"]
                    for judgment in pull_request_judgments["pullRequests"]
                ],
            )
            review_events = [
                json.loads(line)
                for line in (
                    state / "ledgers" / "review-events.jsonl"
                ).read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [("issue", 1), ("issue", 21), ("pull-request", 23)],
                [
                    (event["targetKind"], event["targetNumber"])
                    for event in review_events
                ],
            )
            successor_input = root / "successor-input.json"
            successor_snapshot = json.loads(
                (work / "input.json").read_text(encoding="utf-8")
            )
            successor_snapshot["collectedAt"] = "2026-08-31T13:00:00Z"
            for evidence_record in successor_snapshot["evidence"].values():
                evidence_record["collectedAt"] = "2026-08-31T13:00:00Z"
            successor_input.write_text(
                json.dumps(successor_snapshot),
                encoding="utf-8",
            )
            successor = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=root / "successor-work",
                checkout=None,
                shepherd_author="ankj",
                input_path=successor_input,
            )
            self.assertEqual("awaiting-review", successor["stage"])
            self.assertEqual(1, successor["issueReviewCount"])
            self.assertEqual(0, successor["pullRequestReviewCount"])

    def test_combined_agent_assessment_fails_before_splitting_invalid_output(
        self,
    ) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            input_path = root / "input.json"
            input_path.write_text(
                json.dumps(snapshot("2026-08-31T12:00:00Z")),
                encoding="utf-8",
            )
            state = root / "state"
            work = root / "work"
            started = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=work,
                checkout=None,
                shepherd_author="ankj",
                input_path=input_path,
            )
            issue_artifact = work / "agent-judgments.json"
            pull_request_artifact = work / "agent-pull-request-judgments.json"
            issue_before = issue_artifact.read_bytes()
            pull_request_before = pull_request_artifact.read_bytes()
            (work / "agent-assessment.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "snapshotId": started["snapshotId"],
                        "issues": [],
                        "pullRequests": {},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValidationError,
                "Agent assessment pullRequests must be an array",
            ):
                finish_reviewed_cycle(
                    work_dir=work,
                    agent_assessment_path=work / "agent-assessment.json",
                )

            self.assertEqual(issue_before, issue_artifact.read_bytes())
            self.assertEqual(pull_request_before, pull_request_artifact.read_bytes())
            self.assertFalse((state / "current.json").exists())

    def test_finish_cli_accepts_combined_agent_assessment(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            input_path = root / "input.json"
            input_path.write_text(
                json.dumps(snapshot("2026-08-31T12:00:00Z")),
                encoding="utf-8",
            )
            work = root / "work"
            cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=root / "state",
                work_dir=work,
                checkout=None,
                shepherd_author="ankj",
                input_path=input_path,
            )

            write_assessment_receipts(work)
            supplied_receipts = work / "submitted-receipts.json"
            (work / "assessment-receipts.json").rename(supplied_receipts)
            result = subprocess.run(
                [
                    sys.executable,
                    str(REPOSITORY_ROOT / ".ci-shepherd-build" / "scripts" / "cycle.py"),
                    "finish",
                    "--work-dir",
                    str(work),
                    "--agent-assessment",
                    str(work / "agent-assessment.json"),
                    "--assessment-receipts",
                    str(supplied_receipts),
                ],
                capture_output=True,
                check=False,
                text=True,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("completed", json.loads(result.stdout)["stage"])

    def test_cycle_refuses_to_publish_when_history_advanced_after_start(self) -> None:
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            state = root / "state"
            state.mkdir()
            state = state.resolve()
            state.chmod(0o700)
            first_input = root / "input-1.json"
            second_input = root / "input-2.json"
            first_input.write_text(
                json.dumps(snapshot("2026-08-28T10:00:00Z")),
                encoding="utf-8",
            )
            second_input.write_text(
                json.dumps(snapshot("2026-08-28T11:00:00Z")),
                encoding="utf-8",
            )
            first_work = root / "work-1"
            second_work = root / "work-2"

            cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=first_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=first_input,
            )
            cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=second_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=second_input,
            )
            finish_reviewed_cycle(
                work_dir=first_work,
                agent_judgments_path=first_work / "agent-judgments.json",
            )

            with self.assertRaisesRegex(
                HistoryError,
                "History advanced after this cycle started",
            ):
                finish_reviewed_cycle(
                    work_dir=second_work,
                    agent_judgments_path=second_work / "agent-judgments.json",
                )

    def test_next_cycle_receives_completed_investigation_result(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            state = root / "state"
            first_input = root / "input-1.json"
            first_input.write_text(
                json.dumps(snapshot("2026-08-28T20:00:00Z")),
                encoding="utf-8",
            )
            first_work = root / "work-1"
            cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=first_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=first_input,
            )
            finish_reviewed_cycle(
                work_dir=first_work,
                agent_judgments_path=first_work / "agent-judgments.json",
            )
            investigation_plan = json.loads(
                (first_work / "investigation-plan.json").read_text(encoding="utf-8")
            )
            request = investigation_plan["requests"][0]
            checkout = root / "investigation-checkout"
            checkout.mkdir()
            subprocess.run(
                ["git", "init", "--quiet", str(checkout)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "-c",
                    "user.name=CI Shepherd",
                    "-c",
                    "user.email=ci-shepherd@example.invalid",
                    "commit",
                    "--quiet",
                    "--allow-empty",
                    "-m",
                    "initial",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            record_investigation_session_event(
                state,
                request,
                status="started",
                recorded_at="2026-08-28T20:20:00Z",
                session_id="investigation-session-1",
                checkout=checkout,
            )
            record_investigation_result(
                state,
                request,
                {
                    "outcome": "fixable",
                    "summary": "The failure is actionable.",
                    "evidenceIds": ["issue:1"],
                    "reassessWhen": "When a fix pull request changes state.",
                    "fixHandoff": {
                        "problem": "A deterministic parser failure.",
                        "likelyPaths": ["src/Product/Parser.cs"],
                        "validation": ["Run the parser regression test."],
                    },
                },
                recorded_at="2026-08-28T20:30:00Z",
                session_id="investigation-session-1",
                checkout=checkout,
            )

            second_input = root / "input-2.json"
            second_input.write_text(
                json.dumps(snapshot("2026-08-29T20:00:00Z")),
                encoding="utf-8",
            )
            second_work = root / "work-2"
            cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=second_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=second_input,
            )

            prepared = json.loads(
                (second_work / "assessment-input.json").read_text(encoding="utf-8")
            )
            compact = json.loads(
                (second_work / "agent-input.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                "fixable",
                prepared["issues"][0]["investigationResults"][0]["outcome"],
            )
            self.assertEqual(
                "fixable",
                compact["issues"][0]["investigationResults"][0]["outcome"],
            )
            finish_reviewed_cycle(
                work_dir=second_work,
                agent_judgments_path=second_work / "agent-judgments.json",
            )
            second_plan = json.loads(
                (second_work / "investigation-plan.json").read_text(encoding="utf-8")
            )
            self.assertEqual([], second_plan["requests"])
            self.assertEqual(
                [request["investigationId"]],
                second_plan["reusedInvestigationIds"],
            )

            third_input = root / "input-3.json"
            third_input.write_text(
                json.dumps(
                    snapshot(
                        "2026-08-30T20:00:00Z",
                        title="Changed unknown CI failure",
                    )
                ),
                encoding="utf-8",
            )
            third_work = root / "work-3"
            cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=third_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=third_input,
            )
            changed_prepared = json.loads(
                (third_work / "assessment-input.json").read_text(encoding="utf-8")
            )
            changed_compact = json.loads(
                (third_work / "agent-input.json").read_text(encoding="utf-8")
            )
            self.assertNotIn(
                "investigationResults",
                changed_prepared["issues"][0],
            )
            self.assertNotIn(
                "investigationResults",
                changed_compact["issues"][0],
            )
            finish_reviewed_cycle(
                work_dir=third_work,
                agent_judgments_path=third_work / "agent-judgments.json",
            )
            third_plan = json.loads(
                (third_work / "investigation-plan.json").read_text(encoding="utf-8")
            )
            self.assertEqual(1, len(third_plan["requests"]))
            self.assertNotEqual(
                request["investigationId"],
                third_plan["requests"][0]["investigationId"],
            )

    def test_excludes_quarantine_candidate_for_already_quarantined_issue(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            state = root / "state"
            input_path = root / "input.json"
            input_snapshot = snapshot("2026-08-28T20:00:00Z")
            input_snapshot["evidence"]["issue:1"]["payload"]["labels"] = [
                "quarantined-test"
            ]
            input_path.write_text(
                json.dumps(input_snapshot),
                encoding="utf-8",
            )
            work = root / "work"
            started = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=work,
                checkout=None,
                shepherd_author="ankj",
                input_path=input_path,
            )
            agent_input = json.loads(
                (work / "agent-input.json").read_text(encoding="utf-8")
            )
            selection = json.loads(
                (work / "review-selection.json").read_text(encoding="utf-8")
            )

            self.assertEqual("completed", started["stage"])
            self.assertEqual([], agent_input["issues"])
            self.assertEqual([], selection["selected"])
            self.assertEqual(
                [{"issueNumber": 1, "reason": "not-review-required"}],
                selection["omitted"],
            )

    def test_unverifiable_source_makes_no_quarantine_reconciliation_claim(
        self,
    ) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            input_path = root / "input.json"
            input_snapshot = snapshot(
                "2026-08-28T20:00:00Z",
                title="Flaky test Demo.Tests.Flaky",
            )
            input_snapshot["evidence"]["issue:1"]["payload"]["labels"] = [
                "quarantined-test"
            ]
            input_path.write_text(json.dumps(input_snapshot), encoding="utf-8")
            work = root / "work"
            cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=root / "state",
                work_dir=work,
                checkout=None,
                shepherd_author="ankj",
                input_path=input_path,
            )

            reconciliation = json.loads(
                (work / "quarantine-reconciliation.json").read_text(encoding="utf-8")
            )
            proposals = json.loads(
                (work / "action-proposals.json").read_text(encoding="utf-8")
            )

            self.assertEqual([], reconciliation["findings"])
            self.assertEqual([1], reconciliation["unverifiableIssueNumbers"])
            self.assertEqual([], proposals["proposals"])
            self.assertIn(
                "## Quarantine source reconciliation",
                (work / "report-details.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "could not be verified against source: #1",
                (work / "report-details.md").read_text(encoding="utf-8"),
            )

    def test_fails_closed_when_quarantine_source_inspection_is_unavailable(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            state = root / "state"
            input_path = root / "input.json"
            input_snapshot = snapshot("2026-08-28T20:00:00Z")
            input_snapshot["evidence"]["issue:1"]["payload"]["labels"] = []
            add_class_a_retry_evidence(
                input_snapshot,
                "Namespace.Type.FlakyTest",
            )
            input_path.write_text(
                json.dumps(input_snapshot),
                encoding="utf-8",
            )
            work = root / "work"
            cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=work,
                checkout=None,
                shepherd_author="ankj",
                input_path=input_path,
            )
            agent_judgments = {
                "schemaVersion": 1,
                "snapshotId": "snapshot:owner/repo:2026-08-28T20:00:00Z",
                "issues": [
                    {
                        "issueNumber": 1,
                        "category": "flaky-test",
                        "recommendations": [
                            {
                                "disposition": "review-quarantine",
                                "target": {
                                    "kind": "test",
                                    "value": "Namespace.Type.FlakyTest",
                                },
                                "confidence": "high",
                                "summary": "The test recovered on a retry.",
                                "evidenceIds": ["issue:1"],
                                "missingEvidence": [],
                                "reassessWhen": "After the quarantine PR merges.",
                            }
                        ],
                    }
                ],
            }
            agent_path = work / "agent-judgments.json"
            agent_path.write_text(json.dumps(agent_judgments), encoding="utf-8")

            finish_reviewed_cycle(
                work_dir=work,
                agent_judgments_path=agent_path,
            )

            plan = json.loads(
                (work / "quarantine-session.json").read_text(encoding="utf-8")
            )
            self.assertIsNone(plan["proposal"])
            self.assertEqual("blocked-targets", plan["suppressionReason"])
            self.assertEqual(
                [
                    {
                        "testName": "Namespace.Type.FlakyTest",
                        "reason": "source-inspection-unavailable",
                    }
                ],
                plan["blockedTargets"],
            )
            self.assertIn(
                "source-inspection-unavailable",
                (work / "report-details.md").read_text(encoding="utf-8"),
            )

    def test_fails_closed_when_observation_generation_is_invalid(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            state = root / "state"
            input_path = root / "input.json"
            input_snapshot = snapshot("2026-08-28T20:00:00Z")
            input_snapshot["evidence"]["issue:1"]["payload"]["labels"] = []
            add_class_a_retry_evidence(input_snapshot, "Namespace.Type.FlakyTest")
            del input_snapshot["evidence"]["run:200"]
            input_path.write_text(
                json.dumps(input_snapshot),
                encoding="utf-8",
            )
            work = root / "work"
            cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=work,
                checkout=None,
                shepherd_author="ankj",
                input_path=input_path,
            )
            agent_path = work / "agent-judgments.json"
            agent_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "snapshotId": (
                            "snapshot:owner/repo:"
                            "2026-08-28T20:00:00Z"
                        ),
                        "issues": [
                            {
                                "issueNumber": 1,
                                "category": "flaky-test",
                                "recommendations": [
                                    {
                                        "disposition": (
                                            "review-quarantine"
                                        ),
                                        "target": {
                                            "kind": "test",
                                            "value": (
                                                "Namespace.Type.FlakyTest"
                                            ),
                                        },
                                        "confidence": "high",
                                        "summary": (
                                            "The test recovered."
                                        ),
                                        "evidenceIds": ["issue:1"],
                                        "missingEvidence": [],
                                        "reassessWhen": (
                                            "After a retry."
                                        ),
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            finish_reviewed_cycle(
                work_dir=work,
                agent_judgments_path=agent_path,
            )

            evidence = json.loads(
                (work / "quarantine-evidence.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                "run:200:attempt:1:job:901 requires workflow-run evidence run:200.",
                evidence["error"],
            )
            plan = json.loads(
                (work / "quarantine-session.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIsNone(plan["proposal"])
            self.assertEqual(
                "insufficient-evidence-class",
                plan["blockedTargets"][0]["reason"],
            )

    def test_unsupported_quarantine_target_reaches_finalized_comment_proposals(self) -> None:
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            input_snapshot = snapshot("2026-08-28T20:00:00Z")
            issue = input_snapshot["evidence"]["issue:1"]["payload"]
            issue["labels"] = ["ci-failure-cause"]
            issue["updatedAt"] = "2026-08-28T19:59:00Z"
            test_name = "VS Code extension E2E (Linux, azure-functions)"
            add_class_a_retry_evidence(input_snapshot, test_name)
            input_path = root / "input.json"
            input_path.write_text(json.dumps(input_snapshot), encoding="utf-8")
            work = root / "work"
            cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=root / "state",
                work_dir=work,
                checkout=None,
                shepherd_author="ankj",
                input_path=input_path,
            )
            prepared = json.loads((work / "assessment-input.json").read_text(encoding="utf-8"))
            agent_path = work / "agent-judgments.json"
            agent_path.write_text(
                json.dumps({
                    "schemaVersion": 1,
                    "snapshotId": prepared["snapshotId"],
                    "issues": [{
                        "issueNumber": 1,
                        "category": "flaky-test",
                        "recommendations": [{
                            "disposition": "review-quarantine",
                            "target": {"kind": "test", "value": test_name},
                            "confidence": "high",
                            "summary": "Review the reported E2E target for quarantine.",
                            "evidenceIds": ["issue:1"],
                            "missingEvidence": [],
                            "reassessWhen": "After the quarantine decision.",
                        }],
                    }],
                }),
                encoding="utf-8",
            )

            result = finish_reviewed_cycle(work_dir=work, agent_judgments_path=agent_path)

            self.assertEqual("completed", result["stage"])
            plan = json.loads((work / "quarantine-session.json").read_text(encoding="utf-8"))
            self.assertIsNone(plan["proposal"])
            self.assertEqual([{
                "testName": test_name,
                "reason": "not-a-test-method",
                "issueNumbers": [1],
                "issueUrls": ["https://github.com/owner/repo/issues/1"],
            }], plan["blockedTargets"])
            proposals = json.loads((work / "action-proposals.json").read_text(encoding="utf-8"))
            proposal, = proposals["proposals"]
            self.assertEqual("create-comment", proposal["operation"])
            self.assertTrue(proposal["actionId"].endswith("quarantine-blocked-comment"))
            self.assertTrue(proposal["executionEligibility"]["eligible"])
            selection = json.loads((work / "comment-selection.json").read_text(encoding="utf-8"))
            self.assertEqual([proposal["actionId"]], selection["selectedActionIds"])
            self.assertFalse((root / "state" / "action-events.jsonl").exists())

    def test_proposes_only_a_source_resolved_quarantine_candidate(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            state = root / "state"
            input_path = root / "input.json"
            input_snapshot = snapshot("2026-08-28T20:00:00Z")
            input_snapshot["evidence"]["issue:1"]["payload"]["labels"] = []
            test_name = (
                "Aspire.Hosting.Tests.SecretsStoreTests."
                "GetOrSetUserSecret_SavesValueToUserSecrets"
            )
            add_class_a_retry_evidence(input_snapshot, test_name)
            input_path.write_text(
                json.dumps(input_snapshot),
                encoding="utf-8",
            )
            work = root / "work"
            cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=work,
                checkout=REPOSITORY_ROOT,
                shepherd_author="ankj",
                input_path=input_path,
            )
            agent_path = work / "agent-judgments.json"
            agent_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "snapshotId": (
                            "snapshot:owner/repo:2026-08-28T20:00:00Z"
                        ),
                        "issues": [
                            {
                                "issueNumber": 1,
                                "category": "flaky-test",
                                "recommendations": [
                                    {
                                        "disposition": "review-quarantine",
                                        "target": {
                                            "kind": "test",
                                            "value": test_name,
                                        },
                                        "confidence": "high",
                                        "summary": (
                                            "The test recovered on a retry."
                                        ),
                                        "evidenceIds": ["issue:1"],
                                        "missingEvidence": [],
                                        "reassessWhen": (
                                            "After the quarantine PR merges."
                                        ),
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            finish_reviewed_cycle(
                work_dir=work,
                agent_judgments_path=agent_path,
            )

            plan = json.loads(
                (work / "quarantine-session.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [test_name],
                [
                    test["testName"]
                    for test in plan["proposal"]["tests"]
                ],
            )
            self.assertEqual(
                {
                    "file": "Aspire.Hosting.Tests/SecretsStoreTests.cs",
                    "line": 28,
                },
                plan["proposal"]["tests"][0]["sourceLocation"],
            )
            proposed_test = plan["proposal"]["tests"][0]
            self.assertEqual("A", proposed_test["evidenceClass"])
            self.assertEqual(
                "occurrence:1:200:1:901:1",
                proposed_test["failureOccurrenceId"],
            )
            self.assertEqual(
                (
                    "coverage:run:200:attempt:2:job:902:test:"
                    "Aspire.Hosting.Tests.SecretsStoreTests."
                    "GetOrSetUserSecret_SavesValueToUserSecrets"
                ),
                proposed_test["recoveryCoverageId"],
            )
            quarantine_evidence = json.loads(
                (work / "quarantine-evidence.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [test_name],
                [
                    occurrence["testName"]
                    for occurrence in quarantine_evidence["occurrences"]
                    if occurrence.get(
                        "testNameEvidenceId",
                        "",
                    ).endswith(":test-results")
                ],
            )
            self.assertEqual(
                [test_name],
                [
                    coverage["testName"]
                    for coverage in quarantine_evidence["coverage"]
                    if coverage["subjectKind"] == "test"
                ],
            )
            self.assertRegex(
                plan["proposal"]["sourceRevision"],
                r"^[0-9a-f]{40}$",
            )
            self.assertRegex(
                plan["proposal"]["sourceTreeDigest"],
                r"^sha256:[0-9a-f]{64}$",
            )

    def test_bootstraps_review_events_for_state_created_before_scheduling(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            state = root / "state"
            first_input = root / "input-1.json"
            first_input.write_text(
                json.dumps(snapshot("2026-08-20T12:00:00Z")),
                encoding="utf-8",
            )
            first_work = root / "work-1"
            cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=first_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=first_input,
            )
            finish_reviewed_cycle(
                work_dir=first_work,
                agent_judgments_path=first_work / "agent-judgments.json",
            )
            (state / "ledgers" / "review-events.jsonl").unlink()

            next_input = root / "input-2.json"
            next_input.write_text(
                json.dumps(snapshot("2026-08-21T12:00:00Z")),
                encoding="utf-8",
            )
            next_work = root / "work-2"
            started = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=next_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=next_input,
            )
            selection = json.loads(
                (next_work / "review-selection.json").read_text(encoding="utf-8")
            )

            self.assertEqual("awaiting-review", started["stage"])
            self.assertEqual("first-seen", selection["selected"][0]["changeClass"])
            self.assertEqual(
                ["first-seen"],
                selection["selected"][0]["changeReasons"],
            )
            self.assertIn(
                "initial-assessment",
                selection["selected"][0]["reviewReasons"],
            )

    def test_known_delegated_issue_waits_for_future_wakeup_outside_assessment(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            state = root / "state"
            first_snapshot = snapshot("2026-08-20T12:00:00Z")
            first_snapshot["openIssues"] = [1, 2]
            second_issue = copy.deepcopy(first_snapshot["evidence"]["issue:1"])
            second_issue["url"] = "https://github.com/owner/repo/issues/2"
            second_issue["payload"].update(
                number=2,
                url=second_issue["url"],
            )
            first_snapshot["evidence"]["issue:2"] = second_issue
            first_input = root / "input-1.json"
            first_input.write_text(json.dumps(first_snapshot), encoding="utf-8")
            first_work = root / "work-1"
            cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=first_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=first_input,
            )
            finish_reviewed_cycle(
                work_dir=first_work,
                agent_judgments_path=first_work / "agent-judgments.json",
            )
            record_review_wakeup(
                state,
                "owner/repo",
                target_kind="issue",
                target_number=1,
                evaluate_at="2026-08-27T12:00:00Z",
                reason="human-stale-progress",
            )
            wakeups_path = state / "ledgers" / "review-wakeups.jsonl"
            wakeups_before = wakeups_path.read_bytes()

            next_snapshot = copy.deepcopy(first_snapshot)
            next_snapshot.update(
                collectedAt="2026-08-21T12:00:00Z",
                openIssues=[2],
                delegatedIssues=[1],
                delegatedIssueDetails=[{"number": 1}],
            )
            next_input = root / "input-2.json"
            next_input.write_text(json.dumps(next_snapshot), encoding="utf-8")
            next_work = root / "work-2"
            completed = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=next_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=next_input,
            )

            self.assertEqual("completed", completed["stage"])
            self.assertEqual(0, completed["issueReviewCount"])
            for filename, expected_numbers in (
                ("assessment-input.json", [2]),
                ("assessment-defaults.json", [2]),
                ("agent-input.json", []),
                ("judgments.json", [2]),
            ):
                with self.subTest(filename=filename):
                    document = json.loads(
                        (next_work / filename).read_text(encoding="utf-8")
                    )
                    self.assertEqual(
                        expected_numbers,
                        [issue["issueNumber"] for issue in document["issues"]],
                    )
            selection = json.loads(
                (next_work / "review-selection.json").read_text(encoding="utf-8")
            )
            self.assertEqual([], selection["selected"])
            self.assertEqual([2], [issue["issueNumber"] for issue in selection["omitted"]])
            expected_context = {
                "lastReviewedAt": "2026-08-20T12:00:00Z",
                "reassessAt": "2026-08-27T12:00:00Z",
                "wakeReason": "human-stale-progress",
            }
            schedule = json.loads(
                (next_work / "review-schedule.json").read_text(encoding="utf-8")
            )
            self.assertEqual([], schedule["dueIssueNumbers"])
            self.assertEqual(expected_context, schedule["issues"]["1"])
            self.assertEqual(wakeups_before, wakeups_path.read_bytes())
            due_schedule = load_review_schedule(
                state,
                "owner/repo",
                "2026-08-27T12:00:00Z",
                issue_numbers=[1, 2],
                pull_request_numbers=[],
            )
            self.assertEqual([1], due_schedule["dueIssueNumbers"])
            self.assertEqual(expected_context, due_schedule["issues"]["1"])

    def test_reselects_an_unchanged_case_when_its_typed_wakeup_becomes_due(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            state = root / "state"

            first_input = root / "input-1.json"
            first_input.write_text(
                json.dumps(snapshot("2026-08-20T12:00:00Z")),
                encoding="utf-8",
            )
            first_work = root / "work-1"
            started = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=first_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=first_input,
            )
            finish_reviewed_cycle(
                work_dir=first_work,
                agent_judgments_path=first_work / "agent-judgments.json",
            )
            self.assertEqual("awaiting-review", started["stage"])
            record_review_wakeup(
                state,
                "owner/repo",
                target_kind="issue",
                target_number=1,
                evaluate_at="2026-08-27T12:00:00Z",
                reason="closure-without-recurrence",
            )

            due_input = root / "input-2.json"
            due_snapshot = snapshot("2026-08-27T12:00:00Z")
            due_snapshot["openIssues"] = []
            due_snapshot["delegatedIssues"] = [1]
            due_snapshot["delegatedIssueDetails"] = [{"number": 1}]
            due_input.write_text(
                json.dumps(due_snapshot),
                encoding="utf-8",
            )
            due_work = root / "work-2"
            due = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=due_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=due_input,
            )

            self.assertEqual("awaiting-review", due["stage"])
            selection = json.loads(
                (due_work / "review-selection.json").read_text(encoding="utf-8")
            )
            self.assertEqual("due", selection["selected"][0]["changeClass"])
            self.assertEqual(
                "2026-08-20T12:00:00Z",
                selection["selected"][0]["lastReviewedAt"],
            )
            self.assertEqual(
                "2026-08-27T12:00:00Z",
                selection["selected"][0]["reassessAt"],
            )
            finish_reviewed_cycle(
                work_dir=due_work,
                agent_judgments_path=due_work / "agent-judgments.json",
            )

            after_review_input = root / "input-3.json"
            after_review_input.write_text(
                json.dumps(snapshot("2026-08-28T12:00:00Z")),
                encoding="utf-8",
            )
            after_review = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=root / "work-3",
                checkout=None,
                shepherd_author="ankj",
                input_path=after_review_input,
            )
            self.assertEqual("completed", after_review["stage"])
            self.assertEqual(0, after_review["issueReviewCount"])

    def test_resumes_agent_review_then_auto_finalizes_unchanged_cycle(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            state = root / "state"
            first_work = root / "work-1"
            first_input = root / "input-1.json"
            first_input.write_text(
                json.dumps(snapshot("2026-08-27T12:00:00Z")),
                encoding="utf-8",
            )

            started = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=first_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=first_input,
                max_comments=1,
            )

            self.assertEqual("awaiting-review", started["stage"])
            self.assertEqual(1, started["issueReviewCount"])
            self.assertTrue((first_work / "assessment-defaults.json").is_file())
            selected = json.loads(
                (first_work / "review-selection.json").read_text(encoding="utf-8")
            )
            agent_input = json.loads(
                (first_work / "agent-input.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [entry["issueNumber"] for entry in selected["selected"]],
                [issue["issueNumber"] for issue in agent_input["issues"]],
            )
            self.assertTrue((first_work / "agent-pull-request-judgments.json").is_file())
            agent_judgments = first_work / "agent-judgments.json"
            self.assertTrue(agent_judgments.is_file())

            with patch.object(
                cycle_script, "render_run_markdown", wraps=cycle_script.render_run_markdown,
            ) as render_report:
                completed = finish_reviewed_cycle(
                    work_dir=first_work,
                    agent_judgments_path=agent_judgments,
                )
            render_report.assert_called_once()
            self.assertEqual(
                json.loads((first_work / "assessment-batches.json").read_text(encoding="utf-8")),
                render_report.call_args.kwargs["assessment_manifest"],
            )
            self.assertIsNone(render_report.call_args.kwargs["pre_expansion_assessment_manifest"])

            self.assertEqual("completed", completed["stage"])
            self.assertTrue((first_work / "report.md").is_file())
            self.assertTrue((first_work / "action-proposals.json").is_file())
            self.assertTrue((first_work / "comment-selection.json").is_file())
            self.assertTrue((first_work / "policy-selection.json").is_file())
            self.assertTrue((first_work / "coordinator-projection.json").is_file())
            comment_selection = json.loads(
                (first_work / "comment-selection.json").read_text(encoding="utf-8")
            )
            self.assertEqual(1, comment_selection["maxComments"])
            proposals = json.loads(
                (first_work / "action-proposals.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                {
                    "schemaVersion": 1,
                    "evidenceRound": 0,
                    "managedItemCoverage": {
                        "schemaVersion": 2,
                        "valid": True,
                        "blockers": [],
                        "globalBlockers": [],
                        "blockedScopes": [],
                    },
                },
                proposals["productionPilotCapability"],
            )
            report = (first_work / "report-details.md").read_text(encoding="utf-8")
            self.assertIn("## Legacy production comment pilot selection", report)
            self.assertIn(
                "## Autonomous policy selection at cycle finalization",
                report,
            )
            self.assertIn(
                "This section is authoritative for autonomous policy execution",
                report,
            )
            self.assertNotIn("## Policy-aware action selection", report)
            self.assertEqual("awaiting-policy", completed["coordinatorStage"])
            projection = json.loads(
                (first_work / "coordinator-projection.json").read_text(
                    encoding="utf-8"
                )
            )
            policy_selection = json.loads(
                (first_work / "policy-selection.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                projection["stateRevision"],
                policy_selection["coordinatorStateRevision"],
            )
            self.assertEqual([], policy_selection["selectedActionIds"])
            self.assertEqual(1, len(list((state / "runs").iterdir())))

            second_work = root / "work-2"
            second_input = root / "input-2.json"
            second_input.write_text(
                json.dumps(snapshot("2026-08-28T12:00:00Z")),
                encoding="utf-8",
            )
            unchanged = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=second_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=second_input,
            )

            self.assertEqual("completed", unchanged["stage"])
            self.assertEqual(0, unchanged["issueReviewCount"])
            self.assertEqual(2, len(list((state / "runs").iterdir())))
            unchanged_report = (second_work / "report-details.md").read_text(encoding="utf-8")
            self.assertNotIn("Unclassified CI failure", unchanged_report)
            self.assertIn(
                "**Carried forward unchanged cases:** 1",
                unchanged_report,
            )

            changed_work = root / "work-3"
            changed_input = root / "input-3.json"
            changed_input.write_text(
                json.dumps(
                    snapshot(
                        "2026-08-29T12:00:00Z",
                        title="Unclassified CI failure with a new signature",
                    )
                ),
                encoding="utf-8",
            )
            changed = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=changed_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=changed_input,
            )

            self.assertEqual("awaiting-review", changed["stage"])
            self.assertEqual(1, changed["issueReviewCount"])
            changed_selection = json.loads(
                (changed_work / "review-selection.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                ["derived-assessment-changed"],
                changed_selection["selected"][0]["changeReasons"],
            )
            self.assertEqual(
                "investigate",
                changed_selection["selected"][0]["previousDisposition"],
            )

    def test_checkout_revision_alone_does_not_reselect_unchanged_issue(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            state = root / "state"
            first_snapshot = snapshot("2026-08-27T12:00:00Z")
            add_source_path_evidence(first_snapshot, "a" * 40)
            first_input = root / "input-1.json"
            first_input.write_text(json.dumps(first_snapshot), encoding="utf-8")
            first_work = root / "work-1"
            cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=first_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=first_input,
            )
            finish_reviewed_cycle(
                work_dir=first_work,
                agent_judgments_path=first_work / "agent-judgments.json",
            )

            second_snapshot = snapshot("2026-08-28T12:00:00Z")
            add_source_path_evidence(second_snapshot, "b" * 40)
            second_input = root / "input-2.json"
            second_input.write_text(json.dumps(second_snapshot), encoding="utf-8")
            second_work = root / "work-2"

            result = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=second_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=second_input,
            )

            self.assertEqual("completed", result["stage"])
            self.assertEqual(0, result["issueReviewCount"])

    def test_unchanged_cycle_carries_forward_issue_agent_override(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            state = root / "state"
            first_input = root / "input-1.json"
            first_snapshot = snapshot(
                "2026-08-27T12:00:00Z",
                title="[13.5] Changelog feedback",
            )
            first_snapshot["evidence"]["issue:1"]["payload"]["updatedAt"] = (
                "2026-08-27T11:00:00Z"
            )
            first_input.write_text(
                json.dumps(first_snapshot),
                encoding="utf-8",
            )
            first_work = root / "work-1"
            started = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=first_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=first_input,
            )
            defaults = json.loads(
                (first_work / "assessment-defaults.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                "watch",
                defaults["issues"][0]["defaultJudgment"]["recommendations"][0][
                    "disposition"
                ],
            )
            agent_judgments = {
                "schemaVersion": 1,
                "snapshotId": started["snapshotId"],
                "issues": [
                    {
                        "issueNumber": 1,
                        "category": "unknown",
                        "recommendations": [
                            {
                                "disposition": "investigate",
                                "target": {"kind": "issue", "value": 1},
                                "confidence": "low",
                                "summary": "This is not a CI failure signature.",
                                "evidenceIds": ["issue:1"],
                                "missingEvidence": ["recognized-producer-ledger"],
                                "reassessWhen": "After the next evidence update.",
                            }
                        ],
                    }
                ],
            }
            agent_path = first_work / "agent-judgments.json"
            agent_path.write_text(json.dumps(agent_judgments), encoding="utf-8")
            finish_reviewed_cycle(
                work_dir=first_work,
                agent_judgments_path=agent_path,
            )

            second_input = root / "input-2.json"
            second_snapshot = snapshot(
                "2026-08-28T12:00:00Z",
                title="[13.5] Changelog feedback",
            )
            second_snapshot["evidence"]["issue:1"]["payload"]["updatedAt"] = (
                "2026-08-27T11:00:00Z"
            )
            second_input.write_text(
                json.dumps(second_snapshot),
                encoding="utf-8",
            )
            second_work = root / "work-2"
            completed = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=second_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=second_input,
            )

            self.assertEqual("completed", completed["stage"])
            self.assertEqual(0, completed["issueReviewCount"])
            judgments = json.loads(
                (second_work / "judgments.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                "investigate",
                judgments["issues"][0]["recommendations"][0]["disposition"],
            )

    def test_unchanged_cycle_carries_forward_pull_request_agent_override(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            state = root / "state"
            first_collected_at = "2026-08-27T12:00:00Z"
            first_snapshot = pull_request_snapshot(first_collected_at)
            current_state = first_snapshot["evidence"]["pr:23"]["payload"][
                "currentState"
            ]
            current_state["review"]["decision"] = "changes-requested"
            current_state["mergeable"] = False
            current_state["mergeableState"] = "dirty"
            first_input = root / "input-1.json"
            first_input.write_text(json.dumps(first_snapshot), encoding="utf-8")
            first_work = root / "work-1"
            started = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=first_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=first_input,
            )
            pull_request_judgments = {
                "schemaVersion": 1,
                "snapshotId": started["snapshotId"],
                "pullRequests": [
                    {
                        "pullRequestNumber": 23,
                        "disposition": "ping-human",
                        "summary": "The pull request needs a conflict decision.",
                        "evidenceIds": ["pr:23"],
                        "missingEvidence": [],
                        "reassessWhen": "After the conflict is resolved.",
                        "humanEscalation": {
                            "context": "The branch no longer merges cleanly.",
                            "whyHuman": "The author must choose the resolution.",
                            "question": "Should this branch be rebased or superseded?",
                            "suggestedNextSteps": ["Rebase the branch."],
                            "routingHint": "Ask the pull request author.",
                        },
                    }
                ],
            }
            pull_request_judgments_path = (
                first_work / "agent-pull-request-judgments.json"
            )
            pull_request_judgments_path.write_text(
                json.dumps(pull_request_judgments),
                encoding="utf-8",
            )
            finish_reviewed_cycle(
                work_dir=first_work,
                agent_judgments_path=first_work / "agent-judgments.json",
                pull_request_judgments_path=pull_request_judgments_path,
            )

            second_snapshot = pull_request_snapshot("2026-08-28T12:00:00Z")
            second_snapshot["pullRequests"][0]["updatedAt"] = first_collected_at
            second_state = second_snapshot["evidence"]["pr:23"]["payload"][
                "currentState"
            ]
            second_state["review"]["decision"] = "changes-requested"
            second_state["mergeable"] = False
            second_state["mergeableState"] = "dirty"
            second_input = root / "input-2.json"
            second_input.write_text(json.dumps(second_snapshot), encoding="utf-8")
            second_work = root / "work-2"
            completed = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=second_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=second_input,
            )

            self.assertEqual("completed", completed["stage"])
            self.assertEqual(0, completed["pullRequestReviewCount"])
            judgments = json.loads(
                (second_work / "pull-request-judgments.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                [(23, "ping-human")],
                [
                    (entry["pullRequestNumber"], entry["disposition"])
                    for entry in judgments["pullRequests"]
                ],
            )
            self.assertIn(
                "Human input:",
                (second_work / "report.md").read_text(encoding="utf-8"),
            )

    def test_legacy_pull_request_handoff_without_judgments_is_reviewed_once(
        self,
    ) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            state = root / "state"
            first_collected_at = "2026-08-27T12:00:00Z"
            first_input = root / "input-1.json"
            first_input.write_text(
                json.dumps(pull_request_snapshot(first_collected_at)),
                encoding="utf-8",
            )
            first_work = root / "work-1"
            started = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=first_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=first_input,
            )
            finish_reviewed_cycle(
                work_dir=first_work,
                agent_judgments_path=first_work / "agent-judgments.json",
                pull_request_judgments_path=(
                    first_work / "agent-pull-request-judgments.json"
                ),
            )
            current = cycle_script.load_current(state, "owner/repo")
            self.assertIsNotNone(current)
            legacy_run = root / "legacy-run"
            legacy_run.mkdir()
            for name in ("snapshot.json", "pull-request-review.json"):
                (legacy_run / name).write_bytes(
                    (current.run_directory / name).read_bytes()
                )
            legacy_current = SimpleNamespace(
                run_directory=legacy_run,
                previous_decisions=current.previous_decisions,
            )

            second_snapshot = pull_request_snapshot("2026-08-28T12:00:00Z")
            second_snapshot["pullRequests"][0]["updatedAt"] = first_collected_at
            second_input = root / "input-2.json"
            second_input.write_text(json.dumps(second_snapshot), encoding="utf-8")

            with patch.object(
                cycle_script,
                "load_current",
                return_value=legacy_current,
            ):
                restarted = cycle_script.start_cycle(
                    repository="owner/repo",
                    state_dir=state,
                    work_dir=root / "work-2",
                    checkout=None,
                    shepherd_author="ankj",
                    input_path=second_input,
                )

            self.assertEqual("awaiting-review", restarted["stage"])
            self.assertEqual(1, restarted["pullRequestReviewCount"])

    def test_unchanged_default_pull_request_is_counted_without_retained_judgment(
        self,
    ) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            state = root / "state"
            first_collected_at = "2026-08-27T12:00:00Z"
            first_input = root / "input-1.json"
            first_input.write_text(
                json.dumps(pull_request_snapshot(first_collected_at)),
                encoding="utf-8",
            )
            first_work = root / "work-1"
            started = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=first_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=first_input,
            )
            (first_work / "agent-judgments.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "snapshotId": started["snapshotId"],
                        "issues": [],
                    }
                ),
                encoding="utf-8",
            )
            pull_request_judgments_path = (
                first_work / "agent-pull-request-judgments.json"
            )
            pull_request_judgments_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "snapshotId": started["snapshotId"],
                        "pullRequests": [],
                    }
                ),
                encoding="utf-8",
            )
            finish_reviewed_cycle(
                work_dir=first_work,
                agent_judgments_path=first_work / "agent-judgments.json",
                pull_request_judgments_path=pull_request_judgments_path,
            )

            second_snapshot = pull_request_snapshot("2026-08-28T12:00:00Z")
            second_snapshot["pullRequests"][0]["updatedAt"] = first_collected_at
            second_input = root / "input-2.json"
            second_input.write_text(
                json.dumps(second_snapshot),
                encoding="utf-8",
            )
            second_work = root / "work-2"

            completed = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=second_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=second_input,
            )

            self.assertEqual("completed", completed["stage"])
            self.assertEqual(0, completed["pullRequestReviewCount"])
            judgments = json.loads(
                (second_work / "pull-request-judgments.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual([], judgments["pullRequests"])
            report = (second_work / "report.md").read_text(encoding="utf-8")
            self.assertIn(
                "0 PRs selected for review",
                report,
            )
            self.assertIn("unchanged-stable", report)

    def test_interrupted_started_cycle_does_not_advance_current_state(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            state = root / "state"
            first_input = root / "input-1.json"
            first_input.write_text(
                json.dumps(snapshot("2026-08-27T12:00:00Z")),
                encoding="utf-8",
            )
            first_work = root / "work-1"
            started = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=first_work,
                checkout=None,
                shepherd_author="ankj",
                input_path=first_input,
            )
            (first_work / "agent-judgments.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "snapshotId": started["snapshotId"],
                        "issues": [],
                    }
                ),
                encoding="utf-8",
            )
            finish_reviewed_cycle(
                work_dir=first_work,
                agent_judgments_path=first_work / "agent-judgments.json",
            )
            current_path = state / "current.json"
            prior_current = current_path.read_bytes()

            second_input = root / "input-2.json"
            second_input.write_text(
                json.dumps(
                    snapshot(
                        "2026-08-28T12:00:00Z",
                        title="Materially changed CI failure",
                    )
                ),
                encoding="utf-8",
            )

            interrupted = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=state,
                work_dir=root / "work-2",
                checkout=None,
                shepherd_author="ankj",
                input_path=second_input,
            )

            self.assertEqual("awaiting-review", interrupted["stage"])
            self.assertEqual(prior_current, current_path.read_bytes())

    def test_finishes_pull_request_review_without_executable_pr_proposals(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            input_path = root / "input.json"
            input_path.write_text(
                json.dumps(pull_request_snapshot("2026-08-27T12:00:00Z")),
                encoding="utf-8",
            )
            work = root / "work"
            started = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=root / "state",
                work_dir=work,
                checkout=None,
                shepherd_author="ankj",
                input_path=input_path,
            )

            self.assertEqual("awaiting-review", started["stage"])
            self.assertEqual(0, started["issueReviewCount"])
            self.assertEqual(1, started["pullRequestReviewCount"])
            pull_request_judgments = work / "pull-request-judgments.json"
            pull_request_judgments.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "snapshotId": started["snapshotId"],
                        "pullRequests": [],
                    }
                ),
                encoding="utf-8",
            )

            completed = finish_reviewed_cycle(
                work_dir=work,
                agent_judgments_path=work / "agent-judgments.json",
                pull_request_judgments_path=pull_request_judgments,
            )

            self.assertEqual("completed", completed["stage"])
            self.assertIn("## Pull requests", (work / "report.md").read_text())
            proposals = json.loads(
                (work / "action-proposals.json").read_text(encoding="utf-8")
            )
            self.assertEqual([], proposals["proposals"])
            self.assertNotIn("unchangedPullRequestNumbers", proposals)
            self.assertNotIn("suppressedPullRequests", proposals)

    def test_report_surfaces_incomplete_collection(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            document = snapshot("2026-08-27T12:00:00Z")
            document["warnings"] = ["open bot-authored inventory is incomplete"]
            document["collectionErrors"] = [
                {
                    "stage": "open-bot-scan",
                    "endpoint": "/repos/owner/repo/issues?page=2",
                    "message": "rate limited",
                }
            ]
            document["openBotScan"] = {
                "status": "failed",
                "complete": False,
                "scannedPages": 1,
                "pageBudget": 40,
                "itemBudget": 250,
                "botAuthoredFound": 100,
                "botAuthoredAdopted": 100,
                "detail": "rate limited",
            }
            input_path = root / "input.json"
            input_path.write_text(json.dumps(document), encoding="utf-8")
            work = root / "work"
            started = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=root / "state",
                work_dir=work,
                checkout=None,
                shepherd_author="ankj",
                input_path=input_path,
            )
            agent_judgments = work / "agent-judgments.json"
            agent_judgments.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "snapshotId": started["snapshotId"],
                        "issues": [],
                    }
                ),
                encoding="utf-8",
            )

            finish_reviewed_cycle(
                work_dir=work,
                agent_judgments_path=agent_judgments,
            )

            report = (work / "report-details.md").read_text(encoding="utf-8")
            self.assertIn("## Collection completeness", report)
            self.assertIn("**Open bot scan:** `failed`", report)
            self.assertIn("**Collection errors:** 1", report)
            self.assertIn(
                "`open-bot-scan`: rate limited "
                "(`/repos/owner/repo/issues?page=2`)",
                report,
            )
            self.assertIn("open bot-authored inventory is incomplete", report)

    def test_report_surfaces_truncated_open_bot_scan(self) -> None:
        artifacts = Path(__file__).parent / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=artifacts) as scratch:
            root = Path(scratch)
            document = snapshot("2026-08-27T12:00:00Z")
            document["warnings"] = [
                "open bot-authored inventory is incomplete because its "
                "page budget was exhausted"
            ]
            document["openBotScan"] = {
                "status": "truncated",
                "complete": False,
                "scannedPages": 40,
                "pageBudget": 40,
                "itemBudget": 250,
                "botAuthoredFound": 100,
                "botAuthoredAdopted": 100,
                "detail": "page budget exhausted",
            }
            input_path = root / "input.json"
            input_path.write_text(json.dumps(document), encoding="utf-8")
            work = root / "work"
            started = cycle_script.start_cycle(
                repository="owner/repo",
                state_dir=root / "state",
                work_dir=work,
                checkout=None,
                shepherd_author="ankj",
                input_path=input_path,
            )
            agent_judgments = work / "agent-judgments.json"
            agent_judgments.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "snapshotId": started["snapshotId"],
                        "issues": [],
                    }
                ),
                encoding="utf-8",
            )
            finish_reviewed_cycle(
                work_dir=work,
                agent_judgments_path=agent_judgments,
            )

            report = (work / "report-details.md").read_text(encoding="utf-8")
            self.assertIn("**Open bot scan:** `truncated`", report)
            self.assertIn("page budget was exhausted", report)


if __name__ == "__main__":
    unittest.main()
