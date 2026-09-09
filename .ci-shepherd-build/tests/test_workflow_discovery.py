from __future__ import annotations

import json
import io
from contextlib import redirect_stdout
from datetime import UTC, datetime
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest.mock import patch

import collect as collect_script

from ci_shepherd.collector import Collector, InventoryResult, enrich_workflow_discovery
from ci_shepherd.actions import build_action_proposals
from ci_shepherd.actor import validate_action_proposals
from ci_shepherd.github import GitHubApiError, GitHubTextResponse
from ci_shepherd.history import record_history
from ci_shepherd.models import ValidationError, validate_snapshot
from ci_shepherd.lifecycle import prepare_assessment
from ci_shepherd.poc import build_compact_poc_input
from ci_shepherd.run_report import render_run_markdown
from ci_shepherd.refresh import RefreshPlan, plan_refresh
from ci_shepherd.workflow_discovery import BOUNDS
from tests.test_collector import ScriptedClient, make_issue
from tests.test_refresh import current_history


REPOSITORY = "owner/repo"
NOW = datetime(2026, 9, 8, 20, tzinfo=UTC)
REPOSITORY_INFO = {"id": 7, "full_name": REPOSITORY, "fork": False, "default_branch": "trunk"}
WORKFLOW_PATH = ".github/workflows/ci.yml"


def run(number: int, *, event: str = "push", conclusion: str = "failure") -> dict:
    return {
        "id": number, "workflow_id": 10, "path": WORKFLOW_PATH, "run_number": number,
        "run_attempt": 1, "event": event, "head_branch": "trunk", "head_sha": f"{number:040x}",
        "status": "completed", "conclusion": conclusion,
        "created_at": f"2026-09-08T12:{number:02d}:00Z", "updated_at": f"2026-09-08T13:{number:02d}:00Z",
        "run_started_at": f"2026-09-08T12:{number:02d}:00Z",
        "repository": REPOSITORY_INFO, "head_repository": REPOSITORY_INFO, "pull_requests": [],
        "url": f"https://api.github.com/repos/{REPOSITORY}/actions/runs/{number}",
        "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{number}",
        "name": "CI",
    }


def job(run_number: int, *, conclusion: str = "failure", number: int | None = None) -> dict:
    number = number or run_number * 100
    return {
        "id": number, "run_id": run_number, "run_attempt": 1,
        "head_sha": f"{run_number:040x}", "head_branch": "trunk",
        "name": "Tests (linux, net10.0)", "labels": ["ubuntu-latest"],
        "status": "completed", "conclusion": conclusion,
        "started_at": f"2026-09-08T12:{run_number:02d}:00Z",
        "completed_at": f"2026-09-08T13:{run_number:02d}:00Z",
        "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_number}/job/{number}",
        "check_run_url": f"https://api.github.com/repos/{REPOSITORY}/check-runs/{number}",
        "steps": [{"number": 1, "name": "Run tests", "status": "completed", "conclusion": conclusion}],
    }


def api_error(endpoint: str, status: int = 404) -> GitHubApiError:
    return GitHubApiError(
        category="not-found", endpoint=endpoint, status=status, headers={},
        retryable=False, attempts=1, sanitized_stderr="Unavailable",
    )


class DiscoveryClient(ScriptedClient):
    def __init__(self, responses: dict, **options) -> None:
        super().__init__(**options)
        self.responses = responses
        self.byte_limits = []

    def get_text(self, endpoint: str, max_bytes: int = 200_000) -> GitHubTextResponse:
        self.calls.append(("get_text", endpoint))
        self.byte_limits.append(max_bytes)
        parsed = urlsplit(endpoint)
        page = int(parse_qs(parsed.query).get("page", ["1"])[0])
        response = self.responses.get((parsed.path, page), self.responses.get(parsed.path))
        if response is None:
            raise AssertionError(f"Unexpected bounded GET: {endpoint}")
        if callable(response):
            response = response(parse_qs(parsed.query))
        if isinstance(response, Exception):
            raise response
        if isinstance(response, GitHubTextResponse):
            return response
        text = response if isinstance(response, str) else json.dumps(response)
        raw = text.encode("utf-8")
        return GitHubTextResponse(
            text=raw[:max_bytes].decode("utf-8"), truncated=len(raw) > max_bytes, status=200, headers={},
        )


def responses_for(runs: list[dict]) -> dict:
    def history(query: dict) -> dict:
        created = query.get("created", [""])[0]
        if ".." in created:
            start, end = created.split("..")
            rows = [
                item for item in runs
                if datetime.fromisoformat(start) <= datetime.fromisoformat(item["created_at"]) <= datetime.fromisoformat(end)
            ]
        else:
            rows = runs
        page = int(query.get("page", ["1"])[0])
        size = int(query.get("per_page", ["5"])[0])
        return {"total_count": len(rows), "workflow_runs": rows[(page - 1) * size:page * size]}

    return {
        f"/repos/{REPOSITORY}": REPOSITORY_INFO,
        f"/repos/{REPOSITORY}/actions/runs": {"total_count": len(runs), "workflow_runs": runs},
        f"/repos/{REPOSITORY}/actions/workflows/10/runs": history,
        **{f"/repos/{REPOSITORY}/actions/runs/{item['id']}": item for item in runs},
        **{
            f"/repos/{REPOSITORY}/actions/runs/{item['id']}/attempts/{item['run_attempt']}/jobs": {
                "total_count": 1, "jobs": [{
                    **job(item["id"], conclusion=item["conclusion"], number=item["id"] * 100 + item["run_attempt"] - 1),
                    "run_attempt": item["run_attempt"],
                }],
            }
            for item in runs
        },
        **{
            f"/repos/{REPOSITORY}/actions/jobs/{item['id'] * 100 + item['run_attempt'] - 1}/logs": "Error: connection timed out\n"
            for item in runs if item["conclusion"] == "failure"
        },
    }


def empty_inventory() -> InventoryResult:
    return InventoryResult([], [], {}, [], [], {})


