# CI Shepherd Implementation Status

**Status date:** 2026-08-28

**Branch:** `radical-design-ci-shepherd`

**Implementation commit:** `443e989126ca01315319f5bc633dfcf655b20266`

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
- Collection completeness, warnings, error details, pull-request handoff
  exclusions, progress, and GET audit data in recorded artifacts.

## Validation

The final repository validation passed:

```text
864 tests passed
python3 -m compileall passed
git diff --check passed
```

Two fresh Opus 5 reviewers independently rechecked the implementation. Their
final passes reported no blockers after reproducing the legacy-comment,
run-recovery, pull-request-budget, dry-run, and failed-check-fetch cases.

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
  action-results.json
```

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
- Older disposition-scoped status comments may leave one stale legacy comment
  after the newest comment is migrated to the canonical status slot.
- Unreadable prior state fails closed instead of silently starting a new
  baseline.
- Ledger/state operation assumes one writer. Parallel scheduled or manual
  cycles against the same state directory are unsupported.
- Local state is not durable across machines. GitHub-hosted scheduling remains
  deferred until remote state ownership is designed.
- The current live evidence includes one unavailable workflow log and several
  explicit reference-budget warnings. These are visible in the report rather
  than converted into success-shaped defaults.

## Repository State

The worktree was clean after commit `443e989126`. Earlier implementation
commits on the branch are:

```text
66ff7e83cd feat(ci-shepherd): persist lifecycle replay state
97bc8f750f fix(ci-shepherd): render duplicate closure proposals
2a2a165d95 feat(ci): add artifact-driven CI issue shepherd
```

No branch push, pull request, GitHub comment, issue close, rerun, assignment, or
other repository mutation was performed as part of the final validation.
