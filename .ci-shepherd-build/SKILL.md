---
name: ci-shepherd
description: "Incremental CI shepherd for microsoft/aspire. A coordinator refreshes bounded GET-only evidence, a fresh agent reviews first-seen, materially changed, or periodically due cases, and deterministic scripts validate reports and exact-action proposals."
---

# CI Shepherd

The shepherd refreshes the complete eligible issue and pull-request inventory,
reuses unchanged factual evidence, and sends first-seen, materially changed, or
periodically due cases to a fresh assessment agent. Stable reviewed cases stay
out of model input until their seven-day reassessment deadline. Collection and
assessment are advisory. The coordinator may run bounded read-only
investigations without approval. GitHub-visible effects and local quarantine
work remain separate, individually approved actions.

## Supported cycle

Use stable private state and a disposable work directory:

```bash
export CHECKOUT="$(git rev-parse --show-toplevel)"
export GITHUB_LOGIN="$(gh api user --jq .login)"
export CI_SHEPHERD_ROOT="$CHECKOUT/.ci-shepherd-build"
export STATE="$HOME/.copilot/ci-shepherd/state"
export SCRATCH="$HOME/.copilot/ci-shepherd/runs/manual-$(date -u +%Y%m%dT%H%M%SZ)"

python3 "$CI_SHEPHERD_ROOT/scripts/cycle.py" start \
  --repository microsoft/aspire \
  --checkout "$CHECKOUT" \
  --state-dir "$STATE" \
  --work-dir "$SCRATCH" \
  --shepherd-author "$GITHUB_LOGIN"
```

`cycle.py start` performs the GET-only refresh, prepares compact issue and
pull-request handoffs, and prints a cycle manifest. If nothing needs model
review, it also finalizes and records the run. Otherwise, launch a fresh cheap
assessment agent with only this skill and these files:

```text
$SCRATCH/agent-input.json
$SCRATCH/review-selection.json
$SCRATCH/pull-request-review.json
```

The assessment agent writes sparse issue overrides to
`$SCRATCH/agent-judgments.json` and sparse pull-request overrides to
`$SCRATCH/agent-pull-request-judgments.json`. Silence for a selected case means
"keep the deterministic default"; omitted cases must not be returned. Finish
the exact cycle with:

```bash
python3 "$CI_SHEPHERD_ROOT/scripts/cycle.py" finish \
  --work-dir "$SCRATCH" \
  --agent-judgments "$SCRATCH/agent-judgments.json" \
  --pull-request-judgments "$SCRATCH/agent-pull-request-judgments.json"
```

The supported cycle writes deterministic `report.md`,
`action-proposals.json`, `actor-dry-run.json`, `investigation-plan.json`, and
`quarantine-session.json`, then records the validated snapshot, judgments, and
artifacts under `$STATE/runs/<cycle-id>/`. A failed or interrupted cycle does
not advance `current.json`. Successfully selected issue and pull-request reviews
are recorded in
`$STATE/ledgers/review-events.jsonl`; merely refreshing an unchanged case does
not reset its reassessment clock.

## Open inventory scope

Open primary inventory covers every item a human reviewer should be able to
see moving:

- every issue and pull request carrying a target label, whoever opened it;
- every issue and pull request opened by **any** bot, not only the logins
  configured in `BOT_AUTHORS`;
- minus anything currently assigned to Copilot, which is rejected and recorded
  in `rejectedCandidates`.

The "any bot" half cannot use search: GitHub rejects an app-author wildcard
(`author:app/*` returns HTTP 422) and `creator=` takes exactly one login. The
collector therefore pages `/repos/{repo}/issues?state=open` sorted by recency
and keeps items whose `user.type` is `Bot`.

That scan is bounded by `max_open_scan_pages` and `max_bot_authored_open`, and
reports which bound it hit in `InventoryResult.open_bot_scan`:

- `complete` — the whole open list was scanned.
- `truncated` — a budget stopped the scan; the most recently updated items are
  the ones kept.
- `failed` — a page request failed or returned an unexpected shape.

A `truncated` or `failed` scan degrades rather than aborts: label and
configured-creator results are already collected by that point, and the cycle
is review-only, so a missed item is deferred work rather than a wrong action.
Both non-complete outcomes add a warning, and `failed` also records a
`CollectionError` with stage `open-bot-scan`, so an incomplete inventory can
never read as a clean one.

## Safety boundary

- The coordinator and a bounded issue investigator may use GET-only GitHub
  access.
- The fresh assessment agent must never access GitHub, run `gh`, browse
  GitHub, use web search, or call GitHub APIs.
- Collection, assessment, and investigation must never write to GitHub.
- A quarantine recommendation is a separately approved request for one isolated
  local worktree session. The worker may edit and validate locally, but must not
  push or open a pull request until its draft title and body receive approval.
- Push, pull-request creation, quarantine, rerun, and retry remain individually
  approval-gated.
- Every mutation requires an exact machine-readable authorization grant. The
  grant enumerates action IDs, targets, operations, expiry, proposal identity,
  and a persistent mutation budget. Prose, labels, disposition names, and
  sequential invocation are never authorization.
- Executable proposals are limited to issue comments, issue-comment edits, and
  issue closure. Pull-request findings and all other high-risk actions remain
  advisory and never enter the executable proposal document.

