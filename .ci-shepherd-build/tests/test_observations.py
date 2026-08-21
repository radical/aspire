from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import Mapping
from urllib.parse import quote

from ci_shepherd import lifecycle, observations, timeutils
from ci_shepherd.observations import build_observations, is_scoped_to_issue
from ci_shepherd.policy import ManualPolicy


COLLECTED_AT = "2026-08-19T16:00:00Z"
REPOSITORY = "microsoft/aspire"
FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"


class MappingSubclass(Mapping):
    """A Mapping that is not a ``dict``, as produced by wrapper evidence readers."""

    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def __getitem__(self, key: str) -> object:
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def policy(*retry_safe_pattern_ids: str) -> ManualPolicy:
    return ManualPolicy(
        policy_version="manual-v1",
        quarantine_review_min_distinct_runs=2,
        quarantine_review_min_distinct_commits=2,
        recovery_min_independent_successes=2,
        dormant_human_review_after_days=7,
        systemic_transient_window_days=14,
        systemic_transient_min_occurrences=3,
        systemic_transient_min_failure_rate=0.05,
        proposal_ttl_hours=24,
        max_proposals_per_issue=3,
        retry_safe_pattern_ids=frozenset(retry_safe_pattern_ids),
    )


def evidence(
    evidence_id: str,
    kind: str,
    payload: dict[str, object],
    *,
    availability: str = "available",
) -> tuple[str, dict[str, object]]:
    return (
        evidence_id,
        {
            "kind": kind,
            "url": f"https://github.com/{REPOSITORY}/actions/runs/{payload.get('runId', 100)}",
            "collectedAt": COLLECTED_AT,
            "availability": availability,
            "payload": payload,
        },
    )


def issue_payload(
    number: int,
    *,
    facts: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "number": number,
        "state": "open",
        "title": f"Issue {number}",
        "url": f"https://github.com/{REPOSITORY}/issues/{number}",
        "createdAt": "2026-08-01T00:00:00Z",
        "updatedAt": "2026-08-10T00:00:00Z",
        "labels": ["ci-failure-cause"],
        "producer": "ci-failure-cause",
        "facts": facts or [],
    }


def fact(field: str, raw: str, normalized: str | None = None, **extra: object) -> dict[str, object]:
    return {
        "field": field,
        "raw": raw,
        "normalized": normalized or raw,
        "method": "test-fixture",
        **extra,
    }


def association(issue_number: int) -> list[dict[str, object]]:
    return [{"sourceIssueNumber": issue_number, "sourceEvidenceId": f"issue:{issue_number}"}]


def run_payload(
    *,
    run_id: int = 100,
    attempt: int | None = 1,
    conclusion: str = "failure",
    head_sha: str = "a" * 40,
    recent_history_total_count: int = 5,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "runId": run_id,
        "targetRepository": REPOSITORY,
        "workflowId": 9,
        "workflow": "CI",
        "event": "push",
        "branch": "main",
        "headSha": head_sha,
        "status": "completed",
        "conclusion": conclusion,
        "createdAt": "2026-08-19T15:00:00Z",
        "updatedAt": "2026-08-19T16:00:00Z",
        "runStartedAt": "2026-08-19T15:00:00Z",
        "recentHistoryCollected": True,
        "recentHistoryTotalCount": recent_history_total_count,
        "recentHistory": [],
    }
    if attempt is not None:
        payload["attempt"] = attempt
    return payload


def job_payload(
    issue_number: int,
    *,
    run_id: int = 100,
    attempt: int | None = 1,
    job_id: int = 900,
    name: str = "Tests / Aspire.Hosting.Tests (ubuntu-latest)",
    conclusion: str = "failure",
    log_evidence_id: str | None = None,
    steps: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "runId": run_id,
        "targetRepository": REPOSITORY,
        "jobId": job_id,
        "checkRunId": job_id + 1000,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "startedAt": "2026-08-19T15:01:00Z",
        "completedAt": "2026-08-19T15:30:00Z",
        "steps": steps or [],
        "annotationEvidenceIds": [],
        "referencedBy": association(issue_number),
    }
    if attempt is not None:
        payload["attempt"] = attempt
    if log_evidence_id is not None:
        payload["logEvidenceId"] = log_evidence_id
    return payload


def log_payload(
    issue_number: int,
    *,
    run_id: int = 100,
    attempt: int | None = 1,
    job_id: int = 900,
    excerpt: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "evidenceId": f"run:{run_id}:attempt:{attempt}:job:{job_id}:log",
        "runId": run_id,
        "jobId": job_id,
        "targetRepository": REPOSITORY,
        "excerpt": excerpt,
        "truncated": False,
        "status": 200,
        "referencedBy": association(issue_number),
    }
    if attempt is not None:
        payload["attempt"] = attempt
    return payload


def snapshot(
    issue: dict[str, object],
    *extra_evidence: tuple[str, dict[str, object]],
    collected_at: str = COLLECTED_AT,
) -> dict[str, object]:
    number = int(issue["number"])
    issue_record = evidence(f"issue:{number}", "issue-event", issue)
    return {
        "schemaVersion": 1,
        "repository": REPOSITORY,
        "collectedAt": collected_at,
        "openIssues": [number],
        "issues": [issue],
        "supportingIssues": [],
        "evidence": dict((issue_record, *extra_evidence)),
        "collectionErrors": [],
        "warnings": [],
        "references": {},
    }


def annotation_payload(
    issue_number: int,
    *,
    run_id: int = 100,
    attempt: int | None = 1,
    job_id: int | None = 900,
    check_run_id: int = 1900,
    annotation_id: int = 5,
) -> dict[str, object]:
    """Mirror the collector's annotation payload shape (see the checked-in fixture)."""
    payload: dict[str, object] = {
        "runId": run_id,
        "targetRepository": REPOSITORY,
        "checkRunId": check_run_id,
        "annotationId": annotation_id,
        "path": "tests/Alpha.Tests/SampleTests.cs",
        "startLine": 42,
        "endLine": 42,
        "level": "failure",
        "message": "Assert.Equal() Failure: Values differ",
        "title": "Alpha.Tests.FromAnnotation",
        "referencedBy": association(issue_number),
    }
    if attempt is not None:
        payload["attempt"] = attempt
    if job_id is not None:
        payload["jobId"] = job_id
    return payload


