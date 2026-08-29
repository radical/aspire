# CI Shepherd Continuation Plan

**Agreed direction:** run a cheap evidence refresh and selective assessment
regularly, preserve deterministic JSON as the authority, avoid repetitive
comments, execute independently validated low-risk actions in the same run
under a configured pre-authorization policy, and keep higher-risk actions
behind explicit per-action approval.

Current implementation and evidence are recorded in
[CI Shepherd Implementation Status](../status/2026-08-28-ci-shepherd-implementation.md).

The staged live-action trials exposed authorization, crash-recovery, proposal
integrity, and observability defects. All further live mutation is suspended
until the tracked
[CI Shepherd Safety Remediation Plan](2026-08-29-ci-shepherd-safety-remediation.md)
passes its local and `radical/aspire` gates.

## Completed Foundation

- [x] Collect all target-labeled issues and pull requests.
- [x] Collect all open bot-authored issues and pull requests within explicit
  scan budgets.
- [x] Exclude Copilot-assigned items without using assignment as a selection
  signal.
- [x] Refresh factual evidence cheaply and reuse unchanged evidence.
- [x] Send every first-seen or materially changed issue case to the assessment
  model, including changed cases whose deterministic default is unambiguous.
- [x] Reassess unchanged issue and pull-request cases after seven days without a
  review.
- [x] Persist actual review events separately so evidence-only refreshes do not
  reset the reassessment clock.
- [x] Carry structured wake reasons, prior buckets, and review timing into the
  bounded handoff.
- [x] Handle pull requests conservatively and suppress low-value watch
  comments.
- [x] Keep generic unknown failures with unavailable diagnostics in a
  report-only investigation queue instead of posting passive watch comments.
- [x] Persist bounded read-only investigation results by issue, target, and
  source-evidence fingerprint; reuse unchanged results and discard stale ones
  from later assessment input.
- [x] Limit each cycle to five new investigation sessions, defer the remaining
  requests deterministically, and attach all matching target-specific results
  without allowing one result to overwrite another.
- [x] Build one deterministic local quarantine proposal containing every current
  test candidate and its original issue URL.
- [x] Enforce one active quarantine session with a serialized ledger update,
  require restore plus affected-test project builds, record partial success
  precisely, and distinguish an open quarantine PR from merge-confirmed
  completion.
- [x] Aggregate duplicate issue owners for the same test, preserve every source
  issue in the PR handoff, and recover or fail an abandoned session from a later
  cycle.
- [x] Recognize complete one-off incidents with a directly cited later
  successful `main` run from the same workflow as closure candidates when no
  contradictory blocker remains; never use age or silence alone as recovery.
- [x] Retire an existing owned watch or human-request comment when the issue
  moves to report-only investigation.
- [x] Preserve a deterministic full default document and accept sparse model
  overrides.
- [x] Render the Markdown report deterministically from validated JSON.
- [x] Produce exact, individually approvable proposals.
- [x] Persist immutable cycle artifacts and lifecycle ledgers.
- [x] Validate a full live read-only cycle and an incremental replay.
- [x] Create a disabled local daily workflow using a low-cost model.
- [x] Execute and reconcile a frozen review-close batch: 26 new comment/close
  pairs completed, one terminal-ledger duplicate was skipped, and recollection
  reproposed none of the 27 cases.
- [x] Exercise one exact quarantine session and reconcile it without an empty
  commit or misleading pull request when the test was already quarantined on
  `origin/main`.
- [x] Exercise bounded investigation at concurrency one, independently reject
  unsupported initial verdicts, and persist corrected evidence-backed results.
- [x] Define a final fresh, read-only retrospective over bounded run artifacts.

## Phase 1: Land and Activate the Read-Only Loop

- [ ] Land commit `443e989126` on a branch available to the stable Aspire
  project checkout.
- [ ] Confirm the scheduled workflow checkout contains
  `.ci-shepherd-build/scripts/cycle.py`.
