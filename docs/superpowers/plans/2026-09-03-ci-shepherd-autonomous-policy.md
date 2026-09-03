# CI Shepherd Autonomous Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make CI Shepherd run headlessly under durable, capped operation-class policy while preserving frozen actions, one-action grants, crash reconciliation, and a maximum of three concurrent investigation sessions.

**Architecture:** Add an append-only coordinator ledger for standing-policy revisions and exact decisions, then derive a policy-aware selection from frozen proposals plus authoritative action events. A deterministic coordinator CLI is the shared boundary for scheduled operation and the later Canvas; GitHub mutation remains exclusively in the existing executor and requires a short-lived exact child grant. Investigation scheduling is a durable admission queue: the Python coordinator decides which requests may start, while the autonomous skill uses the existing session tool to launch only those admitted requests.

**Tech Stack:** Python 3 standard library, `unittest`, JSON/JSONL durable artifacts, existing cross-platform file locking, existing CI Shepherd proposal/grant/executor pipeline.

---

## Quick read

- **What changes:** permission filtering moves before deterministic selection, so
  an edit-only policy can select edits without first authorizing higher-ranked
  creates.
- **What stays fixed:** proposals, action bodies, targets, executor preflights,
  intent-before-write, and reconciliation.
- **Authorization:** standing policy licenses one short-lived exact child grant
  at a time; it never becomes a broad GitHub credential.
- **Durability:** policy revisions, exact decisions, selection, grants, intents,
  results, and budgets survive process and Canvas restarts.
- **Investigations:** five requests may complete in one cycle, but no more than
  three fresh sessions may be active concurrently.
- **Production gate:** finish local, fixture, recording-fake, headless, and
  independent-review validation before enabling one mutation class.

```mermaid
flowchart LR
    A[Proposals] --> B[Policy and exact decisions]
    B --> C[Deterministic permitted set]
    C --> D[Freeze one action]
    D --> E[Exact child grant]
    E --> F[Intent under both locks]
    F --> G[Execute or reconcile]
    G --> H[Durable result and budgets]
```

## Scope and file structure

This plan produces working headless software without any Canvas dependency.

**Create:**

- `.ci-shepherd-build/scripts/ci_shepherd/operation_policy.py` — strict policy schema, operation classification, defaults, expiry, and hard ceilings.
- `.ci-shepherd-build/scripts/ci_shepherd/coordinator_state.py` — append-only policy/decision ledger, monotonic state revision, locked compare-and-append commands, and read projections.
- `.ci-shepherd-build/scripts/ci_shepherd/policy_selection.py` — policy-aware filtering, cap accounting, deterministic ranking, exact overrides, and selection rendering.
- `.ci-shepherd-build/scripts/ci_shepherd/investigation_scheduler.py` — durable three-slot admission and restart reconstruction.
- `.ci-shepherd-build/scripts/coordinator.py` — deterministic query/command CLI used by headless operation and the Canvas adapter.
- `.ci-shepherd-build/tests/test_operation_policy.py`
- `.ci-shepherd-build/tests/test_coordinator_state.py`
- `.ci-shepherd-build/tests/test_policy_selection.py`
- `.ci-shepherd-build/tests/test_investigation_scheduler.py`
- `.ci-shepherd-build/tests/test_coordinator.py`
- `.ci-shepherd-build/tests/fixtures/production-policy-selection-20260903/action-proposals.json`
- `.ci-shepherd-build/tests/fixtures/production-policy-selection-20260903/original-comment-selection.json`

**Modify:**

- `.ci-shepherd-build/scripts/ci_shepherd/authorization.py` — optional autonomous child-grant license and exact selection binding.
- `.ci-shepherd-build/scripts/ci_shepherd/execution_state.py` — atomically enforce durable policy budgets when reserving an intent.
- `.ci-shepherd-build/scripts/ci_shepherd/investigations.py` — persist
  pre-launch admissions in the existing investigation lifecycle ledger.
- `.ci-shepherd-build/scripts/create_authorization.py` — autonomous child-grant command arguments.
- `.ci-shepherd-build/scripts/execute_actions.py` — pass the autonomous
  capability and bound policy-selection artifact into existing execution.
- `.ci-shepherd-build/scripts/cycle.py` — emit policy selection/projection artifacts and coordinator stages.
- `.ci-shepherd-build/scripts/ci_shepherd/retrospective.py` — admit policy artifacts to retrospective evidence.
- `.ci-shepherd-build/tests/test_authorization.py`
- `.ci-shepherd-build/tests/test_execution_state.py`
- `.ci-shepherd-build/tests/test_cycle.py`
- `.ci-shepherd-build/SKILL.md` — autonomous loop, investigation admission protocol, and production rollout guard.

Do not add executable rerun APIs in this change. `rerun-or-retry` is a valid policy class with no current proposal mapping; projections must show zero current candidates and the Canvas must later warn about that. Current mappings are:

```python
OPERATION_CLASS_BY_OPERATION = {
    "create-comment": "create-comment",
    "edit-comment": "edit-comment",
    "close-issue": "close-issue",
    "assign-copilot": "delegate-copilot",
}
```

`unassign-copilot`, quarantine source mutation, pushes, and pull-request creation remain outside this policy.

## Invariants to keep visible during implementation

1. A policy is permission, not an executable grant.
2. Each child grant authorizes exactly one frozen action and expires after at most 15 minutes.
3. Exact rejection wins over standing policy and exact approval.
4. Policy deny rules and exact rejection are absolute. Exact approval can bypass
   a disabled/exhausted class cap, but not the 100/run or 300/24h repository
   ceilings.
5. Budget checks and intent append happen under the same action-event lock.
6. Policy revocation stops new grants; it never erases or retries an existing intent.
7. Headless results are identical whether or not a Canvas extension is installed.
8. Three is an investigation concurrency limit, not a cycle-total limit.

### Task 1: Define the operation-policy schema

**Files:**

- Create: `.ci-shepherd-build/scripts/ci_shepherd/operation_policy.py`
- Create: `.ci-shepherd-build/tests/test_operation_policy.py`