class ObservationTests(unittest.TestCase):
    def test_two_failed_tests_in_one_job_get_stable_distinct_ids_sorted_by_test_name(self) -> None:
        issue_number = 12
        observations = build_observations(
            snapshot(
                issue_payload(
                    issue_number,
                    facts=[
                        fact("testName", "Beta.Tests.FailingTest"),
                        fact("testName", "Alpha.Tests.FailingTest"),
                    ],
                ),
                evidence("run:100", "workflow-run", run_payload()),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    job_payload(issue_number),
                ),
            ),
            policy=policy(),
        )

        occurrences = observations["occurrences"]

        self.assertEqual(
            ["occurrence:12:100:1:900:1", "occurrence:12:100:1:900:2"],
            [occurrence["occurrenceId"] for occurrence in occurrences],
        )
        self.assertEqual(
            ["Alpha.Tests.FailingTest", "Beta.Tests.FailingTest"],
            [occurrence["testName"] for occurrence in occurrences],
        )
        self.assertEqual(
            [
                "test-flake",
                "test-contention",
                "product-regression-suspect",
                "unknown",
            ],
            occurrences[0]["allowedCauses"],
        )

    def test_twelve_failed_tests_preserve_numeric_ordinal_order(self) -> None:
        issue_number = 12
        test_names = [f"Alpha.Tests.FailingTest{index:02d}" for index in range(1, 13)]
        observations = build_observations(
            snapshot(
                issue_payload(
                    issue_number,
                    facts=[fact("testName", test_name) for test_name in reversed(test_names)],
                ),
                evidence("run:100", "workflow-run", run_payload()),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    job_payload(issue_number),
                ),
            ),
            policy=policy(),
        )

        self.assertEqual(
            [f"occurrence:12:100:1:900:{index}" for index in range(1, 13)],
            [occurrence["occurrenceId"] for occurrence in observations["occurrences"]],
        )
        self.assertEqual(test_names, [occurrence["testName"] for occurrence in observations["occurrences"]])

    def test_later_attempt_success_records_attempt_two_without_independent_recovery_eligibility(self) -> None:
        issue_number = 12
        observations = build_observations(
            snapshot(
                issue_payload(issue_number),
                evidence(
                    "run:100",
                    "workflow-run",
                    run_payload(attempt=2, conclusion="success", head_sha="b" * 40),
                ),
                evidence(
                    "run:100:attempt:2:job:900",
                    "workflow-job",
                    job_payload(issue_number, attempt=2, conclusion="success"),
                ),
            ),
            policy=policy(),
        )

        self.assertEqual(
            [
                {
                    "coverageId": "coverage:run:100:attempt:2:job:900",
                    "subjectKind": "lane",
                    "subjectId": "ci:aspire-hosting-tests:ubuntu-latest",
                    "runId": 100,
                    "attempt": 2,
                    "headSha": "b" * 40,
                    "observedAt": "2026-08-19T15:30:00Z",
                    "status": "succeeded",
                    "independentRecoveryEligible": False,
                    "evidenceIds": ["run:100", "run:100:attempt:2:job:900"],
                }
            ],
            observations["coverage"],
        )

    def test_coverage_ids_and_subject_ids_match_task_contract(self) -> None:
        issue_number = 12
        log_id = "run:200:attempt:1:job:901:log"
        observations = build_observations(
            snapshot(
                issue_payload(issue_number),
                evidence(
                    "run:200",
                    "workflow-run",
                    {**run_payload(run_id=200, conclusion="success"), "workflow": "CI Tests"},
                ),
                evidence(
                    "run:200:attempt:1:job:901",
                    "workflow-job",
                    job_payload(
                        issue_number,
                        run_id=200,
                        job_id=901,
                        name="CI Tests / tests-linux (ubuntu-latest)",
                        conclusion="success",
                        log_evidence_id=log_id,
                    ),
                ),
                evidence(
                    log_id,
                    "workflow-log",
                    log_payload(
                        issue_number,
                        run_id=200,
                        job_id=901,
                        excerpt="Passed Alpha.Tests.Punctuation_Case [42 ms]",
                    ),
                ),
            ),
            policy=policy(),
        )

        self.assertEqual(
            [
                (
                    "coverage:run:200:attempt:1:job:901",
                    "lane",
                    "ci-tests:tests-linux:ubuntu-latest",
                ),
                (
                    f"coverage:run:200:attempt:1:job:901:test:{quote('Alpha.Tests.Punctuation_Case', safe='')}",
                    "test",
                    "ci-tests:tests-linux:ubuntu-latest:test:alpha-tests-punctuation-case",
                ),
            ],
            [
                (coverage["coverageId"], coverage["subjectKind"], coverage["subjectId"])
                for coverage in observations["coverage"]
            ],
        )

    def test_exact_test_coverage_ids_encode_full_test_name_without_collisions(self) -> None:
        issue_number = 12
        log_id = "run:200:attempt:1:job:901:log"
        first_test_name = "Alpha.Tests.Punctuation_Case"
        second_test_name = "alpha.tests.punctuation-case"
        observations = build_observations(
            snapshot(
                issue_payload(issue_number),
                evidence(
                    "run:200",
                    "workflow-run",
                    run_payload(run_id=200, conclusion="success"),
                ),
                evidence(
                    "run:200:attempt:1:job:901",
                    "workflow-job",
                    job_payload(
                        issue_number,
                        run_id=200,
                        job_id=901,
                        name="CI Tests / tests-linux (ubuntu-latest)",
                        conclusion="success",
                        log_evidence_id=log_id,
                    ),
                ),
                evidence(
                    log_id,
                    "workflow-log",
                    log_payload(
                        issue_number,
                        run_id=200,
                        job_id=901,
                        excerpt=(
                            f"Passed {first_test_name} [42 ms]\n"
                            f"Passed {second_test_name} [43 ms]"
                        ),
                    ),
                ),
            ),
            policy=policy(),
        )

        test_coverage = [
            coverage
            for coverage in observations["coverage"]
            if coverage["subjectKind"] == "test"
        ]
        self.assertEqual(2, len(test_coverage))
        self.assertEqual(
            [
                f"coverage:run:200:attempt:1:job:901:test:{quote(first_test_name, safe='')}",
                f"coverage:run:200:attempt:1:job:901:test:{quote(second_test_name, safe='')}",
            ],
            [coverage["coverageId"] for coverage in test_coverage],
        )
        self.assertEqual(2, len({coverage["coverageId"] for coverage in test_coverage}))
        # Both raw names denote the same test identity, so they share one subject even
        # though their coverage IDs stay distinct.
        self.assertEqual(
            [
                "ci:tests-linux:ubuntu-latest:test:alpha-tests-punctuation-case",
                "ci:tests-linux:ubuntu-latest:test:alpha-tests-punctuation-case",
            ],
            [coverage["subjectId"] for coverage in test_coverage],
        )

    def test_exact_test_coverage_subject_matches_the_test_fingerprint_identity(self) -> None:
        issue_number = 12
        failing_log_id = "run:100:attempt:1:job:900:log"
        passing_log_id = "run:200:attempt:1:job:901:log"
        test_name = "Alpha.Tests.Flaky_Case"
        observations = build_observations(
            snapshot(
                issue_payload(issue_number),
                evidence("run:100", "workflow-run", run_payload()),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    job_payload(
                        issue_number,
                        name="CI / tests-linux (ubuntu-latest)",
                        log_evidence_id=failing_log_id,
                    ),
                ),
                evidence(
                    failing_log_id,
                    "workflow-log",
                    {
                        **log_payload(issue_number, excerpt="failed"),
                        "facts": [fact("testName", test_name, "alpha-tests-flaky-case")],
                    },
                ),
                evidence("run:200", "workflow-run", run_payload(run_id=200, conclusion="success")),
                evidence(
                    "run:200:attempt:1:job:901",
                    "workflow-job",
                    job_payload(
                        issue_number,
                        run_id=200,
                        job_id=901,
                        name="CI / tests-linux (ubuntu-latest)",
                        conclusion="success",
                        log_evidence_id=passing_log_id,
                    ),
                ),
                evidence(
                    passing_log_id,
                    "workflow-log",
                    log_payload(
                        issue_number,
                        run_id=200,
                        job_id=901,
                        excerpt=f"Passed {test_name} [42 ms]",
                    ),
                ),
            ),
            policy=policy(),
        )

        test_coverage = next(
            coverage for coverage in observations["coverage"] if coverage["subjectKind"] == "test"
        )
        self.assertEqual(
            f"coverage:run:200:attempt:1:job:901:test:{quote(test_name, safe='')}",
            test_coverage["coverageId"],
        )
        self.assertEqual(
            "ci:tests-linux:ubuntu-latest:test:alpha-tests-flaky-case",
            test_coverage["subjectId"],
        )
        self.assertEqual(
            "test:alpha-tests-flaky-case",
            observations["occurrences"][0]["fingerprintId"],
        )
        self.assertTrue(
            test_coverage["subjectId"].endswith(
                observations["occurrences"][0]["fingerprintId"]
            )
        )

    def test_unavailable_workflow_jobs_do_not_create_occurrences_or_coverage(self) -> None:
        for availability in ("partial", "not-enriched", "expired-or-unavailable"):
            with self.subTest(availability=availability, conclusion="failure"):
                issue_number = 12
                observations = build_observations(
                    snapshot(
                        issue_payload(issue_number, facts=[fact("testName", "Alpha.Tests.FailingTest")]),
                        evidence("run:100", "workflow-run", run_payload()),
                        evidence(
                            "run:100:attempt:1:job:900",
                            "workflow-job",
                            job_payload(issue_number),
                            availability=availability,
                        ),
                    ),
                    policy=policy(),
                )

                self.assertEqual([], observations["occurrences"])
                self.assertEqual([], observations["coverage"])

            with self.subTest(availability=availability, conclusion="success"):
                issue_number = 12
                observations = build_observations(
                    snapshot(
                        issue_payload(issue_number),
                        evidence("run:100", "workflow-run", run_payload(conclusion="success")),
                        evidence(
                            "run:100:attempt:1:job:900",
                            "workflow-job",
                            job_payload(issue_number, conclusion="success"),
                            availability=availability,
                        ),
                    ),
                    policy=policy(),
                )

                self.assertEqual([], observations["occurrences"])
                self.assertEqual([], observations["coverage"])

    def test_unavailable_logs_do_not_contribute_to_occurrence_identity(self) -> None:
        for availability in ("partial", "not-enriched", "expired-or-unavailable"):
            with self.subTest(availability=availability):
                issue_number = 12
                log_id = "run:100:attempt:1:job:900:log"
                observations = build_observations(
                    snapshot(
                        issue_payload(issue_number),
                        evidence("run:100", "workflow-run", run_payload()),
                        evidence(
                            "run:100:attempt:1:job:900",
                            "workflow-job",
                            job_payload(issue_number, log_evidence_id=log_id),
                        ),
                        evidence(
                            log_id,
                            "workflow-log",
                            {
                                **log_payload(
                                    issue_number,
                                    excerpt="HTTP 502\nFailed Alpha.Tests.FailingTest",
                                ),
                                "facts": [fact("testName", "Alpha.Tests.FailingTest")],
                            },
                            availability=availability,
                        ),
                    ),
                    policy=policy(),
                )

                self.assertEqual(1, len(observations["occurrences"]))
                occurrence = observations["occurrences"][0]
                self.assertIsNone(occurrence["testName"])
                self.assertEqual("unknown:12:100:900", occurrence["fingerprintId"])
                self.assertEqual(
                    ["run:100", "run:100:attempt:1:job:900"],
                    occurrence["evidenceIds"],
                )

    def test_unavailable_successful_test_execution_logs_do_not_create_test_coverage(self) -> None:
        for availability in ("partial", "not-enriched", "expired-or-unavailable"):
            with self.subTest(availability=availability):
                issue_number = 12
                log_id = "run:100:attempt:1:job:900:log"
                observations = build_observations(
                    snapshot(
                        issue_payload(issue_number),
                        evidence("run:100", "workflow-run", run_payload(conclusion="success")),
                        evidence(
                            "run:100:attempt:1:job:900",
                            "workflow-job",
                            job_payload(issue_number, conclusion="success", log_evidence_id=log_id),
                        ),
                        evidence(
                            log_id,
                            "workflow-log",
                            log_payload(issue_number, excerpt="Passed Alpha.Tests.FailingTest [42 ms]"),
                            availability=availability,
                        ),
                    ),
                    policy=policy(),
                )

                self.assertEqual(["lane"], [coverage["subjectKind"] for coverage in observations["coverage"]])
                self.assertEqual(
                    ["run:100", "run:100:attempt:1:job:900"],
                    observations["coverage"][0]["evidenceIds"],
                )

    def test_fingerprint_id_is_normalized_but_raw_components_are_preserved(self) -> None:
        issue_number = 12
        log_id = "run:100:attempt:1:job:900:log"
        observations = build_observations(
            snapshot(
                issue_payload(issue_number),
                evidence("run:100", "workflow-run", run_payload()),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    job_payload(
                        issue_number,
                        name="Build / Foo.Bar (Ubuntu-Latest)",
                        log_evidence_id=log_id,
                        steps=[
                            {
                                "number": 3,
                                "name": "Download Artifact!",
                                "status": "completed",
                                "conclusion": "failure",
                            }
                        ],
                    ),
                ),
                evidence(
                    log_id,
                    "workflow-log",
                    log_payload(issue_number, excerpt="Download Artifact! failed with HTTP 502"),
                ),
            ),
            policy=policy(),
        )

        occurrence = observations["occurrences"][0]
        self.assertEqual("infra:http-502:ubuntu-latest:download-artifact", occurrence["fingerprintId"])
        self.assertIn("fingerprintComponents", occurrence)
        self.assertEqual(
            {
                "patternId": "http-502",
                "runnerOS": "Ubuntu-Latest",
                "step": "Download Artifact!",
                "errorCode": None,
                "job": "Build / Foo.Bar (Ubuntu-Latest)",
                "testName": None,
            },
            occurrence["fingerprintComponents"],
        )

    def test_test_fingerprint_raw_components_preserve_punctuation_and_case(self) -> None:
        issue_number = 12
        observations = build_observations(
            snapshot(
                issue_payload(issue_number, facts=[fact("testName", "Alpha.Tests.Some-Test!")]),
                evidence("run:100", "workflow-run", run_payload()),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    job_payload(issue_number, name="Tests / Lane (Ubuntu-Latest)"),
                ),
            ),
            policy=policy(),
        )

        occurrence = observations["occurrences"][0]
        self.assertEqual("test:alpha-tests-some-test", occurrence["fingerprintId"])
        self.assertIn("fingerprintComponents", occurrence)
        self.assertEqual(
            {
                "patternId": None,
                "runnerOS": "Ubuntu-Latest",
                "step": None,
                "errorCode": None,
                "job": "Tests / Lane (Ubuntu-Latest)",
                "testName": "Alpha.Tests.Some-Test!",
            },
            occurrence["fingerprintComponents"],
        )

    def test_identity_fields_must_be_positive_non_bool_integers(self) -> None:
        cases: list[tuple[str, object, str]] = [
            ("openIssues", 0, "Snapshot openIssues[0]"),
            ("openIssues", True, "Snapshot openIssues[0]"),
            ("runId", 0, "run:100 payload.runId"),
            ("runId", True, "run:100 payload.runId"),
            ("attempt", 0, "run:100:attempt:1:job:900 payload.attempt"),
            ("attempt", True, "run:100:attempt:1:job:900 payload.attempt"),
            ("jobId", 0, "run:100:attempt:1:job:900 payload.jobId"),
            ("jobId", True, "run:100:attempt:1:job:900 payload.jobId"),
        ]

        for field, value, expected_field in cases:
            with self.subTest(field=field, value=value):
                issue_number = 12
                run = run_payload()
                job = job_payload(issue_number)
                open_issue = issue_number
                if field == "openIssues":
                    open_issue = value  # type: ignore[assignment]
                elif field == "runId":
                    run["runId"] = value
                else:
                    job[field] = value

                snapshot_payload = snapshot(
                    issue_payload(issue_number),
                    evidence("run:100", "workflow-run", run),
                    evidence("run:100:attempt:1:job:900", "workflow-job", job),
                )
                snapshot_payload["openIssues"] = [open_issue]

                with self.assertRaisesRegex(ValueError, re.escape(expected_field)):
                    build_observations(snapshot_payload, policy=policy())

    def test_workflow_evidence_ids_must_match_payload_identity_fields(self) -> None:
        issue_number = 12
        cases: list[tuple[str, str, dict[str, object], str]] = [
            (
                "run:100",
                "workflow-run",
                run_payload(run_id=101),
                "runId mismatch",
            ),
            (
                "run:100:attempt:1:job:900",
                "workflow-job",
                job_payload(issue_number, run_id=101),
                "runId mismatch",
            ),
            (
                "run:100:attempt:1:job:900",
                "workflow-job",
                job_payload(issue_number, attempt=2),
                "attempt mismatch",
            ),
            (
                "run:100:attempt:1:job:900",
                "workflow-job",
                job_payload(issue_number, job_id=901),
                "jobId mismatch",
            ),
            (
                "run:100:attempt:1:job:900:log",
                "workflow-log",
                log_payload(issue_number, job_id=901, excerpt="HTTP 502"),
                "jobId mismatch",
            ),
            (
                "run:100:attempt:1:job:900:log",
                "workflow-log",
                {
                    **log_payload(issue_number, excerpt="HTTP 502"),
                    "evidenceId": "run:100:attempt:1:job:901:log",
                },
                "evidenceId mismatch",
            ),
        ]

        for evidence_id, kind, payload, expected_mismatch in cases:
            with self.subTest(evidence_id=evidence_id, expected_mismatch=expected_mismatch):
                with self.assertRaisesRegex(ValueError, expected_mismatch):
                    build_observations(
                        snapshot(
                            issue_payload(issue_number),
                            evidence("run:100", "workflow-run", run_payload()),
                            evidence(evidence_id, kind, payload),
                        ),
                        policy=policy(),
                    )

    def test_issue_evidence_ids_must_match_payload_and_open_issue_identity(self) -> None:
        snapshot_payload = snapshot(
            issue_payload(12),
            evidence("run:100", "workflow-run", run_payload()),
            evidence(
                "run:100:attempt:1:job:900",
                "workflow-job",
                job_payload(12),
            ),
        )
        snapshot_payload["evidence"]["issue:12"]["payload"]["number"] = 13

        with self.assertRaisesRegex(ValueError, "issueNumber mismatch"):
            build_observations(snapshot_payload, policy=policy())

    def test_missing_attempt_failure_occurrence_round_trips_through_history_validation(self) -> None:
        issue_number = 12
        current = build_observations(
            snapshot(
                issue_payload(issue_number, facts=[fact("testName", "Alpha.Tests.FailingTest")]),
                evidence("run:100", "workflow-run", run_payload(attempt=None)),
                evidence(
                    "run:100:attempt:none:job:900",
                    "workflow-job",
                    job_payload(issue_number, attempt=None),
                ),
            ),
            policy=policy(),
        )

        occurrence = current["occurrences"][0]
        self.assertEqual("occurrence:12:100:none:900:1", occurrence["occurrenceId"])
        self.assertIsNone(occurrence["attempt"])

        historical = build_observations(
            snapshot(issue_payload(issue_number)),
            policy=policy(),
            history={"occurrences": [occurrence]},
        )

        self.assertEqual(
            ["occurrence:12:100:none:900:1"],
            historical["fingerprints"][0]["occurrenceIds"],
        )

    def test_history_occurrence_identity_fields_are_validated_before_summary(self) -> None:
        base_occurrence: dict[str, object] = {
            "occurrenceId": "occurrence:12:100:1:900:1",
            "issueNumber": 12,
            "runId": 100,
            "attempt": 1,
            "jobId": 900,
            "workflow": "CI",
            "lane": "Aspire.Hosting.Tests",
            "os": "ubuntu-latest",
            "observedAt": "2026-08-18T15:30:00Z",
            "fingerprintId": "test:alpha-tests-failingtest",
        }
        cases: list[tuple[str, object]] = [
            ("occurrenceId", ""),
            ("fingerprintId", ""),
            ("issueNumber", 0),
            ("issueNumber", True),
            ("runId", 0),
            ("runId", True),
            ("attempt", 0),
            ("attempt", True),
            ("observedAt", ""),
            ("observedAt", 123),
        ]

        for field, value in cases:
            with self.subTest(field=field, value=value):
                occurrence = {**base_occurrence, field: value}
                with self.assertRaisesRegex(ValueError, rf"history\.occurrences\[0\]\.{field}"):
                    build_observations(
                        snapshot(issue_payload(12)),
                        policy=policy(),
                        history={"occurrences": [occurrence]},
                    )

    def test_history_occurrence_id_must_match_record_identity_fields(self) -> None:
        base_occurrence: dict[str, object] = {
            "occurrenceId": "occurrence:12:100:1:900:1",
            "issueNumber": 12,
            "runId": 100,
            "attempt": 1,
            "jobId": 900,
            "workflow": "CI",
            "lane": "Aspire.Hosting.Tests",
            "os": "ubuntu-latest",
            "observedAt": "2026-08-18T15:30:00Z",
            "fingerprintId": "test:alpha-tests-failingtest",
        }
        cases: list[tuple[str, object, str]] = [
            ("occurrenceId", "occurrence:13:100:1:900:1", "issueNumber mismatch"),
            ("occurrenceId", "occurrence:12:101:1:900:1", "runId mismatch"),
            ("occurrenceId", "occurrence:12:100:2:900:1", "attempt mismatch"),
            ("occurrenceId", "occurrence:12:100:none:900:1", "attempt mismatch"),
            ("occurrenceId", "occurrence:12:100:1:901:1", "jobId mismatch"),
            ("occurrenceId", "occurrence:12:100:1:900:0", "ordinal"),
            ("attempt", None, "attempt mismatch"),
            ("jobId", None, "jobId mismatch"),
        ]

        for field, value, expected in cases:
            with self.subTest(field=field, value=value):
                occurrence = {**base_occurrence, field: value}
                with self.assertRaisesRegex(ValueError, expected):
                    build_observations(
                        snapshot(issue_payload(12)),
                        policy=policy(),
                        history={"occurrences": [occurrence]},
                    )

    def test_history_occurrence_observed_at_must_be_timezone_aware_iso8601(self) -> None:
        base_occurrence: dict[str, object] = {
            "occurrenceId": "occurrence:12:100:1:900:1",
            "issueNumber": 12,
            "runId": 100,
            "attempt": 1,
            "jobId": 900,
            "workflow": "CI",
            "lane": "Aspire.Hosting.Tests",
            "os": "ubuntu-latest",
            "observedAt": "2026-08-18T15:30:00Z",
            "fingerprintId": "test:alpha-tests-failingtest",
        }
        cases = ["2026-08-18T15:30:00", "not-a-timestamp"]

        for observed_at in cases:
            with self.subTest(observed_at=observed_at):
                occurrence = {**base_occurrence, "observedAt": observed_at}
                with self.assertRaisesRegex(ValueError, r"history\.occurrences\[0\]\.observedAt"):
                    build_observations(
                        snapshot(issue_payload(12)),
                        policy=policy(),
                        history={"occurrences": [occurrence]},
                    )

    def test_collected_at_must_be_timezone_aware_iso8601(self) -> None:
        for collected_at in ("2026-08-19T16:00:00", "not-a-timestamp"):
            with self.subTest(collected_at=collected_at):
                snapshot_payload = snapshot(issue_payload(12))
                snapshot_payload["collectedAt"] = collected_at

                with self.assertRaisesRegex(ValueError, "Snapshot collectedAt"):
                    build_observations(snapshot_payload, policy=policy())

    def test_observed_matching_lane_runs_counts_only_matching_observed_runs(self) -> None:
        issue_number = 12
        observations = build_observations(
            snapshot(
                issue_payload(issue_number, facts=[fact("testName", "Alpha.Tests.FailingTest")]),
                evidence(
                    "run:200",
                    "workflow-run",
                    run_payload(run_id=200, recent_history_total_count=99),
                ),
                evidence(
                    "run:200:attempt:1:job:901",
                    "workflow-job",
                    job_payload(
                        issue_number,
                        run_id=200,
                        job_id=901,
                        name="CI Tests / tests-linux (ubuntu-latest)",
                    ),
                ),
                evidence(
                    "run:201",
                    "workflow-run",
                    run_payload(run_id=201, conclusion="success", recent_history_total_count=99),
                ),
                evidence(
                    "run:201:attempt:1:job:902",
                    "workflow-job",
                    job_payload(
                        issue_number,
                        run_id=201,
                        job_id=902,
                        name="CI Tests / tests-linux (ubuntu-latest)",
                        conclusion="success",
                    ),
                ),
                evidence(
                    "run:202",
                    "workflow-run",
                    run_payload(run_id=202, conclusion="success", recent_history_total_count=99),
                ),
                evidence(
                    "run:202:attempt:1:job:903",
                    "workflow-job",
                    job_payload(
                        issue_number,
                        run_id=202,
                        job_id=903,
                        name="CI Tests / tests-windows (ubuntu-latest)",
                        conclusion="success",
                    ),
                ),
                evidence(
                    "run:203",
                    "workflow-run",
                    run_payload(run_id=203, conclusion="success", recent_history_total_count=99),
                ),
                evidence(
                    "run:203:attempt:1:job:904",
                    "workflow-job",
                    job_payload(
                        issue_number,
                        run_id=203,
                        job_id=904,
                        name="CI Tests / tests-linux (windows-latest)",
                        conclusion="success",
                    ),
                ),
                evidence(
                    "run:204",
                    "workflow-run",
                    {
                        **run_payload(run_id=204, conclusion="success", recent_history_total_count=99),
                        "workflow": "Other",
                    },
                ),
                evidence(
                    "run:204:attempt:1:job:905",
                    "workflow-job",
                    job_payload(
                        issue_number,
                        run_id=204,
                        job_id=905,
                        name="CI Tests / tests-linux (ubuntu-latest)",
                        conclusion="success",
                    ),
                ),
            ),
            policy=policy(),
        )

        self.assertEqual(2, observations["fingerprints"][0]["observedMatchingLaneRunDenominator"])
        self.assertGreaterEqual(
            observations["fingerprints"][0]["observedMatchingLaneRunDenominator"],
            len(observations["fingerprints"][0]["distinctRunIds"]),
        )

    def test_systemic_window_includes_cutoff_and_excludes_stale_matching_lane_runs(self) -> None:
        issue_number = 12
        cutoff = "2026-08-05T16:00:00Z"
        before_cutoff = "2026-08-05T15:59:59Z"
        observations = build_observations(
            snapshot(
                issue_payload(issue_number, facts=[fact("testName", "Alpha.Tests.FailingTest")]),
                evidence(
                    "run:200",
                    "workflow-run",
                    {**run_payload(run_id=200), "runStartedAt": cutoff, "createdAt": cutoff, "updatedAt": cutoff},
                ),
                evidence(
                    "run:200:attempt:1:job:901",
                    "workflow-job",
                    {
                        **job_payload(
                            issue_number,
                            run_id=200,
                            job_id=901,
                            name="CI Tests / tests-linux (ubuntu-latest)",
                        ),
                        "completedAt": cutoff,
                    },
                ),
                evidence(
                    "run:201",
                    "workflow-run",
                    {
                        **run_payload(run_id=201, conclusion="success"),
                        "runStartedAt": cutoff,
                        "createdAt": cutoff,
                        "updatedAt": cutoff,
                    },
                ),
                evidence(
                    "run:201:attempt:1:job:902",
                    "workflow-job",
                    {
                        **job_payload(
                            issue_number,
                            run_id=201,
                            job_id=902,
                            name="CI Tests / tests-linux (ubuntu-latest)",
                            conclusion="success",
                        ),
                        "completedAt": cutoff,
                    },
                ),
                evidence(
                    "run:202",
                    "workflow-run",
                    {
                        **run_payload(run_id=202, conclusion="success"),
                        "runStartedAt": before_cutoff,
                        "createdAt": before_cutoff,
                        "updatedAt": before_cutoff,
                    },
                ),
                evidence(
                    "run:202:attempt:1:job:903",
                    "workflow-job",
                    {
                        **job_payload(
                            issue_number,
                            run_id=202,
                            job_id=903,
                            name="CI Tests / tests-linux (ubuntu-latest)",
                            conclusion="success",
                        ),
                        "completedAt": before_cutoff,
                    },
                ),
            ),
            policy=policy(),
        )

        summary = observations["fingerprints"][0]
        self.assertEqual(["occurrence:12:200:1:901:1"], summary["occurrenceIds"])
        self.assertEqual([200], summary["distinctRunIds"])
        self.assertEqual(2, summary["observedMatchingLaneRunDenominator"])

    def test_systemic_window_excludes_stale_history_occurrences_from_numerator(self) -> None:
        issue_number = 12
        observations = build_observations(
            snapshot(
                issue_payload(issue_number, facts=[fact("testName", "Alpha.Tests.FailingTest")]),
                evidence("run:100", "workflow-run", run_payload()),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    job_payload(issue_number),
                ),
            ),
            policy=policy(),
            history={
                "occurrences": [
                    {
                        "occurrenceId": "occurrence:11:99:1:901:1",
                        "issueNumber": 11,
                        "runId": 99,
                        "attempt": 1,
                        "jobId": 901,
                        "workflow": "CI",
                        "lane": "Aspire.Hosting.Tests",
                        "os": "ubuntu-latest",
                        "observedAt": "2026-08-05T15:59:59Z",
                        "fingerprintId": "test:alpha-tests-failingtest",
                    }
                ]
            },
        )

        summary = observations["fingerprints"][0]
        self.assertEqual(["occurrence:12:100:1:900:1"], summary["occurrenceIds"])
        self.assertEqual([12], summary["issueNumbers"])
        self.assertEqual([100], summary["distinctRunIds"])

    def test_systemic_window_includes_cutoff_and_excludes_future_history_occurrences(self) -> None:
        issue_number = 12
        observations = build_observations(
            snapshot(
                issue_payload(issue_number, facts=[fact("testName", "Alpha.Tests.FailingTest")]),
                evidence("run:100", "workflow-run", run_payload()),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    job_payload(issue_number),
                ),
            ),
            policy=policy(),
            history={
                "occurrences": [
                    {
                        "occurrenceId": "occurrence:11:99:1:901:1",
                        "issueNumber": 11,
                        "runId": 99,
                        "attempt": 1,
                        "jobId": 901,
                        "workflow": "CI",
                        "lane": "Aspire.Hosting.Tests",
                        "os": "ubuntu-latest",
                        "observedAt": "2026-08-05T16:00:00Z",
                        "fingerprintId": "test:alpha-tests-failingtest",
                    },
                    {
                        "occurrenceId": "occurrence:13:101:1:902:1",
                        "issueNumber": 13,
                        "runId": 101,
                        "attempt": 1,
                        "jobId": 902,
                        "workflow": "CI",
                        "lane": "Aspire.Hosting.Tests",
                        "os": "ubuntu-latest",
                        "observedAt": "2026-08-19T16:00:01Z",
                        "fingerprintId": "test:alpha-tests-failingtest",
                    },
                ]
            },
        )

        summary = observations["fingerprints"][0]
        self.assertEqual(
            ["occurrence:11:99:1:901:1", "occurrence:12:100:1:900:1"],
            summary["occurrenceIds"],
        )
        self.assertEqual([11, 12], summary["issueNumbers"])
        self.assertEqual([99, 100], summary["distinctRunIds"])

    def test_success_coverage_is_bounded_by_cutoff_and_snapshot_time(self) -> None:
        issue_number = 12
        observations = build_observations(
            snapshot(
                issue_payload(issue_number),
                evidence(
                    "run:100",
                    "workflow-run",
                    run_payload(run_id=100, conclusion="success"),
                ),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    {
                        **job_payload(issue_number, run_id=100, job_id=900, conclusion="success"),
                        "completedAt": "2026-08-05T16:00:00Z",
                    },
                ),
                evidence(
                    "run:101",
                    "workflow-run",
                    run_payload(run_id=101, conclusion="success"),
                ),
                evidence(
                    "run:101:attempt:1:job:901",
                    "workflow-job",
                    {
                        **job_payload(issue_number, run_id=101, job_id=901, conclusion="success"),
                        "completedAt": "2026-08-19T16:00:01Z",
                    },
                ),
            ),
            policy=policy(),
        )

        self.assertEqual(
            ["coverage:run:100:attempt:1:job:900"],
            [coverage["coverageId"] for coverage in observations["coverage"]],
        )

    def test_success_coverage_observed_at_must_be_timezone_aware_iso8601(self) -> None:
        for observed_at in ("2026-08-19T15:30:00", "not-a-timestamp"):
            with self.subTest(observed_at=observed_at):
                issue_number = 12
                with self.assertRaisesRegex(ValueError, r"run:100:attempt:1:job:900 observedAt"):
                    build_observations(
                        snapshot(
                            issue_payload(issue_number),
                            evidence("run:100", "workflow-run", run_payload(conclusion="success")),
                            evidence(
                                "run:100:attempt:1:job:900",
                                "workflow-job",
                                {
                                    **job_payload(issue_number, conclusion="success"),
                                    "completedAt": observed_at,
                                },
                            ),
                        ),
                        policy=policy(),
                    )

    def test_fingerprint_seen_times_use_utc_order_for_equivalent_instants(self) -> None:
        issue_number = 12
        observations = build_observations(
            snapshot(
                issue_payload(issue_number, facts=[fact("testName", "Alpha.Tests.FailingTest")]),
                evidence("run:100", "workflow-run", run_payload()),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    {
                        **job_payload(issue_number),
                        "completedAt": "2026-08-19T17:00:00+02:00",
                    },
                ),
            ),
            policy=policy(),
            history={
                "occurrences": [
                    {
                        "occurrenceId": "occurrence:11:99:1:901:1",
                        "issueNumber": 11,
                        "runId": 99,
                        "attempt": 1,
                        "jobId": 901,
                        "workflow": "CI",
                        "lane": "Aspire.Hosting.Tests",
                        "os": "ubuntu-latest",
                        "observedAt": "2026-08-19T15:00:00Z",
                        "fingerprintId": "test:alpha-tests-failingtest",
                    }
                ]
            },
        )

        summary = observations["fingerprints"][0]
        self.assertEqual("2026-08-19T15:00:00Z", summary["firstSeenAt"])
        self.assertEqual("2026-08-19T15:00:00Z", summary["lastSeenAt"])

    def test_fingerprint_seen_times_use_utc_order_for_later_offset_timestamp(self) -> None:
        issue_number = 12
        observations = build_observations(
            snapshot(
                issue_payload(issue_number, facts=[fact("testName", "Alpha.Tests.FailingTest")]),
                evidence("run:100", "workflow-run", run_payload()),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    {
                        **job_payload(issue_number),
                        "completedAt": "2026-08-19T16:30:00+02:00",
                    },
                ),
            ),
            policy=policy(),
            history={
                "occurrences": [
                    {
                        "occurrenceId": "occurrence:11:99:1:901:1",
                        "issueNumber": 11,
                        "runId": 99,
                        "attempt": 1,
                        "jobId": 901,
                        "workflow": "CI",
                        "lane": "Aspire.Hosting.Tests",
                        "os": "ubuntu-latest",
                        "observedAt": "2026-08-19T10:00:00-05:00",
                        "fingerprintId": "test:alpha-tests-failingtest",
                    }
                ]
            },
        )

        summary = observations["fingerprints"][0]
        self.assertEqual("2026-08-19T14:30:00Z", summary["firstSeenAt"])
        self.assertEqual("2026-08-19T15:00:00Z", summary["lastSeenAt"])

    def test_history_only_fingerprint_summary_counts_failure_runs_in_matching_lane_denominator(self) -> None:
        observations = build_observations(
            snapshot(issue_payload(12)),
            policy=policy(),
            history={
                "occurrences": [
                    {
                        "occurrenceId": "occurrence:11:99:1:901:1",
                        "issueNumber": 11,
                        "runId": 99,
                        "attempt": 1,
                        "jobId": 901,
                        "workflow": "CI",
                        "lane": "tests-linux",
                        "os": "ubuntu-latest",
                        "observedAt": "2026-08-18T15:30:00Z",
                        "fingerprintId": "test:alpha-tests-failingtest",
                    }
                ]
            },
        )

        self.assertEqual(1, len(observations["fingerprints"]))
        self.assertIn("observedMatchingLaneRunDenominator", observations["fingerprints"][0])
        self.assertEqual(1, observations["fingerprints"][0]["observedMatchingLaneRunDenominator"])
        self.assertGreaterEqual(
            observations["fingerprints"][0]["observedMatchingLaneRunDenominator"],
            len(observations["fingerprints"][0]["distinctRunIds"]),
        )

    def test_network_pattern_is_not_retry_safe_without_policy_allowlist(self) -> None:
        issue_number = 12
        log_id = "run:100:attempt:1:job:900:log"
        observations = build_observations(
            snapshot(
                issue_payload(issue_number),
                evidence("run:100", "workflow-run", run_payload()),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    job_payload(
                        issue_number,
                        log_evidence_id=log_id,
                        steps=[
                            {
                                "number": 3,
                                "name": "download-artifact",
                                "status": "completed",
                                "conclusion": "failure",
                            }
                        ],
                    ),
                ),
                evidence(
                    log_id,
                    "workflow-log",
                    log_payload(
                        issue_number,
                        excerpt="download-artifact failed with HTTP 502 while fetching logs",
                    ),
                ),
            ),
            policy=policy(),
        )

        self.assertEqual(1, len(observations["occurrences"]))
        occurrence = observations["occurrences"][0]
        self.assertEqual(["infra-transient", "unknown"], occurrence["allowedCauses"])
        self.assertFalse(occurrence["retrySafe"])
        self.assertEqual("infra:http-502:ubuntu-latest:download-artifact", occurrence["fingerprintId"])

    def test_http_transient_classification_only_accepts_retry_relevant_statuses(self) -> None:
        cases = [
            (200, "unknown", ["unknown"]),
            (302, "unknown", ["unknown"]),
            (404, "unknown", ["unknown"]),
            (408, "infra:http-408:ubuntu-latest:download-artifact", ["infra-transient", "unknown"]),
            (429, "infra:http-429:ubuntu-latest:download-artifact", ["infra-transient", "unknown"]),
            (502, "infra:http-502:ubuntu-latest:download-artifact", ["infra-transient", "unknown"]),
        ]

        for status_code, expected_fingerprint_prefix, expected_causes in cases:
            with self.subTest(status_code=status_code):
                issue_number = 12
                log_id = "run:100:attempt:1:job:900:log"
                observations = build_observations(
                    snapshot(
                        issue_payload(issue_number),
                        evidence("run:100", "workflow-run", run_payload()),
                        evidence(
                            "run:100:attempt:1:job:900",
                            "workflow-job",
                            job_payload(
                                issue_number,
                                log_evidence_id=log_id,
                                steps=[
                                    {
                                        "number": 3,
                                        "name": "download-artifact",
                                        "status": "completed",
                                        "conclusion": "failure",
                                    }
                                ],
                            ),
                        ),
                        evidence(
                            log_id,
                            "workflow-log",
                            log_payload(
                                issue_number,
                                excerpt=f"download-artifact failed with HTTP {status_code}",
                            ),
                        ),
                    ),
                    policy=policy(),
                )

                occurrence = observations["occurrences"][0]
                self.assertTrue(occurrence["fingerprintId"].startswith(expected_fingerprint_prefix))
                self.assertEqual(expected_causes, occurrence["allowedCauses"])

    def test_compile_signal_takes_precedence_over_http_transient_token(self) -> None:
        issue_number = 12
        log_id = "run:100:attempt:1:job:900:log"
        observations = build_observations(
            snapshot(
                issue_payload(issue_number),
                evidence("run:100", "workflow-run", run_payload()),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    job_payload(
                        issue_number,
                        log_evidence_id=log_id,
                        steps=[
                            {
                                "number": 3,
                                "name": "build",
                                "status": "completed",
                                "conclusion": "failure",
                            }
                        ],
                    ),
                ),
                evidence(
                    log_id,
                    "workflow-log",
                    log_payload(
                        issue_number,
                        excerpt="HTTP 502\nsrc/Program.cs(10,20): error CS1002: ; expected",
                    ),
                ),
            ),
            policy=policy(),
        )

        occurrence = observations["occurrences"][0]
        self.assertEqual(
            [
                "toolchain-build-break",
                "product-regression-suspect",
                "unknown",
            ],
            occurrence["allowedCauses"],
        )
        self.assertEqual("build:cs1002:tests-aspire-hosting-tests-ubuntu-latest", occurrence["fingerprintId"])
        self.assertNotIn("infra-transient", occurrence["allowedCauses"])

    def test_package_build_signal_takes_precedence_over_http_transient_token(self) -> None:
        issue_number = 12
        log_id = "run:100:attempt:1:job:900:log"
        observations = build_observations(
            snapshot(
                issue_payload(issue_number),
                evidence("run:100", "workflow-run", run_payload()),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    job_payload(issue_number, log_evidence_id=log_id),
                ),
                evidence(
                    log_id,
                    "workflow-log",
                    log_payload(
                        issue_number,
                        excerpt="HTTP 502\nerror NU1101: Unable to find package Aspire.Foo",
                    ),
                ),
            ),
            policy=policy(),
        )

        occurrence = observations["occurrences"][0]
        self.assertEqual(
            [
                "toolchain-build-break",
                "product-regression-suspect",
                "unknown",
            ],
            occurrence["allowedCauses"],
        )
        self.assertEqual("build:nu1101:tests-aspire-hosting-tests-ubuntu-latest", occurrence["fingerprintId"])
        self.assertNotIn("infra-transient", occurrence["allowedCauses"])

    def test_attempt_one_success_is_independent_recovery_eligible(self) -> None:
        issue_number = 12
        observations = build_observations(
            snapshot(
                issue_payload(issue_number),
                evidence("run:100", "workflow-run", run_payload(conclusion="success")),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    job_payload(issue_number, conclusion="success"),
                ),
            ),
            policy=policy(),
        )

        self.assertTrue(observations["coverage"][0]["independentRecoveryEligible"])

    def test_missing_attempt_success_is_not_independent_recovery_eligible(self) -> None:
        issue_number = 12
        observations = build_observations(
            snapshot(
                issue_payload(issue_number),
                evidence("run:100", "workflow-run", run_payload(attempt=None, conclusion="success")),
                evidence(
                    "run:100:attempt:none:job:900",
                    "workflow-job",
                    job_payload(issue_number, attempt=None, conclusion="success"),
                ),
            ),
            policy=policy(),
        )

        self.assertFalse(observations["coverage"][0]["independentRecoveryEligible"])

    def test_green_lane_does_not_infer_exact_test_coverage(self) -> None:
        issue_number = 12
        observations = build_observations(
            snapshot(
                issue_payload(issue_number),
                evidence("run:100", "workflow-run", run_payload(conclusion="success")),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    job_payload(issue_number, conclusion="success"),
                ),
            ),
            policy=policy(),
        )

        self.assertEqual(["lane"], [coverage["subjectKind"] for coverage in observations["coverage"]])

    def test_explicit_successful_test_execution_evidence_produces_exact_test_coverage(self) -> None:
        issue_number = 12
        log_id = "run:100:attempt:1:job:900:log"
        observations = build_observations(
            snapshot(
                issue_payload(issue_number),
                evidence("run:100", "workflow-run", run_payload(conclusion="success")),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    job_payload(issue_number, conclusion="success", log_evidence_id=log_id),
                ),
                evidence(
                    log_id,
                    "workflow-log",
                    log_payload(issue_number, excerpt="Passed Alpha.Tests.FailingTest [42 ms]"),
                ),
            ),
            policy=policy(),
        )

        self.assertEqual(
            ["lane", "test"],
            [coverage["subjectKind"] for coverage in observations["coverage"]],
        )
        self.assertEqual(
            "ci:aspire-hosting-tests:ubuntu-latest:test:alpha-tests-failingtest",
            observations["coverage"][1]["subjectId"],
        )

    def test_history_occurrences_only_affect_fingerprint_summaries(self) -> None:
        issue_number = 12
        observations = build_observations(
            snapshot(
                issue_payload(
                    issue_number,
                    facts=[fact("testName", "Alpha.Tests.FailingTest")],
                ),
                evidence("run:100", "workflow-run", run_payload()),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    job_payload(issue_number),
                ),
            ),
            policy=policy(),
            history={
                "occurrences": [
                    {
                        "occurrenceId": "occurrence:11:99:1:901:1",
                        "issueNumber": 11,
                        "runId": 99,
                        "attempt": 1,
                        "jobId": 901,
                        "workflow": "CI",
                        "lane": "Aspire.Hosting.Tests",
                        "os": "ubuntu-latest",
                        "headSha": "c" * 40,
                        "observedAt": "2026-08-18T15:30:00Z",
                        "testName": "Alpha.Tests.FailingTest",
                        "fingerprintId": "test:alpha-tests-failingtest",
                        "allowedCauses": ["unknown"],
                        "retrySafe": True,
                        "evidenceIds": ["stale:evidence"],
                        "selectedCause": "unknown",
                        "proposal": {"intent": "close"},
                    }
                ],
            },
        )

        self.assertEqual(1, len(observations["occurrences"]))
        self.assertEqual(
            ["test-flake", "test-contention", "product-regression-suspect", "unknown"],
            observations["occurrences"][0]["allowedCauses"],
        )
        self.assertFalse(observations["occurrences"][0]["retrySafe"])
        self.assertEqual(1, len(observations["fingerprints"]))
        summary = observations["fingerprints"][0]
        self.assertEqual(
            ["occurrence:11:99:1:901:1", "occurrence:12:100:1:900:1"],
            summary["occurrenceIds"],
        )
        self.assertEqual([11, 12], summary["issueNumbers"])
        self.assertEqual([99, 100], summary["distinctRunIds"])
        self.assertNotIn("selectedCause", summary)
        self.assertNotIn("proposal", summary)


