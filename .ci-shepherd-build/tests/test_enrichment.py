from __future__ import annotations

import inspect
import copy
import json
import unittest
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from ci_shepherd.collector import Collector, InventoryResult
from ci_shepherd.models import validate_snapshot


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"
REPOSITORY = "owner/repo"
NOW = datetime(2026, 8, 17, 22, 0, tzinfo=UTC)
CUTOFF = "2026-05-19T22:00:00Z"


class FakeApiError(RuntimeError):
    def __init__(self, category: str, status: int = 0, message: str = "boom") -> None:
        super().__init__(message)
        self.category = category
        self.status = status


class EnrichmentClient:
    def __init__(
        self,
        *,
        pages: dict[str, object] | None = None,
        singles: dict[str, object] | None = None,
        texts: dict[str, object] | None = None,
    ) -> None:
        self._pages = dict(pages or {})
        self._singles = dict(singles or {})
        self._texts = dict(texts or {})
        self.calls: list[tuple[str, str]] = []
        self.text_calls: list[tuple[str, str, int]] = []

    def get_pages(self, endpoint: str, key: str | None = None) -> object:
        self.calls.append(("get_pages", endpoint))
        response = self._pages[endpoint]
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)

    def get(self, endpoint: str) -> object:
        self.calls.append(("get", endpoint))
        if endpoint not in self._singles and "state=open&sort=updated" in endpoint:
            # The open bot-author scan pages an endpoint most fixtures do not
            # care about; an empty page means "no bot-authored items here".
            return []
        response = self._singles[endpoint]
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)

    def get_text(self, endpoint: str, max_bytes: int = 200000) -> object:
        self.text_calls.append(("get_text", endpoint, max_bytes))
        response = self._texts[endpoint]
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)


class FakeTextResponse:
    def __init__(self, text: str, *, truncated: bool, status: int = 200) -> None:
        self.text = text
        self.truncated = truncated
        self.status = status
        self.headers: dict[str, str] = {}


def load_fixture(name: str) -> object:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def make_issue(
    number: int,
    *,
    state: str = "open",
    title: str | None = None,
    body: str = "",
    labels: list[str] | None = None,
    created_at: str = "2026-08-01T00:00:00Z",
    updated_at: str = "2026-08-02T00:00:00Z",
    closed_at: str | None = None,
    is_pull_request: bool = False,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "number": number,
        "state": state,
        "title": title or f"Issue {number}",
        "body": body,
        "html_url": f"https://github.com/{REPOSITORY}/issues/{number}",
        "created_at": created_at,
        "updated_at": updated_at,
        "closed_at": closed_at,
        "labels": [{"name": name} for name in (labels or [])],
        "user": {"login": "octocat"},
    }
    if is_pull_request:
        payload["pull_request"] = {"url": f"https://api.github.com/repos/{REPOSITORY}/pulls/{number}"}
    return payload


def snapshot_from_result(result) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "repository": REPOSITORY,
        "collectedAt": NOW.isoformat().replace("+00:00", "Z"),
        "openIssues": [issue["number"] for issue in result.open_issues],
        "evidence": result.evidence,
        "collectionErrors": [asdict(error) for error in result.collection_errors],
    }


