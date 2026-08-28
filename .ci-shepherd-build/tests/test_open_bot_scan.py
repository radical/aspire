"""Public behaviour of the complete open bot-authored inventory scan.

The label and `creator=` queries only surface preconfigured bot logins, so any
other app that files issues stays invisible. GitHub search cannot express
"any app author" (`author:app/*` is rejected with HTTP 422), so the collector
pages the full open list and filters on `user.type == "Bot"`. These tests pin
the completeness and failure semantics of that scan.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from ci_shepherd.collector import Collector

from test_enrichment import EnrichmentClient, FakeApiError, REPOSITORY


NOW = datetime(2026, 8, 17, 22, 0, tzinfo=UTC)

OPEN_CAUSE = f"/repos/{REPOSITORY}/issues?state=open&labels=ci-failure-cause&per_page=100"
OPEN_AUTOMATION = (
    f"/repos/{REPOSITORY}/issues?state=open&labels=automation-broken&per_page=100"
)
OPEN_CREATOR = (
    f"/repos/{REPOSITORY}/issues?state=open&creator=github-actions[bot]&per_page=100"
)


def scan_page(page: int) -> str:
    return (
        f"/repos/{REPOSITORY}/issues?state=open&sort=updated&direction=desc"
        f"&per_page=100&page={page}"
    )


def item(
    number: int,
    *,
    login: str = "octocat",
    user_type: str = "User",
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    is_pull_request: bool = False,
    updated_at: str = "2026-08-02T00:00:00Z",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "number": number,
        "state": "open",
        "title": f"Item {number}",
        "body": "",
        "html_url": f"https://github.com/{REPOSITORY}/issues/{number}",
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": updated_at,
        "closed_at": None,
        "labels": [{"name": name} for name in (labels or [])],
        "user": {"login": login, "type": user_type},
        "assignees": [{"login": name} for name in (assignees or [])],
    }
    if is_pull_request:
        payload["pull_request"] = {
            "url": f"https://api.github.com/repos/{REPOSITORY}/pulls/{number}"
        }
    return payload


def bot(number: int, **kwargs: object) -> dict[str, object]:
    kwargs.setdefault("login", "aspire-repo-bot[bot]")
    kwargs.setdefault("user_type", "Bot")
    return item(number, **kwargs)  # type: ignore[arg-type]


def base_pages(**overrides: object) -> dict[str, object]:
    pages: dict[str, object] = {
        OPEN_CAUSE: [],
        OPEN_AUTOMATION: [],
        OPEN_CREATOR: [],
        f"/repos/{REPOSITORY}/issues?state=closed&labels=ci-failure-cause"
        f"&since={(NOW).isoformat().replace('+00:00', 'Z')}&per_page=100": [],
    }
    pages.update(overrides)
    return pages


def collect(
    scan_singles: dict[str, object],
    *,
    pages: dict[str, object] | None = None,
    budgets: dict[str, int] | None = None,
):
    client = EnrichmentClient(
        pages=pages if pages is not None else base_pages(),
        singles=scan_singles,
    )
    collector = Collector(client, REPOSITORY, NOW, budgets=budgets)
    return collector.collect(include_supporting=False, include_timeline=False), client


class OpenBotScanTests(unittest.TestCase):
    def test_scan_adopts_bot_author_not_covered_by_configured_creator_queries(
        self,
    ) -> None:
        result, _ = collect({scan_page(1): [bot(11)]})

        self.assertEqual([11], [issue["number"] for issue in result.open_issues])

    def test_scan_ignores_human_authored_items_without_target_labels(self) -> None:
        result, _ = collect({scan_page(1): [item(12), item(13, login="someone")]})

        self.assertEqual([], result.open_issues)

    def test_label_matched_human_items_remain_in_the_inventory(self) -> None:
        result, _ = collect(
            {scan_page(1): [item(14)]},
            pages=base_pages(**{OPEN_CAUSE: [item(9, labels=["ci-failure-cause"])]}),
        )

        self.assertEqual([9], [issue["number"] for issue in result.open_issues])

    def test_scan_sourced_pull_requests_record_the_bot_author_selection_reason(
        self,
    ) -> None:
        result, _ = collect({scan_page(1): [bot(21, is_pull_request=True)]})

        self.assertEqual(
            [(21, ["bot-author"])],
            [
                (pull["number"], pull["selectionReasons"])
                for pull in result.open_pull_requests
            ],
        )

    def test_copilot_assigned_bot_items_are_rejected_not_inventoried(self) -> None:
        result, _ = collect(
            {
                scan_page(1): [
                    bot(31, assignees=["Copilot"]),
                    bot(32, assignees=["Copilot"], is_pull_request=True),
                ]
            }
        )

        self.assertEqual([], result.open_issues)
        self.assertEqual([], result.open_pull_requests)
        self.assertEqual(
            [
                {"number": 31, "targetKind": "issue", "reason": "assigned-to-copilot"},
                {
                    "number": 32,
                    "targetKind": "pull-request",
                    "reason": "assigned-to-copilot",
                },
            ],
            sorted(result.rejected_candidates, key=lambda entry: entry["number"]),
        )

    def test_short_final_page_reports_a_complete_scan(self) -> None:
        result, client = collect(
            {scan_page(1): [bot(41)], scan_page(2): [bot(42)]}
        )

        self.assertEqual(
            {
                "status": "complete",
                "complete": True,
                "scannedPages": 1,
                "pageBudget": 40,
                "itemBudget": 250,
                "botAuthoredFound": 1,
                "botAuthoredAdopted": 1,
                "detail": None,
            },
            result.open_bot_scan,
        )
        self.assertNotIn(("get", scan_page(2)), client.calls)
        self.assertEqual([], result.warnings)

    def test_exhausting_the_page_budget_reports_a_truncated_scan(self) -> None:
        full_page = [bot(number) for number in range(100, 200)]
        result, client = collect(
            {scan_page(1): full_page, scan_page(2): full_page},
            budgets={"max_open_scan_pages": 2},
        )

        self.assertEqual("truncated", result.open_bot_scan["status"])
        self.assertFalse(result.open_bot_scan["complete"])
        self.assertEqual(2, result.open_bot_scan["scannedPages"])
        self.assertEqual(
            [("get", scan_page(1)), ("get", scan_page(2))],
            [call for call in client.calls if "sort=updated" in call[1]],
        )
        self.assertEqual(
            ["open bot-authored inventory is incomplete (truncated): "
             "open scan stopped after the 2 page budget"],
            result.warnings,
        )

    def test_item_budget_keeps_the_most_recently_updated_items(self) -> None:
        result, _ = collect(
            {
                scan_page(1): [
                    bot(51, updated_at="2026-08-09T00:00:00Z"),
                    bot(52, updated_at="2026-08-08T00:00:00Z"),
                    bot(53, updated_at="2026-08-07T00:00:00Z"),
                ]
            },
            budgets={"max_bot_authored_open": 2},
        )

        self.assertEqual([51, 52], [issue["number"] for issue in result.open_issues])
        self.assertEqual("truncated", result.open_bot_scan["status"])
        self.assertEqual(3, result.open_bot_scan["botAuthoredFound"])
        self.assertEqual(2, result.open_bot_scan["botAuthoredAdopted"])

    def test_page_failure_degrades_without_discarding_earlier_results(self) -> None:
        result, _ = collect(
            {
                scan_page(1): [bot(number) for number in range(100, 200)],
                scan_page(2): FakeApiError("502 Bad Gateway"),
            },
            pages=base_pages(**{OPEN_CAUSE: [item(9, labels=["ci-failure-cause"])]}),
        )

        self.assertEqual(
            [9] + list(range(100, 200)),
            sorted(issue["number"] for issue in result.open_issues),
        )
        self.assertEqual("failed", result.open_bot_scan["status"])
        self.assertEqual(1, result.open_bot_scan["scannedPages"])
        self.assertEqual(
            [("open-bot-scan", scan_page(2))],
            [
                (error.stage, error.endpoint)
                for error in result.collection_errors
                if error.stage == "open-bot-scan"
            ],
        )

    def test_first_page_failure_is_not_fatal_to_the_collection(self) -> None:
        result, _ = collect(
            {scan_page(1): FakeApiError("500 Server Error")},
            pages=base_pages(**{OPEN_CAUSE: [item(9, labels=["ci-failure-cause"])]}),
        )

        self.assertEqual([9], [issue["number"] for issue in result.open_issues])
        self.assertEqual("failed", result.open_bot_scan["status"])
        self.assertEqual(0, result.open_bot_scan["scannedPages"])

    def test_unexpected_payload_shape_is_reported_as_a_failed_scan(self) -> None:
        result, _ = collect({scan_page(1): {"items": []}})

        self.assertEqual("failed", result.open_bot_scan["status"])
        self.assertEqual(
            "Unexpected open issue list payload shape",
            result.open_bot_scan["detail"],
        )

    def test_scan_issues_only_bounded_get_requests_against_a_fixed_endpoint(
        self,
    ) -> None:
        _, client = collect({scan_page(1): [bot(61)]})

        scan_calls = [call for call in client.calls if "sort=updated" in call[1]]
        self.assertEqual([("get", scan_page(1))], scan_calls)

    def test_scan_record_survives_github_evidence_enrichment(self) -> None:
        client = EnrichmentClient(pages=base_pages(), singles={scan_page(1): []})
        collector = Collector(client, REPOSITORY, NOW)
        result = collector.collect(include_supporting=False, include_timeline=False)

        enriched = collector.enrich_github_evidence(result)

        self.assertEqual(result.open_bot_scan, enriched.open_bot_scan)

    def test_non_bot_account_types_are_never_adopted(self) -> None:
        result, _ = collect(
            {
                scan_page(1): [
                    item(71, login="an-org", user_type="Organization"),
                    item(72, login="ghost", user_type="Mannequin"),
                    item(73, login="nobody", user_type=""),
                ]
            }
        )

        self.assertEqual([], result.open_issues)
        self.assertEqual(0, result.open_bot_scan["botAuthoredFound"])


if __name__ == "__main__":
    unittest.main()