### Pull-request assessment

New, changed, or periodically due primary pull requests carry current
head-commit checks, current review state, mergeability, and only shepherd-owned
canonical status comments.
If any current-state fetch fails, the handoff says the evidence is incomplete
and permits only `watch`. An empty or cancelled check set is not green. Current
state is collected for at most 100 primary pull requests per cycle; any
additional pull requests remain visible with incomplete evidence and a warning.
Primary-inventory pull requests do not fetch changed-file lists because that
data is not used by the PR assessment.

The pull-request agent output has this sparse shape:

```json
{
  "schemaVersion": 1,
  "snapshotId": "snapshot:microsoft/aspire:2026-08-28T12:00:00Z",
  "pullRequests": [
    {
      "pullRequestNumber": 123,
      "disposition": "investigate",
      "summary": "Current checks fail in the generated workflow.",
      "evidenceIds": ["pr:123"]
    }
  ]
}
```

Allowed dispositions are `investigate`, `watch`, `ping-human`, and
`no-action`. Closure is not representable. `ping-human` requires a reported
human decision such as changes requested or a merge conflict, plus structured
`humanEscalation`. Only `ping-human` creates a pull-request comment;
`watch`, `investigate`, and `no-action` remain report-only unless they replace
an existing shepherd escalation with a terminal status edit. Proposed comments
use one canonical `pull-request:<number>:status` identity and are suppressed
when their complete body is unchanged. Copilot assignment is checked during
inventory, proposal rendering, and execution.

## Issue communication and action boundary

The assessment agent never executes actions. It emits evidence-backed
recommendations only. The coordinator may render local action proposals after
finalization and validation, but a proposal is not authorization to post.

Before approval, show the exact target, complete rendered text or command, cited
evidence, expected result, dependency chain, and maximum mutation count. Record
that approval in an exact machine-readable grant bound to the frozen proposal
document and state directory. If the executor does not receive a valid grant,
leave every effect proposed and make no GitHub write.

`action-proposals.json` remains the only source of GitHub-visible effects, but it
is not authorization. Execute only action IDs explicitly enumerated by the
grant, and stop when its persisted mutation or chain budget is exhausted.
Recheck that the evidence fingerprint is unchanged, the target remains in its
expected state, and the exact action has no terminal ledger result. Use the
frozen comment body. Run a dependent close only after its comment reconciles
successfully. Any grant, document-integrity, collection-completeness, or budget
violation fails closed before GitHub mutation. Reconcile live state and update
the report after all attempted effects.

Only explicit issue or pull-request URLs, structured triggering-PR fields,
occurrence-table PRs, and references in an explicit resolution context may
support a GitHub-visible action. A bare `#1234` mention proves neither
relatedness nor that the target is even the intended kind; proposals citing
only that provenance are execution-ineligible.

Collection GET subprocesses and mutation subprocesses each have a 60-second
timeout. Collection stages emit throttled owner-only progress heartbeats and
fail after 15 minutes rather than advancing persistent state after an
unbounded stall.

Use one canonical CI shepherd status comment per issue. All automatically
posted GitHub text starts with `[automated] `. The comment uses identity-only
markers:

```html
<!-- ci-shepherd:role=status -->
<!-- ci-shepherd:idempotency-key=issue:19166:status -->
```

Shepherd-authored status comments must not contribute markers, facts, or
references. Retain their owned comment identity only for idempotency. An
unchanged watch state must not create or edit a comment. A changed comment body
is a new reviewable proposal and requires separate approval.

## Bounded investigation lifecycle

`investigation-plan.json` contains at most five new requests per cycle for
current `investigate` recommendations whose issue, target, or source evidence
has not already been investigated. Additional requests remain visible under
`deferredRequests` and become eligible after earlier requests are recorded.
Each request has a deterministic
`investigationId`, the issue URL, evidence IDs, missing evidence, stop
condition, and an exact `workerPrompt`.

Launch each new request in a fresh read-only agent. Give the worker only the
request and its prompt. The worker must use the issue-investigation workflow,
must not edit code or write to GitHub, and must return the required JSON result.
Validate and record it with:

```bash
python3 "$CI_SHEPHERD_ROOT/scripts/investigation_result.py" \
  --state-dir "$STATE" \
  --plan "$SCRATCH/investigation-plan.json" \
  --investigation-id "investigation:..." \
  --result "$SCRATCH/investigation-result.json" \
  --recorded-at "2026-08-28T20:30:00Z" \
  --session-id "<worker-session-id>"
```

The next cycle attaches every target-specific result whose source-evidence
fingerprint still matches. An unchanged issue reuses the completed results and
starts no duplicate investigations. Materially changed evidence creates new
requests and the stale results are not shown to the assessment agent. A
`fixable` result is only a structured handoff candidate; it does not authorize
code changes, assignment, or a pull request.

## Approved quarantine session

`quarantine-session.json` combines all current `review-quarantine`
recommendations into one deterministic proposal. It preserves each exact test
name and every original issue URL. Multiple issues for the same test become one
test edit whose PR body addresses every source issue. Tests in an open
quarantine PR and tests in a merged PR are removed from later batches, and an
active local session suppresses every new quarantine proposal.

