# CI Shepherd Typed Assessment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current single-action CI issue report with a read-only, versioned assessment that classifies failure occurrences, derives issue lifecycle, and emits typed, evidence-backed proposals for manual review.

**Architecture:** Deterministic Python code loads explicit policy, normalizes evidence into occurrences and coverage observations, derives conservative lifecycle and allowed-intent candidates, and validates all semantic judgments made by a fresh assessment agent. A finalizer compiles those validated judgments into stable typed proposals; history, rendering, and later executors consume that envelope without treating agent prose as authority.

**Tech Stack:** Python 3 standard library, `unittest`, GitHub CLI GET-only REST access, JSON, Markdown.

---

## Scope and file map

This plan implements the **manual, read-only assessment producer**. It does not
implement GitHub comments, labels, closure, reruns, Copilot assignment, policy
PRs, or quarantine PRs. Those executor capabilities require a separate plan
after shadow-mode precision is measured.

The prototype remains under `.ci-shepherd-build/`, which is intentionally
excluded by the checkout's `.git/info/exclude`. Do not force-add it to the
Aspire repository. The committed artifacts for this phase are this plan and
the already committed design. Each prototype task therefore ends with a
focused test checkpoint rather than a Git commit.

- `.ci-shepherd-build/policies/manual-v1.json`: explicit experimental review
  thresholds and retry-safe pattern allowlist.
- `.ci-shepherd-build/scripts/ci_shepherd/policy.py`: strict policy loading and
  validation.
- `.ci-shepherd-build/scripts/ci_shepherd/observations.py`: occurrence,
  coverage, attempt-lineage, and normalized-fingerprint construction.
- `.ci-shepherd-build/scripts/ci_shepherd/lifecycle.py`: issue lifecycle and
  deterministic allowed-intent derivation.
- `.ci-shepherd-build/scripts/ci_shepherd/assessment.py`: judgment validation
  and compilation of the final typed assessment envelope.
- `.ci-shepherd-build/scripts/ci_shepherd/models.py`: shared schema
  vocabularies and structural validators.
- `.ci-shepherd-build/scripts/ci_shepherd/collector.py`: only the additional
  workflow/job dimensions needed by observations.
- `.ci-shepherd-build/scripts/prepare.py`: build bounded semantic input.
- `.ci-shepherd-build/scripts/finalize.py`: compile agent judgments into
  `assessment.json`.
- `.ci-shepherd-build/scripts/validate.py`: validate the prepared input,
  judgments, and final assessment together.
- `.ci-shepherd-build/scripts/render.py`: render deterministic manual queues
  from typed proposals.
- `.ci-shepherd-build/scripts/record.py`: persist snapshot, preparation,
  judgments, final assessment, and derived history separately.
- `.ci-shepherd-build/scripts/ci_shepherd/history.py`: retain occurrence and
  proposal history across issue closure while preserving v1 evidence reuse.
- `.ci-shepherd-build/SKILL.md`: coordinator and fresh-agent contracts.
- `.ci-shepherd-build/tests/test_policy.py`: policy validation.
- `.ci-shepherd-build/tests/test_observations.py`: occurrence, attempt,
  coverage, and fingerprint behavior.
- `.ci-shepherd-build/tests/test_lifecycle.py`: lifecycle and allowed intents.
- `.ci-shepherd-build/tests/test_assessment.py`: judgment authority and final
  envelope.
- `.ci-shepherd-build/tests/test_collector.py`: added workflow/job dimensions.
- `.ci-shepherd-build/tests/test_models.py`: schema validation.
- `.ci-shepherd-build/tests/test_history.py`: v1 compatibility and v2 history.
- `.ci-shepherd-build/tests/test_scripts.py`: CLI, rendering, and skill
  contract.

### Task 1: Add explicit manual policy and typed vocabularies

**Files:**
- Create: `.ci-shepherd-build/policies/manual-v1.json`
- Create: `.ci-shepherd-build/scripts/ci_shepherd/policy.py`
- Create: `.ci-shepherd-build/tests/test_policy.py`
- Modify: `.ci-shepherd-build/scripts/ci_shepherd/models.py`
- Modify: `.ci-shepherd-build/tests/test_models.py`

- [ ] **Step 1: Write failing policy tests**

Create `tests/test_policy.py` with assertions that the checked-in policy loads,
unknown keys fail closed, and review thresholds cannot be zero:

```python
import json
from pathlib import Path
import unittest

from ci_shepherd.policy import PolicyError, load_policy, load_policy_document


ROOT = Path(__file__).resolve().parents[1]


class ManualPolicyTests(unittest.TestCase):
    def test_manual_v1_loads_explicit_review_thresholds(self) -> None:
        policy = load_policy(ROOT / "policies" / "manual-v1.json")

        self.assertEqual("manual-v1", policy.policy_version)
        self.assertEqual(2, policy.quarantine_review_min_distinct_runs)
        self.assertEqual(2, policy.recovery_min_independent_successes)
        self.assertEqual(24, policy.proposal_ttl_hours)
        self.assertEqual(frozenset(), policy.retry_safe_pattern_ids)

    def test_unknown_policy_field_is_rejected(self) -> None:
        document = json.loads(
            (ROOT / "policies" / "manual-v1.json").read_text(encoding="utf-8")
        )
        document["unexpected"] = True
        with self.assertRaisesRegex(PolicyError, "unknown fields"):
            load_policy_document(document)

    def test_zero_review_threshold_is_rejected(self) -> None:
        document = json.loads(
            (ROOT / "policies" / "manual-v1.json").read_text(encoding="utf-8")
        )
        document["quarantineReviewMinDistinctRuns"] = 0

        with self.assertRaisesRegex(PolicyError, "must be positive"):
            load_policy_document(document)
```