class StableOrdinalTests(unittest.TestCase):
    def _snapshot(self, issue_number: int, *test_names: str) -> dict[str, object]:
        log_id = "run:100:attempt:1:job:900:log"
        return snapshot(
            issue_payload(issue_number),
            evidence("run:100", "workflow-run", run_payload()),
            evidence(
                "run:100:attempt:1:job:900",
                "workflow-job",
                job_payload(issue_number, log_evidence_id=log_id),
            ),
            evidence(
                log_id,
                "workflow-log",
                {
                    **log_payload(issue_number, excerpt="failed"),
                    "facts": [fact("testName", name) for name in test_names],
                },
            ),
        )

    def _ids_by_test(self, observations: dict[str, object]) -> dict[str | None, str]:
        return {
            occurrence["testName"]: occurrence["occurrenceId"]
            for occurrence in observations["occurrences"]
        }

    def test_cold_run_assigns_ordinals_by_deterministic_sorted_identity(self) -> None:
        observations = build_observations(
            self._snapshot(12, "Beta.Tests.Two", "Alpha.Tests.One"),
            policy=policy(),
        )

        self.assertEqual(
            {
                "Alpha.Tests.One": "occurrence:12:100:1:900:1",
                "Beta.Tests.Two": "occurrence:12:100:1:900:2",
            },
            self._ids_by_test(observations),
        )

    def test_new_facts_do_not_renumber_existing_occurrences(self) -> None:
        first = build_observations(self._snapshot(12, "Beta.Tests.Two"), policy=policy())
        self.assertEqual(
            {"Beta.Tests.Two": "occurrence:12:100:1:900:1"},
            self._ids_by_test(first),
        )

        grown = build_observations(
            self._snapshot(12, "Beta.Tests.Two", "Alpha.Tests.One"),
            policy=policy(),
            history={"occurrences": first["occurrences"]},
        )

        self.assertEqual(
            {
                "Beta.Tests.Two": "occurrence:12:100:1:900:1",
                "Alpha.Tests.One": "occurrence:12:100:1:900:2",
            },
            self._ids_by_test(grown),
        )

    def test_removed_facts_do_not_renumber_the_survivors(self) -> None:
        grown = build_observations(
            self._snapshot(12, "Alpha.Tests.One", "Beta.Tests.Two"),
            policy=policy(),
        )
        shrunk = build_observations(
            self._snapshot(12, "Beta.Tests.Two"),
            policy=policy(),
            history={"occurrences": grown["occurrences"]},
        )

        self.assertEqual(
            {"Beta.Tests.Two": "occurrence:12:100:1:900:2"},
            self._ids_by_test(shrunk),
        )
        # The removed fact survives only in the history-fed summaries.
        self.assertEqual(
            ["test:alpha-tests-one", "test:beta-tests-two"],
            [summary["fingerprintId"] for summary in shrunk["fingerprints"]],
        )

    def test_re_added_facts_reclaim_their_original_ordinal(self) -> None:
        grown = build_observations(
            self._snapshot(12, "Alpha.Tests.One", "Beta.Tests.Two"),
            policy=policy(),
        )
        shrunk = build_observations(
            self._snapshot(12, "Beta.Tests.Two"),
            policy=policy(),
            history={"occurrences": grown["occurrences"]},
        )
        history = {
            occurrence["occurrenceId"]: occurrence
            for occurrence in (*grown["occurrences"], *shrunk["occurrences"])
        }

        re_added = build_observations(
            self._snapshot(12, "Alpha.Tests.One", "Beta.Tests.Two"),
            policy=policy(),
            history={"occurrences": sorted(history.values(), key=lambda item: item["occurrenceId"])},
        )

        self.assertEqual(
            {
                "Alpha.Tests.One": "occurrence:12:100:1:900:1",
                "Beta.Tests.Two": "occurrence:12:100:1:900:2",
            },
            self._ids_by_test(re_added),
        )

    def test_expired_history_identity_does_not_abort_the_build(self) -> None:
        expired = {
            "occurrenceId": "occurrence:12:100:1:900:1",
            "fingerprintId": "test:gamma-tests-removed",
            "testName": "Gamma.Tests.Removed",
            "issueNumber": 12,
            "runId": 100,
            "attempt": 1,
            "jobId": 900,
            "observedAt": "2026-08-19T15:00:00Z",
        }

        observations = build_observations(
            self._snapshot(12, "Alpha.Tests.One"),
            policy=policy(),
            history={"occurrences": [expired]},
        )

        self.assertEqual(
            {"Alpha.Tests.One": "occurrence:12:100:1:900:2"},
            self._ids_by_test(observations),
        )

    def test_every_ordinal_history_published_in_a_group_stays_reserved(self) -> None:
        # One identity can hold more than one published ordinal: an earlier cycle
        # numbered it differently, and both IDs were quoted in issue comments. Only
        # reserving the lowest would hand a published ID to a different identity.
        group = {
            "issueNumber": 12,
            "runId": 100,
            "attempt": 1,
            "jobId": 900,
            "observedAt": "2026-08-19T15:00:00Z",
        }
        history = {
            "occurrences": [
                {
                    **group,
                    "occurrenceId": "occurrence:12:100:1:900:1",
                    "fingerprintId": "test:alpha-tests-one",
                    "testName": "Alpha.Tests.One",
                },
                {
                    **group,
                    "occurrenceId": "occurrence:12:100:1:900:2",
                    "fingerprintId": "test:alpha-tests-one",
                    "testName": "Alpha.Tests.One",
                },
                {
                    **group,
                    "occurrenceId": "occurrence:12:100:1:900:3",
                    "fingerprintId": "test:beta-tests-two",
                    "testName": "Beta.Tests.Two",
                },
            ]
        }

        observations = build_observations(
            self._snapshot(12, "Alpha.Tests.One", "Gamma.Tests.Three"),
            policy=policy(),
            history=history,
        )

        self.assertEqual(
            {
                "Alpha.Tests.One": "occurrence:12:100:1:900:1",
                "Gamma.Tests.Three": "occurrence:12:100:1:900:4",
            },
            self._ids_by_test(observations),
        )

    def test_historical_test_name_must_be_a_string_or_null(self) -> None:
        base = {
            "occurrenceId": "occurrence:12:100:1:900:1",
            "fingerprintId": "test:alpha-tests-one",
            "issueNumber": 12,
            "runId": 100,
            "attempt": 1,
            "jobId": 900,
            "observedAt": "2026-08-19T15:00:00Z",
        }
        for test_name in (5, True, ["Alpha.Tests.One"], {"name": "Alpha.Tests.One"}, ""):
            with self.subTest(testName=test_name):
                with self.assertRaisesRegex(ValueError, r"history\.occurrences\[0\]\.testName"):
                    build_observations(
                        self._snapshot(12, "Alpha.Tests.One"),
                        policy=policy(),
                        history={"occurrences": [{**base, "testName": test_name}]},
                    )

    def test_historical_occurrence_may_omit_a_test_name(self) -> None:
        base = {
            "occurrenceId": "occurrence:12:100:1:900:1",
            "fingerprintId": "infra:http-502:ubuntu-latest:download",
            "issueNumber": 12,
            "runId": 100,
            "attempt": 1,
            "jobId": 900,
            "observedAt": "2026-08-19T15:00:00Z",
        }
        for occurrence in (base, {**base, "testName": None}):
            with self.subTest(testName=occurrence.get("testName")):
                observations = build_observations(
                    self._snapshot(12, "Alpha.Tests.One"),
                    policy=policy(),
                    history={"occurrences": [occurrence]},
                )

                self.assertEqual(
                    {"Alpha.Tests.One": "occurrence:12:100:1:900:2"},
                    self._ids_by_test(observations),
                )

    def test_history_reusing_one_ordinal_for_two_identities_is_rejected(self) -> None:
        base = {
            "occurrenceId": "occurrence:12:100:1:900:1",
            "issueNumber": 12,
            "runId": 100,
            "attempt": 1,
            "jobId": 900,
            "observedAt": "2026-08-19T15:00:00Z",
        }
        history = {
            "occurrences": [
                {**base, "fingerprintId": "test:alpha-tests-one", "testName": "Alpha.Tests.One"},
                {**base, "fingerprintId": "test:beta-tests-two", "testName": "Beta.Tests.Two"},
            ]
        }

        with self.assertRaisesRegex(ValueError, r"occurrence:12:100:1:900:1"):
            build_observations(self._snapshot(12, "Alpha.Tests.One"), policy=policy(), history=history)

    def test_ordinals_are_stable_per_physical_group(self) -> None:
        issue_number = 12
        first_log = "run:100:attempt:1:job:900:log"
        second_log = "run:100:attempt:1:job:901:log"

        def build(names: dict[int, list[str]], history: dict[str, object] | None) -> dict[str, object]:
            return build_observations(
                snapshot(
                    issue_payload(issue_number),
                    evidence("run:100", "workflow-run", run_payload()),
                    evidence(
                        "run:100:attempt:1:job:900",
                        "workflow-job",
                        job_payload(issue_number, log_evidence_id=first_log),
                    ),
                    evidence(
                        first_log,
                        "workflow-log",
                        {
                            **log_payload(issue_number, excerpt="failed"),
                            "facts": [fact("testName", name) for name in names[900]],
                        },
                    ),
                    evidence(
                        "run:100:attempt:1:job:901",
                        "workflow-job",
                        job_payload(
                            issue_number,
                            job_id=901,
                            name="CI / tests-mac (macos-14)",
                            log_evidence_id=second_log,
                        ),
                    ),
                    evidence(
                        second_log,
                        "workflow-log",
                        {
                            **log_payload(issue_number, job_id=901, excerpt="failed"),
                            "facts": [fact("testName", name) for name in names[901]],
                        },
                    ),
                ),
                policy=policy(),
                history=history,
            )

        first = build({900: ["Beta.Tests.Two"], 901: ["Beta.Tests.Two"]}, None)
        grown = build(
            {900: ["Alpha.Tests.One", "Beta.Tests.Two"], 901: ["Beta.Tests.Two"]},
            {"occurrences": first["occurrences"]},
        )

        self.assertEqual(
            [
                ("occurrence:12:100:1:900:1", 900, "Beta.Tests.Two"),
                ("occurrence:12:100:1:900:2", 900, "Alpha.Tests.One"),
                ("occurrence:12:100:1:901:1", 901, "Beta.Tests.Two"),
            ],
            sorted(
                (occurrence["occurrenceId"], occurrence["jobId"], occurrence["testName"])
                for occurrence in grown["occurrences"]
            ),
        )