Before starting, show the user the batch ID, complete test list, original issue
links, and exact worker prompt. After approval:

1. Create one idle local worktree session from the repository default branch.
2. Record `started` before sending work to that session. For a staged
   single-test trial, add `--test-name "Namespace.Type.Method"`; this derives a
   separately identified one-test batch from the validated proposal:

   ```bash
   python3 "$CI_SHEPHERD_ROOT/scripts/quarantine_session.py" \
     --state-dir "$STATE" \
     --request "$SCRATCH/quarantine-session.json" \
     --status started \
     --recorded-at "2026-08-28T20:30:00Z" \
     --session-id "<worktree-session-id>"
   ```

3. Send the proposal's exact `workerPrompt` to the session. Do not start a
   second quarantine worker.
4. Require the worker to run QuarantineTools once per test, restore once, build
   every affected test project, verify every target is excluded as quarantined,
   and return the exact diff, commands, results, and draft PR title/body.
5. Leave an unresolved or non-method target unchanged, report it as blocked, and
   continue validating the remaining targets. If unrelated files changed or a
   changed target still fails validation after one quarantine-only correction,
   record `failed` and stop.
6. If a target is already quarantined with the original issue URL, do not create
   an empty commit or PR. Verify the merged PR that introduced the exact
   attribute, then record `completed` directly from the started batch with that
   PR URL and exact test list. This reconciles stale proposals without a false
   `pull-request-open` event.
7. Show the draft PR title and full body. Only after approval, commit, push the
   branch to the user's fork, and open a draft PR. The visible body begins with
   `[automated] ` and uses `Addresses #N`; the original failure issues remain
   open.
8. Record `pull-request-open` with the draft PR URL and only the tests actually
   changed and validated:

   ```bash
   python3 "$CI_SHEPHERD_ROOT/scripts/quarantine_session.py" \
    --state-dir "$STATE" \
    --request "$SCRATCH/quarantine-session.json" \
    --status pull-request-open \
    --recorded-at "2026-08-28T21:00:00Z" \
    --session-id "<worktree-session-id>" \
    --pull-request-url "https://github.com/microsoft/aspire/pull/..." \
     --completed-test "Namespace.Type.Method"
   ```

   Repeat `--completed-test` for every test represented in the pull request.
   Blocked targets are not recorded in the open-PR event and remain eligible
   for later reassessment.
9. A draft PR is not completion. Every later pass reconciles each pending PR
   before proposing another quarantine batch:
   - if it merged, record `completed` with the same URL and exact test list;
   - if it is open with PR-caused failing checks, resume the recorded worktree
     session to diagnose, fix, validate, commit, and push;
   - if it is open with pending or successful checks, leave it awaiting the PR;
   - if it closed unmerged or was abandoned, record `failed` so the tests can be
     proposed again.

   The follow-up worker preserves complete failing-command output and reports
   each pushed commit. A later cycle can finish either terminal transition by
   passing `--batch-id` with its current `quarantine-session.json`; the session
   ID is recovered from the ledger.

This is intentionally a lightweight coordinator protocol, not a scheduler or
general job engine.

## Dry-run action actor

`judgments.json` is the only validated decision authority. Deterministic
proposal rendering converts those judgments into exact effects:

```bash
python3 "$CI_SHEPHERD_ROOT/scripts/propose_actions.py" \
  --snapshot "$SCRATCH/input.round-1.json" \
  --prepared "$SCRATCH/assessment-input.round-1.json" \
  --agent-input "$SCRATCH/agent-input.round-1.json" \
  --judgments "$SCRATCH/judgments.json" \
  --shepherd-author "$SHEPHERD_AUTHOR" \
  --output "$SCRATCH/action-proposals.json"
```

`action-proposals.json` is the only external-effect authority. The actor never
reinterprets `judgments.json`, issue prose, or evidence, and never regenerates
comment text or close reasons. The compact agent input supplies deterministic
action-cluster context only; it cannot create a proposal without a matching
validated judgment.

The actor is dry-run by default. This command validates and prints every exact
proposed effect:

```bash
python3 "$CI_SHEPHERD_ROOT/scripts/execute_actions.py" \
  --proposals "$SCRATCH/action-proposals.json" \
  --state-dir "$STATE"
```

Dry-run performs no GitHub access and does not create or modify
`action-events.jsonl`. Add `--action-id <exact-id>` to preview only one
proposal. `--state-dir` is optional for dry-run.

Mutation is a separate validated step. `--execute` requires one exact
`--action-id`, one exact `--authorization` grant, and the grant-bound
`--state-dir`. Sequential invocation does not limit total impact; only the
persisted grant budget does.

The authorization file is an exact grant, not an operator note. It must bind
the repository, absolute state directory, snapshot ID, SHA-256 digest of the
raw proposal bytes, explicit action IDs, operations, issue targets, chain
roots, expiry, and mutation/chain budgets. Unknown or duplicate fields are
rejected. Copying the grant does not reset its budget because consumption is
derived from the grant ID in the grant-bound append-only event log.

