# CI Shepherd Implementation Status

**Status date:** 2026-08-28

**Branch:** `radical-design-ci-shepherd`

**Committed baseline:** `7905bc4804` plus the bounded-action lifecycle on this
branch

This checkpoint records the current repository state, the validation evidence,
and the remaining operational constraints. The continuation plan is
[CI Shepherd Continuation Plan](../plans/2026-08-28-ci-shepherd-continuation.md).
The governing behavior and safety contract remains
[CI Shepherd Full-Cycle POC Design](../specs/2026-08-21-ci-shepherd-full-cycle-poc-design.md).

## Implemented

- Complete primary inventory for target-labeled issues and pull requests.
- Bounded scan of all open bot-authored issues and pull requests.
- Explicit exclusion of items assigned to Copilot.
- Incremental evidence reuse with visible collection and enrichment budgets.
- Bounded current-state refresh for the 100 most recently updated primary pull
  requests.
- Deterministic issue defaults plus selected-only low-cost agent input.
- Initial cheap assessment for every first-seen nonsuperseded case.
- Fresh assessment for every directly or indirectly changed case, even when the
  new deterministic default is otherwise unambiguous.
- Seven-day reassessment backstop for unchanged issues and pull requests.
- Structured transition context containing the wake reason, prior issue bucket,
  prior pull-request default, and prior review timing.
- Sparse issue and pull-request overrides; silence preserves the deterministic
  default.
- Resumable `cycle.py start` and `cycle.py finish` orchestration.
- Persistent immutable runs, fingerprint and case-event ledgers, and action
  result history.
- Deterministic Markdown, exact action proposals, and network-free actor dry
  runs.
- One-action execution with current-state, target-kind, assignment,
  dependency, ownership, URL, and idempotency preflight.
- Canonical issue and pull-request status identities with legacy issue comment
  migration.
- Report-only pull-request `watch`, `investigate`, and `no-action` states.
  `ping-human` may create a comment; a later non-escalated state may only retire
  that existing comment.
- Unknown issue categories remain separate from their disposition. Generic exit
  codes with unavailable or budget-excluded diagnostics default to report-only
  investigation instead of producing a passive watch comment.
- Complete one-off unknown incidents may become closure candidates only when a
  directly issue-scoped later successful `main` run from the same workflow
  proves recovery and no contradictory blocker remains. Age or silence alone is
  not recovery, and recurrent incidents stay open.
- When an issue transitions from a visible watch or human request to report-only
  investigation, the shepherd proposes retiring the existing owned status
  comment in place rather than leaving stale guidance visible.
- Deterministic `investigation-plan.json` requests for report-only investigation
  recommendations, capped at five new sessions per cycle with the remaining
  work recorded as explicitly deferred.
- Append-only investigation results keyed by issue, target, and source-evidence
  fingerprint. Unchanged evidence reuses a completed result; changed evidence
  produces a fresh request and never exposes the stale result to assessment.
  Multiple target-specific results attach without overwriting one another.
- Structured fix handoffs for `fixable` investigations, without authorizing code
  changes or GitHub writes.
- Deterministic `quarantine-session.json` proposals that combine every current
  test-target candidate, merge duplicate source issues for one test, and
  preserve every original issue URL.
- A serialized one-active-session quarantine ledger with crash-safe appends,
  repository isolation, abandoned-session recovery, and precise partial-success
  tracking.
- Separate `pull-request-open` and merge-confirmed `completed` states. An
  unmerged or closed draft cannot permanently suppress a test; a failed event
  makes its targets eligible again.
- A bounded local quarantine worker contract requiring QuarantineTools, one
  restore, every affected test-project build, quarantine exclusion checks, and
  review of the exact draft PR title/body before push or creation.
- Existing comment and close execution remains comment-first, live-reconciled,
  and individually approval-gated.
- Collection completeness, warnings, error details, pull-request handoff
  exclusions, progress, and GET audit data in recorded artifacts.

## Validation

The final repository validation passed:

```text
917 tests passed
python3 -m compileall passed
git diff --check passed
```

Two Opus 5 reviewers independently reviewed the bounded-action implementation.
They reproduced duplicate-test cycle failure, premature quarantine completion,
truncated-ledger loss, stale and overwritten investigation results, repository
scope errors, abandoned-session deadlock, unbounded investigation fan-out, and
ambiguous test identity. Those findings were fixed. The architecture re-review
verified each correction against the working tree and reported no remaining
design problem; the complete suite then passed again.

### Live read-only cycle

Artifact directory:

```text
/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-complete-live-20260828-final
```

Observed:

- 121 issues and 16 pull requests in the collected primary inventory.
- 56 issue and 16 pull-request cases selected for fresh assessment.
- 112 exact dry-run proposals.
- 626 audited GitHub calls, all `GET`.
- Complete 22-page open-bot scan with 154 bot-authored items adopted.
- No `action-results.json`; no GitHub effect was executed.
- All JSON and JSONL parsed and all artifacts were owner-only.

### Incremental replay

Artifact directory:

```text
/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-complete-live-replay-20260828
```

Observed:

- 122 issues and the same 16 pull requests.
- 7 new or changed issues selected for assessment.
- 0 pull requests selected; all 16 were unchanged.
- 414 audited GitHub calls, all `GET`.
- The same 112 pending proposals remained available because no proposal had
  been approved or executed. This is intentional: an unexecuted action must not
  disappear merely because the next cycle is otherwise unchanged.