class GitHubEnrichmentTests(unittest.TestCase):
    def build_inventory(self, client: EnrichmentClient, *, body: str) -> object:
        pages = dict(client._pages)
        pages.update(
            {
                f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                    make_issue(11, labels=["ci-failure-cause"], body=body)
                ],
                f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
                f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [],
                f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
                f"/repos/{REPOSITORY}/issues/11/comments": [],
                f"/repos/{REPOSITORY}/issues/11/timeline": [],
            }
        )
        inventory_client = EnrichmentClient(pages=pages, singles=client._singles, texts=client._texts)
        collector = Collector(inventory_client, REPOSITORY, NOW)
        return collector.collect()

    def make_run(self, run_id: int = 7001) -> dict[str, object]:
        return {
            "id": run_id,
            "workflow_id": 0,
            "run_attempt": 1,
            "name": "CI",
            "event": "pull_request",
            "head_branch": "feature",
            "head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "status": "completed",
            "conclusion": "failure",
            "created_at": "2026-08-17T20:00:00Z",
            "updated_at": "2026-08-17T20:30:00Z",
            "run_started_at": "2026-08-17T20:01:00Z",
            "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
        }

    def make_job(self, job_id: int, *, conclusion: str = "failure", check_run_id: int = 0) -> dict[str, object]:
        job = {
            "id": job_id,
            "run_id": 7001,
            "run_attempt": 1,
            "name": f"job-{job_id}",
            "status": "completed",
            "conclusion": conclusion,
            "started_at": "2026-08-17T20:01:00Z",
            "completed_at": "2026-08-17T20:20:00Z",
            "html_url": f"https://github.com/{REPOSITORY}/actions/runs/7001/job/{job_id}",
            "steps": [],
        }
        if check_run_id:
            job["check_run_url"] = f"https://api.github.com/repos/{REPOSITORY}/check-runs/{check_run_id}"
        return job

    def make_history_run(self, run_id: int, created_at: str) -> dict[str, object]:
        return {
            "id": run_id,
            "run_attempt": 1,
            "event": "push",
            "head_branch": "main",
            "head_sha": f"{run_id:040x}"[-40:],
            "conclusion": "success",
            "created_at": created_at,
            "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
        }

    def collect_run_history(
        self,
        history: object,
        *,
        run: dict[str, object] | None = None,
    ) -> tuple[dict[str, object], EnrichmentClient, InventoryResult]:
        source_run = copy.deepcopy(run if run is not None else self.make_run())
        source_run["workflow_id"] = 9001
        source_run["head_branch"] = "main"
        history_endpoint = f"/repos/{REPOSITORY}/actions/workflows/9001/runs?branch=main&per_page=10"
        client = EnrichmentClient(
            pages={
                f"/repos/{REPOSITORY}/actions/runs/7001/jobs?per_page=100": {"jobs": []},
            },
            singles={
                f"/repos/{REPOSITORY}/actions/runs/7001": source_run,
                history_endpoint: history,
            },
        )
        inventory = self.build_inventory(
            client,
            body=f"See https://github.com/{REPOSITORY}/actions/runs/7001",
        )
        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(
            inventory,
            minimal_run_evidence=True,
            include_run_history=True,
        )
        return enriched.evidence["run:7001"]["payload"], client, enriched

    def test_enrich_github_evidence_can_skip_issue_reference_dereferencing(self) -> None:
        client = EnrichmentClient()
        inventory = InventoryResult(
            open_issues=[{"number": 11}],
            supporting_issues=[],
            evidence={},
            collection_errors=[],
            warnings=[],
            references={
                11: [
                    {
                        "sourceIssueNumber": 11,
                        "sourceEvidenceId": "issue:11",
                        "sourceUrl": f"https://github.com/{REPOSITORY}/issues/11",
                        "targetType": "issue",
                        "targetRepository": REPOSITORY,
                        "targetNumber": 88,
                        "targetUrl": f"https://github.com/{REPOSITORY}/issues/88",
                        "extractionMethod": "local-issue",
                    }
                ]
            },
        )

        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(
            inventory,
            include_issue_references=False,
        )

        self.assertEqual([], client.calls)
        self.assertNotIn("issue:88", enriched.evidence)

    def test_minimal_run_enrichment_fetches_current_jobs_and_failed_logs_only(self) -> None:
        client = EnrichmentClient(
            pages={
                f"/repos/{REPOSITORY}/actions/runs/7001/jobs?per_page=100": load_fixture("jobs-failed.json"),
            },
            singles={
                f"/repos/{REPOSITORY}/actions/runs/7001": load_fixture("run-failed.json"),
            },
            texts={
                f"/repos/{REPOSITORY}/actions/jobs/2001/logs": FakeTextResponse(
                    "failed",
                    truncated=False,
                ),
            },
        )
        inventory = self.build_inventory(
            client,
            body=f"See https://github.com/{REPOSITORY}/actions/runs/7001",
        )

        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(
            inventory,
            minimal_run_evidence=True,
        )

        run_payload = enriched.evidence["run:7001"]["payload"]
        self.assertEqual([2001], [job["jobId"] for job in run_payload["jobs"]])
        self.assertEqual([], run_payload["artifacts"])
        self.assertEqual([], run_payload["recentHistory"])
        self.assertEqual(
            [
                ("get", f"/repos/{REPOSITORY}/actions/runs/7001"),
                ("get_pages", f"/repos/{REPOSITORY}/actions/runs/7001/jobs?per_page=100"),
            ],
            sorted(client.calls),
        )
        self.assertEqual(
            [("get_text", f"/repos/{REPOSITORY}/actions/jobs/2001/logs", 200000)],
            client.text_calls,
        )

    def test_minimal_run_enrichment_can_collect_one_bounded_history_request(self) -> None:
        signature = inspect.signature(Collector.enrich_github_evidence)
        if "include_run_history" not in signature.parameters:
            self.fail("enrich_github_evidence must expose include_run_history")

        run = self.make_run()
        run["workflow_id"] = 9001
        run["head_branch"] = "main"
        history = {
            "total_count": 12,
            "workflow_runs": [
                {
                    "id": 7100 + offset,
                    "run_attempt": 1,
                    "event": "push",
                    "head_branch": "main",
                    "head_sha": f"{offset:040x}"[-40:],
                    "conclusion": "success",
                    "created_at": f"2026-08-{offset:02d}T00:00:00Z",
                    "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{7100 + offset}",
                }
                for offset in range(10, 0, -1)
            ],
        }
        history_endpoint = f"/repos/{REPOSITORY}/actions/workflows/9001/runs?branch=main&per_page=10"
        client = EnrichmentClient(
            pages={
                f"/repos/{REPOSITORY}/actions/runs/7001/jobs?per_page=100": {
                    "jobs": [self.make_job(2001)]
                },
            },
            singles={
                f"/repos/{REPOSITORY}/actions/runs/7001": run,
                history_endpoint: history,
            },
            texts={
                f"/repos/{REPOSITORY}/actions/jobs/2001/logs": FakeTextResponse(
                    "failed",
                    truncated=False,
                ),
            },
        )
        inventory = self.build_inventory(
            client,
            body=f"See https://github.com/{REPOSITORY}/actions/runs/7001",
        )

        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(
            inventory,
            minimal_run_evidence=True,
            include_run_history=True,
        )

        payload = enriched.evidence["run:7001"]["payload"]
        self.assertEqual([2001], [job["jobId"] for job in payload["jobs"]])
        self.assertEqual([], payload["artifacts"])
        self.assertTrue(payload["recentHistoryCollected"])
        self.assertTrue(payload["recentHistoryTruncated"])
        self.assertEqual(12, payload.get("recentHistoryTotalCount"))
        self.assertIs(payload.get("historyCoversSourceRun"), False)
        self.assertEqual(10, len(payload["recentHistory"]))
        self.assertEqual(1, client.calls.count(("get", history_endpoint)))
        self.assertNotIn(("get_pages", history_endpoint), client.calls)

    def test_history_response_missing_run_list_is_not_collected(self) -> None:
        payload, _, enriched = self.collect_run_history({"total_count": 0})

        self.assertFalse(payload["recentHistoryCollected"])
        self.assertEqual([], payload["recentHistory"])
        self.assertFalse(payload["recentHistoryTruncated"])
        self.assertIs(payload.get("historyCoversSourceRun"), False)
        self.assertEqual("unexpected-response", payload["recentHistoryGap"])
        self.assertEqual("workflow-history", enriched.collection_errors[-1].stage)

    def test_malformed_history_run_is_not_collected(self) -> None:
        payload, _, enriched = self.collect_run_history(
            {
                "total_count": 1,
                "workflow_runs": [
                    self.make_history_run(7100, "not-a-timestamp"),
                ],
            }
        )

        self.assertFalse(payload["recentHistoryCollected"])
        self.assertEqual([], payload["recentHistory"])
        self.assertFalse(payload["recentHistoryTruncated"])
        self.assertIs(payload.get("historyCoversSourceRun"), False)
        self.assertEqual("unexpected-response", payload["recentHistoryGap"])
        self.assertIn("malformed", enriched.collection_errors[-1].message.lower())

    def test_truncated_history_without_source_run_does_not_prove_coverage(self) -> None:
        run = self.make_run()
        run["created_at"] = "2026-08-01T00:00:00Z"
        history = {
            "total_count": 20,
            "workflow_runs": [
                self.make_history_run(7100 + day, f"2026-08-{day:02d}T00:00:00Z")
                for day in range(11, 1, -1)
            ],
        }

        payload, _, _ = self.collect_run_history(history, run=run)

        self.assertTrue(payload["recentHistoryCollected"])
        self.assertEqual(20, payload.get("recentHistoryTotalCount"))
        self.assertTrue(payload["recentHistoryTruncated"])
        self.assertIs(payload.get("historyCoversSourceRun"), False)

    def test_truncated_history_with_source_run_proves_all_newer_runs_are_covered(self) -> None:
        run = self.make_run()
        run["created_at"] = "2026-08-05T00:00:00Z"
        history_runs = [
            self.make_history_run(7100 + day, f"2026-08-{day:02d}T00:00:00Z")
            for day in range(10, 5, -1)
        ]
        history_runs.append(self.make_history_run(7001, "2026-08-05T00:00:00Z"))
        history_runs.extend(
            self.make_history_run(7100 + day, f"2026-08-{day:02d}T00:00:00Z")
            for day in range(4, 0, -1)
        )

        payload, _, _ = self.collect_run_history(
            {
                "total_count": 20,
                "workflow_runs": history_runs,
            },
            run=run,
        )

        self.assertTrue(payload["recentHistoryCollected"])
        self.assertTrue(payload["recentHistoryTruncated"])
        self.assertIs(payload.get("historyCoversSourceRun"), True)

    def test_full_short_history_proves_coverage_without_source_in_window(self) -> None:
        run = self.make_run()
        run["created_at"] = "2026-08-01T00:00:00Z"
        history = {
            "total_count": 3,
            "workflow_runs": [
                self.make_history_run(7100 + day, f"2026-08-{day:02d}T00:00:00Z")
                for day in range(4, 1, -1)
            ],
        }

        payload, _, _ = self.collect_run_history(history, run=run)

        self.assertTrue(payload["recentHistoryCollected"])
        self.assertEqual(3, payload.get("recentHistoryTotalCount"))
        self.assertFalse(payload["recentHistoryTruncated"])
        self.assertIs(payload.get("historyCoversSourceRun"), True)

    def test_missing_source_run_identity_or_timestamp_skips_history_collection(self) -> None:
        for missing_field in ("id", "created_at"):
            with self.subTest(missing_field=missing_field):
                run = self.make_run()
                run.pop(missing_field)

                payload, client, enriched = self.collect_run_history(
                    {
                        "total_count": 0,
                        "workflow_runs": [],
                    },
                    run=run,
                )

                self.assertFalse(payload["recentHistoryCollected"])
                self.assertIs(payload.get("historyCoversSourceRun"), False)
                self.assertEqual("source-run-identity-unavailable", payload["recentHistoryGap"])
                self.assertFalse(
                    any("/actions/workflows/" in endpoint for _, endpoint in client.calls)
                )
                self.assertEqual("workflow-history", enriched.collection_errors[-1].stage)

    def test_history_endpoint_failure_records_collection_gap(self) -> None:
        signature = inspect.signature(Collector.enrich_github_evidence)
        if "include_run_history" not in signature.parameters:
            self.fail("enrich_github_evidence must expose include_run_history")

        run = self.make_run()
        run["workflow_id"] = 9001
        run["head_branch"] = "main"
        history_endpoint = f"/repos/{REPOSITORY}/actions/workflows/9001/runs?branch=main&per_page=10"
        client = EnrichmentClient(
            pages={
                f"/repos/{REPOSITORY}/actions/runs/7001/jobs?per_page=100": {"jobs": []},
            },
            singles={
                f"/repos/{REPOSITORY}/actions/runs/7001": run,
                history_endpoint: FakeApiError("server", 500, "history unavailable"),
            },
        )
        inventory = self.build_inventory(
            client,
            body=f"See https://github.com/{REPOSITORY}/actions/runs/7001",
        )

        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(
            inventory,
            minimal_run_evidence=True,
            include_run_history=True,
        )

        payload = enriched.evidence["run:7001"]["payload"]
        self.assertFalse(payload["recentHistoryCollected"])
        self.assertFalse(payload["recentHistoryTruncated"])
        self.assertIs(payload.get("historyCoversSourceRun"), False)
        self.assertEqual([], payload["recentHistory"])
        self.assertEqual("request-failed", payload["recentHistoryGap"])
        self.assertEqual("workflow-history", enriched.collection_errors[-1].stage)
        self.assertEqual("recent workflow history not collected", enriched.collection_errors[-1].effect)

    def test_missing_workflow_identity_records_history_collection_gap(self) -> None:
        client = EnrichmentClient(
            pages={
                f"/repos/{REPOSITORY}/actions/runs/7001/jobs?per_page=100": {"jobs": []},
            },
            singles={
                f"/repos/{REPOSITORY}/actions/runs/7001": self.make_run(),
            },
        )
        inventory = self.build_inventory(
            client,
            body=f"See https://github.com/{REPOSITORY}/actions/runs/7001",
        )

        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(
            inventory,
            minimal_run_evidence=True,
            include_run_history=True,
        )

        payload = enriched.evidence["run:7001"]["payload"]
        self.assertFalse(payload["recentHistoryCollected"])
        self.assertFalse(payload["recentHistoryTruncated"])
        self.assertIs(payload.get("historyCoversSourceRun"), False)
        self.assertEqual([], payload["recentHistory"])
        self.assertEqual("workflow-or-branch-unavailable", payload["recentHistoryGap"])
        self.assertEqual("workflow-history", enriched.collection_errors[-1].stage)
        self.assertIn("workflow/branch identity", enriched.collection_errors[-1].message)

    def test_run_detail_failure_records_history_collection_gap(self) -> None:
        run_endpoint = f"/repos/{REPOSITORY}/actions/runs/7001"
        client = EnrichmentClient(
            singles={
                run_endpoint: FakeApiError("server", 500, "run unavailable"),
            },
        )
        inventory = self.build_inventory(
            client,
            body=f"See https://github.com/{REPOSITORY}/actions/runs/7001",
        )

        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(
            inventory,
            minimal_run_evidence=True,
            include_run_history=True,
        )

        record = enriched.evidence["run:7001"]
        self.assertEqual("partial", record["availability"])
        self.assertEqual([], record["payload"]["recentHistory"])
        self.assertFalse(record["payload"]["recentHistoryCollected"])
        self.assertFalse(record["payload"]["recentHistoryTruncated"])
        self.assertIs(record["payload"].get("historyCoversSourceRun"), False)
        self.assertEqual("run-detail-unavailable", record["payload"]["recentHistoryGap"])

    def test_minimal_run_enrichment_caps_failed_logs_at_three_per_run(self) -> None:
        failed_jobs = {
            "jobs": [
                {
                    "id": job_id,
                    "run_id": 7001,
                    "run_attempt": 2,
                    "name": f"failed-{job_id}",
                    "status": "completed",
                    "conclusion": "failure",
                    "html_url": f"https://github.com/{REPOSITORY}/actions/runs/7001/job/{job_id}",
                    "steps": [],
                }
                for job_id in range(1, 6)
            ]
        }
        client = EnrichmentClient(
            pages={
                f"/repos/{REPOSITORY}/actions/runs/7001/jobs?per_page=100": failed_jobs,
            },
            singles={
                f"/repos/{REPOSITORY}/actions/runs/7001": load_fixture("run-failed.json"),
            },
            texts={
                f"/repos/{REPOSITORY}/actions/jobs/{job_id}/logs": FakeTextResponse(
                    f"failed-{job_id}",
                    truncated=False,
                )
                for job_id in range(1, 4)
            },
        )
        inventory = self.build_inventory(
            client,
            body=f"See https://github.com/{REPOSITORY}/actions/runs/7001",
        )

        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(
            inventory,
            minimal_run_evidence=True,
        )

        self.assertEqual(3, len(client.text_calls))
        self.assertIn("run:7001:attempt:2:job:3:log", enriched.evidence)
        self.assertNotIn("run:7001:attempt:2:job:4:log", enriched.evidence)

    def test_minimal_run_enrichment_does_not_spend_log_quota_on_log_ineligible_failed_jobs(self) -> None:
        jobs = {
            "jobs": [
                self.make_job(1, conclusion="action_required"),
                self.make_job(2, conclusion="startup_failure"),
                self.make_job(3, conclusion="failure"),
                self.make_job(4, conclusion="failure"),
                self.make_job(5, conclusion="failure"),
            ]
        }
        client = EnrichmentClient(
            pages={f"/repos/{REPOSITORY}/actions/runs/7001/jobs?per_page=100": jobs},
            singles={f"/repos/{REPOSITORY}/actions/runs/7001": self.make_run()},
            texts={
                f"/repos/{REPOSITORY}/actions/jobs/{job_id}/logs": FakeTextResponse(
                    f"failed-{job_id}",
                    truncated=False,
                )
                for job_id in range(3, 6)
            },
        )
        inventory = self.build_inventory(
            client,
            body=f"See https://github.com/{REPOSITORY}/actions/runs/7001",
        )

        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(
            inventory,
            minimal_run_evidence=True,
        )

        self.assertEqual(
            [
                ("get_text", f"/repos/{REPOSITORY}/actions/jobs/3/logs", 200000),
                ("get_text", f"/repos/{REPOSITORY}/actions/jobs/4/logs", 200000),
                ("get_text", f"/repos/{REPOSITORY}/actions/jobs/5/logs", 200000),
            ],
            client.text_calls,
        )
        self.assertNotIn("run:7001:attempt:1:job:1:log", enriched.evidence)
        self.assertNotIn("run:7001:attempt:1:job:2:log", enriched.evidence)
        self.assertIn("run:7001:attempt:1:job:5:log", enriched.evidence)

    def test_minimal_run_enrichment_caps_failed_jobs_at_ten_per_run(self) -> None:
        failed_jobs = {
            "jobs": [
                {
                    "id": job_id,
                    "run_id": 7001,
                    "run_attempt": 2,
                    "name": f"failed-{job_id}",
                    "status": "completed",
                    "conclusion": "failure",
                    "html_url": f"https://github.com/{REPOSITORY}/actions/runs/7001/job/{job_id}",
                    "steps": [],
                }
                for job_id in range(1, 16)
            ]
        }
        client = EnrichmentClient(
            pages={
                f"/repos/{REPOSITORY}/actions/runs/7001/jobs?per_page=100": failed_jobs,
            },
            singles={
                f"/repos/{REPOSITORY}/actions/runs/7001": load_fixture("run-failed.json"),
            },
            texts={
                f"/repos/{REPOSITORY}/actions/jobs/{job_id}/logs": FakeTextResponse(
                    f"failed-{job_id}",
                    truncated=False,
                )
                for job_id in range(1, 4)
            },
        )
        inventory = self.build_inventory(
            client,
            body=f"See https://github.com/{REPOSITORY}/actions/runs/7001",
        )

        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(
            inventory,
            minimal_run_evidence=True,
        )

        self.assertEqual(10, len(enriched.evidence["run:7001"]["payload"]["jobs"]))
        self.assertEqual(15, enriched.evidence["run:7001"]["payload"]["totalFailedJobs"])
        self.assertTrue(enriched.evidence["run:7001"]["payload"]["jobsTruncated"])
        self.assertIn("run:7001:attempt:2:job:10", enriched.evidence)
        self.assertNotIn("run:7001:attempt:2:job:11", enriched.evidence)

    def test_minimal_run_enrichment_caps_targets_to_ten_newest_run_ids(self) -> None:
        refs = [
            {
                "sourceIssueNumber": 11,
                "sourceEvidenceId": "issue:11",
                "sourceUrl": f"https://github.com/{REPOSITORY}/issues/11",
                "targetType": "workflow-run",
                "targetRepository": REPOSITORY,
                "runId": run_id,
                "targetUrl": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
                "extractionMethod": "actions-run-url",
            }
            for run_id in range(1, 13)
        ]
        selected_run_ids = range(3, 13)
        client = EnrichmentClient(
            pages={
                f"/repos/{REPOSITORY}/actions/runs/{run_id}/jobs?per_page=100": {"jobs": []}
                for run_id in selected_run_ids
            },
            singles={
                f"/repos/{REPOSITORY}/actions/runs/{run_id}": load_fixture("run-failed.json")
                for run_id in selected_run_ids
            },
        )
        inventory = InventoryResult(
            open_issues=[{"number": 11}],
            supporting_issues=[],
            evidence={},
            collection_errors=[],
            warnings=[],
            references={11: refs},
        )

        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(
            inventory,
            minimal_run_evidence=True,
        )

        self.assertEqual(
            [f"run:{run_id}" for run_id in range(1, 13)],
            sorted(
                (evidence_id for evidence_id in enriched.evidence if evidence_id.startswith("run:")),
                key=lambda evidence_id: int(evidence_id.split(":")[1]),
            ),
        )
        self.assertTrue(
            all(enriched.evidence[f"run:{run_id}"]["availability"] == "available" for run_id in selected_run_ids)
        )
        self.assertTrue(
            all(enriched.evidence[f"run:{run_id}"]["availability"] == "partial" for run_id in (1, 2))
        )
        self.assertTrue(
            all(enriched.evidence[f"run:{run_id}"]["payload"]["runBudgetExcluded"] for run_id in (1, 2))
        )
        self.assertEqual(20, len(client.calls))

    def test_minimal_run_enrichment_marks_eleventh_referenced_run_partial(self) -> None:
        run_ids = range(11, 22)
        selected_run_ids = range(12, 22)
        client = EnrichmentClient(
            pages={
                f"/repos/{REPOSITORY}/actions/runs/{run_id}/jobs?per_page=100": {"jobs": []}
                for run_id in selected_run_ids
            },
            singles={
                f"/repos/{REPOSITORY}/actions/runs/{run_id}": self.make_run(run_id)
                for run_id in selected_run_ids
            },
        )
        inventory = self.build_inventory(
            client,
            body="\n".join(
                f"https://github.com/{REPOSITORY}/actions/runs/{run_id}"
                for run_id in run_ids
            ),
        )

        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(
            inventory,
            minimal_run_evidence=True,
        )

        excluded_run = enriched.evidence["run:11"]
        self.assertEqual("partial", excluded_run["availability"])
        self.assertEqual(
            {
                "runId": 11,
                "targetRepository": REPOSITORY,
                "runBudgetExcluded": True,
                "referencedBy": [
                    {
                        "sourceIssueNumber": 11,
                        "sourceEvidenceId": "issue:11",
                        "sourceUrl": f"https://github.com/{REPOSITORY}/issues/11",
                        "extractionMethod": "actions-run-url",
                    }
                ],
            },
            excluded_run["payload"],
        )
        self.assertNotIn(("get", f"/repos/{REPOSITORY}/actions/runs/11"), client.calls)
        validate_snapshot(snapshot_from_result(enriched))

    def test_workflow_job_evidence_inherits_parent_referenced_by(self) -> None:
        client = EnrichmentClient(
            pages={f"/repos/{REPOSITORY}/actions/runs/7001/jobs?per_page=100": {"jobs": [self.make_job(2001)]}},
            singles={f"/repos/{REPOSITORY}/actions/runs/7001": self.make_run()},
            texts={f"/repos/{REPOSITORY}/actions/jobs/2001/logs": FakeTextResponse("failed", truncated=False)},
        )
        inventory = self.build_inventory(client, body=f"See https://github.com/{REPOSITORY}/actions/runs/7001")

        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(
            inventory,
            minimal_run_evidence=True,
        )

        self.assertEqual(
            enriched.evidence["run:7001"]["payload"]["referencedBy"],
            enriched.evidence["run:7001:attempt:1:job:2001"]["payload"]["referencedBy"],
        )
        validate_snapshot(snapshot_from_result(enriched))

    def test_workflow_annotation_evidence_inherits_parent_referenced_by(self) -> None:
        client = EnrichmentClient(
            pages={
                f"/repos/{REPOSITORY}/actions/runs/7001/jobs?per_page=100": {"jobs": [self.make_job(2001, check_run_id=8001)]},
                f"/repos/{REPOSITORY}/actions/runs/7001/artifacts?per_page=100": {"artifacts": []},
                f"/repos/{REPOSITORY}/check-runs/8001/annotations?per_page=100": [
                    {
                        "id": 9001,
                        "path": "tests/test_sample.py",
                        "start_line": 10,
                        "end_line": 10,
                        "annotation_level": "failure",
                        "message": "Assertion failed",
                    }
                ],
            },
            singles={f"/repos/{REPOSITORY}/actions/runs/7001": self.make_run()},
            texts={f"/repos/{REPOSITORY}/actions/jobs/2001/logs": FakeTextResponse("failed", truncated=False)},
        )
        inventory = self.build_inventory(client, body=f"See https://github.com/{REPOSITORY}/actions/runs/7001")

        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(inventory)

        self.assertEqual(
            enriched.evidence["run:7001"]["payload"]["referencedBy"],
            enriched.evidence["run:7001:check:8001:annotation:9001"]["payload"]["referencedBy"],
        )
        validate_snapshot(snapshot_from_result(enriched))

    def test_workflow_log_evidence_inherits_parent_referenced_by(self) -> None:
        client = EnrichmentClient(
            pages={f"/repos/{REPOSITORY}/actions/runs/7001/jobs?per_page=100": {"jobs": [self.make_job(2001)]}},
            singles={f"/repos/{REPOSITORY}/actions/runs/7001": self.make_run()},
            texts={f"/repos/{REPOSITORY}/actions/jobs/2001/logs": FakeTextResponse("failed", truncated=False)},
        )
        inventory = self.build_inventory(client, body=f"See https://github.com/{REPOSITORY}/actions/runs/7001")

        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(
            inventory,
            minimal_run_evidence=True,
        )

        self.assertEqual(
            enriched.evidence["run:7001"]["payload"]["referencedBy"],
            enriched.evidence["run:7001:attempt:1:job:2001:log"]["payload"]["referencedBy"],
        )
        validate_snapshot(snapshot_from_result(enriched))

    def test_external_workflow_children_inherit_parent_repository_and_associations(self) -> None:
        target_repository = "other/repo"
        raw_run = self.make_run()
        raw_run["html_url"] = f"https://github.com/{target_repository}/actions/runs/7001"
        raw_job = self.make_job(2001, check_run_id=8001)
        raw_job["html_url"] = f"https://github.com/{target_repository}/actions/runs/7001/job/2001"
        raw_job["check_run_url"] = f"https://api.github.com/repos/{target_repository}/check-runs/8001"
        client = EnrichmentClient(
            pages={
                f"/repos/{target_repository}/actions/runs/7001/jobs?per_page=100": {"jobs": [raw_job]},
                f"/repos/{target_repository}/actions/runs/7001/artifacts?per_page=100": {"artifacts": []},
                f"/repos/{target_repository}/check-runs/8001/annotations?per_page=100": [
                    {
                        "id": 9001,
                        "path": "external/build.py",
                        "start_line": 1,
                        "end_line": 1,
                        "annotation_level": "failure",
                        "message": "failed",
                    }
                ],
            },
            singles={f"/repos/{target_repository}/actions/runs/7001": raw_run},
            texts={
                f"/repos/{target_repository}/actions/jobs/2001/logs": FakeTextResponse(
                    "failed",
                    truncated=False,
                )
            },
        )
        inventory = self.build_inventory(
            client,
            body=f"https://github.com/{target_repository}/actions/runs/7001",
        )

        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(inventory)

        run_payload = enriched.evidence["run:7001"]["payload"]
        child_payloads = (
            run_payload["jobs"][0],
            enriched.evidence["run:7001:attempt:1:job:2001"]["payload"],
            enriched.evidence["run:7001:check:8001:annotation:9001"]["payload"],
            enriched.evidence["run:7001:attempt:1:job:2001:log"]["payload"],
        )
        for payload in child_payloads:
            with self.subTest(payload=payload):
                self.assertEqual(target_repository, payload.get("targetRepository"))
                self.assertEqual(run_payload["referencedBy"], payload.get("referencedBy"))
        validate_snapshot(snapshot_from_result(enriched))

    def test_enrich_github_evidence_adds_run_attempts_jobs_logs_annotations_artifacts_and_recent_history(self) -> None:
        run = load_fixture("run-failed.json")
        jobs = load_fixture("jobs-failed.json")
        previous_attempt_jobs = {
            "jobs": [
                {
                    "id": 1001,
                    "run_id": 7001,
                    "run_attempt": 1,
                    "name": "test-linux",
                    "status": "completed",
                    "conclusion": "cancelled",
                    "started_at": "2026-08-16T11:40:00Z",
                    "completed_at": "2026-08-16T11:55:00Z",
                    "html_url": "https://github.com/owner/repo/actions/runs/7001/job/1001",
                    "check_run_url": "https://api.github.com/repos/owner/repo/check-runs/7002",
                    "steps": [
                        {
                            "number": 1,
                            "name": "Run tests",
                            "status": "completed",
                            "conclusion": "cancelled",
                            "started_at": "2026-08-16T11:42:00Z",
                            "completed_at": "2026-08-16T11:55:00Z"
                        }
                    ]
                }
            ]
        }
        history = {
            "total_count": 12,
            "workflow_runs": [
                {
                    "id": 7100 + offset,
                    "run_attempt": 1,
                    "event": "push",
                    "head_branch": "main",
                    "head_sha": f"{offset:040x}"[-40:],
                    "conclusion": "success" if offset % 2 == 0 else "failure",
                    "created_at": f"2026-08-{offset:02d}T00:00:00Z",
                    "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{7100 + offset}"
                }
                for offset in range(12, 0, -1)
            ]
        }
        client = EnrichmentClient(
            pages={
                f"/repos/{REPOSITORY}/actions/runs/7001/jobs?per_page=100": jobs,
                f"/repos/{REPOSITORY}/actions/runs/7001/attempts/1/jobs?per_page=100": previous_attempt_jobs,
                f"/repos/{REPOSITORY}/actions/runs/7001/artifacts?per_page=100": {
                    "artifacts": [
                        {"id": 1, "name": "test-results", "expired": False},
                        {"id": 2, "name": "old-logs", "expired": True},
                    ]
                },
                f"/repos/{REPOSITORY}/check-runs/8001/annotations?per_page=100": [
                    {
                        "path": "tests/test_sample.py",
                        "start_line": 10,
                        "end_line": 10,
                        "annotation_level": "failure",
                        "message": "Assertion failed"
                    }
                ],
            },
            singles={
                f"/repos/{REPOSITORY}/actions/runs/7001": run,
                f"/repos/{REPOSITORY}/actions/workflows/9001/runs?branch=main&per_page=10": history,
            },
            texts={
                f"/repos/{REPOSITORY}/actions/jobs/2001/logs": FakeTextResponse("f" * 200000, truncated=True),
            },
        )
        inventory = self.build_inventory(client, body="See https://github.com/owner/repo/actions/runs/7001")

        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(inventory)

        run_payload = enriched.evidence["run:7001"]["payload"]
        self.assertEqual("CI", run_payload["workflow"])
        self.assertEqual([1, 2], run_payload["attempts"])
        self.assertEqual([1001, 2001, 2002], [job["jobId"] for job in run_payload["jobs"]])
        self.assertEqual(
            [7112, 7111, 7110, 7109, 7108, 7107, 7106, 7105, 7104, 7103],
            [item["runId"] for item in run_payload["recentHistory"]],
        )
        self.assertEqual(
            [
                {"name": "test-results", "expired": False},
                {"name": "old-logs", "expired": True},
            ],
            run_payload["artifacts"],
        )
        failed_job = enriched.evidence["run:7001:attempt:2:job:2001"]["payload"]
        self.assertEqual(["run:7001:check:8001:annotation:1"], failed_job["annotationEvidenceIds"])
        self.assertEqual(
            "run:7001:attempt:2:job:2001:log",
            enriched.evidence["run:7001:attempt:2:job:2001:log"]["payload"]["evidenceId"],
        )
        self.assertTrue(enriched.evidence["run:7001:attempt:2:job:2001:log"]["payload"]["truncated"])
        self.assertEqual(200000, len(enriched.evidence["run:7001:attempt:2:job:2001:log"]["payload"]["excerpt"]))
        self.assertNotIn("run:7001:attempt:1:job:1001:log", enriched.evidence)
        self.assertNotIn("run:7001:attempt:2:job:2002:log", enriched.evidence)
        self.assertNotIn(("get_text", f"/repos/{REPOSITORY}/actions/jobs/1001/logs", 200000), client.text_calls)
        self.assertIn(("get_text", f"/repos/{REPOSITORY}/actions/jobs/2001/logs", 200000), client.text_calls)
        self.assertNotIn(("get_text", f"/repos/{REPOSITORY}/actions/jobs/2002/logs", 200000), client.text_calls)
        self.assertNotIn(
            ("get_pages", f"/repos/{REPOSITORY}/check-runs/7002/annotations?per_page=100"),
            client.calls,
        )
        self.assertNotIn(
            ("get_pages", f"/repos/{REPOSITORY}/check-runs/8002/annotations?per_page=100"),
            client.calls,
        )
        self.assertIn(("get_pages", f"/repos/{REPOSITORY}/actions/runs/7001/attempts/1/jobs?per_page=100"), client.calls)
        self.assertNotIn(("get_pages", f"/repos/{REPOSITORY}/actions/runs/7001/attempts/2/jobs?per_page=100"), client.calls)
        validate_snapshot(snapshot_from_result(enriched))

    def test_enrich_github_evidence_marks_unavailable_failed_logs_and_records_effect(self) -> None:
        run = load_fixture("run-failed.json")
        jobs = load_fixture("jobs-failed.json")
        client = EnrichmentClient(
            pages={
                f"/repos/{REPOSITORY}/actions/runs/7001/jobs?per_page=100": jobs,
                f"/repos/{REPOSITORY}/actions/runs/7001/attempts/1/jobs?per_page=100": {"jobs": []},
                f"/repos/{REPOSITORY}/actions/runs/7001/artifacts?per_page=100": {"artifacts": []},
                f"/repos/{REPOSITORY}/check-runs/8001/annotations?per_page=100": [],
                f"/repos/{REPOSITORY}/check-runs/8002/annotations?per_page=100": [],
            },
            singles={
                f"/repos/{REPOSITORY}/actions/runs/7001": run,
                f"/repos/{REPOSITORY}/actions/workflows/9001/runs?branch=main&per_page=10": {
                    "total_count": 0,
                    "workflow_runs": [],
                },
            },
            texts={f"/repos/{REPOSITORY}/actions/jobs/2001/logs": FakeApiError("expired", 410, "expired")},
        )
        inventory = self.build_inventory(client, body="See https://github.com/owner/repo/actions/runs/7001")

        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(inventory)

        log_record = enriched.evidence["run:7001:attempt:2:job:2001:log"]
        self.assertEqual("expired-or-unavailable", log_record["availability"])
        self.assertEqual("expired", log_record["payload"]["errorCategory"])
        self.assertEqual(
            [(error.stage, error.effect) for error in enriched.collection_errors],
            [("workflow-log", "workflow-log evidence unavailable")],
        )
        validate_snapshot(snapshot_from_result(enriched))

    def test_enrich_github_evidence_adds_merged_pull_request_details_files_and_linked_issues(self) -> None:
        client = EnrichmentClient(
            pages={
                f"/repos/{REPOSITORY}/pulls/77/files?per_page=100": [
                    {"filename": "src/app.py", "status": "modified"},
                    {"filename": "tests/test_app.py", "status": "added"},
                ]
            },
            singles={
                f"/repos/{REPOSITORY}/issues/77": make_issue(
                    77,
                    state="closed",
                    body="Fixes #88\nRelated to #89",
                    labels=["bug"],
                    is_pull_request=True,
                    closed_at="2026-08-10T09:30:00Z",
                ),
                f"/repos/{REPOSITORY}/pulls/77": load_fixture("pr-merged.json"),
            },
        )
        inventory = self.build_inventory(client, body="See https://github.com/owner/repo/pull/77")

        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(inventory)

        pr_payload = enriched.evidence["pr:77"]["payload"]
        self.assertEqual("closed", pr_payload["state"])
        self.assertEqual("2026-08-10T09:30:00Z", pr_payload["mergedAt"])
        self.assertEqual("dddddddddddddddddddddddddddddddddddddddd", pr_payload["mergeCommitSha"])
        self.assertEqual({"ref": "main", "sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}, pr_payload["base"])
        self.assertEqual(
            {
                "ref": "feature/fix-flake",
                "sha": "cccccccccccccccccccccccccccccccccccccccc",
                "repository": REPOSITORY,
            },
            pr_payload["head"],
        )
        self.assertEqual(
            [
                {"path": "src/app.py", "status": "modified"},
                {"path": "tests/test_app.py", "status": "added"},
            ],
            pr_payload["files"],
        )
        self.assertEqual([88, 89], [ref["targetNumber"] for ref in pr_payload["linkedIssues"]])
        validate_snapshot(snapshot_from_result(enriched))

    def test_enrich_github_evidence_leaves_normal_issue_refs_as_issues_not_pull_requests(self) -> None:
        client = EnrichmentClient(
            singles={
                f"/repos/{REPOSITORY}/issues/88": make_issue(
                    88,
                    state="closed",
                    body="Resolved by docs update",
                    labels=["documentation"],
                    closed_at="2026-08-11T00:00:00Z",
                )
            }
        )
        inventory = self.build_inventory(client, body="See https://github.com/owner/repo/issues/88")

        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(inventory)

        self.assertIn("issue:88", enriched.evidence)
        self.assertNotIn("pr:88", enriched.evidence)
        self.assertEqual("closed", enriched.evidence["issue:88"]["payload"]["state"])
        validate_snapshot(snapshot_from_result(enriched))

    def test_enrich_github_evidence_preserves_existing_issue_detail_when_referenced_issue_is_already_collected(self) -> None:
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(11, labels=["ci-failure-cause"], body="#88"),
                make_issue(
                    88,
                    labels=["ci-failure-cause"],
                    body=(
                        "<!-- ci-shepherd:signature=Sample.Flake -->\n"
                        "Test name: Sample.Tests.Flake"
                    ),
                ),
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/11/comments": [],
            f"/repos/{REPOSITORY}/issues/11/timeline": [],
            f"/repos/{REPOSITORY}/issues/88/comments": [
                {
                    "id": 8801,
                    "html_url": f"https://github.com/{REPOSITORY}/issues/88#issuecomment-8801",
                    "created_at": "2026-08-04T00:00:00Z",
                    "updated_at": "2026-08-04T00:00:00Z",
                    "user": {"login": "mona"},
                    "body": "Exception type: System.TimeoutException\nRelated to #99",
                }
            ],
            f"/repos/{REPOSITORY}/issues/88/timeline": [
                {"id": 8802, "event": "closed", "created_at": "2026-08-05T00:00:00Z", "actor": {"login": "mona"}},
                {"id": 8803, "event": "reopened", "created_at": "2026-08-06T00:00:00Z", "actor": {"login": "mona"}},
            ],
        }
        client = EnrichmentClient(
            pages=pages,
            singles={
                f"/repos/{REPOSITORY}/issues/88": make_issue(
                    88,
                    title="Issue 88 refreshed",
                    labels=["automation-broken"],
                    body="Updated summary",
                )
            },
        )
        inventory = Collector(client, REPOSITORY, NOW).collect()

        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(inventory)

        payload = enriched.evidence["issue:88"]["payload"]
        self.assertEqual("Issue 88 refreshed", payload["title"])
        self.assertEqual(["automation-broken", "ci-failure-cause"], payload["labels"])
        self.assertEqual([8801], [comment["id"] for comment in payload["comments"]])
        self.assertEqual(
            [
                {"openedAt": "2026-08-01T00:00:00Z", "closedAt": "2026-08-05T00:00:00Z"},
                {"openedAt": "2026-08-06T00:00:00Z", "closedAt": None},
            ],
            payload["episodes"],
        )
        self.assertEqual(["signature"], [marker["key"] for marker in payload["markers"]])
        self.assertEqual(["testName"], [fact["field"] for fact in payload["facts"]])
        self.assertEqual(
            ["exceptionType"],
            [fact["field"] for fact in enriched.evidence["issue:88:comment:8801"]["payload"]["facts"]],
        )
        self.assertEqual([99], [reference["targetNumber"] for reference in payload["references"]])
        self.assertEqual(
            [
                {
                    "sourceIssueNumber": 11,
                    "sourceEvidenceId": "issue:11",
                    "sourceUrl": f"https://github.com/{REPOSITORY}/issues/11",
                    "extractionMethod": "local-issue",
                }
            ],
            payload["referencedBy"],
        )
        validate_snapshot(snapshot_from_result(enriched))

    def test_enrich_github_evidence_qualifies_cross_repository_issue_ids(self) -> None:
        first_issue = make_issue(77, title="First repository issue")
        first_issue["html_url"] = "https://github.com/alpha/project/issues/77"
        second_issue = make_issue(77, title="Second repository issue")
        second_issue["html_url"] = "https://github.com/beta/project/issues/77"
        client = EnrichmentClient(
            singles={
                "/repos/alpha/project/issues/77": first_issue,
                "/repos/beta/project/issues/77": second_issue,
            }
        )
        inventory = self.build_inventory(
            client,
            body=(
                "See https://github.com/alpha/project/issues/77 and "
                "https://github.com/beta/project/issues/77"
            ),
        )

        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(inventory)

        self.assertEqual(
            "First repository issue",
            enriched.evidence["issue:alpha/project:77"]["payload"]["title"],
        )
        self.assertEqual(
            "Second repository issue",
            enriched.evidence["issue:beta/project:77"]["payload"]["title"],
        )
        validate_snapshot(snapshot_from_result(enriched))

    def test_enrich_github_evidence_upgrades_local_shorthand_issue_reference_to_pull_request(self) -> None:
        client = EnrichmentClient(
            pages={
                f"/repos/{REPOSITORY}/issues/77/comments": [],
                f"/repos/{REPOSITORY}/issues/77/timeline": [],
                f"/repos/{REPOSITORY}/pulls/77/files?per_page=100": [],
            },
            singles={
                f"/repos/{REPOSITORY}/issues/77": make_issue(77, is_pull_request=True),
                f"/repos/{REPOSITORY}/pulls/77": load_fixture("pr-merged.json"),
            },
        )
        inventory = self.build_inventory(client, body="Fixed by #77")

        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(inventory)

        self.assertIn("pr:77", enriched.evidence)
        self.assertNotIn("issue:77", enriched.evidence)
        self.assertEqual("2026-08-10T09:30:00Z", enriched.evidence["pr:77"]["payload"]["mergedAt"])
        validate_snapshot(snapshot_from_result(enriched))

    def test_enrich_github_evidence_preserves_shorthand_and_full_pull_references(self) -> None:
        client = EnrichmentClient(
            pages={
                f"/repos/{REPOSITORY}/issues/77/comments": [],
                f"/repos/{REPOSITORY}/issues/77/timeline": [],
                f"/repos/{REPOSITORY}/pulls/77/files?per_page=100": [],
            },
            singles={
                f"/repos/{REPOSITORY}/issues/77": make_issue(77, is_pull_request=True),
                f"/repos/{REPOSITORY}/pulls/77": load_fixture("pr-merged.json"),
            },
        )
        inventory = self.build_inventory(
            client,
            body=f"Fixed by #77 and https://github.com/{REPOSITORY}/pull/77",
        )

        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(inventory)

        referenced_by = enriched.evidence["pr:77"]["payload"]["referencedBy"]
        self.assertEqual(
            ["full-pull-url", "local-issue"],
            [reference["extractionMethod"] for reference in referenced_by],
        )
        validate_snapshot(snapshot_from_result(enriched))

    def test_enrich_github_evidence_marks_malformed_pull_payload_partial(self) -> None:
        client = EnrichmentClient(
            singles={
                f"/repos/{REPOSITORY}/issues/77": make_issue(77, is_pull_request=True),
                f"/repos/{REPOSITORY}/pulls/77": [],
            }
        )
        inventory = self.build_inventory(client, body=f"See https://github.com/{REPOSITORY}/pull/77")

        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(inventory)

        self.assertEqual("partial", enriched.evidence["pr:77"]["availability"])
        self.assertEqual("generic", enriched.evidence["pr:77"]["payload"]["errorCategory"])
        self.assertEqual("pull-request", enriched.collection_errors[-1].stage)
        validate_snapshot(snapshot_from_result(enriched))

    def test_enrich_github_evidence_resolves_shorthand_links_against_external_pull_repository(self) -> None:
        external_repository = "alpha/project"
        external_issue = make_issue(77, body="Fixes #88", is_pull_request=True)
        external_issue["html_url"] = f"https://github.com/{external_repository}/pull/77"
        external_pull = load_fixture("pr-merged.json")
        external_pull["html_url"] = f"https://github.com/{external_repository}/pull/77"
        client = EnrichmentClient(
            pages={f"/repos/{external_repository}/pulls/77/files?per_page=100": []},
            singles={
                f"/repos/{external_repository}/issues/77": external_issue,
                f"/repos/{external_repository}/pulls/77": external_pull,
            },
        )
        inventory = self.build_inventory(
            client,
            body=f"See https://github.com/{external_repository}/pull/77",
        )

        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(inventory)

        linked_issue = enriched.evidence["pr:alpha/project:77"]["payload"]["linkedIssues"][0]
        self.assertEqual(88, linked_issue["targetNumber"])
        self.assertEqual(
            f"https://github.com/{external_repository}/issues/88",
            linked_issue["targetUrl"],
        )
        validate_snapshot(snapshot_from_result(enriched))

    def test_enrich_github_evidence_uses_full_commit_sha_and_fetches_duplicates_once(self) -> None:
        short_sha = "abcdef1234567"
        full_sha = "abcdef1234567890abcdef1234567890abcdef12"
        client = EnrichmentClient(
            singles={
                f"/repos/{REPOSITORY}/commits/{short_sha}": {
                    "sha": full_sha,
                    "html_url": f"https://github.com/{REPOSITORY}/commit/{full_sha}",
                    "commit": {
                        "author": {
                            "name": "Mona Octocat",
                            "email": "mona@example.com",
                            "date": "2026-08-12T00:00:00Z"
                        },
                        "message": "Fix flaky test"
                    },
                    "author": {"login": "mona"},
                    "files": [
                        {"filename": "src/app.py"},
                        {"filename": "tests/test_app.py"}
                    ]
                }
            }
        )
        inventory = self.build_inventory(
            client,
            body=(
                f"Commit: {short_sha}\n"
                f"Again commit: {short_sha}\n"
                f"And https://github.com/{REPOSITORY}/commit/{short_sha}"
            ),
        )

        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(inventory)

        self.assertIn(f"commit:{full_sha}", enriched.evidence)
        self.assertNotIn(f"commit:{short_sha}", enriched.evidence)
        commit_payload = enriched.evidence[f"commit:{full_sha}"]["payload"]
        self.assertEqual(full_sha, commit_payload["sha"])
        self.assertEqual("mona", commit_payload["author"]["login"])
        self.assertEqual("Mona Octocat", commit_payload["author"]["name"])
        self.assertEqual("mona@example.com", commit_payload["author"]["email"])
        self.assertEqual("2026-08-12T00:00:00Z", commit_payload["author"]["date"])
        self.assertEqual("Fix flaky test", commit_payload["message"])
        self.assertEqual(["src/app.py", "tests/test_app.py"], commit_payload["changedPaths"])
        self.assertEqual(1, client.calls.count(("get", f"/repos/{REPOSITORY}/commits/{short_sha}")))
        validate_snapshot(snapshot_from_result(enriched))

    def test_enrich_github_evidence_merges_referenced_by_for_short_and_full_commit_aliases(self) -> None:
        short_sha = "abcdef1234567"
        full_sha = "abcdef1234567890abcdef1234567890abcdef12"
        commit_payload = {
            "sha": full_sha,
            "html_url": f"https://github.com/{REPOSITORY}/commit/{full_sha}",
            "commit": {
                "author": {
                    "name": "Mona Octocat",
                    "email": "mona@example.com",
                    "date": "2026-08-12T00:00:00Z",
                },
                "message": "Fix flaky test",
            },
            "author": {"login": "mona"},
            "files": [{"filename": "src/app.py"}],
        }
        client = EnrichmentClient(
            singles={
                f"/repos/{REPOSITORY}/commits/{short_sha}": commit_payload,
                f"/repos/{REPOSITORY}/commits/{full_sha}": commit_payload,
            }
        )
        inventory = InventoryResult(
            open_issues=[{"number": 11}, {"number": 12}],
            supporting_issues=[],
            evidence={},
            collection_errors=[],
            warnings=[],
            references={
                11: [
                    {
                        "sourceIssueNumber": 11,
                        "sourceEvidenceId": "issue:11",
                        "sourceUrl": f"https://github.com/{REPOSITORY}/issues/11",
                        "targetType": "commit",
                        "targetRepository": REPOSITORY,
                        "sha": short_sha,
                        "targetUrl": f"https://github.com/{REPOSITORY}/commit/{short_sha}",
                        "extractionMethod": "labelled-commit",
                    }
                ],
                12: [
                    {
                        "sourceIssueNumber": 12,
                        "sourceEvidenceId": "issue:12",
                        "sourceUrl": f"https://github.com/{REPOSITORY}/issues/12",
                        "targetType": "commit",
                        "targetRepository": REPOSITORY,
                        "sha": full_sha,
                        "targetUrl": f"https://github.com/{REPOSITORY}/commit/{full_sha}",
                        "extractionMethod": "commit-url",
                    }
                ],
            },
        )

        enriched = Collector(client, REPOSITORY, NOW).enrich_github_evidence(inventory)

        self.assertIn(f"commit:{full_sha}", enriched.evidence)
        self.assertNotIn(f"commit:{short_sha}", enriched.evidence)
        self.assertEqual(
            [
                {
                    "sourceIssueNumber": 11,
                    "sourceEvidenceId": "issue:11",
                    "sourceUrl": f"https://github.com/{REPOSITORY}/issues/11",
                    "extractionMethod": "labelled-commit",
                },
                {
                    "sourceIssueNumber": 12,
                    "sourceEvidenceId": "issue:12",
                    "sourceUrl": f"https://github.com/{REPOSITORY}/issues/12",
                    "extractionMethod": "commit-url",
                },
            ],
            enriched.evidence[f"commit:{full_sha}"]["payload"]["referencedBy"],
        )
        validate_snapshot(snapshot_from_result(enriched))


if __name__ == "__main__":
    unittest.main()
