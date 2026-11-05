---
name: investigating-ci-failure-connections
description: Use when investigating Aspire main CI failure issues, broken workflows, flaky-test recurrences, attempted fixes, or failures that escaped pull-request validation.
---

# Investigating CI failure connections

Experimental prompt for a failure-driven investigation and repair loop.
Run a bounded working pass, expose its evidence, and tune the next pass from
observed mistakes. This is not a continuous watcher or authority to publish.

## Current investigation focus

Inventory relevant reports broadly at metadata level, then prioritize the
latest actual failures, generally in the last one to two weeks. This is a
priority window, not an exclusion cutoff. Cover individual cause families,
repair PRs and workflow reports within the assigned scope. One workflow
investigator handles main CI, outerloop, quarantine, scheduled and internal
workflow reports using the same evidence and lifecycle rules.
Follow history and attempted fixes to answer concrete current-pattern
questions, not to perform a full historical log sweep or restart deferred work.
A run referenced by an umbrella can still supply evidence for an individual
failure. A cause-specific tracker is not excluded merely because it records
repeated occurrences of the same signature.

## Outcome

Produce a locally preserved decision report and, for selected substantive
claims, repair-ready packets:

issue, failing run, or repair PR -> exact occurrence -> revision and diagnostic -> supported connection
-> why validation missed it -> repair/prevention proposal -> recovery evidence.

Use issues, observed failures, and repair PRs as discovery inputs, not an
unrestricted download of every pipeline log. A regression need not have an
issue to enter the investigation.
Inventory open and historical `ci-failure-cause` issues within the current
focus, then search for related unlabeled reports and open repair PRs.
Include bounded `automation-broken` and umbrella-workflow discovery. Include public
GitHub Actions CI, internal official builds, outerloop/quarantine, and relevant
scheduled workflows. Record query scope, pagination, collection time and gaps.

Read applicable repository instructions and use existing CI investigation
skills/tools. Reuse receipts with matching identities. Loading a skill does
not authorize a remote build, publisher, or other side effect.

## First working pass

1. Establish the run's objective, scopes, permissions, evidence bounds and
   output paths. Inventory open and historical reports at metadata level;
   inspect the latest actual failures first. Record unexamined candidates.
2. Record run, attempt, job, failing revision, exact diagnostic and artifact
   identity. Separate primary failure, downstream consequence, and unknown.
3. Join matching historical records and attempted fixes, including open PRs.
   Verify entity type, state, target branch, head/merge revision and relevant
   diff against primary sources. Follow older PRs and git history as far as
   the causal question requires; bound collection, not relevant history.
4. Prove a failing revision contains a fix before calling it a recurrence.
   Then compare signatures: a new mechanism in the same test is not proof the
   old fix failed. A late-filed issue can describe a pre-fix run.
5. For selected substantive recurrence, causal or repair claims, investigate
   why the affected consumer escaped original PR validation. Administrative
   dispositions do not each require deep repair or escape analysis.
6. Use independent evidence review for material claims as described below.
   Reconcile once, preserve disagreements, and name remaining questions.

## Classify before choosing a handler

Record independent dimensions, not one label inferred from the issue title:

- `trackerScope`: individual-cause, active-main-health or reused-workflow-health.
- `observedFailureKind`: test-failure, build-failure, execution-disruption, mixed
  or unknown; include the diagnostic and source.
- `failingStage`: setup, build, test-execution, artifact/publish or unknown.
- `intermittentStatus`: unproven unless matched contrasting executions support
  intermittency for the comparable scenario, lane and revision.

A workflow-related unit test is still an individual test failure. A compiler
error inside a template test is a build failure at test-execution stage.
An exit-only message can establish the stage without establishing the kind.
Execution disruption does not assign fault to infrastructure. Keep distinct
signatures and primary/cascade evidence separate.

### Individual test failures