- No GitHub effect was executed.

The replay proves that model review is incremental. It does not yet prove
post-execution comment suppression; that requires one explicitly approved live
action followed by recollection.

### Unknown-incident policy replay

The frozen reassessment snapshot was replayed after tightening unknown-incident
handling:

- 892 CI shepherd tests pass; Python compilation and diff checks pass.
- Issue #19452 remains category `unknown` but changes from `watch` to
  report-only `investigate`.
- No comment or other action is proposed for #19452.
- Of 19 unknown issues, 13 default to `investigate`, 5 remain genuine bounded
  watches, and 1 is an evidence-backed duplicate closure candidate.
- No GitHub effect was executed.

### Bounded-action replay

Artifact directory:

```text
/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-bounded-actions-replay-2
```

The corrected frozen replay is stored under
`ci-shepherd-bounded-actions-replay-3`. The same complete live inventory now
produces 5 bounded investigation requests and records 23 additional requests as
deferred. It produces one quarantine session proposal containing 11 exact-case
targets. Seven targets resolve to current C# test methods; one names a test
class, one names a removed or renamed method, and two name VS Code extension E2E
lanes. The worker contract leaves those unresolved targets unchanged, reports
them as blocked, and can open a draft PR only for the validated subset. No
GitHub effect was executed.

## Persisted State

The live trial used private state under the Copilot session artifact root:

```text
/Users/ankj/.copilot/session-state/2d2c6a43-652d-4695-8b36-aa23a7bc689b/files/ci-shepherd-complete-state
```

The supported recurring location is:

```text
$HOME/.copilot/ci-shepherd/state
```

State layout:

```text
state/
  current.json
  runs/<cycle-id>/
  ledgers/fingerprints.jsonl
  ledgers/case-events.jsonl
  ledgers/review-events.jsonl
  ledgers/investigation-results.jsonl
  ledgers/quarantine-sessions.jsonl
  action-results.json
```

`review-events.jsonl` advances only when a selected case finishes assessment.
An unchanged evidence refresh does not postpone the next review. State created
before this ledger existed receives one bootstrap assessment for each current
nonsuperseded case, after which the normal seven-day schedule applies.

Only one cycle writer may use a state directory at a time. The local state has
no remote backup or multi-machine reconciliation.

## Scheduled Workflow

A disabled app-local workflow was created:

```text
Name: CI shepherd daily review
ID: f0381574-877d-4255-9e1d-173047cc3885
Schedule: daily at 08:00 local time
Model: gpt-5-mini, medium reasoning
Mode: autopilot
Enabled: false
```

It is intentionally disabled. The stable Aspire project currently starts from
its normal project branch, which does not contain commit `443e989126` until this
work lands. The workflow checks for `cycle.py` and stops without substitution
if the implementation is unavailable. Do not enable it before the committed
implementation is present in the workflow checkout.

## Known Constraints

- Every GitHub-visible effect still requires separate approval for one exact
  action ID. There is no bulk approval or autonomous mutation.
- Read-only investigations may run without approval, but their results only
  affect a later assessment cycle. A `fixable` result is not permission to
  assign Copilot or start implementation.
- Quarantine preparation requires separate approval for one exact batch. Only
  one local quarantine session may be active, and its worker cannot push or open
  the draft pull request until the title and full `[automated]` body are shown
  and approved.
- The session ledger is the authoritative quarantine reconciliation path. The
  operator records an opened draft, then records completion only after GET-only
  reconciliation proves merge. A closed-unmerged or abandoned draft is recorded
  as failed. Automatic discovery of an unrecorded quarantine pull request is
  not implemented.
- Older disposition-scoped status comments may leave one stale legacy comment
  after the newest comment is migrated to the canonical status slot.
- Unreadable prior state fails closed instead of silently starting a new
  baseline.
- Ledger/state operation assumes one writer. Parallel scheduled or manual
  cycles against the same state directory are unsupported.
- The seven-day reassessment interval is intentionally fixed while the read-only
  trial establishes whether it is too frequent or too sparse.
- A bootstrap review gives the initial inventory one shared deadline, so the
  first weekly reassessment can be a large cohort. This preserves the exact
  seven-day backstop; the read-only trial should measure whether model capacity
  requires a later deterministic distribution policy.
- Local state is not durable across machines. GitHub-hosted scheduling remains
  deferred until remote state ownership is designed.
- The current live evidence includes one unavailable workflow log and several
  explicit reference-budget warnings. These are visible in the report rather
  than converted into success-shaped defaults.

## Implementation Baseline

The committed lifecycle baseline is:

```text
7905bc4804 fix(ci-shepherd): investigate failures missing diagnostics
c25d919f27 feat(ci-shepherd): reassess changed and aging cases
aa5778a62f docs(ci-shepherd): record implementation status
443e989126 feat(ci-shepherd): complete incremental issue and PR lifecycle
66ff7e83cd feat(ci-shepherd): persist lifecycle replay state
97bc8f750f fix(ci-shepherd): render duplicate closure proposals
2a2a165d95 feat(ci): add artifact-driven CI issue shepherd
```

No branch push, pull request, GitHub comment, issue close, rerun, assignment, or
other repository mutation was performed as part of the final validation.
