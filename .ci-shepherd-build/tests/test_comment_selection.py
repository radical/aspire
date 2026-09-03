from __future__ import annotations

import unittest

from ci_shepherd.comment_selection import build_comment_selection


def _proposal(
    issue_number: int,
    suffix: str,
    *,
    operation: str = "create-comment",
    eligible: bool = True,
) -> dict[str, object]:
    return {
        "actionId": f"snapshot:owner/repo:time:issue:{issue_number}:{suffix}",
        "issueNumber": issue_number,
        "operation": operation,
        "executionEligibility": {
            "eligible": eligible,
            "blockingReasons": [] if eligible else ["unavailable-evidence"],
        },
    }


class CommentSelectionTests(unittest.TestCase):
    def test_selection_is_stable_ranked_and_discloses_the_cut(self) -> None:
        proposals = {
            "repository": "owner/repo",
            "snapshotId": "snapshot:owner/repo:time",
            "proposals": [
                _proposal(30, "watch-comment"),
                _proposal(11, "watch-comment"),
                _proposal(40, "quarantine-reconciliation-comment"),
                _proposal(20, "ping-human-comment", operation="edit-comment"),
                _proposal(10, "ping-human-comment"),
                _proposal(50, "watch-comment", eligible=False),
            ],
        }

        first = build_comment_selection(proposals, max_comments=3)
        second = build_comment_selection(proposals, max_comments=3)

        self.assertEqual(first, second)
        self.assertEqual(
            [
                "snapshot:owner/repo:time:issue:10:ping-human-comment",
                "snapshot:owner/repo:time:issue:20:ping-human-comment",
                "snapshot:owner/repo:time:issue:40:"
                "quarantine-reconciliation-comment",
            ],
            first["selectedActionIds"],
        )
        self.assertEqual(5, first["eligibleCount"])
        self.assertEqual(3, first["selectedCount"])
        self.assertEqual(
            "Selected the first 3 of 5 eligible issue comments by the committed "
            "priority order.",
            first["cutReason"],
        )
        self.assertEqual(
            ["unavailable-evidence"],
            first["excluded"][0]["reasons"],
        )

    def test_missing_execution_eligibility_is_excluded(self) -> None:
        proposal = _proposal(42, "watch-comment")
        proposal.pop("executionEligibility")

        selection = build_comment_selection(
            {
                "repository": "owner/repo",
                "snapshotId": "snapshot:owner/repo:time",
                "proposals": [proposal],
            },
            max_comments=1,
        )

        self.assertEqual([], selection["selectedActionIds"])
        self.assertEqual(
            [
                {
                    "actionId": proposal["actionId"],
                    "reasons": ["not-execution-eligible"],
                }
            ],
            selection["excluded"],
        )


if __name__ == "__main__":
    unittest.main()
