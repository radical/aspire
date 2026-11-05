---
name: investigating-ci-failure-connections
description: Use when investigating Aspire main CI failure issues, broken workflows, flaky-test recurrences, attempted fixes, or failures that escaped pull-request validation.
---

# Investigating CI failure connections

Experimental prompt for a failure-driven investigation and repair loop.
Run a bounded working pass, expose its evidence, and tune the next pass from
observed mistakes. This is not a continuous watcher or authority to publish.

## Current investigation focus

Prioritize individual test-failure families, fresh regressions and their repair
PRs, starting with stale-issue pruning proposals. Reused workflow-health
umbrella trackers are deferred until the user reinstates them; retain prior
evidence without marking them recovered.
A run referenced by an umbrella can still supply evidence for an individual
failure. A cause-specific tracker is not excluded merely because it records
repeated occurrences of the same signature.

## Outcome

Produce a locally preserved report and repair-ready packets:

issue, failing run, or repair PR -> exact occurrence -> revision and diagnostic -> supported connection
-> why validation missed it -> repair/prevention proposal -> recovery evidence.

Use issues, observed failures, and repair PRs as discovery inputs, not an
unrestricted download of every pipeline log. A regression need not have an
issue to enter the investigation.
Inventory open and historical `ci-failure-cause` issues within the current
focus, then search for related unlabeled reports and open repair PRs.
Expand `automation-broken` and umbrella-workflow discovery when that scope is
reinstated. Include public
GitHub Actions CI, internal official builds, outerloop/quarantine, and relevant
scheduled workflows. Record query scope, pagination, collection time and gaps.

Read applicable repository instructions and use existing CI investigation
skills/tools. Reuse receipts with matching identities. Loading a skill does
not authorize a remote build, publisher, or other side effect.

## First working pass

1. Inventory the issues; inspect promising families and recent issue-linked
   failures. Deliver a few actionable cases before exhaustive historical work.
2. Record run, attempt, job, failing revision, exact diagnostic and artifact
   identity. Separate primary failure, downstream consequence, and unknown.
3. Join matching historical records and attempted fixes, including open PRs.
   Verify entity type, state, target branch, head/merge revision and relevant
   diff against primary sources. Follow older PRs and git history as far as
   the causal question requires; bound collection, not relevant history.
4. Prove a failing revision contains a fix before calling it a recurrence.
   Then compare signatures: a new mechanism in the same test is not proof the
   old fix failed. A late-filed issue can describe a pre-fix run.
5. Investigate why the affected consumer escaped original PR validation.
6. Have an independent challenger review the evidence and proposed handoff.
   Reconcile once, preserve disagreements, and name remaining questions.

## Relationship and lifecycle rules

Use explicit relationships: duplicate, shared fixture/fix, producer-consumer
cascade, blocks-validation-of, same scenario/different signature, or unproven.
Matching names, timing, host exit codes or green headlines do not establish
causality. Do not assign a common root cause from a shared timeout alone.

Compute last-seen across the canonical family, including reopened duplicates
and comments containing actual occurrences. State whether three months means
calendar months or elapsed days. Use the latest actual occurrence, not the
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

The single-hit cutoff is two calendar months; a 45-day alternative is not
enabled. Show the distinct occurrence count, latest actual occurrence, cutoff
and qualifying rule in each pruning row. An age-qualified proposal still
requires the existing exact-draft approval gate before external action.

Before an age-based closure, inspect linked repair PRs and cross-references for
newer matching failure evidence omitted from the issue's occurrence table.
Verify cited run/job diagnostics before changing the last-seen date. An open
repair alone does not block stale retirement; a newer verified failure does.

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
For pruning, state the cutoff, evidence limits and exact proposed closure text;
confidence in policy-based retirement is not confidence a bug was fixed.
State approval status and important uncertainty beside that overview. Inventory
counts must not imply diagnostic coverage. Keep detailed evidence, history, measurements
and process improvements below it; record prompt checkpoints and what each
improvement addresses in the local evaluation history.

Measure inventory/evidence coverage, independently adjudicated false merges
and false splits, causal-claim precision, eligible post-fix executions,
recurrence, detection-to-handoff latency, and cost per accepted finding.
Use historical replay with an evidence cutoff; allow abstention. Report
unmeasured metrics as unmeasured, not zero. Closure count is not success.
Compute replay conclusions from evidence available at the cutoff only; keep
later observations in a separate current-history view.

Default to read-only. Present exact drafts and obtain applicable authorization
before external updates; do not close, comment, queue, rerun, schedule, push,
or launch repair implementation as an incidental investigation step.
Before any issue comment or closure, show an approval table containing the
issue link, proposed action, evidence/reason, exact comment or closure text
and approval status. Long drafts may be separately numbered and linked from
their rows. Start every row as pending; execute only the specific actions and
wording the user approves. General permission to investigate is not approval
to engage issues. Apply the required automated attribution to proposed drafts.
Record observed process failures locally, propose a small prompt revision,
then evaluate the changed prompt afresh on the same cases. Label prototype
evaluations honestly; do not claim continuous operation or proven accuracy.
