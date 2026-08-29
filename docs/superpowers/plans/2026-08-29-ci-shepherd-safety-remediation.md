# CI Shepherd Safety Remediation Plan

## Goal

Make it impossible for prose ambiguity, a stale proposal document, a partial
collection, or a process interruption to expand a bounded approval into
unintended GitHub mutations.

No further mutation against `microsoft/aspire` is allowed while this plan is in
progress. Mutation-path simulations use `radical/aspire` only.

## Implementation Status

Implemented and covered by the local suite:

- exact raw-byte-bound grants, fixed enforcement, repository/target/operation
  binding, expiry, and caller-independent state directories;
- persisted mutation and chain budgets;
- append-only intent and terminal events with bounded locking and exact
  reconciliation;
- case-insensitive hard denial of `microsoft/aspire`;
- proposal TTL, per-issue limits, proposal-level eligibility, and fail-closed
  document-level eligibility;
- stable cross-snapshot comment suppression using persisted effects;
- legacy-result migration and paginated comment reads;
- issue-only executable proposals and mandatory explicit repositories.

Still required before production enablement:

- completion of the fork-only interrupted-action and dependent-close
  simulation.

Validated on 2026-08-29:

- three independent validators re-reviewed the repaired safety clusters and
  returned no blockers, and a final independent Opus 5 review verified the
  complete remediation after its four findings were repaired;
- the full local suite passed 968 tests and Python compilation;
- a copy of the real 103-result state migrated to 103 append-only events;
- fork collection found both approved synthetic issues with parsed occurrences
  and no collection errors;
- missing and out-of-scope grants made zero fork mutations;
- one authorized comment executed once and exact-grant replay made no duplicate;
- a fresh collection generated zero duplicate proposals while the marker was
  present;
- after an approved marker-only edit, a fresh-snapshot create was suppressed by
  the persisted stable effect and made no duplicate.
- every executable proposal now carries the source issue `updatedAt` value and
  execution re-fetches the issue and refuses mutation when that version changed;
- related issue or pull-request evidence with only an unverified bare-number
  provenance now makes the complete proposal document execution-ineligible;
- investigation timestamps are parsed as timezone-aware ISO 8601 values;
- collection GET subprocesses have a 60-second timeout, progress heartbeats are
  throttled to 30 seconds, and collection stages fail after 15 minutes without
  advancing a completed run;
- eligibility provenance now round-trips through proposal validation;
- streamed log reads are bounded and terminate stalled subprocesses;
- collection and pipeline wrappers are exempt from leaf-stage deadlines while
  every leaf remains bounded;
- comment and edit results carry the post-mutation issue version so dependent
  closes accept only that exact version;
- the final local suite passed 979 tests after these additions.

## Confirmed Failure Modes

- The executor cannot receive a machine-readable authorization.
- `requiresSeparateApproval` is an always-true schema constant, not a gate.
- The operator instructions authorize every proposal matching prose policy and
  contain no mutation budget.
- Proposal TTL and per-issue proposal limits are parsed but not enforced.
- The action ledger is written after the GitHub mutation and its read-modify-write
  cycle is unlocked.
- A post-mutation reconciliation error can record a successful effect as failed.
- A comment collection error does not make its issue proposal-ineligible.
- The executor reads only the first 100 comments.
- Watch proposals can describe non-CI and zero-occurrence issues as failures.
- Bare issue-like numbers in prose can become apparently verified evidence.
- A stale snapshot can still execute; a prior trial used one more than ten hours
  old.
- Invalid investigation timestamps are accepted.
- Long collection stages have neither a heartbeat nor a deadline.

Happy-path cross-cycle suppression is proven by the prior review-close trial.
Repeat execution is nevertheless unsafe on degraded and interrupted paths.

## Safety Invariants

1. `--execute` performs no GitHub mutation without a valid authorization grant.
2. A grant enumerates exact action IDs; prose, labels, patterns, and disposition
   names cannot broaden it.
3. A persisted budget limits mutation attempts and logical action chains across
   process restarts.
4. The grant is bound to one repository, proposal document digest, snapshot,
   issue or pull-request targets, operation set, and expiry.
5. The proposal document must be execution-eligible as a whole. Any unresolved
   integrity violation blocks every mutation from that document.

   The executor treats an absent `executionEligibility` field as blocked and
   re-derives cheap invariants rather than trusting the producer's self-assertion.
