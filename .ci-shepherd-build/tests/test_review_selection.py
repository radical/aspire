from __future__ import annotations

import unittest

from ci_shepherd.models import EVIDENCE_REQUEST_DECISION_GATES, ValidationError
from ci_shepherd.poc import build_compact_poc_input, validate_poc_judgments
from ci_shepherd.review_selection import (
    build_review_selection,
    merge_selected_poc_judgments,
    selected_issue_numbers,
)

from test_poc import (
    _compact_issue,
    _compact_prepared,
    _human_decision_compact_issue,
)


def _compact(issues: list[dict[str, object]]) -> dict[str, object]:
    return build_compact_poc_input(_compact_prepared(issues))


def _stable_issue(issue_number: int) -> dict[str, object]:
    """A single-occurrence flaky test: watch, reviewRequired False."""
    return _compact_issue(
        issue_number,
        candidate_state="active",
        candidate_action="watch",
        resolution_evidence={},
    )


def _investigate_issue(issue_number: int) -> dict[str, object]:
    """A build breakage: the deterministic default is investigate."""
    return _compact_issue(
        issue_number,
        title="[Main CI Failure] Project did not compile",
        tier2_test_name=None,
        candidate_state="actionable",
        candidate_action="investigate",
        resolution_evidence={},
    )


def _selected(selection: dict[str, object], issue_number: int) -> dict[str, object]:
    for entry in selection["selected"]:
        if entry["issueNumber"] == issue_number:
            return entry
    raise AssertionError(f"Issue {issue_number} was not selected.")


def _omitted(selection: dict[str, object], issue_number: int) -> dict[str, object]:
    for entry in selection["omitted"]:
        if entry["issueNumber"] == issue_number:
            return entry
    raise AssertionError(f"Issue {issue_number} was not omitted.")


