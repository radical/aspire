from __future__ import annotations

import copy
import unittest

from ci_shepherd.models import ValidationError
from ci_shepherd.pull_requests import (
    CHECKS_GREEN,
    CHECKS_PENDING,
    CHECKS_RED,
    CHECKS_UNKNOWN,
    REVIEW_APPROVED,
    REVIEW_CHANGES_REQUESTED,
    REVIEW_REQUIRED,
    build_pull_request_comment_proposals,
    build_pull_request_current_state,
    build_pull_request_handoff,
    merge_pull_request_judgments,
    pull_request_requires_human_decision,
    render_pull_request_section,
    validate_pull_request_judgments,
)
from ci_shepherd.actor import validate_action_proposals


SHEPHERD = "ci-shepherd-bot"


def green_state() -> dict[str, object]:
    return build_pull_request_current_state(
        {"head": {"sha": "abc"}, "mergeable": True, "mergeable_state": "clean"},
        check_runs=[
            {"name": "build", "status": "completed", "conclusion": "success"}
        ],
        reviews=[],
    )


def conflicted_state() -> dict[str, object]:
    return build_pull_request_current_state(
        {"head": {"sha": "abc"}, "mergeable": False, "mergeable_state": "dirty"},
        check_runs=[{"name": "build", "status": "completed", "conclusion": "success"}],
        reviews=[],
    )


def red_state() -> dict[str, object]:
    return build_pull_request_current_state(
        {"head": {"sha": "abc"}, "mergeable": True, "mergeable_state": "clean"},
        check_runs=[
            {
                "name": "build",
                "status": "completed",
                "conclusion": "failure",
                "html_url": "https://github.com/owner/repo/runs/1",
            }
        ],
        reviews=[],
    )


def snapshot(
    *,
    updated_at: str = "2026-08-27T10:00:00Z",
    current_state: object | None = None,
    availability: str = "available",
    assignees: list[str] | None = None,
    status_comments: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "number": 23,
        "state": "open",
        "head": {"sha": "abc", "ref": "automation/fix"},
        "base": {"sha": "def", "ref": "main"},
        "files": [{"path": "eng/example.yml", "status": "modified"}],
    }
    if current_state is not None:
        payload["currentState"] = current_state
    if status_comments is not None:
        payload["shepherdStatusComments"] = status_comments
    return {
        "schemaVersion": 1,
        "repository": "owner/repo",
        "collectedAt": "2026-08-27T12:00:00Z",
        "openIssues": [],
        "openPullRequests": [23],
        "pullRequests": [
            {
                "number": 23,
                "state": "open",
                "title": "Update generated CI configuration",
                "url": "https://github.com/owner/repo/pull/23",
                "updatedAt": updated_at,
                "labels": ["automation-broken"],
                "author": "github-actions[bot]",
                "assignees": list(assignees or []),
                "selectionReasons": ["label:automation-broken"],
            }
        ],
        "evidence": {
            "pr:23": {
                "kind": "pull-request",
                "url": "https://github.com/owner/repo/pull/23",
                "collectedAt": "2026-08-27T12:00:00Z",
                "availability": availability,
                "payload": payload,
            }
        },
        "collectionErrors": [],
    }


def judgment_document(
    handoff: dict[str, object],
    *judgments: dict[str, object],
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "snapshotId": handoff["snapshotId"],
        "pullRequests": list(judgments),
    }


