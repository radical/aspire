"""Tests for ci_shepherd.policy_selection: policy-aware action selection.

These tests exercise the authorization boundary directly: every "fails
closed" assertion below is checking that a malformed or under-licensed
input produces no executable selection, not merely that *something* is
returned. See docs/superpowers/plans/2026-09-03-ci-shepherd-autonomous-policy.md
(Task 3) for the full specification these tests are written against.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ci_shepherd import policy_selection as ps
from ci_shepherd.comment_selection import build_comment_selection
from ci_shepherd.models import stable_json
from ci_shepherd.operation_policy import DEFAULT_CAPS, OPERATION_CLASSES

REPOSITORY = "microsoft/aspire"
FIXTURES_DIR = (
    Path(__file__).resolve().parent / "fixtures" / "production-policy-selection-20260903"
)
PROPOSALS_FIXTURE_PATH = FIXTURES_DIR / "action-proposals.json"
ORIGINAL_SELECTION_FIXTURE_PATH = FIXTURES_DIR / "original-comment-selection.json"

# Expected SHA-256 of the exact frozen fixture bytes copied from the manual
# run. Asserting these directly makes any accidental fixture drift a loud
# test failure rather than a silent behavior change.
PROPOSALS_FIXTURE_SHA256 = "fed8a27dea6b51c7b531ac2856b18796a8572e0c70adc0e012f66b64ab6280a7"
ORIGINAL_SELECTION_FIXTURE_SHA256 = "d1ab449ef58332b4ff0a318228107f3fd3dca07b64ea2b1412be775275b7df55"

_TYPED_STATUSES = frozenset(
    {
        "automatic",
        "exact",
        "denied",
        "exhausted",
        "ineligible",
        "superseded",
        "outside-policy-surface",
    }
)


def _sha256_hex(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _digest_of(document: object) -> str:
    return "sha256:" + hashlib.sha256(stable_json(document).encode("utf-8")).hexdigest()


def _rfc3339(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _eligibility(eligible: bool) -> dict[str, object]:
    if eligible:
        return {
            "eligible": True,
            "evidenceBasis": "issue-state",
            "ciLabels": ["ci-failure-cause"],
            "occurrenceCount": 1,
            "collectionComplete": True,
            "unavailableEvidenceIds": [],
            "untrustedReferenceEvidenceIds": [],
            "blockingReasons": [],
        }
    return {
        "eligible": False,
        "evidenceBasis": "issue-state",
        "ciLabels": [],
        "occurrenceCount": 0,
        "collectionComplete": True,
        "unavailableEvidenceIds": [],
        "untrustedReferenceEvidenceIds": [],
        "blockingReasons": ["missing-ci-label"],
    }


def _comment_proposal(
    *,
    action_id: str,
    issue_number: int,
    operation: str = "create-comment",
    eligible: bool = True,
    comment_id: int = 1,
    depends_on: str | None = None,
) -> dict[str, object]:
    idempotency_key = f"{action_id}:key"
    body = f"[automated] Test body.\n\n<!-- ci-shepherd:idempotency-key={idempotency_key} -->"
    proposal: dict[str, object] = {
        "actionId": action_id,
        "operation": operation,
        "issueNumber": issue_number,
        "issueUrl": f"https://github.com/{REPOSITORY}/issues/{issue_number}",
        "expectedIssueState": "open",
        "idempotencyKey": idempotency_key,
        "evidenceBasis": "issue-state",
        "evidenceIds": [f"issue:{issue_number}"],
        "body": body,
        "executionEligibility": _eligibility(eligible),
        "sourceEvidenceFingerprint": {"issueUpdatedAt": "2026-09-03T00:00:00Z"},
    }
    if operation == "edit-comment":
        proposal["commentId"] = comment_id
        # Required whenever blockingReasons does not include
        # "source-comment-unavailable" (see actor._validate_proposal).
        proposal["sourceCommentFingerprint"] = {
            "bodySha256": hashlib.sha256(b"existing-comment-body").hexdigest()
        }
    if depends_on is not None:
        proposal["dependsOn"] = depends_on
    return proposal


def _close_proposal(
    *,
    action_id: str,
    issue_number: int,
    close_reason: str = "completed",
    eligible: bool = True,
    depends_on: str | None = None,
) -> dict[str, object]:
    proposal: dict[str, object] = {
        "actionId": action_id,
        "operation": "close-issue",
        "issueNumber": issue_number,
        "issueUrl": f"https://github.com/{REPOSITORY}/issues/{issue_number}",
        "expectedIssueState": "open",
        "idempotencyKey": f"{action_id}:key",
        "evidenceBasis": "issue-state",
        "evidenceIds": [f"issue:{issue_number}"],
        "closeReason": close_reason,
        "executionEligibility": _eligibility(eligible),
        "sourceEvidenceFingerprint": {"issueUpdatedAt": "2026-09-03T00:00:00Z"},
    }
    if depends_on is not None:
        proposal["dependsOn"] = depends_on
    return proposal


def _unassign_proposal(
    *, action_id: str, issue_number: int, eligible: bool = True
) -> dict[str, object]:
    return {
        "actionId": action_id,
        "operation": "unassign-copilot",
        "issueNumber": issue_number,
        "issueUrl": f"https://github.com/{REPOSITORY}/issues/{issue_number}",
        "expectedIssueState": "open",
        "idempotencyKey": f"{action_id}:key",
        "evidenceBasis": "issue-state",
        "evidenceIds": [f"issue:{issue_number}"],
        "executionEligibility": _eligibility(eligible),
        "sourceEvidenceFingerprint": {"issueUpdatedAt": "2026-09-03T00:00:00Z"},
    }


def _document(
    proposals: list[dict[str, object]],
    *,
    snapshot_id: str = "snapshot:test-policy-selection:1",
    unchanged_issue_numbers: list[int] | None = None,
    max_proposals_per_issue: int = 2,
) -> dict[str, object]:
    violations: list[dict[str, object]] = []
    for proposal in proposals:
        eligibility = proposal["executionEligibility"]
        if eligibility["eligible"] is not True:
            violations.append(
                {
                    "actionId": proposal["actionId"],
                    "blockingReasons": list(eligibility["blockingReasons"]),
                }
            )
    if not violations:
        status = "eligible"
    elif len(violations) == len(proposals):
        status = "blocked"
    else:
        status = "partially-eligible"
    return {
        "schemaVersion": 2,
        "repository": REPOSITORY,
        "snapshotId": snapshot_id,
        "shepherdAuthor": "radical",
        "generatedAtUtc": "2026-09-03T16:00:00Z",
        "proposalTtlHours": 6,
        "maxProposalsPerIssue": max_proposals_per_issue,
        "executionEligibility": {"status": status, "violations": violations},
        "proposals": proposals,
        "unchangedIssueNumbers": unchanged_issue_numbers or [],
    }


def _policy_document(
    *,
    revision: int = 1,
    status: str = "active",
    created_at_utc: datetime = datetime(2026, 9, 3, 16, 0, tzinfo=UTC),
    expires_at_utc: datetime | None = None,
    enabled_classes: frozenset[str] = frozenset({"edit-comment"}),
    denied_action_ids: list[str] | None = None,
    denied_targets: list[str] | None = None,
    caps: dict[str, dict[str, int]] | None = None,
) -> dict[str, object]:
    expires_at_utc = expires_at_utc or (created_at_utc + timedelta(days=30))
    caps = caps or DEFAULT_CAPS
    return {
        "schemaVersion": 1,
        "repository": REPOSITORY,
        "revisionId": f"policy:{revision}",
        "revision": revision,
        "status": status,
        "createdAtUtc": _rfc3339(created_at_utc),
        "expiresAtUtc": _rfc3339(expires_at_utc),
        "actor": "github:radical",
        "replacesRevisionId": None if revision == 1 else f"policy:{revision - 1}",
        "operationClasses": {
            name: {
                "enabled": name in enabled_classes,
                "maxPerRun": caps[name]["maxPerRun"],
                "maxRolling24h": caps[name]["maxRolling24h"],
            }
            for name in OPERATION_CLASSES
        },
        "deniedActionIds": denied_action_ids or [],
        "deniedTargets": denied_targets or [],
    }


def _projection(
    *,
    state_revision: int = 1,
    policy_doc: dict[str, object] | None,
    exact_decisions: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    effective_policy = None
    if policy_doc is not None:
        effective_policy = dict(policy_doc)
        effective_policy["policyDigest"] = _digest_of(policy_doc)
    return {
        "stateRevision": state_revision,
        "effectivePolicy": effective_policy,
        "exactDecisions": exact_decisions or [],
    }


def _exact_decision(
    *,
    action_id: str,
    proposal_digest: str,
    decision: str,
    now: datetime,
    actor: str = "github:radical",
    event_revision: int = 1,
    expires_at: datetime | None = None,
) -> dict[str, object]:
    expires = expires_at or (now + timedelta(days=7))
    return {
        "actionId": action_id,
        "proposalDigest": proposal_digest,
        "decision": decision,
        "actor": actor,
        "expiresAtUtc": _rfc3339(expires),
        "eventRevision": event_revision,
    }


def _event(
    *,
    event_type: str,
    action_id: str,
    operation: str,
    target_number: int,
    idempotency_key: str,
    recorded_at: datetime,
    run_id: str | None = None,
    target_kind: str = "issue",
    outcome: str | None = None,
    repository: str = REPOSITORY,
    snapshot_id: str = "snapshot:test-policy-selection:1",
    body_digest: str | None = None,
) -> dict[str, object]:
    event: dict[str, object] = {
        "eventType": event_type,
        "repository": repository,
        "actionId": action_id,
        "operation": operation,
        "target": {"kind": target_kind, "number": target_number},
        "idempotencyKey": idempotency_key,
        "snapshotId": snapshot_id,
        "bodyDigest": body_digest,
        "runId": run_id,
        "recordedAt": _rfc3339(recorded_at),
    }
    if event_type == "terminal":
        event["outcome"] = outcome or "executed"
    return event


class FrozenFixtureTests(unittest.TestCase):
    """The frozen regression required by the task: fixture drift, and the
    original rank-before-permission selection, must both stay unchanged."""

    def test_fixture_bytes_match_expected_sha256(self) -> None:
        self.assertEqual(PROPOSALS_FIXTURE_SHA256, _sha256_hex(PROPOSALS_FIXTURE_PATH))
        self.assertEqual(
            ORIGINAL_SELECTION_FIXTURE_SHA256, _sha256_hex(ORIGINAL_SELECTION_FIXTURE_PATH)
        )

    def test_original_comment_selection_output_is_unchanged(self) -> None:
        document = _load_json(PROPOSALS_FIXTURE_PATH)
        original = _load_json(ORIGINAL_SELECTION_FIXTURE_PATH)

        selection = build_comment_selection(document, max_comments=2)

        self.assertEqual(original, selection)

    def test_frozen_regression_selects_only_enabled_edit_comment_class(self) -> None:
        document = _load_json(PROPOSALS_FIXTURE_PATH)
        proposals_digest = _digest_of(document)
        self.assertEqual("sha256:" + PROPOSALS_FIXTURE_SHA256, proposals_digest)

        now = datetime(2026, 9, 3, 16, 5, tzinfo=UTC)
        policy_doc = _policy_document(created_at_utc=datetime(2026, 9, 3, 16, 0, tzinfo=UTC))
        projection = _projection(policy_doc=policy_doc)

        selection = ps.build_policy_selection(
            document,
            run_id="run-frozen-1",
            policy_projection=projection,
            action_events=[],
            now=now,
        )

        def action_id_for(issue: int) -> str:
            return next(p["actionId"] for p in document["proposals"] if p["issueNumber"] == issue)

        expected_automatic = [action_id_for(n) for n in (19166, 19453, 19530)]
        self.assertEqual(expected_automatic, selection["automaticActionIds"])
        self.assertEqual([], selection["exactActionIds"])
        self.assertEqual(expected_automatic, selection["selectedActionIds"])
        self.assertEqual(proposals_digest, selection["proposalsDigest"])
        self.assertEqual("policy:1", selection["policyRevisionId"])
        # Only edit-comment is enabled (maxPerRun=10, maxRolling24h=30); every
        # other class is disabled and contributes zero, per _build_budgets'
        # `enabled` gate on both remainingThisRun and remainingRolling24h.
        self.assertEqual({"thisRun": 10, "rolling24h": 30}, selection["maximumWriteExposure"])

        by_id = {c["actionId"]: c for c in selection["candidates"]}
        for issue in (18203, 18299):
            self.assertEqual("ineligible", by_id[action_id_for(issue)]["status"])
        for issue in (19835, 19839, 19875, 19876):
            candidate = by_id[action_id_for(issue)]
            self.assertEqual("denied", candidate["status"])
            self.assertEqual("operation-disabled", candidate["reason"])
        for issue in (19166, 19453, 19530):
            candidate = by_id[action_id_for(issue)]
            self.assertEqual("automatic", candidate["status"])
            self.assertEqual("policy:1", candidate["licenseSource"])


class DeterministicOrderingTests(unittest.TestCase):
    def test_terminal_action_is_removed_so_sequential_loop_advances(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        first = _comment_proposal(action_id="a:first", issue_number=1)
        second = _comment_proposal(action_id="b:second", issue_number=2)
        document = _document([first, second])
        projection = _projection(
            policy_doc=_policy_document(
                enabled_classes=frozenset({"create-comment"})
            )
        )

        initial = ps.build_policy_selection(
            document,
            run_id="run-1",
            policy_projection=projection,
            action_events=[],
            now=now,
        )
        after_first = ps.build_policy_selection(
            document,
            run_id="run-1",
            policy_projection=projection,
            action_events=[
                _event(
                    event_type="terminal",
                    action_id=first["actionId"],
                    operation=first["operation"],
                    target_number=first["issueNumber"],
                    idempotency_key=first["idempotencyKey"],
                    recorded_at=now - timedelta(minutes=1),
                    run_id="run-1",
                    outcome="executed",
                    body_digest=(
                        "sha256:"
                        + hashlib.sha256(first["body"].encode("utf-8")).hexdigest()
                    ),
                )
            ],
            now=now,
        )

        self.assertEqual(["a:first", "b:second"], initial["selectedActionIds"])
        self.assertEqual(["b:second"], after_first["selectedActionIds"])
        first_candidate = next(
            candidate
            for candidate in after_first["candidates"]
            if candidate["actionId"] == "a:first"
        )
        self.assertEqual("exhausted", first_candidate["status"])
        self.assertEqual("already-terminal", first_candidate["reason"])

    def test_indeterminate_action_remains_selected_for_reconciliation(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        proposal = _comment_proposal(action_id="a:first", issue_number=1)
        document = _document([proposal])
        projection = _projection(
            policy_doc=_policy_document(
                enabled_classes=frozenset({"create-comment"})
            )
        )
        event = _event(
            event_type="terminal",
            action_id=proposal["actionId"],
            operation=proposal["operation"],
            target_number=proposal["issueNumber"],
            idempotency_key=proposal["idempotencyKey"],
            recorded_at=now - timedelta(minutes=1),
            run_id="run-1",
            outcome="indeterminate",
            body_digest=(
                "sha256:"
                + hashlib.sha256(proposal["body"].encode("utf-8")).hexdigest()
            ),
        )

        selection = ps.build_policy_selection(
            document,
            run_id="run-1",
            policy_projection=projection,
            action_events=[event],
            now=now,
        )

        self.assertEqual(["a:first"], selection["selectedActionIds"])

    def test_deterministic_order_within_permitted_set(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        create_low = _comment_proposal(
            action_id="snapshot:test:1:issue:50:watch-comment",
            issue_number=50,
            operation="create-comment",
        )
        create_high = _comment_proposal(
            action_id="snapshot:test:1:issue:150:watch-comment",
            issue_number=150,
            operation="create-comment",
        )
        edit_low = _comment_proposal(
            action_id="snapshot:test:1:issue:100:retire-status-comment",
            issue_number=100,
            operation="edit-comment",
        )
        edit_high = _comment_proposal(
            action_id="snapshot:test:1:issue:200:retire-status-comment",
            issue_number=200,
            operation="edit-comment",
        )
        # Deliberately out of expected rank order in the input document.
        document = _document([edit_high, create_high, edit_low, create_low])
        policy_doc = _policy_document(enabled_classes=frozenset({"create-comment", "edit-comment"}))
        projection = _projection(policy_doc=policy_doc)

        selection = ps.build_policy_selection(
            document, run_id="run-1", policy_projection=projection, action_events=[], now=now
        )

        self.assertEqual(
            [
                create_low["actionId"],
                create_high["actionId"],
                edit_low["actionId"],
                edit_high["actionId"],
            ],
            selection["automaticActionIds"],
        )
        by_id = {c["actionId"]: c for c in selection["candidates"]}
        self.assertEqual(1, by_id[create_low["actionId"]]["automaticRank"])
        self.assertEqual(2, by_id[create_high["actionId"]]["automaticRank"])
        self.assertEqual(3, by_id[edit_low["actionId"]]["automaticRank"])
        self.assertEqual(4, by_id[edit_high["actionId"]]["automaticRank"])
        # Candidates stay in original proposal input order; only rank fields
        # express the deterministic scan order.
        self.assertEqual(
            [
                edit_high["actionId"],
                create_high["actionId"],
                edit_low["actionId"],
                create_low["actionId"],
            ],
            [c["actionId"] for c in selection["candidates"]],
        )

    def test_edit_cap_exhausted_then_scan_admits_next_allowed_create(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        # All three proposals deliberately share the same semantic-suffix
        # tier, isolating "continue scanning past an exhausted class cap"
        # from suffix-tier ordering (covered by
        # test_deterministic_order_within_permitted_set). Note these
        # particular issue numbers do not, by themselves, pin the
        # edit-before-create operation_priority tiebreak -- removing it
        # would not change this test's outcome, since edit_b is exhausted
        # by cap regardless of scan position. See
        # test_operation_priority_breaks_tie_over_issue_number_within_same_semantic_tier
        # for a scenario that does isolate that tiebreak.
        edit_a = _comment_proposal(
            action_id="snapshot:test:1:issue:10:review-close-comment",
            issue_number=10,
            operation="edit-comment",
        )
        edit_b = _comment_proposal(
            action_id="snapshot:test:1:issue:20:review-close-comment",
            issue_number=20,
            operation="edit-comment",
        )
        create_a = _comment_proposal(
            action_id="snapshot:test:1:issue:15:review-close-comment",
            issue_number=15,
            operation="create-comment",
        )
        document = _document([edit_a, edit_b, create_a])
        caps = {name: dict(values) for name, values in DEFAULT_CAPS.items()}
        caps["edit-comment"] = {"maxPerRun": 1, "maxRolling24h": 30}
        policy_doc = _policy_document(
            enabled_classes=frozenset({"create-comment", "edit-comment"}), caps=caps
        )
        projection = _projection(policy_doc=policy_doc)

        selection = ps.build_policy_selection(
            document, run_id="run-1", policy_projection=projection, action_events=[], now=now
        )

        self.assertEqual(
            [edit_a["actionId"], create_a["actionId"]], selection["automaticActionIds"]
        )
        by_id = {c["actionId"]: c for c in selection["candidates"]}
        self.assertEqual("exhausted", by_id[edit_b["actionId"]]["status"])
        self.assertEqual("per-run-cap-exhausted", by_id[edit_b["actionId"]]["reason"])
        self.assertEqual(1, by_id[edit_a["actionId"]]["automaticRank"])
        self.assertEqual(2, by_id[create_a["actionId"]]["automaticRank"])

    def test_operation_priority_breaks_tie_over_issue_number_within_same_semantic_tier(
        self,
    ) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        # Same semantic-suffix tier ("watch-comment") on both proposals, so
        # the only remaining tiebreak is operation_priority then issueNumber.
        # The create proposal's issue number is deliberately LOWER than the
        # edit proposal's: sorting by issueNumber alone (i.e. with
        # operation_priority removed from the key) would rank create first.
        # operation_priority must still force edit first.
        edit = _comment_proposal(
            action_id="snapshot:test:1:issue:500:watch-comment",
            issue_number=500,
            operation="edit-comment",
        )
        create = _comment_proposal(
            action_id="snapshot:test:1:issue:100:watch-comment",
            issue_number=100,
            operation="create-comment",
        )
        # Input order deliberately matches the issue-number-only order, so a
        # mutation that dropped operation_priority would silently reproduce
        # this same ordering unless the assertions below catch it.
        document = _document([create, edit])
        policy_doc = _policy_document(
            enabled_classes=frozenset({"create-comment", "edit-comment"})
        )
        projection = _projection(policy_doc=policy_doc)

        selection = ps.build_policy_selection(
            document, run_id="run-1", policy_projection=projection, action_events=[], now=now
        )

        self.assertEqual(
            [edit["actionId"], create["actionId"]], selection["automaticActionIds"]
        )
        by_id = {c["actionId"]: c for c in selection["candidates"]}
        self.assertEqual(1, by_id[edit["actionId"]]["automaticRank"])
        self.assertEqual(2, by_id[create["actionId"]]["automaticRank"])


class BudgetWindowTests(unittest.TestCase):
    def test_rolling_window_is_exclusive_start_inclusive_end_terminal_only(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        proposal = _comment_proposal(
            action_id="snapshot:test:1:issue:690:retire-status-comment",
            issue_number=690,
            operation="edit-comment",
        )
        document = _document([proposal])
        policy_doc = _policy_document(enabled_classes=frozenset({"edit-comment"}))
        projection = _projection(policy_doc=policy_doc)

        events = [
            _event(  # exactly at window start: excluded (strictly-after required)
                event_type="terminal",
                action_id="hist:1",
                operation="edit-comment",
                target_number=1,
                idempotency_key="hist:1:key",
                recorded_at=now - timedelta(hours=24),
                outcome="executed",
            ),
            _event(  # just inside the window: included
                event_type="terminal",
                action_id="hist:2",
                operation="edit-comment",
                target_number=2,
                idempotency_key="hist:2:key",
                recorded_at=now - timedelta(hours=24) + timedelta(seconds=1),
                outcome="executed",
            ),
            _event(  # exactly at now: included (inclusive end)
                event_type="terminal",
                action_id="hist:3",
                operation="edit-comment",
                target_number=3,
                idempotency_key="hist:3:key",
                recorded_at=now,
                outcome="executed",
            ),
            _event(  # intent (non-terminal) inside the window: excluded
                event_type="intent",
                action_id="hist:4",
                operation="edit-comment",
                target_number=4,
                idempotency_key="hist:4:key",
                recorded_at=now - timedelta(hours=1),
            ),
        ]

        selection = ps.build_policy_selection(
            document,
            run_id="run-unused",
            policy_projection=projection,
            action_events=events,
            now=now,
        )

        self.assertEqual(2, selection["budgets"]["edit-comment"]["usedRolling24h"])

    def test_per_run_usage_matches_explicit_run_id(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        proposal = _comment_proposal(
            action_id="snapshot:test:1:issue:691:retire-status-comment",
            issue_number=691,
            operation="edit-comment",
        )
        document = _document([proposal])
        policy_doc = _policy_document(enabled_classes=frozenset({"edit-comment"}))
        projection = _projection(policy_doc=policy_doc)

        events = [
            _event(
                event_type="intent",
                action_id="run:1",
                operation="edit-comment",
                target_number=1,
                idempotency_key="run:1:key",
                recorded_at=now,
                run_id="run-a",
            ),
            _event(
                event_type="terminal",
                action_id="run:2",
                operation="edit-comment",
                target_number=2,
                idempotency_key="run:2:key",
                recorded_at=now,
                run_id="run-a",
                outcome="executed",
            ),
            _event(  # different runId: does not count against run-a's usage
                event_type="intent",
                action_id="run:3",
                operation="edit-comment",
                target_number=3,
                idempotency_key="run:3:key",
                recorded_at=now,
                run_id="run-b",
            ),
        ]

        selection = ps.build_policy_selection(
            document, run_id="run-a", policy_projection=projection, action_events=events, now=now
        )

        self.assertEqual(2, selection["budgets"]["edit-comment"]["usedThisRun"])

    def test_rolling_24h_cap_exhausted_with_per_run_headroom_remaining(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        proposal = _comment_proposal(
            action_id="snapshot:test:1:issue:730:retire-status-comment",
            issue_number=730,
            operation="edit-comment",
        )
        document = _document([proposal])
        caps = {name: dict(values) for name, values in DEFAULT_CAPS.items()}
        caps["edit-comment"] = {"maxPerRun": 10, "maxRolling24h": 1}
        policy_doc = _policy_document(enabled_classes=frozenset({"edit-comment"}), caps=caps)
        projection = _projection(policy_doc=policy_doc)

        # Recorded under a different runId: this consumes only the
        # rolling-24h allowance, not this run's per-run allowance, isolating
        # the rolling-cap branch from the per-run-cap branch that precedes
        # it in the automatic scan.
        rolling_saturating_event = _event(
            event_type="terminal",
            action_id="hist:rolling-1",
            operation="edit-comment",
            target_number=1,
            idempotency_key="hist:rolling-1:key",
            recorded_at=now - timedelta(hours=1),
            outcome="executed",
            run_id="other-run",
        )

        selection = ps.build_policy_selection(
            document,
            run_id="run-1",
            policy_projection=projection,
            action_events=[rolling_saturating_event],
            now=now,
        )

        self.assertEqual(10, selection["budgets"]["edit-comment"]["remainingThisRun"])
        self.assertEqual(0, selection["budgets"]["edit-comment"]["remainingRolling24h"])
        candidate = selection["candidates"][0]
        self.assertEqual("exhausted", candidate["status"])
        self.assertEqual("rolling-24h-cap-exhausted", candidate["reason"])

    def test_repository_hard_ceiling_per_run_exhausted_with_unclassified_operations(
        self,
    ) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        proposal = _comment_proposal(
            action_id="snapshot:test:1:issue:740:retire-status-comment",
            issue_number=740,
            operation="edit-comment",
        )
        document = _document([proposal])
        policy_doc = _policy_document(enabled_classes=frozenset({"edit-comment"}))
        projection = _projection(policy_doc=policy_doc)

        # An unclassified legacy operation never appears in any per-class
        # remaining dict, so it consumes only the repository-wide 100/run
        # hard ceiling, isolating that branch from the per-class caps that
        # precede it.
        saturating_events = [
            _event(
                event_type="intent",
                action_id=f"legacy:{i}",
                operation="legacy-pilot-op",
                target_number=i + 1,
                idempotency_key=f"legacy:{i}:key",
                recorded_at=now,
                run_id="run-1",
            )
            for i in range(100)
        ]

        selection = ps.build_policy_selection(
            document,
            run_id="run-1",
            policy_projection=projection,
            action_events=saturating_events,
            now=now,
        )

        self.assertEqual(10, selection["budgets"]["edit-comment"]["remainingThisRun"])
        candidate = selection["candidates"][0]
        self.assertEqual("exhausted", candidate["status"])
        self.assertEqual("repository-hard-ceiling-run-exhausted", candidate["reason"])

    def test_repository_hard_ceiling_rolling_exhausted_with_unclassified_operations(
        self,
    ) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        proposal = _comment_proposal(
            action_id="snapshot:test:1:issue:745:retire-status-comment",
            issue_number=745,
            operation="edit-comment",
        )
        document = _document([proposal])
        policy_doc = _policy_document(enabled_classes=frozenset({"edit-comment"}))
        projection = _projection(policy_doc=policy_doc)

        # Terminal legacy events recorded under a different runId consume
        # only the repository-wide 300/rolling-24h hard ceiling: not this
        # run's per-run ceiling (different runId), and not any per-class
        # budget (unclassified operation). This isolates the rolling
        # hard-ceiling branch.
        saturating_events = [
            _event(
                event_type="terminal",
                action_id=f"legacy:{i}",
                operation="legacy-pilot-op",
                target_number=i + 1,
                idempotency_key=f"legacy:{i}:key",
                recorded_at=now,
                outcome="executed",
                run_id="other-run",
            )
            for i in range(300)
        ]

        selection = ps.build_policy_selection(
            document,
            run_id="run-1",
            policy_projection=projection,
            action_events=saturating_events,
            now=now,
        )

        self.assertEqual(10, selection["budgets"]["edit-comment"]["remainingThisRun"])
        candidate = selection["candidates"][0]
        self.assertEqual("exhausted", candidate["status"])
        self.assertEqual("repository-hard-ceiling-rolling-exhausted", candidate["reason"])


class ExactDecisionTests(unittest.TestCase):
    def test_reject_once_blocks_broad_policy_permission(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        proposal = _comment_proposal(
            action_id="snapshot:test:1:issue:680:retire-status-comment",
            issue_number=680,
            operation="edit-comment",
        )
        document = _document([proposal])
        policy_doc = _policy_document(enabled_classes=frozenset({"edit-comment"}))
        proposals_digest = _digest_of(document)
        reject = _exact_decision(
            action_id=proposal["actionId"],
            proposal_digest=proposals_digest,
            decision="reject-once",
            now=now,
        )
        projection = _projection(policy_doc=policy_doc, exact_decisions=[reject])

        selection = ps.build_policy_selection(
            document, run_id="run-1", policy_projection=projection, action_events=[], now=now
        )

        candidate = selection["candidates"][0]
        self.assertEqual("denied", candidate["status"])
        self.assertEqual("exact-rejected", candidate["reason"])
        self.assertEqual(
            {
                "eventRevision": 1,
                "proposalDigest": proposals_digest,
                "decision": "reject-once",
                "actor": "github:radical",
            },
            candidate["exactDecision"],
        )
        self.assertEqual([], selection["automaticActionIds"])

    def test_approve_once_admits_disabled_class(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        proposal = _comment_proposal(
            action_id="snapshot:test:1:issue:610:retire-status-comment",
            issue_number=610,
            operation="edit-comment",
        )
        document = _document([proposal])
        policy_doc = _policy_document(enabled_classes=frozenset())  # edit-comment disabled
        proposals_digest = _digest_of(document)
        approve = _exact_decision(
            action_id=proposal["actionId"],
            proposal_digest=proposals_digest,
            decision="approve-once",
            now=now,
        )
        projection = _projection(policy_doc=policy_doc, exact_decisions=[approve])

        selection = ps.build_policy_selection(
            document, run_id="run-1", policy_projection=projection, action_events=[], now=now
        )

        candidate = selection["candidates"][0]
        self.assertEqual("exact", candidate["status"])
        self.assertEqual("exact-approval", candidate["reason"])
        self.assertEqual("decision:1", candidate["licenseSource"])
        self.assertEqual([proposal["actionId"]], selection["exactActionIds"])
        self.assertEqual([], selection["automaticActionIds"])

    def test_approve_once_admits_exhausted_class(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        proposal = _comment_proposal(
            action_id="snapshot:test:1:issue:620:retire-status-comment",
            issue_number=620,
            operation="edit-comment",
        )
        document = _document([proposal])
        caps = {name: dict(values) for name, values in DEFAULT_CAPS.items()}
        caps["edit-comment"] = {"maxPerRun": 0, "maxRolling24h": 0}
        policy_doc = _policy_document(enabled_classes=frozenset({"edit-comment"}), caps=caps)
        proposals_digest = _digest_of(document)
        approve = _exact_decision(
            action_id=proposal["actionId"],
            proposal_digest=proposals_digest,
            decision="approve-once",
            now=now,
        )
        projection = _projection(policy_doc=policy_doc, exact_decisions=[approve])

        selection = ps.build_policy_selection(
            document, run_id="run-1", policy_projection=projection, action_events=[], now=now
        )

        candidate = selection["candidates"][0]
        self.assertEqual("exact", candidate["status"])
        self.assertEqual([proposal["actionId"]], selection["exactActionIds"])

    def test_stale_exact_decision_digest_mismatch_does_not_license(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        proposal = _comment_proposal(
            action_id="snapshot:test:1:issue:630:retire-status-comment",
            issue_number=630,
            operation="edit-comment",
        )
        document = _document([proposal])
        policy_doc = _policy_document(enabled_classes=frozenset())
        stale_digest = "sha256:" + "1" * 64
        approve = _exact_decision(
            action_id=proposal["actionId"],
            proposal_digest=stale_digest,
            decision="approve-once",
            now=now,
        )
        projection = _projection(policy_doc=policy_doc, exact_decisions=[approve])

        selection = ps.build_policy_selection(
            document, run_id="run-1", policy_projection=projection, action_events=[], now=now
        )

        candidate = selection["candidates"][0]
        self.assertEqual("denied", candidate["status"])
        self.assertEqual("operation-disabled", candidate["reason"])
        self.assertEqual([], selection["exactActionIds"])

    def test_stale_exact_decision_action_id_mismatch_does_not_license(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        proposal = _comment_proposal(
            action_id="snapshot:test:1:issue:640:retire-status-comment",
            issue_number=640,
            operation="edit-comment",
        )
        document = _document([proposal])
        policy_doc = _policy_document(enabled_classes=frozenset())
        proposals_digest = _digest_of(document)
        approve = _exact_decision(
            action_id="snapshot:test:1:issue:999:retire-status-comment",
            proposal_digest=proposals_digest,
            decision="approve-once",
            now=now,
        )
        projection = _projection(policy_doc=policy_doc, exact_decisions=[approve])

        selection = ps.build_policy_selection(
            document, run_id="run-1", policy_projection=projection, action_events=[], now=now
        )

        candidate = selection["candidates"][0]
        self.assertEqual("denied", candidate["status"])
        self.assertEqual([], selection["exactActionIds"])

    def test_exact_approval_does_not_exceed_hard_ceiling(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        proposal = _comment_proposal(
            action_id="snapshot:test:1:issue:600:retire-status-comment",
            issue_number=600,
            operation="edit-comment",
        )
        document = _document([proposal])
        policy_doc = _policy_document(enabled_classes=frozenset())  # everything disabled
        proposals_digest = _digest_of(document)
        approve = _exact_decision(
            action_id=proposal["actionId"],
            proposal_digest=proposals_digest,
            decision="approve-once",
            now=now,
        )
        projection = _projection(policy_doc=policy_doc, exact_decisions=[approve])

        # Saturate the 100/run repository hard ceiling with unrelated legacy
        # events so no automatic or exact grant can consume more headroom.
        saturating_events = [
            _event(
                event_type="intent",
                action_id=f"legacy:{i}",
                operation="legacy-pilot-op",
                target_number=i + 1,
                idempotency_key=f"legacy:{i}:key",
                recorded_at=now,
                run_id="run-1",
            )
            for i in range(100)
        ]

        selection = ps.build_policy_selection(
            document,
            run_id="run-1",
            policy_projection=projection,
            action_events=saturating_events,
            now=now,
        )

        candidate = selection["candidates"][0]
        self.assertEqual("denied", candidate["status"])
        self.assertEqual("operation-disabled", candidate["reason"])
        self.assertEqual([], selection["exactActionIds"])

    def test_exact_approval_does_not_exceed_rolling_hard_ceiling(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        proposal = _comment_proposal(
            action_id="snapshot:test:1:issue:605:retire-status-comment",
            issue_number=605,
            operation="edit-comment",
        )
        document = _document([proposal])
        policy_doc = _policy_document(enabled_classes=frozenset())  # everything disabled
        proposals_digest = _digest_of(document)
        approve = _exact_decision(
            action_id=proposal["actionId"],
            proposal_digest=proposals_digest,
            decision="approve-once",
            now=now,
        )
        projection = _projection(policy_doc=policy_doc, exact_decisions=[approve])

        # Saturate only the 300/rolling-24h repository hard ceiling
        # (terminal events under a different runId), leaving the 100/run
        # ceiling untouched, so the rolling branch of the exact-approval
        # exposure check -- not the per-run branch -- is what blocks this
        # approve-once.
        saturating_events = [
            _event(
                event_type="terminal",
                action_id=f"legacy:{i}",
                operation="legacy-pilot-op",
                target_number=i + 1,
                idempotency_key=f"legacy:{i}:key",
                recorded_at=now,
                outcome="executed",
                run_id="other-run",
            )
            for i in range(300)
        ]

        selection = ps.build_policy_selection(
            document,
            run_id="run-1",
            policy_projection=projection,
            action_events=saturating_events,
            now=now,
        )

        candidate = selection["candidates"][0]
        self.assertEqual("denied", candidate["status"])
        self.assertEqual("operation-disabled", candidate["reason"])
        self.assertEqual([], selection["exactActionIds"])

    def test_denied_action_ids_and_targets_absolute_and_attributed_to_policy_revision(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        denied_by_id = _comment_proposal(
            action_id="snapshot:test:1:issue:650:retire-status-comment",
            issue_number=650,
            operation="edit-comment",
        )
        denied_by_target = _comment_proposal(
            action_id="snapshot:test:1:issue:660:retire-status-comment",
            issue_number=660,
            operation="edit-comment",
        )
        document = _document([denied_by_id, denied_by_target])
        policy_doc = _policy_document(
            enabled_classes=frozenset({"edit-comment"}),
            denied_action_ids=[denied_by_id["actionId"]],
            denied_targets=["issue:660"],
        )
        projection = _projection(policy_doc=policy_doc)

        selection = ps.build_policy_selection(
            document, run_id="run-1", policy_projection=projection, action_events=[], now=now
        )

        by_id = {c["actionId"]: c for c in selection["candidates"]}
        self.assertEqual("denied", by_id[denied_by_id["actionId"]]["status"])
        self.assertEqual("policy-denied-action-id", by_id[denied_by_id["actionId"]]["reason"])
        self.assertEqual("denied", by_id[denied_by_target["actionId"]]["status"])
        self.assertEqual("policy-denied-target", by_id[denied_by_target["actionId"]]["reason"])
        self.assertEqual("policy:1", by_id[denied_by_id["actionId"]]["policyRevisionId"])
        self.assertEqual("policy:1", by_id[denied_by_target["actionId"]]["policyRevisionId"])
        self.assertEqual([], selection["automaticActionIds"])

    def test_deny_remains_absolute_against_approve_once(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        proposal = _comment_proposal(
            action_id="snapshot:test:1:issue:670:retire-status-comment",
            issue_number=670,
            operation="edit-comment",
        )
        document = _document([proposal])
        policy_doc = _policy_document(
            enabled_classes=frozenset({"edit-comment"}),
            denied_action_ids=[proposal["actionId"]],
        )
        proposals_digest = _digest_of(document)
        approve = _exact_decision(
            action_id=proposal["actionId"],
            proposal_digest=proposals_digest,
            decision="approve-once",
            now=now,
        )
        projection = _projection(policy_doc=policy_doc, exact_decisions=[approve])

        selection = ps.build_policy_selection(
            document, run_id="run-1", policy_projection=projection, action_events=[], now=now
        )

        candidate = selection["candidates"][0]
        self.assertEqual("denied", candidate["status"])
        self.assertEqual("policy-denied-action-id", candidate["reason"])
        self.assertEqual([], selection["exactActionIds"])

    def _denied_pair_document(
        self, *, id_issue: int, target_issue: int
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        denied_by_id = _comment_proposal(
            action_id=f"snapshot:test:1:issue:{id_issue}:retire-status-comment",
            issue_number=id_issue,
            operation="edit-comment",
        )
        denied_by_target = _comment_proposal(
            action_id=f"snapshot:test:1:issue:{target_issue}:retire-status-comment",
            issue_number=target_issue,
            operation="edit-comment",
        )
        document = _document([denied_by_id, denied_by_target])
        return document, denied_by_id, denied_by_target

    def test_denies_do_not_apply_when_policy_paused_without_exact_approval(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        document, denied_by_id, denied_by_target = self._denied_pair_document(
            id_issue=750, target_issue=751
        )
        policy_doc = _policy_document(
            status="paused",
            enabled_classes=frozenset({"edit-comment"}),
            denied_action_ids=[denied_by_id["actionId"]],
            denied_targets=["issue:751"],
        )
        projection = _projection(policy_doc=policy_doc)

        selection = ps.build_policy_selection(
            document, run_id="run-1", policy_projection=projection, action_events=[], now=now
        )

        by_id = {c["actionId"]: c for c in selection["candidates"]}
        for proposal in (denied_by_id, denied_by_target):
            candidate = by_id[proposal["actionId"]]
            self.assertEqual("denied", candidate["status"])
            self.assertEqual("no-active-policy", candidate["reason"])
            # Attribution to the revision is retained even though the
            # revision's deny lists no longer apply while it is paused.
            self.assertEqual("policy:1", candidate["policyRevisionId"])
        self.assertEqual([], selection["automaticActionIds"])

    def test_denies_do_not_apply_when_policy_revoked_without_exact_approval(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        document, denied_by_id, denied_by_target = self._denied_pair_document(
            id_issue=752, target_issue=753
        )
        policy_doc = _policy_document(
            status="revoked",
            enabled_classes=frozenset({"edit-comment"}),
            denied_action_ids=[denied_by_id["actionId"]],
            denied_targets=["issue:753"],
        )
        projection = _projection(policy_doc=policy_doc)

        selection = ps.build_policy_selection(
            document, run_id="run-1", policy_projection=projection, action_events=[], now=now
        )

        by_id = {c["actionId"]: c for c in selection["candidates"]}
        for proposal in (denied_by_id, denied_by_target):
            candidate = by_id[proposal["actionId"]]
            self.assertEqual("denied", candidate["status"])
            self.assertEqual("no-active-policy", candidate["reason"])
        self.assertEqual([], selection["automaticActionIds"])

    def test_denies_do_not_apply_when_policy_naturally_expired_without_exact_approval(
        self,
    ) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        document, denied_by_id, denied_by_target = self._denied_pair_document(
            id_issue=754, target_issue=755
        )
        created_at = now - timedelta(days=60)
        policy_doc = _policy_document(
            status="active",
            created_at_utc=created_at,
            expires_at_utc=created_at + timedelta(days=30),  # expired 30 days before `now`
            enabled_classes=frozenset({"edit-comment"}),
            denied_action_ids=[denied_by_id["actionId"]],
            denied_targets=["issue:755"],
        )
        projection = _projection(policy_doc=policy_doc)

        selection = ps.build_policy_selection(
            document, run_id="run-1", policy_projection=projection, action_events=[], now=now
        )

        by_id = {c["actionId"]: c for c in selection["candidates"]}
        for proposal in (denied_by_id, denied_by_target):
            candidate = by_id[proposal["actionId"]]
            self.assertEqual("denied", candidate["status"])
            self.assertEqual("no-active-policy", candidate["reason"])
        self.assertEqual([], selection["automaticActionIds"])

    def test_denied_action_id_paused_policy_with_approve_once_admits_exact(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        document, denied_by_id, denied_by_target = self._denied_pair_document(
            id_issue=756, target_issue=757
        )
        policy_doc = _policy_document(
            status="paused",
            enabled_classes=frozenset({"edit-comment"}),
            denied_action_ids=[denied_by_id["actionId"]],
            denied_targets=["issue:757"],
        )
        proposals_digest = _digest_of(document)
        approvals = [
            _exact_decision(
                action_id=proposal["actionId"],
                proposal_digest=proposals_digest,
                decision="approve-once",
                now=now,
                event_revision=revision,
            )
            for revision, proposal in enumerate((denied_by_id, denied_by_target), start=1)
        ]
        projection = _projection(policy_doc=policy_doc, exact_decisions=approvals)

        selection = ps.build_policy_selection(
            document, run_id="run-1", policy_projection=projection, action_events=[], now=now
        )

        by_id = {c["actionId"]: c for c in selection["candidates"]}
        for revision, proposal in enumerate((denied_by_id, denied_by_target), start=1):
            candidate = by_id[proposal["actionId"]]
            self.assertEqual("exact", candidate["status"])
            self.assertEqual("exact-approval", candidate["reason"])
            self.assertEqual(f"decision:{revision}", candidate["licenseSource"])
        self.assertEqual(
            {denied_by_id["actionId"], denied_by_target["actionId"]},
            set(selection["exactActionIds"]),
        )
        self.assertEqual([], selection["automaticActionIds"])

    def test_denied_action_id_revoked_policy_with_approve_once_admits_exact(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        document, denied_by_id, denied_by_target = self._denied_pair_document(
            id_issue=758, target_issue=759
        )
        policy_doc = _policy_document(
            status="revoked",
            enabled_classes=frozenset({"edit-comment"}),
            denied_action_ids=[denied_by_id["actionId"]],
            denied_targets=["issue:759"],
        )
        proposals_digest = _digest_of(document)
        approvals = [
            _exact_decision(
                action_id=proposal["actionId"],
                proposal_digest=proposals_digest,
                decision="approve-once",
                now=now,
                event_revision=revision,
            )
            for revision, proposal in enumerate((denied_by_id, denied_by_target), start=1)
        ]
        projection = _projection(policy_doc=policy_doc, exact_decisions=approvals)

        selection = ps.build_policy_selection(
            document, run_id="run-1", policy_projection=projection, action_events=[], now=now
        )

        self.assertEqual(
            {denied_by_id["actionId"], denied_by_target["actionId"]},
            set(selection["exactActionIds"]),
        )
        self.assertEqual([], selection["automaticActionIds"])

    def test_denied_action_id_expired_policy_with_approve_once_admits_exact(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        document, denied_by_id, denied_by_target = self._denied_pair_document(
            id_issue=760, target_issue=761
        )
        created_at = now - timedelta(days=60)
        policy_doc = _policy_document(
            status="active",
            created_at_utc=created_at,
            expires_at_utc=created_at + timedelta(days=30),  # expired 30 days before `now`
            enabled_classes=frozenset({"edit-comment"}),
            denied_action_ids=[denied_by_id["actionId"]],
            denied_targets=["issue:761"],
        )
        proposals_digest = _digest_of(document)
        approvals = [
            _exact_decision(
                action_id=proposal["actionId"],
                proposal_digest=proposals_digest,
                decision="approve-once",
                now=now,
                event_revision=revision,
            )
            for revision, proposal in enumerate((denied_by_id, denied_by_target), start=1)
        ]
        projection = _projection(policy_doc=policy_doc, exact_decisions=approvals)

        selection = ps.build_policy_selection(
            document, run_id="run-1", policy_projection=projection, action_events=[], now=now
        )

        self.assertEqual(
            {denied_by_id["actionId"], denied_by_target["actionId"]},
            set(selection["exactActionIds"]),
        )
        self.assertEqual([], selection["automaticActionIds"])


class DependentClosePrerequisiteTests(unittest.TestCase):
    def test_dependent_close_blocked_until_exact_terminal_then_admitted_with_digest(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        dep = _comment_proposal(
            action_id="snapshot:test:1:issue:700:watch-comment",
            issue_number=700,
            operation="create-comment",
        )
        close = _close_proposal(
            action_id="snapshot:test:1:issue:700:review-close",
            issue_number=700,
            depends_on=dep["actionId"],
        )
        document = _document([dep, close])
        # Only close-issue is licensed: this both isolates the prerequisite
        # logic and proves the dependency itself is never silently granted.
        policy_doc = _policy_document(enabled_classes=frozenset({"close-issue"}))
        projection = _projection(policy_doc=policy_doc)

        blocked = ps.build_policy_selection(
            document, run_id="run-1", policy_projection=projection, action_events=[], now=now
        )
        by_id = {c["actionId"]: c for c in blocked["candidates"]}
        self.assertEqual("exhausted", by_id[close["actionId"]]["status"])
        self.assertEqual("prerequisite-not-terminal", by_id[close["actionId"]]["reason"])
        self.assertIsNone(by_id[close["actionId"]]["satisfiedPrerequisites"])
        self.assertEqual("denied", by_id[dep["actionId"]]["status"])
        self.assertEqual("operation-disabled", by_id[dep["actionId"]]["reason"])

        dep_body_digest = "sha256:" + hashlib.sha256(dep["body"].encode("utf-8")).hexdigest()
        stale_event = _event(
            event_type="terminal",
            action_id=dep["actionId"],
            operation="create-comment",
            target_number=700,
            idempotency_key=dep["idempotencyKey"],
            recorded_at=now - timedelta(hours=2),
            outcome="executed",
            snapshot_id=document["snapshotId"],
            body_digest=dep_body_digest,
        )
        fresh_event = _event(
            event_type="terminal",
            action_id=dep["actionId"],
            operation="create-comment",
            target_number=700,
            idempotency_key=dep["idempotencyKey"],
            recorded_at=now - timedelta(minutes=5),
            outcome="executed",
            snapshot_id=document["snapshotId"],
            body_digest=dep_body_digest,
        )
        non_matching_event = _event(  # different bodyDigest: never matches
            event_type="terminal",
            action_id=dep["actionId"],
            operation="create-comment",
            target_number=700,
            idempotency_key=dep["idempotencyKey"],
            recorded_at=now - timedelta(minutes=1),
            outcome="executed",
            snapshot_id=document["snapshotId"],
            body_digest="sha256:" + "0" * 64,
        )

        admitted = ps.build_policy_selection(
            document,
            run_id="run-1",
            policy_projection=projection,
            action_events=[stale_event, fresh_event, non_matching_event],
            now=now,
        )
        close_candidate = next(
            c for c in admitted["candidates"] if c["actionId"] == close["actionId"]
        )
        self.assertEqual("automatic", close_candidate["status"])
        expected_digest = "sha256:" + hashlib.sha256(
            stable_json(fresh_event).encode("utf-8")
        ).hexdigest()
        self.assertEqual(
            [{"actionId": dep["actionId"], "eventDigest": expected_digest}],
            close_candidate["satisfiedPrerequisites"],
        )
        self.assertIn(close["actionId"], admitted["automaticActionIds"])
        # The dependency itself is terminal and therefore is not selected
        # again; only the dependent close advances.
        self.assertEqual(
            "exhausted",
            next(
                c
                for c in admitted["candidates"]
                if c["actionId"] == dep["actionId"]
            )["status"],
        )

    def test_dependent_close_prerequisite_requires_every_identity_field_to_match(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        dep = _comment_proposal(
            action_id="snapshot:test:1:issue:750:watch-comment",
            issue_number=750,
            operation="create-comment",
        )
        close = _close_proposal(
            action_id="snapshot:test:1:issue:750:review-close",
            issue_number=750,
            depends_on=dep["actionId"],
        )
        document = _document([dep, close])
        policy_doc = _policy_document(enabled_classes=frozenset({"close-issue"}))
        projection = _projection(policy_doc=policy_doc)
        dep_body_digest = "sha256:" + hashlib.sha256(dep["body"].encode("utf-8")).hexdigest()

        # Every kwarg here is an exact match to the dependency's identity.
        # The positive control below proves this template, unperturbed,
        # does satisfy the prerequisite -- so each negative scenario is a
        # genuine single-field isolation, not a broken harness.
        matching_kwargs: dict[str, object] = dict(
            event_type="terminal",
            action_id=dep["actionId"],
            operation=dep["operation"],
            target_number=750,
            idempotency_key=dep["idempotencyKey"],
            recorded_at=now - timedelta(minutes=5),
            outcome="executed",
            repository=REPOSITORY,
            snapshot_id=document["snapshotId"],
            body_digest=dep_body_digest,
        )

        def selection_for(events: list[dict[str, object]]) -> dict[str, object]:
            return ps.build_policy_selection(
                document,
                run_id="run-1",
                policy_projection=projection,
                action_events=events,
                now=now,
            )

        control = selection_for([_event(**matching_kwargs)])
        control_close = next(
            c for c in control["candidates"] if c["actionId"] == close["actionId"]
        )
        self.assertEqual("automatic", control_close["status"])
        self.assertIsNotNone(control_close["satisfiedPrerequisites"])

        # field name (as it appears in the event) -> (kwarg name, mismatched value)
        mismatches: dict[str, tuple[str, object]] = {
            "outcome": ("outcome", "failed"),
            "actionId": ("action_id", "snapshot:test:1:issue:750:some-other-action"),
            "snapshotId": ("snapshot_id", "snapshot:test-policy-selection:2"),
            "operation": ("operation", "edit-comment"),
            "targetNumber": ("target_number", 751),
            "idempotencyKey": ("idempotency_key", "different:key"),
            "repository": ("repository", "microsoft/some-other-repo"),
        }
        for field_name, (kwarg_name, bad_value) in mismatches.items():
            with self.subTest(field=field_name):
                kwargs = dict(matching_kwargs)
                kwargs[kwarg_name] = bad_value
                selection = selection_for([_event(**kwargs)])
                close_candidate = next(
                    c for c in selection["candidates"] if c["actionId"] == close["actionId"]
                )
                self.assertEqual("exhausted", close_candidate["status"])
                self.assertEqual("prerequisite-not-terminal", close_candidate["reason"])
                self.assertIsNone(close_candidate["satisfiedPrerequisites"])


class SuppressionAndSurfaceTests(unittest.TestCase):
    def test_same_issue_suppression_scoped_to_comment_operations(self) -> None:
        # The real actor.py schema hard-caps proposals to two per issue
        # (MAX_EXECUTABLE_PROPOSALS_PER_ISSUE), so this uses two issues:
        # one to prove same-issue comment suppression, and a second where a
        # close-issue proposal shares an issue with a lower-priority comment
        # -- proving suppression never sweeps up a non-comment operation.
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        high = _comment_proposal(
            action_id="snapshot:test:1:issue:900:review-close-comment",
            issue_number=900,
            operation="create-comment",
        )
        low = _comment_proposal(
            action_id="snapshot:test:1:issue:900:watch-comment",
            issue_number=900,
            operation="create-comment",
        )
        low_sibling = _comment_proposal(
            action_id="snapshot:test:1:issue:901:review-close-comment",
            issue_number=901,
            operation="create-comment",
        )
        close = _close_proposal(action_id="snapshot:test:1:issue:901:review-close", issue_number=901)
        document = _document([high, low, low_sibling, close])
        policy_doc = _policy_document(enabled_classes=frozenset({"create-comment", "close-issue"}))
        projection = _projection(policy_doc=policy_doc)

        selection = ps.build_policy_selection(
            document, run_id="run-1", policy_projection=projection, action_events=[], now=now
        )

        by_id = {c["actionId"]: c for c in selection["candidates"]}
        self.assertEqual("automatic", by_id[high["actionId"]]["status"])
        self.assertEqual("superseded", by_id[low["actionId"]]["status"])
        self.assertEqual(
            "lower-priority-comment-for-same-issue", by_id[low["actionId"]]["reason"]
        )
        # The close shares issue 901 with a lower-priority comment, but
        # close-issue is not in COMMENT_OPERATIONS, so it must not be
        # swept up by comment-only same-issue suppression.
        self.assertEqual("automatic", by_id[close["actionId"]]["status"])
        self.assertEqual("automatic", by_id[low_sibling["actionId"]]["status"])

    def test_unsupported_operation_is_outside_policy_surface(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        proposal = _unassign_proposal(
            action_id="snapshot:test:1:issue:950:unassign", issue_number=950
        )
        document = _document([proposal])
        policy_doc = _policy_document(enabled_classes=frozenset(OPERATION_CLASSES))
        projection = _projection(policy_doc=policy_doc)

        selection = ps.build_policy_selection(
            document, run_id="run-1", policy_projection=projection, action_events=[], now=now
        )

        candidate = selection["candidates"][0]
        self.assertEqual("outside-policy-surface", candidate["status"])
        self.assertEqual("unsupported-operation", candidate["reason"])
        self.assertIsNone(candidate["operationClass"])
        # rerun-or-retry is a recognized policy class with no proposal
        # mapping/current candidates.
        self.assertIn("rerun-or-retry", selection["budgets"])
        self.assertEqual(0, selection["budgets"]["rerun-or-retry"]["usedThisRun"])


class InvariantTests(unittest.TestCase):
    def test_each_candidate_has_exactly_one_typed_status(self) -> None:
        now = datetime(2026, 9, 3, 16, 5, tzinfo=UTC)
        document = _load_json(PROPOSALS_FIXTURE_PATH)
        policy_doc = _policy_document(created_at_utc=datetime(2026, 9, 3, 16, 0, tzinfo=UTC))
        projection = _projection(policy_doc=policy_doc)

        selection = ps.build_policy_selection(
            document, run_id="run-1", policy_projection=projection, action_events=[], now=now
        )

        self.assertTrue(selection["candidates"])
        for candidate in selection["candidates"]:
            self.assertIn(candidate["status"], _TYPED_STATUSES)
            self.assertFalse(
                candidate["automaticRank"] is not None and candidate["exactRank"] is not None,
                f"{candidate['actionId']} has both an automatic and an exact rank.",
            )

    def test_maximum_exposure_bounded_by_class_and_hard_ceiling(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        proposal = _comment_proposal(
            action_id="snapshot:test:1:issue:960:retire-status-comment",
            issue_number=960,
            operation="edit-comment",
        )
        document = _document([proposal])
        policy_doc = _policy_document(enabled_classes=frozenset(OPERATION_CLASSES))
        projection = _projection(policy_doc=policy_doc)

        # Sum of per-class maxPerRun allowances is well under the 100/run
        # hard ceiling; consume most of the ceiling with unrelated legacy
        # events so the hard ceiling -- not the per-class sum -- binds.
        legacy_events = [
            _event(
                event_type="intent",
                action_id=f"legacy:{i}",
                operation="legacy-pilot-op",
                target_number=i + 1,
                idempotency_key=f"legacy:{i}:key",
                recorded_at=now,
                run_id="run-1",
            )
            for i in range(90)
        ]

        selection = ps.build_policy_selection(
            document,
            run_id="run-1",
            policy_projection=projection,
            action_events=legacy_events,
            now=now,
        )

        sum_per_class_remaining = sum(
            budget["remainingThisRun"] for budget in selection["budgets"].values()
        )
        self.assertGreater(sum_per_class_remaining, 10)
        self.assertEqual(10, selection["maximumWriteExposure"]["thisRun"])

    def test_disabled_class_budget_has_zero_remaining_despite_nonzero_caps(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        proposal = _comment_proposal(
            action_id="snapshot:test:1:issue:965:retire-status-comment",
            issue_number=965,
            operation="edit-comment",
        )
        document = _document([proposal])
        # Only edit-comment is enabled; close-issue has nonzero maxPerRun/
        # maxRolling24h in DEFAULT_CAPS (5/10) but zero usage, so its
        # remaining budgets must be forced to zero purely because the class
        # itself is disabled -- proving the `if enabled else 0` gate, not
        # usage accounting, is what zeroes it out.
        policy_doc = _policy_document(enabled_classes=frozenset({"edit-comment"}))
        projection = _projection(policy_doc=policy_doc)

        selection = ps.build_policy_selection(
            document, run_id="run-1", policy_projection=projection, action_events=[], now=now
        )

        close_budget = selection["budgets"]["close-issue"]
        self.assertFalse(close_budget["enabled"])
        self.assertGreater(close_budget["maxPerRun"], 0)
        self.assertGreater(close_budget["maxRolling24h"], 0)
        self.assertEqual(0, close_budget["usedThisRun"])
        self.assertEqual(0, close_budget["usedRolling24h"])
        self.assertEqual(0, close_budget["remainingThisRun"])
        self.assertEqual(0, close_budget["remainingRolling24h"])

    def test_maximum_exposure_is_zero_when_policy_paused(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        proposal = _comment_proposal(
            action_id="snapshot:test:1:issue:966:retire-status-comment",
            issue_number=966,
            operation="edit-comment",
        )
        document = _document([proposal])
        # Every class is nominally "enabled" in the document with ample
        # per-class headroom, but the policy itself is paused: exposure
        # must still collapse to zero.
        policy_doc = _policy_document(status="paused", enabled_classes=frozenset(OPERATION_CLASSES))
        projection = _projection(policy_doc=policy_doc)

        selection = ps.build_policy_selection(
            document, run_id="run-1", policy_projection=projection, action_events=[], now=now
        )

        self.assertEqual({"thisRun": 0, "rolling24h": 0}, selection["maximumWriteExposure"])
        for op_class in OPERATION_CLASSES:
            self.assertEqual(0, selection["budgets"][op_class]["remainingThisRun"])
            self.assertEqual(0, selection["budgets"][op_class]["remainingRolling24h"])

    def test_maximum_exposure_is_zero_when_policy_naturally_expired(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        proposal = _comment_proposal(
            action_id="snapshot:test:1:issue:967:retire-status-comment",
            issue_number=967,
            operation="edit-comment",
        )
        document = _document([proposal])
        created_at = now - timedelta(days=60)
        policy_doc = _policy_document(
            status="active",
            created_at_utc=created_at,
            expires_at_utc=created_at + timedelta(days=30),
            enabled_classes=frozenset(OPERATION_CLASSES),
        )
        projection = _projection(policy_doc=policy_doc)

        selection = ps.build_policy_selection(
            document, run_id="run-1", policy_projection=projection, action_events=[], now=now
        )

        self.assertEqual({"thisRun": 0, "rolling24h": 0}, selection["maximumWriteExposure"])


class FailClosedTests(unittest.TestCase):
    def _valid_document_and_policy(self) -> tuple[dict[str, object], dict[str, object]]:
        proposal = _comment_proposal(
            action_id="snapshot:test:1:issue:970:retire-status-comment",
            issue_number=970,
            operation="edit-comment",
        )
        document = _document([proposal])
        policy_doc = _policy_document(enabled_classes=frozenset({"edit-comment"}))
        return document, policy_doc

    def test_malformed_policy_projection_missing_field_fails_closed(self) -> None:
        document, _ = self._valid_document_and_policy()
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        malformed_projection = {"stateRevision": 1, "effectivePolicy": None}  # missing exactDecisions

        with self.assertRaises(ps.PolicySelectionError):
            ps.build_policy_selection(
                document,
                run_id="run-1",
                policy_projection=malformed_projection,
                action_events=[],
                now=now,
            )

    def test_malformed_action_event_type_fails_closed(self) -> None:
        document, policy_doc = self._valid_document_and_policy()
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        projection = _projection(policy_doc=policy_doc)
        bad_event = {"eventType": "not-a-real-kind", "repository": REPOSITORY}

        with self.assertRaises(ps.PolicySelectionError):
            ps.build_policy_selection(
                document,
                run_id="run-1",
                policy_projection=projection,
                action_events=[bad_event],
                now=now,
            )

    def test_conflicting_duplicate_action_events_fail_closed(self) -> None:
        document, policy_doc = self._valid_document_and_policy()
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        projection = _projection(policy_doc=policy_doc)
        first = _event(
            event_type="intent",
            action_id="dup:1",
            operation="create-comment",
            target_number=1,
            idempotency_key="key-a",
            recorded_at=now,
            run_id="run-1",
        )
        # Same actionId, but disagrees on operation: a conflicting duplicate.
        second = _event(
            event_type="intent",
            action_id="dup:1",
            operation="edit-comment",
            target_number=1,
            idempotency_key="key-a",
            recorded_at=now,
            run_id="run-1",
        )

        with self.assertRaises(ps.PolicySelectionError):
            ps.build_policy_selection(
                document,
                run_id="run-1",
                policy_projection=projection,
                action_events=[first, second],
                now=now,
            )

    def test_malformed_proposals_document_fails_closed(self) -> None:
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        projection = _projection(policy_doc=None)

        with self.assertRaises((TypeError, ValueError)):
            ps.build_policy_selection(
                "not-a-document",
                run_id="run-1",
                policy_projection=projection,
                action_events=[],
                now=now,
            )

    def test_missing_run_id_fails_closed(self) -> None:
        document, policy_doc = self._valid_document_and_policy()
        now = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        projection = _projection(policy_doc=policy_doc)

        with self.assertRaises(ps.PolicySelectionError):
            ps.build_policy_selection(
                document, run_id="", policy_projection=projection, action_events=[], now=now
            )

    def test_naive_now_fails_closed(self) -> None:
        document, policy_doc = self._valid_document_and_policy()
        projection = _projection(policy_doc=policy_doc)

        with self.assertRaises(ps.PolicySelectionError):
            ps.build_policy_selection(
                document,
                run_id="run-1",
                policy_projection=projection,
                action_events=[],
                now=datetime(2026, 9, 3, 18, 0),  # no tzinfo
            )


class RenderTests(unittest.TestCase):
    def test_render_section_is_deterministic_and_informative(self) -> None:
        now = datetime(2026, 9, 3, 16, 5, tzinfo=UTC)
        document = _load_json(PROPOSALS_FIXTURE_PATH)
        policy_doc = _policy_document(created_at_utc=datetime(2026, 9, 3, 16, 0, tzinfo=UTC))
        projection = _projection(policy_doc=policy_doc)
        selection = ps.build_policy_selection(
            document, run_id="run-1", policy_projection=projection, action_events=[], now=now
        )

        first = ps.render_policy_selection_section(selection)
        second = ps.render_policy_selection_section(selection)

        self.assertEqual(first, second)
        self.assertIn("Policy-aware action selection", first)
        self.assertIn("Automatic", first)
        self.assertIn("19166", first)
        self.assertIn("policy:1", first)
        self.assertIn("edit-comment", first)

    def test_render_section_handles_no_candidates(self) -> None:
        empty_selection = {
            "automaticActionIds": [],
            "exactActionIds": [],
            "selectedActionIds": [],
            "candidates": [],
            "policyRevisionId": None,
            "coordinatorStateRevision": 0,
            "maximumWriteExposure": {"thisRun": 0, "rolling24h": 0},
            "budgets": {
                cls: {
                    "enabled": False,
                    "maxPerRun": 0,
                    "maxRolling24h": 0,
                    "usedThisRun": 0,
                    "usedRolling24h": 0,
                    "remainingThisRun": 0,
                    "remainingRolling24h": 0,
                }
                for cls in OPERATION_CLASSES
            },
        }

        rendered = ps.render_policy_selection_section(empty_selection)

        self.assertIn("No candidates", rendered)


if __name__ == "__main__":
    unittest.main()
