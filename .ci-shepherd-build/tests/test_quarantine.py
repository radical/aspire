from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ci_shepherd.quarantine import (
    build_quarantine_session_plan,
    build_quarantine_session_request,
    read_quarantine_session_events,
    record_quarantine_session_event,
    render_quarantine_session_section,
    select_quarantine_session_request,
)
from quarantine_session import _load_request


def _prepared() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "repository": "owner/repo",
        "sourceCollectedAt": "2026-08-28T20:00:00Z",
        "snapshotId": "snapshot:owner/repo:2026-08-28T20:00:00Z",
        "issues": [
            {
                "issueNumber": 21,
                "issueUrl": "https://github.com/owner/repo/issues/21",
                "title": "First flaky test",
            },
            {
                "issueNumber": 22,
                "issueUrl": "https://github.com/owner/repo/issues/22",
                "title": "Second flaky test",
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


class QuarantineSessionRequestTests(unittest.TestCase):
    def test_batches_all_quarantine_candidates_with_original_issue_urls(self) -> None:
        request = build_quarantine_session_request(_prepared(), _judgments())

        self.assertEqual(1, request["schemaVersion"])
        self.assertEqual("owner/repo", request["repository"])
        self.assertEqual("prepare-quarantine-pr", request["operation"])
        self.assertTrue(request["requiresSeparateApproval"])
        self.assertEqual(
            [
                {
                    "testName": "Tests.FirstTest",
                    "issueNumber": 21,
                    "issueUrl": "https://github.com/owner/repo/issues/21",
                    "issueNumbers": [21],
                    "issueUrls": ["https://github.com/owner/repo/issues/21"],
                    "evidenceIds": ["issue:21", "run:210"],
                    "summary": "The test failed in two independent runs.",
                },
                {
                    "testName": "Tests.SecondTest",
                    "issueNumber": 22,
                    "issueUrl": "https://github.com/owner/repo/issues/22",
                    "issueNumbers": [22],
                    "issueUrls": ["https://github.com/owner/repo/issues/22"],
                    "evidenceIds": ["issue:22", "run:220"],
                    "summary": "The test failed in three independent runs.",
                },
            ],
            request["tests"],
        )
        self.assertIn("test-management", request["workerPrompt"])
        self.assertIn("QuarantineTools once for each test", request["workerPrompt"])
        self.assertIn("Build every affected test project", request["workerPrompt"])
        self.assertIn("Addresses #21", request["workerPrompt"])
        self.assertIn("Addresses #22", request["workerPrompt"])
        self.assertIn("must remain open", request["workerPrompt"])
        self.assertIn(
            "PR body must begin with `[automated] `",
            request["workerPrompt"],
        )
        self.assertIn(
            "If a target is already quarantined",
            request["workerPrompt"],
        )
        self.assertIn(
            "Identify the merged pull request",
            request["workerPrompt"],
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
                read_quarantine_session_events(state)


if __name__ == "__main__":
    unittest.main()