```bash
python3 "$CI_SHEPHERD_ROOT/scripts/execute_actions.py" \
  --proposals "$SCRATCH/action-proposals.json" \
  --authorization "$SCRATCH/authorization-grant.json" \
  --state-dir "$STATE" \
  --action-id "snapshot:...:issue:19149:review-close-comment" \
  --execute
```

Execute mode accepts only proposal schema v2 and re-derives its CI-label,
occurrence, collection-completeness, and evidence-availability eligibility
checks. Before any mutation it fsyncs an `intent` event under a bounded lock.
It then checks dependencies and current GitHub state, performs one fixed
operation, refetches the target, and appends a terminal event to owner-only
`$STATE/action-events.jsonl`. A surviving `intent` or `indeterminate` event
permits reconciliation only; it never permits another mutation. Reconciliation
requires the exact idempotency key, body, and authenticated author. The
executor never treats `--execute` as approval for the whole proposal document,
never accepts `--results` in execute mode, and hard-denies mutations to
`microsoft/aspire` while remediation remains active.

## Artifacts

The live trial uses this artifact set:

```text
input.json
assessment-input.json
assessment-defaults.json
agent-input.json
review-selection.json
pull-request-review.json
agent-judgments.json
agent-pull-request-judgments.json
judgments.json
pull-request-judgments.json
investigation-plan.json
quarantine-session.json
report.md
action-proposals.json
actor-dry-run.json
progress.json
api-calls.jsonl
cycle.json
run-completion.json
retrospective-request.json
retrospective.json
retrospective.md
```

Cross-cycle lifecycle state is stored separately from the immutable scratch
artifacts:

```text
$STATE/
  current.json
  runs/<cycle-id>/
  ledgers/fingerprints.jsonl
  ledgers/case-events.jsonl
  ledgers/review-events.jsonl
  ledgers/investigation-results.jsonl
  ledgers/quarantine-sessions.jsonl
  action-events.jsonl
  action-results-migration-v1.json
```

`input.json` is the coordinator-owned raw collection. `assessment-input.json`
is the coordinator-owned prepared assessment. `assessment-defaults.json`
contains the complete deterministic compact assessment used when sparse
overrides are merged; `agent-input.json` contains only the selected issues sent
to the model. `related-issues.json` is an
optional frozen canonical-test search result used only for offline tracker and
history matching. The compact handoff is generated by `compact.py` from
`assessment-input.json`. It produces `agent-input.json`.
`fingerprints.jsonl` is the append-only exact-fingerprint occurrence ledger
under `$STATE/ledgers`, so recurrence survives scratch cleanup.
`case-events.jsonl` records bootstrap and material disposition transitions.
`review-events.jsonl` records only cases actually handed to the assessment
agent. Its latest timestamp per target drives the seven-day reassessment
backstop; automatic unchanged cycles do not postpone that review.
`investigation-results.jsonl` records validated read-only conclusions keyed by
the issue, target, and source-evidence fingerprint.
`quarantine-sessions.jsonl` records the one-at-a-time local quarantine
lifecycle, including the completed draft pull-request URL.
After ledger bootstrap has converged, replaying unchanged evidence must append
no case event when the deterministic pipeline and agent override input produce
the same material case state.
The POC state directory has one recorder at a time; concurrent lifecycle
recorders against the same state directory are unsupported.
The `round-1` artifacts are one bounded evidence-planning and expansion pass:
the request document, immutable expanded snapshot, regenerated prepared input,
fresh compact verifier input, and fresh verifier judgments.
`review-selection.json` sends every first-seen issue, every materially changed
issue, and every issue whose seven-day reassessment is due to the model.
`agent-input.json` is filtered to that same set. Stable reviewed cases are
omitted from both until they change or become due.
`agent-judgments.json` is the only issue assessment-agent output. `finalize.py`
accepts sparse agent changes only for selected cases and restores every safe
deterministic default into `judgments.json`. `report.md` is rendered
deterministically after validation. The report includes collection completeness
and warnings. `progress.json` records stage status, and `api-calls.jsonl` is the
coordinator-owned GET audit for collection or expansion. Both are copied into
the immutable recorded run when present.

For prompt and rule iteration, freeze one `assessment-input.json` and reuse it.
Offline prompt iterations must start from a frozen `assessment-input.json` and
must not rerun collection. Regenerate only `agent-input.json`,
`agent-judgments.json`, `judgments.json`, and `report.md`. Refresh the frozen
input only when evaluating collection behavior or intentionally taking a new
evidence snapshot.

Legacy `report.json` final-agent flow is deprecated. Do not ask a live trial
assessment agent to produce it; use `judgments.json` and the POC validation and
rendering commands below.

## Coordinator responsibilities

The coordinator collects, prepares, validates, renders, and records artifacts.
It owns any permitted deterministic GitHub collection scripts and gives the
fresh assessment agent only the bounded compact input.

Use these POC commands:

```bash
python3 "$CI_SHEPHERD_ROOT/scripts/collect.py" \
  --repository microsoft/aspire \
  --checkout "$CHECKOUT" \
  --output-dir "$SCRATCH" \
  --state-dir "$STATE" \
  --max-run-refs-per-issue 12 \
  --max-issue-refs-per-issue 5 \
  --max-commit-refs-per-issue 3
```

