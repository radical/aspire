from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from ci_shepherd.collector import (
    Collector,
    InventoryResult,
    InventoryError,
    _CandidateIssue,
    _CORRELATION_FACT_FIELDS,
    _CORRELATION_MARKER_KEYS,
    _requires_full_issue_recollection,
    _repository_scoped_evidence_id,
)
from ci_shepherd.models import ValidationError, validate_report, validate_snapshot
from ci_shepherd.history import record_history
from ci_shepherd.refresh import RefreshPlan, complete_refresh_plan


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"
REPOSITORY = "owner/repo"
NOW = datetime(2026, 8, 17, 22, 0, tzinfo=UTC)
CUTOFF = "2026-05-19T22:00:00Z"


class FixtureClient:
    def __init__(self, fixture_root: Path) -> None:
        self._fixture_root = fixture_root
        self._api_map = json.loads((fixture_root / "api-map.json").read_text(encoding="utf-8"))
        self.calls: list[tuple[str, str]] = []

    def get_pages(self, endpoint: str, key: str | None = None) -> object:
        self.calls.append(("get_pages", endpoint))
        return self._load(endpoint)

    def get(self, endpoint: str) -> object:
        self.calls.append(("get", endpoint))
        return self._load(endpoint)

    def _load(self, endpoint: str) -> object:
        mapping = self._api_map[endpoint]
        payload = json.loads((self._fixture_root / mapping["file"]).read_text(encoding="utf-8"))
        if "key" in mapping:
            payload = payload[mapping["key"]]
        return copy.deepcopy(payload)


class RepositoryScopedEvidenceIdTests(unittest.TestCase):
    def test_repository_scoped_evidence_id_treats_case_variant_primary_repository_as_local(self) -> None:
        self.assertEqual(
            "issue:2",
            _repository_scoped_evidence_id("issue", "OWNER/REPO", "owner/repo", 2),
        )


class IncrementalCollectionPlanTests(unittest.TestCase):
    def test_only_root_issue_retries_require_full_issue_recollection(self) -> None:
        self.assertFalse(
            _requires_full_issue_recollection(
                RefreshPlan(retry=("run:99", "source:tests%2FExample.cs")),
                (1,),
            )
        )
        self.assertTrue(
            _requires_full_issue_recollection(
                RefreshPlan(retry=("issue:1",)),
                (1,),
            )
        )


class ScriptedClient:
    def __init__(self, pages: dict[str, object] | None = None, singles: dict[str, object] | None = None) -> None:
        self._pages = dict(pages or {})
        self._singles = dict(singles or {})
        self.calls: list[tuple[str, str]] = []

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


def mixed_root_report(high_risk_issue_number: int) -> dict[str, object]:
    decisions = []
    for issue_number in (21, 22):
        high_risk = issue_number == high_risk_issue_number
        evidence = [
            {"id": f"issue:{issue_number}", "kind": "issue-event"},
        ]
        related_issues = []
        if high_risk:
            evidence.append(
                {
                    "id": "issue:403",
                    "kind": "issue-event",
                    "role": "canonical-issue",
                }
            )
            related_issues.append(
                {
                    "type": "canonical-tracker",
                    "sourceIssueNumber": issue_number,
                    "targetIssueNumber": 403,
                }
            )
        decisions.append(
            {
                "issueNumber": issue_number,
                "issueUrl": f"https://github.com/{REPOSITORY}/issues/{issue_number}",
                "issueKind": "incident",
                "state": "tracked-elsewhere" if high_risk else "observing",
                "proposedAction": "close-as-tracked" if high_risk else "wait",
                "confidence": "high",
                "summary": "summary",
                "reasoning": "reasoning",
                "evidence": evidence,
                "contradictoryEvidence": [],
                "missingEvidence": [],
                "nextCondition": {
                    "type": "monitor",
                    "description": "Wait for the next workflow run.",
                },
                "suggestedOwners": [],
                "relatedIssues": related_issues,
                "changedSincePreviousRun": False,
            }
        )
    return {
        "schemaVersion": 1,
        "repository": REPOSITORY,
        "decisions": decisions,
    }