class OccurrenceIdentityCollisionTests(unittest.TestCase):
    def test_current_occurrence_conflicting_with_history_facts_is_rejected(self) -> None:
        issue_number = 12
        current = build_observations(
            snapshot(
                issue_payload(issue_number, facts=[fact("testName", "Alpha.Tests.FailingTest")]),
                evidence("run:100", "workflow-run", run_payload()),
                evidence("run:100:attempt:1:job:900", "workflow-job", job_payload(issue_number)),
            ),
            policy=policy(),
        )
        occurrence = current["occurrences"][0]
        self.assertEqual("occurrence:12:100:1:900:1", occurrence["occurrenceId"])

        # Same occurrence identity (issue/run/attempt/job/test) recorded under the same
        # ordinal but with a different fingerprint: one of the two facts would be lost.
        corrupt = {**occurrence, "fingerprintId": "test:beta-tests-renamedtest"}

        with self.assertRaisesRegex(ValueError, r"occurrence:12:100:1:900:1"):
            build_observations(
                snapshot(
                    issue_payload(issue_number, facts=[fact("testName", "Alpha.Tests.FailingTest")]),
                    evidence("run:100", "workflow-run", run_payload()),
                    evidence("run:100:attempt:1:job:900", "workflow-job", job_payload(issue_number)),
                ),
                policy=policy(),
                history={"occurrences": [corrupt]},
            )

    def test_identical_history_and_current_occurrence_is_merged_without_error(self) -> None:
        issue_number = 12
        snapshot_payload = snapshot(
            issue_payload(issue_number, facts=[fact("testName", "Alpha.Tests.FailingTest")]),
            evidence("run:100", "workflow-run", run_payload()),
            evidence("run:100:attempt:1:job:900", "workflow-job", job_payload(issue_number)),
        )
        occurrence = build_observations(snapshot_payload, policy=policy())["occurrences"][0]

        merged = build_observations(
            snapshot_payload,
            policy=policy(),
            history={"occurrences": [occurrence]},
        )

        self.assertEqual(
            ["occurrence:12:100:1:900:1"],
            merged["fingerprints"][0]["occurrenceIds"],
        )