```bash
python3 "$CI_SHEPHERD_ROOT/scripts/prepare.py" \
  --input "$SCRATCH/input.json" \
  --output "$SCRATCH/assessment-input.json" \
  --max-bundle-records 25
```

```bash
python3 "$CI_SHEPHERD_ROOT/scripts/compact.py" \
  --prepared "$SCRATCH/assessment-input.json" \
  --related-issues "$FIXTURE/related-issues.json" \
  --fingerprints "$STATE/ledgers/fingerprints.jsonl" \
  --output "$SCRATCH/agent-input.json"
```

```bash
python3 "$CI_SHEPHERD_ROOT/scripts/finalize.py" \
  --agent-input "$SCRATCH/agent-input.json" \
  --agent-judgments "$SCRATCH/agent-judgments.json" \
  --output "$SCRATCH/judgments.json"
```

```bash
python3 "$CI_SHEPHERD_ROOT/scripts/validate.py" \
  --prepared "$SCRATCH/assessment-input.json" \
  --judgments "$SCRATCH/judgments.json"
```

```bash
python3 "$CI_SHEPHERD_ROOT/scripts/render.py" \
  --prepared "$SCRATCH/assessment-input.json" \
  --judgments "$SCRATCH/judgments.json" \
  --snapshot "$SCRATCH/input.json" \
  --output "$SCRATCH/report.md"
```

After final validation and rendering, `record_poc.py` records the finalized POC
cycle as an immutable run and updates its state-backed ledgers. Expanded
evidence rounds use a round-qualified snapshot identity, so baseline and
expanded judgments cannot claim the same evidence set.

```bash
python3 "$CI_SHEPHERD_ROOT/scripts/record_poc.py" \
  --state-dir "$STATE" \
  --input "$SCRATCH/input.round-1.json" \
  --prepared "$SCRATCH/assessment-input.round-1.json" \
  --judgments "$SCRATCH/judgments.json" \
  --report "$SCRATCH/report.md" \
  --artifacts "$SCRATCH"
```

For network-free lifecycle trials, place cycle directories under one scenario
directory. Each cycle must contain a frozen `input.json` and may contain a
frozen `agent-overrides.json` containing only the issue judgments that differ
from deterministic defaults. The replay rebuilds the complete
`agent-judgments.json` from current defaults plus those overrides, then reruns
prepare, compact, finalize, render, and record through one shared state
directory. It preserves the generated artifacts and writes a per-cycle ledger
delta summary. To model unchanged evidence, retain the evidence facts and
advance only `collectedAt`; identical collection identities are intentionally
rejected as duplicate immutable runs. The first replay after bootstrap may
legitimately discover cross-issue recurrence from the newly persistent
fingerprint ledger; use the following unchanged cycle to verify convergence.

```bash
python3 "$CI_SHEPHERD_ROOT/scripts/replay_scenario.py" \
  --scenario-dir "$SCENARIO" \
  --output-dir "$REPLAY" \
  --state-dir "$STATE"
```

## One-round evidence verification

The POC uses one expansion round and at most 25 requests. The purpose is to
verify recurrence, recovery, duplication, and current workflow state well
enough to choose a queue. It is not a failure-diagnosis loop.

Use this artifact flow:

```text
input.json
  -> prepare.py writes assessment-input.json
  -> compact.py writes agent-input.json
  -> request-planning agent writes evidence-requests.round-1.json
  -> validate_requests.py validates the request document
  -> expand.py writes input.round-1.json
  -> prepare.py writes assessment-input.round-1.json
  -> compact.py writes agent-input.round-1.json
  -> fresh assessment agent writes agent-judgments.round-1.json
  -> finalize.py / validate.py / render.py
```

The coordinator runs:

```bash
python3 "$CI_SHEPHERD_ROOT/scripts/compact.py" \
  --prepared "$SCRATCH/assessment-input.json" \
  --related-issues "$FIXTURE/related-issues.json" \
  --fingerprints "$STATE/ledgers/fingerprints.jsonl" \
  --output "$SCRATCH/agent-input.json"

python3 "$CI_SHEPHERD_ROOT/scripts/validate_requests.py" \
  --input "$SCRATCH/input.json" \
  --requests "$SCRATCH/evidence-requests.round-1.json"

python3 "$CI_SHEPHERD_ROOT/scripts/expand.py" \
  --input "$SCRATCH/input.json" \
  --requests "$SCRATCH/evidence-requests.round-1.json" \
  --output "$SCRATCH/input.round-1.json" \
  --errors "$SCRATCH/expansion-errors.round-1.json" \
  --audit "$SCRATCH/api-calls.jsonl"

python3 "$CI_SHEPHERD_ROOT/scripts/prepare.py" \
  --input "$SCRATCH/input.round-1.json" \
  --output "$SCRATCH/assessment-input.round-1.json" \
  --max-bundle-records 25

python3 "$CI_SHEPHERD_ROOT/scripts/compact.py" \
  --prepared "$SCRATCH/assessment-input.round-1.json" \
  --related-issues "$FIXTURE/related-issues.json" \
  --fingerprints "$STATE/ledgers/fingerprints.jsonl" \
  --output "$SCRATCH/agent-input.round-1.json"
```