- [ ] **Step 1: Write failing schema and classification tests**

Add tests that construct the complete document rather than relying on defaults:

```python
from datetime import UTC, datetime
import unittest

from ci_shepherd.operation_policy import (
    DEFAULT_CAPS,
    HARD_MAX_PER_RUN,
    HARD_MAX_ROLLING_24H,
    OperationPolicyError,
    classify_operation,
    load_operation_policy_document,
)


class OperationPolicyTests(unittest.TestCase):
    def policy(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "repository": "microsoft/aspire",
            "revisionId": "policy:1",
            "revision": 1,
            "status": "active",
            "createdAtUtc": "2026-09-03T16:00:00Z",
            "expiresAtUtc": "2026-10-03T16:00:00Z",
            "actor": "github:radical",
            "replacesRevisionId": None,
            "operationClasses": {
                name: {
                    "enabled": name == "edit-comment",
                    "maxPerRun": caps["maxPerRun"],
                    "maxRolling24h": caps["maxRolling24h"],
                }
                for name, caps in DEFAULT_CAPS.items()
            },
            "deniedActionIds": [],
            "deniedTargets": [],
        }

    def test_loads_complete_active_policy(self) -> None:
        policy = load_operation_policy_document(
            self.policy(),
            now=datetime(2026, 9, 3, 16, 1, tzinfo=UTC),
        )

        self.assertEqual("policy:1", policy.revision_id)
        self.assertTrue(policy.operation_classes["edit-comment"].enabled)
        self.assertEqual("sha256:", policy.digest[:7])

    def test_rejects_total_caps_above_hard_ceiling(self) -> None:
        document = self.policy()
        document["operationClasses"]["edit-comment"]["maxPerRun"] = HARD_MAX_PER_RUN
        document["operationClasses"]["create-comment"]["maxPerRun"] = 1

        with self.assertRaisesRegex(OperationPolicyError, "100 per run"):
            load_operation_policy_document(document)

    def test_rejects_expiry_beyond_ninety_days(self) -> None:
        document = self.policy()
        document["expiresAtUtc"] = "2026-12-03T16:00:01Z"

        with self.assertRaisesRegex(OperationPolicyError, "90 days"):
            load_operation_policy_document(document)

    def test_classifies_only_supported_executor_operations(self) -> None:
        self.assertEqual("create-comment", classify_operation("create-comment"))
        self.assertEqual("edit-comment", classify_operation("edit-comment"))
        self.assertEqual("close-issue", classify_operation("close-issue"))
        self.assertEqual("delegate-copilot", classify_operation("assign-copilot"))
        self.assertIsNone(classify_operation("unassign-copilot"))
        self.assertIsNone(classify_operation("prepare-quarantine-pr"))
        self.assertIsNone(classify_operation("request-rerun"))

    def test_default_caps_match_the_approved_design(self) -> None:
        self.assertEqual(
            {
                "create-comment": {"maxPerRun": 10, "maxRolling24h": 30},
                "edit-comment": {"maxPerRun": 10, "maxRolling24h": 30},
                "close-issue": {"maxPerRun": 5, "maxRolling24h": 10},
                "delegate-copilot": {"maxPerRun": 3, "maxRolling24h": 5},
                "rerun-or-retry": {"maxPerRun": 5, "maxRolling24h": 15},
            },
            DEFAULT_CAPS,
        )
        self.assertEqual(100, HARD_MAX_PER_RUN)
        self.assertEqual(300, HARD_MAX_ROLLING_24H)
```

- [ ] **Step 2: Run the test and verify the missing module failure**

Run:

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest tests.test_operation_policy -v
```

Expected: `ModuleNotFoundError: No module named 'ci_shepherd.operation_policy'`.

- [ ] **Step 3: Implement strict immutable policy types**

Implement:

```python
OPERATION_CLASSES = (
    "create-comment",
    "edit-comment",
    "close-issue",
    "delegate-copilot",
    "rerun-or-retry",
)
DEFAULT_CAPS = {
    "create-comment": {"maxPerRun": 10, "maxRolling24h": 30},
    "edit-comment": {"maxPerRun": 10, "maxRolling24h": 30},
    "close-issue": {"maxPerRun": 5, "maxRolling24h": 10},
    "delegate-copilot": {"maxPerRun": 3, "maxRolling24h": 5},
    "rerun-or-retry": {"maxPerRun": 5, "maxRolling24h": 15},
}
HARD_MAX_PER_RUN = 100
HARD_MAX_ROLLING_24H = 300
DEFAULT_EXPIRY_DAYS = 30
MAX_EXPIRY_DAYS = 90
OPERATION_CLASS_BY_OPERATION = {
    "create-comment": "create-comment",
    "edit-comment": "edit-comment",
    "close-issue": "close-issue",
    "assign-copilot": "delegate-copilot",
}
```

Define frozen dataclasses `OperationClassPolicy` and `OperationPolicyRevision`. The loader must:

- reject duplicate JSON keys, unknown/missing fields, booleans used as integers, negative caps, unknown classes, duplicate denied IDs/targets, malformed repository/actor/revision identity, and naive timestamps;
- require all five operation classes even when disabled;
- require `revision >= 1`, `status in {"active", "paused", "revoked"}`, and `replacesRevisionId is None` only for revision 1;
- require `createdAtUtc < expiresAtUtc <= createdAtUtc + 90 days`;
- require sums of class caps to remain at or below 100/run and 300/24h;
- expose `active_at(now)` that is true only when status is active and `createdAtUtc <= now < expiresAtUtc`; a policy never authorizes before its activation instant, keeping the 90-day wall-clock ceiling meaningful;
- compute `digest` as SHA-256 over `stable_json(document).encode("utf-8")`, because this identity participates in the authorization boundary.

Use `parse_aware_iso8601` and `format_utc_z` from `ci_shepherd.timeutils`.

- [ ] **Step 4: Run policy tests**

Run the Step 2 command.

Expected: all `OperationPolicyTests` pass.

- [ ] **Step 5: Show and create the checkpoint commit**

Proposed commit:

```text
feat(ci): define autonomous shepherd policy
```

Stage only:

```bash
git add -- \
  .ci-shepherd-build/scripts/ci_shepherd/operation_policy.py \
  .ci-shepherd-build/tests/test_operation_policy.py