class CurrentStateTests(unittest.TestCase):
    def test_all_completed_successful_check_runs_are_green_and_complete(self) -> None:
        state = build_pull_request_current_state(
            {"head": {"sha": "abc"}, "mergeable": True, "mergeable_state": "clean"},
            check_runs=[
                {"name": "build", "status": "completed", "conclusion": "success"},
                {"name": "lint", "status": "completed", "conclusion": "skipped"},
            ],
            reviews=[],
        )

        self.assertEqual(CHECKS_GREEN, state["checks"]["state"])
        self.assertTrue(state["checks"]["complete"])
        self.assertTrue(state["complete"])
        self.assertEqual([], state["incompleteReasons"])

    def test_failing_check_run_is_red_and_names_the_failing_check(self) -> None:
        state = build_pull_request_current_state(
            {"head": {"sha": "abc"}},
            check_runs=[
                {"name": "build", "status": "completed", "conclusion": "success"},
                {
                    "name": "tests",
                    "status": "completed",
                    "conclusion": "failure",
                    "html_url": "https://github.com/owner/repo/runs/9",
                },
            ],
            reviews=[],
        )

        self.assertEqual(CHECKS_RED, state["checks"]["state"])
        self.assertTrue(state["complete"])
        self.assertEqual(
            [
                {
                    "name": "tests",
                    "conclusion": "failure",
                    "url": "https://github.com/owner/repo/runs/9",
                }
            ],
            state["checks"]["failing"],
        )

    def test_in_progress_check_run_is_pending_and_incomplete(self) -> None:
        state = build_pull_request_current_state(
            {"head": {"sha": "abc"}},
            check_runs=[
                {"name": "build", "status": "completed", "conclusion": "success"},
                {"name": "tests", "status": "in_progress", "conclusion": None},
            ],
            reviews=[],
        )

        self.assertEqual(CHECKS_PENDING, state["checks"]["state"])
        self.assertFalse(state["checks"]["complete"])
        self.assertFalse(state["complete"])
        self.assertEqual(
            ["current head-commit check conclusion is pending"],
            state["incompleteReasons"],
        )

    def test_cancelled_check_run_is_not_treated_as_success(self) -> None:
        state = build_pull_request_current_state(
            {"head": {"sha": "abc"}},
            check_runs=[
                {"name": "build", "status": "completed", "conclusion": "cancelled"}
            ],
            reviews=[],
        )

        self.assertEqual(CHECKS_UNKNOWN, state["checks"]["state"])
        self.assertFalse(state["complete"])

    def test_failed_check_fetch_is_unknown_rather_than_empty_green(self) -> None:
        state = build_pull_request_current_state(
            {"head": {"sha": "abc"}},
            check_runs=None,
            combined_status=None,
            reviews=[],
        )

        self.assertEqual("none", state["checks"]["source"])
        self.assertEqual(CHECKS_UNKNOWN, state["checks"]["state"])
        self.assertFalse(state["complete"])

    def test_failed_check_fetch_cannot_be_laundered_by_combined_status(self) -> None:
        state = build_pull_request_current_state(
            {"head": {"sha": "abc"}},
            check_runs=None,
            combined_status={
                "sha": "abc",
                "state": "success",
                "statuses": [{"context": "legacy/ci", "state": "success"}],
            },
            reviews=[],
        )

        self.assertEqual("none", state["checks"]["source"])
        self.assertEqual(CHECKS_UNKNOWN, state["checks"]["state"])
        self.assertFalse(state["complete"])

    def test_empty_check_runs_fall_back_to_combined_status(self) -> None:
        state = build_pull_request_current_state(
            {"head": {"sha": "abc"}},
            check_runs=[],
            combined_status={
                "sha": "abc",
                "state": "failure",
                "statuses": [
                    {
                        "context": "legacy/ci",
                        "state": "failure",
                        "target_url": "https://example.test/1",
                    }
                ],
            },
            reviews=[],
        )

        self.assertEqual("combined-status", state["checks"]["source"])
        self.assertEqual(CHECKS_RED, state["checks"]["state"])
        self.assertTrue(state["complete"])

    def test_combined_status_success_without_contexts_is_unknown(self) -> None:
        state = build_pull_request_current_state(
            {"head": {"sha": "abc"}},
            check_runs=[],
            combined_status={"sha": "abc", "state": "success", "statuses": []},
            reviews=[],
        )

        self.assertEqual(CHECKS_UNKNOWN, state["checks"]["state"])
        self.assertFalse(state["complete"])

    def test_check_run_for_a_stale_head_commit_is_ignored(self) -> None:
        state = build_pull_request_current_state(
            {"head": {"sha": "abc"}},
            check_runs=[
                {
                    "name": "build",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": "older",
                }
            ],
            reviews=[],
        )

        self.assertEqual(0, state["checks"]["total"])
        self.assertEqual(CHECKS_UNKNOWN, state["checks"]["state"])

    def test_missing_head_sha_leaves_state_incomplete(self) -> None:
        state = build_pull_request_current_state({}, check_runs=[], reviews=[])

        self.assertIsNone(state["headSha"])
        self.assertFalse(state["complete"])
        self.assertIn(
            "pull request head commit is unknown", state["incompleteReasons"]
        )

    def test_latest_review_per_reviewer_decides_the_review_state(self) -> None:
        state = build_pull_request_current_state(
            {"head": {"sha": "abc"}},
            check_runs=[
                {"name": "build", "status": "completed", "conclusion": "success"}
            ],
            reviews=[
                {"user": {"login": "alice"}, "state": "CHANGES_REQUESTED"},
                {"user": {"login": "alice"}, "state": "APPROVED"},
                {"user": {"login": "bob"}, "state": "COMMENTED"},
            ],
        )

        self.assertEqual(REVIEW_APPROVED, state["review"]["decision"])
        self.assertEqual(
            [{"login": "alice", "state": REVIEW_APPROVED}],
            state["review"]["reviewers"],
        )

    def test_changes_requested_review_wins_over_approval(self) -> None:
        state = build_pull_request_current_state(
            {"head": {"sha": "abc"}},
            check_runs=[
                {"name": "build", "status": "completed", "conclusion": "success"}
            ],
            reviews=[
                {"user": {"login": "alice"}, "state": "APPROVED"},
                {"user": {"login": "bob"}, "state": "CHANGES_REQUESTED"},
            ],
        )

        self.assertEqual(REVIEW_CHANGES_REQUESTED, state["review"]["decision"])

    def test_failed_review_fetch_leaves_state_incomplete(self) -> None:
        state = build_pull_request_current_state(
            {"head": {"sha": "abc"}},
            check_runs=[
                {"name": "build", "status": "completed", "conclusion": "success"}
            ],
            reviews=None,
        )

        self.assertEqual(REVIEW_REQUIRED, state["review"]["decision"])
        self.assertFalse(state["complete"])
        self.assertIn("current review state is unavailable", state["incompleteReasons"])

    def test_check_lists_are_bounded(self) -> None:
        state = build_pull_request_current_state(
            {"head": {"sha": "abc"}},
            check_runs=[
                {
                    "name": f"check-{index:02d}",
                    "status": "completed",
                    "conclusion": "failure",
                }
                for index in range(25)
            ],
            reviews=[],
            limit=3,
        )

        self.assertEqual(25, state["checks"]["total"])
        self.assertEqual(3, len(state["checks"]["failing"]))
        self.assertTrue(state["checks"]["truncated"])
        self.assertEqual(
            ["check-00", "check-01", "check-02"],
            [entry["name"] for entry in state["checks"]["failing"]],
        )