Inspect the exact test/case, exception/assertion, occurrence identities and
attempted repairs. Do not infer flakiness from the title or repetition.
Use verified repairs or the individual-cause age rules for closure proposals.

### Individual build/job failures

Inspect the actual failing operation, first supported diagnostic and downstream
consequences. A test-named job can fail before tests run. Unknown causes stay
unknown; do not merge generic exits. Individual-cause age rules apply.

### Workflow-health reports

For main, outerloop, quarantine, scheduled and internal scopes, inspect the
latest failure-bearing body/comment, linked attempt, newer relevant executions
and reporter lifecycle contract. Follow actual failure evidence, not the last
administrative comment or issue creation date. A reused tracker created months
ago remains current when it records recent actual failures.

For health trackers, distinguish branch, workflow, event and matrix/policy
eligibility. Preserve changed failures between attempts. Green notices,
reopens and repeated comments are not distinct same-signature occurrences.
A later eligible green may retire the current incident without repairing or
erasing a recurring cause family. Some reporters auto-close on green and
others retain trackers; check the contract rather than assuming one lifecycle.
Use workflow relevance/currentness dispositions below, not individual-cause
age cutoffs. A successful individual job does not establish whole-main health.

## Relationship and lifecycle rules

Use explicit relationships: duplicate, shared fixture/fix, producer-consumer
cascade, blocks-validation-of, same scenario/different signature, or unproven.
Matching names, timing, host exit codes or green headlines do not establish
causality. Do not assign a common root cause from a shared timeout alone.

### Verified cause duplicates

For `ci-failure-cause` issues, compare the specific diagnostic, scenario and
scope against primary occurrence evidence, not titles or generic timeouts or
exit codes. A verified family can span distinct runs. The same test with a
different exception/signature is not a duplicate without additional evidence.

Choose useful active canonical tracking. Before retiring a duplicate, preserve
the union of distinct occurrences and unique evidence in the canonical record
or an accessible linked evidence index, and link the duplicate to the canonical.
Do not retire the sole active tracker into a closed obsolete canonical as
though a current failure were resolved; keep useful active tracking or obtain
authority for the necessary canonical update. Missing evidence that could
change the duplicate decision requires a hold.

### Individual-cause age retirement

Compute last-seen across the canonical family, including reopened duplicates
and comments containing actual occurrences. Use calendar-month cutoffs and
the latest actual occurrence, not the
issue's creation date or a recent administrative comment. Under the user's
age policy, a latest occurrence strictly older than three calendar months is
sufficient for stale-record closure; proof of repair or a fresh green run is
not required. Unknown dates, incomplete occurrence lists or a newer matching
duplicate prevent an age-qualified decision. Describe this as retirement of
the recorded incident, not proof of recovery.

For a family with exactly one distinct recorded failure, also propose stale
retirement when that occurrence is strictly older than two calendar months.
Count across the issue, known matching duplicates, comments and linked failure
evidence. Repeated mentions of the same run/attempt/job/test count once;
distinct failing jobs or attempts count separately. Unknown identities or
incomplete evidence require a hold, not an assumed singleton. Multi-hit
families retain the general three-calendar-month rule.

Show the distinct occurrence count, latest actual occurrence, cutoff and
qualifying rule in each pruning row. Individual-cause age retirement retains
the exact-draft approval requirement; delegation of workflow retirement or
verified duplicates does not authorize this separate category.

Before an age-based closure, inspect linked repair PRs and cross-references for
newer matching failure evidence omitted from the issue's occurrence table.
Verify cited run/job diagnostics before changing the last-seen date. An open
repair alone does not block stale retirement; a newer verified failure does.

### Attempted repairs and execution history

Inspect recent maintainer comments, including "already fixed" recommendations.
Record the author, exact claim, date and comment link; author identity alone
does not establish a fix. Verify a linked repair's merged state and relevant
change, then compare the latest failure revision with the repair. A tentative
claim without supporting evidence is not a confirmed fix, but does not block
an independently age-qualified closure. Keep stale retirement, verified repair
and removal of an execution lane as separate closure reasons. Removed main
execution lanes can still matter to supported release branches.