git commit -m "feat(ci): define autonomous shepherd policy"
```

### Task 2: Persist append-only policy revisions and exact decisions

**Files:**

- Create: `.ci-shepherd-build/scripts/ci_shepherd/coordinator_state.py`
- Create: `.ci-shepherd-build/tests/test_coordinator_state.py`

- [ ] **Step 1: Write failing state-transition tests**

Use a temporary state root and assert these transitions:

```python
first = store.append_policy_revision(
    repository="microsoft/aspire",
    expected_revision=0,
    document=policy_document(revision=1, replaces=None),
)
self.assertEqual(1, first["stateRevision"])

with self.assertRaisesRegex(CoordinatorStateError, "stale-view"):
    store.append_policy_revision(
        repository="microsoft/aspire",
        expected_revision=0,
        document=policy_document(revision=2, replaces="policy:1"),
    )

decision = store.append_exact_decision(
    repository="microsoft/aspire",
    expected_revision=1,
    proposals_path=proposals_path,
    action_id="snapshot:microsoft/aspire:time:issue:7:retire-status-comment",
    decision="reject-once",
    actor="github:radical",
    now=datetime(2026, 9, 3, 16, 5, tzinfo=UTC),
)
self.assertEqual(2, decision["stateRevision"])
self.assertEqual("reject-once", store.projection("microsoft/aspire")["exactDecisions"][0]["decision"])
```

Also test:

- concurrent compare-and-append calls at the same expected revision produce one success and one `stale-view`;
- revision 2 must replace revision 1 exactly;
- pause and revoke append new revisions and preserve prior bytes;
- `clear` appends a decision event rather than deleting history;
- a decision is bound to action ID, proposal digest, actor, and expiry;
- clearing after an action intent exists is rejected by the injected
  lock-free durable-intent reader;
- malformed/truncated JSONL fails closed;
- state directories and ledger files may not be symlinks;
- `projection()` returns a monotonic state revision and the latest effective policy/decision values.

- [ ] **Step 2: Run the test and verify the missing module failure**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest tests.test_coordinator_state -v
```

Expected: missing `ci_shepherd.coordinator_state`.

- [ ] **Step 3: Implement the locked append-only store**

Create `CoordinatorStateStore` with:

```python
class CoordinatorStateStore:
    def __init__(
        self,
        state_dir: Path,
        *,
        durable_intent_reader: Callable[[str], bool],
        lock_timeout_seconds: float = 5.0,
    ) -> None

projection(repository: str, *, now: datetime | None = None) -> dict[str, object]

append_policy_revision(
    *,
    repository: str,
    expected_revision: int,
    document: Mapping[str, object],
) -> dict[str, object]

append_exact_decision(
    *,
    repository: str,
    expected_revision: int,
    proposals_path: Path,
    action_id: str,
    decision: str,
    actor: str,
    now: datetime,
) -> dict[str, object]
```

Persist owner-only files under:

```text
<state>/coordinator/policy-events.jsonl
<state>/coordinator/policy-events.lock
```

Each event includes `schemaVersion`, `stateRevision`, `eventType`, `repository`, `recordedAtUtc`, and an event-specific payload. Hold the lock from reading the current revision through `os.write` and `os.fsync`. Reuse the platform-specific lock behavior from `execution_state.py`, but extract the duplicated lock helpers into `.ci-shepherd-build/scripts/ci_shepherd/file_lock.py` only if both modules need identical logic; do not change lock semantics.

`append_exact_decision` reads and validates the proposal document once, derives
its raw-byte digest and expiry, resolves exactly one matching action, and stores
those derived values. Neither the Canvas nor another caller may provide a
digest or decision expiry.

Projection rules:

- latest policy event is authoritative;
- latest non-expired decision for each `(proposalDigest, actionId)` is authoritative;
- `clear` removes the effective decision only in the projection;
- expired decisions remain in history but not the effective list;
- events for another repository never affect the requested projection.

`durable_intent_reader` is called while `policy-events.lock` is held and must
read the append-only action-event file without acquiring `action-events.lock`.
It fails closed on an incomplete or malformed tail. Autonomous reservation
holds `action-events.lock`, then `policy-events.lock`, and fsyncs intent before
releasing either; therefore a concurrent clear sees either no intent before
reservation or the complete intent after reservation, without lock inversion.
Add a concurrency test that races clear against reservation and asserts neither
side times out.

- [ ] **Step 4: Run state tests**

Run the Step 2 command.

Expected: all coordinator-state tests pass, including the two-writer race.

- [ ] **Step 5: Show and create the checkpoint commit**

Proposed commit:

```text
feat(ci): persist shepherd policy decisions
```

Stage the state implementation and its tests only, including `file_lock.py` if extraction was necessary.

### Task 3: Replace rank-before-permission with policy-aware selection

**Files:**

- Create: `.ci-shepherd-build/scripts/ci_shepherd/policy_selection.py`
- Create: `.ci-shepherd-build/tests/test_policy_selection.py`
- Create: `.ci-shepherd-build/tests/fixtures/production-policy-selection-20260903/action-proposals.json`
- Create: `.ci-shepherd-build/tests/fixtures/production-policy-selection-20260903/original-comment-selection.json`

- [ ] **Step 1: Freeze and verify the failed production fixture**

Copy the immutable artifacts:

```bash
mkdir -p .ci-shepherd-build/tests/fixtures/production-policy-selection-20260903
cp /Users/ankj/.copilot/ci-shepherd/runs/manual-20260903T155517Z/action-proposals.json \
  .ci-shepherd-build/tests/fixtures/production-policy-selection-20260903/action-proposals.json
cp /Users/ankj/.copilot/ci-shepherd/runs/manual-20260903T155517Z/comment-selection.json \
  .ci-shepherd-build/tests/fixtures/production-policy-selection-20260903/original-comment-selection.json
shasum -a 256 \
  .ci-shepherd-build/tests/fixtures/production-policy-selection-20260903/action-proposals.json \
  .ci-shepherd-build/tests/fixtures/production-policy-selection-20260903/original-comment-selection.json
```

