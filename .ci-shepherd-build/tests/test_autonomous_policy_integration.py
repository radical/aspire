"""Proves execute_actions.py's live GitHub-mutation boundary when it consumes
an autonomous one-action authorization grant plus its bound
`policy-selection.json`, end-to-end, through a recording fake actor.

The local coordinator (Task 4/authorization.py) can already mint an
autonomous grant bound to a real `build_policy_selection` artifact, and
`load_authorized_execution` already knows how to validate and consume one --
but until execute_actions.py's CLI parses `--autonomous-policy` and
`--policy-selection` and forwards them, the executor has no way to reach that
code path at all. These tests build the frozen artifacts with the real
policy/coordinator/selection/grant APIs (never a hand-rolled stand-in), drive
the CLI exactly as a caller would, and inspect the real
`action-events.jsonl` ledger -- not just mock call counts -- to prove intent
precedes mutation, live preflight still runs against the fake's answers, and
crash/replay/re-grant never mutate twice.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

SCRIPTS_ROOT = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import ci_shepherd.policy_selection as ps  # noqa: E402
from ci_shepherd.authorization import (  # noqa: E402
    AuthorizationBudget,
    AuthorizationError,
    AuthorizationGrant,
    AuthorizedExecution,
    AutonomousPolicyLicense,
    generate_authorization_grant,
    write_authorization_grant,
)
from ci_shepherd.coordinator_state import CoordinatorStateStore  # noqa: E402
from ci_shepherd.execution_state import ExecutionBudgetError  # noqa: E402
from ci_shepherd.operation_policy import DEFAULT_CAPS, OPERATION_CLASSES  # noqa: E402

# Reuses test_actor.py's own runner-level fakes -- the same convention as
# test_review_selection.py's `from test_poc import ...` -- so these tests
# construct the REAL GitHubActorClient (not a stand-in for it) and fake only
# the outermost `gh api` subprocess boundary.
from test_actor import SequencedRunner  # noqa: E402


def load_script(name: str):
    """Load a `.ci-shepherd-build/scripts/{name}.py` module by file path, the
    same way `tests/test_scripts.py` does, so patching
    `module.GitHubActorClient` patches the exact attribute `execute_actions.main`
    resolves at call time.
    """

    path = SCRIPTS_ROOT / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
        "deniedActionIds": [],
        "deniedTargets": [],
    }


class RecordingActor:
    """A recording fake `ActorClient` that proves the real boundary rather
    than merely counting mock calls.

    Every method returns HTTP-response-shaped data the real
    `execute_action`/`reconcile_action` preflight logic actually inspects (CI
    labels, `issueUpdatedAt`, comment ownership) -- nothing here is a
    rubber-stamped bypass, so a caller-supplied answer that no longer matches
    the frozen proposal genuinely trips the same "stale" preflight
    production would. `create_comment` additionally re-reads
    `action-events.jsonl` from disk before recording itself, proving the
    durable "intent" record is already fsynced strictly before this, the one
    mutating call under test, ever runs.
    """

    def __init__(
        self,
        *,
        events_path: Path,
        login: str,
        issue_labels: list[str],
        issue_updated_at: str,
        comment_body: str,
        created_comment_id: int = 5001,
    ) -> None:
        self.events_path = events_path
        self.login = login
        self.issue_labels = issue_labels
        self.issue_updated_at = issue_updated_at
        self.comment_body = comment_body
        self.created_comment_id = created_comment_id
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.intent_present_before_mutation: bool | None = None

    def _record(self, name: str, *args: object) -> None:
        self.calls.append((name, args))

    def get_issue(self, repository: str, issue_number: int) -> dict[str, object]:
        self._record("get_issue", repository, issue_number)
        return {
            "state": "open",
            "updated_at": self.issue_updated_at,
            "html_url": f"https://github.com/{repository}/issues/{issue_number}",
            "labels": [{"name": label} for label in self.issue_labels],
        }

    def get_authenticated_login(self) -> str:
        self._record("get_authenticated_login")
        return self.login

    def list_comments(
        self, repository: str, issue_number: int
    ) -> list[dict[str, object]]:
        self._record("list_comments", repository, issue_number)
        return []

    def create_comment(
        self, repository: str, issue_number: int, body: str
    ) -> dict[str, object]:
        self._snapshot_durable_intent()
        self._record("create_comment", repository, issue_number, body)
        return {"id": self.created_comment_id}

    def get_comment(self, repository: str, comment_id: int) -> dict[str, object]:
        self._record("get_comment", repository, comment_id)
        return {
            "body": self.comment_body,
            "html_url": (
                f"https://github.com/{repository}/issues/1#issuecomment-{comment_id}"
            ),
            "user": {"login": self.login},
        }

    def _snapshot_durable_intent(self) -> None:
        if not self.events_path.exists():
            self.intent_present_before_mutation = False
            return
        lines = [
            line
            for line in self.events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.intent_present_before_mutation = any(
            json.loads(line).get("eventType") == "intent" for line in lines
        )

    @property
    def call_names(self) -> list[str]:
        return [name for name, _args in self.calls]


class AutonomousPolicyExecutionBoundaryTests(unittest.TestCase):
    """End-to-end boundary tests for a single `create-comment` action
    autonomously licensed directly by an active operation-policy revision
    (no `dependsOn` chain needed -- see `test_authorization.py`'s
    `AutonomousPolicyGrantTests.test_grant_includes_expected_license_shape`,
    which establishes that a standalone action can be licensed by
    `policy:N` alone).
    """

    def setUp(self) -> None:
        self.execute_script = load_script("execute_actions")
        self.scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch.mkdir(parents=True)
        self.state_dir = (self.scratch / "state").resolve()
        self.proposals_path = self.scratch / "action-proposals.json"
        self.policy_selection_path = self.scratch / "policy-selection.json"
        self.authorization_path = self.scratch / "authorization-grant.json"

        self.repository = "microsoft/aspire"
        self.login = "radical"
        self.run_id = "run-autonomous-1"
        # A real wall-clock "now" (not a frozen historical fixture date) so
        # the production snapshot-freshness and grant-TTL checks --
        # `_production_freshness_deadline`/`_validate_autonomous_policy_grant`
        # -- see a genuinely fresh snapshot/grant when `execute_actions.main`
        # later reads real `datetime.now(UTC)`, exactly like
        # `test_scripts.py`'s existing full-flow tests.
        self.now = datetime.now(UTC)
        collected_at = _rfc3339(self.now)
        self.snapshot_id = f"snapshot:{self.repository}:{collected_at}:r1"
        self.action_id = (
            f"snapshot:{self.repository}:{collected_at}:issue:777:create-comment"
        )
        self.idempotency_key = "issue:777:watch"
        self.body = (
            "[automated] Watching this failure.\n\n"
            f"<!-- ci-shepherd:idempotency-key={self.idempotency_key} -->"
        )
        self.issue_updated_at = collected_at
        self.proposal: dict[str, object] = {
            "actionId": self.action_id,
            "issueNumber": 777,
            "issueUrl": f"https://github.com/{self.repository}/issues/777",
            "operation": "create-comment",
            "idempotencyKey": self.idempotency_key,
            "body": self.body,
            "evidenceIds": ["issue:777", "run:1"],
            "evidenceBasis": "ci-occurrence",
            "expectedIssueState": "open",
            "executionEligibility": {
                "eligible": True,
                "evidenceBasis": "ci-occurrence",
                "ciLabels": ["ci-failure-cause"],
                "occurrenceCount": 3,
                "collectionComplete": True,
                "unavailableEvidenceIds": [],
                "untrustedReferenceEvidenceIds": [],
                "blockingReasons": [],
            },
            "sourceEvidenceFingerprint": {
                "issueUpdatedAt": self.issue_updated_at,
            },
        }
        self.proposals: dict[str, object] = {
            "schemaVersion": 2,
            "repository": self.repository,
            "snapshotId": self.snapshot_id,
            "shepherdAuthor": self.login,
            "generatedAtUtc": collected_at,
            "proposalTtlHours": 1,
            "maxProposalsPerIssue": 2,
            "productionPilotCapability": {
                "schemaVersion": 1,
                "evidenceRound": 1,
            },
            "executionEligibility": {"status": "eligible", "violations": []},
            "proposals": [self.proposal],
            "unchangedIssueNumbers": [],
        }
        self._write_proposals()

        self.store = CoordinatorStateStore(
            self.state_dir, durable_intent_reader=_no_durable_intent
        )
        self._append_policy(
            revision=1, enabled_classes=frozenset({"create-comment"})
        )
        self._build_and_write_selection()
        self.grant = self._mint_grant()

    def tearDown(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)

    # -- fixture plumbing ---------------------------------------------------

    def _write_proposals(self) -> None:
        self.proposals_path.write_bytes(
            (json.dumps(self.proposals, indent=2, sort_keys=True) + "\n").encode()
        )

    def _append_policy(
        self,
        *,
        revision: int,
        enabled_classes: frozenset[str],
        replaces: str | None = None,
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
                status="active",
                replaces=replaces,
                created_at_utc=self.now - timedelta(days=1),
                expires_at_utc=self.now + timedelta(days=30),
                enabled_classes=enabled_classes,
            ),
        )

    def _build_and_write_selection(self, *, action_events=()) -> None:
        projection = self.store.projection(self.repository, now=self.now)
        selection = ps.build_policy_selection(
            self.proposals,
            run_id=self.run_id,
            policy_projection=projection,
            action_events=list(action_events),
            now=self.now,
        )
        self.policy_selection_path.write_bytes(
            (json.dumps(selection, indent=2, sort_keys=True) + "\n").encode()
        )

    def _mint_grant(self, *, output_path: Path | None = None) -> dict[str, object]:
        grant = generate_authorization_grant(
            self.proposals_path,
            action_ids=[self.action_id],
            state_dir=self.state_dir,
            allow_autonomous_policy=True,
            policy_selection_path=self.policy_selection_path,
            policy_action_id=self.action_id,
            now=self.now,
        )
        write_authorization_grant(grant, output_path or self.authorization_path)
        return grant

    def _argv(
        self,
        *,
        autonomous_policy: bool = True,
        policy_selection: bool = True,
        authorization_path: Path | None = None,
    ) -> list[str]:
        argv = [
            "--proposals",
            str(self.proposals_path),
            "--state-dir",
            str(self.state_dir),
            "--authorization",
            str(authorization_path or self.authorization_path),
            "--action-id",
            self.action_id,
            "--execute",
        ]
        if autonomous_policy:
            argv.append("--autonomous-policy")
        if policy_selection:
            argv += ["--policy-selection", str(self.policy_selection_path)]
        return argv

    def _actor(self, **overrides: object) -> RecordingActor:
        defaults: dict[str, object] = dict(
            events_path=self.state_dir / "action-events.jsonl",
            login=self.login,
            issue_labels=["ci-failure-cause"],
            issue_updated_at=self.issue_updated_at,
            comment_body=self.body,
        )
        defaults.update(overrides)
        return RecordingActor(**defaults)  # type: ignore[arg-type]

    def _run(
        self, argv: list[str], actor: object
    ) -> tuple[int, dict[str, object]]:
        stdout = io.StringIO()
        with (
            patch.object(
                self.execute_script, "GitHubActorClient", return_value=actor
            ),
            contextlib.redirect_stdout(stdout),
        ):
            code = self.execute_script.main(argv)
        return code, json.loads(stdout.getvalue())

    def _events(self) -> list[dict[str, object]]:
        events_path = self.state_dir / "action-events.jsonl"
        if not events_path.exists():
            return []
        return [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _assert_zero_write(self) -> None:
        self.assertEqual([], self._events())

    # -- zero-write boundary assertions --------------------------------------

    def test_zero_write_without_valid_authorization_grant(self) -> None:
        missing_path = self.scratch / "no-such-grant.json"
        argv = self._argv(authorization_path=missing_path)
        with (
            patch.object(
                self.execute_script,
                "GitHubActorClient",
                side_effect=AssertionError("must not construct a client"),
            ),
            self.assertRaises(AuthorizationError),
        ):
            self.execute_script.main(argv)
        self._assert_zero_write()

    def test_zero_write_without_autonomous_policy_flag_even_with_valid_grant(
        self,
    ) -> None:
        argv = self._argv(autonomous_policy=False)
        with (
            patch.object(
                self.execute_script,
                "GitHubActorClient",
                side_effect=AssertionError("must not construct a client"),
            ),
            self.assertRaisesRegex(
                AuthorizationError,
                "protected during remediation",
            ),
        ):
            self.execute_script.main(argv)
        self._assert_zero_write()

    def test_zero_write_with_missing_policy_selection(self) -> None:
        argv = self._argv(policy_selection=False)
        with (
            patch.object(
                self.execute_script,
                "GitHubActorClient",
                side_effect=AssertionError("must not construct a client"),
            ),
            self.assertRaisesRegex(
                AuthorizationError,
                "requires the bound policy selection artifact",
            ),
        ):
            self.execute_script.main(argv)
        self._assert_zero_write()

    def test_zero_write_with_tampered_policy_selection(self) -> None:
        with self.policy_selection_path.open("ab") as handle:
            handle.write(b" ")  # Flip the byte digest without breaking JSON.
        argv = self._argv()
        with (
            patch.object(
                self.execute_script,
                "GitHubActorClient",
                side_effect=AssertionError("must not construct a client"),
            ),
            self.assertRaisesRegex(
                AuthorizationError,
                "policySelectionDigest does not match",
            ),
        ):
            self.execute_script.main(argv)
        self._assert_zero_write()

    def test_zero_write_with_tampered_proposal_body(self) -> None:
        tampered = json.loads(json.dumps(self.proposals))
        tampered["proposals"][0]["body"] = (
            "[automated] Tampered body.\n\n"
            f"<!-- ci-shepherd:idempotency-key={self.idempotency_key} -->"
        )
        self.proposals_path.write_bytes(
            (json.dumps(tampered, indent=2, sort_keys=True) + "\n").encode()
        )
        argv = self._argv()
        with (
            patch.object(
                self.execute_script,
                "GitHubActorClient",
                side_effect=AssertionError("must not construct a client"),
            ),
            self.assertRaisesRegex(
                AuthorizationError,
                "proposalsDigest does not match",
            ),
        ):
            self.execute_script.main(argv)
        self._assert_zero_write()

    # -- happy path: exactly one write, matching the frozen proposal ---------

    def test_exactly_one_write_matches_frozen_proposal_with_durable_intent_first(
        self,
    ) -> None:
        argv = self._argv()
        actor = self._actor()

        code, result = self._run(argv, actor)

        self.assertEqual(0, code)
        self.assertEqual("executed", result["outcome"])
        self.assertEqual(actor.created_comment_id, result["result"]["commentId"])

        mutating_calls = [call for call in actor.calls if call[0] == "create_comment"]
        self.assertEqual(1, len(mutating_calls))
        _name, args = mutating_calls[0]
        self.assertEqual((self.repository, 777, self.body), args)

        self.assertTrue(actor.intent_present_before_mutation)

        # Live preflight actually ran against the fake's answers -- this is
        # not a shortcut around it.
        self.assertIn("get_issue", actor.call_names)
        self.assertIn("get_authenticated_login", actor.call_names)
        self.assertIn("list_comments", actor.call_names)
        self.assertLess(
            actor.call_names.index("get_issue"),
            actor.call_names.index("create_comment"),
        )

        events = self._events()
        self.assertEqual(["intent", "terminal"], [e["eventType"] for e in events])
        self.assertEqual("create-comment", events[0]["operation"])
        self.assertEqual(self.repository, events[0]["repository"])
        self.assertEqual(self.action_id, events[0]["actionId"])

    # -- live preflight is not mocked away ------------------------------------

    def test_ci_label_preflight_still_blocks_mutation_when_label_missing(
        self,
    ) -> None:
        argv = self._argv()
        actor = self._actor(issue_labels=[])

        code, result = self._run(argv, actor)

        self.assertEqual(0, code)
        self.assertEqual("stale", result["outcome"])
        self.assertEqual("missing-ci-label", result["reason"])
        self.assertNotIn("create_comment", actor.call_names)
        events = self._events()
        self.assertEqual(["intent", "terminal"], [e["eventType"] for e in events])

    def test_source_evidence_preflight_still_blocks_mutation_when_issue_changed(
        self,
    ) -> None:
        argv = self._argv()
        changed_updated_at = (self.now + timedelta(minutes=1)).isoformat().replace(
            "+00:00", "Z"
        )
        actor = self._actor(issue_updated_at=changed_updated_at)

        code, result = self._run(argv, actor)

        self.assertEqual(0, code)
        self.assertEqual("stale", result["outcome"])
        self.assertEqual("source-evidence-changed", result["reason"])
        self.assertNotIn("create_comment", actor.call_names)
        events = self._events()
        self.assertEqual(["intent", "terminal"], [e["eventType"] for e in events])

    # -- crash / reconcile / replay -------------------------------------------

    def test_crash_after_intent_before_terminal_then_reconciles_without_remutating(
        self,
    ) -> None:
        argv = self._argv()
        crashing_actor = self._actor()
        with (
            patch.object(
                self.execute_script, "GitHubActorClient", return_value=crashing_actor
            ),
            patch.object(
                self.execute_script, "execute_action", side_effect=SystemExit(70)
            ),
            self.assertRaises(SystemExit),
        ):
            self.execute_script.main(argv)

        events = self._events()
        self.assertEqual(["intent"], [e["eventType"] for e in events])
        self.assertEqual([], crashing_actor.calls)

        reconciling_actor = self._actor()
        code, result = self._run(argv, reconciling_actor)

        self.assertEqual(0, code)
        self.assertNotIn("create_comment", reconciling_actor.call_names)
        self.assertEqual("indeterminate", result["outcome"])
        self.assertEqual("mutation-not-confirmed", result["reason"])
        events = self._events()
        self.assertEqual(
            ["intent", "terminal"], [e["eventType"] for e in events]
        )

    def test_successful_terminal_replay_never_calls_mutation_twice(self) -> None:
        argv = self._argv()
        first_actor = self._actor()
        code, result = self._run(argv, first_actor)
        self.assertEqual(0, code)
        self.assertEqual("executed", result["outcome"])
        self.assertEqual(
            1, len([call for call in first_actor.calls if call[0] == "create_comment"])
        )

        replay_stdout = io.StringIO()
        with (
            patch.object(
                self.execute_script,
                "GitHubActorClient",
                side_effect=AssertionError("replay must not construct a client"),
            ),
            contextlib.redirect_stdout(replay_stdout),
        ):
            self.assertEqual(0, self.execute_script.main(argv))
        self.assertEqual(result, json.loads(replay_stdout.getvalue()))

        events = self._events()
        self.assertEqual(["intent", "terminal"], [e["eventType"] for e in events])

    def test_fresh_grant_for_same_action_replays_ledger_without_remutating(
        self,
    ) -> None:
        argv = self._argv()
        first_actor = self._actor()
        code, result = self._run(argv, first_actor)
        self.assertEqual(0, code)
        self.assertEqual("executed", result["outcome"])

        second_authorization_path = self.scratch / "authorization-grant-2.json"
        second_grant = self._mint_grant(output_path=second_authorization_path)
        self.assertNotEqual(self.grant["grantId"], second_grant["grantId"])

        second_argv = self._argv(authorization_path=second_authorization_path)
        replay_stdout = io.StringIO()
        with (
            patch.object(
                self.execute_script,
                "GitHubActorClient",
                side_effect=AssertionError(
                    "a fresh grant for an already-terminal action must not "
                    "construct a client"
                ),
            ),
            contextlib.redirect_stdout(replay_stdout),
        ):
            self.assertEqual(0, self.execute_script.main(second_argv))
        self.assertEqual(result, json.loads(replay_stdout.getvalue()))

        events = self._events()
        self.assertEqual(["intent", "terminal"], [e["eventType"] for e in events])

    # -- one representative failure per remaining validation layer -----------

    def test_denied_when_licensing_policy_revision_is_no_longer_active(self) -> None:
        # A fresh replacement revision makes `policy:1` -- the license this
        # grant was minted under -- no longer the effective revision, which
        # `_resolve_autonomous_license_source` re-checks at load time
        # independent of the grant's own self-declared license.
        self._append_policy(
            revision=2,
            enabled_classes=frozenset({"create-comment"}),
            replaces="policy:1",
        )
        argv = self._argv()
        with (
            patch.object(
                self.execute_script,
                "GitHubActorClient",
                side_effect=AssertionError("must not construct a client"),
            ),
            self.assertRaisesRegex(
                AuthorizationError,
                "Licensing policy revision is no longer effective",
            ),
        ):
            self.execute_script.main(argv)
        self._assert_zero_write()

    def test_repository_hard_ceiling_exhausted_before_write(self) -> None:
        events_path = self.state_dir / "action-events.jsonl"
        seed_lines = [
            json.dumps(
                {
                    "schemaVersion": 1,
                    "eventType": "intent",
                    "recordedAt": _rfc3339(self.now),
                    "repository": self.repository,
                    "actionId": f"seed-action-{index}",
                    "runId": self.run_id,
                    "snapshotId": self.snapshot_id,
                }
            )
            for index in range(100)
        ]
        events_path.write_text("\n".join(seed_lines) + "\n", encoding="utf-8")

        argv = self._argv()
        with (
            patch.object(
                self.execute_script,
                "GitHubActorClient",
                side_effect=AssertionError("must not construct a client"),
            ),
            self.assertRaisesRegex(
                ExecutionBudgetError,
                "hard ceiling for this run is exhausted",
            ),
        ):
            self.execute_script.main(argv)

        events = self._events()
        self.assertEqual(100, len(events))  # No new intent was appended.


class AutonomousProtectedOverrideMappingTests(unittest.TestCase):
    """Proves execute_actions.py routes an autonomous grant's *revalidated*
    operation class -- never the `--autonomous-policy` CLI flag alone -- into
    `GitHubActorClient`'s `protected_comment_repositories` /
    `protected_delegation_repositories` override sets.

    Unlike `AutonomousPolicyExecutionBoundaryTests` above, these tests
    construct the REAL `GitHubActorClient` (not `RecordingActor`) and fake
    only the outermost `gh api` subprocess runner, reusing test_actor.py's
    own `SequencedRunner`. This proves a valid autonomous create-comment
    grant genuinely reaches GitHub's HTTP boundary on the protected
    microsoft/aspire repository -- the actual live-pilot blocker this commit
    fixes -- and that autonomous close-issue there still is not.
    """

    def setUp(self) -> None:
        self.execute_script = load_script("execute_actions")
        self.scratch = Path(__file__).parent / ".artifacts" / self._testMethodName
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch.mkdir(parents=True)
        self.state_dir = (self.scratch / "state").resolve()
        self.proposals_path = self.scratch / "action-proposals.json"
        self.policy_selection_path = self.scratch / "policy-selection.json"
        self.authorization_path = self.scratch / "authorization-grant.json"

        self.repository = "microsoft/aspire"
        self.login = "radical"
        self.run_id = "run-autonomous-override-1"
        self.now = datetime.now(UTC)

    def tearDown(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)

    # -- fixture plumbing (mirrors AutonomousPolicyExecutionBoundaryTests, ---
    # -- generalized across operations so a genuine, freshly-minted grant ---
    # -- can be built for create-comment, close-issue, or assign-copilot. ---

    def _mint_fixture(
        self,
        *,
        operation: str,
        enabled_classes: frozenset[str],
        proposal_extra: dict[str, object],
        idempotency_key: str,
    ) -> str:
        collected_at = _rfc3339(self.now)
        snapshot_id = f"snapshot:{self.repository}:{collected_at}:r1"
        action_id = f"snapshot:{self.repository}:{collected_at}:issue:777:{operation}"
        proposal: dict[str, object] = {
            "actionId": action_id,
            "issueNumber": 777,
            "issueUrl": f"https://github.com/{self.repository}/issues/777",
            "operation": operation,
            "idempotencyKey": idempotency_key,
            "evidenceIds": ["issue:777", "run:1"],
            "evidenceBasis": "ci-occurrence",
            "expectedIssueState": "open",
            "executionEligibility": {
                "eligible": True,
                "evidenceBasis": "ci-occurrence",
                "ciLabels": ["ci-failure-cause"],
                "occurrenceCount": 3,
                "collectionComplete": True,
                "unavailableEvidenceIds": [],
                "untrustedReferenceEvidenceIds": [],
                "blockingReasons": [],
            },
            "sourceEvidenceFingerprint": {"issueUpdatedAt": collected_at},
        }
        proposal.update(proposal_extra)
        proposals: dict[str, object] = {
            "schemaVersion": 2,
            "repository": self.repository,
            "snapshotId": snapshot_id,
            "shepherdAuthor": self.login,
            "generatedAtUtc": collected_at,
            "proposalTtlHours": 1,
            "maxProposalsPerIssue": 2,
            "productionPilotCapability": {"schemaVersion": 1, "evidenceRound": 1},
            "executionEligibility": {"status": "eligible", "violations": []},
            "proposals": [proposal],
            "unchangedIssueNumbers": [],
        }
        self.proposals_path.write_bytes(
            (json.dumps(proposals, indent=2, sort_keys=True) + "\n").encode()
        )

        store = CoordinatorStateStore(
            self.state_dir, durable_intent_reader=_no_durable_intent
        )
        expected_revision = store.projection(self.repository, now=self.now)[
            "stateRevision"
        ]
        store.append_policy_revision(
            repository=self.repository,
            expected_revision=expected_revision,
            document=_policy_document(
                repository=self.repository,
                revision=1,
                created_at_utc=self.now - timedelta(days=1),
                expires_at_utc=self.now + timedelta(days=30),
                enabled_classes=enabled_classes,
            ),
        )
        projection = store.projection(self.repository, now=self.now)
        selection = ps.build_policy_selection(
            proposals,
            run_id=self.run_id,
            policy_projection=projection,
            action_events=[],
            now=self.now,
        )
        self.policy_selection_path.write_bytes(
            (json.dumps(selection, indent=2, sort_keys=True) + "\n").encode()
        )

        grant = generate_authorization_grant(
            self.proposals_path,
            action_ids=[action_id],
            state_dir=self.state_dir,
            allow_autonomous_policy=True,
            policy_selection_path=self.policy_selection_path,
            policy_action_id=action_id,
            now=self.now,
        )
        write_authorization_grant(grant, self.authorization_path)
        return action_id

    def _argv(self, action_id: str, *, autonomous_policy: bool = True) -> list[str]:
        argv = [
            "--proposals",
            str(self.proposals_path),
            "--state-dir",
            str(self.state_dir),
            "--authorization",
            str(self.authorization_path),
            "--action-id",
            action_id,
            "--execute",
        ]
        if autonomous_policy:
            argv.append("--autonomous-policy")
        argv += ["--policy-selection", str(self.policy_selection_path)]
        return argv

    def _real_client_factory(self, runner: SequencedRunner):
        """Constructs the REAL `GitHubActorClient` execute_actions.py would,
        forwarding every kwarg it receives, and only substitutes the runner
        (the final `gh api` subprocess boundary) -- never the class itself.
        """

        real_client_class = self.execute_script.GitHubActorClient

        def factory(**kwargs: object):
            return real_client_class(runner=runner, **kwargs)

        return factory

    @staticmethod
    def _call_method(call: tuple[list[str], object]) -> str:
        command, _payload = call
        return command[command.index("--method") + 1]

    def _events(self) -> list[dict[str, object]]:
        events_path = self.state_dir / "action-events.jsonl"
        if not events_path.exists():
            return []
        return [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    # -- the actual live-pilot blocker: create-comment reaches the runner ---

    def test_autonomous_create_comment_grant_reaches_permitted_runner_boundary_on_protected_repository(
        self,
    ) -> None:
        idempotency_key = "issue:777:override-watch"
        body = (
            "[automated] Watching this failure.\n\n"
            f"<!-- ci-shepherd:idempotency-key={idempotency_key} -->"
        )
        action_id = self._mint_fixture(
            operation="create-comment",
            enabled_classes=frozenset({"create-comment"}),
            proposal_extra={"body": body},
            idempotency_key=idempotency_key,
        )
        collected_at = _rfc3339(self.now)
        comment_id = 6001
        issue_payload = {
            "state": "open",
            "updated_at": collected_at,
            "html_url": f"https://github.com/{self.repository}/issues/777",
            "labels": [{"name": "ci-failure-cause"}],
        }
        comment_payload = {
            "body": body,
            "html_url": (
                f"https://github.com/{self.repository}/issues/777"
                f"#issuecomment-{comment_id}"
            ),
            "user": {"login": self.login},
        }
        # Exact call order execute_action's create-comment path issues:
        # get_issue, get_authenticated_login, list_comments (one empty
        # page), create_comment (the mutation under test), get_comment,
        # get_issue again (schemaVersion 2's sourceIssueUpdatedAt).
        runner = SequencedRunner(
            [
                issue_payload,
                {"login": self.login},
                [],
                {"id": comment_id},
                comment_payload,
                issue_payload,
            ]
        )

        stdout = io.StringIO()
        with (
            patch.object(
                self.execute_script,
                "GitHubActorClient",
                new=self._real_client_factory(runner),
            ),
            contextlib.redirect_stdout(stdout),
        ):
            code = self.execute_script.main(self._argv(action_id))
        result = json.loads(stdout.getvalue())

        self.assertEqual(0, code)
        self.assertEqual("executed", result["outcome"], result)
        self.assertEqual(comment_id, result["result"]["commentId"])

        methods = [self._call_method(call) for call in runner.calls]
        mutating_indices = [
            index for index, method in enumerate(methods) if method != "GET"
        ]
        self.assertEqual(1, len(mutating_indices), methods)
        mutating_index = mutating_indices[0]
        self.assertEqual("POST", methods[mutating_index])
        mutating_command, mutating_payload = runner.calls[mutating_index]
        self.assertTrue(
            mutating_command[-1].endswith("/issues/777/comments"),
            mutating_command[-1],
        )
        self.assertEqual(body, mutating_payload["body"])

        events = self._events()
        self.assertEqual(["intent", "terminal"], [e["eventType"] for e in events])

    def test_autonomous_create_comment_grant_without_autonomous_policy_flag_reaches_zero_runner_calls(
        self,
    ) -> None:
        idempotency_key = "issue:777:override-watch-2"
        body = (
            "[automated] Watching this failure.\n\n"
            f"<!-- ci-shepherd:idempotency-key={idempotency_key} -->"
        )
        action_id = self._mint_fixture(
            operation="create-comment",
            enabled_classes=frozenset({"create-comment"}),
            proposal_extra={"body": body},
            idempotency_key=idempotency_key,
        )
        runner = SequencedRunner([])

        with (
            patch.object(
                self.execute_script,
                "GitHubActorClient",
                new=self._real_client_factory(runner),
            ),
            self.assertRaisesRegex(AuthorizationError, "protected during remediation"),
        ):
            self.execute_script.main(self._argv(action_id, autonomous_policy=False))

        self.assertEqual([], runner.calls)
        self.assertEqual([], self._events())

    # -- constructor-level proof for delegate-copilot (no live task launch) -

    def test_delegate_copilot_autonomous_grant_maps_delegation_override_only(
        self,
    ) -> None:
        action_id = self._mint_fixture(
            operation="assign-copilot",
            enabled_classes=frozenset({"delegate-copilot"}),
            proposal_extra={
                "targetRepository": self.repository,
                "baseBranch": "main",
                "customInstructions": "Fix issue #777.",
                "model": "",
            },
            idempotency_key="issue:777:assign",
        )

        class _StoppedBeforeDelegationLaunch(Exception):
            """Sentinel proving nothing beyond client construction ran."""

        with (
            patch.object(self.execute_script, "GitHubActorClient") as client_cls,
            patch.object(
                self.execute_script,
                "reserve_delegation_start",
                side_effect=_StoppedBeforeDelegationLaunch(),
            ),
            self.assertRaises(_StoppedBeforeDelegationLaunch),
        ):
            self.execute_script.main(self._argv(action_id))

        client_cls.assert_called_once()
        kwargs = client_cls.call_args.kwargs
        self.assertEqual(
            {self.repository}, kwargs["protected_delegation_repositories"]
        )
        self.assertEqual(set(), kwargs["protected_comment_repositories"])

    # -- deliberately NOT mapped: close-issue stays denied ------------------

    def test_autonomous_close_issue_grant_remains_blocked_on_protected_repository_with_zero_runner_calls(
        self,
    ) -> None:
        action_id = self._mint_fixture(
            operation="close-issue",
            enabled_classes=frozenset({"close-issue"}),
            proposal_extra={"closeReason": "completed"},
            idempotency_key="issue:777:close",
        )
        collected_at = _rfc3339(self.now)
        issue_payload = {
            "state": "open",
            "updated_at": collected_at,
            "html_url": f"https://github.com/{self.repository}/issues/777",
            "labels": [{"name": "ci-failure-cause"}],
        }
        # Only the common preflight (get_issue, get_authenticated_login)
        # reaches the runner; the close_issue PATCH must never get there.
        runner = SequencedRunner([issue_payload, {"login": self.login}])

        stdout = io.StringIO()
        with (
            patch.object(
                self.execute_script,
                "GitHubActorClient",
                new=self._real_client_factory(runner),
            ),
            contextlib.redirect_stdout(stdout),
        ):
            code = self.execute_script.main(self._argv(action_id))
        result = json.loads(stdout.getvalue())

        self.assertEqual(0, code)
        self.assertEqual("indeterminate", result["outcome"], result)
        self.assertIn("protected", result["reason"])

        methods = [self._call_method(call) for call in runner.calls]
        self.assertEqual(["GET", "GET"], methods)

        events = self._events()
        self.assertEqual(["intent", "terminal"], [e["eventType"] for e in events])


if __name__ == "__main__":
    unittest.main()