Preserve attempt history. The latest attempt's failures are not the union of
every attempt. Canceled runs can contain real failures; skipped tests are not
passes. Verify the affected scenario actually executes after a fix.

Separate a proposed repair, merged repair, prevention-only change and verified
recovery. An open PR is not a fix on main, and a repair claim is not causal
proof. Check existing repair work before proposing a new assignment; prepare
missing evidence or validation for that work instead of duplicating it.

## Workflow relevance and disposition

Judge relevance, currentness and actionability within verified workflow,
branch, event and matrix/policy scope. There is no fixed day threshold,
minimum open-report quota or mandatory age-based protection. Verify cadence
from primary configuration when it matters: silence on an infrequent workflow
is weaker evidence than comparable executed scope. Do not require diagnosis
of all historical failures before disposing of a superseded report.

Distinguish these dispositions and state the evidence and tracking consequence:

- **Active/current:** a relevant current failure needs active tracking.
- **Recurring-cause:** retain useful cause tracking separately from a recovered
  workflow episode; recovery of one execution does not repair the family.
- **Recovered:** a newer completed eligible green establishes recovery only
  for the affected scope actually executed, with no newer contrary evidence.
  Skips, cancellation or a narrower green do not recover broader scope.
- **Stale/obsolete:** the report no longer has relevant actionable content;
  justify this from current evidence and cadence, not age alone.
- **Superseded:** a current report covers the relevant scope and the older
  record has no unique unresolved actionable information. The older report
  can retire even while a different current failure remains red.
- **Insufficient-evidence:** hold only when a named missing fact could change
  the action. An unknown root cause does not itself make disposition unknown.

Preserve closed-history evidence and links; never erase comments. Name the
current tracker for superseded reports and preserve any unique actionable
information before retirement. Do not imply permanent repair from a green
execution or housekeeping. These judgments do not change the separate
three-calendar-month general or two-calendar-month verified-singleton
individual-cause policies.

## Escape analysis and prevention

For each repair packet, assess these independently:

- **Admission:** Did public CI, internal CI or the necessary scheduled workflow
  run on this PR/revision? Did selection omit a runtime-only consumer?
- **Policy/environment:** Did manual versus automatic, run versus publish,
  OS/SDK/signing, credentials or other relevant settings change the path?
- **Detection:** Did assertions check the broken contract, not just build
  success? Did a failed prerequisite create misleading downstream failures?
- **Review:** What did human/Copilot reviews actually request or miss? Cite
  discussion and receipts; unknown review coverage is not reviewer blame.

Recommend the smallest falsifiable safeguard: a behavior test, script guard,
safe consumer check, pipeline routing, specialized scanner or missing reviewer
decision rule. Check existing instructions before adding more. Linting,
security review, security scanning and pipeline execution are distinct evidence.
Do not weaken signing/security checks to obtain a green result.

## Collaboration and report

Use bounded, non-overlapping assignments: issue families, a concrete domain's
escape analysis, and independent evidence/process challenge. Follow the user's
model policy. The challenger must not merely validate its own findings.
The investigator prepares repair work; a repair agent implements only an
authorized specific assignment.

Use the reusable [worker prompts](worker-prompts.md) with explicit inputs,
evidence bounds, output contracts and stopping conditions. Use one workflow
investigator by default; the same prompt may cover independent workflow
scopes separately when useful. Do not duplicate scope or require separate
status, age-screening or main-only assignments. Independent evidence review
remains a separate role. Report actual assignments, not hypothetical delegation.
Each worker returns observed prompt traps and proposed improvements. Preserve
the triggering evidence and a counterexample; exercise a changed prompt before
claiming improvement. Prompt iteration is not model training.

### Independent audits and checkpoints

