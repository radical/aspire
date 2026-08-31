from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ci_shepherd.quarantine import (
    apply_quarantine_source_inspection,
    build_quarantine_session_plan,
    build_quarantine_session_request as _build_quarantine_session_request,
    read_quarantine_session_events,
    record_quarantine_session_event,
    render_quarantine_session_section,
    select_quarantine_session_request,
)
from ci_shepherd.repository_policy import load_repository_policy_document
from quarantine_session import _load_request


def _repository_policy_identity(repository: str) -> dict[str, object]:
    policy = load_repository_policy_document(
        {
            "schemaVersion": 1,
            "policyVersion": "test-v1",
            "repositories": [repository],
            "retryTestResults": {
                "aggregateJobSuffixes": ["Aggregate Results"],
                "artifactNames": ["Combined-Results"],
            },
        }
    )
    return {**policy.as_public_dict(), "digest": policy.digest}


def _prepared() -> dict[str, object]:
    policy = _repository_policy_identity("owner/repo")
    return {
        "schemaVersion": 1,
        "repository": "owner/repo",
        "sourceCollectedAt": "2026-08-28T20:00:00Z",
        "snapshotId": "snapshot:owner/repo:2026-08-28T20:00:00Z",
        "repositoryPolicy": policy,
        "repositoryPolicyDigest": policy["digest"],
        "issues": [
            {
                "issueNumber": 21,
                "issueUrl": "https://github.com/owner/repo/issues/21",
                "title": "First flaky test",
                "evidenceBundle": [
                    {
                        "id": "issue:21",
                        "kind": "issue-event",
                        "payload": {"labels": []},
                    }
                ],
            },
            {
                "issueNumber": 22,
                "issueUrl": "https://github.com/owner/repo/issues/22",
                "title": "Second flaky test",
                "evidenceBundle": [
                    {
                        "id": "issue:22",
                        "kind": "issue-event",
                        "payload": {"labels": []},
                    }
                ],
            },
        ],
    }


def _judgments() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "snapshotId": "snapshot:owner/repo:2026-08-28T20:00:00Z",
        "issues": [
            {
                "issueNumber": 22,
                "category": "flaky-test",
                "recommendations": [
                    {
                        "disposition": "review-quarantine",
                        "target": {
                            "kind": "test",
                            "value": "Tests.SecondTest",
                        },
                        "confidence": "high",
                        "summary": "The test failed in three independent runs.",
                        "evidenceIds": ["issue:22", "run:220"],
                        "missingEvidence": [],
                        "reassessWhen": "After the quarantine pull request merges.",
                    }
                ],
            },
            {
                "issueNumber": 21,
                "category": "flaky-test",
                "recommendations": [
                    {
                        "disposition": "review-quarantine",
                        "target": {
                            "kind": "test",
                            "value": "Tests.FirstTest",
                        },
                        "confidence": "high",
                        "summary": "The test failed in two independent runs.",
                        "evidenceIds": ["run:210", "issue:21"],
                        "missingEvidence": [],
                        "reassessWhen": "After the quarantine pull request merges.",
                    }
                ],
            },
        ],
    }


def _class_a_observations() -> dict[str, object]:
    occurrences: list[dict[str, object]] = []
    coverage: list[dict[str, object]] = []
    for issue_number, run_id, test_name in (
        (21, 210, "Tests.FirstTest"),
        (22, 220, "Tests.SecondTest"),
    ):
        failed_log_id = (
            f"run:{run_id}:attempt:1:job:900:test-results"
        )
        passed_log_id = (
            f"run:{run_id}:attempt:2:job:901:test-results"
        )
        identity = {
            "runId": run_id,
            "headSha": f"{run_id:040x}",
            "workflow": "CI",
            "jobName": "Tests / unit (ubuntu-latest)",
            "lane": "unit",
            "os": "ubuntu-latest",
        }
        occurrences.append(
            {
                **identity,
                "occurrenceId": f"occurrence:{issue_number}:{run_id}:1:900:1",
                "issueNumber": issue_number,
                "attempt": 1,
                "jobId": 900,
                "testName": test_name,
                "testNameEvidenceId": failed_log_id,
                "evidenceIds": [
                    f"run:{run_id}",
                    f"run:{run_id}:attempt:1:job:900",
                    failed_log_id,
                ],
            }
        )
        coverage.append(
            {
                **identity,
                "coverageId": (
                    f"coverage:run:{run_id}:attempt:2:job:901:test:{test_name}"
                ),
                "subjectKind": "test",
                "subjectId": f"ci:unit:ubuntu-latest:test:{test_name.lower()}",
                "attempt": 2,
                "jobId": 901,
                "testName": test_name,
                "status": "succeeded",
                "evidenceIds": [
                    f"run:{run_id}",
                    f"run:{run_id}:attempt:2:job:901",
                    passed_log_id,
                ],
            }
        )
    return {"occurrences": occurrences, "coverage": coverage, "fingerprints": []}