Expected:

```text
fed8a27dea6b51c7b531ac2856b18796a8572e0c70adc0e012f66b64ab6280a7  .ci-shepherd-build/tests/fixtures/production-policy-selection-20260903/action-proposals.json
d1ab449ef58332b4ff0a318228107f3fd3dca07b64ea2b1412be775275b7df55  .ci-shepherd-build/tests/fixtures/production-policy-selection-20260903/original-comment-selection.json
```

- [ ] **Step 2: Write the failing regression and cap-scanning tests**

The regression test must load the fixture and activate only `edit-comment` with cap 10. Assert:

```python
self.assertEqual(
    [
        "snapshot:microsoft/aspire:2026-09-03T15:55:19.413043Z:issue:19166:retire-status-comment",
        "snapshot:microsoft/aspire:2026-09-03T15:55:19.413043Z:issue:19453:retire-status-comment",
        "snapshot:microsoft/aspire:2026-09-03T15:55:19.413043Z:issue:19530:retire-status-comment",
    ],
    selection["automaticActionIds"],
)
self.assertEqual([], selection["exactActionIds"])
self.assertEqual(
    [18203, 18299],
    [
        row["issueNumber"]
        for row in selection["candidates"]
        if row["status"] == "ineligible"
    ],
)
self.assertEqual(
    [19835, 19839, 19875, 19876],
    [
        row["issueNumber"]
        for row in selection["candidates"]
        if row["status"] == "denied"
        and row["reason"] == "operation-disabled"
    ],
)
```

Add focused tests proving:

- deterministic order is unchanged inside the permitted set;
- once `edit-comment` reaches its class cap, scanning continues and admits the next allowed `create-comment`;
- rolling usage is calculated only from terminal results within `(now - 24h, now]`;
- per-run usage matches the explicit `runId`, not snapshot text parsing;
- `reject-once` blocks a broadly allowed action;
- `approve-once` admits one disabled/exhausted-class action;
- exact approval fails when its proposal digest or action ID differs;
- exact approval cannot exceed repository hard ceilings;
- policy-level `deniedActionIds` and `deniedTargets` block otherwise permitted
  actions and are attributed to the licensing policy revision;
- policy deny rules remain absolute when an `approve-once` decision also exists;
- a dependent close is `blocked` with `prerequisite-not-terminal` until its exact
  comment dependency has a durable terminal `executed` result;
- after that dependency is terminal, selection admits the close and binds its
  exact terminal event as a satisfied prerequisite;
- same-issue lower-priority comments remain suppressed;
- unsupported operations receive `outside-policy-surface`;
- every candidate has one typed status: `automatic`, `exact`, `denied`, `exhausted`, `ineligible`, `superseded`, or `outside-policy-surface`;
- maximum exposure equals the sum of remaining class allowances, bounded by both hard ceilings.

- [ ] **Step 3: Run the tests and verify the missing selector failure**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest tests.test_policy_selection -v
```

Expected: missing `ci_shepherd.policy_selection`.

- [ ] **Step 4: Implement policy-aware selection**

Expose:

```python
def build_policy_selection(
    proposals_document: object,
    *,
    run_id: str,
    policy_projection: Mapping[str, object],
    action_events: Sequence[Mapping[str, object]],
    now: datetime,
) -> dict[str, object]


render_policy_selection_section(selection: Mapping[str, object]) -> str
```

The returned schema is:

```json
{
  "schemaVersion": 1,
  "repository": "microsoft/aspire",
  "snapshotId": "snapshot:microsoft/aspire:2026-09-03T15:55:19.413043Z",
  "runId": "2026-09-03T15-55-19.413043Z-r0",
  "proposalsDigest": "sha256:fed8a27dea6b51c7b531ac2856b18796a8572e0c70adc0e012f66b64ab6280a7",
  "coordinatorStateRevision": 4,
  "policyRevisionId": "policy:4",
  "policyDigest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "generatedAtUtc": "2026-09-03T16:10:00Z",
  "automaticActionIds": [],
  "exactActionIds": [],
  "selectedActionIds": [],
  "candidates": [],
  "budgets": {},
  "maximumWriteExposure": {
    "thisRun": 0,
    "rolling24h": 0
  }
}
```

Use the existing semantic suffix priorities from `comment_selection.py`; move
them to exported helpers there rather than duplicating them. Compute candidate
eligibility first, apply policy denies and exact rejection, classify operation,
test policy activity, calculate remaining budgets, then scan deterministic
order once. Append exact approvals afterward in the same deterministic key
order. Never take a global prefix before policy filtering. Keep `candidates` in
proposal order for audit stability; use explicit rank fields for automatic and
exact execution order.

For `dependsOn`, do not silently include another action in the grant. Resolve
the exact dependency from the proposal graph and require a durable terminal
`executed` result whose repository, snapshot, action ID, operation, target,
idempotency key, and body digest match the frozen dependency. Record that
terminal event digest in the selected close candidate.

- [ ] **Step 5: Run selector and existing comment-selection tests**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest \
  tests.test_policy_selection \
  tests.test_comment_selection -v
```

Expected: all tests pass; existing production-pilot selection remains unchanged.

- [ ] **Step 6: Show and create the checkpoint commit**

Proposed commit:

```text
feat(ci): select actions within policy
```

Stage only the selector, tests, fixture, and any focused `comment_selection.py` helper extraction.

### Task 4: Bind one-action child grants to policy selection

**Files:**

- Modify: `.ci-shepherd-build/scripts/ci_shepherd/authorization.py`
- Modify: `.ci-shepherd-build/scripts/create_authorization.py`
- Modify: `.ci-shepherd-build/tests/test_authorization.py`

- [ ] **Step 1: Write failing child-grant tests**

