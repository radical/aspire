from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ci_shepherd.poc_state import (
    case_key,
    load_latest_case_state,
    load_review_schedule,
    record_poc_ledgers,
    record_review_events,
    record_review_wakeup,
)
from ci_shepherd.replay import replay_lifecycle_scenario


def prepared(observed_at: str, *, run_id: int = 1001) -> dict[str, object]:
    return {
        "repository": "owner/repo",
        "sourceCollectedAt": observed_at,
        "issues": [
            {
                "issueNumber": 1,
                "identity": {
                    "tier1CauseId": None,
                    "tier2TestName": "Namespace.Type.Test",
                    "tier2ExceptionType": None,
                    "tier3ErrorCode": None,
                    "tier3Job": None,
                },
                "ledger": {
                    "rows": [
                        {
                            "date": "2026-08-20",
                            "sourceRun": run_id,
                            "job": "Tests / Linux",
                        }
                    ]
                },
            }
        ],
    }


def judgments(snapshot_id: str, disposition: str) -> dict[str, object]:
    return {
        "snapshotId": snapshot_id,
        "issues": [
            {
                "issueNumber": 1,
                "category": "flaky-test",
                "recommendations": [
                    {
                        "disposition": disposition,
                        "target": {
                            "kind": "test",
                            "value": "Namespace.Type.Test",
                        },
                        "confidence": "low",
                    }
                ],
            }
        ],
    }