`compact.py` treats an absent fingerprint ledger as empty history. Only
`record_poc.py` appends fingerprints after the finalized cycle has been
validated and immutably recorded.

### Request-planning agent contract

The request-planning agent reads only `agent-input.json` and writes only
`evidence-requests.round-1.json`. The request-planning agent emits no
judgments. It may make at most 25 requests and may request
`issue-reference` and `workflow-run` only. Every request must:

- name an `evidenceId` already present in that issue's `allowedEvidence`;
- select evidence whose availability is `partial` or `not-enriched`;
- use an exact value from `EVIDENCE_REQUEST_DECISION_GATES`;
- explain which disposition or confidence gate the requested fact can change;
- omit endpoints, query strings, repositories, branches, SHAs, paths, windows,
  methods, and bodies because the validator derives those values; and
- stay within the source issue's existing deterministic scope.

Use exactly this document shape; do not add `snapshotId`, summaries, judgments,
or other top-level or per-request fields:

```json
{
  "schemaVersion": 1,
  "repository": "microsoft/aspire",
  "round": 1,
  "requests": [
    {
      "type": "workflow-run",
      "sourceIssueNumber": 19149,
      "evidenceId": "run:31211923676",
      "decisionGate": "recovery",
      "reason": "Verify whether the directly referenced later run recovered."
    }
  ]
}
```

The exact POC `EVIDENCE_REQUEST_DECISION_GATES` values are:

```text
merged-fix
recovery
post-fix-green
no-newer-matching-failure
no-recent-matching-failure
canonical-issue
canonical-search-complete
obsolete-surface
current-failing-run
prior-resolved-episode
```

Prioritize requests that can distinguish recovered from active failures,
independent recurrence from repeated metadata, and a canonical issue from a
duplicate record. Do not spend requests merely to collect more detail. Do not
investigate root cause. A product or test failure that needs diagnosis belongs
in a separate investigation session.

The coordinator validates the request document before expansion. Invalid,
ungrounded, over-budget, or unsupported requests stop the round; they are not
silently rewritten.

### Fresh verification boundary

Do not include preliminary judgments in verifier input. The fresh assessment
agent receives no preliminary judgments, planner reasoning, or prior agent
analysis. It receives only this skill, `agent-input.round-1.json`, and the
validated list of source issue numbers from
`evidence-requests.round-1.json`. Regenerating the compact input after
expansion is mandatory; never append evidence to an earlier agent prompt.

The fresh agent copies all deterministic defaults. It spends substantive
reasoning only on requested source issues whose `reviewRequired` value is
`true`, and only when the expanded cited evidence changes a decision gate.
Every override must cite the expanded evidence ID that changed the result.
Unrequested issues retain their regenerated deterministic defaults.

Do not investigate root cause. Emit a bounded investigation handoff instead:
state the observed failure identity, the evidence already checked, the missing
fact, and the stop condition for a separate issue-focused investigation.
Do not hypothesize why a test or product failed.

### Artifact-pipeline regression protocol

Freeze source inputs and preserve every generated artifact when testing a
candidate. Inspect intermediate artifacts only to locate information loss or a
transformation error. Validated `judgments.json` is the only decision source.
Do not substitute conversation-side analysis. `action-proposals.json` is the
only source of external effects.

When an outcome is wrong: Locate the earliest incorrect artifact and replay
from frozen input. Change that stage or its prompt, then regenerate every
downstream artifact. Never repair the result by manually rewriting a later
judgment or action proposal.

## Fresh assessment-agent contract

A fresh assessment agent reads `agent-input.json` and
`review-selection.json`. It writes only evidence-supported overrides for
entries in `review-selection.json.selected`. Deterministic defaults already
apply the safe recurrence rubric; omitting a selected issue means "keep the
default." Do not return omitted issues or copy all defaults. Process selected
issues in batches of at most 10, but load each input file only once. Write only
`agent-judgments.json`. Report the number of overrides, plus category and
disposition counts, in the completion response. The coordinator owns finalized
`judgments.json`.

The deterministic selector includes every first-seen issue, every direct or
derived material change, and every seven-day scheduled reassessment. Selected
cases carry structured `changeReasons`, their previous category and disposition
when known, and prior review timing when available. This lets the agent judge
the delta instead of reconstructing history from prose. It omits unchanged
reviewed cases before their deadline, even when their deterministic default
still says review is required. This is the cheap baseline-refresh boundary:
GitHub evidence is refreshed deterministically for the full inventory, while
model reasoning is reserved for initial analysis, observed change, and a
periodic stale-understanding backstop.

State created before `review-events.jsonl` existed receives one bootstrap
assessment for each current nonsuperseded case. That migration establishes the
first durable review timestamp instead of silently treating old defaults as
fresh judgments.

## Recurring operation

Schedule a fresh local Copilot workflow with the supported cycle command above,
using the checkout that contains this skill. The workflow prompt must:

1. keep `$HOME/.copilot/ci-shepherd/state` across runs;
2. create a new timestamped scratch directory for each run;
3. run `cycle.py start`;
4. if the manifest says `awaiting-review`, read only the three bounded handoff
   files, write the typed sparse judgment files, and run `cycle.py finish`;
5. launch and record new read-only requests in `investigation-plan.json`;
6. independently validate investigation results and regenerate frozen
   `action-proposals.json`;
