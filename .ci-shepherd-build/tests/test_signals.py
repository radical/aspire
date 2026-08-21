from __future__ import annotations

from dataclasses import FrozenInstanceError
import unittest

from ci_shepherd.signals import Occurrence, extract_issue_signals, select_references


ISSUE_NUMBER = 19456
SOURCE_EVIDENCE_ID = f"issue:{ISSUE_NUMBER}"
SOURCE_URL = f"https://github.com/microsoft/aspire/issues/{ISSUE_NUMBER}"
REPOSITORY = "microsoft/aspire"


def extract(text: str):
    return extract_issue_signals(
        ISSUE_NUMBER,
        SOURCE_EVIDENCE_ID,
        SOURCE_URL,
        text,
        REPOSITORY,
    )


def occurrence_table(rows: tuple[tuple[str, int, str, int], ...]) -> str:
    return (
        "## Occurrences\n"
        "| Date | Build | Job | PR |\n"
        "|------|-------|-----|----|\n"
        + "\n".join(
            f"| {date_value} | [{run_id}](https://github.com/microsoft/aspire/actions/runs/{run_id}) "
            f"| {job} | #{pull_request} |"
            for date_value, run_id, job, pull_request in rows
        )
    )


class IssueSignalTests(unittest.TestCase):
    def test_explicit_resolution_run_precedes_full_occurrence_run_budget(self) -> None:
        rows = tuple(
            (
                f"2026-08-{day:02d}",
                100 + day,
                "Tests / Hosting.Azure",
                19000 + day,
            )
            for day in range(1, 13)
        )
        signals = extract(
            occurrence_table(rows)
            + "\n\n"
            "## Resolution\n\n"
            "The post-fix run succeeded: "
            "https://github.com/microsoft/aspire/actions/runs/999."
        )

        selection = select_references(
            signals.references,
            signals.occurrences,
            max_run_refs_per_issue=12,
            max_issue_refs_per_issue=20,
            max_commit_refs_per_issue=3,
        )

        self.assertEqual(
            [999, *[row[1] for row in reversed(rows[1:])]],
            [
                reference["runId"]
                for reference in selection.selected
                if reference["targetType"] == "workflow-run"
            ],
        )
        self.assertEqual(
            [(101, "max_run_refs_per_issue")],
            [
                (reference["runId"], reference["exclusionReason"])
                for reference in selection.excluded
                if reference["targetType"] == "workflow-run"
            ],
        )

    def test_duplicate_run_provenance_consumes_one_unique_target_slot(self) -> None:
        body_signals = extract(
            "Run https://github.com/microsoft/aspire/actions/runs/100 "
            "and https://github.com/microsoft/aspire/actions/runs/200."
        )
        comment_signals = extract_issue_signals(
            ISSUE_NUMBER,
            f"{SOURCE_EVIDENCE_ID}:comment:1",
            f"{SOURCE_URL}#issuecomment-1",
            "Run https://github.com/microsoft/aspire/actions/runs/100.",
            REPOSITORY,
        )

        selection = select_references(
            body_signals.references + comment_signals.references,
            (),
            max_run_refs_per_issue=2,
            max_issue_refs_per_issue=5,
            max_commit_refs_per_issue=3,
        )

        self.assertEqual(
            [(100, SOURCE_EVIDENCE_ID), (100, f"{SOURCE_EVIDENCE_ID}:comment:1"), (200, SOURCE_EVIDENCE_ID)],
            sorted(
                (
                    reference["runId"],
                    reference["sourceEvidenceId"],
                )
                for reference in selection.selected
            ),
        )
        self.assertEqual((), selection.excluded)

    def test_duplicate_issue_provenance_consumes_one_unique_target_slot(self) -> None:
        body_signals = extract("Related issues #100 and #200.")
        comment_signals = extract_issue_signals(
            ISSUE_NUMBER,
            f"{SOURCE_EVIDENCE_ID}:comment:1",
            f"{SOURCE_URL}#issuecomment-1",
            "Also tracked by #100.",
            REPOSITORY,
        )

        selection = select_references(
            body_signals.references + comment_signals.references,
            (),
            max_run_refs_per_issue=12,
            max_issue_refs_per_issue=2,
            max_commit_refs_per_issue=3,
        )

        self.assertEqual(
            [(100, SOURCE_EVIDENCE_ID), (100, f"{SOURCE_EVIDENCE_ID}:comment:1"), (200, SOURCE_EVIDENCE_ID)],
            sorted(
                (
                    reference["targetNumber"],
                    reference["sourceEvidenceId"],
                )
                for reference in selection.selected
            ),
        )
        self.assertEqual((), selection.excluded)

    def test_issue_and_pull_request_for_same_number_consume_one_shared_target_slot(self) -> None:
        signals = extract(
            "Issue https://github.com/microsoft/aspire/issues/100, "
            "pull request https://github.com/microsoft/aspire/pull/100, "
            "and issue https://github.com/microsoft/aspire/issues/101."
        )

        selection = select_references(
            signals.references,
            (),
            max_run_refs_per_issue=12,
            max_issue_refs_per_issue=1,
            max_commit_refs_per_issue=3,
        )

        self.assertEqual(
            [("issue", 100), ("pull-request", 100)],
            [
                (reference["targetType"], reference["targetNumber"])
                for reference in selection.selected
            ],
        )
        self.assertEqual(
            [("issue", 101, "max_issue_refs_per_issue")],
            [
                (
                    reference["targetType"],
                    reference["targetNumber"],
                    reference["exclusionReason"],
                )
                for reference in selection.excluded
            ],
        )

    def test_issue_18720_selects_twelve_occurrence_runs_with_many_issue_references(self) -> None:
        rows = (
            ("2026-07-10", 29060131780, "Tests / Hosting.Azure / Hosting.Azure (windows-latest)", 18696),
            ("2026-07-18", 29622211818, "Tests / Hosting.Dotnet / Hosting.Dotnet (windows-latest)", 18813),
            ("2026-07-21", 29828970048, "Tests / Azure.Search.Documents / Azure.Search.Documents (windows-latest)", 18816),
            ("2026-07-22", 29884310827, "Tests / Hosting.PostgreSQL / Hosting.PostgreSQL (windows-latest)", 18853),
            ("2026-07-27", 30225781652, "Tests / Hosting.Browsers / Hosting.Browsers (windows-latest)", 18839),
            ("2026-08-04", 30906478483, "Tests / Hosting-1 / Hosting-1 (windows-latest)", 1056),
            ("2026-08-05", 31047577163, "Tests / Hosting-1 / Hosting-1 (windows-latest)", 18850),
            ("2026-08-11", 31481900781, "Tests / Hosting-1 / Hosting-1 (windows-latest)", 19222),
            ("2026-08-12", 31567169550, "Tests / Hosting.Sdk / Hosting.Sdk (windows-latest)", 19276),
            ("2026-08-12", 31574459556, "Tests / Hosting.Qdrant / Hosting.Qdrant (windows-latest)", 19127),
            ("2026-08-13", 31654765605, "Tests / Templates-BuildAndRunTemplateTests (windows-latest)", 19321),
            ("2026-08-13", 31739682865, "Tests / Cli / Cli (windows-latest)", 16842),
        )
        signals = extract(
            occurrence_table(rows)
            + "\n\n"
            "Related incidents: #18001 #18002 #18003 #18004 #18005 #18006 #18007."
        )

        selection = select_references(
            signals.references,
            signals.occurrences,
            max_run_refs_per_issue=12,
            max_issue_refs_per_issue=5,
            max_commit_refs_per_issue=3,
        )

        self.assertEqual(
            [row[1] for row in reversed(rows)],
            [
                reference["runId"]
                for reference in selection.selected
                if reference["targetType"] == "workflow-run"
            ],
        )
        self.assertEqual(
            5,
            sum(
                reference["targetType"] in {"issue", "pull-request"}
                for reference in selection.selected
            ),
        )
        self.assertGreater(
            sum(
                reference["targetType"] in {"issue", "pull-request"}
                for reference in selection.excluded
            ),
            5,
        )

    def test_issue_18592_and_19150_occurrence_patterns_preserve_all_runs(self) -> None:
        cases = {
            18592: (
                ("2026-07-01", 28425753079, "Tests / Hosting.Maui / Hosting.Maui (windows-latest)", 18549),
                ("2026-08-04", 30899298287, "Tests / Hosting.Maui / Hosting.Maui (windows-latest)", 1056),
                ("2026-08-05", 31047577163, "Tests / Hosting-1 / Hosting-1 (windows-latest)", 18850),
                ("2026-08-13", 31665454621, "Tests / Hosting.Maui / Hosting.Maui (windows-latest)", 19329),
                ("2026-08-13", 31735085796, "Tests / Hosting.PostgreSQL / Hosting.PostgreSQL (windows-latest)", 17811),
                ("2026-08-18", 32166536627, "Tests / Hosting.Maui / Hosting.Maui (windows-latest)", 19068),
            ),
            19150: (
                ("2026-08-07", 31203621605, "Tests / Hosting.Azure / Hosting.Azure (windows-latest)", 19090),
                ("2026-08-11", 31468992084, "Tests / Cli / Cli (windows-latest)", 19215),
                ("2026-08-17", 32053958727, "Tests / Hosting-2 / Hosting-2 (8-core-ubuntu-latest)", 19400),
            ),
        }

        for issue_number, rows in cases.items():
            with self.subTest(issue=issue_number):
                signals = extract(occurrence_table(rows))
                selection = select_references(
                    signals.references,
                    signals.occurrences,
                    max_run_refs_per_issue=12,
                    max_issue_refs_per_issue=5,
                    max_commit_refs_per_issue=3,
                )

                self.assertEqual(
                    [row[1] for row in reversed(rows)],
                    [
                        reference["runId"]
                        for reference in selection.selected
                        if reference["targetType"] == "workflow-run"
                    ],
                )

    def test_select_references_orders_by_decision_value_and_records_exclusions(self) -> None:
        signals = extract(
            """\
Build: https://github.com/microsoft/aspire/actions/runs/100
Pull request: #19090
Generic related issue #18000.
Commit: fedcba7654321

## Resolution

Fixed by #19148 and https://github.com/microsoft/aspire/commit/abcdef1234567.
The post-fix main run succeeded:
https://github.com/microsoft/aspire/actions/runs/300

## Occurrences

| Date | Build | Job | PR |
|------|-------|-----|----|
| 2026-08-07 | [201](https://github.com/microsoft/aspire/actions/runs/201) | Tests / Hosting.Azure | #19090 |
| 2026-08-08 | [202](https://github.com/microsoft/aspire/actions/runs/202) | Tests / Hosting.Azure | #19091 |
"""
        )

        selection = select_references(
            signals.references,
            signals.occurrences,
            max_run_refs_per_issue=3,
            max_issue_refs_per_issue=2,
            max_commit_refs_per_issue=1,
        )

        selected_targets = [
            (
                reference["targetType"],
                reference.get("runId", reference.get("targetNumber", reference.get("sha"))),
            )
            for reference in selection.selected
        ]
        self.assertEqual(
            [
                ("workflow-run", 300),
                ("workflow-run", 202),
                ("workflow-run", 201),
                ("issue", 19148),
                ("commit", "abcdef1234567"),
                ("pull-request", 19090),
            ],
            selected_targets,
        )
        self.assertEqual(
            {
                ("workflow-run", 100, "max_run_refs_per_issue"),
                ("commit", "fedcba7654321", "max_commit_refs_per_issue"),
                ("pull-request", 19091, "max_issue_refs_per_issue"),
                ("issue", 18000, "max_issue_refs_per_issue"),
            },
            {
                (
                    reference["targetType"],
                    reference.get("runId", reference.get("targetNumber", reference.get("sha"))),
                    reference["exclusionReason"],
                )
                for reference in selection.excluded
            },
        )
        for reference in selection.excluded:
            self.assertEqual(ISSUE_NUMBER, reference["sourceIssueNumber"])
            self.assertEqual(SOURCE_EVIDENCE_ID, reference["sourceEvidenceId"])
            self.assertEqual(SOURCE_URL, reference["sourceUrl"])

    def test_resolution_provenance_wins_when_target_was_already_referenced(self) -> None:
        signals = extract(
            """\
Possibly related to #19148.

## Resolution

Fixed by #19148.
"""
        )

        self.assertEqual(1, len(signals.references))
        self.assertEqual("explicit-resolution", signals.references[0]["decisionValue"])

    def test_extracts_actual_composite_ci_failure_template(self) -> None:
        text = """\
<!-- ci-failure-cause:hosting-testing-httpclientgettest-timeout -->

## Build Information

Build: https://github.com/microsoft/aspire/actions/runs/32091076460
Build error leg or test failing: Tests / Hosting.Testing / Hosting.Testing (ubuntu-latest) / `Aspire.Hosting.Testing.Tests.TestingFactoryTests.HttpClientGetTest`
Pull request: #1424

## Error Message

```
Polly.Timeout.TimeoutRejectedException : Timed out. ---- System.TimeoutException : The operation timed out.
error CS0123: Example compiler failure
Response status code does not indicate success: 429 (Too Many Requests).
Process completed with exit code -1073741502. This corresponds to 0xC0000142 (STATUS_DLL_INIT_FAILED).
```

## Description

Intermittent failure.

**Type**: flaky-test

## Occurrences

| Date | Build | Job | PR |
|------|-------|-----|----|
| 2026-08-18 | [32091076460](https://github.com/microsoft/aspire/actions/runs/32091076460) | Tests / Hosting.Testing / Hosting.Testing (ubuntu-latest) | #1424 |
"""

        signals = extract(text)

        self.assertEqual(
            [
                (
                    "ci-failure-cause",
                    "hosting-testing-httpclientgettest-timeout",
                    "hosting-testing-httpclientgettest-timeout",
                    "html-comment",
                    SOURCE_EVIDENCE_ID,
                )
            ],
            [
                (
                    marker["key"],
                    marker["raw"],
                    marker["normalized"],
                    marker["method"],
                    marker["sourceEvidenceId"],
                )
                for marker in signals.markers
            ],
        )
        facts = {
            (fact["field"], fact["raw"], fact["normalized"], fact["method"])
            for fact in signals.facts
        }
        self.assertIn(
            (
                "sourceRun",
                "https://github.com/microsoft/aspire/actions/runs/32091076460",
                "https://github.com/microsoft/aspire/actions/runs/32091076460",
                "build-run",
            ),
            facts,
        )
        self.assertIn(
            (
                "testName",
                "Aspire.Hosting.Testing.Tests.TestingFactoryTests.HttpClientGetTest",
                "aspire.hosting.testing.tests.testingfactorytests.httpclientgettest",
                "build-error-leg",
            ),
            facts,
        )
        self.assertIn(
            (
                "job",
                "Tests / Hosting.Testing / Hosting.Testing (ubuntu-latest)",
                "tests / hosting.testing / hosting.testing (ubuntu-latest)",
                "build-error-leg",
            ),
            facts,
        )
        self.assertIn(("triggeringPullRequest", "#1424", "1424", "triggering-pull-request"), facts)
        self.assertIn(("failureType", "flaky-test", "flaky-test", "labelled-type"), facts)
        self.assertIn(
            ("causeId", "hosting-testing-httpclientgettest-timeout", "hosting-testing-httpclientgettest-timeout", "html-comment"),
            facts,
        )
        self.assertIn(
            ("exceptionType", "Polly.Timeout.TimeoutRejectedException", "polly.timeout.timeoutrejectedexception", "error-message"),
            facts,
        )
        self.assertIn(
            ("exceptionType", "System.TimeoutException", "system.timeoutexception", "error-message"),
            facts,
        )
        self.assertIn(("errorCode", "CS0123", "CS0123", "compiler-error-code"), facts)
        self.assertIn(("errorCode", "429", "429", "http-status"), facts)
        self.assertIn(("errorCode", "-1073741502", "-1073741502", "exit-code"), facts)
        self.assertIn(("errorCode", "0xC0000142", "0XC0000142", "hex-exit-code"), facts)
        self.assertEqual(
            (
                Occurrence(
                    date="2026-08-18",
                    source_run=32091076460,
                    run_url="https://github.com/microsoft/aspire/actions/runs/32091076460",
                    job="Tests / Hosting.Testing / Hosting.Testing (ubuntu-latest)",
                    pull_request=1424,
                ),
            ),
            signals.occurrences,
        )

    def test_extracts_job_from_build_error_leg_without_test(self) -> None:
        signals = extract("Build error leg: Build / Windows / Windows (windows-latest)")

        self.assertEqual(
            [
                (
                    "job",
                    "Build / Windows / Windows (windows-latest)",
                    "build / windows / windows (windows-latest)",
                    "build-error-leg",
                )
            ],
            [
                (fact["field"], fact["raw"], fact["normalized"], fact["method"])
                for fact in signals.facts
            ],
        )

    def test_extracts_parameterized_xunit_test_from_build_error_leg(self) -> None:
        signals = extract(
            'Build error leg or test failing: Tests / Linux / `Can_connect(resource: "redis", port: 6379)`'
        )

        self.assertEqual(
            [
                (
                    "job",
                    "Tests / Linux",
                    "tests / linux",
                    "build-error-leg",
                ),
                (
                    "testName",
                    'Can_connect(resource: "redis", port: 6379)',
                    'can_connect(resource: "redis", port: 6379)',
                    "build-error-leg",
                ),
            ],
            [
                (fact["field"], fact["raw"], fact["normalized"], fact["method"])
                for fact in signals.facts
            ],
        )

    def test_extracts_js_path_like_test_from_build_error_leg(self) -> None:
        signals = extract(
            "Build error leg or test failing: Tests / JavaScript / `test/foo.spec.ts > checkout flow [chromium]`"
        )

        self.assertEqual(
            [
                (
                    "job",
                    "Tests / JavaScript",
                    "tests / javascript",
                    "build-error-leg",
                ),
                (
                    "testName",
                    "test/foo.spec.ts > checkout flow [chromium]",
                    "test/foo.spec.ts > checkout flow [chromium]",
                    "build-error-leg",
                ),
            ],
            [
                (fact["field"], fact["raw"], fact["normalized"], fact["method"])
                for fact in signals.facts
            ],
        )

    def test_run_markdown_link_does_not_create_issue_reference(self) -> None:
        signals = extract(
            "CI failed in [run #2960](https://github.com/microsoft/aspire/actions/runs/29787895542).\n"
            "<!-- run:29787895542 -->"
        )

        self.assertEqual(
            [("workflow-run", 29787895542)],
            [(reference["targetType"], reference["runId"]) for reference in signals.references],
        )

    def test_generic_markdown_link_label_and_url_do_not_create_local_issues(self) -> None:
        signals = extract(
            "See [tracking issue #2960](https://example.test/tickets/#777) and "
            "[the docs](https://example.test/#888)."
        )

        self.assertEqual((), signals.references)

    def test_reference_style_markdown_link_label_does_not_create_local_issue(self) -> None:
        signals = extract(
            "See [tracking issue #2960][tracker].\n\n"
            "[tracker]: https://example.test/tickets/#777"
        )

        self.assertEqual((), signals.references)

    def test_nested_markdown_link_label_does_not_create_local_issue(self) -> None:
        signals = extract("See [the [tracking issue #2960]](https://example.test/tickets/2960).")

        self.assertEqual((), signals.references)

    def test_unterminated_repeated_markdown_links_preserve_following_prose_reference(self) -> None:
        signals = extract(("[x](" * 5_000) + "Related to #4242.")

        self.assertEqual(
            [4242],
            [
                reference["targetNumber"]
                for reference in signals.references
                if reference["targetType"] == "issue"
            ],
        )

    def test_genuine_local_issue_reference_is_retained(self) -> None:
        signals = extract("Related to #2960.")

        self.assertEqual(
            [
                {
                    "sourceIssueNumber": ISSUE_NUMBER,
                    "sourceEvidenceId": SOURCE_EVIDENCE_ID,
                    "sourceUrl": SOURCE_URL,
                    "targetType": "issue",
                    "targetRepository": REPOSITORY,
                    "targetNumber": 2960,
                    "targetUrl": "https://github.com/microsoft/aspire/issues/2960",
                    "extractionMethod": "local-issue",
                }
            ],
            [dict(reference) for reference in signals.references],
        )

    def test_generic_local_issue_references_ignore_code(self) -> None:
        cases = (
            ("fenced error log", "```\nFailure while processing #4242\n```", []),
            ("inline code", "The error was `Failure while processing #4242`.", []),
            ("prose", "Related to #4242.", [4242]),
        )

        for name, text, expected_issue_numbers in cases:
            with self.subTest(name=name):
                signals = extract(text)

                self.assertEqual(
                    expected_issue_numbers,
                    [
                        reference["targetNumber"]
                        for reference in signals.references
                        if reference["targetType"] == "issue"
                    ],
                )

    def test_code_masking_preserves_structured_and_url_references(self) -> None:
        signals = extract(
            "`Ignore #4242 here.`\n"
            "Pull request: #18835\n"
            "[Related issue](https://github.com/other/repo/issues/77)"
        )

        self.assertEqual(
            {
                ("pull-request", REPOSITORY, 18835, "triggering-pull-request"),
                ("issue", "other/repo", 77, "full-issue-url"),
            },
            {
                (
                    reference["targetType"],
                    reference["targetRepository"],
                    reference["targetNumber"],
                    reference["extractionMethod"],
                )
                for reference in signals.references
            },
        )

    def test_triggering_pull_request_is_not_duplicated_as_an_issue(self) -> None:
        signals = extract("Pull request: #18835")

        self.assertEqual(
            [("pull-request", 18835, "triggering-pull-request")],
            [
                (
                    reference["targetType"],
                    reference["targetNumber"],
                    reference["extractionMethod"],
                )
                for reference in signals.references
            ],
        )

    def test_only_whitelisted_html_markers_are_recognized(self) -> None:
        signals = extract(
            "\n".join(
                (
                    "<!-- ci-failure-cause:cause-a -->",
                    "<!-- ci-failure:ci.yml:push:main -->",
                    "<!-- autoclose:true -->",
                    "<!-- run:123 -->",
                    "<!-- automation-broken:nightly.yml -->",
                    "<!-- gh-aw-agentic-workflow:nightly.md -->",
                    "<!-- ci-shepherd:test=old-format -->",
                    "<!-- gh-aw-expires:2026-08-20 -->",
                    "<!-- gh-aw-failure-issue:123 -->",
                    "<!-- unknown-marker:value -->",
                )
            )
        )

        self.assertEqual(
            [
                "autoclose",
                "automation-broken",
                "ci-failure",
                "ci-failure-cause",
                "gh-aw-agentic-workflow",
                "gh-aw-expires",
                "gh-aw-failure-issue",
                "run",
            ],
            [marker["key"] for marker in signals.markers],
        )

    def test_workflow_identity_marker_does_not_synthesize_cause_or_run_evidence(self) -> None:
        signals = extract("<!-- ci-failure:ci.yml:push:main -->")

        self.assertEqual((), signals.facts)
        self.assertEqual((), signals.references)

    def test_bare_url_fragment_does_not_create_local_issue_reference(self) -> None:
        signals = extract(
            "Logs: https://example.test/build/a-#111 and "
            "https://example.test/search?q=a&b=-#222"
        )

        self.assertEqual((), signals.references)

    def test_references_preserve_full_urls_and_labelled_commits(self) -> None:
        signals = extract(
            "\n".join(
                (
                    "https://github.com/other/repo/issues/12",
                    "https://github.com/other/repo/pull/13",
                    "https://github.com/other/repo/actions/runs/14",
                    "https://github.com/other/repo/commit/ABCDEF1234567",
                    "Commit: fedcba7654321",
                )
            )
        )

        self.assertEqual(
            {
                ("issue", "other/repo", 12, "full-issue-url"),
                ("pull-request", "other/repo", 13, "full-pull-url"),
                ("workflow-run", "other/repo", 14, "actions-run-url"),
                ("commit", "other/repo", "abcdef1234567", "commit-url"),
                ("commit", REPOSITORY, "fedcba7654321", "labelled-commit"),
            },
            {
                (
                    reference["targetType"],
                    reference["targetRepository"],
                    reference.get("targetNumber", reference.get("runId", reference.get("sha"))),
                    reference["extractionMethod"],
                )
                for reference in signals.references
            },
        )

    def test_oversized_github_ids_do_not_crash_or_create_references(self) -> None:
        oversized_id = "9" * 5_000
        signals = extract(
            "\n".join(
                (
                    f"https://github.com/other/repo/issues/{oversized_id}",
                    f"https://github.com/other/repo/pull/{oversized_id}",
                    f"https://github.com/other/repo/actions/runs/{oversized_id}",
                    f"Related to #{oversized_id}",
                    f"Pull request: #{oversized_id}",
                    f"Build: https://github.com/microsoft/aspire/actions/runs/{oversized_id}",
                    f"<!-- run:{oversized_id} -->",
                    "## Occurrences",
                    "| Date | Build | Job | PR |",
                    "|------|-------|-----|----|",
                    (
                        "| 2026-08-18 "
                        f"| [{oversized_id}](https://github.com/microsoft/aspire/actions/runs/{oversized_id}) "
                        f"| Linux | #{oversized_id} |"
                    ),
                )
            )
        )

        self.assertEqual((), signals.occurrences)
        self.assertEqual((), signals.references)

    def test_normalizes_deduplicates_and_preserves_ambiguous_facts(self) -> None:
        signals = extract(
            """\
<!-- ci-failure-cause: Mixed   Cause -->
<!-- ci-failure-cause:mixed cause -->
Build: https://github.com/microsoft/aspire/actions/runs/11
Build: https://github.com/microsoft/aspire/actions/runs/12
**Type**: Flaky-Test
**Type**: flaky-test
## Error Message
```
System.TimeoutException and System.TimeoutException
```
"""
        )

        self.assertEqual(1, len(signals.markers))
        source_runs = [fact["normalized"] for fact in signals.facts if fact["field"] == "sourceRun"]
        self.assertEqual(
            [
                "https://github.com/microsoft/aspire/actions/runs/11",
                "https://github.com/microsoft/aspire/actions/runs/12",
            ],
            source_runs,
        )
        self.assertEqual(1, sum(fact["field"] == "failureType" for fact in signals.facts))
        self.assertEqual(1, sum(fact["field"] == "exceptionType" for fact in signals.facts))

    def test_occurrences_ignore_malformed_rows_and_deduplicate(self) -> None:
        signals = extract(
            """\
## Occurrences
| Date | Build | Job | PR |
|------|-------|-----|----|
| 2026-08-18 | [21](https://github.com/microsoft/aspire/actions/runs/21) | Linux | #8 |
| 2026-08-18 | [21](https://github.com/microsoft/aspire/actions/runs/21) | Linux | #8 |
| not-a-date | [22](https://github.com/microsoft/aspire/actions/runs/22) | Linux | #8 |
| 2026-08-19 | [wrong](https://github.com/microsoft/aspire/actions/runs/23) | Linux | #8 |
| 2026-08-20 | [24](https://github.com/microsoft/aspire/actions/runs/24) |  | #8 |
| 2026-08-21 | [25](https://github.com/microsoft/aspire/actions/runs/25) | Linux | issue 8 |
| too | few | cells |
## Next section
| 2026-08-22 | [26](https://github.com/microsoft/aspire/actions/runs/26) | Linux | N/A |
"""
        )

        self.assertEqual(
            (
                Occurrence(
                    date="2026-08-18",
                    source_run=21,
                    run_url="https://github.com/microsoft/aspire/actions/runs/21",
                    job="Linux",
                    pull_request=8,
                ),
            ),
            signals.occurrences,
        )
        self.assertNotIn(
            ("issue", 8),
            [
                (reference["targetType"], reference.get("targetNumber"))
                for reference in signals.references
            ],
        )

    def test_occurrence_ledger_recognizes_complete_four_column_schema(self) -> None:
        signals = extract(
            """\
## Occurrences
| Date | Build | Job | PR |
|------|-------|-----|----|
| 2026-08-18 | [21](https://github.com/microsoft/aspire/actions/runs/21) | Linux | #8 |
"""
        )

        self.assertEqual(
            {
                "source": "body-table",
                "schema": "occurrences-v1",
                "schemaRecognized": True,
                "sourceRecordCount": 1,
                "parsedRowCount": 1,
                "complete": True,
            },
            signals.occurrence_ledger.as_record(),
        )

    def test_occurrence_ledger_recognizes_five_column_schema_and_annotated_merge(self) -> None:
        signals = extract(
            """\
**Type:** `main-repository-breakage`

## Occurrences
| Date | Build | Branch | Job | Triggering merge |
|---|---|---|---|---|
| 2026-08-07 | [31203621605](https://github.com/microsoft/aspire/actions/runs/31203621605) | `main` | Tests / Hosting.Azure | #19090 (unrelated) |
"""
        )

        self.assertEqual("occurrences-v2", signals.occurrence_ledger.schema)
        self.assertTrue(signals.occurrence_ledger.complete)
        self.assertEqual(19090, signals.occurrences[0].pull_request)
        self.assertIn(
            ("failureType", "main-repository-breakage"),
            [
                (fact["field"], fact["normalized"])
                for fact in signals.facts
            ],
        )

    def test_occurrence_ledger_rejects_unrecognized_or_partially_parsed_tables(self) -> None:
        unrecognized = extract(
            """\
## Occurrences
| When | Build |
|---|---|
| Yesterday | 21 |
"""
        )
        partial = extract(
            """\
## Occurrences
| Date | Build | Job | PR |
|------|-------|-----|----|
| 2026-08-18 | [21](https://github.com/microsoft/aspire/actions/runs/21) | Linux | #8 |
| not-a-date | [22](https://github.com/microsoft/aspire/actions/runs/22) | Linux | #8 |
"""
        )

        self.assertFalse(unrecognized.occurrence_ledger.schema_recognized)
        self.assertFalse(unrecognized.occurrence_ledger.complete)
        self.assertEqual(1, unrecognized.occurrence_ledger.source_record_count)
        self.assertTrue(partial.occurrence_ledger.schema_recognized)
        self.assertFalse(partial.occurrence_ledger.complete)
        self.assertEqual(2, partial.occurrence_ledger.source_record_count)
        self.assertEqual(1, partial.occurrence_ledger.parsed_row_count)

    def test_occurrence_rows_are_masked_without_masking_following_prose(self) -> None:
        signals = extract(
            """\
## Occurrences
| Date | Build | Job | PR |
|------|-------|-----|----|
| not-a-date | [22](https://github.com/microsoft/aspire/actions/runs/22) | Linux | #111 |

Related to #4242.
"""
        )

        self.assertEqual((), signals.occurrences)
        self.assertEqual(
            [4242],
            [
                reference["targetNumber"]
                for reference in signals.references
                if reference["targetType"] == "issue"
            ],
        )

    def test_occurrences_are_sorted_deterministically_and_deduplicated(self) -> None:
        signals = extract(
            """\
## Occurrences
| Date | Build | Job | PR |
|------|-------|-----|----|
| 2026-08-20 | [30](https://github.com/microsoft/aspire/actions/runs/30) | Windows | #9 |
| 2026-08-18 | [10](https://github.com/microsoft/aspire/actions/runs/10) | Linux | #7 |
| 2026-08-19 | [20](https://github.com/microsoft/aspire/actions/runs/20) | macOS | #8 |
| 2026-08-18 | [10](https://github.com/microsoft/aspire/actions/runs/10) | Linux | #7 |
"""
        )

        self.assertEqual(
            (
                Occurrence(
                    date="2026-08-18",
                    source_run=10,
                    run_url="https://github.com/microsoft/aspire/actions/runs/10",
                    job="Linux",
                    pull_request=7,
                ),
                Occurrence(
                    date="2026-08-19",
                    source_run=20,
                    run_url="https://github.com/microsoft/aspire/actions/runs/20",
                    job="macOS",
                    pull_request=8,
                ),
                Occurrence(
                    date="2026-08-20",
                    source_run=30,
                    run_url="https://github.com/microsoft/aspire/actions/runs/30",
                    job="Windows",
                    pull_request=9,
                ),
            ),
            signals.occurrences,
        )

    def test_occurrence_sort_uses_total_tie_break_key(self) -> None:
        signals = extract(
            """\
## Occurrences
| Date | Build | Job | PR |
|------|-------|-----|----|
| 2026-08-18 | [20](https://github.com/microsoft/aspire/actions/runs/20) | Windows | #8 |
| 2026-08-18 | [10](https://github.com/microsoft/aspire/actions/runs/10) | Windows | #9 |
| 2026-08-18 | [10](https://github.com/microsoft/aspire/actions/runs/10) | Linux | #7 |
| 2026-08-18 | [10](https://github.com/microsoft/aspire/actions/runs/10) | Linux | #0 |
"""
        )

        self.assertEqual(
            (
                Occurrence(
                    date="2026-08-18",
                    source_run=10,
                    run_url="https://github.com/microsoft/aspire/actions/runs/10",
                    job="Linux",
                    pull_request=None,
                ),
                Occurrence(
                    date="2026-08-18",
                    source_run=10,
                    run_url="https://github.com/microsoft/aspire/actions/runs/10",
                    job="Linux",
                    pull_request=7,
                ),
                Occurrence(
                    date="2026-08-18",
                    source_run=10,
                    run_url="https://github.com/microsoft/aspire/actions/runs/10",
                    job="Windows",
                    pull_request=9,
                ),
                Occurrence(
                    date="2026-08-18",
                    source_run=20,
                    run_url="https://github.com/microsoft/aspire/actions/runs/20",
                    job="Windows",
                    pull_request=8,
                ),
            ),
            signals.occurrences,
        )

    def test_occurrence_pr_zero_is_retained_without_pull_request_reference(self) -> None:
        signals = extract(
            """\
## Occurrences
| Date | Build | Job | PR |
|------|-------|-----|----|
| 2026-08-18 | [10](https://github.com/microsoft/aspire/actions/runs/10) | Linux | #0 |
"""
        )

        self.assertEqual(
            (
                Occurrence(
                    date="2026-08-18",
                    source_run=10,
                    run_url="https://github.com/microsoft/aspire/actions/runs/10",
                    job="Linux",
                    pull_request=None,
                ),
            ),
            signals.occurrences,
        )
        self.assertEqual(
            {
                "date": "2026-08-18",
                "sourceRun": 10,
                "runUrl": "https://github.com/microsoft/aspire/actions/runs/10",
                "job": "Linux",
                "pullRequest": None,
            },
            signals.occurrences[0].as_record(),
        )
        self.assertFalse(any(reference["targetType"] == "pull-request" for reference in signals.references))

    def test_signal_values_are_immutable(self) -> None:
        signals = extract("Related to #7")

        with self.assertRaises(FrozenInstanceError):
            signals.occurrences = ()  # type: ignore[misc]
        with self.assertRaises(TypeError):
            signals.references[0]["targetNumber"] = 8  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