- [ ] **Step 2: Run the tests and verify the import failure**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest tests.test_policy -v
```

Expected: FAIL because `ci_shepherd.policy` does not exist.

- [ ] **Step 3: Add the explicit experimental policy**

Create `policies/manual-v1.json`:

```json
{
  "schemaVersion": 1,
  "policyVersion": "manual-v1",
  "quarantineReviewMinDistinctRuns": 2,
  "quarantineReviewMinDistinctCommits": 2,
  "recoveryMinIndependentSuccesses": 2,
  "dormantHumanReviewAfterDays": 7,
  "systemicTransientWindowDays": 14,
  "systemicTransientMinOccurrences": 3,
  "systemicTransientMinFailureRate": 0.05,
  "proposalTtlHours": 24,
  "maxProposalsPerIssue": 3,
  "retrySafePatternIds": []
}
```

These values trigger **manual review**, not automatic quarantine, retry, or
closure. The empty retry-safe allowlist deliberately prevents retry proposals
until a maintainer adds a reviewed pattern ID.

- [ ] **Step 4: Implement strict policy loading**

Create `policy.py` with an immutable `ManualPolicy`, exact-key validation,
positive integer validation, a `(0, 1]` rate check, and duplicate-free string
allowlist validation:

```python
@dataclass(frozen=True)
class ManualPolicy:
    policy_version: str
    quarantine_review_min_distinct_runs: int
    quarantine_review_min_distinct_commits: int
    recovery_min_independent_successes: int
    dormant_human_review_after_days: int
    systemic_transient_window_days: int
    systemic_transient_min_occurrences: int
    systemic_transient_min_failure_rate: float
    proposal_ttl_hours: int
    max_proposals_per_issue: int
    retry_safe_pattern_ids: frozenset[str]


def load_policy(path: Path) -> ManualPolicy:
    return load_policy_document(json.loads(path.read_text(encoding="utf-8")))
```

Reject booleans where integers are expected because `bool` subclasses `int`.

- [ ] **Step 5: Replace the legacy report vocabularies**

In `models.py`, add these exact sets without deleting snapshot validation:

```python
OCCURRENCE_CAUSES = frozenset({
    "test-flake",
    "test-contention",
    "infra-transient",
    "product-regression-suspect",
    "toolchain-build-break",
    "repo-config-break",
    "unknown",
})
LIFECYCLE_STATES = frozenset({
    "new",
    "observing",
    "recurrent",
    "dormant-unverified",
    "dormant-verified",
    "fix-merged-unverified",
    "resolved-verified",
    "needs-policy",
    "human-owned",
    "duplicate-of",
    "data-quality-blocked",
})
PROPOSAL_INTENTS = frozenset({
    "no-op",
    "keep-watching",
    "investigate-now",
    "assign-copilot-investigation",
    "request-closure-review",
    "request-quarantine-review",
    "propose-retry-pattern",
    "request-rerun",
    "escalate-systemic",
    "escalate-blocking",
    "flag-data-quality",
})
TARGET_KINDS = frozenset({
    "issue",
    "test",
    "failureFingerprint",
    "workflowRun",
    "investigation",
})
EXECUTOR_CAPABILITIES = frozenset({
    "post-comment",
    "apply-label",
    "remove-label",
    "close-issue",
    "assign-copilot-investigation",
    "dispatch-rerun",
    "create-policy-pr",
    "create-quarantine-pr",
})
```

- [ ] **Step 6: Run focused tests**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest \
  tests.test_policy tests.test_models -v
```

Expected: PASS.

### Task 2: Normalize occurrences, attempts, coverage, and fingerprints

**Files:**
- Create: `.ci-shepherd-build/scripts/ci_shepherd/observations.py`
- Create: `.ci-shepherd-build/tests/test_observations.py`
- Modify: `.ci-shepherd-build/scripts/ci_shepherd/lifecycle.py`

- [ ] **Step 1: Write failing observation tests**

Cover one failed test, one network transient, two failures in one job, and a
green rerun:

```python
class ObservationTests(unittest.TestCase):
    def test_two_failed_tests_in_one_job_get_distinct_occurrence_ids(self) -> None:
        result = build_observations(
            snapshot_with_failed_tests("A.Test", "B.Test"),
            policy=manual_policy(),
        )

        self.assertEqual(
            [
                "occurrence:12:100:1:900:1",
                "occurrence:12:100:1:900:2",
            ],
            [item["occurrenceId"] for item in result["occurrences"]],
        )
        self.assertEqual(
            ["test-flake", "test-contention", "product-regression-suspect", "unknown"],
            result["occurrences"][0]["allowedCauses"],
        )

    def test_later_attempt_success_is_not_independent_recovery(self) -> None:
        result = build_observations(
            snapshot_with_attempts(
                failed_attempt=1,
                successful_attempt=2,
            ),
            policy=manual_policy(),
        )

        coverage = result["coverage"][0]
        self.assertEqual(2, coverage["attempt"])
        self.assertFalse(coverage["independentRecoveryEligible"])

    def test_network_pattern_is_not_retry_safe_without_policy_allowlist(self) -> None:
        result = build_observations(
            snapshot_with_network_failure(pattern_id="http-502"),
            policy=manual_policy(retry_safe_pattern_ids=frozenset()),
        )

        occurrence = result["occurrences"][0]
        self.assertEqual(["infra-transient", "unknown"], occurrence["allowedCauses"])
        self.assertFalse(occurrence["retrySafe"])
        self.assertEqual(
            "infra:http-502:ubuntu-latest:download-artifact",
            occurrence["fingerprintId"],
        )
```