- [ ] Confirm `$HOME/.copilot/ci-shepherd/state` is owner-only and not shared by
  another writer.
- [ ] Run the disabled workflow manually once.
- [ ] Compare its inventory, review selection, report, proposals, GET audit, and
  immutable run with the final manual live cycle.
- [ ] Enable the daily workflow only after the manual run is equivalent and
  contains no GitHub mutations.

Success criteria:

- Open-bot scan is `complete`, or a non-complete state is prominent.
- Every audited GitHub request is `GET`.
- No action result is written.
- Stable reviewed issues and pull requests are omitted from model input until
  they change or reach the seven-day reassessment deadline.
- The report, proposals, and dry-run regenerate deterministically.

## Phase 2: Measure Read-Only Quality

Run the read-only workflow for several days before approving effects. Track:

- total issues and pull requests in scope;
- collection completeness and errors;
- API call count by endpoint family;
- selected versus omitted issue and pull-request counts;
- reassessments caused by direct source changes, derived evidence changes, and
  the seven-day backstop;
- deterministic defaults versus agent overrides;
- proposal count and proposal-body churn;
- cases repeatedly classified `investigate` without a concrete next question;
- cases where an evidence budget prevents a useful decision;
- newly opened bot issues that duplicate an existing active problem.

Do not add a watched label yet. Reconsider it only if local state and canonical
comments are insufficient for operators to understand ownership.

Exit criteria:

- Incremental runs remain materially cheaper than the bootstrap run.
- Stable watch cases do not produce repeated comment edits.
- Unknown cases produce a status comment only when all current investigation
  work is exhausted and a named future event is the remaining gate.
- New recurring failures transition predictably.
- Unchanged cases are reviewed at most once per seven days, and a completed
  review resets that deadline.
- Closure proposals cite current deterministic recovery or duplicate evidence.
- Human escalations contain one answerable question and a routing hint.

## Phase 3: Exercise and Promote Staged Live Actions

The vertical slices proved the execution and reconciliation path. During a
staged trial:

1. Show the exact proposal, full visible text, cited evidence, expected result,
   and abort behavior.
2. Obtain approval for one exact action ID.
3. Execute only that action.
4. Reconcile the live GitHub result.
5. Recollect into a new cycle.
6. Confirm the result is observed and the same action is not proposed again.

Keep a separate local Markdown trial log for these slices. The structured
proposal and result files remain authoritative; the log is the operator view of
the issue or test currently being processed, its intended action, evidence,
action IDs, current phase, preflight result, terminal result, and post-action
reconciliation. Update the same entry as the slice progresses instead of
creating disconnected progress notes.

Recommended order:

- [x] Edit or create one canonical shepherd status comment where the message
  provides clear new value.
- [x] Execute one evidence-backed comment-plus-close sequence for a resolved or
  superseded issue.
- [x] Launch and record one bounded read-only investigation for a concrete
  unresolved case. Recollect afterward and confirm unchanged evidence reuses the
  result while changed evidence creates a new request.
- [x] Approve one exact `quarantine-session.json` batch, create one local
  worktree session, run QuarantineTools for every listed test, build each
  affected test project, review the draft PR title/body, and only then push and
  open the draft PR. The trial correctly stopped before commit and PR creation
  because the target was already quarantined on `origin/main`.
- [ ] Recollect after the quarantine PR is opened and confirm its tests are
  suppressed only as in-flight work. Reconcile merge or close state, record
  `completed` only after merge, and confirm closed-unmerged work becomes
  eligible again. Keep the original failure issues open.
- [ ] Exercise a pull-request `ping-human` comment and later confirm that the
  terminal retirement edit is stable.

The former prose pre-authorization is revoked. Sequential execution did not
bound total impact, and `requiresSeparateApproval` was not an executable gate.
Future mutations require an exact, expiring, snapshot-bound authorization grant
with a persistent mutation budget. No such grant exists until the safety
remediation plan is implemented and validated.