Use a separate primary-review context periodically, especially for a changed
retirement rule or material correlation claim before acting on it. Resolve its
model and effort from the user's current selection and role policy; verify
availability. Bound the audit to explicit claims, cases and receipts, and
record supported/narrowed/hold decisions. A cancelled, missing or incomplete
audit is not acceptance. Reconcile material objections before execution.

Exercise changed rules on frozen cases before and after the edit, including
supersession, recent reused trackers, duplicate signatures, unknown coverage,
individual-cause cutoff boundaries and permission counterexamples.
Record omissions and regressions, not only passing cases. Checkpoint coherent
improvements with previewed commit messages; push to the authorized branch and
destination only when requested, then verify the remote revision.

Each packet contains tracking links, exact symptom, candidate mechanism,
confidence, counterevidence, prior fix/ancestry, escape analysis, proposed
repair/prevention, and an observable acceptance criterion. Separate confirmed
facts, hypotheses, retirement candidates and blocked validation.

Keep the current report and raw evidence in session artifacts. Summarize
coverage and gaps; link detailed family reports. Do not put downloaded logs,
private receipts or machine paths into committed/public prose.

Start the report with a short decision overview: a table of issues or groups
being tracked, supported findings, recommendations and action confidence.
Show tracker age, latest recorded actual failure date across verified family
members, and elapsed time since that hit. Unknown dates remain unknown;
issue updates and closure recommendations are not failure occurrences.
List full verified group membership and relationship type, not just example
pairs, without merging distinct signatures to manufacture larger groups.
For retirement, state the applicable rule, evidence limits and exact closure
text; include cutoffs only for individual-cause age decisions. Confidence in
retirement is not confidence a bug was fixed.
State authorization and important uncertainty beside that overview. Inventory
counts must not imply diagnostic coverage. Keep detailed evidence, history, measurements
and process improvements below it; record prompt checkpoints and what each
improvement addresses in the local evaluation history. Return the report,
action receipts (or none), evidence gaps, observed prompt traps and next
bounded queue. Do not turn that queue into unrequested work.

Measure inventory/evidence coverage, independently adjudicated false merges
and false splits, causal-claim precision, eligible post-fix executions,
recurrence, detection-to-handoff latency, and cost per accepted finding.
Use historical replay with an evidence cutoff; allow abstention. Report
unmeasured metrics as unmeasured, not zero. Closure count is not success.
Compute replay conclusions from evidence available at the cutoff only; keep
later observations in a separate current-history view.

## Per-run authority and decision comments

Default to read-only without applicable delegation. Record this run's
authorization source, categories, scope and limits. Explicitly delegated
workflow stale/superseded/recovered retirement, verified cause duplicates or
short material decision comments may execute after live evidence checks
without redundant per-item approval. Record the exact comment/reason,
canonical/current tracker link and verified receipts for each action.
Categories not delegated remain proposals. Prior batch receipts authorize
neither new rows nor retries.

Preserve any explicit draft-approval requirements that still apply to the
category; do not let an unconditional per-item gate negate newer explicit
delegation. General investigation permission is not write authority. New
issues/PRs, product fixes, remote builds/reruns, schedules and unrelated writes
need their own authority and applicable draft approval.

Use short `[automated] ` decision comments, normally one to three sentences:
the actual decision, concise evidence, and a canonical/current tracker or
relevant run link. Do not comment on every unchanged keep or repeat status
spam. Housekeeping must not imply a fix.

Before writing, check live state, body, comments and decision-changing
evidence. Record each action's authorization, evidence, exact text and reason;
show drafts and obtain approval where required. Preserve evidence links before
closing duplicates. After writing, verify final state, state reason and exact
comment. Persist uncertain outcomes and partial errors; inspect live
state/comments before any authorized retry, never blindly repeat a mutation.

Record observed process failures locally, propose a small prompt revision,
then evaluate the changed prompt afresh on the same cases. Label prototype
evaluations honestly; do not claim continuous operation or proven accuracy.
