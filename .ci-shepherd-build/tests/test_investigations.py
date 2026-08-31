from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import copy
import unittest

from ci_shepherd.investigations import (
    attach_latest_investigation_results,
    build_investigation_plan,
    read_investigation_session_events,
    read_investigation_results,
    record_investigation_result,
    record_investigation_session_event,
    select_investigation_request,
)


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
                "title": "Unknown CI failure",
                "identity": {
                    "tier1CauseId": None,
                    "tier2TestName": None,
                    "tier2ExceptionType": None,
                    "tier3ErrorCode": "exit-code-1",
                    "tier3Job": "Tests / Linux",
                },
                "evidenceBundle": [
                    {"id": "issue:21", "kind": "issue-event"},
                    {"id": "run:210", "kind": "workflow-run"},
                ],
            }
        ],
    }


def _judgments() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "snapshotId": "snapshot:owner/repo:2026-08-28T20:00:00Z",
        "issues": [
            {
                "issueNumber": 21,
                "category": "unknown",
                "recommendations": [
                    {
                        "disposition": "investigate",
                        "target": {"kind": "issue", "value": 21},
                        "confidence": "low",
                        "summary": "Determine whether the failure is actionable.",
                        "evidenceIds": ["issue:21", "run:210"],
                        "missingEvidence": ["diagnostic logs"],
                        "reassessWhen": "After the bounded investigation completes.",
                    }
                ],
            }
        ],
    }


