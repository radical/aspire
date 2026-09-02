from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import copy
import subprocess
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


def _clean_checkout(root: Path) -> Path:
    checkout = root / "checkout"
    checkout.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(checkout)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.name=CI Shepherd",
            "-c",
            "user.email=ci-shepherd@example.invalid",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return checkout


class InvestigationLifecycleTests(unittest.TestCase):
    def test_plan_embeds_only_assigned_evidence_payloads(self) -> None:
        prepared = _prepared()
        prepared["issues"][0]["evidenceBundle"] = [
            {
                "id": "issue:21",
                "kind": "issue-event",
                "url": "https://github.com/owner/repo/issues/21",
                "availability": "available",
                "payload": {
                    "title": "Unknown CI failure",
                    "body": "issue-details-marker",
                },
            },
            {
                "id": "run:210",
                "kind": "workflow-run",
                "url": "https://github.com/owner/repo/actions/runs/210",
                "availability": "available",
                "payload": {
                    "conclusion": "failure",
                    "diagnostic": "run-details-marker",
                },
            },
            {
                "id": "run:211",
                "kind": "workflow-run",
                "url": "https://github.com/owner/repo/actions/runs/211",
                "availability": "available",
                "payload": {
                    "conclusion": "failure",
                    "diagnostic": "out-of-scope-marker",
                },
            },
        ]

        request = build_investigation_plan(prepared, _judgments(), [])["requests"][0]

        self.assertEqual(
            ["issue:21", "run:210"],
            [record["id"] for record in request["allowedEvidence"]],
        )
        self.assertIn("issue-details-marker", request["workerPrompt"])
        self.assertIn("run-details-marker", request["workerPrompt"])
        self.assertNotIn("out-of-scope-marker", request["workerPrompt"])

    def test_record_rejects_invalid_recorded_at_timestamp(self) -> None:
        request = build_investigation_plan(_prepared(), _judgments(), [])[
            "requests"
        ][0]

        with TemporaryDirectory() as scratch:
            checkout = _clean_checkout(Path(scratch))
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
                    checkout=checkout,
                )

    def test_completed_result_is_reused_until_source_evidence_changes(self) -> None:
        prepared = _prepared()
        first_plan = build_investigation_plan(prepared, _judgments(), [])
        request = first_plan["requests"][0]
        self.assertIn('"outcome": "fixable | recovered | duplicate', request["workerPrompt"])
        self.assertIn('"fixHandoff": null', request["workerPrompt"])
        self.assertIn(
            '"likelyPaths": ["repo-relative path"],',
            request["workerPrompt"],
        )
        self.assertIn(
            '"validation": ["specific validation command or test"]',
            request["workerPrompt"],
        )
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
            checkout = _clean_checkout(state)
            record_investigation_session_event(
                state,
                request,
                status="started",
                recorded_at="2026-08-28T20:20:00Z",
                session_id="investigation-session-1",
                checkout=checkout,
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
                checkout=checkout,
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
            checkout = _clean_checkout(Path(scratch))
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
                    checkout=checkout,
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
            checkout = _clean_checkout(state)
            for index, request in enumerate(requests):
                record_investigation_session_event(
                    state,
                    request,
                    status="started",
                    recorded_at=f"2026-08-28T20:2{index}:00Z",
                    session_id=f"investigation-session-{index}",
                    checkout=checkout,
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
                    checkout=checkout,
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

    def test_truncated_ledger_tail_blocks_a_new_result(self) -> None:
        request = build_investigation_plan(_prepared(), _judgments(), [])[
            "requests"
        ][0]
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            checkout = _clean_checkout(state)
            ledger = state / "ledgers" / "investigation-results.jsonl"
            ledger.parent.mkdir(parents=True)
            ledger.write_text('{"truncated":', encoding="utf-8")
            record_investigation_session_event(
                state,
                request,
                status="started",
                recorded_at="2026-08-28T20:20:00Z",
                session_id="investigation-session-1",
                checkout=checkout,
            )

            with self.assertRaisesRegex(ValueError, "incomplete final row"):
                record_investigation_result(
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
                    checkout=checkout,
                )

    def test_active_investigation_is_suppressed_until_session_fails(self) -> None:
        prepared = _prepared()
        judgments = _judgments()
        request = build_investigation_plan(prepared, judgments, [])["requests"][0]
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            checkout = _clean_checkout(state)
            started = record_investigation_session_event(
                state,
                request,
                status="started",
                recorded_at="2026-08-28T20:20:00Z",
                session_id="investigation-session-1",
                checkout=checkout,
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
                checkout=checkout,
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
            checkout = _clean_checkout(state)
            record_investigation_session_event(
                state,
                request,
                status="started",
                recorded_at="2026-08-28T20:20:00Z",
                session_id="investigation-session-1",
                checkout=checkout,
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

    def test_active_investigation_uses_the_started_request_scope(self) -> None:
        request = build_investigation_plan(_prepared(), _judgments(), [])[
            "requests"
        ][0]
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            checkout = _clean_checkout(state)
            record_investigation_session_event(
                state,
                request,
                status="started",
                recorded_at="2026-08-28T20:20:00Z",
                session_id="investigation-session-1",
                checkout=checkout,
            )
            expanded_request = {
                **request,
                "evidenceIds": [*request["evidenceIds"], "run:211"],
                "allowedEvidenceUrls": [
                    *request["allowedEvidenceUrls"],
                    "https://github.com/owner/repo/actions/runs/211",
                ],
            }
            later_plan = {
                "repository": "owner/repo",
                "requests": [expanded_request],
                "activeInvestigationIds": [],
            }

            recovered = select_investigation_request(
                later_plan,
                str(request["investigationId"]),
                state_directory=state,
            )

            self.assertEqual(request, recovered)
            with self.assertRaisesRegex(ValueError, "outside its request"):
                record_investigation_result(
                    state,
                    recovered,
                    {
                        "outcome": "inconclusive",
                        "summary": "The added run remains inconclusive.",
                        "evidenceIds": ["run:211"],
                        "reassessWhen": "When evidence changes.",
                    },
                    recorded_at="2026-08-28T20:30:00Z",
                    session_id="investigation-session-1",
                    checkout=checkout,
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
            checkout = _clean_checkout(state)
            record_investigation_session_event(
                state,
                request,
                status="started",
                recorded_at="2026-08-28T20:20:00Z",
                session_id="investigation-session-1",
                checkout=checkout,
            )

            with self.assertRaisesRegex(ValueError, "belongs to another session"):
                record_investigation_result(
                    state,
                    request,
                    result,
                    recorded_at="2026-08-28T20:30:00Z",
                    session_id="investigation-session-2",
                    checkout=checkout,
                )

            first = record_investigation_result(
                state,
                request,
                result,
                recorded_at="2026-08-28T20:30:00Z",
                session_id="investigation-session-1",
                checkout=checkout,
            )
            replay = record_investigation_result(
                state,
                request,
                result,
                recorded_at="2026-08-28T20:31:00Z",
                session_id="investigation-session-1",
                checkout=checkout,
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
            checkout = _clean_checkout(state)

            def start(session_id: str) -> str:
                try:
                    record_investigation_session_event(
                        state,
                        request,
                        status="started",
                        recorded_at="2026-08-28T20:20:00Z",
                        session_id=session_id,
                        checkout=checkout,
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

    def test_start_rejects_a_dirty_checkout(self) -> None:
        request = build_investigation_plan(_prepared(), _judgments(), [])[
            "requests"
        ][0]
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            checkout = _clean_checkout(state)
            (checkout / "unexpected.txt").write_text("changed", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "not clean"):
                record_investigation_session_event(
                    state,
                    request,
                    status="started",
                    recorded_at="2026-08-28T20:20:00Z",
                    session_id="investigation-session-1",
                    checkout=checkout,
                )

            self.assertEqual([], read_investigation_session_events(state))

    def test_result_rejects_a_dirty_checkout(self) -> None:
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
            checkout = _clean_checkout(state)
            record_investigation_session_event(
                state,
                request,
                status="started",
                recorded_at="2026-08-28T20:20:00Z",
                session_id="investigation-session-1",
                checkout=checkout,
            )
            (checkout / "unexpected.txt").write_text("changed", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "not clean"):
                record_investigation_result(
                    state,
                    request,
                    result,
                    recorded_at="2026-08-28T20:30:00Z",
                    session_id="investigation-session-1",
                    checkout=checkout,
                )

    def test_result_rejects_a_committed_checkout_change(self) -> None:
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
            checkout = _clean_checkout(state)
            record_investigation_session_event(
                state,
                request,
                status="started",
                recorded_at="2026-08-28T20:20:00Z",
                session_id="investigation-session-1",
                checkout=checkout,
            )
            (checkout / "committed.txt").write_text("changed", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(checkout), "add", "committed.txt"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "-c",
                    "user.name=CI Shepherd",
                    "-c",
                    "user.email=ci-shepherd@example.invalid",
                    "commit",
                    "-m",
                    "unexpected worker commit",
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            with self.assertRaisesRegex(ValueError, "checkout HEAD changed"):
                record_investigation_result(
                    state,
                    request,
                    result,
                    recorded_at="2026-08-28T20:30:00Z",
                    session_id="investigation-session-1",
                    checkout=checkout,
                )

    def test_stopped_stale_worker_can_be_abandoned_and_retried(self) -> None:
        prepared = _prepared()
        judgments = _judgments()
        request = build_investigation_plan(prepared, judgments, [])["requests"][0]
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            checkout = _clean_checkout(state)
            record_investigation_session_event(
                state,
                request,
                status="started",
                recorded_at="2026-08-28T20:20:00Z",
                session_id="investigation-session-1",
                checkout=checkout,
            )

            with self.assertRaisesRegex(ValueError, "one-hour session limit"):
                record_investigation_session_event(
                    state,
                    request,
                    status="abandoned",
                    recorded_at="2026-08-28T20:30:00Z",
                    session_id="investigation-session-1",
                    checkout=checkout,
                    failure_reason="The worker is no longer running.",
                    confirm_worker_stopped=True,
                )

            abandoned = record_investigation_session_event(
                state,
                request,
                status="abandoned",
                recorded_at="2026-08-28T21:20:00Z",
                session_id="investigation-session-1",
                checkout=checkout,
                failure_reason="The worker is no longer running.",
                confirm_worker_stopped=True,
            )
            retry = build_investigation_plan(
                prepared,
                judgments,
                [],
                read_investigation_session_events(state),
            )

            self.assertEqual("worker-unavailable", abandoned["failureCategory"])
            self.assertEqual(2, retry["requests"][0]["attempt"])


if __name__ == "__main__":
    unittest.main()