class HandoffTests(unittest.TestCase):
    def test_selects_only_new_or_changed_pull_requests(self) -> None:
        previous = snapshot(current_state=green_state())

        unchanged = build_pull_request_handoff(
            snapshot(current_state=green_state()),
            previous_snapshot=previous,
        )
        changed = build_pull_request_handoff(
            snapshot(updated_at="2026-08-27T11:00:00Z", current_state=green_state()),
            previous_snapshot=previous,
        )

        self.assertEqual([], unchanged["tasks"])
        self.assertEqual(
            [{"number": 23, "reason": "unchanged-stable"}], unchanged["excluded"]
        )
        self.assertEqual("changed", changed["tasks"][0]["changeClass"])
        self.assertEqual(
            {"kind": "pull-request", "number": 23}, changed["tasks"][0]["target"]
        )

    def test_selects_check_change_when_pull_request_timestamp_is_unchanged(self) -> None:
        pending = build_pull_request_current_state(
            {"head": {"sha": "abc"}},
            check_runs=[
                {"name": "build", "status": "in_progress", "conclusion": None}
            ],
            reviews=[],
        )
        previous = snapshot(current_state=pending)

        changed = build_pull_request_handoff(
            snapshot(current_state=red_state()),
            previous_snapshot=previous,
        )

        self.assertEqual("changed", changed["tasks"][0]["changeClass"])
        self.assertEqual(
            CHECKS_RED,
            changed["tasks"][0]["currentState"]["checks"]["state"],
        )

    def test_green_checks_default_to_no_action_with_conclusive_dispositions(self) -> None:
        handoff = build_pull_request_handoff(snapshot(current_state=green_state()))
        task = handoff["tasks"][0]

        self.assertEqual("complete", task["evidenceStatus"])
        self.assertEqual(
            ["investigate", "no-action", "watch"], task["allowedDispositions"]
        )
        self.assertEqual("no-action", task["defaultJudgment"]["disposition"])

    def test_red_checks_default_to_investigate(self) -> None:
        handoff = build_pull_request_handoff(snapshot(current_state=red_state()))
        task = handoff["tasks"][0]

        self.assertEqual("investigate", task["defaultJudgment"]["disposition"])
        self.assertEqual(CHECKS_RED, task["currentState"]["checks"]["state"])

    def test_incomplete_checks_restrict_the_task_to_watch(self) -> None:
        pending = build_pull_request_current_state(
            {"head": {"sha": "abc"}},
            check_runs=[{"name": "build", "status": "queued", "conclusion": None}],
            reviews=[],
        )

        handoff = build_pull_request_handoff(snapshot(current_state=pending))
        task = handoff["tasks"][0]

        self.assertEqual("incomplete", task["evidenceStatus"])
        self.assertEqual(["watch"], task["allowedDispositions"])
        self.assertEqual("watch", task["defaultJudgment"]["disposition"])
        self.assertIn("reassessWhen", task["defaultJudgment"])

    def test_missing_evidence_record_produces_a_conservative_unknown_state(self) -> None:
        document = snapshot(current_state=green_state())
        del document["evidence"]["pr:23"]

        task = build_pull_request_handoff(document)["tasks"][0]

        self.assertEqual("incomplete", task["evidenceStatus"])
        self.assertEqual(["watch"], task["allowedDispositions"])
        self.assertEqual(
            ["no pull request evidence was collected"],
            task["currentState"]["incompleteReasons"],
        )

    def test_partial_evidence_record_is_not_trusted_as_current(self) -> None:
        document = snapshot(current_state=green_state(), availability="partial")

        task = build_pull_request_handoff(document)["tasks"][0]

        self.assertEqual("incomplete", task["evidenceStatus"])
        self.assertEqual(["watch"], task["allowedDispositions"])

    def test_copilot_assigned_pull_requests_are_excluded_from_the_handoff(self) -> None:
        handoff = build_pull_request_handoff(
            snapshot(current_state=green_state(), assignees=["Copilot"])
        )

        self.assertEqual([], handoff["tasks"])
        self.assertEqual(
            [{"number": 23, "reason": "assigned-to-copilot"}], handoff["excluded"]
        )

    def test_changes_requested_reports_a_human_decision_and_allows_ping_human(self) -> None:
        state = build_pull_request_current_state(
            {"head": {"sha": "abc"}, "mergeable": True, "mergeable_state": "clean"},
            check_runs=[
                {"name": "build", "status": "completed", "conclusion": "success"}
            ],
            reviews=[{"user": {"login": "bob"}, "state": "CHANGES_REQUESTED"}],
        )

        task = build_pull_request_handoff(snapshot(current_state=state))["tasks"][0]

        self.assertIn("ping-human", task["allowedDispositions"])
        self.assertTrue(pull_request_requires_human_decision(task))
        self.assertEqual("watch", task["defaultJudgment"]["disposition"])

    def test_conflicting_branch_reports_a_human_decision(self) -> None:
        state = build_pull_request_current_state(
            {"head": {"sha": "abc"}, "mergeable": False, "mergeable_state": "dirty"},
            check_runs=[
                {"name": "build", "status": "completed", "conclusion": "success"}
            ],
            reviews=[],
        )

        task = build_pull_request_handoff(snapshot(current_state=state))["tasks"][0]

        self.assertIn("ping-human", task["allowedDispositions"])
        self.assertIn("conflicts", str(task["humanDecision"]))

    def test_stop_conditions_forbid_closure_and_copilot_targets(self) -> None:
        task = build_pull_request_handoff(
            snapshot(current_state=green_state())
        )["tasks"][0]
        stop_conditions = " ".join(task["stopConditions"])

        self.assertIn("assigned to Copilot", stop_conditions)
        self.assertIn("Never propose closing", stop_conditions)
        self.assertIn(
            "current check conclusions", " ".join(task["questions"])
        )