Add tests that call `generate_authorization_grant` with
`policy_selection_path=selection_path`, `policy_action_id=action_id`, and
`allow_autonomous_policy=True`. Assert:

- exactly one action ID is required;
- the action must occur in `selectedActionIds`;
- the grant includes `autonomousPolicyLicense` with selection digest, run ID, coordinator revision, operation class, and licensing source (`policy:<revisionId>` or `decision:<eventRevision>`);
- TTL is `min(15 minutes, proposal expiry, policy expiry, production snapshot freshness)`;
- changed selection bytes, proposal bytes, body, target, operation, licensing
  policy/decision revision, or action ID fail before execution;
- an unrelated coordinator event does not invalidate a child grant;
- replacement/pause/revocation of the licensing policy invalidates its grant;
- clearing/rejecting the exact approval that licensed a grant invalidates it;
- a dependent close grant contains one allowed action plus the exact digest of
  its already-terminal prerequisite and is rejected when that terminal changes;
- old production comment/delegation pilot grants still load unchanged;
- autonomous capability is mutually exclusive with every production pilot flag.

Use this expected license shape:

```json
{
  "schemaVersion": 1,
  "runId": "2026-09-03T15-55-19.413043Z-r0",
  "operationClass": "edit-comment",
  "selectionDigest": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "selectionStateRevision": 4,
  "licenseSource": "policy:4",
  "satisfiedPrerequisites": []
}
```

- [ ] **Step 2: Run targeted authorization tests and verify failure**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest tests.test_authorization -v
```

Expected: new keyword arguments or grant fields are unsupported.

- [ ] **Step 3: Extend the strict grant schema**

Add optional `autonomousPolicyLicense` and `policySelectionDigest` fields without weakening existing grant validation. Add:

```python
@dataclass(frozen=True, slots=True)
class AutonomousPolicyLicense:
    run_id: str
    operation_class: str
    selection_digest: str
    selection_state_revision: int
    license_source: str
    satisfied_prerequisites: tuple[tuple[str, str], ...]
```

The generator and loader must bind the exact raw selection bytes.
`_validate_policy_selection` validates identity/digest fields, verifies the one
action is selected, and cross-checks the proposal's operation through
`classify_operation`. A dependent action is permitted only when the selection
contains its exact satisfied-prerequisite digest. Do not let the grant carry
class caps; authoritative consumption is derived from durable state in Task 5.

Add `allow_autonomous_policy` to the production-capability mutual-exclusion sum,
`production_pilot_enabled`, and both generation/load dispatch chains. The
autonomous production branch must still call `_production_freshness_deadline`;
it must not fall through to the delegation steady-state validator. For
`delegate-copilot`, also run the existing production delegation capacity-policy
validation and bind `capacityPolicyDigest`, so class caps supplement rather than
replace live task/PR/repository capacity checks.

Do not require the global coordinator state revision to remain unchanged.
Execution instead revalidates the semantic license source: the named policy
revision is still effective, or the named exact approval is still effective,
and no absolute policy denial or exact rejection now blocks this action.

Extend `create_authorization.py` with:

```text
--policy-selection PATH
--policy-action-id ACTION_ID
--autonomous-policy
```

When `--autonomous-policy` is used, reject more than one `--action-id`, require the selection path, and require `--policy-action-id` to equal that action.

- [ ] **Step 4: Run authorization tests**

Run the Step 2 command.

Expected: all old and new authorization tests pass.

- [ ] **Step 5: Show and create the checkpoint commit**

Proposed commit:

```text
feat(ci): bind policy child grants
```

Stage only authorization files and tests.

### Task 5: Make budget consumption atomic with intent

**Files:**

- Modify: `.ci-shepherd-build/scripts/ci_shepherd/execution_state.py`
- Modify: `.ci-shepherd-build/tests/test_execution_state.py`
- Modify: `.ci-shepherd-build/tests/test_coordinator_state.py`

- [ ] **Step 1: Write failing atomic-consumption tests**

Construct grants with an `AutonomousPolicyLicense` and a `PolicyBudgetValidator` backed by the coordinator store. Cover:

- two threads racing for the last per-run slot append exactly one intent;
- restart reconstructs usage from durable intents/terminal events;
- an intent consumes capacity immediately, so crash/reconciliation cannot open another slot;
- terminal `failed` and `stale` results still count as mutation attempts because the write boundary may have been crossed;
- a pre-intent authorization failure consumes nothing;
- exact approval bypasses class caps but increments class counters;
- neither policy nor exact approval bypasses hard ceilings;
- revoked, expired, replaced, or semantically stale licensing policy cannot
  reserve a new intent;
- an existing intent returns `reconcile` after policy revocation.
- every repository intent, including legacy pilot intents, consumes the
  repository-wide run and rolling hard ceilings;
- only autonomous licensed intents consume per-class standing-policy caps.

- [ ] **Step 2: Run the targeted tests and verify over-admission**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest \
  tests.test_execution_state \
  tests.test_coordinator_state -v
```

Expected: the new final-slot race permits both reservations or lacks a validator.

- [ ] **Step 3: Add locked policy validation to reservation**

Add an optional policy validator to `ActionEventStore`:

```python
class PolicyBudgetValidator(Protocol):
    @contextmanager
    def reservation_guard(
        self,
        *,
        grant: AuthorizationGrant,
        action_events: Sequence[Mapping[str, Any]],
        intent: Mapping[str, Any],
        at: datetime,
    ) -> Iterator[None]
```

Call it inside `_reserve_locked` after idempotent prior-intent/terminal checks,
and keep the returned context open through `_append_event(intent)`. Add `runId`,
`operationClass`, `policySelectionDigest`, `coordinatorStateRevision`, and
`licenseSource` and satisfied-prerequisite digests to autonomous intent events.
The guard acquires the coordinator
policy lock, re-reads the latest policy/decision state, validates capacity, and
does not release that lock until the action intent has been fsynced. Both stores
are therefore serialized in one documented lock order:

```text
action-events.lock -> policy-events.lock
```