def build_quarantine_session_request(
    prepared: dict[str, object],
    judgments: dict[str, object],
) -> dict[str, object]:
    return _build_quarantine_session_request(
        prepared,
        judgments,
        _class_a_observations(),
    )


class QuarantineSessionRequestTests(unittest.TestCase):
    def test_request_binds_repository_policy_digest(self) -> None:
        request = build_quarantine_session_request(
            _prepared(),
            _judgments(),
        )

        self.assertEqual(
            _prepared()["repositoryPolicyDigest"],
            request["repositoryPolicyDigest"],
        )
        self.assertEqual(
            _prepared()["repositoryPolicy"],
            request["repositoryPolicy"],
        )

    def test_request_blocks_candidates_without_repository_policy_identity(self) -> None:
        prepared = _prepared()
        del prepared["repositoryPolicyDigest"]

        request = build_quarantine_session_request(
            prepared,
            _judgments(),
        )

        self.assertEqual([], request["tests"])
        self.assertEqual(
            [
                {
                    "testName": "Tests.FirstTest",
                    "reason": "repository-policy-unavailable",
                },
                {
                    "testName": "Tests.SecondTest",
                    "reason": "repository-policy-unavailable",
                },
            ],
            request["blockedTargets"],
        )

    def test_blocks_agent_recommendation_without_class_a_evidence(self) -> None:
        request = _build_quarantine_session_request(
            _prepared(),
            _judgments(),
            {"occurrences": [], "coverage": [], "fingerprints": []},
        )

        self.assertEqual([], request["tests"])
        self.assertEqual(
            [
                {
                    "testName": "Tests.FirstTest",
                    "reason": "insufficient-evidence-class",
                    "evidenceReason": (
                        "no artifact-derived exact failing test occurrence was collected"
                    ),
                },
                {
                    "testName": "Tests.SecondTest",
                    "reason": "insufficient-evidence-class",
                    "evidenceReason": (
                        "no artifact-derived exact failing test occurrence was collected"
                    ),
                },
            ],
            request["blockedTargets"],
        )

    def test_blocks_title_derived_test_name_even_when_retry_log_shows_a_pass(
        self,
    ) -> None:
        observations = _class_a_observations()
        observations["occurrences"][0]["testNameEvidenceId"] = "issue:21"
        observations["occurrences"][0]["evidenceIds"].append("issue:21")

        request = _build_quarantine_session_request(
            _prepared(),
            _judgments(),
            observations,
        )

        self.assertEqual(
            ["Tests.SecondTest"],
            [test["testName"] for test in request["tests"]],
        )
        self.assertEqual(
            "no artifact-derived exact failing test occurrence was collected",
            request["blockedTargets"][0]["evidenceReason"],
        )

    def test_blocks_green_retry_without_exact_test_pass_evidence(self) -> None:
        observations = _class_a_observations()
        recovery = observations["coverage"][0]
        recovery["subjectKind"] = "lane"
        recovery["testName"] = None

        request = _build_quarantine_session_request(
            _prepared(),
            _judgments(),
            observations,
        )

        self.assertEqual(
            ["Tests.SecondTest"],
            [test["testName"] for test in request["tests"]],
        )
        self.assertIn(
            "no exact passing test result",
            request["blockedTargets"][0]["evidenceReason"],
        )

    def test_blocks_exact_pass_from_a_different_lane(self) -> None:
        observations = _class_a_observations()
        observations["coverage"][0]["jobName"] = "Tests / other (ubuntu-latest)"

        request = _build_quarantine_session_request(
            _prepared(),
            _judgments(),
            observations,
        )

        self.assertEqual(
            ["Tests.SecondTest"],
            [test["testName"] for test in request["tests"]],
        )
        self.assertEqual(
            "no later equivalent retry passed the exact test",
            request["blockedTargets"][0]["evidenceReason"],
        )

    def test_class_a_requires_every_retry_identity_conjunct(self) -> None:
        mutations = {
            "same head SHA": ("headSha", "f" * 40),
            "later attempt": ("attempt", 1),
            "same workflow": ("workflow", "Other"),
            "same OS": ("os", "windows-latest"),
            "successful result": ("status", "failed"),
        }
        for description, (field, value) in mutations.items():
            with self.subTest(description):
                observations = _class_a_observations()
                observations["coverage"][0][field] = value

                request = _build_quarantine_session_request(
                    _prepared(),
                    _judgments(),
                    observations,
                )

                self.assertEqual(
                    ["Tests.SecondTest"],
                    [
                        test["testName"]
                        for test in request["tests"]
                    ],
                )
                self.assertEqual(
                    "insufficient-evidence-class",
                    request["blockedTargets"][0]["reason"],
                )

    def test_applies_source_inspection_and_blocks_existing_suppressions(self) -> None:
        request = build_quarantine_session_request(_prepared(), _judgments())

        inspected = apply_quarantine_source_inspection(
            request,
            {
                "schemaVersion": 1,
                "tests": [
                    {
                        "testName": "Tests.FirstTest",
                        "status": "resolved",
                        "matches": [
                            {
                                "file": "Project/FirstTests.cs",
                                "line": 42,
                                "quarantineAttributes": [],
                                "activeIssueAttributes": [],
                                "fileSemanticDigest": "sha256:" + "c" * 64,
                                "fileQuarantines": [],
                            }
                        ],
                    },
                    {
                        "testName": "Tests.SecondTest",
                        "status": "resolved",
                        "matches": [
                            {
                                "file": "Project/SecondTests.cs",
                                "line": 84,
                                "quarantineAttributes": [
                                    {
                                        "name": "QuarantinedTest",
                                        "issueUrl": "https://github.com/owner/repo/issues/20",
                                    }
                                ],
                                "activeIssueAttributes": [],
                                "fileSemanticDigest": "sha256:" + "d" * 64,
                                "fileQuarantines": [
                                    {
                                        "testName": "Tests.SecondTest",
                                        "issueUrl": "https://github.com/owner/repo/issues/20",
                                    }
                                ],
                            }
                        ],
                    },
                ],
            },
            source_revision="a" * 40,
            source_tree_digest="sha256:" + "b" * 64,
        )

        self.assertEqual(
            ["Tests.FirstTest"],
            [test["testName"] for test in inspected["tests"]],
        )
        self.assertEqual(
            {
                "file": "Project/FirstTests.cs",
                "line": 42,
            },
            inspected["tests"][0]["sourceLocation"],
        )
        self.assertEqual(
            {
                "fileSemanticDigest": "sha256:" + "c" * 64,
                "fileQuarantines": [],
            },
            inspected["tests"][0]["sourceValidation"],
        )
        self.assertEqual("a" * 40, inspected["sourceRevision"])
        self.assertEqual(
            "sha256:" + "b" * 64,
            inspected["sourceTreeDigest"],
        )
        self.assertEqual(
            [
                {
                    "testName": "Tests.SecondTest",
                    "reason": "already-quarantined",
                    "sourceFile": "Project/SecondTests.cs",
                    "existingIssueUrl": "https://github.com/owner/repo/issues/20",
                }
            ],
            inspected["blockedTargets"],
        )

    def test_source_inspection_blocks_missing_ambiguous_and_disabled_targets(self) -> None:
        request = build_quarantine_session_request(_prepared(), _judgments())
        request["tests"].append(
            {
                **request["tests"][0],
                "testName": "Tests.DisabledTest",
            }
        )

        inspected = apply_quarantine_source_inspection(
            request,
            {
                "schemaVersion": 1,
                "tests": [
                    {
                        "testName": "Tests.FirstTest",
                        "status": "not-found",
                        "matches": [],
                    },
                    {
                        "testName": "Tests.SecondTest",
                        "status": "ambiguous",
                        "matches": [
                            {
                                "file": "One/SecondTests.cs",
                                "line": 10,
                                "quarantineAttributes": [],
                                "activeIssueAttributes": [],
                                "fileSemanticDigest": "sha256:" + "c" * 64,
                                "fileQuarantines": [],
                            },
                            {
                                "file": "Two/SecondTests.cs",
                                "line": 20,
                                "quarantineAttributes": [],
                                "activeIssueAttributes": [],
                                "fileSemanticDigest": "sha256:" + "d" * 64,
                                "fileQuarantines": [],
                            },
                        ],
                    },
                    {
                        "testName": "Tests.DisabledTest",
                        "status": "resolved",
                        "matches": [
                            {
                                "file": "DisabledTests.cs",
                                "line": 30,
                                "quarantineAttributes": [],
                                "activeIssueAttributes": [
                                    {
                                        "name": "ActiveIssue",
                                        "issueUrl": "https://github.com/owner/repo/issues/19",
                                    }
                                ],
                                "fileSemanticDigest": "sha256:" + "e" * 64,
                                "fileQuarantines": [],
                            }
                        ],
                    },
                ],
            },
            source_revision="a" * 40,
            source_tree_digest="sha256:" + "b" * 64,
        )

        self.assertEqual([], inspected["tests"])
        self.assertEqual(
            [
                {
                    "testName": "Tests.FirstTest",
                    "reason": "target-not-found-in-checkout",
                },
                {
                    "testName": "Tests.SecondTest",
                    "reason": "ambiguous-target-in-checkout",
                },
                {
                    "testName": "Tests.DisabledTest",
                    "reason": "already-suppressed",
                    "sourceFile": "DisabledTests.cs",
                    "existingAttribute": "ActiveIssue",
                    "existingIssueUrl": "https://github.com/owner/repo/issues/19",
                },
            ],
            inspected["blockedTargets"],
        )

    def test_batches_all_quarantine_candidates_with_original_issue_urls(self) -> None:
        request = build_quarantine_session_request(_prepared(), _judgments())

        self.assertEqual(1, request["schemaVersion"])
        self.assertEqual("owner/repo", request["repository"])
        self.assertEqual("prepare-quarantine-pr", request["operation"])
        self.assertTrue(request["requiresSeparateApproval"])
        self.assertEqual(
            [
                (
                    "Tests.FirstTest",
                    21,
                    "https://github.com/owner/repo/issues/21",
                    "A",
                ),
                (
                    "Tests.SecondTest",
                    22,
                    "https://github.com/owner/repo/issues/22",
                    "A",
                ),
            ],
            [
                (
                    test["testName"],
                    test["issueNumber"],
                    test["issueUrl"],
                    test["evidenceClass"],
                )
                for test in request["tests"]
            ],
        )
        for test in request["tests"]:
            self.assertIn("failed and later passed", test["evidenceReason"])
            self.assertTrue(test["failureOccurrenceId"].startswith("occurrence:"))
            self.assertTrue(test["recoveryCoverageId"].startswith("coverage:"))
            self.assertTrue(
                any(
                    value.endswith(":test-results")
                    for value in test["evidenceIds"]
                )
            )
        self.assertIn("test-management", request["workerPrompt"])
        self.assertIn("QuarantineTools once for each test", request["workerPrompt"])
        self.assertIn("affected-project builds", request["workerPrompt"])
        self.assertIn("Addresses #21", request["workerPrompt"])
        self.assertIn("Addresses #22", request["workerPrompt"])
        self.assertIn("must remain open", request["workerPrompt"])
        self.assertIn(
            "PR body must begin with `[automated] `",
            request["workerPrompt"],
        )

    def test_rejects_a_job_or_shard_label_as_a_test_method(self) -> None:
        judgments = _judgments()
        for issue in judgments["issues"]:
            issue["recommendations"][0]["target"]["value"] = (
                "Tests (Ubuntu shard 3)"
            )

        request = build_quarantine_session_request(_prepared(), judgments)

        self.assertEqual([], request["tests"])
        self.assertIsNone(request["batchId"])
        self.assertIsNone(request["workerPrompt"])
        self.assertEqual(
            [
                {
                    "testName": "Tests (Ubuntu shard 3)",
                    "reason": "not-a-test-method",
                }
            ],
            request["blockedTargets"],
        )

    def test_reads_quarantine_label_from_the_source_issue_evidence(self) -> None:
        prepared = _prepared()
        source_issue = prepared["issues"][1]
        source_issue["evidenceBundle"] = [
            {
                "id": "issue:1",
                "kind": "issue-event",
                "payload": {"labels": ["area-engineering"]},
            },
            {
                "id": "issue:22",
                "kind": "issue-event",
                "payload": {"labels": ["quarantined-test"]},
            },
        ]

        request = build_quarantine_session_request(prepared, _judgments())

        self.assertEqual(
            ["Tests.FirstTest"],
            [test["testName"] for test in request["tests"]],
        )
        self.assertEqual(
            [
                {
                    "testName": "Tests.SecondTest",
                    "reason": "already-quarantined-by-label",
                }
            ],
            request["blockedTargets"],
        )

    def test_missing_source_issue_evidence_blocks_quarantine(self) -> None:
        prepared = _prepared()
        source_issue = prepared["issues"][1]
        source_issue["evidenceBundle"] = [
            {
                "id": "issue:1",
                "kind": "issue-event",
                "payload": {"labels": ["area-engineering"]},
            }
        ]

        request = build_quarantine_session_request(prepared, _judgments())

        self.assertEqual(
            ["Tests.FirstTest"],
            [test["testName"] for test in request["tests"]],
        )
        self.assertEqual(
            [
                {
                    "testName": "Tests.SecondTest",
                    "reason": "source-labels-unavailable",
                }
            ],
            request["blockedTargets"],
        )

    def test_combines_duplicate_issue_owners_for_the_same_test(self) -> None:
        judgments = _judgments()
        judgments["issues"][0]["recommendations"][0]["target"]["value"] = (
            "Tests.FirstTest"
        )

        request = build_quarantine_session_request(_prepared(), judgments)

        self.assertEqual(1, len(request["tests"]))
        self.assertEqual([21, 22], request["tests"][0]["issueNumbers"])
        self.assertIn("Addresses #21", request["workerPrompt"])
        self.assertIn("Addresses #22", request["workerPrompt"])

    def test_selects_one_test_for_a_bounded_trial(self) -> None:
        request = build_quarantine_session_request(_prepared(), _judgments())

        selected = select_quarantine_session_request(
            request,
            "Tests.FirstTest",
        )

        self.assertEqual(
            ["Tests.FirstTest"],
            [test["testName"] for test in selected["tests"]],
        )
        self.assertNotEqual(request["batchId"], selected["batchId"])
        self.assertIn("Tests.FirstTest", selected["workerPrompt"])
        self.assertNotIn("Tests.SecondTest", selected["workerPrompt"])

    def test_suppresses_a_new_batch_while_another_session_is_active(self) -> None:
        request = build_quarantine_session_request(_prepared(), _judgments())
        plan = build_quarantine_session_plan(
            request,
            [
                {
                    "repository": "owner/repo",
                    "batchId": "quarantine:fnv1a64:older",
                    "status": "started",
                    "recordedAt": "2026-08-28T19:00:00Z",
                }
            ],
        )

        self.assertIsNone(plan["proposal"])
        self.assertEqual("another-session-active", plan["suppressionReason"])
        self.assertEqual("quarantine:fnv1a64:older", plan["activeBatchId"])

    def test_reports_outstanding_work_when_current_cycle_has_no_candidates(self) -> None:
        request = build_quarantine_session_request(
            _prepared(),
            {
                **_judgments(),
                "issues": [],
            },
        )
        active_plan = build_quarantine_session_plan(
            request,
            [
                {
                    "repository": "owner/repo",
                    "batchId": "quarantine:active",
                    "status": "started",
                }
            ],
        )
        open_plan = build_quarantine_session_plan(
            request,
            [
                {
                    "repository": "owner/repo",
                    "batchId": "quarantine:open",
                    "status": "pull-request-open",
                    "pullRequestUrl": "https://github.com/owner/repo/pull/99",
                    "tests": [],
                }
            ],
        )

        self.assertEqual(
            "another-session-active",
            active_plan["suppressionReason"],
        )
        self.assertEqual("quarantine:active", active_plan["activeBatchId"])
        self.assertEqual(
            "awaiting-pull-request",
            open_plan["suppressionReason"],
        )
        self.assertEqual(["quarantine:open"], open_plan["openBatchIds"])
        self.assertIn(
            "https://github.com/owner/repo/pull/99",
            render_quarantine_session_section(open_plan),
        )

    def test_records_one_active_session_and_terminal_result(self) -> None:
        request = build_quarantine_session_request(_prepared(), _judgments())
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            started = record_quarantine_session_event(
                state,
                request,
                status="started",
                recorded_at="2026-08-28T20:10:00Z",
                session_id="session-123",
            )

            self.assertEqual("started", started["status"])
            with self.assertRaisesRegex(ValueError, "already active"):
                record_quarantine_session_event(
                    state,
                    {
                        **request,
                        "batchId": "quarantine:fnv1a64:another",
                    },
                    status="started",
                    recorded_at="2026-08-28T20:11:00Z",
                    session_id="session-456",
                )

            opened = record_quarantine_session_event(
                state,
                request,
                status="pull-request-open",
                recorded_at="2026-08-28T20:20:00Z",
                session_id="session-123",
                pull_request_url="https://github.com/owner/repo/pull/99",
                pull_request_head_sha="a" * 40,
                completed_test_names=[
                    "Tests.FirstTest",
                    "Tests.SecondTest",
                ],
            )
            completed = record_quarantine_session_event(
                state,
                request,
                status="completed",
                recorded_at="2026-08-28T20:30:00Z",
                session_id="session-123",
                pull_request_url="https://github.com/owner/repo/pull/99",
                pull_request_head_sha="a" * 40,
                completed_test_names=[
                    "Tests.FirstTest",
                    "Tests.SecondTest",
                ],
            )

            self.assertEqual("pull-request-open", opened["status"])
            self.assertEqual("completed", completed["status"])
            self.assertEqual(
                ["started", "pull-request-open", "completed"],
                [
                    event["status"]
                    for event in read_quarantine_session_events(state)
                ],
            )

    def test_completion_requires_the_exact_validated_test_names(self) -> None:
        request = build_quarantine_session_request(_prepared(), _judgments())
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            record_quarantine_session_event(
                state,
                request,
                status="started",
                recorded_at="2026-08-28T20:10:00Z",
                session_id="session-123",
            )

            with self.assertRaisesRegex(ValueError, "Completed test names"):
                record_quarantine_session_event(
                    state,
                    request,
                    status="pull-request-open",
                    recorded_at="2026-08-28T20:20:00Z",
                    session_id="session-123",
                    pull_request_url="https://github.com/owner/repo/pull/99",
                    pull_request_head_sha="a" * 40,
                )

    def test_completed_tests_are_removed_from_a_later_batch(self) -> None:
        request = build_quarantine_session_request(_prepared(), _judgments())
        completed_test = request["tests"][0]
        plan = build_quarantine_session_plan(
            request,
            [
                {
                    "repository": "owner/repo",
                    "batchId": "quarantine:fnv1a64:older",
                    "status": "completed",
                    "recordedAt": "2026-08-28T19:00:00Z",
                    "tests": [completed_test],
                    "pullRequestUrl": "https://github.com/owner/repo/pull/98",
                }
            ],
        )

        self.assertEqual(
            ["Tests.SecondTest"],
            [test["testName"] for test in plan["proposal"]["tests"]],
        )
        self.assertNotEqual(request["batchId"], plan["proposal"]["batchId"])
        self.assertNotIn("Tests.FirstTest", plan["proposal"]["workerPrompt"])
        self.assertIn("Tests.SecondTest", plan["proposal"]["workerPrompt"])

    def test_pull_request_open_requires_a_head_sha(self) -> None:
        request = build_quarantine_session_request(_prepared(), _judgments())
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            record_quarantine_session_event(
                state,
                request,
                status="started",
                recorded_at="2026-08-28T20:10:00Z",
                session_id="session-123",
            )

            with self.assertRaisesRegex(ValueError, "head SHA"):
                record_quarantine_session_event(
                    state,
                    request,
                    status="pull-request-open",
                    recorded_at="2026-08-28T20:20:00Z",
                    session_id="session-123",
                    pull_request_url="https://github.com/owner/repo/pull/99",
                    completed_test_names=["Tests.FirstTest"],
                )

    def test_headless_legacy_open_event_cannot_complete_without_enrichment(self) -> None:
        request = build_quarantine_session_request(_prepared(), _judgments())
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            ledger = state / "ledgers" / "quarantine-sessions.jsonl"
            ledger.parent.mkdir(parents=True)
            ledger.write_text(
                json.dumps(
                    {
                        **request,
                        "status": "pull-request-open",
                        "recordedAt": "2026-08-28T20:20:00Z",
                        "sessionId": "session-123",
                        "pullRequestUrl": "https://github.com/owner/repo/pull/99",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "GET-verified and enriched"):
                record_quarantine_session_event(
                    state,
                    request,
                    status="completed",
                    recorded_at="2026-08-28T20:30:00Z",
                    session_id="session-123",
                    pull_request_url="https://github.com/owner/repo/pull/99",
                    pull_request_head_sha="a" * 40,
                    completed_test_names=[
                        "Tests.FirstTest",
                        "Tests.SecondTest",
                    ],
                )

    def test_partial_completion_records_only_validated_tests(self) -> None:
        request = build_quarantine_session_request(_prepared(), _judgments())
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            record_quarantine_session_event(
                state,
                request,
                status="started",
                recorded_at="2026-08-28T20:10:00Z",
                session_id="session-123",
            )

            opened = record_quarantine_session_event(
                state,
                request,
                status="pull-request-open",
                recorded_at="2026-08-28T20:20:00Z",
                session_id="session-123",
                pull_request_url="https://github.com/owner/repo/pull/99",
                pull_request_head_sha="a" * 40,
                completed_test_names=["Tests.FirstTest"],
            )
            next_plan = build_quarantine_session_plan(
                request,
                read_quarantine_session_events(state),
            )

            self.assertEqual(
                ["Tests.FirstTest"],
                [test["testName"] for test in opened["tests"]],
            )
            self.assertEqual(
                ["Tests.SecondTest"],
                [
                    test["testName"]
                    for test in next_plan["proposal"]["tests"]
                ],
            )

    def test_unmerged_pull_request_does_not_permanently_complete_tests(self) -> None:
        request = build_quarantine_session_request(_prepared(), _judgments())
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            record_quarantine_session_event(
                state,
                request,
                status="started",
                recorded_at="2026-08-28T20:10:00Z",
                session_id="session-123",
            )
            record_quarantine_session_event(
                state,
                request,
                status="pull-request-open",
                recorded_at="2026-08-28T20:20:00Z",
                session_id="session-123",
                pull_request_url="https://github.com/owner/repo/pull/99",
                pull_request_head_sha="a" * 40,
                completed_test_names=["Tests.FirstTest", "Tests.SecondTest"],
            )

            awaiting = build_quarantine_session_plan(
                request,
                read_quarantine_session_events(state),
            )
            self.assertEqual("awaiting-pull-request", awaiting["suppressionReason"])

            record_quarantine_session_event(
                state,
                request,
                status="failed",
                recorded_at="2026-08-28T20:30:00Z",
                session_id="session-123",
                failure_reason="The draft pull request closed without merging.",
            )
            retried = build_quarantine_session_plan(
                request,
                read_quarantine_session_events(state),
            )
            self.assertIsNotNone(retried["proposal"])

    def test_started_batch_can_reconcile_an_already_merged_pull_request(self) -> None:
        request = build_quarantine_session_request(_prepared(), _judgments())
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            record_quarantine_session_event(
                state,
                request,
                status="started",
                recorded_at="2026-08-28T20:10:00Z",
                session_id="session-123",
            )

            completed = record_quarantine_session_event(
                state,
                request,
                status="completed",
                recorded_at="2026-08-28T20:20:00Z",
                session_id="session-123",
                pull_request_url="https://github.com/owner/repo/pull/98",
                pull_request_head_sha="a" * 40,
                completed_test_names=[
                    "Tests.FirstTest",
                    "Tests.SecondTest",
                ],
            )

            self.assertEqual("completed", completed["status"])
            self.assertEqual(
                "https://github.com/owner/repo/pull/98",
                completed["pullRequestUrl"],
            )

    def test_repository_sessions_are_isolated(self) -> None:
        request = build_quarantine_session_request(_prepared(), _judgments())
        other_repository_event = {
            "repository": "other/repo",
            "batchId": "quarantine:other",
            "status": "started",
            "recordedAt": "2026-08-28T19:00:00Z",
        }

        plan = build_quarantine_session_plan(request, [other_repository_event])

        self.assertIsNotNone(plan["proposal"])

    def test_truncated_ledger_tail_does_not_swallow_a_new_event(self) -> None:
        request = build_quarantine_session_request(_prepared(), _judgments())
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            ledger = state / "ledgers" / "quarantine-sessions.jsonl"
            ledger.parent.mkdir(parents=True)
            ledger.write_text('{"truncated":', encoding="utf-8")

            event = record_quarantine_session_event(
                state,
                request,
                status="started",
                recorded_at="2026-08-28T20:10:00Z",
                session_id="session-123",
            )

            self.assertEqual(
                [event],
                read_quarantine_session_events(state),
            )

    def test_later_cycle_plan_can_recover_an_abandoned_session(self) -> None:
        request = build_quarantine_session_request(_prepared(), _judgments())
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            state = root / "state"
            started = record_quarantine_session_event(
                state,
                request,
                status="started",
                recorded_at="2026-08-28T20:10:00Z",
                session_id="session-123",
            )
            plan = build_quarantine_session_plan(
                request,
                read_quarantine_session_events(state),
            )
            plan_path = root / "quarantine-session.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")

            recovered = _load_request(plan_path, state, None)

            self.assertEqual(started["batchId"], recovered["batchId"])
            self.assertEqual("session-123", recovered["sessionId"])

    def test_concurrent_starts_record_only_one_active_session(self) -> None:
        request = build_quarantine_session_request(_prepared(), _judgments())
        with TemporaryDirectory() as scratch:
            state = Path(scratch)

            def start(session_id: str) -> str:
                try:
                    record_quarantine_session_event(
                        state,
                        request,
                        status="started",
                        recorded_at="2026-08-28T20:10:00Z",
                        session_id=session_id,
                    )
                except ValueError:
                    return "rejected"
                return "started"

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = sorted(
                    executor.map(start, ("session-1", "session-2"))
                )

            self.assertEqual(["rejected", "started"], outcomes)
            self.assertEqual(
                1,
                len(read_quarantine_session_events(state)),
            )

    def test_batch_id_selects_one_of_multiple_open_pull_requests(self) -> None:
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            state = root / "state"
            ledger = state / "ledgers" / "quarantine-sessions.jsonl"
            ledger.parent.mkdir(parents=True)
            events = [
                {
                    "repository": "owner/repo",
                    "batchId": f"quarantine:{suffix}",
                    "status": "pull-request-open",
                    "sessionId": f"session-{suffix}",
                    "tests": [],
                }
                for suffix in ("one", "two")
            ]
            ledger.write_text(
                "".join(json.dumps(event) + "\n" for event in events),
                encoding="utf-8",
            )
            plan_path = root / "quarantine-session.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "repository": "owner/repo",
                        "proposal": None,
                        "activeBatchId": None,
                        "openBatchIds": [
                            "quarantine:one",
                            "quarantine:two",
                        ],
                    }
                ),
                encoding="utf-8",
            )

            selected = _load_request(
                plan_path,
                state,
                "quarantine:two",
            )

            self.assertEqual("session-two", selected["sessionId"])

    def test_dangling_ledger_symlink_is_rejected(self) -> None:
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            ledger = state / "ledgers" / "quarantine-sessions.jsonl"
            ledger.parent.mkdir(parents=True)
            ledger.symlink_to(state / "missing-ledger.jsonl")

            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                read_quarantine_session_events(state                )

    def test_rejects_a_non_iso8601_recorded_at(self) -> None:
        request = build_quarantine_session_request(_prepared(), _judgments())
        with TemporaryDirectory() as scratch:
            with self.assertRaisesRegex(
                ValueError,
                "recordedAt must be a timezone-aware ISO-8601 timestamp",
            ):
                record_quarantine_session_event(
                    Path(scratch),
                    request,
                    status="started",
                    recorded_at="not-a-timestamp",
                    session_id="session-123",
                )

    def test_event_records_the_snapshot_id(self) -> None:
        request = build_quarantine_session_request(_prepared(), _judgments())
        with TemporaryDirectory() as scratch:
            event = record_quarantine_session_event(
                Path(scratch),
                request,
                status="started",
                recorded_at="2026-08-28T20:10:00Z",
                session_id="session-123",
            )

            self.assertEqual(request["snapshotId"], event["snapshotId"])

    def test_failed_event_requires_a_reason(self) -> None:
        request = build_quarantine_session_request(_prepared(), _judgments())
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            record_quarantine_session_event(
                state,
                request,
                status="started",
                recorded_at="2026-08-28T20:10:00Z",
                session_id="session-123",
            )

            with self.assertRaisesRegex(ValueError, "failure reason"):
                record_quarantine_session_event(
                    state,
                    request,
                    status="failed",
                    recorded_at="2026-08-28T20:20:00Z",
                    session_id="session-123",
                )

    def test_pull_request_url_must_match_the_ledger_repository(self) -> None:
        request = build_quarantine_session_request(_prepared(), _judgments())
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            record_quarantine_session_event(
                state,
                request,
                status="started",
                recorded_at="2026-08-28T20:10:00Z",
                session_id="session-123",
            )

            with self.assertRaisesRegex(ValueError, "owner/repo pull request"):
                record_quarantine_session_event(
                    state,
                    request,
                    status="pull-request-open",
                    recorded_at="2026-08-28T20:20:00Z",
                    session_id="session-123",
                    pull_request_url="https://example.invalid/not-a-pull-request",
                    completed_test_names=["Tests.FirstTest"],
                )

    def test_report_lists_pending_pull_requests_with_a_new_proposal(self) -> None:
        request = build_quarantine_session_request(_prepared(), _judgments())
        plan = build_quarantine_session_plan(
            request,
            [
                {
                    "repository": "owner/repo",
                    "batchId": "quarantine:older",
                    "status": "pull-request-open",
                    "pullRequestUrl": "https://github.com/owner/repo/pull/7",
                    "tests": [request["tests"][0]],
                }
            ],
        )

        self.assertIsNotNone(plan["proposal"])
        self.assertIn(
            "https://github.com/owner/repo/pull/7",
            render_quarantine_session_section(plan),
        )

    def test_report_lists_blocked_targets_alongside_a_new_proposal(self) -> None:
        rendered = render_quarantine_session_section(
            {
                "proposal": {
                    "batchId": "quarantine:new",
                    "tests": [
                        {
                            "testName": "Tests.New",
                            "issueUrls": [
                                "https://github.com/owner/repo/issues/2"
                            ],
                        }
                    ],
                },
                "blockedTargets": [
                    {
                        "testName": "Tests.Blocked",
                        "reason": "The source method was ambiguous.",
                    }
                ],
                "pendingPullRequests": [],
            }
        )

        self.assertIn("Tests.New", rendered)
        self.assertIn("Tests.Blocked", rendered)
        self.assertIn("The source method was ambiguous.", rendered)


if __name__ == "__main__":
    unittest.main()