## Phase 4: Improve the Issue Producers

The shepherd manages symptoms; the issue-opening workflows should still be
improved where deterministic producer changes can prevent noise.

- [ ] Separate recurring problem identity from individual `ci/main` incident
  episodes.
- [ ] Reuse an active canonical issue when the deterministic failure identity
  matches.
- [ ] Do not reopen a human-closed issue merely because its old identity
  matched; create or link a new episode according to an explicit producer
  policy.
- [ ] When a recovered one-off incident is closed, create a new linked incident
  for a future recurrence instead of reopening or reusing the closed record.
- [ ] Preserve structured occurrence metadata so the shepherd does not have to
  parse prose or truncated comments.
- [ ] Ensure producer-created issues expose enough run, job, test, and branch
  identity to distinguish recurrence, regression, and unrelated failures.
- [ ] Add producer-side tests for close, reopen, reuse, and duplicate behavior.
- [ ] Correlate incident episodes by workflow and stable failure fingerprint
  across issues and time. A recovered transient episode may still close, but a
  recurring cluster should raise a pattern signal on the newest relevant issue
  with links to similar episodes, occurrence counts across distinct runs and
  days, and a recommendation to investigate a durable fix.
- [ ] Surface the pattern on the current issue rather than creating a separate
  meta-issue initially. The comment must distinguish the latest episode from
  the recurring failure family and must remain idempotent as more occurrences
  arrive.

These changes should follow observed shepherd reports rather than speculative
rewrites of all issue-opening automation.

## Phase 5: Decide Whether to Expand Automation

After the read-only and staged-action trials, decide separately whether to add:

- watched labels;
- [x] automatic low-risk comment edits under the frozen-proposal policy;
- [x] dependent closure of deterministic duplicates or recovered incidents
  after comment reconciliation;
- multi-repository inventory;
- GitHub-hosted scheduling;
- remote or replicated state;
- multiple concurrent workers;
- batch approval.

Unmarked expansions remain disabled. Each expansion requires evidence that the
current bounded design is the limiting problem and a testable rollback or
reconciliation story.

Every completed run ends with one fresh read-only retrospective reviewer. It
starts only after reconciliation and final report rendering create
`run-completion.json`, an explicit seal containing the run's matching action
and investigation ledger outcomes. It then reads only allowlisted run
artifacts and produces validated `retrospective.json` plus deterministic
`retrospective.md`. Its findings are advisory; they do not modify GitHub,
state, or shepherd code automatically.

## Stop Conditions

Disable the workflow and investigate if:

- a non-GET call appears during assessment;
- an incomplete inventory reads as complete;
- the same unchanged status body is proposed repeatedly after execution;
- an item assigned to Copilot is selected for work;
- a pull request is proposed for closure or merge;
- a stale investigation result appears after its source evidence changes;
- a second quarantine session starts while another is active;
- a quarantine worker skips restore, an affected-project build, or exclusion
  verification;
- a merge-confirmed quarantine test is proposed again;
- an open or closed-unmerged quarantine PR is treated as permanently completed;
- one issue's malformed evidence aborts unrelated approved work;
- state is advanced after an interrupted or failed cycle;
- two writers use the same state directory.

## Deferred Design Work

Do not implement these as part of the next operational trial:

- broad or unbounded GitHub mutation;
- concurrent mutation;
- remote multi-writer state;
- automatic rollback;
- generalized retries;
- labels used only to mirror local state;
- a separate service or queue.

The next concrete action is one full end-to-end run with investigation
concurrency one, same-run execution of eligible pre-authorized actions,
reconciliation, final report rendering, and the fresh retrospective. Repeat the
same full run to verify idempotency, evidence reuse, stable reports, and useful
retrospective findings before increasing investigation concurrency.
