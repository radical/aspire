# CI Shepherd Proposal and Execution Design

## Summary

The CI shepherd will classify failure occurrences, derive issue lifecycle state,
and emit typed proposals in a versioned JSON assessment. A separate executor
will eventually consume those proposals, but it will enable one capability at a
time and will never treat assessment output as sufficient authority to mutate
GitHub.

The first implementation remains manual and read-only. Its purpose is to run the
process repeatedly, inspect the proposals, tune simple policy values, and measure
precision before any executor capability is enabled.

## Goals

- Distinguish flaky tests, transient infrastructure failures, blocking build
  failures, and unknown failures without forcing them into one issue action.
- Decide what needs immediate investigation, observation, human review,
  quarantine review, retry-pattern review, rerun review, or closure review.
- Retain occurrence history after individual issues close so repeated transient
  fingerprints can become systemic signals.
- Produce machine-validated JSON that future executor capabilities can consume.
- Promote executor capabilities independently based on measured precision.

## Non-Goals

- Automatically quarantine or unquarantine tests.
- Automatically modify retry configuration.
- Automatically rerun workflows, assign investigations, comment, label, or close
  issues in the first implementation.
- Invent quarantine-entry policy when the repository has not defined one.
- Treat absence of a recorded failure as proof that a surface is healthy.

## Existing Repository Constraints

The design must account for current repository behavior rather than infer policy
from issue age:

- `.github/workflows/auto-rerun-transient-ci-failures.yml` currently sets
  `FORCE_RERUN_ALL: 'true'`. A green later attempt of the same run is therefore
  selected by a prior failure and is not independent recovery evidence.
- `docs/unquarantine-policy.md` requires 21 consecutive days with zero failures
  across Windows, Linux, and macOS before unquarantine. It defines exit criteria,
  not quarantine-entry criteria.
- `docs/quarantined-tests.md` states that quarantined tests are excluded from
  regular CI and run in quarantine CI every six hours. Silence in regular CI
  after quarantine is not evidence that the test recovered.

Policy values used during manual iteration may live in the skill or a small
separate policy file. They must be explicit inputs to deterministic preparation,
not values invented by the assessment agent.

## Assessment Architecture

The assessment envelope separates four concepts that must not share one enum.

### Occurrence Cause

Cause is attached to an observed failure occurrence, not permanently to an
issue:

- `test-flake`
- `test-contention`
- `infra-transient`
- `product-regression-suspect`
- `toolchain-build-break`
- `repo-config-break`
- `unknown`

An issue can accumulate occurrences with different causes. Every workflow
observation is keyed by run ID, attempt, job, lane, OS, and commit where
available.

Attempt lineage is mandatory. A successful later attempt of the same run cannot
count as an independent healthy build. Recovery evidence requires distinct run
IDs and first-attempt success on the same relevant lane.

### Issue Lifecycle

Lifecycle is derived deterministically from producer state, occurrence history,
coverage, fixes, and current ownership:

- `new`
- `observing`
- `recurrent`
- `dormant-unverified`
- `dormant-verified`
- `fix-merged-unverified`
- `resolved-verified`
- `needs-policy`
- `human-owned`
- `duplicate-of`
- `data-quality-blocked`

`dormant-unverified` means no recent matching failure was observed but the
shepherd cannot prove that the relevant test or lane executed. It cannot support
closure.

`dormant-verified` requires positive coverage proof: the relevant test or lane
executed successfully during the observation window with no matching failure.

### Proposed Intent

The read-only assessment emits advisory intents:

- `no-op`
- `keep-watching`
- `investigate-now`
- `assign-copilot-investigation`
- `request-closure-review`
- `request-quarantine-review`
- `propose-retry-pattern`
- `request-rerun`
- `escalate-systemic`
- `escalate-blocking`
- `flag-data-quality`

The agent selects only from deterministic allowed intents and may downgrade but
never upgrade a candidate.

### Executor Capability

Executor capabilities are a separate allowlist:

- `post-comment`
- `apply-label`
- `remove-label`
- `close-issue`
- `assign-copilot-investigation`
- `dispatch-rerun`
- `create-policy-pr`
- `create-quarantine-pr`

No capability is enabled by this design. Each capability receives its own
shadow-mode history, approval policy, budgets, cooldowns, and promotion decision.

## Typed Proposal Targets

One assessment envelope can contain proposals for different target types:

- `issue`: comment, label, escalation, or closure review.
- `test`: quarantine review for a fully qualified test name.
- `failureFingerprint`: systemic escalation or retry-pattern review.
- `workflowRun`: rerun review for an exact run and attempt.
- `investigation`: a bounded Copilot investigation attached to an issue.

This avoids pretending that a quarantine source change or retry-pattern change
is merely an issue action.

## Initial Manual Policy

### Flaky Tests

A first occurrence triggers verification rather than passive waiting. The
shepherd must identify the exact test, affected lane and OS, current quarantine
state, and independent execution history.

A quarantine review requires recurrence across distinct run IDs or commits, or
another explicit configured rule. The proposal carries evidence such as
per-lane failures, distinct commits or pull requests affected, and whether the
pattern appears contention-dependent. The shepherd does not add
`[QuarantinedTest]`; quarantine is a reviewed source change.

If no recurrence appears, time alone does not prove recovery. The shepherd uses
successful execution count as the primary window and elapsed days as a secondary
guard:

- Executions observed with no failure can produce `dormant-verified`.
- No executions or unknown coverage produces `dormant-unverified`.
- A configured run/time threshold can produce a human review proposal, but not
  an automatic closure.

