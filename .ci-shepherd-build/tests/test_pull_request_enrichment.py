from __future__ import annotations

import unittest
from dataclasses import asdict
from datetime import UTC, datetime

from ci_shepherd.collector import Collector, InventoryResult
from ci_shepherd.models import validate_snapshot
from ci_shepherd.pull_requests import (
    CHECKS_GREEN,
    CHECKS_RED,
    CHECKS_UNKNOWN,
    REVIEW_CHANGES_REQUESTED,
    build_pull_request_handoff,
)
from ci_shepherd.refresh import RefreshPlan

from test_enrichment import EnrichmentClient, FakeApiError, REPOSITORY, make_issue


NOW = datetime(2026, 8, 17, 22, 0, tzinfo=UTC)
HEAD_SHA = "cccccccccccccccccccccccccccccccccccccccc"
SHEPHERD = "ci-shepherd-bot"

PULL_NUMBER = 42
PULL_URL = f"https://github.com/{REPOSITORY}/pull/{PULL_NUMBER}"

ISSUE_ENDPOINT = f"/repos/{REPOSITORY}/issues/{PULL_NUMBER}"
PULL_ENDPOINT = f"/repos/{REPOSITORY}/pulls/{PULL_NUMBER}"
FILES_ENDPOINT = f"/repos/{REPOSITORY}/pulls/{PULL_NUMBER}/files?per_page=100"
CHECKS_ENDPOINT = f"/repos/{REPOSITORY}/commits/{HEAD_SHA}/check-runs?per_page=100"
STATUS_ENDPOINT = f"/repos/{REPOSITORY}/commits/{HEAD_SHA}/status"
REVIEWS_ENDPOINT = f"/repos/{REPOSITORY}/pulls/{PULL_NUMBER}/reviews?per_page=100"
COMMENTS_ENDPOINT = f"/repos/{REPOSITORY}/issues/{PULL_NUMBER}/comments?per_page=100"


def raw_pull(*, mergeable: bool = True, mergeable_state: str = "clean") -> dict[str, object]:
    return {
        "number": PULL_NUMBER,
        "state": "open",
        "html_url": PULL_URL,
        "merged_at": None,
        "merge_commit_sha": None,
        "draft": False,
        "mergeable": mergeable,
        "mergeable_state": mergeable_state,
        "base": {"ref": "main", "sha": "b" * 40},
        "head": {"ref": "automation/fix", "sha": HEAD_SHA, "repo": {"full_name": REPOSITORY}},
    }


def inventory_pull_request(assignees: list[str] | None = None) -> dict[str, object]:
    return {
        "number": PULL_NUMBER,
        "state": "open",
        "title": "Update generated CI configuration",
        "url": PULL_URL,
        "updatedAt": "2026-08-16T10:00:00Z",
        "labels": ["automation-broken"],
        "author": "github-actions[bot]",
        "assignees": list(assignees or []),
        "selectionReasons": ["label:automation-broken"],
    }


def base_pages(**overrides: object) -> dict[str, object]:
    pages: dict[str, object] = {
        FILES_ENDPOINT: [{"filename": "eng/example.yml", "status": "modified"}],
        CHECKS_ENDPOINT: {
            "check_runs": [
                {
                    "name": "build",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": HEAD_SHA,
                }
            ]
        },
        REVIEWS_ENDPOINT: [],
        COMMENTS_ENDPOINT: [],
    }
    pages.update(overrides)
    return pages


def base_singles(**overrides: object) -> dict[str, object]:
    singles: dict[str, object] = {
        ISSUE_ENDPOINT: make_issue(PULL_NUMBER, is_pull_request=True),
        PULL_ENDPOINT: raw_pull(),
    }
    singles.update(overrides)
    return singles


def snapshot_from_result(result: InventoryResult) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "repository": REPOSITORY,
        "collectedAt": NOW.isoformat().replace("+00:00", "Z"),
        "openIssues": [issue["number"] for issue in result.open_issues],
        "openPullRequests": [pull["number"] for pull in result.open_pull_requests],
        "pullRequests": result.open_pull_requests,
        "evidence": result.evidence,
        "collectionErrors": [asdict(error) for error in result.collection_errors],
    }