class JudgmentContractTests(unittest.TestCase):
    def handoff(self, **kwargs: object) -> dict[str, object]:
        return build_pull_request_handoff(snapshot(**kwargs))

    def test_accepts_a_valid_investigate_judgment(self) -> None:
        handoff = self.handoff(current_state=red_state())
        document = judgment_document(
            handoff,
            {
                "pullRequestNumber": 23,
                "disposition": "investigate",
                "summary": "The generated configuration no longer compiles.",
                "evidenceIds": ["pr:23"],
            },
        )

        validated = validate_pull_request_judgments(handoff, document)

        self.assertEqual(1, len(validated["pullRequests"]))
        self.assertEqual("investigate", validated["pullRequests"][0]["disposition"])

    def test_rejects_a_closure_disposition(self) -> None:
        handoff = self.handoff(current_state=green_state())
        document = judgment_document(
            handoff,
            {
                "pullRequestNumber": 23,
                "disposition": "review-close",
                "summary": "Close the stale pull request.",
                "evidenceIds": ["pr:23"],
            },
        )

        with self.assertRaises(ValidationError) as error:
            validate_pull_request_judgments(handoff, document)
        self.assertIn("unsupported", str(error.exception))

    def test_rejects_a_judgment_for_a_pull_request_that_was_not_handed_off(self) -> None:
        handoff = self.handoff(current_state=green_state())
        document = judgment_document(
            handoff,
            {
                "pullRequestNumber": 99,
                "disposition": "no-action",
                "summary": "Nothing to do.",
                "evidenceIds": ["pr:99"],
            },
        )

        with self.assertRaises(ValidationError) as error:
            validate_pull_request_judgments(handoff, document)
        self.assertIn("was not handed off", str(error.exception))

    def test_rejects_a_conclusive_disposition_without_complete_evidence(self) -> None:
        handoff = self.handoff()
        document = judgment_document(
            handoff,
            {
                "pullRequestNumber": 23,
                "disposition": "no-action",
                "summary": "Looks fine.",
                "evidenceIds": ["pr:23"],
            },
        )

        with self.assertRaises(ValidationError) as error:
            validate_pull_request_judgments(handoff, document)
        self.assertIn("allowed dispositions are ['watch']", str(error.exception))

    def test_rejects_a_conclusive_disposition_when_the_allow_list_was_tampered_with(
        self,
    ) -> None:
        handoff = self.handoff()
        handoff["tasks"][0]["allowedDispositions"] = ["no-action", "watch"]
        document = judgment_document(
            handoff,
            {
                "pullRequestNumber": 23,
                "disposition": "no-action",
                "summary": "Looks fine.",
                "evidenceIds": ["pr:23"],
            },
        )

        with self.assertRaises(ValidationError) as error:
            validate_pull_request_judgments(handoff, document)
        self.assertIn("complete current check and review evidence", str(error.exception))

    def test_rejects_watch_without_reassess_when(self) -> None:
        handoff = self.handoff()
        document = judgment_document(
            handoff,
            {
                "pullRequestNumber": 23,
                "disposition": "watch",
                "summary": "Waiting for checks.",
                "evidenceIds": ["pr:23"],
            },
        )

        with self.assertRaises(ValidationError) as error:
            validate_pull_request_judgments(handoff, document)
        self.assertIn("reassessWhen", str(error.exception))

    def test_rejects_ping_human_without_a_reported_human_decision(self) -> None:
        handoff = self.handoff(current_state=green_state())
        handoff["tasks"][0]["allowedDispositions"] = ["ping-human"]
        document = judgment_document(
            handoff,
            {
                "pullRequestNumber": 23,
                "disposition": "ping-human",
                "summary": "Someone should look.",
                "evidenceIds": ["pr:23"],
                "humanEscalation": {
                    "context": "The pull request is old.",
                    "whyHuman": "Automation cannot decide.",
                    "question": "Should this merge?",
                    "suggestedNextSteps": ["Decide."],
                    "routingHint": "area-infra",
                },
            },
        )

        with self.assertRaises(ValidationError) as error:
            validate_pull_request_judgments(handoff, document)
        self.assertIn("requires a reported human decision", str(error.exception))

    def test_rejects_ping_human_without_a_complete_escalation(self) -> None:
        handoff = self.handoff(current_state=conflicted_state())
        document = judgment_document(
            handoff,
            {
                "pullRequestNumber": 23,
                "disposition": "ping-human",
                "summary": "The branch conflicts.",
                "evidenceIds": ["pr:23"],
                "humanEscalation": {
                    "context": "The branch conflicts with main.",
                    "whyHuman": "Only the author can resolve it.",
                    "question": "Should this be rebased or closed by its author?",
                    "suggestedNextSteps": [],
                    "routingHint": "area-infra",
                },
            },
        )

        with self.assertRaises(ValidationError) as error:
            validate_pull_request_judgments(handoff, document)
        self.assertIn("suggestedNextSteps", str(error.exception))

    def test_rejects_human_escalation_on_a_non_escalating_disposition(self) -> None:
        handoff = self.handoff(current_state=green_state())
        document = judgment_document(
            handoff,
            {
                "pullRequestNumber": 23,
                "disposition": "no-action",
                "summary": "Green.",
                "evidenceIds": ["pr:23"],
                "humanEscalation": {
                    "context": "c",
                    "whyHuman": "w",
                    "question": "q",
                    "suggestedNextSteps": ["s"],
                    "routingHint": "r",
                },
            },
        )

        with self.assertRaises(ValidationError) as error:
            validate_pull_request_judgments(handoff, document)
        self.assertIn("only valid for ping-human", str(error.exception))

    def test_rejects_evidence_outside_the_handed_off_bundle(self) -> None:
        handoff = self.handoff(current_state=green_state())
        document = judgment_document(
            handoff,
            {
                "pullRequestNumber": 23,
                "disposition": "no-action",
                "summary": "Green.",
                "evidenceIds": ["pr:23", "issue:5"],
            },
        )

        with self.assertRaises(ValidationError) as error:
            validate_pull_request_judgments(handoff, document)
        self.assertIn("outside its handed-off evidence", str(error.exception))

    def test_rejects_duplicate_and_unknown_fields_and_mismatched_snapshots(self) -> None:
        handoff = self.handoff(current_state=green_state())
        duplicate = judgment_document(
            handoff,
            {
                "pullRequestNumber": 23,
                "disposition": "no-action",
                "summary": "Green.",
                "evidenceIds": ["pr:23"],
            },
            {
                "pullRequestNumber": 23,
                "disposition": "investigate",
                "summary": "Red.",
                "evidenceIds": ["pr:23"],
            },
        )
        with self.assertRaises(ValidationError) as duplicate_error:
            validate_pull_request_judgments(handoff, duplicate)
        self.assertIn("Duplicate judgment", str(duplicate_error.exception))

        extra_field = judgment_document(
            handoff,
            {
                "pullRequestNumber": 23,
                "disposition": "no-action",
                "summary": "Green.",
                "evidenceIds": ["pr:23"],
                "closeReason": "not_planned",
            },
        )
        with self.assertRaises(ValidationError) as field_error:
            validate_pull_request_judgments(handoff, extra_field)
        self.assertIn("unsupported fields", str(field_error.exception))

        mismatched = judgment_document(handoff)
        mismatched["snapshotId"] = "snapshot:owner/repo:1999-01-01T00:00:00Z"
        with self.assertRaises(ValidationError) as snapshot_error:
            validate_pull_request_judgments(handoff, mismatched)
        self.assertIn("snapshotId must match", str(snapshot_error.exception))

    def test_rejects_a_malformed_judgment_document(self) -> None:
        handoff = self.handoff(current_state=green_state())

        with self.assertRaises(ValidationError):
            validate_pull_request_judgments(handoff, {"schemaVersion": 2})
        with self.assertRaises(ValidationError):
            validate_pull_request_judgments(handoff, ["not-a-document"])
        with self.assertRaises(ValidationError):
            validate_pull_request_judgments(
                handoff,
                {
                    "schemaVersion": 1,
                    "snapshotId": handoff["snapshotId"],
                    "pullRequests": "not-a-list",
                },
            )
        with self.assertRaises(ValidationError):
            validate_pull_request_judgments(
                handoff,
                judgment_document(
                    handoff,
                    {
                        "pullRequestNumber": 23,
                        "disposition": "no-action",
                        "summary": "   ",
                        "evidenceIds": ["pr:23"],
                    },
                ),
            )

    def test_sparse_judgments_keep_deterministic_defaults_for_silent_cases(self) -> None:
        document = snapshot(current_state=green_state())
        document["openPullRequests"].append(24)
        document["pullRequests"].append(
            {
                **copy.deepcopy(document["pullRequests"][0]),
                "number": 24,
                "url": "https://github.com/owner/repo/pull/24",
            }
        )
        document["evidence"]["pr:24"] = copy.deepcopy(document["evidence"]["pr:23"])
        document["evidence"]["pr:24"]["payload"]["number"] = 24
        document["evidence"]["pr:24"]["payload"]["currentState"] = red_state()
        handoff = build_pull_request_handoff(document)

        merged = merge_pull_request_judgments(
            handoff,
            judgment_document(
                handoff,
                {
                    "pullRequestNumber": 24,
                    "disposition": "watch",
                    "summary": "The failure is already tracked elsewhere.",
                    "evidenceIds": ["pr:24"],
                    "reassessWhen": "After the next push to the branch.",
                },
            ),
        )

        self.assertEqual(
            [(23, "no-action"), (24, "watch")],
            [
                (entry["pullRequestNumber"], entry["disposition"])
                for entry in merged["pullRequests"]
            ],
        )

    def test_unchanged_pull_requests_need_no_judgment_at_all(self) -> None:
        previous = snapshot(current_state=green_state())
        handoff = build_pull_request_handoff(
            snapshot(current_state=green_state()), previous_snapshot=previous
        )

        merged = merge_pull_request_judgments(handoff, judgment_document(handoff))

        self.assertEqual([], merged["pullRequests"])