def issue_inventory(*, labels: list[str] | None = None) -> InventoryResult:
    issue = make_issue(
        42, labels=labels or ["ci-failure-cause"],
        body=f"Build: https://github.com/{REPOSITORY}/actions/runs/1\nJob: Tests (linux, net10.0)\n",
    )
    client = ScriptedClient(pages={
        "/repos/owner/repo/issues?state=open&labels=ci-failure-cause&per_page=100": [issue],
        "/repos/owner/repo/issues?state=open&labels=automation-broken&per_page=100": [],
        "/repos/owner/repo/issues/42/comments": [],
    })
    return Collector(client, REPOSITORY, NOW).collect(include_supporting=False, include_timeline=False)


class WorkflowDiscoveryTests(unittest.TestCase):
    def test_incomplete_neighbor_does_not_hide_a_fully_observed_current_failure(self) -> None:
        older = {
            **run(1), "created_at": "2026-08-15T12:01:00Z",
            "updated_at": "2026-08-15T13:01:00Z", "run_started_at": "2026-08-15T12:01:00Z",
        }
        responses = responses_for([run(3), run(2, conclusion="success"), older])
        responses[f"/repos/{REPOSITORY}/actions/runs"] = {
            "total_count": 2, "workflow_runs": [run(3), run(2, conclusion="success")],
        }
        responses[f"/repos/{REPOSITORY}/actions/runs/2/attempts/1/jobs"] = {"total_count": 2, "jobs": []}
        responses[f"/repos/{REPOSITORY}/actions/runs/1/attempts/1/jobs"]["jobs"][0].update(
            started_at="2026-08-15T12:01:00Z", completed_at="2026-08-15T13:01:00Z",
        )
        for number in (1, 3):
            responses[f"/repos/{REPOSITORY}/actions/jobs/{number * 100}/logs"] = (
                "src/File.cs(1,1): error CS1002: ; expected"
            )
        inventory = enrich_workflow_discovery(issue_inventory(), DiscoveryClient(responses), REPOSITORY, NOW)
        snapshot = collect_script.build_snapshot(REPOSITORY, NOW, inventory)
        validate_snapshot(snapshot)
        health = prepare_assessment(snapshot)["issues"][0]["workflowHealth"]
        self.assertFalse(health["coverageComplete"])
        self.assertFalse(health["recurrent"])
        self.assertFalse(health["closureAllowed"])
        self.assertTrue(health["current"])
        self.assertEqual("delegate-copilot", health["route"])

    def test_other_runner_architecture_cannot_supply_recovery_from_another_issue(self) -> None:
        issues = [
            make_issue(
                number, labels=["ci-failure-cause"],
                body=f"<!-- ci-failure-cause:compile-error-{number} -->\n## Occurrences\n"
                     "| Date | Build | Job | PR |\n|---|---|---|---|\n"
                     f"| 2026-09-08 | [{source}](https://github.com/{REPOSITORY}/actions/runs/{source})"
                     " | Tests (linux, net10.0) | #0 |\n",
            )
            for number, source in ((42, 1), (43, 2))
        ]
        inventory = Collector(ScriptedClient(pages={
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": issues,
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/42/comments": [],
            f"/repos/{REPOSITORY}/issues/43/comments": [],
        }), REPOSITORY, NOW).collect(include_supporting=False, include_timeline=False)
        responses = responses_for([run(3, conclusion="success"), run(2), run(1)])
        for number in (1, 2, 3):
            responses[f"/repos/{REPOSITORY}/actions/runs/{number}/attempts/1/jobs"]["jobs"][0]["labels"] = [
                "ubuntu-latest", "ARM64" if number == 1 else "X64",
            ]
            if number < 3:
                responses[f"/repos/{REPOSITORY}/actions/jobs/{number * 100}/logs"] = (
                    "src/File.cs(1,1): error CS1002: ; expected"
                    if number == 1 else "src/File.cs(1,1): error CS0246: Missing type"
                )
        inventory = enrich_workflow_discovery(inventory, DiscoveryClient(responses), REPOSITORY, NOW)
        snapshot = collect_script.build_snapshot(REPOSITORY, NOW, inventory)
        validate_snapshot(snapshot)
        prepared = prepare_assessment(snapshot)
        affected = next(item for item in prepared["issues"] if item["issueNumber"] == 42)
        self.assertIsNone(affected["recovery"]["subjects"][0]["coverage"])
        self.assertFalse(affected["workflowHealth"]["closureAllowed"])
        self.assertEqual("unknown", affected["workflowHealth"]["samples"][0]["outcome"])
        compact = build_compact_poc_input(prepared)
        proposals = build_action_proposals(snapshot, prepared, {
            "schemaVersion": 1, "snapshotId": prepared["snapshotId"],
            "issues": [item["defaultJudgment"] for item in compact["issues"]],
        }, "radical", agent_input=compact)
        self.assertEqual([], [
            proposal for proposal in proposals["proposals"]
            if proposal["issueNumber"] == 42 and proposal["operation"] == "close-issue"
        ])

    def test_absent_job_in_complete_window_is_an_unknown_sample_not_consecutive_failure(self) -> None:
        responses = responses_for([run(3), run(2, conclusion="success"), run(1)])
        responses[f"/repos/{REPOSITORY}/actions/runs/2/attempts/1/jobs"] = {"total_count": 0, "jobs": []}
        for number in (1, 3):
            responses[f"/repos/{REPOSITORY}/actions/jobs/{number * 100}/logs"] = "##[error]Download failed: HTTP 503"
        inventory = enrich_workflow_discovery(
            issue_inventory(), DiscoveryClient(responses), REPOSITORY, NOW,
        )
        snapshot = collect_script.build_snapshot(REPOSITORY, NOW, inventory)
        validate_snapshot(snapshot)
        health = prepare_assessment(snapshot)["issues"][0]["workflowHealth"]
        self.assertEqual([3, 2, 1], health["sampleRunIds"])
        self.assertEqual(["failure", "unknown", "failure"], [sample["outcome"] for sample in health["samples"]])
        self.assertFalse(health["recurrent"])
        self.assertEqual("watch", health["route"])

    def test_failed_run_without_job_access_is_visible_with_coverage_gap(self) -> None:
        responses = responses_for([run(3)])
        endpoint = f"/repos/{REPOSITORY}/actions/runs/3/attempts/1/jobs"
        responses[endpoint] = api_error(endpoint)
        inventory = enrich_workflow_discovery(empty_inventory(), DiscoveryClient(responses), REPOSITORY, NOW)
        snapshot = collect_script.build_snapshot(REPOSITORY, NOW, inventory)
        validate_snapshot(snapshot)
        report = render_run_markdown(snapshot, prepare_assessment(snapshot), {"issues": []})
        self.assertIn("Job coverage incomplete", report)
        self.assertIn("Some failed jobs may be unobserved", report)
        self.assertIn("job-inventory-incomplete", report)
        self.assertIn("/actions/runs/3", report)
        self.assertIn("0 complete / 1 collected", report)

    def test_producer_snapshot_drives_recurrence_and_read_only_untracked_report(self) -> None:
        raw_runs = [run(3), run(2), run(1)]
        responses = responses_for(raw_runs)
        for number in (1, 2, 3):
            responses[f"/repos/{REPOSITORY}/actions/jobs/{number * 100}/logs"] = (
                "##[error]Download failed: https://downloads.example.test/sdk.tar.gz returned HTTP 503"
            )
        inventory = enrich_workflow_discovery(
            issue_inventory(), DiscoveryClient(responses), REPOSITORY, NOW,
        )
        snapshot = collect_script.build_snapshot(REPOSITORY, NOW, inventory)
        validate_snapshot(snapshot)
        prepared = prepare_assessment(snapshot)
        issue = build_compact_poc_input(prepared)["issues"][0]
        self.assertTrue(issue["workflowHealth"]["coverageComplete"])
        self.assertEqual([3, 2, 1], issue["workflowHealth"]["sampleRunIds"])
        self.assertTrue(issue["workflowHealth"]["recurrent"])
        self.assertEqual("delegate-copilot", issue["defaultJudgment"]["recommendations"][0]["disposition"])
        compact = build_compact_poc_input(prepared)
        proposals = build_action_proposals(snapshot, prepared, {
            "schemaVersion": 1, "snapshotId": prepared["snapshotId"], "issues": [issue["defaultJudgment"]],
        }, "radical", agent_input=compact)
        validate_action_proposals(proposals)
        self.assertEqual(["assign-copilot"], [proposal["operation"] for proposal in proposals["proposals"]])
        self.assertIn("`Refs #42`", proposals["proposals"][0]["customInstructions"])
        inventory = enrich_workflow_discovery(
            empty_inventory(), DiscoveryClient(responses_for(raw_runs)), REPOSITORY, NOW,
        )
        snapshot = collect_script.build_snapshot(REPOSITORY, NOW, inventory)
        prepared = prepare_assessment(snapshot)
        self.assertEqual([], prepared["issues"])
        report = render_run_markdown(snapshot, prepared, {"issues": []})
        self.assertIn("## Default-branch workflow discovery", report)
        self.assertIn("No tracker in collected evidence", report)
        self.assertIn("/actions/runs/3/job/300", report)
        self.assertIn("does not create issues or assign Copilot", report)

    def test_refresh_retains_original_repair_subject_and_current_closed_issue_state(self) -> None:
        first = enrich_workflow_discovery(
            issue_inventory(), DiscoveryClient(responses_for([run(1)])), REPOSITORY, NOW,
        )
        previous = collect_script.build_snapshot(REPOSITORY, NOW, first)
        tracked = {
            "repository": REPOSITORY, "issueNumber": 42, "actionId": "assignment:42",
            "taskId": "task-42", "taskState": "completed", "taskObservation": "available",
            "startedAt": "2026-09-08T13:01:30Z", "lifecycle": "completed",
            "attemptOutcome": "merged", "requiresNewDecision": True, "requiresHuman": False,
            "retired": True, "issueOpen": False, "copilotAssigned": False, "pullRequests": [{
                "databaseId": 101, "globalId": "PR_101", "number": 101, "state": "merged",
                "isDraft": False, "changedFiles": 1, "mergedAt": "2026-09-08T13:02:00Z",
                "mergeCommitSha": f"{3:040x}",
            }],
        }
        previous["delegationStatus"]["records"] = [tracked]
        closed = make_issue(42, labels=["ci-failure-cause"], state="closed", body="Resolved by the repair.")
        inventory = replace(empty_inventory(), delegated_issues=[closed])
        newer = run(3, conclusion="success")
        newer["run_started_at"] = newer["created_at"] = "2026-09-08T13:03:00Z"
        responses = responses_for([newer])
        responses[f"/repos/{REPOSITORY}/actions/runs/1/attempts/1"] = run(1)
        responses[f"/repos/{REPOSITORY}/actions/runs/1/attempts/1/jobs"] = {"total_count": 1, "jobs": [job(1)]}
        responses[f"/repos/{REPOSITORY}/actions/runs/3/attempts/1/jobs"]["jobs"][0].update(
            started_at="2026-09-08T13:03:00Z", completed_at="2026-09-08T13:04:00Z",
        )
        inventory = enrich_workflow_discovery(
            inventory, DiscoveryClient(responses), REPOSITORY, NOW, previous_snapshot=previous,
        )
        snapshot = collect_script.build_snapshot(
            REPOSITORY, NOW, inventory, delegation_status={"status": "complete", "records": [tracked]},
        )
        validate_snapshot(snapshot)
        self.assertEqual([], snapshot["openIssues"])
        self.assertEqual("closed", snapshot["evidence"]["issue:42"]["payload"]["state"])
        self.assertIn("run:1:attempt:1:job:100", snapshot["evidence"])
        prepared = prepare_assessment(snapshot)
        self.assertEqual([], prepared["issues"])
        followup = prepared["closedIssueFollowups"][0]["repairFollowup"]
        self.assertEqual("verified", followup["status"])
        self.assertEqual(3, followup["verification"]["runId"])

    def test_target_fork_is_supported_but_a_different_head_repository_is_not(self) -> None:
        runs = [run(3)]
        responses = responses_for(runs)
        own_fork = {**REPOSITORY_INFO, "fork": True}
        responses[f"/repos/{REPOSITORY}"] = own_fork
        runs[0].update(repository=own_fork, head_repository=own_fork)
        result = enrich_workflow_discovery(empty_inventory(), DiscoveryClient(responses), REPOSITORY, NOW)
        self.assertEqual([3], [row["runId"] for row in result.workflow_discovery["runs"]])
        validate_snapshot(collect_script.build_snapshot(REPOSITORY, NOW, result))
        runs[0]["head_repository"] = {**own_fork, "id": 99, "full_name": "foreign/repo"}
        result = enrich_workflow_discovery(empty_inventory(), DiscoveryClient(responses), REPOSITORY, NOW)
        self.assertEqual([], result.workflow_discovery["runs"])

    def test_default_branch_automation_events_are_observed_without_a_tracker(self) -> None:
        for event in ("workflow_run", "issues", "issue_comment", "repository_dispatch"):
            with self.subTest(event=event):
                result = enrich_workflow_discovery(
                    empty_inventory(), DiscoveryClient(responses_for([run(3, event=event)])),
                    REPOSITORY, NOW,
                )
                self.assertEqual([3], [row["runId"] for row in result.workflow_discovery["runs"]])
                validate_snapshot(collect_script.build_snapshot(REPOSITORY, NOW, result))

    def test_discovers_default_branch_job_failure_without_an_issue_tracker(self) -> None:
        client = DiscoveryClient(responses_for([run(3), run(2, conclusion="success"), run(1)]))
        result = enrich_workflow_discovery(empty_inventory(), client, REPOSITORY, NOW)
        discovery = result.workflow_discovery
        self.assertEqual("trunk", discovery["defaultBranch"])
        self.assertTrue(discovery["defaultBranchVerified"])
        self.assertEqual([3, 2, 1], [item["runId"] for item in discovery["runs"]])
        self.assertEqual("failure", discovery["runs"][0]["jobs"][0]["conclusion"])
        self.assertEqual([], discovery["issueAssociations"])
        self.assertEqual([], result.open_issues)
        self.assertEqual([], result.collection_errors)
        self.assertTrue(discovery["workflows"][0]["windowComplete"])
        self.assertTrue(all(limit <= 512_000 for limit in client.byte_limits))
        self.assertTrue(all("exclude_pull_requests" not in endpoint for _, endpoint in client.calls))

    def test_uncertain_branch_does_not_leave_a_complete_comparable_window(self) -> None:
        uncertain = run(2)
        uncertain["head_branch"] = None
        client = DiscoveryClient(responses_for([run(3), uncertain, run(1)]))
        discovery = enrich_workflow_discovery(empty_inventory(), client, REPOSITORY, NOW).workflow_discovery
        self.assertEqual([3, 1], discovery["workflows"][0]["runIds"])
        self.assertFalse(discovery["workflows"][0]["windowComplete"])
        self.assertTrue(any(gap["code"] == "unverified-run-scope" for gap in discovery["workflows"][0]["gaps"]))

    def test_date_filtered_history_retains_the_actual_recent_failure(self) -> None:
        responses = responses_for([run(5), run(4, conclusion="success"), run(1)])
        responses[f"/repos/{REPOSITORY}/actions/runs"] = {"total_count": 1, "workflow_runs": [run(5)]}
        responses[f"/repos/{REPOSITORY}/actions/workflows/10/runs"] = lambda query: (
            {"total_count": 2, "workflow_runs": [run(5), run(4, conclusion="success")]}
            if query.get("created", [""])[0].startswith("2026-09-01")
            else {"total_count": 0, "workflow_runs": []} if "created" in query
            else {"total_count": 1, "workflow_runs": [run(1)]}
        )
        discovery = enrich_workflow_discovery(
            empty_inventory(), DiscoveryClient(responses), REPOSITORY, NOW,
        ).workflow_discovery
        self.assertEqual([5, 4], discovery["workflows"][0]["runIds"])

    def test_five_realistically_sized_ci_runs_fit_the_job_budget(self) -> None:
        runs = [run(number) for number in range(5, 0, -1)]
        responses = responses_for(runs)
        for item in runs:
            # Fixed observed CI size, deliberately independent of implementation limits.
            jobs = [
                {**job(item["id"], number=item["id"] * 10_000 + index, conclusion="success"), "name": f"Job {index}"}
                for index in range(1, 313)
            ]
            responses[f"/repos/{REPOSITORY}/actions/runs/{item['id']}/attempts/1/jobs"] = (
                lambda query, jobs=jobs: {
                    "total_count": len(jobs),
                    "jobs": jobs[(int(query["page"][0]) - 1) * 50:int(query["page"][0]) * 50],
                }
            )
        result = enrich_workflow_discovery(empty_inventory(), DiscoveryClient(responses), REPOSITORY, NOW)
        self.assertTrue(result.workflow_discovery["workflows"][0]["windowComplete"])
        self.assertEqual([312] * 5, [len(item["jobs"]) for item in result.workflow_discovery["runs"]])

    def test_canonical_collect_writes_discovery_when_the_bot_created_no_issues(self) -> None:
        client = DiscoveryClient(
            responses_for([run(3)]),
            pages={
                "/repos/owner/repo/issues?state=open&labels=ci-failure-cause&per_page=100": [],
                "/repos/owner/repo/issues?state=open&labels=automation-broken&per_page=100": [],
                "/repos/owner/repo/issues?state=open&creator=github-actions%5Bbot%5D&per_page=100": [],
            },
        )
        with (
            TemporaryDirectory() as temporary,
            patch.object(collect_script, "GitHubClient", return_value=client),
            patch.object(collect_script, "datetime") as clock,
        ):
            clock.now.return_value = NOW
            output = collect_script.collect(
                REPOSITORY, Path(temporary) / "collected", None,
                repository_policy_path=Path(__file__).parent / "fixtures" / "repository-policy-widget-v1.json",
            )
            snapshot = json.loads((output / "input.json").read_text())
        self.assertEqual([], snapshot["openIssues"])
        self.assertEqual("trunk", snapshot["workflowDiscovery"]["defaultBranch"])
        self.assertEqual("failure", snapshot["workflowDiscovery"]["runs"][0]["jobs"][0]["conclusion"])

    def test_job_from_a_different_run_is_not_a_recovery_observation(self) -> None:
        responses = responses_for([run(3, conclusion="success")])
        responses[f"/repos/{REPOSITORY}/actions/runs/3/attempts/1/jobs"] = {
            "total_count": 1, "jobs": [{**job(3, conclusion="success"), "run_id": 999}],
        }
        discovery = enrich_workflow_discovery(
            empty_inventory(), DiscoveryClient(responses), REPOSITORY, NOW,
        ).workflow_discovery
        self.assertEqual([], discovery["runs"][0]["jobs"])
        self.assertFalse(discovery["runs"][0]["jobsComplete"])
        self.assertFalse(discovery["workflows"][0]["windowComplete"])

    def test_global_job_budget_preserves_partial_observations_without_issue_errors(self) -> None:
        client = DiscoveryClient(responses_for([run(3), run(2), run(1)]))
        with patch.dict(BOUNDS, jobs=2):
            result = enrich_workflow_discovery(empty_inventory(), client, REPOSITORY, NOW)
        discovery = result.workflow_discovery
        self.assertEqual(2, sum(len(item["jobs"]) for item in discovery["runs"]))
        self.assertEqual(2, discovery["usage"]["jobs"])
        self.assertFalse(discovery["workflows"][0]["windowComplete"])
        self.assertTrue(any(gap["code"] == "job-budget" for gap in discovery["gaps"]))
        self.assertEqual([], result.collection_errors)

    def test_current_failure_stays_visible_when_history_read_fails(self) -> None:
        responses = responses_for([run(3)])
        endpoint = f"/repos/{REPOSITORY}/actions/workflows/10/runs"
        responses[endpoint] = api_error(endpoint, 503)
        discovery = enrich_workflow_discovery(
            empty_inventory(), DiscoveryClient(responses), REPOSITORY, NOW,
        ).workflow_discovery
        self.assertEqual([3], [item["runId"] for item in discovery["runs"]])
        self.assertEqual("failure", discovery["runs"][0]["jobs"][0]["conclusion"])
        self.assertFalse(discovery["workflows"][0]["windowComplete"])

    def test_exact_source_run_and_job_link_matching_workflow_lane_to_issue(self) -> None:
        responses = responses_for([run(3), run(2, conclusion="success"), run(1)])
        responses[f"/repos/{REPOSITORY}/actions/runs"] = {"total_count": 1, "workflow_runs": [run(3)]}
        result = enrich_workflow_discovery(
            issue_inventory(), DiscoveryClient(responses), REPOSITORY, NOW,
        )
        association, = result.workflow_discovery["issueAssociations"]
        self.assertEqual(
            (42, 1, 1, 100),
            tuple(association[key] for key in ("issueNumber", "sourceRunId", "sourceAttempt", "sourceJobId")),
        )
        self.assertEqual(result.workflow_discovery["runs"][0]["jobs"][0]["laneId"], association["laneId"])
        self.assertIn("run:3:attempt:1:job:300", association["evidenceIds"])
        self.assertEqual(
            "failure", result.evidence["run:3:attempt:1:job:300"]["payload"]["conclusion"],
        )

    def test_discovered_failure_reuses_normalized_log_evidence_and_fact_extraction(self) -> None:
        responses = responses_for([run(3), run(1)])
        text = "Exception type: System.Net.Http.HttpRequestException\nConnection timed out\n"
        responses[f"/repos/{REPOSITORY}/actions/jobs/300/logs"] = text
        result = enrich_workflow_discovery(issue_inventory(), DiscoveryClient(responses), REPOSITORY, NOW)
        log = result.evidence["run:3:attempt:1:job:300:log"]["payload"]
        self.assertEqual(text, log["excerpt"])
        self.assertTrue(any(fact["field"] == "exceptionType" for fact in log["facts"]))
        self.assertEqual([42], [reference["sourceIssueNumber"] for reference in log["referencedBy"]])

    def test_duplicate_job_lanes_cannot_supply_recovery_evidence(self) -> None:
        responses = responses_for([run(3, conclusion="success"), run(1)])
        responses[f"/repos/{REPOSITORY}/actions/runs/3/attempts/1/jobs"] = {
            "total_count": 2, "jobs": [job(3, conclusion="success"), job(3, number=301, conclusion="success")],
        }
        result = enrich_workflow_discovery(issue_inventory(), DiscoveryClient(responses), REPOSITORY, NOW)
        self.assertFalse(result.workflow_discovery["workflows"][0]["windowComplete"])
        association, = result.workflow_discovery["issueAssociations"]
        self.assertEqual(
            ["run:1", "run:1:attempt:1:job:100", "run:1:attempt:1:job:100:log"],
            association["evidenceIds"],
        )

    def test_snapshot_validation_rejects_cross_branch_discovery(self) -> None:
        result = enrich_workflow_discovery(
            empty_inventory(), DiscoveryClient(responses_for([run(3)])), REPOSITORY, NOW,
        )
        snapshot = collect_script.build_snapshot(REPOSITORY, NOW, result)
        snapshot["workflowDiscovery"]["defaultBranch"] = "another-branch"
        with self.assertRaises(ValidationError):
            validate_snapshot(snapshot)

    def test_incremental_refresh_retires_old_discovery_owned_observations(self) -> None:
        result = enrich_workflow_discovery(
            issue_inventory(), DiscoveryClient(responses_for([run(3), run(1)])), REPOSITORY, NOW,
        )
        snapshot = collect_script.build_snapshot(REPOSITORY, NOW, result)
        plan = plan_refresh(REPOSITORY, [make_issue(42)], snapshot, current_history(snapshot))
        self.assertTrue({"run:3", "run:3:attempt:1:job:300"}.issubset(plan.retire))

    def test_collect_cli_supports_explicit_legacy_discovery_opt_out(self) -> None:
        with (
            patch("sys.argv", ["collect.py", "--repository", REPOSITORY, "--output-dir", "unused", "--skip-workflow-discovery"]),
            patch.object(collect_script, "collect", return_value=Path("unused")) as collect,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(0, collect_script.main())
        self.assertFalse(collect.call_args.kwargs["include_workflow_discovery"])

    def test_window_cap_does_not_hide_already_observed_untracked_failures(self) -> None:
        runs = [
            {**run(number), "workflow_id": number + 9, "path": f".github/workflows/job-{number}.yml"}
            for number in range(9, 0, -1)
        ]
        responses = responses_for(runs)
        for item in runs:
            responses[f"/repos/{REPOSITORY}/actions/workflows/{item['workflow_id']}/runs"] = (
                lambda query, item=item: {
                    "total_count": 1 if query["created"][0].startswith("2026-09-01") else 0,
                    "workflow_runs": [item] if query["created"][0].startswith("2026-09-01") else [],
                }
            )
        result = enrich_workflow_discovery(empty_inventory(), DiscoveryClient(responses), REPOSITORY, NOW)
        self.assertEqual(set(range(1, 10)), {item["runId"] for item in result.workflow_discovery["runs"]})
        self.assertEqual(8, len(result.workflow_discovery["workflows"]))
        self.assertFalse(result.workflow_discovery["runs"][-1]["jobsComplete"])
        validate_snapshot(collect_script.build_snapshot(REPOSITORY, NOW, result))

    def test_canonical_incremental_collection_observes_new_runs_without_carrying_old_discovery(self) -> None:
        pages = {
            "/repos/owner/repo/issues?state=open&labels=ci-failure-cause&per_page=100": [],
            "/repos/owner/repo/issues?state=open&labels=automation-broken&per_page=100": [],
            "/repos/owner/repo/issues?state=open&creator=github-actions%5Bbot%5D&per_page=100": [],
        }
        clients = [
            DiscoveryClient(responses_for([run(3)]), pages=pages),
            DiscoveryClient(responses_for([run(5, conclusion="success")]), pages=pages),
        ]
        with (
            TemporaryDirectory() as temporary,
            patch.object(collect_script, "GitHubClient", side_effect=clients),
            patch.object(collect_script, "datetime") as clock,
        ):
            clock.now.return_value = NOW
            root = Path(temporary)
            policy = Path(__file__).parent / "fixtures" / "repository-policy-widget-v1.json"
            first = collect_script.collect(REPOSITORY, root / "first", None, repository_policy_path=policy)
            snapshot = json.loads((first / "input.json").read_text())
            record_history(root / "state", REPOSITORY, "first", snapshot, {
                "schemaVersion": 1, "repository": REPOSITORY, "decisions": [],
            })
            second = collect_script.collect(
                REPOSITORY, root / "second", None, repository_policy_path=policy, state_dir=root / "state",
            )
            refreshed = json.loads((second / "input.json").read_text())
        self.assertEqual([5], [item["runId"] for item in refreshed["workflowDiscovery"]["runs"]])
        self.assertEqual([], [
            key for key, record in refreshed["evidence"].items() if record.get("discoveredBy") == "workflow-discovery"
        ])

    def test_pr_related_events_and_fork_or_uncertain_heads_are_never_admitted(self) -> None:
        variants = [
            {"event": event} for event in (
                "pull_request", "pull_request_target", "merge_group",
            )
        ] + [
            {"pull_requests": [{"number": 12}]}, {"pull_requests": None},
            {"head_branch": None}, {"head_branch": "feature"},
            {"head_repository": {**REPOSITORY_INFO, "fork": True}},
            {"head_repository": {**REPOSITORY_INFO, "full_name": "fork/repo", "id": 8}},
            {"head_repository": None},
        ]
        for changes in variants:
            with self.subTest(changes=changes):
                result = enrich_workflow_discovery(
                    empty_inventory(), DiscoveryClient(responses_for([{**run(3), **changes}])), REPOSITORY, NOW,
                )
                self.assertEqual([], result.workflow_discovery["runs"])
                self.assertEqual([], result.workflow_discovery["issueAssociations"])
                self.assertEqual(1, len(result.workflow_discovery["excludedRuns"]))
                validate_snapshot(collect_script.build_snapshot(REPOSITORY, NOW, result))

    def test_successful_quarantine_job_is_not_test_recovery_evidence(self) -> None:
        result = enrich_workflow_discovery(
            issue_inventory(labels=["quarantined-test"]),
            DiscoveryClient(responses_for([run(3, conclusion="success"), run(1)])), REPOSITORY, NOW,
        )
        association, = result.workflow_discovery["issueAssociations"]
        self.assertTrue(association["testTracker"])
        self.assertEqual([], association["evidenceIds"])
        self.assertEqual([], [record for record in result.evidence.values() if record["kind"] == "workflow-job"])
        validate_snapshot(collect_script.build_snapshot(REPOSITORY, NOW, result))

    def test_lost_workflow_coverage_forces_reassessment_of_previously_associated_issue(self) -> None:
        prior = enrich_workflow_discovery(
            issue_inventory(), DiscoveryClient(responses_for([run(3), run(1)])), REPOSITORY, NOW,
        ).workflow_discovery
        client = DiscoveryClient({f"/repos/{REPOSITORY}": api_error(f"/repos/{REPOSITORY}", 503)})
        inventory = replace(issue_inventory(), refresh_plan=RefreshPlan(reuse=("issue:42",)))
        refreshed = enrich_workflow_discovery(
            inventory, client, REPOSITORY, NOW, previous_discovery=prior,
        )
        self.assertEqual((42,), refreshed.refresh_plan.changed_issues)
        self.assertEqual("unavailable", refreshed.workflow_discovery["status"])

    def test_unverified_issue_anchor_does_not_invalidate_an_independently_complete_window(self) -> None:
        responses = responses_for([run(3), run(1)])
        responses[f"/repos/{REPOSITORY}/actions/runs/1"] = {**run(1), "head_branch": None}
        result = enrich_workflow_discovery(issue_inventory(), DiscoveryClient(responses), REPOSITORY, NOW)
        self.assertTrue(result.workflow_discovery["workflows"][0]["windowComplete"])
        self.assertEqual([], result.workflow_discovery["issueAssociations"])
        self.assertTrue(any(gap["scope"] == "issue" for gap in result.workflow_discovery["gaps"]))

    def test_missing_fork_provenance_breaks_consecutive_run_coverage(self) -> None:
        for head in ({}, {**REPOSITORY_INFO, "fork": None}):
            with self.subTest(head=head):
                result = enrich_workflow_discovery(
                    empty_inventory(),
                    DiscoveryClient(responses_for([run(3), {**run(2), "head_repository": head}, run(1)])),
                    REPOSITORY, NOW,
                )
                self.assertEqual([3, 1], result.workflow_discovery["workflows"][0]["runIds"])
                self.assertFalse(result.workflow_discovery["workflows"][0]["windowComplete"])

    def test_success_without_execution_timestamps_does_not_supply_recovery(self) -> None:
        responses = responses_for([run(3, conclusion="success"), run(1)])
        responses[f"/repos/{REPOSITORY}/actions/runs/3/attempts/1/jobs"] = {
            "total_count": 1, "jobs": [{**job(3, conclusion="success"), "completed_at": None}],
        }
        result = enrich_workflow_discovery(issue_inventory(), DiscoveryClient(responses), REPOSITORY, NOW)
        self.assertFalse(result.workflow_discovery["workflows"][0]["windowComplete"])
        self.assertEqual(
            ["run:1", "run:1:attempt:1:job:100", "run:1:attempt:1:job:100:log"],
            result.workflow_discovery["issueAssociations"][0]["evidenceIds"],
        )

    def test_listing_cannot_smuggle_an_old_failure_into_the_recent_window(self) -> None:
        old = {**run(1), "created_at": "2026-01-01T12:00:00Z"}
        result = enrich_workflow_discovery(
            empty_inventory(), DiscoveryClient(responses_for([old])), REPOSITORY, NOW,
        )
        self.assertEqual([], result.workflow_discovery["runs"])
        self.assertTrue(result.workflow_discovery["gaps"])

    def test_malformed_run_identity_is_a_scoped_gap_not_an_admitted_run(self) -> None:
        for changes in ({"id": True}, {"event": {}}, {"head_sha": "not-a-commit"}, {"status": {}}):
            with self.subTest(changes=changes):
                responses = responses_for([run(3)])
                responses[f"/repos/{REPOSITORY}/actions/runs"] = {
                    "total_count": 1, "workflow_runs": [{**run(3), **changes}],
                }
                result = enrich_workflow_discovery(empty_inventory(), DiscoveryClient(responses), REPOSITORY, NOW)
                self.assertEqual([], result.workflow_discovery["runs"])
                self.assertTrue(result.workflow_discovery["gaps"])
                validate_snapshot(collect_script.build_snapshot(REPOSITORY, NOW, result))

    def test_malformed_job_is_a_scoped_gap_without_breaking_collection(self) -> None:
        for changes in (
            {"name": None}, {"steps": None}, {"status": {}}, {"run_attempt": True},
            {"html_url": "https://github.com/foreign/repo/actions/runs/3/job/300"},
        ):
            with self.subTest(changes=changes):
                responses = responses_for([run(3)])
                responses[f"/repos/{REPOSITORY}/actions/runs/3/attempts/1/jobs"] = {
                    "total_count": 1, "jobs": [{**job(3), **changes}],
                }
                result = enrich_workflow_discovery(empty_inventory(), DiscoveryClient(responses), REPOSITORY, NOW)
                self.assertFalse(result.workflow_discovery["workflows"][0]["windowComplete"])
                self.assertEqual([], result.workflow_discovery["runs"][0]["jobs"])
                validate_snapshot(collect_script.build_snapshot(REPOSITORY, NOW, result))

    def test_association_evidence_cannot_disagree_with_observed_job_outcome(self) -> None:
        result = enrich_workflow_discovery(
            issue_inventory(), DiscoveryClient(responses_for([run(3), run(1)])), REPOSITORY, NOW,
        )
        snapshot = collect_script.build_snapshot(REPOSITORY, NOW, result)
        snapshot["evidence"]["run:3:attempt:1:job:300"]["payload"]["conclusion"] = "success"
        with self.assertRaises(ValidationError):
            validate_snapshot(snapshot)

    def test_explicit_discovery_opt_out_invalidates_prior_workflow_judgments(self) -> None:
        previous = enrich_workflow_discovery(
            issue_inventory(), DiscoveryClient(responses_for([run(3), run(1)])), REPOSITORY, NOW,
        )
        snapshot = collect_script.build_snapshot(REPOSITORY, NOW, previous)
        fresh = replace(issue_inventory(), refresh_plan=RefreshPlan(reuse=("issue:42",)))
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "snapshot.json").write_text(json.dumps(snapshot))
            current = SimpleNamespace(run_directory=root, document=current_history(snapshot))
            with (
                patch.object(collect_script, "load_current", return_value=current),
                patch.object(collect_script, "GitHubClient", return_value=object()),
                patch.object(Collector, "collect_incremental", return_value=fresh),
                patch.object(Collector, "enrich_github_evidence", side_effect=lambda inventory, **_: inventory),
                patch.object(Collector, "enrich_ownership_evidence", side_effect=lambda inventory, **_: inventory),
                patch.object(collect_script, "datetime") as clock,
            ):
                clock.now.return_value = NOW
                output = collect_script.collect(
                    REPOSITORY, root / "output", None, state_dir=root / "state",
                    include_workflow_discovery=False,
                    repository_policy_path=Path(__file__).parent / "fixtures" / "repository-policy-widget-v1.json",
                )
            refreshed = json.loads((output / "input.json").read_text())
        self.assertEqual([42], refreshed["refreshSummary"]["changedIssueNumbers"])
        self.assertNotIn("workflowDiscovery", refreshed)

    def test_issue_source_just_outside_last_five_runs_still_gets_its_job_verified(self) -> None:
        result = enrich_workflow_discovery(
            issue_inventory(), DiscoveryClient(responses_for([run(number) for number in range(6, 0, -1)])),
            REPOSITORY, NOW,
        )
        self.assertTrue(result.workflow_discovery["workflows"][0]["windowComplete"])
        association, = result.workflow_discovery["issueAssociations"]
        self.assertEqual(1, association["sourceRunId"])
        self.assertIn("run:6:attempt:1:job:600", association["evidenceIds"])

    def test_retry_attempts_count_as_one_independent_run_with_the_latest_attempt(self) -> None:
        latest = {**run(3, conclusion="success"), "run_attempt": 2}
        result = enrich_workflow_discovery(
            empty_inventory(), DiscoveryClient(responses_for([latest, run(3), run(2)])), REPOSITORY, NOW,
        )
        discovery = result.workflow_discovery
        self.assertEqual([3, 2], discovery["workflows"][0]["runIds"])
        self.assertTrue(discovery["workflows"][0]["windowComplete"])
        self.assertEqual(2, discovery["runs"][0]["attempt"])
        self.assertEqual(2, discovery["runs"][0]["jobs"][0]["attempt"])
        validate_snapshot(collect_script.build_snapshot(REPOSITORY, NOW, result))

    def test_incomplete_job_page_retains_jobs_and_diagnostics_without_claiming_coverage(self) -> None:
        responses = responses_for([run(3)])
        path = f"/repos/{REPOSITORY}/actions/runs/3/attempts/1/jobs"
        responses[path] = {"total_count": 51, "jobs": [job(3)]}
        responses[(path, 2)] = api_error(path, 503)
        result = enrich_workflow_discovery(empty_inventory(), DiscoveryClient(responses), REPOSITORY, NOW)
        observed, = result.workflow_discovery["runs"]
        self.assertEqual([300], [item["jobId"] for item in observed["jobs"]])
        self.assertFalse(observed["jobsComplete"])
        self.assertTrue(any(gap["code"] == "read-failed" for gap in observed["gaps"]))
        self.assertEqual([], result.collection_errors)
        validate_snapshot(collect_script.build_snapshot(REPOSITORY, NOW, result))

    def test_request_limit_is_hard_and_reports_incomplete_windows(self) -> None:
        client = DiscoveryClient(responses_for([run(3)]))
        with patch.dict(BOUNDS, requests=2):
            result = enrich_workflow_discovery(empty_inventory(), client, REPOSITORY, NOW)
        self.assertEqual(2, result.workflow_discovery["usage"]["requests"])
        self.assertEqual(2, len(client.calls))
        self.assertFalse(result.workflow_discovery["workflows"][0]["windowComplete"])
        validate_snapshot(collect_script.build_snapshot(REPOSITORY, NOW, result))

    def test_total_byte_limit_bounds_the_next_request_and_reports_truncation(self) -> None:
        client = DiscoveryClient(responses_for([run(3)]))
        limit = len(json.dumps(REPOSITORY_INFO).encode("utf-8")) + 10
        with patch.dict(BOUNDS, totalResponseBodyBytes=limit):
            result = enrich_workflow_discovery(empty_inventory(), client, REPOSITORY, NOW)
        self.assertEqual(limit, result.workflow_discovery["usage"]["responseBodyBytes"])
        self.assertEqual(10, client.byte_limits[-1])
        self.assertEqual([], result.workflow_discovery["runs"])
        self.assertTrue(any(gap["code"] == "response-truncated" for gap in result.workflow_discovery["gaps"]))
        validate_snapshot(collect_script.build_snapshot(REPOSITORY, NOW, result))

    def test_matching_job_display_names_do_not_cross_workflow_identity(self) -> None:
        source, current = run(1), {**run(3), "workflow_id": 11}
        responses = responses_for([current, source])
        history_path = f"/repos/{REPOSITORY}/actions/workflows/10/runs"
        responses[history_path] = responses_for([source])[history_path]
        responses[f"/repos/{REPOSITORY}/actions/workflows/11/runs"] = responses_for([current])[history_path]
        result = enrich_workflow_discovery(issue_inventory(), DiscoveryClient(responses), REPOSITORY, NOW)
        self.assertEqual(
            ["run:1:attempt:1:job:100"],
            [key for key, record in result.evidence.items() if record["kind"] == "workflow-job"],
        )
        validate_snapshot(collect_script.build_snapshot(REPOSITORY, NOW, result))

    def test_workflow_success_does_not_replace_skipped_or_neutral_job_conclusion(self) -> None:
        for conclusion in ("skipped", "neutral"):
            with self.subTest(conclusion=conclusion):
                responses = responses_for([run(3, conclusion="success"), run(1)])
                responses[f"/repos/{REPOSITORY}/actions/runs/3/attempts/1/jobs"] = {
                    "total_count": 1, "jobs": [job(3, conclusion=conclusion)],
                }
                result = enrich_workflow_discovery(issue_inventory(), DiscoveryClient(responses), REPOSITORY, NOW)
                self.assertEqual("success", result.evidence["run:3"]["payload"]["conclusion"])
                self.assertEqual(conclusion, result.evidence["run:3:attempt:1:job:300"]["payload"]["conclusion"])
                validate_snapshot(collect_script.build_snapshot(REPOSITORY, NOW, result))

    def test_historical_source_attempt_does_not_downgrade_known_later_attempt(self) -> None:
        source = {**run(1), "created_at": "2026-01-01T12:00:00Z"}
        responses = responses_for([source])
        responses[f"/repos/{REPOSITORY}/actions/runs"] = {"total_count": 0, "workflow_runs": []}
        previous = enrich_workflow_discovery(issue_inventory(), DiscoveryClient(responses), REPOSITORY, NOW)
        previous.evidence["run:1"]["payload"].update({"attempt": 2, "conclusion": "success"})
        responses[f"/repos/{REPOSITORY}/actions/runs/1/attempts/1"] = source
        refreshed = enrich_workflow_discovery(previous, DiscoveryClient(responses), REPOSITORY, NOW)
        self.assertEqual(2, refreshed.evidence["run:1"]["payload"]["attempt"])
        self.assertEqual("success", refreshed.evidence["run:1"]["payload"]["conclusion"])
        self.assertEqual(1, refreshed.workflow_discovery["sourceRuns"][0]["attempt"])

    def test_unordered_history_is_not_certified_as_the_latest_comparable_window(self) -> None:
        result = enrich_workflow_discovery(
            empty_inventory(), DiscoveryClient(responses_for([run(5), run(1), run(4), run(3), run(2)])),
            REPOSITORY, NOW,
        )
        self.assertFalse(result.workflow_discovery["workflows"][0]["windowComplete"])
        self.assertTrue(any(gap["code"] == "history-order-unverified" for gap in result.workflow_discovery["gaps"]))

    def test_missing_history_identity_keeps_the_requested_workflow_gap_scope(self) -> None:
        for changes in ({"workflow_id": None}, {"event": None}):
            with self.subTest(changes=changes):
                result = enrich_workflow_discovery(
                    empty_inventory(), DiscoveryClient(responses_for([run(3), {**run(2), **changes}, run(1)])),
                    REPOSITORY, NOW,
                )
                self.assertFalse(result.workflow_discovery["workflows"][0]["windowComplete"])
                self.assertTrue(any(
                    gap.get("workflowId") == 10 and gap.get("event") == "push"
                    for gap in result.workflow_discovery["workflows"][0]["gaps"]
                ))
