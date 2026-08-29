# CI Shepherd Continuation Plan

**Agreed direction:** run a cheap evidence refresh and selective assessment
regularly, preserve deterministic JSON as the authority, avoid repetitive
comments, and keep every GitHub-visible action behind explicit per-action
approval.

Current implementation and evidence are recorded in
[CI Shepherd Implementation Status](../status/2026-08-28-ci-shepherd-implementation.md).

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

## Phase 3: Exercise Staged Live Actions

Use the staged vertical-slice approach from the full-cycle design. For each
slice:

1. Show the exact proposal, full visible text, cited evidence, expected result,
   and abort behavior.
2. Obtain approval for one exact action ID.
3. Execute only that action.
4. Reconcile the live GitHub result.
5. Recollect into a new cycle.
6. Confirm the result is observed and the same action is not proposed again.

Recommended order:

- [ ] Edit or create one canonical shepherd status comment where the message
  provides clear new value.
- [ ] Execute one evidence-backed comment-plus-close sequence for a resolved or
  superseded issue.
- [ ] Launch one bounded investigation session for a concrete unresolved case;
  do not select an item merely because Copilot is assigned.
- [ ] Exercise one repository-native quarantine or rerun path only after its
  exact command and resulting pull request or run are understood.
- [ ] Exercise a pull-request `ping-human` comment and later confirm that the
  terminal retirement edit is stable.

No slice authorizes later slices. A stale preflight or failed reconciliation
stops the slice.

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

These changes should follow observed shepherd reports rather than speculative
rewrites of all issue-opening automation.

## Phase 5: Decide Whether to Expand Automation

After the read-only and staged-action trials, decide separately whether to add:

- watched labels;
- automatic low-risk comment edits;
- automatic closure of deterministic duplicates or recovered incidents;
- multi-repository inventory;
- GitHub-hosted scheduling;
- remote or replicated state;
- multiple concurrent workers;
- batch approval.

Default answer remains no. Each expansion requires evidence that the current
manual approval cost is the limiting problem and a testable rollback or
reconciliation story.

## Stop Conditions

Disable the workflow and investigate if:

- a non-GET call appears during assessment;
- an incomplete inventory reads as complete;
- the same unchanged status body is proposed repeatedly after execution;
- an item assigned to Copilot is selected for work;
- a pull request is proposed for closure or merge;
- one issue's malformed evidence aborts unrelated approved work;
- state is advanced after an interrupted or failed cycle;
- two writers use the same state directory.

## Deferred Design Work

Do not implement these as part of the next operational trial:

- autonomous GitHub mutation;
- bulk execution;
- remote multi-writer state;
- automatic rollback;
- generalized retries;
- labels used only to mirror local state;
- a separate service or queue.

The next concrete action is Phase 1: make the committed implementation
available to the stable workflow checkout, run the disabled workflow manually,
and compare it with the recorded live cycle before enabling the schedule.
