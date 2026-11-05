---
name: investigating-ci-failure-connections
description: Use when investigating Aspire main CI failure issues, broken workflows, flaky-test recurrences, attempted fixes, or failures that escaped pull-request validation.
---

# Investigating CI failure connections

Experimental prompt for a failure-driven investigation and repair loop.
Run a bounded working pass, expose its evidence, and tune the next pass from
observed mistakes. This is not a continuous watcher or authority to publish.

## Outcome

Produce a locally preserved report and repair-ready packets:

issue, failing run, or repair PR -> exact occurrence -> revision and diagnostic -> supported connection
-> why validation missed it -> repair/prevention proposal -> recovery evidence.

Use issues, observed failures, and repair PRs as discovery inputs, not an
unrestricted download of every pipeline log. A regression need not have an
issue to enter the investigation.
Inventory open and historical `ci-failure-cause` and `automation-broken` issues,
then search for relevant workflow reports without either label and open PRs
that may repair or prevent the observed failures. Include public
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
calendar months or elapsed days. Old evidence supports retirement review,
not proof of recovery. Removed main execution lanes can still matter to
supported release branches.

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