Every caller must use that order; no policy command may acquire the action lock
while holding the policy lock. Revocation waits for an in-flight reservation to
append its intent, so the action is unambiguously licensed-before-intent or
revoked-before-license. This prevents deadlock and closes the
validate-then-revoke race.

Count an autonomous action once by unique action ID when an intent exists.
Rolling time uses intent `recordedAt`; replay/reconciliation does not increment
it again. Count every repository intent for the 100/run and 300/24h hard
ceilings, including legacy production-pilot intents. Before a dependent close
intent, re-read and revalidate the exact prerequisite terminal digest while
both locks are held.

- [ ] **Step 4: Run atomic-consumption tests**

Run the Step 2 command.

Expected: all tests pass repeatedly:

```bash
for i in 1 2 3 4 5; do
  PYTHONPATH=scripts python3 -m unittest \
    tests.test_execution_state \
    tests.test_coordinator_state >/dev/null || exit 1
done
```

- [ ] **Step 5: Show and create the checkpoint commit**

Proposed commit:

```text
fix(ci): enforce policy budgets at intent
```

### Task 6: Expose one deterministic coordinator CLI

**Files:**

- Create: `.ci-shepherd-build/scripts/coordinator.py`
- Create: `.ci-shepherd-build/tests/test_coordinator.py`

- [ ] **Step 1: Write failing CLI tests**

Call `coordinator.main(argv)` directly and capture stdout. Cover commands:

```text
projection --repository --state-dir [--proposals] [--run-id] [--now]
policy-append --repository --state-dir --expected-revision --document
policy-activate --repository --state-dir --expected-revision --caps --expires-in-days --actor --now
policy-pause --repository --state-dir --expected-revision --actor --now
policy-revoke --repository --state-dir --expected-revision --actor --now
policy-preview --repository --state-dir --expected-revision --caps --expires-in-days --proposals --run-id --now
decision-set --repository --state-dir --expected-revision --proposals --action-id --decision --actor --now
decision-clear --repository --state-dir --expected-revision --proposals --action-id --actor --now
select --repository --state-dir --proposals --run-id --output --now
grant-next --repository --state-dir --proposals --selection --output --now
```

Assertions:

- every mutating command requires `--expected-revision`;
- stale revision returns exit code 2 and JSON containing
  `code: "stale-view"` plus the complete refreshed `projection`;
- `decision-set` derives the proposal digest and expiry from the proposal document rather than accepting caller claims;
- `policy-activate`, pause, and revoke derive revision ID, revision number,
  predecessor, timestamps, and status in Python; browser input can supply only
  caps and requested expiry duration;
- `policy-preview` validates the draft through the same policy loader and
  returns coordinator-computed reachable actions and maximum exposure without
  appending an event;
- `select` writes owner-only JSON atomically;
- `grant-next` picks the first exact action, otherwise first automatic action, and emits no grant when nothing is permitted;
- `projection` works when no policy exists and reports `stage: "awaiting-policy"`;
- no command imports a GitHub client or accepts a GitHub token.

- [ ] **Step 2: Run CLI tests and verify the missing script failure**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest tests.test_coordinator -v
```

Expected: import failure for `coordinator`.

- [ ] **Step 3: Implement the CLI as a thin adapter**

Keep parsing and JSON output in `coordinator.py`; keep all decisions in the modules from Tasks 1–5. Define `main(argv: Sequence[str] | None = None) -> int`. Use `stable_json` for stdout and owner-only atomic writes. Error responses must be typed JSON on stderr with nonzero exit status; never translate invalid state into a successful empty projection.

`grant-next` uses `generate_authorization_grant` with exactly one action, a maximum 15-minute TTL, and `allow_autonomous_policy=True`.

`policy-append --document` is retained for headless administration and tests.
The Canvas uses only `policy-activate`, `policy-pause`, `policy-revoke`, and
`policy-preview`; its trusted adapter supplies actor identity from the
foreground authenticated session, never from a browser field.

- [ ] **Step 4: Run CLI tests**

Run the Step 2 command.

Expected: all coordinator tests pass.

- [ ] **Step 5: Show and create the checkpoint commit**

Proposed commit:

```text
feat(ci): add headless policy coordinator
```

### Task 7: Add the durable three-slot investigation scheduler

**Files:**

- Create: `.ci-shepherd-build/scripts/ci_shepherd/investigation_scheduler.py`
- Create: `.ci-shepherd-build/tests/test_investigation_scheduler.py`
- Modify: `.ci-shepherd-build/scripts/ci_shepherd/investigations.py`
- Modify: `.ci-shepherd-build/tests/test_investigations.py`
- Modify: `.ci-shepherd-build/scripts/coordinator.py`
- Modify: `.ci-shepherd-build/tests/test_coordinator.py`

- [ ] **Step 1: Write barrier-controlled scheduler tests**

Use `threading.Barrier`, `threading.Event`, and a recording launcher. Create five requests and prove:

```python
scheduler.run_until_blocked(plan, launch)
self.assertEqual(3, launch.active_count)
self.assertEqual(3, launch.maximum_active_count)
self.assertEqual([1, 2, 3], launch.started_issue_numbers)

launch.complete(issue_number=1)
scheduler.run_until_blocked(plan, launch)
self.assertEqual([1, 2, 3, 4], launch.started_issue_numbers)

launch.complete(issue_number=2)
launch.complete(issue_number=3)
scheduler.run_until_blocked(plan, launch)
self.assertEqual([1, 2, 3, 4, 5], launch.started_issue_numbers)
self.assertEqual(3, launch.maximum_active_count)
```

Add tests for:

- one completion opens exactly one slot;
- two completions open exactly two slots;
- failed worker records terminal failure before a slot is reusable;
- restart reconstructs active requests from investigation session events;
- a request is never admitted twice;
- repeated admission before session creation returns no duplicate because the
  durable `admitted` event already occupies the slot;
- an admission that cannot launch is terminally released before another request
  is admitted;
- an admission older than five minutes is durably abandoned and becomes
  eligible for a bounded retry;
- the existing five-request cycle budget remains independent;
- an existing active session does not count as a newly launched session but does occupy a slot;
- malformed/truncated lifecycle state fails closed.

- [ ] **Step 2: Run scheduler tests and verify the missing module failure**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest tests.test_investigation_scheduler -v
```

