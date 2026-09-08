from __future__ import annotations

import contextlib
import copy
from datetime import UTC, datetime, timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import create_authorization
from ci_shepherd.authorization import (
    AuthorizationError,
    generate_authorization_grant,
    load_authorized_execution,
    write_authorization_grant,
)
from ci_shepherd.comment_selection import build_comment_selection
from ci_shepherd.coordinator_state import CoordinatorStateStore
from ci_shepherd.operation_policy import DEFAULT_CAPS, OPERATION_CLASSES
from ci_shepherd import policy_selection as ps


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
                    "evidenceBasis": "ci-occurrence",
                    "idempotencyKey": "issue:1:status",
                    "body": (
                        "[automated] Watching.\n\n"
                        "<!-- ci-shepherd:idempotency-key=issue:1:status -->"
                    ),
                    "evidenceIds": ["issue:1"],
                    "expectedIssueState": "open",
                    "executionEligibility": {
                        "eligible": True,
                        "evidenceBasis": "ci-occurrence",
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
            "schemaVersion": 2,
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
                "maxRunningCopilotTasks": 2,
                "maxCopilotStartsPerRolling24h": 3,
                "maxOpenDelegatedPullRequests": 5,
                "maxRepositoryRunningCopilotTasks": 100,
            },
            "productionCommentPilot": False,
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
                "evidenceBasis": "ci-occurrence",
                "idempotencyKey": "issue:1:blocked",
                "body": (
                    "[automated] Blocked.\n\n"
                    "<!-- ci-shepherd:idempotency-key=issue:1:blocked -->"
                ),
                "evidenceIds": ["missing:1"],
                "expectedIssueState": "open",
                "executionEligibility": {
                    "eligible": False,
                    "evidenceBasis": "ci-occurrence",
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
            "evidenceBasis": "ci-occurrence",
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
                '{"schemaVersion": 2,',
                '{"schemaVersion": 2, "schemaVersion": 2,',
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(AuthorizationError, "duplicate key"):
            self._authorize()

    def test_legacy_authorization_grant_schema_is_rejected(self) -> None:
        self._write_inputs(grant_updates={"schemaVersion": 1})

        with self.assertRaisesRegex(
            AuthorizationError,
            "schemaVersion must equal 2",
        ):
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
        self.comment_selection_path = self.scratch / "comment-selection.json"
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
                    "evidenceBasis": "ci-occurrence",
                    "evidenceBasis": "ci-occurrence",
                    "idempotencyKey": "issue:1:status",
                    "body": (
                        "[automated] Watching.\n\n"
                        "<!-- ci-shepherd:idempotency-key=issue:1:status -->"
                    ),
                    "evidenceIds": ["issue:1"],
                    "expectedIssueState": "open",
                    "executionEligibility": {
                        "eligible": True,
                        "evidenceBasis": "ci-occurrence",
                        "evidenceBasis": "ci-occurrence",
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
                    "evidenceBasis": "ci-occurrence",
                    "idempotencyKey": "issue:1:close",
                    "closeReason": "not_planned",
                    "evidenceIds": ["issue:1"],
                    "expectedIssueState": "open",
                    "executionEligibility": {
                        "eligible": True,
                        "evidenceBasis": "ci-occurrence",
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
        if (
            kwargs.get("allow_production_comment_pilot") is True
            and "comment_selection_path" not in kwargs
        ):
            self._write_comment_selection(list(kwargs["action_ids"]))
            kwargs["comment_selection_path"] = self.comment_selection_path
        return generate_authorization_grant(
            self.proposals_path,
            state_dir=self.state_dir,
            **kwargs,
        )

    def _write_comment_selection(self, action_ids: list[str]) -> bytes:
        selection = build_comment_selection(
            self.proposals,
            max_comments=min(5, max(1, len(action_ids))),
        )
        selection["selectedActionIds"] = action_ids
        selection_bytes = (
            json.dumps(
                selection,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode()
        self.comment_selection_path.write_bytes(selection_bytes)
        return selection_bytes

    def _use_production_repository(self) -> None:
        serialized = json.dumps(self.proposals).replace(
            "radical/aspire",
            "microsoft/aspire",
        )
        self.proposals = json.loads(serialized)
        self.proposals["snapshotId"] += ":r1"
        self.proposals["productionPilotCapability"] = {
            "schemaVersion": 1,
            "evidenceRound": 1,
        }
        for proposal in self.proposals["proposals"]:
            if proposal["operation"] == "create-comment":
                proposal["operation"] = "edit-comment"
                proposal["commentId"] = 1000 + proposal["issueNumber"]
                proposal["sourceCommentFingerprint"] = {
                    "bodySha256": "0" * 64
                }
        self.comment_action_id = self.comment_action_id.replace(
            "radical/aspire",
            "microsoft/aspire",
        )
        self.close_action_id = self.close_action_id.replace(
            "radical/aspire",
            "microsoft/aspire",
        )

    def _add_comment_proposal(
        self,
        issue_number: int,
        *,
        suffix: str = "watch-comment",
    ) -> str:
        proposal = copy.deepcopy(self.proposals["proposals"][0])
        action_id = (
            f"{self.proposals['snapshotId']}:issue:{issue_number}:{suffix}"
        )
        proposal.update(
            {
                "actionId": action_id,
                "issueNumber": issue_number,
                "issueUrl": (
                    f"https://github.com/{self.proposals['repository']}/issues/"
                    f"{issue_number}"
                ),
                "idempotencyKey": f"issue:{issue_number}:status:{suffix}",
                "body": (
                    f"[automated] Watching #{issue_number}.\n\n"
                    "<!-- ci-shepherd:idempotency-key="
                    f"issue:{issue_number}:status:{suffix} -->"
                ),
            }
        )
        proposal.pop("dependsOn", None)
        self.proposals["proposals"].append(proposal)
        return action_id

    def test_two_action_chain_round_trips_through_the_loader(self) -> None:
        proposal_bytes = self._write_proposals()

        grant = self._generate(
            action_ids=[self.comment_action_id, self.close_action_id]
        )

        self.assertEqual(2, grant["schemaVersion"])
        self.assertEqual("grant:fixed-for-test", grant["grantId"])
        self.assertEqual("radical/aspire", grant["repository"])
        self.assertEqual("2026-08-29T20:00:00Z", grant["issuedAtUtc"])
        self.assertEqual("2026-08-29T20:15:00Z", grant["expiresAtUtc"])
        self.assertEqual(
            [self.comment_action_id, self.close_action_id],
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
            {
                "maxMutationAttempts": 2,
                "maxChains": 1,
                "maxRunningCopilotTasks": 2,
                "maxCopilotStartsPerRolling24h": 3,
                "maxOpenDelegatedPullRequests": 5,
                "maxRepositoryRunningCopilotTasks": 100,
            },
            grant["budget"],
        )
        self.assertFalse(grant["productionCommentPilot"])
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

    def test_copilot_capacity_limits_round_trip_as_signed_grant_data(self) -> None:
        self._write_proposals()
        grant = self._generate(
            action_ids=[self.comment_action_id],
            max_running_copilot_tasks=10,
            max_copilot_starts_per_rolling_24h=5,
            max_open_delegated_prs=12,
        )
        self.output_path.write_text(json.dumps(grant), encoding="utf-8")

        authorized = load_authorized_execution(
            self.proposals_path,
            self.output_path,
            state_dir=self.state_dir,
            action_id=self.comment_action_id,
            now=datetime(2026, 8, 29, 20, 5, tzinfo=UTC),
        )

        self.assertEqual(10, authorized.grant.budget.max_running_copilot_tasks)
        self.assertEqual(
            5,
            authorized.grant.budget.max_copilot_starts_per_rolling_24h,
        )
        self.assertEqual(12, authorized.grant.budget.max_open_delegated_prs)

    def test_source_reconciliation_revalidates_checkout_before_execution(
        self,
    ) -> None:
        proposal = self.proposals["proposals"][0]
        assert isinstance(proposal, dict)
        proposal["evidenceBasis"] = "source-reconciliation"
        proposal["executionEligibility"]["evidenceBasis"] = "source-reconciliation"
        proposal["licensedClaims"] = [
            {
                "kind": "source-method-match",
                "testName": "Example.Tests.Flaky",
                "file": "tests/Example.Tests/FlakyTests.cs",
                "line": 42,
                "quarantineIssueUrls": [],
            },
            {
                "kind": "no-quarantine-link-to-current-issue",
                "issueUrl": "https://github.com/radical/aspire/issues/1",
            },
        ]
        proposal["sourceEvidenceFingerprint"].update(
            {
                "sourceRevision": "a" * 40,
                "sourceTreeDigest": "sha256:" + "b" * 64,
                "inspectorTreeDigest": "sha256:" + "c" * 64,
                "findingDigest": "sha256:" + "d" * 64,
            }
        )
        self._write_proposals()
        grant = self._generate(action_ids=[self.comment_action_id])
        self.output_path.write_text(json.dumps(grant), encoding="utf-8")

        with (
            patch(
                "ci_shepherd.authorization.current_quarantine_source_fingerprint",
                return_value={
                    "sourceRevision": "e" * 40,
                    "sourceTreeDigest": "sha256:" + "b" * 64,
                    "inspectorTreeDigest": "sha256:" + "c" * 64,
                },
            ),
            self.assertRaisesRegex(
                AuthorizationError,
                "evidence is unavailable or changed before execution",
            ),
        ):
            load_authorized_execution(
                self.proposals_path,
                self.output_path,
                state_dir=self.state_dir,
                action_id=self.comment_action_id,
                source_checkout_path=self.scratch,
                now=datetime(2026, 8, 29, 20, 5, tzinfo=UTC),
            )

    def test_fork_grant_authorizes_exact_copilot_assignment(self) -> None:
        proposal = self.proposals["proposals"][0]
        assert isinstance(proposal, dict)
        proposal.pop("body")
        proposal.update(
            {
                "operation": "assign-copilot",
                "targetRepository": "radical/aspire",
                "baseBranch": "main",
                "customInstructions": (
                    "Fix the exact quarantined test and add regression coverage."
                ),
                "model": "",
            }
        )
        self.proposals["proposals"] = [proposal]
        self._write_proposals()

        grant = self._generate(action_ids=[self.comment_action_id])
        self.output_path.write_text(json.dumps(grant), encoding="utf-8")
        authorized = load_authorized_execution(
            self.proposals_path,
            self.output_path,
            state_dir=self.state_dir,
            action_id=self.comment_action_id,
            now=datetime(2026, 8, 29, 20, 5, tzinfo=UTC),
        )

        self.assertEqual(["assign-copilot"], grant["allowedOperations"])
        self.assertEqual("assign-copilot", authorized.proposal["operation"])

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
                "evidenceBasis": "ci-occurrence",
                "idempotencyKey": "issue:2:relabel",
                "body": (
                    "[automated] Relabeled.\n\n"
                    "<!-- ci-shepherd:idempotency-key=issue:2:relabel -->"
                ),
                "evidenceIds": ["issue:2"],
                "expectedIssueState": "closed",
                "executionEligibility": {
                    "eligible": True,
                    "evidenceBasis": "ci-occurrence",
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
            "evidenceBasis": "ci-occurrence",
            "ciLabels": [],
            "occurrenceCount": 1,
            "collectionComplete": True,
            "unavailableEvidenceIds": [],
            "untrustedReferenceEvidenceIds": [],
            "blockingReasons": ["missing-ci-label"],
        }
        close_proposal["executionEligibility"] = {
            "eligible": False,
            "evidenceBasis": "ci-occurrence",
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
            "evidenceBasis": "ci-occurrence",
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

    def test_production_comment_pilot_round_trips_with_two_explicit_opt_ins(
        self,
    ) -> None:
        self._use_production_repository()
        self._write_proposals()
        selection_bytes = self._write_comment_selection([self.comment_action_id])

        grant = self._generate(
            action_ids=[self.comment_action_id],
            comment_selection_path=self.comment_selection_path,
            allow_production_comment_pilot=True,
        )
        self.assertTrue(grant["productionCommentPilot"])
        self.assertEqual(
            f"sha256:{hashlib.sha256(selection_bytes).hexdigest()}",
            grant["commentSelectionDigest"],
        )
        self.assertEqual(
            {
                "maxMutationAttempts": 1,
                "maxChains": 1,
                "maxRunningCopilotTasks": 2,
                "maxCopilotStartsPerRolling24h": 3,
                "maxOpenDelegatedPullRequests": 5,
                "maxRepositoryRunningCopilotTasks": 100,
            },
            grant["budget"],
        )
        self.output_path.write_text(json.dumps(grant), encoding="utf-8")

        authorized = load_authorized_execution(
            self.proposals_path,
            self.output_path,
            state_dir=self.state_dir,
            action_id=self.comment_action_id,
            comment_selection_path=self.comment_selection_path,
            allow_production_comment_pilot=True,
            now=datetime(2026, 8, 29, 20, 5, tzinfo=UTC),
        )

        self.assertTrue(authorized.grant.production_comment_pilot)
        self.assertEqual(self.comment_action_id, authorized.proposal["actionId"])

    def test_production_execution_rejects_changed_comment_selection(self) -> None:
        self._use_production_repository()
        self._write_proposals()
        self._write_comment_selection([self.comment_action_id])
        grant = self._generate(
            action_ids=[self.comment_action_id],
            comment_selection_path=self.comment_selection_path,
            allow_production_comment_pilot=True,
        )
        self.output_path.write_text(json.dumps(grant), encoding="utf-8")
        self.comment_selection_path.write_text("{}\n", encoding="utf-8")

        with self.assertRaisesRegex(
            AuthorizationError,
            "commentSelectionDigest does not match",
        ):
            load_authorized_execution(
                self.proposals_path,
                self.output_path,
                state_dir=self.state_dir,
                action_id=self.comment_action_id,
                comment_selection_path=self.comment_selection_path,
                allow_production_comment_pilot=True,
                now=datetime(2026, 8, 29, 20, 5, tzinfo=UTC),
            )

    def test_production_delegation_pilot_allows_one_capped_assignment(self) -> None:
        self._use_production_repository()
        proposal = self.proposals["proposals"][0]
        assert isinstance(proposal, dict)
        proposal["operation"] = "assign-copilot"
        proposal["targetRepository"] = "microsoft/aspire"
        proposal["baseBranch"] = "main"
        proposal["customInstructions"] = "Fix issue #1 and open a draft PR."
        proposal["model"] = ""
        proposal.pop("body")
        proposal.pop("commentId")
        proposal.pop("sourceCommentFingerprint")
        self.proposals["proposals"] = [proposal]
        self._write_proposals()

        grant = self._generate(
            action_ids=[self.comment_action_id],
            max_running_copilot_tasks=1,
            max_copilot_starts_per_rolling_24h=1,
            max_open_delegated_prs=1,
            allow_production_delegation_pilot=True,
        )
        self.output_path.write_text(json.dumps(grant), encoding="utf-8")
        authorized = load_authorized_execution(
            self.proposals_path,
            self.output_path,
            state_dir=self.state_dir,
            action_id=self.comment_action_id,
            allow_production_delegation_pilot=True,
            now=datetime(2026, 8, 29, 20, 5, tzinfo=UTC),
        )

        self.assertFalse(grant["productionCommentPilot"])
        self.assertTrue(grant["productionDelegationPilot"])
        self.assertEqual(["assign-copilot"], grant["allowedOperations"])
        self.assertTrue(authorized.grant.production_delegation_pilot)

    def test_production_delegation_pilot_rejects_broader_capacity(self) -> None:
        self._use_production_repository()
        proposal = self.proposals["proposals"][0]
        assert isinstance(proposal, dict)
        proposal.update(
            {
                "operation": "assign-copilot",
                "targetRepository": "microsoft/aspire",
                "baseBranch": "main",
                "customInstructions": "Fix issue #1 and open a draft PR.",
                "model": "",
            }
        )
        proposal.pop("body")
        proposal.pop("commentId")
        proposal.pop("sourceCommentFingerprint")
        self.proposals["proposals"] = [proposal]
        self._write_proposals()

        with self.assertRaisesRegex(AuthorizationError, "must all equal one"):
            self._generate(
                action_ids=[self.comment_action_id],
                allow_production_delegation_pilot=True,
            )

    def test_production_delegation_steady_state_uses_policy_capacity(self) -> None:
        self._use_production_repository()
        proposal = self.proposals["proposals"][0]
        assert isinstance(proposal, dict)
        proposal.update(
            {
                "operation": "assign-copilot",
                "targetRepository": "microsoft/aspire",
                "baseBranch": "main",
                "customInstructions": "Fix issue #1 and open a draft PR.",
                "model": "",
            }
        )
        proposal.pop("body")
        proposal.pop("commentId")
        proposal.pop("sourceCommentFingerprint")
        self.proposals["proposals"] = [proposal]
        self._write_proposals()

        grant = self._generate(
            action_ids=[self.comment_action_id],
            max_running_copilot_tasks=10,
            max_copilot_starts_per_rolling_24h=5,
            max_open_delegated_prs=10,
            max_repository_running_copilot_tasks=100,
            allow_production_delegation_steady_state=True,
        )
        self.output_path.write_text(json.dumps(grant), encoding="utf-8")
        authorized = load_authorized_execution(
            self.proposals_path,
            self.output_path,
            state_dir=self.state_dir,
            action_id=self.comment_action_id,
            allow_production_delegation_steady_state=True,
            now=datetime(2026, 8, 29, 20, 5, tzinfo=UTC),
        )

        self.assertTrue(grant["productionDelegationSteadyState"])
        self.assertIsNotNone(grant["capacityPolicyDigest"])
        self.assertTrue(authorized.grant.production_delegation_steady_state)
        self.assertEqual(10, authorized.grant.budget.max_running_copilot_tasks)
        self.assertEqual(
            5,
            authorized.grant.budget.max_copilot_starts_per_rolling_24h,
        )

    def test_production_delegation_steady_state_rejects_policy_drift(self) -> None:
        self._use_production_repository()
        proposal = self.proposals["proposals"][0]
        assert isinstance(proposal, dict)
        proposal.update(
            {
                "operation": "assign-copilot",
                "targetRepository": "microsoft/aspire",
                "baseBranch": "main",
                "customInstructions": "Fix issue #1 and open a draft PR.",
                "model": "",
            }
        )
        proposal.pop("body")
        proposal.pop("commentId")
        proposal.pop("sourceCommentFingerprint")
        self.proposals["proposals"] = [proposal]
        self._write_proposals()
        policy_path = self.scratch / "policy.json"
        policy = {
            "schemaVersion": 1,
            "repository": "microsoft/aspire",
            "maxActionsPerGrant": 5,
            "capacity": {
                "maxRunningCopilotTasks": 10,
                "maxCopilotStartsPerRolling24h": 5,
                "maxOpenDelegatedPullRequests": 10,
                "maxRepositoryRunningCopilotTasks": 100,
            },
        }
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        grant = self._generate(
            action_ids=[self.comment_action_id],
            max_running_copilot_tasks=10,
            max_copilot_starts_per_rolling_24h=5,
            max_open_delegated_prs=10,
            max_repository_running_copilot_tasks=100,
            allow_production_delegation_steady_state=True,
            production_delegation_policy_path=policy_path,
        )
        self.output_path.write_text(json.dumps(grant), encoding="utf-8")
        policy["capacity"]["maxRunningCopilotTasks"] = 9
        policy_path.write_text(json.dumps(policy), encoding="utf-8")

        with self.assertRaisesRegex(AuthorizationError, "changed after grant"):
            load_authorized_execution(
                self.proposals_path,
                self.output_path,
                state_dir=self.state_dir,
                action_id=self.comment_action_id,
                allow_production_delegation_steady_state=True,
                production_delegation_policy_path=policy_path,
                now=datetime(2026, 8, 29, 20, 5, tzinfo=UTC),
            )

    def test_production_comment_pilot_accepts_finalized_round_zero_snapshot(
        self,
    ) -> None:
        self._use_production_repository()
        self.proposals["snapshotId"] = self.proposals["snapshotId"].removesuffix(
            ":r1"
        )
        self.proposals["productionPilotCapability"] = {
            "schemaVersion": 1,
            "evidenceRound": 0,
        }
        self._write_proposals()

        grant = self._generate(
            action_ids=[self.comment_action_id],
            allow_production_comment_pilot=True,
        )

        self.assertEqual(self.proposals["snapshotId"], grant["snapshotId"])

    def test_production_grant_accepts_managed_coverage_capability(self) -> None:
        self._use_production_repository()
        capability = self.proposals["productionPilotCapability"]
        assert isinstance(capability, dict)
        capability["managedItemCoverage"] = {
            "schemaVersion": 1,
            "valid": True,
            "blockers": [],
        }
        self._write_proposals()

        grant = self._generate(
            action_ids=[self.comment_action_id],
            allow_production_comment_pilot=True,
        )

        self.assertEqual(self.proposals["snapshotId"], grant["snapshotId"])

    def test_production_comment_pilot_allows_up_to_five_actions(
        self,
    ) -> None:
        self._use_production_repository()
        second_action_id = self._add_comment_proposal(2)
        third_action_id = self._add_comment_proposal(3)
        self._write_proposals()

        grant = self._generate(
            action_ids=[
                self.comment_action_id,
                second_action_id,
                third_action_id,
            ],
            allow_production_comment_pilot=True,
        )

        self.assertEqual(
            {"maxMutationAttempts": 3, "maxChains": 3},
            {
                key: grant["budget"][key]
                for key in ("maxMutationAttempts", "maxChains")
            },
        )

    def test_production_comment_grant_rejects_reordered_selection(self) -> None:
        self._use_production_repository()
        second_action_id = self._add_comment_proposal(2)
        self._write_proposals()
        self._write_comment_selection(
            [self.comment_action_id, second_action_id]
        )

        with self.assertRaisesRegex(
            AuthorizationError,
            "exactly match the ordered comment selection",
        ):
            self._generate(
                action_ids=[second_action_id, self.comment_action_id],
                comment_selection_path=self.comment_selection_path,
                allow_production_comment_pilot=True,
            )

    def test_production_comment_grant_recomputes_deterministic_selection(
        self,
    ) -> None:
        self._use_production_repository()
        second_action_id = self._add_comment_proposal(2)
        self._write_proposals()
        self._write_comment_selection(
            [second_action_id, self.comment_action_id]
        )

        with self.assertRaisesRegex(
            AuthorizationError,
            "deterministically recomputed selection",
        ):
            self._generate(
                action_ids=[second_action_id, self.comment_action_id],
                comment_selection_path=self.comment_selection_path,
                allow_production_comment_pilot=True,
            )

    def test_production_comment_pilot_rejects_more_than_five_actions(self) -> None:
        self._use_production_repository()
        action_ids = [self.comment_action_id]
        action_ids.extend(self._add_comment_proposal(number) for number in range(2, 7))
        self._write_proposals()

        with self.assertRaisesRegex(
            AuthorizationError,
            "deterministically recomputed selection",
        ):
            self._generate(
                action_ids=action_ids,
                allow_production_comment_pilot=True,
            )

    def test_production_comment_pilot_allows_one_comment_creation(self) -> None:
        self._use_production_repository()
        proposal = self.proposals["proposals"][0]
        proposal["operation"] = "create-comment"
        proposal.pop("commentId")
        proposal.pop("sourceCommentFingerprint")
        self._write_proposals()

        grant = self._generate(
            action_ids=[self.comment_action_id],
            allow_production_comment_pilot=True,
        )
        self.output_path.write_text(json.dumps(grant), encoding="utf-8")
        authorized = load_authorized_execution(
            self.proposals_path,
            self.output_path,
            state_dir=self.state_dir,
            action_id=self.comment_action_id,
            allow_production_comment_pilot=True,
            now=datetime(2026, 8, 29, 20, 5, tzinfo=UTC),
        )

        self.assertEqual(["create-comment"], grant["allowedOperations"])
        self.assertEqual("create-comment", authorized.proposal["operation"])

    def test_production_comment_pilot_requires_finalized_cycle_capability(
        self,
    ) -> None:
        self._use_production_repository()
        self.proposals["snapshotId"] = self.proposals["snapshotId"].removesuffix(
            ":r1"
        )
        self.proposals.pop("productionPilotCapability")
        self._write_proposals()

        with self.assertRaisesRegex(
            AuthorizationError,
            "finalized-cycle capability",
        ):
            self._generate(
                action_ids=[self.comment_action_id],
                allow_production_comment_pilot=True,
            )

    def test_production_comment_pilot_rejects_capability_snapshot_round_mismatch(
        self,
    ) -> None:
        self._use_production_repository()
        self.proposals["snapshotId"] = self.proposals["snapshotId"].removesuffix(
            ":r1"
        )
        self._write_proposals()

        with self.assertRaisesRegex(
            AuthorizationError,
            "does not match its snapshot round",
        ):
            self._generate(
                action_ids=[self.comment_action_id],
                allow_production_comment_pilot=True,
            )

    def test_production_comment_pilot_rejects_round_zero_capability_for_round_one(
        self,
    ) -> None:
        self._use_production_repository()
        self.proposals["productionPilotCapability"]["evidenceRound"] = 0
        self._write_proposals()

        with self.assertRaisesRegex(
            AuthorizationError,
            "does not match its snapshot round",
        ):
            self._generate(
                action_ids=[self.comment_action_id],
                allow_production_comment_pilot=True,
            )

    def test_production_execution_revalidates_finalized_cycle_capability(
        self,
    ) -> None:
        self._use_production_repository()
        self._write_proposals()
        grant = self._generate(
            action_ids=[self.comment_action_id],
            allow_production_comment_pilot=True,
        )
        self.proposals.pop("productionPilotCapability")
        proposal_bytes = self._write_proposals()
        grant["proposalsDigest"] = (
            f"sha256:{hashlib.sha256(proposal_bytes).hexdigest()}"
        )
        selection_bytes = self._write_comment_selection([self.comment_action_id])
        grant["commentSelectionDigest"] = (
            f"sha256:{hashlib.sha256(selection_bytes).hexdigest()}"
        )
        self.output_path.write_text(json.dumps(grant), encoding="utf-8")

        with self.assertRaisesRegex(
            AuthorizationError,
            "finalized-cycle capability",
        ):
            load_authorized_execution(
                self.proposals_path,
                self.output_path,
                state_dir=self.state_dir,
                action_id=self.comment_action_id,
                allow_production_comment_pilot=True,
                now=datetime(2026, 8, 29, 20, 5, tzinfo=UTC),
            )

    def test_production_comment_pilot_allows_time_for_collection_and_review(
        self,
    ) -> None:
        self._use_production_repository()
        self._write_proposals()
        self._write_comment_selection([self.comment_action_id])

        grant = generate_authorization_grant(
            self.proposals_path,
            action_ids=[self.comment_action_id],
            state_dir=self.state_dir,
            comment_selection_path=self.comment_selection_path,
            allow_production_comment_pilot=True,
            now=datetime(2026, 8, 29, 20, 30, tzinfo=UTC),
            grant_id="grant:test",
        )

        self.assertEqual("2026-08-29T20:45:00Z", grant["expiresAtUtc"])

    def test_production_comment_pilot_rejects_stale_expanded_snapshot(self) -> None:
        self._use_production_repository()
        self._write_proposals()
        self._write_comment_selection([self.comment_action_id])

        with self.assertRaisesRegex(
            AuthorizationError,
            "less than 45 minutes old",
        ):
            generate_authorization_grant(
                self.proposals_path,
                action_ids=[self.comment_action_id],
                state_dir=self.state_dir,
                comment_selection_path=self.comment_selection_path,
                allow_production_comment_pilot=True,
                now=datetime(2026, 8, 29, 20, 46, tzinfo=UTC),
                grant_id="grant:test",
            )

    def test_production_comment_pilot_expires_with_snapshot_freshness(self) -> None:
        self._use_production_repository()
        self._write_proposals()
        self._write_comment_selection([self.comment_action_id])

        grant = generate_authorization_grant(
            self.proposals_path,
            action_ids=[self.comment_action_id],
            state_dir=self.state_dir,
            comment_selection_path=self.comment_selection_path,
            ttl_minutes=15,
            allow_production_comment_pilot=True,
            now=datetime(2026, 8, 29, 20, 40, tzinfo=UTC),
            grant_id="grant:test",
        )

        self.assertEqual("2026-08-29T20:45:00Z", grant["expiresAtUtc"])

    def test_production_pilot_grant_requires_execution_confirmation(self) -> None:
        self._use_production_repository()
        self._write_proposals()
        grant = self._generate(
            action_ids=[self.comment_action_id],
            allow_production_comment_pilot=True,
        )
        self.output_path.write_text(json.dumps(grant), encoding="utf-8")

        with self.assertRaisesRegex(AuthorizationError, "protected"):
            load_authorized_execution(
                self.proposals_path,
                self.output_path,
                state_dir=self.state_dir,
                action_id=self.comment_action_id,
                now=datetime(2026, 8, 29, 20, 5, tzinfo=UTC),
            )

    def test_production_comment_pilot_rejects_broader_generation(self) -> None:
        self._use_production_repository()
        original = json.loads(json.dumps(self.proposals))

        cases = [
            (
                "comment plus closure",
                [self.comment_action_id, self.close_action_id],
                {},
                "deterministically recomputed selection",
            ),
            (
                "closure",
                [self.comment_action_id],
                {"operation": "close-issue"},
                "deterministically recomputed selection",
            ),
            (
                "long lifetime",
                [self.comment_action_id],
                {"ttl_minutes": 16},
                "at most 15 minutes",
            ),
            (
                "suppression override",
                [self.comment_action_id],
                {
                    "override_suppression_for_action_ids": [
                        self.comment_action_id
                    ]
                },
                "cannot override suppression",
            ),
        ]
        for name, action_ids, changes, message in cases:
            with self.subTest(name=name):
                self.proposals = json.loads(json.dumps(original))
                generation_options = dict(changes)
                operation = generation_options.pop("operation", None)
                if operation is not None:
                    proposals = self.proposals["proposals"]
                    assert isinstance(proposals, list)
                    proposal = proposals[0]
                    assert isinstance(proposal, dict)
                    proposal["operation"] = operation
                    proposal.pop("body")
                    proposal.pop("commentId")
                    proposal.pop("sourceCommentFingerprint")
                    proposal["closeReason"] = "not_planned"
                self._write_proposals()
                with self.assertRaisesRegex(AuthorizationError, message):
                    self._generate(
                        action_ids=action_ids,
                        allow_production_comment_pilot=True,
                        **generation_options,
                    )

    def test_production_comment_pilot_rejects_broadened_grant(self) -> None:
        self._use_production_repository()
        self._write_proposals()
        valid_grant = self._generate(
            action_ids=[self.comment_action_id],
            allow_production_comment_pilot=True,
        )
        cases = [
            (
                "action",
                "allowedActionIds",
                [self.comment_action_id, self.close_action_id],
                "ordered comment selection",
            ),
            (
                "operation",
                "allowedOperations",
                ["edit-comment", "create-comment"],
                "comment creation or editing only",
            ),
            (
                "target",
                "allowedTargets",
                [
                    {"kind": "issue", "number": 1},
                    {"kind": "issue", "number": 2},
                ],
                "one issue target per action",
            ),
            (
                "suppression",
                "overrideSuppressionForActionIds",
                [self.comment_action_id],
                "cannot override suppression",
            ),
            (
                "budget",
                "budget",
                {
                    "maxMutationAttempts": 2,
                    "maxChains": 1,
                    "maxRunningCopilotTasks": 2,
                    "maxCopilotStartsPerRolling24h": 3,
                    "maxOpenDelegatedPullRequests": 5,
                    "maxRepositoryRunningCopilotTasks": 100,
                },
                "exact action count",
            ),
            (
                "lifetime",
                "expiresAtUtc",
                "2026-08-29T20:46:00Z",
                "outlives its source snapshot",
            ),
        ]
        for name, key, value, message in cases:
            with self.subTest(name=name):
                grant = json.loads(json.dumps(valid_grant))
                grant[key] = value
                self.output_path.write_text(json.dumps(grant), encoding="utf-8")

                with self.assertRaisesRegex(
                    AuthorizationError,
                    message,
                ):
                    load_authorized_execution(
                        self.proposals_path,
                        self.output_path,
                        state_dir=self.state_dir,
                        action_id=self.comment_action_id,
                        allow_production_comment_pilot=True,
                        now=datetime(2026, 8, 29, 20, 5, tzinfo=UTC),
                    )

    def test_production_comment_pilot_allows_one_edit(self) -> None:
        self._use_production_repository()
        proposals = self.proposals["proposals"]
        assert isinstance(proposals, list)
        proposal = proposals[0]
        assert isinstance(proposal, dict)
        proposal["operation"] = "edit-comment"
        proposal["commentId"] = 123
        proposal["sourceCommentFingerprint"] = {"bodySha256": "0" * 64}
        self._write_proposals()

        grant = self._generate(
            action_ids=[self.comment_action_id],
            allow_production_comment_pilot=True,
        )

        self.assertEqual(["edit-comment"], grant["allowedOperations"])

    def test_production_comment_pilot_flag_is_rejected_for_forks(self) -> None:
        self._write_proposals()

        with self.assertRaisesRegex(
            AuthorizationError,
            "only valid for microsoft/aspire",
        ):
            self._generate(
                action_ids=[self.comment_action_id],
                allow_production_comment_pilot=True,
            )

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


def _no_durable_intent(_action_id: str) -> bool:
    return False


def _rfc3339(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _policy_document(
    *,
    repository: str,
    revision: int,
    enabled_classes: frozenset[str],
    created_at_utc: datetime,
    expires_at_utc: datetime,
    status: str = "active",
    replaces: str | None = None,
    denied_action_ids: frozenset[str] = frozenset(),
    denied_targets: frozenset[str] = frozenset(),
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "repository": repository,
        "revisionId": f"policy:{revision}",
        "revision": revision,
        "status": status,
        "createdAtUtc": _rfc3339(created_at_utc),
        "expiresAtUtc": _rfc3339(expires_at_utc),
        "actor": "github:radical",
        "replacesRevisionId": replaces,
        "operationClasses": {
            name: {
                "enabled": name in enabled_classes,
                "maxPerRun": DEFAULT_CAPS[name]["maxPerRun"],
                "maxRolling24h": DEFAULT_CAPS[name]["maxRolling24h"],
            }
            for name in OPERATION_CLASSES
        },
        "deniedActionIds": sorted(denied_action_ids),
        "deniedTargets": sorted(denied_targets),
    }


class AutonomousPolicyGrantTests(unittest.TestCase):
    """Tests for Task 4's autonomous one-action child grants: minting and
    reloading a grant bound to the frozen Task 3 policy-selection artifact
    plus the semantic policy-or-decision license source that selected it.

    The fixture reuses the same two-step comment/close dependency chain as
    `GenerateAuthorizationGrantTests`, pre-switched to the production
    repository microsoft/aspire (the only repository autonomous policy
    grants are valid for), backed by a real `CoordinatorStateStore` rooted
    at the same `state_dir` the grant itself is bound to. Selections are
    built through the real `build_policy_selection` selector -- never a
    hand-rolled stand-in -- so these tests stay honest about the selection
    artifact shape Task 3 actually produces.
    """

    def setUp(self) -> None:
        self.scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch.mkdir(parents=True)
        self.state_dir = (self.scratch / "state").resolve()
        self.proposals_path = self.scratch / "action-proposals.json"
        self.policy_selection_path = self.scratch / "policy-selection.json"
        self.output_path = self.scratch / "authorization-grant.json"
        self.repository = "microsoft/aspire"
        self.now = datetime(2026, 8, 29, 20, 0, tzinfo=UTC)
        self.comment_action_id = (
            "snapshot:microsoft/aspire:2026-08-29T20:00:00Z:"
            "issue:1:watch-comment"
        )
        self.close_action_id = (
            "snapshot:microsoft/aspire:2026-08-29T20:00:00Z:"
            "issue:1:review-close"
        )
        self.proposals: dict[str, object] = {
            "schemaVersion": 2,
            "repository": self.repository,
            "snapshotId": "snapshot:microsoft/aspire:2026-08-29T20:00:00Z:r1",
            "shepherdAuthor": "radical",
            "generatedAtUtc": "2026-08-29T20:00:00Z",
            "proposalTtlHours": 24,
            "maxProposalsPerIssue": 2,
            "productionPilotCapability": {
                "schemaVersion": 1,
                "evidenceRound": 1,
            },
            "executionEligibility": {"status": "eligible", "violations": []},
            "proposals": [
                {
                    "actionId": self.comment_action_id,
                    "issueNumber": 1,
                    "issueUrl": "https://github.com/microsoft/aspire/issues/1",
                    "operation": "edit-comment",
                    "commentId": 1001,
                    "sourceCommentFingerprint": {"bodySha256": "0" * 64},
                    "evidenceBasis": "ci-occurrence",
                    "idempotencyKey": "issue:1:status",
                    "body": (
                        "[automated] Watching.\n\n"
                        "<!-- ci-shepherd:idempotency-key=issue:1:status -->"
                    ),
                    "evidenceIds": ["issue:1"],
                    "expectedIssueState": "open",
                    "executionEligibility": {
                        "eligible": True,
                        "evidenceBasis": "ci-occurrence",
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
                    "issueUrl": "https://github.com/microsoft/aspire/issues/1",
                    "operation": "close-issue",
                    "evidenceBasis": "ci-occurrence",
                    "idempotencyKey": "issue:1:close",
                    "closeReason": "not_planned",
                    "evidenceIds": ["issue:1"],
                    "expectedIssueState": "open",
                    "executionEligibility": {
                        "eligible": True,
                        "evidenceBasis": "ci-occurrence",
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
        self._write_proposals()
        self.store = CoordinatorStateStore(
            self.state_dir, durable_intent_reader=_no_durable_intent
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)

    # -- fixture plumbing -------------------------------------------------

    def _write_proposals(self) -> bytes:
        proposal_bytes = (
            json.dumps(self.proposals, indent=2, sort_keys=True) + "\n"
        ).encode()
        self.proposals_path.write_bytes(proposal_bytes)
        return proposal_bytes

    def _append_policy(
        self,
        *,
        revision: int,
        enabled_classes: frozenset[str],
        replaces: str | None = None,
        status: str = "active",
        created_at_utc: datetime | None = None,
        expires_at_utc: datetime | None = None,
        denied_action_ids: frozenset[str] = frozenset(),
        denied_targets: frozenset[str] = frozenset(),
    ) -> None:
        expected_revision = self.store.projection(
            self.repository, now=self.now
        )["stateRevision"]
        self.store.append_policy_revision(
            repository=self.repository,
            expected_revision=expected_revision,
            document=_policy_document(
                repository=self.repository,
                revision=revision,
                status=status,
                replaces=replaces,
                created_at_utc=created_at_utc or (self.now - timedelta(days=1)),
                expires_at_utc=expires_at_utc or (self.now + timedelta(days=30)),
                enabled_classes=enabled_classes,
                denied_action_ids=denied_action_ids,
                denied_targets=denied_targets,
            ),
        )

    def _flip_current_policy_status_in_ledger(self, *, status: str) -> None:
        """Rewrite the ledger's latest policy event in place, keeping its
        exact revisionId/revision but changing only its status.

        `append_policy_revision` (the only production write path) always
        mints a *new* revisionId when a policy is replaced -- including when
        an operator pauses or revokes it, per this module's own "pauses and
        revocations are simply new revisions" contract (see
        `coordinator_state.py`'s module docstring). That means a black-box
        test going through the public API can never produce a policy whose
        `revisionId` still equals a grant's `licenseSource` but whose
        `status` is no longer "active" -- `_resolve_autonomous_license_source`
        would always reject such a grant via the `revision_id != license_
        source` half of its check, never actually exercising the sibling
        `status != "active"` half. Appending a hand-built ledger line here
        (bypassing `CoordinatorStateStore` entirely) isolates that status
        check on its own, independent of revision identity.
        """
        projection = self.store.projection(self.repository, now=self.now)
        effective_policy = dict(projection["effectivePolicy"])
        policy_digest = effective_policy.pop("policyDigest")
        effective_policy["status"] = status
        ledger_path = self.state_dir / "coordinator" / "policy-events.jsonl"
        existing_line_count = len(
            ledger_path.read_text(encoding="utf-8").splitlines()
        )
        event = {
            "schemaVersion": 1,
            "stateRevision": existing_line_count + 1,
            "eventType": "policy",
            "repository": self.repository,
            "recordedAtUtc": _rfc3339(self.now),
            "policy": effective_policy,
            "policyDigest": policy_digest,
        }
        with ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")

    def _append_decision(
        self,
        *,
        action_id: str,
        decision: str,
        now: datetime | None = None,
    ) -> None:
        expected_revision = self.store.projection(
            self.repository, now=self.now
        )["stateRevision"]
        self.store.append_exact_decision(
            repository=self.repository,
            expected_revision=expected_revision,
            proposals_path=self.proposals_path,
            action_id=action_id,
            decision=decision,
            actor="github:radical",
            now=now or self.now,
        )

    def _build_and_write_selection(
        self, *, action_events: list = (), now: datetime | None = None
    ) -> dict[str, object]:
        now = now or self.now
        projection = self.store.projection(self.repository, now=now)
        selection = ps.build_policy_selection(
            self.proposals,
            run_id=f"cycle:{self.proposals['snapshotId']}",
            policy_projection=projection,
            action_events=list(action_events),
            now=now,
        )
        selection_bytes = (
            json.dumps(selection, indent=2, sort_keys=True) + "\n"
        ).encode()
        self.policy_selection_path.write_bytes(selection_bytes)
        return selection

    def _generate(self, **kwargs):
        kwargs.setdefault("now", self.now)
        kwargs.setdefault("grant_id", "grant:fixed-for-test")
        return generate_authorization_grant(
            self.proposals_path,
            state_dir=self.state_dir,
            **kwargs,
        )

    def _mint(self, action_id: str, **kwargs) -> dict[str, object]:
        grant = self._generate(
            action_ids=[action_id],
            allow_autonomous_policy=True,
            policy_selection_path=self.policy_selection_path,
            policy_action_id=action_id,
            **kwargs,
        )
        self.output_path.write_text(json.dumps(grant), encoding="utf-8")
        return grant

    def _load(self, action_id: str, **kwargs):
        kwargs.setdefault("now", self.now)
        return load_authorized_execution(
            self.proposals_path,
            self.output_path,
            state_dir=self.state_dir,
            action_id=action_id,
            allow_autonomous_policy=True,
            policy_selection_path=self.policy_selection_path,
            **kwargs,
        )

    # -- generation-time validation ---------------------------------------

    def test_requires_exactly_one_action_id(self) -> None:
        with self.assertRaisesRegex(AuthorizationError, "exactly one actionId"):
            self._generate(
                action_ids=[self.comment_action_id, self.close_action_id],
                allow_autonomous_policy=True,
                policy_selection_path=self.policy_selection_path,
                policy_action_id=self.comment_action_id,
            )

    def test_rejects_action_not_in_selection(self) -> None:
        self._append_policy(
            revision=1, enabled_classes=frozenset({"close-issue"})
        )
        self._build_and_write_selection()

        with self.assertRaisesRegex(
            AuthorizationError, "does not select actionId"
        ):
            self._mint(self.comment_action_id)

    def test_grant_includes_expected_license_shape(self) -> None:
        self._append_policy(
            revision=1, enabled_classes=frozenset({"edit-comment"})
        )
        self._build_and_write_selection()

        grant = self._mint(self.comment_action_id)

        license_payload = grant["autonomousPolicyLicense"]
        self.assertEqual(1, license_payload["schemaVersion"])
        self.assertEqual(
            f"cycle:{self.proposals['snapshotId']}",
            license_payload["runId"],
        )
        self.assertEqual("edit-comment", license_payload["operationClass"])
        self.assertRegex(
            license_payload["selectionDigest"], r"^sha256:[0-9a-f]{64}$"
        )
        self.assertIsInstance(license_payload["selectionStateRevision"], int)
        self.assertEqual("policy:1", license_payload["licenseSource"])
        self.assertEqual([], license_payload["satisfiedPrerequisites"])
        self.assertEqual(
            license_payload["selectionDigest"], grant["policySelectionDigest"]
        )

        execution = self._load(self.comment_action_id)
        self.assertEqual(self.comment_action_id, execution.proposal["actionId"])

    # -- TTL = min(15 minutes, proposal expiry, policy/decision expiry, ---
    # -- production snapshot freshness) ------------------------------------

    def test_ttl_never_exceeds_fifteen_minutes_by_default(self) -> None:
        self._append_policy(
            revision=1, enabled_classes=frozenset({"edit-comment"})
        )
        self._build_and_write_selection()

        grant = self._mint(self.comment_action_id)

        self.assertEqual("2026-08-29T20:15:00Z", grant["expiresAtUtc"])

    def test_ttl_over_fifteen_minutes_rejected_at_generation(self) -> None:
        self._append_policy(
            revision=1, enabled_classes=frozenset({"edit-comment"})
        )
        self._build_and_write_selection()

        with self.assertRaisesRegex(
            AuthorizationError, "may live for at most 15 minutes"
        ):
            self._mint(self.comment_action_id, ttl_minutes=20)

    def test_ttl_capped_by_policy_expiry(self) -> None:
        self._append_policy(
            revision=1,
            enabled_classes=frozenset({"edit-comment"}),
            expires_at_utc=self.now + timedelta(minutes=5),
        )
        self._build_and_write_selection()

        grant = self._mint(self.comment_action_id)

        self.assertEqual("2026-08-29T20:00:00Z", grant["issuedAtUtc"])
        self.assertEqual("2026-08-29T20:05:00Z", grant["expiresAtUtc"])

    def test_ttl_capped_by_proposal_expiry(self) -> None:
        # Isolate the proposal-expiry term of the TTL formula: skew
        # generatedAtUtc 20 minutes before the snapshot's own embedded
        # collection time, so the minimum allowed proposalTtlHours (a whole
        # hour) expires before the 45-minute production snapshot freshness
        # window would otherwise be the tighter cap.
        self.proposals["snapshotId"] = (
            "snapshot:microsoft/aspire:2026-08-29T20:20:00Z:r1"
        )
        self.proposals["generatedAtUtc"] = "2026-08-29T20:00:00Z"
        self.proposals["proposalTtlHours"] = 1
        self._write_proposals()
        self._append_policy(
            revision=1, enabled_classes=frozenset({"edit-comment"})
        )
        generate_now = datetime(2026, 8, 29, 20, 50, tzinfo=UTC)
        self._build_and_write_selection(now=generate_now)

        grant = self._mint(self.comment_action_id, now=generate_now)

        self.assertEqual("2026-08-29T20:50:00Z", grant["issuedAtUtc"])
        self.assertEqual("2026-08-29T21:00:00Z", grant["expiresAtUtc"])

    def test_ttl_capped_by_production_snapshot_freshness(self) -> None:
        # Isolate the freshness term: mint 35 minutes after the snapshot's
        # own embedded collection time (fixture default 20:00). That leaves
        # the 45-minute freshness window closing at 20:45 -- five minutes
        # before the naive 15-minute cap (20:35 + 15m = 20:50) and hours
        # before the default policy (+30 days) and proposal (+24h) expiry
        # terms, so freshness alone must be the binding cap.
        self._append_policy(
            revision=1, enabled_classes=frozenset({"edit-comment"})
        )
        generate_now = datetime(2026, 8, 29, 20, 35, tzinfo=UTC)
        self._build_and_write_selection(now=generate_now)

        grant = self._mint(self.comment_action_id, now=generate_now)

        self.assertEqual("2026-08-29T20:35:00Z", grant["issuedAtUtc"])
        self.assertEqual("2026-08-29T20:45:00Z", grant["expiresAtUtc"])

    def test_rejects_stale_production_snapshot(self) -> None:
        self._append_policy(
            revision=1, enabled_classes=frozenset({"edit-comment"})
        )
        generate_now = datetime(2026, 8, 29, 20, 46, tzinfo=UTC)
        self._build_and_write_selection(now=generate_now)

        with self.assertRaisesRegex(
            AuthorizationError, "less than 45 minutes old"
        ):
            self._mint(self.comment_action_id, now=generate_now)

    def test_rejects_selection_run_id_outside_proposal_cycle_namespace(
        self,
    ) -> None:
        self._append_policy(
            revision=1, enabled_classes=frozenset({"edit-comment"})
        )
        selection = self._build_and_write_selection()
        selection["runId"] = "fresh-budget-namespace"
        self.policy_selection_path.write_text(
            json.dumps(selection, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            AuthorizationError, r"cycle:<proposal snapshotId>"
        ):
            self._mint(self.comment_action_id)

    # -- byte-binding: any change fails before execution --------------------

    def test_changed_selection_bytes_fail_before_execution(self) -> None:
        self._append_policy(
            revision=1, enabled_classes=frozenset({"edit-comment"})
        )
        self._build_and_write_selection()
        self._mint(self.comment_action_id)

        selection = json.loads(
            self.policy_selection_path.read_text(encoding="utf-8")
        )
        selection["runId"] = "tampered-run"
        self.policy_selection_path.write_text(
            json.dumps(selection, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            AuthorizationError, "policySelectionDigest does not match"
        ):
            self._load(self.comment_action_id)

    def test_changed_proposal_bytes_fail_before_execution(self) -> None:
        self._append_policy(
            revision=1, enabled_classes=frozenset({"edit-comment"})
        )
        self._build_and_write_selection()
        self._mint(self.comment_action_id)

        self.proposals["proposals"][0]["body"] += " Edited after grant."
        self._write_proposals()

        with self.assertRaisesRegex(
            AuthorizationError, "proposalsDigest does not match"
        ):
            self._load(self.comment_action_id)

    # -- semantic re-validation: named license only, not global revision ---

    def test_unrelated_coordinator_event_does_not_invalidate_grant(self) -> None:
        self._append_policy(
            revision=1, enabled_classes=frozenset({"edit-comment"})
        )
        self._build_and_write_selection()
        self._mint(self.comment_action_id)

        # An exact decision for a *different* action moves the ledger's
        # global stateRevision forward but must never perturb this grant.
        self._append_decision(
            action_id=self.close_action_id, decision="reject-once"
        )

        execution = self._load(self.comment_action_id)
        self.assertEqual(self.comment_action_id, execution.proposal["actionId"])

    def test_policy_replacement_invalidates_policy_licensed_grant(self) -> None:
        self._append_policy(
            revision=1, enabled_classes=frozenset({"edit-comment"})
        )
        self._build_and_write_selection()
        self._mint(self.comment_action_id)

        self._append_policy(
            revision=2,
            enabled_classes=frozenset({"edit-comment"}),
            replaces="policy:1",
        )

        with self.assertRaisesRegex(
            AuthorizationError,
            "Licensing policy revision is no longer effective",
        ):
            self._load(self.comment_action_id)

    def test_decision_clear_invalidates_decision_licensed_grant(self) -> None:
        # No active policy enables edit-comment: the action can only reach
        # "exact" status via an approve-once exact decision, per
        # build_policy_selection's exact-promotion precedence (an
        # automatically-licensed action never reaches the exact scan).
        self._append_policy(revision=1, enabled_classes=frozenset())
        self._append_decision(
            action_id=self.comment_action_id, decision="approve-once"
        )
        self._build_and_write_selection()
        self._mint(self.comment_action_id)

        self._append_decision(
            action_id=self.comment_action_id, decision="clear"
        )

        with self.assertRaisesRegex(
            AuthorizationError, "Exact approval is no longer effective"
        ):
            self._load(self.comment_action_id)

    def test_policy_pause_invalidates_policy_licensed_grant(self) -> None:
        self._append_policy(
            revision=1, enabled_classes=frozenset({"edit-comment"})
        )
        self._build_and_write_selection()
        self._mint(self.comment_action_id)

        # An unrelated event first -- the global stateRevision moves, but
        # the grant must still load.
        self._append_decision(
            action_id=self.close_action_id, decision="reject-once"
        )
        execution = self._load(self.comment_action_id)
        self.assertEqual(self.comment_action_id, execution.proposal["actionId"])

        # Pausing the exact revision that licensed the grant -- same
        # revisionId, status alone flips -- must invalidate it.
        self._flip_current_policy_status_in_ledger(status="paused")

        with self.assertRaisesRegex(
            AuthorizationError,
            "Licensing policy revision is no longer effective: policy:1",
        ):
            self._load(self.comment_action_id)

    def test_policy_revocation_invalidates_policy_licensed_grant(self) -> None:
        self._append_policy(
            revision=1, enabled_classes=frozenset({"edit-comment"})
        )
        self._build_and_write_selection()
        self._mint(self.comment_action_id)

        self._append_decision(
            action_id=self.close_action_id, decision="reject-once"
        )
        execution = self._load(self.comment_action_id)
        self.assertEqual(self.comment_action_id, execution.proposal["actionId"])

        self._flip_current_policy_status_in_ledger(status="revoked")

        with self.assertRaisesRegex(
            AuthorizationError,
            "Licensing policy revision is no longer effective: policy:1",
        ):
            self._load(self.comment_action_id)

    def test_later_reject_once_invalidates_decision_licensed_grant(self) -> None:
        # Distinct from test_decision_clear_invalidates_decision_licensed_
        # grant: here the same (proposalDigest, actionId) key receives a
        # *later* reject-once rather than a clear of the approve-once that
        # licensed the grant. `_latest_decision_events` keeps only the
        # ledger-order-last event per key, so the reject-once supersedes the
        # approval outright, and `_resolve_autonomous_license_source`'s
        # unconditional reject-scan (which runs before either license-source
        # branch) fires -- raising "Exact decision rejects", never "Exact
        # approval is no longer effective".
        self._append_policy(revision=1, enabled_classes=frozenset())
        self._append_decision(
            action_id=self.comment_action_id, decision="approve-once"
        )
        self._build_and_write_selection()
        self._mint(self.comment_action_id)

        self._append_decision(
            action_id=self.close_action_id, decision="reject-once"
        )
        execution = self._load(self.comment_action_id)
        self.assertEqual(self.comment_action_id, execution.proposal["actionId"])

        self._append_decision(
            action_id=self.comment_action_id, decision="reject-once"
        )

        with self.assertRaisesRegex(
            AuthorizationError,
            f"Exact decision rejects actionId: {self.comment_action_id}",
        ):
            self._load(self.comment_action_id)

    def test_same_action_reject_once_invalidates_policy_licensed_grant(
        self,
    ) -> None:
        # A policy-licensed grant (not a decision-licensed one, distinct
        # from the reject-once scenario above) can still be blocked by a
        # later exact reject-once for the same action: the reject-scan in
        # _resolve_autonomous_license_source runs unconditionally, ahead of
        # the "policy:" branch.
        self._append_policy(
            revision=1, enabled_classes=frozenset({"edit-comment"})
        )
        self._build_and_write_selection()
        self._mint(self.comment_action_id)

        self._append_decision(
            action_id=self.close_action_id, decision="reject-once"
        )
        execution = self._load(self.comment_action_id)
        self.assertEqual(self.comment_action_id, execution.proposal["actionId"])

        self._append_decision(
            action_id=self.comment_action_id, decision="reject-once"
        )

        with self.assertRaisesRegex(
            AuthorizationError,
            f"Exact decision rejects actionId: {self.comment_action_id}",
        ):
            self._load(self.comment_action_id)

    def test_active_standing_policy_deny_action_id_blocks_licensed_action(
        self,
    ) -> None:
        self._append_policy(
            revision=1, enabled_classes=frozenset({"edit-comment"})
        )
        self._build_and_write_selection()
        self._mint(self.comment_action_id)

        self._append_decision(
            action_id=self.close_action_id, decision="reject-once"
        )
        execution = self._load(self.comment_action_id)
        self.assertEqual(self.comment_action_id, execution.proposal["actionId"])

        # A later revision that denies this exact actionId blocks it via
        # the standing-policy deny-check -- which runs before, and raises
        # a distinct message from, the "Licensing policy revision is no
        # longer effective" check that the revision-identity mismatch
        # alone would otherwise raise.
        self._append_policy(
            revision=2,
            enabled_classes=frozenset({"edit-comment"}),
            replaces="policy:1",
            denied_action_ids=frozenset({self.comment_action_id}),
        )

        with self.assertRaisesRegex(
            AuthorizationError,
            f"Standing policy denies actionId: {self.comment_action_id}",
        ):
            self._load(self.comment_action_id)

    def test_active_standing_policy_deny_target_blocks_licensed_action(
        self,
    ) -> None:
        self._append_policy(
            revision=1, enabled_classes=frozenset({"edit-comment"})
        )
        self._build_and_write_selection()
        self._mint(self.comment_action_id)

        self._append_decision(
            action_id=self.close_action_id, decision="reject-once"
        )
        execution = self._load(self.comment_action_id)
        self.assertEqual(self.comment_action_id, execution.proposal["actionId"])

        # Both proposals target issue 1 (see setUp), so denying that target
        # blocks the licensed action the same way denying its actionId
        # would, via the sibling deniedTargets half of the same check.
        self._append_policy(
            revision=2,
            enabled_classes=frozenset({"edit-comment"}),
            replaces="policy:1",
            denied_targets=frozenset({"issue:1"}),
        )

        with self.assertRaisesRegex(
            AuthorizationError,
            f"Standing policy denies actionId: {self.comment_action_id}",
        ):
            self._load(self.comment_action_id)

    # -- dependent-action prerequisite binding -----------------------------

    def test_dependent_close_grant_binds_prerequisite_digest(self) -> None:
        self._append_policy(
            revision=1, enabled_classes=frozenset({"close-issue"})
        )
        comment_proposal = self.proposals["proposals"][0]
        body_digest = (
            "sha256:"
            + hashlib.sha256(
                comment_proposal["body"].encode("utf-8")
            ).hexdigest()
        )
        terminal_event = {
            "eventType": "terminal",
            "repository": self.repository,
            "actionId": self.comment_action_id,
            "operation": "edit-comment",
            "target": {"kind": "issue", "number": 1},
            "idempotencyKey": comment_proposal["idempotencyKey"],
            "snapshotId": self.proposals["snapshotId"],
            "bodyDigest": body_digest,
            "runId": "prior-run",
            "recordedAt": "2026-08-29T19:55:00Z",
            "outcome": "executed",
        }
        self._build_and_write_selection(action_events=[terminal_event])

        grant = self._mint(self.close_action_id)

        license_payload = grant["autonomousPolicyLicense"]
        self.assertEqual("close-issue", license_payload["operationClass"])
        self.assertEqual("policy:1", license_payload["licenseSource"])
        selection = json.loads(
            self.policy_selection_path.read_text(encoding="utf-8")
        )
        close_candidate = next(
            candidate
            for candidate in selection["candidates"]
            if candidate["actionId"] == self.close_action_id
        )
        self.assertEqual(
            close_candidate["satisfiedPrerequisites"],
            license_payload["satisfiedPrerequisites"],
        )

        execution = self._load(self.close_action_id)
        self.assertEqual(self.close_action_id, execution.proposal["actionId"])

        # If the terminal event underlying the prerequisite changes -- even
        # though the dependency remains individually "terminal" -- the whole
        # selection's bytes differ, so the grant's bound
        # policySelectionDigest can no longer match.
        changed_terminal_event = dict(terminal_event)
        changed_terminal_event["recordedAt"] = "2026-08-29T19:56:00Z"
        self._build_and_write_selection(action_events=[changed_terminal_event])

        with self.assertRaisesRegex(
            AuthorizationError, "policySelectionDigest does not match"
        ):
            self._load(self.close_action_id)

    # -- delegate-copilot: class caps supplement, never replace, live -----
    # -- capacity controls -------------------------------------------------

    def test_operator_assignment_requires_exact_license_and_binds_instructions(self) -> None:
        from tests.test_actor import _assignment_proposals

        proposal = _assignment_proposals()["proposals"][0]
        action_id = f"{self.proposals['snapshotId']}:issue:21:assign-copilot"
        proposal.update(
            actionId=action_id,
            issueUrl="https://github.com/microsoft/aspire/issues/21",
            targetRepository=self.repository,
            evidenceBasis="operator-request",
        )
        proposal["executionEligibility"].update(
            evidenceBasis="operator-request", ciLabels=[], occurrenceCount=0,
        )
        self.proposals["proposals"] = [proposal]
        self._write_proposals()
        self._append_policy(revision=1, enabled_classes=frozenset({"delegate-copilot"}))
        self._build_and_write_selection()
        with self.assertRaisesRegex(AuthorizationError, "does not select"):
            self._mint(action_id)

        self._append_decision(action_id=action_id, decision="approve-once")
        selection = self._build_and_write_selection()
        grant = self._mint(action_id)
        self.assertTrue(grant["autonomousPolicyLicense"]["licenseSource"].startswith("decision:"))
        self.assertEqual(proposal["customInstructions"], self._load(action_id).proposal["customInstructions"])

        candidate, = selection["candidates"]
        candidate.update(status="automatic", licenseSource="policy:1")
        self.policy_selection_path.write_text(json.dumps(selection), encoding="utf-8")
        with self.assertRaisesRegex(AuthorizationError, "requires an exact approval"):
            self._mint(action_id)

        proposal["customInstructions"] += "\nAdditional instruction."
        self._write_proposals()
        selection = self._build_and_write_selection()
        self.assertEqual([], selection["selectedActionIds"])
        with self.assertRaisesRegex(AuthorizationError, "does not select"):
            self._mint(action_id)

    def test_delegate_copilot_binds_capacity_policy_digest(self) -> None:
        delegate_action_id = (
            "snapshot:microsoft/aspire:2026-08-29T20:00:00Z:issue:2:delegate"
        )
        self.proposals["proposals"].append(
            {
                "actionId": delegate_action_id,
                "issueNumber": 2,
                "issueUrl": "https://github.com/microsoft/aspire/issues/2",
                "operation": "assign-copilot",
                "targetRepository": "microsoft/aspire",
                "baseBranch": "main",
                "customInstructions": "Fix issue #2 and open a draft PR.",
                "model": "",
                "evidenceBasis": "ci-occurrence",
                "idempotencyKey": "issue:2:delegate",
                "evidenceIds": ["issue:2"],
                "expectedIssueState": "open",
                "executionEligibility": {
                    "eligible": True,
                    "evidenceBasis": "ci-occurrence",
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
        policy_path = self.scratch / "delegation-policy.json"
        policy_document = {
            "schemaVersion": 1,
            "repository": "microsoft/aspire",
            "maxActionsPerGrant": 5,
            "capacity": {
                "maxRunningCopilotTasks": 2,
                "maxCopilotStartsPerRolling24h": 3,
                "maxOpenDelegatedPullRequests": 5,
                "maxRepositoryRunningCopilotTasks": 100,
            },
        }
        policy_path.write_text(json.dumps(policy_document), encoding="utf-8")
        self._append_policy(
            revision=1, enabled_classes=frozenset({"delegate-copilot"})
        )
        self._build_and_write_selection()

        grant = self._mint(
            delegate_action_id, production_delegation_policy_path=policy_path
        )

        self.assertEqual(
            "delegate-copilot", grant["autonomousPolicyLicense"]["operationClass"]
        )
        self.assertIsNotNone(grant["capacityPolicyDigest"])

        execution = self._load(
            delegate_action_id, production_delegation_policy_path=policy_path
        )
        self.assertEqual(delegate_action_id, execution.proposal["actionId"])

        # Drift in the pinned capacity policy after grant creation is still
        # caught: class caps supplement, never replace, live capacity
        # controls.
        policy_document["capacity"]["maxRunningCopilotTasks"] = 1
        policy_path.write_text(json.dumps(policy_document), encoding="utf-8")

        with self.assertRaisesRegex(AuthorizationError, "changed after grant"):
            self._load(
                delegate_action_id, production_delegation_policy_path=policy_path
            )

    # -- byte/schema compatibility and mutual exclusion --------------------

    def test_legacy_grant_without_autonomous_keys_still_loads(self) -> None:
        # A grant minted before Task 4 existed carries none of the new
        # autonomous/policy-selection keys at all -- not even as null
        # placeholders. The loader must still accept this exact byte shape.
        legacy_action_id = (
            "snapshot:radical/aspire:2026-08-29T20:00:00Z:issue:1:watch-comment"
        )
        legacy_proposals = {
            "schemaVersion": 2,
            "repository": "radical/aspire",
            "snapshotId": "snapshot:radical/aspire:2026-08-29T20:00:00Z",
            "shepherdAuthor": "radical",
            "generatedAtUtc": "2026-08-29T20:00:00Z",
            "proposalTtlHours": 24,
            "maxProposalsPerIssue": 2,
            "executionEligibility": {"status": "eligible", "violations": []},
            "proposals": [
                {
                    "actionId": legacy_action_id,
                    "issueNumber": 1,
                    "issueUrl": "https://github.com/radical/aspire/issues/1",
                    "operation": "create-comment",
                    "evidenceBasis": "ci-occurrence",
                    "idempotencyKey": "issue:1:status",
                    "body": (
                        "[automated] Watching.\n\n"
                        "<!-- ci-shepherd:idempotency-key=issue:1:status -->"
                    ),
                    "evidenceIds": ["issue:1"],
                    "expectedIssueState": "open",
                    "executionEligibility": {
                        "eligible": True,
                        "evidenceBasis": "ci-occurrence",
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
        legacy_proposals_path = self.scratch / "legacy-proposals.json"
        legacy_authorization_path = self.scratch / "legacy-grant.json"
        proposal_bytes = (
            json.dumps(legacy_proposals, indent=2, sort_keys=True) + "\n"
        ).encode()
        legacy_proposals_path.write_bytes(proposal_bytes)
        legacy_grant = {
            "schemaVersion": 2,
            "grantId": "grant:legacy",
            "repository": "radical/aspire",
            "stateDirectory": str(self.state_dir),
            "issuedAtUtc": "2026-08-29T20:00:00Z",
            "expiresAtUtc": "2026-08-29T20:15:00Z",
            "snapshotId": legacy_proposals["snapshotId"],
            "proposalsDigest": (
                f"sha256:{hashlib.sha256(proposal_bytes).hexdigest()}"
            ),
            "allowedActionIds": [legacy_action_id],
            "allowedOperations": ["create-comment"],
            "allowedTargets": [{"kind": "issue", "number": 1}],
            "allowedChainRoots": [legacy_action_id],
            "overrideSuppressionForActionIds": [],
            "budget": {
                "maxMutationAttempts": 1,
                "maxChains": 1,
                "maxRunningCopilotTasks": 2,
                "maxCopilotStartsPerRolling24h": 3,
                "maxOpenDelegatedPullRequests": 5,
                "maxRepositoryRunningCopilotTasks": 100,
            },
            "productionCommentPilot": False,
        }
        legacy_authorization_path.write_text(
            json.dumps(legacy_grant), encoding="utf-8"
        )

        execution = load_authorized_execution(
            legacy_proposals_path,
            legacy_authorization_path,
            state_dir=self.state_dir,
            action_id=legacy_action_id,
            now=datetime(2026, 8, 29, 20, 5, tzinfo=UTC),
        )

        self.assertEqual(legacy_action_id, execution.proposal["actionId"])
        self.assertFalse(execution.grant.autonomous_policy)
        self.assertIsNone(execution.grant.autonomous_policy_license)

    def test_autonomous_capability_is_mutually_exclusive_with_pilot_flags(
        self,
    ) -> None:
        self._append_policy(
            revision=1, enabled_classes=frozenset({"edit-comment"})
        )
        self._build_and_write_selection()

        for flag_name in (
            "allow_production_comment_pilot",
            "allow_production_delegation_pilot",
            "allow_production_delegation_steady_state",
        ):
            with self.subTest(flag=flag_name):
                with self.assertRaisesRegex(
                    AuthorizationError, "mutually exclusive"
                ):
                    self._generate(
                        action_ids=[self.comment_action_id],
                        allow_autonomous_policy=True,
                        policy_selection_path=self.policy_selection_path,
                        policy_action_id=self.comment_action_id,
                        **{flag_name: True},
                    )

    def test_loader_rejects_unknown_autonomous_license_field(self) -> None:
        self._append_policy(
            revision=1, enabled_classes=frozenset({"edit-comment"})
        )
        self._build_and_write_selection()
        grant = self._mint(self.comment_action_id)

        grant["autonomousPolicyLicense"]["unexpectedField"] = "nope"
        self.output_path.write_text(json.dumps(grant), encoding="utf-8")

        with self.assertRaises(AuthorizationError):
            self._load(self.comment_action_id)

    def test_loader_rejects_malformed_autonomous_license_field(self) -> None:
        self._append_policy(
            revision=1, enabled_classes=frozenset({"edit-comment"})
        )
        self._build_and_write_selection()
        grant = self._mint(self.comment_action_id)

        grant["autonomousPolicyLicense"]["selectionStateRevision"] = "not-an-int"
        self.output_path.write_text(json.dumps(grant), encoding="utf-8")

        with self.assertRaises(AuthorizationError):
            self._load(self.comment_action_id)

    # -- load-time revalidation: the loader must re-derive every semantic --
    # -- field from the frozen selection bytes and the real proposal, ------
    # -- never trust a grant's self-declared AutonomousPolicyLicense. ------
    #
    # Each test below starts from one previously-minted, previously-valid
    # grant and mutates a single field of its own JSON in place -- never the
    # selection artifact, never the ledger -- to prove the loader itself
    # (not some other layer) is the one closing each gap.

    def test_loader_rejects_grant_retargeted_to_unselected_action(self) -> None:
        self._append_policy(
            revision=1, enabled_classes=frozenset({"edit-comment"})
        )
        self._build_and_write_selection()
        grant = self._mint(self.comment_action_id)

        # close_action_id's class ("close-issue") is not enabled by the
        # policy above, so it was never a member of the frozen selection's
        # selectedActionIds. Retargeting every action-identifying field in
        # lockstep keeps the grant internally self-consistent -- the attack
        # only a re-check against the real selection bytes can catch.
        mutated = copy.deepcopy(grant)
        mutated["allowedActionIds"] = [self.close_action_id]
        mutated["allowedChainRoots"] = [self.close_action_id]
        mutated["allowedOperations"] = ["close-issue"]
        self.output_path.write_text(json.dumps(mutated), encoding="utf-8")

        with self.assertRaisesRegex(
            AuthorizationError, "does not select actionId"
        ):
            self._load(self.close_action_id)

    def test_loader_rejects_license_operation_class_mismatch(self) -> None:
        self._append_policy(
            revision=1, enabled_classes=frozenset({"edit-comment"})
        )
        self._build_and_write_selection()
        grant = self._mint(self.comment_action_id)

        mutated = copy.deepcopy(grant)
        mutated["autonomousPolicyLicense"]["operationClass"] = "close-issue"
        self.output_path.write_text(json.dumps(mutated), encoding="utf-8")

        with self.assertRaisesRegex(
            AuthorizationError,
            "no longer matches its bound policy selection",
        ):
            self._load(self.comment_action_id)

    def test_loader_rejects_license_source_substitution(self) -> None:
        # policy:1 is active but does NOT enable "edit-comment"; only an
        # exact approve-once decision licenses this specific action, so the
        # frozen selection's authoritative licenseSource is "decision:<n>".
        self._append_policy(revision=1, enabled_classes=frozenset())
        self._append_decision(
            action_id=self.comment_action_id, decision="approve-once"
        )
        self._build_and_write_selection()
        grant = self._mint(self.comment_action_id)
        self.assertTrue(
            grant["autonomousPolicyLicense"]["licenseSource"].startswith(
                "decision:"
            )
        )

        # Substituting "policy:1" as the licenseSource passes
        # `_resolve_autonomous_license_source`'s standalone effectiveness
        # check in isolation -- policy:1 really is active right now -- even
        # though policy:1 never actually licensed this action's operation
        # class. Only re-deriving licenseSource from the frozen selection
        # candidate catches this identity substitution.
        mutated = copy.deepcopy(grant)
        mutated["autonomousPolicyLicense"]["licenseSource"] = "policy:1"
        self.output_path.write_text(json.dumps(mutated), encoding="utf-8")

        with self.assertRaisesRegex(
            AuthorizationError,
            "no longer matches its bound policy selection",
        ):
            self._load(self.comment_action_id)

    def test_loader_rejects_license_selection_state_revision_mismatch(
        self,
    ) -> None:
        self._append_policy(
            revision=1, enabled_classes=frozenset({"edit-comment"})
        )
        self._build_and_write_selection()
        grant = self._mint(self.comment_action_id)

        mutated = copy.deepcopy(grant)
        mutated["autonomousPolicyLicense"]["selectionStateRevision"] += 1
        self.output_path.write_text(json.dumps(mutated), encoding="utf-8")

        with self.assertRaisesRegex(
            AuthorizationError,
            "no longer matches its bound policy selection",
        ):
            self._load(self.comment_action_id)

    def test_loader_rejects_license_run_id_mismatch(self) -> None:
        self._append_policy(
            revision=1, enabled_classes=frozenset({"edit-comment"})
        )
        self._build_and_write_selection()
        grant = self._mint(self.comment_action_id)

        mutated = copy.deepcopy(grant)
        mutated["autonomousPolicyLicense"]["runId"] = "a-different-run"
        self.output_path.write_text(json.dumps(mutated), encoding="utf-8")

        with self.assertRaisesRegex(
            AuthorizationError,
            "no longer matches its bound policy selection",
        ):
            self._load(self.comment_action_id)

    def test_loader_rejects_license_satisfied_prerequisites_mismatch(
        self,
    ) -> None:
        self._append_policy(
            revision=1, enabled_classes=frozenset({"close-issue"})
        )
        comment_proposal = self.proposals["proposals"][0]
        body_digest = (
            "sha256:"
            + hashlib.sha256(
                comment_proposal["body"].encode("utf-8")
            ).hexdigest()
        )
        terminal_event = {
            "eventType": "terminal",
            "repository": self.repository,
            "actionId": self.comment_action_id,
            "operation": "edit-comment",
            "target": {"kind": "issue", "number": 1},
            "idempotencyKey": comment_proposal["idempotencyKey"],
            "snapshotId": self.proposals["snapshotId"],
            "bodyDigest": body_digest,
            "runId": "prior-run",
            "recordedAt": "2026-08-29T19:55:00Z",
            "outcome": "executed",
        }
        self._build_and_write_selection(action_events=[terminal_event])
        grant = self._mint(self.close_action_id)
        self.assertEqual(
            1,
            len(grant["autonomousPolicyLicense"]["satisfiedPrerequisites"]),
        )

        # Stripping the dependent action's bound prerequisite lets a
        # retargeted or replayed close execute as though its prerequisite
        # had never been proven terminal, even though the frozen selection
        # bytes (recomputed fresh) still carry the one true digest.
        mutated = copy.deepcopy(grant)
        mutated["autonomousPolicyLicense"]["satisfiedPrerequisites"] = []
        self.output_path.write_text(json.dumps(mutated), encoding="utf-8")

        with self.assertRaisesRegex(
            AuthorizationError,
            "no longer matches its bound policy selection",
        ):
            self._load(self.close_action_id)

    def test_loader_rejects_grant_with_inflated_mutation_attempts(
        self,
    ) -> None:
        self._append_policy(
            revision=1, enabled_classes=frozenset({"edit-comment"})
        )
        self._build_and_write_selection()
        grant = self._mint(self.comment_action_id)

        mutated = copy.deepcopy(grant)
        mutated["budget"]["maxMutationAttempts"] = 2
        self.output_path.write_text(json.dumps(mutated), encoding="utf-8")

        with self.assertRaisesRegex(
            AuthorizationError,
            "exactly one mutation attempt and one chain",
        ):
            self._load(self.comment_action_id)

    def test_loader_rejects_grant_with_inflated_max_chains(self) -> None:
        self._append_policy(
            revision=1, enabled_classes=frozenset({"edit-comment"})
        )
        self._build_and_write_selection()
        grant = self._mint(self.comment_action_id)

        mutated = copy.deepcopy(grant)
        mutated["budget"]["maxChains"] = 2
        self.output_path.write_text(json.dumps(mutated), encoding="utf-8")

        with self.assertRaisesRegex(
            AuthorizationError,
            "exactly one mutation attempt and one chain",
        ):
            self._load(self.comment_action_id)

    def test_loader_rejects_delegate_grant_relabeled_to_bypass_capacity(
        self,
    ) -> None:
        delegate_action_id = (
            "snapshot:microsoft/aspire:2026-08-29T20:00:00Z:issue:2:delegate"
        )
        self.proposals["proposals"].append(
            {
                "actionId": delegate_action_id,
                "issueNumber": 2,
                "issueUrl": "https://github.com/microsoft/aspire/issues/2",
                "operation": "assign-copilot",
                "targetRepository": "microsoft/aspire",
                "baseBranch": "main",
                "customInstructions": "Fix issue #2 and open a draft PR.",
                "model": "",
                "evidenceBasis": "ci-occurrence",
                "idempotencyKey": "issue:2:delegate",
                "evidenceIds": ["issue:2"],
                "expectedIssueState": "open",
                "executionEligibility": {
                    "eligible": True,
                    "evidenceBasis": "ci-occurrence",
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
        policy_path = self.scratch / "delegation-policy.json"
        policy_document = {
            "schemaVersion": 1,
            "repository": "microsoft/aspire",
            "maxActionsPerGrant": 5,
            "capacity": {
                "maxRunningCopilotTasks": 2,
                "maxCopilotStartsPerRolling24h": 3,
                "maxOpenDelegatedPullRequests": 5,
                "maxRepositoryRunningCopilotTasks": 100,
            },
        }
        policy_path.write_text(json.dumps(policy_document), encoding="utf-8")
        self._append_policy(
            revision=1, enabled_classes=frozenset({"delegate-copilot"})
        )
        self._build_and_write_selection()

        grant = self._mint(
            delegate_action_id, production_delegation_policy_path=policy_path
        )
        self.assertEqual(
            "delegate-copilot",
            grant["autonomousPolicyLicense"]["operationClass"],
        )
        self.assertIsNotNone(grant["capacityPolicyDigest"])

        # Tighten live capacity below the grant's own bound budget (2)
        # *after* generation. If the loader still branched on the grant's
        # self-declared operationClass, relabeling it to "edit-comment" and
        # dropping capacityPolicyDigest would skip the entire
        # delegate-copilot capacity gate, even though the *real* proposal
        # operation ("assign-copilot") is still delegate-copilot and would
        # fail this tighter policy.
        policy_document["capacity"]["maxRunningCopilotTasks"] = 1
        policy_path.write_text(json.dumps(policy_document), encoding="utf-8")

        mutated = copy.deepcopy(grant)
        mutated["autonomousPolicyLicense"]["operationClass"] = "edit-comment"
        mutated["capacityPolicyDigest"] = None
        self.output_path.write_text(json.dumps(mutated), encoding="utf-8")

        with self.assertRaisesRegex(
            AuthorizationError,
            "no longer matches its bound policy selection",
        ):
            self._load(
                delegate_action_id,
                production_delegation_policy_path=policy_path,
            )

    # -- CLI: --autonomous-policy argparse validation -----------------------
    #
    # create_authorization.py's own `main()` validates --autonomous-policy's
    # companion flags with `parser.error(...)` before ever calling
    # generate_authorization_grant, so these exercise argparse's exit path
    # directly rather than duplicating authorization.py's own tests.

    def _run_create_authorization_cli(self, argv: list[str]) -> tuple[int, str]:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                create_authorization.main(argv)
        exit_code = raised.exception.code
        assert isinstance(exit_code, int)
        return exit_code, stderr.getvalue()

    def test_cli_autonomous_mode_rejects_multiple_action_ids(self) -> None:
        exit_code, stderr = self._run_create_authorization_cli(
            [
                "--proposals", str(self.proposals_path),
                "--state-dir", str(self.state_dir),
                "--output", str(self.output_path),
                "--action-id", self.comment_action_id,
                "--action-id", self.close_action_id,
                "--autonomous-policy",
                "--policy-selection", str(self.policy_selection_path),
                "--policy-action-id", self.comment_action_id,
            ]
        )
        self.assertEqual(2, exit_code)
        self.assertIn(
            "--autonomous-policy allows exactly one --action-id", stderr
        )

    def test_cli_autonomous_mode_requires_policy_selection(self) -> None:
        exit_code, stderr = self._run_create_authorization_cli(
            [
                "--proposals", str(self.proposals_path),
                "--state-dir", str(self.state_dir),
                "--output", str(self.output_path),
                "--action-id", self.comment_action_id,
                "--autonomous-policy",
                "--policy-action-id", self.comment_action_id,
            ]
        )
        self.assertEqual(2, exit_code)
        self.assertIn(
            "--autonomous-policy requires --policy-selection", stderr
        )

    def test_cli_autonomous_mode_requires_matching_policy_action_id(
        self,
    ) -> None:
        exit_code, stderr = self._run_create_authorization_cli(
            [
                "--proposals", str(self.proposals_path),
                "--state-dir", str(self.state_dir),
                "--output", str(self.output_path),
                "--action-id", self.comment_action_id,
                "--autonomous-policy",
                "--policy-selection", str(self.policy_selection_path),
                "--policy-action-id", self.close_action_id,
            ]
        )
        self.assertEqual(2, exit_code)
        self.assertIn(
            "--policy-action-id must equal the single --action-id", stderr
        )


if __name__ == "__main__":
    unittest.main()