class IssueLevelTestAttributionTests(unittest.TestCase):
    def _two_run_two_lane_snapshot(self, issue_number: int) -> dict[str, object]:
        records: list[tuple[str, dict[str, object]]] = []
        for run_id, job_base in ((100, 900), (101, 902)):
            records.append(evidence(f"run:{run_id}", "workflow-run", run_payload(run_id=run_id)))
            for offset, os_label in enumerate(("ubuntu-latest", "windows-latest")):
                job_id = job_base + offset
                records.append(
                    evidence(
                        f"run:{run_id}:attempt:1:job:{job_id}",
                        "workflow-job",
                        job_payload(
                            issue_number,
                            run_id=run_id,
                            job_id=job_id,
                            name=f"CI / tests ({os_label})",
                        ),
                    )
                )
        return snapshot(
            issue_payload(
                issue_number,
                facts=[
                    fact("testName", "Alpha.Tests.One"),
                    fact("testName", "Alpha.Tests.Two"),
                ],
            ),
            *records,
        )

    def test_issue_test_facts_are_not_multiplied_across_runs_and_lanes(self) -> None:
        issue_number = 12
        observations = build_observations(
            self._two_run_two_lane_snapshot(issue_number),
            policy=policy(),
        )

        occurrences = observations["occurrences"]

        self.assertEqual(
            [
                "occurrence:12:100:1:900:1",
                "occurrence:12:100:1:901:1",
                "occurrence:12:101:1:902:1",
                "occurrence:12:101:1:903:1",
            ],
            [occurrence["occurrenceId"] for occurrence in occurrences],
        )
        self.assertEqual([None, None, None, None], [occurrence["testName"] for occurrence in occurrences])
        self.assertEqual(
            [
                "unknown:12:100:900",
                "unknown:12:100:901",
                "unknown:12:101:902",
                "unknown:12:101:903",
            ],
            [occurrence["fingerprintId"] for occurrence in occurrences],
        )

    def test_ambiguous_issue_test_facts_do_not_fabricate_cross_os_recurrence(self) -> None:
        issue_number = 12
        observations = build_observations(
            self._two_run_two_lane_snapshot(issue_number),
            policy=policy(),
        )

        test_fingerprints = [
            summary
            for summary in observations["fingerprints"]
            if str(summary["fingerprintId"]).startswith("test:")
        ]

        self.assertEqual([], test_fingerprints)
        self.assertEqual(
            [1, 1, 1, 1],
            [summary["distinctFailureRunCount"] for summary in observations["fingerprints"]],
        )

    def test_job_scoped_test_evidence_stays_attached_to_its_own_job(self) -> None:
        issue_number = 12
        observations = build_observations(
            snapshot(
                issue_payload(issue_number),
                evidence("run:100", "workflow-run", run_payload()),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    {
                        **job_payload(issue_number, name="CI / tests (ubuntu-latest)"),
                        "facts": [fact("testName", "Alpha.Tests.One")],
                    },
                ),
                evidence(
                    "run:100:attempt:1:job:901",
                    "workflow-job",
                    job_payload(issue_number, job_id=901, name="CI / tests (windows-latest)"),
                ),
            ),
            policy=policy(),
        )

        self.assertEqual(
            [("occurrence:12:100:1:900:1", "Alpha.Tests.One"), ("occurrence:12:100:1:901:1", None)],
            [
                (occurrence["occurrenceId"], occurrence["testName"])
                for occurrence in observations["occurrences"]
            ],
        )

    def test_issue_ledger_run_without_resolvable_job_produces_job_less_occurrence(self) -> None:
        issue_number = 12
        issue = issue_payload(issue_number, facts=[fact("testName", "Alpha.Tests.One")])
        issue["ledger"] = {
            "source": "issue-body",
            "schema": "occurrences-table",
            "schemaRecognized": True,
            "sourceRecordCount": 1,
            "parsedRowCount": 1,
            "complete": True,
            "rows": [
                {
                    "date": "2026-08-19",
                    "sourceRun": 100,
                    "runUrl": f"https://github.com/{REPOSITORY}/actions/runs/100",
                    "job": "Some Lane That Was Never Collected",
                    "pullRequest": None,
                }
            ],
        }
        observations = build_observations(
            snapshot(
                issue,
                evidence("run:100", "workflow-run", run_payload()),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    job_payload(issue_number, name="CI / tests (ubuntu-latest)"),
                ),
                evidence(
                    "run:100:attempt:1:job:901",
                    "workflow-job",
                    job_payload(issue_number, job_id=901, name="CI / tests (windows-latest)"),
                ),
            ),
            policy=policy(),
        )

        job_less = [
            occurrence
            for occurrence in observations["occurrences"]
            if occurrence["jobId"] is None
        ]

        self.assertEqual(1, len(job_less))
        self.assertEqual("occurrence:12:100:none:none:1", job_less[0]["occurrenceId"])
        self.assertIsNone(job_less[0]["attempt"])
        self.assertEqual("Alpha.Tests.One", job_less[0]["testName"])
        self.assertEqual(["issue:12", "run:100"], job_less[0]["evidenceIds"])
        self.assertEqual(
            [None, None],
            [occurrence["testName"] for occurrence in observations["occurrences"] if occurrence["jobId"] is not None],
        )

    def test_issue_ledger_job_name_resolves_to_its_own_workflow_job(self) -> None:
        issue_number = 12
        issue = issue_payload(issue_number, facts=[fact("testName", "Alpha.Tests.One")])
        issue["ledger"] = {
            "source": "issue-body",
            "schema": "occurrences-table",
            "schemaRecognized": True,
            "sourceRecordCount": 1,
            "parsedRowCount": 1,
            "complete": True,
            "rows": [
                {
                    "date": "2026-08-19",
                    "sourceRun": 100,
                    "runUrl": f"https://github.com/{REPOSITORY}/actions/runs/100",
                    "job": "CI / tests (windows-latest)",
                    "pullRequest": None,
                }
            ],
        }
        observations = build_observations(
            snapshot(
                issue,
                evidence("run:100", "workflow-run", run_payload()),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    job_payload(issue_number, name="CI / tests (ubuntu-latest)"),
                ),
                evidence(
                    "run:100:attempt:1:job:901",
                    "workflow-job",
                    job_payload(issue_number, job_id=901, name="CI / tests (windows-latest)"),
                ),
            ),
            policy=policy(),
        )

        self.assertEqual(
            [("occurrence:12:100:1:900:1", None), ("occurrence:12:100:1:901:1", "Alpha.Tests.One")],
            [
                (occurrence["occurrenceId"], occurrence["testName"])
                for occurrence in observations["occurrences"]
            ],
        )
        self.assertEqual(
            ["issue:12", "run:100", "run:100:attempt:1:job:901"],
            observations["occurrences"][1]["evidenceIds"],
        )