7. execute only exact action IDs enumerated by a valid authorization grant, in
   dependency order, stopping at its persisted mutation and chain budgets;
8. never execute a proposal without that grant or start
   `quarantine-session.json` without the required approval;
9. update the final report with investigation and action outcomes, proposals
   still needing approval, and structurally incomplete evidence; and
10. run the completed-cycle retrospective described below as the last phase.

Run daily initially. Do not overlap cycles against the same state directory;
the append-only ledgers and `current.json` have a single-writer contract.
GitHub-hosted scheduling is unsupported until the private state directory has
a durable remote persistence design.

## Final run retrospective

The retrospective is the final phase of the run. Run it only after all
investigations, authorized effects, reconciliation, ledger updates, and report
rendering are complete. A retrospective failure does not roll back completed
actions; record the failure in the operator output and preserve the completed
run artifacts for later review.

First use `run_retrospective.py seal` to snapshot the current run's matching
action and investigation ledger outcomes into `run-completion.json`. This is
the explicit post-action reconciliation marker; a completed `cycle.json` alone
is not sufficient because cycle finalization precedes external effects.

```bash
python3 "$CI_SHEPHERD_ROOT/scripts/run_retrospective.py" seal \
  --work-dir "$SCRATCH" \
  --state-dir "$STATE" \
  --sealed-at "$CURRENT_TIMESTAMP" \
  --output "$SCRATCH/run-completion.json"
```

The seal filters the persistent ledgers to action IDs in
`action-proposals.json` and investigation IDs in `investigation-plan.json`.
It records unrecorded action IDs and missing investigation results explicitly
so an interrupted or intentionally deferred phase cannot look like a clean
run.

Then use `run_retrospective.py prepare` to create the bounded handoff:

```bash
python3 "$CI_SHEPHERD_ROOT/scripts/run_retrospective.py" prepare \
  --work-dir "$SCRATCH" \
  --reviewed-session-id "$CURRENT_SESSION_ID" \
  --output "$SCRATCH/retrospective-request.json"
```

Launch one fresh, read-only retrospective reviewer in a new local session. Give
it only `workerPrompt` from `retrospective-request.json`. It may read only the
listed run artifacts and must not access GitHub, run `gh`, edit code, mutate
state, post comments, close issues, assign actors, or start implementation.
The reviewer writes its JSON response to
`$SCRATCH/agent-retrospective.json`.

Use `run_retrospective.py finalize` to validate and render the result:

```bash
python3 "$CI_SHEPHERD_ROOT/scripts/run_retrospective.py" finalize \
  --request "$SCRATCH/retrospective-request.json" \
  --result "$SCRATCH/agent-retrospective.json" \
  --json-output "$SCRATCH/retrospective.json" \
  --markdown-output "$SCRATCH/retrospective.md"
```

The validated retrospective records evidence-linked observations, future watch
conditions, and safeguards that worked in `retrospective.md`. It is advisory
and must not modify the shepherd automatically. Recommendations require later
review and a separate implementation decision.

For each prepared issue, choose a category and one or more recommendations.
Prefer `unknown` or `investigate` over unsupported certainty. Distinguish
same-run reruns from independent recovery. Surface missing positive execution
coverage. Surface missing positive execution coverage when closure, no-action,
retry, rerun, or quarantine confidence depends on a successful later execution.

Evaluate `actionCluster` before evaluating individual issue rows. Only the
canonical member may retain the cluster's substantive investigation,
quarantine, or retry recommendation. Preserve a superseded member's
deterministic `review-close` default unless frozen evidence proves the
relationship is wrong. Duplicate closure is not recovery: closing a redundant
issue record does not claim that the underlying failure stopped, so it does not
require a later successful run. The canonical member continues to own the
shared failure target. A canonical recommendation must name the shared target
and superseded issue records.

The deterministic defaults use this compact rubric. Use it to review ambiguous
defaults and avoid contradicting safe queues:

1. An exact `tier2TestName` identifies a flaky-test candidate. Do not recommend
   quarantine from `occurrenceCount` alone. A quarantine review needs at least
   two independent runs on at least two distinct days and a normalized cause
   consistent with nondeterminism. A deterministic prerequisite failure such as
   an expired emulator or unavailable dependency remains an investigation.
2. Use `independentRunCount`, `distinctDayCount`, and the normalized identity to
   distinguish recurrence from duplicate ledger rows. Classify a clear
   infrastructure cause even when it happened only once. A single transient
   occurrence remains `transient-infrastructure` and `watch`. A retry review
   needs at least three independent runs on at least two distinct days.
3. Failures that clearly block main, release, compilation, packaging, or
   repository configuration are investigations unless the prepared issue
   reports a specific decision, permission, or access question only a person
   can answer. Do not turn a generic ownership gap into `ping-human`.
4. For automation trackers, `autoclose: true` with no blockers may be no-action.
   Recurrent actionable trackers without autoclose need investigation. Missing
   or unrecognized producer ledgers need human review.