- [ ] **Step 2: Run the tests and verify failure**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest tests.test_observations -v
```

Expected: FAIL because `observations.py` does not exist.

- [ ] **Step 3: Implement deterministic observation construction**

Implement:

```python
def build_observations(
    snapshot: Mapping[str, Any],
    *,
    policy: ManualPolicy,
    history: Mapping[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    return {
        "occurrences": _build_occurrences(snapshot, policy),
        "coverage": _build_coverage(snapshot),
        "fingerprints": _summarize_fingerprints(snapshot, history, policy),
    }
```

Use sorted source records and stable IDs:

```python
occurrence_id = (
    f"occurrence:{issue_number}:{run_id}:{attempt}:"
    f"{job_id if job_id is not None else 'none'}:{ordinal}"
)
```

Each occurrence must contain `issueNumber`, `runId`, `attempt`, `jobId`,
`workflow`, `lane`, `os`, `headSha`, `observedAt`, `testName`,
`fingerprintId`, `allowedCauses`, `retrySafe`, and `evidenceIds`.

Use readable normalized fingerprints, not cryptographic hashes:

```python
test:{fully_qualified_test_name}
infra:{pattern_id}:{runner_label}:{step_name}
build:{error_code}:{job_name}
unknown:{issue_number}:{run_id}:{job_id}
```

Normalize components to lowercase ASCII with runs of punctuation collapsed to
`-`. Keep the full source values in the occurrence so normalization is
auditable.

- [ ] **Step 4: Make coverage positive and explicit**

Emit lane coverage only for completed successful jobs. Set
`independentRecoveryEligible` only when `attempt == 1`. Emit exact test
coverage only when an evidence record explicitly names the test as executed;
never infer a test pass from a green job:

```python
{
    "coverageId": "coverage:run:200:attempt:1:job:901",
    "subjectKind": "lane",
    "subjectId": "ci-tests:tests-linux:ubuntu-latest",
    "runId": 200,
    "attempt": 1,
    "headSha": "abc...",
    "observedAt": "2026-08-19T12:00:00Z",
    "status": "succeeded",
    "independentRecoveryEligible": True,
    "evidenceIds": ["run:200", "run:200:attempt:1:job:901"]
}
```

Merge prior occurrence records from history only into fingerprint summaries.
Do not copy prior causes or proposals into current semantic candidates. This
lets a closed issue contribute to a systemic rate without biasing the agent's
classification of a new occurrence.

- [ ] **Step 5: Remove decision logic from evidence projection**

Move `_identity`, `_latest_occurrence`, and attempt/coverage helpers from
`lifecycle.py` into `observations.py`. Keep `lifecycle.py` responsible only for
candidate lifecycle and allowed-intent derivation.

- [ ] **Step 6: Run focused tests**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest \
  tests.test_observations tests.test_lifecycle -v
```

Expected: PASS.

### Task 3: Collect the workflow dimensions required by observations

**Files:**
- Modify: `.ci-shepherd-build/scripts/ci_shepherd/collector.py`
- Modify: `.ci-shepherd-build/scripts/ci_shepherd/lifecycle.py`
- Modify: `.ci-shepherd-build/tests/test_collector.py`
- Modify: `.ci-shepherd-build/tests/test_lifecycle.py`

- [ ] **Step 1: Add failing collector tests**

Pin the dimensions returned by the existing GitHub workflow and jobs
endpoints:

```python
self.assertEqual(1, run_payload["attempt"])
self.assertEqual(".github/workflows/tests.yml", run_payload["workflowPath"])
self.assertEqual(["ubuntu-latest"], job_payload["runnerLabels"])
self.assertEqual("ubuntu-latest", job_payload["os"])
self.assertEqual("Tests (Linux)", job_payload["lane"])
self.assertEqual("Run tests", job_payload["failingStep"])
```

Add a lifecycle assertion that missing attempt remains a blocker rather than
defaulting to attempt one:

```python
self.assertIn("missing-attempt-lineage", candidate["blockers"])
self.assertNotIn("request-closure-review", candidate["allowedIntents"])
```

- [ ] **Step 2: Run focused tests and verify failure**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest \
  tests.test_collector tests.test_lifecycle -v
```

Expected: FAIL on missing normalized fields.

- [ ] **Step 3: Extend workflow-run projection**

Map GitHub's `run_attempt` and `path` fields to `attempt` and `workflowPath`.
Do not default a missing attempt:

```python
"attempt": raw_run.get("run_attempt"),
"workflowPath": raw_run.get("path"),
```

Completed reused run evidence remains immutable except when recent-history
metadata makes it volatile, preserving the Trial 20 warm-refresh behavior.

- [ ] **Step 4: Extend workflow-job projection**

Map `labels`, `runner_name`, and failed-step data. Derive `os` only from this
closed set:

```python
{
    "ubuntu-latest": "ubuntu-latest",
    "windows-latest": "windows-latest",
    "macos-latest": "macos-latest",
}
```

If no known label exists, set `os` to `None`. Use the full job name as `lane`;
do not strip matrix dimensions. Set `failingStep` only when exactly one step
has conclusion `failure`; otherwise leave it `None`.

- [ ] **Step 5: Preserve new fields in bounded bundles and history**

Add `workflowPath`, `runnerLabels`, `os`, `lane`, and `failingStep` to
`_PAYLOAD_FIELDS_BY_KIND` in `lifecycle.py` and ensure history evidence
projection retains them.

- [ ] **Step 6: Run focused tests**

Run the command from Step 2.

Expected: PASS.

### Task 4: Derive lifecycle and deterministic allowed intents

**Files:**
- Modify: `.ci-shepherd-build/scripts/ci_shepherd/lifecycle.py`
- Modify: `.ci-shepherd-build/tests/test_lifecycle.py`
- Modify: `.ci-shepherd-build/scripts/prepare.py`
- Modify: `.ci-shepherd-build/tests/test_scripts.py`

- [ ] **Step 1: Replace legacy lifecycle assertions with typed cases**

Add fixtures for first-occurrence flake, recurrent flake, unverified silence,
independent transient recovery, blocking compilation, human ownership, and
data-quality failure. Include quarantined, renamed, deleted, and skipped tests
so their silence cannot become recovery:

```python
candidate = candidate_for(prepared, 12)
self.assertEqual("recurrent", candidate["candidateLifecycle"])
self.assertEqual(
    ["investigate-now", "request-quarantine-review", "keep-watching", "no-op"],
    candidate["allowedIntents"],
)

candidate = candidate_for(prepared_with_green_rerun_only, 13)
self.assertEqual("dormant-unverified", candidate["candidateLifecycle"])
self.assertNotIn("request-closure-review", candidate["allowedIntents"])

candidate = candidate_for(prepared_with_two_first_attempt_successes, 14)
self.assertEqual("resolved-verified", candidate["candidateLifecycle"])
self.assertIn("request-closure-review", candidate["allowedIntents"])

candidate = candidate_for(prepared_with_compile_failure_on_main, 15)
self.assertEqual("new", candidate["candidateLifecycle"])
self.assertIn("escalate-blocking", candidate["allowedIntents"])

for prepared in (
    prepared_with_quarantined_test_only_regular_ci,
    prepared_with_renamed_test_without_replacement_coverage,
    prepared_with_deleted_test_without_source_history,
    prepared_with_skipped_test,
):
    candidate = candidate_for(prepared, 16)
    self.assertIn(
        candidate["candidateLifecycle"],
        {"dormant-unverified", "data-quality-blocked"},
    )
    self.assertNotIn("request-closure-review", candidate["allowedIntents"])
```

- [ ] **Step 2: Run lifecycle tests and verify failure**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest tests.test_lifecycle -v
```

Expected: FAIL because candidates still contain `candidateState` and
`candidateAction`.

- [ ] **Step 3: Build preparation schema version 2**

Change `prepare_assessment` to accept policy plus optional current history and
emit:

```python
{
    "schemaVersion": 2,
    "snapshotId": "snapshot:microsoft/aspire:2026-08-19T16:00:00Z",
    "policyVersion": "manual-v1",
    "repository": "microsoft/aspire",
    "sourceCollectedAt": "2026-08-19T16:00:00Z",
    "policy": policy.as_public_dict(),
    "occurrences": observations["occurrences"],
    "coverage": observations["coverage"],
    "fingerprints": observations["fingerprints"],
    "issues": candidates,
}
```

Add optional `--history-current "$STATE/current.json"` to `prepare.py`. Cold
runs omit it. Warm runs validate the repository and history schema before
using its prior occurrence index. A malformed history file fails preparation
rather than silently discarding systemic evidence.

Each issue candidate contains `candidateLifecycle`, `allowedLifecycles`,
`allowedIntents`, `reasonCodes`, `blockers`, `missingPrerequisites`,
`targetDescriptors`, `evidenceBundle`, and `completenessProof`.

- [ ] **Step 4: Implement conservative lifecycle rules**

Apply these rules in order:

1. Incomplete ledger, unknown producer, missing attempt lineage, or conflicting
   identity yields `data-quality-blocked`.
2. Explicit human ownership or a human-filed issue yields `human-owned`.
3. A deterministic duplicate marker yields `duplicate-of`.
4. A merged fix without sufficient independent coverage yields
   `fix-merged-unverified`.
5. Sufficient first-attempt coverage after the latest occurrence or merged fix
   yields `resolved-verified`.
6. No recent occurrence with exact positive coverage yields
   `dormant-verified`; without positive coverage it yields
   `dormant-unverified`.
7. Distinct-run recurrence yields `recurrent`.
8. One current occurrence yields `new`; otherwise use `observing`.

Never count attempt two or later toward recovery. Never use a green lane as
exact test coverage. Quarantined tests require quarantine-workflow execution;
renamed tests require an explicit old-to-new identity record; deleted tests
require deterministic source-history evidence; skipped tests are not positive
coverage. Synthetic episodes and incomplete episode histories may support
investigation but cannot support `resolved-verified`.

- [ ] **Step 5: Derive intents independently from lifecycle**

Use explicit gates:

```python
if blocking_surface:
    allowed.add("escalate-blocking")
if exact_test and distinct_failure_runs >= policy.quarantine_review_min_distinct_runs:
    allowed.add("request-quarantine-review")
if occurrence["retrySafe"] and cause == "infra-transient":
    allowed.add("propose-retry-pattern")
if independent_successes >= policy.recovery_min_independent_successes:
    allowed.add("request-closure-review")
```

Always include `no-op`. Include `keep-watching` for nonterminal states.
`product-regression-suspect` must never permit `propose-retry-pattern`.
Producer-owned autoclose may influence reason codes, but must not collapse all
other intents to waiting.

Also apply these intent gates:

- `request-rerun` requires a current completed failed workflow run, a selected
  `infra-transient` cause, no newer attempt, and an unexpired snapshot.
- `assign-copilot-investigation` requires a bounded issue or investigation
  target with unresolved evidence; it never grants source or GitHub writes.
- `escalate-systemic` requires
  `systemicTransientMinOccurrences` inside `systemicTransientWindowDays` and a
  failure rate at least `systemicTransientMinFailureRate`. The denominator is
  observed matching lane runs, not elapsed days or all repository builds.
- A dormant-unverified issue older than `dormantHumanReviewAfterDays` permits
  `investigate-now` with reason code `coverage-unverified-after-window`; it
  still does not permit closure.
- `human-owned` permits only `no-op` and `keep-watching` unless the prepared
  issue contains an explicit handoff marker.

- [ ] **Step 6: Make policy an explicit prepare CLI input**

Add required `--policy` to `prepare.py`:

```bash
python3 scripts/prepare.py \
  --input "$SCRATCH/input.json" \
  --history-current "$STATE/current.json" \
  --policy policies/manual-v1.json \
  --output "$SCRATCH/assessment-input.json" \
  --max-bundle-records 25
```

- [ ] **Step 7: Run focused tests**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest \
  tests.test_lifecycle tests.test_scripts -v
```

Expected: PASS.

### Task 5: Validate semantic judgments and compile typed proposals

**Files:**
- Create: `.ci-shepherd-build/scripts/ci_shepherd/assessment.py`
- Create: `.ci-shepherd-build/scripts/finalize.py`
- Create: `.ci-shepherd-build/tests/test_assessment.py`
- Modify: `.ci-shepherd-build/scripts/ci_shepherd/models.py`
- Modify: `.ci-shepherd-build/scripts/validate.py`
- Modify: `.ci-shepherd-build/tests/test_models.py`
- Modify: `.ci-shepherd-build/tests/test_scripts.py`

- [ ] **Step 1: Write failing judgment-authority tests**

Pin allowed cause/lifecycle selection, forbidden upgrades, explicit no-op,
stable IDs, expiry, and typed target validation:

```python
assessment = finalize_assessment(prepared, valid_judgments(), policy)
self.assertEqual(1, assessment["schemaVersion"])
self.assertEqual(prepared["snapshotId"], assessment["snapshotId"])
self.assertEqual("manual-v1", assessment["policyVersion"])
self.assertEqual(
    "proposal:assessment:microsoft-aspire:2026-08-19T16-00-00Z:1",
    assessment["proposals"][0]["proposalId"],
)

with self.assertRaisesRegex(ValidationError, "outside allowedIntents"):
    finalize_assessment(
        prepared,
        judgments_with_intent("request-closure-review"),
        policy,
    )

with self.assertRaisesRegex(ValidationError, "exactly one proposal or no-op"):
    finalize_assessment(prepared, judgments_missing_issue(12), policy)
```

- [ ] **Step 2: Run tests and verify failure**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest tests.test_assessment -v
```

Expected: FAIL because `assessment.py` does not exist.

- [ ] **Step 3: Define the agent judgment document**

Require this exact top-level shape:

```json
{
  "schemaVersion": 1,
  "snapshotId": "snapshot:microsoft/aspire:2026-08-19T16:00:00Z",
  "policyVersion": "manual-v1",
  "occurrenceJudgments": [
    {
      "occurrenceId": "occurrence:12:100:1:900:1",
      "cause": "test-flake",
      "confidence": "medium",
      "reasonCodes": ["exact-test-failure"],
      "evidenceIds": ["run:100:attempt:1:job:900:log"]
    }
  ],
  "issueJudgments": [
    {
      "issueNumber": 12,
      "lifecycle": "recurrent",
      "reasonCodes": ["distinct-run-recurrence"],
      "evidenceIds": ["run:100", "run:101"]
    }
  ],
  "proposalJudgments": [
    {
      "issueNumber": 12,
      "target": {"kind": "test", "testName": "Namespace.Type.Method"},
      "intent": "request-quarantine-review",
      "confidence": "medium",
      "reasonCodes": ["distinct-run-recurrence"],
      "evidenceIds": ["run:100", "run:101"],
      "summary": "Review the recurrent test failure for quarantine."
    }
  ]
}
```

Agent output does not contain `proposalId`, `assessmentId`, `expiresAt`, or an
executor capability. The finalizer owns those fields.

- [ ] **Step 4: Implement judgment validation**

For each judgment:

- Require one occurrence judgment per prepared occurrence.
- Require one issue judgment per open issue.
- Require the selected cause and lifecycle to be in the prepared allowed sets.
- Require every cited evidence ID to occur in that issue's bounded bundle.
- Require every issue to have at least one proposal judgment; `no-op` is a
  proposal intent and makes omissions explicit.
- Reject duplicate targets with the same intent.
- Reject test targets without an exact prepared test name.
- Reject fingerprint and workflow-run targets that were not prepared.
- Reject `propose-retry-pattern` unless the occurrence is deterministically
  `retrySafe` and selected cause is `infra-transient`.
- Enforce `maxProposalsPerIssue`.

- [ ] **Step 5: Compile the final assessment deterministically**

Sort proposals by issue number, target kind, canonical target identifier, and
intent. Generate IDs from that order. Map intents to advisory capabilities:

```python
_CAPABILITY_BY_INTENT = {
    "assign-copilot-investigation": "assign-copilot-investigation",
    "request-closure-review": "close-issue",
    "request-quarantine-review": "create-quarantine-pr",
    "propose-retry-pattern": "create-policy-pr",
    "request-rerun": "dispatch-rerun",
}
```

All mapped execution blocks use `"approval": "manual"` and
`"enabled": false`. Intents without a future capability use `execution: null`.
Compute `expiresAt` from `sourceCollectedAt + proposalTtlHours`.

Generate stable envelope IDs as:

```python
timestamp_id = source_collected_at.replace(":", "-")
repository_id = repository.replace("/", "-").lower()
assessment_id = f"assessment:{repository_id}:{timestamp_id}"
proposal_id = f"proposal:{assessment_id}:{position}"
```

The final envelope copies occurrence IDs with their selected causes and issue
numbers with their selected lifecycles. It derives proposal blockers from the
prepared candidate, not agent prose, and includes `occurrences`, `issues`, and
`proposals` even when every issue is `no-op`.

- [ ] **Step 6: Add the finalizer CLI**

Implement:

```bash
python3 scripts/finalize.py \
  --prepared "$SCRATCH/assessment-input.json" \
  --judgments "$SCRATCH/judgments.json" \
  --policy policies/manual-v1.json \
  --output "$SCRATCH/assessment.json"
```

Use the existing owner-only atomic write pattern from `prepare.py`.

- [ ] **Step 7: Make validation cover the whole chain**

Replace legacy `--report` validation with:

```bash
python3 scripts/validate.py \
  --input "$SCRATCH/input.json" \
  --prepared "$SCRATCH/assessment-input.json" \
  --judgments "$SCRATCH/judgments.json" \
  --assessment "$SCRATCH/assessment.json" \
  --policy policies/manual-v1.json
```

Validation must re-finalize in memory and require exact equality with the
supplied final assessment. This catches manual edits after finalization.

- [ ] **Step 8: Run focused tests**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest \
  tests.test_assessment tests.test_models tests.test_scripts -v
```

Expected: PASS.

### Task 6: Persist evidence, candidates, proposals, and outcomes separately

**Files:**
- Modify: `.ci-shepherd-build/scripts/ci_shepherd/history.py`
- Modify: `.ci-shepherd-build/scripts/record.py`
- Modify: `.ci-shepherd-build/tests/test_history.py`
- Modify: `.ci-shepherd-build/tests/test_scripts.py`

- [ ] **Step 1: Write failing history v2 tests**

Verify immutable file separation, cross-issue fingerprint retention, and v1
refresh compatibility:

```python
current = record_history(
    state,
    REPOSITORY,
    "run-2",
    snapshot,
    prepared,
    judgments,
    assessment,
    policy=manual_policy(),
    outcomes=[],
)

self.assertEqual(2, current.schema_version)
self.assertEqual(
    ["assessment", "evidence", "judgments", "outcomes", "prepared"],
    sorted(current.record_kinds),
)
self.assertIn("infra:http-502:ubuntu-latest:download-artifact", current.fingerprints)
self.assertEqual(
    "request-closure-review",
    current.previous_proposals[0]["intent"],
)
```

Record a v1 fixture first, then load and append v2. Assert v1 factual evidence
remains available to `plan_refresh`, while v1 decisions do not become typed
proposals.

- [ ] **Step 2: Run history tests and verify failure**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest \
  tests.test_history tests.test_refresh -v
```

Expected: FAIL because history only stores snapshot/report pairs.

- [ ] **Step 3: Add immutable history schema version 2**

Store these files in each immutable run:

```text
snapshot.json
prepared.json
judgments.json
assessment.json
outcomes.json
manifest.json
```

The manifest records the source schema version for each file. Continue
accepting valid v1 runs. Never reinterpret `previousDecisions` from v1 as typed
proposals.

Change the public function to:

```python
def record_history(
    state_directory: str | os.PathLike[str],
    repository: str,
    run_id: str,
    snapshot: object,
    prepared: object,
    judgments: object,
    assessment: object,
    *,
    policy: ManualPolicy,
    outcomes: object,
    artifacts: Mapping[str, bytes] | Iterable[tuple[str, bytes]] = (),
) -> CurrentHistory:
    ...
```

- [ ] **Step 4: Build bounded derived history**

When rebuilding `current.json`, scan valid runs and derive:

```json
{
  "schemaVersion": 2,
  "repository": "microsoft/aspire",
  "evidence": {},
  "previousProposals": [],
  "fingerprints": {
    "infra:http-502:ubuntu-latest:download-artifact": {
      "occurrenceIds": [],
      "issueNumbers": [],
      "distinctRunIds": [],
      "firstSeenAt": "2026-08-10T00:00:00Z",
      "lastSeenAt": "2026-08-19T00:00:00Z"
    }
  },
  "outcomes": []
}
```

Deduplicate occurrences by `occurrenceId`. Keep all immutable runs, but bound
the derived fingerprint metrics to the policy's configured window when
preparing the next assessment. Closed issues therefore disappear from the
live inventory without losing their occurrence fingerprints.

- [ ] **Step 5: Isolate decisions from factual evidence**

Extend the existing recursive guard so proposal, judgment, and outcome content
cannot appear inside snapshot evidence. Add the inverse check: prepared
candidate records may reference evidence IDs but may not copy raw issue bodies
or comments.

- [ ] **Step 6: Update record.py**

Require `--prepared`, `--judgments`, `--assessment`, and `--policy`; allow an
optional `--outcomes` that defaults to a generated empty array:

```bash
python3 scripts/record.py \
  --state-dir "$STATE" \
  --input "$SCRATCH/input.json" \
  --prepared "$SCRATCH/assessment-input.json" \
  --judgments "$SCRATCH/judgments.json" \
  --assessment "$SCRATCH/assessment.json" \
  --policy policies/manual-v1.json \
  --artifacts "$SCRATCH"
```

Validate the full chain before creating the staging run directory. Any
validation failure leaves history unchanged.

- [ ] **Step 7: Run focused tests**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest \
  tests.test_history tests.test_refresh tests.test_scripts -v
```

Expected: PASS.

### Task 7: Render proposal queues for manual review

**Files:**
- Modify: `.ci-shepherd-build/scripts/render.py`
- Modify: `.ci-shepherd-build/tests/test_scripts.py`

- [ ] **Step 1: Write failing deterministic queue tests**

Build one proposal for each intent and assert exact section placement:

```python
markdown = render_markdown(snapshot, assessment, snapshot_path=Path("input.json"))

self.assertIn("## Flaky-test verification", markdown)
self.assertIn("## Transient recovery", markdown)
self.assertIn("## Blocking failures", markdown)
self.assertIn("## Systemic patterns", markdown)
self.assertIn("## Quarantine review", markdown)
self.assertIn("## Retry-pattern review", markdown)
self.assertIn("## Copilot investigations", markdown)
self.assertIn("## Rerun review", markdown)
self.assertIn("## Human escalation", markdown)
self.assertIn("## Closure review", markdown)
self.assertIn("## Watching and no action", markdown)
self.assertIn("## Data-quality blockers", markdown)
```

Assert a `request-quarantine-review` proposal appears only in Quarantine
review, and that queue membership depends on typed fields rather than summary
text.

- [ ] **Step 2: Run the render test and verify failure**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest \
  tests.test_scripts.ScriptTests.test_render_script_groups_typed_proposal_queues -v
```

Expected: FAIL because rendering still reads legacy `proposedAction`.

- [ ] **Step 3: Render from assessment.json**

Change the CLI to:

```bash
python3 scripts/render.py \
  --input "$SCRATCH/input.json" \
  --assessment "$SCRATCH/assessment.json" \
  --output "$SCRATCH/report.md"
```

Render:

- Counts by occurrence cause, lifecycle, intent, target kind, and confidence.
- A systemic-fingerprint summary with bounded numerator and denominator.
- One deterministic table per queue.
- Evidence IDs, reason codes, blockers, expiry, and manual capability state.
- Collection errors and missing prerequisites that materially limit proposals.

Do not render agent reasoning as an instruction. The summary is descriptive
text only.

- [ ] **Step 4: Run focused tests**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest tests.test_scripts -v
```

Expected: PASS.

### Task 8: Rewrite the coordinator and fresh-agent contracts

**Files:**
- Modify: `.ci-shepherd-build/SKILL.md`
- Modify: `.ci-shepherd-build/tests/test_scripts.py`

- [ ] **Step 1: Add failing skill-contract assertions**

Assert that `SKILL.md` names the four independent vocabularies, requires
`judgments.json`, forbids executor authority, and documents the new command
chain:

```python
self.assertIn("Occurrence cause is not issue lifecycle", skill)
self.assertIn("write only `judgments.json`", skill)
self.assertIn("recompute, then intersect", skill)
self.assertIn("--prepared \"$SCRATCH/assessment-input.json\"", skill)
self.assertIn("--judgments \"$SCRATCH/judgments.json\"", skill)
self.assertNotIn("Waiting or owned by automation", skill)
```

- [ ] **Step 2: Run the contract test and verify failure**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest \
  tests.test_scripts.ScriptTests.test_skill_documents_typed_assessment_contract -v
```

Expected: FAIL on the legacy report contract.

- [ ] **Step 3: Update the coordinator workflow**

Document this exact artifact sequence:

```text
input.json
assessment-input.json
judgments.json
assessment.json
report.md
progress.json
api-calls.jsonl
```

The coordinator runs collection, preparation, finalization, validation,
rendering, and recording. A fresh agent reads only bounded
`assessment-input.json`, works in batches of at most 10 issues, and writes only
`judgments.json`. The same agent may request bounded evidence expansion and be
resumed; it never accesses GitHub or the raw snapshot.

- [ ] **Step 4: Update the semantic rules**

State explicitly:

- Cause, lifecycle, intent, target, and capability are separate fields.
- The agent selects only prepared causes, lifecycles, intents, and targets.
- It may downgrade to `keep-watching`, `no-op`, or `flag-data-quality`; it may
  never create a stronger candidate.
- Later attempts of one run are not independent recovery.
- Silence without positive execution coverage is `dormant-unverified`.
- A first blocking build failure escalates immediately.
- Quarantine and retry remain reviewed source/configuration changes.
- Assessment JSON is a proposal graph, not a command list.
- Any future executor must recollect and “recompute, then intersect.”

- [ ] **Step 5: Update progress stages**

Use:

```text
inventory -> collection -> preparation -> assessment ->
finalization -> validation -> rendering -> recording
```

Each stage records `pending`, `in-progress`, `completed`, or `failed`, plus
start/end timestamps and output path. A stage may not remain `in-progress`
after its process exits.

- [ ] **Step 6: Run focused tests**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest tests.test_scripts -v
```

Expected: PASS.

### Task 9: Run the complete local regression suite

**Files:**
- Modify only files implicated by failures in `.ci-shepherd-build/`

- [ ] **Step 1: Compile every Python module**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m compileall -q scripts tests
```

Expected: exit code 0 with no output.

- [ ] **Step 2: Run all prototype tests**

```bash
cd .ci-shepherd-build
TMPDIR="$PWD/tests/.tmp" \
  PYTHONPATH=scripts \
  python3 -m unittest discover -s tests -v
```

Expected: all tests PASS. Preserve the final test count in the implementation
handoff.

- [ ] **Step 3: Prove the old unsafe cases remain blocked**

Run these focused tests together:

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 -m unittest -v \
  tests.test_observations.ObservationTests.test_later_attempt_success_is_not_independent_recovery \
  tests.test_lifecycle.LifecycleAssessmentTests.test_silence_without_execution_is_dormant_unverified \
  tests.test_lifecycle.LifecycleAssessmentTests.test_product_regression_cannot_propose_retry \
  tests.test_assessment.AssessmentTests.test_missing_issue_proposal_is_rejected \
  tests.test_history.HistoryTests.test_v1_decisions_do_not_become_typed_proposals
```

Expected: all five tests PASS. These are the falsifiable regressions for the
core safety claims.

### Task 10: Run and watch one fresh-agent shadow assessment

**Files:**
- Read: `.ci-shepherd-build/SKILL.md`
- Create outside the repository: one new session artifact directory containing
  the seven artifacts documented in Task 8

- [ ] **Step 1: Start from the latest valid history**

Use the existing private CI shepherd state and run the collector with
incremental reuse. Record the starting timestamp and API-call count. Do not
copy Trial 15 judgments into the new run.

- [ ] **Step 2: Prepare typed bounded input**

```bash
cd .ci-shepherd-build
PYTHONPATH=scripts python3 scripts/prepare.py \
  --input "$SCRATCH/input.json" \
  --history-current "$STATE/current.json" \
  --policy policies/manual-v1.json \
  --output "$SCRATCH/assessment-input.json" \
  --max-bundle-records 25
```

Expected: schema version 2 input with one issue candidate for every open issue.

- [ ] **Step 3: Launch one fresh assessment agent**

Give the agent the current `SKILL.md`, `ASSESSMENT_INPUT_PATH`, `OUTPUT_DIR`,
and `ASSESSMENT_PHASE=plan`. Require batches of at most 10 issues. Reuse the
same agent for evidence expansion and final judgment generation.

- [ ] **Step 4: Monitor stage transitions without polling loops**

Inspect `progress.json` after the agent reports a batch or the runtime sends a
completion notification. Confirm issue counts advance and no stage remains
unchanged for more than one expected batch duration. If progress stalls,
inspect the current batch and API audit before changing code; do not restart
the whole run blindly.

- [ ] **Step 5: Finalize, validate, render, and record**

Run the exact Task 5, Task 7, and Task 6 commands. Expected:

- Validation prints `valid`.
- Every open issue has a typed proposal or explicit `no-op`.
- Every executor capability is `enabled: false`.
- `report.md` has no “Waiting or owned by automation” section.
- The immutable history run contains separate prepared, judgments, assessment,
  and outcomes records.

- [ ] **Step 6: Compare quality and latency with Trials 15 and 20**

Record:

- Total elapsed time and stage durations.
- GET count and reuse count.
- Counts by cause, lifecycle, intent, and target.
- Number of `data-quality-blocked` and `dormant-unverified` issues.
- Number and evidence basis of quarantine, retry, blocking, systemic,
  investigation, rerun, and closure-review proposals.
- Any proposal rejected by the finalizer and the exact validation reason.

The warm collection target remains approximately Trial 20's 48 seconds and 44
GETs. Assessment quality takes priority over that target; do not weaken
evidence gates to improve runtime.

- [ ] **Step 7: Stop after the shadow report**

Do not post, label, close, rerun, assign, quarantine, or open a PR from the
assessment. Bring the typed JSON, Markdown report, performance comparison, and
validation results back for manual review before designing an executor.