6. An action intent is durably recorded before the first mutating request.
7. An interrupted or indeterminate action is reconciled before any retry; it is
   never blindly repeated.
8. One lock serializes grant consumption and the complete action-result
   read-modify-write cycle.
9. A collection gap affecting mutation safety fails closed for the affected
   target and blocks the proposal document from execution.
10. Stable idempotency identity survives snapshot changes and missing or edited
    GitHub comments.
11. Snapshot age and source-evidence identity are revalidated at execution.
12. Issue closure requires independently verifiable non-self evidence and a
    reconciled prerequisite comment.
13. Simulation code cannot mutate `microsoft/aspire`, even when proposals,
    grants, and command-line arguments consistently name it.
14. Authorization state has one grant-bound location; callers cannot reset
    budgets or idempotency by selecting another results file.

## Public Interfaces

### Proposal document v2

Action proposal documents gain:

- `schemaVersion: 2`;
- `generatedAtUtc`;
- `snapshotId`;
- `snapshotCreatedAtUtc`;
- `executionEligibility.status`, either `eligible` or `blocked`;
- structured `executionEligibility.violations`.

Legacy v1 proposal documents remain readable for reports and dry runs but are
not executable.

The misleading `requiresSeparateApproval` field is removed from v2 proposals.
All execution approval is represented by the grant.

### Authorization grant

`execute_actions.py --execute` requires `--authorization PATH`.

The grant contains:

```json
{
  "schemaVersion": 1,
  "grantId": "grant:<opaque-id>",
  "repository": "radical/aspire",
  "stateDirectory": "/absolute/owner-only/state/path",
  "issuedAtUtc": "2026-08-29T20:00:00Z",
  "expiresAtUtc": "2026-08-29T20:15:00Z",
  "snapshotId": "snapshot:radical/aspire:<timestamp>",
  "proposalsDigest": "sha256:<canonical-proposal-document-digest>",
  "allowedActionIds": ["<exact-action-id>"],
  "allowedOperations": ["create-comment"],
  "allowedTargets": [
    {
      "kind": "issue",
      "number": 1
    }
  ],
  "allowedChainRoots": ["<exact-root-action-id>"],
  "overrideSuppressionForActionIds": [],
  "budget": {
    "maxMutationAttempts": 1,
    "maxChains": 1
  }
}
```

SHA-256 is computed over the raw proposal file bytes. The executor reads the
file once, hashes those bytes, and parses those same bytes so a path change
cannot create a hash/parse time-of-check/time-of-use gap. SHA-256 is appropriate
because the digest is part of an authorization boundary.

The grant loader rejects duplicate keys, unknown fields, malformed timestamps,
duplicate action IDs, empty grants, invalid targets, and inconsistent budgets.
It also validates dependency acyclicity and maximum depth before deriving chain
roots.

Execution accepts only `--state-dir`; `--results` is rejected with `--execute`.
The canonical state directory must equal `stateDirectory` in the grant.
Consumption is appended to a grant sidecar at a path derived from that bound
directory and `grantId`. Copying or moving the grant cannot reset its budget.
Authorization violations always refuse the action without consuming budget;
the grant cannot choose weaker enforcement.

During remediation, the mutating client has a hard deny for
`microsoft/aspire`. Collection entry points require an explicit `--repository`
instead of defaulting to production. Lifting the deny is a separate reviewed
production-enablement change after all go/no-go gates pass.

### Execution result v2

An append-only action event log is authoritative. A rebuildable current-result
projection keeps compatibility with reporting. Events contain:

- `actionId`, `grantId`, `idempotencyKey`, operation, and target;
- `state`: `intent`, `executed`, `stale`, `failed`, or `indeterminate`;
- `startedAtUtc` and optional `completedAtUtc`;
- preflight and reconciliation evidence;
- the resulting comment or issue identity when known.

The CLI appends `intent` under the result lock before making a mutating request,
fsyncs the file and containing directory, and appends the terminal event
afterward. Lock acquisition has a bounded timeout and the lock remains held
across the network operation. If the process exits while the latest event is
`intent` or `indeterminate`, the next invocation performs reconciliation only.
Existing terminal guards are state-aware: terminal results refuse; `intent` and
`indeterminate` reconcile. Absence of the exact expected effect remains
indeterminate and requires a new grant; it does not trigger an automatic retry.

Suppression and reconciliation use different matchers. Suppression can recognize
legacy sibling status keys. Reconciliation requires the exact idempotency key,
body digest, and authenticated login recorded by the intent.