class ParentRunEvidenceTests(unittest.TestCase):
    def test_failed_workflow_job_without_parent_run_evidence_is_rejected(self) -> None:
        issue_number = 12
        with self.assertRaisesRegex(ValueError, r"run:100:attempt:1:job:900 requires workflow-run evidence run:100"):
            build_observations(
                snapshot(
                    issue_payload(issue_number),
                    evidence("run:100:attempt:1:job:900", "workflow-job", job_payload(issue_number)),
                ),
                policy=policy(),
            )

    def test_successful_workflow_job_without_parent_run_evidence_is_rejected(self) -> None:
        issue_number = 12
        with self.assertRaisesRegex(ValueError, r"run:100:attempt:1:job:900 requires workflow-run evidence run:100"):
            build_observations(
                snapshot(
                    issue_payload(issue_number),
                    evidence(
                        "run:100:attempt:1:job:900",
                        "workflow-job",
                        job_payload(issue_number, conclusion="success"),
                    ),
                ),
                policy=policy(),
            )

    def test_unavailable_workflow_job_without_parent_run_evidence_is_ignored(self) -> None:
        issue_number = 12
        observations = build_observations(
            snapshot(
                issue_payload(issue_number),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    job_payload(issue_number),
                    availability="expired-or-unavailable",
                ),
            ),
            policy=policy(),
        )

        self.assertEqual([], observations["occurrences"])
        self.assertEqual([], observations["coverage"])


class RetrySafePatternTests(unittest.TestCase):
    def _http_502_occurrence(self, retry_safe_policy: ManualPolicy) -> dict[str, object]:
        issue_number = 12
        log_id = "run:100:attempt:1:job:900:log"
        observations = build_observations(
            snapshot(
                issue_payload(issue_number),
                evidence("run:100", "workflow-run", run_payload()),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    job_payload(
                        issue_number,
                        log_evidence_id=log_id,
                        steps=[
                            {
                                "number": 3,
                                "name": "download-artifact",
                                "status": "completed",
                                "conclusion": "failure",
                            }
                        ],
                    ),
                ),
                evidence(
                    log_id,
                    "workflow-log",
                    log_payload(issue_number, excerpt="download-artifact failed with HTTP 502"),
                ),
            ),
            policy=retry_safe_policy,
        )
        return observations["occurrences"][0]

    def test_non_canonical_policy_pattern_id_matches_normalized_occurrence_pattern(self) -> None:
        occurrence = self._http_502_occurrence(policy("HTTP_502"))

        self.assertEqual("infra:http-502:ubuntu-latest:download-artifact", occurrence["fingerprintId"])
        self.assertTrue(occurrence["retrySafe"])

    def test_canonical_policy_pattern_id_matches_normalized_occurrence_pattern(self) -> None:
        occurrence = self._http_502_occurrence(policy("http-502"))

        self.assertTrue(occurrence["retrySafe"])

    def test_unrelated_policy_pattern_id_leaves_occurrence_not_retry_safe(self) -> None:
        occurrence = self._http_502_occurrence(policy("HTTP_503"))

        self.assertFalse(occurrence["retrySafe"])


class CausePrecedenceTests(unittest.TestCase):
    def test_exact_test_identity_keeps_test_causes_despite_http_and_build_text(self) -> None:
        issue_number = 12
        log_id = "run:100:attempt:1:job:900:log"
        observations = build_observations(
            snapshot(
                issue_payload(issue_number),
                evidence("run:100", "workflow-run", run_payload()),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    {
                        **job_payload(issue_number, log_evidence_id=log_id),
                        "facts": [fact("testName", "Alpha.Tests.FailingTest")],
                    },
                ),
                evidence(
                    log_id,
                    "workflow-log",
                    log_payload(
                        issue_number,
                        excerpt=(
                            "Failed Alpha.Tests.FailingTest [3 ms]\n"
                            "  Assert.Equal() Failure: Strings differ\n"
                            "  Expected: HTTP 503 error CS1002\n"
                            "  Actual:   HTTP 200 OK\n"
                        ),
                    ),
                ),
            ),
            policy=policy(),
        )

        occurrence = observations["occurrences"][0]

        self.assertEqual("Alpha.Tests.FailingTest", occurrence["testName"])
        self.assertEqual("test:alpha-tests-failingtest", occurrence["fingerprintId"])
        self.assertEqual(
            ["test-flake", "test-contention", "product-regression-suspect", "unknown"],
            occurrence["allowedCauses"],
        )

    def test_assertion_text_is_not_recognized_as_http_or_build_diagnostic(self) -> None:
        issue_number = 12
        log_id = "run:100:attempt:1:job:900:log"
        observations = build_observations(
            snapshot(
                issue_payload(issue_number),
                evidence("run:100", "workflow-run", run_payload()),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    job_payload(issue_number, log_evidence_id=log_id),
                ),
                evidence(
                    log_id,
                    "workflow-log",
                    log_payload(
                        issue_number,
                        excerpt=(
                            "  Expected: HTTP 503\n"
                            "  Actual:   status code sequence CS1002\n"
                        ),
                    ),
                ),
            ),
            policy=policy(),
        )

        occurrence = observations["occurrences"][0]

        self.assertEqual("unknown:12:100:900", occurrence["fingerprintId"])
        self.assertEqual(["unknown"], occurrence["allowedCauses"])

    def test_build_diagnostic_line_still_takes_precedence_over_infrastructure(self) -> None:
        issue_number = 12
        log_id = "run:100:attempt:1:job:900:log"
        observations = build_observations(
            snapshot(
                issue_payload(issue_number),
                evidence("run:100", "workflow-run", run_payload()),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    job_payload(issue_number, log_evidence_id=log_id),
                ),
                evidence(
                    log_id,
                    "workflow-log",
                    log_payload(
                        issue_number,
                        excerpt=(
                            "Restore failed with HTTP 502\n"
                            "src/Program.cs(10,20): error CS1002: ; expected\n"
                        ),
                    ),
                ),
            ),
            policy=policy(),
        )

        occurrence = observations["occurrences"][0]

        self.assertEqual("build:cs1002:tests-aspire-hosting-tests-ubuntu-latest", occurrence["fingerprintId"])
        self.assertEqual(
            ["toolchain-build-break", "product-regression-suspect", "unknown"],
            occurrence["allowedCauses"],
        )


class StructuredJobDimensionTests(unittest.TestCase):
    def test_structured_job_payload_lane_os_and_failing_step_are_preferred(self) -> None:
        issue_number = 12
        log_id = "run:100:attempt:1:job:900:log"
        observations = build_observations(
            snapshot(
                issue_payload(issue_number),
                evidence("run:100", "workflow-run", run_payload()),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    {
                        **job_payload(
                            issue_number,
                            name="Tests / Aspire.Hosting.Tests (net10.0, Debug)",
                            steps=[
                                {
                                    "number": 2,
                                    "name": "Misleading Step",
                                    "status": "completed",
                                    "conclusion": "failure",
                                }
                            ],
                        ),
                        "lane": "Tests (Linux)",
                        "os": "ubuntu-latest",
                        "runnerLabels": ["ubuntu-latest"],
                        "failingStep": "Run tests",
                    },
                ),
                evidence(
                    log_id,
                    "workflow-log",
                    log_payload(issue_number, excerpt="Run tests failed with HTTP 502"),
                ),
            ),
            policy=policy(),
        )

        occurrence = observations["occurrences"][0]

        self.assertEqual("Tests (Linux)", occurrence["lane"])
        self.assertEqual("ubuntu-latest", occurrence["os"])
        self.assertEqual("infra:http-502:ubuntu-latest:run-tests", occurrence["fingerprintId"])
        self.assertEqual("Run tests", occurrence["fingerprintComponents"]["step"])

    def test_runner_labels_supply_os_when_structured_os_is_absent(self) -> None:
        issue_number = 12
        observations = build_observations(
            snapshot(
                issue_payload(issue_number),
                evidence("run:100", "workflow-run", run_payload()),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    {
                        **job_payload(issue_number, name="Tests / Aspire.Hosting.Tests (shard-3)"),
                        "lane": "Tests / Aspire.Hosting.Tests (shard-3)",
                        "runnerLabels": ["self-hosted", "windows-latest"],
                    },
                ),
            ),
            policy=policy(),
        )

        occurrence = observations["occurrences"][0]

        self.assertEqual("windows-latest", occurrence["os"])
        self.assertEqual("Tests / Aspire.Hosting.Tests (shard-3)", occurrence["lane"])

    def test_matrix_job_name_parentheses_are_not_treated_as_operating_system(self) -> None:
        issue_number = 12
        observations = build_observations(
            snapshot(
                issue_payload(issue_number),
                evidence("run:100", "workflow-run", run_payload()),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    job_payload(issue_number, name="Tests / Aspire.Hosting.Tests (net10.0, Debug)"),
                ),
            ),
            policy=policy(),
        )

        occurrence = observations["occurrences"][0]

        self.assertIsNone(occurrence["os"])
        self.assertEqual("Aspire.Hosting.Tests (net10.0, Debug)", occurrence["lane"])
        self.assertIsNone(occurrence["fingerprintComponents"]["runnerOS"])

    def test_versioned_runner_label_in_job_name_is_still_recognized_as_os(self) -> None:
        issue_number = 12
        observations = build_observations(
            snapshot(
                issue_payload(issue_number),
                evidence("run:100", "workflow-run", run_payload()),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    job_payload(issue_number, name="Tests / Aspire.Hosting.Tests (ubuntu-22.04)"),
                ),
            ),
            policy=policy(),
        )

        occurrence = observations["occurrences"][0]

        self.assertEqual("ubuntu-22.04", occurrence["os"])
        self.assertEqual("Aspire.Hosting.Tests", occurrence["lane"])


class RepoConfigEvidenceTests(unittest.TestCase):
    def _build_break_causes(self, *extra_evidence: tuple[str, dict[str, object]]) -> list[str]:
        issue_number = 12
        log_id = "run:100:attempt:1:job:900:log"
        observations = build_observations(
            snapshot(
                issue_payload(issue_number),
                evidence("run:100", "workflow-run", run_payload()),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    job_payload(issue_number, log_evidence_id=log_id),
                ),
                evidence(
                    log_id,
                    "workflow-log",
                    log_payload(issue_number, excerpt="error NETSDK1004: Assets file not found"),
                ),
                *extra_evidence,
            ),
            policy=policy(),
        )
        return list(observations["occurrences"][0]["allowedCauses"])

    def test_trusted_source_path_evidence_under_github_enables_repo_config_break(self) -> None:
        causes = self._build_break_causes(
            evidence(
                "source-path:.github/workflows/tests.yml",
                "source-path",
                {
                    "path": ".github/workflows/tests.yml",
                    "exists": True,
                    "targetRepository": REPOSITORY,
                    "referencedBy": association(12),
                },
            )
        )

        self.assertEqual(
            ["toolchain-build-break", "repo-config-break", "product-regression-suspect", "unknown"],
            causes,
        )

    def test_untrusted_payload_path_key_does_not_enable_repo_config_break(self) -> None:
        causes = self._build_break_causes(
            evidence(
                "run:100:attempt:1:job:900:log",
                "workflow-log",
                {
                    **log_payload(12, excerpt="error NETSDK1004: Assets file not found"),
                    "path": ".github/workflows/tests.yml",
                },
            )
        )

        self.assertEqual(
            ["toolchain-build-break", "product-regression-suspect", "unknown"],
            causes,
        )

    def test_removed_source_path_does_not_enable_repo_config_break(self) -> None:
        causes = self._build_break_causes(
            evidence(
                "source-path:.github/workflows/tests.yml",
                "source-path",
                {
                    "path": ".github/workflows/tests.yml",
                    "exists": False,
                    "targetRepository": REPOSITORY,
                    "referencedBy": association(12),
                },
            )
        )

        self.assertEqual(
            ["toolchain-build-break", "product-regression-suspect", "unknown"],
            causes,
        )

    def test_source_path_outside_repo_config_roots_does_not_enable_repo_config_break(self) -> None:
        causes = self._build_break_causes(
            evidence(
                "source-path:src/Aspire.Hosting/Program.cs",
                "source-path",
                {
                    "path": "src/Aspire.Hosting/Program.cs",
                    "exists": True,
                    "targetRepository": REPOSITORY,
                    "referencedBy": association(12),
                },
            )
        )

        self.assertEqual(
            ["toolchain-build-break", "product-regression-suspect", "unknown"],
            causes,
        )