class InvestigationLifecycleTests(unittest.TestCase):
    def test_record_rejects_invalid_recorded_at_timestamp(self) -> None:
        request = build_investigation_plan(_prepared(), _judgments(), [])[
            "requests"
        ][0]

        with TemporaryDirectory() as scratch:
            with self.assertRaisesRegex(
                ValueError,
                "recordedAt must be a timezone-aware ISO-8601 timestamp",
            ):
                record_investigation_result(
                    Path(scratch),
                    request,
                    {
                        "outcome": "inconclusive",
                        "summary": "No decisive evidence was found.",
                        "evidenceIds": ["issue:21"],
                        "reassessWhen": "When new evidence is available.",
                        "fixHandoff": None,
                    },
                    recorded_at="not-a-timestamp",
                    session_id="investigation-session-1",
                )

    def test_completed_result_is_reused_until_source_evidence_changes(self) -> None:
        prepared = _prepared()
        first_plan = build_investigation_plan(prepared, _judgments(), [])
        request = first_plan["requests"][0]
        self.assertIn('"outcome": "fixable | recovered | duplicate', request["workerPrompt"])
        self.assertIn('"fixHandoff": null', request["workerPrompt"])
        self.assertIn(
            "Do not invoke issue-investigation or discover additional evidence",
            request["workerPrompt"],
        )
        self.assertEqual(
            ["https://github.com/owner/repo/issues/21"],
            request["allowedEvidenceUrls"],
        )

        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            record_investigation_session_event(
                state,
                request,
                status="started",
                recorded_at="2026-08-28T20:20:00Z",
                session_id="investigation-session-1",
            )
            record_investigation_result(
                state,
                request,
                {
                    "outcome": "fixable",
                    "summary": "The failure is a deterministic product bug.",
                    "evidenceIds": ["issue:21", "run:210"],
                    "reassessWhen": "When the linked fix changes state.",
                    "fixHandoff": {
                        "problem": "The parser rejects valid input.",
                        "likelyPaths": ["src/Product/Parser.cs"],
                        "validation": ["Run ParserTests.ValidInput."],
                    },
                },
                recorded_at="2026-08-28T20:30:00Z",
                session_id="investigation-session-1",
            )

            results = read_investigation_results(state)
            repeated = build_investigation_plan(
                prepared,
                _judgments(),
                results,
            )
            attached = attach_latest_investigation_results(prepared, results)

            self.assertEqual([], repeated["requests"])
            self.assertEqual(
                [request["investigationId"]],
                repeated["reusedInvestigationIds"],
            )
            issue = attached["issues"][0]
            self.assertEqual(
                "fixable",
                issue["investigationResults"][0]["outcome"],
            )

            changed = copy.deepcopy(attached)
            changed["issues"][0]["evidenceBundle"].append(
                {"id": "run:211", "kind": "workflow-run"}
            )
            refreshed = build_investigation_plan(
                changed,
                _judgments(),
                results,
            )
            changed_attached = attach_latest_investigation_results(changed, results)
            self.assertEqual(1, len(refreshed["requests"]))
            self.assertNotEqual(
                request["investigationId"],
                refreshed["requests"][0]["investigationId"],
            )
            self.assertNotIn(
                "investigationResults",
                changed_attached["issues"][0],
            )

    def test_collection_timestamp_does_not_change_investigation_identity(self) -> None:
        prepared = _prepared()
        first_plan = build_investigation_plan(prepared, _judgments(), [])
        refreshed = copy.deepcopy(prepared)
        refreshed["sourceCollectedAt"] = "2026-08-29T20:00:00Z"
        refreshed["snapshotId"] = "snapshot:owner/repo:2026-08-29T20:00:00Z"
        refreshed_judgments = copy.deepcopy(_judgments())
        refreshed_judgments["snapshotId"] = refreshed["snapshotId"]

        next_plan = build_investigation_plan(refreshed, refreshed_judgments, [])

        self.assertEqual(
            first_plan["requests"][0]["investigationId"],
            next_plan["requests"][0]["investigationId"],
        )

    def test_fixable_result_requires_a_complete_fix_handoff(self) -> None:
        request = build_investigation_plan(_prepared(), _judgments(), [])[
            "requests"
        ][0]
        with TemporaryDirectory() as scratch:
            with self.assertRaisesRegex(ValueError, "fixHandoff.problem"):
                record_investigation_result(
                    Path(scratch),
                    request,
                    {
                        "outcome": "fixable",
                        "summary": "The failure is actionable.",
                        "evidenceIds": ["issue:21"],
                        "reassessWhen": "When a fix changes state.",
                        "fixHandoff": {
                            "likelyPaths": ["src/Product/Parser.cs"],
                            "validation": ["Run the parser regression test."],
                        },
                    },
                    recorded_at="2026-08-28T20:30:00Z",
                    session_id="investigation-session-1",
                )

    def test_attaches_results_for_every_target_on_the_issue(self) -> None:
        prepared = _prepared()
        judgments = _judgments()
        judgments["issues"][0]["recommendations"].append(
            {
                **judgments["issues"][0]["recommendations"][0],
                "target": {"kind": "workflow", "value": "tests.yml"},
            }
        )
        requests = build_investigation_plan(
            prepared,
            judgments,
            [],
            max_requests=2,
        )["requests"]
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            for index, request in enumerate(requests):
                record_investigation_session_event(
                    state,
                    request,
                    status="started",
                    recorded_at=f"2026-08-28T20:2{index}:00Z",
                    session_id=f"investigation-session-{index}",
                )
                record_investigation_result(
                    state,
                    request,
                    {
                        "outcome": "inconclusive",
                        "summary": f"Target {index} remains inconclusive.",
                        "evidenceIds": ["issue:21"],
                        "reassessWhen": "When evidence changes.",
                    },
                    recorded_at=f"2026-08-28T20:3{index}:00Z",
                    session_id=f"investigation-session-{index}",
                )

            attached = attach_latest_investigation_results(
                prepared,
                read_investigation_results(state),
            )

            self.assertEqual(
                2,
                len(attached["issues"][0]["investigationResults"]),
            )

    def test_limits_new_investigations_per_cycle(self) -> None:
        judgments = _judgments()
        judgments["issues"][0]["recommendations"].append(
            {
                **judgments["issues"][0]["recommendations"][0],
                "target": {"kind": "workflow", "value": "tests.yml"},
            }
        )

        plan = build_investigation_plan(
            _prepared(),
            judgments,
            [],
            max_requests=1,
        )

        self.assertEqual(1, len(plan["requests"]))
        self.assertEqual(1, len(plan["deferredRequests"]))

    def test_default_investigation_budget_limits_twelve_requests_to_five(
        self,
    ) -> None:
        judgments = _judgments()
        recommendation = judgments["issues"][0]["recommendations"][0]
        judgments["issues"][0]["recommendations"] = [
            {
                **recommendation,
                "target": {"kind": "workflow", "value": f"workflow-{index}.yml"},
            }
            for index in range(12)
        ]

        plan = build_investigation_plan(_prepared(), judgments, [])

        self.assertEqual(5, len(plan["requests"]))
        self.assertEqual(7, len(plan["deferredRequests"]))

    def test_repository_case_does_not_change_investigation_identity(self) -> None:
        prepared = _prepared()
        first = build_investigation_plan(prepared, _judgments(), [])["requests"][0]
        prepared["repository"] = "Owner/Repo"
        second = build_investigation_plan(prepared, _judgments(), [])["requests"][0]

        self.assertEqual(first["investigationId"], second["investigationId"])

    def test_truncated_ledger_tail_does_not_swallow_a_new_result(self) -> None:
        request = build_investigation_plan(_prepared(), _judgments(), [])[
            "requests"
        ][0]
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            ledger = state / "ledgers" / "investigation-results.jsonl"
            ledger.parent.mkdir(parents=True)
            ledger.write_text('{"truncated":', encoding="utf-8")
            record_investigation_session_event(
                state,
                request,
                status="started",
                recorded_at="2026-08-28T20:20:00Z",
                session_id="investigation-session-1",
            )

            result = record_investigation_result(
                state,
                request,
                {
                    "outcome": "inconclusive",
                    "summary": "The available evidence is insufficient.",
                    "evidenceIds": ["issue:21"],
                    "reassessWhen": "When evidence changes.",
                },
                recorded_at="2026-08-28T20:30:00Z",
                session_id="investigation-session-1",
            )

            self.assertEqual([result], read_investigation_results(state))

    def test_active_investigation_is_suppressed_until_session_fails(self) -> None:
        prepared = _prepared()
        judgments = _judgments()
        request = build_investigation_plan(prepared, judgments, [])["requests"][0]
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            started = record_investigation_session_event(
                state,
                request,
                status="started",
                recorded_at="2026-08-28T20:20:00Z",
                session_id="investigation-session-1",
            )

            active = build_investigation_plan(
                prepared,
                judgments,
                [],
                read_investigation_session_events(state),
            )

            self.assertEqual([], active["requests"])
            self.assertEqual(
                [request["investigationId"]],
                active["activeInvestigationIds"],
            )
            self.assertEqual("started", started["status"])

            record_investigation_session_event(
                state,
                request,
                status="failed",
                recorded_at="2026-08-28T20:30:00Z",
                session_id="investigation-session-1",
                failure_reason="The worker could not access the cited run.",
            )
            retry = build_investigation_plan(
                prepared,
                judgments,
                [],
                read_investigation_session_events(state),
            )
            self.assertEqual(1, len(retry["requests"]))
            self.assertEqual([], retry["activeInvestigationIds"])
            self.assertEqual(2, retry["requests"][0]["attempt"])

            record_investigation_session_event(
                state,
                retry["requests"][0],
                status="started",
                recorded_at="2026-08-28T20:40:00Z",
                session_id="investigation-session-2",
            )
            rejected = record_investigation_session_event(
                state,
                retry["requests"][0],
                status="failed",
                recorded_at="2026-08-28T20:45:00Z",
                session_id="investigation-session-2",
                failure_reason="The replacement result cited outside evidence.",
                failure_category="out-of-scope-evidence",
            )

            exhausted = build_investigation_plan(
                prepared,
                judgments,
                [],
                read_investigation_session_events(state),
            )
            self.assertEqual("out-of-scope-evidence", rejected["failureCategory"])
            self.assertEqual([], exhausted["requests"])
            self.assertEqual(
                "investigation-attempt-limit",
                exhausted["deferredRequests"][0]["reason"],
            )

    def test_active_investigation_can_be_recovered_from_a_later_plan(self) -> None:
        prepared = _prepared()
        judgments = _judgments()
        request = build_investigation_plan(prepared, judgments, [])["requests"][0]
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            record_investigation_session_event(
                state,
                request,
                status="started",
                recorded_at="2026-08-28T20:20:00Z",
                session_id="investigation-session-1",
            )
            later_plan = build_investigation_plan(
                prepared,
                judgments,
                [],
                read_investigation_session_events(state),
            )

            recovered = select_investigation_request(
                later_plan,
                str(request["investigationId"]),
                state_directory=state,
            )
            record_investigation_session_event(
                state,
                recovered,
                status="failed",
                recorded_at="2026-08-28T20:30:00Z",
                session_id="investigation-session-1",
                failure_reason="The worker could not access the cited run.",
            )

            self.assertEqual(request, recovered)
            self.assertEqual(
                "failed",
                read_investigation_session_events(state)[-1]["status"],
            )

    def test_result_requires_matching_started_session_and_completes_it(self) -> None:
        request = build_investigation_plan(_prepared(), _judgments(), [])[
            "requests"
        ][0]
        result = {
            "outcome": "inconclusive",
            "summary": "The available evidence is insufficient.",
            "evidenceIds": ["issue:21"],
            "reassessWhen": "When evidence changes.",
        }
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            record_investigation_session_event(
                state,
                request,
                status="started",
                recorded_at="2026-08-28T20:20:00Z",
                session_id="investigation-session-1",
            )

            with self.assertRaisesRegex(ValueError, "belongs to another session"):
                record_investigation_result(
                    state,
                    request,
                    result,
                    recorded_at="2026-08-28T20:30:00Z",
                    session_id="investigation-session-2",
                )

            first = record_investigation_result(
                state,
                request,
                result,
                recorded_at="2026-08-28T20:30:00Z",
                session_id="investigation-session-1",
            )
            replay = record_investigation_result(
                state,
                request,
                result,
                recorded_at="2026-08-28T20:30:00Z",
                session_id="investigation-session-1",
            )

            self.assertEqual(first, replay)
            self.assertEqual(
                ["started", "completed"],
                [
                    event["status"]
                    for event in read_investigation_session_events(state)
                ],
            )

    def test_concurrent_investigation_starts_record_only_one_session(self) -> None:
        request = build_investigation_plan(_prepared(), _judgments(), [])[
            "requests"
        ][0]
        with TemporaryDirectory() as scratch:
            state = Path(scratch)

            def start(session_id: str) -> str:
                try:
                    record_investigation_session_event(
                        state,
                        request,
                        status="started",
                        recorded_at="2026-08-28T20:20:00Z",
                        session_id=session_id,
                    )
                except ValueError:
                    return "rejected"
                return "started"

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = sorted(
                    executor.map(
                        start,
                        ("investigation-session-1", "investigation-session-2"),
                    )
                )

            self.assertEqual(["rejected", "started"], outcomes)
            self.assertEqual(
                1,
                len(read_investigation_session_events(state)),
            )


if __name__ == "__main__":
    unittest.main()
