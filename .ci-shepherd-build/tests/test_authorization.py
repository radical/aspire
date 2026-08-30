from __future__ import annotations

import copy
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import unittest

from ci_shepherd.authorization import (
    AuthorizationError,
    generate_authorization_grant,
    load_authorized_execution,
    write_authorization_grant,
)


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

    def _add_blocked_sibling(self) -> str:
        action_id = "blocked-sibling"
        self.proposals["executionEligibility"] = {
            "status": "partially-eligible",
            "violations": [
                {
                    "actionId": action_id,
                    "blockingReasons": ["unavailable-evidence"],
                }
            ],
        }
        self.proposals["proposals"].append(
            {
                "actionId": action_id,
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
        return action_id

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

    def test_grant_snapshot_must_match_even_when_proposal_digest_matches(self) -> None:
        granted_snapshot_id = self.proposals["snapshotId"]
        self.proposals["snapshotId"] = (
            "snapshot:radical/aspire:2026-08-29T20:01:00Z"
        )
        self._write_inputs(grant_updates={"snapshotId": granted_snapshot_id})

        with self.assertRaisesRegex(
            AuthorizationError,
            "snapshotId does not match",
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

    def test_partially_eligible_document_allows_an_eligible_sibling_action(
        self,
    ) -> None:
        self._add_blocked_sibling()
        self._write_inputs()

        authorized = self._authorize()

        self.assertEqual(
            self.proposals["proposals"][0]["actionId"],
            authorized.proposal["actionId"],
        )

    def test_partially_eligible_document_rejects_authorizing_blocked_action(
        self,
    ) -> None:
        self.action_id = self._add_blocked_sibling()
        self._write_inputs()

        with self.assertRaisesRegex(
            AuthorizationError,
            "not eligible for execution",
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


class GenerateAuthorizationGrantTests(unittest.TestCase):
    """Tests for the grant *generator*, as opposed to the loader above.

    Fixture is a two-step chain on the same issue: a comment action with no
    dependency, and a close action that `dependsOn` the comment action. This
    is the minimal shape needed to prove dependency-chain enforcement.
    """

    def setUp(self) -> None:
        self.scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch.mkdir(parents=True)
        self.state_dir = (self.scratch / "state").resolve()
        self.proposals_path = self.scratch / "action-proposals.json"
        self.output_path = self.scratch / "authorization-grant.json"
        self.now = datetime(2026, 8, 29, 20, 0, tzinfo=UTC)
        self.comment_action_id = (
            "snapshot:radical/aspire:2026-08-29T20:00:00Z:"
            "issue:1:watch-comment"
        )
        self.close_action_id = (
            "snapshot:radical/aspire:2026-08-29T20:00:00Z:"
            "issue:1:review-close"
        )
        self.proposals: dict[str, object] = {
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
                    "actionId": self.comment_action_id,
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
                },
                {
                    "actionId": self.close_action_id,
                    "dependsOn": self.comment_action_id,
                    "issueNumber": 1,
                    "issueUrl": "https://github.com/radical/aspire/issues/1",
                    "operation": "close-issue",
                    "idempotencyKey": "issue:1:close",
                    "closeReason": "not_planned",
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
                },
            ],
            "unchangedIssueNumbers": [],
        }

    def tearDown(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)

    def _write_proposals(self) -> bytes:
        proposal_bytes = (
            json.dumps(self.proposals, indent=2, sort_keys=True) + "\n"
        ).encode()
        self.proposals_path.write_bytes(proposal_bytes)
        return proposal_bytes

    def _generate(self, **kwargs):
        kwargs.setdefault("now", self.now)
        kwargs.setdefault("grant_id", "grant:fixed-for-test")
        return generate_authorization_grant(
            self.proposals_path,
            state_dir=self.state_dir,
            **kwargs,
        )

    def test_two_action_chain_round_trips_through_the_loader(self) -> None:
        proposal_bytes = self._write_proposals()

        grant = self._generate(
            action_ids=[self.comment_action_id, self.close_action_id]
        )

        self.assertEqual(1, grant["schemaVersion"])
        self.assertEqual("grant:fixed-for-test", grant["grantId"])
        self.assertEqual("radical/aspire", grant["repository"])
        self.assertEqual("2026-08-29T20:00:00Z", grant["issuedAtUtc"])
        self.assertEqual("2026-08-29T20:15:00Z", grant["expiresAtUtc"])
        self.assertEqual(
            sorted([self.comment_action_id, self.close_action_id]),
            grant["allowedActionIds"],
        )
        self.assertEqual(
            ["close-issue", "create-comment"], grant["allowedOperations"]
        )
        self.assertEqual(
            [{"kind": "issue", "number": 1}], grant["allowedTargets"]
        )
        self.assertEqual([self.comment_action_id], grant["allowedChainRoots"])
        self.assertEqual([], grant["overrideSuppressionForActionIds"])
        self.assertEqual(
            {"maxMutationAttempts": 2, "maxChains": 1}, grant["budget"]
        )
        self.assertEqual(
            f"sha256:{hashlib.sha256(proposal_bytes).hexdigest()}",
            grant["proposalsDigest"],
        )

        self.output_path.write_text(json.dumps(grant), encoding="utf-8")

        for action_id in (self.comment_action_id, self.close_action_id):
            authorized = load_authorized_execution(
                self.proposals_path,
                self.output_path,
                state_dir=self.state_dir,
                action_id=action_id,
                now=datetime(2026, 8, 29, 20, 5, tzinfo=UTC),
            )
            self.assertEqual(action_id, authorized.proposal["actionId"])
            self.assertEqual(self.comment_action_id, authorized.chain_root)

    def test_omitted_dependency_is_rejected(self) -> None:
        self._write_proposals()

        with self.assertRaisesRegex(
            AuthorizationError,
            "depends on .* which is not also selected",
        ):
            self._generate(action_ids=[self.close_action_id])

    def test_omitted_middle_of_chain_dependency_is_rejected(self) -> None:
        # Extend the fixture with a third step depending on the close action,
        # forming comment -> close -> relabel. Put it on a second issue so the
        # per-issue proposal cap (2) is not exceeded. Selecting the two ends
        # while skipping the middle must still be rejected.
        relabel_action_id = (
            "snapshot:radical/aspire:2026-08-29T20:00:00Z:issue:2:relabel"
        )
        proposals = self.proposals["proposals"]
        assert isinstance(proposals, list)
        proposals.append(
            {
                "actionId": relabel_action_id,
                "dependsOn": self.close_action_id,
                "issueNumber": 2,
                "issueUrl": "https://github.com/radical/aspire/issues/2",
                "operation": "create-comment",
                "idempotencyKey": "issue:2:relabel",
                "body": (
                    "[automated] Relabeled.\n\n"
                    "<!-- ci-shepherd:idempotency-key=issue:2:relabel -->"
                ),
                "evidenceIds": ["issue:2"],
                "expectedIssueState": "closed",
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
        )
        self._write_proposals()

        with self.assertRaisesRegex(
            AuthorizationError,
            "depends on .* which is not also selected",
        ):
            self._generate(
                action_ids=[self.comment_action_id, relabel_action_id]
            )

    def test_digest_binds_to_exact_proposal_bytes(self) -> None:
        proposal_bytes = self._write_proposals()

        grant = self._generate(action_ids=[self.comment_action_id])

        self.assertEqual(
            f"sha256:{hashlib.sha256(proposal_bytes).hexdigest()}",
            grant["proposalsDigest"],
        )

        # A grant minted against the original bytes must not authorize
        # execution once the proposal document on disk changes underneath it.
        self.output_path.write_text(json.dumps(grant), encoding="utf-8")
        self.proposals_path.write_text(
            self.proposals_path.read_text(encoding="utf-8") + " ",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            AuthorizationError,
            "proposalsDigest does not match",
        ):
            load_authorized_execution(
                self.proposals_path,
                self.output_path,
                state_dir=self.state_dir,
                action_id=self.comment_action_id,
                now=datetime(2026, 8, 29, 20, 5, tzinfo=UTC),
            )

    def test_generated_grant_output_is_owner_only_on_posix(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX permission bits are not meaningful on Windows.")
        self._write_proposals()
        grant = self._generate(action_ids=[self.comment_action_id])

        written_path = write_authorization_grant(grant, self.output_path)

        mode = written_path.stat().st_mode & 0o777
        self.assertEqual(0o600, mode)
        self.assertEqual(grant, json.loads(written_path.read_text(encoding="utf-8")))

    def test_writing_grant_does_not_change_existing_parent_permissions(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX permission bits are not meaningful on Windows.")
        self._write_proposals()
        grant = self._generate(action_ids=[self.comment_action_id])
        self.scratch.chmod(0o755)

        write_authorization_grant(grant, self.output_path)

        self.assertEqual(0o755, self.scratch.stat().st_mode & 0o777)

    def test_output_symlink_is_rejected(self) -> None:
        self._write_proposals()
        grant = self._generate(action_ids=[self.comment_action_id])
        target = self.scratch / "elsewhere.json"
        target.write_text("{}", encoding="utf-8")
        self.output_path.symlink_to(target)

        with self.assertRaisesRegex(AuthorizationError, "cannot traverse a symlink"):
            write_authorization_grant(grant, self.output_path)

    def test_state_dir_symlink_is_rejected_consistently_with_loader(self) -> None:
        self._write_proposals()
        real_dir = self.scratch / "real-state"
        real_dir.mkdir()
        symlinked_state_dir = self.scratch / "state-link"
        symlinked_state_dir.symlink_to(real_dir)

        with self.assertRaisesRegex(AuthorizationError, "cannot traverse a symlink"):
            generate_authorization_grant(
                self.proposals_path,
                action_ids=[self.comment_action_id],
                state_dir=symlinked_state_dir,
                now=self.now,
                grant_id="grant:fixed-for-test",
            )

    def test_blocked_document_is_rejected(self) -> None:
        # Every proposal is ineligible, so the document status is unambiguously
        # "blocked" under any valid derivation. A grant produced against a
        # blocked document would always fail at execution time regardless of
        # which action was selected, so generation must refuse it up front.
        comment_proposal = self.proposals["proposals"][0]
        close_proposal = self.proposals["proposals"][1]
        assert isinstance(comment_proposal, dict)
        assert isinstance(close_proposal, dict)
        comment_proposal["executionEligibility"] = {
            "eligible": False,
            "ciLabels": [],
            "occurrenceCount": 1,
            "collectionComplete": True,
            "unavailableEvidenceIds": [],
            "untrustedReferenceEvidenceIds": [],
            "blockingReasons": ["missing-ci-label"],
        }
        close_proposal["executionEligibility"] = {
            "eligible": False,
            "ciLabels": ["ci-failure-cause"],
            "occurrenceCount": 1,
            "collectionComplete": True,
            "unavailableEvidenceIds": ["missing:1"],
            "untrustedReferenceEvidenceIds": [],
            "blockingReasons": ["unavailable-evidence"],
        }
        self.proposals["executionEligibility"] = {
            "status": "blocked",
            "violations": [
                {
                    "actionId": self.comment_action_id,
                    "blockingReasons": ["missing-ci-label"],
                },
                {
                    "actionId": self.close_action_id,
                    "blockingReasons": ["unavailable-evidence"],
                },
            ],
        }
        self._write_proposals()

        with self.assertRaisesRegex(
            AuthorizationError,
            "Proposal document is not eligible",
        ):
            self._generate(action_ids=[self.comment_action_id])

    def test_partially_eligible_document_grants_only_selected_eligible_action(
        self,
    ) -> None:
        close_proposal = self.proposals["proposals"][1]
        assert isinstance(close_proposal, dict)
        close_proposal["executionEligibility"] = {
            "eligible": False,
            "ciLabels": ["ci-failure-cause"],
            "occurrenceCount": 1,
            "collectionComplete": False,
            "unavailableEvidenceIds": [],
            "untrustedReferenceEvidenceIds": [],
            "blockingReasons": ["incomplete-collection"],
        }
        self.proposals["executionEligibility"] = {
            "status": "partially-eligible",
            "violations": [
                {
                    "actionId": self.close_action_id,
                    "blockingReasons": ["incomplete-collection"],
                }
            ],
        }
        self._write_proposals()

        grant = self._generate(action_ids=[self.comment_action_id])

        self.assertEqual([self.comment_action_id], grant["allowedActionIds"])
        self.assertEqual(["create-comment"], grant["allowedOperations"])

    def test_production_repository_is_rejected(self) -> None:
        self.proposals["repository"] = "microsoft/aspire"
        self.proposals["snapshotId"] = "snapshot:microsoft/aspire:2026-08-29T20:00:00Z"
        for proposal in self.proposals["proposals"]:
            assert isinstance(proposal, dict)
            proposal["issueUrl"] = "https://github.com/microsoft/aspire/issues/1"
        self._write_proposals()

        with self.assertRaisesRegex(AuthorizationError, "protected"):
            self._generate(action_ids=[self.comment_action_id])
        self.assertFalse(self.output_path.exists())

    def test_selecting_one_action_does_not_authorize_a_sibling_action(self) -> None:
        self._write_proposals()
        grant = self._generate(action_ids=[self.comment_action_id])
        self.output_path.write_text(json.dumps(grant), encoding="utf-8")

        with self.assertRaisesRegex(AuthorizationError, "does not enumerate"):
            load_authorized_execution(
                self.proposals_path,
                self.output_path,
                state_dir=self.state_dir,
                action_id=self.close_action_id,
                now=datetime(2026, 8, 29, 20, 5, tzinfo=UTC),
            )

    def test_unknown_action_id_is_rejected(self) -> None:
        self._write_proposals()

        with self.assertRaisesRegex(
            AuthorizationError,
            "is not in the proposal document",
        ):
            self._generate(action_ids=["does-not-exist"])

    def test_duplicate_action_id_is_rejected(self) -> None:
        self._write_proposals()

        with self.assertRaisesRegex(AuthorizationError, "Duplicate selected actionId"):
            self._generate(
                action_ids=[self.comment_action_id, self.comment_action_id]
            )

    def test_legacy_proposal_schema_is_rejected(self) -> None:
        self.proposals["schemaVersion"] = 1
        self.proposals_path.write_bytes(
            (json.dumps(self.proposals, indent=2, sort_keys=True) + "\n").encode()
        )

        with self.assertRaisesRegex(AuthorizationError, "schemaVersion 2"):
            self._generate(action_ids=[self.comment_action_id])

    def test_ttl_defaults_to_fifteen_minutes(self) -> None:
        self._write_proposals()

        grant = self._generate(action_ids=[self.comment_action_id])

        self.assertEqual("2026-08-29T20:15:00Z", grant["expiresAtUtc"])

    def test_ttl_above_hard_maximum_is_rejected(self) -> None:
        self._write_proposals()

        with self.assertRaisesRegex(AuthorizationError, "between 1 and 60 minutes"):
            self._generate(
                action_ids=[self.comment_action_id], ttl_minutes=61
            )

    def test_ttl_of_zero_is_rejected(self) -> None:
        self._write_proposals()

        with self.assertRaisesRegex(AuthorizationError, "between 1 and 60 minutes"):
            self._generate(action_ids=[self.comment_action_id], ttl_minutes=0)

    def test_override_suppression_must_be_selected(self) -> None:
        self._write_proposals()

        with self.assertRaisesRegex(
            AuthorizationError,
            "must reference a selected actionId",
        ):
            self._generate(
                action_ids=[self.comment_action_id],
                override_suppression_for_action_ids=[self.close_action_id],
            )

    def test_override_suppression_is_never_defaulted(self) -> None:
        self._write_proposals()

        grant = self._generate(
            action_ids=[self.comment_action_id, self.close_action_id],
            override_suppression_for_action_ids=[self.close_action_id],
        )

        self.assertEqual(
            [self.close_action_id], grant["overrideSuppressionForActionIds"]
        )

        grant_without_override = self._generate(
            action_ids=[self.comment_action_id, self.close_action_id]
        )
        self.assertEqual([], grant_without_override["overrideSuppressionForActionIds"])


if __name__ == "__main__":
    unittest.main()