class MarkdownTests(unittest.TestCase):
    def test_renders_a_deterministic_row_per_disposition(self) -> None:
        handoff = build_pull_request_handoff(snapshot(current_state=red_state()))

        markdown = render_pull_request_section(
            handoff, judgment_document(handoff)
        )

        self.assertIn("## Pull requests", markdown)
        self.assertIn("### Investigate", markdown)
        self.assertIn(
            "| [#23](https://github.com/owner/repo/pull/23) Update generated CI "
            "configuration | red (1 failing) | review-required | `pr:23` |",
            markdown,
        )
        self.assertEqual(
            markdown,
            render_pull_request_section(handoff, judgment_document(handoff)),
        )

    def test_omits_unchanged_pull_requests_from_the_section(self) -> None:
        previous = snapshot(current_state=green_state())
        handoff = build_pull_request_handoff(
            snapshot(current_state=green_state()), previous_snapshot=previous
        )

        markdown = render_pull_request_section(handoff, judgment_document(handoff))

        self.assertIn("No new or changed pull requests required review.", markdown)
        self.assertIn(
            "**Handoff:** 0 selected; 1 excluded "
            "(`unchanged-stable`: 1).",
            markdown,
        )
        self.assertNotIn("#23", markdown)


class CommentProposalTests(unittest.TestCase):
    def ping_human_judgment(self, handoff: dict[str, object]) -> dict[str, object]:
        return judgment_document(
            handoff,
            {
                "pullRequestNumber": 23,
                "disposition": "ping-human",
                "summary": "The branch conflicts with main.",
                "evidenceIds": ["pr:23"],
                "humanEscalation": {
                    "context": "This automation pull request no longer merges cleanly.",
                    "whyHuman": "Only the author can choose how to resolve the conflict.",
                    "question": "Should this branch be rebased or superseded?",
                    "suggestedNextSteps": ["Rebase onto main.", "Or close and regenerate."],
                    "routingHint": "area-infra",
                },
            },
        )

    def test_watch_is_report_only(self) -> None:
        document = snapshot()
        handoff = build_pull_request_handoff(document)

        proposals = build_pull_request_comment_proposals(
            document, handoff, judgment_document(handoff), SHEPHERD
        )

        self.assertEqual([], proposals["proposals"])

    def test_proposals_pass_the_shared_actor_contract(self) -> None:
        document = snapshot(current_state=conflicted_state())
        handoff = build_pull_request_handoff(document)
        proposals = build_pull_request_comment_proposals(
            document, handoff, self.ping_human_judgment(handoff), SHEPHERD
        )

        validate_action_proposals(
            {
                "schemaVersion": 1,
                "repository": "owner/repo",
                "proposals": proposals["proposals"],
            }
        )

    def test_never_proposes_a_closure_operation(self) -> None:
        document = snapshot(current_state=conflicted_state())
        handoff = build_pull_request_handoff(document)

        proposals = build_pull_request_comment_proposals(
            document, handoff, self.ping_human_judgment(handoff), SHEPHERD
        )

        self.assertEqual(
            {"create-comment"},
            {proposal["operation"] for proposal in proposals["proposals"]},
        )
        self.assertNotIn(
            "closeReason",
            {key for proposal in proposals["proposals"] for key in proposal},
        )

    def test_investigate_and_no_action_produce_no_comment(self) -> None:
        for state in (green_state(), red_state()):
            with self.subTest(state=state["checks"]["state"]):
                document = snapshot(current_state=state)
                handoff = build_pull_request_handoff(document)

                proposals = build_pull_request_comment_proposals(
                    document, handoff, judgment_document(handoff), SHEPHERD
                )

                self.assertEqual([], proposals["proposals"])

    def test_non_escalated_state_retires_existing_human_request(self) -> None:
        document = snapshot(
            current_state=green_state(),
            status_comments=[
                {
                    "id": 5001,
                    "url": "https://github.com/owner/repo/pull/23#issuecomment-5001",
                    "body": "[automated] Human decision needed.",
                    "idempotencyKey": "pull-request:23:status",
                }
            ],
        )
        handoff = build_pull_request_handoff(document)

        proposals = build_pull_request_comment_proposals(
            document,
            handoff,
            judgment_document(handoff),
            SHEPHERD,
        )

        self.assertEqual(1, len(proposals["proposals"]))
        proposal = proposals["proposals"][0]
        self.assertEqual("edit-comment", proposal["operation"])
        self.assertEqual(5001, proposal["commentId"])
        self.assertIn("no longer needs the prior human decision", proposal["body"])
        self.assertIn("pull-request:23:status", proposal["body"])

        next_state = green_state()
        next_state["headSha"] = "next"
        next_document = snapshot(
            updated_at="2026-08-28T10:00:00Z",
            current_state=next_state,
            status_comments=[
                {
                    "id": 5001,
                    "url": "https://github.com/owner/repo/pull/23#issuecomment-5001",
                    "body": proposal["body"],
                    "idempotencyKey": "pull-request:23:status",
                }
            ],
        )
        next_handoff = build_pull_request_handoff(next_document)

        repeated = build_pull_request_comment_proposals(
            next_document,
            next_handoff,
            judgment_document(next_handoff),
            SHEPHERD,
        )

        self.assertEqual([], repeated["proposals"])

    def test_identical_canonical_comment_suppresses_a_new_proposal(self) -> None:
        document = snapshot(current_state=conflicted_state())
        handoff = build_pull_request_handoff(document)
        first = build_pull_request_comment_proposals(
            document, handoff, self.ping_human_judgment(handoff), SHEPHERD
        )
        body = first["proposals"][0]["body"]

        posted = snapshot(
            current_state=conflicted_state(),
            status_comments=[
                {
                    "id": 5001,
                    "url": "https://github.com/owner/repo/pull/23#issuecomment-5001",
                    "body": body,
                    "idempotencyKey": "pull-request:23:status",
                }
            ]
        )
        posted_handoff = build_pull_request_handoff(posted)

        second = build_pull_request_comment_proposals(
            posted,
            posted_handoff,
            self.ping_human_judgment(posted_handoff),
            SHEPHERD,
        )

        self.assertEqual([], second["proposals"])
        self.assertEqual([23], second["unchangedPullRequestNumbers"])

    def test_changed_canonical_comment_becomes_an_edit_proposal(self) -> None:
        posted = snapshot(
            current_state=conflicted_state(),
            status_comments=[
                {
                    "id": 5001,
                    "url": "https://github.com/owner/repo/pull/23#issuecomment-5001",
                    "body": "[automated] stale text\n<!-- ci-shepherd:role=status -->",
                    "idempotencyKey": "pull-request:23:status",
                }
            ]
        )
        handoff = build_pull_request_handoff(posted)

        proposals = build_pull_request_comment_proposals(
            posted, handoff, self.ping_human_judgment(handoff), SHEPHERD
        )

        self.assertEqual("edit-comment", proposals["proposals"][0]["operation"])
        self.assertEqual(5001, proposals["proposals"][0]["commentId"])

    def test_multiple_owned_status_comments_are_rejected(self) -> None:
        posted = snapshot(
            current_state=conflicted_state(),
            status_comments=[
                {
                    "id": 5001,
                    "url": "https://example.test/1",
                    "body": "[automated] one",
                    "idempotencyKey": "pull-request:23:status",
                },
                {
                    "id": 5002,
                    "url": "https://example.test/2",
                    "body": "[automated] two",
                    "idempotencyKey": "pull-request:23:status",
                },
            ]
        )
        handoff = build_pull_request_handoff(posted)

        with self.assertRaises(ValidationError) as error:
            build_pull_request_comment_proposals(
                posted, handoff, self.ping_human_judgment(handoff), SHEPHERD
            )
        self.assertIn("multiple owned status comments", str(error.exception))

    def test_copilot_assignment_after_handoff_suppresses_the_proposal(self) -> None:
        document = snapshot(current_state=conflicted_state())
        handoff = build_pull_request_handoff(document)
        # The pull request was free when the handoff was built; Copilot picked
        # it up before the proposal was rendered.
        raced = snapshot(
            current_state=conflicted_state(),
            assignees=["copilot-swe-agent[bot]"],
        )

        proposals = build_pull_request_comment_proposals(
            raced, handoff, self.ping_human_judgment(handoff), SHEPHERD
        )

        self.assertEqual([], proposals["proposals"])
        self.assertEqual(
            [{"number": 23, "reason": "assigned-to-copilot"}],
            proposals["suppressedPullRequests"],
        )

    def test_ping_human_body_asks_the_decision_and_names_next_steps(self) -> None:
        document = snapshot(current_state=conflicted_state())
        handoff = build_pull_request_handoff(document)

        proposals = build_pull_request_comment_proposals(
            document, handoff, self.ping_human_judgment(handoff), SHEPHERD
        )
        body = proposals["proposals"][0]["body"]

        self.assertTrue(body.startswith("[automated] "))
        self.assertIn("**Decision needed:** Should this branch be rebased", body)
        self.assertIn("- Rebase onto main.", body)
        self.assertIn("**Routing hint:** `area-infra`", body)
        self.assertIn("No merge, closure, rerun, or other change", body)

    def test_rejects_an_empty_shepherd_author(self) -> None:
        document = snapshot()
        handoff = build_pull_request_handoff(document)

        with self.assertRaises(ValidationError):
            build_pull_request_comment_proposals(
                document, handoff, judgment_document(handoff), "   "
            )


if __name__ == "__main__":
    unittest.main()
