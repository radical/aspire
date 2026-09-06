from __future__ import annotations

import copy
import unittest

from ci_shepherd.meaningful_progress import attach_meaningful_progress
from ci_shepherd.pull_requests import build_pull_request_current_state


class MeaningfulProgressTests(unittest.TestCase):
    def test_manager_manual_comment_advances_progress_but_automation_does_not(self) -> None:
        previous = _snapshot()
        prior_comment = _comment()
        prior_comment["payload"]["createdAt"] = "2026-08-24T10:00:00Z"
        previous["evidence"]["issue:21:comment:99"] = prior_comment
        attach_meaningful_progress(previous, None, shepherd_author="operator")

        for changes, expected_at in (
            ({}, "2026-08-25T10:00:00Z"),
            ({"body": "[automated] Checking progress."}, "2026-08-24T10:00:00Z"),
            ({"shepherdStatus": {"owned": True}}, "2026-08-24T10:00:00Z"),
            ({"authorType": None}, "2026-08-24T10:00:00Z"),
        ):
            with self.subTest(changes=changes):
                snapshot = _snapshot()
                comment = _comment()
                comment["payload"].update({"author": "operator", **changes})
                snapshot["evidence"]["issue:21:comment:101"] = comment

                attach_meaningful_progress(snapshot, previous, shepherd_author="operator")

                self.assertEqual(
                    expected_at,
                    snapshot["delegationStatus"]["records"][0]["meaningfulProgress"]["at"],
                )

    def test_manager_manual_review_advances_progress_but_automation_does_not(self) -> None:
        previous = _snapshot()
        _add_pull_request(previous, "current-head")
        prior_review = {
            "id": 50,
            "user": {"login": "maintainer", "type": "User"},
            "state": "COMMENTED",
            "body": "Please add coverage.",
            "submitted_at": "2026-08-24T10:00:00Z",
        }
        previous["evidence"]["pr:23"]["payload"]["currentState"] = build_pull_request_current_state(
            {"head": {"sha": "current-head"}}, reviews=[prior_review],
        )
        attach_meaningful_progress(previous, None, shepherd_author="operator")

        for changes, expected_at in (
            ({}, "2026-08-25T10:00:00Z"),
            ({"body": "[automated] Reviewing this pull request."}, "2026-08-24T10:00:00Z"),
            ({"user": {"login": "operator"}}, "2026-08-24T10:00:00Z"),
        ):
            with self.subTest(changes=changes):
                snapshot = _snapshot()
                _add_pull_request(snapshot, "current-head")
                review = {
                    **prior_review,
                    "id": 51,
                    "user": {"login": "operator", "type": "User"},
                    "submitted_at": "2026-08-25T10:00:00Z",
                    **changes,
                }
                snapshot["evidence"]["pr:23"]["payload"]["currentState"] = build_pull_request_current_state(
                    {"head": {"sha": "current-head"}}, reviews=[review],
                )

                attach_meaningful_progress(snapshot, previous, shepherd_author="operator")

                self.assertEqual(
                    expected_at, snapshot["pullRequests"][0]["meaningfulProgress"]["at"],
                )

    def test_human_comment_preserves_source_event_time_and_evidence(self) -> None:
        snapshot = _snapshot()
        snapshot["evidence"]["issue:21:comment:101"] = _comment()

        attach_meaningful_progress(snapshot, None, shepherd_author="operator")

        self.assertEqual(
            {
                "status": "observed",
                "at": "2026-08-25T10:00:00Z",
                "basis": "human-comment",
                "evidenceIds": ["issue:21:comment:101"],
                "precision": "source-event",
            },
            snapshot["delegationStatus"]["records"][0]["meaningfulProgress"],
        )

    def test_head_change_uses_observation_time_not_updated_at(self) -> None:
        previous = _snapshot()
        previous["collectedAt"] = "2026-08-24T12:00:00Z"
        _add_pull_request(previous, "old-head")
        snapshot = _snapshot()
        _add_pull_request(snapshot, "new-head")

        attach_meaningful_progress(snapshot, previous, shepherd_author="operator")

        self.assertEqual(
            {
                "status": "observed",
                "at": "2026-08-25T12:00:00Z",
                "basis": "pull-request-head-change",
                "evidenceIds": ["pr:23"],
                "precision": "observed-change",
            },
            snapshot["pullRequests"][0]["meaningfulProgress"],
        )

    def test_updated_at_and_check_churn_preserve_last_observed_head_change(self) -> None:
        previous = _snapshot()
        previous["collectedAt"] = "2026-08-24T12:00:00Z"
        _add_pull_request(previous, "old-head")
        changed = _snapshot()
        _add_pull_request(changed, "new-head")
        attach_meaningful_progress(changed, previous, shepherd_author="operator")
        current = copy.deepcopy(changed)
        current["collectedAt"] = "2026-08-26T12:00:00Z"
        current["pullRequests"][0]["updatedAt"] = "2026-08-26T11:00:00Z"
        current["evidence"]["pr:23"]["collectedAt"] = current["collectedAt"]
        current["evidence"]["pr:23"]["payload"]["currentState"]["checks"] = {
            "state": "green",
        }

        attach_meaningful_progress(current, changed, shepherd_author="operator")
        first = copy.deepcopy(current)
        attach_meaningful_progress(current, changed, shepherd_author="operator")

        self.assertEqual(first, current)
        self.assertEqual(
            changed["pullRequests"][0]["meaningfulProgress"],
            current["pullRequests"][0]["meaningfulProgress"],
        )

    def test_linked_delegated_pull_head_change_is_progress_on_owning_issue(self) -> None:
        previous = _snapshot()
        previous["collectedAt"] = "2026-08-24T12:00:00Z"
        previous["delegationStatus"]["records"][0]["pullRequests"] = [{
            "databaseId": 101,
            "number": 23,
            "progressSource": {"headSha": "old-head"},
        }]
        snapshot = copy.deepcopy(previous)
        snapshot["collectedAt"] = "2026-08-25T12:00:00Z"
        snapshot["delegationStatus"]["records"][0]["pullRequests"][0]["progressSource"] = {
            "headSha": "new-head",
        }

        attach_meaningful_progress(snapshot, previous, shepherd_author="operator")

        self.assertEqual(
            {
                "status": "observed",
                "at": "2026-08-25T12:00:00Z",
                "basis": "pull-request-head-change",
                "evidenceIds": ["pr:23"],
                "precision": "observed-change",
            },
            snapshot["delegationStatus"]["records"][0]["meaningfulProgress"],
        )

    def test_recollection_retains_known_progress_without_moving_its_clock(self) -> None:
        previous = _snapshot()
        previous["evidence"]["issue:21:comment:101"] = _comment()
        attach_meaningful_progress(previous, None, shepherd_author="operator")
        snapshot = _snapshot()
        snapshot["collectedAt"] = "2026-08-26T12:00:00Z"

        attach_meaningful_progress(snapshot, previous, shepherd_author="operator")

        self.assertEqual(
            previous["delegationStatus"]["records"][0]["meaningfulProgress"],
            snapshot["delegationStatus"]["records"][0]["meaningfulProgress"],
        )

    def test_human_review_progress_reaches_linked_handoff(self) -> None:
        snapshot = _snapshot()
        _add_pull_request(snapshot, "current-head")
        snapshot["evidence"]["pr:23"]["payload"]["currentState"] = build_pull_request_current_state(
            {"head": {"sha": "current-head"}},
            reviews=[{
                "id": 51,
                "user": {"login": "maintainer", "type": "User"},
                "state": "COMMENTED",
                "body": "Please add coverage for the error path.",
                "submitted_at": "2026-08-25T10:00:00Z",
            }],
        )
        snapshot["delegationStatus"]["records"][0]["pullRequests"] = [{
            "number": 23, "databaseId": 101,
        }]

        attach_meaningful_progress(snapshot, None, shepherd_author="operator")

        expected = {
            "status": "observed",
            "at": "2026-08-25T10:00:00Z",
            "basis": "human-review",
            "evidenceIds": ["pr:23"],
            "precision": "source-event",
        }
        self.assertEqual(expected, snapshot["pullRequests"][0]["meaningfulProgress"])
        self.assertEqual(
            expected,
            snapshot["delegationStatus"]["records"][0]["meaningfulProgress"],
        )

    def test_automation_unknown_identity_and_comment_edits_are_not_new_progress(self) -> None:
        for changes in (
            {"author": "worker[bot]"},
            {"authorType": "Bot"},
            {"authorType": None},
            {"body": "[automated] Checking progress."},
            {"body": " \n[AuToMaTeD] Checking progress."},
            {"shepherdStatus": {"owned": True}},
            {"createdAt": None},
            {"createdAt": "2026-08-26T10:00:00Z"},
            {"createdAt": "not-a-time"},
        ):
            with self.subTest(changes=changes):
                snapshot = _snapshot()
                comment = _comment()
                comment["payload"].update(changes)
                snapshot["evidence"]["issue:21:comment:101"] = comment

                attach_meaningful_progress(snapshot, None, shepherd_author="operator")

                self.assertEqual("unknown", snapshot["delegationStatus"]["records"][0]["meaningfulProgress"]["status"])
        snapshot = _snapshot()
        comment = _comment()
        comment["payload"]["updatedAt"] = "2026-08-25T11:59:00Z"
        snapshot["evidence"]["issue:21:comment:101"] = comment
        attach_meaningful_progress(snapshot, None, shepherd_author="operator")
        self.assertEqual(
            "2026-08-25T10:00:00Z",
            snapshot["delegationStatus"]["records"][0]["meaningfulProgress"]["at"],
        )

    def test_first_observation_does_not_invent_progress_from_updated_at(self) -> None:
        snapshot = _snapshot()
        _add_pull_request(snapshot, "current-head")

        attach_meaningful_progress(snapshot, None, shepherd_author="operator")

        self.assertEqual(
            {
                "status": "unknown", "at": None, "basis": None,
                "evidenceIds": [], "precision": None,
            },
            snapshot["pullRequests"][0]["meaningfulProgress"],
        )

    def test_unverified_or_future_head_evidence_cannot_advance_progress(self) -> None:
        for change in (
            {"availability": "partial"},
            {"collectedAt": "2026-08-26T12:00:00Z"},
            {"collectedAt": None},
        ):
            with self.subTest(change=change):
                previous = _snapshot()
                previous["collectedAt"] = "2026-08-24T12:00:00Z"
                _add_pull_request(previous, "old-head")
                snapshot = _snapshot()
                _add_pull_request(snapshot, "new-head")
                snapshot["evidence"]["pr:23"].update(change)

                attach_meaningful_progress(snapshot, previous, shepherd_author="operator")

                self.assertEqual(
                    "unknown", snapshot["pullRequests"][0]["meaningfulProgress"]["status"],
                )

    def test_latest_event_is_selected_chronologically_with_fractional_seconds(self) -> None:
        snapshot = _snapshot()
        snapshot["evidence"]["issue:21:comment:101"] = _comment()
        later = _comment()
        later["payload"]["createdAt"] = "2026-08-25T10:00:00.500Z"
        snapshot["evidence"]["issue:21:comment:102"] = later

        attach_meaningful_progress(snapshot, None, shepherd_author="operator")

        progress = snapshot["delegationStatus"]["records"][0]["meaningfulProgress"]
        self.assertEqual(["issue:21:comment:102"], progress["evidenceIds"])


def _snapshot() -> dict:
    return {
        "repository": "owner/repo",
        "collectedAt": "2026-08-25T12:00:00Z",
        "pullRequests": [],
        "evidence": {},
        "delegationStatus": {
            "status": "complete",
            "records": [
                {
                    "repository": "owner/repo",
                    "issueNumber": 21,
                    "actionId": "assignment:21",
                    "pullRequests": [],
                }
            ],
        },
    }


def _comment() -> dict:
    return {
        "kind": "issue-comment",
        "availability": "available",
        "payload": {
            "author": "maintainer",
            "authorType": "User",
            "body": "The prerequisite is fixed; I am checking the remaining failure.",
            "createdAt": "2026-08-25T10:00:00Z",
            "updatedAt": "2026-08-25T10:00:00Z",
        },
    }


def _add_pull_request(snapshot: dict, head: str) -> None:
    snapshot["pullRequests"] = [{
        "number": 23,
        "updatedAt": "2026-08-25T11:00:00Z",
    }]
    snapshot["evidence"]["pr:23"] = {
        "kind": "pull-request",
        "availability": "available",
        "collectedAt": snapshot["collectedAt"],
        "payload": {"currentState": {"headSha": head}},
    }


if __name__ == "__main__":
    unittest.main()