class RateEvidenceCompletenessTests(unittest.TestCase):
    LANE = "CI / tests (ubuntu-latest)"
    LANE_SUBJECT = "ci:tests:ubuntu-latest"

    def _failure_only_snapshot(self, issue_number: int = 12, **run_payload_overrides: object) -> dict[str, object]:
        return snapshot(
            issue_payload(issue_number),
            evidence(
                "run:100",
                "workflow-run",
                {**run_payload(run_id=100), **run_payload_overrides},
            ),
            evidence(
                "run:100:attempt:1:job:900",
                "workflow-job",
                {
                    **job_payload(issue_number, run_id=100, job_id=900, name=self.LANE),
                    "facts": [fact("testName", "Alpha.Tests.One")],
                },
            ),
        )

    def _lane_success_snapshot(self, issue_number: int = 12) -> dict[str, object]:
        return snapshot(
            issue_payload(issue_number),
            evidence("run:100", "workflow-run", run_payload(run_id=100)),
            evidence(
                "run:100:attempt:1:job:900",
                "workflow-job",
                {
                    **job_payload(issue_number, run_id=100, job_id=900, name=self.LANE),
                    "facts": [fact("testName", "Alpha.Tests.One")],
                },
            ),
            evidence("run:101", "workflow-run", run_payload(run_id=101, conclusion="success")),
            evidence(
                "run:101:attempt:1:job:901",
                "workflow-job",
                job_payload(
                    issue_number,
                    run_id=101,
                    job_id=901,
                    name=self.LANE,
                    conclusion="success",
                ),
            ),
        )

    def _coverage(self, run_id: int, subject_id: str, observed_at: str = "2026-08-18T15:00:00Z") -> dict[str, object]:
        return {
            "coverageId": f"coverage:run:{run_id}:attempt:1:job:{run_id + 800}",
            "subjectKind": "lane",
            "subjectId": subject_id,
            "runId": run_id,
            "attempt": 1,
            "headSha": "a" * 40,
            "observedAt": observed_at,
            "status": "succeeded",
            "independentRecoveryEligible": True,
            "evidenceIds": [f"run:{run_id}"],
        }

    def test_failure_only_evidence_reports_no_probability(self) -> None:
        summary = build_observations(self._failure_only_snapshot(), policy=policy())["fingerprints"][0]

        self.assertIsNone(summary["failureRate"])
        self.assertFalse(summary["rateEvidenceComplete"])
        self.assertEqual(1, summary["distinctFailureRunCount"])
        self.assertEqual(1, summary["observedMatchingLaneRunDenominator"])

    def test_matching_lane_success_completes_the_rate(self) -> None:
        summary = build_observations(self._lane_success_snapshot(), policy=policy())["fingerprints"][0]

        self.assertTrue(summary["rateEvidenceComplete"])
        self.assertEqual(0.5, summary["failureRate"])

    def test_history_lane_coverage_reduces_a_complete_failure_rate(self) -> None:
        summary = build_observations(
            self._failure_only_snapshot(),
            policy=policy(),
            history={
                "coverage": [
                    self._coverage(101, self.LANE_SUBJECT),
                    self._coverage(102, self.LANE_SUBJECT),
                    self._coverage(103, self.LANE_SUBJECT),
                ]
            },
        )["fingerprints"][0]

        self.assertTrue(summary["rateEvidenceComplete"])
        self.assertEqual(1, summary["distinctFailureRunCount"])
        self.assertEqual(4, summary["observedMatchingLaneRunDenominator"])
        self.assertEqual(0.25, summary["failureRate"])

    def test_emitted_lane_coverage_round_trips_as_history_population(self) -> None:
        prior = build_observations(self._lane_success_snapshot(), policy=policy())
        self.assertEqual(
            [self.LANE_SUBJECT],
            sorted({record["subjectId"] for record in prior["coverage"] if record["subjectKind"] == "lane"}),
        )

        summary = build_observations(
            self._failure_only_snapshot(),
            policy=policy(),
            history={"coverage": prior["coverage"]},
        )["fingerprints"][0]

        self.assertTrue(summary["rateEvidenceComplete"])
        self.assertEqual(1, summary["distinctFailureRunCount"])
        self.assertEqual(2, summary["observedMatchingLaneRunDenominator"])
        self.assertEqual(0.5, summary["failureRate"])

    def test_history_failures_without_population_yield_an_incomplete_rate(self) -> None:
        summary = build_observations(
            self._failure_only_snapshot(),
            policy=policy(),
            history={
                "occurrences": [
                    {
                        "occurrenceId": "occurrence:11:99:1:899:1",
                        "issueNumber": 11,
                        "runId": 99,
                        "attempt": 1,
                        "jobId": 899,
                        "workflow": "CI",
                        "lane": "tests",
                        "os": "ubuntu-latest",
                        "observedAt": "2026-08-18T15:30:00Z",
                        "fingerprintId": "test:alpha-tests-one",
                    }
                ]
            },
        )["fingerprints"][0]

        self.assertIsNone(summary["failureRate"])
        self.assertFalse(summary["rateEvidenceComplete"])
        self.assertEqual(2, summary["distinctFailureRunCount"])
        self.assertEqual(2, summary["observedMatchingLaneRunDenominator"])

    def test_unrelated_lane_coverage_is_excluded_from_the_denominator(self) -> None:
        summary = build_observations(
            self._lane_success_snapshot(),
            policy=policy(),
            history={
                "coverage": [
                    self._coverage(102, "ci:tests:windows-latest"),
                    self._coverage(103, "other-workflow:tests:ubuntu-latest"),
                ]
            },
        )["fingerprints"][0]

        self.assertEqual(2, summary["observedMatchingLaneRunDenominator"])
        self.assertEqual(0.5, summary["failureRate"])

    def test_out_of_window_history_coverage_is_excluded(self) -> None:
        summary = build_observations(
            self._failure_only_snapshot(),
            policy=policy(),
            history={"coverage": [self._coverage(101, self.LANE_SUBJECT, "2026-07-01T15:00:00Z")]},
        )["fingerprints"][0]

        self.assertIsNone(summary["failureRate"])
        self.assertFalse(summary["rateEvidenceComplete"])
        self.assertEqual(1, summary["observedMatchingLaneRunDenominator"])

    def test_history_decision_records_are_rejected_as_factual_input(self) -> None:
        for field in ("causes", "proposals"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, rf"history\.{field}"):
                    build_observations(
                        self._failure_only_snapshot(),
                        policy=policy(),
                        history={field: [{"id": "anything"}]},
                    )

    def test_collector_recent_history_counts_never_inflate_the_denominator(self) -> None:
        # Shape copied from Collector._normalize_recent_run: run-level only, with no
        # workflow, lane, or OS dimension, plus a workflow-wide total count.
        summary = build_observations(
            self._failure_only_snapshot(
                recentHistory=[
                    {
                        "runId": 90 + index,
                        "attempt": 1,
                        "event": "push",
                        "branch": "main",
                        "headSha": "b" * 40,
                        "conclusion": "success",
                        "createdAt": "2026-08-18T15:00:00Z",
                        "url": "https://github.com/microsoft/aspire/actions/runs/90",
                    }
                    for index in range(5)
                ],
                recentHistoryCollected=True,
                recentHistoryTruncated=False,
                recentHistoryTotalCount=500,
            ),
            policy=policy(),
        )["fingerprints"][0]

        self.assertEqual(1, summary["observedMatchingLaneRunDenominator"])
        self.assertIsNone(summary["failureRate"])
        self.assertFalse(summary["rateEvidenceComplete"])

    def test_lane_dimensioned_recent_history_counts_as_lane_population(self) -> None:
        summary = build_observations(
            self._failure_only_snapshot(
                recentHistory=[
                    {
                        "runId": 101,
                        "attempt": 1,
                        "workflow": "CI",
                        "lane": "tests",
                        "os": "ubuntu-latest",
                        "conclusion": "success",
                        "createdAt": "2026-08-18T15:00:00Z",
                    }
                ],
                recentHistoryCollected=True,
            ),
            policy=policy(),
        )["fingerprints"][0]

        self.assertEqual(2, summary["observedMatchingLaneRunDenominator"])
        self.assertEqual(0.5, summary["failureRate"])
        self.assertTrue(summary["rateEvidenceComplete"])

    def test_unresolved_lane_identity_leaves_the_rate_incomplete(self) -> None:
        issue_number = 12
        summary = build_observations(
            snapshot(
                issue_payload(issue_number),
                evidence("run:100", "workflow-run", run_payload(run_id=100)),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    {
                        **job_payload(issue_number, run_id=100, job_id=900, name="build"),
                        "facts": [fact("testName", "Alpha.Tests.One")],
                    },
                ),
                evidence("run:101", "workflow-run", run_payload(run_id=101, conclusion="success")),
                evidence(
                    "run:101:attempt:1:job:901",
                    "workflow-job",
                    job_payload(
                        issue_number,
                        run_id=101,
                        job_id=901,
                        name="build",
                        conclusion="success",
                    ),
                ),
            ),
            policy=policy(),
        )["fingerprints"][0]

        self.assertIsNone(summary["failureRate"])
        self.assertFalse(summary["rateEvidenceComplete"])

    def _second_lane_job_snapshot(
        self,
        *,
        status: object,
        conclusion: object,
        issue_number: int = 12,
    ) -> dict[str, object]:
        """A failing lane run plus one more run on the same lane in a given state."""
        second_job = job_payload(
            issue_number,
            run_id=101,
            job_id=901,
            name=self.LANE,
        )
        second_job["status"] = status
        second_job["conclusion"] = conclusion
        return snapshot(
            issue_payload(issue_number),
            evidence("run:100", "workflow-run", run_payload(run_id=100)),
            evidence(
                "run:100:attempt:1:job:900",
                "workflow-job",
                {
                    **job_payload(issue_number, run_id=100, job_id=900, name=self.LANE),
                    "facts": [fact("testName", "Alpha.Tests.One")],
                },
            ),
            evidence("run:101", "workflow-run", run_payload(run_id=101, conclusion="success")),
            evidence("run:101:attempt:1:job:901", "workflow-job", second_job),
        )

    def test_only_a_completed_successful_lane_job_proves_the_lane_ran(self) -> None:
        # Anything other than a completed success is an execution that never
        # reported a verdict for this lane, so it cannot stand in for the
        # "ran without this failure" population a rate divides by.
        for status, conclusion, proves_execution in (
            ("completed", "success", True),
            ("completed", "failure", False),
            ("completed", "cancelled", False),
            ("completed", "skipped", False),
            ("completed", "neutral", False),
            ("completed", "stale", False),
            ("completed", "timed_out", False),
            ("completed", "action_required", False),
            ("completed", None, False),
            ("in_progress", None, False),
            ("queued", None, False),
            ("in_progress", "success", False),
        ):
            with self.subTest(status=status, conclusion=conclusion):
                summary = build_observations(
                    self._second_lane_job_snapshot(status=status, conclusion=conclusion),
                    policy=policy(),
                )["fingerprints"][0]

                self.assertEqual(proves_execution, summary["rateEvidenceComplete"])
                self.assertEqual(0.5 if proves_execution else None, summary["failureRate"])
                # The run is still an observed run on the lane either way: only the
                # proof-of-execution changes, never the counted population.
                self.assertEqual(1, summary["distinctFailureRunCount"])
                self.assertEqual(2, summary["observedMatchingLaneRunDenominator"])

    def test_lane_coverage_and_rate_population_agree_on_what_succeeded(self) -> None:
        for status, conclusion in (
            ("completed", "success"),
            ("completed", "cancelled"),
            ("completed", "skipped"),
            ("completed", None),
            ("in_progress", None),
        ):
            with self.subTest(status=status, conclusion=conclusion):
                observations = build_observations(
                    self._second_lane_job_snapshot(status=status, conclusion=conclusion),
                    policy=policy(),
                )
                emitted_lane_coverage = [
                    record for record in observations["coverage"] if record["subjectKind"] == "lane"
                ]

                self.assertEqual(
                    bool(emitted_lane_coverage),
                    observations["fingerprints"][0]["rateEvidenceComplete"],
                )

    def test_only_an_explicit_recent_history_success_proves_the_lane_ran(self) -> None:
        for conclusion, proves_execution in (
            ("success", True),
            ("failure", False),
            ("cancelled", False),
            ("skipped", False),
            ("neutral", False),
            ("stale", False),
            ("timed_out", False),
            ("action_required", False),
            (None, False),
            ("", False),
        ):
            with self.subTest(conclusion=conclusion):
                entry: dict[str, object] = {
                    "runId": 101,
                    "attempt": 1,
                    "workflow": "CI",
                    "lane": "tests",
                    "os": "ubuntu-latest",
                    "createdAt": "2026-08-18T15:00:00Z",
                }
                if conclusion is not None:
                    entry["conclusion"] = conclusion
                summary = build_observations(
                    self._failure_only_snapshot(recentHistory=[entry], recentHistoryCollected=True),
                    policy=policy(),
                )["fingerprints"][0]

                self.assertEqual(proves_execution, summary["rateEvidenceComplete"])
                self.assertEqual(0.5 if proves_execution else None, summary["failureRate"])
                self.assertEqual(2, summary["observedMatchingLaneRunDenominator"])

    def test_recent_history_success_on_another_lane_never_proves_this_lane_ran(self) -> None:
        for dimensions in (
            {"workflow": "CI", "lane": "tests", "os": "windows-latest"},
            {"workflow": "CI", "lane": "build", "os": "ubuntu-latest"},
            {"workflow": "Nightly", "lane": "tests", "os": "ubuntu-latest"},
        ):
            with self.subTest(**dimensions):
                summary = build_observations(
                    self._failure_only_snapshot(
                        recentHistory=[
                            {
                                "runId": 101,
                                "attempt": 1,
                                "conclusion": "success",
                                "createdAt": "2026-08-18T15:00:00Z",
                                **dimensions,
                            }
                        ],
                        recentHistoryCollected=True,
                    ),
                    policy=policy(),
                )["fingerprints"][0]

                self.assertFalse(summary["rateEvidenceComplete"])
                self.assertIsNone(summary["failureRate"])
                self.assertEqual(1, summary["observedMatchingLaneRunDenominator"])

    def test_history_coverage_records_must_be_well_formed(self) -> None:
        for mutation, expected in (
            ({"subjectId": ""}, r"history\.coverage\[0\]\.subjectId"),
            ({"runId": 0}, r"history\.coverage\[0\]\.runId"),
            ({"observedAt": "not-a-timestamp"}, r"history\.coverage\[0\]\.observedAt"),
            ({"subjectKind": ""}, r"history\.coverage\[0\]\.subjectKind"),
            ({"status": ""}, r"history\.coverage\[0\]\.status"),
        ):
            with self.subTest(mutation=mutation):
                with self.assertRaisesRegex(ValueError, expected):
                    build_observations(
                        self._failure_only_snapshot(),
                        policy=policy(),
                        history={"coverage": [{**self._coverage(101, self.LANE_SUBJECT), **mutation}]},
                    )