class PocStateTests(unittest.TestCase):
    def test_schedules_only_explicit_issue_and_pull_request_wakeups(self) -> None:
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            appended = record_review_events(
                state,
                "owner/repo",
                "2026-08-20T12:00:00Z",
                issue_numbers=[1],
                pull_request_numbers=[2],
            )
            record_review_wakeup(
                state,
                "owner/repo",
                target_kind="issue",
                target_number=1,
                evaluate_at="2026-08-27T12:00:00Z",
                reason="closure-without-recurrence",
            )
            record_review_wakeup(
                state,
                "owner/repo",
                target_kind="pull-request",
                target_number=2,
                evaluate_at="2026-08-27T12:00:00Z",
                reason="pending-pr-timeout",
            )

            before_due = load_review_schedule(
                state,
                "owner/repo",
                "2026-08-27T11:59:59Z",
                issue_numbers=[1],
                pull_request_numbers=[2],
            )
            due = load_review_schedule(
                state,
                "owner/repo",
                "2026-08-27T12:00:00Z",
                issue_numbers=[1],
                pull_request_numbers=[2],
            )

            self.assertEqual(2, len(appended))
            self.assertEqual([], before_due["dueIssueNumbers"])
            self.assertEqual([], before_due["duePullRequestNumbers"])
            self.assertEqual([1], due["dueIssueNumbers"])
            self.assertEqual([2], due["duePullRequestNumbers"])
            self.assertEqual(
                "2026-08-27T12:00:00Z",
                due["issues"]["1"]["reassessAt"],
            )
            self.assertEqual(
                "closure-without-recurrence",
                due["issues"]["1"]["wakeReason"],
            )

    def test_preserves_review_events_after_an_interrupted_trailing_newline(self) -> None:
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            record_review_events(
                state,
                "owner/repo",
                "2026-08-20T12:00:00Z",
                issue_numbers=[1],
                pull_request_numbers=[],
            )
            ledger = state / "ledgers" / "review-events.jsonl"
            ledger.write_bytes(ledger.read_bytes().rstrip(b"\n"))

            record_review_events(
                state,
                "owner/repo",
                "2026-08-21T12:00:00Z",
                issue_numbers=[1],
                pull_request_numbers=[],
            )
            schedule = load_review_schedule(
                state,
                "owner/repo",
                "2026-08-28T12:00:00Z",
                issue_numbers=[1],
                pull_request_numbers=[],
            )

            self.assertEqual(
                "2026-08-21T12:00:00Z",
                schedule["issues"]["1"]["lastReviewedAt"],
            )

    def test_loads_latest_case_state_for_repository(self) -> None:
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            record_poc_ledgers(
                state,
                "owner/repo",
                prepared("2026-08-20T06:00:00Z"),
                judgments(
                    "snapshot:owner/repo:2026-08-20T06:00:00Z",
                    "watch",
                ),
            )
            record_poc_ledgers(
                state,
                "owner/repo",
                prepared("2026-08-21T06:00:00Z", run_id=1002),
                judgments(
                    "snapshot:owner/repo:2026-08-21T06:00:00Z",
                    "review-quarantine",
                ),
            )

            latest = load_latest_case_state(state, "owner/repo")

            key = case_key(
                "owner/repo",
                1,
                {"kind": "test", "value": "Namespace.Type.Test"},
            )
            self.assertEqual("review-quarantine", latest[key]["disposition"])
            self.assertEqual("transition", latest[key]["eventKind"])

    def test_distinguishes_convergence_from_source_evidence_transition(self) -> None:
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            first_prepared = prepared("2026-08-20T06:00:00Z")
            _, first_events = record_poc_ledgers(
                state,
                "owner/repo",
                first_prepared,
                judgments(
                    "snapshot:owner/repo:2026-08-20T06:00:00Z",
                    "watch",
                ),
            )

            _, convergence_events = record_poc_ledgers(
                state,
                "owner/repo",
                prepared("2026-08-21T06:00:00Z"),
                judgments(
                    "snapshot:owner/repo:2026-08-21T06:00:00Z",
                    "review-quarantine",
                ),
            )
            _, transition_events = record_poc_ledgers(
                state,
                "owner/repo",
                prepared("2026-08-22T06:00:00Z", run_id=1002),
                judgments(
                    "snapshot:owner/repo:2026-08-22T06:00:00Z",
                    "watch",
                ),
            )

            self.assertEqual("bootstrap", first_events[0]["eventKind"])
            self.assertEqual("convergence", convergence_events[0]["eventKind"])
            self.assertEqual("transition", transition_events[0]["eventKind"])
            self.assertEqual(
                "review-quarantine",
                transition_events[0]["previousDisposition"],
            )
            self.assertEqual(
                ["bootstrap", "convergence", "transition"],
                [
                    json.loads(line)["eventKind"]
                    for line in (
                        state / "ledgers" / "case-events.jsonl"
                    )
                    .read_text(encoding="utf-8")
                    .splitlines()
                ],
            )
            self.assertEqual(
                2,
                len(
                    (state / "ledgers" / "fingerprints.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ),
            )

    def test_preserves_case_events_after_an_interrupted_trailing_newline(self) -> None:
        with TemporaryDirectory() as scratch:
            state = Path(scratch)
            record_poc_ledgers(
                state,
                "owner/repo",
                prepared("2026-08-20T06:00:00Z"),
                judgments(
                    "snapshot:owner/repo:2026-08-20T06:00:00Z",
                    "watch",
                ),
            )
            case_ledger = state / "ledgers" / "case-events.jsonl"
            case_ledger.write_bytes(case_ledger.read_bytes().rstrip(b"\n"))

            _, events = record_poc_ledgers(
                state,
                "owner/repo",
                prepared("2026-08-21T06:00:00Z"),
                judgments(
                    "snapshot:owner/repo:2026-08-21T06:00:00Z",
                    "review-quarantine",
                ),
            )

            self.assertEqual("convergence", events[0]["eventKind"])
            persisted = [
                json.loads(line)
                for line in case_ledger.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                ["bootstrap", "convergence"],
                [event["eventKind"] for event in persisted],
            )

    def test_replays_bootstrap_unchanged_and_transition_cycles(self) -> None:
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            scenario = root / "scenario"
            output = root / "output"
            state = root / "state"
            _write_cycle(
                scenario / "cycle-001",
                "2026-08-20T06:00:00Z",
                "watch",
            )
            _write_cycle(
                scenario / "cycle-002",
                "2026-08-21T06:00:00Z",
                "watch",
            )
            _write_cycle(
                scenario / "cycle-003",
                "2026-08-22T06:00:00Z",
                "review-quarantine",
            )

            summary = replay_lifecycle_scenario(
                scenario_directory=scenario,
                output_directory=output,
                state_directory=state,
            )

            self.assertEqual(
                [1, 0, 1],
                [
                    cycle["caseEventsAppended"]
                    for cycle in summary["cycles"]
                ],
            )
            self.assertEqual(
                3,
                len(list((state / "runs").iterdir())),
            )
            self.assertEqual(
                ["bootstrap", "transition"],
                [
                    json.loads(line)["eventKind"]
                    for line in (
                        state / "ledgers" / "case-events.jsonl"
                    )
                    .read_text(encoding="utf-8")
                    .splitlines()
                ],
            )
            self.assertEqual(
                summary,
                json.loads(
                    (output / "replay-summary.json").read_text(encoding="utf-8")
                ),
            )
            self.assertFalse((scenario / "cycle-001" / "assessment-input.json").exists())
            for cycle in ("cycle-001", "cycle-002", "cycle-003"):
                with self.subTest(cycle=cycle):
                    generated = output / cycle
                    for artifact in (
                        "assessment-input.json",
                        "agent-input.json",
                        "agent-judgments.json",
                        "judgments.json",
                        "report.md",
                    ):
                        self.assertTrue((generated / artifact).is_file())

            first = json.loads(
                (output / "cycle-001" / "judgments.json").read_text(encoding="utf-8")
            )
            second = json.loads(
                (output / "cycle-002" / "judgments.json").read_text(encoding="utf-8")
            )
            self.assertEqual(first["issues"], second["issues"])


def _write_cycle(
    path: Path,
    observed_at: str,
    disposition: str,
) -> None:
    path.mkdir(parents=True)
    snapshot_id = f"snapshot:owner/repo:{observed_at}"
    snapshot = {
        "schemaVersion": 1,
        "repository": "owner/repo",
        "collectedAt": observed_at,
        "openIssues": [1],
        "evidence": {
            "issue:1": {
                "kind": "issue-event",
                "url": "https://github.com/owner/repo/issues/1",
                "collectedAt": observed_at,
                "availability": "available",
                "payload": {
                    "number": 1,
                    "state": "open",
                    "title": (
                        "[Failing test] Namespace.Type.Test"
                        if disposition == "watch"
                        else "[Failing test] Namespace.Type.Test (new occurrence)"
                    ),
                },
            }
        },
        "collectionErrors": [],
    }
    (path / "input.json").write_text(json.dumps(snapshot), encoding="utf-8")
    if disposition != "watch":
        judgment_document = {
            **judgments(snapshot_id, disposition),
            "schemaVersion": 1,
        }
        recommendation = judgment_document["issues"][0]["recommendations"][0]
        recommendation.update(
            {
                "summary": f"Current disposition is {disposition}.",
                "evidenceIds": ["issue:1"],
                "missingEvidence": [],
                "reassessWhen": "New independent evidence is available.",
                "target": {"kind": "issue", "value": 1},
            }
        )
        (path / "agent-overrides.json").write_text(
            json.dumps(
                {
                    "schemaVersion": judgment_document["schemaVersion"],
                    "issues": judgment_document["issues"],
                }
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
