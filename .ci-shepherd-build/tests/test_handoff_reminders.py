from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ci_shepherd.handoff_reminders import derive_handoff_reminders
from ci_shepherd.poc_state import (
    load_review_schedule,
    record_review_events,
    record_review_wakeup,
)
from ci_shepherd.poc_history import read_ledger_rows
from ci_shepherd.repository_policy import HandoffReminderPolicy


class HandoffReminderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = HandoffReminderPolicy(
            interval=timedelta(hours=24),
            stale_progress_interval=timedelta(days=7),
            maximum=2,
        )

    def test_non_successful_effects_leave_same_ordinal_pending(self) -> None:
        for outcome in (None, "failed", "indeterminate", "stale", "skipped"):
            with self.subTest(outcome=outcome):
                records = [_handoff_record()]
                derive_handoff_reminders(
                    records,
                    [] if outcome is None else [_terminal(outcome, ordinal=1)],
                    self.policy,
                )

                self.assertEqual(
                    {
                        "episodeId": "assignment:21:handoff",
                        "ordinal": 1,
                        "state": "pending",
                        "nextWakeup": {
                            "reason": "escalation-reminder",
                            "evaluateAt": "2026-08-21T15:00:00Z",
                        },
                    },
                    records[0]["handoffReminder"],
                )

    def test_success_advances_once_and_restart_is_stable(self) -> None:
        events = [_terminal("executed", ordinal=1)]
        first = [_handoff_record()]
        restarted = [_handoff_record()]

        derive_handoff_reminders(first, events, self.policy)
        derive_handoff_reminders(restarted, events, self.policy)

        self.assertEqual(first, restarted)
        reminder = first[0]["handoffReminder"]
        self.assertEqual(2, reminder["ordinal"])
        self.assertEqual("2026-08-23T16:00:00Z", reminder["nextWakeup"]["evaluateAt"])

    def test_success_schedules_exactly_one_next_wakeup_on_replay(self) -> None:
        with TemporaryDirectory() as directory:
            state_directory = Path(directory)
            record_review_wakeup(
                state_directory,
                "owner/repo",
                target_kind="issue",
                target_number=21,
                evaluate_at="2026-08-22T15:00:00Z",
                reason="escalation-reminder",
            )
            records = [_handoff_record()]
            derive_handoff_reminders(
                records,
                [_terminal("executed", ordinal=1)],
                self.policy,
            )
            wakeup = records[0]["nextWakeup"]
            for _ in range(2):
                record_review_wakeup(
                    state_directory,
                    "owner/repo",
                    target_kind="issue",
                    target_number=21,
                    evaluate_at=wakeup["evaluateAt"],
                    reason=wakeup["reason"],
                )

            rows = read_ledger_rows(
                state_directory / "ledgers" / "review-wakeups.jsonl"
            )
            schedule = load_review_schedule(
                state_directory,
                "owner/repo",
                "2026-08-22T17:00:00Z",
                issue_numbers=[21],
                pull_request_numbers=[],
            )

        self.assertEqual(2, len(rows))
        self.assertEqual("2026-08-23T16:00:00Z", rows[-1]["evaluateAt"])
        self.assertEqual([], schedule["dueIssueNumbers"])

    def test_maximum_transitions_to_operator_escalation(self) -> None:
        records = [_handoff_record()]
        derive_handoff_reminders(
            records,
            [
                _terminal("executed", ordinal=1),
                _terminal("executed", ordinal=2, hour=17),
            ],
            self.policy,
        )

        reminder = records[0]["handoffReminder"]
        self.assertEqual("operator-escalation", reminder["state"])
        self.assertEqual("operator-escalation", reminder["nextWakeup"]["reason"])

    def test_verified_takeover_schedules_stale_progress_without_reminder(self) -> None:
        for takeover in (
            {"humanAssigned": True},
            {
                "pullRequests": [
                    {
                        "databaseId": 101,
                        "state": "open",
                        "isDraft": False,
                        "humanAuthored": True,
                    }
                ]
            },
        ):
            with self.subTest(takeover=takeover):
                record = _handoff_record()
                record.update(takeover)
                derive_handoff_reminders([record], [], self.policy)

                reminder = record["handoffReminder"]
                self.assertEqual("human-owned", reminder["state"])
                self.assertEqual(
                    "human-stale-progress",
                    reminder["nextWakeup"]["reason"],
                )

    def test_review_event_does_not_consume_transactional_wakeup(self) -> None:
        with TemporaryDirectory() as directory:
            state_directory = Path(directory)
            record_review_wakeup(
                state_directory,
                "owner/repo",
                target_kind="issue",
                target_number=21,
                evaluate_at="2026-08-22T15:00:00Z",
                reason="escalation-reminder",
            )
            record_review_events(
                state_directory,
                "owner/repo",
                "2026-08-22T16:00:00Z",
                issue_numbers=[21],
                pull_request_numbers=[],
            )

            schedule = load_review_schedule(
                state_directory,
                "owner/repo",
                "2026-08-22T17:00:00Z",
                issue_numbers=[21],
                pull_request_numbers=[],
            )

        self.assertEqual([21], schedule["dueIssueNumbers"])
        self.assertEqual(
            "escalation-reminder",
            schedule["issues"]["21"]["wakeReason"],
        )


def _handoff_record() -> dict[str, object]:
    return {
        "actionId": "assignment:21",
        "repository": "owner/repo",
        "issueNumber": 21,
        "startedAt": "2026-08-21T14:00:00Z",
        "handoffStartedAt": "2026-08-21T15:00:00Z",
        "taskId": "task-21",
        "taskState": "completed",
        "lifecycle": "handoff_required",
        "requiresHuman": True,
        "pullRequests": [],
    }


def _terminal(
    outcome: str,
    *,
    ordinal: int,
    hour: int = 16,
) -> dict[str, object]:
    return {
        "eventType": "terminal",
        "outcome": outcome,
        "operation": "edit-comment",
        "idempotencyKey": "issue:21:status",
        "target": {"kind": "issue", "number": 21},
        "actionId": (
            "snapshot:issue:21:ping-human-comment:"
            f"assignment:21:handoff:reminder-{ordinal}"
        ),
        "recordedAt": f"2026-08-22T{hour:02d}:00:00Z",
    }


if __name__ == "__main__":
    unittest.main()