### Infrastructure and Network Transients

Closure review requires later independent first-attempt successes on the same
workflow, job, lane, and OS where those dimensions are relevant. A green rerun
attempt of the original failure does not count.

History aggregates a normalized transient fingerprint such as pattern ID,
runner label, and failing step. Systemic escalation uses a windowed failure rate,
not an unbounded lifetime count.

`propose-retry-pattern` is limited to failures already classified as retry-safe
infrastructure behavior. It proposes a reviewed policy/configuration change. It
must never be used for `product-regression-suspect` failures or as a way to hide
recurrent product or test races.

### Other Build and Tooling Failures

Urgency is independent of recurrence. A first occurrence that blocks `main`,
release validation, compilation, packaging, or repository configuration becomes
`investigate-now` or `escalate-blocking`. It does not wait for a second
occurrence and is not made retry-eligible merely because a rerun succeeds.

## Assessment JSON

The envelope contains immutable evidence references and typed proposals:

```json
{
  "schemaVersion": 1,
  "assessmentId": "assessment-...",
  "snapshotId": "snapshot-...",
  "policyVersion": "manual-v1",
  "collectedAt": "2026-08-19T00:00:00Z",
  "occurrences": [],
  "issues": [],
  "proposals": [
    {
      "proposalId": "proposal-...",
      "target": {
        "kind": "issue",
        "number": 123
      },
      "intent": "investigate-now",
      "reasonCodes": [
        "blocking-main"
      ],
      "evidenceIds": [
        "run:456"
      ],
      "blockers": [],
      "confidence": "high",
      "expiresAt": "2026-08-20T00:00:00Z",
      "execution": {
        "capability": "assign-copilot-investigation",
        "approval": "manual"
      }
    }
  ]
}
```

Every issue receives either one or more proposals or an explicit `no-op` reason.
This makes omissions visible and allows precision and recall to be evaluated.

Proposal reason codes and blockers are machine-readable. Agent prose is
explanatory only and cannot become an executor command or unvalidated issue
comment.

## History Model

History stores four records separately:

1. Evidence observed.
2. Deterministic issue state and allowed intents.
3. Proposals selected.
4. Human or executor outcomes.

Occurrence history survives issue closure and is indexed by normalized failure
fingerprint. Metrics use a bounded time/run window and a denominator such as
failures per observed lane runs.

The history also records whether a later proposal proved correct. Precision is
measured independently for quarantine review, retry-pattern review, rerun,
Copilot investigation, escalation, and closure.

## Executor Boundary

The executor treats assessment JSON as a proposal, never as authority:

1. Validate schema, policy version, proposal type, target, and evidence IDs.
2. Reject expired proposals.
3. Recollect the target and required current evidence.
4. Recompute deterministic allowed capabilities.
5. Intersect the recomputed capabilities with the proposed capability.
6. Reject stale snapshot or evidence-hash mismatches.
7. Check producer opt-in, episode identity, idempotency, budgets, cooldowns, and
   kill switch.
8. Journal intent before execution.
9. Execute one typed capability.
10. Journal the observed outcome.

Capabilities are promoted individually. A reliable label action cannot be used
to justify enabling closure or rerun execution.

Generated comments use executor-owned templates over validated fields. Agent
text cannot inject producer markers, mentions, closing keywords, or arbitrary
Markdown into issue bodies or comments.

## Actions That Remain Human-Reviewed

The following are never direct automatic actions:

- Quarantining or unquarantining a test.
- Modifying retry patterns or CI policy.
- Acting on `product-regression-suspect` beyond investigation or escalation.
- Acting on human-filed or `human-owned` issues without explicit handoff.
- Acting on release or servicing branches without separate policy.
- Closing based on a synthetic episode, incomplete ledger, stale snapshot, or
  silence without coverage proof.

## Manual Rollout

The first rollout is read-only:

1. Emit the typed JSON from current evidence and simple policy values.
2. Render separate queues for flaky verification, transient recovery, blocking
   failures, systemic patterns, quarantine review, retry-pattern review, Copilot
   investigation, rerun review, human escalation, and closure review.
3. Manually inspect every non-`no-op` proposal.
4. Compare proposals with later human outcomes.
5. Tune policy and evidence requirements.
6. Promote one executor capability only after its own shadow precision is
   acceptable.

## Validation

Fixtures and live shadow runs must prove:

- A green rerun attempt is not independent recovery evidence.
- Silence without observed executions cannot support closure.
- Quarantined, renamed, deleted, or skipped tests require appropriate coverage
  proof.
- A first-occurrence blocking failure escalates immediately.
- Product-regression suspects never become retry proposals.
- Repeated transient fingerprints escalate by rate and bounded window.
- Incomplete ledgers, synthetic episodes, stale snapshots, and human ownership
  block execution.
- The executor rejects stale or altered proposals and recomputes before
  intersecting capabilities.
- Every issue has an explicit proposal or `no-op` reason.
- Existing GitHub collection remains GET-only during the manual phase.

## Acceptance Criteria

- The JSON schema separates occurrences, lifecycle, intents, targets, and
  executor capabilities.
- Flaky-test decisions require exact test identity and execution coverage.
- Transient recovery excludes later attempts of the original run.
- Blocking failures can escalate on their first occurrence.
- Cross-issue transient history can produce a systemic proposal.
- Thresholds are explicit policy inputs, not agent-generated values.
- Assessment output cannot directly authorize a write.
- Executor capabilities can be enabled and evaluated independently.