class FailureRateTests(unittest.TestCase):
    def _shared_lane_snapshot(self, issue_number: int) -> dict[str, object]:
        lane_name = "CI / tests (ubuntu-latest)"
        return snapshot(
            issue_payload(issue_number),
            evidence("run:100", "workflow-run", run_payload(run_id=100)),
            evidence(
                "run:100:attempt:1:job:900",
                "workflow-job",
                {
                    **job_payload(issue_number, run_id=100, job_id=900, name=lane_name),
                    "facts": [fact("testName", "Alpha.Tests.One")],
                },
            ),
            evidence(
                "run:100:attempt:1:job:901",
                "workflow-job",
                {
                    **job_payload(issue_number, run_id=100, job_id=901, name=lane_name),
                    "facts": [fact("testName", "Alpha.Tests.One")],
                },
            ),
            evidence("run:101", "workflow-run", run_payload(run_id=101, conclusion="success")),
            evidence(
                "run:101:attempt:1:job:902",
                "workflow-job",
                job_payload(
                    issue_number,
                    run_id=101,
                    job_id=902,
                    name=lane_name,
                    conclusion="success",
                ),
            ),
        )

    def test_failure_rate_numerator_counts_distinct_runs_not_occurrences(self) -> None:
        observations = build_observations(self._shared_lane_snapshot(12), policy=policy())

        summary = observations["fingerprints"][0]

        self.assertEqual("test:alpha-tests-one", summary["fingerprintId"])
        self.assertEqual(
            ["occurrence:12:100:1:900:1", "occurrence:12:100:1:901:1"],
            summary["occurrenceIds"],
        )
        self.assertEqual(1, summary["distinctFailureRunCount"])
        self.assertEqual(2, summary["observedMatchingLaneRunDenominator"])
        self.assertEqual(0.5, summary["failureRate"])
        self.assertLessEqual(summary["failureRate"], 1)
        self.assertGreaterEqual(
            summary["observedMatchingLaneRunDenominator"],
            summary["distinctFailureRunCount"],
        )

    def test_history_failure_runs_extend_numerator_and_denominator_together(self) -> None:
        observations = build_observations(
            self._shared_lane_snapshot(12),
            policy=policy(),
            history={
                "occurrences": [
                    {
                        "occurrenceId": "occurrence:11:99:1:899:1",
                        "issueNumber": 11,
                        "runId": 99,
                        "attempt": 1,
                        "jobId": 899,
                        "workflow": "CI",
                        "lane": "tests",
                        "os": "ubuntu-latest",
                        "observedAt": "2026-08-18T15:30:00Z",
                        "fingerprintId": "test:alpha-tests-one",
                    }
                ]
            },
        )

        summary = observations["fingerprints"][0]

        self.assertEqual(2, summary["distinctFailureRunCount"])
        self.assertEqual(3, summary["observedMatchingLaneRunDenominator"])
        self.assertAlmostEqual(2 / 3, summary["failureRate"])
        self.assertLessEqual(summary["failureRate"], 1)

    def test_multiple_distinct_tests_in_one_run_keep_each_failure_rate_bounded(self) -> None:
        issue_number = 12
        lane_name = "CI / tests (ubuntu-latest)"
        observations = build_observations(
            snapshot(
                issue_payload(issue_number),
                evidence("run:100", "workflow-run", run_payload(run_id=100)),
                evidence(
                    "run:100:attempt:1:job:900",
                    "workflow-job",
                    {
                        **job_payload(issue_number, run_id=100, job_id=900, name=lane_name),
                        "facts": [
                            fact("testName", "Alpha.Tests.One"),
                            fact("testName", "Alpha.Tests.Two"),
                        ],
                    },
                ),
                evidence("run:101", "workflow-run", run_payload(run_id=101, conclusion="success")),
                evidence(
                    "run:101:attempt:1:job:901",
                    "workflow-job",
                    job_payload(
                        issue_number,
                        run_id=101,
                        job_id=901,
                        name=lane_name,
                        conclusion="success",
                    ),
                ),
            ),
            policy=policy(),
        )

        self.assertEqual(
            [("test:alpha-tests-one", 1, 2, 0.5), ("test:alpha-tests-two", 1, 2, 0.5)],
            [
                (
                    summary["fingerprintId"],
                    summary["distinctFailureRunCount"],
                    summary["observedMatchingLaneRunDenominator"],
                    summary["failureRate"],
                )
                for summary in observations["fingerprints"]
            ],
        )


class OccurrenceRecordShapeTests(unittest.TestCase):
    def test_occurrence_record_pins_every_required_task_two_field(self) -> None:
        issue_number = 12
        observations = build_observations(
            snapshot(
                issue_payload(issue_number, facts=[fact("testName", "Alpha.Tests.FailingTest")]),
                evidence("run:100", "workflow-run", run_payload()),
                evidence("run:100:attempt:1:job:900", "workflow-job", job_payload(issue_number)),
            ),
            policy=policy(),
        )

        self.assertEqual(
            [
                {
                    "issueNumber": 12,
                    "runId": 100,
                    "attempt": 1,
                    "jobId": 900,
                    "workflow": "CI",
                    "lane": "Aspire.Hosting.Tests",
                    "os": "ubuntu-latest",
                    "headSha": "a" * 40,
                    "observedAt": "2026-08-19T15:30:00Z",
                    "testName": "Alpha.Tests.FailingTest",
                    "fingerprintId": "test:alpha-tests-failingtest",
                    "fingerprintComponents": {
                        "patternId": None,
                        "runnerOS": "ubuntu-latest",
                        "step": None,
                        "errorCode": None,
                        "job": "Tests / Aspire.Hosting.Tests (ubuntu-latest)",
                        "testName": "Alpha.Tests.FailingTest",
                    },
                    "allowedCauses": [
                        "test-flake",
                        "test-contention",
                        "product-regression-suspect",
                        "unknown",
                    ],
                    "retrySafe": False,
                    "evidenceIds": ["issue:12", "run:100", "run:100:attempt:1:job:900"],
                    "occurrenceId": "occurrence:12:100:1:900:1",
                }
            ],
            observations["occurrences"],
        )


    def test_fingerprint_summary_pins_every_required_task_two_field(self) -> None:
        issue_number = 12
        observations = build_observations(
            snapshot(
                issue_payload(issue_number, facts=[fact("testName", "Alpha.Tests.FailingTest")]),
                evidence("run:100", "workflow-run", run_payload()),
                evidence("run:100:attempt:1:job:900", "workflow-job", job_payload(issue_number)),
            ),
            policy=policy(),
        )

        self.assertEqual(
            [
                {
                    "fingerprintId": "test:alpha-tests-failingtest",
                    "occurrenceIds": ["occurrence:12:100:1:900:1"],
                    "issueNumbers": [12],
                    "distinctRunIds": [100],
                    "firstSeenAt": "2026-08-19T15:30:00Z",
                    "lastSeenAt": "2026-08-19T15:30:00Z",
                    "distinctFailureRunCount": 1,
                    "observedMatchingLaneRunDenominator": 1,
                    "rateEvidenceComplete": False,
                    "failureRate": None,
                }
            ],
            observations["fingerprints"],
        )


class AnnotationEvidenceTests(unittest.TestCase):
    def test_collector_annotation_evidence_builds_observations(self) -> None:
        # Fixture captured verbatim from Collector.enrich_github_evidence output.
        fixture = json.loads(
            (FIXTURE_ROOT / "collector-annotation-evidence.json").read_text(encoding="utf-8")
        )
        evidence_records = fixture["evidence"]
        annotation_id = "run:7001:check:8001:annotation:9001"
        self.assertIn(annotation_id, evidence_records)

        observations = build_observations(
            snapshot(
                issue_payload(11),
                *evidence_records.items(),
                collected_at=fixture["collectedAt"],
            ),
            policy=policy(),
        )

        self.assertEqual(
            [("occurrence:11:7001:1:2001:1", 2001)],
            [
                (occurrence["occurrenceId"], occurrence["jobId"])
                for occurrence in observations["occurrences"]
            ],
        )
        self.assertEqual([], observations["coverage"])
        self.assertEqual(
            [["run:7001", "run:7001:attempt:1:job:2001"]],
            [occurrence["evidenceIds"] for occurrence in observations["occurrences"]],
        )

    def test_annotation_evidence_never_becomes_its_own_lane_run_or_coverage(self) -> None:
        issue_number = 12
        annotation_id = "run:100:check:1900:annotation:5"
        observations = build_observations(
            snapshot(
                issue_payload(issue_number),
                evidence("run:100", "workflow-run", run_payload()),
                evidence("run:100:attempt:1:job:900", "workflow-job", job_payload(issue_number)),
                evidence(
                    annotation_id,
                    "workflow-job",
                    {**annotation_payload(issue_number), "status": "completed", "conclusion": "success"},
                ),
            ),
            policy=policy(),
        )

        self.assertEqual(
            ["occurrence:12:100:1:900:1"],
            [occurrence["occurrenceId"] for occurrence in observations["occurrences"]],
        )
        self.assertEqual([], observations["coverage"])
        self.assertEqual(
            [(1, 1, None, False)],
            [
                (
                    summary["distinctFailureRunCount"],
                    summary["observedMatchingLaneRunDenominator"],
                    summary["failureRate"],
                    summary["rateEvidenceComplete"],
                )
                for summary in observations["fingerprints"]
            ],
        )

    def test_annotation_identity_fields_must_match_the_evidence_id(self) -> None:
        issue_number = 12
        annotation_id = "run:100:check:1900:annotation:5"
        for field, value, expected in (
            ("runId", 101, "runId mismatch"),
            ("checkRunId", 1901, "checkRunId mismatch"),
            ("annotationId", 6, "annotationId mismatch"),
        ):
            with self.subTest(field=field):
                payload = {**annotation_payload(issue_number), field: value}
                with self.assertRaisesRegex(ValueError, expected):
                    build_observations(
                        snapshot(
                            issue_payload(issue_number),
                            evidence("run:100", "workflow-run", run_payload()),
                            evidence(
                                "run:100:attempt:1:job:900",
                                "workflow-job",
                                job_payload(issue_number),
                            ),
                            evidence(annotation_id, "workflow-job", payload),
                        ),
                        policy=policy(),
                    )

    def test_unsupported_workflow_job_evidence_ids_are_still_rejected(self) -> None:
        issue_number = 12
        with self.assertRaisesRegex(ValueError, "supported local workflow evidence ID"):
            build_observations(
                snapshot(
                    issue_payload(issue_number),
                    evidence("run:100", "workflow-run", run_payload()),
                    evidence(
                        "run:100:check:1900:annotation:0",
                        "workflow-job",
                        annotation_payload(issue_number, annotation_id=0),
                    ),
                ),
                policy=policy(),
            )


class ScopingHelperTests(unittest.TestCase):
    def test_issue_evidence_id_is_scoped_to_its_own_issue(self) -> None:
        self.assertTrue(is_scoped_to_issue("issue:12", {}, 12))
        self.assertFalse(is_scoped_to_issue("issue:12", {}, 13))

    def test_source_issue_number_scopes_a_payload(self) -> None:
        self.assertTrue(is_scoped_to_issue("run:100", {"sourceIssueNumber": 12}, 12))
        self.assertFalse(is_scoped_to_issue("run:100", {"sourceIssueNumber": 13}, 12))

    def test_referenced_by_entries_scope_a_payload(self) -> None:
        payload = {"referencedBy": [{"sourceIssueNumber": 13}, {"sourceIssueNumber": 12}]}

        self.assertTrue(is_scoped_to_issue("run:100", payload, 12))
        self.assertFalse(is_scoped_to_issue("run:100", payload, 14))

    def test_unrelated_payload_is_not_scoped(self) -> None:
        for payload in ({}, {"referencedBy": []}, {"referencedBy": "issue:12"}, {"referencedBy": [None, 12]}):
            with self.subTest(payload=payload):
                self.assertFalse(is_scoped_to_issue("run:100", payload, 12))

    def test_evidence_record_input_is_scoped_through_its_payload(self) -> None:
        record = {"kind": "workflow-run", "payload": {"referencedBy": [{"sourceIssueNumber": 12}]}}

        self.assertTrue(is_scoped_to_issue("run:100", record, 12))
        self.assertFalse(is_scoped_to_issue("run:100", {"kind": "workflow-run", "payload": {}}, 12))

    def test_mapping_subclasses_are_accepted_for_records_payloads_and_references(self) -> None:
        record = MappingSubclass(
            {
                "kind": "workflow-run",
                "payload": MappingSubclass(
                    {"referencedBy": [MappingSubclass({"sourceIssueNumber": 12})]}
                ),
            }
        )

        self.assertTrue(is_scoped_to_issue("run:100", record, 12))
        self.assertFalse(is_scoped_to_issue("run:100", record, 13))

    def test_record_without_a_mapping_payload_is_not_scoped(self) -> None:
        self.assertFalse(is_scoped_to_issue("run:100", {"kind": "workflow-run", "payload": None}, 12))


class ModuleLayeringTests(unittest.TestCase):
    def test_timezone_helpers_live_in_timeutils(self) -> None:
        self.assertTrue(callable(timeutils.parse_aware_iso8601))
        self.assertTrue(callable(timeutils.format_utc_z))

    def test_observations_does_not_own_lifecycle_decision_helpers(self) -> None:
        for name in ("summarize_identity_facts", "latest_occurrence_timestamp"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(observations, name))

    def test_lifecycle_owns_its_identity_and_latest_occurrence_helpers(self) -> None:
        for name in ("summarize_identity_facts", "latest_occurrence_timestamp"):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(lifecycle, name)))


if __name__ == "__main__":
    unittest.main()