## TDD Implementation Slices

Each slice follows red, green, refactor before the next slice starts.

### Slice 0: Revoke prose authorization

Behavior:

- Operator instructions contain no "execute every" or "without requiring
  another prompt" authorization.
- Instructions state that only an exact grant permits mutation.

Implementation:

- Rewrite the action-authorization and recurring-operation sections in
  `SKILL.md`.
- Add a documentation contract test preventing the old wording from returning.

### Slice 1: Authorization is mandatory and exact

Behavior:

- `--execute` without `--authorization` exits nonzero and makes zero actor calls.
- An expired, wrong-repository, wrong-snapshot, wrong-digest, wrong-operation,
  wrong-target, or non-enumerated action grant exits nonzero and makes zero actor
  calls.
- Dry-run remains available without a grant.
- A valid grant permits only its exact action.
- `--execute --results <fresh-path>` is rejected.
- The grant-bound state directory cannot be changed by command-line input.
- Copying a consumed grant file does not restore its budget.

Implementation:

- Add `ci_shepherd/authorization.py`.
- Add `--authorization` to `scripts/execute_actions.py`.
- Bump executable proposal documents to schema v2.
- Remove `requiresSeparateApproval` from v2 producers and validation.
- Delete grant-controlled violation behavior.

### Slice 1.5: Repository mutation guard

Behavior:

- A proposal and grant that both name `microsoft/aspire` still make zero
  mutating requests while remediation mode is armed.
- Collection and cycle entry points reject an omitted repository.

Implementation:

- Make `--repository` required in collection entry points.
- Require an explicit allowed repository in the actor client.
- Hard-deny non-GET requests to `microsoft/aspire` until a separate production
  enablement change.

### Slice 2: Budgets survive process restarts

Behavior:

- A grant with one mutation attempt cannot execute a second action.
- Reconstructing the CLI from disk does not reset the budget.
- Two processes cannot consume the same final budget slot.
- A comment-plus-close chain counts as one chain and two mutation attempts.

Implementation:

- Add `grantId` and chain-root identity to result records.
- Count unique persisted attempts and roots while holding the result lock.
- Persist a separate grant-consumption sidecar derived from the bound state
  directory and `grantId`.
- Reuse the repository's cross-platform file-locking primitives with a bounded
  acquisition timeout.

### Slice 3: Mutations are crash-reconcilable

Behavior:

- Intent exists before the fake actor receives a mutating call.
- A simulated exit after GitHub accepts a comment leaves `state: intent`.
- Re-running that action finds the exact marker/body and records `executed`
  without a second POST.
- A response or reconciliation failure after a mutating call records
  `indeterminate`, preserving enough identity for reconciliation.
- Concurrent executors cannot lose one another's records.
- A sibling legacy status marker cannot satisfy exact reconciliation.
- `intent` and `indeterminate` route to reconciliation rather than the generic
  already-attempted guard.

Implementation:

- Split action preparation, mutation, and reconciliation behind a small execution
  interface.
- Hold one result lock across grant consumption, the network call, and append-only
  action state transitions.
- Treat any exception after the mutating call begins as indeterminate.
- Fsync intent and terminal events and their containing directory.

### Slice 4: Stable repeat-run suppression

Behavior:

- A prior executed v2 result with the same stable `idempotencyKey` prevents a
  create after its comment is deleted or its marker is edited away.
- A changed status body becomes an explicit drift/review result rather than a
  new create.
- More than 100 comments still finds an existing marker.
- A comments-fetch failure performs no mutation.

Implementation:

- Persist stable idempotency keys and body digests in results.
- Consult both live comments and prior effects during execution.
- Paginate `GitHubActorClient.list_comments`.
- Migrate existing results by joining action IDs to immutable historical
  proposal documents, not by parsing action ID strings.
- Keep `edit-comment` as the sanctioned status-update path.
- Record actor login and keep suppression identity-aware.
- Permit suppression override only for exact action IDs explicitly enumerated
  by the grant.

### Slice 5a: Cheap proposal integrity fails closed

Behavior:

- Issues without an approved CI-failure label produce an explicitly ineligible
  watch proposal that blocks the complete document.
- `unresolved-identity` or zero-occurrence issues produce an explicitly
  ineligible watch proposal that blocks the complete document.
