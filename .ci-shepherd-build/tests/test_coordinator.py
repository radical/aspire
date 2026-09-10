"""Tests for coordinator.py: the headless local policy coordinator CLI.

coordinator.py is a thin adapter over the Task 1-5 modules
(operation_policy, coordinator_state, policy_selection, authorization) --
it must not duplicate any policy or exact-decision logic beyond assembling
CLI-supplied fields into calls those modules already validate. These tests
therefore assert behavior at the CLI boundary (argv in, exit code +
stdout/stderr JSON out) and cross-check a handful of derived fields (policy
revision identity, TTL, grant shape) against what the underlying modules
independently compute, rather than re-deriving policy or selection rules
here.

See docs/superpowers/plans/2026-09-03-ci-shepherd-autonomous-policy.md
(Task 6) for the full command/behavior specification these tests are
written against.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import os
import shutil
import stat
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ci_shepherd.models import stable_json
from ci_shepherd.operation_policy import DEFAULT_CAPS, MAX_EXPIRY_DAYS, OPERATION_CLASSES

# Reuse the frozen, already-reviewed action-proposal fixture builders from
# Task 3's own test suite instead of re-deriving the schemaVersion-2 shape
# here: any drift in what a valid proposal/selection document looks like
# should surface as a single shared-fixture change, not two forks that can
# silently disagree.
from tests.test_policy_selection import (
    REPOSITORY,
    _close_proposal,
    _comment_proposal,
    _document as _selection_document,
)

import coordinator


def _rfc3339(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _caps_document(
    *, enabled_classes: frozenset[str] = frozenset({"create-comment"})
) -> dict[str, object]:
    return {
        name: {
            "enabled": name in enabled_classes,
            "maxPerRun": DEFAULT_CAPS[name]["maxPerRun"],
            "maxRolling24h": DEFAULT_CAPS[name]["maxRolling24h"],
        }
        for name in OPERATION_CLASSES
    }


def _policy_document(
    *,
    revision: int = 1,
    replaces: str | None = None,
    status: str = "active",
    created_at_utc: datetime,
    expires_at_utc: datetime,
    enabled_classes: frozenset[str] = frozenset({"create-comment"}),
    actor: str = "github:radical",
    denied_action_ids: list[str] | None = None,
    denied_targets: list[str] | None = None,
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "repository": REPOSITORY,
        "revisionId": f"policy:{revision}",
        "revision": revision,
        "status": status,
        "createdAtUtc": _rfc3339(created_at_utc),
        "expiresAtUtc": _rfc3339(expires_at_utc),
        "actor": actor,
        "replacesRevisionId": replaces,
        "operationClasses": _caps_document(enabled_classes=enabled_classes),
        "deniedActionIds": list(denied_action_ids or []),
        "deniedTargets": list(denied_targets or []),
    }


class CoordinatorCliTestCase(unittest.TestCase):
    """Shared scratch-directory plumbing for every coordinator.py test."""

    def setUp(self) -> None:
        self.scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch.mkdir(parents=True)
        self.state_dir = self.scratch / "state"
        self.repository = REPOSITORY
        self.now = datetime(2026, 9, 3, 16, 0, tzinfo=UTC)
        self.proposals_path = self.scratch / "action-proposals.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)

    # -- CLI plumbing -------------------------------------------------------

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = coordinator.main(argv)
        self.assertIsInstance(code, int)
        return code, stdout.getvalue(), stderr.getvalue()

    def _now_arg(self, when: datetime | None = None) -> str:
        return _rfc3339(when or self.now)

    def _write_json(self, path: Path, document: object) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(stable_json(document).encode("utf-8"))
        return path

    def _current_state_revision(self) -> int:
        code, stdout, _stderr = self._run(
            [
                "projection",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--now", self._now_arg(),
            ]
        )
        self.assertEqual(0, code)
        return json.loads(stdout)["stateRevision"]

    def _write_proposals(
        self,
        proposals: list[dict[str, object]],
        *,
        evidence_round: int = 1,
        **kwargs: object,
    ) -> dict[str, object]:
        # generate_authorization_grant's production-repository freshness
        # check (independent of, and in addition to, autonomous policy)
        # requires every proposals document for microsoft/aspire to carry a
        # snapshotId of the form "snapshot:<repository>:<collected-at>[:r1]"
        # plus a matching productionPilotCapability. Policy-backed fork grants
        # retain that finalized/fresh snapshot requirement, so the default fixture
        # supplies it rather than having each call site re-derive one.
        collected_at = self.now - timedelta(minutes=5)
        snapshot_id = f"snapshot:{self.repository}:{_rfc3339(collected_at)}"
        if evidence_round == 1:
            snapshot_id += ":r1"
        kwargs.setdefault("snapshot_id", snapshot_id)
        document = _selection_document(proposals, **kwargs)
        document["productionPilotCapability"] = {
            "schemaVersion": 1,
            "evidenceRound": evidence_round,
        }
        self._write_json(self.proposals_path, document)
        return document

    def _cycle_run_id(self) -> str:
        proposals = json.loads(self.proposals_path.read_text(encoding="utf-8"))
        return f"cycle:{proposals['snapshotId']}"

    def _activate_policy(
        self,
        *,
        enabled_classes: frozenset[str] = frozenset({"create-comment"}),
        expires_in_days: int = 30,
        actor: str = "github:radical",
        now: datetime | None = None,
    ) -> dict[str, object]:
        caps_path = self.scratch / "caps.json"
        self._write_json(caps_path, _caps_document(enabled_classes=enabled_classes))
        code, stdout, stderr = self._run(
            [
                "policy-activate",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--expected-revision", str(self._current_state_revision()),
                "--caps", str(caps_path),
                "--expires-in-days", str(expires_in_days),
                "--actor", actor,
                "--now", self._now_arg(now),
            ]
        )
        self.assertEqual(0, code, stderr)
        return json.loads(stdout)

    def _set_decision(
        self,
        *,
        action_id: str,
        decision: str,
        actor: str = "github:radical",
        now: datetime | None = None,
    ) -> dict[str, object]:
        code, stdout, stderr = self._run(
            [
                "decision-set",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--expected-revision", str(self._current_state_revision()),
                "--proposals", str(self.proposals_path),
                "--action-id", action_id,
                "--decision", decision,
                "--actor", actor,
                "--now", self._now_arg(now),
            ]
        )
        self.assertEqual(0, code, stderr)
        return json.loads(stdout)

    def _select(
        self,
        *,
        run_id: str | None = None,
        now: datetime | None = None,
    ) -> Path:
        run_id = run_id or self._cycle_run_id()
        output_path = self.scratch / "selection.json"
        code, stdout, stderr = self._run(
            [
                "select",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--proposals", str(self.proposals_path),
                "--run-id", run_id,
                "--output", str(output_path),
                "--now", self._now_arg(now),
            ]
        )
        self.assertEqual(0, code, stderr)
        return output_path


# ---------------------------------------------------------------------------
# projection
# ---------------------------------------------------------------------------


class ProjectionCommandTests(CoordinatorCliTestCase):
    def test_exact_selection_is_ready_without_active_policy(self) -> None:
        self.assertEqual(
            "ready",
            coordinator._stage_for(
                None,
                self.now,
                {"selectedActionIds": ["action:1"]},
            ),
        )

    def test_no_policy_returns_awaiting_policy_stage_not_error(self) -> None:
        code, stdout, stderr = self._run(
            [
                "projection",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--now", self._now_arg(),
            ]
        )

        self.assertEqual(0, code, stderr)
        result = json.loads(stdout)
        self.assertEqual("awaiting-policy", result["stage"])
        self.assertIsNone(result["effectivePolicy"])
        self.assertEqual(0, result["stateRevision"])

    def test_active_policy_returns_policy_active_stage(self) -> None:
        self._activate_policy()

        code, stdout, stderr = self._run(
            [
                "projection",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--now", self._now_arg(),
            ]
        )

        self.assertEqual(0, code, stderr)
        result = json.loads(stdout)
        self.assertEqual("policy-active", result["stage"])
        self.assertEqual("policy:1", result["effectivePolicy"]["revisionId"])

    def test_proposals_and_run_id_embed_selection_and_ready_stage(self) -> None:
        self._activate_policy()
        action_id = "snapshot:test:1:issue:1:comment"
        self._write_proposals(
            [_comment_proposal(action_id=action_id, issue_number=1)],
            unchanged_issue_numbers=[],
        )

        code, stdout, stderr = self._run(
            [
                "projection",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--proposals", str(self.proposals_path),
                "--run-id", self._cycle_run_id(),
                "--now", self._now_arg(),
            ]
        )

        self.assertEqual(0, code, stderr)
        result = json.loads(stdout)
        self.assertEqual("ready", result["stage"])
        self.assertEqual([action_id], result["selection"]["automaticActionIds"])

    def test_proposals_without_run_id_is_rejected(self) -> None:
        code, _stdout, stderr = self._run(
            [
                "projection",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--proposals", str(self.proposals_path),
                "--now", self._now_arg(),
            ]
        )

        self.assertNotEqual(0, code)
        error = json.loads(stderr)
        self.assertTrue(error["error"])
        self.assertIn("--run-id", error["message"])

    def test_run_id_without_proposals_is_rejected(self) -> None:
        code, _stdout, stderr = self._run(
            [
                "projection",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--run-id", "cycle:missing",
                "--now", self._now_arg(),
            ]
        )

        self.assertNotEqual(0, code)
        error = json.loads(stderr)
        self.assertTrue(error["error"])
        self.assertIn("--proposals", error["message"])

    def test_rejects_symlinked_state_dir(self) -> None:
        real_dir = self.scratch / "real-state"
        real_dir.mkdir()
        link_dir = self.scratch / "linked-state"
        link_dir.symlink_to(real_dir, target_is_directory=True)

        code, _stdout, stderr = self._run(
            [
                "projection",
                "--repository", self.repository,
                "--state-dir", str(link_dir),
                "--now", self._now_arg(),
            ]
        )

        self.assertNotEqual(0, code)
        error = json.loads(stderr)
        self.assertTrue(error["error"])


# ---------------------------------------------------------------------------
# policy-append
# ---------------------------------------------------------------------------


class PolicyAppendCommandTests(CoordinatorCliTestCase):
    def test_success_returns_state_revision(self) -> None:
        document_path = self.scratch / "policy.json"
        self._write_json(
            document_path,
            _policy_document(
                revision=1,
                replaces=None,
                created_at_utc=self.now - timedelta(days=1),
                expires_at_utc=self.now + timedelta(days=29),
            ),
        )

        code, stdout, stderr = self._run(
            [
                "policy-append",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--expected-revision", "0",
                "--document", str(document_path),
            ]
        )

        self.assertEqual(0, code, stderr)
        self.assertEqual(1, json.loads(stdout)["stateRevision"])

    def test_stale_revision_returns_typed_error_with_refreshed_projection(
        self,
    ) -> None:
        self._activate_policy()  # advances the ledger to stateRevision 1
        document_path = self.scratch / "policy.json"
        self._write_json(
            document_path,
            _policy_document(
                revision=2,
                replaces="policy:1",
                created_at_utc=self.now,
                expires_at_utc=self.now + timedelta(days=30),
            ),
        )

        code, _stdout, stderr = self._run(
            [
                "policy-append",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--expected-revision", "0",  # stale: ledger is already at 1
                "--document", str(document_path),
            ]
        )

        self.assertEqual(2, code)
        error = json.loads(stderr)
        self.assertEqual("stale-view", error["code"])
        self.assertEqual(1, error["projection"]["stateRevision"])
        self.assertEqual(
            "policy:1", error["projection"]["effectivePolicy"]["revisionId"]
        )

    def test_rejects_malformed_document_with_typed_error(self) -> None:
        document_path = self.scratch / "policy.json"
        self._write_json(document_path, {"not": "a policy document"})

        code, _stdout, stderr = self._run(
            [
                "policy-append",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--expected-revision", "0",
                "--document", str(document_path),
            ]
        )

        self.assertNotEqual(0, code)
        error = json.loads(stderr)
        self.assertTrue(error["error"])
        # append_policy_revision wraps a Task 1 schema failure into its own
        # CoordinatorStateError (it additionally needs to check the
        # document's repository/predecessor against ledger state before it
        # can even reach a Task 1 validation error), so the malformed
        # document surfaces as coordinator-state-error, not
        # operation-policy-error.
        self.assertEqual("coordinator-state-error", error["code"])

    def test_missing_expected_revision_flag_is_rejected(self) -> None:
        document_path = self.scratch / "policy.json"
        self._write_json(
            document_path,
            _policy_document(
                revision=1,
                replaces=None,
                created_at_utc=self.now,
                expires_at_utc=self.now + timedelta(days=30),
            ),
        )

        code, _stdout, stderr = self._run(
            [
                "policy-append",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--document", str(document_path),
            ]
        )

        self.assertNotEqual(0, code)
        error = json.loads(stderr)
        self.assertTrue(error["error"])

    def test_rejects_symlinked_document_path(self) -> None:
        real_path = self.scratch / "real-policy.json"
        self._write_json(
            real_path,
            _policy_document(
                revision=1,
                replaces=None,
                created_at_utc=self.now,
                expires_at_utc=self.now + timedelta(days=30),
            ),
        )
        link_path = self.scratch / "linked-policy.json"
        link_path.symlink_to(real_path)

        code, _stdout, stderr = self._run(
            [
                "policy-append",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--expected-revision", "0",
                "--document", str(link_path),
            ]
        )

        self.assertNotEqual(0, code)
        error = json.loads(stderr)
        self.assertTrue(error["error"])


# ---------------------------------------------------------------------------
# policy-activate / policy-pause / policy-revoke
# ---------------------------------------------------------------------------


class PolicyActivateCommandTests(CoordinatorCliTestCase):
    def test_activates_checked_in_bounded_live_pilot_caps(self) -> None:
        caps_path = (
            Path(__file__).parents[1]
            / "policies"
            / "autonomous-live-pilot-caps-v1.json"
        )

        code, stdout, stderr = self._run(
            [
                "policy-activate",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--expected-revision", "0",
                "--caps", str(caps_path),
                "--expires-in-days", "1",
                "--actor", "github:radical",
                "--now", self._now_arg(),
            ]
        )

        self.assertEqual(0, code, stderr)
        operation_classes = json.loads(stdout)["effectivePolicy"]["operationClasses"]
        self.assertEqual(
            {
                "create-comment": {
                    "enabled": True,
                    "maxPerRun": 2,
                    "maxRolling24h": 10,
                },
                "edit-comment": {
                    "enabled": True,
                    "maxPerRun": 2,
                    "maxRolling24h": 10,
                },
                "close-issue": {
                    "enabled": True,
                    "maxPerRun": 2,
                    "maxRolling24h": 4,
                },
                "delegate-copilot": {
                    "enabled": True,
                    "maxPerRun": 3,
                    "maxRolling24h": 10,
                },
                "rerun-or-retry": {
                    "enabled": False,
                    "maxPerRun": 0,
                    "maxRolling24h": 0,
                },
            },
            operation_classes,
        )

    def test_first_activation_derives_revision_one_with_null_predecessor(
        self,
    ) -> None:
        result = self._activate_policy(actor="github:radical")

        self.assertEqual("policy:1", result["effectivePolicy"]["revisionId"])
        self.assertEqual(1, result["effectivePolicy"]["revision"])
        self.assertIsNone(result["effectivePolicy"]["replacesRevisionId"])
        self.assertEqual("active", result["effectivePolicy"]["status"])
        self.assertEqual("github:radical", result["effectivePolicy"]["actor"])
        self.assertEqual(
            self._now_arg(), result["effectivePolicy"]["createdAtUtc"]
        )
        self.assertEqual(
            _rfc3339(self.now + timedelta(days=30)),
            result["effectivePolicy"]["expiresAtUtc"],
        )

    def test_second_activation_derives_predecessor_from_current_state(
        self,
    ) -> None:
        self._activate_policy()
        result = self._activate_policy(now=self.now + timedelta(hours=1))

        self.assertEqual("policy:2", result["effectivePolicy"]["revisionId"])
        self.assertEqual("policy:1", result["effectivePolicy"]["replacesRevisionId"])

    def test_rejects_expiry_beyond_policy_schema_maximum(self) -> None:
        caps_path = self.scratch / "caps.json"
        self._write_json(caps_path, _caps_document())

        code, _stdout, stderr = self._run(
            [
                "policy-activate",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--expected-revision", "0",
                "--caps", str(caps_path),
                "--expires-in-days", str(MAX_EXPIRY_DAYS + 1),
                "--actor", "github:radical",
                "--now", self._now_arg(),
            ]
        )

        self.assertNotEqual(0, code)
        error = json.loads(stderr)
        # Same wrapping as policy-append: policy-activate's derived document
        # is validated inside coordinator_state.append_policy_revision, so
        # a Task 1 schema rejection surfaces as coordinator-state-error.
        self.assertEqual("coordinator-state-error", error["code"])

    def test_stale_revision_returns_typed_error_with_refreshed_projection(
        self,
    ) -> None:
        self._activate_policy()
        caps_path = self.scratch / "caps.json"
        self._write_json(caps_path, _caps_document())

        code, _stdout, stderr = self._run(
            [
                "policy-activate",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--expected-revision", "0",  # stale
                "--caps", str(caps_path),
                "--expires-in-days", "30",
                "--actor", "github:radical",
                "--now", self._now_arg(self.now + timedelta(hours=1)),
            ]
        )

        self.assertEqual(2, code)
        error = json.loads(stderr)
        self.assertEqual("stale-view", error["code"])
        self.assertEqual(1, error["projection"]["stateRevision"])

    def test_rejects_caps_document_with_unexpected_fields(self) -> None:
        caps_path = self.scratch / "caps.json"
        malformed = _caps_document()
        malformed["revisionId"] = "policy:99"  # untrusted internal field
        self._write_json(caps_path, malformed)

        code, _stdout, stderr = self._run(
            [
                "policy-activate",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--expected-revision", "0",
                "--caps", str(caps_path),
                "--expires-in-days", "30",
                "--actor", "github:radical",
                "--now", self._now_arg(),
            ]
        )

        self.assertNotEqual(0, code)
        error = json.loads(stderr)
        self.assertTrue(error["error"])

    def test_cap_and_expiry_refresh_preserves_denied_action_ids_and_targets(
        self,
    ) -> None:
        # Kill switches (deniedActionIds/deniedTargets) are the standing
        # policy's own emergency brake. A routine cap/expiry refresh must
        # never silently clear them: refreshing operationClasses and the
        # expiry window is expected, but a denied action id or target must
        # remain denied across that refresh with no operator input at all.
        denied_by_id_action = "action:denied-by-id"
        denied_by_target_action = "action:denied-by-target"
        self._write_proposals(
            [
                _comment_proposal(action_id=denied_by_id_action, issue_number=1),
                _comment_proposal(action_id=denied_by_target_action, issue_number=2),
            ],
            unchanged_issue_numbers=[],
        )
        document_path = self.scratch / "policy.json"
        self._write_json(
            document_path,
            _policy_document(
                revision=1,
                replaces=None,
                created_at_utc=self.now,
                expires_at_utc=self.now + timedelta(days=30),
                denied_action_ids=[denied_by_id_action],
                denied_targets=["issue:2"],
            ),
        )
        code, _stdout, stderr = self._run(
            [
                "policy-append",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--expected-revision", "0",
                "--document", str(document_path),
            ]
        )
        self.assertEqual(0, code, stderr)

        def _assert_both_actions_are_denied() -> None:
            selection_path = self._select()
            selection = json.loads(selection_path.read_text(encoding="utf-8"))
            self.assertEqual([], selection["automaticActionIds"])
            self.assertEqual([], selection["exactActionIds"])
            candidates_by_action_id = {
                candidate["actionId"]: candidate
                for candidate in selection["candidates"]
            }
            self.assertEqual(
                "denied", candidates_by_action_id[denied_by_id_action]["status"]
            )
            self.assertEqual(
                "policy-denied-action-id",
                candidates_by_action_id[denied_by_id_action]["reason"],
            )
            self.assertEqual(
                "denied", candidates_by_action_id[denied_by_target_action]["status"]
            )
            self.assertEqual(
                "policy-denied-target",
                candidates_by_action_id[denied_by_target_action]["reason"],
            )

            grant_output_path = self.scratch / "grant.json"
            code, stdout, stderr = self._run(
                [
                    "grant-next",
                    "--repository", self.repository,
                    "--state-dir", str(self.state_dir),
                    "--proposals", str(self.proposals_path),
                    "--selection", str(selection_path),
                    "--output", str(grant_output_path),
                    "--now", self._now_arg(),
                ]
            )
            self.assertEqual(0, code, stderr)
            result = json.loads(stdout)
            self.assertFalse(result["granted"])
            self.assertFalse(grant_output_path.exists())

        _assert_both_actions_are_denied()

        refreshed = self._activate_policy(expires_in_days=45)
        self.assertEqual("policy:2", refreshed["effectivePolicy"]["revisionId"])
        self.assertEqual(
            [denied_by_id_action],
            refreshed["effectivePolicy"]["deniedActionIds"],
        )
        self.assertEqual(
            ["issue:2"], refreshed["effectivePolicy"]["deniedTargets"]
        )
        self.assertEqual(
            _rfc3339(self.now + timedelta(days=45)),
            refreshed["effectivePolicy"]["expiresAtUtc"],
        )

        code, stdout, stderr = self._run(
            [
                "projection",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--now", self._now_arg(),
            ]
        )
        self.assertEqual(0, code, stderr)
        projection = json.loads(stdout)
        self.assertEqual(
            [denied_by_id_action],
            projection["effectivePolicy"]["deniedActionIds"],
        )
        self.assertEqual(
            ["issue:2"], projection["effectivePolicy"]["deniedTargets"]
        )

        _assert_both_actions_are_denied()


class PolicyPauseRevokeCommandTests(CoordinatorCliTestCase):
    def test_pause_transitions_status_and_preserves_caps(self) -> None:
        activated = self._activate_policy()

        code, stdout, stderr = self._run(
            [
                "policy-pause",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--expected-revision", str(self._current_state_revision()),
                "--actor", "github:radical",
                "--now", self._now_arg(self.now + timedelta(hours=1)),
            ]
        )

        self.assertEqual(0, code, stderr)
        result = json.loads(stdout)
        paused = result["effectivePolicy"]
        self.assertEqual("paused", paused["status"])
        self.assertEqual("policy:2", paused["revisionId"])
        self.assertEqual("policy:1", paused["replacesRevisionId"])
        self.assertEqual(
            activated["effectivePolicy"]["operationClasses"],
            paused["operationClasses"],
        )
        self.assertEqual(
            activated["effectivePolicy"]["expiresAtUtc"], paused["expiresAtUtc"]
        )

    def test_pause_requires_a_current_policy(self) -> None:
        code, _stdout, stderr = self._run(
            [
                "policy-pause",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--expected-revision", "0",
                "--actor", "github:radical",
                "--now", self._now_arg(),
            ]
        )

        self.assertNotEqual(0, code)
        error = json.loads(stderr)
        self.assertTrue(error["error"])

    def test_pause_stale_revision(self) -> None:
        self._activate_policy()

        code, _stdout, stderr = self._run(
            [
                "policy-pause",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--expected-revision", "0",  # stale
                "--actor", "github:radical",
                "--now", self._now_arg(self.now + timedelta(hours=1)),
            ]
        )

        self.assertEqual(2, code)
        self.assertEqual("stale-view", json.loads(stderr)["code"])

    def test_revoke_transitions_status_and_preserves_caps(self) -> None:
        activated = self._activate_policy()

        code, stdout, stderr = self._run(
            [
                "policy-revoke",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--expected-revision", str(self._current_state_revision()),
                "--actor", "github:radical",
                "--now", self._now_arg(self.now + timedelta(hours=1)),
            ]
        )

        self.assertEqual(0, code, stderr)
        revoked = json.loads(stdout)["effectivePolicy"]
        self.assertEqual("revoked", revoked["status"])
        self.assertEqual(
            activated["effectivePolicy"]["operationClasses"],
            revoked["operationClasses"],
        )

    def test_revoke_stale_revision(self) -> None:
        self._activate_policy()

        code, _stdout, stderr = self._run(
            [
                "policy-revoke",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--expected-revision", "0",  # stale
                "--actor", "github:radical",
                "--now", self._now_arg(self.now + timedelta(hours=1)),
            ]
        )

        self.assertEqual(2, code)
        self.assertEqual("stale-view", json.loads(stderr)["code"])


# ---------------------------------------------------------------------------
# policy-preview
# ---------------------------------------------------------------------------


class PolicyPreviewCommandTests(CoordinatorCliTestCase):
    def test_computes_selection_without_appending_to_the_ledger(self) -> None:
        action_id = "snapshot:test:1:issue:1:comment"
        self._write_proposals(
            [_comment_proposal(action_id=action_id, issue_number=1)],
            unchanged_issue_numbers=[],
        )
        caps_path = self.scratch / "caps.json"
        self._write_json(caps_path, _caps_document())
        before = self._current_state_revision()

        code, stdout, stderr = self._run(
            [
                "policy-preview",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--expected-revision", str(before),
                "--caps", str(caps_path),
                "--expires-in-days", "30",
                "--proposals", str(self.proposals_path),
                "--run-id", self._cycle_run_id(),
                "--now", self._now_arg(),
            ]
        )

        self.assertEqual(0, code, stderr)
        result = json.loads(stdout)
        self.assertEqual([action_id], result["selection"]["automaticActionIds"])
        self.assertIn("maximumWriteExposure", result["selection"])
        after = self._current_state_revision()
        self.assertEqual(before, after)

    def test_rejects_run_id_outside_proposal_cycle_namespace(self) -> None:
        self._write_proposals(
            [_comment_proposal(action_id="a:1", issue_number=1)],
            unchanged_issue_numbers=[],
        )
        caps_path = self.scratch / "caps.json"
        self._write_json(caps_path, _caps_document())

        code, _stdout, stderr = self._run(
            [
                "policy-preview",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--expected-revision", "0",
                "--caps", str(caps_path),
                "--expires-in-days", "30",
                "--proposals", str(self.proposals_path),
                "--run-id", "arbitrary-budget-namespace",
                "--now", self._now_arg(),
            ]
        )

        self.assertNotEqual(0, code)
        self.assertEqual("invalid-argument", json.loads(stderr)["code"])

    def test_stale_expected_revision_is_a_view_consistency_failure(self) -> None:
        self._activate_policy()
        caps_path = self.scratch / "caps.json"
        self._write_json(caps_path, _caps_document())
        self._write_proposals(
            [_comment_proposal(action_id="a:1", issue_number=1)],
            unchanged_issue_numbers=[],
        )

        code, _stdout, stderr = self._run(
            [
                "policy-preview",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--expected-revision", "0",  # stale: ledger is at 1
                "--caps", str(caps_path),
                "--expires-in-days", "30",
                "--proposals", str(self.proposals_path),
                "--run-id", self._cycle_run_id(),
                "--now", self._now_arg(),
            ]
        )

        self.assertEqual(2, code)
        error = json.loads(stderr)
        self.assertEqual("stale-view", error["code"])
        self.assertEqual(1, error["projection"]["stateRevision"])

    def test_rejects_expiry_beyond_policy_schema_maximum(self) -> None:
        caps_path = self.scratch / "caps.json"
        self._write_json(caps_path, _caps_document())
        self._write_proposals(
            [_comment_proposal(action_id="a:1", issue_number=1)],
            unchanged_issue_numbers=[],
        )

        code, _stdout, stderr = self._run(
            [
                "policy-preview",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--expected-revision", "0",
                "--caps", str(caps_path),
                "--expires-in-days", str(MAX_EXPIRY_DAYS + 1),
                "--proposals", str(self.proposals_path),
                "--run-id", self._cycle_run_id(),
                "--now", self._now_arg(),
            ]
        )

        self.assertNotEqual(0, code)
        self.assertEqual(
            "operation-policy-error", json.loads(stderr)["code"]
        )


# ---------------------------------------------------------------------------
# decision-set / decision-clear
# ---------------------------------------------------------------------------


class DecisionCommandTests(CoordinatorCliTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.action_id = "snapshot:test:1:issue:9:close"
        self._write_proposals(
            [_close_proposal(action_id=self.action_id, issue_number=9)],
            unchanged_issue_numbers=[],
        )
        # close-issue is not enabled, so the action is initially denied --
        # exactly the "operation-disabled" condition approve-once exists to
        # bypass -- without requiring a second, unrelated policy class.
        self._activate_policy(enabled_classes=frozenset({"create-comment"}))

    def test_approve_once_and_reject_once_round_trip(self) -> None:
        result = self._set_decision(action_id=self.action_id, decision="approve-once")
        self.assertEqual(1, len(result["exactDecisions"]))
        self.assertEqual("approve-once", result["exactDecisions"][0]["decision"])

        result = self._set_decision(action_id=self.action_id, decision="reject-once")
        self.assertEqual("reject-once", result["exactDecisions"][0]["decision"])

    def test_invalid_decision_value_is_rejected_by_argparse_choices(self) -> None:
        code, _stdout, stderr = self._run(
            [
                "decision-set",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--expected-revision", str(self._current_state_revision()),
                "--proposals", str(self.proposals_path),
                "--action-id", self.action_id,
                "--decision", "approve-forever",
                "--actor", "github:radical",
                "--now", self._now_arg(),
            ]
        )

        self.assertNotEqual(0, code)
        error = json.loads(stderr)
        self.assertTrue(error["error"])

    def test_cannot_supply_proposal_digest_or_expiry_directly(self) -> None:
        code, _stdout, stderr = self._run(
            [
                "decision-set",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--expected-revision", str(self._current_state_revision()),
                "--proposals", str(self.proposals_path),
                "--action-id", self.action_id,
                "--decision", "approve-once",
                "--actor", "github:radical",
                "--now", self._now_arg(),
                "--proposal-digest", "sha256:" + "0" * 64,
            ]
        )

        self.assertNotEqual(0, code)
        error = json.loads(stderr)
        self.assertTrue(error["error"])

    def test_stale_revision(self) -> None:
        code, _stdout, stderr = self._run(
            [
                "decision-set",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--expected-revision", "0",  # stale: activation already at 1
                "--proposals", str(self.proposals_path),
                "--action-id", self.action_id,
                "--decision", "approve-once",
                "--actor", "github:radical",
                "--now", self._now_arg(),
            ]
        )

        self.assertEqual(2, code)
        self.assertEqual("stale-view", json.loads(stderr)["code"])

    def test_clear_removes_an_effective_decision(self) -> None:
        self._set_decision(action_id=self.action_id, decision="approve-once")

        code, stdout, stderr = self._run(
            [
                "decision-clear",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--expected-revision", str(self._current_state_revision()),
                "--proposals", str(self.proposals_path),
                "--action-id", self.action_id,
                "--actor", "github:radical",
                "--now", self._now_arg(),
            ]
        )

        self.assertEqual(0, code, stderr)
        self.assertEqual([], json.loads(stdout)["exactDecisions"])

    def test_clear_requires_an_effective_decision(self) -> None:
        code, _stdout, stderr = self._run(
            [
                "decision-clear",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--expected-revision", str(self._current_state_revision()),
                "--proposals", str(self.proposals_path),
                "--action-id", self.action_id,
                "--actor", "github:radical",
                "--now", self._now_arg(),
            ]
        )

        self.assertNotEqual(0, code)
        error = json.loads(stderr)
        self.assertTrue(error["error"])


# ---------------------------------------------------------------------------
# select
# ---------------------------------------------------------------------------


class SelectCommandTests(CoordinatorCliTestCase):
    def test_managed_coverage_blocker_survives_live_reselection(self) -> None:
        self._activate_policy()
        action_id = "snapshot:test:1:issue:1:comment"
        document = self._write_proposals(
            [_comment_proposal(action_id=action_id, issue_number=1)],
            unchanged_issue_numbers=[],
        )
        capability = document["productionPilotCapability"]
        assert isinstance(capability, dict)
        capability["managedItemCoverage"] = {
            "schemaVersion": 1,
            "valid": False,
            "blockers": ["issue:1:uncovered"],
        }
        self._write_json(self.proposals_path, document)

        output_path = self._select()

        selection = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual([], selection["selectedActionIds"])
        self.assertTrue(selection["mutationBlocked"])
        self.assertEqual(
            ["issue:1:uncovered"],
            selection["mutationBlockers"],
        )
        self.assertEqual(
            {"thisRun": 0, "rolling24h": 0},
            selection["maximumWriteExposure"],
        )

    def test_rejects_run_id_not_bound_to_proposal_snapshot(self) -> None:
        self._activate_policy()
        self._write_proposals(
            [_comment_proposal(action_id="a:1", issue_number=1)],
            unchanged_issue_numbers=[],
        )
        output_path = self.scratch / "selection.json"

        code, _stdout, stderr = self._run(
            [
                "select",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--proposals", str(self.proposals_path),
                "--run-id", "fresh-budget-namespace",
                "--output", str(output_path),
                "--now", self._now_arg(),
            ]
        )

        self.assertNotEqual(0, code)
        error = json.loads(stderr)
        self.assertEqual("invalid-argument", error["code"])
        self.assertIn("proposal snapshot", error["message"])
        self.assertFalse(output_path.exists())

    def test_writes_atomic_owner_only_selection_output(self) -> None:
        self._activate_policy()
        action_id = "snapshot:test:1:issue:1:comment"
        self._write_proposals(
            [_comment_proposal(action_id=action_id, issue_number=1)],
            unchanged_issue_numbers=[],
        )

        output_path = self._select()

        mode = stat.S_IMODE(output_path.stat().st_mode)
        self.assertEqual(0o600, mode)
        written = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual([action_id], written["automaticActionIds"])
        self.assertEqual(self._cycle_run_id(), written["runId"])

    def test_rejects_malformed_proposals_document(self) -> None:
        self._write_json(self.proposals_path, {"schemaVersion": 2})
        output_path = self.scratch / "selection.json"

        code, _stdout, stderr = self._run(
            [
                "select",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--proposals", str(self.proposals_path),
                "--run-id", "cycle:malformed",
                "--output", str(output_path),
                "--now", self._now_arg(),
            ]
        )

        self.assertNotEqual(0, code)
        error = json.loads(stderr)
        self.assertTrue(error["error"])
        self.assertFalse(output_path.exists())

    def test_rejects_symlinked_output_path(self) -> None:
        self._activate_policy()
        self._write_proposals(
            [_comment_proposal(action_id="a:1", issue_number=1)],
            unchanged_issue_numbers=[],
        )
        real_output = self.scratch / "real-selection.json"
        real_output.write_text("{}", encoding="utf-8")
        link_output = self.scratch / "linked-selection.json"
        link_output.symlink_to(real_output)

        code, _stdout, stderr = self._run(
            [
                "select",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--proposals", str(self.proposals_path),
                "--run-id", self._cycle_run_id(),
                "--output", str(link_output),
                "--now", self._now_arg(),
            ]
        )

        self.assertNotEqual(0, code)
        error = json.loads(stderr)
        self.assertTrue(error["error"])


# ---------------------------------------------------------------------------
# grant-next
# ---------------------------------------------------------------------------


class GrantNextCommandTests(CoordinatorCliTestCase):
    def test_fork_policy_execution_uses_no_protected_override_and_replays_once(self) -> None:
        from unittest.mock import patch
        import execute_actions as execute_script
        from ci_shepherd.github_actor import GitHubActorClient
        from tests.test_actor import ScriptedActorClient

        self.repository = "owner/fork"
        self.now = datetime.now(UTC).replace(microsecond=0)
        proposal = _comment_proposal(action_id="action:fork-comment", issue_number=1)
        proposal["issueUrl"] = f"https://github.com/{self.repository}/issues/1"
        document = self._write_proposals([proposal], unchanged_issue_numbers=[])
        document.update(repository=self.repository, shepherdAuthor="ankj", generatedAtUtc=self._now_arg())
        self._write_json(self.proposals_path, document)
        self._activate_policy()
        selection = self._select()
        grant = self.scratch / "fork-grant.json"
        code, _, stderr = self._run([
            "grant-next", "--repository", self.repository, "--state-dir", str(self.state_dir),
            "--proposals", str(self.proposals_path), "--selection", str(selection),
            "--output", str(grant), "--now", self._now_arg(),
        ])
        self.assertEqual(0, code, stderr)
        issue = {
            "number": 1, "state": "open", "updated_at": proposal["sourceEvidenceFingerprint"]["issueUpdatedAt"],
            "labels": [{"name": "ci-failure-cause"}], "assignees": [],
        }
        comment = {"id": 900, "body": proposal["body"], "user": {"login": "ankj"}}
        client = ScriptedActorClient(
            issues=[issue, issue], comments=[[], [comment]], single_comments=[comment],
        )

        def make_client(**options):
            GitHubActorClient(**options)
            return client

        args = [
            "--proposals", str(self.proposals_path), "--authorization", str(grant),
            "--state-dir", str(self.state_dir), "--action-id", proposal["actionId"],
            "--policy-selection", str(selection), "--autonomous-policy", "--execute",
        ]
        first = io.StringIO()
        with patch.object(execute_script, "GitHubActorClient", side_effect=make_client) as factory:
            with contextlib.redirect_stdout(first):
                self.assertEqual(0, execute_script.main(args))
            calls = list(client.calls)
            replay = io.StringIO()
            with contextlib.redirect_stdout(replay):
                self.assertEqual(0, execute_script.main(args))
        self.assertEqual("executed", json.loads(first.getvalue())["outcome"])
        self.assertEqual(first.getvalue(), replay.getvalue())
        self.assertEqual(calls, client.calls)
        factory.assert_called_once()
        self.assertEqual(set(), factory.call_args.kwargs["protected_comment_repositories"])
        self.assertEqual(set(), factory.call_args.kwargs["protected_closure_repositories"])
        self.assertEqual(set(), factory.call_args.kwargs["protected_delegation_repositories"])

    def test_fork_grant_next_preserves_explicit_narrow_task_capacity(self) -> None:
        from ci_shepherd.authorization import load_authorized_execution
        from tests.test_actor import _assignment_proposals

        self.repository = "owner/repo"
        proposal = _assignment_proposals()["proposals"][0]
        document = self._write_proposals([proposal], unchanged_issue_numbers=[])
        document["repository"] = self.repository
        self._write_json(self.proposals_path, document)
        self._activate_policy(enabled_classes=frozenset({"delegate-copilot"}))
        selection_path = self._select()
        output_path = self.scratch / "fork-grant.json"
        args = [
            "grant-next", "--repository", self.repository,
            "--state-dir", str(self.state_dir), "--proposals", str(self.proposals_path),
            "--selection", str(selection_path), "--output", str(output_path),
            "--now", self._now_arg(),
            "--max-running-copilot-tasks", "1",
            "--max-copilot-starts-per-rolling-24h", "1",
            "--max-open-delegated-prs", "1",
        ]
        code, stdout, stderr = self._run(args)
        self.assertEqual(0, code, stderr)
        self.assertTrue(json.loads(stdout)["granted"])
        execution = load_authorized_execution(
            self.proposals_path, output_path, state_dir=self.state_dir,
            action_id=proposal["actionId"], allow_autonomous_policy=True,
            policy_selection_path=selection_path, now=self.now,
        )
        budget = execution.grant.budget
        self.assertEqual((1, 1, 1), (
            budget.max_running_copilot_tasks, budget.max_copilot_starts_per_rolling_24h,
            budget.max_open_delegated_prs,
        ))
        invalid_path = self.scratch / "over-cap-grant.json"
        invalid = list(args)
        invalid[invalid.index("--output") + 1] = str(invalid_path)
        invalid[invalid.index("--max-running-copilot-tasks") + 1] = "4"
        self.assertNotEqual(0, self._run(invalid)[0])
        self.assertFalse(invalid_path.exists())

    def test_prefers_first_exact_action_over_automatic_deterministically(
        self,
    ) -> None:
        automatic_action_id = "snapshot:test:1:issue:1:comment"
        exact_action_id = "snapshot:test:1:issue:2:close"
        self._write_proposals(
            [
                _comment_proposal(action_id=automatic_action_id, issue_number=1),
                _close_proposal(action_id=exact_action_id, issue_number=2),
            ],
            unchanged_issue_numbers=[],
        )
        # Only create-comment is licensed; close-issue starts denied
        # ("operation-disabled") until promoted by an approve-once decision.
        self._activate_policy(enabled_classes=frozenset({"create-comment"}))
        self._set_decision(action_id=exact_action_id, decision="approve-once")
        selection_path = self._select()
        selection = json.loads(selection_path.read_text(encoding="utf-8"))
        self.assertEqual([automatic_action_id], selection["automaticActionIds"])
        self.assertEqual([exact_action_id], selection["exactActionIds"])
        output_path = self.scratch / "grant.json"

        code, stdout, stderr = self._run(
            [
                "grant-next",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--proposals", str(self.proposals_path),
                "--selection", str(selection_path),
                "--output", str(output_path),
                "--now", self._now_arg(),
            ]
        )

        self.assertEqual(0, code, stderr)
        result = json.loads(stdout)
        self.assertTrue(result["granted"])
        self.assertEqual(exact_action_id, result["actionId"])
        grant = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual((exact_action_id,), tuple(grant["allowedActionIds"]))
        self.assertTrue(grant["autonomousPolicy"])
        mode = stat.S_IMODE(output_path.stat().st_mode)
        self.assertEqual(0o600, mode)

    def test_falls_back_to_first_automatic_action_when_no_exact(self) -> None:
        action_id = "snapshot:test:1:issue:1:comment"
        self._write_proposals(
            [_comment_proposal(action_id=action_id, issue_number=1)],
            unchanged_issue_numbers=[],
        )
        self._activate_policy()
        selection_path = self._select()
        output_path = self.scratch / "grant.json"

        code, stdout, stderr = self._run(
            [
                "grant-next",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--proposals", str(self.proposals_path),
                "--selection", str(selection_path),
                "--output", str(output_path),
                "--now", self._now_arg(),
            ]
        )

        self.assertEqual(0, code, stderr)
        result = json.loads(stdout)
        self.assertTrue(result["granted"])
        self.assertEqual(action_id, result["actionId"])

        grant = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertLessEqual(
            (
                datetime.fromisoformat(
                    grant["expiresAtUtc"].replace("Z", "+00:00")
                )
                - datetime.fromisoformat(
                    grant["issuedAtUtc"].replace("Z", "+00:00")
                )
            ),
            timedelta(minutes=15),
        )

    def test_emits_explicit_no_action_result_when_nothing_is_eligible(
        self,
    ) -> None:
        self._write_proposals(
            [_comment_proposal(action_id="a:1", issue_number=1)],
            unchanged_issue_numbers=[],
        )
        # No policy has ever been activated: the only candidate is denied
        # ("no-active-policy") and there is no approve-once decision to
        # promote it, so nothing is selected at all.
        selection_path = self._select()
        output_path = self.scratch / "grant.json"

        code, stdout, stderr = self._run(
            [
                "grant-next",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--proposals", str(self.proposals_path),
                "--selection", str(selection_path),
                "--output", str(output_path),
                "--now", self._now_arg(),
            ]
        )

        self.assertEqual(0, code, stderr)
        result = json.loads(stdout)
        self.assertFalse(result["granted"])
        self.assertIn("reason", result)
        self.assertFalse(output_path.exists())

    def test_no_action_result_removes_a_stale_grant_left_at_output(self) -> None:
        # grant-next's --output is meant to hold at most one currently-valid
        # grant. A prior run may have written a real grant there; if a
        # later run finds nothing eligible, that stale grant must not be
        # left behind for a caller to mistakenly treat as still valid.
        action_id = "snapshot:test:1:issue:1:comment"
        self._write_proposals(
            [_comment_proposal(action_id=action_id, issue_number=1)],
            unchanged_issue_numbers=[],
        )
        self._activate_policy()
        selection_path = self._select()
        output_path = self.scratch / "grant.json"

        code, stdout, stderr = self._run(
            [
                "grant-next",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--proposals", str(self.proposals_path),
                "--selection", str(selection_path),
                "--output", str(output_path),
                "--now", self._now_arg(),
            ]
        )
        self.assertEqual(0, code, stderr)
        self.assertTrue(json.loads(stdout)["granted"])
        self.assertTrue(output_path.exists())

        # Revoke the policy: the same action id is now denied
        # ("no-active-policy"), so a fresh selection has no eligible action.
        code, _stdout, stderr = self._run(
            [
                "policy-revoke",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--expected-revision", str(self._current_state_revision()),
                "--actor", "github:radical",
                "--now", self._now_arg(),
            ]
        )
        self.assertEqual(0, code, stderr)
        selection_path = self._select()

        code, stdout, stderr = self._run(
            [
                "grant-next",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--proposals", str(self.proposals_path),
                "--selection", str(selection_path),
                "--output", str(output_path),
                "--now", self._now_arg(),
            ]
        )

        self.assertEqual(0, code, stderr)
        result = json.loads(stdout)
        self.assertFalse(result["granted"])
        self.assertFalse(
            output_path.exists(),
            "grant-next must not leave a prior valid grant behind when "
            "nothing is eligible",
        )

    def test_rejects_symlinked_output_without_deleting_its_target_when_no_action_is_permitted(
        self,
    ) -> None:
        self._write_proposals(
            [_comment_proposal(action_id="a:1", issue_number=1)],
            unchanged_issue_numbers=[],
        )
        # No policy has ever been activated, so the only candidate is
        # denied and grant-next takes the no-action path, which is exactly
        # the path that must reject (rather than unlink through) a
        # symlinked --output.
        selection_path = self._select()
        real_output_path = self.scratch / "real-grant.json"
        real_output_path.write_text("not a grant, just a sentinel", encoding="utf-8")
        symlinked_output_path = self.scratch / "linked-grant.json"
        symlinked_output_path.symlink_to(real_output_path)

        code, _stdout, stderr = self._run(
            [
                "grant-next",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--proposals", str(self.proposals_path),
                "--selection", str(selection_path),
                "--output", str(symlinked_output_path),
                "--now", self._now_arg(),
            ]
        )

        self.assertNotEqual(0, code)
        error = json.loads(stderr)
        self.assertTrue(error["error"])
        self.assertTrue(real_output_path.exists())
        self.assertEqual(
            "not a grant, just a sentinel",
            real_output_path.read_text(encoding="utf-8"),
        )

    def test_rejects_selection_repository_mismatch(self) -> None:
        self._write_proposals(
            [_comment_proposal(action_id="a:1", issue_number=1)],
            unchanged_issue_numbers=[],
        )
        self._activate_policy()
        selection_path = self._select()
        selection = json.loads(selection_path.read_text(encoding="utf-8"))
        selection["repository"] = "someone/else"
        selection_path.write_text(json.dumps(selection), encoding="utf-8")
        output_path = self.scratch / "grant.json"

        code, _stdout, stderr = self._run(
            [
                "grant-next",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--proposals", str(self.proposals_path),
                "--selection", str(selection_path),
                "--output", str(output_path),
                "--now", self._now_arg(),
            ]
        )

        self.assertNotEqual(0, code)
        error = json.loads(stderr)
        self.assertTrue(error["error"])
        self.assertFalse(output_path.exists())


# ---------------------------------------------------------------------------
# Cross-cutting: import/parser surface, help, and state-mutation invariants
# ---------------------------------------------------------------------------


class CrossCuttingTests(CoordinatorCliTestCase):
    def test_source_imports_no_network_client(self) -> None:
        # coordinator.py legitimately spells "github:<owner>" as a policy
        # actor-identity sentinel (Task 1's own schema convention), so this
        # asserts the import surface -- no networking module and no
        # GitHub-client-shaped module is ever imported -- rather than
        # scanning the whole source for the substring "github".
        source = Path(coordinator.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
        lowered_modules = {module.lower() for module in imported_modules}
        self.assertTrue(imported_modules, "expected coordinator.py to import something")
        for forbidden in ("github", "requests", "urllib", "http"):
            self.assertFalse(
                any(forbidden in module for module in lowered_modules),
                f"unexpected {forbidden!r}-related import among {imported_modules!r}",
            )

    def test_parser_surface_has_no_token_or_network_flags(self) -> None:
        parser = coordinator._build_parser()
        option_strings: list[str] = []

        def _collect(candidate: argparse.ArgumentParser) -> None:
            for action in candidate._actions:
                option_strings.extend(action.option_strings)
                if isinstance(action, argparse._SubParsersAction):
                    for sub_parser in action.choices.values():
                        _collect(sub_parser)

        _collect(parser)
        lowered_options = [option.lower() for option in option_strings]
        self.assertTrue(lowered_options)
        self.assertFalse(any("token" in option for option in lowered_options))
        self.assertFalse(any("github" in option for option in lowered_options))
        self.assertFalse(any("url" in option for option in lowered_options))

    def test_help_exits_zero_and_lists_every_subcommand(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as raised:
                coordinator.main(["--help"])
        self.assertEqual(0, raised.exception.code)
        help_text = stdout.getvalue()
        for command in (
            "projection",
            "policy-append",
            "policy-activate",
            "policy-pause",
            "policy-revoke",
            "policy-preview",
            "decision-set",
            "decision-clear",
            "select",
            "grant-next",
        ):
            self.assertIn(command, help_text)

    def test_every_state_mutating_command_requires_expected_revision(
        self,
    ) -> None:
        base_flags_by_command = {
            "policy-append": ["--document", str(self.scratch / "policy.json")],
            "policy-activate": [
                "--caps", str(self.scratch / "caps.json"),
                "--expires-in-days", "30",
                "--actor", "github:radical",
                "--now", self._now_arg(),
            ],
            "policy-pause": ["--actor", "github:radical", "--now", self._now_arg()],
            "policy-revoke": ["--actor", "github:radical", "--now", self._now_arg()],
            "policy-preview": [
                "--caps", str(self.scratch / "caps.json"),
                "--expires-in-days", "30",
                "--proposals", str(self.proposals_path),
                "--run-id", "cycle:missing",
                "--now", self._now_arg(),
            ],
            "decision-set": [
                "--proposals", str(self.proposals_path),
                "--action-id", "a:1",
                "--decision", "approve-once",
                "--actor", "github:radical",
                "--now", self._now_arg(),
            ],
            "decision-clear": [
                "--proposals", str(self.proposals_path),
                "--action-id", "a:1",
                "--actor", "github:radical",
                "--now", self._now_arg(),
            ],
        }
        for command, extra_flags in base_flags_by_command.items():
            with self.subTest(command=command):
                code, _stdout, stderr = self._run(
                    [
                        command,
                        "--repository", self.repository,
                        "--state-dir", str(self.state_dir),
                        *extra_flags,
                    ]
                )
                self.assertNotEqual(0, code)
                error = json.loads(stderr)
                self.assertTrue(error["error"])

    def test_corrupt_ledger_never_returns_success_shaped_empty_output(
        self,
    ) -> None:
        self._activate_policy()
        ledger_path = self.state_dir / "coordinator" / "policy-events.jsonl"
        with ledger_path.open("a", encoding="utf-8") as handle:
            handle.write("{not valid json\n")

        code, stdout, stderr = self._run(
            [
                "projection",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--now", self._now_arg(),
            ]
        )

        self.assertNotEqual(0, code)
        self.assertEqual("", stdout)
        error = json.loads(stderr)
        self.assertTrue(error["error"])

    def test_legacy_ledger_written_before_coordinator_cli_existed_is_readable(
        self,
    ) -> None:
        # Simulate state produced by direct CoordinatorStateStore usage (for
        # example, by another Task 1-5 script) before coordinator.py ever
        # ran against this state directory.
        from ci_shepherd.coordinator_state import CoordinatorStateStore

        def _no_durable_intent(_action_id: str) -> bool:
            return False

        legacy_store = CoordinatorStateStore(
            self.state_dir, durable_intent_reader=_no_durable_intent
        )
        legacy_store.append_policy_revision(
            repository=self.repository,
            expected_revision=0,
            document=_policy_document(
                revision=1,
                replaces=None,
                created_at_utc=self.now - timedelta(days=1),
                expires_at_utc=self.now + timedelta(days=29),
            ),
        )

        code, stdout, stderr = self._run(
            [
                "projection",
                "--repository", self.repository,
                "--state-dir", str(self.state_dir),
                "--now", self._now_arg(),
            ]
        )

        self.assertEqual(0, code, stderr)
        result = json.loads(stdout)
        self.assertEqual("policy-active", result["stage"])
        self.assertEqual(1, result["stateRevision"])


if __name__ == "__main__":
    unittest.main()