Expected: missing `ci_shepherd.investigation_scheduler`.

- [ ] **Step 3: Implement admission, not session creation**

Expose:

```python
MAX_CONCURRENT_INVESTIGATIONS = 3


def build_investigation_admission(
    plan: Mapping[str, object],
    session_events: Sequence[Mapping[str, object]],
    *,
    max_concurrent: int = MAX_CONCURRENT_INVESTIGATIONS,
) -> dict[str, object]


def admit_investigation_requests(
    state_dir: Path,
    plan: Mapping[str, object],
    *,
    admitted_at: datetime,
    max_concurrent: int = MAX_CONCURRENT_INVESTIGATIONS,
) -> dict[str, object]
```

Return:

```json
{
  "schemaVersion": 1,
  "maxConcurrent": 3,
  "activeInvestigationIds": [],
  "admittedRequests": [],
  "queuedInvestigationIds": [],
  "availableSlots": 3
}
```

`admit_investigation_requests` acquires the existing investigation-session JSONL
lock and appends one `admitted` event per granted slot before returning.
`admitted` and `started` events without a later terminal event occupy slots.
Preserve request ordering from `investigation-plan.json`. An admission carries
an opaque `admissionId`; `started` must name that ID and the returned session ID.
A session-launch failure records `failed` against the admission. An admission
older than five minutes is first appended as `abandoned` before it can be
retried, and existing two-attempt limits still apply.

The scheduler must not call `create_session`; the autonomous skill consumes
only the durably `admittedRequests`, launches each with the session tool, and
immediately records the returned session ID through the extended
`investigation_session.py --status started --admission-id ADMISSION_ID`
boundary.

Add coordinator command:

```text
investigations-admit --state-dir --plan --output
```

- [ ] **Step 4: Run scheduler and lifecycle tests**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest \
  tests.test_investigation_scheduler \
  tests.test_investigations \
  tests.test_coordinator -v
```

Expected: all tests pass.

- [ ] **Step 5: Show and create the checkpoint commit**

Proposed commit:

```text
feat(ci): schedule three investigations at a time
```

### Task 8: Integrate policy projection into cycle artifacts and reporting

**Files:**

- Modify: `.ci-shepherd-build/scripts/cycle.py`
- Modify: `.ci-shepherd-build/scripts/ci_shepherd/retrospective.py`
- Modify: `.ci-shepherd-build/tests/test_cycle.py`

- [ ] **Step 1: Write failing cycle integration tests**

Add a fixture cycle with an active edit-only policy. After `finish_cycle`, assert:

- `policy-selection.json` exists and chooses edits after policy filtering;
- `coordinator-projection.json` exists with the same `coordinatorStateRevision`;
- `report.md` contains policy status, maximum exposure, selected automatic/exact counts, exhausted/blocked reasons, and investigation active/queued/completed counts;
- no policy produces `awaiting-policy` and zero autonomous grants, not `completed`;
- paused, revoked, expired, and stale policy states are distinct;
- old `comment-selection.json` remains for the legacy production-comment pilot during migration;
- retrospective evidence includes `policy-selection.json`, `coordinator-projection.json`, and the exact child grant/event rows for the run;
- installing no Canvas files changes no selected IDs.

- [ ] **Step 2: Run cycle tests and verify missing artifacts**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest tests.test_cycle -v
```

Expected: `policy-selection.json` or projection assertion fails.

- [ ] **Step 3: Write policy artifacts after proposals freeze**

In `finish_cycle`, after final `action-proposals.json` is written:

1. load the coordinator projection;
2. call `build_policy_selection`;
3. write `policy-selection.json`;
4. write `coordinator-projection.json`;
5. append `render_policy_selection_section` to `report.md`;
6. include both files in immutable run recording;
7. set a new `coordinatorStage` field using `collecting`, `investigating`,
   `awaiting-policy`, `ready`, `executing`, `reconciling`, `completed`, or
   `blocked`; leave the existing `stage` state machine unchanged.

Add a regression proving `stage: "completed"` can coexist with
`coordinatorStage: "awaiting-policy"` and the retrospective still finalizes.

Do not mint or execute grants inside `finish_cycle`; this function freezes and records the cycle. The autonomous loop in `SKILL.md` calls `coordinator.py grant-next`, then `execute_actions.py --execute`, then regenerates projection/selection until no action remains.

- [ ] **Step 4: Run cycle tests**

Run the Step 2 command.

Expected: all cycle tests pass.

- [ ] **Step 5: Show and create the checkpoint commit**

Proposed commit:

```text
feat(ci): report autonomous policy decisions
```

### Task 9: Prove the GitHub boundary with a recording fake

**Files:**

- Modify: `.ci-shepherd-build/tests/test_actor.py`
- Modify: `.ci-shepherd-build/tests/test_execution_state.py`
- Modify: `.ci-shepherd-build/tests/test_scripts.py`
- Create: `.ci-shepherd-build/tests/test_autonomous_policy_integration.py`

- [ ] **Step 1: Build one recording fake**

Reuse the existing actor protocol and implement a test fake that records method, repository, target, body, and whether an intent was already durable when called. Do not add a second production executor.

- [ ] **Step 2: Write end-to-end boundary tests**

For frozen proposals, policy selection, child grant, and executor, assert:

- zero writes without a child grant;
- zero writes for disabled, exhausted, rejected, expired, stale, or replayed actions;
- every write exactly matches the frozen target/body/operation;
- current CI-label, ownership, source-comment, and issue-version preflights still run;
- crash after intent reconciles rather than repeats;
- a terminal result changes per-class/run/rolling counters exactly once;
- a copied grant cannot reset budget or terminal identity;
- `microsoft/aspire` remains denied unless the explicit autonomous production capability is supplied.

