from __future__ import annotations

import copy
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import shutil
import unittest

from ci_shepherd.authorization import AuthorizationError, load_authorized_execution


class AuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch.mkdir(parents=True)
        self.state_dir = (self.scratch / "state").resolve()
        self.proposals_path = self.scratch / "action-proposals.json"
        self.authorization_path = self.scratch / "authorization-grant.json"
        self.action_id = (
            "snapshot:radical/aspire:2026-08-29T20:00:00Z:"
            "issue:1:watch-comment"
        )
        self.proposals = {
            "schemaVersion": 2,
            "repository": "radical/aspire",
            "snapshotId": "snapshot:radical/aspire:2026-08-29T20:00:00Z",
            "shepherdAuthor": "radical",
            "generatedAtUtc": "2026-08-29T20:00:00Z",
            "proposalTtlHours": 24,
            "maxProposalsPerIssue": 2,
            "executionEligibility": {
                "status": "eligible",
                "violations": [],
            },
            "proposals": [
                {
                    "actionId": self.action_id,
                    "issueNumber": 1,
                    "issueUrl": "https://github.com/radical/aspire/issues/1",
                    "operation": "create-comment",
                    "idempotencyKey": "issue:1:status",
                    "body": (
                        "[automated] Watching.\n\n"
                        "<!-- ci-shepherd:idempotency-key=issue:1:status -->"
                    ),
                    "evidenceIds": ["issue:1"],
                    "expectedIssueState": "open",
                    "executionEligibility": {
                        "eligible": True,
                        "ciLabels": ["ci-failure-cause"],
                        "occurrenceCount": 1,
                        "collectionComplete": True,
                        "unavailableEvidenceIds": [],
                        "untrustedReferenceEvidenceIds": [],
                        "blockingReasons": [],
                    },
                    "sourceEvidenceFingerprint": {
                        "issueUpdatedAt": "2026-08-29T19:59:00Z",
                    },
                }
            ],
            "unchangedIssueNumbers": [],
        }

    def tearDown(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)

    def _write_inputs(
        self,
        *,
        grant_updates: dict[str, object] | None = None,
    ) -> bytes:
        proposal_bytes = (
            json.dumps(self.proposals, indent=2, sort_keys=True) + "\n"
        ).encode()
        self.proposals_path.write_bytes(proposal_bytes)
        grant = {
            "schemaVersion": 1,
            "grantId": "grant:test",
            "repository": "radical/aspire",
            "stateDirectory": str(self.state_dir),
            "issuedAtUtc": "2026-08-29T20:00:00Z",
            "expiresAtUtc": "2026-08-29T20:15:00Z",
            "snapshotId": self.proposals["snapshotId"],
            "proposalsDigest": (
                f"sha256:{hashlib.sha256(proposal_bytes).hexdigest()}"
            ),
            "allowedActionIds": [self.action_id],
            "allowedOperations": ["create-comment"],
            "allowedTargets": [{"kind": "issue", "number": 1}],
            "allowedChainRoots": [self.action_id],
            "overrideSuppressionForActionIds": [],
            "budget": {
                "maxMutationAttempts": 1,
                "maxChains": 1,
            },
        }
        grant.update(grant_updates or {})
        self.authorization_path.write_text(
            json.dumps(grant),
            encoding="utf-8",
        )
        return proposal_bytes

    def _authorize(self):
        return load_authorized_execution(
            self.proposals_path,
            self.authorization_path,
            state_dir=self.state_dir,
            action_id=self.action_id,
            now=datetime(2026, 8, 29, 20, 5, tzinfo=UTC),
        )

    def test_valid_grant_authorizes_exact_action(self) -> None:
        proposal_bytes = self._write_inputs()

        authorized = self._authorize()

        self.assertEqual("grant:test", authorized.grant.grant_id)
        self.assertEqual(self.action_id, authorized.proposal["actionId"])
        self.assertEqual(proposal_bytes, authorized.proposal_bytes)

    def test_changed_proposal_bytes_are_rejected(self) -> None:
        self._write_inputs()
        self.proposals_path.write_text(
            self.proposals_path.read_text(encoding="utf-8") + " ",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            AuthorizationError,
            "proposalsDigest does not match",
        ):
            self._authorize()

    def test_mismatched_state_directory_is_rejected(self) -> None:
        self._write_inputs(
            grant_updates={
                "stateDirectory": str((self.scratch / "other-state").resolve())
            }
        )

        with self.assertRaisesRegex(
            AuthorizationError,
            "stateDirectory does not match",
        ):
            self._authorize()

    def test_production_repository_is_rejected_during_remediation(self) -> None:
        for repository in ("microsoft/aspire", "Microsoft/aspire", "microsoft/Aspire"):
            with self.subTest(repository=repository):
                self.proposals["repository"] = repository
                self.proposals["snapshotId"] = (
                    f"snapshot:{repository}:2026-08-29T20:00:00Z"
                )
                proposal = self.proposals["proposals"][0]
                assert isinstance(proposal, dict)
                proposal["issueUrl"] = (
                    f"https://github.com/{repository}/issues/1"
                )
                self._write_inputs(
                    grant_updates={
                        "repository": repository,
                        "snapshotId": self.proposals["snapshotId"],
                    }
                )

                with self.assertRaisesRegex(AuthorizationError, "protected"):
                    self._authorize()

    def test_blocked_document_refuses_an_eligible_sibling_action(self) -> None:
        self.proposals["executionEligibility"] = {
            "status": "blocked",
            "violations": [
                {
                    "actionId": "blocked-sibling",
                    "blockingReasons": ["unavailable-evidence"],
                }
            ],
        }
        self.proposals["proposals"].append(
            {
                "actionId": "blocked-sibling",
                "issueNumber": 1,
                "issueUrl": "https://github.com/radical/aspire/issues/1",
                "operation": "create-comment",
                "idempotencyKey": "issue:1:blocked",
                "body": (
                    "[automated] Blocked.\n\n"
                    "<!-- ci-shepherd:idempotency-key=issue:1:blocked -->"
                ),
                "evidenceIds": ["missing:1"],
                "expectedIssueState": "open",
                "executionEligibility": {
                    "eligible": False,
                    "ciLabels": ["ci-failure-cause"],
                    "occurrenceCount": 1,
                    "collectionComplete": True,
                    "unavailableEvidenceIds": ["missing:1"],
                    "untrustedReferenceEvidenceIds": [],
                    "blockingReasons": ["unavailable-evidence"],
                },
                "sourceEvidenceFingerprint": {
                    "issueUpdatedAt": "2026-08-29T19:59:00Z",
                },
            }
        )
        self._write_inputs()

        with self.assertRaisesRegex(
            AuthorizationError,
            "Proposal document is not eligible",
        ):
            self._authorize()

    def test_document_cannot_claim_eligible_with_an_ineligible_sibling(self) -> None:
        sibling = copy.deepcopy(self.proposals["proposals"][0])
        sibling["actionId"] = "blocked-sibling"
        sibling["idempotencyKey"] = "issue:1:blocked"
        sibling["body"] = (
            "[automated] Blocked.\n\n"
            "<!-- ci-shepherd:idempotency-key=issue:1:blocked -->"
        )
        sibling["evidenceIds"] = ["missing:1"]
        sibling["executionEligibility"] = {
            "eligible": False,
            "ciLabels": ["ci-failure-cause"],
            "occurrenceCount": 1,
            "collectionComplete": True,
            "unavailableEvidenceIds": ["missing:1"],
            "untrustedReferenceEvidenceIds": [],
            "blockingReasons": ["unavailable-evidence"],
        }
        self.proposals["proposals"].append(sibling)
        self._write_inputs()

        with self.assertRaisesRegex(
            AuthorizationError,
            "internally inconsistent",
        ):
            self._authorize()

    def test_expired_grant_is_rejected(self) -> None:
        self._write_inputs(
            grant_updates={"expiresAtUtc": "2026-08-29T20:04:59Z"}
        )

        with self.assertRaisesRegex(AuthorizationError, "expired"):
            self._authorize()

    def test_long_lived_grant_is_rejected(self) -> None:
        self._write_inputs(
            grant_updates={"expiresAtUtc": "2026-08-29T21:00:01Z"}
        )

        with self.assertRaisesRegex(AuthorizationError, "at most 1 hour"):
            self._authorize()

    def test_legacy_proposal_schema_is_not_executable(self) -> None:
        self.proposals["schemaVersion"] = 1
        for field in (
            "generatedAtUtc",
            "proposalTtlHours",
            "maxProposalsPerIssue",
        ):
            self.proposals.pop(field)
        proposal = self.proposals["proposals"][0]
        assert isinstance(proposal, dict)
        proposal.pop("executionEligibility")
        proposal["requiresSeparateApproval"] = True
        self._write_inputs()

        with self.assertRaisesRegex(AuthorizationError, "schemaVersion 2"):
            self._authorize()

    def test_stale_proposal_document_is_rejected(self) -> None:
        self.proposals["generatedAtUtc"] = "2026-08-28T19:00:00Z"
        self.proposals["proposalTtlHours"] = 24
        self._write_inputs()

        with self.assertRaisesRegex(AuthorizationError, "expired"):
            self._authorize()

    def test_non_enumerated_action_is_rejected(self) -> None:
        self._write_inputs(
            grant_updates={
                "allowedActionIds": ["different-action"],
                "allowedChainRoots": ["different-action"],
            }
        )

        with self.assertRaisesRegex(AuthorizationError, "does not enumerate"):
            self._authorize()

    def test_ineligible_action_is_rejected_before_reservation(self) -> None:
        eligibility = self.proposals["proposals"][0]["executionEligibility"]
        assert isinstance(eligibility, dict)
        eligibility.update(
            {
                "eligible": False,
                "ciLabels": [],
                "blockingReasons": ["missing-ci-label"],
            }
        )
        self.proposals["executionEligibility"] = {
            "status": "blocked",
            "violations": [
                {
                    "actionId": self.action_id,
                    "blockingReasons": ["missing-ci-label"],
                }
            ],
        }
        self._write_inputs()

        with self.assertRaisesRegex(AuthorizationError, "not eligible"):
            self._authorize()

    def test_grant_cannot_select_violation_behavior(self) -> None:
        self._write_inputs(grant_updates={"onViolation": "continue"})

        with self.assertRaisesRegex(
            AuthorizationError,
            "exactly the supported fields",
        ):
            self._authorize()

    def test_duplicate_grant_keys_are_rejected(self) -> None:
        self._write_inputs()
        grant_text = self.authorization_path.read_text(encoding="utf-8")
        self.authorization_path.write_text(
            grant_text.replace(
                '{"schemaVersion": 1,',
                '{"schemaVersion": 1, "schemaVersion": 1,',
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(AuthorizationError, "duplicate key"):
            self._authorize()

    def test_symlinked_grant_is_rejected(self) -> None:
        self._write_inputs()
        target = self.scratch / "grant-target.json"
        self.authorization_path.replace(target)
        self.authorization_path.symlink_to(target)

        with self.assertRaisesRegex(AuthorizationError, "cannot traverse a symlink"):
            self._authorize()


if __name__ == "__main__":
    unittest.main()