5. Do not ping a human solely because an issue is old. For an old single
   occurrence, investigate positive execution coverage; silence is not
   recovery. A complete one-off record with a citable later successful run on
   `main` may be `review-close`. The successful run must match the failed
   workflow and no contradictory blocker may remain. Without that recovery
   proof, investigate when machine-fetchable evidence remains or continue
   watching for a named future event. A future recurrence must create a new
   incident linked to the closed issue instead of reopening or reusing it.
   `review-close` requires the prepared resolution evidence and no
   contradictory blocker.
   Missing machine-fetchable evidence is `investigate`, not `ping-human`.
   `ping-human` is reserved for a decision, permission, ownership, or access
   question only a person can answer.
6. A `watch` recommendation must name its `watchReason` and the exact evidence
   event that ends the watch. `single-test-occurrence` waits for another
   independent failure on a different day. `single-infrastructure-occurrence`
   waits for recurrence or positive recovery. A generic exit code with
   unavailable logs is an investigation, not a watch. Choose `investigate`
   when useful investigation work can happen now, including fetching missing
   logs, reconciling related issues, or diagnosing repeated failures. Choose
   `watch` only after current evidence is exhausted and only a named future
   event can change the decision. An `unknown` category does not itself justify
   a status comment. `investigate` remains report-only; a status comment is
   proposed only for a genuine `watch` or when specific human input is needed.
   When an issue moves from a visible watch or human request to report-only
   investigation, retire the existing owned status comment in place.
7. `relatedIssues` is a candidate relationship, not proof of duplication.
   Aggregate `clusterOccurrenceSummary` only when the listed relationship and
   failure symptoms are compatible. Exact canonical tests with compatible
   symptoms may share recurrence evidence. Equivalent signed/hex process exit
   codes may share infrastructure recurrence evidence. If symptoms differ,
   keep the issues separate and investigate the relationship.
   An open `same-test-tracker` usually means the CI issue should be related to
   the existing failing-test tracker rather than treated as a new isolated
   occurrence. Closed tracker or quarantine history is context for
   investigation, not proof that the current failure is fixed.
8. Two independent test failures on one day do not justify quarantine, but they
   do justify investigation. Two infrastructure failures across two days remain
   below the retry-review threshold unless the failure is deterministic. A
   repeated deterministic HTTP 404 is a product or tooling investigation, not
   transient infrastructure.
9. Group bot-authored gh-aw failure issues by the stable `workflow_id` in their
   `gh-aw-failure-issue` marker, with normalized workflow name as a fallback.
   Treat each generated issue as an occurrence of that workflow failure, not
   as an independent cause. Do not combine different failure shapes merely
   because they belong to one workflow; the coordinator's `actionCluster`
   requires a compatible issue signature. The newest compatible occurrence is
   the canonical investigation owner and older compatible occurrences are
   superseded closure candidates. Use the run IDs and expiration markers to
   distinguish active failures from stale trackers. An expired gh-aw failure
   issue that remains open after later successful runs is a closure candidate
   and evidence of a producer lifecycle defect. This recovery closure is
   separate from duplicate closure.

Every `ping-human` recommendation must include `humanEscalation` with
`context`, `whyHuman`, `question`, `suggestedNextSteps`, and `routingHint`.
The question must identify the decision the human should make; "please
investigate" is not a decision. The rendered draft comment must begin with
`[automated]`, state why automation cannot proceed, ask the question, and give
concrete next steps.

Use multiple recommendations for one issue only when the targets differ. Never
split one target across multiple queues to hedge. If evidence is incomplete,
choose `investigate`, `watch`, or `ping-human` with missing evidence rather than
inventing a stronger conclusion.

## Judgment shape

`judgments.json` uses this shape:

```json
{
  "schemaVersion": 1,
  "snapshotId": "snapshot:microsoft/aspire:2026-08-20T06:00:00Z",
  "issues": [
    {
      "issueNumber": 123,
      "category": "flaky-test",
      "recommendations": [
        {
          "disposition": "review-quarantine",
          "target": { "kind": "test", "value": "Namespace.Type.Method" },
          "confidence": "medium",
          "summary": "Review the recurrent test failure for quarantine.",
          "evidenceIds": ["issue:123"],
          "missingEvidence": ["positive execution coverage"],
          "reassessWhen": "After the next rolling run has completed."
        }
      ]
    }
  ]
}
```

For `ping-human`, the recommendation also contains:

```json
{
  "humanEscalation": {
    "context": "Deployment cleanup failed ten consecutive times because its Azure tenant expired.",
    "whyHuman": "An authorized owner must choose and configure the workflow identity.",
    "question": "Should the tenant be renewed or should the workflow migrate, and who owns the change?",
    "suggestedNextSteps": [
      "Choose the identity path and owner.",
      "Update the workflow authentication configuration.",
      "Rerun the workflow and link the first successful run."
    ],
    "routingHint": "area-deployment"
  }
}
```

Allowed categories are `flaky-test`, `transient-infrastructure`,
`blocking-build`, `product-or-tooling`, `automation-tracker`, and `unknown`.

Allowed dispositions are `investigate`, `watch`, `ping-human`,
`review-quarantine`, `review-retry`, `review-rerun`, `review-close`, and
`no-action`.

Allowed target kinds are `issue`, `test`, `failure-fingerprint`, and
`workflow-run`.

Confidence is `high`, `medium`, or `low`.