- [ ] **Step 3: Run the integration test and verify the first missing capability**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest tests.test_autonomous_policy_integration -v
```

Expected: autonomous production capability is rejected before the fake receives a write.

- [ ] **Step 4: Wire the executor capability without bypassing preflights**

Add `--autonomous-policy` and `--policy-selection` to `execute_actions.py`. Route them only to `load_authorized_execution`; do not branch around actor validation, live preflight, intent persistence, mutation, or reconciliation.

- [ ] **Step 5: Run integration and existing actor tests**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest \
  tests.test_autonomous_policy_integration \
  tests.test_actor \
  tests.test_scripts \
  tests.test_execution_state -v
```

Expected: all tests pass.

- [ ] **Step 6: Show and create the checkpoint commit**

Proposed commit:

```text
feat(ci): execute exact policy actions
```

### Task 10: Document the autonomous operator loop

**Files:**

- Modify: `.ci-shepherd-build/SKILL.md`
- Modify: `.ci-shepherd-build/tests/test_scripts.py`

- [ ] **Step 1: Write failing contract tests**

Assert the skill contract contains:

- policy-aware selection before grant;
- one action per child grant;
- no Canvas dependency;
- exact rejection precedence;
- intent reconciliation before new mutation;
- `investigations-admit` and maximum three active sessions;
- repeated admission after completion until the queue is empty;
- no direct substitution outside the selector;
- shadow mode before production promotion;
- a fresh read-only audit before enabling each production class.

Also assert it no longer instructs an autonomous policy run to grant the complete ordered `comment-selection.json` prefix.

- [ ] **Step 2: Run the contract test and verify failure**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest tests.test_scripts -v
```

Expected: new autonomous commands/requirements are absent.

- [ ] **Step 3: Add exact command sequences**

Document this loop:

```text
cycle start/finish
-> coordinator investigations-admit
-> launch only admitted fresh sessions
-> record each start immediately
-> record each terminal result
-> repeat admission until queue empty
-> regenerate proposals after investigation results
-> coordinator select
-> reconcile any prior intent
-> coordinator grant-next
-> execute exactly one child grant
-> regenerate selection/projection
-> repeat until no permitted action remains
-> seal and retrospective
```

State that the autonomous worker must never wait for or inspect a Canvas connection. Add command examples with exact paths and flags from Tasks 6–9.

- [ ] **Step 4: Run the contract test**

Run the Step 2 command.

Expected: all tests pass.

- [ ] **Step 5: Show and create the checkpoint commit**

Proposed commit:

```text
docs(ci): define autonomous shepherd loop
```

### Task 11: Full headless acceptance and independent review gate

**Files:**

- Modify only if failures reveal defects in files already owned by Tasks 1–10.
- Create no report in the repository; preserve run/audit output under the session state directory.

- [ ] **Step 1: Run the complete Python suite**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Expected: all tests pass.

- [ ] **Step 2: Compile all Python modules**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m compileall -q scripts tests
```

Expected: exit code 0 and no output.

- [ ] **Step 3: Run the frozen production fixture headlessly**

Use a temporary state directory, append an edit-only policy, run `coordinator.py select`, and assert with a small `python3 -c` command that the selected IDs are exactly issues 19166, 19453, and 19530. Do not pass `--execute`; this is action-free.

Expected: three edits, zero creates, zero GitHub calls.

- [ ] **Step 4: Exercise cap persistence across process restart**

Against `radical/aspire` with the recording fake, execute one child grant, start a fresh Python process, regenerate selection, and verify the per-run and rolling remaining counts each decreased by one and the same action is terminal.

Expected: no duplicate write and persisted counters.

- [ ] **Step 5: Exercise the five-request scheduler**

Run the barrier-controlled integration harness with five requests.

Expected:

```text
maximum active: 3
started: 5
duplicate starts: 0
terminal before slot reuse: true
```

- [ ] **Step 6: Compare headless results while the Canvas performs reads**

Run selection once with no Canvas process and once while the Canvas adapter
repeatedly requests projection/preview without issuing a mutation command.

Expected: byte-identical `policy-selection.json` after normalizing only the generated timestamp pinned by `--now`.

- [ ] **Step 7: Commission a fresh read-only safety review**

Give a fresh reviewer the spec, this plan, final diff, full test output, frozen fixture result, and recording-fake ledger. Require it to challenge:

- policy-to-child-grant binding;
- lock order and final-slot races;
- revocation after intent;
- exact-approval hard-ceiling behavior;
- restart/replay handling;
- unsupported operation isolation;
- three-slot scheduler reconstruction.

Do not enable production mutation when the reviewer reports an unresolved high-confidence safety finding.

- [ ] **Step 8: Show and create the final implementation commit if repairs were needed**

Use a subject describing the repaired behavior, show the complete proposed message before committing, and stage only the repaired files.

## Plan self-review

**Spec coverage:** Tasks 1–6 cover durable policy, exact decisions, revisions,
expiry, caps, stale views, draft preview, selection, and child grants. Tasks 4,
5, and 9 cover dependent close prerequisites, delegation capacity, atomic
budget consumption, replay, intent reconciliation, and the GitHub boundary.
Task 7 covers durable admission and rolling maximum-three investigation
concurrency independent of total request budget. Tasks 8 and 10 cover
coordinator states, reporting, retrospective evidence, and headless operation.
Task 11 covers the frozen 2026-09-03 regression, restart, active-Canvas
equivalence, and independent audit.

**Deliberate deferrals:** The policy class `rerun-or-retry` is represented but maps no executable proposal because the current actor exposes no rerun/retry mutation. Adding a new GitHub mutation surface without a separately specified target identity and preflight contract would be unsafe scope expansion. The Canvas plan must display this class as having zero reachable current actions.

**Type consistency:** `operationClass`, `runId`, `coordinatorStateRevision`, `policySelectionDigest`, and `licenseSource` use the same names in selection, grant, intent, projection, and report artifacts. All exact decisions bind `(proposalDigest, actionId)`. All policy commands use `expectedRevision`; stale writes return `stale-view`.

**Placeholder scan:** The plan contains no deferred implementation marker or
abbreviated implementation body.