class ReviewSelectionTests(unittest.TestCase):
    def test_omits_stable_known_cases_and_selects_changed_ones(self) -> None:
        compact = _compact([_stable_issue(101), _stable_issue(102)])

        selection = build_review_selection(
            compact,
            changed_issue_numbers=[102],
            known_issue_numbers=[101, 102],
        )

        self.assertEqual({102}, selected_issue_numbers(selection))
        self.assertEqual("changed", _selected(selection, 102)["changeClass"])
        self.assertEqual("unchanged-stable", _omitted(selection, 101)["reason"])

    def test_omits_known_unchanged_cases_even_when_review_is_required(self) -> None:
        compact = _compact([_investigate_issue(101)])
        self.assertTrue(compact["issues"][0]["reviewRequired"])

        selection = build_review_selection(compact, known_issue_numbers=[101])

        self.assertEqual(set(), selected_issue_numbers(selection))
        self.assertEqual("unchanged-stable", _omitted(selection, 101)["reason"])

    def test_selects_changed_cases_even_when_the_default_is_unambiguous(self) -> None:
        compact = _compact([_stable_issue(101)])
        self.assertFalse(compact["issues"][0]["reviewRequired"])

        selection = build_review_selection(
            compact,
            changed_issue_numbers=[101],
            known_issue_numbers=[101],
        )

        self.assertEqual({101}, selected_issue_numbers(selection))
        self.assertEqual("changed", _selected(selection, 101)["changeClass"])
        self.assertIn("material-change", _selected(selection, 101)["reviewReasons"])

    def test_selects_every_case_when_no_prior_state_is_known(self) -> None:
        compact = _compact([_investigate_issue(101), _stable_issue(102)])

        selection = build_review_selection(compact)

        self.assertEqual({101, 102}, selected_issue_numbers(selection))
        self.assertEqual("first-seen", _selected(selection, 101)["changeClass"])
        self.assertIn(
            "initial-assessment",
            _selected(selection, 102)["reviewReasons"],
        )

    def test_selects_new_issues_and_records_the_review_reasons(self) -> None:
        compact = _compact([_investigate_issue(101)])

        selection = build_review_selection(
            compact,
            new_issue_numbers=[101],
            known_issue_numbers=[],
        )

        entry = _selected(selection, 101)
        self.assertEqual("new", entry["changeClass"])
        self.assertIn("investigate-default", entry["reviewReasons"])

    def test_selects_an_unchanged_case_when_reassessment_is_due(self) -> None:
        compact = _compact([_stable_issue(101)])

        selection = build_review_selection(
            compact,
            known_issue_numbers=[101],
            due_issue_numbers=[101],
            reassessment_context_by_issue={
                101: {
                    "lastReviewedAt": "2026-08-20T12:00:00Z",
                    "reassessAt": "2026-08-27T12:00:00Z",
                }
            },
        )

        entry = _selected(selection, 101)
        self.assertEqual("due", entry["changeClass"])
        self.assertEqual(["scheduled-reassessment"], entry["changeReasons"])
        self.assertIn("scheduled-reassessment", entry["reviewReasons"])
        self.assertEqual("2026-08-20T12:00:00Z", entry["lastReviewedAt"])
        self.assertEqual("2026-08-27T12:00:00Z", entry["reassessAt"])

    def test_selected_case_carries_an_exact_question_and_stop_condition(self) -> None:
        compact = _compact([_investigate_issue(101)])
        selection = build_review_selection(
            compact,
            changed_issue_numbers=[101],
            known_issue_numbers=[101],
        )
        question = _selected(selection, 101)["question"]

        self.assertIn("#101", question["ask"])
        self.assertNotIn("Investigate this issue", question["ask"])
        self.assertNotEqual("After the next evidence update.", question["stopCondition"])
        self.assertIn("101", question["stopCondition"])
        self.assertTrue(question["evidenceChecked"])
        self.assertTrue(
            set(question["decisionGates"]).issubset(EVIDENCE_REQUEST_DECISION_GATES)
        )
        self.assertTrue(question["decisionGates"])

    def test_changed_case_carries_prior_bucket_and_change_reasons(self) -> None:
        compact = _compact([_stable_issue(101)])
        selection = build_review_selection(
            compact,
            changed_issue_numbers=[101],
            known_issue_numbers=[101],
            change_reasons_by_issue={
                101: ["derived-assessment-changed", "issue-source-updated"]
            },
            previous_judgments=[
                {
                    "issueNumber": 101,
                    "category": "flaky-test",
                    "recommendations": [{"disposition": "investigate"}],
                }
            ],
        )

        selected = _selected(selection, 101)
        self.assertEqual(
            ["derived-assessment-changed", "issue-source-updated"],
            selected["changeReasons"],
        )
        self.assertEqual("investigate", selected["previousDisposition"])
        self.assertEqual("flaky-test", selected["previousCategory"])

    def test_legacy_previous_decision_does_not_abort_selection(self) -> None:
        compact = _compact([_stable_issue(101)])

        selection = build_review_selection(
            compact,
            changed_issue_numbers=[101],
            known_issue_numbers=[101],
            previous_judgments=[
                {
                    "issueNumber": 101,
                    "issueUrl": "https://github.com/owner/repo/issues/101",
                    "issueKind": "ci-failure",
                    "state": "observing",
                    "proposedAction": "wait",
                }
            ],
        )

        selected = _selected(selection, 101)
        self.assertNotIn("previousDisposition", selected)
        self.assertNotIn("previousCategory", selected)

    def test_allowed_dispositions_exclude_unprojectable_close_and_escalation(self) -> None:
        compact = _compact([_investigate_issue(101)])

        allowed = set(_selected(selection=build_review_selection(compact), issue_number=101)["allowedDispositions"])

        self.assertNotIn("review-close", allowed)
        self.assertNotIn("ping-human", allowed)
        self.assertIn("investigate", allowed)
        self.assertIn("watch", allowed)

    def test_allowed_dispositions_include_close_backed_by_resolution_evidence(self) -> None:
        compact = _compact(
            [
                _compact_issue(
                    101,
                    parsed_row_count=2,
                    ledger_rows=[
                        {"date": "2026-08-16", "sourceRun": 900, "job": "Tests"},
                        {"date": "2026-08-17", "sourceRun": 901, "job": "Tests"},
                    ],
                    candidate_state="resolved",
                    candidate_action="recommend-close",
                    resolution_evidence={
                        "pullRequestEvidenceId": "pr:101",
                        "runEvidenceId": "run:101",
                        "mergeCommitSha": "a" * 40,
                    },
                )
            ]
        )
        self.assertTrue(compact["issues"][0]["reviewRequired"])

        selection = build_review_selection(compact)

        self.assertIn("review-close", _selected(selection, 101)["allowedDispositions"])

    def test_omits_a_deterministic_close_that_is_not_ambiguous(self) -> None:
        compact = _compact(
            [
                _compact_issue(
                    101,
                    candidate_state="resolved",
                    candidate_action="recommend-close",
                    resolution_evidence={
                        "pullRequestEvidenceId": "pr:101",
                        "runEvidenceId": "run:101",
                        "mergeCommitSha": "a" * 40,
                    },
                )
            ]
        )

        selection = build_review_selection(
            compact,
            known_issue_numbers=[101],
        )

        self.assertEqual(set(), selected_issue_numbers(selection))
        self.assertEqual("not-review-required", _omitted(selection, 101)["reason"])

    def test_allowed_dispositions_include_escalation_for_a_reported_decision(self) -> None:
        compact = _compact([_human_decision_compact_issue(309)])

        selection = build_review_selection(compact)

        self.assertIn("ping-human", _selected(selection, 309)["allowedDispositions"])

    def test_rejects_new_or_changed_issues_outside_the_compact_input(self) -> None:
        compact = _compact([_investigate_issue(101)])

        with self.assertRaisesRegex(ValidationError, "999"):
            build_review_selection(compact, changed_issue_numbers=[999])

    def test_rejects_reassessment_context_for_an_unknown_issue(self) -> None:
        compact = _compact([_investigate_issue(101)])

        with self.assertRaisesRegex(ValidationError, "999"):
            build_review_selection(
                compact,
                reassessment_context_by_issue={
                    999: {
                        "lastReviewedAt": "2026-08-20T12:00:00Z",
                        "reassessAt": "2026-08-27T12:00:00Z",
                    }
                },
            )

    def test_rejects_a_malformed_reassessment_timestamp(self) -> None:
        selection = build_review_selection(_compact([_stable_issue(101)]))
        selection["selected"][0]["lastReviewedAt"] = "not-a-timestamp"

        with self.assertRaisesRegex(ValidationError, "lastReviewedAt"):
            selected_issue_numbers(selection)

    def test_accepts_a_version_one_selection_without_change_reasons(self) -> None:
        selection = build_review_selection(_compact([_stable_issue(101)]))
        selection["schemaVersion"] = 1
        del selection["selected"][0]["changeReasons"]

        self.assertEqual({101}, selected_issue_numbers(selection))