class CollectorTests(unittest.TestCase):
    def test_enriches_primary_pull_request_inventory_as_evidence(self) -> None:
        issue_payload = make_issue(23, is_pull_request=True)
        issue_payload["html_url"] = f"https://github.com/{REPOSITORY}/pull/23"
        client = ScriptedClient(
            pages={
                f"/repos/{REPOSITORY}/pulls/23/files?per_page=100": [
                    {"filename": "eng/example.yml", "status": "modified"}
                ]
            },
            singles={
                f"/repos/{REPOSITORY}/issues/23": issue_payload,
                f"/repos/{REPOSITORY}/pulls/23": {
                    "number": 23,
                    "state": "open",
                    "html_url": f"https://github.com/{REPOSITORY}/pull/23",
                    "merged_at": None,
                    "merge_commit_sha": None,
                    "base": {"ref": "main", "sha": "base"},
                    "head": {
                        "ref": "automation/fix",
                        "sha": "head",
                        "repo": {"full_name": REPOSITORY},
                    },
                },
            },
        )
        inventory = InventoryResult(
            open_issues=[],
            supporting_issues=[],
            evidence={},
            collection_errors=[],
            warnings=[],
            references={},
            open_pull_requests=[
                {
                    "number": 23,
                    "url": f"https://github.com/{REPOSITORY}/pull/23",
                }
            ],
        )

        result = Collector(client, REPOSITORY, NOW).enrich_github_evidence(
            inventory
        )

        self.assertEqual("pull-request", result.evidence["pr:23"]["kind"])
        self.assertEqual("head", result.evidence["pr:23"]["payload"]["head"]["sha"])
        self.assertEqual([], result.evidence["pr:23"]["payload"]["files"])
        self.assertNotIn(
            ("get_pages", f"/repos/{REPOSITORY}/pulls/23/files?per_page=100"),
            client.calls,
        )

    def test_collect_includes_bot_pull_requests_and_excludes_copilot_assigned_work(
        self,
    ) -> None:
        bot_pull = make_issue(23, labels=["dependencies"])
        bot_pull["user"] = {"login": "github-actions[bot]"}
        bot_pull["pull_request"] = {
            "url": f"https://api.github.com/repos/{REPOSITORY}/pulls/23"
        }
        bot_pull["html_url"] = f"https://github.com/{REPOSITORY}/pull/23"
        copilot_issue = make_issue(24, labels=["ci-failure-cause"])
        copilot_issue["assignees"] = [{"login": "copilot-swe-agent[bot]"}]
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                copilot_issue
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=open&creator=github-actions%5Bbot%5D&per_page=100": [
                bot_pull
            ],
        }

        result = Collector(
            ScriptedClient(pages=pages),
            REPOSITORY,
            NOW,
            bot_authors=("github-actions[bot]",),
        ).collect(
            include_supporting=False,
            include_timeline=False,
        )

        self.assertEqual([], result.open_issues)
        self.assertEqual([23], [pull["number"] for pull in result.open_pull_requests])
        self.assertEqual(
            [
                {
                    "number": 24,
                    "targetKind": "issue",
                    "reason": "assigned-to-copilot",
                }
            ],
            result.rejected_candidates,
        )

    def test_collect_includes_open_github_actions_issues_without_target_labels(self) -> None:
        bot_issue = make_issue(22, labels=["agentic-workflows"])
        bot_issue["user"] = {"login": "github-actions[bot]"}
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(21, labels=["ci-failure-cause"])
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=open&creator=github-actions%5Bbot%5D&per_page=100": [
                bot_issue
            ],
            f"/repos/{REPOSITORY}/issues/21/comments": [],
            f"/repos/{REPOSITORY}/issues/22/comments": [],
        }

        result = Collector(
            ScriptedClient(pages=pages),
            REPOSITORY,
            NOW,
            bot_authors=("github-actions[bot]",),
        ).collect(
            include_supporting=False,
            include_timeline=False,
        )

        self.assertEqual([21, 22], [issue["number"] for issue in result.open_issues])
        self.assertEqual("github-actions[bot]", result.open_issues[1]["author"])

    def test_collect_recognizes_gh_aw_failure_issue_producer(self) -> None:
        body = """\
<!-- gh-aw-agentic-workflow: Milestone Changelog Generator, engine: copilot, id: 1001, workflow_id: milestone-changelog, run: https://github.com/owner/repo/actions/runs/1001 -->
<!-- gh-aw-failure-issue: true, workflow_id: milestone-changelog, branch: main, failure_categories: agent_failure -->
<!-- gh-aw-expires: 2026-08-24T00:00:00Z -->
"""
        bot_issue = make_issue(22, labels=["agentic-workflows"], body=body)
        bot_issue["user"] = {"login": "github-actions[bot]"}
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=open&creator=github-actions%5Bbot%5D&per_page=100": [
                bot_issue
            ],
            f"/repos/{REPOSITORY}/issues/22/comments": [],
        }

        result = Collector(
            ScriptedClient(pages=pages),
            REPOSITORY,
            NOW,
            bot_authors=("github-actions[bot]",),
        ).collect(
            include_supporting=False,
            include_timeline=False,
        )

        self.assertEqual("gh-aw-failure-issue", result.open_issues[0]["producer"])

    def test_collect_recognizes_gh_aw_failed_jobs_issue_without_failure_marker(self) -> None:
        body = """\
<!-- gh-aw-agentic-workflow: Analyze CI Failure, id: 1001, run: https://github.com/owner/repo/actions/runs/1001 -->
<!-- gh-aw-expires: 2026-08-24T00:00:00Z -->
"""
        bot_issue = make_issue(
            22,
            title="[aw] Failed jobs: Analyze CI Failure",
            labels=["agentic-workflows"],
            body=body,
        )
        bot_issue["user"] = {"login": "github-actions[bot]"}
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=open&creator=github-actions%5Bbot%5D&per_page=100": [
                bot_issue
            ],
            f"/repos/{REPOSITORY}/issues/22/comments": [],
        }

        result = Collector(
            ScriptedClient(pages=pages),
            REPOSITORY,
            NOW,
            bot_authors=("github-actions[bot]",),
        ).collect(
            include_supporting=False,
            include_timeline=False,
        )

        self.assertEqual("gh-aw-failure-issue", result.open_issues[0]["producer"])

    def test_collect_matches_recent_gh_aw_failures_for_the_same_workflow(self) -> None:
        failure_marker = (
            "<!-- gh-aw-failure-issue: true, workflow_id: milestone-changelog, "
            "branch: main, failure_categories: agent_failure -->"
        )
        open_issue = make_issue(22, labels=["agentic-workflows"], body=failure_marker)
        open_issue["user"] = {"login": "github-actions[bot]"}
        closed_issue = make_issue(
            23,
            state="closed",
            labels=["agentic-workflows"],
            body=failure_marker,
            closed_at="2026-08-16T00:00:00Z",
        )
        closed_issue["user"] = {"login": "github-actions[bot]"}
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=open&creator=github-actions%5Bbot%5D&per_page=100": [
                open_issue
            ],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&creator=github-actions%5Bbot%5D&since={CUTOFF}&per_page=100": [
                closed_issue
            ],
            f"/repos/{REPOSITORY}/issues/22/comments": [],
            f"/repos/{REPOSITORY}/issues/23/comments": [],
        }

        result = Collector(
            ScriptedClient(pages=pages),
            REPOSITORY,
            NOW,
            bot_authors=("github-actions[bot]",),
        ).collect(
            include_supporting=True,
            include_timeline=False,
        )

        self.assertEqual([23], [issue["number"] for issue in result.supporting_issues])

    def collect(self, client, **kwargs):
        collector = Collector(client, REPOSITORY, NOW, **kwargs)
        return collector.collect()

    def test_minimal_run_budget_preserves_reused_runs_outside_cap(self) -> None:
        source_url = f"https://github.com/{REPOSITORY}/issues/1"
        references = [
            {
                "sourceIssueNumber": 1,
                "sourceEvidenceId": "issue:1",
                "sourceUrl": source_url,
                "targetType": "workflow-run",
                "targetRepository": REPOSITORY,
                "targetUrl": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
                "runId": run_id,
                "extractionMethod": "actions-run-url",
            }
            for run_id in range(1, 13)
        ]
        evidence = {
            f"run:{run_id}": {
                "kind": "workflow-run",
                "url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
                "collectedAt": NOW.isoformat().replace("+00:00", "Z"),
                "availability": "available",
                "payload": {
                    "runId": run_id,
                    "targetRepository": REPOSITORY,
                    "status": "completed",
                    "conclusion": "failure",
                    "referencedBy": [
                        {
                            "sourceIssueNumber": 1,
                            "sourceEvidenceId": "issue:1",
                            "sourceUrl": source_url,
                            "extractionMethod": "actions-run-url",
                        }
                    ],
                },
            }
            for run_id in range(1, 13)
        }
        inventory = InventoryResult(
            open_issues=[],
            supporting_issues=[],
            evidence=evidence,
            collection_errors=[],
            warnings=[],
            references={1: references},
            refresh_plan=RefreshPlan(
                reuse=tuple(f"run:{run_id}" for run_id in range(3, 13)),
                refresh=("run:1", "run:2"),
            ),
        )

        client = ScriptedClient()
        result = Collector(client, REPOSITORY, NOW).enrich_github_evidence(
            inventory,
            minimal_run_evidence=True,
        )

        self.assertEqual([], client.calls)
        self.assertEqual(
            {"available"},
            {result.evidence[f"run:{run_id}"]["availability"] for run_id in range(1, 13)},
        )
        self.assertFalse(
            any(
                result.evidence[f"run:{run_id}"]["payload"].get("runBudgetExcluded")
                for run_id in range(1, 13)
            )
        )
        self.assertEqual(("run:1", "run:2"), result.refresh_plan.retry)
        self.assertNotIn("run:1", result.refresh_plan.refresh)
        self.assertNotIn("run:2", result.refresh_plan.refresh)

    def test_issue_19149_retains_post_fix_success_run_with_many_issue_references(self) -> None:
        body = """\
<!-- ci-failure-cause:azurebicepresourcescope-tests-stale-compile-error -->

- **Failed build:** https://github.com/owner/repo/actions/runs/31203621605
- **Failed commit:** `5e7a15c05d91b1b8257f4ed620981b4e93e390c7`
- **Triggering merge:** #19090 was unrelated.

## Root Cause

#19084 and #18976 merged independently. See also #18001, #18002, #18003,
#18004, #18005, and #18006.

## Resolution

#19148 updated the stale call sites. The subsequent `main` run succeeded:
https://github.com/owner/repo/actions/runs/31211923676

## Occurrences

| Date | Build | Branch | Job | Triggering merge |
|---|---|---|---|---|
| 2026-08-07 | [31203621605](https://github.com/owner/repo/actions/runs/31203621605) | `main` | Tests / Hosting.Azure | #19090 |
"""
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(19149, labels=["ci-failure-cause"], body=body)
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/19149/comments": [],
        }

        result = self.collect(
            ScriptedClient(pages=pages),
            budgets={
                "max_run_refs_per_issue": 12,
                "max_issue_refs_per_issue": 5,
                "max_commit_refs_per_issue": 3,
            },
        )

        self.assertEqual(
            [31211923676, 31203621605],
            [
                reference["runId"]
                for reference in result.references[19149]
                if reference["targetType"] == "workflow-run"
            ],
        )
        self.assertIn(
            ("issue", 19148),
            [
                (reference["targetType"], reference.get("targetNumber"))
                for reference in result.references[19149]
            ],
        )
        excluded = result.evidence["issue:19149"]["payload"]["excludedReferences"]
        self.assertEqual(
            5,
            sum(reference["exclusionReason"] == "max_issue_refs_per_issue" for reference in excluded),
        )
        self.assertTrue(all("sourceEvidenceId" in reference for reference in excluded))
        validate_snapshot(snapshot_from_result(result))

    def test_collect_attaches_cause_producer_ledger_and_episode_completeness(self) -> None:
        body = """\
<!-- ci-failure-cause:compile-error -->
## Occurrences
| Date | Build | Job | PR |
|---|---|---|---|
| 2026-08-18 | [21](https://github.com/owner/repo/actions/runs/21) | Linux | #8 |
"""
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(21, labels=["ci-failure-cause"], body=body)
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/21/comments": [],
        }

        result = Collector(ScriptedClient(pages=pages), REPOSITORY, NOW).collect(
            include_supporting=False,
            include_timeline=False,
        )

        issue = result.open_issues[0]
        self.assertEqual("ci-failure-cause", issue["producer"])
        self.assertIsNone(issue["autoclose"])
        self.assertEqual("body-table", issue["ledger"]["source"])
        self.assertTrue(issue["ledger"]["complete"])
        self.assertFalse(issue["episodesComplete"])
        self.assertEqual(issue["ledger"], result.evidence["issue:21"]["payload"]["ledger"])

    def test_tracking_issue_uses_run_marker_comments_as_authoritative_ledger(self) -> None:
        body = """\
<!-- ci-failure:deployment-tests -->
<!-- autoclose:true -->
Deployment tests are failing.
"""
        comments = [
            {
                "id": comment_id,
                "html_url": f"https://github.com/{REPOSITORY}/issues/22#issuecomment-{comment_id}",
                "created_at": created_at,
                "updated_at": created_at,
                "user": {"login": "github-actions[bot]"},
                "body": f"Run failed.\n\n<!-- run:{run_id} -->",
            }
            for comment_id, run_id, created_at in (
                (2201, 1001, "2026-08-17T00:00:00Z"),
                (2202, 1002, "2026-08-18T00:00:00Z"),
            )
        ]
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [
                make_issue(22, labels=["automation-broken"], body=body)
            ],
            f"/repos/{REPOSITORY}/issues/22/comments": comments,
        }

        result = Collector(ScriptedClient(pages=pages), REPOSITORY, NOW).collect(
            include_supporting=False,
            include_timeline=False,
        )

        issue = result.open_issues[0]
        self.assertEqual("tracking-issue", issue["producer"])
        self.assertIs(issue["autoclose"], True)
        self.assertEqual(
            {
                "source": "run-comments",
                "schema": "tracking-comments-v1",
                "schemaRecognized": True,
                "sourceRecordCount": 2,
                "parsedRowCount": 2,
                "complete": True,
                "rows": [
                    {
                        "commentId": 2201,
                        "createdAt": "2026-08-17T00:00:00Z",
                        "runId": 1001,
                    },
                    {
                        "commentId": 2202,
                        "createdAt": "2026-08-18T00:00:00Z",
                        "runId": 1002,
                    },
                ],
            },
            issue["ledger"],
        )

    def test_failed_tracking_comment_collection_blocks_ledger_completeness(self) -> None:
        body = "<!-- ci-failure:deployment-tests -->\n<!-- autoclose:true -->"
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [
                make_issue(23, labels=["automation-broken"], body=body)
            ],
            f"/repos/{REPOSITORY}/issues/23/comments": RuntimeError("comments unavailable"),
        }

        result = Collector(ScriptedClient(pages=pages), REPOSITORY, NOW).collect(
            include_supporting=False,
            include_timeline=False,
        )

        issue = result.open_issues[0]
        self.assertEqual("tracking-issue", issue["producer"])
        self.assertFalse(issue["ledger"]["complete"])
        self.assertEqual(0, issue["ledger"]["sourceRecordCount"])

    def test_dashboard_issue_has_unknown_ledger_and_human_only_policy(self) -> None:
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [
                make_issue(
                    24,
                    labels=["automation-broken"],
                    body="Failing since 10h.\n\n_Filed from the CI Health dashboard._",
                )
            ],
            f"/repos/{REPOSITORY}/issues/24/comments": [],
        }

        result = Collector(ScriptedClient(pages=pages), REPOSITORY, NOW).collect(
            include_supporting=False,
            include_timeline=False,
        )

        issue = result.open_issues[0]
        self.assertEqual("ci-health-dashboard", issue["producer"])
        self.assertIsNone(issue["autoclose"])
        self.assertEqual("none", issue["ledger"]["source"])
        self.assertFalse(issue["ledger"]["complete"])

    def test_collect_open_only_skips_closed_inventory_and_explicit_issue_traversal(self) -> None:
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(21, labels=["ci-failure-cause"], body="Related to #401")
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/21/comments": [],
        }
        client = ScriptedClient(pages=pages)

        result = Collector(client, REPOSITORY, NOW).collect(
            include_supporting=False,
            include_timeline=False,
        )

        self.assertEqual([21], [issue["number"] for issue in result.open_issues])
        self.assertEqual([], result.supporting_issues)
        self.assertFalse(any("state=closed" in endpoint for _, endpoint in client.calls))
        self.assertFalse(any("/timeline" in endpoint for _, endpoint in client.calls))
        self.assertNotIn(("get", f"/repos/{REPOSITORY}/issues/401"), client.calls)

    def test_incremental_no_change_reuses_issue_and_refreshes_only_volatile_run_history(self) -> None:
        run_id = 99
        body = f"https://github.com/{REPOSITORY}/actions/runs/{run_id}"
        open_cause = (
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100"
        )
        open_automation = (
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100"
        )
        open_bot_scan_page_1 = (
            f"/repos/{REPOSITORY}/issues?state=open&sort=updated&direction=desc"
            f"&per_page=100&page=1"
        )
        comments = f"/repos/{REPOSITORY}/issues/1/comments"
        closed_cause = (
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause"
            f"&since={CUTOFF}&per_page=100"
        )
        closed_automation = (
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken"
            f"&since={CUTOFF}&per_page=100"
        )
        run = f"/repos/{REPOSITORY}/actions/runs/{run_id}"
        jobs = f"/repos/{REPOSITORY}/actions/runs/{run_id}/jobs?per_page=100"
        history_endpoint = (
            f"/repos/{REPOSITORY}/actions/workflows/7/runs?branch=main&per_page=10"
        )
        pages = {
            open_cause: [make_issue(1, labels=["ci-failure-cause"], body=body)],
            open_automation: [],
            closed_cause: [],
            closed_automation: [],
            comments: [],
            jobs: {"jobs": []},
        }
        singles = {
            run: {
                "id": run_id,
                "workflow_id": 7,
                "run_attempt": 1,
                "name": "CI",
                "event": "push",
                "head_branch": "main",
                "head_sha": "abc",
                "status": "completed",
                "conclusion": "failure",
                "created_at": "2026-08-10T00:00:00Z",
                "updated_at": "2026-08-10T01:00:00Z",
                "run_started_at": "2026-08-10T00:01:00Z",
                "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
            },
            history_endpoint: {
                "total_count": 1,
                "workflow_runs": [
                    {
                        "id": run_id,
                        "status": "completed",
                        "conclusion": "failure",
                        "created_at": "2026-08-10T00:00:00Z",
                        "updated_at": "2026-08-10T01:00:00Z",
                        "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
                    }
                ],
            },
        }
        first_client = ScriptedClient(pages=pages, singles=singles)
        first_collector = Collector(first_client, REPOSITORY, NOW)
        first = first_collector.collect(include_supporting=True, include_timeline=False)
        first = first_collector.enrich_github_evidence(
            first,
            minimal_run_evidence=True,
            include_run_history=True,
        )
        snapshot = {
            "schemaVersion": 1,
            "repository": REPOSITORY,
            "collectedAt": NOW.isoformat().replace("+00:00", "Z"),
            "openIssues": [1],
            "issues": first.open_issues,
            "supportingIssues": first.supporting_issues,
            "evidence": first.evidence,
            "collectionErrors": [],
            "warnings": [],
            "references": {"1": first.references[1]},
        }
        report = {
            "schemaVersion": 1,
            "repository": REPOSITORY,
            "decisions": [
                {
                    "issueNumber": 1,
                    "issueUrl": f"https://github.com/{REPOSITORY}/issues/1",
                    "issueKind": "incident",
                    "state": "observing",
                    "proposedAction": "wait",
                    "confidence": "high",
                    "summary": "Keep observing.",
                    "reasoning": "Current evidence does not justify action.",
                    "evidence": [{"id": "issue:1", "kind": "issue-event"}],
                    "contradictoryEvidence": [],
                    "missingEvidence": [],
                    "nextCondition": {
                        "type": "monitor",
                        "description": "Wait for another workflow run.",
                    },
                    "suggestedOwners": [],
                    "relatedIssues": [],
                    "changedSincePreviousRun": False,
                }
            ],
        }
        history_root = Path(__file__).parent / ".tmp"
        history_root.mkdir(parents=True, exist_ok=True)
        temporary_history = tempfile.TemporaryDirectory(dir=history_root)
        self.addCleanup(temporary_history.cleanup)
        current = record_history(
            Path(temporary_history.name) / "state",
            REPOSITORY,
            "run-001",
            snapshot,
            report,
        )

        second_history = copy.deepcopy(singles[history_endpoint])
        second_history["total_count"] = 2
        second_history["workflow_runs"].insert(
            0,
            {
                "id": 100,
                "status": "completed",
                "conclusion": "success",
                "created_at": "2026-08-11T00:00:00Z",
                "updated_at": "2026-08-11T01:00:00Z",
                "html_url": f"https://github.com/{REPOSITORY}/actions/runs/100",
            },
        )
        second_client = ScriptedClient(
            pages={open_cause: pages[open_cause], open_automation: []},
            singles={history_endpoint: second_history},
        )
        second_collector = Collector(second_client, REPOSITORY, NOW)

        second = second_collector.collect_incremental(
            snapshot,
            current.document,
            include_supporting=True,
            include_timeline=False,
        )
        second = second_collector.enrich_github_evidence(
            second,
            minimal_run_evidence=True,
            include_run_history=True,
        )

        self.assertEqual(
            [
                ("get_pages", open_cause),
                ("get_pages", open_automation),
                ("get", open_bot_scan_page_1),
                ("get", history_endpoint),
            ],
            second_client.calls,
        )
        self.assertEqual(first.open_issues, second.open_issues)
        self.assertEqual(
            first.evidence["issue:1"],
            second.evidence["issue:1"],
        )
        self.assertEqual(
            [100, 99],
            [item["runId"] for item in second.evidence["run:99"]["payload"]["recentHistory"]],
        )
        first_expensive_calls = [
            call
            for call in first_client.calls
            if call[1] not in {open_cause, open_automation, history_endpoint}
        ]
        second_expensive_calls = [
            call
            for call in second_client.calls
            if call[1] not in {open_cause, open_automation, history_endpoint}
        ]
        self.assertGreaterEqual(len(first_expensive_calls), 3)
        self.assertLessEqual(
            len(second_expensive_calls),
            len(first_expensive_calls) * 0.2,
        )

    def test_incremental_refreshes_changed_supporting_issue_and_its_run(self) -> None:
        open_cause = f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100"
        open_automation = f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100"
        closed_cause = f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100"
        closed_automation = f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100"
        root_comments = f"/repos/{REPOSITORY}/issues/1/comments"
        support_comments = f"/repos/{REPOSITORY}/issues/401/comments"
        support_detail = f"/repos/{REPOSITORY}/issues/401"
        run_id = 777
        run_endpoint = f"/repos/{REPOSITORY}/actions/runs/{run_id}"
        jobs_endpoint = f"{run_endpoint}/jobs?per_page=100"
        history_endpoint = f"/repos/{REPOSITORY}/actions/workflows/7/runs?branch=main&per_page=10"
        root = make_issue(
            1,
            labels=["ci-failure-cause"],
            body=f"Related to #{401}",
        )
        support_v1 = make_issue(
            401,
            state="closed",
            body=f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
            closed_at="2026-08-10T00:00:00Z",
        )
        pages = {
            open_cause: [root],
            open_automation: [],
            closed_cause: [support_v1],
            closed_automation: [],
            root_comments: [],
            support_comments: [],
            jobs_endpoint: {"jobs": []},
        }
        run_v1 = {
            "id": run_id,
            "workflow_id": 7,
            "run_attempt": 1,
            "name": "CI",
            "event": "push",
            "head_branch": "main",
            "head_sha": "abc",
            "status": "in_progress",
            "conclusion": None,
            "created_at": "2026-08-10T00:00:00Z",
            "updated_at": "2026-08-10T01:00:00Z",
            "run_started_at": "2026-08-10T00:01:00Z",
            "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
        }
        singles = {
            support_detail: support_v1,
            run_endpoint: run_v1,
            history_endpoint: {
                "total_count": 1,
                "workflow_runs": [run_v1],
            },
        }
        first_collector = Collector(ScriptedClient(pages=pages, singles=singles), REPOSITORY, NOW)
        first = first_collector.collect(include_supporting=True, include_timeline=False)
        first = first_collector.enrich_github_evidence(
            first,
            minimal_run_evidence=True,
            include_run_history=True,
        )
        snapshot = {
            "schemaVersion": 1,
            "repository": REPOSITORY,
            "collectedAt": NOW.isoformat().replace("+00:00", "Z"),
            "openIssues": [1],
            "issues": first.open_issues,
            "supportingIssues": first.supporting_issues,
            "evidence": first.evidence,
            "collectionErrors": [],
            "warnings": [],
            "references": {str(number): refs for number, refs in first.references.items()},
        }
        report = {
            "schemaVersion": 1,
            "repository": REPOSITORY,
            "decisions": [{
                "issueNumber": 1,
                "issueUrl": f"https://github.com/{REPOSITORY}/issues/1",
                "issueKind": "incident",
                "state": "observing",
                "proposedAction": "wait",
                "confidence": "high",
                "summary": "Keep observing.",
                "reasoning": "Current evidence does not justify action.",
                "evidence": [{"id": "issue:1", "kind": "issue-event"}],
                "contradictoryEvidence": [],
                "missingEvidence": [],
                "nextCondition": {"type": "monitor", "description": "Wait."},
                "suggestedOwners": [],
                "relatedIssues": [],
                "changedSincePreviousRun": False,
            }],
        }
        history_root = Path(__file__).parent / ".tmp"
        history_root.mkdir(parents=True, exist_ok=True)
        temporary_history = tempfile.TemporaryDirectory(dir=history_root)
        self.addCleanup(temporary_history.cleanup)
        current = record_history(
            Path(temporary_history.name) / "state",
            REPOSITORY,
            "run-support-001",
            snapshot,
            report,
        )

        support_v2 = copy.deepcopy(support_v1)
        support_v2["state"] = "open"
        support_v2["updated_at"] = "2026-08-18T00:00:00Z"
        run_v2 = copy.deepcopy(run_v1)
        run_v2["conclusion"] = "success"
        run_v2["updated_at"] = "2026-08-18T01:00:00Z"
        second_client = ScriptedClient(
            pages={
                open_cause: [root],
                open_automation: [],
                closed_cause: [support_v2],
                closed_automation: [],
                root_comments: [],
                support_comments: [],
                jobs_endpoint: {"jobs": []},
            },
            singles={
                support_detail: support_v2,
                run_endpoint: run_v2,
                history_endpoint: {"total_count": 1, "workflow_runs": [run_v2]},
            },
        )
        second_collector = Collector(second_client, REPOSITORY, NOW)

        second = second_collector.collect_incremental(
            snapshot,
            current.document,
            include_supporting=True,
            include_timeline=False,
        )
        second = second_collector.enrich_github_evidence(
            second,
            minimal_run_evidence=True,
            include_run_history=True,
        )

        self.assertEqual("open", second.evidence["issue:401"]["payload"]["state"])
        self.assertEqual("open", second.supporting_issues[0]["state"])
        self.assertEqual("success", second.evidence["run:777"]["payload"]["conclusion"])
        self.assertIn("issue:401", second.refresh_plan.refresh)
        self.assertIn("run:777", second.refresh_plan.refresh)
        self.assertNotIn("issue:401", second.refresh_plan.reuse)
        self.assertIn(("get", support_detail), second_client.calls)
        self.assertIn(("get", run_endpoint), second_client.calls)

    def test_incremental_replaces_supporting_comments_when_issue_refreshes(self) -> None:
        open_cause = f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100"
        open_automation = f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100"
        closed_cause = f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100"
        closed_automation = f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100"
        root_comments = f"/repos/{REPOSITORY}/issues/1/comments"
        second_root_comments = f"/repos/{REPOSITORY}/issues/2/comments"
        support_comments = f"/repos/{REPOSITORY}/issues/401/comments"
        support_detail = f"/repos/{REPOSITORY}/issues/401"
        marker = "<!-- ci-failure-cause:shared-timeout -->"
        root = make_issue(
            1,
            labels=["ci-failure-cause"],
            body=f"Related to #401\n{marker}",
        )
        second_root = make_issue(
            2,
            labels=["ci-failure-cause"],
            body=marker,
        )
        support = make_issue(
            401,
            state="closed",
            closed_at="2026-08-10T00:00:00Z",
            body=marker,
        )

        def comment(comment_id: int, body: str, updated_at: str) -> dict[str, object]:
            return {
                "id": comment_id,
                "html_url": (
                    f"https://github.com/{REPOSITORY}/issues/401"
                    f"#issuecomment-{comment_id}"
                ),
                "created_at": "2026-08-10T01:00:00Z",
                "updated_at": updated_at,
                "user": {"login": "octocat"},
                "body": body,
            }

        first_client = ScriptedClient(
            pages={
                open_cause: [root, second_root],
                open_automation: [],
                closed_cause: [support],
                closed_automation: [],
                root_comments: [],
                second_root_comments: [],
                support_comments: [
                    comment(9001, "stale supporting detail", "2026-08-10T01:00:00Z"),
                    comment(9002, "deleted supporting detail", "2026-08-10T02:00:00Z"),
                ],
            },
            singles={support_detail: support},
        )
        first_collector = Collector(first_client, REPOSITORY, NOW)
        first = first_collector.collect(include_supporting=True, include_timeline=False)
        snapshot = {
            "schemaVersion": 1,
            "repository": REPOSITORY,
            "collectedAt": NOW.isoformat().replace("+00:00", "Z"),
            "openIssues": [1, 2],
            "issues": first.open_issues,
            "supportingIssues": first.supporting_issues,
            "evidence": first.evidence,
            "collectionErrors": [],
            "warnings": [],
            "references": {str(number): refs for number, refs in first.references.items()},
        }
        report = {
            "schemaVersion": 1,
            "repository": REPOSITORY,
            "decisions": [{
                "issueNumber": 1,
                "issueUrl": f"https://github.com/{REPOSITORY}/issues/1",
                "issueKind": "incident",
                "state": "observing",
                "proposedAction": "wait",
                "confidence": "high",
                "summary": "Keep observing.",
                "reasoning": "Current evidence does not justify action.",
                "evidence": [{"id": "issue:1", "kind": "issue-event"}],
                "contradictoryEvidence": [],
                "missingEvidence": [],
                "nextCondition": {"type": "monitor", "description": "Wait."},
                "suggestedOwners": [],
                "relatedIssues": [],
                "changedSincePreviousRun": False,
            }],
        }
        second_decision = copy.deepcopy(report["decisions"][0])
        second_decision.update(
            {
                "issueNumber": 2,
                "issueUrl": f"https://github.com/{REPOSITORY}/issues/2",
            }
        )
        report["decisions"].append(second_decision)
        history_root = Path(__file__).parent / ".tmp"
        history_root.mkdir(parents=True, exist_ok=True)
        temporary_history = tempfile.TemporaryDirectory(dir=history_root)
        self.addCleanup(temporary_history.cleanup)
        current = record_history(
            Path(temporary_history.name) / "state",
            REPOSITORY,
            "run-support-comment-001",
            snapshot,
            report,
        )
        second_history = current.document

        second_client = ScriptedClient(
            pages={
                open_cause: [root, second_root],
                open_automation: [],
                support_comments: [
                    comment(9001, "fresh supporting detail", "2026-08-18T01:00:00Z"),
                    comment(9003, "new supporting detail", "2026-08-18T02:00:00Z"),
                ],
            },
            singles={support_detail: support},
        )
        second_collector = Collector(second_client, REPOSITORY, NOW)
        second = second_collector.collect_incremental(
            snapshot,
            second_history,
            include_supporting=True,
            include_timeline=False,
        )
        second = second_collector.enrich_github_evidence(second)
        completed = complete_refresh_plan(second.refresh_plan, second.evidence)
        edited_comment_id = "issue:401:comment:9001"
        deleted_comment_id = "issue:401:comment:9002"
        added_comment_id = "issue:401:comment:9003"

        self.assertIn("issue:401", second.refresh_plan.refresh)
        self.assertIn(edited_comment_id, second.refresh_plan.reuse)
        self.assertIn(deleted_comment_id, second.refresh_plan.reuse)
        self.assertEqual(
            "fresh supporting detail",
            second.evidence[edited_comment_id]["payload"]["body"],
        )
        self.assertEqual(
            "new supporting detail",
            second.evidence[added_comment_id]["payload"]["body"],
        )
        self.assertNotIn(deleted_comment_id, second.evidence)
        self.assertNotIn("stale supporting detail", repr(second.evidence))
        self.assertNotIn("deleted supporting detail", repr(second.evidence))
        self.assertNotIn("stale supporting detail", repr(second.supporting_issues))
        self.assertNotIn("deleted supporting detail", repr(second.supporting_issues))
        self.assertIn(added_comment_id, completed.refresh)
        self.assertIn(deleted_comment_id, completed.retry)
        self.assertIn(("get_pages", support_comments), second_client.calls)

        failed_client = ScriptedClient(
            pages={
                open_cause: [root, second_root],
                open_automation: [],
                support_comments: RuntimeError("comments unavailable"),
            },
            singles={support_detail: support},
        )
        failed_collector = Collector(failed_client, REPOSITORY, NOW)
        failed = failed_collector.collect_incremental(
            snapshot,
            second_history,
            include_supporting=True,
            include_timeline=False,
        )
        failed = failed_collector.enrich_github_evidence(failed)
        failed_completion = complete_refresh_plan(failed.refresh_plan, failed.evidence)

        self.assertNotIn(edited_comment_id, failed.evidence)
        self.assertNotIn(deleted_comment_id, failed.evidence)
        self.assertNotIn("stale supporting detail", repr(failed.supporting_issues))
        self.assertNotIn("deleted supporting detail", repr(failed.supporting_issues))
        self.assertNotIn(edited_comment_id, failed_completion.refresh)
        self.assertIn(edited_comment_id, failed_completion.retry)
        self.assertIn(deleted_comment_id, failed_completion.retry)
        self.assertEqual(
            {"kind": "issue", "issueNumbers": [1, 2]},
            failed.collection_errors[0].scope,
        )

    def test_incremental_reuses_foreign_commit_and_merged_pull_request(self) -> None:
        foreign_repository = "other/repo"
        foreign_sha = "a" * 40
        root_body = (
            f"https://github.com/{foreign_repository}/commit/{foreign_sha} "
            f"https://github.com/{foreign_repository}/pull/7"
        )
        open_cause = f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100"
        open_automation = f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100"
        closed_cause = f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100"
        closed_automation = f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100"
        comments = f"/repos/{REPOSITORY}/issues/1/comments"
        commit_endpoint = f"/repos/{foreign_repository}/commits/{foreign_sha}"
        pull_issue_endpoint = f"/repos/{foreign_repository}/issues/7"
        pull_endpoint = f"/repos/{foreign_repository}/pulls/7"
        pull_files_endpoint = f"/repos/{foreign_repository}/pulls/7/files?per_page=100"
        root = make_issue(1, labels=["ci-failure-cause"], body=root_body)
        pages = {
            open_cause: [root],
            open_automation: [],
            closed_cause: [],
            closed_automation: [],
            comments: [],
            pull_files_endpoint: [],
        }
        singles = {
            commit_endpoint: {
                "sha": foreign_sha,
                "html_url": f"https://github.com/{foreign_repository}/commit/{foreign_sha}",
                "commit": {"author": {}, "message": "Fix"},
                "files": [],
            },
            pull_issue_endpoint: {
                "number": 7,
                "state": "closed",
                "html_url": f"https://github.com/{foreign_repository}/pull/7",
                "pull_request": {
                    "url": f"https://api.github.com/repos/{foreign_repository}/pulls/7"
                },
            },
            pull_endpoint: {
                "number": 7,
                "state": "closed",
                "merged_at": "2026-08-10T00:00:00Z",
                "merge_commit_sha": "def456",
                "html_url": f"https://github.com/{foreign_repository}/pull/7",
                "base": {},
                "head": {"repo": {"full_name": foreign_repository}},
            },
        }
        first_client = ScriptedClient(pages=pages, singles=singles)
        first_collector = Collector(first_client, REPOSITORY, NOW)
        first = first_collector.collect(include_supporting=True, include_timeline=False)
        first = first_collector.enrich_github_evidence(first)
        snapshot = {
            "schemaVersion": 1,
            "repository": REPOSITORY,
            "collectedAt": NOW.isoformat().replace("+00:00", "Z"),
            "openIssues": [1],
            "issues": first.open_issues,
            "supportingIssues": [],
            "evidence": first.evidence,
            "collectionErrors": [],
            "warnings": [],
            "references": {"1": first.references[1]},
        }
        report = {
            "schemaVersion": 1,
            "repository": REPOSITORY,
            "decisions": [{
                "issueNumber": 1,
                "issueUrl": f"https://github.com/{REPOSITORY}/issues/1",
                "issueKind": "incident",
                "state": "observing",
                "proposedAction": "wait",
                "confidence": "high",
                "summary": "Keep observing.",
                "reasoning": "Current evidence does not justify action.",
                "evidence": [{"id": "issue:1", "kind": "issue-event"}],
                "contradictoryEvidence": [],
                "missingEvidence": [],
                "nextCondition": {"type": "monitor", "description": "Wait."},
                "suggestedOwners": [],
                "relatedIssues": [],
                "changedSincePreviousRun": False,
            }],
        }
        history_root = Path(__file__).parent / ".tmp"
        history_root.mkdir(parents=True, exist_ok=True)
        temporary_history = tempfile.TemporaryDirectory(dir=history_root)
        self.addCleanup(temporary_history.cleanup)
        current = record_history(
            Path(temporary_history.name) / "state",
            REPOSITORY,
            "run-foreign-001",
            snapshot,
            report,
        )
        second_client = ScriptedClient(
            pages={open_cause: [root], open_automation: []},
        )
        second_collector = Collector(second_client, REPOSITORY, NOW)
        foreign_commit_id = f"commit:{foreign_repository}:{foreign_sha}"
        foreign_pull_id = f"pr:{foreign_repository}:7"

        second = second_collector.collect_incremental(
            snapshot,
            current.document,
            include_supporting=False,
            include_timeline=False,
        )
        self.assertIn(foreign_commit_id, second.refresh_plan.reuse, second.refresh_plan)
        self.assertIn(foreign_pull_id, second.refresh_plan.reuse, second.refresh_plan)
        second = second_collector.enrich_github_evidence(second)

        self.assertEqual(first.evidence[foreign_commit_id], second.evidence[foreign_commit_id])
        self.assertEqual(first.evidence[foreign_pull_id], second.evidence[foreign_pull_id])
        self.assertIn(foreign_commit_id, second.refresh_plan.reuse)
        self.assertIn(foreign_pull_id, second.refresh_plan.reuse)
        first_expensive = [call for call in first_client.calls if call[1] not in {open_cause, open_automation}]
        second_expensive = [call for call in second_client.calls if call[1] not in {open_cause, open_automation}]
        self.assertGreaterEqual(len(first_expensive), 4)
        self.assertLessEqual(len(second_expensive), len(first_expensive) * 0.2)

    def test_incremental_refreshes_inferred_support_without_explicit_reference_row(self) -> None:
        target_number = 401
        endpoint = f"/repos/{REPOSITORY}/issues/{target_number}"
        comments_endpoint = f"{endpoint}/comments"
        client = ScriptedClient(
            pages={comments_endpoint: []},
            singles={
                endpoint: make_issue(
                    target_number,
                    state="open",
                    updated_at="2026-08-18T00:00:00Z",
                )
            }
        )
        referenced_by = [{
            "sourceIssueNumber": 1,
            "sourceEvidenceId": "issue:1",
            "sourceUrl": f"https://github.com/{REPOSITORY}/issues/1",
            "extractionMethod": "fact-match",
        }]
        inventory = Collector(client, REPOSITORY, NOW)
        prior = inventory._make_evidence_record(
            "issue-event",
            f"https://github.com/{REPOSITORY}/issues/{target_number}",
            {
                "number": target_number,
                "state": "closed",
                "updatedAt": "2026-08-10T00:00:00Z",
                "referencedBy": referenced_by,
            },
        )
        result = inventory.enrich_github_evidence(
            InventoryResult(
                open_issues=[{"number": 1}],
                supporting_issues=[{
                    "number": target_number,
                    "state": "closed",
                    "updatedAt": "2026-08-10T00:00:00Z",
                }],
                evidence={f"issue:{target_number}": prior},
                collection_errors=[],
                warnings=[],
                references={},
                refresh_plan=RefreshPlan(refresh=(f"issue:{target_number}",)),
            )
        )

        self.assertEqual(
            [("get_pages", comments_endpoint), ("get", endpoint)],
            client.calls,
        )
        self.assertEqual("open", result.evidence[f"issue:{target_number}"]["payload"]["state"])
        self.assertEqual("open", result.supporting_issues[0]["state"])

    def test_collect_delegates_actual_issue_signals_and_exposes_occurrences(self) -> None:
        body = """\
<!-- ci-failure-cause:testing-timeout -->
Build: https://github.com/owner/repo/actions/runs/29787895542
Build error leg or test failing: Tests / Hosting / Hosting (ubuntu-latest) / `Aspire.Hosting.Tests.ExampleTests.TimesOut`
Pull request: #18835
## Error Message
```
System.TimeoutException : Timed out.
```
**Type**: flaky-test
## Occurrences
| Date | Build | Job | PR |
|------|-------|-----|----|
| 2026-08-18 | [run #29787895542](https://github.com/owner/repo/actions/runs/29787895542) | Tests / Hosting / Hosting (ubuntu-latest) | #18835 |
"""
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(21, labels=["ci-failure-cause"], body=body)
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/21/comments": [],
        }

        result = Collector(ScriptedClient(pages=pages), REPOSITORY, NOW).collect(
            include_supporting=False,
            include_timeline=False,
        )

        issue = result.open_issues[0]
        self.assertEqual(
            [
                {
                    "date": "2026-08-18",
                    "sourceRun": 29787895542,
                    "runUrl": "https://github.com/owner/repo/actions/runs/29787895542",
                    "job": "Tests / Hosting / Hosting (ubuntu-latest)",
                    "pullRequest": 18835,
                }
            ],
            issue["occurrences"],
        )
        self.assertEqual(
            issue["occurrences"],
            result.evidence["issue:21"]["payload"]["occurrences"],
        )
        self.assertEqual(
            [("workflow-run", 29787895542), ("pull-request", 18835)],
            [
                (
                    reference["targetType"],
                    reference.get("targetNumber", reference.get("runId")),
                )
                for reference in result.references[21]
            ],
        )
        self.assertNotIn(
            ("issue", 18835),
            [
                (reference["targetType"], reference.get("targetNumber"))
                for reference in result.references[21]
            ],
        )

    def test_supporting_search_records_completed_zero_match_on_issue_and_evidence(self) -> None:
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(21, labels=["ci-failure-cause"])
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/21/comments": [],
        }
        result = Collector(ScriptedClient(pages=pages), REPOSITORY, NOW).collect(
            include_timeline=False,
        )
        expected = {
            "complete": True,
            "candidateIssueNumbers": [],
            "truncated": False,
        }

        self.assertEqual(expected, result.open_issues[0].get("supportingSearch"))
        self.assertEqual(expected, result.evidence["issue:21"]["payload"].get("supportingSearch"))

    def test_supporting_search_is_incomplete_when_closed_inventory_fails(self) -> None:
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(21, labels=["ci-failure-cause"])
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": RuntimeError(
                "closed query failed"
            ),
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/21/comments": [],
        }
        result = Collector(ScriptedClient(pages=pages), REPOSITORY, NOW).collect(
            include_timeline=False,
        )

        self.assertEqual(
            {
                "complete": False,
                "candidateIssueNumbers": [],
                "truncated": False,
            },
            result.open_issues[0].get("supportingSearch"),
        )

    def test_issue_comment_and_timeline_evidence_record_source_issue_number(self) -> None:
        result = self.collect(FixtureClient(FIXTURE_ROOT))

        self.assertEqual(
            101,
            result.evidence["issue:101:comment:1001"]["payload"].get("sourceIssueNumber"),
        )
        self.assertEqual(
            101,
            result.evidence["issue:101:event:5001"]["payload"].get("sourceIssueNumber"),
        )

    def test_collect_caps_supporting_candidates_before_fetching_closed_issue_details(self) -> None:
        full_sha = "0123456789abcdef0123456789abcdef01234567"
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(
                    21,
                    labels=["ci-failure-cause"],
                    body=(
                        "#401\n"
                        "Test name: Shared.Flake\n"
                        "See https://github.com/owner/repo/pull/77\n"
                        "See https://github.com/owner/repo/actions/runs/6666\n"
                        f"Commit: {full_sha}\n"
                    ),
                )
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [
                make_issue(401, state="closed", closed_at="2026-08-01T00:00:00Z", labels=["ci-failure-cause"], body="Test name: Shared.Flake"),
                make_issue(402, state="closed", closed_at="2026-08-02T00:00:00Z", labels=["ci-failure-cause"], body="Test name: Shared.Flake"),
                make_issue(403, state="closed", closed_at="2026-08-03T00:00:00Z", labels=["ci-failure-cause"], body="Test name: Shared.Flake"),
            ],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/21/comments": [],
            f"/repos/{REPOSITORY}/issues/21/timeline": [],
            f"/repos/{REPOSITORY}/issues/401/comments": [],
            f"/repos/{REPOSITORY}/issues/401/timeline": [],
            f"/repos/{REPOSITORY}/issues/402/comments": [],
            f"/repos/{REPOSITORY}/issues/402/timeline": [],
            f"/repos/{REPOSITORY}/issues/403/comments": [],
            f"/repos/{REPOSITORY}/issues/403/timeline": [],
        }
        client = ScriptedClient(pages=pages)

        result = self.collect(client, budgets={"max_supporting_closed": 2})

        self.assertEqual([401, 403], [issue["number"] for issue in result.supporting_issues])
        self.assertNotIn(("get_pages", f"/repos/{REPOSITORY}/issues/402/comments"), client.calls)
        self.assertNotIn(("get_pages", f"/repos/{REPOSITORY}/issues/402/timeline"), client.calls)
        self.assertIn("discarded 1", "\n".join(result.warnings))

    def test_collect_caps_explicit_issue_detail_probes_at_global_supporting_budget(self) -> None:
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(21, labels=["ci-failure-cause"], body="#301\n#302"),
                make_issue(22, labels=["ci-failure-cause"], body="#303\n#304"),
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/21/comments": [],
            f"/repos/{REPOSITORY}/issues/21/timeline": [],
            f"/repos/{REPOSITORY}/issues/22/comments": [],
            f"/repos/{REPOSITORY}/issues/22/timeline": [],
            **{
                f"/repos/{REPOSITORY}/issues/{number}/comments": []
                for number in range(301, 305)
            },
            **{
                f"/repos/{REPOSITORY}/issues/{number}/timeline": []
                for number in range(301, 305)
            },
        }
        singles = {
            f"/repos/{REPOSITORY}/issues/{number}": make_issue(
                number,
                state="closed",
                closed_at=f"2026-01-{number - 300:02d}T00:00:00Z",
            )
            for number in range(301, 305)
        }
        client = ScriptedClient(pages=pages, singles=singles)
        collector = Collector(
            client,
            REPOSITORY,
            NOW,
            budgets={"max_supporting_closed": 2, "max_issue_refs_per_issue": 2},
        )

        result = collector.collect()
        enriched = collector.enrich_github_evidence(result)

        detail_calls = [
            endpoint
            for method, endpoint in client.calls
            if method == "get" and "/issues/" in endpoint
        ]
        self.assertEqual(
            [
                f"/repos/{REPOSITORY}/issues/301",
                f"/repos/{REPOSITORY}/issues/302",
            ],
            detail_calls,
        )
        self.assertIn(
            "max_supporting_closed budget truncated 2 explicit issue detail candidate(s)",
            "\n".join(result.warnings),
        )
        self.assertEqual(
            {
                "complete": False,
                "candidateIssueNumbers": [],
                "truncated": True,
                "candidateDispositions": [
                    {
                        "issueNumber": candidate_number,
                        "disposition": "excluded-budget",
                        "provenance": [
                            {
                                "sourceEvidenceId": "issue:22",
                                "sourceUrl": f"https://github.com/{REPOSITORY}/issues/22",
                                "extractionMethod": "local-issue",
                            }
                        ],
                    }
                    for candidate_number in (303, 304)
                ],
            },
            result.open_issues[1]["supportingSearch"],
        )
        self.assertEqual(
            "not-enriched",
            enriched.evidence["issue:303"]["availability"],
        )

    def test_collect_retains_stub_evidence_for_discarded_explicit_refs_without_enriching_them(self) -> None:
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(
                    21,
                    labels=["ci-failure-cause"],
                    body="#401\n#402\n#403",
                )
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [
                make_issue(401, state="closed", closed_at="2026-08-01T00:00:00Z", labels=["ci-failure-cause"]),
                make_issue(402, state="closed", closed_at="2026-08-02T00:00:00Z", labels=["ci-failure-cause"]),
                make_issue(403, state="closed", closed_at="2026-08-03T00:00:00Z", labels=["ci-failure-cause"]),
            ],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/21/comments": [],
            f"/repos/{REPOSITORY}/issues/21/timeline": [],
            f"/repos/{REPOSITORY}/issues/401/comments": [],
            f"/repos/{REPOSITORY}/issues/401/timeline": [],
            f"/repos/{REPOSITORY}/issues/402/comments": [],
            f"/repos/{REPOSITORY}/issues/402/timeline": [],
            f"/repos/{REPOSITORY}/issues/403/comments": [],
            f"/repos/{REPOSITORY}/issues/403/timeline": [],
        }
        client = ScriptedClient(pages=pages)

        result = self.collect(client, budgets={"max_supporting_closed": 2, "max_issue_refs_per_issue": 5})

        self.assertEqual([402, 403], [issue["number"] for issue in result.supporting_issues])
        self.assertEqual([402, 403], result.open_issues[0]["supportingIssueNumbers"])
        self.assertEqual([401, 402, 403], [reference["targetNumber"] for reference in result.references[21]])
        for reference in result.references[21]:
            self.assertIn(f"issue:{reference['targetNumber']}", result.evidence)

        self.assertNotIn(("get_pages", f"/repos/{REPOSITORY}/issues/401/comments"), client.calls)
        self.assertNotIn(("get_pages", f"/repos/{REPOSITORY}/issues/401/timeline"), client.calls)
        self.assertIn(("get_pages", f"/repos/{REPOSITORY}/issues/402/comments"), client.calls)
        self.assertIn(("get_pages", f"/repos/{REPOSITORY}/issues/402/timeline"), client.calls)
        self.assertIn(("get_pages", f"/repos/{REPOSITORY}/issues/403/comments"), client.calls)
        self.assertIn(("get_pages", f"/repos/{REPOSITORY}/issues/403/timeline"), client.calls)

        excluded_record = result.evidence["issue:401"]
        self.assertEqual("not-enriched", excluded_record["availability"])
        self.assertTrue(excluded_record["payload"]["supportingBudgetExcluded"])
        self.assertEqual(401, excluded_record["payload"]["number"])
        validate_snapshot(snapshot_from_result(result))

    def test_collect_filters_evidence_and_reference_payloads_to_retained_refs(self) -> None:
        full_sha = "0123456789abcdef0123456789abcdef01234567"
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(
                    21,
                    labels=["ci-failure-cause"],
                    body=(
                        "#401\n"
                        "Test name: Shared.Flake\n"
                        "See https://github.com/owner/repo/pull/77\n"
                        "See https://github.com/owner/repo/actions/runs/6666\n"
                        f"Commit: {full_sha}\n"
                    ),
                )
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [
                make_issue(401, state="closed", closed_at="2026-08-01T00:00:00Z", labels=["ci-failure-cause"], body="Test name: Shared.Flake"),
                make_issue(402, state="closed", closed_at="2026-08-02T00:00:00Z", labels=["ci-failure-cause"], body="Test name: Shared.Flake"),
                make_issue(403, state="closed", closed_at="2026-08-03T00:00:00Z", labels=["ci-failure-cause"], body="Test name: Shared.Flake"),
            ],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/21/comments": [
                {
                    "id": 2101,
                    "html_url": f"https://github.com/{REPOSITORY}/issues/21#issuecomment-2101",
                    "created_at": "2026-08-04T00:00:00Z",
                    "updated_at": "2026-08-04T00:00:00Z",
                    "user": {"login": "octocat"},
                    "body": "See https://github.com/owner/repo/actions/runs/7777",
                }
            ],
            f"/repos/{REPOSITORY}/issues/21/timeline": [],
            f"/repos/{REPOSITORY}/issues/401/comments": [],
            f"/repos/{REPOSITORY}/issues/401/timeline": [],
            f"/repos/{REPOSITORY}/issues/402/comments": [],
            f"/repos/{REPOSITORY}/issues/402/timeline": [],
            f"/repos/{REPOSITORY}/issues/403/comments": [],
            f"/repos/{REPOSITORY}/issues/403/timeline": [],
        }
        client = ScriptedClient(pages=pages)

        result = self.collect(
            client,
            budgets={
                "max_supporting_closed": 2,
                "max_run_refs_per_issue": 1,
                "max_issue_refs_per_issue": 2,
                "max_commit_refs_per_issue": 1,
            },
        )

        retained_targets = {
            (reference["targetType"], reference.get("targetNumber", reference.get("runId", reference.get("sha"))))
            for reference in result.references[21]
        }
        self.assertEqual(
            {
                ("commit", full_sha),
                ("issue", 401),
                ("pull-request", 77),
                ("workflow-run", 6666),
            },
            retained_targets,
        )
        self.assertNotIn(("workflow-run", 7777), retained_targets)
        self.assertNotIn(402, result.references)

        issue_payload_refs = result.evidence["issue:21"]["payload"]["references"]
        self.assertEqual(result.references[21], issue_payload_refs)
        self.assertEqual([], result.evidence["issue:21:comment:2101"]["payload"]["references"])
        self.assertNotIn("issue:402", result.evidence)
        self.assertFalse(any(evidence_id.startswith("issue:402:") for evidence_id in result.evidence))
        self.assertIn("pr:77", result.evidence)
        self.assertIn("run:6666", result.evidence)
        self.assertIn(f"commit:{full_sha}", result.evidence)
        self.assertNotIn("run:7777", result.evidence)
        self.assertIn("discarded 1", "\n".join(result.warnings))
        self.assertIn("max_run_refs_per_issue", "\n".join(result.warnings))

    def test_collect_unions_dedupes_and_sorts_open_issues_across_labels(self) -> None:
        result = self.collect(FixtureClient(FIXTURE_ROOT))

        self.assertEqual([101, 102], [issue["number"] for issue in result.open_issues])
        self.assertEqual([140, 150, 201], [issue["number"] for issue in result.supporting_issues])
        self.assertEqual(["automation-broken", "ci-failure-cause"], result.open_issues[0]["labels"])
        self.assertEqual([101, 102, 140, 150], sorted(result.references))

    def test_collect_raises_inventory_error_when_an_open_label_query_fails(self) -> None:
        client = ScriptedClient(
            pages={
                f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": RuntimeError("boom"),
                f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
                f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [],
                f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
            }
        )

        with self.assertRaisesRegex(InventoryError, "ci-failure-cause"):
            self.collect(client)

    def test_collect_filters_closed_issues_by_closed_at_cutoff(self) -> None:
        result = self.collect(FixtureClient(FIXTURE_ROOT))

        self.assertNotIn(202, [issue["number"] for issue in result.supporting_issues])

    def test_collect_orders_comments_chronologically_and_builds_reopen_episodes(self) -> None:
        result = self.collect(FixtureClient(FIXTURE_ROOT))
        issue = result.open_issues[0]

        self.assertEqual([1001, 1002], [comment["id"] for comment in issue["comments"]])
        self.assertEqual(
            [
                {"openedAt": "2026-06-01T00:00:00Z", "closedAt": "2026-07-01T00:00:00Z"},
                {"openedAt": "2026-07-03T00:00:00Z", "closedAt": "2026-07-05T00:00:00Z"},
                {"openedAt": "2026-07-06T00:00:00Z", "closedAt": None},
            ],
            issue["episodes"],
        )

    def test_normalize_timeline_uses_issue_closed_at_for_missing_final_close_event(self) -> None:
        collector = Collector(ScriptedClient(), REPOSITORY, NOW)

        episodes = collector.normalize_timeline(
            401,
            "closed",
            "2026-06-01T00:00:00Z",
            [
                {"id": 5001, "event": "closed", "created_at": "2026-07-01T00:00:00Z"},
                {"id": 5002, "event": "reopened", "created_at": "2026-07-03T00:00:00Z"},
            ],
            "2026-07-05T00:00:00Z",
        )

        self.assertEqual(
            [
                {"openedAt": "2026-06-01T00:00:00Z", "closedAt": "2026-07-01T00:00:00Z"},
                {"openedAt": "2026-07-03T00:00:00Z", "closedAt": "2026-07-05T00:00:00Z"},
            ],
            episodes,
        )
        self.assertEqual(
            ["issue 401 missing-close-event warning; using issue.closed_at 2026-07-05T00:00:00Z"],
            collector._warnings,
        )

        closed_at_missing = Collector(ScriptedClient(), REPOSITORY, NOW)
        missing_episodes = closed_at_missing.normalize_timeline(
            402,
            "closed",
            "2026-06-01T00:00:00Z",
            [
                {"id": 5003, "event": "closed", "created_at": "2026-07-01T00:00:00Z"},
                {"id": 5004, "event": "reopened", "created_at": "2026-07-03T00:00:00Z"},
            ],
            "not-a-timestamp",
        )

        self.assertEqual(
            [
                {"openedAt": "2026-06-01T00:00:00Z", "closedAt": "2026-07-01T00:00:00Z"},
                {"openedAt": "2026-07-03T00:00:00Z", "closedAt": None},
            ],
            missing_episodes,
        )
        self.assertEqual(
            ["issue 402 missing-close-event warning; issue.closed_at missing or invalid"],
            closed_at_missing._warnings,
        )

    def test_collect_warns_for_duplicate_close_and_reopen_events(self) -> None:
        result = self.collect(FixtureClient(FIXTURE_ROOT))

        warning_text = "\n".join(result.warnings)
        self.assertIn("duplicate close", warning_text)
        self.assertIn("duplicate reopen", warning_text)

    def test_collect_extracts_reference_types_and_ignores_unlabelled_sha_fragments(self) -> None:
        result = self.collect(FixtureClient(FIXTURE_ROOT))
        refs = result.references[102]

        target_types = [
            (
                reference["targetType"],
                reference.get("targetNumber", reference.get("runId", reference.get("sha"))),
            )
            for reference in refs
        ]
        self.assertIn(("pull-request", 77), target_types)
        self.assertIn(("workflow-run", 6666), target_types)
        self.assertIn(("commit", "abcdef1234567"), target_types)
        self.assertNotIn(("commit", "deadbeef"), target_types)
        self.assertIn("full-pull-url", {reference["extractionMethod"] for reference in refs})

    def test_collect_retains_old_explicit_issue_refs_but_stops_at_depth_two(self) -> None:
        client = FixtureClient(FIXTURE_ROOT)
        result = self.collect(client)

        self.assertIn(150, [issue["number"] for issue in result.supporting_issues])
        self.assertIn(140, [issue["number"] for issue in result.supporting_issues])
        self.assertNotIn(130, [issue["number"] for issue in result.supporting_issues])
        self.assertNotIn(("get", f"/repos/{REPOSITORY}/issues/130"), client.calls)

    def test_depth_excluded_reference_is_not_enriched_after_supporting_traversal(self) -> None:
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(21, labels=["ci-failure-cause"], body="#401")
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [
                make_issue(
                    401,
                    state="closed",
                    closed_at="2026-08-01T00:00:00Z",
                    labels=["ci-failure-cause"],
                    body="#402",
                ),
                make_issue(
                    402,
                    state="closed",
                    closed_at="2026-08-02T00:00:00Z",
                    labels=["ci-failure-cause"],
                    body="#403",
                ),
                make_issue(
                    403,
                    state="closed",
                    closed_at="2026-08-03T00:00:00Z",
                    labels=["ci-failure-cause"],
                ),
            ],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/21/comments": [],
            f"/repos/{REPOSITORY}/issues/401/comments": [],
            f"/repos/{REPOSITORY}/issues/402/comments": [],
        }
        singles = {
            f"/repos/{REPOSITORY}/issues/403": make_issue(
                403,
                state="closed",
                closed_at="2026-08-03T00:00:00Z",
                labels=["ci-failure-cause"],
            )
        }
        client = ScriptedClient(pages=pages, singles=singles)
        collector = Collector(client, REPOSITORY, NOW)

        result = collector.collect(include_timeline=False)
        enriched = collector.enrich_github_evidence(result)

        self.assertEqual([401, 402], result.open_issues[0]["supportingIssueNumbers"])
        self.assertFalse(any(method == "get" and "/issues/" in endpoint for method, endpoint in client.calls))
        self.assertEqual(
            {
                "complete": False,
                "candidateIssueNumbers": [401, 402],
                "truncated": True,
                "candidateDispositions": [
                    {
                        "issueNumber": 403,
                        "disposition": "excluded-depth",
                        "provenance": [
                            {
                                "sourceEvidenceId": "issue:402",
                                "sourceUrl": f"https://github.com/{REPOSITORY}/issues/402",
                                "extractionMethod": "local-issue",
                            }
                        ],
                    }
                ],
            },
            result.open_issues[0]["supportingSearch"],
        )
        self.assertEqual(
            {
                "state": "excluded",
                "reasons": ["depth-limit"],
                "rootIssueNumbers": [21],
            },
            result.references[402][0]["supportingSelection"],
        )
        self.assertEqual("not-enriched", enriched.evidence["issue:403"]["availability"])
        self.assertTrue(enriched.evidence["issue:403"]["payload"]["supportingDepthExcluded"])
        self.assertEqual(
            [
                {
                    "sourceIssueNumber": 21,
                    "sourceEvidenceId": "issue:402",
                    "sourceUrl": f"https://github.com/{REPOSITORY}/issues/402",
                    "extractionMethod": "local-issue",
                }
            ],
            enriched.evidence["issue:403"]["payload"]["referencedBy"],
        )

    def test_mixed_roots_keep_depth_exclusion_out_of_available_supporting_evidence(self) -> None:
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(21, labels=["ci-failure-cause"], body="#401"),
                make_issue(22, labels=["ci-failure-cause"], body="#403"),
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [
                make_issue(
                    401,
                    state="closed",
                    closed_at="2026-08-01T00:00:00Z",
                    labels=["ci-failure-cause"],
                    body="#402",
                ),
                make_issue(
                    402,
                    state="closed",
                    closed_at="2026-08-02T00:00:00Z",
                    labels=["ci-failure-cause"],
                    body="#403",
                ),
                make_issue(
                    403,
                    state="closed",
                    closed_at="2026-08-03T00:00:00Z",
                    labels=["ci-failure-cause"],
                ),
            ],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
            **{
                f"/repos/{REPOSITORY}/issues/{number}/comments": []
                for number in (21, 22, 401, 402, 403)
            },
        }
        collector = Collector(ScriptedClient(pages=pages), REPOSITORY, NOW)

        result = collector.collect(include_timeline=False)
        enriched = collector.enrich_github_evidence(result)

        self.assertEqual("available", enriched.evidence["issue:403"]["availability"])
        self.assertEqual(
            [22],
            [
                association["sourceIssueNumber"]
                for association in enriched.evidence["issue:403"]["payload"]["referencedBy"]
            ],
        )
        self.assertEqual(
            {
                "complete": False,
                "candidateIssueNumbers": [401, 402],
                "truncated": True,
                "candidateDispositions": [
                    {
                        "issueNumber": 403,
                        "disposition": "excluded-depth",
                        "provenance": [
                            {
                                "sourceEvidenceId": "issue:402",
                                "sourceUrl": f"https://github.com/{REPOSITORY}/issues/402",
                                "extractionMethod": "local-issue",
                            }
                        ],
                    }
                ],
            },
            result.open_issues[0]["supportingSearch"],
        )
        scoped_snapshot = snapshot_from_result(enriched)
        scoped_snapshot["evidence"] = {
            evidence_id: enriched.evidence[evidence_id]
            for evidence_id in ("issue:21", "issue:22", "issue:403")
        }
        validate_snapshot(scoped_snapshot)
        with self.assertRaisesRegex(ValidationError, "canonical-issue"):
            validate_report(scoped_snapshot, mixed_root_report(21))
        self.assertIsNone(validate_report(scoped_snapshot, mixed_root_report(22)))

    def test_mixed_roots_keep_budget_exclusion_out_of_available_supporting_evidence(self) -> None:
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(21, labels=["ci-failure-cause"], body="#401"),
                make_issue(22, labels=["ci-failure-cause"], body="#403"),
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [
                make_issue(
                    401,
                    state="closed",
                    closed_at="2026-08-01T00:00:00Z",
                    labels=["ci-failure-cause"],
                    body="#403",
                ),
                make_issue(
                    403,
                    state="closed",
                    closed_at="2026-08-03T00:00:00Z",
                    labels=["ci-failure-cause"],
                ),
            ],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
            **{
                f"/repos/{REPOSITORY}/issues/{number}/comments": []
                for number in (21, 22, 401, 403)
            },
        }
        collector = Collector(
            ScriptedClient(pages=pages),
            REPOSITORY,
            NOW,
            budgets={"max_supporting_closed": 2},
        )

        result = collector.collect(include_timeline=False)
        enriched = collector.enrich_github_evidence(result)

        self.assertEqual("available", enriched.evidence["issue:403"]["availability"])
        self.assertEqual(
            [22],
            [
                association["sourceIssueNumber"]
                for association in enriched.evidence["issue:403"]["payload"]["referencedBy"]
            ],
        )
        self.assertEqual(
            {
                "complete": False,
                "candidateIssueNumbers": [401],
                "truncated": True,
                "candidateDispositions": [
                    {
                        "issueNumber": 403,
                        "disposition": "excluded-budget",
                        "provenance": [
                            {
                                "sourceEvidenceId": "issue:401",
                                "sourceUrl": f"https://github.com/{REPOSITORY}/issues/401",
                                "extractionMethod": "local-issue",
                            }
                        ],
                    }
                ],
            },
            result.open_issues[0]["supportingSearch"],
        )
        scoped_snapshot = snapshot_from_result(enriched)
        scoped_snapshot["evidence"] = {
            evidence_id: enriched.evidence[evidence_id]
            for evidence_id in ("issue:21", "issue:22", "issue:403")
        }
        validate_snapshot(scoped_snapshot)
        with self.assertRaisesRegex(ValidationError, "canonical-issue"):
            validate_report(scoped_snapshot, mixed_root_report(21))
        self.assertIsNone(validate_report(scoped_snapshot, mixed_root_report(22)))

    def test_supporting_fanout_honors_shared_budget_and_depth_selection_during_enrichment(self) -> None:
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(21, labels=["ci-failure-cause"], body="#401")
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [
                make_issue(
                    401,
                    state="closed",
                    closed_at="2026-08-01T00:00:00Z",
                    labels=["ci-failure-cause"],
                    body="#402\n#403",
                ),
                make_issue(
                    402,
                    state="closed",
                    closed_at="2026-08-02T00:00:00Z",
                    labels=["ci-failure-cause"],
                    body="#404",
                ),
                make_issue(
                    403,
                    state="closed",
                    closed_at="2026-08-03T00:00:00Z",
                    labels=["ci-failure-cause"],
                ),
                make_issue(
                    404,
                    state="closed",
                    closed_at="2026-08-04T00:00:00Z",
                    labels=["ci-failure-cause"],
                ),
            ],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/21/comments": [],
            f"/repos/{REPOSITORY}/issues/401/comments": [],
            f"/repos/{REPOSITORY}/issues/402/comments": [],
        }
        singles = {
            f"/repos/{REPOSITORY}/issues/{number}": make_issue(
                number,
                state="closed",
                closed_at=f"2026-08-{number - 400:02d}T00:00:00Z",
                labels=["ci-failure-cause"],
            )
            for number in (403, 404)
        }
        client = ScriptedClient(pages=pages, singles=singles)
        collector = Collector(
            client,
            REPOSITORY,
            NOW,
            budgets={"max_supporting_closed": 2},
        )

        result = collector.collect(include_timeline=False)
        enriched = collector.enrich_github_evidence(result)

        self.assertEqual([401, 402], result.open_issues[0]["supportingIssueNumbers"])
        self.assertFalse(any(method == "get" and "/issues/" in endpoint for method, endpoint in client.calls))
        self.assertEqual(
            {
                "complete": False,
                "candidateIssueNumbers": [401, 402],
                "truncated": True,
                "candidateDispositions": [
                    {
                        "issueNumber": 403,
                        "disposition": "excluded-budget",
                        "provenance": [
                            {
                                "sourceEvidenceId": "issue:401",
                                "sourceUrl": f"https://github.com/{REPOSITORY}/issues/401",
                                "extractionMethod": "local-issue",
                            }
                        ],
                    },
                    {
                        "issueNumber": 404,
                        "disposition": "excluded-depth",
                        "provenance": [
                            {
                                "sourceEvidenceId": "issue:402",
                                "sourceUrl": f"https://github.com/{REPOSITORY}/issues/402",
                                "extractionMethod": "local-issue",
                            }
                        ],
                    },
                ],
            },
            result.open_issues[0]["supportingSearch"],
        )
        selections = {
            reference["targetNumber"]: reference.get("supportingSelection")
            for references in result.references.values()
            for reference in references
            if reference["targetType"] == "issue"
            and reference["targetNumber"] in {403, 404}
        }
        self.assertEqual(
            {
                403: {
                    "state": "excluded",
                    "reasons": ["global-budget"],
                    "rootIssueNumbers": [21],
                },
                404: {
                    "state": "excluded",
                    "reasons": ["depth-limit"],
                    "rootIssueNumbers": [21],
                },
            },
            selections,
        )
        for issue_number, reason_key in (
            (403, "supportingBudgetExcluded"),
            (404, "supportingDepthExcluded"),
        ):
            with self.subTest(issue_number=issue_number):
                record = enriched.evidence[f"issue:{issue_number}"]
                self.assertEqual("not-enriched", record["availability"])
                self.assertTrue(record["payload"][reason_key])
                self.assertEqual(
                    [21],
                    [
                        association["sourceIssueNumber"]
                        for association in record["payload"]["referencedBy"]
                    ],
                )

    def test_collect_matches_supporting_issues_from_identity_markers_and_test_names(self) -> None:
        result = self.collect(FixtureClient(FIXTURE_ROOT))

        open_issues = {issue["number"]: issue for issue in result.open_issues}
        self.assertEqual([140, 150, 201], open_issues[101]["supportingIssueNumbers"])
        self.assertEqual([], open_issues[102]["supportingIssueNumbers"])
        marker_methods = {marker["method"] for marker in open_issues[101]["markers"]}
        fact_fields = {fact["field"] for fact in open_issues[101]["facts"]}
        self.assertIn("html-comment", marker_methods)
        self.assertIn("testName", fact_fields)
        self.assertIn("exceptionType", fact_fields)

    def test_direct_comment_reference_preserves_comment_provenance_for_root_association(self) -> None:
        comment_url = f"https://github.com/{REPOSITORY}/issues/21#issuecomment-900"
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(21, labels=["ci-failure-cause"])
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [
                make_issue(
                    401,
                    state="closed",
                    closed_at="2026-08-01T00:00:00Z",
                    labels=["ci-failure-cause"],
                )
            ],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/21/comments": [
                {
                    "id": 900,
                    "html_url": comment_url,
                    "created_at": "2026-08-03T00:00:00Z",
                    "updated_at": "2026-08-03T00:00:00Z",
                    "user": {"login": "octocat"},
                    "body": "#401",
                }
            ],
            f"/repos/{REPOSITORY}/issues/401/comments": [],
        }

        result = Collector(ScriptedClient(pages=pages), REPOSITORY, NOW).collect(
            include_timeline=False,
        )

        self.assertEqual(
            [
                {
                    "sourceIssueNumber": 21,
                    "sourceEvidenceId": "issue:21:comment:900",
                    "sourceUrl": comment_url,
                    "extractionMethod": "local-issue",
                }
            ],
            result.evidence["issue:401"]["payload"]["referencedBy"],
        )

    def test_owned_shepherd_status_comment_is_retained_without_becoming_evidence(self) -> None:
        comment_url = f"https://github.com/{REPOSITORY}/issues/21#issuecomment-900"
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(21, labels=["ci-failure-cause"])
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [
                make_issue(
                    401,
                    state="closed",
                    closed_at="2026-08-01T00:00:00Z",
                    labels=["ci-failure-cause"],
                )
            ],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/21/comments": [
                {
                    "id": 900,
                    "html_url": comment_url,
                    "created_at": "2026-08-21T12:00:00Z",
                    "updated_at": "2026-08-21T12:00:00Z",
                    "user": {"login": "ankj"},
                    "body": (
                        "[automated] Watching #401 and "
                        f"https://github.com/{REPOSITORY}/actions/runs/777.\n\n"
                        "<!-- ci-shepherd:role=status -->\n"
                        "<!-- ci-shepherd:idempotency-key=issue:21:watch -->"
                    ),
                }
            ],
            f"/repos/{REPOSITORY}/issues/401/comments": [],
        }

        result = Collector(
            ScriptedClient(pages=pages),
            REPOSITORY,
            NOW,
            shepherd_author="ankj",
        ).collect(include_timeline=False)

        payload = result.evidence["issue:21:comment:900"]["payload"]
        self.assertEqual(
            {
                "role": "status",
                "idempotencyKey": "issue:21:watch",
                "owned": True,
            },
            payload["shepherdStatus"],
        )
        self.assertEqual([], payload["markers"])
        self.assertEqual([], payload["facts"])
        self.assertEqual([], payload["references"])
        self.assertNotIn("issue:401", result.evidence)
        self.assertNotIn(21, result.references)

    def test_unowned_shepherd_marker_does_not_hide_comment_evidence(self) -> None:
        comment_url = f"https://github.com/{REPOSITORY}/issues/21#issuecomment-900"
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(21, labels=["ci-failure-cause"])
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [
                make_issue(
                    401,
                    state="closed",
                    closed_at="2026-08-01T00:00:00Z",
                    labels=["ci-failure-cause"],
                )
            ],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/21/comments": [
                {
                    "id": 900,
                    "html_url": comment_url,
                    "created_at": "2026-08-21T12:00:00Z",
                    "updated_at": "2026-08-21T12:00:00Z",
                    "user": {"login": "someone-else"},
                    "body": (
                        "[automated] Watching #401.\n\n"
                        "<!-- ci-shepherd:role=status -->\n"
                        "<!-- ci-shepherd:idempotency-key=issue:21:watch -->"
                    ),
                }
            ],
            f"/repos/{REPOSITORY}/issues/401/comments": [],
        }

        result = Collector(
            ScriptedClient(pages=pages),
            REPOSITORY,
            NOW,
            shepherd_author="ankj",
        ).collect(include_timeline=False)

        payload = result.evidence["issue:21:comment:900"]["payload"]
        self.assertNotIn("shepherdStatus", payload)
        self.assertIn("issue:401", result.evidence)
        self.assertEqual(
            "issue:21:comment:900",
            result.evidence["issue:401"]["payload"]["referencedBy"][0][
                "sourceEvidenceId"
            ],
        )

    def test_transitive_comment_reference_preserves_immediate_comment_provenance_with_root_issue_number(
        self,
    ) -> None:
        comment_url = f"https://github.com/{REPOSITORY}/issues/401#issuecomment-901"
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(21, labels=["ci-failure-cause"], body="#401")
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [
                make_issue(
                    401,
                    state="closed",
                    closed_at="2026-08-01T00:00:00Z",
                    labels=["ci-failure-cause"],
                ),
                make_issue(
                    402,
                    state="closed",
                    closed_at="2026-08-02T00:00:00Z",
                    labels=["ci-failure-cause"],
                ),
            ],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/21/comments": [],
            f"/repos/{REPOSITORY}/issues/401/comments": [
                {
                    "id": 901,
                    "html_url": comment_url,
                    "created_at": "2026-08-03T00:00:00Z",
                    "updated_at": "2026-08-03T00:00:00Z",
                    "user": {"login": "octocat"},
                    "body": "#402",
                }
            ],
            f"/repos/{REPOSITORY}/issues/402/comments": [],
        }

        result = Collector(ScriptedClient(pages=pages), REPOSITORY, NOW).collect(
            include_timeline=False,
        )

        self.assertEqual(
            [
                {
                    "sourceIssueNumber": 21,
                    "sourceEvidenceId": "issue:401:comment:901",
                    "sourceUrl": comment_url,
                    "extractionMethod": "local-issue",
                }
            ],
            result.evidence["issue:402"]["payload"]["referencedBy"],
        )

    def test_selected_transitive_explicit_support_issue_is_associated_with_root_open_issue(self) -> None:
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(21, labels=["ci-failure-cause"], body="#401")
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [
                make_issue(
                    401,
                    state="closed",
                    closed_at="2026-08-01T00:00:00Z",
                    labels=["ci-failure-cause"],
                    body="#402",
                ),
                make_issue(
                    402,
                    state="closed",
                    closed_at="2026-08-02T00:00:00Z",
                    labels=["ci-failure-cause"],
                ),
            ],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/21/comments": [],
            f"/repos/{REPOSITORY}/issues/21/timeline": [],
            f"/repos/{REPOSITORY}/issues/401/comments": [],
            f"/repos/{REPOSITORY}/issues/401/timeline": [],
            f"/repos/{REPOSITORY}/issues/402/comments": [],
            f"/repos/{REPOSITORY}/issues/402/timeline": [],
        }

        result = self.collect(ScriptedClient(pages=pages))

        self.assertIn(402, result.open_issues[0]["supportingIssueNumbers"])
        self.assertIn(
            {
                "sourceIssueNumber": 21,
                "sourceEvidenceId": "issue:401",
                "sourceUrl": f"https://github.com/{REPOSITORY}/issues/401",
                "extractionMethod": "local-issue",
            },
            result.evidence["issue:402"]["payload"]["referencedBy"],
        )

    def test_support_comment_reference_cannot_bypass_exhausted_global_supporting_budget(self) -> None:
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(21, labels=["ci-failure-cause"], body="#401")
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/21/comments": [],
            f"/repos/{REPOSITORY}/issues/401/comments": [
                {
                    "id": 40101,
                    "html_url": f"https://github.com/{REPOSITORY}/issues/401#issuecomment-40101",
                    "created_at": "2026-08-03T00:00:00Z",
                    "updated_at": "2026-08-03T00:00:00Z",
                    "user": {"login": "octocat"},
                    "body": "#402",
                }
            ],
        }
        singles = {
            f"/repos/{REPOSITORY}/issues/401": make_issue(
                401,
                state="closed",
                closed_at="2026-08-01T00:00:00Z",
            ),
            f"/repos/{REPOSITORY}/issues/402": make_issue(
                402,
                state="closed",
                closed_at="2026-08-02T00:00:00Z",
            ),
        }
        client = ScriptedClient(pages=pages, singles=singles)
        collector = Collector(
            client,
            REPOSITORY,
            NOW,
            budgets={"max_supporting_closed": 1},
        )

        result = collector.collect(include_timeline=False)
        enriched = collector.enrich_github_evidence(result)

        self.assertEqual(
            [f"/repos/{REPOSITORY}/issues/401"],
            [
                endpoint
                for method, endpoint in client.calls
                if method == "get" and endpoint.startswith(f"/repos/{REPOSITORY}/issues/")
            ],
        )
        self.assertEqual(
            {
                "complete": False,
                "candidateIssueNumbers": [401],
                "truncated": True,
                "candidateDispositions": [
                    {
                        "issueNumber": 402,
                        "disposition": "excluded-budget",
                        "provenance": [
                            {
                                "sourceEvidenceId": "issue:401:comment:40101",
                                "sourceUrl": (
                                    f"https://github.com/{REPOSITORY}/issues/401"
                                    "#issuecomment-40101"
                                ),
                                "extractionMethod": "local-issue",
                            }
                        ],
                    }
                ],
            },
            result.open_issues[0]["supportingSearch"],
        )
        self.assertEqual(
            [21],
            [
                association["sourceIssueNumber"]
                for association in enriched.evidence["issue:402"]["payload"]["referencedBy"]
            ],
        )
        self.assertEqual("not-enriched", enriched.evidence["issue:402"]["availability"])

    def test_support_comment_reference_inherits_root_when_global_budget_allows_fetch(self) -> None:
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(21, labels=["ci-failure-cause"], body="#401")
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/21/comments": [],
            f"/repos/{REPOSITORY}/issues/401/comments": [
                {
                    "id": 40101,
                    "html_url": f"https://github.com/{REPOSITORY}/issues/401#issuecomment-40101",
                    "created_at": "2026-08-03T00:00:00Z",
                    "updated_at": "2026-08-03T00:00:00Z",
                    "user": {"login": "octocat"},
                    "body": "#402",
                }
            ],
            f"/repos/{REPOSITORY}/issues/402/comments": [],
        }
        singles = {
            f"/repos/{REPOSITORY}/issues/401": make_issue(
                401,
                state="closed",
                closed_at="2026-08-01T00:00:00Z",
            ),
            f"/repos/{REPOSITORY}/issues/402": make_issue(
                402,
                state="closed",
                closed_at="2026-08-02T00:00:00Z",
            ),
        }
        client = ScriptedClient(pages=pages, singles=singles)
        collector = Collector(
            client,
            REPOSITORY,
            NOW,
            budgets={"max_supporting_closed": 2},
        )

        result = collector.collect(include_timeline=False)
        enriched = collector.enrich_github_evidence(result)

        self.assertEqual([401, 402], [issue["number"] for issue in result.supporting_issues])
        self.assertEqual(
            {
                "complete": True,
                "candidateIssueNumbers": [401, 402],
                "truncated": False,
            },
            result.open_issues[0]["supportingSearch"],
        )
        self.assertEqual(
            [21],
            [
                association["sourceIssueNumber"]
                for association in enriched.evidence["issue:402"]["payload"]["referencedBy"]
            ],
        )

    def test_inferred_support_issue_associations_merge_marker_and_fact_matches_from_open_issues(self) -> None:
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(
                    21,
                    labels=["ci-failure-cause"],
                    body="<!-- ci-failure-cause:shared-timeout -->",
                ),
                make_issue(
                    22,
                    labels=["ci-failure-cause"],
                    body="Test name: Shared.Timeout",
                ),
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [
                make_issue(
                    401,
                    state="closed",
                    closed_at="2026-08-01T00:00:00Z",
                    labels=["ci-failure-cause"],
                    body=(
                        "<!-- ci-failure-cause:shared-timeout -->\n"
                        "Test name: Shared.Timeout"
                    ),
                ),
            ],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/21/comments": [],
            f"/repos/{REPOSITORY}/issues/21/timeline": [],
            f"/repos/{REPOSITORY}/issues/22/comments": [],
            f"/repos/{REPOSITORY}/issues/22/timeline": [],
            f"/repos/{REPOSITORY}/issues/401/comments": [],
            f"/repos/{REPOSITORY}/issues/401/timeline": [],
        }

        result = self.collect(ScriptedClient(pages=pages))

        self.assertEqual(
            [
                {
                    "sourceIssueNumber": 21,
                    "sourceEvidenceId": "issue:21",
                    "sourceUrl": f"https://github.com/{REPOSITORY}/issues/21",
                    "extractionMethod": "marker-match",
                },
                {
                    "sourceIssueNumber": 22,
                    "sourceEvidenceId": "issue:22",
                    "sourceUrl": f"https://github.com/{REPOSITORY}/issues/22",
                    "extractionMethod": "fact-match",
                },
            ],
            result.evidence["issue:401"]["payload"].get("referencedBy", []),
        )

    def test_ci_failure_identity_requires_exact_workflow_event_and_ref(self) -> None:
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(
                    21,
                    labels=["ci-failure-cause"],
                    body="<!-- ci-failure:ci.yml:push:main -->",
                )
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [
                make_issue(
                    401,
                    state="closed",
                    closed_at="2026-08-01T00:00:00Z",
                    labels=["ci-failure-cause"],
                    body="<!-- ci-failure:ci.yml:push:main -->",
                ),
                make_issue(
                    402,
                    state="closed",
                    closed_at="2026-08-02T00:00:00Z",
                    labels=["ci-failure-cause"],
                    body="<!-- ci-failure:ci.yml:push:release/13.3 -->",
                ),
                make_issue(
                    403,
                    state="closed",
                    closed_at="2026-08-03T00:00:00Z",
                    labels=["ci-failure-cause"],
                    body="<!-- ci-failure:tests-outerloop.yml:test-failures -->",
                ),
            ],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/21/comments": [],
            f"/repos/{REPOSITORY}/issues/21/timeline": [],
            f"/repos/{REPOSITORY}/issues/401/comments": [],
            f"/repos/{REPOSITORY}/issues/401/timeline": [],
        }

        result = self.collect(ScriptedClient(pages=pages))

        self.assertEqual([401], result.open_issues[0]["supportingIssueNumbers"])
        self.assertEqual([401], [issue["number"] for issue in result.supporting_issues])

    def test_correlation_allowlists_are_closed(self) -> None:
        self.assertEqual(
            frozenset(
                {
                    "automation-broken",
                    "ci-failure",
                    "ci-failure-cause",
                    "gh-aw-failure-issue",
                }
            ),
            _CORRELATION_MARKER_KEYS,
        )
        self.assertEqual(frozenset({"testName"}), _CORRELATION_FACT_FIELDS)

        unrelated_fact_fields = (
            "job",
            "sourceRun",
            "exceptionType",
            "triggeringPullRequest",
            "causeId",
        )
        for field in unrelated_fact_fields:
            with self.subTest(field=field):
                candidate = _CandidateIssue(
                    issue=make_issue(401, state="closed"),
                    markers=[],
                    facts=[{"field": field, "normalized": "shared-value"}],
                )

                _, fact_index = Collector(
                    ScriptedClient(),
                    REPOSITORY,
                    NOW,
                )._build_candidate_indexes({401: candidate})

                self.assertEqual({}, fact_index)

    def test_failure_type_does_not_correlate_unrelated_issues(self) -> None:
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(
                    21,
                    labels=["ci-failure-cause"],
                    body="**Type**: Test Failure",
                )
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [
                make_issue(
                    401,
                    state="closed",
                    closed_at="2026-08-01T00:00:00Z",
                    labels=["ci-failure-cause"],
                    body="**Type**: Test Failure",
                ),
            ],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/21/comments": [],
            f"/repos/{REPOSITORY}/issues/21/timeline": [],
        }

        result = self.collect(ScriptedClient(pages=pages))

        self.assertEqual([], result.open_issues[0]["supportingIssueNumbers"])
        self.assertEqual([], result.supporting_issues)
        self.assertNotIn("issue:401", result.evidence)

    def test_error_code_does_not_correlate_unrelated_issues(self) -> None:
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(
                    21,
                    labels=["ci-failure-cause"],
                    body="Error code: CS0123",
                )
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [
                make_issue(
                    401,
                    state="closed",
                    closed_at="2026-08-01T00:00:00Z",
                    labels=["ci-failure-cause"],
                    body="Error code: CS0123",
                ),
            ],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/21/comments": [],
            f"/repos/{REPOSITORY}/issues/21/timeline": [],
        }

        result = self.collect(ScriptedClient(pages=pages))

        self.assertEqual([], result.open_issues[0]["supportingIssueNumbers"])
        self.assertEqual([], result.supporting_issues)
        self.assertNotIn("issue:401", result.evidence)

    def test_exact_test_name_correlates_issues(self) -> None:
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(
                    21,
                    labels=["ci-failure-cause"],
                    body="Test name: Shared.Timeout",
                )
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [
                make_issue(
                    401,
                    state="closed",
                    closed_at="2026-08-01T00:00:00Z",
                    labels=["ci-failure-cause"],
                    body="Test name: Shared.Timeout",
                ),
            ],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/21/comments": [],
            f"/repos/{REPOSITORY}/issues/21/timeline": [],
            f"/repos/{REPOSITORY}/issues/401/comments": [],
            f"/repos/{REPOSITORY}/issues/401/timeline": [],
        }

        result = self.collect(ScriptedClient(pages=pages))

        self.assertEqual([401], result.open_issues[0]["supportingIssueNumbers"])
        self.assertEqual([401], [issue["number"] for issue in result.supporting_issues])
        self.assertEqual(
            "fact-match",
            result.evidence["issue:401"]["payload"]["referencedBy"][0]["extractionMethod"],
        )

    def test_marker_match_precedes_fact_match_at_global_supporting_budget(self) -> None:
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(
                    21,
                    labels=["ci-failure-cause"],
                    body=(
                        "<!-- ci-failure-cause:shared-timeout -->\n"
                        "Test name: Shared.Timeout"
                    ),
                )
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [
                make_issue(
                    401,
                    state="closed",
                    closed_at="2026-08-01T00:00:00Z",
                    labels=["ci-failure-cause"],
                    body="<!-- ci-failure-cause:shared-timeout -->",
                ),
                make_issue(
                    402,
                    state="closed",
                    closed_at="2026-08-02T00:00:00Z",
                    labels=["ci-failure-cause"],
                    body="Test name: Shared.Timeout",
                ),
            ],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/21/comments": [],
            f"/repos/{REPOSITORY}/issues/21/timeline": [],
            f"/repos/{REPOSITORY}/issues/401/comments": [],
            f"/repos/{REPOSITORY}/issues/401/timeline": [],
        }

        result = self.collect(
            ScriptedClient(pages=pages),
            budgets={"max_supporting_closed": 1},
        )

        self.assertEqual([401], result.open_issues[0]["supportingIssueNumbers"])
        self.assertEqual([401], [issue["number"] for issue in result.supporting_issues])
        self.assertEqual(
            "marker-match",
            result.evidence["issue:401"]["payload"]["referencedBy"][0]["extractionMethod"],
        )
        self.assertNotIn("issue:402", result.evidence)

    def test_only_identity_markers_correlate_automation_workflows(self) -> None:
        shared_non_identity_markers = (
            "<!-- autoclose:true -->\n"
            "<!-- run:123 -->\n"
            "<!-- gh-aw-agentic-workflow:shared-agentic-workflow.md -->"
        )
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [
                make_issue(
                    21,
                    labels=["automation-broken"],
                    body=(
                        "<!-- automation-broken:workflow-a.lock.yml -->\n"
                        f"{shared_non_identity_markers}"
                    ),
                )
            ],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [
                make_issue(
                    401,
                    state="closed",
                    closed_at="2026-08-01T00:00:00Z",
                    labels=["automation-broken"],
                    body=(
                        "<!-- automation-broken:workflow-a.lock.yml -->\n"
                        f"{shared_non_identity_markers}"
                    ),
                ),
                make_issue(
                    402,
                    state="closed",
                    closed_at="2026-08-02T00:00:00Z",
                    labels=["automation-broken"],
                    body=(
                        "<!-- automation-broken:workflow-b.lock.yml -->\n"
                        f"{shared_non_identity_markers}"
                    ),
                ),
            ],
            f"/repos/{REPOSITORY}/issues/21/comments": [],
            f"/repos/{REPOSITORY}/issues/401/comments": [],
        }

        result = self.collect(ScriptedClient(pages=pages))

        self.assertEqual([401], result.open_issues[0]["supportingIssueNumbers"])
        self.assertEqual([401], [issue["number"] for issue in result.supporting_issues])
        self.assertEqual(
            [
                {
                    "sourceIssueNumber": 21,
                    "sourceEvidenceId": "issue:21",
                    "sourceUrl": f"https://github.com/{REPOSITORY}/issues/21",
                    "extractionMethod": "marker-match",
                }
            ],
            result.evidence["issue:401"]["payload"].get("referencedBy", []),
        )
        self.assertNotIn("issue:402", result.evidence)

    def test_collect_records_partial_errors_for_secondary_fetch_failures(self) -> None:
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [make_issue(11, labels=["ci-failure-cause"])],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [],
            f"/repos/{REPOSITORY}/issues/11/comments": RuntimeError("comments failed"),
            f"/repos/{REPOSITORY}/issues/11/timeline": RuntimeError("timeline failed"),
        }
        client = ScriptedClient(pages=pages)

        result = self.collect(client)

        self.assertEqual([11], [issue["number"] for issue in result.open_issues])
        self.assertEqual(
            [(error.stage, error.endpoint) for error in result.collection_errors],
            [
                ("comments", f"/repos/{REPOSITORY}/issues/11/comments"),
                ("timeline", f"/repos/{REPOSITORY}/issues/11/timeline"),
            ],
        )
        self.assertEqual(
            [
                {"kind": "issue", "issueNumbers": [11]},
                {"kind": "issue", "issueNumbers": [11]},
            ],
            [error.scope for error in result.collection_errors],
        )

    def test_collect_emits_budget_warnings_and_truncates_refs_and_supporting_sets(self) -> None:
        pages = {
            f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                make_issue(21, labels=["ci-failure-cause"], body=" ".join(f"#{number}" for number in range(300, 305)) + "\nTest name: Shared.Flake")
            ],
            f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since={CUTOFF}&per_page=100": [
                make_issue(401, state="closed", closed_at="2026-08-01T00:00:00Z", labels=["ci-failure-cause"], body="Test name: Shared.Flake"),
                make_issue(402, state="closed", closed_at="2026-08-02T00:00:00Z", labels=["ci-failure-cause"], body="Test name: Shared.Flake"),
            ],
            f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since={CUTOFF}&per_page=100": [
                make_issue(403, state="closed", closed_at="2026-08-03T00:00:00Z", labels=["automation-broken"], body="Test name: Shared.Flake"),
            ],
            f"/repos/{REPOSITORY}/issues/21/comments": [],
            f"/repos/{REPOSITORY}/issues/21/timeline": [],
            f"/repos/{REPOSITORY}/issues/401/comments": [],
            f"/repos/{REPOSITORY}/issues/401/timeline": [],
            f"/repos/{REPOSITORY}/issues/402/comments": [],
            f"/repos/{REPOSITORY}/issues/402/timeline": [],
            f"/repos/{REPOSITORY}/issues/403/comments": [],
            f"/repos/{REPOSITORY}/issues/403/timeline": [],
        }
        singles = {
            f"/repos/{REPOSITORY}/issues/300": make_issue(300, state="closed", closed_at="2026-01-01T00:00:00Z"),
            f"/repos/{REPOSITORY}/issues/301": make_issue(301, state="closed", closed_at="2026-01-02T00:00:00Z"),
            f"/repos/{REPOSITORY}/issues/302": make_issue(302, state="closed", closed_at="2026-01-03T00:00:00Z"),
            f"/repos/{REPOSITORY}/issues/303": make_issue(303, state="closed", closed_at="2026-01-04T00:00:00Z"),
            f"/repos/{REPOSITORY}/issues/304": make_issue(304, state="closed", closed_at="2026-01-05T00:00:00Z"),
        }
        client = ScriptedClient(pages=pages, singles=singles)

        result = self.collect(
            client,
            budgets={
                "max_issue_refs_per_issue": 2,
                "max_supporting_closed": 2,
                "fact_candidates": 1,
            },
        )

        self.assertEqual(2, len(result.references[21]))
        self.assertEqual(2, len(result.supporting_issues))
        warning_text = "\n".join(result.warnings)
        self.assertIn("max_issue_refs_per_issue", warning_text)
        self.assertIn("max_supporting_closed", warning_text)
        self.assertIn("fact_candidates", warning_text)
        search = result.open_issues[0].get("supportingSearch")
        self.assertIsNotNone(search)
        self.assertFalse(search["complete"])
        self.assertTrue(search["truncated"])
        self.assertEqual([300, 301], search["candidateIssueNumbers"])

    def test_collect_filters_pull_requests_from_issue_inventory(self) -> None:
        result = self.collect(FixtureClient(FIXTURE_ROOT))
        all_numbers = {issue["number"] for issue in result.open_issues + result.supporting_issues}

        self.assertFalse({103, 104, 204} & all_numbers)

    def test_collect_produces_evidence_that_passes_snapshot_validation(self) -> None:
        result = self.collect(FixtureClient(FIXTURE_ROOT))

        validate_snapshot(snapshot_from_result(result))
        self.assertIn("issue:101", result.evidence)
        self.assertIn("issue:101:comment:1001", result.evidence)
        self.assertIn("issue:101:event:5001", result.evidence)


if __name__ == "__main__":
    unittest.main()