- An issue whose comments collection failed is proposal-ineligible.
- A close proposal with only self evidence, or without its canonical issue
  evidence, makes the proposal document execution-ineligible.
- Any integrity violation blocks execution of all actions in that document.

Implementation:

- Add explicit issue-level collection completeness to the snapshot.
- Restrict effect-producing issue proposals to approved CI labels.
- Add structured document-level execution eligibility.

### Slice 5b: Reference provenance is trustworthy

Behavior:

- A quoted example such as `PR #1234` is not citable evidence.
- Resolving a reference proves existence, not relatedness.

Implementation:

- Preserve reference provenance.
- Require an explicit link or trustworthy relationship before including a
  reference in action evidence.

### Slice 6: Freshness and evidence are revalidated

Behavior:

- An expired grant or stale snapshot cannot mutate.
- A changed evidence fingerprint cannot mutate.
- A close cannot run until its exact prerequisite comment is reconciled.

Implementation:

- Enforce a short proposal TTL at execution.
- Carry the source-evidence fingerprint into v2 proposals and grants.
- Re-fetch and compare the target's execution-relevant evidence before mutation.

### Slice 7: Observability and timestamp hygiene

Behavior:

- Invalid RFC 3339 investigation timestamps are rejected.
- Long stages emit periodic owner-only heartbeat records.
- A stage exceeding its configured deadline aborts with a diagnostic progress
  event and does not advance persistent cycle state.

Implementation:

- Reuse `parse_aware_iso8601`.
- Add heartbeat and deadline support to `ProgressTracker`.
- Wrap GitHub enrichment and other long stages with bounded progress reporting.

This slice follows all mutation-safety slices and the first fork simulation; it
does not gate the initial local safety implementation.

### Slice 8: Remove dead policy

Behavior:

- Every policy field has a production consumer.
- Adding an unconsumed field fails a policy contract test.

Implementation:

- Wire the proposal TTL through execution.
- Remove policy fields that are not used for a current deterministic decision,
  including `maxProposalsPerIssue` if document-level eligibility supersedes it.
- Keep an explicit policy-field/consumer contract test.

## Validation

### Local

- Run each new failing test before implementing its behavior.
- Run the targeted action, proposal, collector, investigation, and script tests.
- Run the complete `.ci-shepherd-build` suite.
- Run Python compilation checks.
- Run a read-only lifecycle replay against existing immutable snapshots.

### `radical/aspire` simulation

After local tests and independent review pass:

1. Create the required CI-failure labels on `radical/aspire`, then create one
   clearly labeled synthetic CI-shepherd simulation issue.
2. Prove a missing grant and an out-of-scope grant make zero mutations.
3. Execute one grant-of-one comment with a 15-minute expiry.
4. Re-run the exact grant and verify no duplicate.
5. Recollect, assert the snapshot contains the target issue as a positive
   control, and verify it produces zero duplicate proposals for that issue.
6. Remove or alter the marker on the fork only, then verify the stable effect
   ledger prevents a replacement comment.
7. Simulate an interrupted local response around a fork mutation and reconcile
   without a second effect.
8. Exercise one comment-plus-close chain on a second fork-only issue only after
   close-evidence guards pass.

Every fork mutation is enumerated in a test manifest before execution and
reconciled afterward. No simulation grant names `microsoft/aspire`.
The hard repository deny remains armed, so a consistent production-repository
typo still cannot mutate production.

## Independent Review Gate

Before implementation, an independent reviewer must:

- challenge the authorization schema and state machine;
- identify bypasses and unsafe restart behavior;
- verify every confirmed postmortem failure maps to a behavior test;
- reject unnecessary complexity;
- confirm the fork simulation can prove the intended guarantees without
  touching `microsoft/aspire`.

Implementation starts only after the review findings are incorporated.

## Go/No-Go Gates

No live mutation is allowed until:

- authorization is mandatory, exact, expiring, and budgeted;
- the budget and result ledger are persistent and locked;
- interrupted effects reconcile without retry;
- degraded collection and proposal-integrity failures fail closed;
- stable idempotency survives missing comments;
- snapshot age and evidence identity are checked at execution;
- the executor cannot accept a caller-selected result path;
- a consumed grant remains consumed when copied;
- `microsoft/aspire` mutations are hard-denied during remediation;
- the complete suite and fork simulation pass.

Issue closure on `microsoft/aspire` remains disabled until the 27 prior closures
receive human review and the close-evidence and dependency tests pass.