class PullRequestEnrichmentTests(unittest.TestCase):
    def enrich(
        self,
        client: EnrichmentClient,
        *,
        assignees: list[str] | None = None,
        shepherd_author: str | None = SHEPHERD,
        budgets: dict[str, int] | None = None,
    ) -> InventoryResult:
        inventory = InventoryResult(
            open_issues=[],
            supporting_issues=[],
            evidence={},
            collection_errors=[],
            warnings=[],
            references={},
            open_pull_requests=[inventory_pull_request(assignees)],
        )
        collector = Collector(
            client,
            REPOSITORY,
            NOW,
            shepherd_author=shepherd_author,
            budgets=budgets,
        )
        return collector.enrich_github_evidence(inventory)

    def current_state(self, result: InventoryResult) -> dict[str, object]:
        return result.evidence[f"pr:{PULL_NUMBER}"]["payload"]["currentState"]

    def test_selected_pull_request_gains_current_check_and_review_state(self) -> None:
        client = EnrichmentClient(
            pages=base_pages(
                **{REVIEWS_ENDPOINT: [{"user": {"login": "alice"}, "state": "APPROVED"}]}
            ),
            singles=base_singles(),
        )

        result = self.enrich(client)
        state = self.current_state(result)

        self.assertEqual(HEAD_SHA, state["headSha"])
        self.assertEqual(CHECKS_GREEN, state["checks"]["state"])
        self.assertEqual("check-runs", state["checks"]["source"])
        self.assertEqual("approved", state["review"]["decision"])
        self.assertTrue(state["complete"])
        self.assertEqual([], result.collection_errors)
        validate_snapshot(snapshot_from_result(result))

    def test_primary_pull_request_skips_unused_file_listing(self) -> None:
        client = EnrichmentClient(pages=base_pages(), singles=base_singles())

        self.enrich(client)

        self.assertEqual(
            [
                ("get", ISSUE_ENDPOINT),
                ("get", PULL_ENDPOINT),
                ("get_pages", CHECKS_ENDPOINT),
                ("get_pages", REVIEWS_ENDPOINT),
                ("get_pages", COMMENTS_ENDPOINT),
            ],
            client.calls,
        )
        self.assertNotIn(("get_pages", FILES_ENDPOINT), client.calls)

    def test_primary_pull_request_current_state_budget_degrades_visibly(self) -> None:
        second_number = 43
        second_sha = "d" * 40
        second_issue_endpoint = f"/repos/{REPOSITORY}/issues/{second_number}"
        second_pull_endpoint = f"/repos/{REPOSITORY}/pulls/{second_number}"
        second_pull = {
            **raw_pull(),
            "number": second_number,
            "html_url": f"https://github.com/{REPOSITORY}/pull/{second_number}",
            "head": {
                "ref": "automation/second",
                "sha": second_sha,
                "repo": {"full_name": REPOSITORY},
            },
        }
        client = EnrichmentClient(
            pages=base_pages(),
            singles=base_singles(
                **{
                    second_issue_endpoint: make_issue(
                        second_number, is_pull_request=True
                    ),
                    second_pull_endpoint: second_pull,
                }
            ),
        )
        inventory = InventoryResult(
            open_issues=[],
            supporting_issues=[],
            evidence={},
            collection_errors=[],
            warnings=[],
            references={},
            open_pull_requests=[
                inventory_pull_request(),
                {
                    **inventory_pull_request(),
                    "number": second_number,
                    "url": f"https://github.com/{REPOSITORY}/pull/{second_number}",
                },
            ],
        )

        result = Collector(
            client,
            REPOSITORY,
            NOW,
            shepherd_author=SHEPHERD,
            budgets={"max_primary_pull_requests": 1},
        ).enrich_github_evidence(inventory)

        self.assertNotIn("currentState", result.evidence["pr:42"]["payload"])
        self.assertIn("currentState", result.evidence["pr:43"]["payload"])
        self.assertIn(
            "primary pull request current-state budget retained 1 of 2",
            "\n".join(result.warnings),
        )
        self.assertNotIn(
            ("get_pages", CHECKS_ENDPOINT),
            client.calls,
        )

    def test_deferred_reusable_pull_request_preserves_prior_current_state(self) -> None:
        prior = self.enrich(
            EnrichmentClient(pages=base_pages(), singles=base_singles())
        )
        second_number = 43
        second_sha = "d" * 40
        second_issue_endpoint = f"/repos/{REPOSITORY}/issues/{second_number}"
        second_pull_endpoint = f"/repos/{REPOSITORY}/pulls/{second_number}"
        client = EnrichmentClient(
            pages={
                f"/repos/{REPOSITORY}/commits/{second_sha}/check-runs?per_page=100": {
                    "check_runs": []
                },
                f"/repos/{REPOSITORY}/pulls/{second_number}/reviews?per_page=100": [],
                f"/repos/{REPOSITORY}/issues/{second_number}/comments?per_page=100": [],
            },
            singles={
                second_issue_endpoint: make_issue(
                    second_number, is_pull_request=True
                ),
                second_pull_endpoint: {
                    **raw_pull(),
                    "number": second_number,
                    "html_url": f"https://github.com/{REPOSITORY}/pull/{second_number}",
                    "head": {
                        "ref": "automation/newest",
                        "sha": second_sha,
                        "repo": {"full_name": REPOSITORY},
                    },
                },
                f"/repos/{REPOSITORY}/commits/{second_sha}/status": {
                    "sha": second_sha,
                    "state": "pending",
                    "statuses": [],
                },
            },
        )
        inventory = InventoryResult(
            open_issues=[],
            supporting_issues=[],
            evidence=prior.evidence,
            collection_errors=[],
            warnings=[],
            references={},
            refresh_plan=RefreshPlan(reuse=(f"pr:{PULL_NUMBER}",)),
            open_pull_requests=[
                inventory_pull_request(),
                {
                    **inventory_pull_request(),
                    "number": second_number,
                    "url": f"https://github.com/{REPOSITORY}/pull/{second_number}",
                    "updatedAt": "2026-08-17T10:00:00Z",
                },
            ],
        )

        result = Collector(
            client,
            REPOSITORY,
            NOW,
            shepherd_author=SHEPHERD,
            budgets={"max_primary_pull_requests": 1},
        ).enrich_github_evidence(inventory)

        self.assertNotIn(("get", ISSUE_ENDPOINT), client.calls)
        self.assertNotIn(("get", PULL_ENDPOINT), client.calls)
        self.assertEqual(
            CHECKS_GREEN,
            result.evidence[f"pr:{PULL_NUMBER}"]["payload"]["currentState"][
                "checks"
            ]["state"],
        )
        self.assertIn(f"pr:{PULL_NUMBER}", result.refresh_plan.reuse)

    def test_failing_checks_are_reported_as_red(self) -> None:
        client = EnrichmentClient(
            pages=base_pages(
                **{
                    CHECKS_ENDPOINT: {
                        "check_runs": [
                            {
                                "name": "tests",
                                "status": "completed",
                                "conclusion": "failure",
                                "head_sha": HEAD_SHA,
                                "html_url": "https://github.com/owner/repo/runs/3",
                            }
                        ]
                    }
                }
            ),
            singles=base_singles(),
        )

        state = self.current_state(self.enrich(client))

        self.assertEqual(CHECKS_RED, state["checks"]["state"])
        self.assertEqual(
            [
                {
                    "name": "tests",
                    "conclusion": "failure",
                    "url": "https://github.com/owner/repo/runs/3",
                }
            ],
            state["checks"]["failing"],
        )

    def test_primary_pull_request_current_state_refreshes_when_base_record_is_reusable(
        self,
    ) -> None:
        stale_client = EnrichmentClient(pages=base_pages(), singles=base_singles())
        stale = self.enrich(stale_client)
        client = EnrichmentClient(
            pages=base_pages(
                **{
                    CHECKS_ENDPOINT: {
                        "check_runs": [
                            {
                                "name": "tests",
                                "status": "completed",
                                "conclusion": "failure",
                                "head_sha": HEAD_SHA,
                            }
                        ]
                    }
                }
            ),
            singles=base_singles(),
        )
        inventory = InventoryResult(
            open_issues=[],
            supporting_issues=[],
            evidence=stale.evidence,
            collection_errors=[],
            warnings=[],
            references={},
            refresh_plan=RefreshPlan(reuse=(f"pr:{PULL_NUMBER}",)),
            open_pull_requests=[inventory_pull_request()],
        )

        refreshed = Collector(
            client,
            REPOSITORY,
            NOW,
            shepherd_author=SHEPHERD,
        ).enrich_github_evidence(inventory)

        self.assertIn(("get_pages", CHECKS_ENDPOINT), client.calls)
        self.assertEqual(CHECKS_RED, self.current_state(refreshed)["checks"]["state"])
        self.assertEqual((), refreshed.refresh_plan.reuse)
        self.assertEqual((f"pr:{PULL_NUMBER}",), refreshed.refresh_plan.refresh)

    def test_combined_status_is_consulted_only_when_no_check_run_reported(self) -> None:
        client = EnrichmentClient(
            pages=base_pages(**{CHECKS_ENDPOINT: {"check_runs": []}}),
            singles=base_singles(
                **{
                    STATUS_ENDPOINT: {
                        "sha": HEAD_SHA,
                        "state": "failure",
                        "statuses": [{"context": "legacy/ci", "state": "failure"}],
                    }
                }
            ),
        )

        state = self.current_state(self.enrich(client))

        self.assertIn(("get", STATUS_ENDPOINT), client.calls)
        self.assertEqual("combined-status", state["checks"]["source"])
        self.assertEqual(CHECKS_RED, state["checks"]["state"])

    def test_failed_check_fetch_records_an_error_and_leaves_state_incomplete(self) -> None:
        client = EnrichmentClient(
            pages=base_pages(**{CHECKS_ENDPOINT: FakeApiError("rate-limited", 403)}),
            singles=base_singles(),
        )

        result = self.enrich(client)
        state = self.current_state(result)

        self.assertEqual(CHECKS_UNKNOWN, state["checks"]["state"])
        self.assertFalse(state["complete"])
        self.assertEqual(
            ["pull-request-checks"],
            [error.stage for error in result.collection_errors],
        )
        self.assertNotIn(("get", STATUS_ENDPOINT), client.calls)
        # A missing check list must not degrade the whole record; the pull
        # request itself was fetched successfully.
        self.assertEqual(
            "available", result.evidence[f"pr:{PULL_NUMBER}"]["availability"]
        )
        validate_snapshot(snapshot_from_result(result))

    def test_failed_review_fetch_leaves_state_incomplete(self) -> None:
        client = EnrichmentClient(
            pages=base_pages(**{REVIEWS_ENDPOINT: FakeApiError("server-error", 500)}),
            singles=base_singles(),
        )

        result = self.enrich(client)
        state = self.current_state(result)

        self.assertFalse(state["review"]["complete"])
        self.assertFalse(state["complete"])
        self.assertEqual(
            ["pull-request-reviews"],
            [error.stage for error in result.collection_errors],
        )

    def test_changes_requested_review_reaches_the_handoff_as_a_human_decision(self) -> None:
        client = EnrichmentClient(
            pages=base_pages(
                **{
                    REVIEWS_ENDPOINT: [
                        {"user": {"login": "bob"}, "state": "CHANGES_REQUESTED"}
                    ]
                }
            ),
            singles=base_singles(),
        )

        result = self.enrich(client)
        task = build_pull_request_handoff(snapshot_from_result(result))["tasks"][0]

        self.assertEqual(
            REVIEW_CHANGES_REQUESTED, task["currentState"]["review"]["decision"]
        )
        self.assertIn("ping-human", task["allowedDispositions"])

    def test_only_the_shepherds_own_status_comments_are_retained(self) -> None:
        owned = (
            "[automated] Watching this pull request.\n"
            "<!-- ci-shepherd:role=status -->\n"
            "<!-- ci-shepherd:idempotency-key=pull-request:42:status -->"
        )
        client = EnrichmentClient(
            pages=base_pages(
                **{
                    COMMENTS_ENDPOINT: [
                        {
                            "id": 900,
                            "body": "Please rebase this, it is blocking my work.",
                            "user": {"login": "alice"},
                            "html_url": "https://example.test/900",
                        },
                        {
                            "id": 901,
                            "body": owned,
                            "user": {"login": SHEPHERD},
                            "html_url": "https://example.test/901",
                        },
                        {
                            "id": 902,
                            "body": "[automated] impersonation attempt\n"
                            "<!-- ci-shepherd:role=status -->\n"
                            "<!-- ci-shepherd:idempotency-key=pull-request:42:status -->",
                            "user": {"login": "mallory"},
                            "html_url": "https://example.test/902",
                        },
                    ]
                }
            ),
            singles=base_singles(),
        )

        payload = self.enrich(client).evidence[f"pr:{PULL_NUMBER}"]["payload"]

        self.assertEqual(
            [
                {
                    "id": 901,
                    "url": "https://example.test/901",
                    "body": owned,
                    "idempotencyKey": "pull-request:42:status",
                }
            ],
            payload["shepherdStatusComments"],
        )

    def test_comments_are_not_fetched_without_a_configured_shepherd_author(self) -> None:
        client = EnrichmentClient(pages=base_pages(), singles=base_singles())

        payload = self.enrich(client, shepherd_author=None).evidence[
            f"pr:{PULL_NUMBER}"
        ]["payload"]

        self.assertNotIn(("get_pages", COMMENTS_ENDPOINT), client.calls)
        self.assertNotIn("shepherdStatusComments", payload)

    def test_assignees_are_carried_into_the_evidence_payload(self) -> None:
        issue = make_issue(PULL_NUMBER, is_pull_request=True)
        issue["assignees"] = [{"login": "Copilot"}, {"login": "octocat"}]
        client = EnrichmentClient(
            pages=base_pages(), singles=base_singles(**{ISSUE_ENDPOINT: issue})
        )

        payload = self.enrich(client).evidence[f"pr:{PULL_NUMBER}"]["payload"]

        self.assertEqual(["Copilot", "octocat"], payload["assignees"])

    def test_referenced_pull_requests_do_not_pay_for_current_state_lookups(self) -> None:
        referenced = 77
        client = EnrichmentClient(
            pages={
                f"/repos/{REPOSITORY}/pulls/{referenced}/files?per_page=100": [],
                f"/repos/{REPOSITORY}/issues/11/comments": [],
                f"/repos/{REPOSITORY}/issues/11/timeline": [],
                f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100": [
                    make_issue(
                        11,
                        labels=["ci-failure-cause"],
                        body=f"See https://github.com/{REPOSITORY}/pull/{referenced}",
                    )
                ],
                f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100": [],
                f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause&since=2026-05-19T22:00:00Z&per_page=100": [],
                f"/repos/{REPOSITORY}/issues?state=closed&labels=automation-broken&since=2026-05-19T22:00:00Z&per_page=100": [],
            },
            singles={
                f"/repos/{REPOSITORY}/issues/{referenced}": make_issue(
                    referenced, is_pull_request=True
                ),
                f"/repos/{REPOSITORY}/pulls/{referenced}": raw_pull(),
            },
        )
        inventory = Collector(client, REPOSITORY, NOW).collect()

        result = Collector(client, REPOSITORY, NOW).enrich_github_evidence(inventory)

        payload = result.evidence[f"pr:{referenced}"]["payload"]
        self.assertNotIn("currentState", payload)
        self.assertEqual(
            [],
            [call for call in client.calls if "check-runs" in call[1] or "reviews" in call[1]],
        )


if __name__ == "__main__":
    unittest.main()