def _agent(compact: dict[str, object], issues: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "snapshotId": compact["snapshotId"],
        "issues": issues,
    }


def _recommendation(
    issue_number: int,
    disposition: str,
    **extra: object,
) -> dict[str, object]:
    recommendation: dict[str, object] = {
        "disposition": disposition,
        "target": {"kind": "issue", "value": issue_number},
        "confidence": "medium",
        "summary": f"Agent chose {disposition}.",
        "evidenceIds": [f"issue:{issue_number}"],
        "missingEvidence": [],
        "reassessWhen": "After the next independent occurrence.",
    }
    recommendation.update(extra)
    return recommendation


def _agent_issue(
    issue_number: int,
    disposition: str,
    *,
    category: str = "product-or-tooling",
    **extra: object,
) -> dict[str, object]:
    return {
        "issueNumber": issue_number,
        "category": category,
        "recommendations": [_recommendation(issue_number, disposition, **extra)],
    }


class SelectedJudgmentMergeTests(unittest.TestCase):
    def test_omitted_issues_keep_their_deterministic_defaults(self) -> None:
        compact = _compact([_investigate_issue(101), _stable_issue(102)])
        selection = build_review_selection(
            compact,
            new_issue_numbers=[101],
            known_issue_numbers=[102],
        )
        default_102 = compact["issues"][1]["defaultJudgment"]

        merged = merge_selected_poc_judgments(
            compact,
            selection,
            _agent(compact, [_agent_issue(101, "watch")]),
        )

        self.assertEqual([101, 102], [issue["issueNumber"] for issue in merged["issues"]])
        self.assertEqual("watch", merged["issues"][0]["recommendations"][0]["disposition"])
        self.assertEqual(default_102, merged["issues"][1])

    def test_accepts_an_empty_agent_response_as_full_deterministic_agreement(self) -> None:
        compact = _compact([_investigate_issue(101)])
        selection = build_review_selection(compact)

        merged = merge_selected_poc_judgments(compact, selection, _agent(compact, []))

        self.assertEqual([compact["issues"][0]["defaultJudgment"]], merged["issues"])

    def test_rejects_a_judgment_for_an_unselected_issue(self) -> None:
        compact = _compact([_investigate_issue(101), _stable_issue(102)])
        selection = build_review_selection(
            compact,
            new_issue_numbers=[101],
            known_issue_numbers=[102],
        )

        with self.assertRaisesRegex(ValidationError, "was not selected"):
            merge_selected_poc_judgments(
                compact,
                selection,
                _agent(compact, [_agent_issue(102, "watch")]),
            )

    def test_rejects_a_judgment_for_an_issue_outside_the_compact_input(self) -> None:
        compact = _compact([_investigate_issue(101)])
        selection = build_review_selection(compact)

        with self.assertRaisesRegex(ValidationError, "Unexpected agent judgment"):
            merge_selected_poc_judgments(
                compact,
                selection,
                _agent(compact, [_agent_issue(999, "watch")]),
            )

    def test_rejects_review_close_without_deterministic_prerequisites(self) -> None:
        compact = _compact([_investigate_issue(101)])
        selection = build_review_selection(compact)

        with self.assertRaisesRegex(ValidationError, "review-close"):
            merge_selected_poc_judgments(
                compact,
                selection,
                _agent(compact, [_agent_issue(101, "review-close")]),
            )

    def test_rejects_close_authorized_only_by_a_tampered_selection(self) -> None:
        compact = _compact([_investigate_issue(101)])
        selection = build_review_selection(compact)
        selection["selected"][0]["allowedDispositions"].append("review-close")

        with self.assertRaisesRegex(ValidationError, "duplicate or resolution evidence"):
            merge_selected_poc_judgments(
                compact,
                selection,
                _agent(compact, [_agent_issue(101, "review-close")]),
            )

    def test_rejects_escalation_without_a_reported_human_decision(self) -> None:
        compact = _compact([_investigate_issue(101)])
        selection = build_review_selection(compact)
        escalation = {
            "context": "The build keeps failing.",
            "whyHuman": "Somebody should decide.",
            "question": "Who owns this?",
            "suggestedNextSteps": ["Find an owner."],
            "routingHint": "area-unknown",
        }

        with self.assertRaisesRegex(ValidationError, "ping-human"):
            merge_selected_poc_judgments(
                compact,
                selection,
                _agent(
                    compact,
                    [_agent_issue(101, "ping-human", humanEscalation=escalation)],
                ),
            )

    def test_rejects_escalation_authorized_only_by_a_tampered_selection(self) -> None:
        compact = _compact([_investigate_issue(101)])
        selection = build_review_selection(compact)
        selection["selected"][0]["allowedDispositions"].append("ping-human")
        escalation = {
            "context": "The build keeps failing.",
            "whyHuman": "Somebody should decide.",
            "question": "Who owns this?",
            "suggestedNextSteps": ["Find an owner."],
            "routingHint": "area-unknown",
        }

        with self.assertRaisesRegex(ValidationError, "reported human decision"):
            merge_selected_poc_judgments(
                compact,
                selection,
                _agent(
                    compact,
                    [_agent_issue(101, "ping-human", humanEscalation=escalation)],
                ),
            )

    def test_accepts_escalation_grounded_in_a_reported_decision(self) -> None:
        compact = _compact([_human_decision_compact_issue(309)])
        selection = build_review_selection(compact)
        default = compact["issues"][0]["defaultJudgment"]

        merged = merge_selected_poc_judgments(
            compact,
            selection,
            _agent(compact, [default]),
        )

        self.assertEqual(
            "ping-human",
            merged["issues"][0]["recommendations"][0]["disposition"],
        )

    def test_rejects_agent_judgments_from_another_snapshot(self) -> None:
        compact = _compact([_investigate_issue(101)])
        selection = build_review_selection(compact)
        agent = _agent(compact, [_agent_issue(101, "watch")])
        agent["snapshotId"] = "snapshot:owner/repo:2020-01-01T00:00:00Z"

        with self.assertRaisesRegex(ValidationError, "snapshotId"):
            merge_selected_poc_judgments(compact, selection, agent)

    def test_rejects_a_selection_from_another_snapshot(self) -> None:
        compact = _compact([_investigate_issue(101)])
        selection = build_review_selection(compact)
        selection["snapshotId"] = "snapshot:owner/repo:2020-01-01T00:00:00Z"

        with self.assertRaisesRegex(ValidationError, "snapshotId"):
            merge_selected_poc_judgments(
                compact,
                selection,
                _agent(compact, [_agent_issue(101, "watch")]),
            )

    def test_rejects_duplicate_agent_judgments(self) -> None:
        compact = _compact([_investigate_issue(101)])
        selection = build_review_selection(compact)

        with self.assertRaisesRegex(ValidationError, "Duplicate agent judgment"):
            merge_selected_poc_judgments(
                compact,
                selection,
                _agent(compact, [_agent_issue(101, "watch"), _agent_issue(101, "investigate")]),
            )

    def test_unprojectable_close_candidate_keeps_a_safe_default(self) -> None:
        compact = _compact(
            [
                _compact_issue(
                    101,
                    candidate_state="active",
                    candidate_action="recommend-close",
                    resolution_evidence={},
                ),
                _investigate_issue(102),
            ]
        )
        self.assertEqual(
            "watch",
            compact["issues"][0]["defaultJudgment"]["recommendations"][0]["disposition"],
        )
        selection = build_review_selection(compact)

        merged = merge_selected_poc_judgments(
            compact,
            selection,
            _agent(compact, [_agent_issue(102, "watch")]),
        )

        self.assertEqual([101, 102], [issue["issueNumber"] for issue in merged["issues"]])
        self.assertEqual(
            "watch",
            merged["issues"][0]["recommendations"][0]["disposition"],
        )
        self.assertEqual("watch", merged["issues"][1]["recommendations"][0]["disposition"])

    def test_merged_judgments_validate_against_the_prepared_assessment(self) -> None:
        prepared = _compact_prepared([_investigate_issue(101), _stable_issue(102)])
        compact = build_compact_poc_input(prepared)
        selection = build_review_selection(compact)

        merged = merge_selected_poc_judgments(
            compact,
            selection,
            _agent(compact, [_agent_issue(101, "watch")]),
        )

        validate_poc_judgments(prepared, merged)


if __name__ == "__main__":
    unittest.main()
