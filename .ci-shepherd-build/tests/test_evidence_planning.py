from __future__ import annotations

import unittest

from ci_shepherd.evidence_planning import build_proposal_evidence_requests
from ci_shepherd.models import validate_evidence_requests


class ProposalEvidencePlanningTests(unittest.TestCase):
    def test_requests_partial_workflow_run_that_blocks_a_proposal(self) -> None:
        snapshot = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "collectedAt": "2026-08-31T12:00:00Z",
            "openIssues": [7],
            "openPullRequests": [],
            "pullRequests": [],
            "rejectedCandidates": [],
            "evidence": {
                "issue:7": {
                    "kind": "issue-event",
                    "url": "https://github.com/owner/repo/issues/7",
                    "collectedAt": "2026-08-31T12:00:00Z",
                    "availability": "available",
                    "payload": {
                        "number": 7,
                        "state": "open",
                        "title": "CI failure",
                        "url": "https://github.com/owner/repo/issues/7",
                        "author": "github-actions[bot]",
                    },
                },
                "run:123": {
                    "kind": "workflow-run",
                    "url": "https://github.com/owner/repo/actions/runs/123",
                    "collectedAt": "2026-08-31T12:00:00Z",
                    "availability": "partial",
                    "payload": {
                        "runId": 123,
                        "targetRepository": "owner/repo",
                        "referencedBy": [
                            {
                                "sourceIssueNumber": 7,
                                "sourceEvidenceId": "issue:7",
                            }
                        ],
                    },
                },
            },
            "collectionErrors": [],
        }
        proposals = {
            "proposals": [
                {
                    "issueNumber": 7,
                    "evidenceIds": ["issue:7", "run:123"],
                    "executionEligibility": {
                        "unavailableEvidenceIds": ["run:123"],
                    },
                }
            ]
        }

        requests, deferred = build_proposal_evidence_requests(snapshot, proposals)

        self.assertEqual([], deferred)
        self.assertEqual(
            [
                {
                    "type": "workflow-run",
                    "sourceIssueNumber": 7,
                    "evidenceId": "run:123",
                    "decisionGate": "current-failing-run",
                    "reason": (
                        "Refresh the exact run cited by a projected status action "
                        "before deciding whether that action is executable."
                    ),
                }
            ],
            requests["requests"],
        )
        validate_evidence_requests(snapshot, requests)

    def test_caps_expansion_and_reports_deferred_exact_evidence(self) -> None:
        snapshot = {
            "schemaVersion": 1,
            "repository": "owner/repo",
            "collectedAt": "2026-08-31T12:00:00Z",
            "openIssues": list(range(1, 28)),
            "openPullRequests": [],
            "pullRequests": [],
            "rejectedCandidates": [],
            "evidence": {},
            "collectionErrors": [],
        }
        proposals = {"proposals": []}
        for issue_number in range(1, 28):
            snapshot["evidence"][f"issue:{issue_number}"] = {
                "kind": "issue-event",
                "url": f"https://github.com/owner/repo/issues/{issue_number}",
                "collectedAt": "2026-08-31T12:00:00Z",
                "availability": "available",
                "payload": {
                    "number": issue_number,
                    "state": "open",
                    "title": "CI failure",
                    "url": f"https://github.com/owner/repo/issues/{issue_number}",
                    "author": "github-actions[bot]",
                },
            }
            evidence_id = f"run:{1000 + issue_number}"
            snapshot["evidence"][evidence_id] = {
                "kind": "workflow-run",
                "url": (
                    "https://github.com/owner/repo/actions/runs/"
                    f"{1000 + issue_number}"
                ),
                "collectedAt": "2026-08-31T12:00:00Z",
                "availability": "partial",
                "payload": {
                    "runId": 1000 + issue_number,
                    "targetRepository": "owner/repo",
                    "referencedBy": [
                        {
                            "sourceIssueNumber": issue_number,
                            "sourceEvidenceId": f"issue:{issue_number}",
                        }
                    ],
                },
            }
            proposals["proposals"].append(
                {
                    "issueNumber": issue_number,
                    "evidenceIds": [f"issue:{issue_number}", evidence_id],
                    "executionEligibility": {
                        "unavailableEvidenceIds": [evidence_id],
                    },
                }
            )

        requests, deferred = build_proposal_evidence_requests(snapshot, proposals)

        self.assertEqual(25, len(requests["requests"]))
        self.assertEqual(["run:1026", "run:1027"], deferred)
        validate_evidence_requests(snapshot, requests)


if __name__ == "__main__":
    unittest.main()
